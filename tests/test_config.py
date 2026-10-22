from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from warranty.config import Settings
from warranty.errors import ServiceError


class SettingsTests(unittest.TestCase):
    def test_relative_project_storage_is_resolved_and_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(Path("."), Path(directory) / "config")
            self.assertTrue(settings.data_dir.is_absolute())
            with self.assertRaises(ValueError):
                settings.prepare()
            self.assertFalse(settings.config_dir.exists())

    def test_all_paths_validated_before_any_directory_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            private_data = Path(directory) / "private-data"
            settings = Settings(private_data, Path("."))
            with self.assertRaises(ValueError):
                settings.prepare()
            self.assertFalse(private_data.exists())

    def test_external_directories_can_be_created(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(Path(directory) / "data", Path(directory) / "config")
            settings.prepare()
            self.assertTrue(settings.data_dir.is_dir())
            self.assertTrue(settings.config_dir.is_dir())


class RuntimeInputTests(unittest.TestCase):
    def test_configured_invalid_input_is_never_a_synthetic_fallback(self):
        from warranty.application import build_application
        for value in ([], ["nonexistent"], {}, False, 1, None, ""):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                settings = Settings(Path(directory) / "data", Path(directory) / "config")
                settings.prepare()
                (settings.config_dir / "runtime.json").write_text(json.dumps({"metadata_path": value}), encoding="utf-8")
                with patch.dict(os.environ, {"METADATA_PATH": "", "METADATA_EXPECTED_HASH": ""}):
                    with self.assertRaises(ServiceError) as error:
                        build_application(settings=settings)
                self.assertEqual("CONFIG_INVALID", error.exception.code)
                self.assertFalse((settings.data_dir / "warranty.sqlite3").exists())

    def test_runtime_hash_and_explicit_path_are_validated(self):
        from warranty.application import build_application
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(Path(directory) / "data", Path(directory) / "config")
            settings.prepare()
            config = settings.config_dir / "runtime.json"
            for value in (None, [], False, "invalid"):
                config.write_text(json.dumps({"metadata_expected_hash": value}), encoding="utf-8")
                with self.assertRaises(ServiceError) as error:
                    build_application(settings=settings)
                self.assertEqual("CONFIG_INVALID", error.exception.code)
            config.write_text("{}", encoding="utf-8")
            with self.assertRaises(ServiceError) as error:
                build_application(settings=settings, metadata_path=[])
            self.assertEqual("CONFIG_INVALID", error.exception.code)

    def test_standard_cli_returns_safe_error_for_malformed_private_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            private_config = base / "config"
            private_config.mkdir()
            (private_config / "runtime.json").write_text(json.dumps({"metadata_path": ["synthetic-hidden-path"]}), encoding="utf-8")
            env = dict(os.environ, PRIVATE_CONFIG_DIR=str(private_config), PRIVATE_DATA_DIR=str(base / "data"),
                       METADATA_PATH="", METADATA_EXPECTED_HASH="")
            result = subprocess.run([sys.executable, "-m", "warranty", "status"], env=env,
                                    capture_output=True, text=True, timeout=30, shell=False)
            self.assertEqual(1, result.returncode)
            self.assertEqual("CONFIG_INVALID", json.loads(result.stdout)["error"]["code"])
            self.assertEqual("", result.stderr)
            self.assertNotIn("synthetic-hidden-path", result.stdout)
