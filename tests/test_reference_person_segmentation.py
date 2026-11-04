import pytest

from fukamiru.revisions.reference_canonical import canonicalize_reference


def _record(*raw: str) -> dict:
    return canonicalize_reference({
        "title": "想像の共同体",
        "record_type": "book",
        "publication_language": "ja",
        "provider": "ndl_search",
        "url": "https://ndlsearch.ndl.go.jp/books/example",
        "responsibility_sources": [{
            "provider": "ndl_search",
            "url": "https://ndlsearch.ndl.go.jp/books/example",
            "responsibility_source": "exact_record_detail",
            "responsibility_raw": list(raw),
        }],
    })


def test_two_japanese_translators_are_separate_people() -> None:
    item = _record("白石隆, 白石さや 訳")
    assert item["translators"] == ["白石隆", "白石さや"]
    assert [(p["family"], p["given"]) for p in item["bibliography_people"]] == [
        ("白石", "隆"), ("白石", "さや"),
    ]


def test_reversed_translator_list_stays_separate() -> None:
    item = _record("白石さや, 白石隆 訳")
    assert item["translators"] == ["白石さや", "白石隆"]


def test_leading_separator_does_not_merge_two_supervising_translators() -> None:
    item = _record("; 岡野八代, 牟田和恵 監訳")
    assert item["translators"] == ["岡野八代", "牟田和恵"]
    assert all(
        person.get("role_subtype") == "supervising_translator"
        for person in item["bibliography_people"]
    )


def test_supervising_translator_does_not_keep_role_prefix_in_name() -> None:
    item = _record("藤木秀朗 監訳", "大﨑晴美 訳")
    assert item["translators"] == ["藤木秀朗", "大﨑晴美"]
    assert item["bibliography_people"][0]["role_subtype"] == "supervising_translator"
    assert item["bibliography_people"][0]["family"] == "藤木"


def test_katakana_middle_dot_and_authority_comma_are_not_list_separators() -> None:
    item = _record("ピエール・ブルデュー 著", "石井, 洋二郎 訳")
    assert item["authors"] == ["ブルデュー、ピエール"]
    assert item["translators"] == ["石井洋二郎"]


@pytest.mark.parametrize(
    "title,author_raw,translator_raw",
    [
        ("男性支配", "ブルデュー [著]", "坂本さやか, 坂本浩也 訳"),
        ("男同士の絆", "イヴ・K.セジウィック 著", "上原早苗, 亀澤美由紀 訳"),
        ("日常的実践のポイエティーク", "ミシェル・ド・セルトー 著", "山田登世子 訳"),
        ("オリエンタリズム", "エドワード・W.サイード 著", "今沢紀子 訳"),
        ("アート・ワールド", "ハワード・S・ベッカー 著", "後藤将之 訳"),
    ],
)
def test_explicit_detail_display_name_is_not_blocked_by_missing_authority(
    title: str, author_raw: str, translator_raw: str,
) -> None:
    item = canonicalize_reference({
        "title": title, "record_type": "book", "publication_language": "ja",
        "publisher": "藤原書店", "year": "2017", "provider": "ndl_search",
        "url": "https://ndlsearch.ndl.go.jp/books/example",
        "responsibility_sources": [{
            "provider": "ndl_search", "url": "https://ndlsearch.ndl.go.jp/books/example",
            "responsibility_source": "exact_record_detail",
            "responsibility_raw": [author_raw, translator_raw],
        }],
    })
    assert item["responsibility_role_confirmed"] is True
    assert item["person_display_name_confirmed"] is True
    assert item["person_authority_confirmed"] is False
    assert item["v29_render_status"] == "render_ready"
    assert "person_name_unconfirmed" not in item["v29_review_reasons"]
    assert len(item["authors"]) == 1
    assert len(item["translators"]) == (2 if "," in translator_raw else 1)


def test_unlabelled_creator_list_does_not_gain_confirmed_roles() -> None:
    item = canonicalize_reference({
        "title": "男性支配", "record_type": "book", "publication_language": "ja",
        "publisher": "藤原書店", "year": "2017", "provider": "ndl_search",
        "url": "https://ndlsearch.ndl.go.jp/books/example",
        "responsibility_sources": [{
            "provider": "ndl_search", "url": "https://ndlsearch.ndl.go.jp/books/example",
            "responsibility_source": "ndl_search:dc_creator_mixed",
            "responsibility_raw": ["ブルデュー", "坂本さやか", "坂本浩也"],
        }],
    })
    assert item["responsibility_role_confirmed"] is False
    assert item["v29_render_status"] != "render_ready"


@pytest.mark.parametrize(
    "raw,given,family",
    [
        ("G.W.F.ヘーゲル [著]", "G.W.F.", "ヘーゲル"),
        ("G. W. F. ヘーゲル [著]", "G.W.F.", "ヘーゲル"),
        ("J.-P.サルトル [著]", "J.-P.", "サルトル"),
        ("W.G.ゼーバルト [著]", "W.G.", "ゼーバルト"),
        ("T.S.エリオット [著]", "T.S.", "エリオット"),
    ],
)
def test_hybrid_initials_katakana(raw: str, given: str, family: str) -> None:
    item = _record(raw, "牧野紀之 訳")
    author = item["bibliography_people"][0]
    assert author["name_profile"] == "western_initials_katakana"
    assert (author["given"], author["family"]) == (given, family)
    assert item["authors"] == [given + family]
    assert item["translators"] == ["牧野紀之"]
