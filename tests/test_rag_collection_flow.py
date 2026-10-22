"""Real local orchestration with authored fixtures and zero network requests."""
from contextlib import closing, contextmanager
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_rag as fixtures
from warranty.collection import Collector
from warranty.config import Settings
from warranty.documents import ACTIONS
from warranty.errors import ServiceError
from warranty.rag import RagService
from warranty.vectorstore import MockEmbeddings


class RecordingSearch:
    is_remote = False
    cache_version = "MOCK-registered-search-fixture-v1"

    def __init__(self, events):
        self.events, self.queries = events, []

    def search(self, query, **request):
        self.events.append("search")
        self.queries.append(query)
        return [row["id"] for row in request["candidates"]]


class RecordingFetcher:
    """A byte fixture transport; never opens a socket or resolves a hostname."""
    def __init__(self, events):
        self.events, self.calls, self.fail = events, [], False

    def fetch(self, source_id, url, **kwargs):
        self.events.append("fetch")
        self.calls.append((source_id, url, kwargs.get("actor")))
        if self.fail:
            raise ServiceError("FETCH_FAILED", "SYNTHETIC_INTERNAL_SECRET_MUST_NOT_ESCAPE", 503, True)
        return SimpleNamespace(body=fixtures.BODY, content_type="text/html", final_url=url, status=200)


