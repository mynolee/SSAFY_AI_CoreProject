"""Korean localhost demonstration UI over the shared application services.

Streamlit is imported only by ``run`` so controllers and rendering contracts remain
testable without the optional UI dependency. External content is rendered as text.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
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
    "MODEL_REMOTE_BLOCKED": "실모델 사용이나 외부 처리에 필요한 허용 범위를 서버에서 확인해 주세요.",
    "MODEL_UNAVAILABLE": "모델 응답을 받지 못했습니다. 서버의 연결 설정과 제공기관 상태를 확인해 주세요.",
    "PROMPT_MISSING": "서버의 답변 설정이 준비되지 않았습니다. 운영자가 설정을 확인해야 합니다.",
    "JOB_QUEUE_UNAVAILABLE": "문서 준비 작업을 접수할 수 없습니다. 서버 처리기 상태를 확인해 주세요.",
    "JOB_NOT_FOUND": "현재 사용자가 조회할 수 있는 문서 작업을 찾지 못했습니다.",
    "INVALID_RESPONSE": "서버 응답을 확인할 수 없습니다. 잠시 후 다시 조회해 주세요.",
    "CONFLICT": "자료 또는 현재 상태가 서로 맞지 않습니다. 최신 상태와 누락 정보를 확인해 주세요.",
    "VERSION_CONFLICT": "다른 작업으로 상태가 바뀌었습니다. 화면을 새로고침해 주세요.",
    "FORBIDDEN": "현재 시연 사용자는 이 작업의 권한이 없습니다.",
    "NOT_FOUND": "현재 사용자가 조회할 수 있는 대상을 찾지 못했습니다.",
    "VALIDATION_ERROR": "입력값을 확인해 주세요. 승인과 정산 조건은 서버에서 검증합니다.",
    "NEEDS_INFORMATION": "자료 또는 확인이 부족합니다. 누락 정보를 먼저 확인해 주세요.",
    "INTERNAL_ERROR": "요청을 처리하지 못했습니다. 잠시 후 다시 확인해 주세요.",
}
for _code in ("INVALID_INPUT", "INVALID_MONEY", "INVALID_AMOUNT", "INVALID_ACTION_PAYLOAD",
              "MUTATION_PRECONDITIONS_REQUIRED", "INVALID_TERMS", "INVALID_RETURN_CONDITIONS",
              "INVALID_ALLOCATIONS", "ZERO_ALLOCATION", "INVALID_EVIDENCE", "INVALID_TRANSFER_STATUS",
              "INVALID_DISPUTE_SCOPE", "INVALID_RESOLUTION", "INVALID_CONDITION_CHECKS"):
    ERRORS[_code] = ERRORS["VALIDATION_ERROR"]
for _code in ("BUYER_REQUIRED", "PARTY_REQUIRED", "ASSIGNED_ADMIN_REQUIRED", "INVALID_PARTIES"):
    ERRORS[_code] = ERRORS["FORBIDDEN"]
for _code in ("TARGET_REVISION_MISMATCH", "APPROVAL_TARGET_MISMATCH", "EVIDENCE_STALE",
              "PROPOSAL_STALE", "RESOLUTION_STALE", "IDEMPOTENCY_CONFLICT", "TRADE_VERSION_CONFLICT"):
    ERRORS[_code] = ERRORS["VERSION_CONFLICT"]
ERRORS.update({
    "FUNDED_CONTRACT_IMMUTABLE": "예치된 계약의 D/W 금액은 변경할 수 없습니다. 현재 금액을 유지해 주세요.",
    "ACTIVE_SETTLEMENT": "예약되었거나 결과를 확인 중인 정산 작업을 먼저 처리해 주세요.",
    "TRADE_TERMINAL": "이미 종료되거나 취소된 거래입니다. 새 거래를 이용해 주세요.",
    "INSUFFICIENT_WALLET": "시연 지갑의 가용 잔액이 부족합니다.",
    "FUNDED_CURRENT_CONTRACT_REQUIRED": "현재 계약에 대한 양측 동의와 전액 시연 예치를 먼저 확인해 주세요.",
    "W_CURRENT_REVIEW_REQUIRED": "채택 검토의 문서 버전·이용 범위가 변경되었습니다. 현재 자료로 검토하고 새 합의를 진행해 주세요.",
    "REVIEW_NOT_VERIFIED": "현재 항목과 적용성에 맞는 서버 검토가 필요합니다. 조건을 확인한 뒤 다시 질문해 주세요.",
    "INVALID_REVIEW": "채택할 검토의 조건과 근거를 확인해 주세요.",
    "W_NOT_APPLICABLE": "보증 프리미엄 W가 있는 현재 계약에서 사용할 수 있는 요청입니다.",
    "FUNDED_W_REQUIRED": "보증 프리미엄 W가 있는 예치 계약이 필요합니다.",
    "RETRY_REQUIRES_FAILED": "확정 실패한 작업만 재시도할 수 있습니다. 결과 불명 작업은 대사를 요청해 주세요.",
    "RETRY_CONFLICT": "관련 정산이나 분쟁 상태를 확인한 뒤 재시도해 주세요.",
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
    "JOBS_UNAVAILABLE": "문서 작업 처리기가 아직 연결되지 않았습니다. 서버 설정을 확인해 주세요.",
    "DOCUMENT_JOB_FAILED": "문서 준비를 마치지 못했습니다. 작업 단계와 자료를 확인해 주세요.",
    "JOB_INTERRUPTED": "문서 작업이 중단되었습니다. 서버 처리기의 복구 상태를 확인해 주세요.",
    "JOB_RETRY_LIMIT": "문서 작업의 재시도 한도에 도달했습니다.",
    "DATASET_CHANGED": "입력 자료가 변경되었습니다. 현재 항목으로 새 문서 작업을 요청해 주세요.",
    "PROMPT_CONFIG_MISSING": "서버의 답변 설정이 없습니다. 운영자가 비공개 설정을 확인해야 합니다.",
    "PROMPT_CONFIG_INVALID": "서버의 답변 설정을 검증하지 못했습니다. 운영자가 확인해야 합니다.",
    "SOURCE_REVIEW_MISSING": "해당 출처에 필요한 이용 범위 확인이 없습니다.",
    "SOURCE_NOT_REGISTERED": "서버에 등록된 공식 출처가 필요합니다.",
    "COLLECTION_DISABLED": "서버의 문서 수집 설정이 비활성입니다.",
    "NETWORK_DISABLED": "외부 본문 수집이 비활성입니다. 서버의 허용 범위를 확인해 주세요.",
    "SEARCH_UNAVAILABLE": "공식 문서 후보 탐색 설정을 확인해 주세요.",
    "SEARCH_FAILED": "공식 문서 후보를 탐색하지 못했습니다.",
    "SEARCH_PROVIDER_UNAVAILABLE": "문서 탐색 제공기관을 사용할 수 없습니다.",
    "HTTP_RATE_LIMITED": "출처가 요청 빈도를 제한했습니다. 서버가 안내한 재시도 상태를 확인해 주세요.",
    "HTTP_FAILED": "공식 출처에 연결하지 못했습니다. 문서 작업 상태를 확인해 주세요.",
    "DOCUMENT_VERSION_CONFLICT": "등록된 문서 버전이 달라졌습니다. 문서 연결을 확인해 주세요.",
    "LIVE_BUDGET_UNCONFIRMED": "GMS 가격·크레딧 예산이 확정되지 않아 LIVE 실행이 보류되었습니다.",
    "LIVE_BUDGET_UNAVAILABLE": "GMS 예산 상태를 확인할 수 없습니다. 서버 운영자가 확인해야 합니다.",
    "CREDIT_LIMIT_REACHED": "승인된 GMS 크레딧 사용 한도에 도달했습니다.",
    "USAGE_LIMIT_REACHED": "승인된 LIVE 요청 사용 한도에 도달했습니다.",
    "PROCESSING_APPROVAL_REQUIRED": "LIVE 실행에 필요한 처리 범위·사용 예산을 확인해야 합니다.",
    "LIVE_PURPOSE_REQUIRED": "허용된 목적의 명시적 CLI LIVE 실행이 필요합니다.",
})
for _code in ("JOB_CLAIM_LOST", "JOB_PROGRESS_INVALID", "JOB_REQUEST_INVALID", "JOB_RESULT_INVALID"):
    ERRORS[_code] = "문서 작업의 실행 상태를 확인하지 못했습니다. 서버 처리기 상태를 확인해 주세요."
APPLICABILITY_LABELS = {
    "country": "가입 국가·지역",
    "enrollment_date": "가입일 (YYYY-MM-DD)",
    "plan_generation": "플랜·약관 세대",
    "payment_method": "결제 방식",
    "channel": "가입 채널",
    "document_type": "적용 문서 종류",
}
JOB_ACTIVE = frozenset({"QUEUED", "RUNNING"})
JOB_TERMINAL = frozenset({"COMPLETED", "BLOCKED", "FAILED"})
JOB_LABELS = {"QUEUED": "접수됨 · 처리 대기", "RUNNING": "진행 중", "COMPLETED": "완료",
              "BLOCKED": "자료·허용 범위 확인 필요", "FAILED": "실패"}
JOB_STAGES = {
    "QUEUED": "작업 대기", "RECOVERING": "중단된 작업 복구", "QUERYING": "검색어 생성",
    "DISCOVERING": "공식 문서 후보 탐색", "SELECTING": "후보 선별", "FETCHING": "허용된 본문 수집",
    "CHECKING_DOCUMENTS": "문서·이용 범위 확인", "PARSING_STORED_BODIES": "저장 본문 파싱",
    "INDEXING": "문서 색인", "INDEX_READY": "색인 준비 완료", "DOC_BLOCKED": "문서 준비 보류",
    "COMPLETED": "작업 완료", "FAILED": "작업 실패",
}
JOB_FIELDS = frozenset({"job_id", "item_id", "requester", "status", "stage", "failure_code",
                        "candidate_count", "body_count", "chunk_count", "revision", "created_at",
                        "updated_at", "attempt_no", "retryable"})
LIVE_PURPOSE_LABELS = {"evaluation": "평가", "verification": "검증", "final_verification": "최종 검증",
                       "demo": "시연", "embedding_preparation": "임베딩 준비"}
GMS_UI_NOTICE = "GMS LIVE는 명시한 CLI 명령에서만 실행합니다. 일반 화면에서는 GMS 문서 준비·재시도·질문을 실행하지 않습니다."


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
            source_number = row.get("number")
            citation_number = source_number if type(source_number) is int and source_number > 0 else number
            st.text(f"[{citation_number}] {row.get('title', '제목 미확인')}")
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
    elif result.get("model"):
        st.caption("실모델 모드 · 적용성·근거와 지급 조건은 서버가 별도로 검증합니다.")
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


def _checklist_context(st: Any, result: dict[str, Any]) -> None:
    """Show the service-selected inputs and versions behind this checklist."""
    with st.expander("이 체크리스트의 적용 조건·문서 버전", expanded=True):
        st.text("검토 ID: " + str(result.get("review_id") or "현재 조건에 맞는 저장 검토 없음"))
        st.text("문서 색인 revision: " + str(result.get("revision") or "미준비"))
        inputs = result.get("applicability") or {}
        if inputs:
            for key, label in APPLICABILITY_LABELS.items():
                if inputs.get(key):
                    st.text(f"{label}: {inputs[key]}")
        else:
            st.caption("확인된 적용성 입력이 없습니다. 가입 조건을 입력한 뒤 질문해 주세요.")
        documents = result.get("documents") or []
        if not documents:
            st.caption("이 체크리스트에 연결된 사용 가능한 문서 버전이 없습니다.")
        for document in documents:
            if isinstance(document, dict):
                st.text(f"문서: {document.get('title', '제목 미확인')} · 버전: {document.get('version_id', '미확인')} · 역할: {document.get('role', '미확인')}")
        st.caption("연결 문서와 실제 답변 인용은 구분합니다. 실제 인용 목록은 아래 근거 패널에서 확인하세요.")


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
        st.text(f"메타데이터 색인: {corpus.get('metadata_count', 0)}개")
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
    st.text("메타데이터 로드: " + ("준비됨" if readiness.get("status") == "META_READY" else "상태 확인 필요"))
    st.caption("메타데이터와 공식 문서 본문은 별도 자료입니다. 현재 공식 문서 준비 상태는 아래에서 확인하세요.")
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


def _read_runtime_status(st: Any, app: Any) -> dict[str, Any] | None:
    getter = getattr(app, "runtime_status", None)
    ok, status = _call(st, getter) if callable(getter) else (True, {})
    if not ok:
        return None
    if not isinstance(status, dict):
        _error(st, {"code": "INVALID_RESPONSE"})
        return None
    return status


def gms_selected(status: dict[str, Any]) -> bool:
    return any(isinstance(status.get(field), dict) and status[field].get("provider") == "gms"
               for field in ("chat", "embedding"))


def _ordinary_model_action_allowed(st: Any, app: Any) -> bool:
    """UI requests never grant the server's explicit CLI LIVE execution scope."""
    status = _read_runtime_status(st, app)
    if status is None:
        return False
    if gms_selected(status):
        st.warning(GMS_UI_NOTICE)
        return False
    return True


