CREATE TABLE html_import_previews (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    document_id TEXT NOT NULL REFERENCES content_documents(id) ON DELETE CASCADE,
    source_path TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    original_html TEXT NOT NULL,
    proposed_html TEXT NOT NULL,
    validation_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'imported', 'checkpoint_failed', 'rejected')
    ),
    imported_revision_id TEXT REFERENCES revisions(id) ON DELETE SET NULL,
    artifact_path TEXT,
    checkpoint_sha TEXT,
    error_text TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE INDEX idx_html_import_previews_document
ON html_import_previews(document_id, created_at);
