from __future__ import annotations

import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from fukamiru.errors import FukamiruError
from fukamiru.projects.service import ProjectService
from fukamiru.revisions import ManuscriptImportService, RevisionService
from fukamiru.storage.database import SQLiteDatabase


class ManuscriptImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        migrations = Path(__file__).resolve().parents[1] / "migrations" / "state"
        self.database = SQLiteDatabase(self.root / "state.db", migrations)
        self.database.migrate()
        self.connection = self.database.connect()
        self.project_root = self.root / "project"
        project = ProjectService(self.connection).create(
            name="Manuscript fixture", root_path=self.project_root
        )
        self.project_id = str(project["id"])
        self.document_id = RevisionService(self.connection).create_document(
            project_id=self.project_id, kind="paper", title="Working paper"
        )
        self.imports = ManuscriptImportService(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.temp.cleanup()

    def test_txt_preview_confirms_human_revision_without_changing_source(self) -> None:
        source = self.root / "draft.txt"
        original = "First paragraph.\n\nSecond paragraph."
        source.write_text(original, encoding="utf-8")

        preview = self.imports.preview(
            document_id=self.document_id, source_path=source
        )

        self.assertEqual(preview["source_format"], "txt")
        self.assertEqual(preview["extraction"]["encoding"], "UTF-8")
        self.assertEqual(preview["extraction"]["block_count"], 2)
        self.assertTrue(preview["validation"]["valid"])
        self.assertIn("First paragraph.", preview["proposed_html"])
        self.assertTrue(preview["original_unchanged"])

        confirmed = self.imports.confirm(preview["id"])

        self.assertEqual(confirmed["status"], "imported")
        self.assertTrue(Path(confirmed["artifact_path"]).is_file())
        self.assertEqual(source.read_text(encoding="utf-8"), original)
        revision = self.connection.execute(
            "SELECT editor_type, operation FROM revisions WHERE id = ?",
            (confirmed["imported_revision_id"],),
        ).fetchone()
        self.assertEqual(
            (revision["editor_type"], revision["operation"]),
            ("human", "human_import"),
        )
        protected = self.connection.execute(
            "SELECT COUNT(*) FROM content_blocks WHERE document_id = ? AND protected = 1",
            (self.document_id,),
        ).fetchone()[0]
        self.assertGreater(protected, 0)

    def test_markdown_preserves_headings_lists_quotes_and_escapes_raw_html(self) -> None:
        source = self.root / "draft.md"
        source.write_text(
            "# Introduction\n\nA <script>draft</script>.\n\n- One\n- Two\n\n> Quoted note.",
            encoding="utf-8",
        )

        preview = self.imports.preview(
            document_id=self.document_id, source_path=source
        )

        canonical = preview["proposed_html"]
        self.assertTrue(preview["validation"]["valid"])
        self.assertIn("<h2", canonical)
        self.assertIn("<ul", canonical)
        self.assertIn("<blockquote", canonical)
        self.assertIn("&lt;script&gt;draft&lt;/script&gt;", canonical)
        self.assertNotIn("<script>", canonical)

    def test_docx_preserves_structure_and_reports_lossy_elements(self) -> None:
        source = self.root / "draft.docx"
        source.write_bytes(_docx_fixture())

        preview = self.imports.preview(
            document_id=self.document_id, source_path=source
        )

        self.assertTrue(preview["validation"]["valid"])
        self.assertIn("Chapter One", preview["proposed_html"])
        self.assertIn("List item", preview["proposed_html"])
        self.assertIn("Cell A | Cell B", preview["proposed_html"])
        self.assertIn("[1] Footnote text", preview["proposed_html"])
        self.assertEqual(preview["extraction"]["table_row_count"], 1)
        self.assertEqual(preview["extraction"]["note_count"], 1)
        self.assertTrue(
            any("images" in warning for warning in preview["extraction"]["warnings"])
        )

    def test_pdf_preserves_page_provenance_and_warns_for_blank_pages(self) -> None:
        source = self.root / "draft.pdf"
        source.write_bytes(_pdf_fixture())

        preview = self.imports.preview(
            document_id=self.document_id, source_path=source
        )

        self.assertTrue(preview["validation"]["valid"])
        self.assertEqual(preview["extraction"]["page_count"], 2)
        self.assertIn('data-source-page="1"', preview["proposed_html"])
        self.assertTrue(
            any("page 2" in warning for warning in preview["extraction"]["warnings"])
        )

    def test_changed_source_must_be_previewed_again(self) -> None:
        source = self.root / "changing.txt"
        source.write_text("Before", encoding="utf-8")
        preview = self.imports.preview(
            document_id=self.document_id, source_path=source
        )
        source.write_text("After", encoding="utf-8")

        with self.assertRaisesRegex(FukamiruError, "changed after preview"):
            self.imports.confirm(preview["id"])

        count = self.connection.execute("SELECT COUNT(*) FROM revisions").fetchone()[0]
        self.assertEqual(count, 0)

    def test_legacy_doc_has_actionable_error(self) -> None:
        source = self.root / "legacy.doc"
        source.write_bytes(b"legacy")
        with self.assertRaisesRegex(FukamiruError, "save it as .docx"):
            self.imports.preview(document_id=self.document_id, source_path=source)

    def test_project_flow_lists_source_and_creates_document_before_revision(self) -> None:
        source = self.project_root / "drafts" / "chapter-one.md"
        source.parent.mkdir()
        source.write_text("# Chapter One\n\nAn unfinished argument.", encoding="utf-8")
        (self.project_root / "ignored.csv").write_text("not,a,manuscript", encoding="utf-8")

        listing = self.imports.list_project_sources(self.project_id)

        self.assertEqual(
            [item["relative_path"] for item in listing["files"]],
            [str(Path("drafts") / "chapter-one.md")],
        )
        preview = self.imports.preview_project_manuscript(
            project_id=self.project_id,
            source_path=Path("drafts") / "chapter-one.md",
            title="Chapter One Draft",
            document_kind="paper",
        )
        document = self.connection.execute(
            "SELECT project_id, kind, title, current_revision_id "
            "FROM content_documents WHERE id = ?",
            (preview["document_id"],),
        ).fetchone()
        self.assertEqual(document["project_id"], self.project_id)
        self.assertEqual(document["kind"], "paper")
        self.assertEqual(document["title"], "Chapter One Draft")
        self.assertIsNone(document["current_revision_id"])

        confirmed = self.imports.confirm(preview["id"])

        document = self.connection.execute(
            "SELECT current_revision_id FROM content_documents WHERE id = ?",
            (preview["document_id"],),
        ).fetchone()
        self.assertEqual(document["current_revision_id"], confirmed["imported_revision_id"])

    def test_project_flow_rejects_outside_source_without_orphan_document(self) -> None:
        source = self.root / "outside.md"
        source.write_text("# Outside", encoding="utf-8")
        before = self.connection.execute(
            "SELECT COUNT(*) FROM content_documents"
        ).fetchone()[0]

        with self.assertRaisesRegex(FukamiruError, "inside the selected Project"):
            self.imports.preview_project_manuscript(
                project_id=self.project_id,
                source_path=source,
                title="Must not be created",
                document_kind="paper",
            )

        after = self.connection.execute(
            "SELECT COUNT(*) FROM content_documents"
        ).fetchone()[0]
        self.assertEqual(after, before)

    def test_failed_project_extraction_does_not_leave_orphan_document(self) -> None:
        source = self.project_root / "empty.txt"
        source.write_text("\n\n", encoding="utf-8")
        before = self.connection.execute(
            "SELECT COUNT(*) FROM content_documents"
        ).fetchone()[0]

        with self.assertRaisesRegex(FukamiruError, "no importable text"):
            self.imports.preview_project_manuscript(
                project_id=self.project_id,
                source_path=Path("empty.txt"),
                title="Empty document",
                document_kind="paper",
            )

        after = self.connection.execute(
            "SELECT COUNT(*) FROM content_documents"
        ).fetchone()[0]
        self.assertEqual(after, before)


def _docx_fixture() -> bytes:
    document = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
    <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
      <w:body>
        <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Chapter One</w:t></w:r></w:p>
        <w:p><w:r><w:t>Body text.</w:t></w:r></w:p>
        <w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/>
        </w:numPr></w:pPr><w:r><w:t>List item</w:t></w:r></w:p>
        <w:tbl><w:tr>
          <w:tc><w:p><w:r><w:t>Cell A</w:t></w:r></w:p></w:tc>
          <w:tc><w:p><w:r><w:t>Cell B</w:t></w:r></w:p></w:tc>
        </w:tr></w:tbl>
        <w:sectPr/>
      </w:body>
    </w:document>"""
    footnotes = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
    <w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
      <w:footnote w:id="1"><w:p><w:r><w:t>Footnote text</w:t></w:r></w:p></w:footnote>
    </w:footnotes>"""
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("word/document.xml", document)
        archive.writestr("word/footnotes.xml", footnotes)
        archive.writestr("word/media/image1.png", b"not-an-image")
    return output.getvalue()


def _pdf_fixture() -> bytes:
    text_stream = b"BT /F1 12 Tf 72 720 Td (Page one paragraph.) Tj ET"
    blank_stream = b""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 5 0 R] /Count 2 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 7 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length "
        + str(len(text_stream)).encode()
        + b">>\nstream\n"
        + text_stream
        + b"\nendstream",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 7 0 R >> >> /Contents 6 0 R >>"
        ),
        b"<< /Length "
        + str(len(blank_stream)).encode()
        + b">>\nstream\n"
        + blank_stream
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, value in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode())
        output.extend(value)
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode()
    )
    return bytes(output)


if __name__ == "__main__":
    unittest.main()
