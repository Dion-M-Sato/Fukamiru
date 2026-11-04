ALTER TABLE projects ADD COLUMN archived_at TEXT;

CREATE INDEX idx_projects_archived_updated
ON projects(archived_at, updated_at);
