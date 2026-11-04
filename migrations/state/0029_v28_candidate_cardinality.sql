DROP INDEX idx_manuscript_polish_generation_run;

CREATE TABLE manuscript_polish_generation_audits_v28 (
    target_id TEXT PRIMARY KEY REFERENCES manuscript_polish_targets(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    model_identity TEXT NOT NULL,
    expected_candidate_count INTEGER NOT NULL CHECK (
        expected_candidate_count BETWEEN 1 AND 8
    ),
    generated_candidate_count INTEGER NOT NULL CHECK (generated_candidate_count >= 1),
    issues_json TEXT NOT NULL DEFAULT '[]',
    completed_at TEXT NOT NULL
);

INSERT INTO manuscript_polish_generation_audits_v28(
    target_id, run_id, model_identity, expected_candidate_count,
    generated_candidate_count, issues_json, completed_at
)
SELECT
    target_id, run_id, model_identity, expected_candidate_count,
    generated_candidate_count, issues_json, completed_at
FROM manuscript_polish_generation_audits;

DROP TABLE manuscript_polish_generation_audits;
ALTER TABLE manuscript_polish_generation_audits_v28
RENAME TO manuscript_polish_generation_audits;

CREATE INDEX idx_manuscript_polish_generation_run
ON manuscript_polish_generation_audits(run_id, completed_at);
