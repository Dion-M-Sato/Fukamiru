CREATE TABLE manuscript_reasoning_reviews (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE REFERENCES runs(id) ON DELETE CASCADE,
    node_run_id TEXT REFERENCES node_runs(id) ON DELETE SET NULL,
    document_id TEXT NOT NULL REFERENCES content_documents(id) ON DELETE CASCADE,
    source_proposal_id TEXT REFERENCES manuscript_rewrite_proposals(id) ON DELETE SET NULL,
    human_review TEXT NOT NULL DEFAULT '',
    assessment_json TEXT NOT NULL,
    research_queries_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK (status IN ('completed', 'failed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_manuscript_reasoning_reviews_document
ON manuscript_reasoning_reviews(document_id, created_at);

CREATE TABLE manuscript_research_evidence (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    review_id TEXT NOT NULL REFERENCES manuscript_reasoning_reviews(id) ON DELETE CASCADE,
    finding_key TEXT,
    query TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK (source_type IN ('local_rag', 'online_paper')),
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

CREATE INDEX idx_manuscript_research_evidence_run
ON manuscript_research_evidence(run_id, query);

CREATE TABLE manuscript_polish_checkpoints (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    node_run_id TEXT REFERENCES node_runs(id) ON DELETE SET NULL,
    block_id TEXT NOT NULL,
    chunk_ordinal INTEGER NOT NULL CHECK (chunk_ordinal >= 0),
    request_signature TEXT NOT NULL,
    model_identity TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    source_text TEXT NOT NULL,
    edited_text TEXT NOT NULL,
    edits_json TEXT NOT NULL DEFAULT '[]',
    reasons_json TEXT NOT NULL DEFAULT '[]',
    issues_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK (status IN ('accepted', 'waiting_human', 'rejected')),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    diff TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(run_id, block_id, chunk_ordinal, request_signature)
);

CREATE INDEX idx_manuscript_polish_checkpoints_run
ON manuscript_polish_checkpoints(run_id, block_id, chunk_ordinal);
