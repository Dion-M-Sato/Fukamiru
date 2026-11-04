"""Persistence for immutable OCR text layers and source-page provenance."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from fukamiru.errors import FukamiruError
from fukamiru.storage.runs import utc_now

OCRLayer = Literal[
    "raw_ocr",
    "normalized_ocr",
    "human_corrected_ocr",
    "translation_source",
    "translated_text",
]


@dataclass(frozen=True)
class OCRPageInput:
    page_number: int
    start_char: int
    end_char: int
    text: str
    printed_label: str | None = None
    source_page_id: str | None = None


@dataclass(frozen=True)
class OCRBlockInput:
    stable_block_id: str
    block_type: str
    ordinal: int
    start_char: int
    end_char: int
    text: str
    language: str | None = None
    human_protected: bool = False
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OCRProvenanceInput:
    descendant_start_char: int
    descendant_end_char: int
    source_start_char: int
    source_end_char: int
    relation_type: str
    confidence: float
    descendant_stable_block_id: str | None = None
    source_page_number: int | None = None
    source_span_id: str | None = None


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stable_id(prefix: str, *parts: object) -> str:
    digest = hashlib.sha256("\x1f".join(str(part) for part in parts).encode()).hexdigest()[:20]
    return f"{prefix}-{digest}"


class OCRRevisionRepository:
    def __init__(
        self, connection: sqlite3.Connection, *, autocommit: bool = True
    ) -> None:
        self.connection = connection
        self.autocommit = autocommit

    def create_revision(
        self,
        *,
        source_asset_id: str,
        layer: OCRLayer,
        content: str,
        pages: list[OCRPageInput],
        blocks: list[OCRBlockInput],
        parent_revision_id: str | None = None,
        language: str | None = None,
        created_by: str = "deterministic",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        self._validate_source_and_parent(source_asset_id, parent_revision_id)
        self._validate_ranges(content, pages, blocks)
        if parent_revision_id is not None and created_by != "human":
            protected = {
                str(row["stable_block_id"]): str(row["text"])
                for row in self.connection.execute(
                    """
                    SELECT stable_block_id, text FROM ocr_blocks
                    WHERE revision_id = ? AND is_human_protected = 1
                    """,
                    (parent_revision_id,),
                )
            }
            proposed = {block.stable_block_id: block.text for block in blocks}
            changed = [
                block_id
                for block_id, text in protected.items()
                if proposed.get(block_id) != text
            ]
            if changed:
                raise FukamiruError(
                    "Agent OCR revision changes protected human blocks: "
                    + ", ".join(sorted(changed))
                )
        revision_id = str(uuid.uuid4())
        now = utc_now()
        if self.autocommit:
            self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                """
                INSERT INTO ocr_text_revisions(
                    id, source_asset_id, parent_revision_id, layer, content_text,
                    content_sha256, language, created_by, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    revision_id,
                    source_asset_id,
                    parent_revision_id,
                    layer,
                    content,
                    _sha256(content),
                    language,
                    created_by,
                    json.dumps(metadata or {}, ensure_ascii=False, separators=(",", ":")),
                    now,
                ),
            )
            page_ids: dict[int, str] = {}
            for page in pages:
                page_id = str(uuid.uuid4())
                page_ids[page.page_number] = page_id
                self.connection.execute(
                    """
                    INSERT INTO ocr_revision_pages(
                        id, revision_id, page_number, printed_label, source_page_id,
                        start_char, end_char, page_text, page_text_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        page_id,
                        revision_id,
                        page.page_number,
                        page.printed_label,
                        page.source_page_id,
                        page.start_char,
                        page.end_char,
                        page.text,
                        _sha256(page.text),
                    ),
                )
            block_ids: dict[str, str] = {}
            for block in blocks:
                block_id = str(uuid.uuid4())
                block_ids[block.stable_block_id] = block_id
                self.connection.execute(
                    """
                    INSERT INTO ocr_blocks(
                        id, revision_id, stable_block_id, block_type, ordinal,
                        start_char, end_char, text, language, is_human_protected,
                        attributes_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        block_id,
                        revision_id,
                        block.stable_block_id,
                        block.block_type,
                        block.ordinal,
                        block.start_char,
                        block.end_char,
                        block.text,
                        block.language,
                        int(block.human_protected),
                        json.dumps(block.attributes, ensure_ascii=False, separators=(",", ":")),
                    ),
                )
            for page in pages:
                containing = next(
                    (
                        block
                        for block in blocks
                        if block.start_char <= page.start_char <= block.end_char
                    ),
                    None,
                )
                marker_id = _stable_id("PB", source_asset_id, page.page_number)
                self.connection.execute(
                    """
                    INSERT INTO ocr_page_boundaries(
                        id, revision_id, revision_page_id, stable_marker_id,
                        page_number, char_offset, block_id, offset_in_block
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        revision_id,
                        page_ids[page.page_number],
                        marker_id,
                        page.page_number,
                        page.start_char,
                        block_ids.get(containing.stable_block_id) if containing else None,
                        page.start_char - containing.start_char if containing else None,
                    ),
                )
            if self.autocommit:
                self.connection.commit()
        except Exception:
            if self.autocommit:
                self.connection.rollback()
            raise
        return revision_id

    def create_human_correction(
        self,
        *,
        parent_revision_id: str,
        corrections: dict[str, str],
        message: str = "",
    ) -> str:
        if not corrections:
            raise FukamiruError("At least one OCR block correction is required")
        parent = self.snapshot(parent_revision_id)
        parent_blocks = sorted(parent["blocks"], key=lambda item: int(item["start_char"]))
        known = {str(block["stable_block_id"]) for block in parent_blocks}
        unknown = sorted(set(corrections) - known)
        if unknown:
            raise FukamiruError("Unknown OCR blocks: " + ", ".join(unknown))
        edits = [
            (
                int(block["start_char"]),
                int(block["end_char"]),
                corrections[str(block["stable_block_id"])],
            )
            for block in parent_blocks
            if str(block["stable_block_id"]) in corrections
        ]
        content = str(parent["content_text"])
        for start, end, replacement in reversed(edits):
            content = content[:start] + replacement + content[end:]

        def mapped(position: int) -> int:
            delta = 0
            for start, end, replacement in edits:
                if position >= end:
                    delta += len(replacement) - (end - start)
                    continue
                if position <= start:
                    break
                return start + delta + min(position - start, len(replacement))
            return position + delta

        pages = [
            OCRPageInput(
                page_number=int(page["page_number"]),
                start_char=mapped(int(page["start_char"])),
                end_char=mapped(int(page["end_char"])),
                text=content[
                    mapped(int(page["start_char"])) : mapped(int(page["end_char"]))
                ],
                printed_label=page["printed_label"],
                source_page_id=page["source_page_id"],
            )
            for page in parent["pages"]
        ]
        blocks = [
            OCRBlockInput(
                stable_block_id=str(block["stable_block_id"]),
                block_type=str(block["block_type"]),
                ordinal=int(block["ordinal"]),
                start_char=mapped(int(block["start_char"])),
                end_char=(
                    mapped(int(block["start_char"]))
                    + len(corrections[str(block["stable_block_id"])])
                    if str(block["stable_block_id"]) in corrections
                    else mapped(int(block["end_char"]))
                ),
                text=(
                    corrections[str(block["stable_block_id"])]
                    if str(block["stable_block_id"]) in corrections
                    else str(block["text"])
                ),
                language=block["language"],
                human_protected=(
                    bool(block["is_human_protected"])
                    or str(block["stable_block_id"]) in corrections
                ),
                attributes=json.loads(block["attributes_json"]),
            )
            for block in parent_blocks
        ]
        revision_id = self.create_revision(
            source_asset_id=str(parent["source_asset_id"]),
            layer="human_corrected_ocr",
            content=content,
            pages=pages,
            blocks=blocks,
            parent_revision_id=parent_revision_id,
            language=parent["language"],
            created_by="human",
            metadata={"message": message, "corrected_block_ids": sorted(corrections)},
        )
        for block in blocks:
            source_page = next(
                (
                    page.page_number
                    for page in pages
                    if page.start_char <= block.start_char < page.end_char
                ),
                None,
            )
            parent_block = next(
                item
                for item in parent_blocks
                if item["stable_block_id"] == block.stable_block_id
            )
            self.add_provenance(
                descendant_revision_id=revision_id,
                descendant_stable_block_id=block.stable_block_id,
                source_revision_id=parent_revision_id,
                source_page_number=source_page,
                descendant_start_char=block.start_char,
                descendant_end_char=block.end_char,
                source_start_char=int(parent_block["start_char"]),
                source_end_char=int(parent_block["end_char"]),
                relation_type="human_corrected_from",
                confidence=1.0,
            )
        self.copy_notes(parent_revision_id, revision_id)
        return revision_id

    def copy_notes(self, source_revision_id: str, target_revision_id: str) -> None:
        source = self.snapshot(source_revision_id)
        target = self.snapshot(target_revision_id)
        source_blocks = {
            str(block["id"]): str(block["stable_block_id"])
            for block in source["blocks"]
        }
        target_blocks = {
            str(block["stable_block_id"]): block for block in target["blocks"]
        }
        note_map: dict[str, str] = {}
        for note in source["notes"]:
            stable_body = source_blocks.get(str(note.get("body_block_id")))
            body_available = stable_body in target_blocks if stable_body else False
            note_map[str(note["id"])] = self.add_note(
                revision_id=target_revision_id,
                stable_note_id=str(note["stable_note_id"]),
                note_kind=str(note["note_kind"]),
                scope_id=str(note["scope_id"]),
                display_label=str(note["display_label"]),
                status=str(note["status"]) if body_available else "ambiguous",
                body_stable_block_id=stable_body if body_available else None,
                source_page_number=note["source_page_number"],
                metadata=json.loads(note["metadata_json"]),
            )
        for reference in source["note_references"]:
            stable_block = source_blocks.get(str(reference.get("block_id")))
            target_block = target_blocks.get(stable_block or "")
            if target_block is None:
                continue
            offset = min(int(reference["offset_in_block"]), len(str(target_block["text"])))
            self.add_note_reference(
                revision_id=target_revision_id,
                stable_reference_id=str(reference["stable_reference_id"]),
                display_label=str(reference["display_label"]),
                offset_in_block=offset,
                status=str(reference["status"]),
                note_id=note_map.get(str(reference.get("note_id"))),
                block_stable_id=stable_block,
            )

    def add_provenance(
        self,
        *,
        descendant_revision_id: str,
        source_revision_id: str,
        descendant_start_char: int,
        descendant_end_char: int,
        source_start_char: int,
        source_end_char: int,
        relation_type: str,
        confidence: float,
        descendant_stable_block_id: str | None = None,
        source_page_number: int | None = None,
        source_span_id: str | None = None,
    ) -> str:
        return self.add_provenance_batch(
            descendant_revision_id=descendant_revision_id,
            source_revision_id=source_revision_id,
            links=[OCRProvenanceInput(
                descendant_start_char=descendant_start_char,
                descendant_end_char=descendant_end_char,
                source_start_char=source_start_char,
                source_end_char=source_end_char,
                relation_type=relation_type,
                confidence=confidence,
                descendant_stable_block_id=descendant_stable_block_id,
                source_page_number=source_page_number,
                source_span_id=source_span_id,
            )],
        )[0]

    def add_provenance_batch(
        self,
        *,
        descendant_revision_id: str,
        source_revision_id: str,
        links: list[OCRProvenanceInput],
    ) -> list[str]:
        """Validate one immutable revision pair, then persist its links atomically."""
        if not links:
            return []
        descendant = self._revision_identity(descendant_revision_id)
        source = self._revision_identity(source_revision_id)
        if descendant["source_asset_id"] != source["source_asset_id"]:
            raise FukamiruError("OCR provenance cannot cross SourceAssets")
        requested_blocks = {link.descendant_stable_block_id for link in links
                            if link.descendant_stable_block_id}
        block_query = "SELECT id, stable_block_id FROM ocr_blocks WHERE revision_id = ?"
        block_parameters = (descendant_revision_id,)
        if len(requested_blocks) == 1:
            block_query += " AND stable_block_id = ?"
            block_parameters += (next(iter(requested_blocks)),)
        blocks = {
            str(row["stable_block_id"]): str(row["id"])
            for row in self.connection.execute(
                block_query, block_parameters,
            )
        } if requested_blocks else {}
        requested_pages = {link.source_page_number for link in links
                           if link.source_page_number is not None}
        page_query = "SELECT id, page_number FROM ocr_revision_pages WHERE revision_id = ?"
        page_parameters: tuple[Any, ...] = (source_revision_id,)
        if len(requested_pages) == 1:
            page_query += " AND page_number = ?"
            page_parameters += (next(iter(requested_pages)),)
        pages = {
            int(row["page_number"]): str(row["id"])
            for row in self.connection.execute(
                page_query, page_parameters,
            )
        } if requested_pages else {}
        rows: list[tuple[Any, ...]] = []
        link_ids: list[str] = []
        now = utc_now()
        for link in links:
            block_id = None
            if link.descendant_stable_block_id:
                block_id = blocks.get(link.descendant_stable_block_id)
                if block_id is None:
                    raise FukamiruError("Unknown descendant OCR block")
            page_id = None
            if link.source_page_number is not None:
                page_id = pages.get(link.source_page_number)
                if page_id is None:
                    raise FukamiruError("Unknown source OCR page")
            link_id = str(uuid.uuid4())
            link_ids.append(link_id)
            rows.append((
                link_id, descendant_revision_id, block_id, source_revision_id,
                page_id, link.source_span_id, link.descendant_start_char,
                link.descendant_end_char, link.source_start_char, link.source_end_char,
                link.relation_type, link.confidence, now,
            ))
        owns_transaction = not self.connection.in_transaction
        if owns_transaction:
            self.connection.execute("BEGIN")
        self.connection.execute("SAVEPOINT ocr_provenance_batch")
        try:
            self.connection.executemany(
                """
                INSERT INTO ocr_provenance_links(
                    id, descendant_revision_id, descendant_block_id, source_revision_id,
                    source_revision_page_id, source_span_id, descendant_start_char,
                    descendant_end_char, source_start_char, source_end_char,
                    relation_type, confidence, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        except Exception:
            self.connection.execute("ROLLBACK TO SAVEPOINT ocr_provenance_batch")
            self.connection.execute("RELEASE SAVEPOINT ocr_provenance_batch")
            if owns_transaction:
                self.connection.rollback()
            raise
        self.connection.execute("RELEASE SAVEPOINT ocr_provenance_batch")
        if self.autocommit:
            self.connection.commit()
        return link_ids

    def add_note(
        self,
        *,
        revision_id: str,
        stable_note_id: str,
        note_kind: str,
        scope_id: str,
        display_label: str,
        status: str,
        body_stable_block_id: str | None = None,
        source_page_number: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        self._revision_identity(revision_id)
        body_block_id = None
        if body_stable_block_id:
            block = self.connection.execute(
                "SELECT id FROM ocr_blocks WHERE revision_id = ? AND stable_block_id = ?",
                (revision_id, body_stable_block_id),
            ).fetchone()
            if block is None:
                raise FukamiruError("Unknown OCR note body block")
            body_block_id = block["id"]
        note_id = str(uuid.uuid4())
        self.connection.execute(
            """
            INSERT INTO ocr_notes(
                id, revision_id, stable_note_id, note_kind, scope_id, display_label,
                body_block_id, status, source_page_number, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                note_id,
                revision_id,
                stable_note_id,
                note_kind,
                scope_id,
                display_label,
                body_block_id,
                status,
                source_page_number,
                json.dumps(metadata or {}, ensure_ascii=False, separators=(",", ":")),
            ),
        )
        if self.autocommit:
            self.connection.commit()
        return note_id

    def add_note_reference(
        self,
        *,
        revision_id: str,
        stable_reference_id: str,
        display_label: str,
        offset_in_block: int,
        status: str,
        note_id: str | None = None,
        block_stable_id: str | None = None,
    ) -> str:
        self._revision_identity(revision_id)
        block_id = None
        if block_stable_id:
            block = self.connection.execute(
                "SELECT id FROM ocr_blocks WHERE revision_id = ? AND stable_block_id = ?",
                (revision_id, block_stable_id),
            ).fetchone()
            if block is None:
                raise FukamiruError("Unknown OCR note reference block")
            block_id = block["id"]
        if note_id is not None:
            note = self.connection.execute(
                "SELECT id FROM ocr_notes WHERE id = ? AND revision_id = ?",
                (note_id, revision_id),
            ).fetchone()
            if note is None:
                raise FukamiruError("OCR note reference target is not in this revision")
        reference_id = str(uuid.uuid4())
        self.connection.execute(
            """
            INSERT INTO ocr_note_references(
                id, revision_id, stable_reference_id, note_id, block_id,
                offset_in_block, display_label, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                reference_id,
                revision_id,
                stable_reference_id,
                note_id,
                block_id,
                offset_in_block,
                display_label,
                status,
            ),
        )
        if self.autocommit:
            self.connection.commit()
        return reference_id

    def snapshot(self, revision_id: str) -> dict[str, Any]:
        revision = dict(self._revision(revision_id))
        revision["metadata"] = json.loads(revision.pop("metadata_json"))
        for table, key in (
            ("ocr_revision_pages", "pages"),
            ("ocr_blocks", "blocks"),
            ("ocr_page_boundaries", "boundaries"),
            ("ocr_notes", "notes"),
            ("ocr_note_references", "note_references"),
        ):
            rows = self.connection.execute(
                f"SELECT * FROM {table} WHERE revision_id = ? ORDER BY rowid",  # noqa: S608
                (revision_id,),
            ).fetchall()
            revision[key] = [dict(row) for row in rows]
        revision["provenance"] = [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT * FROM ocr_provenance_links
                WHERE descendant_revision_id = ? ORDER BY rowid
                """,
                (revision_id,),
            )
        ]
        return revision

    def _revision_identity(self, revision_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT id, source_asset_id FROM ocr_text_revisions WHERE id = ?", (revision_id,)
        ).fetchone()
        if row is None:
            raise FukamiruError(f"Unknown OCR revision: {revision_id}")
        return row

    def _revision(self, revision_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM ocr_text_revisions WHERE id = ?", (revision_id,)
        ).fetchone()
        if row is None:
            raise FukamiruError(f"Unknown OCR revision: {revision_id}")
        return row

    def _validate_source_and_parent(
        self, source_asset_id: str, parent_revision_id: str | None
    ) -> None:
        source = self.connection.execute(
            "SELECT id FROM source_assets WHERE id = ?", (source_asset_id,)
        ).fetchone()
        if source is None:
            raise FukamiruError(f"Unknown SourceAsset: {source_asset_id}")
        if parent_revision_id is not None:
            parent = self._revision(parent_revision_id)
            if parent["source_asset_id"] != source_asset_id:
                raise FukamiruError("OCR parent revision belongs to another SourceAsset")

    @staticmethod
    def _validate_ranges(
        content: str, pages: list[OCRPageInput], blocks: list[OCRBlockInput]
    ) -> None:
        if not pages:
            raise FukamiruError("OCR revision requires at least one source page")
        page_numbers = [page.page_number for page in pages]
        if len(page_numbers) != len(set(page_numbers)):
            raise FukamiruError("OCR revision has duplicate page numbers")
        for page in pages:
            if page.page_number < 1 or not 0 <= page.start_char <= page.end_char <= len(content):
                raise FukamiruError("OCR page range is outside revision content")
            if content[page.start_char : page.end_char] != page.text:
                raise FukamiruError("OCR page text does not match its revision range")
        block_ids = [block.stable_block_id for block in blocks]
        if len(block_ids) != len(set(block_ids)):
            raise FukamiruError("OCR revision has duplicate stable block IDs")
        for block in blocks:
            if not 0 <= block.start_char <= block.end_char <= len(content):
                raise FukamiruError("OCR block range is outside revision content")
            if content[block.start_char : block.end_char] != block.text:
                raise FukamiruError("OCR block text does not match its revision range")