def _live_status(st: Any, app: Any) -> None:
    getter = getattr(app, "live_status", None)
    ok, value = _call(st, getter) if callable(getter) else (True, {})
    value = value if ok and isinstance(value, dict) else {}
    st.caption("개발·회귀 검증: Codex·MOCK. GMS 70,000크레딧 사용 계획은 평가·검증·시연과 이에 필요한 임베딩 준비 범위에서 관리합니다.")
    st.caption("화면 조회·새로고침은 GMS 호출을 만들지 않습니다. LIVE 실행 권한은 명시한 CLI 명령의 서버 범위에서만 부여됩니다.")
    if not value:
        st.text("GMS LIVE 사전 설정: 확인되지 않음")
        return
    st.text("GMS LIVE 실행 설정: " + ("활성" if value.get("enabled") is True else "비활성"))
    if value.get("budget_confirmed") is not True:
        st.warning("GMS 가격·예산 범위가 확정되지 않아 LIVE 준비를 완료하지 못했습니다.")
    elif value.get("ready") is not True:
        st.warning("GMS LIVE 준비 미완료 · 서버의 연결 설정과 허용 범위를 확인해야 합니다.")
    else:
        st.text("GMS LIVE 사전 설정: 확인됨 · 명시한 CLI 실행 필요")
    purposes = value.get("purposes")
    if isinstance(purposes, list):
        labels = [label for purpose, label in LIVE_PURPOSE_LABELS.items() if purpose in purposes]
        st.text("LIVE 설정 목적: " + (", ".join(labels) if labels else "확인되지 않음"))


