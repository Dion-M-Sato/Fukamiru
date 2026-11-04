"""SQLite connection and versioned migration support."""

from __future__ import annotations

import hashlib
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from fukamiru.config import Settings
from fukamiru.errors import MigrationError, SQLiteRecoveryExhaustedError

T = TypeVar("T")


def is_sqlite_contention(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    code = getattr(exc, "sqlite_errorcode", 0) or 0
    return code & 0xFF in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} or str(exc) in {
        "database is locked",
        "database table is locked",
    }


def retry_sqlite_operation(
    operation: Callable[[], T],
    *,
    on_retry: Callable[[int, BaseException], None] | None = None,
) -> T:
    """Retry only lock contention; each operation must own a fresh connection."""
    for attempt in range(1, 5):
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            if not is_sqlite_contention(exc):
                raise
            if attempt == 4:
                raise SQLiteRecoveryExhaustedError(
                    f"SQLite lock recovery exhausted after {attempt} attempts: {exc}"
                ) from exc
            if on_retry is not None:
                on_retry(attempt, exc)
            time.sleep(0.5 * 2 ** (attempt - 1))
    raise AssertionError("unreachable")


class ClosingSQLiteConnection(sqlite3.Connection):
    """Make ``with database.connect()`` close Windows file handles deterministically."""

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc, traceback))
        finally:
            self.close()


def utc_now_sql() -> str:
    return "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"


class SQLiteDatabase:
    def __init__(self, path: Path, migrations_dir: Path) -> None:
        self.path = path
        self.migrations_dir = migrations_dir

    def connect(
        self, *, busy_timeout_ms: int = 30_000, cache_size_kib: int | None = None
    ) -> sqlite3.Connection:
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms must be non-negative")
        if cache_size_kib is not None and cache_size_kib <= 0:
            raise ValueError("cache_size_kib must be positive")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.path,
            timeout=busy_timeout_ms / 1000,
            factory=ClosingSQLiteConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        connection.execute("PRAGMA journal_mode = WAL")
        if cache_size_kib is not None:
            connection.execute(f"PRAGMA cache_size = -{cache_size_kib}")
        return connection

    def migrate(self) -> list[str]:
        if not self.migrations_dir.is_dir():
            raise MigrationError(f"Migration directory not found: {self.migrations_dir}")
        applied_now: list[str] = []
        with self.connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    checksum TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                )
                """
            )
            applied = {
                row["version"]: row["checksum"]
                for row in connection.execute(
                    "SELECT version, checksum FROM schema_migrations ORDER BY version"
                )
            }

        for migration_path in sorted(self.migrations_dir.glob("[0-9][0-9][0-9][0-9]_*.sql")):
            version = migration_path.stem
            sql = migration_path.read_text(encoding="utf-8")
            checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            if version in applied:
                if applied[version] != checksum:
                    raise MigrationError(
                        f"Applied migration {version} has changed; create a new migration instead"
                    )
                continue
            escaped_version = version.replace("'", "''")
            escaped_checksum = checksum.replace("'", "''")
            script = (
                "BEGIN IMMEDIATE;\n"
                + sql
                + "\nINSERT INTO schema_migrations(version, checksum, applied_at) "
                + f"VALUES ('{escaped_version}', '{escaped_checksum}', "
                + "strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));\nCOMMIT;"
            )
            connection = self.connect()
            try:
                connection.executescript(script)
            except sqlite3.Error as exc:
                if connection.in_transaction:
                    connection.rollback()
                raise MigrationError(f"Failed migration {version}: {exc}") from exc
            finally:
                connection.close()
            applied_now.append(version)
        return applied_now

    def table_names(self) -> set[str]:
        with self.connect() as connection:
            return {
                row["name"]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }


@dataclass(frozen=True)
class DatabasePair:
    state: SQLiteDatabase
    knowledge: SQLiteDatabase

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        migrations_root: Path | None = None,
    ) -> DatabasePair:
        if migrations_root is None:
            migrations_root = Path(__file__).resolve().parents[3] / "migrations"
        return cls(
            state=SQLiteDatabase(settings.paths.state_db, migrations_root / "state"),
            knowledge=SQLiteDatabase(settings.paths.knowledge_db, migrations_root / "knowledge"),
        )

    def migrate(self) -> dict[str, list[str]]:
        return {"state": self.state.migrate(), "knowledge": self.knowledge.migrate()}
