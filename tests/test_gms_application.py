"""Real application composition with invented private inputs and zero wire I/O."""
from contextlib import ExitStack, closing
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from starlette.requests import Request

from warranty.application import build_application
from warranty.catalog import Catalog
from warranty.config import Settings
from warranty.documents import ACTIONS
from warranty.errors import ServiceError
from warranty.gms import GMSEmbeddings, GMSStructuredModel
from warranty.public_gms import GMSPublicStructuredModel
from warranty.public_protocol import public_instruction_hashes, public_source_sha256
from warranty.interfaces.api import COOKIE_NAME, create_app
from warranty.live import LIVE_PURPOSES
from warranty.prompts import output_schema


APPROVAL = "SYNTHETIC-factory-approval"
KEY_A, KEY_B = "SYNTHETIC-NOT-A-CREDENTIAL-A", "SYNTHETIC-NOT-A-CREDENTIAL-B"
BASE = "https://gms.ssafy.io/gmsapi/api.openai.com/v1"


def configuration():
    return {"models": {"chat_provider": "gms", "chat_model": "gpt-4.1-mini",
        "chat_model_version": "SYNTHETIC-configured-chat-version", "embedding_provider": "gms",
        "embedding_model": "text-embedding-3-small", "embedding_model_version": "SYNTHETIC-configured-embedding-version",
        "embedding_dimension": 1536, "gms_base_url": BASE, "gms_chat_route": "chat/completions",
        "gms_max_output_tokens": 128, "external_processing_approved": True,
        "allow_question_processing": True, "usage_approval_id": APPROVAL,
        "usage_limits": {"chat_requests": 4, "embedding_requests": 4, "input_bytes": 100000}},
        "collection": {"manager_actor": "manager", "worker_enabled": False}}


def synthetic_policy():
    sources = Catalog.synthetic().sources
    return {"schema_version": 1,
        "registry": [{"source_id": source_id, "url": row["url"], "is_synthetic": True}
                     for source_id, row in sources.items()],
        "reviews": [{"source_id": source_id, "action": action, "status": "ALLOWED",
            "basis_type": "OTHER_RECORDED_BASIS", "basis_url": "https://example.invalid/synthetic-review",
            "reviewed_on": "2026-01-01", "scope": "Authored synthetic factory test",
            "conditions": "No real provider or document processing", "user_scope": "Named test actors",
            "processing_environment": "Local synthetic test", "version_scope": "SOURCE",
            "allowed_actors": ["manager", "buyer"]} for source_id in sources for action in ACTIONS],
        "capabilities": {"manager": ["DATA_MANAGER"]}}


def invented_credit_profile():
    """Arbitrary test arithmetic, explicitly unrelated to provider prices."""
    common = {"input_bound": "wire_utf8_bytes", "input_bound_confirmed": True,
        "input_token_overhead": 0, "max_input_tokens": 100000, "output_bound_confirmed": True,
        "input_microcredits_per_token": 13}
    return {"profile_id": "SYNTHETIC-factory-profile", "pricing_confirmed": True,
        "pricing_version": "SYNTHETIC-INVENTED-NOT-REAL-PRICES",
        "allocations": {purpose: "1000" for purpose in LIVE_PURPOSES}, "demo_reserve_credits": "100",
        "models": {"gpt-4.1-mini": {**common, "kind": "chat", "route": "chat.completions",
            "output_microcredits_per_token": 17, "output_bound": "max_tokens", "max_output_tokens": 128},
            "text-embedding-3-small": {**common, "kind": "embedding", "route": "embeddings",
                "output_microcredits_per_token": 0, "output_bound": "none", "max_output_tokens": 0}}}


class GMSApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="warranty-gms-factory-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.settings = Settings(self.base / "data", self.base / "config", remote_models=True)
        self.settings.prepare()
        self.apps = []
        self.addCleanup(self._close_apps)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {"METADATA_PATH": "", "METADATA_EXPECTED_HASH": "",
            "PRIVATE_DATA_DIR": str(self.settings.data_dir), "PRIVATE_CONFIG_DIR": str(self.settings.config_dir),
            "ENABLE_REMOTE_MODELS": "true", "OPENAI_API_KEY": "", "CHAT_MODEL": "", "CHAT_MODEL_VERSION": "",
            "EMBEDDING_MODEL": "", "EMBEDDING_MODEL_VERSION": "", "EMBEDDING_DIMENSION": "",
            "GMS_API_KEY": "", "GMS_BASE_URL": "", "GMS_CHAT_MODEL": "", "GMS_CHAT_MODEL_VERSION": "",
            "GMS_CHAT_ROUTE": "", "GMS_EMBEDDING_MODEL": "", "GMS_EMBEDDING_MODEL_VERSION": "",
            "GMS_EMBEDDING_DIMENSION": "", "GMS_MAX_OUTPUT_TOKENS": "",
            "LANGSMITH_TRACING": "false", "LANGCHAIN_TRACING_V2": "false", "ANONYMIZED_TELEMETRY": "false"}))
        self.stack.enter_context(patch("socket.create_connection", side_effect=AssertionError("No network in factory verification")))
        self.stack.enter_context(patch("socket.socket.connect", side_effect=AssertionError("No network in factory verification")))
        self.wire = self.stack.enter_context(patch("warranty.gms._Gateway.request", side_effect=AssertionError("GMS wire forbidden")))
        self.transport = self.stack.enter_context(patch("httpx.HTTPTransport", side_effect=AssertionError("HTTP transport forbidden")))
        self.sdk_chat = self.stack.enter_context(patch("warranty.prompts._official_sdk_client", side_effect=AssertionError("SDK forbidden")))
        self.sdk_embedding = self.stack.enter_context(patch("warranty.embeddings._official_sdk_client", side_effect=AssertionError("SDK forbidden")))
        self.fetch = self.stack.enter_context(patch("warranty.security.SafeFetcher.fetch", side_effect=AssertionError("Source HTTP forbidden")))
        self.embed = self.stack.enter_context(patch.object(GMSEmbeddings, "embed", autospec=True, side_effect=GMSEmbeddings.embed))
        self.generate = self.stack.enter_context(patch.object(GMSPublicStructuredModel, "generate", autospec=True,
                                                               side_effect=GMSPublicStructuredModel.generate))
        self.legacy_generate = self.stack.enter_context(patch.object(GMSStructuredModel, "generate", autospec=True,
            side_effect=AssertionError("Legacy private GMS wire is forbidden in the product factory")))

    def _close_apps(self):
        for app in reversed(self.apps):
            app.close()

    def write(self, filename, value):
        (self.settings.config_dir / filename).write_text(json.dumps(value), encoding="utf-8")

    def build(self, runtime=None, *, policy=False, key="", remote=True, worker=False):
        runtime = deepcopy(configuration() if runtime is None else runtime)
        if "gms" in {runtime.get("models", {}).get(slot) for slot in ("chat_provider", "embedding_provider")}:
            # Content declarations are not source-rights or execution approval.
            # This invented, unregistered source is never fetched or generated.
            public = {"title": "Fictional public factory terms", "url": "https://example.invalid/public-factory",
                      "excerpt": "This invented factory fixture requires a public transfer confirmation."}
            bundle = {"schema_version": 1,
                "item_ids": [Catalog.synthetic().items[0]["item_id"]],
                "sources": [{"source_id": "SYNTHETIC-factory-source", "version_id": "SYNTHETIC-factory-version", **public}],
                "embedding_document_sha256s": [hashlib.sha256(public["excerpt"].encode()).hexdigest()],
                "protocol": {"schema_version": 1, "model": "gpt-4.1-mini", "route": "chat/completions",
                    "instruction_sha256s": public_instruction_hashes(),
                    "question_sha256s": [hashlib.sha256(b"Synthetic factory public question").hexdigest()],
                    "source_sha256s": [public_source_sha256(public)],
                    "max_request_bytes": 16384, "max_output_tokens": 128}}
            self.write("public_protocol.synthetic.json", bundle)
            runtime["models"]["public_protocol_path"] = "public_protocol.synthetic.json"
        self.write("runtime.json", runtime)
        if policy:
            self.write("source_policy.json", synthetic_policy())
        with patch.dict(os.environ, {"GMS_API_KEY": key}):
            app = build_application(Settings(self.settings.data_dir, self.settings.config_dir, remote_models=remote),
                                    start_document_worker=worker)
        self.apps.append(app)
        return app

    def close(self, app):
        self.assertIs(app.close(), True)
        self.apps.remove(app)

    def assert_no_external(self):
        for spy in (self.wire, self.transport, self.sdk_chat, self.sdk_embedding, self.fetch, self.legacy_generate):
            spy.assert_not_called()

    def assert_no_budget_reservations(self, app):
        usage = app.models.usage_budget.snapshot()
        credit = app.models.credit_budget.snapshot()
        self.assertEqual((usage["chat_requests"], usage["embedding_requests"], usage["input_bytes"]), (0, 0, 0))
        self.assertEqual(credit["request_count"], 0)
        self.assertEqual(credit["reserved_upper_credits"], "0.000000" if app.models.credit_budget.confirmed else None)
        self.assertIsNone(credit["provider_reported_actual_credits"])
        self.assertFalse((self.settings.data_dir / "model_usage.sqlite3").exists())
        self.assertFalse((self.settings.data_dir / "model_credits.sqlite3").exists())

    def assert_public(self, value):
        encoded = json.dumps(value)
        for marker in (str(self.base), KEY_A, KEY_B, APPROVAL, "GMS_API_KEY", "live_budget.json", "context_fingerprint"):
            self.assertNotIn(marker, encoded)

    def assert_code(self, code, function):
        with self.assertRaises(ServiceError) as caught:
            function()
        self.assertEqual(caught.exception.code, code)
        self.assert_public(caught.exception.as_dict())

    def write_query_prompt(self):
        folder = self.settings.config_dir / "prompts"
        folder.mkdir(exist_ok=True)
        value = {"version": "1.0.0", "kind": "query", "role": "Synthetic factory fixture",
            "input_variables": ["item", "question", "applicability"], "evidence_limits": ["Synthetic fixture only"],
            "output_schema": output_schema("query"), "missing_information_rules": ["Synthetic fixture"],
            "conflict_rules": ["Synthetic fixture"], "attack_rules": ["Synthetic fixture"], "template": "Synthetic fixture"}
        (folder / "query.json").write_text(json.dumps(value), encoding="utf-8")

    def test_factory_binds_exact_gms_adapters_shared_budgets_and_cache(self):
        app = self.build()
        chat, embedding = app.models.chat.adapter, app.models.embedding
        self.assertIsInstance(chat, GMSPublicStructuredModel)
        self.assertTrue(chat.public_protocol_only)
        self.assertEqual(app.models.chat.format_repair_attempts, 0)
        self.assertTrue(chat.cache_identity.startswith("gms-public-v1:"))
        self.assertIsInstance(embedding, GMSEmbeddings)
        self.assertIs(app.models.chat, app.rag.model)
        self.assertIs(app.rag.runner.model, app.models.chat)
        self.assertIs(embedding, app.rag.vectors.embeddings)
        for adapter in (chat, embedding):
            self.assertIs(adapter._usage_budget, app.models.usage_budget)
            self.assertIs(adapter._credit_budget, app.models.credit_budget)
            self.assertIsNone(adapter._gateway._client)
            self.assertIsNone(adapter._live_scope.get())
        self.assertIs(embedding._embedding_cache, app.models.embedding_cache)
        self.assertTrue(embedding.public_protocol_only)
        self.assertEqual(embedding.approved_document_sha256s, app.models.public_context.document_sha256s)
        self.assertIs(app.rag.vectors.metadata_embeddings, app.models.metadata_embedding)
        self.assertTrue(app.models.metadata_embedding.is_mock)
        self.assertEqual((chat.model_id, chat.route, chat.max_tokens), ("gpt-4.1-mini", "chat/completions", 128))
        self.assertEqual((embedding.model_id, embedding.dimension), ("text-embedding-3-small", 1536))
        # Public mode indexes catalog metadata locally, with its own mock
        # identity; it consumes no GMS embedding request or credit reservation.
        self.assertEqual(app.rag.metadata_count, len(app.catalog.items))
        self.embed.assert_not_called()
        self.generate.assert_not_called()
        self.assert_no_budget_reservations(app)
        self.assert_no_external()

    def test_missing_key_and_profile_keep_public_live_status_unready(self):
        app = self.build()
        for _ in range(2):
            status = app.runtime_status()
            self.assertEqual(status["live"], {"enabled": True, "ready": False,
                "purposes": list(LIVE_PURPOSES), "budget_confirmed": False})
            for slot in ("chat", "embedding"):
                self.assertEqual(status[slot], {"provider": "gms", "configured": False, "ready": False, "is_mock": False})
            self.assertEqual(app.summary()["runtime"]["live"], app.live_status())
            self.assert_public(status)
            self.assert_public(app.summary())
        self.assertFalse(app.models.credit_budget.confirmed)
        self.assertFalse(app.models.credit_budget.ready)
        self.embed.assert_not_called()
        self.generate.assert_not_called()
        self.assert_no_budget_reservations(app)
        self.assert_no_external()

    def test_synthetic_key_does_not_replace_missing_credit_profile(self):
        app = self.build(policy=True, key=KEY_A)
        self.assertTrue(app.runtime_status()["chat"]["configured"])
        self.assertFalse(app.live_status()["ready"])
        self.assert_code("PROCESSING_APPROVAL_REQUIRED", lambda: app.live_execution("verification", APPROVAL).__enter__())
        for adapter in (app.models.chat.adapter, app.models.embedding):
            self.assertFalse(adapter.metadata()["configuration_ready"])
            self.assertFalse(adapter.metadata()["can_generate"])
            self.assertIsNone(adapter._live_scope.get())
        self.assert_no_budget_reservations(app)
        self.assert_no_external()

    def test_normal_processing_scopes_never_grant_gms_live_scope(self):
        app = self.build(policy=True, key=KEY_A)
        item_id = app.catalog.items[0]["item_id"]
        with app.models.chat.processing_context("buyer", item_id):
            self.assert_code("MODEL_REMOTE_BLOCKED", lambda: app.models.chat.generate("query", "{}"))
        context = [{"kind": "question", "source_id": None, "version_id": None, "actor": "buyer"}]
        with app.models.embedding.processing_context(context):
            self.assert_code("MODEL_REMOTE_BLOCKED", lambda: app.models.embedding.embed(["Synthetic question"]))
        self.assert_no_budget_reservations(app)
        self.assert_no_external()

    def test_api_runtime_projects_live_status_without_granting_scope(self):
        app = self.build(key=KEY_A)
        api = create_app(app)
        session = api.state.sessions.create_demo("buyer", "127.0.0.1", "http://localhost")
        endpoint = next(route.endpoint for route in api.routes if getattr(route, "path", None) == "/runtime")
        for _ in range(2):
            request = Request({"type": "http", "method": "GET", "path": "/runtime",
                "headers": [(b"cookie", (COOKIE_NAME + "=" + session.id).encode("ascii"))],
                "state": {"request_id": "SYNTHETIC-runtime-status"}})
            # This route has no suspension points. Invoke its actual session and
            # DTO code without Windows event-loop socketpair or any transport.
            coroutine = endpoint(request)
            try:
                with self.assertRaises(StopIteration) as stopped:
                    coroutine.send(None)
            finally:
                coroutine.close()
            response = stopped.exception.value
            self.assertEqual(response.status_code, 200)
            result = json.loads(response.body)
            self.assertIsNone(result["error"])
            self.assertEqual(result["data"]["live"], app.live_status())
            self.assert_public(result)
        self.assertIsNone(app.models.chat.adapter._live_scope.get())
        self.assertIsNone(app.models.embedding._live_scope.get())
        self.embed.assert_not_called()
        self.generate.assert_not_called()
        self.assert_no_budget_reservations(app)
        self.assert_no_external()

    def test_source_default_deny_survives_remote_and_external_opt_in(self):
        app = self.build(key=KEY_A)
        item = app.catalog.items[0]
        self.assert_code("SOURCE_BLOCKED", lambda: app.rag.registry.policy.require(item["source_id"], "external_process",
                                                        actor="buyer", version_id=app.catalog.dataset_hash))
        with app.models.chat.processing_context("buyer", item["item_id"]):
            self.assertIs(app.authorization.model_guard("query", "item", item), False)
        self.assert_no_budget_reservations(app)
        self.assert_no_external()

    def test_external_approval_false_blocks_even_allowed_synthetic_source(self):
        runtime = configuration()
        runtime["models"]["external_processing_approved"] = False
        app = self.build(runtime, policy=True, key=KEY_A)
        item = app.catalog.items[0]
        with app.models.chat.processing_context("buyer", item["item_id"]):
            self.assertIs(app.authorization.model_guard("query", "item", item), False)
        self.assertEqual(app.live_status()["enabled"], False)
        self.assert_code("MODEL_REMOTE_BLOCKED", lambda: app.live_execution("demo", APPROVAL).__enter__())
        self.assert_no_budget_reservations(app)
        self.assert_no_external()

    def test_disabled_remote_setting_keeps_gms_unready(self):
        app = self.build(key=KEY_A, remote=False)
        self.assertFalse(app.live_status()["enabled"])
        self.assertFalse(app.runtime_status()["chat"]["ready"])
        self.assert_code("MODEL_REMOTE_BLOCKED", lambda: app.live_execution("evaluation", APPROVAL).__enter__())
        self.assert_no_budget_reservations(app)
        self.assert_no_external()

    def test_persisted_gms_job_reopens_and_runs_blocked_without_live_scope(self):
        runtime = configuration()
        runtime["collection"]["enabled"] = True
        self.write_query_prompt()
        first = self.build(runtime, policy=True)
        item_id = first.catalog.items[0]["item_id"]
        queued = first.rag.enqueue_prepare("buyer", item_id, question="Synthetic persisted GMS request")
        self.close(first)
        reopened = self.build(runtime, policy=True, key=KEY_A)
        self.assertEqual(reopened.rag.job("buyer", queued["job_id"])["status"], "QUEUED")
        result = reopened.document_jobs.run_pending()
        self.assertEqual(result["processed_jobs"], 1)
        final = reopened.rag.job("buyer", queued["job_id"])
        self.assertEqual((final["status"], final["failure_code"]), ("BLOCKED", "MODEL_REMOTE_BLOCKED"))
        self.assertIsNone(reopened.models.chat.adapter._live_scope.get())
        self.assert_no_budget_reservations(reopened)
        self.assert_no_external()

    def test_idle_gms_background_worker_has_no_live_scope_or_auto_embedding(self):
        runtime = configuration()
        runtime["collection"]["worker_enabled"] = True
        app = self.build(runtime, worker=True)
        self.assertTrue(app.document_jobs.worker_running)
        self.assertFalse(app.live_status()["ready"])
        self.close(app)
        self.embed.assert_not_called()
        self.generate.assert_not_called()
        self.assert_no_external()

    def test_reopening_with_changed_synthetic_key_does_not_reset_usage_binding(self):
        first = self.build(key=KEY_A)
        # Deliberate synthetic local usage mutation; no model/credit reservation.
        first.models.usage_budget.consume("chat", 7)
        self.assertEqual(first.models.usage_budget.snapshot()["chat_requests"], 1)
        with closing(sqlite3.connect(self.settings.data_dir / "model_usage.sqlite3")) as connection:
            original = connection.execute("SELECT context_fingerprint FROM model_usage_approvals").fetchone()[0]
        self.close(first)
        second = self.build(key=KEY_B)
        snapshot = second.models.usage_budget.snapshot()
        self.assertEqual(snapshot["chat_requests"], 1)
        self.assertEqual(snapshot["input_bytes"], 7)
        self.assertTrue(snapshot["approved"])
        self.assertEqual(snapshot["remaining"]["chat_requests"], 3)
        with closing(sqlite3.connect(self.settings.data_dir / "model_usage.sqlite3")) as connection:
            rows = connection.execute("SELECT * FROM model_usage_approvals").fetchall()
            self.assertEqual(connection.execute("SELECT context_fingerprint FROM model_usage_approvals").fetchone()[0], original)
        self.assertNotIn(KEY_A, repr(rows))
        self.assertNotIn(KEY_B, repr(rows))
        self.assertFalse((self.settings.data_dir / "model_credits.sqlite3").exists())
        self.assert_no_external()

    def test_usage_limit_change_requires_new_approval_after_existing_record(self):
        first = self.build()
        first.models.usage_budget.consume("embedding", 3)
        self.close(first)
        runtime = configuration()
        runtime["models"]["usage_limits"]["embedding_requests"] = 5
        second = self.build(runtime)
        snapshot = second.models.usage_budget.snapshot()
        self.assertFalse(snapshot["approved"])
        self.assertFalse(snapshot["ready"])
        self.assertEqual(snapshot["embedding_requests"], 1)
        self.assertFalse((self.settings.data_dir / "model_credits.sqlite3").exists())
        self.assert_no_external()

    def test_null_private_credit_profile_loads_without_authorizing_or_reserving(self):
        self.write("live_budget.json", None)
        app = self.build(key=KEY_A)
        self.assertFalse(app.models.credit_budget.confirmed)
        self.assertFalse(app.live_status()["budget_confirmed"])
        self.assert_no_budget_reservations(app)
        self.assert_no_external()

    def test_unconfirmed_private_credit_profile_does_not_become_live_ready(self):
        profile = invented_credit_profile()
        profile["pricing_confirmed"] = False
        self.write("live_budget.json", profile)
        app = self.build(key=KEY_A)
        self.assertFalse(app.models.credit_budget.confirmed)
        self.assertFalse(app.live_status()["ready"])
        self.assert_code("PROCESSING_APPROVAL_REQUIRED", lambda: app.live_execution("demo", APPROVAL).__enter__())
        self.assert_no_budget_reservations(app)
        self.assert_no_external()

    def test_invented_confirmed_profile_load_is_read_only_and_missing_key_still_blocks(self):
        self.write("live_budget.json", invented_credit_profile())
        app = self.build()
        self.assertTrue(app.models.credit_budget.confirmed)
        self.assertTrue(app.models.credit_budget.ready)
        self.assertTrue(app.live_status()["budget_confirmed"])
        self.assertFalse(app.live_status()["ready"])
        self.assert_code("MODEL_REMOTE_BLOCKED", lambda: app.live_execution("verification", APPROVAL).__enter__())
        self.assertIsNone(app.models.chat.adapter._gateway._client)
        self.assert_no_budget_reservations(app)
        self.assert_no_external()

    def test_malformed_confirmed_private_credit_profile_returns_safe_budget_error(self):
        profile = invented_credit_profile()
        profile["allocations"] = {"demo": "1000"}
        self.write("live_budget.json", profile)
        self.assert_code("LIVE_BUDGET_UNCONFIRMED", self.build)
        self.assertFalse((self.settings.data_dir / "model_usage.sqlite3").exists())
        self.assertFalse((self.settings.data_dir / "model_credits.sqlite3").exists())
        self.assert_no_external()

    def test_duplicate_private_credit_profile_is_safe_config_error(self):
        (self.settings.config_dir / "live_budget.json").write_text('{"pricing_confirmed":false,"pricing_confirmed":true}', encoding="utf-8")
        self.assert_code("CONFIG_INVALID", self.build)
        self.assert_no_external()

    def test_malformed_gms_runtime_fields_are_safe_config_errors(self):
        cases = (("gms_base_url", "https://example.invalid/"), ("gms_base_url", [BASE]),
            ("gms_chat_route", "/chat/completions"), ("gms_chat_route", {}),
            ("gms_max_output_tokens", 0), ("gms_max_output_tokens", 4097), ("gms_max_output_tokens", True),
            ("usage_approval_id", []), ("usage_approval_id", "has whitespace"),
            ("usage_limits", {"chat_requests": 1}),
            ("usage_limits", {"chat_requests": True, "embedding_requests": 1, "input_bytes": 10}),
            ("usage_limits", {"chat_requests": 1, "embedding_requests": -1, "input_bytes": 10}),
            ("usage_limits", {"chat_requests": 1, "embedding_requests": 1, "input_bytes": 1.5}))
        for field, value in cases:
            with self.subTest(field=field, value=value):
                runtime = configuration()
                runtime["models"][field] = value
                self.assert_code("CONFIG_INVALID", lambda: self.build(runtime))
        self.embed.assert_not_called()
        self.generate.assert_not_called()
        self.assert_no_external()

    def test_invalid_gms_cli_returns_safe_json_without_stderr_or_private_config(self):
        from warranty.cli import main
        runtime = configuration()
        runtime["models"]["gms_base_url"] = "https://example.invalid/private-base"
        self.write("runtime.json", runtime)
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err):
            with self.assertRaises(SystemExit) as caught:
                main(["status"])
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(err.getvalue(), "")
        result = json.loads(out.getvalue())
        self.assertEqual(result["error"]["code"], "CONFIG_INVALID")
        self.assert_public(result)
        self.assert_no_external()

    def test_default_mock_factory_keeps_local_metadata_and_no_gms_budgets(self):
        app = self.build({"collection": {"worker_enabled": False}}, key=KEY_A)
        self.assertTrue(app.models.chat.is_mock)
        self.assertTrue(app.models.embedding.is_mock)
        self.assertEqual(app.rag.metadata_count, len(app.catalog.items))
        self.assertIsNone(app.models.usage_budget)
        self.assertIsNone(app.models.credit_budget)
        self.assertIsNone(app.models.embedding_cache)
        self.assertEqual(app.live_status()["enabled"], False)
        self.embed.assert_not_called()
        self.generate.assert_not_called()
        self.assert_no_external()


if __name__ == "__main__":
    unittest.main()
