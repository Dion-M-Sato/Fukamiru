-- A canonical Chunk can anchor multiple exact, non-overlapping search ranges.
-- Preserve existing versions and source identities while removing that v1 limit.
CREATE TABLE retrieval_index_groups_v2 (
    index_version_id TEXT NOT NULL REFERENCES vector_index_versions(id) ON DELETE CASCADE,
    vector_key INTEGER NOT NULL CHECK (vector_key >= 0),
    chunk_id TEXT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    document_unit_id TEXT NOT NULL REFERENCES document_units(id) ON DELETE CASCADE,
    start_source_span_id TEXT NOT NULL REFERENCES source_spans(id) ON DELETE CASCADE,
    end_source_span_id TEXT NOT NULL REFERENCES source_spans(id) ON DELETE CASCADE,
    source_span_ids_json TEXT NOT NULL,
    text TEXT NOT NULL,
    token_count INTEGER NOT NULL CHECK (token_count > 0),
    source_ranges_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (index_version_id, vector_key)
);
INSERT INTO retrieval_index_groups_v2 (
    index_version_id, vector_key, chunk_id, document_unit_id,
    start_source_span_id, end_source_span_id, source_span_ids_json, text, token_count
)
SELECT index_version_id, vector_key, chunk_id, document_unit_id,
       start_source_span_id, end_source_span_id, source_span_ids_json, text, token_count
FROM retrieval_index_groups;
DROP TABLE retrieval_index_groups;
ALTER TABLE retrieval_index_groups_v2 RENAME TO retrieval_index_groups;
CREATE INDEX idx_retrieval_index_groups_chunk
ON retrieval_index_groups(index_version_id, chunk_id);
