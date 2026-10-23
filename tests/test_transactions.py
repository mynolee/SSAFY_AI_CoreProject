"""Synthetic settlement regression tests; no real money, documents or uploads."""
import concurrent.futures
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid

from warranty.db import Database
from warranty.errors import ServiceError
from warranty.transactions import TradeService, canonical, digest, verify_accounting
from warranty.worker import SettlementWorker


def verified_review(actor, item_id, review_id):
    return {"review_id": review_id, "classification": "TRANSFERABLE",
            "applicability_ok": True, "conditions_ok": True,
            "missing_information": [], "conflicts": [],
            "evidence": [{"id": "SYNTHETIC:terms", "type": "official_document", "synthetic": True}],
            "document_hashes": ["synthetic-document-hash"]}


class TransactionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "synthetic.sqlite")
        self.db.initialize()
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO items(item_id,name,provider) VALUES('SYNTHETIC:item','Synthetic phone','SYNTHETIC')")
        self.service = TradeService(self.db, verified_review)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def create(self, D=800000, W=80000):
        return self.service.create("buyer", "buyer", "seller", "SYNTHETIC:item", D, W)

    def call(self, trade, action, payload=None, actor="buyer", key=None, version=None):
        current = self.service.get(actor, trade["trade_id"])
        return self.service.mutate(actor, trade["trade_id"], action, payload or {}, key or uuid.uuid4().hex,
                                   current["version"] if version is None else version)

    def consent_contract(self, trade):
        current = self.service.get("buyer", trade["trade_id"])
        contract = current["contract"]
        payload = {"revision": contract["revision"], "hash": contract["hash"]}
        self.call(trade, "contract_consent", payload)
        return self.call(trade, "contract_consent", payload, "seller")

    def fund(self, W=80000, D=800000):
        trade = self.create(D, W)
        self.consent_contract(trade)
        self.call(trade, "deposit")
        return self.call(trade, "receipt", {"contract_revision": trade["contract_revision"]})

    def ready(self, trade, bilateral=True):
        trade = self.call(trade, "adopt_review", {"review_id": "SYNTHETIC:review"})
        self.consent_contract(trade)
        trade = self.call(trade, "receipt", {"contract_revision": trade["contract_revision"]})
        trade = self.call(trade, "evidence", {"references": ["SYNTHETIC:transfer-proof"]})
        evidence = trade["evidence"]
        payload = {"evidence_id": evidence["evidence_id"], "hash": evidence["hash"]}
        self.call(trade, "confirm_evidence", payload)
        if bilateral:
            trade = self.call(trade, "confirm_evidence", payload, "seller")
        return trade

    def proposal(self, trade, allocations, refund_reason=None):
        payload = {"allocations": allocations}
        if refund_reason is not None:
            payload["refund_reason"] = refund_reason
        trade = self.call(trade, "proposal", payload)
        proposal = trade["proposals"][-1]
        consent = {"proposal_id": proposal["proposal_id"], "revision": proposal["revision"], "hash": proposal["hash"]}
        self.call(trade, "proposal_consent", consent)
        trade = self.call(trade, "proposal_consent", consent, "seller")
        return trade, proposal

    def reserve(self, trade, proposal, **kwargs):
        return self.call(trade, "reserve", {"proposal_id": proposal["proposal_id"], "revision": proposal["revision"]}, **kwargs)

    def assert_accounts(self, trade):
        current = self.service.get("buyer", trade["trade_id"])
        for balance in current["balances"].values():
            amounts = [balance[k] for k in ("held", "pay_reserved", "refund_reserved", "paid", "refunded")]
            self.assertTrue(all(v >= 0 for v in amounts))
            self.assertEqual(balance["deposited"], sum(amounts))
        with self.db.connect() as conn:
            verify_accounting(conn, trade["trade_id"])
            self.assertEqual([], list(conn.execute("PRAGMA foreign_key_check")))
            self.assertEqual([], list(conn.execute("SELECT group_id FROM ledger_entries GROUP BY group_id HAVING SUM(debit)!=SUM(credit)")))
        return current

    def assert_code(self, code, fn):
        with self.assertRaises(ServiceError) as caught:
            fn()
        self.assertEqual(code, caught.exception.code)

    def test_D_only_pays_without_review_or_evidence(self):
        trade = self.fund()
        trade, proposal = self.proposal(trade, [{"component": "D", "pay_won": 800000, "refund_won": 0}])
        self.assertIsNone(proposal["review_id"])
        self.assertIsNone(proposal["evidence_id"])
        self.reserve(trade, proposal)
        self.assertEqual(1, self.service.run_pending()["processed_jobs"])
        current = self.assert_accounts(trade)
        self.assertEqual(800000, current["balances"]["D"]["paid"])
        self.assertEqual(80000, current["balances"]["W"]["held"])

    def test_repeated_receipt_does_not_create_a_new_trade_change(self):
        trade = self.fund(W=0)
        with self.db.connect() as conn:
            accepted = conn.execute("SELECT COUNT(*) FROM audit_events WHERE trade_id=? AND action='receipt'", (trade["trade_id"],)).fetchone()[0]
        repeated = self.call(trade, "receipt", {"contract_revision": trade["contract_revision"]})
        self.assertEqual(trade["version"], repeated["version"])
        self.assertEqual(trade["ledger"], repeated["ledger"])
        with self.db.connect() as conn:
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM receipt_confirmations WHERE trade_id=?", (trade["trade_id"],)).fetchone()[0])
            self.assertEqual(accepted, conn.execute("SELECT COUNT(*) FROM audit_events WHERE trade_id=? AND action='receipt'", (trade["trade_id"],)).fetchone()[0])

    def test_one_evidence_consent_creates_no_reservation_or_ledger(self):
        trade = self.ready(self.fund(), bilateral=False)
        trade, proposal = self.proposal(trade, [{"component": "W", "pay_won": 80000, "refund_won": 0}])
        before = self.service.get("buyer", trade["trade_id"])
        self.assert_code("W_BILATERAL_EVIDENCE_REQUIRED", lambda: self.reserve(trade, proposal))
        after = self.assert_accounts(trade)
        self.assertEqual(before["ledger"], after["ledger"])
        self.assertEqual([], after["operations"])

    def test_W_refund_needs_no_successful_evidence(self):
        trade = self.fund()
        trade, proposal = self.proposal(trade, [{"component": "W", "pay_won": 0, "refund_won": 80000}], "mutual_agreement")
        self.reserve(trade, proposal)
        self.service.run_pending()
        self.assertEqual(80000, self.assert_accounts(trade)["balances"]["W"]["refunded"])

    def test_mixed_W_two_directions_one_group(self):
        trade = self.ready(self.fund())
        trade, proposal = self.proposal(trade, [{"component": "W", "pay_won": 50000, "refund_won": 30000}], "mutual_agreement")
        current = self.reserve(trade, proposal)
        self.assertEqual(1, len(current["groups"]))
        self.assertEqual(2, len(current["operations"]))
        self.service.run_pending()
        current = self.assert_accounts(trade)
        self.assertEqual(50000, current["balances"]["W"]["paid"])
        self.assertEqual(30000, current["balances"]["W"]["refunded"])

    def test_twenty_concurrent_keys_exactly_once_and_logical_replay(self):
        trade, proposal = self.proposal(self.fund(W=0), [{"component": "D", "pay_won": 800000, "refund_won": 0}])
        key, version = "same-key", trade["version"]
        payload = {"proposal_id": proposal["proposal_id"], "revision": 1}
        def submit(_):
            return self.service.mutate("buyer", trade["trade_id"], "reserve", payload, key, version)
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
            responses = list(pool.map(submit, range(20)))
        self.assertTrue(all(result == responses[0] for result in responses))
        replay = self.reserve(trade, proposal, key="another-key", version=1)
        self.assertEqual(1, len(replay["operations"]))
        self.assertEqual(1, len(replay["groups"]))
        self.assertEqual(responses[0]["version"], replay["version"])
        self.assert_accounts(trade)

    def test_stale_success_replay_and_key_payload_conflict(self):
        trade, proposal = self.proposal(self.fund(W=0), [{"component": "D", "pay_won": 800000, "refund_won": 0}])
        version = trade["version"]
        self.reserve(trade, proposal, key="reserve", version=version)
        self.service.run_pending()
        current = self.reserve(trade, proposal, key="different", version=1)
        self.assertEqual("SUCCEEDED", current["operations"][0]["status"])
        self.assert_code("IDEMPOTENCY_CONFLICT", lambda: self.reserve(trade, proposal, key="reserve", version=version + 1))
        other = self.fund(W=0, D=100)
        other, other_proposal = self.proposal(other, [{"component": "D", "pay_won": 100, "refund_won": 0}])
        self.assert_code("IDEMPOTENCY_CONFLICT", lambda: self.reserve(other, other_proposal, key="reserve"))
        self.assertEqual(800000, self.assert_accounts(trade)["balances"]["D"]["paid"])

    def test_changed_evidence_stales_proposal_and_active_reservation_blocks_changes(self):
        trade = self.ready(self.fund())
        trade, proposal = self.proposal(trade, [{"component": "W", "pay_won": 80000, "refund_won": 0}])
        self.call(trade, "evidence", {"references": ["SYNTHETIC:new-proof"]})
        self.assert_code("PROPOSAL_STALE", lambda: self.reserve(trade, proposal))
        trade = self.ready(trade)
        trade, proposal = self.proposal(trade, [{"component": "W", "pay_won": 80000, "refund_won": 0}])
        self.reserve(trade, proposal)
        self.assert_code("ACTIVE_SETTLEMENT", lambda: self.call(trade, "adopt_review", {"review_id": "SYNTHETIC:changed"}))
        self.assert_code("ACTIVE_SETTLEMENT", lambda: self.call(trade, "evidence", {"references": ["SYNTHETIC:changed"]}))

    def test_dispute_cancels_entire_unclaimed_mixed_group(self):
        trade = self.ready(self.fund())
        trade, proposal = self.proposal(trade, [{"component": "D", "pay_won": 800000, "refund_won": 0}, {"component": "W", "pay_won": 80000, "refund_won": 0}])
        self.reserve(trade, proposal)
        self.call(trade, "dispute", {"scope": "W"})
        self.assertEqual(0, self.service.run_pending()["processed_jobs"])
        current = self.assert_accounts(trade)
        self.assertTrue(all(op["status"] == "CANCELLED" for op in current["operations"]))
        self.assertEqual(800000, current["balances"]["D"]["held"])
        self.assertEqual(80000, current["balances"]["W"]["held"])

    def test_worker_claim_interruption_rolls_back_to_reserved(self):
        trade, proposal = self.proposal(self.fund(W=0), [{"component": "D", "pay_won": 800000, "refund_won": 0}])
        self.reserve(trade, proposal)
        worker = SettlementWorker(self.db)
        worker.fail_after_claim = True
        with self.assertRaises(RuntimeError):
            worker.run_pending()
        self.assertEqual("RESERVED", self.assert_accounts(trade)["operations"][0]["status"])
        worker.fail_after_claim = False
        self.assertEqual(1, worker.run_pending()["processed_jobs"])

    def test_unknown_holds_and_concurrent_reconcile_settles_once(self):
        trade, proposal = self.proposal(self.fund(W=0), [{"component": "D", "pay_won": 800000, "refund_won": 0}])
        trade = self.reserve(trade, proposal)
        worker = SettlementWorker(self.db, lambda op: "UNKNOWN")
        worker.run_pending()
        current = self.assert_accounts(trade)
        op = current["operations"][0]
        self.assertEqual(800000, current["balances"]["D"]["pay_reserved"])
        self.assert_code("RETRY_REQUIRES_FAILED", lambda: self.call(trade, "retry", {"operation_id": op["operation_id"]}))
        self.assertEqual(0, SettlementWorker(self.db).run_pending()["processed_jobs"])
        worker.record_event(op["operation_id"], 1, "SUCCEEDED", "server-result")
        key, version = "same-reconcile", current["version"]
        def reconcile(_):
            return self.service.mutate("buyer", trade["trade_id"], "reconcile", {"operation_id": op["operation_id"]}, key, version)
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            responses = list(pool.map(reconcile, range(10)))
        self.assertTrue(all(x == responses[0] for x in responses))
        current = self.assert_accounts(trade)
        self.assertEqual(800000, current["balances"]["D"]["paid"])
        self.assertEqual(1, sum(1 for x in current["ledger"] if x["event_type"] == "SETTLE" and x["line_no"] == 1))
        self.assert_code("INVALID_ACTION_PAYLOAD", lambda: self.call(trade, "reconcile", {"operation_id": op["operation_id"], "outcome": "FAILED"}))

    def test_persisted_processing_recovery_and_late_event_audit(self):
        trade, proposal = self.proposal(self.fund(W=0), [{"component": "D", "pay_won": 800000, "refund_won": 0}])
        trade = self.reserve(trade, proposal)
        op = trade["operations"][0]
        with self.db.transaction() as conn:
            conn.execute("UPDATE settlement_operations SET status='PROCESSING' WHERE operation_id=?", (op["operation_id"],))
        worker = SettlementWorker(self.db)
        self.assertEqual(1, worker.recover_processing())
        worker.record_event(op["operation_id"], 1, "SUCCEEDED")
        self.call(trade, "reconcile", {"operation_id": op["operation_id"]})
        before = self.assert_accounts(trade)
        worker.record_event(op["operation_id"], 1, "FAILED")
        after = self.assert_accounts(trade)
        self.assertEqual(before["ledger"], after["ledger"])
        self.assertEqual("SUCCEEDED", after["operations"][0]["status"])
        with self.db.connect() as conn:
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM audit_events WHERE action='late_simulation_event'").fetchone()[0])

    def test_concurrent_failed_retry_same_operation_once(self):
        trade, proposal = self.proposal(self.fund(W=0), [{"component": "D", "pay_won": 800000, "refund_won": 0}])
        self.reserve(trade, proposal)
        SettlementWorker(self.db, lambda op: "FAILED").run_pending()
        current = self.assert_accounts(trade)
        op, version = current["operations"][0], current["version"]
        def retry(index):
            try:
                self.service.mutate("buyer", trade["trade_id"], "retry", {"operation_id": op["operation_id"]}, f"retry-{index}", version)
                return True
            except ServiceError as error:
                self.assertIn(error.code, ("VERSION_CONFLICT", "RETRY_REQUIRES_FAILED"))
                return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            self.assertEqual(1, sum(pool.map(retry, range(10))))
        current = self.assert_accounts(trade)
        self.assertEqual(op["operation_id"], current["operations"][0]["operation_id"])
        self.assertEqual(2, current["operations"][0]["attempt_no"])
        worker = SettlementWorker(self.db, lambda op: "UNKNOWN")
        worker.run_pending()
        worker.record_event(op["operation_id"], 1, "SUCCEEDED")
        self.call(trade, "reconcile", {"operation_id": op["operation_id"]})
        self.assertEqual("UNKNOWN", self.assert_accounts(trade)["operations"][0]["status"])
        worker.record_event(op["operation_id"], 2, "SUCCEEDED")
        self.call(trade, "reconcile", {"operation_id": op["operation_id"]})
        self.assertEqual(800000, self.assert_accounts(trade)["balances"]["D"]["paid"])

    def test_unknown_retry_holds_a_separately_queued_sibling_attempt(self):
        trade = self.ready(self.fund())
        trade, proposal = self.proposal(trade, [{"component": "W", "pay_won": 50000, "refund_won": 30000}], "mutual_agreement")
        self.reserve(trade, proposal)
        SettlementWorker(self.db, lambda op: "FAILED").run_pending()
        for op in self.assert_accounts(trade)["operations"]:
            self.call(trade, "retry", {"operation_id": op["operation_id"]})
        calls = []
        def unknown_once(op):
            calls.append(op["operation_id"])
            return "UNKNOWN"
        worker = SettlementWorker(self.db, unknown_once)
        self.assertEqual(1, worker.run_pending()["processed_jobs"])
        self.assertEqual(1, len(calls))
        current = self.assert_accounts(trade)
        self.assertEqual({"UNKNOWN", "RESERVED"}, {op["status"] for op in current["operations"]})
        self.assertEqual(0, worker.run_pending()["processed_jobs"])
        self.assertEqual(1, len(calls))
        unknown = next(op for op in current["operations"] if op["status"] == "UNKNOWN")
        worker.record_event(unknown["operation_id"], unknown["attempt_no"], "SUCCEEDED")
        self.call(trade, "reconcile", {"operation_id": unknown["operation_id"]})
        self.assertEqual(1, SettlementWorker(self.db).run_pending()["processed_jobs"])
        current = self.assert_accounts(trade)
        self.assertTrue(all(op["status"] == "SUCCEEDED" for op in current["operations"]))
        self.assertEqual(50000, current["balances"]["W"]["paid"])
        self.assertEqual(30000, current["balances"]["W"]["refunded"])

    def test_processing_recovery_preserves_other_jobs_in_the_same_group(self):
        trade = self.ready(self.fund())
        trade, proposal = self.proposal(trade, [{"component": "W", "pay_won": 50000, "refund_won": 30000}], "mutual_agreement")
        self.reserve(trade, proposal)
        SettlementWorker(self.db, lambda op: "FAILED").run_pending()
        for op in self.assert_accounts(trade)["operations"]:
            self.call(trade, "retry", {"operation_id": op["operation_id"]})
        current = self.assert_accounts(trade)
        recovering = current["operations"][0]
        waiting = current["operations"][1]
        with self.db.transaction() as conn:
            conn.execute("UPDATE settlement_operations SET status='PROCESSING' WHERE operation_id=?", (recovering["operation_id"],))
        worker = SettlementWorker(self.db)
        self.assertEqual(1, worker.recover_processing())
        with self.db.connect() as conn:
            waiting_job = next(row for row in conn.execute("SELECT * FROM jobs WHERE group_id=?", (recovering["group_id"],))
                               if [waiting["operation_id"], waiting["attempt_no"]] in json.loads(row["attempts_json"]))
            self.assertEqual("PENDING", waiting_job["status"])
        self.assertEqual(0, worker.run_pending()["processed_jobs"])
        worker.record_event(recovering["operation_id"], recovering["attempt_no"], "SUCCEEDED")
        self.call(trade, "reconcile", {"operation_id": recovering["operation_id"]})
        self.assertEqual(1, worker.run_pending()["processed_jobs"])
        current = self.assert_accounts(trade)
        self.assertTrue(all(op["status"] == "SUCCEEDED" for op in current["operations"]))

    def test_unknown_component_allows_an_independent_component_retry(self):
        trade, proposal = self.proposal(self.fund(D=100, W=80), [
            {"component": "D", "pay_won": 100, "refund_won": 0},
            {"component": "W", "pay_won": 0, "refund_won": 80}], "mutual_agreement")
        self.reserve(trade, proposal)
        SettlementWorker(self.db, lambda op: "FAILED").run_pending()
        for op in self.assert_accounts(trade)["operations"]:
            self.call(trade, "retry", {"operation_id": op["operation_id"]})
        worker = SettlementWorker(self.db, lambda op: "UNKNOWN" if op["component"] == "D" else "SUCCEEDED")
        self.assertEqual(2, worker.run_pending()["processed_jobs"])
        current = self.assert_accounts(trade)
        self.assertEqual({"D": "UNKNOWN", "W": "SUCCEEDED"}, {op["component"]: op["status"] for op in current["operations"]})
        self.assertEqual(100, current["balances"]["D"]["pay_reserved"])
        self.assertEqual(80, current["balances"]["W"]["refunded"])

    def test_mixed_allocation_failure_rolls_back_all(self):
        trade = self.fund()
        trade, proposal = self.proposal(trade, [{"component": "D", "pay_won": 800000, "refund_won": 0}, {"component": "W", "pay_won": 80000, "refund_won": 0}])
        before = self.assert_accounts(trade)
        self.assert_code("W_REVIEW_NOT_READY", lambda: self.reserve(trade, proposal))
        after = self.assert_accounts(trade)
        self.assertEqual(before["ledger"], after["ledger"])
        self.assertEqual([], after["operations"])

    def test_zero_W_close_and_terminal_guards(self):
        draft = self.create(W=0)
        self.assert_code("UNFUNDED_CANNOT_CLOSE", lambda: self.call(draft, "close"))
        trade, proposal = self.proposal(self.fund(W=0), [{"component": "D", "pay_won": 800000, "refund_won": 0}])
        self.reserve(trade, proposal)
        self.service.run_pending()
        closed = self.call(trade, "close")
        self.assertEqual("CLOSED", closed["status"])
        self.assertTrue(all(op["component"] == "D" for op in closed["operations"]))
        self.assert_code("TRADE_TERMINAL", lambda: self.call(trade, "retry", {"operation_id": closed["operations"][0]["operation_id"]}))
        replay = self.reserve(trade, proposal, version=1)
        self.assertEqual("CLOSED", replay["status"])

    def test_deposit_cancel_race_has_one_transition(self):
        trade = self.create(W=0)
        trade = self.consent_contract(trade)
        consent = {"revision": 1, "hash": trade["contract"]["hash"]}
        trade = self.call(trade, "cancel_consent", consent, "seller")
        version = trade["version"]
        def race(action):
            try:
                return self.service.mutate("buyer", trade["trade_id"], action, {} if action == "deposit" else consent, action, version)["status"]
            except ServiceError as error:
                return error.code
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(race, ["deposit", "cancel_consent"]))
        current = self.assert_accounts(trade)
        self.assertIn(current["status"], ("FUNDED", "CANCELLED"))
        self.assertEqual(1, sum(x in ("FUNDED", "CANCELLED") for x in results))

    def test_child_ownership_and_admin_proxy_rejected(self):
        trade = self.fund(W=0)
        other = self.create(D=100, W=0)
        other, proposal = self.proposal(other, [{"component": "D", "pay_won": 100, "refund_won": 0}])
        self.assert_code("PROPOSAL_NOT_FOUND", lambda: self.reserve(trade, proposal))
        self.assert_code("TRADE_NOT_FOUND", lambda: self.service.get("outsider", trade["trade_id"]))
        contract = trade["contract"]
        self.assert_code("PARTY_REQUIRED", lambda: self.call(trade, "contract_consent", {"revision": contract["revision"], "hash": contract["hash"]}, "admin"))
        self.assert_code("INVALID_ACTION_PAYLOAD", lambda: self.call(trade, "deposit", {"actor": "buyer"}))

    def test_two_trades_cannot_spend_same_wallet_balance(self):
        first, second = self.create(D=1500000, W=0), self.create(D=1500000, W=0)
        first, second = self.consent_contract(first), self.consent_contract(second)
        def deposit(trade):
            try:
                self.call(trade, "deposit")
                return True
            except ServiceError as error:
                self.assertEqual("INSUFFICIENT_WALLET", error.code)
                return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(1, sum(pool.map(deposit, [first, second])))
        with self.db.connect() as conn:
            self.assertEqual(500000, conn.execute("SELECT available FROM demo_wallets WHERE user_id='buyer'").fetchone()[0])

    def resolve(self, trade):
        current = self.service.get("admin", trade["trade_id"])
        dispute = current["disputes"][-1]
        current = self.call(trade, "resolution", {"dispute_id": dispute["dispute_id"], "terms": "SYNTHETIC mutual resolution"}, "admin")
        resolution = current["resolutions"][-1]
        payload = {"resolution_id": resolution["resolution_id"], "revision": resolution["revision"], "hash": resolution["hash"]}
        self.call(trade, "resolution_consent", payload)
        return self.call(trade, "resolution_consent", payload, "seller")

    def test_resolution_requires_fresh_settlement(self):
        trade, proposal = self.proposal(self.fund(W=0), [{"component": "D", "pay_won": 800000, "refund_won": 0}])
        self.call(trade, "dispute", {"scope": "D"})
        self.resolve(trade)
        self.assert_code("PROPOSAL_STALE", lambda: self.reserve(trade, proposal))
        trade, new_proposal = self.proposal(trade, [{"component": "D", "pay_won": 800000, "refund_won": 0}])
        self.reserve(trade, new_proposal)
        self.service.run_pending()
        self.assertEqual("CLOSED", self.call(trade, "close")["status"])

    def test_partial_success_retries_only_failed_sibling(self):
        trade = self.ready(self.fund())
        trade, proposal = self.proposal(trade, [{"component": "W", "pay_won": 50000, "refund_won": 30000}], "mutual_agreement")
        self.reserve(trade, proposal)
        SettlementWorker(self.db, lambda op: "SUCCEEDED" if op["direction"] == "PAY" else "FAILED").run_pending()
        current = self.assert_accounts(trade)
        failed = next(op for op in current["operations"] if op["status"] == "FAILED")
        success = next(op for op in current["operations"] if op["status"] == "SUCCEEDED")
        self.call(trade, "retry", {"operation_id": failed["operation_id"]})
        self.service.run_pending()
        current = self.assert_accounts(trade)
        self.assertEqual(1, next(op for op in current["operations"] if op["operation_id"] == success["operation_id"])["attempt_no"])
        self.assertEqual(2, next(op for op in current["operations"] if op["operation_id"] == failed["operation_id"])["attempt_no"])
        self.assertEqual(50000, current["balances"]["W"]["paid"])
        self.assertEqual(30000, current["balances"]["W"]["refunded"])

    def test_retry_job_dispute_cancels_attempt_preserves_prior_success(self):
        trade = self.ready(self.fund())
        trade, proposal = self.proposal(trade, [{"component": "W", "pay_won": 50000, "refund_won": 30000}], "mutual_agreement")
        self.reserve(trade, proposal)
        SettlementWorker(self.db, lambda op: "SUCCEEDED" if op["direction"] == "PAY" else "FAILED").run_pending()
        current = self.assert_accounts(trade)
        failed = next(op for op in current["operations"] if op["status"] == "FAILED")
        self.call(trade, "retry", {"operation_id": failed["operation_id"]})
        self.call(trade, "dispute", {"scope": "W"})
        current = self.assert_accounts(trade)
        self.assertEqual({"SUCCEEDED", "CANCELLED"}, {op["status"] for op in current["operations"]})
        self.resolve(trade)
        self.assertEqual(0, self.service.run_pending()["processed_jobs"])
        self.assertEqual(30000, self.assert_accounts(trade)["balances"]["W"]["held"])

    def test_max_attempts_and_integer_guards(self):
        self.assert_code("INVALID_AMOUNT", lambda: self.create(D=True))
        trade, proposal = self.proposal(self.fund(W=0), [{"component": "D", "pay_won": 800000, "refund_won": 0}])
        self.assert_code("TARGET_REVISION_MISMATCH", lambda: self.call(trade, "reserve", {"proposal_id": proposal["proposal_id"], "revision": True}))
        self.reserve(trade, proposal)
        worker = SettlementWorker(self.db, lambda op: "FAILED")
        for attempt in range(1, 4):
            worker.run_pending()
            current = self.assert_accounts(trade)
            op = current["operations"][0]
            self.assertEqual(attempt, op["attempt_no"])
            if attempt < 3:
                self.call(trade, "retry", {"operation_id": op["operation_id"]})
        self.assert_code("MAX_ATTEMPTS_REACHED", lambda: self.call(trade, "retry", {"operation_id": op["operation_id"]}))

    def test_configured_maximum_attempts_is_enforced_and_validated(self):
        for value in (True, False, 0, -1, 11, 1.0, "3", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                TradeService(self.db, max_settlement_attempts=value)
        self.service = TradeService(self.db, verified_review, max_settlement_attempts=1)
        trade, proposal = self.proposal(self.fund(W=0), [{"component": "D", "pay_won": 800000, "refund_won": 0}])
        self.reserve(trade, proposal)
        SettlementWorker(self.db, lambda op: "FAILED").run_pending()
        current = self.assert_accounts(trade)
        op = current["operations"][0]
        self.assert_code("MAX_ATTEMPTS_REACHED", lambda: self.call(trade, "retry", {"operation_id": op["operation_id"]}))
        self.assertEqual(current, self.assert_accounts(trade))

    def test_review_adoption_binds_new_contract_and_invalidates_consent_receipt(self):
        trade = self.fund()
        old_contract = trade["contract"]
        current = self.call(trade, "adopt_review", {"review_id": "SYNTHETIC:new-review"})
        self.assertEqual(2, current["contract"]["revision"])
        self.assertEqual(current["review_id"], current["contract"]["review_id"])
        self.assertNotEqual(old_contract["hash"], current["contract"]["hash"])
        self.assertEqual([], current["contract_consents"])
        self.assertFalse(current["receipt"]["received"])
        self.assertEqual(old_contract["D"], current["contract"]["D"])
        self.assertEqual(old_contract["W"], current["contract"]["W"])
        with self.db.connect() as conn:
            self.assertEqual(2, conn.execute("SELECT COUNT(*) FROM contract_revisions WHERE trade_id=?", (trade["trade_id"],)).fetchone()[0])

    def test_review_adoption_cannot_revise_contract_during_live_D_reservation(self):
        trade, proposal = self.proposal(self.fund(), [{"component": "D", "pay_won": 800000, "refund_won": 0}])
        self.reserve(trade, proposal)
        self.assert_code("ACTIVE_SETTLEMENT", lambda: self.call(trade, "adopt_review", {"review_id": "SYNTHETIC:changed"}))
        current = self.assert_accounts(trade)
        self.assertEqual(1, current["contract_revision"])
        self.assertEqual("RESERVED", current["operations"][0]["status"])
        self.service.run_pending()
        current = self.call(trade, "adopt_review", {"review_id": "SYNTHETIC:changed"})
        self.assertEqual(2, current["contract_revision"])
        self.assertEqual(800000, current["balances"]["D"]["paid"])

    def test_database_immutable_journal_and_revisions(self):
        trade = self.fund(W=0)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.transaction() as conn:
                conn.execute("UPDATE contract_revisions SET D=1 WHERE trade_id=?", (trade["trade_id"],))
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.transaction() as conn:
                conn.execute("DELETE FROM ledger_entries WHERE group_id=?", (trade["ledger"][0]["group_id"],))

    def test_funded_contract_terms_revision_preserves_money_renews_approvals(self):
        trade = self.fund()
        self.assert_code("FUNDED_CONTRACT_IMMUTABLE", lambda: self.call(trade, "contract", {"D": 1, "W": 80000, "terms": {}}))
        current = self.call(trade, "contract", {"D": 800000, "W": 80000, "terms": {"confirmation_deadline": "2026-12-01"}})
        self.assertEqual(2, current["contract_revision"])
        self.assertEqual([], current["contract_consents"])
        self.assertFalse(current["receipt"]["received"])
        self.assertEqual(800000, current["balances"]["D"]["held"])
        self.assertEqual(80000, current["balances"]["W"]["held"])

    def test_public_review_and_legacy_replay_omit_private_source_and_generated_text(self):
        private_text = {"excerpt": "SYNTHETIC_PRIVATE_EXCERPT_92",
                        "answer": "SYNTHETIC_PRIVATE_ANSWER_93",
                        "claim": "SYNTHETIC_PRIVATE_CLAIM_94",
                        "condition": "SYNTHETIC_PRIVATE_CONDITION_95"}
        review = verified_review("buyer", "SYNTHETIC:item", "SYNTHETIC:review")
        review.update({"answer": private_text["answer"],
                       "claims": [{"text": private_text["claim"]}],
                       "conditions": [{"condition": private_text["condition"]}],
                       "evidence": [{"evidence_id": "SYNTHETIC:evidence-id",
                                     "document_id": "SYNTHETIC:document-id",
                                     "version_id": "SYNTHETIC:version-id",
                                     "text": private_text["excerpt"],
                                     "title": private_text["claim"]}],
                       "document_hashes": ["a" * 64]})
        self.service.review_validator = lambda actor, item_id, review_id: review
        trade = self.fund()
        version = trade["version"]
        payload = {"review_id": "SYNTHETIC:review"}
        adopted = self.call(trade, "adopt_review", payload, key="adopt-private-review", version=version)
        fetched = self.service.get("seller", trade["trade_id"])
        replayed = self.service.mutate("buyer", trade["trade_id"], "adopt_review", payload, "adopt-private-review", version)
        for response in (adopted, fetched, replayed):
            encoded = canonical(response)
            for value in private_text.values():
                self.assertNotIn(value, encoded)
            projected = response["review"]
            self.assertEqual("TRANSFERABLE", projected["classification"])
            self.assertEqual(1, projected["applicability_ok"])
            self.assertEqual(["SYNTHETIC:evidence-id"], projected["evidence_ids"])
            self.assertEqual(["SYNTHETIC:document-id"], projected["document_ids"])
            self.assertEqual(["SYNTHETIC:version-id"], projected["document_version_ids"])
            self.assertEqual(["a" * 64], projected["document_hashes"])
            self.assertFalse({"snapshot", "payload_json", "answer", "evidence", "claims", "conditions"} & set(projected))
        with self.db.connect() as conn:
            stored = dict(conn.execute("SELECT * FROM review_snapshots WHERE review_id=?", (adopted["review_id"],)).fetchone())
        self.assertEqual(review, json.loads(stored["payload_json"]))
        legacy = dict(adopted)
        legacy["review"] = {**stored, "snapshot": review}
        legacy_payload = {"contract_revision": adopted["contract_revision"]}
        legacy_version = 1
        request_hash = digest({"actor": "buyer", "action": "receipt", "trade_id": trade["trade_id"],
                               "payload": legacy_payload, "expected_version": legacy_version})
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO idempotency_records(actor,scope,key,request_hash,response_json) VALUES('buyer','receipt',?,?,?)",
                         ("legacy-private-review", request_hash, canonical(legacy)))
        legacy_replay = self.service.mutate("buyer", trade["trade_id"], "receipt", legacy_payload,
                                           "legacy-private-review", legacy_version)
        self.assertEqual(adopted, legacy_replay)
        for value in private_text.values():
            self.assertNotIn(value, canonical(legacy_replay))
        with self.db.connect() as conn:
            retained = conn.execute("SELECT payload_json FROM review_snapshots WHERE review_id=?", (adopted["review_id"],)).fetchone()[0]
            retained_cache = conn.execute("SELECT response_json FROM idempotency_records WHERE key='legacy-private-review'").fetchone()[0]
        self.assertEqual(stored["payload_json"], retained)
        self.assertEqual(canonical(legacy), retained_cache)

    @staticmethod
    def conditional_review():
        review = verified_review("buyer", "SYNTHETIC:item", "SYNTHETIC:conditional")
        review.update({"classification": "CONDITIONAL", "conditions_ok": True,
                       "conditions": [
                           {"text": "SYNTHETIC private clause: first required procedure",
                            "evidence_ids": ["SYNTHETIC:terms"], "condition_id": "model-forged-first"},
                           {"text": "SYNTHETIC private clause: second required procedure",
                            "evidence_ids": ["SYNTHETIC:terms"], "condition_id": "model-forged-second"}],
                       "required_condition_ids": ["model-forged-first", "model-forged-second"]})
        return review

    def conditional_trade(self, review=None):
        review = self.conditional_review() if review is None else review
        self.service.review_validator = lambda actor, item_id, review_id: review
        trade = self.call(self.fund(), "adopt_review", {"review_id": "SYNTHETIC:conditional"})
        self.consent_contract(trade)
        return self.call(trade, "receipt", {"contract_revision": trade["contract_revision"]})

    def checked_evidence(self, trade, checks=None, both=True):
        payload = {"references": ["SYNTHETIC:conditional-proof"]}
        if checks is not None:
            payload["condition_checks"] = checks
        trade = self.call(trade, "evidence", payload)
        evidence = trade["evidence"]
        confirmation = {"evidence_id": evidence["evidence_id"], "hash": evidence["hash"]}
        self.call(trade, "confirm_evidence", confirmation)
        if both:
            trade = self.call(trade, "confirm_evidence", confirmation, "seller")
        return trade

    def assert_no_new_reservation(self, trade, proposal, code):
        before = self.assert_accounts(trade)
        self.assert_code(code, lambda: self.reserve(trade, proposal))
        after = self.assert_accounts(trade)
        self.assertEqual(before["operations"], after["operations"])
        self.assertEqual(before["groups"], after["groups"])
        self.assertEqual(before["ledger"], after["ledger"])
        self.assertEqual(before["balances"], after["balances"])

    def test_conditional_missing_and_false_attestations_cannot_reserve(self):
        trade = self.conditional_trade()
        self.assertEqual(0, trade["review"]["conditions_ok"])
        required = trade["review"]["required_condition_ids"]
        self.assertEqual(2, len(required))
        self.assertTrue(all(value.startswith("condition-") for value in required))
        self.assertNotIn("model-forged-first", required)
        with self.db.connect() as conn:
            normalized = json.loads(conn.execute("SELECT payload_json FROM review_snapshots WHERE review_id=?", (trade["review_id"],)).fetchone()[0])
        self.assertEqual(required, sorted(condition["condition_id"] for condition in normalized["conditions"]))
        self.assertFalse(normalized["conditions_ok"])
        trade = self.checked_evidence(trade)
        trade, proposal = self.proposal(trade, [{"component": "W", "pay_won": 80000, "refund_won": 0}])
        self.assert_no_new_reservation(trade, proposal, "W_CONDITIONS_NOT_CONFIRMED")
        checks = [{"condition_id": value, "confirmed": index != 0} for index, value in enumerate(required)]
        trade = self.checked_evidence(trade, checks)
        trade, proposal = self.proposal(trade, [{"component": "W", "pay_won": 80000, "refund_won": 0}])
        self.assert_no_new_reservation(trade, proposal, "W_CONDITIONS_NOT_CONFIRMED")

    def test_condition_checks_reject_foreign_duplicate_missing_and_nonboolean_values(self):
        trade = self.conditional_trade()
        required = trade["review"]["required_condition_ids"]
        good = [{"condition_id": value, "confirmed": True} for value in required]
        invalid = [good + [{"condition_id": "condition-" + "f" * 64, "confirmed": True}],
                   good + [good[0]], good[:-1], [],
                   [{**good[0], "confirmed": 1}, good[1]],
                   [{**good[0], "confirmed": "true"}, good[1]],
                   [{**good[0], "unexpected": True}, good[1]]]
        for checks in invalid:
            with self.subTest(checks=checks):
                before = self.assert_accounts(trade)
                self.assert_code("INVALID_CONDITION_CHECKS", lambda: self.call(trade, "evidence", {
                    "references": ["SYNTHETIC:bad-conditional-proof"], "condition_checks": checks}))
                after = self.assert_accounts(trade)
                self.assertEqual(before["version"], after["version"])
                self.assertEqual(before["evidence"], after["evidence"])
                self.assertEqual(before["balances"], after["balances"])
                self.assertEqual(before["ledger"], after["ledger"])
                self.assertEqual([], after["operations"])

    def test_conditional_all_checks_require_bilateral_latest_bundle_then_pay(self):
        trade = self.conditional_trade()
        required = trade["review"]["required_condition_ids"]
        checks = [{"condition_id": value, "confirmed": True} for value in required]
        trade = self.checked_evidence(trade, checks, both=False)
        evidence = trade["evidence"]
        self.assertEqual(checks, evidence["content"]["condition_checks"])
        trade, proposal = self.proposal(trade, [{"component": "W", "pay_won": 80000, "refund_won": 0}])
        self.assert_no_new_reservation(trade, proposal, "W_BILATERAL_EVIDENCE_REQUIRED")
        self.call(trade, "confirm_evidence", {"evidence_id": evidence["evidence_id"], "hash": evidence["hash"]}, "seller")
        self.reserve(trade, proposal)
        self.service.run_pending()
        current = self.assert_accounts(trade)
        self.assertEqual(80000, current["balances"]["W"]["paid"])
        for condition in self.conditional_review()["conditions"]:
            self.assertNotIn(condition["text"], canonical(current))
        self.assertEqual(required, current["review"]["required_condition_ids"])

    def test_empty_or_inconsistent_conditional_definitions_never_pass_vacuously(self):
        cases = []
        empty = self.conditional_review()
        empty["conditions"] = []
        cases.append(empty)
        foreign_evidence = self.conditional_review()
        foreign_evidence["conditions"][0]["evidence_ids"] = ["SYNTHETIC:other-evidence"]
        cases.append(foreign_evidence)
        inconsistent = self.conditional_review()
        inconsistent["classification"] = "TRANSFERABLE"
        cases.append(inconsistent)
        for review in cases:
            with self.subTest(classification=review["classification"], conditions=review["conditions"]):
                # Keep one wallet-independent transaction per definition case.
                trade = self.conditional_trade(review)
                self.assertEqual([], trade["review"]["required_condition_ids"])
                trade = self.checked_evidence(trade)
                trade, proposal = self.proposal(trade, [{"component": "W", "pay_won": 80000, "refund_won": 0}])
                self.assert_no_new_reservation(trade, proposal, "W_REQUIRED_CONDITIONS_MISSING")
                # Return this synthetic deposit so all cases can share the demo wallet.
                refund, refund_proposal = self.proposal(trade, [
                    {"component": "D", "pay_won": 0, "refund_won": 800000},
                    {"component": "W", "pay_won": 0, "refund_won": 80000}], "mutual_agreement")
                self.reserve(refund, refund_proposal)
                self.service.run_pending()

    def test_legacy_conditional_true_flag_still_needs_exact_human_attestations(self):
        trade = self.fund()
        legacy = self.conditional_review()
        with self.db.transaction() as conn:
            review_id = "SYNTHETIC:legacy-condition-review"
            conn.execute("INSERT INTO review_snapshots(review_id,trade_id,revision,hash,classification,applicability_ok,conditions_ok,payload_json) VALUES(?,?,1,?,'CONDITIONAL',1,1,?)",
                         (review_id, trade["trade_id"], digest(legacy), canonical(legacy)))
            body = {"item_id": trade["item_id"], "revision": 2, "D": 800000, "W": 80000,
                    "terms": trade["contract"]["terms"], "review_id": review_id}
            conn.execute("INSERT INTO contract_revisions(trade_id,revision,hash,item_id,D,W,terms_json,review_id) VALUES(?,2,?,?,800000,80000,?,?)",
                         (trade["trade_id"], digest(body), trade["item_id"], canonical(body["terms"]), review_id))
            conn.execute("UPDATE trades SET contract_revision=2,review_id=?,receipt_revision=NULL,version=version+1 WHERE trade_id=?", (review_id, trade["trade_id"]))
        trade = self.consent_contract(trade)
        trade = self.call(trade, "receipt", {"contract_revision": 2})
        self.assertEqual(0, trade["review"]["conditions_ok"])
        self.assertEqual(2, len(trade["review"]["required_condition_ids"]))
        trade = self.checked_evidence(trade)
        trade, proposal = self.proposal(trade, [{"component": "W", "pay_won": 80000, "refund_won": 0}])
        self.assert_no_new_reservation(trade, proposal, "W_CONDITIONS_NOT_CONFIRMED")
        with self.db.connect() as conn:
            self.assertEqual(1, conn.execute("SELECT conditions_ok FROM review_snapshots WHERE review_id=?", (review_id,)).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
