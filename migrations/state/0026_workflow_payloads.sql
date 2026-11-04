CREATE TABLE workflow_payloads (
    sha256 TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE manuscript_unit_checkpoints (
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    phase TEXT NOT NULL,
    signature TEXT NOT NULL,
    unit_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (run_id, phase, signature, unit_id)
);
