from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from fukamiru.bootstrap import bootstrap
from fukamiru.config import load_settings
from fukamiru.documents.canonical_html import CanonicalHTMLValidator
from fukamiru.errors import FukamiruError
from fukamiru.knowledge.pdf_layout import (
    LayoutBlock,
    LayoutDocument,
    LayoutPage,
    PDFLayoutAnalyzer,
    PDFLayoutHTMLRenderer,
    is_nonlinguistic_artifact,
)
from fukamiru.knowledge.service import IngestionService
from fukamiru.ocr.pipeline import OCRPipelineService
from fukamiru.ocr.repository import OCRRevisionRepository
from fukamiru.storage.database import DatabasePair, SQLiteDatabase
from fukamiru.storage.runs import utc_now
from fukamiru.worker.runner import WorkerRunner


def _pdf_text(text: str) -> bytes:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").encode(
        "latin-1"
    )


def _write_layout_fixture(path: Path) -> None:
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    bold = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica-Bold"),
        }
    )
    font_ref = writer._add_object(font)
    bold_ref = writer._add_object(bold)
    page_commands = [
        [
            ("F1", 9, 72, 770, "Fukamiru fixture"),
            ("F2", 24, 150, 710, "Layout First Translation"),
            ("F2", 16, 72, 650, "Contents"),
            ("F1", 11, 72, 620, "Chapter One ........ 3"),
            ("F1", 11, 72, 560, "This paragraph belongs to the introduction."),
            ("F1", 8, 72, 60, "1 A source footnote at the visual bottom."),
            ("F1", 9, 300, 20, "1"),
        ],
        [
            ("F1", 9, 72, 770, "Fukamiru fixture"),
            ("F2", 18, 72, 710, "Chapter One"),
            ("F1", 11, 72, 670, "The first visual line of a paragraph"),
            ("F1", 11, 72, 654, "continues on the following visual line."),
            ("F2", 18, 300, 400, '\"\"'),
            ("F1", 9, 300, 20, "2"),
        ],
    ]
    for commands in page_commands:
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/Font"): DictionaryObject(
                    {NameObject("/F1"): font_ref, NameObject("/F2"): bold_ref}
                )
            }
        )
        content = b"\n".join(
            b"BT /"
            + font_name.encode()
            + b" "
            + str(size).encode()
            + b" Tf 1 0 0 1 "
            + str(x).encode()
            + b" "
            + str(y).encode()
            + b" Tm ("
            + _pdf_text(text)
            + b") Tj ET"
            for font_name, size, x, y, text in commands
        )
        stream = DecodedStreamObject()
        stream.set_data(content)
        page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as handle:
        writer.write(handle)


