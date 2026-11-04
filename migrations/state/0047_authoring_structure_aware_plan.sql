ALTER TABLE authoring_plan_revisions
    ADD COLUMN architecture_json TEXT NOT NULL DEFAULT '{}';
