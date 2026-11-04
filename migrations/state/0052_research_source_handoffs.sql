-- Idempotent, independent ingestion of an OCR source judged useful while writing.
CREATE TABLE research_source_handoffs (
    source_sha256 TEXT PRIMARY KEY,
    requesting_run_id TEXT NOT NULL REFERENCES runs(id),
    ingestion_run_id TEXT NOT NULL UNIQUE REFERENCES runs(id),
    ocr_job_id TEXT NOT NULL REFERENCES pdf_ocr_jobs(id),
    source_metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
