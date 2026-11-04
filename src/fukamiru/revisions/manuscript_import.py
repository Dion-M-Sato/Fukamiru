"""Non-destructive manuscript extraction and canonical import previews."""

from __future__ import annotations

import hashlib
import html
import io
import json
import os
import re
import sqlite3
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from xml.etree import ElementTree as ET

from pypdf import PdfReader

from fukamiru.documents.canonical_html import CanonicalHTMLValidator
from fukamiru.errors import FukamiruError
from fukamiru.projects.git_checkpoint import GitCheckpointError, GitCheckpointService
from fukamiru.revisions.service import RevisionService, utc_now

SourceFormat = Literal["docx", "pdf", "txt", "markdown"]
DocumentKind = Literal["paper", "dissertation", "translation", "note", "appendix", "supplement"]

_WORD_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_WORD = {"w": _WORD_NAMESPACE}
_WORD_VAL = f"{{{_WORD_NAMESPACE}}}val"
_WORD_ID = f"{{{_WORD_NAMESPACE}}}id"
_HEADING_STYLE = re.compile(r"^heading\s*([1-6])$", re.IGNORECASE)
_MARKDOWN_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_MARKDOWN_LIST = re.compile(r"^\s*(?:([-+*])|(\d+)[.)])\s+(.+)$")
_XML_INVALID = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


@dataclass(frozen=True)
class ExtractedBlock:
    kind: Literal["heading", "paragraph", "blockquote", "list", "code"]
    text: str = ""
    level: int = 1
    ordered: bool = False
    items: tuple[str, ...] = ()
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ExtractionResult:
    blocks: tuple[ExtractedBlock, ...]
    metadata: dict[str, Any]
    warnings: tuple[str, ...] = ()


