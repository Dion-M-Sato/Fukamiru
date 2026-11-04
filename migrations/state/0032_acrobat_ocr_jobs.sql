CREATE TABLE acrobat_ocr_batches (
    id TEXT PRIMARY KEY,
    language TEXT NOT NULL CHECK (language IN ('ja', 'en', 'fr')),
    status TEXT NOT NULL CHECK (
        status IN ('prepared', 'processing', 'completed', 'needs_review', 'failed', 'cancelled')
    ),
    input_dir TEXT NOT NULL,
    output_dir TEXT NOT NULL,
    manifest_path TEXT NOT NULL,
    action_name TEXT NOT NULL,
    expected_count INTEGER NOT NULL DEFAULT 0 CHECK (expected_count >= 0),
    completed_count INTEGER NOT NULL DEFAULT 0 CHECK (completed_count >= 0),
    review_count INTEGER NOT NULL DEFAULT 0 CHECK (review_count >= 0),
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE acrobat_ocr_jobs (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    corpus_item_id TEXT REFERENCES corpus_sync_items(id) ON DELETE CASCADE,
    batch_id TEXT REFERENCES acrobat_ocr_batches(id) ON DELETE SET NULL,
    source_path TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK (length(source_sha256) = 64),
    source_language TEXT NOT NULL CHECK (source_language IN ('und', 'ja', 'en', 'fr')),
    status TEXT NOT NULL CHECK (
        status IN (
            'needs_language', 'queued', 'staged', 'processing',
            'completed', 'needs_review', 'failed', 'cancelled'
        )
    ),
    staged_input_path TEXT,
    expected_output_path TEXT,
    output_path TEXT,
    output_sha256 TEXT,
    source_page_count INTEGER CHECK (source_page_count IS NULL OR source_page_count >= 0),
    output_page_count INTEGER CHECK (output_page_count IS NULL OR output_page_count >= 0),
    visible_character_count INTEGER CHECK (
        visible_character_count IS NULL OR visible_character_count >= 0
    ),
    error_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(run_id, source_sha256)
);

CREATE INDEX idx_acrobat_ocr_jobs_status
    ON acrobat_ocr_jobs(status, source_language, created_at);
CREATE INDEX idx_acrobat_ocr_jobs_batch
    ON acrobat_ocr_jobs(batch_id, status);
CREATE INDEX idx_acrobat_ocr_jobs_run
    ON acrobat_ocr_jobs(run_id, status);
