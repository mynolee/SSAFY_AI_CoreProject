"""Synthetic local document worker lifecycle and concurrency checks."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from warranty.catalog import Catalog
from warranty.db import Database
from warranty.errors import ServiceError
from warranty.jobs import DocumentJobQueue, _Progress


class DocumentJobTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="warranty-document-jobs-")
        self.addCleanup(self.temporary.cleanup)
        self.db = Database(Path(self.temporary.name) / "app.sqlite3")
        self.db.initialize()
        self.catalog = Catalog.synthetic()
        self.item_id = self.catalog.items[0]["item_id"]
        with self.db.transaction() as connection:
            for item in self.catalog.items:
                connection.execute("INSERT INTO items(item_id,name,provider) VALUES(?,?,?)",
                                   (item["item_id"], item["name"], item["provider"]))
        self.calls = []
        self.current_time = 1000.0
        self.queue = self.make_queue()

    def make_queue(self, handler=None, **options):
        queue = DocumentJobQueue(self.db, self.catalog, handler or self.handler,
                                 manager_check=lambda actor: actor == "source_manager", **options)
        self.addCleanup(queue.stop)
        return queue

    def handler(self, request, progress):
        self.calls.append(request)
        progress("DISCOVERING", candidate_count=2)
        progress("INDEXING", body_count=1, chunk_count=3)
        return {"status": "COMPLETED", "stage": "INDEX_READY", "candidate_count": 2, "body_count": 1, "chunk_count": 3}

    def enqueue(self, queue=None, **request):
        return (queue or self.queue).enqueue("buyer", self.item_id, request)

    def clock(self):
        return self.current_time

    def test_enqueue_persists_without_running_and_reopen_uses_server_snapshot(self):
        request = {"question": "  SYNTHETIC prepare question  ", "applicability": {"country": " KR "}}
        queued = self.queue.enqueue("buyer", self.item_id, request)
        request["question"] = "SYNTHETIC modified client object"
        request["applicability"]["country"] = "SYNTHETIC changed"
        self.assertEqual("QUEUED", queued["status"])
        self.assertEqual([], self.calls)
        self.assertEqual(0, queued["attempt_no"])
        reopened = self.make_queue()
        result = reopened.run_pending()
        self.assertEqual(1, result["processed_jobs"])
        self.assertEqual("SYNTHETIC prepare question", self.calls[0]["question"])
        self.assertEqual({"country": "KR"}, self.calls[0]["applicability"])
        self.assertEqual("buyer", self.calls[0]["requester"])
        self.assertEqual(self.catalog.dataset_hash, self.calls[0]["dataset_hash"])
        self.assertEqual("COMPLETED", reopened.job("buyer", queued["job_id"])["status"])

    def test_concurrent_duplicate_enqueue_and_claim_execute_once(self):
        with ThreadPoolExecutor(max_workers=20) as pool:
            views = list(pool.map(lambda _: self.enqueue(question="SYNTHETIC same request"), range(20)))
        self.assertEqual(1, len({view["job_id"] for view in views}))
        with ThreadPoolExecutor(max_workers=10) as pool:
            workers = list(pool.map(lambda _: self.queue.run_pending(), range(10)))
        self.assertEqual(1, sum(result["processed_jobs"] for result in workers))
        self.assertEqual(1, len(self.calls))
        self.assertEqual(1, self.queue.job("buyer", views[0]["job_id"])["attempt_no"])
        self.assertEqual(0, self.queue.run_pending()["processed_jobs"])

    def test_progress_is_pollable_while_handler_runs_and_never_exposes_payload(self):
        entered, release = threading.Event(), threading.Event()
        def handler(request, progress):
            progress("FETCHING", candidate_count=1)
            entered.set()
            if not release.wait(3):
                raise RuntimeError("SYNTHETIC test deadline")
            return {"status": "COMPLETED", "body_count": 1}
        queue = self.make_queue(handler)
        queued = self.enqueue(queue, question="SYNTHETIC private question")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(queue.run_pending)
            try:
                self.assertTrue(entered.wait(2))
                live = queue.job("buyer", queued["job_id"])
                self.assertEqual(("RUNNING", "FETCHING", 1), (live["status"], live["stage"], live["candidate_count"]))
                for private in ("request_json", "request_hash", "claim_token", "question", "applicability", "dataset_hash", "lease_until"):
                    self.assertNotIn(private, live)
            finally:
                release.set()
            self.assertEqual(1, future.result(timeout=3)["processed_jobs"])

    def test_job_owner_and_manager_are_checked_without_admin_proxy(self):
        queued = self.enqueue(question="SYNTHETIC owner only")
        for actor in ("seller", "admin", "outsider", "not-a-server-user", []):
            with self.subTest(actor=actor), self.assertRaises(ServiceError) as caught:
                self.queue.job(actor, queued["job_id"])
            self.assertEqual(403, caught.exception.status)
        self.assertEqual(queued["job_id"], self.queue.job("source_manager", queued["job_id"])["job_id"])
        with self.assertRaises(ServiceError):
            self.queue.enqueue("not-a-server-user", self.item_id)

    def test_payload_cannot_select_actor_target_handler_or_shell(self):
        for request in ({"actor": "admin"}, {"requester": "seller"}, {"item_id": "SYNTHETIC-002"},
                        {"handler": "os.system"}, {"command": "cmd /c whoami"},
                        {"applicability": []}, {"question": ""}, {"applicability": {"country": False}}):
            with self.subTest(request=request), self.assertRaises(ServiceError):
                self.queue.enqueue("buyer", self.item_id, request)
        self.assertEqual([], self.calls)
        with self.db.connect() as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM index_jobs").fetchone()[0])

    def test_failure_is_safe_and_bounded_explicit_retry_keeps_same_job(self):
        def failed(request, progress):
            progress("FETCHING")
            raise ServiceError("FETCH_FAILED", "SYNTHETIC secret stack and private path", 503, True)
        queue = self.make_queue(failed, max_attempts=2)
        queued = self.enqueue(queue)
        queue.run_pending()
        failed_view = queue.job("buyer", queued["job_id"])
        self.assertEqual(("FAILED", "FETCH_FAILED", True), (failed_view["status"], failed_view["failure_code"], failed_view["retryable"]))
        self.assertNotIn("secret", json.dumps(failed_view))
        with self.assertRaises(ServiceError):
            queue.retry("admin", queued["job_id"])
        retried = queue.retry("buyer", queued["job_id"])
        self.assertEqual((queued["job_id"], "QUEUED"), (retried["job_id"], retried["status"]))
        queue.run_pending()
        final = queue.job("buyer", queued["job_id"])
        self.assertEqual(2, final["attempt_no"])
        self.assertFalse(final["retryable"])
        with self.assertRaises(ServiceError) as caught:
            queue.retry("buyer", queued["job_id"])
        self.assertEqual("JOB_RETRY_LIMIT", caught.exception.code)

    def test_retry_deduplicates_against_an_already_active_same_request(self):
        queue = self.make_queue(lambda request, progress: {"status": "FAILED", "failure_code": "FETCH_FAILED"})
        original = self.enqueue(queue)
        queue.run_pending()
        newer = self.enqueue(queue)
        replay = queue.retry("buyer", original["job_id"])
        self.assertEqual(newer["job_id"], replay["job_id"])
        with self.db.connect() as connection:
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM index_jobs WHERE status='QUEUED'").fetchone()[0])

    def test_expired_claim_recovers_and_old_token_cannot_write_progress_or_completion(self):
        queue = self.make_queue(clock=self.clock, lease_seconds=2)
        queued = self.enqueue(queue)
        old = queue._claim()
        self.assertEqual("RUNNING", queue.job("buyer", queued["job_id"])["status"])
        self.current_time += 3
        reopened = self.make_queue(clock=self.clock, lease_seconds=2)
        self.assertEqual(1, reopened.run_pending()["processed_jobs"])
        current = reopened.job("buyer", queued["job_id"])
        self.assertEqual(("COMPLETED", 2), (current["status"], current["attempt_no"]))
        with self.assertRaises(ServiceError) as caught:
            _Progress(queue, old["job_id"], old["claim_token"])("INDEXING", chunk_count=999)
        self.assertEqual("JOB_CLAIM_LOST", caught.exception.code)
        self.assertFalse(queue._finish(old, {"status": "FAILED", "failure_code": "FETCH_FAILED"}))
        self.assertEqual(current, reopened.job("buyer", queued["job_id"]))

    def test_commit_fence_rolls_back_stale_handler_domain_writes(self):
        queue = self.make_queue(clock=self.clock, lease_seconds=1)
        queued = self.enqueue(queue)
        claim = queue._claim()
        with self.db.connect() as connection:
            connection.execute("CREATE TABLE synthetic_document_commit(value INTEGER NOT NULL)")
        self.current_time += 2
        with self.assertRaises(ServiceError):
            with self.db.transaction() as connection:
                connection.execute("INSERT INTO synthetic_document_commit VALUES(1)")
                _Progress(queue, queued["job_id"], claim["claim_token"]).check(connection)
        with self.db.connect() as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM synthetic_document_commit").fetchone()[0])

    def test_crash_recovery_has_a_finite_attempt_budget(self):
        queue = self.make_queue(clock=self.clock, lease_seconds=1, max_attempts=2)
        queued = self.enqueue(queue)
        self.assertIsNotNone(queue._claim())
        self.current_time += 2
        self.assertIsNotNone(queue._claim())
        self.current_time += 2
        self.assertEqual(1, queue.recover_expired())
        self.assertIsNone(queue._claim())
        final = queue.job("buyer", queued["job_id"])
        self.assertEqual(("FAILED", 2, "JOB_RETRY_LIMIT"), (final["status"], final["attempt_no"], final["failure_code"]))
        self.assertEqual([], self.calls)

    def test_heartbeat_prevents_a_second_worker_claiming_a_slow_handler(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def handler(request, progress):
            calls.append(request["attempt_no"])
            entered.set()
            if not release.wait(4):
                raise RuntimeError("SYNTHETIC test deadline")
            return {"status": "COMPLETED"}
        queue = self.make_queue(handler, lease_seconds=.6)
        queued = self.enqueue(queue)
        competitor = self.make_queue(handler, lease_seconds=.6)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(queue.run_pending)
            try:
                self.assertTrue(entered.wait(2))
                self.assertFalse(release.wait(1.0))
                self.assertEqual(0, competitor.run_pending()["processed_jobs"])
            finally:
                release.set()
            self.assertEqual(1, future.result(timeout=3)["processed_jobs"])
        self.assertEqual([1], calls)
        self.assertEqual(1, queue.job("buyer", queued["job_id"])["attempt_no"])

    def test_changed_dataset_or_revoked_actor_blocks_before_handler(self):
        queued = self.enqueue()
        self.catalog.dataset_hash = "SYNTHETIC-new-dataset-revision"
        self.queue.run_pending()
        self.assertEqual("DATASET_CHANGED", self.queue.job("buyer", queued["job_id"])["failure_code"])
        self.assertEqual([], self.calls)
        authorized = {"enqueue", "read"}
        queue = self.make_queue(authorize=lambda actor, item, action: action in authorized)
        other = self.enqueue(queue, question="SYNTHETIC revoked before run")
        queue.run_pending()
        self.assertEqual("BLOCKED", queue.job("buyer", other["job_id"])["status"])
        self.assertEqual([], self.calls)

    def test_corrupt_server_snapshot_and_invalid_handler_result_are_safe(self):
        job_id = uuid4().hex
        forged = {"requester": "seller", "item_id": self.item_id, "question": "SYNTHETIC forged owner",
                  "applicability": {}, "dataset_hash": self.catalog.dataset_hash}
        encoded = json.dumps(forged, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self.db.transaction() as connection:
            connection.execute("INSERT INTO index_jobs(job_id,requester,item_id,status,stage) VALUES(?,'buyer',?,'QUEUED','QUEUED')", (job_id, self.item_id))
            connection.execute("INSERT INTO document_job_runs(job_id,request_json,request_hash) VALUES(?,?,?)",
                               (job_id, encoded, hashlib.sha256(encoded.encode()).hexdigest()))
        self.queue.run_pending()
        self.assertEqual("JOB_REQUEST_INVALID", self.queue.job("buyer", job_id)["failure_code"])
        self.assertEqual([], self.calls)
        invalid = self.make_queue(lambda request, progress: {"status": "COMPLETED", "chunk_count": True})
        queued = self.enqueue(invalid, question="SYNTHETIC invalid result")
        invalid.run_pending()
        self.assertEqual("JOB_RESULT_INVALID", invalid.job("buyer", queued["job_id"])["failure_code"])

    def test_legacy_job_polling_remains_owner_scoped_and_cannot_be_retried(self):
        job_id = uuid4().hex
        with self.db.transaction() as connection:
            connection.execute("INSERT INTO index_jobs(job_id,requester,item_id,status,stage) VALUES(?,'buyer',?,'BLOCKED','DOC_BLOCKED')", (job_id, self.item_id))
        self.assertEqual(0, self.queue.job("buyer", job_id)["attempt_no"])
        with self.assertRaises(ServiceError):
            self.queue.job("seller", job_id)
        with self.assertRaises(ServiceError):
            self.queue.retry("buyer", job_id)

    def test_crashed_legacy_rows_do_not_consume_managed_queue_capacity(self):
        with self.db.transaction() as connection:
            for _ in range(20):
                connection.execute("INSERT INTO index_jobs(job_id,requester,item_id,status,stage) VALUES(?,'buyer',?,'RUNNING','INDEXING')", (uuid4().hex, self.item_id))
        queued = self.enqueue()
        self.assertEqual("QUEUED", queued["status"])
        self.assertEqual(1, self.queue.run_pending()["processed_jobs"])

    def test_background_worker_returns_enqueue_before_completion_and_stops_cleanly(self):
        entered, release = threading.Event(), threading.Event()
        def handler(request, progress):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("SYNTHETIC test deadline")
            return {"status": "COMPLETED"}
        queue = self.make_queue(handler, auto_start=True)
        before = time.monotonic()
        queued = self.enqueue(queue)
        try:
            self.assertLess(time.monotonic() - before, 1)
            self.assertTrue(entered.wait(2))
            self.assertIn(queue.job("buyer", queued["job_id"])["status"], {"QUEUED", "RUNNING"})
            self.assertFalse(queue.start())
        finally:
            release.set()
        self.assertTrue(queue.stop(timeout=3))
        self.assertFalse(queue.worker_running)
        self.assertEqual("COMPLETED", queue.job("buyer", queued["job_id"])["status"])


if __name__ == "__main__":
    unittest.main()
