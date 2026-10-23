"""Runtime evaluation with authored fake models; never external provider calls."""
from contextlib import contextmanager
from copy import deepcopy
import json
import importlib.util
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from warranty.errors import ServiceError
from warranty.evaluation_cli import run_evaluation


class FakeStore:
    def load(self, kind):
        return {"kind": kind, "version": "1.0.0", "template": "SYNTHETIC-PRIVATE-POLICY-MARKER"}

    def render(self, kind, variables):
        return json.dumps({"policy": self.load(kind), "input": variables})


class FakePolicy:
    def __init__(self):
        self.allowed, self.calls = True, []

    def require(self, source_id, action, actor=None, version_id=None):
        self.calls.append((source_id, action, actor, version_id))
        if not self.allowed:
            raise ServiceError("SOURCE_BLOCKED")
        return {"status": "ALLOWED"}


class FakeCatalog:
    dataset_hash = "SYNTHETIC-dataset"

    def get(self, item_id):
        return {"item_id": item_id, "name": "SYNTHETIC authored item", "source_id": "SYNTHETIC-source",
                "index_text": "SYNTHETIC metadata evidence", "is_synthetic": item_id.startswith("SYNTHETIC")}


class FakeModel:
    is_mock = True
    model_id = "MOCK-runtime-model"

    def __init__(self):
        self.inputs, self.scopes, self.active, self.invalid_once = [], [], [], False
        self.on_generate = None

    def metadata(self):
        return {"provider": "local", "model_id": self.model_id, "model_version": "1",
                "is_mock": self.is_mock, "is_remote": not self.is_mock, "can_generate": True,
                "temperature": None, "sampling": "provider_default", "generation_settings": {"store": False}}

    @contextmanager
    def processing_context(self, actor, item_id):
        self.scopes.append((actor, item_id))
        self.active.append((actor, item_id))
        try:
            yield
        finally:
            self.active.pop()

    def generate(self, kind, prompt):
        if not self.active:
            raise AssertionError("No trusted processing context")
        envelope = json.loads(prompt)
        self.inputs.append(deepcopy(envelope["input"]))
        if self.on_generate:
            self.on_generate()
        if self.invalid_once:
            self.invalid_once = False
            return "invalid authored JSON"
        return {"classification": "NEEDS_INFORMATION", "answer": "MOCK authored fake reply",
                "claims": [], "conditions": [], "missing_information": [], "conflicts": [], "cited_evidence_ids": []}


class FakeEmbeddings:
    """Authored declarations only; this fixture never invokes a provider."""
    is_mock, is_remote = True, False
    name, dimension = "MOCK-authored-embedding:1", 16

    def metadata(self):
        return {"provider": "local", "model_id": "MOCK-embedding" if self.is_mock else "declared-embedding-fixture",
                "model_version": "1", "name": self.name, "dimension": self.dimension,
                "is_mock": self.is_mock, "is_remote": self.is_remote, "can_generate": True,
                "authorization": "per_context_required", "generation_settings": {"dimensions": self.dimension}}

    @contextmanager
    def processing_context(self, contexts):
        yield

    def embed(self, texts):
        raise AssertionError("This runtime fixture must never call an embedding provider")


