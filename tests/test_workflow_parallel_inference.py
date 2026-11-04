from __future__ import annotations

import sqlite3
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from fukamiru.errors import ExternalServiceUnavailable, RunCancelled
from fukamiru.projects.service import ProjectService
from fukamiru.revisions import ManuscriptRewriteService, RevisionService
from fukamiru.storage.runs import RunRepository
from fukamiru.worker.runner import HandlerResult
from fukamiru.workflow.graph import WorkflowDefinition
from fukamiru.workflow.templates import (
    manuscript_rewrite_workflow,
    manuscript_rewrite_workflow_v22,
    manuscript_rewrite_workflow_v23,
    manuscript_rewrite_workflow_v24,
    manuscript_rewrite_workflow_v25,
    manuscript_rewrite_workflow_v26,
    manuscript_rewrite_workflow_v27,
    manuscript_rewrite_workflow_v28,
)


@pytest.fixture
def case():
    from test_worker import WorkerTests

    value = WorkerTests()
    value.setUp()
    yield value
    value.tearDown()


def claimed(case, *, node="plan_term_introductions", version="23"):
    run_id = case._create_run("manuscript_rewrite")
    value = case.worker._claim(lane="ingestion_cpu")
    assert value is not None and value.run_id == run_id
    return replace(value, node_id=node, workflow_version=version, resource_lane="codex_remote")


def manuscript(count):
    return (
        '<article id="D"><section id="S"><h2 id="H">本文</h2>'
        + "".join(f'<p id="P{i}">第{i}の段落である。</p>' for i in range(count))
        + "</section></article>"
    )


def records(blocks):
    return {
        "records": [
            {
                "block_id": b["id"],
                "kind": "block_review",
                "status": "no_revision_needed",
                "explanation": "本文を検証した。",
            }
            for b in blocks
        ]
    }


def test_v23_is_independent_and_default_v29_preserves_old_versions():
    v22 = manuscript_rewrite_workflow_v22()
    assert manuscript_rewrite_workflow().version == "29"
    assert manuscript_rewrite_workflow_v28().version == "28"
    v23 = manuscript_rewrite_workflow_v23()
    assert v23.version == "23"
    assert v23.nodes == v22.nodes and v23.edges == v22.edges
    assert manuscript_rewrite_workflow_v24().nodes == v23.nodes
    assert manuscript_rewrite_workflow_v25().version == "25"
    assert manuscript_rewrite_workflow_v26().version == "26"
    assert manuscript_rewrite_workflow_v27().version == "27"
    assert WorkflowDefinition.model_validate(v23.model_dump()) == v23
    v23.nodes[0].title = "changed"
    assert manuscript_rewrite_workflow_v22().model_dump() == v22.model_dump()


def test_v24_japanese_analysis_overlaps_blocks_and_merges_in_source_order(case, monkeypatch):
    work = case.worker
    task = claimed(case, node="analyze_japanese_structure", version="24")
    first_started = threading.Event()
    second_started = threading.Event()
    active = 0
    peak = 0
    lock = threading.Lock()
    calls = []

    monkeypatch.setattr(work, "_manuscript_inference_backend", lambda *_: object())
    monkeypatch.setattr(
        work, "_register_language_engine_v22", lambda *_, **__: {"fingerprint": "f" * 64}
    )
    monkeypatch.setattr(work, "_reserve_codex_subslots", lambda *_, **__: 2)
    monkeypatch.setattr(work, "_release_codex_subslots", lambda *_, **__: None)

    def fake_analysis(_connection, text, _context, _resolver, *, engine_fingerprint):
        nonlocal active, peak
        assert engine_fingerprint == "f" * 64
        with lock:
            active += 1
            peak = max(peak, active)
            calls.append(text)
            call_index = len(calls)
            if call_index == 1:
                first_started.set()
            elif call_index == 2:
                second_started.set()
        if call_index == 1:
            assert second_started.wait(3)
            time.sleep(0.03)
        with lock:
            active -= 1
        return {"status": "analyzed", "decision": "analysis_ready", "text": text}

    monkeypatch.setattr("fukamiru.worker.runner.cached_analysis_v22", fake_analysis)
    result = work._manuscript_analyze_japanese(task, {"proposed_html": manuscript(6)})
    assert first_started.is_set() and second_started.is_set()
    assert peak >= 2
    assert list(result.context["japanese_language_analysis"]) == [
        "H", *(f"P{i}" for i in range(6))
    ]
    assert len(calls) == 7


