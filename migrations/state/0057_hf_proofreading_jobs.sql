-- 0056 is already deployed by the independent search-report feature.
-- Keep lifecycle and request receipts authoritative even after a worker crash.
CREATE TABLE hf_proofreading_jobs (
    session_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    instance_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    job_id TEXT,
    status TEXT NOT NULL,
    reserved_micro_usd INTEGER NOT NULL,
    timeout_seconds INTEGER NOT NULL,
    created_at REAL NOT NULL,
    heartbeat_at REAL NOT NULL,
    stopped_at REAL,
    error_code TEXT,
    backend_json TEXT
);
CREATE INDEX hf_proofreading_active ON hf_proofreading_jobs(instance_id,status);
CREATE TABLE hf_proofreading_requests (
    run_id TEXT NOT NULL REFERENCES runs(id),
    stage TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES hf_proofreading_jobs(session_id),
    status TEXT NOT NULL,
    request_json TEXT NOT NULL,
    progress_json TEXT,
    raw_response_json TEXT,
    result_json TEXT,
    error_code TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(run_id,stage,fingerprint)
);
