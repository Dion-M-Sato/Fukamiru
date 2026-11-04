CREATE TABLE dissertation_evaluation_runs (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    dissertation_document_id TEXT NOT NULL REFERENCES content_documents(id) ON DELETE CASCADE,
    evaluator_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    summary_json TEXT NOT NULL DEFAULT '{}',
    started_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX idx_dissertation_evaluations_document
ON dissertation_evaluation_runs(dissertation_document_id, started_at);

CREATE TABLE dissertation_evaluation_defects (
    dissertation_evaluation_id TEXT NOT NULL
        REFERENCES dissertation_evaluation_runs(id) ON DELETE CASCADE,
    defect_id TEXT NOT NULL REFERENCES defects(id) ON DELETE CASCADE,
    evaluator_name TEXT NOT NULL,
    PRIMARY KEY (dissertation_evaluation_id, defect_id)
);
