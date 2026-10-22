"""Korean localhost demonstration UI over the shared application services.

Streamlit is imported only by ``run`` so controllers and rendering contracts remain
testable without the optional UI dependency. External content is rendered as text.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

NOTICE = "공식 조건 확인을 돕는 정보이며 제공기관의 확인이나 금융·법률 전문가의 판단을 대신하지 않습니다. 시연 금액은 실제 자금이 아닙니다."
CLASSIFICATIONS = {
    "TRANSFERABLE": "약관상 이전 가능",
    "NOT_TRANSFERABLE": "약관상 이전 불가",
    "CONDITIONAL": "조건부 · 조건 확인 필요",
    "NEEDS_INFORMATION": "정보 부족 · 확인 보류",
}
ERRORS = {
    "INDEX_EMPTY": "검색 색인이 없습니다. 문서 준비 상태를 확인해 주세요.",
    "KEY_MISSING": "모델 연결 설정이 없습니다. 서버 운영자가 설정을 확인해야 합니다.",
    "SOURCE_BLOCKED": "해당 출처의 이용 범위가 확인되지 않아 처리가 보류되었습니다.",
    "OWN_DOCUMENT_MISSING": "선택 항목의 공식 문서 본문이 필요합니다.",
    "DOC_BLOCKED": "선택 항목에 사용할 수 있는 공식 문서가 준비되지 않았습니다.",
    "FETCH_FAILED": "문서를 가져오지 못했습니다. 문서 준비 작업을 확인해 주세요.",
    "PARSE_FAILED": "문서 본문을 읽지 못했습니다. 문서 형식을 확인해야 합니다.",
    "NO_RESULTS": "확인할 수 있는 결과가 없습니다. 조건을 바꾸거나 필요한 자료를 확인해 주세요.",
    "APPLICABILITY_UNKNOWN": "가입일·지역·플랜 등 적용성 입력을 확인해 주세요.",
    "MODEL_OUTPUT_INVALID": "답변 형식을 검증하지 못했습니다. 다시 질문해 주세요.",
    "CONFLICT": "자료 또는 현재 상태가 서로 맞지 않습니다. 최신 상태와 누락 정보를 확인해 주세요.",
    "VERSION_CONFLICT": "다른 작업으로 상태가 바뀌었습니다. 화면을 새로고침해 주세요.",
    "FORBIDDEN": "현재 시연 사용자는 이 작업의 권한이 없습니다.",
    "NOT_FOUND": "현재 사용자가 조회할 수 있는 대상을 찾지 못했습니다.",
    "VALIDATION_ERROR": "입력값을 확인해 주세요. 승인과 정산 조건은 서버에서 검증합니다.",
    "NEEDS_INFORMATION": "자료 또는 확인이 부족합니다. 누락 정보를 먼저 확인해 주세요.",
    "INTERNAL_ERROR": "요청을 처리하지 못했습니다. 잠시 후 다시 확인해 주세요.",
}
for _code in ("INVALID_INPUT", "INVALID_MONEY", "INVALID_TERMS", "INVALID_RETURN_CONDITIONS",
              "INVALID_ALLOCATIONS", "ZERO_ALLOCATION", "INVALID_EVIDENCE", "INVALID_TRANSFER_STATUS",
              "INVALID_DISPUTE_SCOPE", "INVALID_RESOLUTION", "INVALID_CONDITION_CHECKS"):
    ERRORS[_code] = ERRORS["VALIDATION_ERROR"]
for _code in ("BUYER_REQUIRED", "PARTY_REQUIRED", "ASSIGNED_ADMIN_REQUIRED", "INVALID_PARTIES"):
    ERRORS[_code] = ERRORS["FORBIDDEN"]
for _code in ("TARGET_REVISION_MISMATCH", "APPROVAL_TARGET_MISMATCH", "EVIDENCE_STALE",
              "PROPOSAL_STALE", "RESOLUTION_STALE", "IDEMPOTENCY_CONFLICT", "TRADE_VERSION_CONFLICT"):
    ERRORS[_code] = ERRORS["VERSION_CONFLICT"]
ERRORS.update({
    "FUNDED_CONTRACT_REQUIRED": "현재 계약에 대한 양측 동의와 전액 시연 예치가 필요합니다.",
    "PROPOSAL_CONSENT_REQUIRED": "현재 정산안의 정확한 배분·revision·hash에 양측 동의가 필요합니다.",
    "CONTRACT_CONSENT_REQUIRED": "현재 계약 revision·hash에 양측 동의가 필요합니다.",
    "RECEIPT_REQUIRED": "현재 계약에 대한 구매자의 물건 수령 확인이 필요합니다.",
    "W_REVIEW_NOT_READY": "W 지급에 필요한 적용성·공식 문서·채택 검토를 확인해 주세요.",
    "W_CONDITIONS_NOT_CONFIRMED": "채택 검토의 조건이 아직 충족되지 않았습니다.",
    "W_REQUIRED_CONDITIONS_MISSING": "공식 문서 근거에 연결된 필수 조건 정의를 먼저 확인해야 합니다.",
    "W_EVIDENCE_NOT_READY": "현재 계약과 채택 검토에 연결된 최신 증빙 묶음이 필요합니다.",
    "W_BILATERAL_EVIDENCE_REQUIRED": "동일한 최신 증빙 묶음에 구매자와 판매자의 확인이 필요합니다.",
    "RETURN_CONDITION_REQUIRED": "계약에 기록된 반환 조건과 정산안의 반환 사유를 확인해 주세요.",
    "UNRESOLVED_DISPUTE": "관련 범위의 분쟁이 해결되지 않았습니다.",
    "SETTLEMENT_UNCERTAIN": "처리 중이거나 결과가 불명인 관련 작업을 먼저 대사해야 합니다.",
    "INSUFFICIENT_HELD": "정산안이 현재 보류 잔액을 초과합니다.",
    "INSUFFICIENT_FUNDS": "시연 지갑의 가용 잔액이 부족합니다.",
    "CLOSE_CONDITIONS_NOT_MET": "보류·예약·미확정 작업·미해결 분쟁을 확인한 뒤 종료해 주세요.",
    "UNFUNDED_CANNOT_CLOSE": "예치되지 않은 계약은 정산 완료로 종료할 수 없습니다.",
    "FUNDED_CANNOT_CANCEL": "이미 예치된 거래입니다. 계약 조건에 따라 정산안을 합의해 주세요.",
    "MAX_ATTEMPTS_REACHED": "이 작업은 재시도 한도에 도달했습니다.",
})
APPLICABILITY_LABELS = {
    "country": "가입 국가·지역",
    "enrollment_date": "가입일 (YYYY-MM-DD)",
    "plan_generation": "플랜·약관 세대",
    "payment_method": "결제 방식",
    "channel": "가입 채널",
    "document_type": "적용 문서 종류",
}


def conversation_key(actor: str, item_id: str) -> str:
    """Unambiguous user/item boundary, including IDs containing delimiters."""
    return "conversation:" + json.dumps([actor, item_id], ensure_ascii=False)


def applicability_inputs(values: dict[str, Any]) -> dict[str, str]:
    """Omit unknown values instead of inventing country, date or generation."""
    return {key: str(values[key]).strip() for key in APPLICABILITY_LABELS
            if values.get(key) is not None and str(values[key]).strip()}


def public_error(error: Any) -> dict[str, Any]:
    """Never reflect an exception message, paths, prompts or model output."""
    code = error.get("code") if isinstance(error, dict) else getattr(error, "code", None)
    retryable = error.get("retryable", False) if isinstance(error, dict) else getattr(error, "retryable", False)
    code = code if code in ERRORS else "INTERNAL_ERROR"
    return {"code": code, "message": ERRORS[code], "retryable": bool(retryable)}


def source_url(value: Any) -> str | None:
    """Only server supplied HTTPS links; no executable, local or embedded URL."""
    if not isinstance(value, str) or len(value) > 2048 or any(ch.isspace() for ch in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            return None
        if parsed.port not in (None, 443):
            return None
    except ValueError:
        return None
    return value


def cited_evidence(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Render the intersection of actual citations and the service evidence map."""
    evidence = result.get("evidence", [])
    if isinstance(evidence, dict):
        evidence = [dict(value, evidence_id=key) for key, value in evidence.items()
                    if isinstance(value, dict)]
    by_id = {entry.get("evidence_id"): entry for entry in evidence if isinstance(entry, dict)}
    seen = set()
    rows = []
    for evidence_id in result.get("cited_evidence_ids", []):
        if isinstance(evidence_id, str) and evidence_id in by_id and evidence_id not in seen:
            seen.add(evidence_id)
            rows.append(by_id[evidence_id])
    return rows


