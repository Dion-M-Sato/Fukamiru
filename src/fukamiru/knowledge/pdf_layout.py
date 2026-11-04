"""Deterministic PDF display-geometry analysis and canonical HTML rendering."""

from __future__ import annotations

import hashlib
import html
import json
import math
import re
import sqlite3
import statistics
import uuid
from collections import Counter
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from fukamiru.documents.canonical_html import CanonicalHTMLValidator
from fukamiru.errors import FukamiruError
from fukamiru.knowledge.page_reading import vertical_columns
from fukamiru.knowledge.pdf_text_layer import PDFTextLayer
from fukamiru.storage.runs import utc_now

ANALYZER_VERSION = "pdf-display-layout-v2"
_TOC_HEADING = re.compile(
    r"^(?:table\s+of\s+contents|contents|sommaire|table\s+des\s+mati[eè]res|目次)$",
    re.IGNORECASE,
)
_TOC_ENTRY = re.compile(r"^.{2,}?\s*(?:\.{2,}|…+)\s*[ivxlcdm\d]+\s*$", re.IGNORECASE)
_PAGE_NUMBER = re.compile(r"^(?:p\.?\s*)?(?:\d{1,4}|[ivxlcdm]{1,10})$", re.IGNORECASE)
_HEADING_TEXT = re.compile(
    r"^(?:chapter|chapitre|part|partie|section|appendix|annexe|第\s*\d+\s*章)\b",
    re.IGNORECASE,
)
_LIST_PREFIX = re.compile(r"^(?:[•‣◦▪–—-]|\(?\d{1,3}[.)]|[A-Za-z][.)])\s+")
_NOTE_PREFIX = re.compile(r"^(?:\[?\d{1,3}\]?|[*†‡])(?:[.)])?\s+")
_NUMBERED_MARKER = re.compile(
    r"^(?:\(?\d{1,4}[.)]?|[ivxlcdm]{1,10}[.)]?)$", re.IGNORECASE
)


def _xml_safe_text(value: str) -> str:
    """Remove characters forbidden by XML 1.0 before canonical HTML validation."""
    return "".join(
        character
        for character in value
        if character in "\t\n\r"
        or 0x20 <= ord(character) <= 0xD7FF
        or 0xE000 <= ord(character) <= 0xFFFD
        or 0x10000 <= ord(character) <= 0x10FFFF
    )


def is_nonlinguistic_artifact(text: str) -> bool:
    """Identify display-text debris that must not be sent for translation.

    Searchable PDFs sometimes expose individual drawing glyphs as tiny text
    objects.  They can inherit a large font and otherwise look like headings.
    Keep legitimate numbered markers, but reject punctuation-only and
    overwhelmingly symbolic fragments.
    """
    stripped = text.strip()
    if not stripped:
        return True
    compact = "".join(character for character in stripped if not character.isspace())
    if _NUMBERED_MARKER.fullmatch(compact):
        return False
    alphanumeric = sum(character.isalnum() for character in compact)
    if alphanumeric == 0:
        return True
    longest_letter_run = 0
    current_letter_run = 0
    for character in compact:
        if character.isalpha():
            current_letter_run += 1
            longest_letter_run = max(longest_letter_run, current_letter_run)
        else:
            current_letter_run = 0
    if longest_letter_run < 2 and not compact.isalpha():
        return True
    return len(compact) >= 8 and alphanumeric / len(compact) < 0.35


@dataclass(frozen=True)
class PositionedText:
    text: str
    x0: float
    top: float
    x1: float
    bottom: float
    font_size: float
    font_name: str


