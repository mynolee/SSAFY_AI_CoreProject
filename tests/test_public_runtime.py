"""Public content scopes and production-factory defaults with synthetic inputs."""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from warranty.config import Settings
from warranty.errors import ServiceError
from warranty.public_protocol import public_instruction_hashes, public_source_sha256
from warranty.public_runtime import public_bundle, load_public_bundle, PublicProcessingContext
from warranty.runtime import ProcessingAuthorization, build_models, load_runtime


QUESTION = "SYNTHETIC public question?"
SOURCE = {"source_id": "synthetic-source", "version_id": "synthetic-version",
          "title": "Public synthetic document", "url": "https://example.invalid/terms",
          "excerpt": "Public synthetic warranty excerpt.", "section": "Synthetic section"}


def sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def bundle_value(question=QUESTION):
    return {"schema_version": 1, "item_ids": ["SYNTHETIC-001"], "sources": [deepcopy(SOURCE)],
            "embedding_document_sha256s": [sha(SOURCE["excerpt"])],
            "protocol": {"schema_version": 1, "model": "gpt-4.1-mini", "route": "chat/completions",
                         "instruction_sha256s": public_instruction_hashes(), "question_sha256s": [sha(question)],
                         "source_sha256s": [public_source_sha256({k: SOURCE[k] for k in ("title", "url", "excerpt")})],
                         "max_request_bytes": 16384, "max_output_tokens": 128}}


class BaseAuthorization:
    def __init__(self):
        self._request = ContextVar("synthetic-public-runtime-scope", default=None)
        self.allowed = True
        self.registry = SimpleNamespace(sections=lambda *args, **kwargs: [
            {"text": SOURCE["excerpt"], "section": SOURCE["section"], "page": None}])
        self.row = {"id": "private-candidate", "source_id": SOURCE["source_id"],
                    "version_id": SOURCE["version_id"], "title": SOURCE["title"], "canonical_url": SOURCE["url"]}

    @contextmanager
    def scope(self, item="SYNTHETIC-001"):
        token = self._request.set({"actor": "synthetic-actor", "item_id": item})
        try:
            yield
        finally:
            self._request.reset(token)

    def _candidates(self, item):
        return [self.row]

    def model_guard(self, *args):
        return self.allowed

    def embedding_guard(self, *args):
        return self.allowed


