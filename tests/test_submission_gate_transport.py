from __future__ import annotations

import json
from pathlib import Path

import pytest

from fukamiru.documents.japanese_surface import JapaneseSurfaceQualityGate as Surface
from fukamiru.errors import ExternalServiceUnavailable
from fukamiru.revisions.coherence import ManuscriptCoherenceService as Coherence
from fukamiru.storage.runs import RunRepository
from fukamiru.translation.codex import CodexTranslationAdapter
from fukamiru.worker.runner import WorkerRunner
from fukamiru.workflow.templates import manuscript_rewrite_workflow_v21


def manuscript(text):
    return (
        '<article id="D" lang="ja"><section id="S"><h2 id="H">結論</h2>'
        f'<p id="P">{text}</p></section></article>'
    )


def test_format_only_warning_does_not_block_additional_research():
    before = manuscript("『甲』『乙』は同じ棚に入りうるという仮説である。")
    after = Surface.guard_canonical_html(before, before).canonical_html
    report = {
        "passed": False,
        "issues": [
            {
                "id": f"R{i}",
                "category": "missing_warrant",
                "severity": "blocking",
                "resolution_route": "additional_research",
                "block_ids": ["P"],
                "research_query": f"資料{i}",
            }
            for i in range(4)
        ]
        + [
            {
                "id": "W",
                "category": "missing_warrant",
                "severity": "warning",
                "resolution_route": "none",
                "block_ids": ["P"],
            }
        ],
    }
    classified = WorkerRunner._classify_final_coherence_issues(
        report, pre_polish_html=before, candidate_html=after
    )
    assert classified["issues"][-1]["severity"] == "warning"
    assert all(i["formatting_only_change"] for i in classified["issues"])
    routes = WorkerRunner._coherence_resolution_routes(classified)
    assert len(routes) == 4
    assert WorkerRunner._coherence_requires_additional_research(routes)
    restored, ids = Coherence.restore_blocks(before, after, {"P"})
    assert ids == []
    assert restored == after
    assert Surface.guard_canonical_html(before, restored).canonical_html == after


@pytest.mark.parametrize(
    "text",
    [
        "『甲』・『乙』は同じ棚に入らない。",
        "『甲』・『乙』は必ず同じ棚に入る。",
        "『甲』・『丙』は同じ棚に入る。",
    ],
)
def test_meaning_change_is_not_hidden_by_separator_equivalence(text):
    before = manuscript("『甲』『乙』は同じ棚に入る。")
    classified = WorkerRunner._classify_final_coherence_issues(
        {
            "issues": [
                {
                    "id": "R",
                    "severity": "blocking",
                    "block_ids": ["P"],
                    "resolution_route": "bounded_rewrite",
                }
            ]
        },
        pre_polish_html=before,
        candidate_html=manuscript(text),
    )
    issue = classified["issues"][0]
    assert issue["semantic_changed_since_pre_polish"]
    assert not issue["formatting_only_change"]
    assert issue["severity"] == "blocking"
    assert issue["resolution_route"] == "bounded_rewrite"
    assert issue["final_gate_disposition"] == "post_edit_finding"


def test_restore_keeps_required_separators_and_converges():
    before = manuscript("『甲』『乙』は同じ棚に入りうる。")
    damaged = manuscript("『甲』・『乙』は必ず同じ棚に入る。")
    restored, ids = Coherence.restore_blocks(before, damaged, {"P"})
    assert ids == ["P"]
    assert "『甲』・『乙』は同じ棚に入りうる。" in restored
    assert Surface.guard_canonical_html(before, restored).canonical_html == restored
    assert Coherence.restore_blocks(before, restored, {"P"}) == (restored, [])


def test_capacity_error_is_typed_and_does_not_echo_manuscript():
    error = CodexTranslationAdapter._execution_error(
        "manuscript coherence validation",
        "private manuscript 429\nERROR: Selected model is at capacity.\n"
        "ERROR: Selected model is at capacity.",
    )
    assert isinstance(error, ExternalServiceUnavailable)
    assert "private manuscript" not in str(error)
    assert str(error).count("at capacity") == 1
    assert WorkerRunner._is_codex_rate_limit(error)
    other = CodexTranslationAdapter._execution_error("test", "manuscript at capacity 429")
    assert not isinstance(other, ExternalServiceUnavailable)


