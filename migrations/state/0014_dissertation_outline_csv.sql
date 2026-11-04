CREATE TABLE dissertation_outline_imports (
    id TEXT PRIMARY KEY,
    dissertation_document_id TEXT NOT NULL REFERENCES content_documents(id) ON DELETE CASCADE,
    source_path TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    source_encoding TEXT NOT NULL,
    headers_json TEXT NOT NULL DEFAULT '[]',
    row_count INTEGER NOT NULL CHECK (row_count >= 0),
    plan_json TEXT NOT NULL,
    warnings_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK (status IN ('previewed', 'applied', 'rejected')),
    created_at TEXT NOT NULL,
    applied_at TEXT
);

CREATE TABLE dissertation_outline_directives (
    id TEXT PRIMARY KEY,
    import_id TEXT NOT NULL REFERENCES dissertation_outline_imports(id) ON DELETE RESTRICT,
    outline_node_id TEXT NOT NULL UNIQUE REFERENCES dissertation_nodes(id) ON DELETE CASCADE,
    source_row_number INTEGER NOT NULL CHECK (source_row_number >= 2),
    summary TEXT NOT NULL DEFAULT '',
    instructions TEXT NOT NULL DEFAULT '',
    target_characters INTEGER NOT NULL DEFAULT 0 CHECK (target_characters >= 0),
    generation_status TEXT NOT NULL DEFAULT 'planned' CHECK (
        generation_status IN ('planned', 'drafting', 'completed', 'blocked')
    ),
    source_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_dissertation_outline_imports_document
    ON dissertation_outline_imports(dissertation_document_id, created_at);
CREATE INDEX idx_dissertation_outline_directives_import
    ON dissertation_outline_directives(import_id, source_row_number);
