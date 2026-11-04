"""Canonical HTML rendering for contract-bound authoring drafts."""

from __future__ import annotations

import html
import re
import unicodedata
from typing import Any
from xml.etree import ElementTree as ET

from fukamiru.documents.canonical_html import CanonicalHTMLValidator
from fukamiru.errors import FukamiruError


def _identifier(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.:-]+", "-", value).strip("-")
    return normalized or "generated"


def _render_citation_mentions(text: str, mentions: list[dict[str, Any]]) -> str:
    spans: list[tuple[int, int, str]] = []
    for mention in mentions:
        reference_id = str(mention.get("reference_id") or "").strip()
        visible_text = str(mention.get("text") or "")
        if not reference_id or not visible_text:
            raise FukamiruError("Citation mentions require a reference ID and visible title")
        matches = [
            index for index in range(len(text)) if text.startswith(visible_text, index)
        ]
        if len(matches) != 1:
            raise FukamiruError(
                "Citation title must occur exactly once in its generated paragraph: "
                + reference_id
            )
        start = matches[0]
        spans.append((start, start + len(visible_text), reference_id))
    spans.sort()
    if any(left[1] > right[0] for left, right in zip(spans, spans[1:], strict=False)):
        raise FukamiruError("Generated citation title spans must not overlap")
    rendered: list[str] = []
    cursor = 0
    for start, end, reference_id in spans:
        rendered.append(html.escape(text[cursor:start]))
        rendered.append(
            f'<cite data-reference-id="{html.escape(reference_id, quote=True)}">'
            f"{html.escape(text[start:end])}</cite>"
        )
        cursor = end
    rendered.append(html.escape(text[cursor:]))
    return "".join(rendered)


