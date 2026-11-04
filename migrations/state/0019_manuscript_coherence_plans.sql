CREATE TABLE manuscript_structure_plans (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE REFERENCES runs(id) ON DELETE CASCADE,
    review_id TEXT NOT NULL REFERENCES manuscript_reasoning_reviews(id) ON DELETE CASCADE,
    document_id TEXT NOT NULL REFERENCES content_documents(id) ON DELETE CASCADE,
    plan_json TEXT NOT NULL,
    validation_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('valid', 'invalid')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_manuscript_structure_plans_document
ON manuscript_structure_plans(document_id, created_at);

CREATE TABLE manuscript_coherence_checks (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    node_run_id TEXT REFERENCES node_runs(id) ON DELETE SET NULL,
    stage TEXT NOT NULL CHECK (stage IN ('pre_polish', 'post_polish')),
    pass_index INTEGER NOT NULL CHECK (pass_index >= 1),
    report_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('passed', 'failed')),
    created_at TEXT NOT NULL,
    UNIQUE(run_id, stage, pass_index)
);

CREATE INDEX idx_manuscript_coherence_checks_run
ON manuscript_coherence_checks(run_id, stage, pass_index);
