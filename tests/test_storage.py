"""Storage tests run with Python's standard library alone."""

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from storage import Identity, StorageCapacityError, StorageError, Store


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "usage.sqlite3"
        self.store = Store(self.path)
        self.user = Identity("qq-main", "100", "200", "300", "测试用户")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_lifetime_commands_and_pagination(self):
        for index in range(12):
            for _ in range(index + 1):
                self.store.record_command(
                    self.user, f"plugin:command{index:02}", f"指令{index}", 23
                )
        first = self.store.personal(self.user)
        last = self.store.personal(self.user, page=999)
        self.assertEqual(first["command_count"], 78)
        self.assertEqual(
            first["commands"][0],
            {"key": "plugin:command11", "name": "指令11", "count": 12},
        )
        self.assertEqual(first["command_hours"][23], 78)
        self.assertEqual((first["command_types"], first["pages"]), (12, 2))
        self.assertEqual((last["page"], len(last["commands"])), (2, 2))

    def test_tokens_missing_zero_failures_and_models(self):
        self.store.record_llm(self.user, "p", "m", 1, 10, 5, None)
        self.store.record_llm(self.user, "p", "m", 1, None, None, 20)
        self.store.record_llm(self.user, "p", "m", 2, 0, 0, 0)
        self.store.record_llm(self.user, "p", "m2", 2, None, None, None, failed=True)
        self.store.record_llm(self.user, "p", "m2", 2, 7, None, None)
        stats = self.store.personal(self.user)
        self.assertEqual((stats["llm_calls"], stats["total_tokens"]), (5, 35))
        self.assertEqual((stats["input_tokens"], stats["output_tokens"]), (17, 5))
        self.assertEqual((stats["missing_usage"], stats["failed_calls"]), (2, 1))
        self.assertEqual(stats["llm_hours"][1:3], [2, 3])
        self.assertEqual(stats["models"][0]["calls"], 3)
        self.assertEqual(self.store.leaderboard(self.user)["entries"], [])

    def test_identity_isolation_and_personal_cross_group(self):
        people = [
            self.user,
            replace(self.user, group="other"),
            replace(self.user, group=""),
            replace(self.user, bot="other"),
            replace(self.user, platform="other"),
            replace(self.user, user="other"),
        ]
        for person in people:
            self.store.record_command(person, "p:cmd", "命令", 0)
        self.assertEqual(self.store.personal(self.user)["command_count"], 3)
        for person in people[3:]:
            self.assertEqual(self.store.personal(person)["command_count"], 1)
        board = self.store.leaderboard(self.user)
        self.assertEqual(board["total_users"], 2)
        self.assertEqual(board["self"]["count"], 1)
        self.assertEqual(
            self.store.leaderboard(replace(self.user, group=""))["entries"], []
        )

    def test_leaderboard_limit_self_ties_and_latest_name(self):
        for uid, calls in (("a", 5), ("b", 3), ("c", 3), ("d", 1)):
            for _ in range(calls):
                self.store.record_command(
                    replace(self.user, user=uid, name=uid), "p:c", "c", 10
                )
        d = replace(self.user, user="d", name="最新名称")
        self.store.record_llm(d, "p", "m", 10, 1, 1, 2)
        board = self.store.leaderboard(d, limit=3)
        self.assertEqual([item["rank"] for item in board["entries"]], [1, 2, 2])
        self.assertEqual(
            board["self"], {"user": "d", "name": "最新名称", "count": 1, "rank": 4}
        )

    def test_concurrent_threads_and_connections_do_not_lose_updates(self):
        second = Store(self.path)
        try:

            def write(index):
                store = second if index % 2 else self.store
                store.record_command(self.user, "p:c", "c", index % 24)
                store.record_llm(self.user, "p", "m", index % 24, 2, 3, 5)

            with ThreadPoolExecutor(max_workers=8) as executor:
                list(executor.map(write, range(120)))
        finally:
            second.close()
        stats = self.store.personal(self.user)
        self.assertEqual(
            (stats["command_count"], stats["llm_calls"], stats["total_tokens"]),
            (120, 120, 600),
        )
        self.assertEqual(stats["command_hours"], [5] * 24)
        self.assertEqual(stats["llm_hours"], [5] * 24)

    def test_restart_preserves_exact_aggregates(self):
        self.store.record_command(self.user, "p:c", "中文", 8)
        self.store.record_llm(self.user, "p", "m", 8, 11, 22, 33)
        before = self.store.personal(self.user)
        status_before = self.store.status()
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.personal(self.user), before)
        self.assertEqual(self.store.leaderboard(self.user)["self"]["count"], 1)
        self.assertEqual(self.store.status()["started_at"], status_before["started_at"])
        self.assertIsNotNone(self.store.status()["updated_at"])

    def test_space_limit_rolls_back_atomically_and_is_visible(self):
        self.store.close()
        self.store = Store(self.path, max_mib=0.0625)
        committed = 0
        failed_key = None
        for index in range(2000):
            key = f"command{index:04}:" + "x" * 400
            try:
                self.store.record_command(self.user, key, "long command", 5)
                committed += 1
            except StorageCapacityError:
                failed_key = key
                break
        self.assertIsNotNone(failed_key)
        status = self.store.status()
        self.assertEqual(status["write_failures"], 1)
        self.assertTrue(status["capacity_exceeded"])
        self.assertIsNotNone(status["last_error"])
        self.assertLessEqual(status["database_bytes"], status["max_bytes"])
        self.assertEqual(status["journal_bytes"], 0)
        stats = self.store.personal(self.user)
        self.assertEqual(stats["command_count"], committed)
        self.assertEqual(stats["command_types"], committed)
        self.assertEqual(stats["command_hours"][5], committed)
        self.assertEqual(self.store.leaderboard(self.user)["self"]["count"], committed)
        self.store.close()
        self.store = Store(self.path, max_mib=0.0625)
        self.assertEqual(self.store.status()["write_failures"], 1)
        self.assertTrue(self.store.status()["capacity_exceeded"])
        self.assertIsNotNone(self.store.status()["last_error"])

    def test_model_pagination_is_bounded_and_complete(self):
        for index in range(19):
            self.store.record_llm(self.user, "p", f"model{index:02}", 0, 1, 1, 2)
        first = self.store.personal(self.user)
        last = self.store.personal(self.user, model_page=999)
        self.assertEqual((first["model_types"], first["model_pages"]), (19, 3))
        self.assertEqual(len(first["models"]), 8)
        self.assertEqual((last["model_page"], len(last["models"])), (3, 3))
        self.assertEqual(last["models"][-1]["model"], "model18")

    def test_integer_overflow_is_atomic_and_reported(self):
        self.store.record_llm(self.user, "p", "m", 0, None, None, (1 << 63) - 1)
        with self.assertRaises(StorageError):
            self.store.record_llm(self.user, "p", "m", 0, 1, 0, 1)
        stats = self.store.personal(self.user)
        self.assertEqual(stats["llm_calls"], 1)
        self.assertEqual(stats["total_tokens"], (1 << 63) - 1)
        self.assertEqual(self.store.status()["write_failures"], 1)

    def test_existing_database_larger_than_limit_is_rejected(self):
        for index in range(100):
            self.store.record_command(self.user, f"key{index}" + "x" * 400, "c", 0)
        self.assertGreater(self.store.status()["database_bytes"], 32768)
        with self.assertRaises(StorageCapacityError):
            Store(self.path, max_mib=32768 / 1024 / 1024)

    def test_fixed_hour_blobs_and_no_growth_for_repeated_calls(self):
        self.store.record_command(self.user, "p:c", "c", 0)
        self.store.record_llm(self.user, "p", "m", 0, 1, 2, 3)
        initial_size = self.store.status()["database_bytes"]
        for index in range(500):
            self.store.record_command(self.user, "p:c", "c", index % 24)
            self.store.record_llm(self.user, "p", "m", index % 24, 1, 2, 3)
        self.assertEqual(initial_size, self.store.status()["database_bytes"])
        with sqlite3.connect(self.path) as conn:
            lengths = conn.execute(
                "SELECT length(command_hours), length(llm_hours) FROM users"
            ).fetchone()
            self.assertEqual(lengths, (192, 192))
            self.assertEqual(
                conn.execute("PRAGMA journal_mode").fetchone()[0], "delete"
            )

    def test_metadata_is_bounded_without_collapsing_long_names(self):
        for end in ("a", "b"):
            self.store.record_llm(
                self.user, "p" * 1000 + end, "m" * 1000 + end, 0, 1, 0, 1
            )
        models = self.store.personal(self.user)["models"]
        self.assertEqual(len(models), 2)
        for model in models:
            self.assertLessEqual(len(model["provider"]), 128)
            self.assertLessEqual(len(model["model"]), 128)

    def test_invalid_input_does_not_create_statistics(self):
        for hour in (-1, 24, True, "2"):
            with self.assertRaises(ValueError):
                self.store.record_command(self.user, "p:c", "c", hour)
        for tokens in (-1, 1.5, True, 1 << 63):
            with self.assertRaises(ValueError):
                self.store.record_llm(self.user, "p", "m", 0, tokens, 0, None)
        with self.assertRaises(ValueError):
            self.store.record_command(self.user, "x" * 513, "c", 0)
        self.assertEqual(self.store.personal(self.user)["command_count"], 0)
        self.assertEqual(self.store.personal(self.user)["llm_calls"], 0)

    def test_close_is_idempotent_and_reads_fail_loudly(self):
        self.store.close()
        self.store.close()
        with self.assertRaises(StorageError):
            self.store.personal(self.user)
        with self.assertRaises(StorageError):
            self.store.record_command(self.user, "p:c", "c", 0)
        self.assertTrue(self.store.status()["closed"])


if __name__ == "__main__":
    unittest.main()
