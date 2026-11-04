CREATE TABLE literature_searches (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    query TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    requested_limit INTEGER NOT NULL CHECK (requested_limit BETWEEN 1 AND 100),
    error_text TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE literature_candidates (
    id TEXT PRIMARY KEY,
    search_id TEXT NOT NULL REFERENCES literature_searches(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    provider_record_id TEXT NOT NULL,
    title TEXT NOT NULL,
    authors_json TEXT NOT NULL DEFAULT '[]',
    publication_title TEXT,
    publisher TEXT,
    publication_date TEXT,
    record_type TEXT NOT NULL,
    doi TEXT,
    url TEXT,
    oa_url TEXT,
    oa_status TEXT,
    license_url TEXT,
    provider_payload_json TEXT NOT NULL,
    review_status TEXT NOT NULL CHECK (
        review_status IN ('pending', 'approved', 'rejected', 'acquired')
    ),
    bibliographic_record_id TEXT REFERENCES bibliographic_records(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    UNIQUE (search_id, provider, provider_record_id)
);

CREATE INDEX idx_literature_candidates_review
ON literature_candidates(review_status, created_at);

CREATE TABLE bibliographic_provenance (
    bibliographic_record_id TEXT NOT NULL REFERENCES bibliographic_records(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    provider_record_id TEXT NOT NULL,
    source_url TEXT,
    license_url TEXT,
    retrieved_at TEXT NOT NULL,
    provider_payload_json TEXT NOT NULL,
    PRIMARY KEY (bibliographic_record_id, provider, provider_record_id)
);
