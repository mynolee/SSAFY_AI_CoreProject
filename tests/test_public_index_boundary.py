"""Public index boundaries on fresh local data and fake adapters only."""
from pathlib import Path
import hashlib
import json
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from warranty.catalog import Catalog
from warranty.config import Settings
from warranty.db import Database
from warranty.documents import ACTIONS, chunk_sections, parse_document
from warranty.errors import ServiceError
from warranty.prompts import MockStructuredModel
from warranty.rag import RagService
from warranty.security import SourcePolicy
from warranty.vectorstore import LocalVectorStore, MockEmbeddings

BODY = b"<html><h1>Transfer</h1><p>Public transfer requires provider confirmation.</p><h2>Other clause</h2><p>Public second clause remains available in the local original.</p></html>"
OTHER = b"<html><h1>Supplement</h1><p>Public supplementary information alone proves no own-document scope.</p></html>"
QUESTION = "Public synthetic transfer question?"
SCOPE = {"country":"SYNTHETIC", "plan_generation":"SYNTHETIC", "payment_method":"SYNTHETIC",
         "channel":"SYNTHETIC", "document_type":"SERVICE_TERMS", "date_start":"2020-01-01", "date_end":"2030-12-31"}
INPUTS = {**{key:value for key,value in SCOPE.items() if not key.startswith("date_")}, "enrollment_date":"2025-01-01"}


def text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class FakePublicEmbeddings(MockEmbeddings):
    name = "MOCK-public-remote-boundary-32"
    dimension = 32
    is_remote = True
    public_protocol_only = True

    def __init__(self, texts):
        self.allowed = {*texts, QUESTION}
        self.approved_document_sha256s = tuple(text_hash(text) for text in texts)
        self.calls = []

    def embed(self, texts):
        if any(text not in self.allowed for text in texts):
            raise AssertionError("A private catalog text or internal identity reached the public adapter")
        self.calls.append(list(texts))
        return super().embed(texts)


class FakePublicModel(MockStructuredModel):
    is_mock = False
    public_protocol_only = True
    model_id = "MOCK-public-model"
    cache_identity = "MOCK-public-manifest-A"


class PublicPromptFixture:
    def render(self, kind, variables):
        return json.dumps({"policy":{"kind":kind, "fixture":"PUBLIC_SYNTHETIC"}, "input":variables})

    def load(self, kind):
        return {"kind":kind, "fixture":"PUBLIC_SYNTHETIC"}


class PublicIndexBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        def denied(*args, **kwargs):
            raise AssertionError("Network is forbidden in public index boundary tests")
        cls.socket_guard = patch.multiple(socket.socket, connect=denied, connect_ex=denied)
        cls.dns_guard = patch.multiple(socket, create_connection=denied, getaddrinfo=denied)
        cls.socket_guard.start(); cls.dns_guard.start()

    @classmethod
    def tearDownClass(cls):
        cls.dns_guard.stop(); cls.socket_guard.stop()

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="warranty-public-index-")
        self.addCleanup(directory.cleanup)
        self.temporary_root = Path(directory.name).resolve()

    def folder(self):
        directory = tempfile.TemporaryDirectory(prefix="case-", dir=self.temporary_root)
        path = Path(directory.name)
        self.assertTrue(path.resolve().is_relative_to(self.temporary_root))
        def cleanup():
            if not path.resolve().is_relative_to(self.temporary_root):
                raise AssertionError("Temporary cleanup escaped its fixed root")
            directory.cleanup()
        self.addCleanup(cleanup)
        return path

    def service(self, texts=None, *, data=None):
        selected = texts if texts is not None else [chunk_sections(parse_document(BODY, "HTML"))[0]["text"]]
        adapter = FakePublicEmbeddings(selected)
        folder = data or self.folder()
        settings = Settings(folder / "data", folder / "config", remote_models=True)
        settings.prepare()
        catalog = Catalog.synthetic()
        db = Database(settings.data_dir / "app.sqlite3"); db.initialize()
        sources = [*catalog.sources.values(), {"source_id":"SYNTHETIC-PUBLIC-DOC", "url":"https://example.org/public", "allowed_hosts":["example.org"]}]
        reviews = [{"source_id":source["source_id"], "action":action,
            "status":"BLOCKED" if source["source_id"] == "SYNTHETIC-SOURCE" and action == "external_process" else "ALLOWED",
            "basis_type":"OTHER_RECORDED_BASIS", "basis_url":"https://example.org/", "reviewed_on":"2026-01-01",
            "scope":"Authored public synthetic fixture", "conditions":"Offline only", "user_scope":"test", "processing_environment":"LOCAL"}
            for source in sources for action in ACTIONS]
        policy = SourcePolicy(sources, reviews, {"manager":{"DATA_MANAGER"}})
        item = catalog.items[0]["item_id"]
        rag = RagService(catalog, db, settings, model=FakePublicModel(), prompt_store=PublicPromptFixture(),
            source_policy=policy, index_actor="manager", embeddings=adapter,
            metadata_embeddings=MockEmbeddings(), document_item_ids=[item])
        self.addCleanup(rag.close)
        return rag, adapter, policy, item

    def document(self, rag, item, body=BODY, role="SERVICE_TERMS"):
        document = rag.registry.register_document("manager", source_id="SYNTHETIC-PUBLIC-DOC", title="PUBLIC SYNTHETIC terms",
            canonical_url="https://example.org/public", document_type="HTML", is_synthetic=True)
        version = rag.registry.ingest_bytes("manager", document["document_id"], body, applicability=SCOPE)
        rag.registry.link("manager", item, version["version_id"], role=role,
            applicability_basis="Authored fixture explicitly covers this synthetic item.")
        return version

    def test_104_metadata_rows_use_separate_local_identity_and_reopen(self):
        root = self.folder() / "chroma"
        remote = FakePublicEmbeddings(["Public body"])
        local = MockEmbeddings()
        store = LocalVectorStore(root, remote, metadata_embeddings=local); self.addCleanup(store.close)
        records = [{"item_id":f"INTERNAL-{index}", "text":f"PRIVATE CATALOG fixture {index}"} for index in range(104)]
        self.assertEqual(store.index_metadata(records, "SYNTHETIC-DATASET"), 104)
        self.assertEqual(remote.calls, [])
        self.assertEqual(store.meta.metadata["embedding_model"], local.name)
        self.assertNotEqual(store.meta.name, store.docs.name)
        other = FakePublicEmbeddings(["Public body"]); other.name = "MOCK-other-public-model"; other.dimension = 16
        reopened = LocalVectorStore(root, other, metadata_embeddings=MockEmbeddings()); self.addCleanup(reopened.close)
        self.assertTrue(reopened.metadata_matches(records, "SYNTHETIC-DATASET"))
        self.assertEqual(other.calls, [])
        self.assertNotEqual(reopened.docs.name, store.docs.name)

    def test_public_prepare_filters_chunks_validates_locally_and_reuses_revision(self):
        rag, adapter, _, item = self.service()
        self.document(rag, item)
        self.document(rag, rag.catalog.items[1]["item_id"], OTHER)
        self.assertEqual(rag.metadata_count, 2)
        self.assertEqual(adapter.calls, [])
        with patch.object(rag.vectors, "search", side_effect=AssertionError("Index validation must not embed an internal ID")):
            result = rag.prepare("buyer", item, INPUTS, QUESTION)
            repeated = rag.prepare("buyer", item, INPUTS, QUESTION)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(repeated["revision"], result["revision"])
        self.assertEqual(adapter.calls, [[next(iter(adapter.allowed - {QUESTION}))]])
        rows = rag.vectors.docs.get(where={"revision":result["revision"]}, include=["documents", "metadatas"])
        self.assertEqual(len(rows["ids"]), 1)
        self.assertEqual(rows["metadatas"][0]["item_id"], item)
        scope = rag.corpus_summary()["document_scope"]
        self.assertEqual(scope["mode"], "approved_chunk_hashes")
        self.assertFalse(scope["entire_registered_bodies_indexed"])
        context = rag.retrieve_evidence("buyer", item, QUESTION, INPUTS)
        self.assertEqual(context["readiness"]["status"], "INDEX_READY")
        self.assertEqual(adapter.calls[-1], [QUESTION])

    def test_unselected_item_and_supplement_only_scope_never_embed(self):
        supplement = chunk_sections(parse_document(OTHER, "HTML"))[0]["text"]
        rag, adapter, _, item = self.service([supplement])
        self.document(rag, item)
        self.document(rag, item, OTHER, role="TRANSFER_GUIDE")
        blocked = rag.prepare("buyer", item, INPUTS, QUESTION)
        self.assertEqual(blocked["failure_code"], "OWN_DOCUMENT_MISSING")
        other = rag.catalog.items[1]["item_id"]
        self.assertEqual(rag.prepare("buyer", other, INPUTS, QUESTION)["failure_code"], "SOURCE_BLOCKED")
        with self.assertRaises(ServiceError):
            rag.retrieve_evidence("buyer", other, QUESTION, INPUTS)
        self.assertEqual(adapter.calls, [])

    def test_document_external_revocation_still_precedes_remote_query(self):
        rag, adapter, policy, item = self.service()
        self.document(rag, item)
        self.assertEqual(rag.prepare("buyer", item, INPUTS, QUESTION)["status"], "COMPLETED")
        before = list(adapter.calls)
        review = policy.require("SYNTHETIC-PUBLIC-DOC", "external_process")
        policy.set_review("manager", {**review, "status":"BLOCKED"})
        with self.assertRaises(ServiceError) as blocked:
            rag.retrieve_evidence("buyer", item, QUESTION, INPUTS)
        self.assertEqual(blocked.exception.code, "SOURCE_BLOCKED")
        self.assertEqual(adapter.calls, before)

    def test_public_subset_changes_revision_identity_and_midrun_drift_stops(self):
        pieces = chunk_sections(parse_document(BODY, "HTML"))
        rag, adapter, _, item = self.service([pieces[0]["text"]])
        self.document(rag, item)
        self.assertEqual(rag.prepare("buyer", item, INPUTS, QUESTION)["status"], "COMPLETED")
        folder = rag.settings.data_dir.parent
        other, _, _, _ = self.service([pieces[1]["text"]], data=folder)
        self.assertNotEqual(rag.chunk_config_hash, other.chunk_config_hash)
        self.assertIsNone(other._active())
        adapter.approved_document_sha256s = (text_hash(pieces[1]["text"]),)
        before = list(adapter.calls)
        self.assertEqual(rag.prepare("buyer", item, INPUTS, QUESTION)["failure_code"], "SOURCE_BLOCKED")
        self.assertEqual(adapter.calls, before)

    def test_local_has_document_retains_all_four_filters(self):
        store = LocalVectorStore(self.folder() / "chroma"); self.addCleanup(store.close)
        store.stage_documents("revision-A", [{"evidence_id":"public-A", "text":"Public clause", "item_id":"item-A",
            "version_id":"version-A", "role":"SERVICE_TERMS"}])
        self.assertTrue(store.has_document("item-A", "revision-A", "version-A", "SERVICE_TERMS"))
        for values in (("item-B","revision-A","version-A","SERVICE_TERMS"), ("item-A","revision-B","version-A","SERVICE_TERMS"),
                       ("item-A","revision-A","version-B","SERVICE_TERMS"), ("item-A","revision-A","version-A","TRANSFER_GUIDE")):
            self.assertFalse(store.has_document(*values))
