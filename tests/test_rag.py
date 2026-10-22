"""Executable local Chroma/LangGraph checks using authored synthetic documents."""
from contextlib import closing
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from warranty.catalog import Catalog
from warranty.config import Settings
from warranty.db import Database
from warranty.documents import ACTIONS
from warranty.documents import digest
from warranty.errors import ServiceError
from warranty.prompts import MockStructuredModel
from warranty.rag import RagService
from warranty.security import SourcePolicy
from warranty.vectorstore import MockEmbeddings


class SyntheticPromptStore:
    """Authored test envelope, never the private product prompt contents."""
    def render(self, kind, variables):
        return json.dumps({"policy": {"kind": kind, "fixture": "SYNTHETIC"}, "input": variables})

    def load(self, kind):
        return {"kind": kind, "fixture": "SYNTHETIC"}


SCOPE = {"country": "SYNTHETIC-COUNTRY", "plan_generation": "SYNTHETIC-PLAN",
         "payment_method": "SYNTHETIC-MONTHLY", "channel": "SYNTHETIC-CHANNEL",
         "document_type": "SERVICE_TERMS", "date_start": "2020-01-01", "date_end": "2030-12-31"}
INPUTS = {key: value for key, value in SCOPE.items() if not key.startswith("date_")}
INPUTS["enrollment_date"] = "2025-01-01"
BODY = b"<html><title>SYNTHETIC TERMS</title><h1>Transfer</h1><p>Transfer requires provider confirmation.</p><h2>Limit</h2><p>Only one transfer is permitted in this synthetic fixture.</p></html>"


def policy():
    source = {"source_id": "SYNTHETIC-DOC", "url": "https://example.org/synthetic-terms", "allowed_hosts": ["example.org"]}
    reviews = [{"source_id": "SYNTHETIC-DOC", "action": action, "status": "ALLOWED",
                "basis_type": "OTHER_RECORDED_BASIS", "basis_url": "https://example.org/",
                "reviewed_on": "2026-01-01", "scope": "Authored synthetic test fixture",
                "conditions": "Offline tests only", "user_scope": "test", "processing_environment": "LOCAL"}
               for action in ACTIONS]
    return SourcePolicy([source], reviews, {"manager": {"DATA_MANAGER"}})


class ExtractiveModel(MockStructuredModel):
    """An explicit MOCK emitting the exact supplied fixture clause."""
    model_id = "MOCK-extractive-fixture-v1"

    def generate(self, kind, prompt):
        if kind != "answer":
            return super().generate(kind, prompt)
        self.calls += 1
        inputs = json.loads(prompt)["input"]
        doc = next((entry for entry in inputs["evidence"] if entry["source_type"] != "metadata"), None)
        if doc:
            ids = [doc["evidence_id"]]
            text = doc["text"]
            return {"classification": "CONDITIONAL", "answer": f"{text} [{doc['number']}]",
                    "claims": [{"text": text, "evidence_ids": ids}],
                    "conditions": [{"text": text, "evidence_ids": ids}],
                    "missing_information": [], "conflicts": [], "cited_evidence_ids": ids}
        return {"classification": "TRANSFERABLE", "answer": "MOCK attempts unsupported approval.",
                "claims": [], "conditions": [], "missing_information": [], "conflicts": [], "cited_evidence_ids": []}


class RagTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="warranty-rag-test-")
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(Path(self.temp.name) / "data", Path(self.temp.name) / "config")
        self.settings.prepare()
        self.catalog = Catalog.synthetic()
        self.db = Database(self.settings.data_dir / "app.sqlite3")
        self.db.initialize()
        self.policy = policy()
        self.model = ExtractiveModel()
        self.rag = self.make_service()
        self.item = self.catalog.items[0]["item_id"]
        self.other = self.catalog.items[1]["item_id"]

    def make_service(self, **kwargs):
        service = RagService(self.catalog, self.db, self.settings, model=kwargs.pop("model", self.model),
            prompt_store=SyntheticPromptStore(), source_policy=self.policy, index_actor="manager", **kwargs)
        self.addCleanup(service.close)
        return service

    def document(self, item=None, body=BODY, role="SERVICE_TERMS"):
        doc = self.rag.registry.register_document("manager", source_id="SYNTHETIC-DOC", title="SYNTHETIC terms",
            canonical_url="https://example.org/synthetic-terms", document_type="HTML", is_synthetic=True)
        version = self.rag.registry.ingest_bytes("manager", doc["document_id"], body, applicability=SCOPE)
        self.rag.registry.link("manager", item or self.item, version["version_id"], role=role,
                               applicability_basis="Authored test document explicitly covers synthetic item.")
        return version

    def test_two_metadata_records_upsert_no_growth_and_genuine_graph(self):
        self.assertEqual(self.rag.vectors.meta.count(), 2)
        self.rag.vectors.index_metadata(self.rag._metadata_records)
        self.assertEqual(self.rag.vectors.meta.count(), 2)
        nodes = set(self.rag.graph.get_graph().nodes) - {"__start__", "__end__"}
        self.assertEqual(nodes, {"retrieve", "generate"})

    def test_server_downgrades_unsupported_mock_and_empty_checklist(self):
        result = self.rag.ask("buyer", self.item, "Can I transfer?", INPUTS)
        self.assertEqual(result["classification"], "NEEDS_INFORMATION")
        self.assertFalse(result["conditions_ok"])
        self.assertEqual(result["readiness"]["status"], "DOC_BLOCKED")
        self.assertIn("확인 가능한 근거", result["answer"])
        self.assertEqual(self.rag.checklist(self.item)["conditions"], [])
        self.assertEqual(self.rag.prepare("buyer", self.item)["failure_code"], "OWN_DOCUMENT_MISSING")

    def test_own_document_retained_when_selection_rejects(self):
        self.document()
        self.assertEqual(self.rag.prepare("buyer", self.item)["status"], "COMPLETED")
        original = self.model.generate
        def rejecting(kind, prompt):
            if kind == "selection":
                ids = [row["id"] for row in json.loads(prompt)["input"]["candidates"]]
                return {"selected_candidate_ids": [], "rejected": [{"id": value, "reason": "MOCK relevance rejection"} for value in ids], "conflicts": [], "missing_information": []}
            return original(kind, prompt)
        with patch.object(self.model, "generate", side_effect=rejecting):
            result = self.rag.ask("buyer", self.item, "Transfer requirement?", INPUTS)
        self.assertEqual(result["classification"], "CONDITIONAL")
        self.assertTrue(result["evidence"])
        self.assertTrue(result["evidence"][0]["url"].startswith("https://"))
        self.assertEqual(result["evidence"][0]["source_type"], "synthetic_document")

    def test_shared_document_has_separate_item_ids_and_no_premature_readiness(self):
        version = self.document()
        self.rag.prepare("buyer", self.item)
        self.rag.registry.link("manager", self.other, version["version_id"], role="SERVICE_TERMS", applicability_basis="Synthetic shared scope")
        before = self.rag.ask("buyer", self.other, "Transfer requirement?", INPUTS)
        self.assertEqual(before["readiness"]["status"], "DOC_BLOCKED")
        job = self.rag.prepare("buyer", self.other)
        self.assertEqual(job["status"], "COMPLETED")
        first = self.rag.ask("buyer", self.item, "Transfer requirement A?", INPUTS)
        second = self.rag.ask("buyer", self.other, "Transfer requirement B?", INPUTS)
        self.assertTrue(all(row["item_id"] == self.item for row in first["evidence"]))
        self.assertTrue(all(row["item_id"] == self.other for row in second["evidence"]))
        self.assertFalse(set(first["cited_evidence_ids"]) & set(second["cited_evidence_ids"]))

    def test_config_revision_cleanup_and_failed_new_revision_keeps_old(self):
        self.document(body=b"<h1>Long SYNTHETIC</h1><p>" + b"Transfer requires provider confirmation. " * 90 + b"</p>")
        first = self.rag.prepare("buyer", self.item)
        self.assertEqual(first["status"], "COMPLETED")
        changed = self.make_service(chunk_size=700, chunk_overlap=100)
        with patch.object(changed.vectors, "stage_documents", side_effect=ServiceError("INDEX_EMPTY", "Synthetic stage failure", 503)):
            failed = changed.prepare("buyer", self.item)
        self.assertEqual(failed["status"], "BLOCKED")
        self.assertEqual(self.rag._active()["revision"], first["revision"])
        second = changed.prepare("buyer", self.item)
        self.assertEqual(second["status"], "COMPLETED")
        self.assertNotEqual(first["revision"], second["revision"])
        rows = changed.vectors.docs.get(include=["metadatas"])
        self.assertEqual({row["revision"] for row in rows["metadatas"]}, {second["revision"]})

    def test_topk_and_mmr_keep_one_metadata_context(self):
        self.document(body=b"<h1>Transfer</h1>" + b"Transfer one. " * 350)
        self.rag.prepare("buyer", self.item)
        for strategy in ("similarity", "mmr"):
            for top_k in (3, 6, 10):
                result = self.rag.ask("buyer", self.item, f"Transfer {strategy} {top_k}?", INPUTS, top_k, strategy)
                self.assertFalse(result["errors"], result["errors"])
                self.assertEqual(sum(value.startswith("meta-") for value in result["retrieved_evidence_ids"]), 1)
                self.assertLessEqual(len(result["retrieved_evidence_ids"]), top_k + 1)

    def test_foreign_evidence_and_fabricated_citation_rejected(self):
        self.document()
        self.rag.prepare("buyer", self.item)
        original = self.rag.vectors.search
        def foreign(*args, **kwargs):
            rows = original(*args, **kwargs)
            for row in rows:
                row["item_id"] = "FOREIGN"
                row["revision"] = "FOREIGN"
            return rows
        with patch.object(self.rag.vectors, "search", side_effect=foreign):
            result = self.rag.ask("buyer", self.item, "Foreign boundary?", INPUTS)
        self.assertEqual(result["classification"], "NEEDS_INFORMATION")
        self.assertEqual(result["evidence"], [])
        original_model = self.model.generate
        def bad_number(kind, prompt):
            output = original_model(kind, prompt)
            if kind == "answer":
                output["answer"] += " [999]"
            return output
        with patch.object(self.model, "generate", side_effect=bad_number):
            result = self.rag.ask("buyer", self.item, "Fabricated citation?", INPUTS)
        self.assertEqual(result["errors"][0]["code"], "MODEL_EVIDENCE_INVALID")
        self.assertFalse(result["conditions_ok"])

    def test_applicability_conflicts_and_metadata_condition_abuse(self):
        self.document()
        self.rag.prepare("buyer", self.item)
        missing = self.rag.ask("buyer", self.item, "Missing applicability?", {})
        self.assertEqual(missing["classification"], "NEEDS_INFORMATION")
        wrong = self.rag.ask("buyer", self.item, "Wrong contract?", {**INPUTS, "enrollment_date": "2010-01-01"})
        self.assertEqual(wrong["classification"], "NEEDS_INFORMATION")
        self.assertTrue(wrong["conflicts"])
        self.assertFalse(self.rag.validate_review("buyer", self.item, wrong["review_id"])["conditions_ok"])
        original = self.model.generate
        def metadata_condition(kind, prompt):
            if kind != "answer":
                return original(kind, prompt)
            meta = json.loads(prompt)["input"]["evidence"][0]
            return {"classification": "TRANSFERABLE", "answer": "Metadata allows transfer [1]",
                    "claims": [], "conditions": [{"text": "Transfer is allowed", "evidence_ids": [meta["evidence_id"]]}],
                    "missing_information": [], "conflicts": [], "cited_evidence_ids": [meta["evidence_id"]]}
        with patch.object(self.model, "generate", side_effect=metadata_condition):
            abused = self.rag.ask("buyer", self.item, "Metadata abuse?", INPUTS)
        self.assertFalse(abused["conditions_ok"])
        self.assertEqual(abused["errors"][0]["code"], "MODEL_OUTPUT_INVALID")

    def test_history_jobs_and_review_are_owner_item_bound(self):
        self.document()
        job = self.rag.prepare("buyer", self.item)
        first = self.rag.ask("buyer", self.item, "Question A", INPUTS)
        self.rag.ask("buyer", self.other, "Question B")
        self.rag.ask("seller", self.item, "Question C")
        self.assertEqual([row["question"] for row in self.rag.history("buyer", self.item)], ["Question A"])
        with self.assertRaises(ServiceError):
            self.rag.job("seller", job["job_id"])
        with self.assertRaises(ServiceError):
            self.rag.validate_review("seller", self.item, first["review_id"])
        review = self.rag.validate_review("buyer", self.item, first["review_id"])
        self.assertTrue(review["applicability_ok"])
        self.assertFalse(review["conditions_ok"])
        self.assertTrue(review["document_hashes"])

    def test_review_revocation_applies_even_with_cached_answer(self):
        self.document()
        self.rag.prepare("buyer", self.item)
        result = self.rag.ask("buyer", self.item, "Cached transfer?", INPUTS)
        self.assertTrue(result["applicability_ok"])
        self.assertFalse(result["conditions_ok"])
        blocked = {**self.policy.require("SYNTHETIC-DOC", "display_excerpt"), "status": "BLOCKED"}
        self.policy.set_review("manager", blocked)
        repeated = self.rag.ask("buyer", self.item, "Cached transfer?", INPUTS)
        self.assertEqual(repeated["classification"], "NEEDS_INFORMATION")
        self.assertFalse(self.rag.validate_review("buyer", self.item, result["review_id"])["conditions_ok"])
        historic = self.rag.history("buyer", self.item)[0]["result"]
        self.assertEqual(historic["evidence"], [])
        self.assertEqual(historic["cited_evidence_ids"], [])
        self.assertNotIn("Transfer requires", historic["answer"])
        with closing(self.db.connect()) as connection:
            raw = json.loads(connection.execute("SELECT result_json FROM chat_history ORDER BY message_id LIMIT 1").fetchone()[0])
        self.assertTrue(raw["evidence"])  # immutable server record is preserved

    def test_answer_cache_skips_generation_only_not_permission_checks(self):
        self.document()
        self.rag.prepare("buyer", self.item)
        self.rag.ask("buyer", self.item, "Same question", INPUTS)
        calls = self.model.calls
        self.rag.ask("buyer", self.item, "Same question", INPUTS)
        self.assertEqual(self.model.calls - calls, 2)  # query and candidate-selection only
        with closing(self.db.connect()) as connection:
            self.assertGreater(connection.execute("SELECT COUNT(*) FROM source_action_reviews").fetchone()[0], 0)

    def test_different_questions_reuse_same_query_collection_identity(self):
        self.document()
        self.rag.prepare("buyer", self.item)
        with patch.object(self.rag.registry, "sections", wraps=self.rag.registry.sections) as parser:
            first = self.rag.ask("buyer", self.item, "What is the transfer limit?", INPUTS)
            second = self.rag.ask("buyer", self.item, "Which transfer documents are needed?", INPUTS)
        self.assertEqual(first["queries"], second["queries"])
        self.assertEqual(first["collection_cache_hits"], 0)
        self.assertEqual(second["collection_cache_hits"], 1)
        self.assertEqual(parser.call_count, 0)  # both reuse already indexed local body
        with closing(self.db.connect()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM rag_cache WHERE cache_kind='collection'").fetchone()[0], 1)

    def test_identical_clause_in_different_versions_preserves_conflicting_evidence(self):
        first = self.document(body=b"<h1>Shared</h1>Transfer requires confirmation.<h2>Limit</h2>Only one transfer is allowed.")
        second = self.document(body=b"<h1>Shared</h1>Transfer requires confirmation.<h2>Limit</h2>No transfer is allowed.")
        self.rag.prepare("buyer", self.item)
        retrieved = self.rag.retrieve_evidence("buyer", self.item, "Transfer confirmation", INPUTS, 10)
        clauses = [row for row in retrieved["evidence"] if row["text"] == "Transfer requires confirmation."]
        self.assertEqual({row["version_id"] for row in clauses}, {first["version_id"], second["version_id"]})
        self.assertEqual(len(clauses), 2)
        text = " ".join(row["text"] for row in retrieved["evidence"] if row["source_type"] != "metadata")
        self.assertIn("Only one transfer is allowed.", text)
        self.assertIn("No transfer is allowed.", text)

    def test_supported_negative_review_is_verified_without_payment_eligibility(self):
        self.document(body=b"<h1>SYNTHETIC restriction</h1>Transfer is not permitted.")
        self.rag.prepare("buyer", self.item)
        original = self.model.generate
        def negative(kind, prompt):
            output = original(kind, prompt)
            if kind == "answer":
                output["classification"] = "NOT_TRANSFERABLE"
                output["conditions"] = []
            return output
        with patch.object(self.model, "generate", side_effect=negative):
            result = self.rag.ask("buyer", self.item, "Is transfer prohibited?", INPUTS)
        verified = self.rag.validate_review("buyer", self.item, result["review_id"])
        self.assertEqual(verified["classification"], "NOT_TRANSFERABLE")
        self.assertTrue(verified["applicability_ok"])
        self.assertFalse(verified["conditions_ok"])

    def test_supported_unconditional_review_needs_no_invented_conditions(self):
        self.document(body=b"<h1>SYNTHETIC permission</h1>Transfer is permitted without additional conditions.")
        self.rag.prepare("buyer", self.item)
        original = self.model.generate
        def unconditional(kind, prompt):
            output = original(kind, prompt)
            if kind == "answer":
                output["classification"] = "TRANSFERABLE"
                output["conditions"] = []
            return output
        with patch.object(self.model, "generate", side_effect=unconditional):
            result = self.rag.ask("buyer", self.item, "Is transfer unconditionally permitted?", INPUTS)
        verified = self.rag.validate_review("buyer", self.item, result["review_id"])
        self.assertEqual(verified["classification"], "TRANSFERABLE")
        self.assertTrue(verified["conditions_ok"])

    def test_conditional_clauses_are_not_fulfilment_and_ids_are_server_bound(self):
        self.document()
        self.rag.prepare("buyer", self.item)
        first = self.rag.ask("buyer", self.item, "Which conditions apply?", INPUTS)
        second = self.rag.ask("buyer", self.item, "Which conditions apply?", INPUTS)
        self.assertEqual(first["classification"], "CONDITIONAL")
        self.assertTrue(first["applicability_ok"])
        self.assertFalse(first["conditions_ok"])
        self.assertTrue(first["conditions"])
        for condition in first["conditions"]:
            self.assertEqual(condition["condition_id"], "condition-" + digest({
                "text": condition["text"], "evidence_ids": sorted(condition["evidence_ids"])}))
        self.assertEqual(first["required_condition_ids"], sorted({row["condition_id"] for row in first["conditions"]}))
        self.assertEqual(first["required_condition_ids"], second["required_condition_ids"])
        self.assertNotEqual(first["review_id"], second["review_id"])
        self.assertFalse(self.rag.validate_review("buyer", self.item, first["review_id"])["conditions_ok"])

    def test_transferable_label_with_clauses_cannot_bypass_fulfilment(self):
        self.document()
        self.rag.prepare("buyer", self.item)
        original = self.model.generate
        def mislabeled(kind, prompt):
            output = original(kind, prompt)
            if kind == "answer":
                output["classification"] = "TRANSFERABLE"
            return output
        with patch.object(self.model, "generate", side_effect=mislabeled):
            result = self.rag.ask("buyer", self.item, "Mislabeled unconditional answer?", INPUTS)
        self.assertTrue(result["conditions"])
        self.assertEqual(result["classification"], "CONDITIONAL")
        self.assertFalse(result["conditions_ok"])
        # Previously persisted flags cannot turn clauses into attestations.
        with self.db.transaction() as connection:
            legacy = {**result, "classification": "TRANSFERABLE", "conditions_ok": True}
            connection.execute("UPDATE chat_history SET result_json=? WHERE user_id=? AND item_id=?",
                (json.dumps(legacy), "buyer", self.item))
        projected = self.rag.validate_review("buyer", self.item, result["review_id"])
        self.assertEqual(projected["classification"], "CONDITIONAL")
        self.assertFalse(projected["conditions_ok"])

    def test_checklist_populates_only_matching_actor_item_applicability_revision(self):
        self.document()
        self.rag.prepare("buyer", self.item)
        seller = self.rag.ask("seller", self.item, "Seller-specific saved question", INPUTS)
        self.assertEqual(self.rag.checklist(self.item, INPUTS, user_id="buyer")["conditions"], [])
        own = self.rag.ask("buyer", self.item, "Buyer-specific saved question", INPUTS)
        # A later review for another contract does not replace a matching one.
        self.rag.ask("buyer", self.item, "Different contract", {**INPUTS, "country": "OTHER"})
        table = self.rag.checklist(self.item, INPUTS, user_id="buyer")
        self.assertEqual(table["review_id"], own["review_id"])
        self.assertEqual(table["conditions"], own["conditions"])
        self.assertEqual(table["evidence"], own["evidence"])
        self.assertEqual(table["required_condition_ids"], own["required_condition_ids"])
        self.assertEqual(table["classification"], "CONDITIONAL")
        self.assertFalse(table["conditions_ok"])
        self.assertTrue(table["documents"])
        self.assertEqual(self.rag.checklist(self.item, INPUTS)["conditions"], [])
        self.assertEqual(self.rag.checklist(self.item, INPUTS, user_id="seller", review_id=own["review_id"])["conditions"], [])
        self.assertEqual(self.rag.checklist(self.other, INPUTS, user_id="buyer", review_id=own["review_id"])["conditions"], [])
        self.assertEqual(self.rag.checklist(self.item, {}, user_id="buyer", review_id=own["review_id"])["conditions"], [])
        self.assertEqual(self.rag.checklist(self.item, INPUTS, user_id="seller")["review_id"], seller["review_id"])
        changed = self.make_service(chunk_size=700, chunk_overlap=100)
        changed.prepare("buyer", self.item)
        self.assertEqual(changed.checklist(self.item, INPUTS, user_id="buyer", review_id=own["review_id"])["conditions"], [])

    def test_saved_checklist_rechecks_source_display_approval(self):
        self.document()
        self.rag.prepare("buyer", self.item)
        saved = self.rag.ask("buyer", self.item, "Saved source conditions", INPUTS)
        self.assertTrue(self.rag.checklist(self.item, INPUTS, user_id="buyer")["conditions"])
        self.policy.set_review("manager", {**self.policy.require("SYNTHETIC-DOC", "display_excerpt"), "status": "BLOCKED"})
        table = self.rag.checklist(self.item, INPUTS, user_id="buyer", review_id=saved["review_id"])
        self.assertEqual(table["conditions"], [])
        self.assertEqual(table["evidence"], [])
        self.assertEqual(table["required_condition_ids"], [])
        self.assertFalse(table["conditions_ok"])
        self.assertEqual(table["classification"], "NEEDS_INFORMATION")

    def test_remote_embedding_preflights_and_revocation_precede_adapter_calls(self):
        class SpyEmbeddings(MockEmbeddings):
            name = "MOCK-remote-location-spy-no-network"
            is_remote = True
            calls = 0

            def embed(self, texts):
                self.calls += 1
                return super().embed(texts)

        spy = SpyEmbeddings()
        disabled = self.make_service(embeddings=spy)
        self.assertEqual(disabled.metadata_count, 0)
        with self.assertRaises(ServiceError) as blocked:
            disabled.prepare_metadata("manager", item_ids=[self.item])
        self.assertEqual(blocked.exception.code, "MODEL_REMOTE_BLOCKED")
        self.assertEqual(spy.calls, 0)
        metadata_source = self.catalog.items[0]["source_id"]
        self.policy.register_source("manager", {"source_id": metadata_source, "url": self.catalog.sources[metadata_source]["url"]})
        review = {**self.policy.require("SYNTHETIC-DOC", "external_process"), "source_id": metadata_source}
        self.policy.set_review("manager", review)
        remote_settings = Settings(self.settings.data_dir, self.settings.config_dir, remote_models=True)
        remote = RagService(self.catalog, self.db, remote_settings, model=self.model, prompt_store=SyntheticPromptStore(),
            source_policy=self.policy, index_actor="manager", embeddings=spy)
        self.addCleanup(remote.close)
        self.document()
        remote.prepare_metadata("manager", item_ids=[self.item])
        self.assertEqual(remote.prepare("buyer", self.item)["status"], "COMPLETED")
        calls = spy.calls
        self.policy.set_review("manager", {**review, "status": "BLOCKED"})
        result = remote.ask("buyer", self.item, "Must never reach query embed", INPUTS)
        self.assertEqual(spy.calls, calls)
        self.assertEqual(result["classification"], "NEEDS_INFORMATION")
        self.assertEqual(result["errors"][0]["code"], "SOURCE_BLOCKED")


if __name__ == "__main__":
    unittest.main()
