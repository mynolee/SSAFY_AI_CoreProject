"""Private evaluation of the application's configured model and retrieval path.

No provider is selected, constructed, or authenticated here. The application
supplies its model, prompt store, guarded retrieval service, and source policy.
Reference answers are saved for human evaluation and never enter model input.
"""
from __future__ import annotations

from contextlib import nullcontext
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import time

from .documents import APPLICABILITY_FIELDS
from .errors import ServiceError
from .evaluation import (ComparisonRunner, answer_review_template, assessed_answer_review,
                         _canonical, _contains_mock, _hash, _json_copy, _text)
from .prompts import StructuredRunner


def _fail(code="EVALUATION_CONFIG_INVALID", status=422):
    raise ServiceError(code, "비교 평가 입력·실행 권한·자료 준비 상태를 확인해 주세요.", status)


def _private_json(value):
    if not isinstance(value, (str, Path)):
        return value
    try:
        path = Path(value).expanduser().resolve()
        repo = Path(__file__).resolve().parents[2]
        if (path == repo or repo in path.parents or str(path).startswith("\\\\")
                or any((parent / ".git").exists() for parent in path.parents)
                or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024):
            _fail("EVALUATION_INPUT_PRIVATE_REQUIRED")
        def unique(pairs):
            result = {}
            for key, part in pairs:
                if key in result:
                    raise ValueError()
                result[key] = part
            return result
        return json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (OSError, TypeError, ValueError, RecursionError):
        _fail("EVALUATION_INPUT_INVALID")


def _safe_code(exc):
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) and code.isascii() and code.replace("_", "").isalnum() else "EVALUATION_RUNTIME_FAILED"


def _metadata(model, *, embedding=False):
    provider = getattr(model, "metadata", None)
    if callable(provider):
        result = provider()
        if not isinstance(result, dict):
            _fail("EVALUATION_MODEL_INVALID")
    elif getattr(model, "is_mock", None) is True:
        result = {"provider": "local", "model_id": getattr(model, "model_id", "MOCK-configured-adapter"),
                  "model_version": "MOCK-UNVERSIONED", "is_mock": True, "is_remote": False,
                  "temperature": None, "sampling": "unspecified", "generation_settings": {}}
        if embedding:
            result.update(name=getattr(model, "name", None), dimension=getattr(model, "dimension", None))
    else:
        _fail("EVALUATION_MODEL_INVALID")
    allowed = {"provider", "model_id", "model_version", "version_source", "provider_reported_model",
               "is_mock", "is_remote", "can_generate", "authorization", "temperature", "sampling", "generation_settings", "transport_settings",
               "name", "dimension", "route", "configuration_ready", "live_execution_required", "live_verified", "version_source"}
    return _json_copy({key: part for key, part in result.items() if key in allowed})


def _stable_model(metadata):
    return {key: part for key, part in metadata.items() if key != "provider_reported_model"}


def _runtime_snapshot(app):
    """Record public configuration facts, excluding keys, paths and approval IDs."""
    runtime = getattr(app, "runtime", None)
    models = runtime.get("models", {}) if isinstance(runtime, dict) else {}
    model_keys = {"chat_provider", "chat_model", "chat_model_version", "embedding_provider", "embedding_model",
                  "embedding_model_version", "embedding_dimension", "external_processing_approved", "allow_question_processing"}
    config = {key: part for key, part in models.items() if key in model_keys and type(part) in (str, int, bool, type(None))}
    method = getattr(app, "runtime_status", None)
    status = method() if callable(method) else {}
    if not isinstance(status, dict):
        _fail("EVALUATION_MODEL_INVALID")
    safe = {key: status[key] for key in ("external_processing_approved", "question_processing_approved")
            if type(status.get(key)) is bool}
    for kind in ("chat", "embedding"):
        entry = status.get(kind)
        if isinstance(entry, dict):
            safe[kind] = {key: entry[key] for key in ("provider", "configured", "ready", "is_mock")
                          if key in entry and type(entry[key]) in (str, bool)}
    return {"remote_models_enabled": getattr(app.settings, "remote_models", False) is True,
            "model_configuration": config, "status": safe}


