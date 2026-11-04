CREATE TABLE review_queue_entries (
    id TEXT PRIMARY KEY,
    entry_kind TEXT NOT NULL CHECK (
        entry_kind IN ('human_review', 'manuscript_rewrite_proposal')
    ),
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    node_run_id TEXT REFERENCES node_runs(id) ON DELETE SET NULL,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    question TEXT NOT NULL,
    options_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK (status IN ('open', 'approved', 'rejected')),
    response_json TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    workflow_name TEXT NOT NULL,
    run_status TEXT NOT NULL,
    source_path TEXT,
    source_title TEXT,
    source_asset_id TEXT,
    document_id TEXT,
    project_name TEXT,
    proposal_status TEXT,
    quality_status TEXT,
    bulk_approval_status TEXT,
    confirmed_revision_id TEXT,
    output_path TEXT,
    output_status TEXT NOT NULL,
    checkpoint_sha TEXT,
    output_error TEXT,
    validation_valid INTEGER NOT NULL DEFAULT 0 CHECK (validation_valid IN (0, 1)),
    applied_edit_count INTEGER NOT NULL DEFAULT 0,
    coherence_warning_count INTEGER NOT NULL DEFAULT 0,
    skipped_edit_count INTEGER NOT NULL DEFAULT 0,
    unresolved_question_count INTEGER NOT NULL DEFAULT 0,
    quality_blocking_count INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_review_queue_entries_status
ON review_queue_entries(status, resolved_at, created_at);

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
       NULL AS source_title,
       COALESCE(
           CASE WHEN json_valid(r.output_json)
               THEN json_extract(r.output_json, '$.source_asset_id') END,
           CASE WHEN json_valid(r.input_json)
               THEN json_extract(r.input_json, '$.source_asset_id') END
       ) AS source_asset_id,
       NULL AS document_id,
       NULL AS project_name,
       NULL AS proposal_status,
       NULL AS quality_status,
       NULL AS bulk_approval_status,
       NULL AS confirmed_revision_id,
       COALESCE(
           CASE WHEN json_valid(r.output_json)
               THEN json_extract(r.output_json, '$.canonical_project_path') END,
           CASE WHEN json_valid(r.output_json)
               THEN json_extract(r.output_json, '$.translation_final_path') END,
           CASE WHEN json_valid(r.output_json)
               THEN json_extract(r.output_json, '$.approved_snapshot_path') END
       ) AS output_path,
       CASE
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
       0 AS quality_blocking_count
FROM human_reviews h
JOIN runs r ON r.id = h.run_id
WHERE h.status IN ('open', 'approved', 'rejected');

CREATE VIEW review_queue_manuscript_projection_source AS
SELECT 'manuscript-rewrite:' || p.id AS id,
       'manuscript_rewrite_proposal' AS entry_kind,
       p.run_id,
       NULL AS node_run_id,
       'manuscript_rewrite_proposal' AS object_type,
       p.id AS object_id,
       '原稿改稿案: ' || d.title AS question,
       CASE WHEN p.status = 'pending'
           THEN '["review","confirm","discard"]' ELSE '[]' END AS options_json,
       CASE
           WHEN p.status = 'pending' THEN 'open'
           WHEN p.status = 'rejected' THEN 'rejected'
           ELSE 'approved'
       END AS status,
       NULL AS response_json,
       p.created_at,
       p.resolved_at,
       'manuscript_rewrite' AS workflow_name,
       'completed' AS run_status,
       NULL AS source_path,
       d.title AS source_title,
       NULL AS source_asset_id,
       p.document_id,
       project.name AS project_name,
       CASE
           WHEN p.status = 'pending' AND p.quality_status = 'needs_human_review'
               THEN 'needs_human_review'
           ELSE p.status
       END AS proposal_status,
       p.quality_status,
       CASE
           WHEN p.status = 'rejected' THEN NULL
           WHEN json_valid(p.context_json)
               THEN json_extract(p.context_json, '$.bulk_approval.status')
           ELSE NULL
       END AS bulk_approval_status,
       p.confirmed_revision_id,
       p.artifact_path AS output_path,
       CASE WHEN p.artifact_path IS NOT NULL THEN 'ready' ELSE 'unavailable' END
           AS output_status,
       p.checkpoint_sha,
       p.error_text AS output_error,
       CASE
           WHEN p.status = 'rejected' THEN 0
           WHEN json_valid(p.validation_json)
               THEN COALESCE(json_extract(p.validation_json, '$.valid'), 0)
           ELSE 0
       END AS validation_valid,
       CASE
           WHEN p.status = 'rejected' THEN 0
           WHEN json_valid(p.edit_summary_json)
               THEN COALESCE(json_extract(p.edit_summary_json, '$.applied_edit_count'), 0)
           ELSE 0
       END AS applied_edit_count,
       CASE
           WHEN p.status = 'rejected' THEN 0
           WHEN json_valid(p.context_json) THEN COALESCE((
               SELECT COUNT(*)
               FROM json_each(p.context_json, '$.coherence_checks[#-1].issues')
               WHERE json_extract(value, '$.severity') = 'warning'
           ), 0)
           ELSE 0
       END AS coherence_warning_count,
       CASE
           WHEN p.status = 'rejected' THEN 0
           WHEN json_valid(p.edit_summary_json) THEN COALESCE(
               json_array_length(p.edit_summary_json, '$.warnings'), 0
           )
           ELSE 0
       END AS skipped_edit_count,
       CASE
           WHEN p.status = 'rejected' THEN 0
           WHEN json_valid(p.edit_summary_json) THEN COALESCE(
               json_array_length(p.edit_summary_json, '$.unresolved_questions'), 0
           )
           ELSE 0
       END AS unresolved_question_count,
       CASE
           WHEN p.status = 'rejected' THEN 0
           WHEN json_valid(p.context_json) THEN COALESCE(
               json_extract(p.context_json, '$.completion_gate.blocking_count'), 0
           )
           ELSE 0
       END AS quality_blocking_count
FROM manuscript_rewrite_proposals p
JOIN content_documents d ON d.id = p.document_id
JOIN projects project ON project.id = p.project_id;

INSERT INTO review_queue_entries
SELECT * FROM review_queue_human_projection_source;

INSERT INTO review_queue_entries
SELECT * FROM review_queue_manuscript_projection_source;

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

CREATE TRIGGER review_queue_manuscript_insert
AFTER INSERT ON manuscript_rewrite_proposals
BEGIN
    INSERT OR REPLACE INTO review_queue_entries
    SELECT * FROM review_queue_manuscript_projection_source
    WHERE object_id = NEW.id;
END;

CREATE TRIGGER review_queue_manuscript_update
AFTER UPDATE ON manuscript_rewrite_proposals
BEGIN
    DELETE FROM review_queue_entries
    WHERE id = 'manuscript-rewrite:' || OLD.id;
    INSERT OR REPLACE INTO review_queue_entries
    SELECT * FROM review_queue_manuscript_projection_source
    WHERE object_id = NEW.id;
END;

CREATE TRIGGER review_queue_manuscript_delete
AFTER DELETE ON manuscript_rewrite_proposals
BEGIN
    DELETE FROM review_queue_entries
    WHERE id = 'manuscript-rewrite:' || OLD.id;
END;

CREATE TRIGGER review_queue_document_title_update
AFTER UPDATE OF title ON content_documents
BEGIN
    INSERT OR REPLACE INTO review_queue_entries
    SELECT * FROM review_queue_manuscript_projection_source
    WHERE document_id = NEW.id;
END;

CREATE TRIGGER review_queue_project_name_update
AFTER UPDATE OF name ON projects
BEGIN
    INSERT OR REPLACE INTO review_queue_entries
    SELECT source.*
    FROM review_queue_manuscript_projection_source source
    JOIN manuscript_rewrite_proposals p ON p.id = source.object_id
    WHERE p.project_id = NEW.id;
END;
