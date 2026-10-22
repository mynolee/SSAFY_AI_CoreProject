"""Composition root shared by the local user interfaces."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
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

    def close(self):
        self.rag.close()
        self.db.close()

    def summary(self):
        return {**self.catalog.summary(), "index": self.rag.corpus_summary(),
                "simulation": True, "remote_models_enabled": self.settings.remote_models,
                "real_uploads_enabled": False}


def _runtime(settings):
    path = settings.config_dir / "runtime.json"
    if not path.exists():
        return {}
    try:
        if path.stat().st_size > 65536:
            raise ValueError()
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError()
        if "metadata_path" in value and (not isinstance(value["metadata_path"], str) or not value["metadata_path"].strip()):
            raise ValueError()
        if "metadata_expected_hash" in value and (
            not isinstance(value["metadata_expected_hash"], str)
            or not re.fullmatch(r"[a-fA-F0-9]{64}", value["metadata_expected_hash"])
        ):
            raise ValueError()
        return value
    except (OSError, ValueError):
        raise ServiceError("CONFIG_INVALID", "비공개 실행 설정을 확인해 주세요.", 503) from None


def build_application(settings: Settings | None = None, metadata_path=None) -> Application:
    """Load configured facts, use synthetic facts only when no input was configured."""
    os.environ["ANONYMIZED_TELEMETRY"] = "false"
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    from .catalog import Catalog
    from .db import Database
    from .rag import RagService
    from .transactions import TradeService

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
    source = metadata_path if metadata_path is not None else os.environ.get("METADATA_PATH") or runtime.get("metadata_path")
    expected_hash = os.environ.get("METADATA_EXPECTED_HASH") or runtime.get("metadata_expected_hash")
    catalog = Catalog.load(source, expected_hash=expected_hash) if source is not None else Catalog.synthetic()
    db = Database(settings.data_dir / "warranty.sqlite3")
    db.initialize()
    with db.transaction() as conn:
        for item in catalog.items:
            conn.execute("INSERT OR IGNORE INTO items(item_id,name,provider) VALUES(?,?,?)",
                         (item["item_id"], item["name"], item["provider"]))
        for source_id, entry in catalog.sources.items():
            conn.execute("INSERT OR IGNORE INTO sources(source_id,canonical_url,title) VALUES(?,?,?)",
                         (source_id, entry.get("url") or entry.get("source_url") or entry.get("canonical_url", ""), entry.get("title", source_id)))
    rag = RagService(catalog, db, settings)

    def validate_review(*args, **kwargs):
        validator = getattr(rag, "validate_review", None)
        if validator is None:
            raise ServiceError("APPLICABILITY_UNKNOWN", "보증 검토에 필요한 공식 근거가 준비되지 않았습니다.")
        return validator(*args, **kwargs)

    trades = TradeService(db, review_validator=validate_review)
    return Application(settings, db, catalog, rag, trades, trades.worker)
