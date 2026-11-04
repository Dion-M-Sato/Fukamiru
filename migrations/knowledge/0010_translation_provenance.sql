ALTER TABLE translation_units
ADD COLUMN raw_ocr_revision_id TEXT REFERENCES ocr_text_revisions(id) ON DELETE SET NULL;

ALTER TABLE translation_units
ADD COLUMN normalized_ocr_revision_id TEXT REFERENCES ocr_text_revisions(id) ON DELETE SET NULL;

ALTER TABLE translation_units
ADD COLUMN translation_source_revision_id TEXT REFERENCES ocr_text_revisions(id) ON DELETE SET NULL;

CREATE TABLE translation_batch_alignments (
    id TEXT PRIMARY KEY,
    translation_version_id TEXT NOT NULL REFERENCES translation_versions(id) ON DELETE CASCADE,
    batch_ordinal INTEGER NOT NULL CHECK (batch_ordinal >= 0),
    target_start_char INTEGER NOT NULL CHECK (target_start_char >= 0),
    target_end_char INTEGER NOT NULL CHECK (target_end_char >= target_start_char),
    source_span_ids_json TEXT NOT NULL DEFAULT '[]',
    source_page_start INTEGER CHECK (source_page_start IS NULL OR source_page_start > 0),
    source_page_end INTEGER CHECK (source_page_end IS NULL OR source_page_end > 0),
    raw_ocr_revision_id TEXT REFERENCES ocr_text_revisions(id) ON DELETE SET NULL,
    normalized_ocr_revision_id TEXT REFERENCES ocr_text_revisions(id) ON DELETE SET NULL,
    translation_source_revision_id TEXT REFERENCES ocr_text_revisions(id) ON DELETE SET NULL,
    alignment_status TEXT NOT NULL CHECK (
        alignment_status IN ('exact_batch', 'human_confirmed', 'ambiguous')
    ),
    created_at TEXT NOT NULL,
    UNIQUE (translation_version_id, batch_ordinal)
);

CREATE INDEX idx_translation_alignment_source_span
ON translation_batch_alignments(translation_version_id, source_page_start, source_page_end);
