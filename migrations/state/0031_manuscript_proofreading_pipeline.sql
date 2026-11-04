-- V29 production Japanese proofreading. Existing migrations remain immutable.
CREATE TABLE manuscript_proofreading_sentences (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    document_id TEXT NOT NULL REFERENCES content_documents(id) ON DELETE CASCADE,
    source_sentence_id TEXT NOT NULL,
    block_id TEXT NOT NULL,
    sentence_ordinal INTEGER NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    original_text TEXT NOT NULL,
    original_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'detected',
    error_text TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, block_id, sentence_ordinal),
    UNIQUE(run_id, source_sentence_id)
);

CREATE INDEX idx_manuscript_proofreading_sentences_run
ON manuscript_proofreading_sentences(run_id, block_id, sentence_ordinal);

CREATE TABLE manuscript_proofreading_patches (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    sentence_id TEXT NOT NULL REFERENCES manuscript_proofreading_sentences(id) ON DELETE CASCADE,
    candidate_ordinal INTEGER NOT NULL,
    original_text TEXT NOT NULL,
    raw_candidate_text TEXT NOT NULL,
    normalized_candidate_text TEXT NOT NULL,
    original_hash TEXT NOT NULL,
    candidate_hash TEXT NOT NULL,
    generation_seed INTEGER NOT NULL,
    generation_model TEXT NOT NULL,
    generation_prompt_version TEXT NOT NULL,
    frequency INTEGER NOT NULL DEFAULT 1,
    generation_seeds_json TEXT NOT NULL DEFAULT '[]',
    focus TEXT NOT NULL DEFAULT '',
    raw_changed_spans_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'generated',
    status_reasons_json TEXT NOT NULL DEFAULT '[]',
    numeric_result_json TEXT NOT NULL DEFAULT '{}',
    attribution_result_json TEXT NOT NULL DEFAULT '{}',
    bibliographic_title_result_json TEXT NOT NULL DEFAULT '{}',
    lint_result_json TEXT NOT NULL DEFAULT '{}',
    luna_result_json TEXT NOT NULL DEFAULT '{}',
    selected INTEGER NOT NULL DEFAULT 0,
    apply_allowed INTEGER NOT NULL DEFAULT 0,
    applied INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(sentence_id, candidate_ordinal),
    UNIQUE(sentence_id, normalized_candidate_text)
);

CREATE INDEX idx_manuscript_proofreading_patches_run
ON manuscript_proofreading_patches(run_id, status, sentence_id);

CREATE TABLE manuscript_proofreading_title_registry (
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    confidence TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(run_id, title)
);

CREATE TABLE manuscript_luna_cache (
    cache_key TEXT PRIMARY KEY,
    model_id TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    original_hash TEXT NOT NULL,
    candidate_hash TEXT NOT NULL,
    raw_changed_spans_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE manuscript_luna_attempts (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    node_run_id TEXT REFERENCES node_runs(id) ON DELETE SET NULL,
    batch_index INTEGER NOT NULL,
    attempt_index INTEGER NOT NULL,
    input_ids_json TEXT NOT NULL,
    status TEXT NOT NULL,
    error_text TEXT,
    latency_seconds REAL NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, batch_index, attempt_index)
);

CREATE TABLE manuscript_proofreading_logs (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    patch_id TEXT REFERENCES manuscript_proofreading_patches(id) ON DELETE CASCADE,
    sentence_id TEXT REFERENCES manuscript_proofreading_sentences(id) ON DELETE CASCADE,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    version TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX idx_manuscript_proofreading_logs_run
ON manuscript_proofreading_logs(run_id, stage, created_at);
