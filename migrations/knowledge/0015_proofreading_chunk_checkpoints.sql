CREATE TABLE proofreading_chunk_checkpoints (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    translation_version_id TEXT NOT NULL
        REFERENCES translation_versions(id) ON DELETE CASCADE,
    chunk_ordinal INTEGER NOT NULL CHECK (chunk_ordinal >= 0),
    request_signature TEXT NOT NULL,
    source_text_sha256 TEXT NOT NULL,
    source_text TEXT NOT NULL,
    edited_text TEXT NOT NULL,
    edit_contract_json TEXT NOT NULL,
    diff_text TEXT NOT NULL,
    reasons_json TEXT NOT NULL DEFAULT '[]',
    invariant_results_json TEXT NOT NULL DEFAULT '[]',
    result_status TEXT NOT NULL
        CHECK (result_status IN ('accepted', 'rejected', 'waiting_human')),
    confidence REAL NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (run_id, translation_version_id, chunk_ordinal, request_signature)
);

CREATE INDEX idx_proofreading_chunk_resume
ON proofreading_chunk_checkpoints(
    run_id, translation_version_id, request_signature, chunk_ordinal
);
