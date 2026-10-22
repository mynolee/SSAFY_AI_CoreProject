"""Cross-service acceptance with authored synthetic records and no network.

These checks exercise local demo money, not financial or provider systems.
Run with: python -m unittest discover -s tests -p test_acceptance.py -v
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import gc
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from warranty.catalog import Catalog
from warranty.config import Settings
from warranty.db import Database
from warranty.errors import ServiceError


def synthetic_review(actor, item_id, review_id):
    """Server adapter fixture; never an actual official document review."""
    return {"review_id": review_id, "classification": "TRANSFERABLE", "applicability_ok": True,
            "conditions_ok": True, "missing_information": [], "conflicts": [],
            "document_hashes": ["synthetic-body-hash"],
            "evidence": [{"evidence_id": "SYNTHETIC-evidence", "source_type": "official_document",
                          "document_role": "SERVICE_TERMS", "is_synthetic": True,
                          "item_id": item_id, "body_hash": "synthetic-body-hash"}]}


class TradeAcceptanceTests(unittest.TestCase):
    def setUp(self):
        from warranty.transactions import TradeService
        from warranty.worker import SettlementWorker

        self.temporary = tempfile.TemporaryDirectory(prefix="warranty-acceptance-")
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(gc.collect)
        self.db = Database(Path(self.temporary.name) / "app.sqlite3")
        self.db.initialize()
        self.catalog = Catalog.synthetic()
        self.item_id = self.catalog.items[0]["item_id"]
        with self.db.transaction() as connection:
            for item in self.catalog.items:
                connection.execute("INSERT INTO items(item_id,name,provider) VALUES(?,?,?)",
                                   (item["item_id"], item["name"], item["provider"]))
        self.service = TradeService(self.db, review_validator=synthetic_review)
        self.worker_type = SettlementWorker
        self.worker = SettlementWorker(self.db)

    def create(self, D=800000, W=80000):
        return self.service.create("buyer", "buyer", "seller", self.item_id, D, W)

    def mutate(self, trade, action, payload=None, actor="buyer", key=None, version=None):
        return self.service.mutate(actor, trade["trade_id"], action, payload or {}, key or uuid4().hex,
                                   trade["version"] if version is None else version)

    def fund(self, D=800000, W=80000):
        trade = self.create(D, W)
        for actor in ("buyer", "seller"):
            contract = trade["contract"]
            trade = self.mutate(trade, "contract_consent", {"revision": contract["revision"], "hash": contract["hash"]}, actor)
        return self.mutate(trade, "deposit")

    def ready(self, D=800000, W=80000, confirmations=2):
        trade = self.fund(D, W)
        trade = self.mutate(trade, "receipt", {"contract_revision": trade["contract"]["revision"]})
        if W:
            trade = self.mutate(trade, "adopt_review", {"review_id": "SYNTHETIC-review"})
            for actor in ("buyer", "seller"):
                trade = self.mutate(trade, "contract_consent", {"revision": trade["contract"]["revision"], "hash": trade["contract"]["hash"]}, actor)
            trade = self.mutate(trade, "receipt", {"contract_revision": trade["contract"]["revision"]})
            trade = self.mutate(trade, "evidence", {"references": ["SYNTHETIC:transfer-receipt"]})
            evidence = trade["evidence"]
            if isinstance(evidence, list):
                evidence = evidence[-1]
            for actor in ("buyer", "seller")[:confirmations]:
                trade = self.mutate(trade, "confirm_evidence", {"evidence_id": evidence["evidence_id"], "hash": evidence["hash"]}, actor)
        return trade

    def propose(self, trade, allocations):
        trade = self.mutate(trade, "proposal", {"allocations": allocations, "refund_reason": "mutual_agreement"})
        proposal = trade["proposals"][-1]
        for actor in ("buyer", "seller"):
            trade = self.mutate(trade, "proposal_consent", {"proposal_id": proposal["proposal_id"],
                                "revision": proposal["revision"], "hash": proposal["hash"]}, actor)
        return trade, proposal

    def reserve(self, trade, proposal):
        return self.mutate(trade, "reserve", {"proposal_id": proposal["proposal_id"], "revision": proposal["revision"]})

    def snapshot(self, trade):
        with closing(self.db.connect()) as connection:
            return {
                "balances": [tuple(row) for row in connection.execute("SELECT component,deposited,held,pay_reserved,refund_reserved,paid,refunded FROM component_balances WHERE trade_id=? ORDER BY component", (trade["trade_id"],))],
                "groups": connection.execute("SELECT COUNT(*) FROM ledger_groups WHERE trade_id=?", (trade["trade_id"],)).fetchone()[0],
                "operations": connection.execute("SELECT COUNT(*) FROM settlement_operations WHERE trade_id=?", (trade["trade_id"],)).fetchone()[0],
            }

    def assert_integrity(self, trade):
        with closing(self.db.connect()) as connection:
            self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())
            for row in connection.execute("SELECT * FROM component_balances WHERE trade_id=?", (trade["trade_id"],)):
                self.assertEqual(row["deposited"], sum(row[field] for field in ("held", "pay_reserved", "refund_reserved", "paid", "refunded")))
                self.assertTrue(all(row[field] >= 0 for field in ("held", "pay_reserved", "refund_reserved", "paid", "refunded")))
            for row in connection.execute("SELECT g.group_id,SUM(e.debit) AS debit,SUM(e.credit) AS credit FROM ledger_groups g JOIN ledger_entries e ON e.group_id=g.group_id WHERE g.trade_id=? GROUP BY g.group_id", (trade["trade_id"],)):
                self.assertEqual(row["debit"], row["credit"])
                self.assertGreater(row["debit"], 0)

    def test_d_payment_requires_receipt_but_no_warranty_evidence(self):
        trade = self.fund()
        trade, proposal = self.propose(trade, [{"component": "D", "pay_won": 800000, "refund_won": 0}])
        before = self.snapshot(trade)
        with self.assertRaises(ServiceError):
            self.reserve(trade, proposal)
        self.assertEqual(before, self.snapshot(trade))
        trade = self.mutate(trade, "receipt", {"contract_revision": trade["contract"]["revision"]})
        trade = self.reserve(trade, proposal)
        self.worker.run_pending()
        trade = self.service.get("buyer", trade["trade_id"])
        self.assertEqual(1, len(trade["operations"]))
        with closing(self.db.connect()) as connection:
            self.assertEqual(800000, connection.execute("SELECT paid FROM component_balances WHERE trade_id=? AND component='D'", (trade["trade_id"],)).fetchone()[0])
            self.assertEqual(80000, connection.execute("SELECT held FROM component_balances WHERE trade_id=? AND component='W'", (trade["trade_id"],)).fetchone()[0])
        self.assert_integrity(trade)

    def test_w_payment_one_confirmation_is_atomic_rejection(self):
        trade = self.ready(confirmations=1)
        trade, proposal = self.propose(trade, [{"component": "W", "pay_won": 80000, "refund_won": 0}])
        before = self.snapshot(trade)
        with self.assertRaises(ServiceError):
            self.reserve(trade, proposal)
        self.assertEqual(before, self.snapshot(trade))
        self.assert_integrity(trade)

    def test_w_refund_needs_no_successful_transfer_evidence(self):
        trade = self.fund()
        trade, proposal = self.propose(trade, [{"component": "W", "pay_won": 0, "refund_won": 80000}])
        trade = self.reserve(trade, proposal)
        self.worker.run_pending()
        with closing(self.db.connect()) as connection:
            balance = connection.execute("SELECT refunded,paid FROM component_balances WHERE trade_id=? AND component='W'", (trade["trade_id"],)).fetchone()
        self.assertEqual((80000, 0), tuple(balance))
        self.assert_integrity(trade)

    def test_mixed_w_allocation_conserves_funds(self):
        trade = self.ready()
        trade, proposal = self.propose(trade, [{"component": "W", "pay_won": 50000, "refund_won": 30000}])
        trade = self.reserve(trade, proposal)
        self.assertEqual(2, len(trade["operations"]))
        self.worker.run_pending()
        with closing(self.db.connect()) as connection:
            balance = connection.execute("SELECT paid,refunded,held,pay_reserved,refund_reserved FROM component_balances WHERE trade_id=? AND component='W'", (trade["trade_id"],)).fetchone()
        self.assertEqual((50000, 30000, 0, 0, 0), tuple(balance))
        self.assert_integrity(trade)

    def test_concurrent_replays_create_one_operation_and_posting(self):
        trade = self.ready(W=0)
        trade, proposal = self.propose(trade, [{"component": "D", "pay_won": 800000, "refund_won": 0}])
        payload = {"proposal_id": proposal["proposal_id"], "revision": proposal["revision"]}
        key = uuid4().hex
        with ThreadPoolExecutor(max_workers=20) as pool:
            results = list(pool.map(lambda _: self.mutate(trade, "reserve", payload, key=key), range(20)))
        operation_ids = {result["operations"][0]["operation_id"] for result in results}
        self.assertEqual(1, len(operation_ids))
        current = self.service.get("buyer", trade["trade_id"])
        repeated = self.mutate(current, "reserve", payload)
        self.assertEqual(1, len(repeated["operations"]))
        self.worker.run_pending()
        self.worker.run_pending()
        self.assert_integrity(current)

    def test_stale_success_replay_and_payload_conflict(self):
        trade = self.ready(W=0)
        trade, proposal = self.propose(trade, [{"component": "D", "pay_won": 800000, "refund_won": 0}])
        payload = {"proposal_id": proposal["proposal_id"], "revision": proposal["revision"]}
        key = uuid4().hex
        first = self.mutate(trade, "reserve", payload, key=key)
        replay = self.mutate(trade, "reserve", payload, key=key)
        self.assertEqual(first, replay)
        self.assertEqual(first["version"], self.service.get("buyer", trade["trade_id"])["version"])
        with self.assertRaises(ServiceError) as raised:
            self.mutate(trade, "reserve", dict(payload, revision=999), key=key)
        self.assertEqual(409, raised.exception.status)
        other = self.create(D=100, W=0)
        with self.assertRaises(ServiceError) as raised:
            self.mutate(other, "reserve", payload, key=key)
        self.assertEqual(409, raised.exception.status)
        self.assert_integrity(first)

    def test_reserved_evidence_change_rejected(self):
        trade = self.ready()
        trade, proposal = self.propose(trade, [{"component": "W", "pay_won": 80000, "refund_won": 0}])
        trade = self.reserve(trade, proposal)
        before = self.snapshot(trade)
        with self.assertRaises(ServiceError):
            self.mutate(trade, "evidence", {"references": ["SYNTHETIC:replacement"]})
        with self.assertRaises(ServiceError):
            self.mutate(trade, "adopt_review", {"review_id": "SYNTHETIC-replacement"})
        self.assertEqual(before, self.snapshot(trade))

    def test_dispute_reservation_race_has_one_legal_winner(self):
        trade = self.ready(W=0)
        trade, proposal = self.propose(trade, [{"component": "D", "pay_won": 800000, "refund_won": 0}])
        barrier = threading.Barrier(2)
        def run(action):
            barrier.wait()
            try:
                return self.mutate(trade, action, {"scope": "D"} if action == "dispute" else {"proposal_id": proposal["proposal_id"], "revision": proposal["revision"]})
            except ServiceError as exc:
                return exc
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, ("reserve", "dispute")))
        self.assertEqual(1, sum(isinstance(result, dict) for result in results))
        self.assert_integrity(trade)

    def test_unknown_keeps_reservation_until_trusted_reconciliation(self):
        trade = self.ready(W=0)
        trade, proposal = self.propose(trade, [{"component": "D", "pay_won": 800000, "refund_won": 0}])
        trade = self.reserve(trade, proposal)
        operation_id = trade["operations"][0]["operation_id"]
        worker = self.worker_type(self.db, outcome_provider=lambda *_: "UNKNOWN")
        worker.run_pending()
        worker.run_pending()
        current = self.service.get("buyer", trade["trade_id"])
        self.assertEqual("UNKNOWN", current["operations"][0]["status"])
        before = self.snapshot(current)
        with self.assertRaises(ServiceError):
            self.mutate(current, "retry", {"operation_id": operation_id})
        with self.assertRaises(ServiceError):
            self.mutate(current, "reconcile", {"operation_id": operation_id, "outcome": "SUCCEEDED", "amount": 800000})
        self.assertEqual(before, self.snapshot(current))
        worker.record_event(operation_id, 1, "SUCCEEDED", event_id="SYNTHETIC-trusted-event")
        current = self.mutate(current, "reconcile", {"operation_id": operation_id})
        self.assertEqual("SUCCEEDED", current["operations"][0]["status"])
        self.assert_integrity(current)

    def test_failed_concurrent_retry_has_one_next_attempt(self):
        trade = self.ready(W=0)
        trade, proposal = self.propose(trade, [{"component": "D", "pay_won": 800000, "refund_won": 0}])
        trade = self.reserve(trade, proposal)
        operation_id = trade["operations"][0]["operation_id"]
        self.worker_type(self.db, outcome_provider=lambda *_: "FAILED").run_pending()
        current = self.service.get("buyer", trade["trade_id"])
        self.assertEqual("FAILED", current["operations"][0]["status"])
        key = uuid4().hex
        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(lambda _: self.mutate(current, "retry", {"operation_id": operation_id}, key=key), range(10)))
        self.assertTrue(all(result["operations"][0]["operation_id"] == operation_id for result in results))
        self.assertEqual(2, results[0]["operations"][0]["attempt_no"])
        self.worker.run_pending()
        self.assert_integrity(current)

    def test_group_reservation_rejects_all_if_one_direction_invalid(self):
        trade = self.ready(confirmations=1)
        trade, proposal = self.propose(trade, [{"component": "D", "pay_won": 800000, "refund_won": 0},
                                              {"component": "W", "pay_won": 80000, "refund_won": 0}])
        before = self.snapshot(trade)
        with self.assertRaises(ServiceError):
            self.reserve(trade, proposal)
        self.assertEqual(before, self.snapshot(trade))

    def test_zero_w_can_close_but_unfunded_cannot_close(self):
        draft = self.create(D=100, W=0)
        with self.assertRaises(ServiceError):
            self.mutate(draft, "close")
        trade = self.ready(W=0)
        trade, proposal = self.propose(trade, [{"component": "D", "pay_won": 800000, "refund_won": 0}])
        trade = self.reserve(trade, proposal)
        self.worker.run_pending()
        current = self.service.get("buyer", trade["trade_id"])
        closed = self.mutate(current, "close")
        self.assertEqual("CLOSED", closed["status"])
        self.assertEqual(["D"], [operation["component"] for operation in closed["operations"]])
        with self.assertRaises(ServiceError):
            self.mutate(closed, "retry", {"operation_id": closed["operations"][0]["operation_id"]})

    def test_outsider_and_admin_cannot_approve_for_parties(self):
        trade = self.create()
        before = self.snapshot(trade)
        with self.assertRaises(ServiceError) as raised:
            self.service.get("outsider", trade["trade_id"])
        self.assertIn(raised.exception.status, (403, 404))
        for actor in ("outsider", "admin"):
            with self.subTest(actor=actor), self.assertRaises(ServiceError):
                self.mutate(trade, "contract_consent", {"revision": trade["contract"]["revision"], "hash": trade["contract"]["hash"]}, actor)
        self.assertEqual(before, self.snapshot(trade))

    def test_two_deposits_cannot_exceed_demo_wallet(self):
        trades = []
        for _ in range(2):
            trade = self.create(D=1500000, W=0)
            for actor in ("buyer", "seller"):
                trade = self.mutate(trade, "contract_consent", {"revision": trade["contract"]["revision"], "hash": trade["contract"]["hash"]}, actor)
            trades.append(trade)
        def deposit(trade):
            try:
                return self.mutate(trade, "deposit")
            except ServiceError as error:
                return error
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(deposit, trades))
        self.assertEqual(1, sum(isinstance(result, dict) for result in results))
        with closing(self.db.connect()) as connection:
            self.assertEqual(500000, connection.execute("SELECT available FROM demo_wallets WHERE user_id='buyer'").fetchone()[0])
        for trade in trades:
            self.assert_integrity(trade)

    def test_dispute_resolution_requires_both_parties_then_new_agreement(self):
        trade = self.fund(W=0)
        trade = self.mutate(trade, "dispute", {"scope": "D"})
        dispute = trade["disputes"][-1]
        trade = self.mutate(trade, "resolution", {"dispute_id": dispute["dispute_id"], "terms": "SYNTHETIC agreed refund"}, "admin")
        resolution = trade["resolutions"][-1]
        payload = {"resolution_id": resolution["resolution_id"], "revision": resolution["revision"], "hash": resolution["hash"]}
        trade = self.mutate(trade, "resolution_consent", payload)
        trade, unapproved_resolution_proposal = self.propose(trade, [{"component": "D", "pay_won": 0, "refund_won": 800000}])
        before = self.snapshot(trade)
        with self.assertRaises(ServiceError):
            self.reserve(trade, unapproved_resolution_proposal)
        self.assertEqual(before, self.snapshot(trade))
        trade = self.mutate(trade, "resolution_consent", payload, "seller")
        trade, proposal = self.propose(trade, [{"component": "D", "pay_won": 0, "refund_won": 800000}])
        trade = self.reserve(trade, proposal)
        self.worker.run_pending()
        self.assert_integrity(trade)


class RagAcceptanceTests(unittest.TestCase):
    create = TradeAcceptanceTests.create
    mutate = TradeAcceptanceTests.mutate
    fund = TradeAcceptanceTests.fund
    propose = TradeAcceptanceTests.propose
    reserve = TradeAcceptanceTests.reserve
    snapshot = TradeAcceptanceTests.snapshot
    assert_integrity = TradeAcceptanceTests.assert_integrity

    def setUp(self):
        from test_prompts import synthetic_policy
        from warranty.prompts import MockStructuredModel
        from warranty.rag import RagService
        from warranty.security import SourcePolicy

        TradeAcceptanceTests.setUp(self)
        self.settings = Settings(Path(self.temporary.name) / "data", Path(self.temporary.name) / "config")
        self.settings.prepare()
        (self.settings.config_dir / "prompts").mkdir()
        for kind in ("query", "selection", "answer"):
            (self.settings.config_dir / "prompts" / f"{kind}.json").write_text(json.dumps(synthetic_policy(kind)), encoding="utf-8")

        class RecordingModel(MockStructuredModel):
            model_id = "SYNTHETIC-optimistic-integration"
            def __init__(self):
                super().__init__()
                self.answer_inputs = []
                self.invalid_citation = False

            def generate(self, kind, prompt):
                if kind != "answer":
                    return super().generate(kind, prompt)
                inputs = json.loads(prompt)["input"]
                self.answer_inputs.append(inputs)
                evidence_id = "SYNTHETIC-forged-id" if self.invalid_citation else inputs["evidence"][0]["evidence_id"]
                return {"classification": "TRANSFERABLE", "answer": "SYNTHETIC optimistic answer [1].",
                        "claims": [{"text": "SYNTHETIC fact", "evidence_ids": [evidence_id]}],
                        "conditions": [], "missing_information": [], "conflicts": [],
                        "cited_evidence_ids": [evidence_id]}

        self.model = RecordingModel()
        source = self.catalog.sources["SYNTHETIC-SOURCE"]
        reviews = [{"source_id": source["source_id"], "action": action, "status": "ALLOWED",
                    "basis_type": "OTHER_RECORDED_BASIS", "basis_url": source["url"],
                    "reviewed_on": "2026-01-01", "scope": "SYNTHETIC authored fixture only",
                    "conditions": "SYNTHETIC offline", "user_scope": "SYNTHETIC local actors",
                    "processing_environment": "LOCAL"}
                   for action in ("view_link", "store_body", "extract", "local_embed", "display_excerpt")]
        self.policy = SourcePolicy(registry=[source], reviews=reviews, capabilities={"manager": ["DATA_MANAGER"]})
        self.rag = RagService(self.catalog, self.db, self.settings, model=self.model,
                              source_policy=self.policy, index_actor="manager")
        self.addCleanup(self._close_rag)

    def _close_rag(self):
        self.rag.close()

    def test_model_cannot_authorize_w_payment_without_documents(self):
        from warranty.transactions import TradeService

        self.service = TradeService(self.db, review_validator=self.rag.validate_review)
        result = self.rag.ask("buyer", self.item_id, "SYNTHETIC may I transfer this warranty?")
        self.assertTrue(self.model.answer_inputs)
        self.assertEqual("NEEDS_INFORMATION", result["classification"])
        self.assertFalse(result["applicability_ok"])
        self.assertFalse(result["conditions_ok"])
        self.assertEqual("DOC_BLOCKED", result["readiness"]["status"])
        trade = self.fund()
        trade = self.mutate(trade, "adopt_review", {"review_id": result["review_id"]})
        for actor in ("buyer", "seller"):
            trade = self.mutate(trade, "contract_consent", {"revision": trade["contract"]["revision"], "hash": trade["contract"]["hash"]}, actor)
        trade = self.mutate(trade, "receipt", {"contract_revision": trade["contract"]["revision"]})
        trade = self.mutate(trade, "evidence", {"references": ["SYNTHETIC:bilateral-receipt"]})
        evidence = trade["evidence"]
        for actor in ("buyer", "seller"):
            trade = self.mutate(trade, "confirm_evidence", {"evidence_id": evidence["evidence_id"], "hash": evidence["hash"]}, actor)
        trade, proposal = self.propose(trade, [{"component": "W", "pay_won": 80000, "refund_won": 0}])
        before = self.snapshot(trade)
        with self.assertRaises(ServiceError):
            self.reserve(trade, proposal)
        self.assertEqual(before, self.snapshot(trade))
        self.assert_integrity(trade)

    def test_metadata_reindex_has_no_growth(self):
        before = self.rag.vectors.meta.count()
        records = [{"item_id": item["item_id"], "text": item["index_text"]} for item in self.catalog.items]
        self.rag.vectors.index_metadata(records)
        self.rag.vectors.index_metadata(records)
        self.assertEqual(2, before)
        self.assertEqual(before, self.rag.vectors.meta.count())
        self.assertEqual(0, self.rag.vectors.docs.count())

    def test_unready_own_document_stays_blocked(self):
        job = self.rag.prepare("buyer", self.item_id)
        self.assertEqual("BLOCKED", job["status"])
        self.assertEqual("DOC_BLOCKED", job["stage"])
        self.assertEqual(0, job["body_count"])
        self.assertEqual(0, job["chunk_count"])
        with self.assertRaises(ServiceError):
            self.rag.job("seller", job["job_id"])
        self.assertEqual("DOC_BLOCKED", self.rag.checklist(self.item_id)["readiness"]["status"])

    def test_selected_metadata_always_in_context(self):
        graph_nodes = set(self.rag.graph.get_graph().nodes) - {"__start__", "__end__"}
        self.assertEqual({"retrieve", "generate"}, graph_nodes)
        for top_k in (3, 6, 10):
            result = self.rag.ask("buyer", self.item_id, f"SYNTHETIC context {top_k}", top_k=top_k)
            context = self.model.answer_inputs[-1]["evidence"]
            self.assertEqual(1, sum(entry["source_type"] == "metadata" for entry in context))
            self.assertEqual({self.item_id}, {entry["item_id"] for entry in context})
            self.assertTrue(set(result["cited_evidence_ids"]).issubset(result["retrieved_evidence_ids"]))
            self.assertEqual(set(result["cited_evidence_ids"]), {entry["evidence_id"] for entry in result["evidence"]})

    def test_invalid_model_citation_is_not_trusted(self):
        self.model.invalid_citation = True
        result = self.rag.ask("buyer", self.item_id, "SYNTHETIC forged source attempt")
        self.assertEqual("NEEDS_INFORMATION", result["classification"])
        self.assertEqual([], result["cited_evidence_ids"])
        self.assertEqual([], result["evidence"])
        self.assertTrue(result["errors"])
        self.assertLessEqual(len(self.model.answer_inputs), 2)

    def test_item_chat_history_never_crosses_users_or_items(self):
        other_id = self.catalog.items[1]["item_id"]
        for actor, item, question in (("buyer", self.item_id, "SYNTHETIC buyer A"),
                                       ("buyer", other_id, "SYNTHETIC buyer B"),
                                       ("seller", self.item_id, "SYNTHETIC seller A"),
                                       ("buyer", self.item_id, "SYNTHETIC buyer A again")):
            self.rag.ask(actor, item, question)
        self.assertEqual(["SYNTHETIC buyer A", "SYNTHETIC buyer A again"], [entry["question"] for entry in self.rag.history("buyer", self.item_id)])
        self.assertEqual(["SYNTHETIC buyer B"], [entry["question"] for entry in self.rag.history("buyer", other_id)])
        self.assertEqual(["SYNTHETIC seller A"], [entry["question"] for entry in self.rag.history("seller", self.item_id)])

    def test_synthetic_chunk_revision_is_atomic(self):
        from warranty.rag import RagService

        document = self.rag.registry.register_document("manager", source_id="SYNTHETIC-SOURCE",
                    title="SYNTHETIC authored terms", canonical_url="https://example.invalid/synthetic",
                    document_type="HTML", is_synthetic=True)
        body = ("<html><h1>SYNTHETIC terms</h1><p>" + "SYNTHETIC authored condition. " * 90 + "</p></html>").encode()
        applicability = {"country": "KR", "date_start": "2026-01-01", "date_end": "2026-12-31", "plan_generation": "SYNTHETIC",
                         "payment_method": "SYNTHETIC", "channel": "SYNTHETIC", "document_type": "SERVICE_TERMS"}
        version = self.rag.registry.ingest_bytes("manager", document["document_id"], body, applicability=applicability)
        for item in self.catalog.items:
            self.rag.registry.link("manager", item["item_id"], version["version_id"], role="SERVICE_TERMS",
                                   applicability_basis="SYNTHETIC shared fixture applies to both items", locator="SYNTHETIC section")
        first = self.rag.prepare("buyer", self.item_id)
        self.assertEqual("COMPLETED", first["status"])
        first_rows = self.rag.vectors.docs.get(include=["metadatas"])
        self.assertEqual(len(first_rows["ids"]), len(set(first_rows["ids"])))
        self.assertEqual({item["item_id"] for item in self.catalog.items}, {row["item_id"] for row in first_rows["metadatas"]})
        replacement = RagService(self.catalog, self.db, self.settings, model=self.model, source_policy=self.policy,
                                 index_actor="manager", chunk_size=700, chunk_overlap=100)
        self.addCleanup(replacement.close)
        second = replacement.prepare("buyer", self.item_id)
        self.assertEqual("COMPLETED", second["status"])
        self.assertNotEqual(first["revision"], second["revision"])
        current = replacement.vectors.docs.get(include=["metadatas"])
        self.assertEqual({second["revision"]}, {row["revision"] for row in current["metadatas"]})
        for item in self.catalog.items:
            rows = replacement.vectors.search(item["item_id"], second["revision"], "SYNTHETIC terms", top_k=10)
            self.assertTrue(rows)
            self.assertEqual({item["item_id"]}, {row["item_id"] for row in rows})
            self.assertTrue(all(row.get("section") for row in rows))


class FrontendIntegrationTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from warranty.interfaces.api import create_app

        self.facade = SimpleNamespace(settings=SimpleNamespace(demo_mode=True), catalog=Catalog.synthetic(),
                                      rag=Mock(), trades=Mock(), worker=Mock())
        self.facade.rag.ask.return_value = {"answer": "SYNTHETIC shared service result", "evidence": [], "cited_evidence_ids": []}
        self.facade.trades.mutate.return_value = {"trade_id": "SYNTHETIC-trade", "version": 8}
        self.client = TestClient(create_app(self.facade), base_url="http://localhost",
                                 client=("127.0.0.1", 50000), raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        response = self.client.post("/session", json={"actor": "buyer"}, headers={"Origin": "http://localhost"})
        self.assertEqual(200, response.status_code)
        self.headers = {"Origin": "http://localhost", "X-CSRF-Token": response.json()["data"]["csrf_token"]}

    def test_api_and_streamlit_share_facade_service(self):
        from warranty.interfaces.streamlit_app import run

        item_id = self.facade.catalog.items[0]["item_id"]
        response = self.client.post("/chat", json={"item_id": item_id, "question": "SYNTHETIC API question"}, headers=self.headers)
        self.assertEqual(200, response.status_code)
        self.facade.rag.ask.assert_called_once_with("buyer", item_id, "SYNTHETIC API question", applicability=None, top_k=6, strategy="similarity")
        st = MagicMock()
        st.session_state = {}
        st.get_option.return_value = "127.0.0.1"
        st.selectbox.return_value = "buyer"
        with patch("warranty.interfaces.streamlit_app._catalog", return_value=None) as catalog_page:
            run(self.facade, st)
            self.assertIs(self.facade, catalog_page.call_args.args[1])

    def test_api_mutation_uses_session_csrf_version_and_idempotency(self):
        route = "/trades/SYNTHETIC-trade/deposit"
        body = {"expected_version": 7}
        response = self.client.post(route, json=body, headers={"Origin": "http://localhost", "Idempotency-Key": "SYNTHETIC-key"})
        self.assertEqual(403, response.status_code)
        self.facade.trades.mutate.assert_not_called()
        response = self.client.post(route, json=body, headers=self.headers)
        self.assertEqual(400, response.status_code)
        response = self.client.post(route, json=dict(body, actor="admin"), headers=dict(self.headers, **{"Idempotency-Key": "SYNTHETIC-key"}))
        self.assertEqual(403, response.status_code)
        self.facade.trades.mutate.assert_not_called()
        response = self.client.post(route, json=body, headers=dict(self.headers, **{"Idempotency-Key": "SYNTHETIC-key"}))
        self.assertEqual(200, response.status_code)
        self.facade.trades.mutate.assert_called_once_with("buyer", "SYNTHETIC-trade", "deposit", {}, "SYNTHETIC-key", 7)
        self.assertEqual({"data", "error", "request_id"}, set(response.json()))

    def test_safe_errors_and_disabled_upload(self):
        marker = "SYNTHETIC-secret-not-for-output"
        self.facade.rag.ask.side_effect = RuntimeError(marker)
        item_id = self.facade.catalog.items[0]["item_id"]
        response = self.client.post("/chat", json={"item_id": item_id, "question": "SYNTHETIC failure"}, headers=self.headers)
        self.assertEqual(500, response.status_code)
        self.assertNotIn(marker, response.text)
        self.assertNotIn("Traceback", response.text)
        self.assertEqual("INTERNAL_ERROR", response.json()["error"]["code"])
        response = self.client.post("/trades/SYNTHETIC-trade/uploads", json={}, headers=self.headers)
        self.assertEqual(403, response.status_code)
        self.assertEqual("UPLOAD_DISABLED", response.json()["error"]["code"])
        self.facade.trades.mutate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
