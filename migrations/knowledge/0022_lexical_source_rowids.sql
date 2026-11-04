-- FTS5 UNINDEXED metadata cannot support a source-specific lookup. Mirror only
-- its rowids and source ids in a normal indexed table, backfilled once here.
-- Do not cascade from chunks: old FTS rows must remain addressable after
-- document structure replacement, until the lexical index is replaced atomically.
CREATE TABLE chunk_fts_sources (
    fts_rowid INTEGER PRIMARY KEY,
    source_asset_id TEXT NOT NULL
);

CREATE INDEX idx_chunk_fts_sources_asset ON chunk_fts_sources(source_asset_id);

INSERT INTO chunk_fts_sources(fts_rowid, source_asset_id)
SELECT rowid, source_asset_id FROM chunk_fts;
