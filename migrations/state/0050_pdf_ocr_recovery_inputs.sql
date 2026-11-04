-- Preserve original PDF identity while OCR runs against a derived image-only PDF.
ALTER TABLE pdf_ocr_jobs ADD COLUMN input_pdf_path TEXT;
ALTER TABLE pdf_ocr_jobs ADD COLUMN input_pdf_sha256 TEXT;
ALTER TABLE pdf_ocr_jobs ADD COLUMN recovery_kind TEXT;
ALTER TABLE pdf_ocr_jobs ADD COLUMN recovery_manifest_path TEXT;
