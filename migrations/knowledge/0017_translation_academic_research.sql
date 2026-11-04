CREATE TABLE translation_research_triggers (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    document_unit_id TEXT NOT NULL REFERENCES document_units(id) ON DELETE CASCADE,
    trigger_type TEXT NOT NULL CHECK (
        trigger_type IN ('person', 'research', 'concept', 'quotation')
    ),
    source_text TEXT NOT NULL,
    normalized_key TEXT NOT NULL,
    query_text TEXT NOT NULL,
    source_span_ids_json TEXT NOT NULL DEFAULT '[]',
    context_text TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL CHECK (status IN ('detected', 'resolved', 'unresolved')),
    confidence REAL NOT NULL CHECK (confidence BETWEEN 0.0 AND 1.0),
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    UNIQUE (run_id, trigger_type, normalized_key)
);

CREATE INDEX idx_translation_research_triggers_run
ON translation_research_triggers(run_id, status, trigger_type);

CREATE TABLE translation_academic_evidence (
    id TEXT PRIMARY KEY,
    trigger_id TEXT NOT NULL
        REFERENCES translation_research_triggers(id) ON DELETE CASCADE,
    evidence_type TEXT NOT NULL CHECK (
        evidence_type IN (
            'user_instruction', 'local_corpus', 'bibliographic_record',
            'provider_metadata', 'primary_pdf'
        )
    ),
    bibliographic_record_id TEXT
        REFERENCES bibliographic_records(id) ON DELETE SET NULL,
    title TEXT,
    authors_json TEXT NOT NULL DEFAULT '[]',
    source_url TEXT,
    locator TEXT,
    excerpt TEXT NOT NULL,
    excerpt_sha256 TEXT NOT NULL,
    relevance REAL NOT NULL CHECK (relevance BETWEEN 0.0 AND 1.0),
    confidence REAL NOT NULL CHECK (confidence BETWEEN 0.0 AND 1.0),
    provenance_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX idx_translation_academic_evidence_trigger
ON translation_academic_evidence(trigger_id, relevance DESC, created_at);

CREATE TABLE translation_term_decisions (
    id TEXT PRIMARY KEY,
    origin_run_id TEXT,
    decision_kind TEXT NOT NULL CHECK (decision_kind IN ('term', 'person_name')),
    source_term TEXT NOT NULL,
    source_language TEXT NOT NULL,
    target_term TEXT NOT NULL,
    target_language TEXT NOT NULL,
    scope_type TEXT NOT NULL CHECK (scope_type IN ('document', 'author', 'research')),
    scope_key TEXT NOT NULL,
    document_unit_id TEXT REFERENCES document_units(id) ON DELETE CASCADE,
    author_name TEXT,
    research_title TEXT,
    rationale TEXT NOT NULL,
    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    forbidden_translations_json TEXT NOT NULL DEFAULT '[]',
    required INTEGER NOT NULL DEFAULT 1 CHECK (required IN (0, 1)),
    status TEXT NOT NULL CHECK (status IN ('candidate', 'approved', 'deprecated')),
    confidence REAL NOT NULL CHECK (confidence BETWEEN 0.0 AND 1.0),
    created_by TEXT NOT NULL CHECK (created_by IN ('human', 'agent', 'instruction')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (
        decision_kind, source_term, source_language, target_language,
        scope_type, scope_key
    )
);

CREATE INDEX idx_translation_term_decisions_scope
ON translation_term_decisions(
    scope_type, scope_key, target_language, status, required
);

CREATE TABLE translation_compliance_findings (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    translation_version_id TEXT NOT NULL
        REFERENCES translation_versions(id) ON DELETE CASCADE,
    batch_ordinal INTEGER,
    finding_type TEXT NOT NULL CHECK (
        finding_type IN (
            'forbidden_translation', 'person_name_remaining',
            'decision_not_applied'
        )
    ),
    severity TEXT NOT NULL CHECK (severity IN ('minor', 'major', 'critical')),
    decision_id TEXT REFERENCES translation_term_decisions(id) ON DELETE SET NULL,
    source_excerpt TEXT NOT NULL DEFAULT '',
    target_excerpt TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL CHECK (status IN ('open', 'resolved', 'waived')),
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE INDEX idx_translation_compliance_findings_run
ON translation_compliance_findings(run_id, status, severity, created_at);
