"""HTTP adapter checks with synthetic, service-owned facts and identities."""
from pathlib import Path
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
import json
import threading
from types import SimpleNamespace
import tempfile
import unittest

from fastapi.testclient import TestClient

from warranty.errors import ServiceError
from warranty.interfaces.api import COOKIE_NAME, create_app


class CatalogStub:
    def __init__(self):
        self.items = [{"item_id": "synthetic-a", "name": "합성 항목 A", "provider": "DEMO", "monthly_fee_won": None,
                       "transfer_allowed": None, "is_synthetic": True, "readiness": {"documents": "DOC_BLOCKED"}}]

    def get(self, item_id):
        if item_id == "explode":
            raise RuntimeError("internal-private-token-not-for-response")
        if item_id != "synthetic-a":
            raise ServiceError("NOT_FOUND", "항목을 찾을 수 없습니다.", 404)
        return self.items[0]

    def summary(self):
        return {"items": len(self.items), "synthetic": True}

    def search(self, **kwargs):
        self.last_search = kwargs
        return {"matches": [], "unknown": self.items}


class RagStub:
    def __init__(self):
        self.calls = []

    def ask(self, actor, item_id, question, **kwargs):
        self.calls.append((actor, item_id, question, kwargs))
        return {"classification": "NEEDS_INFORMATION", "answer": "합성 모의 응답 · 공식 본문 미확보", "cited_evidence_ids": [], "evidence": []}

    def history(self, actor, item_id):
        return [{"question": call[2], "answer": "합성 응답"} for call in self.calls if call[:2] == (actor, item_id)]

    def checklist(self, item_id, applicability, *, user_id=None, review_id=None):
        self.last_checklist = (item_id, applicability, user_id, review_id)
        return {"conditions": [], "classification": "NEEDS_INFORMATION", "missing_information": ["공식 본문"]}

    def prepare(self, actor, item_id):
        raise AssertionError("The HTTP adapter must enqueue document work without running it")

    def enqueue_prepare(self, actor, item_id, applicability=None, question=None):
        self.enqueued = (actor, item_id, applicability, question)
        return {"job_id": "synthetic-job", "item_id": item_id, "status": "BLOCKED", "stage": "DOC_BLOCKED"}

    def retry_prepare(self, actor, job_id):
        self.retried = (actor, job_id)
        return {"job_id": job_id, "status": "QUEUED", "stage": "QUEUED", "retryable": False}

    def job(self, actor, job_id):
        return {"job_id": job_id, "status": "BLOCKED"}


class TradesStub:
    def __init__(self):
        self.calls = []

    def create(self, actor, buyer_id, seller_id, item_id, D, W, **terms):
        self.calls.append((actor, buyer_id, seller_id, item_id, D, W, terms))
        return {"trade_id": "synthetic-trade", "version": 1, "status": "DRAFT"}

    def get(self, actor, trade_id):
        if actor == "outsider":
            raise ServiceError("NOT_FOUND", "거래를 찾을 수 없습니다.", 404)
        return {"trade_id": trade_id, "version": 1}

    def mutate(self, actor, trade_id, action, payload, key, expected_version):
        self.calls.append((actor, trade_id, action, payload, key, expected_version))
        return {"trade_id": trade_id, "version": expected_version + 1}

    def get_operation(self, actor, operation_id):
        return {"operation_id": operation_id, "trade_id": "synthetic-trade"}


