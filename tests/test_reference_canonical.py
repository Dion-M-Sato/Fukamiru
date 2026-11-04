import pytest

from fukamiru.revisions.bibliography_style import format_reference
from fukamiru.revisions.reference_canonical import (
    canonicalize_reference,
    cluster_manifestations_v29,
    materialize_canonical_manifestation_v29,
)
from fukamiru.revisions.references import JapaneseReferenceService


def test_v29_canonicalization_accepts_only_explicitly_supported_record() -> None:
    source = {
        "reference_id": "r1",
        "title": "A Book",
        "record_type": "book",
        "publication_language": "en",
        "publisher": "Example Press",
        "year": "2020",
        "authors": ["Doe, Jane"],
        "responsibility_raw": ["Doe, Jane 著"],
        "provider": "example",
        "url": "https://example.org/book",
    }
    result = canonicalize_reference(source)
    assert result["v29_render_status"] == "render_ready"
    assert result["v29_render_supported"] is True
    assert result["bibliography_people"][0]["role"] == "author"
    assert result["field_evidence"]["publisher"][0]["provider"] == "example"
    assert source.get("bibliography_people") is None
    assert format_reference(result).supported


def test_v29_canonicalization_does_not_guess_ndl_mixed_responsibility() -> None:
    result = canonicalize_reference(
        {
            "reference_id": "r2",
            "title": "ある本",
            "record_type": "図書",
            "publication_language": None,
            "provider": "ndl_search",
            "authors": ["Doe, Jane", "山田, 花子"],
            "responsibility_raw": ["Doe, Jane", "山田, 花子"],
        }
    )
    assert result["bibliography_people"] == []
    assert result["v29_render_status"] == "human_review_required"
    assert result["final_revision_blocked"] is True
    assert "edition_language_unconfirmed" in result["v29_review_reasons"]
    assert "ndl_responsibility_unconfirmed" in result["v29_review_reasons"]
    assert result["v29_review_packet"]["reference_id"] == "r2"


def test_v29_canonicalization_preserves_uncertain_manifestation() -> None:
    source = {
        "title": "A Book",
        "record_type": "book",
        "publication_language": "en",
        "responsibility_raw": ["Doe, Jane 著"],
        "publisher": "Example Press",
        "year": "2020",
        "resolution_status": "manifestation_retained_unconfirmed",
        "consulted_status": "unconfirmed",
        "final_revision_blocked": True,
    }
    result = canonicalize_reference(source)
    assert result["v29_render_supported"] is True
    assert result["resolution_status"] == "manifestation_retained_unconfirmed"
    assert result["consulted_status"] == "unconfirmed"
    assert result["final_revision_blocked"] is True


@pytest.mark.parametrize(
    ("creator", "translator", "expected_author", "expected_translator"),
    [
        (
            "Bourdieu, Pierre, 1930-2002 著",
            "石井, 洋二郎, 1951- 訳",
            "Pierre Bourdieu",
            "石井洋二郎",
        ),
        ("Butler, Judith 著", "竹村, 和子 訳", "Judith Butler", "竹村和子"),
        ("Foucault, Michel 著", "中村, 雄二郎 訳", "Michel Foucault", "中村雄二郎"),
    ],
)
def test_ndl_explicit_roles_are_separated(
    creator: str, translator: str, expected_author: str, expected_translator: str
) -> None:
    result = canonicalize_reference(
        {
            "title": "検証対象",
            "record_type": "図書",
            "publication_language": "jpn",
            "publisher": "実在の出版社",
            "year": "2020",
            "provider": "ndl_search",
            "url": "https://ndlsearch.ndl.go.jp/books/example",
            "authors": [creator, translator],
            "responsibility_raw": [creator, translator],
        }
    )
    assert result["authors"] == [expected_author]
    assert result["translators"] == [expected_translator]
    assert [person["role"] for person in result["bibliography_people"]] == ["author", "translator"]
    assert result["publication_language"] == "ja"
    assert result["record_type"] == "book"
    assert result["is_translation"] is True
    assert result["v29_render_supported"] is True


def test_ndl_mixed_names_without_roles_are_not_guessed() -> None:
    result = canonicalize_reference(
        {
            "title": "検証対象",
            "record_type": "図書",
            "publication_language": "jpn",
            "provider": "ndl_search",
            "authors": ["LaMarre, Thomas", "藤木秀朗", "大崎晴美"],
            "responsibility_raw": ["LaMarre, Thomas", "藤木秀朗", "大崎晴美"],
        }
    )
    assert result["authors"] == []
    assert result["translators"] in (None, [])
    assert result["v29_render_supported"] is False
    assert all(item["role"] == "unknown" for item in result["responsibility_candidates"])


