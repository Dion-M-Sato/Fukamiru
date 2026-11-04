"""Structure-preserving Markdown book translation and footnote validation."""

from __future__ import annotations

import hashlib
import html
import re
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from typing import Any

from fukamiru.documents.canonical_html import CanonicalHTMLValidator
from fukamiru.errors import FukamiruError

_PAGE_MARKER = re.compile(r"<!--\s*page\s+([^>]+?)\s*-->", re.IGNORECASE)
_IMAGE = re.compile(r"!?\[[^\]]*\]\([^\n)]+\)")
_HEADING = re.compile(r"^(#{1,6})\s+(.+)$", re.DOTALL)
_NOTE_DEFINITION = re.compile(r"^\s*(\d{1,3})\.\s+(.+)$", re.DOTALL)
_SUPERSCRIPT = re.compile(r"[⁰¹²³⁴⁵⁶⁷⁸⁹]+")
_SUPERSCRIPT_DIGITS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")
_CHAPTER = re.compile(
    r"^(?:chapitre|chapter)\s+(?:[ivxlcdm]+|\d+|premier|première)\b|^第[^\s]+章",
    re.IGNORECASE,
)


def _key(value: str) -> str:
    return re.sub(r"\W+", " ", value, flags=re.UNICODE).strip().casefold()


def _stable_id(kind: str, *values: object) -> str:
    payload = "\0".join(str(value) for value in values)
    return f"{kind}-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:20]}"


@dataclass(frozen=True)
class MarkdownBook:
    source_sha256: str
    segments: tuple[dict[str, Any], ...]
    footnotes: tuple[dict[str, Any], ...]
    reference_ids: tuple[str, ...]
    issues: tuple[dict[str, str], ...]
    discarded: dict[str, int]

    def manifest(self) -> dict[str, Any]:
        return {
            "source_sha256": self.source_sha256,
            "footnote_ids": [item["footnote_id"] for item in self.footnotes],
            "reference_ids": list(self.reference_ids),
            "reference_footnote_ids": [
                segment["footnote_id_after"]
                for segment in self.segments
                if segment.get("footnote_id_after")
            ],
            "issues": list(self.issues),
            "discarded": dict(self.discarded),
        }


