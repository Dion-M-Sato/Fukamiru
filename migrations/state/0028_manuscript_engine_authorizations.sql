CREATE TABLE manuscript_engine_authorizations (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    node_run_id TEXT NOT NULL REFERENCES node_runs(id) ON DELETE CASCADE,
    from_fingerprint TEXT NOT NULL CHECK (length(from_fingerprint) = 64),
    target_fingerprint TEXT NOT NULL CHECK (length(target_fingerprint) = 64),
    status TEXT NOT NULL CHECK (status IN ('pending', 'consumed', 'superseded')),
    created_at TEXT NOT NULL,
    consumed_at TEXT,
    superseded_at TEXT,
    CHECK (
        (status = 'pending' AND consumed_at IS NULL AND superseded_at IS NULL)
        OR (status = 'consumed' AND consumed_at IS NOT NULL AND superseded_at IS NULL)
        OR (status = 'superseded' AND consumed_at IS NULL AND superseded_at IS NOT NULL)
    )
);

CREATE INDEX idx_manuscript_engine_authorizations_pending
ON manuscript_engine_authorizations(run_id, node_run_id, status);
