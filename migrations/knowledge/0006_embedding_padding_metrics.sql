ALTER TABLE embedding_builds ADD COLUMN sampled_padded_token_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE embedding_builds ADD COLUMN sampled_padding_token_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE embedding_builds ADD COLUMN padding_ratio REAL;
ALTER TABLE embedding_builds ADD COLUMN gpu_timing_sample_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE embedding_batch_metrics ADD COLUMN padded_token_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE embedding_batch_metrics ADD COLUMN padding_token_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE embedding_batch_metrics ADD COLUMN padding_ratio REAL;
ALTER TABLE embedding_batch_metrics ADD COLUMN gpu_timing_sampled INTEGER NOT NULL DEFAULT 0
    CHECK (gpu_timing_sampled IN (0, 1));
