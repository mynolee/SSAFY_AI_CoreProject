"""Offline GMS composition boundaries; every transport and credit rate is synthetic.

These tests use the real persistent stores and services, never a GMS account,
SDK connection, real document, actual provider price, or real financial action.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, ExitStack
from decimal import Decimal
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from warranty.application import build_application
from warranty.catalog import Catalog
from warranty.config import Settings
from warranty.documents import ACTIONS
from warranty.embedding_cache import EmbeddingCache
from warranty.errors import ServiceError
from warranty.gms import GMS_BASE_URL, GMSEmbeddings, GMSStructuredModel
from warranty.interfaces.api import create_app
from warranty.live import live_execution
from warranty.live_budget import CreditBudget
from warranty.prompts import output_schema
from warranty.transactions import verify_accounting
from warranty.usage import UsageBudget


APPROVAL = "synthetic-live-boundary"
FINGERPRINT = "7" * 64
ENV = {"GMS_API_KEY": "synthetic-key-never-live", "GMS_BASE_URL": GMS_BASE_URL,
       "GMS_CHAT_MODEL": "gpt-4.1-mini", "GMS_CHAT_ROUTE": "chat/completions",
       "GMS_MAX_OUTPUT_TOKENS": "64", "GMS_EMBEDDING_MODEL": "text-embedding-3-small",
       "GMS_EMBEDDING_DIMENSION": "2"}
CONTEXTS = [{"kind": "question", "source_id": "SYNTHETIC", "version_id": None, "actor": "buyer"}]


def synthetic_profile(*, verification="10000", confirmed=True):
    """Authored test prices and bounds; no actual GMS pricing is represented."""
    def row(kind):
        return {"kind": kind, "route": "chat.completions" if kind == "chat" else "embeddings",
                "input_microcredits_per_token": 1000,
                "output_microcredits_per_token": 1000 if kind == "chat" else 0,
                "input_bound": "wire_utf8_bytes", "input_bound_confirmed": True,
                "input_token_overhead": 7, "max_input_tokens": 100000,
                "output_bound": "max_tokens" if kind == "chat" else "none",
                "output_bound_confirmed": True, "max_output_tokens": 64 if kind == "chat" else 0}
    return {"profile_id": "synthetic-no-real-price", "pricing_confirmed": confirmed,
            "pricing_version": "synthetic-v1",
            "allocations": {"evaluation": "10000", "verification": verification,
                            "final_verification": "10000", "demo": "10000",
                            "embedding_preparation": "10000"}, "demo_reserve_credits": "1000",
            "models": {"gpt-4.1-mini": row("chat"), "text-embedding-3-small": row("embedding")}}


def synthetic_prompt():
    variables = {"item": {}, "question": "SYNTHETIC offline query", "applicability": {}}
    policy = {"version": "1.0.0", "kind": "query", "role": "Synthetic fixture",
              "input_variables": list(variables), "evidence_limits": ["Synthetic rule"],
              "output_schema": output_schema("query"), "missing_information_rules": ["Synthetic rule"],
              "conflict_rules": ["Synthetic rule"], "attack_rules": ["Synthetic rule"],
              "template": "Synthetic fixture"}
    return json.dumps({"policy": policy, "input": variables}, ensure_ascii=False)


def chat_result():
    return {"model": "gpt-4.1-mini", "choices": [{"index": 0, "finish_reason": "stop",
        "message": {"role": "assistant", "content": json.dumps({"queries": [], "filters": {},
            "required_document_roles": [], "missing_information": []})}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 3, "credits": "0.000001"}}


def chat_upper_bound():
    envelope = json.loads(synthetic_prompt())
    body = {"model": ENV["GMS_CHAT_MODEL"], "messages": [
        {"role": "system", "content": json.dumps(envelope["policy"], ensure_ascii=False)},
        {"role": "user", "content": json.dumps({"input": envelope["input"]}, ensure_ascii=False)}],
        "max_tokens": 64, "stream": False}
    wire_bytes = len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode())
    return Decimal(wire_bytes + 7 + 64) / 1000


class GMSLiveBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.base = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="synthetic-gms-boundary-")))
        self.settings = Settings(self.base / "data", self.base / "config", remote_models=True)
        self.settings.prepare()
        self.requests, self.adapters = [], []
        self.request_lock = threading.Lock()
        self.addCleanup(self.close_adapters)
        self.status = 200
        self.reported_model = ENV["GMS_CHAT_MODEL"]
        original_connect = socket.socket.connect
        def connect(sock, address):
            # Windows asyncio implements its private wake-up pipe with the
            # stdlib's loopback socketpair. This is not an HTTP transport.
            frame = sys._getframe(1)
            while frame is not None:
                if frame.f_code.co_name == "_fallback_socketpair" and Path(frame.f_code.co_filename).name == "socket.py":
                    return original_connect(sock, address)
                frame = frame.f_back
            raise AssertionError("Real network forbidden in synthetic regression")
        self.stack.enter_context(patch.object(socket.socket, "connect", connect))
        self.stack.enter_context(patch.object(socket.socket, "connect_ex",
            side_effect=AssertionError("Real network forbidden in synthetic regression")))
        self.stack.enter_context(patch.object(socket, "create_connection",
            side_effect=AssertionError("Real network forbidden in synthetic regression")))
        self.stack.enter_context(patch("httpx.HTTPTransport",
            side_effect=AssertionError("Only injected MockTransport is allowed")))
        for target in ("warranty.prompts._official_sdk_client", "warranty.embeddings._official_sdk_client"):
            self.stack.enter_context(patch(target, side_effect=AssertionError("Real SDK forbidden")))

    def close_adapters(self):
        for adapter in self.adapters:
            adapter.close()

    def transport(self, request):
        with self.request_lock:
            self.requests.append(request)
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(self.status, json={"model": "text-embedding-3-small",
                "data": [{"index": 0, "embedding": [0.25, -0.5]}], "usage": {"prompt_tokens": 5}})
        result = chat_result()
        result["model"] = self.reported_model
        return httpx.Response(self.status, json=result)

    def stores(self, *, limits=None, profile=None, name="shared"):
        limits = limits or {"chat_requests": 100, "embedding_requests": 100, "input_bytes": 10000000}
        usage = UsageBudget(self.base / (name + "-usage.sqlite3"), APPROVAL, limits,
                            context_fingerprint=FINGERPRINT)
        credit = CreditBudget(self.base / (name + "-credits.sqlite3"), APPROVAL,
                              synthetic_profile() if profile is None else profile,
                              context_fingerprint=FINGERPRINT)
        return usage, credit

    def chat(self, usage, credit, *, handler=None):
        model = GMSStructuredModel(self.settings, lambda *args: True, env=dict(ENV),
            usage_budget=usage, credit_budget=credit, transport=httpx.MockTransport(handler or self.transport))
        self.adapters.append(model)
        return model

    def embedding(self, usage, credit, *, handler=None, guard=None):
        model = GMSEmbeddings(self.settings, guard or (lambda *args: True), env=dict(ENV),
            usage_budget=usage, credit_budget=credit,
            embedding_cache=EmbeddingCache(self.base / "shared-vectors.sqlite3"),
            transport=httpx.MockTransport(handler or self.transport))
        self.adapters.append(model)
        return model

    def assert_code(self, expected, operation):
        with self.assertRaises(ServiceError) as raised:
            operation()
        self.assertEqual(expected, raised.exception.code)
        encoded = json.dumps(raised.exception.as_dict())
        self.assertNotIn(ENV["GMS_API_KEY"], encoded)
        self.assertNotIn(str(self.base), encoded)

    def build(self, *, confirmed=True):
        runtime = {"models": {"chat_provider": "gms", "chat_model": ENV["GMS_CHAT_MODEL"],
            "embedding_provider": "gms", "embedding_model": ENV["GMS_EMBEDDING_MODEL"],
            "embedding_dimension": 2, "gms_base_url": GMS_BASE_URL,
            "gms_chat_route": "chat/completions", "gms_max_output_tokens": 64,
            "external_processing_approved": True, "allow_question_processing": True,
            "usage_approval_id": APPROVAL,
            "usage_limits": {"chat_requests": 10, "embedding_requests": 10, "input_bytes": 1000000}},
            "collection": {"manager_actor": "manager", "worker_enabled": False}}
        catalog = Catalog.synthetic()
        policy = {"schema_version": 1, "registry": [], "reviews": [],
                  "capabilities": {"manager": ["DATA_MANAGER"]}}
        for source_id, source in catalog.sources.items():
            url = source.get("url") or source.get("source_url") or source.get("canonical_url")
            policy["registry"].append({"source_id": source_id, "url": url, "is_synthetic": True})
            policy["reviews"].extend({"source_id": source_id, "action": action, "status": "ALLOWED",
                "basis_type": "OTHER_RECORDED_BASIS", "basis_url": "https://example.invalid/synthetic-basis",
                "reviewed_on": "2026-01-01", "scope": "Authored synthetic offline boundary only",
                "conditions": "No actual source or provider approval is represented",
                "user_scope": "Named synthetic actors", "processing_environment": "LOCAL",
                "allowed_actors": ["buyer", "seller", "manager"], "version_scope": "SOURCE"}
                for action in ACTIONS)
        for name, value in (("runtime.json", runtime), ("source_policy.json", policy),
                            ("live_budget.json", synthetic_profile(confirmed=confirmed))):
            (self.settings.config_dir / name).write_text(json.dumps(value), encoding="utf-8")
        environment = {**ENV, "METADATA_PATH": "", "METADATA_EXPECTED_HASH": "",
                       "OPENAI_API_KEY": "", "CHAT_MODEL": "", "EMBEDDING_MODEL": ""}
        self.stack.enter_context(patch.dict(os.environ, environment))
        # Preserve the real adapters and budgets, replacing only their transport.
        self.stack.enter_context(patch("warranty.gms.GMSStructuredModel",
            side_effect=lambda *args, **kwargs: GMSStructuredModel(*args,
                transport=httpx.MockTransport(self.transport), **kwargs)))
        self.stack.enter_context(patch("warranty.gms.GMSEmbeddings",
            side_effect=lambda *args, **kwargs: GMSEmbeddings(*args,
                transport=httpx.MockTransport(self.transport), **kwargs)))
        app = build_application(settings=self.settings, start_document_worker=False)
        self.addCleanup(app.close)
        return app

    def settle_demo(self, app):
        service = app.trades
        item = app.catalog.items[0]["item_id"]
        trade = service.create("buyer", "buyer", "seller", item, 100, 20)
        def change(action, payload=None, actor="buyer"):
            current = service.get(actor, trade["trade_id"])
            return service.mutate(actor, trade["trade_id"], action, payload or {}, uuid4().hex, current["version"])
        contract = trade["contract"]
        for actor in ("buyer", "seller"):
            change("contract_consent", {"revision": contract["revision"], "hash": contract["hash"]}, actor)
        change("deposit")
        change("receipt", {"contract_revision": trade["contract_revision"]})
        proposed = change("proposal", {"allocations": [{"component": "D", "pay_won": 100, "refund_won": 0},
            {"component": "W", "pay_won": 0, "refund_won": 20}], "refund_reason": "mutual_agreement"})
        proposal = proposed["proposals"][-1]
        target = {"proposal_id": proposal["proposal_id"], "revision": proposal["revision"], "hash": proposal["hash"]}
        for actor in ("buyer", "seller"):
            change("proposal_consent", target, actor)
        change("reserve", {"proposal_id": proposal["proposal_id"], "revision": proposal["revision"]})
        self.assertEqual(1, service.run_pending()["processed_jobs"])
        current = service.get("buyer", trade["trade_id"])
        self.assertEqual(100, current["balances"]["D"]["paid"])
        self.assertEqual(20, current["balances"]["W"]["refunded"])
        self.assertIsNone(current["review"])
        with app.db.connect() as connection:
            verify_accounting(connection, trade["trade_id"])
            self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())

    def test_factory_http_refresh_chat_and_queue_resume_cannot_open_live_scope(self):
        app = self.build()
        item = app.catalog.items[0]["item_id"]
        with TestClient(create_app(app), base_url="http://localhost", client=("127.0.0.1", 51000)) as client:
            headers = {"Origin": "http://localhost"}
            response = client.post("/session", json={"actor": "buyer"}, headers=headers)
            self.assertEqual(200, response.status_code, response.text)
            headers["X-CSRF-Token"] = response.json()["data"]["csrf_token"]
            for _ in range(2):
                response = client.get("/runtime")
                self.assertEqual(200, response.status_code, response.text)
                self.assertEqual("gms", response.json()["data"]["chat"]["provider"])
                self.assertFalse(app.models.chat.metadata()["can_generate"])
                self.assertEqual(200, client.get("/items").status_code)
            client.post("/chat", json={"item_id": item, "question": "SYNTHETIC question"}, headers=headers)
            queued = client.post(f"/items/{item}/index-jobs", json={}, headers=headers)
            self.assertEqual(202, queued.status_code, queued.text)
            job_id = queued.json()["data"]["job_id"]
            app.document_jobs.run_pending(limit=1)
            job = client.get(f"/index-jobs/{job_id}").json()["data"]
            self.assertIn(job["status"], {"BLOCKED", "FAILED"})
            retried = client.post(f"/index-jobs/{job_id}/retry", json={}, headers=headers)
            self.assertEqual(202, retried.status_code, retried.text)
            app.document_jobs.run_pending(limit=1)
        self.settle_demo(app)
        self.assertEqual([], self.requests)
        self.assertFalse((self.settings.data_dir / "model_usage.sqlite3").exists())
        self.assertFalse((self.settings.data_dir / "model_credits.sqlite3").exists())

    def test_unconfirmed_credit_profile_blocks_model_but_keeps_d_and_w_refund_usable(self):
        app = self.build(confirmed=False)
        self.assert_code("PROCESSING_APPROVAL_REQUIRED",
                         lambda: live_execution(app, "verification", APPROVAL).__enter__())
        self.settle_demo(app)
        self.assertEqual([], self.requests)
        self.assertFalse((self.settings.data_dir / "model_usage.sqlite3").exists())
        self.assertFalse((self.settings.data_dir / "model_credits.sqlite3").exists())

    def test_persistent_failed_retry_and_success_keep_upper_credit_separate_from_actual(self):
        usage, credit = self.stores(limits={"chat_requests": 2, "embedding_requests": 0, "input_bytes": 100000})
        first = self.chat(usage, credit)
        self.status = 503
        with first.live_execution("verification", APPROVAL):
            self.assert_code("MODEL_UNAVAILABLE", lambda: first.generate("query", synthetic_prompt()))
        usage2, credit2 = self.stores(limits={"chat_requests": 2, "embedding_requests": 0, "input_bytes": 100000})
        second = self.chat(usage2, credit2)
        self.status = 200
        with second.live_execution("verification", APPROVAL):
            second.generate("query", synthetic_prompt())
            self.assert_code("USAGE_LIMIT_REACHED", lambda: second.generate("query", synthetic_prompt()))
        self.assertEqual(2, len(self.requests))
        self.assertEqual(2, usage2.snapshot()["chat_requests"])
        snapshot = credit2.snapshot()
        self.assertEqual(2, snapshot["request_count"])
        self.assertEqual(2, snapshot["unknown_request_count"])
        self.assertIsNone(snapshot["provider_reported_actual_credits"])
        self.assertEqual("0.014000", snapshot["token_based_estimate_credits"])
        self.assertGreater(Decimal(snapshot["reserved_upper_credits"]), Decimal("0.014000"))
        with closing(sqlite3.connect(self.base / "shared-credits.sqlite3")) as connection:
            rows = connection.execute("SELECT request_id,state,provider_actual_micro FROM credit_requests").fetchall()
        self.assertEqual(2, len({row[0] for row in rows}))
        self.assertTrue(all(row[1:] == ("UNKNOWN", None) for row in rows))

    def test_shared_persistent_credit_cap_fences_concurrent_adapters_before_http(self):
        request_cost = chat_upper_bound()
        profile = synthetic_profile(verification=str(request_cost * 3))
        barrier = threading.Barrier(12)
        models = [self.chat(*self.stores(profile=profile)) for _ in range(12)]
        def run(model):
            barrier.wait(timeout=5)
            try:
                with model.live_execution("verification", APPROVAL):
                    model.generate("query", synthetic_prompt())
                return "SUCCEEDED"
            except ServiceError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=12) as pool:
            outcomes = list(pool.map(run, models))
        self.assertEqual(3, outcomes.count("SUCCEEDED"), outcomes)
        self.assertEqual(3, len(self.requests))
        self.assertTrue(set(outcomes) <= {"SUCCEEDED", "CREDIT_LIMIT_REACHED", "LIVE_BUDGET_UNCONFIRMED"}, outcomes)
        _, credit = self.stores(profile=profile)
        snapshot = credit.snapshot()
        self.assertEqual(3, snapshot["request_count"])
        self.assertEqual(request_cost * 3, Decimal(snapshot["purposes"]["verification"]["reserved_upper_credits"]))
        self.assertEqual("0.000000", snapshot["purposes"]["verification"]["remaining_credits"])
        self.assertLessEqual(Decimal(snapshot["reserved_upper_credits"]), Decimal(70000))

    def test_last_request_slots_are_usable_once_for_chat_and_embedding(self):
        for kind in ("chat", "embedding"):
            with self.subTest(kind=kind):
                usage, credit = self.stores(name=kind, limits={"chat_requests": int(kind == "chat"),
                    "embedding_requests": int(kind == "embedding"), "input_bytes": 100000})
                model = self.chat(usage, credit) if kind == "chat" else self.embedding(usage, credit)
                before = len(self.requests)
                with model.live_execution("verification", APPROVAL):
                    if kind == "chat":
                        model.generate("query", synthetic_prompt())
                        self.assert_code("USAGE_LIMIT_REACHED", lambda: model.generate("query", synthetic_prompt()))
                    else:
                        with model.processing_context(CONTEXTS):
                            self.assertEqual([[0.25, -0.5]], model.embed(["SYNTHETIC final allowed string"]))
                            self.assert_code("USAGE_LIMIT_REACHED", lambda: model.embed(["SYNTHETIC different string"]))
                self.assertEqual(before + 1, len(self.requests))
                self.assertEqual(1, credit.snapshot()["request_count"])

    def test_exact_final_credit_reservation_can_send_once_without_rechecking_new_capacity(self):
        profile = synthetic_profile()
        profile["allocations"] = {purpose: "0" for purpose in profile["allocations"]}
        profile["allocations"]["verification"] = str(chat_upper_bound())
        profile["demo_reserve_credits"] = "0"
        usage, credit = self.stores(profile=profile)
        model = self.chat(usage, credit)
        with model.live_execution("verification", APPROVAL):
            model.generate("query", synthetic_prompt())
            with self.assertRaises(ServiceError):
                model.generate("query", synthetic_prompt())
        self.assertEqual(1, len(self.requests))
        self.assertEqual(1, credit.snapshot()["request_count"])
        self.assertEqual("0.000000", credit.snapshot()["purposes"]["verification"]["remaining_credits"])

    def test_shared_embedding_claim_and_retry_reuse_vectors_without_rebilling(self):
        usage, credit = self.stores()
        entered, release = threading.Event(), threading.Event()
        def held(request):
            entered.set()
            if not release.wait(5):
                raise AssertionError("Synthetic transport synchronization timed out")
            return self.transport(request)
        first = self.embedding(usage, credit, handler=held)
        second = self.embedding(*self.stores())
        def embed(model):
            with model.live_execution("embedding_preparation", APPROVAL), model.processing_context(CONTEXTS):
                return model.embed(["SYNTHETIC immutable body"])
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(embed, first)
            try:
                self.assertTrue(entered.wait(5))
                self.assert_code("EMBEDDING_CACHE_IN_PROGRESS", lambda: embed(second))
            finally:
                release.set()
            self.assertEqual([[0.25, -0.5]], pending.result(timeout=5))
        self.assertEqual([[0.25, -0.5]], embed(second))
        second._guard = lambda *args: False
        self.assert_code("MODEL_REMOTE_BLOCKED", lambda: embed(second))
        self.assertEqual(1, len(self.requests))
        self.assertEqual(1, usage.snapshot()["embedding_requests"])
        self.assertEqual(1, credit.snapshot()["request_count"])
        self.assertNotIn(b"SYNTHETIC immutable body", (self.base / "shared-vectors.sqlite3").read_bytes())

    def test_factory_exhausted_request_budget_leaves_trade_services_available(self):
        app = self.build()
        adapter = app.models.chat.adapter
        item = app.catalog.items[0]["item_id"]
        for _ in range(10):
            app.models.usage_budget.consume("chat", 1)
        # The separate embedding allowance can keep the shared budget ready;
        # it cannot grant another chat request.
        with live_execution(app, "verification", APPROVAL), app.models.chat.processing_context("buyer", item):
            self.assert_code("USAGE_LIMIT_REACHED", lambda: adapter.generate("query", synthetic_prompt()))
        self.settle_demo(app)
        self.assertEqual([], self.requests)
        self.assertEqual(10, app.models.usage_budget.snapshot()["chat_requests"])
        self.assertEqual(0, app.models.credit_budget.snapshot()["request_count"])

    def test_unapproved_reported_model_persists_credit_freeze_without_disabling_trades(self):
        app = self.build()
        self.reported_model = "SYNTHETIC-unapproved-model"
        item = app.catalog.items[0]["item_id"]
        with live_execution(app, "verification", APPROVAL), app.models.chat.processing_context("buyer", item):
            self.assert_code("LIVE_MODEL_IDENTITY_MISMATCH",
                             lambda: app.models.chat.generate("query", synthetic_prompt()))
        self.assertEqual(1, len(self.requests))
        snapshot = app.models.credit_budget.snapshot()
        self.assertFalse(snapshot["ready"])
        self.assertEqual("LIVE_BUDGET_UNAVAILABLE", snapshot["reason_code"])
        self.assertEqual(1, snapshot["unknown_request_count"])
        self.assertIsNone(snapshot["provider_reported_actual_credits"])
        path = self.settings.data_dir / "model_credits.sqlite3"
        with closing(sqlite3.connect(path)) as connection:
            fingerprint = connection.execute("SELECT context_fingerprint FROM credit_approvals").fetchone()[0]
        reopened = CreditBudget(path, APPROVAL, synthetic_profile(), context_fingerprint=fingerprint)
        self.assertFalse(reopened.ready)
        self.assertEqual(1, reopened.snapshot()["request_count"])
        self.settle_demo(app)
        self.assertEqual(1, len(self.requests))


if __name__ == "__main__":
    unittest.main()