def _runtime_status(st: Any, app: Any) -> dict[str, Any]:
    """Read only public status methods; never inspect credentials or enter LIVE."""
    status = _read_runtime_status(st, app) or {}
    with st.expander("모델·문서 수집 연결 상태", expanded=True):
        columns = st.columns(2)
        for column, field, label in zip(columns, ("chat", "embedding"), ("대화 모델", "임베딩 모델")):
            value = status.get(field) or {}
            if not value:
                column.text(f"{label}: 준비 상태 미확인")
                continue
            provider = {"mock": "MOCK", "openai": "OpenAI", "gms": "GMS", "local": "로컬"}.get(value.get("provider"), "제공자 미확인")
            if value.get("is_mock") is True:
                column.text(f"{label}: {provider} · 오프라인 모의 모드")
            elif value.get("configured") is not True:
                column.warning(f"{label}: {provider} · 서버 설정 미완료")
            elif value.get("ready") is not True:
                column.warning(f"{label}: {provider} · 준비 미완료")
            else:
                column.text(f"{label}: {provider} · 실모델 설정 준비됨")
        st.caption("설정 준비 상태를 조회한 결과입니다. 실제 모델 호출 성공이나 답변 품질 검증을 뜻하지 않습니다. MOCK 응답·임베딩은 합성 검증용입니다.")
        _live_status(st, app)
        if gms_selected(status):
            st.info(GMS_UI_NOTICE)
        collection = status.get("collection") or {}
        if collection:
            count = collection.get("registered_target_count")
            st.text("등록된 문서 수집 대상: " + (f"{count}개" if type(count) is int else "미확인"))
            st.text("문서 수집 설정: " + ("활성" if collection.get("enabled") is True else "비활성"))
            st.text("외부 본문 수집: " + ("서버 허용 범위에서 사용" if collection.get("network_enabled") is True else "비활성"))
        for field, label in (("external_processing_approved", "외부 문서 처리"),
                             ("question_processing_approved", "질문의 외부 처리")):
            if field in status:
                st.text(label + ": " + ("허용 범위 확인됨" if status[field] is True else "허용 범위 확인 필요"))
        if "document_worker_enabled" in status and status["document_worker_enabled"] is not True:
            st.warning("문서 자동 처리기가 비활성입니다. 접수된 작업은 서버 처리기가 실행될 때까지 대기할 수 있습니다.")
        st.caption("연결 설정과 허용 범위는 서버에서 관리합니다. 이 화면은 키·환경 설정·전체 프롬프트를 입력하거나 표시하지 않습니다.")
    return status


