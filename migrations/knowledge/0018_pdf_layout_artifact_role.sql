CREATE TABLE pdf_layout_blocks_v2 (
    id TEXT PRIMARY KEY,
    layout_page_id TEXT NOT NULL REFERENCES pdf_layout_pages(id) ON DELETE CASCADE,
    stable_block_id TEXT NOT NULL,
    structural_role TEXT NOT NULL CHECK (
        structural_role IN (
            'title', 'heading', 'toc_entry', 'paragraph', 'quote', 'list_item',
            'footnote', 'endnote', 'page_header', 'page_footer', 'page_number',
            'artifact', 'other'
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

INSERT INTO pdf_layout_blocks_v2(
    id, layout_page_id, stable_block_id, structural_role, ordinal,
    x0, y0, x1, y1, font_size, font_name, text,
    confidence, translatable, attributes_json, created_at
)
SELECT
    id, layout_page_id, stable_block_id, structural_role, ordinal,
    x0, y0, x1, y1, font_size, font_name, text,
    confidence, translatable, attributes_json, created_at
FROM pdf_layout_blocks;

DROP TABLE pdf_layout_blocks;
ALTER TABLE pdf_layout_blocks_v2 RENAME TO pdf_layout_blocks;

CREATE INDEX idx_pdf_layout_blocks_page_role
    ON pdf_layout_blocks(layout_page_id, structural_role, ordinal);
