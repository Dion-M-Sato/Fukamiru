"""V29 reference rendering from the supplied bibliography house-style summary."""

from __future__ import annotations

import copy
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

from fukamiru.revisions.bibliography_style import format_reference
from fukamiru.revisions.discourse import ManuscriptDiscourseService
from fukamiru.revisions.references import JapaneseReferenceService
from fukamiru.worker.runner import WorkerRunner

HTML = (
    '<article id="DOC" lang="ja"><section id="BIB">'
    '<h2 id="HEADING">参考文献</h2><p id="REF">旧書誌。</p>'
    "</section></article>"
)


def record(reference_id: str, **changes: object) -> dict:
    return {
        "reference_id": reference_id,
        "source_block_id": "REF",
        "entry_ordinal": 0,
        "parse_status": "parsed",
        "resolution_status": "resolved",
        "record_type": "book",
        "publication_language": "ja",
        "author": "著者",
        "title": "書名",
        "publisher": "出版社",
        "year": "2020",
        **changes,
    }


@pytest.mark.parametrize(
    "label", ("新装版", "改訂版", "増補版", "新版", "新訳版", "文庫版"),
)
def test_edition_label_is_rendered_once_across_fields(label: str) -> None:
    entry = record(
        "duplicate-edition", volume=f": {label}", edition=label,
        unparsed_fields=[label, f": {label}"],
    )
    styled = format_reference(entry)
    assert styled.supported
    assert styled.text.count(label) == 1
    assert f": {label}" not in styled.text
    subtitle = record(
        "subtitle-edition", title=f"書名 : {label}", edition=label,
        unparsed_fields=[label],
    )
    assert format_reference(subtitle).text.count(label) == 1


def test_house_style_examples_and_japanese_punctuation_do_not_change_metadata():
    french = record(
        "french",
        publication_language="fr",
        author="Baudin, Henri",
        title="La science-fiction\u00a0: un univers en expansion",
        publication_place="Paris",
        publisher="Bordas",
        year="1971",
    )
    japanese = record(
        "japanese",
        author="ベルクソン・アンリ",
        bibliography_people=[
            {
                "role": "author",
                "name_profile": "western_katakana",
                "family": "ベルクソン",
                "given": "アンリ",
            },
            {
                "role": "translator",
                "name_profile": "japanese_native",
                "family": "田島",
                "given": "節夫",
            },
        ],
        title="物質と記憶．補論，追記",
        translators=["田島節夫"],
        publisher="白水社",
        year="1965",
        pages="５",
    )
    original = copy.deepcopy(japanese)

    assert format_reference(french).text == (
        "Baudin, Henri, La science-fiction\u00a0: un univers en expansion, Paris, Bordas, 1971."
    )
    assert format_reference(french).spans[2] == (
        "La science-fiction\u00a0: un univers en expansion",
        True,
    )
    assert JapaneseReferenceService.render_entry(japanese, workflow_version=29) == (
        "ベルクソン、アンリ『物質と記憶。補論、追記』、田島節夫訳、白水社、1965年、5頁。"
    )
    assert japanese == original


def test_articles_use_language_specific_quotes_container_italics_and_pages():
    japanese = record(
        "ja-article",
        record_type="article",
        title="論文題，補説．後編",
        container_title="学術誌",
        volume="12",
        issue="3",
        pages="１２–１５",
    )
    french = record(
        "fr-article",
        publication_language="fr",
        record_type="article",
        author="Durand, Marie",
        title="La question",
        container_title="Revue des études",
        volume="12",
        pages="20–25",
    )
    english = record(
        "en-article",
        publication_language="en",
        record_type="article",
        author="Smith, Ann",
        title="A Study",
        container_title="Research Review",
        volume="4",
        issue="2",
        pages="10-18",
    )

    assert format_reference(japanese).text == (
        "著者「論文題、補説。後編」、『学術誌』、12(3)、2020年、12-15頁。"
    )
    assert format_reference(french).text == (
        "Durand, Marie, « La question », Revue des études, 12, 2020, pp. 20-25."
    )
    assert ("Revue des études", True) in format_reference(french).spans
    assert format_reference(english).text == (
        "Smith, Ann, “A Study”, Research Review, 4(2), 2020, pp. 10-18."
    )


