"""Synthetic private registrations, fetch fencing, and additive migration checks."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from warranty.catalog import Catalog
from warranty.config import Settings
from warranty.db import Database
from warranty.documents import ACTIONS, DocumentRegistry, digest, parse_document
from warranty.errors import ServiceError
from warranty.security import FetchResult, SourcePolicy
import warranty.db as database_module


SCOPE = {"country": "SYNTHETIC", "plan_generation": "SYNTHETIC-PLAN",
         "payment_method": "SYNTHETIC-MONTHLY", "channel": "SYNTHETIC",
         "document_type": "SERVICE_TERMS", "date_start": "2020-01-01", "date_end": "2030-01-01"}
BODY = b"<h1>SYNTHETIC</h1><p>Authored transfer clause A.</p>"
OTHER_BODY = b"<h1>SYNTHETIC</h1><p>Authored transfer clause B.</p>"


class FakeFetcher:
    def __init__(self):
        self.calls = 0
        self.body = BODY
        self.failure = None

    def fetch(self, source_id, url, actor=None):
        self.calls += 1
        if self.failure:
            raise self.failure
        return FetchResult(self.body, "text/html", url, 200)


class DocumentRegistryRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="warranty-document-registry-")
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(Path(self.temp.name) / "data", Path(self.temp.name) / "config")
        self.settings.prepare()
        self.db = Database(self.settings.data_dir / "private.sqlite3")
        self.addCleanup(self.db.close)
        self.db.initialize()
        self.catalog = Catalog.synthetic()
        self.item, self.other = (item["item_id"] for item in self.catalog.items)
        self.reviews = [{"source_id": "SYNTHETIC-SOURCE", "action": action, "status": "ALLOWED",
                         "basis_type": "OTHER_RECORDED_BASIS", "basis_url": "https://example.org/",
                         "reviewed_on": "2026-01-01", "scope": "Authored synthetic fixture",
                         "conditions": "Offline tests only", "user_scope": "test", "processing_environment": "LOCAL"}
                        for action in ACTIONS]
        self.policy = SourcePolicy([{"source_id": "SYNTHETIC-SOURCE", "url": "https://example.org/synthetic",
                                     "approved_urls": ["https://example.org/synthetic", "https://example.org/other"],
                                     "allowed_hosts": ["example.org"]}], self.reviews, {"manager": {"DATA_MANAGER"}})
        self.fetcher = FakeFetcher()
        self.registry = DocumentRegistry(self.catalog, self.db, self.settings, self.policy, self.fetcher)
        self.document = self.registry.register_document("manager", source_id="SYNTHETIC-SOURCE", title="SYNTHETIC terms",
            canonical_url="https://example.org/synthetic", document_type="HTML", is_synthetic=True)

    def target(self, item=None, **kwargs):
        return self.registry.register_target("manager", item or self.item, self.document["document_id"],
            role=kwargs.pop("role", "SERVICE_TERMS"), applicability=kwargs.pop("applicability", SCOPE),
            applicability_basis=kwargs.pop("applicability_basis", "Authored fixture explicitly covers this synthetic item."), **kwargs)

    def test_unfetched_own_document_is_discoverable_without_fabricated_body(self):
        target = self.target(locator="Synthetic section A")
        self.assertEqual(target["id"], target["target_id"])
        self.assertTrue(target["own_document"])
        self.assertEqual(target["applicability"], SCOPE)
        self.assertIsNone(target["version_id"])
        self.assertEqual(self.registry.discovery_targets(self.item), [target])
        self.assertEqual(self.registry.pending_candidates(self.item), [target])
        self.assertEqual(self.registry.candidates(self.item), [])
        self.assertEqual(self.fetcher.calls, 0)
        self.assertEqual(self.target(locator="Synthetic section A")["target_id"], target["target_id"])

    def test_only_manager_can_register_complete_or_fetch_and_roles_are_explicit(self):
        target = self.target()
        for actor in ("buyer", "admin", "outsider"):
            with self.subTest(actor=actor):
                with self.assertRaises(ServiceError):
                    self.registry.register_target(actor, self.item, self.document["document_id"], role="SERVICE_TERMS", applicability=SCOPE, applicability_basis="Synthetic")
                with self.assertRaises(ServiceError):
                    self.registry.fetch_target(actor, target["target_id"])
                with self.assertRaises(ServiceError):
                    self.registry.complete_target(actor, target["target_id"], "model-invented-version")
        with self.assertRaises(ServiceError):
            self.target(role="MODEL_PROVIDED_ROLE")
        self.assertEqual(self.fetcher.calls, 0)

    def test_complete_preserves_registered_scope_and_refuses_wrong_document(self):
        target = self.target()
        wrong_scope = self.registry.ingest_bytes("manager", self.document["document_id"], BODY, applicability={**SCOPE, "country": "OTHER"})
        with self.assertRaises(ServiceError) as caught:
            self.registry.complete_target("manager", target["target_id"], wrong_scope["version_id"])
        self.assertEqual(caught.exception.code, "DOCUMENT_TARGET_MISMATCH")
        other = self.registry.register_document("manager", source_id="SYNTHETIC-SOURCE", title="Other synthetic",
            canonical_url="https://example.org/other", document_type="HTML", is_synthetic=True)
        wrong_document = self.registry.ingest_bytes("manager", other["document_id"], BODY, applicability=SCOPE)
        with self.assertRaises(ServiceError):
            self.registry.complete_target("manager", target["target_id"], wrong_document["version_id"])
        self.assertEqual(self.registry.candidates(self.item), [])
        with self.db.transaction() as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE document_targets SET role='TRANSFER_GUIDE' WHERE target_id=?", (target["target_id"],))

    def test_shared_document_reuses_body_across_questions_and_items(self):
        first, second = self.target(), self.target(self.other)
        fetched = self.registry.fetch_target("manager", first["target_id"], cache_key="question one")
        cached = self.registry.fetch_target("manager", first["target_id"], cache_key="question two")
        shared = self.registry.fetch_target("manager", second["target_id"], cache_key="another item")
        self.assertFalse(fetched["cache_hit"])
        self.assertTrue(cached["cache_hit"] and shared["cache_hit"])
        self.assertEqual({fetched["version_id"], cached["version_id"], shared["version_id"]}, {fetched["version_id"]})
        self.assertEqual(self.fetcher.calls, 1)
        self.assertEqual(len(self.registry.candidates()), 2)
        with self.db.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM document_versions").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM document_target_versions").fetchone()[0], 2)

    def test_concurrent_fetch_lease_blocks_duplicate_without_holding_sql_writer(self):
        target = self.target()
        started, release = threading.Event(), threading.Event()
        def fetch(source_id, url, actor=None):
            self.fetcher.calls += 1
            started.set()
            if not release.wait(5):
                raise TimeoutError()
            return FetchResult(BODY, "text/html", url, 200)
        self.fetcher.fetch = fetch
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.registry.fetch_target, "manager", target["target_id"])
            self.assertTrue(started.wait(5))
            try:
                with self.db.transaction() as connection:
                    connection.execute("UPDATE demo_wallets SET available=available WHERE user_id='buyer'")
                with self.assertRaises(ServiceError) as caught:
                    self.registry.fetch_target("manager", target["target_id"])
                self.assertEqual(caught.exception.code, "DOCUMENT_FETCH_IN_PROGRESS")
                self.assertTrue(caught.exception.retryable)
            finally:
                release.set()
            self.assertFalse(future.result(timeout=5)["cache_hit"])
        self.assertTrue(self.registry.fetch_target("manager", target["target_id"])["cache_hit"])
        self.assertEqual(self.fetcher.calls, 1)

    def test_failed_fetch_does_not_become_a_success_cache(self):
        target = self.target()
        self.fetcher.failure = OSError("SYNTHETIC failure")
        with self.assertRaises(ServiceError) as caught:
            self.registry.fetch_target("manager", target["target_id"])
        self.assertEqual(caught.exception.code, "FETCH_FAILED")
        with self.db.connect() as connection:
            self.assertEqual(connection.execute("SELECT status FROM document_fetch_leases").fetchone()[0], "FAILED")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM document_versions").fetchone()[0], 0)
        self.fetcher.failure = None
        self.assertFalse(self.registry.fetch_target("manager", target["target_id"])["cache_hit"])
        self.assertEqual(self.fetcher.calls, 2)

    def test_expired_claim_cannot_activate_a_stale_response(self):
        target = self.target()
        started, release = threading.Event(), threading.Event()
        lock = threading.Lock()
        def fetch(source_id, url, actor=None):
            with lock:
                self.fetcher.calls += 1
                call = self.fetcher.calls
            if call == 1:
                started.set()
                if not release.wait(5):
                    raise TimeoutError()
            return FetchResult(BODY if call == 1 else OTHER_BODY, "text/html", url, 200)
        self.fetcher.fetch = fetch
        with ThreadPoolExecutor(max_workers=1) as pool:
            old = pool.submit(self.registry.fetch_target, "manager", target["target_id"])
            self.assertTrue(started.wait(5))
            try:
                with self.db.transaction() as connection:
                    connection.execute("UPDATE document_fetch_leases SET lease_until=0")
                newer = self.registry.fetch_target("manager", target["target_id"])
            finally:
                release.set()
            with self.assertRaises(ServiceError) as caught:
                old.result(timeout=5)
            self.assertEqual(caught.exception.code, "DOCUMENT_FETCH_LEASE_LOST")
        self.assertEqual(self.registry.registered_candidates(self.item)[0]["version_id"], newer["version_id"])
        with self.db.connect() as connection:
            self.assertEqual(connection.execute("SELECT version_id FROM document_fetch_leases").fetchone()[0], newer["version_id"])
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM document_target_versions").fetchone()[0], 1)

    def test_explicit_refresh_links_new_immutable_version_and_keeps_history(self):
        target = self.target()
        original = self.registry.fetch_target("manager", target["target_id"])
        self.fetcher.body = OTHER_BODY
        refreshed = self.registry.fetch_target("manager", target["target_id"], refresh=True)
        self.assertNotEqual(original["version_id"], refreshed["version_id"])
        self.assertFalse(refreshed["cache_hit"])
        self.assertEqual(self.fetcher.calls, 2)
        self.assertEqual([row["version_id"] for row in self.registry.candidates(self.item)], [refreshed["version_id"]])
        with self.db.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM document_target_versions").fetchone()[0], 2)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.transaction() as connection:
                connection.execute("UPDATE document_versions SET body_hash='forged' WHERE version_id=?", (original["version_id"],))

    def test_revoked_action_blocks_cached_body_reuse(self):
        target = self.target()
        self.registry.fetch_target("manager", target["target_id"])
        self.policy.set_review("manager", {**next(review for review in self.reviews if review["action"] == "fetch"), "status": "BLOCKED"})
        with self.assertRaises(ServiceError) as caught:
            self.registry.fetch_target("manager", target["target_id"])
        self.assertEqual(caught.exception.code, "SOURCE_BLOCKED")
        self.assertEqual(self.fetcher.calls, 1)

    def test_corrupted_cached_body_is_not_reported_as_success_and_refresh_repairs_it(self):
        target = self.target()
        first = self.registry.fetch_target("manager", target["target_id"])
        with self.db.connect() as connection:
            path = Path(connection.execute("SELECT body_path FROM document_versions WHERE version_id=?", (first["version_id"],)).fetchone()[0])
        path.write_text("[]", encoding="utf-8")
        with self.assertRaises(ServiceError) as caught:
            self.registry.fetch_target("manager", target["target_id"])
        self.assertEqual(caught.exception.code, "PARSE_FAILED")
        repaired = self.registry.fetch_target("manager", target["target_id"], refresh=True)
        self.assertEqual(repaired["version_id"], first["version_id"])
        self.assertTrue(self.registry.fetch_target("manager", target["target_id"])["cache_hit"])
        self.assertEqual(self.fetcher.calls, 2)

    def test_revoked_target_is_not_silently_reactivated_by_re_registration(self):
        target = self.target()
        with self.db.transaction() as connection:
            connection.execute("UPDATE document_targets SET status='REVOKED' WHERE target_id=?", (target["target_id"],))
        with self.assertRaises(ServiceError) as caught:
            self.target()
        self.assertEqual(caught.exception.code, "SOURCE_BLOCKED")
        self.assertEqual(self.registry.registered_candidates(), [])

    def test_concurrent_same_body_cannot_silently_claim_different_scope(self):
        barrier = threading.Barrier(2)
        def ingest(country):
            barrier.wait(timeout=5)
            try:
                return self.registry.ingest_bytes("manager", self.document["document_id"], BODY, applicability={**SCOPE, "country": country})
            except ServiceError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(ingest, ("SYNTHETIC-A", "SYNTHETIC-B")))
        self.assertEqual(sum(isinstance(row, dict) for row in results), 1)
        self.assertIn("DOCUMENT_VERSION_CONFLICT", results)

    def test_job_request_identity_and_claim_columns_are_constrained(self):
        self.target()
        with self.db.transaction() as connection:
            connection.execute("INSERT INTO index_jobs(job_id,requester,item_id,status) VALUES('synthetic-job','buyer',?,'QUEUED')", (self.item,))
            connection.execute("INSERT INTO document_job_runs(job_id,request_json,request_hash) VALUES('synthetic-job','{}','synthetic-hash')")
        for sql in ("UPDATE index_jobs SET requester='admin' WHERE job_id='synthetic-job'",
                    "UPDATE document_job_runs SET request_json='[]' WHERE job_id='synthetic-job'",
                    "UPDATE document_job_runs SET claim_token='partial' WHERE job_id='synthetic-job'"):
            with self.subTest(sql=sql):
                with self.assertRaises(sqlite3.IntegrityError):
                    with self.db.transaction() as connection:
                        connection.execute(sql)

    def test_metadata_version_review_audit_preserves_hash_without_document_fk(self):
        metadata_hash = "SYNTHETIC-DATASET-HASH"
        review = {**next(row for row in self.reviews if row["action"] == "external_process"), "version_id": metadata_hash}
        self.policy.set_review("manager", review)
        self.registry._require("SYNTHETIC-SOURCE", "external_process", "manager", metadata_hash)
        with self.db.connect() as connection:
            saved = connection.execute("SELECT document_version_id,reviewed_version_id FROM source_action_reviews WHERE reviewed_version_id=?", (metadata_hash,)).fetchone()
            self.assertIsNotNone(saved)
            self.assertIsNone(saved["document_version_id"])
            self.assertEqual(saved["reviewed_version_id"], metadata_hash)
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_exact_version_denial_prevents_body_and_version_persistence(self):
        body_hash = digest(parse_document(BODY, "HTML"))
        version_id = "ver-" + digest([self.document["document_id"], body_hash])[:32]
        for blocked_action in ("store_body", "extract"):
            with self.subTest(action=blocked_action):
                for action in ("store_body", "extract"):
                    review = next(row for row in self.reviews if row["action"] == action)
                    self.policy.set_review("manager", {**review, "version_id": version_id,
                        "status": "BLOCKED" if action == blocked_action else "ALLOWED"})
                with self.assertRaises(ServiceError) as caught:
                    self.registry.ingest_bytes("manager", self.document["document_id"], BODY, applicability=SCOPE)
                self.assertEqual(caught.exception.code, "SOURCE_BLOCKED")
                with self.db.connect() as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM document_versions").fetchone()[0], 0)
                self.assertEqual(list(self.registry.body_dir.iterdir()), [])


class CollectionMigrationTests(unittest.TestCase):
    def test_existing_schema_upgrade_reentry_and_failed_migration_rollback(self):
        with tempfile.TemporaryDirectory(prefix="warranty-migration-upgrade-") as directory:
            path = Path(directory) / "private.sqlite3"
            migrations = Path(database_module.__file__).with_name("migrations")
            connection = sqlite3.connect(path)
            try:
                connection.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,name TEXT NOT NULL,sha256 TEXT NOT NULL,applied_at TEXT NOT NULL DEFAULT 'SYNTHETIC')")
                for migration in sorted(migrations.glob("00[1-5]_*.sql")):
                    raw = migration.read_bytes()
                    connection.executescript(raw.decode("utf-8"))
                    connection.execute("INSERT INTO schema_migrations(version,name,sha256) VALUES(?,?,?)", (int(migration.name[:3]), migration.name, hashlib.sha256(raw).hexdigest()))
                    connection.commit()
                connection.execute("UPDATE demo_wallets SET available=12345 WHERE user_id='buyer'")
                connection.execute("INSERT INTO items(item_id) VALUES('SYNTHETIC-LEGACY')")
                connection.execute("INSERT INTO trades(trade_id,buyer_id,seller_id,item_id) VALUES('legacy-trade','buyer','seller','SYNTHETIC-LEGACY')")
                connection.execute("INSERT INTO contract_revisions(trade_id,revision,hash,item_id,D,W,terms_json) VALUES('legacy-trade',1,'synthetic-hash','SYNTHETIC-LEGACY',100,0,'{}')")
                connection.execute("INSERT INTO ledger_groups(group_id,trade_id,event_type) VALUES('legacy-ledger','legacy-trade','DEPOSIT')")
                connection.executemany("INSERT INTO ledger_entries(group_id,line_no,account,debit,credit) VALUES('legacy-ledger',?,?,?,?)", [(1, "escrow:legacy-trade:D:held", 100, 0), (2, "wallet:buyer", 0, 100)])
                connection.execute("UPDATE ledger_groups SET posted=1 WHERE group_id='legacy-ledger'")
                connection.execute("PRAGMA user_version=5")
                connection.commit()
            finally:
                connection.close()
            db = Database(path)
            original_read = Path.read_bytes
            def corrupted_migration(file):
                raw = original_read(file)
                return raw + b"\nTHIS IS A SYNTHETIC SQL FAILURE;\n" if file.name.startswith("006_") else raw
            try:
                with patch.object(Path, "read_bytes", corrupted_migration):
                    with self.assertRaises(sqlite3.OperationalError):
                        db.initialize()
                with db.connect() as connection:
                    self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 5)
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0], 5)
                    self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='document_targets'").fetchone())
                    self.assertEqual(connection.execute("SELECT available FROM demo_wallets WHERE user_id='buyer'").fetchone()[0], 12345)
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM ledger_entries WHERE group_id='legacy-ledger'").fetchone()[0], 2)
                db.initialize()
                db.initialize()
                with db.connect() as connection:
                    self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 6)
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0], 6)
                    self.assertEqual(connection.execute("SELECT available FROM demo_wallets WHERE user_id='buyer'").fetchone()[0], 12345)
                    self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
                    self.assertEqual(connection.execute("SELECT posted FROM ledger_groups WHERE group_id='legacy-ledger'").fetchone()[0], 1)
                with self.assertRaises(sqlite3.IntegrityError):
                    with db.transaction() as connection:
                        connection.execute("DELETE FROM ledger_entries WHERE group_id='legacy-ledger'")
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
