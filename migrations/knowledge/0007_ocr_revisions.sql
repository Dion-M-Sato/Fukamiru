CREATE TABLE ocr_text_revisions (
    id TEXT PRIMARY KEY,
    source_asset_id TEXT NOT NULL REFERENCES source_assets(id) ON DELETE CASCADE,
    parent_revision_id TEXT REFERENCES ocr_text_revisions(id) ON DELETE RESTRICT,
    layer TEXT NOT NULL CHECK (
        layer IN ('raw_ocr', 'normalized_ocr', 'human_corrected_ocr',
                  'translation_source', 'translated_text')
    ),
    content_text TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    language TEXT,
    created_by TEXT NOT NULL CHECK (created_by IN ('extractor', 'deterministic', 'human', 'agent')),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX idx_ocr_revisions_asset_layer
ON ocr_text_revisions(source_asset_id, layer, created_at);

CREATE TABLE ocr_revision_pages (
    id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL REFERENCES ocr_text_revisions(id) ON DELETE CASCADE,
    page_number INTEGER NOT NULL CHECK (page_number > 0),
    printed_label TEXT,
    source_page_id TEXT REFERENCES pages(id) ON DELETE SET NULL,
    start_char INTEGER NOT NULL CHECK (start_char >= 0),
    end_char INTEGER NOT NULL CHECK (end_char >= start_char),
    page_text TEXT NOT NULL,
    page_text_sha256 TEXT NOT NULL,
    UNIQUE (revision_id, page_number)
);

CREATE TABLE ocr_blocks (
    id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL REFERENCES ocr_text_revisions(id) ON DELETE CASCADE,
    stable_block_id TEXT NOT NULL,
    block_type TEXT NOT NULL CHECK (
        block_type IN ('title', 'heading', 'paragraph', 'quote', 'list_item',
                       'footnote', 'endnote', 'bibliography', 'other')
    ),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    start_char INTEGER NOT NULL CHECK (start_char >= 0),
    end_char INTEGER NOT NULL CHECK (end_char >= start_char),
    text TEXT NOT NULL,
    language TEXT,
    is_human_protected INTEGER NOT NULL DEFAULT 0 CHECK (is_human_protected IN (0, 1)),
    attributes_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (revision_id, stable_block_id),
    UNIQUE (revision_id, ordinal)
);

CREATE INDEX idx_ocr_blocks_revision_offsets
ON ocr_blocks(revision_id, start_char, end_char);

CREATE TABLE ocr_page_boundaries (
    id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL REFERENCES ocr_text_revisions(id) ON DELETE CASCADE,
    revision_page_id TEXT NOT NULL REFERENCES ocr_revision_pages(id) ON DELETE CASCADE,
    stable_marker_id TEXT NOT NULL,
    page_number INTEGER NOT NULL CHECK (page_number > 0),
    char_offset INTEGER NOT NULL CHECK (char_offset >= 0),
    block_id TEXT REFERENCES ocr_blocks(id) ON DELETE SET NULL,
    offset_in_block INTEGER CHECK (offset_in_block IS NULL OR offset_in_block >= 0),
    UNIQUE (revision_id, page_number),
    UNIQUE (revision_id, stable_marker_id)
);

CREATE INDEX idx_ocr_boundaries_revision_offset
ON ocr_page_boundaries(revision_id, char_offset);

CREATE TABLE ocr_notes (
    id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL REFERENCES ocr_text_revisions(id) ON DELETE CASCADE,
    stable_note_id TEXT NOT NULL,
    note_kind TEXT NOT NULL CHECK (note_kind IN ('source_footnote', 'source_endnote', 'translator_note')),
    scope_id TEXT NOT NULL,
    display_label TEXT NOT NULL,
    body_block_id TEXT REFERENCES ocr_blocks(id) ON DELETE SET NULL,
    status TEXT NOT NULL CHECK (status IN ('resolved', 'ambiguous', 'orphan')),
    source_page_number INTEGER CHECK (source_page_number IS NULL OR source_page_number > 0),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (revision_id, stable_note_id)
);

CREATE TABLE ocr_note_references (
    id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL REFERENCES ocr_text_revisions(id) ON DELETE CASCADE,
    stable_reference_id TEXT NOT NULL,
    note_id TEXT REFERENCES ocr_notes(id) ON DELETE SET NULL,
    block_id TEXT REFERENCES ocr_blocks(id) ON DELETE SET NULL,
    offset_in_block INTEGER NOT NULL CHECK (offset_in_block >= 0),
    display_label TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('resolved', 'ambiguous', 'orphan')),
    UNIQUE (revision_id, stable_reference_id)
);

CREATE TABLE ocr_provenance_links (
    id TEXT PRIMARY KEY,
    descendant_revision_id TEXT NOT NULL REFERENCES ocr_text_revisions(id) ON DELETE CASCADE,
    descendant_block_id TEXT REFERENCES ocr_blocks(id) ON DELETE CASCADE,
    source_revision_id TEXT NOT NULL REFERENCES ocr_text_revisions(id) ON DELETE RESTRICT,
    source_revision_page_id TEXT REFERENCES ocr_revision_pages(id) ON DELETE SET NULL,
    source_span_id TEXT REFERENCES source_spans(id) ON DELETE SET NULL,
    descendant_start_char INTEGER NOT NULL CHECK (descendant_start_char >= 0),
    descendant_end_char INTEGER NOT NULL CHECK (descendant_end_char >= descendant_start_char),
    source_start_char INTEGER NOT NULL CHECK (source_start_char >= 0),
    source_end_char INTEGER NOT NULL CHECK (source_end_char >= source_start_char),
    relation_type TEXT NOT NULL CHECK (
        relation_type IN ('normalized_from', 'human_corrected_from',
                          'translation_source_from', 'translated_from')
    ),
    confidence REAL NOT NULL CHECK (confidence BETWEEN 0.0 AND 1.0),
    created_at TEXT NOT NULL
);

CREATE INDEX idx_ocr_provenance_descendant
ON ocr_provenance_links(descendant_revision_id, descendant_block_id);

CREATE INDEX idx_ocr_provenance_source
ON ocr_provenance_links(source_revision_id, source_revision_page_id, source_span_id);
