"""UI contracts and real Streamlit AppTest interactions using synthetic services."""
from __future__ import annotations

import importlib.util
from contextlib import ExitStack
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest

from warranty.errors import ServiceError
from warranty.interfaces.streamlit_app import (
    applicability_inputs, cited_evidence, conversation_key, mutation_key,
    public_error, source_url,
)


def fake_application():
    """No file, model, HTTP or payment access; explicit synthetic fixture."""
    items = [{"item_id": identifier, "name": name, "provider": "DEMO",
              "monthly_fee_won": 3000, "transfer_allowed": None,
              "sale_status": "NOT_VERIFIED", "is_synthetic": True,
              "readiness": {"documents": "DOC_BLOCKED"}}
             for identifier, name in (("demo-a", "합성 보증 A"), ("demo-b", "합성 보증 B"))]

    class Catalog:
        def __init__(self):
            self.items = items
            self.queries = []

        def summary(self):
            return {"real_item_count": 0, "synthetic_item_count": 2,
                    "official_document_body_count": 0}

        def search(self, query=""):
            self.queries.append(query)
            return {"matches": [] if query else self.items,
                    "unknown": self.items if query else []}

        def get(self, item_id):
            return next(item for item in self.items if item["item_id"] == item_id)

    class Rag:
        def __init__(self):
            self.messages = {}
            self.requests = []
            self.checklist_requests = []
            self.enqueue_requests = []
            self.job_requests = []
            self.jobs = {}
            self.document_status = "DOC_BLOCKED"
            self.job_result = {"status": "BLOCKED", "stage": "DOC_BLOCKED",
                               "failure_code": "OWN_DOCUMENT_MISSING", "retryable": True,
                               "candidate_count": 0, "body_count": 0, "chunk_count": 0}

        def ask(self, actor, item_id, question, applicability=None, top_k=6):
            self.requests.append((actor, item_id, question, applicability, top_k))
            result = self.checklist(item_id, applicability)
            result["answer"] = f"{item_id}에 대한 모의 답변"
            result["review_id"] = f"review-{item_id}"
            self.messages.setdefault((actor, item_id), []).append({"question": question, "result": result})
            return result

        def history(self, actor, item_id):
            return self.messages.get((actor, item_id), [])

        def checklist(self, item_id, applicability=None, *, user_id=None, review_id=None):
            self.checklist_requests.append((item_id, user_id, applicability, review_id))
            return {"classification": "NEEDS_INFORMATION", "answer": "공식 문서가 필요합니다.",
                    "conditions": [], "missing_information": ["가입일·공식 문서"],
                    "conflicts": [], "cited_evidence_ids": [], "evidence": [],
                    "readiness": {"status": self.document_status}, "errors": [],
                    "progress": [{"stage": "retrieve", "status": "BLOCKED"}], "model": "MOCK"}

        def prepare(self, actor, item_id):
            raise AssertionError("Synchronous preparation must not run from the UI")

        def enqueue_prepare(self, actor, item_id, applicability=None, question=None):
            self.enqueue_requests.append((actor, item_id, applicability, question))
            job_id = f"synthetic-job-{len(self.enqueue_requests)}"
            result = {"job_id": job_id, "item_id": item_id, "requester": actor,
                      "status": "QUEUED", "stage": "QUEUED", "retryable": False,
                      "candidate_count": 0, "body_count": 0, "chunk_count": 0,
                      "updated_at": "2026-10-22T01:00:00+00:00"}
            self.jobs[job_id] = result
            return result

        def job(self, actor, job_id):
            self.job_requests.append((actor, job_id))
            if self.jobs[job_id]["requester"] != actor:
                raise ServiceError("FORBIDDEN")
            return {**self.jobs[job_id], **self.job_result}

    app = SimpleNamespace(settings=SimpleNamespace(demo_mode=True), catalog=Catalog(), rag=Rag(),
                          trades=SimpleNamespace(), worker=SimpleNamespace(), public_status={
                              "chat": {"provider": "mock", "configured": True, "ready": True, "is_mock": True},
                              "embedding": {"provider": "mock", "configured": True, "ready": True, "is_mock": True},
                              "collection": {"enabled": False, "network_enabled": False, "registered_target_count": 0},
                              "external_processing_approved": False, "question_processing_approved": False,
                              "document_worker_enabled": False})
    app.runtime_status = lambda: app.public_status
    app.public_live_status = {"enabled": False, "ready": False, "purposes": ["evaluation", "verification", "demo"],
                              "budget_confirmed": False}
    app.live_status = lambda: app.public_live_status
    return app


