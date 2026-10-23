"""Server-side public input selection. A content manifest never grants LIVE authority."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re

from .errors import ServiceError
from .public_protocol import PublicProtocol, public_source_sha256


def _blocked():
    return ServiceError("MODEL_REMOTE_BLOCKED", "승인된 공개 질문과 문서 범위를 확인해 주세요.", 503, False)


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _hash_list(value, *, empty=False):
    return (type(value) is list and (bool(value) or empty) and len(value) <= 10000
            and all(type(row) is str and re.fullmatch(r"[a-f0-9]{64}", row) for row in value)
            and len(value) == len(set(value)))


@dataclass(frozen=True)
class PublicBundle:
    _value: str
    fingerprint: str
    path: Path | None = None
    file_sha256: str | None = None

    @property
    def value(self):
        return json.loads(self._value)

    @property
    def protocol(self):
        return self.value["protocol"]

    @property
    def item_ids(self):
        return tuple(self.value["item_ids"])

    @property
    def document_sha256s(self):
        return tuple(self.value["embedding_document_sha256s"])

    def current(self):
        if self.path is None:
            return True
        try:
            for part in (self.path, *self.path.parents):
                if part.is_symlink() or (part.exists() and getattr(part.lstat(), "st_file_attributes", 0) & 0x400):
                    return False
            return (self.path.is_file() and self.path.stat().st_size <= 1048576
                    and hashlib.sha256(self.path.read_bytes()).hexdigest() == self.file_sha256)
        except OSError:
            return False


def public_bundle(value):
    """Validate an in-memory content bundle, without execution, file or key access."""
    try:
        if (type(value) is not dict or set(value) != {"schema_version", "protocol", "item_ids", "sources", "embedding_document_sha256s"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1):
            raise ValueError()
        value = deepcopy(value)
        ids = value["item_ids"]
        if (type(ids) is not list or not ids or len(ids) > 1000 or len(set(ids)) != len(ids)
                or any(type(row) is not str or not row.strip() or len(row) > 256 for row in ids)):
            raise ValueError()
        protocol = value["protocol"]
        PublicProtocol(protocol)
        if not _hash_list(protocol.get("question_sha256s")) or not _hash_list(value["embedding_document_sha256s"]):
            raise ValueError()
        sources = value["sources"]
        if type(sources) is not list or not sources or len(sources) > 1000:
            raise ValueError()
        seen = set()
        for source in sources:
            required = {"source_id", "version_id", "title", "url", "excerpt"}
            if (type(source) is not dict or not required <= set(source)
                    or set(source) - required - {"page", "section"}
                    or any(type(source[key]) is not str or not source[key].strip() for key in required)):
                raise ValueError()
            if any(len(source[key]) > 512 for key in ("source_id", "version_id")):
                raise ValueError()
            if "page" in source and source["page"] is not None and (type(source["page"]) is not int or source["page"] < 1):
                raise ValueError()
            if "section" in source and source["section"] is not None and type(source["section"]) is not str:
                raise ValueError()
            public = {key: source[key] for key in ("title", "url", "excerpt")}
            if public_source_sha256(public) not in protocol["source_sha256s"]:
                raise ValueError()
            identity = (source["source_id"], source["version_id"], public_source_sha256(public))
            if identity in seen:
                raise ValueError()
            seen.add(identity)
        # A document embedding is limited to exactly selected, reviewed excerpts.
        if not set(value["embedding_document_sha256s"]) <= {_hash(source["excerpt"]) for source in sources}:
            raise ValueError()
        encoded = _canonical(value)
        if len(encoded.encode("utf-8")) > 1048576:
            raise ValueError()
        return PublicBundle(encoded, _hash(encoded))
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError, ServiceError):
        raise ServiceError("CONFIG_INVALID", "공개 처리 범위 파일을 확인해 주세요.", 503, False) from None


def load_public_bundle(settings, path):
    if path is None:
        return None
    try:
        root = Path(settings.config_dir).resolve()
        candidate = Path(path)
        candidate = candidate if candidate.is_absolute() else root / candidate
        for part in (candidate, *candidate.parents):
            if part.is_symlink() or (part.exists() and getattr(part.lstat(), "st_file_attributes", 0) & 0x400):
                raise ValueError()
        candidate = candidate.resolve()
        if root not in candidate.parents or candidate.suffix.lower() != ".json" or candidate.stat().st_size > 1048576:
            raise ValueError()
        raw = candidate.read_bytes()
        def unique(pairs):
            result = {}
            for key, item in pairs:
                if key in result:
                    raise ValueError()
                result[key] = item
            return result
        value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        bundle = public_bundle(value)
        return PublicBundle(bundle._value, bundle.fingerprint, candidate, hashlib.sha256(raw).hexdigest())
    except (OSError, TypeError, ValueError, UnicodeError, RecursionError):
        raise ServiceError("CONFIG_INVALID", "공개 처리 범위 파일을 확인해 주세요.", 503, False) from None


class PublicProcessingContext:
    def __init__(self, bundle, authorization):
        self.bundle, self.authorization = bundle, authorization
        self.item_ids = bundle.item_ids
        self.document_sha256s = bundle.document_sha256s
        self.fingerprint = bundle.fingerprint

    def _scope(self):
        scope = self.authorization._request.get()
        if (not self.bundle.current() or not scope or scope["item_id"] not in self.item_ids):
            raise _blocked()
        return scope

    def _question(self, question):
        return (type(question) is str and _hash(question) in self.bundle.protocol["question_sha256s"])

    def model_guard(self, kind, context_kind, context):
        try:
            self._scope()
            if kind not in {"query", "selection", "answer"}:
                return False
            if context_kind == "question":
                if type(context) is not dict or set(context) != {"question"} or not self._question(context["question"]):
                    return False
            elif context_kind not in {"candidate", "evidence"}:
                return False
            elif context_kind == "evidence" and context.get("source_type") == "metadata":
                return False
            return self.authorization.model_guard(kind, context_kind, context)
        except (ServiceError, KeyError, TypeError, ValueError):
            return False

    def embedding_guard(self, texts, contexts):
        try:
            if (not self.bundle.current() or type(texts) is not list or not texts
                    or type(contexts) is not list or not contexts or any(type(text) is not str for text in texts)):
                return False
            kinds = {row.get("kind") for row in contexts}
            if kinds == {"question"}:
                if any(not self._question(text) for text in texts):
                    return False
            elif kinds == {"document"}:
                pairs = {(row["source_id"], row["version_id"]) for row in self.bundle.value["sources"]}
                if (any(_hash(text) not in self.document_sha256s for text in texts)
                        or any((row.get("source_id"), row.get("version_id")) not in pairs for row in contexts)):
                    return False
            else:
                return False
            return self.authorization.embedding_guard(texts, contexts)
        except (ServiceError, KeyError, TypeError, ValueError):
            return False

    def source_resolver(self, kind, variables):
        scope = self._scope()
        if not self._question(variables.get("question")):
            raise _blocked()
        if kind == "query":
            return []
        selected = []
        # Public titles are manifest-bound display text. The base guard checks
        # the full original local row, including its potentially private title;
        # source/version/URL/body bindings connect that row to this projection.
        approved = self.bundle.value["sources"]
        if kind == "answer":
            for entry in variables.get("evidence", []):
                if entry.get("source_type") == "metadata":
                    continue
                for source in approved:
                    if (entry.get("source_id") == source["source_id"] and entry.get("version_id") == source["version_id"]
                            and entry.get("url") == source["url"]
                            and entry.get("text") == source["excerpt"]
                            and all(source.get(key) is None or source[key] == entry.get(key) for key in ("page", "section"))):
                        if not self.model_guard(kind, "evidence", entry):
                            raise _blocked()
                        selected.append({"local_id": entry["evidence_id"], **{key: source[key] for key in ("title", "url", "excerpt")}})
                        break
        elif kind == "selection":
            rows = {row["id"]: row for row in self.authorization._candidates(scope["item_id"])}
            for candidate in variables.get("candidates", []):
                row = rows.get(candidate.get("id"))
                if row is None:
                    raise _blocked()
                for source in approved:
                    if (row.get("source_id") == source["source_id"] and row.get("version_id") == source["version_id"]
                            and row.get("canonical_url") == source["url"]):
                        if not self.model_guard(kind, "candidate", candidate):
                            raise _blocked()
                        sections = self.authorization.registry.sections(row, scope["actor"], action="external_process")
                        if not any(source["excerpt"] in part["text"] and all(source.get(key) is None or source[key] == part.get(key)
                                   for key in ("page", "section")) for part in sections):
                            raise _blocked()
                        selected.append({"local_id": candidate["id"], **{key: source[key] for key in ("title", "url", "excerpt")}})
                        break
        else:
            raise _blocked()
        return selected

    resolve_sources = source_resolver

    def summary(self):
        return {"configured": True, "manifest_current": self.bundle.current(), "manifest_sha256": self.fingerprint, "item_count": len(self.item_ids),
                "question_count": len(self.bundle.protocol["question_sha256s"]),
                "source_count": len(self.bundle.value["sources"]), "embedding_document_count": len(self.document_sha256s),
                "metadata_processing": "LOCAL_ONLY", "creates_transmission_authority": False}


class PublicProtocolRequired:
    """Factory placeholder; a missing public content contract cannot use legacy wire."""
    is_remote = True
    is_mock = False
    format_repair_attempts = 0
    model_id = None

    def __init__(self, provider, *, dimension=64):
        self.provider, self.dimension = provider, dimension or 64
        self.name = provider + ":PUBLIC-PROTOCOL-REQUIRED"

    def generate(self, *args, **kwargs):
        raise _blocked()

    def embed(self, *args, **kwargs):
        raise _blocked()

    def metadata(self):
        return {"provider": self.provider, "model_id": None, "mode": "BLOCKED", "is_remote": True,
                "can_generate": False, "configuration_ready": False, "public_protocol_required": True,
                "live_verified": False, "reason_code": "PUBLIC_PROTOCOL_REQUIRED"}

    def close(self):
        pass
