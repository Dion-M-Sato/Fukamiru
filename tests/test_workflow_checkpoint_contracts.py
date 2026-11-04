from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from fukamiru.revisions.certainty import hedge_interpretation
from fukamiru.revisions.enumeration import ManuscriptEnumerationService
from fukamiru.revisions.japanese_polish import (
    JapaneseCandidateGenerator,
    JapanesePolishTargetDetector,
)
from fukamiru.revisions.reasoning import ManuscriptReasoningRepository
from fukamiru.revisions.research_validation import search_all_providers, subclaim_units
from fukamiru.revisions.structure_gate import heading_edit_issue, heading_issues
from fukamiru.storage.payloads import PayloadStore, UnitCheckpoints
from fukamiru.storage.runs import RunRepository
from fukamiru.workflow.templates import (
    manuscript_rewrite_workflow,
    manuscript_rewrite_workflow_v19,
)


@pytest.fixture
def manuscript_case():
    from test_manuscript_rewrite import ManuscriptRewriteTests

    case = ManuscriptRewriteTests()
    case.setUp()
    run = RunRepository(case.connection).create(
        workflow_name="manuscript_rewrite",
        workflow_version="20",
        entry_node="prepare_rewrite_context",
        project_id=case.project["id"],
        inputs={},
    )
    case.run_id = run.run_id
    case.node_id = case.connection.execute(
        "SELECT id FROM node_runs WHERE run_id=?", (run.run_id,)
    ).fetchone()[0]
    try:
        yield case
    finally:
        case.tearDown()


