"""Atomic local demo contracts and settlements; no payment or insurer integration."""
from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import nullcontext

from .errors import ServiceError


MAX_SETTLEMENT_ATTEMPTS = 3
LIVE = ("RESERVED", "PROCESSING", "UNKNOWN")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def identifier():
    return uuid.uuid4().hex


def fail(code, message="The requested transaction conditions are not satisfied.", status=422):
    raise ServiceError(code, message, status)


def money(value, positive=False):
    if type(value) is not int or value < (1 if positive else 0) or value > 10**12:
        fail("INVALID_AMOUNT", "Amounts must be nonnegative whole won within the demo limit.", 400)
    return value


def rowdict(row):
    return dict(row) if row is not None else None


def review_condition_ids(snapshot):
    """Derive required IDs from server-stored clauses, never supplied ID fields.

    None means the conditional definition is incomplete or inconsistent. An
    unconditional review has no required clauses and returns an empty list.
    """
    if not isinstance(snapshot, dict):
        return None
    conditions = snapshot.get("conditions", [])
    if snapshot.get("classification") != "CONDITIONAL":
        return [] if not conditions else None
    if not isinstance(conditions, list) or not conditions or len(conditions) > 100:
        return None
    evidence = snapshot.get("evidence", [])
    if not isinstance(evidence, list):
        return None
    known_ids = {entry.get("evidence_id", entry.get("id")) for entry in evidence
                 if isinstance(entry, dict) and entry.get("source_type") != "metadata"
                 and isinstance(entry.get("evidence_id", entry.get("id")), str)}
    required = []
    for condition in conditions:
        if not isinstance(condition, dict):
            return None
        text, evidence_ids = condition.get("text"), condition.get("evidence_ids")
        if (not isinstance(text, str) or not text.strip() or len(text) > 10000 or
                not isinstance(evidence_ids, list) or not evidence_ids or
                any(not isinstance(value, str) or value not in known_ids for value in evidence_ids) or
                len(set(evidence_ids)) != len(evidence_ids)):
            return None
        condition_id = "condition-" + digest({"text": text, "evidence_ids": sorted(evidence_ids)})
        if condition_id in required:
            return None
        required.append(condition_id)
    return sorted(required)


def condition_checks(values, required, *, exact=True):
    """Validate explicit party attestations; false is data, never a default true."""
    if not isinstance(values, list) or len(values) > 100:
        fail("INVALID_CONDITION_CHECKS", "Condition checks must be an explicit list.", 400)
    checks = {}
    for check in values:
        if (not isinstance(check, dict) or set(check) != {"condition_id", "confirmed"} or
                not isinstance(check.get("condition_id"), str) or type(check.get("confirmed")) is not bool or
                check["condition_id"] in checks or check["condition_id"] not in required):
            fail("INVALID_CONDITION_CHECKS", "Use each required condition ID once with an explicit boolean.", 400)
        checks[check["condition_id"]] = check["confirmed"]
    if exact and set(checks) != set(required):
        fail("INVALID_CONDITION_CHECKS", "Condition checks must identify the exact required conditions.", 400)
    return [{"condition_id": key, "confirmed": checks[key]} for key in sorted(checks)]


def public_review(review):
    """Project immutable private reviews without redisplaying source excerpts.

    This pure projection also handles pre-existing idempotency responses. Source
    permissions may change after adoption; source text and generated prose are
    therefore never part of the transaction service's public review DTO.
    """
    if not isinstance(review, dict):
        return None
    result = {key: review[key] for key in ("review_id", "trade_id", "revision", "hash",
              "classification", "applicability_ok", "conditions_ok", "created_at") if key in review}
    if result.get("classification") == "CONDITIONAL":
        result["conditions_ok"] = 0
    snapshot = review.get("snapshot")
    if not isinstance(snapshot, dict):
        try:
            snapshot = json.loads(review.get("payload_json", "{}"))
        except (TypeError, ValueError):
            snapshot = {}
    if not isinstance(snapshot, dict):
        snapshot = {}
    values = {}
    for field in ("document_ids", "document_version_ids", "document_hashes", "evidence_ids"):
        values[field] = set()
        for source in (review, snapshot):
            entries = source.get(field, [])
            if isinstance(entries, list):
                values[field].update(value for value in entries if isinstance(value, str))
    evidence = snapshot.get("evidence", [])
    if isinstance(evidence, list):
        for entry in evidence:
            if not isinstance(entry, dict):
                continue
            for field, keys in (("document_ids", ("document_id",)),
                                ("document_version_ids", ("document_version_id", "version_id")),
                                ("evidence_ids", ("evidence_id", "id"))):
                for key in keys:
                    if isinstance(entry.get(key), str):
                        values[field].add(entry[key])
    if "conditions" in snapshot or snapshot.get("classification") == "CONDITIONAL":
        required = review_condition_ids(snapshot) or []
    else:
        # A newer cached response already contains only this server-derived DTO.
        required = review.get("required_condition_ids", [])
        if not isinstance(required, list):
            required = []
        required = sorted({value for value in required if isinstance(value, str)
                           and value.startswith("condition-") and len(value) == 74
                           and all(c in "0123456789abcdef" for c in value[10:])})
    result["required_condition_ids"] = required
    return {**result, **{key: sorted(entries) for key, entries in values.items()}}


def public_trade_response(response):
    """Apply current disclosure rules to freshly built and cached trade views."""
    projected = dict(response)
    projected["review"] = public_review(response.get("review"))
    return projected


def post_ledger(conn, trade_id, event_type, amount, debit, credit, operation_id=None, attempt_no=1):
    """Create and seal a balanced append-only journal inside the caller transaction."""
    group_id = identifier()
    conn.execute("INSERT INTO ledger_groups(group_id,trade_id,operation_id,attempt_no,event_type) VALUES(?,?,?,?,?)",
                 (group_id, trade_id, operation_id, attempt_no, event_type))
    conn.executemany("INSERT INTO ledger_entries(group_id,line_no,account,debit,credit) VALUES(?,?,?,?,?)",
                     [(group_id, 1, debit, amount, 0), (group_id, 2, credit, 0, amount)])
    conn.execute("UPDATE ledger_groups SET posted=1 WHERE group_id=?", (group_id,))
    return group_id


def audit(conn, trade_id, actor, action, target_id, code):
    conn.execute("INSERT INTO audit_events(event_id,trade_id,actor,action,target_id,code) VALUES(?,?,?,?,?,?)",
                 (identifier(), trade_id, actor, action, target_id, code))


def balance_move(conn, trade_id, component, source, target, amount):
    allowed = {"held", "pay_reserved", "refund_reserved", "paid", "refunded"}
    if source not in allowed or target not in allowed or source == target:
        raise RuntimeError("Invalid internal balance movement")
    changed = conn.execute(
        f"UPDATE component_balances SET {source}={source}-?,{target}={target}+? "
        f"WHERE trade_id=? AND component=? AND {source}>=?",
        (amount, amount, trade_id, component, amount)).rowcount
    if changed != 1:
        fail("INSUFFICIENT_HELD", "The requested amount exceeds the available held funds.", 409)


