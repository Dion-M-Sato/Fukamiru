ALTER TABLE section_contracts
    ADD COLUMN article_role TEXT NOT NULL DEFAULT 'body'
    CHECK (article_role IN ('introduction', 'body', 'conclusion'));

ALTER TABLE section_contracts
    ADD COLUMN minimum_evidence_items INTEGER NOT NULL DEFAULT 1
    CHECK (minimum_evidence_items >= 1);

ALTER TABLE section_contracts
    ADD COLUMN minimum_concrete_examples INTEGER NOT NULL DEFAULT 1
    CHECK (minimum_concrete_examples >= 1);

ALTER TABLE section_contracts
    ADD COLUMN required_argument_moves_json TEXT NOT NULL DEFAULT '[]';
