"""UI contracts and real Streamlit AppTest interactions using synthetic services."""
from __future__ import annotations

import importlib.util
from contextlib import ExitStack
from pathlib import Path
import tempfile
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
                    "readiness": {"status": "DOC_BLOCKED"}, "errors": [],
                    "progress": [{"stage": "retrieve", "status": "BLOCKED"}], "model": "MOCK"}

        def prepare(self, actor, item_id):
            return {"job_id": "synthetic-job", "status": "BLOCKED", "stage": "DOCUMENT_REVIEW",
                    "failure_code": "OWN_DOCUMENT_MISSING", "candidate_count": 0,
                    "body_count": 0, "chunk_count": 0}

        def job(self, actor, job_id):
            return self.prepare(actor, "demo-a")

    return SimpleNamespace(settings=SimpleNamespace(demo_mode=True), catalog=Catalog(), rag=Rag(),
                           trades=SimpleNamespace(), worker=SimpleNamespace())


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
                cleanup.callback(app.rag.vectors.client.close)
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
