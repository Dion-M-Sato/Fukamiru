"""Deterministic source ingestion and always-available lexical retrieval."""

from __future__ import annotations

import dataclasses
import hashlib
import html
import json
import mimetypes
import re
import sqlite3
import time
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path
from threading import Lock
from typing import Any

from fukamiru.config import Settings
from fukamiru.errors import (
    EmptyEmbeddingOffsetsError,
    FukamiruError,
    NonRetryableFukamiruError,
)
from fukamiru.knowledge.bibliography import ensure_source_bibliography, source_bibliography
from fukamiru.knowledge.language import classify_language
from fukamiru.knowledge.model_cache import (
    configure_huggingface_offline,
    resolve_local_model_snapshot,
)
from fukamiru.knowledge.pdf_layout import (
    ANALYZER_VERSION,
    PDFLayoutAnalyzer,
    PDFLayoutHTMLRenderer,
    PDFLayoutRepository,
    PreparedPDF,
)
from fukamiru.knowledge.pdf_text_layer import PDFTextLayer
from fukamiru.ocr.audit import AuditPage, OCRIntegrityAuditor
from fukamiru.ocr.pipeline import OCRPipelineService
from fukamiru.ocr.repository import OCRRevisionRepository

SUPPORTED_SUFFIXES = {".epub", ".pdf", ".txt", ".md", ".markdown", ".html", ".htm"}
TOKEN_PATTERN = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]|[\wÀ-ÿ]+|[^\s]", re.UNICODE)
IngestionProgress = Callable[[dict[str, Any]], None]
_tokenizer_init_lock = Lock()


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _uuid() -> str:
    return str(uuid.uuid4())


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    try:
        japanese_text = raw.decode("cp932")
    except UnicodeDecodeError:
        japanese_text = None
    if japanese_text and re.search(r"[\u3040-\u30ff\u3400-\u9fff]", japanese_text):
        return japanese_text
    try:
        return raw.decode("cp1252")
    except UnicodeDecodeError:
        if japanese_text is not None:
            return japanese_text
    raise FukamiruError(f"Text encoding could not be detected: {path}")


