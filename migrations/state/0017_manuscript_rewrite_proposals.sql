CREATE TABLE manuscript_rewrite_proposals (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE REFERENCES runs(id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    document_id TEXT NOT NULL REFERENCES content_documents(id) ON DELETE CASCADE,
    base_revision_id TEXT NOT NULL REFERENCES revisions(id) ON DELETE RESTRICT,
    outline_node_id TEXT REFERENCES dissertation_nodes(id) ON DELETE SET NULL,
    instructions TEXT NOT NULL,
    use_rag INTEGER NOT NULL CHECK (use_rag IN (0, 1)),
    context_json TEXT NOT NULL,
    proposed_html TEXT NOT NULL,
    proposed_sha256 TEXT NOT NULL,
    validation_json TEXT NOT NULL,
    edit_summary_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'confirmed', 'checkpoint_failed', 'rejected')
    ),
    confirmed_revision_id TEXT REFERENCES revisions(id) ON DELETE SET NULL,
    artifact_path TEXT,
    checkpoint_sha TEXT,
    error_text TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE INDEX idx_manuscript_rewrite_proposals_document
ON manuscript_rewrite_proposals(document_id, created_at);
