"""Synthetic scope and budget contracts; no provider/client construction."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from warranty.errors import ServiceError
from warranty.live import LIVE_PURPOSES, live_execution, live_status


APPROVAL = "synthetic-live-approval"
GATEWAY = "https://gms.ssafy.io/gmsapi/api.openai.com/v1"


class FakeBudget:
    def __init__(self):
        self.ready, self.confirmed, self.matches = True, True, True
        self.reads = 0

    def approval_matches(self, identifier):
        self.reads += 1
        return self.matches if self.matches is not True else identifier == APPROVAL

    def consume(self, *args, **kwargs):
        raise AssertionError("The helper must not consume any budget")

    reserve = consume


class FakeGMS:
    is_mock, is_remote = False, True

    def __init__(self, kind):
        self.kind = kind
        self.model_id = "gpt-4.1-mini" if kind == "chat" else "text-embedding-3-small"
        self.dimension = 2
        self.scope = ContextVar("synthetic-live-" + kind, default=None)
        self.processing = ContextVar("synthetic-processing-" + kind, default=None)
        self.entered, self.exited = [], []
        self.changes, self.on_enter = {}, None

    def metadata(self):
        return {"provider": "gms", "model_id": self.model_id, "model_version": "synthetic-version",
                "route": "chat/completions" if self.kind == "chat" else "embeddings", "dimension": self.dimension,
                "configuration_ready": True, "can_generate": self.scope.get() is not None,
                "live_execution_required": True, "gateway_base_url": GATEWAY,
                "is_mock": False, "is_remote": True, **self.changes}

    @contextmanager
    def live_execution(self, purpose, approval_id):
        if self.on_enter is not None:
            self.on_enter()
        self.entered.append((purpose, approval_id))
        token = self.scope.set((purpose, approval_id))
        try:
            yield self
        finally:
            self.exited.append(self.scope.get())
            self.scope.reset(token)


class FakeWrapper:
    is_mock, is_remote = False, True

    def __init__(self, adapter):
        self.adapter, self.model_id = adapter, adapter.model_id

    def metadata(self):
        return self.adapter.metadata()


def application(*, chat="gms", embedding="gms"):
    adapter = FakeGMS("chat")
    chat_model = FakeWrapper(adapter) if chat == "gms" else SimpleNamespace(is_mock=True)
    embedding_model = FakeGMS("embedding") if embedding == "gms" else SimpleNamespace(is_mock=True)
    models = SimpleNamespace(chat=chat_model, embedding=embedding_model,
                             usage_budget=FakeBudget(), credit_budget=FakeBudget())
    values = {"chat_provider": chat, "embedding_provider": embedding,
              "chat_model": adapter.model_id if chat == "gms" else None,
              "embedding_model": embedding_model.model_id if embedding == "gms" else None,
              "chat_model_version": "synthetic-version" if chat == "gms" else None,
              "embedding_model_version": "synthetic-version" if embedding == "gms" else None,
              "embedding_dimension": 2 if embedding == "gms" else None,
              "external_processing_approved": True, "usage_approval_id": APPROVAL,
              "gms_base_url": GATEWAY, "gms_chat_route": "chat/completions"}
    rag = SimpleNamespace(model=chat_model, runner=SimpleNamespace(model=chat_model),
                          vectors=SimpleNamespace(embeddings=embedding_model))
    return SimpleNamespace(settings=SimpleNamespace(remote_models=True), runtime={"models": values},
                           models=models, rag=rag)


class LiveExecutionTests(unittest.TestCase):
    def setUp(self):
        self.app = application()
        self.chat = self.app.models.chat.adapter
        self.embedding = self.app.models.embedding
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("socket.create_connection", side_effect=AssertionError("Network forbidden")))
        self.stack.enter_context(patch("socket.socket.connect", side_effect=AssertionError("Network forbidden")))

    def denied(self, app=None, *, purpose="evaluation", approval=APPROVAL, code="MODEL_REMOTE_BLOCKED"):
        with self.assertRaises(ServiceError) as caught:
            with live_execution(app or self.app, purpose, approval):
                self.fail("A denied configuration granted LIVE scope")
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn(APPROVAL, json.dumps(caught.exception.as_dict()))
        self.assertIsNone(self.chat.scope.get())
        self.assertIsNone(self.embedding.scope.get())

    def test_explicit_scope_enters_both_selected_adapters_and_restores(self):
        self.assertFalse(self.chat.metadata()["can_generate"])
        with live_execution(self.app, "evaluation", APPROVAL) as app:
            self.assertIs(app, self.app)
            self.assertEqual(self.chat.scope.get(), ("evaluation", APPROVAL))
            self.assertEqual(self.embedding.scope.get(), ("evaluation", APPROVAL))
            self.assertIsNone(self.chat.processing.get())
            self.assertIsNone(self.embedding.processing.get())
        self.assertIsNone(self.chat.scope.get())
        self.assertIsNone(self.embedding.scope.get())
        self.assertEqual(len(self.chat.entered), 1)
        self.assertEqual(len(self.embedding.exited), 1)

    def test_all_and_only_declared_purposes(self):
        for purpose in LIVE_PURPOSES:
            with live_execution(self.app, purpose, APPROVAL):
                self.assertEqual(self.chat.scope.get()[0], purpose)
        for purpose in (None, [], {}, True, "development", "UI", "evaluation ", ""):
            with self.subTest(purpose=purpose):
                self.denied(purpose=purpose, code="INVALID_INPUT")

    def test_approval_requires_exact_nonempty_valid_identifier(self):
        for approval in (None, [], True, "", " " + APPROVAL, APPROVAL + "\n", "x" * 129, "\uac00"):
            with self.subTest(approval=approval):
                self.denied(approval=approval, code="INVALID_INPUT")
        self.denied(approval="another-synthetic-approval", code="PROCESSING_APPROVAL_REQUIRED")
        self.assertFalse(self.chat.entered)

    def test_mock_and_other_remote_provider_combinations_are_rejected(self):
        for chat, embedding in (("mock", "mock"), ("openai", "mock"), ("gms", "openai"), ("openai", "gms")):
            with self.subTest(chat=chat, embedding=embedding):
                self.denied(application(chat=chat, embedding=embedding))

    def test_one_selected_gms_with_other_mock_is_allowed(self):
        for chat, embedding in (("gms", "mock"), ("mock", "gms")):
            app = application(chat=chat, embedding=embedding)
            with live_execution(app, "verification", APPROVAL):
                self.assertTrue(live_status(app)["ready"])

    def test_remote_and_external_approval_must_be_exact_true(self):
        for value in (False, 1, "true", None):
            with self.subTest(value=value):
                self.app.settings.remote_models = value
                self.denied()
                self.app.settings.remote_models = True
                self.app.runtime["models"]["external_processing_approved"] = value
                self.denied()
                self.app.runtime["models"]["external_processing_approved"] = True

    def test_missing_runtime_approval_denies_before_scope(self):
        for approval in (None, "", [], True, "bad approval"):
            self.app.runtime["models"]["usage_approval_id"] = approval
            self.denied(code="PROCESSING_APPROVAL_REQUIRED")
        self.assertFalse(self.chat.entered)

    def test_both_budgets_require_exact_ready_and_matching_approval(self):
        for budget in (self.app.models.usage_budget, self.app.models.credit_budget):
            for attribute in ("ready", "matches"):
                for value in (False, 1, None):
                    with self.subTest(budget=budget, attribute=attribute, value=value):
                        setattr(budget, attribute, value)
                        self.denied(code="PROCESSING_APPROVAL_REQUIRED")
                setattr(budget, attribute, True)
        self.app.models.credit_budget.confirmed = False
        self.denied(code="PROCESSING_APPROVAL_REQUIRED")
        self.assertFalse(self.chat.entered)

    def test_configuration_ready_rejects_before_any_adapter_scope(self):
        self.embedding.changes["configuration_ready"] = False
        self.denied()
        self.assertFalse(self.chat.entered)
        self.assertFalse(self.embedding.entered)

    def test_actual_pipeline_model_identity_is_required(self):
        for mutate in (lambda: setattr(self.app.rag, "model", FakeWrapper(FakeGMS("chat"))),
                       lambda: setattr(self.app.rag.runner, "model", FakeWrapper(FakeGMS("chat"))),
                       lambda: setattr(self.app.rag.vectors, "embeddings", FakeGMS("embedding"))):
            self.setUpApp()
            mutate()
            self.denied()
        self.assertFalse(self.chat.entered)

    def setUpApp(self):
        self.app = application()
        self.chat, self.embedding = self.app.models.chat.adapter, self.app.models.embedding

    def test_provider_metadata_model_version_route_and_dimension_match_runtime(self):
        cases = (("chat", "provider", "openai"), ("chat", "model_id", "other-model"),
                 ("chat", "model_version", "other-version"), ("chat", "route", "responses"),
                 ("chat", "gateway_base_url", "https://example.invalid/private"),
                 ("chat", "is_mock", True), ("chat", "is_remote", 1),
                 ("chat", "live_execution_required", False), ("embedding", "dimension", 3))
        for slot, field, value in cases:
            with self.subTest(slot=slot, field=field):
                adapter = self.chat if slot == "chat" else self.embedding
                adapter.changes[field] = value
                self.denied()
                adapter.changes.clear()
        self.assertFalse(self.chat.entered)

    def test_environment_selected_model_can_match_without_runtime_override(self):
        for key in ("chat_model", "chat_model_version", "embedding_model", "embedding_model_version", "embedding_dimension"):
            self.app.runtime["models"][key] = None
        with live_execution(self.app, "demo", APPROVAL):
            self.assertTrue(self.chat.metadata()["can_generate"])

    def test_nested_scopes_restore_prior_purpose(self):
        with live_execution(self.app, "evaluation", APPROVAL):
            with live_execution(self.app, "verification", APPROVAL):
                self.assertEqual(self.chat.scope.get(), ("verification", APPROVAL))
            self.assertEqual(self.chat.scope.get(), ("evaluation", APPROVAL))
        self.assertIsNone(self.chat.scope.get())

    def test_nested_exception_and_denied_inner_scope_preserve_outer_scope(self):
        with live_execution(self.app, "evaluation", APPROVAL):
            with self.assertRaisesRegex(RuntimeError, "Synthetic nested error"):
                with live_execution(self.app, "demo", APPROVAL):
                    raise RuntimeError("Synthetic nested error")
            with self.assertRaises(ServiceError):
                with live_execution(self.app, "demo", "wrong-synthetic-approval"):
                    self.fail("A mismatching nested approval was accepted")
            self.assertEqual(self.chat.scope.get(), ("evaluation", APPROVAL))
            self.assertEqual(self.embedding.scope.get(), ("evaluation", APPROVAL))
        self.assertIsNone(self.chat.scope.get())
        self.assertIsNone(self.embedding.scope.get())

    def test_user_exception_is_preserved_and_every_scope_is_reset(self):
        error = RuntimeError("Synthetic caller error")
        with self.assertRaises(RuntimeError) as caught:
            with live_execution(self.app, "demo", APPROVAL):
                raise error
        self.assertIs(caught.exception, error)
        self.assertIsNone(self.chat.scope.get())
        self.assertIsNone(self.embedding.scope.get())

    def test_second_scope_entry_failure_cleans_first_and_hides_details(self):
        def fail():
            raise RuntimeError(APPROVAL + " synthetic-key private-path")
        self.embedding.on_enter = fail
        self.denied()
        self.assertEqual(len(self.chat.entered), 1)
        self.assertEqual(len(self.chat.exited), 1)

    def test_post_entry_budget_revocation_cleans_all_scopes(self):
        self.embedding.on_enter = lambda: setattr(self.app.models.credit_budget, "ready", False)
        self.denied(code="PROCESSING_APPROVAL_REQUIRED")
        self.assertEqual(len(self.chat.exited), 1)
        self.assertEqual(len(self.embedding.exited), 1)

    def test_adapter_must_report_effective_live_scope_after_entry(self):
        self.embedding.changes["can_generate"] = False
        self.denied()
        self.assertEqual(len(self.chat.exited), 1)
        self.assertEqual(len(self.embedding.exited), 1)

    def test_thread_does_not_inherit_live_scope(self):
        with ThreadPoolExecutor(max_workers=1) as pool:
            with live_execution(self.app, "demo", APPROVAL):
                scopes = pool.submit(lambda: (self.chat.scope.get(), self.embedding.scope.get())).result(timeout=5)
            self.assertEqual(scopes, (None, None))

    def test_concurrent_scopes_are_isolated(self):
        barrier = threading.Barrier(2)
        def run(purpose):
            with live_execution(self.app, purpose, APPROVAL):
                barrier.wait(timeout=5)
                found = self.chat.scope.get(), self.embedding.scope.get()
            return found, self.chat.scope.get(), self.embedding.scope.get()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run, purpose) for purpose in ("evaluation", "demo")]
            for purpose, future in zip(("evaluation", "demo"), futures):
                found, chat, embedding = future.result(timeout=5)
                self.assertEqual(found, ((purpose, APPROVAL), (purpose, APPROVAL)))
                self.assertIsNone(chat)
                self.assertIsNone(embedding)

    def test_status_is_read_only_public_and_does_not_grant_scope(self):
        result = live_status(self.app)
        self.assertEqual(result, {"enabled": True, "ready": True, "purposes": list(LIVE_PURPOSES), "budget_confirmed": True})
        self.assertNotIn(APPROVAL, json.dumps(result))
        self.assertFalse(self.chat.entered)
        self.assertFalse(self.embedding.entered)
        self.assertFalse(self.chat.metadata()["can_generate"])
        result["purposes"].clear()
        self.assertEqual(live_status(self.app)["purposes"], list(LIVE_PURPOSES))

    def test_unconfirmed_budget_and_incomplete_app_status_fail_closed(self):
        self.app.models.credit_budget.confirmed = False
        self.assertEqual(live_status(self.app), {"enabled": True, "ready": False, "purposes": list(LIVE_PURPOSES), "budget_confirmed": False})
        self.assertEqual(live_status(SimpleNamespace()), {"enabled": False, "ready": False, "purposes": list(LIVE_PURPOSES), "budget_confirmed": False})
        self.app.settings.remote_models = False
        self.assertFalse(live_status(self.app)["enabled"])

    def test_bad_adapter_metadata_errors_are_safe(self):
        with patch.object(self.chat, "metadata", side_effect=ValueError(APPROVAL + " private-path")):
            self.denied()
            self.assertFalse(live_status(self.app)["ready"])

    def test_missing_budget_reference_is_denied_without_entering_scope(self):
        del self.app.models.credit_budget
        self.denied()
        self.assertFalse(self.chat.entered)


if __name__ == "__main__":
    unittest.main()
