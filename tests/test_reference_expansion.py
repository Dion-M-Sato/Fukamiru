"""Post-Phase-A manifestation selection and evidence reconciliation contracts."""

from __future__ import annotations

import copy

import pytest

from fukamiru.revisions.reference_canonical import (
    finalize_reference_v29,
    reconcile_same_manifestation_v29,
)
from fukamiru.revisions.reference_web import parse_ndl_detail_metadata
from fukamiru.revisions.references import JapaneseReferenceService

HTML = (
    '<article id="DOC" lang="ja"><section id="REF-SEC">'
    '<h2 id="REF-H">参考文献</h2><p id="REF-1">ブルデュー『芸術の規則』</p>'
    "</section></article>"
)


def _candidate(
    candidate_id: str,
    *,
    title: str = "芸術の規則",
    isbn: str,
    year: str,
    volume: str | None = None,
    author: str = "ピエール・ブルデュー[著]",
    translator: str = "石井, 洋二郎訳",
    publisher: str = "藤原書店",
) -> dict:
    return {
        "candidate_id": candidate_id,
        "title": title,
        "isbn": isbn,
        "year": year,
        "volume": volume,
        "publisher": publisher,
        "record_type": "book",
        "publication_language": "ja",
        "provider": "ndl_search",
        "url": f"https://ndlsearch.ndl.go.jp/books/{candidate_id}",
        "responsibility_sources": [
            {
                "provider": "ndl_search",
                "url": f"https://ndlsearch.ndl.go.jp/books/{candidate_id}",
                "responsibility_source": "exact_record_detail",
                "responsibility_raw": [author, translator],
            }
        ],
    }


def _parent(*, candidates: list[dict] | None = None, hints: dict | None = None) -> dict:
    return {
        "reference_id": "r-art",
        "source_block_id": "REF-1",
        "entry_ordinal": 0,
        "parse_status": "parsed",
        "original_text": "ブルデュー『芸術の規則』",
        "title": "芸術の規則",
        "author": "ブルデュー",
        "record_type": "book_or_journal",
        "provenance": {
            "title": {"kind": "source_explicit", "label": "原稿に明記"},
            "author": {"kind": "source_explicit", "label": "原稿に明記"},
        },
        "selection_evidence": hints or {},
        "candidate_matches": candidates or [
            _candidate("vol1", isbn="4-89434-009-7", year="1995", volume="1"),
            _candidate("vol2", isbn="4-89434-030-5", year="1996", volume="2"),
        ],
    }


def _target_codes(parent: dict) -> set[str]:
    rendered, _ = JapaneseReferenceService.render_sections_v22(
        HTML, [parent], workflow_version=29, render_machine_section=False
    )
    report = JapaneseReferenceService.quality_report(rendered, [parent], workflow_version=29)
    return {
        issue["code"]
        for issue in report["issues"]
        if issue.get("reference_id") == parent["reference_id"]
    }


def test_ready_candidates_without_hint_are_formal_multiple_output() -> None:
    parent = finalize_reference_v29(_parent())
    assert parent["reference_state_v29"] == "MULTIPLE_MANIFESTATIONS_READY"
    assert parent["consulted_manifestation_id"] is None
    assert parent["v29_ready_manifestation_count"] == 2
    assert parent["final_revision_blocked"] is False
    assert len(parent["expanded_reference_entries"]) == 2
    assert not _target_codes(parent)


@pytest.mark.parametrize(
    ("hints", "expected"),
    [
        ({"volume": "2"}, ("vol2", "1996")),
        ({"year": "1995"}, ("vol1", "1995")),
        ({"isbn": "4-89434-030-5"}, ("vol2", "1996")),
    ],
)
def test_explicit_edition_evidence_selects_one(hints: dict, expected: tuple[str, str]) -> None:
    parent = finalize_reference_v29(_parent(hints=hints))
    assert parent["reference_state_v29"] == "SINGLE_MANIFESTATION_RESOLVED"
    assert parent["selected_candidate_id"] == expected[0]
    assert parent["year"] == expected[1]
    assert parent["consulted_manifestation_id"]
    assert parent["final_revision_blocked"] is False
    assert parent["v29_render_status"] == "render_ready"
    assert "reference.manifestation_selection_unconfirmed" not in _target_codes(parent)
    assert "reference.house_style_undetermined" not in _target_codes(parent)
    assert "reference.metadata_incomplete" not in _target_codes(parent)


