"""Offline comparison collector checks with authored synthetic cases only."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import time
import unittest

from warranty.errors import ServiceError
from warranty.evaluation import ComparisonRunner


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="warranty-evaluation-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.cases = [{"case_id": f"SYNTHETIC-case-{index}", "item_id": "SYNTHETIC-001",
                       "question": f"SYNTHETIC authored question {index}",
                       "applicability": {"country": "SYNTHETIC"}}
                      for index in range(1, 6)]

    def runner(self, **kwargs):
        return ComparisonRunner(self.base / "private", **kwargs)

    def live_configuration(self):
        # Only a guard fixture. No test performs a real/provider model call.
        return {"mode": "LIVE", "model_metadata": {"provider": "test-provider", "model_id": "test-model",
                "model_version": "test-version", "is_mock": False}, "settings": {"temperature": 0},
                "revision": "test-revision", "dataset_fingerprint": "test-dataset",
                "official_body_count": 1, "external_process_authorized": True}

    def test_pairs_use_identical_frozen_context_and_capture_actual_responses(self):
        model = {"provider": "local", "model_id": "MOCK-authored", "model_version": "v1", "is_mock": True}
        settings = {"temperature": 0, "generation": {"seed": 7}}
        runner = self.runner(model_metadata=model, settings=settings, revision="SYNTHETIC-r1")
        model["model_version"] = "changed-after-construction"
        settings["generation"]["seed"] = 123
        seen = []

        def answer(request):
            seen.append(deepcopy(request))
            return {"response": {"answer": request["variant"] + ":" + request["question"], "model": "MOCK-authored"},
                    "execution": request["context"], "input_context": {"evidence": []}}

        artifact = runner.run_answers(self.cases, answer, answer)
        self.assertEqual(artifact["status"], "COLLECTED")
        self.assertEqual(artifact["attempt_count"], 10)
        self.assertEqual(artifact["case_count"], 5)
        self.assertEqual(artifact["acceptance_status"], "NOT_ASSESSED")
        self.assertIsNone(artifact["scores"])
        self.assertEqual(artifact["mode"], "MOCK")
        for before, after in zip(seen[::2], seen[1::2]):
            self.assertEqual(before["context"], after["context"])
            self.assertEqual(before["question"], after["question"])
            self.assertEqual(before["item_id"], after["item_id"])
            self.assertEqual(before["context"]["settings"]["generation"]["seed"], 7)
        result = artifact["results"][0]["runs"]
        self.assertNotEqual(result["no_rag"]["raw_response"], result["rag"]["raw_response"])
        self.assertTrue(runner.is_current(artifact, self.cases))
        saved = json.loads(Path(artifact["output_path"]).read_text(encoding="utf-8"))
        self.assertTrue(runner.is_current(saved, self.cases))
        self.assertNotIn("output_path", saved)

    def test_latency_is_measured_instead_of_trusting_callback_claim(self):
        def answer(request):
            time.sleep(0.002)
            return {"answer": "SYNTHETIC", "elapsed_seconds": 999999}

        artifact = self.runner().run_answers(self.cases, answer, answer)
        for case in artifact["results"]:
            for run in case["runs"].values():
                self.assertGreaterEqual(run["elapsed_seconds"], 0.001)
                self.assertLess(run["elapsed_seconds"], 30)
                self.assertEqual(run["raw_response"]["elapsed_seconds"], 999999)

    def test_less_than_five_and_duplicate_fixed_cases_rejected_before_callbacks(self):
        called = []
        for cases in (self.cases[:4], [self.cases[0]] * 5,
                      self.cases[:4] + [{**self.cases[0], "case_id": "new-id", "question": "  SYNTHETIC authored   question 1 "}]):
            with self.subTest(cases=cases), self.assertRaises(ServiceError):
                self.runner().run_answers(cases, lambda r: called.append(r), lambda r: called.append(r))
        self.assertEqual(called, [])
        self.assertFalse((self.base / "private").exists())

    def test_case_and_config_changes_invalidate_prior_artifact(self):
        runner = self.runner(settings={"temperature": 0}, revision="SYNTHETIC-r1", dataset_fingerprint="SYNTHETIC-d1")
        artifact = runner.run_answers(self.cases, lambda r: "MOCK", lambda r: "MOCK")
        altered = deepcopy(self.cases)
        altered[0]["question"] += " changed"
        self.assertFalse(runner.is_current(artifact, altered))
        for arguments in ({"settings": {"temperature": 1}, "revision": "SYNTHETIC-r1", "dataset_fingerprint": "SYNTHETIC-d1"},
                          {"settings": {"temperature": 0}, "revision": "SYNTHETIC-r2", "dataset_fingerprint": "SYNTHETIC-d1"},
                          {"settings": {"temperature": 0}, "revision": "SYNTHETIC-r1", "dataset_fingerprint": "SYNTHETIC-d2"}):
            self.assertFalse(self.runner(**arguments).is_current(artifact, self.cases))
        tampered = deepcopy(artifact)
        tampered["results"][0]["runs"]["rag"]["raw_response"] = "changed"
        self.assertFalse(runner.is_current(tampered, self.cases))

    def test_each_run_executes_again_instead_of_reusing_measured_output(self):
        called = []
        runner = self.runner()
        callback = lambda request: called.append(request["variant"]) or "MOCK"
        first = runner.run_answers(self.cases, callback, callback)
        second = runner.run_answers(self.cases, callback, callback)
        self.assertEqual(len(called), 20)
        self.assertNotEqual(first["output_path"], second["output_path"])

    def test_failures_are_counted_and_exception_secrets_are_not_recorded(self):
        def failed(request):
            raise RuntimeError("private-path/secret-auth-value")

        artifact = self.runner().run_answers(self.cases, lambda r: "MOCK success", failed)
        self.assertEqual(artifact["status"], "PARTIAL")
        self.assertEqual(artifact["collected_count"], 5)
        self.assertEqual(artifact["failed_count"], 5)
        self.assertNotIn("private-path", json.dumps(artifact))
        for case in artifact["results"]:
            failure = case["runs"]["rag"]
            self.assertEqual(failure["error"]["code"], "EVALUATION_CALLBACK_FAILED")
            self.assertGreaterEqual(failure["elapsed_seconds"], 0)

    def test_live_gates_reject_missing_bodies_authorization_and_metadata(self):
        for change in ({"official_body_count": 0}, {"external_process_authorized": False},
                       {"model_metadata": {}}, {"model_metadata": None}, {"settings": {}},
                       {"revision": None}, {"dataset_fingerprint": None},
                       {"model_metadata": {"provider": "local", "model_id": "MOCK", "model_version": "1", "is_mock": True}}):
            configuration = self.live_configuration()
            configuration.update(change)
            with self.subTest(change=change), self.assertRaises(ServiceError) as raised:
                self.runner(**configuration)
            self.assertEqual(raised.exception.code, "EVALUATION_LIVE_BLOCKED")
        self.assertFalse((self.base / "private").exists())

    def test_mock_responses_cannot_be_recorded_as_successful_live_collection(self):
        runner = self.runner(**self.live_configuration())

        def mock_packet(request):
            return {"response": {"answer": "MOCK", "model": "MOCK-deterministic"},
                    "execution": request["context"], "input_context": []}

        artifact = runner.run_answers(self.cases, lambda r: "MOCK", mock_packet)
        self.assertEqual(artifact["status"], "FAILED")
        self.assertEqual(artifact["collected_count"], 0)
        self.assertEqual(artifact["acceptance_status"], "NOT_ASSESSED")
        self.assertIsNone(artifact["scores"])

    def test_nested_mock_answer_content_and_metadata_are_rejected_in_live_mode(self):
        runner = self.runner(**self.live_configuration())
        for response in ({"answer": "[MOCK authored reply]"},
                         ["MOCK fixture response"],
                         {"choices": [{"message": {"content": "MOCK deterministic response"}}]},
                         {"output": [{"content": [{"type": "output_text", "text": "[MOCK] fixture"}]}]},
                         {"nested": {"execution": {"is_mock": True}}},
                         {"model_metadata": {"model": {"name": "MOCK-adapter"}}}):
            with self.subTest(response=response):
                def callback(request):
                    return {"response": response, "execution": request["context"], "input_context": []}

                artifact = runner.run_answers(self.cases, callback, callback)
                self.assertEqual(artifact["status"], "FAILED")
                self.assertEqual(artifact["collected_count"], 0)
                self.assertEqual(artifact["acceptance_status"], "NOT_ASSESSED")
                self.assertEqual(artifact["results"][0]["runs"]["rag"]["error"]["code"], "EVALUATION_EXECUTION_MISMATCH")

    def test_execution_metadata_drift_is_retained_as_failure(self):
        def changed(request):
            request["context"]["revision"] = "different"
            return {"response": "MOCK", "execution": request["context"], "input_context": []}

        artifact = self.runner().run_answers(self.cases, changed, lambda r: "MOCK")
        self.assertEqual(artifact["failed_count"], 5)
        failed = artifact["results"][0]["runs"]["no_rag"]
        self.assertIn("raw_callback_result", failed)
        self.assertEqual(failed["error"]["code"], "EVALUATION_EXECUTION_MISMATCH")

    def test_retrieval_compares_same_inputs_and_records_observed_diversity(self):
        requests = []

        def retrieve(request):
            requests.append(deepcopy(request))
            base = {"item_id": request["item_id"], "revision": "SYNTHETIC-r1", "role": "SERVICE_TERMS",
                    "source_type": "synthetic_document", "version_id": "SYNTHETIC-v1"}
            one = {**base, "evidence_id": "SYNTHETIC-c1", "document_id": "SYNTHETIC-d1", "text": "Repeated text"}
            two = {**base, "evidence_id": "SYNTHETIC-c2", "document_id": "SYNTHETIC-d1", "text": "Repeated  text"}
            if request["strategy"] == "mmr":
                two = {**base, "evidence_id": "SYNTHETIC-c3", "document_id": "SYNTHETIC-d2", "text": "Different text", "role": "TRANSFER_GUIDE"}
            return [one, two]

        runner = self.runner(revision="SYNTHETIC-r1")
        artifact = runner.run_retrieval(self.cases, retrieve, top_k=3, filters={"country": "SYNTHETIC"})
        self.assertEqual(artifact["collected_count"], 10)
        pair = artifact["results"][0]["runs"]
        self.assertEqual(pair["similarity"]["metrics"]["duplicate_count"], 1)
        self.assertEqual(pair["similarity"]["metrics"]["distinct_document_count"], 1)
        self.assertEqual(pair["mmr"]["metrics"]["duplicate_count"], 0)
        self.assertEqual(pair["mmr"]["metrics"]["distinct_document_count"], 2)
        self.assertEqual(pair["mmr"]["metrics"]["own_document_count"], 1)
        self.assertEqual(pair["mmr"]["metrics"]["chunk_ids"], ["SYNTHETIC-c1", "SYNTHETIC-c3"])
        for left, right in zip(requests[::2], requests[1::2]):
            self.assertEqual({k: v for k, v in left.items() if k not in {"variant", "strategy"}},
                             {k: v for k, v in right.items() if k not in {"variant", "strategy"}})
        self.assertTrue(runner.is_current(artifact, self.cases, comparison="retrieval", top_k=3, filters={"country": "SYNTHETIC"}))
        self.assertFalse(runner.is_current(artifact, self.cases, comparison="retrieval", top_k=6, filters={"country": "SYNTHETIC"}))
        self.assertFalse(runner.is_current(artifact, self.cases, comparison="retrieval", top_k=3, filters={}))

    def test_wrong_item_revision_or_metadata_chunk_is_not_a_valid_comparison(self):
        for row in ({"evidence_id": "c", "item_id": "OTHER"},
                    {"evidence_id": "c", "revision": "OTHER"},
                    {"evidence_id": "c", "source_type": "metadata"},
                    {"evidence_id": "c", "role": []}):
            with self.subTest(row=row):
                artifact = self.runner().run_retrieval(self.cases, lambda r: [row])
                self.assertEqual(artifact["collected_count"], 0)
                self.assertEqual(artifact["failed_count"], 10)

    def test_missing_document_identity_and_explicit_own_flag_are_not_invented(self):
        rows = [{"evidence_id": "SYNTHETIC-c1", "version_id": "SYNTHETIC-v1", "role": "SERVICE_TERMS", "own_document": False},
                {"evidence_id": "SYNTHETIC-c1", "version_id": "SYNTHETIC-v1", "role": "TRANSFER_GUIDE"}]
        artifact = self.runner().run_retrieval(self.cases, lambda r: rows)
        metrics = artifact["results"][0]["runs"]["similarity"]["metrics"]
        self.assertEqual(metrics["distinct_document_count"], 0)
        self.assertEqual(metrics["unknown_document_identity_count"], 2)
        self.assertEqual(metrics["distinct_document_version_count"], 1)
        self.assertEqual(metrics["duplicate_chunk_id_count"], 1)
        self.assertEqual(metrics["own_document_chunk_count"], 0)

    def test_output_inside_repository_rejected_before_writing(self):
        actual_repo = Path(__file__).resolve().parents[1]
        for target in (actual_repo, actual_repo / "evaluation-output"):
            with self.subTest(target=target), self.assertRaises(ServiceError):
                ComparisonRunner(target)
        unrelated_repo = self.base / "other-repo"
        (unrelated_repo / ".git").mkdir(parents=True)
        with self.assertRaises(ServiceError):
            ComparisonRunner(unrelated_repo / "results")
        self.assertFalse((unrelated_repo / "results").exists())

    def test_non_json_responses_fail_without_loss_of_other_attempts(self):
        artifact = self.runner().run_answers(self.cases, lambda r: float("nan"), lambda r: "MOCK")
        self.assertEqual(artifact["failed_count"], 5)
        self.assertEqual(artifact["attempt_count"], 10)


if __name__ == "__main__":
    unittest.main()
