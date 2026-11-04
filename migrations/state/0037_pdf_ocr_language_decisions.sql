ALTER TABLE pdf_ocr_jobs
    ADD COLUMN language_origin TEXT NOT NULL DEFAULT 'unresolved'
    CHECK (language_origin IN ('explicit', 'detected', 'manual', 'unresolved'));

ALTER TABLE pdf_ocr_jobs
    ADD COLUMN language_confidence REAL
    CHECK (language_confidence IS NULL OR (language_confidence >= 0.0 AND language_confidence <= 1.0));

ALTER TABLE pdf_ocr_jobs
    ADD COLUMN language_decision_json TEXT;

UPDATE pdf_ocr_jobs
SET language_origin = 'explicit',
    language_confidence = 1.0,
    language_decision_json = '{"language":"' || source_language
        || '","confidence":1.0,"method":"legacy_explicit_or_manual","evidence":{}}'
WHERE source_language IN ('ja', 'en', 'fr');

UPDATE pdf_ocr_jobs
SET language_decision_json =
    '{"language":"und","confidence":0.0,"method":"legacy_unresolved","evidence":{}}'
WHERE source_language = 'und';
