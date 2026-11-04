from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

import pytest
from test_workflow_inference_availability import authorize_engine_transition

from fukamiru.errors import (
    ExternalServiceUnavailable,
    FukamiruError,
    NonRetryableFukamiruError,
)
from fukamiru.revisions import japanese_analysis
from fukamiru.revisions.japanese_analysis import cached_analysis_v22

HTML = (
    '<article id="D" lang="ja"><section id="S"><h2 id="H">本文</h2>'
    '<p id="P1">第一の段落である。</p><p id="P2">第二の段落である。</p>'
    '<p id="P3">第三の段落である。</p></section></article>'
)


def record(block_id: str, status: str = "no_revision_needed") -> dict[str, str]:
    return {
        "block_id": block_id,
        "kind": "block_review",
        "status": status,
        "explanation": "本文を確認した。",
    }


class ScriptedLanguageResolver:
    def __init__(self, script):
        self.script = list(script)
        self.calls: list[list[str]] = []

    def plan_manuscript_language(self, *, blocks, **_):
        self.calls.append([str(block["id"]) for block in blocks])
        action = self.script.pop(0)
        if isinstance(action, BaseException):
            raise action
        return action(blocks) if callable(action) else action


@pytest.fixture
def worker_case():
    from test_worker import WorkerTests

    case = WorkerTests()
    case.setUp()
    try:
        yield case
    finally:
        case.tearDown()


def claimed_v22(case):
    run_id = case._create_run("manuscript_rewrite")
    claimed = case.worker._claim(lane="ingestion_cpu")
    assert claimed is not None and claimed.run_id == run_id
    return replace(
        claimed,
        node_id="plan_term_introductions",
        workflow_version="22",
        resource_lane="codex_remote",
    )


def checkpoint_rows(case, run_id: str):
    with case.databases.state.connect() as connection:
        return connection.execute(
            "SELECT phase,unit_id,payload_json FROM manuscript_unit_checkpoints "
            "WHERE run_id=? AND phase != 'manuscript-v22-engine' ORDER BY phase,unit_id",
            (run_id,),
        ).fetchall()


def test_v22_transport_failure_escapes_without_content_checkpoint(worker_case):
    claimed = claimed_v22(worker_case)
    resolver = ScriptedLanguageResolver([ExternalServiceUnavailable("capacity")])
    worker_case.worker.translator = resolver

    with pytest.raises(ExternalServiceUnavailable):
        worker_case.worker._language_plan(claimed, {"proposed_html": HTML}, "plan")

    rows = checkpoint_rows(worker_case, claimed.run_id)
    assert [row["phase"] for row in rows] == ["language-v22-engine:plan"]
    assert all("semantic_analysis_unresolved" not in row["payload_json"] for row in rows)


def test_v22_missing_mandatory_resolver_waits_instead_of_becoming_content(worker_case):
    claimed = claimed_v22(worker_case)
    worker_case.worker.translator = object()

    with pytest.raises(ExternalServiceUnavailable, match="required but unavailable"):
        worker_case.worker._language_plan(claimed, {"proposed_html": HTML}, "plan")

    rows = checkpoint_rows(worker_case, claimed.run_id)
    # No unavailable result becomes language content or a phase-level engine
    # record. The separate Run-wide ledger still protects the desired adapter.
    assert rows == []
    with worker_case.databases.state.connect() as connection:
        binding = connection.execute(
            "SELECT payload_json FROM manuscript_unit_checkpoints "
            "WHERE run_id=? AND phase='manuscript-v22-engine'",
            (claimed.run_id,),
        ).fetchone()
    desired = json.loads(binding["payload_json"])
    assert desired["binding_kind"] == "desired_only"
    assert desired["runtime_engine_fingerprint"] is None


