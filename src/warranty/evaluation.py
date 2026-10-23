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
_REVIEW_SCHEMA = "evaluation-answer-review-v1"
_REVIEW_OUTCOMES = {
    "accuracy": {"CORRECT", "INCORRECT", "UNVERIFIED", "NOT_APPLICABLE"},
    "evidence": {"SUPPORTED", "UNSUPPORTED", "NO_CLAIMS", "UNVERIFIED"},
    "uncertainty": {"APPROPRIATE", "INAPPROPRIATE", "NOT_APPLICABLE", "UNVERIFIED"},
}


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


def _answer_artifact(artifact):
    """Check integrity, not authenticity of a supplied local collector artifact."""
    if not isinstance(artifact, dict):
        _error("EVALUATION_ARTIFACT_INVALID")
    unsigned = {key: value for key, value in artifact.items() if key not in {"artifact_fingerprint", "output_path"}}
    if (artifact.get("schema_version") != _SCHEMA or artifact.get("comparison") != "answers"
            or artifact.get("artifact_fingerprint") != _hash(unsigned)
            or not isinstance(artifact.get("mode"), str) or artifact["mode"] not in {"MOCK", "LIVE"}
            or not isinstance(artifact.get("context"), dict)
            or not isinstance(artifact.get("results"), list)):
        _error("EVALUATION_ARTIFACT_INVALID")
    inputs = artifact.get("inputs")
    if (not isinstance(inputs, dict) or type(artifact.get("failed_count")) is not int
            or artifact["failed_count"] < 0):
        _error("EVALUATION_ARTIFACT_INVALID")
    cases = ComparisonRunner._cases(inputs.get("cases"))
    context = artifact["context"]
    if (len(artifact["results"]) != len(cases) or artifact.get("case_count") != len(cases)
            or artifact.get("attempt_count") != 2 * len(cases)
            or context.get("mode") != artifact["mode"]
            or context.get("config_fingerprint") != artifact.get("config_fingerprint")
            or _hash({key: part for key, part in context.items() if key != "config_fingerprint"}) != artifact.get("config_fingerprint")
            or _hash(inputs) != artifact.get("input_fingerprint")):
        _error("EVALUATION_ARTIFACT_INVALID")
    for case, pair in zip(cases, artifact["results"]):
        if (not isinstance(pair, dict) or pair.get("case") != case
                or not isinstance(pair.get("runs"), dict)
                or set(pair["runs"]) != {"no_rag", "rag"}
                or any(not isinstance(run, dict) or run.get("status") not in {"COLLECTED", "FAILED"}
                       or run.get("status") == "COLLECTED" and "raw_response" not in run
                       for run in pair["runs"].values())):
            _error("EVALUATION_ARTIFACT_INVALID")
    failures = sum(run["status"] == "FAILED" for pair in artifact["results"] for run in pair["runs"].values())
    if artifact["failed_count"] != failures or artifact.get("collected_count") != 2 * len(cases) - failures:
        _error("EVALUATION_ARTIFACT_INVALID")
    return artifact


def answer_review_template(artifact):
    """Create an ungraded, immutable-response-bound review worksheet.

    References remain the ones supplied before collection. Reviewers must check
    their source and applicability independently; a declared VERIFIED reference
    is not independently authenticated by this module.
    """
    artifact = _answer_artifact(artifact)
    results = []
    for pair in artifact["results"]:
        for variant, run in pair["runs"].items():
            if run["status"] != "COLLECTED":
                continue
            results.append({"case_id": pair["case"]["case_id"], "variant": variant,
                "response_fingerprint": _hash(run["raw_response"]),
                "reference": deepcopy(pair["case"].get("reference")),
                "assessment": {name: {"outcome": "UNREVIEWED", "rationale": "", "reference_ids": []}
                               for name in _REVIEW_OUTCOMES}})
    return {"schema_version": _REVIEW_SCHEMA,
            "source_artifact_fingerprint": artifact["artifact_fingerprint"],
            "reviewer": "", "reviewer_type": "HUMAN", "reviewed_at_utc": "", "results": results}


def _reference_ids(reference):
    """IDs from pre-collection reference sources with a concrete locator."""
    if not isinstance(reference, dict) or reference.get("status") != "VERIFIED":
        return set()
    sources = reference.get("sources")
    if not isinstance(sources, list):
        return set()
    ids = [part["evidence_id"] for part in sources if isinstance(part, dict)
           and _text(part.get("evidence_id")) and _text(part.get("locator"))]
    return set(ids)


