"""Explicit manager operations over the private document plan.

These helpers register approved targets and import bounded local files. They do
not fetch URLs, build indexes, invoke models, or run during application startup.
"""
from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
import stat

from .documents import DocumentRegistry, canonical_json
from .errors import ServiceError


MAX_BODY_BYTES = 10 * 1024 * 1024
_DOCUMENT_FIELDS = {"source_id", "title", "canonical_url", "document_type", "operator", "language", "is_synthetic"}
_TARGET_FIELDS = {"item_id", "role", "applicability", "applicability_basis", "locator", "published_on", "source_version_note"}
_ERROR_CODES = {"INVALID_INPUT", "NO_RESULTS", "PARSE_FAILED", "DOCUMENT_VERSION_CONFLICT", "CONFIG_INVALID",
                "DOCUMENT_BODY_UNAVAILABLE", "DOCUMENT_BODY_TOO_LARGE", "DOCUMENT_BODY_INVALID", "INTERNAL_ERROR"}


def _invalid(code="CONFIG_INVALID"):
    return ServiceError(code, "비공개 문서 계획과 로컬 자료를 확인해 주세요.", 422, False)


def _permission_error(exc):
    if isinstance(exc, ServiceError) and (exc.status in {401, 403} or exc.code in {"SOURCE_BLOCKED", "FORBIDDEN"}):
        code = "FORBIDDEN" if exc.code == "FORBIDDEN" else "SOURCE_BLOCKED"
        raise ServiceError(code, "문서 처리에 필요한 현재 권한을 확인해 주세요.", 403, False) from None


def _scope(target):
    return {"applicability": target["applicability"], "published_on": target.get("published_on"),
            "source_version_note": target.get("source_version_note", "")}


def _body_path(settings, value, document_type):
    if not isinstance(value, str) or not value.strip():
        raise _invalid()
    root = Path(settings.data_dir).resolve()
    path = Path(value)
    path = (root / path).resolve() if not path.is_absolute() else path.resolve()
    suffixes = {"HTML": {".html", ".htm"}, "PDF": {".pdf"}}[document_type]
    if root not in path.parents or path.suffix.lower() not in suffixes:
        raise _invalid("DOCUMENT_BODY_INVALID")
    return path


def _prepare(app, actor, *, importing):
    registry = app.rag.registry
    registry.policy.require_capability(actor, "DATA_MANAGER")
    plan = deepcopy(app.document_plan)
    if not isinstance(plan, list) or len(plan) > 2000:
        raise _invalid()
    prepared, known_scopes = [], {}
    for document in plan:
        if (not isinstance(document, dict) or set(document) - (_DOCUMENT_FIELDS | {"targets", "body_path"})
                or not {"source_id", "title", "canonical_url", "document_type", "targets"} <= set(document)
                or not isinstance(document["document_type"], str) or document["document_type"] not in {"HTML", "PDF"}
                or not isinstance(document["targets"], list) or not document["targets"]):
            raise _invalid()
        for field, maximum in (("source_id", 128), ("title", 500), ("canonical_url", 4096)):
            if not isinstance(document[field], str) or not document[field].strip() or len(document[field]) > maximum:
                raise _invalid()
        for field, default, maximum in (("operator", "", 500), ("language", "ko", 50)):
            if not isinstance(document.get(field, default), str) or len(document.get(field, default)) > maximum:
                raise _invalid()
        if type(document.get("is_synthetic", False)) is not bool:
            raise _invalid()
        for target in document["targets"]:
            if (not isinstance(target, dict) or set(target) - _TARGET_FIELDS
                    or not {"item_id", "role", "applicability", "applicability_basis"} <= set(target)):
                raise _invalid()
            app.catalog.get(target["item_id"])
            DocumentRegistry._validate_association(target["role"], target["applicability_basis"], target.get("locator", ""))
            DocumentRegistry._validate_scope(**_scope(target))
        # A single imported body has exactly one immutable applicability/version
        # record. Every target completed from it must identify the same record.
        if importing and "body_path" in document:
            scopes = {canonical_json(_scope(target)) for target in document["targets"]}
            if len(scopes) != 1:
                raise _invalid("DOCUMENT_VERSION_CONFLICT")
            path = _body_path(app.settings, document["body_path"], document["document_type"])
            identity = (document["source_id"], document["canonical_url"], document["document_type"], str(path))
            scope = next(iter(scopes))
            if identity in known_scopes and known_scopes[identity] != scope:
                raise _invalid("DOCUMENT_VERSION_CONFLICT")
            known_scopes[identity] = scope
        else:
            path = None
        prepared.append({"registration": {key: document[key] for key in _DOCUMENT_FIELDS if key in document},
                         "targets": document["targets"], "path": path})
    # All source actions are preflighted before registering a target or reading
    # any body; policy.read_guard held by the caller fences later revocation.
    for entry in prepared:
        registration = entry["registration"]
        source_id = registration["source_id"]
        registry.policy.require(source_id, "view_link", actor=actor)
        source = registry.policy.source(source_id)
        urls = {source.get("url"), source.get("canonical_url"), *source.get("approved_urls", [])}
        if registration["canonical_url"] not in urls:
            raise ServiceError("SOURCE_BLOCKED", "문서 처리에 필요한 현재 권한을 확인해 주세요.", 403, False)
        if importing and entry["path"] is not None:
            for action in ("store_body", "extract"):
                registry.policy.require(source_id, action, actor=actor)
    return prepared


