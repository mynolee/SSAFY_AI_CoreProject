"""Independent runtime authorization checks with authored sources and no network."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import socket
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from warranty.errors import ServiceError
from warranty.prompts import OpenAIStructuredModel, output_schema
from warranty.runtime import AuthorizedChatModel, ProcessingAuthorization, load_runtime, read_private_json
from warranty.security import SourcePolicy


DATASET = "a" * 64
KEY_MARKER = "sk-" + "S" * 24
ACTOR_A, ACTOR_B = "synthetic-buyer", "synthetic-seller"
SOURCE_A, SOURCE_B = "synthetic-source-a", "synthetic-source-b"
ITEM_A, ITEM_B = "synthetic-item-a", "synthetic-item-b"
VERSION_A, VERSION_B = "synthetic-version-a", "synthetic-version-b"


class SyntheticCatalog:
    dataset_hash = DATASET

    def __init__(self):
        self.records = {
            ITEM_A: {"item_id": ITEM_A, "name": "Synthetic item A", "source_id": SOURCE_A,
                     "source_url": "https://example.invalid/a", "index_text": "Synthetic metadata A"},
            ITEM_B: {"item_id": ITEM_B, "name": "Synthetic item B", "source_id": SOURCE_B,
                     "source_url": "https://example.invalid/b", "index_text": "Synthetic metadata B"},
        }

    @property
    def items(self):
        return deepcopy(list(self.records.values()))

    def get(self, item_id):
        if item_id not in self.records:
            raise ServiceError("ITEM_NOT_FOUND")
        return deepcopy(self.records[item_id])


def review(source, version, *, status="ALLOWED", actors=None):
    return {"source_id": source, "action": "external_process", "status": status, "basis_type": "LICENSE",
            "basis_url": "https://example.invalid/synthetic-permission", "reviewed_on": "2026-01-01",
            "scope": "Synthetic test only", "conditions": "Synthetic test only", "user_scope": "Synthetic test actors",
            "processing_environment": "Synthetic injected client only", "version_scope": "VERSION", "version_id": version,
            "allowed_actors": list(actors or [ACTOR_A, ACTOR_B])}


def policy():
    sources = [{"source_id": source, "url": "https://example.invalid/" + suffix}
               for source, suffix in ((SOURCE_A, "a"), (SOURCE_B, "b"))]
    reviews = [review(source, version) for source, version in
               ((SOURCE_A, DATASET), (SOURCE_B, DATASET), (SOURCE_A, VERSION_A), (SOURCE_B, VERSION_B))]
    return SourcePolicy.from_config({"schema_version": 1, "registry": sources, "reviews": reviews,
                                     "capabilities": {"synthetic-manager": ["DATA_MANAGER"]}}, network_enabled=False)


class SyntheticRegistry:
    def __init__(self, source_policy):
        self.policy = source_policy
        self.section_calls = []
        self.rows = [{"id": "synthetic-candidate-a", "item_id": ITEM_A, "source_id": SOURCE_A,
                      "version_id": VERSION_A, "role": "SERVICE_TERMS", "own_document": True,
                      "title": "Synthetic terms A", "canonical_url": "https://example.invalid/a",
                      "document_id": "synthetic-document-a", "document_type": "HTML",
                      "review_json": json.dumps({"is_synthetic": True})},
                     {"id": "synthetic-candidate-b", "item_id": ITEM_B, "source_id": SOURCE_B,
                      "version_id": VERSION_B, "role": "SERVICE_TERMS", "own_document": True,
                      "title": "Synthetic terms B", "canonical_url": "https://example.invalid/b",
                      "document_id": "synthetic-document-b", "document_type": "HTML",
                      "review_json": json.dumps({"is_synthetic": True})}]
        self.bodies = {VERSION_A: [{"section": "Synthetic section A", "text": "Synthetic approved clause A. Transfer review remains conditional."}],
                       VERSION_B: [{"section": "Synthetic section B", "text": "Synthetic approved clause B. Evidence review is required."}]}

    def candidates(self, item_id=None):
        return deepcopy([row for row in self.rows if item_id is None or row["item_id"] == item_id])

    def registered_candidates(self, item_id=None):
        return []

    def sections(self, candidate, actor=None, *, action="local_embed"):
        self.section_calls.append((candidate["source_id"], candidate["version_id"], actor, action))
        self.policy.require(candidate["source_id"], action, actor=actor, version_id=candidate["version_id"])
        return deepcopy(self.bodies[candidate["version_id"]])


class RuntimeAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = SimpleNamespace(remote_models=True, config_dir=Path(self.temp.name))
        (self.settings.config_dir / "runtime.json").write_text(json.dumps({"models": {
            "external_processing_approved": True, "allow_question_processing": True}}), encoding="utf-8")
        self.runtime = load_runtime(self.settings)
        self.catalog, self.policy = SyntheticCatalog(), policy()
        self.authorization = ProcessingAuthorization(self.settings, self.runtime, self.catalog, self.policy)
        self.registry = SyntheticRegistry(self.policy)
        self.authorization.registry = self.registry
        self.network = []
        self.stack = ExitStack()

        def denied(*args, **kwargs):
            self.network.append(True)
            raise AssertionError("No network in runtime authorization checks")

        self.stack.enter_context(patch.object(socket.socket, "connect", denied))
        self.stack.enter_context(patch.object(socket, "create_connection", denied))

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()
        self.assertEqual([], self.network)

    def metadata_context(self, source=SOURCE_A, actor=ACTOR_A):
        return {"kind": "metadata", "source_id": source, "version_id": DATASET, "actor": actor}

    def document_context(self, source=SOURCE_A, version=VERSION_A, actor=ACTOR_A):
        return {"kind": "document", "source_id": source, "version_id": version, "actor": actor}

    def document_evidence(self):
        row = self.registry.candidates(ITEM_A)[0]
        return {"evidence_id": "synthetic-evidence-a", "item_id": ITEM_A, "source_type": "synthetic_document",
                "source_id": SOURCE_A, "version_id": VERSION_A, "document_version_id": VERSION_A,
                "document_id": row["document_id"], "role": row["role"], "url": row["canonical_url"],
                "type": "HTML", "section": "Synthetic section A", "revision": "synthetic-revision",
                "text": "Synthetic approved clause A."}

    def test_missing_scope_and_runtime_approval_deny_by_default(self):
        self.assertFalse(self.authorization.model_guard("answer", "item", self.catalog.get(ITEM_A)))
        with self.authorization.scope(ACTOR_A, ITEM_A):
            self.runtime["models"]["external_processing_approved"] = False
            self.assertFalse(self.authorization.model_guard("answer", "item", self.catalog.get(ITEM_A)))
            self.runtime["models"]["external_processing_approved"] = True
            self.settings.remote_models = False
            self.assertFalse(self.authorization.model_guard("answer", "question", {"question": "Synthetic question"}))

    def test_empty_no_rag_item_does_not_bypass_scope_or_question_approval(self):
        self.assertFalse(self.authorization.model_guard("answer", "item", {}))
        with self.authorization.scope(ACTOR_A, ITEM_A):
            self.assertTrue(self.authorization.model_guard("answer", "item", {}))
            self.runtime["models"]["allow_question_processing"] = False
            self.assertFalse(self.authorization.model_guard("answer", "question", {"question": "Synthetic question"}))

    def test_scope_is_restored_after_nested_failure(self):
        with self.authorization.scope(ACTOR_A, ITEM_A):
            self.assertTrue(self.authorization.model_guard("answer", "item", self.catalog.get(ITEM_A)))
            with self.assertRaises(RuntimeError):
                with self.authorization.scope(ACTOR_B, ITEM_B):
                    self.assertTrue(self.authorization.model_guard("answer", "item", self.catalog.get(ITEM_B)))
                    self.assertFalse(self.authorization.model_guard("answer", "item", self.catalog.get(ITEM_A)))
                    raise RuntimeError("Synthetic interruption")
            self.assertTrue(self.authorization.model_guard("answer", "item", self.catalog.get(ITEM_A)))
        self.assertFalse(self.authorization.model_guard("answer", "item", {}))

    def test_actor_and_item_scopes_are_isolated_between_threads(self):
        barrier = threading.Barrier(2)

        def invoke(pair):
            actor, item_id = pair
            other = ITEM_B if item_id == ITEM_A else ITEM_A
            with self.authorization.scope(actor, item_id):
                barrier.wait(timeout=5)
                return (self.authorization.model_guard("answer", "item", self.catalog.get(item_id)),
                        self.authorization.model_guard("answer", "item", self.catalog.get(other)))

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual([(True, False), (True, False)], list(pool.map(invoke, [(ACTOR_A, ITEM_A), (ACTOR_B, ITEM_B)])))
        self.assertFalse(self.authorization.model_guard("answer", "item", {}))

    def test_current_actor_and_exact_metadata_version_are_required(self):
        self.policy.set_review("synthetic-manager", review(SOURCE_A, DATASET, actors=[ACTOR_A]))
        with self.authorization.scope(ACTOR_A, ITEM_A):
            self.assertTrue(self.authorization.model_guard("answer", "item", self.catalog.get(ITEM_A)))
        with self.authorization.scope(ACTOR_B, ITEM_A):
            self.assertFalse(self.authorization.model_guard("answer", "item", self.catalog.get(ITEM_A)))
        self.policy.set_review("synthetic-manager", review(SOURCE_A, DATASET, status="BLOCKED"))
        with self.authorization.scope(ACTOR_A, ITEM_A):
            self.assertFalse(self.authorization.model_guard("answer", "item", self.catalog.get(ITEM_A)))

    def test_forged_item_and_candidate_fields_are_never_forwarded(self):
        candidate = self.registry.candidates(ITEM_A)[0]
        public = {key: candidate[key] for key in ("id", "title", "role", "own_document")}
        with self.authorization.scope(ACTOR_A, ITEM_A):
            self.assertTrue(self.authorization.model_guard("selection", "candidate", public))
            for mutation in ({"id": "synthetic-forged"}, {"title": "Forged title"}, {"role": "TRANSFER_GUIDE"},
                             {"own_document": False}, {"actor": ACTOR_B}):
                self.assertFalse(self.authorization.model_guard("selection", "candidate", {**public, **mutation}))
            self.assertFalse(self.authorization.model_guard("query", "item", {**self.catalog.get(ITEM_A), "name": "Forged"}))

    def test_metadata_evidence_requires_current_item_source_and_text(self):
        evidence = {"item_id": ITEM_A, "source_id": SOURCE_A, "source_type": "metadata", "text": "Synthetic metadata A"}
        with self.authorization.scope(ACTOR_A, ITEM_A):
            self.assertTrue(self.authorization.model_guard("answer", "evidence", evidence))
            for mutation in ({"item_id": ITEM_B}, {"source_id": SOURCE_B}, {"text": "Synthetic metadata B"}):
                self.assertFalse(self.authorization.model_guard("answer", "evidence", {**evidence, **mutation}))

    def test_document_evidence_is_tied_to_actual_approved_body(self):
        evidence = self.document_evidence()
        with self.authorization.scope(ACTOR_A, ITEM_A):
            self.assertTrue(self.authorization.model_guard("answer", "evidence", evidence))
            for mutation in ({"text": "Forged unregistered body"}, {"text": "Synthetic approved clause B."},
                             {"source_id": SOURCE_B}, {"version_id": VERSION_B}, {"url": "https://example.invalid/forged"}):
                with self.subTest(mutation=mutation):
                    self.assertFalse(self.authorization.model_guard("answer", "evidence", {**evidence, **mutation}))

    def test_document_locator_and_provenance_type_cannot_be_forged(self):
        evidence = self.document_evidence()
        with self.authorization.scope(ACTOR_A, ITEM_A):
            for mutation in ({"source_type": "official_document"}, {"type": "PDF"},
                             {"section": "Unregistered section"}, {"document_id": "foreign-document"},
                             {"body_path": "synthetic-private/internal-source.json"}):
                with self.subTest(mutation=mutation):
                    self.assertFalse(self.authorization.model_guard("answer", "evidence", {**evidence, **mutation}))

    def test_source_permission_is_rechecked_after_revocation(self):
        evidence = self.document_evidence()
        with self.authorization.scope(ACTOR_A, ITEM_A):
            self.assertTrue(self.authorization.model_guard("answer", "evidence", evidence))
            self.policy.set_review("synthetic-manager", review(SOURCE_A, VERSION_A, status="BLOCKED"))
            self.assertFalse(self.authorization.model_guard("answer", "evidence", evidence))

    def test_personal_question_information_is_blocked_in_all_derived_user_contexts(self):
        for text in ("synthetic.person@example.invalid", "010-1234-5678"):
            with self.authorization.scope(ACTOR_A, ITEM_A):
                for context_kind, context in (("question", {"question": text}),
                                              ("applicability", {"country": text}),
                                              ("review_context", {"missing_information": [text], "conflicts": []})):
                    with self.subTest(context_kind=context_kind, text=text):
                        self.assertFalse(self.authorization.model_guard("answer", context_kind, context))

    def test_key_patterns_are_blocked_in_model_and_embedding_inputs(self):
        with self.authorization.scope(ACTOR_A, ITEM_A):
            for kind, context in (("question", {"question": KEY_MARKER}),
                                  ("review_context", {"missing_information": [KEY_MARKER], "conflicts": []}),
                                  ("evidence", {**self.document_evidence(), "text": KEY_MARKER})):
                self.assertFalse(self.authorization.model_guard("answer", kind, context))
        self.assertFalse(self.authorization.embedding_guard([KEY_MARKER], [self.document_context()]))

    def test_metadata_embedding_requires_every_text_source_in_the_approved_contexts(self):
        context_a, context_b = self.metadata_context(), self.metadata_context(SOURCE_B)
        self.assertTrue(self.authorization.embedding_guard(["Synthetic metadata A"], [context_a]))
        self.assertTrue(self.authorization.embedding_guard(["Synthetic metadata A", "Synthetic metadata B"], [context_a, context_b]))
        self.assertFalse(self.authorization.embedding_guard(["Synthetic metadata B"], [context_a]))
        self.assertFalse(self.authorization.embedding_guard(["Synthetic metadata A", "Synthetic metadata B"], [context_a]))

    def test_metadata_embedding_requires_current_dataset_and_actor_review(self):
        self.assertFalse(self.authorization.embedding_guard(["Synthetic metadata A"], [{**self.metadata_context(), "version_id": "foreign"}]))
        self.policy.set_review("synthetic-manager", review(SOURCE_A, DATASET, actors=[ACTOR_A]))
        self.assertFalse(self.authorization.embedding_guard(["Synthetic metadata A"], [self.metadata_context(actor=ACTOR_B)]))

    def test_document_embedding_uses_registered_body_of_each_approved_source(self):
        self.assertTrue(self.authorization.embedding_guard(["Synthetic approved clause A."], [self.document_context()]))
        self.assertFalse(self.authorization.embedding_guard(["Synthetic approved clause B."], [self.document_context()]))
        self.assertFalse(self.authorization.embedding_guard(["Forged text"], [self.document_context()]))
        self.assertFalse(self.authorization.embedding_guard(["Synthetic approved clause A."], [{**self.document_context(), "version_id": VERSION_B}]))

    def test_question_embedding_requires_approval_and_blocks_detectable_pii(self):
        context = {"kind": "question", "source_id": None, "version_id": None, "actor": ACTOR_A}
        self.assertTrue(self.authorization.embedding_guard(["Synthetic question"], [context]))
        self.assertFalse(self.authorization.embedding_guard(["synthetic.person@example.invalid"], [context]))
        self.runtime["models"]["allow_question_processing"] = False
        self.assertFalse(self.authorization.embedding_guard(["Synthetic question"], [context]))

    def test_empty_mixed_unknown_and_forged_embedding_contexts_deny(self):
        for contexts in ([], [{"kind": "unknown", "actor": ACTOR_A}], [self.metadata_context(), self.document_context()],
                         [{**self.metadata_context(), "actor": "synthetic-outsider"}]):
            self.assertFalse(self.authorization.embedding_guard(["Synthetic metadata A"], contexts))

    def test_authorized_wrapper_without_scope_cannot_reach_injected_provider(self):
        calls = []

        def request(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(output_text=json.dumps({"classification": "NEEDS_INFORMATION", "answer": "Synthetic reply",
                "claims": [], "conditions": [], "missing_information": [], "conflicts": [], "cited_evidence_ids": []}),
                status="completed", error=None, incomplete_details=None, output=[], model="synthetic-provider")

        adapter = OpenAIStructuredModel(self.settings, self.authorization.model_guard,
            client=SimpleNamespace(responses=SimpleNamespace(create=request)),
            env={"OPENAI_API_KEY": "synthetic-secret", "CHAT_MODEL": "synthetic-model"})
        wrapper = AuthorizedChatModel(adapter, self.authorization)
        variables = {"item": {}, "question": "Synthetic question", "applicability": {},
                     "evidence": [], "missing_information": [], "conflicts": []}
        fixture = {"version": "1.0.0", "kind": "answer", "role": "Synthetic fixture", "template": "Synthetic fixture",
                   "input_variables": list(variables), "output_schema": output_schema("answer"),
                   **{key: ["Synthetic rule"] for key in ("evidence_limits", "missing_information_rules", "conflict_rules", "attack_rules")}}
        prompt = json.dumps({"policy": fixture, "input": variables})
        with self.assertRaises(ServiceError) as raised:
            wrapper.generate("answer", prompt)
        self.assertEqual("MODEL_REMOTE_BLOCKED", raised.exception.code)
        self.assertEqual([], calls)
        with wrapper.processing_context(ACTOR_A, ITEM_A):
            wrapper.generate("answer", prompt)
        self.assertEqual(1, len(calls))
        self.assertEqual(adapter.metadata(), wrapper.metadata())
        self.assertNotIn("synthetic-secret", json.dumps(wrapper.metadata()))


class PrivateRuntimeParsingTests(unittest.TestCase):
    def test_default_runtime_is_offline_and_permission_free(self):
        with tempfile.TemporaryDirectory() as directory:
            result = load_runtime(SimpleNamespace(config_dir=Path(directory)))
        self.assertEqual("mock", result["models"]["chat_provider"])
        self.assertEqual("mock", result["models"]["embedding_provider"])
        self.assertFalse(result["models"]["external_processing_approved"])
        self.assertFalse(result["models"]["allow_question_processing"])
        self.assertFalse(result["collection"]["network_enabled"])

    def test_duplicate_keys_nonfinite_values_and_oversized_config_are_safe_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.json"
            for raw in ('{"models":{},"models":{}}', '{"retrieval":{"lambda_mult":NaN}}', " " * 65537):
                path.write_text(raw, encoding="utf-8")
                with self.assertRaises(ServiceError) as raised:
                    load_runtime(SimpleNamespace(config_dir=Path(directory)))
                self.assertEqual("CONFIG_INVALID", raised.exception.code)
                self.assertNotIn(str(path), json.dumps(raised.exception.as_dict()))

    def test_unhashable_provider_and_strategy_types_are_safe_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.json"
            for value in ({"models": {"chat_provider": []}}, {"models": {"embedding_provider": {}}},
                          {"retrieval": {"strategy": []}}):
                path.write_text(json.dumps(value), encoding="utf-8")
                with self.subTest(value=value):
                    with self.assertRaises(ServiceError) as raised:
                        load_runtime(SimpleNamespace(config_dir=Path(directory)))
                    self.assertEqual("CONFIG_INVALID", raised.exception.code)

    def test_runtime_cannot_carry_api_keys_or_implicit_network_authorization(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.json"
            for value in ({"OPENAI_API_KEY": KEY_MARKER}, {"models": {"api_key": KEY_MARKER}},
                          {"collection": {"network_enabled": True}}, {"models": {"external_processing_approved": "true"}}):
                path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(ServiceError) as raised:
                    load_runtime(SimpleNamespace(config_dir=Path(directory)))
                self.assertEqual("CONFIG_INVALID", raised.exception.code)
                self.assertNotIn(KEY_MARKER, json.dumps(raised.exception.as_dict()))

    def test_missing_private_json_default_is_a_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            default = {"nested": []}
            result = read_private_json(Path(directory) / "missing.json", default=default)
            result["nested"].append("Synthetic mutation")
        self.assertEqual({"nested": []}, default)


if __name__ == "__main__":
    unittest.main()
