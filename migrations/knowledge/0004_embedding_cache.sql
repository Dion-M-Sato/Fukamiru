ALTER TABLE embedding_models
    ADD COLUMN model_revision TEXT NOT NULL DEFAULT 'legacy';

CREATE TABLE embedding_cache (
    content_sha256 TEXT NOT NULL,
    embedding_model_id TEXT NOT NULL REFERENCES embedding_models(id) ON DELETE CASCADE,
    model_revision TEXT NOT NULL,
    dimensions INTEGER NOT NULL CHECK (dimensions > 0),
    vector_blob BLOB NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT NOT NULL,
    PRIMARY KEY (content_sha256, embedding_model_id, model_revision)
);

CREATE INDEX idx_embedding_cache_model_revision
    ON embedding_cache(embedding_model_id, model_revision, content_sha256);

CREATE TABLE embedding_builds (
    id TEXT PRIMARY KEY,
    index_version_id TEXT UNIQUE
        REFERENCES vector_index_versions(id) ON DELETE CASCADE,
    corpus_signature TEXT NOT NULL,
    embedding_model_id TEXT REFERENCES embedding_models(id) ON DELETE RESTRICT,
    model_name TEXT NOT NULL,
    model_revision TEXT NOT NULL,
    configured_device TEXT NOT NULL,
    actual_device TEXT,
    status TEXT NOT NULL CHECK (status IN ('building', 'completed', 'failed')),
    total_count INTEGER NOT NULL CHECK (total_count >= 0),
    completed_count INTEGER NOT NULL DEFAULT 0 CHECK (completed_count >= 0),
    cache_hit_count INTEGER NOT NULL DEFAULT 0 CHECK (cache_hit_count >= 0),
    encoded_count INTEGER NOT NULL DEFAULT 0 CHECK (encoded_count >= 0),
    batch_size INTEGER NOT NULL CHECK (batch_size > 0),
    chunks_per_second REAL,
    eta_seconds REAL,
    error_json TEXT,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX idx_embedding_builds_resume
    ON embedding_builds(corpus_signature, model_name, model_revision, status, updated_at);
