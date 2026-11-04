CREATE TABLE source_assets (
    id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE,
    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
    media_type TEXT NOT NULL,
    source_language TEXT,
    ingestion_status TEXT NOT NULL CHECK (
        ingestion_status IN ('registered', 'extracting', 'ready', 'failed', 'quarantined')
    ),
    managed_path TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE source_locations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_asset_id TEXT NOT NULL REFERENCES source_assets(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    is_primary INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0, 1)),
    first_seen_at TEXT NOT NULL,
    last_verified_at TEXT,
    UNIQUE (source_asset_id, path)
);

CREATE INDEX idx_source_locations_path ON source_locations(path);

CREATE TABLE document_units (
    id TEXT PRIMARY KEY,
    source_asset_id TEXT NOT NULL REFERENCES source_assets(id) ON DELETE CASCADE,
    parent_id TEXT REFERENCES document_units(id) ON DELETE CASCADE,
    unit_type TEXT NOT NULL CHECK (
        unit_type IN ('document', 'book', 'chapter', 'article', 'literary_work', 'issue', 'section')
    ),
    title TEXT,
    sequence_index INTEGER NOT NULL DEFAULT 0 CHECK (sequence_index >= 0),
    language TEXT,
    material_role TEXT NOT NULL CHECK (
        material_role IN ('primary', 'secondary', 'reference', 'user_writing', 'unknown')
    ),
    permission TEXT NOT NULL CHECK (
        permission IN ('quote', 'paraphrase_only', 'metadata_only', 'private_reference', 'unknown')
    ),
    structure_confidence REAL CHECK (structure_confidence BETWEEN 0.0 AND 1.0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_document_units_asset ON document_units(source_asset_id, sequence_index);
CREATE INDEX idx_document_units_parent ON document_units(parent_id, sequence_index);

CREATE TABLE pages (
    id TEXT PRIMARY KEY,
    document_unit_id TEXT NOT NULL REFERENCES document_units(id) ON DELETE CASCADE,
    page_number INTEGER NOT NULL CHECK (page_number > 0),
    printed_label TEXT,
    text TEXT NOT NULL DEFAULT '',
    image_path TEXT,
    ocr_confidence REAL CHECK (ocr_confidence BETWEEN 0.0 AND 1.0),
    created_at TEXT NOT NULL,
    UNIQUE (document_unit_id, page_number)
);

CREATE TABLE source_spans (
    id TEXT PRIMARY KEY,
    document_unit_id TEXT NOT NULL REFERENCES document_units(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    start_page_id TEXT REFERENCES pages(id) ON DELETE SET NULL,
    end_page_id TEXT REFERENCES pages(id) ON DELETE SET NULL,
    start_char INTEGER CHECK (start_char IS NULL OR start_char >= 0),
    end_char INTEGER CHECK (end_char IS NULL OR end_char >= 0),
    text TEXT NOT NULL,
    language TEXT,
    reconstruction_method TEXT NOT NULL CHECK (
        reconstruction_method IN ('native', 'deterministic', 'ai_inferred', 'human_corrected')
    ),
    confidence REAL CHECK (confidence BETWEEN 0.0 AND 1.0),
    created_at TEXT NOT NULL,
    UNIQUE (document_unit_id, ordinal),
    CHECK (end_char IS NULL OR start_char IS NULL OR end_char >= start_char)
);

CREATE INDEX idx_source_spans_pages ON source_spans(start_page_id, end_page_id);

CREATE TABLE chunks (
    id TEXT PRIMARY KEY,
    document_unit_id TEXT NOT NULL REFERENCES document_units(id) ON DELETE CASCADE,
    primary_source_span_id TEXT NOT NULL REFERENCES source_spans(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    text TEXT NOT NULL,
    token_count INTEGER NOT NULL CHECK (token_count > 0),
    tokenizer_name TEXT NOT NULL,
    previous_source_span_id TEXT REFERENCES source_spans(id) ON DELETE SET NULL,
    next_source_span_id TEXT REFERENCES source_spans(id) ON DELETE SET NULL,
    is_retrieval_slice INTEGER NOT NULL DEFAULT 0 CHECK (is_retrieval_slice IN (0, 1)),
    created_at TEXT NOT NULL,
    UNIQUE (document_unit_id, ordinal)
);

CREATE INDEX idx_chunks_primary_span ON chunks(primary_source_span_id);

CREATE TABLE vector_index_versions (
    id TEXT PRIMARY KEY,
    backend TEXT NOT NULL CHECK (backend IN ('faiss', 'qdrant')),
    embedding_model TEXT NOT NULL,
    dimensions INTEGER NOT NULL CHECK (dimensions > 0),
    index_path TEXT,
    status TEXT NOT NULL CHECK (status IN ('building', 'active', 'superseded', 'failed')),
    chunk_count INTEGER NOT NULL DEFAULT 0 CHECK (chunk_count >= 0),
    created_at TEXT NOT NULL,
    activated_at TEXT
);

CREATE TABLE chunk_index_records (
    index_version_id TEXT NOT NULL REFERENCES vector_index_versions(id) ON DELETE CASCADE,
    chunk_id TEXT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    vector_key TEXT NOT NULL,
    PRIMARY KEY (index_version_id, chunk_id),
    UNIQUE (index_version_id, vector_key)
);

CREATE TABLE bibliographic_records (
    id TEXT PRIMARY KEY,
    record_type TEXT NOT NULL,
    title TEXT NOT NULL,
    authors_json TEXT NOT NULL DEFAULT '[]',
    publication_title TEXT,
    publisher TEXT,
    publication_date TEXT,
    volume TEXT,
    issue TEXT,
    pages TEXT,
    doi TEXT,
    isbn TEXT,
    url TEXT,
    csl_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX idx_bibliographic_doi ON bibliographic_records(doi) WHERE doi IS NOT NULL;

CREATE TABLE document_unit_bibliography (
    document_unit_id TEXT NOT NULL REFERENCES document_units(id) ON DELETE CASCADE,
    bibliographic_record_id TEXT NOT NULL REFERENCES bibliographic_records(id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL CHECK (
        relation_type IN ('describes', 'contained_in', 'edition_of', 'translation_of')
    ),
    PRIMARY KEY (document_unit_id, bibliographic_record_id, relation_type)
);

CREATE TABLE translation_units (
    id TEXT PRIMARY KEY,
    source_span_id TEXT REFERENCES source_spans(id) ON DELETE SET NULL,
    source_text TEXT NOT NULL,
    source_text_sha256 TEXT NOT NULL,
    source_language TEXT NOT NULL,
    target_language TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_translation_units_hash_lang
    ON translation_units(source_text_sha256, source_language, target_language);

CREATE TABLE translation_versions (
    id TEXT PRIMARY KEY,
    translation_unit_id TEXT NOT NULL REFERENCES translation_units(id) ON DELETE CASCADE,
    parent_version_id TEXT REFERENCES translation_versions(id) ON DELETE RESTRICT,
    translated_text TEXT NOT NULL,
    editor_type TEXT NOT NULL CHECK (editor_type IN ('human', 'agent')),
    operation TEXT NOT NULL CHECK (
        operation IN ('generated', 'human_edit', 'agent_revision', 'terminology_update')
    ),
    is_current INTEGER NOT NULL DEFAULT 0 CHECK (is_current IN (0, 1)),
    is_approved INTEGER NOT NULL DEFAULT 0 CHECK (is_approved IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX idx_translation_current
    ON translation_versions(translation_unit_id)
    WHERE is_current = 1;

CREATE TABLE terminology_entries (
    id TEXT PRIMARY KEY,
    source_term TEXT NOT NULL,
    source_language TEXT NOT NULL,
    target_term TEXT NOT NULL,
    target_language TEXT NOT NULL,
    scope_type TEXT NOT NULL CHECK (scope_type IN ('global', 'project', 'document', 'context')),
    scope_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('candidate', 'approved', 'deprecated')),
    priority INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL CHECK (created_by IN ('human', 'agent', 'import')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_terminology_lookup
    ON terminology_entries(source_term, source_language, target_language, status, priority);