@pytest.fixture
def worker_case():
    from test_worker import WorkerTests

    case = WorkerTests()
    case.setUp()
    yield case
    case.tearDown()


def test_external_retry_is_durable_bounded_and_does_not_consume_node_attempt(worker_case):
    case = worker_case
    rid = case._create_run("translation")
    with case.databases.state.connect() as c:
        c.execute(
            "UPDATE node_runs SET node_id='draft_translation', "
            "resource_lane='codex_remote' WHERE run_id=?",
            (rid,),
        )
        c.commit()
    node_id = None
    for failure in range(1, 5):
        claimed = case.worker._claim(lane="codex_remote")
        assert claimed is not None
        assert claimed.attempt == 1
        node_id = node_id or claimed.node_run_id
        assert claimed.node_run_id == node_id
        case.worker._wait_for_codex(claimed, ExternalServiceUnavailable("at capacity"))
        # Simulate process-local cooldown loss: the persisted not-before still wins.
        case.worker._codex_retry_after = 0
        assert case.worker._claim(lane="codex_remote") is None
        with case.databases.state.connect() as c:
            node = c.execute("SELECT * FROM node_runs WHERE id=?", (node_id,)).fetchone()
            inputs = json.loads(node["input_json"])
            assert inputs["_external_retry"]["count"] == failure
            assert node["status"] == ("waiting_human" if failure == 4 else "queued")
            assert node["lease_owner"] is None
            if failure < 4:
                inputs["_external_retry"]["not_before"] = "2000-01-01T00:00:00Z"
                c.execute(
                    "UPDATE node_runs SET input_json=? WHERE id=?", (json.dumps(inputs), node_id)
                )
                c.commit()
    with case.databases.state.connect() as c:
        run = c.execute("SELECT status,error_json FROM runs WHERE id=?", (rid,)).fetchone()
        assert run["status"] == "waiting_human"
        assert json.loads(run["error_json"])["type"] == "ExternalServiceUnavailable"


def test_v22_manual_resume_resets_only_the_bounded_transport_budget(worker_case):
    case = worker_case
    rid = case._create_run("manuscript_rewrite")
    with case.databases.state.connect() as c:
        c.execute(
            "UPDATE node_runs SET resource_lane='codex_remote' WHERE run_id=?",
            (rid,),
        )
        c.commit()
    node_id = None
    for _ in range(4):
        claimed = case.worker._claim(lane="codex_remote")
        assert claimed is not None
        node_id = node_id or claimed.node_run_id
        assert claimed.node_run_id == node_id
        case.worker._wait_for_codex(claimed, ExternalServiceUnavailable("at capacity"))
        case.worker._codex_retry_after = 0
        with case.databases.state.connect() as c:
            node = c.execute("SELECT * FROM node_runs WHERE id=?", (node_id,)).fetchone()
            inputs = json.loads(node["input_json"])
            if node["status"] == "queued":
                inputs["_external_retry"]["not_before"] = "2000-01-01T00:00:00Z"
                c.execute(
                    "UPDATE node_runs SET input_json=? WHERE id=?",
                    (json.dumps(inputs), node_id),
                )
                c.commit()

    with case.databases.state.connect() as c:
        result = RunRepository(c).resume_external_wait(rid)
    assert result["status"] == "queued"
    assert result["node_run_id"] == node_id

    claimed = case.worker._claim(lane="codex_remote")
    assert claimed is not None
    assert claimed.node_run_id == node_id
    assert claimed.attempt == 1
    case.worker._wait_for_codex(claimed, ExternalServiceUnavailable("still at capacity"))
    with case.databases.state.connect() as c:
        node = c.execute("SELECT * FROM node_runs WHERE id=?", (node_id,)).fetchone()
        inputs = json.loads(node["input_json"])
        run = c.execute("SELECT status FROM runs WHERE id=?", (rid,)).fetchone()
    assert inputs["_external_retry"]["count"] == 1
    assert node["status"] == "queued"
    assert run["status"] == "queued"


