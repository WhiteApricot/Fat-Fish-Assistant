"""Versioned SQLite schema for the replacement cognitive memory database."""

import sqlite3

SCHEMA_VERSION = 2
DATABASE_NAME = "ffa-cognitive-memory.sqlite3"

DDL = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE scope (
    id INTEGER PRIMARY KEY,
    agent TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('private', 'session', 'group', 'agent_public', 'shared')),
    scope_key TEXT NOT NULL,
    owner_principal TEXT,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    generation INTEGER NOT NULL DEFAULT 0 CHECK (generation >= 0),
    UNIQUE (agent, kind, scope_key)
);
CREATE INDEX scope_owner ON scope(owner_principal, kind);

CREATE TABLE session (
    id INTEGER PRIMARY KEY,
    scope_id INTEGER NOT NULL REFERENCES scope(id),
    provider TEXT NOT NULL,
    platform TEXT NOT NULL,
    bot TEXT NOT NULL,
    conversation_type TEXT NOT NULL,
    conversation_key TEXT NOT NULL,
    epoch INTEGER NOT NULL CHECK (epoch >= 0),
    opened_at INTEGER NOT NULL,
    closed_at INTEGER CHECK (closed_at >= opened_at),
    UNIQUE (provider, platform, bot, conversation_type, conversation_key, epoch),
    UNIQUE (scope_id, id)
);
CREATE INDEX session_scope_time ON session(scope_id, opened_at);

CREATE TABLE "grant" (
    id INTEGER PRIMARY KEY,
    source_scope INTEGER NOT NULL REFERENCES scope(id),
    target_scope INTEGER NOT NULL REFERENCES scope(id),
    issuer_principal TEXT NOT NULL,
    policy_key TEXT NOT NULL,
    issued_at INTEGER NOT NULL,
    expires_at INTEGER CHECK (expires_at >= issued_at),
    revoked_at INTEGER CHECK (revoked_at >= issued_at),
    revision INTEGER NOT NULL CHECK (revision > 0),
    CHECK (source_scope <> target_scope),
    UNIQUE (source_scope, target_scope, policy_key, revision)
);
CREATE INDEX grant_audience ON "grant"(target_scope, source_scope, expires_at) WHERE revoked_at IS NULL;

CREATE TABLE source (
    id INTEGER PRIMARY KEY,
    scope_id INTEGER NOT NULL REFERENCES scope(id),
    session_id INTEGER,
    source_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    author_principal TEXT,
    producer TEXT NOT NULL,
    observed_at INTEGER NOT NULL,
    ingested_at INTEGER NOT NULL,
    trust_class TEXT NOT NULL CHECK (trust_class IN (
        'direct_self', 'owner_confirmed', 'verified_connector',
        'external_unverified', 'third_party', 'inference'
    )),
    receipt_digest TEXT NOT NULL,
    expires_at INTEGER,
    revoked_at INTEGER,
    FOREIGN KEY (scope_id, session_id) REFERENCES session(scope_id, id),
    UNIQUE (scope_id, producer, source_key),
    UNIQUE (scope_id, id)
);
CREATE INDEX source_session_time ON source(session_id, observed_at);
CREATE INDEX source_receipt ON source(scope_id, receipt_digest);

CREATE TABLE event (
    id INTEGER PRIMARY KEY,
    scope_id INTEGER NOT NULL REFERENCES scope(id),
    source_id INTEGER NOT NULL,
    correlation_key TEXT NOT NULL,
    phase TEXT NOT NULL CHECK (phase IN (
        'observed', 'planned', 'submitted', 'running', 'succeeded', 'failed', 'cancelled', 'unknown'
    )),
    actor_kind TEXT NOT NULL,
    actor_ref TEXT,
    event_kind TEXT NOT NULL,
    occurred_at INTEGER NOT NULL,
    known_at INTEGER NOT NULL,
    snippet TEXT CHECK (length(snippet) <= 1500),
    payload_kind TEXT NOT NULL,
    payload_json TEXT,
    FOREIGN KEY (scope_id, source_id) REFERENCES source(scope_id, id),
    UNIQUE (scope_id, correlation_key, phase, source_id),
    UNIQUE (id, source_id),
    UNIQUE (scope_id, id)
);
CREATE INDEX event_timeline ON event(scope_id, event_kind, occurred_at DESC);
CREATE INDEX event_source ON event(source_id);

