"""Query-driven collection of explicitly registered, reviewed document targets.

The default search is a local lookup over the private approved inventory. It is
not an internet search service. An injected search adapter may rank registered
IDs, but cannot register URLs, change document scope, or grant source rights.
"""
from __future__ import annotations

from contextlib import closing
import copy
import json
import math
import re
import threading
import time

from .documents import APPLICABILITY_FIELDS, OWN_ROLES, ROLES, digest, normalize
from .errors import ServiceError


_BODY_ACTIONS = ("view_link", "fetch", "store_body", "extract")
_BLOCKED = {"FORBIDDEN", "SOURCE_BLOCKED", "NETWORK_DISABLED", "URL_BLOCKED", "DEMO_LOGIN_DISABLED"}


def _invalid():
    raise ServiceError("INVALID_INPUT", "문서 수집 입력을 확인해 주세요.", 400)


def _public_error(error):
    code = getattr(error, "code", "COLLECTION_FAILED")
    if not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code):
        code = "COLLECTION_FAILED"
    messages = {"SOURCE_BLOCKED": "출처의 문서 처리 이용 범위를 확인해야 합니다.",
                "FORBIDDEN": "등록 문서 수집 권한이 필요합니다.",
                "NETWORK_DISABLED": "외부 문서 요청이 비활성입니다.",
                "DOCUMENT_FETCH_IN_PROGRESS": "동일 문서의 수집이 진행 중입니다. 작업 상태를 다시 확인해 주세요.",
                "PARSE_FAILED": "문서 본문을 읽지 못했습니다.",
                "FETCH_FAILED": "문서 요청을 완료하지 못했습니다."}
    return {"code": code, "message": messages.get(code, "등록 문서 수집을 완료하지 못했습니다."),
            "retryable": bool(getattr(error, "retryable", False))}


class RegisteredDocumentSearch:
    """Actual local query matching over server-registered candidate metadata."""

    is_remote = False
    cache_version = "registered-inventory-v1"

    def search(self, query, *, item_id, applicability, actor, candidates):
        tokens = re.findall(r"[^\W_]+", normalize(query).casefold(), flags=re.UNICODE)
        matches = []
        for candidate in candidates:
            text = " ".join(str(candidate.get(key, "")) for key in
                            ("title", "url", "source_id", "role", "locator")).casefold()
            score = sum(token in text for token in tokens)
            if score:
                matches.append((score, candidate["id"]))
        return [identifier for _, identifier in sorted(matches, key=lambda row: (-row[0], row[1]))]


