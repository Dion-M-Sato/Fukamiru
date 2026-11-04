import pytest

from fukamiru.revisions.reference_canonical import canonicalize_reference
from fukamiru.revisions.reference_web import (
    _enrich_candidates_from_pages,
    enrich_from_web_v29,
    general_web_search,
    parse_ndl_detail_metadata,
    parse_page_metadata,
    parse_researcher_contribution_items,
)


def test_json_ld_extracts_explicit_book_metadata_not_site_language() -> None:
    document = """
    <html lang="ja"><script type="application/ld+json">
    {"@type":"Book","name":"Justice Scales","isbn":"9780000000001",
     "author":{"name":"Fraser, Nancy"},"translator":{"name":"山田, 花子"},
     "publisher":{"name":"Example Press"},"datePublished":"2020-05-01",
     "inLanguage":"en","translationOfWork":{"name":"Original Work"}}
    </script></html>
    """
    fields = parse_page_metadata(document)
    assert fields["publication_language"] == "en"
    assert fields["page_language"] == "ja"
    assert fields["authors"] == ["Fraser, Nancy"]
    assert fields["translators"] == ["山田, 花子"]
    assert fields["year"] == "2020"
    assert fields["original_title"] == "Original Work"
    assert fields["original_title_relation_verified"] is True


def test_ndl_detail_extracts_publication_and_roles_from_labelled_record() -> None:
    document = """
    <main>ISBN 978-4-588-00931-0 タイトル 感性的なもののパルタージュ : 美学と政治
    タイトルよみ カンセイ 著者・編者 ジャック・ランシエール [著] 梶田裕 訳
    シリーズタイトル 叢書 出版事項 東京 : 法政大学出版局
    出版年月日等 2009.12 本文の言語コード jpn</main>
    """
    fields = parse_ndl_detail_metadata(document)
    assert fields["isbn"] == "978-4-588-00931-0"
    assert fields["publisher"] == "法政大学出版局"
    assert fields["year"] == "2009"
    assert fields["authors"] == ["ジャック・ランシエール"]
    assert fields["translators"] == ["梶田裕"]


def test_ndl_detail_replaces_mixed_rss_creator_without_false_conflict() -> None:
    url = "https://ndlsearch.ndl.go.jp/books/R100000002-I1"
    candidates = [{
        "url": url, "provider": "ndl_search",
        "responsibility_source": "ndl_search:dc_creator_mixed",
        "authors": ["Author, Alice", "Translator, Bob"],
        "publication_language": "eng", "publication_place": "FR",
        "publisher": "名古屋大学出版会",
    }]
    _enrich_candidates_from_pages(candidates, [{
        "url": url, "status": "retrieved",
        "structured_metadata": {
            "authors": ["Alice Author"], "translators": ["Bob Translator"],
            "responsibility_raw": ["Alice Author 著", "Bob Translator 訳"],
            "publication_language": "en", "publication_place": "Paris",
            "publisher": "名古屋大学出版会 (出版)",
        },
    }])
    assert candidates[0]["authors"] == ["Alice Author"]
    assert candidates[0]["translators"] == ["Bob Translator"]
    assert candidates[0]["publication_place"] == "Paris"
    assert candidates[0]["metadata_conflicts"] == {}
    assert candidates[0]["responsibility_sources"][-1]["responsibility_source"] == (
        "exact_record_detail"
    )


def test_same_title_publisher_different_year_does_not_enrich_another_edition() -> None:
    candidates = [{
        "url": "https://ndlsearch.ndl.go.jp/books/R1",
        "title": "知の考古学", "publisher": "河出書房新社", "year": "1995",
    }]
    _enrich_candidates_from_pages(candidates, [{
        "url": "https://ndlsearch.ndl.go.jp/books/R2", "status": "retrieved",
        "structured_metadata": {
            "title": "知の考古学", "publisher": "河出書房新社", "year": "1987",
            "translators": ["訳者甲"],
        },
    }])
    assert candidates[0]["year"] == "1995"
    assert "translators" not in candidates[0]
    assert candidates[0]["metadata_conflicts"] == {}


@pytest.mark.parametrize("statement", [
    "白澤社 (出版) 現代書館 (発売)",
    "白澤社 (出版) 東京 : 現代書館 (頒布)",
])
def test_ndl_primary_publisher_and_labelled_distributor_are_not_conflict(
    statement: str,
) -> None:
    from fukamiru.revisions.reference_web import _enrich_candidates_from_pages

    candidate = {
        "title": "愛の労働あるいは依存とケアの正義論",
        "publisher": "白澤社", "year": "2023", "isbn": "978-4-7684-7996-4",
        "url": "https://ndlsearch.ndl.go.jp/books/example",
    }
    _enrich_candidates_from_pages([candidate], [{
        "status": "retrieved", "url": candidate["url"],
        "structured_metadata": {
            "publisher": statement,
            "year": "2023", "isbn": "978-4-7684-7996-4",
        },
    }])
    assert candidate["metadata_conflicts"] == {}
    assert candidate["field_evidence"]["publisher"][0]["value"] == statement


