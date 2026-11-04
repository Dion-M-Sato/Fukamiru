CREATE TABLE authoring_proposal_revalidations (
    id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    revalidation_ordinal INTEGER NOT NULL,
    title TEXT NOT NULL,
    proposed_html TEXT NOT NULL,
    proposed_sha256 TEXT NOT NULL,
    quality_status TEXT NOT NULL,
    review_context_json TEXT NOT NULL,
    artifact_path TEXT,
    archived_at TEXT NOT NULL,
    UNIQUE(proposal_id, revalidation_ordinal)
);

CREATE INDEX idx_authoring_proposal_revalidations_run
    ON authoring_proposal_revalidations(run_id, revalidation_ordinal);