def mutation_key(state: Any, actor: str, trade_id: str, action: str,
                 payload: dict[str, Any], version: int) -> str:
    """Keep the request key for retransmission of the same displayed intent."""
    encoded = json.dumps([actor, trade_id, action, payload, version], sort_keys=True,
                         ensure_ascii=False, separators=(",", ":"))
    fingerprint = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    keys = state.setdefault("warranty_mutation_keys", {})
    if fingerprint not in keys:
        keys[fingerprint] = str(uuid4())
    return keys[fingerprint]


def _error(st: Any, error: Any) -> None:
    safe = public_error(error)
    st.error(f"{safe['code']} · {safe['message']}")
    st.caption("재시도 가능" if safe["retryable"] else "필요한 입력·권한·현재 상태를 확인해 주세요.")


def _call(st: Any, callback: Any) -> tuple[bool, Any]:
    try:
        return True, callback()
    except Exception as exc:
        _error(st, exc)
        return False, None


def _text_list(st: Any, values: Any, empty: str | None = None) -> None:
    if not values and empty:
        st.caption(empty)
    for value in values or []:
        if isinstance(value, dict):
            if "text" in value:
                st.text(str(value["text"]))
            else:
                fields = ("actor", "actor_id", "user_id", "revision", "hash", "status")
                st.text(" · ".join(f"{key}: {value[key]}" for key in fields if key in value))
        else:
            st.text(str(value))


