"""Offline product checks using temporary storage and synthetic data only.

Run with: python -m unittest discover -s tests -p test_smoke.py -v
"""
from pathlib import Path
from contextlib import closing
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from warranty.config import Settings


class ProductSmokeTests(unittest.TestCase):
    def test_private_runtime_storage_rejects_project_directory(self):
        project = Path(__file__).resolve().parents[1]
        settings = Settings(project / ".test-data", project / ".test-config")
        with self.assertRaises(ValueError):
            settings.prepare()
        self.assertFalse(settings.data_dir.exists())

    def test_database_transaction_failure_rolls_back(self):
        from warranty.db import Database

        with tempfile.TemporaryDirectory(prefix="warranty-smoke-") as directory:
            db = Database(Path(directory) / "app.sqlite3")
            db.initialize()
            with closing(db.connect()) as connection:
                self.assertEqual(1, connection.execute("PRAGMA foreign_keys").fetchone()[0])
                connection.execute("CREATE TABLE smoke_atomic (value INTEGER NOT NULL)")
            with self.assertRaises(RuntimeError):
                with db.transaction() as connection:
                    connection.execute("INSERT INTO smoke_atomic VALUES (?)", (7,))
                    raise RuntimeError("synthetic interruption")
            with closing(db.connect()) as connection:
                self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM smoke_atomic").fetchone()[0])

    def test_synthetic_catalog_has_explicit_readiness_and_unknowns(self):
        from warranty.catalog import Catalog

        catalog = Catalog.synthetic()
        self.assertEqual(2, len(catalog.items))
        for item in catalog.items:
            self.assertTrue(item["item_id"].startswith("SYNTHETIC"))
            selected = catalog.get(item["item_id"])
            self.assertIn("readiness", selected)
        result = catalog.search(transfer_required=True)
        self.assertEqual([], result["matches"])
        self.assertEqual(2, len(result["unknown"]))

    def test_shared_facade_initializes_without_network_credentials(self):
        from warranty.application import build_application

        with tempfile.TemporaryDirectory(prefix="warranty-smoke-") as directory:
            base = Path(directory)
            settings = Settings(base / "data", base / "config")
            with patch.dict(os.environ, {"METADATA_PATH": "", "METADATA_EXPECTED_HASH": ""}):
                app = build_application(settings=settings)
            try:
                self.assertEqual(2, len(app.catalog.items))
                self.assertIs(app.settings, settings)
                self.assertIsNotNone(app.db)
                self.assertIsNotNone(app.rag)
                self.assertIsNotNone(app.trades)
                self.assertFalse(settings.remote_models)
                self.assertFalse(settings.real_uploads)
            finally:
                app.rag.close()
                app.db.close()


if __name__ == "__main__":
    unittest.main()