class Collector:
    """Discover first, then collect selected and mandatory own-document targets.

    ``actor`` is the requesting server identity. ``manager_actor`` is separately
    configured by the server and never comes from the question or model output.
    DocumentRegistry owns the persistent fetch lease and body/version storage.
    """

    def __init__(self, registry, search_adapter=None, manager_actor=None, *,
                 clock=time.time, search_ttl=300, no_results_ttl=60, failure_ttl=5):
        for ttl in (search_ttl, no_results_ttl, failure_ttl):
            if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not math.isfinite(ttl) or not 0 < ttl <= 86400:
                raise ValueError("Invalid collection cache lifetime")
        self.registry, self.db = registry, registry.db
        self.search_adapter = search_adapter or RegisteredDocumentSearch()
        self.manager_actor, self.clock = manager_actor, clock
        self.search_ttl, self.no_results_ttl, self.failure_ttl = search_ttl, no_results_ttl, failure_ttl
        self._lock = threading.RLock()

    def _inputs(self, item_id, question, queries, applicability, actor):
        if (not isinstance(item_id, str) or not item_id or len(item_id) > 128
                or not isinstance(actor, str) or not actor or len(actor) > 128
                or not isinstance(question, str) or not question.strip() or len(question) > 5000
                or not isinstance(queries, list) or not 1 <= len(queries) <= 10
                or any(not isinstance(query, str) or not query.strip() or len(query) > 512 for query in queries)):
            _invalid()
        if applicability is None:
            applicability = {}
        if (not isinstance(applicability, dict) or set(applicability) - set(APPLICABILITY_FIELDS)
                or any(not isinstance(value, str) or len(value) > 200 for value in applicability.values())):
            _invalid()
        self.registry.catalog.get(item_id)
        terms = list(dict.fromkeys(normalize(query) for query in queries))
        scope = {key: value.strip() for key, value in applicability.items() if value.strip()}
        return terms, scope

    @staticmethod
    def _candidate(raw):
        if not isinstance(raw, dict):
            raise ServiceError("SOURCE_BLOCKED", "등록 문서 정보를 확인해야 합니다.", 403)
        candidate_id = raw.get("target_id") or raw.get("id")
        scope = raw.get("applicability", raw.get("scope"))
        if scope is None:
            try:
                scope = json.loads(raw.get("applicability_json", "{}"))
            except (TypeError, ValueError):
                raise ServiceError("SOURCE_BLOCKED", "등록 문서의 적용 범위를 확인해야 합니다.", 403) from None
        candidate = {"id": candidate_id, "target_id": raw.get("target_id"),
                     "item_id": raw.get("item_id"), "document_id": raw.get("document_id"),
                     "source_id": raw.get("source_id"), "title": raw.get("title"),
                     "url": raw.get("url", raw.get("canonical_url")),
                     "type": raw.get("type", raw.get("document_type")), "role": raw.get("role"),
                     "scope": scope, "applicability_basis": raw.get("applicability_basis"),
                     "version_id": raw.get("version_id"), "locator": raw.get("locator", ""),
                     "published_on": raw.get("published_on"), "source_version_note": raw.get("source_version_note", "")}
        if (any(not isinstance(candidate.get(key), str) or not candidate[key] for key in
                ("id", "item_id", "document_id", "source_id", "title", "url", "applicability_basis"))
                or candidate["type"] not in {"HTML", "PDF"} or candidate["role"] not in ROLES or not isinstance(scope, dict)):
            raise ServiceError("SOURCE_BLOCKED", "등록 문서 정보를 확인해야 합니다.", 403)
        candidate["own_document"] = candidate["role"] in OWN_ROLES
        return candidate

    def _inventory(self, item_id):
        registered = [self._candidate(row) for row in self.registry.registered_candidates(item_id)]
        covered = {(row["document_id"], row["version_id"], row["role"]) for row in registered if row["version_id"]}
        existing = [self._candidate(row) for row in self.registry.candidates(item_id)
                    if (row["document_id"], row["version_id"], row["role"]) not in covered]
        result = {}
        for row in registered + existing:
            if row["item_id"] != item_id or row["id"] in result:
                raise ServiceError("SOURCE_BLOCKED", "등록 항목의 문서 경계를 확인해야 합니다.", 403)
            result[row["id"]] = row
        return result

    def _reviews(self, candidate, actor):
        values = {}
        try:
            values["source"] = self.registry.policy.source(candidate["source_id"])
        except ServiceError:
            values["source"] = "BLOCKED"
        for action in (*_BODY_ACTIONS, "local_embed"):
            try:
                values[action] = self.registry.policy.require(candidate["source_id"], action, actor=actor,
                                                              version_id=candidate["version_id"])
            except ServiceError:
                values[action] = "BLOCKED"
        return digest(values)

    def _require(self, candidate, actor, actions):
        for action in actions:
            self.registry.policy.require(candidate["source_id"], action, actor=actor,
                                         version_id=candidate["version_id"])

    def _cache_get(self, kind, key):
        with closing(self.db.connect()) as connection:
            row = connection.execute("SELECT status,value_json,expires_at FROM rag_cache WHERE cache_kind=? AND cache_key=?", (kind, key)).fetchone()
        if row is None or row["expires_at"] is not None and row["expires_at"] <= self.clock():
            return None
        try:
            return {"status": row["status"], "value": json.loads(row["value_json"])}
        except (TypeError, ValueError):
            return None

    def _cache_put(self, kind, key, status, value, ttl=None):
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        with self.db.transaction() as connection:
            connection.execute("INSERT OR REPLACE INTO rag_cache(cache_kind,cache_key,status,value_json,expires_at) VALUES(?,?,?,?,?)",
                               (kind, key, status, encoded, self.clock() + ttl if ttl is not None else None))

    def _indexed_current(self, value):
        if not isinstance(value, dict) or not isinstance(value.get("revision"), str):
            return False
        with closing(self.db.connect()) as connection:
            row = connection.execute("SELECT version_ids_json FROM index_manifests WHERE revision=? AND active=1", (value["revision"],)).fetchone()
        return bool(row and value.get("version_id") in json.loads(row["version_ids_json"]))

    def discover(self, item_id, question, queries, applicability, actor, *, refresh=False):
        terms, scope = self._inputs(item_id, question, queries, applicability, actor)
        if type(refresh) is not bool:
            _invalid()
        # No approved remote search endpoint is shipped. Never send a question
        # to a new external service through an undeclared/injected adapter.
        if getattr(self.search_adapter, "is_remote", None) is not False:
            raise ServiceError("SOURCE_BLOCKED", "승인된 로컬 문서 검색만 사용할 수 있습니다.", 403)
        with self._lock:
            inventory = self._inventory(item_id)
            allowed = {}
            for identifier, candidate in inventory.items():
                try:
                    self._require(candidate, actor, ("view_link",))
                    allowed[identifier] = candidate
                except ServiceError:
                    if candidate["own_document"]:
                        raise
            registration_fingerprint = digest([{key: value for key, value in row.items() if key != "version_id"}
                                               for row in sorted(allowed.values(), key=lambda row: row["id"])])
            selected = {identifier for identifier, row in allowed.items() if row["own_document"]}
            for term in terms:
                key = digest([getattr(self.search_adapter, "cache_version", type(self.search_adapter).__name__),
                              item_id, scope, normalize(term).casefold(), registration_fingerprint,
                              sorted(self._reviews(row, actor) for row in allowed.values())])
                cached = None if refresh else self._cache_get("collection_search", key)
                if cached and cached["status"] in {"FOUND", "NO_RESULTS"}:
                    identifiers = cached["value"].get("candidate_ids", [])
                else:
                    rows = self.search_adapter.search(term, item_id=item_id, applicability=copy.deepcopy(scope), actor=actor,
                                                      candidates=copy.deepcopy(list(allowed.values())))
                    if not isinstance(rows, list) or len(rows) > 1000:
                        raise ServiceError("SOURCE_BLOCKED", "등록 검색 결과를 검증하지 못했습니다.", 403)
                    identifiers = [row.get("id") if isinstance(row, dict) else row for row in rows]
                    if any(not isinstance(identifier, str) or identifier not in allowed for identifier in identifiers):
                        raise ServiceError("SOURCE_BLOCKED", "등록되지 않은 검색 후보를 사용할 수 없습니다.", 403)
                    identifiers = list(dict.fromkeys(identifiers))
                    self._cache_put("collection_search", key, "FOUND" if identifiers else "NO_RESULTS",
                                    {"candidate_ids": identifiers}, self.search_ttl if identifiers else self.no_results_ttl)
                if any(identifier not in allowed for identifier in identifiers):
                    raise ServiceError("SOURCE_BLOCKED", "검색 후보의 등록 상태가 변경되었습니다.", 403)
                selected.update(identifiers)
            return [copy.deepcopy(allowed[identifier]) for identifier in sorted(selected)]

    def _fetch_key(self, candidate, terms, scope, actor):
        return digest([candidate["source_id"], candidate["item_id"], scope,
                       sorted({normalize(term).casefold() for term in terms}),
                       {key: candidate[key] for key in ("id", "document_id", "url", "type", "role", "scope", "applicability_basis", "published_on", "source_version_note")},
                       self._reviews(candidate, actor)])

    def collect(self, item_id, question, queries, applicability, actor, *, selected_candidate_ids=None,
                candidates=None, refresh=False):
        terms, scope = self._inputs(item_id, question, queries, applicability, actor)
        if type(refresh) is not bool or selected_candidate_ids is not None and (
                not isinstance(selected_candidate_ids, list) or len(selected_candidate_ids) > 1000
                or any(not isinstance(identifier, str) for identifier in selected_candidate_ids)):
            _invalid()
        with self._lock:
            inventory = self._inventory(item_id)
            discovered = self.discover(item_id, question, queries, applicability, actor, refresh=refresh) if candidates is None else candidates
            if not isinstance(discovered, list):
                _invalid()
            for supplied in discovered:
                if not isinstance(supplied, dict) or supplied.get("id") not in inventory:
                    raise ServiceError("SOURCE_BLOCKED", "등록되지 않은 문서 후보를 사용할 수 없습니다.", 403)
                trusted = inventory[supplied["id"]]
                for key in ("item_id", "document_id", "source_id", "url", "type", "role", "scope", "own_document"):
                    if key in supplied and supplied[key] != trusted[key]:
                        raise ServiceError("SOURCE_BLOCKED", "문서 후보의 등록 정보가 일치하지 않습니다.", 403)
            supplied_ids = {row["id"] for row in discovered}
            selected = set(selected_candidate_ids or [])
            if selected - supplied_ids or selected - inventory.keys():
                raise ServiceError("SOURCE_BLOCKED", "선별 대상은 등록 검색 후보여야 합니다.", 403)
            selected.update(identifier for identifier, row in inventory.items() if row["own_document"])
            result = {"status": "NO_RESULTS", "candidate_ids": [], "version_ids": [], "linked": [],
                      "cache_hits": 0, "errors": [], "cache_keys": []}
            if not selected:
                key = digest([item_id, scope, sorted(normalize(term).casefold() for term in terms), "NO_RESULTS"])
                cached = None if refresh else self._cache_get("collection_fetch", key)
                result["cache_hits"] = int(bool(cached and cached["status"] == "NO_RESULTS"))
                result["cache_keys"] = [key]
                self._cache_put("collection_fetch", key, "NO_RESULTS", {"item_id": item_id}, self.no_results_ttl)
                return result
            indexed = []
            for identifier in sorted(selected):
                candidate = inventory[identifier]
                key = self._fetch_key(candidate, terms, scope, actor)
                result["cache_keys"].append(key)
                try:
                    # Recheck rights before every cache read or body reuse.
                    self._require(candidate, actor, _BODY_ACTIONS)
                    cached = None if refresh else self._cache_get("collection_fetch", key)
                    if cached and cached["status"] == "FAILED":
                        result["errors"].append(cached["value"]["error"])
                        continue
                    version_id = candidate["version_id"]
                    cache_hit = False
                    if (not refresh and cached and cached["status"] in {"FETCHED", "INDEXED"}
                            and version_id and cached["value"].get("version_id") == version_id):
                        cache_hit = True
                    elif candidate["target_id"]:
                        if self.manager_actor is None:
                            raise ServiceError("FORBIDDEN", "문서 수집 담당자 설정이 필요합니다.", 403)
                        self.registry.policy.require_capability(self.manager_actor, "DATA_MANAGER")
                        fetched = self.registry.fetch_target(self.manager_actor, candidate["target_id"], key, refresh=refresh)
                        version_id = fetched["version_id"]
                        cache_hit = fetched.get("cache_hit") is True
                    elif version_id and not refresh:
                        cache_hit = True
                    else:
                        raise ServiceError("SOURCE_BLOCKED", "본문 갱신에는 등록된 문서 수집 대상이 필요합니다.", 403)
                    if not isinstance(version_id, str) or not version_id:
                        raise ServiceError("FETCH_FAILED", "문서 버전을 확인하지 못했습니다.", 503, True)
                    # The completed inventory is the authority for item/role/version,
                    # not a transport or model-provided association.
                    completed = self._inventory(item_id).get(identifier)
                    if not completed or completed["version_id"] != version_id:
                        raise ServiceError("SOURCE_BLOCKED", "수집 문서와 등록 대상의 연결을 확인하지 못했습니다.", 403)
                    self._require(completed, actor, _BODY_ACTIONS)
                    key = self._fetch_key(completed, terms, scope, actor)
                    if key not in result["cache_keys"]:
                        result["cache_keys"].append(key)
                    prior = self._cache_get("collection_fetch", key)
                    status = "INDEXED" if (not refresh and prior and prior["status"] == "INDEXED"
                                          and prior["value"].get("version_id") == version_id
                                          and self._indexed_current(prior["value"])) else "FETCHED"
                    payload = {"item_id": item_id, "candidate_id": identifier, "source_id": completed["source_id"],
                               "version_id": version_id, "role": completed["role"], "revision": prior["value"].get("revision") if status == "INDEXED" else None}
                    self._cache_put("collection_fetch", key, status, payload)
                    indexed.append(status == "INDEXED")
                    result["candidate_ids"].append(identifier)
                    result["version_ids"].append(version_id)
                    result["linked"].append({"candidate_id": identifier, "version_id": version_id, "role": completed["role"], "item_id": item_id})
                    result["cache_hits"] += int(cache_hit)
                except ServiceError as error:
                    safe = _public_error(error)
                    result["errors"].append(safe)
                    self._cache_put("collection_fetch", key, "FAILED", {"item_id": item_id, "error": safe}, self.failure_ttl)
                except Exception:
                    safe = _public_error(None)
                    result["errors"].append(safe)
                    self._cache_put("collection_fetch", key, "FAILED", {"item_id": item_id, "error": safe}, self.failure_ttl)
            result["version_ids"] = sorted(set(result["version_ids"]))
            if result["errors"]:
                result["status"] = "BLOCKED" if all(error["code"] in _BLOCKED for error in result["errors"]) else "FAILED"
            elif result["linked"]:
                result["status"] = "INDEXED" if all(indexed) else "FETCHED"
            return result

    def mark_indexed(self, collection_result, revision):
        """Promote only versions verified in the currently active index manifest."""
        if not isinstance(collection_result, dict) or not isinstance(revision, str) or not revision or len(revision) > 256:
            _invalid()
        keys = collection_result.get("cache_keys", [])
        if not isinstance(keys, list) or len(keys) > 2000 or any(not isinstance(key, str) for key in keys):
            _invalid()
        # This transaction contains database and in-memory policy checks only;
        # an index activation cannot interleave with the cache promotion.
        with self._lock, self.registry.policy.read_guard(), self.db.transaction() as connection:
            manifest = connection.execute("SELECT version_ids_json FROM index_manifests WHERE revision=? AND active=1", (revision,)).fetchone()
            if manifest is None:
                raise ServiceError("INDEX_EMPTY", "현재 활성 문서 색인을 확인해야 합니다.", 409)
            versions = set(json.loads(manifest["version_ids_json"]))
            changed = 0
            for key in dict.fromkeys(keys):
                row = connection.execute("SELECT status,value_json FROM rag_cache WHERE cache_kind='collection_fetch' AND cache_key=?", (key,)).fetchone()
                if not row or row["status"] not in {"FETCHED", "INDEXED"}:
                    continue
                value = json.loads(row["value_json"])
                if value.get("version_id") not in versions:
                    continue
                self.registry.policy.require(value["source_id"], "local_embed", actor=self.manager_actor,
                                             version_id=value["version_id"])
                encoded = json.dumps({**value, "revision": revision}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
                connection.execute("UPDATE rag_cache SET status='INDEXED',value_json=?,expires_at=NULL WHERE cache_kind='collection_fetch' AND cache_key=?", (encoded, key))
                changed += 1
            return changed
