CREATE TABLE evaluation_runs (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    revision_id TEXT NOT NULL REFERENCES revisions(id) ON DELETE CASCADE,
    evaluator_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    max_repair_attempts INTEGER NOT NULL DEFAULT 2 CHECK (max_repair_attempts BETWEEN 0 AND 10),
    summary_json TEXT NOT NULL DEFAULT '{}',
    started_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX idx_evaluation_runs_revision
ON evaluation_runs(revision_id, started_at);

CREATE TABLE evaluation_defects (
    evaluation_run_id TEXT NOT NULL REFERENCES evaluation_runs(id) ON DELETE CASCADE,
    defect_id TEXT NOT NULL REFERENCES defects(id) ON DELETE CASCADE,
    evaluator_name TEXT NOT NULL,
    PRIMARY KEY (evaluation_run_id, defect_id)
);

CREATE TABLE repair_attempts (
    id TEXT PRIMARY KEY,
    evaluation_run_id TEXT NOT NULL REFERENCES evaluation_runs(id) ON DELETE CASCADE,
    defect_id TEXT NOT NULL REFERENCES defects(id) ON DELETE CASCADE,
    attempt INTEGER NOT NULL CHECK (attempt > 0),
    strategy TEXT NOT NULL,
    input_revision_id TEXT NOT NULL REFERENCES revisions(id) ON DELETE RESTRICT,
    output_revision_id TEXT REFERENCES revisions(id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK (
        status IN ('queued', 'running', 'completed', 'rejected', 'failed', 'waiting_human')
    ),
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE (evaluation_run_id, defect_id, attempt)
);

CREATE INDEX idx_repair_attempts_evaluation
ON repair_attempts(evaluation_run_id, defect_id, attempt);
