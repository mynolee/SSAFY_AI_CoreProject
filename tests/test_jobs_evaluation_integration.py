"""Cross-check persisted job fencing against private MOCK evaluation snapshots."""
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from warranty.catalog import Catalog
from warranty.db import Database
from warranty.errors import ServiceError
from warranty.evaluation_cli import run_evaluation
from warranty.jobs import DocumentJobQueue, _Progress
from warranty.security import SourcePolicy
from warranty.vectorstore import MockEmbeddings


class SyntheticEvaluationModel:
    is_mock = True
    model_id = "MOCK-job-review-model"

    def __init__(self):
        self.contexts, self.inputs = [], []
        self.on_first_call = None

    def metadata(self):
        return {"provider": "local", "model_id": self.model_id, "model_version": "1",
                "is_mock": True, "is_remote": False, "can_generate": True,
                "temperature": None, "generation_settings": {}}

    @contextmanager
    def processing_context(self, actor, item_id):
        self.contexts.append((actor, item_id))
        yield

    def generate(self, kind, prompt):
        self.inputs.append(json.loads(prompt)["input"])
        if self.on_first_call is not None:
            callback, self.on_first_call = self.on_first_call, None
            callback()
        return {"classification": "NEEDS_INFORMATION", "answer": "MOCK synthetic review response",
                "claims": [], "conditions": [], "missing_information": [], "conflicts": [], "cited_evidence_ids": []}


class SyntheticPrompts:
    def load(self, kind):
        return {"kind": kind, "version": "1", "template": "SYNTHETIC reviewer-only policy"}

    def render(self, kind, variables):
        return json.dumps({"policy": self.load(kind), "input": variables})


class JobsEvaluationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="warranty-jobs-evaluation-review-")
        self.addCleanup(self.temp.cleanup)
        self.base, self.catalog = Path(self.temp.name), Catalog.synthetic()
        self.item = self.catalog.items[0]["item_id"]
        self.db = Database(self.base / "synthetic.sqlite3")
        self.db.initialize()
        self.addCleanup(self.db.close)
        with self.db.transaction() as connection:
            connection.execute("INSERT INTO items(item_id,name,provider) VALUES(?,?,?)", (self.item, "SYNTHETIC item", "SYNTHETIC"))
            connection.execute("INSERT INTO index_manifests(revision,version_ids_json,active) VALUES('SYNTHETIC-initial','[]',1)")
        self.clock = [1000.0]
        self.model = SyntheticEvaluationModel()
        self.embedding = MockEmbeddings()
        outer = self

        class SyntheticEvaluationRag:
            model, prompt_store = outer.model, SyntheticPrompts()
            registry = SimpleNamespace(policy=SourcePolicy())
            vectors = SimpleNamespace(embeddings=outer.embedding)

            def corpus_summary(self, item_ids=None):
                if item_ids is not None:
                    outer.assertEqual(item_ids, [outer.item])
                with outer.db.connect() as connection:
                    revision = connection.execute("SELECT revision FROM index_manifests WHERE active=1").fetchone()[0]
                # Explicit synthetic adapter; no official body or real model is claimed.
                return {"revision": revision, "official_document_body_count": 0, "official_document_chunk_count": 0,
                        "synthetic_document_body_count": 0, "synthetic_document_chunk_count": 0,
                        "metadata_index_matches": True,
                        "active_index": {"revision": revision, "dataset_hash": outer.catalog.dataset_hash,
                                         "embedding_model": outer.embedding.name, "dimension": outer.embedding.dimension}}

            def evaluation_context(self, actor, item_id, question, applicability=None, top_k=6, strategy="similarity", *, allow_collection=False, cache=False):
                outer.assertFalse(allow_collection)
                outer.assertFalse(cache)
                return {"item": outer.catalog.get(item_id), "evidence": [], "revision": self.corpus_summary()["revision"],
                        "readiness": {"status": "DOC_BLOCKED"}, "missing_information": ["SYNTHETIC body absent"], "conflicts": []}

        self.app = SimpleNamespace(catalog=self.catalog, rag=SyntheticEvaluationRag(),
                                   models=SimpleNamespace(chat=self.model, embedding=self.embedding),
                                   settings=SimpleNamespace(remote_models=False))
        self.cases = [{"case_id": f"SYNTHETIC-review-{index}", "item_id": self.item, "question": f"SYNTHETIC review question {index}"} for index in range(5)]
        self.config = {"mode": "MOCK", "user_id": "buyer", "comparisons": ["answers"], "output_dir": str(self.base / "private-results")}

    def queue(self):
        def handler(request, progress):
            progress("INDEXING")
            with self.db.transaction() as connection:
                progress.check(connection)
                connection.execute("UPDATE index_manifests SET active=0 WHERE active=1")
                connection.execute("INSERT INTO index_manifests(revision,version_ids_json,active) VALUES('SYNTHETIC-new','[]',1) ON CONFLICT(revision) DO UPDATE SET active=1")
            return {"status": "COMPLETED", "stage": "INDEX_READY", "revision": "SYNTHETIC-new"}
        return DocumentJobQueue(self.db, self.catalog, handler, lease_seconds=1, clock=lambda: self.clock[0])

    def load_answers(self, result):
        path = Path(result["outputs"]["answers"]["output_path"])
        self.assertTrue(path.is_relative_to(self.base))
        return json.loads(path.read_text(encoding="utf-8"))

    def test_actual_job_activation_during_model_call_invalidates_measured_attempt(self):
        queue = self.queue()
        job = queue.enqueue("buyer", self.item, {"question": "SYNTHETIC private preparation question"})
        self.model.on_first_call = lambda: queue.run_pending(limit=1)
        result = run_evaluation(self.app, self.cases, self.config)
        artifact = self.load_answers(result)
        first = artifact["results"][0]["runs"]["no_rag"]
        self.assertEqual(first["status"], "FAILED")
        self.assertEqual(first["error"]["code"], "EVALUATION_SNAPSHOT_CHANGED")
        self.assertEqual(len(first["raw_callback_result"]["response"]["raw_model_calls"]), 1)
        self.assertGreaterEqual(first["elapsed_seconds"], 0)
        self.assertEqual(artifact["collected_count"], 0)
        self.assertEqual((result["status"], result["mode"], result["acceptance_status"]), ("INCOMPLETE", "MOCK", "NOT_ASSESSED"))
        self.assertEqual(queue.job("buyer", job["job_id"])["status"], "COMPLETED")
        self.assertNotIn("private preparation question", json.dumps(queue.job("buyer", job["job_id"])))
        with self.assertRaises(ServiceError):
            queue.job("seller", job["job_id"])

    def test_expired_handler_cannot_replace_revision_used_by_fresh_evaluation(self):
        old_queue = self.queue()
        job = old_queue.enqueue("buyer", self.item)
        old_claim = old_queue._claim()
        self.clock[0] += 2
        new_queue = self.queue()
        self.assertEqual(new_queue.run_pending(limit=1)["processed_jobs"], 1)
        with self.assertRaises(ServiceError) as caught:
            with self.db.transaction() as connection:
                connection.execute("UPDATE index_manifests SET active=0 WHERE active=1")
                connection.execute("INSERT INTO index_manifests(revision,version_ids_json,active) VALUES('SYNTHETIC-stale','[]',1)")
                _Progress(old_queue, job["job_id"], old_claim["claim_token"]).check(connection)
        self.assertEqual(caught.exception.code, "JOB_CLAIM_LOST")
        self.assertFalse(old_queue._finish(old_claim, {"status": "COMPLETED", "revision": "SYNTHETIC-initial"}))
        self.assertEqual(self.app.rag.corpus_summary()["revision"], "SYNTHETIC-new")
        result = run_evaluation(self.app, self.cases, self.config)
        artifact = self.load_answers(result)
        self.assertEqual(artifact["collected_count"], 10)
        self.assertEqual(artifact["context"]["revision"], "SYNTHETIC-new")
        self.assertEqual((artifact["mode"], artifact["acceptance_status"]), ("MOCK", "NOT_ASSESSED"))
        self.assertTrue(self.model.contexts)
        self.assertEqual(set(self.model.contexts), {("buyer", self.item)})
        self.assertEqual(new_queue.job("buyer", job["job_id"])["attempt_no"], 2)


if __name__ == "__main__":
    unittest.main()
