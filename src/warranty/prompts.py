"""Private prompt loading and bounded validation of structured model responses.

The product contains interface schemas only. Prompt policy is supplied from the
configured private directory; the default model is an explicit offline mock.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import re
from typing import Any, Protocol

from .errors import ServiceError


_KINDS = frozenset({"query", "selection", "answer"})
_LIMIT = 131_072
_VARIABLES = {
    "query": ("item", "question", "applicability"),
    "selection": ("item", "question", "applicability", "candidates"),
    "answer": ("item", "question", "applicability", "evidence", "missing_information", "conflicts"),
}
_STR = {"type": "string", "minLength": 1, "maxLength": 16_384}
_IDS = {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 512}, "maxItems": 100, "uniqueItems": True}
_STRINGS = {"type": "array", "items": _STR, "maxItems": 100, "uniqueItems": True}


def _object(properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


OUTPUT_SCHEMAS = {
    "query": _object({
        "queries": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 512}, "maxItems": 10, "uniqueItems": True},
        "filters": {"type": "object", "additionalProperties": {"type": ["string", "number", "boolean", "null"]}, "maxProperties": 20},
        "required_document_roles": _STRINGS,
        "missing_information": _STRINGS,
    }),
    "selection": _object({
        "selected_candidate_ids": _IDS,
        "rejected": {"type": "array", "items": _object({"id": _STR, "reason": _STR}), "maxItems": 100},
        "conflicts": _STRINGS,
        "missing_information": _STRINGS,
    }),
    "answer": _object({
        "classification": {"type": "string", "enum": ["TRANSFERABLE", "NOT_TRANSFERABLE", "CONDITIONAL", "NEEDS_INFORMATION"]},
        "answer": _STR,
        "claims": {"type": "array", "items": _object({"text": _STR, "evidence_ids": _IDS}), "maxItems": 100},
        "conditions": {"type": "array", "items": _object({"text": _STR, "evidence_ids": _IDS}), "maxItems": 100},
        "missing_information": _STRINGS,
        "conflicts": _STRINGS,
        "cited_evidence_ids": _IDS,
    }),
}


def output_schema(kind: str) -> dict:
    """Return a copy so an adapter cannot weaken shared validation."""
    _check_kind(kind)
    return deepcopy(OUTPUT_SCHEMAS[kind])


def _error(code: str) -> ServiceError:
    return ServiceError(code, "구조화 응답 또는 프롬프트 설정을 확인해 주세요.", 422)


def _check_kind(kind: str) -> None:
    if not isinstance(kind, str) or kind not in _KINDS:
        raise _error("PROMPT_KIND_INVALID")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate object key")
        result[key] = value
    return result


def _parse(value: Any) -> dict:
    try:
        if isinstance(value, str):
            if len(value.encode("utf-8")) > _LIMIT:
                raise ValueError("Size limit")
            value = json.loads(value, object_pairs_hook=_unique_object,
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite number")))
        if not isinstance(value, dict):
            raise ValueError("Object required")
        # This also rejects nonserializable and nonfinite values supplied as dicts.
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > _LIMIT:
            raise ValueError("Size limit")
        return deepcopy(value)
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise _error("MODEL_OUTPUT_INVALID") from None


def _validate(value: Any, schema: dict) -> bool:
    expected = schema.get("type")
    if isinstance(expected, list):
        return any(_validate(value, {**schema, "type": item}) for item in expected)
    if expected == "null":
        return value is None
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "string":
        if not isinstance(value, str) or not value.strip():
            return False
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 16_384):
            return False
        return "enum" not in schema or value in schema["enum"]
    if expected == "array":
        if not isinstance(value, list) or len(value) > schema.get("maxItems", 100):
            return False
        if not all(_validate(item, schema["items"]) for item in value):
            return False
        if schema.get("uniqueItems"):
            serialized = [json.dumps(item, sort_keys=True, ensure_ascii=False) for item in value]
            return len(set(serialized)) == len(serialized)
        return True
    if expected == "object":
        if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
            return False
        if len(value) > schema.get("maxProperties", 100):
            return False
        props = schema.get("properties", {})
        if not set(schema.get("required", ())).issubset(value):
            return False
        additional = schema.get("additionalProperties", False)
        for key, item in value.items():
            if key in props:
                if not _validate(item, props[key]):
                    return False
            elif additional is False:
                return False
            elif isinstance(additional, dict) and not _validate(item, additional):
                return False
        return True
    return False


def validate_output(kind: str, value: Any, allowed_candidate_ids=None, allowed_evidence_ids=None) -> dict:
    """Validate shape and optional context membership; never grant domain authority.

    Semantic ID failures are final and must not be sent through format repair.
    The RAG service additionally verifies item, revision, source role, scope and
    whether the cited material supports the claim.
    """
    _check_kind(kind)
    result = _parse(value)
    if not _validate(result, OUTPUT_SCHEMAS[kind]):
        raise _error("MODEL_OUTPUT_INVALID")
    if kind == "selection":
        selected = result["selected_candidate_ids"]
        rejected = [row["id"] for row in result["rejected"]]
        if len(rejected) != len(set(rejected)) or set(selected) & set(rejected):
            raise _error("MODEL_CANDIDATE_INVALID")
        if allowed_candidate_ids is not None and not set(selected + rejected).issubset(set(allowed_candidate_ids)):
            raise _error("MODEL_CANDIDATE_INVALID")
    if kind == "answer":
        used = {eid for row in result["claims"] + result["conditions"] for eid in row["evidence_ids"]}
        cited = set(result["cited_evidence_ids"])
        if used != cited:
            raise _error("MODEL_EVIDENCE_INVALID")
        if any(not row["evidence_ids"] for row in result["claims"] + result["conditions"]):
            raise _error("MODEL_EVIDENCE_INVALID")
        if allowed_evidence_ids is not None and not cited.issubset(set(allowed_evidence_ids)):
            raise _error("MODEL_EVIDENCE_INVALID")
        if not cited and result["classification"] != "NEEDS_INFORMATION":
            raise _error("MODEL_EVIDENCE_INVALID")
    return result


class PromptStore:
    def __init__(self, config_dir):
        self.config_dir = Path(config_dir)

    def load(self, kind: str) -> dict:
        _check_kind(kind)
        try:
            path = self.config_dir / "prompts" / f"{kind}.json"
            if path.stat().st_size > _LIMIT:
                raise ValueError("Size limit")
            value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
            required = {"version", "kind", "role", "input_variables", "evidence_limits", "output_schema",
                        "missing_information_rules", "conflict_rules", "attack_rules", "template"}
            if not isinstance(value, dict) or set(value) != required or value["kind"] != kind:
                raise ValueError("Invalid prompt contract")
            if not isinstance(value["version"], str) or not re.fullmatch(r"\d+\.\d+\.\d+", value["version"]):
                raise ValueError("Version required")
            if value["input_variables"] != list(_VARIABLES[kind]) or value["output_schema"] != OUTPUT_SCHEMAS[kind]:
                raise ValueError("Schema mismatch")
            if any(not isinstance(value[key], str) or not value[key].strip() for key in ("role", "template")):
                raise ValueError("Instruction required")
            for key in ("evidence_limits", "missing_information_rules", "conflict_rules", "attack_rules"):
                if not isinstance(value[key], list) or not value[key] or not all(isinstance(row, str) and row.strip() for row in value[key]):
                    raise ValueError("Rules required")
            return value
        except (OSError, TypeError, ValueError, KeyError, RecursionError):
            raise _error("PROMPT_CONFIG_INVALID") from None

    def render(self, kind: str, variables: dict) -> str:
        policy = self.load(kind)
        if not isinstance(variables, dict) or set(variables) != set(_VARIABLES[kind]):
            raise _error("PROMPT_INPUT_INVALID")
        try:
            rendered = json.dumps({"policy": policy, "input": variables}, ensure_ascii=False, allow_nan=False)
            if len(rendered.encode("utf-8")) > 1_048_576:
                raise ValueError("Size limit")
            return rendered
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise _error("PROMPT_INPUT_INVALID") from None


class StructuredModel(Protocol):
    def generate(self, kind: str, prompt: str) -> str | dict: ...


class StructuredRunner:
    """At most two model calls: original output plus one format-only repair."""
    def __init__(self, store: PromptStore, model: StructuredModel):
        self.store, self.model = store, model
        self.last_attempts = 0

    def run(self, kind: str, variables: dict) -> dict:
        prompt = self.store.render(kind, variables)
        self.last_attempts = 0
        allowed_candidates = None
        allowed_evidence = None
        if kind == "selection":
            allowed_candidates = [row.get("id") for row in variables["candidates"] if isinstance(row, dict)]
        if kind == "answer":
            allowed_evidence = [row.get("evidence_id") for row in variables["evidence"] if isinstance(row, dict)]
        for attempt in range(2):
            self.last_attempts += 1
            try:
                raw = self.model.generate(kind, prompt)
            except ServiceError as exc:
                if exc.code == "MODEL_REMOTE_BLOCKED":
                    raise _error("MODEL_REMOTE_BLOCKED") from None
                raise _error("MODEL_UNAVAILABLE") from None
            except Exception:
                raise _error("MODEL_UNAVAILABLE") from None
            try:
                return validate_output(kind, raw, allowed_candidates, allowed_evidence)
            except ServiceError as exc:
                if exc.code != "MODEL_OUTPUT_INVALID" or attempt == 1:
                    raise
                # Do not append raw invalid output: it could contain instructions,
                # confidential values or an attacker-controlled repair request.
                envelope = json.loads(prompt)
                envelope["format_repair"] = {"attempt": 1, "code": "MODEL_OUTPUT_INVALID",
                                             "output_schema": output_schema(kind)}
                prompt = json.dumps(envelope, ensure_ascii=False, allow_nan=False)
        raise _error("MODEL_OUTPUT_INVALID")


class MockStructuredModel:
    """Deterministic local plumbing mock, with no model quality claims."""
    is_mock = True
    model_id = "deterministic-mock-v1"

    def __init__(self, *, responses=None):
        self.responses = iter(responses) if responses is not None else None
        self.calls = 0

    def generate(self, kind: str, prompt: str) -> dict | str:
        self.calls += 1
        if self.responses is not None:
            return deepcopy(next(self.responses))
        inputs = json.loads(prompt)["input"]
        if kind == "query":
            item = inputs["item"]
            parts = [item.get(key) for key in ("provider", "name", "item_name", "product_name", "generation", "country")]
            parts = list(dict.fromkeys(str(part).strip() for part in parts if isinstance(part, (str, int)) and str(part).strip()))
            return {"queries": [" ".join(parts)[:512]] if parts else [], "filters": {},
                    "required_document_roles": ["own_document"], "missing_information": []}
        if kind == "selection":
            return {"selected_candidate_ids": [row["id"] for row in inputs["candidates"]],
                    "rejected": [], "conflicts": [], "missing_information": []}
        if kind == "answer":
            return {"classification": "NEEDS_INFORMATION",
                    "answer": "MOCK: 확인 가능한 근거를 찾지 못했습니다. 실제 모델의 보증 이전 검토 결과가 아닙니다.",
                    "claims": [], "conditions": [],
                    "missing_information": list(dict.fromkeys(inputs["missing_information"] + ["실제 모델의 근거 검토가 필요합니다."])),
                    "conflicts": inputs["conflicts"], "cited_evidence_ids": []}
        raise _error("PROMPT_KIND_INVALID")


class OpenAIStructuredModel:
    """Opt-in SDK adapter; constructing it never opens a network connection.

    The trusted server guard must return exactly True for *every* context sent
    externally. For document contexts it must enforce the source registry's
    external_process action at the document version, as well as a separate
    authorization for question/metadata processing. A missing guard or context
    authorization blocks before a client is constructed. An injected client is
    a server-side test seam, never a user-supplied argument.
    """
    is_mock = False

    def __init__(self, settings, external_process_guard=None, *, client=None, env=None):
        environment = os.environ if env is None else env
        self.enabled = settings.remote_models is True
        self._api_key = environment.get("OPENAI_API_KEY", "")
        self.model_id = environment.get("CHAT_MODEL", "")
        self._guard = external_process_guard
        self._client = client

    def generate(self, kind: str, prompt: str) -> str:
        _check_kind(kind)
        if (not self.enabled or not isinstance(self._api_key, str) or not self._api_key.strip()
                or not isinstance(self.model_id, str) or not self.model_id.strip()
                or len(self.model_id) > 200 or not callable(self._guard)):
            raise _error("MODEL_REMOTE_BLOCKED")
        try:
            envelope = json.loads(prompt)
            variables = envelope["input"]
            contexts = [("item", variables["item"]), ("question", {"question": variables["question"]}),
                        ("applicability", variables["applicability"])]
            contexts.extend(("candidate", row) for row in variables.get("candidates", []))
            contexts.extend(("evidence", row) for row in variables.get("evidence", []))
            if kind == "answer":
                contexts.append(("review_context", {"missing_information": variables["missing_information"],
                                                    "conflicts": variables["conflicts"]}))
            for context_kind, context in contexts:
                if self._guard(kind, context_kind, deepcopy(context)) is not True:
                    raise ValueError("External context is not approved")
        except Exception:
            raise _error("MODEL_REMOTE_BLOCKED") from None
        try:
            if self._client is None:
                from openai import OpenAI
                # Explicit origin prevents OPENAI_BASE_URL from redirecting data.
                self._client = OpenAI(api_key=self._api_key, base_url="https://api.openai.com/v1",
                                      timeout=20.0, max_retries=0)
            model_input = {"input": variables}
            if "format_repair" in envelope:
                model_input["format_repair"] = envelope["format_repair"]
            response = self._client.responses.create(
                model=self.model_id,
                instructions=json.dumps(envelope["policy"], ensure_ascii=False, allow_nan=False),
                input=json.dumps(model_input, ensure_ascii=False, allow_nan=False),
                text={"format": {"type": "json_object"}},
                store=False, tools=[], max_output_tokens=4096,
            )
            if not isinstance(response.output_text, str) or not response.output_text.strip():
                raise ValueError("No structured output")
            return response.output_text
        except Exception:
            raise _error("MODEL_UNAVAILABLE") from None
