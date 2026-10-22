"""Private, content-addressed embedding reuse without storing source text.

Callers must authorize the current source and processing scope before reading
this cache. A hit is stored data, never a grant of source or model permissions.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import struct
import tempfile
import time

from .errors import ServiceError


_APPLICATION_ID = 0x57454331
_VERSION = 1
_HEX = re.compile(r"[a-f0-9]{64}")
_SCHEMA = (
    "CREATE TABLE embedding_vectors (cache_key TEXT PRIMARY KEY NOT NULL, "
    "dimension INTEGER NOT NULL CHECK(dimension BETWEEN 1 AND 8192), "
    "vector_blob BLOB NOT NULL, integrity_hash TEXT NOT NULL, "
    "CHECK(length(cache_key)=64), CHECK(length(integrity_hash)=64), "
    "CHECK(length(vector_blob)=dimension*8))",
    "CREATE TABLE embedding_claims (cache_key TEXT PRIMARY KEY NOT NULL, "
    "token TEXT NOT NULL, expires_at REAL NOT NULL, "
    "CHECK(length(cache_key)=64), CHECK(length(token)=64))",
)
_COLUMNS = {
    "embedding_vectors": [("cache_key", "TEXT", 1, 1), ("dimension", "INTEGER", 1, 0),
                          ("vector_blob", "BLOB", 1, 0), ("integrity_hash", "TEXT", 1, 0)],
    "embedding_claims": [("cache_key", "TEXT", 1, 1), ("token", "TEXT", 1, 0),
                         ("expires_at", "REAL", 1, 0)],
}


def _error(code):
    messages = {
        "EMBEDDING_CACHE_INVALID": "임베딩 재사용 입력을 확인해 주세요.",
        "EMBEDDING_CACHE_UNAVAILABLE": "임베딩 재사용 저장소를 사용할 수 없습니다.",
        "EMBEDDING_CACHE_CORRUPT": "임베딩 재사용 자료의 무결성을 확인해야 합니다.",
        "EMBEDDING_CACHE_CONFLICT": "동일 임베딩의 저장 결과가 일치하지 않습니다.",
        "EMBEDDING_CACHE_IN_PROGRESS": "동일한 임베딩 요청이 처리 중입니다.",
        "EMBEDDING_CACHE_CLAIM_LOST": "임베딩 요청의 처리 권한이 만료되었습니다.",
    }
    return ServiceError(code, messages[code], 503 if code != "EMBEDDING_CACHE_INVALID" else 422,
                        code == "EMBEDDING_CACHE_IN_PROGRESS")


def _database_error(exc):
    code = getattr(exc, "sqlite_errorcode", None)
    transient = {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_FULL,
                 sqlite3.SQLITE_CANTOPEN, sqlite3.SQLITE_READONLY, sqlite3.SQLITE_IOERR}
    return _error("EMBEDDING_CACHE_UNAVAILABLE" if code is not None and code & 255 in transient
                  else "EMBEDDING_CACHE_CORRUPT")


class EmbeddingCache:
    """Persist exact text/model/version/dimension reuse across local processes.

    ``model_key`` is a server-owned namespace including provider origin, route,
    model ID and configured version. Only its digest is stored. Optional claims
    prevent concurrent cache misses from causing duplicate paid requests. No
    SQLite transaction is held while the caller performs an external request.
    """

    def __init__(self, path, *, clock=time.time):
        try:
            if not isinstance(path, (str, os.PathLike)) or not str(path).strip():
                raise ValueError()
            if str(path).startswith(("\\\\", "//")):
                raise ValueError()
            target = Path(path).resolve()
            repo = Path(__file__).resolve().parents[2]
            if target == repo or repo in target.parents or not callable(clock):
                raise ValueError()
            target.parent.mkdir(parents=True, exist_ok=True)
            self.path, self._clock = target, clock
            if not target.exists():
                self._create()
            with self._connection() as connection:
                self._validate_schema(connection)
        except ServiceError:
            raise
        except sqlite3.Error as exc:
            raise _database_error(exc) from None
        except (OSError, ValueError, TypeError, RuntimeError):
            raise _error("EMBEDDING_CACHE_UNAVAILABLE") from None

    def _create(self):
        # Install a fully initialized database atomically. Competing processes
        # cannot observe the temporary database before its schema is committed.
        descriptor, temporary = tempfile.mkstemp(prefix=".embedding-", suffix=".sqlite3", dir=self.path.parent)
        os.close(descriptor)
        candidate = Path(temporary)
        connection = None
        try:
            connection = sqlite3.connect(candidate, isolation_level=None)
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(f"PRAGMA application_id={_APPLICATION_ID}")
            connection.execute(f"PRAGMA user_version={_VERSION}")
            for statement in _SCHEMA:
                connection.execute(statement)
            connection.execute("COMMIT")
            connection.close()
            connection = None
            try:
                os.link(candidate, self.path)
            except FileExistsError:
                pass
        finally:
            if connection is not None:
                connection.close()
            candidate.unlink(missing_ok=True)

    @contextmanager
    def _connection(self, *, write=False):
        connection = None
        try:
            # mode=rw prevents deletion or a wrong path from silently creating
            # an empty cache and triggering another billable embedding request.
            connection = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True,
                                         timeout=2.0, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA trusted_schema=OFF")
            if write:
                connection.execute("BEGIN IMMEDIATE")
            self._validate_schema(connection)
            yield connection
            if write:
                connection.execute("COMMIT")
        except sqlite3.Error as exc:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            raise _database_error(exc) from None
        except Exception:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _validate_schema(connection):
        if (connection.execute("PRAGMA application_id").fetchone()[0] != _APPLICATION_ID
                or connection.execute("PRAGMA user_version").fetchone()[0] != _VERSION):
            raise _error("EMBEDDING_CACHE_CORRUPT")
        objects = connection.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
        expected = {statement.split()[2]: statement for statement in _SCHEMA}
        if ({(row["type"], row["name"]) for row in objects}
                != {("table", name) for name in expected}):
            raise _error("EMBEDDING_CACHE_CORRUPT")
        for row in objects:
            if row["sql"] != expected[row["name"]]:
                raise _error("EMBEDDING_CACHE_CORRUPT")
        for table, expected_columns in _COLUMNS.items():
            columns = connection.execute("PRAGMA table_info(" + table + ")").fetchall()
            if [(row["name"], row["type"], row["notnull"], row["pk"]) for row in columns] != expected_columns:
                raise _error("EMBEDDING_CACHE_CORRUPT")

    @staticmethod
    def _key(model_key, dimension, text):
        try:
            if (not isinstance(model_key, str) or not model_key.strip() or len(model_key) > 4096
                    or type(dimension) is not int or not 1 <= dimension <= 8192
                    or not isinstance(text, str) or not text.strip()):
                raise ValueError()
            model, body = model_key.encode("utf-8"), text.encode("utf-8")
            if len(body) > 4_194_304:
                raise ValueError()
            value = (b"warranty-embedding-cache-v1\0" + struct.pack(">I", len(model)) + model
                     + struct.pack(">IQ", dimension, len(body)) + body)
            return hashlib.sha256(value).hexdigest()
        except (TypeError, ValueError, UnicodeError, OverflowError):
            raise _error("EMBEDDING_CACHE_INVALID") from None

    @staticmethod
    def _vector(vector, dimension):
        try:
            if not isinstance(vector, list) or len(vector) != dimension:
                raise ValueError()
            result = []
            for value in vector:
                if type(value) not in (int, float):
                    raise ValueError()
                converted = float(value)
                if not math.isfinite(converted):
                    raise ValueError()
                result.append(converted)
            return struct.pack("<" + "d" * dimension, *result)
        except (TypeError, ValueError, OverflowError, struct.error):
            raise _error("EMBEDDING_CACHE_INVALID") from None

    @staticmethod
    def _integrity(key, dimension, blob):
        return hashlib.sha256(b"embedding-vector-v1\0" + key.encode("ascii")
                              + struct.pack(">I", dimension) + blob).hexdigest()

    @classmethod
    def _decode(cls, row, key, dimension):
        if row is None:
            return None
        try:
            blob = row["vector_blob"]
            if (row["cache_key"] != key or type(row["dimension"]) is not int or row["dimension"] != dimension
                    or not isinstance(blob, bytes) or len(blob) != 8 * dimension
                    or not isinstance(row["integrity_hash"], str)
                    or row["integrity_hash"] != cls._integrity(key, dimension, blob)):
                raise ValueError()
            vector = list(struct.unpack("<" + "d" * dimension, blob))
            if any(not math.isfinite(value) for value in vector):
                raise ValueError()
            return vector
        except (KeyError, TypeError, ValueError, OverflowError, struct.error):
            raise _error("EMBEDDING_CACHE_CORRUPT") from None

    def _now(self):
        now = self._clock()
        if type(now) not in (int, float) or not math.isfinite(now) or now <= 0:
            raise _error("EMBEDDING_CACHE_UNAVAILABLE")
        return float(now)

    @staticmethod
    def _claim_row(row):
        if row is not None and (not isinstance(row["token"], str) or not _HEX.fullmatch(row["token"])
                                or type(row["expires_at"]) not in (int, float)
                                or not math.isfinite(row["expires_at"]) or row["expires_at"] <= 0):
            raise _error("EMBEDDING_CACHE_CORRUPT")
        return row

    def get(self, model_key, dimension, text):
        key = self._key(model_key, dimension, text)
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM embedding_vectors WHERE cache_key=?", (key,)).fetchone()
            return self._decode(row, key, dimension)

    def put(self, model_key, dimension, text, vector, *, claim_token=None):
        key = self._key(model_key, dimension, text)
        blob = self._vector(vector, dimension)
        if claim_token is not None and (not isinstance(claim_token, str) or not _HEX.fullmatch(claim_token)):
            raise _error("EMBEDDING_CACHE_INVALID")
        with self._connection(write=True) as connection:
            claim = self._claim_row(connection.execute("SELECT * FROM embedding_claims WHERE cache_key=?", (key,)).fetchone())
            now = self._now()
            if claim_token is not None:
                if claim is None or claim["token"] != claim_token or claim["expires_at"] <= now:
                    raise _error("EMBEDDING_CACHE_CLAIM_LOST")
            elif claim is not None and claim["expires_at"] > now:
                raise _error("EMBEDDING_CACHE_IN_PROGRESS")
            existing = connection.execute("SELECT * FROM embedding_vectors WHERE cache_key=?", (key,)).fetchone()
            self._decode(existing, key, dimension)
            if existing is not None and existing["vector_blob"] != blob:
                raise _error("EMBEDDING_CACHE_CONFLICT")
            connection.execute("INSERT OR IGNORE INTO embedding_vectors VALUES(?,?,?,?)",
                               (key, dimension, blob, self._integrity(key, dimension, blob)))
            connection.execute("DELETE FROM embedding_claims WHERE cache_key=?", (key,))

    def claim(self, model_key, dimension, text, *, lease_seconds=120):
        key = self._key(model_key, dimension, text)
        if (type(lease_seconds) not in (int, float) or not math.isfinite(lease_seconds)
                or not 1 <= lease_seconds <= 300):
            raise _error("EMBEDDING_CACHE_INVALID")
        with self._connection(write=True) as connection:
            # Corruption must stop before the caller can reserve or send again.
            self._decode(connection.execute("SELECT * FROM embedding_vectors WHERE cache_key=?", (key,)).fetchone(), key, dimension)
            current = self._claim_row(connection.execute("SELECT * FROM embedding_claims WHERE cache_key=?", (key,)).fetchone())
            now = self._now()
            if current is not None and current["expires_at"] > now:
                raise _error("EMBEDDING_CACHE_IN_PROGRESS")
            token = secrets.token_hex(32)
            connection.execute("INSERT OR REPLACE INTO embedding_claims VALUES(?,?,?)", (key, token, now + lease_seconds))
            return token

    def release(self, model_key, dimension, text, token):
        key = self._key(model_key, dimension, text)
        if not isinstance(token, str) or not _HEX.fullmatch(token):
            raise _error("EMBEDDING_CACHE_INVALID")
        with self._connection(write=True) as connection:
            return connection.execute("DELETE FROM embedding_claims WHERE cache_key=? AND token=?", (key, token)).rowcount == 1