def parse_markdown_book(source: str) -> MarkdownBook:
    """Parse a page-derived Markdown book without exposing its physical page furniture."""
    normalized = source.replace("\r\n", "\n").replace("\r", "\n")
    source_sha = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    markers = list(_PAGE_MARKER.finditer(normalized))
    page_ranges: list[tuple[int, int, int]] = []
    if markers:
        for index, marker in enumerate(markers):
            page_ranges.append(
                (
                    index + 1,
                    marker.end(),
                    markers[index + 1].start()
                    if index + 1 < len(markers)
                    else len(normalized),
                )
            )
    else:
        page_ranges.append((1, 0, len(normalized)))

    raw_pages: list[list[dict[str, Any]]] = []
    boundary_candidates: Counter[str] = Counter()
    for page_number, start, end in page_ranges:
        blocks: list[dict[str, Any]] = []
        page = normalized[start:end]
        matches = list(re.finditer(r"\S(?:.*?\S)?(?=\n\s*\n|\Z)", page, re.DOTALL))
        for ordinal, match in enumerate(matches):
            value = match.group(0).strip()
            blocks.append(
                {
                    "text": value,
                    "absolute_start": start + match.start(),
                    "page_number": page_number,
                    "page_ordinal": ordinal,
                    "page_block_count": len(matches),
                }
            )
            if ordinal < 3 or ordinal >= max(0, len(matches) - 2):
                plain = re.sub(r"^#{1,6}\s+", "", value).strip()
                if "\n" not in plain and 1 < len(plain) <= 100 and not plain.isdigit():
                    boundary_candidates[_key(plain)] += 1
        raw_pages.append(blocks)
    repeated_furniture = {
        candidate for candidate, count in boundary_candidates.items() if candidate and count >= 2
    }

    definition_candidates: list[dict[str, Any]] = []
    reference_candidates: list[dict[str, Any]] = []
    for blocks in raw_pages:
        for block in blocks:
            definition_starts = list(re.finditer(r"(?m)^\s*(\d{1,3})\.\s+", block["text"]))
            if definition_starts and definition_starts[0].start() == 0:
                for index, definition in enumerate(definition_starts):
                    body_start = definition.end()
                    body_end = (
                        definition_starts[index + 1].start()
                        if index + 1 < len(definition_starts)
                        else len(block["text"])
                    )
                    definition_candidates.append(
                        {
                            "label": definition.group(1),
                            "text": block["text"][body_start:body_end].strip(),
                            "page_number": block["page_number"],
                            "absolute_start": block["absolute_start"] + definition.start(),
                            "block_start": block["absolute_start"],
                        }
                    )
                continue
            for reference in _SUPERSCRIPT.finditer(block["text"]):
                reference_candidates.append(
                    {
                        "label": reference.group(0).translate(_SUPERSCRIPT_DIGITS),
                        "page_number": block["page_number"],
                        "absolute_start": block["absolute_start"] + reference.start(),
                    }
                )

    note_by_reference_offset: dict[int, dict[str, Any]] = {}
    used_definition_offsets: set[int] = set()
    used_definition_blocks: set[int] = set()
    footnotes: list[dict[str, Any]] = []
    for reference in reference_candidates:
        available = [
            candidate
            for candidate in definition_candidates
            if candidate["label"] == reference["label"]
            and candidate["absolute_start"] not in used_definition_offsets
            and abs(int(candidate["page_number"]) - int(reference["page_number"])) <= 1
        ]
        if not available:
            continue
        candidate = min(
            available,
            key=lambda item: (
                abs(int(item["page_number"]) - int(reference["page_number"])),
                abs(int(item["absolute_start"]) - int(reference["absolute_start"])),
            ),
        )
        footnote_id = _stable_id(
            "FN",
            source_sha,
            candidate["absolute_start"],
            candidate["label"],
            _key(str(candidate["text"]))[:160],
        )
        note = {**candidate, "footnote_id": footnote_id}
        footnotes.append(note)
        used_definition_offsets.add(int(candidate["absolute_start"]))
        used_definition_blocks.add(int(candidate["block_start"]))
        note_by_reference_offset[int(reference["absolute_start"])] = note

    discarded: Counter[str] = Counter()
    issues: list[dict[str, str]] = []
    segments: list[dict[str, Any]] = []
    reference_ids: list[str] = []
    chapter_id = "front"
    block_ordinal = 0
    for blocks in raw_pages:
        for block in blocks:
            original = block["text"]
            if (
                _NOTE_DEFINITION.match(original)
                and int(block["absolute_start"]) in used_definition_blocks
            ):
                discarded["footnote_definition"] += 1
                continue
            if re.fullmatch(r"-{3,}", original):
                discarded["horizontal_rule"] += 1
                continue
            if re.fullmatch(r"\d{1,4}", original):
                discarded["page_number"] += 1
                continue
            heading_match = _HEADING.match(original)
            plain_for_furniture = re.sub(r"^#{1,6}\s+", "", original).strip()
            at_boundary = block["page_ordinal"] < 3 or block["page_ordinal"] >= max(
                0, block["page_block_count"] - 2
            )
            if (
                at_boundary
                and not heading_match
                and _key(plain_for_furniture) in repeated_furniture
            ):
                discarded["running_header"] += 1
                continue
            cleaned = _IMAGE.sub("", original)
            cleaned = _PAGE_MARKER.sub("", cleaned).strip()
            if not cleaned:
                discarded["image"] += 1
                continue
            if cleaned != original and not cleaned.strip():
                discarded["image"] += 1
                continue
            heading_match = _HEADING.match(cleaned)
            role = "paragraph"
            heading_level = None
            if heading_match:
                heading_level = len(heading_match.group(1))
                cleaned = re.sub(r"\s*\n\s*", " ", heading_match.group(2)).strip()
                role = "chapter" if _CHAPTER.match(cleaned) else "heading"
                if role == "chapter":
                    chapter_id = _stable_id("CH", source_sha, block["absolute_start"], cleaned)
            else:
                cleaned = re.sub(r"\s*\n\s*", " ", cleaned).strip()
            if not cleaned:
                continue

            block_id = _stable_id("BK", source_sha, block["absolute_start"], role)
            cursor = 0
            fragment_index = 0
            matches = list(_SUPERSCRIPT.finditer(cleaned))
            for ref_ordinal, match in enumerate(matches):
                label = match.group(0).translate(_SUPERSCRIPT_DIGITS)
                note = note_by_reference_offset.get(block["absolute_start"] + match.start())
                if note is None:
                    issues.append(
                        {
                            "code": "book.orphan_reference",
                            "message": f"Footnote reference {label} has no definition",
                        }
                    )
                reference_id = _stable_id(
                    "FNR", source_sha, block["absolute_start"] + match.start(), label, ref_ordinal
                )
                reference_ids.append(reference_id)
                fragment = cleaned[cursor : match.start()].strip()
                segments.append(
                    {
                        "text": fragment,
                        "structural_role": role,
                        "book_block_id": block_id,
                        "book_chapter_id": chapter_id,
                        "heading_level": heading_level,
                        "fragment_index": fragment_index,
                        "footnote_id_after": note["footnote_id"] if note else None,
                        "footnote_reference_id_after": reference_id,
                        "orphan_footnote_label": label if note is None else None,
                        "source_page_start": block["page_number"],
                        "source_page_end": block["page_number"],
                        "start_page": block["page_number"],
                        "end_page": block["page_number"],
                        "source_stable_block_id": block_id,
                        "ordinal": block_ordinal,
                        "translatable": bool(fragment),
                    }
                )
                fragment_index += 1
                cursor = match.end()
            tail = cleaned[cursor:].strip()
            if tail or not matches:
                segments.append(
                    {
                        "text": tail,
                        "structural_role": role,
                        "book_block_id": block_id,
                        "book_chapter_id": chapter_id,
                        "heading_level": heading_level,
                        "fragment_index": fragment_index,
                        "footnote_id_after": None,
                        "footnote_reference_id_after": None,
                        "orphan_footnote_label": None,
                        "source_page_start": block["page_number"],
                        "source_page_end": block["page_number"],
                        "start_page": block["page_number"],
                        "end_page": block["page_number"],
                        "source_stable_block_id": block_id,
                        "ordinal": block_ordinal,
                        "translatable": bool(tail),
                    }
                )
            block_ordinal += 1

    referenced_note_ids = {
        str(segment["footnote_id_after"])
        for segment in segments
        if segment.get("footnote_id_after")
    }
    for note in footnotes:
        if note["footnote_id"] not in referenced_note_ids:
            issues.append(
                {
                    "code": "book.orphan_definition",
                    "message": f"Footnote definition {note['label']} has no reference",
                }
            )
        segments.append(
            {
                "text": note["text"],
                "structural_role": "footnote",
                "book_block_id": note["footnote_id"],
                "book_chapter_id": _chapter_for_note(segments, note["page_number"]),
                "heading_level": None,
                "fragment_index": 0,
                "footnote_id_after": None,
                "footnote_reference_id_after": None,
                "orphan_footnote_label": None,
                "source_page_start": note["page_number"],
                "source_page_end": note["page_number"],
                "start_page": note["page_number"],
                "end_page": note["page_number"],
                "source_stable_block_id": note["footnote_id"],
                "ordinal": block_ordinal,
                "footnote_label": note["label"],
                "footnote_id": note["footnote_id"],
                "translatable": True,
            }
        )
        block_ordinal += 1

    return MarkdownBook(
        source_sha256=source_sha,
        segments=tuple(segments),
        footnotes=tuple(footnotes),
        reference_ids=tuple(reference_ids),
        issues=tuple(issues),
        discarded=dict(discarded),
    )


