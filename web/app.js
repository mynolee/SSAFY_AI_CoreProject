"use strict";

const $ = (id) => document.getElementById(id);
const state = { actor: null, csrf: null, selected: null, trade: null, offset: 0, limit: 12, total: 0, applicability: new Map(), reviews: new Map(), jobs: new Map(), jobWatches: new Map(), mutationKeys: new Map(), busy: new Set() };
const won = (value) => value === null || value === undefined ? "미확인" : `${Number(value).toLocaleString("ko-KR")}원`;
const asText = (value) => typeof value === "string" ? value : JSON.stringify(value, null, 2);
const clear = (node) => node.replaceChildren();
const STATUS = { DOC_BLOCKED: "공식 문서 확인 필요", INDEX_READY: "문서 검색 준비됨", META_READY: "항목 정보 준비됨", BLOCKED: "추가 확인 필요", FAILED: "처리 실패", RUNNING: "처리 중", COMPLETED: "준비 완료", NEEDS_INFORMATION: "정보 부족 · 확인 보류", TRANSFERABLE: "약관상 이전 가능", CONDITIONAL: "조건부 · 확인 필요", NOT_TRANSFERABLE: "약관상 이전 불가", SUCCEEDED: "완료", RESERVED: "예약됨", UNKNOWN: "결과 불명 · 대사 필요", DRAFT: "계약 승인 대기", CONTRACTED: "계약 합의됨", AGREED: "계약 합의됨", FUNDED: "예치됨", ACTIVE: "거래 진행 중", CLOSED: "거래 종료", CANCELLED: "예치 전 취소", DISPUTED: "분쟁 확인 중" };
const LABELS = { country: "가입 국가", enrollment_date: "가입일", plan_generation: "플랜·약관 세대", payment_method: "결제 방식", channel: "가입 채널", document_type: "적용 문서 종류", own_document: "선택 항목의 승인·색인된 공식 본문", applicable_transfer_terms: "이 항목에 적용되는 이전 약관 근거", evidence_supported_review_required: "문서 근거가 포함된 질문·검토", validated_model_output_required: "검증된 형식과 근거를 갖춘 답변", validated_item_evidence_required: "현재 항목·문서 버전에 맞는 검증된 근거", OWN_DOCUMENT_MISSING: "선택 항목의 공식 문서 본문이 필요합니다.", NO_APPROVED_LINKED_DOCUMENTS: "이 항목에 연결된 승인 문서가 없습니다.", SOURCE_BLOCKED: "출처의 처리·표시 이용 범위를 확인해야 합니다.", KEY_MISSING: "모델 연결 설정이 필요합니다.", INDEX_EMPTY: "문서 검색 색인이 필요합니다.", APPLICABILITY_UNKNOWN: "가입 조건과 문서 적용 범위를 확인해야 합니다.", MODEL_OUTPUT_INVALID: "답변 형식과 근거를 검증하지 못했습니다.", FETCH_FAILED: "문서 요청에 실패했습니다. 출처 상태와 작업 준비를 확인하세요.", PARSE_FAILED: "문서 본문을 읽지 못했습니다. 문서 형식을 확인하세요." };
const friendly = (value) => { if (typeof value === "string") { const key = value.split(":")[0]; return LABELS[key] || STATUS[value] || value; } if (value && typeof value === "object") return LABELS[value.code] || value.text || value.message || value.condition || value.description || "세부 자료 확인이 필요합니다."; return "미확인"; };
const lines = (values) => (Array.isArray(values) ? values : [values]).map(friendly).join("\n");
function readinessText(readiness = {}) {
  readiness = readiness || {};
  const status = readiness.documents || readiness.status;
  const reasons = readiness.reasons || [];
  return `${STATUS[status] || "문서 준비 여부 미확인"}${reasons.length ? `\n${lines(reasons)}` : ""}`;
}
const scope = (itemId = state.selected?.item_id) => JSON.stringify([state.actor, itemId]);
const JOB_STAGES = { QUEUED: "작업 대기", RECOVERING: "중단된 작업 복구", QUERYING: "문서 검색어 준비", DISCOVERING: "등록 문서 탐색", SELECTING: "문서 후보 선별", FETCHING: "승인된 문서 수집", CHECKING_DOCUMENTS: "등록 문서·권한 확인", PARSING_STORED_BODIES: "본문 추출", INDEXING: "문서 색인", INDEX_READY: "검색 준비", DOC_BLOCKED: "필요 자료 확인", COMPLETED: "준비 완료", FAILED: "처리 실패" };
const JOB_TERMINAL = new Set(["COMPLETED", "FAILED", "BLOCKED"]);
function mutationKey(target, action, payload, version) {
  const intent = JSON.stringify([state.actor, target, action, payload, version]);
  if (!state.mutationKeys.has(intent)) state.mutationKeys.set(intent, crypto.randomUUID());
  return state.mutationKeys.get(intent);
}
function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function notice(message, info = false) {
  $("notice").textContent = message;
  $("notice").className = info ? "info" : "";
  $("notice").hidden = !message;
}
function errorText(error) {
  return `${error.code || "REQUEST_FAILED"} · ${error.message || "요청을 처리하지 못했습니다."}${error.retryable ? "\n재시도할 수 있습니다." : ""}${error.request_id ? `\n요청 ID: ${error.request_id}` : ""}`;
}
async function api(path, options = {}) {
  const headers = { Accept: "application/json" };
  const method = options.method || "GET";
  if (method !== "GET") {
    headers["Content-Type"] = "application/json";
    if (state.csrf) headers["X-CSRF-Token"] = state.csrf;
  }
  if (options.key) headers["Idempotency-Key"] = options.key;
  let response;
  try {
    response = await fetch(path, { method, headers, credentials: "same-origin", body: method === "GET" ? undefined : JSON.stringify(options.body || {}) });
  } catch (_) {
    throw { code: "CONNECTION_FAILED", message: "서버 연결을 확인해 주세요.", retryable: true };
  }
  let envelope;
  try { envelope = await response.json(); } catch (_) { throw { code: "INVALID_RESPONSE", message: "서버 응답을 확인할 수 없습니다." }; }
  if (!response.ok || envelope.error) {
    if (response.status === 401) { state.csrf = null; state.actor = null; $("session-status").textContent = "다시 로그인 필요"; }
    throw { ...(envelope.error || { code: "REQUEST_FAILED", message: "요청을 처리하지 못했습니다." }), request_id: envelope.request_id };
  }
  return envelope.data;
}
async function run(name, work) {
  if (state.busy.has(name)) return;
  state.busy.add(name);
  notice("");
  try { await work(); } catch (error) { notice(errorText(error)); }
  finally { state.busy.delete(name); }
}
function requireItem() {
  if (!state.selected) throw { code: "ITEM_REQUIRED", message: "먼저 항목을 선택해 주세요." };
  return state.selected.item_id;
}
function fact(parent, label, value) {
  const box = el("div", undefined, "fact");
  box.append(el("small", label), el("span", value)); parent.append(box);
}
async function loadRuntime() {
  let node = $("runtime-status");
  if (!node) {
    node = el("div", undefined, "readiness"); node.id = "runtime-status";
    node.setAttribute("role", "status"); node.setAttribute("aria-live", "polite");
    document.querySelector(".page-heading").after(node);
  }
  if (!state.actor) { node.textContent = "로그인 후 모델과 문서 처리의 설정 준비 상태를 확인할 수 있습니다."; return; }
  const runtime = await api("/runtime");
  const adapter = (value = {}) => `${value.is_mock ? "MOCK · 모의" : value.provider === "gms" ? "GMS · LIVE 전용" : value.provider === "openai" ? "외부 모델" : "미설정"} · ${value.ready ? "설정 준비됨" : value.configured ? "준비 확인 필요" : "설정 필요"}`;
  const live = runtime.live || {};
  const liveText = runtime.chat?.provider === "gms" || runtime.embedding?.provider === "gms"
    ? `\nGMS LIVE ${live.ready ? "설정 준비됨" : live.enabled ? "예산·승인 확인 필요" : "사용 설정 필요"} · ${live.budget_confirmed ? "예산 확인됨" : "예산 미확인"}\nGMS 실행은 명시적 CLI 실행 범위에서만 허용됩니다. 화면 진입·새로고침과 일반 작업 재개는 LIVE 실행 승인을 부여하지 않습니다.` : "";
  node.textContent = `답변 ${adapter(runtime.chat)}\n검색 임베딩 ${adapter(runtime.embedding)}\n문서 처리기 ${runtime.document_worker_enabled ? "설정됨" : "설정 필요"} · 등록 수집 대상 ${runtime.collection?.registered_target_count || 0}개${liveText}\n표시된 준비 상태는 설정과 처리 범위 확인 결과이며 실제 모델 연결 성공이나 공식 문서 확보를 뜻하지 않습니다.`;
}
function renderJob(job, itemId, actor, waiting = false) {
  if (state.selected?.item_id !== itemId || state.actor !== actor) return;
  const node = $("job-status"); clear(node);
  const status = job.status === "QUEUED" ? "대기 중" : STATUS[job.status] || "상태 확인";
  const message = `${JOB_STAGES[job.stage] || "문서 처리"} · ${status}\n등록 후보 ${job.candidate_count || 0}건 · 처리 본문 ${job.body_count || 0}건 · 색인 청크 ${job.chunk_count || 0}개${job.attempt_no ? ` · 시도 ${job.attempt_no}회` : ""}`;
  node.append(el("p", message, "subtle"));
  if (job.failure_code) node.append(el("p", `${job.failure_code} · ${LABELS[job.failure_code] || "문서 처리 조건과 서버 상태를 확인해 주세요."}`, "subtle"));
  if (waiting) node.append(el("p", "작업은 서버에서 계속 진행됩니다. 상태 새로고침으로 결과를 다시 확인하세요.", "subtle"));
  if (!job.job_id) return;
  node.append(el("p", `작업 ID: ${job.job_id}`, "subtle"));
  const refresh = el("button", "상태 새로고침", "secondary"); refresh.type = "button";
  refresh.addEventListener("click", () => run("job-refresh", async () => {
    const current = await api(`/index-jobs/${encodeURIComponent(job.job_id)}`);
    await watchJob(current, itemId, actor);
  }));
  node.append(refresh);
  if (job.retryable === true && JOB_TERMINAL.has(job.status)) {
    const retry = el("button", "문서 준비 재시도", "secondary"); retry.type = "button";
    retry.addEventListener("click", () => run("job-retry", async () => {
      const queued = await api(`/index-jobs/${encodeURIComponent(job.job_id)}/retry`, { method: "POST", body: {} });
      await watchJob(queued, itemId, actor);
    }));
    node.append(retry);
  }
}
async function watchJob(initial, itemId, actor) {
  const key = JSON.stringify([actor, itemId]); const token = Symbol("document-job-watch");
  state.jobWatches.set(key, token);
  let job = initial;
  for (let tries = 0; tries < 60; tries++) {
    if (state.jobWatches.get(key) !== token || state.selected?.item_id !== itemId || state.actor !== actor) return;
    state.jobs.set(key, job); renderJob(job, itemId, actor);
    if (!job.job_id || JOB_TERMINAL.has(job.status)) return;
    await new Promise((resolve) => setTimeout(resolve, 1000));
    if (state.jobWatches.get(key) !== token || state.selected?.item_id !== itemId || state.actor !== actor) return;
    job = await api(`/index-jobs/${encodeURIComponent(job.job_id)}`);
  }
  state.jobs.set(key, job); renderJob(job, itemId, actor, true);
}
function itemCard(item) {
  const button = el("button", undefined, `item-card${state.selected?.item_id === item.item_id ? " selected" : ""}`);
  button.type = "button";
  button.dataset.itemId = item.item_id;
  button.append(el("span", `${item.provider || "제공사 미확인"}${item.is_synthetic ? " · 합성 시연 항목" : ""}`, "provider"), el("strong", item.name || item.item_id), el("span", `월요금 ${won(item.monthly_fee_won)} · 이전 ${item.transfer_allowed === true ? "가능 확인" : item.transfer_allowed === false ? "불가 확인" : "미확인"}`, "price"));
  if (item.unknown_conditions?.length) button.append(el("span", `미확인: ${item.unknown_conditions.join(", ")}`, "subtle"));
  button.addEventListener("click", () => run("select", () => selectItem(item.item_id)));
  return button;
}
function renderItems(items, unknown = []) {
  clear($("items")); clear($("unknown-items"));
  if (!items.length) $("items").append(el("p", "확인된 값으로 조건을 충족하는 항목이 없습니다.", "empty"));
  for (const item of items) $("items").append(itemCard(item));
  for (const item of unknown) $("unknown-items").append(itemCard(item));
  $("unknown-wrap").hidden = !unknown.length;
  $("unknown-summary").textContent = `조건 확인이 필요한 후보 ${unknown.length}개`;
}
async function loadItems(offset = 0) {
  const data = await api(`/items?offset=${offset}&limit=${state.limit}`);
  state.offset = offset; state.total = data.total;
  $("catalog-count").textContent = `${data.total}개 등록 항목`;
  $("search-state").textContent = "확인된 값 기준 · 판매 여부 미검증";
  renderItems(data.items);
  clear($("pagination"));
  if (offset > 0) {
    const prev = el("button", "이전", "secondary"); prev.addEventListener("click", () => run("items", () => loadItems(Math.max(0, offset - state.limit)))); $("pagination").append(prev);
  }
  $("pagination").append(el("span", `${Math.min(offset + 1, data.total)}–${Math.min(offset + state.limit, data.total)} / ${data.total}`, "subtle"));
  if (offset + state.limit < data.total) {
    const next = el("button", "다음", "secondary"); next.addEventListener("click", () => run("items", () => loadItems(offset + state.limit))); $("pagination").append(next);
  }
}
function applicability() {
  const values = {};
  for (const input of document.querySelectorAll("[data-applicability]")) if (input.value.trim()) values[input.dataset.applicability] = input.value.trim();
  return values;
}
function restoreApplicability(itemId) {
  const values = state.applicability.get(scope(itemId)) || {};
  for (const input of document.querySelectorAll("[data-applicability]")) input.value = values[input.dataset.applicability] || "";
}
function addMessage(role, text, suffix = "") {
  for (const placeholder of $("chat-log").querySelectorAll(".empty")) placeholder.remove();
  const node = el("div", undefined, `message ${role === "user" ? "user" : "assistant"}`);
  node.append(el("small", role === "user" ? "나의 질문" : `보증 조건 안내${suffix ? ` · ${STATUS[suffix] || suffix}` : ""}`), el("span", text));
  $("chat-log").append(node); $("chat-log").scrollTop = $("chat-log").scrollHeight;
}
async function loadHistory(itemId) {
  const actor = state.actor;
  clear($("chat-log"));
  if (!state.actor) { $("chat-log").append(el("p", "시연 로그인 후 항목별로 질문하고 이력을 확인할 수 있습니다.", "empty")); return; }
  const history = await api(`/items/${encodeURIComponent(itemId)}/history`);
  if (state.selected?.item_id !== itemId || state.actor !== actor) return;
  for (const entry of history) {
    if (entry.question) addMessage("user", entry.question);
    const result = entry.result || (entry.answer ? { answer: entry.answer, classification: entry.classification } : null);
    if (result) addMessage("assistant", typeof result.answer === "object" ? asText(result.answer) : result.answer || "확인 가능한 답변이 없습니다.", result.classification);
    if (!entry.question && entry.role && entry.content) addMessage(entry.role, entry.content);
  }
  if (history.at(-1)?.result) renderEvidence(history.at(-1).result);
  if (!history.length) $("chat-log").append(el("p", "선택한 항목에 관한 첫 질문을 남겨 보세요.", "empty"));
}
async function selectItem(itemId) {
  if (state.selected) state.applicability.set(scope(), applicability());
  const item = await api(`/items/${encodeURIComponent(itemId)}`);
  state.selected = item; restoreApplicability(itemId);
  $("detail").hidden = false; $("review-workspace").hidden = false;
  $("item-title").textContent = item.name;
  clear($("item-meta"));
  fact($("item-meta"), "제공사", item.provider || "미확인"); fact($("item-meta"), "월요금", won(item.monthly_fee_won));
  fact($("item-meta"), "요금 기준", item.monthly_fee_basis || "미확인");
  fact($("item-meta"), "이전 가능 여부", item.transfer_allowed === null || item.transfer_allowed === undefined ? "미확인" : item.transfer_allowed ? "확인된 가능" : "확인된 불가");
  fact($("item-meta"), "정보 구분", item.is_synthetic ? "합성 시연 항목" : "등록 메타데이터 · 판매 미검증");
  $("readiness").textContent = `${readinessText(item.readiness)}\n공식 본문·적용성이 확인되기 전에는 이전 가능 조건을 단정하지 않습니다.`;
  $("job-status").textContent = "";
  const savedJob = state.jobs.get(scope(itemId));
  if (savedJob) renderJob(savedJob, itemId, state.actor, !JOB_TERMINAL.has(savedJob.status));
  $("classification").textContent = "검토 전"; clear($("answer-meta")); clear($("evidence"));
  $("evidence").append(el("p", "이 항목 답변의 실제 인용 근거만 표시합니다.", "empty"));
  clear($("checklist")); $("checklist").append(el("p", "적용 정보를 입력한 후 체크리스트를 확인하세요.", "empty"));
  for (const card of document.querySelectorAll("[data-item-id]")) card.classList.toggle("selected", card.dataset.itemId === itemId);
  await loadHistory(itemId);
}
function renderEvidence(result) {
  $("classification").textContent = STATUS[result.classification] || "미확인";
  clear($("answer-meta")); clear($("evidence"));
  $("answer-meta").append(el("p", "약관·자료에 대한 분류이며 실제 이전 완료나 지급 승인을 의미하지 않습니다.", "subtle"));
  state.reviews.set(scope(), result);
  if (String(result.model || "").startsWith("MOCK")) $("answer-meta").append(el("div", "MOCK · 결정적 모의 응답입니다. 실제 LLM 검증 결과가 아닙니다.", "meta-block"));
  if (result.review_id) $("answer-meta").append(el("p", `서버 검토 ID: ${result.review_id}`, "subtle"));
  const groups = [["추가 확인", result.missing_information], ["상충 사항", result.conflicts], ["오류·제약", result.errors]];
  for (const [label, values] of groups) if (values && (!Array.isArray(values) || values.length)) $("answer-meta").append(el("div", `${label}\n${lines(values)}`, "meta-block"));
  if (result.readiness) $("answer-meta").append(el("p", readinessText(result.readiness), "subtle"));
  if (result.progress?.length) {
    const stages = { retrieve: "근거 검색", generate: "답변 생성", RETRIEVING: "근거 검색", GENERATING: "답변 생성", CHECKING_DOCUMENTS: "문서 확인", INDEXING: "문서 색인" };
    $("answer-meta").append(el("p", result.progress.map((step) => `${stages[step.stage] || "문서 처리"} · ${STATUS[step.status] || (step.status === "DONE" ? "완료" : "상태 확인")}`).join("\n"), "subtle"));
  }
  const ids = new Set(result.cited_evidence_ids || []);
  const all = Array.isArray(result.evidence) ? result.evidence : Object.values(result.evidence || {});
  const cited = all.filter((item) => ids.has(item.evidence_id || item.id));
  if (!cited.length) $("evidence").append(el("p", "실제 인용한 근거가 없습니다. 공식 조건은 확인되지 않았습니다.", "empty"));
  for (const item of cited) {
    const id = item.evidence_id || item.id;
    const card = el("article", undefined, "evidence-card");
    const marker = Number.isInteger(item.number) && item.number > 0 ? `[${item.number}]` : "근거 번호 미확인";
    card.append(el("strong", `${marker} ${item.title || "제목 미확인"}`), el("small", `${item.source_type || "종류 미확인"} · ${item.role || item.document_role || "역할 미확인"}${item.page ? ` · ${item.page}쪽` : ""}${item.section ? ` · ${item.section}` : ""}`), el("small", `근거 ID: ${id}`));
    if (item.text || item.excerpt) card.append(el("p", item.excerpt || item.text));
    const href = item.source_url || item.url;
    try {
      const url = new URL(href);
      if (url.protocol === "https:" && !url.username && !url.password && (!url.port || url.port === "443")) {
        const link = el("a", "등록된 원문 출처 열기 ↗"); link.href = url.href; link.target = "_blank"; link.rel = "noopener noreferrer"; card.append(link);
      }
    } catch (_) { /* An absent or unsafe link remains plain text. */ }
    $("evidence").append(card);
  }
}
function renderChecklist(data) {
  if (data.review_id) state.reviews.set(scope(), data);
  clear($("checklist"));
  const summary = el("div", `${STATUS[data.classification] || "미확인"}\n${readinessText(data.readiness)}`, "meta-block"); $("checklist").append(summary);
  const conditions = data.conditions || data.checklist || [];
  if (!conditions.length) $("checklist").append(el("p", "공식 문서에서 확인한 이전 조건이 없습니다. 필요한 자료를 확인해 주세요.", "empty"));
  for (const row of conditions) $("checklist").append(el("div", `${friendly(row)}${row.evidence_ids?.length ? `\n근거: ${row.evidence_ids.join(", ")}` : ""}`, "checklist-row"));
  if (data.missing_information?.length) $("checklist").append(el("div", `추가 확인\n${lines(data.missing_information)}`, "meta-block"));
  if (data.conflicts?.length) $("checklist").append(el("div", `상충 사항\n${lines(data.conflicts)}`, "meta-block"));
  if (data.review_id) $("checklist").append(el("p", `서버 검토 ID: ${data.review_id}`, "subtle"));
}
function tradeId() { return state.trade?.trade_id || state.trade?.id; }
function currentContract() { return state.trade?.contract || {}; }
function renderTrade(data) {
  state.trade = data; $("trade-content").hidden = false; $("trade-id").value = tradeId() || "";
  $("trade-json").textContent = JSON.stringify(data, null, 2);
  clear($("trade-summary"));
  fact($("trade-summary"), "거래 상태", STATUS[data.status] || "상태 확인 필요"); fact($("trade-summary"), "계약 버전", currentContract().revision);
  fact($("trade-summary"), "물건값 D", won(currentContract().D ?? data.D)); fact($("trade-summary"), "보증 프리미엄 W", won(currentContract().W ?? data.W));
  const terms = currentContract().terms || {};
  if (terms.return_conditions) fact($("trade-summary"), "계약의 반환 조건", terms.return_conditions.map((condition) => condition === "mutual_agreement" ? "양측 합의에 따른 반환" : condition).join(" · "));
  if (terms.evidence_standard) fact($("trade-summary"), "증빙 확인 기준", terms.evidence_standard === "SYNTHETIC bilateral confirmation" ? "합성 증빙을 양측이 확인" : terms.evidence_standard);
  fact($("trade-summary"), "확인 기한", terms.confirmation_deadline || "별도 기한 미정");
  if (terms.dispute_rule) fact($("trade-summary"), "분쟁 해결 기준", terms.dispute_rule === "assigned administrator proposal and bilateral consent" ? "배정 관리자 제안과 양측 동의" : terms.dispute_rule);
  if (data.wallet) fact($("trade-summary"), "현재 사용자 시연 지갑", won(data.wallet.available));
  if (data.balances) for (const part of ["D", "W"]) if (data.balances[part]) { const b = data.balances[part]; fact($("trade-summary"), `${part} 분리 잔액`, `보류 ${won(b.held)} · 예약 ${won((b.pay_reserved || 0) + (b.refund_reserved || 0))} · 지급 ${won(b.paid)} · 환불 ${won(b.refunded)}`); }
  fact($("trade-summary"), "이전 절차", data.transfer_status === "PARTIES_CONFIRMED" ? "당사자 증빙 확인 · 제공기관 이전 완료 아님" : data.transfer_status || "진행 기록 없음");
  clear($("trade-actions"));
  const actions = [["현재 계약 승인", "contract-consents", () => ({ revision: currentContract().revision, hash: currentContract().hash })], ["구매자 시연 예치", "deposit", () => ({})], ["구매자 수령 확인", "receipt", () => ({ contract_revision: currentContract().revision })], ["거래 종료 확인", "close", () => ({})]];
  for (const [label, route, payload] of actions) { const button = el("button", label, "secondary"); button.addEventListener("click", () => run("trade-action", () => mutateTrade(route, payload()))); $("trade-actions").append(button); }
  const refresh = el("button", "상태 새로고침", "secondary"); refresh.addEventListener("click", () => run("trade-refresh", reloadTrade)); $("trade-actions").append(refresh);
  if (state.actor === "admin") { const worker = el("button", "관리자 · 예약된 시연 작업 처리", "secondary"); worker.addEventListener("click", () => run("worker", async () => { await api("/worker/run", { method: "POST", body: {} }); await reloadTrade(); notice("서버 시연 처리기 실행 결과를 반영했습니다.", true); })); $("trade-actions").append(worker); }
  else $("trade-actions").append(el("p", "예약 후에는 시연 관리자로 로그인해 서버 작업 처리기를 실행하세요.", "subtle"));
  clear($("operations"));
  for (const op of data.operations || []) {
    const row = el("div", undefined, "operation-row"); row.append(el("span", `${op.component || ""} ${op.direction === "PAY" ? "지급" : "환불"} ${won(op.amount)} · ${STATUS[op.status] || "처리 상태 확인 필요"}`));
    const action = op.status === "FAILED" ? "retry" : op.status === "UNKNOWN" ? "reconcile" : null;
    if (action) { const button = el("button", action === "retry" ? "확정 실패 재시도" : "기존 결과 대사", "secondary"); button.addEventListener("click", () => run("operation", async () => { const id = op.operation_id || op.id; const body = { expected_version: state.trade.version }; await api(`/operations/${encodeURIComponent(id)}/${action}`, { method: "POST", key: mutationKey(id, action, {}, state.trade.version), body }); await reloadTrade(); })); row.append(button); }
    $("operations").append(row);
  }
  actionTemplate();
}
async function reloadTrade() { if (tradeId()) renderTrade(await api(`/trades/${encodeURIComponent(tradeId())}`)); }
async function mutateTrade(route, payload) {
  if (!tradeId()) throw { code: "TRADE_REQUIRED", message: "먼저 거래를 생성하거나 불러와 주세요." };
  const id = tradeId(); const expected = state.trade.version;
  const result = await api(`/trades/${encodeURIComponent(id)}/${route}`, { method: "POST", key: mutationKey(id, route, payload, expected), body: { ...payload, expected_version: expected } });
  await reloadTrade(); notice("요청 결과를 서버 상태에서 확인했습니다.", true); return result;
}
function actionTemplate() {
  const trade = state.trade || {}; const contract = currentContract();
  const route = $("trade-action").value;
  const area = $("action-fields"); clear(area);
  const submit = $("advanced-action").querySelector("button[type=submit]"); submit.disabled = false;
  const inputs = new Map();
  function field(key, label, options = {}) {
    const wrapper = el("label", label);
    const input = el(options.options ? "select" : options.type === "textarea" ? "textarea" : "input");
    input.id = `action-${key}`;
    wrapper.htmlFor = input.id;
    if (options.options) for (const option of options.options) {
      const node = el("option", option.label); node.value = option.value; input.append(node);
    }
    else if (options.type === "textarea") { input.rows = 4; input.maxLength = 2000; }
    else { input.type = options.type || "text"; input.maxLength = 500; if (input.type === "number") { input.min = "0"; input.max = "1000000000000"; input.step = "1"; } }
    if (options.value !== undefined) input.value = options.value;
    if (options.required) input.required = true;
    wrapper.append(input); area.append(wrapper); inputs.set(key, input); return input;
  }
  const value = (key) => inputs.get(key).value.trim();
  const amount = (key) => Number(value(key));
  const info = (text) => area.append(el("p", text, "meta-block"));
  const blocked = (text) => { info(text); submit.disabled = true; };
  const hint = (text) => { $("action-hint").textContent = text; };
  state.actionPayload = () => ({});
  const evidence = Array.isArray(trade.evidence) ? trade.evidence.at(-1) : trade.evidence;
  if (route === "evidence-bundles") {
    info("합성 이전 기록 1건을 시연 증빙 묶음으로 등록합니다. 실제 파일·진위 검사·자동 마스킹 자료가 아닙니다.");
    field("description", "기록 설명 · 선택", { value: "합성 시연 이전 기록" });
    const requiredIds = trade.review?.required_condition_ids || [];
    const currentConditions = state.reviews.get(scope())?.conditions || [];
    const conditionText = new Map(currentConditions.filter((condition) => condition.condition_id).map((condition) => [condition.condition_id, condition.text]));
    const checks = [];
    if (requiredIds.length) area.append(el("p", "각 조건을 실제로 확인한 경우에만 선택하세요. 미선택은 미충족으로 기록되며, 모든 필수 조건이 확인되기 전에는 W 지급을 예약할 수 없습니다.", "subtle"));
    for (const [index, conditionId] of requiredIds.entries()) {
      const label = el("label", undefined, "checkbox");
      const input = el("input"); input.type = "checkbox"; input.id = `condition-check-${index}`; input.checked = false;
      label.htmlFor = input.id;
      label.append(input, el("span", conditionText.get(conditionId) || `조건 ${index + 1} · ${conditionId}`));
      area.append(label); checks.push({ condition_id: conditionId, input });
    }
    state.actionPayload = () => ({ references: ["SYNTHETIC:demo-transfer-record"], description: value("description"), condition_checks: checks.map((check) => ({ condition_id: check.condition_id, confirmed: check.input.checked })) });
    hint("조건 충족 여부를 포함한 동일 묶음의 정확한 내용과 버전을 구매자와 판매자가 각각 확인해야 합니다.");
  } else if (route === "evidence-confirmations") {
    if (!evidence) blocked("현재 합성 증빙이 없습니다. 합성 증빙 묶음을 먼저 등록해 주세요.");
    else {
      info(`확인할 최신 증빙: ${evidence.evidence_id}\n${evidence.content?.description || "합성 시연 기록"}\n이 묶음의 정확한 버전과 내용을 확인합니다.`);
      for (const check of evidence.content?.condition_checks || []) {
        const clause = (state.reviews.get(scope())?.conditions || []).find((condition) => condition.condition_id === check.condition_id)?.text || check.condition_id;
        info(`${clause}\n기록된 충족 확인: ${check.confirmed === true ? "확인함" : "미확인"}`);
      }
    }
    state.actionPayload = () => ({ evidence_id: evidence.evidence_id, hash: evidence.hash });
    hint("제공기관의 이전 성공 확인과 구분되는 당사자 확인입니다.");
  } else if (route === "reviews") {
    const review = state.reviews.get(scope());
    field("review", "채택할 서버 검토 ID", { value: review?.review_id || "", required: true });
    if (review) info(`최근 답변: ${STATUS[review.classification] || "분류 미확인"}\n${readinessText(review.readiness)}`);
    state.actionPayload = () => ({ review_id: value("review") });
    hint("채택하면 계약의 새 버전을 만듭니다. 양측 계약 승인과 수령 확인을 다시 기록해야 합니다. 자료가 부족한 검토로 W 지급을 승인할 수 없습니다.");
  } else if (route === "settlement-proposals") {
    field("d-pay", "물건값 D · 판매자에게 지급할 금액 (원)", { type: "number", value: trade.balances?.D?.held ?? contract.D ?? 0 });
    field("d-refund", "물건값 D · 구매자에게 환불할 금액 (원)", { type: "number", value: 0 });
    field("w-pay", "보증 프리미엄 W · 판매자에게 지급할 금액 (원)", { type: "number", value: 0 });
    field("w-refund", "보증 프리미엄 W · 구매자에게 환불할 금액 (원)", { type: "number", value: 0 });
    const conditions = contract.terms?.return_conditions || [];
    field("reason", "환불을 포함할 때 적용할 계약 반환 조건", { options: conditions.map((condition) => ({ value: condition, label: condition === "mutual_agreement" ? "양측 합의에 따른 반환" : condition })) });
    state.actionPayload = () => {
      const allocations = [{ component: "D", pay_won: amount("d-pay"), refund_won: amount("d-refund") }, { component: "W", pay_won: amount("w-pay"), refund_won: amount("w-refund") }].filter((row) => row.pay_won || row.refund_won);
      const payload = { allocations };
      if (allocations.some((row) => row.refund_won)) payload.refund_reason = value("reason");
      if (allocations.some((row) => row.component === "W" && row.pay_won)) { payload.review_id = trade.review_id; payload.evidence_id = evidence?.evidence_id; }
      return payload;
    };
    hint("0원인 방향은 제안에서 제외합니다. D 지급은 수령·양측 정산안 승인이 필요하며, W 지급은 적용성 검토·같은 최신 증빙 확인도 필요합니다. W 환불에는 성공 이전 증빙을 요구하지 않습니다.");
  } else if (route === "settlement-consents" || route === "settlement-executions") {
    const proposals = trade.proposals || [];
    if (!proposals.length) blocked("정산안이 없습니다. 지급·환불 금액을 정해 새 정산안을 만들어 주세요.");
    else {
      const selector = field("proposal", "확인할 정산안", { value: proposals.at(-1).proposal_id, options: proposals.map((proposal, index) => ({ value: proposal.proposal_id, label: `정산안 ${index + 1} · ${proposal.allocations.map((row) => `${row.component} 지급 ${won(row.pay_won)} / 환불 ${won(row.refund_won)}`).join(" · ")}` })) });
      const summary = el("p", undefined, "meta-block"); area.append(summary);
      const selected = () => proposals.find((proposal) => proposal.proposal_id === selector.value);
      const update = () => { const proposal = selected(); summary.textContent = `같은 정산안에 승인한 당사자: ${(proposal.consents || []).map((consent) => consent.actor || consent.actor_id || consent.user_id).join(", ") || "아직 없음"}\n${proposal.allocations.map((row) => `${row.component}: 판매자 지급 ${won(row.pay_won)}, 구매자 환불 ${won(row.refund_won)}`).join("\n")}`; };
      selector.addEventListener("change", update); update();
      state.actionPayload = () => { const proposal = selected(); return route === "settlement-consents" ? { proposal_id: proposal.proposal_id, revision: proposal.revision, hash: proposal.hash } : { proposal_id: proposal.proposal_id, revision: proposal.revision }; };
    }
    hint(route === "settlement-consents" ? "위에 표시된 정확한 지급·환불 금액과 같은 정산안 버전에 현재 사용자로 승인합니다." : "서버가 전체 조건을 검증한 뒤 D/W를 원자적으로 예약합니다. 예약 후 관리자 시연 처리기로 실행하세요.");
  } else if (route === "transfer-events") {
    field("status", "당사자가 기록하는 진행 상태", { options: [{ value: "IN_PROGRESS", label: "이전 절차 진행 중" }, { value: "FAILED", label: "이전 절차 실패" }, { value: "ON_HOLD", label: "이전 절차 보류" }] });
    state.actionPayload = () => ({ status: value("status") });
    hint("수동 진행 기록만으로 이전 성공이나 지급 조건 충족을 인정하지 않습니다.");
  } else if (route === "disputes") {
    field("scope", "분쟁이 있는 금액 범위", { options: [{ value: "W", label: "보증 프리미엄 W" }, { value: "D", label: "물건값 D" }, { value: "BOTH", label: "D와 W 모두" }] });
    state.actionPayload = () => ({ scope: value("scope") }); hint("관련 금액의 분쟁이 해결되지 않으면 서버가 정산 예약을 제한합니다.");
  } else if (route === "resolutions") {
    const disputes = trade.disputes || [];
    if (!disputes.length) blocked("등록된 분쟁이 없습니다.");
    field("dispute", "해결안을 제안할 분쟁", { options: disputes.map((dispute, index) => ({ value: dispute.dispute_id, label: `분쟁 ${index + 1} · ${dispute.scope} · ${dispute.status}` })) });
    field("terms", "당사자가 검토할 해결 조건", { type: "textarea", required: true });
    state.actionPayload = () => ({ dispute_id: value("dispute"), terms: value("terms") }); hint("배정 관리자만 제안할 수 있습니다. 이후 양측이 같은 해결안을 승인해야 합니다.");
  } else if (route === "resolution-consents") {
    const resolutions = trade.resolutions || [];
    if (!resolutions.length) blocked("검토할 해결안이 없습니다.");
    else {
      const selector = field("resolution", "승인할 해결안", { value: resolutions.at(-1).resolution_id, options: resolutions.map((resolution, index) => ({ value: resolution.resolution_id, label: `해결안 ${index + 1} · ${resolution.content?.terms || resolution.terms || "조건 미확인"}` })) });
      state.actionPayload = () => { const resolution = resolutions.find((row) => row.resolution_id === selector.value); return { resolution_id: resolution.resolution_id, revision: resolution.revision, hash: resolution.hash }; };
    }
    hint("관리자가 당사자를 대신해 승인할 수 없습니다. 승인 후 필요한 새 정산안을 합의하세요.");
  } else if (route === "cancel-before-deposit") {
    info(`현재 계약의 예치 전 취소에 동의합니다. 물건값 D ${won(contract.D)}, 보증 프리미엄 W ${won(contract.W)}.`);
    state.actionPayload = () => ({ revision: contract.revision, hash: contract.hash }); hint("예치 전 양측이 같은 계약의 취소에 동의해야 합니다. 예치 후에는 지급·환불 정산안을 합의하세요.");
  }
}

