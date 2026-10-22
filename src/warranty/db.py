"""Local SQLite connections, atomic units of work, and versioned schema."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
from pathlib import Path
import sqlite3
import time
from typing import Iterator
import uuid


class _ClosingConnection(sqlite3.Connection):
    """SQLite's usual context manager does not release its file handle."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class Database:
    """One connection per request; callers keep external work outside transactions."""

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 2000,
                 begin_attempts: int = 3):
        if isinstance(busy_timeout_ms, bool) or not 0 <= busy_timeout_ms <= 5000:
            raise ValueError("busy_timeout_ms must be between 0 and 5000")
        if isinstance(begin_attempts, bool) or not 1 <= begin_attempts <= 5:
            raise ValueError("begin_attempts must be between 1 and 5")
        self.path = Path(path) if str(path) != ":memory:" else ":memory:"
        self.busy_timeout_ms = int(busy_timeout_ms)
        self.begin_attempts = int(begin_attempts)
        self._uri = str(path) == ":memory:"
        self._target = (f"file:warranty_{uuid.uuid4().hex}?mode=memory&cache=shared"
                        if self._uri else str(Path(path)))
        self._anchor = self.connect() if self._uri else None

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._target, timeout=self.busy_timeout_ms / 1000,
            isolation_level=None, uri=self._uri, factory=_ClosingConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Acquire SQLite's single writer, validate posting, commit or roll back."""
        connection = self.connect()
        try:
            for attempt in range(self.begin_attempts):
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    break
                except sqlite3.OperationalError as error:
                    if ("locked" not in str(error).lower() or
                            attempt + 1 == self.begin_attempts):
                        raise
                    time.sleep(0.01 * (attempt + 1))
            yield connection
            self._validate_postings(connection)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _validate_postings(connection: sqlite3.Connection) -> None:
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ledger_groups'"
        ).fetchone():
            return
        invalid = connection.execute(
            "SELECT g.group_id FROM ledger_groups g "
            "LEFT JOIN ledger_entries e ON e.group_id=g.group_id "
            "GROUP BY g.group_id HAVING g.posted != 1 OR COUNT(e.line_no)<2 "
            "OR COALESCE(SUM(e.debit),0)<=0 "
            "OR COALESCE(SUM(e.debit),0)!=COALESCE(SUM(e.credit),0) LIMIT 1"
        ).fetchone()
        if invalid:
            raise sqlite3.IntegrityError("ledger group must be balanced and posted")

    def initialize(self) -> None:
        """Apply each checked migration exactly once, without implicit commits."""
        if not self._uri:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        migration_dir = Path(__file__).with_name("migrations")
        migrations = sorted(migration_dir.glob("[0-9]*.sql"))
        if not migrations:
            raise RuntimeError("database migrations are missing")
        with self.transaction() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
                "sha256 TEXT NOT NULL, applied_at TEXT NOT NULL DEFAULT "
                "(strftime('%Y-%m-%dT%H:%M:%fZ','now')))"
            )
            for migration in migrations:
                version = int(migration.name.split("_", 1)[0])
                raw = migration.read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
                previous = connection.execute(
                    "SELECT name,sha256 FROM schema_migrations WHERE version=?",
                    (version,),
                ).fetchone()
                if previous:
                    if previous["name"] != migration.name or previous["sha256"] != digest:
                        raise RuntimeError("applied migration has changed")
                    continue
                statement = ""
                for line in raw.decode("utf-8").splitlines(keepends=True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        connection.execute(statement)
                        statement = ""
                if statement.strip():
                    raise RuntimeError("incomplete database migration")
                connection.execute(
                    "INSERT INTO schema_migrations(version,name,sha256) VALUES(?,?,?)",
                    (version, migration.name, digest),
                )
                connection.execute(f"PRAGMA user_version = {version}")

    def close(self) -> None:
        if self._anchor is not None:
            self._anchor.close()
            self._anchor = None