def _evidence(st: Any, result: dict[str, Any]) -> None:
    rows = cited_evidence(result)
    if not rows:
        st.info("확인 가능한 근거를 찾지 못했습니다.")
        return
    for number, row in enumerate(rows, 1):
        with st.container(border=True):
            st.text(f"[{number}] {row.get('title', '제목 미확인')}")
            source_type = {"metadata": "메타데이터", "official_document": "공식 문서",
                           "synthetic_document": "합성 시연 문서"}.get(row.get("source_type"), "종류 미확인")
            st.text(f"유형: {source_type} · 근거 ID: {row.get('evidence_id', '')}")
            positions = []
            if row.get("page") is not None:
                positions.append(f"PDF {row['page']}쪽")
            if row.get("section"):
                positions.append(f"HTML 섹션: {row['section']}")
            if row.get("version_id"):
                positions.append(f"문서 버전: {row['version_id']}")
            if positions:
                st.text(" · ".join(positions))
            url = source_url(row.get("url"))
            if url:
                st.link_button("원문 출처 열기", url)
            else:
                st.caption("표시할 수 있는 HTTPS 출처 링크가 없습니다.")


def _review(st: Any, result: dict[str, Any], show_evidence: bool = True) -> None:
    st.info(CLASSIFICATIONS.get(result.get("classification"), "분류 미확인"))
    st.caption("약관과 자료에 대한 분류입니다. 실제 보증 이전 완료나 정산 승인을 의미하지 않습니다.")
    if str(result.get("model", "")).startswith("MOCK"):
        st.warning("MOCK · 결정적 모의 응답입니다. 실제 LLM 검증 결과가 아닙니다.")
    st.text(str(result.get("answer", "아래 조건과 미확인 정보를 확인해 주세요.")))
    for label, key in (("확인이 필요한 조건", "conditions"), ("추가로 필요한 정보", "missing_information"),
                       ("서로 맞지 않는 정보", "conflicts")):
        if result.get(key):
            with st.expander(label, expanded=key != "conditions"):
                _text_list(st, result[key])
                if key == "conditions":
                    for condition in result[key]:
                        if isinstance(condition, dict) and condition.get("evidence_ids"):
                            if condition.get("condition_id"):
                                st.text("조건 ID: " + str(condition["condition_id"]))
                            st.text("조건의 근거 ID: " + ", ".join(map(str, condition["evidence_ids"])))
    readiness = result.get("readiness") or {}
    if readiness.get("status") == "DOC_BLOCKED":
        st.warning("DOC_BLOCKED · 선택 항목의 사용 가능한 공식 문서가 부족합니다.")
    for error in result.get("errors", []):
        _error(st, error)
    if result.get("progress"):
        with st.expander("이번 응답의 처리 단계"):
            for step in result["progress"]:
                if isinstance(step, dict):
                    st.text(f"{step.get('stage', '')} · {step.get('status', '')}")
    if show_evidence:
        st.caption("실제 인용한 근거")
        _evidence(st, result)


def _catalog(st: Any, app: Any) -> str | None:
    summary = app.catalog.summary()
    corpus = None
    corpus_summary = getattr(app.rag, "corpus_summary", None)
    if callable(corpus_summary):
        ok, value = _call(st, corpus_summary)
        if ok:
            corpus = value
    counts = st.columns(3)
    counts[0].metric("실제 메타 항목", summary.get("real_item_count", 0))
    counts[1].metric("합성 시연 항목", summary.get("synthetic_item_count", 0))
    counts[2].metric("현재 공식 문서 본문" if corpus is not None else "전달 자료의 공식 본문",
                     (corpus if corpus is not None else summary).get("official_document_body_count", 0))
    if corpus is not None:
        st.text(f"현재 색인 · 공식 청크 {corpus.get('official_document_chunk_count', 0)}개 · 합성 시연 본문 {corpus.get('synthetic_document_body_count', 0)}개 · 합성 청크 {corpus.get('synthetic_document_chunk_count', 0)}개")
    st.caption("항목 수는 판매 중인 매물 수가 아닙니다. 미확인 요금과 이전 여부는 확인값으로 취급하지 않습니다. 항목별 문서 준비 상태도 함께 확인하세요.")
    with st.form("catalog_search"):
        query = st.text_input("원하는 조건", placeholder="예: SKT, 월 5천원 이하, 양도 가능한 항목", max_chars=500)
        submitted = st.form_submit_button("조건 탐색", type="primary")
    if submitted or "warranty_search" not in st.session_state:
        ok, result = _call(st, lambda: app.catalog.search(query=query))
        if ok:
            st.session_state["warranty_search"] = result
    result = st.session_state.get("warranty_search", {"matches": [], "unknown": []})
    matches, unknown = result.get("matches", []), result.get("unknown", [])
    for condition in result.get("unsupported_conditions", []):
        st.text(f"자동 확인하지 않은 조건: {condition}")
    st.subheader("확인된 조건에 맞는 항목")
    if not matches:
        st.info("확인된 조건에 맞는 항목이 0개입니다. 미확인 후보를 따로 확인해 주세요.")
    _cards(st, matches, "match")
    if unknown:
        with st.expander(f"조건 확인이 필요한 후보 · {len(unknown)}개", expanded=not matches):
            st.caption("해당 조건의 충족 여부를 확인할 수 없습니다. 적합 항목으로 추천하지 않습니다.")
            _cards(st, unknown, "unknown")
    items = app.catalog.items
    if not items:
        return None
    item_ids = [item["item_id"] for item in items]
    names = {item["item_id"]: item.get("name", item["item_id"]) for item in items}
    if st.session_state.get("warranty_selected_item") not in item_ids:
        st.session_state["warranty_selected_item"] = item_ids[0]
    return st.selectbox("상세·채팅할 항목", item_ids, key="warranty_selected_item",
                        format_func=lambda item_id: names[item_id])


