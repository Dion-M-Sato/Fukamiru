from __future__ import annotations

from pathlib import Path

from pypdf import PdfWriter

from fukamiru.knowledge.bibliography import extract_source_bibliography
from fukamiru.revisions.references import JapaneseReferenceService


def test_pdf_document_info_is_read_without_inventing_missing_fields(tmp_path: Path) -> None:
    path = tmp_path / "paper.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.add_metadata({
        "/Title": "Known Work", "/Author": "A. Researcher", "/DOI": "10.1234/example"
    })
    with path.open("wb") as handle:
        writer.write(handle)

    bibliography = extract_source_bibliography(path, fallback_title=path.stem)
    assert bibliography["title"] == "Known Work"
    assert bibliography["authors"] == ["A. Researcher"]
    assert bibliography["doi"] == "10.1234/example"
    assert bibliography["publication_date"] is None
    assert bibliography["metadata_status"] == "partial"


def test_html_citation_metadata_is_ready_for_manuscript_reference(tmp_path: Path) -> None:
    path = tmp_path / "article.html"
    path.write_text(
        "<html><head>"
        '<meta name="citation_title" content="A Source Article">'
        '<meta name="citation_author" content="First Author">'
        '<meta name="citation_author" content="Second Author">'
        '<meta name="citation_journal_title" content="Research Review">'
        '<meta name="citation_publication_date" content="2023-09-01">'
        '<meta name="citation_doi" content="10.5678/article">'
        "</head><body><p>Evidence.</p></body></html>",
        encoding="utf-8",
    )
    bibliography = extract_source_bibliography(path, fallback_title=path.stem)
    assert bibliography["record_type"] == "article"
    assert bibliography["authors"] == ["First Author", "Second Author"]
    assert bibliography["metadata_status"] == "complete"

    reference = JapaneseReferenceService.reference_from_usage_v22({
        "usage_id": "usage-1",
        "provider": "knowledge_base",
        "provider_record_id": "chunk-1",
        "source_type": "local_rag",
        "disposition": "consulted_or_cited",
        "title": bibliography["title"],
        "authors": bibliography["authors"],
        "provenance": {"citation": bibliography},
    })
    assert reference["title"] == "A Source Article"
    assert reference["container_title"] == "Research Review"
    assert reference["year"] == "2023"
    assert reference["doi"] == "10.5678/article"


def test_markdown_front_matter_accepts_windows_line_endings(tmp_path: Path) -> None:
    path = tmp_path / "chapter.md"
    path.write_bytes(
        b"---\r\ntitle: Chapter Title\r\nauthor: Example Writer\r\n"
        b"date: 2022\r\n---\r\n\r\nBody text."
    )
    bibliography = extract_source_bibliography(path, fallback_title=path.stem)
    assert bibliography["title"] == "Chapter Title"
    assert bibliography["authors"] == ["Example Writer"]
    assert bibliography["publication_date"] == "2022"
