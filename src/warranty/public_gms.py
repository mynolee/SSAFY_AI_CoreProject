"""Explicit LIVE adapter for the separately authored public wire protocol.

The private PromptStore envelope is a local application interface only. Neither
its policy nor item/applicability/review metadata is projected onto the wire.
The existing GMS gateway remains the sole transport and budget boundary.
"""
from __future__ import annotations

from contextvars import ContextVar
from copy import deepcopy
import hashlib
import json
import re

from .errors import ServiceError
from .gms import GMSStructuredModel, _blocked, _safe_json
from .prompts import _error, validate_output
from .public_protocol import MODEL, ROUTE, PublicProtocol


_VARIABLES = {
    "query": {"item", "question", "applicability"},
    "selection": {"item", "question", "applicability", "candidates"},
    "answer": {"item", "question", "applicability", "evidence", "missing_information", "conflicts"},
}


class GMSPublicStructuredModel(GMSStructuredModel):
    """Hash-bound public requests with local source/response conversion.

    ``public_source_resolver(kind, variables)`` is a trusted server callback,
    not a request/user supplied value. It must resolve the current registry
    version, public excerpt and source rights. The existing context guard is
    independently checked on the question and every transmitted source.
    A manifest never opens LIVE scope or grants source processing permission.
    """

    public_protocol_only = True
    format_repair_attempts = 0

    def __init__(self, settings, external_process_guard=None, *, public_manifest=None,
                 public_source_resolver=None, client=None, transport=None, env=None,
                 requested_model_version=None, route=None, usage_budget=None,
                 credit_budget=None, max_tokens=None):
        # This adapter never falls back to ambient account/environment values.
        environment = {} if env is None else env
        super().__init__(settings, external_process_guard, client=client, transport=transport,
                         env=environment, requested_model_version=requested_model_version,
                         route=route, usage_budget=usage_budget, credit_budget=credit_budget,
                         max_tokens=max_tokens)
        self._public_protocol = None
        self.cache_identity = "gms-public-unconfigured"
        self._public_source_resolver = public_source_resolver
        self._public_request = ContextVar("warranty_public_gms_request", default=None)
        try:
            protocol = PublicProtocol(deepcopy(public_manifest))
            if (self.model_id != MODEL or self.route != ROUTE
                    or self.max_tokens != protocol.max_output_tokens):
                raise ValueError("Public model/route/output cap mismatch")
            self._public_protocol = protocol
            canonical = json.dumps(public_manifest, ensure_ascii=False, allow_nan=False,
                                   sort_keys=True, separators=(",", ":")).encode("utf-8")
            self.cache_identity = "gms-public-v1:" + hashlib.sha256(canonical).hexdigest()
        except (TypeError, ValueError, KeyError, RecursionError):
            # Configuration readiness is distinct from runtime authorization.
            # Invalid public configuration must not fall back to private wire.
            pass

    def _provider_ready(self):
        return bool(super()._provider_ready() and self._public_protocol is not None
                    and callable(self._public_source_resolver))

    @staticmethod
    def _variables(kind, prompt):
        if (kind not in _VARIABLES or not isinstance(prompt, str)
                or len(prompt.encode("utf-8")) > 1_048_576):
            raise ValueError("Invalid local input")
        envelope = _safe_json(prompt)
        if set(envelope) != {"policy", "input"} or not isinstance(envelope["policy"], dict):
            raise ValueError("Local envelope required; repairs are disabled")
        value = envelope["input"]
        if (not isinstance(value, dict) or set(value) != _VARIABLES[kind]
                or not isinstance(value["item"], dict) or not isinstance(value["applicability"], dict)):
            raise ValueError("Invalid local variables")
        for name in ("candidates", "evidence"):
            if name in value and (not isinstance(value[name], list) or len(value[name]) > 100
                                  or any(not isinstance(row, dict) for row in value[name])):
                raise ValueError("Invalid local context")
        for name in ("missing_information", "conflicts"):
            if name in value and (not isinstance(value[name], list) or len(value[name]) > 100
                                  or any(not isinstance(row, str) for row in value[name])):
                raise ValueError("Invalid local context")
        return deepcopy(value)

    def _prepare(self, kind, variables):
        sources = self._public_source_resolver(kind, deepcopy(variables))
        # Query has no document/candidate context to authorize source content.
        if kind == "query" and sources != []:
            raise ValueError("Query source context unavailable")
        prepared = self._public_protocol.prepare(kind, deepcopy(variables), deepcopy(sources))
        body = prepared.body
        encoded = json.dumps(body, ensure_ascii=False, allow_nan=False,
                             separators=(",", ":")).encode("utf-8")
        if (encoded != prepared.wire_bytes or body.get("model") != self.model_id
                or body.get("max_tokens") != self.max_tokens or body.get("store") is not False
                or body.get("stream") is not False):
            raise ValueError("Public wire mismatch")
        return prepared

    @staticmethod
    def _contexts(kind, variables, prepared):
        records = [("question", {"question": variables["question"]})]
        local_ids = set(prepared.label_to_local_id.values())
        if kind in {"selection", "answer"}:
            field, identity, context = (("candidates", "id", "candidate") if kind == "selection"
                                        else ("evidence", "evidence_id", "evidence"))
            records.extend((context, row) for row in variables[field] if row.get(identity) in local_ids)
        return records

    def _before_request(self, *, reserved=False):
        # Includes explicit LIVE scope, persistent usage/credit authorization,
        # revocation and the existing per-source guard, on each gateway check.
        super()._before_request(reserved=reserved)
        try:
            state = self._public_request.get()
            if state is None:
                raise ValueError("Public request context required")
            kind, variables, expected = state
            current = self._prepare(kind, variables)
            if (current.wire_bytes != expected.wire_bytes or current.kind != expected.kind
                    or current.question_sha256 != expected.question_sha256
                    or current.source_sha256s != expected.source_sha256s
                    or current.max_output_tokens != expected.max_output_tokens
                    or dict(current.label_to_local_id) != dict(expected.label_to_local_id)
                    or dict(current.label_to_number) != dict(expected.label_to_number)):
                raise ValueError("Public source or request context changed")
        except Exception:
            raise _blocked() from None

    @staticmethod
    def _response_text(result):
        choices = result.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise ValueError("One completed assistant choice required")
        choice = choices[0]
        if type(choice.get("index")) is not int or choice["index"] != 0 or choice.get("finish_reason") != "stop":
            raise ValueError("Incomplete response")
        message = choice.get("message")
        if (not isinstance(message, dict) or message.get("role") != "assistant" or message.get("tool_calls")
                or message.get("function_call") or message.get("refusal")):
            raise ValueError("Unexpected response")
        return message.get("content")

    def _decode_response(self, prepared, response_text):
        """Recover only absent citation syntax, without another model request."""
        try:
            return self._public_protocol.decode(prepared, response_text), 0
        except ValueError as exc:
            if prepared.kind != "answer" or str(exc) != "PUBLIC_RESPONSE_MARKER_INVALID":
                raise
        value = _safe_json(response_text)
        text, citations = value.get("answer"), value.get("citations")
        # Existing, partial, bare or parenthetical label references are never
        # corrected. A recovery only renders the model's already valid labels.
        if (type(text) is not str or "[" in text or "]" in text
                or re.search(r"(?<![A-Za-z0-9_])S[0-9]+(?![A-Za-z0-9_])", text, re.IGNORECASE)
                or type(citations) is not list or not citations
                or any(type(label) is not str or label not in prepared.label_to_number for label in citations)
                or len(citations) != len(set(citations))):
            raise ValueError("PUBLIC_RESPONSE_MARKER_INVALID")
        value["answer"] = text + " " + " ".join("[" + label + "]" for label in citations)
        # Run the complete strict decoder again, including checks that follow
        # marker validation, such as condition shape and source membership.
        return self._public_protocol.decode(prepared, json.dumps(value, ensure_ascii=False, allow_nan=False)), 1

    def generate(self, kind, prompt):
        self._last_response.model = None
        self._last_response.local_citation_marker_recovery = 0
        self._gateway.reset()
        self._require_execution()
        context_token = self._validated_contexts.set([])
        request_token = None
        try:
            try:
                variables = self._variables(kind, prompt)
                prepared = self._prepare(kind, variables)
                request_token = self._public_request.set((kind, deepcopy(variables), prepared))
                for context_kind, context in self._contexts(kind, variables, prepared):
                    if self._guard(kind, context_kind, deepcopy(context)) is not True:
                        raise ValueError("Unapproved context")
            except Exception:
                raise _blocked() from None
            result = self._gateway.request(self.route, prepared.body, "chat")
            try:
                answer, marker_recovery = self._decode_response(prepared, self._response_text(result))
                answer = validate_output(kind, answer,
                    allowed_candidate_ids=[row.get("id") for row in variables.get("candidates", [])],
                    allowed_evidence_ids=[row.get("evidence_id") for row in variables.get("evidence", [])])
                self._last_response.model = result["model"]
                self._last_response.local_citation_marker_recovery = marker_recovery
                return answer
            except (TypeError, ValueError, KeyError, RecursionError, ServiceError):
                raise _error("MODEL_OUTPUT_INVALID") from None
        except ServiceError:
            self._raise_preserved()
            raise
        finally:
            if request_token is not None:
                self._public_request.reset(request_token)
            self._validated_contexts.reset(context_token)

    def metadata(self):
        result = super().metadata()
        result.update(public_protocol_only=True, format_repair_attempts=0,
                      structured_output="PUBLIC_PROTOCOL_AND_LOCAL_VALIDATION",
                      private_policy_transmitted=False,
                      local_citation_marker_recovery=getattr(
                          self._last_response, "local_citation_marker_recovery", 0))
        return result
