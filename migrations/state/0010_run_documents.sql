CREATE TABLE run_documents (
    run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    document_id TEXT NOT NULL REFERENCES content_documents(id) ON DELETE CASCADE,
    final_revision_id TEXT REFERENCES revisions(id) ON DELETE SET NULL,
    git_commit_sha TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (document_id)
);
