from __future__ import annotations

from xml.etree import ElementTree as ET

import pytest

from fukamiru.documents.html_blocks import canonical_html_blocks
from fukamiru.errors import FukamiruError
from fukamiru.translation.book import (
    assert_translated_book,
    parse_markdown_book,
    render_translated_book,
    validate_translated_book,
)

BOOK_FIXTURE = """<!-- page 6 -->

# Chapitre premier

# Un système de classement documentaire

Le classement est important¹ et la recherche demeure rare².

1. Première note qui continue
sur une seconde ligne.

2. Deuxième note.

---

<!-- page 7 -->

12

Chapitre premier

Le débat porte sur les catalogues¹⁴.

![figure](img-1.jpeg)

14. Note consacrée aux catalogues.

---

<!-- page 8 -->

13

Chapitre premier

## Une section

Le texte continue.
"""


def _render_fixture() -> tuple[str, dict[str, object]]:
    book = parse_markdown_book(BOOK_FIXTURE)
    records = [dict(segment) for segment in book.segments]
    translated = [f"訳:{segment['text']}" for segment in book.segments]
    rendered = render_translated_book("run-book-footnotes", translated, records, "ja")
    return rendered, book.manifest()


def test_markdown_book_structures_and_preserves_stable_footnotes() -> None:
    book = parse_markdown_book(BOOK_FIXTURE)

    assert len(book.reference_ids) == 3
    assert len(book.footnotes) == 3
    assert not book.issues
    assert book.discarded == {
        "footnote_definition": 3,
        "horizontal_rule": 2,
        "page_number": 2,
        "running_header": 2,
        "image": 1,
    }
    ref_14 = next(
        segment
        for segment in book.segments
        if segment.get("footnote_id_after")
        and segment["text"].endswith("catalogues")
    )
    note_14 = next(note for note in book.footnotes if note["label"] == "14")
    assert ref_14["footnote_id_after"] == note_14["footnote_id"]
    assert "page" not in note_14["footnote_id"].casefold()


def test_book_renderer_reflows_chapters_and_emits_linked_chapter_endnotes() -> None:
    rendered, manifest = _render_fixture()
    root = ET.fromstring(rendered)

    assert root.get("data-book-reflow") == "true"
    assert len(root.findall(".//sup[@data-footnote-reference='true']")) == 3
    definitions = root.findall(".//li[@data-footnote-definition='true']")
    assert len(definitions) == 3
    assert not root.findall(".//span[@class='footnote-number']")
    list_blocks = [
        block for block in canonical_html_blocks(rendered) if block["type"] == "ol1"
    ]
    assert len(list_blocks) == 3
    assert all(not block["text"].startswith(("1 ", "2 ", "3 ")) for block in list_blocks)
    assert root.findall(".//section[@data-footnote-placement='chapter-end']")
    assert "<!-- page" not in rendered
    assert "![" not in rendered
    assert ">---<" not in rendered
    assert "># " not in rendered
    assert rendered.count("Chapitre premier") == 1
    report = validate_translated_book(rendered, manifest)
    assert report["status"] == "passed"
    assert report["reference_count"] == report["definition_count"] == 3


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (
            lambda value: value.replace(' data-footnote-reference="true"', "", 1),
            "book.reference_order_or_count",
        ),
        (
            lambda value: value.replace("</article>", '<p id="P-RAW">![x](x.png)</p></article>'),
            "book.markdown_image",
        ),
    ],
)
def test_book_approval_gate_blocks_lost_notes_and_raw_markdown(
    mutation: object, expected_code: str
) -> None:
    rendered, manifest = _render_fixture()
    damaged = mutation(rendered)  # type: ignore[operator]

    report = validate_translated_book(damaged, manifest)

    assert report["status"] == "blocked"
    assert expected_code in {issue["code"] for issue in report["issues"]}
    with pytest.raises(FukamiruError, match="quality gate blocked approval"):
        assert_translated_book(damaged, manifest)


def test_stable_ids_do_not_depend_on_translated_wording() -> None:
    first = parse_markdown_book(BOOK_FIXTURE)
    second = parse_markdown_book(BOOK_FIXTURE)

    assert first.reference_ids == second.reference_ids
    assert [note["footnote_id"] for note in first.footnotes] == [
        note["footnote_id"] for note in second.footnotes
    ]
