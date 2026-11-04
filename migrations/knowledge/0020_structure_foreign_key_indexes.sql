-- Keep document replacement bounded to the affected document's rows.
-- SQLite checks referencing foreign keys while cascading deletes; without these
-- indexes, deleting one page or span scans the full source_spans/chunks tables.
CREATE INDEX IF NOT EXISTS idx_source_spans_end_page_fk
ON source_spans(end_page_id);

CREATE INDEX IF NOT EXISTS idx_chunks_previous_span_fk
ON chunks(previous_source_span_id);

CREATE INDEX IF NOT EXISTS idx_chunks_next_span_fk
ON chunks(next_source_span_id);

CREATE INDEX IF NOT EXISTS idx_ocr_revision_pages_source_page_fk
ON ocr_revision_pages(source_page_id);

CREATE INDEX IF NOT EXISTS idx_translation_units_source_span_fk
ON translation_units(source_span_id);

CREATE INDEX IF NOT EXISTS idx_ocr_provenance_links_source_span_fk
ON ocr_provenance_links(source_span_id);

CREATE INDEX IF NOT EXISTS idx_translation_research_triggers_document_unit_fk
ON translation_research_triggers(document_unit_id);

CREATE INDEX IF NOT EXISTS idx_translation_term_decisions_document_unit_fk
ON translation_term_decisions(document_unit_id);
