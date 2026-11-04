ALTER TABLE corpus_sync_batches ADD COLUMN embedding_build_id TEXT;
ALTER TABLE corpus_sync_batches
    ADD COLUMN embedding_completed_count INTEGER NOT NULL DEFAULT 0
    CHECK (embedding_completed_count >= 0);
ALTER TABLE corpus_sync_batches
    ADD COLUMN embedding_total_count INTEGER NOT NULL DEFAULT 0
    CHECK (embedding_total_count >= 0);
ALTER TABLE corpus_sync_batches ADD COLUMN embedding_chunks_per_second REAL;
ALTER TABLE corpus_sync_batches ADD COLUMN embedding_eta_seconds REAL;
ALTER TABLE corpus_sync_batches ADD COLUMN embedding_batch_size INTEGER;
ALTER TABLE corpus_sync_batches ADD COLUMN embedding_model TEXT;
ALTER TABLE corpus_sync_batches ADD COLUMN embedding_model_revision TEXT;
ALTER TABLE corpus_sync_batches ADD COLUMN configured_device TEXT;
ALTER TABLE corpus_sync_batches ADD COLUMN actual_embedding_device TEXT;