class ManuscriptImportService:
    """Preview DOCX/PDF/plain-text/Markdown as canonical human prose."""

    SUPPORTED_SUFFIXES: dict[str, SourceFormat] = {
        ".docx": "docx",
        ".pdf": "pdf",
        ".txt": "txt",
        ".md": "markdown",
        ".markdown": "markdown",
    }
    DOCUMENT_KINDS = {
        "paper",
        "dissertation",
        "translation",
        "note",
        "appendix",
        "supplement",
    }
    MAX_PROJECT_SOURCES = 5000

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.validator = CanonicalHTMLValidator()

    def preview(
        self,
        *,
        document_id: str,
        source_path: Path,
        max_bytes: int = 100 * 1024 * 1024,
    ) -> dict[str, Any]:
        document = self.connection.execute(
            """
            SELECT d.id, d.project_id, d.title, p.root_path
            FROM content_documents d JOIN projects p ON p.id = d.project_id
            WHERE d.id = ?
            """,
            (document_id,),
        ).fetchone()
        if document is None:
            raise FukamiruError(f"Unknown document: {document_id}")
        (
            source_path,
            source_format,
            source_size,
            source_sha256,
            extraction,
        ) = self._read_source(source_path, max_bytes=max_bytes, require_absolute=True)
        canonical_html = _CanonicalManuscriptBuilder(
            document_id=document_id,
            source_sha256=source_sha256,
            fallback_title=str(document["title"]),
        ).build(extraction.blocks)
        validation = self._validation(canonical_html)
        if not validation["valid"]:
            raise FukamiruError("Generated manuscript preview is not valid canonical HTML")

        preview_id = self._insert_preview(
            project_id=str(document["project_id"]),
            document_id=document_id,
            source_path=source_path,
            source_format=source_format,
            source_size=source_size,
            source_sha256=source_sha256,
            extraction=extraction,
            canonical_html=canonical_html,
            validation=validation,
        )
        self.connection.commit()
        return self.get(preview_id)

    def list_project_sources(self, project_id: str) -> dict[str, Any]:
        """List supported manuscripts physically contained by an active Project."""
        project = self._project(project_id)
        root = Path(str(project["root_path"])).resolve()
        if not root.is_dir():
            raise FukamiruError(f"Project root is unavailable: {root}")
        sources: list[dict[str, Any]] = []
        truncated = False
        for directory, directory_names, file_names in os.walk(root, followlinks=False):
            directory_names[:] = [name for name in directory_names if name != ".git"]
            base = Path(directory)
            for name in file_names:
                source_format = self.SUPPORTED_SUFFIXES.get(Path(name).suffix.casefold())
                if source_format is None:
                    continue
                path = (base / name).resolve()
                try:
                    relative = path.relative_to(root)
                    size = path.stat().st_size
                except (OSError, ValueError):
                    continue
                sources.append(
                    {
                        "relative_path": str(relative),
                        "name": name,
                        "source_format": source_format,
                        "size": size,
                    }
                )
                if len(sources) >= self.MAX_PROJECT_SOURCES:
                    truncated = True
                    break
            if truncated:
                break
        sources.sort(key=lambda item: str(item["relative_path"]).casefold())
        return {
            "project_id": project_id,
            "root_path": str(root),
            "files": sources,
            "truncated": truncated,
        }

    def preview_project_manuscript(
        self,
        *,
        project_id: str,
        source_path: Path,
        title: str,
        document_kind: DocumentKind,
        max_bytes: int = 100 * 1024 * 1024,
    ) -> dict[str, Any]:
        """Create the Content Document and its pending Preview atomically."""
        project = self._project(project_id)
        title = title.strip()
        if not title:
            raise FukamiruError("Manuscript title is required")
        if document_kind not in self.DOCUMENT_KINDS:
            raise FukamiruError(f"Unsupported Content Document kind: {document_kind}")
        project_root = Path(str(project["root_path"])).resolve()
        resolved_source = self._resolve_project_source(project_root, source_path)
        (
            resolved_source,
            source_format,
            source_size,
            source_sha256,
            extraction,
        ) = self._read_source(resolved_source, max_bytes=max_bytes, require_absolute=True)
        document_id = str(uuid.uuid4())
        canonical_html = _CanonicalManuscriptBuilder(
            document_id=document_id,
            source_sha256=source_sha256,
            fallback_title=title,
        ).build(extraction.blocks)
        validation = self._validation(canonical_html)
        if not validation["valid"]:
            raise FukamiruError("Generated manuscript preview is not valid canonical HTML")
        preview_id = str(uuid.uuid4())
        now = utc_now()
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute(
                """
                INSERT INTO content_documents(
                    id, project_id, kind, title, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (document_id, project_id, document_kind, title, now, now),
            )
            self._insert_preview(
                preview_id=preview_id,
                project_id=project_id,
                document_id=document_id,
                source_path=resolved_source,
                source_format=source_format,
                source_size=source_size,
                source_sha256=source_sha256,
                extraction=extraction,
                canonical_html=canonical_html,
                validation=validation,
                created_at=now,
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return self.get(preview_id)

    def update_proposed(self, preview_id: str, *, canonical_html: str) -> dict[str, Any]:
        preview = self._row(preview_id)
        if preview["status"] != "pending":
            raise FukamiruError("Only a pending Manuscript Import Preview can be edited")
        validation = self._validation(canonical_html)
        self.connection.execute(
            """
            UPDATE manuscript_import_previews
            SET proposed_html = ?, validation_json = ? WHERE id = ?
            """,
            (canonical_html, json.dumps(validation, ensure_ascii=False), preview_id),
        )
        self.connection.commit()
        return self.get(preview_id)

    def confirm(self, preview_id: str) -> dict[str, Any]:
        preview = self._row(preview_id)
        if preview["status"] != "pending":
            raise FukamiruError("Manuscript Import Preview is no longer pending")
        if not self._source_unchanged(dict(preview)):
            raise FukamiruError(
                "The source manuscript changed after preview; create a new preview before import"
            )
        canonical_html = str(preview["proposed_html"])
        self.validator.assert_valid(canonical_html)
        result = RevisionService(self.connection).commit_revision(
            document_id=str(preview["document_id"]),
            canonical_html=canonical_html,
            editor_type="human",
            operation="human_import",
            message=(f"Import {preview['source_format']} manuscript preview {preview_id}"),
        )
        project = self.connection.execute(
            "SELECT root_path FROM projects WHERE id = ?", (preview["project_id"],)
        ).fetchone()
        assert project is not None
        project_root = Path(str(project["root_path"])).resolve()
        artifact = (
            project_root / "documents" / str(preview["document_id"]) / f"{result.revision_id}.html"
        )
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(canonical_html, encoding="utf-8")
        checkpoint_sha: str | None = None
        status = "imported"
        error_text: str | None = None
        try:
            checkpoint = GitCheckpointService(project_root).checkpoint(
                [artifact], f"Import manuscript revision {result.revision_id}"
            )
            checkpoint_sha = checkpoint.commit_sha if checkpoint else None
        except GitCheckpointError as exc:
            status = "checkpoint_failed"
            error_text = str(exc)
        self.connection.execute(
            """
            UPDATE manuscript_import_previews
            SET status = ?, imported_revision_id = ?, artifact_path = ?,
                checkpoint_sha = ?, error_text = ?, resolved_at = ?
            WHERE id = ?
            """,
            (
                status,
                result.revision_id,
                str(artifact),
                checkpoint_sha,
                error_text,
                utc_now(),
                preview_id,
            ),
        )
        self.connection.commit()
        return self.get(preview_id)

    def get(self, preview_id: str) -> dict[str, Any]:
        result = dict(self._row(preview_id))
        result["extraction"] = json.loads(result.pop("extraction_json"))
        result["validation"] = json.loads(result.pop("validation_json"))
        result["original_unchanged"] = self._source_unchanged(result)
        return result

    def _row(self, preview_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            """
            SELECT i.*, d.title AS document_title, d.kind AS document_kind,
                   p.name AS project_name, p.root_path AS project_root
            FROM manuscript_import_previews i
            JOIN content_documents d ON d.id = i.document_id
            JOIN projects p ON p.id = i.project_id
            WHERE i.id = ?
            """,
            (preview_id,),
        ).fetchone()
        if row is None:
            raise FukamiruError(f"Manuscript Import Preview not found: {preview_id}")
        return row

    def _extract(self, source_format: SourceFormat, source_bytes: bytes) -> ExtractionResult:
        if source_format == "docx":
            return _extract_docx(source_bytes)
        if source_format == "pdf":
            return _extract_pdf(source_bytes)
        text, encoding = _decode_text(source_bytes)
        if source_format == "markdown":
            result = _extract_markdown(text)
            return ExtractionResult(
                result.blocks,
                {**result.metadata, "encoding": encoding},
                result.warnings,
            )
        return ExtractionResult(
            tuple(ExtractedBlock("paragraph", paragraph) for paragraph in _text_paragraphs(text)),
            {"encoding": encoding},
        )

    def _project(self, project_id: str) -> sqlite3.Row:
        project = self.connection.execute(
            """
            SELECT id, root_path FROM projects
            WHERE id = ? AND archived_at IS NULL
            """,
            (project_id,),
        ).fetchone()
        if project is None:
            raise FukamiruError(f"Active Project not found: {project_id}")
        return project

    @staticmethod
    def _resolve_project_source(project_root: Path, source_path: Path) -> Path:
        candidate = source_path if source_path.is_absolute() else project_root / source_path
        resolved = candidate.resolve()
        try:
            resolved.relative_to(project_root)
        except ValueError as exc:
            raise FukamiruError(
                "Manuscript source must be inside the selected Project root"
            ) from exc
        return resolved

    def _read_source(
        self,
        source_path: Path,
        *,
        max_bytes: int,
        require_absolute: bool,
    ) -> tuple[Path, SourceFormat, int, str, ExtractionResult]:
        if require_absolute and not source_path.is_absolute():
            raise FukamiruError("Manuscript import source must be an absolute path")
        source_path = source_path.resolve()
        suffix = source_path.suffix.casefold()
        if suffix == ".doc":
            raise FukamiruError(
                "Legacy Word .doc is not supported; save it as .docx or PDF first"
            )
        source_format = self.SUPPORTED_SUFFIXES.get(suffix)
        if source_format is None:
            supported = ", ".join(sorted(self.SUPPORTED_SUFFIXES))
            raise FukamiruError(f"Unsupported manuscript format; use {supported}")
        try:
            source_size = source_path.stat().st_size
            if not source_path.is_file():
                raise OSError("path is not a file")
            if source_size > max_bytes:
                raise FukamiruError("Manuscript source exceeds the 100 MiB preview limit")
            source_bytes = source_path.read_bytes()
        except FukamiruError:
            raise
        except OSError as exc:
            raise FukamiruError(f"Manuscript import source is unavailable: {exc}") from exc
        extraction = self._extract(source_format, source_bytes)
        if not extraction.blocks:
            raise FukamiruError("The manuscript contains no importable text")
        return (
            source_path,
            source_format,
            source_size,
            hashlib.sha256(source_bytes).hexdigest(),
            extraction,
        )

    def _insert_preview(
        self,
        *,
        project_id: str,
        document_id: str,
        source_path: Path,
        source_format: SourceFormat,
        source_size: int,
        source_sha256: str,
        extraction: ExtractionResult,
        canonical_html: str,
        validation: dict[str, Any],
        preview_id: str | None = None,
        created_at: str | None = None,
    ) -> str:
        preview_id = preview_id or str(uuid.uuid4())
        extraction_payload = {
            "format": source_format,
            "block_count": len(extraction.blocks),
            "warnings": list(extraction.warnings),
            **extraction.metadata,
        }
        self.connection.execute(
            """
            INSERT INTO manuscript_import_previews(
                id, project_id, document_id, source_path, source_format,
                source_sha256, source_size, proposed_html, extraction_json,
                validation_json, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                preview_id,
                project_id,
                document_id,
                str(source_path),
                source_format,
                source_sha256,
                source_size,
                canonical_html,
                json.dumps(extraction_payload, ensure_ascii=False),
                json.dumps(validation, ensure_ascii=False),
                created_at or utc_now(),
            ),
        )
        return preview_id

    def _validation(self, canonical_html: str) -> dict[str, Any]:
        report = self.validator.validate(canonical_html)
        return {
            "valid": report.valid,
            "element_count": report.element_count,
            "block_count": report.block_count,
            "issues": [
                {
                    "code": issue.code,
                    "message": issue.message,
                    "severity": issue.severity,
                    "element_id": issue.element_id,
                }
                for issue in report.issues
            ],
        }

    @staticmethod
    def _source_unchanged(preview: dict[str, Any]) -> bool | None:
        source = Path(str(preview["source_path"]))
        try:
            return hashlib.sha256(source.read_bytes()).hexdigest() == preview["source_sha256"]
        except OSError:
            return None