def test_french_publisher_brand_variant_on_exact_edition_is_not_conflict() -> None:
    from fukamiru.revisions.reference_web import _enrich_candidates_from_pages

    candidate = {
        "title": "Le partage du sensible", "publisher": "La Fabrique",
        "url": "https://openlibrary.org/books/example", "year": "2000",
    }
    _enrich_candidates_from_pages([candidate], [{
        "status": "retrieved", "url": candidate["url"],
        "structured_metadata": {"publisher": "Fabrique éditions", "year": "2000"},
    }])
    assert candidate["metadata_conflicts"] == {}


def test_catalog_detail_is_not_retyped_as_nonstandard_work() -> None:
    from fukamiru.revisions.reference_web import _inspect_page_evidence

    page = {
        "status": "retrieved", "url": "https://ndlsearch.ndl.go.jp/books/example",
        "title": "感性的なもののパルタージュ : 美学と政治",
        "metadata_excerpt": "感性的なもののパルタージュ : 美学と政治 著者: ランシエール",
        "structured_metadata": {"isbn": "9784588009310", "year": "2009"},
    }
    _inspect_page_evidence(page, "感性的なもののパルタージュ : 美学と政治", "")
    assert page["structured_metadata"].get("record_type") is None
    assert page["page_evidence"]["self_identification_as_work"] is False


def test_same_isbn_article_page_does_not_conflict_with_book_manifestation() -> None:
    from fukamiru.revisions.reference_web import _enrich_candidates_from_pages

    candidate = {
        "candidate_id": "book", "record_type": "図書", "isbn": "9784588009310",
        "title": "感性的なもののパルタージュ : 美学と政治",
        "url": "https://ndlsearch.ndl.go.jp/books/book",
    }
    _enrich_candidates_from_pages([candidate], [{
        "status": "retrieved", "url": "https://publisher.example/articles/review",
        "structured_metadata": {
            "record_type": "article", "isbn": "9784588009310",
            "title": candidate["title"],
        },
    }])
    assert candidate["metadata_conflicts"] == {}
    assert not candidate.get("field_evidence")


def test_relation_mode_searches_explicit_original_title_and_stops() -> None:
    from fukamiru.revisions.reference_web import SearchLead, enrich_from_web_v29

    foreign = {
        "candidate_id": "foreign", "title": "Marxism and Literature",
        "record_type": "book", "publication_language": "en", "year": "1977",
        "publisher": "Oxford University Press", "authors": ["Raymond Williams"],
        "url": "https://catalog.example/original", "provider": "catalog",
        "bibliography_people": [{"role": "author", "name_profile": "western_latin",
                                 "family": "Williams", "given": "Raymond"}],
    }
    queries: list[str] = []
    detail = "https://ndlsearch.ndl.go.jp/books/translation"

    def search(query: str) -> list[str]:
        queries.append(query)
        return [SearchLead(detail, "マルクス主義と文学", "原題あり", 1)]

    def fetch(url: str) -> dict:
        if url == detail:
            return {"url": url, "status": "retrieved", "structured_metadata": {
                "title": "マルクス主義と文学", "original_title": "Marxism and Literature",
                "original_title_relation_verified": True, "record_type": "book",
                "publication_language": "ja", "publisher": "出版社", "year": "1980",
                "authors": ["レイモンド・ウィリアムズ"],
            }}
        return {"url": url, "status": "not_found", "structured_metadata": {}}

    result = enrich_from_web_v29(
        {"title": "マルクス主義と文学", "candidate_matches": [foreign]},
        search=search, fetch=fetch,
    )
    assert queries == ['"マルクス主義と文学"']
    assert result["relation_resolution_mode"] == "translation_side_discovery"
    assert result["relation_query_stopped_on_evidence"] is True
    assert len(result["candidate_matches"]) == 2
    assert result["candidate_matches"][1]["original_title_audit"][0]["source_url"] == detail