class _Capture:
    """Capture raw answer attempts and inputs, excluding private prompt policy."""
    def __init__(self, model):
        self.model, self.calls = model, []

    def generate(self, kind, prompt):
        envelope = json.loads(prompt)
        record = {"kind": kind, "prompt_fingerprint": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                  "input_context": _json_copy(envelope["input"]), "format_repair": "format_repair" in envelope}
        started = time.perf_counter()
        try:
            result = self.model.generate(kind, prompt)
            record["elapsed_seconds"] = time.perf_counter() - started
            record["raw_response"] = _json_copy(result)
            record["model_metadata_after"] = _metadata(self.model)
            self.calls.append(record)
            return result
        except Exception as exc:
            record["elapsed_seconds"] = time.perf_counter() - started
            record["error"] = {"code": _safe_code(exc)}
            self.calls.append(record)
            raise


class _Runtime:
    def __init__(self, app, cases, config):
        if not isinstance(config, dict):
            _fail()
        allowed = {"mode", "output_dir", "user_id", "comparisons", "top_k", "filters", "performance_runs",
                   "measure_cold_prepare", "external_process_authorized"}
        if set(config) - allowed or "output_dir" not in config:
            _fail()
        self.config = _json_copy(config)
        self.mode = config.get("mode", "MOCK")
        self.user = config.get("user_id", "buyer")
        self.top_k = config.get("top_k", 6)
        self.filters = config.get("filters", {})
        self.comparisons = config.get("comparisons", ["answers", "retrieval", "performance"])
        self.iterations = config.get("performance_runs", 20)
        if (self.mode not in {"MOCK", "LIVE"} or not _text(self.user) or len(self.user) > 128
                or type(self.top_k) is not int or not 1 <= self.top_k <= 30
                or not isinstance(self.filters, dict) or set(self.filters) - set(APPLICABILITY_FIELDS)
                or any(not _text(part) or len(part) > 200 for part in self.filters.values())
                or not isinstance(self.comparisons, list) or not self.comparisons
                or any(part not in {"answers", "retrieval", "performance"} for part in self.comparisons)
                or len(set(self.comparisons)) != len(self.comparisons)
                or type(self.iterations) is not int or not 20 <= self.iterations <= 1000
                or type(config.get("measure_cold_prepare", False)) is not bool
                or type(config.get("external_process_authorized", False)) is not bool):
            _fail()
        self.app, self.rag = app, app.rag
        models = getattr(app, "models", None)
        self.model = models.get("chat") if isinstance(models, dict) else getattr(models, "chat", None)
        if self.model is None:
            self.model = self.rag.model
        if self.model is not self.rag.model or not callable(getattr(self.model, "generate", None)):
            _fail("EVALUATION_MODEL_INVALID")
        self.model_metadata = _metadata(self.model)
        vectors = getattr(self.rag, "vectors", None)
        self.embedding = models.get("embedding") if isinstance(models, dict) else getattr(models, "embedding", None)
        if self.embedding is None:
            self.embedding = getattr(vectors, "embeddings", None)
        if self.embedding is None or self.embedding is not getattr(vectors, "embeddings", None):
            _fail("EVALUATION_MODEL_INVALID")
        self.embedding_metadata = _metadata(self.embedding, embedding=True)
        self.runtime_snapshot = _runtime_snapshot(app)
        if self.mode == "MOCK" and getattr(self.model, "is_mock", None) is not True:
            _fail("EVALUATION_MODE_MISMATCH", 403)
        if self.mode == "LIVE" and (
                getattr(app.settings, "remote_models", False) is not True
                or config.get("external_process_authorized") is not True
                or getattr(self.model, "is_mock", None) is not False
                or self.model_metadata.get("is_mock") is not False
                or self.model_metadata.get("is_remote") is not True
                or self.model_metadata.get("can_generate") is not True
                or not callable(getattr(self.model, "processing_context", None))
                or _contains_mock(self.model_metadata, model_field=True)):
            _fail("EVALUATION_LIVE_BLOCKED", 403)
        self.store = self.rag.prompt_store
        self.prompts = {kind: _hash(self.store.load(kind)) for kind in ("query", "selection", "answer")}
        self.cases = ComparisonRunner._cases(cases)
        for case in self.cases:
            self.app.catalog.get(case["item_id"])
            if len(case["question"]) > 5000 or set(case["applicability"]) - set(APPLICABILITY_FIELDS):
                _fail("EVALUATION_CASES_INVALID")
            for key, part in self.filters.items():
                if key in case["applicability"] and case["applicability"][key] != part:
                    _fail("EVALUATION_FILTER_CONFLICT")
            case["applicability"] = {**self.filters, **case["applicability"]}
            if any(not _text(part) or len(part) > 200 for part in case["applicability"].values()):
                _fail("EVALUATION_CASES_INVALID")
        self.item_ids = sorted({case["item_id"] for case in self.cases})
        self.corpus = self.rag.corpus_summary(item_ids=self.item_ids)
        self.revision = self.corpus.get("revision") or "MOCK-META-ONLY"
        self.dataset_hash = self.app.catalog.dataset_hash
        self.retrieval_snapshots, self.retrieval_pairs, self.observed = {}, {}, {}
        self._live_corpus()
        for case in self.cases:
            self._guard_case(case)
        generation = {"temperature": self.model_metadata.get("temperature"),
                      "sampling": self.model_metadata.get("sampling"),
                      "generation_settings": self.model_metadata.get("generation_settings", {}),
                      "prompt_fingerprints": self.prompts, "top_k": self.top_k, "filters": self.filters,
                      "answer_cache": False, "collection": False,
                      "embedding_metadata": _stable_model(self.embedding_metadata),
                      "runtime_configuration": self.runtime_snapshot,
                      "active_index": self.corpus.get("active_index"),
                      "metadata_index_matches": self.corpus.get("metadata_index_matches")}
        self.runner = ComparisonRunner(config["output_dir"], mode=self.mode, model_metadata=self.model_metadata,
            settings=generation, revision=self.revision, dataset_fingerprint=self.dataset_hash,
            official_body_count=self.corpus.get("official_document_body_count", 0),
            external_process_authorized=config.get("external_process_authorized", False))

    def _live_corpus(self):
        if self.mode == "LIVE" and (not _text(self.corpus.get("revision"))
                or type(self.corpus.get("official_document_body_count")) is not int
                or self.corpus["official_document_body_count"] <= 0
                or type(self.corpus.get("official_document_chunk_count")) is not int
                or self.corpus["official_document_chunk_count"] <= 0):
            _fail("EVALUATION_LIVE_BLOCKED", 403)
        if self.mode != "LIVE":
            return
        meta = self.embedding_metadata
        active = self.corpus.get("active_index")
        status = self.runtime_snapshot["status"]
        if (getattr(self.embedding, "is_mock", None) is not False or meta.get("is_mock") is not False
                or getattr(self.embedding, "is_remote", None) is not True or meta.get("is_remote") is not True
                or meta.get("can_generate") is not True
                or not callable(getattr(self.embedding, "processing_context", None))
                or any(not _text(meta.get(key)) for key in ("provider", "model_id", "model_version", "name"))
                or _contains_mock(meta, model_field=True)
                or type(meta.get("dimension")) is not int or not 1 <= meta["dimension"] <= 8192
                or meta["name"] != getattr(self.embedding, "name", None)
                or meta["dimension"] != getattr(self.embedding, "dimension", None)
                or not isinstance(active, dict)
                or active.get("revision") != self.revision or active.get("dataset_hash") != self.dataset_hash
                or active.get("embedding_model") != meta["name"] or active.get("dimension") != meta["dimension"]
                or self.corpus.get("metadata_index_matches") is not True
                or status.get("external_processing_approved") is not True
                or status.get("question_processing_approved") is not True):
            _fail("EVALUATION_LIVE_BLOCKED", 403)
        for kind, metadata in (("chat", self.model_metadata), ("embedding", meta)):
            configured = status.get(kind, {})
            if (configured.get("ready") is not True or configured.get("configured") is not True
                    or configured.get("is_mock") is not False or configured.get("provider") != metadata.get("provider")):
                _fail("EVALUATION_LIVE_BLOCKED", 403)
        # These checks do not grant GMS live_execution scope or claim that the
        # provider was called. The configured adapters retain those gates.

    def _processing(self, item_id):
        method = getattr(self.model, "processing_context", None)
        return method(self.user, item_id) if callable(method) else nullcontext()

    def _public_metadata_local_only(self, case):
        """Recognize the factory's exact public adapter/local metadata boundary.

        A marker or a manifest is not an authorization exemption. This checks
        the actual adapter, authorization callbacks and both embedding handles;
        source/version rights and LIVE/budget checks still run on every request.
        """
        from .public_gms import GMSPublicStructuredModel
        from .public_runtime import PublicProcessingContext
        from .runtime import AuthorizedChatModel
        from .vectorstore import MockEmbeddings

        models = getattr(self.app, "models", None)
        get = models.get if isinstance(models, dict) else lambda key: getattr(models, key, None)
        context = get("public_context")
        if not isinstance(context, PublicProcessingContext) or not isinstance(self.model, AuthorizedChatModel):
            return False
        adapter = self.model.adapter
        local_metadata = get("metadata_embedding")
        protocol = getattr(adapter, "_public_protocol", None)
        if (not isinstance(adapter, GMSPublicStructuredModel) or get("chat") is not self.model
                or self.rag.model is not self.model
                or context.authorization is not getattr(self.app, "authorization", None)
                or self.model.authorization is not context.authorization
                or context.authorization.catalog is not self.app.catalog
                or context.authorization.registry is not self.rag.registry
                or context.authorization.vectors is not self.rag.vectors
                or adapter._original_guard != context.model_guard
                or adapter._public_source_resolver != context.source_resolver
                or getattr(protocol, "_manifest", None) != context.bundle.protocol
                or not context.bundle.current()
                or case["item_id"] not in context.item_ids
                or hashlib.sha256(case["question"].encode("utf-8")).hexdigest() not in context.bundle.protocol["question_sha256s"]
                or get("document_item_ids") != list(context.item_ids)
                or self.rag.document_item_ids != context.item_ids
                or not isinstance(local_metadata, MockEmbeddings)
                or local_metadata is not self.rag.vectors.metadata_embeddings
                or local_metadata is self.embedding
                or get("embedding") is not self.embedding
                or self.embedding is not self.rag.vectors.embeddings
                or getattr(self.embedding, "public_protocol_only", None) is not True
                or getattr(self.embedding, "_guard", None) != context.embedding_guard
                or getattr(self.embedding, "approved_document_sha256s", None) != context.document_sha256s):
            return False
        return True

    def _guard_case(self, case):
        item = self.app.catalog.get(case["item_id"])
        if self.mode == "LIVE":
            if item.get("is_synthetic") is True or case["item_id"].startswith("SYNTHETIC"):
                _fail("EVALUATION_LIVE_BLOCKED", 403)
            if self._public_metadata_local_only(case):
                return item
            models = getattr(self.app, "models", None)
            public = models.get("public_context") if isinstance(models, dict) else getattr(models, "public_context", None)
            if public is not None or getattr(self.model, "public_protocol_only", False) is True:
                _fail("EVALUATION_LIVE_BLOCKED", 403)
            self.rag.registry.policy.require(item["source_id"], "external_process", actor=self.user,
                                             version_id=self.dataset_hash)
        return item

    def _unchanged(self):
        corpus = self.rag.corpus_summary(item_ids=self.item_ids)
        models = getattr(self.app, "models", None)
        chat = models.get("chat") if isinstance(models, dict) else getattr(models, "chat", None)
        embedding = models.get("embedding") if isinstance(models, dict) else getattr(models, "embedding", None)
        if ((corpus.get("revision") or "MOCK-META-ONLY") != self.revision
                or self.model is not self.rag.model or chat is not None and chat is not self.model
                or self.embedding is not getattr(self.rag.vectors, "embeddings", None)
                or embedding is not None and embedding is not self.embedding
                or self.app.catalog.dataset_hash != self.dataset_hash
                or _stable_model(_metadata(self.model)) != _stable_model(self.model_metadata)
                or _stable_model(_metadata(self.embedding, embedding=True)) != _stable_model(self.embedding_metadata)
                or _runtime_snapshot(self.app) != self.runtime_snapshot
                or corpus.get("active_index") != self.corpus.get("active_index")
                or corpus.get("metadata_index_matches") != self.corpus.get("metadata_index_matches")
                or any(_hash(self.store.load(kind)) != value for kind, value in self.prompts.items())):
            _fail("EVALUATION_SNAPSHOT_CHANGED")

    def context(self, request):
        self._unchanged()
        self._guard_case(request)
        with self._processing(request["item_id"]):
            context = self.rag.evaluation_context(self.user, request["item_id"], request["question"],
                applicability=request["applicability"], top_k=request.get("top_k") or self.top_k,
                strategy=request.get("strategy") or "similarity", allow_collection=False, cache=False)
        return self._validate_context(context, request)

    def _validate_context(self, context, request):
        if not isinstance(context, dict):
            _fail("EVALUATION_CONTEXT_INVALID")
        actual_revision = context.get("revision") or "MOCK-META-ONLY"
        if actual_revision != self.revision:
            _fail("EVALUATION_SNAPSHOT_CHANGED")
        evidence = context.get("evidence")
        if not isinstance(evidence, list):
            _fail("EVALUATION_CONTEXT_INVALID")
        for chunk in evidence:
            if not isinstance(chunk, dict) or chunk.get("item_id") != request["item_id"]:
                _fail("EVALUATION_CONTEXT_INVALID")
            if chunk.get("source_type") == "metadata":
                continue
            if self.mode == "LIVE":
                if (chunk.get("source_type") != "official_document" or chunk.get("is_synthetic") is True
                        or chunk.get("revision") != self.revision or not _text(chunk.get("version_id"))
                        or chunk.get("type") not in {"HTML", "PDF"}
                        or chunk["type"] == "PDF" and (type(chunk.get("page")) is not int or chunk["page"] < 1)
                        or chunk["type"] == "HTML" and not _text(chunk.get("section"))):
                    _fail("EVALUATION_LIVE_BLOCKED", 403)
                for action in ("local_embed", "display_excerpt", "external_process"):
                    self.rag.registry.policy.require(chunk["source_id"], action, actor=self.user, version_id=chunk["version_id"])
            identity = (chunk.get("source_type"), chunk.get("document_id"), chunk.get("version_id"), chunk.get("type"))
            self.observed[identity] = {key: chunk.get(key) for key in ("source_type", "document_id", "version_id", "type")}
        if self.mode == "LIVE" and (context.get("readiness", {}).get("status") != "INDEX_READY"
                or not any(part.get("source_type") == "official_document" and part.get("role") in
                           {"PRIMARY_DESCRIPTION", "SERVICE_TERMS", "INSURANCE_TERMS"} for part in evidence)):
            _fail("EVALUATION_LIVE_BLOCKED", 403)
        self._unchanged()
        return context

    def answer(self, request):
        self._unchanged()
        self._guard_case(request)
        if request["variant"] == "no_rag":
            variables = {"item": {}, "question": request["question"], "applicability": request["applicability"],
                         "evidence": [], "missing_information": [], "conflicts": []}
            prepared = False
        else:
            context = self.context(request)
            item = context.get("item", context.get("item_context"))
            if not isinstance(item, dict):
                _fail("EVALUATION_CONTEXT_INVALID")
            variables = {"item": item, "question": request["question"], "applicability": request["applicability"],
                         "evidence": context["evidence"], "missing_information": context.get("missing_information", []),
                         "conflicts": context.get("conflicts", [])}
            prepared = context.get("readiness", {}).get("status") == "INDEX_READY" and not context.get("errors")
        capture = _Capture(self.model)
        packet = {"execution": request["context"], "input_context": variables, "prepared": prepared}
        try:
            with self._processing(request["item_id"]):
                answer = StructuredRunner(self.store, capture).run("answer", variables)
            self._unchanged()
            packet["response"] = {"validated_answer": answer, "raw_model_calls": capture.calls,
                                  "model": self.model_metadata["model_id"]}
        except Exception as exc:
            packet["response"] = {"raw_model_calls": capture.calls}
            packet["evaluation_error"] = {"code": _safe_code(exc)}
        packet["answer_model_invocation_count"] = len(capture.calls)
        return packet

    def retrieve(self, request):
        compare = getattr(self.rag, "compare_retrieval", None)
        if callable(compare):
            self._unchanged()
            self._guard_case(request)
            if request["case_id"] not in self.retrieval_pairs:
                with self._processing(request["item_id"]):
                    pair = compare(self.user, request["item_id"], request["question"],
                                   applicability=request["applicability"], top_k=request["top_k"])
                self._validate_context(pair, request)
                if not isinstance(pair.get("similarity"), list) or not isinstance(pair.get("mmr"), list):
                    _fail("EVALUATION_CONTEXT_INVALID")
                for strategy in ("similarity", "mmr"):
                    projected = {**pair, "evidence": pair[strategy]}
                    # Preserve metadata for the same own-document/context guard.
                    projected["evidence"] = [part for part in pair["evidence"] if part.get("source_type") == "metadata"] + pair[strategy]
                    self._validate_context(projected, request)
                self.retrieval_pairs[request["case_id"]] = pair
            pair = self.retrieval_pairs[request["case_id"]]
            self._validate_context({**pair, "evidence": [part for part in pair["evidence"] if part.get("source_type") == "metadata"] + pair[request["strategy"]]}, request)
            return {"chunks": pair[request["strategy"]], "execution": request["context"],
                    "retrieval_snapshot": pair.get("retrieval_filters", {}),
                    "ranking_elapsed_seconds": pair.get("ranking_elapsed_seconds", {}).get(request["strategy"]),
                    "common_context_elapsed_seconds": pair.get("common_context_elapsed_seconds"),
                    "callback_timing_scope": "shared paired retrieval on first callback; projection on second; backend ranking timing is separate"}
        context = self.context(request)
        # A nondeterministic query/selection change would confound the strategy
        # comparison. Record that attempt as failed rather than a fair pair.
        identity = request["case_id"]
        selection = context.get("selection", {})
        frozen = {"queries": context.get("queries", []),
                  "selected_candidate_ids": sorted(selection.get("selected_candidate_ids", [])),
                  "retrieval_filters": context.get("retrieval_filters", request["filters"])}
        if identity in self.retrieval_snapshots and self.retrieval_snapshots[identity] != frozen:
            _fail("EVALUATION_FILTER_DRIFT")
        self.retrieval_snapshots[identity] = frozen
        return {"chunks": [part for part in context["evidence"] if part.get("source_type") != "metadata"],
                "execution": request["context"], "retrieval_snapshot": frozen}