def verify_accounting(conn, trade_id):
    """Cached display balances must agree with the sealed journal, not vice versa."""
    for balance in conn.execute("SELECT * FROM component_balances WHERE trade_id=?", (trade_id,)):
        comp = balance["component"]
        deposited = conn.execute("SELECT COALESCE(SUM(e.debit),0) FROM ledger_entries e JOIN ledger_groups g ON g.group_id=e.group_id WHERE g.trade_id=? AND g.event_type='DEPOSIT' AND g.posted=1 AND e.account=?", (trade_id, f"escrow:{trade_id}:{comp}:held")).fetchone()[0]
        if deposited != balance["deposited"]:
            fail("ACCOUNTING_INVARIANT", "The demo deposit and journal disagree.", 409)
        for bucket in ("held", "pay_reserved", "refund_reserved"):
            net = conn.execute("SELECT COALESCE(SUM(e.debit-e.credit),0) FROM ledger_entries e JOIN ledger_groups g ON g.group_id=e.group_id WHERE g.trade_id=? AND g.posted=1 AND e.account=?", (trade_id, f"escrow:{trade_id}:{comp}:{bucket}")).fetchone()[0]
            if net != balance[bucket]:
                fail("ACCOUNTING_INVARIANT", "The demo journal and account balances disagree.", 409)
        for direction, bucket in (("PAY", "paid"), ("REFUND", "refunded")):
            settled = conn.execute("SELECT COALESCE(SUM(o.amount),0) FROM settlement_operations o JOIN ledger_groups g ON g.operation_id=o.operation_id AND g.attempt_no=o.attempt_no AND g.event_type='SETTLE' AND g.posted=1 WHERE o.trade_id=? AND o.component=? AND o.direction=?", (trade_id, comp, direction)).fetchone()[0]
            if settled != balance[bucket]:
                fail("ACCOUNTING_INVARIANT", "The demo journal and final allocations disagree.", 409)


