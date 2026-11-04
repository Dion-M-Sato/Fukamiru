"""RAG-first research, canonical citations and durable source reuse."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest

from fukamiru.authoring.source_reads import (
    CachedLiteratureProvider,
    SourceReadCache,
    knowledge_revision,
    known_work_passages,
    known_work_read,
    named_work_hits,
    reuse_local_passages,
)
from fukamiru.knowledge.service import IngestionService, KnowledgeSearchService
from fukamiru.literature.content import pdf_passages, read_oa_pdf
from fukamiru.literature.service import LiteratureCandidate


@pytest.fixture
def case():
    from test_worker import WorkerTests

    value = WorkerTests()
    value.setUp()
    yield value
    value.tearDown()


def task(case, node="resolve_manuscript_evidence"):
    case._create_run("manuscript_rewrite")
    return replace(
        case.worker._claim(lane="ingestion_cpu"),
        workflow_name="manuscript_generator",
        workflow_version="32",
        node_id=node,
    )


def plan(query="漫画 表現", obligation="O"):
    return {
        "query": query,
        "variants": ["manga visual conventions"],
        "obligation_id": obligation,
        "subclaims": [query],
        "policy": "authoring-atomic-evidence-v1",
        "source_kind": "mixed",
        "origin": "authoring_atomic_proposition",
        "evidence_requirement": "preferred",
        "proposition_id": "p1",
        "claim_ids": ["c1"],
    }


def hit():
    return {
        "chunk_id": "chunk",
        "source_asset_id": "asset",
        "document_unit_id": "doc",
        "title": "異なる言葉を使う研究",
        "text": "各媒体には異なる描画の慣習がある。",
        "retrieval": "semantic",
        "bibliography": {"metadata_status": "partial"},
        "citation": {
            "source_span_id": "span",
            "source_span_ids": ["span"],
            "start_page": 7,
            "end_page": 8,
        },
    }


def local_record():
    return {
        "query": "漫画 表現",
        "provider": "knowledge_base",
        "provider_record_id": "chunk",
        "source_type": "local_rag",
        "title": "関連資料",
        "excerpt": hit()["text"],
        "locator": "pp. 7–8",
        "confidence": 0.01,
        "discovery_quality": {"passed": False},
        "provenance": {
            "source_level": "full_text",
            "source_asset_id": "asset",
            "citation": hit()["citation"],
        },
    }


def test_local_proof_uses_existing_review_and_skips_external_retry(case, monkeypatch):
    import fukamiru.worker.runner as runner

    claimed = task(case)
    case.worker.settings = replace(
        case.worker.settings,
        translation=replace(case.worker.settings.translation, academic_research_online=True),
    )
    monkeypatch.setattr(case.worker, "_authoring_search_hits", lambda *a, **k: [hit()])
    provider_calls = []

    def online(providers, *a, **k):
        provider_calls.extend(providers)
        return []

    monkeypatch.setattr(runner, "search_all_providers", online)
    saved = []
    monkeypatch.setattr(case.worker, "_save_manuscript_evidence", lambda *a: saved.append(a[-1]))
    audits = []

    def audit(_task, phase, units, sources):
        audits.append(sources)
        return {
            "verdicts": [
                {
                    "unit_id": units[0]["unit_id"],
                    "status": "supports_subclaim",
                    "rationale": "出典の記述を確認した。",
                    "citations": [
                        {"candidate_key": sources[0]["candidate_key"], "quote": hit()["text"]}
                    ],
                }
            ]
        }

    monkeypatch.setattr(case.worker, "_readonly_manuscript_audit", audit)
    context = {
        "use_rag": True,
        "use_online_research": True,
        "research_queries": [plan()["query"]],
        "research_query_assignments": [plan()],
        "research_search_plan": [plan()],
    }
    searched = case.worker._manuscript_rewrite_evidence(claimed, context).context
    checked = case.worker._manuscript_validate_evidence_support(claimed, searched).context
    result = case.worker._authoring_bounded_research_retry(
        replace(claimed, node_id="bounded_research_retry"), checked
    ).context
    assert not provider_calls
    assert searched["research_retrieval_stage"] == "local_first"
    assert searched["online_research_performed"] is False
    assert checked["evidence_coverage"][0]["covered"] is True
    assert len(audits) == 1 and len(saved) == 1
    assert saved[0]["locator"] == "pp. 7–8"
    assert saved[0]["provenance"]["citation"]["source_span_ids"] == ["span"]
    assert result["bounded_research_retry_report"]["attempted"] is False


def test_retry_searches_only_the_gap_and_passes_partial_reading_to_planner(case, monkeypatch):
    claimed = task(case, "bounded_research_retry")
    first, second = plan("verified", "A"), plan("missing", "B")
    generated = []

    def generate(*args, **kwargs):
        generated.append(kwargs["payload"])
        return {"plans": []}  # The original variants have not been sent externally yet.

    monkeypatch.setattr(case.worker, "_authoring_generate", generate)
    searched = []
    monkeypatch.setattr(
        case.worker,
        "_authoring_execute_research_retry",
        lambda task, context, targets, plans: (
            searched.append((targets, plans)) or SimpleNamespace(context=context)
        ),
    )
    case.worker._authoring_bounded_research_retry(
        claimed,
        {
            "use_rag": True,
            "use_online_research": True,
            "research_retrieval_stage": "local_first",
            "research_query_assignments": [first, second],
            "research_search_plan": [first, second],
            "evidence_coverage": [
                {"obligation_id": "A", "covered": True},
                {
                    "obligation_id": "B",
                    "covered": False,
                    "support_verdict": {"rationale": "年代が未確認"},
                },
            ],
            "research_observations": [{"obligation_id": "B", "quote": "関連する記述。"}],
        },
    )
    assert searched[0][0] == ["B"]
    assert searched[0][1][0]["variants"] == second["variants"]
    assert generated[0]["units"][0]["related_passages"][0]["quote"] == "関連する記述。"
    assert generated[0]["units"][0]["support_details"] == "年代が未確認"


@pytest.mark.parametrize(
    "external_policy",
    [
        {"requires_external_research": True},
        {"research_goal": "rival"},
    ],
)
def test_explicit_external_scope_is_not_satisfied_by_local_proof(
    case, monkeypatch, external_policy
):
    claimed = task(case, "bounded_research_retry")
    target = {**plan(), **external_policy}
    monkeypatch.setattr(case.worker, "_authoring_generate", lambda *a, **k: {"plans": []})
    selected = []
    monkeypatch.setattr(
        case.worker,
        "_authoring_execute_research_retry",
        lambda task, context, targets, plans: (
            selected.extend(targets) or SimpleNamespace(context=context)
        ),
    )
    case.worker._authoring_bounded_research_retry(
        claimed,
        {
            "use_rag": True,
            "use_online_research": True,
            "research_retrieval_stage": "local_first",
            "research_query_assignments": [target],
            "research_search_plan": [target],
            "evidence_coverage": [{"obligation_id": "O", "covered": True}],
        },
    )
    assert selected == ["O"]


def test_partial_quotes_survive_without_becoming_verified_evidence(case, monkeypatch):
    claimed = task(case)
    source = local_record()
    # No asset match or OA URL: the observation is retained, no extra model call is added.
    source["provenance"].pop("source_asset_id")
    monkeypatch.setattr(
        case.worker,
        "_readonly_manuscript_audit",
        lambda *a: {
            "verdicts": [
                {
                    "unit_id": "O",
                    "status": "insufficient",
                    "rationale": "因果関係は未確認。",
                    "citations": [
                        {"candidate_key": "knowledge_base:chunk", "quote": hit()["text"]}
                    ],
                }
            ]
        },
    )
    result = case.worker._manuscript_validate_evidence_support(
        claimed,
        {
            "use_rag": True,
            "research_search_plan": [plan()],
            "research_discoveries": [{"query": plan()["query"], "candidates": [source]}],
        },
    ).context
    assert result["evidence_coverage"][0]["covered"] is False
    assert result["evidence_context"] == []
    assert result["research_observations"][0]["quote"] == hit()["text"]
    assert result["reference_usage_records"][0]["passed_to_draft"] is False


def test_read_cache_is_single_flight_and_survives_worker_restart(case):
    claimed = task(case)
    cache = case.worker._authoring_source_reads(claimed.run_id)
    calls = []

    def load():
        calls.append(1)
        return {"excerpt": "取得済み本文"}

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(
            row["excerpt"] == "取得済み本文"
            for row in pool.map(
                lambda _: cache.read("pdf", {"doi": "10.1234/paper"}, load), range(4)
            )
        )
    restarted = SourceReadCache(case.databases.state, claimed.run_id, threading.RLock())
    assert restarted.read("pdf", {"doi": "10.1234/paper"}, load)["excerpt"] == "取得済み本文"
    assert calls == [1]


def test_rag_search_cache_invalidates_when_the_corpus_changes(case, monkeypatch):
    import fukamiru.worker.runner as runner

    claimed = task(case)
    calls = []
    monkeypatch.setattr(
        runner,
        "KnowledgeSearchService",
        lambda *a, **k: SimpleNamespace(search=lambda *a, **k: calls.append(1) or [hit()]),
    )
    case.worker._authoring_search_hits(claimed.run_id, "query", limit=8)
    case.worker._authoring_search_hits(claimed.run_id, "query", limit=8)
    assert calls == [1]
    with case.databases.knowledge.connect() as connection:
        service = IngestionService(connection, case.settings)
        service.ingest_path(str(case.source))
    case.worker._authoring_search_hits(claimed.run_id, "query", limit=8)
    assert calls == [1, 1]


def test_draft_reuses_registered_passages_with_real_ids_and_locations(case, monkeypatch):
    task(case)
    source = local_record()
    context = {
        "use_rag": True,
        "research_question": {"text": "関連する研究は何か"},
        "research_query_assignments": [plan()],
        "research_discoveries": [{"query": plan()["query"], "candidates": [source]}],
    }
    monkeypatch.setattr(
        case.worker, "_authoring_search_hits", lambda *a, **k: pytest.fail("duplicate search")
    )
    first = case.worker._authoring_retrieval_context(context, sections=[{"claim_ids": ["c1"]}])
    assert first == case.worker._authoring_retrieval_context(
        context, sections=[{"claim_ids": ["c1"]}]
    )
    assert '"source_span_ids": ["span"]' in first
    assert "pp. 7–8" in first and hit()["text"] in first


def test_draft_fallback_search_is_cached_per_run(case, monkeypatch):
    import fukamiru.worker.runner as runner

    claimed = task(case)
    calls = []
    monkeypatch.setattr(
        runner,
        "KnowledgeSearchService",
        lambda *a, **k: SimpleNamespace(search=lambda *a, **k: calls.append(1) or [hit()]),
    )
    context = {
        "use_rag": True,
        "authoring_run_id": claimed.run_id,
        "note_text": "漫画の表現を調べる",
    }
    assert case.worker._authoring_retrieval_context(context)
    assert case.worker._authoring_retrieval_context(context)
    assert calls == [1]


def test_provider_queries_survive_restart_and_failed_queries_are_not_cached(case):
    claimed = task(case)
    calls = []
    candidate = LiteratureCandidate(
        "crossref",
        "P",
        "Paper",
        (),
        None,
        None,
        None,
        "article",
        "10.1234/paper",
        None,
        None,
        None,
        None,
        {},
    )

    def search(query, *, limit):
        calls.append(query)
        if query == "retry" and calls.count("retry") == 1:
            raise OSError("temporary")
        return [candidate]

    backend = SimpleNamespace(name="crossref", search=search)
    cache = case.worker._authoring_source_reads(claimed.run_id)
    assert CachedLiteratureProvider(backend, cache).search("query", limit=6) == [candidate]
    restarted = SourceReadCache(case.databases.state, claimed.run_id, threading.RLock())
    assert CachedLiteratureProvider(backend, restarted).search("query", limit=6) == [candidate]
    with pytest.raises(OSError):
        CachedLiteratureProvider(backend, cache).search("retry", limit=6)
    assert CachedLiteratureProvider(backend, cache).search("retry", limit=6) == [candidate]
    assert calls == ["query", "retry", "retry"]


def test_provider_failure_keeps_the_http_diagnostic(case):
    from fukamiru.literature.transport import ProviderRequestError

    claimed = task(case)
    backend = SimpleNamespace(name="crossref", request_outcomes=[])

    def search(query, *, limit):
        backend.request_outcomes.append({"outcome": "rate_limited", "status_code": 429})
        raise ProviderRequestError("crossref", "rate_limited", 429, retry_after_seconds=90)

    backend.search = search
    cached = CachedLiteratureProvider(backend, case.worker._authoring_source_reads(claimed.run_id))
    with pytest.raises(ProviderRequestError) as raised:
        cached.search("query", limit=6)
    assert raised.value.retry_after_seconds == 90
    assert cached.request_outcomes == [{"outcome": "rate_limited", "status_code": 429}]


def test_work_identity_keeps_distinct_passages_and_different_editions():
    first = local_record()
    first["provenance"]["doi"] = "https://doi.org/10.1234/Paper"
    second = {**local_record(), "provider_record_id": "other", "excerpt": "別の箇所。"}
    second["provenance"]["doi"] = "10.1234/paper"
    online = {
        "provider": "crossref",
        "provider_record_id": "10.1234/paper",
        "source_type": "online_paper",
        "provenance": {"doi": "10.1234/paper"},
    }
    other_edition = {**online, "provider_record_id": "new", "provenance": {"doi": "10.1234/new"}}
    result = reuse_local_passages([first, second, online, other_edition])
    assert result == [first, second, other_edition]
    assert first["provenance"]["provider_aliases"][0]["provider"] == "crossref"


def test_read_existing_work_without_downloading_or_reembedding(case, monkeypatch):
    import fukamiru.worker.runner as runner

    claimed = task(case)
    path = case.root / "paper.html"
    path.write_text(
        '<html><head><meta name="citation_doi" content="10.1234/paper"></head>'
        "<body><p>前置き。</p><p>料理漫画では食事が会話の場になる。</p></body></html>",
        encoding="utf-8",
    )
    with case.databases.knowledge.connect() as connection:
        IngestionService(connection, case.settings).ingest_path(str(path))
        before = knowledge_revision(connection)
    monkeypatch.setattr(
        runner, "read_oa_pdf", lambda *a, **k: pytest.fail("duplicate PDF download")
    )
    record = {
        **local_record(),
        "provider": "openalex",
        "provider_record_id": "W",
        "source_type": "online_paper",
        "provenance": {
            "doi": "10.1234/paper",
            "oa_status": "green",
            "oa_url": "https://example.org/P.pdf",
        },
    }
    result = case.worker._authoring_read_research_source(
        claimed, {"use_rag": True}, record, ["料理漫画"]
    )
    assert "料理漫画では食事が会話の場になる。" in result["excerpt"]
    assert result["provenance"]["source_origin"] == "local_rag"
    assert result["provenance"]["citation"]["source_span_ids"]
    with case.databases.knowledge.connect() as connection:
        assert knowledge_revision(connection) == before


def test_pdf_page_selection_uses_late_pages_and_reselects_cached_text():
    content = {
        "status": "retrieved",
        "read_complete": True,
        "pages": [
            {"number": n, "text": ("序論" * 1000 if n < 50 else "料理漫画の調理と人間関係。")}
            for n in range(1, 51)
        ],
    }
    selected = pdf_passages(content, ["料理漫画 人間関係"])
    assert "p. 50" in selected["locator"]
    assert "料理漫画の調理と人間関係。" in selected["excerpt"]
    assert selected["excerpt_complete"] is False


def test_local_excerpt_budget_never_implies_complete_source_coverage():
    read = {
        "source_asset_id": "asset", "document_unit_id": "doc",
        "content_sha256": "hash", "bibliography": {},
        "spans": [{"id": "span", "text": "序論" * 5000 + "料理漫画", "start_page": 1}],
    }
    partial = known_work_passages(read, ["料理漫画"], limit=2000)
    assert "料理漫画" in partial["excerpt"]
    assert partial["read_complete"] is True
    assert partial["excerpt_complete"] is False
    read["spans"][0]["text"] = "完全に収まる資料。"
    assert known_work_passages(read, ["料理漫画"])["excerpt_complete"] is True


def test_long_abstract_is_read_when_needed_and_pdf_is_shared_across_claims(case, monkeypatch):
    import fukamiru.worker.runner as runner

    claimed = task(case)
    downloads = []
    audits = []
    abstract = "研究の対象と背景についての説明。" * 40
    body = "この実験では対象の変化を確認した。"

    def read(candidate, *a, **k):
        downloads.append(candidate.oa_url)
        return {
            "status": "retrieved",
            "pages": [{"number": 55, "text": body}],
            "read_complete": True,
            "content_sha256": "abc",
            "source_url": candidate.oa_url,
        }

    def audit(task, phase, units, sources):
        audits.append(units[0]["query"])
        source = sources[0]
        complete = source["provenance"]["source_level"] == "full_text"
        return {
            "verdicts": [
                {
                    "unit_id": units[0]["unit_id"],
                    "status": "supports_subclaim" if complete else "insufficient",
                    "rationale": "結果の確認が必要。",
                    "citations": [
                        {
                            "candidate_key": source["candidate_key"],
                            "quote": body if complete else abstract,
                        }
                    ],
                }
            ]
        }

    monkeypatch.setattr(runner, "read_oa_pdf", read)
    monkeypatch.setattr(case.worker, "_readonly_manuscript_audit", audit)
    monkeypatch.setattr(case.worker, "_save_manuscript_evidence", lambda *a: None)
    for index, query in enumerate(["first claim", "different claim"]):
        source = {
            "query": query,
            "provider": "openalex",
            "provider_record_id": f"P{index}",
            "source_type": "online_paper",
            "title": "対象の実験",
            "excerpt": abstract,
            "confidence": 0.9,
            "provenance": {
                "source_level": "abstract",
                "doi": "10.1234/paper",
                "oa_status": "green",
                "oa_url": f"https://example.org/version-{index}.pdf",
            },
        }
        result = case.worker._manuscript_validate_evidence_support(
            claimed,
            {
                "use_rag": False,
                "research_search_plan": [plan(query)],
                "research_discoveries": [{"query": query, "candidates": [source]}],
            },
        ).context
        assert result["evidence_coverage"][0]["covered"] is True
        refreshed = result["research_discoveries"][0]["candidates"][0]
        assert refreshed["locator"] == "p. 55"
        assert refreshed["source_url"] == "https://example.org/version-0.pdf"
    assert len(downloads) == 1
    assert audits == ["first claim", "first claim", "different claim", "different claim"]


def test_pdf_reader_reports_execution_budget_instead_of_assuming_complete_read(monkeypatch):
    import fukamiru.literature.content as content

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def read(self, size):
            return b"%PDF-test"

    monkeypatch.setattr(content, "public_https", lambda url: True)
    monkeypatch.setattr(
        content.urllib.request,
        "build_opener",
        lambda *a: SimpleNamespace(open=lambda *a, **k: Response()),
    )
    monkeypatch.setattr(
        content,
        "PdfReader",
        lambda _: SimpleNamespace(
            is_encrypted=False, pages=[SimpleNamespace(extract_text=lambda: "本文" * 50)] * 55
        ),
    )
    candidate = SimpleNamespace(oa_url="https://example.org/P.pdf", oa_status="green")
    complete = read_oa_pdf(candidate, ["本文"], timeout=1, max_bytes=1024)
    assert complete["pages_examined"] == 55 and complete["read_complete"] is True
    partial = read_oa_pdf(candidate, ["本文"], timeout=1, max_bytes=1024, max_text_characters=150)
    assert partial["read_complete"] is False
    assert partial["stopped_reason"] == "text_size_budget"
    assert partial["pages_examined"] == 2 and partial["total_pages"] == 55


def test_lexical_context_citation_covers_actual_neighboring_spans(case):
    path = case.root / "spans.txt"
    path.write_text("導入の文章。\n\n料理漫画で会話する。\n\n結論の文章。", encoding="utf-8")
    with case.databases.knowledge.connect() as connection:
        IngestionService(connection, case.settings).ingest_path(str(path))
        [result] = KnowledgeSearchService(connection).lexical_search("料理漫画", limit=1)
        citation = result["citation"]
        spans = [
            connection.execute("SELECT text FROM source_spans WHERE id=?", (key,)).fetchone()[0]
            for key in citation["source_span_ids"]
        ]
    assert "\n\n".join(spans) == result["text"]
    assert citation["locator"] == "p. 1"
    assert result["source_span_id"] == citation["source_span_id"]


def test_semantic_group_keeps_interior_spans_and_exact_boundary_offsets(case):
    path = case.root / "group.txt"
    path.write_text("\n\n".join(f"段落{n}の本文。" for n in range(9)), encoding="utf-8")
    with case.databases.knowledge.connect() as connection:
        value = IngestionService(connection, case.settings).ingest_path(str(path))
        spans = connection.execute(
            "SELECT * FROM source_spans WHERE document_unit_id=? ORDER BY ordinal",
            (value["document_unit_id"],),
        ).fetchall()
        chunk = connection.execute(
            "SELECT id FROM chunks WHERE primary_source_span_id=?", (spans[0]["id"],)
        ).fetchone()[0]
        ranges = [
            (s["id"], 2 if i == 0 else 0, 3 if i == len(spans) - 1 else len(s["text"]))
            for i, s in enumerate(spans)
        ]
        text = "\n\n".join(
            s["text"][span_range[1] : span_range[2]]
            for s, span_range in zip(spans, ranges, strict=True)
        )
        raw = {
            "chunk_id": chunk, "text": text, "retrieval": "semantic", "score": 0.9,
            "source_span_ids": [s["id"] for s in spans], "source_ranges": ranges,
        }
        index = SimpleNamespace(available=lambda: True, search=lambda *a, **k: [raw])
        service = KnowledgeSearchService(connection, semantic_index=index)
        [result] = service._hydrate_results([raw])
        [hybrid] = [row for row in service.search("段落", limit=20) if row["chunk_id"] == chunk]
    assert result["text"] == text
    assert result["citation"]["source_span_ids"] == [s["id"] for s in spans]
    assert result["citation"]["start_char"] == spans[0]["start_char"] + 2
    assert result["citation"]["end_char"] == spans[-1]["start_char"] + 3
    assert hybrid["retrieval"] == "hybrid"
    assert hybrid["source_span_id"] == hybrid["citation"]["source_span_id"]
    assert hybrid["source_ranges"] == ranges
    assert hybrid["citation"] == result["citation"]


def test_unknown_work_never_matches_an_empty_local_identifier(case):
    with case.databases.knowledge.connect() as connection:
        value = IngestionService(connection, case.settings).ingest_path(str(case.source))
        connection.execute("UPDATE bibliographic_records SET isbn=''")
        assert not known_work_read(connection, {"doi": "10.9999/unrelated"})
        # A canonical asset is still readable if its optional bibliography is missing.
        connection.execute("DELETE FROM source_asset_bibliography")
        read = known_work_read(connection, {"source_asset_id": value["source_asset_id"]})
        assert read["spans"]


def test_cloned_run_uses_its_own_read_checkpoints(case, monkeypatch):
    from fukamiru.worker.runner import HandlerResult

    seen = []
    run_id = case._create_run(
        "manuscript_generator",
        extra_inputs={
            "note_text": "原稿",
            "authoring_run_id": "inherited-parent-run",
        },
    )

    def capture(claimed, context):
        seen.append(context["authoring_run_id"])
        return HandlerResult(context)

    monkeypatch.setitem(case.worker.handlers, "authoring.prepare_note", capture)
    assert case.worker.run_once(lane="ingestion_cpu")
    assert seen == [run_id]


def test_audit_reuse_requires_same_claim_context_source_and_model(case, monkeypatch):
    claimed = task(case)
    calls = []

    def invoke(task, method, **kwargs):
        calls.append(kwargs)
        return {
            "verdicts": [
                {"unit_id": "O", "status": "insufficient", "rationale": "未確認", "citations": []}
            ]
        }

    monkeypatch.setattr(case.worker, "_invoke_manuscript_inference", invoke)
    unit = {"unit_id": "O", "query": "対象の主張", "source_context": {"original": "原文"}}
    source = {**local_record(), "candidate_key": "knowledge_base:chunk"}
    case.worker._readonly_manuscript_audit(claimed, "evidence_support", [unit], [source])
    case.worker._readonly_manuscript_audit(claimed, "evidence_support", [unit], [source])
    assert len(calls) == 1
    changed = {**unit, "query": "異なる主張"}
    case.worker._readonly_manuscript_audit(claimed, "evidence_support", [changed], [source])
    changed = {**unit, "source_context": {"original": "異なる原文"}}
    case.worker._readonly_manuscript_audit(claimed, "evidence_support", [changed], [source])
    case.worker._readonly_manuscript_audit(
        claimed, "evidence_support", [unit], [{**source, "excerpt": "異なる出典本文"}]
    )
    case.worker.settings = replace(
        case.worker.settings,
        translation=replace(
            case.worker.settings.translation, manuscript_codex_model="another-model"
        ),
    )
    case.worker._readonly_manuscript_audit(claimed, "evidence_support", [unit], [source])
    assert len(calls) == 5


def test_planner_keeps_memo_title_and_concept_when_model_returns_generic_queries(case, monkeypatch):
    claimed = task(case)
    received = []

    def generate(task, **kwargs):
        received.extend(kwargs["payloads"])
        return [{"plans": [{"unit_id": "O", "subclaims": ["模型の一般論"],
                            "source_kind": "book", "variants": ["spatial arrangement"]}]}]

    monkeypatch.setattr(case.worker, "_authoring_parallel_generate", generate)
    paragraph = "『架空資料の比較分析』の「セノグラフィー」を雑誌に応用する。"
    result = case.worker._manuscript_plan_research_queries(claimed, {
        "research_query_assignments": [plan("配置の効果")], "note_text": paragraph,
        "propositions": [{"id": "p1", "text": "配置の効果", "source_span": paragraph}],
    }).context
    planned = result["research_search_plan"][0]
    assert planned["variants"][:2] == ["架空資料の比較分析", "セノグラフィー"]
    assert received[0]["units"][0]["source_context"]["original_note_paragraph"] == paragraph
    assert planned["subclaims"] == ["配置の効果"]


def test_reading_budget_survives_retry_and_never_verifies_on_exhaustion(case, monkeypatch):
    claimed = task(case)
    case.worker.settings = replace(case.worker.settings, translation=replace(
        case.worker.settings.translation, academic_research_reading_input_tokens=1))
    monkeypatch.setattr(case.worker, "_readonly_manuscript_audit",
                        lambda *a: pytest.fail("Budget must reject this audit"))
    monkeypatch.setattr(case.worker, "_save_manuscript_evidence", lambda *a: None)
    context = {"research_search_plan": [plan()],
               "research_discoveries": [{"query": plan()["query"],
                                         "candidates": [local_record()]}]}
    for _ in range(2):
        context = case.worker._manuscript_validate_evidence_support(claimed, context).context
        row = context["evidence_coverage"][0]
        assert row["covered"] is False
        assert row["support_status"] == "insufficient"
        assert row["reading_budget"]["stop_reason"] == "input_token_budget"
        assert context["research_reading_budget"]["allocations"] == {"O": 1}
        assert not context["evidence_context"]
    context["atomic_evidence_obligations"] = [{
        "obligation_id": "O", "proposition_id": "p1", "argument_necessity": "core",
        "verification_requirement": "external_required", "source_span": "未検証の外部事実",
        "source_origin": "memo",
    }]
    decision = case.worker._authoring_decide_evidence_realization(
        claimed, context).context["evidence_realization_decisions"][0]
    assert decision["may_assert_as_external_fact"] is False
    assert decision["realization"] == "omit"
    assert decision["research_stop_reason"] == "input_token_budget"
    clone = task(case)
    assert clone.run_id != claimed.run_id
    cloned = case.worker._manuscript_validate_evidence_support(clone, context).context
    assert cloned["research_reading_budget"]["run_id"] == clone.run_id


def test_named_work_reads_its_concept_not_a_commentary_title_hit(case, monkeypatch):
    book = case.root / "架空資料の比較分析.txt"
    commentary = case.root / "架空資料の比較分析をめぐる長い対談と読書記録.txt"
    book.write_text("索引：セノグラフィー。\n\n" + "前置き。" * 2000
                    + "\n\nセノグラフィーは配置を扱う。ただし雑誌への応用は著者の推論である。",
                    encoding="utf-8")
    commentary.write_text("セノグラフィーへの短い言及。", encoding="utf-8")
    with case.databases.knowledge.connect() as connection:
        ingestor = IngestionService(connection, case.settings)
        original = ingestor.ingest_path(str(book))
        ingestor.ingest_path(str(commentary))
        before = knowledge_revision(connection)
        hits = named_work_hits(connection, ["架空資料の比較分析"], ["セノグラフィー"])
        assert knowledge_revision(connection) == before
    assert len(hits) == 1
    assert hits[0]["source_asset_id"] == original["source_asset_id"]
    assert "ただし雑誌への応用は著者の推論である。" in hits[0]["text"]
    assert hits[0]["title_match"]["status"] == "candidate_unverified"
    assert hits[0]["citation"]["source_span_ids"]

    # Exercise the worker handoff too, with ordinary RAG retrieval deliberately empty.
    claimed = task(case)
    monkeypatch.setattr(case.worker, "_authoring_search_hits", lambda *a, **k: [])
    monkeypatch.setattr(case.worker, "_save_manuscript_evidence", lambda *a: None)
    quote = "セノグラフィーは配置を扱う。"

    def audit(task, phase, units, sources):
        assert quote in sources[0]["excerpt"]
        return {"verdicts": [{"unit_id": units[0]["unit_id"], "status": "supports_subclaim",
                              "rationale": "定義の記述を確認した。", "citations": [{
                                  "candidate_key": sources[0]["candidate_key"], "quote": quote,
                              }]}]}

    monkeypatch.setattr(case.worker, "_readonly_manuscript_audit", audit)
    target = {**plan("概念の定義"), "variants": ["セノグラフィー"], "source_context": {
        "original_note_paragraph": "『架空資料の比較分析』の「セノグラフィー」。",
    }}
    searched = case.worker._manuscript_rewrite_evidence(claimed, {
        "use_rag": True, "use_online_research": False,
        "research_queries": [target["query"]], "research_query_assignments": [target],
        "research_search_plan": [target],
    }).context
    assert searched["research_discoveries"][0]["candidates"][0]["provenance"][
        "retrieval"] == "named_work"
    checked = case.worker._manuscript_validate_evidence_support(claimed, searched).context
    assert checked["evidence_coverage"][0]["covered"] is True
    assert checked["research_reading_budget"]["sessions"]["O"]["stop_reason"] == "resolved"


def test_named_work_handles_catalogue_volume_series_and_isbn_without_merging_volumes(case):
    titles = [
        "芸術の規則〈1〉 （ブルデューライブラリー） 著者 394p_4894340097",
        "芸術の規則〈2〉 （ブルデューライブラリー） 著者 312p_4894340305",
        "人間の条件 （ちくま学芸文庫） 著者 549p_4480081569",
        "男同士の絆？ カール・レーフラー捏造事件からキリスト教綱要初版まで",
    ]
    assets = []
    with case.databases.knowledge.connect() as connection:
        service = IngestionService(connection, case.settings)
        for number, title in enumerate(titles):
            path = case.root / f"book-{number}.txt"
            path.write_text(f"資料{number}の本文。場の自律性、労働、仕事、活動。", encoding="utf-8")
            value = service.ingest_path(str(path))
            assets.append(value["source_asset_id"])
            connection.execute("UPDATE document_units SET title=? WHERE source_asset_id=?",
                               (title, value["source_asset_id"]))
        works = named_work_hits(connection, ["芸術の規則"], ["場 自律性"])
        assert {h["source_asset_id"] for h in works} == set(assets[:2])
        assert len({h["document_unit_id"] for h in works}) == 2
        assert all(h["title_match"]["status"] == "candidate_unverified" for h in works)
        [arendt] = named_work_hits(connection, ["人間の条件"], ["労働 仕事 活動"])
        assert arendt["source_asset_id"] == assets[2]
        assert not named_work_hits(connection, ["男同士の絆"], ["女性 媒介"])
