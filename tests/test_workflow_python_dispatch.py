from __future__ import annotations

from dataclasses import replace

import pytest

from fukamiru.errors import FukamiruError
from fukamiru.revisions.research_validation import requires_semantic_query_planning


@pytest.fixture
def case():
    from test_worker import WorkerTests

    value = WorkerTests()
    value.setUp()
    yield value
    value.tearDown()


def claimed(case, node_id):
    run_id = case._create_run("manuscript_rewrite")
    task = case.worker._claim(lane="ingestion_cpu")
    assert task is not None and task.run_id == run_id
    return replace(
        task, node_id=node_id, workflow_version="25", resource_lane="ingestion_cpu"
    )


def manuscript():
    return (
        '<article id="D"><section id="S"><h2 id="H">題名</h2>'
        '<p id="P1">明確な文。</p><p id="P2">曖昧な文。</p>'
        "</section></article>"
    )


def test_simple_queries_use_python_and_only_compound_queries_call_codex(case, monkeypatch):
    task = claimed(case, "plan_research_queries")
    calls = []

    def audit(_task, phase, units, sources):
        calls.append((phase, units, sources))
        return {
            "plans": [
                {
                    "unit_id": unit["unit_id"],
                    "variants": [unit["query"]],
                    "subclaims": ["思想", "権力"],
                    "source_kind": "mixed",
                }
                for unit in units
            ]
        }

    monkeypatch.setattr(case.worker, "_readonly_manuscript_audit", audit)
    result = case.worker._manuscript_plan_research_queries(
        task,
        {
            "research_query_assignments": [
                {"query": "『思想』 出版年"},
                {"query": "思想と権力の関係"},
            ]
        },
    )
    plans = result.context["research_search_plan"]
    assert plans[0]["planning_status"] == "python_planned"
    assert plans[0]["subclaims"] == ["『思想』 出版年"]
    assert plans[0]["variants"] == ["思想", "『思想』 出版年"]
    assert plans[1]["planning_status"] == "planned"
    assert plans[1]["subclaims"] == ["思想", "権力"]
    assert len(calls) == 1
    assert [unit["query"] for unit in calls[0][1]] == ["思想と権力の関係"]


def test_all_simple_queries_never_start_a_codex_audit(case, monkeypatch):
    task = claimed(case, "plan_research_queries")
    monkeypatch.setattr(
        case.worker,
        "_readonly_manuscript_audit",
        lambda *_: pytest.fail("Simple queries must stay in Python"),
    )
    result = case.worker._manuscript_plan_research_queries(
        task, {"research_query_assignments": [{"query": "『思想』 出版年"}]}
    )
    assert result.context["research_search_plan"][0]["planning_status"] == "python_planned"
    assert requires_semantic_query_planning("思想と権力の関係")
    assert not requires_semantic_query_planning("『思想』 出版年")


def test_authoring_claim_subclaims_do_not_disable_retrieval_query_planning(
    case, monkeypatch
):
    task = replace(
        claimed(case, "plan_research_queries"),
        workflow_name="authoring",
        workflow_version="30",
    )
    calls = []

    def generate(stage, payload, context=""):
        calls.append((stage, payload, context))
        return {
            "plans": [
                {
                    "unit_id": unit["unit_id"],
                    "variants": ["1980年代 ヤング誌 編成史", "青年漫画 雑誌史"],
                    "subclaims": ["planner must not replace locked claim text"],
                    "source_kind": "mixed",
                }
                for unit in payload["units"]
            ]
        }

    monkeypatch.setattr(case.worker.authoring_adapter, "generate", generate)
    monkeypatch.setattr(
        case.worker,
        "_readonly_manuscript_audit",
        lambda *_: pytest.fail("Authoring query planning must use Luna without reasoning"),
    )
    locked_subclaims = ["青年誌の編成が作品の隣接関係を変えた。"]
    result = case.worker._manuscript_plan_research_queries(
        task,
        {
            "research_query_assignments": [
                {
                    "query": "青年誌の編成が作品の隣接関係を変えたという歴史的主張を検証する",
                    "subclaims": locked_subclaims,
                }
            ]
        },
    )

    [plan] = result.context["research_search_plan"]
    assert calls and calls[0][0] == "plan_research_queries"
    assert plan["planning_status"] == "planned"
    assert plan["variants"] == ["1980年代 ヤング誌 編成史", "青年漫画 雑誌史"]
    assert plan["subclaims"] == locked_subclaims
    assert calls[0][1]["output_language"] == "ja"