def test_v29_node_renders_italics_as_canonical_html_and_is_idempotent():
    french = record(
        "reference:fr",
        publication_language="fr",
        author="Baudin, Henri",
        title="La science-fiction",
        publication_place="Paris",
        publisher="Bordas",
        year="1971",
    )
    context = {
        "proposed_html": HTML,
        "reference_entries": [french],
        "paragraph_boundary_lock": ManuscriptDiscourseService.rendered_boundary_lock(HTML, {}),
    }
    worker = SimpleNamespace()
    claimed = SimpleNamespace(workflow_version="29")

    first = WorkerRunner._manuscript_render_references(worker, claimed, context).context
    second = WorkerRunner._manuscript_render_references(worker, claimed, first).context
    root = ET.fromstring(first["proposed_html"])
    paragraph = next(element for element in root.iter("p") if element.get("id") == "REF")

    assert paragraph.find("em") is not None
    assert paragraph.find("em").text == "La science-fiction"
    assert "".join(paragraph.itertext()) == (
        "Baudin, Henri, La science-fiction, Paris, Bordas, 1971."
    )
    assert second["proposed_html"] == first["proposed_html"]
    assert second["reference_render_report"]["edits"] == []
    assert second["paragraph_boundary_lock"] == first["paragraph_boundary_lock"]
    assert not JapaneseReferenceService.format_issues(
        first["proposed_html"], [french], workflow_version=29
    )
    assert context["proposed_html"] == HTML


def test_v29_generated_consulted_row_validates_by_reference_id():
    annotation_reference = record(
        "reference:annotation",
        source_block_id="memo-annotation:missing-from-generated-manuscript",
    )

    rendered, _ = JapaneseReferenceService.render_sections_v22(
        '<article id="DOC" lang="ja"><section id="BODY">'
        '<h2 id="HEADING">本文</h2><p id="P1">本文。</p>'
        "</section></article>",
        [annotation_reference],
        workflow_version=29,
        render_machine_section=False,
    )

    root = ET.fromstring(rendered)
    generated = next(
        element
        for element in root.iter("p")
        if element.get("data-reference-id") == "reference:annotation"
    )
    assert generated.get("data-reference-generated") == "v22-support-selected"
    assert not JapaneseReferenceService.format_issues(
        rendered, [annotation_reference], workflow_version=29
    )


def test_v29_machine_candidate_keeps_its_label_and_italic_journal():
    consulted = record("reference:ja")
    machine = record(
        "reference:machine",
        source_block_id="",
        publication_language="en",
        record_type="article",
        author="Smith, Ann",
        authors=["Smith, Ann"],
        title="A Study",
        container_title="Research Review",
        volume="4",
        pages="10-18",
        machine_reference_id="machine:related",
    )
    context = {
        "proposed_html": HTML,
        "reference_entries": [consulted],
        "machine_reference_entries": [machine],
    }
    worker = SimpleNamespace()
    claimed = SimpleNamespace(workflow_version="29")

    first = WorkerRunner._manuscript_render_references(worker, claimed, context).context
    second = WorkerRunner._manuscript_render_references(worker, claimed, first).context
    root = ET.fromstring(first["proposed_html"])
    machine_rows = [
        element for element in root.iter("p") if element.get("data-machine-reference-id")
    ]

    assert len(machine_rows) == 1
    assert machine_rows[0].find("em").text == "Research Review"
    assert "機械判別候補（未確認・参照済みではありません）" in first["proposed_html"]
    assert second["proposed_html"] == first["proposed_html"]


def test_selected_edition_language_overrides_source_reference_language():
    source = record(
        "source",
        language="ja",
        candidate_matches=[
            {
                "candidate_id": "fr",
                "title": "La science-fiction",
                "authors": ["Baudin, Henri"],
                "publication_language": "fr",
                "publisher": "Bordas",
                "year": "1971",
            }
        ],
    )
    selected = JapaneseReferenceService.select_candidate(source, "fr", workflow_version=29)

    assert selected["publication_language"] == "fr"
    assert format_reference(selected).text == ("Baudin, Henri, La science-fiction, Bordas, 1971.")


def test_unsupported_style_is_retained_and_reported_for_review():
    unsupported = record(
        "unknown",
        publication_language="es",
        record_type="article",
        author="Autor, Nombre",
        title="Título",
        container_title="Revista",
        pages="1-4",
    )
    rendered, _ = JapaneseReferenceService.render_sections_v22(
        HTML, [unsupported], workflow_version=29, render_machine_section=False
    )
    report = JapaneseReferenceService.quality_report(rendered, [unsupported], workflow_version=29)

    assert "旧書誌。" in rendered
    assert "Título" not in rendered
    assert any(issue["code"] == "reference.house_style_undetermined" for issue in report["issues"])


def test_pre_v29_output_keeps_existing_japanese_formatter():
    entry = record("legacy", title="書名．補論", edition="Revised ed.")
    assert JapaneseReferenceService.render_entry(entry, workflow_version=28) == (
        "著者『書名．補論』、Revised ed. 出版社、2020。"
    )


