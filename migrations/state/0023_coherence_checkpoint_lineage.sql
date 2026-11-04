ALTER TABLE manuscript_coherence_checkpoints
ADD COLUMN lineage_signature TEXT;

ALTER TABLE manuscript_coherence_checkpoints
ADD COLUMN evidence_signature TEXT;

ALTER TABLE manuscript_coherence_checkpoints
ADD COLUMN source_sha256 TEXT;

ALTER TABLE manuscript_coherence_checkpoints
ADD COLUMN evidence_round_index INTEGER NOT NULL DEFAULT 1
CHECK (evidence_round_index >= 1);

CREATE INDEX idx_manuscript_coherence_checkpoints_lineage
ON manuscript_coherence_checkpoints(
    run_id, stage, lineage_signature, round_index, updated_at
);