class _CanonicalManuscriptBuilder:
    def __init__(self, *, document_id: str, source_sha256: str, fallback_title: str) -> None:
        token = hashlib.sha256(f"{document_id}:{source_sha256}".encode()).hexdigest()[:12].upper()
        self.token = token
        self.fallback_title = fallback_title.strip() or "Imported manuscript"
        self.counter = 0

    def build(self, blocks: tuple[ExtractedBlock, ...]) -> str:
        lines = [f'<article id="DOC-MANUSCRIPT-{self.token}" lang="und">']
        section_depths: list[int] = []
        content_started = False
        for block in blocks:
            if block.kind == "heading":
                source_level = max(1, min(6, block.level))
                target_depth = min(source_level, len(section_depths) + 1, 5)
                while len(section_depths) >= target_depth:
                    lines.append("</section>")
                    section_depths.pop()
                while len(section_depths) < target_depth - 1:
                    self._open_section(
                        lines,
                        section_depths,
                        "Untitled section",
                        {"data-generated-heading": "true"},
                    )
                self._open_section(lines, section_depths, block.text, block.attributes)
                content_started = True
                continue
            if not content_started:
                self._open_section(lines, section_depths, self.fallback_title, {})
                content_started = True
            self._append_content(lines, block)
        if not content_started:
            self._open_section(lines, section_depths, self.fallback_title, {})
        while section_depths:
            lines.append("</section>")
            section_depths.pop()
        lines.append("</article>")
        return "\n".join(lines)

    def _open_section(
        self,
        lines: list[str],
        section_depths: list[int],
        title: str,
        attributes: dict[str, str],
    ) -> None:
        section_id = self._id("SEC")
        heading_id = self._id("H")
        attr_text = self._attributes(attributes)
        depth = len(section_depths) + 1
        heading_level = min(depth + 1, 6)
        lines.append(f'<section id="{section_id}"{attr_text}>')
        lines.append(
            f'<h{heading_level} id="{heading_id}">'
            f"{_escape_xml_text(title.strip())}</h{heading_level}>"
        )
        section_depths.append(depth)

    def _append_content(self, lines: list[str], block: ExtractedBlock) -> None:
        attr_text = self._attributes(block.attributes)
        if block.kind == "list":
            tag = "ol" if block.ordered else "ul"
            lines.append(f'<{tag} id="{self._id("L")}"{attr_text}>')
            for item in block.items:
                lines.append(f'<li id="{self._id("LI")}">{_escape_xml_text(item)}</li>')
            lines.append(f"</{tag}>")
            return
        tag = "blockquote" if block.kind == "blockquote" else "p"
        class_text = ' class="imported-code"' if block.kind == "code" else ""
        body = _escape_xml_text(block.text)
        if block.kind == "code":
            body = f"<code>{body}</code>"
        lines.append(f'<{tag} id="{self._id("B")}"{class_text}{attr_text}>{body}</{tag}>')

    def _id(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}-MANUSCRIPT-{self.token}-{self.counter:06d}"

    @staticmethod
    def _attributes(attributes: dict[str, str]) -> str:
        return "".join(
            f' {html.escape(name, quote=True)}="{_escape_xml_text(value, quote=True)}"'
            for name, value in sorted(attributes.items())
            if name.startswith("data-")
        )