def queued_application(directory):
    """Real local persistence and queue with a gated synthetic progress handler."""
    from warranty.application import build_application
    from warranty.config import Settings
    from warranty.jobs import DocumentJobQueue
    base = Path(directory)
    app = build_application(Settings(base / "data", base / "config"))
    existing = getattr(app, "document_jobs", None)
    if existing is not None:
        existing.stop()
    app.test_started, app.test_release = threading.Event(), threading.Event()
    def handler(request, progress):
        progress("FETCHING", candidate_count=2)
        app.test_started.set()
        if not app.test_release.wait(timeout=5):
            return {"status": "FAILED", "failure_code": "JOB_INTERRUPTED"}
        progress("INDEXING", body_count=1, chunk_count=2)
        return {"status": "COMPLETED", "stage": "INDEX_READY", "body_count": 1, "chunk_count": 2}
    app.document_jobs = DocumentJobQueue(app.db, app.catalog, handler, auto_start=False)
    app.rag.document_jobs = app.document_jobs
    return app


class StreamlitContractTests(unittest.TestCase):
    def test_conversation_keys_separate_users_and_items(self):
        keys = {conversation_key("buyer", "a:b"), conversation_key("buyer:a", "b"),
                conversation_key("seller", "a:b"), conversation_key("buyer", "b")}
        self.assertEqual(len(keys), 4)

    def test_only_actual_known_citations_are_presented(self):
        result = {"cited_evidence_ids": ["used", "missing", "used"],
                  "evidence": [{"evidence_id": "unused", "title": "검색만 된 문서"},
                               {"evidence_id": "used", "title": "실제 인용 문서"}]}
        self.assertEqual([row["title"] for row in cited_evidence(result)], ["실제 인용 문서"])
        self.assertEqual(cited_evidence({"evidence": result["evidence"]}), [])

    def test_errors_never_reflect_exception_payload(self):
        secret = "secret-prompt C:/private/token-value"
        for exc in (RuntimeError(secret), ServiceError("KEY_MISSING", secret),
                    {"code": "MODEL_OUTPUT_INVALID", "message": secret, "retryable": True}):
            response = public_error(exc)
            self.assertNotIn(secret, str(response))
            self.assertIn(response["code"], ("INTERNAL_ERROR", "KEY_MISSING", "MODEL_OUTPUT_INVALID"))

    def test_unknown_applicability_is_not_filled(self):
        self.assertEqual(applicability_inputs({"country": "", "enrollment_date": None}), {})
        self.assertEqual(applicability_inputs({"country": " KR ", "unexpected": "value"}), {"country": "KR"})

    def test_output_links_reject_executable_and_embedded_content(self):
        for url in ("javascript:alert(1)", "data:text/html,secret", "http://source.example/doc",
                    "https://user:secret@source.example", "https://source.example:8443/doc", "https://source.example/\nsecret"):
            self.assertIsNone(source_url(url))
        self.assertEqual(source_url("https://source.example/doc#section"), "https://source.example/doc#section")

    def test_idempotency_key_is_retained_for_same_intent(self):
        state = {}
        args = ("buyer", "trade-a", "reserve", {"proposal_id": "p1", "revision": 1}, 3)
        first = mutation_key(state, *args)
        self.assertEqual(first, mutation_key(state, *args))
        self.assertNotEqual(first, mutation_key(state, "seller", *args[1:]))
        self.assertNotEqual(first, mutation_key(state, *args[:-1], 4))


