from __future__ import annotations

import io
import json
import time
import urllib.error
from dataclasses import replace
from types import SimpleNamespace

import pytest

from fukamiru.literature import transport
from fukamiru.literature.content import (
    abstract_text,
    merge_source_records,
    public_https,
    read_oa_pdf,
    source_passages,
)
from fukamiru.literature.service import NDLSearchProvider, SemanticScholarProvider
from fukamiru.revisions.research_validation import (
    LiteratureSearchDeferred,
    academic_variants,
    grounded_support,
    search_all_providers,
)
from fukamiru.storage.payloads import PayloadStore


@pytest.fixture
def case():
    from test_worker import WorkerTests

    value = WorkerTests()
    value.setUp()
    yield value
    value.tearDown()


def test_all_variants_are_searched_even_after_full_irrelevant_first_page():
    calls = []

    def search(query, *, limit):
        calls.append(query)
        return [SimpleNamespace(provider_record_id="duplicate")] * limit + [
            SimpleNamespace(provider_record_id=query)
        ]

    result = search_all_providers((SimpleNamespace(name="crossref", search=search),),
                                  ["broad", "narrow", "rival"], 6)[0]
    assert calls == ["broad", "narrow", "rival"]
    assert len(result["candidates"]) == 4
    assert result["completed_queries"] == calls


def test_capacity_failure_retains_completed_queries_and_resumes_only_pending():
    calls = []

    def search(query, *, limit):
        calls.append(query)
        if query == "second" and calls.count("second") == 1:
            raise transport.ProviderRequestError(
                "openalex", "rate_limited", 429, retry_after_seconds=90
            )
        return [query]

    provider = SimpleNamespace(name="openalex", search=search)
    first = search_all_providers((provider,), ["first", "second", "third"], 1)[0]
    assert first["candidates"] == ["first"]
    assert first["pending_queries"] == ["second", "third"]
    assert first["retry_at"] >= time.time() + 89
    second = search_all_providers((provider,), ["first", "second", "third"], 1,
                                 completed_queries={"openalex": first["completed_queries"]})[0]
    assert calls == ["first", "second", "second", "third"]
    assert second["pending_queries"] == []


def test_query_contract_rejects_explanation_and_keeps_japanese_and_english():
    assert academic_variants([
        "漫画雑誌 ジェンダー", "manga magazines gender readership",
        "漫画雑誌におけるジェンダー表象に関する研究", "a " * 13,
        "長" * 97, "漫画雑誌 ジェンダー",
    ]) == ["漫画雑誌 ジェンダー", "manga magazines gender readership"]


def test_semantic_scholar_requests_abstract_and_long_abstract_is_not_prefix_only():
    assert "abstract" in SemanticScholarProvider.fields.split(",")
    text = "前置き。" * 2000 + "漫画雑誌は読者のジェンダー観を形成する。"
    restored = abstract_text({"abstract": "<jats:p>" + text + "</jats:p>"})
    excerpt = source_passages(restored, ["漫画雑誌 ジェンダー 読者"])
    assert "漫画雑誌は読者のジェンダー観を形成する。" in excerpt
    assert len(excerpt) < len(text)


def test_oa_reader_does_not_fetch_unknown_rights_or_private_redirect(monkeypatch):
    candidate = SimpleNamespace(oa_url="https://example.org/paper.pdf", oa_status="unknown")
    assert read_oa_pdf(candidate, ["query"], timeout=1, max_bytes=1024)["status"] == (
        "not_open_access"
    )
    monkeypatch.setattr("fukamiru.literature.content.socket.getaddrinfo", lambda *a, **k: [
        (None, None, None, None, ("127.0.0.1", 443))
    ])
    assert not public_https("https://example.org/paper.pdf")
    assert not public_https("http://example.org/paper.pdf")


def test_ndl_uses_shared_rate_budget_and_caches_xml(monkeypatch):
    transport._budgets.clear()
    calls = []

    class Response:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return b'<rss><channel><item><title>Paper</title><guid>1</guid></item></channel></rss>'

    def open_url(*args, **kwargs):
        calls.append(args[0].full_url)
        return Response()

    monkeypatch.setattr(transport.urllib.request, "urlopen", open_url)
    provider = NDLSearchProvider(minimum_interval_seconds=0)
    assert provider.search("漫画", limit=1)[0].title == "Paper"
    assert provider.search("漫画", limit=1)[0].title == "Paper"
    assert len(calls) == 1
    assert provider.request_outcomes[-1]["outcome"] == "cache_hit"
    transport._budgets.clear()