CREATE TABLE operation (
    id INTEGER PRIMARY KEY,
    scope_id INTEGER NOT NULL REFERENCES scope(id),
    producer TEXT NOT NULL,
    operation_key TEXT NOT NULL,
    phase TEXT NOT NULL CHECK (phase IN (
        'planned', 'submitted', 'running', 'succeeded', 'failed', 'cancelled', 'unknown'
    )),
    latest_event_id INTEGER NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    updated_at INTEGER NOT NULL,
    FOREIGN KEY (scope_id, latest_event_id) REFERENCES event(scope_id, id),
    UNIQUE (scope_id, producer, operation_key)
);

CREATE TABLE item (
    id INTEGER PRIMARY KEY,
    scope_id INTEGER NOT NULL REFERENCES scope(id),
    observer_ref TEXT NOT NULL,
    subject_ref TEXT NOT NULL,
    kind TEXT NOT NULL,
    slot_key TEXT NOT NULL,
    variant_key TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('candidate', 'active', 'conflict', 'archived', 'suspended', 'deleted')),
    current_rev INTEGER,
    last_rev INTEGER NOT NULL DEFAULT 0 CHECK (last_rev >= 0),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    FOREIGN KEY (id, current_rev) REFERENCES revision(item_id, rev) DEFERRABLE INITIALLY DEFERRED,
    UNIQUE (scope_id, id),
    UNIQUE (scope_id, observer_ref, subject_ref, kind, slot_key, variant_key)
);
CREATE INDEX item_recall ON item(scope_id, state, kind, updated_at DESC);
CREATE INDEX item_subject ON item(scope_id, observer_ref, subject_ref, kind);
CREATE INDEX item_recent ON item(scope_id, state, updated_at DESC, id DESC);

CREATE TABLE revision (
    item_id INTEGER NOT NULL REFERENCES item(id) ON DELETE CASCADE,
    rev INTEGER NOT NULL CHECK (rev > 0),
    body TEXT NOT NULL CHECK (length(body) BETWEEN 1 AND 4096),
    value_json TEXT NOT NULL,
    valid_from INTEGER,
    valid_to INTEGER,
    known_from INTEGER NOT NULL,
    known_to INTEGER,
    confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    importance REAL NOT NULL CHECK (importance BETWEEN 0 AND 1),
    trust_class TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    conflict_key TEXT,
    CHECK (valid_from IS NULL OR valid_to IS NULL OR valid_to > valid_from),
    CHECK (known_to IS NULL OR known_to > known_from),
    PRIMARY KEY (item_id, rev)
);
CREATE INDEX revision_time ON revision(item_id, known_to, valid_from, valid_to);
CREATE INDEX revision_conflict ON revision(conflict_key, known_to);

CREATE TABLE evidence (
    item_id INTEGER NOT NULL,
    rev INTEGER NOT NULL,
    source_id INTEGER NOT NULL REFERENCES source(id),
    event_id INTEGER,
    role TEXT NOT NULL CHECK (role IN ('support', 'refute')),
    grant_id INTEGER REFERENCES "grant"(id),
    weight REAL NOT NULL CHECK (weight BETWEEN 0 AND 1),
    independence_key TEXT NOT NULL,
    PRIMARY KEY (item_id, rev, source_id, role),
    FOREIGN KEY (item_id, rev) REFERENCES revision(item_id, rev) ON DELETE CASCADE,
    FOREIGN KEY (event_id, source_id) REFERENCES event(id, source_id)
);
CREATE INDEX evidence_source ON evidence(source_id, item_id, rev);
CREATE INDEX evidence_grant ON evidence(grant_id, item_id, rev);

CREATE TABLE profile (
    scope_id INTEGER NOT NULL,
    observer_ref TEXT NOT NULL,
    subject_ref TEXT NOT NULL,
    slot TEXT NOT NULL,
    source_item_id INTEGER NOT NULL,
    rev INTEGER NOT NULL,
    view_json TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    generation INTEGER NOT NULL CHECK (generation >= 0),
    PRIMARY KEY (scope_id, observer_ref, subject_ref, slot),
    FOREIGN KEY (scope_id, source_item_id) REFERENCES item(scope_id, id),
    FOREIGN KEY (source_item_id, rev) REFERENCES revision(item_id, rev) ON DELETE CASCADE
);

CREATE TABLE lifecycle (
    item_id INTEGER PRIMARY KEY REFERENCES item(id) ON DELETE CASCADE,
    pinned INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
    clarity INTEGER NOT NULL CHECK (clarity BETWEEN 0 AND 4),
    access_count INTEGER NOT NULL DEFAULT 0 CHECK (access_count >= 0),
    last_access_at INTEGER,
    last_decay_day INTEGER NOT NULL,
    retire_at INTEGER
);
CREATE INDEX lifecycle_retire ON lifecycle(retire_at, pinned);