def _chapter_for_note(segments: list[dict[str, Any]], page_number: int) -> str:
    candidates = [
        str(segment["book_chapter_id"])
        for segment in segments
        if segment.get("structural_role") != "footnote"
        and int(segment.get("source_page_start") or 0) <= page_number
    ]
    return candidates[-1] if candidates else "front"


def render_translated_book(
    run_id: str,
    translated: list[str],
    records: list[dict[str, Any]],
    language: str,
) -> str:
    """Recombine independently translated prose fragments and notes as a semantic book."""
    if len(translated) != len(records):
        raise FukamiruError("Book translation record count changed")
    prefix = run_id.replace("-", "")[:12]
    blocks: list[dict[str, Any]] = []
    notes: dict[str, dict[str, Any]] = {}
    for text, record in zip(translated, records, strict=True):
        if record.get("structural_role") == "footnote":
            notes[str(record["footnote_id"])] = {**record, "translated": text.strip()}
            continue
        block_id = str(record["book_block_id"])
        if not blocks or blocks[-1]["book_block_id"] != block_id:
            blocks.append({**record, "fragments": []})
        blocks[-1]["fragments"].append(
            {
                "text": text.strip(),
                "footnote_id": record.get("footnote_id_after"),
                "reference_id": record.get("footnote_reference_id_after"),
            }
        )

    chapter_order: list[str] = []
    chapter_blocks: dict[str, list[dict[str, Any]]] = {}
    for block in blocks:
        chapter_id = str(block.get("book_chapter_id") or "front")
        if chapter_id not in chapter_blocks:
            chapter_order.append(chapter_id)
            chapter_blocks[chapter_id] = []
        chapter_blocks[chapter_id].append(block)
    note_order = [
        fragment["footnote_id"]
        for block in blocks
        for fragment in block["fragments"]
        if fragment.get("footnote_id")
    ]

    rendered_sections: list[str] = []
    for chapter_index, chapter_id in enumerate(chapter_order, start=1):
        chapter = chapter_blocks[chapter_id]
        chapter_key = re.sub(r"[^A-Za-z0-9_.:-]", "-", chapter_id)
        heading_index = next(
            (index for index, block in enumerate(chapter) if block["structural_role"] == "chapter"),
            None,
        )
        if heading_index is None:
            heading_content = html.escape(
                "Translation" if chapter_index == 1 else f"Section {chapter_index}"
            )
        else:
            heading_content = _render_fragments(chapter[heading_index]["fragments"])
        body: list[str] = []
        open_subsection = False
        for index, block in enumerate(chapter):
            if index == heading_index:
                continue
            role = str(block.get("structural_role") or "paragraph")
            content = _render_fragments(block["fragments"])
            block_key = re.sub(r"[^A-Za-z0-9_.:-]", "-", str(block["book_block_id"]))
            if role == "heading":
                if open_subsection:
                    body.append("</section>")
                body.append(
                    f'<section id="SEC-{block_key}"><h3 id="H-{block_key}">{content}</h3>'
                )
                open_subsection = True
            else:
                body.append(f'<p id="P-{block_key}">{content}</p>')
        chapter_note_ids = list(
            dict.fromkeys(
                note_id
                for note_id in note_order
                if note_id in notes and str(notes[note_id]["book_chapter_id"]) == chapter_id
            )
        )
        if chapter_note_ids:
            if open_subsection:
                body.append("</section>")
                open_subsection = False
            note_items = []
            for note_id in chapter_note_ids:
                note = notes[note_id]
                backlinks = [
                    str(fragment["reference_id"])
                    for block in chapter
                    for fragment in block["fragments"]
                    if fragment.get("footnote_id") == note_id
                ]
                backlink = (
                    f' <a href="#{html.escape(backlinks[0], quote=True)}" '
                    f'aria-label="本文へ戻る">↩</a>'
                    if backlinks
                    else ""
                )
                note_items.append(
                    f'<li id="{html.escape(note_id, quote=True)}" data-footnote-definition="true">'
                    f'{html.escape(note["translated"])}{backlink}</li>'
                )
            body.append(
                f'<section id="SEC-{chapter_key}-NOTES" data-footnote-placement="chapter-end">'
                f'<h3 id="H-{chapter_key}-NOTES">注</h3>'
                f'<ol id="LIST-{chapter_key}-NOTES">{"".join(note_items)}</ol></section>'
            )
        if open_subsection:
            body.append("</section>")
        rendered_sections.append(
            f'<section id="SEC-{chapter_key}" data-book-section="true">'
            f'<h2 id="H-{chapter_key}">{heading_content}</h2>{"".join(body)}</section>'
        )
    result = (
        f'<article id="DOC-{prefix}" lang="{html.escape(language, quote=True)}" '
        f'data-book-reflow="true">{"".join(rendered_sections)}</article>'
    )
    CanonicalHTMLValidator().assert_valid(result)
    return result


