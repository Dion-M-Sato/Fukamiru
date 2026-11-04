CREATE TABLE translation_segment_alignments (
    id TEXT PRIMARY KEY,
    translation_version_id TEXT NOT NULL REFERENCES translation_versions(id) ON DELETE CASCADE,
    batch_alignment_id TEXT NOT NULL REFERENCES translation_batch_alignments(id) ON DELETE CASCADE,
    segment_ordinal INTEGER NOT NULL CHECK (segment_ordinal >= 0),
    source_batch_start_char INTEGER NOT NULL CHECK (source_batch_start_char >= 0),
    source_batch_end_char INTEGER NOT NULL CHECK (
        source_batch_end_char >= source_batch_start_char
    ),
    target_start_char INTEGER NOT NULL CHECK (target_start_char >= 0),
    target_end_char INTEGER NOT NULL CHECK (target_end_char >= target_start_char),
    source_text TEXT NOT NULL,
    target_text TEXT NOT NULL,
    source_span_ids_json TEXT NOT NULL DEFAULT '[]',
    source_page_start INTEGER CHECK (source_page_start IS NULL OR source_page_start > 0),
    source_page_end INTEGER CHECK (source_page_end IS NULL OR source_page_end > 0),
    alignment_status TEXT NOT NULL CHECK (
        alignment_status IN ('sentence_order', 'human_confirmed', 'ambiguous')
    ),
    created_at TEXT NOT NULL,
    UNIQUE (translation_version_id, segment_ordinal)
);

CREATE INDEX idx_translation_segment_span
ON translation_segment_alignments(translation_version_id, source_page_start, source_page_end);
