ALTER TABLE authoring_plan_revisions ADD COLUMN provisional_answer TEXT NOT NULL DEFAULT '';
ALTER TABLE authoring_plan_revisions ADD COLUMN research_gaps_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE authoring_plan_revisions ADD COLUMN rival_interpretations_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE atomic_propositions ADD COLUMN argument_role TEXT NOT NULL DEFAULT 'author_claim';
ALTER TABLE claim_tree_nodes ADD COLUMN research_gap TEXT NOT NULL DEFAULT '';
ALTER TABLE claim_tree_nodes ADD COLUMN rival_interpretation TEXT NOT NULL DEFAULT '';
ALTER TABLE discourse_nodes ADD COLUMN section_answer TEXT NOT NULL DEFAULT '';
ALTER TABLE section_contracts ADD COLUMN section_answer TEXT NOT NULL DEFAULT '';
