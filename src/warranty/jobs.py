"""Persistent local document preparation jobs with leased, fenced execution.

The queue invokes one constructor-injected Python callable. Request data never
selects a module, command, worker identity, source permission, or result.
Settlement jobs and their worker are intentionally separate.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import threading
import time
import uuid

from .documents import APPLICABILITY_FIELDS
from .errors import ServiceError


ACTIVE = frozenset({"QUEUED", "RUNNING"})
TERMINAL = frozenset({"COMPLETED", "BLOCKED", "FAILED"})
STAGES = frozenset({
    "QUEUED", "RECOVERING", "QUERYING", "DISCOVERING", "SELECTING", "FETCHING",
    "CHECKING_DOCUMENTS", "PARSING_STORED_BODIES", "INDEXING", "INDEX_READY",
    "DOC_BLOCKED", "COMPLETED", "FAILED",
})
COUNTS = frozenset({"candidate_count", "body_count", "chunk_count"})
SAFE_CODES = frozenset({
    "APPLICABILITY_UNKNOWN", "CONFIG_INVALID", "DATASET_CHANGED", "DOC_BLOCKED",
    "DOCUMENT_JOB_FAILED", "FORBIDDEN", "FETCH_FAILED", "INDEX_EMPTY", "INVALID_INPUT",
    "JOB_CLAIM_LOST", "JOB_INTERRUPTED", "JOB_PROGRESS_INVALID", "JOB_REQUEST_INVALID",
    "JOB_RESULT_INVALID", "JOB_RETRY_LIMIT", "KEY_MISSING", "MODEL_OUTPUT_INVALID",
    "MODEL_REMOTE_BLOCKED", "NO_RESULTS", "OWN_DOCUMENT_MISSING", "PARSE_FAILED",
    "PROMPT_CONFIG_MISSING", "PROMPT_CONFIG_INVALID", "SOURCE_BLOCKED",
    "SOURCE_REVIEW_MISSING", "SEARCH_UNAVAILABLE", "COLLECTION_DISABLED",
    "SEARCH_FAILED", "SEARCH_PROVIDER_UNAVAILABLE", "DOCUMENT_VERSION_CONFLICT",
    "NETWORK_DISABLED", "SOURCE_NOT_REGISTERED", "HTTP_RATE_LIMITED", "HTTP_FAILED",
    "MODEL_UNAVAILABLE", "EMBEDDING_INPUT_INVALID", "EMBEDDING_OUTPUT_INVALID",
    "PROCESSING_APPROVAL_REQUIRED", "USAGE_LIMIT_REACHED", "LIVE_BUDGET_UNCONFIRMED",
    "LIVE_BUDGET_UNAVAILABLE", "CREDIT_LIMIT_REACHED", "LIVE_REQUEST_CONFLICT",
    "LIVE_PURPOSE_REQUIRED", "LIVE_MODEL_IDENTITY_MISMATCH",
})


def _error(code, status=422, retryable=False):
    return ServiceError(code, "문서 준비 작업의 상태와 허용 범위를 확인해 주세요.", status, retryable)


def _stamp():
    return datetime.now(timezone.utc).isoformat()


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _code(value, fallback="DOCUMENT_JOB_FAILED"):
    return value if isinstance(value, str) and value in SAFE_CODES else fallback


class _Progress:
    """Claim capability handed only to the fixed server-side handler."""

    def __init__(self, queue, job_id, token):
        self._queue, self._job_id, self._token = queue, job_id, token

    def __call__(self, stage, **fields):
        return self._queue._report(self._job_id, self._token, stage, fields)

    def check(self, connection=None):
        """Check inside a handler's commit transaction to fence domain writes."""
        if connection is not None:
            self._queue._check_claim(connection, self._job_id, self._token)
            return True
        with self._queue.db.connect() as connection:
            self._queue._check_claim(connection, self._job_id, self._token)
        return True


