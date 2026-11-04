DROP TRIGGER IF EXISTS review_queue_human_insert;
DROP TRIGGER IF EXISTS review_queue_human_update;
DROP TRIGGER IF EXISTS review_queue_human_delete;
DROP TRIGGER IF EXISTS review_queue_run_update;
DROP TRIGGER IF EXISTS review_queue_authoring_proposal_insert;
DROP TRIGGER IF EXISTS review_queue_authoring_proposal_update;
DROP VIEW IF EXISTS review_queue_human_projection_source;

CREATE VIEW review_queue_human_projection_source AS
SELECT h.id,
       'human_review' AS entry_kind,
       h.run_id,
       h.node_run_id,
       h.object_type,
       h.object_id,
       h.question,
       h.options_json,
       h.status,
       h.response_json,
       h.created_at,
       h.resolved_at,
       r.workflow_name,
       r.status AS run_status,
       COALESCE(
           CASE WHEN json_valid(r.output_json)
               THEN json_extract(r.output_json, '$.source_path') END,
           CASE WHEN json_valid(r.input_json)
               THEN json_extract(r.input_json, '$.source_path') END
       ) AS source_path,
       CASE WHEN h.object_type = 'authoring_submission' THEN p.title END AS source_title,
       COALESCE(
           CASE WHEN json_valid(r.output_json)
               THEN json_extract(r.output_json, '$.source_asset_id') END,
           CASE WHEN json_valid(r.input_json)
               THEN json_extract(r.input_json, '$.source_asset_id') END
       ) AS source_asset_id,
       CASE WHEN h.object_type = 'authoring_submission'
           THEN p.target_document_id END AS document_id,
       CASE WHEN h.object_type = 'authoring_submission'
           THEN project.name END AS project_name,
       CASE WHEN h.object_type = 'authoring_submission'
           THEN p.status END AS proposal_status,
       CASE WHEN h.object_type = 'authoring_submission'
           THEN p.quality_status END AS quality_status,
       NULL AS bulk_approval_status,
       CASE WHEN h.object_type = 'authoring_submission'
           THEN p.confirmed_revision_id END AS confirmed_revision_id,
       CASE
           WHEN h.object_type = 'authoring_submission' THEN p.artifact_path
           ELSE COALESCE(
               CASE WHEN json_valid(r.output_json)
                   THEN json_extract(r.output_json, '$.canonical_project_path') END,
               CASE WHEN json_valid(r.output_json)
                   THEN json_extract(r.output_json, '$.translation_final_path') END,
               CASE WHEN json_valid(r.output_json)
                   THEN json_extract(r.output_json, '$.approved_snapshot_path') END
           )
       END AS output_path,
       CASE
           WHEN h.object_type = 'authoring_submission' AND p.artifact_path IS NOT NULL
               THEN 'ready'
           WHEN h.object_type = 'authoring_submission' THEN 'unavailable'
           WHEN COALESCE(
               CASE WHEN json_valid(r.output_json)
                   THEN json_extract(r.output_json, '$.canonical_project_path') END,
               CASE WHEN json_valid(r.output_json)
                   THEN json_extract(r.output_json, '$.translation_final_path') END,
               CASE WHEN json_valid(r.output_json)
                   THEN json_extract(r.output_json, '$.approved_snapshot_path') END
           ) IS NOT NULL THEN 'ready'
           WHEN r.status IN ('queued', 'running', 'waiting_human') THEN 'pending'
           ELSE 'unavailable'
       END AS output_status,
       NULL AS checkpoint_sha,
       NULL AS output_error,
       0 AS validation_valid,
       0 AS applied_edit_count,
       0 AS coherence_warning_count,
       0 AS skipped_edit_count,
       0 AS unresolved_question_count,
       CASE
           WHEN h.object_type = 'authoring_submission'
                AND json_valid(p.review_context_json)
               THEN COALESCE(
                   json_extract(p.review_context_json, '$.completion_gate.blocking_count'),
                   0
               )
           ELSE 0
       END AS quality_blocking_count
FROM human_reviews h
JOIN runs r ON r.id = h.run_id
LEFT JOIN authoring_pending_proposals p
  ON p.run_id = h.run_id AND h.object_type = 'authoring_submission'
LEFT JOIN projects project ON project.id = p.project_id
WHERE h.status IN ('open', 'approved', 'rejected');

DELETE FROM review_queue_entries WHERE entry_kind = 'human_review';
INSERT INTO review_queue_entries
SELECT * FROM review_queue_human_projection_source;

CREATE TRIGGER review_queue_human_insert
AFTER INSERT ON human_reviews
BEGIN
    INSERT OR REPLACE INTO review_queue_entries
    SELECT * FROM review_queue_human_projection_source WHERE id = NEW.id;
END;

CREATE TRIGGER review_queue_human_update
AFTER UPDATE ON human_reviews
BEGIN
    DELETE FROM review_queue_entries WHERE id = OLD.id;
    INSERT OR REPLACE INTO review_queue_entries
    SELECT * FROM review_queue_human_projection_source WHERE id = NEW.id;
END;

CREATE TRIGGER review_queue_human_delete
AFTER DELETE ON human_reviews
BEGIN
    DELETE FROM review_queue_entries WHERE id = OLD.id;
END;

CREATE TRIGGER review_queue_run_update
AFTER UPDATE OF workflow_name, status, input_json, output_json ON runs
BEGIN
    INSERT OR REPLACE INTO review_queue_entries
    SELECT * FROM review_queue_human_projection_source WHERE run_id = NEW.id;
END;

CREATE TRIGGER review_queue_authoring_proposal_insert
AFTER INSERT ON authoring_pending_proposals
BEGIN
    INSERT OR REPLACE INTO review_queue_entries
    SELECT * FROM review_queue_human_projection_source WHERE run_id = NEW.run_id;
END;

CREATE TRIGGER review_queue_authoring_proposal_update
AFTER UPDATE ON authoring_pending_proposals
BEGIN
    INSERT OR REPLACE INTO review_queue_entries
    SELECT * FROM review_queue_human_projection_source WHERE run_id = NEW.run_id;
END;