def test_ndl_original_title_excludes_following_japanese_note() -> None:
    from fukamiru.revisions.reference_web import parse_ndl_detail_metadata

    fields = parse_ndl_detail_metadata(
        "原タイトル DU MODE D'EXISTENCE DES OBJETS TECHNIQUES "
        "簡訳書での翻訳 書誌注記 本文の言語コード jpn"
    )
    assert fields["original_title"] == "DU MODE D'EXISTENCE DES OBJETS TECHNIQUES"
    assert fields["original_title_relation_verified"] is True


def test_title_first_web_page_can_resolve_identifier_free_publication() -> None:
    from fukamiru.revisions.reference_web import SearchLead, enrich_from_web_v29

    title = "架空資料の比較分析"
    url = "https://creator.example/works/novel-game"
    queries: list[str] = []
    fetched: list[str] = []

    def search(query: str) -> list[str]:
        queries.append(query)
        return [SearchLead(url, title + " | 作者サイト", title, 1)]

    def fetch(page_url: str) -> dict:
        fetched.append(page_url)
        return {
            "url": page_url, "status": "retrieved",
            "title": title + " | 作者サイト",
            "metadata_excerpt": title + " 著者: 山田花子 公開日: 2022",
            "structured_metadata": {
                "publication_language": "ja", "page_site_name": "作者サイト",
            },
        }

    result = enrich_from_web_v29(
        {"title": title, "candidate_matches": []}, search=search, fetch=fetch,
    )
    assert queries == [f'"{title}"']
    assert fetched == [url]
    assert result["title_first_web_search_count"] == 1
    assert result["exact_title_web_hit_count"] == 1
    assert result["web_page_fetch_count"] == 1
    assert result["web_evidence_pages"]
    candidate = result["candidate_matches"][0]
    assert not candidate.get("isbn")
    assert candidate["record_type"] == "nonstandard_publication"
    assert candidate["field_evidence"]["authors"][0]["source_type"] == "web_page_body"
    assert candidate["field_evidence"]["year"][0]["source_url"] == url
    assert canonicalize_reference(candidate)["v29_render_status"] == "render_ready"


def test_exact_title_only_in_container_body_is_lead_not_work_candidate() -> None:
    from fukamiru.revisions.reference_web import SearchLead, enrich_from_web_v29

    title = "架空資料の比較分析"
    url = "https://seller.example/collections/essays"
    result = enrich_from_web_v29(
        {"title": title, "candidate_matches": []},
        search=lambda _query: [SearchLead(url, "評論集", title + " を収録", 1)],
        fetch=lambda page_url: {
            "url": page_url, "status": "retrieved", "title": "評論集",
            "metadata_excerpt": "評論集には " + title + " を収録。著者: 山田花子 発行日: 2022",
            "structured_metadata": {},
        },
    )
    assert result["web_page_fetch_count"] == 1
    assert result["web_evidence_pages"][0]["page_evidence"]["exact_title_in_page_body"]
    assert not result.get("candidate_matches")


def test_creativework_schema_keeps_nonstandard_kind_without_book_isbn() -> None:
    from fukamiru.revisions.reference_web import parse_page_metadata

    fields = parse_page_metadata(
        '<html lang="ja"><script type="application/ld+json">'
        '{"@type":"CreativeWork","name":"ある公開作品",'
        '"author":{"name":"山田花子"},"datePublished":"2022-04-01",'
        '"inLanguage":"ja"}</script></html>'
    )
    assert fields["record_type"] == "nonstandard_publication"
    assert fields["authors"] == ["山田花子"]
    assert fields["year"] == "2022"
    assert not fields.get("isbn")


def test_exact_title_search_uses_second_public_index_when_rss_is_lexical(
    monkeypatch,
) -> None:
    from fukamiru.revisions.reference_web import general_web_search

    class Response:
        def __init__(self, body: bytes) -> None:
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _size: int) -> bytes:
            return self.body

    class Opener:
        def open(self, request, timeout: float):
            assert timeout == 3
            if "yahoo.co.jp" in request.full_url:
                return Response(
                    '<ul><li><a href="https://publisher.example/work">作品情報</a>'
                    '<div>架空資料の比較分析</div></li></ul>'.encode()
                )
            return Response(
                b'<rss><channel><item><link>https://noise.example/novels</link>'
                b'<title>Novel site</title></item></channel></rss>'
            )

    monkeypatch.setattr(
        "fukamiru.revisions.reference_web.urllib.request.build_opener",
        lambda *_args: Opener(),
    )
    hits = general_web_search('"架空資料の比較分析"', timeout=3)
    assert hits == ["https://publisher.example/work"]
    assert "架空資料の比較分析" in hits[0].snippet


