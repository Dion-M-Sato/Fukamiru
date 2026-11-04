CREATE TABLE pdf_ocr_jobs_multilingual (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    corpus_item_id TEXT REFERENCES corpus_sync_items(id) ON DELETE CASCADE,
    source_path TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK (length(source_sha256) = 64),
    source_language TEXT NOT NULL CHECK (
        source_language IN ('und', 'ja', 'en', 'fr', 'ru', 'zh', 'ko', 'de')
    ),
    engine TEXT CHECK (engine IS NULL OR engine IN ('direct', 'mistral', 'ndlocr_lite')),
    status TEXT NOT NULL CHECK (
        status IN ('needs_language', 'queued', 'processing', 'completed', 'failed', 'cancelled')
    ),
    force_ocr INTEGER NOT NULL DEFAULT 0 CHECK (force_ocr IN (0, 1)),
    output_text_path TEXT,
    output_json_path TEXT,
    source_page_count INTEGER CHECK (source_page_count IS NULL OR source_page_count >= 0),
    output_page_count INTEGER CHECK (output_page_count IS NULL OR output_page_count >= 0),
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    error_json TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL,
    language_origin TEXT NOT NULL DEFAULT 'unresolved'
        CHECK (language_origin IN ('explicit', 'detected', 'manual', 'unresolved')),
    language_confidence REAL CHECK (
        language_confidence IS NULL OR (language_confidence >= 0.0 AND language_confidence <= 1.0)
    ),
    language_decision_json TEXT,
    UNIQUE(run_id, source_sha256)
);

INSERT INTO pdf_ocr_jobs_multilingual (
    id, run_id, corpus_item_id, source_path, source_sha256, source_language,
    engine, status, force_ocr, output_text_path, output_json_path,
    source_page_count, output_page_count, attempt, error_json, created_at,
    started_at, completed_at, updated_at, language_origin, language_confidence,
    language_decision_json
)
SELECT id, run_id, corpus_item_id, source_path, source_sha256, source_language,
       engine, status, force_ocr, output_text_path, output_json_path,
       source_page_count, output_page_count, attempt, error_json, created_at,
       started_at, completed_at, updated_at, language_origin, language_confidence,
       language_decision_json
FROM pdf_ocr_jobs;

DROP TABLE pdf_ocr_jobs;
ALTER TABLE pdf_ocr_jobs_multilingual RENAME TO pdf_ocr_jobs;

CREATE INDEX idx_pdf_ocr_jobs_status
    ON pdf_ocr_jobs(status, source_language, created_at);
CREATE INDEX idx_pdf_ocr_jobs_run
    ON pdf_ocr_jobs(run_id, status);