class TradeService:
    """All identities are server identities, never identities embedded in payloads."""

    def __init__(self, db, review_validator=None, review_guard=None):
        self.db = db
        self.review_validator = review_validator
        validator_owner = getattr(review_validator, "__self__", None)
        self.review_guard = review_guard or getattr(validator_owner, "review_guard", None)
        from .worker import SettlementWorker
        self.worker = SettlementWorker(db)

    def run_pending(self):
        return self.worker.run_pending()

    @staticmethod
    def _actor(conn, actor):
        user = conn.execute("SELECT * FROM demo_users WHERE user_id=?", (actor,)).fetchone()
        if user is None:
            fail("AUTHENTICATION_REQUIRED", "A server demo session is required.", 401)
        return user

    def _trade(self, conn, actor, trade_id):
        self._actor(conn, actor)
        trade = conn.execute("SELECT * FROM trades WHERE trade_id=?", (trade_id,)).fetchone()
        if trade is None or actor not in (trade["buyer_id"], trade["seller_id"], trade["admin_id"]):
            fail("TRADE_NOT_FOUND", "The trade is unavailable to this session.", 404)
        return trade

    @staticmethod
    def _party(trade, actor):
        if actor not in (trade["buyer_id"], trade["seller_id"]):
            fail("PARTY_REQUIRED", "Only the trade parties may give this approval.", 403)

    @staticmethod
    def _contract(conn, trade):
        return conn.execute("SELECT * FROM contract_revisions WHERE trade_id=? AND revision=?",
                            (trade["trade_id"], trade["contract_revision"])).fetchone()

    @staticmethod
    def _terms(terms):
        defaults = {"currency": "KRW", "settlement_unit": 1,
                    "evidence_standard": "SYNTHETIC bilateral confirmation",
                    "confirmation_deadline": None, "return_conditions": ["mutual_agreement"],
                    "dispute_rule": "assigned administrator proposal and bilateral consent"}
        if not isinstance(terms, dict):
            fail("INVALID_TERMS", status=400)
        allowed = set(defaults)
        if set(terms) - allowed:
            fail("INVALID_TERMS", "Only supported contract terms may be supplied.", 400)
        defaults.update(terms)
        if defaults["currency"] != "KRW" or type(defaults["settlement_unit"]) is not int or defaults["settlement_unit"] != 1:
            fail("INVALID_TERMS", status=400)
        conditions = defaults["return_conditions"]
        if not isinstance(conditions, list) or not conditions or any(not isinstance(x, str) or not x.strip() or len(x) > 500 for x in conditions):
            fail("INVALID_RETURN_CONDITIONS", status=400)
        for key in ("evidence_standard", "dispute_rule"):
            if not isinstance(defaults[key], str) or not defaults[key].strip() or len(defaults[key]) > 2000:
                fail("INVALID_TERMS", status=400)
        deadline = defaults["confirmation_deadline"]
        if deadline is not None and (not isinstance(deadline, str) or len(deadline) > 100):
            fail("INVALID_TERMS", status=400)
        return defaults

    def create(self, actor, buyer_id, seller_id, item_id, D, W, **terms):
        money(D, True)
        money(W)
        terms = self._terms(terms)
        with self.db.transaction() as conn:
            self._actor(conn, actor)
            users = {r["user_id"]: r["role"] for r in conn.execute("SELECT user_id,role FROM demo_users")}
            if actor not in (buyer_id, seller_id) or users.get(buyer_id) != "buyer" or users.get(seller_id) != "seller" or buyer_id == seller_id:
                fail("INVALID_PARTIES", "Create a trade using the server's buyer and seller identities.", 403)
            if not isinstance(item_id, str) or not conn.execute("SELECT 1 FROM items WHERE item_id=?", (item_id,)).fetchone():
                fail("ITEM_NOT_FOUND", "Select an existing catalog item.", 404)
            trade_id = identifier()
            body = {"item_id": item_id, "revision": 1, "D": D, "W": W, "terms": terms, "review_id": None}
            conn.execute("INSERT INTO trades(trade_id,buyer_id,seller_id,admin_id,item_id,status,version,contract_revision) VALUES(?,?,?,?,?,'DRAFT',1,1)",
                         (trade_id, buyer_id, seller_id, "admin", item_id))
            conn.execute("INSERT INTO contract_revisions(trade_id,revision,hash,item_id,D,W,terms_json) VALUES(?,1,?,?,?,?,?)",
                         (trade_id, digest(body), item_id, D, W, canonical(terms)))
            for component in ("D", "W"):
                conn.execute("INSERT INTO component_balances(trade_id,component,deposited,held,pay_reserved,refund_reserved,paid,refunded) VALUES(?,?,0,0,0,0,0,0)", (trade_id, component))
            audit(conn, trade_id, actor, "create", trade_id, "SYNTHETIC_CREATED")
            return self._view(conn, actor, trade_id)

    def get(self, actor, trade_id):
        with self.db.connect() as conn:
            self._trade(conn, actor, trade_id)
            return self._view(conn, actor, trade_id)

    def get_operation(self, actor, operation_id):
        with self.db.connect() as conn:
            op = conn.execute("SELECT * FROM settlement_operations WHERE operation_id=?", (operation_id,)).fetchone()
            if op is None:
                self._actor(conn, actor)
                fail("OPERATION_NOT_FOUND", status=404)
            self._trade(conn, actor, op["trade_id"])
            return dict(op)

    def _view(self, conn, actor, trade_id):
        trade = dict(self._trade(conn, actor, trade_id))
        verify_accounting(conn, trade_id)
        contract = dict(self._contract(conn, trade))
        contract["terms"] = json.loads(contract.pop("terms_json"))
        trade["contract"] = contract
        trade["simulation"] = True
        trade["balances"] = {r["component"]: dict(r) for r in conn.execute("SELECT * FROM component_balances WHERE trade_id=?", (trade_id,))}
        trade["contract_consents"] = [dict(r) for r in conn.execute("SELECT * FROM consents WHERE trade_id=? AND target_type='contract' AND revision=?", (trade_id, trade["contract_revision"]))]
        trade["receipt"] = {"received": trade["receipt_revision"] == trade["contract_revision"], "contract_revision": trade["receipt_revision"]}
        trade["review"] = rowdict(conn.execute("SELECT * FROM review_snapshots WHERE trade_id=? AND review_id=?", (trade_id, trade["review_id"])).fetchone())
        trade["review"] = public_review(trade["review"])
        trade["evidence"] = rowdict(conn.execute("SELECT * FROM evidence_bundles WHERE trade_id=? AND evidence_id=?", (trade_id, trade["evidence_id"])).fetchone())
        if trade["evidence"]:
            trade["evidence"]["content"] = json.loads(trade["evidence"].pop("payload_json"))
        trade["evidence_confirmations"] = [dict(r) for r in conn.execute("SELECT * FROM evidence_confirmations WHERE trade_id=? AND evidence_id=?", (trade_id, trade["evidence_id"]))]
        latest_transfer = conn.execute("SELECT status FROM transfer_events WHERE trade_id=? ORDER BY rowid DESC LIMIT 1", (trade_id,)).fetchone()
        trade["transfer_status"] = "PARTIES_CONFIRMED" if len(trade["evidence_confirmations"]) == 2 else (latest_transfer[0] if latest_transfer else "NOT_STARTED")
        proposals = []
        for row in conn.execute("SELECT * FROM settlement_proposals WHERE trade_id=? ORDER BY rowid", (trade_id,)):
            proposal = dict(row)
            proposal["conditions"] = json.loads(proposal.pop("payload_json"))
            proposal["allocations"] = [dict(a) for a in conn.execute("SELECT * FROM allocations WHERE proposal_id=?", (row["proposal_id"],))]
            proposal["consents"] = [dict(a) for a in conn.execute("SELECT * FROM consents WHERE trade_id=? AND target_type='proposal' AND target_id=? AND revision=?", (trade_id, row["proposal_id"], row["revision"]))]
            proposals.append(proposal)
        trade["proposals"] = proposals
        trade["operations"] = [dict(r) for r in conn.execute("SELECT * FROM settlement_operations WHERE trade_id=? ORDER BY rowid", (trade_id,))]
        trade["groups"] = [dict(r) for r in conn.execute("SELECT * FROM settlement_groups WHERE trade_id=? ORDER BY rowid", (trade_id,))]
        trade["disputes"] = [dict(r) for r in conn.execute("SELECT * FROM disputes WHERE trade_id=? ORDER BY rowid", (trade_id,))]
        trade["resolutions"] = []
        for row in conn.execute("SELECT * FROM resolution_revisions WHERE trade_id=? ORDER BY rowid", (trade_id,)):
            resolution = dict(row)
            resolution["content"] = json.loads(resolution.pop("payload_json"))
            resolution["consents"] = [dict(a) for a in conn.execute("SELECT * FROM consents WHERE trade_id=? AND target_type='resolution' AND target_id=? AND revision=?", (trade_id, row["resolution_id"], row["revision"]))]
            trade["resolutions"].append(resolution)
        trade["ledger"] = [dict(r) for r in conn.execute("SELECT g.*,e.line_no,e.account,e.debit,e.credit FROM ledger_groups g JOIN ledger_entries e ON e.group_id=g.group_id WHERE g.trade_id=? ORDER BY g.rowid,e.line_no", (trade_id,))]
        trade["wallet"] = rowdict(conn.execute("SELECT * FROM demo_wallets WHERE user_id=?", (actor,)).fetchone())
        return trade

    _FIELDS = {
        "contract": {"D", "W", "terms"}, "contract_consent": {"revision", "hash"},
        "deposit": set(), "receipt": {"contract_revision"}, "adopt_review": {"review_id"},
        "evidence": {"references", "description", "condition_checks"}, "confirm_evidence": {"evidence_id", "hash"},
        "proposal": {"allocations", "review_id", "evidence_id", "refund_reason"},
        "proposal_consent": {"proposal_id", "revision", "hash"}, "reserve": {"proposal_id", "revision"},
        "retry": {"operation_id"}, "reconcile": {"operation_id"}, "dispute": {"scope"},
        "resolution": {"dispute_id", "terms"}, "resolution_consent": {"resolution_id", "revision", "hash"},
        "close": set(), "cancel_consent": {"revision", "hash"}, "transfer_event": {"status"},
    }

    def mutate(self, actor, trade_id, action, payload, key, expected_version):
        if action not in self._FIELDS or not isinstance(payload, dict) or set(payload) - self._FIELDS[action]:
            fail("INVALID_ACTION_PAYLOAD", "Unsupported action or payload fields.", 400)
        if not isinstance(key, str) or not key.strip() or len(key) > 200 or type(expected_version) is not int:
            fail("MUTATION_PRECONDITIONS_REQUIRED", "An idempotency key and integer expected version are required.", 400)
        try:
            request_hash = digest({"actor": actor, "action": action, "trade_id": trade_id,
                                   "payload": payload, "expected_version": expected_version})
        except (TypeError, ValueError):
            fail("INVALID_ACTION_PAYLOAD", "Payload must contain valid JSON values.", 400)
        target = self._current_review_target(actor, trade_id, action, payload, key)
        guard = self.review_guard() if target is not None and self.review_guard is not None else nullcontext()
        with guard:
            current_review = self._revalidate_review(target)
            return self._mutate_guarded(actor, trade_id, action, payload, key, expected_version,
                                        request_hash, current_review)

    def _mutate_guarded(self, actor, trade_id, action, payload, key, expected_version,
                        request_hash, current_review):
        validated_review = None
        # No model, HTTP, file inspection or application callback inside the write transaction.
        if action == "adopt_review":
            with self.db.connect() as conn:
                self._trade(conn, actor, trade_id)
                prior = conn.execute("SELECT request_hash FROM idempotency_records WHERE actor=? AND scope=? AND key=?", (actor, action, key)).fetchone()
            if prior is None:
                if self.review_validator is None or not isinstance(payload.get("review_id"), str):
                    fail("REVIEW_NOT_VERIFIED", "A persisted server-validated review is required.")
                with self.db.connect() as conn:
                    item_id = self._trade(conn, actor, trade_id)["item_id"]
                validated_review = self.review_validator(actor, item_id, payload["review_id"])
        with self.db.transaction() as conn:
            trade = self._trade(conn, actor, trade_id)
            previous = conn.execute("SELECT * FROM idempotency_records WHERE actor=? AND scope=? AND key=?", (actor, action, key)).fetchone()
            if previous:
                if previous["request_hash"] != request_hash:
                    fail("IDEMPOTENCY_CONFLICT", "This key was already used for a different request.", 409)
                return public_trade_response(json.loads(previous["response_json"]))
            # A new key for the same logical allocation must find it before the version guard.
            if action == "reserve":
                proposal = self._proposal(conn, trade, payload.get("proposal_id"))
                self._party(trade, actor)
                if type(payload.get("revision")) is not int or payload.get("revision") != proposal["revision"]:
                    fail("TARGET_REVISION_MISMATCH", status=409)
                existing = conn.execute("SELECT 1 FROM settlement_groups WHERE proposal_id=? AND revision=?", (proposal["proposal_id"], proposal["revision"])).fetchone()
                if existing:
                    response = self._view(conn, actor, trade_id)
                    self._remember(conn, actor, action, key, request_hash, response)
                    return response
            if trade["status"] in ("CLOSED", "CANCELLED"):
                fail("TRADE_TERMINAL", "This trade has ended.", 409)
            if trade["version"] != expected_version:
                fail("VERSION_CONFLICT", "Reload the current trade before submitting a new change.", 409)
            changed = self._apply(conn, trade, actor, action, payload, validated_review, current_review)
            if changed:
                conn.execute("UPDATE trades SET version=version+1 WHERE trade_id=?", (trade_id,))
                audit(conn, trade_id, actor, action, trade_id, "ACCEPTED")
            response = self._view(conn, actor, trade_id)
            self._remember(conn, actor, action, key, request_hash, response)
            return response

    def _current_review_target(self, actor, trade_id, action, payload, key):
        """Find a *new* W/PAY target before taking the policy/readiness guard."""
        if action not in ("reserve", "retry"):
            return None
        with self.db.connect() as conn:
            trade = self._trade(conn, actor, trade_id)
            self._party(trade, actor)
            if conn.execute("SELECT 1 FROM idempotency_records WHERE actor=? AND scope=? AND key=?",
                            (actor, action, key)).fetchone():
                return None
            if action == "reserve":
                proposal = self._proposal(conn, trade, payload.get("proposal_id"))
                if conn.execute("SELECT 1 FROM settlement_groups WHERE proposal_id=? AND revision=?",
                                (proposal["proposal_id"], proposal["revision"])).fetchone():
                    return None
                positive = conn.execute("SELECT 1 FROM allocations WHERE proposal_id=? AND component='W' AND pay_won>0",
                                        (proposal["proposal_id"],)).fetchone()
            else:
                operation = self._operation(conn, trade, payload.get("operation_id"))
                positive = operation["status"] == "FAILED" and operation["component"] == "W" and operation["direction"] == "PAY"
                proposal = self._proposal(conn, trade, operation["proposal_id"])
            if not positive:
                return None
            review = conn.execute("SELECT * FROM review_snapshots WHERE trade_id=? AND review_id=?",
                                  (trade_id, proposal["review_id"])).fetchone()
            if review is None:
                return None  # The ordinary domain gate supplies the precise error.
            snapshot = json.loads(review["payload_json"])
            adopter = conn.execute("SELECT actor FROM audit_events WHERE trade_id=? AND action='adopt_review_snapshot' AND target_id=? ORDER BY rowid DESC LIMIT 1",
                                   (trade_id, review["review_id"])).fetchone()
            if adopter is None:
                # Legacy immutable snapshots predate the adoption audit target.
                # Resolve ownership only from server-persisted conversation rows.
                owners = set()
                for row in conn.execute("SELECT user_id,result_json FROM chat_history WHERE item_id=?", (trade["item_id"],)):
                    saved = json.loads(row["result_json"])
                    if saved.get("review_id") == snapshot.get("review_id"):
                        owners.add(row["user_id"])
                adopter_id = next(iter(owners)) if len(owners) == 1 else None
            else:
                adopter_id = adopter[0]
            if adopter_id not in (trade["buyer_id"], trade["seller_id"]):
                adopter_id = None
            return {"review_id": review["review_id"], "hash": review["hash"],
                    "item_id": trade["item_id"], "proposal_id": proposal["proposal_id"],
                    "adopter": adopter_id, "snapshot": snapshot}

    @staticmethod
    def _review_identity(snapshot):
        evidence = snapshot.get("evidence", [])
        conditions = snapshot.get("conditions", [])
        return digest({"review_id": snapshot.get("review_id"), "review_hash": snapshot.get("review_hash"),
            "revision": snapshot.get("revision"), "classification": snapshot.get("classification"),
            "applicability": snapshot.get("applicability"),
            "document_hashes": sorted(snapshot.get("document_hashes", [])),
            "evidence": sorted(({
                key: entry.get(key) for key in ("evidence_id", "id", "item_id", "document_id", "version_id",
                                               "document_version_id", "body_hash", "role", "source_type", "revision")
            } for entry in evidence if isinstance(entry, dict)), key=canonical),
            "conditions": sorted(({"text": condition.get("text"),
                "evidence_ids": sorted(condition.get("evidence_ids", []))}
                for condition in conditions if isinstance(condition, dict)), key=canonical)})

    def _revalidate_review(self, target):
        if target is None:
            return None
        checked = {"review_id": target["review_id"], "hash": target["hash"],
                   "proposal_id": target["proposal_id"], "revision": target["snapshot"].get("revision")}
        try:
            if self.review_validator is None or target["adopter"] is None:
                fail("W_CURRENT_REVIEW_REQUIRED", "The adopted review cannot be verified against current server evidence.")
            if target["snapshot"].get("revision") and self.review_guard is None:
                fail("W_CURRENT_REVIEW_REQUIRED", "Current document review protection is not configured.")
            result = self.review_validator(target["adopter"], target["item_id"], target["snapshot"].get("review_id"))
            if (not isinstance(result, dict) or result.get("applicability_ok") is not True or
                    result.get("classification") not in ("TRANSFERABLE", "CONDITIONAL") or
                    result.get("missing_information") or result.get("conflicts") or
                    self._review_identity(result) != self._review_identity(target["snapshot"])):
                fail("W_CURRENT_REVIEW_REQUIRED", "Current source approval and the exact adopted review must remain valid.")
        except ServiceError as error:
            checked["error"] = error
        except (TypeError, ValueError, KeyError):
            checked["error"] = ServiceError("W_CURRENT_REVIEW_REQUIRED", "The current adopted review could not be verified.")
        return checked

    @staticmethod
    def _require_current_review(conn, proposal, review, checked):
        if checked is None or checked["review_id"] != review["review_id"] or checked["hash"] != review["hash"] or checked["proposal_id"] != proposal["proposal_id"]:
            fail("W_CURRENT_REVIEW_REQUIRED", "Reload and verify the exact current adopted review.")
        if checked.get("error") is not None:
            raise checked["error"]
        if checked["revision"] is not None:
            active = conn.execute("SELECT revision FROM index_manifests WHERE active=1").fetchone()
            if active is None or active[0] != checked["revision"]:
                fail("W_CURRENT_REVIEW_REQUIRED", "The adopted document revision is no longer active.")

    @staticmethod
    def _remember(conn, actor, action, key, request_hash, response):
        conn.execute("INSERT INTO idempotency_records(actor,scope,key,request_hash,response_json) VALUES(?,?,?,?,?)",
                     (actor, action, key, request_hash, canonical(response)))

    @staticmethod
    def _exact(payload, row, id_key=None):
        if type(payload.get("revision")) is not int or payload.get("revision") != row["revision"] or payload.get("hash") != row["hash"]:
            fail("TARGET_REVISION_MISMATCH", "Approval must identify the exact current revision and hash.", 409)
        if id_key and payload.get(id_key) != row[id_key]:
            fail("TARGET_REVISION_MISMATCH", status=409)

    def _consent(self, conn, trade, actor, kind, target_id, revision, target_hash):
        self._party(trade, actor)
        return conn.execute("INSERT OR IGNORE INTO consents(trade_id,target_type,target_id,revision,actor,target_hash) VALUES(?,?,?,?,?,?)",
                            (trade["trade_id"], kind, target_id, revision, actor, target_hash)).rowcount > 0

    @staticmethod
    def _bilateral(conn, trade, kind, target_id, revision, target_hash):
        actors = {r["actor"] for r in conn.execute("SELECT actor FROM consents WHERE trade_id=? AND target_type=? AND target_id=? AND revision=? AND target_hash=?", (trade["trade_id"], kind, target_id, revision, target_hash))}
        return {trade["buyer_id"], trade["seller_id"]}.issubset(actors)

    @staticmethod
    def _active(conn, trade_id, components=None):
        rows = conn.execute("SELECT component FROM settlement_operations WHERE trade_id=? AND status IN ('RESERVED','PROCESSING','UNKNOWN')", (trade_id,)).fetchall()
        return any(components is None or r["component"] in components for r in rows)

    def _block_changes(self, conn, trade_id, components=None):
        if self._active(conn, trade_id, components):
            fail("ACTIVE_SETTLEMENT", "An active reservation must be resolved before changing its approval targets.", 409)

    @staticmethod
    def _proposal(conn, trade, proposal_id):
        proposal = conn.execute("SELECT * FROM settlement_proposals WHERE trade_id=? AND proposal_id=?", (trade["trade_id"], proposal_id)).fetchone()
        if proposal is None:
            fail("PROPOSAL_NOT_FOUND", status=404)
        return proposal

    @staticmethod
    def _operation(conn, trade, operation_id):
        op = conn.execute("SELECT * FROM settlement_operations WHERE trade_id=? AND operation_id=?", (trade["trade_id"], operation_id)).fetchone()
        if op is None:
            fail("OPERATION_NOT_FOUND", status=404)
        return op

    def _apply(self, conn, trade, actor, action, payload, validated_review, current_review=None):
        tid = trade["trade_id"]
        contract = self._contract(conn, trade)
        if action == "contract":
            self._party(trade, actor)
            self._block_changes(conn, tid)
            D, W = money(payload.get("D"), True), money(payload.get("W"))
            if trade["funded"] and (D != contract["D"] or W != contract["W"]):
                fail("FUNDED_CONTRACT_IMMUTABLE", "Funded prices cannot be changed.", 409)
            terms = self._terms(payload.get("terms", {}))
            revision = contract["revision"] + 1
            review_id = trade["review_id"] if trade["funded"] else None
            body = {"item_id": trade["item_id"], "revision": revision, "D": D, "W": W, "terms": terms, "review_id": review_id}
            conn.execute("INSERT INTO contract_revisions(trade_id,revision,hash,item_id,D,W,terms_json,review_id) VALUES(?,?,?,?,?,?,?,?)", (tid, revision, digest(body), trade["item_id"], D, W, canonical(terms), review_id))
            conn.execute("UPDATE trades SET contract_revision=?,status=CASE WHEN funded=1 THEN 'ACTIVE' ELSE 'DRAFT' END,receipt_revision=NULL,review_id=?,evidence_id=NULL WHERE trade_id=?", (revision, review_id, tid))
        elif action == "contract_consent":
            self._exact(payload, contract)
            changed = self._consent(conn, trade, actor, "contract", tid, contract["revision"], contract["hash"])
            if self._bilateral(conn, trade, "contract", tid, contract["revision"], contract["hash"]):
                conn.execute("UPDATE trades SET status=CASE WHEN funded=1 THEN status ELSE 'AGREED' END WHERE trade_id=?", (tid,))
            return changed
        elif action == "deposit":
            self._party(trade, actor)
            if actor != trade["buyer_id"]:
                fail("BUYER_REQUIRED", status=403)
            if trade["funded"]:
                return False
            if not self._bilateral(conn, trade, "contract", tid, contract["revision"], contract["hash"]):
                fail("CONTRACT_CONSENT_REQUIRED")
            total = contract["D"] + contract["W"]
            changed = conn.execute("UPDATE demo_wallets SET available=available-? WHERE user_id=? AND available>=?", (total, actor, total)).rowcount
            if changed != 1:
                fail("INSUFFICIENT_WALLET", "The demo wallet has insufficient available funds.", 409)
            gid = identifier()
            conn.execute("INSERT INTO ledger_groups(group_id,trade_id,attempt_no,event_type,event_key) VALUES(?,?,1,'DEPOSIT',?)", (gid, tid, f"deposit:{tid}:{contract['revision']}"))
            lines = []
            for comp in ("D", "W"):
                if contract[comp]:
                    lines.append((gid, len(lines)+1, f"escrow:{tid}:{comp}:held", contract[comp], 0))
            lines.append((gid, len(lines)+1, f"wallet:{actor}", 0, total))
            conn.executemany("INSERT INTO ledger_entries(group_id,line_no,account,debit,credit) VALUES(?,?,?,?,?)", lines)
            conn.execute("UPDATE ledger_groups SET posted=1 WHERE group_id=?", (gid,))
            conn.execute("INSERT INTO deposit_records(trade_id,contract_revision,amount,ledger_group_id) VALUES(?,?,?,?)", (tid, contract["revision"], total, gid))
            for comp in ("D", "W"):
                conn.execute("UPDATE component_balances SET deposited=?,held=? WHERE trade_id=? AND component=?", (contract[comp], contract[comp], tid, comp))
            conn.execute("UPDATE trades SET status='FUNDED',funded=1 WHERE trade_id=?", (tid,))
        elif action == "receipt":
            self._party(trade, actor)
            if actor != trade["buyer_id"]:
                fail("BUYER_REQUIRED", status=403)
            if not trade["funded"] or type(payload.get("contract_revision")) is not int or payload.get("contract_revision") != contract["revision"]:
                fail("FUNDED_CURRENT_CONTRACT_REQUIRED")
            conn.execute("INSERT OR IGNORE INTO receipt_confirmations(trade_id,contract_revision,actor) VALUES(?,?,?)", (tid, contract["revision"], actor))
            conn.execute("UPDATE trades SET receipt_revision=?,status='ACTIVE' WHERE trade_id=?", (contract["revision"], tid))
        elif action == "adopt_review":
            self._party(trade, actor)
            if contract["W"] == 0:
                fail("W_NOT_APPLICABLE")
            # Adoption revises the contract itself, so every live approval target
            # must remain stable, including a previously reserved D-only job.
            self._block_changes(conn, tid)
            if not isinstance(validated_review, dict):
                fail("REVIEW_NOT_VERIFIED")
            review = dict(validated_review)
            classification = review.get("classification", "NEEDS_INFORMATION")
            if classification not in ("TRANSFERABLE", "CONDITIONAL", "NOT_TRANSFERABLE", "NEEDS_INFORMATION"):
                fail("INVALID_REVIEW")
            if classification == "CONDITIONAL":
                # The model discovers clauses; only bilateral evidence can
                # attest fulfilment. Even a legacy true flag has no authority.
                review["conditions_ok"] = False
                review["required_condition_ids"] = review_condition_ids(review) or []
                if review["required_condition_ids"]:
                    review["conditions"] = [{**condition, "condition_id": "condition-" + digest({
                        "text": condition["text"], "evidence_ids": sorted(condition["evidence_ids"])})}
                        for condition in review["conditions"]]
            rid = identifier()
            revision = conn.execute("SELECT COALESCE(MAX(revision),0)+1 FROM review_snapshots WHERE trade_id=?", (tid,)).fetchone()[0]
            conn.execute("INSERT INTO review_snapshots(review_id,trade_id,revision,hash,classification,applicability_ok,conditions_ok,payload_json) VALUES(?,?,?,?,?,?,?,?)", (rid, tid, revision, digest(review), classification, int(review.get("applicability_ok") is True), int(review.get("conditions_ok") is True), canonical(review)))
            audit(conn, tid, actor, "adopt_review_snapshot", rid, "ADOPTED")
            contract_revision = contract["revision"] + 1
            terms = json.loads(contract["terms_json"])
            body = {"item_id": trade["item_id"], "revision": contract_revision, "D": contract["D"], "W": contract["W"], "terms": terms, "review_id": rid}
            conn.execute("INSERT INTO contract_revisions(trade_id,revision,hash,item_id,D,W,terms_json,review_id) VALUES(?,?,?,?,?,?,?,?)", (tid, contract_revision, digest(body), trade["item_id"], contract["D"], contract["W"], canonical(terms), rid))
            conn.execute("UPDATE trades SET contract_revision=?,review_id=?,evidence_id=NULL,receipt_revision=NULL,status=CASE WHEN funded=1 THEN 'ACTIVE' ELSE 'DRAFT' END WHERE trade_id=?", (contract_revision, rid, tid))
        elif action == "evidence":
            self._party(trade, actor)
            if contract["W"] == 0:
                fail("W_NOT_APPLICABLE")
            self._block_changes(conn, tid, {"W"})
            refs = payload.get("references")
            if not isinstance(refs, list) or not refs or len(refs) > 20 or any(not isinstance(x, str) or not x.startswith("SYNTHETIC:") or len(x) > 300 for x in refs):
                fail("SYNTHETIC_EVIDENCE_ONLY", "Only labelled synthetic evidence references are enabled.", 400)
            if payload.get("description") is not None and (not isinstance(payload["description"], str) or len(payload["description"]) > 2000):
                fail("INVALID_EVIDENCE", status=400)
            review_row = conn.execute("SELECT payload_json FROM review_snapshots WHERE trade_id=? AND review_id=?", (tid, trade["review_id"])).fetchone()
            snapshot = json.loads(review_row[0]) if review_row else {}
            required = review_condition_ids(snapshot) or []
            checks = condition_checks(payload.get("condition_checks", []), required,
                                      exact="condition_checks" in payload)
            eid = identifier()
            revision = conn.execute("SELECT COALESCE(MAX(revision),0)+1 FROM evidence_bundles WHERE trade_id=?", (tid,)).fetchone()[0]
            body = {"references": refs, "description": payload.get("description"), "condition_checks": checks,
                    "contract_revision": contract["revision"], "review_id": trade["review_id"], "synthetic": True}
            conn.execute("INSERT INTO evidence_bundles(evidence_id,trade_id,revision,hash,contract_revision,review_id,payload_json) VALUES(?,?,?,?,?,?,?)", (eid, tid, revision, digest(body), contract["revision"], trade["review_id"], canonical(body)))
            conn.execute("UPDATE trades SET evidence_id=?,status=CASE WHEN funded=1 THEN 'ACTIVE' ELSE status END WHERE trade_id=?", (eid, tid))
        elif action == "confirm_evidence":
            self._party(trade, actor)
            evidence = conn.execute("SELECT * FROM evidence_bundles WHERE trade_id=? AND evidence_id=?", (tid, payload.get("evidence_id"))).fetchone()
            if evidence is None:
                fail("EVIDENCE_NOT_FOUND", status=404)
            if evidence["evidence_id"] != trade["evidence_id"] or evidence["hash"] != payload.get("hash") or evidence["contract_revision"] != contract["revision"] or evidence["review_id"] != trade["review_id"]:
                fail("EVIDENCE_STALE", status=409)
            return conn.execute("INSERT OR IGNORE INTO evidence_confirmations(trade_id,evidence_id,actor,target_hash) VALUES(?,?,?,?)", (tid, evidence["evidence_id"], actor, evidence["hash"])).rowcount > 0
        elif action == "proposal":
            self._party(trade, actor)
            allocations = self._allocations(payload.get("allocations"))
            self._block_changes(conn, tid, {a["component"] for a in allocations})
            wpay = any(a["component"] == "W" and a["pay_won"] > 0 for a in allocations)
            rid, eid = (trade["review_id"], trade["evidence_id"]) if wpay else (None, None)
            if payload.get("review_id", rid) != rid or payload.get("evidence_id", eid) != eid:
                fail("APPROVAL_TARGET_MISMATCH", status=409)
            if any(a["component"] == "W" for a in allocations) and contract["W"] == 0:
                fail("W_NOT_APPLICABLE")
            pid = identifier()
            body = {"contract_revision": contract["revision"], "review_id": rid, "evidence_id": eid,
                    "allocations": allocations, "refund_reason": payload.get("refund_reason"),
                    "resolution_context": self._resolution_context(conn, tid, {a["component"] for a in allocations})}
            conn.execute("INSERT INTO settlement_proposals(proposal_id,trade_id,revision,hash,contract_revision,review_id,evidence_id,payload_json) VALUES(?,?,1,?,?,?,?,?)", (pid, tid, digest(body), contract["revision"], rid, eid, canonical(body)))
            for allocation in allocations:
                conn.execute("INSERT INTO allocations(proposal_id,component,pay_won,refund_won) VALUES(?,?,?,?)", (pid, allocation["component"], allocation["pay_won"], allocation["refund_won"]))
        elif action == "proposal_consent":
            proposal = self._proposal(conn, trade, payload.get("proposal_id"))
            self._exact(payload, proposal, "proposal_id")
            self._proposal_current(conn, trade, proposal)
            return self._consent(conn, trade, actor, "proposal", proposal["proposal_id"], proposal["revision"], proposal["hash"])
        elif action == "reserve":
            self._party(trade, actor)
            proposal = self._proposal(conn, trade, payload.get("proposal_id"))
            allocations = [dict(r) for r in conn.execute("SELECT * FROM allocations WHERE proposal_id=?", (proposal["proposal_id"],))]
            self._validate_reservation(conn, trade, proposal, allocations, current_review)
            gid = identifier()
            conn.execute("INSERT INTO settlement_groups(group_id,trade_id,proposal_id,revision,status) VALUES(?,?,?,?,'RESERVED')", (gid, tid, proposal["proposal_id"], proposal["revision"]))
            attempts = []
            for allocation in allocations:
                for direction, field in (("PAY", "pay_won"), ("REFUND", "refund_won")):
                    amount = allocation[field]
                    if amount:
                        oid = identifier()
                        conn.execute("INSERT INTO settlement_operations(operation_id,trade_id,group_id,proposal_id,revision,component,direction,amount,status,attempt_no) VALUES(?,?,?,?,?,?,?,?,'RESERVED',1)", (oid, tid, gid, proposal["proposal_id"], proposal["revision"], allocation["component"], direction, amount))
                        self._reserve_amount(conn, tid, oid, 1, allocation["component"], direction, amount)
                        attempts.append([oid, 1])
            self._job(conn, tid, gid, attempts)
        elif action in ("retry", "reconcile"):
            op = self._operation(conn, trade, payload.get("operation_id"))
            if action == "reconcile":
                from .worker import reconcile_operation
                return reconcile_operation(conn, op, actor)
            self._party(trade, actor)
            if op["status"] != "FAILED":
                fail("RETRY_REQUIRES_FAILED", "Only a confirmed failure can be executed again.", 409)
            if op["attempt_no"] >= MAX_SETTLEMENT_ATTEMPTS:
                fail("MAX_ATTEMPTS_REACHED", status=409)
            proposal = self._proposal(conn, trade, op["proposal_id"])
            allocation = {"component": op["component"], "pay_won": op["amount"] if op["direction"] == "PAY" else 0, "refund_won": op["amount"] if op["direction"] == "REFUND" else 0}
            self._validate_reservation(conn, trade, proposal, [allocation], current_review)
            attempt = op["attempt_no"] + 1
            if conn.execute("UPDATE settlement_operations SET status='RESERVED',attempt_no=? WHERE operation_id=? AND status='FAILED' AND attempt_no=?", (attempt, op["operation_id"], op["attempt_no"])).rowcount != 1:
                fail("RETRY_CONFLICT", status=409)
            self._reserve_amount(conn, tid, op["operation_id"], attempt, op["component"], op["direction"], op["amount"])
            conn.execute("UPDATE settlement_groups SET status='RESERVED' WHERE group_id=?", (op["group_id"],))
            self._job(conn, tid, op["group_id"], [[op["operation_id"], attempt]])
        elif action == "dispute":
            self._party(trade, actor)
            scope = payload.get("scope")
            if scope not in ("D", "W", "BOTH"):
                fail("INVALID_DISPUTE_SCOPE", status=400)
            did = identifier()
            conn.execute("INSERT INTO disputes(dispute_id,trade_id,scope,status) VALUES(?,?,?,'OPEN')", (did, tid, scope))
            components = {"D", "W"} if scope == "BOTH" else {scope}
            # The retry job contains only the retried attempt, never successful
            # siblings from an earlier job for the same logical proposal.
            jobs = conn.execute("SELECT * FROM jobs WHERE trade_id=? AND status='PENDING'", (tid,)).fetchall()
            for job in jobs:
                attempts = json.loads(job["attempts_json"])
                ops = []
                for oid, attempt in attempts:
                    op = conn.execute("SELECT * FROM settlement_operations WHERE trade_id=? AND operation_id=? AND attempt_no=?", (tid, oid, attempt)).fetchone()
                    if op is not None:
                        ops.append(op)
                if len(ops) == len(attempts) and any(op["component"] in components for op in ops) and all(op["status"] == "RESERVED" for op in ops):
                    for op in ops:
                        self._release(conn, op, "CANCELLED")
                    from .worker import update_group
                    update_group(conn, job["group_id"])
                    conn.execute("UPDATE jobs SET status='CANCELLED' WHERE job_id=?", (job["job_id"],))
        elif action == "resolution":
            if actor != trade["admin_id"]:
                fail("ASSIGNED_ADMIN_REQUIRED", status=403)
            dispute = conn.execute("SELECT * FROM disputes WHERE trade_id=? AND dispute_id=?", (tid, payload.get("dispute_id"))).fetchone()
            if dispute is None:
                fail("DISPUTE_NOT_FOUND", status=404)
            if dispute["status"] == "RESOLVED":
                fail("DISPUTE_ALREADY_RESOLVED", status=409)
            terms = payload.get("terms")
            if not isinstance(terms, str) or not terms.strip() or len(terms) > 2000:
                fail("INVALID_RESOLUTION", status=400)
            revision = conn.execute("SELECT COALESCE(MAX(revision),0)+1 FROM resolution_revisions WHERE dispute_id=?", (dispute["dispute_id"],)).fetchone()[0]
            rid = identifier()
            body = {"dispute_id": dispute["dispute_id"], "revision": revision, "terms": terms}
            conn.execute("INSERT INTO resolution_revisions(resolution_id,trade_id,dispute_id,revision,hash,payload_json) VALUES(?,?,?,?,?,?)", (rid, tid, dispute["dispute_id"], revision, digest(body), canonical(body)))
            conn.execute("UPDATE disputes SET status='PROPOSED',resolution_id=? WHERE dispute_id=?", (rid, dispute["dispute_id"]))
        elif action == "resolution_consent":
            resolution = conn.execute("SELECT * FROM resolution_revisions WHERE trade_id=? AND resolution_id=?", (tid, payload.get("resolution_id"))).fetchone()
            if resolution is None:
                fail("RESOLUTION_NOT_FOUND", status=404)
            dispute = conn.execute("SELECT * FROM disputes WHERE trade_id=? AND dispute_id=?", (tid, resolution["dispute_id"])).fetchone()
            if dispute["resolution_id"] != resolution["resolution_id"]:
                fail("RESOLUTION_STALE", status=409)
            self._exact(payload, resolution, "resolution_id")
            changed = self._consent(conn, trade, actor, "resolution", resolution["resolution_id"], resolution["revision"], resolution["hash"])
            if self._bilateral(conn, trade, "resolution", resolution["resolution_id"], resolution["revision"], resolution["hash"]):
                conn.execute("UPDATE disputes SET status='RESOLVED' WHERE dispute_id=?", (resolution["dispute_id"],))
            return changed
        elif action == "close":
            self._party(trade, actor)
            if not trade["funded"]:
                fail("UNFUNDED_CANNOT_CLOSE", status=409)
            remaining = conn.execute("SELECT SUM(held+pay_reserved+refund_reserved) FROM component_balances WHERE trade_id=?", (tid,)).fetchone()[0]
            unresolved = conn.execute("SELECT 1 FROM disputes WHERE trade_id=? AND status<>'RESOLVED'", (tid,)).fetchone()
            if remaining or unresolved or self._active(conn, tid):
                fail("CLOSE_CONDITIONS_NOT_MET", status=409)
            conn.execute("UPDATE trades SET status='CLOSED' WHERE trade_id=?", (tid,))
        elif action == "cancel_consent":
            self._party(trade, actor)
            if trade["funded"]:
                fail("FUNDED_CANNOT_CANCEL", status=409)
            self._exact(payload, contract)
            changed = self._consent(conn, trade, actor, "cancel", tid, contract["revision"], contract["hash"])
            if self._bilateral(conn, trade, "cancel", tid, contract["revision"], contract["hash"]):
                conn.execute("UPDATE trades SET status='CANCELLED' WHERE trade_id=?", (tid,))
            return changed
        elif action == "transfer_event":
            self._party(trade, actor)
            if payload.get("status") not in ("NOT_STARTED", "IN_PROGRESS", "FAILED", "ON_HOLD"):
                fail("INVALID_TRANSFER_STATUS", status=400)
            if not trade["funded"] or contract["W"] == 0:
                fail("FUNDED_W_REQUIRED")
            conn.execute("INSERT INTO transfer_events(event_id,trade_id,actor,status) VALUES(?,?,?,?)", (identifier(), tid, actor, payload["status"]))
            audit(conn, tid, actor, "transfer_event", tid, payload["status"])
            conn.execute("UPDATE trades SET status='ACTIVE' WHERE trade_id=?", (tid,))
        return True

    @staticmethod
    def _allocations(values):
        if not isinstance(values, list) or not 1 <= len(values) <= 2:
            fail("INVALID_ALLOCATIONS", status=400)
        result, seen = [], set()
        for value in values:
            if not isinstance(value, dict) or set(value) != {"component", "pay_won", "refund_won"}:
                fail("INVALID_ALLOCATIONS", status=400)
            comp = value["component"]
            if comp not in ("D", "W") or comp in seen:
                fail("INVALID_ALLOCATIONS", status=400)
            seen.add(comp)
            pay, refund = money(value["pay_won"]), money(value["refund_won"])
            if pay + refund == 0:
                fail("ZERO_ALLOCATION", status=400)
            result.append({"component": comp, "pay_won": pay, "refund_won": refund})
        return sorted(result, key=lambda a: a["component"])

    @staticmethod
    def _proposal_current(conn, trade, proposal):
        if proposal["contract_revision"] != trade["contract_revision"]:
            fail("PROPOSAL_STALE", status=409)
        wpay = conn.execute("SELECT 1 FROM allocations WHERE proposal_id=? AND component='W' AND pay_won>0", (proposal["proposal_id"],)).fetchone()
        if wpay and (proposal["review_id"] != trade["review_id"] or proposal["evidence_id"] != trade["evidence_id"]):
            fail("PROPOSAL_STALE", status=409)
        components = {r[0] for r in conn.execute("SELECT component FROM allocations WHERE proposal_id=?", (proposal["proposal_id"],))}
        if json.loads(proposal["payload_json"]).get("resolution_context", []) != TradeService._resolution_context(conn, trade["trade_id"], components):
            fail("PROPOSAL_STALE", "A new settlement agreement is required after dispute resolution.", 409)

    @staticmethod
    def _resolution_context(conn, trade_id, components):
        return sorted(r["resolution_id"] for r in conn.execute("SELECT scope,resolution_id FROM disputes WHERE trade_id=? AND status='RESOLVED'", (trade_id,)) if r["scope"] == "BOTH" or r["scope"] in components)

    def _validate_reservation(self, conn, trade, proposal, allocations, current_review=None):
        tid = trade["trade_id"]
        contract = self._contract(conn, trade)
        self._proposal_current(conn, trade, proposal)
        if not trade["funded"] or not self._bilateral(conn, trade, "contract", tid, contract["revision"], contract["hash"]):
            fail("FUNDED_CONTRACT_REQUIRED")
        if not self._bilateral(conn, trade, "proposal", proposal["proposal_id"], proposal["revision"], proposal["hash"]):
            fail("PROPOSAL_CONSENT_REQUIRED")
        terms = json.loads(contract["terms_json"])
        conditions = json.loads(proposal["payload_json"])
        for allocation in allocations:
            comp, pay, refund = allocation["component"], allocation["pay_won"], allocation["refund_won"]
            if conn.execute("SELECT 1 FROM disputes WHERE trade_id=? AND status<>'RESOLVED' AND scope IN (?, 'BOTH')", (tid, comp)).fetchone():
                fail("UNRESOLVED_DISPUTE", status=409)
            if conn.execute("SELECT 1 FROM settlement_operations WHERE trade_id=? AND component=? AND status IN ('PROCESSING','UNKNOWN')", (tid, comp)).fetchone():
                fail("SETTLEMENT_UNCERTAIN", status=409)
            held = conn.execute("SELECT held FROM component_balances WHERE trade_id=? AND component=?", (tid, comp)).fetchone()[0]
            if pay + refund > held:
                fail("INSUFFICIENT_HELD", status=409)
            if refund and conditions.get("refund_reason") not in terms["return_conditions"]:
                fail("RETURN_CONDITION_REQUIRED")
            if pay and comp == "D" and trade["receipt_revision"] != contract["revision"]:
                fail("RECEIPT_REQUIRED")
            if pay and comp == "W":
                review = conn.execute("SELECT * FROM review_snapshots WHERE trade_id=? AND review_id=?", (tid, proposal["review_id"])).fetchone()
                if review is None or review["classification"] not in ("TRANSFERABLE", "CONDITIONAL") or not review["applicability_ok"]:
                    fail("W_REVIEW_NOT_READY")
                snapshot = json.loads(review["payload_json"])
                if snapshot.get("classification") != review["classification"] or snapshot.get("missing_information") or snapshot.get("conflicts") or not snapshot.get("evidence") or not snapshot.get("document_hashes"):
                    fail("W_REVIEW_NOT_READY")
                required = review_condition_ids(snapshot)
                if required is None:
                    fail("W_REQUIRED_CONDITIONS_MISSING", "Valid document-supported condition definitions are required.")
                evidence = conn.execute("SELECT * FROM evidence_bundles WHERE trade_id=? AND evidence_id=?", (tid, proposal["evidence_id"])).fetchone()
                if evidence is None or evidence["evidence_id"] != trade["evidence_id"] or evidence["review_id"] != review["review_id"] or evidence["contract_revision"] != contract["revision"]:
                    fail("W_EVIDENCE_NOT_READY")
                if review["classification"] == "CONDITIONAL":
                    if not required:
                        fail("W_REQUIRED_CONDITIONS_MISSING")
                    checks = json.loads(evidence["payload_json"]).get("condition_checks", [])
                    try:
                        checks = condition_checks(checks, required)
                    except ServiceError:
                        fail("W_CONDITIONS_NOT_CONFIRMED", "Both parties must confirm evidence for every required condition.")
                    if not all(check["confirmed"] is True for check in checks):
                        fail("W_CONDITIONS_NOT_CONFIRMED", "Both parties must confirm evidence for every required condition.")
                actors = {r[0] for r in conn.execute("SELECT actor FROM evidence_confirmations WHERE trade_id=? AND evidence_id=? AND target_hash=?", (tid, evidence["evidence_id"], evidence["hash"]))}
                if not {trade["buyer_id"], trade["seller_id"]}.issubset(actors):
                    fail("W_BILATERAL_EVIDENCE_REQUIRED")
                self._require_current_review(conn, proposal, review, current_review)

    @staticmethod
    def _job(conn, trade_id, group_id, attempts):
        conn.execute("INSERT INTO jobs(job_id,trade_id,group_id,attempts_json,status) VALUES(?,?,?,?,'PENDING')", (identifier(), trade_id, group_id, canonical(attempts)))

    @staticmethod
    def _reserve_amount(conn, tid, oid, attempt, component, direction, amount):
        target = "pay_reserved" if direction == "PAY" else "refund_reserved"
        balance_move(conn, tid, component, "held", target, amount)
        post_ledger(conn, tid, "RESERVE", amount, f"escrow:{tid}:{component}:{target}", f"escrow:{tid}:{component}:held", oid, attempt)

    @staticmethod
    def _release(conn, op, status):
        source = "pay_reserved" if op["direction"] == "PAY" else "refund_reserved"
        balance_move(conn, op["trade_id"], op["component"], source, "held", op["amount"])
        post_ledger(conn, op["trade_id"], "RELEASE", op["amount"], f"escrow:{op['trade_id']}:{op['component']}:held", f"escrow:{op['trade_id']}:{op['component']}:{source}", op["operation_id"], op["attempt_no"])
        conn.execute("UPDATE settlement_operations SET status=? WHERE operation_id=?", (status, op["operation_id"]))
