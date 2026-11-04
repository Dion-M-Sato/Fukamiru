ALTER TABLE manuscript_rewrite_proposals
ADD COLUMN quality_status TEXT NOT NULL DEFAULT 'ready'
CHECK (quality_status IN ('ready', 'needs_human_review'));

CREATE INDEX idx_manuscript_rewrite_proposals_quality
ON manuscript_rewrite_proposals(status, quality_status, created_at);
