CREATE TABLE manuscript_collocation_frames (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    node_run_id TEXT REFERENCES node_runs(id) ON DELETE SET NULL,
    document_id TEXT NOT NULL REFERENCES content_documents(id) ON DELETE CASCADE,
    block_id TEXT NOT NULL,
    sentence_ordinal INTEGER NOT NULL CHECK (sentence_ordinal >= 0),
    sentence_start INTEGER NOT NULL CHECK (sentence_start >= 0),
    sentence_end INTEGER NOT NULL CHECK (sentence_end > sentence_start),
    predicate_start INTEGER NOT NULL CHECK (predicate_start >= sentence_start),
    predicate_end INTEGER NOT NULL CHECK (predicate_end > predicate_start),
    source_expression TEXT NOT NULL,
    frame_json TEXT NOT NULL,
    signature TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, signature)
);

CREATE INDEX idx_manuscript_collocation_frames_run
ON manuscript_collocation_frames(run_id, block_id, sentence_ordinal, predicate_start);

CREATE TABLE manuscript_collocation_candidates (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    frame_id TEXT NOT NULL REFERENCES manuscript_collocation_frames(id) ON DELETE CASCADE,
    candidate_ordinal INTEGER NOT NULL CHECK (candidate_ordinal >= 0),
    expression TEXT NOT NULL,
    source_kind TEXT NOT NULL CHECK (source_kind IN ('original', 'codex', 'llm_jp_instruct', 'rule', 'lexical_resource', 'corpus_retrieval')),
    rationale TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(frame_id, candidate_ordinal),
    UNIQUE(frame_id, expression)
);

CREATE TABLE manuscript_collocation_probes (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    frame_id TEXT NOT NULL REFERENCES manuscript_collocation_frames(id) ON DELETE CASCADE,
    probe_kind TEXT NOT NULL CHECK (probe_kind IN ('original_sentence', 'local_predicate_frame', 'canonicalized_frame')),
    prefix_text TEXT NOT NULL,
    suffix_text TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(frame_id, probe_kind)
);

CREATE TABLE manuscript_collocation_scores (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    candidate_id TEXT NOT NULL REFERENCES manuscript_collocation_candidates(id) ON DELETE CASCADE,
    probe_id TEXT NOT NULL REFERENCES manuscript_collocation_probes(id) ON DELETE CASCADE,
    scorer_type TEXT NOT NULL,
    scorer_identity TEXT NOT NULL,
    features_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(candidate_id, probe_id, scorer_type, scorer_identity)
);

CREATE INDEX idx_manuscript_collocation_scores_run
ON manuscript_collocation_scores(run_id, scorer_type, created_at);

CREATE TABLE manuscript_collocation_semantic_verdicts (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    candidate_id TEXT NOT NULL REFERENCES manuscript_collocation_candidates(id) ON DELETE CASCADE,
    verdict TEXT NOT NULL CHECK (verdict IN ('true', 'false', 'uncertain')),
    rationale TEXT NOT NULL,
    verifier_identity TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(candidate_id, verifier_identity)
);

CREATE TABLE manuscript_collocation_decisions (
    frame_id TEXT PRIMARY KEY REFERENCES manuscript_collocation_frames(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    original_candidate_id TEXT NOT NULL REFERENCES manuscript_collocation_candidates(id),
    suggested_candidate_id TEXT REFERENCES manuscript_collocation_candidates(id),
    decision TEXT NOT NULL CHECK (decision IN ('original', 'suggestion', 'needs_human_review')),
    evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE manuscript_collocation_human_judgments (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    frame_id TEXT NOT NULL REFERENCES manuscript_collocation_frames(id) ON DELETE CASCADE,
    candidate_id TEXT NOT NULL REFERENCES manuscript_collocation_candidates(id) ON DELETE CASCADE,
    judgment TEXT NOT NULL CHECK (judgment IN ('accept', 'reject')),
    comment TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
