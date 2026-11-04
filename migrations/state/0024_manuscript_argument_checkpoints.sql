CREATE TABLE manuscript_argument_checkpoints (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    node_run_id TEXT REFERENCES node_runs(id) ON DELETE SET NULL,
    process_name TEXT NOT NULL CHECK (
        process_name IN (
            'block_roles',
            'reference_classification',
            'claim_evidence_links',
            'integration_validation'
        )
    ),
    target_signature TEXT NOT NULL,
    output_json TEXT NOT NULL,
    validation_json TEXT NOT NULL,
    target_count INTEGER NOT NULL CHECK (target_count >= 0),
    completed_count INTEGER NOT NULL CHECK (completed_count >= 0),
    reused_count INTEGER NOT NULL CHECK (reused_count >= 0),
    unresolved_count INTEGER NOT NULL CHECK (unresolved_count >= 0),
    status TEXT NOT NULL CHECK (status IN ('completed', 'invalid')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(run_id, process_name, target_signature)
);

CREATE INDEX idx_manuscript_argument_checkpoints_run
ON manuscript_argument_checkpoints(run_id, process_name, updated_at);
