ALTER TABLE embedding_builds ADD COLUMN sampled_text_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE embedding_builds ADD COLUMN sampled_token_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE embedding_builds ADD COLUMN average_tokens_per_chunk REAL;
ALTER TABLE embedding_builds ADD COLUMN tokens_per_second REAL;
ALTER TABLE embedding_builds ADD COLUMN tokenization_seconds REAL NOT NULL DEFAULT 0;
ALTER TABLE embedding_builds ADD COLUMN gpu_inference_seconds REAL NOT NULL DEFAULT 0;
ALTER TABLE embedding_builds ADD COLUMN db_write_seconds REAL NOT NULL DEFAULT 0;
ALTER TABLE embedding_builds ADD COLUMN measured_batch_seconds REAL NOT NULL DEFAULT 0;
ALTER TABLE embedding_builds ADD COLUMN resource_sample_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE embedding_builds ADD COLUMN gpu_utilization_avg REAL;
ALTER TABLE embedding_builds ADD COLUMN gpu_utilization_max REAL;
ALTER TABLE embedding_builds ADD COLUMN vram_used_avg_mib REAL;
ALTER TABLE embedding_builds ADD COLUMN vram_used_max_mib REAL;
ALTER TABLE embedding_builds ADD COLUMN vram_total_mib REAL;
ALTER TABLE embedding_builds ADD COLUMN cpu_utilization_avg REAL;
ALTER TABLE embedding_builds ADD COLUMN cpu_utilization_max REAL;

CREATE TABLE embedding_batch_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    build_id TEXT NOT NULL REFERENCES embedding_builds(id) ON DELETE CASCADE,
    batch_ordinal INTEGER NOT NULL,
    processed_count INTEGER NOT NULL,
    chunk_count INTEGER NOT NULL,
    encoded_chunk_count INTEGER NOT NULL,
    cache_hit_count INTEGER NOT NULL,
    token_count INTEGER NOT NULL,
    average_tokens_per_chunk REAL,
    batch_size INTEGER NOT NULL,
    batch_seconds REAL NOT NULL,
    tokenization_seconds REAL NOT NULL,
    gpu_inference_seconds REAL NOT NULL,
    db_write_seconds REAL NOT NULL,
    chunks_per_second REAL,
    tokens_per_second REAL,
    created_at TEXT NOT NULL,
    UNIQUE(build_id, batch_ordinal)
);

CREATE INDEX idx_embedding_batch_metrics_build
    ON embedding_batch_metrics(build_id, batch_ordinal DESC);

CREATE TABLE embedding_resource_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    build_id TEXT NOT NULL REFERENCES embedding_builds(id) ON DELETE CASCADE,
    sampled_at TEXT NOT NULL,
    gpu_utilization_percent REAL,
    vram_used_mib REAL,
    vram_total_mib REAL,
    cpu_utilization_percent REAL
);

CREATE INDEX idx_embedding_resource_samples_build
    ON embedding_resource_samples(build_id, sampled_at DESC);
