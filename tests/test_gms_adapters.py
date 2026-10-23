"""Synthetic mock transports only: these tests never contact GMS."""
from contextlib import ExitStack
import json
import threading
from types import SimpleNamespace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from warranty.errors import ServiceError
from warranty.gms import GMS_BASE_URL, GMSEmbeddings, GMSStructuredModel
from warranty.prompts import output_schema
from warranty.usage import UsageBudget
from warranty.embedding_cache import EmbeddingCache
from warranty.security import SourcePolicy


def prompt(kind="query"):
    variables = {"item": {"item_id": "SYNTHETIC-1"}, "question": "합성 질문", "applicability": {}}
    names = list(variables)
    policy = {"version": "1.0.0", "kind": kind, "role": "Synthetic fixture", "input_variables": names,
              "evidence_limits": ["Synthetic rule"], "output_schema": output_schema(kind),
              "missing_information_rules": ["Synthetic rule"], "conflict_rules": ["Synthetic rule"],
              "attack_rules": ["Synthetic rule"], "template": "Synthetic fixture"}
    return json.dumps({"policy": policy, "input": variables}, ensure_ascii=False)


ENV = {"GMS_API_KEY": "synthetic-key-never-live", "GMS_BASE_URL": GMS_BASE_URL,
       "GMS_CHAT_MODEL": "gpt-4.1-mini", "GMS_CHAT_ROUTE": "chat/completions",
       "GMS_MAX_OUTPUT_TOKENS": "512",
       "GMS_EMBEDDING_MODEL": "text-embedding-3-small", "GMS_EMBEDDING_DIMENSION": "2"}
CONTEXTS = [{"kind": "question", "source_id": "SYNTHETIC-1", "version_id": None, "actor": "buyer"}]


class FakeBudget:
    def __init__(self, events, *, limit=100):
        self.events, self.limit, self.count, self.ready = events, limit, 0, True
        self._lock = threading.Lock()

    def approval_matches(self, approval_id):
        return approval_id == "synthetic-approval"

    def consume(self, kind, input_bytes):
        with self._lock:
            if self.count >= self.limit:
                raise ServiceError("USAGE_LIMIT_REACHED", "Safe quota", 429)
            self.count += 1
            self.events.append(("consume", kind, input_bytes))

    def snapshot(self):
        return {"ready": self.ready, "reason_code": "PROCESSING_APPROVAL_REQUIRED"}


class FakeCredit:
    def __init__(self, events):
        self.events, self.ready = events, True
        self.reservations, self.settlements = [], []

    def approval_matches(self, approval_id):
        return approval_id == "synthetic-approval"

    def estimate(self, kind, model, input_bytes, *, output_tokens_bound, route):
        self.events.append(("estimate",))
        return {"estimated_upper_credits": "1.0", "input_tokens_bound": input_bytes,
                "output_tokens_bound": output_tokens_bound}

    def reserve(self, request_id, purpose, estimated_upper_credits, **kwargs):
        self.events.append(("reserve",))
        self.reservations.append({"request_id": request_id, "purpose": purpose, **kwargs})
        return {"request_id": request_id, "replayed": False, "state": "RESERVED"}

    def settle(self, request_id, actual_credits=None, *, usage=None):
        self.events.append(("settle",))
        self.settlements.append({"request_id": request_id, "actual_credits": actual_credits, "usage": usage})

    def snapshot(self):
        return {"ready": self.ready, "reason_code": None if self.ready else "LIVE_BUDGET_UNCONFIRMED"}

    def allowed_reported_models(self, kind, requested_model, route):
        return frozenset({requested_model})

    def freeze(self, reason="MODEL_IDENTITY_MISMATCH"):
        self.ready = False
        self.events.append(("freeze", reason))


