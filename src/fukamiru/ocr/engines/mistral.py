"""Mistral OCR adapter for supported non-Japanese PDFs."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fukamiru.errors import FukamiruError
from fukamiru.ocr.models import OCRPage, OCRResult

_MAX_CHUNK_PAGES = 20
_MAX_CHUNK_BYTES = 20 * 1024 * 1024


class MistralOCREngine:
    name = "mistral"

    def __init__(
        self,
        *,
        model: str = "mistral-ocr-latest",
        max_attempts: int = 4,
        client_factory: Callable[[str], Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model = model
        self.max_attempts = max(1, max_attempts)
        self.client_factory = client_factory or self._default_client
        self.sleeper = sleeper

    @staticmethod
    def _default_client(api_key: str) -> Any:
        try:
            import truststore
            from mistralai.client import Mistral
        except ImportError as exc:
            raise FukamiruError(
                "Mistral SDKがありません。`uv sync --extra ocr`を実行してください。"
            ) from exc
        truststore.inject_into_ssl()
        return Mistral(api_key=api_key)

    def process(
        self, source: Path, language: str, *, checkpoint_dir: Path | None = None
    ) -> OCRResult:
        api_key = os.environ.get("MISTRAL_API_KEY")
        if not api_key:
            raise FukamiruError(
                "MISTRAL_API_KEY が設定されていません。\n"
                "Codex Desktop の PowerShell で以下を実行してください:\n\n"
                '$env:MISTRAL_API_KEY="..."'
            )
        client = self.client_factory(api_key)
        digest = _sha256(source)
        import pymupdf

        collected: list[OCRPage] = []
        with pymupdf.open(source) as pdf:
            if pdf.page_count == 0:
                raise FukamiruError("Mistral OCR対象PDFにページがありません")
            if source.stat().st_size <= _MAX_CHUNK_BYTES and pdf.page_count <= _MAX_CHUNK_PAGES:
                pages = self._request_pages(client, source.read_bytes(), 0, pdf.page_count)
            else:
                for start in range(0, pdf.page_count, _MAX_CHUNK_PAGES):
                    end = min(start + _MAX_CHUNK_PAGES, pdf.page_count)
                    collected.extend(
                        self._process_range(pdf, client, digest, start, end, checkpoint_dir)
                    )
                pages = tuple(collected)
        if not pages:
            raise FukamiruError("Mistral OCRがページ結果を返しませんでした")
        return OCRResult(source, digest, language, self.name, pages)

    def _request_pages(
        self, client: Any, pdf_bytes: bytes, start: int, expected: int | None
    ) -> tuple[OCRPage, ...]:
        encoded = base64.b64encode(pdf_bytes).decode("ascii")
        document = {
            "type": "document_url",
            "document_url": f"data:application/pdf;base64,{encoded}",
        }
        response: Any | None = None
        for attempt in range(self.max_attempts):
            try:
                response = client.ocr.process(
                    model=self.model,
                    document=document,
                    include_image_base64=False,
                )
                break
            except Exception as exc:
                if not self._retryable(exc) or attempt + 1 >= self.max_attempts:
                    raise FukamiruError(self._error_message(exc)) from exc
                self.sleeper(float(2**attempt))
        pages = tuple(
            OCRPage(
                page_number=start + int(getattr(page, "index", index - 1)) + 1,
                text=str(getattr(page, "markdown", "") or ""),
                metadata={"dimensions": self._serializable(getattr(page, "dimensions", None))},
            )
            for index, page in enumerate(getattr(response, "pages", ()) or (), start=1)
        )
        if expected is not None and sorted(page.page_number for page in pages) != list(
            range(start + 1, start + expected + 1)
        ):
            raise FukamiruError(
                f"Mistral OCRのページ対応が不完全です: {start + 1}-{start + expected}"
            )
        return tuple(sorted(pages, key=lambda page: page.page_number))

    def _process_range(
        self,
        pdf: Any,
        client: Any,
        digest: str,
        start: int,
        end: int,
        checkpoint_dir: Path | None,
    ) -> tuple[OCRPage, ...]:
        checkpoint = None
        if checkpoint_dir is not None:
            checkpoint_dir.mkdir(parents=True, exist_ok=True)
            model_hash = hashlib.sha256(self.model.encode("utf-8")).hexdigest()[:10]
            checkpoint = checkpoint_dir / f"{digest[:16]}-{model_hash}-{start:05d}-{end:05d}.json"
            if checkpoint.is_file():
                try:
                    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
                    if (
                        saved.get("source_sha256") == digest
                        and saved.get("model") == self.model
                        and saved.get("start") == start
                        and saved.get("end") == end
                    ):
                        pages = tuple(
                            OCRPage(
                                page_number=int(page["page_number"]),
                                text=str(page["text"]),
                                metadata=dict(page["metadata"]),
                            )
                            for page in saved["pages"]
                        )
                        if [p.page_number for p in pages] == list(range(start + 1, end + 1)):
                            return pages
                except (OSError, ValueError, KeyError, TypeError):
                    pass

        import pymupdf

        with pymupdf.open() as part:
            part.insert_pdf(pdf, from_page=start, to_page=end - 1, links=False, annots=False)
            pdf_bytes = part.tobytes(garbage=3, deflate=True)
        if len(pdf_bytes) > _MAX_CHUNK_BYTES:
            if end - start > 1:
                middle = start + (end - start) // 2
                return (
                    *self._process_range(pdf, client, digest, start, middle, checkpoint_dir),
                    *self._process_range(pdf, client, digest, middle, end, checkpoint_dir),
                )
            page = pdf.load_page(start)
            scale = min(150 / 72, 2500 / max(page.rect.width, page.rect.height))
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
            with pymupdf.open() as image_pdf:
                image_page = image_pdf.new_page(
                    width=page.rect.width, height=page.rect.height
                )
                image_page.insert_image(
                    image_page.rect, stream=pixmap.tobytes("jpeg", jpg_quality=75)
                )
                pdf_bytes = image_pdf.tobytes(garbage=3, deflate=True)
            if len(pdf_bytes) > _MAX_CHUNK_BYTES:
                raise FukamiruError(f"Mistral OCR単一ページが容量上限を超えます: {start + 1}")

        pages = self._request_pages(client, pdf_bytes, start, end - start)
        if checkpoint is not None:
            data = {
                "source_sha256": digest,
                "model": self.model,
                "start": start,
                "end": end,
                "pages": [
                    {
                        "page_number": page.page_number,
                        "text": page.text,
                        "metadata": page.metadata,
                    }
                    for page in pages
                ],
            }
            temporary = checkpoint.with_suffix(".json.tmp")
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, checkpoint)
        return pages

    @staticmethod
    def _serializable(value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool, list, dict)):
            return value
        if hasattr(value, "model_dump"):
            return value.model_dump()
        return str(value)

    @staticmethod
    def _retryable(exc: Exception) -> bool:
        status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
        return status == 429 or (isinstance(status, int) and status >= 500) or status is None

    @staticmethod
    def _error_message(exc: Exception) -> str:
        status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
        if status in {401, 403}:
            return "Mistral APIの認証に失敗しました"
        if status == 429:
            return "Mistral APIのレート制限に達しました"
        if isinstance(status, int) and status >= 500:
            return f"Mistral APIで障害が発生しました (HTTP {status})"
        return f"Mistral OCRへの接続または処理に失敗しました: {exc}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