class _VisibleHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in {"script", "style", "template"}:
            self.hidden_depth += 1
        elif not self.hidden_depth and tag in {"p", "div", "section", "article", "li", "br"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "template"} and self.hidden_depth:
            self.hidden_depth -= 1
        elif not self.hidden_depth and tag in {"p", "div", "section", "article", "li"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth:
            self.parts.append(data)

    def text(self) -> str:
        return html.unescape("".join(self.parts))


def _extract_pages(path: Path) -> list[str]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:  # pragma: no cover - install script supplies dependency
            raise FukamiruError("PDF support is unavailable; rerun install.ps1") from exc
        try:
            reader = PdfReader(path)
            pages = [(page.extract_text() or "").strip() for page in reader.pages]
        except Exception as exc:
            raise FukamiruError(f"PDF text extraction failed: {exc}") from exc
        visible = sum(len(re.sub(r"\s+", "", page)) for page in pages)
        from fukamiru.knowledge.pdf_text_layer import has_usable_text_layer

        if not has_usable_text_layer(len(pages), visible):
            raise FukamiruError(
                "OCR_REQUIRED: PDFに利用可能なテキスト層がありません。"
                "言語別OCRパイプラインを実行してください。"
            )
        return pages
    text = _read_text(path)
    if suffix in {".html", ".htm"}:
        parser = _VisibleHTMLParser()
        parser.feed(text)
        text = parser.text()
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise FukamiruError(f"Source contains no extractable text: {path}")
    return [text]


def _paragraphs(text: str) -> list[str]:
    normalized = re.sub(r"[ \t]+", " ", text.replace("\u00a0", " "))
    values = [re.sub(r"\s*\n\s*", " ", item).strip() for item in re.split(r"\n\s*\n+", normalized)]
    values = [value for value in values if value]
    if len(values) == 1 and len(values[0]) > 2500:
        values = [
            value.strip()
            for value in re.split(r"(?<=[。！？.!?])\s+(?=[^\s])", values[0])
            if value.strip()
        ]
    return values


def _token_matches(text: str) -> list[re.Match[str]]:
    return list(TOKEN_PATTERN.finditer(text))


@lru_cache(maxsize=4)
def _embedding_tokenizer(snapshot_path: str) -> Any:
    # Two source lanes can first request the lazy Transformers import together.
    with _tokenizer_init_lock:
        try:
            from transformers import AutoTokenizer
        except ImportError as exc:
            raise NonRetryableFukamiruError(
                "Embedding tokenizer dependency is unavailable; install the 'rag' extra"
            ) from exc
        try:
            return AutoTokenizer.from_pretrained(
                snapshot_path,
                use_fast=True,
                local_files_only=True,
                token=False,
            )
        except (OSError, ValueError) as exc:
            raise NonRetryableFukamiruError(
                f"Embedding tokenizer could not be loaded from {snapshot_path}: {exc}"
            ) from exc


def preflight_embedding_tokenizer(settings: Settings) -> str:
    """Validate and load the pinned tokenizer without allowing Hub resolution."""

    cache_dir = settings.retrieval.embedding_cache_dir
    configure_huggingface_offline(cache_dir)
    snapshot = resolve_local_model_snapshot(
        cache_dir=cache_dir,
        model_name=settings.retrieval.embedding_model,
        revision=settings.retrieval.embedding_model_revision,
    )
    _embedding_tokenizer(str(snapshot))
    return str(snapshot)


def _token_offsets(
    text: str,
    model_name: str,
    *,
    use_embedding: bool,
    snapshot_path: str | None,
) -> tuple[list[tuple[int, int]], str]:
    if use_embedding:
        if not snapshot_path:
            raise NonRetryableFukamiruError(
                "Embedding tokenizer mode requires a resolved local model snapshot"
            )
        tokenizer = _embedding_tokenizer(snapshot_path)
        encoded = tokenizer(
            text,
            add_special_tokens=False,
            return_attention_mask=False,
            return_offsets_mapping=True,
            truncation=False,
        )
        offsets = [
            (int(start), int(end))
            for start, end in encoded["offset_mapping"]
            if int(end) > int(start)
        ]
        if offsets:
            return offsets, model_name
        raise EmptyEmbeddingOffsetsError(
            "Embedding tokenizer produced no offsets; Unicode fallback is disabled "
            "while retrieval.tokenizer_mode='embedding'."
        )
    return [(match.start(), match.end()) for match in _token_matches(text)], "unicode-fallback-v1"


@dataclass(frozen=True)
class TokenBoundedSlice:
    text: str
    token_count: int
    tokenizer_name: str
    start_char: int
    end_char: int


def split_token_bounded(
    text: str,
    model_name: str,
    *,
    max_tokens: int,
    use_embedding: bool,
    snapshot_path: str | None,
) -> list[TokenBoundedSlice]:
    """Retokenize each character slice; never trust parent token boundaries.

    Several subword tokens can share one character offset. Cutting their parent
    token array can therefore duplicate a character and exceed the token limit.
    Character ranges here form a contiguous partition of the original text.
    """
    from bisect import bisect_right

    if max_tokens < 1:
        raise ValueError("max_tokens must be positive")

    def measure(value: str) -> tuple[list[tuple[int, int]], str]:
        return _token_offsets(
            value, model_name, use_embedding=use_embedding, snapshot_path=snapshot_path
        )

    offsets, tokenizer_name = measure(text)
    if len(offsets) <= max_tokens:
        return [TokenBoundedSlice(text, len(offsets), tokenizer_name, 0, len(text))]
    ends = [end for _, end in offsets]
    result: list[TokenBoundedSlice] = []
    start = 0
    while start < len(text):
        first = bisect_right(ends, start)
        last = min(first + max_tokens, len(offsets)) - 1
        end = len(text) if last == len(offsets) - 1 else ends[last]
        if end <= start:
            raise NonRetryableFukamiruError("Tokenizer offsets made no character progress")
        while True:
            piece_offsets, tokenizer_name = measure(text[start:end])
            if len(piece_offsets) <= max_tokens:
                break
            # Shrink even when a shared offset points at the same final character.
            end = start + min(end - start - 1, piece_offsets[max_tokens - 1][1])
            if end <= start:
                raise NonRetryableFukamiruError(
                    "A single source character exceeds the retrieval token limit"
                )
        result.append(TokenBoundedSlice(
            text[start:end], len(piece_offsets), tokenizer_name, start, end
        ))
        start = end
    return result


@dataclass(frozen=True)
class LocatedSource:
    path: Path
    sha256: str
    byte_size: int
    media_type: str


class IngestionService:
    def __init__(self, connection: sqlite3.Connection, settings: Settings) -> None:
        self.connection = connection
        self.settings = settings

    def locate(self, source_path: str) -> LocatedSource:
        normalized = source_path.strip()
        if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in {'"', "'"}:
            normalized = normalized[1:-1].strip()
        path = Path(normalized).expanduser().resolve()
        if not path.is_file():
            raise FukamiruError(f"Source file does not exist: {path}")
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            supported = ", ".join(sorted(SUPPORTED_SUFFIXES))
            raise FukamiruError(f"Unsupported source type {path.suffix}; supported: {supported}")
        media_type = (
            "application/epub+zip"
            if path.suffix.lower() == ".epub"
            else mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        )
        return LocatedSource(path, _sha256_file(path), path.stat().st_size, media_type)

    @staticmethod
    def detect_language(text: str) -> str:
        return classify_language(text)[0]

    def has_reusable_source(self, source: LocatedSource) -> bool:
        asset = self.connection.execute(
            """
            SELECT a.id FROM source_assets a
            WHERE a.sha256 = ? AND a.ingestion_status = 'ready'
            AND EXISTS (SELECT 1 FROM document_units d WHERE d.source_asset_id = a.id)
            """,
            (source.sha256,),
        ).fetchone()
        if asset is None:
            return False
        if source.path.suffix.casefold() != ".pdf":
            return True
        layout = self.connection.execute(
            """
            SELECT analyzer_version, structured_html_path FROM pdf_layout_documents
            WHERE source_asset_id = ? ORDER BY created_at DESC LIMIT 1
            """,
            (asset["id"],),
        ).fetchone()
        return bool(
            layout is not None
            and layout["analyzer_version"] == ANALYZER_VERSION
            and layout["structured_html_path"]
            and Path(str(layout["structured_html_path"])).is_file()
        )

    def register(self, source: LocatedSource, source_language: str | None = None) -> str:
        now = utc_now()
        row = self.connection.execute(
            "SELECT id FROM source_assets WHERE sha256 = ?", (source.sha256,)
        ).fetchone()
        source_asset_id = str(row["id"]) if row else _uuid()
        language = None if source_language in {None, "", "auto"} else source_language
        self.connection.execute(
            """
            UPDATE source_locations
            SET is_primary = 0, is_available = 0
            WHERE path = ? AND source_asset_id <> ?
            """,
            (str(source.path), source_asset_id),
        )
        self.connection.execute(
            "UPDATE source_locations SET is_primary = 0 WHERE source_asset_id = ?",
            (source_asset_id,),
        )
        self.connection.execute(
            """
            INSERT INTO source_assets(
                id, sha256, byte_size, media_type, source_language,
                ingestion_status, managed_path, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'registered', ?, ?, ?)
            ON CONFLICT(sha256) DO UPDATE SET
                byte_size = excluded.byte_size,
                media_type = excluded.media_type,
                source_language = COALESCE(excluded.source_language, source_assets.source_language),
                managed_path = excluded.managed_path,
                updated_at = excluded.updated_at
            """,
            (
                source_asset_id,
                source.sha256,
                source.byte_size,
                source.media_type,
                language,
                str(source.path),
                now,
                now,
            ),
        )
        self.connection.execute(
            """
            INSERT INTO source_locations(
                source_asset_id, path, is_primary, first_seen_at, last_verified_at
            ) VALUES (?, ?, 1, ?, ?)
            ON CONFLICT(source_asset_id, path) DO UPDATE SET
                is_primary = 1,
                is_available = 1,
                last_verified_at = excluded.last_verified_at
            """,
            (source_asset_id, str(source.path), now, now),
        )
        self.connection.commit()
        return source_asset_id

    @staticmethod
    def _report_progress(
        progress: IngestionProgress | None,
        *,
        phase: str,
        label: str,
        stage: int,
        stage_total: int = 6,
        completed: int | None = None,
        total: int | None = None,
        unit: str | None = None,
        indeterminate: bool = False,
        **details: Any,
    ) -> None:
        if progress is None:
            return
        try:
            progress(
                {
                    "kind": "ingestion_activity",
                    "phase": phase,
                    "label": label,
                    "stage": stage,
                    "stage_total": stage_total,
                    "completed": completed,
                    "total": total,
                    "unit": unit,
                    "indeterminate": indeterminate,
                    "details": details,
                }
            )
        except Exception:
            # Progress reporting must never invalidate canonical ingestion work.
            return

    def extract(
        self,
        source_asset_id: str,
        path: Path,
        *,
        progress: IngestionProgress | None = None,
        supplied_pages: list[str] | None = None,
        ocr_metadata: dict[str, Any] | None = None,
        prepared_pdf: PreparedPDF | None = None,
    ) -> Path:
        if supplied_pages is not None:
            protected = self.connection.execute(
                """SELECT 1 FROM ocr_text_revisions r
                   LEFT JOIN ocr_blocks b ON b.revision_id=r.id
                   WHERE r.source_asset_id=? AND
                       (r.layer='human_corrected_ocr' OR b.is_human_protected=1)
                   LIMIT 1""", (source_asset_id,),
            ).fetchone()
            if protected:
                raise FukamiruError("人間が修正・保護したOCRは自動で置換できません")
        self.connection.execute(
            "UPDATE source_assets SET ingestion_status = 'extracting', updated_at = ? WHERE id = ?",
            (utc_now(), source_asset_id),
        )
        self.connection.commit()
        structured_pages: list[dict[str, Any]] | None = None
        layout: Any | None = None
        layout_document_id: str | None = None
        structured_html_path: Path | None = None
        structured_html: str | None = None
        source_blocks_by_page: list[list[dict[str, str]]] | None = None
        page_metadata: list[dict[str, str | None]] | None = None
        extracted_title: str | None = None
        extracted_language: str | None = None
        savepoint_active = False
        language_row = self.connection.execute(
            "SELECT source_language FROM source_assets WHERE id = ?", (source_asset_id,)
        ).fetchone()
        source_language = language_row["source_language"] if language_row else None
        try:
            if supplied_pages is not None:
                pages = supplied_pages
                if not pages or not any(page.strip() for page in pages):
                    raise FukamiruError("OCR結果に本文がありません")
                self._report_progress(
                    progress,
                    phase="ocr_result_loading",
                    label="言語別OCR結果を読み込みました",
                    stage=1,
                    completed=len(pages),
                    total=len(pages),
                    unit="pages",
                    engine=(ocr_metadata or {}).get("engine"),
                )
            elif path.suffix.lower() == ".pdf":
                self._report_progress(
                    progress,
                    phase="pdf_layout_analysis",
                    label="PDFレイアウトを解析中",
                    stage=1,
                    indeterminate=True,
                    source_path=str(path),
                )
                layout = PDFLayoutAnalyzer().analyze(
                    path, source_asset_id=source_asset_id, prepared=prepared_pdf
                )
                structured_pages = layout.serializable_pages()
                pages = [page.text for page in layout.pages]
                self._report_progress(
                    progress,
                    phase="pdf_layout_analysis",
                    label="PDFレイアウト解析完了",
                    stage=1,
                    completed=len(pages),
                    total=len(pages),
                    unit="pages",
                )
                destination = self.settings.paths.knowledge_artifacts_root / "structured_source"
                destination.mkdir(parents=True, exist_ok=True)
                structured_html_path = destination / f"{source_asset_id}.layout.html"
            elif path.suffix.lower() == ".epub":
                from fukamiru.knowledge.epub import EPUBExtractor

                epub = EPUBExtractor().extract(path)
                pages = [page.text for page in epub.pages]
                source_blocks_by_page = [
                    [{"text": block.text, "role": block.role} for block in page.blocks]
                    for page in epub.pages
                ]
                page_metadata = [
                    {"chapter_title": page.title, "resource_href": page.href}
                    for page in epub.pages
                ]
                extracted_title = epub.title
                extracted_language = epub.language
            else:
                pages = _extract_pages(path)
            if source_language in {None, "", "auto"}:
                detected_language = extracted_language or self.detect_language("\n".join(pages))
                if detected_language != "und":
                    source_language = detected_language
                    extracted_language = detected_language
                    self.connection.execute(
                        "UPDATE source_assets SET source_language = ?, updated_at = ? WHERE id = ?",
                        (source_language, utc_now(), source_asset_id),
                    )
                    self.connection.commit()
            if layout is not None:
                structured_html = PDFLayoutHTMLRenderer().render(
                    layout, title=path.stem, language=source_language
                )
            # Reserve the writer before OCR repositories read revision identities.
            # A deferred read snapshot cannot safely upgrade after another writer commits.
            if not self.connection.in_transaction:
                self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute("SAVEPOINT ocr_extract")
            savepoint_active = True
            self._report_progress(
                progress,
                phase="ocr_revision_persistence",
                label="OCR revisionと注記を保存中",
                stage=2,
                completed=0,
                total=len(pages),
                unit="pages",
            )
            if layout is not None and structured_html_path is not None:
                layout_document_id = PDFLayoutRepository(self.connection, autocommit=False).store(
                    layout,
                    structured_html_path=structured_html_path,
                )
            pipeline = OCRPipelineService(self.connection).process(
                source_asset_id=source_asset_id,
                pages=pages,
                language=source_language,
                structured_pages=structured_pages,
            )
            self._report_progress(
                progress,
                phase="ocr_revision_persistence",
                label="OCR revision保存完了",
                stage=2,
                completed=len(pages),
                total=len(pages),
                unit="pages",
            )
            if structured_html_path is not None and structured_html is not None:
                structured_html_path.write_text(structured_html, encoding="utf-8")
            destination = self.settings.paths.knowledge_artifacts_root / "normalized_text"
            destination.mkdir(parents=True, exist_ok=True)
            artifact = destination / f"{source_asset_id}.json"
            artifact.write_text(
                json.dumps(
                    {
                        "source_path": str(path),
                        "pages": list(pipeline.normalized_pages),
                        "raw_pages": pages,
                        "raw_ocr_revision_id": pipeline.raw_revision_id,
                        "normalized_ocr_revision_id": pipeline.normalized_revision_id,
                        "ocr_audit": pipeline.serializable_audit(),
                        "layout_document_id": layout_document_id,
                        "structured_html_path": (
                            str(structured_html_path) if structured_html_path else None
                        ),
                        "layout_pages": structured_pages,
                        "source_blocks_by_page": source_blocks_by_page,
                        "page_metadata": page_metadata,
                        "extracted_title": extracted_title,
                        "extracted_language": extracted_language,
                        "ocr_engine": (ocr_metadata or {}).get("engine"),
                        "ocr_source_json_path": (ocr_metadata or {}).get("json_path"),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            self.connection.execute("RELEASE SAVEPOINT ocr_extract")
            savepoint_active = False
            self.connection.commit()
            return artifact
        except Exception:
            if savepoint_active:
                self.connection.execute("ROLLBACK TO SAVEPOINT ocr_extract")
                self.connection.execute("RELEASE SAVEPOINT ocr_extract")
            self.connection.execute(
                "UPDATE source_assets SET ingestion_status = 'failed', updated_at = ? WHERE id = ?",
                (utc_now(), source_asset_id),
            )
            self.connection.commit()
            raise

    def infer_structure(
        self,
        source_asset_id: str,
        artifact_path: Path,
        *,
        title: str,
        language: str | None,
        material_role: str = "unknown",
        permission: str = "unknown",
        progress: IngestionProgress | None = None,
    ) -> str:
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        pages: list[str] = payload["pages"]
        source_blocks_by_page = payload.get("source_blocks_by_page") or []
        page_metadata = payload.get("page_metadata") or []
        layout_document_id = payload.get("layout_document_id")
        layout_blocks_by_page: dict[int, list[dict[str, Any]]] = {}
        normalized_revision_id = payload.get("normalized_ocr_revision_id")
        if layout_document_id and normalized_revision_id:
            revision = OCRRevisionRepository(self.connection).snapshot(str(normalized_revision_id))
            for block in revision["blocks"]:
                attributes = json.loads(block.get("attributes_json") or "{}")
                page_number = int(attributes.get("source_page_number") or 1)
                layout_blocks_by_page.setdefault(page_number, []).append(
                    {**block, "attributes": attributes}
                )
        now = utc_now()
        document_unit_id = _uuid()
        existing_unit_ids = [
            str(row["id"])
            for row in self.connection.execute(
                "SELECT id FROM document_units WHERE source_asset_id = ?",
                (source_asset_id,),
            )
        ]
        existing_spans = 0
        existing_chunks = 0
        if existing_unit_ids:
            placeholders = ",".join("?" for _ in existing_unit_ids)
            existing_spans = int(
                self.connection.execute(
                    f"SELECT COUNT(*) FROM source_spans WHERE document_unit_id IN ({placeholders})",  # noqa: S608
                    existing_unit_ids,
                ).fetchone()[0]
            )
            existing_chunks = int(
                self.connection.execute(
                    f"SELECT COUNT(*) FROM chunks WHERE document_unit_id IN ({placeholders})",  # noqa: S608
                    existing_unit_ids,
                ).fetchone()[0]
            )
        self._report_progress(
            progress,
            phase="replace_existing_structure",
            label="既存の文書構造を置換中（SQLite整合性確認）",
            stage=3,
            indeterminate=True,
            existing_document_units=len(existing_unit_ids),
            existing_source_spans=existing_spans,
            existing_chunks=existing_chunks,
        )
        self.connection.execute("BEGIN IMMEDIATE")
        delete_started = time.monotonic()
        last_delete_report = delete_started
        progress_callbacks = 0

        def report_delete_activity() -> int:
            nonlocal last_delete_report, progress_callbacks
            progress_callbacks += 1
            current = time.monotonic()
            if current - last_delete_report >= 5.0:
                self._report_progress(
                    progress,
                    phase="replace_existing_structure",
                    label="既存の文書構造を置換中（SQLite全表走査）",
                    stage=3,
                    indeterminate=True,
                    elapsed_seconds=round(current - delete_started, 1),
                    activity_count=progress_callbacks,
                    sqlite_vm_steps=progress_callbacks * 100_000,
                    existing_source_spans=existing_spans,
                    existing_chunks=existing_chunks,
                )
                last_delete_report = current
            return 0

        self.connection.set_progress_handler(report_delete_activity, 100_000)
        try:
            self.connection.execute(
                "DELETE FROM document_units WHERE source_asset_id = ?", (source_asset_id,)
            )
        finally:
            self.connection.set_progress_handler(None, 0)
        self._report_progress(
            progress,
            phase="replace_existing_structure",
            label="既存の文書構造を置換しました",
            stage=3,
            completed=existing_chunks,
            total=existing_chunks,
            unit="chunks",
            elapsed_seconds=round(time.monotonic() - delete_started, 1),
        )
        self.connection.execute(
            """
            INSERT INTO document_units(
                id, source_asset_id, unit_type, title, sequence_index, language,
                material_role, permission, structure_confidence, created_at, updated_at
            ) VALUES (?, ?, 'document', ?, 0, ?, ?, ?, ?, ?, ?)
            """,
            (
                document_unit_id,
                source_asset_id,
                title,
                language,
                material_role
                if material_role in {"primary", "secondary", "reference", "user_writing", "unknown"}
                else "unknown",
                permission
                if permission
                in {"quote", "paraphrase_only", "metadata_only", "private_reference", "unknown"}
                else "unknown",
                0.9 if layout_document_id else 0.8,
                now,
                now,
            ),
        )
        ordinal = 0
        structure_started = time.monotonic()
        last_structure_report = structure_started
        self._report_progress(
            progress,
            phase="rebuild_source_spans",
            label="ページとSourceSpanを再構築中",
            stage=4,
            completed=0,
            total=len(pages),
            unit="pages",
        )
        for page_number, page_text in enumerate(pages, start=1):
            page_id = _uuid()
            self.connection.execute(
                """
                INSERT INTO pages(
                    id, document_unit_id, page_number, printed_label, text, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    page_id,
                    document_unit_id,
                    page_number,
                    (page_metadata[page_number - 1].get("chapter_title")
                     if page_number <= len(page_metadata) else None),
                    page_text,
                    now,
                ),
            )
            cursor = 0
            structured_blocks = sorted(
                layout_blocks_by_page.get(page_number, []),
                key=lambda item: int(item["ordinal"]),
            )
            source_blocks = (
                [
                    {
                        "text": block.get("text", ""),
                        "attributes": {},
                        "block_type": block.get("role", "paragraph"),
                    }
                    for block in source_blocks_by_page[page_number - 1]
                ]
                if page_number <= len(source_blocks_by_page)
                else []
            )
            spans = structured_blocks or source_blocks or [
                {"text": paragraph, "attributes": {}, "block_type": "paragraph"}
                for paragraph in _paragraphs(page_text)
            ]
            for source_block in spans:
                paragraph = str(source_block["text"])
                attributes = source_block.get("attributes") or {}
                role = str(
                    attributes.get("structural_role")
                    or source_block.get("block_type")
                    or "paragraph"
                )
                if role in {"page_header", "page_footer", "page_number"}:
                    continue
                start = page_text.find(paragraph, cursor)
                if start < 0:
                    start = cursor
                end = start + len(paragraph)
                cursor = end
                self.connection.execute(
                    """
                    INSERT INTO source_spans(
                        id, document_unit_id, ordinal, start_page_id, end_page_id,
                        start_char, end_char, text, language,
                        reconstruction_method, confidence, created_at,
                        structural_role, layout_block_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'deterministic', ?, ?, ?, ?)
                    """,
                    (
                        _uuid(),
                        document_unit_id,
                        ordinal,
                        page_id,
                        page_id,
                        start,
                        end,
                        paragraph,
                        language,
                        0.9 if layout_document_id else 0.8,
                        now,
                        role,
                        attributes.get("layout_block_id"),
                    ),
                )
                ordinal += 1
            current = time.monotonic()
            if page_number == len(pages) or current - last_structure_report >= 5.0:
                self._report_progress(
                    progress,
                    phase="rebuild_source_spans",
                    label="ページとSourceSpanを再構築中",
                    stage=4,
                    completed=page_number,
                    total=len(pages),
                    unit="pages",
                    source_spans=ordinal,
                    elapsed_seconds=round(current - structure_started, 1),
                )
                last_structure_report = current
        if ordinal == 0:
            self.connection.rollback()
            raise FukamiruError("No SourceSpan could be reconstructed")
        source_row = self.connection.execute(
            "SELECT managed_path FROM source_assets WHERE id = ?", (source_asset_id,)
        ).fetchone()
        if source_row and source_row["managed_path"]:
            ensure_source_bibliography(
                self.connection,
                source_asset_id=source_asset_id,
                document_unit_id=document_unit_id,
                path=Path(str(source_row["managed_path"])),
                fallback_title=title,
                now=now,
            )
        self.connection.commit()
        return document_unit_id

    def build_chunks(
        self,
        document_unit_id: str,
        *,
        progress: IngestionProgress | None = None,
    ) -> int:
        rows = self.connection.execute(
            """SELECT id, ordinal, text FROM source_spans
            WHERE document_unit_id = ? ORDER BY ordinal""",
            (document_unit_id,),
        ).fetchall()
        if not rows:
            raise FukamiruError("DocumentUnit has no SourceSpan")
        use_embedding_tokenizer = self.settings.retrieval.tokenizer_mode == "embedding"
        tokenizer_snapshot: str | None = None
        if use_embedding_tokenizer:
            self._report_progress(
                progress, phase="prepare_tokenizer", label="検索用Tokenizerを準備中", stage=5,
                indeterminate=True,
            )
            tokenizer_snapshot = preflight_embedding_tokenizer(self.settings)
        now = utc_now()
        chunk_ordinal = 0
        prepared_chunks: list[tuple[Any, ...]] = []
        overlap = max(0, self.settings.retrieval.overlap_source_spans)
        hard_max = self.settings.retrieval.hard_max_tokens
        chunk_started = time.monotonic()
        last_chunk_report = chunk_started
        self._report_progress(
            progress,
            phase="build_chunks",
            label="検索用Chunkを再構築中",
            stage=5,
            completed=0,
            total=len(rows),
            unit="source_spans",
        )
        for index, row in enumerate(rows):
            primary = str(row["text"])
            try:
                offsets, tokenizer_name = _token_offsets(
                    primary,
                    self.settings.retrieval.embedding_model,
                    use_embedding=use_embedding_tokenizer,
                    snapshot_path=tokenizer_snapshot,
                )
            except EmptyEmbeddingOffsetsError:
                # EPUBs can contain standalone zero-width format markers. Keep
                # their SourceSpans, but do not create a tokenless retrieval chunk.
                if primary and all(
                    char.isspace() or unicodedata.category(char) == "Cf"
                    for char in primary
                ):
                    continue
                raise
            slices: list[tuple[str, int, str]]
            if len(offsets) > hard_max:
                slices = [
                    (part.text, part.token_count, part.tokenizer_name)
                    for part in split_token_bounded(
                        primary,
                        self.settings.retrieval.embedding_model,
                        max_tokens=hard_max,
                        use_embedding=use_embedding_tokenizer,
                        snapshot_path=tokenizer_snapshot,
                    )
                ]
            else:
                start = max(0, index - overlap)
                end = min(len(rows), index + overlap + 1)
                contextual = "\n\n".join(str(item["text"]) for item in rows[start:end])
                contextual_offsets, contextual_tokenizer = _token_offsets(
                    contextual,
                    self.settings.retrieval.embedding_model,
                    use_embedding=use_embedding_tokenizer,
                    snapshot_path=tokenizer_snapshot,
                )
                if len(contextual_offsets) <= hard_max:
                    slices = [(contextual, max(1, len(contextual_offsets)), contextual_tokenizer)]
                    tokenizer_name = contextual_tokenizer
                else:
                    slices = [(primary, max(1, len(offsets)), tokenizer_name)]
            for slice_text, token_count, slice_tokenizer in slices:
                previous_id = str(rows[index - 1]["id"]) if index else None
                next_id = str(rows[index + 1]["id"]) if index + 1 < len(rows) else None
                prepared_chunks.append(
                    (
                        _uuid(),
                        document_unit_id,
                        row["id"],
                        chunk_ordinal,
                        slice_text,
                        token_count,
                        slice_tokenizer or tokenizer_name,
                        previous_id,
                        next_id,
                        int(len(slices) > 1),
                        now,
                    ),
                )
                chunk_ordinal += 1
            current = time.monotonic()
            completed_spans = index + 1
            if completed_spans == len(rows) or current - last_chunk_report >= 5.0:
                self._report_progress(
                    progress,
                    phase="build_chunks",
                    label="検索用Chunkを再構築中",
                    stage=5,
                    completed=completed_spans,
                    total=len(rows),
                    unit="source_spans",
                    chunks_prepared=chunk_ordinal,
                    elapsed_seconds=round(current - chunk_started, 1),
                )
                last_chunk_report = current
        # Expensive tokenization above runs before acquiring the SQLite writer.
        # Replacing the document's chunks remains one atomic database operation.
        self.connection.execute("SAVEPOINT persist_ingestion_chunks")
        try:
            self.connection.execute(
                "DELETE FROM chunks WHERE document_unit_id = ?", (document_unit_id,)
            )
            self.connection.executemany(
                """
                INSERT INTO chunks(
                    id, document_unit_id, primary_source_span_id, ordinal, text,
                    token_count, tokenizer_name, previous_source_span_id,
                    next_source_span_id, is_retrieval_slice, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                prepared_chunks,
            )
        except Exception:
            self.connection.execute("ROLLBACK TO SAVEPOINT persist_ingestion_chunks")
            self.connection.execute("RELEASE SAVEPOINT persist_ingestion_chunks")
            raise
        self.connection.execute("RELEASE SAVEPOINT persist_ingestion_chunks")
        self.connection.commit()
        self._report_progress(
            progress,
            phase="persist_chunks",
            label="検索用Chunkを保存しました",
            stage=5,
            completed=len(rows),
            total=len(rows),
            unit="source_spans",
            chunks_created=chunk_ordinal,
        )
        return chunk_ordinal

    def index(
        self,
        source_asset_id: str,
        document_unit_id: str,
        *,
        progress: IngestionProgress | None = None,
    ) -> int:
        rows = self.connection.execute(
            "SELECT id, text FROM chunks WHERE document_unit_id = ? ORDER BY ordinal",
            (document_unit_id,),
        ).fetchall()
        self._report_progress(
            progress,
            phase="lexical_index",
            label="Lexical索引を更新中",
            stage=6,
            completed=0,
            total=len(rows),
            unit="chunks",
        )
        owns_transaction = not self.connection.in_transaction
        if owns_transaction:
            self.connection.execute("BEGIN IMMEDIATE")
        self.connection.execute("SAVEPOINT persist_lexical_index")
        try:
            old_rowids = self.connection.execute(
                "SELECT fts_rowid FROM chunk_fts_sources WHERE source_asset_id = ?",
                (source_asset_id,),
            ).fetchall()
            # Point lookups avoid scanning every FTS document's UNINDEXED metadata.
            self.connection.executemany(
                "DELETE FROM chunk_fts WHERE rowid = ?",
                [(row["fts_rowid"],) for row in old_rowids],
            )
            self.connection.execute(
                "DELETE FROM chunk_fts_sources WHERE source_asset_id = ?", (source_asset_id,)
            )
            next_rowid = int(self.connection.execute(
                "SELECT COALESCE(MAX(fts_rowid), 0) + 1 FROM chunk_fts_sources"
            ).fetchone()[0])
            self.connection.executemany(
                "INSERT INTO chunk_fts_sources(fts_rowid, source_asset_id) VALUES (?, ?)",
                [(next_rowid + index, source_asset_id) for index in range(len(rows))],
            )
            self.connection.executemany(
                """INSERT INTO chunk_fts(
                    rowid, chunk_id, document_unit_id, source_asset_id, text
                ) VALUES (?, ?, ?, ?, ?)""",
                [(next_rowid + index, row["id"], document_unit_id, source_asset_id, row["text"])
                 for index, row in enumerate(rows)],
            )
            self.connection.execute(
                "UPDATE source_assets SET ingestion_status = 'ready', updated_at = ? WHERE id = ?",
                (utc_now(), source_asset_id),
            )
        except Exception:
            self.connection.execute("ROLLBACK TO SAVEPOINT persist_lexical_index")
            self.connection.execute("RELEASE SAVEPOINT persist_lexical_index")
            if owns_transaction:
                self.connection.rollback()
            raise
        self.connection.execute("RELEASE SAVEPOINT persist_lexical_index")
        self.connection.commit()
        self._report_progress(
            progress,
            phase="completed",
            label="資料取り込み完了",
            stage=6,
            completed=len(rows),
            total=len(rows),
            unit="chunks",
        )
        return len(rows)

    def ingest_path(
        self,
        source_path: str,
        *,
        progress: IngestionProgress | None = None,
        located_source: LocatedSource | None = None,
        inspected_pdf_text_layer: PDFTextLayer | None = None,
        prepared_pdf: PreparedPDF | None = None,
        **metadata: Any,
    ) -> dict[str, Any]:
        source = located_source or self.locate(source_path)
        reusable = self.has_reusable_source(source)
        if source.path.suffix.casefold() == ".pdf":
            if not reusable and inspected_pdf_text_layer is None:
                prepared_pdf = prepared_pdf or PDFLayoutAnalyzer().prepare(source.path)
                inspected_pdf_text_layer = prepared_pdf.text_layer
            if inspected_pdf_text_layer is not None and not inspected_pdf_text_layer.usable:
                raise FukamiruError("OCR_REQUIRED: PDFに利用可能なテキスト層がありません。")
        source_asset_id = self.register(source, metadata.get("source_language"))
        ready = self.connection.execute(
            "SELECT ingestion_status FROM source_assets WHERE id = ?", (source_asset_id,)
        ).fetchone()
        existing = self.connection.execute(
            """SELECT id FROM document_units
            WHERE source_asset_id = ? ORDER BY sequence_index LIMIT 1""",
            (source_asset_id,),
        ).fetchone()
        layout = self.connection.execute(
            """
            SELECT id, structured_html_path FROM pdf_layout_documents
            WHERE source_asset_id = ? ORDER BY created_at DESC LIMIT 1
            """,
            (source_asset_id,),
        ).fetchone()
        layout_required = source.path.suffix.casefold() == ".pdf"
        if (
            reusable
            and ready
            and ready["ingestion_status"] == "ready"
            and existing
            and (not layout_required or layout is not None)
        ):
            bibliography = ensure_source_bibliography(
                self.connection,
                source_asset_id=source_asset_id,
                document_unit_id=str(existing["id"]),
                path=source.path,
                fallback_title=source.path.stem,
                now=utc_now(),
            )
            self.connection.commit()
            revision = self.connection.execute(
                """
                SELECT id, parent_revision_id FROM ocr_text_revisions
                WHERE source_asset_id = ? AND layer = 'normalized_ocr'
                ORDER BY created_at DESC LIMIT 1
                """,
                (source_asset_id,),
            ).fetchone()
            effective_revision = self.connection.execute(
                """
                SELECT id FROM ocr_text_revisions
                WHERE source_asset_id = ? AND layer = 'human_corrected_ocr'
                ORDER BY created_at DESC LIMIT 1
                """,
                (source_asset_id,),
            ).fetchone()
            audited_revision_id = (
                str(effective_revision["id"])
                if effective_revision is not None
                else str(revision["id"])
                if revision is not None
                else None
            )
            if audited_revision_id is not None:
                page_rows = self.connection.execute(
                    """
                    SELECT page_number, page_text FROM ocr_revision_pages
                    WHERE revision_id = ? ORDER BY page_number
                    """,
                    (audited_revision_id,),
                ).fetchall()
                audit_source = "persisted_normalized_ocr"
            else:
                page_rows = self.connection.execute(
                    """
                    SELECT page_number, text AS page_text FROM pages
                    WHERE document_unit_id = ? ORDER BY page_number
                    """,
                    (existing["id"],),
                ).fetchall()
                audit_source = "legacy_document_pages"
            audit = OCRIntegrityAuditor().audit(
                [AuditPage(int(row["page_number"]), str(row["page_text"])) for row in page_rows],
                language=metadata.get("source_language"),
            )
            return {
                "source_path": str(source.path),
                "source_asset_id": source_asset_id,
                "document_unit_id": str(existing["id"]),
                "raw_ocr_revision_id": (
                    str(revision["parent_revision_id"])
                    if revision is not None and revision["parent_revision_id"]
                    else None
                ),
                "normalized_ocr_revision_id": (
                    str(revision["id"]) if revision is not None else None
                ),
                "audited_ocr_revision_id": audited_revision_id,
                "ocr_audit": self._serialize_audit(audit),
                "ocr_audit_source": audit_source,
                "layout_document_id": str(layout["id"]) if layout else None,
                "structured_html_path": (
                    str(layout["structured_html_path"])
                    if layout and layout["structured_html_path"]
                    else None
                ),
                "reused": True,
                "bibliography": bibliography,
            }
        artifact = self.extract(
            source_asset_id, source.path, progress=progress, prepared_pdf=prepared_pdf
        )
        artifact_payload = json.loads(artifact.read_text(encoding="utf-8"))
        language = (
            str(metadata["source_language"])
            if metadata.get("source_language") not in {None, "", "auto"}
            else artifact_payload.get("extracted_language")
        )
        document_unit_id = self.infer_structure(
            source_asset_id,
            artifact,
            title=str(artifact_payload.get("extracted_title") or source.path.stem),
            language=language,
            material_role=str(metadata.get("material_role", "unknown")),
            permission=str(metadata.get("permission", "unknown")),
            progress=progress,
        )
        chunk_count = self.build_chunks(document_unit_id, progress=progress)
        self.index(source_asset_id, document_unit_id, progress=progress)
        return {
            "source_path": str(source.path),
            "source_asset_id": source_asset_id,
            "document_unit_id": document_unit_id,
            "normalized_text_path": str(artifact),
            "raw_ocr_revision_id": artifact_payload.get("raw_ocr_revision_id"),
            "normalized_ocr_revision_id": artifact_payload.get("normalized_ocr_revision_id"),
            "audited_ocr_revision_id": artifact_payload.get("normalized_ocr_revision_id"),
            "ocr_audit": artifact_payload.get("ocr_audit", {}),
            "ocr_audit_source": "new_ingestion",
            "layout_document_id": artifact_payload.get("layout_document_id"),
            "structured_html_path": artifact_payload.get("structured_html_path"),
            "epub_title": artifact_payload.get("extracted_title"),
            "epub_language": artifact_payload.get("extracted_language"),
            "chunk_count": chunk_count,
            "reused": False,
            "bibliography": source_bibliography(self.connection, source_asset_id),
        }

    def ingest_ocr_pages(
        self,
        source_path: str,
        pages: list[str],
        *,
        source_language: str,
        engine: str,
        json_path: str,
        material_role: str = "unknown",
        permission: str = "unknown",
        progress: IngestionProgress | None = None,
    ) -> dict[str, Any]:
        """Ingest page text while keeping the original PDF as the immutable source."""
        source = self.locate(source_path)
        source_asset_id = self.register(source, source_language)
        artifact = self.extract(
            source_asset_id,
            source.path,
            progress=progress,
            supplied_pages=pages,
            ocr_metadata={"engine": engine, "json_path": json_path},
        )
        document_unit_id = self.infer_structure(
            source_asset_id,
            artifact,
            title=source.path.stem,
            language=source_language,
            material_role=material_role,
            permission=permission,
            progress=progress,
        )
        chunk_count = self.build_chunks(document_unit_id, progress=progress)
        self.index(source_asset_id, document_unit_id, progress=progress)
        payload = json.loads(artifact.read_text(encoding="utf-8"))
        return {
            "source_path": str(source.path),
            "source_asset_id": source_asset_id,
            "document_unit_id": document_unit_id,
            "normalized_text_path": str(artifact),
            "raw_ocr_revision_id": payload.get("raw_ocr_revision_id"),
            "normalized_ocr_revision_id": payload.get("normalized_ocr_revision_id"),
            "audited_ocr_revision_id": payload.get("normalized_ocr_revision_id"),
            "ocr_audit": payload.get("ocr_audit", {}),
            "ocr_engine": engine,
            "chunk_count": chunk_count,
            "reused": False,
            "bibliography": source_bibliography(self.connection, source_asset_id),
        }

    @staticmethod
    def _serialize_audit(audit: Any) -> dict[str, Any]:
        return {
            "findings": [dataclasses.asdict(finding) for finding in audit.findings],
            "boundary_contexts": [
                dataclasses.asdict(context) for context in audit.boundary_contexts
            ],
            "applied_repairs": list(audit.applied_repairs),
            "has_critical": audit.has_critical,
        }

    def document_text(self, document_unit_id: str, range_spec: str | None = None) -> str:
        return "\n\n".join(
            str(segment["text"]) for segment in self.document_segments(document_unit_id, range_spec)
        )

    def document_segments(
        self, document_unit_id: str, range_spec: str | None = None
    ) -> list[dict[str, Any]]:
        params: list[Any] = [document_unit_id]
        page_clause = ""
        if range_spec:
            match = re.search(r"(?:pages?|p\.?|ページ)?\s*(\d+)\s*[–—-]\s*(\d+)", range_spec, re.I)
            if match:
                start, end = sorted((int(match.group(1)), int(match.group(2))))
                page_clause = " AND p.page_number BETWEEN ? AND ?"
                params.extend((start, end))
        rows = self.connection.execute(
            """
            SELECT s.id AS source_span_id, p.page_number AS start_page,
                   COALESCE(ep.page_number, p.page_number) AS end_page,
                   s.ordinal, s.text, s.structural_role, s.layout_block_id
            FROM source_spans s
            JOIN pages p ON p.id = s.start_page_id
            LEFT JOIN pages ep ON ep.id = s.end_page_id
            WHERE s.document_unit_id = ?"""
            + page_clause
            + " ORDER BY p.page_number, s.ordinal",
            params,
        ).fetchall()
        if not rows:
            raise FukamiruError("The selected DocumentUnit/range contains no text")
        return [dict(row) for row in rows]


class KnowledgeSearchService:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        semantic_index: Any | None = None,
        rrf_k: int = 60,
    ) -> None:
        self.connection = connection
        self.semantic_index = semantic_index
        self.rrf_k = rrf_k

    def lexical_search(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        query = query.strip()
        if not query:
            return []
        if len(query) < 3:
            rows = self.connection.execute(
                """
                SELECT f.chunk_id, f.document_unit_id, f.source_asset_id, f.text,
                       0.0 AS score, d.title, l.path
                FROM chunk_fts f
                JOIN document_units d ON d.id = f.document_unit_id
                JOIN source_locations l
                  ON l.source_asset_id = f.source_asset_id
                 AND l.is_primary = 1 AND l.is_available = 1
                WHERE f.text LIKE ? LIMIT ?
                """,
                (f"%{query}%", limit),
            ).fetchall()
        else:
            terms = re.findall(r'"([^"\n]+)"|『([^』]+)』|([^\s、,;；。]+)', query)
            words = list(dict.fromkeys(next(part for part in match if part) for match in terms))
            phrase = " OR ".join('"' + word.replace('"', '""') + '"' for word in words)
            rows = self.connection.execute(
                """
                SELECT f.chunk_id, f.document_unit_id, f.source_asset_id, f.text,
                       bm25(chunk_fts) AS score, d.title, l.path
                FROM chunk_fts f
                JOIN document_units d ON d.id = f.document_unit_id
                JOIN source_locations l
                  ON l.source_asset_id = f.source_asset_id
                 AND l.is_primary = 1 AND l.is_available = 1
                WHERE chunk_fts MATCH ? ORDER BY score LIMIT ?
                """,
                (phrase, limit),
            ).fetchall()
        return self._hydrate_results(
            [
                {
                    "chunk_id": row["chunk_id"],
                    "document_unit_id": row["document_unit_id"],
                    "source_asset_id": row["source_asset_id"],
                    "title": row["title"],
                    "path": row["path"],
                    "text": row["text"],
                    "score": row["score"],
                    "retrieval": "lexical",
                }
                for row in rows
            ]
        )

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        semantic_index_version_id: str | None = None,
        metadata_filters: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        query = query.strip()
        if not query:
            return []
        candidate_limit = max(limit * (10 if metadata_filters else 3), 20)
        lexical = self.lexical_search(query, limit=candidate_limit)
        semantic: list[dict[str, Any]] = []
        if self.semantic_index is not None and self.semantic_index.available():
            semantic = self.semantic_index.search(
                query,
                limit=candidate_limit,
                index_version_id=semantic_index_version_id,
            )
            semantic = self._hydrate_results(semantic)
        lexical = self._filter_results(lexical, metadata_filters)
        semantic = self._filter_results(semantic, metadata_filters)
        if not semantic:
            return lexical[:limit]
        if not lexical:
            return semantic[:limit]

        combined: dict[str, dict[str, Any]] = {}
        for retrieval, results in (("lexical", lexical), ("semantic", semantic)):
            for rank, result in enumerate(results, start=1):
                chunk_id = str(result["chunk_id"])
                item = combined.setdefault(
                    chunk_id,
                    {
                        **result,
                        "score": 0.0,
                        "retrieval": retrieval,
                    },
                )
                if retrieval == "semantic" and result.get("source_span_ids"):
                    item["text"] = result["text"]
                    item["citation"] = result["citation"]
                    item["source_span_ids"] = result["source_span_ids"]
                    item["source_span_id"] = result["source_span_id"]
                    for key in ("source_ranges", "end_source_span_id"):
                        if key in result:
                            item[key] = result[key]
                item["score"] += 1.0 / (self.rrf_k + rank)
                item[f"{retrieval}_score"] = result.get("score")
                if item["retrieval"] != retrieval:
                    item["retrieval"] = "hybrid"
        return sorted(
            combined.values(),
            key=lambda item: (-float(item["score"]), str(item["chunk_id"])),
        )[:limit]

    def _hydrate_results(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        chunk_ids = list(dict.fromkeys(str(item["chunk_id"]) for item in results))
        if not chunk_ids:
            return []
        placeholders = ",".join("?" for _ in chunk_ids)
        rows = self.connection.execute(
            f"""
            SELECT c.id AS chunk_id, c.primary_source_span_id, u.source_asset_id,
                   c.previous_source_span_id, c.next_source_span_id,
                   c.text AS chunk_text, c.is_retrieval_slice,
                   u.id AS document_unit_id, u.unit_type, u.title, u.language,
                   u.material_role, u.permission, l.path,
                   s.start_char, s.end_char, s.reconstruction_method,
                   sp.page_number AS start_page, ep.page_number AS end_page,
                   sp.printed_label AS start_label, ep.printed_label AS end_label
            FROM chunks c
            JOIN document_units u ON u.id = c.document_unit_id
            JOIN source_spans s ON s.id = c.primary_source_span_id
            LEFT JOIN pages sp ON sp.id = s.start_page_id
            LEFT JOIN pages ep ON ep.id = s.end_page_id
            LEFT JOIN source_locations l
              ON l.source_asset_id = u.source_asset_id
             AND l.is_primary = 1 AND l.is_available = 1
            WHERE c.id IN ({placeholders})
            """,
            chunk_ids,
        ).fetchall()
        metadata = {str(row["chunk_id"]): row for row in rows}
        # Semantic groups already carry their complete canonical span mapping.
        # Only their boundaries need hydration; fetching every interior span is
        # especially expensive for OCR that produced many one-character spans.
        span_ids = set()
        for result in results:
            mapped = result.get("source_span_ids") or []
            if mapped:
                span_ids.update((str(mapped[0]), str(mapped[-1])))
        span_ids.update(str(row[key]) for row in rows for key in (
            "primary_source_span_id", "previous_source_span_id", "next_source_span_id"
        ) if row[key])
        spans: dict[str, Any] = {}
        if span_ids:
            span_placeholders = ",".join("?" for _ in span_ids)
            spans = {
                str(row["id"]): row
                for row in self.connection.execute(
                    f"""
                    SELECT s.id, s.text, s.start_char, s.end_char, s.reconstruction_method,
                           sp.page_number AS start_page, sp.printed_label AS start_label,
                           ep.page_number AS end_page, ep.printed_label AS end_label
                    FROM source_spans s
                    LEFT JOIN pages sp ON sp.id = s.start_page_id
                    LEFT JOIN pages ep ON ep.id = s.end_page_id
                    WHERE s.id IN ({span_placeholders})
                    """,  # noqa: S608 - generated placeholders only
                    list(span_ids),
                )
            }
        bibliographies = {
            asset_id: source_bibliography(self.connection, asset_id)
            for asset_id in {str(row["source_asset_id"]) for row in rows}
        }
        hydrated: list[dict[str, Any]] = []
        for result in results:
            row = metadata.get(str(result["chunk_id"]))
            if row is None:
                continue
            primary_id = str(row["primary_source_span_id"])
            selected_ids = [str(value) for value in result.get("source_span_ids") or []]
            result_text = str(result.get("text") or "")
            relative_start = 0
            relative_end = None
            if not selected_ids:
                primary_text = str(spans[primary_id]["text"])
                if row["is_retrieval_slice"] and result_text in primary_text:
                    selected_ids = [primary_id]
                    relative_start = primary_text.index(result_text)
                    relative_end = relative_start + len(result_text)
                else:
                    # Legacy lexical chunks can include surrounding spans. Map
                    # that actual text, rather than attributing it all to the primary.
                    previous = str(row["previous_source_span_id"] or "")
                    following = str(row["next_source_span_id"] or "")
                    for ids in ([previous, primary_id, following], [previous, primary_id],
                                [primary_id, following], [primary_id]):
                        ids = [value for value in ids if value in spans]
                        if "\n\n".join(str(spans[value]["text"]) for value in ids) == result_text:
                            selected_ids = ids
                            break
                    if not selected_ids:
                        # A historical overlap policy may be unknown. Return the
                        # canonical primary, never an incorrectly located quote.
                        selected_ids = [primary_id]
                        result_text = primary_text
            if selected_ids[0] not in spans or selected_ids[-1] not in spans:
                continue
            start_span = spans[selected_ids[0]]
            end_span = spans[selected_ids[-1]]
            ranges = result.get("source_ranges") or []
            if ranges:
                relative_start = int(ranges[0][1])
                relative_end = int(ranges[-1][2])
            start_char = (start_span["start_char"] or 0) + relative_start
            end_char = ((end_span["start_char"] or 0) + relative_end
                        if relative_end is not None else end_span["end_char"])
            start_page = start_span["start_page"]
            end_page = end_span["end_page"]
            start_label = start_span["start_label"] or start_page
            end_label = end_span["end_label"] or end_page
            locator = ("" if start_label is None else f"p. {start_label}"
                       if start_label == end_label else f"pp. {start_label}–{end_label}")
            item = {
                **result,
                "text": result_text,
                "source_span_id": selected_ids[0],
                "source_span_ids": selected_ids,
                "document_unit_id": str(row["document_unit_id"]),
                "source_asset_id": str(row["source_asset_id"]),
                "unit_type": str(row["unit_type"]),
                "title": row["title"],
                "language": row["language"],
                "material_role": str(row["material_role"]),
                "permission": str(row["permission"]),
                "path": row["path"],
                "bibliography": bibliographies[str(row["source_asset_id"])],
                "citation": {
                    "source_span_id": selected_ids[0],
                    "source_asset_id": str(row["source_asset_id"]),
                    "document_unit_id": str(row["document_unit_id"]),
                    "start_page": start_page,
                    "end_page": end_page,
                    "start_label": start_span["start_label"],
                    "end_label": end_span["end_label"],
                    "start_char": start_char,
                    "end_char": end_char,
                    "locator": locator,
                    "source_span_ids": selected_ids,
                    "reconstruction_method": str(start_span["reconstruction_method"]),
                    "bibliographic_record_id": (
                        bibliographies[str(row["source_asset_id"])]["id"]
                        if bibliographies[str(row["source_asset_id"])]
                        else None
                    ),
                },
            }
            hydrated.append(item)
        return hydrated

    @staticmethod
    def _filter_results(
        results: list[dict[str, Any]], filters: dict[str, str] | None
    ) -> list[dict[str, Any]]:
        if not filters:
            return results
        allowed = {
            "source_asset_id",
            "document_unit_id",
            "unit_type",
            "language",
            "material_role",
            "permission",
        }
        unknown = set(filters) - allowed
        if unknown:
            raise FukamiruError("Unsupported metadata filters: " + ", ".join(sorted(unknown)))
        normalized = {
            key: str(value)
            for key, value in filters.items()
            if value is not None and str(value).strip()
        }
        return [
            item
            for item in results
            if all(str(item.get(key) or "") == value for key, value in normalized.items())
        ]
