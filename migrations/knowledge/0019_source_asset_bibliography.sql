CREATE TABLE source_asset_bibliography (
    source_asset_id TEXT PRIMARY KEY REFERENCES source_assets(id) ON DELETE CASCADE,
    bibliographic_record_id TEXT NOT NULL REFERENCES bibliographic_records(id),
    metadata_source TEXT NOT NULL,
    metadata_status TEXT NOT NULL CHECK (metadata_status IN ('complete', 'partial')),
    source_fields_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_source_asset_bibliography_record
ON source_asset_bibliography(bibliographic_record_id);
