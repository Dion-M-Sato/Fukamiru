CREATE TABLE translation_batch_checkpoints (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    batch_ordinal INTEGER NOT NULL CHECK (batch_ordinal >= 0),
    request_signature TEXT NOT NULL,
    source_text_sha256 TEXT NOT NULL,
    source_span_ids_json TEXT NOT NULL DEFAULT '[]',
    translated_text TEXT NOT NULL,
    translator_identity TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('completed', 'invalidated')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (run_id, batch_ordinal, request_signature)
);

CREATE INDEX idx_translation_batch_resume
ON translation_batch_checkpoints(run_id, request_signature, status, batch_ordinal);
