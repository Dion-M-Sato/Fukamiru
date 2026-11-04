-- V31 uses zero to mean that evidence and example counts are not prose quotas.
-- Keep the existing values of immutable V30 plans while relaxing both checks.
CREATE TEMP TABLE section_contract_minima_before_v31 (
    id TEXT PRIMARY KEY,
    minimum_evidence_items INTEGER NOT NULL,
    minimum_concrete_examples INTEGER NOT NULL
);

INSERT INTO section_contract_minima_before_v31
SELECT id, minimum_evidence_items, minimum_concrete_examples
FROM section_contracts;

DROP TRIGGER section_contracts_immutable_update;

ALTER TABLE section_contracts DROP COLUMN minimum_evidence_items;
ALTER TABLE section_contracts DROP COLUMN minimum_concrete_examples;

ALTER TABLE section_contracts ADD COLUMN minimum_evidence_items
    INTEGER NOT NULL DEFAULT 1 CHECK (minimum_evidence_items >= 0);
ALTER TABLE section_contracts ADD COLUMN minimum_concrete_examples
    INTEGER NOT NULL DEFAULT 1 CHECK (minimum_concrete_examples >= 0);

UPDATE section_contracts
SET minimum_evidence_items = (
        SELECT old.minimum_evidence_items
        FROM section_contract_minima_before_v31 AS old
        WHERE old.id = section_contracts.id
    ),
    minimum_concrete_examples = (
        SELECT old.minimum_concrete_examples
        FROM section_contract_minima_before_v31 AS old
        WHERE old.id = section_contracts.id
    );

DROP TABLE section_contract_minima_before_v31;

CREATE TRIGGER section_contracts_immutable_update BEFORE UPDATE ON section_contracts
BEGIN SELECT RAISE(ABORT, 'authoring plan content is immutable'); END;