def test_unambiguous_japanese_analysis_stays_local_without_engine_binding(case, monkeypatch):
    task = claimed(case, "analyze_japanese_structure")
    monkeypatch.setattr(
        "fukamiru.worker.runner.parse_japanese",
        lambda _text: {"status": "analyzed", "ambiguities": [], "tokens": []},
    )
    monkeypatch.setattr(
        case.worker,
        "_manuscript_inference_backend",
        lambda *_: pytest.fail("Unambiguous text must not bind Codex"),
    )
    monkeypatch.setattr(
        case.worker,
        "_register_language_engine_v22",
        lambda *_, **__: pytest.fail("Unambiguous text must not register an engine"),
    )
    result = case.worker._manuscript_analyze_japanese(
        task, {"proposed_html": manuscript()}
    )
    analyses = result.context["japanese_language_analysis"]
    assert set(analyses) == {"H", "P1", "P2"}
    assert all(item["decision"] == "analysis_ready" for item in analyses.values())
    assert all(
        item["analysis_provenance"]["decision_source"] == "local_analyzers"
        and "inference_engine_fingerprint" not in item
        for item in analyses.values()
    )


def test_only_ambiguous_japanese_analysis_uses_engine_bound_adjudication(case, monkeypatch):
    task = claimed(case, "analyze_japanese_structure")
    calls = []

    def parse(text):
        ambiguity = ["implicit_subject"] if "曖昧" in text else []
        return {"status": "unresolved" if ambiguity else "analyzed",
                "ambiguities": ambiguity, "tokens": [], "ambiguity_spans": []}

    monkeypatch.setattr("fukamiru.worker.runner.parse_japanese", parse)
    monkeypatch.setattr("fukamiru.revisions.japanese_analysis.parse_japanese", parse)
    monkeypatch.setattr(
        case.worker,
        "_manuscript_inference_backend",
        lambda *_: calls.append("backend") or object(),
    )
    monkeypatch.setattr(
        case.worker,
        "_register_language_engine_v22",
        lambda *_, **__: calls.append("engine") or {"fingerprint": "f" * 64},
    )
    monkeypatch.setattr(
        case.worker,
        "_invoke_manuscript_inference",
        lambda _task, method, **_: calls.append(method)
        or {"status": "resolved", "explanation": "主語を確認", "roles": [{"subject": "本文"}]},
    )
    result = case.worker._manuscript_analyze_japanese(
        task, {"proposed_html": manuscript()}
    )
    analyses = result.context["japanese_language_analysis"]
    assert calls == ["backend", "engine", "resolve_japanese_ambiguity"]
    assert analyses["P2"]["inference_engine_fingerprint"] == "f" * 64
    assert "inference_engine_fingerprint" not in analyses["P1"]
    assert all(item["status"] == "analyzed" for item in analyses.values())


def test_unavailable_japanese_parser_fails_before_codex_binding(case, monkeypatch):
    task = claimed(case, "analyze_japanese_structure")
    monkeypatch.setattr(
        "fukamiru.worker.runner.parse_japanese",
        lambda _text: {
            "status": "unresolved",
            "ambiguities": ["analyzer_unavailable:ImportError"],
        },
    )
    monkeypatch.setattr(
        case.worker,
        "_manuscript_inference_backend",
        lambda *_: pytest.fail("A missing parser must not trigger Codex"),
    )
    with pytest.raises(FukamiruError, match="analysis is unavailable"):
        case.worker._manuscript_analyze_japanese(
            task, {"proposed_html": manuscript()}
        )