def _cards(st: Any, items: list[dict[str, Any]], prefix: str) -> None:
    # Bounded rendering keeps the initial screen usable for a 104-row catalog.
    for item in items[:12]:
        with st.container(border=True):
            left, right = st.columns([4, 1])
            left.text(item.get("name", item["item_id"]))
            fee = item.get("monthly_fee_won")
            label = f"월 {fee:,}원" if type(fee) is int else "월요금 미확인"
            left.text(f"{item.get('provider', '제공사 미확인')} · {label}")
            if item.get("monthly_fee_basis"):
                left.text(f"요금 기준: {item['monthly_fee_basis']}")
            left.caption("SYNTHETIC · 합성 시연" if item.get("is_synthetic") else "REAL METADATA · 실제 입력 메타데이터")
            if item.get("unknown_conditions"):
                left.text("확인 필요: " + ", ".join(map(str, item["unknown_conditions"])))
            if right.button("선택", key=f"{prefix}:{item['item_id']}"):
                st.session_state["warranty_selected_item"] = item["item_id"]
                st.rerun()
    if len(items) > 12:
        st.caption(f"처음 12개를 표시했습니다. 전체 {len(items)}개 중 다른 항목은 아래 선택창에서 선택할 수 있습니다.")


def _details(st: Any, app: Any, item: dict[str, Any]) -> None:
    st.subheader("선택한 보증 항목")
    st.text(item.get("name", item["item_id"]))
    st.text(f"항목 ID: {item['item_id']} · 제공사: {item.get('provider', '미확인')}")
    st.caption("SYNTHETIC · 합성 데이터입니다." if item.get("is_synthetic") else "REAL METADATA · 전달된 실제 메타데이터입니다.")
    fee = item.get("monthly_fee_won")
    transfer = item.get("transfer_allowed")
    st.text(f"월요금: {format(fee, ',') + '원' if type(fee) is int else '미확인'}")
    st.text(f"요금 기준: {item.get('monthly_fee_basis') or '미확인'}")
    st.text("이전 여부: " + ({True: "메타데이터상 가능", False: "메타데이터상 불가"}.get(transfer, "미확인")))
    st.text(f"판매 상태: {item.get('sale_status', 'NOT_VERIFIED')}")
    readiness = item.get("readiness") or {}
    if readiness.get("documents") == "DOC_BLOCKED" or readiness.get("status") == "DOC_BLOCKED":
        st.warning("DOC_BLOCKED · 이 항목의 공식 문서 본문이 준비되지 않았습니다. 메타데이터만으로 보증 이전을 확정하지 않습니다.")
    url = source_url(item.get("source_url"))
    if url:
        st.link_button("메타데이터 원문 출처", url)


def _applicability(st: Any, actor: str, item_id: str) -> dict[str, str]:
    prefix = conversation_key(actor, item_id)
    with st.expander("내 가입 조건 · 적용성 입력", expanded=True):
        st.caption("확실한 값만 입력하세요. 미입력은 미확인으로 남으며 최신 약관을 임의로 적용하지 않습니다.")
        columns = st.columns(2)
        values = {key: columns[index % 2].text_input(label, key=f"{prefix}:{key}", max_chars=120)
                  for index, (key, label) in enumerate(APPLICABILITY_LABELS.items())}
    return applicability_inputs(values)


def _job(st: Any, job: dict[str, Any]) -> None:
    st.text(f"작업: {job.get('job_id', '')}")
    st.text(f"현재 단계: {job.get('stage', '미확인')} · 상태: {job.get('status', '미확인')}")
    st.text(f"후보 {job.get('candidate_count', 0)} · 본문 {job.get('body_count', 0)} · 청크 {job.get('chunk_count', 0)}")
    if job.get("failure_code"):
        _error(st, {"code": job["failure_code"]})