class FakeCache:
    def __init__(self):
        self.rows, self.claims = {}, {}

    def get(self, key, dimension, text):
        return self.rows.get((key, dimension, text))

    def claim(self, key, dimension, text):
        identity = (key, dimension, text)
        if identity in self.claims:
            raise ServiceError("EMBEDDING_CACHE_IN_PROGRESS", "Safe busy", 409)
        token = "synthetic-token"
        self.claims[identity] = token
        return token

    def put(self, key, dimension, text, vector, *, claim_token):
        identity = (key, dimension, text)
        if self.claims.get(identity) != claim_token:
            raise ServiceError("EMBEDDING_CACHE_CLAIM_LOST", "Safe lost", 409)
        self.rows[identity] = vector
        del self.claims[identity]

    def release(self, key, dimension, text, token):
        self.claims.pop((key, dimension, text), None)
        return True


def chat_response(content='{"queries":[],"filters":{},"required_document_roles":[],"missing_information":[]}', **changes):
    result = {"model": "gpt-4.1-mini", "choices": [{"index": 0, "finish_reason": "stop",
              "message": {"role": "assistant", "content": content}}]}
    result.update(changes)
    return result


class GMSTests(unittest.TestCase):
    def setUp(self):
        self.events, self.requests = [], []
        self.budget = FakeBudget(self.events)
        self.credit = FakeCredit(self.events)
        self.cache = FakeCache()
        self.result = chat_response()
        self.response_status = 200
        self.settings = SimpleNamespace(remote_models=True)
        self.guard = lambda *args: self.events.append(("guard",)) or True
        self.adapters = []

    def tearDown(self):
        for adapter in self.adapters:
            adapter.close()

    def transport(self, request):
        self.events.append(("http",))
        self.requests.append(request)
        return httpx.Response(self.response_status, json=self.result)

    def chat(self, **kwargs):
        options = {"env": dict(ENV), "usage_budget": self.budget, "credit_budget": self.credit,
                   "transport": httpx.MockTransport(self.transport)}
        options.update(kwargs)
        adapter = GMSStructuredModel(self.settings, self.guard, **options)
        self.adapters.append(adapter)
        return adapter

    def embedding(self, **kwargs):
        options = {"env": dict(ENV), "usage_budget": self.budget, "credit_budget": self.credit,
                   "embedding_cache": self.cache, "transport": httpx.MockTransport(self.transport)}
        options.update(kwargs)
        adapter = GMSEmbeddings(self.settings, self.guard, **options)
        self.adapters.append(adapter)
        return adapter

    def scope(self, adapter, purpose="verification"):
        return adapter.live_execution(purpose, "synthetic-approval")

    def assert_code(self, code, callback):
        with self.assertRaises(ServiceError) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)
        self.assertNotIn(ENV["GMS_API_KEY"], str(raised.exception) + raised.exception.message)

    def test_startup_metadata_refresh_and_processing_context_never_grant_live(self):
        model = self.chat()
        self.assertFalse(model.metadata()["can_generate"])
        self.assert_code("MODEL_REMOTE_BLOCKED", lambda: model.generate("query", prompt()))
        embedding = self.embedding()
        with embedding.processing_context(CONTEXTS):
            self.assert_code("MODEL_REMOTE_BLOCKED", lambda: embedding.embed(["합성 문장"]))
        self.assertEqual(self.events, [])
        self.assertIsNone(model._gateway._client)
        self.assertIsNone(embedding._gateway._client)

    def test_exact_chat_wire_and_source_budget_transport_order(self):
        model = self.chat()
        with self.scope(model):
            self.assertEqual(model.generate("query", prompt()), self.result["choices"][0]["message"]["content"])
        request = self.requests[0]
        self.assertEqual(str(request.url), GMS_BASE_URL + "/chat/completions")
        self.assertEqual(request.headers["authorization"], "Bearer " + ENV["GMS_API_KEY"])
        body = json.loads(request.content)
        self.assertEqual(set(body), {"model", "messages", "max_tokens", "stream", "store"})
        self.assertIs(body["stream"], False)
        self.assertIs(body["store"], False)
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertEqual(json.loads(body["messages"][1]["content"])["input"]["question"], "합성 질문")
        self.assertEqual([row for row in self.events if row[0] != "guard"],
                         [("estimate",), ("consume", "chat", len(request.content)),
                          ("reserve",), ("http",), ("settle",)])
        self.assertTrue(all(self.events[index - 1][0] == "guard" for index, row in enumerate(self.events) if row[0] == "http"))
        self.assertEqual(body["max_tokens"], 512)

    def test_no_openai_key_model_or_base_fallback(self):
        model = self.chat(env={"OPENAI_API_KEY": "not-gms", "CHAT_MODEL": "gpt-4.1-mini", "OPENAI_BASE_URL": GMS_BASE_URL})
        with self.scope(model):
            self.assert_code("MODEL_REMOTE_BLOCKED", lambda: model.generate("query", prompt()))
        self.assertEqual(self.events, [])

    def test_invalid_origin_and_model_route_combinations_fail_closed(self):
        cases = [{"GMS_BASE_URL": "https://gms.ssafy.io.evil.example/gmsapi/api.openai.com/v1"},
                 {"GMS_BASE_URL": GMS_BASE_URL + "?redirect=other"}, {"GMS_BASE_URL": "http://gms.ssafy.io/gmsapi/api.openai.com/v1"},
                 {"GMS_BASE_URL": "https://key@gms.ssafy.io/gmsapi/api.openai.com/v1"}, {"GMS_BASE_URL": GMS_BASE_URL + "//"},
                 {"GMS_CHAT_MODEL": "catalog-only-model"}, {"GMS_CHAT_ROUTE": "//chat/completions"},
                 {"GMS_CHAT_ROUTE": "responses", "GMS_CHAT_MODEL": "gpt-4.1-mini"}, {"GMS_CHAT_ROUTE": ""}]
        for changes in cases:
            with self.subTest(changes=changes):
                model = self.chat(env={**ENV, **changes})
                with self.scope(model):
                    self.assert_code("MODEL_REMOTE_BLOCKED", lambda: model.generate("query", prompt()))
        self.assertEqual(self.events, [])

    def test_trailing_base_slash_and_leading_route_slash_normalize_only(self):
        model = self.chat(env={**ENV, "GMS_BASE_URL": GMS_BASE_URL + "/", "GMS_CHAT_ROUTE": "/chat/completions"})
        with self.scope(model):
            model.generate("query", prompt())
        self.assertEqual(str(self.requests[0].url), GMS_BASE_URL + "/chat/completions")

    def test_model_specific_documented_developer_role(self):
        model = self.chat(env={**ENV, "GMS_CHAT_MODEL": "gpt-4o-mini"})
        self.result = chat_response(model="gpt-4o-mini")
        with self.scope(model):
            model.generate("query", prompt())
        self.assertEqual(json.loads(self.requests[0].content)["messages"][0]["role"], "developer")

    def test_scope_is_explicit_approval_matched_and_restored_after_exception(self):
        model = self.chat()
        self.assert_code("INVALID_INPUT", lambda: model.live_execution("ui", "synthetic-approval").__enter__())
        self.assert_code("PROCESSING_APPROVAL_REQUIRED", lambda: model.live_execution("verification", "other").__enter__())
        with self.assertRaises(RuntimeError):
            with self.scope(model):
                raise RuntimeError("synthetic")
        self.assert_code("MODEL_REMOTE_BLOCKED", lambda: model.generate("query", prompt()))
        self.assertEqual(self.events, [])

    def test_live_context_does_not_cross_thread_or_adapter(self):
        model, other = self.chat(), self.chat()
        errors = []
        with self.scope(model):
            thread = threading.Thread(target=lambda: errors.append(self._thread_error(model)))
            thread.start()
            thread.join()
            self.assert_code("MODEL_REMOTE_BLOCKED", lambda: other.generate("query", prompt()))
        self.assertEqual(errors, ["MODEL_REMOTE_BLOCKED"])
        self.assertEqual(self.events, [])

    def _thread_error(self, model):
        try:
            model.generate("query", prompt())
        except ServiceError as exc:
            return exc.code

    def test_guard_denial_and_invalid_prompt_consume_nothing(self):
        self.guard = lambda *args: False
        model = self.chat()
        with self.scope(model):
            self.assert_code("MODEL_REMOTE_BLOCKED", lambda: model.generate("query", prompt()))
            self.assert_code("MODEL_REMOTE_BLOCKED", lambda: model.generate("query", "{}"))
        self.assertEqual(self.events, [])

    def test_missing_or_unready_usage_budget_is_readonly_deny(self):
        model = self.chat(usage_budget=None)
        self.assert_code("PROCESSING_APPROVAL_REQUIRED", lambda: self.scope(model).__enter__())
        self.budget.ready = False
        model = self.chat()
        with self.scope(model):
            self.assert_code("PROCESSING_APPROVAL_REQUIRED", lambda: model.generate("query", prompt()))
        self.assertEqual(self.events, [])

    def test_atomic_quota_error_is_preserved_and_no_retry_after_failure(self):
        self.budget.limit = 1
        self.response_status = 503
        model = self.chat()
        with self.scope(model):
            self.assert_code("MODEL_UNAVAILABLE", lambda: model.generate("query", prompt()))
            self.assert_code("USAGE_LIMIT_REACHED", lambda: model.generate("query", prompt()))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.budget.count, 1)

    def test_redirect_error_refusal_tools_and_truncation_are_safe(self):
        bad = [chat_response(choices=[]), chat_response(choices=[{"index": 0, "finish_reason": "length", "message": {"role": "assistant", "content": "{}"}}]),
               chat_response(choices=[{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "{}", "refusal": "secret"}}]),
               chat_response(choices=[{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "{}", "tool_calls": [{"id": "x"}]}}]),
               chat_response(content=""), chat_response(content="x" * 131073), chat_response(error={"message": "secret"})]
        model = self.chat()
        with self.scope(model):
            for result in bad:
                self.result = result
                self.assert_code("MODEL_UNAVAILABLE", lambda: model.generate("query", prompt()))
            self.response_status = 302
            self.assert_code("MODEL_UNAVAILABLE", lambda: model.generate("query", prompt()))
        self.assertEqual(len(self.requests), len(bad) + 1)

    def test_response_bounds_duplicate_json_and_nonfinite_fail(self):
        def response(request):
            return httpx.Response(200, content=b'{"model":"x","model":"y"}', headers={"content-type": "application/json"})
        model = self.chat(transport=httpx.MockTransport(response))
        with self.scope(model):
            self.assert_code("MODEL_UNAVAILABLE", lambda: model.generate("query", prompt()))
        for body in (b'{"x":NaN}', b"x" * 2097153):
            model = self.chat(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body, headers={"content-type": "application/json"})))
            with self.scope(model):
                self.assert_code("MODEL_UNAVAILABLE", lambda: model.generate("query", prompt()))

    def test_metadata_has_actual_wire_fields_and_no_unsupported_retention_claim(self):
        model = self.chat()
        metadata = model.metadata()
        self.assertEqual(metadata["provider"], "gms")
        self.assertFalse(metadata["live_verified"])
        self.assertEqual(metadata["retention_policy"], "NOT_VERIFIED")
        self.assertIs(metadata["generation_settings"]["store"], False)
        self.assertNotIn("text", metadata["generation_settings"])
        self.assertNotIn(ENV["GMS_API_KEY"], json.dumps(metadata))
        with self.scope(model):
            self.assertTrue(model.metadata()["can_generate"])

    def test_embedding_each_string_exact_wire_order_and_output_dimension(self):
        self.result = {"model": "text-embedding-3-small", "data": [{"index": 0, "embedding": [1, 0.5]}]}
        model = self.embedding()
        with self.scope(model, "embedding_preparation"), model.processing_context(CONTEXTS):
            self.assertEqual(model.embed(["합성 하나", "합성 둘"]), [[1.0, 0.5], [1.0, 0.5]])
        self.assertEqual(len(self.requests), 2)
        for index, request in enumerate(self.requests):
            self.assertEqual(str(request.url), GMS_BASE_URL + "/embeddings")
            self.assertEqual(json.loads(request.content), {"model": "text-embedding-3-small", "input": ["합성 하나", "합성 둘"][index]})
        self.assertEqual([row[2] for row in self.events if row[0] == "consume"], [len(row.content) for row in self.requests])
        self.assertEqual(model.metadata()["generation_settings"], {})
        self.assertTrue(model.name.startswith("gms:"))

    def test_embedding_rechecks_full_context_each_actual_request(self):
        self.result = {"model": "text-embedding-3-small", "data": [{"index": 0, "embedding": [1, 0]}]}
        calls = []
        def guard(texts, contexts):
            calls.append((texts, contexts))
            return len(self.requests) <= 1
        previous_transport = self.transport
        def revoke_after_first(request):
            response = previous_transport(request)
            model._guard = lambda *args: False
            return response
        self.guard = guard
        model = self.embedding(transport=httpx.MockTransport(revoke_after_first))
        with self.scope(model), model.processing_context(CONTEXTS):
            self.assert_code("MODEL_REMOTE_BLOCKED", lambda: model.embed(["first", "second"]))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.budget.count, 1)
        self.assertTrue(all(row == (["first", "second"], CONTEXTS) for row in calls))

    def test_embedding_context_and_input_validation_precede_budget(self):
        model = self.embedding()
        with self.scope(model):
            self.assert_code("MODEL_REMOTE_BLOCKED", lambda: model.embed(["text"]))
            with model.processing_context(CONTEXTS):
                self.assert_code("EMBEDDING_INPUT_INVALID", lambda: model.embed(["x" * 8193]))
                self.assertEqual(model.embed([]), [])
        self.assertEqual(self.events, [])

    def test_embedding_malformed_vectors_index_and_model_fail(self):
        model = self.embedding()
        results = [{"model": "text-embedding-3-small", "data": [{"index": index, "embedding": vector}]}
                   for index, vector in [(True, [1, 0]), (1, [1, 0]), (0, [1]), (0, [True, 0]), (0, ["1", 0])]]
        results.append({"model": None, "data": [{"index": 0, "embedding": [1, 0]}]})
        with self.scope(model), model.processing_context(CONTEXTS):
            for result in results:
                self.result = result
                with self.assertRaises(ServiceError):
                    model.embed(["text"])
        self.assertEqual(len(self.requests), len(results))

    def test_transport_settings_disable_proxy_redirect_and_retries(self):
        model = self.chat()
        with self.scope(model):
            model.generate("query", prompt())
        client = model._gateway._client
        self.assertFalse(client.trust_env)
        self.assertFalse(client.follow_redirects)
        self.assertEqual(client.timeout.connect, 5.0)
        self.assertEqual(client.timeout.read, 20.0)
        self.assertEqual(model.metadata()["transport_settings"]["max_retries"], 0)

    def test_closing_adapter_does_not_close_injected_client(self):
        client = httpx.Client(transport=httpx.MockTransport(self.transport), trust_env=False)
        try:
            model = self.chat(client=client)
            with self.scope(model):
                model.generate("query", prompt())
            model.close()
            self.assertFalse(client.is_closed)
        finally:
            client.close()

    def test_credit_profile_unconfirmed_or_absent_stops_before_source_and_http(self):
        model = self.chat(credit_budget=None)
        self.assert_code("PROCESSING_APPROVAL_REQUIRED", lambda: self.scope(model).__enter__())
        self.credit.ready = False
        model = self.chat()
        self.assertFalse(model.metadata()["configuration_ready"])
        with self.scope(model):
            self.assert_code("LIVE_BUDGET_UNCONFIRMED", lambda: model.generate("query", prompt()))
        self.assertEqual(self.events, [])

    def test_credit_refusal_preserved_before_http_and_request_attempt_not_refunded(self):
        def denied(*args, **kwargs):
            raise ServiceError("CREDIT_LIMIT_REACHED", "Safe credits", 429)
        self.credit.reserve = denied
        model = self.chat()
        with self.scope(model):
            self.assert_code("CREDIT_LIMIT_REACHED", lambda: model.generate("query", prompt()))
        self.assertEqual(self.budget.count, 1)
        self.assertEqual(self.requests, [])
        self.assertIsNone(model._gateway._client)

    def test_missing_explicit_output_limit_and_responses_have_no_live_requests(self):
        cases = [{"GMS_MAX_OUTPUT_TOKENS": value} for value in ("", "0", "4097", "true", " 512")]
        cases.append({"GMS_CHAT_ROUTE": "responses", "GMS_CHAT_MODEL": "gpt-4.1"})
        for changes in cases:
            model = self.chat(env={**ENV, **changes})
            self.assertFalse(model.metadata()["configuration_ready"])
            with self.scope(model):
                self.assert_code("MODEL_REMOTE_BLOCKED", lambda: model.generate("query", prompt()))
        self.assertEqual(self.events, [])

    def test_failed_and_unknown_requests_retain_reservation_and_actual_usage_separate(self):
        model = self.chat()
        with self.scope(model):
            self.response_status = 503
            self.assert_code("MODEL_UNAVAILABLE", lambda: model.generate("query", prompt()))
            self.response_status = 200
            self.result = chat_response(usage={"prompt_tokens": 7, "completion_tokens": 3, "credits": 0.001})
            model.generate("query", prompt())
        self.assertEqual(len(self.credit.reservations), 2)
        self.assertNotEqual(self.credit.reservations[0]["request_id"], self.credit.reservations[1]["request_id"])
        self.assertEqual(self.credit.reservations[0]["route"], "chat.completions")
        self.assertEqual(self.credit.reservations[0]["output_tokens_bound"], 512)
        self.assertEqual(self.credit.settlements[0]["usage"], None)
        self.assertEqual(self.credit.settlements[1]["usage"], {"input_tokens": 7, "output_tokens": 3})
        self.assertTrue(all(row["actual_credits"] is None for row in self.credit.settlements))

    def test_replayed_credit_reservation_must_never_repeat_http(self):
        self.credit.reserve = lambda *args, **kwargs: {"replayed": True, "state": "RESERVED"}
        model = self.chat()
        with self.scope(model):
            self.assert_code("LIVE_BUDGET_UNCONFIRMED", lambda: model.generate("query", prompt()))
        self.assertEqual(self.requests, [])

    def test_persistent_cache_and_request_budget_cross_adapter_reuse(self):
        self.result = {"model": "text-embedding-3-small", "data": [{"index": 0, "embedding": [1, 0]}]}
        with tempfile.TemporaryDirectory() as directory:
            usage_path, cache_path = Path(directory) / "usage.sqlite3", Path(directory) / "cache.sqlite3"
            limits = {"chat_requests": 10, "embedding_requests": 10, "input_bytes": 100000}
            budget = UsageBudget(usage_path, "synthetic-approval", limits, context_fingerprint="1" * 64)
            first = self.embedding(usage_budget=budget, embedding_cache=EmbeddingCache(cache_path))
            second = self.embedding(usage_budget=budget, embedding_cache=EmbeddingCache(cache_path))
            self.assertFalse(usage_path.exists())
            for model in (first, second):
                with self.scope(model), model.processing_context(CONTEXTS):
                    self.assertEqual(model.embed(["SYNTHETIC document body"]), [[1.0, 0.0]])
            self.assertEqual(len(self.requests), 1)
            self.assertEqual(len(self.credit.reservations), 1)
            self.assertEqual(budget.snapshot()["embedding_requests"], 1)
            self.assertIsNone(second.metadata()["provider_reported_model"])
            self.assertNotIn(b"SYNTHETIC document body", cache_path.read_bytes())
            self.assertNotIn(ENV["GMS_API_KEY"].encode(), cache_path.read_bytes() + usage_path.read_bytes())

    def test_cache_hit_requires_live_scope_and_current_rights_but_charges_zero(self):
        self.result = {"model": "text-embedding-3-small", "data": [{"index": 0, "embedding": [1, 0]}]}
        model = self.embedding()
        with self.scope(model), model.processing_context(CONTEXTS):
            model.embed(["text"])
        self.assertEqual(self.budget.count, 1)
        with model.processing_context(CONTEXTS):
            self.assert_code("MODEL_REMOTE_BLOCKED", lambda: model.embed(["text"]))
        model._guard = lambda *args: False
        with self.scope(model), model.processing_context(CONTEXTS):
            self.assert_code("MODEL_REMOTE_BLOCKED", lambda: model.embed(["text"]))
        model._guard = lambda *args: True
        with self.scope(model), model.processing_context(CONTEXTS):
            self.assertEqual(model.embed(["text"]), [[1.0, 0.0]])
        self.assertEqual(self.budget.count, 1)
        self.assertEqual(len(self.credit.reservations), 1)

    def test_cache_corruption_or_busy_never_causes_paid_fallback(self):
        model = self.embedding()
        for code in ("EMBEDDING_CACHE_CORRUPT", "EMBEDDING_CACHE_IN_PROGRESS"):
            def denied(*args, **kwargs):
                raise ServiceError(code, "Safe cache", 503)
            self.cache.get = denied
            with self.scope(model), model.processing_context(CONTEXTS):
                self.assert_code(code, lambda: model.embed(["text"]))
        self.assertEqual(self.requests, [])
        self.assertEqual(self.budget.count, 0)

    def test_model_version_and_dimensions_separate_cache_identity(self):
        self.result = {"model": "text-embedding-3-small", "data": [{"index": 0, "embedding": [1, 0]}]}
        first = self.embedding(requested_model_version="operator-v1")
        second = self.embedding(requested_model_version="operator-v2")
        for model in (first, second):
            with self.scope(model), model.processing_context(CONTEXTS):
                model.embed(["text"])
        self.assertEqual(len(self.requests), 2)
        third = self.embedding(env={**ENV, "GMS_EMBEDDING_DIMENSION": "3"})
        self.result = {"model": "text-embedding-3-small", "data": [{"index": 0, "embedding": [1, 0, 0]}]}
        with self.scope(third), third.processing_context(CONTEXTS):
            self.assertEqual(third.embed(["text"]), [[1.0, 0.0, 0.0]])
        self.assertEqual(len(self.requests), 3)

    def test_accidentally_configured_key_as_identifier_is_not_public(self):
        model = self.chat(env={**ENV, "GMS_CHAT_MODEL": ENV["GMS_API_KEY"], "GMS_CHAT_MODEL_VERSION": ENV["GMS_API_KEY"]})
        self.assertNotIn(ENV["GMS_API_KEY"], json.dumps(model.metadata()))

    def source_policy(self):
        record = {"source_id": "SYNTHETIC", "action": "external_process", "status": "ALLOWED",
                  "basis_type": "OTHER_RECORDED_BASIS", "basis_url": "https://example.org/",
                  "reviewed_on": "2026-01-01", "scope": "Synthetic fixture", "conditions": "Offline",
                  "user_scope": "Synthetic buyer", "processing_environment": "Mock transport", "version_id": "v1"}
        policy = SourcePolicy([{"source_id": "SYNTHETIC", "url": "https://example.org/terms"}], [record],
                              capabilities={"manager": {"DATA_MANAGER"}})
        return policy, record

    def test_actual_source_revocation_during_reservation_blocks_both_adapters(self):
        policy, record = self.source_policy()
        self.guard = lambda *args: policy.allows("SYNTHETIC", "external_process", actor="buyer", version_id="v1")
        reserve = self.credit.reserve
        def revoke(*args, **kwargs):
            result = reserve(*args, **kwargs)
            policy.set_review("manager", {**record, "status": "BLOCKED"})
            return result
        self.credit.reserve = revoke
        chat, embedding = self.chat(), self.embedding()
        with self.scope(chat):
            self.assert_code("MODEL_REMOTE_BLOCKED", lambda: chat.generate("query", prompt()))
        policy.set_review("manager", record)
        with self.scope(embedding), embedding.processing_context(CONTEXTS):
            self.assert_code("MODEL_REMOTE_BLOCKED", lambda: embedding.embed(["text"]))
        self.assertEqual(self.requests, [])
        self.assertEqual(self.budget.count, 2)
        self.assertEqual(len(self.credit.reservations), 2)
        self.assertTrue(all(row["usage"] is None for row in self.credit.settlements))
        self.assertEqual(self.cache.rows, {})
        self.assertIsNone(chat._gateway._client)
        self.assertIsNone(embedding._gateway._client)

    def test_revocation_while_receiving_does_not_publish_or_cache_output(self):
        for embedding in (False, True):
            with self.subTest(embedding=embedding):
                policy, record = self.source_policy()
                self.guard = lambda *args: policy.allows("SYNTHETIC", "external_process", actor="buyer", version_id="v1")
                self.result = ({"model": "text-embedding-3-small", "data": [{"index": 0, "embedding": [1, 0]}]}
                               if embedding else chat_response())
                def receive_then_revoke(request):
                    response = self.transport(request)
                    policy.set_review("manager", {**record, "status": "BLOCKED"})
                    return response
                model = self.embedding(transport=httpx.MockTransport(receive_then_revoke)) if embedding else self.chat(transport=httpx.MockTransport(receive_then_revoke))
                with self.scope(model):
                    if embedding:
                        with model.processing_context(CONTEXTS):
                            self.assert_code("MODEL_REMOTE_BLOCKED", lambda: model.embed(["text"]))
                    else:
                        self.assert_code("MODEL_REMOTE_BLOCKED", lambda: model.generate("query", prompt()))
                self.assertEqual(self.cache.rows, {})
                self.assertIsNone(model.metadata()["provider_reported_model"])
        self.assertEqual(len(self.requests), 2)

    def test_final_real_usage_slot_is_sent_once_for_both_adapters(self):
        with tempfile.TemporaryDirectory() as directory:
            for embedding in (False, True):
                with self.subTest(embedding=embedding):
                    budget = UsageBudget(Path(directory) / f"quota-{embedding}.sqlite3", "synthetic-approval",
                        {"chat_requests": 0 if embedding else 1, "embedding_requests": 1 if embedding else 0,
                         "input_bytes": 100000}, context_fingerprint="2" * 64)
                    self.result = ({"model": "text-embedding-3-small", "data": [{"index": 0, "embedding": [1, 0]}]}
                                   if embedding else chat_response())
                    model = self.embedding(usage_budget=budget) if embedding else self.chat(usage_budget=budget)
                    with self.scope(model):
                        if embedding:
                            with model.processing_context(CONTEXTS):
                                self.assertEqual(model.embed(["last permitted slot"]), [[1.0, 0.0]])
                                self.assert_code("USAGE_LIMIT_REACHED", lambda: model.embed(["new text"]))
                        else:
                            model.generate("query", prompt())
                            self.assert_code("USAGE_LIMIT_REACHED", lambda: model.generate("query", prompt()))
                    self.assertFalse(budget.ready)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(len(self.credit.reservations), 2)

    def test_reported_model_mismatch_and_missing_identity_freeze_and_cache_nothing(self):
        for reported in (None, "gpt-4.1", "unapproved-snapshot"):
            with self.subTest(reported=reported):
                self.credit.ready = True
                model = self.chat()
                self.result = chat_response(model=reported, usage={"prompt_tokens": 1, "completion_tokens": 2})
                with self.scope(model):
                    self.assert_code("LIVE_MODEL_IDENTITY_MISMATCH", lambda: model.generate("query", prompt()))
                    self.assert_code("LIVE_BUDGET_UNCONFIRMED", lambda: model.generate("query", prompt()))
                self.assertEqual(self.credit.settlements[-1]["usage"], {"input_tokens": 1, "output_tokens": 2})
        self.credit.ready = True
        embedding = self.embedding()
        self.result = {"model": "text-embedding-3-large", "data": [{"index": 0, "embedding": [1, 0]}]}
        with self.scope(embedding), embedding.processing_context(CONTEXTS):
            self.assert_code("LIVE_MODEL_IDENTITY_MISMATCH", lambda: embedding.embed(["text"]))
        self.assertEqual(self.cache.rows, {})
        self.assertEqual(len(self.requests), 4)

    def test_only_explicit_approved_reported_alias_is_accepted(self):
        self.credit.allowed_reported_models = lambda kind, model, route: frozenset({model, "synthetic-approved-snapshot"})
        self.result = chat_response(model="synthetic-approved-snapshot")
        model = self.chat()
        with self.scope(model):
            model.generate("query", prompt())
        self.assertEqual(model.metadata()["provider_reported_model"], "synthetic-approved-snapshot")
        self.assertIsNone(model.metadata()["model_version"])
        self.assertTrue(self.credit.ready)


if __name__ == "__main__":
    unittest.main()