CREATE TABLE search_doc (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL,
    rev INTEGER NOT NULL,
    scope_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    terms_text TEXT NOT NULL,
    norm_version INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    FOREIGN KEY (scope_id, item_id) REFERENCES item(scope_id, id),
    FOREIGN KEY (item_id, rev) REFERENCES revision(item_id, rev) ON DELETE CASCADE,
    UNIQUE (item_id, rev, norm_version),
    UNIQUE (scope_id, id)
);
CREATE INDEX search_scope_kind ON search_doc(scope_id, kind, updated_at DESC, id);

CREATE TABLE embedding (
    doc_id INTEGER NOT NULL REFERENCES search_doc(id) ON DELETE CASCADE,
    space TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    dimension INTEGER NOT NULL CHECK (dimension BETWEEN 1 AND 4096),
    vector BLOB NOT NULL CHECK (typeof(vector) = 'blob' AND length(vector) = dimension * 4),
    generated_at INTEGER NOT NULL,
    index_generation INTEGER NOT NULL,
    PRIMARY KEY (doc_id, space)
);

CREATE TABLE job (
    id INTEGER PRIMARY KEY,
    scope_id INTEGER NOT NULL REFERENCES scope(id),
    kind TEXT NOT NULL,
    dedup_key TEXT NOT NULL,
    priority INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'pending', 'ready', 'leased', 'applying', 'done', 'retry', 'cancelled', 'dead-letter'
    )),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    eligible_at INTEGER NOT NULL,
    lease_nonce TEXT,
    lease_until INTEGER,
    scope_gen INTEGER NOT NULL,
    watermark_from INTEGER,
    watermark_to INTEGER,
    last_error TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    CHECK ((lease_nonce IS NULL) = (lease_until IS NULL)),
    UNIQUE (scope_id, kind, dedup_key)
);
CREATE INDEX job_ready ON job(eligible_at, priority, id) WHERE status IN ('ready', 'retry');
CREATE INDEX job_lease ON job(lease_until) WHERE lease_nonce IS NOT NULL;

CREATE TABLE candidate (
    id INTEGER PRIMARY KEY,
    scope_id INTEGER NOT NULL REFERENCES scope(id),
    job_id INTEGER REFERENCES job(id) ON DELETE SET NULL,
    proposal_digest TEXT NOT NULL,
    kind TEXT NOT NULL,
    validated_json TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'accepted', 'rejected', 'expired')),
    expected_rev INTEGER,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL CHECK (expires_at >= created_at),
    UNIQUE (scope_id, proposal_digest)
);
CREATE INDEX candidate_review ON candidate(scope_id, status, created_at);

