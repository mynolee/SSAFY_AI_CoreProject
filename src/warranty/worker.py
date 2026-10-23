"""Local atomic settlement worker and trusted fault-injection reconciliation."""
from __future__ import annotations

import json

from .errors import ServiceError
from .transactions import TradeService, audit, balance_move, identifier, post_ledger, verify_accounting


def update_group(conn, group_id):
    states = [r[0] for r in conn.execute("SELECT status FROM settlement_operations WHERE group_id=?", (group_id,))]
    if not states:
        return
    if all(x == states[0] for x in states):
        state = states[0]
    elif "UNKNOWN" in states or "PROCESSING" in states:
        state = "UNKNOWN"
    elif "RESERVED" in states:
        state = "RESERVED"
    else:
        state = "PARTIAL"
    conn.execute("UPDATE settlement_groups SET status=? WHERE group_id=?", (state, group_id))


def finish_operation(conn, op, outcome):
    """Called only with a trusted outcome and a matching live attempt."""
    if outcome == "UNKNOWN":
        conn.execute("UPDATE settlement_operations SET status='UNKNOWN' WHERE operation_id=?", (op["operation_id"],))
        return
    if outcome == "FAILED":
        TradeService._release(conn, op, "FAILED")
        return
    if outcome != "SUCCEEDED":
        raise RuntimeError("Invalid trusted simulator outcome")
    tid, comp, amount = op["trade_id"], op["component"], op["amount"]
    source = "pay_reserved" if op["direction"] == "PAY" else "refund_reserved"
    target = "paid" if op["direction"] == "PAY" else "refunded"
    trade = conn.execute("SELECT buyer_id,seller_id FROM trades WHERE trade_id=?", (tid,)).fetchone()
    beneficiary = trade["seller_id"] if op["direction"] == "PAY" else trade["buyer_id"]
    balance_move(conn, tid, comp, source, target, amount)
    conn.execute("UPDATE demo_wallets SET available=available+? WHERE user_id=?", (amount, beneficiary))
    post_ledger(conn, tid, "SETTLE", amount, f"wallet:{beneficiary}", f"escrow:{tid}:{comp}:{source}", op["operation_id"], op["attempt_no"])
    conn.execute("UPDATE settlement_operations SET status='SUCCEEDED' WHERE operation_id=?", (op["operation_id"],))


def reconcile_operation(conn, op, actor):
    events = conn.execute("SELECT * FROM simulation_events WHERE operation_id=? AND applied=0 ORDER BY rowid", (op["operation_id"],)).fetchall()
    changed = False
    current = op
    for event in events:
        if event["attempt_no"] != current["attempt_no"] or current["status"] != "UNKNOWN":
            audit(conn, op["trade_id"], actor, "late_simulation_event", op["operation_id"], "AUDIT_ONLY")
        elif event["outcome"] in ("SUCCEEDED", "FAILED"):
            finish_operation(conn, current, event["outcome"])
            changed = True
            audit(conn, op["trade_id"], actor, "reconcile", op["operation_id"], event["outcome"])
            current = conn.execute("SELECT * FROM settlement_operations WHERE operation_id=?", (op["operation_id"],)).fetchone()
        else:
            audit(conn, op["trade_id"], actor, "reconcile", op["operation_id"], "STILL_UNKNOWN")
        conn.execute("UPDATE simulation_events SET applied=1 WHERE event_id=?", (event["event_id"],))
    if changed:
        update_group(conn, op["group_id"])
        verify_accounting(conn, op["trade_id"])
    return changed