class PDFLayoutTests(unittest.TestCase):
    def test_page_dimensions_fall_back_to_cropbox(self) -> None:
        class BrokenMediaBoxPage:
            @property
            def mediabox(self) -> object:
                raise AssertionError

            @property
            def cropbox(self) -> list[float]:
                return [1.0, 2.0, 401.0, 602.0]

            def get(self, _key: str) -> None:
                return None

        self.assertEqual(
            PDFLayoutAnalyzer._page_dimensions(BrokenMediaBoxPage()),
            (400.0, 600.0),
        )

    def test_positioned_text_clamps_coordinates_to_page(self) -> None:
        class OutsidePage:
            @staticmethod
            def extract_text(*, visitor_text: object) -> None:
                visitor_text(
                    "outside",
                    [1.0, 0.0, 0.0, 1.0, 0.0, 0.0],
                    [1.0, 0.0, 0.0, 1.0, 10.0, -100.0],
                    {},
                    10.0,
                )

        positioned = PDFLayoutAnalyzer._positioned_text(OutsidePage(), page_height=100.0)
        self.assertEqual(positioned[0].top, 100.0)
        self.assertGreaterEqual(positioned[0].bottom, positioned[0].top)

    def test_renderer_removes_xml_forbidden_control_characters(self) -> None:
        block = LayoutBlock(
            "PDFB-control", 1, 0, "paragraph", "before\x0bafter",
            0.0, 0.0, 10.0, 10.0, 10.0, "Helvetica", 0.9,
        )
        document = LayoutDocument(
            "asset-control",
            "test",
            (LayoutPage(1, 100.0, 100.0, 0, None, (block,)),),
        )
        rendered = PDFLayoutHTMLRenderer().render(
            document, title="Control\x00Title", language="ja"
        )
        self.assertNotIn("\x00", rendered)
        self.assertNotIn("\x0b", rendered)
        self.assertTrue(CanonicalHTMLValidator().validate(rendered).valid)

    def test_symbol_debris_is_not_a_translation_candidate(self) -> None:
        self.assertTrue(is_nonlinguistic_artifact('\"\"'))
        self.assertTrue(is_nonlinguistic_artifact("<Q)B"))
        self.assertTrue(is_nonlinguistic_artifact("...... ......0\"'"))
        self.assertFalse(is_nonlinguistic_artifact("Chapitre IV"))
        self.assertFalse(is_nonlinguistic_artifact("4."))

    def test_structured_translation_preserves_heading_list_note_and_page(self) -> None:
        records = [
            {
                "structural_role": "heading",
                "source_stable_block_id": "PDFB-heading",
                "source_page_start": 3,
            },
            {
                "structural_role": "paragraph",
                "source_stable_block_id": "PDFB-body",
                "source_page_start": 3,
            },
            {
                "structural_role": "list_item",
                "source_stable_block_id": "PDFB-list",
                "source_page_start": 3,
            },
            {
                "structural_role": "footnote",
                "source_stable_block_id": "PDFB-note",
                "source_page_start": 3,
            },
        ]
        rendered = WorkerRunner._canonical_structured_translation(
            "run-layout", ["章", "本文", "項目", "注釈"], records, "ja"
        )

        self.assertTrue(CanonicalHTMLValidator().validate(rendered).valid)
        self.assertIn("Source page 3", rendered)
        self.assertIn('data-layout-role="heading"', rendered)
        self.assertIn('data-layout-role="list_item"', rendered)
        self.assertIn('data-layout-role="footnote"', rendered)

    def test_analyzer_classifies_display_roles_and_renderer_is_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "layout.pdf"
            _write_layout_fixture(path)

            document = PDFLayoutAnalyzer().analyze(path, source_asset_id="asset-layout")
            roles = [block.structural_role for page in document.pages for block in page.blocks]

            self.assertIn("title", roles)
            self.assertIn("toc_entry", roles)
            self.assertIn("footnote", roles)
            self.assertEqual(roles.count("page_header"), 2)
            self.assertEqual(roles.count("page_number"), 2)
            paragraph = next(
                block
                for block in document.pages[1].blocks
                if "first visual line" in block.text
            )
            self.assertIn("continues on the following", paragraph.text)
            artifact = next(
                block
                for block in document.pages[1].blocks
                if block.text == '\"\"'
            )
            self.assertEqual(artifact.structural_role, "artifact")
            self.assertFalse(artifact.translatable)
            rendered = PDFLayoutHTMLRenderer().render(
                document, title="Layout fixture", language="en"
            )
            self.assertTrue(CanonicalHTMLValidator().validate(rendered).valid)
            self.assertIn('data-layout-role="toc_entry"', rendered)
            self.assertIn('data-layout-role="artifact"', rendered)
            self.assertIn('data-translatable="false"', rendered)

    def test_prepared_pdf_preserves_layout_without_a_second_pdf_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "layout.pdf"
            _write_layout_fixture(path)
            analyzer = PDFLayoutAnalyzer()
            expected = analyzer.analyze(path, source_asset_id="asset-layout")
            prepared = analyzer.prepare(path)
            self.assertTrue(prepared.text_layer.usable)
            with patch("pypdf.PdfReader", side_effect=AssertionError("second PDF read")):
                actual = analyzer.analyze(path, source_asset_id="asset-layout", prepared=prepared)
            self.assertEqual(actual.serializable_pages(), expected.serializable_pages())
            with self.assertRaisesRegex(FukamiruError, "another source path"):
                analyzer.analyze(
                    path.with_name("another.pdf"), source_asset_id="asset-layout", prepared=prepared
                )

    def test_ready_pdf_preserves_legacy_content_and_missing_derived_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "layout.pdf"
            _write_layout_fixture(path)
            config = root / "local.toml"
            paths = {
                "state_db": root / "state.db", "knowledge_db": root / "knowledge.db",
                "runtime_root": root / "runtime", "corpus_root": root / "corpus",
                "ocr_root": root / "ocr", "knowledge_artifacts_root": root / "artifacts",
                "faiss_root": root / "faiss",
            }
            config.write_text(
                "[paths]\n" + "\n".join(
                    f'{key} = "{value.as_posix()}"' for key, value in paths.items()
                ) + '\n[retrieval]\ntokenizer_mode = "unicode"\nrequire_cuda = false\n',
                encoding="utf-8",
            )
            settings = load_settings(config)
            bootstrap(settings)
            with DatabasePair.from_settings(settings).knowledge.connect() as connection:
                service = IngestionService(connection, settings)
                first = service.ingest_path(str(path), source_language="en")
                connection.execute(
                    "UPDATE pdf_layout_documents SET analyzer_version='pdf-display-layout-v1'"
                )
                connection.execute(
                    "UPDATE ocr_text_revisions SET layer='human_corrected_ocr',created_by='human'"
                )
                protected = connection.execute(
                    "UPDATE ocr_blocks SET is_human_protected=1"
                ).rowcount
                self.assertGreater(protected, 0)
                connection.commit()
                tables = ("document_units", "pages", "source_spans", "chunks",
                          "ocr_text_revisions", "ocr_blocks", "pdf_layout_documents")
                before = {
                    table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
                    for table in tables
                }
                with patch.object(
                    PDFLayoutAnalyzer, "prepare", side_effect=AssertionError("unnecessary parse")
                ):
                    reused = service.ingest_path(str(path), source_language="en")
                self.assertTrue(reused["reused"])
                self.assertEqual(reused["document_unit_id"], first["document_unit_id"])
                Path(first["structured_html_path"]).unlink()
                with patch.object(
                    PDFLayoutAnalyzer, "prepare", side_effect=AssertionError("unnecessary parse")
                ):
                    reused = service.ingest_path(str(path), source_language="en")
                self.assertTrue(reused["reused"])
                self.assertEqual(reused["document_unit_id"], first["document_unit_id"])
                self.assertEqual(before, {
                    table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
                    for table in tables
                })
                connection.execute("DELETE FROM pdf_layout_documents")
                connection.commit()
                with patch.object(
                    PDFLayoutAnalyzer, "prepare", side_effect=AssertionError("unnecessary parse")
                ):
                    reused = service.ingest_path(str(path), source_language="en")
                self.assertTrue(reused["reused"])
                for table in tables[:-1]:
                    self.assertEqual(before[table], [
                        tuple(row) for row in connection.execute(f"SELECT * FROM {table}")
                    ])

    def test_layout_roles_survive_into_normalized_ocr_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "layout.pdf"
            _write_layout_fixture(path)
            migrations = Path(__file__).resolve().parents[1] / "migrations" / "knowledge"
            database = SQLiteDatabase(root / "knowledge.db", migrations)
            database.migrate()
            connection = database.connect()
            try:
                connection.execute(
                    """
                    INSERT INTO source_assets(
                        id, sha256, byte_size, media_type, source_language,
                        ingestion_status, managed_path, created_at, updated_at
                    ) VALUES ('asset-layout', 'hash-layout', 1, 'application/pdf',
                              'en', 'registered', ?, ?, ?)
                    """,
                    (str(path), utc_now(), utc_now()),
                )
                connection.commit()
                layout = PDFLayoutAnalyzer().analyze(path, source_asset_id="asset-layout")
                result = OCRPipelineService(connection).process(
                    source_asset_id="asset-layout",
                    pages=[page.text for page in layout.pages],
                    language="en",
                    structured_pages=layout.serializable_pages(),
                )
                revision = OCRRevisionRepository(connection).snapshot(
                    result.normalized_revision_id
                )
                attributes = [
                    json.loads(block["attributes_json"])
                    for block in revision["blocks"]
                ]
                roles = {item["structural_role"] for item in attributes}
                self.assertTrue({"title", "heading", "toc_entry", "footnote"} <= roles)
                self.assertFalse({"page_header", "page_number"} & roles)
                self.assertTrue(all(item["layout_block_id"] for item in attributes))
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