def _rag(st: Any, app: Any, actor: str, item: dict[str, Any], applicability: dict[str, str]) -> None:
    item_id = item["item_id"]
    prefix = conversation_key(actor, item_id)
    with st.expander("문서 준비·작업 진행"):
        st.caption("서버에 등록되고 이용 범위가 승인된 문서만 처리합니다. 임의 URL 입력과 실제 파일 업로드는 비활성입니다.")
        if st.button("문서 준비 요청", key=f"prepare:{prefix}"):
            with st.spinner("등록 문서와 처리 권한을 확인하는 중…"):
                ok, job = _call(st, lambda: app.rag.prepare(actor, item_id))
            if ok:
                st.session_state[f"job:{prefix}"] = job
        job = st.session_state.get(f"job:{prefix}")
        if job:
            if st.button("작업 상태 새로고침", key=f"job_refresh:{prefix}"):
                ok, latest = _call(st, lambda: app.rag.job(actor, job["job_id"]))
                if ok:
                    job = latest
                    st.session_state[f"job:{prefix}"] = job
            _job(st, job)
    chat_tab, checklist_tab = st.tabs(["항목별 질문", "보증 이전 조건표·체크리스트"])
    with chat_tab:
        st.caption("선택 항목과 현재 시연 사용자별로 대화를 보관합니다. 질문은 승인이나 지급을 실행하지 않습니다.")
        ok, history = _call(st, lambda: app.rag.history(actor, item_id))
        if ok:
            if history:
                st.session_state[f"latest_review:{prefix}"] = history[-1].get("result") or {}
            for message in history:
                with st.chat_message("user"):
                    st.text(str(message.get("question", "")))
                with st.chat_message("assistant"):
                    _review(st, message.get("result") or {})
        with st.form(f"ask:{prefix}", clear_on_submit=True):
            question = st.text_area("이 항목에 질문하기", placeholder="예: 가입일에 따라 적용되는 이전 조건이 달라지나요?", max_chars=2000)
            top_k = st.select_slider("외부 문서 검색 수", options=[3, 6, 10], value=6)
            submitted = st.form_submit_button("근거와 함께 질문", type="primary")
        if submitted:
            if not question.strip():
                st.warning("질문을 입력해 주세요.")
            else:
                with st.spinner("선택 항목의 근거를 검색하고 답변을 생성하는 중…"):
                    ok, result = _call(st, lambda: app.rag.ask(actor, item_id, question.strip(),
                                                              applicability=applicability, top_k=top_k))
                if ok:
                    st.session_state[f"latest_review:{prefix}"] = result
                    st.rerun()
    with checklist_tab:
        ok, result = _call(st, lambda: app.rag.checklist(item_id, applicability=applicability, user_id=actor))
        if ok:
            _review(st, result)
            st.caption("거래에 채택하면 서버가 해당 검토와 문서 버전의 불변 스냅샷을 만듭니다. 채팅을 추가하는 것과 구분됩니다.")


def _mutate(st: Any, app: Any, actor: str, trade: dict[str, Any], action: str,
            payload: dict[str, Any]) -> None:
    trade_id, version = trade["trade_id"], trade["version"]
    key = mutation_key(st.session_state, actor, trade_id, action, payload, version)
    ok, _ = _call(st, lambda: app.trades.mutate(actor, trade_id, action, payload, key, version))
    if ok:
        st.session_state["warranty_flash"] = "서버가 요청을 처리했습니다. 최신 상태를 확인하세요."
        st.rerun()


def _action(st: Any, app: Any, actor: str, trade: dict[str, Any], action: str,
            label: str, payload: dict[str, Any], suffix: str = "") -> None:
    if st.button(label, key=f"trade:{trade['trade_id']}:{action}:{suffix}"):
        _mutate(st, app, actor, trade, action, payload)