def test_partial_generation_merge_reopen_and_atomic_save(manuscript_case):
    from test_japanese_polish import CapturingCandidates, target_fixture

    case = manuscript_case
    repo = ManuscriptReasoningRepository(case.connection)
    target = target_fixture()
    repo.replace_polish_targets(
        (target,),
        run_id=case.run_id,
        node_run_id=case.node_id,
        document_id=case.document_id,
        project_id=case.project["id"],
    )
    candidates = JapaneseCandidateGenerator(CapturingCandidates()).generate(target).candidates
    kwargs = dict(
        run_id=case.run_id,
        target_id=target.id,
        model_identity="S",
        expected_candidate_count=6,
        issues=(),
    )
    repo.save_polish_candidates(candidates[:3], **kwargs)
    assert repo.reserve_generation_attempt(case.run_id, target.id, "S", 2) == 1
    case.connection.close()
    case.connection = sqlite3.connect(case.root / "state.db")
    case.connection.row_factory = sqlite3.Row
    case.connection.execute("PRAGMA foreign_keys=ON")
    repo = ManuscriptReasoningRepository(case.connection)
    partial = repo.polish_candidates_for_run(case.run_id)[target.id]
    # A replenishment response starts numbering at 1 again, exactly as the failed Run.
    merged = (*partial, *(replace(c, ordinal=i + 1) for i, c in enumerate(candidates[3:])))
    repo.save_polish_candidates(merged, **kwargs)
    saved = repo.polish_candidates_for_run(case.run_id)[target.id]
    assert [c.ordinal for c in saved] == list(range(6))
    assert {c.id for c in saved} == {c.id for c in candidates}
    repo.save_polish_candidates((*saved, replace(saved[1], id="duplicate-text")), **kwargs)
    assert len(repo.polish_candidates_for_run(case.run_id)[target.id]) == 6
    assert repo.reserve_generation_attempt(case.run_id, target.id, "S", 2) == 2
    assert repo.reserve_generation_attempt(case.run_id, target.id, "S", 2) is None
    case.connection.execute(
        "CREATE TRIGGER injected_failure BEFORE INSERT ON manuscript_polish_candidates "
        "BEGIN SELECT RAISE(ABORT, 'injected'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        repo.save_polish_candidates(candidates[:2], **kwargs)
    assert len(repo.polish_candidates_for_run(case.run_id)[target.id]) == 6


def test_content_addressed_context_and_missing_reference_fail_closed(manuscript_case):
    case = manuscript_case
    store = PayloadStore(case.connection)
    context = {"document_id": "D", "proposed_html": "本文" * 10000, "inventory": ["資料" * 9000]}
    packed = store.pack(context)
    assert len(json.dumps(packed)) < 400
    assert store.unpack(packed) == context
    assert store.pack(context) == packed
    assert case.connection.execute("SELECT COUNT(*) FROM workflow_payloads").fetchone()[0] == 2
    case.connection.execute("DELETE FROM workflow_payloads")
    with pytest.raises(ValueError, match="Missing or corrupt"):
        store.unpack(packed)


def test_checkpoints_do_not_cross_changed_source_or_run(manuscript_case):
    case = manuscript_case
    repo = UnitCheckpoints(case.connection)
    repo.save(case.run_id, "enumeration", "sourceA", {"C1": {"action": "preserve"}})
    reopened = UnitCheckpoints(case.connection)
    assert "C1" in reopened.load(case.run_id, "enumeration", "sourceA")
    assert reopened.load(case.run_id, "enumeration", "sourceB") == {}
    assert reopened.load("different-run", "enumeration", "sourceA") == {}


def test_all_providers_start_even_if_one_fails():
    barrier = threading.Barrier(3)

    def search(index, query, limit):
        barrier.wait(timeout=2)
        if index == 0:
            raise RuntimeError("429 quota")
        return [f"source-{index}"]

    providers = tuple(
        SimpleNamespace(name=str(i), search=lambda q, limit, i=i: search(i, q, limit))
        for i in range(3)
    )
    results = search_all_providers(providers, ["query"], 1)
    assert "429" in results[0]["error"]
    assert results[1]["candidates"] == ["source-1"]
    assert results[2]["candidates"] == ["source-2"]


def test_two_item_lists_without_syntactic_fragment_false_positives():
    html = (
        '<article id="D"><section id="S"><p id="P">'
        "編集部、書店が作品を選別する。作品を単一の時代精神の表現として括るものでも、"
        "漫画史の通史を提示するものでもない。『作品A』『作品B』を比較する。"
        "</p></section></article>"
    )
    candidates = ManuscriptEnumerationService.inventory(html)
    assert [c["items"] for c in candidates] == [["編集部", "書店"], ["『作品A』", "『作品B』"]]


def test_heading_gate_leaves_long_paragraphs_unrestricted():
    assert heading_edit_issue("### 幕間", "### 幕間\n\n本文。", "p") == "heading.body_contamination"
    assert heading_edit_issue("### 第16節", "## 第16節", "p") == "heading.level_changed"
    assert heading_edit_issue("普通の本文。", "論述。" * 1000, "p") is None
    assert heading_issues(
        '<article><h3 id="H">章題</h3></article>', '<article><h2 id="H">章題</h2></article>'
    )


def test_phrase_offsets_cannot_give_model_the_surrounding_sentence():
    text = "一九八〇年の『作品』では、羞恥が主体の回復に接続されるのだろう。"
    html = f'<article id="D"><section id="S"><p id="P">{text}</p></section></article>'
    targets = JapanesePolishTargetDetector.detect(html, policy_version=2)
    assert targets
    for target in targets:
        assert text[target.start_offset : target.end_offset] == target.source_text
        assert "scope.phrase_only" in target.issue_codes
        assert "一九八〇年" not in target.source_text
        assert "だろう" not in target.source_text
        assert "。" not in target.source_text


def test_certainty_calibration_idempotent_and_keeps_negation_and_tense():
    assert hedge_interpretation("これは解釈である。") == "これは解釈であろう。"
    assert hedge_interpretation("主体は回復した。") == "主体は回復したと考えられる。"
    assert hedge_interpretation("主体は回復しない。") == "主体は回復しないと考えられる。"
    for text in ["これは解釈であろう。", "回復したと考えられる。", "可能性がある。"]:
        assert hedge_interpretation(text) == text


def test_subclaim_identity_and_graph_have_no_repair_back_edge():
    plan = {"query": "AとBの因果", "obligation_id": "O", "subclaims": ["A", "B"]}
    units = subclaim_units(plan)
    assert len({u["unit_id"] for u in units}) == 2
    assert subclaim_units({**plan, "subclaims": ["B", "A"]}) == list(reversed(units))
    graph = manuscript_rewrite_workflow()
    assert manuscript_rewrite_workflow_v19().version == "19"
    assert graph.next_nodes("draft_rewrite", "success") == ["calibrate_manuscript_certainty"]
    assert graph.next_nodes("calibrate_manuscript_certainty", "success") == [
        "validate_rewrite_coherence"
    ]
    visited = set()
    active = set()

    def visit(node):
        assert node not in active
        if node in visited:
            return
        active.add(node)
        for edge in graph.edges:
            if edge.source == node:
                visit(edge.target)
        active.remove(node)
        visited.add(node)

    visit(graph.entry_node)


def test_evidence_handler_preserves_partial_support_and_deduplicates_counts(monkeypatch):
    from test_worker import WorkerTests

    case = WorkerTests()
    case.setUp()
    try:
        worker = case.worker
        monkeypatch.setattr(worker, "_record_manuscript_progress", lambda *a, **kw: None)

        def run(claimed, items, **kw):
            for index, item in enumerate(items):
                result = kw["operation"](index, item)
                kw["consume"](index, item, result)

        monkeypatch.setattr(worker, "_run_ordered_parallel", run)
        collected = []
        monkeypatch.setattr(
            worker, "_save_manuscript_evidence", lambda c, ctx, r: collected.append(r)
        )
        monkeypatch.setattr(ManuscriptReasoningRepository, "evidence_for_run", lambda *a: collected)
        synthesis = "insufficient"

        def audit(claimed, phase, units, sources):
            return {
                "verdicts": [
                    {
                        "unit_id": u["unit_id"],
                        "status": synthesis if u["unit_id"] == "O" else "supports_subclaim",
                        "rationale": "Supplied evidence supports this claim only.",
                        "citations": [
                            {
                                "candidate_key": "knowledge_base:1",
                                "quote": "制度は参加条件を定める。",
                            }
                        ],
                    }
                    for u in units
                ]
            }

        monkeypatch.setattr(worker, "_readonly_manuscript_audit", audit)
        plan = {
            "obligation_id": "O",
            "query": "制度と参加の因果関係",
            "subclaims": ["制度", "参加"],
            "requires_research": True,
        }
        context = {
            "research_search_plan": [plan],
            "research_discoveries": [
                {
                    "query": plan["query"],
                    "candidates": [
                        {
                            "provider": "knowledge_base",
                            "provider_record_id": "1",
                            "query": plan["query"],
                            "excerpt": "制度は参加条件を定める。",
                            "title": "制度論",
                            "provenance": {},
                        }
                    ],
                }
            ],
        }
        output = worker._manuscript_validate_evidence_support(
            SimpleNamespace(run_id="R"), context
        ).context
        assert output["evidence_coverage"][0]["partially_covered"]
        assert not output["evidence_coverage"][0]["covered"]
        assert output["evidence_coverage"][0]["accepted_evidence_count"] == 1
        assert len(output["evidence_context"]) == 1
        collected.clear()
        synthesis = "supports_subclaim"
        output = worker._manuscript_validate_evidence_support(
            SimpleNamespace(run_id="R"), context
        ).context
        assert output["evidence_coverage"][0]["covered"]
        assert output["evidence_coverage"][0]["accepted_evidence_count"] == 1
    finally:
        case.tearDown()


def test_certainty_handler_only_changes_tentative_interpretations(monkeypatch):
    from test_worker import WorkerTests

    case = WorkerTests()
    case.setUp()
    try:
        worker = case.worker
        monkeypatch.setattr(worker, "_record_manuscript_progress", lambda *a, **kw: None)
        monkeypatch.setattr(worker, "_reserve_codex_subslots", lambda *a, **kw: 0)

        def run(claimed, items, **kw):
            for index, item in enumerate(items):
                kw["consume"](index, item, kw["operation"](index, item))

        monkeypatch.setattr(worker, "_run_ordered_parallel", run)

        def audit(claimed, phase, units, sources, **kw):
            return {
                "verdicts": [
                    {
                        "unit_id": u["unit_id"],
                        "status": (
                            "tentative_interpretation"
                            if "解釈" in u["sentence"]
                            else "fact_needs_evidence"
                        ),
                        "rationale": "Evidence status fixture",
                        "citations": [{"candidate_key": u["unit_id"], "quote": u["sentence"]}],
                    }
                    for u in units
                ]
            }

        monkeypatch.setattr(worker, "_readonly_manuscript_audit", audit)
        html = (
            '<article id="D" lang="ja"><section id="S"><h2 id="H">章題</h2>'
            '<p id="P">これは解釈である。'
            "一九八〇年に創刊された。</p></section></article>"
        )
        result = worker._manuscript_calibrate_certainty(
            SimpleNamespace(run_id="R", workflow_version="20"), {"proposed_html": html}
        ).context
        assert "これは解釈であろう。" in result["proposed_html"]
        assert "一九八〇年に創刊された。" in result["proposed_html"]
        assert any("fact_needs_evidence" in q for q in result["unresolved_questions"])
        assert result["certainty_calibration"]["changes"][0]["applied"]
        replay = worker._manuscript_calibrate_certainty(
            SimpleNamespace(run_id="R", workflow_version="20"), result
        ).context
        assert replay["proposed_html"] == result["proposed_html"]
        assert replay["unresolved_questions"] == result["unresolved_questions"]
    finally:
        case.tearDown()


def test_enumeration_partial_checkpoint_replays_only_unresolved_with_persistent_budget(monkeypatch):
    from test_worker import WorkerTests

    from fukamiru.revisions.rewrite import ManuscriptRewriteService

    case = WorkerTests()
    case.setUp()
    try:
        with case.databases.state.connect() as connection:
            run_id = (
                RunRepository(connection)
                .create(
                    workflow_name="manuscript_rewrite",
                    workflow_version="20",
                    entry_node="resolve_enumeration_context",
                    project_id=None,
                    inputs={},
                )
                .run_id
            )
        manuscript = (
            '<article id="D" lang="ja"><section id="S"><h2 id="H">章題</h2>'
            '<p id="P">編集部、書店が選ぶ。図書館、読者が選ぶ。</p></section></article>'
        )
        monkeypatch.setattr(
            ManuscriptRewriteService, "prepare", lambda *a, **kw: {"base_html": manuscript}
        )
        worker = case.worker
        monkeypatch.setattr(worker, "_record_manuscript_progress", lambda *a, **kw: None)
        monkeypatch.setattr(worker, "_reserve_codex_subslots", lambda *a, **kw: 0)

        def run(claimed, items, **kw):
            for index, item in enumerate(items):
                result = kw["operation"](index, item)
                kw["on_completed"](index, item, result)
                kw["consume"](index, item, result)

        monkeypatch.setattr(worker, "_run_ordered_parallel", run)
        calls = []

        def resolve(**kw):
            calls.append([c["candidate_id"] for c in kw["candidates"]])
            if len(calls) > 1:
                return {"contracts": []}
            c = kw["candidates"][0]
            return {
                "contracts": [
                    {
                        "candidate_id": c["candidate_id"],
                        "block_id": c["block_id"],
                        "action": "preserve",
                        "item_roles": [{"item": i} for i in c["items"]],
                    }
                ]
            }

        worker.translator = SimpleNamespace(resolve_manuscript_enumeration_context=resolve)
        context = {
            "document_id": "D",
            "base_revision_id": "R",
            "document_title": "Title",
            "instructions": "Review",
        }
        claimed = SimpleNamespace(run_id=run_id)
        first = worker._manuscript_resolve_enumeration_context(claimed, context).context
        second = worker._manuscript_resolve_enumeration_context(claimed, context).context
        assert len(calls) == 2
        assert len(calls[0]) == 2 and len(calls[1]) == 1
        assert calls[1][0] != calls[0][0]
        assert first["enumeration_context_contracts"] == second["enumeration_context_contracts"]
        assert len(second["enumeration_unresolved_questions"]) == 1
    finally:
        case.tearDown()