class DocumentJobQueue:
    def __init__(self, db, catalog, handler, *, authorize=None, manager_check=None,
                 lease_seconds=120, max_attempts=3, clock=time.time, auto_start=False):
        if not callable(handler) or not callable(clock) or (authorize is not None and not callable(authorize)):
            raise ValueError("A fixed document handler and authorization callable are required")
        if manager_check is not None and not callable(manager_check):
            raise ValueError("Manager access must be a server-owned callable")
        if (isinstance(lease_seconds, bool) or not isinstance(lease_seconds, (int, float))
                or not math.isfinite(lease_seconds) or not .1 <= lease_seconds <= 3600):
            raise ValueError("Document claim duration is out of bounds")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 10 or type(auto_start) is not bool:
            raise ValueError("Invalid document worker configuration")
        self.db, self.catalog, self.handler = db, catalog, handler
        self._authorize, self._manager_check = authorize, manager_check
        self.lease_seconds, self.max_attempts, self._clock = float(lease_seconds), max_attempts, clock
        self.auto_start = auto_start
        self._stop, self._wake, self._thread_lock = threading.Event(), threading.Event(), threading.Lock()
        self._thread = None
        self.last_worker_error = None

    def _manager(self, actor):
        if not isinstance(actor, str) or not actor or len(actor) > 128 or self._manager_check is None:
            return False
        try:
            return self._manager_check(actor) is True
        except Exception:
            return False

    def _allow(self, actor, item_id, action):
        if not isinstance(actor, str) or not actor.strip() or len(actor) > 128:
            raise _error("FORBIDDEN", 403)
        self.catalog.get(item_id)
        if self._authorize is not None:
            try:
                allowed = self._authorize(actor, item_id, action) is True
            except Exception:
                allowed = False
            if not allowed:
                raise _error("FORBIDDEN", 403)
            return
        with self.db.connect() as connection:
            known = connection.execute("SELECT 1 FROM demo_users WHERE user_id=?", (actor,)).fetchone()
        if known is None and not self._manager(actor):
            raise _error("FORBIDDEN", 403)

    @staticmethod
    def _request(request):
        if request is None:
            request = {}
        if not isinstance(request, dict) or set(request) - {"question", "applicability"}:
            raise _error("INVALID_INPUT", 400)
        question = request.get("question", "보증 이전 조건과 적용 계약 문서를 준비해 주세요.")
        if not isinstance(question, str) or not question.strip() or len(question) > 5000:
            raise _error("INVALID_INPUT", 400)
        applicability = request.get("applicability", {})
        if applicability is None:
            applicability = {}
        if not isinstance(applicability, dict) or set(applicability) - set(APPLICABILITY_FIELDS):
            raise _error("INVALID_INPUT", 400)
        if any(not isinstance(value, str) or len(value) > 200 for value in applicability.values()):
            raise _error("INVALID_INPUT", 400)
        normalized = {key: value.strip() for key, value in applicability.items() if value.strip()}
        return {"question": question.strip(), "applicability": normalized}

    def enqueue(self, actor, item_id, request=None):
        self._allow(actor, item_id, "enqueue")
        normalized = self._request(request)
        snapshot = {"requester": actor, "item_id": item_id, **normalized,
                    "dataset_hash": self.catalog.dataset_hash}
        encoded = _canonical(snapshot)
        request_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        with self.db.transaction() as connection:
            existing = connection.execute(
                "SELECT j.job_id FROM index_jobs j JOIN document_job_runs r ON r.job_id=j.job_id "
                "WHERE j.requester=? AND r.request_hash=? AND j.status IN ('QUEUED','RUNNING') "
                "ORDER BY j.created_at,j.job_id LIMIT 1", (actor, request_hash)).fetchone()
            if existing:
                job_id = existing["job_id"]
            else:
                count = self._active_count(connection, actor)
                if count >= 20:
                    raise _error("JOB_LIMIT", 429, True)
                job_id, stamp = uuid.uuid4().hex, _stamp()
                connection.execute(
                    "INSERT INTO index_jobs(job_id,requester,item_id,status,stage,created_at,updated_at) "
                    "VALUES(?,?,?,'QUEUED','QUEUED',?,?)", (job_id, actor, item_id, stamp, stamp))
                connection.execute(
                    "INSERT INTO document_job_runs(job_id,request_json,request_hash,max_attempts) VALUES(?,?,?,?)",
                    (job_id, encoded, request_hash, self.max_attempts))
        self._wake.set()
        if self.auto_start:
            self.start()
        return self.job(actor, job_id)

    @staticmethod
    def _active_count(connection, actor):
        return connection.execute(
            "SELECT COUNT(*) FROM index_jobs j JOIN document_job_runs r ON r.job_id=j.job_id "
            "WHERE j.requester=? AND j.status IN ('QUEUED','RUNNING')", (actor,)).fetchone()[0]

    @staticmethod
    def _public(row):
        fields = ("job_id", "requester", "item_id", "status", "stage", "candidate_count", "body_count",
                  "chunk_count", "revision", "created_at", "updated_at")
        result = {key: row[key] for key in fields}
        result["failure_code"] = _code(row["failure_code"]) if row["failure_code"] else None
        result["attempt_no"] = row["attempt_no"] or 0
        result["retryable"] = bool(row["managed_job"] and row["status"] in {"FAILED", "BLOCKED"}
                                    and result["attempt_no"] < row["max_attempts"])
        return result

    @staticmethod
    def _row(connection, job_id):
        return connection.execute(
            "SELECT j.*,r.attempt_no,r.max_attempts,r.request_json,r.request_hash,r.claim_token,r.lease_until,"
            "CASE WHEN r.job_id IS NULL THEN 0 ELSE 1 END AS managed_job FROM index_jobs j "
            "LEFT JOIN document_job_runs r ON r.job_id=j.job_id WHERE j.job_id=?", (job_id,)).fetchone()

    def job(self, actor, job_id):
        if not isinstance(job_id, str) or not 1 <= len(job_id) <= 128:
            raise _error("JOB_NOT_FOUND", 404)
        with self.db.connect() as connection:
            row = self._row(connection, job_id)
        if row is None:
            raise _error("JOB_NOT_FOUND", 404)
        if actor != row["requester"] and not self._manager(actor):
            raise _error("FORBIDDEN", 403)
        self._allow(actor, row["item_id"], "read")
        return self._public(row)

    def retry(self, actor, job_id):
        view = self.job(actor, job_id)
        if actor != view["requester"]:
            raise _error("FORBIDDEN", 403)
        self._allow(actor, view["item_id"], "retry")
        with self.db.transaction() as connection:
            row = self._row(connection, job_id)
            if row is None:
                raise _error("JOB_NOT_FOUND", 404)
            if actor != row["requester"]:
                raise _error("FORBIDDEN", 403)
            if row["managed_job"] and row["status"] in ACTIVE:
                return self._public(row)
            if not row["managed_job"] or row["status"] not in {"FAILED", "BLOCKED"}:
                raise _error("JOB_NOT_RETRYABLE", 409)
            if row["attempt_no"] >= row["max_attempts"]:
                raise _error("JOB_RETRY_LIMIT", 409)
            existing = connection.execute(
                "SELECT j.job_id FROM index_jobs j JOIN document_job_runs r ON r.job_id=j.job_id "
                "WHERE j.requester=? AND r.request_hash=? AND j.status IN ('QUEUED','RUNNING') LIMIT 1",
                (actor, row["request_hash"])).fetchone()
            if existing:
                job_id = existing["job_id"]
            else:
                if self._active_count(connection, actor) >= 20:
                    raise _error("JOB_LIMIT", 429, True)
                connection.execute("UPDATE document_job_runs SET claim_token=NULL,claimed_at=NULL,lease_until=NULL WHERE job_id=?", (job_id,))
                connection.execute("UPDATE index_jobs SET status='QUEUED',stage='QUEUED',failure_code=NULL,candidate_count=0,body_count=0,chunk_count=0,revision=NULL,updated_at=? WHERE job_id=?", (_stamp(), job_id))
        self._wake.set()
        if self.auto_start:
            self.start()
        return self.job(actor, job_id)

    def _recover(self, connection):
        rows = connection.execute(
            "SELECT j.job_id,r.attempt_no,r.max_attempts FROM index_jobs j JOIN document_job_runs r ON r.job_id=j.job_id "
            "WHERE j.status='RUNNING' AND r.lease_until<=?", (self._clock(),)).fetchall()
        for row in rows:
            exhausted = row["attempt_no"] >= row["max_attempts"]
            connection.execute("UPDATE document_job_runs SET claim_token=NULL,claimed_at=NULL,lease_until=NULL WHERE job_id=?", (row["job_id"],))
            connection.execute("UPDATE index_jobs SET status=?,stage=?,failure_code=?,updated_at=? WHERE job_id=?",
                ("FAILED" if exhausted else "QUEUED", "FAILED" if exhausted else "RECOVERING",
                 "JOB_RETRY_LIMIT" if exhausted else "JOB_INTERRUPTED", _stamp(), row["job_id"]))
        return len(rows)

    def recover_expired(self):
        with self.db.transaction() as connection:
            return self._recover(connection)

    def _claim(self):
        with self.db.transaction() as connection:
            self._recover(connection)
            row = connection.execute(
                "SELECT j.job_id FROM index_jobs j JOIN document_job_runs r ON r.job_id=j.job_id "
                "WHERE j.status='QUEUED' AND r.attempt_no<r.max_attempts AND r.claim_token IS NULL "
                "ORDER BY j.created_at,j.job_id LIMIT 1").fetchone()
            if row is None:
                return None
            job_id, token, claimed_at = row["job_id"], uuid.uuid4().hex, self._clock()
            connection.execute("UPDATE document_job_runs SET attempt_no=attempt_no+1,claim_token=?,claimed_at=?,lease_until=? WHERE job_id=?",
                               (token, claimed_at, claimed_at + self.lease_seconds, job_id))
            connection.execute("UPDATE index_jobs SET status='RUNNING',stage='CHECKING_DOCUMENTS',failure_code=NULL,candidate_count=0,body_count=0,chunk_count=0,revision=NULL,updated_at=? WHERE job_id=?", (_stamp(), job_id))
            return dict(self._row(connection, job_id))

    def _check_claim(self, connection, job_id, token):
        row = connection.execute(
            "SELECT 1 FROM document_job_runs r JOIN index_jobs j ON j.job_id=r.job_id "
            "WHERE r.job_id=? AND r.claim_token=? AND r.lease_until>? AND j.status='RUNNING'",
            (job_id, token, self._clock())).fetchone()
        if row is None:
            raise _error("JOB_CLAIM_LOST", 409)

    @staticmethod
    def _fields(fields):
        if not isinstance(fields, dict) or set(fields) - COUNTS - {"revision"}:
            raise _error("JOB_PROGRESS_INVALID", 400)
        result = {}
        for key, value in fields.items():
            if key in COUNTS and (type(value) is not int or not 0 <= value <= 1_000_000):
                raise _error("JOB_PROGRESS_INVALID", 400)
            if key == "revision" and value is not None and (not isinstance(value, str) or not 1 <= len(value) <= 128):
                raise _error("JOB_PROGRESS_INVALID", 400)
            result[key] = value
        return result

    def _report(self, job_id, token, stage, fields):
        if not isinstance(stage, str) or stage not in STAGES:
            raise _error("JOB_PROGRESS_INVALID", 400)
        values = self._fields(fields)
        values.update(stage=stage, updated_at=_stamp())
        with self.db.transaction() as connection:
            self._check_claim(connection, job_id, token)
            connection.execute("UPDATE index_jobs SET " + ",".join(key + "=?" for key in values) + " WHERE job_id=?",
                               (*values.values(), job_id))
            connection.execute("UPDATE document_job_runs SET lease_until=? WHERE job_id=? AND claim_token=?",
                               (self._clock() + self.lease_seconds, job_id, token))

    def _renew(self, job_id, token):
        with self.db.transaction() as connection:
            self._check_claim(connection, job_id, token)
            connection.execute("UPDATE document_job_runs SET lease_until=? WHERE job_id=? AND claim_token=?",
                               (self._clock() + self.lease_seconds, job_id, token))

    def _finish(self, claim, result):
        if not isinstance(result, dict) or result.get("status") not in TERMINAL:
            result = {"status": "FAILED", "failure_code": "JOB_RESULT_INVALID"}
        if any(key in result and result[key] != claim[key] for key in ("job_id", "requester", "item_id")):
            result = {"status": "FAILED", "failure_code": "JOB_RESULT_INVALID"}
        status = result["status"]
        stage = result.get("stage", {"COMPLETED": "INDEX_READY", "BLOCKED": "DOC_BLOCKED", "FAILED": "FAILED"}[status])
        if not isinstance(stage, str) or stage not in STAGES:
            stage = "FAILED"
            status = "FAILED"
        if status == "COMPLETED" and stage not in {"INDEX_READY", "COMPLETED"}:
            stage = "INDEX_READY"
        try:
            fields = self._fields({key: value for key, value in result.items() if key in COUNTS | {"revision"}})
        except ServiceError:
            status, stage, fields = "FAILED", "FAILED", {}
            result = {"failure_code": "JOB_RESULT_INVALID"}
        fields.update(status=status, stage=stage, updated_at=_stamp(),
                      failure_code=None if status == "COMPLETED" else _code(result.get("failure_code")))
        with self.db.transaction() as connection:
            try:
                self._check_claim(connection, claim["job_id"], claim["claim_token"])
            except ServiceError:
                return False
            if fields.get("revision") and not connection.execute("SELECT 1 FROM index_manifests WHERE revision=?", (fields["revision"],)).fetchone():
                fields.update(status="FAILED", stage="FAILED", failure_code="JOB_RESULT_INVALID", revision=None)
            connection.execute("UPDATE index_jobs SET " + ",".join(key + "=?" for key in fields) + " WHERE job_id=?", (*fields.values(), claim["job_id"]))
            connection.execute("UPDATE document_job_runs SET claim_token=NULL,claimed_at=NULL,lease_until=NULL WHERE job_id=?", (claim["job_id"],))
        return True

    def _execute(self, claim):
        stopped = threading.Event()
        progress = _Progress(self, claim["job_id"], claim["claim_token"])
        def heartbeat():
            while not stopped.wait(max(.05, self.lease_seconds / 3)):
                try:
                    self._renew(claim["job_id"], claim["claim_token"])
                except Exception:
                    return
        heartbeat_thread = threading.Thread(target=heartbeat, name="warranty-document-lease", daemon=True)
        heartbeat_thread.start()
        try:
            try:
                snapshot = json.loads(claim["request_json"])
                if (not isinstance(snapshot, dict)
                        or set(snapshot) != {"requester", "item_id", "question", "applicability", "dataset_hash"}
                        or snapshot["requester"] != claim["requester"] or snapshot["item_id"] != claim["item_id"]
                        or hashlib.sha256(_canonical(snapshot).encode("utf-8")).hexdigest() != claim["request_hash"]):
                    raise _error("JOB_REQUEST_INVALID", 400)
                normalized = self._request({key: snapshot[key] for key in ("question", "applicability")})
                if normalized != {key: snapshot[key] for key in normalized}:
                    raise _error("JOB_REQUEST_INVALID", 400)
                self._allow(claim["requester"], claim["item_id"], "run")
                if snapshot["dataset_hash"] != self.catalog.dataset_hash:
                    raise _error("DATASET_CHANGED", 409)
                progress.check()
                request = {**deepcopy(snapshot), "job_id": claim["job_id"], "attempt_no": claim["attempt_no"]}
                result = self.handler(request, progress)
            except ServiceError as error:
                result = {"status": "FAILED" if error.retryable else "BLOCKED", "failure_code": _code(error.code)}
            except Exception:
                result = {"status": "FAILED", "failure_code": "DOCUMENT_JOB_FAILED"}
            return self._finish(claim, result)
        finally:
            stopped.set()
            heartbeat_thread.join(timeout=2)

    def run_pending(self, limit=10):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Document worker batch size is out of bounds")
        processed = []
        for _ in range(limit):
            claim = self._claim()
            if claim is None:
                break
            if self._execute(claim):
                with self.db.connect() as connection:
                    processed.append(self._public(self._row(connection, claim["job_id"])))
        return {"processed_jobs": len(processed), "jobs": processed}

    @property
    def worker_running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self, poll_interval=.25):
        if (isinstance(poll_interval, bool) or not isinstance(poll_interval, (int, float))
                or not math.isfinite(poll_interval) or not .05 <= poll_interval <= 60):
            raise ValueError("Invalid local document worker interval")
        with self._thread_lock:
            if self.worker_running:
                return False
            self._stop.clear()
            def work():
                while not self._stop.is_set():
                    try:
                        result = self.run_pending(limit=1)
                        self.last_worker_error = None
                    except Exception:
                        result = {"processed_jobs": 0}
                        self.last_worker_error = "DOCUMENT_WORKER_FAILED"
                    if result["processed_jobs"] == 0:
                        self._wake.wait(poll_interval)
                        self._wake.clear()
            self._thread = threading.Thread(target=work, name="warranty-document-worker", daemon=True)
            self._thread.start()
        return True

    def stop(self, timeout=5):
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 0 <= timeout <= 60):
            raise ValueError("Invalid local document worker shutdown timeout")
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        return not self.worker_running

    close = stop