def _trade(st: Any, app: Any, actor: str, item_id: str) -> None:
    with st.expander("D/W 분리 거래 시연", expanded=False):
        st.warning("MOCK · 합성 구매자·판매자·관리자와 시연 잔액을 사용합니다. 실제 송금이나 보증 명의 이전을 수행하지 않습니다.")
        trade_key = f"trade_selection:{item_id}"
        with st.form(f"create_trade:{item_id}"):
            st.caption("D는 물건 대금, W는 보증 프리미엄입니다. 반환 조건과 승인 기준은 서버 계약에 기록됩니다.")
            columns = st.columns(2)
            d = columns[0].number_input("물건 대금 D (시연 원)", min_value=1, value=800000, step=1000)
            w = columns[1].number_input("보증 프리미엄 W (시연 원)", min_value=0, value=80000, step=1000)
            create = st.form_submit_button("선택 항목의 새 시연 계약 만들기")
        if create:
            ok, trade = _call(st, lambda: app.trades.create(actor, "buyer", "seller", item_id, int(d), int(w)))
            if ok:
                st.session_state[trade_key] = trade["trade_id"]
                st.rerun()
        trade_id = st.text_input("기존 시연 거래 ID", key=trade_key, max_chars=120)
        if not trade_id:
            st.caption("시연 계약을 만들거나, 이 항목의 거래 ID를 입력하면 양측 승인과 D/W 현황을 확인할 수 있습니다.")
            return
        ok, trade = _call(st, lambda: app.trades.get(actor, trade_id))
        if not ok:
            return
        if trade.get("item_id") != item_id:
            st.warning("이 거래는 현재 선택 항목과 다릅니다. 해당 항목을 선택한 뒤 조회해 주세요.")
            return
        st.text(f"거래 ID: {trade['trade_id']} · 버전: {trade.get('version')} · 상태: {trade.get('status', trade.get('lifecycle', '미확인'))}")
        st.caption("PARTIES_CONFIRMED의 의미는 ‘당사자 증빙 확인’입니다. 제공기관이 실제 이전을 완료했다는 확인이 아닙니다.")
        receipt = trade.get("receipt") or {}
        st.text("현재 계약의 물건 수령: " + ("구매자 확인됨" if receipt.get("received") else "확인 필요"))
        transfer_labels = {"NOT_STARTED": "미시작", "IN_PROGRESS": "진행 중", "FAILED": "실패 기록",
                           "ON_HOLD": "보류", "PARTIES_CONFIRMED": "당사자 증빙 확인"}
        st.text("이전 절차: " + transfer_labels.get(trade.get("transfer_status"), "미확인"))
        contract = trade.get("contract") or {}
        st.text(f"현재 계약 revision: {contract.get('revision', '미확인')} · hash: {contract.get('hash', '미확인')}")
        st.text(f"D {contract.get('D', trade.get('D', d)):,}원 · W {contract.get('W', trade.get('W', w)):,}원 (시연)")
        if contract.get("terms"):
            with st.expander("현재 계약의 조건"):
                terms = contract["terms"]
                if isinstance(terms, dict):
                    for field in ("return_conditions", "evidence_standard", "confirmation_deadline", "dispute_rule"):
                        if field in terms:
                            st.text(f"{field}: {terms[field]}")
        if trade.get("balances"):
            st.caption("D/W 보류·예약·확정 현황 (시연 원)")
            rows = []
            balances = trade["balances"]
            if isinstance(balances, dict):
                for component, balance in balances.items():
                    if isinstance(balance, dict):
                        rows.append({"구성": component, **{key: balance.get(key, 0) for key in
                                     ("deposited", "held", "pay_reserved", "refund_reserved", "paid", "refunded")}})
            if rows:
                st.dataframe(rows, hide_index=True, width="stretch")
        if trade.get("review"):
            with st.expander("계약에 채택된 검토 스냅샷"):
                adopted = trade["review"]
                st.text(f"검토 ID: {adopted.get('review_id')} · revision: {adopted.get('revision', '미확인')}")
                st.info(CLASSIFICATIONS.get(adopted.get("classification"), "분류 미확인"))
                st.text("적용성 확인: " + ("확인됨" if adopted.get("applicability_ok") else "미확인"))
                st.text("검토 조건 충족: " + ("확인됨" if adopted.get("conditions_ok") else "미확인"))
                for field, label in (("document_ids", "문서 ID"), ("document_version_ids", "문서 버전 ID"),
                                     ("document_hashes", "문서 해시"), ("evidence_ids", "근거 ID"),
                                     ("required_condition_ids", "충족 확인이 필요한 조건 ID")):
                    if adopted.get(field):
                        st.text(label + ": " + ", ".join(map(str, adopted[field])))
                st.caption("채택 당시의 검토 식별자와 확인 상태입니다. 원문 표시 권한은 현재 근거 조회에서 다시 확인하며, 이 분류만으로 실제 이전이나 지급이 승인되지 않습니다.")
        approval_tab, settlement_tab, dispute_tab = st.tabs(["계약·수령·증빙", "정산안·실행", "분쟁·종료"])
        with approval_tab:
            st.caption("각 사용자는 자신의 동의만 기록합니다. 아래 요청도 동일한 서버 권한·버전 검증을 거칩니다.")
            _text_list(st, trade.get("contract_consents"))
            if actor in ("buyer", "seller"):
                exact_contract = {"revision": contract.get("revision"), "hash": contract.get("hash")}
                _action(st, app, actor, trade, "contract_consent", "현재 계약 revision/hash에 동의", exact_contract)
            _action(st, app, actor, trade, "deposit", "D+W 전액 시연 예치 요청", {})
            _action(st, app, actor, trade, "receipt", "구매자 · 현재 계약의 물건 수령 확인", {"contract_revision": contract.get("revision")})
            latest = st.session_state.get(f"latest_review:{conversation_key(actor, item_id)}") or {}
            review_id = latest.get("review_id")
            if review_id:
                st.text(f"채택할 최근 검토: {review_id}")
                st.caption("검토 채택 시 새 계약 revision을 만듭니다. 양측 계약 동의와 구매자 수령 확인을 새 revision에서 다시 기록해야 합니다.")
                _action(st, app, actor, trade, "adopt_review", "이 검토를 계약에 채택 요청", {"review_id": review_id})
            st.caption("실제 증빙 업로드는 비활성입니다. 다음 버튼은 합성 증빙 참조만 만듭니다.")
            required_conditions = (trade.get("review") or {}).get("required_condition_ids") or []
            condition_checks = []
            if required_conditions:
                st.caption("각 조건 ID의 내용을 현재 체크리스트와 근거에서 확인한 뒤, 합성 시연에서 실제 충족을 확인한 경우에만 체크하세요. 모델이 조건을 찾았다는 사실만으로 충족되지 않습니다.")
                st.caption("확인 내용을 바꾸려면 새 증빙 묶음을 만들고, 구매자·판매자가 같은 최신 묶음을 다시 확인해야 합니다.")
            for condition_id in required_conditions:
                confirmed = st.checkbox(f"조건 {condition_id}의 충족을 직접 확인했습니다 (합성 시연)", value=False,
                                        key=f"condition:{actor}:{trade_id}:{(trade.get('review') or {}).get('review_id')}:{condition_id}")
                condition_checks.append({"condition_id": condition_id, "confirmed": confirmed})
            _action(st, app, actor, trade, "evidence", "합성 증빙 묶음 생성",
                    {"references": ["SYNTHETIC:transfer-demo"], "condition_checks": condition_checks})
            evidence = trade.get("evidence") or {}
            if isinstance(evidence, list):
                evidence = evidence[-1] if evidence else {}
            if evidence:
                st.text(f"최신 합성 증빙: {evidence.get('evidence_id')} · hash: {evidence.get('hash')}")
                latest_checks = (evidence.get("content") or {}).get("condition_checks") or []
                for check in latest_checks:
                    st.text(f"조건 {check.get('condition_id')}: " + ("직접 충족 확인 기록" if check.get("confirmed") else "미충족·미확인 기록"))
                _text_list(st, trade.get("evidence_confirmations"))
                if actor in ("buyer", "seller"):
                    _action(st, app, actor, trade, "confirm_evidence", "현재 증빙 묶음/hash를 확인", {"evidence_id": evidence.get("evidence_id"), "hash": evidence.get("hash")})
            with st.form(f"transfer_status:{trade_id}"):
                transfer_status = st.selectbox("수동 이전 절차 기록", ["IN_PROGRESS", "FAILED", "ON_HOLD"])
                if st.form_submit_button("절차 상태 기록 요청"):
                    _mutate(st, app, actor, trade, "transfer_event", {"status": transfer_status})
        with settlement_tab:
            with st.form(f"proposal:{trade_id}"):
                st.caption("양수인 방향만 정산안에 포함합니다. D 지급과 W 지급·환불의 필수 조건은 각각 서버가 판단합니다.")
                columns = st.columns(2)
                d_pay = columns[0].number_input("D 지급액", min_value=0, value=0, step=1000)
                d_refund = columns[1].number_input("D 환불액", min_value=0, value=0, step=1000)
                w_pay = columns[0].number_input("W 지급액", min_value=0, value=0, step=1000)
                w_refund = columns[1].number_input("W 환불액", min_value=0, value=0, step=1000)
                return_conditions = (contract.get("terms") or {}).get("return_conditions") or []
                refund_reason = st.selectbox("계약에 기록된 반환 사유", return_conditions,
                                             index=0 if return_conditions else None)
                if st.form_submit_button("새 정산안 제출"):
                    allocations = [{"component": component, "pay_won": int(pay), "refund_won": int(refund)}
                                   for component, pay, refund in (("D", d_pay, d_refund), ("W", w_pay, w_refund))
                                   if pay or refund]
                    payload = {"allocations": allocations}
                    if refund_reason is not None:
                        payload["refund_reason"] = refund_reason
                    _mutate(st, app, actor, trade, "proposal", payload)
            proposals = trade.get("proposals") or []
            if proposals:
                selected = st.selectbox("검토할 정산안", list(range(len(proposals))),
                                        format_func=lambda index: str(proposals[index].get("proposal_id")))
                proposal = proposals[selected]
                st.text(f"revision: {proposal.get('revision')} · hash: {proposal.get('hash')}")
                st.dataframe(proposal.get("allocations", []), hide_index=True, width="stretch")
                _text_list(st, proposal.get("consents"))
                exact = {key: proposal.get(key) for key in ("proposal_id", "revision", "hash")}
                if actor in ("buyer", "seller"):
                    _action(st, app, actor, trade, "proposal_consent", "표시된 배분/revision/hash에 동의", exact)
                _action(st, app, actor, trade, "reserve", "현재 정산안 예약 요청", {"proposal_id": proposal.get("proposal_id"), "revision": proposal.get("revision")})
            operations = trade.get("operations") or []
            if operations:
                st.caption("작업별 상태 · 일부 완료는 전체 완료와 다릅니다.")
                for group in trade.get("groups") or []:
                    status = "일부 완료" if group.get("status") == "PARTIAL" else group.get("status", "미확인")
                    st.text(f"정산 그룹 {group.get('group_id', '')}: {status}")
                st.dataframe([{key: operation.get(key) for key in ("operation_id", "component", "direction", "amount", "status", "attempt_no")}
                              for operation in operations], hide_index=True, width="stretch")
                op_index = st.selectbox("조회·복구할 작업", list(range(len(operations))),
                                        format_func=lambda index: str(operations[index].get("operation_id")))
                operation_id = operations[op_index].get("operation_id")
                _action(st, app, actor, trade, "retry", "확정 실패 작업 재시도 요청", {"operation_id": operation_id})
                _action(st, app, actor, trade, "reconcile", "기존 작업 결과 대사 요청", {"operation_id": operation_id})
                st.caption("UNKNOWN은 예약을 유지하며 기존 결과를 대사합니다. 성공·실패 결과를 사용자가 지정할 수 없습니다.")
            if actor == "admin":
                st.caption("배정 관리자 · 서버 처리기: 이미 승인·예약된 시연 작업만 처리합니다.")
                if st.button("예약된 로컬 시연 정산 처리", key=f"worker:{trade_id}"):
                    ok, _ = _call(st, lambda: app.worker.run_pending())
                    if ok:
                        st.rerun()
            else:
                st.caption("예약된 시연 작업의 서버 처리기는 관리자 역할에서 실행할 수 있습니다.")
        with dispute_tab:
            with st.form(f"dispute:{trade_id}"):
                scope = st.selectbox("분쟁 범위", ["D", "W", "BOTH"])
                if st.form_submit_button("분쟁 등록 요청"):
                    _mutate(st, app, actor, trade, "dispute", {"scope": scope})
            disputes = trade.get("disputes") or []
            for dispute in disputes:
                st.text(f"분쟁 {dispute.get('dispute_id')} · {dispute.get('scope')} · {dispute.get('status')}")
            if actor == "admin" and disputes:
                with st.form(f"resolution:{trade_id}"):
                    dispute_index = st.selectbox("해결안을 제안할 분쟁", list(range(len(disputes))),
                                                  format_func=lambda index: str(disputes[index].get("dispute_id")))
                    terms = st.text_area("당사자가 확인할 해결안", max_chars=2000)
                    if st.form_submit_button("관리자 해결안 제안"):
                        _mutate(st, app, actor, trade, "resolution", {"dispute_id": disputes[dispute_index].get("dispute_id"), "terms": terms})
            for resolution in trade.get("resolutions") or []:
                st.text(f"해결안 {resolution.get('resolution_id')} · revision {resolution.get('revision')} · hash {resolution.get('hash')}")
                st.text(str(resolution.get("content", {}).get("terms", resolution.get("terms", ""))))
                if actor in ("buyer", "seller"):
                    _action(st, app, actor, trade, "resolution_consent", "이 해결안 revision/hash에 동의",
                            {key: resolution.get(key) for key in ("resolution_id", "revision", "hash")}, str(resolution.get("resolution_id")))
            st.caption("관리자 제안만으로 지급하거나 승인을 되살리지 않습니다. 종료·취소 가능 여부도 서버가 검증합니다.")
            _action(st, app, actor, trade, "close", "정산 완료 조건 확인 후 거래 종료 요청", {})
            if actor in ("buyer", "seller"):
                _action(st, app, actor, trade, "cancel_consent", "예치 전 현재 계약 취소에 동의", {"revision": contract.get("revision"), "hash": contract.get("hash")})
        st.button("거래 상태 새로고침", key=f"refresh:{trade_id}", on_click=lambda: None)