class FakeRag:
    def __init__(self, model, catalog, embedding):
        self.model, self.catalog, self.prompt_store = model, catalog, FakeStore()
        self.vectors = SimpleNamespace(embeddings=embedding)
        self.registry = SimpleNamespace(policy=FakePolicy())
        self.revision, self.official = "SYNTHETIC-r1", False
        self.context_calls, self.compare_calls, self.prepare_calls = [], [], []
        self.blocked_question = None
        self.index_matches = True
        self.active_embedding_name = None

    def corpus_summary(self, item_ids=None):
        return {"revision": self.revision, "official_document_body_count": 2 if self.official else 0,
                "official_document_chunk_count": 2 if self.official else 0,
                "synthetic_document_body_count": 0 if self.official else 2,
                "synthetic_document_chunk_count": 0 if self.official else 2,
                "metadata_index_matches": self.index_matches,
                "active_index": {"revision": self.revision, "dataset_hash": self.catalog.dataset_hash,
                                 "embedding_model": self.active_embedding_name or self.vectors.embeddings.name,
                                 "dimension": self.vectors.embeddings.dimension}}

    def evaluation_context(self, actor, item_id, question, applicability=None, top_k=None, strategy=None,
                           *, allow_collection=False, cache=False):
        if not self.model.active or allow_collection is not False or cache is not False:
            raise AssertionError("Evaluation must use the guarded, fresh, no-collection seam")
        self.context_calls.append((actor, item_id, question, deepcopy(applicability), top_k, strategy))
        index = int(question.rsplit(" ", 1)[-1])
        kind = "HTML" if index % 2 else "PDF"
        document = {"evidence_id": f"SYNTHETIC-chunk-{index}", "item_id": item_id, "source_id": "SYNTHETIC-source",
                    "document_id": f"SYNTHETIC-document-{kind}", "version_id": f"SYNTHETIC-version-{kind}",
                    "source_type": "official_document" if self.official else "synthetic_document",
                    "role": "SERVICE_TERMS", "revision": self.revision, "type": kind, "text": "SYNTHETIC authored text",
                    "page": 1 if kind == "PDF" else None, "section": "SYNTHETIC section" if kind == "HTML" else None}
        meta = {"evidence_id": "meta-" + item_id, "item_id": item_id, "source_type": "metadata", "text": "SYNTHETIC metadata"}
        return {"item": self.catalog.get(item_id), "evidence": [meta, document], "revision": self.revision,
                "readiness": {"status": "DOC_BLOCKED" if question == self.blocked_question else "INDEX_READY"},
                "queries": ["SYNTHETIC fixed query"], "selection": {"selected_candidate_ids": ["SYNTHETIC-candidate"]},
                "missing_information": [], "conflicts": [], "errors": [],
                "retrieval_filters": {"item_id": item_id, "revision": self.revision, "allowed_pairs": [[document["version_id"], "SERVICE_TERMS"]]}}

    def compare_retrieval(self, actor, item_id, question, applicability=None, top_k=None):
        self.compare_calls.append(question)
        started = time.perf_counter()
        context = self.evaluation_context(actor, item_id, question, applicability, top_k, "similarity")
        common = time.perf_counter() - started
        timings = {}
        for strategy in ("similarity", "mmr"):
            started = time.perf_counter()
            context[strategy] = deepcopy([row for row in context["evidence"] if row["source_type"] != "metadata"])
            timings[strategy] = time.perf_counter() - started
        return {**context, "ranking_elapsed_seconds": timings, "common_context_elapsed_seconds": common}

    def prepare(self, actor, item_id, applicability=None, question=None):
        self.prepare_calls.append((actor, item_id))
        self.revision = "SYNTHETIC-r2"
        return {"job_id": "SYNTHETIC-job", "status": "COMPLETED", "stage": "INDEX_READY", "revision": self.revision}

    def ask(self, *args, **kwargs):
        raise AssertionError("Cached product answers must not substitute fresh evaluation calls")


class RuntimeEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="warranty-evaluation-runtime-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        catalog, model, embedding = FakeCatalog(), FakeModel(), FakeEmbeddings()
        rag = FakeRag(model, catalog, embedding)
        self.app = SimpleNamespace(catalog=catalog, rag=rag, models=SimpleNamespace(chat=model, embedding=embedding),
                                   settings=SimpleNamespace(remote_models=False),
                                   runtime={"models": {"chat_provider": "local", "embedding_provider": "local"}})
        self.app.runtime_status = lambda: {"chat": {"provider": "local", "configured": True, "ready": True, "is_mock": model.is_mock},
                                          "embedding": {"provider": "local", "configured": True, "ready": True, "is_mock": embedding.is_mock},
                                          "external_processing_approved": True, "question_processing_approved": True}
        self.cases = [{"case_id": f"SYNTHETIC-case-{index}", "item_id": "SYNTHETIC-item", "question": f"SYNTHETIC question {index}",
                       "applicability": {"country": "SYNTHETIC"}, "reference": {"status": "UNVERIFIED", "answer": "REFERENCE-ONLY-NEVER-SEND"}}
                      for index in range(1, 6)]
        self.config = {"mode": "MOCK", "output_dir": str(self.base / "private-output"), "user_id": "buyer"}

    def load(self, result, comparison=None):
        path = result["outputs"][comparison]["output_path"] if comparison else result["output_path"]
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def live_declarations(self):
        # Adversarial declarations only. The fake still emits an obvious MOCK
        # reply, which must never be counted as collected LIVE evidence.
        self.app.rag.model.is_mock = False
        self.app.rag.model.model_id = "declared-remote-fixture"
        self.app.models.embedding.is_mock, self.app.models.embedding.is_remote = False, True
        self.app.models.embedding.name = "local:declared-embedding-fixture:16:1"
        self.app.rag.official = True
        self.app.settings.remote_models = True
        cases = deepcopy(self.cases)
        for case in cases:
            case["item_id"] = "ADVERSARIAL-not-a-real-product"
        return cases, {**self.config, "mode": "LIVE", "external_process_authorized": True, "comparisons": ["answers"]}

    def test_runtime_runs_real_injected_methods_and_saves_only_private_evidence(self):
        result = run_evaluation(self.app, self.cases, self.config)
        self.assertEqual(result["status"], "COLLECTED")
        self.assertEqual(result["mode"], "MOCK")
        self.assertEqual(result["acceptance_status"], "NOT_ASSESSED")
        self.assertEqual(len(self.app.rag.model.inputs), 30)
        self.assertEqual(len(self.app.rag.compare_calls), 5)
        self.assertEqual(len(self.app.rag.context_calls), 30)
        runtime = self.load(result)
        self.assertEqual(runtime["references"][0]["reference"]["answer"], "REFERENCE-ONLY-NEVER-SEND")
        self.assertEqual({row["type"] for row in runtime["document_type_observations"]}, {"HTML", "PDF"})
        for path in self.base.rglob("comparison-*.json"):
            self.assertNotIn("SYNTHETIC-PRIVATE-POLICY-MARKER", path.read_text(encoding="utf-8"))
        for inputs in self.app.rag.model.inputs:
            self.assertNotIn("REFERENCE-ONLY-NEVER-SEND", json.dumps(inputs))
        self.assertTrue(self.app.rag.model.scopes)

    def test_no_rag_excludes_item_metadata_and_evidence_but_uses_same_policy_model(self):
        result = run_evaluation(self.app, self.cases, {**self.config, "comparisons": ["answers"]})
        artifact = self.load(result, "answers")
        for pair in artifact["results"]:
            left, right = pair["runs"]["no_rag"], pair["runs"]["rag"]
            self.assertEqual(left["input_context"]["item"], {})
            self.assertEqual(left["input_context"]["evidence"], [])
            self.assertEqual(left["input_context"]["question"], right["input_context"]["question"])
            self.assertTrue(right["input_context"]["evidence"])
            self.assertEqual(left["raw_callback_result"]["execution"], right["raw_callback_result"]["execution"])
            self.assertEqual(left["raw_callback_result"]["answer_model_invocation_count"], 1)
            self.assertEqual(right["raw_callback_result"]["answer_model_invocation_count"], 1)

    def test_performance_is_twenty_fresh_prepared_queries_with_observed_percentiles(self):
        result = run_evaluation(self.app, self.cases, {**self.config, "comparisons": ["performance"]})
        artifact = self.load(result, "performance")
        self.assertEqual(artifact["attempt_count"], 20)
        self.assertEqual(artifact["prepared_success_count"], 20)
        self.assertEqual(artifact["excluded_count"], 0)
        times = sorted(row["run"]["elapsed_seconds"] for row in artifact["samples"])
        self.assertEqual(artifact["p50_seconds"], times[9])
        self.assertEqual(artifact["p95_seconds"], times[18])
        self.assertTrue(all(value >= 0 for value in times))
        self.assertFalse(artifact["answer_cache"])
        self.assertFalse(artifact["collection_included"])
        self.assertEqual(len(self.app.rag.model.inputs), 20)

    def test_unprepared_performance_samples_are_reported_not_silently_removed(self):
        self.app.rag.blocked_question = self.cases[0]["question"]
        result = run_evaluation(self.app, self.cases, {**self.config, "comparisons": ["performance"]})
        artifact = self.load(result, "performance")
        self.assertEqual(artifact["prepared_success_count"], 16)
        self.assertEqual(artifact["excluded_count"], 4)
        self.assertEqual(artifact["status"], "INCOMPLETE")
        self.assertFalse(artifact["sample_requirement_met"])
        self.assertEqual(len(artifact["samples"]), 20)
        self.assertEqual(artifact["exclusion_counts"], {"INDEX_NOT_PREPARED": 4})
        all_times = sorted(sample["run"]["elapsed_seconds"] for sample in artifact["samples"])
        self.assertEqual(artifact["all_attempt_p50_seconds"], all_times[9])
        self.assertEqual(artifact["all_attempt_p95_seconds"], all_times[18])

    def test_failed_performance_attempts_keep_latency_and_reason_counts(self):
        def unavailable(*args, **kwargs):
            raise ServiceError("MODEL_UNAVAILABLE")
        self.app.rag.model.generate = unavailable
        result = run_evaluation(self.app, self.cases, {**self.config, "comparisons": ["performance"]})
        artifact = self.load(result, "performance")
        self.assertEqual(artifact["attempt_count"], 20)
        self.assertEqual(artifact["prepared_success_count"], 0)
        self.assertEqual(artifact["exclusion_counts"], {"MODEL_UNAVAILABLE": 20})
        self.assertIsNone(artifact["p95_seconds"])
        self.assertGreaterEqual(artifact["all_attempt_p95_seconds"], 0)
        self.assertEqual(result["status"], "INCOMPLETE")

    def test_cold_preparation_is_separate_from_unmeasured_external_collection(self):
        result = run_evaluation(self.app, self.cases, {**self.config, "comparisons": ["performance"], "measure_cold_prepare": True})
        cold = self.load(result)["cold_preparation"]
        self.assertEqual(cold["status"], "MEASURED_PREPARATION_OPERATION")
        self.assertEqual(len(cold["operations"]), 1)
        self.assertGreaterEqual(cold["operations"][0]["elapsed_seconds"], 0)
        self.assertIsNone(cold["cold_collection_seconds"])
        self.assertEqual(self.load(result, "performance")["context"]["revision"], "SYNTHETIC-r2")

    def test_same_retrieval_pool_uses_actual_backend_timings_and_chunk_ids(self):
        result = run_evaluation(self.app, self.cases, {**self.config, "comparisons": ["retrieval"], "filters": {"country": "SYNTHETIC"}})
        artifact = self.load(result, "retrieval")
        self.assertEqual(len(self.app.rag.compare_calls), 5)
        for pair in artifact["results"]:
            left, right = pair["runs"]["similarity"], pair["runs"]["mmr"]
            self.assertEqual(left["metrics"]["chunk_ids"], right["metrics"]["chunk_ids"])
            self.assertEqual(left["raw_callback_result"]["retrieval_snapshot"], right["raw_callback_result"]["retrieval_snapshot"])
            self.assertGreaterEqual(left["raw_callback_result"]["ranking_elapsed_seconds"], 0)
            self.assertGreaterEqual(right["raw_callback_result"]["ranking_elapsed_seconds"], 0)

    def test_separately_measured_backend_collection_timer_is_preserved(self):
        original = self.app.rag.prepare
        def measured(*args, **kwargs):
            started = time.perf_counter()
            job = original(*args, **kwargs)
            return {**job, "collection_elapsed_seconds": time.perf_counter() - started}
        self.app.rag.prepare = measured
        result = run_evaluation(self.app, self.cases, {**self.config, "comparisons": ["performance"], "measure_cold_prepare": True})
        cold = self.load(result)["cold_preparation"]
        observed = cold["operations"][0]["collection_elapsed_seconds"]
        self.assertGreaterEqual(observed, 0)
        self.assertEqual(cold["cold_collection_seconds"], observed)

    def test_private_input_files_work_and_repository_input_is_rejected(self):
        cases_path, config_path = self.base / "cases.json", self.base / "config.json"
        cases_path.write_text(json.dumps(self.cases), encoding="utf-8")
        config_path.write_text(json.dumps({**self.config, "comparisons": ["answers"]}), encoding="utf-8")
        self.assertEqual(run_evaluation(self.app, cases_path, config_path)["status"], "COLLECTED")
        with self.assertRaises(ServiceError) as raised:
            run_evaluation(self.app, Path(__file__), config_path)
        self.assertEqual(raised.exception.code, "EVALUATION_INPUT_PRIVATE_REQUIRED")

    def test_invalid_or_conflicting_config_fails_before_model_execution(self):
        for change in ({"performance_runs": 19}, {"filters": {"country": "OTHER"}}, {"top_k": True},
                       {"model_metadata": {"is_mock": False}}, {"comparisons": ["answers", "answers"]}):
            with self.subTest(change=change), self.assertRaises(ServiceError):
                run_evaluation(self.app, self.cases, {**self.config, **change})
        self.assertEqual(self.app.rag.model.inputs, [])

    def test_live_does_not_relabel_mock_model_even_with_approval_flag(self):
        with self.assertRaises(ServiceError) as raised:
            run_evaluation(self.app, self.cases, {**self.config, "mode": "LIVE", "external_process_authorized": True})
        self.assertEqual(raised.exception.code, "EVALUATION_LIVE_BLOCKED")
        self.assertEqual(self.app.rag.model.inputs, [])

    def test_live_rejects_mock_embeddings_for_every_full_comparison(self):
        cases, config = self.live_declarations()
        self.app.models.embedding.is_mock = True
        for comparison in ("answers", "retrieval", "performance"):
            with self.subTest(comparison=comparison), self.assertRaises(ServiceError) as raised:
                run_evaluation(self.app, cases, {**config, "comparisons": [comparison]})
            self.assertEqual(raised.exception.code, "EVALUATION_LIVE_BLOCKED")
        self.assertEqual(self.app.rag.model.inputs, [])
        self.assertEqual(self.app.rag.context_calls, [])

    def test_live_embedding_provenance_gates_precede_generation(self):
        cases, config = self.live_declarations()
        embedding = self.app.models.embedding
        original_metadata = embedding.metadata
        changes = [
            lambda: setattr(self.app.rag, "index_matches", False),
            lambda: setattr(self.app.rag, "active_embedding_name", "MOCK-old-index"),
            lambda: setattr(embedding, "is_remote", False),
            lambda: setattr(embedding, "metadata", lambda: {**original_metadata(), "model_version": None}),
            lambda: setattr(embedding, "metadata", lambda: {**original_metadata(), "is_mock": True}),
            lambda: setattr(embedding, "metadata", lambda: {**original_metadata(), "dimension": 99}),
            lambda: setattr(embedding, "metadata", lambda: {**original_metadata(), "can_generate": False}),
        ]
        for change in changes:
            embedding.metadata = original_metadata
            embedding.is_remote = True
            self.app.rag.index_matches = True
            self.app.rag.active_embedding_name = None
            change()
            with self.subTest(change=changes.index(change)), self.assertRaises(ServiceError) as raised:
                run_evaluation(self.app, cases, config)
            self.assertEqual(raised.exception.code, "EVALUATION_LIVE_BLOCKED")
        self.assertEqual(self.app.rag.model.inputs, [])

    def test_live_has_no_implicit_gateway_execution_entry(self):
        cases, config = self.live_declarations()
        original = self.app.rag.model.metadata
        self.app.rag.model.metadata = lambda: {**original(), "live_execution_required": True, "can_generate": False}
        with self.assertRaises(ServiceError) as raised:
            run_evaluation(self.app, cases, config)
        self.assertEqual(raised.exception.code, "EVALUATION_LIVE_BLOCKED")
        self.assertEqual(self.app.rag.model.inputs, [])

    def test_runtime_records_safe_chat_embedding_and_index_configuration(self):
        self.app.runtime["models"]["api_key"] = "PRIVATE-SECRET-DO-NOT-RECORD"
        result = run_evaluation(self.app, self.cases, {**self.config, "comparisons": ["answers"]})
        artifact = self.load(result)
        environment = artifact["execution_environment"]
        self.assertTrue(environment["chat"]["is_mock"])
        self.assertEqual(environment["embedding"]["name"], self.app.models.embedding.name)
        self.assertEqual(environment["embedding"]["dimension"], 16)
        self.assertEqual(artifact["corpus_snapshot"]["active_index"]["embedding_model"], self.app.models.embedding.name)
        self.assertNotIn("PRIVATE-SECRET-DO-NOT-RECORD", json.dumps(artifact))

    def test_embedding_change_invalidates_the_frozen_run(self):
        self.app.rag.model.on_generate = lambda: setattr(self.app.models.embedding, "name", "MOCK-changed-embedding")
        result = run_evaluation(self.app, self.cases, {**self.config, "comparisons": ["answers"]})
        artifact = self.load(result, "answers")
        first = artifact["results"][0]["runs"]["no_rag"]
        self.assertEqual(first["status"], "FAILED")
        self.assertEqual(first["error"]["code"], "EVALUATION_SNAPSHOT_CHANGED")
        self.assertEqual(artifact["collected_count"], 0)

    def test_credit_or_provider_failure_never_produces_quality_success(self):
        cases, config = self.live_declarations()
        for code in ("LIVE_BUDGET_UNCONFIRMED", "MODEL_UNAVAILABLE"):
            def deny(*args, **kwargs):
                raise ServiceError(code)
            self.app.rag.model.generate = deny
            result = run_evaluation(self.app, cases, config)
            artifact = self.load(result, "answers")
            self.assertEqual(artifact["collected_count"], 0)
            self.assertEqual(artifact["failed_count"], 10)
            self.assertEqual(result["status"], "INCOMPLETE")
            self.assertEqual(result["acceptance_status"], "NOT_ASSESSED")
            run = artifact["results"][0]["runs"]["no_rag"]
            self.assertEqual(run["error"]["code"], "MODEL_UNAVAILABLE")
            self.assertEqual(run["raw_callback_result"]["response"]["raw_model_calls"][0]["error"]["code"], code)

    def test_live_corpus_approval_source_and_version_gates_run_before_generation(self):
        cases, config = self.live_declarations()
        for blocked in ("corpus", "approval", "source", "version"):
            with self.subTest(blocked=blocked):
                self.app.rag.official = blocked != "corpus"
                self.app.rag.registry.policy.allowed = blocked != "source"
                original = self.app.rag.model.metadata
                if blocked == "version":
                    self.app.rag.model.metadata = lambda: {**original(), "model_version": None}
                try:
                    with self.assertRaises(ServiceError):
                        run_evaluation(self.app, cases, {**config, "external_process_authorized": blocked != "approval"})
                finally:
                    self.app.rag.model.metadata = original
        self.assertEqual(self.app.rag.model.inputs, [])

    def test_obvious_fake_reply_cannot_be_counted_as_live_model_evidence(self):
        cases, config = self.live_declarations()
        result = run_evaluation(self.app, cases, config)
        artifact = self.load(result, "answers")
        self.assertEqual(artifact["collected_count"], 0)
        self.assertEqual(artifact["failed_count"], 10)
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertEqual(result["acceptance_status"], "NOT_ASSESSED")

    def test_revision_changes_keep_raw_attempt_but_never_count_success(self):
        self.app.rag.model.on_generate = lambda: setattr(self.app.rag, "revision", "CHANGED")
        result = run_evaluation(self.app, self.cases, {**self.config, "comparisons": ["answers"]})
        artifact = self.load(result, "answers")
        first = artifact["results"][0]["runs"]["no_rag"]
        self.assertEqual(first["status"], "FAILED")
        self.assertEqual(first["error"]["code"], "EVALUATION_SNAPSHOT_CHANGED")
        self.assertEqual(len(first["raw_callback_result"]["response"]["raw_model_calls"]), 1)
        self.assertEqual(artifact["collected_count"], 0)

    def test_format_repair_records_both_actual_raw_calls_without_policy_text(self):
        self.app.rag.model.invalid_once = True
        result = run_evaluation(self.app, self.cases, {**self.config, "comparisons": ["answers"]})
        first = self.load(result, "answers")["results"][0]["runs"]["no_rag"]
        calls = first["raw_response"]["raw_model_calls"]
        self.assertEqual(len(calls), 2)
        self.assertFalse(calls[0]["format_repair"])
        self.assertTrue(calls[1]["format_repair"])
        self.assertEqual(calls[0]["raw_response"], "invalid authored JSON")
        self.assertNotIn("SYNTHETIC-PRIVATE-POLICY-MARKER", json.dumps(calls))

    def test_detached_model_handle_is_not_allowed_to_change_comparison_provider(self):
        self.app.models.chat = FakeModel()
        with self.assertRaises(ServiceError) as raised:
            run_evaluation(self.app, self.cases, self.config)
        self.assertEqual(raised.exception.code, "EVALUATION_MODEL_INVALID")

    @unittest.skipUnless(importlib.util.find_spec("chromadb") and importlib.util.find_spec("langgraph"),
                         "Optional application dependencies are required for the shared-facade check")
    def test_real_application_mock_runtime_does_not_claim_empty_corpus_is_prepared(self):
        from warranty.application import build_application
        from warranty.config import Settings
        from warranty.prompts import output_schema
        config_dir = self.base / "app-config"
        (config_dir / "prompts").mkdir(parents=True)
        variables = {"query": ["item", "question", "applicability"],
                     "selection": ["item", "question", "applicability", "candidates"],
                     "answer": ["item", "question", "applicability", "evidence", "missing_information", "conflicts"]}
        for kind, names in variables.items():
            policy = {"version": "1.0.0", "kind": kind, "role": "SYNTHETIC authored integration policy",
                      "input_variables": names, "output_schema": output_schema(kind),
                      "template": "SYNTHETIC authored integration template"}
            for key in ("evidence_limits", "missing_information_rules", "conflict_rules", "attack_rules"):
                policy[key] = ["SYNTHETIC authored test rule"]
            (config_dir / "prompts" / (kind + ".json")).write_text(json.dumps(policy), encoding="utf-8")
        settings = Settings(self.base / "app-data", config_dir, remote_models=False)
        with patch.dict(os.environ, {"METADATA_PATH": "", "METADATA_EXPECTED_HASH": ""}):
            app = build_application(settings)
        self.addCleanup(app.close)
        cases = [{**case, "item_id": app.catalog.items[0]["item_id"]} for case in self.cases]
        result = run_evaluation(app, cases, self.config)
        self.assertEqual(self.load(result, "answers")["collected_count"], 10)
        performance = self.load(result, "performance")
        self.assertEqual(performance["prepared_success_count"], 0)
        self.assertEqual(performance["excluded_count"], 20)
        self.assertIsNone(performance["p95_seconds"])
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertEqual(result["mode"], "MOCK")


if __name__ == "__main__":
    unittest.main()
