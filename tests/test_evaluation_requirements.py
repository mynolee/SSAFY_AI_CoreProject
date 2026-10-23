"""Authored offline fixtures for review integrity and measured metric gaps."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from warranty.errors import ServiceError
from warranty.evaluation import ComparisonRunner, answer_review_template, assessed_answer_review
from warranty.evaluation_cli import review_evaluation


class EvaluationRequirementsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="warranty-evaluation-review-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.runner = ComparisonRunner(self.base / "private")
        self.cases = [{"case_id": f"SYNTHETIC-case-{index}", "item_id": "SYNTHETIC-item",
                       "question": f"SYNTHETIC authored question {index}",
                       "reference": {"status": "VERIFIED", "required_evidence_ids": ["SYNTHETIC-c1", "SYNTHETIC-c2"],
                                     "sources": [{"evidence_id": "SYNTHETIC-ref", "locator": "SYNTHETIC HTML section 1"}]}}
                      for index in range(5)]

    def answers(self, cases=None):
        def answer(request):
            self.assertNotIn("reference", request)
            return {"answer": f"MOCK authored {request['variant']} answer"}
        return self.runner.run_answers(self.cases if cases is None else cases, answer, answer)

    def review(self, artifact):
        review = answer_review_template(artifact)
        review.update(reviewer="SYNTHETIC-authored-reviewer", reviewer_type="AGENT_ASSISTED",
                      reviewed_at_utc="2026-10-23T00:00:00Z")
        for row in review["results"]:
            for name, judgment in row["assessment"].items():
                judgment.update(outcome={"accuracy": "CORRECT", "evidence": "SUPPORTED", "uncertainty": "APPROPRIATE"}[name],
                                rationale="SYNTHETIC authored review basis, not actual quality evidence.",
                                reference_ids=["SYNTHETIC-ref"])
        return review

    def test_draft_and_review_are_private_and_never_promote_mock_acceptance(self):
        artifact = self.answers()
        draft = review_evaluation(artifact["output_path"], self.base / "review-output")
        self.assertEqual(draft["status"], "DRAFT")
        worksheet = json.loads(Path(draft["output_path"]).read_text(encoding="utf-8"))
        self.assertEqual(worksheet["review_template"]["results"][0]["assessment"]["accuracy"]["outcome"], "UNREVIEWED")
        completed = self.review(artifact)
        review_path = self.base / "completed.json"
        review_path.write_text(json.dumps(completed), encoding="utf-8")
        result = review_evaluation(artifact["output_path"], self.base / "review-output", review=review_path)
        self.assertEqual((result["status"], result["mode"], result["acceptance_status"]), ("REVIEWED", "MOCK", "NOT_ASSESSED"))
        report = json.loads(Path(result["output_path"]).read_text(encoding="utf-8"))
        self.assertEqual(report["reviewer_type"], "AGENT_ASSISTED")
        self.assertEqual(report["outcome_counts"]["accuracy"]["CORRECT"], 10)
        self.assertEqual(report["variant_outcome_counts"]["rag"]["accuracy"]["CORRECT"], 5)
        self.assertEqual(report["variant_outcome_counts"]["no_rag"]["accuracy"]["CORRECT"], 5)
        self.assertEqual(report["fully_paired_reviewed_case_count"], 5)
        self.assertEqual(set(report["case_comparisons"][0]["variants"]), {"no_rag", "rag"})
        self.assertEqual(report["reviewed_response_count"], 10)
        self.assertIsNone(report["scores"])
        self.assertTrue(self.runner.is_current(artifact, self.cases))

    def test_changed_response_or_other_artifact_cannot_reuse_review(self):
        artifact = self.answers()
        review = self.review(artifact)
        changed = deepcopy(artifact)
        changed["results"][0]["runs"]["rag"]["raw_response"]["answer"] = "changed"
        for source in (changed, self.answers()):
            with self.subTest(source=source["artifact_fingerprint"]), self.assertRaises(ServiceError):
                review_evaluation(source, self.base / "reviews", review)
        self.assertFalse((self.base / "reviews").exists())

    def test_draft_cannot_be_recorded_without_explicit_rationales_and_identity(self):
        artifact = self.answers()
        with self.assertRaises(ServiceError):
            assessed_answer_review(artifact, answer_review_template(artifact))
        for change in ({"reviewer": ""}, {"reviewer_type": "MODEL"}, {"reviewed_at_utc": "2026-10-23"}):
            with self.subTest(change=change), self.assertRaises(ServiceError):
                assessed_answer_review(artifact, {**self.review(artifact), **change})
        review = self.review(artifact)
        review["results"][0]["assessment"]["accuracy"]["rationale"] = ""
        with self.assertRaises(ServiceError):
            assessed_answer_review(artifact, review)

    def test_verified_source_with_locator_is_required_for_accuracy_and_grounding(self):
        for reference in ({"status": "UNVERIFIED", "sources": self.cases[0]["reference"]["sources"]},
                          {"status": "VERIFIED", "sources": [{"evidence_id": "SYNTHETIC-ref"}]}):
            cases = deepcopy(self.cases)
            cases[0]["reference"] = reference
            artifact = self.answers(cases)
            with self.subTest(reference=reference), self.assertRaises(ServiceError):
                assessed_answer_review(artifact, self.review(artifact))
        artifact = self.answers()
        review = self.review(artifact)
        review["results"][0]["assessment"]["accuracy"]["reference_ids"] = []
        with self.assertRaises(ServiceError) as raised:
            assessed_answer_review(artifact, review)
        self.assertEqual(raised.exception.code, "EVALUATION_REFERENCE_UNVERIFIED")

    def test_unverified_case_remains_unverified_and_cannot_acquire_posthoc_sources(self):
        cases = deepcopy(self.cases)
        cases[0]["reference"] = {"status": "UNVERIFIED", "reason": "SYNTHETIC no applicable product"}
        artifact = self.answers(cases)
        review = self.review(artifact)
        for row in review["results"][:2]:
            for name, judgment in row["assessment"].items():
                judgment.update(outcome="UNVERIFIED", reference_ids=[])
        report = assessed_answer_review(artifact, review)
        self.assertEqual(report["outcome_counts"]["accuracy"]["UNVERIFIED"], 2)
        review["results"][0]["reference"] = deepcopy(self.cases[0]["reference"])
        with self.assertRaises(ServiceError):
            assessed_answer_review(artifact, review)

    def test_partial_duplicate_and_unknown_response_reviews_are_reported_or_rejected(self):
        artifact = self.answers()
        review = self.review(artifact)
        review["results"] = review["results"][:1]
        report = assessed_answer_review(artifact, review)
        self.assertEqual((report["status"], report["unreviewed_response_count"]), ("PARTIAL", 9))
        for mutation in (lambda rows: rows.append(deepcopy(rows[0])),
                         lambda rows: rows[0].update(response_fingerprint="wrong"),
                         lambda rows: rows[0].update(case_id="SYNTHETIC-unknown")):
            changed = deepcopy(review)
            mutation(changed["results"])
            with self.assertRaises(ServiceError):
                assessed_answer_review(artifact, changed)

    def test_failed_generation_is_not_available_as_a_reviewable_response(self):
        def fail(request):
            raise RuntimeError("SYNTHETIC failure")
        artifact = self.runner.run_answers(self.cases, fail, lambda request: "MOCK authored")
        review = self.review(artifact)
        report = assessed_answer_review(artifact, review)
        self.assertEqual(report["failed_response_count"], 5)
        self.assertEqual(report["collected_response_count"], 5)
        self.assertTrue(all(row["variant"] == "rag" for row in review["results"]))

    def test_required_evidence_recall_and_diversity_are_observed_from_actual_chunks(self):
        def retrieve(request):
            self.assertNotIn("reference", request)
            ids = ["SYNTHETIC-c1", "SYNTHETIC-c2"] if request["strategy"] == "mmr" else ["SYNTHETIC-c1", "SYNTHETIC-other"]
            return [{"evidence_id": value, "document_id": value if request["strategy"] == "mmr" else "SYNTHETIC-document"}
                    for value in ids]
        artifact = self.runner.run_retrieval(self.cases, retrieve)
        for pair in artifact["results"]:
            left, right = (pair["runs"][variant]["metrics"] for variant in ("similarity", "mmr"))
            self.assertEqual((left["required_evidence_recall"], right["required_evidence_recall"]), (0.5, 1.0))
            self.assertEqual(left["missing_required_evidence_ids"], ["SYNTHETIC-c2"])
            self.assertEqual(right["missing_required_evidence_ids"], [])
            self.assertEqual(pair["observed_mmr_minus_similarity"]["required_evidence_recall"], 0.5)
            self.assertEqual(pair["observed_mmr_minus_similarity"]["distinct_document_count"], 1)
        self.assertEqual(artifact["acceptance_status"], "NOT_ASSESSED")

    def test_unverified_or_missing_references_do_not_invent_recall(self):
        for reference in (None, {"status": "UNVERIFIED", "required_evidence_ids": ["SYNTHETIC-c1"]},
                          {"status": "VERIFIED", "required_evidence_ids": []},
                          {"status": "VERIFIED", "required_evidence_ids": ["SYNTHETIC-c1", "SYNTHETIC-c1"]}):
            cases = [{**case, "reference": reference} for case in self.cases]
            artifact = self.runner.run_retrieval(cases, lambda request: [{"evidence_id": "SYNTHETIC-c1"}])
            metrics = artifact["results"][0]["runs"]["similarity"]["metrics"]
            self.assertIsNone(metrics["required_evidence_recall"])
            self.assertIsNone(metrics["document_diversity_ratio"])
            self.assertEqual(metrics["required_evidence_status"], "NOT_ASSESSED")

    def test_review_rejects_repository_inputs_and_outputs(self):
        artifact = self.answers()
        with self.assertRaises(ServiceError):
            review_evaluation(Path(__file__), self.base / "private")
        with self.assertRaises(ServiceError):
            review_evaluation(artifact, Path(__file__).resolve().parents[1] / "reviews")


class PublicEvaluationBoundaryTests(unittest.TestCase):
    def setUp(self):
        # The production classes and persistent local index are composed by the
        # existing authored HTTP fixture. No provider/network call is made here.
        from tests.test_public_rag_integration import PublicRagIntegrationTests, QUESTION
        from warranty.evaluation_cli import _Runtime
        self.fixture = PublicRagIntegrationTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        fixture = self.fixture
        # Match the production factory's direct bound resolver; no wire call is
        # needed, so the fixture's phase-observing wrapper is not installed.
        fixture.chat._public_source_resolver = fixture.public.source_resolver
        models = SimpleNamespace(chat=fixture.rag.model, embedding=fixture.embedding,
                                 public_context=fixture.public,
                                 metadata_embedding=fixture.rag.vectors.metadata_embeddings,
                                 document_item_ids=list(fixture.public.item_ids))
        self.runtime = object.__new__(_Runtime)
        self.runtime.app = SimpleNamespace(models=models, authorization=fixture.authorization,
                                           catalog=fixture.catalog)
        self.runtime.rag, self.runtime.model, self.runtime.embedding = fixture.rag, models.chat, fixture.embedding
        self.case = {"item_id": fixture.item, "question": QUESTION}

    def test_actual_public_bindings_use_local_metadata_without_metadata_export_grant(self):
        fixture = self.fixture
        item = fixture.catalog.get(fixture.item)
        with self.assertRaises(ServiceError):
            fixture.policy.require(item["source_id"], "external_process", actor="buyer",
                                   version_id=fixture.catalog.dataset_hash)
        self.assertTrue(self.runtime._public_metadata_local_only(self.case))
        self.assertEqual(fixture.requests, [])

    def test_public_scope_question_item_adapter_and_metadata_mismatches_fail_closed(self):
        runtime, fixture = self.runtime, self.fixture
        self.assertFalse(runtime._public_metadata_local_only({**self.case, "question": "Unapproved authored question"}))
        self.assertFalse(runtime._public_metadata_local_only({**self.case, "item_id": fixture.other}))
        changes = [(runtime.app.models, "metadata_embedding", fixture.embedding),
                   (runtime.app.models, "public_context", SimpleNamespace(item_ids=fixture.public.item_ids)),
                   (fixture.embedding, "approved_document_sha256s", ("0" * 64,)),
                   (fixture.chat, "_public_source_resolver", lambda *args: []),
                   (fixture.chat, "_original_guard", lambda *args: True),
                   (runtime.app.models, "document_item_ids", [fixture.other])]
        for target, name, value in changes:
            original = getattr(target, name)
            try:
                setattr(target, name, value)
                with self.subTest(attribute=name):
                    self.assertFalse(runtime._public_metadata_local_only(self.case))
            finally:
                setattr(target, name, original)
        self.assertEqual(fixture.requests, [])

    def test_changed_public_bundle_is_rejected_before_any_wire_attempt(self):
        from dataclasses import replace
        fixture = self.fixture
        path = fixture.settings.config_dir / "changed-public-bundle.json"
        path.write_text(fixture.bundle._value, encoding="utf-8")
        fixture.public.bundle = replace(fixture.bundle, path=path, file_sha256="0" * 64)
        self.assertFalse(self.runtime._public_metadata_local_only(self.case))
        self.assertEqual(fixture.requests, [])


if __name__ == "__main__":
    unittest.main()
