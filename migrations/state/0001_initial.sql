CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    root_path TEXT NOT NULL UNIQUE,
    git_enabled INTEGER NOT NULL DEFAULT 1 CHECK (git_enabled IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE content_documents (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (
        kind IN ('paper', 'dissertation', 'translation', 'note', 'appendix', 'supplement')
    ),
    title TEXT NOT NULL,
    current_revision_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_content_documents_project ON content_documents(project_id);

CREATE TABLE revisions (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES content_documents(id) ON DELETE CASCADE,
    parent_revision_id TEXT REFERENCES revisions(id) ON DELETE RESTRICT,
    editor_type TEXT NOT NULL CHECK (editor_type IN ('human', 'agent', 'system')),
    operation TEXT NOT NULL CHECK (
        operation IN (
            'create', 'agent_edit', 'human_edit', 'human_import', 'merge',
            'translation', 'post_edit', 'appendix_update'
        )
    ),
    content_sha256 TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX idx_revisions_document_created ON revisions(document_id, created_at);

CREATE TABLE content_blocks (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES content_documents(id) ON DELETE CASCADE,
    block_kind TEXT NOT NULL CHECK (
        block_kind IN ('article', 'section', 'heading', 'paragraph', 'blockquote', 'list', 'note')
    ),
    protected INTEGER NOT NULL DEFAULT 0 CHECK (protected IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE INDEX idx_content_blocks_document ON content_blocks(document_id);

CREATE TABLE revision_blocks (
    revision_id TEXT NOT NULL REFERENCES revisions(id) ON DELETE CASCADE,
    block_id TEXT NOT NULL REFERENCES content_blocks(id) ON DELETE RESTRICT,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    html TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    PRIMARY KEY (revision_id, block_id),
    UNIQUE (revision_id, ordinal)
);

CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
    workflow_name TEXT NOT NULL,
    workflow_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('queued', 'running', 'waiting_human', 'completed', 'failed', 'cancelled')
    ),
    input_json TEXT NOT NULL DEFAULT '{}',
    output_json TEXT,
    error_json TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);

CREATE INDEX idx_runs_project_created ON runs(project_id, created_at);
CREATE INDEX idx_runs_status ON runs(status);

CREATE TABLE node_runs (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    node_id TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt > 0),
    status TEXT NOT NULL CHECK (
        status IN ('queued', 'running', 'waiting_human', 'completed', 'failed', 'skipped', 'cancelled')
    ),
    input_json TEXT NOT NULL DEFAULT '{}',
    output_json TEXT,
    error_json TEXT,
    started_at TEXT,
    completed_at TEXT,
    UNIQUE (run_id, node_id, attempt)
);

CREATE INDEX idx_node_runs_run ON node_runs(run_id, status);

CREATE TABLE human_reviews (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    node_run_id TEXT REFERENCES node_runs(id) ON DELETE SET NULL,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    question TEXT NOT NULL,
    options_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK (status IN ('open', 'approved', 'rejected', 'edited', 'cancelled')),
    response_json TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE INDEX idx_human_reviews_status ON human_reviews(status, created_at);

CREATE TABLE defects (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    node_run_id TEXT REFERENCES node_runs(id) ON DELETE SET NULL,
    defect_type TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('info', 'minor', 'major', 'critical')),
    status TEXT NOT NULL CHECK (status IN ('open', 'repairing', 'resolved', 'accepted', 'wont_fix')),
    object_type TEXT,
    object_id TEXT,
    message TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE INDEX idx_defects_run_status ON defects(run_id, status, severity);

CREATE TABLE metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    node_run_id TEXT REFERENCES node_runs(id) ON DELETE SET NULL,
    metric_name TEXT NOT NULL,
    numeric_value REAL,
    text_value TEXT,
    unit TEXT,
    created_at TEXT NOT NULL,
    CHECK (numeric_value IS NOT NULL OR text_value IS NOT NULL)
);

CREATE INDEX idx_metrics_run_name ON metrics(run_id, metric_name, created_at);

CREATE TABLE claims (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES content_documents(id) ON DELETE CASCADE,
    revision_id TEXT NOT NULL REFERENCES revisions(id) ON DELETE CASCADE,
    block_id TEXT REFERENCES content_blocks(id) ON DELETE SET NULL,
    claim_type TEXT NOT NULL,
    text TEXT NOT NULL,
    modality TEXT,
    status TEXT NOT NULL CHECK (status IN ('proposed', 'supported', 'contested', 'rejected')),
    created_at TEXT NOT NULL
);

CREATE INDEX idx_claims_revision ON claims(revision_id, status);

CREATE TABLE claim_relations (
    source_claim_id TEXT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    target_claim_id TEXT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL CHECK (
        relation_type IN ('supports', 'contradicts', 'qualifies', 'depends_on', 'alternative_to')
    ),
    rationale TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (source_claim_id, target_claim_id, relation_type),
    CHECK (source_claim_id <> target_claim_id)
);

CREATE TABLE evidence_links (
    id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    source_span_id TEXT NOT NULL,
    bibliographic_record_id TEXT,
    relation_type TEXT NOT NULL CHECK (
        relation_type IN ('supports', 'contradicts', 'contextualizes', 'quotes')
    ),
    locator TEXT,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX idx_evidence_links_claim ON evidence_links(claim_id);
CREATE INDEX idx_evidence_links_span ON evidence_links(source_span_id);

CREATE TABLE citation_events (
    id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL REFERENCES revisions(id) ON DELETE CASCADE,
    block_id TEXT REFERENCES content_blocks(id) ON DELETE SET NULL,
    source_span_id TEXT NOT NULL,
    bibliographic_record_id TEXT,
    citation_kind TEXT NOT NULL CHECK (citation_kind IN ('quote', 'paraphrase', 'reference')),
    locator TEXT,
    rendered_text TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE change_requests (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    request_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL CHECK (status IN ('open', 'approved', 'rejected', 'applied', 'cancelled')),
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE dissertation_nodes (
    id TEXT PRIMARY KEY,
    dissertation_document_id TEXT NOT NULL REFERENCES content_documents(id) ON DELETE CASCADE,
    parent_id TEXT REFERENCES dissertation_nodes(id) ON DELETE CASCADE,
    node_type TEXT NOT NULL CHECK (
        node_type IN ('root', 'part', 'chapter', 'section', 'appendix', 'supplement')
    ),
    title TEXT NOT NULL,
    order_index INTEGER NOT NULL CHECK (order_index >= 0),
    created_at TEXT NOT NULL,
    UNIQUE (dissertation_document_id, parent_id, order_index)
);

CREATE TABLE dissertation_memberships (
    id TEXT PRIMARY KEY,
    dissertation_document_id TEXT NOT NULL REFERENCES content_documents(id) ON DELETE CASCADE,
    outline_node_id TEXT NOT NULL REFERENCES dissertation_nodes(id) ON DELETE CASCADE,
    content_document_id TEXT NOT NULL REFERENCES content_documents(id) ON DELETE RESTRICT,
    included_revision_id TEXT NOT NULL REFERENCES revisions(id) ON DELETE RESTRICT,
    role TEXT NOT NULL CHECK (role IN ('chapter', 'appendix', 'supplement')),
    order_index INTEGER NOT NULL CHECK (order_index >= 0),
    status TEXT NOT NULL CHECK (status IN ('active', 'update_available', 'detached')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (dissertation_document_id, outline_node_id)
);

CREATE TABLE cross_references (
    id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL REFERENCES revisions(id) ON DELETE CASCADE,
    source_block_id TEXT NOT NULL REFERENCES content_blocks(id) ON DELETE CASCADE,
    target_document_id TEXT NOT NULL REFERENCES content_documents(id) ON DELETE CASCADE,
    target_block_id TEXT REFERENCES content_blocks(id) ON DELETE SET NULL,
    label TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('valid', 'broken', 'stale')),
    created_at TEXT NOT NULL
);

CREATE TABLE event_log (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    object_type TEXT,
    object_id TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX idx_event_log_run_event ON event_log(run_id, event_id);
CREATE INDEX idx_event_log_created ON event_log(created_at);

