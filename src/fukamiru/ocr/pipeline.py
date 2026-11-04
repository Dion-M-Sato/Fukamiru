"""Build immutable raw and normalized OCR layers without touching state.db."""

from __future__ import annotations

import dataclasses
import hashlib
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from typing import Any

from fukamiru.ocr.audit import (
    AuditPage,
    NoteCandidate,
    NoteReferenceCandidate,
    OCRAuditResult,
    OCRIntegrityAuditor,
)
from fukamiru.ocr.repository import (
    OCRBlockInput,
    OCRPageInput,
    OCRProvenanceInput,
    OCRRevisionRepository,
)


@dataclass(frozen=True)
class OCRPipelineResult:
    raw_revision_id: str
    normalized_revision_id: str
    normalized_pages: tuple[str, ...]
    audit: OCRAuditResult

    def serializable_audit(self) -> dict[str, object]:
        return {
            "findings": [dataclasses.asdict(finding) for finding in self.audit.findings],
            "boundary_contexts": [
                dataclasses.asdict(context) for context in self.audit.boundary_contexts
            ],
            "applied_repairs": list(self.audit.applied_repairs),
            "has_critical": self.audit.has_critical,
        }


class OCRPipelineService:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.revisions = OCRRevisionRepository(connection, autocommit=False)

    def process(
        self,
        *,
        source_asset_id: str,
        pages: list[str],
        language: str | None,
        structured_pages: list[dict[str, Any]] | None = None,
    ) -> OCRPipelineResult:
        self.connection.execute("SAVEPOINT ocr_pipeline")
        try:
            result = self._process(
                source_asset_id=source_asset_id,
                pages=pages,
                language=language,
                structured_pages=structured_pages,
            )
            self.connection.execute("RELEASE SAVEPOINT ocr_pipeline")
        except Exception:
            self.connection.execute("ROLLBACK TO SAVEPOINT ocr_pipeline")
            self.connection.execute("RELEASE SAVEPOINT ocr_pipeline")
            raise
        return result

    def _process(
        self,
        *,
        source_asset_id: str,
        pages: list[str],
        language: str | None,
        structured_pages: list[dict[str, Any]] | None = None,
    ) -> OCRPipelineResult:
        detected_notes, detected_references = self._detect_notes(
            pages, structured_pages=structured_pages
        )
        audit = OCRIntegrityAuditor().audit(
            [AuditPage(index, text) for index, text in enumerate(pages, start=1)],
            language=language,
            notes=[item["candidate"] for item in detected_notes],
            note_references=[item["candidate"] for item in detected_references],
        )
        if structured_pages:
            raw_content, raw_pages, raw_blocks = self._layout_revision_data(
                source_asset_id,
                structured_pages,
                language,
                normalized=False,
            )
        else:
            raw_content, raw_pages = self._page_ranges(pages)
            raw_blocks = [
                OCRBlockInput(
                    stable_block_id=self._block_id(source_asset_id, "RAW", index),
                    block_type="paragraph",
                    ordinal=index - 1,
                    start_char=page.start_char,
                    end_char=page.end_char,
                    text=page.text,
                    language=language,
                )
                for index, page in enumerate(raw_pages, start=1)
                if page.text
            ]
        raw_id = self.revisions.create_revision(
            source_asset_id=source_asset_id,
            layer="raw_ocr",
            content=raw_content,
            pages=raw_pages,
            blocks=raw_blocks,
            language=language,
            created_by="extractor",
            metadata={
                "page_count": len(pages),
                "layout_first": bool(structured_pages),
            },
        )

        if structured_pages:
            normalized_content, normalized_pages, normalized_blocks = (
                self._layout_revision_data(
                    source_asset_id,
                    structured_pages,
                    language,
                    normalized=True,
                )
            )
            normalized_texts = [page.text for page in normalized_pages]
        else:
            normalized_texts = [page.text for page in audit.normalized_pages]
            normalized_content, normalized_pages = self._page_ranges(normalized_texts)
            normalized_blocks = self._paragraph_blocks(
                source_asset_id, normalized_content, language
            )
        normalized_id = self.revisions.create_revision(
            source_asset_id=source_asset_id,
            layer="normalized_ocr",
            content=normalized_content,
            pages=normalized_pages,
            blocks=normalized_blocks,
            parent_revision_id=raw_id,
            language=language,
            created_by="deterministic",
            metadata={
                "audit_finding_count": len(audit.findings),
                "automatic_repair_count": len(audit.applied_repairs),
                "layout_first": bool(structured_pages),
            },
        )
        note_ids: dict[tuple[str, str], str] = {}
        note_statuses: dict[tuple[str, str], str] = {}
        reference_keys = {
            (item["candidate"].scope_id, item["candidate"].label)
            for item in detected_references
        }
        note_groups: dict[tuple[str, str], list[dict[str, object]]] = {}
        for item in detected_notes:
            candidate = item["candidate"]
            key = (candidate.scope_id, candidate.label)
            note_groups.setdefault(key, []).append(item)
        for key, candidates in note_groups.items():
            item = candidates[0]
            candidate = item["candidate"]
            ambiguous = len(candidates) > 1
            body_block = next(
                (
                    block
                    for block in normalized_blocks
                    if str(item["body_text"]) in block.text
                ),
                None,
            ) if not ambiguous else None
            status = (
                "ambiguous"
                if ambiguous
                else "resolved"
                if key in reference_keys
                else "orphan"
            )
            metadata: dict[str, object] = {}
            if ambiguous:
                metadata = {
                    "ambiguity_reason": "duplicate_scope_label",
                    "candidate_count": len(candidates),
                    "candidates": [
                        {
                            "page_number": entry["candidate"].page_number,
                            "body_text": str(entry["body_text"]),
                            "structural_role": str(
                                entry.get("structural_role") or "unknown"
                            ),
                        }
                        for entry in candidates
                    ],
                }
            note_id = self.revisions.add_note(
                revision_id=normalized_id,
                stable_note_id=self._note_id(
                    source_asset_id, candidate.scope_id, candidate.label
                ),
                note_kind=str(item["note_kind"]),
                scope_id=candidate.scope_id,
                display_label=candidate.label,
                status=status,
                body_stable_block_id=body_block.stable_block_id if body_block else None,
                source_page_number=candidate.page_number,
                metadata=metadata,
            )
            note_ids[key] = note_id
            note_statuses[key] = status
        for ordinal, item in enumerate(detected_references, start=1):
            candidate = item["candidate"]
            block = next(
                (
                    block
                    for block in normalized_blocks
                    if str(item["context_text"]) in block.text
                ),
                None,
            )
            if block is None:
                continue
            marker = str(item["marker"])
            offset = block.text.find(marker)
            if offset < 0:
                continue
            key = (candidate.scope_id, candidate.label)
            self.revisions.add_note_reference(
                revision_id=normalized_id,
                stable_reference_id=self._note_reference_id(
                    source_asset_id, candidate.scope_id, candidate.label, ordinal
                ),
                display_label=candidate.label,
                offset_in_block=offset,
                status=note_statuses.get(key, "orphan"),
                note_id=note_ids.get(key),
                block_stable_id=block.stable_block_id,
            )
        provenance_links = []
        for block in normalized_blocks:
            source_start = min(block.start_char, len(raw_content))
            source_end = min(max(source_start, block.end_char), len(raw_content))
            provenance_links.append(OCRProvenanceInput(
                descendant_stable_block_id=block.stable_block_id,
                descendant_start_char=block.start_char,
                descendant_end_char=block.end_char,
                source_start_char=source_start,
                source_end_char=source_end,
                relation_type="normalized_from",
                confidence=1.0 if not audit.applied_repairs else 0.9,
            ))
        self.revisions.add_provenance_batch(
            descendant_revision_id=normalized_id,
            source_revision_id=raw_id,
            links=provenance_links,
        )
        return OCRPipelineResult(
            raw_revision_id=raw_id,
            normalized_revision_id=normalized_id,
            normalized_pages=tuple(normalized_texts),
            audit=audit,
        )

    @staticmethod
    def _layout_revision_data(
        source_asset_id: str,
        structured_pages: list[dict[str, Any]],
        language: str | None,
        *,
        normalized: bool,
    ) -> tuple[str, list[OCRPageInput], list[OCRBlockInput]]:
        content_parts: list[str] = []
        revision_pages: list[OCRPageInput] = []
        revision_blocks: list[OCRBlockInput] = []
        cursor = 0
        ordinal = 0
        type_map = {
            "title": "title",
            "heading": "heading",
            "toc_entry": "list_item",
            "paragraph": "paragraph",
            "quote": "quote",
            "list_item": "list_item",
            "footnote": "footnote",
            "endnote": "endnote",
        }
        for fallback_page, page in enumerate(structured_pages, start=1):
            page_number = int(page.get("page_number") or fallback_page)
            page_start = cursor
            page_parts: list[str] = []
            for raw_block in page.get("blocks", []):
                role = str(raw_block.get("structural_role") or "other")
                translatable = bool(raw_block.get("translatable", True))
                if normalized and role in {"page_header", "page_footer", "page_number"}:
                    continue
                text = str(raw_block.get("text") or "").strip()
                if normalized:
                    text = unicodedata.normalize(
                        "NFC", text.replace("\r\n", "\n").replace("\r", "\n")
                    )
                    text = re.sub(r"(?<![.!?。！？:;])\n(?=[a-zà-öø-ÿ])", " ", text)
                if not text:
                    continue
                if page_parts:
                    page_parts.append("\n\n")
                    cursor += 2
                start = cursor
                page_parts.append(text)
                cursor += len(text)
                attributes = dict(raw_block.get("attributes") or {})
                attributes.update(
                    {
                        "structural_role": role,
                        "layout_block_id": str(raw_block.get("stable_block_id") or ""),
                        "source_page_number": page_number,
                        "bbox": [
                            float(raw_block.get("x0") or 0.0),
                            float(raw_block.get("y0") or 0.0),
                            float(raw_block.get("x1") or 0.0),
                            float(raw_block.get("y1") or 0.0),
                        ],
                        "translatable": translatable,
                    }
                )
                stable_id = str(raw_block.get("stable_block_id") or "") or (
                    OCRPipelineService._block_id(
                        source_asset_id, "PDF", ordinal + 1
                    )
                )
                revision_blocks.append(
                    OCRBlockInput(
                        stable_block_id=stable_id,
                        block_type=type_map.get(role, "other"),
                        ordinal=ordinal,
                        start_char=start,
                        end_char=cursor,
                        text=text,
                        language=language,
                        attributes=attributes,
                    )
                )
                ordinal += 1
            page_text = "".join(page_parts)
            content_parts.append(page_text)
            revision_pages.append(
                OCRPageInput(
                    page_number,
                    page_start,
                    cursor,
                    page_text,
                    printed_label=(
                        str(page["printed_label"])
                        if page.get("printed_label") is not None
                        else str(page_number)
                    ),
                )
            )
        return "".join(content_parts), revision_pages, revision_blocks

    @staticmethod
    def _page_ranges(page_texts: list[str]) -> tuple[str, list[OCRPageInput]]:
        content = "".join(page_texts)
        pages: list[OCRPageInput] = []
        cursor = 0
        for number, text in enumerate(page_texts, start=1):
            end = cursor + len(text)
            pages.append(OCRPageInput(number, cursor, end, text, printed_label=str(number)))
            cursor = end
        return content, pages

    @staticmethod
    def _paragraph_blocks(
        source_asset_id: str, content: str, language: str | None
    ) -> list[OCRBlockInput]:
        blocks: list[OCRBlockInput] = []

        def append_block(start: int, end: int) -> None:
            block_text = content[start:end]
            note_body = re.match(
                r"^\s*(?:\[(\d{1,3})\]|(\d{1,3})[.)])\s+\S", block_text
            )
            ordinal = len(blocks)
            blocks.append(
                OCRBlockInput(
                    stable_block_id=OCRPipelineService._block_id(
                        source_asset_id, "P", ordinal + 1
                    ),
                    block_type="footnote" if note_body else "paragraph",
                    ordinal=ordinal,
                    start_char=start,
                    end_char=end,
                    text=block_text,
                    language=language,
                )
            )

        # Scan each character at most once. A DOTALL paragraph regex can spend
        # minutes backtracking over long OCR output without blank lines, holding
        # the GIL and preventing the API liveness probe from being served.
        start: int | None = None
        end = 0
        cursor = 0
        length = len(content)
        while cursor < length:
            line_end = content.find("\n", cursor)
            if line_end < 0:
                line_end = length
            first = cursor
            while first < line_end and content[first].isspace():
                first += 1
            if first == line_end:
                if start is not None:
                    append_block(start, end)
                    start = None
            else:
                last = line_end
                while content[last - 1].isspace():
                    last -= 1
                if start is None:
                    start = first
                end = last
            cursor = line_end + 1
        if start is not None:
            append_block(start, end)
        return blocks

    @staticmethod
    def _detect_notes(
        pages: list[str],
        *,
        structured_pages: list[dict[str, Any]] | None = None,
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        if structured_pages is not None:
            return OCRPipelineService._detect_structured_notes(structured_pages)
        notes: list[dict[str, object]] = []
        references: list[dict[str, object]] = []
        for page_number, page_text in enumerate(pages, start=1):
            is_endnote_page = bool(
                re.match(r"\s*(?:notes?|endnotes?|注|註)\s*(?:\n|$)", page_text, re.I)
            )
            scope = "document-endnotes" if is_endnote_page else f"page-{page_number}"
            body_ranges: list[tuple[int, int]] = []
            for match in re.finditer(
                r"(?m)^\s*(?:\[(\d{1,3})\]|(\d{1,3})[.)])\s+(.+?)\s*$",
                page_text,
            ):
                label = match.group(1) or match.group(2)
                body_ranges.append(match.span())
                notes.append(
                    {
                        "candidate": NoteCandidate(
                            scope, label, page_number, "endnote" if is_endnote_page else "footnote"
                        ),
                        "body_text": match.group(0).strip(),
                        "note_kind": "source_endnote" if is_endnote_page else "source_footnote",
                    }
                )
            for match in re.finditer(r"\[(\d{1,3})\]", page_text):
                if any(start <= match.start() < end for start, end in body_ranges):
                    continue
                label = match.group(1)
                references.append(
                    {
                        "candidate": NoteReferenceCandidate(scope, label, page_number),
                        "marker": match.group(0),
                        "context_text": next(
                            (
                                paragraph
                                for paragraph in re.split(r"\n\s*\n", page_text)
                                if match.group(0) in paragraph
                            ),
                            match.group(0),
                        ).strip(),
                    }
                )
        endnote_labels = {
            item["candidate"].label
            for item in notes
            if item["note_kind"] == "source_endnote"
        }
        for item in references:
            candidate = item["candidate"]
            if candidate.label in endnote_labels:
                item["candidate"] = NoteReferenceCandidate(
                    "document-endnotes", candidate.label, candidate.page_number
                )
        return notes, references

    @staticmethod
    def _detect_structured_notes(
        structured_pages: list[dict[str, Any]],
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        notes: list[dict[str, object]] = []
        references: list[dict[str, object]] = []
        excluded_reference_roles = {
            "title",
            "heading",
            "toc_entry",
            "list_item",
            "page_header",
            "page_footer",
            "page_number",
            "footnote",
            "endnote",
        }
        for fallback_page, page in enumerate(structured_pages, start=1):
            page_number = int(page.get("page_number") or fallback_page)
            for raw_block in page.get("blocks", []):
                role = str(raw_block.get("structural_role") or "other").casefold()
                block_text = str(raw_block.get("text") or "").strip()
                if not block_text:
                    continue
                if role in {"footnote", "endnote"}:
                    scope = "document-endnotes" if role == "endnote" else f"page-{page_number}"
                    for match in re.finditer(
                        r"(?m)^\s*(?:\[(\d{1,3})\]|(\d{1,3})[.)])\s+(.+?)\s*$",
                        block_text,
                    ):
                        label = match.group(1) or match.group(2)
                        notes.append(
                            {
                                "candidate": NoteCandidate(
                                    scope,
                                    label,
                                    page_number,
                                    "endnote" if role == "endnote" else "footnote",
                                ),
                                "body_text": match.group(0).strip(),
                                "note_kind": (
                                    "source_endnote"
                                    if role == "endnote"
                                    else "source_footnote"
                                ),
                                "structural_role": role,
                            }
                        )
                    continue
                if role in excluded_reference_roles:
                    continue
                for match in re.finditer(r"\[(\d{1,3})\]", block_text):
                    references.append(
                        {
                            "candidate": NoteReferenceCandidate(
                                f"page-{page_number}", match.group(1), page_number
                            ),
                            "marker": match.group(0),
                            "context_text": block_text,
                        }
                    )
        endnote_labels = {
            item["candidate"].label
            for item in notes
            if item["note_kind"] == "source_endnote"
        }
        for item in references:
            candidate = item["candidate"]
            if candidate.label in endnote_labels:
                item["candidate"] = NoteReferenceCandidate(
                    "document-endnotes", candidate.label, candidate.page_number
                )
        return notes, references

    @staticmethod
    def _note_id(source_asset_id: str, scope: str, label: str) -> str:
        digest = hashlib.sha256(f"{source_asset_id}:{scope}:{label}".encode()).hexdigest()[:16]
        return f"NOTE-{digest}"

    @staticmethod
    def _note_reference_id(
        source_asset_id: str, scope: str, label: str, ordinal: int
    ) -> str:
        digest = hashlib.sha256(
            f"{source_asset_id}:{scope}:{label}:{ordinal}".encode()
        ).hexdigest()[:16]
        return f"NOTEREF-{digest}"

    @staticmethod
    def _block_id(source_asset_id: str, kind: str, ordinal: int) -> str:
        digest = hashlib.sha256(f"{source_asset_id}:{kind}:{ordinal}".encode()).hexdigest()[:16]
        return f"{kind}-{digest}"
