ALTER TABLE discourse_nodes
    ADD COLUMN article_role TEXT NOT NULL DEFAULT 'body'
    CHECK (article_role IN ('root', 'introduction', 'body', 'conclusion'));

ALTER TABLE discourse_nodes
    ADD COLUMN discourse_purpose TEXT NOT NULL DEFAULT '';

ALTER TABLE discourse_nodes
    ADD COLUMN claim_ids_json TEXT NOT NULL DEFAULT '[]';
