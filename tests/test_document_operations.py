"""Explicit local document operations using real SQLite and synthetic HTML only."""
from contextlib import ExitStack, closing
from copy import deepcopy
import json
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from warranty.catalog import Catalog
from warranty.config import Settings
from warranty.db import Database
from warranty.document_operations import MAX_BODY_BYTES, import_documents, sync_documents
from warranty.documents import DocumentRegistry
from warranty.errors import ServiceError
from warranty.security import SourcePolicy


ACTOR = "synthetic-manager"
SCOPE = {"country": "KR", "plan_generation": "synthetic", "payment_method": "card", "channel": "direct",
         "document_type": "HTML", "date_start": "2026-01-01", "date_end": "2026-12-31"}


def review(source, action, status="ALLOWED"):
    return {"source_id": source, "action": action, "status": status, "basis_type": "LICENSE",
            "basis_url": "https://example.invalid/synthetic-license", "reviewed_on": "2026-01-01",
            "scope": "Synthetic local test", "conditions": "Synthetic local test", "user_scope": "Named synthetic manager",
            "processing_environment": "Local authored fixture only", "version_scope": "SOURCE", "allowed_actors": [ACTOR]}


class DocumentOperationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = Settings(self.root / "data", self.root / "config")
        self.settings.prepare()
        self.catalog = Catalog.synthetic()
        source = next(iter(self.catalog.sources.values()))
        self.source_id, self.url = source["source_id"], source["url"]
        self.policy = SourcePolicy.from_config({"schema_version": 1,
            "registry": [{"source_id": self.source_id, "url": self.url,
                          "approved_urls": [self.url, "https://example.invalid/second"]}],
            "reviews": [review(self.source_id, action) for action in ("view_link", "store_body", "extract")],
            "capabilities": {ACTOR: ["DATA_MANAGER"]}}, network_enabled=False)
        self.db = Database(self.settings.data_dir / "synthetic.sqlite3")
        self.db.initialize()
        self.registry = DocumentRegistry(self.catalog, self.db, self.settings, self.policy)
        self.app = SimpleNamespace(settings=self.settings, catalog=self.catalog, rag=SimpleNamespace(registry=self.registry),
                                   document_plan=[], runtime={"collection": {"network_enabled": False}})
        self.body = self.settings.data_dir / "synthetic-private-body.html"
        self.body.write_text("<html><h1>Synthetic terms</h1><p>Synthetic approved clause.</p></html>", encoding="utf-8")
        self.stack = ExitStack()
        self.network = []

        def denied(*args, **kwargs):
            self.network.append(True)
            raise AssertionError("No network during local plan operations")

        self.stack.enter_context(patch.object(socket.socket, "connect", denied))
        self.stack.enter_context(patch.object(socket, "create_connection", denied))
        self.stack.enter_context(patch.object(self.registry, "fetch", side_effect=AssertionError("Unexpected fetch")))
        self.stack.enter_context(patch.object(self.registry, "fetch_target", side_effect=AssertionError("Unexpected fetch")))

    def tearDown(self):
        self.stack.close()
        self.db.close()
        self.temp.cleanup()
        self.assertEqual([], self.network)

    def document(self, *, body=True, item=None, url=None):
        target = {"item_id": item or self.catalog.items[0]["item_id"], "role": "SERVICE_TERMS", "applicability": deepcopy(SCOPE),
                  "applicability_basis": "Synthetic reviewed applicability", "locator": "Synthetic terms",
                  "published_on": "2026-01-01", "source_version_note": "Synthetic version"}
        document = {"source_id": self.source_id, "title": "SYNTHETIC private document title",
                    "canonical_url": url or self.url, "document_type": "HTML", "is_synthetic": True, "targets": [target]}
        if body:
            document["body_path"] = str(self.body)
        return document

    def count(self, table):
        with closing(self.db.connect()) as connection:
            return connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]

    def test_sync_registers_targets_without_opening_or_importing_a_body(self):
        document = self.document()
        document["body_path"] = str(self.settings.data_dir / "does-not-exist.html")
        self.app.document_plan = [document]
        with patch("warranty.document_operations.os.open", side_effect=AssertionError("Sync must not open a body")), \
                patch.object(self.registry, "ingest_bytes", side_effect=AssertionError("Sync must not ingest")):
            result = sync_documents(self.app, ACTOR)
        self.assertEqual("COMPLETED", result["status"])
        self.assertEqual(1, result["counts"]["registered_count"])
        self.assertEqual(1, result["counts"]["target_count"])
        self.assertEqual(0, self.count("document_versions"))
        self.assertIsNone(self.registry.registered_candidates()[0]["version_id"])

    def test_sync_is_idempotent_for_the_same_private_plan(self):
        self.app.document_plan = [self.document(body=False)]
        first = sync_documents(self.app, ACTOR)
        second = sync_documents(self.app, ACTOR)
        self.assertEqual(first, second)
        self.assertEqual(1, self.count("documents"))
        self.assertEqual(1, self.count("document_targets"))

    def test_duplicate_plan_entries_share_one_document_and_version_without_inflated_counts(self):
        self.app.document_plan = [self.document(), self.document(item=self.catalog.items[1]["item_id"])]
        result = import_documents(self.app, ACTOR)
        self.assertEqual("COMPLETED", result["status"])
        self.assertEqual(2, result["counts"]["plan_entry_count"])
        self.assertEqual(1, result["counts"]["document_count"])
        self.assertEqual(1, result["counts"]["registered_count"])
        self.assertEqual(1, result["counts"]["imported_count"])
        self.assertEqual(1, result["counts"]["imported_version_count"])
        self.assertEqual(2, result["counts"]["target_count"])
        self.assertEqual(1, self.count("documents"))
        self.assertEqual(1, self.count("document_versions"))

    def test_import_links_all_equal_scope_targets_to_the_exact_same_version(self):
        document = self.document()
        second = deepcopy(document["targets"][0])
        second["item_id"] = self.catalog.items[1]["item_id"]
        document["targets"].append(second)
        self.app.document_plan = [document]
        result = import_documents(self.app, ACTOR)
        self.assertEqual("IMPORTED", result["documents"][0]["status"])
        self.assertEqual(2, result["counts"]["target_count"])
        versions = {target["version_id"] for target in self.registry.registered_candidates()}
        self.assertEqual({result["documents"][0]["version_id"]}, versions)
        self.assertEqual(1, self.count("document_versions"))
        self.assertEqual(2, self.count("item_documents"))
        encoded = json.dumps(result)
        for private in (str(self.body), self.body.name, "SYNTHETIC private document title", "Synthetic approved clause", "body_path"):
            self.assertNotIn(private, encoded)
        self.assertFalse(self.policy.allows(self.source_id, "fetch", ACTOR))
        self.assertFalse(self.policy.allows(self.source_id, "local_embed", ACTOR))
        self.assertFalse(self.policy.allows(self.source_id, "external_process", ACTOR))

    def test_import_without_body_is_explicitly_skipped_after_target_registration(self):
        self.app.document_plan = [self.document(body=False)]
        result = import_documents(self.app, ACTOR)
        self.assertEqual("SKIPPED", result["documents"][0]["status"])
        self.assertEqual(1, result["counts"]["skipped_count"])
        self.assertEqual(0, result["counts"]["imported_count"])
        self.assertEqual(0, self.count("document_versions"))

    def test_empty_plan_still_requires_current_manager_capability(self):
        result = sync_documents(self.app, ACTOR)
        self.assertEqual(0, result["counts"]["document_count"])
        for operation in (sync_documents, import_documents):
            with self.assertRaises(ServiceError) as raised:
                operation(self.app, "synthetic-outsider")
            self.assertEqual("FORBIDDEN", raised.exception.code)
        self.assertEqual(0, self.count("documents"))

    def test_all_required_source_actions_are_checked_before_any_body_read_or_registration(self):
        self.app.document_plan = [self.document()]
        for action in ("view_link", "store_body", "extract"):
            with self.subTest(action=action):
                self.policy.set_review(ACTOR, review(self.source_id, action, "BLOCKED"))
                with patch("warranty.document_operations.os.open", side_effect=AssertionError("No preflight body read")), \
                        self.assertRaises(ServiceError) as raised:
                    import_documents(self.app, ACTOR)
                self.assertEqual("SOURCE_BLOCKED", raised.exception.code)
                self.assertEqual(0, self.count("documents"))
                self.policy.set_review(ACTOR, review(self.source_id, action))

    def test_permission_failure_on_later_source_stops_the_whole_plan_before_mutation(self):
        other = "SYNTHETIC-SECOND-SOURCE"
        self.policy.register_source(ACTOR, {"source_id": other, "url": "https://example.invalid/second"})
        self.policy.set_review(ACTOR, review(other, "view_link"))
        second = self.document(url="https://example.invalid/second")
        second["source_id"] = other
        self.app.document_plan = [self.document(), second]
        with patch("warranty.document_operations.os.open", side_effect=AssertionError("No partial preflight read")), \
                self.assertRaises(ServiceError) as raised:
            import_documents(self.app, ACTOR)
        self.assertEqual("SOURCE_BLOCKED", raised.exception.code)
        self.assertEqual(0, self.count("documents"))

    def test_conflicting_target_scopes_are_rejected_before_document_registration(self):
        document = self.document()
        second = deepcopy(document["targets"][0])
        second["applicability"]["country"] = "OTHER"
        document["targets"].append(second)
        self.app.document_plan = [document]
        with self.assertRaises(ServiceError) as raised:
            import_documents(self.app, ACTOR)
        self.assertEqual("DOCUMENT_VERSION_CONFLICT", raised.exception.code)
        self.assertEqual(0, self.count("documents"))

    def test_same_body_path_cannot_register_different_immutable_version_notes(self):
        second = self.document()
        second["targets"][0]["source_version_note"] = "Different synthetic version"
        self.app.document_plan = [self.document(), second]
        with self.assertRaises(ServiceError) as raised:
            import_documents(self.app, ACTOR)
        self.assertEqual("DOCUMENT_VERSION_CONFLICT", raised.exception.code)
        self.assertEqual(0, self.count("documents"))

    def test_import_rejects_absolute_and_relative_escape_without_leaking_paths(self):
        outside = self.root / "synthetic-outside.html"
        outside.write_text("<p>Synthetic outside</p>", encoding="utf-8")
        for path in (str(outside), "../synthetic-outside.html"):
            document = self.document();document["body_path"] = path
            self.app.document_plan = [document]
            with self.subTest(path=path), self.assertRaises(ServiceError) as raised:
                import_documents(self.app, ACTOR)
            self.assertEqual("DOCUMENT_BODY_INVALID", raised.exception.code)
            self.assertNotIn("synthetic-outside", json.dumps(raised.exception.as_dict()))
        self.assertEqual(0, self.count("documents"))

    def test_resolved_symlink_escape_is_rejected(self):
        outside = self.root / "synthetic-outside.html"
        outside.write_text("<p>Synthetic outside</p>", encoding="utf-8")
        link = self.settings.data_dir / "synthetic-link.html"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("OS does not permit authored symlink fixtures")
        document = self.document();document["body_path"] = str(link)
        self.app.document_plan = [document]
        with self.assertRaises(ServiceError) as raised:
            import_documents(self.app, ACTOR)
        self.assertEqual("DOCUMENT_BODY_INVALID", raised.exception.code)
        self.assertEqual(0, self.count("documents"))

    def test_oversized_file_is_rejected_before_body_open(self):
        with self.body.open("wb") as output:
            output.truncate(MAX_BODY_BYTES + 1)
        self.app.document_plan = [self.document()]
        with patch("warranty.document_operations.os.open", side_effect=AssertionError("Oversized file must not open")):
            result = import_documents(self.app, ACTOR)
        self.assertEqual("DOCUMENT_BODY_TOO_LARGE", result["documents"][0]["error_code"])
        self.assertEqual("FAILED", result["status"])
        self.assertEqual(0, self.count("document_versions"))

    def test_missing_body_records_safe_error_and_next_document_still_imports(self):
        missing = self.document();missing["body_path"] = str(self.settings.data_dir / "private-missing.html")
        self.app.document_plan = [missing, self.document(url="https://example.invalid/second")]
        result = import_documents(self.app, ACTOR)
        self.assertEqual("PARTIAL", result["status"])
        self.assertEqual("DOCUMENT_BODY_UNAVAILABLE", result["documents"][0]["error_code"])
        self.assertEqual("IMPORTED", result["documents"][1]["status"])
        self.assertEqual(1, result["counts"]["error_count"])
        self.assertEqual(1, result["counts"]["imported_count"])
        self.assertNotIn("private-missing", json.dumps(result))

    def test_empty_parsed_body_records_failure_without_suppressing_next_document(self):
        bad = self.settings.data_dir / "synthetic-empty.html"
        bad.write_text("<html><body></body></html>", encoding="utf-8")
        first = self.document();first["body_path"] = str(bad)
        self.app.document_plan = [first, self.document(url="https://example.invalid/second")]
        result = import_documents(self.app, ACTOR)
        self.assertEqual("PARSE_FAILED", result["documents"][0]["error_code"])
        self.assertEqual("IMPORTED", result["documents"][1]["status"])

    def test_existing_immutable_version_cannot_be_reassigned_a_new_scope(self):
        self.app.document_plan = [self.document()]
        first = import_documents(self.app, ACTOR)
        original_version = first["documents"][0]["version_id"]
        self.app.document_plan[0]["targets"][0]["applicability"]["country"] = "OTHER"
        result = import_documents(self.app, ACTOR)
        self.assertEqual("DOCUMENT_VERSION_CONFLICT", result["documents"][0]["error_code"])
        self.assertEqual(1, self.count("document_versions"))
        with closing(self.db.connect()) as connection:
            row = connection.execute("SELECT applicability_json FROM document_versions WHERE version_id=?", (original_version,)).fetchone()
        self.assertEqual("KR", json.loads(row["applicability_json"])["country"])

    def test_existing_scope_conflict_is_reported_before_any_body_replacement(self):
        self.app.document_plan = [self.document()]
        first = import_documents(self.app, ACTOR)
        self.assertEqual("IMPORTED", first["documents"][0]["status"])
        self.app.document_plan[0]["targets"][0]["applicability"]["country"] = "OTHER"
        with patch("warranty.documents.os.replace", side_effect=PermissionError(13, "Synthetic injected file contention")) as replace, \
                patch("warranty.documents.tempfile.NamedTemporaryFile", side_effect=AssertionError("Conflict must not write a body")) as temporary:
            result = import_documents(self.app, ACTOR)
        self.assertEqual("DOCUMENT_VERSION_CONFLICT", result["documents"][0]["error_code"])
        self.assertEqual(0, replace.call_count)
        self.assertEqual(0, temporary.call_count)
        self.assertEqual(1, self.count("document_versions"))

    def test_equal_scope_reimport_repairs_missing_canonical_body(self):
        self.app.document_plan = [self.document()]
        first = import_documents(self.app, ACTOR)
        version_id = first["documents"][0]["version_id"]
        with closing(self.db.connect()) as connection:
            row = connection.execute("SELECT body_path FROM document_versions WHERE version_id=?", (version_id,)).fetchone()
        body_path = Path(row["body_path"])
        body_path.unlink()
        self.assertFalse(body_path.exists())
        second = import_documents(self.app, ACTOR)
        self.assertEqual("IMPORTED", second["documents"][0]["status"])
        self.assertEqual(version_id, second["documents"][0]["version_id"])
        self.assertTrue(body_path.is_file())
        self.assertEqual(1, self.count("document_versions"))

    def test_current_permission_is_checked_again_before_opening_the_body(self):
        self.app.document_plan = [self.document()]
        original = self.registry.register_target

        def revoke_after_registration(*args, **kwargs):
            result = original(*args, **kwargs)
            self.policy.set_review(ACTOR, review(self.source_id, "extract", "BLOCKED"))
            return result

        with patch.object(self.registry, "register_target", side_effect=revoke_after_registration), \
                patch("warranty.document_operations.os.open", side_effect=AssertionError("Revoked read must not open")), \
                self.assertRaises(ServiceError) as raised:
            import_documents(self.app, ACTOR)
        self.assertEqual("SOURCE_BLOCKED", raised.exception.code)
        self.assertEqual(0, self.count("document_versions"))

    def test_document_type_and_extension_must_agree(self):
        document = self.document();document["document_type"] = "PDF"
        self.app.document_plan = [document]
        with self.assertRaises(ServiceError) as raised:
            import_documents(self.app, ACTOR)
        self.assertEqual("DOCUMENT_BODY_INVALID", raised.exception.code)

    def test_unexpected_or_sensitive_service_errors_are_not_reflected(self):
        self.app.document_plan = [self.document()]
        with patch.object(self.registry, "ingest_bytes", side_effect=ServiceError("PRIVATE_UNKNOWN_CODE", "synthetic-private-exception")):
            result = import_documents(self.app, ACTOR)
        self.assertEqual("INTERNAL_ERROR", result["documents"][0]["error_code"])
        self.assertNotIn("synthetic-private-exception", json.dumps(result))
        self.assertNotIn("PRIVATE_UNKNOWN_CODE", json.dumps(result))

    def test_invalid_target_and_unknown_plan_fields_fail_before_mutation(self):
        for mutate in (lambda row: row.update(url="https://example.invalid/unregistered"),
                       lambda row: row["targets"][0].update(item_id="SYNTHETIC-MISSING"),
                       lambda row: row["targets"][0].update(applicability=[])):
            document = self.document();mutate(document);self.app.document_plan = [document]
            with self.assertRaises(ServiceError):
                sync_documents(self.app, ACTOR)
            self.assertEqual(0, self.count("documents"))


if __name__ == "__main__":
    unittest.main()
