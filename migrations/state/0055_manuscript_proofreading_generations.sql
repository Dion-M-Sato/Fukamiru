-- Preserve every prior sentence, candidate, title and log while adding manuscript generations.

CREATE TABLE manuscript_proofreading_generations (
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    generation_id TEXT NOT NULL,
    source_html_sha256 TEXT NOT NULL,
    producer_node_run_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(run_id, generation_id)
);

ALTER TABLE manuscript_proofreading_logs RENAME TO manuscript_proofreading_logs_before_generations;

ALTER TABLE manuscript_proofreading_patches RENAME TO manuscript_proofreading_patches_before_generations;

ALTER TABLE manuscript_proofreading_sentences RENAME TO manuscript_proofreading_sentences_before_generations;

ALTER TABLE manuscript_proofreading_title_registry RENAME TO manuscript_proofreading_title_registry_before_generations;

CREATE TABLE manuscript_proofreading_sentences (
    id TEXT PRIMARY KEY,
    generation_id TEXT NOT NULL DEFAULT 'legacy',
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
    UNIQUE(run_id, generation_id, block_id, sentence_ordinal),
    UNIQUE(run_id, generation_id, source_sentence_id)
);

CREATE TABLE manuscript_proofreading_patches (
    id TEXT PRIMARY KEY,
    generation_id TEXT NOT NULL DEFAULT 'legacy',
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

CREATE TABLE manuscript_proofreading_title_registry (
    generation_id TEXT NOT NULL DEFAULT 'legacy',
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    confidence TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(run_id, generation_id, title)
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

INSERT INTO manuscript_proofreading_sentences (id, run_id, document_id, source_sentence_id, block_id, sentence_ordinal, start_offset, end_offset, original_text, original_hash, status, error_text, created_at)
SELECT id, run_id, document_id, source_sentence_id, block_id, sentence_ordinal, start_offset, end_offset, original_text, original_hash, status, error_text, created_at FROM manuscript_proofreading_sentences_before_generations;

INSERT INTO manuscript_proofreading_patches (id, run_id, sentence_id, candidate_ordinal, original_text, raw_candidate_text, normalized_candidate_text, original_hash, candidate_hash, generation_seed, generation_model, generation_prompt_version, frequency, generation_seeds_json, focus, raw_changed_spans_json, status, status_reasons_json, numeric_result_json, attribution_result_json, bibliographic_title_result_json, lint_result_json, luna_result_json, selected, apply_allowed, applied, created_at, updated_at)
SELECT id, run_id, sentence_id, candidate_ordinal, original_text, raw_candidate_text, normalized_candidate_text, original_hash, candidate_hash, generation_seed, generation_model, generation_prompt_version, frequency, generation_seeds_json, focus, raw_changed_spans_json, status, status_reasons_json, numeric_result_json, attribution_result_json, bibliographic_title_result_json, lint_result_json, luna_result_json, selected, apply_allowed, applied, created_at, updated_at FROM manuscript_proofreading_patches_before_generations;

INSERT INTO manuscript_proofreading_title_registry (run_id, title, evidence_json, confidence, created_at)
SELECT run_id, title, evidence_json, confidence, created_at FROM manuscript_proofreading_title_registry_before_generations;

INSERT INTO manuscript_proofreading_logs (id, run_id, patch_id, sentence_id, stage, status, reason, version, payload_json, created_at)
SELECT id, run_id, patch_id, sentence_id, stage, status, reason, version, payload_json, created_at FROM manuscript_proofreading_logs_before_generations;

DROP TABLE manuscript_proofreading_logs_before_generations;

DROP TABLE manuscript_proofreading_patches_before_generations;

DROP TABLE manuscript_proofreading_sentences_before_generations;

DROP TABLE manuscript_proofreading_title_registry_before_generations;

CREATE INDEX idx_manuscript_proofreading_sentences_run
ON manuscript_proofreading_sentences(run_id, block_id, sentence_ordinal);

CREATE INDEX idx_manuscript_proofreading_patches_run
ON manuscript_proofreading_patches(run_id, status, sentence_id);

CREATE INDEX idx_manuscript_proofreading_logs_run
ON manuscript_proofreading_logs(run_id, stage, created_at);

CREATE INDEX idx_manuscript_proofreading_sentences_generation
ON manuscript_proofreading_sentences(run_id, generation_id, block_id, sentence_ordinal);

CREATE INDEX idx_manuscript_proofreading_patches_generation
ON manuscript_proofreading_patches(run_id, generation_id, status, sentence_id);
