"""Composition root shared by the local user interfaces."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any

from .config import Settings
from .errors import ServiceError


@dataclass
class Application:
    settings: Settings
    db: Any
    catalog: Any
    rag: Any
    trades: Any
    worker: Any
    runtime: Any = None
    models: Any = None
    authorization: Any = None
    document_jobs: Any = None
    document_plan: Any = None

    def close(self):
        if self.document_jobs is not None and not self.document_jobs.stop(timeout=5):
            return False
        self.rag.close()
        self.db.close()
        if self.models is not None:
            for model in (self.models.chat, self.models.embedding):
                close = getattr(model, "close", None)
                if callable(close):
                    close()
        return True

    def runtime_status(self):
        runtime = self.runtime or {"models": {}, "collection": {}}
        config, collection = runtime["models"], runtime["collection"]
        def status(model, provider):
            mock = bool(getattr(model, "is_mock", False))
            metadata = model.metadata() if callable(getattr(model, "metadata", None)) else {}
            adapter = getattr(model, "adapter", model)
            configured = mock or bool(getattr(adapter, "_api_key", "") and metadata.get("model_id"))
            ready = mock or bool(metadata.get("can_generate") and self.settings.remote_models
                                 and config.get("external_processing_approved", False))
            return {"provider": provider, "configured": configured, "ready": ready, "is_mock": mock}
        registry = self.rag.registry
        return {"chat": status(self.rag.model, config.get("chat_provider", "mock")),
            "embedding": status(self.rag.vectors.embeddings, config.get("embedding_provider", "mock")),
            "collection": {"enabled": collection.get("enabled", False),
                "network_enabled": collection.get("network_enabled", False),
                "registered_target_count": len(registry.registered_candidates())},
            "external_processing_approved": config.get("external_processing_approved", False),
            "question_processing_approved": config.get("allow_question_processing", False),
            "document_worker_enabled": collection.get("worker_enabled", False), "live": self.live_status(),
            "public_processing": (self.models.public_context.summary()
                if getattr(self.models, "public_context", None) else {"configured": False, "creates_transmission_authority": False})}

    def live_status(self):
        from .live import live_status
        return live_status(self)

    def live_execution(self, purpose, approval_id):
        from .live import live_execution
        return live_execution(self, purpose, approval_id)

    def summary(self):
        return {**self.catalog.summary(), "index": self.rag.corpus_summary(),
                "simulation": True, "remote_models_enabled": self.settings.remote_models,
                "real_uploads_enabled": False, "runtime": self.runtime_status()}


def _runtime(settings):
    from .runtime import load_runtime
    return load_runtime(settings)


def build_application(settings: Settings | None = None, metadata_path=None, *, start_document_worker=True, env=None) -> Application:
    """Load configured facts, use synthetic facts only when no input was configured."""
    os.environ["ANONYMIZED_TELEMETRY"] = "false"
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    from .catalog import Catalog
    from .db import Database
    from .rag import RagService
    from .transactions import TradeService
    from .runtime import ProcessingAuthorization, build_models, load_source_policy, load_document_plan
    from .security import SafeFetcher
    from .collection import Collector
    from .jobs import DocumentJobQueue

    settings = settings or Settings.from_env()
    try:
        settings.prepare()
    except (ValueError, OSError):
        raise ServiceError("PRIVATE_STORAGE_INVALID", "실행 자료와 설정의 저장 위치를 확인해 주세요.", 503) from None
    runtime = _runtime(settings)
    if metadata_path is not None and (
        not isinstance(metadata_path, (str, Path)) or not str(metadata_path).strip()
    ):
        raise ServiceError("CONFIG_INVALID", "입력 파일 설정을 확인해 주세요.", 503)
    supplied_env = os.environ if env is None else env
    source = metadata_path if metadata_path is not None else supplied_env.get("METADATA_PATH") or runtime.get("metadata_path")
    expected_hash = supplied_env.get("METADATA_EXPECTED_HASH") or runtime.get("metadata_expected_hash")
    catalog = Catalog.load(source, expected_hash=expected_hash) if source is not None else Catalog.synthetic()
    try:
        policy = load_source_policy(settings, runtime)
        plan = load_document_plan(settings)
    except (ValueError, TypeError, KeyError):
        raise ServiceError("CONFIG_INVALID", "비공개 실행 설정을 확인해 주세요.", 503, False) from None
    authorization = ProcessingAuthorization(settings, runtime, catalog, policy)
    models = build_models(settings, runtime, authorization, **({"env": env} if env is not None else {}))
    db = Database(settings.data_dir / "warranty.sqlite3")
    db.initialize()
    with db.transaction() as conn:
        for item in catalog.items:
            conn.execute("INSERT OR IGNORE INTO items(item_id,name,provider) VALUES(?,?,?)",
                         (item["item_id"], item["name"], item["provider"]))
        for source_id, entry in catalog.sources.items():
            conn.execute("INSERT OR IGNORE INTO sources(source_id,canonical_url,title) VALUES(?,?,?)",
                         (source_id, entry.get("url") or entry.get("source_url") or entry.get("canonical_url", ""), entry.get("title", source_id)))
    collection, retrieval = runtime["collection"], runtime["retrieval"]
    try:
        rag = RagService(catalog, db, settings, model=models.chat, embeddings=models.embedding,
            metadata_embeddings=getattr(models, "metadata_embedding", None),
            document_item_ids=getattr(models, "document_item_ids", None),
            source_policy=policy, fetcher=SafeFetcher(policy) if collection["network_enabled"] else None,
            index_actor=collection["manager_actor"], chunk_size=retrieval["chunk_size"],
            chunk_overlap=retrieval["chunk_overlap"], default_top_k=retrieval["top_k"],
            default_strategy=retrieval["strategy"], mmr_fetch_k=retrieval["fetch_k"],
            mmr_lambda_mult=retrieval["lambda_mult"])
    except Exception:
        db.close()
        for model in (models.chat, models.embedding):
            close = getattr(model, "close", None)
            if callable(close):
                close()
        raise
    authorization.registry, authorization.vectors = rag.registry, rag.vectors
    if collection["enabled"]:
        rag.collector = Collector(rag.registry, manager_actor=collection["manager_actor"])

    def validate_review(*args, **kwargs):
        validator = getattr(rag, "validate_review", None)
        if validator is None:
            raise ServiceError("APPLICABILITY_UNKNOWN", "보증 검토에 필요한 공식 근거가 준비되지 않았습니다.")
        return validator(*args, **kwargs)

    trades = TradeService(db, review_validator=validate_review, review_guard=rag.review_guard,
                         max_settlement_attempts=runtime["settlement"]["max_attempts"])
    def manager_check(actor):
        try:
            policy.require_capability(actor, "DATA_MANAGER")
            return True
        except ServiceError:
            return False
    jobs = DocumentJobQueue(db, catalog, rag.run_prepare, manager_check=manager_check,
                            auto_start=collection["worker_enabled"] and start_document_worker)
    rag.document_jobs = jobs
    app = Application(settings, db, catalog, rag, trades, trades.worker, runtime, models,
                      authorization, jobs, plan)
    if collection["worker_enabled"] and start_document_worker:
        jobs.start()
    return app