def test_retry_after_is_preserved_without_reissuing_during_cooldown(monkeypatch):
    transport._budgets.clear()
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        raise urllib.error.HTTPError("https://test.invalid/", 429, "busy",
                                     {"Retry-After": "120"}, io.BytesIO(b"slow down"))

    monkeypatch.setattr(transport.urllib.request, "urlopen", fail)
    provider = SemanticScholarProvider()
    for _ in range(2):
        with pytest.raises(transport.ProviderRequestError) as raised:
            provider.search("first", limit=1)
        assert raised.value.retry_after_seconds > 119
    assert len(calls) == 1
    transport._budgets.clear()


def test_literature_checkpoint_is_durable_and_does_not_reduce_codex_capacity(case):
    case._create_run("manuscript_rewrite")
    claimed = case.worker._claim(lane="ingestion_cpu")
    assert claimed
    claimed = replace(claimed, workflow_name="manuscript_generator", workflow_version="32")
    checkpoint = {"claim": {"completed_queries": {"openalex": ["done"]}}}
    error = LiteratureSearchDeferred({"_academic_search_checkpoint": checkpoint}, 180)
    assert case.worker._should_wait_for_codex(claimed, error)
    case.worker._wait_for_codex(claimed, error)
    with case.databases.state.connect() as connection:
        row = connection.execute("SELECT * FROM node_runs WHERE id=?",
                                 (claimed.node_run_id,)).fetchone()
        inputs = PayloadStore(connection).unpack(json.loads(row["input_json"]))
        assert row["status"] == "queued"
        assert inputs["_academic_search_checkpoint"] == checkpoint
        assert inputs["_external_retry"]["reason"] == "literature_search_deferred"
    assert case.worker._codex_retry_after == 0


@pytest.mark.parametrize("verdict_status,quote,expected", [
    ("supports_subclaim", "少女マンガの読者像を分析した。", True),
    ("supports_subclaim", "本文に存在しない文章。", False),
    ("insufficient", "少女マンガの読者像を分析した。", False),
])
def test_low_lexical_score_is_reviewed_but_only_grounded_support_is_adopted(
    case, monkeypatch, verdict_status, quote, expected,
):
    case._create_run("manuscript_rewrite")
    task = case.worker._claim(lane="ingestion_cpu")
    task = replace(task, workflow_name="manuscript_generator", workflow_version="32",
                   node_id="validate_evidence_support")
    seen = []

    def audit(_task, phase, units, sources):
        seen.extend(sources)
        return {"verdicts": [{"unit_id": unit["unit_id"], "status": verdict_status,
            "rationale": "出典本文の範囲で評価した。",
            "citations": [{"candidate_key": "openalex:paper", "quote": quote}]}
            for unit in units]}

    monkeypatch.setattr(case.worker, "_readonly_manuscript_audit", audit)
    saved = []
    monkeypatch.setattr(case.worker, "_save_manuscript_evidence", lambda *a: saved.append(a[-1]))
    result = case.worker._manuscript_validate_evidence_support(task, {
        "research_search_plan": [{"query": "漫画読者の性別表現", "obligation_id": "O",
            "policy": "authoring-atomic-evidence-v1", "subclaims": ["読者像が形成される。"]}],
        "research_discoveries": [{"query": "漫画読者の性別表現", "candidates": [{
            "provider": "openalex", "provider_record_id": "paper", "source_type": "online_paper",
            "title": "「少女」とは誰か", "excerpt": "少女マンガの読者像を分析した。",
            "confidence": 0.118, "discovery_quality": {"passed": False},
            "provenance": {"source_level": "abstract"},
        }]}],
    }).context
    assert len(seen) == 1
    assert result["evidence_coverage"][0]["covered"] is expected
    assert bool(saved) is expected
    assert result["reference_usage_records"][0]["passed_to_draft"] is expected
    if saved:
        assert case.worker._manuscript_evidence_quality("漫画読者の性別表現", saved[0])["passed"]


