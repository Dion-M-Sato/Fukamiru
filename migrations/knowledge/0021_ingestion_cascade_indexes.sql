-- Parent-row replacement must find referencing rows by their child FK alone.
-- The existing (index_version_id, chunk_id) primary key cannot serve chunk_id lookups.
CREATE INDEX IF NOT EXISTS idx_chunk_index_records_chunk_fk
ON chunk_index_records(chunk_id);

CREATE INDEX IF NOT EXISTS idx_ocr_text_revisions_parent_fk
ON ocr_text_revisions(parent_revision_id);

CREATE INDEX IF NOT EXISTS idx_ocr_page_boundaries_revision_page_fk
ON ocr_page_boundaries(revision_page_id);

CREATE INDEX IF NOT EXISTS idx_ocr_page_boundaries_block_fk
ON ocr_page_boundaries(block_id);

CREATE INDEX IF NOT EXISTS idx_ocr_notes_body_block_fk
ON ocr_notes(body_block_id);

CREATE INDEX IF NOT EXISTS idx_ocr_note_references_block_fk
ON ocr_note_references(block_id);

CREATE INDEX IF NOT EXISTS idx_ocr_note_references_note_fk
ON ocr_note_references(note_id);

CREATE INDEX IF NOT EXISTS idx_ocr_provenance_links_source_page_fk
ON ocr_provenance_links(source_revision_page_id);

CREATE INDEX IF NOT EXISTS idx_ocr_provenance_links_descendant_block_fk
ON ocr_provenance_links(descendant_block_id);