def _document_readiness(st: Any, result: dict[str, Any] | None) -> None:
    readiness = (result or {}).get("readiness") or {}
    status = readiness.get("status")
    if status == "INDEX_READY":
        st.success("문서 준비: 연결된 문서 색인을 사용할 수 있습니다.")
        st.caption("공식 문서와 합성 시연 문서는 현재 집계와 실제 인용 근거의 유형으로 구분합니다.")
        st.caption("문서 준비와 계약 적용성 확인, 실제 보증 이전, 지급 승인은 각각 확인해야 합니다.")
    elif status == "DOC_BLOCKED":
        st.warning("DOC_BLOCKED · 공식 문서 준비가 보류되었습니다. 해당 항목의 본문·이용 범위·적용 정보를 확인해 주세요.")
    elif status == "INDEX_EMPTY":
        st.warning("INDEX_EMPTY · 문서 검색 색인이 없습니다. 문서 준비 작업을 요청해 주세요.")
    else:
        st.caption("공식 문서 준비 상태를 아직 확인하지 못했습니다.")


def _store_job(st: Any, job: Any, actor: str, item_id: str, key: str) -> bool:
    if (not isinstance(job, dict) or not isinstance(job.get("job_id"), str)
            or not 0 < len(job["job_id"]) <= 128):
        _error(st, {"code": "INVALID_RESPONSE"})
        return False
    if job.get("requester", actor) != actor or job.get("item_id", item_id) != item_id:
        _error(st, {"code": "FORBIDDEN"})
        return False
    st.session_state[key] = {field: value for field, value in job.items() if field in JOB_FIELDS}
    return True