def facade(demo=True):
    return SimpleNamespace(settings=SimpleNamespace(demo_mode=demo), catalog=CatalogStub(), rag=RagStub(),
                           trades=TradesStub(), worker=SimpleNamespace(run_pending=lambda: []))


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.application = facade()
        self.client = TestClient(create_app(self.application), base_url="http://localhost", client=("127.0.0.1", 50000), raise_server_exceptions=False)
        self.headers = {"Origin": "http://localhost"}

    def tearDown(self):
        self.client.close()

    def login(self, actor="buyer"):
        response = self.client.post("/session", json={"actor": actor}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.headers["X-CSRF-Token"] = response.json()["data"]["csrf_token"]
        return response

    def test_envelope_and_safe_static_assets(self):
        response = self.client.get("/items")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), {"data", "error", "request_id"})
        self.assertIsNone(response.json()["data"]["items"][0]["monthly_fee_won"])
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        self.assertEqual(self.client.get("/").status_code, 200)
        script_response = self.client.get("/assets/app.js")
        self.assertEqual(script_response.headers["content-type"].split(";", 1)[0], "text/javascript")
        self.assertEqual(self.client.get("/assets/style.css").headers["content-type"].split(";", 1)[0], "text/css")
        script = script_response.text
        self.assertNotIn("innerHTML", script)
        self.assertIn("textContent", script)

    def test_local_demo_host_and_peer_are_required(self):
        with TestClient(create_app(facade()), base_url="http://public.example", client=("203.0.113.1", 4000)) as remote:
            self.assertEqual(remote.get("/items").status_code, 403)
            self.assertEqual(remote.post("/session", json={"actor": "buyer"}, headers={"Origin": "http://public.example"}).status_code, 403)

    def test_login_cookie_and_rotation(self):
        self.client.cookies.set(COOKIE_NAME, "malformed")
        response = self.login()
        cookie = response.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=strict", cookie)
        self.assertEqual(self.client.get("/session").json()["data"]["actor"], "buyer")
        self.login("seller")
        self.assertEqual(self.client.get("/session").json()["data"]["actor"], "seller")

    def test_non_demo_does_not_issue_identity_or_expose_facts(self):
        with TestClient(create_app(facade(False)), base_url="http://localhost", client=("127.0.0.1", 4000)) as production:
            self.assertEqual(production.post("/session", json={"actor": "buyer"}, headers={"Origin": "http://localhost"}).status_code, 403)
            self.assertEqual(production.get("/items").status_code, 401)

    def test_chat_auth_csrf_origin_and_server_identity(self):
        body = {"item_id": "synthetic-a", "question": "조건은?"}
        self.assertEqual(self.client.post("/chat", json=body).status_code, 401)
        self.login()
        self.assertEqual(self.client.post("/chat", json=body).status_code, 403)
        headers = {**self.headers, "Origin": "https://evil.example"}
        self.assertEqual(self.client.post("/chat", json=body, headers=headers).status_code, 403)
        response = self.client.post("/chat", json={**body, "actor": "admin"}, headers=self.headers)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.application.rag.calls, [])
        response = self.client.post("/chat", json=body, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.application.rag.calls[0][0], "buyer")

    def test_history_is_scoped_to_session_actor_and_item(self):
        self.login()
        self.client.post("/chat", json={"item_id": "synthetic-a", "question": "구매자 질문"}, headers=self.headers)
        self.assertEqual(len(self.client.get("/items/synthetic-a/history").json()["data"]), 1)
        self.login("seller")
        self.assertEqual(self.client.get("/items/synthetic-a/history").json()["data"], [])

    def test_search_unknowns_pass_unchanged_to_ui(self):
        self.login()
        response = self.client.post("/search", json={"query": "양도 가능한 5천원 이하", "transfer_required": True}, headers=self.headers)
        self.assertEqual(response.json()["data"]["matches"], [])
        self.assertIsNone(response.json()["data"]["unknown"][0]["transfer_allowed"])
        self.assertTrue(self.application.catalog.last_search["transfer_required"])

    def test_checklist_receives_only_server_session_identity(self):
        self.login("seller")
        response = self.client.post("/items/synthetic-a/checklist", json={"applicability": {"country": "SYNTHETIC"}, "review_id": "synthetic-review"}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.application.rag.last_checklist, ("synthetic-a", {"country": "SYNTHETIC"}, "seller", "synthetic-review"))
        self.assertEqual(self.client.post("/items/synthetic-a/checklist", json={"user_id": "buyer"}, headers=self.headers).status_code, 403)

    def test_invalid_inputs_are_bounded_safe_and_use_400(self):
        self.login()
        cases = [({"item_id": "synthetic-a", "question": "a" * 4001}, 400),
                 ({"item_id": "synthetic-a", "question": "조건?", "top_k": True}, 400),
                 ({"item_id": "synthetic-a", "question": "조건?", "applicability": {"country": []}}, 400)]
        for body, status in cases:
            with self.subTest(body=str(body)[:60]):
                self.assertEqual(self.client.post("/chat", json=body, headers=self.headers).status_code, status)
        response = self.client.post("/chat", content=b"x" * 65537, headers={**self.headers, "Content-Type": "application/json"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get("/items?limit=invalid").status_code, 400)

    def test_jobs_accept_only_server_registered_sources(self):
        self.login()
        self.assertEqual(self.client.post("/items/synthetic-a/index-jobs", json={"url": "http://127.0.0.1/private"}, headers=self.headers).status_code, 403)
        response = self.client.post("/items/synthetic-a/index-jobs", json={}, headers=self.headers)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["data"]["status"], "BLOCKED")

    def test_document_enqueue_preserves_scope_and_rejects_worker_controls(self):
        self.login()
        response = self.client.post("/items/synthetic-a/index-jobs", json={
            "question": "  SYNTHETIC transfer requirements  ", "applicability": {"country": "SYNTHETIC"}}, headers=self.headers)
        self.assertEqual(202, response.status_code, response.text)
        self.assertEqual(("buyer", "synthetic-a", {"country": "SYNTHETIC"}, "SYNTHETIC transfer requirements"), self.application.rag.enqueued)
        for body in ({"outcome": "COMPLETED"}, {"source_id": "forged-source"}, {"worker": "manager"},
                     {"actor": "admin"}, {"applicability": {"allowed_hosts": ["localhost"]}}):
            with self.subTest(body=body):
                self.assertIn(self.client.post("/items/synthetic-a/index-jobs", json=body, headers=self.headers).status_code, (400, 403))
        self.assertEqual(403, self.client.post("/items/synthetic-a/index-jobs", json={}, headers={"Origin": "http://localhost"}).status_code)

    def test_document_retry_uses_existing_job_and_server_session(self):
        self.login("seller")
        response = self.client.post("/index-jobs/synthetic-job/retry", json={}, headers=self.headers)
        self.assertEqual(202, response.status_code, response.text)
        self.assertEqual("QUEUED", response.json()["data"]["status"])
        self.assertEqual(("seller", "synthetic-job"), self.application.rag.retried)
        self.assertEqual(403, self.client.post("/index-jobs/synthetic-job/retry", json={"outcome": "COMPLETED"}, headers=self.headers).status_code)
        self.assertEqual(403, self.client.post("/index-jobs/synthetic-job/retry", json={}, headers={"Origin": "http://localhost"}).status_code)

    def test_runtime_status_is_authenticated_allowlisted_configuration_readiness(self):
        self.assertEqual(401, self.client.get("/runtime").status_code)
        self.login()
        secret = "SYNTHETIC_PRIVATE_RUNTIME_VALUE"
        self.application.runtime_status = lambda: {
            "chat": {"provider": "openai", "configured": True, "ready": True, "is_mock": False, "api_key": secret},
            "embedding": {"provider": "mock", "configured": True, "ready": True, "is_mock": True, "path": secret},
            "collection": {"enabled": True, "network_enabled": False, "registered_target_count": 2, "source_path": secret},
            "external_processing_approved": True, "question_processing_approved": False,
            "document_worker_enabled": True, "private_config": secret}
        response = self.client.get("/runtime")
        self.assertEqual(200, response.status_code)
        data = response.json()["data"]
        self.assertEqual({"chat", "embedding", "collection", "external_processing_approved", "question_processing_approved", "document_worker_enabled", "live"}, set(data))
        self.assertEqual({"provider", "configured", "ready", "is_mock"}, set(data["chat"]))
        self.assertEqual(2, data["collection"]["registered_target_count"])
        self.assertNotIn(secret, response.text)
        self.assertNotIn("api_key", response.text)

    def test_gms_runtime_read_is_live_configuration_only_and_strips_private_budget(self):
        self.login()
        secret = "SYNTHETIC_PRIVATE_GMS_VALUE"
        calls = []
        def status():
            calls.append("read-only-status")
            return {"chat": {"provider": "gms", "configured": True, "ready": False, "is_mock": False},
                    "embedding": {"provider": "gms", "configured": True, "ready": False, "is_mock": False},
                    "live": {"enabled": True, "ready": False, "budget_confirmed": False,
                             "purposes": ["demo", "verification", "demo", secret],
                             "approval_id": secret, "api_key": secret, "store_path": secret,
                             "actual_credits": secret, "live_verified": True}, "private_config": secret}
        self.application.runtime_status = status
        for _ in range(2):
            response = self.client.get("/runtime")
            self.assertEqual(200, response.status_code, response.text)
            data = response.json()["data"]
            self.assertEqual("gms", data["chat"]["provider"])
            self.assertFalse(data["chat"]["ready"])
            self.assertFalse(data["chat"]["is_mock"])
            self.assertEqual({"enabled": True, "ready": False, "budget_confirmed": False,
                              "purposes": ["verification", "demo"]}, data["live"])
            self.assertNotIn(secret, response.text)
            self.assertNotIn("live_verified", response.text)
        self.assertEqual(["read-only-status", "read-only-status"], calls)

    def test_trade_route_passes_key_version_and_only_session_actor(self):
        self.login()
        body = {"expected_version": 2, "revision": 1, "hash": "synthetic-hash"}
        path = "/trades/synthetic-trade/contract-consents"
        self.assertEqual(self.client.post(path, json=body, headers=self.headers).status_code, 400)
        headers = {**self.headers, "Idempotency-Key": "synthetic-key-1"}
        self.assertEqual(self.client.post(path, json={"revision": 1}, headers=headers).status_code, 400)
        response = self.client.post(path, json=body, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.application.trades.calls[-1], ("buyer", "synthetic-trade", "contract_consent", {"revision": 1, "hash": "synthetic-hash"}, "synthetic-key-1", 2))

    def test_trade_create_validates_amounts_and_uses_facade(self):
        self.login()
        body = {"buyer_id": "buyer", "seller_id": "seller", "item_id": "synthetic-a", "D": 1000, "W": 0, "terms": {"refund_condition": "합성 반환 조건"}}
        self.assertEqual(self.client.post("/trades", json={**body, "D": True}, headers=self.headers).status_code, 400)
        self.assertEqual(self.client.post("/trades", json={**body, "terms": {"actor": "admin"}}, headers=self.headers).status_code, 400)
        response = self.client.post("/trades", json=body, headers=self.headers)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.application.trades.calls[-1][0], "buyer")

    def test_object_authorization_remains_service_owned(self):
        self.login("outsider")
        self.assertEqual(self.client.get("/trades/synthetic-trade").status_code, 404)

    def test_operation_recovery_cannot_choose_outcomes(self):
        self.login()
        headers = {**self.headers, "Idempotency-Key": "synthetic-op-key"}
        path = "/operations/synthetic-op/reconcile"
        self.assertEqual(self.client.post(path, json={"expected_version": 1, "outcome": "SUCCESS"}, headers=headers).status_code, 403)
        response = self.client.post(path, json={"expected_version": 1}, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.application.trades.calls[-1][2:4], ("reconcile", {"operation_id": "synthetic-op"}))

    def test_upload_is_disabled_and_worker_takes_no_outcomes(self):
        self.login()
        self.assertEqual(self.client.post("/trades/synthetic-trade/evidence-files", files={"file": ("demo.pdf", b"%PDF-", "application/pdf")}, headers=self.headers).status_code, 403)
        self.assertEqual(self.client.post("/worker/run", json={"outcome": "SUCCESS"}, headers=self.headers).status_code, 403)
        self.assertEqual(self.client.post("/worker/run", json={}, headers=self.headers).status_code, 403)
        self.login("outsider")
        self.assertEqual(self.client.post("/worker/run", json={}, headers=self.headers).status_code, 403)
        self.login("admin")
        self.assertEqual(self.client.post("/worker/run", json={"outcome": "SUCCESS"}, headers=self.headers).status_code, 403)
        self.assertEqual(self.client.post("/worker/run", json={}, headers=self.headers).status_code, 200)

    def test_error_does_not_expose_exception_or_trace(self):
        response = self.client.get("/items/explode")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error"]["code"], "INTERNAL_ERROR")
        self.assertNotIn("internal-private", response.text)
        self.assertNotIn("Traceback", response.text)

    def test_logout_revokes_server_session(self):
        self.login()
        self.assertEqual(self.client.post("/logout", json={}, headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get("/session").status_code, 401)


class SharedServiceApiTests(unittest.TestCase):
    def test_http_enqueue_returns_before_worker_and_get_shows_actual_progress(self):
        from warranty.catalog import Catalog
        from warranty.db import Database
        from warranty.jobs import DocumentJobQueue

        with ExitStack() as cleanup:
            base = Path(cleanup.enter_context(tempfile.TemporaryDirectory(prefix="synthetic-api-job-")))
            catalog = Catalog.synthetic()
            db = Database(base / "app.sqlite3")
            db.initialize()
            cleanup.callback(db.close)
            with db.transaction() as connection:
                for item in catalog.items:
                    connection.execute("INSERT INTO items(item_id,name,provider) VALUES(?,?,?)",
                                       (item["item_id"], item["name"], item["provider"]))
            reached, release = threading.Event(), threading.Event()
            requests = []
            def handler(request, progress):
                requests.append(request)
                progress("FETCHING", candidate_count=1, body_count=0)
                reached.set()
                if not release.wait(3):
                    raise RuntimeError("SYNTHETIC job synchronization timed out")
                return {"status": "BLOCKED", "stage": "DOC_BLOCKED", "failure_code": "SOURCE_BLOCKED"}
            queue = DocumentJobQueue(db, catalog, handler, auto_start=False)
            application = facade()
            application.catalog = catalog
            application.rag = SimpleNamespace(
                enqueue_prepare=lambda actor, item, applicability, question: queue.enqueue(actor, item, {
                    "applicability": applicability or {}, "question": question or "SYNTHETIC document preparation"}),
                job=queue.job, retry_prepare=queue.retry)
            client = cleanup.enter_context(TestClient(create_app(application), base_url="http://localhost", client=("127.0.0.1", 50000)))
            headers = {"Origin": "http://localhost"}
            def login(actor):
                response = client.post("/session", json={"actor": actor}, headers=headers)
                self.assertEqual(200, response.status_code)
                headers["X-CSRF-Token"] = response.json()["data"]["csrf_token"]
            login("buyer")
            item = catalog.items[0]["item_id"]
            response = client.post(f"/items/{item}/index-jobs", json={"applicability": {"country": "SYNTHETIC"}}, headers=headers)
            self.assertEqual(202, response.status_code, response.text)
            queued = response.json()["data"]
            self.assertEqual("QUEUED", queued["status"])
            self.assertEqual([], requests)
            with ThreadPoolExecutor(max_workers=1) as pool:
                worker = pool.submit(queue.run_pending, 1)
                try:
                    self.assertTrue(reached.wait(3))
                    running = client.get(f"/index-jobs/{queued['job_id']}").json()["data"]
                    self.assertEqual("RUNNING", running["status"])
                    self.assertEqual("FETCHING", running["stage"])
                    self.assertEqual(1, running["candidate_count"])
                    self.assertNotIn("request_json", running)
                    self.assertNotIn("claim_token", running)
                finally:
                    release.set()
                worker.result(timeout=5)
            completed = client.get(f"/index-jobs/{queued['job_id']}").json()["data"]
            self.assertEqual("BLOCKED", completed["status"])
            self.assertEqual("SOURCE_BLOCKED", completed["failure_code"])
            self.assertTrue(completed["retryable"])
            login("seller")
            self.assertEqual(403, client.get(f"/index-jobs/{queued['job_id']}").status_code)
            self.assertEqual(403, client.post(f"/index-jobs/{queued['job_id']}/retry", json={}, headers=headers).status_code)
            login("admin")
            self.assertEqual(403, client.get(f"/index-jobs/{queued['job_id']}").status_code)
            login("buyer")
            retry = client.post(f"/index-jobs/{queued['job_id']}/retry", json={}, headers=headers)
            self.assertEqual(202, retry.status_code, retry.text)
            self.assertEqual("QUEUED", retry.json()["data"]["status"])
            self.assertEqual(1, len(requests))

    def test_populated_checklist_uses_actual_rag_and_isolated_saved_review(self):
        from warranty.catalog import Catalog
        from warranty.config import Settings
        from warranty.db import Database
        from warranty.documents import ACTIONS
        from warranty.prompts import MockStructuredModel
        from warranty.rag import RagService
        from warranty.security import SourcePolicy
        from warranty.transactions import TradeService

        class SyntheticPrompts:
            def render(self, kind, variables):
                return json.dumps({"policy": {"kind": kind, "fixture": "SYNTHETIC"}, "input": variables})

            def load(self, kind):
                return {"kind": kind, "fixture": "SYNTHETIC"}

        class SyntheticClauseModel(MockStructuredModel):
            def generate(self, kind, prompt):
                if kind != "answer":
                    return super().generate(kind, prompt)
                document = next(entry for entry in json.loads(prompt)["input"]["evidence"] if entry["source_type"] == "synthetic_document")
                ids = [document["evidence_id"]]
                return {"classification": "CONDITIONAL", "answer": f"MOCK {document['text']} [{document['number']}]",
                        "claims": [{"text": document["text"], "evidence_ids": ids}],
                        "conditions": [{"text": document["text"], "evidence_ids": ids}],
                        "missing_information": [], "conflicts": [], "cited_evidence_ids": ids}

        with ExitStack() as cleanup:
            base = Path(cleanup.enter_context(tempfile.TemporaryDirectory(prefix="warranty-api-checklist-")))
            settings = Settings(base / "data", base / "config")
            settings.prepare()
            catalog = Catalog.synthetic()
            db = Database(base / "data" / "app.sqlite3")
            db.initialize()
            with db.transaction() as connection:
                for item in catalog.items:
                    connection.execute("INSERT INTO items(item_id,name,provider) VALUES(?,?,?)", (item["item_id"], item["name"], item["provider"]))
            source_id, url = "SYNTHETIC-API-DOC", "https://example.org/synthetic-api-terms"
            reviews = [{"source_id": source_id, "action": action, "status": "ALLOWED", "basis_type": "OTHER_RECORDED_BASIS",
                        "basis_url": "https://example.org/", "reviewed_on": "2026-01-01", "scope": "Authored synthetic local test",
                        "conditions": "Offline fixture", "user_scope": "test", "processing_environment": "LOCAL"} for action in ACTIONS]
            policy = SourcePolicy([{"source_id": source_id, "url": url, "allowed_hosts": ["example.org"]}], reviews, {"manager": {"DATA_MANAGER"}})
            rag = RagService(catalog, db, settings, model=SyntheticClauseModel(), prompt_store=SyntheticPrompts(), source_policy=policy, index_actor="manager")
            cleanup.callback(rag.close)
            application = SimpleNamespace(settings=settings, catalog=catalog, rag=rag,
                                          trades=TradeService(db, review_validator=rag.validate_review), worker=SimpleNamespace(run_pending=lambda: []))
            scope = {"country": "SYNTHETIC", "plan_generation": "SYNTHETIC-PLAN", "payment_method": "MONTHLY", "channel": "SYNTHETIC", "document_type": "SERVICE_TERMS", "date_start": "2020-01-01", "date_end": "2030-12-31"}
            inputs = {key: value for key, value in scope.items() if not key.startswith("date_")}
            inputs["enrollment_date"] = "2025-01-01"
            item_id, other_id = [item["item_id"] for item in catalog.items]
            document = rag.registry.register_document("manager", source_id=source_id, title="SYNTHETIC API terms", canonical_url=url, document_type="HTML", is_synthetic=True)
            version = rag.registry.ingest_bytes("manager", document["document_id"], b"<html><h1>SYNTHETIC</h1><p>Transfer requires provider confirmation.</p></html>", applicability=scope)
            rag.registry.link("manager", item_id, version["version_id"], role="SERVICE_TERMS", applicability_basis="Authored synthetic fixture covers this item.")
            self.assertEqual(rag.prepare("buyer", item_id)["status"], "COMPLETED")
            client = cleanup.enter_context(TestClient(create_app(application), base_url="http://localhost", client=("127.0.0.1", 50000)))
            headers = {"Origin": "http://localhost"}

            def login(actor):
                response = client.post("/session", json={"actor": actor}, headers=headers)
                self.assertEqual(response.status_code, 200, response.text)
                headers["X-CSRF-Token"] = response.json()["data"]["csrf_token"]

            def checklist(item=item_id, applicability=None, review_id=None):
                body = {"applicability": inputs if applicability is None else applicability}
                if review_id:
                    body["review_id"] = review_id
                response = client.post(f"/items/{item}/checklist", json=body, headers=headers)
                self.assertEqual(response.status_code, 200, response.text)
                return response.json()["data"]

            login("buyer")
            response = client.post("/chat", json={"item_id": item_id, "question": "SYNTHETIC transfer requirements?", "applicability": inputs}, headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            answer = response.json()["data"]
            self.assertEqual(answer["classification"], "CONDITIONAL")
            self.assertTrue(answer["conditions"])
            projected = checklist(review_id=answer["review_id"])
            self.assertEqual(projected["conditions"], answer["conditions"])
            self.assertEqual(projected["cited_evidence_ids"], answer["cited_evidence_ids"])
            self.assertTrue(projected["required_condition_ids"])
            self.assertFalse(projected["conditions_ok"])
            self.assertEqual(checklist(item=other_id)["conditions"], [])
            self.assertEqual(checklist(applicability={**inputs, "enrollment_date": "2024-01-01"})["conditions"], [])
            login("seller")
            self.assertEqual(checklist(review_id=answer["review_id"])["conditions"], [])

            # The same actual API boundary must preserve explicit false checks:
            # knowing a clause does not attest that its required act happened.
            login("buyer")
            response = client.post("/trades", json={"buyer_id": "buyer", "seller_id": "seller", "item_id": item_id,
                                                  "D": 1000, "W": 80, "terms": {}}, headers=headers)
            self.assertEqual(response.status_code, 201, response.text)
            trade = response.json()["data"]
            sequence = 0

            def mutate(route, payload=None, expected_status=200):
                nonlocal trade, sequence
                sequence += 1
                response = client.post(f"/trades/{trade['trade_id']}/{route}", json={**(payload or {}), "expected_version": trade["version"]},
                                       headers={**headers, "Idempotency-Key": f"synthetic-condition-{sequence}"})
                self.assertEqual(response.status_code, expected_status, response.text)
                trade = client.get(f"/trades/{trade['trade_id']}").json()["data"]
                return response

            mutate("reviews", {"review_id": answer["review_id"]})
            self.assertEqual(trade["review"]["required_condition_ids"], projected["required_condition_ids"])
            self.assertNotIn("conditions", trade["review"])
            for actor in ("buyer", "seller"):
                login(actor)
                mutate("contract-consents", {key: trade["contract"][key] for key in ("revision", "hash")})
            login("buyer")
            mutate("deposit")
            required = trade["review"]["required_condition_ids"]

            def attest_and_propose(confirmed):
                mutate("evidence-bundles", {"references": ["SYNTHETIC:condition-check"],
                       "condition_checks": [{"condition_id": condition_id, "confirmed": confirmed} for condition_id in required]})
                self.assertTrue(all(check["confirmed"] is confirmed for check in trade["evidence"]["content"]["condition_checks"]))
                for actor in ("buyer", "seller"):
                    login(actor)
                    mutate("evidence-confirmations", {"evidence_id": trade["evidence"]["evidence_id"], "hash": trade["evidence"]["hash"]})
                mutate("settlement-proposals", {"allocations": [{"component": "W", "pay_won": 80, "refund_won": 0}]})
                proposal = trade["proposals"][-1]
                for actor in ("buyer", "seller"):
                    login(actor)
                    mutate("settlement-consents", {key: proposal[key] for key in ("proposal_id", "revision", "hash")})
                return {"proposal_id": proposal["proposal_id"], "revision": proposal["revision"]}

            false_proposal = attest_and_propose(False)
            before_ledger = len(trade["ledger"])
            response = mutate("settlement-executions", false_proposal, expected_status=422)
            self.assertEqual(response.json()["error"]["code"], "W_CONDITIONS_NOT_CONFIRMED")
            self.assertEqual(trade["balances"]["W"]["pay_reserved"], 0)
            self.assertEqual(trade["operations"], [])
            self.assertEqual(len(trade["ledger"]), before_ledger)
            true_proposal = attest_and_propose(True)
            mutate("settlement-executions", true_proposal)
            self.assertEqual(trade["balances"]["W"]["pay_reserved"], 80)
            self.assertEqual(len(trade["operations"]), 1)

    def test_real_facade_d_only_trade_and_w_refund(self):
        from warranty.application import build_application
        from warranty.config import Settings
        with ExitStack() as cleanup:
            base = Path(cleanup.enter_context(tempfile.TemporaryDirectory(prefix="warranty-api-")))
            application = build_application(Settings(base / "data", base / "config"))
            cleanup.callback(application.rag.vectors.close)
            client = cleanup.enter_context(TestClient(create_app(application), base_url="http://localhost", client=("127.0.0.1", 50000)))
            headers = {"Origin": "http://localhost"}

            def role(actor):
                response = client.post("/session", json={"actor": actor}, headers=headers)
                self.assertEqual(response.status_code, 200, response.text)
                headers["X-CSRF-Token"] = response.json()["data"]["csrf_token"]

            def mutate(route, payload=None):
                nonlocal trade
                response = client.post(f"/trades/{trade['trade_id']}/{route}", json={**(payload or {}), "expected_version": trade["version"]},
                                       headers={**headers, "Idempotency-Key": f"synthetic-{trade['version']}-{route}"})
                self.assertEqual(response.status_code, 200, response.text)
                trade = client.get(f"/trades/{trade['trade_id']}").json()["data"]
                return response

            def both_contract():
                for actor in ("buyer", "seller"):
                    role(actor)
                    mutate("contract-consents", {"revision": trade["contract"]["revision"], "hash": trade["contract"]["hash"]})

            def settle(allocations, **conditions):
                role("buyer")
                mutate("settlement-proposals", {"allocations": allocations, **conditions})
                proposal = trade["proposals"][-1]
                for actor in ("buyer", "seller"):
                    role(actor)
                    mutate("settlement-consents", {key: proposal[key] for key in ("proposal_id", "revision", "hash")})
                mutate("settlement-executions", {"proposal_id": proposal["proposal_id"], "revision": proposal["revision"]})
                role("admin")
                response = client.post("/worker/run", json={}, headers=headers)
                self.assertEqual(response.status_code, 200, response.text)

            role("buyer")
            item_id = client.get("/items").json()["data"]["items"][0]["item_id"]
            response = client.post("/trades", json={"buyer_id": "buyer", "seller_id": "seller", "item_id": item_id,
                                                  "D": 1000, "W": 80, "terms": {"return_conditions": ["mutual_agreement"]}}, headers=headers)
            self.assertEqual(response.status_code, 201, response.text)
            trade = response.json()["data"]
            both_contract()
            role("buyer")
            mutate("deposit")
            mutate("receipt", {"contract_revision": trade["contract"]["revision"]})
            settle([{"component": "D", "pay_won": 1000, "refund_won": 0}])
            trade = client.get(f"/trades/{trade['trade_id']}").json()["data"]
            self.assertEqual(trade["balances"]["D"]["paid"], 1000)
            self.assertIsNone(trade["review"])
            self.assertIsNone(trade["evidence"])
            settle([{"component": "W", "pay_won": 0, "refund_won": 80}], refund_reason="mutual_agreement")
            trade = client.get(f"/trades/{trade['trade_id']}").json()["data"]
            role("buyer")
            mutate("close")
            self.assertEqual(trade["status"], "CLOSED")
            self.assertEqual(trade["balances"]["W"]["refunded"], 80)
            self.assertEqual(len(trade["operations"]), 2)


if __name__ == "__main__":
    unittest.main()
