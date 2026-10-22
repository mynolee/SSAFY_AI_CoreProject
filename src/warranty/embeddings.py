"""Opt-in semantic embeddings with explicit server processing contexts."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import math
import os
import threading

from .errors import ServiceError
from .prompts import _official_sdk_client, _public_model_identifier, _quiet_sdk_logs


def _blocked():
    return ServiceError("MODEL_REMOTE_BLOCKED", "외부 검색 처리에 필요한 설정과 승인을 확인해 주세요.", 503)


def _invalid():
    return ServiceError("EMBEDDING_OUTPUT_INVALID", "의미 검색 응답을 확인할 수 없습니다.", 503)


class OpenAIEmbeddings:
    """One source-aware authorization scope is required for each embed call.

    contexts are trusted server records with kind/source_id/version_id/actor.
    They describe every source represented in the batch, not one record per
    string. The guard must validate current external_process authorization for
    each source/version/actor and separate approval for user question text.
    """
    is_remote = True
    is_mock = False

    def __init__(self, settings, external_process_guard=None, *, client=None, env=None,
                 requested_model_version=None):
        environment = os.environ if env is None else env
        self.enabled = settings.remote_models is True
        self._api_key = environment.get("OPENAI_API_KEY", "")
        self.model_id = environment.get("EMBEDDING_MODEL", "")
        raw_dimension = environment.get("EMBEDDING_DIMENSION", "")
        try:
            self.dimension = int(raw_dimension) if isinstance(raw_dimension, (str, int)) and not isinstance(raw_dimension, bool) else 0
        except (ValueError, TypeError):
            self.dimension = 0
        if not 1 <= self.dimension <= 8192:
            self.dimension = 0
        version = requested_model_version if requested_model_version is not None else environment.get("EMBEDDING_MODEL_VERSION")
        self._requested_version = version if _public_model_identifier(version) else None
        safe_model = self.model_id if _public_model_identifier(self.model_id) else "unconfigured"
        self.name = f"openai:{safe_model}:{self.dimension}:{self._requested_version or 'unversioned'}"
        self._guard = external_process_guard
        self._client = client
        self._owns_client = client is None
        self._client_lock = threading.Lock()
        self._contexts = ContextVar("warranty_embedding_processing_context", default=None)
        self._last_response = threading.local()

    def _configured(self):
        return (self.enabled and isinstance(self._api_key, str) and bool(self._api_key.strip())
                and _public_model_identifier(self.model_id) and self.dimension > 0 and callable(self._guard))

    def metadata(self):
        return {"provider": "openai", "model_id": self.model_id if _public_model_identifier(self.model_id) else None,
                "model_version": self._requested_version,
                "version_source": "SERVER_CONFIGURED_NOT_PROVIDER_VERIFIED" if self._requested_version else "UNKNOWN",
                "provider_reported_model": getattr(self._last_response, "model", None),
                "name": self.name, "dimension": self.dimension, "is_mock": False, "is_remote": True,
                "can_generate": bool(self._configured()), "authorization": "per_context_required",
                "generation_settings": {"dimensions": self.dimension, "encoding_format": "float"},
                "transport_settings": {"timeout_seconds": 20.0, "max_retries": 0,
                                       "follow_redirects": False, "trust_env": False}}

    @contextmanager
    def processing_context(self, contexts):
        try:
            if not isinstance(contexts, list) or not contexts or len(contexts) > 2048:
                raise ValueError("Context required")
            copied = deepcopy(contexts)
            for row in copied:
                if (not isinstance(row, dict) or set(row) != {"kind", "source_id", "version_id", "actor"}
                        or row["kind"] not in {"metadata", "document", "question"}
                        or not isinstance(row["actor"], str) or not row["actor"].strip()):
                    raise ValueError("Invalid context")
                for key in ("source_id", "version_id"):
                    if row["kind"] == "question" and row[key] is None:
                        continue
                    if not isinstance(row[key], str) or not row[key].strip() or len(row[key]) > 512:
                        raise ValueError("Invalid context")
        except Exception:
            raise _blocked() from None
        token = self._contexts.set(copied)
        try:
            yield self
        finally:
            self._contexts.reset(token)

    def close(self):
        with self._client_lock:
            if self._owns_client and self._client is not None:
                self._client.close()
                self._client = None

    def embed(self, texts: list[str]) -> list[list[float]]:
        self._last_response.model = None
        if not self._configured() or not self._contexts.get():
            raise _blocked()
        try:
            if (not isinstance(texts, list) or len(texts) > 2048
                    or any(not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > 8192 for text in texts)
                    or sum(len(text.encode("utf-8")) for text in texts) > 4_194_304):
                raise ValueError("Invalid input")
            inputs = list(texts)
        except (TypeError, ValueError, UnicodeError):
            raise ServiceError("EMBEDDING_INPUT_INVALID", "검색할 텍스트의 형식 또는 길이를 확인해 주세요.", 422) from None
        if not inputs:
            return []
        result = []
        offset = 0
        while offset < len(inputs):
            batch, byte_count = [], 0
            while offset + len(batch) < len(inputs) and len(batch) < 100:
                text = inputs[offset + len(batch)]
                size = len(text.encode("utf-8"))
                if batch and byte_count + size > 200_000:
                    break
                batch.append(text)
                byte_count += size
            try:
                # Recheck the complete immutable call scope before *each* request.
                if self._guard(deepcopy(inputs), deepcopy(self._contexts.get())) is not True:
                    raise ValueError("External processing denied")
            except Exception:
                raise _blocked() from None
            try:
                _quiet_sdk_logs()
                with self._client_lock:
                    if self._client is None:
                        self._client = _official_sdk_client(self._api_key)
                response = self._client.embeddings.create(
                    input=batch, model=self.model_id, dimensions=self.dimension, encoding_format="float")
            except Exception:
                raise ServiceError("MODEL_UNAVAILABLE", "의미 검색 요청을 처리할 수 없습니다.", 503) from None
            try:
                rows = response.data
                if not isinstance(rows, list) or len(rows) != len(batch):
                    raise ValueError("Response count")
                ordered = [None] * len(batch)
                for row in rows:
                    index, vector = row.index, row.embedding
                    if (type(index) is not int or not 0 <= index < len(batch) or ordered[index] is not None
                            or not isinstance(vector, list) or len(vector) != self.dimension
                            or any(type(value) not in (int, float) or not math.isfinite(value) for value in vector)):
                        raise ValueError("Invalid vector")
                    ordered[index] = [float(value) for value in vector]
                reported = getattr(response, "model", None)
                if not _public_model_identifier(reported) or any(row is None for row in ordered):
                    raise ValueError("Invalid response")
                self._last_response.model = reported
                result.extend(ordered)
            except (AttributeError, TypeError, ValueError, OverflowError):
                raise _invalid() from None
            offset += len(batch)
        return result
