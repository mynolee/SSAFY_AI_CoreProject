"""Server guards for required documents and ambiguous contract versions.

Every document and model response here is an authored offline fixture.
"""
import json
import unittest
from unittest.mock import patch

import test_rag as fixtures
import test_rag_collection_flow as flow


class RagRequirementTests(unittest.TestCase):
    setUp = fixtures.RagTests.setUp
    make_service = fixtures.RagTests.make_service
    document = fixtures.RagTests.document

    def unconditional_terms(self, kind, prompt):
        if kind != "answer":
            return fixtures.MockStructuredModel.generate(self.model, kind, prompt)
        evidence = json.loads(prompt)["input"]["evidence"]
        term = next((row for row in evidence if row["role"] == "SERVICE_TERMS"), None)
        if term is None:
            return fixtures.MockStructuredModel.generate(self.model, kind, prompt)
        return {"classification": "TRANSFERABLE", "answer": f"{term['text']} [{term['number']}]",
                "claims": [{"text": term["text"], "evidence_ids": [term["evidence_id"]]}],
                "conditions": [], "missing_information": [], "conflicts": [],
                "cited_evidence_ids": [term["evidence_id"]]}

    def test_new_unindexed_own_document_blocks_fresh_and_saved_reviews(self):
        self.document(body=b"<h1>SYNTHETIC terms</h1>Transfer is permitted.")
        self.rag.prepare("buyer", self.item)
        with patch.object(self.model, "generate", side_effect=self.unconditional_terms):
            approved = self.rag.ask("buyer", self.item, "Original contract", fixtures.INPUTS)
            self.assertTrue(approved["conditions_ok"])
            self.document(body=b"<h1>SYNTHETIC description</h1>Additional item description.",
                          role="PRIMARY_DESCRIPTION")
            current = self.rag.ask("buyer", self.item, "All required documents?", fixtures.INPUTS)
        self.assertEqual(current["classification"], "NEEDS_INFORMATION")
        self.assertEqual(current["readiness"]["status"], "DOC_BLOCKED")
        self.assertFalse(current["conditions_ok"])
        saved = self.rag.validate_review("buyer", self.item, approved["review_id"])
        self.assertFalse(saved["applicability_ok"])
        self.assertFalse(saved["conditions_ok"])
        self.assertEqual(saved["readiness"]["status"], "DOC_BLOCKED")
        self.assertIn("own_document:approved_indexed_body_required", saved["missing_information"])
        table = self.rag.checklist(self.item, fixtures.INPUTS, user_id="buyer", review_id=approved["review_id"])
        self.assertFalse(table["conditions_ok"])
        self.assertEqual(table["readiness"]["status"], "DOC_BLOCKED")
        self.assertIn("own_document:approved_indexed_body_required", table["missing_information"])

    def test_uncited_required_source_revocation_cannot_be_masked_by_cited_terms(self):
        self.document(body=b"<h1>SYNTHETIC terms</h1>Transfer is permitted.")
        description = self.document(body=b"<h1>SYNTHETIC description</h1>Item description.",
                                    role="PRIMARY_DESCRIPTION")
        self.rag.prepare("buyer", self.item)
        with patch.object(self.model, "generate", side_effect=self.unconditional_terms):
            approved = self.rag.ask("buyer", self.item, "Original required documents", fixtures.INPUTS)
            self.assertTrue(approved["conditions_ok"])
            self.assertNotIn(description["version_id"], {row.get("version_id") for row in approved["evidence"]})
            review = self.policy.require("SYNTHETIC-DOC", "display_excerpt")
            self.policy.set_review("manager", {**review, "version_id": description["version_id"], "status": "BLOCKED"})
            current = self.rag.ask("buyer", self.item, "After description approval changed", fixtures.INPUTS)
        self.assertEqual(current["classification"], "NEEDS_INFORMATION")
        self.assertEqual(current["readiness"]["status"], "DOC_BLOCKED")
        saved = self.rag.validate_review("buyer", self.item, approved["review_id"])
        self.assertFalse(saved["conditions_ok"])
        self.assertEqual(saved["classification"], "NEEDS_INFORMATION")
        self.assertEqual(saved["readiness"]["status"], "DOC_BLOCKED")

    def test_unfetched_required_target_blocks_existing_document_and_saved_review(self):
        self.document(body=b"<h1>SYNTHETIC terms</h1>Transfer is permitted.")
        self.rag.prepare("buyer", self.item)
        with patch.object(self.model, "generate", side_effect=self.unconditional_terms):
            approved = self.rag.ask("buyer", self.item, "Existing terms", fixtures.INPUTS)
            document = self.rag.registry.register_document("manager", source_id="SYNTHETIC-DOC",
                title="SYNTHETIC required description", canonical_url="https://example.org/synthetic-terms",
                document_type="HTML", is_synthetic=True)
            self.rag.registry.register_target("manager", self.item, document["document_id"],
                role="PRIMARY_DESCRIPTION", applicability=fixtures.SCOPE,
                applicability_basis="Authored pending required description target.")
            current = self.rag.ask("buyer", self.item, "Pending own description", fixtures.INPUTS)
        self.assertEqual(current["readiness"]["status"], "DOC_BLOCKED")
        self.assertEqual(current["classification"], "NEEDS_INFORMATION")
        self.assertFalse(self.rag.validate_review("buyer", self.item, approved["review_id"])["conditions_ok"])
        self.assertEqual(self.rag.prepare("buyer", self.item)["failure_code"], "OWN_DOCUMENT_MISSING")

    def test_overlapping_versions_are_ambiguous_even_when_model_omits_conflict(self):
        first = self.document(body=b"<h1>SYNTHETIC terms</h1>Transfer is permitted.")
        second = self.document(body=b"<h1>SYNTHETIC terms</h1>Transfer is prohibited.")
        self.rag.prepare("buyer", self.item)
        context = self.rag.retrieve_evidence("buyer", self.item, "Compare transfer terms", fixtures.INPUTS, 10)
        self.assertEqual({row["version_id"] for row in context["evidence"] if row["role"] == "SERVICE_TERMS"},
                         {first["version_id"], second["version_id"]})
        with patch.object(self.model, "generate", side_effect=self.unconditional_terms):
            answer = self.rag.ask("buyer", self.item, "Select the applicable contract", fixtures.INPUTS, 10)
        self.assertEqual(answer["classification"], "NEEDS_INFORMATION")
        self.assertFalse(answer["conditions_ok"])
        self.assertTrue(any("Multiple linked document versions" in conflict for conflict in answer["conflicts"]))
        self.assertEqual(self.rag.validate_review("buyer", self.item, answer["review_id"])["classification"],
                         "NEEDS_INFORMATION")

    def test_new_overlapping_version_invalidates_saved_review_before_reindexing(self):
        self.document(body=b"<h1>SYNTHETIC terms</h1>Transfer is permitted.")
        self.rag.prepare("buyer", self.item)
        with patch.object(self.model, "generate", side_effect=self.unconditional_terms):
            original = self.rag.ask("buyer", self.item, "Old single version", fixtures.INPUTS)
        self.assertTrue(original["conditions_ok"])
        self.document(body=b"<h1>SYNTHETIC terms</h1>Transfer is prohibited.")
        saved = self.rag.validate_review("buyer", self.item, original["review_id"])
        self.assertFalse(saved["conditions_ok"])
        self.assertEqual(saved["classification"], "NEEDS_INFORMATION")
        self.assertTrue(saved["conflicts"])