def create_manuscript_run(case):
    from fukamiru.projects.service import ProjectService
    from fukamiru.revisions import RevisionService
    from fukamiru.storage.runs import RunRepository
    with case.databases.state.connect() as c:
        project = ProjectService(c).create(
            name="Final gate test", root_path=Path(case.temp.name) / "project"
        )
        revisions = RevisionService(c)
        document = revisions.create_document(project_id=project["id"], kind="paper", title="Test")
        base = revisions.commit_revision(
            document_id=document,
            canonical_html=manuscript("媒体規則は歴史的に組み替えられてきた。"),
            editor_type="human",
            operation="human_import",
        )
        # These integration cases exercise the V21 coherence-repair and
        # conditional-research transport contract.  Register and select that
        # immutable graph explicitly so promoting the latest workflow cannot
        # silently turn them into V22 language-policy tests.
        workflow = manuscript_rewrite_workflow_v21()
        case.worker.workflow_versions[(workflow.name, workflow.version)] = workflow
        return (
            RunRepository(c)
            .create(
                workflow_name=workflow.name,
                workflow_version=workflow.version,
                entry_node=workflow.entry_node,
                project_id=project["id"],
                inputs={
                    "document_id": document,
                    "base_revision_id": base.revision_id,
                    "instructions": "意味と根拠の範囲を保持する。",
                    "use_rag": False,
                    "use_online_research": False,
                    "use_llm_jp": False,
                },
            )
            .run_id
        )


def readonly_audit_fixture(*, phase, units, **_):
    status = (
        "established"
        if phase == "certainty"
        else "still_open"
        if phase == "final_findings"
        else "insufficient"
    )
    return {
        "verdicts": [
            {
                "unit_id": str(unit["unit_id"]),
                "status": status,
                "rationale": "The fixture completed the mandatory read-only audit.",
                "citations": [],
            }
            for unit in units
        ]
    }


def test_real_graph_finishes_conditionally_with_four_research_issues_and_warning(worker_case):
    from test_worker import FakeTranslator

    from fukamiru.revisions import ManuscriptRewriteService
    from fukamiru.storage.runs import RunRepository

    class Translator(FakeTranslator):
        post_calls = 0

        audit_manuscript_records = staticmethod(readonly_audit_fixture)

        def validate_manuscript_coherence(
            self, *, blocks, stage, document_manifest, **kwargs
        ):
            report = super().validate_manuscript_coherence(
                blocks=blocks, document_manifest=document_manifest
            )
            if stage == "post_polish":
                self.post_calls += 1
                target = next(b["id"] for b in blocks if b["tag"] == "p")
                report.update(
                    passed=False,
                    issues=[
                        {
                            "id": f"R{i}",
                            "category": "missing_warrant",
                            "severity": "blocking",
                            "block_ids": [target],
                            "resolution_route": "additional_research",
                            "research_query": f"歴史資料{i}",
                            "explanation": "資料が必要。",
                            "recommendation": "追加研究。",
                        }
                        for i in range(4)
                    ]
                    + [
                        {
                            "id": "W",
                            "category": "missing_warrant",
                            "severity": "warning",
                            "block_ids": [target],
                            "resolution_route": "none",
                            "explanation": "仮説であるとの限定を維持。",
                            "recommendation": "原文維持。",
                        }
                    ],
                )
            return report

    case = worker_case
    case.worker.translator = translator = Translator()
    rid = create_manuscript_run(case)
    case._drain(limit=40)
    with case.databases.state.connect() as c:
        run = RunRepository(c).get(rid)
        assert run["status"] == "completed", run.get("error")
        assert run["output"]["completion_status"] == "conditional_success"
        proposal = ManuscriptRewriteService(c).get_by_run(rid)
        assert proposal["proposed_html"]
        assert len(proposal["context"]["additional_research_requirements"]) == 4
    assert translator.post_calls == 1