CREATE TABLE job_input (
    job_id INTEGER NOT NULL REFERENCES job(id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL REFERENCES source(id),
    event_id INTEGER NOT NULL,
    PRIMARY KEY (job_id, event_id),
    FOREIGN KEY (event_id, source_id) REFERENCES event(id, source_id)
);
CREATE INDEX job_input_source ON job_input(source_id, job_id);

CREATE TABLE audit (
    seq INTEGER PRIMARY KEY,
    scope_id INTEGER REFERENCES scope(id),
    operation TEXT NOT NULL,
    opaque_ref TEXT NOT NULL,
    actor_class TEXT NOT NULL,
    result TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    at INTEGER NOT NULL
);
CREATE INDEX audit_scope_time ON audit(scope_id, at DESC);

CREATE TABLE erasure (
    ledger_seq INTEGER PRIMARY KEY CHECK (ledger_seq > 0),
    generation INTEGER NOT NULL CHECK (generation >= 0),
    target_kind TEXT NOT NULL,
    key_id TEXT NOT NULL,
    target_hmac TEXT NOT NULL,
    scope_hmac TEXT NOT NULL,
    requested_at INTEGER NOT NULL,
    applied_at INTEGER
);
CREATE INDEX erasure_target ON erasure(scope_hmac, key_id, target_kind, target_hmac);

CREATE TABLE posting (
    scope_id INTEGER NOT NULL,
    term TEXT NOT NULL,
    doc_id INTEGER NOT NULL,
    tf INTEGER NOT NULL CHECK (tf > 0),
    PRIMARY KEY (scope_id, term, doc_id),
    FOREIGN KEY (scope_id, doc_id) REFERENCES search_doc(scope_id, id) ON DELETE CASCADE
);
CREATE INDEX posting_document ON posting(doc_id);
"""

# The same policy predicate governs both creation and replacement of evidence.
EVIDENCE_ALLOWED = """
    SELECT 1 FROM item AS i
    JOIN scope AS destination ON destination.id = i.scope_id AND destination.enabled = 1
    JOIN source AS s ON s.id = NEW.source_id AND s.revoked_at IS NULL
    JOIN scope AS origin ON origin.id = s.scope_id AND origin.enabled = 1
    WHERE i.id = NEW.item_id AND origin.agent = destination.agent
      AND (destination.kind <> 'agent_public' OR (
          s.kind = 'public_attestation' AND s.trust_class = 'verified_connector'
          AND s.scope_id = i.scope_id AND s.author_principal IS NULL
      ))
      AND (
          (s.scope_id = i.scope_id AND NEW.grant_id IS NULL)
          OR EXISTS (
              SELECT 1 FROM "grant" AS g
              WHERE g.id = NEW.grant_id AND g.source_scope = s.scope_id
                AND g.target_scope = i.scope_id AND g.revoked_at IS NULL
          )
      )
"""

TRIGGERS = "\n".join(
    f"""CREATE TRIGGER evidence_guard_{action.lower()} BEFORE {action} ON evidence
    WHEN NOT EXISTS ({EVIDENCE_ALLOWED})
    BEGIN
        SELECT RAISE(ABORT, 'Evidence is not valid for this item scope');
    END;"""
    for action in ("INSERT", "UPDATE")
)


GRAPH_DDL = (
    """CREATE TABLE memory_node (
        id INTEGER PRIMARY KEY,
        scope_id INTEGER NOT NULL REFERENCES scope(id),
        kind TEXT NOT NULL CHECK (kind IN ('entity','episode')),
        created_at INTEGER NOT NULL,
        UNIQUE(scope_id,id)
    )""",
    """CREATE TABLE memory_mention (
        scope_id INTEGER NOT NULL,
        node_id INTEGER NOT NULL,
        item_id INTEGER NOT NULL,
        rev INTEGER NOT NULL,
        label TEXT NOT NULL CHECK(length(label) BETWEEN 1 AND 64),
        PRIMARY KEY(item_id,rev,node_id,label),
        FOREIGN KEY(scope_id,node_id) REFERENCES memory_node(scope_id,id) ON DELETE CASCADE,
        FOREIGN KEY(scope_id,item_id) REFERENCES item(scope_id,id),
        FOREIGN KEY(item_id,rev) REFERENCES revision(item_id,rev) ON DELETE CASCADE
    )""",
    "CREATE INDEX memory_mention_node ON memory_mention(scope_id,node_id,item_id,rev)",
    """CREATE TRIGGER cleanup_memory_node AFTER DELETE ON memory_mention
        BEGIN
            DELETE FROM memory_node WHERE id=OLD.node_id AND NOT EXISTS (
                SELECT 1 FROM memory_mention WHERE scope_id=OLD.scope_id AND node_id=OLD.node_id
            );
        END""",
)


def initialize(db: sqlite3.Connection, installation: str, database_id: str) -> None:
    """Initialize an empty database. Reject foreign and legacy databases."""
    if db.in_transaction:
        raise ValueError("Schema initialization requires an idle connection")
    db.execute("PRAGMA foreign_keys = ON")
    try:
        db.execute("BEGIN IMMEDIATE")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version in (1, SCHEMA_VERSION):
            row = db.execute("SELECT value FROM meta WHERE key='installation'").fetchone()
            if row is None or row[0] != installation:
                raise ValueError("Memory database installation mismatch")
        else:
            if version != 0 or db.execute(
                "SELECT 1 FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
            ).fetchone():
                raise ValueError("Unsupported memory database schema")
            statement = ""
            for line in (DDL + "\n" + TRIGGERS).splitlines(keepends=True):
                statement += line
                if sqlite3.complete_statement(statement):
                    db.execute(statement)
                    statement = ""
            if statement.strip():
                raise ValueError("Incomplete memory schema")
            db.executemany("INSERT INTO meta VALUES (?, ?)", (
                ("installation", installation), ("db_uuid", database_id),
                ("ledger_applied_seq", "0"), ("index_generation", "0"),
                ("policy_generation", "0"), ("search_backend", "posting"),
            ))
        if version < SCHEMA_VERSION:
            for statement in GRAPH_DDL:
                db.execute(statement)
            db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        db.commit()
    except BaseException:
        db.rollback()
        raise
