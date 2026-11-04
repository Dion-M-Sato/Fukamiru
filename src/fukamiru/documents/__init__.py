"""Canonical HTML parsing and document services."""

from fukamiru.documents.canonical_html import (
    CanonicalHTMLReport,
    CanonicalHTMLValidator,
    ValidationIssue,
)
from fukamiru.documents.html_blocks import canonical_html_blocks
from fukamiru.documents.japanese_style import (
    ENGLISH_JAPANESE_PROSE_GUIDANCE,
    JAPANESE_PROSE_GUIDANCE,
)
from fukamiru.documents.japanese_surface import (
    JapaneseSurfaceGateResult,
    JapaneseSurfaceIssue,
    JapaneseSurfaceQualityGate,
)

__all__ = [
    "CanonicalHTMLReport",
    "CanonicalHTMLValidator",
    "JapaneseSurfaceGateResult",
    "JapaneseSurfaceIssue",
    "JapaneseSurfaceQualityGate",
    "ENGLISH_JAPANESE_PROSE_GUIDANCE",
    "JAPANESE_PROSE_GUIDANCE",
    "ValidationIssue",
    "canonical_html_blocks",
]
