CREATE TABLE oa_acquisitions (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES literature_candidates(id) ON DELETE RESTRICT,
    bibliographic_record_id TEXT NOT NULL
        REFERENCES bibliographic_records(id) ON DELETE RESTRICT,
    source_url TEXT NOT NULL,
    license_url TEXT,
    status TEXT NOT NULL CHECK (
        status IN ('downloading', 'completed', 'failed')
    ),
    staged_path TEXT,
    stored_path TEXT,
    content_sha256 TEXT,
    content_type TEXT,
    byte_size INTEGER CHECK (byte_size IS NULL OR byte_size >= 0),
    error_text TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX idx_oa_acquisitions_candidate
ON oa_acquisitions(candidate_id, created_at);

CREATE UNIQUE INDEX idx_oa_acquisitions_completed_candidate
ON oa_acquisitions(candidate_id) WHERE status = 'completed';
