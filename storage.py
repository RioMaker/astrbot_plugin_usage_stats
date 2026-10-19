"""Compact lifetime aggregates; this module never stores messages or call logs."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import struct
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

_HOURS = struct.Struct("<24Q")
_EMPTY_HOURS = _HOURS.pack(*([0] * 24))
_MAX_INT = (1 << 63) - 1
_META_BYTES = 2048
_BEIJING = timezone(timedelta(hours=8))
_TOKEN_FIELDS = (
    "llm_calls",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "missing_usage",
    "failed_calls",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _beijing_day(value: datetime | None = None) -> str:
    value = datetime.now(timezone.utc) if value is None else value
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("token timestamps must be timezone-aware datetimes")
    return value.astimezone(_BEIJING).date().isoformat()


class StorageError(RuntimeError):
    """An aggregate could not be written or read; callers must expose the failure."""


class StorageCapacityError(StorageError):
    """SQLite refused a write because its configured space limit was reached."""


@dataclass(frozen=True)
class Identity:
    platform: str
    bot: str
    user: str
    group: str = ""
    name: str = ""


def _key(value: str, label: str, limit: int = 256, *, optional: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    value = value.strip()
    if (not optional and not value) or len(value) > limit:
        raise ValueError(f"{label} must contain 1..{limit} characters")
    return value


def _label(value: str, limit: int = 128) -> str:
    """Bound metadata while retaining distinct long provider/model identifiers."""
    value = str(value or "").strip()
    if len(value) <= limit:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return value[: limit - 17] + "~" + digest


def _identity(value: Identity) -> Identity:
    return Identity(
        platform=_key(value.platform, "platform"),
        bot=_key(value.bot, "bot"),
        user=_key(value.user, "user"),
        group=_key(value.group, "group", optional=True),
        name=str(value.name or "").strip()[:128],
    )


def _hour(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 24:
        raise ValueError("hour must be an integer in 0..23")
    return value


def _tokens(value: int | None) -> int | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= _MAX_INT
    ):
        raise ValueError("token counts must be non-negative signed 64-bit integers")
    return value


def _increment_hour(blob: bytes, hour: int) -> bytes:
    values = list(_HOURS.unpack(blob))
    values[hour] += 1
    return _HOURS.pack(*values)


class Store:
    """Thread-safe synchronous store, suitable for ``asyncio.to_thread``.

    Every record is committed atomically. BEGIN IMMEDIATE also protects read/modify/
    write hour arrays when multiple Store instances share a database. The database
    has a hard SQLite page limit; DELETE journaling avoids accumulating WAL files.
    A rollback journal can temporarily occupy additional space during a transaction.
    """

    def __init__(self, path: str | Path, max_mib: float = 64):
        if isinstance(max_mib, bool) or not math.isfinite(max_mib) or max_mib <= 0:
            raise ValueError("max_mib must be a positive finite number")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._write_failures = 0
        self._last_error: str | None = None
        self._capacity_exceeded = False
        self._requested_bytes = int(max_mib * 1024 * 1024)
        self._conn = sqlite3.connect(
            str(self.path), timeout=5, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA journal_mode=DELETE")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("PRAGMA temp_store=MEMORY")
            self._page_size = self._conn.execute("PRAGMA page_size").fetchone()[0]
            self._max_pages = self._requested_bytes // self._page_size
            if self._max_pages < 8:
                raise ValueError("max_mib must allow at least 8 SQLite pages")
            current_pages = self._conn.execute("PRAGMA page_count").fetchone()[0]
            if current_pages > self._max_pages:
                raise StorageCapacityError(
                    "existing statistics database exceeds the configured limit"
                )
            actual_limit = self._conn.execute(
                f"PRAGMA max_page_count={self._max_pages}"
            ).fetchone()[0]
            if actual_limit != self._max_pages:
                raise StorageCapacityError("could not apply the database page limit")
            self._initialize()
        except BaseException:
            self._conn.close()
            self._closed = True
            raise

    def _initialize(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2):
            raise StorageError(f"unsupported statistics schema version: {version}")
        statements = [
            """CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                platform TEXT NOT NULL, bot TEXT NOT NULL, user TEXT NOT NULL,
                name TEXT NOT NULL,
                command_count INTEGER NOT NULL DEFAULT 0,
                llm_calls INTEGER NOT NULL DEFAULT 0,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                missing_usage INTEGER NOT NULL DEFAULT 0,
                failed_calls INTEGER NOT NULL DEFAULT 0,
                command_hours BLOB NOT NULL CHECK(length(command_hours)=192),
                llm_hours BLOB NOT NULL CHECK(length(llm_hours)=192),
                UNIQUE(platform, bot, user)
            )""",
            """CREATE TABLE IF NOT EXISTS commands (
                user_id INTEGER NOT NULL REFERENCES users(id),
                key TEXT NOT NULL, name TEXT NOT NULL, count INTEGER NOT NULL,
                PRIMARY KEY(user_id, key)
            ) WITHOUT ROWID""",
            """CREATE TABLE IF NOT EXISTS models (
                user_id INTEGER NOT NULL REFERENCES users(id),
                provider TEXT NOT NULL, model TEXT NOT NULL,
                calls INTEGER NOT NULL, input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL, total_tokens INTEGER NOT NULL,
                missing_usage INTEGER NOT NULL, failed_calls INTEGER NOT NULL,
                PRIMARY KEY(user_id, provider, model)
            ) WITHOUT ROWID""",
            """CREATE TABLE IF NOT EXISTS group_counts (
                user_id INTEGER NOT NULL REFERENCES users(id),
                group_id TEXT NOT NULL, count INTEGER NOT NULL,
                PRIMARY KEY(user_id, group_id)
            ) WITHOUT ROWID""",
            """CREATE INDEX IF NOT EXISTS group_lookup
                ON group_counts(group_id, count DESC, user_id)""",
            """CREATE TABLE IF NOT EXISTS daily_tokens (
                user_id INTEGER PRIMARY KEY REFERENCES users(id),
                day TEXT NOT NULL,
                llm_calls INTEGER NOT NULL, input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL, total_tokens INTEGER NOT NULL,
                missing_usage INTEGER NOT NULL, failed_calls INTEGER NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS group_tokens (
                user_id INTEGER NOT NULL REFERENCES users(id),
                group_id TEXT NOT NULL,
                calls INTEGER NOT NULL, total_tokens INTEGER NOT NULL,
                missing_usage INTEGER NOT NULL,
                PRIMARY KEY(user_id, group_id)
            ) WITHOUT ROWID""",
            """CREATE INDEX IF NOT EXISTS group_tokens_lookup
                ON group_tokens(group_id, total_tokens DESC, user_id)""",
            """CREATE TABLE IF NOT EXISTS metadata (
                id INTEGER PRIMARY KEY CHECK(id=1),
                state BLOB NOT NULL CHECK(length(state)=2048)
            )""",
        ]
        with self._transaction(write=True):
            for statement in statements:
                self._conn.execute(statement)
            started_at = _now()
            state = {
                "started_at": started_at,
                "updated_at": None,
                "write_failures": 0,
                "last_error": None,
                "capacity_exceeded": False,
            }
            self._conn.execute(
                "INSERT OR IGNORE INTO metadata(id, state) VALUES(1, ?)",
                (self._encode_metadata(state),),
            )
            # Existing lifetime totals cannot be assigned to a historical day or
            # group. Record the new coverage start without inventing that history.
            state = self._metadata()
            state.setdefault("daily_started_at", started_at)
            state.setdefault("group_tokens_started_at", started_at)
            self._save_metadata(state)
            self._conn.execute("PRAGMA user_version=2")
        state = self._metadata()
        self._write_failures = state["write_failures"]
        self._last_error = state["last_error"]
        self._capacity_exceeded = state["capacity_exceeded"]

    @staticmethod
    def _encode_metadata(state: dict) -> bytes:
        encoded = json.dumps(state, ensure_ascii=True, separators=(",", ":")).encode(
            "ascii"
        )
        if len(encoded) > _META_BYTES:
            raise StorageError("statistics metadata exceeds its reserved space")
        return encoded.ljust(_META_BYTES, b" ")

    def _metadata(self) -> dict:
        row = self._conn.execute("SELECT state FROM metadata WHERE id=1").fetchone()
        return json.loads(row[0])

    def _save_metadata(self, state: dict) -> None:
        self._conn.execute(
            "UPDATE metadata SET state=? WHERE id=1", (self._encode_metadata(state),)
        )

    def _touch(self) -> None:
        state = self._metadata()
        state["updated_at"] = _now()
        state["capacity_exceeded"] = False
        self._save_metadata(state)

    def _record_failure(self, exc: Exception, *, is_full: bool) -> None:
        self._last_error = str(exc)[:256]
        self._capacity_exceeded = is_full
        self._write_failures += 1
        # This fixed-size row is allocated at creation. It can usually be updated
        # even after a page-limit failure, but genuine I/O failures may prevent it.
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            state = self._metadata()
            state["write_failures"] = max(
                self._write_failures, state["write_failures"] + 1
            )
            state["last_error"] = self._last_error
            state["capacity_exceeded"] = is_full
            self._save_metadata(state)
            self._conn.commit()
            self._write_failures = state["write_failures"]
        except (sqlite3.Error, StorageError, ValueError, TypeError):
            self._conn.rollback()

    def _ensure_open(self) -> None:
        if self._closed:
            raise StorageError("statistics database is closed")

    @contextmanager
    def _transaction(self, *, write: bool) -> Iterator[None]:
        with self._lock:
            self._ensure_open()
            try:
                self._conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield
                self._conn.commit()
                if write:
                    self._capacity_exceeded = False
            except BaseException as exc:
                self._conn.rollback()
                code = getattr(exc, "sqlite_errorcode", None)
                is_full = code == 13 or "database or disk is full" in str(exc).lower()
                if write and isinstance(exc, Exception):
                    self._record_failure(exc, is_full=is_full)
                if isinstance(exc, sqlite3.Error):
                    self._last_error = str(exc)[:256]
                    # sqlite_errorcode is available on 3.11+, with a 3.10 fallback.
                    if is_full:
                        self._capacity_exceeded = True
                        raise StorageCapacityError(str(exc)) from exc
                    raise StorageError(str(exc)) from exc
                raise

    def _user(self, identity: Identity) -> sqlite3.Row:
        self._conn.execute(
            """INSERT INTO users(platform, bot, user, name, command_hours, llm_hours)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(platform, bot, user) DO UPDATE SET
               name=CASE WHEN excluded.name <> ''
                         THEN excluded.name ELSE users.name END""",
            (
                identity.platform,
                identity.bot,
                identity.user,
                identity.name,
                _EMPTY_HOURS,
                _EMPTY_HOURS,
            ),
        )
        return self._conn.execute(
            "SELECT * FROM users WHERE platform=? AND bot=? AND user=?",
            (identity.platform, identity.bot, identity.user),
        ).fetchone()

    def record_command(
        self, identity: Identity, command_key: str, command_name: str, hour: int
    ) -> None:
        identity = _identity(identity)
        command_key = _key(command_key, "command_key", 512)
        command_name = str(command_name or command_key).strip()[:128]
        hour = _hour(hour)
        with self._transaction(write=True):
            user = self._user(identity)
            if user["command_count"] == _MAX_INT:
                raise StorageError("command_count exceeds the supported integer range")
            self._conn.execute(
                """UPDATE users SET command_count=command_count+1, command_hours=?
                   WHERE id=?""",
                (_increment_hour(user["command_hours"], hour), user["id"]),
            )
            self._conn.execute(
                """INSERT INTO commands(user_id, key, name, count) VALUES (?, ?, ?, 1)
                   ON CONFLICT(user_id, key) DO UPDATE SET
                   count=commands.count+1, name=excluded.name""",
                (user["id"], command_key, command_name),
            )
            if identity.group:
                self._conn.execute(
                    """INSERT INTO group_counts(user_id, group_id, count)
                       VALUES (?, ?, 1)
                       ON CONFLICT(user_id, group_id) DO UPDATE SET
                       count=group_counts.count+1""",
                    (user["id"], identity.group),
                )
            self._touch()

    def record_llm(
        self,
        identity: Identity,
        provider: str,
        model: str,
        hour: int,
        input_tokens: int | None,
        output_tokens: int | None,
        total_tokens: int | None,
        failed: bool = False,
        *,
        occurred_at: datetime | None = None,
    ) -> None:
        """Record a completed call; daily totals follow its Beijing calendar day.

        Only the latest observed day is retained per user. A delayed completion
        from an older day still enters lifetime/group totals but cannot replace
        the newer daily bucket. Without a supplied completion timestamp, the time
        is sampled inside the write transaction to avoid midnight lock ordering.
        """
        identity = _identity(identity)
        provider, model = _label(provider) or "unknown", _label(model) or "unknown"
        hour = _hour(hour)
        input_tokens, output_tokens, total_tokens = (
            _tokens(input_tokens),
            _tokens(output_tokens),
            _tokens(total_tokens),
        )
        if (
            total_tokens is None
            and input_tokens is not None
            and output_tokens is not None
        ):
            total_tokens = _tokens(input_tokens + output_tokens)
        missing = int(total_tokens is None)
        values = (
            input_tokens or 0,
            output_tokens or 0,
            total_tokens or 0,
            missing,
            int(failed),
        )
        explicit_day = _beijing_day(occurred_at) if occurred_at is not None else None
        with self._transaction(write=True):
            day = explicit_day or _beijing_day()
            user = self._user(identity)
            if user["llm_calls"] == _MAX_INT:
                raise StorageError("llm_calls exceeds the supported integer range")
            # SQLite otherwise promotes overflowing integers to REAL.
            for field, amount in zip(
                (
                    "input_tokens",
                    "output_tokens",
                    "total_tokens",
                    "missing_usage",
                    "failed_calls",
                ),
                values,
            ):
                if user[field] + amount > _MAX_INT:
                    raise StorageError(f"{field} exceeds the supported integer range")
            self._conn.execute(
                """UPDATE users SET llm_calls=llm_calls+1,
                   input_tokens=input_tokens+?, output_tokens=output_tokens+?,
                   total_tokens=total_tokens+?, missing_usage=missing_usage+?,
                   failed_calls=failed_calls+?, llm_hours=? WHERE id=?""",
                (*values, _increment_hour(user["llm_hours"], hour), user["id"]),
            )
            self._conn.execute(
                """INSERT INTO models(user_id, provider, model, calls, input_tokens,
                   output_tokens, total_tokens, missing_usage, failed_calls)
                   VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id, provider, model) DO UPDATE SET
                   calls=models.calls+1,
                   input_tokens=models.input_tokens+excluded.input_tokens,
                   output_tokens=models.output_tokens+excluded.output_tokens,
                   total_tokens=models.total_tokens+excluded.total_tokens,
                   missing_usage=models.missing_usage+excluded.missing_usage,
                   failed_calls=models.failed_calls+excluded.failed_calls""",
                (user["id"], provider, model, *values),
            )
            self._conn.execute(
                """INSERT INTO daily_tokens(user_id, day, llm_calls, input_tokens,
                   output_tokens, total_tokens, missing_usage, failed_calls)
                   VALUES (?, ?, 1, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                   day=excluded.day,
                   llm_calls=CASE WHEN excluded.day=daily_tokens.day
                       THEN daily_tokens.llm_calls+1 ELSE 1 END,
                   input_tokens=CASE WHEN excluded.day=daily_tokens.day
                       THEN daily_tokens.input_tokens+excluded.input_tokens
                       ELSE excluded.input_tokens END,
                   output_tokens=CASE WHEN excluded.day=daily_tokens.day
                       THEN daily_tokens.output_tokens+excluded.output_tokens
                       ELSE excluded.output_tokens END,
                   total_tokens=CASE WHEN excluded.day=daily_tokens.day
                       THEN daily_tokens.total_tokens+excluded.total_tokens
                       ELSE excluded.total_tokens END,
                   missing_usage=CASE WHEN excluded.day=daily_tokens.day
                       THEN daily_tokens.missing_usage+excluded.missing_usage
                       ELSE excluded.missing_usage END,
                   failed_calls=CASE WHEN excluded.day=daily_tokens.day
                       THEN daily_tokens.failed_calls+excluded.failed_calls
                       ELSE excluded.failed_calls END
                   WHERE excluded.day>=daily_tokens.day""",
                (user["id"], day, *values),
            )
            if identity.group:
                self._conn.execute(
                    """INSERT INTO group_tokens(user_id, group_id, calls,
                       total_tokens, missing_usage) VALUES (?, ?, 1, ?, ?)
                       ON CONFLICT(user_id, group_id) DO UPDATE SET
                       calls=group_tokens.calls+1,
                       total_tokens=group_tokens.total_tokens+excluded.total_tokens,
                       missing_usage=group_tokens.missing_usage+excluded.missing_usage""",
                    (user["id"], identity.group, total_tokens or 0, missing),
                )
            self._touch()

    def token_summary(self, identity: Identity, *, now: datetime | None = None) -> dict:
        """Lifetime and current-day tokens; reading never creates or resets rows."""
        identity = _identity(identity)
        result = {
            "user": identity.user,
            "name": identity.name or identity.user,
            "today": _beijing_day(now),
            **dict.fromkeys(_TOKEN_FIELDS, 0),
            **dict.fromkeys((f"today_{key}" for key in _TOKEN_FIELDS), 0),
        }
        with self._transaction(write=False):
            state = self._metadata()
            result["started_at"] = state["started_at"]
            result["daily_started_at"] = state["daily_started_at"]
            user = self._conn.execute(
                "SELECT * FROM users WHERE platform=? AND bot=? AND user=?",
                (identity.platform, identity.bot, identity.user),
            ).fetchone()
            if user is None:
                return result
            result["name"] = user["name"] or identity.name or identity.user
            result.update((key, user[key]) for key in _TOKEN_FIELDS)
            daily = self._conn.execute(
                "SELECT * FROM daily_tokens WHERE user_id=? AND day=?",
                (user["id"], result["today"]),
            ).fetchone()
            if daily is not None:
                result.update((f"today_{key}", daily[key]) for key in _TOKEN_FIELDS)
        return result

    def token_leaderboard(self, identity: Identity, limit: int = 10) -> dict:
        """Rank known tokens consumed in this group, with shared ranks for ties."""
        identity = _identity(identity)
        limit = max(1, min(100, int(limit)))
        result = {"entries": [], "self": None, "total_users": 0, "missing_usage": 0}
        parameters = (identity.platform, identity.bot, identity.group)
        ranking = """SELECT u.user,
                     CASE WHEN u.name='' THEN u.user ELSE u.name END AS name,
                     g.total_tokens AS count, g.calls, g.missing_usage,
                     RANK() OVER (ORDER BY g.total_tokens DESC) AS rank
                     FROM group_tokens g JOIN users u ON u.id=g.user_id
                     WHERE u.platform=? AND u.bot=? AND g.group_id=?
                     AND g.calls>g.missing_usage"""
        with self._transaction(write=False):
            result["started_at"] = self._metadata()["group_tokens_started_at"]
            if not identity.group:
                return result
            result["entries"] = [
                dict(row)
                for row in self._conn.execute(
                    f"SELECT * FROM ({ranking}) ORDER BY count DESC, user LIMIT ?",
                    (*parameters, limit),
                )
            ]
            own = self._conn.execute(
                f"SELECT * FROM ({ranking}) WHERE user=?", (*parameters, identity.user)
            ).fetchone()
            if own is not None:
                result["self"] = dict(own)
            rows = self._conn.execute(
                """SELECT g.calls, g.missing_usage
                   FROM group_tokens g JOIN users u ON u.id=g.user_id
                   WHERE u.platform=? AND u.bot=? AND g.group_id=?""",
                parameters,
            )
            # Sum with Python integers: a group total may exceed SQLite INT64 even
            # while every per-user aggregate remains in the supported range.
            for row in rows:
                result["total_users"] += int(row["calls"] > row["missing_usage"])
                result["missing_usage"] += row["missing_usage"]
        return result

    def personal(
        self,
        identity: Identity,
        page: int = 1,
        page_size: int = 10,
        model_page: int = 1,
        model_page_size: int = 8,
    ) -> dict:
        identity = _identity(identity)
        page, page_size = max(1, int(page)), max(1, min(100, int(page_size)))
        model_page = max(1, int(model_page))
        model_page_size = max(1, min(100, int(model_page_size)))
        result = {
            "user": identity.user,
            "name": identity.name or identity.user,
            "command_count": 0,
            "llm_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "missing_usage": 0,
            "failed_calls": 0,
            "command_hours": [0] * 24,
            "llm_hours": [0] * 24,
            "commands": [],
            "command_types": 0,
            "page": 1,
            "page_size": page_size,
            "pages": 1,
            "models": [],
            "model_types": 0,
            "model_page": 1,
            "model_page_size": model_page_size,
            "model_pages": 1,
        }
        with self._transaction(write=False):
            user = self._conn.execute(
                "SELECT * FROM users WHERE platform=? AND bot=? AND user=?",
                (identity.platform, identity.bot, identity.user),
            ).fetchone()
            if user is None:
                return result
            result["name"] = user["name"] or identity.name or identity.user
            for key in (
                "command_count",
                "llm_calls",
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "missing_usage",
                "failed_calls",
            ):
                result[key] = user[key]
            result["command_hours"] = list(_HOURS.unpack(user["command_hours"]))
            result["llm_hours"] = list(_HOURS.unpack(user["llm_hours"]))
            count = self._conn.execute(
                "SELECT COUNT(*) FROM commands WHERE user_id=?", (user["id"],)
            ).fetchone()[0]
            result["command_types"] = count
            result["pages"] = max(1, (count + page_size - 1) // page_size)
            result["page"] = min(page, result["pages"])
            result["commands"] = [
                dict(row)
                for row in self._conn.execute(
                    """SELECT key, name, count FROM commands WHERE user_id=?
                       ORDER BY count DESC, key LIMIT ? OFFSET ?""",
                    (user["id"], page_size, (result["page"] - 1) * page_size),
                )
            ]
            model_count = self._conn.execute(
                "SELECT COUNT(*) FROM models WHERE user_id=?", (user["id"],)
            ).fetchone()[0]
            result["model_types"] = model_count
            result["model_pages"] = max(
                1, (model_count + model_page_size - 1) // model_page_size
            )
            result["model_page"] = min(model_page, result["model_pages"])
            result["models"] = [
                dict(row)
                for row in self._conn.execute(
                    """SELECT provider, model, calls, input_tokens, output_tokens,
                       total_tokens, missing_usage, failed_calls
                       FROM models WHERE user_id=?
                       ORDER BY calls DESC, provider, model LIMIT ? OFFSET ?""",
                    (
                        user["id"],
                        model_page_size,
                        (result["model_page"] - 1) * model_page_size,
                    ),
                )
            ]
        return result

    def leaderboard(self, identity: Identity, limit: int = 10) -> dict:
        """Current group's ranking; ties share rank, ordered by user ID."""
        identity = _identity(identity)
        limit = max(1, min(100, int(limit)))
        result = {"entries": [], "self": None, "total_users": 0}
        if not identity.group:
            return result
        parameters = (identity.platform, identity.bot, identity.group)
        ranking = """SELECT u.user,
                     CASE WHEN u.name='' THEN u.user ELSE u.name END AS name,
                     g.count, RANK() OVER (ORDER BY g.count DESC) AS rank
                     FROM group_counts g JOIN users u ON u.id=g.user_id
                     WHERE u.platform=? AND u.bot=? AND g.group_id=?"""
        with self._transaction(write=False):
            result["entries"] = [
                dict(row)
                for row in self._conn.execute(
                    f"SELECT * FROM ({ranking}) ORDER BY count DESC, user LIMIT ?",
                    (*parameters, limit),
                )
            ]
            own = self._conn.execute(
                f"SELECT * FROM ({ranking}) WHERE user=?", (*parameters, identity.user)
            ).fetchone()
            if own is not None:
                result["self"] = dict(own)
            result["total_users"] = self._conn.execute(
                """SELECT COUNT(*) FROM group_counts g JOIN users u ON u.id=g.user_id
                   WHERE u.platform=? AND u.bot=? AND g.group_id=?""",
                parameters,
            ).fetchone()[0]
        return result

    def status(self) -> dict:
        with self._lock:
            page_count = 0
            freelist_count = 0
            state = {}
            if not self._closed:
                try:
                    page_count = self._conn.execute("PRAGMA page_count").fetchone()[0]
                    freelist_count = self._conn.execute(
                        "PRAGMA freelist_count"
                    ).fetchone()[0]
                    state = self._metadata()
                    self._write_failures = max(
                        self._write_failures, state["write_failures"]
                    )
                    if state["write_failures"] >= self._write_failures:
                        self._last_error = state["last_error"]
                        self._capacity_exceeded = state["capacity_exceeded"]
                except sqlite3.Error as exc:
                    self._last_error = str(exc)
            file_bytes = self.path.stat().st_size if self.path.exists() else 0
            journal_bytes = 0
            for suffix in ("-journal", "-wal", "-shm"):
                journal = Path(str(self.path) + suffix)
                if journal.exists():
                    journal_bytes += journal.stat().st_size
            return {
                "database_bytes": file_bytes,
                "started_at": state.get("started_at"),
                "updated_at": state.get("updated_at"),
                "journal_bytes": journal_bytes,
                "total_bytes": file_bytes + journal_bytes,
                "max_bytes": self._max_pages * self._page_size,
                "page_count": page_count,
                "free_pages": freelist_count,
                "write_failures": self._write_failures,
                "last_error": self._last_error,
                "capacity_exceeded": self._capacity_exceeded,
                "closed": self._closed,
            }

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True
