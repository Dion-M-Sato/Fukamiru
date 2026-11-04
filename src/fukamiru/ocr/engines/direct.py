"""Extract an existing PDF text layer without invoking OCR."""

from __future__ import annotations

import hashlib
from pathlib import Path

from pypdf import PdfReader

from fukamiru.errors import FukamiruError
from fukamiru.ocr.models import OCRPage, OCRResult


class DirectTextEngine:
    name = "direct"

    def process(self, source: Path, language: str) -> OCRResult:
        try:
            reader = PdfReader(source)
            if reader.is_encrypted and reader.decrypt("") == 0:
                raise FukamiruError("PDFは暗号化されており、本文を抽出できません")
            pages = tuple(
                OCRPage(page_number=index, text=(page.extract_text() or "").strip())
                for index, page in enumerate(reader.pages, start=1)
            )
        except FukamiruError:
            raise
        except Exception as exc:
            raise FukamiruError(f"PDF本文の直接抽出に失敗しました: {exc}") from exc
        return OCRResult(
            source=source,
            source_sha256=_sha256(source),
            language=language,
            engine=self.name,
            pages=pages,
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