def test_ambiguous_or_conflicting_hint_never_selects_first_candidate() -> None:
    ambiguous = finalize_reference_v29(_parent(hints={"publisher": "藤原書店"}))
    conflict = finalize_reference_v29(_parent(hints={"isbn": "9780000000000"}))
    assert ambiguous["reference_state_v29"] == "MULTIPLE_MANIFESTATIONS_READY"
    assert not ambiguous.get("selected_candidate_id")
    assert conflict["reference_state_v29"] == "UNRESOLVED_METADATA"
    assert conflict["consulted_manifestation_id"] is None


def test_same_isbn_representations_cluster_before_selection() -> None:
    first = _candidate("representation-a", isbn="4-622-04905-8", year="1997")
    second = copy.deepcopy(first)
    second["candidate_id"] = "representation-b"
    second["url"] = "https://ndlsearch.ndl.go.jp/books/other"
    parent = finalize_reference_v29(_parent(candidates=[first, second]))
    assert parent["v29_ready_candidate_count"] == 2
    assert parent["v29_ready_manifestation_count"] == 1
    assert parent["reference_state_v29"] == "SINGLE_MANIFESTATION_RESOLVED"


def test_bright_chamber_isbn_selects_only_matching_manifestation() -> None:
    old = _candidate(
        "1985", title="明るい部屋", isbn="4-622-00489-5", year="1985",
        author="ロラン・バルト[著]", translator="花輪, 光訳", publisher="みすず書房",
    )
    newer = {**old, "candidate_id": "1997", "isbn": "4-622-04905-8", "year": "1997"}
    for isbn, expected in ((old["isbn"], "1985"), (newer["isbn"], "1997")):
        source = _parent(candidates=[old, newer], hints={"isbn": isbn})
        source.update(title="明るい部屋", author="バルト", original_text="バルト『明るい部屋』")
        parent = finalize_reference_v29(source)
        assert parent["selected_candidate_id"] == expected
        assert parent["v29_render_status"] == "render_ready"


def test_cross_candidate_role_correction_requires_same_manifestation() -> None:
    ndl = _candidate(
        "ndl", title="正義の秤(スケール)", isbn="978-4-588-60327-3",
        year="2013", author="ナンシー・フレイザー[著]", translator="向山, 恭一訳",
        publisher="法政大学出版局",
    )
    openalex = {
        **ndl,
        "candidate_id": "openalex",
        "provider": "openalex",
        "url": "https://openalex.org/W1",
        "responsibility_sources": [
            {"provider": "openalex", "authors": ["Nancy Fraser", "恭一 向山"]}
        ],
    }
    records, audit = reconcile_same_manifestation_v29([openalex, ndl])
    assert records[0]["translators"] == ["向山恭一"]
    assert "恭一 向山" not in records[0]["authors"]
    assert any(item["reason"] == "same_manifestation_isbn_match" for item in audit)
    other = {**ndl, "candidate_id": "other", "isbn": "9780000000000"}
    guarded, guarded_audit = reconcile_same_manifestation_v29([openalex, other])
    assert guarded_audit == []
    assert guarded[0]["translators"] == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("La chambre claire 書誌注記 文献あり 索引あり", "La chambre claire"),
        (
            "La chambre claire: note sur la photographie 書誌注記 巻末: 参考文献",
            "La chambre claire: note sur la photographie",
        ),
        ("Scales of Justice 書誌注記 文献あり 索引あり", "Scales of Justice"),
    ],
)
def test_ndl_original_title_stops_at_section_boundary(source: str, expected: str) -> None:
    fields = parse_ndl_detail_metadata(f"<dt>原タイトル</dt><dd>{source}</dd>")
    assert fields["original_title"] == expected
    assert fields["original_title_parser_rule"] == "ndl_original_title_section"
    assert "書誌注記" in fields["original_title_raw"]
