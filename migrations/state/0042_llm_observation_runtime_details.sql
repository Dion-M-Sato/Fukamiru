ALTER TABLE llm_invocation_observations
    ADD COLUMN invocation_kind TEXT NOT NULL DEFAULT 'codex';

ALTER TABLE llm_invocation_observations
    ADD COLUMN batch_index INTEGER;

ALTER TABLE llm_invocation_observations
    ADD COLUMN execution_status TEXT NOT NULL DEFAULT 'completed'
    CHECK (execution_status IN ('completed', 'failed'));

ALTER TABLE llm_invocation_observations
    ADD COLUMN prompt_utf8_bytes INTEGER NOT NULL DEFAULT 0
    CHECK (prompt_utf8_bytes >= 0);

ALTER TABLE llm_invocation_observations
    ADD COLUMN schema_characters INTEGER NOT NULL DEFAULT 0
    CHECK (schema_characters >= 0);

ALTER TABLE llm_invocation_observations
    ADD COLUMN output_characters INTEGER NOT NULL DEFAULT 0
    CHECK (output_characters >= 0);

ALTER TABLE llm_invocation_observations
    ADD COLUMN usage_reported INTEGER NOT NULL DEFAULT 0
    CHECK (usage_reported IN (0, 1));

ALTER TABLE llm_invocation_observations
    ADD COLUMN payload_sha256 TEXT;

ALTER TABLE llm_invocation_observations
    ADD COLUMN prompt_prefix_sha256 TEXT;

ALTER TABLE llm_invocation_observations
    ADD COLUMN latency_seconds REAL NOT NULL DEFAULT 0
    CHECK (latency_seconds >= 0);

ALTER TABLE llm_invocation_observations
    ADD COLUMN error_kind TEXT;

ALTER TABLE llm_invocation_observations
    ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS idx_llm_observations_node_stage
    ON llm_invocation_observations(node_run_id, stage, created_at);