def _register(registry, actor, entry):
    registered = registry.register_document(actor, **entry["registration"])
    document_id = registered["document_id"]
    target_ids = []
    for target in entry["targets"]:
        arguments = {key: value for key, value in target.items() if key != "item_id"}
        result = registry.register_target(actor, target["item_id"], document_id, **arguments)
        target_ids.append(result["target_id"])
    return {"source_id": entry["registration"]["source_id"], "document_id": document_id,
            "target_ids": list(dict.fromkeys(target_ids)), "status": "REGISTERED"}


def _read_body(app, actor, entry):
    registry, policy = app.rag.registry, app.rag.registry.policy
    policy.require_capability(actor, "DATA_MANAGER")
    for action in ("store_body", "extract"):
        policy.require(entry["registration"]["source_id"], action, actor=actor)
    try:
        root = Path(app.settings.data_dir).resolve()
        path = entry["path"].resolve(strict=True)
        if root not in path.parents or path != entry["path"]:
            raise _invalid("DOCUMENT_BODY_INVALID")
        before = path.stat()
        if not stat.S_ISREG(before.st_mode):
            raise _invalid("DOCUMENT_BODY_INVALID")
        if before.st_size > MAX_BODY_BYTES:
            raise _invalid("DOCUMENT_BODY_TOO_LARGE")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(path, flags), "rb") as stream:
            actual = os.fstat(stream.fileno())
            if (not stat.S_ISREG(actual.st_mode) or actual.st_size > MAX_BODY_BYTES
                    or path.resolve(strict=True) != path or (actual.st_dev, actual.st_ino) != (before.st_dev, before.st_ino)):
                raise _invalid("DOCUMENT_BODY_INVALID")
            body = stream.read(MAX_BODY_BYTES + 1)
        if len(body) > MAX_BODY_BYTES:
            raise _invalid("DOCUMENT_BODY_TOO_LARGE")
        if not body:
            raise _invalid("PARSE_FAILED")
        return body
    except OSError:
        raise _invalid("DOCUMENT_BODY_UNAVAILABLE") from None


def _finish(rows, identities):
    errors = sum(row["status"] == "ERROR" for row in rows)
    counts = {"plan_entry_count": len(rows), "document_count": len(set(identities)),
              "registered_count": len({row["document_id"] for row in rows if row["document_id"] is not None}),
              "target_count": len({value for row in rows for value in row["target_ids"]}),
              "imported_count": len({row["document_id"] for row in rows if row["status"] == "IMPORTED"}),
              "imported_version_count": len({row["version_id"] for row in rows if row["status"] == "IMPORTED"}),
              "skipped_count": len({identity for row, identity in zip(rows, identities) if row["status"] == "SKIPPED"}),
              "error_count": len({identity for row, identity in zip(rows, identities) if row["status"] == "ERROR"}),
              "skipped_entry_count": sum(row["status"] == "SKIPPED" for row in rows), "error_entry_count": errors}
    return {"status": ("PARTIAL" if errors < len(rows) else "FAILED") if errors else "COMPLETED",
            "documents": rows, "counts": counts}


def _run(app, actor, *, importing):
    registry = app.rag.registry
    rows = []
    try:
        with registry.policy.read_guard():
            prepared = _prepare(app, actor, importing=importing)
            for entry in prepared:
                row = {"source_id": entry["registration"]["source_id"], "document_id": None,
                       "target_ids": [], "status": "ERROR"}
                try:
                    row = _register(registry, actor, entry)
                    if importing:
                        if entry["path"] is None:
                            row["status"] = "SKIPPED"
                        else:
                            body = _read_body(app, actor, entry)
                            version = registry.ingest_bytes(actor, row["document_id"], body, **_scope(entry["targets"][0]))
                            row["version_id"] = version["version_id"]
                            for target_id in row["target_ids"]:
                                registry.complete_target(actor, target_id, row["version_id"])
                            row["status"] = "IMPORTED"
                except ServiceError as exc:
                    _permission_error(exc)
                    row.update(status="ERROR", error_code=exc.code if exc.code in _ERROR_CODES else "INTERNAL_ERROR")
                except Exception:
                    row.update(status="ERROR", error_code="INTERNAL_ERROR")
                rows.append(row)
    except ServiceError as exc:
        _permission_error(exc)
        raise ServiceError(exc.code if exc.code in _ERROR_CODES else "CONFIG_INVALID",
                           "비공개 문서 계획과 로컬 자료를 확인해 주세요.", 422, False) from None
    except Exception:
        raise _invalid() from None
    identities = [(entry["registration"]["source_id"], entry["registration"]["canonical_url"],
                   entry["registration"]["document_type"]) for entry in prepared]
    return _finish(rows, identities)


def sync_documents(app, actor):
    """Register trusted plan documents/targets after complete permission preflight."""
    return _run(app, actor, importing=False)


def import_documents(app, actor):
    """Explicitly import approved private files and link their exact versions."""
    return _run(app, actor, importing=True)