class PreparationRetryTests(unittest.TestCase):
    setUp = fixtures.RagTests.setUp
    make_service = fixtures.RagTests.make_service
    document = fixtures.RagTests.document
    configure = flow.CollectionFlowTests.configure
    target = flow.CollectionFlowTests.target

    def queue_job(self):
        from warranty.jobs import DocumentJobQueue
        self.configure()
        self.target()
        self.rag.document_jobs = DocumentJobQueue(self.db, self.catalog, self.rag.run_prepare,
            manager_check=lambda actor: actor == "manager", auto_start=False)
        return self.rag.enqueue_prepare("buyer", self.item, fixtures.INPUTS, "Retry registered document")

    def test_explicit_job_retry_bypasses_failed_fetch_cache_immediately(self):
        job = self.queue_job()
        self.fetcher.fail = True
        self.rag.document_jobs.run_pending(limit=1)
        self.assertEqual(self.rag.job("buyer", job["job_id"])["failure_code"], "FETCH_FAILED")
        self.assertEqual(len(self.fetcher.calls), 1)
        self.fetcher.fail = False
        self.rag.retry_prepare("buyer", job["job_id"])
        self.rag.document_jobs.run_pending(limit=1)
        complete = self.rag.job("buyer", job["job_id"])
        self.assertEqual(complete["status"], "COMPLETED")
        self.assertEqual(complete["attempt_no"], 2)
        self.assertEqual(len(self.fetcher.calls), 2)

    def test_job_retry_after_index_failure_reuses_successful_body(self):
        from warranty.errors import ServiceError
        job = self.queue_job()
        with patch.object(self.rag.vectors, "stage_documents",
                          side_effect=ServiceError("INDEX_EMPTY", "SYNTHETIC stage failure", 503, True)):
            self.rag.document_jobs.run_pending(limit=1)
        self.assertEqual(self.rag.job("buyer", job["job_id"])["failure_code"], "INDEX_EMPTY")
        self.assertEqual(len(self.fetcher.calls), 1)
        self.rag.retry_prepare("buyer", job["job_id"])
        self.rag.document_jobs.run_pending(limit=1)
        self.assertEqual(self.rag.job("buyer", job["job_id"])["status"], "COMPLETED")
        self.assertEqual(len(self.fetcher.calls), 1)


if __name__ == "__main__":
    unittest.main()
