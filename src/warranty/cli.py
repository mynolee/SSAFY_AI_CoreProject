"""Standard local entry points; private development tools are not required."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from .errors import ServiceError


def main(argv=None):
    parser = argparse.ArgumentParser(prog="warranty")
    parser.add_argument("command", choices=("status", "streamlit", "api", "worker", "validate-input",
        "documents-sync", "documents-import", "index-metadata", "prepare-documents", "document-worker", "job", "evaluate",
        "live-index-metadata", "live-prepare-documents", "live-query", "public-protocol"))
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--sha256")
    parser.add_argument("--port", type=int)
    parser.add_argument("--actor")
    parser.add_argument("--item")
    parser.add_argument("--question")
    parser.add_argument("--applicability", type=Path)
    parser.add_argument("--job-id")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--cases", type=Path)
    parser.add_argument("--evaluation-config", type=Path)
    parser.add_argument("--items", nargs="+")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--approval-id")
    parser.add_argument("--purpose", choices=("verification", "final_verification", "demo"), default="verification")
    args = parser.parse_args(argv)
    try:
        if args.port is not None and not 1 <= args.port <= 65535:
            raise ServiceError("INVALID_INPUT", "포트 범위를 확인해 주세요.", 422, False)
        if not 1 <= args.limit <= 100:
            raise ServiceError("INVALID_INPUT", "작업 개수 범위를 확인해 주세요.", 422, False)
        if args.command.startswith("live-") and (not args.live or not args.approval_id):
            raise ServiceError("LIVE_EXECUTION_REQUIRED", "명시적 LIVE 실행과 승인된 예산 설정이 필요합니다.", 403, False)
        if args.live and args.command not in {"evaluate", "live-index-metadata", "live-prepare-documents", "live-query"}:
            raise ServiceError("INVALID_INPUT", "LIVE 전용 명령을 사용해 주세요.", 422, False)
        if args.command in {"streamlit", "api"}:
            # Child UI factories receive the same explicit catalog selection.
            if args.metadata is not None:
                os.environ["METADATA_PATH"] = str(args.metadata.resolve())
            if args.sha256 is not None:
                os.environ["METADATA_EXPECTED_HASH"] = args.sha256
        if args.command == "validate-input":
            from .catalog import Catalog
            if not args.metadata:
                parser.error("validate-input requires --metadata")
            result = Catalog.load(args.metadata, expected_hash=args.sha256).summary()
        elif args.command == "streamlit":
            script = Path(__file__).resolve().parent / "interfaces" / "streamlit_app.py"
            code = subprocess.call([sys.executable, "-m", "streamlit", "run", str(script),
                                    "--server.address", "127.0.0.1", "--server.port", str(args.port or 8501),
                                    "--browser.gatherUsageStats", "false", "--server.headless", "true"], shell=False)
            raise SystemExit(code)
        elif args.command == "api":
            import uvicorn
            uvicorn.run("warranty.interfaces.api:create_app", factory=True, host="127.0.0.1", port=args.port or 8000,
                        access_log=False, proxy_headers=False)
            return
        else:
            from .application import build_application
            if args.sha256 is not None:
                os.environ["METADATA_EXPECTED_HASH"] = args.sha256
            app = build_application(metadata_path=args.metadata, start_document_worker=False)
            try:
                actor = args.actor or (getattr(app, "runtime", None) or {}).get("collection", {}).get("manager_actor")
                if args.command == "status":
                    result = app.summary()
                elif args.command == "public-protocol":
                    context = getattr(app.models, "public_context", None)
                    result = (context.summary() if context else {
                        "configured": False, "creates_transmission_authority": False,
                        "reason_code": "PUBLIC_PROTOCOL_REQUIRED"})
                elif args.command == "worker":
                    result = app.worker.run_pending()
                elif args.command in {"documents-sync", "documents-import", "index-metadata"}:
                    if not actor:
                        raise ServiceError("FORBIDDEN", "자료 관리자 실행 주체를 확인해 주세요.", 403, False)
                    if args.command == "index-metadata":
                        result = app.rag.prepare_metadata(actor, item_ids=args.items) if args.items else app.rag.prepare_metadata(actor)
                    else:
                        from .document_operations import sync_documents, import_documents
                        result = (sync_documents if args.command == "documents-sync" else import_documents)(app, actor)
                elif args.command == "prepare-documents":
                    if not actor or not args.item:
                        raise ServiceError("INVALID_INPUT", "실행 주체와 항목을 지정해 주세요.", 422, False)
                    from .runtime import read_private_json
                    scope = read_private_json(args.applicability, default=None) if args.applicability else {}
                    if not isinstance(scope, dict):
                        raise ServiceError("INVALID_INPUT", "적용 조건을 확인해 주세요.", 422, False)
                    result = app.rag.enqueue_prepare(actor, args.item, applicability=scope, question=args.question)
                elif args.command == "document-worker":
                    result = app.document_jobs.run_pending(limit=args.limit)
                elif args.command == "job":
                    if not actor or not args.job_id:
                        raise ServiceError("INVALID_INPUT", "실행 주체와 작업 ID를 지정해 주세요.", 422, False)
                    result = app.document_jobs.job(actor, args.job_id)
                elif args.command.startswith("live-"):
                    if not actor:
                        raise ServiceError("FORBIDDEN", "실행 주체를 확인해 주세요.", 403, False)
                    purpose = args.purpose if args.command == "live-query" else "embedding_preparation"
                    from .runtime import read_private_json
                    scope = read_private_json(args.applicability, default=None) if args.applicability else {}
                    if not isinstance(scope, dict):
                        raise ServiceError("INVALID_INPUT", "적용 조건을 확인해 주세요.", 422, False)
                    with app.live_execution(purpose, args.approval_id):
                        if args.command == "live-index-metadata":
                            if not args.items:
                                raise ServiceError("INVALID_INPUT", "처리할 항목을 명시해 주세요.", 422, False)
                            result = app.rag.prepare_metadata(actor, item_ids=args.items)
                        else:
                            if not args.item or not args.question:
                                raise ServiceError("INVALID_INPUT", "항목과 질문을 지정해 주세요.", 422, False)
                            if args.command == "live-prepare-documents":
                                result = app.rag.prepare(actor, args.item, applicability=scope, question=args.question)
                            else:
                                result = app.rag.ask(actor, args.item, args.question, applicability=scope,
                                                     cache=False, allow_collection=False)
                    result = {"mode": "LIVE", "acceptance_status": "NOT_ASSESSED", "result": result}
                else:
                    if not args.cases or not args.evaluation_config:
                        raise ServiceError("INVALID_INPUT", "비공개 평가 입력과 설정을 지정해 주세요.", 422, False)
                    from .evaluation_cli import run_evaluation
                    from .runtime import read_private_json
                    evaluation = read_private_json(args.evaluation_config, default=None, max_bytes=2097152)
                    if not isinstance(evaluation, dict):
                        raise ServiceError("INVALID_INPUT", "평가 설정을 확인해 주세요.", 422, False)
                    if evaluation.get("mode", "MOCK") == "LIVE":
                        if not args.live or not args.approval_id:
                            raise ServiceError("LIVE_EXECUTION_REQUIRED", "명시적 LIVE 실행과 승인된 예산 설정이 필요합니다.", 403, False)
                        with app.live_execution("evaluation", args.approval_id):
                            result = run_evaluation(app, args.cases, args.evaluation_config)
                    else:
                        if args.live:
                            raise ServiceError("INVALID_INPUT", "LIVE 평가 모드를 확인해 주세요.", 422, False)
                        result = run_evaluation(app, args.cases, args.evaluation_config)
            finally:
                app.close()
        print(json.dumps({"data": result, "error": None}, ensure_ascii=True, indent=2))
    except ServiceError as error:
        print(json.dumps({"data": None, "error": error.as_dict()}, ensure_ascii=True))
        raise SystemExit(1) from None
    except Exception:
        error = ServiceError("RUNTIME_UNAVAILABLE", "실행 상태와 비공개 설정을 확인해 주세요.", 503, True)
        print(json.dumps({"data": None, "error": error.as_dict()}, ensure_ascii=True))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
