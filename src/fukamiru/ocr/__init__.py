"""Immutable OCR layers, language routing, audits, and rendering."""

from fukamiru.ocr.audit import (
    AuditPage,
    NoteCandidate,
    NoteReferenceCandidate,
    OCRAuditResult,
    OCRFinding,
    OCRIntegrityAuditor,
)
from fukamiru.ocr.models import OCRPage, OCRResult
from fukamiru.ocr.pipeline import OCRPipelineResult, OCRPipelineService
from fukamiru.ocr.render import OCRCanonicalHTMLRenderer
from fukamiru.ocr.repository import (
    OCRBlockInput,
    OCRPageInput,
    OCRProvenanceInput,
    OCRRevisionRepository,
)
from fukamiru.ocr.service import (
    SUPPORTED_OCR_LANGUAGES,
    DocumentOCRService,
    PDFOCRService,
    load_ocr_pages,
    normalize_language,
    routed_engine,
)

__all__ = [
    "AuditPage",
    "NoteCandidate",
    "NoteReferenceCandidate",
    "OCRAuditResult",
    "OCRBlockInput",
    "OCRFinding",
    "OCRIntegrityAuditor",
    "OCRCanonicalHTMLRenderer",
    "OCRPageInput",
    "OCRProvenanceInput",
    "OCRPipelineResult",
    "OCRPipelineService",
    "OCRRevisionRepository",
    "DocumentOCRService",
    "OCRPage",
    "OCRResult",
    "PDFOCRService",
    "SUPPORTED_OCR_LANGUAGES",
    "load_ocr_pages",
    "normalize_language",
    "routed_engine",
]
