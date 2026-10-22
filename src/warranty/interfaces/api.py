"""Same-origin HTTP adapter over the shared application services."""
from pathlib import Path
import json
import re
import uuid
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

from warranty.errors import ServiceError
from warranty.live import LIVE_PURPOSES
from warranty.security import SessionStore


COOKIE_NAME = "warranty_session"
_IDENTITY_FIELDS = {"actor", "user_id", "role", "session_id", "csrf_token"}
_APPLICABILITY_FIELDS = {"country", "enrollment_date", "plan_generation", "payment_method", "channel", "document_type"}
_ACTIONS = {
    "contracts": "contract", "contract-consents": "contract_consent",
    "deposit": "deposit", "receipt": "receipt", "reviews": "adopt_review",
    "transfer-events": "transfer_event", "evidence-bundles": "evidence",
    "evidence-confirmations": "confirm_evidence",
    "settlement-proposals": "proposal", "settlement-consents": "proposal_consent",
    "settlement-executions": "reserve", "disputes": "dispute",
    "resolutions": "resolution", "resolution-consents": "resolution_consent",
    "close": "close", "cancel-before-deposit": "cancel_consent",
}


def _fail(code, message, status=400, retryable=False):
    raise ServiceError(code, message, status, retryable)


def _string(value, name, maximum=400, required=True):
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        _fail("INVALID_INPUT", f"{name} 입력 형식이나 길이를 확인해 주세요.")
    return value.strip()


def _integer(value, name, minimum=0, maximum=10**12):
    if type(value) is not int or not minimum <= value <= maximum:
        _fail("INVALID_INPUT", f"{name}에 허용 범위의 정수를 입력해 주세요.")
    return value


