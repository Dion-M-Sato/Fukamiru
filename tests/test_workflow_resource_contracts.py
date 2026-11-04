from __future__ import annotations

import copy
import io
import json
import sqlite3
import urllib.error
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from test_japanese_polish import CapturingCandidates, target_fixture

from fukamiru.literature.service import OpenAlexProvider
from fukamiru.literature.transport import ProviderRequestError, _budgets
from fukamiru.revisions.discourse import ManuscriptDiscourseService
from fukamiru.revisions.japanese_polish import JapaneseCandidateGenerator
from fukamiru.revisions.reasoning import ManuscriptReasoningRepository
from fukamiru.revisions.references import JapaneseReferenceService
from fukamiru.revisions.research_validation import grounded_support, plan_queries
from fukamiru.storage.runs import RunRepository
from fukamiru.workflow.templates import (
    manuscript_rewrite_workflow_v18,
    manuscript_rewrite_workflow_v19,
)


@pytest.mark.parametrize("mutation", ["extra", "number", "long", "relation", "count"])
def test_host_contract_rejects_sampler_escape(mutation):
    target = target_fixture()
    payload = CapturingCandidates().complete_json()
    if mutation == "extra":
        payload["candidates"][0]["confidence"] = 1
    elif mutation == "number":
        payload["candidates"][0]["text"] = 123
    elif mutation == "long":
        payload["candidates"][0]["text"] = "字" * 2001
    elif mutation == "relation":
        payload["candidates"][0]["relation_type"] = "invented"
    else:
        payload["candidates"].clear()
    assert not JapaneseCandidateGenerator.validate_payload(payload, target, 5)


def test_sampling_grammar_and_remaining_candidate_contract():
    adapter = CapturingCandidates()
    target = target_fixture()
    result = JapaneseCandidateGenerator(adapter).generate(target)
    assert len(result.candidates) == 6
    assert "maxLength" not in json.dumps(adapter.call["json_schema"])
    assert JapaneseCandidateGenerator.validate_payload(
        {"candidates": [CapturingCandidates().complete_json()["candidates"][0]]}, target, 1
    )


def test_generation_attempt_budget_survives_repository_reopen():
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE manuscript_generation_attempts (run_id,target_id,signature,"
        "attempt,status DEFAULT 'started',diagnostic,started_at,completed_at)"
    )
    repository = ManuscriptReasoningRepository(connection)
    assert repository.reserve_generation_attempt("R", "T", "S", 2) == 1
    repository.finish_generation_attempt("R", "T", "S", 1, "incomplete")
    repository = ManuscriptReasoningRepository(connection)
    assert repository.reserve_generation_attempt("R", "T", "S", 2) == 2
    assert repository.reserve_generation_attempt("R", "T", "S", 2) is None
    assert repository.reserve_generation_attempt("R", "T", "new-contract", 2) == 1


def test_reference_subtitle_and_chapter_are_not_confused():
    assert (
        JapaneseReferenceService._title_score("行為と演技", "行為と演技 : 日常生活における自己呈示")
        == 1
    )
    assert JapaneseReferenceService._type_score("book_or_journal", "book-chapter") == 0
    assert JapaneseReferenceService._type_score("article_poem_or_chapter", "book-chapter") == 1


def test_headings_not_forced_into_predicate_contract():
    manuscript = (
        '<article id="D"><section id="S"><p id="H">## Ⅳ　労働・ケアの可視性</p>'
        '<p id="P">制度が読者の回復に寄与する。</p></section></article>'
    )
    candidates = ManuscriptDiscourseService.predicate_candidates(manuscript)
    assert "H" not in {c["block_id"] for c in candidates}
    assert "P" in {c["block_id"] for c in candidates}


def test_search_obligations_survive_shared_query():
    records = plan_queries(
        [
            {"query": "『作品』の制度", "finding_id": "F1"},
            {"query": "『作品』の制度", "finding_id": "F2"},
        ]
    )
    assert records[0]["obligation_id"] != records[1]["obligation_id"]
    assert records[0]["variants"][0] == "作品"


