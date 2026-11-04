"""Phase G translation-side discovery, diagnostics, and Web safety contracts."""

from __future__ import annotations

from fukamiru.revisions.reference_canonical import (
    canonicalize_reference,
    finalize_reference_v29,
)
from fukamiru.revisions.reference_web import (
    SearchLead,
    _translation_side_discovery_plan,
    _web_relevance_decision,
    enrich_from_web_v29,
    parse_researcher_contribution_items,
)
from fukamiru.revisions.references import JapaneseReferenceService


def _ready(candidate_id: str, title: str, language: str = "en") -> dict:
    url = f"https://catalog.example/books/{candidate_id}"
    return {
        "candidate_id": candidate_id,
        "title": title,
        "record_type": "book",
        "publication_language": language,
        "publisher": "Academic Press",
        "year": "1977",
        "authors": ["Raymond Williams"],
        "url": url,
        "responsibility_sources": [{
            "provider": "catalog", "url": url,
            "responsibility_raw": ["Raymond Williams 著"],
            "responsibility_source": "exact_record_detail",
            "authors": ["Raymond Williams"],
        }],
    }


def test_translation_side_discovery_triggers_for_ready_foreign_only() -> None:
    plan = _translation_side_discovery_plan(
        "マルクス主義と文学", "ウィリアムズ",
        [_ready("original", "Marxism and Literature")],
        {"titles": ["Marxism and Literature"], "authors": ["Raymond Williams"]},
    )
    assert plan["triggered"] is True
    assert plan["mode"] == "translation_side_discovery"
    assert [item["tier"] for item in plan["queries"]][:3] == ["J1", "J1", "J1"]


def test_translation_side_discovery_does_not_trigger_with_ready_japanese() -> None:
    plan = _translation_side_discovery_plan(
        "邦訳", "著者", [_ready("jp", "邦訳", "ja")], {},
    )
    assert plan["triggered"] is False
    assert plan["queries"] == []


def test_translation_side_discovery_does_not_trigger_for_latin_mention() -> None:
    plan = _translation_side_discovery_plan(
        "Marxism and Literature", "Raymond Williams",
        [_ready("original", "Marxism and Literature")], {},
    )
    assert plan["triggered"] is False


def test_explicit_japanese_manifestation_is_materialized_and_related() -> None:
    original = _ready("original", "Marxism and Literature")
    url = "https://ndlsearch.ndl.go.jp/books/japanese"

    def search(_query: str) -> list[str]:
        return [SearchLead(url, "マルクス主義と文学", "ウィリアムズ 訳書", 1)]

    def fetch(page_url: str) -> dict:
        return {
            "url": page_url, "status": "retrieved", "title": "マルクス主義と文学",
            "metadata_excerpt": "マルクス主義と文学 ウィリアムズ著 山田太郎訳 出版社 1980",
            "structured_metadata": {
                "title": "マルクス主義と文学", "record_type": "book",
                "publication_language": "ja", "publisher": "出版社", "year": "1980",
                "authors": ["Raymond Williams"], "translators": ["山田太郎"],
                "isbn": "9784000000001", "original_title": "Marxism and Literature",
                "original_title_relation_verified": True,
            },
        }

    discovered = enrich_from_web_v29(
        {
            "reference_id": "r", "title": "マルクス主義と文学",
            "author": "ウィリアムズ", "candidate_matches": [original],
            "search_hypotheses": {"titles": ["Marxism and Literature"]},
        },
        search=search, fetch=fetch,
    )
    assert discovered["translation_side_discovery_triggered"] is True
    assert discovered["japanese_manifestation_ready"] is True
    final = finalize_reference_v29(discovered)
    assert final["work_relation_status"] == "WORK_RELATION_CONFIRMED"
    assert final["source_language_manifestation_missing"] is False


def test_foreign_only_result_has_precise_source_language_diagnostic() -> None:
    final = finalize_reference_v29({
        "reference_id": "r", "title": "マルクス主義と文学",
        "candidate_matches": [_ready("original", "Marxism and Literature")],
        "translation_side_discovery_triggered": True,
        "relation_resolution_mode": "translation_side_discovery",
    })
    assert final["source_language_manifestation_missing"] is True
    assert final["translation_manifestation_not_found"] is True
    assert final["work_relation_unconfirmed"] is True
    assert final["reference_diagnostic_codes"] == [
        "reference.source_language_manifestation_missing"
    ]


def test_dictionary_page_is_rejected_even_with_exact_title() -> None:
    lead = SearchLead(
        "https://dictionary.cambridge.org/dictionary/english/culture",
        "Culture", "definition of culture", 1,
    )
    decision = _web_relevance_decision(str(lead), "Culture", "Raymond Williams", [])
    # Preserve SearchLead attributes while using the URL value.
    decision = _web_relevance_decision(lead, "Culture", "Raymond Williams", [])
    assert decision["decision"] == "reject"
    assert decision["reject_reason"] == "dictionary_or_encyclopedia"


