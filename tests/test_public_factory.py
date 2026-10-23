"""Actual application factory and CLI regression over a temporary public scope."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from warranty.application import build_application
from warranty.catalog import Catalog
from warranty.config import Settings
from warranty.db import Database
from warranty.documents import ACTIONS, DocumentRegistry, parse_document
from warranty.public_protocol import public_instruction_hashes, public_source_sha256
from warranty.security import SourcePolicy


ROOT = Path(__file__).resolve().parents[1]
OBSERVATIONS = {}
ACTOR = "buyer"
SOURCE = "SYNTHETIC-FACTORY-PUBLIC"
URL = "https://example.invalid/public-factory-fixture"
TITLE = "Authored factory public terms"
QUESTION = "What does the fictional selected clause require?"
EXCERPT = "This fictional selected clause requires provider confirmation."
OMITTED = "OMITTED_UNAPPROVED_FACTORY_CHUNK must stay outside the selected index."
SCOPE = {"country": "SYNTHETIC-COUNTRY", "plan_generation": "SYNTHETIC-PLAN",
         "payment_method": "SYNTHETIC-MONTHLY", "channel": "SYNTHETIC-CHANNEL",
         "document_type": "SERVICE_TERMS", "date_start": "2020-01-01", "date_end": "2030-12-31"}
INPUTS = {key: value for key, value in SCOPE.items() if not key.startswith("date_")}
INPUTS["enrollment_date"] = "2025-01-01"


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def literal_environment(data, config, guard_dir, profile):
    """Supply a fresh process environment without reading or merging the host."""
    return {"SystemRoot": r"C:\Windows", "WINDIR": r"C:\Windows",
            "PATH": r"C:\Windows\System32;C:\Windows", "USERPROFILE": str(profile),
            "APPDATA": str(profile / "roaming"), "LOCALAPPDATA": str(profile / "local"),
            "TEMP": str(profile / "temp"), "TMP": str(profile / "temp"),
            "PYTHONPATH": str(guard_dir) + ";" + str(ROOT / "src"),
            "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
            "PRIVATE_DATA_DIR": str(data), "PRIVATE_CONFIG_DIR": str(config),
            "ENABLE_REMOTE_MODELS": "false", "ANONYMIZED_TELEMETRY": "false",
            "LANGSMITH_TRACING": "false", "LANGCHAIN_TRACING_V2": "false"}


class PublicFactoryTests(unittest.TestCase):
    def test_public_bundle_factory_and_real_cli_preserve_selected_mock_scope(self):
        with tempfile.TemporaryDirectory(prefix="warranty-public-factory-") as folder:
            base = Path(folder)
            settings = Settings(base / "data", base / "config")
            settings.prepare()
            payload = deepcopy(Catalog.synthetic()._payload)
            metadata = settings.data_dir / "authored_catalog.json"
            metadata.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            catalog = Catalog.load(metadata)
            selected, other = [row["item_id"] for row in catalog.items]
            registry = [{"source_id": source_id, "url": row["url"], "is_synthetic": True}
                        for source_id, row in catalog.sources.items()]
            registry.append({"source_id": SOURCE, "url": URL, "is_synthetic": True})
            reviews = [{"source_id": row["source_id"], "action": action, "status": "ALLOWED",
                        "basis_type": "OTHER_RECORDED_BASIS", "basis_url": URL,
                        "reviewed_on": "2026-01-01", "scope": "Authored temporary factory fixture",
                        "conditions": "Offline local MOCK only", "user_scope": "Fixture buyer",
                        "processing_environment": "LOCAL", "version_scope": "SOURCE", "allowed_actors": [ACTOR]}
                       for row in registry for action in ACTIONS]
            for review in reviews:
                if review["action"] == "external_process":
                    review["status"] = "BLOCKED"
            policy_value = {"schema_version": 1, "registry": registry, "reviews": reviews,
                            "capabilities": {ACTOR: ["DATA_MANAGER"]}}
            policy = SourcePolicy.from_config(policy_value, network_enabled=False)
            (settings.config_dir / "source_policy.json").write_text(json.dumps(policy_value), encoding="utf-8")
            db = Database(settings.data_dir / "warranty.sqlite3")
            db.initialize()
            with db.transaction() as connection:
                for item in catalog.items:
                    connection.execute("INSERT INTO items(item_id,name,provider) VALUES(?,?,?)",
                                       (item["item_id"], item["name"], item["provider"]))
            documents = DocumentRegistry(catalog, db, settings, policy)
            document = documents.register_document(ACTOR, source_id=SOURCE, title=TITLE,
                canonical_url=URL, document_type="HTML", is_synthetic=True)
            body = ("<html><h1>Approved</h1><p>" + EXCERPT + "</p><h1>Unselected</h1><p>" + OMITTED + "</p></html>").encode()
            version = documents.ingest_bytes(ACTOR, document["document_id"], body, applicability=SCOPE)
            for item_id in (selected, other):
                documents.link(ACTOR, item_id, version["version_id"], role="SERVICE_TERMS",
                               applicability_basis="This authored fixture explicitly covers this synthetic item.")
            db.close()
            sections = parse_document(body, "HTML")
            self.assertEqual(len(sections), 2)
            source = {"source_id": SOURCE, "version_id": version["version_id"], "title": TITLE,
                      "url": URL, "excerpt": EXCERPT, "section": sections[0]["section"]}
            bundle = {"schema_version": 1, "item_ids": [selected], "sources": [source],
                      "embedding_document_sha256s": [sha(EXCERPT)], "protocol": {
                          "schema_version": 1, "model": "gpt-4.1-mini", "route": "chat/completions",
                          "instruction_sha256s": public_instruction_hashes(), "question_sha256s": [sha(QUESTION)],
                          "source_sha256s": [public_source_sha256({"title": TITLE, "url": URL, "excerpt": EXCERPT})],
                          "max_request_bytes": 16384, "max_output_tokens": 128}}
            bundle_path = settings.config_dir / "public_bundle.json"
            bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
            runtime = {"schema_version": 1, "metadata_path": str(metadata),
                       "metadata_expected_hash": hashlib.sha256(metadata.read_bytes()).hexdigest(),
                       "models": {"chat_provider": "mock", "embedding_provider": "mock",
                                  "public_protocol_path": "public_bundle.json"},
                       "collection": {"manager_actor": ACTOR, "enabled": False,
                                      "network_enabled": False, "worker_enabled": False}}
            (settings.config_dir / "runtime.json").write_text(json.dumps(runtime), encoding="utf-8")
            fixture_hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                              for path in (metadata, bundle_path, settings.config_dir / "runtime.json",
                                           settings.config_dir / "source_policy.json")}

            # This is the production composition root. No model, factory or
            # runtime method is patched and no ambient key map is consulted.
            app = build_application(settings, start_document_worker=False, env={})
            try:
                self.assertIsInstance(app.models.document_item_ids, list)
                self.assertEqual(app.models.document_item_ids, [selected])
                self.assertEqual(app.rag.document_item_ids, (selected,))
                self.assertIs(app.rag.vectors.metadata_embeddings, app.models.metadata_embedding)
                self.assertIsNot(app.models.metadata_embedding, app.models.embedding)
                self.assertEqual(app.rag.vectors.meta.count(), 2)
                self.assertEqual(app.rag.vectors.docs.count(), 0)
                self.assertIs(app.models.embedding.public_protocol_only, True)
                self.assertEqual(app.models.embedding.approved_document_sha256s, (sha(EXCERPT),))
                public = app.runtime_status()["public_processing"]
                self.assertTrue(public["configured"])
                self.assertEqual(public["item_count"], 1)
                self.assertEqual(public["metadata_processing"], "LOCAL_ONLY")
                self.assertFalse(public["creates_transmission_authority"])
                prepared = app.rag.prepare(ACTOR, selected, INPUTS, question=QUESTION)
                self.assertEqual(prepared["status"], "COMPLETED", prepared)
                indexed = app.rag.vectors.docs.get(include=["documents", "metadatas"])
                self.assertEqual(indexed["documents"], [EXCERPT])
                self.assertEqual({row["item_id"] for row in indexed["metadatas"]}, {selected})
                self.assertEqual(app.rag.prepare(ACTOR, other, INPUTS, question=QUESTION)["status"], "BLOCKED")
                expected_index = app.rag.corpus_summary()
                self.assertEqual(expected_index["synthetic_document_chunk_count"], 1)
                self.assertEqual(expected_index["document_scope"]["mode"], "approved_chunk_hashes")
                self.assertFalse(expected_index["document_scope"]["entire_registered_bodies_indexed"])
                self.assertFalse(app.runtime_status()["live"]["enabled"])
            finally:
                self.assertTrue(app.close())

            cli_results = []
            for command in ("public-protocol", "status"):
                guard = base / ("guard_" + command)
                guard.mkdir()
                log = guard / "network_guard.log"
                startup = ("import socket\nfrom pathlib import Path\n"
                           + "_log = Path(" + repr(str(log)) + ")\n_log.write_text('STARTUP\\n', encoding='utf-8')\n"
                           + "def _deny(*args, **kwargs):\n"
                           + "    with _log.open('a', encoding='utf-8') as stream: stream.write('DENIED\\n')\n"
                           + "    raise AssertionError('Network denied before actual CLI imports')\n"
                           + "socket.create_connection = _deny\nsocket.socket.connect = _deny\n"
                           + "socket.socket.connect_ex = _deny\nsocket.getaddrinfo = _deny\n")
                (guard / "sitecustomize.py").write_text(startup, encoding="utf-8")
                profile = base / ("profile_" + command)
                for subfolder in (profile / "temp", profile / "roaming", profile / "local"):
                    subfolder.mkdir(parents=True, exist_ok=True)
                child_env = literal_environment(settings.data_dir, settings.config_dir, guard, profile)
                argv = [str(ROOT / ".venv" / "Scripts" / "python.exe"), "-m", "warranty", command]
                completed = subprocess.run(argv, cwd=str(ROOT), env=child_env, capture_output=True,
                                           text=True, encoding="utf-8", timeout=45)
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                result = json.loads(completed.stdout)
                self.assertIsNone(result["error"])
                self.assertEqual(log.read_text(encoding="utf-8"), "STARTUP\n")
                if command == "public-protocol":
                    self.assertEqual(result["data"], public)
                else:
                    self.assertEqual(result["data"]["index"]["metadata_count"], 2)
                    self.assertEqual(result["data"]["index"]["synthetic_document_chunk_count"], 1)
                    self.assertEqual(result["data"]["index"]["document_scope"], expected_index["document_scope"])
                    self.assertEqual(result["data"]["runtime"]["public_processing"], public)
                cli_results.append({"argv": argv, "returncode": completed.returncode, "data": result["data"],
                                    "startup_guard": "STARTUP_ONLY_NO_NETWORK_ATTEMPT",
                                    "guard_sha256": hashlib.sha256((guard / "sitecustomize.py").read_bytes()).hexdigest()})
            for filename in ("model_usage.sqlite3", "model_credits.sqlite3", "embedding_cache.sqlite3"):
                self.assertFalse((settings.data_dir / filename).exists())
            self.assertEqual(fixture_hashes, {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                              for path in (metadata, bundle_path, settings.config_dir / "runtime.json",
                                           settings.config_dir / "source_policy.json")})
            OBSERVATIONS.update({"fixture_hashes": fixture_hashes, "fresh_temp_prefix": "warranty-public-factory-",
                                 "metadata_count": 2, "selected_item_count": 1, "linked_item_count": 2,
                                 "registered_text_sections": 2, "approved_indexed_chunks": 1,
                                 "unapproved_chunk_indexed": False, "public_summary": public,
                                 "cli": cli_results, "original_inputs_accessed": False,
                                 "actual_api_or_live_calls": 0, "budget_or_remote_cache_files_created": False})


if __name__ == "__main__":
    unittest.main()