def test_provider_noise_is_normalized_without_inventing_metadata() -> None:
    result = canonicalize_reference(
        {
            "title": "A Book",
            "record_type": "図書",
            "publication_language": "eng",
            "publication_place": "JP",
            "volume": ": pbk",
            "provider": "ndl_search",
            "responsibility_raw": ["Derrida, Jacques, 1930-2004 著"],
        }
    )
    assert result["publication_place"] is None
    assert result["publication_country_code"] == "JP"
    assert result["volume"] is None
    assert result["binding"] == "paperback"
    assert result["bibliography_people"][0]["family"] == "Derrida"
    assert result["bibliography_people"][0]["given"] == "Jacques"
    assert result["bibliography_people"][0]["authority_dates"] == "1930-2004"
    assert result["v29_render_supported"] is True


def test_self_authored_path_does_not_claim_external_publication() -> None:
    result = canonicalize_reference(
        {
            "title": "架空資料の比較分析",
            "author": "自著",
            "record_type": "book",
            "parse_status": "parsed",
        }
    )
    assert result["is_self_authored"] is True
    assert result["self_publication_review"]["venue_or_publisher"] is None
    assert result["v29_render_status"] == "human_review_required"


def test_selected_candidate_materializes_fields_with_provenance() -> None:
    source = {
        "reference_id": "r1",
        "title": "A Book",
        "record_type": "book_or_journal",
        "resolution_status": "resolved",
        "selected_candidate_id": "manifestation:1",
    }
    candidate = {
        "candidate_id": "manifestation:1",
        "title": "A Book",
        "record_type": "図書",
        "publication_language": "eng",
        "publisher": "Example Press",
        "year": "2020",
        "authors": ["Doe, Jane"],
        "provider": "crossref",
        "provider_record_id": "x1",
        "url": "https://example.org/book",
    }
    result = materialize_canonical_manifestation_v29(source, candidate)
    assert result["record_type"] == "book"
    assert result["publisher"] == "Example Press"
    assert result["publication_language"] == "en"
    assert result["canonical_manifestation_id"] == "manifestation:1"
    assert result["provenance"]["publisher"]["source"] == "crossref"
    assert result["v29_render_supported"] is True
    assert source.get("publisher") is None


def test_confirmed_translator_clears_stale_translation_review_requirement() -> None:
    result = canonicalize_reference(
        {
            "title": "検証対象",
            "record_type": "book",
            "publication_language": "ja",
            "publisher": "出版社",
            "year": "2020",
            "provider": "ndl_search",
            "responsibility_raw": ["Butler, Judith 著", "竹村, 和子 訳"],
            "required_missing_fields": ["translation_status_confirmation", "translators"],
        }
    )
    assert result["is_translation"] is True
    assert result["v29_required_missing_fields"] == []
    assert result["v29_render_status"] == "render_ready"


def test_undetermined_original_relation_alone_does_not_remove_ready_book() -> None:
    base = {
        "title": "日本語オリジナル論集", "record_type": "book",
        "publication_language": "ja", "publisher": "小規模出版社", "year": "2020",
        "bibliography_people": [{
            "role": "author", "name_profile": "japanese_native",
            "family": "東", "given": "浩紀",
        }],
        "required_missing_fields": ["translation_status_confirmation"],
    }
    result = canonicalize_reference(base)
    assert result["translation_status"] == "undetermined"
    assert result["is_translation"] is None
    assert result["v29_render_status"] == "render_ready"
    translated = canonicalize_reference({
        **base, "is_translation": True,
        "required_missing_fields": ["translation_status_confirmation"],
    })
    assert "translators" in translated["v29_required_missing_fields"]
    assert translated["v29_render_status"] == "human_review_required"


def test_ndl_bracketed_author_and_authority_translator_are_materialized() -> None:
    result = canonicalize_reference(
        {
            "title": "明るい部屋",
            "record_type": "book",
            "publisher": "みすず書房",
            "year": "1997",
            "provider": "ndl_search",
            "url": "https://ndlsearch.ndl.go.jp/books/example",
            "responsibility_raw": ["ロラン・バルト[著]", "花輪, 光訳"],
        }
    )
    assert result["authors"] == ["バルト、ロラン"]
    assert result["translators"] == ["花輪光"]
    assert result["publication_language"] == "ja"
    assert result["language_status"] == "corroborated"
    assert result["v29_render_status"] == "render_ready"


def test_parenthesized_katakana_reading_keeps_work_identity() -> None:
    assert JapaneseReferenceService._title_score(
        "正義の秤", "正義の秤(スケール) : グローバル化する世界"
    ) == 1.0