def _percentile(values, fraction):
    """Nearest-rank percentile; no interpolation or unobserved latency values."""
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def _performance(runtime):
    samples, included = [], []
    for iteration in range(runtime.iterations):
        case = runtime.cases[iteration % len(runtime.cases)]
        request = runtime.runner._request(case, "answers", "rag", runtime.top_k, runtime.filters)
        run = runtime.runner._invoke(runtime.answer, request, answers=True)
        packet = run.get("raw_callback_result", {})
        eligible = (run["status"] == "COLLECTED" and packet.get("prepared") is True
                    and packet.get("answer_model_invocation_count", 0) >= 1)
        if eligible:
            included.append(run["elapsed_seconds"])
        samples.append({"iteration": iteration + 1, "case_id": case["case_id"], "included": eligible,
                        "exclusion_reason": None if eligible else (run.get("error", {}).get("code") or "INDEX_NOT_PREPARED"),
                        "run": run})
    all_times = [sample["run"]["elapsed_seconds"] for sample in samples]
    exclusions = Counter(sample["exclusion_reason"] for sample in samples if not sample["included"])
    return runtime.runner.save_report("performance", {"schema_version": "evaluation-performance-v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(), "status": "COLLECTED" if len(included) >= 20 else "INCOMPLETE",
        "input_fingerprint": _hash([runtime.cases, runtime.iterations, runtime.top_k, runtime.filters]),
        "attempt_count": len(samples), "prepared_success_count": len(included), "excluded_count": len(samples) - len(included),
        "minimum_prepared_samples": 20, "sample_requirement_met": len(included) >= 20,
        "p50_seconds": _percentile(included, 0.5), "p95_seconds": _percentile(included, 0.95),
        "all_attempt_p50_seconds": _percentile(all_times, 0.5),
        "all_attempt_p95_seconds": _percentile(all_times, 0.95),
        "exclusion_counts": dict(sorted(exclusions.items())),
        "percentile_method": "nearest rank over included collector-observed fresh prepared-query latencies",
        "answer_cache": False, "collection_included": False, "samples": samples})