def _decode_text(source_bytes: bytes) -> tuple[str, str]:
    candidates = (
        ("utf-8-sig", "UTF-8"),
        ("utf-16", "UTF-16"),
        ("cp932", "CP932"),
        ("cp1252", "Windows-1252"),
    )
    for codec, label in candidates:
        try:
            return source_bytes.decode(codec), label
        except (UnicodeDecodeError, UnicodeError):
            continue
    raise FukamiruError(
        "Text manuscript encoding is unsupported; use UTF-8, UTF-16, CP932, or Windows-1252"
    )


def _escape_xml_text(value: str, *, quote: bool = False) -> str:
    return html.escape(_XML_INVALID.sub("\ufffd", value), quote=quote)


def _text_paragraphs(text: str) -> list[str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs: list[str] = []
    for group in re.split(r"\n\s*\n+", normalized):
        value = " ".join(line.strip() for line in group.splitlines() if line.strip())
        if value:
            paragraphs.append(value)
    return paragraphs


def _extract_markdown(text: str) -> ExtractionResult:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[ExtractedBlock] = []
    warnings: list[str] = []
    index = 0
    if lines and lines[0].strip() == "---":
        try:
            closing = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
        except StopIteration:
            closing = -1
        if closing > 0:
            warnings.append("Markdown front matter was excluded from prose")
            index = closing + 1
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append(ExtractedBlock("paragraph", " ".join(paragraph).strip()))
            paragraph.clear()

    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()
        heading = _MARKDOWN_HEADING.match(stripped)
        list_match = _MARKDOWN_LIST.match(raw)
        if not stripped:
            flush_paragraph()
            index += 1
            continue
        if stripped.startswith("```") or stripped.startswith("~~~"):
            flush_paragraph()
            marker = stripped[:3]
            code_lines: list[str] = []
            index += 1
            while index < len(lines) and not lines[index].strip().startswith(marker):
                code_lines.append(lines[index])
                index += 1
            if index == len(lines):
                warnings.append("An unclosed Markdown code fence was imported as code")
            else:
                index += 1
            blocks.append(ExtractedBlock("code", "\n".join(code_lines)))
            continue
        if heading:
            flush_paragraph()
            blocks.append(ExtractedBlock("heading", heading.group(2), level=len(heading.group(1))))
            index += 1
            continue
        if list_match:
            flush_paragraph()
            ordered = list_match.group(2) is not None
            items: list[str] = []
            while index < len(lines):
                item_match = _MARKDOWN_LIST.match(lines[index])
                if item_match is None or (item_match.group(2) is not None) != ordered:
                    break
                items.append(item_match.group(3).strip())
                index += 1
            blocks.append(ExtractedBlock("list", ordered=ordered, items=tuple(items)))
            continue
        if stripped.startswith(">"):
            flush_paragraph()
            quote_lines: list[str] = []
            while index < len(lines) and lines[index].strip().startswith(">"):
                quote_lines.append(lines[index].strip().lstrip(">").strip())
                index += 1
            blocks.append(ExtractedBlock("blockquote", " ".join(quote_lines)))
            continue
        paragraph.append(stripped)
        index += 1
    flush_paragraph()
    return ExtractionResult(tuple(blocks), {"line_count": len(lines)}, tuple(warnings))


def _extract_pdf(source_bytes: bytes) -> ExtractionResult:
    try:
        reader = PdfReader(io.BytesIO(source_bytes))
        if reader.is_encrypted and reader.decrypt("") == 0:
            raise FukamiruError("Password-protected PDF cannot be imported")
    except FukamiruError:
        raise
    except Exception as exc:
        raise FukamiruError(f"PDF could not be opened: {exc}") from exc
    blocks: list[ExtractedBlock] = []
    warnings = ["PDF text was imported page-by-page; visual-only layout and images require review"]
    for page_number, page in enumerate(reader.pages, start=1):
        try:
            try:
                text = page.extract_text(extraction_mode="layout") or ""
            except TypeError:
                text = page.extract_text() or ""
        except Exception as exc:
            warnings.append(f"PDF page {page_number} text extraction failed: {exc}")
            continue
        paragraphs = _text_paragraphs(text)
        if not paragraphs:
            warnings.append(f"PDF page {page_number} has no extractable text")
            continue
        for paragraph in paragraphs:
            blocks.append(
                ExtractedBlock(
                    "paragraph",
                    paragraph,
                    attributes={"data-source-page": str(page_number)},
                )
            )
    if not blocks:
        raise FukamiruError(
            "PDF has no extractable text; run OCR first or import an "
            "OCR-generated TXT/Markdown file"
        )
    return ExtractionResult(tuple(blocks), {"page_count": len(reader.pages)}, tuple(warnings))


def _extract_docx(source_bytes: bytes) -> ExtractionResult:
    try:
        archive = zipfile.ZipFile(io.BytesIO(source_bytes))
    except (OSError, zipfile.BadZipFile) as exc:
        raise FukamiruError(f"DOCX could not be opened: {exc}") from exc
    with archive:
        names = set(archive.namelist())
        if "word/document.xml" not in names:
            raise FukamiruError("DOCX is missing word/document.xml")
        expanded_size = sum(item.file_size for item in archive.infolist())
        if expanded_size > 250 * 1024 * 1024:
            raise FukamiruError("DOCX expanded content exceeds the 250 MiB safety limit")
        try:
            root = _parse_word_xml(archive.read("word/document.xml"))
        except (ET.ParseError, KeyError) as exc:
            raise FukamiruError(f"DOCX document XML is invalid: {exc}") from exc
        body = root.find("w:body", _WORD)
        if body is None:
            raise FukamiruError("DOCX contains no document body")
        blocks: list[ExtractedBlock] = []
        list_items: list[str] = []
        warnings: list[str] = []
        table_rows = 0
        list_count = 0

        def flush_list() -> None:
            nonlocal list_count
            if list_items:
                blocks.append(ExtractedBlock("list", items=tuple(list_items)))
                list_items.clear()
                list_count += 1

        for child in body:
            if child.tag == f"{{{_WORD_NAMESPACE}}}p":
                text_value = _word_text(child).strip()
                if not text_value:
                    continue
                style = child.find("w:pPr/w:pStyle", _WORD)
                style_name = style.attrib.get(_WORD_VAL, "") if style is not None else ""
                heading_level = _docx_heading_level(style_name)
                numbered = child.find("w:pPr/w:numPr", _WORD) is not None
                if heading_level is not None:
                    flush_list()
                    blocks.append(
                        ExtractedBlock(
                            "heading",
                            text_value,
                            level=heading_level,
                            attributes={"data-docx-style": style_name or "Heading"},
                        )
                    )
                elif numbered:
                    list_items.append(text_value)
                else:
                    flush_list()
                    blocks.append(
                        ExtractedBlock(
                            "paragraph",
                            text_value,
                            attributes=({"data-docx-style": style_name} if style_name else {}),
                        )
                    )
            elif child.tag == f"{{{_WORD_NAMESPACE}}}tbl":
                flush_list()
                for row_number, row in enumerate(child.findall("w:tr", _WORD), start=1):
                    cells = [
                        " ".join(
                            value
                            for paragraph in cell.findall(".//w:p", _WORD)
                            if (value := _word_text(paragraph).strip())
                        )
                        for cell in row.findall("w:tc", _WORD)
                    ]
                    row_text = " | ".join(cells).strip(" |")
                    if row_text:
                        table_rows += 1
                        blocks.append(
                            ExtractedBlock(
                                "paragraph",
                                row_text,
                                attributes={
                                    "data-source-kind": "table-row",
                                    "data-source-row": str(row_number),
                                },
                            )
                        )
        flush_list()
        if table_rows:
            warnings.append(
                f"{table_rows} DOCX table rows were flattened with vertical-bar separators"
            )
        if list_count:
            warnings.append(f"{list_count} DOCX lists were normalized to canonical unordered lists")
        media_count = sum(name.startswith("word/media/") for name in names)
        if media_count:
            warnings.append(f"{media_count} embedded DOCX images were not imported")
        note_count = 0
        for note_kind, xml_name in (
            ("Footnotes", "word/footnotes.xml"),
            ("Endnotes", "word/endnotes.xml"),
        ):
            if xml_name not in names:
                continue
            note_blocks = _docx_notes(archive.read(xml_name))
            if note_blocks:
                blocks.append(
                    ExtractedBlock(
                        "heading",
                        note_kind,
                        level=1,
                        attributes={"data-source-kind": note_kind.casefold()},
                    )
                )
                blocks.extend(note_blocks)
                note_count += len(note_blocks)
        if note_count:
            warnings.append(
                f"{note_count} DOCX notes were appended; inline note-reference links require review"
            )
        if any(name.startswith("word/header") or name.startswith("word/footer") for name in names):
            warnings.append("DOCX headers and footers were excluded from manuscript prose")
    return ExtractionResult(
        tuple(blocks),
        {
            "table_row_count": table_rows,
            "list_count": list_count,
            "note_count": note_count,
        },
        tuple(warnings),
    )


def _word_text(element: ET.Element) -> str:
    output: list[str] = []
    for node in element.iter():
        if node.tag == f"{{{_WORD_NAMESPACE}}}t":
            output.append(node.text or "")
        elif node.tag == f"{{{_WORD_NAMESPACE}}}tab":
            output.append("\t")
        elif node.tag in {
            f"{{{_WORD_NAMESPACE}}}br",
            f"{{{_WORD_NAMESPACE}}}cr",
        }:
            output.append("\n")
    return "".join(output)


def _docx_heading_level(style_name: str) -> int | None:
    compact = style_name.replace("_", " ").replace("-", " ").strip()
    if compact.casefold() in {"title", "subtitle"}:
        return 1
    match = _HEADING_STYLE.match(compact)
    return int(match.group(1)) if match else None


def _docx_notes(source_xml: bytes) -> list[ExtractedBlock]:
    try:
        root = _parse_word_xml(source_xml)
    except (ET.ParseError, FukamiruError):
        return []
    blocks: list[ExtractedBlock] = []
    for note in list(root):
        note_id = note.attrib.get(_WORD_ID, "")
        try:
            if int(note_id) < 0:
                continue
        except ValueError:
            continue
        note_text = " ".join(
            value
            for paragraph in note.findall(".//w:p", _WORD)
            if (value := _word_text(paragraph).strip())
        )
        if note_text:
            blocks.append(
                ExtractedBlock(
                    "paragraph",
                    f"[{note_id}] {note_text}",
                    attributes={"data-note-id": note_id},
                )
            )
    return blocks


def _parse_word_xml(source_xml: bytes) -> ET.Element:
    lowered = source_xml.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise FukamiruError("DOCX XML declarations and entities are not allowed")
    return ET.fromstring(source_xml)