def test_v22_transport_failure_reaches_worker_wait_state(worker_case):
    run_id = worker_case._create_run("manuscript_rewrite")
    resolver = ScriptedLanguageResolver([ExternalServiceUnavailable("capacity")])
    worker_case.worker.translator = resolver
    with worker_case.databases.state.connect() as connection:
        connection.execute(
            "UPDATE node_runs SET node_id='plan_term_introductions', "
            "resource_lane='codex_remote', input_json=? WHERE run_id=?",
            (json.dumps({"proposed_html": HTML}), run_id),
        )
        connection.commit()

    assert worker_case.worker.run_once(lane="codex_remote")
    with worker_case.databases.state.connect() as connection:
        node = connection.execute(
            "SELECT status,input_json FROM node_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        run = connection.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
    assert node["status"] == "queued"
    assert run["status"] == "queued"
    assert json.loads(node["input_json"])["_external_retry"]["count"] == 1
    rows = checkpoint_rows(worker_case, run_id)
    assert [row["phase"] for row in rows] == ["language-v22-engine:plan"]


def test_v21_language_transport_semantics_are_unchanged(worker_case):
    claimed = replace(claimed_v22(worker_case), workflow_version="21")
    resolver = ScriptedLanguageResolver([ExternalServiceUnavailable("capacity")])
    worker_case.worker.translator = resolver

    records = worker_case.worker._language_plan(
        claimed, {"proposed_html": HTML}, "plan"
    )

    assert len(records) == 3
    assert {item["status"] for item in records} == {"semantic_analysis_unresolved"}
    assert {item["explanation"] for item in records} == {"ExternalServiceUnavailable"}
    rows = checkpoint_rows(worker_case, claimed.run_id)
    assert {row["phase"] for row in rows} == {"language:plan"}


def test_v22_keeps_success_and_retries_only_missing_blocks_after_transport_wait(worker_case):
    claimed = claimed_v22(worker_case)
    resolver = ScriptedLanguageResolver(
        [
            {"records": [record("P1")]},
            ExternalServiceUnavailable("capacity"),
        ]
    )
    worker_case.worker.translator = resolver

    with pytest.raises(ExternalServiceUnavailable):
        worker_case.worker._language_plan(claimed, {"proposed_html": HTML}, "plan")
    rows = checkpoint_rows(worker_case, claimed.run_id)
    assert any(row["unit_id"] == "result:P1" for row in rows)
    assert any(row["unit_id"] == "partial:P2" for row in rows)
    assert any(row["unit_id"] == "partial:P3" for row in rows)

    resolver.script = [
        {"records": [record("P2"), record("P3", "semantic_analysis_unresolved")]}
    ]
    resolver.calls.clear()
    records = worker_case.worker._language_plan(
        claimed, {"proposed_html": HTML}, "plan"
    )
    assert resolver.calls == [["P2", "P3"]]
    assert {item["block_id"] for item in records} == {"P1", "P2", "P3"}
    assert next(item for item in records if item["block_id"] == "P3")["status"] == (
        "semantic_analysis_unresolved"
    )


def test_v22_preserves_valid_sibling_record_while_repairing_same_block(worker_case):
    claimed = claimed_v22(worker_case)
    resolver = ScriptedLanguageResolver([
        {
            "records": [
                record("P1"),
                {
                    "block_id": "P1",
                    "kind": "contextual_lexicon",
                    "status": "resolved",
                    "term": "短形式",
                    "explanation": "解析した。",
                },
                record("P2"),
                record("P3"),
            ]
        },
        {
            "records": [
                {
                    "block_id": "P1",
                    "kind": "contextual_lexicon",
                    "status": "resolved",
                    "term": "短形式",
                    "resolved_meaning": "短い形式",
                    "explanation": "文脈上の意味を確定した。",
                }
            ]
        },
    ])
    worker_case.worker.translator = resolver

    records = worker_case.worker._language_plan(
        claimed, {"proposed_html": HTML}, "plan"
    )

    assert resolver.calls == [["P1", "P2", "P3"], ["P1"]]
    p1 = [item for item in records if item["block_id"] == "P1"]
    assert {item["kind"] for item in p1} == {"block_review", "contextual_lexicon"}
    rows = checkpoint_rows(worker_case, claimed.run_id)
    assert any(row["unit_id"] == "partial-valid:P1" for row in rows)
    assert any(row["unit_id"] == "result:P1" for row in rows)


def test_v22_incomplete_supplement_is_terminal_and_not_repeated(worker_case):
    claimed = claimed_v22(worker_case)
    resolver = ScriptedLanguageResolver(
        [
            {"records": [record("P1")]},
            {"records": [record("P2")]},
        ]
    )
    worker_case.worker.translator = resolver
    records = worker_case.worker._language_plan(
        claimed, {"proposed_html": HTML}, "plan"
    )
    incomplete = next(item for item in records if item["block_id"] == "P3")
    assert incomplete["status"] == "response_contract_incomplete"
    assert resolver.calls == [["P1", "P2", "P3"], ["P2", "P3"]]

    resolver.calls.clear()
    resolver.script = []
    assert worker_case.worker._language_plan(
        claimed, {"proposed_html": HTML}, "plan"
    ) == records
    assert resolver.calls == []


def test_v22_language_process_death_after_reservation_is_terminal_on_restart(
    worker_case,
):
    claimed = claimed_v22(worker_case)

    class ProcessDeath(SystemExit):
        pass

    def crash_after_reservation(_blocks):
        with worker_case.databases.state.connect() as connection:
            markers = [
                json.loads(row[0])
                for row in connection.execute(
                    "SELECT payload_json FROM manuscript_unit_checkpoints "
                    "WHERE run_id=? AND phase='language-v22:plan' "
                    "AND unit_id LIKE 'retry:%' ORDER BY unit_id",
                    (claimed.run_id,),
                )
            ]
        assert len(markers) == 2
        assert {marker["status"] for marker in markers} == {"in_progress"}
        raise ProcessDeath("simulated process death")

    resolver = ScriptedLanguageResolver(
        [
            {"records": [record("P1")]},
            crash_after_reservation,
        ]
    )
    worker_case.worker.translator = resolver
    with pytest.raises(ProcessDeath):
        worker_case.worker._language_plan(
            claimed, {"proposed_html": HTML}, "plan"
        )
    assert resolver.calls == [["P1", "P2", "P3"], ["P2", "P3"]]

    resolver.calls.clear()
    resolver.script = [
        {"records": [record("P2"), record("P3")]},
    ]
    records = worker_case.worker._language_plan(
        claimed, {"proposed_html": HTML}, "plan"
    )
    assert resolver.calls == []
    interrupted = {
        item["block_id"]: item
        for item in records
        if item.get("status") == "response_contract_incomplete"
    }
    assert set(interrupted) == {"P2", "P3"}
    assert all(
        item["contract_issues"] == ["supplement_execution_interrupted"]
        for item in interrupted.values()
    )
    with worker_case.databases.state.connect() as connection:
        markers = [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT payload_json FROM manuscript_unit_checkpoints "
                "WHERE run_id=? AND phase='language-v22:plan' "
                "AND unit_id LIKE 'retry:%' ORDER BY unit_id",
                (claimed.run_id,),
            )
        ]
    assert {marker["status"] for marker in markers} == {"consumed"}
    assert {marker["outcome"] for marker in markers} == {
        "supplement_execution_interrupted"
    }