class CollectionFlowTests(unittest.TestCase):
    setUp = fixtures.RagTests.setUp
    make_service = fixtures.RagTests.make_service
    document = fixtures.RagTests.document

    def configure(self):
        self.events = []
        self.fetcher = RecordingFetcher(self.events)
        self.search = RecordingSearch(self.events)
        self.rag.registry.fetcher = self.fetcher
        self.rag.collector = Collector(self.rag.registry, self.search, manager_actor="manager")

    def target(self, *, optional=False):
        source_id, url = "SYNTHETIC-DOC", "https://example.org/synthetic-terms"
        if optional:
            source_id, url = "SYNTHETIC-GUIDE", "https://example.org/synthetic-guide"
            self.policy.register_source("manager", {"source_id": source_id, "url": url})
            for action in ACTIONS:
                self.policy.set_review("manager", {**self.policy.require("SYNTHETIC-DOC", action), "source_id": source_id})
        document = self.rag.registry.register_document("manager", source_id=source_id, title="SYNTHETIC " + source_id,
            canonical_url=url, document_type="HTML", is_synthetic=True)
        return self.rag.registry.register_target("manager", self.item, document["document_id"],
            role="TRANSFER_GUIDE" if optional else "SERVICE_TERMS", applicability=fixtures.SCOPE,
            applicability_basis="Authored synthetic target explicitly covers this item.")

    def test_generated_query_discovers_unfetched_own_target_then_selects_before_fetch(self):
        self.configure()
        own, optional = self.target(), self.target(optional=True)
        self.assertEqual(self.rag.registry.candidates(self.item), [])
        original = self.model.generate
        def reject_all(kind, prompt):
            self.events.append(kind)
            if kind == "selection":
                candidates = json.loads(prompt)["input"]["candidates"]
                self.assertEqual({row["id"] for row in candidates}, {own["id"], optional["id"]})
                return {"selected_candidate_ids": [], "rejected": [{"id": row["id"], "reason": "MOCK relevance rejection"} for row in candidates],
                        "conflicts": [], "missing_information": []}
            return original(kind, prompt)
        with patch.object(self.model, "generate", side_effect=reject_all):
            result = self.rag.ask("buyer", self.item, "Which transfer conditions apply?", fixtures.INPUTS)
        self.assertEqual(self.search.queries, result["queries"])
        self.assertLess(self.events.index("query"), self.events.index("search"))
        self.assertLess(self.events.index("selection"), self.events.index("fetch"))
        self.assertEqual(len(self.fetcher.calls), 1)
        self.assertEqual(self.fetcher.calls[0][0], "SYNTHETIC-DOC")
        self.assertEqual(self.fetcher.calls[0][2], "manager")
        self.assertEqual(result["readiness"]["status"], "INDEX_READY")
        self.assertEqual(result["classification"], "CONDITIONAL")
        self.assertFalse(result["conditions_ok"])
        self.assertTrue(result["evidence"])
        self.assertEqual(set(self.rag.graph.get_graph().nodes) - {"__start__", "__end__"}, {"retrieve", "generate"})

    def test_distinct_questions_same_generated_query_reuse_fetch_and_verified_index(self):
        self.configure()
        self.target()
        first = self.rag.ask("buyer", self.item, "First question about transfer", fixtures.INPUTS)
        second = self.rag.ask("buyer", self.item, "Different question about documents", fixtures.INPUTS)
        self.assertEqual(first["queries"], second["queries"])
        self.assertEqual(len(self.fetcher.calls), 1)
        self.assertEqual(len(self.search.queries), 1)
        self.assertGreaterEqual(second["collection_cache_hits"], 1)
        self.assertEqual(first["revision"], second["revision"])
        with closing(self.db.connect()) as connection:
            states = {row[0] for row in connection.execute("SELECT status FROM rag_cache WHERE cache_kind='collection_fetch'")}
        self.assertEqual(states, {"INDEXED"})

    def test_index_failure_keeps_fetched_body_for_retry_without_second_fetch(self):
        self.configure()
        self.target()
        with patch.object(self.rag.vectors, "stage_documents", side_effect=ServiceError("INDEX_EMPTY", "SYNTHETIC stage failure", 503, True)):
            failed = self.rag.ask("buyer", self.item, "First index attempt", fixtures.INPUTS)
        self.assertEqual(failed["classification"], "NEEDS_INFORMATION")
        self.assertEqual(len(self.fetcher.calls), 1)
        with closing(self.db.connect()) as connection:
            states = {row[0] for row in connection.execute("SELECT status FROM rag_cache WHERE cache_kind='collection_fetch'")}
        self.assertEqual(states, {"FETCHED"})
        recovered = self.rag.ask("buyer", self.item, "Retry index only", fixtures.INPUTS)
        self.assertEqual(recovered["readiness"]["status"], "INDEX_READY")
        self.assertEqual(len(self.fetcher.calls), 1)

    def test_failed_own_fetch_is_not_replaced_by_supplement_and_does_not_leak_error(self):
        self.configure()
        self.target()
        guide = self.target(optional=True)
        version = self.rag.registry.ingest_bytes("manager", guide["document_id"], fixtures.BODY, applicability=fixtures.SCOPE)
        self.rag.registry.complete_target("manager", guide["target_id"], version["version_id"])
        self.fetcher.fail = True
        result = self.rag.ask("buyer", self.item, "An own document must be present", fixtures.INPUTS)
        self.assertEqual(result["classification"], "NEEDS_INFORMATION")
        self.assertEqual(result["readiness"]["status"], "DOC_BLOCKED")
        self.assertNotIn("SYNTHETIC_INTERNAL_SECRET", json.dumps(result))
        self.assertFalse(result["conditions_ok"])

    def test_collected_link_cannot_cross_item_boundary(self):
        self.configure()
        self.target()
        original = self.rag.collector.collect
        def foreign(*args, **kwargs):
            result = original(*args, **kwargs)
            result["linked"][0]["item_id"] = self.other
            return result
        with patch.object(self.rag.collector, "collect", side_effect=foreign):
            result = self.rag.ask("buyer", self.item, "Reject a foreign collection link", fixtures.INPUTS)
        self.assertEqual(result["classification"], "NEEDS_INFORMATION")
        self.assertEqual(result["errors"][0]["code"], "SOURCE_BLOCKED")
        self.assertEqual(result["evidence"], [])

    def test_frozen_evaluation_does_not_collect_and_ranking_shares_selection(self):
        self.document()
        self.rag.prepare("buyer", self.item)
        self.configure()
        with patch.object(self.rag.collector, "discover", side_effect=AssertionError("Evaluation must not discover")):
            before = self.model.calls
            first = self.rag.evaluation_context("buyer", self.item, "Frozen context", fixtures.INPUTS, cache=False)
            second = self.rag.evaluation_context("buyer", self.item, "Frozen context", fixtures.INPUTS, cache=False)
            self.assertEqual(self.model.calls - before, 4)
            before = self.model.calls
            comparison = self.rag.compare_retrieval("buyer", self.item, "Frozen ranking", fixtures.INPUTS)
            self.assertEqual(self.model.calls - before, 2)
        self.assertEqual(first["revision"], second["revision"])
        self.assertEqual(comparison["retrieval_filters"]["revision"], first["revision"])
        self.assertTrue(comparison["similarity"])
        self.assertTrue(comparison["mmr"])
        self.assertGreaterEqual(comparison["ranking_elapsed_seconds"]["similarity"], 0)
        self.assertGreaterEqual(comparison["ranking_elapsed_seconds"]["mmr"], 0)

    def test_remote_embedding_contexts_cover_explicit_metadata_documents_and_question(self):
        class ContextEmbeddings(MockEmbeddings):
            name = "MOCK-context-checked-embedding-spy"
            is_remote = True
            def __init__(self):
                self.active, self.seen = None, []
            @contextmanager
            def processing_context(self, contexts):
                previous, self.active = self.active, contexts
                try:
                    yield
                finally:
                    self.active = previous
            def embed(self, texts):
                if not self.active:
                    raise AssertionError("Embedding context missing")
                self.seen.append(self.active)
                return super().embed(texts)
        metadata_source = self.catalog.items[0]["source_id"]
        self.policy.register_source("manager", {"source_id": metadata_source, "url": self.catalog.sources[metadata_source]["url"]})
        self.policy.set_review("manager", {**self.policy.require("SYNTHETIC-DOC", "external_process"), "source_id": metadata_source})
        embedding = ContextEmbeddings()
        remote = RagService(self.catalog, self.db, Settings(self.settings.data_dir, self.settings.config_dir, remote_models=True),
            model=self.model, embeddings=embedding, prompt_store=fixtures.SyntheticPromptStore(), source_policy=self.policy, index_actor="manager")
        self.addCleanup(remote.close)
        self.assertEqual(embedding.seen, [])
        remote.prepare_metadata("manager", item_ids=[self.item])
        self.rag = remote
        self.configure()
        self.target()
        answer = remote.ask("buyer", self.item, "Question embedding context", fixtures.INPUTS)
        self.assertEqual(answer["readiness"]["status"], "INDEX_READY")
        contexts = [row for batch in embedding.seen for row in batch]
        self.assertTrue(any(row["kind"] == "metadata" and row["actor"] == "manager" and row["version_id"] == remote.dataset_hash for row in contexts))
        self.assertTrue(any(row["kind"] == "document" and row["actor"] == "manager" and row["version_id"].startswith("ver-") for row in contexts))
        self.assertTrue(any(row["kind"] == "question" and row["actor"] == "buyer" and row["version_id"] == remote.dataset_hash for row in contexts))

    def test_persistent_queue_runs_same_job_through_actual_collection_and_index(self):
        from warranty.jobs import DocumentJobQueue
        self.configure()
        self.target()
        queue = DocumentJobQueue(self.db, self.catalog, self.rag.run_prepare,
            manager_check=lambda actor: actor == "manager", auto_start=False)
        self.rag.document_jobs = queue
        queued = self.rag.enqueue_prepare("buyer", self.item, fixtures.INPUTS, "Prepare own document asynchronously")
        self.assertEqual(queued["status"], "QUEUED")
        self.assertEqual(self.fetcher.calls, [])
        with self.assertRaises(ServiceError):
            self.rag.job("seller", queued["job_id"])
        self.assertEqual(queue.run_pending(limit=1)["processed_jobs"], 1)
        complete = self.rag.job("buyer", queued["job_id"])
        self.assertEqual(complete["status"], "COMPLETED")
        self.assertEqual(complete["job_id"], queued["job_id"])
        self.assertEqual(complete["body_count"], 1)
        self.assertGreater(complete["chunk_count"], 0)
        self.assertEqual(len(self.fetcher.calls), 1)
        with closing(self.db.connect()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM index_jobs").fetchone()[0], 1)

    def test_lost_job_claim_cannot_activate_staged_revision(self):
        self.document()
        class LostClaim:
            def __init__(self):
                self.stages = []
            def __call__(self, stage, **counts):
                self.stages.append(stage)
            def check(self, connection=None):
                raise ServiceError("JOB_CLAIM_LOST", "Synthetic expired lease", 409, True)
        progress = LostClaim()
        outcome = self.rag.run_prepare({"job_id": "SYNTHETIC-queue-job", "requester": "buyer", "item_id": self.item,
            "question": "Lease fencing", "applicability": fixtures.INPUTS, "dataset_hash": self.catalog.dataset_hash}, progress)
        self.assertEqual(outcome["status"], "BLOCKED")
        self.assertEqual(outcome["failure_code"], "JOB_CLAIM_LOST")
        self.assertIsNone(self.rag._active())
        self.assertGreater(self.rag.vectors.docs.count(), 0)  # no stale-worker deletion of shared staging
        context = self.rag.evaluation_context("buyer", self.item, "Unpublished rows must stay hidden", fixtures.INPUTS)
        self.assertTrue(all(row["source_type"] == "metadata" for row in context["evidence"]))
        self.assertEqual(context["readiness"]["status"], "DOC_BLOCKED")


if __name__ == "__main__":
    unittest.main()
