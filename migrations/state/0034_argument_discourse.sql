CREATE TABLE authoring_plans (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES content_documents(id) ON DELETE CASCADE,
    source_revision_id TEXT NOT NULL REFERENCES revisions(id) ON DELETE RESTRICT,
    current_revision INTEGER NOT NULL DEFAULT 1 CHECK (current_revision > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (id, document_id)
);

CREATE TABLE authoring_plan_revisions (
    plan_id TEXT NOT NULL REFERENCES authoring_plans(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL CHECK (revision > 0),
    parent_revision INTEGER,
    research_question TEXT NOT NULL,
    scope_json TEXT NOT NULL DEFAULT '{}',
    assumptions_json TEXT NOT NULL DEFAULT '[]',
    schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version > 0),
    content_sha256 TEXT NOT NULL,
    producer_node_run_id TEXT REFERENCES node_runs(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (plan_id, revision),
    FOREIGN KEY (plan_id, parent_revision) REFERENCES authoring_plan_revisions(plan_id, revision) ON DELETE RESTRICT,
    CHECK (parent_revision IS NULL OR parent_revision < revision),
    CHECK (length(trim(research_question)) > 0)
);

CREATE TABLE atomic_propositions (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    plan_revision INTEGER NOT NULL,
    text TEXT NOT NULL CHECK (length(trim(text)) > 0),
    proposition_type TEXT NOT NULL CHECK (proposition_type IN ('fact','observation','interpretation','hypothesis','definition','method','reservation','example')),
    source_block_id TEXT REFERENCES content_blocks(id) ON DELETE SET NULL,
    source_start INTEGER,
    source_end INTEGER,
    source_quote TEXT,
    confidence REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    provenance TEXT NOT NULL CHECK (provenance IN ('memo','rag','llm_inference','user','mixed')),
    producer_node_run_id TEXT REFERENCES node_runs(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (plan_id, plan_revision) REFERENCES authoring_plan_revisions(plan_id, revision) ON DELETE CASCADE,
    CHECK ((source_start IS NULL AND source_end IS NULL) OR (source_start >= 0 AND source_end >= source_start))
);
CREATE INDEX idx_atomic_propositions_plan ON atomic_propositions(plan_id, plan_revision);

CREATE TABLE claim_tree_nodes (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    plan_revision INTEGER NOT NULL,
    parent_id TEXT REFERENCES claim_tree_nodes(id) ON DELETE CASCADE,
    claim_level TEXT NOT NULL CHECK (claim_level IN ('main','sub','supporting')),
    text TEXT NOT NULL CHECK (length(trim(text)) > 0),
    proposition_id TEXT REFERENCES atomic_propositions(id) ON DELETE SET NULL,
    rationale TEXT NOT NULL DEFAULT '',
    provenance TEXT NOT NULL CHECK (provenance IN ('memo','rag','llm_inference','user','mixed')),
    created_at TEXT NOT NULL,
    FOREIGN KEY (plan_id, plan_revision) REFERENCES authoring_plan_revisions(plan_id, revision) ON DELETE CASCADE,
    UNIQUE (id, plan_id, plan_revision)
);
CREATE UNIQUE INDEX idx_claim_tree_single_main ON claim_tree_nodes(plan_id, plan_revision) WHERE claim_level = 'main';
CREATE INDEX idx_claim_tree_parent ON claim_tree_nodes(plan_id, plan_revision, parent_id);

CREATE TABLE argument_nodes (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    plan_revision INTEGER NOT NULL,
    node_type TEXT NOT NULL CHECK (node_type IN ('claim','evidence','warrant','qualifier','counterexample','rebuttal')),
    text TEXT NOT NULL CHECK (length(trim(text)) > 0),
    claim_tree_node_id TEXT REFERENCES claim_tree_nodes(id) ON DELETE CASCADE,
    proposition_id TEXT REFERENCES atomic_propositions(id) ON DELETE SET NULL,
    source_span_id TEXT,
    source_uri TEXT,
    verification_status TEXT NOT NULL DEFAULT 'unverified' CHECK (verification_status IN ('unverified','verified','rejected')),
    provenance TEXT NOT NULL CHECK (provenance IN ('memo','rag','llm_inference','user','mixed')),
    confidence REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    details_json TEXT NOT NULL DEFAULT '{}',
    producer_node_run_id TEXT REFERENCES node_runs(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (plan_id, plan_revision) REFERENCES authoring_plan_revisions(plan_id, revision) ON DELETE CASCADE
);
CREATE INDEX idx_argument_nodes_plan ON argument_nodes(plan_id, plan_revision, node_type);

CREATE TABLE argument_edges (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    plan_revision INTEGER NOT NULL,
    source_node_id TEXT NOT NULL REFERENCES argument_nodes(id) ON DELETE CASCADE,
    target_node_id TEXT NOT NULL REFERENCES argument_nodes(id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL CHECK (relation_type IN ('supports','warrants','qualifies','challenges','rebuts','depends_on','alternative_to')),
    rationale TEXT NOT NULL DEFAULT '',
    provenance TEXT NOT NULL CHECK (provenance IN ('memo','rag','llm_inference','user','mixed')),
    created_at TEXT NOT NULL,
    FOREIGN KEY (plan_id, plan_revision) REFERENCES authoring_plan_revisions(plan_id, revision) ON DELETE CASCADE,
    UNIQUE (source_node_id, target_node_id, relation_type),
    CHECK (source_node_id <> target_node_id)
);
CREATE INDEX idx_argument_edges_target ON argument_edges(plan_id, plan_revision, target_node_id);

CREATE TABLE hypothesis_candidates (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    plan_revision INTEGER NOT NULL,
    text TEXT NOT NULL CHECK (length(trim(text)) > 0),
    gap_type TEXT NOT NULL CHECK (gap_type IN ('causal','comparison','logical_leap','alternative_explanation','falsification','boundary','other')),
    status TEXT NOT NULL CHECK (status IN ('candidate','adopted','rejected')),
    rationale TEXT NOT NULL DEFAULT '',
    provenance TEXT NOT NULL CHECK (provenance IN ('memo','rag','llm_inference','user','mixed')),
    created_at TEXT NOT NULL,
    FOREIGN KEY (plan_id, plan_revision) REFERENCES authoring_plan_revisions(plan_id, revision) ON DELETE CASCADE
);

CREATE TABLE discourse_nodes (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    plan_revision INTEGER NOT NULL,
    parent_id TEXT REFERENCES discourse_nodes(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    node_kind TEXT NOT NULL CHECK (node_kind IN ('root','section','group')),
    rhetorical_relation TEXT NOT NULL CHECK (rhetorical_relation IN ('purpose','evidence','explanation','elaboration','contrast','concession','cause','condition','background','result','sequence','summary')),
    attentional_focus TEXT NOT NULL DEFAULT '',
    section_id TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (plan_id, plan_revision) REFERENCES authoring_plan_revisions(plan_id, revision) ON DELETE CASCADE,
    UNIQUE (plan_id, plan_revision, parent_id, ordinal)
);
CREATE UNIQUE INDEX idx_discourse_single_root ON discourse_nodes(plan_id, plan_revision) WHERE parent_id IS NULL;

CREATE TABLE section_contracts (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    plan_revision INTEGER NOT NULL,
    discourse_node_id TEXT NOT NULL UNIQUE REFERENCES discourse_nodes(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    local_question TEXT NOT NULL,
    expected_conclusion TEXT NOT NULL,
    claim_ids_json TEXT NOT NULL DEFAULT '[]',
    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    warrant_ids_json TEXT NOT NULL DEFAULT '[]',
    qualifiers_json TEXT NOT NULL DEFAULT '[]',
    counterarguments_json TEXT NOT NULL DEFAULT '[]',
    previous_bridge TEXT NOT NULL DEFAULT '',
    next_bridge TEXT NOT NULL DEFAULT '',
    required_terms_json TEXT NOT NULL DEFAULT '[]',
    forbidden_topics_json TEXT NOT NULL DEFAULT '[]',
    target_length INTEGER CHECK (target_length IS NULL OR target_length > 0),
    created_at TEXT NOT NULL,
    FOREIGN KEY (plan_id, plan_revision) REFERENCES authoring_plan_revisions(plan_id, revision) ON DELETE CASCADE
);

CREATE TABLE section_drafts (
    id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL REFERENCES section_contracts(id) ON DELETE CASCADE,
    attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt > 0),
    body TEXT NOT NULL,
    used_claim_ids_json TEXT NOT NULL DEFAULT '[]',
    used_evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    new_propositions_json TEXT NOT NULL DEFAULT '[]',
    producer_node_run_id TEXT REFERENCES node_runs(id) ON DELETE SET NULL,
    content_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (contract_id, attempt)
);

CREATE TABLE audit_findings (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    plan_revision INTEGER NOT NULL,
    defect_type TEXT NOT NULL CHECK (defect_type IN ('claim_drift','topic_drift','unsupported_claim','missing_warrant','evidence_mismatch','qualifier_loss','counterargument_omission','conclusion_mismatch','focus_break','redundancy','contradiction','citation_overclaim','structure_invalid','other')),
    severity TEXT NOT NULL CHECK (severity IN ('info','minor','major','critical')),
    object_type TEXT,
    object_id TEXT,
    message TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','repairing','resolved','accepted','wont_fix')),
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    FOREIGN KEY (plan_id, plan_revision) REFERENCES authoring_plan_revisions(plan_id, revision) ON DELETE CASCADE
);
CREATE INDEX idx_audit_findings_plan_status ON audit_findings(plan_id, plan_revision, status, severity);

CREATE TRIGGER authoring_plan_revision_immutable_update
BEFORE UPDATE ON authoring_plan_revisions BEGIN SELECT RAISE(ABORT, 'authoring plan revisions are immutable'); END;
CREATE TRIGGER authoring_plan_revision_immutable_delete
BEFORE DELETE ON authoring_plan_revisions BEGIN SELECT RAISE(ABORT, 'authoring plan revisions are immutable'); END;

CREATE TRIGGER atomic_propositions_immutable_update BEFORE UPDATE ON atomic_propositions
BEGIN SELECT RAISE(ABORT, 'authoring plan content is immutable'); END;
CREATE TRIGGER atomic_propositions_immutable_delete BEFORE DELETE ON atomic_propositions
BEGIN SELECT RAISE(ABORT, 'authoring plan content is immutable'); END;
CREATE TRIGGER claim_tree_nodes_immutable_update BEFORE UPDATE ON claim_tree_nodes
BEGIN SELECT RAISE(ABORT, 'authoring plan content is immutable'); END;
CREATE TRIGGER claim_tree_nodes_immutable_delete BEFORE DELETE ON claim_tree_nodes
BEGIN SELECT RAISE(ABORT, 'authoring plan content is immutable'); END;
CREATE TRIGGER argument_nodes_immutable_update BEFORE UPDATE ON argument_nodes
BEGIN SELECT RAISE(ABORT, 'authoring plan content is immutable'); END;
CREATE TRIGGER argument_nodes_immutable_delete BEFORE DELETE ON argument_nodes
BEGIN SELECT RAISE(ABORT, 'authoring plan content is immutable'); END;
CREATE TRIGGER argument_edges_immutable_update BEFORE UPDATE ON argument_edges
BEGIN SELECT RAISE(ABORT, 'authoring plan content is immutable'); END;
CREATE TRIGGER argument_edges_immutable_delete BEFORE DELETE ON argument_edges
BEGIN SELECT RAISE(ABORT, 'authoring plan content is immutable'); END;
CREATE TRIGGER hypothesis_candidates_immutable_update BEFORE UPDATE ON hypothesis_candidates
BEGIN SELECT RAISE(ABORT, 'authoring plan content is immutable'); END;
CREATE TRIGGER hypothesis_candidates_immutable_delete BEFORE DELETE ON hypothesis_candidates
BEGIN SELECT RAISE(ABORT, 'authoring plan content is immutable'); END;
CREATE TRIGGER discourse_nodes_immutable_update BEFORE UPDATE ON discourse_nodes
BEGIN SELECT RAISE(ABORT, 'authoring plan content is immutable'); END;
CREATE TRIGGER discourse_nodes_immutable_delete BEFORE DELETE ON discourse_nodes
BEGIN SELECT RAISE(ABORT, 'authoring plan content is immutable'); END;
CREATE TRIGGER section_contracts_immutable_update BEFORE UPDATE ON section_contracts
BEGIN SELECT RAISE(ABORT, 'authoring plan content is immutable'); END;
CREATE TRIGGER section_contracts_immutable_delete BEFORE DELETE ON section_contracts
BEGIN SELECT RAISE(ABORT, 'authoring plan content is immutable'); END;
