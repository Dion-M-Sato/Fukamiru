from fukamiru.revisions.reference_search import (
    build_search_hypotheses,
    query_plan,
    query_variants,
)
from fukamiru.revisions.reference_web import SearchLead, _web_relevance_decision


def test_exact_library_result_is_admitted_with_audit() -> None:
    lead = SearchLead(
        "https://ndlsearch.ndl.go.jp/books/R123",
        "散種 - 国立国会図書館サーチ",
        "ジャック・デリダ 著 書誌詳細",
        1,
    )
    decision = _web_relevance_decision(lead, "散種", "デリダ", [])
    assert decision["decision"] == "accept"
    assert decision["deterministic_features"]["title_exact"]
    assert decision["relevance_score"] >= decision["threshold"]


def test_publisher_product_page_with_exact_title_is_admitted() -> None:
    lead = SearchLead(
        "https://publisher.example/products/abc123",
        "ジェンダー・トラブル | 書籍",
        "ジュディス・バトラー、出版社、ISBN 9780000000000",
        2,
    )
    decision = _web_relevance_decision(lead, "ジェンダー・トラブル", "バトラー", [])
    assert decision["decision"] == "accept"


def test_dictionary_and_generic_topic_results_are_rejected() -> None:
    dictionary = SearchLead("https://ja.wikipedia.org/wiki/散種", "散種とは", "概要", 1)
    generic = SearchLead("https://example.com/topic/identity", "哲学の概説", "思想全般", 2)
    assert _web_relevance_decision(dictionary, "散種", "デリダ", [])["reject_reason"] == (
        "dictionary_or_encyclopedia"
    )
    assert _web_relevance_decision(generic, "散種", "デリダ", [])["reject_reason"] == (
        "title_no_match"
    )


def test_original_title_hypothesis_is_search_only() -> None:
    entry = {"title": "架空の分類研究", "author": "山田花子"}
    assert build_search_hypotheses(entry)["titles"] == []
    hypotheses = build_search_hypotheses(
        entry, {"titles": ["Synthetic Classification"], "authors": ["Alice Example"]}
    )
    assert "Synthetic Classification" in hypotheses["titles"]
    assert hypotheses["provenance"] == "search_hypothesis_only"
    assert "Synthetic Classification Alice Example" in query_variants(entry, hypotheses)
    assert entry == {"title": "架空の分類研究", "author": "山田花子"}
    lead = SearchLead(
        "https://catalog.example.test/books/alpha", "Synthetic Classification", "Alice Example", 1,
    )
    assert _web_relevance_decision(
        lead, "架空の分類研究", "山田花子", [], title_hypotheses=("Synthetic Classification",),
    )["decision"] == "accept"


def test_query_plan_prioritizes_recall_and_records_reason() -> None:
    entry = {
        "title": "管理される心 : 感情が商品になるとき",
        "author": "ホックシールド", "translators": ["石川准"],
    }
    plan = query_plan(entry)
    assert plan[0]["query_reason"] == "exact_title"
    assert any(row["query_reason"] == "title_without_subtitle" for row in plan)
    assert any(row["query_reason"] == "title_translators" for row in plan)
    assert all(row["target_source_class"] == "structured_bibliography" for row in plan)


def test_ndl_timeout_retry_is_bounded_and_audited(monkeypatch) -> None:
    from fukamiru.revisions.reference_search import discover_reference

    class TransientProvider:
        name = "ndl_search"

        def __init__(self) -> None:
            self.calls = 0

        def search_bibliographic(self, *_args, limit: int):
            assert limit == 20
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("timed out")
            return []

        def search(self, _query: str, *, limit: int):
            assert limit == 20
            return []

    monkeypatch.setattr("fukamiru.revisions.reference_search.time.sleep", lambda _: None)
    monkeypatch.setattr(
        "fukamiru.revisions.reference_search.general_web_search", lambda _: [],
    )
    provider = TransientProvider()
    output = discover_reference(
        {"title": "Unidentified Book", "author": "Doe", "record_type": "book"},
        (provider,), workflow_version=29,
    )
    assert provider.calls == 2
    assert output["search_attempts"][0]["attempts"][0]["status"] == "timeout_retry"
    assert output["provider_statuses"][0]["status"] == "available_no_hits"


def test_japanese_catalog_is_queried_before_other_structured_sources(monkeypatch) -> None:
    from fukamiru.revisions.reference_search import discover_reference

    calls = []

    class Provider:
        def __init__(self, name):
            self.name = name

        def search(self, _query, *, limit):
            assert limit == 20
            calls.append(self.name)
            return []

    monkeypatch.setattr("fukamiru.revisions.reference_search.general_web_search", lambda _: [])
    discover_reference(
        {"title": "テスト書", "author": "著者", "record_type": "book"},
        (Provider("crossref"), Provider("ndl_search")), workflow_version=29,
    )
    assert calls[0] == "ndl_search"