class PublicRuntimeTests(unittest.TestCase):
    def test_bundle_rejects_unknown_fields_and_unselected_embedding_content(self):
        for change in (lambda v: v.update(external_processing_approved=True),
                       lambda v: v["sources"][0].update(reference_answer="PRIVATE"),
                       lambda v: v.update(embedding_document_sha256s=[sha("other text")]),
                       lambda v: v["protocol"].update(question_sha256s=["not-a-hash"])):
            value = bundle_value()
            change(value)
            with self.assertRaises(ServiceError):
                public_bundle(value)
        value = bundle_value()
        bundle = public_bundle(value)
        value["sources"][0]["excerpt"] = "changed caller copy"
        self.assertEqual(bundle.value["sources"][0]["excerpt"], SOURCE["excerpt"])

    def test_file_change_revokes_scope_before_another_request(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-public-file-") as temporary:
            root = Path(temporary)
            path = root / "public.json"
            path.write_text(json.dumps(bundle_value()), encoding="utf-8")
            bundle = load_public_bundle(SimpleNamespace(config_dir=root), "public.json")
            base = BaseAuthorization()
            context = PublicProcessingContext(bundle, base)
            with base.scope():
                self.assertTrue(context.model_guard("query", "question", {"question": QUESTION}))
                path.write_text(json.dumps(bundle_value("another fictional question")), encoding="utf-8")
                self.assertFalse(context.model_guard("query", "question", {"question": QUESTION}))
                with self.assertRaises(ServiceError):
                    context.source_resolver("query", {"question": QUESTION})

    def test_scope_exact_question_and_nonpublic_contexts_fail_closed(self):
        base = BaseAuthorization()
        context = PublicProcessingContext(public_bundle(bundle_value()), base)
        self.assertFalse(context.model_guard("query", "question", {"question": QUESTION}))
        with base.scope():
            self.assertTrue(context.model_guard("query", "question", {"question": QUESTION}))
            for question in (QUESTION + " ", "private-item-id", "another question"):
                self.assertFalse(context.model_guard("query", "question", {"question": question}))
            self.assertFalse(context.model_guard("query", "item", {"item_id": "SYNTHETIC-001"}))
            self.assertFalse(context.model_guard("answer", "evidence", {"source_type": "metadata"}))
        with base.scope("SYNTHETIC-OTHER"):
            self.assertFalse(context.model_guard("query", "question", {"question": QUESTION}))

    def test_embedding_guard_rechecks_exact_text_and_never_allows_metadata(self):
        base = BaseAuthorization()
        context = PublicProcessingContext(public_bundle(bundle_value()), base)
        question_context = [{"kind": "question", "actor": "synthetic-actor"}]
        doc_context = [{"kind": "document", "actor": "synthetic-actor", "source_id": SOURCE["source_id"],
                        "version_id": SOURCE["version_id"]}]
        self.assertTrue(context.embedding_guard([QUESTION], question_context))
        self.assertFalse(context.embedding_guard(["private-item-id"], question_context))
        self.assertTrue(context.embedding_guard([SOURCE["excerpt"]], doc_context))
        self.assertFalse(context.embedding_guard([SOURCE["excerpt"]], [{**doc_context[0], "version_id": "stale"}]))
        self.assertFalse(context.embedding_guard([SOURCE["excerpt"]], [{"kind": "metadata"}]))
        base.allowed = False
        self.assertFalse(context.embedding_guard([SOURCE["excerpt"]], doc_context))

    def test_resolver_revalidates_registry_and_omits_catalog_metadata(self):
        base = BaseAuthorization()
        context = PublicProcessingContext(public_bundle(bundle_value()), base)
        evidence = {**SOURCE, "url": SOURCE["url"], "text": SOURCE["excerpt"], "evidence_id": "private-evidence",
                    "source_type": "synthetic_document"}
        with base.scope():
            self.assertEqual(context.source_resolver("query", {"question": QUESTION}), [])
            rows = context.source_resolver("answer", {"question": QUESTION, "evidence": [
                {"source_type": "metadata", "text": "PRIVATE CATALOG"}, evidence]})
            self.assertEqual(rows, [{"local_id": "private-evidence", **{k: SOURCE[k] for k in ("title", "url", "excerpt")}}])
            self.assertEqual(len(context.source_resolver("selection", {"question": QUESTION,
                "candidates": [{"id": "private-candidate"}]})), 1)
            base.allowed = False
            with self.assertRaises(ServiceError):
                context.source_resolver("answer", {"question": QUESTION, "evidence": [evidence]})

    def test_factory_missing_public_bundle_is_blocked_without_touching_ledgers(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-public-factory-") as temporary:
            root = Path(temporary)
            settings = Settings(root / "data", root / "config", remote_models=True)
            settings.prepare()
            runtime = load_runtime(settings)
            runtime["models"].update(chat_provider="gms", embedding_provider="gms", embedding_dimension=3)
            catalog = SimpleNamespace(get=lambda item: {"item_id": item})
            auth = ProcessingAuthorization(settings, runtime, catalog, None)
            models = build_models(settings, runtime, auth, env={})
            self.assertIsNone(models.public_context)
            self.assertFalse(models.chat.metadata()["configuration_ready"])
            with self.assertRaises(ServiceError):
                models.chat.generate("query", "PRIVATE ENVELOPE")
            with self.assertRaises(ServiceError):
                models.embedding.embed(["PRIVATE CATALOG"])
            self.assertEqual(list(settings.data_dir.glob("*.sqlite3")), [])

    def test_public_title_projection_does_not_send_local_review_title(self):
        base = BaseAuthorization()
        base.row["title"] = "PRIVATE local review title"
        context = PublicProcessingContext(public_bundle(bundle_value()), base)
        checked = []

        def check_local(kind, category, entry):
            checked.append((category, entry.get("title")))
            return category == "question" or entry.get("title") == base.row["title"]

        base.model_guard = check_local
        candidate = {"id": base.row["id"], "title": base.row["title"]}
        evidence = {**SOURCE, "title": base.row["title"], "text": SOURCE["excerpt"],
                    "evidence_id": "private-evidence", "source_type": "synthetic_document"}
        with base.scope():
            for kind, field, row in (("selection", "candidates", candidate), ("answer", "evidence", evidence)):
                rows = context.source_resolver(kind, {"question": QUESTION, field: [row]})
                self.assertEqual(rows[0]["title"], SOURCE["title"])
                self.assertNotIn("PRIVATE", json.dumps(rows))
        self.assertIn(("candidate", base.row["title"]), checked)
        self.assertIn(("evidence", base.row["title"]), checked)


if __name__ == "__main__":
    unittest.main()
