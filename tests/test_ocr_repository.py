from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fukamiru.errors import FukamiruError
from fukamiru.ocr import OCRBlockInput, OCRPageInput, OCRRevisionRepository
from fukamiru.ocr.pipeline import OCRPipelineService
from fukamiru.ocr.repository import OCRProvenanceInput
from fukamiru.storage.database import SQLiteDatabase


class OCRRevisionRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        migrations = Path(__file__).resolve().parents[1] / "migrations" / "knowledge"
        self.database = SQLiteDatabase(Path(self.temp.name) / "knowledge.db", migrations)
        self.database.migrate()
        self.connection = self.database.connect()
        self.connection.execute(
            """
            INSERT INTO source_assets(
                id, sha256, byte_size, media_type, ingestion_status, created_at, updated_at
            ) VALUES ('asset-1', 'hash-1', 1, 'application/pdf', 'ready', 'now', 'now')
            """
        )
        self.connection.commit()
        self.repository = OCRRevisionRepository(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.temp.cleanup()

    def test_page_boundary_inside_paragraph_and_layer_provenance_are_durable(self) -> None:
        content = "La trans-formation continue."
        pages = [
            OCRPageInput(12, 0, 9, "La trans-", printed_label="12"),
            OCRPageInput(13, 9, len(content), "formation continue.", printed_label="13"),
        ]
        blocks = [
            OCRBlockInput("P-0001", "paragraph", 0, 0, len(content), content, language="fr")
        ]
        raw_id = self.repository.create_revision(
            source_asset_id="asset-1",
            layer="raw_ocr",
            content=content,
            pages=pages,
            blocks=blocks,
            created_by="extractor",
            language="fr",
        )
        normalized_id = self.repository.create_revision(
            source_asset_id="asset-1",
            layer="normalized_ocr",
            content=content,
            pages=pages,
            blocks=blocks,
            parent_revision_id=raw_id,
            language="fr",
        )
        self.repository.add_provenance(
            descendant_revision_id=normalized_id,
            descendant_stable_block_id="P-0001",
            source_revision_id=raw_id,
            source_page_number=12,
            descendant_start_char=0,
            descendant_end_char=len(content),
            source_start_char=0,
            source_end_char=len(content),
            relation_type="normalized_from",
            confidence=1.0,
        )

        raw = self.repository.snapshot(raw_id)
        normalized = self.repository.snapshot(normalized_id)
        page_13 = next(item for item in normalized["boundaries"] if item["page_number"] == 13)
        raw_page_13 = next(item for item in raw["boundaries"] if item["page_number"] == 13)
        self.assertEqual(page_13["char_offset"], 9)
        self.assertEqual(page_13["offset_in_block"], 9)
        self.assertEqual(page_13["stable_marker_id"], raw_page_13["stable_marker_id"])
        self.assertEqual(normalized["parent_revision_id"], raw_id)
        self.assertEqual(normalized["provenance"][0]["source_revision_id"], raw_id)

    def test_note_reference_and_body_are_linked_with_stable_ids(self) -> None:
        content = "Texte1\nNote un"
        revision_id = self.repository.create_revision(
            source_asset_id="asset-1",
            layer="normalized_ocr",
            content=content,
            pages=[OCRPageInput(1, 0, len(content), content)],
            blocks=[
                OCRBlockInput("P-1", "paragraph", 0, 0, 6, "Texte1"),
                OCRBlockInput("N-1", "footnote", 1, 7, len(content), "Note un"),
            ],
        )
        note_id = self.repository.add_note(
            revision_id=revision_id,
            stable_note_id="NOTE-ch1-1",
            note_kind="source_footnote",
            scope_id="chapter-1",
            display_label="1",
            body_stable_block_id="N-1",
            source_page_number=1,
            status="resolved",
        )
        self.repository.add_note_reference(
            revision_id=revision_id,
            stable_reference_id="NOTEREF-ch1-1-a",
            note_id=note_id,
            block_stable_id="P-1",
            offset_in_block=5,
            display_label="1",
            status="resolved",
        )

        snapshot = self.repository.snapshot(revision_id)
        self.assertEqual(snapshot["notes"][0]["stable_note_id"], "NOTE-ch1-1")
        self.assertEqual(snapshot["note_references"][0]["note_id"], note_id)
        child_id = self.repository.create_revision(
            source_asset_id="asset-1",
            layer="translation_source",
            content=content,
            pages=[OCRPageInput(1, 0, len(content), content)],
            blocks=[
                OCRBlockInput("P-1", "paragraph", 0, 0, 6, "Texte1"),
                OCRBlockInput("N-1", "footnote", 1, 7, len(content), "Note un"),
            ],
            parent_revision_id=revision_id,
            created_by="agent",
        )
        self.repository.copy_notes(revision_id, child_id)
        child = self.repository.snapshot(child_id)
        self.assertEqual(child["notes"][0]["stable_note_id"], "NOTE-ch1-1")
        self.assertEqual(
            child["note_references"][0]["note_id"], child["notes"][0]["id"]
        )

    def test_page_text_must_match_content_range(self) -> None:
        with self.assertRaisesRegex(FukamiruError, "page text"):
            self.repository.create_revision(
                source_asset_id="asset-1",
                layer="raw_ocr",
                content="abc",
                pages=[OCRPageInput(1, 0, 3, "xyz")],
                blocks=[],
                created_by="extractor",
            )

    def test_pipeline_extracts_page_footnote_reference_and_body(self) -> None:
        result = OCRPipelineService(self.connection).process(
            source_asset_id="asset-1",
            pages=["Texte principal[1].\n\n[1] Note de bas de page."],
            language="fr",
        )
        snapshot = self.repository.snapshot(result.normalized_revision_id)
        self.assertEqual(snapshot["notes"][0]["note_kind"], "source_footnote")
        self.assertEqual(snapshot["notes"][0]["status"], "resolved")
        self.assertEqual(snapshot["note_references"][0]["status"], "resolved")
        self.assertEqual(
            snapshot["note_references"][0]["note_id"], snapshot["notes"][0]["id"]
        )

    def test_pipeline_links_endnote_across_pages_and_reports_orphan_reference(self) -> None:
        result = OCRPipelineService(self.connection).process(
            source_asset_id="asset-1",
            pages=[
                "Texte avec note[2] et référence orpheline[9].\n\n",
                "Notes\n\n2. Note de fin.",
            ],
            language="fr",
        )
        snapshot = self.repository.snapshot(result.normalized_revision_id)
        endnote = next(note for note in snapshot["notes"] if note["display_label"] == "2")
        linked = next(
            reference
            for reference in snapshot["note_references"]
            if reference["display_label"] == "2"
        )
        orphan = next(
            reference
            for reference in snapshot["note_references"]
            if reference["display_label"] == "9"
        )
        self.assertEqual(endnote["note_kind"], "source_endnote")
        self.assertEqual(linked["note_id"], endnote["id"])
        self.assertEqual(orphan["status"], "orphan")
        self.assertIn(
            "note_reference_without_body",
            {finding.defect_type for finding in result.audit.findings},
        )

    def test_layout_roles_exclude_numbered_headings_and_toc_entries_from_notes(
        self,
    ) -> None:
        structured_pages = [
            {
                "page_number": 32,
                "blocks": [
                    {
                        "stable_block_id": "HEADING-3",
                        "structural_role": "list_item",
                        "text": "3. CLANNAD OU L'EMPLOI DU TEMPS",
                    },
                    {
                        "stable_block_id": "TOC-3",
                        "structural_role": "toc_entry",
                        "text": "3. L'énigme du mélodrame ........ 17",
                    },
                    {
                        "stable_block_id": "BODY-3",
                        "structural_role": "paragraph",
                        "text": "Texte principal[3].",
                    },
                    {
                        "stable_block_id": "NOTE-3",
                        "structural_role": "footnote",
                        "text": "3. Cette partie reprend des éléments de notre article.",
                    },
                ],
            }
        ]
        result = OCRPipelineService(self.connection).process(
            source_asset_id="asset-1",
            pages=["unused when layout blocks are available"],
            language="fr",
            structured_pages=structured_pages,
        )

        snapshot = self.repository.snapshot(result.normalized_revision_id)
        self.assertEqual(len(snapshot["notes"]), 1)
        self.assertEqual(snapshot["notes"][0]["display_label"], "3")
        self.assertEqual(snapshot["notes"][0]["status"], "resolved")
        self.assertEqual(len(snapshot["note_references"]), 1)

    def test_duplicate_layout_notes_are_recorded_as_ambiguous(self) -> None:
        structured_pages = [
            {
                "page_number": 4,
                "blocks": [
                    {
                        "stable_block_id": "BODY-1",
                        "structural_role": "paragraph",
                        "text": "Texte principal[1].",
                    },
                    {
                        "stable_block_id": "NOTE-1-A",
                        "structural_role": "footnote",
                        "text": "1. Première interprétation.",
                    },
                    {
                        "stable_block_id": "NOTE-1-B",
                        "structural_role": "footnote",
                        "text": "1. Deuxième interprétation.",
                    },
                ],
            }
        ]
        result = OCRPipelineService(self.connection).process(
            source_asset_id="asset-1",
            pages=["unused when layout blocks are available"],
            language="fr",
            structured_pages=structured_pages,
        )

        snapshot = self.repository.snapshot(result.normalized_revision_id)
        self.assertEqual(len(snapshot["notes"]), 1)
        note = snapshot["notes"][0]
        metadata = json.loads(note["metadata_json"])
        self.assertEqual(note["status"], "ambiguous")
        self.assertIsNone(note["body_block_id"])
        self.assertEqual(metadata["ambiguity_reason"], "duplicate_scope_label")
        self.assertEqual(metadata["candidate_count"], 2)
        self.assertEqual(snapshot["note_references"][0]["status"], "ambiguous")
        self.assertEqual(snapshot["note_references"][0]["note_id"], note["id"])

    def test_many_blocks_do_not_reload_revision_bodies_for_each_provenance_link(self) -> None:
        content = "\n\n".join(f"Paragraph {number} keeps its source text." for number in range(80))
        queries: list[str] = []
        self.connection.set_trace_callback(queries.append)
        try:
            result = OCRPipelineService(self.connection).process(
                source_asset_id="asset-1", pages=[content], language="en"
            )
        finally:
            self.connection.set_trace_callback(None)
        full_revision_reads = [
            sql for sql in queries if "select * from ocr_text_revisions" in sql.casefold()
        ]
        self.assertLessEqual(len(full_revision_reads), 1)
        snapshot = self.repository.snapshot(result.normalized_revision_id)
        self.assertEqual(snapshot["content_text"], content)
        self.assertEqual(len(snapshot["blocks"]), 80)
        self.assertEqual(len(snapshot["provenance"]), 80)
        self.assertEqual(
            {row["descendant_block_id"] for row in snapshot["provenance"]},
            {row["id"] for row in snapshot["blocks"]},
        )

    def test_provenance_batch_validation_and_sql_failure_leave_no_partial_links(self) -> None:
        result = OCRPipelineService(self.connection).process(
            source_asset_id="asset-1",
            pages=["First paragraph.\n\nSecond paragraph."],
            language="en",
        )
        snapshot = self.repository.snapshot(result.normalized_revision_id)
        block = snapshot["blocks"][0]
        valid = OCRProvenanceInput(
            descendant_start_char=block["start_char"],
            descendant_end_char=block["end_char"],
            source_start_char=block["start_char"],
            source_end_char=block["end_char"],
            relation_type="normalized_from",
            confidence=1.0,
            descendant_stable_block_id=block["stable_block_id"],
        )
        repository = OCRRevisionRepository(self.connection, autocommit=False)
        self.connection.execute("UPDATE source_assets SET ingestion_status='extracting'")
        for invalid, exception in (
            (replace(valid, descendant_stable_block_id="missing-block"), FukamiruError),
            (replace(valid, relation_type="invalid-relation"), sqlite3.IntegrityError),
            (replace(valid, source_page_number=999), FukamiruError),
        ):
            with self.subTest(invalid=invalid), self.assertRaises(exception):
                repository.add_provenance_batch(
                    descendant_revision_id=result.normalized_revision_id,
                    source_revision_id=result.raw_revision_id,
                    links=[valid, invalid],
                )
            self.assertTrue(self.connection.in_transaction)
            count = self.connection.execute(
                "SELECT COUNT(*) FROM ocr_provenance_links WHERE descendant_revision_id=?",
                (result.normalized_revision_id,),
            ).fetchone()[0]
            self.assertEqual(count, len(snapshot["provenance"]))
        self.connection.rollback()

    def test_provenance_batch_still_rejects_cross_asset_links(self) -> None:
        self.connection.execute(
            """INSERT INTO source_assets(
                id,sha256,byte_size,media_type,ingestion_status,created_at,updated_at
            ) VALUES ('asset-2','hash-2',1,'text/plain','ready','now','now')"""
        )
        self.connection.commit()
        revisions = []
        for asset in ("asset-1", "asset-2"):
            revisions.append(self.repository.create_revision(
                source_asset_id=asset, layer="raw_ocr", content="Text",
                pages=[OCRPageInput(1, 0, 4, "Text")], blocks=[],
            ))
        with self.assertRaisesRegex(FukamiruError, "cross SourceAssets"):
            self.repository.add_provenance_batch(
                descendant_revision_id=revisions[0], source_revision_id=revisions[1],
                links=[OCRProvenanceInput(0, 4, 0, 4, "normalized_from", 1.0)],
            )

    def test_pipeline_failure_rolls_back_all_ocr_revisions(self) -> None:
        pipeline = OCRPipelineService(self.connection)
        with (
            patch.object(
                pipeline.revisions,
                "add_note",
                side_effect=sqlite3.IntegrityError("forced note failure"),
            ),
            self.assertRaisesRegex(sqlite3.IntegrityError, "forced note failure"),
        ):
            pipeline.process(
                source_asset_id="asset-1",
                pages=["Texte principal[1].\n\n[1] Note."],
                language="fr",
            )

        revision_count = self.connection.execute(
            "SELECT COUNT(*) FROM ocr_text_revisions WHERE source_asset_id = 'asset-1'"
        ).fetchone()[0]
        self.assertEqual(revision_count, 0)

    def test_human_correction_is_immutable_and_protected_from_agent_rewrite(self) -> None:
        original = "Texte avec une eruer OCR."
        parent_id = self.repository.create_revision(
            source_asset_id="asset-1",
            layer="normalized_ocr",
            content=original,
            pages=[OCRPageInput(1, 0, len(original), original)],
            blocks=[OCRBlockInput("P-HUMAN", "paragraph", 0, 0, len(original), original)],
        )
        corrected_text = "Texte avec une erreur OCR."
        corrected_id = self.repository.create_human_correction(
            parent_revision_id=parent_id,
            corrections={"P-HUMAN": corrected_text},
            message="Correct obvious OCR typo",
        )
        corrected = self.repository.snapshot(corrected_id)
        self.assertEqual(corrected["layer"], "human_corrected_ocr")
        self.assertEqual(corrected["content_text"], corrected_text)
        self.assertEqual(corrected["blocks"][0]["is_human_protected"], 1)
        self.assertEqual(corrected["provenance"][0]["source_revision_id"], parent_id)

        agent_text = "Texte réécrit par agent."
        with self.assertRaisesRegex(FukamiruError, "protected human blocks"):
            self.repository.create_revision(
                source_asset_id="asset-1",
                layer="translation_source",
                content=agent_text,
                pages=[OCRPageInput(1, 0, len(agent_text), agent_text)],
                blocks=[
                    OCRBlockInput(
                        "P-HUMAN", "paragraph", 0, 0, len(agent_text), agent_text
                    )
                ],
                parent_revision_id=corrected_id,
                created_by="agent",
            )


if __name__ == "__main__":
    unittest.main()
