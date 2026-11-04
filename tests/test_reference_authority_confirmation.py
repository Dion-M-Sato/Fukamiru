from fukamiru.revisions.reference_canonical import (
    canonicalize_reference,
    finalize_reference_v29,
)
from fukamiru.revisions.reference_web import SearchLead, enrich_from_web_v29
from fukamiru.revisions.references import JapaneseReferenceService


def _ready(candidate_id: str, title: str, language: str = "en") -> dict:
    return {
        "candidate_id": candidate_id,
        "title": title,
        "record_type": "book",
        "publication_language": language,
        "publisher": "Press",
        "year": "2020",
        "authors": ["Jane Doe"],
        "url": f"https://catalog.example/{candidate_id}",
        "bibliography_people": [{
            "role": "author", "name_profile": "western_latin",
            "family": "Doe", "given": "Jane",
            "responsibility_role_confirmed": True,
            "person_display_name_confirmed": True,
            "person_authority_confirmed": False,
        }],
    }


def test_relation_confirmed_one_ready_manifestation_is_single() -> None:
    original = _ready("original", "Original Work")
    translation = {
        **_ready("translation", "邦訳", "ja"),
        "original_title": "Original Work",
        "original_title_relation_verified": True,
        "original_title_audit": [{
            "original_title": "Original Work",
            "source_url": "https://catalog.example/translation",
            "parser_rule": "ndl_original_title_section",
        }],
        "url": "https://catalog.example/translation",
    }
    # The translation is deliberately incomplete; only the linked original is ready.
    translation["publisher"] = None
    result = finalize_reference_v29({
        "reference_id": "r", "title": "邦訳", "candidate_matches": [original, translation],
    })
    assert result["work_relation_status"] == "WORK_RELATION_CONFIRMED"
    assert result["reference_output_mode_v29"] == "SINGLE"


def test_relation_confirmed_multiple_ready_manifestations_are_multiple() -> None:
    originals = [_ready("o1", "Original Work"), _ready("o2", "Original Work")]
    originals[1]["year"] = "2021"
    translation = {
        **_ready("t", "邦訳", "ja"),
        "original_title": "Original Work", "original_title_relation_verified": True,
        "original_title_audit": [{
            "original_title": "Original Work", "source_url": "https://catalog.example/t",
            "parser_rule": "ndl_original_title_section",
        }],
        "url": "https://catalog.example/t",
    }
    result = finalize_reference_v29({
        "reference_id": "r", "title": "邦訳・別表記",
        "candidate_matches": [*originals, translation],
    })
    assert result["reference_output_mode_v29"] == "MULTIPLE"
    assert result["manifestation_resolution_status"] == "MANIFESTATION_MULTIPLE"
    assert result["relation_confirmed_multiple_count"] == 1


def test_confirmed_relation_does_not_expand_direct_japanese_mention() -> None:
    original = _ready("original", "Original Work")
    translation = {
        **_ready("translation", "邦訳 : 副題", "ja"),
        "original_title": "Original Work",
        "original_title_relation_verified": True,
        "original_title_audit": [{
            "original_title": "Original Work",
            "source_url": "https://catalog.example/translation",
            "parser_rule": "ndl_original_title_section",
        }],
    }
    result = finalize_reference_v29({
        "reference_id": "r", "title": "邦訳",
        "candidate_matches": [original, translation],
    })
    assert result["work_relation_status"] == "WORK_RELATION_CONFIRMED"
    assert result["reference_output_mode_v29"] == "SINGLE"


def test_title_match_only_does_not_confirm_relation() -> None:
    result = finalize_reference_v29({
        "reference_id": "r", "title": "邦訳",
        "candidate_matches": [_ready("o", "Original Work")],
    })
    assert result["work_relation_status"] == "WORK_RELATION_UNCONFIRMED"
    assert result["reference_output_mode_v29"] == "REVIEW"


def test_targeted_translator_recovery_uses_role_evidence() -> None:
    candidate = {
        **_ready("jp", "性の歴史Ⅰ", "ja"), "isbn": "9784105067045",
        "is_translation": True, "translators": [],
    }
    url = "https://publisher.example/book"
    result = enrich_from_web_v29(
        {"title": "性の歴史Ⅰ", "author": "フーコー", "candidate_matches": [candidate]},
        search=lambda query: [SearchLead(url, "性の歴史Ⅰ", "渡辺守章 訳", 1)],
        fetch=lambda page_url: {
            "url": page_url, "status": "retrieved", "title": "性の歴史Ⅰ",
            "metadata_excerpt": "性の歴史Ⅰ 訳：渡辺守章 1986年",
            "structured_metadata": {"title": "性の歴史Ⅰ"},
        },
    )
    assert result["targeted_fields_requested"] == ["translators"]
    assert result["targeted_fields_recovered"] == ["translators"]
    assert result["candidate_matches"][0]["translators"] == ["渡辺守章"]
    assert result["candidate_matches"][0]["responsibility_sources"][-1][
        "responsibility_raw"
    ] == ["渡辺守章 訳"]