class SettlementWorker:
    """No public payload can choose an outcome.

    outcome_provider is a server-side test adapter taking an operation dict. Its
    invocation occurs before BEGIN IMMEDIATE. Default outcomes always succeed.
    """

    def __init__(self, db, outcome_provider=None):
        self.db = db
        self.outcome_provider = outcome_provider
        self.fail_after_claim = False  # Explicit local test failpoint; never read from API input.

    def recover_processing(self):
        with self.db.transaction() as conn:
            rows = conn.execute("SELECT * FROM settlement_operations WHERE status='PROCESSING'").fetchall()
            trades, groups = set(), set()
            for op in rows:
                conn.execute("UPDATE settlement_operations SET status='UNKNOWN' WHERE operation_id=? AND status='PROCESSING'", (op["operation_id"],))
                trades.add(op["trade_id"])
                groups.add(op["group_id"])
                audit(conn, op["trade_id"], "worker", "recovery", op["operation_id"], "UNKNOWN_RESERVATION_PRESERVED")
            recovered = {(op["operation_id"], op["attempt_no"]) for op in rows}
            for gid in groups:
                # Retry jobs share the original group, but own separate attempts.
                # Recover only jobs that actually claimed a recovered attempt.
                for job in conn.execute("SELECT * FROM jobs WHERE group_id=? AND status IN ('PENDING','PROCESSING')", (gid,)):
                    if recovered.intersection(tuple(attempt) for attempt in json.loads(job["attempts_json"])):
                        conn.execute("UPDATE jobs SET status='UNKNOWN' WHERE job_id=?", (job["job_id"],))
                update_group(conn, gid)
            for tid in trades:
                conn.execute("UPDATE trades SET version=version+1 WHERE trade_id=?", (tid,))
            return len(rows)

    @staticmethod
    def _has_uncertain_component(conn, operations):
        components = {(op["trade_id"], op["component"]) for op in operations}
        return any(conn.execute(
            "SELECT 1 FROM settlement_operations WHERE trade_id=? AND component=? "
            "AND status IN ('PROCESSING','UNKNOWN') LIMIT 1", (trade_id, component)
        ).fetchone() for trade_id, component in components)

    def run_pending(self):
        self.recover_processing()
        with self.db.connect() as conn:
            jobs = [dict(r) for r in conn.execute("SELECT * FROM jobs WHERE status='PENDING' ORDER BY rowid")]
        processed, operations = 0, []
        for job in jobs:
            attempts = json.loads(job["attempts_json"])
            with self.db.connect() as conn:
                snapshots = []
                for oid, attempt in attempts:
                    op = conn.execute("SELECT * FROM settlement_operations WHERE operation_id=? AND attempt_no=? AND status='RESERVED'", (oid, attempt)).fetchone()
                    if op is not None:
                        snapshots.append(dict(op))
                blocked = self._has_uncertain_component(conn, snapshots)
            if len(snapshots) != len(attempts) or blocked:
                continue
            # Fault adapters are isolated from the open write transaction.
            outcomes = {}
            for op in snapshots:
                outcome = self.outcome_provider(dict(op)) if self.outcome_provider else "SUCCEEDED"
                if outcome not in ("SUCCEEDED", "FAILED", "UNKNOWN"):
                    raise ValueError("Fault adapter must return SUCCEEDED, FAILED or UNKNOWN")
                outcomes[op["operation_id"]] = outcome
            with self.db.transaction() as conn:
                current_job = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone()
                if current_job is None or current_job["status"] != "PENDING":
                    continue
                current = []
                for oid, attempt in attempts:
                    op = conn.execute("SELECT * FROM settlement_operations WHERE trade_id=? AND group_id=? AND operation_id=? AND attempt_no=? AND status='RESERVED'", (job["trade_id"], job["group_id"], oid, attempt)).fetchone()
                    if op is not None:
                        current.append(op)
                if len(current) != len(attempts):
                    continue
                if self._has_uncertain_component(conn, current):
                    continue
                if any(conn.execute("SELECT 1 FROM disputes WHERE trade_id=? AND status<>'RESOLVED' AND scope IN (?, 'BOTH')", (op["trade_id"], op["component"])).fetchone() for op in current):
                    continue
                conn.execute("UPDATE jobs SET status='PROCESSING' WHERE job_id=? AND status='PENDING'", (job["job_id"],))
                for op in current:
                    conn.execute("UPDATE settlement_operations SET status='PROCESSING' WHERE operation_id=? AND status='RESERVED' AND attempt_no=?", (op["operation_id"], op["attempt_no"]))
                if self.fail_after_claim:
                    raise RuntimeError("Injected interruption before atomic worker commit")
                for op in current:
                    outcome = outcomes[op["operation_id"]]
                    eid = identifier()
                    conn.execute("INSERT INTO simulation_events(event_id,operation_id,attempt_no,outcome,applied) VALUES(?,?,?,?,1)", (eid, op["operation_id"], op["attempt_no"], outcome))
                    finish_operation(conn, op, outcome)
                    audit(conn, op["trade_id"], "worker", "settle", op["operation_id"], outcome)
                    operations.append({"operation_id": op["operation_id"], "attempt_no": op["attempt_no"], "status": outcome})
                update_group(conn, job["group_id"])
                verify_accounting(conn, job["trade_id"])
                conn.execute("UPDATE jobs SET status='DONE' WHERE job_id=?", (job["job_id"],))
                conn.execute("UPDATE trades SET version=version+1 WHERE trade_id=?", (job["trade_id"],))
                processed += 1
        return {"processed_jobs": processed, "operations": operations, "simulation": True}

    def record_event(self, operation_id, attempt_no, outcome, event_id=None):
        """Trusted test/recovery input, intentionally absent from HTTP/UI actions."""
        if type(attempt_no) is not int or attempt_no < 1 or outcome not in ("SUCCEEDED", "FAILED", "UNKNOWN", "DELAYED"):
            raise ValueError("Invalid trusted simulator event")
        with self.db.transaction() as conn:
            op = conn.execute("SELECT * FROM settlement_operations WHERE operation_id=?", (operation_id,)).fetchone()
            if op is None:
                raise ServiceError("OPERATION_NOT_FOUND", "The operation does not exist.", 404)
            eid = event_id or identifier()
            prior = conn.execute("SELECT * FROM simulation_events WHERE event_id=?", (eid,)).fetchone()
            if prior is not None:
                if prior["operation_id"] != operation_id or prior["attempt_no"] != attempt_no or prior["outcome"] != outcome:
                    raise ServiceError("EVENT_CONFLICT", "This event identifier already has another meaning.", 409)
                return eid
            stale = op["attempt_no"] != attempt_no or op["status"] not in ("UNKNOWN", "PROCESSING")
            conn.execute("INSERT INTO simulation_events(event_id,operation_id,attempt_no,outcome,applied) VALUES(?,?,?,?,?)", (eid, operation_id, attempt_no, outcome, int(stale)))
            if stale:
                audit(conn, op["trade_id"], "worker", "late_simulation_event", operation_id, "AUDIT_ONLY")
            return eid
