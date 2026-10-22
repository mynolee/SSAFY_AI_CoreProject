"""Read-only prepared-metadata checks on actual local Chroma, no model calls."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from warranty.vectorstore import LocalVectorStore, MockEmbeddings


class CountingEmbeddings(MockEmbeddings):
    name = "MOCK-metadata-readonly-fixture"

    def __init__(self):
        self.calls = 0
        self.deny = False

    def embed(self, texts):
        if self.deny:
            raise AssertionError("A read-only prepared-index check must never embed")
        self.calls += 1
        return super().embed(texts)


class MetadataReadinessTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="warranty-vector-readonly-")
        self.addCleanup(directory.cleanup)
        self.embedding = CountingEmbeddings()
        self.store = LocalVectorStore(Path(directory.name) / "chroma", self.embedding)
        self.addCleanup(self.store.close)
        self.records = [{"item_id": "SYNTHETIC-ALPHA", "text": "SYNTHETIC alpha monthly_fee=null; transferability=unknown"},
                        {"item_id": "SYNTHETIC-BETA", "text": "SYNTHETIC beta " + "보존할 사실 " * 250 + "; final_value=unknown"}]
        self.dataset_hash = "SYNTHETIC-dataset-revision-A"

    def prepared(self):
        self.assertEqual(self.store.index_metadata(self.records, dataset_hash=self.dataset_hash), 2)
        self.assertEqual(self.embedding.calls, 1)
        self.embedding.deny = True

    def test_matching_catalog_is_read_only_and_never_embeds(self):
        self.prepared()
        before = self.store.meta.get(include=["documents", "metadatas"])
        with patch.object(self.store.meta, "upsert", side_effect=AssertionError("Unexpected mutation")), \
             patch.object(self.store.meta, "delete", side_effect=AssertionError("Unexpected mutation")), \
             patch.object(self.store.meta, "update", side_effect=AssertionError("Unexpected mutation")):
            for _ in range(3):
                self.assertTrue(self.store.metadata_matches(self.records, self.dataset_hash))
        self.assertEqual(self.embedding.calls, 1)
        self.assertEqual(self.store.meta.get(include=["documents", "metadatas"]), before)

    def test_stale_dataset_hash_does_not_claim_ready(self):
        self.prepared()
        self.assertFalse(self.store.metadata_matches(self.records, "SYNTHETIC-dataset-revision-B"))
        self.assertEqual(self.embedding.calls, 1)

    def test_full_text_must_match_including_final_character_and_unknown_values(self):
        self.prepared()
        changed = deepcopy(self.records)
        changed[1]["text"] = changed[1]["text"][:-1] + "!"
        self.assertFalse(self.store.metadata_matches(changed, self.dataset_hash))
        changed = deepcopy(self.records)
        changed[0]["text"] = changed[0]["text"].replace("transferability=unknown", "transferability=confirmed")
        self.assertFalse(self.store.metadata_matches(changed, self.dataset_hash))
        self.assertEqual(self.embedding.calls, 1)

    def test_duplicate_requested_identity_is_not_ready(self):
        self.prepared()
        requested = [self.records[0], {**self.records[1], "item_id": self.records[0]["item_id"]}]
        self.assertFalse(self.store.metadata_matches(requested, self.dataset_hash))

    def test_equal_count_with_foreign_identity_is_not_ready(self):
        self.prepared()
        requested = [self.records[0], {**self.records[1], "item_id": "SYNTHETIC-FOREIGN"}]
        self.assertFalse(self.store.metadata_matches(requested, self.dataset_hash))
        self.assertEqual(self.embedding.calls, 1)

    def test_missing_or_additional_catalog_row_is_not_ready(self):
        self.prepared()
        self.assertFalse(self.store.metadata_matches(self.records[:1], self.dataset_hash))
        self.assertFalse(self.store.metadata_matches(self.records + [{"item_id": "SYNTHETIC-EXTRA", "text": "SYNTHETIC extra"}], self.dataset_hash))
        self.assertEqual(self.embedding.calls, 1)

    def test_missing_hash_tag_is_not_ready_for_hash_bound_runtime(self):
        self.store.index_metadata(self.records)
        self.embedding.deny = True
        self.assertFalse(self.store.metadata_matches(self.records, self.dataset_hash))
        self.assertEqual(self.embedding.calls, 1)

    def test_prepared_hash_bound_catalog_survives_reopen_without_embedding(self):
        self.prepared()
        reopened_embedding = CountingEmbeddings()
        reopened_embedding.deny = True
        reopened = LocalVectorStore(self.store.path, reopened_embedding)
        self.addCleanup(reopened.close)
        self.assertTrue(reopened.metadata_matches(self.records, self.dataset_hash))
        self.assertEqual(reopened_embedding.calls, 0)


if __name__ == "__main__":
    unittest.main()