def test_targeted_translator_does_not_propagate_to_unrelated_candidate() -> None:
    first = {**_ready("a", "A", "ja"), "is_translation": True, "translators": []}
    second = {**_ready("b", "B", "ja"), "is_translation": True, "translators": []}
    enrich_from_web_v29(
        {"title": "A", "candidate_matches": [first, second]},
        search=lambda _query: [], fetch=lambda _url: {},
    )
    assert second["translators"] == []


def test_nonstandard_contribution_pages_null_is_ready() -> None:
    result = canonicalize_reference({
        "title": "寄稿", "record_type": "nonstandard_contribution",
        "publication_language": "ja", "authors": ["山田花子"],
        "container_title": "同人誌", "year": "2022", "url": "https://example.test/item",
        "responsibility_sources": [{
            "provider": "web", "url": "https://example.test/item",
            "responsibility_raw": ["山田花子 著"],
            "responsibility_source": "web_explicit_author_metadata",
        }],
    })
    assert result["v29_render_status"] == "render_ready"
    assert result["pages_status"] == "unknown_allowed"
    assert result["pages_required"] is False
    assert JapaneseReferenceService.required_missing_fields(result) == []
    assert "要ユーザー指定" not in JapaneseReferenceService.render_entry(
        result, workflow_version=29
    )


def test_standard_article_pages_null_remains_incomplete() -> None:
    result = canonicalize_reference({
        "title": "論文", "record_type": "article", "publication_language": "ja",
        "authors": ["山田花子"], "container_title": "学術誌", "year": "2022",
        "responsibility_sources": [{
            "provider": "catalog", "url": "https://example.test/article",
            "responsibility_raw": ["山田花子 著"],
            "responsibility_source": "exact_record_detail",
        }],
    })
    assert result["v29_render_status"] == "human_review_required"
    assert "pages" in result["v29_required_missing_fields"]
    assert result["pages_exception_rejected_for_standard_record_count"] == 1


def test_social_snippet_is_hypothesis_only_and_not_ready() -> None:
    title = "ある寄稿"
    result = enrich_from_web_v29(
        {"title": title, "candidate_matches": []},
        search=lambda _query: [SearchLead(
            "https://x.com/example/status/1", title, f"{title}『同人誌』(2022)", 1,
        )],
        fetch=lambda _url: {},
    )
    assert result["container_hypotheses"][0]["source_kind"] == "search_hypothesis_only"
    assert not result.get("candidate_matches")


def test_container_and_author_pages_do_not_form_one_work_record() -> None:
    title = "ある寄稿"
    author_page = "https://research.example/person"
    container_page = "https://shop.example/item"

    def search(query: str) -> list[str]:
        if "同人誌" in query:
            return [SearchLead(container_page, "同人誌", title, 1)]
        return [
            SearchLead(author_page, "山田 花子 (Hanako Yamada)", title, 1),
            SearchLead(container_page, "同人誌", f"{title}『同人誌』(2022)", 2),
        ]

    def fetch(url: str) -> dict:
        excerpt = (
            f"{title}。 山田 花子。 同人誌 10-20 2022"
            if url == author_page else f"目次 {title} 山田花子 著 同人誌 2022"
        )
        return {"url": url, "status": "retrieved", "title": (
            "山田 花子 (Hanako Yamada)" if url == author_page else "同人誌"
        ), "metadata_excerpt": excerpt, "structured_metadata": {}}

    result = enrich_from_web_v29(
        {"title": title, "candidate_matches": []}, search=search, fetch=fetch,
    )
    assert not result.get("candidate_matches")
    assert result["nonstandard_candidate_count"] == 0


def test_exact_title_body_without_author_or_container_is_not_ready() -> None:
    title = "ある寄稿"
    result = enrich_from_web_v29(
        {"title": title, "candidate_matches": []},
        search=lambda _query: [SearchLead("https://example.test/list", "目次", title, 1)],
        fetch=lambda url: {"url": url, "status": "retrieved", "title": "目次",
                           "metadata_excerpt": title, "structured_metadata": {}},
    )
    assert not result.get("candidate_matches")


def test_web_and_doujin_contribution_aliases_allow_unknown_pages() -> None:
    for kind in ("web_contribution", "doujin_contribution"):
        result = canonicalize_reference({
            "title": "寄稿", "record_type": kind, "publication_language": "ja",
            "container_title": "媒体", "year": "2022", "url": "https://example.test",
            "responsibility_sources": [{
                "provider": "web", "url": "https://example.test",
                "responsibility_raw": ["山田花子 著"],
                "responsibility_source": "web_explicit_author_metadata",
            }],
        })
        assert result["v29_render_status"] == "render_ready"
        assert result["pages_status"] == "unknown_allowed"
