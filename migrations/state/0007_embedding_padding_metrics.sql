ALTER TABLE corpus_sync_batches ADD COLUMN embedding_padded_token_count INTEGER;
ALTER TABLE corpus_sync_batches ADD COLUMN embedding_padding_token_count INTEGER;
ALTER TABLE corpus_sync_batches ADD COLUMN embedding_padding_ratio REAL;