def test_two_completed_repairs_survive_capacity_wait_and_worker_restart(worker_case):
    from test_worker import FakeTranslator

    from fukamiru.storage.runs import RunRepository

    class Translator(FakeTranslator):
        post_calls = 0

        audit_manuscript_records = staticmethod(readonly_audit_fixture)

        def validate_manuscript_coherence(
            self, *, blocks, stage, document_manifest, **kwargs
        ):
            report = super().validate_manuscript_coherence(
                blocks=blocks, document_manifest=document_manifest
            )
            if stage != "post_polish":
                return report
            self.post_calls += 1
            if self.post_calls == 3:
                raise ExternalServiceUnavailable("Selected model is at capacity.")
            if self.post_calls > 3:
                assert "一次限定。" in str(blocks)
                assert "二次限定。" in str(blocks)
                return report
            target = next(b for b in blocks if b["tag"] == "p")
            report.update(
                passed=False,
                issues=[
                    {
                        "id": "limit",
                        "category": "missing_warrant",
                        "severity": "blocking",
                        "block_ids": [target["id"]],
                        "resolution_route": "claim_limitation",
                        "explanation": "主張の限定が必要。",
                        "recommendation": "限定を追記。",
                    }
                ],
                repair_edits=[
                    {
                        "operation": "replace_text",
                        "block_id": target["id"],
                        "text": target["text"]
                        + ("一次限定。" if self.post_calls == 1 else "二次限定。"),
                        "review_finding_ids": ["limit"],
                        "evidence_ids": [],
                    }
                ],
            )
            return report

    case = worker_case
    case.worker.translator = translator = Translator()
    rid = create_manuscript_run(case)
    case._drain(limit=40)
    with case.databases.state.connect() as c:
        current = RunRepository(c).get(rid)
        assert current["status"] == "queued", (
            translator.post_calls,
            current["error"],
            current.get("output", {}).get("completion_status"),
        )
        checkpoint = c.execute(
            "SELECT * FROM manuscript_coherence_checkpoints WHERE run_id=? "
            "AND stage='post_polish' ORDER BY round_index DESC LIMIT 1",
            (rid,),
        ).fetchone()
        assert checkpoint["round_index"] == 2
        saved_html = checkpoint["candidate_html"]
        row = c.execute(
            "SELECT * FROM node_runs WHERE run_id=? AND node_id='validate_final_coherence'", (rid,)
        ).fetchone()
        assert row["attempt"] == 1
        inputs = json.loads(row["input_json"])
        inputs["_external_retry"]["not_before"] = "2000-01-01T00:00:00Z"
        c.execute("UPDATE node_runs SET input_json=? WHERE id=?", (json.dumps(inputs), row["id"]))
        c.commit()
        v21_workflow = manuscript_rewrite_workflow_v21()
        case.worker = WorkerRunner(
            case.settings,
            case.databases,
            case.workflows,
            workflow_versions={
                **{
                    (definition.name, definition.version): definition
                    for definition in case.workflows.values()
                },
                (v21_workflow.name, v21_workflow.version): v21_workflow,
            },
            translator=translator,
            semantic_index=case.semantic_index,
        )
    case._drain(limit=20)
    with case.databases.state.connect() as c:
        run = RunRepository(c).get(rid)
        assert run["status"] == "completed", run.get("error")
        assert "_external_retry" not in run["output"]
        checkpoints = c.execute(
            "SELECT * FROM manuscript_coherence_checkpoints WHERE run_id=? AND stage='post_polish' "
            "ORDER BY round_index",
            (rid,),
        ).fetchall()
        assert [r["round_index"] for r in checkpoints] == [1, 2, 3]
        assert checkpoints[1]["candidate_html"] == saved_html
        assert all(
            r["attempt"] == 1
            for r in c.execute("SELECT attempt FROM node_runs WHERE run_id=?", (rid,))
        )
    assert translator.post_calls == 4
