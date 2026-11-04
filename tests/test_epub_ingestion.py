from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from fukamiru.bootstrap import bootstrap
from fukamiru.config import load_settings
from fukamiru.errors import FukamiruError
from fukamiru.knowledge import CorpusSyncService
from fukamiru.knowledge.service import IngestionService, KnowledgeSearchService
from fukamiru.storage.database import DatabasePair
from fukamiru.storage.runs import RunRepository


def _write_epub(
    path: Path,
    *,
    chapters: list[tuple[str, str]] | None = None,
    encrypted_resource: str | None = None,
    bibliography_metadata: str = "",
) -> None:
    chapters = chapters or [
        (
            "chapter-1.xhtml",
            """<html xmlns="http://www.w3.org/1999/xhtml"><head>
            <style>.hidden { display:none }</style><script>bad()</script></head><body>
            <nav><a href="chapter-2.xhtml">Navigation noise</a></nav>
            <h1>First chapter</h1><p>Alpha body with searchable evidence.</p>
            </body></html>""",
        ),
        (
            "chapter-2.xhtml",
            """<html xmlns="http://www.w3.org/1999/xhtml"><body>
            <h2>Second chapter</h2><p>Beta body follows the spine order.</p>
            <ul><li>A retained list item.</li></ul></body></html>""",
        ),
    ]
    manifest = "".join(
        f'<item id="c{index}" href="{href}" media-type="application/xhtml+xml"/>'
        for index, (href, _body) in enumerate(chapters, start=1)
    )
    spine = "".join(f'<itemref idref="c{index}"/>' for index in range(1, len(chapters) + 1))
    nav_items = "".join(
        f'<li><a href="{href}">{"One" if index == 1 else "Two"}</a></li>'
        for index, (href, _body) in enumerate(chapters, start=1)
    )
    opf = f"""<?xml version="1.0" encoding="utf-8"?>
    <package xmlns="http://www.idpf.org/2007/opf" version="3.0">
      <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
        <dc:title>Fixture Book</dc:title><dc:language>en</dc:language>
        {bibliography_metadata}
      </metadata>
      <manifest>{manifest}
        <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
      </manifest><spine>{spine}</spine>
    </package>"""
    container = """<?xml version="1.0"?>
    <container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
      <rootfiles><rootfile full-path="OEBPS/content.opf"
      media-type="application/oebps-package+xml"/></rootfiles>
    </container>"""
    nav = f"""<html xmlns="http://www.w3.org/1999/xhtml"
      xmlns:epub="http://www.idpf.org/2007/ops"><body>
      <nav epub:type="toc"><ol>{nav_items}</ol></nav></body></html>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", opf)
        archive.writestr("OEBPS/nav.xhtml", nav)
        for href, body in chapters:
            archive.writestr(f"OEBPS/{href}", body)
        if encrypted_resource:
            archive.writestr(
                "META-INF/encryption.xml",
                f"""<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
                <EncryptedData><CipherData><CipherReference URI="{encrypted_resource}"/>
                </CipherData></EncryptedData></encryption>""",
            )


class EPUBIngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        config = self.root / "local.toml"
        config.write_text(
            f"""
