CREATE TABLE pdf_layout_documents (
    id TEXT PRIMARY KEY,
    source_asset_id TEXT NOT NULL REFERENCES source_assets(id) ON DELETE CASCADE,
    analyzer_version TEXT NOT NULL,
    page_count INTEGER NOT NULL CHECK (page_count > 0),
    structured_html_path TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (source_asset_id, analyzer_version)
);

CREATE TABLE pdf_layout_pages (
    id TEXT PRIMARY KEY,
    layout_document_id TEXT NOT NULL REFERENCES pdf_layout_documents(id) ON DELETE CASCADE,
    page_number INTEGER NOT NULL CHECK (page_number > 0),
    width REAL NOT NULL CHECK (width > 0),
    height REAL NOT NULL CHECK (height > 0),
    rotation INTEGER NOT NULL DEFAULT 0,
    printed_label TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (layout_document_id, page_number)
);

CREATE TABLE pdf_layout_blocks (
    id TEXT PRIMARY KEY,
    layout_page_id TEXT NOT NULL REFERENCES pdf_layout_pages(id) ON DELETE CASCADE,
    stable_block_id TEXT NOT NULL,
    structural_role TEXT NOT NULL CHECK (
        structural_role IN (
            'title', 'heading', 'toc_entry', 'paragraph', 'quote', 'list_item',
            'footnote', 'endnote', 'page_header', 'page_footer', 'page_number', 'other'
        )
    ),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    x0 REAL NOT NULL,
    y0 REAL NOT NULL,
    x1 REAL NOT NULL,
    y1 REAL NOT NULL,
    font_size REAL NOT NULL CHECK (font_size >= 0),
    font_name TEXT,
    text TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence BETWEEN 0.0 AND 1.0),
    translatable INTEGER NOT NULL DEFAULT 1 CHECK (translatable IN (0, 1)),
    attributes_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE (layout_page_id, ordinal),
    UNIQUE (stable_block_id),
    CHECK (x1 >= x0),
    CHECK (y1 >= y0)
);

CREATE INDEX idx_pdf_layout_documents_source
    ON pdf_layout_documents(source_asset_id, created_at);
CREATE INDEX idx_pdf_layout_blocks_page_role
    ON pdf_layout_blocks(layout_page_id, structural_role, ordinal);

ALTER TABLE source_spans ADD COLUMN structural_role TEXT;
ALTER TABLE source_spans ADD COLUMN layout_block_id TEXT;

