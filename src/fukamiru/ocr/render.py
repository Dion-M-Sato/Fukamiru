"""Canonical Wikipedia-style HTML renderer for OCR revisions."""

from __future__ import annotations

import html
from collections import defaultdict
from typing import Any

from fukamiru.documents.canonical_html import CanonicalHTMLValidator


class OCRCanonicalHTMLRenderer:
    def render(
        self,
        revision: dict[str, Any],
        *,
        title: str,
        bibliographic_lines: list[str] | None = None,
    ) -> str:
        revision_id = str(revision["id"])
        source_asset_id = str(revision["source_asset_id"])
        prefix = revision_id.replace("-", "")[:12]
        language = html.escape(str(revision.get("language") or "und"), quote=True)
        blocks = sorted(revision.get("blocks", []), key=lambda item: int(item["ordinal"]))
        boundaries_by_block: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for boundary in revision.get("boundaries", []):
            if boundary.get("block_id"):
                boundaries_by_block[str(boundary["block_id"])].append(boundary)
        references_by_block: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for reference in revision.get("note_references", []):
            if reference.get("block_id"):
                references_by_block[str(reference["block_id"])].append(reference)
        notes_by_id = {str(note["id"]): note for note in revision.get("notes", [])}
        blocks_by_id = {str(block["id"]): block for block in blocks}

        metadata = bibliographic_lines or []
        metadata_body = "".join(
            f'<p id="META-{prefix}-{index:04d}">{html.escape(line)}</p>'
            for index, line in enumerate(metadata, start=1)
        ) or f'<p id="META-{prefix}-0001">SourceAsset {html.escape(source_asset_id)}</p>'

        body_parts: list[str] = []
        toc_items: list[str] = []
        current_section: list[str] = []
        current_section_open = False
        section_number = 0

        def flush_section() -> None:
            nonlocal current_section_open
            if current_section:
                if current_section_open:
                    current_section.append("</section>")
                body_parts.append("".join(current_section))
                current_section.clear()
                current_section_open = False

        for block in blocks:
            block_type = str(block["block_type"])
            if block_type in {"footnote", "endnote"}:
                continue
            stable_id = html.escape(str(block["stable_block_id"]), quote=True)
            if block_type == "heading":
                flush_section()
                section_number += 1
                section_id = f"SEC-{stable_id}"
                heading_text = html.escape(str(block["text"]))
                toc_items.append(
                    f'<li id="TOC-{prefix}-{section_number:04d}">'
                    f'<a href="#{stable_id}">{heading_text}</a></li>'
                )
                current_section.append(
                    f'<section id="{section_id}"><h3 id="{stable_id}">{heading_text}</h3>'
                )
                current_section_open = True
                continue
            rendered = self._render_block_text(
                block,
                boundaries_by_block.get(str(block["id"]), []),
                references_by_block.get(str(block["id"]), []),
                notes_by_id,
                source_asset_id,
            )
            tag = "blockquote" if block_type == "quote" else "p"
            current_section.append(f'<{tag} id="{stable_id}">{rendered}</{tag}>')
        flush_section()
        if not body_parts:
            body_parts.append(f'<p id="P-{prefix}-EMPTY">No text blocks.</p>')
        body_html = "".join(body_parts)

        toc_body = "".join(toc_items) or (
            f'<li id="TOC-{prefix}-0001"><a href="#H-{prefix}-TEXT">Text</a></li>'
        )
        note_items: list[str] = []
        for note in revision.get("notes", []):
            stable_note_id = html.escape(str(note["stable_note_id"]), quote=True)
            body = blocks_by_id.get(str(note.get("body_block_id")))
            note_text = html.escape(str(body["text"]) if body else "Unresolved note")
            backlinks = [
                reference
                for reference in revision.get("note_references", [])
                if reference.get("note_id") == note["id"]
            ]
            backlink_html = "".join(
                f' <a href="#{html.escape(str(reference["stable_reference_id"]), quote=True)}" '
                f'aria-label="Back to note reference">↩</a>'
                for reference in backlinks
            )
            note_items.append(
                f'<li id="{stable_note_id}" data-note-kind="{html.escape(str(note["note_kind"]))}">'
                f'<span id="LABEL-{stable_note_id}" class="note-label">'
                f'{html.escape(str(note["display_label"]))}</span> {note_text}{backlink_html}</li>'
            )
        notes_section = ""
        if note_items:
            notes_section = (
                f'<section id="SEC-{prefix}-NOTES"><h2 id="H-{prefix}-NOTES">Notes</h2>'
                f'<ol id="LIST-{prefix}-NOTES">{"".join(note_items)}</ol></section>'
            )

        document = (
            f'<article id="DOC-{prefix}" lang="{language}" '
            f'data-ocr-revision-id="{html.escape(revision_id, quote=True)}">'
            f'<section id="SEC-{prefix}-TITLE"><h2 id="H-{prefix}-TITLE">'
            f'{html.escape(title)}</h2>{metadata_body}</section>'
            f'<section id="SEC-{prefix}-TOC"><h2 id="H-{prefix}-TOC">Contents</h2>'
            f'<ol id="LIST-{prefix}-TOC">{toc_body}</ol></section>'
            f'<section id="SEC-{prefix}-TEXT"><h2 id="H-{prefix}-TEXT">Text</h2>'
            f"{body_html}</section>{notes_section}</article>"
        )
        CanonicalHTMLValidator().assert_valid(document)
        return document

    @staticmethod
    def _render_block_text(
        block: dict[str, Any],
        boundaries: list[dict[str, Any]],
        references: list[dict[str, Any]],
        notes_by_id: dict[str, dict[str, Any]],
        source_asset_id: str,
    ) -> str:
        text = str(block["text"])
        insertions: dict[int, list[str]] = defaultdict(list)
        for boundary in boundaries:
            offset = int(boundary.get("offset_in_block") or 0)
            marker_id = html.escape(str(boundary["stable_marker_id"]), quote=True)
            page = int(boundary["page_number"])
            href = f"/api/source-assets/{html.escape(source_asset_id, quote=True)}/pages/{page}"
            insertions[offset].append(
                f'<span id="{marker_id}" class="source-page-boundary" '
                f'data-source-page="{page}" aria-label="Source page {page} begins">'
                f'<a href="{href}" data-source-page-link="true">p. {page}</a></span>'
            )
        for reference in references:
            offset = int(reference["offset_in_block"])
            reference_id = html.escape(str(reference["stable_reference_id"]), quote=True)
            note = notes_by_id.get(str(reference.get("note_id")))
            target = html.escape(str(note["stable_note_id"]), quote=True) if note else ""
            label = html.escape(str(reference["display_label"]))
            if target:
                reference_html = f'<a href="#{target}">{label}</a>'
            else:
                reference_html = f'<span class="unresolved-note">{label}</span>'
            insertions[offset].append(
                f'<sup id="{reference_id}" class="note-reference">'
                f"{reference_html}</sup>"
            )
        parts: list[str] = []
        previous = 0
        for offset in sorted(insertions):
            safe_offset = max(previous, min(offset, len(text)))
            parts.append(html.escape(text[previous:safe_offset]))
            parts.extend(insertions[offset])
            previous = safe_offset
        parts.append(html.escape(text[previous:]))
        return "".join(parts)
