"""Offline SDK request-contract checks; all payloads and clients are synthetic."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from copy import deepcopy
import json
import logging
from pathlib import Path
import socket
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from warranty.embeddings import OpenAIEmbeddings
from warranty.errors import ServiceError
from warranty.prompts import OpenAIStructuredModel, PromptStore, StructuredRunner, output_schema


ENV = {"OPENAI_API_KEY": "synthetic-secret", "CHAT_MODEL": "synthetic-chat", "EMBEDDING_MODEL": "synthetic-embedding",
       "EMBEDDING_DIMENSION": "3"}
SETTINGS = SimpleNamespace(remote_models=True)
DOC_CONTEXT = {"kind": "document", "source_id": "synthetic-source", "version_id": "synthetic-version", "actor": "synthetic-actor"}
INPUTS = {"item": {}, "question": "Synthetic question", "applicability": {},
          "evidence": [], "missing_information": [], "conflicts": []}
ANSWER = {"classification": "NEEDS_INFORMATION", "answer": "Synthetic response", "claims": [], "conditions": [],
          "missing_information": [], "conflicts": [], "cited_evidence_ids": []}


def policy():
    return {"version": "1.0.0", "kind": "answer", "role": "Synthetic fixture", "template": "Synthetic fixture",
            "input_variables": list(INPUTS), "output_schema": output_schema("answer"),
            **{key: ["Synthetic rule"] for key in ("evidence_limits", "missing_information_rules", "conflict_rules", "attack_rules")}}


def prompt():
    return json.dumps({"policy": policy(), "input": INPUTS})


def chat_response(**changes):
    fields = {"output_text": json.dumps(ANSWER), "status": "completed", "error": None,
              "incomplete_details": None, "output": [], "model": "synthetic-provider-chat"}
    return SimpleNamespace(**{**fields, **changes})


def embedding_response(count=1):
    return SimpleNamespace(model="synthetic-provider-embedding", data=[
        SimpleNamespace(index=index, embedding=[index + 0.1, 0.2, 0.3]) for index in range(count)])


class FakeClient:
    def __init__(self, *, chat=None, embeddings=None):
        self.chat_calls, self.embedding_calls = [], []
        self.chat = chat or (lambda **kwargs: chat_response())
        self.embedding = embeddings or (lambda **kwargs: embedding_response(len(kwargs["input"])))
        self.responses = SimpleNamespace(create=self._chat)
        self.embeddings = SimpleNamespace(create=self._embedding)

    def _chat(self, **kwargs):
        self.chat_calls.append(deepcopy(kwargs))
        return self.chat(**kwargs)

    def _embedding(self, **kwargs):
        self.embedding_calls.append(deepcopy(kwargs))
        return self.embedding(**kwargs)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.network_attempts = []

        def denied(*args, **kwargs):
            self.network_attempts.append(True)
            raise AssertionError("Network is forbidden in adapter tests")

        self.stack.enter_context(patch.object(socket.socket, "connect", denied))
        self.stack.enter_context(patch.object(socket.socket, "connect_ex", denied))
        self.stack.enter_context(patch.object(socket, "create_connection", denied))

    def tearDown(self):
        self.stack.close()
        self.assertEqual([], self.network_attempts)

    def embedding_model(self, client=None, guard=None, env=None, settings=SETTINGS):
        return OpenAIEmbeddings(settings, guard if guard is not None else lambda texts, contexts: True,
                                client=client or FakeClient(), env=ENV if env is None else env)

    def test_embedding_requires_opt_in_key_model_dimension_and_guard(self):
        cases = [(SimpleNamespace(remote_models=False), ENV, lambda *args: True),
                 (SETTINGS, {**ENV, "OPENAI_API_KEY": ""}, lambda *args: True),
                 (SETTINGS, {**ENV, "EMBEDDING_MODEL": ""}, lambda *args: True),
                 (SETTINGS, {**ENV, "EMBEDDING_DIMENSION": ""}, lambda *args: True),
                 (SETTINGS, ENV, None)]
        for settings, env, guard in cases:
            client = FakeClient()
            model = OpenAIEmbeddings(settings, guard, client=client, env=env)
            self.assertFalse(model.metadata()["can_generate"])
            with model.processing_context([DOC_CONTEXT]), self.assertRaises(ServiceError) as raised:
                model.embed(["Synthetic text"])
            self.assertEqual("MODEL_REMOTE_BLOCKED", raised.exception.code)
            self.assertEqual([], client.embedding_calls)

    def test_embedding_requires_current_context_even_with_injected_client(self):
        client = FakeClient()
        model = self.embedding_model(client)
        with self.assertRaises(ServiceError) as raised:
            model.embed(["Synthetic text"])
        self.assertEqual("MODEL_REMOTE_BLOCKED", raised.exception.code)
        self.assertEqual([], client.embedding_calls)

    def test_context_shape_actor_source_and_version_are_validated(self):
        model = self.embedding_model()
        for contexts in ([], {}, [{**DOC_CONTEXT, "version_id": None}], [{**DOC_CONTEXT, "actor": ""}],
                         [{**DOC_CONTEXT, "kind": "unknown"}], [{**DOC_CONTEXT, "extra": "value"}]):
            with self.subTest(contexts=contexts), self.assertRaises(ServiceError) as raised:
                with model.processing_context(contexts):
                    self.fail("Invalid processing context entered")
            self.assertEqual("MODEL_REMOTE_BLOCKED", raised.exception.code)

    def test_question_context_requires_separate_guard_and_restores_scope(self):
        observed = []
        model = self.embedding_model(guard=lambda texts, contexts: observed.append(contexts) or True)
        question = {"kind": "question", "source_id": None, "version_id": None, "actor": "synthetic-actor"}
        with model.processing_context([DOC_CONTEXT]):
            with model.processing_context([question]):
                model.embed(["Synthetic question"])
            model.embed(["Synthetic document"])
        self.assertEqual(["question", "document"], [rows[0]["kind"] for rows in observed])
        with self.assertRaises(ServiceError):
            model.embed(["Synthetic text"])

    def test_processing_contexts_are_isolated_between_threads(self):
        barrier, observed, lock = threading.Barrier(2), [], threading.Lock()

        def guard(texts, contexts):
            with lock:
                observed.append((texts[0], contexts[0]["actor"]))
            return True

        model = self.embedding_model(guard=guard)

        def invoke(actor):
            with model.processing_context([{**DOC_CONTEXT, "actor": actor}]):
                barrier.wait(timeout=5)
                return model.embed([actor])

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(2, len(list(pool.map(invoke, ["synthetic-a", "synthetic-b"]))))
        self.assertEqual({("synthetic-a", "synthetic-a"), ("synthetic-b", "synthetic-b")}, set(observed))

    def test_authorization_copies_cannot_change_outbound_payload(self):
        client = FakeClient()

        def guard(texts, contexts):
            texts[0] = "mutated"
            contexts[0]["actor"] = "mutated"
            return True

        contexts = [deepcopy(DOC_CONTEXT)]
        model = self.embedding_model(client, guard)
        with model.processing_context(contexts):
            model.embed(["Synthetic original"])
        self.assertEqual(["Synthetic original"], client.embedding_calls[0]["input"])
        self.assertEqual("synthetic-actor", contexts[0]["actor"])

    def test_embedding_request_contract_and_response_order(self):
        response = embedding_response(2)
        response.data.reverse()
        client = FakeClient(embeddings=lambda **kwargs: response)
        model = self.embedding_model(client)
        with model.processing_context([DOC_CONTEXT]):
            result = model.embed(["Synthetic one", "Synthetic two"])
        self.assertEqual([[0.1, 0.2, 0.3], [1.1, 0.2, 0.3]], result)
        self.assertEqual({"input": ["Synthetic one", "Synthetic two"], "model": "synthetic-embedding",
                          "dimensions": 3, "encoding_format": "float"}, client.embedding_calls[0])
        self.assertEqual("synthetic-provider-embedding", model.metadata()["provider_reported_model"])

    def test_guard_is_rechecked_before_every_bounded_embedding_request(self):
        client, checks = FakeClient(), []
        model = self.embedding_model(client, lambda texts, contexts: checks.append(len(texts)) or len(checks) == 1)
        with model.processing_context([DOC_CONTEXT]), self.assertRaises(ServiceError) as raised:
            model.embed(["Synthetic"] * 101)
        self.assertEqual("MODEL_REMOTE_BLOCKED", raised.exception.code)
        self.assertEqual([101, 101], checks)
        self.assertEqual(1, len(client.embedding_calls))

    def test_embedding_denial_exception_and_nonboolean_approval_never_send(self):
        def failing_guard(*args):
            raise RuntimeError("synthetic-secret")

        for guard in (lambda *args: False, lambda *args: {"approved": True}, failing_guard):
            client = FakeClient()
            model = self.embedding_model(client, guard)
            with model.processing_context([DOC_CONTEXT]), self.assertRaises(ServiceError) as raised:
                model.embed(["Synthetic"])
            self.assertEqual("MODEL_REMOTE_BLOCKED", raised.exception.code)
            self.assertEqual([], client.embedding_calls)

    def test_embedding_rejects_invalid_inputs_without_provider_calls(self):
        client = FakeClient()
        model = self.embedding_model(client)
        for texts in ([""], [" "], [1], "text", ["x" * 8193], ["Synthetic"] * 2049):
            with model.processing_context([DOC_CONTEXT]), self.assertRaises(ServiceError) as raised:
                model.embed(texts)
            self.assertEqual("EMBEDDING_INPUT_INVALID", raised.exception.code)
        with model.processing_context([DOC_CONTEXT]):
            self.assertEqual([], model.embed([]))
        self.assertEqual([], client.embedding_calls)

    def test_embedding_rejects_wrong_count_index_dimension_and_nonfinite_values(self):
        invalid = [SimpleNamespace(model="synthetic", data=[])]
        for vector in ([1.0, 2.0], [float("nan"), 0.0, 1.0], [float("inf"), 0.0, 1.0],
                       [True, 0.0, 1.0], ["secret", 0.0, 1.0], "base64-not-accepted"):
            invalid.append(SimpleNamespace(model="synthetic", data=[SimpleNamespace(index=0, embedding=vector)]))
        invalid.extend([SimpleNamespace(model="synthetic", data=[SimpleNamespace(index=True, embedding=[0.0] * 3)]),
                        SimpleNamespace(model="synthetic", data=[SimpleNamespace(index=1, embedding=[0.0] * 3)]),
                        SimpleNamespace(data=[SimpleNamespace(index=0, embedding=[0.0] * 3)])])
        for response in invalid:
            client = FakeClient(embeddings=lambda **kwargs: response)
            model = self.embedding_model(client)
            with model.processing_context([DOC_CONTEXT]), self.assertRaises(ServiceError) as raised:
                model.embed(["Synthetic"])
            self.assertEqual("EMBEDDING_OUTPUT_INVALID", raised.exception.code)

    def test_duplicate_response_indices_are_rejected(self):
        response = embedding_response(2)
        response.data[1].index = 0
        model = self.embedding_model(FakeClient(embeddings=lambda **kwargs: response))
        with model.processing_context([DOC_CONTEXT]), self.assertRaises(ServiceError) as raised:
            model.embed(["Synthetic one", "Synthetic two"])
        self.assertEqual("EMBEDDING_OUTPUT_INVALID", raised.exception.code)

    def test_embeddings_exception_and_metadata_never_expose_key_or_input(self):
        def fail(**kwargs):
            raise RuntimeError("synthetic-secret Synthetic private text")

        model = self.embedding_model(FakeClient(embeddings=fail))
        self.assertNotIn("synthetic-secret", json.dumps(model.metadata()))
        with model.processing_context([DOC_CONTEXT]), self.assertRaises(ServiceError) as raised:
            model.embed(["Synthetic private text"])
        self.assertEqual("MODEL_UNAVAILABLE", raised.exception.code)
        self.assertNotIn("synthetic", json.dumps(raised.exception.as_dict()))

    def test_chat_metadata_has_no_fabricated_version_and_configuration_is_not_approval(self):
        model = OpenAIStructuredModel(SETTINGS, lambda *args: False, client=FakeClient(), env=ENV)
        metadata = model.metadata()
        self.assertTrue(metadata["can_generate"])
        self.assertTrue(metadata["is_remote"])
        self.assertIsNone(metadata["model_version"])
        self.assertIsNone(metadata["provider_reported_model"])
        self.assertEqual("per_context_required", metadata["authorization"])
        self.assertIsNone(metadata["temperature"])
        self.assertNotIn("synthetic-secret", json.dumps(metadata))
        with self.assertRaises(ServiceError) as raised:
            model.generate("answer", prompt())
        self.assertEqual("MODEL_REMOTE_BLOCKED", raised.exception.code)

    def test_explicit_operator_versions_are_distinct_from_provider_reported_names(self):
        client = FakeClient()
        model = OpenAIStructuredModel(SETTINGS, lambda *args: True, client=client, env=ENV,
                                     requested_model_version="synthetic-recorded-version")
        model.generate("answer", prompt())
        metadata = model.metadata()
        self.assertEqual("synthetic-recorded-version", metadata["model_version"])
        self.assertEqual("SERVER_CONFIGURED_NOT_PROVIDER_VERIFIED", metadata["version_source"])
        self.assertEqual("synthetic-provider-chat", metadata["provider_reported_model"])

    def test_chat_send_has_no_tools_storage_user_identifiers_or_key(self):
        client = FakeClient()
        model = OpenAIStructuredModel(SETTINGS, lambda *args: True, client=client, env=ENV)
        self.assertEqual(json.dumps(ANSWER), model.generate("answer", prompt()))
        sent = client.chat_calls[0]
        self.assertEqual(model.metadata()["generation_settings"], {key: sent[key] for key in ("max_output_tokens", "text", "store", "tools")})
        self.assertEqual({"input": INPUTS}, json.loads(sent["input"]))
        self.assertNotIn("synthetic-secret", json.dumps(sent))
        self.assertNotIn("user", sent)

    def test_chat_rejects_unguarded_fields_and_forged_repair_before_provider_call(self):
        for mutate in (
            lambda envelope: envelope["input"].update(unapproved="synthetic-private"),
            lambda envelope: envelope.update(unapproved="synthetic-private"),
            lambda envelope: envelope["policy"].update(unapproved="synthetic-private"),
            lambda envelope: envelope.update(format_repair={"attempt": 2}),
        ):
            client = FakeClient()
            model = OpenAIStructuredModel(SETTINGS, lambda *args: True, client=client, env=ENV)
            envelope = json.loads(prompt())
            mutate(envelope)
            with self.assertRaises(ServiceError) as raised:
                model.generate("answer", json.dumps(envelope))
            self.assertEqual("MODEL_REMOTE_BLOCKED", raised.exception.code)
            self.assertEqual([], client.chat_calls)

    def test_chat_incomplete_refusal_and_unexpected_tool_response_are_safe_errors(self):
        responses = [chat_response(status="incomplete"), chat_response(error=SimpleNamespace(message="synthetic-secret")),
                     chat_response(output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="refusal")])]),
                     chat_response(output=[SimpleNamespace(type="function_call")]), chat_response(output_text="")]
        for response in responses:
            model = OpenAIStructuredModel(SETTINGS, lambda *args: True,
                                         client=FakeClient(chat=lambda **kwargs: response), env=ENV)
            with self.assertRaises(ServiceError) as raised:
                model.generate("answer", prompt())
            self.assertEqual("MODEL_UNAVAILABLE", raised.exception.code)
            self.assertNotIn("synthetic-secret", json.dumps(raised.exception.as_dict()))

    def test_format_repair_rechecks_authorization_and_does_not_send_invalid_output(self):
        client = FakeClient(chat=lambda **kwargs: chat_response(output_text="synthetic-invalid-secret"))
        checks = []

        def guard(kind, context_kind, context):
            checks.append(context_kind)
            return len(checks) <= 4

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);(root / "prompts").mkdir()
            (root / "prompts" / "answer.json").write_text(json.dumps(policy()), encoding="utf-8")
            model = OpenAIStructuredModel(SETTINGS, guard, client=client, env=ENV)
            with self.assertRaises(ServiceError) as raised:
                StructuredRunner(PromptStore(root), model).run("answer", deepcopy(INPUTS))
        self.assertEqual("MODEL_REMOTE_BLOCKED", raised.exception.code)
        self.assertEqual(1, len(client.chat_calls))
        self.assertEqual(["item", "question", "applicability", "review_context", "item"], checks)

    def test_sdk_factory_pins_origin_and_disables_proxy_redirect_and_retries(self):
        import openai
        captured, client = {}, FakeClient()

        def construct(**kwargs):
            captured.update(kwargs)
            return client

        with patch.object(openai, "OpenAI", side_effect=construct), patch.dict("os.environ", {
                "OPENAI_BASE_URL": "https://example.invalid", "HTTPS_PROXY": "https://example.invalid"}):
            model = OpenAIStructuredModel(SETTINGS, lambda *args: True, env=ENV)
            model.generate("answer", prompt())
        try:
            self.assertEqual("https://api.openai.com/v1", captured["base_url"])
            self.assertEqual(0, captured["max_retries"])
            self.assertEqual(20.0, captured["timeout"])
            self.assertFalse(captured["http_client"].follow_redirects)
            self.assertFalse(captured["http_client"]._trust_env)
        finally:
            captured["http_client"].close()

    def test_sdk_payload_logs_are_suppressed_but_application_audit_logs_remain(self):
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = Capture()
        provider = logging.getLogger("openai._base_client")
        audit = logging.getLogger("warranty.synthetic_audit")
        audit.addHandler(handler)
        audit.setLevel(logging.INFO)
        provider.addHandler(handler)

        def response(**kwargs):
            provider.error("synthetic-secret %s", kwargs)
            audit.info("Synthetic audit preserved")
            return chat_response()

        try:
            model = OpenAIStructuredModel(SETTINGS, lambda *args: True, client=FakeClient(chat=response), env=ENV)
            model.generate("answer", prompt())
        finally:
            audit.removeHandler(handler)
            provider.removeHandler(handler)
        self.assertEqual(["Synthetic audit preserved"], records)


if __name__ == "__main__":
    unittest.main()
