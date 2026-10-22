"""Explicit LIVE-only GMS adapters using publicly documented request fields.

The fixed gateway and model/route combinations below were checked against the
official public GMS guides on 2026-10-22. Response parsing follows the declared
OpenAI compatibility; no successful LIVE request is implied by this module.
No client, connection, or usage store is created by constructing an adapter.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import json
import math
import os
import re
import threading
import time
from types import SimpleNamespace
import uuid

from .embeddings import OpenAIEmbeddings
from .errors import ServiceError
from .prompts import OpenAIStructuredModel, _public_model_identifier, _quiet_sdk_logs, _unique_object


GMS_BASE_URL = "https://gms.ssafy.io/gmsapi/api.openai.com/v1"
_CHAT_MODELS = {"responses": frozenset({"gpt-4.1"}),
                "chat/completions": frozenset({"gpt-4.1", "gpt-4.1-mini", "gpt-4o-mini"})}
_EMBEDDING_MODELS = frozenset({"text-embedding-3-small", "text-embedding-3-large"})
_PURPOSES = frozenset({"evaluation", "verification", "final_verification", "demo", "embedding_preparation"})
_QUOTA_ERRORS = frozenset({"PROCESSING_APPROVAL_REQUIRED", "USAGE_LIMIT_REACHED"})
_CREDIT_ERRORS = frozenset({"LIVE_BUDGET_UNCONFIRMED", "CREDIT_LIMIT_REACHED", "LIVE_REQUEST_CONFLICT",
                          "LIVE_BUDGET_UNAVAILABLE", "LIVE_PURPOSE_REQUIRED", "LIVE_MODEL_IDENTITY_MISMATCH"})
_RESPONSE_LIMIT = 2_097_152


def _blocked():
    return ServiceError("MODEL_REMOTE_BLOCKED", "승인된 LIVE 실행 범위와 GMS 설정을 확인해 주세요.", 503)


def _unavailable():
    return ServiceError("MODEL_UNAVAILABLE", "GMS 요청 또는 응답을 확인할 수 없습니다.", 503)


def _approval_error(code="PROCESSING_APPROVAL_REQUIRED"):
    return ServiceError(code, "승인된 처리 예산을 확인해 주세요.", 429 if code == "USAGE_LIMIT_REACHED" else 503)


def _credit_error(code="LIVE_BUDGET_UNCONFIRMED"):
    return ServiceError(code, "확인된 LIVE 크레딧 예산이 필요합니다.",
                        429 if code == "CREDIT_LIMIT_REACHED" else 409 if code == "LIVE_REQUEST_CONFLICT"
                        else 403 if code == "LIVE_PURPOSE_REQUIRED" else 503)


def _max_tokens(value):
    if type(value) is int:
        return value if 1 <= value <= 4096 else 0
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,3}", value):
        return int(value) if int(value) <= 4096 else 0
    return 0


def _valid_key(value):
    return (isinstance(value, str) and 1 <= len(value) <= 4096
            and value == value.strip() and all(33 <= ord(char) <= 126 for char in value))


def _normal_base(value):
    # Accept one conventional trailing slash only. Never decode or join an
    # untrusted host/path, and never use an ambient OPENAI_BASE_URL.
    return GMS_BASE_URL if value in (GMS_BASE_URL, GMS_BASE_URL + "/") else None


def _normal_route(value):
    if not isinstance(value, str):
        return None
    normalized = value[1:] if value.startswith("/") else value
    return normalized if normalized in _CHAT_MODELS else None


def _safe_json(raw):
    def invalid_constant(value):
        raise ValueError("Nonfinite JSON")
    result = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=invalid_constant)
    if not isinstance(result, dict):
        raise ValueError("Object required")
    return result


class _Gateway:
    def __init__(self, owner, *, client=None, transport=None):
        self.owner, self._client, self._transport = owner, client, transport
        self._owns_client = client is None
        self._lock = threading.Lock()
        self._error = threading.local()

    def reset(self):
        self._error.value = None

    def close(self):
        with self._lock:
            if self._owns_client and self._client is not None:
                self._client.close()
                self._client = None

    def request(self, route, body, kind):
        reserved_id, result = None, None
        try:
            # This rechecks scope/approval and current source rights for each
            # HTTP request, including each string in an embedding batch.
            self.owner._before_request()
            encoded = json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
            credit_route = "chat.completions" if route == "chat/completions" else route
            plan = self.owner._credit_budget.estimate(kind, self.owner.model_id, len(encoded),
                output_tokens_bound=self.owner.max_tokens if kind == "chat" else 0, route=credit_route)
            self.owner._usage_budget.consume(kind, len(encoded))
            request_id = str(uuid.uuid4())
            reservation = self.owner._credit_budget.reserve(request_id, self.owner._live_scope.get()[0],
                plan["estimated_upper_credits"], kind=kind, model=self.owner.model_id, input_bytes=len(encoded),
                input_tokens_bound=plan["input_tokens_bound"], output_tokens_bound=plan["output_tokens_bound"], route=credit_route)
            if not isinstance(reservation, dict) or reservation.get("replayed") is not False:
                raise _credit_error()
            reserved_id = request_id
            self.owner._before_request(reserved=True)
            _quiet_sdk_logs()
            with self._lock:
                if self._client is None:
                    import httpx
                    transport = self._transport
                    if transport is None:
                        transport = httpx.HTTPTransport(retries=0, trust_env=False)
                    self._client = httpx.Client(transport=transport, timeout=httpx.Timeout(20.0, connect=5.0),
                                                follow_redirects=False, trust_env=False)
            self.owner._before_request(reserved=True)
            deadline = time.monotonic() + 20.0
            with self._client.stream("POST", GMS_BASE_URL + "/" + route, content=encoded,
                                     headers={"Authorization": "Bearer " + self.owner._api_key,
                                              "Content-Type": "application/json", "Accept": "application/json", "Accept-Encoding": "identity"},
                                     follow_redirects=False) as response:
                if response.status_code != 200:
                    raise ValueError("Unsuccessful provider request")
                if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                    raise ValueError("Unexpected content type")
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise ValueError("Unexpected response encoding")
                declared = response.headers.get("content-length")
                if declared is not None and (not declared.isdecimal() or int(declared) > _RESPONSE_LIMIT):
                    raise ValueError("Response too large")
                chunks, size = [], 0
                for chunk in response.iter_bytes(chunk_size=65_536):
                    if time.monotonic() > deadline:
                        raise ValueError("Response deadline")
                    size += len(chunk)
                    if size > _RESPONSE_LIMIT:
                        raise ValueError("Response too large")
                    chunks.append(chunk)
                result = _safe_json(b"".join(chunks))
                if result.get("error") is not None:
                    raise ValueError("Provider error")
                self.owner._before_request(reserved=True)
                reported = result.get("model")
                allowed = self.owner._credit_budget.allowed_reported_models(kind, self.owner.model_id, credit_route)
                if not isinstance(reported, str) or reported not in allowed:
                    self.owner._credit_budget.freeze(reason="MODEL_IDENTITY_MISMATCH")
                    raise ServiceError("LIVE_MODEL_IDENTITY_MISMATCH", "GMS 응답 모델을 확인해야 합니다.", 503)
                return result
        except ServiceError as exc:
            if exc.code in _QUOTA_ERRORS | _CREDIT_ERRORS or exc.code == "MODEL_REMOTE_BLOCKED":
                self._error.value = exc
            raise
        except Exception:
            raise _unavailable() from None
        finally:
            if reserved_id is not None:
                try:
                    # Public guides do not document actual credit receipts.
                    # Keep the full reservation; actual tokens are a distinct
                    # provider-reported measurement, never a money estimate.
                    usage = self._usage(result, route) if result is not None else None
                    self.owner._credit_budget.settle(reserved_id, actual_credits=None, usage=usage)
                except ServiceError as exc:
                    self._error.value = exc if exc.code in _CREDIT_ERRORS else _credit_error()
                    raise self._error.value from None
                except Exception:
                    self._error.value = _credit_error()
                    raise self._error.value from None

    @staticmethod
    def _usage(result, route):
        usage = result.get("usage")
        if not isinstance(usage, dict):
            return None
        if route == "responses":
            input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
        elif route == "embeddings":
            input_tokens, output_tokens = usage.get("prompt_tokens"), 0
        else:
            input_tokens, output_tokens = usage.get("prompt_tokens"), usage.get("completion_tokens")
        if any(type(value) is not int or not 0 <= value <= 10**12 for value in (input_tokens, output_tokens)):
            return None
        return {"input_tokens": input_tokens, "output_tokens": output_tokens}


class _LiveMixin:
    def _init_live(self, environment, usage_budget, credit_budget, client, transport):
        self._base = _normal_base(environment.get("GMS_BASE_URL"))
        self._usage_budget = usage_budget
        self._credit_budget = credit_budget
        self._live_scope = ContextVar("warranty_gms_live_scope", default=None)
        self._gateway = _Gateway(self, client=client, transport=transport)

    def _budget_ready(self):
        try:
            return self._usage_budget is not None and self._usage_budget.ready is True
        except Exception:
            return False

    def _approval_matches(self, approval_id):
        try:
            return (self._usage_budget.approval_matches(approval_id) is True
                    and self._credit_budget.approval_matches(approval_id) is True)
        except Exception:
            return False

    @contextmanager
    def live_execution(self, purpose, approval_id):
        """Trusted explicit entry point; UI/processing_context grants no scope.

        This is an execution boundary, not an authorization issuer. The ID must
        match an independently configured persistent usage approval.
        """
        if (not isinstance(purpose, str) or purpose not in _PURPOSES or not isinstance(approval_id, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", approval_id) is None):
            raise ServiceError("INVALID_INPUT", "LIVE 실행 목적과 승인 식별자를 확인해 주세요.", 400)
        if not self._approval_matches(approval_id):
            raise _approval_error()
        token = self._live_scope.set((purpose, approval_id))
        try:
            yield self
        finally:
            self._live_scope.reset(token)

    def _require_execution(self, *, reserved=False):
        scope = self._live_scope.get()
        if scope is None or not self._provider_ready():
            raise _blocked()
        if not self._approval_matches(scope[1]):
            raise _approval_error()
        if reserved:
            # The already granted request can consume the final available
            # slot. Check revocation/integrity, without demanding a second
            # unused slot or credit reservation for that same request.
            try:
                state = self._credit_budget.snapshot()
            except Exception:
                raise _credit_error("LIVE_BUDGET_UNAVAILABLE") from None
            reason = state.get("reason_code")
            if reason is not None and reason != "CREDIT_LIMIT_REACHED":
                raise _credit_error(reason if reason in _CREDIT_ERRORS else "LIVE_BUDGET_UNAVAILABLE")
            return
        if not self._budget_ready():
            try:
                code = self._usage_budget.snapshot().get("reason_code")
            except Exception:
                code = None
            raise _approval_error(code if code in _QUOTA_ERRORS else "PROCESSING_APPROVAL_REQUIRED")
        if not self._credit_ready():
            try:
                code = self._credit_budget.snapshot().get("reason_code")
            except Exception:
                code = None
            raise _credit_error(code if code in _CREDIT_ERRORS else "LIVE_BUDGET_UNCONFIRMED")

    def _credit_ready(self):
        try:
            return self._credit_budget is not None and self._credit_budget.ready is True
        except Exception:
            return False

    def _configured(self):
        scope = self._live_scope.get()
        return bool(scope is not None and self._provider_ready() and self._budget_ready() and self._credit_ready()
                    and self._approval_matches(scope[1]))

    def _before_request(self, *, reserved=False):
        self._require_execution(reserved=reserved)

    def _raise_preserved(self):
        error = getattr(self._gateway._error, "value", None)
        if error is not None:
            raise error from None

    def _metadata(self, result):
        result.update(provider="gms", can_generate=bool(self._configured()),
                      configuration_ready=bool(self._provider_ready() and self._budget_ready() and self._credit_ready()),
                      authorization="explicit_live_scope_and_per_context_required",
                      gateway_base_url=self._base, live_execution_required=True,
                      live_verified=False, retention_policy="NOT_VERIFIED",
                      response_format_basis="DECLARED_OPENAI_COMPATIBILITY_NOT_LIVE_VERIFIED")
        result["model_id"] = self.model_id if self._known_model() else None
        if result.get("provider_reported_model") == self._api_key:
            result["provider_reported_model"] = None
        return result

    def close(self):
        self._gateway.close()


class _ChatShim:
    def __init__(self, owner):
        self.owner = owner
        self.responses = self

    def create(self, **request):
        owner = self.owner
        if owner.route == "responses":
            # The GMS guide demonstrates only model + string input here.
            envelope = {"policy": _safe_json(request["instructions"]), **_safe_json(request["input"])}
            body = {"model": request["model"], "input": json.dumps(envelope, ensure_ascii=False, allow_nan=False)}
        else:
            body = {"model": request["model"],
                    "messages": [{"role": "developer" if owner.model_id == "gpt-4o-mini" else "system", "content": request["instructions"]},
                                 {"role": "user", "content": request["input"]}],
                    "max_tokens": owner.max_tokens, "stream": False}
        result = owner._gateway.request(owner.route, body, "chat")
        if owner.route == "chat/completions":
            choices = result.get("choices")
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError("Invalid choices")
            choice = choices[0]
            if not isinstance(choice, dict) or type(choice.get("index")) is not int or choice["index"] != 0 or choice.get("finish_reason") != "stop":
                raise ValueError("Incomplete choice")
            message = choice.get("message")
            if (not isinstance(message, dict) or message.get("role") != "assistant"
                    or message.get("tool_calls") or message.get("function_call") or message.get("refusal")):
                raise ValueError("Unexpected response")
            text = message.get("content")
        else:
            if result.get("status") != "completed" or result.get("incomplete_details") is not None:
                raise ValueError("Incomplete response")
            output = result.get("output")
            if not isinstance(output, list):
                raise ValueError("Output required")
            parts = []
            for entry in output:
                if not isinstance(entry, dict) or entry.get("type") not in {"message", "reasoning"}:
                    raise ValueError("Unexpected response")
                if entry["type"] == "reasoning":
                    continue
                if entry.get("role") != "assistant" or entry.get("status", "completed") != "completed" or not isinstance(entry.get("content"), list):
                    raise ValueError("Incomplete message")
                for part in entry["content"]:
                    if not isinstance(part, dict) or part.get("type") != "output_text" or not isinstance(part.get("text"), str):
                        raise ValueError("Unexpected response")
                    parts.append(part["text"])
            text = "".join(parts)
        if not _public_model_identifier(result.get("model")):
            raise ValueError("Model required")
        return SimpleNamespace(output_text=text, model=result["model"], status="completed", output=[], error=None,
                               incomplete_details=None)


class GMSStructuredModel(_LiveMixin, OpenAIStructuredModel):
    def __init__(self, settings, external_process_guard=None, *, client=None, transport=None, env=None,
                 requested_model_version=None, route=None, usage_budget=None, credit_budget=None, max_tokens=None):
        environment = os.environ if env is None else env
        mapped = {"OPENAI_API_KEY": environment.get("GMS_API_KEY", ""), "CHAT_MODEL": environment.get("GMS_CHAT_MODEL", ""),
                  "CHAT_MODEL_VERSION": environment.get("GMS_CHAT_MODEL_VERSION")}
        self.route = _normal_route(route if route is not None else environment.get("GMS_CHAT_ROUTE"))
        self.max_tokens = _max_tokens(max_tokens if max_tokens is not None else environment.get("GMS_MAX_OUTPUT_TOKENS"))
        OpenAIStructuredModel.__init__(self, settings, external_process_guard, client=_ChatShim(self), env=mapped,
                                       requested_model_version=requested_model_version)
        self._original_guard = external_process_guard
        self._validated_contexts = ContextVar("warranty_gms_validated_chat_contexts", default=None)
        self._guard = self._capture_guard if callable(external_process_guard) else None
        self._init_live(environment, usage_budget, credit_budget, client, transport)
        if self._requested_version == self._api_key:
            self._requested_version = None

    def _known_model(self):
        return isinstance(self.model_id, str) and self.model_id in _CHAT_MODELS.get(self.route, ())

    def _provider_ready(self):
        return bool(self.enabled and self._base and _valid_key(self._api_key) and callable(self._guard)
                    and self._known_model() and self.route == "chat/completions" and self.max_tokens > 0)

    def _capture_guard(self, kind, context_kind, context):
        result = self._original_guard(kind, context_kind, deepcopy(context))
        records = self._validated_contexts.get()
        if result is True and records is not None:
            records.append((kind, context_kind, deepcopy(context)))
        return result

    def _before_request(self, *, reserved=False):
        super()._before_request(reserved=reserved)
        try:
            records = self._validated_contexts.get()
            if not records:
                raise ValueError("Validated source context required")
            for kind, context_kind, context in records:
                if self._original_guard(kind, context_kind, deepcopy(context)) is not True:
                    raise ValueError("Context denied")
        except Exception:
            raise _blocked() from None

    def generate(self, kind, prompt):
        self._last_response.model = None
        self._gateway.reset()
        self._require_execution()
        token = self._validated_contexts.set([])
        try:
            return super().generate(kind, prompt)
        except ServiceError:
            self._raise_preserved()
            raise
        finally:
            self._validated_contexts.reset(token)

    def metadata(self):
        result = self._metadata(super().metadata())
        result.update(route=self.route, structured_output="PROMPT_SCHEMA_AND_SERVER_VALIDATION",
                      generation_settings=({"max_tokens": self.max_tokens, "stream": False} if self.route == "chat/completions" else {}),
                      wire_fields=(["model", "messages", "max_tokens", "stream"] if self.route == "chat/completions"
                                   else ["model", "input"] if self.route == "responses" else []))
        return result


class _EmbeddingShim:
    def __init__(self, owner):
        self.owner = owner
        self.embeddings = self

    def create(self, **request):
        rows, reported = [], None
        for index, text in enumerate(request["input"]):
            owner, claim = self.owner, None
            try:
                owner._before_request()
                vector = owner._embedding_cache.get(owner._cache_key, owner.dimension, text)
                if vector is None:
                    claim = owner._embedding_cache.claim(owner._cache_key, owner.dimension, text)
                    vector = owner._embedding_cache.get(owner._cache_key, owner.dimension, text)
                    if vector is None:
                        result = owner._gateway.request("embeddings", {"model": request["model"], "input": text}, "embedding")
                        data, model = result.get("data"), result.get("model")
                        if (not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict)
                                or type(data[0].get("index")) is not int or data[0]["index"] != 0
                                or not _public_model_identifier(model) or (reported is not None and model != reported)):
                            raise ValueError("Invalid embedding response")
                        reported, vector = model, data[0].get("embedding")
                        owner._reported_http.model = model
                        self._validate(vector)
                        owner._before_request(reserved=True)
                        owner._embedding_cache.put(owner._cache_key, owner.dimension, text, vector, claim_token=claim)
                self._validate(vector)
                owner._before_request(reserved=True)
                rows.append(SimpleNamespace(index=index, embedding=vector))
            except ServiceError as exc:
                if exc.code.startswith("EMBEDDING_CACHE_") or exc.code == "MODEL_REMOTE_BLOCKED":
                    owner._gateway._error.value = exc
                raise
            finally:
                if claim is not None:
                    try:
                        owner._embedding_cache.release(owner._cache_key, owner.dimension, text, claim)
                    except ServiceError as exc:
                        owner._gateway._error.value = exc
                        raise
        # Cache stores vectors without assuming a provider-reported snapshot.
        # The configured model is the only truthful fallback for an all-hit call.
        reported = reported or self.owner.model_id
        return SimpleNamespace(data=rows, model=reported)

    def _validate(self, vector):
        if (not isinstance(vector, list) or len(vector) != self.owner.dimension
                or any(type(value) not in (int, float) or not math.isfinite(value) for value in vector)):
            raise ValueError("Invalid vector")


class GMSEmbeddings(_LiveMixin, OpenAIEmbeddings):
    def __init__(self, settings, external_process_guard=None, *, client=None, transport=None, env=None,
                 requested_model_version=None, usage_budget=None, credit_budget=None, embedding_cache=None):
        environment = os.environ if env is None else env
        mapped = {"OPENAI_API_KEY": environment.get("GMS_API_KEY", ""), "EMBEDDING_MODEL": environment.get("GMS_EMBEDDING_MODEL", ""),
                  "EMBEDDING_DIMENSION": environment.get("GMS_EMBEDDING_DIMENSION", ""),
                  "EMBEDDING_MODEL_VERSION": environment.get("GMS_EMBEDDING_MODEL_VERSION")}
        OpenAIEmbeddings.__init__(self, settings, external_process_guard, client=_EmbeddingShim(self), env=mapped,
                                  requested_model_version=requested_model_version)
        if self._requested_version == self._api_key:
            self._requested_version = None
        self.name = f"gms:{self.model_id if self._known_model() else 'unconfigured'}:{self.dimension}:{self._requested_version or 'unversioned'}"
        self._init_live(environment, usage_budget, credit_budget, client, transport)
        self._embedding_cache = embedding_cache
        self._cache_key = "gms:" + json.dumps({"base": self._base, "route": "embeddings", "model": self.model_id,
            "version": self._requested_version, "dimension": self.dimension}, sort_keys=True, separators=(",", ":"))
        self._full_inputs = ContextVar("warranty_gms_embedding_inputs", default=None)
        self._reported_http = threading.local()

    def _known_model(self):
        return isinstance(self.model_id, str) and self.model_id in _EMBEDDING_MODELS

    def _provider_ready(self):
        return bool(self.enabled and self._base and _valid_key(self._api_key) and callable(self._guard)
                    and self._known_model() and self.dimension > 0 and self._embedding_cache is not None)

    def _before_request(self, *, reserved=False):
        super()._before_request(reserved=reserved)
        try:
            if self._guard(deepcopy(self._full_inputs.get()), deepcopy(self._contexts.get())) is not True:
                raise ValueError("Context denied")
        except Exception:
            raise _blocked() from None

    def embed(self, texts):
        self._last_response.model = None
        self._reported_http.model = None
        self._gateway.reset()
        self._require_execution()
        token = self._full_inputs.set(deepcopy(texts))
        try:
            result = super().embed(texts)
            self._last_response.model = self._reported_http.model
            return result
        except ServiceError:
            self._raise_preserved()
            raise
        finally:
            self._full_inputs.reset(token)

    def metadata(self):
        result = self._metadata(super().metadata())
        result.update(route="embeddings", name=self.name, generation_settings={},
                      wire_fields=["model", "input"], wire_input_type="string", dimension_source="SERVER_EXPECTED_OUTPUT_ONLY")
        return result
