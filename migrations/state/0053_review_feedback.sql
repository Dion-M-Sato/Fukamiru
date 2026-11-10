CREATE TABLE review_feedback (
    review_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    questions_json TEXT NOT NULL,
    preview_html TEXT NOT NULL DEFAULT '',
    answers_json TEXT NOT NULL DEFAULT '[]',
    overall_comment TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 0,
    submitted_revision INTEGER,
    submitted_answers_json TEXT NOT NULL DEFAULT '[]',
    submitted_comment TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    submitted_at TEXT
);
CREATE INDEX idx_review_feedback_run ON review_feedback(run_id, submitted_at);