def test_quotes_must_not_bridge_artificial_passage_boundaries():
    assert not grounded_support({"status": "supports_subclaim", "rationale": "理由",
        "citations": [{"candidate_key": "K", "quote": "前半。\n[…]\n後半。"}]},
        [{"candidate_key": "K", "excerpt": "前半。\n[…]\n後半。"}])


def test_oa_pdf_reader_keeps_matching_page_locator_and_content_hash(monkeypatch):
    import hashlib

    import fukamiru.literature.content as content

    data = b"%PDF-source-fixture"

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size):
            assert size == 1025
            return data

    monkeypatch.setattr(content, "public_https", lambda url: True)
    monkeypatch.setattr(content.urllib.request, "build_opener", lambda *a: SimpleNamespace(
        open=lambda *a, **k: Response()
    ))
    monkeypatch.setattr(content, "PdfReader", lambda stream: SimpleNamespace(
        is_encrypted=False, pages=[
            SimpleNamespace(extract_text=lambda: "序論。") for _ in range(5)
        ] + [SimpleNamespace(extract_text=lambda: "漫画読者のジェンダー観を検証した。")]
    ))
    candidate = SimpleNamespace(oa_url="https://example.org/paper.pdf", oa_status="green")
    result = read_oa_pdf(candidate, ["漫画読者 ジェンダー"], timeout=1, max_bytes=1024)
    assert result["status"] == "retrieved"
    assert "p. 6" in result["locator"]
    assert "ジェンダー観を検証" in result["excerpt"]
    assert result["content_sha256"] == hashlib.sha256(data).hexdigest()


def test_preferred_retry_resumes_search_without_replanning_or_losing_original_plan(
    case, monkeypatch,
):
    case._create_run("manuscript_rewrite")
    task = replace(case.worker._claim(lane="ingestion_cpu"),
                   workflow_name="manuscript_generator", workflow_version="32")
    context = {
        "use_online_research": True,
        "research_queries": ["claim"],
        "research_query_assignments": [{"query": "claim", "obligation_id": "O",
            "origin": "authoring_atomic_proposition", "evidence_requirement": "preferred"}],
        "research_search_plan": [{"query": "claim", "obligation_id": "O", "variants": ["old"]}],
        "evidence_coverage": [{"obligation_id": "O", "covered": False}],
        "research_discoveries": [{"query": "claim", "candidates": [{"title": "old source"}]}],
    }
    planned = []

    def generate(*a, **k):
        planned.append(1)
        return {"plans": [{"unit_id": "O", "variants": ["new keywords"]}]}

    calls = []

    def search(_task, inputs):
        calls.append(inputs)
        if len(calls) == 1:
            raise LiteratureSearchDeferred({"_academic_search_checkpoint": {"claim": {
                "candidates": [{"title": "partial source"}],
                "completed_queries": {"openalex": ["done"]},
            }}}, 120)
        assert inputs["_academic_search_checkpoint"]["claim"]["completed_queries"] == {
            "openalex": ["done"]
        }
        return SimpleNamespace(context={**inputs, "research_discoveries": [{
            "query": "claim", "candidates": [{"title": "new source"}]
        }]})

    monkeypatch.setattr(case.worker, "_authoring_generate", generate)
    monkeypatch.setattr(case.worker, "_manuscript_rewrite_evidence", search)
    monkeypatch.setattr(case.worker, "_manuscript_validate_evidence_support",
                        lambda task, inputs: SimpleNamespace(context=inputs))
    with pytest.raises(LiteratureSearchDeferred) as raised:
        case.worker._authoring_bounded_research_retry(task, context)
    resumed = case.worker._authoring_bounded_research_retry(task, raised.value.context).context
    assert planned == [1]
    assert resumed["research_search_plan"] == context["research_search_plan"]
    assert resumed["bounded_research_retry_report"]["attempted"] is True
    assert len(resumed["research_discoveries"][0]["candidates"]) == 2
    assert "_bounded_research_resume" not in resumed


