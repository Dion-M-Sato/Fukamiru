CREATE TABLE run_artifacts (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    node_run_id TEXT REFERENCES node_runs(id) ON DELETE SET NULL,
    artifact_type TEXT NOT NULL,
    path TEXT NOT NULL,
    media_type TEXT NOT NULL,
    title TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX idx_run_artifacts_run ON run_artifacts(run_id, created_at);
