"""Private, measured comparison collection through explicitly supplied adapters.

No model, retriever, or network client is constructed here. Collection does not
grade accuracy or declare any product requirement passed. Real execution and
the model/context metadata are reported by the supplied adapter; the collector
checks their consistency and records its own observed wall-clock latency.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
import uuid

from .errors import ServiceError


_SCHEMA = "comparison-collector-v1"
_OWN_ROLES = {"PRIMARY_DESCRIPTION", "SERVICE_TERMS", "INSURANCE_TERMS"}
_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,79}\Z")


def _error(code="EVALUATION_INVALID", message="비교 평가 설정과 입력을 확인해 주세요.", status=422):
    raise ServiceError(code, message, status)


def _canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError):
        _error()


def _hash(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _json_copy(value):
    # Round-tripping rejects custom/non-JSON values rather than recording reprs
    # that might disclose local objects, credentials, or exception internals.
    return json.loads(_canonical(value))


def _contains_mock(value, *, text_field=False, model_field=False):
    """Reject obvious adapter/mock markers, including structured completions.

    This detects declarations and markers, not the authenticity of an opaque
    model call. LIVE still relies on a trusted server-side adapter.
    """
    if isinstance(value, str):
        return (model_field and "mock" in value.casefold()
                or text_field and re.search(r"\bmock\b", value, re.I) is not None)
    if isinstance(value, list):
        return any(_contains_mock(part, text_field=text_field, model_field=model_field) for part in value)
    if not isinstance(value, dict):
        return False
    for key, part in value.items():
        normalized = key.casefold()
        if normalized in {"is_mock", "mock"} and part is True:
            return True
        if normalized in {"mode", "execution_mode"} and isinstance(part, str) and part.upper() == "MOCK":
            return True
        if _contains_mock(part,
                          text_field=normalized in {"answer", "text", "content", "response", "output", "output_text", "generated_text"},
                          model_field=model_field or normalized in {"model", "model_metadata", "model_id", "model_name", "model_version", "provider"}):
            return True
    return False


class ComparisonRunner:
    """Collect no-RAG/RAG answers and similarity/MMR retrieval comparisons.

    Supply at least five fixed cases. A case contains ``item_id``, ``question``,
    and optional ``case_id``, ``applicability`` and private ``reference`` data.
    Every callback receives a fresh request with the same frozen ``context``.

    MOCK callbacks may return a JSON answer directly or a list of document
    chunks. LIVE callbacks must return ``{'response': raw, 'input_context': ...,
    'execution': request['context']}`` for answers or ``{'chunks': [...],
    'execution': request['context']}`` for retrieval. Execution metadata must
    describe the actual adapter run. Echoing metadata alone cannot prove a
    provider call happened; artifacts never assert independent verification.
    """

    def __init__(self, output_dir, *, mode="MOCK", model_metadata=None, settings=None,
                 revision=None, dataset_fingerprint=None, official_body_count=0,
                 external_process_authorized=False, repository_root=None):
        if mode not in {"MOCK", "LIVE"}:
            _error()
        if type(official_body_count) is not int or official_body_count < 0:
            _error()
        if type(external_process_authorized) is not bool:
            _error()
        self._mode = mode
        try:
            self.output_dir = Path(output_dir).expanduser().resolve()
            actual_repo = Path(__file__).resolve().parents[2]
            self._repositories = {actual_repo}
            if repository_root is not None:
                self._repositories.add(Path(repository_root).expanduser().resolve())
        except (OSError, TypeError, ValueError):
            _error("EVALUATION_OUTPUT_INVALID", "평가 결과를 저장할 비공개 경로를 확인해 주세요.")
        self._check_output_boundary()
        metadata = model_metadata if model_metadata is not None else {
            "provider": "local", "model_id": "MOCK-injected-adapter", "model_version": "1", "is_mock": True}
        if not isinstance(metadata, dict) or not isinstance(settings if settings is not None else {}, dict):
            _error()
        configuration = {
            "collector_schema": _SCHEMA, "mode": mode,
            "model_metadata": metadata, "settings": settings if settings is not None else {},
            "revision": revision if revision is not None else "MOCK-NO-REVISION",
            "dataset_fingerprint": dataset_fingerprint if dataset_fingerprint is not None else "MOCK-NO-DATASET",
            "official_body_count": official_body_count,
            "external_process_authorized": external_process_authorized,
        }
        self._configuration = _json_copy(configuration)
        if mode == "LIVE":
            if official_body_count == 0 or external_process_authorized is not True:
                _error("EVALUATION_LIVE_BLOCKED", "공식 본문과 외부 처리 승인 범위를 먼저 확인해 주세요.", 403)
            if (not isinstance(model_metadata, dict)
                    or any(not _text(model_metadata.get(key)) for key in ("provider", "model_id", "model_version"))
                    or model_metadata.get("is_mock") is not False
                    or any("mock" in model_metadata[key].casefold() for key in ("provider", "model_id", "model_version"))
                    or not _text(revision) or not _text(dataset_fingerprint)
                    or not isinstance(settings, dict) or "temperature" not in settings):
                _error("EVALUATION_LIVE_BLOCKED", "실제 모델·설정·자료 버전 정보가 필요합니다.", 403)
            temperature = settings["temperature"]
            if temperature is not None and (type(temperature) not in (int, float)
                                             or not math.isfinite(temperature) or temperature < 0):
                _error()
        elif not _text(configuration["revision"]) or not _text(configuration["dataset_fingerprint"]):
            _error()
        self.config_fingerprint = _hash(self._configuration)

    @property
    def mode(self):
        return self._mode

    def _check_output_boundary(self):
        try:
            resolved = self.output_dir.resolve()
            if (resolved == Path(resolved.anchor) or str(resolved).startswith("\\\\")
                    or any(resolved == repo or repo in resolved.parents for repo in self._repositories)):
                _error("EVALUATION_OUTPUT_INVALID", "평가 결과는 프로젝트 밖의 비공개 경로에 저장해 주세요.")
            # An unrelated repository is also an unsafe default for raw answers.
            if any((parent / ".git").exists() for parent in (resolved, *resolved.parents)):
                _error("EVALUATION_OUTPUT_INVALID", "평가 결과는 저장소 밖의 비공개 경로에 저장해 주세요.")
            self.output_dir = resolved
        except (OSError, ValueError):
            _error("EVALUATION_OUTPUT_INVALID", "평가 결과를 저장할 비공개 경로를 확인해 주세요.")

    @staticmethod
    def _cases(cases):
        if not isinstance(cases, (list, tuple)) or not 5 <= len(cases) <= 1000:
            _error("EVALUATION_CASES_INVALID", "서로 다른 고정 질문을 5개 이상 입력해 주세요.")
        result, ids, identities = [], set(), set()
        allowed = {"case_id", "item_id", "question", "applicability", "reference"}
        for index, original in enumerate(cases, 1):
            if not isinstance(original, dict) or set(original) - allowed:
                _error("EVALUATION_CASES_INVALID")
            if (not _text(original.get("item_id")) or len(original["item_id"]) > 256
                    or not _text(original.get("question")) or len(original["question"]) > 10000
                    or not isinstance(original.get("applicability", {}), dict)):
                _error("EVALUATION_CASES_INVALID")
            case = _json_copy(original)
            case.setdefault("case_id", f"case-{index:03d}")
            case.setdefault("applicability", {})
            if not _text(case["case_id"]) or len(case["case_id"]) > 128 or case["case_id"] in ids:
                _error("EVALUATION_CASES_INVALID")
            identity = _hash([case["item_id"], re.sub(r"\s+", " ", case["question"]).strip().casefold(), case["applicability"]])
            if identity in identities:
                _error("EVALUATION_CASES_INVALID", "중복되지 않는 고정 질문을 입력해 주세요.")
            ids.add(case["case_id"])
            identities.add(identity)
            result.append(case)
        return result

    def _context(self):
        return {**deepcopy(self._configuration), "config_fingerprint": self.config_fingerprint}

    def _input(self, cases, comparison, top_k=None, filters=None):
        if comparison not in {"answers", "retrieval"}:
            _error()
        if comparison == "retrieval":
            if type(top_k) is not int or not 1 <= top_k <= 100 or not isinstance(filters, dict):
                _error()
        return {"cases": cases, "comparison": comparison, "top_k": top_k, "filters": filters}

    def is_current(self, artifact, cases, *, comparison="answers", top_k=6, filters=None):
        """Return False for changed cases/configuration or altered artifact data.

        This is a validation helper, not a cache: running a comparison always
        invokes the supplied callbacks again and measures those invocations.
        """
        try:
            fixed = self._cases(cases)
            inputs = self._input(fixed, comparison, top_k if comparison == "retrieval" else None,
                                 _json_copy(filters or {}) if comparison == "retrieval" else None)
            if not isinstance(artifact, dict):
                return False
            unsigned = {k: v for k, v in artifact.items() if k not in {"artifact_fingerprint", "output_path"}}
            return (artifact.get("schema_version") == _SCHEMA
                    and artifact.get("config_fingerprint") == self.config_fingerprint
                    and artifact.get("input_fingerprint") == _hash(inputs)
                    and artifact.get("comparison") == comparison
                    and artifact.get("artifact_fingerprint") == _hash(unsigned))
        except ServiceError:
            return False

    def _request(self, case, comparison, variant, top_k=None, filters=None):
        return {"case_id": case["case_id"], "item_id": case["item_id"], "question": case["question"],
                "applicability": deepcopy(case["applicability"]), "comparison": comparison,
                "variant": variant, "strategy": variant if comparison == "retrieval" else None,
                "top_k": top_k, "filters": deepcopy(filters), "context": self._context()}

    def _execution(self, raw, *, answers):
        envelope = isinstance(raw, dict) and "execution" in raw
        if self.mode == "LIVE":
            if (not envelope or raw["execution"] != self._context()
                    or answers and "input_context" not in raw
                    or answers and raw.get("input_context") is None):
                _error("EVALUATION_EXECUTION_MISMATCH", "실제 실행의 모델·설정·입력 문맥을 확인할 수 없습니다.")
        elif envelope and raw["execution"] != self._context():
            _error("EVALUATION_EXECUTION_MISMATCH", "비교 실행의 모델·설정·자료 버전이 다릅니다.")
        if envelope:
            key = "response" if answers else "chunks"
            if key not in raw:
                _error("EVALUATION_CALLBACK_INVALID")
            response = raw[key]
        else:
            response = raw
        if self.mode == "LIVE" and _contains_mock(response, text_field=True):
            _error("EVALUATION_EXECUTION_MISMATCH", "모의 응답을 실제 모델 결과로 기록할 수 없습니다.")
        return response, envelope

    @staticmethod
    def _metrics(chunks, request):
        if not isinstance(chunks, list) or len(chunks) > request["top_k"]:
            _error("EVALUATION_RETRIEVAL_INVALID")
        chunk_ids, document_ids, version_ids, own_ids, duplicate_keys = [], [], [], [], []
        own_chunks = 0
        for chunk in chunks:
            if not isinstance(chunk, dict):
                _error("EVALUATION_RETRIEVAL_INVALID")
            chunk_id = chunk.get("evidence_id", chunk.get("chunk_id", chunk.get("id")))
            if not _text(chunk_id) or chunk.get("source_type") == "metadata":
                _error("EVALUATION_RETRIEVAL_INVALID")
            if chunk.get("item_id", request["item_id"]) != request["item_id"]:
                _error("EVALUATION_RETRIEVAL_INVALID", "다른 항목의 검색 근거가 포함되었습니다.")
            context = request["context"]
            if "revision" in chunk and chunk["revision"] != context["revision"]:
                _error("EVALUATION_EXECUTION_MISMATCH", "검색 근거의 자료 버전이 다릅니다.")
            if context["mode"] == "LIVE" and (
                chunk.get("source_type") != "official_document" or chunk.get("is_synthetic") is True
                or chunk.get("item_id") != request["item_id"] or chunk.get("revision") != context["revision"]
            ):
                _error("EVALUATION_EXECUTION_MISMATCH", "실제 검색 비교에는 같은 항목·버전의 공식 문서 근거가 필요합니다.")
            document_id = chunk.get("document_id")
            version_id = chunk.get("version_id") or chunk.get("document_version_id")
            if context["mode"] == "LIVE" and not _text(document_id):
                _error("EVALUATION_RETRIEVAL_INVALID")
            chunk_ids.append(chunk_id)
            if _text(document_id):
                document_ids.append(document_id)
            if _text(version_id):
                version_ids.append(version_id)
            text = chunk.get("text")
            duplicate_keys.append(("text", re.sub(r"\s+", " ", text).strip()) if _text(text) else ("id", chunk_id))
            role = chunk.get("role")
            if role is not None and not isinstance(role, str):
                _error("EVALUATION_RETRIEVAL_INVALID")
            if "own_document" in chunk and type(chunk["own_document"]) is not bool:
                _error("EVALUATION_RETRIEVAL_INVALID")
            is_own = chunk["own_document"] if "own_document" in chunk else role in _OWN_ROLES
            if is_own:
                own_chunks += 1
                if _text(document_id):
                    own_ids.append(document_id)
        return {"chunk_ids": chunk_ids, "returned_chunk_count": len(chunks),
                "distinct_chunk_id_count": len(set(chunk_ids)),
                "duplicate_chunk_id_count": len(chunk_ids) - len(set(chunk_ids)),
                "duplicate_count": len(duplicate_keys) - len(set(duplicate_keys)),
                "duplicate_definition": "equal normalized text where supplied; otherwise equal chunk ID",
                "distinct_document_count": len(set(document_ids)), "document_ids": sorted(set(document_ids)),
                "unknown_document_identity_count": len(chunks) - len(document_ids),
                "distinct_document_version_count": len(set(version_ids)),
                "own_document_count": len(set(own_ids)), "own_document_ids": sorted(set(own_ids)),
                "own_document_chunk_count": own_chunks,
                "own_document_definition": "explicit own_document flag when present; otherwise linked primary/service/insurance role"}

    def _invoke(self, callback, request, *, answers):
        entry = {"variant": request["variant"], "status": "FAILED"}
        started = time.perf_counter()
        try:
            raw = callback(deepcopy(request))
        except Exception as exc:
            entry["elapsed_seconds"] = time.perf_counter() - started
            code = getattr(exc, "code", None)
            entry["error"] = {"code": code if isinstance(code, str) and _CODE.fullmatch(code) else "EVALUATION_CALLBACK_FAILED"}
            return entry
        entry["elapsed_seconds"] = time.perf_counter() - started
        try:
            raw = _json_copy(raw)
            entry["raw_callback_result"] = raw
            response, envelope = self._execution(raw, answers=answers)
            if answers:
                entry["raw_response"] = response
                entry["input_context"] = raw.get("input_context") if envelope else None
                entry["input_context_capture"] = "ADAPTER_REPORTED" if envelope and "input_context" in raw else "NOT_SUPPLIED"
            else:
                entry["chunks"] = response
                entry["metrics"] = self._metrics(response, request)
            entry["execution_provenance"] = "ADAPTER_REPORTED" if envelope else "MOCK_CALLBACK"
            entry["status"] = "COLLECTED"
        except ServiceError as exc:
            entry["error"] = {"code": exc.code}
        return entry

    def _save(self, artifact):
        self._check_output_boundary()
        artifact["artifact_fingerprint"] = _hash(artifact)
        name = f"comparison-{artifact['comparison']}-{uuid.uuid4().hex}.json"
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            self._check_output_boundary()
            destination = self.output_dir / name
            temporary = self.output_dir / (".evaluation-" + uuid.uuid4().hex + ".tmp")
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                    stream.write(json.dumps(artifact, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                self._check_output_boundary()
                os.replace(temporary, destination)
            finally:
                if temporary.exists():
                    temporary.unlink()
        except OSError:
            _error("EVALUATION_OUTPUT_UNAVAILABLE", "비공개 평가 결과를 저장하지 못했습니다.", 503)
        return {**artifact, "output_path": str(destination)}

    def _run(self, cases, callbacks, *, comparison, top_k=None, filters=None):
        if any(not callable(callback) for callback in callbacks.values()):
            _error("EVALUATION_CALLBACK_INVALID")
        fixed = self._cases(cases)
        filters = _json_copy(filters) if filters is not None else None
        inputs = self._input(fixed, comparison, top_k, filters)
        results = []
        for case in fixed:
            pair = {"case": deepcopy(case), "runs": {}}
            for variant, callback in callbacks.items():
                request = self._request(case, comparison, variant, top_k, filters)
                pair["runs"][variant] = self._invoke(callback, request, answers=comparison == "answers")
            results.append(pair)
        runs = [run for case in results for run in case["runs"].values()]
        collected = sum(run["status"] == "COLLECTED" for run in runs)
        artifact = {"schema_version": _SCHEMA, "comparison": comparison, "mode": self.mode,
                    "created_at_utc": datetime.now(timezone.utc).isoformat(),
                    "status": "COLLECTED" if collected == len(runs) else ("PARTIAL" if collected else "FAILED"),
                    "acceptance_status": "NOT_ASSESSED", "scores": None,
                    "provenance_notice": "Execution/model/context are adapter-reported; latency is collector-observed. No automatic accuracy or requirement pass is asserted.",
                    "context": self._context(), "config_fingerprint": self.config_fingerprint,
                    "input_fingerprint": _hash(inputs), "inputs": inputs,
                    "call_order": list(callbacks), "case_count": len(fixed), "attempt_count": len(runs),
                    "collected_count": collected, "failed_count": len(runs) - collected,
                    "results": results}
        return self._save(artifact)

    def run_answers(self, cases, no_rag, rag):
        return self._run(cases, {"no_rag": no_rag, "rag": rag}, comparison="answers")

    def run_retrieval(self, cases, retrieve, *, top_k=6, filters=None):
        return self._run(cases, {"similarity": retrieve, "mmr": retrieve}, comparison="retrieval",
                         top_k=top_k, filters={} if filters is None else filters)
