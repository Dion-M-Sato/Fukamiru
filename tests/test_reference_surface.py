from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from fukamiru.documents.japanese_surface import JapaneseSurfaceQualityGate
from fukamiru.errors import NonRetryableFukamiruError
from fukamiru.revisions import ManuscriptDiscourseService
from fukamiru.revisions.references import JapaneseReferenceService
from fukamiru.worker.runner import WorkerRunner

HTML = (
    '<article id="DOC" lang="ja"><section id="BODY">'
    '<h2 id="HEAD">本文</h2><p id="P1">論証本文。</p></section>'
    '<section id="BIB"><h2 id="BIB-HEAD">参考文献</h2>'
    '<p id="REF">元の文献項目。</p></section></article>'
)


def book(reference_id: str, **metadata) -> dict:
    return {
        "reference_id": reference_id,
        "parse_status": "parsed",
        "resolution_status": "resolved",
        "title": "書名．補論、追記",
        "author": "著者",
        "authors": ["著者"],
        "record_type": "book",
        "publisher": "Verso",
        "edition": "Ｒｅｖｉｓｅｄ　ｅｄ．",
        "year": "2020",
        "is_translation": False,
        "translation_status": "confirmed_original",
        "translator_status": "not_required",
        **metadata,
    }


def render_context() -> dict:
    return {
        "proposed_html": HTML,
        "reference_entries": [
            book("reference:first", source_block_id="REF", entry_ordinal=0),
            book(
                "reference:second",
                source_block_id="REF",
                entry_ordinal=1,
                edition="2nd ed.",
                year="2024",
            ),
            book("reference:consulted", title="追加の参照文献"),
        ],
        "machine_reference_entries": [
            book("reference:machine", machine_reference_id="machine:related", title="関連文献"),
        ],
        "reference_inventory": {"reference_section_count": 1},
        "paragraph_boundary_lock": ManuscriptDiscourseService.rendered_boundary_lock(HTML, {}),
    }


def render(context: dict, version: int, worker=None) -> dict:
    worker = worker or SimpleNamespace(_record_manuscript_progress=Mock())
    return WorkerRunner._manuscript_render_references(
        worker, SimpleNamespace(workflow_version=str(version)), context
    ).context


@pytest.mark.parametrize("version", [22, 23])
@pytest.mark.parametrize("edition", ["Ｒｅｖｉｓｅｄ　ｅｄ．", "Revised ed."])
def test_abbreviation_period_and_title_punctuation_are_preserved(version, edition):
    text = JapaneseReferenceService.render_entry(
        book("ref", edition=edition), workflow_version=version
    )

    assert f"『書名．補論、追記』、{edition} Verso、2020。" in text
    assert JapaneseSurfaceQualityGate.find(text) == ()


@pytest.mark.parametrize("field", ["volume", "edition", "publication_place", "publisher", "series"])
@pytest.mark.parametrize("period", [".", "．"])
def test_field_separator_checks_each_abbreviated_field(field, period):
    text = JapaneseReferenceService.render_entry(
        book("ref", **{field: "Abbr" + period}), workflow_version=22
    )

    assert "Abbr" + period + " " in text
    assert JapaneseSurfaceQualityGate.find(text) == ()


@pytest.mark.parametrize("period", [".", "．"])
def test_last_abbreviation_keeps_both_period_and_japanese_item_terminator(period):
    text = JapaneseReferenceService.render_entry(
        book("ref", year="", publisher="Publisher" + period), workflow_version=22
    )

    assert text.endswith("Publisher" + period + " 。")
    assert JapaneseSurfaceQualityGate.find(text) == ()


def test_legacy_formatter_retains_its_existing_output():
    text = JapaneseReferenceService.render_entry(book("ref"), workflow_version=21)
    assert "Ｒｅｖｉｓｅｄ　ｅｄ．、Verso" in text


@pytest.mark.parametrize("version", [22, 23])
def test_all_reference_rows_are_safe_idempotent_and_keep_boundary_lock(version):
    context = render_context()
    original = copy.deepcopy(context)
    result = render(context, version)
    second = render(result, version)

    assert context == original
    assert second["proposed_html"] == result["proposed_html"]
    assert second["reference_render_report"]["edits"] == []
    assert second["paragraph_boundary_lock"] == result["paragraph_boundary_lock"]
    assert result["reference_render_report"]["surface_quality"] == {
        "checked_block_count": 4,
        "issues": [],
        "passed": True,
    }
    assert second["reference_render_report"]["surface_quality"]["checked_block_count"] == 4
    assert JapaneseSurfaceQualityGate.find_canonical_html(result["proposed_html"]) == ()
    guarded = JapaneseSurfaceQualityGate.guard_canonical_html(HTML, result["proposed_html"])
    assert guarded.reverted_blocks == ()
    assert guarded.canonical_html == result["proposed_html"]
    assert ManuscriptDiscourseService.validate_rendered_boundary_lock(
        guarded.canonical_html, result["paragraph_boundary_lock"]
    )["valid"]


@pytest.mark.parametrize("version", [22, 23])
@pytest.mark.parametrize("row", ["source", "candidate", "consulted", "machine"])
def test_preflight_stops_with_reference_id_and_token_before_updating_lock(version, row):
    context = render_context()
    entry = (
        context["machine_reference_entries"][0]
        if row == "machine"
        else context["reference_entries"][["source", "candidate", "consulted"].index(row)]
    )
    # Punctuation inside a title must not be silently rewritten by the formatter.
    entry["title"] = "不正な書名．、補論"
    original = copy.deepcopy(context)
    worker = SimpleNamespace(_record_manuscript_progress=Mock())

    with pytest.raises(NonRetryableFukamiruError, match="reference.surface_violation") as exc:
        render(context, version, worker)

    report = json.loads(str(exc.value).split(": ", 1)[1])
    assert len(report["issues"]) == 1
    finding = report["issues"][0]
    if row == "machine":
        sections = JapaneseReferenceService.bibliography_sections_v22(
            context["reference_entries"], machine_entries=context["machine_reference_entries"]
        )
        expected_id = sections[-1]["entries"][0]["machine_reference_id"]
    else:
        expected_id = entry["reference_id"]
    assert finding["reference_id"] == expected_id
    assert finding["issues"][0]["token"] == "．、"
    assert finding["issues"][0]["code"] == "surface.consecutive_punctuation"
    assert finding["block_id"]
    assert context == original
    worker._record_manuscript_progress.assert_not_called()


def test_preflight_checks_unchanged_rows_even_if_renderer_reports_no_edits():
    context = render_context()
    context["reference_entries"][1]["title"] = "書名．、補論"
    rendered, _ = JapaneseReferenceService.render_sections_v22(
        HTML, context["reference_entries"], machine_entries=context["machine_reference_entries"]
    )
    repeated, edits = JapaneseReferenceService.render_sections_v22(
        rendered, context["reference_entries"], machine_entries=context["machine_reference_entries"]
    )
    assert repeated == rendered
    assert edits == []
    with pytest.raises(NonRetryableFukamiruError, match="reference:second"):
        render({**context, "proposed_html": rendered}, 23)