def review_evaluation(artifact, output_dir, review=None):
    """Prepare or record a private answer review without creating an application.

    Inputs accept a trusted server-side mapping or JSON file outside repositories.
    A template is a draft, with no inferred judgments. A completed review names
    HUMAN or AGENT_ASSISTED provenance and binds each judgment to the original
    response. No network, model invocation, or independent human authentication
    occurs, and no review changes an execution artifact or declares acceptance.
    """
    artifact = _private_json(artifact)
    template = answer_review_template(artifact)
    context = artifact["context"]
    keys = ("mode", "model_metadata", "settings", "revision", "dataset_fingerprint",
            "official_body_count", "external_process_authorized")
    if any(key not in context for key in keys):
        _fail("EVALUATION_ARTIFACT_INVALID")
    runner = ComparisonRunner(output_dir, **{key: context[key] for key in keys})
    if (runner.config_fingerprint != artifact.get("config_fingerprint")
            or context.get("config_fingerprint") != runner.config_fingerprint
            or context["mode"] != artifact["mode"]):
        _fail("EVALUATION_ARTIFACT_INVALID")
    if review is None:
        report = {"schema_version": "evaluation-answer-review-template-v1", "status": "DRAFT",
                  "source_artifact_fingerprint": artifact["artifact_fingerprint"],
                  "reviewer_type": None, "review_template": template,
                  "notice": "Complete review_template with explicit judgments; no outcomes were inferred."}
        comparison = "review-template"
    else:
        review = _private_json(review)
        # Permit the saved worksheet envelope as well as its review_template.
        if isinstance(review, dict) and review.get("schema_version") == "evaluation-answer-review-template-v1":
            review = review.get("review_template")
        report = assessed_answer_review(artifact, review)
        comparison = "review"
    result = runner.save_report(comparison, {**report, "created_at_utc": datetime.now(timezone.utc).isoformat()})
    return {key: result[key] for key in ("mode", "status", "acceptance_status", "output_path",
                                       "artifact_fingerprint", "source_artifact_fingerprint", "reviewer_type")}


