CREATE TABLE translation_memory_entries (
    id TEXT PRIMARY KEY,
    source_text_sha256 TEXT NOT NULL,
    source_text TEXT NOT NULL,
    source_language TEXT NOT NULL,
    target_language TEXT NOT NULL,
    target_text TEXT NOT NULL,
    translation_unit_id TEXT NOT NULL REFERENCES translation_units(id) ON DELETE CASCADE,
    translation_version_id TEXT NOT NULL REFERENCES translation_versions(id) ON DELETE CASCADE,
    created_by TEXT NOT NULL CHECK (created_by IN ('human', 'agent')),
    quality_status TEXT NOT NULL CHECK (
        quality_status IN ('human_corrected', 'approved', 'candidate', 'rejected')
    ),
    created_at TEXT NOT NULL,
    UNIQUE (translation_version_id, quality_status)
);

CREATE INDEX idx_translation_memory_lookup
    ON translation_memory_entries(
        source_text_sha256, source_language, target_language, quality_status, created_at
    );
