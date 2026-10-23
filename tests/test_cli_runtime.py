"""Operational CLI checks with authored synthetic adapters and no external work."""
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys
import unittest
from unittest.mock import Mock, patch
import tempfile

from warranty.cli import main
from warranty.errors import ServiceError


class CliRuntimeTests(unittest.TestCase):
    def run_cli(self, arguments):
        output, errors = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {}, clear=False), redirect_stdout(output), redirect_stderr(errors):
            main(arguments)
        self.assertEqual("", errors.getvalue())
        return json.loads(output.getvalue())

    @staticmethod
    def application():
        summary = {"items": 2, "synthetic": True, "simulation": True,
                   "index": {"official_document_body_count": 0, "synthetic_document_body_count": 0,
                             "embedding_model": "MOCK-synthetic-cli"}}
        blocked = Mock(side_effect=AssertionError("Read-only commands must not execute external or document work"))
        return SimpleNamespace(summary=Mock(return_value=summary), close=Mock(),
            worker=SimpleNamespace(run_pending=Mock(return_value={"processed_jobs": 0, "simulation": True})),
            document_jobs=SimpleNamespace(run_pending=blocked),
            rag=SimpleNamespace(prepare=blocked, enqueue_prepare=blocked, prepare_metadata=blocked,
                                model=SimpleNamespace(generate=blocked)))

    def test_status_preserves_facts_without_executing_document_or_model_work(self):
        app = self.application()
        with patch("warranty.application.build_application", return_value=app):
            response = self.run_cli(["status"])
        self.assertIsNone(response["error"])
        self.assertEqual(2, response["data"]["items"])
        self.assertTrue(response["data"]["synthetic"])
        self.assertEqual(0, response["data"]["index"]["official_document_body_count"])
        self.assertTrue(response["data"]["index"]["embedding_model"].startswith("MOCK"))
        app.close.assert_called_once()
        app.worker.run_pending.assert_not_called()
        app.document_jobs.run_pending.assert_not_called()
        app.rag.model.generate.assert_not_called()

    def test_input_validation_does_not_construct_runtime_or_run_a_model(self):
        catalog = SimpleNamespace(summary=Mock(return_value={"items": 2, "synthetic": True}))
        with patch("warranty.catalog.Catalog.load", return_value=catalog) as load, patch(
                "warranty.application.build_application", side_effect=AssertionError("Validation must not construct an application")):
            response = self.run_cli(["validate-input", "--metadata", "SYNTHETIC-input.json", "--sha256", "a" * 64])
        self.assertEqual(2, response["data"]["items"])
        self.assertIsNone(response["error"])
        self.assertEqual(Path("SYNTHETIC-input.json"), load.call_args.args[0])
        self.assertEqual("a" * 64, load.call_args.kwargs["expected_hash"])

    def test_status_configuration_error_uses_safe_json_and_nonzero_exit(self):
        output, errors = io.StringIO(), io.StringIO()
        with patch("warranty.application.build_application", side_effect=ServiceError(
                "CONFIG_INVALID", "The synthetic runtime configuration is unavailable.", 503)), redirect_stdout(output), redirect_stderr(errors):
            with self.assertRaises(SystemExit) as stopped:
                main(["status"])
        self.assertEqual(1, stopped.exception.code)
        response = json.loads(output.getvalue())
        self.assertIsNone(response["data"])
        self.assertEqual("CONFIG_INVALID", response["error"]["code"])
        self.assertEqual("", errors.getvalue())
        self.assertNotIn("Traceback", output.getvalue())

    def test_existing_worker_remains_settlement_only(self):
        app = self.application()
        with patch("warranty.application.build_application", return_value=app):
            response = self.run_cli(["worker"])
        self.assertIsNone(response["error"])
        self.assertTrue(response["data"]["simulation"])
        self.assertEqual(0, response["data"]["processed_jobs"])
        app.worker.run_pending.assert_called_once()
        app.document_jobs.run_pending.assert_not_called()
        app.close.assert_called_once()

    def test_non_ui_commands_keep_background_worker_disabled(self):
        app = self.application()
        with patch("warranty.application.build_application", return_value=app) as build:
            self.run_cli(["status"])
        self.assertIs(build.call_args.kwargs["start_document_worker"], False)

    def test_prepare_documents_only_enqueues_exact_actor_item_and_private_scope(self):
        app = self.application()
        app.rag.enqueue_prepare = Mock(return_value={"job_id": "SYNTHETIC-job", "status": "QUEUED"})
        with tempfile.TemporaryDirectory(prefix="synthetic-cli-scope-") as directory:
            path = Path(directory) / "scope.json"
            path.write_text(json.dumps({"country": "SYNTHETIC"}), encoding="utf-8")
            with patch("warranty.application.build_application", return_value=app) as build:
                response = self.run_cli(["prepare-documents", "--actor", "buyer", "--item", "SYNTHETIC-item",
                                         "--question", "SYNTHETIC document requirements", "--applicability", str(path)])
        self.assertEqual("QUEUED", response["data"]["status"])
        self.assertIs(build.call_args.kwargs["start_document_worker"], False)
        app.rag.enqueue_prepare.assert_called_once_with("buyer", "SYNTHETIC-item",
            applicability={"country": "SYNTHETIC"}, question="SYNTHETIC document requirements")
        app.document_jobs.run_pending.assert_not_called()
        app.rag.prepare.assert_not_called()
        app.rag.model.generate.assert_not_called()
        app.close.assert_called_once()

    def test_document_worker_is_explicit_one_shot_and_separate_from_settlements(self):
        app = self.application()
        app.document_jobs.run_pending = Mock(return_value=[{"job_id": "SYNTHETIC-job", "status": "BLOCKED",
                                                            "failure_code": "SOURCE_BLOCKED"}])
        with patch("warranty.application.build_application", return_value=app) as build:
            response = self.run_cli(["document-worker", "--limit", "1"])
        self.assertEqual("BLOCKED", response["data"][0]["status"])
        self.assertEqual("SOURCE_BLOCKED", response["data"][0]["failure_code"])
        app.document_jobs.run_pending.assert_called_once_with(limit=1)
        app.worker.run_pending.assert_not_called()
        self.assertIs(build.call_args.kwargs["start_document_worker"], False)

    def test_job_only_reads_authorized_existing_status(self):
        app = self.application()
        app.document_jobs.job = Mock(return_value={"job_id": "SYNTHETIC-job", "status": "BLOCKED"})
        with patch("warranty.application.build_application", return_value=app):
            response = self.run_cli(["job", "--actor", "buyer", "--job-id", "SYNTHETIC-job"])
        self.assertEqual("BLOCKED", response["data"]["status"])
        app.document_jobs.job.assert_called_once_with("buyer", "SYNTHETIC-job")
        app.document_jobs.run_pending.assert_not_called()
        app.worker.run_pending.assert_not_called()

    def test_retry_job_queues_only_the_existing_authorized_request(self):
        app = self.application()
        app.document_jobs.retry = Mock(return_value={"job_id": "SYNTHETIC-job", "status": "QUEUED", "attempt_no": 1})
        with patch("warranty.application.build_application", return_value=app) as build:
            response = self.run_cli(["retry-job", "--actor", "buyer", "--job-id", "SYNTHETIC-job"])
        self.assertEqual("QUEUED", response["data"]["status"])
        app.document_jobs.retry.assert_called_once_with("buyer", "SYNTHETIC-job")
        self.assertIs(build.call_args.kwargs["start_document_worker"], False)
        app.document_jobs.run_pending.assert_not_called()
        app.worker.run_pending.assert_not_called()
        app.rag.prepare.assert_not_called()
        app.rag.model.generate.assert_not_called()
        app.close.assert_called_once()

    def test_offline_evaluation_review_never_constructs_runtime(self):
        for command, extra, expected_review in (("evaluate-review-template", [], None),
                ("evaluate-review", ["--review", "SYNTHETIC-review.json"], Path("SYNTHETIC-review.json"))):
            with self.subTest(command=command), patch("warranty.application.build_application") as build, patch(
                    "warranty.evaluation_cli.review_evaluation", create=True,
                    return_value={"status": "DRAFT", "acceptance_status": "NOT_ASSESSED"}) as review:
                response = self.run_cli([command, "--artifact", "SYNTHETIC-artifact.json", "--output-dir", "SYNTHETIC-output", *extra])
            self.assertEqual("NOT_ASSESSED", response["data"]["acceptance_status"])
            build.assert_not_called()
            review.assert_called_once_with(Path("SYNTHETIC-artifact.json"), Path("SYNTHETIC-output"), review=expected_review)

    def test_offline_evaluation_review_requires_its_inputs_before_runtime_construction(self):
        for arguments in (["evaluate-review-template"],
                ["evaluate-review", "--artifact", "SYNTHETIC-artifact.json", "--output-dir", "SYNTHETIC-output"],
                ["evaluate-review-template", "--artifact", "SYNTHETIC-artifact.json", "--output-dir", "SYNTHETIC-output", "--review", "SYNTHETIC-review.json"]):
            output = io.StringIO()
            with self.subTest(arguments=arguments), patch("warranty.application.build_application") as build, redirect_stdout(output):
                with self.assertRaises(SystemExit) as stopped:
                    main(arguments)
            self.assertEqual(1, stopped.exception.code)
            self.assertEqual("INVALID_INPUT", json.loads(output.getvalue())["error"]["code"])
            build.assert_not_called()

    def test_explicit_metadata_index_uses_server_operator_without_other_work(self):
        app = self.application()
        app.runtime = {"collection": {"manager_actor": "SYNTHETIC-manager"}}
        app.rag.prepare_metadata = Mock(return_value={"status": "META_READY", "metadata_count": 2,
                                                     "embedding_model": "MOCK-synthetic-cli"})
        with patch("warranty.application.build_application", return_value=app):
            response = self.run_cli(["index-metadata"])
        self.assertEqual("META_READY", response["data"]["status"])
        self.assertTrue(response["data"]["embedding_model"].startswith("MOCK"))
        app.rag.prepare_metadata.assert_called_once_with("SYNTHETIC-manager")
        app.document_jobs.run_pending.assert_not_called()
        app.rag.model.generate.assert_not_called()

    def test_invalid_port_or_worker_limit_fails_before_constructing_runtime(self):
        for arguments in (["api", "--port", "0"], ["api", "--port", "65536"],
                          ["document-worker", "--limit", "0"], ["document-worker", "--limit", "101"]):
            output = io.StringIO()
            with (self.subTest(arguments=arguments), patch("warranty.application.build_application") as build,
                  redirect_stdout(output)):
                with self.assertRaises(SystemExit) as stopped:
                    main(arguments)
            self.assertEqual(1, stopped.exception.code)
            self.assertEqual("INVALID_INPUT", json.loads(output.getvalue())["error"]["code"])
            build.assert_not_called()

    def test_api_and_streamlit_forward_explicit_metadata_selection_without_remote_bind(self):
        with patch.dict(os.environ, {}, clear=False), patch("uvicorn.run") as serve:
            main(["api", "--metadata", "SYNTHETIC-input.json", "--sha256", "b" * 64, "--port", "8107"])
            self.assertEqual(str(Path("SYNTHETIC-input.json").resolve()), os.environ["METADATA_PATH"])
            self.assertEqual("b" * 64, os.environ["METADATA_EXPECTED_HASH"])
        self.assertEqual("127.0.0.1", serve.call_args.kwargs["host"])
        self.assertEqual(8107, serve.call_args.kwargs["port"])
        self.assertFalse(serve.call_args.kwargs["proxy_headers"])
        with patch.dict(os.environ, {}, clear=False), patch("warranty.cli.subprocess.call", return_value=0) as launch:
            with self.assertRaises(SystemExit) as stopped:
                main(["streamlit", "--metadata", "SYNTHETIC-input.json", "--sha256", "c" * 64])
            self.assertEqual(str(Path("SYNTHETIC-input.json").resolve()), os.environ["METADATA_PATH"])
            self.assertEqual("c" * 64, os.environ["METADATA_EXPECTED_HASH"])
        self.assertEqual(0, stopped.exception.code)
        self.assertFalse(launch.call_args.kwargs["shell"])
        self.assertIn("127.0.0.1", launch.call_args.args[0])

    def test_evaluate_delegates_configured_application_and_preserves_mock_label(self):
        app = self.application()
        app.runtime = {"collection": {"manager_actor": None}}
        with tempfile.TemporaryDirectory(prefix="synthetic-cli-evaluation-") as directory:
            config = Path(directory) / "evaluation.json"
            config.write_text(json.dumps({"mode": "MOCK"}), encoding="utf-8")
            with patch("warranty.application.build_application", return_value=app) as build, patch(
                    "warranty.evaluation_cli.run_evaluation", return_value={"mode": "MOCK", "scores": None}) as evaluate:
                response = self.run_cli(["evaluate", "--cases", "SYNTHETIC-cases.json",
                                         "--evaluation-config", str(config)])
        self.assertEqual("MOCK", response["data"]["mode"])
        self.assertIsNone(response["data"]["scores"])
        evaluate.assert_called_once_with(app, Path("SYNTHETIC-cases.json"), config)
        self.assertIs(build.call_args.kwargs["start_document_worker"], False)
        app.document_jobs.run_pending.assert_not_called()
        app.close.assert_called_once()

    def test_live_flags_are_rejected_for_normal_commands_before_runtime_construction(self):
        for arguments, expected in ((["status", "--live"], "INVALID_INPUT"),
                (["document-worker", "--live"], "INVALID_INPUT"),
                (["api", "--live"], "INVALID_INPUT"),
                (["live-query", "--actor", "buyer"], "LIVE_EXECUTION_REQUIRED"),
                (["live-index-metadata", "--live"], "LIVE_EXECUTION_REQUIRED")):
            output = io.StringIO()
            with self.subTest(arguments=arguments), patch("warranty.application.build_application") as build, redirect_stdout(output):
                with self.assertRaises(SystemExit) as stopped:
                    main(arguments)
            self.assertEqual(1, stopped.exception.code)
            self.assertEqual(expected, json.loads(output.getvalue())["error"]["code"])
            build.assert_not_called()

    def test_explicit_live_commands_scope_only_the_requested_synchronous_work(self):
        for command, purpose in (("live-index-metadata", "embedding_preparation"),
                ("live-prepare-documents", "embedding_preparation")):
            with self.subTest(command=command):
                app = self.application()
                events = []
                @contextmanager
                def scope(actual_purpose, approval):
                    events.append(("enter", actual_purpose, approval))
                    try:
                        yield
                    finally:
                        events.append(("exit", actual_purpose, approval))
                app.live_execution = Mock(side_effect=scope)
                def result(*args, **kwargs):
                    self.assertEqual([("enter", purpose, "SYNTHETIC-approval")], events)
                    return {"status": "SYNTHETIC_OFFLINE_RESULT"}
                app.rag.prepare_metadata = Mock(side_effect=result)
                app.rag.prepare = Mock(side_effect=result)
                app.rag.ask = Mock(side_effect=result)
                arguments = [command, "--live", "--approval-id", "SYNTHETIC-approval", "--actor", "buyer"]
                if command == "live-index-metadata":
                    arguments += ["--items", "SYNTHETIC-a", "SYNTHETIC-b"]
                else:
                    arguments += ["--item", "SYNTHETIC-a", "--question", "SYNTHETIC question", "--purpose", "demo"]
                with patch("warranty.application.build_application", return_value=app) as build:
                    response = self.run_cli(arguments)
                self.assertEqual("LIVE", response["data"]["mode"])
                self.assertEqual("NOT_ASSESSED", response["data"]["acceptance_status"])
                self.assertEqual([("enter", purpose, "SYNTHETIC-approval"),
                                  ("exit", purpose, "SYNTHETIC-approval")], events)
                self.assertIs(build.call_args.kwargs["start_document_worker"], False)
                if command == "live-index-metadata":
                    app.rag.prepare_metadata.assert_called_once_with("buyer", item_ids=["SYNTHETIC-a", "SYNTHETIC-b"])
                else:
                    app.rag.prepare.assert_called_once_with("buyer", "SYNTHETIC-a", applicability={}, question="SYNTHETIC question")
                app.rag.ask.assert_not_called()
                app.document_jobs.run_pending.assert_not_called()
                app.rag.enqueue_prepare.assert_not_called()
                app.close.assert_called_once()

    def test_live_query_opens_one_demo_scope_without_answer_cache_or_document_collection(self):
        app = self.application()
        events = []
        @contextmanager
        def scope(purpose, approval):
            events.append(("enter", purpose, approval))
            try:
                yield
            finally:
                events.append(("exit", purpose, approval))
        app.live_execution = Mock(side_effect=scope)
        def query(*args, **kwargs):
            self.assertEqual([("enter", "demo", "SYNTHETIC-approval")], events)
            return {"status": "SYNTHETIC_OFFLINE_RESULT"}
        app.rag.ask = Mock(side_effect=query)
        with patch("warranty.application.build_application", return_value=app) as build:
            response = self.run_cli(["live-query", "--live", "--approval-id", "SYNTHETIC-approval",
                "--actor", "buyer", "--item", "SYNTHETIC-a", "--question", "SYNTHETIC question", "--purpose", "demo"])
        self.assertIsNone(response["error"])
        self.assertEqual("LIVE", response["data"]["mode"])
        self.assertEqual("NOT_ASSESSED", response["data"]["acceptance_status"])
        app.live_execution.assert_called_once_with("demo", "SYNTHETIC-approval")
        self.assertEqual([("enter", "demo", "SYNTHETIC-approval"), ("exit", "demo", "SYNTHETIC-approval")], events)
        app.rag.ask.assert_called_once_with("buyer", "SYNTHETIC-a", "SYNTHETIC question",
                                           applicability={}, cache=False, allow_collection=False)
        self.assertIs(build.call_args.kwargs["start_document_worker"], False)
        app.rag.prepare_metadata.assert_not_called()
        app.rag.prepare.assert_not_called()
        app.rag.enqueue_prepare.assert_not_called()
        app.document_jobs.run_pending.assert_not_called()
        app.worker.run_pending.assert_not_called()
        app.close.assert_called_once()

    def test_live_metadata_missing_explicit_items_is_safe_and_does_not_index_everything(self):
        app = self.application()
        events = []
        @contextmanager
        def scope(purpose, approval):
            events.append(("enter", purpose, approval))
            try:
                yield
            finally:
                events.append(("exit", purpose, approval))
        app.live_execution = Mock(side_effect=scope)
        output, errors = io.StringIO(), io.StringIO()
        with patch("warranty.application.build_application", return_value=app), redirect_stdout(output), redirect_stderr(errors):
            with self.assertRaises(SystemExit) as stopped:
                main(["live-index-metadata", "--live", "--approval-id", "SYNTHETIC-approval", "--actor", "buyer"])
        self.assertEqual(1, stopped.exception.code)
        response = json.loads(output.getvalue())
        self.assertEqual("INVALID_INPUT", response["error"]["code"])
        self.assertIsNone(response["data"])
        self.assertNotIn("SYNTHETIC-approval", output.getvalue())
        self.assertEqual("", errors.getvalue())
        self.assertEqual([("enter", "embedding_preparation", "SYNTHETIC-approval"),
                          ("exit", "embedding_preparation", "SYNTHETIC-approval")], events)
        app.rag.prepare_metadata.assert_not_called()
        app.rag.prepare.assert_not_called()
        app.rag.model.generate.assert_not_called()
        app.rag.enqueue_prepare.assert_not_called()
        app.document_jobs.run_pending.assert_not_called()
        app.close.assert_called_once()

    def test_live_evaluation_requires_explicit_flag_and_approval_and_mock_cannot_claim_live(self):
        for mode, flags, expected in (("LIVE", [], "LIVE_EXECUTION_REQUIRED"),
                ("LIVE", ["--live"], "LIVE_EXECUTION_REQUIRED"),
                ("MOCK", ["--live", "--approval-id", "SYNTHETIC-approval"], "INVALID_INPUT"),
                ("LIVE", ["--live", "--approval-id", "SYNTHETIC-approval"], None)):
            with self.subTest(mode=mode, flags=flags), tempfile.TemporaryDirectory(prefix="synthetic-cli-live-eval-") as directory:
                app = self.application()
                events = []
                @contextmanager
                def scope(purpose, approval):
                    events.append(("enter", purpose, approval))
                    try:
                        yield
                    finally:
                        events.append(("exit", purpose, approval))
                app.live_execution = Mock(side_effect=scope)
                config = Path(directory) / "evaluation.json"
                config.write_text(json.dumps({"mode": mode}), encoding="utf-8")
                arguments = ["evaluate", "--cases", "SYNTHETIC-cases.json", "--evaluation-config", str(config), *flags]
                output = io.StringIO()
                def evaluate(*args):
                    self.assertEqual([("enter", "evaluation", "SYNTHETIC-approval")], events)
                    return {"mode": "LIVE", "acceptance_status": "NOT_ASSESSED"}
                with patch("warranty.application.build_application", return_value=app), patch(
                        "warranty.evaluation_cli.run_evaluation", side_effect=evaluate) as run, redirect_stdout(output):
                    if expected is not None:
                        with self.assertRaises(SystemExit) as stopped:
                            main(arguments)
                        self.assertEqual(1, stopped.exception.code)
                    else:
                        main(arguments)
                response = json.loads(output.getvalue())
                if expected is not None:
                    self.assertEqual(expected, response["error"]["code"])
                    run.assert_not_called()
                    app.live_execution.assert_not_called()
                else:
                    self.assertEqual("LIVE", response["data"]["mode"])
                    run.assert_called_once_with(app, Path("SYNTHETIC-cases.json"), config)
                    self.assertEqual(("exit", "evaluation", "SYNTHETIC-approval"), events[-1])
                app.rag.model.generate.assert_not_called()
                app.document_jobs.run_pending.assert_not_called()
                app.close.assert_called_once()

    def test_explicit_missing_applicability_is_not_silently_replaced_by_empty_scope(self):
        app = self.application()
        app.rag.enqueue_prepare = Mock()
        output, errors = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory(prefix="synthetic-cli-missing-") as directory:
            missing = Path(directory) / "missing-scope.json"
            with patch("warranty.application.build_application", return_value=app), redirect_stdout(output), redirect_stderr(errors):
                with self.assertRaises(SystemExit) as stopped:
                    main(["prepare-documents", "--actor", "buyer", "--item", "SYNTHETIC-item",
                          "--applicability", str(missing)])
        self.assertEqual(1, stopped.exception.code)
        self.assertIn(json.loads(output.getvalue())["error"]["code"], {"INVALID_INPUT", "CONFIG_INVALID"})
        self.assertEqual("", errors.getvalue())
        app.rag.enqueue_prepare.assert_not_called()
        app.close.assert_called_once()

    def test_unexpected_runtime_error_does_not_expose_path_token_or_stack(self):
        output, errors = io.StringIO(), io.StringIO()
        private_value = "SYNTHETIC_PRIVATE_PATH_AND_TOKEN"
        with patch("warranty.application.build_application", side_effect=RuntimeError(private_value)), redirect_stdout(output), redirect_stderr(errors):
            with self.assertRaises(SystemExit) as stopped:
                main(["status"])
        self.assertEqual(1, stopped.exception.code)
        self.assertEqual("RUNTIME_UNAVAILABLE", json.loads(output.getvalue())["error"]["code"])
        self.assertNotIn(private_value, output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())
        self.assertEqual("", errors.getvalue())

    def test_document_registration_and_import_preserve_operator_and_do_not_run_jobs(self):
        operations = ModuleType("warranty.document_operations")
        operations.sync_documents = Mock(return_value={"status": "SYNTHETIC_REGISTERED", "fetched_count": 0})
        operations.import_documents = Mock(return_value={"status": "SYNTHETIC_IMPORTED", "is_synthetic": True})
        for command, target, actor_args, expected_actor in (
                ("documents-sync", operations.sync_documents, [], "SYNTHETIC-manager"),
                ("documents-import", operations.import_documents, ["--actor", "buyer"], "buyer")):
            with self.subTest(command=command):
                app = self.application()
                app.runtime = {"collection": {"manager_actor": "SYNTHETIC-manager"}}
                with patch.dict(sys.modules, {"warranty.document_operations": operations}), patch(
                        "warranty.application.build_application", return_value=app) as build:
                    response = self.run_cli([command, *actor_args])
                self.assertIsNone(response["error"])
                target.assert_called_once_with(app, expected_actor)
                self.assertIs(build.call_args.kwargs["start_document_worker"], False)
                app.document_jobs.run_pending.assert_not_called()
                app.worker.run_pending.assert_not_called()
                app.rag.model.generate.assert_not_called()
                app.close.assert_called_once()

    def test_manager_commands_without_server_operator_fail_before_work(self):
        for command in ("documents-sync", "documents-import", "index-metadata"):
            app = self.application()
            output = io.StringIO()
            with self.subTest(command=command), patch("warranty.application.build_application", return_value=app), redirect_stdout(output):
                with self.assertRaises(SystemExit) as stopped:
                    main([command])
            self.assertEqual(1, stopped.exception.code)
            self.assertEqual("FORBIDDEN", json.loads(output.getvalue())["error"]["code"])
            app.rag.prepare_metadata.assert_not_called()
            app.document_jobs.run_pending.assert_not_called()
            app.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
