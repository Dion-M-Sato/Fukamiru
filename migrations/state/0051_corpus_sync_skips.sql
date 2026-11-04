-- Keep excluded Item rows and their original failure records for audit.
ALTER TABLE corpus_sync_items ADD COLUMN skip_reason TEXT;
ALTER TABLE corpus_sync_items ADD COLUMN skipped_at TEXT;
