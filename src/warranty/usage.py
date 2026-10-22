"""Private, persistent pre-request model budgets; no provider or payload data."""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import contextmanager
import hmac
import os
from pathlib import Path
import re
import sqlite3
import time

from .errors import ServiceError


_CAPS = {"chat_requests": 1_000_000, "embedding_requests": 1_000_000, "input_bytes": 1_000_000_000_000}
_APPROVAL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FINGERPRINT = re.compile(r"[a-fA-F0-9]{64}\Z")
_TABLE = "model_usage_approvals"
_SCHEMA = """CREATE TABLE IF NOT EXISTS model_usage_approvals (
    approval_id TEXT PRIMARY KEY,
    context_fingerprint TEXT NOT NULL CHECK(length(context_fingerprint)=64),
    chat_limit INTEGER NOT NULL CHECK(typeof(chat_limit)='integer' AND chat_limit BETWEEN 0 AND 1000000),
    embedding_limit INTEGER NOT NULL CHECK(typeof(embedding_limit)='integer' AND embedding_limit BETWEEN 0 AND 1000000),
    byte_limit INTEGER NOT NULL CHECK(typeof(byte_limit)='integer' AND byte_limit BETWEEN 0 AND 1000000000000),
    chat_requests INTEGER NOT NULL DEFAULT 0 CHECK(typeof(chat_requests)='integer' AND chat_requests BETWEEN 0 AND chat_limit),
    embedding_requests INTEGER NOT NULL DEFAULT 0 CHECK(typeof(embedding_requests)='integer' AND embedding_requests BETWEEN 0 AND embedding_limit),
    input_bytes INTEGER NOT NULL DEFAULT 0 CHECK(typeof(input_bytes)='integer' AND input_bytes BETWEEN 0 AND byte_limit),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)"""


def _approval_required():
    return ServiceError("PROCESSING_APPROVAL_REQUIRED", "외부 처리 승인과 사용량 한도를 확인해 주세요.", 503, False)


def _limit_reached():
    return ServiceError("USAGE_LIMIT_REACHED", "승인된 외부 처리 사용량 한도에 도달했습니다.", 429, False)


def _invalid():
    return ServiceError("CONFIG_INVALID", "비공개 사용량 승인 설정을 확인해 주세요.", 503, False)


def _wait_for_initialization(path):
    """Observe a concurrent first creator without creating or repairing files."""
    lock = path.with_name(path.name + ".initializing")
    deadline = time.monotonic() + 2
    while lock.exists():
        if time.monotonic() >= deadline:
            raise OSError("Budget initialization is unavailable") from None
        time.sleep(.01)


@contextmanager
def _initialization_guard(path):
    """Only the first creator may initialize; an existing empty file is damage.

    This zero-content bootstrap lock protects the small SQLite creation window.
    Counters and approvals remain in one SQLite transaction. A crashed creator
    leaves a fail-closed lock; no automatic repair can erase prior usage.
    """
    lock = path.with_name(path.name + ".initializing")
    deadline = time.monotonic() + 2
    descriptor = None
    while True:
        if path.exists() and not lock.exists():
            yield False
            return
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise OSError("Budget initialization is unavailable") from None
            time.sleep(.01)
    try:
        yield not path.exists()
    finally:
        os.close(descriptor)
        lock.unlink()