def citation_mention_render_failures(
    sections: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Locate citation spans that cannot be represented as distinct cite tags."""

    failures: list[dict[str, Any]] = []
    for section in sections:
        section_id = str(section.get("section_id") or "")
        for paragraph_index, paragraph in enumerate(section.get("paragraphs") or [], 1):
            if not isinstance(paragraph, dict) or not paragraph.get("citation_mentions"):
                continue
            try:
                _render_citation_mentions(
                    str(paragraph.get("text") or ""),
                    list(paragraph["citation_mentions"]),
                )
            except FukamiruError as exc:
                failures.append({
                    "section_id": section_id,
                    "paragraph_index": paragraph_index,
                    "paragraph_id": str(paragraph.get("paragraph_id") or ""),
                    "error": str(exc),
                })
    return failures


_CITATION_QUOTES = {
    "『": "』", "「": "」", "《": "》", "〈": "〉",
    "“": "”", "‘": "’", '"': '"', "'": "'",
}
_CITATION_IGNORED = frozenset("『』「」《》〈〉“”‘’\"'")


def _compact_with_offsets(value: str) -> tuple[str, list[int]]:
    compacted: list[str] = []
    offsets: list[int] = []
    for index, character in enumerate(value):
        normalized = unicodedata.normalize("NFKC", character).casefold()
        for item in normalized:
            if item.isspace() or item in _CITATION_IGNORED:
                continue
            compacted.append(item)
            offsets.append(index)
    return "".join(compacted), offsets


def _title_spans(value: str, title: str) -> list[tuple[int, int, str]]:
    haystack, offsets = _compact_with_offsets(value)
    needle, _ = _compact_with_offsets(title)
    if not needle:
        return []
    spans: list[tuple[int, int, str]] = []
    cursor = 0
    while (start := haystack.find(needle, cursor)) >= 0:
        end = start + len(needle)
        raw_start, raw_end = offsets[start], offsets[end - 1] + 1
        visible_start, visible_end = raw_start, raw_end
        if visible_start and visible_end < len(value):
            opener = value[visible_start - 1]
            if opener in _CITATION_QUOTES and value[visible_end] == _CITATION_QUOTES[opener]:
                visible_start -= 1
                visible_end += 1
        spans.append((visible_start, visible_end, value[visible_start:visible_end]))
        cursor = end
    return spans


def recover_citation_mentions(
    sections: list[dict[str, Any]], citation_anchors: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Restore only uniquely resolvable work-title to reference-ID bindings.

    Ambiguous or absent required titles are returned as paragraph-level findings;
    no reference is guessed from author names or list order.
    """
    anchors_by_section: dict[str, list[dict[str, Any]]] = {}
    for anchor in citation_anchors:
        if not isinstance(anchor, dict):
            continue
        section_id = str(anchor.get("section_id") or "")
        reference_id = str(anchor.get("reference_id") or "").strip()
        work = str(anchor.get("work") or "").strip()
        if section_id and reference_id and work:
            anchors_by_section.setdefault(section_id, []).append(dict(anchor))

    updated_sections: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    corrections: list[dict[str, Any]] = []
    for section in sections:
        section_id = str(section.get("section_id") or "")
        anchors = anchors_by_section.get(section_id, [])
        updated_section = dict(section)
        updated_paragraphs: list[dict[str, Any]] = []
        for paragraph_index, raw_paragraph in enumerate(section.get("paragraphs") or [], 1):
            if not isinstance(raw_paragraph, dict):
                updated_paragraphs.append(raw_paragraph)
                continue
            paragraph = dict(raw_paragraph)
            text = str(paragraph.get("text") or "")
            old_mentions = paragraph.get("citation_mentions") or []
            old_records = [dict(item) for item in old_mentions if isinstance(item, dict)]
            invalid_old_mentions = (
                not isinstance(old_mentions, list)
                or len(old_records) != len(old_mentions)
            )
            evidence_ids = {
                str(item) for item in paragraph.get("evidence_ids") or [] if str(item)
            }
            matched: dict[tuple[int, int], dict[str, dict[str, Any]]] = {}
            repeated: list[dict[str, Any]] = []
            for anchor in anchors:
                spans = _title_spans(text, str(anchor.get("work") or ""))
                if len(spans) > 1:
                    repeated.append(anchor)
                for start, end, _visible in spans:
                    ref_id = str(anchor.get("reference_id") or "")
                    matched.setdefault((start, end), {}).setdefault(ref_id, anchor)

            mentions: list[dict[str, str]] = []
            ambiguous_spans: set[tuple[int, int]] = set()
            for span, references in matched.items():
                paragraph_references = {
                    reference_id: anchor
                    for reference_id, anchor in references.items()
                }
                linked_references = {
                    reference_id: anchor
                    for reference_id, anchor in references.items()
                    if str(anchor.get("evidence_id") or "") in evidence_ids
                    or str(anchor.get("citation_evidence_id") or "") in evidence_ids
                }
                applicable = (
                    linked_references
                    if len(linked_references) == 1
                    else paragraph_references
                )
                if len(applicable) != 1:
                    ambiguous_spans.add(span)
                    continue
                reference_id = next(iter(applicable))
                mentions.append(
                    {"reference_id": reference_id, "text": text[span[0]:span[1]]}
                )
            repeated_reference_ids = {
                str(anchor.get("reference_id") or "") for anchor in repeated
            }
            if repeated_reference_ids:
                mentions = [
                    item for item in mentions
                    if item["reference_id"] not in repeated_reference_ids
                ]

            ordered = sorted(matched)
            for left, right in zip(ordered, ordered[1:], strict=False):
                if left[1] > right[0]:
                    ambiguous_spans.update((left, right))
            if ambiguous_spans:
                mentions = [
                    item for item in mentions
                    if not any(
                        item["text"] == text[start:end]
                        for start, end in ambiguous_spans
                    )
                ]

            mentioned_ids = {item["reference_id"] for item in mentions}
            required = {
                str(anchor.get("reference_id") or ""): anchor
                for anchor in anchors
                if str(anchor.get("evidence_id") or "") in evidence_ids
                or str(anchor.get("citation_evidence_id") or "") in evidence_ids
            }
            missing = sorted(set(required) - mentioned_ids)
            if repeated or ambiguous_spans or missing or invalid_old_mentions:
                involved = {
                    **required,
                    **{
                        reference_id: anchor
                        for references in matched.values()
                        for reference_id, anchor in references.items()
                    },
                }
                findings.append(
                    {
                        "section_id": section_id,
                        "paragraph_index": paragraph_index,
                        "paragraph_id": str(paragraph.get("paragraph_id") or ""),
                        "category": "citation_title_mapping_unresolved",
                        "reference_ids": sorted(involved),
                        "required_reference_ids": missing,
                        "source_ids": sorted(
                            {
                                *involved,
                                *{
                                    str(anchor.get(field) or "")
                                    for anchor in involved.values()
                                    for field in ("evidence_id", "citation_evidence_id")
                                    if str(anchor.get(field) or "")
                                },
                            }
                        ),
                        "visible_text": text,
                        "original_mentions": old_records,
                        "reason": (
                            "書名が見つからない、複数箇所にある、または書誌IDを一意に決められません。"
                        ),
                    }
                )
            if mentions != [dict(item) for item in old_mentions if isinstance(item, dict)]:
                corrections.extend(
                    {
                        "section_id": section_id,
                        "paragraph_index": paragraph_index,
                        "reference_id": item["reference_id"],
                        "visible_text": item["text"],
                    }
                    for item in mentions
                )
            paragraph["citation_mentions"] = mentions
            updated_paragraphs.append(paragraph)
        updated_section["paragraphs"] = updated_paragraphs
        updated_sections.append(updated_section)
    return updated_sections, findings, corrections


def _wrap_citation_mention(
    paragraph: ET.Element, *, reference_id: str, visible_text: str
) -> bool:
    existing = [
        cite
        for cite in paragraph.iter("cite")
        if cite.get("data-reference-id") == reference_id
    ]
    if existing:
        if len(existing) == 1 and "".join(existing[0].itertext()) == visible_text:
            return False
        raise FukamiruError(
            "Saved citation markup conflicts with its reference ID: " + reference_id
        )

    matches: list[tuple[ET.Element, str, int]] = []
    for node in paragraph.iter():
        for slot in ("text", "tail"):
            if slot == "tail" and node is paragraph:
                continue
            value = str(getattr(node, slot) or "")
            search_from = 0
            while True:
                start = value.find(visible_text, search_from)
                if start < 0:
                    break
                matches.append((node, slot, start))
                search_from = start + 1
    if len(matches) != 1:
        raise FukamiruError(
            "Citation title must occur exactly once in the saved paragraph: "
            + str(paragraph.get("id") or reference_id)
        )
    node, slot, start = matches[0]
    if slot == "text" and node.tag in {"cite", "a", "code", "q", "sup"}:
        raise FukamiruError("Citation title cannot be nested in protected inline markup")
    value = str(getattr(node, slot) or "")
    before = value[:start]
    after = value[start + len(visible_text) :]
    cite = ET.Element("cite", {"data-reference-id": reference_id})
    cite.text = visible_text
    cite.tail = after
    setattr(node, slot, before)
    if slot == "text":
        node.insert(0, cite)
        return True
    parent = next(candidate for candidate in paragraph.iter() if node in list(candidate))
    parent.insert(list(parent).index(node) + 1, cite)
    return True


def annotate_citation_mentions(
    canonical_html: str,
    *,
    plan_id: str,
    sections: list[dict[str, Any]],
) -> str:
    """Restore semantic book-title tags after later prose-only HTML edits."""

    if not sections or not any(
        isinstance(paragraph, dict) and paragraph.get("citation_mentions")
        for section in sections
        if isinstance(section, dict)
        for paragraph in section.get("paragraphs") or []
    ):
        return canonical_html
    root = ET.fromstring(canonical_html)
    prefix = _identifier(plan_id)
    changed = False
    for section in sections:
        if not isinstance(section, dict):
            continue
        section_key = _identifier(str(section.get("section_id") or ""))
        for paragraph_index, draft_paragraph in enumerate(
            section.get("paragraphs") or [], start=1
        ):
            if not isinstance(draft_paragraph, dict):
                continue
            mentions = [
                item
                for item in draft_paragraph.get("citation_mentions") or []
                if isinstance(item, dict)
            ]
            if not mentions:
                continue
            block_id = f"P-{prefix}-{section_key}-{paragraph_index:03d}"
            paragraph = next(
                (element for element in root.iter("p") if element.get("id") == block_id),
                None,
            )
            if paragraph is None:
                raise FukamiruError(
                    "Citation mention paragraph is missing from final HTML: " + block_id
                )
            visible = "".join(paragraph.itertext())
            spans: list[tuple[int, int]] = []
            for mention in mentions:
                reference_id = str(mention.get("reference_id") or "").strip()
                visible_text = str(mention.get("text") or "")
                if not reference_id or not visible_text:
                    raise FukamiruError(
                        "Citation mentions require a reference ID and visible title"
                    )
                matches = [
                    index
                    for index in range(len(visible))
                    if visible.startswith(visible_text, index)
                ]
                if len(matches) != 1:
                    raise FukamiruError(
                        "Citation title must occur exactly once in final HTML: " + block_id
                    )
                spans.append((matches[0], matches[0] + len(visible_text)))
            ordered_spans = sorted(spans)
            if any(
                left[1] > right[0]
                for left, right in zip(ordered_spans, ordered_spans[1:], strict=False)
            ):
                raise FukamiruError("Final citation title spans must not overlap: " + block_id)
            for mention in mentions:
                changed = _wrap_citation_mention(
                    paragraph,
                    reference_id=str(mention["reference_id"]),
                    visible_text=str(mention["text"]),
                ) or changed
    if not changed:
        return canonical_html
    result = ET.tostring(root, encoding="unicode", short_empty_elements=False)
    CanonicalHTMLValidator().assert_valid(result)
    return result


def render_canonical_html(
    *,
    plan_id: str,
    sections: list[dict[str, Any]],
    language: str = "ja",
) -> str:
    prefix = _identifier(plan_id)
    body: list[str] = []
    for section_index, section in enumerate(sections, start=1):
        section_key = _identifier(str(section.get("section_id") or section_index))
        section_id = f"SEC-{prefix}-{section_key}"
        heading_id = f"H-{prefix}-{section_key}"
        title = html.escape(str(section.get("title") or f"Section {section_index}"))
        article_role = str(section.get("article_role") or "")
        role_attribute = (
            f' data-authoring-role="{html.escape(article_role)}"'
            if article_role in {"introduction", "body", "conclusion"}
            else ""
        )
        paragraph_records = section.get("paragraphs")
        if isinstance(paragraph_records, list):
            rendered_parts: list[str] = []
            for paragraph_index, paragraph_record in enumerate(paragraph_records, start=1):
                if not isinstance(paragraph_record, dict):
                    continue
                paragraph = str(paragraph_record.get("text") or "").strip()
                if not paragraph:
                    continue
                citation_mentions = [
                    item
                    for item in paragraph_record.get("citation_mentions") or []
                    if isinstance(item, dict)
                ]
                rendered_parts.append(
                    f'<p id="P-{prefix}-{section_key}-{paragraph_index:03d}">'
                    f'{_render_citation_mentions(paragraph, citation_mentions)}</p>'
                )
            rendered = "".join(rendered_parts)
        else:
            raw = str(section.get("prose") or section.get("text") or "").strip()
            paragraphs = [part.strip() for part in re.split(r"\n\s*\n+", raw) if part.strip()]
            rendered = "".join(
                f'<p id="P-{prefix}-{section_key}-{paragraph_index:03d}">'
                f'{html.escape(paragraph)}</p>'
                for paragraph_index, paragraph in enumerate(paragraphs, start=1)
            )
        if not rendered:
            rendered = (
                f'<p id="P-{prefix}-{section_key}-001">'
                "No prose was generated for this section.</p>"
            )
        body.append(
            f'<section id="{section_id}"><h2 id="{heading_id}"{role_attribute}>'
            f'{title}</h2>{rendered}</section>'
        )
    canonical = (
        f'<article id="DOC-{prefix}" lang="{html.escape(language)}">'
        + "".join(body)
        + "</article>"
    )
    CanonicalHTMLValidator().assert_valid(canonical)
    return canonical
