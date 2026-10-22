"""Local Chroma persistence with explicitly nonsemantic MOCK embeddings.

No embedding provider, download, or network client is constructed here. The
hashed token vectors make offline integration reproducible; they are not a
substitute for evaluating a real semantic embedding model.
"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path
import re
from typing import Any

from .errors import ServiceError


class MockEmbeddings:
    name = "MOCK-token-hash-v1-NOT-SEMANTIC"
    dimension = 64
    is_mock = True
    is_remote = False

    def embed(self, texts: list[str]) -> list[list[float]]:
        result = []
        for text in texts:
            vector = [0.0] * self.dimension
            for token in re.findall(r"[\w가-힣]+", text.casefold()):
                digest = hashlib.sha256(token.encode("utf-8")).digest()
                vector[int.from_bytes(digest[:4], "big") % self.dimension] += 1.0
            norm = math.sqrt(sum(value * value for value in vector)) or 1.0
            result.append([value / norm for value in vector])
        return result


def _dot(left, right):
    return sum(a * b for a, b in zip(left, right))


class LocalVectorStore:
    """Two logical collections; staged document rows are revision-isolated.

    Public evidence IDs follow the document chunk identity contract. Physical
    Chroma IDs additionally include the revision so staging can never mutate
    the last good revision. Only validated revisions become active in SQLite.
    """

    def __init__(self, path: Path, embeddings=None):
        try:
            import chromadb
            from chromadb.config import Settings as ChromaSettings
        except ImportError as exc:
            raise ServiceError("INDEX_EMPTY", "Local Chroma dependency is unavailable.", 503, False) from exc
        self.embeddings = embeddings or MockEmbeddings()
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self.client = chromadb.PersistentClient(
            path=str(self.path), settings=ChromaSettings(anonymized_telemetry=False)
        )
        suffix = "" if (self.embeddings.name == MockEmbeddings.name and self.embeddings.dimension == 64) else (
            "_" + hashlib.sha256(self.embeddings.name.encode()).hexdigest()[:12] + "_" + str(self.embeddings.dimension))
        self.meta = self.client.get_or_create_collection("warranty_meta" + suffix,
            metadata={"hnsw:space": "cosine", "embedding_model": self.embeddings.name}, embedding_function=None)
        self.docs = self.client.get_or_create_collection("warranty_docs" + suffix,
            metadata={"hnsw:space": "cosine", "embedding_model": self.embeddings.name}, embedding_function=None)

    def index_metadata(self, records: list[dict[str, Any]]) -> int:
        ids = [str(record["item_id"]) for record in records]
        if len(ids) != len(set(ids)):
            raise ServiceError("MODEL_OUTPUT_INVALID", "Duplicate catalog identity.", 409, False)
        if records:
            texts = [record["text"] for record in records]
            self.meta.upsert(ids=ids, documents=texts, embeddings=self.embeddings.embed(texts),
                metadatas=[{"item_id": value, "source_type": "metadata"} for value in ids])
        old_ids = self.meta.get(include=[])['ids']
        stale = [value for value in old_ids if value not in set(ids)]
        if stale:
            self.meta.delete(ids=stale)
        if self.meta.count() != len(ids):
            raise ServiceError("INDEX_EMPTY", "Metadata index validation failed.", 503, True)
        return self.meta.count()

    def close(self):
        close = getattr(self.client, "close", None)
        if callable(close):
            close()

    def stage_documents(self, revision: str, chunks: list[dict[str, Any]]) -> int:
        for start in range(0, len(chunks), 100):
            batch = chunks[start:start + 100]
            texts = [chunk["text"] for chunk in batch]
            self.docs.upsert(
                ids=[self._physical_id(revision, chunk["evidence_id"]) for chunk in batch],
                documents=texts, embeddings=self.embeddings.embed(texts),
                metadatas=[{key: value for key, value in {**chunk, "revision": revision}.items()
                    if key != "text" and value is not None and isinstance(value, (str, int, float, bool))}
                    for chunk in batch],
            )
        actual = self.docs.get(where={"revision": revision}, include=[])["ids"]
        if len(actual) != len(chunks):
            raise ServiceError("INDEX_EMPTY", "Document index validation failed.", 503, True)
        return len(actual)

    @staticmethod
    def _physical_id(revision, evidence_id):
        return hashlib.sha256((revision + ":" + evidence_id).encode()).hexdigest()

    def discard_revision(self, revision: str):
        rows = self.docs.get(where={"revision": revision}, include=[])["ids"]
        if rows:
            self.docs.delete(ids=rows)

    def has_document(self, item_id, revision, version_id, role):
        if not revision:
            return False
        return bool(self.docs.get(where={"$and": [{"item_id": item_id}, {"revision": revision},
            {"version_id": version_id}, {"role": role}]}, include=[], limit=1)["ids"])

    def cleanup_except(self, revision: str):
        rows = self.docs.get(include=["metadatas"])
        stale = [key for key, metadata in zip(rows["ids"], rows["metadatas"])
            if metadata.get("revision") != revision]
        if stale:
            self.docs.delete(ids=stale)

    def search(self, item_id: str, revision: str, question: str, top_k: int = 6,
               strategy: str = "similarity", fetch_k: int = 30, lambda_mult: float = 0.5,
               allowed_pairs=None):
        if strategy not in {"similarity", "mmr"} or not 1 <= top_k <= 30:
            raise ServiceError("INVALID_INPUT", "Invalid retrieval settings.", 422, False)
        where = {"$and": [{"item_id": item_id}, {"revision": revision}]}
        if allowed_pairs is not None:
            pairs = list(allowed_pairs)
            if not pairs:
                return []
            if len(pairs) == 1:
                where["$and"].extend([{"version_id": pairs[0][0]}, {"role": pairs[0][1]}])
            else:
                where["$and"].append({"$or": [{"$and": [{"version_id": version}, {"role": role}]}
                    for version, role in pairs]})
        available = self.docs.get(where=where, include=[])["ids"]
        if not available:
            return []
        query = self.embeddings.embed([question])[0]
        limit = min(len(available), max(top_k, fetch_k) if strategy == "mmr" else top_k)
        rows = self.docs.query(query_embeddings=[query], n_results=limit, where=where,
            include=["documents", "metadatas", "embeddings", "distances"])
        candidates = []
        seen = set()
        for index, metadata in enumerate(rows["metadatas"][0]):
            # Equal text in separate versions/roles is preserved to expose conflicts.
            key = (metadata["version_id"], metadata["role"], rows["documents"][0][index])
            if key in seen:
                continue
            seen.add(key)
            candidates.append({**metadata, "text": rows["documents"][0][index],
                "score": 1 - rows["distances"][0][index],
                "_embedding": rows["embeddings"][0][index]})
        if strategy == "mmr":
            selected = []
            while candidates and len(selected) < top_k:
                best_index = max(range(len(candidates)), key=lambda index: lambda_mult * _dot(query, candidates[index]["_embedding"])
                    - (1 - lambda_mult) * max((_dot(candidates[index]["_embedding"], other["_embedding"])
                        for other in selected), default=0))
                selected.append(candidates.pop(best_index))
            candidates = selected
        for chunk in candidates:
            chunk.pop("_embedding", None)
        return candidates[:top_k]
