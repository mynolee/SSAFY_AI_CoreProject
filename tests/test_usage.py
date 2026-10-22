"""Model request budgets use synthetic private SQLite files and no providers."""
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
import hashlib
import json
import multiprocessing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from warranty.errors import ServiceError
from warranty.usage import UsageBudget


FINGERPRINT = hashlib.sha256(b"SYNTHETIC approved provider context").hexdigest()
LIMITS = {"chat_requests": 5, "embedding_requests": 4, "input_bytes": 100}


def process_charge(arguments):
    path, limits = arguments
    budget = UsageBudget(path, "SYNTHETIC-approval", limits, context_fingerprint=FINGERPRINT)
    try:
        budget.consume("chat", 1)
        return "ACCEPTED"
    except ServiceError as error:
        return error.code


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="warranty-usage-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "private" / "model_usage.sqlite3"

    def budget(self, *, approval="SYNTHETIC-approval", limits=None, fingerprint=FINGERPRINT):
        return UsageBudget(self.path, approval, LIMITS if limits is None else limits, context_fingerprint=fingerprint)

    def assert_code(self, code, callback):
        with self.assertRaises(ServiceError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn(str(self.path), caught.exception.message)
        return caught.exception

    def test_constructor_and_snapshot_are_read_only_and_status_has_no_private_identity(self):
        budget = self.budget()
        self.assertTrue(budget.ready)
        self.assertTrue(budget.approval_matches("SYNTHETIC-approval"))
        self.assertFalse(budget.approval_matches("another-approval"))
        for invalid in (None, [], True, "배정되지않은승인"):
            self.assertFalse(budget.approval_matches(invalid))
        snapshot = budget.snapshot()
        self.assertEqual((snapshot["chat_requests"], snapshot["embedding_requests"], snapshot["input_bytes"]), (0, 0, 0))
        self.assertEqual(snapshot["limits"], LIMITS)
        for secret in (str(self.path), FINGERPRINT, "SYNTHETIC-approval"):
            self.assertNotIn(secret, json.dumps(snapshot))
        self.assertFalse(self.path.parent.exists())

    def test_unapproved_and_zero_limits_do_not_create_files(self):
        for approval in (None, ""):
            with self.subTest(approval=approval):
                budget = self.budget(approval=approval)
                self.assertFalse(budget.ready)
                self.assertFalse(budget.approval_matches("SYNTHETIC-approval"))
                self.assert_code("PROCESSING_APPROVAL_REQUIRED", lambda: budget.consume("chat", 1))
        for limits, kind in (({**LIMITS, "chat_requests": 0}, "chat"),
                             ({**LIMITS, "embedding_requests": 0}, "embedding"),
                             ({**LIMITS, "input_bytes": 0}, "chat")):
            with self.subTest(limits=limits):
                budget = self.budget(limits=limits)
                self.assert_code("USAGE_LIMIT_REACHED", lambda: budget.consume(kind, 0))
        self.assertFalse(self.path.parent.exists())

    def test_requests_and_exact_bytes_are_shared_persistent_counters(self):
        budget = self.budget()
        budget.consume("chat", len("합성".encode("utf-8")))
        budget.consume("embedding", 7)
        reopened = self.budget()
        snapshot = reopened.snapshot()
        self.assertEqual((snapshot["chat_requests"], snapshot["embedding_requests"], snapshot["input_bytes"]), (1, 1, 13))
        reopened.consume("chat", 3)
        self.assertEqual(budget.snapshot()["input_bytes"], 16)

    def test_existing_empty_or_schema_less_store_is_never_initialized(self):
        self.path.parent.mkdir(parents=True)
        for schema_less in (False, True):
            with self.subTest(schema_less=schema_less):
                self.path.write_bytes(b"")
                if schema_less:
                    with sqlite3.connect(self.path) as connection:
                        connection.execute("PRAGMA user_version=0")
                    connection.close()
                before = self.path.read_bytes()
                budget = self.budget()
                self.assertFalse(budget.ready)
                self.assertFalse(budget.approval_matches("SYNTHETIC-approval"))
                self.assert_code("PROCESSING_APPROVAL_REQUIRED", lambda: budget.consume("chat", 1))
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_truncated_or_unversioned_existing_usage_cannot_reset_counts(self):
        budget = self.budget()
        budget.consume("chat", 9)
        original = self.path.read_bytes()
        for mutation in ("unversioned", "truncated"):
            with self.subTest(mutation=mutation):
                self.path.write_bytes(original)
                if mutation == "truncated":
                    self.path.write_bytes(b"")
                else:
                    with sqlite3.connect(self.path) as connection:
                        connection.execute("PRAGMA user_version=0")
                    connection.close()
                before = self.path.read_bytes()
                for reopened in (budget, self.budget(), self.budget(approval="new-ID")):
                    self.assertFalse(reopened.ready)
                    self.assert_code("PROCESSING_APPROVAL_REQUIRED", lambda: reopened.consume("chat", 1))
                self.assertEqual(self.path.read_bytes(), before)
        self.path.write_bytes(original)
        self.assertEqual(self.budget().snapshot()["chat_requests"], 1)

    def test_interrupted_bootstrap_is_not_deleted_or_automatically_repaired(self):
        self.path.parent.mkdir(parents=True)
        lock = self.path.with_name(self.path.name + ".initializing")
        lock.write_bytes(b"")
        budget = self.budget()
        with patch("warranty.usage.time.monotonic", side_effect=[0, 3, 0, 3]):
            self.assertFalse(budget.ready)
            self.assert_code("PROCESSING_APPROVAL_REQUIRED", lambda: budget.consume("chat", 1))
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.path.parent.iterdir()), [lock])

    def test_denied_consumption_never_increments_counts(self):
        budget = self.budget(limits={"chat_requests": 1, "embedding_requests": 1, "input_bytes": 4})
        self.assert_code("USAGE_LIMIT_REACHED", lambda: budget.consume("chat", 5))
        self.assertFalse(self.path.exists())
        budget.consume("chat", 3)
        before = budget.snapshot()
        self.assert_code("USAGE_LIMIT_REACHED", lambda: budget.consume("chat", 0))
        self.assert_code("USAGE_LIMIT_REACHED", lambda: budget.consume("embedding", 2))
        self.assertEqual(budget.snapshot(), before)
        budget.consume("embedding", 1)
        self.assertFalse(budget.ready)
        self.assertTrue(budget.approval_matches("SYNTHETIC-approval"))
        self.assert_code("USAGE_LIMIT_REACHED", lambda: budget.consume("embedding", 0))

    def test_same_approval_cannot_change_caps_or_context_after_restart(self):
        budget = self.budget()
        budget.consume("chat", 9)
        before = budget.snapshot()
        mutations = [self.budget(limits={**LIMITS, "chat_requests": 6}),
                     self.budget(limits={**LIMITS, "input_bytes": 99}),
                     self.budget(fingerprint=hashlib.sha256(b"another synthetic context").hexdigest())]
        for changed in mutations:
            with self.subTest(changed=changed):
                self.assertFalse(changed.ready)
                self.assertFalse(changed.approval_matches("SYNTHETIC-approval"))
                self.assert_code("PROCESSING_APPROVAL_REQUIRED", lambda: changed.consume("chat", 1))
        self.assertEqual(budget.snapshot(), before)

    def test_caller_mutations_cannot_change_owned_limit_configuration(self):
        limits = dict(LIMITS)
        budget = self.budget(limits=limits)
        limits["chat_requests"] = 0
        public = budget.snapshot()
        public["limits"]["chat_requests"] = 0
        public["remaining"]["chat_requests"] = 0
        budget.consume("chat", 1)
        self.assertEqual(budget.snapshot()["limits"], LIMITS)

    def test_concurrent_instances_allow_only_configured_request_count(self):
        limits = {"chat_requests": 7, "embedding_requests": 0, "input_bytes": 100}
        with ThreadPoolExecutor(max_workers=20) as executor:
            results = list(executor.map(process_charge, [(str(self.path), limits)] * 40))
        self.assertEqual(results.count("ACCEPTED"), 7)
        self.assertEqual(results.count("USAGE_LIMIT_REACHED"), 33)
        snapshot = self.budget(limits=limits).snapshot()
        self.assertEqual((snapshot["chat_requests"], snapshot["input_bytes"]), (7, 7))

    def test_independent_processes_share_the_same_persistent_limit(self):
        limits = {"chat_requests": 5, "embedding_requests": 0, "input_bytes": 100}
        with ProcessPoolExecutor(max_workers=3, mp_context=multiprocessing.get_context("spawn")) as executor:
            results = list(executor.map(process_charge, [(str(self.path), limits)] * 15))
        self.assertEqual(results.count("ACCEPTED"), 5)
        self.assertEqual(results.count("USAGE_LIMIT_REACHED"), 10)
        self.assertEqual(self.budget(limits=limits).snapshot()["chat_requests"], 5)

    def test_concurrent_first_configuration_binds_one_immutable_context(self):
        def charge(limits):
            try:
                self.budget(limits=limits).consume("chat", 1)
                return "ACCEPTED"
            except ServiceError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(charge, [LIMITS, {**LIMITS, "chat_requests": 6}]))
        self.assertEqual(sorted(outcomes), ["ACCEPTED", "PROCESSING_APPROVAL_REQUIRED"])

    def test_failed_transport_has_no_refund_interface_or_counter_reversal(self):
        budget = self.budget()
        with self.assertRaisesRegex(OSError, "synthetic transport"):
            budget.consume("chat", 12)
            raise OSError("synthetic transport failed after reservation")
        snapshot = self.budget().snapshot()
        self.assertEqual((snapshot["chat_requests"], snapshot["input_bytes"]), (1, 12))
        self.assertFalse(hasattr(budget, "refund"))

    def test_uncertain_commit_remains_charged_and_fails_closed(self):
        class UncertainCommit(sqlite3.Connection):
            def commit(self):
                super().commit()
                raise sqlite3.OperationalError("SYNTHETIC uncertain commit")
        budget = self.budget()
        def connect(*, read_only):
            if read_only:
                raise AssertionError("consume unexpectedly read a snapshot")
            connection = sqlite3.connect(self.path, isolation_level=None, factory=UncertainCommit)
            connection.row_factory = sqlite3.Row
            return connection
        with patch.object(budget, "_connect", side_effect=connect):
            self.assert_code("PROCESSING_APPROVAL_REQUIRED", lambda: budget.consume("chat", 11))
        self.assertEqual((self.budget().snapshot()["chat_requests"], self.budget().snapshot()["input_bytes"]), (1, 11))

    def test_corrupt_storage_and_io_failure_are_safe_and_never_reset_limits(self):
        self.path.parent.mkdir()
        self.path.write_bytes(b"SYNTHETIC invalid SQLite bytes")
        budget = self.budget()
        self.assertFalse(budget.ready)
        self.assert_code("PROCESSING_APPROVAL_REQUIRED", lambda: budget.consume("chat", 1))
        self.assertEqual(self.path.read_bytes(), b"SYNTHETIC invalid SQLite bytes")
        with patch.object(budget, "_connect", side_effect=OSError("SYNTHETIC private storage path")):
            self.assertFalse(budget.ready)
            self.assert_code("PROCESSING_APPROVAL_REQUIRED", lambda: budget.consume("embedding", 1))

    def test_unrelated_database_is_never_used_for_budget_storage(self):
        self.path.parent.mkdir()
        connection = sqlite3.connect(self.path)
        connection.execute("CREATE TABLE unrelated_user_records(value INTEGER)")
        connection.execute("INSERT INTO unrelated_user_records VALUES(9)")
        connection.commit()
        connection.close()
        budget = self.budget()
        self.assertFalse(budget.ready)
        self.assert_code("PROCESSING_APPROVAL_REQUIRED", lambda: budget.consume("chat", 1))
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT value FROM unrelated_user_records").fetchone()[0], 9)
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='model_usage_approvals'").fetchone())
        connection.close()

    def test_only_approval_configuration_counters_and_timestamps_are_persisted(self):
        self.budget().consume("chat", 8)
        with sqlite3.connect(self.path) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(model_usage_approvals)")}
        connection.close()
        self.assertEqual(columns, {"approval_id", "context_fingerprint", "chat_limit", "embedding_limit", "byte_limit",
                                   "chat_requests", "embedding_requests", "input_bytes", "created_at", "updated_at"})

    def test_strict_configuration_and_consume_types_reject_booleans_and_bounds(self):
        invalid_limits = [[], {}, {**LIMITS, "extra": 1}, {**LIMITS, "chat_requests": True},
                          {**LIMITS, "embedding_requests": -1}, {**LIMITS, "chat_requests": 1_000_001},
                          {**LIMITS, "input_bytes": 1_000_000_000_001}, {**LIMITS, "input_bytes": 1.0}]
        for limits in invalid_limits:
            with self.subTest(limits=limits):
                self.assert_code("CONFIG_INVALID", lambda: self.budget(limits=limits))
        for fingerprint in (None, "raw question text", "a" * 63, "g" * 64):
            self.assert_code("CONFIG_INVALID", lambda: self.budget(fingerprint=fingerprint))
        for approval in (True, [], " ", "approval with spaces"):
            self.assert_code("CONFIG_INVALID", lambda: self.budget(approval=approval))
        budget = self.budget()
        for kind, count in (("unknown", 1), ([], 1), ("chat", True), ("chat", -1), ("embedding", 1.0)):
            self.assert_code("INVALID_INPUT", lambda: budget.consume(kind, count))
        self.assertFalse(self.path.parent.exists())


if __name__ == "__main__":
    unittest.main()