def test_self_authored_contribution_uses_researcher_registry_and_parses_container() -> None:
    from fukamiru.revisions.reference_web import SearchLead

    title = "架空資料の比較分析"
    url = "https://jglobal.jst.go.jp/public/202101014504544090"
    queries: list[str] = []

    def search(query: str) -> list[str]:
        queries.append(query)
        if "site:jglobal.jst.go.jp" in query:
            return [SearchLead(
                url,
                "山田 花子 | 研究者情報 | J-GLOBAL",
                f"{title}--『星の記録』における行為と竜",
                1,
            )]
        return []

    def fetch(found_url: str) -> dict:
        assert found_url == url
        document = (
            '<ul class="mdisc"><li>山田 花子. '
            f'{title}--『星の記録』における行為と竜. '
            '架空論集. 2022. 131-176</li>'
            '<li>山田 花子. 侵犯的リアリズム. Rhetorica. 2018. 72-86</li></ul>'
        )
        return {
            "url": url,
            "status": "retrieved",
            "title": "山田 花子 | 研究者情報 | J-GLOBAL",
            "structured_metadata": {},
            "contribution_listing": True,
            "contribution_items": parse_researcher_contribution_items(document, url),
            "metadata_excerpt": document,
        }

    result = enrich_from_web_v29(
        {"reference_id": "self-work", "title": title, "author": "自著",
         "is_self_authored": True},
        search=search,
        fetch=fetch,
    )

    assert any("site:researchmap.jp" in query for query in queries)
    assert not any("自著" in query for query in queries)
    assert result["web_page_fetch_count"] == 1
    assert result["work_identity_status"] == "matched"
    candidate = result["candidate_matches"][0]
    assert candidate["authors"] == ["山田花子"]
    assert candidate["container_title"] == "架空論集"
    assert candidate["pages"] == "131-176"
    assert candidate["year"] == "2022"
    assert candidate["title"].endswith("『星の記録』における行為と竜")
    assert candidate["field_evidence"]["container_title"][0]["source_url"] == url


def test_researcher_site_query_falls_back_when_rss_ignores_site(monkeypatch) -> None:
    class Response:
        def __init__(self, body: bytes) -> None:
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _size: int) -> bytes:
            return self.body

    class Opener:
        def open(self, request, timeout: float):
            assert timeout == 3
            if "yahoo.co.jp" in request.full_url:
                return Response(
                    '<ul><li><a href="https://researchmap.jp/Koji_Shirae">'
                    '山田 花子</a><div>架空資料の比較分析</div>'
                    '</li><li><a href="https://noise.example/work">'
                    '架空資料の比較分析</a></li></ul>'.encode()
                )
            return Response(
                b'<rss><channel><item><link>https://noise.example/novels</link>'
                b'<title>Novel site</title></item></channel></rss>'
            )

    monkeypatch.setattr(
        "fukamiru.revisions.reference_web.urllib.request.build_opener",
        lambda *_args: Opener(),
    )
    hits = general_web_search(
        "架空資料の比較分析 site:researchmap.jp", timeout=3
    )
    assert hits == ["https://researchmap.jp/Koji_Shirae"]


def test_web_fallback_creates_unconfirmed_candidate_with_evidence() -> None:
    result = {"reference_id": "r1", "title": "Justice Scales", "author": "Fraser"}
    calls: list[str] = []

    def search(query: str) -> list[str]:
        calls.append(query)
        return ["https://publisher.example/book"]

    def fetch(url: str) -> dict:
        return {
            "url": url,
            "status": "retrieved",
            "structured_metadata": {
                "title": "Justice Scales",
                "record_type": "book",
                "isbn": "9780000000001",
                "publisher": "Example Press",
                "year": "2020",
                "publication_language": "en",
                "authors": ["Fraser, Nancy"],
            },
        }

    output = enrich_from_web_v29(result, search=search, fetch=fetch)
    assert len(calls) <= 4
    assert len(output["web_evidence_pages"]) == 1
    assert output["work_identity_status"] == "matched"
    assert output["edition_status"] == "unconfirmed"
    assert output["final_revision_blocked"] is True
    candidate = output["candidate_matches"][0]
    assert candidate["field_evidence"]["publisher"][0]["source_url"] == (
        "https://publisher.example/book"
    )
    assert canonicalize_reference(candidate)["v29_render_supported"] is True


