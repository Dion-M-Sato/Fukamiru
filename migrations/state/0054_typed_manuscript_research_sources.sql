-- V32 already distinguishes external books, primary sources and bibliography.
-- Retain every existing evidence record and its identity while matching that contract.
CREATE TABLE manuscript_research_evidence_v54 (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    review_id TEXT NOT NULL REFERENCES manuscript_reasoning_reviews(id) ON DELETE CASCADE,
    finding_key TEXT,
    query TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK (source_type IN (
        'local_rag', 'online_paper', 'online_book', 'online_primary', 'online_bibliography'
    )),
    provider TEXT NOT NULL,
    provider_record_id TEXT NOT NULL,
    title TEXT,
    authors_json TEXT NOT NULL DEFAULT '[]',
    source_url TEXT,
    locator TEXT,
    excerpt TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.0 CHECK (confidence >= 0 AND confidence <= 1),
    provenance_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(run_id, query, source_type, provider, provider_record_id)
);
INSERT INTO manuscript_research_evidence_v54 (
    id,run_id,review_id,finding_key,query,source_type,provider,provider_record_id,
    title,authors_json,source_url,locator,excerpt,confidence,provenance_json,created_at
)
SELECT id,run_id,review_id,finding_key,query,source_type,provider,provider_record_id,
       title,authors_json,source_url,locator,excerpt,confidence,provenance_json,created_at
FROM manuscript_research_evidence;
DROP TABLE manuscript_research_evidence;
ALTER TABLE manuscript_research_evidence_v54 RENAME TO manuscript_research_evidence;
CREATE INDEX idx_manuscript_research_evidence_run
ON manuscript_research_evidence(run_id, query);