def run(application: Any = None, st: Any = None) -> None:
    """Run using an injected facade or the same factory as the API and CLI."""
    if st is None:
        import streamlit as st
    st.set_page_config(page_title="보증이음 · 조건부터 확인하는 거래", page_icon="🔎", layout="wide")
    st.title("보증이음")
    st.caption("조건과 근거를 확인하고, 물건 대금과 보증 프리미엄을 나누어 합의하세요.")
    if application is None:
        from warranty.application import build_application
        ok, application = _call(st, lambda: st.cache_resource(show_spinner=False)(build_application)())
        if not ok:
            return
    app = application
    address = st.get_option("server.address")
    if address not in {"localhost", "127.0.0.1", "::1"} or not app.settings.demo_mode:
        st.error("이 합성 사용자 시연 화면은 DEMO_MODE의 localhost 실행에서만 사용할 수 있습니다.")
        st.stop()
        return
    with st.sidebar:
        st.subheader("로컬 시연 세션")
        st.caption("실제 인증 서비스가 아닙니다. 합성 역할 전환은 이 로컬 시연에서만 제공합니다.")
        actor = st.selectbox("현재 시연 사용자", ["buyer", "seller", "admin"],
                             format_func=lambda value: {"buyer": "구매자", "seller": "판매자", "admin": "배정 관리자"}[value],
                             key="warranty_demo_actor")
        st.info("관리자는 당사자의 동의나 지급 승인을 대신할 수 없습니다.")
        st.caption(NOTICE)
    if st.session_state.pop("warranty_flash", None):
        st.success("서버가 요청을 처리했습니다. 최신 상태를 확인하세요.")
    st.info(NOTICE)
    try:
        selected = _catalog(st, app)
        if selected is None:
            return
        ok, item = _call(st, lambda: app.catalog.get(selected))
        if not ok:
            return
        st.divider()
        _details(st, app, item)
        applicability = _applicability(st, actor, selected)
        _rag(st, app, actor, item, applicability)
        _trade(st, app, actor, selected)
    except Exception as exc:
        _error(st, exc)


if __name__ == "__main__":
    run()
