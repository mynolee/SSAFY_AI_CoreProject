"""Item-scoped retrieval and guarded answers, with exactly two business nodes."""
from __future__ import annotations

import copy
from contextlib import closing, nullcontext, contextmanager
import json
from pathlib import Path
import re
import threading
import time
from typing import Any, TypedDict
import uuid

from .documents import (DocumentRegistry, OWN_ROLES, ROLES, APPLICABILITY_FIELDS,
                        applicability_issues, canonical_json, chunk_sections, digest, now, normalize)
from .errors import ServiceError
from .prompts import PromptStore, StructuredRunner, MockStructuredModel, validate_output
from .vectorstore import LocalVectorStore


class GraphState(TypedDict, total=False):
    item_id: str
    question: str
    user_id: str
    item_context: dict
    applicability_inputs: dict
    queries: list
    candidate_ids: list
    retrieved_chunks: list
    evidence_map: dict
    missing_information: list
    conflicts: list
    answer: dict
    cited_evidence_ids: list
    readiness: dict
    errors: list
    progress: list
    top_k: int
    strategy: str
    revision: str | None
    selection: dict
    collection_cache_hits: int
    allow_collection: bool
    use_answer_cache: bool
    retrieval_pairs: list
    retrieval_elapsed_seconds: float


def _error(code, message, retryable=False):
    return {"code": code, "message": message, "retryable": retryable}


