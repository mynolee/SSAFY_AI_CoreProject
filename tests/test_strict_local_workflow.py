"""Strict source-policy workflow through actual local services, without GMS."""
from contextlib import ExitStack
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_rag as fixtures
from test_rag_collection_flow import RecordingFetcher
from warranty.catalog import Catalog
from warranty.collection import Collector
from warranty.config import Settings
from warranty.db import Database
from warranty.errors import ServiceError
from warranty.rag import RagService
from warranty.security import SourcePolicy
from warranty.transactions import TradeService


class StrictLocalWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        temporary = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="synthetic-strict-flow-")))
        for name in ("connect", "connect_ex"):
            self.stack.enter_context(patch.object(socket.socket, name, side_effect=AssertionError("Network forbidden")))
        self.stack.enter_context(patch.object(socket, "create_connection", side_effect=AssertionError("Network forbidden")))
        self.settings = Settings(temporary / "data", temporary / "config")
        self.settings.prepare()
        self.catalog = Catalog.synthetic()
        self.item = self.catalog.items[0]["item_id"]
        self.db = Database(self.settings.data_dir / "app.sqlite3")
        self.db.initialize()
        self.stack.callback(self.db.close)
        readers = ["manager", "buyer", "seller", "admin"]
        sources = [{"source_id": "SYNTHETIC-DOC", "url": "https://example.org/synthetic-terms"},
                   {"source_id": "SYNTHETIC-SOURCE", "url": self.catalog.sources["SYNTHETIC-SOURCE"]["url"]}]
        reviews = []
        for source in sources:
            for action in fixtures.ACTIONS:
                reviews.append({"source_id": source["source_id"], "action": action,
                    "status": "BLOCKED" if action in {"external_process", "public_redistribute"} else "ALLOWED",
                    "basis_type": "OTHER_RECORDED_BASIS", "basis_url": source["url"],
                    "reviewed_on": "2026-01-01", "scope": "Authored synthetic local workflow only",
                    "conditions": "No external processing or public redistribution",
                    "user_scope": "Synthetic actors representing one local test operator", "processing_environment": "LOCAL",
                    "version_scope": "SOURCE", "allowed_actors": readers if action in
                    {"view_link", "local_embed", "display_excerpt"} else ["manager"]})
        self.policy = SourcePolicy.from_config({"schema_version": 1, "registry": sources,
            "reviews": reviews, "capabilities": {"manager": ["DATA_MANAGER"]}}, network_enabled=False)
        self.fetcher = RecordingFetcher([])
        self.rag = RagService(self.catalog, self.db, self.settings, model=fixtures.ExtractiveModel(),
            prompt_store=fixtures.SyntheticPromptStore(), source_policy=self.policy,
            fetcher=self.fetcher, index_actor="manager")
        self.stack.callback(self.rag.close)
        self.collector = Collector(self.rag.registry, manager_actor="manager")
        self.rag.collector = self.collector
        document = self.rag.registry.register_document("manager", source_id="SYNTHETIC-DOC",
            title="SYNTHETIC strict workflow terms", canonical_url=sources[0]["url"],
            document_type="HTML", is_synthetic=True)
        self.target = self.rag.registry.register_target("manager", self.item, document["document_id"],
            role="SERVICE_TERMS", applicability=fixtures.SCOPE,
            applicability_basis="Authored synthetic document explicitly covers the synthetic item")

    def collect(self, actor="buyer", question="SYNTHETIC first question"):
        return self.collector.collect(self.item, question, ["transfer"], fixtures.INPUTS, actor)

    def mutate(self, trades, trade_id, action, payload=None, actor="buyer"):
        current = trades.get(actor, trade_id)
        return trades.mutate(actor, trade_id, action, payload or {}, uuid4().hex, current["version"])

    def test_manager_acquisition_reader_reuse_grounded_review_and_adoption(self):
        first = self.collect()
        second = self.collect(question="SYNTHETIC different question with the same query")
        self.assertEqual((first["status"], second["status"], second["cache_hits"]), ("FETCHED", "FETCHED", 1))
        self.assertEqual([row[2] for row in self.fetcher.calls], ["manager"])
        prepared = self.rag.prepare("buyer", self.item, fixtures.INPUTS)
        answer = self.rag.ask("buyer", self.item, "SYNTHETIC transfer requirements?", fixtures.INPUTS)
        checklist = self.rag.checklist(self.item, fixtures.INPUTS, user_id="buyer", review_id=answer["review_id"])
        self.assertEqual((prepared["status"], answer["readiness"]["status"], answer["classification"]),
                         ("COMPLETED", "INDEX_READY", "CONDITIONAL"))
        self.assertTrue(checklist["documents"])
        self.assertEqual((checklist["review_id"], checklist["conditions"]), (answer["review_id"], answer["conditions"]))
        self.assertTrue(checklist["cited_evidence_ids"])
        self.assertFalse(checklist["conditions_ok"])
        self.assertEqual([row["result"]["review_id"] for row in self.rag.history("buyer", self.item)], [answer["review_id"]])
        self.assertEqual(self.rag.history("seller", self.item), [])

        trades = TradeService(self.db, review_validator=self.rag.validate_review, review_guard=self.rag.review_guard)
        trade = trades.create("buyer", "buyer", "seller", self.item, 500, 50)
        target = {"revision": trade["contract"]["revision"], "hash": trade["contract"]["hash"]}
        self.mutate(trades, trade["trade_id"], "contract_consent", target)
        self.mutate(trades, trade["trade_id"], "contract_consent", target, "seller")
        self.mutate(trades, trade["trade_id"], "deposit")
        adopted = self.mutate(trades, trade["trade_id"], "adopt_review", {"review_id": answer["review_id"]})
        versions = sorted({row["version_id"] for row in answer["evidence"] if row["source_type"] != "metadata"})
        self.assertEqual((adopted["contract"]["review_id"], adopted["review"]["classification"],
                          adopted["review"]["document_version_ids"]),
                         (adopted["review"]["review_id"], "CONDITIONAL", versions))
        self.assertEqual(adopted["contract"]["revision"], trade["contract"]["revision"] + 1)
        self.assertEqual(len(self.fetcher.calls), 1)

    def test_cached_body_does_not_grant_reader_or_mutator_permissions(self):
        self.collect()
        self.rag.prepare("buyer", self.item, fixtures.INPUTS)
        saved = self.rag.ask("buyer", self.item, "SYNTHETIC authorized review", fixtures.INPUTS)
        with self.assertRaises(ServiceError) as denied:
            self.collect(actor="outsider")
        self.assertEqual(denied.exception.code, "SOURCE_BLOCKED")
        blocked = self.rag.checklist(self.item, fixtures.INPUTS, user_id="outsider", review_id=saved["review_id"])
        self.assertEqual((blocked["readiness"]["status"], blocked["documents"], blocked["evidence"]),
                         ("DOC_BLOCKED", [], []))
        with self.assertRaises(ServiceError) as foreign:
            self.rag.validate_review("outsider", self.item, saved["review_id"])
        self.assertEqual(foreign.exception.code, "FORBIDDEN")
        with self.assertRaises(ServiceError):
            self.policy.require_capability("buyer", "DATA_MANAGER")
        with self.assertRaises(ServiceError):
            self.policy.require("SYNTHETIC-DOC", "fetch", actor="buyer")
        for action in ("external_process", "public_redistribute"):
            with self.subTest(action=action), self.assertRaises(ServiceError):
                self.policy.require("SYNTHETIC-DOC", action, actor="manager")
        self.assertEqual(len(self.fetcher.calls), 1)


if __name__ == "__main__":
    unittest.main()
