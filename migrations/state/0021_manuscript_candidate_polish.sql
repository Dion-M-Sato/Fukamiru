CREATE TABLE manuscript_polish_targets (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    node_run_id TEXT REFERENCES node_runs(id) ON DELETE SET NULL,
    document_id TEXT NOT NULL REFERENCES content_documents(id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    section_id TEXT NOT NULL,
    block_id TEXT NOT NULL,
    target_ordinal INTEGER NOT NULL CHECK (target_ordinal >= 0),
    start_offset INTEGER NOT NULL CHECK (start_offset >= 0),
    end_offset INTEGER NOT NULL CHECK (end_offset > start_offset),
    source_sha256 TEXT NOT NULL,
    source_text TEXT NOT NULL,
    context_before TEXT NOT NULL DEFAULT '',
    context_after TEXT NOT NULL DEFAULT '',
    issue_codes_json TEXT NOT NULL DEFAULT '[]',
    relation_type TEXT NOT NULL,
    semantic_contract_json TEXT NOT NULL DEFAULT '{}',
    request_signature TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'detected'
        CHECK (status IN ('detected', 'generated', 'scored', 'selected')),
    selected_candidate_id TEXT,
    selection_mode TEXT,
    selection_margin REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(run_id, block_id, target_ordinal, request_signature)
);

CREATE INDEX idx_manuscript_polish_targets_run
ON manuscript_polish_targets(run_id, section_id, block_id, start_offset);

CREATE TABLE manuscript_polish_candidates (
    id TEXT PRIMARY KEY,
    target_id TEXT NOT NULL REFERENCES manuscript_polish_targets(id) ON DELETE CASCADE,
    candidate_ordinal INTEGER NOT NULL CHECK (candidate_ordinal >= 0),
    candidate_text TEXT NOT NULL,
    source_kind TEXT NOT NULL CHECK (source_kind IN ('original', 'llm_jp')),
    relation_type TEXT NOT NULL,
    rationale TEXT NOT NULL DEFAULT '',
    model_confidence REAL NOT NULL DEFAULT 0.0
        CHECK (model_confidence >= 0 AND model_confidence <= 1),
    hard_vetoed INTEGER NOT NULL DEFAULT 0 CHECK (hard_vetoed IN (0, 1)),
    veto_reasons_json TEXT NOT NULL DEFAULT '[]',
    component_scores_json TEXT NOT NULL DEFAULT '{}',
    total_score REAL,
    rank INTEGER CHECK (rank IS NULL OR rank >= 1),
    selected INTEGER NOT NULL DEFAULT 0 CHECK (selected IN (0, 1)),
    selection_mode TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(target_id, candidate_ordinal),
    UNIQUE(target_id, candidate_text)
);

CREATE INDEX idx_manuscript_polish_candidates_target
ON manuscript_polish_candidates(target_id, hard_vetoed, rank);

CREATE TABLE manuscript_polish_generation_audits (
    target_id TEXT PRIMARY KEY REFERENCES manuscript_polish_targets(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    model_identity TEXT NOT NULL,
    expected_candidate_count INTEGER NOT NULL CHECK (
        expected_candidate_count BETWEEN 5 AND 8
    ),
    generated_candidate_count INTEGER NOT NULL CHECK (generated_candidate_count >= 1),
    issues_json TEXT NOT NULL DEFAULT '[]',
    completed_at TEXT NOT NULL
);

CREATE INDEX idx_manuscript_polish_generation_run
ON manuscript_polish_generation_audits(run_id, completed_at);

CREATE TABLE manuscript_polish_preferences (
    id TEXT PRIMARY KEY,
    scope_type TEXT NOT NULL CHECK (scope_type IN ('document', 'research', 'author')),
    scope_key TEXT NOT NULL,
    target_id TEXT REFERENCES manuscript_polish_targets(id) ON DELETE SET NULL,
    relation_type TEXT NOT NULL,
    source_text TEXT NOT NULL,
    selected_text TEXT NOT NULL,
    rejected_texts_json TEXT NOT NULL DEFAULT '[]',
    features_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX idx_manuscript_polish_preferences_scope
ON manuscript_polish_preferences(scope_type, scope_key, relation_type, created_at);
