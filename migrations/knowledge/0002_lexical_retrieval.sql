CREATE VIRTUAL TABLE chunk_fts USING fts5(
    chunk_id UNINDEXED,
    document_unit_id UNINDEXED,
    source_asset_id UNINDEXED,
    text,
    tokenize = 'trigram'
);
