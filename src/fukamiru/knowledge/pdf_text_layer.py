"""Shared, read-only PDF text-layer preflight for ingestion and OCR verification."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from fukamiru.errors import FukamiruError


@dataclass(frozen=True)
class PDFTextLayer:
    page_count: int
    visible_characters: int
    minimum_characters: int

    @property
    def usable(self) -> bool:
        return self.page_count > 0 and self.visible_characters >= self.minimum_characters


def has_usable_text_layer(page_count: int, visible_characters: int) -> bool:
    return page_count > 0 and visible_characters >= max(20, page_count * 5)


def inspect_pdf_text_layer(path: Path) -> PDFTextLayer:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - installation supplies pypdf
        raise FukamiruError("PDF support is unavailable; rerun install.ps1") from exc
    try:
        reader = PdfReader(path)
        count = len(reader.pages)
        visible = sum(
            len(re.sub(r"\s+", "", page.extract_text() or ""))
            for page in reader.pages
        )
    except Exception as exc:
        raise FukamiruError(f"PDF text-layer inspection failed: {exc}") from exc
    return PDFTextLayer(count, visible, max(20, count * 5))


def require_pdf_text_layer(path: Path) -> PDFTextLayer:
    result = inspect_pdf_text_layer(path)
    if not result.usable:
        raise FukamiruError(
            "OCR_REQUIRED: PDFに利用可能なテキスト層がありません。"
            "言語別OCRパイプラインを実行してください。"
        )
    return result