def test_v22_unresolved_language_records_create_conditional_review_requirements(worker_case):
    claimed = claimed_v22(worker_case)
    records = [
        {
            **record("P1", "semantic_analysis_unresolved"),
            "kind": "term_introduction",
        },
        {
            **record("P2", "semantic_analysis_unresolved"),
            "kind": "contextual_lexicon",
        },
        {**record("P3", "response_contract_incomplete")},
    ]
    context = worker_case.worker._language_condition_context_v22(
        claimed, {}, phase="plan", records=records
    )
    assert context["completion_status"] == "conditional_success"
    classes = {item["review_class"] for item in context["language_review_requirements"]}
    assert classes == {
        "human_semantic_judgement_required",
        "minor_lexical_source_preserved",
        "response_contract_incomplete",
    }
    assert context["language_execution_policy"]["automatic_engine_fallback"] is False

    resolved = [{**record("P1", "resolved"), "kind": "term_introduction"}]
    only_term = {
        **context,
        "language_review_requirements": [context["language_review_requirements"][0]],
    }
    cleared = worker_case.worker._language_condition_context_v22(
        claimed, only_term, phase="final", records=resolved
    )
    assert cleared["language_review_requirements"] == []
    assert cleared["completion_status"] == "success"
    assert "conditional_success" not in cleared


def test_v22_uncertain_causal_relation_requires_human_judgement(worker_case):
    claimed = claimed_v22(worker_case)
    payload = {
        "records": [
            {
                **record("P1", "semantic_analysis_unresolved"),
                "kind": "causal_relation",
                "cause": "媒体の配置変更",
                "effect": "読者経験の変化",
                "causal_warrant": "受容資料が不足しており確定できない",
            }
        ]
    }

    valid, issues = worker_case.worker._validate_language_payload_v22(payload, {"P1"})
    assert not issues["P1"]
    context = worker_case.worker._language_condition_context_v22(
        claimed, {}, phase="plan", records=valid["P1"]
    )

    requirement = context["language_review_requirements"][0]
    assert requirement["kind"] == "causal_relation"
    assert requirement["review_class"] == "human_semantic_judgement_required"
    assert context["completion_status"] == "conditional_success"


def test_v22_engine_change_after_transport_is_rejected_without_exact_authorization(worker_case):
    claimed = claimed_v22(worker_case)
    first = ScriptedLanguageResolver([ExternalServiceUnavailable("capacity")])
    worker_case.worker.translator = first
    with pytest.raises(ExternalServiceUnavailable):
        worker_case.worker._language_plan(claimed, {"proposed_html": HTML}, "plan")

    class OtherResolver(ScriptedLanguageResolver):
        pass

    worker_case.worker.translator = OtherResolver(
        [{"records": [record("P1"), record("P2"), record("P3")]}]
    )
    with pytest.raises(NonRetryableFukamiruError, match="without_authorization"):
        worker_case.worker._language_plan(claimed, {"proposed_html": HTML}, "plan")

    fingerprint = worker_case.worker._language_engine_descriptor_v22()["fingerprint"]
    authorized, _ = authorize_engine_transition(worker_case, claimed, fingerprint)
    context = {**authorized.inputs, "proposed_html": HTML}
    records = worker_case.worker._language_plan(authorized, context, "plan")
    audited = worker_case.worker._language_condition_context_v22(
        claimed, context, phase="plan", records=records
    )
    history = audited["language_inference_history"]
    assert len(history) == 2
    assert history[-1]["change_authorized"] is True
    assert history[-1]["engine"]["fingerprint"] == fingerprint


