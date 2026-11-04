ALTER TABLE authoring_plan_revisions
    ADD COLUMN fixed_asset_hash TEXT;

ALTER TABLE section_contracts
    ADD COLUMN section_output_json TEXT NOT NULL DEFAULT '{}';

ALTER TABLE audit_findings
    ADD COLUMN audit_version TEXT;
ALTER TABLE audit_findings
    ADD COLUMN manuscript_hash TEXT;
ALTER TABLE audit_findings
    ADD COLUMN target_ids_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE audit_findings
    ADD COLUMN source_ids_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE audit_findings
    ADD COLUMN disposition TEXT CHECK (
        disposition IS NULL OR disposition IN ('auto_repair','human_review','blocking')
    );
ALTER TABLE audit_findings
    ADD COLUMN repair_operations_json TEXT NOT NULL DEFAULT '[]';

CREATE TABLE authoring_fixed_asset_snapshots (
    run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    schema_version TEXT NOT NULL,
    asset_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK (schema_version = '31.1'),
    CHECK (length(asset_hash) = 64)
);

CREATE TABLE authoring_evidence_uses (
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    evidence_use_id TEXT NOT NULL,
    section_id TEXT NOT NULL,
    sentence_id TEXT NOT NULL,
    claim_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, evidence_use_id)
);
CREATE INDEX idx_authoring_evidence_uses_sentence
    ON authoring_evidence_uses(run_id, sentence_id);

CREATE TABLE authoring_review_items (
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    review_item_id TEXT NOT NULL,
    section_id TEXT NOT NULL,
    sentence_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('open','resolved','dismissed')),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, review_item_id)
);
CREATE INDEX idx_authoring_review_items_status
    ON authoring_review_items(run_id, status);