def test_web_metadata_conflict_is_retained_not_overwritten() -> None:
    candidate = {
        "candidate_id": "c1",
        "title": "A Book",
        "isbn": "9780000000001",
        "publisher": "First Press",
        "year": "2020",
    }
    result = {"title": "A Book", "candidate_matches": [candidate]}
    output = enrich_from_web_v29(
        result,
        search=lambda _: ["https://publisher.example/book"],
        fetch=lambda url: {
            "url": url,
            "status": "retrieved",
            "structured_metadata": {
                "title": "A Book",
                "isbn": "9780000000001",
                "publisher": "Second Press",
                "year": "2020",
            },
        },
    )
    assert output["candidate_matches"][0]["publisher"] == "First Press"
    assert output["candidate_matches"][0]["metadata_conflicts"]["publisher"] == [
        "First Press",
        "Second Press",
    ]


def test_general_web_search_parses_only_https_rss_links(monkeypatch) -> None:
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _size: int) -> bytes:
            return (
                b"<rss><channel><item><link>https://publisher.example/book</link></item>"
                b"<item><link>http://localhost/private</link></item></channel></rss>"
            )

    class Opener:
        def open(self, _request, timeout: float):
            assert timeout == 3
            return Response()

    monkeypatch.setattr(
        "fukamiru.revisions.reference_web.urllib.request.build_opener",
        lambda *_args: Opener(),
    )
    assert general_web_search("book", timeout=3) == ["https://publisher.example/book"]


def test_discovery_v29_calls_web_fallback_without_provider_hits(monkeypatch) -> None:
    from fukamiru.revisions.reference_search import discover_reference

    class EmptyProvider:
        name = "empty"

        def search(self, _query: str, *, limit: int):
            assert limit == 20
            return []

    monkeypatch.setattr(
        "fukamiru.revisions.reference_search.general_web_search",
        lambda _query: ["https://publisher.example/book"],
    )
    monkeypatch.setattr(
        "fukamiru.revisions.reference_search.publisher_page",
        lambda url: {
            "url": url,
            "status": "retrieved",
            "structured_metadata": {
                "title": "A Book",
                "record_type": "book",
                "isbn": "9780000000001",
                "publisher": "Example Press",
                "year": "2020",
                "publication_language": "en",
                "authors": ["Doe, Jane"],
            },
        },
    )
    result = discover_reference(
        {
            "reference_id": "r1",
            "title": "A Book",
            "author": "Doe, Jane",
            "parse_status": "parsed",
            "record_type": "book",
        },
        (EmptyProvider(),),
        workflow_version=29,
    )
    assert result["web_search_attempts"]
    assert len(result["candidate_matches"]) == 1
    assert result["consulted_status"] == "unconfirmed"
    assert result["final_revision_blocked"] is True


def test_ndl_detail_extracts_roles_and_language() -> None:
    document = (
        "<dl><dt>著者・編者</dt><dd>ピエール・ブルデュー [著] "
        "石井洋二郎 訳</dd><dt>シリーズタイトル</dt></dl>"
        "<dt>本文の言語コード</dt><dd>jpn</dd>"
    )
    fields = parse_ndl_detail_metadata(document)
    assert fields["responsibility_raw"] == ["ピエール・ブルデュー [著]", "石井洋二郎 訳"]
    assert fields["publication_language"] == "jpn"


def test_irrelevant_dictionary_results_are_rejected_before_fetch() -> None:
    fetched: list[str] = []
    result = enrich_from_web_v29(
        {"title": "正義の秤", "author": "フレイザー"},
        search=lambda _query: ["https://kotobank.jp/word/正義-123"],
        fetch=lambda url: fetched.append(url) or {"url": url, "status": "retrieved"},
    )
    assert fetched == []
    assert result["web_relevance_rejected_count"] == len(result["web_search_attempts"])


def test_exact_record_enrichment_skips_general_search_when_ready() -> None:
    calls: list[str] = []
    url = "https://ndlsearch.ndl.go.jp/books/example"
    candidate = {
        "candidate_id": "c1",
        "provider": "ndl_search",
        "url": url,
        "title": "芸術の規則",
        "record_type": "book",
        "publisher": "藤原書店",
        "year": "1996",
        "responsibility_sources": [{"provider": "ndl_search", "responsibility_raw": []}],
    }
    result = enrich_from_web_v29(
        {"title": "芸術の規則", "candidate_matches": [candidate]},
        search=lambda query: calls.append(query) or [],
        fetch=lambda fetched_url: {
            "url": fetched_url,
            "status": "retrieved",
            "structured_metadata": {
                "responsibility_raw": ["ピエール・ブルデュー[著]", "石井, 洋二郎訳"],
                "publication_language": "jpn",
            },
        },
        seed_urls=(url,),
    )
    assert calls == []
    assert result["web_search_attempts"] == []
    assert canonicalize_reference(result["candidate_matches"][0])["v29_render_status"] == (
        "render_ready"
    )
