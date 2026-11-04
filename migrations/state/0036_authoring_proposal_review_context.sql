ALTER TABLE authoring_pending_proposals
    ADD COLUMN quality_status TEXT NOT NULL DEFAULT 'needs_human_review'
    CHECK (quality_status IN ('ready', 'needs_human_review'));

ALTER TABLE authoring_pending_proposals
    ADD COLUMN review_context_json TEXT NOT NULL DEFAULT '{}';

DROP TRIGGER authoring_pending_proposal_content_immutable;

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
  OR NEW.quality_status <> OLD.quality_status
  OR NEW.review_context_json <> OLD.review_context_json
  OR NEW.artifact_path IS NOT OLD.artifact_path
  OR NEW.created_at <> OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'authoring proposal content is immutable');
END;