def test_v24_coherence_mixed_routes_are_conditional(case):
    work = case.worker
    task = claimed(case, node="validate_rewrite_coherence", version="24")
    routes = [
        {"category": "evidence", "resolution_route": "additional_research",
         "research_query": "source", "block_ids": ["P0"]},
        {"category": "wording", "resolution_route": "bounded_rewrite",
         "block_ids": ["P1"]},
        {"category": "scope", "resolution_route": "claim_limitation",
         "block_ids": ["P2"]},
    ]
    context = {
        "structure_plan_validation": {
            "valid": False, "valid_for_conditional_execution": True,
        },
        "paragraph_plan_validation": {
            "valid": True, "global_valid": True, "fallback_used": True,
        },
        "paragraph_plan": {"unresolved_boundaries": ["source boundary retained"]},
        "paragraph_boundary_lock": {"lock_status": "locked"},
    }
    outcome = work._coherence_conditional_success_result(
        task,
        context=context,
        stage="pre_polish",
        candidate_html=manuscript(3).replace('<article id="D">', '<article id="D" lang="ja">'),
        reports=[{"passed": False, "contract_issues": []}],
        applied_edits=[], warnings=[], restored_ids=[], repair_history=[],
        routes=routes, pass_index=4,
    )
    assert outcome.outcome == "conditional_success"
    assert outcome.context["conditional_success"]["route_counts"] == {
        "additional_research": 1, "bounded_rewrite": 1, "claim_limitation": 1,
    }
    assert outcome.context["proposed_html"] == manuscript(3).replace(
        '<article id="D">', '<article id="D" lang="ja">'
    )


def test_v24_full_run_persists_whole_proposal_for_bulk_review(case):
    v24 = manuscript_rewrite_workflow_v24()
    case.workflows["manuscript_rewrite"] = v24
    case.worker.workflows["manuscript_rewrite"] = v24
    case.worker.workflow_versions[("manuscript_rewrite", "24")] = v24
    with case.databases.state.connect() as connection:
        project = ProjectService(connection).create(
            name="V24 review", root_path=Path(case.temp.name) / "v24-review"
        )
        revisions = RevisionService(connection)
        document_id = revisions.create_document(
            project_id=project["id"], kind="paper", title="V24 draft"
        )
        base = revisions.commit_revision(
            document_id=document_id,
            canonical_html=(
                '<article id="D-V24" lang="en"><section id="S-V24">'
                '<h2 id="H-V24">Argument</h2>'
                '<p id="P-V24">Initial working claim.</p>'
                '</section></article>'
            ),
            editor_type="human",
            operation="human_import",
        )
    run_id = case._create_run(
        "manuscript_rewrite",
        project_id=project["id"],
        extra_inputs={
            "document_id": document_id,
            "base_revision_id": base.revision_id,
            "use_rag": False,
            "use_online_research": False,
            "use_llm_jp": False,
        },
    )
    case._drain(limit=50)
    with case.databases.state.connect() as connection:
        snapshot = RunRepository(connection).snapshot(run_id)
        proposal = ManuscriptRewriteService(connection).get_by_run(run_id)
    assert snapshot["run"]["status"] == "completed"
    assert snapshot["run"]["output"]["completion_status"] == "conditional_success"
    assert proposal is not None
    assert str(proposal["context"]["workflow_version"]) == "24"
    assert proposal["context"]["conditional_success"]["reason"] == "quality_review_required"
    assert proposal["status"] in {"pending", "needs_human_review"}
    assert proposal["context"]["paragraph_boundary_lock"]["lock_status"] == "locked"
    assert proposal["context"]["approval_structure_lock"]["lock_status"] == "locked"
    with case.databases.state.connect() as connection:
        confirmed = ManuscriptRewriteService(connection).confirm(
            proposal["id"],
            expected_sha256=proposal["proposed_sha256"],
            acknowledge_unresolved=True,
        )
    assert confirmed["status"] == "confirmed"
    assert confirmed["context"]["bulk_approval"]["approved_candidate_sha256"] == (
        proposal["proposed_sha256"]
    )


