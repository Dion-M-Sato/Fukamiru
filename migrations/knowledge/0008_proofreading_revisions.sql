CREATE TABLE proofreading_revisions (
    id TEXT PRIMARY KEY,
    translation_version_id TEXT REFERENCES translation_versions(id) ON DELETE CASCADE,
    parent_proofreading_id TEXT REFERENCES proofreading_revisions(id) ON DELETE RESTRICT,
    model_name TEXT NOT NULL,
    model_identity TEXT NOT NULL,
    source_text TEXT NOT NULL,
    edited_text TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    edited_sha256 TEXT NOT NULL,
    edit_contract_json TEXT NOT NULL,
    diff_text TEXT NOT NULL,
    reasons_json TEXT NOT NULL DEFAULT '[]',
    invariant_results_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK (status IN ('accepted', 'rejected', 'waiting_human')),
    created_at TEXT NOT NULL
);

CREATE INDEX idx_proofreading_translation_created
ON proofreading_revisions(translation_version_id, created_at);
