CREATE TABLE embedding_models (
    id TEXT PRIMARY KEY,
    model_name TEXT NOT NULL UNIQUE,
    dimensions INTEGER NOT NULL CHECK (dimensions > 0),
    backend TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE chunk_embeddings (
    chunk_id TEXT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    embedding_model_id TEXT NOT NULL REFERENCES embedding_models(id) ON DELETE CASCADE,
    vector_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (chunk_id, embedding_model_id),
    UNIQUE (embedding_model_id, vector_key)
);

CREATE INDEX idx_chunk_embeddings_model ON chunk_embeddings(embedding_model_id, vector_key);

ALTER TABLE source_locations
    ADD COLUMN is_available INTEGER NOT NULL DEFAULT 1 CHECK (is_available IN (0, 1));

CREATE INDEX idx_source_locations_available ON source_locations(path, is_available);
