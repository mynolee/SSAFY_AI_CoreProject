ALTER TABLE source_action_reviews ADD COLUMN reviewed_version_id TEXT;
CREATE TABLE document_targets (
    target_id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES items(item_id),
    document_id TEXT NOT NULL REFERENCES documents(document_id),
    role TEXT NOT NULL CHECK(role IN ('PRIMARY_DESCRIPTION','SERVICE_TERMS','INSURANCE_TERMS','TRANSFER_GUIDE')),
    applicability_json TEXT NOT NULL,
    applicability_basis TEXT NOT NULL,
    locator TEXT NOT NULL DEFAULT '',
    published_on TEXT,
    source_version_note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'APPROVED' CHECK(status IN ('APPROVED','REVOKED')),
    version_id TEXT REFERENCES document_versions(version_id),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX document_targets_by_item ON document_targets(item_id,status);
CREATE TABLE document_target_versions (
    target_id TEXT NOT NULL REFERENCES document_targets(target_id),
    version_id TEXT NOT NULL REFERENCES document_versions(version_id),
    linked_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY(target_id,version_id)
);
CREATE TABLE document_fetch_leases (
    fetch_key TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(document_id),
    status TEXT NOT NULL CHECK(status IN ('FETCHING','FETCHED','FAILED')),
    token TEXT NOT NULL,
    lease_until REAL NOT NULL,
    updated_at REAL NOT NULL,
    version_id TEXT REFERENCES document_versions(version_id),
    error_code TEXT,
    attempt_no INTEGER NOT NULL DEFAULT 1 CHECK(attempt_no>=1)
);
CREATE TABLE document_job_runs (
    job_id TEXT PRIMARY KEY REFERENCES index_jobs(job_id),
    request_json TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    attempt_no INTEGER NOT NULL DEFAULT 0 CHECK(attempt_no>=0),
    max_attempts INTEGER NOT NULL DEFAULT 3 CHECK(max_attempts BETWEEN 1 AND 10),
    claim_token TEXT,
    claimed_at REAL,
    lease_until REAL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CHECK((claim_token IS NULL AND claimed_at IS NULL AND lease_until IS NULL) OR (claim_token IS NOT NULL AND claimed_at IS NOT NULL AND lease_until IS NOT NULL)),
    CHECK(attempt_no<=max_attempts)
);
CREATE INDEX document_jobs_by_request_hash ON document_job_runs(request_hash);
CREATE TRIGGER document_target_insert_version_guard BEFORE INSERT ON document_targets WHEN NEW.version_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM document_versions WHERE version_id=NEW.version_id AND document_id=NEW.document_id AND applicability_json=NEW.applicability_json AND published_on IS NEW.published_on AND source_version_note=NEW.source_version_note) BEGIN SELECT RAISE(ABORT,'document version does not match the registered target'); END;
CREATE TRIGGER document_target_identity_guard BEFORE UPDATE ON document_targets WHEN NEW.target_id!=OLD.target_id OR NEW.item_id!=OLD.item_id OR NEW.document_id!=OLD.document_id OR NEW.role!=OLD.role OR NEW.applicability_json!=OLD.applicability_json OR NEW.applicability_basis!=OLD.applicability_basis OR NEW.locator!=OLD.locator OR NEW.published_on IS NOT OLD.published_on OR NEW.source_version_note!=OLD.source_version_note OR NEW.created_by!=OLD.created_by OR NEW.created_at!=OLD.created_at BEGIN SELECT RAISE(ABORT,'registered document target identity is immutable'); END;
CREATE TRIGGER document_target_no_delete BEFORE DELETE ON document_targets BEGIN SELECT RAISE(ABORT,'registered document target history is immutable'); END;
CREATE TRIGGER document_target_version_guard BEFORE UPDATE OF version_id ON document_targets WHEN NEW.version_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM document_versions WHERE version_id=NEW.version_id AND document_id=NEW.document_id AND applicability_json=NEW.applicability_json AND published_on IS NEW.published_on AND source_version_note=NEW.source_version_note) BEGIN SELECT RAISE(ABORT,'document version does not match the registered target'); END;
CREATE TRIGGER document_target_link_guard BEFORE INSERT ON document_target_versions WHEN NOT EXISTS(SELECT 1 FROM document_targets t JOIN document_versions v ON v.document_id=t.document_id WHERE t.target_id=NEW.target_id AND v.version_id=NEW.version_id AND v.applicability_json=t.applicability_json AND v.published_on IS t.published_on AND v.source_version_note=t.source_version_note) BEGIN SELECT RAISE(ABORT,'document history must match the registered target'); END;
CREATE TRIGGER document_target_history_no_update BEFORE UPDATE ON document_target_versions BEGIN SELECT RAISE(ABORT,'document target history is immutable'); END;
CREATE TRIGGER document_target_history_no_delete BEFORE DELETE ON document_target_versions BEGIN SELECT RAISE(ABORT,'document target history is immutable'); END;
CREATE TRIGGER document_version_no_update BEFORE UPDATE ON document_versions BEGIN SELECT RAISE(ABORT,'document version is immutable'); END;
CREATE TRIGGER document_version_no_delete BEFORE DELETE ON document_versions BEGIN SELECT RAISE(ABORT,'document version is immutable'); END;
CREATE TRIGGER document_job_identity_guard BEFORE UPDATE ON document_job_runs WHEN NEW.job_id!=OLD.job_id OR NEW.request_json!=OLD.request_json OR NEW.request_hash!=OLD.request_hash OR NEW.max_attempts!=OLD.max_attempts OR NEW.created_at!=OLD.created_at BEGIN SELECT RAISE(ABORT,'document job request identity is immutable'); END;
CREATE TRIGGER document_job_owner_guard BEFORE UPDATE OF requester,item_id ON index_jobs WHEN EXISTS(SELECT 1 FROM document_job_runs WHERE job_id=OLD.job_id) AND (NEW.requester IS NOT OLD.requester OR NEW.item_id!=OLD.item_id) BEGIN SELECT RAISE(ABORT,'document job owner and item are immutable'); END;