def _render_fragments(fragments: list[dict[str, Any]]) -> str:
    rendered: list[str] = []
    for fragment in fragments:
        rendered.append(html.escape(str(fragment["text"])))
        note_id = fragment.get("footnote_id")
        reference_id = fragment.get("reference_id")
        if note_id and reference_id:
            rendered.append(
                f'<sup id="{html.escape(str(reference_id), quote=True)}" '
                f'data-footnote-reference="true"><a '
                f'href="#{html.escape(str(note_id), quote=True)}" '
                f'aria-label="脚注">注</a></sup>'
            )
    return "".join(rendered)


def validate_translated_book(html_text: str, manifest: dict[str, Any]) -> dict[str, Any]:
    """Fail closed on lost notes, duplicate IDs, order drift, and source-format leakage."""
    issues = [dict(item) for item in manifest.get("issues") or []]
    try:
        root = ET.fromstring(html_text)
    except ET.ParseError as exc:
        return {"status": "blocked", "issues": [{"code": "book.invalid_html", "message": str(exc)}]}
    ids = [element.attrib["id"] for element in root.iter() if element.get("id")]
    duplicates = sorted(item for item, count in Counter(ids).items() if count > 1)
    for duplicate in duplicates:
        issues.append({"code": "book.duplicate_id", "message": duplicate})
    references = [
        element for element in root.iter("sup") if element.get("data-footnote-reference") == "true"
    ]
    definitions = [
        element for element in root.iter("li") if element.get("data-footnote-definition") == "true"
    ]
    actual_reference_ids = [str(element.get("id")) for element in references]
    actual_definition_ids = [str(element.get("id")) for element in definitions]
    expected_reference_ids = [str(item) for item in manifest.get("reference_ids") or []]
    expected_definition_ids = [str(item) for item in manifest.get("footnote_ids") or []]
    if actual_reference_ids != expected_reference_ids:
        issues.append(
            {
                "code": "book.reference_order_or_count",
                "message": (
                    f"expected {len(expected_reference_ids)}, "
                    f"found {len(actual_reference_ids)}"
                ),
            }
        )
    if actual_definition_ids != expected_definition_ids:
        issues.append(
            {
                "code": "book.definition_order_or_set",
                "message": (
                    f"expected {len(expected_definition_ids)}, "
                    f"found {len(actual_definition_ids)}"
                ),
            }
        )
    if len(actual_reference_ids) != len(actual_definition_ids):
        issues.append(
            {
                "code": "book.reference_definition_count",
                "message": (
                    f"references {len(actual_reference_ids)}, "
                    f"definitions {len(actual_definition_ids)}"
                ),
            }
        )
    target_ids = set(ids)
    for reference in references:
        links = list(reference.iter("a"))
        href = links[0].get("href", "") if links else ""
        if not href.startswith("#") or href[1:] not in target_ids:
            issues.append({"code": "book.orphan_reference", "message": href or "missing href"})
    referenced = {
        link.get("href", "")[1:]
        for reference in references
        for link in reference.iter("a")
        if link.get("href", "").startswith("#")
    }
    for definition_id in actual_definition_ids:
        if definition_id not in referenced:
            issues.append({"code": "book.orphan_definition", "message": definition_id})
    visible = "\n".join("".join(root.itertext()).splitlines())
    serialized = ET.tostring(root, encoding="unicode")
    leak_patterns = {
        "book.page_marker": r"(?:&lt;|<)!--\s*(?:page|ページ)\s+\d+",
        "book.markdown_heading": r"(?:^|[>\n])\s*#{1,6}\s+\S",
        "book.markdown_image": r"!\[[^\]]*\]\([^)]*\)",
        "book.horizontal_rule": r"(?:^|[>\n])\s*---\s*(?:$|[<\n])",
    }
    for code, pattern in leak_patterns.items():
        if re.search(pattern, serialized if code == "book.page_marker" else visible, re.IGNORECASE):
            issues.append({"code": code, "message": "source formatting leaked into reader text"})
    return {
        "status": "passed" if not issues else "blocked",
        "reference_count": len(actual_reference_ids),
        "definition_count": len(actual_definition_ids),
        "issues": issues,
        "discarded": dict(manifest.get("discarded") or {}),
    }


def assert_translated_book(html_text: str, manifest: dict[str, Any]) -> dict[str, Any]:
    report = validate_translated_book(html_text, manifest)
    if report["status"] != "passed":
        summary = "; ".join(
            f"{item['code']}: {item['message']}" for item in report["issues"][:8]
        )
        raise FukamiruError(f"Translation book quality gate blocked approval: {summary}")
    return report
