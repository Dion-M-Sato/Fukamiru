CREATE TABLE authoring_pending_proposals (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE REFERENCES runs(id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    source_document_id TEXT NOT NULL REFERENCES content_documents(id) ON DELETE RESTRICT,
    source_revision_id TEXT NOT NULL REFERENCES revisions(id) ON DELETE RESTRICT,
    target_document_id TEXT NOT NULL REFERENCES content_documents(id) ON DELETE RESTRICT,
    plan_id TEXT NOT NULL,
    plan_revision INTEGER NOT NULL CHECK (plan_revision > 0),
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    proposed_html TEXT NOT NULL CHECK (length(trim(proposed_html)) > 0),
    proposed_sha256 TEXT NOT NULL CHECK (length(proposed_sha256) = 64),
    status TEXT NOT NULL CHECK (status IN ('pending', 'confirmed', 'rejected')),
    confirmed_revision_id TEXT REFERENCES revisions(id) ON DELETE RESTRICT,
    artifact_path TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    FOREIGN KEY (plan_id, plan_revision)
        REFERENCES authoring_plan_revisions(plan_id, revision) ON DELETE RESTRICT,
    CHECK (
        (status = 'confirmed' AND confirmed_revision_id IS NOT NULL AND resolved_at IS NOT NULL)
        OR (status <> 'confirmed' AND confirmed_revision_id IS NULL)
    )
);

CREATE INDEX idx_authoring_pending_proposals_project_status
    ON authoring_pending_proposals(project_id, status, created_at);
CREATE INDEX idx_authoring_pending_proposals_target
    ON authoring_pending_proposals(target_document_id, status);

CREATE TRIGGER authoring_pending_proposal_content_immutable
BEFORE UPDATE ON authoring_pending_proposals
WHEN NEW.run_id <> OLD.run_id
  OR NEW.project_id <> OLD.project_id
  OR NEW.source_document_id <> OLD.source_document_id
  OR NEW.source_revision_id <> OLD.source_revision_id
  OR NEW.target_document_id <> OLD.target_document_id
  OR NEW.plan_id <> OLD.plan_id
  OR NEW.plan_revision <> OLD.plan_revision
  OR NEW.title <> OLD.title
  OR NEW.proposed_html <> OLD.proposed_html
  OR NEW.proposed_sha256 <> OLD.proposed_sha256
  OR NEW.artifact_path IS NOT OLD.artifact_path
  OR NEW.created_at <> OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'authoring proposal content is immutable');
END;
