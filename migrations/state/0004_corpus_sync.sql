CREATE TABLE corpus_sync_batches (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE REFERENCES runs(id) ON DELETE CASCADE,
    root_path TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('scanning', 'ingesting', 'indexing', 'completed', 'failed')
    ),
    discovered_count INTEGER NOT NULL DEFAULT 0 CHECK (discovered_count >= 0),
    ready_count INTEGER NOT NULL DEFAULT 0 CHECK (ready_count >= 0),
    reused_count INTEGER NOT NULL DEFAULT 0 CHECK (reused_count >= 0),
    ocr_required_count INTEGER NOT NULL DEFAULT 0 CHECK (ocr_required_count >= 0),
    failed_count INTEGER NOT NULL DEFAULT 0 CHECK (failed_count >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE corpus_sync_items (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES corpus_sync_batches(id) ON DELETE CASCADE,
    source_path TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('discovered', 'processing', 'ready', 'reused', 'ocr_required', 'failed')
    ),
    source_asset_id TEXT,
    document_unit_id TEXT,
    error_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(batch_id, source_path)
);

CREATE INDEX idx_corpus_sync_items_batch_status
    ON corpus_sync_items(batch_id, status, relative_path);