class RagService:
    def __init__(self, catalog, db, settings, *, model=None, prompt_store=None,
                 source_policy=None, fetcher=None, embeddings=None, chunk_size=1000,
                 chunk_overlap=200, index_actor=None, collector=None, document_jobs=None,
                 default_top_k=6, default_strategy="similarity", mmr_fetch_k=30, mmr_lambda_mult=0.5):
        self.catalog, self.db, self.settings = catalog, db, settings
        self.model = model or MockStructuredModel()
        self.prompt_store = prompt_store or PromptStore(settings.config_dir)
        self.runner = StructuredRunner(self.prompt_store, self.model)
        self.registry = DocumentRegistry(catalog, db, settings, source_policy, fetcher)
        self.documents = self.registry
        self.index_actor = index_actor
        self.collector, self.document_jobs = collector, document_jobs
        if (type(default_top_k) is not int or not 1 <= default_top_k <= 30 or
                default_strategy not in {"similarity", "mmr"} or type(mmr_fetch_k) is not int or
                not default_top_k <= mmr_fetch_k <= 100 or isinstance(mmr_lambda_mult, bool) or
                not isinstance(mmr_lambda_mult, (int, float)) or not 0 <= mmr_lambda_mult <= 1):
            raise ServiceError("INVALID_INPUT", "Invalid retrieval configuration.", 422, False)
        self.default_top_k, self.default_strategy = default_top_k, default_strategy
        self.mmr_fetch_k, self.mmr_lambda_mult = mmr_fetch_k, mmr_lambda_mult
        self.chunk_size, self.chunk_overlap = chunk_size, chunk_overlap
        chunk_sections([], chunk_size, chunk_overlap)
        self.chunk_config_hash = digest({"size": chunk_size, "overlap": chunk_overlap})
        self._metadata_records = []
        for item in catalog.items:
            clean = {key: value for key, value in item.items() if key != "readiness"}
            self._metadata_records.append({"item_id": str(item["item_id"]), "text": item.get("index_text", canonical_json(clean))})
        self.dataset_hash = getattr(catalog, "dataset_hash", digest(self._metadata_records))
        if embeddings is not None:
            if not isinstance(getattr(embeddings, "is_remote", None), bool):
                raise ServiceError("MODEL_REMOTE_BLOCKED", "The embedding adapter must declare its processing location.", 503, False)
        self.vectors = LocalVectorStore(Path(settings.data_dir) / "chroma", embeddings)
        self._lock = threading.RLock()
        with self.db.transaction() as connection:
            for item in catalog.items:
                connection.execute("INSERT OR IGNORE INTO items(item_id,name,provider) VALUES(?,?,?)",
                    (item["item_id"], item.get("name", item["item_id"]), item.get("provider", "UNKNOWN")))
        if self.vectors.embeddings.is_remote:
            self._metadata_state()
        else:
            self.vectors.index_metadata(self._metadata_records, dataset_hash=self.dataset_hash)
            self._metadata_state()
        self.graph = self._build_graph()

    def _metadata_selection(self, item_ids=None):
        if item_ids is None:
            return self._metadata_records
        if (not isinstance(item_ids, list) or not item_ids or
                any(not isinstance(value, str) or not value.strip() for value in item_ids) or
                len(item_ids) != len(set(item_ids))):
            raise ServiceError("INVALID_INPUT", "Choose distinct catalog item identities.", 422, False)
        known = {record["item_id"]: record for record in self._metadata_records}
        if any(value not in known for value in item_ids):
            raise ServiceError("INVALID_INPUT", "A selected metadata identity is not in the catalog.", 422, False)
        return [known[value] for value in item_ids]

    def _metadata_state(self, item_ids=None):
        selected = self._metadata_selection(item_ids)
        prepared = self.vectors.prepared_metadata_ids(self._metadata_records, self.dataset_hash)
        self.metadata_count = len(prepared)
        self.metadata_complete = self.vectors.metadata_matches(self._metadata_records, self.dataset_hash)
        return {"metadata_count": self.metadata_count, "metadata_complete": self.metadata_complete,
            "metadata_index_matches": self.vectors.metadata_matches(selected, self.dataset_hash, exact=item_ids is None)}

    def prepare_metadata(self, actor, item_ids=None):
        """Prepare only explicitly chosen remote metadata; app startup is read-only."""
        self.registry.policy.require_capability(actor, "DATA_MANAGER")
        remote = self.vectors.embeddings.is_remote
        if remote and (not self.settings.remote_models or item_ids is None):
            raise ServiceError("MODEL_REMOTE_BLOCKED", "Remote metadata preparation requires enabled models and explicit item identities.", 503, False)
        selected = self._metadata_selection(item_ids)
        sources = sorted({self.catalog.get(record["item_id"]).get("source_id", "") for record in selected})
        with self.review_guard():
            if remote:
                for source_id in sources:
                    self.registry._require(source_id, "external_process", actor, self.dataset_hash)
            # Repeated preparation is a verified local read, including with an
            # adapter whose provider does not implement its own vector cache.
            pending = selected
            if remote:
                prepared = set(self.vectors.prepared_metadata_ids(selected, self.dataset_hash))
                pending = [record for record in selected if record["item_id"] not in prepared]
            if pending:
                pending_sources = sorted({self.catalog.get(record["item_id"]).get("source_id", "") for record in pending})
                with self._embedding_context([{"kind": "metadata", "source_id": source,
                        "version_id": self.dataset_hash, "actor": actor} for source in pending_sources]):
                    self.vectors.index_metadata(pending, dataset_hash=self.dataset_hash,
                        replace=not remote and item_ids is None)
            state = self._metadata_state([record["item_id"] for record in selected] if item_ids is not None else None)
        return {"status": "META_READY", **state, "selected_metadata_count": len(selected),
            "item_ids": [record["item_id"] for record in selected], "dataset_hash": self.dataset_hash,
            "embedding_model": self.vectors.embeddings.name}

    def _run_model(self, kind, variables, actor, item_id):
        contextualize = getattr(self.model, "processing_context", None)
        with contextualize(actor, item_id) if callable(contextualize) else nullcontext():
            return self.runner.run(kind, variables)

    def _embedding_context(self, contexts):
        embeddings = self.vectors.embeddings
        contextualize = getattr(embeddings, "processing_context", None)
        if callable(contextualize):
            return contextualize(contexts)
        if embeddings.is_remote and not getattr(embeddings, "is_mock", False):
            raise ServiceError("MODEL_REMOTE_BLOCKED", "Remote embedding processing context is unavailable.", 503, False)
        return nullcontext()

    def _query_context(self, item_id, actor):
        return [{"kind": "question", "source_id": self.catalog.get(item_id).get("source_id", ""),
                 "version_id": self.dataset_hash, "actor": actor}]

    def close(self):
        self.vectors.close()

    @contextmanager
    def review_guard(self):
        """Fence source rights and local revision through a domain commit."""
        with self._lock:
            read_guard = getattr(self.registry.policy, "read_guard", None)
            with read_guard() if callable(read_guard) else nullcontext():
                yield

    def corpus_summary(self, item_ids=None):
        metadata = self._metadata_state(item_ids)
        with closing(self.db.connect()) as connection:
            rows = connection.execute("SELECT v.body_hash,v.version_id,d.document_id,d.review_json FROM document_versions v JOIN documents d ON d.document_id=v.document_id").fetchall()
        official, synthetic = [], []
        for row in rows:
            (synthetic if json.loads(row["review_json"]).get("is_synthetic", False) else official).append(dict(row))
        manifest = self._active()
        chunks = self.vectors.docs.get(where={"revision": manifest["revision"]}, include=["metadatas"])["metadatas"] if manifest else []
        return {**metadata, "revision": manifest["revision"] if manifest else None,
            "active_index": {key: manifest[key] for key in ("revision", "embedding_model", "dimension", "dataset_hash")} if manifest else None,
            "official_document_body_count": len({row["body_hash"] for row in official}),
            "official_document_version_count": len(official), "synthetic_document_body_count": len({row["body_hash"] for row in synthetic}),
            "official_document_chunk_count": sum(row["source_type"] == "official_document" for row in chunks),
            "synthetic_document_chunk_count": sum(row["source_type"] == "synthetic_document" for row in chunks),
            "embedding_model": self.vectors.embeddings.name, "embedding_dimension": self.vectors.embeddings.dimension}

    def _build_graph(self):
        try:
            from langgraph.graph import StateGraph, START, END
        except ImportError as exc:
            raise ServiceError("INDEX_EMPTY", "Local LangGraph dependency is unavailable.", 503, False) from exc
        graph = StateGraph(GraphState)
        graph.add_node("retrieve", self._retrieve)
        graph.add_node("generate", self._generate)
        graph.add_edge(START, "retrieve")
        graph.add_edge("retrieve", "generate")
        graph.add_edge("generate", END)
        return graph.compile()

    def _active(self):
        with closing(self.db.connect()) as connection:
            row = connection.execute("SELECT * FROM index_manifests WHERE active=1").fetchone()
        if row is None:
            return None
        manifest = dict(row)
        # A process with changed model/config must never query an incompatible revision.
        if (manifest["dataset_hash"] != self.dataset_hash or
                manifest["embedding_model"] != self.vectors.embeddings.name or
                manifest["dimension"] != self.vectors.embeddings.dimension or
                manifest["chunk_config_hash"] != self.chunk_config_hash):
            return None
        return manifest

    def _cache_get(self, kind, key):
        with closing(self.db.connect()) as connection:
            row = connection.execute("SELECT * FROM rag_cache WHERE cache_kind=? AND cache_key=?", (kind, key)).fetchone()
        if row is None or row["status"] == "FAILED" or row["expires_at"] and row["expires_at"] < time.time():
            return None
        return {"status": row["status"], "value": json.loads(row["value_json"])}

    def _cache_put(self, kind, key, status, value, ttl=None):
        with self.db.transaction() as connection:
            connection.execute("INSERT OR REPLACE INTO rag_cache(cache_kind,cache_key,status,value_json,expires_at) VALUES(?,?,?,?,?)",
                (kind, key, status, canonical_json(value), time.time() + ttl if ttl else None))

    def _job_update(self, job_id, **fields):
        allowed = {"status", "stage", "failure_code", "candidate_count", "body_count", "chunk_count", "revision"}
        values = {key: value for key, value in fields.items() if key in allowed}
        values["updated_at"] = now()
        with self.db.transaction() as connection:
            connection.execute("UPDATE index_jobs SET " + ",".join(key + "=?" for key in values) + " WHERE job_id=?",
                               (*values.values(), job_id))

    def enqueue_prepare(self, user_id, item_id, applicability=None, question=None):
        self.catalog.get(item_id)
        inputs = self._validate_inputs(applicability)
        if self.document_jobs is None:
            raise ServiceError("JOBS_UNAVAILABLE", "Document background processing is not configured.", 503, False)
        question = self._preparation_question(question)
        return self.document_jobs.enqueue(user_id, item_id, {"question": question, "applicability": inputs})

    def retry_prepare(self, user_id, job_id):
        if self.document_jobs is None:
            raise ServiceError("JOBS_UNAVAILABLE", "Document background processing is not configured.", 503, False)
        return self.document_jobs.retry(user_id, job_id)

    def run_prepare(self, request, progress):
        """Fixed queue handler. Queue claims, retries and fences all progress writes."""
        if not isinstance(request, dict) or not all(isinstance(request.get(key), str) and request[key]
                for key in ("job_id", "requester", "item_id")):
            raise ServiceError("INVALID_INPUT", "Invalid document job request.", 422, False)
        if request.get("dataset_hash", self.dataset_hash) != self.dataset_hash:
            return {"status": "BLOCKED", "stage": "DOC_BLOCKED", "failure_code": "DATASET_CHANGED"}
        return self._prepare(request["requester"], request["item_id"],
            self._validate_inputs(request.get("applicability")), self._preparation_question(request.get("question")),
            job_id=request["job_id"], progress=progress)

    @staticmethod
    def _preparation_question(question):
        if question is None:
            return "보증 이전 조건과 적용 계약 문서를 준비해 주세요."
        if not isinstance(question, str) or not question.strip() or len(question) > 5000:
            raise ServiceError("INVALID_INPUT", "A bounded preparation question is required.", 422, False)
        return question.strip()

    def prepare(self, user_id, item_id, applicability=None, question=None):
        """Synchronous compatibility entry point; UIs enqueue persistent jobs."""
        return self._prepare(user_id, item_id, self._validate_inputs(applicability), self._preparation_question(question))

    def _prepare(self, user_id, item_id, inputs, question, *, job_id=None, progress=None, collect=True):
        """Request and execute a bounded local indexing job.

        The local worker only reads already registered/reviewed bodies. External
        collection is an explicit DATA_MANAGER operation on DocumentRegistry.
        The requesting user never gains the worker's manager capability.
        """
        self.catalog.get(item_id)
        queued = job_id is not None
        job_id = job_id or uuid.uuid4().hex
        stamp = now()
        if not queued:
            with self.db.transaction() as connection:
                connection.execute("INSERT INTO index_jobs(job_id,requester,item_id,status,stage,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (job_id, user_id, item_id, "RUNNING", "CHECKING_DOCUMENTS", stamp, stamp))
        outcome = {"job_id": job_id, "item_id": item_id, "status": "RUNNING", "stage": "CHECKING_DOCUMENTS",
                   "candidate_count": 0, "body_count": 0, "chunk_count": 0, "revision": None, "failure_code": None,
                   "collection_elapsed_seconds": None, "index_elapsed_seconds": None}
        def update(**fields):
            outcome.update(fields)
            if progress is not None:
                progress(fields.get("stage", outcome["stage"]), **{key: value for key, value in fields.items()
                    if key in {"candidate_count", "body_count", "chunk_count", "revision"}})
            else:
                self._job_update(job_id, **fields)
        with self._lock:
            revision = None
            index_started = None
            try:
                if collect and self.collector is not None:
                    collection_started = time.perf_counter()
                    try:
                        prepared = self._source_pipeline(user_id, item_id, question, inputs,
                            progress=lambda stage, **counts: update(stage=stage, **counts))
                    finally:
                        outcome["collection_elapsed_seconds"] = time.perf_counter() - collection_started
                    if prepared["collection_status"] in {"FAILED", "BLOCKED"}:
                        first = prepared["collection_errors"][0] if prepared["collection_errors"] else {}
                        raise ServiceError(first.get("code", "FETCH_FAILED"), "Approved document collection could not complete.", 409, True)
                index_started = time.perf_counter()
                own = self.registry.candidates(item_id)
                update(stage="CHECKING_DOCUMENTS", candidate_count=len(own))
                if not any(candidate["own_document"] for candidate in own):
                    raise ServiceError("OWN_DOCUMENT_MISSING", "No approved document belonging to this item is available.", 409, False)
                actor = self.index_actor or user_id
                self.registry.policy.require_capability(actor, "DATA_MANAGER")
                if self.vectors.embeddings.is_remote and not self.settings.remote_models:
                    raise ServiceError("MODEL_REMOTE_BLOCKED", "Remote embeddings are disabled.", 503, False)
                prepared_metadata = set(self.vectors.prepared_metadata_ids(self._metadata_records, self.dataset_hash))
                if self.vectors.embeddings.is_remote and item_id not in prepared_metadata:
                    raise ServiceError("INDEX_EMPTY", "Prepare this item's metadata before document embeddings.", 409, False)
                candidates = self.registry.candidates()
                if self.vectors.embeddings.is_remote:
                    candidates = [candidate for candidate in candidates if candidate["item_id"] in prepared_metadata]
                chunks = []
                versions = []
                update(stage="PARSING_STORED_BODIES")
                for candidate in candidates:
                    try:
                        sections = self.registry.sections(candidate, actor)
                        # Do not index documents whose text cannot be shown.
                        self.registry._require(candidate["source_id"], "display_excerpt", actor, candidate["version_id"])
                        if self.vectors.embeddings.is_remote:
                            self.registry._require(candidate["source_id"], "external_process", actor, candidate["version_id"])
                            self.registry._require(self.catalog.get(candidate["item_id"]).get("source_id", ""), "external_process", actor, self.dataset_hash)
                    except ServiceError:
                        if candidate["item_id"] == item_id and candidate["own_document"]:
                            raise
                        continue
                    versions.append(candidate["version_id"])
                    is_synthetic = json.loads(candidate["review_json"]).get("is_synthetic", False)
                    for ordinal, chunk in enumerate(chunk_sections(sections, self.chunk_size, self.chunk_overlap)):
                        evidence_id = "doc-" + digest([candidate["item_id"], candidate["version_id"], self.chunk_config_hash, candidate["role"], ordinal])
                        chunks.append({**chunk, "evidence_id": evidence_id,
                            "item_id": candidate["item_id"], "version_id": candidate["version_id"],
                            "document_version_id": candidate["version_id"], "document_id": candidate["document_id"],
                            "source_id": candidate["source_id"], "title": candidate["title"],
                            "url": candidate["canonical_url"], "source_url": candidate["canonical_url"],
                            "type": candidate["document_type"], "role": candidate["role"],
                            "source_type": "synthetic_document" if is_synthetic else "official_document"})
                if not chunks or not any(chunk["item_id"] == item_id and chunk["role"] in OWN_ROLES for chunk in chunks):
                    raise ServiceError("OWN_DOCUMENT_MISSING", "The item's own document could not be indexed.", 409, False)
                revision = digest([self.dataset_hash, sorted(set(versions)),
                                   sorted((chunk["item_id"], chunk["version_id"], chunk["role"]) for chunk in chunks), self.chunk_config_hash,
                                   self.vectors.embeddings.name, self.vectors.embeddings.dimension])
                active = self._active()
                with closing(self.db.connect()) as connection:
                    previous = connection.execute("SELECT revision FROM index_manifests WHERE active=1").fetchone()
                obsolete_revision = previous["revision"] if previous else None
                if not active or active["revision"] != revision:
                    update(stage="INDEXING", body_count=len({candidate["version_id"] for candidate in own}))
                    with self._embedding_context([{"kind": "document", "source_id": source,
                            "version_id": version, "actor": actor}
                            for source, version in sorted({(chunk["source_id"], chunk["version_id"]) for chunk in chunks})]):
                        self.vectors.stage_documents(revision, chunks)
                    # Verify item filters before exposing a new revision.
                    for linked_item in set(chunk["item_id"] for chunk in chunks):
                        with self._embedding_context(self._query_context(linked_item, actor)):
                            found = self.vectors.search(linked_item, revision, linked_item, 1)
                        if not found:
                            raise ServiceError("INDEX_EMPTY", "Retrieval validation of the prepared revision failed.", 503, True)
                    self.registry.policy.require_capability(actor, "DATA_MANAGER")
                    with self.db.transaction() as connection:
                        checker = getattr(progress, "check", None)
                        if callable(checker):
                            checker(connection)
                        connection.execute("UPDATE index_manifests SET active=0 WHERE active=1")
                        connection.execute("INSERT INTO index_manifests(revision,dataset_hash,version_ids_json,embedding_model,dimension,chunk_config_hash,success_count,failure_count,activated_at,active) VALUES(?,?,?,?,?,?,?,?,?,1) ON CONFLICT(revision) DO UPDATE SET active=1,activated_at=excluded.activated_at",
                            (revision, self.dataset_hash, canonical_json(sorted(set(versions))), self.vectors.embeddings.name,
                             self.vectors.embeddings.dimension, self.chunk_config_hash, len(chunks), 0, now()))
                    # Delete only the known previous active revision. A second
                    # process may be staging a different revision concurrently.
                    if obsolete_revision and obsolete_revision != revision:
                        with self.db.transaction() as connection:
                            if not connection.execute("SELECT 1 FROM index_manifests WHERE revision=? AND active=1", (obsolete_revision,)).fetchone():
                                self.vectors.discard_revision(obsolete_revision)
                update(status="COMPLETED", stage="INDEX_READY", body_count=len({candidate["version_id"] for candidate in own}),
                    chunk_count=sum(chunk["item_id"] == item_id for chunk in chunks), revision=revision)
                if collect and self.collector is not None:
                    self.collector.mark_indexed(prepared["collection_result"], revision)
            except ServiceError as exc:
                # Staging IDs are shared by identical deterministic revisions.
                # A failed/stale worker must not delete another worker's rows.
                # Unpublished rows remain hidden and are reused on a retry.
                update(status="BLOCKED", stage="DOC_BLOCKED", failure_code=exc.code)
            except Exception:
                update(status="FAILED", stage="INDEXING", failure_code="INDEX_EMPTY")
            finally:
                if index_started is not None:
                    outcome["index_elapsed_seconds"] = time.perf_counter() - index_started
        if queued:
            return outcome
        return {**self.job(user_id, job_id), "collection_elapsed_seconds": outcome["collection_elapsed_seconds"],
                "index_elapsed_seconds": outcome["index_elapsed_seconds"]}

    def job(self, user_id, job_id):
        if self.document_jobs is not None:
            return self.document_jobs.job(user_id, job_id)
        with closing(self.db.connect()) as connection:
            row = connection.execute("SELECT * FROM index_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise ServiceError("NO_RESULTS", "Indexing job was not found.", 404, False)
        if row["requester"] != user_id:
            self.registry.policy.require_capability(user_id, "DATA_MANAGER")
        return dict(row)

    def _source_pipeline(self, user_id, item_id, question, inputs, *, progress=None, allow_collection=True):
        """Query -> discover -> select -> fetch, all inside the retrieve service.

        Discovery yields registered targets, including own documents with no
        body yet. The model only selects IDs; the collector rechecks its trusted
        inventory before it can fetch or establish an item/version link.
        """
        emit = progress or (lambda stage, **counts: None)
        item = self.catalog.get(item_id)
        if not getattr(self.model, "is_mock", False):
            if not self.settings.remote_models:
                raise ServiceError("KEY_MISSING", "Remote model processing is disabled.", 503, False)
            self.registry._require(item.get("source_id", ""), "external_process", user_id, self.dataset_hash)
        emit("QUERYING")
        query = self._run_model("query", {"item": item, "question": question, "applicability": inputs}, user_id, item_id)
        existing = self.registry.candidates(item_id)
        discovered = []
        if self.collector is not None and allow_collection:
            emit("DISCOVERING")
            discovered = self.collector.discover(item_id, question, query["queries"], inputs, user_id)
            if not isinstance(discovered, list) or len(discovered) > 100:
                raise ServiceError("SOURCE_BLOCKED", "Registered candidate discovery returned an invalid result.", 422, False)
        by_id = {candidate["id"]: candidate for candidate in existing}
        discovery_ids = set()
        for candidate in discovered:
            if (not isinstance(candidate, dict) or not all(isinstance(candidate.get(key), str) and candidate[key]
                    for key in ("id", "source_id", "title", "role")) or candidate["role"] not in ROLES or
                    candidate.get("item_id", item_id) != item_id or candidate["id"] in discovery_ids or
                    type(candidate.get("own_document")) is not bool):
                raise ServiceError("SOURCE_BLOCKED", "Registered candidate identity validation failed.", 422, False)
            prior = by_id.get(candidate["id"])
            if prior and (prior["source_id"] != candidate["source_id"] or prior["role"] != candidate["role"] or
                    prior["version_id"] != candidate.get("version_id") or prior["canonical_url"] != candidate.get("url")):
                raise ServiceError("SOURCE_BLOCKED", "Registered candidate identities conflict.", 403, False)
            self.registry.policy.source(candidate["source_id"])
            discovery_ids.add(candidate["id"])
            by_id[candidate["id"]] = {**(prior or {}), **candidate}
        candidates = list(by_id.values())
        if not getattr(self.model, "is_mock", False):
            for candidate in candidates:
                self.registry._require(candidate["source_id"], "external_process", user_id, candidate.get("version_id"))
        emit("SELECTING", candidate_count=len(candidates))
        public_candidates = [{"id": candidate["id"], "title": candidate["title"], "role": candidate["role"],
                              "own_document": candidate["own_document"]} for candidate in candidates]
        selection = self._run_model("selection", {"item": item, "question": question,
            "applicability": inputs, "candidates": public_candidates}, user_id, item_id)
        validate_output("selection", selection, allowed_candidate_ids={candidate["id"] for candidate in candidates})
        selected = set(selection["selected_candidate_ids"]) | {candidate["id"] for candidate in candidates if candidate["own_document"]}
        collected = {"status": "INDEXED", "linked": [], "cache_hits": 0, "errors": []}
        if self.collector is not None and allow_collection:
            emit("FETCHING", candidate_count=len(discovered))
            collected = self.collector.collect(item_id, question, query["queries"], inputs, user_id,
                selected_candidate_ids=sorted(selected & discovery_ids), candidates=discovered)
            if (not isinstance(collected, dict) or collected.get("status") not in
                    {"FETCHED", "INDEXED", "FAILED", "BLOCKED", "NO_RESULTS"} or
                    not isinstance(collected.get("linked", []), list)):
                raise ServiceError("FETCH_FAILED", "Document collection returned an invalid result.", 503, False)
        post = self.registry.candidates(item_id)
        selected_post = {candidate["id"] for candidate in post if candidate["id"] in selected or candidate["own_document"]}
        for link in collected.get("linked", []):
            origin = by_id.get(link.get("candidate_id")) if isinstance(link, dict) else None
            if (origin is None or origin["id"] not in selected or link.get("item_id") != item_id or
                    link.get("role") != origin["role"]):
                raise ServiceError("SOURCE_BLOCKED", "A collected version does not belong to this candidate selection.", 403, False)
            matches = [candidate for candidate in post if candidate["version_id"] == link.get("version_id") and
                candidate["role"] == link["role"] and candidate["source_id"] == origin["source_id"] and
                candidate["canonical_url"] == origin.get("url", origin.get("canonical_url")) and
                candidate["document_id"] == origin.get("document_id")]
            if not matches:
                raise ServiceError("SOURCE_BLOCKED", "A collected version failed its item and source boundary.", 403, False)
            selected_post.update(candidate["id"] for candidate in matches)
        safe_errors = []
        for error in collected.get("errors", []):
            code = error.get("code") if isinstance(error, dict) else None
            if code not in {"SOURCE_BLOCKED", "OWN_DOCUMENT_MISSING", "FETCH_FAILED", "PARSE_FAILED", "NO_RESULTS", "FORBIDDEN", "APPLICABILITY_UNKNOWN", "INDEX_EMPTY"}:
                code = "FETCH_FAILED"
            safe_errors.append(_error(code, "An approved source could not be prepared.", code in {"FETCH_FAILED", "NO_RESULTS", "INDEX_EMPTY"}))
        return {"query": query, "selection": selection, "candidates": post, "selected": selected_post,
            "discovered_count": len(discovered), "collection_result": collected,
            "collection_status": collected["status"], "collection_errors": safe_errors,
            "collection_cache_hits": collected.get("cache_hits", 0)}

    def _retrieve(self, state):
        item = self.catalog.get(state["item_id"])
        inputs = state["applicability_inputs"]
        allow_collection = state.get("allow_collection", True)
        pipeline = self._source_pipeline(state["user_id"], state["item_id"], state["question"], inputs,
                                         allow_collection=allow_collection)
        query, selection = pipeline["query"], pipeline["selection"]
        candidates, selected = pipeline["candidates"], pipeline["selected"]
        missing = list(dict.fromkeys(query["missing_information"] + selection["missing_information"]))
        conflicts = list(selection["conflicts"])
        errors = list(pipeline["collection_errors"])
        if pipeline["collection_status"] in {"FAILED", "BLOCKED"}:
            missing.append("approved_collection:required_document_unavailable")
        if self.collector is not None and allow_collection and pipeline["collection_status"] not in {"FAILED", "BLOCKED"}:
            active = self._active()
            revision = active["revision"] if active else None
            needs_index = any(candidate["id"] in selected and not self.vectors.has_document(
                state["item_id"], revision, candidate["version_id"], candidate["role"]) for candidate in candidates)
            if needs_index:
                indexed = self._prepare(state["user_id"], state["item_id"], inputs, state["question"], collect=False)
                if indexed["status"] != "COMPLETED":
                    missing.append("approved_collection:index_required")
                    errors.append(_error(indexed.get("failure_code") or "INDEX_EMPTY", "The collected document index is not ready.", True))
            active = self._active()
            if (active and pipeline["collection_status"] in {"FETCHED", "INDEXED"} and selected and
                    (not needs_index or indexed["status"] == "COMPLETED")):
                self.collector.mark_indexed(pipeline["collection_result"], active["revision"])
        manifest = self._active()
        revision = manifest["revision"] if manifest else None
        active_versions = set(json.loads(manifest["version_ids_json"])) if manifest else set()
        usable = []
        for candidate in candidates:
            if (candidate["id"] not in selected or candidate["version_id"] not in active_versions or
                    not self.vectors.has_document(state["item_id"], revision, candidate["version_id"], candidate["role"])):
                continue
            try:
                self.registry._require(candidate["source_id"], "local_embed", state["user_id"], candidate["version_id"])
                self.registry._require(candidate["source_id"], "display_excerpt", state["user_id"], candidate["version_id"])
                issues, clashes = applicability_issues(inputs, json.loads(candidate["applicability_json"]))
                missing.extend(issues)
                conflicts.extend(clashes)
                usable.append(candidate)
            except ServiceError:
                errors.append(_error("SOURCE_BLOCKED", "The source is not approved for this action."))
        if not any(candidate["own_document"] for candidate in usable):
            missing.append("own_document:approved_indexed_body_required")
            errors.append(_error("OWN_DOCUMENT_MISSING", "The selected item's own approved document is unavailable."))
        missing.extend(field for field in APPLICABILITY_FIELDS if not inputs.get(field))
        if not revision:
            errors.append(_error("INDEX_EMPTY", "No compatible document index is active.", True))
        allowed_pairs = {(candidate["version_id"], candidate["role"]) for candidate in usable}
        if self.vectors.embeddings.is_remote:
            if not self.settings.remote_models:
                raise ServiceError("MODEL_REMOTE_BLOCKED", "Remote embeddings are disabled.", 503, False)
            if not self.vectors.metadata_matches(self._metadata_selection([state["item_id"]]), self.dataset_hash, exact=False):
                raise ServiceError("INDEX_EMPTY", "The selected item's metadata index is not ready.", 409, False)
            self.registry._require(item.get("source_id", ""), "external_process", state["user_id"], self.dataset_hash)
            for candidate in usable:
                self.registry._require(candidate["source_id"], "external_process", state["user_id"], candidate["version_id"])
        ranking_start = time.perf_counter()
        if revision:
            with self._embedding_context(self._query_context(state["item_id"], state["user_id"])):
                chunks = self.vectors.search(state["item_id"], revision, state["question"], state["top_k"], state["strategy"],
                    fetch_k=self.mmr_fetch_k, lambda_mult=self.mmr_lambda_mult, allowed_pairs=allowed_pairs)
        else:
            chunks = []
        ranking_elapsed = time.perf_counter() - ranking_start
        trusted = {(candidate["version_id"], candidate["role"]): candidate for candidate in usable}
        safe_chunks = []
        for chunk in chunks:
            candidate = trusted.get((chunk.get("version_id"), chunk.get("role")))
            expected_type = "synthetic_document" if candidate and json.loads(candidate["review_json"]).get("is_synthetic", False) else "official_document"
            if (candidate and chunk.get("item_id") == state["item_id"] and chunk.get("revision") == revision
                    and chunk.get("source_type") == expected_type and chunk.get("source_id") == candidate["source_id"]
                    and chunk.get("url") == candidate["canonical_url"]):
                safe_chunks.append(chunk)
            else:
                errors.append(_error("MODEL_EVIDENCE_INVALID", "Retrieved evidence failed its item and revision boundary."))
                missing.append("validated_item_evidence_required")
        chunks = safe_chunks
        # One selected metadata record is always separate from document TopK.
        meta = next(record for record in self._metadata_records if record["item_id"] == state["item_id"])
        metadata_evidence = {"evidence_id": "meta-" + state["item_id"], "item_id": state["item_id"],
            "title": item.get("name", item.get("product_name", state["item_id"])), "source_type": "metadata",
            "type": "METADATA", "role": "METADATA", "revision": revision or "META_ONLY", "text": meta["text"],
            "url": item.get("source_url"), "source_id": item.get("source_id"), "section": item.get("source_locator")}
        evidence = [metadata_evidence] + chunks
        for index, entry in enumerate(evidence, 1):
            entry["number"] = index
        status = "INDEX_READY" if any(candidate["own_document"] for candidate in usable) else "DOC_BLOCKED"
        # Cache collection identity independently from the question; no network
        # call is ever made by this lookup. Stored body reuse survives failures.
        collection_cache_hits = 0
        cache_sources = {candidate["source_id"] for candidate in candidates} if state.get("use_answer_cache", True) else set()
        for source_id in cache_sources:
            cache_key = digest([source_id, state["item_id"], inputs,
                                sorted(normalize(term).casefold() for term in query["queries"]),
                                sorted(candidate["version_id"] for candidate in candidates)])
            cached_collection = self._cache_get("collection", cache_key)
            expected_status = "INDEXED" if revision else ("FETCHED" if candidates else "NO_RESULTS")
            if cached_collection and cached_collection["status"] == expected_status:
                collection_cache_hits += 1
            else:
                self._cache_put("collection", cache_key, expected_status,
                    {"candidate_ids": sorted(selected), "version_ids": sorted(active_versions)}, ttl=None if candidates else 60)
        if self.collector is not None and allow_collection:
            hits = pipeline["collection_cache_hits"]
            collection_cache_hits = hits if type(hits) is int and hits >= 0 else 0
        return {"item_context": item, "queries": query["queries"], "candidate_ids": sorted(selected),
            "selection": selection, "retrieved_chunks": chunks, "evidence_map": {entry["evidence_id"]: entry for entry in evidence},
            "missing_information": list(dict.fromkeys(missing)), "conflicts": list(dict.fromkeys(conflicts)),
            "readiness": {"status": status, "reasons": list(dict.fromkeys(error["code"] for error in errors))},
            "errors": errors, "revision": revision,
            "collection_cache_hits": collection_cache_hits,
            "retrieval_pairs": sorted(allowed_pairs),
            "retrieval_elapsed_seconds": ranking_elapsed,
            "progress": [{"stage": "RETRIEVING", "status": "COMPLETED"}, {"stage": "GENERATING", "status": "RUNNING"}]}

    def _generate(self, state):
        evidence_map = state["evidence_map"]
        # An explicitly remote adapter additionally requires each source's
        # external-processing review. The default model never uses the network.
        if not getattr(self.model, "is_mock", False):
            if not self.settings.remote_models:
                raise ServiceError("KEY_MISSING", "Remote model processing is disabled.", 503, False)
            for entry in state["retrieved_chunks"]:
                self.registry._require(entry["source_id"], "external_process", state["user_id"], entry["version_id"])
        variables = {"item": state["item_context"], "question": state["question"],
            "applicability": state["applicability_inputs"], "evidence": list(evidence_map.values()),
            "missing_information": state["missing_information"], "conflicts": state["conflicts"]}
        cache_key = digest([self.dataset_hash, state["user_id"], state["item_id"], state["applicability_inputs"],
            normalize(state["question"]).casefold(), state["revision"], self.vectors.embeddings.name,
            self.chunk_config_hash, getattr(self.model, "model_id", type(self.model).__name__),
            self.prompt_store.load("answer"), variables["evidence"], state["missing_information"], state["conflicts"]])
        use_cache = state.get("use_answer_cache", True)
        cached = self._cache_get("answer", cache_key) if use_cache else None
        if cached:
            answer = cached["value"]
        else:
            try:
                answer = self._run_model("answer", variables, state["user_id"], state["item_id"])
            except ServiceError:
                if use_cache:
                    self._cache_put("answer", cache_key, "FAILED", {})
                raise
        validate_output("answer", answer, allowed_evidence_ids=set(evidence_map))
        numbers = {evidence_map[eid]["number"] for eid in answer["cited_evidence_ids"]}
        markers = {int(value) for value in re.findall(r"\[(\d+)\]", answer["answer"])}
        if markers != numbers:
            raise ServiceError("MODEL_EVIDENCE_INVALID", "Citation numbers must match the server evidence references.", 422, False)
        answer["missing_information"] = list(dict.fromkeys(state["missing_information"] + answer["missing_information"]))
        answer["conflicts"] = list(dict.fromkeys(state["conflicts"] + answer["conflicts"]))
        for condition in answer["conditions"]:
            if not any(evidence_map[eid]["source_type"] in {"official_document", "synthetic_document"}
                       and evidence_map[eid]["role"] in {"SERVICE_TERMS", "INSURANCE_TERMS", "TRANSFER_GUIDE"}
                       for eid in condition["evidence_ids"]):
                raise ServiceError("MODEL_OUTPUT_INVALID", "A condition requires applicable document evidence.", 422, False)
        if not cached and use_cache:
            self._cache_put("answer", cache_key, "INDEXED", answer)
        transfer_evidence = [evidence_map[eid] for eid in answer["cited_evidence_ids"]
            if evidence_map[eid]["source_type"] in {"official_document", "synthetic_document"}
            and evidence_map[eid]["role"] in {"SERVICE_TERMS", "INSURANCE_TERMS", "TRANSFER_GUIDE"}]
        if not transfer_evidence:
            answer["missing_information"].append("applicable_transfer_terms:evidence_required")
        answer["missing_information"] = list(dict.fromkeys(answer["missing_information"]))
        if answer["missing_information"] or answer["conflicts"] or state["readiness"]["status"] != "INDEX_READY":
            if answer["classification"] != "NEEDS_INFORMATION":
                answer["answer"] = "이전 가능 여부를 확정할 수 없습니다. 적용 계약, 필수 문서와 미확인 사항을 확인해 주세요."
                if numbers:
                    answer["answer"] += " 검토 자료: " + " ".join(f"[{number}]" for number in sorted(numbers))
            answer["classification"] = "NEEDS_INFORMATION"
        if not answer["cited_evidence_ids"]:
            answer["answer"] += "\n확인 가능한 근거를 찾지 못했습니다."
        if getattr(self.model, "is_mock", False) and "MOCK" not in answer["answer"]:
            answer["answer"] = "[MOCK 결정적 시연] " + answer["answer"]
        return {"answer": answer, "cited_evidence_ids": answer["cited_evidence_ids"],
                "progress": [{"stage": "RETRIEVING", "status": "COMPLETED"}, {"stage": "GENERATING", "status": "COMPLETED"}]}

    def _retrieval_settings(self, top_k, strategy):
        top_k = self.default_top_k if top_k is None else top_k
        strategy = self.default_strategy if strategy is None else strategy
        if type(top_k) is not int or not 1 <= top_k <= 30 or strategy not in {"similarity", "mmr"}:
            raise ServiceError("INVALID_INPUT", "Invalid retrieval settings.", 422, False)
        return top_k, strategy

    def evaluation_context(self, user_id, item_id, question, applicability=None, top_k=None, strategy=None,
                           *, allow_collection=False, cache=False, collection=None):
        """Fresh model query/selection over a frozen corpus; no answer generation."""
        self.catalog.get(item_id)
        if (not isinstance(user_id, str) or not user_id or not isinstance(question, str) or
                not question.strip() or len(question) > 5000):
            raise ServiceError("INVALID_INPUT", "A bounded question is required.", 422, False)
        if collection is not None:
            allow_collection = collection
        if type(allow_collection) is not bool or type(cache) is not bool:
            raise ServiceError("INVALID_INPUT", "Invalid evaluation settings.", 422, False)
        top_k, strategy = self._retrieval_settings(top_k, strategy)
        state = self._retrieve({"user_id": user_id, "item_id": item_id, "question": question,
            "applicability_inputs": self._validate_inputs(applicability), "top_k": top_k, "strategy": strategy,
            "allow_collection": allow_collection, "use_answer_cache": cache})
        return {"item": state["item_context"], "evidence": list(state["evidence_map"].values()), "revision": state["revision"],
            "readiness": state["readiness"], "queries": state["queries"], "selection": state["selection"],
            "missing_information": state["missing_information"], "conflicts": state["conflicts"], "errors": state["errors"],
            "retrieval_elapsed_seconds": state["retrieval_elapsed_seconds"],
            "retrieval_filters": {"item_id": item_id, "revision": state["revision"], "allowed_pairs": state["retrieval_pairs"]}}

    def retrieve_evidence(self, user_id, item_id, question, applicability=None, top_k=None, strategy=None):
        return self.evaluation_context(user_id, item_id, question, applicability, top_k, strategy,
                                       allow_collection=False, cache=False)

    def compare_retrieval(self, user_id, item_id, question, applicability=None, top_k=None):
        """Similarity/MMR share one actual query/selection and identical filters."""
        top_k, _ = self._retrieval_settings(top_k, "similarity")
        common_start = time.perf_counter()
        context = self.evaluation_context(user_id, item_id, question, applicability, top_k, "similarity",
                                          allow_collection=False, cache=False)
        common_elapsed = time.perf_counter() - common_start - context["retrieval_elapsed_seconds"]
        revision = context["revision"]
        pairs = {tuple(pair) for pair in context["retrieval_filters"]["allowed_pairs"]}
        candidates = {(candidate["version_id"], candidate["role"]): candidate for candidate in self.registry.candidates(item_id)}
        for pair in pairs:
            candidate = candidates.get(pair)
            if candidate is None:
                raise ServiceError("SOURCE_BLOCKED", "The evaluation candidate scope changed.", 409, False)
            for action in ("local_embed", "display_excerpt"):
                self.registry._require(candidate["source_id"], action, user_id, candidate["version_id"])
            if self.vectors.embeddings.is_remote:
                self.registry._require(candidate["source_id"], "external_process", user_id, candidate["version_id"])
        active = self._active()
        if revision and (not active or active["revision"] != revision):
            raise ServiceError("INDEX_EMPTY", "The evaluation revision changed.", 409, True)
        ranking_start = time.perf_counter()
        if revision:
            if self.vectors.embeddings.is_remote:
                self.registry._require(self.catalog.get(item_id).get("source_id", ""), "external_process", user_id, self.dataset_hash)
            with self._embedding_context(self._query_context(item_id, user_id)):
                ranked = self.vectors.search(item_id, revision, question, top_k, "mmr", fetch_k=self.mmr_fetch_k,
                    lambda_mult=self.mmr_lambda_mult, allowed_pairs=pairs)
        else:
            ranked = []
        mmr_elapsed = time.perf_counter() - ranking_start
        for chunk in ranked:
            candidate = candidates.get((chunk.get("version_id"), chunk.get("role")))
            expected_type = "synthetic_document" if candidate and json.loads(candidate["review_json"]).get("is_synthetic", False) else "official_document"
            if (not candidate or chunk.get("item_id") != item_id or chunk.get("revision") != revision or
                    chunk.get("source_id") != candidate["source_id"] or chunk.get("url") != candidate["canonical_url"] or
                    chunk.get("source_type") != expected_type):
                raise ServiceError("MODEL_EVIDENCE_INVALID", "Evaluation evidence crossed its corpus boundary.", 409, False)
        active = self._active()
        if revision and (not active or active["revision"] != revision):
            raise ServiceError("INDEX_EMPTY", "The evaluation revision changed.", 409, True)
        return {**context, "similarity": [entry for entry in context["evidence"] if entry["source_type"] != "metadata"], "mmr": ranked,
            "ranking_elapsed_seconds": {"similarity": context["retrieval_elapsed_seconds"], "mmr": mmr_elapsed},
            "common_context_elapsed_seconds": max(0.0, common_elapsed)}

    def ask(self, user_id, item_id, question, applicability=None, top_k=None, strategy=None, *, cache=True, allow_collection=True):
        self.catalog.get(item_id)
        if not isinstance(user_id, str) or not user_id or not isinstance(question, str) or not question.strip() or len(question) > 5000:
            raise ServiceError("INVALID_INPUT", "A question of at most 5000 characters is required.", 422, False)
        top_k, strategy = self._retrieval_settings(top_k, strategy)
        if type(cache) is not bool or type(allow_collection) is not bool:
            raise ServiceError("INVALID_INPUT", "Invalid retrieval settings.", 422, False)
        inputs = self._validate_inputs(applicability)
        initial = {"user_id": user_id, "item_id": item_id, "question": question.strip(),
            "applicability_inputs": inputs, "top_k": top_k, "strategy": strategy,
            "allow_collection": allow_collection, "use_answer_cache": cache}
        start = time.perf_counter()
        try:
            # The graph genuinely invokes the registered retrieve/generate nodes.
            state = self.graph.invoke(initial)
            answer = state["answer"]
            result = {**answer, "evidence": [state["evidence_map"][eid] for eid in answer["cited_evidence_ids"]],
                "readiness": state["readiness"], "errors": state["errors"], "progress": state["progress"],
                "queries": state["queries"], "selection": state["selection"],
                "retrieved_evidence_ids": list(state["evidence_map"]), "revision": state["revision"]}
            result["collection_cache_hits"] = state["collection_cache_hits"]
        except ServiceError as exc:
            result = {"classification": "NEEDS_INFORMATION", "answer": "확인 가능한 근거를 찾지 못했습니다.",
                "claims": [], "conditions": [], "missing_information": ["validated_model_output_required"],
                "conflicts": [], "cited_evidence_ids": [], "evidence": [],
                "readiness": {"status": "DOC_BLOCKED", "reasons": [exc.code]},
                "errors": [_error(exc.code, exc.message, exc.retryable)], "progress": [{"stage": "GENERATING", "status": "FAILED"}],
                "revision": None}
        result.update({"review_id": uuid.uuid4().hex, "item_id": item_id, "applicability": inputs,
            "model": "MOCK-deterministic-offline" if getattr(self.model, "is_mock", False) else type(self.model).__name__,
            "embedding_model": self.vectors.embeddings.name, "elapsed_seconds": time.perf_counter() - start,
            "classification_scope": "document_conditions_only; not transfer completion or payment authorization"})
        result["applicability_ok"] = not result["missing_information"] and not result["conflicts"] and result["readiness"]["status"] == "INDEX_READY"
        # These are discovered contract clauses, never attestations that a
        # buyer or seller fulfilled them. IDs are assigned by the server after
        # model schema/citation validation and bind later domain attestations.
        result["conditions"] = [{**condition, "condition_id": "condition-" + digest({
            "text": condition["text"], "evidence_ids": sorted(condition["evidence_ids"])})}
            for condition in result["conditions"]]
        result["required_condition_ids"] = sorted({condition["condition_id"] for condition in result["conditions"]})
        result = self._payment_projection(result)
        result["review_hash"] = digest({key: value for key, value in result.items() if key not in {"elapsed_seconds", "review_hash"}})
        with self.db.transaction() as connection:
            connection.execute("INSERT INTO chat_history(user_id,item_id,question,result_json,created_at) VALUES(?,?,?,?,?)",
                (user_id, item_id, question.strip(), canonical_json(result), now()))
        return result

    @staticmethod
    def _validate_inputs(applicability):
        if applicability is None:
            return {}
        if not isinstance(applicability, dict) or any(key not in APPLICABILITY_FIELDS for key in applicability):
            raise ServiceError("INVALID_INPUT", "Unsupported contract applicability field.", 422, False)
        if any(not isinstance(value, str) or len(value) > 200 for value in applicability.values()):
            raise ServiceError("INVALID_INPUT", "Invalid contract applicability value.", 422, False)
        return {key: value.strip() for key, value in applicability.items() if value.strip()}

    def history(self, user_id, item_id):
        self.catalog.get(item_id)
        with closing(self.db.connect()) as connection:
            rows = connection.execute("SELECT question,result_json,created_at FROM chat_history WHERE user_id=? AND item_id=? ORDER BY message_id", (user_id, item_id)).fetchall()
        return [{"question": row["question"], "result": self._display_projection(json.loads(row["result_json"]), user_id),
                 "created_at": row["created_at"]} for row in rows]

    def _display_projection(self, result, actor):
        """Current display rights govern old answers too; stored reviews stay immutable."""
        result = self._payment_projection(copy.deepcopy(result))
        for entry in result.get("evidence", []):
            if entry.get("source_type") == "metadata":
                continue
            try:
                self.registry._require(entry.get("source_id"), "display_excerpt", actor, entry.get("version_id"))
            except ServiceError:
                result.update({"classification": "NEEDS_INFORMATION", "answer": "자료 표시 승인이 변경되어 이전 답변을 표시할 수 없습니다.",
                    "claims": [], "conditions": [], "evidence": [], "cited_evidence_ids": [],
                    "required_condition_ids": [],
                    "applicability_ok": False, "conditions_ok": False,
                    "queries": [], "selection": {}, "conflicts": [], "retrieved_evidence_ids": [],
                    "readiness": {"status": "DOC_BLOCKED", "reasons": ["SOURCE_BLOCKED"]}})
                result["errors"] = [_error("SOURCE_BLOCKED", "Source excerpt display is no longer approved.")]
                result["missing_information"] = ["current_display_approval_required"]
                break
        return result

    @staticmethod
    def _payment_projection(result):
        # Recheck legacy saved answers too. A model label cannot turn a
        # nonempty list of obligations into unconditional payment authority.
        if result.get("classification") == "TRANSFERABLE" and result.get("conditions"):
            result["classification"] = "CONDITIONAL"
        result["conditions_ok"] = bool(result.get("applicability_ok") and
            result.get("classification") == "TRANSFERABLE" and not result.get("conditions"))
        return result

    def checklist(self, item_id, applicability=None, *, user_id=None, review_id=None):
        self.catalog.get(item_id)
        inputs = self._validate_inputs(applicability)
        if user_id is not None and (not isinstance(user_id, str) or not user_id or len(user_id) > 128):
            raise ServiceError("INVALID_INPUT", "A server actor is required for saved checklists.", 422, False)
        if review_id is not None and (not isinstance(review_id, str) or not review_id or len(review_id) > 128):
            raise ServiceError("INVALID_INPUT", "Invalid saved review identity.", 422, False)
        candidates = self.registry.candidates(item_id)
        manifest = self._active()
        versions = set(json.loads(manifest["version_ids_json"])) if manifest else set()
        missing = [field for field in APPLICABILITY_FIELDS if not inputs.get(field)]
        conflicts = []
        own_ready = False
        documents = []
        for candidate in candidates:
            try:
                self.registry._require(candidate["source_id"], "local_embed", version_id=candidate["version_id"])
                self.registry._require(candidate["source_id"], "display_excerpt", version_id=candidate["version_id"])
            except ServiceError:
                continue
            if (candidate["version_id"] not in versions or
                    not self.vectors.has_document(item_id, manifest["revision"], candidate["version_id"], candidate["role"])):
                continue
            own_ready |= candidate["own_document"]
            issues, clashes = applicability_issues(inputs, json.loads(candidate["applicability_json"]))
            missing.extend(issues)
            conflicts.extend(clashes)
            documents.append({"version_id": candidate["version_id"], "title": candidate["title"],
                "role": candidate["role"], "url": candidate["canonical_url"], "body_hash": candidate["body_hash"]})
        if not own_ready:
            missing.append("own_document:approved_indexed_body_required")
        result = {"item_id": item_id, "classification": "NEEDS_INFORMATION", "conditions": [],
            "missing_information": list(dict.fromkeys(missing + ["evidence_supported_review_required"])),
            "conflicts": list(dict.fromkeys(conflicts)), "evidence": [], "cited_evidence_ids": [],
            "required_condition_ids": [], "applicability_ok": False, "conditions_ok": False, "review_id": None,
            "documents": documents, "applicability": inputs, "revision": manifest["revision"] if manifest else None,
            "readiness": {"status": "INDEX_READY" if own_ready else "DOC_BLOCKED", "reasons": [] if own_ready else ["OWN_DOCUMENT_MISSING"]}}
        if user_id is None or manifest is None:
            return result
        # The browser may select a review ID; it never selects the actor or
        # bypasses exact item/applicability/revision membership on the server.
        with closing(self.db.connect()) as connection:
            rows = connection.execute("SELECT result_json FROM chat_history WHERE user_id=? AND item_id=? ORDER BY message_id DESC",
                (user_id, item_id)).fetchall()
        selected = next((saved for saved in (json.loads(row["result_json"]) for row in rows)
            if saved.get("applicability") == inputs and saved.get("revision") == manifest["revision"] and
            (review_id is None or saved.get("review_id") == review_id)), None)
        if selected is None:
            return result
        reviewed = self.validate_review(user_id, item_id, selected["review_id"])
        used_versions = {entry["version_id"] for entry in reviewed["evidence"] if entry.get("source_type") != "metadata"}
        result.update({key: copy.deepcopy(reviewed[key]) for key in (
            "review_id", "review_hash", "classification", "conditions", "missing_information", "conflicts",
            "evidence", "cited_evidence_ids", "applicability_ok", "conditions_ok", "readiness")})
        result["required_condition_ids"] = list(reviewed.get("required_condition_ids", []))
        result["documents"] = [document for document in documents if document["version_id"] in used_versions]
        result["errors"] = list(reviewed.get("errors", []))
        return result

    def validate_review(self, actor, item_id, review_id):
        # review_id comes from the caller, every eligibility flag comes from the
        # immutable server result. General chat never edits an adopted snapshot.
        with closing(self.db.connect()) as connection:
            rows = connection.execute("SELECT user_id,item_id,result_json FROM chat_history WHERE item_id=? AND user_id=?", (item_id, actor)).fetchall()
        result = next((json.loads(row["result_json"]) for row in rows if json.loads(row["result_json"]).get("review_id") == review_id), None)
        if result is None:
            raise ServiceError("FORBIDDEN", "The review is not available for this actor and item.", 403, False)
        active = self._active()
        eligibility = bool(active and result["revision"] == active["revision"] and result["applicability_ok"])
        current_candidates = {(candidate["version_id"], candidate["role"]): candidate for candidate in self.registry.candidates(item_id)}
        hashes = []
        for entry in result["evidence"]:
            if entry["source_type"] == "metadata":
                continue
            candidate = current_candidates.get((entry["version_id"], entry["role"]))
            if not candidate or entry["item_id"] != item_id or entry["revision"] != result["revision"]:
                eligibility = False
                continue
            try:
                self.registry._require(entry["source_id"], "local_embed", actor, entry["version_id"])
                self.registry._require(entry["source_id"], "display_excerpt", actor, entry["version_id"])
                missing, conflicts = applicability_issues(result["applicability"], json.loads(candidate["applicability_json"]))
                eligibility &= not missing and not conflicts
                hashes.append(candidate["body_hash"])
            except ServiceError:
                eligibility = False
        projected = self._display_projection(result, actor)
        return {**projected, "applicability_ok": bool(eligibility),
            "conditions_ok": bool(eligibility and projected["conditions_ok"]),
            "document_hashes": sorted(set(hashes)), "classification": projected["classification"] if eligibility else "NEEDS_INFORMATION"}