def assessed_answer_review(artifact, review):
    """Validate explicit reviewer judgments and summarize them without grading.

    Review identity and source correctness are reviewer-reported. Fingerprints
    prevent accidentally applying a worksheet to different collected responses;
    they are not signatures or proof of a human's identity.
    """
    template = answer_review_template(artifact)
    review = _json_copy(review)
    if (not isinstance(review, dict) or set(review) != set(template)
            or review.get("schema_version") != _REVIEW_SCHEMA
            or review.get("source_artifact_fingerprint") != template["source_artifact_fingerprint"]
            or not _text(review.get("reviewer")) or len(review["reviewer"]) > 200
            or not isinstance(review.get("reviewer_type"), str) or review["reviewer_type"] not in {"HUMAN", "AGENT_ASSISTED"}
            or not isinstance(review.get("results"), list)):
        _error("EVALUATION_REVIEW_INVALID")
    try:
        reviewed = datetime.fromisoformat(review["reviewed_at_utc"].replace("Z", "+00:00"))
        if reviewed.tzinfo is None or reviewed.utcoffset().total_seconds() != 0:
            raise ValueError()
    except (AttributeError, TypeError, ValueError):
        _error("EVALUATION_REVIEW_INVALID")
    expected = {(row["case_id"], row["variant"]): row for row in template["results"]}
    seen, counts = set(), {name: {outcome: 0 for outcome in sorted(outcomes)}
                          for name, outcomes in _REVIEW_OUTCOMES.items()}
    variant_counts = {variant: deepcopy(counts) for variant in ("no_rag", "rag")}
    case_comparisons = {}
    for row in review["results"]:
        if (not isinstance(row, dict) or set(row) != {"case_id", "variant", "response_fingerprint", "reference", "assessment"}
                or not isinstance(row.get("case_id"), str) or not isinstance(row.get("variant"), str)):
            _error("EVALUATION_REVIEW_INVALID")
        identity = (row["case_id"], row["variant"])
        source = expected.get(identity)
        if (source is None or identity in seen or row["response_fingerprint"] != source["response_fingerprint"]
                or row["reference"] != source["reference"] or not isinstance(row["assessment"], dict)
                or set(row["assessment"]) != set(_REVIEW_OUTCOMES)):
            _error("EVALUATION_REVIEW_INVALID")
        verified_ids = _reference_ids(source["reference"])
        for name, outcomes in _REVIEW_OUTCOMES.items():
            judgment = row["assessment"][name]
            if (not isinstance(judgment, dict) or set(judgment) != {"outcome", "rationale", "reference_ids"}
                    or not isinstance(judgment.get("outcome"), str) or judgment["outcome"] not in outcomes
                    or not _text(judgment.get("rationale")) or len(judgment["rationale"]) > 10000
                    or not isinstance(judgment.get("reference_ids"), list)
                    or any(not _text(value) for value in judgment["reference_ids"])
                    or len(set(judgment["reference_ids"])) != len(judgment["reference_ids"])
                    or set(judgment["reference_ids"]) - verified_ids):
                _error("EVALUATION_REVIEW_INVALID")
            # A positive/negative accuracy or grounding claim needs independently
            # checked reference material already recorded before generation.
            substantive = (name == "accuracy" and judgment["outcome"] in {"CORRECT", "INCORRECT"}
                           or name == "evidence" and judgment["outcome"] in {"SUPPORTED", "UNSUPPORTED"})
            if substantive and not judgment["reference_ids"]:
                _error("EVALUATION_REFERENCE_UNVERIFIED")
            counts[name][judgment["outcome"]] += 1
            variant_counts[row["variant"]][name][judgment["outcome"]] += 1
        case_comparisons.setdefault(row["case_id"], {})[row["variant"]] = {
            name: judgment["outcome"] for name, judgment in row["assessment"].items()}
        seen.add(identity)
    if not seen:
        _error("EVALUATION_REVIEW_INVALID")
    return {"schema_version": "evaluation-answer-review-report-v1", "review": review,
            "source_artifact_fingerprint": artifact["artifact_fingerprint"],
            "source_mode": artifact["mode"], "reviewer_type": review["reviewer_type"],
            "status": "REVIEWED" if len(seen) == len(expected) else "PARTIAL",
            "source_collection_status": artifact["status"],
            "reviewed_response_count": len(seen), "collected_response_count": len(expected),
            "unreviewed_response_count": len(expected) - len(seen),
            "failed_response_count": artifact["failed_count"], "outcome_counts": counts,
            "variant_outcome_counts": variant_counts,
            "fully_paired_reviewed_case_count": sum(set(pair) == {"no_rag", "rag"} for pair in case_comparisons.values()),
            "case_comparisons": [{"case_id": case_id, "variants": variants} for case_id, variants in case_comparisons.items()],
            "acceptance_status": "NOT_ASSESSED", "scores": None,
            "notice": "Reviewer-reported judgments; reference verification and identity are not independently authenticated. MOCK evidence cannot establish LIVE acceptance."}


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
                          text_field=normalized in {"answer", "text", "content", "response", "raw_response", "output", "output_text", "generated_text"},
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
    def _metrics(chunks, request, reference=None):
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
        required = reference.get("required_evidence_ids") if isinstance(reference, dict) else None
        declared = (reference.get("status") == "VERIFIED" and isinstance(required, list) and bool(required)
                    and all(_text(part) for part in required) and len(set(required)) == len(required)) if isinstance(reference, dict) else False
        expected = set(required) if declared else set()
        recovered = expected.intersection(chunk_ids)
        return {"chunk_ids": chunk_ids, "returned_chunk_count": len(chunks),
                "distinct_chunk_id_count": len(set(chunk_ids)),
                "duplicate_chunk_id_count": len(chunk_ids) - len(set(chunk_ids)),
                "duplicate_count": len(duplicate_keys) - len(set(duplicate_keys)),
                "duplicate_definition": "equal normalized text where supplied; otherwise equal chunk ID",
                "distinct_document_count": len(set(document_ids)), "document_ids": sorted(set(document_ids)),
                "unknown_document_identity_count": len(chunks) - len(document_ids),
                "document_diversity_ratio": len(set(document_ids)) / len(chunks) if chunks and len(document_ids) == len(chunks) else None,
                "distinct_document_version_count": len(set(version_ids)),
                "own_document_count": len(set(own_ids)), "own_document_ids": sorted(set(own_ids)),
                "own_document_chunk_count": own_chunks,
                "own_document_definition": "explicit own_document flag when present; otherwise linked primary/service/insurance role",
                "required_evidence_status": "REFERENCE_DECLARED_VERIFIED" if declared else "NOT_ASSESSED",
                "required_evidence_count": len(expected) if declared else None,
                "recovered_required_evidence_ids": sorted(recovered) if declared else None,
                "missing_required_evidence_ids": sorted(expected - recovered) if declared else None,
                "required_evidence_recall": len(recovered) / len(expected) if declared else None}

    def _invoke(self, callback, request, *, answers, reference=None):
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
            if isinstance(raw, dict) and raw.get("evaluation_error"):
                code = raw["evaluation_error"].get("code") if isinstance(raw["evaluation_error"], dict) else None
                entry["error"] = {"code": code if isinstance(code, str) and _CODE.fullmatch(code) else "EVALUATION_CALLBACK_FAILED"}
                return entry
            response, envelope = self._execution(raw, answers=answers)
            if answers:
                entry["raw_response"] = response
                entry["input_context"] = raw.get("input_context") if envelope else None
                entry["input_context_capture"] = "ADAPTER_REPORTED" if envelope and "input_context" in raw else "NOT_SUPPLIED"
            else:
                entry["chunks"] = response
                entry["metrics"] = self._metrics(response, request, reference=reference)
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
                pair["runs"][variant] = self._invoke(callback, request, answers=comparison == "answers",
                                                      reference=case.get("reference"))
            if comparison == "retrieval" and all(run["status"] == "COLLECTED" for run in pair["runs"].values()):
                left, right = (pair["runs"][variant]["metrics"] for variant in ("similarity", "mmr"))
                pair["observed_mmr_minus_similarity"] = {
                    name: right[name] - left[name] if left[name] is not None and right[name] is not None else None
                    for name in ("returned_chunk_count", "distinct_document_count", "duplicate_count",
                                 "own_document_count", "document_diversity_ratio", "required_evidence_recall")}
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

    def save_report(self, comparison, report):
        """Persist a runtime report through the same private-output boundary."""
        if comparison not in {"runtime", "performance", "review-template", "review"} or not isinstance(report, dict):
            _error()
        return self._save({**_json_copy(report), "comparison": comparison,
                           "config_fingerprint": self.config_fingerprint,
                           "context": self._context(), "mode": self.mode,
                           "acceptance_status": "NOT_ASSESSED", "scores": None})