def test_evidence_search_preserves_partial_candidates_across_resume(case, monkeypatch):
    import fukamiru.worker.runner as runner

    case._create_run("manuscript_rewrite")
    task = replace(case.worker._claim(lane="ingestion_cpu"),
                   workflow_name="manuscript_generator", workflow_version="32")
    context = {"use_online_research": True, "research_queries": ["漫画雑誌 読書"],
               "research_search_plan": [{"query": "漫画雑誌 読書", "variants": ["manga reading"]}]}
    calls = []

    def search(providers, variants, limit, *, completed_queries, pending_queries):
        calls.append(completed_queries)
        index = len(calls)
        candidate = SimpleNamespace(
            provider="openalex", provider_record_id=f"P{index}",
            title="Protein transport", authors=(), publication_title=None,
            publication_date=None, record_type="article", doi=None, publisher=None,
            url=f"https://openalex.org/P{index}", oa_url=None, oa_status=None,
            provider_payload={
                "abstract": "" if index == 1 else "Proteins were measured in the laboratory."
            },
        )
        return [{"provider": "openalex", "candidates": [candidate], "query_count": 1,
                 "error": "429" if index == 1 else None, "attempts": [],
                 "completed_queries": ["done"] if index == 1 else ["done", "manga reading"],
                 "pending_queries": ["manga reading"] if index == 1 else [],
                 "retry_at": time.time() + 90 if index == 1 else None}]

    monkeypatch.setattr(runner, "search_all_providers", search)
    with pytest.raises(LiteratureSearchDeferred) as raised:
        case.worker._manuscript_rewrite_evidence(task, context)
    result = case.worker._manuscript_rewrite_evidence(task, raised.value.context).context
    assert calls == [{}, {"openalex": ["done"]}]
    assert len(result["research_discoveries"][0]["candidates"]) == 2
    assert result["evidence_context"] == []
    assert "_academic_search_checkpoint" not in result


def test_provider_pending_queries_survive_a_changed_retry_plan():
    calls = []
    provider = SimpleNamespace(
        name="openalex", search=lambda query, limit: calls.append(query) or []
    )
    result = search_all_providers((provider,), ["new keywords"], 1,
        completed_queries={"openalex": ["old success"]},
        pending_queries={"openalex": ["old success", "old pending"]})[0]
    assert calls == ["old pending", "new keywords"]
    assert result["pending_queries"] == []


def test_one_unavailable_provider_does_not_block_reading_available_sources(case, monkeypatch):
    import fukamiru.worker.runner as runner

    case._create_run("manuscript_rewrite")
    task = replace(case.worker._claim(lane="ingestion_cpu"),
                   workflow_name="manuscript_generator", workflow_version="32")
    candidate = SimpleNamespace(
        provider="openalex", provider_record_id="P", title="Protein transport", authors=(),
        publication_title=None, publication_date=None, record_type="article", doi=None,
        publisher=None,
        url="https://openalex.org/P", oa_url=None, oa_status=None,
        provider_payload={"abstract": "Proteins were measured in the laboratory."},
    )
    monkeypatch.setattr(runner, "search_all_providers", lambda *a, **k: [{
        "provider": "openalex", "candidates": [candidate], "query_count": 1,
        "error": "429", "attempts": [], "completed_queries": ["done"],
        "pending_queries": ["pending"], "retry_at": time.time() + 90,
    }])
    context = {"use_online_research": True, "research_queries": ["漫画 読書"]}
    result = case.worker._manuscript_rewrite_evidence(task, context).context
    assert result["academic_search_status"] == "partial_provider_unavailable"
    assert result["research_discoveries"][0]["candidates"][0]["excerpt"]
    assert "_academic_search_checkpoint" in result
    task = replace(task, node_id="bounded_research_retry")
    with pytest.raises(LiteratureSearchDeferred):
        case.worker._manuscript_rewrite_evidence(task, result)
    result["_external_retry"] = {"count": 1}
    resumed = case.worker._manuscript_rewrite_evidence(task, result).context
    assert resumed["academic_search_status"] == "partial_provider_unavailable"
    assert resumed["evidence_context"] == []


def test_repeated_search_hit_preserves_previously_read_full_text():
    body = {"provider": "openalex", "provider_record_id": "P", "excerpt": "取得済みの本文。",
            "source_url": "https://example.org/body.pdf", "locator": "p. 6",
            "provenance": {"source_level": "full_text"}, "confidence": 0.1}
    metadata = {**body, "excerpt": "", "provenance": {"source_level": "bibliographic_metadata"},
                "confidence": 0.9}
    assert merge_source_records([body, metadata]) == [body]
    assert merge_source_records([metadata, body]) == [body]