@unittest.skipUnless(importlib.util.find_spec("streamlit"), "optional Streamlit dependency is unavailable")
class StreamlitInteractionTests(unittest.TestCase):
    def setUp(self):
        from streamlit import config
        self.old_address = config.get_option("server.address")
        config.set_option("server.address", "127.0.0.1")
        from streamlit.testing.v1 import AppTest
        script = f"""
import sys
sys.path.insert(0, {str(Path(__file__).parent.resolve())!r})
import streamlit as st
from test_streamlit_contract import fake_application
from warranty.interfaces.streamlit_app import run
if '_test_app' not in st.session_state:
    st.session_state['_test_app'] = fake_application()
run(st.session_state['_test_app'], st)
"""
        self.at = AppTest.from_string(script, default_timeout=15).run()

    def tearDown(self):
        from streamlit import config
        config.set_option("server.address", self.old_address)

    def button(self, label):
        return next(button for button in self.at.button if button.label == label)

    def input(self, label, kind="text_input"):
        return next(widget for widget in getattr(self.at, kind) if widget.label == label)

    def test_first_screen_and_unknown_search(self):
        self.assertFalse(self.at.exception)
        self.assertEqual([metric.value for metric in self.at.metric], ["0", "2", "0"])
        self.input("원하는 조건").input("양도 가능한 5천원 이하")
        self.button("조건 탐색").click().run()
        self.assertFalse(self.at.exception)
        self.assertTrue(any("0개" in item.value for item in self.at.info))
        app = self.at.session_state["_test_app"]
        self.assertEqual(app.catalog.queries[-1], "양도 가능한 5천원 이하")
        self.assertTrue(any("미확인 후보" in str(item.value) for item in self.at.info))

    def test_item_and_user_history_isolation_and_applicability(self):
        self.input("가입 국가·지역").input("KR")
        self.input("이 항목에 질문하기", "text_area").input("A 전용 질문")
        self.button("근거와 함께 질문").click().run()
        self.assertFalse(self.at.exception)
        self.assertEqual(len(self.at.chat_message), 2)
        app = self.at.session_state["_test_app"]
        self.assertEqual(app.rag.requests[-1][3], {"country": "KR"})
        selector = next(widget for widget in self.at.selectbox if widget.label == "상세·채팅할 항목")
        selector.select("demo-b").run()
        self.assertEqual(len(self.at.chat_message), 0)
        selector = next(widget for widget in self.at.selectbox if widget.label == "상세·채팅할 항목")
        selector.select("demo-a").run()
        self.assertEqual(len(self.at.chat_message), 2)
        actor = next(widget for widget in self.at.selectbox if widget.label == "현재 시연 사용자")
        actor.select("seller").run()
        self.assertEqual(len(self.at.chat_message), 0)
        self.assertEqual(app.rag.checklist_requests[-1][1], "seller")

    def test_document_job_and_no_citation_message(self):
        self.button("문서 준비 요청").click().run()
        self.assertFalse(self.at.exception)
        self.assertTrue(any("OWN_DOCUMENT_MISSING" in error.value for error in self.at.error))
        self.assertTrue(any("확인 가능한 근거를 찾지 못했습니다" in info.value for info in self.at.info))
        self.assertEqual(len(self.at.get("file_uploader")), 0)

    def test_enqueue_returns_job_and_poll_progress_uses_same_requester(self):
        app = self.at.session_state["_test_app"]
        app.rag.job_result.update(status="RUNNING", stage="FETCHING", failure_code=None,
                                  retryable=False, candidate_count=4, body_count=1, chunk_count=0)
        self.input("가입 국가·지역").input("KR")
        self.input("문서 탐색 질문 (선택)").input("회선 이전 확인")
        self.button("문서 준비 요청").click().run()
        self.assertFalse(self.at.exception)
        self.assertEqual(app.rag.enqueue_requests, [("buyer", "demo-a", {"country": "KR"}, "회선 이전 확인")])
        self.assertEqual(app.rag.job_requests[-1], ("buyer", "synthetic-job-1"))
        values = " ".join(str(widget.value) for widget in self.at.text)
        self.assertIn("synthetic-job-1", values)
        self.assertIn("허용된 본문 수집", values)
        self.assertIn("후보 4 · 본문 1 · 청크 0", values)
        self.assertTrue(any("2초마다" in str(widget.value) for widget in self.at.caption))
        app.rag.job_result.update(status="COMPLETED", stage="INDEX_READY", body_count=2, chunk_count=5)
        self.button("작업 상태 새로고침").click().run()
        self.assertFalse(self.at.exception)
        self.assertEqual(len(app.rag.enqueue_requests), 1)
        self.assertTrue(any("문서 준비 작업: 완료" in widget.value for widget in self.at.success))
        self.assertIn("후보 4 · 본문 2 · 청크 5", " ".join(str(widget.value) for widget in self.at.text))

    def test_job_retry_is_new_enqueue_and_job_display_is_user_item_scoped(self):
        app = self.at.session_state["_test_app"]
        self.button("문서 준비 요청").click().run()
        self.button("문서 준비 다시 요청").click().run()
        self.assertEqual(len(app.rag.enqueue_requests), 2)
        values = " ".join(str(widget.value) for widget in self.at.text)
        self.assertIn("synthetic-job-2", values)
        selector = next(widget for widget in self.at.selectbox if widget.label == "상세·채팅할 항목")
        selector.select("demo-b").run()
        self.assertFalse(any("synthetic-job-2" in str(widget.value) for widget in self.at.text))
        next(widget for widget in self.at.selectbox if widget.label == "상세·채팅할 항목").select("demo-a").run()
        next(widget for widget in self.at.selectbox if widget.label == "현재 시연 사용자").select("seller").run()
        self.assertFalse(any("synthetic-job-2" in str(widget.value) for widget in self.at.text))
        self.assertFalse(self.at.exception)

    def test_document_failure_codes_and_server_retryability_are_visible(self):
        app = self.at.session_state["_test_app"]
        for code in ("SOURCE_BLOCKED", "OWN_DOCUMENT_MISSING", "FETCH_FAILED", "PARSE_FAILED", "INDEX_EMPTY"):
            with self.subTest(code=code):
                app.rag.job_result.update(status="FAILED", failure_code=code, retryable=False)
                self.button("문서 준비 요청").click().run()
                self.assertFalse(self.at.exception)
                self.assertTrue(any(code in widget.value for widget in self.at.error))
                self.assertFalse(any(widget.label == "문서 준비 다시 요청" for widget in self.at.button))

    def test_public_model_status_and_live_document_readiness_are_separate(self):
        app = self.at.session_state["_test_app"]
        app.public_status["chat"] = {"provider": "openai", "configured": False, "ready": False,
                                     "is_mock": False, "api_key": "never-display-test-key"}
        app.rag.document_status = "INDEX_READY"
        self.at.run()
        self.assertFalse(self.at.exception)
        self.assertTrue(any("대화 모델: OpenAI · 서버 설정 미완료" in widget.value for widget in self.at.warning))
        self.assertTrue(any("연결된 문서 색인" in widget.value for widget in self.at.success))
        self.assertFalse(any("DOC_BLOCKED" in widget.value for widget in self.at.warning))
        displayed = " ".join(str(widget.value) for widget in list(self.at.text) + list(self.at.warning))
        self.assertNotIn("never-display-test-key", displayed)
        self.assertIn("임베딩 모델: MOCK", displayed)

    def test_gms_refresh_and_forced_ui_actions_make_zero_adapter_http_calls(self):
        import httpx
        from warranty.gms import GMSStructuredModel
        app = self.at.session_state["_test_app"]
        http_calls, live_entries, service_attempts = [], [], []
        def transport(request):
            http_calls.append(request)
            raise AssertionError("The ordinary UI must not contact GMS")
        model = GMSStructuredModel(SimpleNamespace(remote_models=True), lambda *args: True,
            transport=httpx.MockTransport(transport), route="chat/completions", max_tokens=128,
            env={"GMS_API_KEY": "synthetic-gms-key-never-real", "GMS_CHAT_MODEL": "gpt-4.1-mini",
                 "GMS_BASE_URL": "https://gms.ssafy.io/gmsapi/api.openai.com/v1"})
        self.addCleanup(model.close)
        def model_action(*args, **kwargs):
            service_attempts.append((args, kwargs))
            return model.generate("query", "synthetic question")
        app.rag.ask = model_action
        app.rag.enqueue_prepare = model_action
        app.live_execution = lambda *args: live_entries.append(args)
        app.public_status["chat"] = {"provider": "gms", "configured": True, "ready": True, "is_mock": False,
                                     "api_key": "never-display-runtime-key"}
        app.public_live_status.update(enabled=True, ready=True, budget_confirmed=True,
            purposes=["evaluation", "verification", "final_verification", "demo", "embedding_preparation",
                      "never-display-private-purpose"], api_key="never-display-live-key")
        self.at.run()
        self.at.run()
        self.assertFalse(self.at.exception)
        self.assertTrue(self.button("문서 준비 요청").disabled)
        self.assertTrue(self.button("근거와 함께 질문").disabled)
        self.button("문서 준비 요청").click().run()
        self.input("이 항목에 질문하기", "text_area").input("합성 범위 확인")
        self.button("근거와 함께 질문").click().run()
        self.input("원하는 조건").input("양도 조건 확인")
        self.button("조건 탐색").click().run()
        self.assertEqual(app.catalog.queries[-1], "양도 조건 확인")
        self.assertEqual(service_attempts, [])
        self.assertEqual(http_calls, [])
        self.assertEqual(live_entries, [])
        displayed = " ".join(str(widget.value) for kind in ("text", "caption", "info", "warning", "error")
                             for widget in getattr(self.at, kind))
        self.assertIn("대화 모델: GMS", displayed)
        self.assertIn("명시한 CLI", displayed)
        self.assertIn("70,000크레딧 사용 계획", displayed)
        self.assertIn("실제 모델 호출 성공", displayed)
        for private in ("never-display-runtime-key", "never-display-live-key", "never-display-private-purpose"):
            self.assertNotIn(private, displayed)
        self.assertFalse(self.at.exception)

    def test_gms_missing_budget_and_key_status_do_not_claim_connection_success(self):
        app = self.at.session_state["_test_app"]
        app.public_status["embedding"] = {"provider": "gms", "configured": False, "ready": False,
                                          "is_mock": False}
        app.public_live_status.update(enabled=True, ready=False, budget_confirmed=False)
        self.at.run()
        self.assertTrue(any("임베딩 모델: GMS · 서버 설정 미완료" in item.value for item in self.at.warning))
        self.assertTrue(any("가격·예산 범위가 확정되지 않아" in item.value for item in self.at.warning))
        self.assertTrue(self.button("문서 준비 요청").disabled)
        self.assertEqual(app.rag.enqueue_requests, [])
        self.assertEqual(app.rag.requests, [])
        self.assertFalse(self.at.exception)

    def test_existing_job_retry_blocks_gms_and_mock_preparation_remains_available(self):
        app = self.at.session_state["_test_app"]
        self.button("문서 준비 요청").click().run()
        self.assertEqual(len(app.rag.enqueue_requests), 1)
        app.public_status["embedding"] = {"provider": "gms", "configured": True, "ready": True,
                                          "is_mock": False}
        self.at.run()
        self.assertTrue(self.button("문서 준비 다시 요청").disabled)
        self.button("문서 준비 다시 요청").click().run()
        self.assertEqual(len(app.rag.enqueue_requests), 1)
        app.public_status["embedding"] = {"provider": "mock", "configured": True, "ready": True,
                                          "is_mock": True}
        self.at.run()
        self.assertFalse(self.button("문서 준비 요청").disabled)
        self.button("문서 준비 다시 요청").click().run()
        self.assertEqual(len(app.rag.enqueue_requests), 2)
        self.assertFalse(self.at.exception)

    def test_foreign_job_projection_is_rejected_and_not_retained(self):
        app = self.at.session_state["_test_app"]
        app.rag.job_result.update(requester="seller", status="RUNNING", question="foreign-private-question")
        self.button("문서 준비 요청").click().run()
        self.assertFalse(self.at.exception)
        self.assertTrue(any("FORBIDDEN" in widget.value for widget in self.at.error))
        self.assertNotIn(f"job:{conversation_key('buyer', 'demo-a')}", self.at.session_state)
        self.assertFalse(any("foreign-private-question" in str(widget.value) for widget in self.at.text))

    def test_service_failure_is_safe_in_actual_ui(self):
        app = self.at.session_state["_test_app"]
        def fail(*args, **kwargs):
            raise ServiceError("KEY_MISSING", "private-token-value C:/private/prompt.json")
        app.rag.ask = fail
        self.input("이 항목에 질문하기", "text_area").input("실패 안내 확인")
        self.button("근거와 함께 질문").click().run()
        self.assertFalse(self.at.exception)
        self.assertTrue(any("KEY_MISSING" in error.value for error in self.at.error))
        displayed = " ".join(str(item.value) for item in list(self.at.error) + list(self.at.text))
        self.assertNotIn("private-token-value", displayed)
        self.assertNotIn("prompt.json", displayed)

    def test_cited_synthetic_source_is_labeled_and_untrusted_answer_is_text(self):
        app = self.at.session_state["_test_app"]
        result = app.rag.checklist("demo-a")
        result.update({"answer": "<script>alert(1)</script> ![remote](https://example.test/pixel)",
                       "cited_evidence_ids": ["synthetic-used"],
                       "evidence": [{"evidence_id": "synthetic-used", "title": "합성 근거",
                                     "source_type": "synthetic_document"},
                                    {"evidence_id": "official-unused", "title": "미인용 원문",
                                     "source_type": "official_document"}]})
        app.rag.checklist = lambda *args, **kwargs: result
        self.at.run()
        text_values = " ".join(str(widget.value) for widget in self.at.text)
        self.assertIn("유형: 합성 시연 문서", text_values)
        self.assertIn("<script>alert(1)</script>", text_values)
        self.assertNotIn("미인용 원문", text_values)
        self.assertFalse(any("alert(1)" in str(widget.value) for widget in self.at.markdown))

    def test_citation_keeps_server_number_when_uncited_first_source_is_omitted(self):
        app = self.at.session_state["_test_app"]
        result = app.rag.checklist("demo-a")
        result.update({"answer": "확인이 필요한 합성 조건입니다. [2]",
                       "cited_evidence_ids": ["used"],
                       "evidence": [{"evidence_id": "unused", "number": 1, "title": "미인용 합성 문서"},
                                    {"evidence_id": "used", "number": 2, "title": "인용한 합성 문서",
                                     "source_type": "synthetic_document"}]})
        app.rag.checklist = lambda *args, **kwargs: result
        self.at.run()
        rendered = " ".join(str(widget.value) for widget in self.at.text)
        self.assertIn("[2] 인용한 합성 문서", rendered)
        self.assertNotIn("[1] 인용한 합성 문서", rendered)
        self.assertNotIn("미인용 합성 문서", rendered)
        self.assertFalse(self.at.exception)

    def test_demo_ui_rejects_nonlocal_binding(self):
        from streamlit import config
        config.set_option("server.address", "0.0.0.0")
        self.at.run()
        self.assertFalse(self.at.exception)
        self.assertTrue(any("localhost" in error.value for error in self.at.error))
        self.assertFalse(self.at.selectbox)


