CREATE TABLE manuscript_generation_attempts (
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    target_id TEXT NOT NULL,
    signature TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'started',
    diagnostic TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    PRIMARY KEY (run_id, target_id, signature, attempt)
);

CREATE TABLE manuscript_readonly_audits (
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    phase TEXT NOT NULL,
    signature TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, phase, signature)
);