def test_generic_exact_title_with_wrong_author_is_rejected() -> None:
    lead = SearchLead(
        "https://example.com/culture", "Culture", "A novel by Jane Smith", 1,
    )
    decision = _web_relevance_decision(lead, "Culture", "Raymond Williams", [])
    assert decision["decision"] == "reject"
    assert decision["reject_reason"] == "generic_exact_title_author_mismatch"


def test_exact_title_and_known_author_is_accepted() -> None:
    lead = SearchLead(
        "https://example.com/culture", "Culture — Raymond Williams",
        "Raymond Williams, 1981", 1,
    )
    assert _web_relevance_decision(
        lead, "Culture", "Raymond Williams", [],
    )["decision"] == "accept"


def test_catalog_identifier_can_override_missing_author_in_search_snippet() -> None:
    candidate = {"isbn": "9780198760566"}
    lead = SearchLead(
        "https://ndlsearch.ndl.go.jp/books/9780198760566",
        "マルクス主義と文学", "ISBN 9780198760566", 1,
    )
    assert _web_relevance_decision(
        lead, "マルクス主義と文学", "ウィリアムズ", [candidate],
    )["decision"] == "accept"


def test_full_contribution_title_and_pages_are_rendered() -> None:
    mention = "架空資料の比較分析"
    full = mention + "——『星の記録』における行為と竜"
    url = "https://researchmap.jp/Koji_Shirae"

    def search(_query: str) -> list[str]:
        return [SearchLead(url, "山田 花子 | researchmap", full, 1)]

    def fetch(page_url: str) -> dict:
        document = (
            '<ul class="rm-cv-list-group">'
            '<li class="list-group-item rm-cv-disclosed">'
            '<a class="rm-cv-list-title" href="/Koji_Shirae/misc/47521633">別論文</a>'
            '<div class="rm-cv-list-author">山田 花子</div>'
            '<div>現代思想&nbsp;115-123&nbsp;2021年</div></li>'
            '<li class="list-group-item rm-cv-disclosed">'
            f'<a class="rm-cv-list-title" href="/Koji_Shirae/misc/47521953">{full}</a>'
            '<div class="rm-cv-list-author">山田 花子</div>'
            '<div>架空論集&nbsp;131-176&nbsp;2022年</div></li>'
            '</ul>'
        )
        return {
            "url": page_url, "status": "retrieved", "title": "山田 花子 | researchmap",
            "metadata_excerpt": document,
            "structured_metadata": {},
            "contribution_listing": True,
            "contribution_items": parse_researcher_contribution_items(document, page_url),
        }

    result = enrich_from_web_v29(
        {"title": mention, "candidate_matches": []}, search=search, fetch=fetch,
    )
    candidate = result["candidate_matches"][0]
    assert candidate["title"] == full
    assert candidate["mention_title"] == mention
    assert candidate["container_title"] == "架空論集"
    assert candidate["pages"] == "131-176"
    assert result["canonical_full_title_applied"] == 1
    candidate["parse_status"] = "parsed"
    rendered = JapaneseReferenceService.render_entry(
        canonicalize_reference(candidate), workflow_version=29,
    )
    assert full in rendered
    assert "131-176頁" in rendered
    prior = {**candidate, "candidate_id": "prior", "title": mention, "mention_title": None}
    final = finalize_reference_v29({
        "reference_id": "r35", "title": mention, "author": "自著",
        "parse_status": "parsed", "candidate_matches": [candidate, prior],
        "provenance": {
            "title": {"kind": "source_explicit", "label": "原稿に明記"},
            "author": {"kind": "source_explicit", "label": "原稿に明記"},
        },
    })
    assert final["reference_output_mode_v29"] == "SINGLE"
    assert final["title"] == full
    assert final["source_title_mention_audit"]["reason"] == (
        "fetched_evidence_expands_source_mention_to_full_title"
    )


def test_nonstandard_unknown_pages_still_render_without_placeholder() -> None:
    entry = {
        "title": "寄稿", "record_type": "nonstandard_contribution",
        "publication_language": "ja", "authors": ["山田花子"],
        "container_title": "同人誌", "year": "2022", "pages": None,
        "pages_status": "unknown_allowed", "pages_required": False,
        "url": "https://example.test/work",
        "parse_status": "parsed",
    }
    rendered = JapaneseReferenceService.render_entry(
        canonicalize_reference(entry), workflow_version=29,
    )
    assert "頁未確認" not in rendered
    assert "https://example.test/work" in rendered


def test_standard_article_unknown_pages_remains_incomplete() -> None:
    entry = {
        "title": "論文", "record_type": "article", "publication_language": "ja",
        "authors": ["山田花子"], "container_title": "学術誌", "year": "2022",
    }
    assert "pages" in JapaneseReferenceService.required_missing_fields(entry)