@unittest.skipUnless(importlib.util.find_spec("streamlit") and importlib.util.find_spec("chromadb"),
                     "optional local UI/RAG dependencies are unavailable")
class StreamlitSharedServiceTests(unittest.TestCase):
    def test_persistent_queue_progress_is_polled_without_ui_running_worker(self):
        from streamlit import config
        from streamlit.testing.v1 import AppTest
        old_address = config.get_option("server.address")
        config.set_option("server.address", "127.0.0.1")
        try:
            with ExitStack() as cleanup:
                temporary = cleanup.enter_context(tempfile.TemporaryDirectory(prefix="warranty-streamlit-queue-"))
                script = f"""
import sys
sys.path.insert(0, {str(Path(__file__).parent.resolve())!r})
import streamlit as st
from test_streamlit_contract import queued_application
from warranty.interfaces.streamlit_app import run
if '_queue_test_app' not in st.session_state:
    st.session_state['_queue_test_app'] = queued_application({temporary!r})
run(st.session_state['_queue_test_app'], st)
"""
                at = AppTest.from_string(script, default_timeout=20).run()
                app = at.session_state["_queue_test_app"]
                cleanup.callback(app.close)
                cleanup.callback(app.document_jobs.stop)
                def button(label):
                    return next(widget for widget in at.button if widget.label == label)
                button("문서 준비 요청").click().run()
                self.assertFalse(at.exception)
                self.assertFalse(at.error)
                item_id = at.session_state["warranty_selected_item"]
                job = at.session_state[f"job:{conversation_key('buyer', item_id)}"]
                self.assertEqual(job["status"], "QUEUED")
                self.assertFalse(app.test_started.is_set())
                worker = threading.Thread(target=app.document_jobs.run_pending, kwargs={"limit": 1})
                worker.start()
                try:
                    self.assertTrue(app.test_started.wait(timeout=2))
                    button("작업 상태 새로고침").click().run()
                    self.assertFalse(at.exception)
                    self.assertIn("허용된 본문 수집", " ".join(str(widget.value) for widget in at.text))
                    job = at.session_state[f"job:{conversation_key('buyer', item_id)}"]
                    self.assertEqual(job["status"], "RUNNING")
                    app.test_release.set()
                    worker.join(timeout=2)
                    self.assertFalse(worker.is_alive())
                    button("작업 상태 새로고침").click().run()
                    self.assertFalse(at.exception)
                    self.assertFalse(at.error)
                    job = at.session_state[f"job:{conversation_key('buyer', item_id)}"]
                    self.assertEqual(job["status"], "COMPLETED")
                    self.assertEqual(job["chunk_count"], 2)
                    self.assertTrue(any("문서 준비 작업: 완료" in widget.value for widget in at.success))
                finally:
                    app.test_release.set()
                    worker.join(timeout=5)
        finally:
            config.set_option("server.address", old_address)

    def test_real_shared_facade_d_only_flow(self):
        from streamlit import config
        from streamlit.testing.v1 import AppTest
        old_address = config.get_option("server.address")
        config.set_option("server.address", "127.0.0.1")
        try:
            with ExitStack() as cleanup:
                temporary = cleanup.enter_context(tempfile.TemporaryDirectory(prefix="warranty-streamlit-"))
                base = Path(temporary)
                script = f"""
import os
from pathlib import Path
import streamlit as st
from warranty.application import build_application
from warranty.config import Settings
from warranty.interfaces.streamlit_app import run
if '_real_test_app' not in st.session_state:
    st.session_state['_real_test_app'] = build_application(Settings(Path({str(base / 'data')!r}), Path({str(base / 'config')!r})))
run(st.session_state['_real_test_app'], st)
"""
                at = AppTest.from_string(script, default_timeout=30).run()
                app = at.session_state["_real_test_app"]
                cleanup.callback(app.close)
                self.assertFalse(at.exception)
                self.assertFalse(at.error)
                def button(label):
                    return next(widget for widget in at.button if widget.label == label)
                def number(label):
                    return next(widget for widget in at.number_input if widget.label == label)
                def role(value):
                    next(widget for widget in at.selectbox if widget.label == "현재 시연 사용자").select(value).run()
                number("물건 대금 D (시연 원)").set_value(1000)
                number("보증 프리미엄 W (시연 원)").set_value(0)
                button("선택 항목의 새 시연 계약 만들기").click().run()
                self.assertFalse(at.error)
                button("현재 계약 revision/hash에 동의").click().run()
                role("seller")
                button("현재 계약 revision/hash에 동의").click().run()
                role("buyer")
                button("D+W 전액 시연 예치 요청").click().run()
                button("구매자 · 현재 계약의 물건 수령 확인").click().run()
                number("D 지급액").set_value(1000)
                button("새 정산안 제출").click().run()
                button("표시된 배분/revision/hash에 동의").click().run()
                role("seller")
                button("표시된 배분/revision/hash에 동의").click().run()
                button("현재 정산안 예약 요청").click().run()
                self.assertFalse(any(widget.label == "예약된 로컬 시연 정산 처리" for widget in at.button))
                role("admin")
                button("예약된 로컬 시연 정산 처리").click().run()
                role("buyer")
                button("정산 완료 조건 확인 후 거래 종료 요청").click().run()
                self.assertFalse(at.exception)
                self.assertFalse(at.error)
                app = at.session_state["_real_test_app"]
                item_id = at.session_state["warranty_selected_item"]
                trade = app.trades.get("buyer", at.session_state[f"trade_selection:{item_id}"])
                self.assertEqual(trade["status"], "CLOSED")
                self.assertEqual(trade["balances"]["D"]["paid"], 1000)
                self.assertEqual(len(trade["operations"]), 1)
                self.assertEqual(trade["operations"][0]["component"], "D")
                # A formerly richer projection must not cause revoked prose to
                # reappear. Only the public eligibility metadata is rendered.
                projected = dict(trade)
                projected["review"] = {"review_id": "public-review", "revision": 2,
                                       "classification": "CONDITIONAL", "applicability_ok": True,
                                       "conditions_ok": False, "evidence_ids": ["public-evidence-id"],
                                       "document_version_ids": ["public-document-version"],
                                       "required_condition_ids": ["public-condition-id"],
                                       "snapshot": {"answer": "revoked-private-excerpt"}}
                app.trades.get = lambda actor, trade_id: projected
                at.run()
                rendered = " ".join(str(widget.value) for widget in at.text)
                self.assertIn("public-evidence-id", rendered)
                self.assertIn("public-document-version", rendered)
                self.assertNotIn("revoked-private-excerpt", rendered)
                self.assertFalse(at.exception)
                checkbox = next(widget for widget in at.checkbox if "public-condition-id" in widget.label)
                self.assertFalse(checkbox.value)
                captured = []
                def capture_mutation(actor, trade_id, action, payload, key, version):
                    captured.append((action, payload))
                    return projected
                app.trades.mutate = capture_mutation
                button("합성 증빙 묶음 생성").click().run()
                self.assertEqual(captured[-1], ("evidence", {
                    "references": ["SYNTHETIC:transfer-demo"],
                    "condition_checks": [{"condition_id": "public-condition-id", "confirmed": False}]}))
                next(widget for widget in at.checkbox if "public-condition-id" in widget.label).check().run()
                button("합성 증빙 묶음 생성").click().run()
                self.assertEqual(captured[-1][1]["condition_checks"], [
                    {"condition_id": "public-condition-id", "confirmed": True}])
                role("seller")
                self.assertFalse(next(widget for widget in at.checkbox if "public-condition-id" in widget.label).value)
                self.assertFalse(at.exception)
        finally:
            config.set_option("server.address", old_address)


if __name__ == "__main__":
    unittest.main()
