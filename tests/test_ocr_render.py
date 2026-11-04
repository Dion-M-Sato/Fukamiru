from __future__ import annotations

import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from fukamiru.documents.canonical_html import CanonicalHTMLValidator
from fukamiru.ocr import (
    OCRBlockInput,
    OCRCanonicalHTMLRenderer,
    OCRPageInput,
    OCRRevisionRepository,
)
from fukamiru.storage.database import SQLiteDatabase


class OCRCanonicalHTMLRendererTests(unittest.TestCase):
    def test_page_marker_remains_inside_paragraph_and_notes_are_bidirectional(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            migrations = Path(__file__).resolve().parents[1] / "migrations" / "knowledge"
            database = SQLiteDatabase(Path(directory) / "knowledge.db", migrations)
            database.migrate()
            with database.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO source_assets(
                        id, sha256, byte_size, media_type, ingestion_status,
                        created_at, updated_at
                    ) VALUES ('asset-1', 'hash-1', 1, 'application/pdf', 'ready', 'now', 'now')
                    """
                )
                connection.commit()
                repository = OCRRevisionRepository(connection)
                paragraph = "La trans-formation continue."
                note = "Note un"
                content = f"{paragraph}\n{note}"
                revision_id = repository.create_revision(
                    source_asset_id="asset-1",
                    layer="normalized_ocr",
                    content=content,
                    pages=[
                        OCRPageInput(12, 0, 9, content[:9]),
                        OCRPageInput(13, 9, len(content), content[9:]),
                    ],
                    blocks=[
                        OCRBlockInput(
                            "P-0001", "paragraph", 0, 0, len(paragraph), paragraph, language="fr"
                        ),
                        OCRBlockInput(
                            "NOTE-BODY-1",
                            "footnote",
                            1,
                            len(paragraph) + 1,
                            len(content),
                            note,
                            language="fr",
                        ),
                    ],
                    language="fr",
                )
                note_id = repository.add_note(
                    revision_id=revision_id,
                    stable_note_id="NOTE-ch1-1",
                    note_kind="source_footnote",
                    scope_id="chapter-1",
                    display_label="1",
                    body_stable_block_id="NOTE-BODY-1",
                    source_page_number=13,
                    status="resolved",
                )
                repository.add_note_reference(
                    revision_id=revision_id,
                    stable_reference_id="NOTEREF-ch1-1-a",
                    note_id=note_id,
                    block_stable_id="P-0001",
                    offset_in_block=8,
                    display_label="1",
                    status="resolved",
                )
                snapshot = repository.snapshot(revision_id)

            rendered = OCRCanonicalHTMLRenderer().render(
                snapshot,
                title="Transformation",
                bibliographic_lines=["Example Press, 2026"],
            )
            report = CanonicalHTMLValidator().validate(rendered)
            self.assertTrue(report.valid, report.issues)
            root = ET.fromstring(rendered)
            paragraph_element = root.find(".//p[@id='P-0001']")
            assert paragraph_element is not None
            page_marker = next(
                marker
                for marker in paragraph_element.findall(
                    ".//span[@class='source-page-boundary']"
                )
                if marker.attrib["data-source-page"] == "13"
            )
            self.assertEqual(page_marker.attrib["data-source-page"], "13")
            page_link = page_marker.find("a")
            assert page_link is not None
            self.assertEqual(page_link.attrib["href"], "/api/source-assets/asset-1/pages/13")
            note_reference = paragraph_element.find(".//sup[@id='NOTEREF-ch1-1-a']/a")
            assert note_reference is not None
            self.assertEqual(note_reference.attrib["href"], "#NOTE-ch1-1")
            backlink = root.find(".//li[@id='NOTE-ch1-1']/a")
            assert backlink is not None
            self.assertEqual(backlink.attrib["href"], "#NOTEREF-ch1-1-a")


if __name__ == "__main__":
    unittest.main()