@pytest.mark.parametrize("mutation", ["late_html_edit", "cycle", "no_sink"])
def test_v23_readonly_tail_cannot_hide_a_mutation_or_nonterminating_path(mutation):
    graph = manuscript_rewrite_workflow_v23().model_dump()
    if mutation == "late_html_edit":
        for node in graph["nodes"]:
            if node["id"] == "validate_discourse_introductions":
                node["handler"] = "manuscript_rewrite.render_references"
    elif mutation == "cycle":
        graph["edges"].append(
            {
                "source": "validate_discourse_introductions",
                "target": "validate_discourse_introductions",
            }
        )
    else:
        graph["edges"] = [
            e for e in graph["edges"] if e["source"] != "validate_discourse_introductions"
        ]
    with pytest.raises(ValueError):
        WorkflowDefinition.model_validate(graph)


@pytest.mark.parametrize("failure", [False, True])
def test_timing_write_failure_does_not_replace_inference_outcome(case, monkeypatch, failure):
    work = case.worker
    task = claimed(case)
    external = ExternalServiceUnavailable("capacity")

    def invoke(*args, **kwargs):
        if failure:
            raise external
        return {"received": "answer"}

    def timing_error(*args, **kwargs):
        raise sqlite3.OperationalError("timing storage busy")

    monkeypatch.setattr(work, "_invoke_bound_manuscript_inference", invoke)
    monkeypatch.setattr("fukamiru.worker.inference_pool.EventRepository.append", timing_error)
    if failure:
        with pytest.raises(ExternalServiceUnavailable) as caught:
            work._invoke_manuscript_inference(task, "test")
        assert caught.value is external
    else:
        assert work._invoke_manuscript_inference(task, "test") == {"received": "answer"}
    assert work._codex_active_count == 0


@pytest.mark.parametrize("phase", ["plan", "final"])
def test_parallel_batches_save_before_slow_predecessor_and_merge_in_order(case, monkeypatch, phase):
    work = case.worker
    task = claimed(case)
    second_saved = threading.Event()
    calls = []
    original_progress = work._record_manuscript_progress

    def progress(*args, **kwargs):
        if kwargs.get("completed") == 5:
            with case.databases.state.connect() as connection:
                row = connection.execute(
                    "SELECT 1 FROM manuscript_unit_checkpoints WHERE run_id=? "
                    "AND phase=? AND unit_id='result:P5'",
                    (task.run_id, "language-v23:" + phase),
                ).fetchone()
            assert row is not None
            second_saved.set()
        return original_progress(*args, **kwargs)

    class Resolver:
        def plan_manuscript_language(self, *, blocks, workflow_version, **kwargs):
            assert workflow_version == 23
            calls.append([b["id"] for b in blocks])
            if blocks[0]["id"] == "P0":
                assert second_saved.wait(5), "Later batch was blocked by its predecessor"
            return records(blocks)

    work.translator = Resolver()
    monkeypatch.setattr(work, "_record_manuscript_progress", progress)
    context = {"proposed_html": manuscript(10)}
    result = work._language_plan(task, context, phase)
    assert [r["block_id"] for r in result] == [f"P{i}" for i in range(10)]
    assert len(calls) == 2
    assert work._language_plan(task, context, phase) == result
    assert len(calls) == 2, "Completed batches must be reused"
    assert work._codex_active_count == 0


