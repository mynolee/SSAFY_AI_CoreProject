"""Offline integration boundaries using authored, explicitly synthetic documents."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from warranty.catalog import Catalog
from warranty.config import Settings
from warranty.db import Database
from warranty.documents import ACTIONS
from warranty.errors import ServiceError
from warranty.prompts import MockStructuredModel
from warranty.rag import RagService
from warranty.security import SourcePolicy
from warranty.transactions import TradeService, verify_accounting
from warranty.worker import SettlementWorker


SOURCE_ID = "SYNTHETIC-TRADE-BOUNDARY"
SOURCE_URL = "https://example.org/synthetic-trade-boundary"
SCOPE = {"country": "SYNTHETIC-COUNTRY", "plan_generation": "SYNTHETIC-PLAN",
         "payment_method": "SYNTHETIC-MONTHLY", "channel": "SYNTHETIC-CHANNEL",
         "document_type": "SERVICE_TERMS", "date_start": "2020-01-01", "date_end": "2030-12-31"}
INPUTS = {key: value for key, value in SCOPE.items() if not key.startswith("date_")}
INPUTS["enrollment_date"] = "2025-01-01"
BODY = b"<h1>SYNTHETIC CONDITIONS</h1><p>SYNTHETIC transfer requires an explicit party attestation.</p>"


class BoundaryPromptStore:
    def render(self, kind, variables):
        return json.dumps({"policy": {"kind": kind, "is_synthetic": True}, "input": variables})

    def load(self, kind):
        return {"kind": kind, "is_synthetic": True}


class BoundaryModel(MockStructuredModel):
    """Quotes only the supplied synthetic fixture; cannot authorize payment."""
    model_id = "MOCK-synthetic-trade-boundary-v1"

    def generate(self, kind, prompt):
        if kind != "answer":
            return super().generate(kind, prompt)
        self.calls += 1
        evidence = json.loads(prompt)["input"]["evidence"]
        document = next((entry for entry in evidence if entry["source_type"] != "metadata"), None)
        if document is None:
            return {"classification": "TRANSFERABLE", "answer": "SYNTHETIC unsupported approval attempt.",
                    "claims": [], "conditions": [], "missing_information": [], "conflicts": [],
                    "cited_evidence_ids": []}
        identifiers = [document["evidence_id"]]
        text = document["text"]
        return {"classification": "CONDITIONAL", "answer": f"{text} [{document['number']}]",
                "claims": [{"text": text, "evidence_ids": identifiers}],
                "conditions": [{"text": text, "evidence_ids": identifiers}],
                "missing_information": [], "conflicts": [], "cited_evidence_ids": identifiers}


class LiveTradeBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="synthetic-trade-boundary-")
        self.addCleanup(self.temporary.cleanup)
        self.settings = Settings(Path(self.temporary.name) / "data", Path(self.temporary.name) / "config")
        self.settings.prepare()
        self.db = Database(self.settings.data_dir / "app.sqlite3")
        self.db.initialize()
        self.addCleanup(self.db.close)
        self.catalog = Catalog.synthetic()
        self.item = self.catalog.items[0]["item_id"]
        reviews = [{"source_id": SOURCE_ID, "action": action, "status": "ALLOWED",
                    "basis_type": "OTHER_RECORDED_BASIS", "basis_url": SOURCE_URL,
                    "reviewed_on": "2026-01-01", "scope": "Authored synthetic offline fixture only",
                    "conditions": "No real source, permission or provider action is represented",
                    "user_scope": "synthetic-test", "processing_environment": "LOCAL"}
                   for action in ACTIONS]
        self.policy = SourcePolicy([{"source_id": SOURCE_ID, "url": SOURCE_URL,
                                     "is_synthetic": True}], reviews, {"manager": {"DATA_MANAGER"}})
        self.rag = self.make_rag()
        self.trades = TradeService(self.db, review_validator=self.rag.validate_review)

    def make_rag(self, **kwargs):
        service = RagService(self.catalog, self.db, self.settings, model=BoundaryModel(),
                             prompt_store=BoundaryPromptStore(), source_policy=self.policy,
                             index_actor="manager", **kwargs)
        self.addCleanup(service.close)
        return service

    def register_body(self, body=BODY):
        document = self.rag.registry.register_document("manager", source_id=SOURCE_ID,
            title="SYNTHETIC offline terms", canonical_url=SOURCE_URL, document_type="HTML",
            is_synthetic=True)
        version = self.rag.registry.ingest_bytes("manager", document["document_id"], body, applicability=SCOPE)
        self.rag.registry.link("manager", self.item, version["version_id"], role="SERVICE_TERMS",
                               applicability_basis="Explicit authored synthetic item coverage")
        return version

    def review(self):
        self.register_body()
        self.assertEqual("COMPLETED", self.rag.prepare("buyer", self.item, INPUTS)["status"])
        review = self.rag.ask("buyer", self.item, "What must the parties attest?", INPUTS)
        self.assertEqual("CONDITIONAL", review["classification"])
        self.assertTrue(review["applicability_ok"])
        self.assertFalse(review["conditions_ok"])
        self.assertTrue(review["required_condition_ids"])
        self.assertTrue(all(entry["source_type"] == "synthetic_document" for entry in review["evidence"]))
        self.assertEqual(0, self.rag.corpus_summary()["official_document_body_count"])
        return review

    def change(self, trade, action, payload=None, actor="buyer"):
        current = self.trades.get(actor, trade["trade_id"])
        return self.trades.mutate(actor, trade["trade_id"], action, payload or {}, uuid4().hex, current["version"])

    def agree(self, trade):
        current = self.trades.get("buyer", trade["trade_id"])
        target = {"revision": current["contract"]["revision"], "hash": current["contract"]["hash"]}
        self.change(trade, "contract_consent", target)
        return self.change(trade, "contract_consent", target, "seller")

    def funded(self):
        trade = self.trades.create("buyer", "buyer", "seller", self.item, 500, 50)
        trade = self.agree(trade)
        return self.change(trade, "deposit")

    def adopt(self, trade, review):
        trade = self.change(trade, "adopt_review", {"review_id": review["review_id"]})
        trade = self.agree(trade)
        return self.change(trade, "receipt", {"contract_revision": trade["contract_revision"]})

    def attest(self, trade, *, both=True):
        current = self.trades.get("buyer", trade["trade_id"])
        checks = [{"condition_id": value, "confirmed": True}
                  for value in current["review"]["required_condition_ids"]]
        trade = self.change(trade, "evidence", {"references": ["SYNTHETIC:party-attestation"],
                                                "condition_checks": checks})
        target = {"evidence_id": trade["evidence"]["evidence_id"], "hash": trade["evidence"]["hash"]}
        trade = self.change(trade, "confirm_evidence", target)
        return self.change(trade, "confirm_evidence", target, "seller") if both else trade

    def proposal(self, trade, component="W", *, refund=False):
        amount = 50 if component == "W" else 500
        trade = self.change(trade, "proposal", {"allocations": [{"component": component,
            "pay_won": 0 if refund else amount, "refund_won": amount if refund else 0}],
            "refund_reason": "mutual_agreement"})
        proposal = trade["proposals"][-1]
        target = {"proposal_id": proposal["proposal_id"], "revision": proposal["revision"], "hash": proposal["hash"]}
        self.change(trade, "proposal_consent", target)
        return self.change(trade, "proposal_consent", target, "seller"), proposal

    def reserve(self, trade, proposal):
        return self.change(trade, "reserve", {"proposal_id": proposal["proposal_id"], "revision": proposal["revision"]})

    def snapshot(self, trade):
        current = self.trades.get("buyer", trade["trade_id"])
        with closing(self.db.connect()) as connection:
            verify_accounting(connection, trade["trade_id"])
            self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())
        return {key: current[key] for key in ("version", "balances", "operations", "groups", "ledger")}

    def assert_blocked_without_reservation(self, trade, proposal):
        before = self.snapshot(trade)
        with self.assertRaises(ServiceError):
            self.reserve(trade, proposal)
        self.assertEqual(before, self.snapshot(trade))

    def revoke(self, action="display_excerpt"):
        self.policy.set_review("manager", {**self.policy.require(SOURCE_ID, action), "status": "BLOCKED"})

    def test_blocked_own_documents_and_model_approval_cannot_authorize_w(self):
        job = self.rag.prepare("buyer", self.item, INPUTS)
        self.assertEqual("BLOCKED", job["status"])
        result = self.rag.ask("buyer", self.item, "Approve W without documents", INPUTS)
        self.assertEqual("NEEDS_INFORMATION", result["classification"])
        trade = self.adopt(self.funded(), result)
        trade, proposal = self.proposal(trade)
        self.assert_blocked_without_reservation(trade, proposal)

    def test_source_revoked_before_adoption_cannot_authorize_w(self):
        result = self.review()
        self.revoke()
        self.assertEqual("NEEDS_INFORMATION", self.rag.validate_review("buyer", self.item, result["review_id"])["classification"])
        trade = self.adopt(self.funded(), result)
        trade, proposal = self.proposal(trade)
        self.assert_blocked_without_reservation(trade, proposal)

    def test_old_revision_before_adoption_cannot_authorize_w(self):
        result = self.review()
        changed = self.make_rag(chunk_size=700, chunk_overlap=100)
        job = changed.prepare("buyer", self.item, INPUTS)
        self.assertEqual("COMPLETED", job["status"])
        self.assertNotEqual(result["revision"], job["revision"])
        self.assertEqual("NEEDS_INFORMATION", self.rag.validate_review("buyer", self.item, result["review_id"])["classification"])
        trade = self.adopt(self.funded(), result)
        trade, proposal = self.proposal(trade)
        self.assert_blocked_without_reservation(trade, proposal)

    def test_source_revoked_after_adoption_blocks_new_w_reservation(self):
        result = self.review()
        trade = self.attest(self.adopt(self.funded(), result))
        trade, proposal = self.proposal(trade)
        self.revoke()
        self.assertEqual("NEEDS_INFORMATION", self.rag.validate_review("buyer", self.item, result["review_id"])["classification"])
        self.assert_blocked_without_reservation(trade, proposal)

    def test_revision_changed_after_adoption_blocks_new_w_reservation(self):
        result = self.review()
        trade = self.attest(self.adopt(self.funded(), result))
        trade, proposal = self.proposal(trade)
        changed = self.make_rag(chunk_size=700, chunk_overlap=100)
        self.assertEqual("COMPLETED", changed.prepare("buyer", self.item, INPUTS)["status"])
        self.assertEqual("NEEDS_INFORMATION", self.rag.validate_review("buyer", self.item, result["review_id"])["classification"])
        self.assert_blocked_without_reservation(trade, proposal)

    def test_d_payment_remains_independent_when_w_documents_are_blocked(self):
        self.assertEqual("BLOCKED", self.rag.prepare("buyer", self.item, INPUTS)["status"])
        trade = self.funded()
        trade = self.change(trade, "receipt", {"contract_revision": trade["contract_revision"]})
        trade, proposal = self.proposal(trade, "D")
        self.reserve(trade, proposal)
        self.trades.run_pending()
        current = self.trades.get("buyer", trade["trade_id"])
        self.assertIsNone(current["review"])
        self.assertEqual(500, current["balances"]["D"]["paid"])
        self.assertEqual(50, current["balances"]["W"]["held"])

    def test_w_refund_remains_independent_of_successful_transfer_evidence(self):
        result = self.rag.ask("buyer", self.item, "No source is available", INPUTS)
        self.assertEqual("NEEDS_INFORMATION", result["classification"])
        trade = self.adopt(self.funded(), result)
        trade, proposal = self.proposal(trade, refund=True)
        self.reserve(trade, proposal)
        self.trades.run_pending()
        current = self.trades.get("buyer", trade["trade_id"])
        self.assertIsNone(current["evidence"])
        self.assertEqual(50, current["balances"]["W"]["refunded"])

    def test_matching_current_review_and_exact_bilateral_attestation_can_reserve(self):
        result = self.review()
        trade = self.attest(self.adopt(self.funded(), result), both=False)
        trade, proposal = self.proposal(trade)
        self.assert_blocked_without_reservation(trade, proposal)
        evidence = trade["evidence"]
        self.change(trade, "confirm_evidence", {"evidence_id": evidence["evidence_id"], "hash": evidence["hash"]}, "seller")
        current = self.reserve(trade, proposal)
        self.assertEqual(1, len(current["operations"]))
        self.assertEqual(50, current["balances"]["W"]["pay_reserved"])

    def test_failed_w_retry_rechecks_source_without_rereserving_funds(self):
        result = self.review()
        trade = self.attest(self.adopt(self.funded(), result))
        trade, proposal = self.proposal(trade)
        self.reserve(trade, proposal)
        SettlementWorker(self.db, lambda operation: "FAILED").run_pending()
        trade = self.trades.get("buyer", trade["trade_id"])
        operation = trade["operations"][0]
        self.assertEqual("FAILED", operation["status"])
        self.revoke("local_embed")
        before = self.snapshot(trade)
        with self.assertRaises(ServiceError):
            self.change(trade, "retry", {"operation_id": operation["operation_id"]})
        self.assertEqual(before, self.snapshot(trade))
        self.assertEqual(1, self.trades.get("buyer", trade["trade_id"])["operations"][0]["attempt_no"])

    def test_unknown_reconciliation_remains_existing_result_confirmation(self):
        result = self.review()
        trade = self.attest(self.adopt(self.funded(), result))
        trade, proposal = self.proposal(trade)
        self.reserve(trade, proposal)
        worker = SettlementWorker(self.db, lambda operation: "UNKNOWN")
        worker.run_pending()
        trade = self.trades.get("buyer", trade["trade_id"])
        operation = trade["operations"][0]
        self.revoke()
        worker.record_event(operation["operation_id"], operation["attempt_no"], "SUCCEEDED")
        trade = self.change(trade, "reconcile", {"operation_id": operation["operation_id"]})
        self.assertEqual(1, len(trade["operations"]))
        self.assertEqual(50, trade["balances"]["W"]["paid"])
        self.assertEqual(0, trade["balances"]["W"]["pay_reserved"])

    def test_unadopted_new_chat_cannot_replace_immutable_conditions_or_approval(self):
        result = self.review()
        trade = self.attest(self.adopt(self.funded(), result))
        trade, proposal = self.proposal(trade)
        with closing(self.db.connect()) as connection:
            before = tuple(connection.execute("SELECT hash,payload_json FROM review_snapshots WHERE review_id=?", (trade["review_id"],)).fetchone())
        unrelated = self.rag.ask("buyer", self.item, "A new conversation with missing contract scope", {})
        self.assertEqual("NEEDS_INFORMATION", unrelated["classification"])
        self.assertNotEqual(result["review_id"], unrelated["review_id"])
        with closing(self.db.connect()) as connection:
            after = tuple(connection.execute("SELECT hash,payload_json FROM review_snapshots WHERE review_id=?", (trade["review_id"],)).fetchone())
        self.assertEqual(before, after)
        self.assertEqual(result["required_condition_ids"], self.trades.get("buyer", trade["trade_id"])["review"]["required_condition_ids"])
        current = self.reserve(trade, proposal)
        self.assertEqual(1, len(current["operations"]))

    def test_seller_can_execute_buyer_adopted_review_without_impersonation(self):
        result = self.review()
        trade = self.attest(self.adopt(self.funded(), result))
        trade, proposal = self.proposal(trade)
        current = self.change(trade, "reserve", {"proposal_id": proposal["proposal_id"], "revision": proposal["revision"]}, "seller")
        self.assertEqual(1, len(current["operations"]))
        self.assertEqual(50, current["balances"]["W"]["pay_reserved"])

    def test_source_revocation_waits_for_guarded_reservation_commit(self):
        result = self.review()
        trade = self.attest(self.adopt(self.funded(), result))
        trade, proposal = self.proposal(trade)
        revoke_record = {**self.policy.require(SOURCE_ID, "display_excerpt"), "status": "BLOCKED"}
        original = self.trades.review_validator
        validated, finish_validation, attempted, revoked = (threading.Event() for _ in range(4))
        def delayed_validation(*args):
            answer = original(*args)
            validated.set()
            if not finish_validation.wait(3):
                raise RuntimeError("Synthetic synchronization timed out")
            return answer
        def revoke():
            attempted.set()
            self.policy.set_review("manager", revoke_record)
            revoked.set()
        self.trades.review_validator = delayed_validation
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending_reservation = pool.submit(self.reserve, trade, proposal)
            try:
                self.assertTrue(validated.wait(3))
                pending_revoke = pool.submit(revoke)
                self.assertTrue(attempted.wait(3))
                self.assertFalse(revoked.wait(0.05))
            finally:
                finish_validation.set()
            current = pending_reservation.result(timeout=5)
            pending_revoke.result(timeout=5)
        self.assertTrue(revoked.is_set())
        self.assertEqual(1, len(current["operations"]))
        # A new key for that same logical operation returns its existing result.
        replay = self.reserve(trade, proposal)
        self.assertEqual(current["operations"], replay["operations"])
        self.assertEqual(current["balances"], replay["balances"])


if __name__ == "__main__":
    unittest.main()
