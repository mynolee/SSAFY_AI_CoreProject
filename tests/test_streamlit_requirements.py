"""Required checklist provenance and editable contract flows through AppTest."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest

from warranty.interfaces.streamlit_app import conversation_key, public_error


class StreamlitActionErrorTests(unittest.TestCase):
    def test_domain_rejections_remain_actionable_without_raw_exception_details(self):
        for code in ("FUNDED_CONTRACT_IMMUTABLE", "ACTIVE_SETTLEMENT", "TRADE_TERMINAL",
                     "W_CURRENT_REVIEW_REQUIRED", "RETRY_REQUIRES_FAILED", "INVALID_AMOUNT"):
            with self.subTest(code=code):
                result = public_error({"code": code, "message": "private-token /private/prompt"})
                self.assertEqual(result["code"], code)
                self.assertNotIn("private-token", result["message"])
                self.assertNotIn("/private/prompt", result["message"])


@unittest.skipUnless(importlib.util.find_spec("streamlit"), "optional Streamlit dependency is unavailable")
class StreamlitChecklistRequirementsTests(unittest.TestCase):
    def setUp(self):
        from streamlit import config
        from streamlit.testing.v1 import AppTest
        self.old_address = config.get_option("server.address")
        config.set_option("server.address", "127.0.0.1")
        self.at = AppTest.from_string(f"""
import sys
sys.path.insert(0, {str(Path(__file__).parent.resolve())!r})
import streamlit as st
from test_streamlit_contract import fake_application
from warranty.interfaces.streamlit_app import run
if '_test_app' not in st.session_state:
    st.session_state['_test_app'] = fake_application()
run(st.session_state['_test_app'], st)
""", default_timeout=15).run()

    def tearDown(self):
        from streamlit import config
        config.set_option("server.address", self.old_address)

    def test_checklist_shows_server_applied_inputs_and_document_versions(self):
        app = self.at.session_state["_test_app"]
        result = app.rag.checklist("demo-a")
        result.update(review_id="current-review", revision="active-index-7",
                      applicability={"country": "KR", "enrollment_date": "2025-01-02"},
                      documents=[{"title": "합성 검토 약관", "version_id": "document-version-9",
                                  "role": "own_document"}])
        app.rag.checklist = lambda *args, **kwargs: result
        self.at.run()
        self.assertFalse(self.at.exception)
        rendered = " ".join(str(item.value) for item in self.at.text)
        for expected in ("current-review", "active-index-7", "가입 국가·지역: KR",
                         "가입일 (YYYY-MM-DD): 2025-01-02", "document-version-9"):
            self.assertIn(expected, rendered)
        self.assertTrue(any("확인 가능한 근거를 찾지 못했습니다" in item.value for item in self.at.info))

    def test_changed_applicability_cannot_keep_last_chat_as_adoption_candidate(self):
        app = self.at.session_state["_test_app"]
        base = app.rag.checklist("demo-a")
        old_review = {**base, "review_id": "old-kr-review", "applicability": {"country": "KR"}}
        app.rag.messages[("buyer", "demo-a")] = [{"question": "이전 질문", "result": old_review}]
        app.rag.checklist = lambda item_id, applicability=None, **kwargs: {
            **base, "applicability": applicability or {},
            "review_id": "old-kr-review" if applicability == {"country": "KR"} else None}
        country = lambda: next(widget for widget in self.at.text_input if widget.label == "가입 국가·지역")
        country().input("KR").run()
        key = f"latest_review:{conversation_key('buyer', 'demo-a')}"
        self.assertEqual(self.at.session_state[key]["review_id"], "old-kr-review")
        country().input("US").run()
        self.assertFalse(self.at.exception)
        self.assertNotIn(key, self.at.session_state)
        self.assertEqual(len(self.at.chat_message), 2)
        self.assertTrue(any("현재 조건에 맞는 저장 검토 없음" in item.value for item in self.at.text))


@unittest.skipUnless(importlib.util.find_spec("streamlit") and importlib.util.find_spec("chromadb"),
                     "optional local UI/RAG dependencies are unavailable")
class StreamlitContractRevisionRequirementsTests(unittest.TestCase):
    def test_ui_revises_exact_contract_and_preserves_funded_price_gate(self):
        from streamlit import config
        from streamlit.testing.v1 import AppTest
        old_address = config.get_option("server.address")
        config.set_option("server.address", "127.0.0.1")
        app = None
        try:
            with tempfile.TemporaryDirectory(prefix="warranty-streamlit-revision-") as directory:
                base = Path(directory)
                at = AppTest.from_string(f"""
