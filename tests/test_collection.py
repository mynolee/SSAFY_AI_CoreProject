"""Offline collection checks with authored synthetic targets and counted fetches."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from warranty.catalog import Catalog
from warranty.collection import Collector, RegisteredDocumentSearch
from warranty.config import Settings
from warranty.db import Database
from warranty.documents import ACTIONS, DocumentRegistry
from warranty.errors import ServiceError
from warranty.security import FetchResult, SourcePolicy


SCOPE = {"country": "SYNTHETIC", "plan_generation": "SYNTHETIC-PLAN", "payment_method": "SYNTHETIC-MONTHLY",
         "channel": "SYNTHETIC-CHANNEL", "document_type": "SERVICE_TERMS", "date_start": "2020-01-01", "date_end": "2030-12-31"}
INPUTS = {key: value for key, value in SCOPE.items() if not key.startswith("date_")}
INPUTS["enrollment_date"] = "2025-01-01"
BODY = b"<html><h1>SYNTHETIC collection fixture</h1><p>Transfer requires provider confirmation.</p></html>"


class CountedFetch:
    """Trusted test transport: no network implementation and no external calls."""
    def __init__(self):
        self.calls, self.failures, self.delay = [], 0, 0
        self.entered, self.release = threading.Event(), None
        self._lock = threading.Lock()

    def fetch(self, source_id, url, actor=None, version_id=None):
        with self._lock:
            self.calls.append((source_id, url, actor))
            fail = self.failures > 0
            self.failures = max(0, self.failures - 1)
        self.entered.set()
        if self.release is not None and not self.release.wait(5):
            raise RuntimeError("Synthetic fetch barrier timed out")
        if self.delay:
            time.sleep(self.delay)
        if fail:
            raise ServiceError("FETCH_FAILED", "Synthetic transient failure", 503, True)
        return FetchResult(BODY, "text/html", url, 200)


class CountedSearch(RegisteredDocumentSearch):
    cache_version = "synthetic-counted-search-v1"

    def __init__(self):
        self.calls = []

    def search(self, query, **kwargs):
        self.calls.append((query, kwargs["item_id"], copy.deepcopy(kwargs["applicability"])))
        return super().search(query, **kwargs)


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="warranty-collection-")
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(Path(self.temp.name) / "data", Path(self.temp.name) / "config")
        self.settings.prepare()
        self.catalog = Catalog.synthetic()
        self.item, self.other = [item["item_id"] for item in self.catalog.items]
        self.db = Database(self.settings.data_dir / "app.sqlite3")
        self.db.initialize()
        self.addCleanup(self.db.close)
        self.sources = [{"source_id": f"SYNTHETIC-{index}", "url": f"https://example.org/synthetic-{index}", "allowed_hosts": ["example.org"]}
                        for index in range(3)]
        self.reviews = [{"source_id": source["source_id"], "action": action, "status": "ALLOWED",
                         "basis_type": "OTHER_RECORDED_BASIS", "basis_url": "https://example.org/",
                         "reviewed_on": "2026-01-01", "scope": "Authored synthetic offline fixture",
                         "conditions": "Local unit tests only", "user_scope": "test", "processing_environment": "LOCAL"}
                        for source in self.sources for action in ACTIONS]
        self.policy = SourcePolicy(self.sources, self.reviews, {"manager": {"DATA_MANAGER"}})
        self.fetcher, self.search = CountedFetch(), CountedSearch()
        self.registry = DocumentRegistry(self.catalog, self.db, self.settings, self.policy, self.fetcher)
        self.clock = [1000.0]
        self.collector = Collector(self.registry, self.search, "manager", clock=lambda: self.clock[0])

    def target(self, index=0, item=None, role="SERVICE_TERMS", title=None):
        source = self.sources[index]
        document = self.registry.register_document("manager", source_id=source["source_id"], title=title or f"SYNTHETIC terms {index}",
                    canonical_url=source["url"], document_type="HTML", is_synthetic=True)
        target = self.registry.register_target("manager", item or self.item, document["document_id"], role=role,
                    applicability=SCOPE, applicability_basis="This authored synthetic fixture explicitly covers this item.")
        return target.get("target_id", target.get("id")) if isinstance(target, dict) else target

    def discover(self, query="transfer", question="Synthetic question A", item=None, **kwargs):
        return self.collector.discover(item or self.item, question, [query], INPUTS, "buyer", **kwargs)

    def collect(self, query="transfer", question="Synthetic question A", item=None, **kwargs):
        return self.collector.collect(item or self.item, question, [query], INPUTS, "buyer", **kwargs)

    def change_review(self, source_id, action, status):
        review = next(row for row in self.reviews if row["source_id"] == source_id and row["action"] == action)
        self.policy.set_review("manager", {**review, "status": status})

    def test_pending_own_document_survives_unrelated_query_before_any_fetch(self):
        target_id = self.target(title="SYNTHETIC own terms")
        candidates = self.discover("unrelated generated query")
        self.assertEqual([candidate["id"] for candidate in candidates], [target_id])
        self.assertTrue(candidates[0]["own_document"])
        self.assertIsNone(candidates[0]["version_id"])
        self.assertEqual(self.fetcher.calls, [])

    def test_each_generated_query_is_executed_and_selection_precedes_fetch(self):
        own = self.target()
        optional = self.target(1, role="TRANSFER_GUIDE", title="SYNTHETIC battery guidance")
        candidates = self.collector.discover(self.item, "question is not a search term", ["battery", "guidance"], INPUTS, "buyer")
        self.assertEqual([row[0] for row in self.search.calls], ["battery", "guidance"])
        self.assertEqual({row["id"] for row in candidates}, {own, optional})
        self.assertEqual(self.fetcher.calls, [])
        result = self.collector.collect(self.item, "question is not a search term", ["battery", "guidance"], INPUTS, "buyer",
                    candidates=candidates, selected_candidate_ids=[])
        self.assertEqual(result["candidate_ids"], [own])
        self.assertEqual(len(self.fetcher.calls), 1)
        self.assertIsNone(next(row for row in self.registry.registered_candidates(self.item) if row["target_id"] == optional)["version_id"])

    def test_selected_optional_and_mandatory_own_preserve_original_link_ids(self):
        own = self.target()
        optional = self.target(1, role="TRANSFER_GUIDE", title="SYNTHETIC battery guide")
        candidates = self.discover("battery")
        result = self.collect(query="battery", candidates=candidates, selected_candidate_ids=[optional])
        self.assertEqual(result["status"], "FETCHED")
        self.assertEqual(set(result["candidate_ids"]), {own, optional})
        self.assertEqual({row["candidate_id"] for row in result["linked"]}, {own, optional})
        self.assertEqual({row["item_id"] for row in result["linked"]}, {self.item})
        self.assertEqual(len(self.fetcher.calls), 2)

    def test_different_questions_same_generated_queries_do_not_refetch(self):
        self.target()
        first = self.collect(question="First question")
        second = self.collect(question="A different question")
        self.assertEqual(first["status"], "FETCHED")
        self.assertEqual(second["status"], "FETCHED")
        self.assertEqual(second["cache_hits"], 1)
        self.assertEqual(len(self.fetcher.calls), 1)
        self.assertEqual(len(self.search.calls), 1)

    def test_source_rights_are_rechecked_before_reusing_success_cache(self):
        self.target()
        self.assertEqual(self.collect()["status"], "FETCHED")
        self.change_review("SYNTHETIC-0", "extract", "BLOCKED")
        blocked = self.collect()
        self.assertEqual(blocked["status"], "BLOCKED")
        self.assertEqual(blocked["version_ids"], [])
        self.assertEqual(blocked["errors"][0]["code"], "SOURCE_BLOCKED")
        self.assertEqual(len(self.fetcher.calls), 1)

    def test_source_action_denied_before_any_body_request(self):
        self.target()
        self.change_review("SYNTHETIC-0", "store_body", "BLOCKED")
        result = self.collect()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(self.fetcher.calls, [])
        with closing(self.db.connect()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM rag_cache WHERE cache_kind='collection_fetch' AND status='FETCHED'").fetchone()[0], 0)

    def test_foreign_or_forged_candidates_never_fetch(self):
        self.target()
        candidates = self.discover()
        forged = [{**candidates[0], "url": "https://unregistered.example/private", "role": "TRANSFER_GUIDE"}]
        for options in ({"candidates": forged, "selected_candidate_ids": [candidates[0]["id"]]},
                        {"candidates": candidates, "selected_candidate_ids": ["foreign-candidate"]}):
            with self.subTest(options=options), self.assertRaises(ServiceError):
                self.collect(**options)
        self.assertEqual(self.fetcher.calls, [])

    def test_injected_search_cannot_create_unregistered_url_authority(self):
        self.target()
        class ForeignSearch(RegisteredDocumentSearch):
            def search(self, query, **kwargs):
                return [{"id": "foreign-id", "url": "https://example.org/not-registered", "role": "SERVICE_TERMS"}]
        collector = Collector(self.registry, ForeignSearch(), "manager")
        with self.assertRaises(ServiceError):
            collector.discover(self.item, "question", ["query"], INPUTS, "buyer")
        self.assertEqual(self.fetcher.calls, [])

    def test_remote_search_adapter_is_disabled_before_invocation(self):
        class RemoteSearch:
            is_remote = True
            def search(self, *args, **kwargs):
                raise AssertionError("Unapproved remote search must never execute")
        collector = Collector(self.registry, RemoteSearch(), "manager")
        with self.assertRaises(ServiceError) as raised:
            collector.discover(self.item, "question", ["query"], INPUTS, "buyer")
        self.assertEqual(raised.exception.code, "SOURCE_BLOCKED")

    def test_failed_request_is_not_success_and_retries_after_failure_ttl(self):
        self.target()
        self.fetcher.failures = 1
        first = self.collect()
        second = self.collect()
        self.assertEqual(first["status"], "FAILED")
        self.assertEqual(second["status"], "FAILED")
        self.assertEqual(second["cache_hits"], 0)
        self.assertEqual(len(self.fetcher.calls), 1)
        self.clock[0] += 6
        self.assertEqual(self.collect()["status"], "FETCHED")
        self.assertEqual(len(self.fetcher.calls), 2)

    def test_explicit_refresh_retries_failed_and_refreshes_fetched_body(self):
        self.target()
        self.fetcher.failures = 1
        self.assertEqual(self.collect()["status"], "FAILED")
        self.assertEqual(self.collect(refresh=True)["status"], "FETCHED")
        self.assertEqual(self.collect(refresh=True)["status"], "FETCHED")
        self.assertEqual(len(self.fetcher.calls), 3)

    def test_no_results_has_separate_ttl_and_creates_no_document(self):
        self.assertEqual(self.collect()["status"], "NO_RESULTS")
        again = self.collect(question="Different question")
        self.assertEqual(again["status"], "NO_RESULTS")
        self.assertEqual(again["cache_hits"], 1)
        self.assertEqual(len(self.search.calls), 1)
        self.clock[0] += 61
        self.assertEqual(self.collect()["status"], "NO_RESULTS")
        self.assertEqual(len(self.search.calls), 2)
        self.assertEqual(self.fetcher.calls, [])
        self.assertEqual(self.registry.candidates(), [])

    def test_index_failure_leaves_fetched_body_reusable_without_refetch(self):
        self.target()
        first = self.collect()
        with self.assertRaises(ServiceError):
            self.collector.mark_indexed(first, "missing-index-revision")
        second = self.collect()
        self.assertEqual(second["status"], "FETCHED")
        self.assertEqual(len(self.fetcher.calls), 1)

    def test_only_active_manifest_can_mark_actual_collected_versions_indexed(self):
        self.target()
        result = self.collect()
        with self.db.transaction() as connection:
            connection.execute("INSERT INTO index_manifests(revision,version_ids_json,active) VALUES(?,?,1)", ("synthetic-index", json.dumps(result["version_ids"])))
        self.assertEqual(self.collector.mark_indexed(result, "synthetic-index"), 1)
        self.assertEqual(self.collect()["status"], "INDEXED")
        self.assertEqual(len(self.fetcher.calls), 1)
        with self.db.transaction() as connection:
            connection.execute("UPDATE index_manifests SET active=0 WHERE revision='synthetic-index'")
        self.assertEqual(self.collect()["status"], "FETCHED")
        self.assertEqual(len(self.fetcher.calls), 1)

    def test_index_promotion_acquires_policy_before_database_writer(self):
        self.target()
        result = self.collect()
        with self.db.transaction() as connection:
            connection.execute("INSERT INTO index_manifests(revision,version_ids_json,active) VALUES(?,?,1)", ("synthetic-lock-index", json.dumps(result["version_ids"])))
        original_guard = self.policy.read_guard
        requested = threading.Event()

        @contextmanager
        def observed_guard():
            requested.set()
            with original_guard():
                yield

        with patch.object(self.policy, "read_guard", observed_guard), ThreadPoolExecutor(max_workers=1) as executor:
            with original_guard():
                pending = executor.submit(self.collector.mark_indexed, result, "synthetic-lock-index")
                self.assertTrue(requested.wait(2))
                # The promotion waits for policy without holding SQLite's writer.
                with self.db.transaction() as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM index_manifests WHERE active=1").fetchone()[0], 1)
            self.assertEqual(pending.result(timeout=3), 1)
    def test_same_body_can_link_two_items_without_duplicate_fetch(self):
        first_target = self.target()
        other_target = self.target(item=self.other)
        first, second = self.collect(), self.collect(item=self.other)
        self.assertNotEqual(first_target, other_target)
        self.assertEqual(first["version_ids"], second["version_ids"])
        self.assertEqual(second["linked"][0]["item_id"], self.other)
        self.assertEqual(len(self.fetcher.calls), 1)

    def test_twenty_concurrent_calls_fetch_one_body(self):
        self.target()
        self.fetcher.delay = 0.01
        with ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(lambda _: self.collect(), range(20)))
        self.assertEqual({result["status"] for result in results}, {"FETCHED"})
        self.assertEqual(len(self.fetcher.calls), 1)
        self.assertEqual(sum(result["cache_hits"] for result in results), 19)

    def test_two_collectors_use_registry_lease_instead_of_duplicate_fetch(self):
        self.target()
        self.fetcher.release = threading.Event()
        other = Collector(self.registry, self.search, "manager", clock=lambda: self.clock[0])
        with ThreadPoolExecutor(max_workers=2) as executor:
            pending = executor.submit(self.collect)
            self.assertTrue(self.fetcher.entered.wait(3))
            try:
                result = other.collect(self.item, "other question", ["transfer"], INPUTS, "buyer")
                self.assertEqual(result["status"], "FAILED")
                self.assertEqual(result["errors"][0]["code"], "DOCUMENT_FETCH_IN_PROGRESS")
            finally:
                self.fetcher.release.set()
            self.assertEqual(pending.result(timeout=5)["status"], "FETCHED")
        self.assertEqual(len(self.fetcher.calls), 1)

    def test_unconfigured_manager_cannot_fetch_pending_target(self):
        self.target()
        collector = Collector(self.registry, manager_actor=None)
        result = collector.collect(self.item, "question", ["transfer"], INPUTS, "buyer")
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(self.fetcher.calls, [])

    def test_real_rag_generates_queries_selects_before_fetch_and_reuses_body(self):
        from warranty.prompts import MockStructuredModel
        from warranty.rag import RagService

        class SyntheticPrompts:
            def render(self, kind, variables):
                return json.dumps({"policy": {"kind": kind, "fixture": "SYNTHETIC"}, "input": variables})
            def load(self, kind):
                return {"kind": kind, "fixture": "SYNTHETIC"}

        fetcher = self.fetcher
        selections = []
        class RejectingSelectionModel(MockStructuredModel):
            def generate(self, kind, prompt):
                if kind == "query":
                    return {"queries": ["battery"], "filters": {}, "required_document_roles": ["own_document"], "missing_information": []}
                if kind == "selection":
                    selections.append(len(fetcher.calls))
                    candidates = json.loads(prompt)["input"]["candidates"]
                    return {"selected_candidate_ids": [], "rejected": [{"id": row["id"], "reason": "SYNTHETIC rejection"} for row in candidates], "conflicts": [], "missing_information": []}
                return super().generate(kind, prompt)

        own = self.target(title="SYNTHETIC own service terms")
        optional = self.target(1, role="TRANSFER_GUIDE", title="SYNTHETIC battery supplement")
        rag = RagService(self.catalog, self.db, self.settings, model=RejectingSelectionModel(), prompt_store=SyntheticPrompts(),
                         source_policy=self.policy, fetcher=self.fetcher, index_actor="manager")
        self.addCleanup(rag.close)
        rag.collector = Collector(rag.registry, self.search, "manager", clock=lambda: self.clock[0])
        first = rag.ask("buyer", self.item, "First synthetic question", INPUTS)
        second = rag.ask("buyer", self.item, "Different synthetic question", INPUTS)
        self.assertEqual(first["readiness"]["status"], "INDEX_READY")
        self.assertEqual(second["readiness"]["status"], "INDEX_READY")
        self.assertEqual(selections[0], 0)
        self.assertEqual(len(self.fetcher.calls), 1)
        self.assertEqual(self.search.calls[0][0], "battery")
        targets = {row["target_id"]: row for row in self.registry.registered_candidates(self.item)}
        self.assertIsNotNone(targets[own]["version_id"])
        self.assertIsNone(targets[optional]["version_id"])
        self.assertEqual(second["collection_cache_hits"], 1)


if __name__ == "__main__":
    unittest.main()
