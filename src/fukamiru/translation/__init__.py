"""Translation provider adapters and durable support services."""

from fukamiru.translation.alignment import TranslationAlignmentService
from fukamiru.translation.book import (
    assert_translated_book,
    parse_markdown_book,
    render_translated_book,
)
from fukamiru.translation.codex import CodexTranslationAdapter
from fukamiru.translation.memory import TranslationMemoryService
from fukamiru.translation.research import (
    AcademicEvidenceResolver,
    AcademicResearchRepository,
    ResearchTriggerDetector,
    TranslationComplianceValidator,
    instruction_decisions,
)

__all__ = [
    "CodexTranslationAdapter",
    "AcademicEvidenceResolver",
    "AcademicResearchRepository",
    "ResearchTriggerDetector",
    "TranslationComplianceValidator",
    "TranslationAlignmentService",
    "TranslationMemoryService",
    "assert_translated_book",
    "parse_markdown_book",
    "render_translated_book",
    "instruction_decisions",
]
