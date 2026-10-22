"""Reviewed document registry, inert local parsers and bounded chunking.

The shipped link catalog is not a document corpus. No source is approved and
no body is fabricated when this module is constructed.
"""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import closing
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import tempfile
import time
import uuid
from urllib.parse import urlsplit

from .errors import ServiceError

ROLES = {"PRIMARY_DESCRIPTION", "SERVICE_TERMS", "INSURANCE_TERMS", "TRANSFER_GUIDE"}
OWN_ROLES = {"PRIMARY_DESCRIPTION", "SERVICE_TERMS", "INSURANCE_TERMS"}
ACTIONS = ("view_link", "fetch", "store_body", "extract", "local_embed",
           "external_process", "display_excerpt", "public_redistribute")
APPLICABILITY_FIELDS = ("country", "enrollment_date", "plan_generation", "payment_method", "channel", "document_type")


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256((value if isinstance(value, bytes) else canonical_json(value).encode("utf-8"))).hexdigest()


def normalize(text):
    return re.sub(r"\s+", " ", text).strip()


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.sections = []
        self.title = ""
        self.heading = "본문"
        self.parts = []
        self.hidden = 0
        self.in_title = False
        self.in_heading = False
        self.heading_parts = []

    def flush(self):
        text = normalize(" ".join(self.parts))
        if text:
            self.sections.append({"section": self.heading, "text": text})
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "template", "svg"}:
            self.hidden += 1
        if self.hidden:
            return
        if tag == "title":
            self.in_title = True
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.flush()
            self.in_heading = True
            self.heading_parts = []
        if tag in {"p", "br", "li", "div", "tr", "td", "section"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "template", "svg"}:
            self.hidden = max(0, self.hidden - 1)
            return
        if self.hidden:
            return
        if tag == "title":
            self.in_title = False
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.heading = normalize(" ".join(self.heading_parts)) or "본문"
            self.in_heading = False

    def handle_data(self, data):
        if self.hidden:
            return
        if self.in_title:
            self.title += data
        elif self.in_heading:
            self.heading_parts.append(data)
        else:
            self.parts.append(data)


def parse_document(body: bytes, document_type: str):
    if not body or len(body) > 10 * 1024 * 1024:
        raise ServiceError("PARSE_FAILED", "Document is empty or exceeds the size limit.", 422, False)
    if document_type == "HTML":
        if body.startswith(b"%PDF"):
            raise ServiceError("PARSE_FAILED", "Document content type does not match its registry record.", 422, False)
        try:
            text = body.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                text = body.decode("cp949")
            except UnicodeDecodeError as exc:
                raise ServiceError("PARSE_FAILED", "Document text encoding is unsupported.", 422, False) from exc
        parser = _HTMLText()
        parser.feed(text)
        parser.flush()
        sections = parser.sections
    elif document_type == "PDF":
        if not body.startswith(b"%PDF"):
            raise ServiceError("PARSE_FAILED", "A valid PDF body is required.", 422, False)
        try:
            import pymupdf
            with pymupdf.open(stream=body, filetype="pdf") as pdf:
                if pdf.needs_pass:
                    raise ValueError("encrypted")
                sections = [{"page": index + 1, "text": normalize(page.get_text())}
                            for index, page in enumerate(pdf)]
        except ImportError as exc:
            raise ServiceError("PARSE_FAILED", "Local PDF parser dependency is unavailable.", 503, False) from exc
        except Exception as exc:
            raise ServiceError("PARSE_FAILED", "PDF extraction failed.", 422, False) from exc
    else:
        raise ServiceError("INVALID_INPUT", "Only HTML and PDF bodies are supported.", 422, False)
    sections = [part for part in sections if part["text"]]
    if not sections:
        raise ServiceError("PARSE_FAILED", "No text was extracted; a scanned document may require OCR.", 422, False)
    return sections


def chunk_sections(sections, chunk_size=1000, chunk_overlap=200):
    if isinstance(chunk_size, bool) or isinstance(chunk_overlap, bool) or not 100 <= chunk_size <= 10000 or not 0 <= chunk_overlap < chunk_size:
        raise ServiceError("INVALID_INPUT", "Invalid document chunk configuration.", 422, False)
    chunks = []
    for section in sections:
        text = section["text"]
        for start in range(0, len(text), chunk_size - chunk_overlap):
            piece = text[start:start + chunk_size]
            if piece:
                chunks.append({**section, "text": piece, "offset": start})
            if start + chunk_size >= len(text):
                break
    return chunks