def test_parallel_failure_preserves_success_and_resumes_only_missing_batch(case):
    work = case.worker
    task = claimed(case)
    started = threading.Barrier(2)
    calls = []

    class Resolver:
        fail = True

        def plan_manuscript_language(self, *, blocks, **_):
            calls.append([b["id"] for b in blocks])
            if self.fail:
                started.wait(5)
                if blocks[0]["id"] == "P0":
                    raise ExternalServiceUnavailable("capacity")
            return records(blocks)

    work.translator = Resolver()
    context = {"proposed_html": manuscript(10)}
    with pytest.raises(ExternalServiceUnavailable):
        work._language_plan(task, context, "plan")
    work.translator.fail = False
    calls.clear()
    result = work._language_plan(task, context, "plan")
    assert calls == [[f"P{i}" for i in range(5)]]
    assert len(result) == 10
    assert work._codex_active_count == 0


def test_each_missing_unit_receives_one_supplement_and_valid_records_survive(case):
    work = case.worker
    task = claimed(case)
    seen = Counter()
    lock = threading.Lock()

    class Resolver:
        def plan_manuscript_language(self, *, blocks, **_):
            with lock:
                seen.update(b["id"] for b in blocks)
            # One missing unit per initial batch remains missing on supplement.
            return records([b for b in blocks if b["id"] not in {"P0", "P5"}])

    work.translator = Resolver()
    context = {"proposed_html": manuscript(10)}
    result = work._language_plan(task, context, "plan")
    assert seen == Counter({f"P{i}": 2 if i in {0, 5} else 1 for i in range(10)})
    assert {r["block_id"] for r in result if r["status"] == "response_contract_incomplete"} == {
        "P0",
        "P5",
    }
    assert work._language_plan(task, context, "plan") == result
    assert sum(seen.values()) == 12


def test_supplement_transport_failure_releases_budget_for_resume(case):
    work = case.worker
    task = claimed(case)
    calls = []

    class Resolver:
        fail = True

        def plan_manuscript_language(self, *, blocks, **_):
            calls.append([b["id"] for b in blocks])
            if len(calls) == 1:
                return records(blocks[1:])
            if self.fail:
                raise ExternalServiceUnavailable("capacity")
            return records(blocks)

    work.translator = Resolver()
    context = {"proposed_html": manuscript(3)}
    with pytest.raises(ExternalServiceUnavailable):
        work._language_plan(task, context, "plan")
    work.translator.fail = False
    result = work._language_plan(task, context, "plan")
    assert calls == [["P0", "P1", "P2"], ["P0"], ["P0"]]
    assert all(r["status"] == "no_revision_needed" for r in result)


def test_interrupted_supplement_is_not_reissued(case, monkeypatch):
    work = case.worker
    task = claimed(case)
    calls = []

    class Resolver:
        def plan_manuscript_language(self, *, blocks, **_):
            calls.append([b["id"] for b in blocks])
            return records(blocks[1:]) if len(calls) == 1 else records(blocks)

    work.translator = Resolver()
    persist = work._persist_missing_unit_supplement_outcome_v22

    def crash(*args, **kwargs):
        raise RuntimeError("process stopped before supplement commit")

    monkeypatch.setattr(work, "_persist_missing_unit_supplement_outcome_v22", crash)
    context = {"proposed_html": manuscript(3)}
    with pytest.raises(RuntimeError, match="process stopped"):
        work._language_plan(task, context, "plan")
    monkeypatch.setattr(work, "_persist_missing_unit_supplement_outcome_v22", persist)
    result = work._language_plan(task, context, "plan")
    assert calls == [["P0", "P1", "P2"], ["P0"]]
    assert result[0]["status"] == "response_contract_incomplete"


