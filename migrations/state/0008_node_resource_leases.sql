ALTER TABLE node_runs ADD COLUMN resource_lane TEXT;
ALTER TABLE node_runs ADD COLUMN lease_owner TEXT;
ALTER TABLE node_runs ADD COLUMN lease_acquired_at TEXT;
ALTER TABLE node_runs ADD COLUMN lease_heartbeat_at TEXT;
ALTER TABLE node_runs ADD COLUMN lease_expires_at TEXT;
ALTER TABLE node_runs ADD COLUMN claim_generation INTEGER NOT NULL DEFAULT 0;

UPDATE node_runs
SET resource_lane = CASE
    WHEN node_id = 'build_semantic_index' THEN 'embedding_gpu'
    WHEN node_id IN ('draft_translation', 'repair_translation', 'human_review')
        THEN 'codex_remote'
    ELSE 'ingestion_cpu'
END
WHERE resource_lane IS NULL;

CREATE INDEX idx_node_runs_resource_claim
ON node_runs(resource_lane, status, lease_expires_at);
