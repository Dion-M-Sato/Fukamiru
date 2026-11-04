"""Common page-oriented OCR result model shared by every engine."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class OCRPage:
    page_number: int
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OCRResult:
    source: Path
    source_sha256: str
    language: str
    engine: str
    pages: tuple[OCRPage, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": str(self.source),
            "source_sha256": self.source_sha256,
            "language": self.language,
            "engine": self.engine,
            "page_count": len(self.pages),
            "pages": [asdict(page) for page in self.pages],
        }