[server]
open_browser = false
[paths]
state_db = "{(self.root / "data/state.db").as_posix()}"
knowledge_db = "{(self.root / "data/knowledge.db").as_posix()}"
runtime_root = "{(self.root / "runtime").as_posix()}"
corpus_root = "{(self.root / "corpus").as_posix()}"
ocr_root = "{(self.root / "ocr").as_posix()}"
knowledge_artifacts_root = "{(self.root / "kb").as_posix()}"
faiss_root = "{(self.root / "faiss").as_posix()}"
[retrieval]
require_cuda = false
embedding_device = "cpu"
tokenizer_mode = "unicode"
""",
            encoding="utf-8",
        )
        self.settings = load_settings(config)
        bootstrap(self.settings)
        self.databases = DatabasePair.from_settings(self.settings)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_auto_classifies_multilingual_text_for_rag_metadata(self) -> None:
        samples = {
            "ru": (
                "Это исследование и его результаты основаны на данных из архива. "
                "В статье мы рассматриваем новые источники и методы для анализа."
            ),
            "zh": "本文研究历史资料中的语言现象，并分析不同文献之间的联系与变化。",
            "ko": "이 연구는 역사 자료를 분석하고 문헌에 나타난 언어의 변화를 검토합니다.",
            "de": (
                "Die Untersuchung der historischen Quellen und die Analyse der Daten "
                "ist für das Verständnis der Entwicklung von Bedeutung."
            ),
        }
        with self.databases.knowledge.connect() as connection:
            service = IngestionService(connection, self.settings)
            for language, content in samples.items():
                with self.subTest(language=language):
                    source = self.root / f"{language}.txt"
                    source.write_text(content, encoding="utf-8")
                    result = service.ingest_path(str(source), source_language="auto")
                    asset = connection.execute(
                        "SELECT source_language FROM source_assets WHERE id = ?",
                        (result["source_asset_id"],),
                    ).fetchone()
                    unit = connection.execute(
                        "SELECT language FROM document_units WHERE id = ?",
                        (result["document_unit_id"],),
                    ).fetchone()
                    revision = connection.execute(
                        "SELECT language FROM ocr_text_revisions WHERE id = ?",
                        (result["normalized_ocr_revision_id"],),
                    ).fetchone()
                    self.assertEqual(asset["source_language"], language)
                    self.assertEqual(unit["language"], language)
                    self.assertEqual(revision["language"], language)

    def test_auto_classification_preserves_explicit_language(self) -> None:
        source = self.root / "manual.txt"
        source.write_text(
            "Die Untersuchung der historischen Quellen und die Analyse der Daten.", encoding="utf-8"
        )
        with self.databases.knowledge.connect() as connection:
            result = IngestionService(connection, self.settings).ingest_path(
                str(source), source_language="en"
            )
            unit = connection.execute(
                "SELECT language FROM document_units WHERE id = ?",
                (result["document_unit_id"],),
            ).fetchone()
        self.assertEqual(unit["language"], "en")

    def test_ingests_spine_order_metadata_and_structural_blocks(self) -> None:
        source = self.root / "book.epub"
        _write_epub(source)
        with self.databases.knowledge.connect() as connection:
            result = IngestionService(connection, self.settings).ingest_path(str(source))
            unit = connection.execute(
                "SELECT title, language FROM document_units WHERE id = ?",
                (result["document_unit_id"],),
            ).fetchone()
            pages = connection.execute(
                "SELECT page_number, printed_label, text FROM pages "
                "WHERE document_unit_id = ? ORDER BY page_number",
                (result["document_unit_id"],),
            ).fetchall()
            spans = connection.execute(
                "SELECT structural_role, text FROM source_spans "
                "WHERE document_unit_id = ? ORDER BY ordinal",
                (result["document_unit_id"],),
            ).fetchall()
            asset = connection.execute(
                "SELECT media_type, source_language FROM source_assets WHERE id = ?",
                (result["source_asset_id"],),
            ).fetchone()
            hits = KnowledgeSearchService(connection).search("Beta body")

        self.assertEqual(tuple(unit), ("Fixture Book", "en"))
        self.assertEqual(tuple(asset), ("application/epub+zip", "en"))
        self.assertEqual([row["printed_label"] for row in pages], ["One", "Two"])
        self.assertIn("Alpha body", pages[0]["text"])
        self.assertIn("Beta body", pages[1]["text"])
        self.assertNotIn("Navigation noise", pages[0]["text"])
        self.assertNotIn("bad()", pages[0]["text"])
        self.assertIn(("list_item", "A retained list item."), [tuple(row) for row in spans])
        self.assertEqual(hits[0]["citation"]["start_label"], "Two")
        self.assertEqual(result["epub_title"], "Fixture Book")
        self.assertEqual(result["bibliography"]["metadata_status"], "partial")
        self.assertEqual(result["bibliography"]["authors"], [])

    def test_duplicate_epub_is_reused_by_content_hash(self) -> None:
        first = self.root / "first.epub"
        second = self.root / "second.epub"
        _write_epub(first)
        second.write_bytes(first.read_bytes())
        with self.databases.knowledge.connect() as connection:
            service = IngestionService(connection, self.settings)
            initial = service.ingest_path(str(first))
            duplicate = service.ingest_path(str(second))
            count = connection.execute("SELECT COUNT(*) FROM source_assets").fetchone()[0]
        self.assertFalse(initial["reused"])
        self.assertTrue(duplicate["reused"])
        self.assertEqual(duplicate["source_asset_id"], initial["source_asset_id"])
        self.assertEqual(count, 1)

    def test_ingestion_persists_epub_bibliography_and_reuses_its_identity(self) -> None:
        source = self.root / "book.epub"
        _write_epub(
            source,
            bibliography_metadata=(
                "<dc:creator>Example Author</dc:creator>"
                "<dc:publisher>Example Press</dc:publisher>"
                "<dc:date>2024</dc:date>"
                "<dc:identifier>ISBN 9781234567897</dc:identifier>"
            ),
        )
        with self.databases.knowledge.connect() as connection:
            service = IngestionService(connection, self.settings)
            first = service.ingest_path(str(source))
            second = service.ingest_path(str(source))
            links = connection.execute(
                "SELECT COUNT(*) FROM source_asset_bibliography WHERE source_asset_id = ?",
                (first["source_asset_id"],),
            ).fetchone()[0]
            unit_link = connection.execute(
                "SELECT bibliographic_record_id FROM document_unit_bibliography "
                "WHERE document_unit_id = ? AND relation_type = 'describes'",
                (first["document_unit_id"],),
            ).fetchone()[0]
            hit = KnowledgeSearchService(connection).search("Beta body")[0]
        bibliography = first["bibliography"]
        self.assertEqual(bibliography["title"], "Fixture Book")
        self.assertEqual(bibliography["authors"], ["Example Author"])
        self.assertEqual(bibliography["publisher"], "Example Press")
        self.assertEqual(bibliography["publication_date"], "2024")
        self.assertEqual(bibliography["isbn"], "9781234567897")
        self.assertEqual(bibliography["metadata_status"], "complete")
        self.assertEqual(links, 1)
        self.assertEqual(unit_link, bibliography["id"])
        self.assertEqual(second["bibliography"]["id"], bibliography["id"])
        self.assertEqual(hit["bibliography"]["id"], bibliography["id"])
        self.assertEqual(hit["citation"]["bibliographic_record_id"], bibliography["id"])

    def test_rejects_encrypted_and_image_only_epubs(self) -> None:
        encrypted = self.root / "encrypted.epub"
        _write_epub(encrypted, encrypted_resource="OEBPS/chapter-1.xhtml")
        image_only = self.root / "images.epub"
        _write_epub(
            image_only,
            chapters=[
                (
                    "chapter-1.xhtml",
                    '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
                    '<img src="page.jpg" alt=""/></body></html>',
                )
            ],
        )
        with self.databases.knowledge.connect() as connection:
            service = IngestionService(connection, self.settings)
            with self.assertRaisesRegex(FukamiruError, "DRM/encryption"):
                service.ingest_path(str(encrypted))
            with self.assertRaisesRegex(FukamiruError, "EPUB_IMAGE_ONLY"):
                service.ingest_path(str(image_only))

    def test_corpus_sync_discovers_and_ingests_epub_with_text_regressions(self) -> None:
        managed = self.settings.paths.corpus_root / "managed_sources" / "books"
        managed.mkdir(parents=True)
        _write_epub(managed / "book.epub")
        (managed / "plain.txt").write_text("Plain TXT regression content.", encoding="utf-8")
        (managed / "notes.md").write_text("# Markdown\n\nRegression content.", encoding="utf-8")
        (managed / "page.html").write_text(
            "<html><style>bad</style><body><p>HTML regression content.</p></body></html>",
            encoding="utf-8",
        )
        with self.databases.state.connect() as connection:
            run_id = (
                RunRepository(connection)
                .create(
                    workflow_name="corpus_sync",
                    workflow_version="fixture",
                    entry_node="scan",
                    project_id=None,
                    inputs={},
                )
                .run_id
            )
        service = CorpusSyncService(self.databases, self.settings)
        scanned = service.scan(run_id, str(managed))
        summary = service.ingest_ready_sources(run_id, scanned["corpus_batch_id"])
        snapshot = service.snapshot(scanned["corpus_batch_id"])
        self.assertEqual(scanned["discovered_count"], 4)
        self.assertEqual(summary["ready_count"], 4)
        self.assertEqual(summary["failed_count"], 0)
        self.assertEqual(
            [item["relative_path"] for item in snapshot["items"]],
            ["book.epub", "notes.md", "page.html", "plain.txt"],
        )


if __name__ == "__main__":
    unittest.main()
