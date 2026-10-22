"""Offline regressions for private policy loading and authorization fencing."""
import copy
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from warranty.errors import ServiceError
from warranty.security import SafeFetcher, SourcePolicy
from warranty.runtime import ProcessingAuthorization, load_source_policy
from warranty.embeddings import OpenAIEmbeddings
from warranty.application import build_application
from warranty.config import Settings
from warranty.prompts import output_schema


SOURCE = {"source_id": "TEST_SOURCE", "url": "https://example.org/terms", "is_synthetic": True}


def record(action="external_process", **changes):
    value = {"source_id": "TEST_SOURCE", "action": action, "status": "ALLOWED",
             "basis_type": "OTHER_RECORDED_BASIS", "basis_url": "https://example.org/review",
             "reviewed_on": "2026-10-22", "scope": "Synthetic fixture source-wide processing",
             "conditions": "Test transport only", "user_scope": "Named test actors",
             "processing_environment": "Offline adapter spy", "version_scope": "SOURCE",
             "allowed_actors": ["manager", "buyer"]}
    value.update(changes)
    return value


def configuration(*reviews):
    return {"schema_version": 1, "registry": [copy.deepcopy(SOURCE)],
            "reviews": list(reviews or [record()]), "capabilities": {"manager": ["DATA_MANAGER"]}}


def public_dns(host, port, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]