from pathlib import Path
import streamlit as st
from warranty.application import build_application
from warranty.config import Settings
from warranty.interfaces.streamlit_app import run
if '_test_app' not in st.session_state:
    st.session_state['_test_app'] = build_application(Settings(Path({str(base / 'data')!r}), Path({str(base / 'config')!r})))
run(st.session_state['_test_app'], st)
""", default_timeout=30).run()
                app = at.session_state["_test_app"]
                def widget(kind, label):
                    return next(item for item in getattr(at, kind) if item.label == label)
                def click(label):
                    widget("button", label).click().run()
                def role(value):
                    widget("selectbox", "현재 시연 사용자").select(value).run()
                widget("number_input", "물건 대금 D (시연 원)").set_value(1000)
                widget("number_input", "보증 프리미엄 W (시연 원)").set_value(0)
                click("선택 항목의 새 시연 계약 만들기")
                item_id = at.session_state["warranty_selected_item"]
                trade_id = at.session_state[f"trade_selection:{item_id}"]
                widget("number_input", "수정 물건 대금 D (시연 원)").set_value(2000)
                widget("text_area", "반환 조건 (한 줄에 하나)").input("합성 이전 실패\n  양측 반환 합의  ")
                widget("text_area", "증빙 확인 기준").input("동일 합성 증빙을 양측이 확인")
                widget("text_input", "증빙 확인 기한 (선택)").input("2026-11-01")
                widget("text_area", "분쟁 처리 규칙").input("관리자 제안 후 양측 동의")
                click("수정 조건으로 새 계약 revision 요청")
                self.assertFalse(at.exception)
                self.assertFalse(at.error)
                trade = app.trades.get("buyer", trade_id)
                self.assertEqual(trade["contract"]["revision"], 2)
                self.assertEqual(trade["contract"]["D"], 2000)
                self.assertEqual(trade["contract"]["terms"]["return_conditions"], ["합성 이전 실패", "양측 반환 합의"])
                self.assertEqual(trade["contract"]["terms"]["confirmation_deadline"], "2026-11-01")
                click("현재 계약 revision/hash에 동의")
                role("seller")
                click("현재 계약 revision/hash에 동의")
                role("buyer")
                click("D+W 전액 시연 예치 요청")
                click("구매자 · 현재 계약의 물건 수령 확인")
                self.assertTrue(app.trades.get("buyer", trade_id)["receipt"]["received"])
                widget("number_input", "수정 물건 대금 D (시연 원)").set_value(3000)
                click("수정 조건으로 새 계약 revision 요청")
                self.assertTrue(any("FUNDED_CONTRACT_IMMUTABLE" in error.value for error in at.error))
                self.assertEqual(app.trades.get("buyer", trade_id)["contract"]["revision"], 2)
                widget("number_input", "수정 물건 대금 D (시연 원)").set_value(2000)
                widget("text_area", "증빙 확인 기준").input("새 합성 증빙 기준")
                click("수정 조건으로 새 계약 revision 요청")
                self.assertFalse(at.error)
                self.assertFalse(at.exception)
                trade = app.trades.get("buyer", trade_id)
                self.assertEqual(trade["contract"]["revision"], 3)
                self.assertEqual(trade["contract"]["terms"]["evidence_standard"], "새 합성 증빙 기준")
                self.assertEqual(trade["balances"]["D"]["held"], 2000)
                self.assertFalse(trade["receipt"]["received"])
                self.assertEqual(trade["contract_consents"], [])
                widget("number_input", "D 환불액").set_value(2000)
                widget("selectbox", "계약에 기록된 반환 사유").select("합성 이전 실패")
                click("새 정산안 제출")
                self.assertFalse(at.error)
                rendered = " ".join(str(item.value) for item in at.text)
                self.assertIn("정산안의 반환 사유: 합성 이전 실패", rendered)
                self.assertIn("정산안의 계약 revision: 3", rendered)
                # Editing the next draft's reason must not alter the immutable
                # proposal terms shown for the approval target.
                widget("selectbox", "계약에 기록된 반환 사유").select("양측 반환 합의").run()
                rendered = " ".join(str(item.value) for item in at.text)
                self.assertIn("정산안의 반환 사유: 합성 이전 실패", rendered)
                self.assertNotIn("정산안의 반환 사유: 양측 반환 합의", rendered)
                app.close()
                app = None
        finally:
            if app is not None:
                app.close()
            config.set_option("server.address", old_address)


if __name__ == "__main__":
    unittest.main()
