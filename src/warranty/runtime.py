"""Private runtime configuration and explicit processing authorization."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import json
import hashlib
import math
import os
from pathlib import Path
import re
from types import SimpleNamespace

from .errors import ServiceError


def _invalid():
    raise ServiceError("CONFIG_INVALID", "비공개 실행 설정을 확인해 주세요.", 503, False)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _invalid()
        result[key] = value
    return result


def read_private_json(path, *, default=None, max_bytes=1048576):
    path = Path(path)
    if not path.exists():
        return deepcopy(default)
    try:
        if not path.is_file() or path.stat().st_size > max_bytes:
            _invalid()
        return json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_object,
                          parse_constant=lambda value: _invalid())
    except (OSError, UnicodeError, ValueError, RecursionError):
        _invalid()


def _section(value, defaults):
    if not isinstance(value, dict) or set(value) - set(defaults):
        _invalid()
    return {**defaults, **value}


def _text(value, *, optional=False, limit=200):
    if optional and value is None:
        return
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 for c in value):
        _invalid()


def load_runtime(settings):
    value = read_private_json(settings.config_dir / "runtime.json", default={}, max_bytes=65536)
    allowed = {"schema_version", "metadata_path", "metadata_expected_hash", "models", "collection", "retrieval", "settlement"}
    if not isinstance(value, dict) or set(value) - allowed or type(value.get("schema_version", 1)) is not int or value.get("schema_version", 1) != 1:
        _invalid()
    if "metadata_path" in value:
        _text(value["metadata_path"], limit=4096)
    if "metadata_expected_hash" in value and (not isinstance(value["metadata_expected_hash"], str)
            or not re.fullmatch(r"[a-fA-F0-9]{64}", value["metadata_expected_hash"])):
        _invalid()
    models = _section(value.get("models", {}), {
        "chat_provider": "mock", "chat_model": None, "chat_model_version": None,
        "embedding_provider": "mock", "embedding_model": None, "embedding_model_version": None,
        "embedding_dimension": None, "external_processing_approved": False,
        "allow_question_processing": False, "gms_base_url": None, "gms_chat_route": None,
        "gms_max_output_tokens": None,
        "public_protocol_path": None,
        "usage_approval_id": None,
        "usage_limits": {"chat_requests": 0, "embedding_requests": 0, "input_bytes": 0}})
    collection = _section(value.get("collection", {}), {
        "enabled": False, "network_enabled": False, "manager_actor": None,
        "worker_enabled": True})
    retrieval = _section(value.get("retrieval", {}), {
        "chunk_size": 1000, "chunk_overlap": 200, "top_k": 6, "strategy": "similarity",
        "fetch_k": 30, "lambda_mult": 0.5})
    settlement = _section(value.get("settlement", {}), {
        "max_attempts": getattr(settings, "max_settlement_attempts", 3)})
    if type(settlement["max_attempts"]) is not int or not 1 <= settlement["max_attempts"] <= 10:
        _invalid()
    for field in ("chat_provider", "embedding_provider"):
        if not isinstance(models[field], str) or models[field] not in {"mock", "openai", "gms"}:
            _invalid()
    for field in ("chat_model", "chat_model_version", "embedding_model", "embedding_model_version"):
        _text(models[field], optional=True)
    _text(models["gms_base_url"], optional=True, limit=200)
    _text(models["gms_chat_route"], optional=True, limit=50)
    _text(models["public_protocol_path"], optional=True, limit=4096)
    if models["gms_base_url"] not in {None, "https://gms.ssafy.io/gmsapi/api.openai.com/v1", "https://gms.ssafy.io/gmsapi/api.openai.com/v1/"}:
        _invalid()
    if models["gms_chat_route"] not in {None, "responses", "chat/completions"}:
        _invalid()
    maximum_output = models["gms_max_output_tokens"]
    if maximum_output is not None and (type(maximum_output) is not int or not 1 <= maximum_output <= 4096):
        _invalid()
    approval = models["usage_approval_id"]
    if approval is not None and (not isinstance(approval, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", approval)):
        _invalid()
    limits = models["usage_limits"]
    if not isinstance(limits, dict) or set(limits) != {"chat_requests", "embedding_requests", "input_bytes"}:
        _invalid()
    for key, maximum in (("chat_requests", 1000000), ("embedding_requests", 1000000), ("input_bytes", 1000000000000)):
        if type(limits[key]) is not int or not 0 <= limits[key] <= maximum:
            _invalid()
    dimension = models["embedding_dimension"]
    if dimension is not None and (type(dimension) is not int or not 1 <= dimension <= 8192):
        _invalid()
    for field in ("external_processing_approved", "allow_question_processing"):
        if type(models[field]) is not bool:
            _invalid()
    for field in ("enabled", "network_enabled", "worker_enabled"):
        if type(collection[field]) is not bool:
            _invalid()
    _text(collection["manager_actor"], optional=True, limit=128)
    if collection["network_enabled"] and not collection["enabled"]:
        _invalid()
    for field, lower, upper in (("chunk_size", 100, 10000), ("chunk_overlap", 0, 9999),
                                ("top_k", 1, 30), ("fetch_k", 1, 100)):
        if type(retrieval[field]) is not int or not lower <= retrieval[field] <= upper:
            _invalid()
    if (retrieval["chunk_overlap"] >= retrieval["chunk_size"] or retrieval["fetch_k"] < retrieval["top_k"]
            or not isinstance(retrieval["strategy"], str) or retrieval["strategy"] not in {"similarity", "mmr"}
            or isinstance(retrieval["lambda_mult"], bool)
            or not isinstance(retrieval["lambda_mult"], (int, float))
            or not math.isfinite(retrieval["lambda_mult"]) or not 0 <= retrieval["lambda_mult"] <= 1):
        _invalid()
    return {**value, "models": models, "collection": collection, "retrieval": retrieval, "settlement": settlement}


def load_source_policy(settings, runtime):
    from .security import SourcePolicy
    value = read_private_json(settings.config_dir / "source_policy.json", default=None)
    if value is None:
        return SourcePolicy(network_enabled=False)
    return SourcePolicy.from_config(value, network_enabled=runtime["collection"]["network_enabled"])


_SECRETS = re.compile(r"(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)")
_PRIVATE_QUESTION = re.compile(r"(?:[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|\b0\d{1,2}[- ]?\d{3,4}[- ]?\d{4}\b|\b\d{13,}\b)")


class ProcessingAuthorization:
    def __init__(self, settings, runtime, catalog, policy):
        self.settings, self.runtime, self.catalog, self.policy = settings, runtime, catalog, policy
        self.registry = None
        self.vectors = None
        self._request = ContextVar("model_request_authorization", default=None)

    @contextmanager
    def scope(self, actor, item_id):
        if not isinstance(actor, str) or not actor or len(actor) > 128:
            raise ServiceError("MODEL_REMOTE_BLOCKED", "외부 처리 범위를 확인해 주세요.", 503, False)
        self.catalog.get(item_id)
        token = self._request.set({"actor": actor, "item_id": item_id})
        try:
            yield
        finally:
            self._request.reset(token)

    def _enabled(self):
        return self.settings.remote_models and self.runtime["models"]["external_processing_approved"]

    def _source(self, source_id, actor, version_id=None):
        self.policy.require(source_id, "external_process", actor=actor, version_id=version_id)

    def _candidates(self, item_id):
        if self.registry is None:
            return []
        rows = self.registry.candidates(item_id)
        pending = getattr(self.registry, "registered_candidates", None)
        if callable(pending):
            rows += pending(item_id)
        return rows

    def model_guard(self, kind, context_kind, context):
        try:
            scope = self._request.get()
            if not self._enabled() or not scope or not isinstance(context, dict):
                return False
            serialized = json.dumps(context, ensure_ascii=False, allow_nan=False)
            if _SECRETS.search(serialized):
                return False
            actor, item_id = scope["actor"], scope["item_id"]
            item = self.catalog.get(item_id)
            if context_kind in {"question", "applicability", "review_context"}:
                if not self.runtime["models"]["allow_question_processing"]:
                    return False
                from .documents import APPLICABILITY_FIELDS
                allowed = {"question": {"question"}, "applicability": set(APPLICABILITY_FIELDS),
                    "review_context": {"missing_information", "conflicts"}}[context_kind]
                if set(context) - allowed:
                    return False
                return not _PRIVATE_QUESTION.search(serialized)
            if context_kind == "item":
                # The no-RAG evaluation deliberately sends an empty item.
                if not context:
                    return True
                if context.get("item_id") != item_id or context != item:
                    return False
                self._source(item.get("source_id", ""), actor, self.catalog.dataset_hash)
                return True
            candidates = self._candidates(item_id)
            if context_kind == "candidate":
                row = next((entry for entry in candidates if entry["id"] == context.get("id")), None)
                if row is None:
                    return False
                expected = {key: row[key] for key in ("id", "title", "role", "own_document")}
                if context != expected:
                    return False
                self._source(row["source_id"], actor, row.get("version_id"))
                return True
            if context_kind == "evidence":
                if context.get("item_id") != item_id:
                    return False
                if context.get("source_type") == "metadata":
                    expected = {"item_id": item_id, "source_id": item.get("source_id"),
                        "source_type": "metadata", "text": item.get("index_text"),
                        "evidence_id": "meta-" + item_id, "title": item.get("name", item.get("product_name", item_id)),
                        "type": "METADATA", "role": "METADATA", "url": item.get("source_url"),
                        "section": item.get("source_locator")}
                    if set(context) - (set(expected) | {"revision", "number"}) or any(
                            context[key] != expected[key] for key in context if key in expected):
                        return False
                    if (context.get("source_id") != item.get("source_id")
                            or context.get("text") != item.get("index_text")):
                        return False
                    self._source(item.get("source_id", ""), actor, self.catalog.dataset_hash)
                    return True
                row = next((entry for entry in candidates if entry.get("version_id") == context.get("version_id")
                    and entry["role"] == context.get("role") and entry["source_id"] == context.get("source_id")), None)
                if row is None or context.get("url") != row["canonical_url"]:
                    return False
                allowed = {"text", "evidence_id", "item_id", "version_id", "document_version_id", "document_id",
                    "source_id", "title", "url", "source_url", "type", "role", "source_type", "revision",
                    "offset", "page", "section", "score", "number"}
                expected = {"document_id": row["document_id"], "document_version_id": row["version_id"],
                    "title": row["title"], "source_url": row["canonical_url"], "type": row["document_type"],
                    "source_type": "synthetic_document" if json.loads(row["review_json"]).get("is_synthetic", False) else "official_document"}
                if set(context) - allowed or any(context[key] != expected[key] for key in context if key in expected):
                    return False
                text = context.get("text")
                offset = context.get("offset")
                if (not isinstance(text, str) or not text or
                        offset is not None and (type(offset) is not int or offset < 0)):
                    return False
                sections = self.registry.sections(row, actor, action="external_process")
                if not any(all(context.get(key) == section.get(key) for key in ("page", "section"))
                    and (text in section["text"] if offset is None else
                         text == section["text"][offset:offset + len(text)]) for section in sections):
                    return False
                if self.vectors is not None:
                    revision, evidence_id = context.get("revision"), context.get("evidence_id")
                    if not isinstance(revision, str) or not isinstance(evidence_id, str):
                        return False
                    stored = self.vectors.docs.get(ids=[self.vectors._physical_id(revision, evidence_id)],
                                                  include=["documents", "metadatas"])
                    if stored["documents"] != [text] or not stored["metadatas"]:
                        return False
                    trusted = stored["metadatas"][0]
                    if any(context.get(key) != value for key, value in trusted.items()):
                        return False
                self._source(row["source_id"], actor, row["version_id"])
                return True
            return False
        except (ServiceError, KeyError, TypeError, ValueError):
            return False

    def embedding_guard(self, texts, contexts):
        try:
            if not self._enabled() or not contexts or any(_SECRETS.search(text) for text in texts):
                return False
            kinds = {row.get("kind") for row in contexts}
            if len(kinds) != 1 or len({row.get("actor") for row in contexts}) != 1:
                return False
            for row in contexts:
                actor = row.get("actor")
                if not isinstance(actor, str) or not actor:
                    return False
                if row["kind"] == "question":
                    if not self.runtime["models"]["allow_question_processing"] or any(_PRIVATE_QUESTION.search(text) for text in texts):
                        return False
                elif row["kind"] in {"metadata", "document"}:
                    self._source(row["source_id"], actor, row["version_id"])
                else:
                    return False
            if kinds == {"metadata"}:
                sources = {row["source_id"] for row in contexts}
                approved = {item.get("index_text") for item in self.catalog.items if item.get("source_id") in sources}
                if any(row["version_id"] != self.catalog.dataset_hash for row in contexts) or any(text not in approved for text in texts):
                    return False
            if kinds == {"document"}:
                if self.registry is None:
                    return False
                candidates = self.registry.candidates()
                approved_sections = []
                for row in contexts:
                    candidate = next((entry for entry in candidates if entry["version_id"] == row["version_id"]
                                      and entry["source_id"] == row["source_id"]), None)
                    if candidate is None:
                        return False
                    approved_sections.extend(part["text"] for part in self.registry.sections(candidate, row["actor"], action="external_process"))
                if any(not any(text in section for section in approved_sections) for text in texts):
                    return False
            return True
        except (ServiceError, KeyError, TypeError, ValueError):
            return False


class AuthorizedChatModel:
    is_mock = False
    is_remote = True

    def __init__(self, adapter, authorization):
        self.adapter, self.authorization = adapter, authorization
        self.model_id = adapter.model_id
        self.format_repair_attempts = getattr(adapter, "format_repair_attempts", 1)
        self.public_protocol_only = getattr(adapter, "public_protocol_only", False)
        self.cache_identity = getattr(adapter, "cache_identity", self.model_id)

    def processing_context(self, actor, item_id):
        return self.authorization.scope(actor, item_id)

    def generate(self, kind, prompt):
        return self.adapter.generate(kind, prompt)

    def metadata(self):
        return self.adapter.metadata()

    def close(self):
        close = getattr(self.adapter, "close", None)
        if callable(close):
            close()


def build_models(settings, runtime, authorization, *, env=None):
    from .prompts import MockStructuredModel, OpenAIStructuredModel
    from .vectorstore import MockEmbeddings
    values = runtime["models"]
    supplied_env = os.environ if env is None else env
    from .public_runtime import load_public_bundle, PublicProcessingContext, PublicProtocolRequired
    public_bundle = load_public_bundle(settings, values.get("public_protocol_path"))
    public_context = PublicProcessingContext(public_bundle, authorization) if public_bundle else None
    if public_context:
        for item_id in public_context.item_ids:
            authorization.catalog.get(item_id)
    environment = {key: supplied_env.get(key, "") for key in
                   ("OPENAI_API_KEY", "CHAT_MODEL", "CHAT_MODEL_VERSION", "EMBEDDING_MODEL", "EMBEDDING_MODEL_VERSION", "EMBEDDING_DIMENSION")}
    for option, variable in (("chat_model", "CHAT_MODEL"), ("chat_model_version", "CHAT_MODEL_VERSION"),
                             ("embedding_model", "EMBEDDING_MODEL"), ("embedding_model_version", "EMBEDDING_MODEL_VERSION"),
                             ("embedding_dimension", "EMBEDDING_DIMENSION")):
        if values[option] is not None:
            environment[variable] = str(values[option])
    chat = MockStructuredModel()
    budget = credit_budget = embedding_cache = None
    if values["chat_provider"] == "openai":
        # Product private PromptStore policies have no outbound authorization.
        chat = AuthorizedChatModel(PublicProtocolRequired("openai"), authorization)
    embedding = MockEmbeddings()
    if values["embedding_provider"] == "openai":
        from .embeddings import OpenAIEmbeddings
        try:
            dimension = int(environment["EMBEDDING_DIMENSION"])
            if not 1 <= dimension <= 8192:
                raise ValueError()
        except (TypeError, ValueError):
            _invalid()
        embedding = PublicProtocolRequired("openai", dimension=dimension)
    if "gms" in {values["chat_provider"], values["embedding_provider"]}:
        from .gms import GMSStructuredModel, GMSEmbeddings
        from .usage import UsageBudget
        from .live_budget import CreditBudget
        from .embedding_cache import EmbeddingCache
        gms_environment = {key: supplied_env.get(key, "") for key in ("GMS_API_KEY", "GMS_BASE_URL",
            "GMS_CHAT_MODEL", "GMS_CHAT_MODEL_VERSION", "GMS_CHAT_ROUTE", "GMS_EMBEDDING_MODEL",
            "GMS_EMBEDDING_MODEL_VERSION", "GMS_EMBEDDING_DIMENSION", "GMS_MAX_OUTPUT_TOKENS")}
        for option, variable in (("chat_model", "GMS_CHAT_MODEL"), ("chat_model_version", "GMS_CHAT_MODEL_VERSION"),
                ("embedding_model", "GMS_EMBEDDING_MODEL"), ("embedding_model_version", "GMS_EMBEDDING_MODEL_VERSION"),
                ("embedding_dimension", "GMS_EMBEDDING_DIMENSION"), ("gms_base_url", "GMS_BASE_URL"),
                ("gms_chat_route", "GMS_CHAT_ROUTE"), ("gms_max_output_tokens", "GMS_MAX_OUTPUT_TOKENS")):
            if values[option] is not None:
                gms_environment[variable] = str(values[option])
        identity = {key: value for key, value in gms_environment.items() if key != "GMS_API_KEY"}
        if public_bundle:
            identity["public_bundle_sha256"] = public_bundle.fingerprint
        fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()
        budget = UsageBudget(settings.data_dir / "model_usage.sqlite3", values["usage_approval_id"] or "",
                             values["usage_limits"], context_fingerprint=fingerprint)
        profile = read_private_json(settings.config_dir / "live_budget.json", default=None)
        credit_budget = CreditBudget(settings.data_dir / "model_credits.sqlite3", values["usage_approval_id"] or "",
                                     profile, context_fingerprint=fingerprint)
        embedding_cache = (EmbeddingCache(settings.data_dir / "embedding_cache.sqlite3")
                           if public_context else None)
        if values["chat_provider"] == "gms":
            if public_context:
                from .public_gms import GMSPublicStructuredModel
                adapter = GMSPublicStructuredModel(settings, public_context.model_guard,
                    public_manifest=public_bundle.protocol, public_source_resolver=public_context.source_resolver,
                    env=gms_environment, usage_budget=budget, credit_budget=credit_budget)
            else:
                adapter = PublicProtocolRequired("gms")
            chat = AuthorizedChatModel(adapter, authorization)
        if values["embedding_provider"] == "gms":
            if public_context:
                embedding = GMSEmbeddings(settings, public_context.embedding_guard,
                    env=gms_environment, usage_budget=budget, credit_budget=credit_budget,
                    embedding_cache=embedding_cache)
                embedding.public_protocol_only = True
                embedding.approved_document_sha256s = public_context.document_sha256s
            else:
                embedding = PublicProtocolRequired("gms", dimension=values["embedding_dimension"])
    if public_context and getattr(embedding, "is_mock", False):
        # Local rehearsal uses the same selected document scope as the public
        # adapter, while catalog metadata remains on a separate local adapter.
        embedding.public_protocol_only = True
        embedding.approved_document_sha256s = public_context.document_sha256s
    return SimpleNamespace(chat=chat, embedding=embedding, usage_budget=budget,
                           credit_budget=credit_budget, embedding_cache=embedding_cache,
                           public_context=public_context,
                           metadata_embedding=MockEmbeddings() if public_context else None,
                           document_item_ids=list(public_context.item_ids) if public_context else None)


def load_document_plan(settings):
    from .documents import DocumentRegistry, ROLES
    value = read_private_json(settings.config_dir / "document_plan.json", default={"schema_version": 1, "documents": []}, max_bytes=2097152)
    if (not isinstance(value, dict) or set(value) != {"schema_version", "documents"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or not isinstance(value["documents"], list) or len(value["documents"]) > 2000):
        _invalid()
    allowed = {"source_id", "title", "canonical_url", "document_type", "operator", "language", "is_synthetic", "targets", "body_path"}
    targets = {"item_id", "role", "applicability", "applicability_basis", "locator", "published_on", "source_version_note"}
    for document in value["documents"]:
        if (not isinstance(document, dict) or set(document) - allowed
                or not {"source_id", "title", "canonical_url", "document_type", "targets"} <= set(document)
                or not isinstance(document["document_type"], str) or document["document_type"] not in {"HTML", "PDF"}
                or not isinstance(document["targets"], list) or not document["targets"]):
            _invalid()
        for field, limit in (("source_id", 128), ("title", 500), ("canonical_url", 4096)):
            _text(document[field], limit=limit)
        for field, limit in (("operator", 500), ("language", 50)):
            if field in document and (not isinstance(document[field], str) or len(document[field]) > limit):
                _invalid()
        if type(document.get("is_synthetic", False)) is not bool:
            _invalid()
        for target in document["targets"]:
            if (not isinstance(target, dict) or set(target) - targets
                    or not {"item_id", "role", "applicability", "applicability_basis"} <= set(target)):
                _invalid()
            for field in ("item_id", "role", "applicability_basis"):
                _text(target[field], limit=2000)
            if target["role"] not in ROLES:
                _invalid()
            try:
                DocumentRegistry._validate_scope(target["applicability"], target.get("published_on"), target.get("source_version_note", ""))
                DocumentRegistry._validate_association(target["role"], target["applicability_basis"], target.get("locator", ""))
            except ServiceError:
                _invalid()
        if "body_path" in document:
            _text(document["body_path"], limit=4096)
            path = Path(document["body_path"])
            path = (settings.data_dir / path).resolve() if not path.is_absolute() else path.resolve()
            if settings.data_dir not in path.parents or path.suffix.lower() not in {".html", ".htm", ".pdf"}:
                _invalid()
            document["body_path"] = str(path)
    return value["documents"]
