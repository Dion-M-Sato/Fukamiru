CREATE TABLE manuscript_coherence_checkpoints (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    node_run_id TEXT REFERENCES node_runs(id) ON DELETE SET NULL,
    stage TEXT NOT NULL CHECK (stage IN ('pre_polish', 'post_polish')),
    round_index INTEGER NOT NULL CHECK (round_index >= 1),
    context_signature TEXT NOT NULL,
    candidate_sha256 TEXT NOT NULL,
    candidate_html TEXT NOT NULL,
    report_json TEXT NOT NULL DEFAULT '{}',
    reports_json TEXT NOT NULL DEFAULT '[]',
    repair_history_json TEXT NOT NULL DEFAULT '[]',
    applied_edits_json TEXT NOT NULL DEFAULT '[]',
    warnings_json TEXT NOT NULL DEFAULT '[]',
    restored_block_ids_json TEXT NOT NULL DEFAULT '[]',
    resolution_routes_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK (
        status IN ('progress', 'passed', 'stalled', 'escalated')
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(node_run_id, round_index)
);

CREATE INDEX idx_manuscript_coherence_checkpoints_resume
ON manuscript_coherence_checkpoints(
    run_id, stage, context_signature, updated_at
);