def test_v23_calls_share_capacity_with_legacy_reservations_and_other_runs(case, monkeypatch):
    work = case.worker
    tasks = [claimed(case), claimed(case)]
    work.settings = replace(
        work.settings, worker=replace(work.settings.worker, codex_max_concurrency=3)
    )
    assert work._try_acquire_codex_slot()  # A legacy node owns one of three slots.
    overlap = threading.Barrier(2)
    active = 0
    peak = 0
    lock = threading.Lock()

    def invoke(*args, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            assert work._codex_active_count <= 3
        try:
            overlap.wait(5)
            return True
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(work, "_invoke_bound_manuscript_inference", invoke)
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [
            executor.submit(work._invoke_manuscript_inference, tasks[i % 2], "test")
            for i in range(6)
        ]
        assert all(f.result(timeout=10) for f in futures)
    assert peak == 2
    assert work._codex_active_count == 1
    work._release_codex_slot()
    assert not work._codex_v23_waiters


def test_per_call_slot_released_on_error_and_cancelled_waiter_removed(case, monkeypatch):
    work = case.worker
    task = claimed(case)

    def fail(*args, **kwargs):
        raise ExternalServiceUnavailable("test")

    monkeypatch.setattr(work, "_invoke_bound_manuscript_inference", fail)
    with pytest.raises(ExternalServiceUnavailable):
        work._invoke_manuscript_inference(task, "test")
    assert work._codex_active_count == 0
    with case.databases.state.connect() as connection:
        connection.execute("UPDATE runs SET status='cancelled' WHERE id=?", (task.run_id,))
        connection.commit()
    with pytest.raises(RunCancelled):
        work._invoke_manuscript_inference(task, "test")
    assert not work._codex_v23_waiters
    assert work._codex_active_count == 0


def test_cancelled_queued_call_does_not_block_following_admission(case, monkeypatch):
    work = case.worker
    task = claimed(case)
    work.settings = replace(
        work.settings, worker=replace(work.settings.worker, codex_max_concurrency=1)
    )
    assert work._try_acquire_codex_slot()
    waiting = threading.Event()
    original_check = work._raise_if_run_cancelled

    def check(*args, **kwargs):
        waiting.set()
        return original_check(*args, **kwargs)

    monkeypatch.setattr(work, "_raise_if_run_cancelled", check)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(work._invoke_manuscript_inference, task, "never-dispatch")
        assert waiting.wait(5)
        with case.databases.state.connect() as connection:
            connection.execute("UPDATE runs SET status='cancelled' WHERE id=?", (task.run_id,))
            connection.commit()
        with pytest.raises(RunCancelled):
            future.result(timeout=5)
    assert not work._codex_v23_waiters
    assert work._codex_active_count == 1
    work._release_codex_slot()
    assert work._try_acquire_codex_slot()
    work._release_codex_slot()


def test_v23_parent_releases_its_slot_and_virtual_subslots_do_not_release_other_owners(case):
    work = case.worker
    definition = manuscript_rewrite_workflow_v23()
    work.workflow_versions[(definition.name, definition.version)] = definition
    run_id = case._create_run("manuscript_rewrite")
    with case.databases.state.connect() as connection:
        connection.execute("UPDATE runs SET workflow_version='23' WHERE id=?", (run_id,))
        connection.execute(
            "UPDATE node_runs SET resource_lane='codex_remote' WHERE run_id=?", (run_id,)
        )
        connection.commit()

    def handler(task, context):
        assert work._codex_active_count == 0
        assert work._try_acquire_codex_slot()
        count = work._reserve_codex_subslots(task, task_count=5, requested_parallelism=5)
        assert count == 4
        work._release_codex_subslots(task, count)
        assert work._codex_active_count == 1
        work._release_codex_slot()
        return HandlerResult(context)

    work.handlers["manuscript_rewrite.prepare_context"] = handler
    assert work.run_once(lane="codex_remote")
    assert work._codex_active_count == 0
    with case.databases.state.connect() as connection:
        status = connection.execute(
            "SELECT status FROM node_runs WHERE run_id=? ORDER BY rowid", (run_id,)
        ).fetchone()[0]
    assert status == "completed"


def test_new_node_admission_is_not_starved_by_pending_v23_batches(case):
    work = case.worker
    ticket = object()
    work._codex_v23_waiters.append(ticket)
    # Extra legacy fan-out yields, but a new node can still start and make progress.
    assert not work._try_acquire_codex_slot()
    assert work._try_acquire_codex_slot(for_node_claim=True)
    assert work._try_acquire_codex_slot(v23_ticket=ticket)
    assert not work._codex_v23_waiters
    assert work._codex_active_count == 2
    work._release_codex_slot()
    work._release_codex_slot()


def test_v22_language_checkpoints_remain_sequential_and_separate(case):
    task = claimed(case, version="22")
    calls = []

    class Resolver:
        def plan_manuscript_language(self, *, blocks, **_):
            calls.append([b["id"] for b in blocks])
            return records(blocks)

    case.worker.translator = Resolver()
    case.worker._language_plan(task, {"proposed_html": manuscript(6)}, "plan")
    assert calls == [[f"P{i}" for i in range(5)], ["P5"]]
    with case.databases.state.connect() as connection:
        phases = {
            r[0]
            for r in connection.execute(
                "SELECT phase FROM manuscript_unit_checkpoints WHERE run_id=?", (task.run_id,)
            )
        }
    assert "language-v22:plan" in phases
    assert "language-v23:plan" not in phases


def test_v23_api_is_explicit_and_default_is_v29(monkeypatch):
    from test_api import APITests

    api = APITests()
    api.setUp()
    try:

        def prepare(*args, **kwargs):
            return {"project_id": None, "base_revision_id": "R", "outline_node_id": None}

        # Use an existing project so run creation satisfies its foreign key.
        with api.app.state.databases.state.connect() as connection:
            project_id = connection.execute("SELECT id FROM projects LIMIT 1").fetchone()
        if project_id is None:
            response = api.client.post(
                "/api/projects",
                json={"name": "V23 test", "root_path": str(api.root / "v23-project")},
            )
            assert response.status_code == 201, response.text
            project = response.json()
            project_id = (project["id"],)

        def prepared(*args, **kwargs):
            return {**prepare(), "project_id": project_id[0]}

        monkeypatch.setattr("fukamiru.api.app.ManuscriptRewriteService.prepare", prepared)
        for requested, expected in [(None, "29"), ("28", "28"), ("27", "27"), ("23", "23")]:
            request = {"document_id": "D", "instructions": "test"}
            if requested:
                request["workflow_version"] = requested
            response = api.client.post("/api/manuscript-rewrites", json=request)
            assert response.status_code == 202, response.text
            with api.app.state.databases.state.connect() as connection:
                row = connection.execute(
                    "SELECT workflow_version FROM runs WHERE id=?", (response.json()["run_id"],)
                ).fetchone()
            assert row[0] == expected
            if requested == "23":
                v23_run_id = response.json()["run_id"]
        monkeypatch.setattr(
            "fukamiru.api.app.ManuscriptRewriteService.get",
            lambda *args: {
                "run_id": v23_run_id,
                "lifecycle_status": "pending",
                "document_id": "D",
                "base_revision_id": "R",
                "outline_node_id": None,
            },
        )
        revised = api.client.post(
            "/api/manuscript-rewrites/proposal/revise", json={"human_review": "追記する"}
        )
        assert revised.status_code == 202, revised.text
        with api.app.state.databases.state.connect() as connection:
            row = connection.execute(
                "SELECT workflow_version FROM runs WHERE id=?", (revised.json()["run_id"],)
            ).fetchone()
        assert row[0] == "23", "An explicit V23 run must not silently downgrade on revision"
        assert (
            api.client.post(
                "/api/manuscript-rewrites",
                json={"document_id": "D", "instructions": "test", "workflow_version": "99"},
            ).status_code
            == 422
        )
    finally:
        api.tearDown()
