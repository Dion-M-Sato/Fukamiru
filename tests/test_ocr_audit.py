from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fukamiru.defects import DefectRepository
from fukamiru.ocr import (
    AuditPage,
    NoteCandidate,
    NoteReferenceCandidate,
    OCRIntegrityAuditor,
)
from fukamiru.storage.database import SQLiteDatabase
from fukamiru.storage.runs import RunRepository


class OCRIntegrityAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.auditor = OCRIntegrityAuditor()

    def test_cross_page_context_and_hyphenation_repair_preserve_boundary(self) -> None:
        result = self.auditor.audit(
            [
                AuditPage(12, "Une longue trans-"),
                AuditPage(13, "formation continue ici."),
            ],
            language="fr",
        )

        finding = next(
            item for item in result.findings if item.defect_type == "page_boundary_hyphenation"
        )
        self.assertTrue(finding.automatic_repair_allowed)
        self.assertIn("trans-", finding.context)
        self.assertIn("formation", finding.context)
        self.assertEqual(result.normalized_pages[0].text, "Une longue trans")
        self.assertEqual(result.normalized_pages[1].text, "formation continue ici.")
        self.assertIn("���PAGE_BOUNDARY���", result.boundary_contexts[0].combined)

    def test_missing_duplicate_reversed_pages_and_orphan_notes(self) -> None:
        result = self.auditor.audit(
            [AuditPage(3, "Trois"), AuditPage(1, "Un"), AuditPage(3, "Trois bis")],
            notes=[NoteCandidate("chapter-1", "2", 3)],
            note_references=[NoteReferenceCandidate("chapter-1", "1", 1)],
        )
        kinds = {finding.defect_type for finding in result.findings}
        self.assertIn("page_duplicate", kinds)
        self.assertIn("page_order_reversed", kinds)
        self.assertIn("page_missing", kinds)
        self.assertIn("note_reference_without_body", kinds)
        self.assertIn("note_body_without_reference", kinds)
        self.assertTrue(result.has_critical)

    def test_repeated_margins_mojibake_ligature_and_japanese_vertical_damage(self) -> None:
        french = self.auditor.audit(
            [
                AuditPage(1, "BOOK TITLE\nUne oeuvre Ã©trange.\n1"),
                AuditPage(2, "BOOK TITLE\nLe coeur du texte.\n1"),
                AuditPage(3, "BOOK TITLE\nLa soeur répond.\n1"),
            ],
            language="fr",
        )
        kinds = {finding.defect_type for finding in french.findings}
        self.assertIn("repeated_header_contamination", kinds)
        self.assertIn("repeated_footer_contamination", kinds)
        self.assertIn("ocr_mojibake", kinds)
        self.assertIn("french_ligature_suspect", kinds)
        self.assertTrue(
            any(repair["type"] == "remove_repeated_header" for repair in french.applied_repairs)
        )

        japanese = self.auditor.audit(
            [AuditPage(1, "日 本 語 縦 書 き")],
            language="ja",
        )
        self.assertIn(
            "japanese_vertical_ocr_damage",
            {finding.defect_type for finding in japanese.findings},
        )

    def test_findings_persist_in_state_db_without_cross_database_foreign_keys(self) -> None:
        finding = self.auditor.audit(
            [AuditPage(12, "trans-"), AuditPage(13, "formation")], language="fr"
        ).findings[0]
        with tempfile.TemporaryDirectory() as directory:
            migrations = Path(__file__).resolve().parents[1] / "migrations" / "state"
            database = SQLiteDatabase(Path(directory) / "state.db", migrations)
            database.migrate()
            with database.connect() as connection:
                created = RunRepository(connection).create(
                    workflow_name="ocr_audit",
                    workflow_version="1",
                    entry_node="audit",
                    project_id=None,
                    inputs={},
                )
                repository = DefectRepository(connection)
                defect_id = repository.record(
                    run_id=created.run_id,
                    finding=finding,
                    source_asset_id="knowledge-db-asset-id",
                )
                defects = repository.list_for_run(created.run_id)
            self.assertEqual(defects[0]["id"], defect_id)
            self.assertEqual(defects[0]["source_asset_id"], "knowledge-db-asset-id")
            self.assertTrue(defects[0]["automatic_repair_allowed"])


if __name__ == "__main__":
    unittest.main()