@dataclass(frozen=True)
class LayoutBlock:
    stable_block_id: str
    page_number: int
    ordinal: int
    structural_role: str
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    font_size: float
    font_name: str
    confidence: float
    translatable: bool = True
    attributes: dict[str, Any] | None = None

    def serializable(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["attributes"] = self.attributes or {}
        return payload


@dataclass(frozen=True)
class LayoutPage:
    page_number: int
    width: float
    height: float
    rotation: int
    printed_label: str | None
    blocks: tuple[LayoutBlock, ...]

    @property
    def text(self) -> str:
        return "\n\n".join(block.text for block in self.blocks if block.text.strip())

    def serializable(self) -> dict[str, Any]:
        return {
            "page_number": self.page_number,
            "width": self.width,
            "height": self.height,
            "rotation": self.rotation,
            "printed_label": self.printed_label,
            "text": self.text,
            "blocks": [block.serializable() for block in self.blocks],
        }


@dataclass(frozen=True)
class LayoutDocument:
    source_asset_id: str
    analyzer_version: str
    pages: tuple[LayoutPage, ...]

    def serializable_pages(self) -> list[dict[str, Any]]:
        return [page.serializable() for page in self.pages]


@dataclass(frozen=True)
class PreparedPDF:
    source_path: Path
    page_data: tuple[tuple[float, float, int, list[PositionedText]], ...]
    text_layer: PDFTextLayer


class PDFLayoutAnalyzer:
    """Infer document roles from text placement on rendered PDF pages.

    The analyzer deliberately uses PDF display coordinates and font metadata before
    any language cleanup.  It does not infer structure from blank lines in OCR text.
    """

    def prepare(self, path: Path) -> PreparedPDF:
        """Read positioned text once for both OCR routing and layout analysis."""
        try:
            from pypdf import PdfReader
        except ImportError as exc:  # pragma: no cover - installation supplies pypdf
            raise FukamiruError("PDF support is unavailable; rerun install.ps1") from exc
        try:
            reader = PdfReader(path)
            page_data: list[tuple[float, float, int, list[PositionedText]]] = []
            for page in reader.pages:
                width, height = self._page_dimensions(page)
                page_data.append(
                    (
                        width,
                        height,
                        int(page.rotation or 0),
                        self._positioned_text(page, page_height=height),
                    )
                )
        except FukamiruError:
            raise
        except Exception as exc:
            raise FukamiruError(f"PDF layout analysis failed: {exc}") from exc
        if not page_data:
            raise FukamiruError("PDF contains no pages")
        visible = sum(
            len(re.sub(r"\s+", "", item.text))
            for *_, spans in page_data
            for item in spans
        )
        return PreparedPDF(
            path.resolve(),
            tuple(page_data),
            PDFTextLayer(len(page_data), visible, max(20, len(page_data) * 5)),
        )

    def analyze(
        self,
        path: Path,
        *,
        source_asset_id: str,
        prepared: PreparedPDF | None = None,
    ) -> LayoutDocument:
        parsed = prepared or self.prepare(path)
        if parsed.source_path != path.resolve():
            raise FukamiruError("Prepared PDF belongs to another source path")
        page_data = parsed.page_data
        if not parsed.text_layer.usable:
            raise FukamiruError(
                "OCR_REQUIRED: PDFに利用可能なテキスト層がありません。"
                "言語別OCRパイプラインを実行してください。"
            )

        provisional: list[list[LayoutBlock]] = []
        for page_number, (width, height, _rotation, spans) in enumerate(page_data, start=1):
            provisional.append(
                self._blocks_for_page(
                    spans,
                    source_asset_id=source_asset_id,
                    page_number=page_number,
                    width=width,
                    height=height,
                )
            )
        classified = self._classify_repeated_margins(provisional, page_data)
        pages: list[LayoutPage] = []
        for page_number, ((width, height, rotation, _), blocks) in enumerate(
            zip(page_data, classified, strict=True), start=1
        ):
            printed = next(
                (block.text for block in blocks if block.structural_role == "page_number"),
                None,
            )
            pages.append(
                LayoutPage(page_number, width, height, rotation, printed, tuple(blocks))
            )
        return LayoutDocument(source_asset_id, ANALYZER_VERSION, tuple(pages))

    @staticmethod
    def _page_dimensions(page: Any) -> tuple[float, float]:
        """Return usable dimensions even when one page box is malformed."""
        candidates: list[Any] = []
        for attribute in ("mediabox", "cropbox"):
            with suppress(AssertionError, TypeError, ValueError):
                candidates.append(getattr(page, attribute))
        for key in ("/CropBox", "/MediaBox"):
            with suppress(Exception):
                candidates.append(page.get(key))
        for box in candidates:
            try:
                if box is None or len(box) != 4:
                    continue
                x0, y0, x1, y1 = (float(value) for value in box)
                width = abs(x1 - x0)
                height = abs(y1 - y0)
                if math.isfinite(width) and math.isfinite(height) and width > 0 and height > 0:
                    return width, height
            except (AssertionError, TypeError, ValueError):
                continue
        raise FukamiruError("PDF page has no usable MediaBox or CropBox")

    @staticmethod
    def _positioned_text(page: Any, *, page_height: float) -> list[PositionedText]:
        positioned: list[PositionedText] = []

        def visit(
            text: str,
            current_matrix: list[float],
            text_matrix: list[float],
            font_dictionary: dict[str, Any] | None,
            font_size: float,
        ) -> None:
            values = [value.strip() for value in re.split(r"[\r\n]+", text) if value.strip()]
            if not values:
                return
            cm = current_matrix
            tm = text_matrix
            x = float(tm[4] * cm[0] + tm[5] * cm[2] + cm[4])
            baseline = float(tm[4] * cm[1] + tm[5] * cm[3] + cm[5])
            text_scale = math.hypot(float(tm[0]), float(tm[1])) or 1.0
            size = max(float(font_size or 0.0) * text_scale, 1.0)
            font_name = str((font_dictionary or {}).get("/BaseFont", ""))
            for index, value in enumerate(values):
                line_baseline = baseline - index * size * 1.2
                top = min(page_height, max(0.0, page_height - line_baseline - size))
                width = max(size * 0.5, len(value) * size * 0.48)
                positioned.append(
                    PositionedText(
                        value,
                        x,
                        top,
                        x + width,
                        min(page_height, top + size * 1.25),
                        size,
                        font_name,
                    )
                )

        page.extract_text(visitor_text=visit)
        return positioned

    def _blocks_for_page(
        self,
        spans: list[PositionedText],
        *,
        source_asset_id: str,
        page_number: int,
        width: float,
        height: float,
    ) -> list[LayoutBlock]:
        lines = self._visual_lines(spans, page_width=width)
        if not lines:
            return []
        font_sizes = [line.font_size for line in lines if line.font_size > 0]
        body_size = statistics.median(font_sizes) if font_sizes else 10.0
        largest = max(font_sizes, default=body_size)
        body_lines = [
            line
            for line in lines
            if len(line.text) >= 40 and line.font_size <= body_size * 1.15
        ]
        body_left = statistics.median(
            line.x0 for line in body_lines
        ) if body_lines else min(line.x0 for line in lines)
        body_right = statistics.median(
            line.x1 for line in body_lines
        ) if body_lines else max(line.x1 for line in lines)
        toc_page = any(_TOC_HEADING.fullmatch(line.text.strip()) for line in lines)

        groups: list[list[PositionedText]] = []
        for line in lines:
            if not groups or not self._same_block(groups[-1][-1], line, body_size):
                groups.append([line])
            else:
                groups[-1].append(line)

        blocks: list[LayoutBlock] = []
        for ordinal, group in enumerate(groups):
            text = self._join_lines(group)
            x0 = min(line.x0 for line in group)
            y0 = min(line.top for line in group)
            x1 = max(line.x1 for line in group)
            y1 = max(line.bottom for line in group)
            size = statistics.median(line.font_size for line in group)
            font_name = Counter(line.font_name for line in group).most_common(1)[0][0]
            role, confidence = self._role(
                text,
                page_number=page_number,
                x0=x0,
                y0=y0,
                x1=x1,
                y1=y1,
                width=width,
                height=height,
                font_size=size,
                body_size=body_size,
                body_left=body_left,
                body_right=body_right,
                largest=largest,
                font_name=font_name,
                toc_page=toc_page,
            )
            stable = self._stable_block_id(source_asset_id, page_number, ordinal, text)
            blocks.append(
                LayoutBlock(
                    stable,
                    page_number,
                    ordinal,
                    role,
                    text,
                    x0,
                    y0,
                    x1,
                    y1,
                    size,
                    font_name,
                    confidence,
                    role
                    not in {"artifact", "page_header", "page_footer", "page_number"},
                    {
                        "analyzer_version": ANALYZER_VERSION,
                        "body_font_size": round(body_size, 3),
                    },
                )
            )
        return blocks

    @staticmethod
    def _visual_lines(
        spans: list[PositionedText], *, page_width: float
    ) -> list[PositionedText]:
        columns = vertical_columns(spans)
        if columns is not None:
            return [PositionedText(
                "".join(item.text.strip() for item in column),
                min(s.x0 for s in column), min(s.top for s in column),
                max(s.x1 for s in column), max(s.bottom for s in column),
                statistics.median(s.font_size for s in column),
                Counter(s.font_name for s in column).most_common(1)[0][0],
            ) for column in columns]
        rows: list[list[PositionedText]] = []
        for span in sorted(spans, key=lambda item: (item.top, item.x0)):
            row = next(
                (
                    candidate
                    for candidate in reversed(rows[-4:])
                    if abs(statistics.median(item.top for item in candidate) - span.top)
                    <= max(2.0, span.font_size * 0.45)
                ),
                None,
            )
            if row is None:
                rows.append([span])
            else:
                row.append(span)
        lines: list[PositionedText] = []
        for row in rows:
            ordered = sorted(row, key=lambda item: item.x0)
            horizontal_groups: list[list[PositionedText]] = []
            for item in ordered:
                crosses_page_center = bool(
                    horizontal_groups
                    and horizontal_groups[-1][-1].x0 < page_width * 0.45
                    and item.x0 >= page_width * 0.5
                )
                has_large_gap = bool(
                    horizontal_groups
                    and item.x0 - horizontal_groups[-1][-1].x1
                    > max(36.0, item.font_size * 4.0)
                )
                if (
                    horizontal_groups
                    and not crosses_page_center
                    and not has_large_gap
                ):
                    horizontal_groups[-1].append(item)
                else:
                    horizontal_groups.append([item])
            for group in horizontal_groups:
                pieces: list[str] = []
                previous: PositionedText | None = None
                for item in group:
                    if previous is not None and item.x0 - previous.x1 > item.font_size * 0.3:
                        pieces.append(" ")
                    pieces.append(item.text)
                    previous = item
                lines.append(
                    PositionedText(
                        re.sub(r"[ \t]+", " ", "".join(pieces)).strip(),
                        min(item.x0 for item in group),
                        min(item.top for item in group),
                        max(item.x1 for item in group),
                        max(item.bottom for item in group),
                        statistics.median(item.font_size for item in group),
                        Counter(item.font_name for item in group).most_common(1)[0][0],
                    )
                )
        lines = [line for line in lines if line.text]
        left = [line for line in lines if line.x0 < page_width * 0.48]
        right = [line for line in lines if line.x0 > page_width * 0.52]
        spanning = [line for line in lines if line not in left and line not in right]
        if len(left) >= 3 and len(right) >= 3:
            return [
                *sorted(spanning, key=lambda item: (item.top, item.x0)),
                *sorted(left, key=lambda item: (item.top, item.x0)),
                *sorted(right, key=lambda item: (item.top, item.x0)),
            ]
        return sorted(lines, key=lambda item: (item.top, item.x0))

    @staticmethod
    def _same_block(previous: PositionedText, current: PositionedText, body_size: float) -> bool:
        vertical_gap = current.top - previous.bottom
        same_font = abs(current.font_size - previous.font_size) <= max(1.0, body_size * 0.12)
        aligned = abs(current.x0 - previous.x0) <= max(18.0, body_size * 2.0)
        previous_heading = previous.font_size > body_size * 1.18 or _HEADING_TEXT.match(
            previous.text
        )
        current_heading = current.font_size > body_size * 1.18 or _HEADING_TEXT.match(current.text)
        return (
            vertical_gap <= max(body_size * 0.9, 5.0)
            and vertical_gap >= -body_size * 0.4
            and same_font
            and aligned
            and not previous_heading
            and not current_heading
            and not _TOC_ENTRY.match(previous.text)
            and not _TOC_ENTRY.match(current.text)
        )

    @staticmethod
    def _join_lines(lines: list[PositionedText]) -> str:
        result = lines[0].text
        for line in lines[1:]:
            if re.search(r"[A-Za-zÀ-ÖØ-öø-ÿ]-$", result) and re.match(
                r"[a-zà-öø-ÿ]", line.text
            ):
                result = result[:-1] + line.text
            else:
                result += " " + line.text
        return re.sub(r"[ \t]+", " ", result).strip()

    @staticmethod
    def _role(
        text: str,
        *,
        page_number: int,
        x0: float,
        y0: float,
        x1: float,
        y1: float,
        width: float,
        height: float,
        font_size: float,
        body_size: float,
        body_left: float,
        body_right: float,
        largest: float,
        font_name: str,
        toc_page: bool,
    ) -> tuple[str, float]:
        stripped = text.strip()
        if is_nonlinguistic_artifact(stripped):
            return "artifact", 0.98
        at_top = y0 <= height * 0.11
        at_bottom = y1 >= height * 0.88
        if (at_top or at_bottom) and _PAGE_NUMBER.fullmatch(stripped):
            return "page_number", 0.99
        if toc_page and (_TOC_ENTRY.match(stripped) or re.search(r"\s\d{1,4}\s*$", stripped)):
            return "toc_entry", 0.92
        if _TOC_HEADING.fullmatch(stripped):
            return "heading", 0.99
        centered = abs(((x0 + x1) / 2.0) - width / 2.0) <= width * 0.12
        short = len(stripped) <= 180
        bold = "bold" in font_name.casefold() or "black" in font_name.casefold()
        if page_number == 1 and y0 <= height * 0.55 and short and font_size >= largest * 0.9:
            return "title", 0.94
        if short and (
            font_size >= body_size * 1.18
            or (bold and font_size >= body_size)
            or _HEADING_TEXT.match(stripped)
        ):
            return "heading", 0.9 if centered or bold else 0.82
        in_note_zone = y0 >= height * 0.58 and font_size <= body_size * 0.9
        if (at_bottom and _NOTE_PREFIX.match(stripped)) or in_note_zone:
            return "footnote", 0.86
        if _LIST_PREFIX.match(stripped):
            return "list_item", 0.9
        quote_marker = stripped.startswith(("“", "‘", "\"", "'", "«"))
        italic = "italic" in font_name.casefold() or "oblique" in font_name.casefold()
        visually_indented = (
            x0 >= body_left + body_size * 2.4
            and x1 <= body_right - body_size * 1.4
        )
        if len(stripped) > 30 and (quote_marker or italic or visually_indented):
            return "quote", 0.7
        return "paragraph", 0.78

    @staticmethod
    def _classify_repeated_margins(
        pages: list[list[LayoutBlock]],
        page_data: list[tuple[float, float, int, list[PositionedText]]],
    ) -> list[list[LayoutBlock]]:
        candidates: list[tuple[int, int, str, str]] = []
        for page_index, (blocks, (_width, height, _rotation, _spans)) in enumerate(
            zip(pages, page_data, strict=True)
        ):
            for block_index, block in enumerate(blocks):
                if block.y0 <= height * 0.1:
                    zone = "top"
                elif block.y1 >= height * 0.9:
                    zone = "bottom"
                else:
                    zone = ""
                if zone and block.structural_role not in {"page_number", "footnote"}:
                    normalized = re.sub(r"\d+", "#", re.sub(r"\s+", " ", block.text.casefold()))
                    candidates.append((page_index, block_index, zone, normalized))
        counts = Counter((zone, text) for _, _, zone, text in candidates)
        threshold = max(2, round(len(pages) * 0.08))
        repeated = {key for key, count in counts.items() if count >= threshold}
        result = [list(blocks) for blocks in pages]
        for page_index, block_index, zone, normalized in candidates:
            if (zone, normalized) not in repeated:
                continue
            role = "page_header" if zone == "top" else "page_footer"
            result[page_index][block_index] = replace(
                result[page_index][block_index],
                structural_role=role,
                confidence=0.98,
                translatable=False,
            )
        return result

    @staticmethod
    def _stable_block_id(
        source_asset_id: str, page_number: int, ordinal: int, text: str
    ) -> str:
        digest = hashlib.sha256(
            f"{source_asset_id}\x1f{page_number}\x1f{ordinal}\x1f{text}".encode()
        ).hexdigest()[:18]
        return f"PDFB-{digest}"


class PDFLayoutRepository:
    def __init__(
        self, connection: sqlite3.Connection, *, autocommit: bool = True
    ) -> None:
        self.connection = connection
        self.autocommit = autocommit

    def store(self, document: LayoutDocument, *, structured_html_path: Path) -> str:
        document_id = str(uuid.uuid4())
        now = utc_now()
        if self.autocommit:
            self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                """
                DELETE FROM pdf_layout_documents
                WHERE source_asset_id = ? AND analyzer_version = ?
                """,
                (document.source_asset_id, document.analyzer_version),
            )
            self.connection.execute(
                """
                INSERT INTO pdf_layout_documents(
                    id, source_asset_id, analyzer_version, page_count,
                    structured_html_path, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id,
                    document.source_asset_id,
                    document.analyzer_version,
                    len(document.pages),
                    str(structured_html_path),
                    now,
                ),
            )
            for page in document.pages:
                page_id = str(uuid.uuid4())
                self.connection.execute(
                    """
                    INSERT INTO pdf_layout_pages(
                        id, layout_document_id, page_number, width, height,
                        rotation, printed_label, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        page_id,
                        document_id,
                        page.page_number,
                        page.width,
                        page.height,
                        page.rotation,
                        page.printed_label,
                        now,
                    ),
                )
                for block in page.blocks:
                    self.connection.execute(
                        """
                        INSERT INTO pdf_layout_blocks(
                            id, layout_page_id, stable_block_id, structural_role,
                            ordinal, x0, y0, x1, y1, font_size, font_name, text,
                            confidence, translatable, attributes_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(uuid.uuid4()),
                            page_id,
                            block.stable_block_id,
                            block.structural_role,
                            block.ordinal,
                            block.x0,
                            block.y0,
                            block.x1,
                            block.y1,
                            block.font_size,
                            block.font_name,
                            block.text,
                            block.confidence,
                            int(block.translatable),
                            json.dumps(
                                block.attributes or {},
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                            now,
                        ),
                    )
            if self.autocommit:
                self.connection.commit()
        except Exception:
            if self.autocommit:
                self.connection.rollback()
            raise
        return document_id


class PDFLayoutHTMLRenderer:
    def render(self, document: LayoutDocument, *, title: str, language: str | None) -> str:
        prefix = document.source_asset_id.replace("-", "")[:12]
        article_id = f"PDF-{prefix}"
        sections = [
            f'<section id="SEC-{prefix}-META"><h2 id="H-{prefix}-META">'
            f'{html.escape(_xml_safe_text(title))}</h2><p id="P-{prefix}-META" '
            f'data-layout-analyzer="{ANALYZER_VERSION}">'
            f"{len(document.pages)} source pages</p></section>"
        ]
        for page in document.pages:
            page_key = f"{prefix}-P{page.page_number:04d}"
            body: list[str] = []
            nested: list[str] | None = None

            toc_items: list[str] = []
            for block in page.blocks:
                escaped = html.escape(_xml_safe_text(block.text))
                attributes = (
                    f'data-layout-role="{block.structural_role}" '
                    f'data-source-page="{page.page_number}" '
                    f'data-bbox="{block.x0:.2f},{block.y0:.2f},{block.x1:.2f},{block.y1:.2f}" '
                    f'data-translatable="{str(block.translatable).lower()}"'
                )
                if block.structural_role in {"title", "heading"}:
                    if nested is not None:
                        nested.append("</section>")
                        body.append("".join(nested))
                    nested = [
                        f'<section id="SEC-{block.stable_block_id}" {attributes}>'
                        f'<h3 id="{block.stable_block_id}">{escaped}</h3>'
                    ]
                    continue
                if block.structural_role == "toc_entry":
                    toc_items.append(
                        f'<li id="{block.stable_block_id}" {attributes}>{escaped}</li>'
                    )
                    continue
                target = nested if nested is not None else body
                tag = "blockquote" if block.structural_role == "quote" else "p"
                target.append(
                    f'<{tag} id="{block.stable_block_id}" {attributes}>{escaped}</{tag}>'
                )
            if nested is not None:
                nested.append("</section>")
                body.append("".join(nested))
            if toc_items:
                body.insert(
                    0,
                    f'<ol id="TOC-{page_key}" data-layout-role="toc">'
                    f'{"".join(toc_items)}</ol>',
                )
            page_body = "".join(body) or (
                f'<p id="P-{page_key}-EMPTY">No positioned text.</p>'
            )
            sections.append(
                f'<section id="SEC-{page_key}" data-source-page="{page.page_number}">'
                f'<h2 id="H-{page_key}">Page {page.printed_label or page.page_number}</h2>'
                f"{page_body}"
                "</section>"
            )
        rendered = (
            f'<article id="{article_id}" '
            f'lang="{html.escape(_xml_safe_text(language or "und"), quote=True)}" '
            f'data-source-asset-id="{html.escape(document.source_asset_id, quote=True)}">'
            f'{"".join(sections)}</article>'
        )
        CanonicalHTMLValidator().assert_valid(rendered)
        return rendered
