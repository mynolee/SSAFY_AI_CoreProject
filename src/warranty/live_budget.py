"""Conservative project-wide LIVE credit reservations; no provider I/O.

Prices, tokenizer bounds and output bounds must be explicitly confirmed in a
trusted private profile. Missing profiles never create a database or authorize
LIVE execution. Reserved upper bounds are never refunded, including failures.
Provider-reported credits and token-based estimates remain separate records.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
from pathlib import Path
import re
import sqlite3

from .errors import ServiceError
from .usage import _initialization_guard, _wait_for_initialization


SCALE = 1_000_000
PROJECT_LIMIT_CREDITS = 70_000
PROJECT_LIMIT_MICRO = PROJECT_LIMIT_CREDITS * SCALE
PURPOSES = frozenset({"evaluation", "verification", "final_verification", "demo", "embedding_preparation"})
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./:-]{0,199}\Z")
_HASH = re.compile(r"[a-fA-F0-9]{64}\Z")
_TABLES = {"credit_project", "credit_purposes", "credit_approvals", "credit_requests"}
_SCHEMA = (
    "CREATE TABLE credit_project(singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
    "hard_limit_micro INTEGER NOT NULL CHECK(hard_limit_micro=70000000000),"
    "policy_fingerprint TEXT NOT NULL,demo_reserve_micro INTEGER NOT NULL CHECK(demo_reserve_micro BETWEEN 0 AND 70000000000),"
    "reserved_upper_micro INTEGER NOT NULL DEFAULT 0 CHECK(reserved_upper_micro BETWEEN 0 AND 70000000000),"
    "frozen INTEGER NOT NULL DEFAULT 0 CHECK(frozen IN (0,1)),frozen_reason TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)",
    "CREATE TABLE credit_purposes(purpose TEXT PRIMARY KEY,limit_micro INTEGER NOT NULL CHECK(limit_micro BETWEEN 0 AND 70000000000),"
    "reserved_upper_micro INTEGER NOT NULL DEFAULT 0 CHECK(reserved_upper_micro BETWEEN 0 AND limit_micro))",
    "CREATE TABLE credit_approvals(approval_id TEXT PRIMARY KEY,context_fingerprint TEXT NOT NULL,profile_fingerprint TEXT NOT NULL,created_at TEXT NOT NULL)",
    "CREATE TABLE credit_requests(request_id TEXT PRIMARY KEY,approval_id TEXT NOT NULL REFERENCES credit_approvals(approval_id),"
    "purpose TEXT NOT NULL REFERENCES credit_purposes(purpose),request_hash TEXT NOT NULL,kind TEXT NOT NULL CHECK(kind IN ('chat','embedding')),"
    "model TEXT NOT NULL,route TEXT NOT NULL,input_bytes INTEGER NOT NULL CHECK(input_bytes>0),"
    "input_tokens_bound INTEGER NOT NULL CHECK(input_tokens_bound>=input_bytes),output_tokens_bound INTEGER NOT NULL CHECK(output_tokens_bound>=0),"
    "reserved_upper_micro INTEGER NOT NULL CHECK(reserved_upper_micro>=0),state TEXT NOT NULL CHECK(state IN ('RESERVED','UNKNOWN','SETTLED')),"
    "provider_actual_micro INTEGER CHECK(provider_actual_micro>=0),reported_input_tokens INTEGER CHECK(reported_input_tokens>=0),"
    "reported_output_tokens INTEGER CHECK(reported_output_tokens>=0),token_estimate_micro INTEGER CHECK(token_estimate_micro>=0),"
    "created_at TEXT NOT NULL,updated_at TEXT NOT NULL)",
    "CREATE INDEX credit_requests_by_purpose ON credit_requests(purpose,state)",
)


def _error(code="LIVE_BUDGET_UNCONFIRMED", status=503):
    return ServiceError(code, "LIVE 처리 승인·가격 기준·예산의 확인이 필요합니다.", status, False)


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _micro(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise _error()
    try:
        decimal = Decimal(value)
        scaled = decimal * SCALE
        if not decimal.is_finite() or decimal < 0 or scaled != scaled.to_integral_value() or scaled > 1_000_000_000_000_000_000:
            raise ValueError()
        return int(scaled)
    except (InvalidOperation, ValueError, OverflowError):
        raise _error() from None


def _credits(value):
    return format(Decimal(value) / SCALE, ".6f")


class CreditBudget:
    """All approval IDs in one dedicated file share the same 70,000 ceiling."""

    def __init__(self, path, approval_id, profile=None, *, context_fingerprint):
        if approval_id is None:
            approval_id = ""
        if (not isinstance(approval_id, str) or (approval_id and _IDENTIFIER.fullmatch(approval_id) is None)
                or not isinstance(context_fingerprint, str) or _HASH.fullmatch(context_fingerprint) is None):
            raise _error()
        try:
            self._path = Path(path).resolve()
        except (ValueError, TypeError, OSError):
            raise _error() from None
        repo = Path(__file__).resolve().parents[2]
        if self._path == repo or repo in self._path.parents:
            raise _error()
        self._approval_id, self._context = approval_id, context_fingerprint.lower()
        self._forced_frozen = False
        self._profile = self._validate_profile(profile)
        self._allocations = ({key: _micro(value) for key, value in self._profile["allocations"].items()} if self._profile else {key: 0 for key in PURPOSES})
        self._demo_reserve = _micro(self._profile["demo_reserve_credits"]) if self._profile else 0
        self._profile_hash = _digest(self._profile) if self._profile else None
        self._policy_hash = _digest({"hard_limit": PROJECT_LIMIT_MICRO, "allocations": self._allocations, "demo_reserve": self._demo_reserve})

    @staticmethod
    def _validate_profile(profile):
        if profile is None or isinstance(profile, dict) and profile.get("pricing_confirmed") is not True:
            return None
        required = {"profile_id", "pricing_confirmed", "pricing_version", "allocations", "demo_reserve_credits", "models"}
        try:
            if not isinstance(profile, dict) or set(profile) != required:
                raise ValueError()
            for key in ("profile_id", "pricing_version"):
                if not isinstance(profile[key], str) or _IDENTIFIER.fullmatch(profile[key]) is None:
                    raise ValueError()
            if not isinstance(profile["allocations"], dict) or set(profile["allocations"]) != PURPOSES:
                raise ValueError()
            allocations = {key: _micro(value) for key, value in profile["allocations"].items()}
            if sum(allocations.values()) > PROJECT_LIMIT_MICRO or _micro(profile["demo_reserve_credits"]) > allocations["demo"]:
                raise ValueError()
            if not isinstance(profile["models"], dict) or not 1 <= len(profile["models"]) <= 100:
                raise ValueError()
            fields = {"kind", "route", "input_microcredits_per_token", "output_microcredits_per_token", "input_bound",
                      "input_bound_confirmed", "input_token_overhead", "max_input_tokens", "output_bound",
                      "output_bound_confirmed", "max_output_tokens"}
            for model, row in profile["models"].items():
                if not isinstance(model, str) or _MODEL.fullmatch(model) is None or not isinstance(row, dict) or set(row) - {"allowed_reported_models"} != fields:
                    raise ValueError()
                aliases = row.get("allowed_reported_models", [])
                if (not isinstance(aliases, list) or len(aliases) > 32 or len(set(aliases)) != len(aliases)
                        or any(not isinstance(value, str) or _MODEL.fullmatch(value) is None for value in aliases)):
                    raise ValueError()
                if (row["input_bound"] != "wire_utf8_bytes" or row["input_bound_confirmed"] is not True
                        or row["output_bound_confirmed"] is not True):
                    return None
                for key, lower, upper in (("input_microcredits_per_token", 1, 1_000_000_000),
                        ("output_microcredits_per_token", 0, 1_000_000_000), ("input_token_overhead", 0, 1_000_000),
                        ("max_input_tokens", 1, 10_000_000), ("max_output_tokens", 0, 1_000_000)):
                    if type(row[key]) is not int or not lower <= row[key] <= upper:
                        raise ValueError()
                if row["kind"] == "embedding":
                    if row["route"] != "embeddings" or row["output_bound"] != "none" or row["max_output_tokens"] != 0 or row["output_microcredits_per_token"] != 0:
                        raise ValueError()
                elif row["kind"] == "chat":
                    if row["max_output_tokens"] <= 0 or (row["route"], row["output_bound"]) not in {
                            ("chat.completions", "max_tokens"), ("responses", "model_maximum")}:
                        return None
                else:
                    raise ValueError()
            normalized = deepcopy(profile)
            normalized["allocations"] = {key: _credits(value) for key, value in allocations.items()}
            normalized["demo_reserve_credits"] = _credits(_micro(profile["demo_reserve_credits"]))
            return normalized
        except (ValueError, TypeError, KeyError, ServiceError):
            raise _error() from None

    @property
    def confirmed(self):
        return self._profile is not None

    @property
    def ready(self):
        return self.snapshot()["ready"]

    def approval_matches(self, approval_id):
        return (isinstance(approval_id, str) and _IDENTIFIER.fullmatch(approval_id) is not None
                and bool(self._approval_id) and hmac.compare_digest(self._approval_id, approval_id)
                and self.snapshot()["approval_valid"])

    def allowed_reported_models(self, kind, requested_model, route):
        if not self.confirmed or not isinstance(requested_model, str):
            return frozenset()
        row = self._profile["models"].get(requested_model)
        if row is None or row["kind"] != kind or row["route"] != route:
            return frozenset()
        return frozenset({requested_model, *row.get("allowed_reported_models", [])})

    def _connect(self, read_only):
        connection = None
        try:
            if read_only:
                connection = sqlite3.connect(self._path.as_uri() + "?mode=ro", uri=True, isolation_level=None, timeout=2)
                connection.execute("PRAGMA query_only=ON")
            else:
                connection = sqlite3.connect(str(self._path), isolation_level=None, timeout=2)
                connection.execute("PRAGMA busy_timeout=2000")
                connection.execute("PRAGMA synchronous=FULL")
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            return connection
        except BaseException:
            if connection is not None:
                connection.close()
            raise

    @staticmethod
    def _schema_state(connection):
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if names - _TABLES or version not in (0, 1) or (names and (names != _TABLES or version != 1)) or (version == 1 and names != _TABLES):
            raise _error("LIVE_BUDGET_UNAVAILABLE")
        return bool(names)

    def _check_binding(self, connection):
        project = connection.execute("SELECT * FROM credit_project WHERE singleton=1").fetchone()
        if project is None or project["policy_fingerprint"] != self._policy_hash or project["hard_limit_micro"] != PROJECT_LIMIT_MICRO:
            raise _error()
        approval = connection.execute("SELECT * FROM credit_approvals WHERE approval_id=?", (self._approval_id,)).fetchone()
        if approval is not None and (approval["context_fingerprint"] != self._context or approval["profile_fingerprint"] != self._profile_hash):
            raise _error()
        return project

    def _counter_rows(self, connection, project):
        purposes = {row["purpose"]: row for row in connection.execute("SELECT * FROM credit_purposes")}
        totals = {row["purpose"]: row["total"] for row in connection.execute("SELECT purpose,SUM(reserved_upper_micro) AS total FROM credit_requests GROUP BY purpose")}
        if (set(purposes) != PURPOSES or set(totals) - PURPOSES
                or any(purposes[key]["limit_micro"] != self._allocations[key]
                       or purposes[key]["reserved_upper_micro"] != totals.get(key, 0) for key in PURPOSES)
                or sum(totals.values()) != project["reserved_upper_micro"]):
            raise _error("LIVE_BUDGET_UNAVAILABLE")
        return purposes

    def _base_snapshot(self, *, reason=None):
        valid = self.confirmed and bool(self._approval_id) and reason is None
        purposes = {key: {"limit_credits": _credits(self._allocations[key]), "reserved_upper_credits": _credits(0),
                          "remaining_credits": _credits(self._allocations[key])} for key in sorted(PURPOSES)}
        if not valid:
            reason = reason or "LIVE_BUDGET_UNCONFIRMED"
        available = any(value > 0 for value in self._allocations.values())
        if valid and not available:
            reason = "CREDIT_LIMIT_REACHED"
        return {"global_limit_credits": PROJECT_LIMIT_CREDITS, "reserved_upper_credits": _credits(0) if valid else None,
                "remaining_credits": _credits(PROJECT_LIMIT_MICRO) if valid else None, "provider_reported_actual_credits": None,
                "token_based_estimate_credits": None, "request_count": 0, "unknown_request_count": 0,
                "confirmed": self.confirmed, "approval_valid": valid, "ready": valid and available,
                "reason_code": reason, "demo_preserved_credits": _credits(self._demo_reserve), "purposes": purposes}

    def snapshot(self):
        if not self.confirmed or not self._approval_id:
            return self._base_snapshot()
        connection = None
        try:
            _wait_for_initialization(self._path)
            if not self._path.exists():
                return self._base_snapshot()
            if not self._path.is_file():
                raise _error("LIVE_BUDGET_UNAVAILABLE")
            connection = self._connect(True)
            connection.execute("BEGIN")
            if not self._schema_state(connection):
                raise _error("LIVE_BUDGET_UNAVAILABLE")
            project = self._check_binding(connection)
            result = self._base_snapshot()
            reserved = project["reserved_upper_micro"]
            result.update(reserved_upper_credits=_credits(reserved), remaining_credits=_credits(PROJECT_LIMIT_MICRO - reserved))
            states = self._counter_rows(connection, project)
            for purpose, row in states.items():
                result["purposes"][purpose].update(reserved_upper_credits=_credits(row["reserved_upper_micro"]),
                    remaining_credits=_credits(row["limit_micro"] - row["reserved_upper_micro"]))
            summary = connection.execute("SELECT COUNT(*),SUM(CASE WHEN state!='SETTLED' THEN 1 ELSE 0 END),SUM(provider_actual_micro),SUM(token_estimate_micro),SUM(reserved_upper_micro) FROM credit_requests").fetchone()
            if (summary[4] or 0) != reserved or sum(row["reserved_upper_micro"] for row in states.values()) != reserved:
                raise _error("LIVE_BUDGET_UNAVAILABLE")
            result.update(request_count=summary[0], unknown_request_count=summary[1] or 0,
                provider_reported_actual_credits=None if summary[2] is None else _credits(summary[2]),
                token_based_estimate_credits=None if summary[3] is None else _credits(summary[3]))
            demo_preserved = max(0, self._demo_reserve - states["demo"]["reserved_upper_micro"])
            result["demo_preserved_credits"] = _credits(demo_preserved)
            room = PROJECT_LIMIT_MICRO - reserved
            available = any(row["reserved_upper_micro"] < row["limit_micro"] and (purpose == "demo" and room > 0 or room > demo_preserved) for purpose, row in states.items())
            if project["frozen"] or self._forced_frozen:
                result.update(ready=False, reason_code="LIVE_BUDGET_UNAVAILABLE")
            elif not available:
                result.update(ready=False, reason_code="CREDIT_LIMIT_REACHED")
            return result
        except ServiceError as error:
            return self._base_snapshot(reason=error.code)
        except (sqlite3.Error, OSError, KeyError, TypeError, ValueError):
            return self._base_snapshot(reason="LIVE_BUDGET_UNAVAILABLE")
        finally:
            if connection is not None:
                connection.close()

    def _plan(self, kind, model, input_bytes, *, output_tokens_bound=None, route):
        if self._forced_frozen:
            raise _error("LIVE_BUDGET_UNAVAILABLE")
        if not self.confirmed or not self._approval_id:
            raise _error()
        if (not isinstance(kind, str) or kind not in {"chat", "embedding"} or not isinstance(model, str)
                or type(input_bytes) is not int or not 1 <= input_bytes <= 10_000_000 or not isinstance(route, str)):
            raise _error()
        row = self._profile["models"].get(model)
        if row is None or row["kind"] != kind or row["route"] != route:
            raise _error()
        input_bound = input_bytes + row["input_token_overhead"]
        if input_bound > row["max_input_tokens"]:
            raise _error()
        if kind == "embedding":
            if output_tokens_bound not in (None, 0) or isinstance(output_tokens_bound, bool):
                raise _error()
            output_bound = 0
        elif row["output_bound"] == "model_maximum":
            if output_tokens_bound is not None and (type(output_tokens_bound) is not int or output_tokens_bound != row["max_output_tokens"]):
                raise _error()
            output_bound = row["max_output_tokens"]
        else:
            if type(output_tokens_bound) is not int or not 1 <= output_tokens_bound <= row["max_output_tokens"]:
                raise _error()
            output_bound = output_tokens_bound
        upper = input_bound * row["input_microcredits_per_token"] + output_bound * row["output_microcredits_per_token"]
        return {"estimated_upper_credits": _credits(upper), "kind": kind, "model": model, "input_bytes": input_bytes,
                "input_tokens_bound": input_bound, "output_tokens_bound": output_bound, "route": route}

    def estimate(self, kind, model, input_bytes, *, output_tokens_bound=None, route):
        plan = self._plan(kind, model, input_bytes, output_tokens_bound=output_tokens_bound, route=route)
        state = self.snapshot()
        if not state["ready"]:
            raise _error(state["reason_code"] or "LIVE_BUDGET_UNCONFIRMED", 429 if state["reason_code"] == "CREDIT_LIMIT_REACHED" else 503)
        if _micro(plan["estimated_upper_credits"]) > _micro(state["remaining_credits"]):
            raise _error("CREDIT_LIMIT_REACHED", 429)
        return plan

    def _initialize(self, connection, stamp):
        for statement in _SCHEMA:
            connection.execute(statement)
        connection.execute("PRAGMA user_version=1")
        connection.execute("INSERT INTO credit_project(singleton,hard_limit_micro,policy_fingerprint,demo_reserve_micro,created_at,updated_at) VALUES(1,?,?,?,?,?)", (PROJECT_LIMIT_MICRO, self._policy_hash, self._demo_reserve, stamp, stamp))
        connection.executemany("INSERT INTO credit_purposes(purpose,limit_micro) VALUES(?,?)", sorted(self._allocations.items()))

    def reserve(self, request_id, purpose, estimated_upper_credits, *, kind, model, input_bytes,
                input_tokens_bound, output_tokens_bound, route):
        if not isinstance(request_id, str) or _IDENTIFIER.fullmatch(request_id) is None:
            raise _error("LIVE_REQUEST_CONFLICT", 409)
        if not isinstance(purpose, str) or purpose not in PURPOSES:
            raise _error("LIVE_PURPOSE_REQUIRED", 403)
        plan = self._plan(kind, model, input_bytes, output_tokens_bound=output_tokens_bound, route=route)
        upper = _micro(estimated_upper_credits)
        if type(input_tokens_bound) is not int or input_tokens_bound != plan["input_tokens_bound"] or upper != _micro(plan["estimated_upper_credits"]):
            raise _error()
        if upper > PROJECT_LIMIT_MICRO or self._allocations[purpose] == 0 or upper > self._allocations[purpose]:
            raise _error("CREDIT_LIMIT_REACHED", 429)
        request_hash = _digest({"approval": self._approval_id, "purpose": purpose, **plan})
        connection = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with _initialization_guard(self._path) as may_initialize:
                connection = self._connect(False)
                connection.execute("BEGIN IMMEDIATE")
                stamp = datetime.now(timezone.utc).isoformat()
                if not self._schema_state(connection):
                    if not may_initialize:
                        raise _error("LIVE_BUDGET_UNAVAILABLE")
                    self._initialize(connection, stamp)
                project = self._check_binding(connection)
                if project["frozen"]:
                    raise _error("LIVE_BUDGET_UNAVAILABLE")
                prior = connection.execute("SELECT * FROM credit_requests WHERE request_id=?", (request_id,)).fetchone()
                if prior is not None:
                    if prior["request_hash"] != request_hash:
                        raise _error("LIVE_REQUEST_CONFLICT", 409)
                    connection.rollback()
                    return {"request_id": request_id, "replayed": True, "state": prior["state"]}
                remaining = PROJECT_LIMIT_MICRO - project["reserved_upper_micro"]
                purposes = self._counter_rows(connection, project)
                preserved = max(0, self._demo_reserve - purposes["demo"]["reserved_upper_micro"])
                if (upper > remaining or upper > purposes[purpose]["limit_micro"] - purposes[purpose]["reserved_upper_micro"]
                        or purpose != "demo" and upper > remaining - preserved):
                    raise _error("CREDIT_LIMIT_REACHED", 429)
                connection.execute("INSERT OR IGNORE INTO credit_approvals(approval_id,context_fingerprint,profile_fingerprint,created_at) VALUES(?,?,?,?)", (self._approval_id, self._context, self._profile_hash, stamp))
                connection.execute("UPDATE credit_project SET reserved_upper_micro=reserved_upper_micro+?,updated_at=? WHERE singleton=1", (upper, stamp))
                connection.execute("UPDATE credit_purposes SET reserved_upper_micro=reserved_upper_micro+? WHERE purpose=?", (upper, purpose))
                connection.execute("INSERT INTO credit_requests(request_id,approval_id,purpose,request_hash,kind,model,route,input_bytes,input_tokens_bound,output_tokens_bound,reserved_upper_micro,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,'RESERVED',?,?)",
                    (request_id, self._approval_id, purpose, request_hash, kind, model, route, input_bytes, input_tokens_bound, plan["output_tokens_bound"], upper, stamp, stamp))
                connection.commit()
                return {"request_id": request_id, "replayed": False, "state": "RESERVED"}
        except ServiceError:
            if connection is not None:
                try:
                    connection.rollback()
                except (sqlite3.Error, OSError):
                    pass
            raise
        except (sqlite3.Error, OSError, KeyError, TypeError, ValueError, OverflowError):
            if connection is not None:
                try:
                    connection.rollback()
                except (sqlite3.Error, OSError):
                    pass
            raise _error("LIVE_BUDGET_UNAVAILABLE") from None
        finally:
            if connection is not None:
                connection.close()

    def settle(self, request_id, actual_credits=None, *, usage=None, actual_credits_source=None):
        if not self.confirmed or not self._approval_id:
            raise _error()
        if not isinstance(request_id, str) or _IDENTIFIER.fullmatch(request_id) is None:
            raise _error("LIVE_REQUEST_CONFLICT", 409)
        actual = None
        if actual_credits is not None:
            if actual_credits_source != "provider_reported":
                raise _error()
            actual = _micro(actual_credits)
        if usage is None:
            usage = {}
        if (not isinstance(usage, dict) or set(usage) - {"input_tokens", "output_tokens"}
                or any(type(value) is not int or not 0 <= value <= 1_000_000_000 for value in usage.values())):
            raise _error()
        if not self._path.is_file():
            raise _error("LIVE_BUDGET_UNAVAILABLE")
        connection = None
        try:
            connection = self._connect(False)
            connection.execute("BEGIN IMMEDIATE")
            if not self._schema_state(connection):
                raise _error("LIVE_BUDGET_UNAVAILABLE")
            project = self._check_binding(connection)
            self._counter_rows(connection, project)
            row = connection.execute("SELECT * FROM credit_requests WHERE request_id=? AND approval_id=?", (request_id, self._approval_id)).fetchone()
            if row is None:
                raise _error("LIVE_REQUEST_CONFLICT", 409)
            reported = {key: usage.get(key, row["reported_" + key]) for key in ("input_tokens", "output_tokens")}
            if any(row["reported_" + key] is not None and reported[key] != row["reported_" + key] for key in reported):
                raise _error("LIVE_REQUEST_CONFLICT", 409)
            if row["provider_actual_micro"] is not None:
                if actual is not None and actual != row["provider_actual_micro"]:
                    raise _error("LIVE_REQUEST_CONFLICT", 409)
                actual = row["provider_actual_micro"]
            model = self._profile["models"][row["model"]]
            estimated = None
            if any(value is not None for value in reported.values()):
                estimated = (reported["input_tokens"] or 0) * model["input_microcredits_per_token"] + (reported["output_tokens"] or 0) * model["output_microcredits_per_token"]
            mismatch = (actual is not None and actual > row["reserved_upper_micro"]
                or reported["input_tokens"] is not None and reported["input_tokens"] > row["input_tokens_bound"]
                or reported["output_tokens"] is not None and reported["output_tokens"] > row["output_tokens_bound"])
            stamp = datetime.now(timezone.utc).isoformat()
            connection.execute("UPDATE credit_requests SET state=?,provider_actual_micro=?,reported_input_tokens=?,reported_output_tokens=?,token_estimate_micro=?,updated_at=? WHERE request_id=?",
                ("SETTLED" if actual is not None else "UNKNOWN", actual, reported["input_tokens"], reported["output_tokens"], estimated, stamp, request_id))
            if mismatch:
                connection.execute("UPDATE credit_project SET frozen=1,frozen_reason='TOKEN_OR_CREDIT_BOUND_MISMATCH',updated_at=? WHERE singleton=1", (stamp,))
            connection.commit()
            return {"request_id": request_id, "state": "SETTLED" if actual is not None else "UNKNOWN",
                    "provider_reported_actual_credits": None if actual is None else _credits(actual),
                    "token_based_estimate_credits": None if estimated is None else _credits(estimated),
                    "reserved_upper_credits": _credits(row["reserved_upper_micro"]), "profile_mismatch": bool(mismatch)}
        except ServiceError:
            if connection is not None:
                try:
                    connection.rollback()
                except (sqlite3.Error, OSError):
                    pass
            raise
        except (sqlite3.Error, OSError, KeyError, TypeError, ValueError, OverflowError):
            if connection is not None:
                try:
                    connection.rollback()
                except (sqlite3.Error, OSError):
                    pass
            raise _error("LIVE_BUDGET_UNAVAILABLE") from None
        finally:
            if connection is not None:
                connection.close()

    def freeze(self, reason="MODEL_IDENTITY_MISMATCH"):
        if reason not in {"MODEL_IDENTITY_MISMATCH", "TOKEN_BOUND_EXCEEDED", "PRICING_PROFILE_MISMATCH"}:
            raise _error()
        self._forced_frozen = True
        if not self.confirmed or not self._approval_id or not self._path.is_file():
            raise _error()
        connection = None
        try:
            connection = self._connect(False)
            connection.execute("BEGIN IMMEDIATE")
            if not self._schema_state(connection):
                raise _error("LIVE_BUDGET_UNAVAILABLE")
            self._check_binding(connection)
            connection.execute("UPDATE credit_project SET frozen=1,frozen_reason=?,updated_at=? WHERE singleton=1", (reason, datetime.now(timezone.utc).isoformat()))
            connection.commit()
        except ServiceError:
            if connection is not None:
                connection.rollback()
            raise
        except (sqlite3.Error, OSError, TypeError, ValueError):
            if connection is not None:
                try:
                    connection.rollback()
                except (sqlite3.Error, OSError):
                    pass
            raise _error("LIVE_BUDGET_UNAVAILABLE") from None
        finally:
            if connection is not None:
                connection.close()