def test_authoring_atomic_obligation_id_survives_query_planning():
    [record] = plan_queries(
        [
            {
                "query": "1998年 制度A 導入",
                "finding_id": "evidence:abc",
                "obligation_id": "evidence:abc",
                "origin": "authoring_atomic_proposition",
            }
        ]
    )

    assert record["obligation_id"] == "evidence:abc"
    assert record["policy"] == "authoring-atomic-evidence-v1"


def test_support_requires_actual_source_quote():
    source = [{"candidate_key": "KB:1", "excerpt": "制度は参加の条件を定める。"}]
    verdict = {
        "status": "inference_bridge",
        "rationale": "条件の推論",
        "citations": [{"candidate_key": "KB:1", "quote": "制度は参加の条件を定める。"}],
    }
    assert grounded_support(verdict, source)
    bad = copy.deepcopy(verdict)
    bad["citations"][0]["quote"] = "引用されていない説明"
    assert not grounded_support(bad, source)
    assert not grounded_support({**verdict, "citations": []}, source)


def test_provider_429_recovery_and_shared_cooldown():
    _budgets.clear()
    response = MagicMock()
    response.__enter__.return_value.read.return_value = b'{"results": []}'
    response.__enter__.return_value.headers = {}
    error = urllib.error.HTTPError(
        "https://api.openalex.org/works", 429, "limited", {"Retry-After": "0"}, io.BytesIO()
    )
    with (
        patch("urllib.request.urlopen", side_effect=[error, response]) as request,
        patch("fukamiru.literature.transport.time.sleep"),
    ):
        provider = OpenAlexProvider()
        assert provider.search("test", limit=1) == []
        assert request.call_count == 2
        assert provider.request_outcomes[0]["outcome"] == "rate_limited"
    _budgets.clear()
    error = urllib.error.HTTPError(
        "https://api.openalex.org/works", 429, "limited", {"Retry-After": "3600"}, io.BytesIO()
    )
    with patch("urllib.request.urlopen", side_effect=error) as request:
        with pytest.raises(ProviderRequestError):
            OpenAlexProvider().search("first", limit=1)
        with pytest.raises(ProviderRequestError):
            OpenAlexProvider().search("second", limit=1)
        assert request.call_count == 1
    _budgets.clear()


def test_v18_preserved_and_v19_has_no_back_edge():
    assert manuscript_rewrite_workflow_v18().version == "18"
    graph = manuscript_rewrite_workflow_v19()
    assert graph.next_nodes("validate_rewrite_coherence", "success") == [
        "resolve_manuscript_predicate_relations"
    ]
    assert graph.next_nodes("resolve_manuscript_predicate_relations", "success") == [
        "detect_japanese_polish_targets"
    ]
    assert graph.next_nodes("validate_final_coherence", "success") == ["persist_rewrite_proposal"]


def test_readonly_audit_repairs_only_missing_units_and_reuses_valid_cache():
    from test_worker import WorkerTests

    case = WorkerTests()
    case.setUp()
    try:
        with case.databases.state.connect() as connection:
            run = RunRepository(connection).create(
                workflow_name="manuscript_rewrite",
                workflow_version="19",
                entry_node="plan_research_queries",
                project_id=None,
                inputs={},
            )
        calls = []

        def audit(**kwargs):
            units = kwargs["units"]
            calls.append([u["unit_id"] for u in units])
            return {
                "verdicts": [
                    {
                        "unit_id": units[0]["unit_id"],
                        "status": "resolved",
                        "rationale": "Current text states the relation.",
                        "citations": [
                            {"candidate_key": "P", "quote": "制度は参加の条件を定める。"}
                        ],
                    }
                ]
            }

        case.worker.translator = SimpleNamespace(audit_manuscript_records=audit)
        claimed = SimpleNamespace(run_id=run.run_id)
        units = [{"unit_id": "A"}, {"unit_id": "B"}]
        sources = [{"candidate_key": "P", "excerpt": "制度は参加の条件を定める。"}]
        result = case.worker._readonly_manuscript_audit(claimed, "final_findings", units, sources)
        assert calls == [["A", "B"], ["B"]]
        assert len(result["verdicts"]) == 2
        assert (
            case.worker._readonly_manuscript_audit(claimed, "final_findings", units, sources)
            == result
        )
        assert len(calls) == 2
    finally:
        case.tearDown()