$("login").addEventListener("click", () => run("login", async () => {
  if (state.selected) state.applicability.set(scope(), applicability());
  const data = await api("/session", { method: "POST", body: { actor: $("actor").value } });
  state.actor = data.actor; state.csrf = data.csrf_token; $("session-status").textContent = `${data.actor} 로그인됨`;
  await loadRuntime();
  $("job-status").textContent = "";
  const savedJob = state.selected && state.jobs.get(scope());
  if (savedJob) renderJob(savedJob, state.selected.item_id, state.actor, !JOB_TERMINAL.has(savedJob.status));
  if (state.selected) { restoreApplicability(state.selected.item_id); clear($("answer-meta")); clear($("evidence")); clear($("checklist")); $("classification").textContent = "검토 전"; await loadHistory(state.selected.item_id); }
  if (tradeId()) await reloadTrade();
  notice("로컬 시연 사용자로 로그인했습니다.", true);
}));
$("search-form").addEventListener("submit", (event) => { event.preventDefault(); run("search", async () => {
  const fee = $("max-fee").value; const data = await api("/search", { method: "POST", body: { query: $("query").value, provider: $("provider").value || null, max_fee: fee === "" ? null : Number(fee), transfer_required: $("transfer-required").checked } });
  renderItems(data.matches || [], data.unknown || []); clear($("pagination")); $("search-state").textContent = `확인된 조건 충족 ${(data.matches || []).length}개 · 미확인 ${(data.unknown || []).length}개`;
}); });
$("reset-items").addEventListener("click", () => run("items", () => loadItems()));
$("chat-form").addEventListener("submit", (event) => { event.preventDefault(); run("chat", async () => {
  const itemId = requireItem(); const actor = state.actor; const question = $("question").value.trim(); if (!question) return;
  const button = $("chat-form").querySelector("button"); button.disabled = true; const oldText = button.textContent; button.textContent = "근거 검색 · 답변 생성 중…";
  addMessage("user", question); $("question").value = "";
  try {
    const result = await api("/chat", { method: "POST", body: { item_id: itemId, question, applicability: applicability(), top_k: Number($("top-k").value), strategy: $("strategy").value } });
    if (state.selected?.item_id === itemId && state.actor === actor) { addMessage("assistant", result.answer || "확인할 수 있는 답변이 없습니다.", result.classification); renderEvidence(result); }
  } finally { button.disabled = false; button.textContent = oldText; }
}); });
$("checklist-button").addEventListener("click", () => run("checklist", async () => { const id = requireItem(); const actor = state.actor; const data = await api(`/items/${encodeURIComponent(id)}/checklist`, { method: "POST", body: { applicability: applicability() } }); if (state.selected?.item_id === id && state.actor === actor) renderChecklist(data); }));
$("prepare").addEventListener("click", () => run("prepare", async () => {
  const itemId = requireItem(); const actor = state.actor; $("job-status").textContent = "등록된 문서 준비 작업을 요청하고 있습니다…";
  const question = $("question").value.trim();
  const job = await api(`/items/${encodeURIComponent(itemId)}/index-jobs`, { method: "POST", body: { applicability: applicability(), ...(question ? { question } : {}) } });
  await watchJob(job, itemId, actor);
}));
$("trade-create").addEventListener("submit", (event) => { event.preventDefault(); run("create-trade", async () => {
  const reason = $("refund-condition").value.trim();
  const result = await api("/trades", { method: "POST", body: { item_id: requireItem(), buyer_id: "buyer", seller_id: "seller", D: Number($("trade-d").value), W: Number($("trade-w").value), terms: reason ? { return_conditions: [reason] } : {} } }); renderTrade(result); notice("합성 시연 거래를 생성했습니다. 양측이 같은 계약을 승인해 주세요.", true);
}); });
$("trade-load").addEventListener("submit", (event) => { event.preventDefault(); run("load-trade", async () => renderTrade(await api(`/trades/${encodeURIComponent($("trade-id").value.trim())}`))); });
$("trade-action").addEventListener("change", actionTemplate);
$("advanced-action").addEventListener("submit", (event) => { event.preventDefault(); run("trade-action", async () => {
  const payload = state.actionPayload();
  await mutateTrade($("trade-action").value, payload);
}); });
run("initial", async () => {
  await loadItems();
  try { const data = await api("/session"); state.actor = data.actor; state.csrf = data.csrf_token; $("actor").value = data.actor; $("session-status").textContent = `${data.actor} 로그인됨`; } catch (error) { if (error.code !== "AUTH_REQUIRED") throw error; }
  await loadRuntime();
});
