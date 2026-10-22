"""Persistent embedding reuse and cost-duplication guards, entirely offline."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from warranty.embedding_cache import EmbeddingCache
from warranty.errors import ServiceError


MODEL = 'gms:{"origin":"synthetic","model":"synthetic-vector","version":"v1"}'
TEXT = "Synthetic source body for exact persistent reuse."
VECTOR = [0.125, -0.5, 1.0]


class EmbeddingCacheTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.path = self.directory / "embedding.sqlite3"
        self.now = [1000.0]
        self.cache = EmbeddingCache(self.path, clock=lambda: self.now[0])
        for name in ("connect", "connect_ex"):
            self.stack.enter_context(patch.object(socket.socket, name, side_effect=AssertionError("Network forbidden")))
        self.stack.enter_context(patch.object(socket, "create_connection", side_effect=AssertionError("Network forbidden")))

    def assert_code(self, expected, operation):
        with self.assertRaises(ServiceError) as raised:
            operation()
        self.assertEqual(expected, raised.exception.code)
        public = json.dumps(raised.exception.as_dict())
        self.assertNotIn(TEXT, public)
        self.assertNotIn(str(self.path), public)

    def sql(self, statement, parameters=()):
        connection = sqlite3.connect(self.path)
        try:
            result = connection.execute(statement, parameters).fetchall()
            connection.commit()
            return result
        finally:
            connection.close()

    def test_restart_and_separate_python_process_reuse_only_hashes_and_vectors(self):
        self.assertIsNone(self.cache.get(MODEL, 3, TEXT))
        self.cache.put(MODEL, 3, TEXT, VECTOR)
        reopened = EmbeddingCache(self.path)
        self.assertEqual(VECTOR, reopened.get(MODEL, 3, TEXT))
        script = ("import json,sys; from warranty.embedding_cache import EmbeddingCache; "
                  "print(json.dumps(EmbeddingCache(sys.argv[1]).get(sys.argv[2],3,sys.argv[3])))")
        result = subprocess.run([sys.executable, "-c", script, str(self.path), MODEL, TEXT],
                                capture_output=True, text=True, timeout=10, shell=False)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(VECTOR, json.loads(result.stdout))
        self.assertEqual([(1,)], self.sql("SELECT count(*) FROM embedding_vectors"))
        content = self.path.read_bytes()
        for raw in (MODEL.encode(), TEXT.encode(), b"synthetic-vector", b"version"):
            self.assertNotIn(raw, content)
        self.assertEqual([], list(self.directory.glob(".embedding-*")))

    def test_namespace_binds_exact_utf8_model_version_dimension_and_text(self):
        value = "Synthetic \u00e9 body"
        self.cache.put(MODEL, 3, value, VECTOR)
        for model, dimension, text in ((MODEL.replace("v1", "v2"), 3, value),
                                       (MODEL.replace("origin", "other-origin"), 3, value),
                                       (MODEL, 2, value), (MODEL, 3, value + " "),
                                       (MODEL, 3, "Synthetic e\u0301 body")):
            with self.subTest(model=model, dimension=dimension, text=text):
                self.assertIsNone(self.cache.get(model, dimension, text))
        self.assertEqual(VECTOR, self.cache.get(MODEL, 3, value))

    def test_invalid_identity_fails_before_any_cache_lookup(self):
        for model, dimension, text in (("", 3, TEXT), ([], 3, TEXT), (MODEL, True, TEXT),
                                       (MODEL, 0, TEXT), (MODEL, 8193, TEXT), (MODEL, 3.0, TEXT),
                                       (MODEL, 3, ""), (MODEL, 3, " \n"), (MODEL, 3, None),
                                       (MODEL, 3, "\ud800"), (MODEL, 3, "x" * 4_194_305)):
            with self.subTest(dimension=dimension):
                self.assert_code("EMBEDDING_CACHE_INVALID", lambda: self.cache.get(model, dimension, text))
        self.assertEqual([(0,)], self.sql("SELECT count(*) FROM embedding_vectors"))

    def test_vector_validation_rejects_nonfinite_boolean_wrong_count_and_overflow(self):
        self.cache.put(MODEL, 3, TEXT, VECTOR)
        for value in ([True, 0, 0], [float("nan"), 0, 0], [float("inf"), 0, 0],
                      [10**400, 0, 0], ["1", 0, 0], [1, 2], (1, 2, 3), None):
            with self.subTest(value=type(value).__name__):
                self.assert_code("EMBEDDING_CACHE_INVALID", lambda: self.cache.put(MODEL, 3, TEXT, value))
        self.assertEqual(VECTOR, self.cache.get(MODEL, 3, TEXT))

    def test_corrupt_entry_is_neither_a_cache_miss_nor_silently_replaced(self):
        self.cache.put(MODEL, 3, TEXT, VECTOR)
        self.sql("UPDATE embedding_vectors SET integrity_hash=?", ("0" * 64,))
        for operation in (lambda: self.cache.get(MODEL, 3, TEXT),
                          lambda: self.cache.put(MODEL, 3, TEXT, VECTOR),
                          lambda: self.cache.claim(MODEL, 3, TEXT)):
            self.assert_code("EMBEDDING_CACHE_CORRUPT", operation)
        self.assertEqual([("0" * 64,)], self.sql("SELECT integrity_hash FROM embedding_vectors"))

    def test_schema_tampering_fails_closed_without_resetting_existing_data(self):
        self.cache.put(MODEL, 3, TEXT, VECTOR)
        self.sql("PRAGMA user_version=77")
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.assert_code("EMBEDDING_CACHE_CORRUPT", lambda: EmbeddingCache(self.path))
        self.assert_code("EMBEDDING_CACHE_CORRUPT", lambda: self.cache.get(MODEL, 3, TEXT))
        self.assertEqual(before, hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual([(1,)], self.sql("SELECT count(*) FROM embedding_vectors"))

    def test_existing_empty_foreign_and_invalid_databases_are_not_reinitialized(self):
        for name, data in (("empty", b""), ("invalid", b"synthetic invalid database")):
            target = self.directory / name
            target.write_bytes(data)
            self.assert_code("EMBEDDING_CACHE_CORRUPT", lambda: EmbeddingCache(target))
            self.assertEqual(data, target.read_bytes())
        target = self.directory / "foreign.sqlite3"
        connection = sqlite3.connect(target)
        connection.execute("CREATE TABLE unrelated(value TEXT)")
        connection.close()
        before = target.read_bytes()
        self.assert_code("EMBEDDING_CACHE_CORRUPT", lambda: EmbeddingCache(target))
        self.assertEqual(before, target.read_bytes())

    def test_deleted_store_is_an_error_instead_of_a_new_billable_miss(self):
        self.cache.put(MODEL, 3, TEXT, VECTOR)
        self.path.unlink()
        self.assert_code("EMBEDDING_CACHE_UNAVAILABLE", lambda: self.cache.get(MODEL, 3, TEXT))
        self.assertFalse(self.path.exists())

    def test_parallel_initialization_and_identical_puts_have_one_durable_row(self):
        target = self.directory / "concurrent.sqlite3"
        barrier = threading.Barrier(12)
        def write(_):
            barrier.wait(timeout=5)
            cache = EmbeddingCache(target)
            cache.put(MODEL, 3, TEXT, VECTOR)
            return cache.get(MODEL, 3, TEXT)
        with ThreadPoolExecutor(max_workers=12) as pool:
            self.assertEqual([VECTOR] * 12, list(pool.map(write, range(12))))
        cache = EmbeddingCache(target)
        self.assertEqual(VECTOR, cache.get(MODEL, 3, TEXT))
        connection = sqlite3.connect(target)
        try:
            self.assertEqual(1, connection.execute("SELECT count(*) FROM embedding_vectors").fetchone()[0])
        finally:
            connection.close()

    def test_conflicting_result_cannot_overwrite_the_first_valid_embedding(self):
        self.cache.put(MODEL, 3, TEXT, VECTOR)
        self.assert_code("EMBEDDING_CACHE_CONFLICT", lambda: self.cache.put(MODEL, 3, TEXT, [0.1, 0.2, 0.3]))
        self.assertEqual(VECTOR, self.cache.get(MODEL, 3, TEXT))

    def test_different_instances_contend_for_one_paid_request_claim(self):
        caches = [EmbeddingCache(self.path, clock=lambda: self.now[0]) for _ in range(20)]
        barrier = threading.Barrier(20)
        def acquire(cache):
            barrier.wait(timeout=5)
            try:
                return cache.claim(MODEL, 3, TEXT)
            except ServiceError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=20) as pool:
            outcomes = list(pool.map(acquire, caches))
        self.assertEqual(19, outcomes.count("EMBEDDING_CACHE_IN_PROGRESS"))
        token = next(value for value in outcomes if value != "EMBEDDING_CACHE_IN_PROGRESS")
        self.cache.put(MODEL, 3, TEXT, VECTOR, claim_token=token)
        self.assertEqual(VECTOR, caches[-1].get(MODEL, 3, TEXT))
        self.assertEqual([(0,)], self.sql("SELECT count(*) FROM embedding_claims"))

    def test_expired_claim_cannot_write_or_release_a_replacement_claim(self):
        old = self.cache.claim(MODEL, 3, TEXT, lease_seconds=2)
        self.now[0] += 3
        current = self.cache.claim(MODEL, 3, TEXT)
        self.assertFalse(self.cache.release(MODEL, 3, TEXT, old))
        self.assert_code("EMBEDDING_CACHE_CLAIM_LOST", lambda: self.cache.put(MODEL, 3, TEXT, VECTOR, claim_token=old))
        self.assert_code("EMBEDDING_CACHE_IN_PROGRESS", lambda: self.cache.put(MODEL, 3, TEXT, VECTOR))
        self.cache.put(MODEL, 3, TEXT, VECTOR, claim_token=current)
        self.assertEqual(VECTOR, self.cache.get(MODEL, 3, TEXT))

    def test_failed_request_can_release_its_claim_without_creating_a_result(self):
        token = self.cache.claim(MODEL, 3, TEXT)
        self.assertTrue(self.cache.release(MODEL, 3, TEXT, token))
        self.assertFalse(self.cache.release(MODEL, 3, TEXT, token))
        self.assertIsNone(self.cache.get(MODEL, 3, TEXT))
        replacement = self.cache.claim(MODEL, 3, TEXT)
        self.cache.put(MODEL, 3, TEXT, VECTOR, claim_token=replacement)
        self.assertEqual(VECTOR, self.cache.get(MODEL, 3, TEXT))

    def test_claim_does_not_hold_a_database_transaction_during_external_work(self):
        token = self.cache.claim(MODEL, 3, TEXT)
        other = sqlite3.connect(self.path, timeout=0.1)
        try:
            other.execute("BEGIN IMMEDIATE")
            other.rollback()
        finally:
            other.close()
        self.cache.put(MODEL, 3, TEXT, VECTOR, claim_token=token)

    def test_corrupted_claim_cannot_be_treated_as_expired_and_send_again(self):
        self.cache.claim(MODEL, 3, TEXT)
        self.sql("UPDATE embedding_claims SET token=?", ("x" * 64,))
        self.assert_code("EMBEDDING_CACHE_CORRUPT", lambda: self.cache.claim(MODEL, 3, TEXT))
        self.assert_code("EMBEDDING_CACHE_CORRUPT", lambda: self.cache.put(MODEL, 3, TEXT, VECTOR))

    def test_private_cache_rejects_project_storage_and_network_paths(self):
        target = Path(__file__).resolve().parents[1] / "forbidden-embedding-cache.sqlite3"
        self.assert_code("EMBEDDING_CACHE_UNAVAILABLE", lambda: EmbeddingCache(target))
        self.assertFalse(target.exists())
        self.assert_code("EMBEDDING_CACHE_UNAVAILABLE", lambda: EmbeddingCache("//synthetic-host/cache.sqlite3"))

    def test_busy_write_is_a_safe_error_and_preserves_the_prior_embedding(self):
        self.cache.put(MODEL, 3, TEXT, VECTOR)
        connection = sqlite3.connect(self.path, isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            self.assert_code("EMBEDDING_CACHE_UNAVAILABLE", lambda: self.cache.put(MODEL, 3, "Other synthetic body", VECTOR))
        finally:
            connection.rollback()
            connection.close()
        self.assertEqual(VECTOR, self.cache.get(MODEL, 3, TEXT))
        self.assertIsNone(self.cache.get(MODEL, 3, "Other synthetic body"))


if __name__ == "__main__":
    unittest.main()