def analysis_connection():
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE manuscript_language_analyses(signature PRIMARY KEY,payload_json)"
    )
    return connection


def unresolved_parse(_):
    return {
        "status": "unresolved",
        "ambiguities": ["implicit_subject"],
        "ambiguity_spans": [],
        "tokens": [],
    }


def test_v22_cached_analysis_does_not_cache_transport_failure(monkeypatch):
    connection = analysis_connection()
    monkeypatch.setattr(japanese_analysis, "parse_japanese", unresolved_parse)

    def unavailable(**_):
        raise ExternalServiceUnavailable("capacity")

    with pytest.raises(ExternalServiceUnavailable):
        cached_analysis_v22(connection, "本文", "前後", unavailable, engine_fingerprint="test")
    count = connection.execute("SELECT COUNT(*) FROM manuscript_language_analyses").fetchone()[0]
    assert count == 0


def test_v22_cached_analysis_requires_the_mandatory_adjudicator(monkeypatch):
    connection = analysis_connection()
    monkeypatch.setattr(japanese_analysis, "parse_japanese", unresolved_parse)

    with pytest.raises(ExternalServiceUnavailable, match="required but unavailable"):
        cached_analysis_v22(connection, "本文", "前後", engine_fingerprint="test")
    count = connection.execute("SELECT COUNT(*) FROM manuscript_language_analyses").fetchone()[0]
    assert count == 0


def test_v22_cached_analysis_does_not_cache_analyzer_configuration_failure(monkeypatch):
    connection = analysis_connection()

    def unavailable_parse(_):
        return {
            "status": "unresolved",
            "ambiguities": ["analyzer_unavailable:mecab"],
            "ambiguity_spans": [],
            "tokens": [],
        }

    monkeypatch.setattr(japanese_analysis, "parse_japanese", unavailable_parse)

    with pytest.raises(FukamiruError, match="analysis is unavailable"):
        cached_analysis_v22(connection, "本文", "前後", lambda **_: {}, engine_fingerprint="test")
    count = connection.execute("SELECT COUNT(*) FROM manuscript_language_analyses").fetchone()[0]
    assert count == 0


def test_v22_cached_analysis_repairs_contract_once_then_caches_explicit_status(monkeypatch):
    connection = analysis_connection()
    monkeypatch.setattr(japanese_analysis, "parse_japanese", unresolved_parse)
    calls = []

    def incomplete(**_):
        calls.append(1)
        return {"status": "resolved", "explanation": "", "roles": []}

    result = cached_analysis_v22(connection, "本文", "前後", incomplete, engine_fingerprint="test")
    assert len(calls) == 2
    assert result["decision"] == "response_contract_incomplete"
    assert cached_analysis_v22(
        connection, "本文", "前後", incomplete, engine_fingerprint="test"
    ) == result
    assert len(calls) == 2


def test_v22_cached_analysis_persists_repair_budget_across_transport_wait(monkeypatch):
    connection = analysis_connection()
    monkeypatch.setattr(japanese_analysis, "parse_japanese", unresolved_parse)
    calls = []

    def interrupted_supplement(**_):
        calls.append(1)
        if len(calls) == 1:
            return {"status": "resolved", "explanation": "", "roles": []}
        raise ExternalServiceUnavailable("capacity")

    with pytest.raises(ExternalServiceUnavailable):
        cached_analysis_v22(
            connection, "本文", "前後", interrupted_supplement, engine_fingerprint="test"
        )
    saved = json.loads(
        connection.execute(
            "SELECT payload_json FROM manuscript_language_analyses"
        ).fetchone()[0]
    )
    assert saved["response_contract_state"] == "supplement_pending"
    assert saved["adjudication_attempt_count"] == 1

    resumed_calls = []

    def still_incomplete(**_):
        resumed_calls.append(1)
        return {"status": "resolved", "explanation": "", "roles": []}

    result = cached_analysis_v22(
        connection, "本文", "前後", still_incomplete, engine_fingerprint="test"
    )
    assert len(resumed_calls) == 1
    assert result["decision"] == "response_contract_incomplete"
    assert result["adjudication_attempt_count"] == 2

    assert cached_analysis_v22(
        connection, "本文", "前後", still_incomplete, engine_fingerprint="test"
    ) == result
    assert len(resumed_calls) == 1
