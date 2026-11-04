ALTER TABLE defects ADD COLUMN source_asset_id TEXT;
ALTER TABLE defects ADD COLUMN source_span_id TEXT;
ALTER TABLE defects ADD COLUMN page_start INTEGER;
ALTER TABLE defects ADD COLUMN page_end INTEGER;
ALTER TABLE defects ADD COLUMN excerpt TEXT;
ALTER TABLE defects ADD COLUMN context_text TEXT;
ALTER TABLE defects ADD COLUMN rationale TEXT;
ALTER TABLE defects ADD COLUMN repair_candidates_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE defects ADD COLUMN confidence REAL CHECK (confidence IS NULL OR confidence BETWEEN 0.0 AND 1.0);
ALTER TABLE defects ADD COLUMN automatic_repair_allowed INTEGER NOT NULL DEFAULT 0
    CHECK (automatic_repair_allowed IN (0, 1));

CREATE INDEX idx_defects_source_pages
ON defects(source_asset_id, page_start, page_end, status);