def applicability_issues(inputs, scope):
    """No latest-document or enrollment-date guesses are permitted."""
    if not isinstance(scope, dict):
        return ["document_scope:valid_scope_required"], []
    missing = [field for field in APPLICABILITY_FIELDS if not inputs.get(field)]
    conflicts = []
    for field in ("country", "plan_generation", "payment_method", "channel", "document_type"):
        rule = scope.get(field)
        if rule is None:
            missing.append("document_scope:" + field)
        elif inputs.get(field) and inputs[field] not in (rule if isinstance(rule, list) else [rule]):
            conflicts.append("Contract applicability differs: " + field)
    date = inputs.get("enrollment_date")
    if date:
        try:
            parsed = datetime.strptime(date, "%Y-%m-%d").date().isoformat()
            start, end = scope.get("date_start"), scope.get("date_end")
            if start and parsed < start or end and parsed > end:
                conflicts.append("Enrollment date is outside the document contract range.")
            if not start and not end:
                missing.append("document_scope:enrollment_date")
        except (ValueError, TypeError):
            missing.append("enrollment_date:valid_date_required")
    return list(dict.fromkeys(missing)), list(dict.fromkeys(conflicts))


class DocumentRegistry:
    def __init__(self, catalog, db, settings, source_policy=None, fetcher=None):
        self.catalog, self.db, self.settings = catalog, db, settings
        if source_policy is None:
            from .security import SourcePolicy
            source_policy = SourcePolicy()
        self.policy = source_policy
        self.fetcher = fetcher
        self.body_dir = Path(settings.data_dir) / "document_bodies"
        self.body_dir.mkdir(parents=True, exist_ok=True)

    def _manager(self, actor):
        self.policy.require_capability(actor, "DATA_MANAGER")

    @staticmethod
    def _validate_scope(applicability, published_on=None, source_version_note=""):
        allowed = {"country", "plan_generation", "payment_method", "channel", "document_type", "date_start", "date_end"}
        if not isinstance(applicability, dict) or set(applicability) - allowed:
            raise ServiceError("INVALID_INPUT", "Invalid registered document applicability scope.", 422, False)
        for key, value in applicability.items():
            if key in {"date_start", "date_end"}:
                DocumentRegistry._validate_date(value)
            elif not (isinstance(value, str) and value.strip() and len(value) <= 200 or
                      isinstance(value, list) and value and len(value) <= 100 and all(isinstance(part, str) and part.strip() and len(part) <= 200 for part in value)):
                raise ServiceError("INVALID_INPUT", "Invalid registered document applicability value.", 422, False)
        if applicability.get("date_start", "0001-01-01") > applicability.get("date_end", "9999-12-31"):
            raise ServiceError("INVALID_INPUT", "Invalid registered document applicability date range.", 422, False)
        if published_on is not None:
            DocumentRegistry._validate_date(published_on)
        if not isinstance(source_version_note, str) or len(source_version_note) > 2000:
            raise ServiceError("INVALID_INPUT", "Invalid registered document version note.", 422, False)

    @staticmethod
    def _validate_date(value):
        try:
            if not isinstance(value, str) or datetime.strptime(value, "%Y-%m-%d").date().isoformat() != value:
                raise ValueError()
        except (ValueError, TypeError):
            raise ServiceError("INVALID_INPUT", "A scope date must be a valid ISO date.", 422, False) from None

    @staticmethod
    def _validate_association(role, applicability_basis, locator):
        if (not isinstance(role, str) or role not in ROLES or not isinstance(applicability_basis, str)
                or not applicability_basis.strip() or len(applicability_basis) > 2000
                or not isinstance(locator, str) or len(locator) > 1000):
            raise ServiceError("INVALID_INPUT", "A document role and recorded applicability basis are required.", 422, False)

    def _require(self, source_id, action, actor=None, version_id=None):
        review = self.policy.require(source_id, action, actor=actor, version_id=version_id)
        source = self.policy.source(source_id)
        review_version = review.get("version_id", review.get("document_version_id"))
        with self.db.transaction() as connection:
            connection.execute("INSERT OR IGNORE INTO sources(source_id,canonical_url,title) VALUES(?,?,?)",
                (source_id, source.get("url", source.get("canonical_url", "")), source.get("title", source_id)))
            known_document_version = review_version if review_version is not None and connection.execute("SELECT 1 FROM document_versions WHERE version_id=?", (review_version,)).fetchone() is not None else None
            connection.execute("INSERT OR IGNORE INTO source_action_reviews(review_id,source_id,document_version_id,action,status,basis_type,basis_url,reviewed_on,scope,conditions,processing_environment,user_scope,reviewed_version_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("review-" + digest(review), source_id, known_document_version, action, review["status"], review["basis_type"],
                 review["basis_url"], review["reviewed_on"], review["scope"], review["conditions"],
                 review["processing_environment"], review["user_scope"], review_version))
        return review

    def register_document(self, actor, *, source_id, title, canonical_url,
                          document_type, operator="", language="ko", is_synthetic=False):
        self._manager(actor)
        if not isinstance(source_id, str) or not source_id or not isinstance(canonical_url, str):
            raise ServiceError("INVALID_INPUT", "Invalid document registration.", 422, False)
        self._require(source_id, "view_link", actor)
        if (not isinstance(document_type, str) or document_type not in {"HTML", "PDF"}
                or not isinstance(title, str) or not title.strip() or len(title) > 500
                or not isinstance(operator, str) or len(operator) > 500 or not isinstance(language, str) or len(language) > 50
                or type(is_synthetic) is not bool):
            raise ServiceError("INVALID_INPUT", "Invalid document registration.", 422, False)
        parsed = urlsplit(canonical_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ServiceError("SOURCE_BLOCKED", "Only a registered HTTPS source is accepted.", 403, False)
        source = self.policy.source(source_id)
        approved_urls = {source.get("url"), source.get("canonical_url"), *source.get("approved_urls", [])}
        if canonical_url not in approved_urls:
            raise ServiceError("SOURCE_BLOCKED", "Document URL is not in the approved source registry.", 403, False)
        document_id = "doc-" + digest([source_id, canonical_url, document_type])[:32]
        review = {"is_synthetic": bool(is_synthetic)}
        with self.db.transaction() as connection:
            connection.execute("INSERT OR IGNORE INTO sources(source_id,canonical_url,title) VALUES(?,?,?)",
                               (source_id, canonical_url, title))
            connection.execute("INSERT OR IGNORE INTO documents(document_id,source_id,title,canonical_url,document_type,operator,language,review_json) VALUES(?,?,?,?,?,?,?,?)",
                (document_id, source_id, title, canonical_url, document_type, operator, language, canonical_json(review)))
        return {"document_id": document_id, "source_id": source_id, "type": document_type}

    def ingest_bytes(self, actor, document_id, body, *, applicability, published_on=None,
                     source_version_note=""):
        self._manager(actor)
        self._validate_scope(applicability, published_on, source_version_note)
        if not isinstance(body, bytes):
            raise ServiceError("INVALID_INPUT", "Document body must be bytes.", 422, False)
        with closing(self.db.connect()) as connection:
            row = connection.execute("SELECT * FROM documents WHERE document_id=?", (document_id,)).fetchone()
        if row is None:
            raise ServiceError("NO_RESULTS", "Registered document was not found.", 404, False)
        for action in ("store_body", "extract"):
            self._require(row["source_id"], action, actor)
        sections = parse_document(body, row["document_type"])
        body_hash = digest(sections)
        version_id = "ver-" + digest([document_id, body_hash])[:32]
        path = self.body_dir / (body_hash + ".json")
        # Version-specific denials take precedence over a generic source grant.
        # Keep policy->DB ordering and fence revocation through persistence.
        with self.policy.read_guard():
            self._manager(actor)
            for action in ("store_body", "extract"):
                self._require(row["source_id"], action, actor, version_id)
            # Diagnose an immutable contract conflict before replacing the body
            # file. The write-transaction check below still fences concurrent
            # inserts from other application or policy instances.
            with closing(self.db.connect()) as connection:
                existing = connection.execute("SELECT * FROM document_versions WHERE document_id=? AND body_hash=?",
                    (document_id, body_hash)).fetchone()
            if existing is not None and (json.loads(existing["applicability_json"]) != applicability
                    or existing["published_on"] != published_on or existing["source_version_note"] != source_version_note):
                raise ServiceError("DOCUMENT_VERSION_CONFLICT", "Existing document text has different immutable contract scope; register a separately identified document.", 409, False)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.body_dir, suffix=".tmp", delete=False) as output:
                    temporary = Path(output.name)
                    output.write(canonical_json(sections))
                os.replace(temporary, path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            with self.db.transaction() as connection:
                existing = connection.execute("SELECT * FROM document_versions WHERE document_id=? AND body_hash=?", (document_id, body_hash)).fetchone()
                reused = existing is not None
                if existing is not None and (json.loads(existing["applicability_json"]) != applicability or existing["published_on"] != published_on
                        or existing["source_version_note"] != source_version_note):
                    raise ServiceError("DOCUMENT_VERSION_CONFLICT", "Existing document text has different immutable contract scope; register a separately identified document.", 409, False)
                connection.execute("INSERT OR IGNORE INTO document_versions(version_id,document_id,raw_hash,body_hash,checked_on,published_on,source_version_note,applicability_json,body_path) VALUES(?,?,?,?,?,?,?,?,?)",
                    (version_id, document_id, digest(body), body_hash, now(), published_on,
                     source_version_note, canonical_json(applicability), str(path)))
        return {"version_id": version_id, "document_id": document_id, "body_hash": body_hash,
                "section_count": len(sections), "reused": reused}

    def _version_count(self, document_id, body_hash):
        with closing(self.db.connect()) as connection:
            return connection.execute("SELECT COUNT(*) FROM document_versions WHERE document_id=? AND body_hash=?", (document_id, body_hash)).fetchone()[0]

    def fetch(self, actor, document_id, *, applicability, **version_fields):
        self._manager(actor)
        if self.fetcher is None:
            raise ServiceError("SOURCE_BLOCKED", "Network document collection is disabled.", 403, False)
        with closing(self.db.connect()) as connection:
            row = connection.execute("SELECT * FROM documents WHERE document_id=?", (document_id,)).fetchone()
        if row is None:
            raise ServiceError("NO_RESULTS", "Registered document was not found.", 404, False)
        self._require(row["source_id"], "fetch", actor)
        response = self.fetcher.fetch(row["source_id"], row["canonical_url"], actor=actor)
        return self.ingest_bytes(actor, document_id, response.body, applicability=applicability, **version_fields)

    def link(self, actor, item_id, version_id, *, role, applicability_basis, locator=""):
        self._manager(actor)
        item = self.catalog.get(item_id)
        if not isinstance(version_id, str) or not version_id or len(version_id) > 200:
            raise ServiceError("INVALID_INPUT", "Invalid document version identifier.", 422, False)
        self._validate_association(role, applicability_basis, locator)
        with self.db.transaction() as connection:
            connection.execute("INSERT OR IGNORE INTO items(item_id,name,provider) VALUES(?,?,?)",
                (item_id, item.get("name", item_id), item.get("provider", "UNKNOWN")))
            if not connection.execute("SELECT 1 FROM document_versions WHERE version_id=?", (version_id,)).fetchone():
                raise ServiceError("NO_RESULTS", "Document version was not found.", 404, False)
            self._link_version(connection, item_id, version_id, role, applicability_basis, locator)
        return {"item_id": item_id, "version_id": version_id, "role": role}

    @staticmethod
    def _link_version(connection, item_id, version_id, role, applicability_basis, locator):
        previous = connection.execute("SELECT * FROM item_documents WHERE item_id=? AND version_id=? AND role=?", (item_id, version_id, role)).fetchone()
        if previous is not None and (previous["locator"] != locator or previous["applicability_basis"] != applicability_basis):
            raise ServiceError("DOCUMENT_LINK_CONFLICT", "Existing document linkage has different recorded applicability evidence.", 409, False)
        connection.execute("INSERT INTO item_documents(item_id,version_id,role,locator,applicability_basis,status) VALUES(?,?,?,?,?,'APPROVED') ON CONFLICT(item_id,version_id,role) DO UPDATE SET status='APPROVED'",
            (item_id, version_id, role, locator, applicability_basis))

    def register_target(self, actor, item_id, document_id, *, role, applicability,
                        applicability_basis, locator="", published_on=None, source_version_note=""):
        """Record an unfetched item document from private DATA_MANAGER input."""
        self._manager(actor)
        item = self.catalog.get(item_id)
        if not isinstance(document_id, str) or not document_id or len(document_id) > 200:
            raise ServiceError("INVALID_INPUT", "Invalid registered document identifier.", 422, False)
        self._validate_association(role, applicability_basis, locator)
        self._validate_scope(applicability, published_on, source_version_note)
        with self.db.connect() as connection:
            document = connection.execute("SELECT * FROM documents WHERE document_id=?", (document_id,)).fetchone()
        if document is None:
            raise ServiceError("NO_RESULTS", "Registered document was not found.", 404, False)
        self._require(document["source_id"], "view_link", actor)
        scope_json = canonical_json(applicability)
        target_id = "target-" + digest([item_id, document_id, role, applicability, applicability_basis, locator, published_on, source_version_note])[:40]
        with self.db.transaction() as connection:
            connection.execute("INSERT OR IGNORE INTO items(item_id,name,provider) VALUES(?,?,?)", (item_id, item.get("name", item_id), item.get("provider", "UNKNOWN")))
            connection.execute("INSERT OR IGNORE INTO document_targets(target_id,item_id,document_id,role,applicability_json,applicability_basis,locator,published_on,source_version_note,created_by) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (target_id, item_id, document_id, role, scope_json, applicability_basis, locator, published_on, source_version_note, actor))
            if connection.execute("SELECT status FROM document_targets WHERE target_id=?", (target_id,)).fetchone()["status"] != "APPROVED":
                raise ServiceError("SOURCE_BLOCKED", "This document target registration was revoked.", 403, False)
        return next(row for row in self.registered_candidates(item_id) if row["target_id"] == target_id)

    def registered_candidates(self, item_id=None):
        """Trusted inventory only; model search results never create these rows."""
        if item_id is not None:
            self.catalog.get(item_id)
        sql = """SELECT d.*,t.target_id,t.item_id,t.role,t.applicability_json,t.applicability_basis,
                 t.locator,t.published_on,t.source_version_note,t.status,t.version_id
                 FROM document_targets t JOIN documents d ON d.document_id=t.document_id
                 WHERE t.status='APPROVED'"""
        with self.db.connect() as connection:
            rows = connection.execute(sql + (" AND t.item_id=?" if item_id is not None else "") + " ORDER BY t.target_id", (item_id,) if item_id is not None else ()).fetchall()
        return [{**dict(row), "id": row["target_id"], "registration_id": row["target_id"],
                 "url": row["canonical_url"], "type": row["document_type"],
                 "applicability": json.loads(row["applicability_json"]), "scope": json.loads(row["applicability_json"]),
                 "own_document": row["role"] in OWN_ROLES} for row in rows]

    def discovery_targets(self, item_id=None):
        return self.registered_candidates(item_id)

    def pending_candidates(self, item_id=None):
        return [row for row in self.registered_candidates(item_id) if row["version_id"] is None]

    @staticmethod
    def _target(connection, target_id):
        if not isinstance(target_id, str) or not target_id or len(target_id) > 200:
            raise ServiceError("INVALID_INPUT", "Invalid registered document target identifier.", 422, False)
        target = connection.execute("SELECT t.*,d.source_id,d.canonical_url,d.document_type FROM document_targets t JOIN documents d ON d.document_id=t.document_id WHERE t.target_id=? AND t.status='APPROVED'", (target_id,)).fetchone()
        if target is None:
            raise ServiceError("NO_RESULTS", "Approved document target was not found.", 404, False)
        return target

    def _complete_target(self, connection, target_id, version_id):
        if not isinstance(version_id, str) or not version_id or len(version_id) > 200:
            raise ServiceError("INVALID_INPUT", "Invalid document version identifier.", 422, False)
        target = self._target(connection, target_id)
        version = connection.execute("SELECT * FROM document_versions WHERE version_id=?", (version_id,)).fetchone()
        if (version is None or version["document_id"] != target["document_id"] or version["applicability_json"] != target["applicability_json"]
                or version["published_on"] != target["published_on"] or version["source_version_note"] != target["source_version_note"]):
            raise ServiceError("DOCUMENT_TARGET_MISMATCH", "Document version does not match the registered document and contract scope.", 409, False)
        self._link_version(connection, target["item_id"], version_id, target["role"], target["applicability_basis"], target["locator"])
        connection.execute("INSERT OR IGNORE INTO document_target_versions(target_id,version_id) VALUES(?,?)", (target_id, version_id))
        connection.execute("UPDATE document_targets SET version_id=? WHERE target_id=?", (version_id, target_id))
        previous = target["version_id"]
        if previous is not None and previous != version_id:
            still_used = connection.execute("SELECT 1 FROM document_targets WHERE item_id=? AND role=? AND version_id=? AND status='APPROVED'", (target["item_id"], target["role"], previous)).fetchone()
            if not still_used:
                connection.execute("UPDATE item_documents SET status='SUPERSEDED' WHERE item_id=? AND version_id=? AND role=?", (target["item_id"], previous, target["role"]))
        return {"target_id": target_id, "version_id": version_id, "item_id": target["item_id"], "role": target["role"]}

    def complete_target(self, actor, target_id, version_id):
        self._manager(actor)
        with self.db.connect() as connection:
            target = self._target(connection, target_id)
        with self.policy.read_guard():
            self._manager(actor)
            self._require(target["source_id"], "view_link", actor, version_id)
            with self.db.transaction() as connection:
                return self._complete_target(connection, target_id, version_id)

    def fetch_target(self, actor, target_id, cache_key=None, *, refresh=False):
        """Claim a finite persisted lease, fetch outside SQL, then fence activation."""
        self._manager(actor)
        if type(refresh) is not bool:
            raise ServiceError("INVALID_INPUT", "Invalid document refresh option.", 422, False)
        with self.db.connect() as connection:
            target = dict(self._target(connection, target_id))
        for action in ("view_link", "fetch", "store_body", "extract"):
            self._require(target["source_id"], action, actor, target["version_id"])
        # cache_key is caller bookkeeping only, never source/URL/scope authority.
        identity = [target["document_id"], target["applicability_json"], target["published_on"], target["source_version_note"]]
        fetch_key = "document-fetch-" + digest(identity)
        token, timestamp = uuid.uuid4().hex, time.time()
        cached_version = None
        cached_candidate, cached_lease_token = None, None
        with self.policy.read_guard(), self.db.transaction() as connection:
            self._manager(actor)
            current = self._target(connection, target_id)
            for action in ("view_link", "fetch", "store_body", "extract"):
                self.policy.require(current["source_id"], action, actor=actor, version_id=current["version_id"])
            lease = connection.execute("SELECT * FROM document_fetch_leases WHERE fetch_key=?", (fetch_key,)).fetchone()
            if lease is not None and lease["status"] == "FETCHING" and lease["lease_until"] > timestamp:
                raise ServiceError("DOCUMENT_FETCH_IN_PROGRESS", "This registered document is already being collected.", 409, True)
            if not refresh:
                cached_version = lease["version_id"] if lease is not None and lease["status"] == "FETCHED" else current["version_id"]
                if cached_version is None:
                    prior = connection.execute("SELECT version_id FROM document_versions WHERE document_id=? AND applicability_json=? AND published_on IS ? AND source_version_note=? ORDER BY rowid DESC LIMIT 1", (current["document_id"], current["applicability_json"], current["published_on"], current["source_version_note"])).fetchone()
                    cached_version = prior["version_id"] if prior is not None else None
                if cached_version is not None:
                    for action in ("view_link", "fetch", "store_body", "extract"):
                        self.policy.require(current["source_id"], action, actor=actor, version_id=cached_version)
                    cached_candidate = dict(connection.execute("SELECT * FROM document_versions WHERE version_id=?", (cached_version,)).fetchone())
                    cached_candidate["source_id"] = current["source_id"]
                    cached_lease_token = lease["token"] if lease is not None else None
            if cached_version is None:
                if self.fetcher is None:
                    raise ServiceError("SOURCE_BLOCKED", "Network document collection is disabled.", 403, False)
                connection.execute("INSERT INTO document_fetch_leases(fetch_key,document_id,status,token,lease_until,updated_at) VALUES(?,?,'FETCHING',?,?,?) ON CONFLICT(fetch_key) DO UPDATE SET status='FETCHING',token=excluded.token,lease_until=excluded.lease_until,updated_at=excluded.updated_at,error_code=NULL,attempt_no=document_fetch_leases.attempt_no+1",
                    (fetch_key, current["document_id"], token, timestamp + 300, timestamp))
        if cached_version is not None:
            # File integrity checks stay outside SQL; a simultaneous refresh may
            # invalidate this snapshot, so recheck its lease token before linking.
            self.sections(cached_candidate, actor, action="extract")
            with self.policy.read_guard():
                self._manager(actor)
                for action in ("view_link", "fetch", "store_body", "extract"):
                    self._require(target["source_id"], action, actor, cached_version)
                with self.db.transaction() as connection:
                    latest = connection.execute("SELECT token,status FROM document_fetch_leases WHERE fetch_key=?", (fetch_key,)).fetchone()
                    if (latest is not None and (latest["status"] == "FETCHING" or latest["token"] != cached_lease_token)) or (latest is None and cached_lease_token is not None):
                        raise ServiceError("DOCUMENT_FETCH_IN_PROGRESS", "The registered document cache changed during this request.", 409, True)
                    result = self._complete_target(connection, target_id, cached_version)
            return {**result, "cache_hit": True}
        try:
            response = self.fetcher.fetch(target["source_id"], target["canonical_url"], actor=actor)
            expected_type = "application/pdf" if target["document_type"] == "PDF" else "text/html"
            if getattr(response, "content_type", expected_type).split(";", 1)[0].strip().lower() != expected_type:
                raise ServiceError("PARSE_FAILED", "Document response type does not match its private registration.", 422, False)
            version = self.ingest_bytes(actor, target["document_id"], response.body,
                applicability=json.loads(target["applicability_json"]), published_on=target["published_on"], source_version_note=target["source_version_note"])
            with self.policy.read_guard():
                self._manager(actor)
                for action in ("view_link", "fetch", "store_body", "extract"):
                    self._require(target["source_id"], action, actor, version["version_id"])
                with self.db.transaction() as connection:
                    lease = connection.execute("SELECT token,status,lease_until FROM document_fetch_leases WHERE fetch_key=?", (fetch_key,)).fetchone()
                    if lease is None or lease["token"] != token or lease["status"] != "FETCHING" or lease["lease_until"] <= time.time():
                        raise ServiceError("DOCUMENT_FETCH_LEASE_LOST", "Document collection must be retried after an interrupted claim.", 409, True)
                    result = self._complete_target(connection, target_id, version["version_id"])
                    connection.execute("UPDATE document_fetch_leases SET status='FETCHED',version_id=?,lease_until=0,updated_at=?,error_code=NULL WHERE fetch_key=? AND token=?", (version["version_id"], time.time(), fetch_key, token))
            return {**result, "cache_hit": False}
        except Exception as error:
            code = error.code if isinstance(error, ServiceError) else "FETCH_FAILED"
            with self.db.transaction() as connection:
                connection.execute("UPDATE document_fetch_leases SET status='FAILED',lease_until=0,updated_at=?,error_code=? WHERE fetch_key=? AND token=? AND status='FETCHING'", (time.time(), code, fetch_key, token))
            if isinstance(error, ServiceError):
                raise
            raise ServiceError("FETCH_FAILED", "Registered document collection failed.", 503, True) from None

    def candidates(self, item_id=None):
        sql = """SELECT d.*,v.*,l.item_id,l.role,l.locator,l.applicability_basis,l.status
                 FROM item_documents l JOIN document_versions v ON v.version_id=l.version_id
                 JOIN documents d ON d.document_id=v.document_id WHERE l.status='APPROVED'"""
        with closing(self.db.connect()) as connection:
            rows = connection.execute(sql + (" AND l.item_id=?" if item_id else ""), (item_id,) if item_id else ()).fetchall()
        return [{**dict(row), "id": digest([row["item_id"], row["version_id"], row["role"]]),
                 "own_document": row["role"] in OWN_ROLES} for row in rows]

    def sections(self, candidate, actor=None, *, action="local_embed"):
        self._require(candidate["source_id"], action, actor, candidate["version_id"])
        path = Path(candidate["body_path"]).resolve()
        if self.body_dir.resolve() not in path.parents:
            raise ServiceError("SOURCE_BLOCKED", "Document storage boundary was violated.", 403, False)
        try:
            parts = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ServiceError("PARSE_FAILED", "Stored document text is unavailable.", 503, True) from exc
        if digest(parts) != candidate["body_hash"]:
            raise ServiceError("PARSE_FAILED", "Stored document integrity validation failed.", 409, False)
        return parts