class UsageBudget:
    """Consume before HTTP; successful, failed and unknown requests never refund.

    Construction and snapshots are read-only. The caller supplies a nonsecret
    approval identifier and a SHA-256 fingerprint of the approved context.
    A changed context or cap requires an explicitly authorized new identifier.
    """

    def __init__(self, path, approval_id, limits, *, context_fingerprint):
        if (not isinstance(limits, dict) or set(limits) != set(_CAPS)
                or any(type(limits[key]) is not int or not 0 <= limits[key] <= bound for key, bound in _CAPS.items())
                or not isinstance(context_fingerprint, str) or _FINGERPRINT.fullmatch(context_fingerprint) is None):
            raise _invalid()
        if approval_id is None:
            approval_id = ""
        if not isinstance(approval_id, str) or (approval_id and _APPROVAL.fullmatch(approval_id) is None):
            raise _invalid()
        try:
            self._path = Path(path).resolve()
        except (TypeError, ValueError, OSError):
            raise _invalid() from None
        repo = Path(__file__).resolve().parents[2]
        if self._path == repo or repo in self._path.parents:
            raise _invalid()
        self._approval_id = approval_id
        self._fingerprint = context_fingerprint.lower()
        self._limits = {key: limits[key] for key in _CAPS}

    @property
    def ready(self):
        return self.snapshot()["ready"]

    def approval_matches(self, approval_id):
        """Check a server execution scope without exposing the configured ID."""
        return (isinstance(approval_id, str) and _APPROVAL.fullmatch(approval_id) is not None
                and bool(self._approval_id) and hmac.compare_digest(self._approval_id, approval_id)
                and self.snapshot()["approved"])

    def _connect(self, *, read_only):
        connection = None
        try:
            if read_only:
                connection = sqlite3.connect(self._path.as_uri() + "?mode=ro", uri=True,
                                             isolation_level=None, timeout=2)
                connection.execute("PRAGMA query_only=ON")
            else:
                connection = sqlite3.connect(str(self._path), isolation_level=None, timeout=2)
                connection.execute("PRAGMA busy_timeout=2000")
                connection.execute("PRAGMA synchronous=FULL")
            connection.row_factory = sqlite3.Row
            return connection
        except BaseException:
            if connection is not None:
                connection.close()
            raise

    @staticmethod
    def _schema_state(connection):
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if names - {_TABLE} or version not in (0, 1) or (names and version != 1) or (version == 1 and _TABLE not in names):
            raise _approval_required()
        return _TABLE in names

    def _matches(self, row):
        return (row["context_fingerprint"] == self._fingerprint
                and row["chat_limit"] == self._limits["chat_requests"]
                and row["embedding_limit"] == self._limits["embedding_requests"]
                and row["byte_limit"] == self._limits["input_bytes"])

    def _snapshot(self, row=None, *, approved=True, reason=None):
        used = {key: row[key] if row is not None else 0 for key in _CAPS}
        remaining = {key: max(0, self._limits[key] - used[key]) for key in _CAPS}
        available = remaining["input_bytes"] > 0 and (remaining["chat_requests"] > 0 or remaining["embedding_requests"] > 0)
        if reason is None and not available:
            reason = "USAGE_LIMIT_REACHED"
        return {**used, "limits": dict(self._limits), "remaining": remaining,
                "approved": approved, "ready": approved and reason is None and available, "reason_code": reason}

    def snapshot(self):
        """Return counter-only status without creating a directory or database."""
        if not self._approval_id:
            return self._snapshot(approved=False, reason="PROCESSING_APPROVAL_REQUIRED")
        connection = None
        try:
            _wait_for_initialization(self._path)
            if not self._path.exists():
                return self._snapshot()
            if not self._path.is_file():
                return self._snapshot(approved=False, reason="PROCESSING_APPROVAL_REQUIRED")
            connection = self._connect(read_only=True)
            connection.execute("BEGIN")
            if not self._schema_state(connection):
                return self._snapshot(approved=False, reason="PROCESSING_APPROVAL_REQUIRED")
            row = connection.execute("SELECT * FROM model_usage_approvals WHERE approval_id=?", (self._approval_id,)).fetchone()
            if row is not None and not self._matches(row):
                return self._snapshot(row, approved=False, reason="PROCESSING_APPROVAL_REQUIRED")
            return self._snapshot(row)
        except (sqlite3.Error, OSError, ServiceError, KeyError, TypeError, ValueError):
            return self._snapshot(approved=False, reason="PROCESSING_APPROVAL_REQUIRED")
        finally:
            if connection is not None:
                connection.close()

    def consume(self, kind, input_bytes):
        """Reserve one request and its exact UTF-8 input size atomically."""
        if not isinstance(kind, str) or kind not in {"chat", "embedding"} or type(input_bytes) is not int or input_bytes < 0:
            raise ServiceError("INVALID_INPUT", "사용량 요청 형식을 확인해 주세요.", 400, False)
        if not self._approval_id:
            raise _approval_required()
        request_key = kind + "_requests"
        if self._limits[request_key] == 0 or self._limits["input_bytes"] == 0 or input_bytes > self._limits["input_bytes"]:
            raise _limit_reached()
        connection = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with _initialization_guard(self._path) as may_initialize:
                connection = self._connect(read_only=False)
                connection.execute("BEGIN IMMEDIATE")
                initialized = self._schema_state(connection)
                if not initialized:
                    if not may_initialize:
                        raise _approval_required()
                    connection.execute(_SCHEMA)
                    connection.execute("PRAGMA user_version=1")
                row = connection.execute("SELECT * FROM model_usage_approvals WHERE approval_id=?", (self._approval_id,)).fetchone()
                stamp = datetime.now(timezone.utc).isoformat()
                if row is None:
                    connection.execute("INSERT INTO model_usage_approvals(approval_id,context_fingerprint,chat_limit,embedding_limit,byte_limit,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                        (self._approval_id, self._fingerprint, self._limits["chat_requests"], self._limits["embedding_requests"], self._limits["input_bytes"], stamp, stamp))
                elif not self._matches(row):
                    raise _approval_required()
                chat, embedding = int(kind == "chat"), int(kind == "embedding")
                changed = connection.execute("UPDATE model_usage_approvals SET chat_requests=chat_requests+?,embedding_requests=embedding_requests+?,input_bytes=input_bytes+?,updated_at=? WHERE approval_id=? AND chat_requests+?<=chat_limit AND embedding_requests+?<=embedding_limit AND input_bytes<byte_limit AND input_bytes+?<=byte_limit",
                    (chat, embedding, input_bytes, stamp, self._approval_id, chat, embedding, input_bytes)).rowcount
                if changed != 1:
                    raise _limit_reached()
                connection.commit()
        except ServiceError:
            if connection is not None:
                try:
                    connection.rollback()
                except (sqlite3.Error, OSError):
                    pass
            raise
        except (sqlite3.Error, OSError, ValueError, TypeError, OverflowError):
            if connection is not None:
                try:
                    connection.rollback()
                except (sqlite3.Error, OSError):
                    pass
            raise _approval_required() from None
        finally:
            if connection is not None:
                connection.close()
