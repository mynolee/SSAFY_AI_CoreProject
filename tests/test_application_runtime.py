"""Composition-root regressions using private temporary synthetic runtime files."""
from contextlib import ExitStack
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from warranty.application import build_application
from warranty.catalog import Catalog
from warranty.config import Settings
from warranty.documents import ACTIONS
from warranty.embeddings import OpenAIEmbeddings
from warranty.errors import ServiceError
from warranty.prompts import OpenAIStructuredModel


SOURCE_ID = "SYNTHETIC-SOURCE"
SOURCE_URL = "https://example.invalid/synthetic"
SCOPE = {"country": "SYNTHETIC", "plan_generation": "SYNTHETIC-PLAN",
         "payment_method": "SYNTHETIC-MONTHLY", "channel": "SYNTHETIC",
         "document_type": "SERVICE_TERMS", "date_start": "2020-01-01", "date_end": "2030-01-01"}


def source_configuration():
    return {"schema_version": 1,
            "registry": [{"source_id": SOURCE_ID, "url": SOURCE_URL, "is_synthetic": True}],
            "reviews": [{"source_id": SOURCE_ID, "action": action, "status": "ALLOWED",
                         "basis_type": "OTHER_RECORDED_BASIS", "basis_url": "https://example.invalid/review",
                         "reviewed_on": "2026-01-01", "scope": "Authored synthetic application fixture",
                         "conditions": "Offline factory tests only", "user_scope": "Named fixture actors",
                         "processing_environment": "Local test transport", "version_scope": "SOURCE",
                         "allowed_actors": ["manager", "buyer", "seller"]} for action in ACTIONS],
            "capabilities": {"manager": ["DATA_MANAGER"]}}


class ApplicationRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="warranty-application-runtime-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.settings = Settings(self.base / "data", self.base / "config")
        self.settings.prepare()
        self.apps = []
        self.addCleanup(self._close_apps)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {
            "METADATA_PATH": "", "METADATA_EXPECTED_HASH": "", "OPENAI_API_KEY": "",
            "CHAT_MODEL": "", "CHAT_MODEL_VERSION": "", "EMBEDDING_MODEL": "",
            "EMBEDDING_MODEL_VERSION": "", "EMBEDDING_DIMENSION": "",
            "ENABLE_REMOTE_MODELS": "false", "LANGSMITH_TRACING": "false",
            "LANGCHAIN_TRACING_V2": "false", "ANONYMIZED_TELEMETRY": "false"}))
        self.stack.enter_context(patch("socket.create_connection", side_effect=AssertionError("Network forbidden in factory tests")))
        self.stack.enter_context(patch("socket.socket.connect", side_effect=AssertionError("Network forbidden in factory tests")))
        self.sdk_chat = self.stack.enter_context(patch("warranty.prompts._official_sdk_client", side_effect=AssertionError("Paid chat client forbidden")))
        self.sdk_embedding = self.stack.enter_context(patch("warranty.embeddings._official_sdk_client", side_effect=AssertionError("Paid embedding client forbidden")))
        self.fetch = self.stack.enter_context(patch("warranty.security.SafeFetcher.fetch", side_effect=AssertionError("Document HTTP forbidden")))

    def _close_apps(self):
        for app in reversed(self.apps):
            app.close()

    def write_json(self, name, value):
        (self.settings.config_dir / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def build(self, runtime=None, *, policy=False, plan=None, remote=False):
        if runtime is not None:
            self.write_json("runtime.json", runtime)
        if policy:
            self.write_json("source_policy.json", source_configuration())
        if plan is not None:
            self.write_json("document_plan.json", plan)
        settings = Settings(self.settings.data_dir, self.settings.config_dir, remote_models=remote)
        app = build_application(settings=settings)
        self.apps.append(app)
        return app

    def assert_no_external_calls(self):
        self.sdk_chat.assert_not_called()
        self.sdk_embedding.assert_not_called()
        self.fetch.assert_not_called()

    def assert_invalid_configuration(self):
        unexpected = None
        try:
            with self.assertRaises(ServiceError) as caught:
                unexpected = build_application(settings=self.settings)
        finally:
            if unexpected is not None:
                self.apps.append(unexpected)
        self.assertEqual(caught.exception.code, "CONFIG_INVALID")
        self.assertNotIn(str(self.base), caught.exception.message)
        self.assertNotIn("SYNTHETIC-PRIVATE-MARKER", caught.exception.message)
        return caught.exception

    def assert_public_status(self, app):
        status = app.runtime_status()
        self.assertIsInstance(status, dict)
        for name in ("chat", "embedding"):
            self.assertIn(status[name]["provider"], {"mock", "openai"})
            for flag in ("configured", "ready", "is_mock"):
                self.assertIs(type(status[name][flag]), bool)
        for flag in ("enabled", "network_enabled"):
            self.assertIs(type(status["collection"][flag]), bool)
        self.assertIs(type(status["collection"]["registered_target_count"]), int)
        for flag in ("external_processing_approved", "question_processing_approved", "document_worker_enabled"):
            self.assertIs(type(status[flag]), bool)
        self.assertEqual(set(status["live"]), {"enabled", "ready", "purposes", "budget_confirmed"})
        for flag in ("enabled", "ready", "budget_confirmed"):
            self.assertIs(type(status["live"][flag]), bool)
        self.assertEqual(status["live"]["purposes"], ["evaluation", "verification", "final_verification", "demo", "embedding_preparation"])
        encoded = json.dumps(status, ensure_ascii=False)
        for private in (str(self.base), "OPENAI_API_KEY", "allowed_actors", "basis_url", "source_policy.json", "document_plan.json"):
            self.assertNotIn(private, encoded)
        return status

    def test_default_factory_shares_exact_mock_adapters_and_public_status(self):
        app = self.build({"collection": {"worker_enabled": False}})
        self.assertIs(app.models.chat, app.rag.model)
        self.assertIs(app.models.embedding, app.rag.vectors.embeddings)
        self.assertIs(app.document_jobs, app.rag.document_jobs)
        self.assertEqual(set(app.rag.graph.get_graph().nodes) - {"__start__", "__end__"}, {"retrieve", "generate"})
        status = self.assert_public_status(app)
        for name in ("chat", "embedding"):
            self.assertEqual(status[name]["provider"], "mock")
            self.assertTrue(status[name]["is_mock"])
            self.assertTrue(status[name]["ready"])
        self.assertFalse(status["collection"]["enabled"])
        self.assertFalse(status["collection"]["network_enabled"])
        self.assertEqual(status["collection"]["registered_target_count"], 0)
        self.assertFalse(status["external_processing_approved"])
        self.assertFalse(status["question_processing_approved"])
        self.assertFalse(status["document_worker_enabled"])
        self.assertEqual(app.catalog.summary()["item_count"], 2)
        self.assert_no_external_calls()

    def test_private_policy_and_pending_plan_load_without_fetch_or_implicit_admin_capability(self):
        item_id = Catalog.synthetic().items[0]["item_id"]
        plan = {"schema_version": 1, "documents": [{"source_id": SOURCE_ID, "title": "SYNTHETIC runtime terms",
            "canonical_url": SOURCE_URL, "document_type": "HTML", "is_synthetic": True,
            "targets": [{"item_id": item_id, "role": "SERVICE_TERMS", "applicability": SCOPE,
                         "applicability_basis": "Private authored fixture explicitly covers this item."}]}]}
        app = self.build({"collection": {"enabled": True, "network_enabled": False,
            "manager_actor": "manager", "worker_enabled": False}}, policy=True, plan=plan)
        policy = app.rag.registry.policy
        policy.require_capability("manager", "DATA_MANAGER")
        with self.assertRaises(ServiceError):
            policy.require_capability("admin", "DATA_MANAGER")
        self.assertTrue(policy.allows(SOURCE_ID, "fetch", "manager", "SYNTHETIC-NEW-VERSION"))
        self.assertFalse(policy.allows(SOURCE_ID, "fetch", "outsider", "SYNTHETIC-NEW-VERSION"))
        self.assertEqual(len(app.document_plan), 1)
        self.assertEqual(app.rag.registry.registered_candidates(item_id), [])
        self.assertEqual(self.assert_public_status(app)["collection"]["registered_target_count"], 0)
        # Loading a plan does not perform its explicit manager registration.
        from warranty.document_operations import sync_documents
        registered = sync_documents(app, "manager")
        self.assertEqual(registered["status"], "COMPLETED")
        self.assertEqual((registered["counts"]["document_count"], registered["counts"]["target_count"]), (1, 1))
        targets = app.rag.registry.registered_candidates(item_id)
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]["applicability"], SCOPE)
        self.assertEqual(targets[0]["role"], "SERVICE_TERMS")
        self.assertEqual(registered["documents"][0]["target_ids"], [targets[0]["target_id"]])
        self.assertIsNone(targets[0]["version_id"])
        self.assertEqual(app.rag.registry.candidates(item_id), [])
        status = self.assert_public_status(app)
        self.assertTrue(status["collection"]["enabled"])
        self.assertFalse(status["collection"]["network_enabled"])
        self.assertEqual(status["collection"]["registered_target_count"], 1)
        self.assert_no_external_calls()

    def test_remote_selected_with_runtime_gate_off_boots_without_model_or_sdk_calls(self):
        runtime = {"models": {"chat_provider": "openai", "chat_model": "SYNTHETIC-chat",
            "embedding_provider": "openai", "embedding_model": "SYNTHETIC-embedding", "embedding_dimension": 64,
            "external_processing_approved": True, "allow_question_processing": True},
            "collection": {"manager_actor": "manager", "worker_enabled": False}}
        with (patch.object(OpenAIStructuredModel, "generate", side_effect=AssertionError("Chat must be lazy")) as chat,
              patch.object(OpenAIEmbeddings, "embed", side_effect=AssertionError("Embedding must be lazy")) as embedding):
            app = self.build(runtime, policy=True, remote=False)
            chat.assert_not_called()
            embedding.assert_not_called()
        self.assertIs(app.models.chat, app.rag.model)
        self.assertIs(app.models.embedding, app.rag.vectors.embeddings)
        status = self.assert_public_status(app)
        for name in ("chat", "embedding"):
            self.assertEqual(status[name]["provider"], "openai")
            self.assertFalse(status[name]["is_mock"])
            self.assertFalse(status[name]["ready"])
        self.assertEqual(app.rag.metadata_count, 0)
        self.assertTrue(status["external_processing_approved"])
        self.assertTrue(status["question_processing_approved"])
        with self.assertRaises(ServiceError) as caught:
            app.rag.prepare_metadata("manager", item_ids=[app.catalog.items[0]["item_id"]])
        self.assertEqual(caught.exception.code, "MODEL_REMOTE_BLOCKED")
        self.assert_no_external_calls()

    def test_remote_selected_without_credentials_stays_unready_and_explicit_prepare_is_denied(self):
        runtime = {"models": {"chat_provider": "openai", "chat_model": "SYNTHETIC-chat",
            "embedding_provider": "openai", "embedding_model": "SYNTHETIC-embedding", "embedding_dimension": 64,
            "external_processing_approved": True, "allow_question_processing": True},
            "collection": {"manager_actor": "manager", "worker_enabled": False}}
        app = self.build(runtime, policy=True, remote=True)
        status = self.assert_public_status(app)
        self.assertFalse(status["chat"]["ready"])
        self.assertFalse(status["embedding"]["ready"])
        with self.assertRaises(ServiceError) as caught:
            app.rag.prepare_metadata("manager", item_ids=[app.catalog.items[0]["item_id"]])
        self.assertEqual(caught.exception.code, "MODEL_REMOTE_BLOCKED")
        self.assert_no_external_calls()

    def test_credentials_do_not_replace_explicit_external_processing_approval(self):
        runtime = {"models": {"chat_provider": "openai", "chat_model": "SYNTHETIC-chat",
            "embedding_provider": "openai", "embedding_model": "SYNTHETIC-embedding", "embedding_dimension": 64,
            "external_processing_approved": False, "allow_question_processing": False},
            "collection": {"manager_actor": "manager", "worker_enabled": False}}
        with patch.dict(os.environ, {"OPENAI_API_KEY": "SYNTHETIC-NOT-A-REAL-CREDENTIAL"}):
            app = self.build(runtime, policy=True, remote=True)
        status = self.assert_public_status(app)
        for name in ("chat", "embedding"):
            # A supplied key cannot configure the factory's blocked private
            # protocol placeholder or authorize a legacy outbound envelope.
            self.assertFalse(status[name]["configured"])
            self.assertFalse(status[name]["ready"])
        for adapter in (app.models.chat.adapter, app.models.embedding):
            metadata = adapter.metadata()
            self.assertTrue(metadata["public_protocol_required"])
            self.assertFalse(metadata["configuration_ready"])
            self.assertFalse(metadata["can_generate"])
            self.assertEqual(metadata["reason_code"], "PUBLIC_PROTOCOL_REQUIRED")
        self.assertFalse(status["external_processing_approved"])
        self.assertFalse(status["question_processing_approved"])
        self.assertNotIn("SYNTHETIC-NOT-A-REAL-CREDENTIAL", json.dumps(status))
        with self.assertRaises(ServiceError) as caught:
            app.rag.prepare_metadata("manager", item_ids=[app.catalog.items[0]["item_id"]])
        self.assertEqual(caught.exception.code, "MODEL_REMOTE_BLOCKED")
        self.assert_no_external_calls()

    def test_queue_is_attached_and_manual_job_lifecycle_is_persisted(self):
        app = self.build({"collection": {"worker_enabled": False}})
        item_id = app.catalog.items[0]["item_id"]
        queued = app.rag.enqueue_prepare("buyer", item_id, question="SYNTHETIC prepare request")
        self.assertEqual((queued["status"], queued["stage"]), ("QUEUED", "QUEUED"))
        self.assertEqual(app.rag.job("buyer", queued["job_id"])["requester"], "buyer")
        with self.assertRaises(ServiceError):
            app.rag.job("seller", queued["job_id"])
        self.assertEqual(app.document_jobs.run_pending()["processed_jobs"], 1)
        final = app.rag.job("buyer", queued["job_id"])
        self.assertEqual(final["status"], "BLOCKED")
        self.assertEqual(final["failure_code"], "OWN_DOCUMENT_MISSING")
        for private in ("question", "request_json", "request_hash", "claim_token", "lease_until"):
            self.assertNotIn(private, final)
        self.assert_no_external_calls()

    def test_enabled_document_worker_starts_and_stops_without_external_processing(self):
        app = self.build({"collection": {"worker_enabled": True}})
        self.assertTrue(app.document_jobs.worker_running)
        self.assertTrue(self.assert_public_status(app)["document_worker_enabled"])
        self.assertIs(app.close(), True)
        self.assertFalse(app.document_jobs.worker_running)
        self.apps.remove(app)
        self.assert_no_external_calls()

    def test_factory_reopen_keeps_queued_request_and_binds_new_queue_handler(self):
        runtime = {"collection": {"worker_enabled": False}}
        first = self.build(runtime)
        item_id = first.catalog.items[0]["item_id"]
        queued = first.rag.enqueue_prepare("buyer", item_id, question="SYNTHETIC persisted factory request")
        self.assertIs(first.close(), True)
        self.apps.remove(first)
        reopened = self.build(runtime)
        restored = reopened.rag.job("buyer", queued["job_id"])
        self.assertEqual(restored["status"], "QUEUED")
        self.assertIs(reopened.document_jobs, reopened.rag.document_jobs)
        self.assertIsNot(reopened.document_jobs, first.document_jobs)
        self.assertEqual(reopened.document_jobs.run_pending()["processed_jobs"], 1)
        final = reopened.rag.job("buyer", queued["job_id"])
        self.assertEqual((final["status"], final["failure_code"]), ("BLOCKED", "OWN_DOCUMENT_MISSING"))
        self.assertEqual(reopened.document_jobs.run_pending()["processed_jobs"], 0)
        self.assert_no_external_calls()

    def test_explicit_network_setting_constructs_safe_fetcher_without_requesting_documents(self):
        app = self.build({"collection": {"enabled": True, "network_enabled": True,
            "manager_actor": "manager", "worker_enabled": False}}, policy=True)
        status = self.assert_public_status(app)
        self.assertTrue(status["collection"]["enabled"])
        self.assertTrue(status["collection"]["network_enabled"])
        self.assertTrue(app.rag.registry.policy.network_enabled)
        self.assertIsNotNone(app.rag.registry.fetcher)
        self.assert_no_external_calls()

    def test_close_preserves_live_resources_if_worker_does_not_stop(self):
        app = self.build({"collection": {"worker_enabled": False}})
        with (patch.object(app.document_jobs, "stop", return_value=False) as stop,
              patch.object(app.rag, "close") as rag_close, patch.object(app.db, "close") as db_close):
            self.assertIs(app.close(), False)
            stop.assert_called_once_with(timeout=5)
            rag_close.assert_not_called()
            db_close.assert_not_called()
        self.assertEqual(app.catalog.get(app.catalog.items[0]["item_id"])["provider"], "SYNTHETIC")
        self.assertIs(app.close(), True)
        self.apps.remove(app)

    def test_successful_close_stops_queue_before_closing_rag_and_database(self):
        app = self.build({"collection": {"worker_enabled": False}})
        events = []
        real_stop, real_rag, real_db = app.document_jobs.stop, app.rag.close, app.db.close
        def stop(*args, **kwargs):
            events.append("queue")
            return real_stop(*args, **kwargs)
        def close_rag():
            events.append("rag")
            return real_rag()
        def close_db():
            events.append("db")
            return real_db()
        with (patch.object(app.document_jobs, "stop", side_effect=stop),
              patch.object(app.rag, "close", side_effect=close_rag), patch.object(app.db, "close", side_effect=close_db)):
            self.assertIs(app.close(), True)
        self.assertEqual(events, ["queue", "rag", "db"])
        self.apps.remove(app)

    def test_invalid_private_runtime_is_safe_config_error_before_any_external_call(self):
        cases = [
            {"unexpected_private_field": "SYNTHETIC-PRIVATE-MARKER"},
            {"models": {"chat_provider": "unregistered-provider"}},
            {"models": {"chat_provider": ["mock"]}},
            {"models": {"external_processing_approved": "yes"}},
            {"models": {"embedding_dimension": True}},
            {"collection": {"network_enabled": True, "enabled": False}},
            {"retrieval": {"chunk_size": 100, "chunk_overlap": 100}},
            {"retrieval": {"lambda_mult": float("nan")}},
        ]
        for value in cases:
            with self.subTest(value=value):
                self.write_json("runtime.json", value)
                self.assert_invalid_configuration()
        (self.settings.config_dir / "runtime.json").write_text('{"models":{"chat_provider":"mock","chat_provider":"openai"}}', encoding="utf-8")
        self.assert_invalid_configuration()
        self.assert_no_external_calls()

    def test_invalid_private_policy_and_plan_cannot_silently_boot_without_permissions(self):
        self.write_json("runtime.json", {"collection": {"enabled": True, "manager_actor": "manager", "worker_enabled": False}})
        malformed = copy.deepcopy(source_configuration())
        malformed["capabilities"]["admin"] = ["IMPLIED_DATA_MANAGER"]
        self.write_json("source_policy.json", malformed)
        self.assert_invalid_configuration()
        self.write_json("source_policy.json", source_configuration())
        self.write_json("document_plan.json", {"schema_version": 1, "documents": [{"source_id": SOURCE_ID,
            "title": "SYNTHETIC invalid plan", "canonical_url": SOURCE_URL, "document_type": "HTML",
            "targets": [{"item_id": "SYNTHETIC-001", "role": "MODEL_INVENTED_ROLE", "applicability": SCOPE,
                         "applicability_basis": "SYNTHETIC configuration error"}]}]})
        self.assert_invalid_configuration()
        self.assert_no_external_calls()


if __name__ == "__main__":
    unittest.main()
