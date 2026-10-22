"""Selected metadata preparation with real local Chroma and offline adapters."""
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_rag as fixtures
from warranty.catalog import Catalog
from warranty.config import Settings
from warranty.db import Database
from warranty.documents import digest
from warranty.errors import ServiceError
from warranty.rag import RagService
from warranty.vectorstore import MockEmbeddings


class ScopedEmbeddingSpy(MockEmbeddings):
    """A declared remote-location spy whose only implementation is local MOCK."""
    name = "MOCK-selected-metadata-scope-spy"
    is_remote = True

    def __init__(self):
        self.inputs, self.contexts, self.active = [], [], None

    @contextmanager
    def processing_context(self, contexts):
        previous, self.active = self.active, deepcopy(contexts)
        try:
            yield
        finally:
            self.active = previous

    def embed(self, texts):
        if not self.active:
            raise AssertionError("Trusted processing scope is required")
        self.inputs.append(deepcopy(texts))
        self.contexts.append(deepcopy(self.active))
        return super().embed(texts)


def authored_catalog():
    base = Catalog.synthetic()
    payload = base.metadata
    items = []
    for index in range(1, 105):
        item = base.items[0]
        item.update(item_id=f"SYNTHETIC-{index:03d}", name=f"SYNTHETIC item {index}",
                    index_text=f"SYNTHETIC item {index}; unknown facts remain null")
        if index > 2:
            item.update(source_id="SYNTHETIC-UNSELECTED-SOURCE", source_url="https://example.invalid/unselected")
        items.append(item)
    first = next(iter(base.sources.values()))
    other = {**first, "source_id": "SYNTHETIC-UNSELECTED-SOURCE", "url": "https://example.invalid/unselected",
             "metadata_item_count": 102}
    payload.update(item_count=104, counts_by_provider={"SYNTHETIC": 104}, items=items, sources=[first, other])
    return Catalog(payload, digest(payload))


class LiveMetadataScopeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="warranty-metadata-scope-")
        self.addCleanup(temporary.cleanup)
        for target in ("socket.create_connection", "socket.socket.connect"):
            blocker = patch(target, side_effect=AssertionError("Network forbidden"))
            blocker.start()
            self.addCleanup(blocker.stop)
        self.settings = Settings(Path(temporary.name) / "data", Path(temporary.name) / "config", remote_models=True)
        self.settings.prepare()
        self.catalog = authored_catalog()
        self.db = Database(self.settings.data_dir / "app.sqlite3")
        self.db.initialize()
        self.policy = fixtures.policy()
        metadata_source = self.catalog.sources["SYNTHETIC-SOURCE"]
        self.policy.register_source("manager", metadata_source)
        self.policy.set_review("manager", {**self.policy.require("SYNTHETIC-DOC", "external_process"),
                                           "source_id": metadata_source["source_id"]})
        self.embedding = ScopedEmbeddingSpy()
        self.rag = self.service(self.embedding)
        self.item, self.other = "SYNTHETIC-001", "SYNTHETIC-002"

    def service(self, embedding):
        rag = RagService(self.catalog, self.db, self.settings, model=fixtures.ExtractiveModel(),
            prompt_store=fixtures.SyntheticPromptStore(), source_policy=self.policy,
            index_actor="manager", embeddings=embedding)
        self.addCleanup(rag.close)
        return rag

    def document(self):
        record = self.rag.registry.register_document("manager", source_id="SYNTHETIC-DOC", title="SYNTHETIC terms",
            canonical_url="https://example.org/synthetic-terms", document_type="HTML", is_synthetic=True)
        version = self.rag.registry.ingest_bytes("manager", record["document_id"], fixtures.BODY, applicability=fixtures.SCOPE)
        for item_id in (self.item, self.other):
            self.rag.registry.link("manager", item_id, version["version_id"], role="SERVICE_TERMS",
                applicability_basis="Authored shared scope for offline tests only.")
        return version

    def test_boot_and_status_are_read_only_with_zero_embedding_calls(self):
        self.assertEqual(self.embedding.inputs, [])
        self.assertEqual(self.rag.metadata_count, 0)
        state = self.rag.corpus_summary(item_ids=[self.item])
        self.assertFalse(state["metadata_index_matches"])
        self.assertFalse(state["metadata_complete"])
        self.assertIsNone(state["active_index"])
        self.assertEqual(self.embedding.inputs, [])

    def test_only_selected_text_and_source_are_sent_and_catalog_is_unchanged(self):
        before = self.catalog.items
        result = self.rag.prepare_metadata("manager", item_ids=[self.item])
        self.assertEqual(self.embedding.inputs, [[self.catalog.get(self.item)["index_text"]]])
        self.assertEqual(self.embedding.contexts, [[{"kind": "metadata", "source_id": "SYNTHETIC-SOURCE",
            "version_id": self.catalog.dataset_hash, "actor": "manager"}]])
        self.assertEqual(self.catalog.items, before)
        self.assertEqual(len(before), 104)
        self.assertEqual(result["metadata_count"], 1)
        self.assertFalse(result["metadata_complete"])
        self.assertTrue(result["metadata_index_matches"])
        self.assertEqual(result["item_ids"], [self.item])
        self.assertEqual(self.rag.vectors.meta.get(include=[])["ids"], [self.item])

    def test_subset_merge_keeps_prepared_rows_and_hash_bound_case_readiness(self):
        self.rag.prepare_metadata("manager", item_ids=[self.item])
        first = self.rag.vectors.meta.get(ids=[self.item], include=["documents", "metadatas", "embeddings"])
        self.rag.prepare_metadata("manager", item_ids=[self.other])
        after = self.rag.vectors.meta.get(ids=[self.item], include=["documents", "metadatas", "embeddings"])
        self.assertEqual(first["documents"], after["documents"])
        self.assertEqual(first["metadatas"], after["metadatas"])
        self.assertEqual(first["embeddings"].tolist(), after["embeddings"].tolist())
        state = self.rag.corpus_summary(item_ids=[self.item, self.other])
        self.assertEqual(state["metadata_count"], 2)
        self.assertTrue(state["metadata_index_matches"])
        self.assertFalse(state["metadata_complete"])
        self.assertFalse(self.rag.corpus_summary()["metadata_index_matches"])
        self.assertFalse(self.rag.corpus_summary(item_ids=["SYNTHETIC-003"])["metadata_index_matches"])
        self.assertEqual([len(batch) for batch in self.embedding.inputs], [1, 1])

    def test_repeated_and_reopened_preparation_reuses_validated_rows_without_embedding(self):
        self.rag.prepare_metadata("manager", item_ids=[self.item])
        self.rag.prepare_metadata("manager", item_ids=[self.item])
        self.assertEqual(len(self.embedding.inputs), 1)
        reopened_embedding = ScopedEmbeddingSpy()
        reopened = self.service(reopened_embedding)
        self.assertEqual(reopened.metadata_count, 1)
        self.assertFalse(reopened.metadata_complete)
        self.assertTrue(reopened.corpus_summary(item_ids=[self.item])["metadata_index_matches"])
        reopened.prepare_metadata("manager", item_ids=[self.item])
        self.assertEqual(reopened_embedding.inputs, [])

    def test_malformed_duplicate_or_unknown_selections_fail_before_embedding(self):
        for invalid in (None, [], self.item, (self.item,), [True], [{}],
                        [self.item, self.item], [self.item, "SYNTHETIC-FOREIGN"]):
            with self.subTest(selection=invalid), self.assertRaises(ServiceError):
                self.rag.prepare_metadata("manager", item_ids=invalid)
        self.assertEqual(self.embedding.inputs, [])
        self.assertEqual(self.rag.vectors.meta.count(), 0)

    def test_actor_and_every_selected_source_are_checked_before_any_embedding(self):
        with self.assertRaises(ServiceError):
            self.rag.prepare_metadata("buyer", item_ids=[self.item])
        with self.assertRaises(ServiceError) as blocked:
            self.rag.prepare_metadata("manager", item_ids=[self.item, "SYNTHETIC-003"])
        self.assertEqual(blocked.exception.code, "SOURCE_BLOCKED")
        self.assertEqual(self.embedding.inputs, [])
        self.assertEqual(self.rag.vectors.meta.count(), 0)

    def test_revoked_source_does_not_reuse_prepared_row_as_permission(self):
        self.rag.prepare_metadata("manager", item_ids=[self.item])
        review = self.policy.require("SYNTHETIC-SOURCE", "external_process", "manager", self.catalog.dataset_hash)
        self.policy.set_review("manager", {**review, "status": "BLOCKED"})
        with self.assertRaises(ServiceError):
            self.rag.prepare_metadata("manager", item_ids=[self.item])
        self.assertEqual(len(self.embedding.inputs), 1)

    def test_stale_hash_and_modified_text_are_not_counted_as_prepared(self):
        self.rag.prepare_metadata("manager", item_ids=[self.item, self.other])
        self.rag.vectors.meta.update(ids=[self.item], documents=["SYNTHETIC changed metadata"],
                                    embeddings=[[1.0] + [0.0] * (self.embedding.dimension - 1)])
        self.rag.vectors.meta.update(ids=[self.other], metadatas=[{"dataset_hash": "SYNTHETIC-stale",
            "item_id": self.other, "source_type": "metadata"}])
        before = len(self.embedding.inputs)
        state = self.rag.corpus_summary(item_ids=[self.item, self.other])
        self.assertEqual(state["metadata_count"], 0)
        self.assertFalse(state["metadata_index_matches"])
        self.assertEqual(len(self.embedding.inputs), before)
        self.rag.prepare_metadata("manager", item_ids=[self.item])
        self.assertEqual(self.rag.metadata_count, 1)
        self.assertIn(self.other, self.rag.vectors.meta.get(include=[])["ids"])

    def test_document_index_uses_prepared_items_and_exposes_actual_manifest(self):
        self.document()
        self.rag.prepare_metadata("manager", item_ids=[self.item])
        result = self.rag.prepare("buyer", self.item)
        self.assertEqual(result["status"], "COMPLETED")
        rows = self.rag.vectors.docs.get(where={"revision": result["revision"]}, include=["metadatas"])
        self.assertEqual({row["item_id"] for row in rows["metadatas"]}, {self.item})
        state = self.rag.corpus_summary(item_ids=[self.item])
        self.assertTrue(state["metadata_index_matches"])
        self.assertEqual(state["active_index"], {"revision": result["revision"],
            "embedding_model": self.embedding.name, "dimension": self.embedding.dimension,
            "dataset_hash": self.catalog.dataset_hash})
        calls = len(self.embedding.inputs)
        blocked = self.rag.prepare("buyer", self.other)
        self.assertEqual((blocked["status"], blocked["failure_code"]), ("BLOCKED", "INDEX_EMPTY"))
        self.assertEqual(len(self.embedding.inputs), calls)

    def test_incompatible_active_manifest_is_not_reported_as_ready(self):
        self.document()
        self.rag.prepare_metadata("manager", item_ids=[self.item])
        self.rag.prepare("buyer", self.item)
        before = len(self.embedding.inputs)
        with self.db.transaction() as connection:
            connection.execute("UPDATE index_manifests SET dimension=dimension+1 WHERE active=1")
        state = self.rag.corpus_summary(item_ids=[self.item])
        self.assertIsNone(state["active_index"])
        self.assertIsNone(state["revision"])
        self.assertEqual(len(self.embedding.inputs), before)

    def test_local_mock_no_argument_still_prepares_the_complete_catalog(self):
        local = self.service(MockEmbeddings())
        result = local.prepare_metadata("manager")
        self.assertEqual(result["metadata_count"], 104)
        self.assertTrue(result["metadata_complete"])
        self.assertTrue(result["metadata_index_matches"])

    def test_vector_merge_validates_inputs_before_adapter_and_preserves_other_rows(self):
        store = self.rag.vectors
        with self.rag._embedding_context([{"kind": "metadata", "source_id": "SYNTHETIC-SOURCE",
                "version_id": self.catalog.dataset_hash, "actor": "manager"}]):
            for records in ([{"item_id": self.item, "text": None}], [{"item_id": 1, "text": "SYNTHETIC"}],
                            [{"item_id": self.item, "text": "SYNTHETIC"}] * 2):
                with self.subTest(records=records), self.assertRaises(ServiceError):
                    store.index_metadata(records, self.catalog.dataset_hash, replace=False)
        self.assertEqual(self.embedding.inputs, [])
        self.rag.prepare_metadata("manager", item_ids=[self.item, self.other])
        selected = self.rag._metadata_selection([self.item])
        self.assertTrue(store.metadata_matches(selected, self.catalog.dataset_hash, exact=False))
        self.assertFalse(store.metadata_matches(selected, self.catalog.dataset_hash))


if __name__ == "__main__":
    unittest.main()
