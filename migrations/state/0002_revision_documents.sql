CREATE TABLE revision_documents (
    revision_id TEXT PRIMARY KEY REFERENCES revisions(id) ON DELETE CASCADE,
    canonical_html TEXT NOT NULL,
    content_sha256 TEXT NOT NULL
);

