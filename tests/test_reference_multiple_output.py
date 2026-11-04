"""Formal V29 manifestation-output behavior."""

from __future__ import annotations

import copy

from fukamiru.revisions.reference_canonical import finalize_reference_v29
from fukamiru.revisions.references import JapaneseReferenceService

HTML = (
    '<article id="DOC" lang="ja"><section id="REF">'
    '<h2 id="HEAD">参考文献</h2><p id="ROW">原稿の参考文献</p>'
    '</section></article>'
)


def _candidate(candidate_id: str, isbn: str, year: str) -> dict:
    return {
        "candidate_id": candidate_id,
        "title": "芸術の規則",
        "record_type": "book",
        "publisher": "藤原書店",
        "year": year,
        "isbn": isbn,
        "publication_language": "ja",
        "provider": "ndl_search",
        "url": f"https://ndlsearch.ndl.go.jp/books/{candidate_id}",
        "responsibility_sources": [{
            "provider": "ndl_search",
            "url": f"https://ndlsearch.ndl.go.jp/books/{candidate_id}",
            "responsibility_source": "exact_record_detail",
            "responsibility_raw": ["ピエール・ブルデュー[著]", "石井, 洋二郎訳"],
        }],
    }


def _parent(candidates: list[dict]) -> dict:
    return {
        "reference_id": "ref-1", "source_block_id": "ROW", "entry_ordinal": 0,
        "parse_status": "parsed", "title": "芸術の規則", "author": "ブルデュー",
        "original_text": "ブルデュー『芸術の規則』", "candidate_matches": candidates,
        "provenance": {
            "title": {"kind": "source_explicit", "label": "原稿に明記"},
            "author": {"kind": "source_explicit", "label": "原稿に明記"},
        },
    }


def _render_quality(parent: dict) -> tuple[str, dict]:
    html, _ = JapaneseReferenceService.render_sections_v22(
        HTML, [parent], workflow_version=29, render_machine_section=False,
    )
    quality = JapaneseReferenceService.quality_report(html, [parent], workflow_version=29)
    return html, quality


def test_multiple_ready_is_formal_output_and_order_is_stable() -> None:
    candidates = [
        _candidate("new", "4-89434-030-5", "1996"),
        _candidate("old", "4-89434-009-7", "1995"),
    ]
    left = finalize_reference_v29(_parent(candidates))
    right = finalize_reference_v29(_parent(list(reversed(candidates))))
    assert left["reference_state_v29"] == "MULTIPLE_MANIFESTATIONS_READY"
    assert left["manifestation_clusters"] == right["manifestation_clusters"]
    assert [item["isbn"] for item in left["expanded_reference_entries"]] == [
        "4-89434-009-7", "4-89434-030-5",
    ]
    html, quality = _render_quality(left)
    assert "候補1" in html and "候補2" in html
    assert quality["passed"]
    assert not any(
        item["code"] in {
            "reference.manifestation_selection_unconfirmed",
            "reference.final_revision_blocked",
        }
        for item in quality["issues"]
    )


def test_duplicate_provider_representation_is_one_output() -> None:
    candidate = _candidate("one", "4-89434-009-7", "1995")
    duplicate = copy.deepcopy(candidate)
    duplicate["candidate_id"] = "two"
    output = finalize_reference_v29(_parent([candidate, duplicate]))
    assert len(output["manifestation_clusters"]) == 1
    assert output["reference_output_mode_v29"] == "SINGLE"
    html, _ = _render_quality(output)
    assert "候補1" not in html


def test_two_ready_and_one_incomplete_outputs_only_two() -> None:
    incomplete = _candidate("incomplete", "4-89434-999-9", "1998")
    incomplete["responsibility_sources"] = []
    incomplete["authors"] = []
    output = finalize_reference_v29(_parent([
        _candidate("old", "4-89434-009-7", "1995"),
        _candidate("new", "4-89434-030-5", "1996"),
        incomplete,
    ]))
    assert output["reference_output_mode_v29"] == "MULTIPLE"
    assert output["output_candidate_count"] == 2
    assert output["review_lead_count"] == 1
    html, quality = _render_quality(output)
    assert "候補1" in html and "候補2" in html and "候補3" not in html
    assert quality["passed"]