def test_unresolved_source_kind_keeps_its_original_reference_text():
    source = HTML.replace("旧書誌。", "著者『書名』、出版社、2020。")
    inventory = JapaneseReferenceService.extract(source, workflow_version=29)
    entry = inventory["entries"][0]

    assert inventory["rule_set"]["id"] == "bibliography-house-style"
    assert entry["record_type"] == "book_or_journal"
    assert format_reference(entry).supported is False
    assert (
        JapaneseReferenceService.render_entry(entry, workflow_version=29) == entry["original_text"]
    )


def test_v29_preserves_existing_semantic_inline_markup_for_review():
    source = HTML.replace("旧書誌。", "Baudin<sup>2</sup>, ancien titre.")
    entry = record(
        "reference:rich",
        publication_language="fr",
        author="Baudin, Henri",
        title="La science-fiction",
        publisher="Bordas",
        year="1971",
    )

    rendered, _ = JapaneseReferenceService.render_sections_v22(
        source, [entry], workflow_version=29, render_machine_section=False
    )
    issues = JapaneseReferenceService.format_issues(rendered, [entry], workflow_version=29)

    assert ET.fromstring(rendered).find(".//sup").text == "2"
    assert any(issue["code"] == "reference.inline_markup_requires_review" for issue in issues)


def test_ndl_mixed_authority_names_and_codes_do_not_become_a_finished_bibliography():
    # Shape observed in V29 run 003efd1b; the provider's creator list does not
    # prove which names are authors and which are translators.
    source = HTML.replace("旧書誌。", '<a href="https://example.org/source">デリダ『散種』</a>')
    entry = record(
        "reference:dissemination",
        publication_language=None,
        language="ja",
        provider="ndl_search",
        authors=[
            "Derrida, Jacques, 1930-2004",
            "藤本, 一勇, 1966-",
            "立花, 史, 1974-",
            "郷原, 佳以, 1975-",
        ],
        author="デリダ",
        title="散種",
        publication_place="JP",
        year="2013",
        original_text="デリダ『散種』",
    )
    styled = format_reference(entry)
    rendered, _ = JapaneseReferenceService.render_sections_v22(
        source, [entry], workflow_version=29, render_machine_section=False
    )
    assert not styled.supported
    assert "edition_language_unconfirmed" in styled.review_reasons
    assert "ndl_responsibility_unconfirmed" in styled.review_reasons
    assert "country_code_in_place" in styled.review_reasons
    assert '<a href="https://example.org/source">デリダ『散種』</a>' in rendered
    assert "1930-2004" not in rendered
    assert "JP" not in rendered


def test_structured_people_keep_roles_and_name_profiles_separate():
    entry = record(
        "structured",
        author="東, 浩紀, 1971-",
        bibliography_people=[
            {
                "role": "author",
                "name_profile": "japanese_native",
                "family": "東",
                "given": "浩紀",
                "authority_dates": "1971-",
            },
            {
                "role": "translator",
                "name_profile": "japanese_native",
                "family": "加藤",
                "given": "文元",
            },
        ],
    )
    styled = format_reference(entry)
    assert styled.supported
    assert styled.text.startswith("東浩紀『書名』、加藤文元訳")
    assert "1971" not in styled.text
    assert entry["bibliography_people"][0]["authority_dates"] == "1971-"


def test_japanese_source_author_beats_romanized_provider_display_name():
    entry = record(
        "azuma",
        author="東浩紀",
        authors=["Hiroki Azuma"],
        title="動物化するポストモダン",
        publisher="講談社",
        year="2001",
        provenance={"author": {"kind": "source_explicit", "source": "memo_annotation"}},
        bibliography_people=[
            {
                "role": "author",
                "name_profile": "western_latin",
                "family": "Azuma",
                "given": "Hiroki",
            }
        ],
    )

    styled = format_reference(entry)

    assert styled.supported
    assert styled.text == "東浩紀『動物化するポストモダン』、講談社、2001年。"
    assert entry["bibliography_people"][0]["family"] == "Azuma"


def test_crossref_given_family_string_needs_name_profile_before_rendering():
    entry = record(
        "crossref-name",
        provider="crossref",
        authors=["Jacques Rancière"],
        author="Jacques Rancière",
        publication_language="fr",
        title="Le partage du sensible",
    )
    styled = format_reference(entry)
    assert not styled.supported
    assert "provider_name_order_unconfirmed" in styled.review_reasons
    assert "Jacques Rancière, Le partage" not in styled.text


def test_v29_keeps_unconfirmed_edition_matches_out_of_consulted_rows():
    source = record(
        "source-work",
        expanded_reference_entries=[
            record("edition-a", consulted_status="unconfirmed", title="書名", year="2001"),
            record("edition-b", consulted_status="unconfirmed", title="書名", year="2010"),
        ],
    )
    sections = JapaneseReferenceService.bibliography_sections_v22(
        [source], workflow_version=29
    )
    assert [item["reference_id"] for item in sections[0]["entries"]] == ["source-work"]
    assert len(source["expanded_reference_entries"]) == 2
