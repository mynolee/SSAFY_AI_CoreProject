CREATE TABLE sources (
    source_id TEXT PRIMARY KEY,
    canonical_url TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT ''
);
CREATE TABLE items (
    item_id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT ''
);
CREATE TABLE documents (
    document_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(source_id),
    title TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    document_type TEXT NOT NULL CHECK(document_type IN ('HTML','PDF')),
    operator TEXT,
    language TEXT,
    review_json TEXT NOT NULL
);
CREATE TABLE document_versions (
    version_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(document_id),
    raw_hash TEXT,
    body_hash TEXT NOT NULL,
    checked_on TEXT,
    published_on TEXT,
    source_version_note TEXT,
    applicability_json TEXT NOT NULL,
    body_path TEXT NOT NULL,
    UNIQUE(document_id,body_hash)
);
CREATE TABLE item_documents (
    item_id TEXT NOT NULL REFERENCES items(item_id),
    version_id TEXT NOT NULL REFERENCES document_versions(version_id),
    role TEXT NOT NULL CHECK(role IN ('PRIMARY_DESCRIPTION','SERVICE_TERMS','INSURANCE_TERMS','TRANSFER_GUIDE')),
    locator TEXT,
    applicability_basis TEXT,
    status TEXT,
    PRIMARY KEY(item_id,version_id,role)
);
CREATE TABLE source_action_reviews (
    review_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(source_id),
    document_version_id TEXT REFERENCES document_versions(version_id),
    action TEXT NOT NULL CHECK(action IN ('view_link','fetch','store_body','extract','local_embed','external_process','display_excerpt','public_redistribute')),
    status TEXT NOT NULL CHECK(status IN ('ALLOWED','BLOCKED','UNRESOLVED')),
    basis_type TEXT NOT NULL CHECK(basis_type IN ('FACT_ONLY','LICENSE','STATUTORY_EXCEPTION','OTHER_RECORDED_BASIS')),
    basis_url TEXT,
    reviewed_on TEXT NOT NULL,
    scope TEXT NOT NULL,
    conditions TEXT NOT NULL DEFAULT '',
    processing_environment TEXT NOT NULL DEFAULT 'LOCAL'
);
CREATE TABLE index_manifests (
    revision TEXT PRIMARY KEY,
    dataset_hash TEXT,
    version_ids_json TEXT,
    embedding_model TEXT,
    dimension INTEGER CHECK(dimension IS NULL OR dimension>0),
    chunk_config_hash TEXT,
    success_count INTEGER NOT NULL DEFAULT 0 CHECK(success_count>=0),
    failure_count INTEGER NOT NULL DEFAULT 0 CHECK(failure_count>=0),
    activated_at TEXT,
    active INTEGER NOT NULL DEFAULT 0 CHECK(active IN (0,1))
);
CREATE UNIQUE INDEX one_active_manifest ON index_manifests(active) WHERE active=1;
CREATE TABLE index_jobs (
    job_id TEXT PRIMARY KEY,
    requester TEXT,
    item_id TEXT NOT NULL REFERENCES items(item_id),
    status TEXT NOT NULL,
    stage TEXT,
    failure_code TEXT,
    candidate_count INTEGER NOT NULL DEFAULT 0 CHECK(candidate_count>=0),
    body_count INTEGER NOT NULL DEFAULT 0 CHECK(body_count>=0),
    chunk_count INTEGER NOT NULL DEFAULT 0 CHECK(chunk_count>=0),
    revision TEXT REFERENCES index_manifests(revision),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE conversations (
    user_id TEXT NOT NULL,
    item_id TEXT NOT NULL REFERENCES items(item_id),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY(user_id,item_id)
);
CREATE TABLE chat_history (
    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    item_id TEXT NOT NULL REFERENCES items(item_id),
    question TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX history_by_user_item ON chat_history(user_id,item_id,message_id);
CREATE TABLE rag_cache (
    cache_kind TEXT NOT NULL,
    cache_key TEXT NOT NULL,
    status TEXT NOT NULL,
    value_json TEXT NOT NULL,
    expires_at REAL,
    PRIMARY KEY(cache_kind,cache_key)
);
CREATE TABLE demo_users (
    user_id TEXT PRIMARY KEY,
    role TEXT NOT NULL CHECK(role IN ('buyer','seller','admin','outsider'))
);
INSERT INTO demo_users(user_id,role) VALUES('buyer','buyer'),('seller','seller'),('admin','admin'),('outsider','outsider');
CREATE TABLE demo_wallets (
    user_id TEXT PRIMARY KEY REFERENCES demo_users(user_id),
    available INTEGER NOT NULL CHECK(typeof(available)='integer' AND available>=0)
);
INSERT INTO demo_wallets(user_id,available) VALUES('buyer',2000000),('seller',0),('admin',0),('outsider',0);
CREATE TABLE trades (
    trade_id TEXT PRIMARY KEY,
    buyer_id TEXT NOT NULL REFERENCES demo_users(user_id),
    seller_id TEXT NOT NULL REFERENCES demo_users(user_id),
    admin_id TEXT NOT NULL DEFAULT 'admin' REFERENCES demo_users(user_id),
    item_id TEXT NOT NULL REFERENCES items(item_id),
    status TEXT NOT NULL DEFAULT 'DRAFT' CHECK(status IN ('DRAFT','AGREED','FUNDED','ACTIVE','CLOSED','CANCELLED')),
    version INTEGER NOT NULL DEFAULT 1 CHECK(typeof(version)='integer' AND version>=1),
    contract_revision INTEGER NOT NULL DEFAULT 1 CHECK(contract_revision>=1),
    review_id TEXT,
    evidence_id TEXT,
    receipt_revision INTEGER,
    funded INTEGER NOT NULL DEFAULT 0 CHECK(funded IN (0,1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CHECK(buyer_id!=seller_id),
    CHECK(status!='CLOSED' OR funded=1),
    CHECK(status!='CANCELLED' OR funded=0),
    FOREIGN KEY(trade_id,contract_revision) REFERENCES contract_revisions(trade_id,revision) DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY(trade_id,receipt_revision) REFERENCES contract_revisions(trade_id,revision),
    FOREIGN KEY(trade_id,review_id) REFERENCES review_snapshots(trade_id,review_id),
    FOREIGN KEY(trade_id,evidence_id) REFERENCES evidence_bundles(trade_id,evidence_id)
);
CREATE TABLE contract_revisions (
    trade_id TEXT NOT NULL REFERENCES trades(trade_id),
    revision INTEGER NOT NULL CHECK(revision>=1),
    hash TEXT NOT NULL,
    item_id TEXT NOT NULL REFERENCES items(item_id),
    D INTEGER NOT NULL CHECK(typeof(D)='integer' AND D>0),
    W INTEGER NOT NULL CHECK(typeof(W)='integer' AND W>=0),
    terms_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY(trade_id,revision)
);
CREATE TABLE component_balances (
    trade_id TEXT NOT NULL REFERENCES trades(trade_id),
    component TEXT NOT NULL CHECK(component IN ('D','W')),
    deposited INTEGER NOT NULL DEFAULT 0 CHECK(typeof(deposited)='integer' AND deposited>=0),
    held INTEGER NOT NULL DEFAULT 0 CHECK(typeof(held)='integer' AND held>=0),
    pay_reserved INTEGER NOT NULL DEFAULT 0 CHECK(typeof(pay_reserved)='integer' AND pay_reserved>=0),
    refund_reserved INTEGER NOT NULL DEFAULT 0 CHECK(typeof(refund_reserved)='integer' AND refund_reserved>=0),
    paid INTEGER NOT NULL DEFAULT 0 CHECK(typeof(paid)='integer' AND paid>=0),
    refunded INTEGER NOT NULL DEFAULT 0 CHECK(typeof(refunded)='integer' AND refunded>=0),
    PRIMARY KEY(trade_id,component),
    CHECK(deposited=held+pay_reserved+refund_reserved+paid+refunded)
);
CREATE TABLE review_snapshots (
    review_id TEXT PRIMARY KEY,
    trade_id TEXT NOT NULL REFERENCES trades(trade_id),
    revision INTEGER NOT NULL CHECK(revision>=1),
    hash TEXT NOT NULL,
    classification TEXT NOT NULL CHECK(classification IN ('TRANSFERABLE','NOT_TRANSFERABLE','CONDITIONAL','NEEDS_INFORMATION')),
    applicability_ok INTEGER NOT NULL CHECK(applicability_ok IN (0,1)),
    conditions_ok INTEGER NOT NULL CHECK(conditions_ok IN (0,1)),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(trade_id,review_id),
    UNIQUE(trade_id,revision)
);
CREATE TABLE evidence_bundles (
    evidence_id TEXT PRIMARY KEY,
    trade_id TEXT NOT NULL REFERENCES trades(trade_id),
    revision INTEGER NOT NULL CHECK(revision>=1),
    hash TEXT NOT NULL,
    contract_revision INTEGER NOT NULL,
    review_id TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(trade_id,evidence_id),
    UNIQUE(trade_id,revision),
    FOREIGN KEY(trade_id,contract_revision) REFERENCES contract_revisions(trade_id,revision),
    FOREIGN KEY(trade_id,review_id) REFERENCES review_snapshots(trade_id,review_id)
);
CREATE TABLE evidence_files (
    file_id TEXT PRIMARY KEY,
    trade_id TEXT NOT NULL REFERENCES trades(trade_id),
    synthetic INTEGER NOT NULL DEFAULT 1 CHECK(synthetic IN (0,1)),
    media_type TEXT NOT NULL,
    byte_count INTEGER NOT NULL CHECK(byte_count>=0 AND byte_count<=10485760),
    storage_key TEXT,
    status TEXT NOT NULL DEFAULT 'SYNTHETIC' CHECK(status IN ('SYNTHETIC','QUARANTINED','SCANNED','REJECTED')),
    UNIQUE(trade_id,file_id)
);
CREATE TABLE evidence_confirmations (
    trade_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    actor TEXT NOT NULL REFERENCES demo_users(user_id),
    target_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY(evidence_id,actor),
    FOREIGN KEY(trade_id,evidence_id) REFERENCES evidence_bundles(trade_id,evidence_id)
);
CREATE TABLE receipt_confirmations (
    trade_id TEXT NOT NULL,
    contract_revision INTEGER NOT NULL,
    actor TEXT NOT NULL REFERENCES demo_users(user_id),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY(trade_id,contract_revision),
    FOREIGN KEY(trade_id,contract_revision) REFERENCES contract_revisions(trade_id,revision)
);
CREATE TABLE transfer_events (
    event_id TEXT PRIMARY KEY,
    trade_id TEXT NOT NULL REFERENCES trades(trade_id),
    actor TEXT NOT NULL REFERENCES demo_users(user_id),
    status TEXT NOT NULL CHECK(status IN ('NOT_STARTED','IN_PROGRESS','PARTIES_CONFIRMED','FAILED','ON_HOLD')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE settlement_proposals (
    proposal_id TEXT PRIMARY KEY,
    trade_id TEXT NOT NULL REFERENCES trades(trade_id),
    revision INTEGER NOT NULL CHECK(revision>=1),
    hash TEXT NOT NULL,
    contract_revision INTEGER NOT NULL,
    review_id TEXT,
    evidence_id TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(trade_id,proposal_id),
    UNIQUE(trade_id,proposal_id,revision),
    FOREIGN KEY(trade_id,contract_revision) REFERENCES contract_revisions(trade_id,revision),
    FOREIGN KEY(trade_id,review_id) REFERENCES review_snapshots(trade_id,review_id),
    FOREIGN KEY(trade_id,evidence_id) REFERENCES evidence_bundles(trade_id,evidence_id)
);
CREATE TABLE allocations (
    proposal_id TEXT NOT NULL REFERENCES settlement_proposals(proposal_id),
    component TEXT NOT NULL CHECK(component IN ('D','W')),
    pay_won INTEGER NOT NULL CHECK(typeof(pay_won)='integer' AND pay_won>=0),
    refund_won INTEGER NOT NULL CHECK(typeof(refund_won)='integer' AND refund_won>=0),
    PRIMARY KEY(proposal_id,component),
    CHECK(pay_won+refund_won>0)
);
CREATE TABLE settlement_groups (
    group_id TEXT PRIMARY KEY,
    trade_id TEXT NOT NULL,
    proposal_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('RESERVED','PROCESSING','SUCCEEDED','FAILED','UNKNOWN','CANCELLED','PARTIAL')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(proposal_id,revision),
    UNIQUE(trade_id,group_id),
    UNIQUE(trade_id,group_id,proposal_id,revision),
    FOREIGN KEY(trade_id,proposal_id,revision) REFERENCES settlement_proposals(trade_id,proposal_id,revision)
);
CREATE TABLE settlement_operations (
    operation_id TEXT PRIMARY KEY,
    trade_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    proposal_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    component TEXT NOT NULL CHECK(component IN ('D','W')),
    direction TEXT NOT NULL CHECK(direction IN ('PAY','REFUND')),
    amount INTEGER NOT NULL CHECK(typeof(amount)='integer' AND amount>0),
    status TEXT NOT NULL CHECK(status IN ('RESERVED','PROCESSING','SUCCEEDED','FAILED','UNKNOWN','CANCELLED')),
    attempt_no INTEGER NOT NULL DEFAULT 1 CHECK(typeof(attempt_no)='integer' AND attempt_no>=1),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(proposal_id,revision,component,direction),
    UNIQUE(trade_id,operation_id),
    FOREIGN KEY(trade_id,group_id,proposal_id,revision) REFERENCES settlement_groups(trade_id,group_id,proposal_id,revision),
    FOREIGN KEY(proposal_id,component) REFERENCES allocations(proposal_id,component),
    FOREIGN KEY(trade_id,component) REFERENCES component_balances(trade_id,component)
);
CREATE TABLE jobs (
    job_id TEXT PRIMARY KEY,
    trade_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    attempts_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('RESERVED','PROCESSING','SUCCEEDED','FAILED','UNKNOWN','CANCELLED','PENDING','DONE')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY(trade_id,group_id) REFERENCES settlement_groups(trade_id,group_id)
);
CREATE TABLE ledger_groups (
    group_id TEXT PRIMARY KEY,
    trade_id TEXT NOT NULL REFERENCES trades(trade_id),
    operation_id TEXT,
    attempt_no INTEGER NOT NULL DEFAULT 1 CHECK(attempt_no>=1),
    event_type TEXT NOT NULL,
    event_key TEXT UNIQUE,
    posted INTEGER NOT NULL DEFAULT 0 CHECK(posted IN (0,1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(operation_id,attempt_no,event_type),
    UNIQUE(trade_id,group_id),
    FOREIGN KEY(trade_id,operation_id) REFERENCES settlement_operations(trade_id,operation_id)
);
CREATE TABLE ledger_entries (
    group_id TEXT NOT NULL REFERENCES ledger_groups(group_id),
    line_no INTEGER NOT NULL CHECK(line_no>=1),
    account TEXT NOT NULL,
    debit INTEGER NOT NULL DEFAULT 0 CHECK(typeof(debit)='integer' AND debit>=0),
    credit INTEGER NOT NULL DEFAULT 0 CHECK(typeof(credit)='integer' AND credit>=0),
    PRIMARY KEY(group_id,line_no),
    CHECK((debit>0 AND credit=0) OR (credit>0 AND debit=0))
);
CREATE TABLE deposit_records (
    trade_id TEXT NOT NULL,
    contract_revision INTEGER NOT NULL,
    amount INTEGER NOT NULL CHECK(typeof(amount)='integer' AND amount>0),
    ledger_group_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY(trade_id,contract_revision),
    FOREIGN KEY(trade_id,contract_revision) REFERENCES contract_revisions(trade_id,revision),
    FOREIGN KEY(trade_id,ledger_group_id) REFERENCES ledger_groups(trade_id,group_id)
);
CREATE TABLE disputes (
    dispute_id TEXT PRIMARY KEY,
    trade_id TEXT NOT NULL REFERENCES trades(trade_id),
    scope TEXT NOT NULL CHECK(scope IN ('D','W','BOTH')),
    status TEXT NOT NULL CHECK(status IN ('OPEN','PROPOSED','RESOLVED')),
    resolution_id TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(trade_id,dispute_id),
    FOREIGN KEY(trade_id,resolution_id) REFERENCES resolution_revisions(trade_id,resolution_id)
);
CREATE TABLE resolution_revisions (
    resolution_id TEXT PRIMARY KEY,
    trade_id TEXT NOT NULL,
    dispute_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision>=1),
    hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(trade_id,resolution_id),
    UNIQUE(dispute_id,revision),
    FOREIGN KEY(trade_id,dispute_id) REFERENCES disputes(trade_id,dispute_id)
);
CREATE TABLE consents (
    trade_id TEXT NOT NULL REFERENCES trades(trade_id),
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision>=1),
    actor TEXT NOT NULL REFERENCES demo_users(user_id),
    target_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY(actor,target_type,target_id,revision)
);
CREATE TABLE resolution_consents (
    trade_id TEXT NOT NULL,
    resolution_id TEXT NOT NULL,
    actor TEXT NOT NULL REFERENCES demo_users(user_id),
    target_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY(resolution_id,actor),
    FOREIGN KEY(trade_id,resolution_id) REFERENCES resolution_revisions(trade_id,resolution_id)
);
CREATE TABLE idempotency_records (
    actor TEXT NOT NULL REFERENCES demo_users(user_id),
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY(actor,scope,key)
);
CREATE TABLE audit_events (
    event_id TEXT PRIMARY KEY,
    trade_id TEXT NOT NULL REFERENCES trades(trade_id),
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target_id TEXT,
    code TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE simulation_events (
    event_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES settlement_operations(operation_id),
    attempt_no INTEGER NOT NULL CHECK(attempt_no>=1),
    outcome TEXT NOT NULL CHECK(outcome IN ('SUCCEEDED','FAILED','UNKNOWN','DELAYED')),
    applied INTEGER NOT NULL DEFAULT 0 CHECK(applied IN (0,1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(operation_id,attempt_no,event_id)
);
CREATE INDEX operations_by_trade_status ON settlement_operations(trade_id,status);
CREATE INDEX groups_by_trade ON settlement_groups(trade_id,created_at);
CREATE INDEX disputes_by_trade_status ON disputes(trade_id,status);
CREATE INDEX jobs_by_status ON jobs(status,created_at);
CREATE TRIGGER ledger_no_preposted_insert BEFORE INSERT ON ledger_groups WHEN NEW.posted!=0 BEGIN SELECT RAISE(ABORT,'ledger must be posted after entries'); END;
CREATE TRIGGER ledger_post_balanced BEFORE UPDATE ON ledger_groups BEGIN
    SELECT CASE WHEN OLD.posted!=0 OR NEW.posted!=1 OR NEW.group_id!=OLD.group_id OR NEW.trade_id!=OLD.trade_id OR NEW.operation_id IS NOT OLD.operation_id OR NEW.attempt_no!=OLD.attempt_no OR NEW.event_type!=OLD.event_type OR NEW.event_key IS NOT OLD.event_key OR NEW.created_at!=OLD.created_at THEN RAISE(ABORT,'ledger group is append-only') END;
    SELECT CASE WHEN (SELECT COUNT(*) FROM ledger_entries WHERE group_id=OLD.group_id)<2 OR (SELECT COALESCE(SUM(debit),0) FROM ledger_entries WHERE group_id=OLD.group_id)<=0 OR (SELECT COALESCE(SUM(debit-credit),0) FROM ledger_entries WHERE group_id=OLD.group_id)!=0 THEN RAISE(ABORT,'ledger group is unbalanced') END;
END;
CREATE TRIGGER ledger_group_no_delete BEFORE DELETE ON ledger_groups BEGIN SELECT RAISE(ABORT,'ledger group is append-only'); END;
CREATE TRIGGER ledger_entry_only_open BEFORE INSERT ON ledger_entries WHEN (SELECT posted FROM ledger_groups WHERE group_id=NEW.group_id)!=0 BEGIN SELECT RAISE(ABORT,'posted ledger is immutable'); END;
CREATE TRIGGER ledger_entry_no_update BEFORE UPDATE ON ledger_entries BEGIN SELECT RAISE(ABORT,'ledger entry is append-only'); END;
CREATE TRIGGER ledger_entry_no_delete BEFORE DELETE ON ledger_entries BEGIN SELECT RAISE(ABORT,'ledger entry is append-only'); END;
CREATE TRIGGER contract_no_update BEFORE UPDATE ON contract_revisions BEGIN SELECT RAISE(ABORT,'contract revision is immutable'); END;
CREATE TRIGGER contract_no_delete BEFORE DELETE ON contract_revisions BEGIN SELECT RAISE(ABORT,'contract revision is immutable'); END;
CREATE TRIGGER review_no_update BEFORE UPDATE ON review_snapshots BEGIN SELECT RAISE(ABORT,'review snapshot is immutable'); END;
CREATE TRIGGER review_no_delete BEFORE DELETE ON review_snapshots BEGIN SELECT RAISE(ABORT,'review snapshot is immutable'); END;
CREATE TRIGGER evidence_no_update BEFORE UPDATE ON evidence_bundles BEGIN SELECT RAISE(ABORT,'evidence revision is immutable'); END;
CREATE TRIGGER evidence_no_delete BEFORE DELETE ON evidence_bundles BEGIN SELECT RAISE(ABORT,'evidence revision is immutable'); END;
CREATE TRIGGER proposal_no_update BEFORE UPDATE ON settlement_proposals BEGIN SELECT RAISE(ABORT,'proposal revision is immutable'); END;
CREATE TRIGGER proposal_no_delete BEFORE DELETE ON settlement_proposals BEGIN SELECT RAISE(ABORT,'proposal revision is immutable'); END;
CREATE TRIGGER allocation_no_update BEFORE UPDATE ON allocations BEGIN SELECT RAISE(ABORT,'allocation is immutable'); END;
CREATE TRIGGER allocation_no_delete BEFORE DELETE ON allocations BEGIN SELECT RAISE(ABORT,'allocation is immutable'); END;
CREATE TRIGGER resolution_no_update BEFORE UPDATE ON resolution_revisions BEGIN SELECT RAISE(ABORT,'resolution revision is immutable'); END;
CREATE TRIGGER resolution_no_delete BEFORE DELETE ON resolution_revisions BEGIN SELECT RAISE(ABORT,'resolution revision is immutable'); END;
CREATE TRIGGER consent_no_update BEFORE UPDATE ON consents BEGIN SELECT RAISE(ABORT,'consent is immutable'); END;
CREATE TRIGGER consent_no_delete BEFORE DELETE ON consents BEGIN SELECT RAISE(ABORT,'consent is immutable'); END;
CREATE TRIGGER evidence_confirmation_no_update BEFORE UPDATE ON evidence_confirmations BEGIN SELECT RAISE(ABORT,'evidence confirmation is immutable'); END;
CREATE TRIGGER evidence_confirmation_no_delete BEFORE DELETE ON evidence_confirmations BEGIN SELECT RAISE(ABORT,'evidence confirmation is immutable'); END;
CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_events BEGIN SELECT RAISE(ABORT,'audit is append-only'); END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_events BEGIN SELECT RAISE(ABORT,'audit is append-only'); END;