class RuntimeSecurityTests(unittest.TestCase):
    def setUp(self):
        # These tests never contact DNS, HTTPS or any external provider.
        self.sockets = [patch("socket.create_connection", side_effect=AssertionError("Network forbidden")),
                        patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))]
        for blocked in self.sockets:
            blocked.start()
            self.addCleanup(blocked.stop)

    def assert_code(self, code, callback):
        with self.assertRaises(ServiceError) as raised:
            callback()
        self.assertEqual(code, raised.exception.code)

    def test_explicit_source_scope_allows_new_versions_only_for_named_actors(self):
        policy = SourcePolicy.from_config(configuration())
        for version in (None, "not-yet-collected", "catalog-dataset-hash"):
            self.assertTrue(policy.allows("TEST_SOURCE", "external_process", "buyer", version))
        for actor in (None, "seller", "*", [], {"role": "manager"}):
            self.assertFalse(policy.allows("TEST_SOURCE", "external_process", actor, "new-version"))
        for action in ("fetch", "store_body", "local_embed", "display_excerpt", "public_redistribute"):
            self.assertFalse(policy.allows("TEST_SOURCE", action, "buyer", "new-version"))

    def test_exact_version_block_or_actor_scope_never_falls_back(self):
        policy = SourcePolicy.from_config(configuration(record(),
            record(version_scope="VERSION", version_id="blocked", status="BLOCKED"),
            record(version_scope="VERSION", version_id="manager-only", allowed_actors=["manager"])))
        self.assertFalse(policy.allows("TEST_SOURCE", "external_process", "buyer", "blocked"))
        self.assertFalse(policy.allows("TEST_SOURCE", "external_process", "buyer", "manager-only"))
        self.assertTrue(policy.allows("TEST_SOURCE", "external_process", "manager", "manager-only"))
        self.assertTrue(policy.allows("TEST_SOURCE", "external_process", "buyer", "another-version"))

    def test_version_scope_does_not_approve_missing_or_foreign_versions(self):
        policy = SourcePolicy.from_config(configuration(record(version_scope="VERSION", version_id="version-a")))
        self.assertTrue(policy.allows("TEST_SOURCE", "external_process", "buyer", "version-a"))
        for version in (None, "version-b", ["version-a"]):
            self.assertFalse(policy.allows("TEST_SOURCE", "external_process", "buyer", version))

    def test_private_schema_rejects_ambiguous_or_broader_approvals(self):
        mutations = [lambda v: v.update(schema_version=True),
                     lambda v: v.update(network_enabled=True),
                     lambda v: v["reviews"][0].pop("allowed_actors"),
                     lambda v: v["reviews"][0].update(allowed_actors=["*"]),
                     lambda v: v["reviews"][0].update(allowed_actors=["buyer", "buyer"]),
                     lambda v: v["reviews"][0].update(version_scope="VERSION"),
                     lambda v: v["reviews"][0].update(version_id="v1"),
                     lambda v: v["reviews"][0].update(version_scope="VERSION", version_id="v1", document_version_id="v2"),
                     lambda v: v["reviews"].append(copy.deepcopy(v["reviews"][0])),
                     lambda v: v["registry"].append(copy.deepcopy(v["registry"][0])),
                     lambda v: v["registry"][0].update(remote_endpoint="https://evil.example/"),
                     lambda v: v["registry"][0].update(canonical_url="https://example.org/other"),
                     lambda v: v["registry"][0].update(is_synthetic="true"),
                     lambda v: v["registry"][0].update(allowed_hosts="example.org"),
                     lambda v: v["capabilities"].update(buyer=["ADMIN"]),
                     lambda v: v["reviews"][0].update(reviewed_on="20261022")]
        for index, mutate in enumerate(mutations):
            value = configuration()
            mutate(value)
            with self.subTest(index=index):
                self.assert_code("CONFIG_INVALID", lambda: SourcePolicy.from_config(value))

    def test_configuration_does_not_enable_network_or_a_remote_model(self):
        policy = SourcePolicy.from_config(configuration(record("fetch"), record()))
        transport_calls = []
        fetcher = SafeFetcher(policy, transport=lambda url: transport_calls.append(url), resolver=public_dns)
        self.assertTrue(policy.allows("TEST_SOURCE", "external_process", "buyer"))
        self.assert_code("NETWORK_DISABLED", lambda: fetcher.fetch("TEST_SOURCE", SOURCE["url"], "buyer"))
        self.assertEqual([], transport_calls)
        self.assertFalse(policy.network_enabled)

    def test_missing_private_source_policy_keeps_runtime_network_and_actions_blocked(self):
        with tempfile.TemporaryDirectory(prefix="warranty-policy-deny-") as directory:
            policy = load_source_policy(SimpleNamespace(config_dir=Path(directory)),
                                        {"collection": {"network_enabled": True}})
        self.assertFalse(policy.network_enabled)
        self.assertFalse(policy.allows("TEST_SOURCE", "external_process", "buyer", "version"))
        self.assertFalse(policy.allows("TEST_SOURCE", "fetch", "manager"))
        self.assert_code("FORBIDDEN", lambda: policy.require_capability("admin"))

    def test_config_and_returned_records_cannot_mutate_authority(self):
        value = configuration()
        policy = SourcePolicy.from_config(value)
        value["reviews"][0]["allowed_actors"].append("seller")
        returned = policy.require("TEST_SOURCE", "external_process", "buyer")
        returned["allowed_actors"].append("seller")
        returned_source = policy.source("TEST_SOURCE")
        returned_source["approved_urls"] += ("https://example.org/unregistered",)
        self.assertFalse(policy.allows("TEST_SOURCE", "external_process", "seller"))
        self.assertEqual((SOURCE["url"],), policy.source("TEST_SOURCE")["approved_urls"])

    def test_only_data_manager_can_mutate_and_every_change_advances_fence(self):
        policy = SourcePolicy.from_config(configuration())
        self.assertEqual(0, policy.generation)
        self.assert_code("FORBIDDEN", lambda: policy.set_review("buyer", record(status="BLOCKED")))
        self.assert_code("FORBIDDEN", lambda: policy.register_source({"role": "manager"}, SOURCE))
        self.assertEqual(0, policy.generation)
        policy.set_review("manager", record(status="BLOCKED"))
        self.assertEqual(1, policy.generation)
        self.assertFalse(policy.allows("TEST_SOURCE", "external_process", "buyer"))
        with self.assertRaises(ValueError):
            policy.set_review("manager", {**record(), "allowed_actors": ["*"]})
        self.assertEqual(1, policy.generation)
        policy.register_source("manager", SOURCE)
        self.assertEqual(2, policy.generation)
        self.assertFalse(policy.allows("TEST_SOURCE", "external_process", "buyer"))

    def test_read_guard_fences_revocation_until_local_transaction_exits(self):
        policy = SourcePolicy.from_config(configuration())
        started, completed = threading.Event(), threading.Event()
        errors = []
        def revoke():
            started.set()
            try:
                policy.set_review("manager", record(status="BLOCKED"))
            except BaseException as exc:
                errors.append(exc)
            finally:
                completed.set()
        with policy.read_guard() as generation:
            worker = threading.Thread(target=revoke, daemon=True)
            worker.start()
            self.assertTrue(started.wait(1))
            self.assertFalse(completed.wait(0.05))
            self.assertEqual(generation, policy.generation)
            self.assertTrue(policy.allows("TEST_SOURCE", "external_process", "buyer"))
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual([], errors)
        self.assertTrue(completed.is_set())
        self.assertEqual(generation + 1, policy.generation)
        self.assertFalse(policy.allows("TEST_SOURCE", "external_process", "buyer"))

    def test_fact_only_record_still_cannot_authorize_body_egress(self):
        policy = SourcePolicy.from_config(configuration(record(basis_type="FACT_ONLY")))
        self.assertFalse(policy.allows("TEST_SOURCE", "external_process", "buyer"))

    def test_revocation_during_backoff_blocks_the_next_request(self):
        policy = SourcePolicy.from_config(configuration(record("fetch")), network_enabled=True)
        calls = []
        def transport(validated):
            calls.append(validated)
            return 503, {}, b""
        def revoke_during_backoff(delay):
            policy.set_review("manager", record("fetch", status="BLOCKED"))
        fetcher = SafeFetcher(policy, transport=transport, resolver=public_dns,
                              sleeper=revoke_during_backoff)
        self.assert_code("SOURCE_BLOCKED", lambda: fetcher.fetch("TEST_SOURCE", SOURCE["url"], "buyer"))
        self.assertEqual(1, len(calls))

    def test_response_is_discarded_when_permission_is_revoked_in_flight(self):
        policy = SourcePolicy.from_config(configuration(record("fetch")), network_enabled=True)
        calls = []
        def transport(validated):
            calls.append(validated)
            policy.set_review("manager", record("fetch", status="BLOCKED"))
            return 200, {"content-type": "text/html"}, b"<p>Synthetic fixture only</p>"
        fetcher = SafeFetcher(policy, transport=transport, resolver=public_dns)
        self.assert_code("SOURCE_BLOCKED", lambda: fetcher.fetch("TEST_SOURCE", SOURCE["url"], "buyer"))
        self.assertEqual(1, len(calls))

    def test_metadata_egress_binds_every_text_to_its_approved_source(self):
        items = [{"item_id": "A", "source_id": "TEST_SOURCE", "index_text": "SYNTHETIC approved facts"},
                 {"item_id": "B", "source_id": "TEST_DENIED", "index_text": "SYNTHETIC denied source facts"}]
        catalog = SimpleNamespace(items=items, dataset_hash="synthetic-dataset",
                                  get=lambda item_id: next(row for row in items if row["item_id"] == item_id))
        value = configuration()
        value["registry"].append({"source_id": "TEST_DENIED", "url": "https://example.org/denied"})
        policy = SourcePolicy.from_config(value)
        authorization = ProcessingAuthorization(SimpleNamespace(remote_models=True),
            {"models": {"external_processing_approved": True, "allow_question_processing": True}}, catalog, policy)
        contexts = [{"kind": "metadata", "source_id": "TEST_SOURCE", "version_id": catalog.dataset_hash,
                     "actor": "buyer"}]
        provider_calls = []
        def embed(**kwargs):
            provider_calls.append(kwargs)
            return SimpleNamespace(model="synthetic-embedding", data=[
                SimpleNamespace(index=index, embedding=[0.1, 0.2]) for index in range(len(kwargs["input"]))])
        adapter = OpenAIEmbeddings(SimpleNamespace(remote_models=True), authorization.embedding_guard,
            env={"OPENAI_API_KEY": "offline-test-placeholder", "EMBEDDING_MODEL": "synthetic-embedding",
                 "EMBEDDING_DIMENSION": "2"}, client=SimpleNamespace(embeddings=SimpleNamespace(create=embed)))
        self.assertTrue(authorization.embedding_guard([items[0]["index_text"]], contexts))
        with adapter.processing_context(contexts):
            self.assertEqual([[0.1, 0.2]], adapter.embed([items[0]["index_text"]]))
        self.assertFalse(authorization.embedding_guard([items[1]["index_text"]], contexts))
        with adapter.processing_context(contexts):
            self.assert_code("MODEL_REMOTE_BLOCKED", lambda: adapter.embed([items[1]["index_text"]]))
        self.assertEqual(1, len(provider_calls))
        self.assertFalse(authorization.embedding_guard([items[0]["index_text"], items[1]["index_text"]], contexts))
        self.assertFalse(authorization.embedding_guard([items[0]["index_text"]],
                         [{**contexts[0], "actor": "seller"}]))
        policy.set_review("manager", record(version_scope="VERSION", version_id=catalog.dataset_hash, status="BLOCKED"))
        self.assertFalse(authorization.embedding_guard([items[0]["index_text"]], contexts))

    def test_document_egress_requires_actual_stored_vector_and_body_provenance(self):
        from test_rag import RagTests, INPUTS
        fixture = RagTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.document()
        prepared = fixture.rag.prepare("buyer", fixture.item, INPUTS)
        self.assertEqual("COMPLETED", prepared["status"])
        evidence = fixture.rag.vectors.search(fixture.item, prepared["revision"], "Transfer", top_k=1)[0]
        evidence["number"] = 2
        authorization = ProcessingAuthorization(SimpleNamespace(remote_models=True),
            {"models": {"external_processing_approved": True, "allow_question_processing": True}},
            fixture.catalog, fixture.policy)
        authorization.registry = fixture.rag.registry
        authorization.vectors = fixture.rag.vectors
        with authorization.scope("buyer", fixture.item):
            self.assertTrue(authorization.model_guard("answer", "evidence", evidence))
            for changes in ({"text": evidence["text"][:10]}, {"evidence_id": "invented-evidence"},
                            {"revision": "invented-revision"}, {"item_id": fixture.other},
                            {"source_type": "official_document"}, {"section": "Invented section"},
                            {"document_id": "invented-document"}, {"body_path": "private/synthetic.json"}):
                with self.subTest(changes=sorted(changes)):
                    self.assertFalse(authorization.model_guard("answer", "evidence", {**evidence, **changes}))
            source_review = fixture.policy.require("SYNTHETIC-DOC", "external_process", "buyer", evidence["version_id"])
            fixture.policy.set_review("manager", {**source_review, "version_id": evidence["version_id"], "status": "BLOCKED"})
            self.assertFalse(authorization.model_guard("answer", "evidence", evidence))

    def test_factory_resumes_job_without_unapproved_model_calls_and_closes_worker_first(self):
        with tempfile.TemporaryDirectory(prefix="warranty-runtime-security-") as directory:
            settings = Settings(Path(directory) / "data", Path(directory) / "config", remote_models=True)
            settings.prepare()
            runtime_path = settings.config_dir / "runtime.json"
            runtime_path.write_text(json.dumps({"collection": {"enabled": True, "manager_actor": "manager",
                                                                 "worker_enabled": False}}), encoding="utf-8")
            policy_config = configuration(record(source_id="SYNTHETIC-SOURCE"))
            policy_config["registry"] = [{"source_id": "SYNTHETIC-SOURCE", "url": "https://example.invalid/synthetic"}]
            (settings.config_dir / "source_policy.json").write_text(json.dumps(policy_config), encoding="utf-8")
            prompts = settings.config_dir / "prompts"
            prompts.mkdir()
            for kind, inputs in {"query": ["item", "question", "applicability"],
                                 "selection": ["item", "question", "applicability", "candidates"],
                                 "answer": ["item", "question", "applicability", "evidence", "missing_information", "conflicts"]}.items():
                policy = {"version": "1.0.0", "kind": kind, "role": "Synthetic fixture", "template": "Synthetic fixture",
                          "input_variables": inputs, "output_schema": output_schema(kind),
                          **{key: ["Synthetic fixture"] for key in ("evidence_limits", "missing_information_rules",
                                                                   "conflict_rules", "attack_rules")}}
                (prompts / (kind + ".json")).write_text(json.dumps(policy), encoding="utf-8")
            environment = {"OPENAI_API_KEY": "offline-test-placeholder", "CHAT_MODEL": "synthetic-chat",
                           "EMBEDDING_MODEL": "synthetic-embedding", "EMBEDDING_DIMENSION": "2"}
            with patch.dict(os.environ, environment), patch("warranty.prompts._official_sdk_client") as chat_client, \
                    patch("warranty.embeddings._official_sdk_client") as embed_client:
                os.environ.pop("METADATA_PATH", None)
                os.environ.pop("METADATA_EXPECTED_HASH", None)
                first = build_application(settings)
                try:
                    item_id = first.catalog.items[0]["item_id"]
                    queued = first.rag.enqueue_prepare("buyer", item_id, question="Synthetic preparation question")
                    self.assertEqual("QUEUED", queued["status"])
                    self.assertFalse(first.document_jobs.worker_running)
                finally:
                    self.assertTrue(first.close())
                runtime_path.write_text(json.dumps({"models": {"chat_provider": "openai", "embedding_provider": "openai",
                    "embedding_dimension": 2, "external_processing_approved": False, "allow_question_processing": False},
                    "collection": {"enabled": True, "manager_actor": "manager", "worker_enabled": True}}), encoding="utf-8")
                app = build_application(settings)
                try:
                    self.assertIs(app.authorization.registry, app.rag.registry)
                    self.assertIs(app.authorization.vectors, app.rag.vectors)
                    self.assertIs(app.authorization.policy, app.rag.registry.policy)
                    self.assertIs(app.trades.review_guard.__self__, app.rag)
                    deadline = time.monotonic() + 3
                    job = app.rag.job("buyer", queued["job_id"])
                    while job["status"] in {"QUEUED", "RUNNING"} and time.monotonic() < deadline:
                        time.sleep(0.01)
                        job = app.rag.job("buyer", queued["job_id"])
                    self.assertEqual("BLOCKED", job["status"])
                    self.assertEqual("MODEL_REMOTE_BLOCKED", job["failure_code"])
                    self.assertFalse(app.runtime_status()["chat"]["ready"])
                    self.assertFalse(app.runtime_status()["embedding"]["ready"])
                    chat_client.assert_not_called()
                    embed_client.assert_not_called()
                    order = []
                    for owner, method, label in ((app.document_jobs, "stop", "jobs"), (app.rag, "close", "rag"),
                                                  (app.db, "close", "db")):
                        original = getattr(owner, method)
                        def counted(*args, _original=original, _label=label, **kwargs):
                            order.append(_label)
                            return _original(*args, **kwargs)
                        self.enterContext(patch.object(owner, method, side_effect=counted))
                    self.assertTrue(app.close())
                    self.assertEqual(["jobs", "rag", "db"], order)
                    self.assertFalse(app.document_jobs.worker_running)
                finally:
                    app.close()


if __name__ == "__main__":
    unittest.main()
