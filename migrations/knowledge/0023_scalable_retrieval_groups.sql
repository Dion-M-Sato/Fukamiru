-- Search-only groups retain their original Chunk and SourceSpan provenance.
-- The full-precision vectors live beside the FAISS snapshot on the configured
-- vector drive, rather than in knowledge.db.
CREATE TABLE retrieval_index_groups (
    index_version_id TEXT NOT NULL REFERENCES vector_index_versions(id) ON DELETE CASCADE,
    vector_key INTEGER NOT NULL CHECK (vector_key >= 0),
    chunk_id TEXT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    document_unit_id TEXT NOT NULL REFERENCES document_units(id) ON DELETE CASCADE,
    start_source_span_id TEXT NOT NULL REFERENCES source_spans(id) ON DELETE CASCADE,
    end_source_span_id TEXT NOT NULL REFERENCES source_spans(id) ON DELETE CASCADE,
    source_span_ids_json TEXT NOT NULL,
    text TEXT NOT NULL,
    token_count INTEGER NOT NULL CHECK (token_count > 0),
    PRIMARY KEY (index_version_id, vector_key),
    UNIQUE (index_version_id, chunk_id)
);

CREATE INDEX idx_retrieval_index_groups_chunk
ON retrieval_index_groups(index_version_id, chunk_id);