def run_evaluation(app, cases, config):
    """Execute configured comparisons and return private artifact references.

    ``cases`` is a list (trusted server caller) or a JSON path outside every
    repository. ``config`` is a mapping or such a JSON path. Its required key is
    ``output_dir``. Defaults run all comparisons in MOCK, with 20 fresh prepared
    query attempts. LIVE requires the application's guarded real chat and
    embedding adapters and a compatible prepared official index. A raw chat
    verification alone is not a full RAG evaluation.
    """
    runtime = _Runtime(app, _private_json(cases), _private_json(config))
    cold = {"status": "NOT_MEASURED", "operations": [], "cold_collection_seconds": None,
            "note": "Local preparation and external collection timings are distinct; no collection duration is inferred."}
    if runtime.config.get("measure_cold_prepare", False):
        cold["status"] = "MEASURED_PREPARATION_OPERATION"
        cold["corpus_before"] = runtime.corpus
        distinct = {}
        for case in runtime.cases:
            distinct.setdefault((case["item_id"], _hash(case["applicability"])), case)
        collection_times = []
        for case in distinct.values():
            item_id = case["item_id"]
            started = time.perf_counter()
            try:
                with runtime._processing(item_id):
                    job = runtime.rag.prepare(runtime.user, item_id, applicability=case["applicability"], question=case["question"])
                result = {"item_id": item_id, "case_id": case["case_id"], "elapsed_seconds": time.perf_counter() - started,
                          "job": {key: job.get(key) for key in ("job_id", "status", "stage", "failure_code", "candidate_count", "body_count", "chunk_count", "revision")}}
                measured = job.get("collection_elapsed_seconds")
                if type(measured) in (int, float) and math.isfinite(measured) and measured >= 0:
                    result["collection_elapsed_seconds"] = measured
                    collection_times.append(measured)
            except Exception as exc:
                result = {"item_id": item_id, "elapsed_seconds": time.perf_counter() - started,
                          "error": {"code": _safe_code(exc)}}
            cold["operations"].append(result)
        if len(collection_times) == len(cold["operations"]):
            cold["cold_collection_seconds"] = sum(collection_times)
        # Freeze the actual resulting revision before any paired comparisons.
        runtime = _Runtime(app, runtime.cases, runtime.config)
        cold["corpus_after"] = runtime.corpus
    outputs = {}
    for comparison in runtime.comparisons:
        if comparison == "answers":
            artifact = runtime.runner.run_answers(runtime.cases, runtime.answer, runtime.answer)
        elif comparison == "retrieval":
            artifact = runtime.runner.run_retrieval(runtime.cases, runtime.retrieve, top_k=runtime.top_k, filters=runtime.filters)
        else:
            artifact = _performance(runtime)
        outputs[comparison] = {key: artifact.get(key) for key in ("output_path", "artifact_fingerprint", "status",
            "attempt_count", "collected_count", "failed_count", "prepared_success_count", "excluded_count", "p50_seconds", "p95_seconds")}
    report = runtime.runner.save_report("runtime", {"schema_version": "evaluation-runtime-v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "COLLECTED" if all(part["status"] == "COLLECTED" for part in outputs.values()) else "INCOMPLETE",
        "input_fingerprint": _hash([runtime.cases, runtime.config]), "outputs": outputs,
        "corpus_snapshot": runtime.corpus, "cold_preparation": cold,
        "execution_environment": {"chat": runtime.model_metadata, "embedding": runtime.embedding_metadata,
                                  "embedding_after": _metadata(runtime.embedding, embedding=True),
                                  "runtime": runtime.runtime_snapshot},
        "document_type_observations": list(runtime.observed.values()),
        "references": [{"case_id": case["case_id"], "reference": case.get("reference"),
                        "assessment": "NOT_ASSESSED", "provenance": "SUPPLIED_REFERENCE_NOT_MODEL_INPUT"}
                       for case in runtime.cases],
        "notice": "MOCK observations do not establish LIVE model/document quality. References and scoring require independent verification."})
    return {key: report[key] for key in ("mode", "status", "acceptance_status", "outputs", "output_path", "artifact_fingerprint")}