def _enqueue_job(st: Any, app: Any, actor: str, item_id: str, applicability: dict[str, str],
                 question: str, key: str) -> None:
    if not _ordinary_model_action_allowed(st, app):
        return
    with st.spinner("문서 준비 작업을 접수하는 중…"):
        ok, job = _call(st, lambda: app.rag.enqueue_prepare(actor, item_id, applicability=applicability,
                                                           question=question.strip() or None))
    if ok and _store_job(st, job, actor, item_id, key):
        st.rerun()


def _job(st: Any, job: dict[str, Any]) -> None:
    st.text(f"작업 ID: {job.get('job_id', '')}")
    status = job.get("status")
    label = JOB_LABELS.get(status, "상태 미확인")
    if status == "COMPLETED":
        st.success("문서 준비 작업: " + label)
    elif status in JOB_ACTIVE:
        st.info("문서 준비 작업: " + label)
    else:
        st.warning("문서 준비 작업: " + label)
    st.text("현재 단계: " + JOB_STAGES.get(job.get("stage"), "단계 확인 중"))
    st.text(f"후보 {job.get('candidate_count', 0)} · 본문 {job.get('body_count', 0)} · 청크 {job.get('chunk_count', 0)}")
    stamp = job.get("updated_at")
    if isinstance(stamp, str):
        try:
            updated = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            updated = updated.replace(tzinfo=timezone.utc) if updated.tzinfo is None else updated
            st.caption("서버 상태 갱신: " + updated.astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M:%S KST"))
        except ValueError:
            pass
    if job.get("failure_code"):
        _error(st, {"code": job["failure_code"], "retryable": job.get("retryable") is True})


def _document_job(st: Any, app: Any, actor: str, item_id: str, applicability: dict[str, str],
                  question: str, prefix: str, model_actions_blocked: bool = False) -> None:
    key = f"job:{prefix}"
    job = st.session_state.get(key)
    if not job:
        st.caption("문서 준비를 요청하면 작업 ID와 실제 처리 단계가 여기에 표시됩니다.")
        return
    active = job.get("status") in JOB_ACTIVE

    @st.fragment(run_every=2.0 if active else None)
    def panel():
        if (st.session_state.get("warranty_demo_actor") != actor
                or st.session_state.get("warranty_selected_item") != item_id):
            return
        current = st.session_state.get(key)
        if not current:
            return
        refresh = st.button("작업 상태 새로고침", key=f"job_refresh:{prefix}")
        was_active = current.get("status") in JOB_ACTIVE
        if was_active or refresh:
            ok, latest = _call(st, lambda: app.rag.job(actor, current["job_id"]))
            if not ok or not _store_job(st, latest, actor, item_id, key):
                st.session_state.pop(key, None)
                return
            current = st.session_state[key]
            if was_active and current.get("status") in JOB_TERMINAL:
                st.rerun()
        _job(st, current)
        if current.get("status") in JOB_ACTIVE:
            st.caption("2초마다 작업 상태를 조회합니다. 창이 열려 있는 동안 자동 갱신되며 수동 새로고침도 가능합니다.")
        elif current.get("status") != "COMPLETED" and current.get("retryable") is True:
            st.caption("아래 요청은 현재 적용성 입력으로 새 작업을 접수합니다. 처리 허용 범위와 자료를 먼저 확인해 주세요.")
            if st.button("문서 준비 다시 요청", key=f"job_retry:{prefix}", disabled=model_actions_blocked):
                _enqueue_job(st, app, actor, item_id, applicability, question, key)
    panel()


def _rag(st: Any, app: Any, actor: str, item: dict[str, Any], applicability: dict[str, str],
         runtime_status: dict[str, Any] | None = None) -> None:
    item_id = item["item_id"]
    prefix = conversation_key(actor, item_id)
    model_actions_blocked = gms_selected(runtime_status or {})
    checklist_ok, checklist = _call(st, lambda: app.rag.checklist(item_id, applicability=applicability, user_id=actor))
    latest_key = f"latest_review:{prefix}"
    if checklist_ok and checklist.get("review_id"):
        st.session_state[latest_key] = checklist
    else:
        st.session_state.pop(latest_key, None)
    _document_readiness(st, checklist if checklist_ok else None)
    with st.expander("문서 준비·작업 진행"):
        st.caption("서버에 등록되고 이용 범위가 승인된 문서만 처리합니다. 임의 URL 입력과 실제 파일 업로드는 비활성입니다.")
        question = st.text_input("문서 탐색 질문 (선택)", key=f"job_question:{prefix}", max_chars=2000,
                                 placeholder="확인할 조건을 입력하면 해당 질문으로 문서 준비를 요청합니다.")
        if st.button("문서 준비 요청", key=f"prepare:{prefix}", disabled=model_actions_blocked):
            _enqueue_job(st, app, actor, item_id, applicability, question, f"job:{prefix}")
        _document_job(st, app, actor, item_id, applicability, question, prefix, model_actions_blocked)
    chat_tab, checklist_tab = st.tabs(["항목별 질문", "보증 이전 조건표·체크리스트"])
    with chat_tab:
        st.caption("선택 항목과 현재 시연 사용자별로 대화를 보관합니다. 질문은 승인이나 지급을 실행하지 않습니다.")
        ok, history = _call(st, lambda: app.rag.history(actor, item_id))
        if ok:
            for message in history:
                with st.chat_message("user"):
                    st.text(str(message.get("question", "")))
                with st.chat_message("assistant"):
                    _review(st, message.get("result") or {})
        with st.form(f"ask:{prefix}", clear_on_submit=True):
            question = st.text_area("이 항목에 질문하기", placeholder="예: 가입일에 따라 적용되는 이전 조건이 달라지나요?", max_chars=2000)
            top_k = st.select_slider("외부 문서 검색 수", options=[3, 6, 10], value=6)
            submitted = st.form_submit_button("근거와 함께 질문", type="primary", disabled=model_actions_blocked)
        if submitted:
            if not question.strip():
                st.warning("질문을 입력해 주세요.")
            elif _ordinary_model_action_allowed(st, app):
                with st.spinner("선택 항목의 근거를 검색하고 답변을 생성하는 중…"):
                    ok, result = _call(st, lambda: app.rag.ask(actor, item_id, question.strip(),
                                                              applicability=applicability, top_k=top_k))
                if ok:
                    st.session_state[f"latest_review:{prefix}"] = result
                    st.rerun()
    with checklist_tab:
        if checklist_ok:
            _checklist_context(st, checklist)
            _review(st, checklist)
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
            if actor in ("buyer", "seller"):
                with st.form(f"revise_contract:{actor}:{trade_id}:{contract.get('revision')}"):
                    st.caption("금액·반환 조건·증빙 기준을 수정하면 새 계약 revision이 됩니다. 양측 동의와 수령·증빙 확인을 다시 진행해야 합니다. 예치 후 금액 변경과 진행 중 정산의 변경 가능 여부는 서버가 검증합니다.")
                    columns = st.columns(2)
                    revised_d = columns[0].number_input("수정 물건 대금 D (시연 원)", min_value=1,
                                                        value=int(contract.get("D", d)), step=1000)
                    revised_w = columns[1].number_input("수정 보증 프리미엄 W (시연 원)", min_value=0,
                                                        value=int(contract.get("W", w)), step=1000)
                    terms = contract.get("terms") or {}
                    returns = st.text_area("반환 조건 (한 줄에 하나)",
                                           value="\n".join(terms.get("return_conditions") or []), max_chars=4000)
                    evidence_standard = st.text_area("증빙 확인 기준", value=terms.get("evidence_standard", ""), max_chars=2000)
                    deadline = st.text_input("증빙 확인 기한 (선택)", value=terms.get("confirmation_deadline") or "", max_chars=100)
                    dispute_rule = st.text_area("분쟁 처리 규칙", value=terms.get("dispute_rule", ""), max_chars=2000)
                    if st.form_submit_button("수정 조건으로 새 계약 revision 요청"):
                        _mutate(st, app, actor, trade, "contract", {
                            "D": int(revised_d), "W": int(revised_w),
                            "terms": {"return_conditions": [line.strip() for line in returns.splitlines() if line.strip()],
                                      "evidence_standard": evidence_standard.strip(),
                                      "confirmation_deadline": deadline.strip() or None,
                                      "dispute_rule": dispute_rule.strip()}})
            _text_list(st, trade.get("contract_consents"))
            if actor in ("buyer", "seller"):
                exact_contract = {"revision": contract.get("revision"), "hash": contract.get("hash")}
                _action(st, app, actor, trade, "contract_consent", "현재 계약 revision/hash에 동의", exact_contract)
            _action(st, app, actor, trade, "deposit", "D+W 전액 시연 예치 요청", {})
            _action(st, app, actor, trade, "receipt", "구매자 · 현재 계약의 물건 수령 확인", {"contract_revision": contract.get("revision")})
            latest = st.session_state.get(f"latest_review:{conversation_key(actor, item_id)}") or {}
            review_id = latest.get("review_id")
            if review_id:
                st.text(f"현재 적용 조건에 맞는 채택 검토: {review_id}")
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
                conditions = proposal.get("conditions") or {}
                for field, label in (("contract_revision", "정산안의 계약 revision"),
                                     ("review_id", "정산안의 채택 검토 ID"),
                                     ("evidence_id", "정산안의 증빙 ID"),
                                     ("refund_reason", "정산안의 반환 사유")):
                    st.text(f"{label}: {conditions.get(field, proposal.get(field)) or '없음'}")
                if conditions.get("resolution_context"):
                    st.text("정산안에 연결된 해결안 ID: " + ", ".join(map(str, conditions["resolution_context"])))
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
        runtime_status = _runtime_status(st, app)
        selected = _catalog(st, app)
        if selected is None:
            return
        ok, item = _call(st, lambda: app.catalog.get(selected))
        if not ok:
            return
        st.divider()
        _details(st, app, item)
        applicability = _applicability(st, actor, selected)
        _rag(st, app, actor, item, applicability, runtime_status)
        _trade(st, app, actor, selected)
    except Exception as exc:
        _error(st, exc)


if __name__ == "__main__":
    run()