def test_mixed_unready_candidate_is_audited_but_not_output() -> None:
    incomplete = [
        _candidate(f"incomplete-{index}", f"4-89434-99{index}-9", str(1998 + index))
        for index in range(8)
    ]
    for item in incomplete:
        item["responsibility_sources"] = []
        item["authors"] = []
    output = finalize_reference_v29(_parent([
        _candidate("ready", "4-89434-009-7", "1995"), *incomplete,
    ]))
    assert output["reference_output_mode_v29"] == "SINGLE"
    assert len(output["review_leads"]) == 8
    assert output["discovered_manifestation_count"] == 9
    assert output["output_candidate_count"] == 1
    html, quality = _render_quality(output)
    assert "候補2" not in html
    assert quality["passed"]


def test_all_unready_candidates_remain_in_audit_only() -> None:
    incomplete = _candidate("incomplete", "4-89434-999-9", "1998")
    incomplete["responsibility_sources"] = []
    incomplete["authors"] = []
    output = finalize_reference_v29(_parent([incomplete]))
    assert output["reference_output_mode_v29"] == "REVIEW"
    assert len(output["expanded_reference_entries"]) == 0
    assert len(output["review_leads"]) == 1
    html, quality = _render_quality(output)
    assert "候補1" not in html and "要確認" in html
    assert not quality["passed"]


def test_no_candidates_is_unresolved_but_self_work_is_review() -> None:
    unresolved = finalize_reference_v29(_parent([]))
    html, quality = _render_quality(unresolved)
    assert unresolved["reference_output_mode_v29"] == "UNRESOLVED"
    assert "未解決" in html and not quality["passed"]
    self_work = _parent([])
    self_work["is_self_authored"] = True
    review = finalize_reference_v29(self_work)
    html, quality = _render_quality(review)
    assert review["reference_output_mode_v29"] == "REVIEW"
    assert "要確認" in html and not quality["passed"]


def test_single_ndl_creator_fallback_requires_matching_work_author() -> None:
    source = {
        "candidate_id": "english-book", "title": "Scales of Justice",
        "record_type": "book", "publication_language": "en",
        "publisher": "Columbia University Press", "year": "2009",
        "provider": "ndl_search", "author": "Nancy Fraser",
        "work_author_identity": "Nancy Fraser",
        "url": "https://ndlsearch.ndl.go.jp/books/example",
        "responsibility_sources": [{
            "provider": "ndl_search", "url": "https://ndlsearch.ndl.go.jp/books/example",
            "responsibility_source": "ndl_search:dc_creator_mixed",
            "responsibility_raw": ["Fraser, Nancy"],
            "authors": ["Fraser, Nancy"], "translators": [], "editors": [],
        }],
    }
    resolved = finalize_reference_v29(_parent([source]))
    candidate = resolved["candidate_matches"][0]
    assert candidate["authors"] == ["Nancy Fraser"]
    assert candidate["bibliography_people"][0]["role"] == "author"
    assert candidate["single_creator_fallback_audit"]
    multiple = copy.deepcopy(source)
    multiple["responsibility_sources"][0]["responsibility_raw"].append("Smith, John")
    multiple["responsibility_sources"][0]["authors"].append("Smith, John")
    guarded = finalize_reference_v29(_parent([multiple]))
    assert guarded["candidate_matches"][0]["authors"] == []
    unverified = copy.deepcopy(source)
    unverified.pop("work_author_identity")
    guarded = finalize_reference_v29(_parent([unverified]))
    assert guarded["candidate_matches"][0]["authors"] == []