def _applicability(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - _APPLICABILITY_FIELDS:
        _fail("INVALID_INPUT", "적용성 입력 형식을 확인해 주세요.")
    for key, item in value.items():
        _string(key, "적용성 항목", 80)
        if item is not None and (not isinstance(item, str) or len(item) > 200):
            _fail("INVALID_INPUT", "적용성 값이 너무 길거나 지원하지 않는 형식입니다.")
    return {key: item for key, item in value.items() if item is not None and item.strip()}


def _public_runtime(value):
    """Never serialize private runtime configuration or adapter objects."""
    value = value if isinstance(value, dict) else {}
    projected = {}
    for kind in ("chat", "embedding"):
        adapter = value.get(kind, {})
        adapter = adapter if isinstance(adapter, dict) else {}
        provider = adapter.get("provider")
        projected[kind] = {"provider": provider if provider in {"mock", "openai", "gms"} else "unavailable",
                           **{field: adapter.get(field) is True for field in ("configured", "ready", "is_mock")}}
    collection = value.get("collection", {})
    collection = collection if isinstance(collection, dict) else {}
    count = collection.get("registered_target_count")
    projected["collection"] = {"enabled": collection.get("enabled") is True,
                               "network_enabled": collection.get("network_enabled") is True,
                               "registered_target_count": count if type(count) is int and 0 <= count <= 100000 else 0}
    for field in ("external_processing_approved", "question_processing_approved", "document_worker_enabled"):
        projected[field] = value.get(field) is True
    live = value.get("live", {})
    live = live if isinstance(live, dict) else {}
    purposes = live.get("purposes")
    projected["live"] = {**{field: live.get(field) is True for field in ("enabled", "ready", "budget_confirmed")},
                         "purposes": [purpose for purpose in LIVE_PURPOSES
                                      if isinstance(purposes, list) and purpose in purposes]}
    return projected


async def _body(request, *, allow_identity=False):
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > 65536:
            _fail("INVALID_INPUT", "요청 내용은 64KB 이하로 입력해 주세요.", 400)
        chunks.append(chunk)
    raw = b"".join(chunks)
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
        _fail("INVALID_INPUT", "JSON 요청이 필요합니다.")
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        _fail("INVALID_INPUT", "올바른 JSON 요청이 필요합니다.")
    if not isinstance(body, dict):
        _fail("INVALID_INPUT", "JSON 객체가 필요합니다.")
    if not allow_identity and _IDENTITY_FIELDS.intersection(body):
        _fail("FORBIDDEN", "사용자 신원은 서버 세션에서 확인합니다.", 403)
    return body


def create_app(application=None):
    """Create an app; injection keeps both frontends on the same service facade."""
    if application is None:
        from warranty.application import build_application
        application = build_application()
    app = FastAPI(title="보증 조건 확인 · 거래 시연", docs_url=None, redoc_url=None)
    app.state.application = application
    sessions = SessionStore(demo_mode=application.settings.demo_mode)
    app.state.sessions = sessions

    def respond(request, data=None, error=None, status=200):
        return JSONResponse(jsonable_encoder({"data": data, "error": error,
                                              "request_id": request.state.request_id}), status_code=status)

    def identity(request, mutation=False):
        session_id = request.cookies.get(COOKIE_NAME, "")
        if mutation:
            return sessions.require_mutation(session_id, request.headers.get("x-csrf-token", ""),
                                             request.headers.get("origin", ""), str(request.base_url).rstrip("/"))
        return sessions.resolve(session_id)

    def key_and_version(request, body):
        key = request.headers.get("idempotency-key", "")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", key):
            _fail("INVALID_INPUT", "8~128자의 Idempotency-Key가 필요합니다.")
        version = _integer(body.pop("expected_version", None), "expected_version")
        return key, version

    @app.middleware("http")
    async def secure_response(request, call_next):
        request.state.request_id = uuid.uuid4().hex
        loopback = {"localhost", "127.0.0.1", "::1"}
        if application.settings.demo_mode and (not request.client or request.client.host not in loopback
                                               or request.url.hostname not in loopback):
            response = respond(request, error={"code": "FORBIDDEN", "message": "로컬 시연 서버는 이 컴퓨터에서만 접근할 수 있습니다.", "retryable": False}, status=403)
        else:
            response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
            "connect-src 'self'; object-src 'none'; frame-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ServiceError)
    async def service_error(request, exc):
        return respond(request, error=exc.as_dict(), status=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return respond(request, error={"code": "INVALID_INPUT", "message": "요청 형식을 확인해 주세요.", "retryable": False}, status=400)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return respond(request, error={"code": "NOT_FOUND" if exc.status_code == 404 else "INVALID_INPUT",
                                       "message": "요청한 경로 또는 메서드를 확인해 주세요.", "retryable": False}, status=exc.status_code)

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        return respond(request, error={"code": "INTERNAL_ERROR", "message": "요청을 처리하지 못했습니다. 요청 ID로 확인해 주세요.", "retryable": False}, status=500)

    @app.get("/health")
    async def health(request: Request):
        return respond(request, {"status": "ok", "demo_mode": application.settings.demo_mode,
                                 "real_uploads": False})

    @app.get("/runtime")
    async def runtime_status(request: Request):
        identity(request)
        status_provider = getattr(application, "runtime_status", None)
        status = status_provider() if callable(status_provider) else {}
        return respond(request, _public_runtime(status))

    @app.post("/session")
    async def login(request: Request):
        body = await _body(request, allow_identity=True)
        if set(body) != {"actor"}:
            _fail("INVALID_INPUT", "시연 사용자만 선택해 주세요.")
        origin = request.headers.get("origin", "")
        request_origin = str(request.base_url).rstrip("/")
        if origin != request_origin or urlsplit(request_origin).hostname not in {"localhost", "127.0.0.1", "::1"}:
            _fail("FORBIDDEN", "로컬 동일 출처에서만 시연 로그인할 수 있습니다.", 403)
        session = sessions.create_demo(_string(body["actor"], "시연 사용자", 20),
                                       request.client.host if request.client else "", origin)
        old = request.cookies.get(COOKIE_NAME)
        if old:
            try:
                sessions.revoke(old)
            except ServiceError:
                pass
        response = respond(request, {"actor": session.actor, "csrf_token": session.csrf_token, "demo_mode": True})
        response.set_cookie(COOKIE_NAME, session.id, httponly=True, samesite="strict",
                            secure=request.url.scheme == "https", max_age=3600, path="/")
        return response

    @app.get("/session")
    async def session_info(request: Request):
        session = identity(request)
        return respond(request, {"actor": session.actor, "csrf_token": session.csrf_token,
                                 "demo_mode": application.settings.demo_mode})

    @app.post("/logout")
    async def logout(request: Request):
        session = identity(request, True)
        sessions.revoke(session.id)
        response = respond(request, {"logged_out": True})
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    @app.get("/items")
    async def items(request: Request, offset: int = 0, limit: int = 30):
        if not application.settings.demo_mode:
            identity(request)
        _integer(offset, "offset", maximum=100000)
        _integer(limit, "limit", minimum=1, maximum=100)
        all_items = application.catalog.items
        return respond(request, {"items": all_items[offset:offset + limit], "total": len(all_items),
                                 "offset": offset, "limit": limit, "summary": application.catalog.summary()})

    @app.get("/items/{item_id}")
    async def item(request: Request, item_id: str):
        if not application.settings.demo_mode:
            identity(request)
        return respond(request, application.catalog.get(_string(item_id, "항목 ID", 120)))

    @app.post("/search")
    async def search(request: Request):
        identity(request, True)
        body = await _body(request)
        query = _string(body.get("query", ""), "검색어", 1000, False)
        provider = body.get("provider")
        if provider is not None:
            provider = _string(provider, "제공사", 80)
        max_fee = body.get("max_fee")
        if max_fee is not None:
            max_fee = _integer(max_fee, "월요금", maximum=10**9)
        transfer = body.get("transfer_required", False)
        if type(transfer) is not bool:
            _fail("INVALID_INPUT", "이전 조건은 참/거짓으로 입력해 주세요.")
        return respond(request, application.catalog.search(query=query, provider=provider, max_fee=max_fee, transfer_required=transfer))

    @app.post("/chat")
    async def chat(request: Request):
        session = identity(request, True)
        body = await _body(request)
        item_id = _string(body.get("item_id"), "항목 ID", 120)
        question = _string(body.get("question"), "질문", 4000)
        top_k = _integer(body.get("top_k", 6), "Top-k", minimum=1, maximum=20)
        strategy = body.get("strategy", "similarity")
        if strategy not in {"similarity", "mmr"}:
            _fail("INVALID_INPUT", "지원하는 검색 방식을 선택해 주세요.")
        return respond(request, application.rag.ask(session.actor, item_id, question,
                       applicability=_applicability(body.get("applicability")), top_k=top_k, strategy=strategy))

    @app.get("/items/{item_id}/history")
    async def history(request: Request, item_id: str):
        session = identity(request)
        application.catalog.get(item_id)
        return respond(request, application.rag.history(session.actor, item_id))

    @app.post("/items/{item_id}/checklist")
    async def checklist(request: Request, item_id: str):
        session = identity(request, True)
        body = await _body(request)
        if set(body) - {"applicability", "review_id"}:
            _fail("INVALID_INPUT", "체크리스트 입력 항목을 확인해 주세요.")
        review_id = body.get("review_id")
        if review_id is not None:
            review_id = _string(review_id, "검토 ID", 120)
        application.catalog.get(item_id)
        return respond(request, application.rag.checklist(item_id, _applicability(body.get("applicability")),
                       user_id=session.actor, review_id=review_id))

    @app.post("/items/{item_id}/index-jobs")
    async def prepare(request: Request, item_id: str):
        session = identity(request, True)
        body = await _body(request)
        if set(body) - {"applicability", "question"}:
            _fail("FORBIDDEN", "서버에 등록된 문서만 준비할 수 있습니다.", 403)
        application.catalog.get(item_id)
        question = body.get("question")
        if question is not None:
            question = _string(question, "문서 준비 질문", 5000)
        enqueue = getattr(application.rag, "enqueue_prepare", None)
        if not callable(enqueue):
            _fail("JOBS_UNAVAILABLE", "문서 준비 처리기를 설정한 후 다시 요청해 주세요.", 503)
        return respond(request, enqueue(session.actor, item_id, _applicability(body.get("applicability")), question), status=202)

    @app.get("/index-jobs/{job_id}")
    async def job(request: Request, job_id: str):
        session = identity(request)
        return respond(request, application.rag.job(session.actor, job_id))

    @app.post("/index-jobs/{job_id}/retry")
    async def retry_document_job(request: Request, job_id: str):
        session = identity(request, True)
        if await _body(request):
            _fail("FORBIDDEN", "기존 문서 준비 작업만 다시 요청할 수 있습니다.", 403)
        retry = getattr(application.rag, "retry_prepare", None)
        if not callable(retry):
            queue = getattr(application.rag, "document_jobs", None)
            retry = getattr(queue, "retry", None)
        if not callable(retry):
            _fail("JOBS_UNAVAILABLE", "문서 준비 처리기를 설정한 후 다시 요청해 주세요.", 503)
        return respond(request, retry(session.actor, _string(job_id, "작업 ID", 128)), status=202)

    @app.post("/trades")
    async def create_trade(request: Request):
        session = identity(request, True)
        body = await _body(request)
        allowed = {"buyer_id", "seller_id", "item_id", "D", "W", "terms"}
        if set(body) - allowed:
            _fail("INVALID_INPUT", "거래 생성 입력 항목을 확인해 주세요.")
        terms = body.get("terms", {})
        if not isinstance(terms, dict) or set(terms).intersection(allowed | _IDENTITY_FIELDS):
            _fail("INVALID_INPUT", "계약 조건은 객체로 입력해 주세요.")
        application.catalog.get(_string(body.get("item_id"), "항목 ID", 120))
        result = application.trades.create(session.actor, _string(body.get("buyer_id"), "구매자", 80),
                    _string(body.get("seller_id"), "판매자", 80), body["item_id"],
                    _integer(body.get("D"), "물건값 D"), _integer(body.get("W"), "보증 프리미엄 W"), **terms)
        return respond(request, result, status=201)

    @app.get("/trades/{trade_id}")
    async def trade(request: Request, trade_id: str):
        session = identity(request)
        return respond(request, application.trades.get(session.actor, trade_id))

    @app.post("/trades/{trade_id}/{route}")
    async def trade_action(request: Request, trade_id: str, route: str):
        session = identity(request, True)
        if route in {"evidence-files", "uploads"}:
            _fail("UPLOAD_DISABLED", "검사 경로가 준비되지 않아 실제 파일 업로드를 사용할 수 없습니다.", 403)
        action = _ACTIONS.get(route)
        if action is None:
            _fail("NOT_FOUND", "지원하지 않는 거래 작업입니다.", 404)
        body = await _body(request)
        key, version = key_and_version(request, body)
        return respond(request, application.trades.mutate(session.actor, trade_id, action, body, key, version))

    @app.get("/operations/{operation_id}")
    async def operation(request: Request, operation_id: str):
        session = identity(request)
        return respond(request, application.trades.get_operation(session.actor, operation_id))

    @app.post("/operations/{operation_id}/{action}")
    async def operation_action(request: Request, operation_id: str, action: str):
        session = identity(request, True)
        if action not in {"retry", "reconcile"}:
            _fail("NOT_FOUND", "지원하지 않는 작업입니다.", 404)
        body = await _body(request)
        key, version = key_and_version(request, body)
        if body:
            _fail("FORBIDDEN", "작업 결과와 금액은 서버 처리기가 확인합니다.", 403)
        op = application.trades.get_operation(session.actor, operation_id)
        return respond(request, application.trades.mutate(session.actor, op["trade_id"], action,
                       {"operation_id": operation_id}, key, version))

    @app.post("/worker/run")
    async def run_worker(request: Request):
        session = identity(request, True)
        if (session.actor != "admin" or not application.settings.demo_mode or not request.client
                or request.client.host not in {"127.0.0.1", "::1", "localhost"}):
            _fail("FORBIDDEN", "로컬 시연 관리자만 작업 처리기를 실행할 수 있습니다.", 403)
        if await _body(request):
            _fail("FORBIDDEN", "처리 결과는 서버가 확인합니다.", 403)
        return respond(request, {"processed": application.worker.run_pending()})

    web_dir = Path(__file__).resolve().parents[3] / "web"
    if web_dir.is_dir():
        @app.get("/assets/app.js", include_in_schema=False)
        async def script():
            return FileResponse(web_dir / "app.js", media_type="text/javascript")

        @app.get("/assets/style.css", include_in_schema=False)
        async def stylesheet():
            return FileResponse(web_dir / "style.css", media_type="text/css")

        app.mount("/assets", StaticFiles(directory=web_dir), name="assets")

        @app.get("/", include_in_schema=False)
        async def index():
            return FileResponse(web_dir / "index.html", media_type="text/html")

    return app