def test_country_code_and_multiple_urls_are_not_metadata_conflicts() -> None:
    result = canonicalize_reference(
        {
            "title": "A Book",
            "record_type": "book",
            "publication_place": "JP",
            "metadata_conflicts": {
                "publication_place": ["JP", "ja"],
                "url": ["https://example.org/a", "https://example.org/b"],
            },
        }
    )
    assert result["publication_place"] is None
    assert result["publication_country_code"] == "JP"
    assert result["publication_language"] == "ja"
    assert result["source_urls"] == ["https://example.org/a", "https://example.org/b"]
    assert result["metadata_conflicts"] == {}


@pytest.mark.parametrize(
    "title,left_year,right_year,left_isbn,right_isbn",
    [
        ("愛の労働あるいは依存とケアの正義論", "2010", "2023", "9784768479353", "9784768479964"),
        ("Visual and Other Pleasures", "1989", "2009", "9780333445297", "9780230240612"),
        ("精神現象学", "1998", "2001", "9784588000001", "9784588000002"),
        ("The Great Good Place", "1989", "2023", "9781557781109", "9780062976123"),
    ],
)
def test_distinct_isbn_editions_are_separate_manifestations_not_conflicts(
    title: str, left_year: str, right_year: str, left_isbn: str, right_isbn: str,
) -> None:
    candidates = [
        {"candidate_id": "a", "title": title, "year": left_year,
         "isbn": left_isbn, "publisher": "Publisher A", "metadata_conflicts": {},
         "v29_render_status": "render_ready"},
        {"candidate_id": "b", "title": title, "year": right_year,
         "isbn": right_isbn, "publisher": "Publisher B", "metadata_conflicts": {},
         "v29_render_status": "render_ready"},
    ]
    clusters = cluster_manifestations_v29(candidates)
    assert len(clusters) == 2
    assert all(cluster["v29_render_ready"] for cluster in clusters)
    assert all(not member["metadata_conflicts"] for cluster in clusters
               for member in cluster["members"])


@pytest.mark.parametrize("statement", [
    "白澤社 (出版) 現代書館 (発売)",
    "白澤社 (出版) 東京 : 現代書館 (頒布)",
])
def test_same_manifestation_publisher_distribution_statement_is_not_conflict(
    statement: str,
) -> None:
    records = [
        {"title": "愛の労働あるいは依存とケアの正義論", "year": "2023",
         "isbn": "9784768479964", "publisher": "白澤社", "provider": "ndl_search"},
        {"title": "愛の労働あるいは依存とケアの正義論", "year": "2023",
         "isbn": "9784768479964", "publisher": statement,
         "provider": "catalog"},
    ]
    merged = JapaneseReferenceService._merge_manifestation_group(records)
    assert merged["metadata_conflicts"] == {}
    assert len(merged["source_records"]) == 2


def test_french_publisher_brand_variant_is_not_true_conflict() -> None:
    records = [
        {"title": "Le partage du sensible", "year": "2000", "isbn": "9782913372016",
         "publisher": "La Fabrique", "provider": "catalog_a"},
        {"title": "Le partage du sensible", "year": "2000", "isbn": "9782913372016",
         "publisher": "Fabrique éditions", "provider": "catalog_b"},
    ]
    merged = JapaneseReferenceService._merge_manifestation_group(records)
    assert merged["metadata_conflicts"] == {}


def test_country_suffix_on_same_publisher_is_not_true_conflict() -> None:
    records = [
        {"title": "Phenomenology of Spirit", "year": "1998", "isbn": "9788120800001",
         "publisher": "Motilal Banarsidass,India", "provider": "catalog_a"},
        {"title": "Phenomenology of Spirit", "year": "1998", "isbn": "9788120800001",
         "publisher": "Motilal Banarsidass", "provider": "catalog_b"},
    ]
    merged = JapaneseReferenceService._merge_manifestation_group(records)
    assert merged["metadata_conflicts"] == {}


def test_true_conflict_within_one_manifestation_remains_blocking() -> None:
    records = [
        {"title": "A Book", "year": "2020", "isbn": "9781234567890",
         "publisher": "Publisher A", "provider": "catalog_a"},
        {"title": "A Book", "year": "2020", "isbn": "9781234567890",
         "publisher": "Publisher B", "provider": "catalog_b"},
    ]
    merged = JapaneseReferenceService._merge_manifestation_group(records)
    assert merged["metadata_conflicts"]["publisher"] == ["Publisher A", "Publisher B"]


def test_nested_provider_metadata_is_not_treated_as_a_scalar_reference_field() -> None:
    records = [
        {"title": "A Book", "year": "2020", "publisher": {"name": "Catalog only"},
         "provider": "catalog_a"},
        {"title": "A Book", "year": "2020", "publisher": "Publisher B",
         "provider": "catalog_b"},
    ]
    merged = JapaneseReferenceService._merge_manifestation_group(records)
    assert merged["publisher"] == "Publisher B"
    assert merged["metadata_conflicts"] == {}
    assert JapaneseReferenceService._first({"publisher": {"name": "Catalog only"}},
                                           "publisher") is None
