CREATE TABLE llm_result_checkpoints (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    node_run_id TEXT REFERENCES node_runs(id) ON DELETE SET NULL,
    node_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    canonical_stage TEXT NOT NULL,
    operation_name TEXT NOT NULL,
    request_fingerprint_sha256 TEXT NOT NULL,
    full_prompt_sha256 TEXT NOT NULL,
    cache_family_sha256 TEXT NOT NULL,
    result_json TEXT NOT NULL,
    validation_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('accepted','invalidated')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(run_id, request_fingerprint_sha256)
);
CREATE INDEX idx_llm_result_checkpoints_run_status
ON llm_result_checkpoints(run_id,status,updated_at);

CREATE TABLE llm_invocation_observations (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    node_run_id TEXT REFERENCES node_runs(id) ON DELETE SET NULL,
    node_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    operation_name TEXT NOT NULL,
    model TEXT NOT NULL,
    reasoning_effort TEXT NOT NULL,
    full_prompt_sha256 TEXT NOT NULL,
    prefix_1024_sha256 TEXT NOT NULL,
    prefix_4096_sha256 TEXT NOT NULL,
    prefix_16384_sha256 TEXT NOT NULL,
    request_fingerprint_sha256 TEXT NOT NULL,
    cache_family_sha256 TEXT NOT NULL,
    prompt_characters INTEGER NOT NULL CHECK(prompt_characters>=0),
    payload_characters INTEGER NOT NULL CHECK(payload_characters>=0),
    rag_context_characters INTEGER NOT NULL CHECK(rag_context_characters>=0),
    common_prefix_characters INTEGER NOT NULL CHECK(common_prefix_characters>=0),
    checkpoint_status TEXT NOT NULL CHECK(checkpoint_status IN ('hit','miss','saved','invalidated')),
    seed_role TEXT NOT NULL CHECK(seed_role IN ('seed','fanout','normal')),
    input_tokens_reported_count INTEGER,
    cached_input_tokens_reported_count INTEGER,
    cache_write_tokens_reported_count INTEGER,
    output_tokens_reported_count INTEGER,
    reasoning_tokens_reported_count INTEGER,
    zero_cached_input_call_count INTEGER NOT NULL CHECK(zero_cached_input_call_count IN (0,1)),
    created_at TEXT NOT NULL
);
CREATE INDEX idx_llm_observations_run_created
ON llm_invocation_observations(run_id,created_at);
