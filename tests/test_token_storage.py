"""Token coverage, bounded daily buckets, and schema migration regressions."""

import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from storage import Identity, StorageCapacityError, StorageError, Store

BEFORE_MIDNIGHT = datetime(2026, 10, 18, 15, 59, 59, tzinfo=timezone.utc)
AFTER_MIDNIGHT = BEFORE_MIDNIGHT + timedelta(seconds=1)


class TokenStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "usage.sqlite3"
        self.store = Store(self.path)
        self.user = Identity("qq-main", "100", "200", "300", "测试用户")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def record(self, total=10, *, identity=None, at=BEFORE_MIDNIGHT, failed=False):
        self.store.record_llm(
            identity or self.user,
            "provider",
            "model",
            23,
            None,
            None,
            total,
            failed,
            occurred_at=at,
        )

    def test_new_user_query_is_zero_and_does_not_allocate_rows(self):
        summary = self.store.token_summary(self.user, now=AFTER_MIDNIGHT)
        self.assertEqual(summary["today"], "2026-10-19")
        self.assertEqual((summary["total_tokens"], summary["today_llm_calls"]), (0, 0))
        self.assertEqual(summary["started_at"], summary["daily_started_at"])
        self.assertEqual(
            self.store.token_leaderboard(self.user)["started_at"],
            summary["started_at"],
        )
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM daily_tokens").fetchone()[0], 0
            )

    def test_beijing_midnight_resets_daily_without_changing_lifetime(self):
        self.store.record_llm(
            self.user, "p", "m", 23, 4, 6, None, occurred_at=BEFORE_MIDNIGHT
        )
        before = self.store.token_summary(self.user, now=BEFORE_MIDNIGHT)
        self.assertEqual(before["today"], "2026-10-18")
        self.assertEqual(before["today_total_tokens"], 10)
        expired = self.store.token_summary(self.user, now=AFTER_MIDNIGHT)
        self.assertEqual(
            (expired["total_tokens"], expired["today_total_tokens"]), (10, 0)
        )
        self.assertEqual(expired["today_llm_calls"], 0)
        self.store.record_llm(
            self.user, "p", "m", 0, 2, 3, None, occurred_at=AFTER_MIDNIGHT
        )
        after = self.store.token_summary(self.user, now=AFTER_MIDNIGHT)
        self.assertEqual((after["total_tokens"], after["today_total_tokens"]), (15, 5))
        self.assertEqual(
            (after["today_input_tokens"], after["today_output_tokens"]), (2, 3)
        )
        self.assertEqual((after["llm_calls"], after["today_llm_calls"]), (2, 1))

    def test_older_completion_cannot_overwrite_newer_day(self):
        self.record(7, at=AFTER_MIDNIGHT)
        self.record(100, at=BEFORE_MIDNIGHT)
        self.record(3, at=AFTER_MIDNIGHT)
        summary = self.store.token_summary(self.user, now=AFTER_MIDNIGHT)
        self.assertEqual(
            (summary["total_tokens"], summary["today_total_tokens"]), (110, 10)
        )
        self.assertEqual(summary["today_llm_calls"], 2)
        self.assertEqual(self.store.token_leaderboard(self.user)["self"]["count"], 110)

    def test_missing_usage_zero_tokens_and_failures_are_separate(self):
        self.record(None, failed=True)
        unknown = self.store.token_leaderboard(self.user)
        self.assertEqual((unknown["entries"], unknown["self"]), ([], None))
        self.assertEqual((unknown["total_users"], unknown["missing_usage"]), (0, 1))
        self.record(0)
        known = self.store.token_leaderboard(self.user)
        self.assertEqual((known["total_users"], known["self"]["count"]), (1, 0))
        self.assertEqual(
            (known["self"]["calls"], known["self"]["missing_usage"]), (2, 1)
        )
        summary = self.store.token_summary(self.user, now=BEFORE_MIDNIGHT)
        for prefix in ("", "today_"):
            self.assertEqual(summary[prefix + "total_tokens"], 0)
            self.assertEqual(summary[prefix + "llm_calls"], 2)
            self.assertEqual(summary[prefix + "missing_usage"], 1)
            self.assertEqual(summary[prefix + "failed_calls"], 1)

    def test_partial_usage_retains_input_without_fabricating_total(self):
        self.store.record_llm(
            self.user, "p", "m", 23, 7, None, None, occurred_at=BEFORE_MIDNIGHT
        )
        summary = self.store.token_summary(self.user, now=BEFORE_MIDNIGHT)
        self.assertEqual(summary["today_input_tokens"], 7)
        self.assertEqual(summary["today_total_tokens"], 0)
        self.assertEqual(summary["today_missing_usage"], 1)
        self.assertEqual(self.store.token_leaderboard(self.user)["entries"], [])

    def test_group_platform_bot_isolation_and_personal_cross_group(self):
        identities = [
            self.user,
            replace(self.user, group="other"),
            replace(self.user, group=""),
            replace(self.user, platform="other"),
            replace(self.user, bot="other"),
            replace(self.user, user="other"),
        ]
        for index, identity in enumerate(identities, start=1):
            self.record(index * 10, identity=identity)
        summary = self.store.token_summary(self.user, now=BEFORE_MIDNIGHT)
        self.assertEqual(
            (summary["total_tokens"], summary["today_total_tokens"]), (60, 60)
        )
        board = self.store.token_leaderboard(self.user)
        self.assertEqual((board["total_users"], board["self"]["count"]), (2, 10))
        self.assertEqual(
            self.store.token_leaderboard(identities[1])["self"]["count"], 20
        )
        self.assertEqual(self.store.token_leaderboard(identities[2])["entries"], [])
        for index in (3, 4):
            other = self.store.token_leaderboard(identities[index])
            self.assertEqual(other["total_users"], 1)
            self.assertEqual(other["self"]["count"], (index + 1) * 10)

    def test_ties_self_outside_limit_and_unknown_only_exclusion(self):
        for uid, total in (("a", 50), ("b", 30), ("c", 30), ("d", 10)):
            self.record(total, identity=replace(self.user, user=uid, name=uid))
        self.record(None, identity=replace(self.user, user="unknown"))
        d = replace(self.user, user="d", name="最新名称")
        self.record(None, identity=d)
        board = self.store.token_leaderboard(d, limit=3)
        self.assertEqual([entry["rank"] for entry in board["entries"]], [1, 2, 2])
        self.assertEqual([entry["user"] for entry in board["entries"]], ["a", "b", "c"])
        self.assertEqual(
            board["self"],
            {
                "user": "d",
                "name": "最新名称",
                "count": 10,
                "rank": 4,
                "calls": 2,
                "missing_usage": 1,
            },
        )
        self.assertEqual((board["total_users"], board["missing_usage"]), (4, 2))

    def test_concurrent_connections_preserve_latest_day_and_all_totals(self):
        second = Store(self.path)
        try:

            def write(index):
                store = self.store if index % 3 else second
                store.record_llm(
                    self.user,
                    "p",
                    "m",
                    0,
                    2,
                    3,
                    5,
                    occurred_at=BEFORE_MIDNIGHT if index % 2 else AFTER_MIDNIGHT,
                )

            with ThreadPoolExecutor(max_workers=8) as executor:
                list(executor.map(write, range(120)))
        finally:
            second.close()
        summary = self.store.token_summary(self.user, now=AFTER_MIDNIGHT)
        self.assertEqual((summary["llm_calls"], summary["total_tokens"]), (120, 600))
        self.assertEqual(
            (summary["today_llm_calls"], summary["today_total_tokens"]), (60, 300)
        )
        self.assertEqual(self.store.token_leaderboard(self.user)["self"]["count"], 600)

    def test_daily_history_remains_one_fixed_row(self):
        self.record(1)
        initial_size = self.store.status()["database_bytes"]
        for day in range(1, 370):
            self.record(1, at=BEFORE_MIDNIGHT + timedelta(days=day))
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM daily_tokens").fetchone()[0], 1
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM group_tokens").fetchone()[0], 1
            )
        self.assertEqual(self.store.status()["database_bytes"], initial_size)

    def make_legacy_database(self):
        """Reconstruct v1 by removing only the newly added schema and metadata."""
        self.record(123)
        self.store.record_command(self.user, "p:c", "命令", 1)
        self.store.close()
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("DROP TABLE daily_tokens")
            conn.execute("DROP TABLE group_tokens")
            state = json.loads(conn.execute("SELECT state FROM metadata").fetchone()[0])
            state.pop("daily_started_at")
            state.pop("group_tokens_started_at")
            state["started_at"] = "2020-01-01T00:00:00+00:00"
            conn.execute(
                "UPDATE metadata SET state=?", (Store._encode_metadata(state),)
            )
            conn.execute("PRAGMA user_version=1")

    def test_schema_migration_preserves_lifetime_without_fabricated_history(self):
        self.make_legacy_database()
        self.store = Store(self.path)
        summary = self.store.token_summary(self.user, now=BEFORE_MIDNIGHT)
        board = self.store.token_leaderboard(self.user)
        self.assertEqual(
            (summary["total_tokens"], summary["today_total_tokens"]), (123, 0)
        )
        self.assertEqual(summary["started_at"], "2020-01-01T00:00:00+00:00")
        self.assertNotEqual(summary["daily_started_at"], summary["started_at"])
        self.assertEqual(board["started_at"], summary["daily_started_at"])
        self.assertEqual(board["entries"], [])
        self.assertEqual(self.store.personal(self.user)["command_count"], 1)
        self.record(10)
        before_restart = self.store.token_summary(self.user, now=BEFORE_MIDNIGHT)
        board_before_restart = self.store.token_leaderboard(self.user)
        self.assertEqual(
            (before_restart["total_tokens"], before_restart["today_total_tokens"]),
            (133, 10),
        )
        self.assertEqual(board_before_restart["self"]["count"], 10)
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(
            self.store.token_summary(self.user, now=BEFORE_MIDNIGHT), before_restart
        )
        self.assertEqual(self.store.token_leaderboard(self.user), board_before_restart)
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)

    def test_migration_failure_rolls_back_schema_and_coverage_metadata(self):
        self.make_legacy_database()
        with patch.object(
            Store,
            "_save_metadata",
            side_effect=sqlite3.OperationalError("database or disk is full"),
        ):
            with self.assertRaises(StorageCapacityError):
                Store(self.path)
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertNotIn("daily_tokens", tables)
            self.assertNotIn("group_tokens", tables)
            state = json.loads(conn.execute("SELECT state FROM metadata").fetchone()[0])
            self.assertNotIn("daily_started_at", state)
            self.assertEqual(
                conn.execute("SELECT total_tokens FROM users").fetchone()[0], 123
            )
        self.store = Store(self.path)

    def test_capacity_failure_rolls_back_lifetime_daily_and_group_together(self):
        self.store.close()
        self.store = Store(self.path, max_mib=0.0625)
        committed = 0
        for index in range(1000):
            try:
                self.store.record_llm(
                    self.user,
                    "p" * 120,
                    f"{index:04}" + "m" * 120,
                    23,
                    2,
                    3,
                    5,
                    occurred_at=BEFORE_MIDNIGHT,
                )
                committed += 1
            except StorageCapacityError:
                break
        else:
            self.fail("test did not reach the configured SQLite capacity")
        summary = self.store.token_summary(self.user, now=BEFORE_MIDNIGHT)
        self.assertEqual(
            (summary["llm_calls"], summary["today_llm_calls"]), (committed, committed)
        )
        self.assertEqual(
            (summary["total_tokens"], summary["today_total_tokens"]),
            (committed * 5, committed * 5),
        )
        self.assertEqual(
            self.store.token_leaderboard(self.user)["self"]["count"], committed * 5
        )
        self.assertEqual(self.store.personal(self.user)["model_types"], committed)
        self.assertTrue(self.store.status()["capacity_exceeded"])

    def test_overflow_rolls_back_daily_and_group_counts(self):
        self.record((1 << 63) - 1)
        with self.assertRaises(StorageError):
            self.record(1)
        summary = self.store.token_summary(self.user, now=BEFORE_MIDNIGHT)
        self.assertEqual((summary["llm_calls"], summary["today_llm_calls"]), (1, 1))
        self.assertEqual(self.store.token_leaderboard(self.user)["self"]["calls"], 1)

    def test_naive_timestamps_rejected_without_creating_statistics(self):
        with self.assertRaises(ValueError):
            self.record(at=datetime(2026, 10, 19))
        with self.assertRaises(ValueError):
            self.store.token_summary(self.user, now=datetime(2026, 10, 19))
        self.assertEqual(self.store.personal(self.user)["llm_calls"], 0)


if __name__ == "__main__":
    unittest.main()
