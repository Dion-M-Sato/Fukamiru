CREATE TABLE knowledge_search_reports (
    id TEXT PRIMARY KEY,
    query TEXT NOT NULL,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    result_count INTEGER NOT NULL,
    source_count INTEGER NOT NULL,
    model TEXT,
    report_json TEXT NOT NULL,
    canonical_html TEXT NOT NULL
);
CREATE INDEX knowledge_search_reports_created ON knowledge_search_reports(created_at DESC);