def test_cross_script_creator_needs_provider_and_search_hypothesis() -> None:
    candidate = {
        "candidate_id": "mulvey", "title": "Visual and Other Pleasures",
        "record_type": "book", "publication_language": "en",
        "publisher": "Palgrave Macmillan", "year": "1989",
        "required_missing_fields": ["translation_status_confirmation"],
        "provider": "ndl_search", "url": "https://ndlsearch.ndl.go.jp/books/mulvey",
        "responsibility_sources": [{
            "provider": "ndl_search", "url": "https://ndlsearch.ndl.go.jp/books/mulvey",
            "responsibility_raw": ["Mulvey, Laura"], "authors": ["Mulvey, Laura"],
        }],
    }
    parent = _parent([candidate])
    parent.update(title="Visual and Other Pleasures", author="マルヴィ",
                  search_hypotheses={"authors": ["マルヴィ", "Laura Mulvey"]})
    resolved = finalize_reference_v29(parent)
    assert resolved["candidate_matches"][0]["authors"] == ["Laura Mulvey"]
    assert resolved["candidate_matches"][0]["v29_render_status"] == "render_ready"
    assert resolved["candidate_matches"][0]["translation_status"] == (
        "evidence_supported_original"
    )
    assert resolved["candidate_matches"][0].get("is_translation") is None
    audit = resolved["candidate_matches"][0]["single_creator_fallback_audit"]
    assert audit["identity_basis"] == "cross_script_hypothesis_and_provider_creator"
    without_alias = copy.deepcopy(parent)
    without_alias.pop("search_hypotheses")
    guarded = finalize_reference_v29(without_alias)
    assert guarded["candidate_matches"][0]["authors"] == []
    with_two_creators = copy.deepcopy(parent)
    source = with_two_creators["candidate_matches"][0]["responsibility_sources"][0]
    source["responsibility_raw"].append("Smith, John")
    source["authors"].append("Smith, John")
    guarded = finalize_reference_v29(with_two_creators)
    assert guarded["candidate_matches"][0]["authors"] == []


def test_hybrid_hegel_name_and_translator_are_render_ready() -> None:
    candidate = {
        "candidate_id": "hegel", "title": "精神現象学", "record_type": "book",
        "publication_language": "ja", "publisher": "未知谷", "year": "2018",
        "provider": "ndl_search", "url": "https://ndlsearch.ndl.go.jp/books/hegel",
        "responsibility_sources": [{
            "provider": "ndl_search", "url": "https://ndlsearch.ndl.go.jp/books/hegel",
            "responsibility_raw": ["G.W.F.ヘーゲル [著]", "牧野紀之 訳"],
        }],
    }
    resolved = finalize_reference_v29(_parent([candidate]))
    people = resolved["candidate_matches"][0]["bibliography_people"]
    assert people[0]["name_profile"] == "western_initials_katakana"
    assert people[0]["family"] == "ヘーゲル"
    assert people[0]["given"] == "G.W.F."
    assert any(person["role"] == "translator" for person in people)


def test_ready_foreign_edition_is_review_lead_for_japanese_mention() -> None:
    japanese = _candidate("japanese", "978-4-588-00989-1", "2013")
    japanese["title"] = "散種"
    foreign = {
        "candidate_id": "foreign", "title": "Dissemination", "record_type": "book",
        "publisher": "University of Chicago Press", "year": "1981",
        "isbn": "0-226-14334-1", "publication_language": "en",
        "provider": "openalex", "bibliography_people": [{
            "role": "author", "name_profile": "western_latin",
            "family": "Derrida", "given": "Jacques",
        }],
    }
    parent = _parent([japanese, foreign])
    parent["title"] = "散種"
    output = finalize_reference_v29(parent)
    assert output["reference_output_mode_v29"] == "SINGLE"
    assert output["output_candidate_count"] == 1
    assert output["review_leads"][0]["reason"] == (
        "foreign_language_manifestation_without_explicit_evidence"
    )
    selected = copy.deepcopy(parent)
    selected["selection_evidence"] = {"isbn": "0-226-14334-1"}
    output = finalize_reference_v29(selected)
    assert output["selected_candidate_id"] == "foreign"


def test_known_translation_without_translator_is_not_single_output() -> None:
    candidate = _candidate("translation", "978-4-10-506701-4", "1986")
    candidate["title"] = "性の歴史Ⅰ"
    candidate["responsibility_sources"][0]["responsibility_raw"] = [
        "ミシェル・フーコー[著]",
    ]
    candidate["is_translation"] = True
    candidate["translation_status"] = "confirmed_translation"
    candidate["translators"] = []
    parent = _parent([candidate])
    parent.update(title="性の歴史Ⅰ", author="フーコー")
    output = finalize_reference_v29(parent)
    assert output["reference_output_mode_v29"] == "REVIEW"
    assert output["output_candidate_count"] == 0
    assert output["review_lead_count"] == 1
