"""Public chat and document embeddings over fake HTTP, with local-only metadata.

All text, identities, source reviews, credentials and prices below are authored
fixtures. Only temporary databases are opened; no account or environment lookup
is used to compose the adapters.
"""
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from warranty.catalog import Catalog
from warranty.config import Settings
from warranty.db import Database
from warranty.documents import ACTIONS, DocumentRegistry, parse_document
from warranty.embedding_cache import EmbeddingCache
from warranty.errors import ServiceError
from warranty.gms import GMS_BASE_URL, GMSEmbeddings
from warranty.live_budget import CreditBudget, PURPOSES
from warranty.public_gms import GMSPublicStructuredModel
from warranty.public_protocol import public_instruction_hashes, public_source_sha256
from warranty.public_runtime import PublicProcessingContext, public_bundle
from warranty.rag import RagService
from warranty.runtime import AuthorizedChatModel, ProcessingAuthorization
from warranty.security import SourcePolicy
from warranty.usage import UsageBudget
from warranty.vectorstore import MockEmbeddings


ACTOR = "buyer"
APPROVAL = "SYNTHETIC-public-integration"
QUESTION = "What confirmation does this fictional public clause require?"
EXCERPT = "Transfer requires provider confirmation in this authored fictional example."
TITLE = "Authored public integration terms"
URL = "https://example.invalid/public-integration-terms"
SOURCE = "SYNTHETIC-PUBLIC-DOCUMENT"
SENTINELS = ("PRIVATE_POLICY_SENTINEL", "PRIVATE_SECRET_SENTINEL",
             "PRIVATE_CATALOG_SENTINEL", "INTERNAL_ID_SENTINEL",
             "VALIDATION_TEXT_SENTINEL", "REFERENCE_ANSWER_SENTINEL")
ENV = {"GMS_API_KEY": "synthetic-not-a-credential-public-integration",
       "GMS_BASE_URL": GMS_BASE_URL, "GMS_CHAT_MODEL": "gpt-4.1-mini",
       "GMS_CHAT_MODEL_VERSION": "SYNTHETIC-public-chat-version",
       "GMS_CHAT_ROUTE": "chat/completions", "GMS_MAX_OUTPUT_TOKENS": "128",
       "GMS_EMBEDDING_MODEL": "text-embedding-3-small",
       "GMS_EMBEDDING_MODEL_VERSION": "SYNTHETIC-public-embedding-version",
       "GMS_EMBEDDING_DIMENSION": "2"}
SCOPE = {"country": "SYNTHETIC-COUNTRY", "plan_generation": "SYNTHETIC-PLAN",
         "payment_method": "SYNTHETIC-MONTHLY", "channel": "SYNTHETIC-CHANNEL",
         "document_type": "SERVICE_TERMS", "date_start": "2020-01-01",
         "date_end": "2030-12-31"}
INPUTS = {key: value for key, value in SCOPE.items() if not key.startswith("date_")}
INPUTS["enrollment_date"] = "2025-01-01"


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class SentinelPromptStore:
    """A local envelope deliberately containing non-public fixture values."""
    def load(self, kind):
        return {"kind": kind, "policy": SENTINELS[0]}

    def render(self, kind, variables):
        value = deepcopy(variables)
        value["item"]["local_only_test_values"] = list(SENTINELS[1:])
        return json.dumps({"policy": self.load(kind), "input": value})


class ObservedCache(EmbeddingCache):
    """Keep the production persistent cache; observe successful local reads."""
    hits = 0

    def get(self, *args, **kwargs):
        result = super().get(*args, **kwargs)
        if result is not None:
            self.hits += 1
        return result


def credit_profile():
    common = {"input_bound": "wire_utf8_bytes", "input_bound_confirmed": True,
              "input_token_overhead": 0, "max_input_tokens": 100000,
              "output_bound_confirmed": True, "input_microcredits_per_token": 1}
    return {"profile_id": "SYNTHETIC-integration-prices", "pricing_confirmed": True,
            "pricing_version": "SYNTHETIC-NOT-REAL-PRICES",
            "allocations": {purpose: "100" for purpose in PURPOSES},
            "demo_reserve_credits": "10", "models": {
                "gpt-4.1-mini": {**common, "kind": "chat", "route": "chat.completions",
                    "output_microcredits_per_token": 1, "output_bound": "max_tokens",
                    "max_output_tokens": 128},
                "text-embedding-3-small": {**common, "kind": "embedding", "route": "embeddings",
                    "output_microcredits_per_token": 0, "output_bound": "none", "max_output_tokens": 0}}}


class PublicRagIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="warranty-public-rag-integration-")
        self.addCleanup(self.temp.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("socket.create_connection", side_effect=AssertionError("Network forbidden")))
        self.stack.enter_context(patch("socket.socket.connect", side_effect=AssertionError("Network forbidden")))
        self.settings = Settings(Path(self.temp.name) / "data", Path(self.temp.name) / "config", remote_models=True)
        self.settings.prepare()
        payload = deepcopy(Catalog.synthetic()._payload)
        for ordinal, item in enumerate(payload["items"]):
            item["item_id"] = f"SYNTHETIC-INTERNAL_ID_SENTINEL-{ordinal}"
            item["index_text"] = "PRIVATE_CATALOG_SENTINEL VALIDATION_TEXT_SENTINEL"
        self.catalog = Catalog(payload, sha(json.dumps(payload, sort_keys=True)))
        self.item, self.other = [row["item_id"] for row in self.catalog.items]
        self.db = Database(self.settings.data_dir / "integration.sqlite3")
        self.db.initialize()
        self.addCleanup(self.db.close)
        with self.db.transaction() as connection:
            for item in self.catalog.items:
                connection.execute("INSERT INTO items(item_id,name,provider) VALUES(?,?,?)",
                                   (item["item_id"], item["name"], item["provider"]))
        sources = [{"source_id": source_id, **row} for source_id, row in self.catalog.sources.items()]
        sources.append({"source_id": SOURCE, "url": URL, "allowed_hosts": ["example.invalid"]})
        reviews = [self.review(source["source_id"], action) for source in sources for action in ACTIONS]
        for review in reviews:
            if review["source_id"] != SOURCE and review["action"] == "external_process":
                review["status"] = "BLOCKED"
        self.policy = SourcePolicy(sources, reviews, {ACTOR: {"DATA_MANAGER"}})
        registry = DocumentRegistry(self.catalog, self.db, self.settings, self.policy)
        document = registry.register_document(ACTOR, source_id=SOURCE, title=TITLE,
            canonical_url=URL, document_type="HTML", is_synthetic=True)
        body = ("<html><p>" + EXCERPT + "</p></html>").encode("utf-8")
        version = registry.ingest_bytes(ACTOR, document["document_id"], body, applicability=SCOPE)
        self.version = version["version_id"]
        registry.link(ACTOR, self.item, self.version, role="SERVICE_TERMS",
                      applicability_basis="This authored fixture explicitly covers the chosen synthetic item.")
        section = parse_document(body, "HTML")[0]
        public_source = {"source_id": SOURCE, "version_id": self.version, "title": TITLE,
                         "url": URL, "excerpt": EXCERPT, "section": section["section"]}
        protocol = {"schema_version": 1, "model": "gpt-4.1-mini", "route": "chat/completions",
                    "instruction_sha256s": public_instruction_hashes(), "question_sha256s": [sha(QUESTION)],
                    "source_sha256s": [public_source_sha256({"title": TITLE, "url": URL, "excerpt": EXCERPT})],
                    "max_request_bytes": 16384, "max_output_tokens": 128}
        self.bundle = public_bundle({"schema_version": 1, "protocol": protocol, "item_ids": [self.item],
                                    "sources": [public_source], "embedding_document_sha256s": [sha(EXCERPT)]})
        runtime = {"models": {"external_processing_approved": True, "allow_question_processing": True}}
        self.authorization = ProcessingAuthorization(self.settings, runtime, self.catalog, self.policy)
        self.public = PublicProcessingContext(self.bundle, self.authorization)
        fingerprint = self.public.fingerprint
        self.usage = UsageBudget(self.settings.data_dir / "usage.sqlite3", APPROVAL,
            {"chat_requests": 30, "embedding_requests": 30, "input_bytes": 1000000},
            context_fingerprint=fingerprint)
        self.credit = CreditBudget(self.settings.data_dir / "credits.sqlite3", APPROVAL,
                                   credit_profile(), context_fingerprint=fingerprint)
        self.cache = ObservedCache(self.settings.data_dir / "cache.sqlite3")
        self.requests, self.embedding_inputs, self.chat_kinds = [], [], []
        self.phase = None
        self.fail_next_chat = False
        self.reported_chat_model = "gpt-4.1-mini"
        def resolve(kind, variables):
            self.phase = kind
            return self.public.source_resolver(kind, variables)
        self.chat = GMSPublicStructuredModel(self.settings, self.public.model_guard,
            public_manifest=protocol, public_source_resolver=resolve, env=dict(ENV),
            usage_budget=self.usage, credit_budget=self.credit, transport=httpx.MockTransport(self.transport))
        self.chat.cache_identity = "gms-public:" + fingerprint
        self.addCleanup(self.chat.close)
        self.embedding = GMSEmbeddings(self.settings, self.public.embedding_guard, env=dict(ENV),
            usage_budget=self.usage, credit_budget=self.credit, embedding_cache=self.cache,
            transport=httpx.MockTransport(self.transport))
        self.embedding.approved_document_sha256s = self.public.document_sha256s
        self.embedding.public_protocol_only = True
        self.addCleanup(self.embedding.close)
        model = AuthorizedChatModel(self.chat, self.authorization)
        self.rag = RagService(self.catalog, self.db, self.settings, model=model,
            prompt_store=SentinelPromptStore(), source_policy=self.policy, embeddings=self.embedding,
            metadata_embeddings=MockEmbeddings(), document_item_ids=list(self.public.item_ids), index_actor=ACTOR)
        self.addCleanup(self.rag.close)
        self.authorization.registry, self.authorization.vectors = self.rag.registry, self.rag.vectors

    @staticmethod
    def review(source_id, action, status="ALLOWED"):
        return {"source_id": source_id, "action": action, "status": status,
                "basis_type": "OTHER_RECORDED_BASIS", "basis_url": URL,
                "reviewed_on": "2026-01-01", "scope": "Authored offline integration fixture",
                "conditions": "No real content or provider request", "user_scope": "Fixture buyer only",
                "processing_environment": "LOCAL MOCK HTTP", "allowed_actors": [ACTOR]}

    def transport(self, request):
        body = json.loads(request.content)
        self.requests.append((request.url.path, body))
        if request.url.path.endswith("/embeddings"):
            values = body["input"] if isinstance(body["input"], list) else [body["input"]]
            self.embedding_inputs.extend(values)
            return httpx.Response(200, json={"model": "text-embedding-3-small",
                "data": [{"index": 0, "embedding": [1.0, 0.0]}], "usage": {"prompt_tokens": 1}})
        self.chat_kinds.append(self.phase)
        if self.fail_next_chat:
            self.fail_next_chat = False
            return httpx.Response(503, json={"error": "SYNTHETIC transient failure"})
        answers = {"query": {"queries": ["fictional public transfer confirmation"]},
                   "selection": {"selected": ["S1"]},
                   "answer": {"answer": EXCERPT + " [S1]", "citations": ["S1"]}}
        return httpx.Response(200, json={"model": self.reported_chat_model,
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps(answers[self.phase])}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    def execution(self):
        stack = ExitStack()
        stack.enter_context(self.chat.live_execution("verification", APPROVAL))
        stack.enter_context(self.embedding.live_execution("verification", APPROVAL))
        return stack

    def prepare(self):
        result = self.rag.prepare(ACTOR, self.item, INPUTS, question=QUESTION)
        self.assertEqual(result["status"], "COMPLETED", result)
        return result

    def ask(self, question=QUESTION, actor=ACTOR):
        return self.rag.ask(actor, self.item, question, INPUTS, cache=False)

    def assert_public_wire(self):
        encoded = json.dumps(self.requests, ensure_ascii=False)
        for value in (*SENTINELS, ENV["GMS_API_KEY"], self.item, self.other, self.version, str(self.settings.data_dir)):
            self.assertNotIn(value, encoded)
        for path, body in self.requests:
            if path.endswith("/embeddings"):
                continue
            self.assertIs(body["store"], False)
            self.assertIs(body["stream"], False)
            payload = json.loads(body["messages"][1]["content"])
            self.assertEqual(set(payload), {"question", "sources"})
            self.assertEqual(payload["question"], QUESTION)
            for source in payload["sources"]:
                self.assertEqual(set(source), {"label", "title", "url", "excerpt"})
        self.assertTrue(set(self.embedding_inputs).issubset({EXCERPT, QUESTION}))

    def test_public_three_stage_answer_remaps_aliases_and_keeps_metadata_local(self):
        self.assertEqual(self.rag.vectors.meta.count(), 2)
        self.assertIs(self.rag.vectors.metadata_embeddings.is_remote, False)
        self.assertEqual((self.rag.vectors.metadata_embeddings.dimension, self.embedding.dimension), (64, 2))
        with self.execution():
            self.prepare()
            result = self.ask()
        self.assertEqual(self.chat_kinds, ["query", "selection", "answer"])
        self.assertEqual(result["classification"], "NEEDS_INFORMATION")
        self.assertEqual(len(result["cited_evidence_ids"]), 1)
        evidence = result["evidence"][0]
        self.assertEqual(evidence["number"], 2)
        self.assertEqual(result["cited_evidence_ids"], [evidence["evidence_id"]])
        self.assertTrue(evidence["evidence_id"].startswith("doc-"))
        self.assertIn(f"[{evidence['number']}]", result["answer"])
        self.assertNotIn("[S1]", result["answer"])
        self.assertEqual(evidence["version_id"], self.version)
        self.assertIn("meta-" + self.item, result["retrieved_evidence_ids"])
        self.assertEqual(set(self.embedding_inputs), {EXCERPT, QUESTION})
        self.assert_public_wire()

    def test_repeat_index_has_no_growth_and_question_uses_persistent_cache(self):
        with self.execution():
            first = self.prepare()
            self.ask()
            before = (self.rag.vectors.meta.count(), self.rag.vectors.docs.count())
            count = len(self.embedding_inputs)
            second = self.prepare()
            self.ask()
        self.assertEqual(first["revision"], second["revision"])
        self.assertEqual(before, (self.rag.vectors.meta.count(), self.rag.vectors.docs.count()))
        self.assertEqual(len(self.embedding_inputs), count)
        self.assertGreater(self.cache.hits, 0)
        self.assertEqual(self.chat_kinds, ["query", "selection", "answer"] * 2)
        self.assertEqual(self.usage.snapshot()["embedding_requests"], 2)
        self.assert_public_wire()

    def test_missing_execution_unknown_question_and_out_of_scope_item_make_no_request(self):
        result = self.ask()
        self.assertEqual(result["classification"], "NEEDS_INFORMATION")
        with self.execution():
            result = self.ask("An unapproved fictional question")
            self.assertEqual(result["classification"], "NEEDS_INFORMATION")
            blocked = self.rag.prepare(ACTOR, self.other, INPUTS, question=QUESTION)
            self.assertEqual(blocked["status"], "BLOCKED")
            self.assertEqual(blocked["failure_code"], "SOURCE_BLOCKED")
        self.assertEqual(self.requests, [])
        self.assertEqual(self.usage.snapshot()["chat_requests"], 0)
        self.assertEqual(self.credit.snapshot()["request_count"], 0)

    def test_source_revocation_blocks_prepared_and_cached_embedding_reuse(self):
        with self.execution():
            self.prepare()
            self.ask()
            count = len(self.requests)
            hits = self.cache.hits
            self.policy.set_review(ACTOR, self.review(SOURCE, "external_process", "BLOCKED"))
            result = self.rag.prepare(ACTOR, self.item, INPUTS, question=QUESTION)
            with self.embedding.processing_context([{"kind": "document", "source_id": SOURCE,
                                                     "version_id": self.version, "actor": ACTOR}]):
                with self.assertRaises(ServiceError):
                    self.embedding.embed([EXCERPT])
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["failure_code"], "SOURCE_BLOCKED")
        self.assertEqual(len(self.requests), count)
        self.assertEqual(self.cache.hits, hits)
        self.assert_public_wire()

    def test_http_failure_stops_graph_without_repair_or_retry(self):
        with self.execution():
            self.prepare()
            count = len(self.requests)
            self.fail_next_chat = True
            result = self.ask()
        self.assertEqual(len(self.requests) - count, 1)
        self.assertEqual(self.chat_kinds, ["query"])
        self.assertEqual(result["classification"], "NEEDS_INFORMATION")
        self.assertEqual(self.rag.runner.last_attempts, 1)
        self.assertEqual(self.credit.snapshot()["unknown_request_count"], 2)
        self.assert_public_wire()

    def test_reported_model_version_mismatch_freezes_shared_budget_before_followup(self):
        with self.execution():
            self.prepare()
            count = len(self.requests)
            self.reported_chat_model = "gpt-4.1-mini-UNAPPROVED-VERSION"
            result = self.ask()
            second = self.ask()
        self.assertEqual(len(self.requests) - count, 1)
        self.assertEqual(self.chat_kinds, ["query"])
        self.assertEqual(result["classification"], "NEEDS_INFORMATION")
        self.assertEqual(second["classification"], "NEEDS_INFORMATION")
        self.assertFalse(self.credit.ready)
        self.assertFalse(self.credit.snapshot()["ready"])
        self.assertEqual(self.usage.snapshot()["chat_requests"], 1)
        self.assert_public_wire()

    def test_public_conditions_reach_local_checklist_without_payment_authority(self):
        from warranty.documents import digest

        private_title = "PRIVATE local review title"
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE documents SET title=? WHERE document_id="
                "(SELECT document_id FROM document_versions WHERE version_id=?)",
                (private_title, self.version))

        def conditional_transport(request):
            response = self.transport(request)
            if self.phase != "answer":
                return response
            payload = response.json()
            payload["choices"][0]["message"]["content"] = json.dumps({
                "classification": "TRANSFERABLE", "answer": EXCERPT + " [S1]",
                "citations": ["S1"],
                "conditions": [{"text": EXCERPT, "citations": ["S1"]}]})
            return httpx.Response(200, json=payload)

        # Only this new case replaces the fake HTTP boundary. The original six
        # fixtures and their default two-field answer remain unchanged.
        self.assertIsNone(self.chat._gateway._client)
        with patch.object(self.chat._gateway, "_transport", httpx.MockTransport(conditional_transport)):
            with self.execution():
                self.prepare()
                result = self.ask()

        self.assertEqual(self.chat_kinds, ["query", "selection", "answer"])
        self.assertEqual(result["classification"], "CONDITIONAL")
        self.assertFalse(result["conditions_ok"])
        self.assertEqual(len(result["conditions"]), 1)
        condition = result["conditions"][0]
        self.assertEqual(condition["text"], EXCERPT)
        self.assertEqual(condition["evidence_ids"], result["cited_evidence_ids"])
        condition_id = "condition-" + digest({
            "text": EXCERPT, "evidence_ids": sorted(result["cited_evidence_ids"])})
        self.assertEqual(condition["condition_id"], condition_id)
        self.assertEqual(result["required_condition_ids"], [condition_id])
        self.assertEqual(result["evidence"][0]["title"], private_title)
        self.assertEqual(self.bundle.value["sources"][0]["title"], TITLE)
        checklist = self.rag.checklist(self.item, INPUTS, user_id=ACTOR, review_id=result["review_id"])
        self.assertEqual(checklist["classification"], "CONDITIONAL")
        self.assertEqual(checklist["conditions"], result["conditions"])
        self.assertEqual(checklist["required_condition_ids"], [condition_id])
        self.assertEqual(checklist["cited_evidence_ids"], result["cited_evidence_ids"])
        self.assertFalse(checklist["conditions_ok"])
        self.assertEqual(result["classification_scope"],
                         "document_conditions_only; not transfer completion or payment authorization")
        self.assertNotIn(private_title, json.dumps(self.requests))
        self.assert_public_wire()


if __name__ == "__main__":
    unittest.main()
