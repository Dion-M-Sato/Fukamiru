from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from fukamiru.errors import CoherenceResolutionRequired, NonRetryableFukamiruError
from fukamiru.revisions import submission_validation
from fukamiru.storage.runs import RunRepository
from fukamiru.translation.codex import CodexTranslationAdapter
from fukamiru.worker import submission_validation as final_worker_v26
from fukamiru.workflow.templates import (
    manuscript_rewrite_workflow_v24,
    manuscript_rewrite_workflow_v25,
    manuscript_rewrite_workflow_v26,
    manuscript_rewrite_workflow_v29,
)

HTML = (
    '<article id="D" lang="ja"><section id="S">'
    '<h2 id="H">本文</h2><p id="P1">主張を述べる。</p>'
    '<p id="P2">根拠を説明する。</p></section>'
    '<section id="R"><h2 id="RH">参考文献</h2>'
    '<p id="RP">著者『作品』、出版社、2020年。</p></section></article>'
)


@pytest.fixture
def worker_case():
    from test_worker import WorkerTests

    case = WorkerTests()
    case.setUp()
    try:
        yield case
    finally:
        case.tearDown()


def claimed(case, node_id: str):
    run_id = case._create_run("manuscript_rewrite")
    task = case.worker._claim(lane="ingestion_cpu")
    assert task is not None and task.run_id == run_id
    return replace(task, node_id=node_id, workflow_version="26", resource_lane="codex_remote")


def fake_report(mode, unit, categories, *, issue=False):
    ids = list(unit["block_ids"])
    issues = []
    repairs = []
    if issue and "P1" in ids and mode == "section":
        issues = [
            {
                "id": "I1",
                "category": "missing_warrant",
                "severity": "blocking",
                "block_ids": ["P1"],
                "explanation": "主張の限定が必要",
                "resolution_route": "claim_limitation",
                "research_query": None,
            }
        ]
        repairs = [
            {
                "block_id": "P1",
                "text": "資料の範囲では主張が成り立つ。",
                "issue_id": "I1",
                "evidence_ids": [],
            }
        ]
    return {
        "checked_block_ids": ids,
        "coverage": categories,
        "issues": issues,
        "repairs": repairs,
        "claims": (
            [
                {
                    "block_id": block_id,
                    "claim": block_id,
                    "temporal_scope": "",
                    "depends_on": [],
                    "evidence_block_ids": [],
                }
                for block_id in ids
            ]
            if unit.get("role") != "references"
            else []
        ),
        "needs_original_block_ids": [],
    }


def test_v26_graph_does_not_mutate_prior_definitions():
    v24, v25, v26 = (
        manuscript_rewrite_workflow_v24(),
        manuscript_rewrite_workflow_v25(),
        manuscript_rewrite_workflow_v26(),
    )
    assert v24.next_nodes("validate_reference_quality", "success") == ["validate_final_coherence"]
    assert v25.next_nodes("validate_reference_quality", "success") == ["validate_final_coherence"]
    assert v26.next_nodes("validate_reference_quality", "success") == [
        "validate_rendered_structure"
    ]
    for node_id in ("plan_research_queries", "analyze_japanese_structure"):
        assert next(n for n in v25.nodes if n.id == node_id).resource_lane == "ingestion_cpu"
        assert next(n for n in v26.nodes if n.id == node_id).resource_lane == "ingestion_cpu"
    assert (
        next(n for n in v26.nodes if n.id == "validate_final_coherence").resource_lane
        == "ingestion_cpu"
    )


def test_v29_parallelization_map_matches_workflow_graph():
    source = (
        Path(__file__).resolve().parents[1]
        / "docs" / "diagrams" / "manuscript-rewrite.workflow.json"
    )
    diagram = json.loads(source.read_text(encoding="utf-8"))
    workflow = manuscript_rewrite_workflow_v29()
    path = diagram["mainPath"]
    assert diagram["meta"]["workflowVersion"] == workflow.version
    assert len(path) == len(workflow.nodes) == 33
    assert set(path) == {node.id for node in workflow.nodes}
    assert path[0] == workflow.entry_node
    for source_id, target_id in zip(path, path[1:], strict=False):
        assert target_id in workflow.next_nodes(source_id, "success")
    lanes = {node.id: node.resource_lane for node in workflow.nodes}
    assert {node["id"]: node["resource_lane"] for node in diagram["nodes"]} == lanes


def test_rendered_structure_detects_terminal_violation_before_inference():
    broken = HTML.replace(
        "</article>",
        '<section id="L"><h2 id="LH">追加の議論</h2>'
        '<p id="LP">後続の主張。</p></section></article>',
    )
    report = submission_validation.rendered_structure_report(broken)
    assert not report["valid"]
    assert "terminal.argument_after_terminal" in {item["code"] for item in report["issues"]}


def test_rendered_structure_enforces_protected_and_read_only_blocks():
    changed = HTML.replace("主張を述べる。", "異なる主張を述べる。").replace(
        "著者『作品』、出版社、2020年。", "未確認の書誌。"
    )
    report = submission_validation.rendered_structure_report(
        changed,
        protected_html=HTML,
        protected_ids=["P1"],
        reference_html=HTML,
    )
    codes = {item["code"] for item in report["issues"]}
    assert "structure.protected_block_changed" in codes
    assert "structure.read_only_reference_changed" in codes


def test_footnote_rendering_may_create_one_terminal_bibliography_section():
    context = {
        "reference_inventory": {"reference_section_count": 0},
        "evidence_footnote_render_report": {
            "policy": "evidence-footnotes-v1",
            "terminal_bibliography_addition_count": 14,
        },
    }

    assert submission_validation.expected_reference_section_count(context) == 1
    assert (
        submission_validation.expected_reference_section_count(
            {
                "reference_inventory": {"reference_section_count": 0},
                "evidence_footnote_render_report": {
                    "policy": "evidence-footnotes-v1",
                    "terminal_bibliography_addition_count": 0,
                    "cited_reference_count": 14,
                },
            }
        )
        == 1
    )


def test_v31_may_remove_an_imported_bibliography_when_every_entry_is_uncited():
    context = {
        "authoring_workflow_policy": {"workflow_version": 31},
        "reference_inventory": {"reference_section_count": 1},
        "evidence_footnote_render_report": {
            "policy": "evidence-footnotes-v1",
            "terminal_bibliography_addition_count": 0,
            "cited_reference_count": 0,
        },
        "reference_render_report": {
            "rule_set_id": "house-style",
            "entry_count": 0,
            "source_reference_count": 37,
            "uncited_reference_review_count": 37,
            "machine_section_rendered": False,
        },
    }

    assert submission_validation.expected_reference_section_count(context) == 0
    context["reference_render_report"]["uncited_reference_review_count"] = 36
    assert submission_validation.expected_reference_section_count(context) == 1
    assert (
        submission_validation.expected_reference_section_count(
            {"reference_inventory": {"reference_section_count": 0}}
        )
        == 0
    )
    assert (
        submission_validation.expected_reference_section_count(
            {
                "reference_inventory": {"reference_section_count": 1},
                "evidence_footnote_render_report": context["evidence_footnote_render_report"],
            }
        )
        == 1
    )


def test_v26_proposal_edits_are_tracked_without_freezing_imported_prose():
    changed = HTML.replace("主張を述べる。", "資料の範囲では主張を述べる。")
    report = submission_validation.rendered_structure_report(changed, source_html=HTML)
    assert report["valid"]
    assert (
        report["source_lineage"]["retained_block_count"]
        == report["source_lineage"]["source_block_count"]
    )
    assert not any(
        issue["code"] == "structure.human_protected_block_changed" for issue in report["issues"]
    )
    removed = changed.replace('<p id="P2">根拠を説明する。</p>', "")
    unmapped = submission_validation.rendered_structure_report(removed, source_html=HTML)
    assert "structure.unmapped_source_block" in {i["code"] for i in unmapped["issues"]}
    mapped = submission_validation.rendered_structure_report(
        removed,
        source_html=HTML,
        source_lineage=[{"source_block_ids": ["P1", "P2"], "output_block_id": "P1"}],
    )
    assert mapped["valid"]
    assert mapped["source_lineage"]["mapped_removed_block_ids"] == ["P2"]


def test_v29_whole_document_lineage_covers_replaced_source_blocks():
    output = HTML.replace('id="H"', 'id="NEW-H"').replace('id="P1"', 'id="NEW-P1"')
    output = output.replace('id="P2"', 'id="NEW-P2"')
    lineage = final_worker_v26._complete_authoring_source_lineage(
        source_html=HTML,
        output_html=output,
        source_lineage=[],
    )
    report = submission_validation.rendered_structure_report(
        output,
        source_html=HTML,
        source_lineage=lineage,
    )
    assert report["valid"]
    assert report["source_lineage"]["unmapped_removed_block_ids"] == []
    assert {item["source_block_ids"][0] for item in lineage} == {
        "H",
        "P1",
        "P2",
        "RH",
        "RP",
    }


def test_v26_final_judgement_keeps_unchecked_audits_reviewable(worker_case, monkeypatch):
    task = claimed(worker_case, "validate_final_coherence")
    sha = submission_validation.digest(HTML)
    monkeypatch.setattr(
        worker_case.worker,
        "_manuscript_completion_gate",
        lambda _context: {"quality_status": "ready", "blocking_count": 0},
    )
    context = {
        "proposed_html": HTML,
        "frozen_candidate_v26": {"html_sha256": sha},
        "v26_coherence_report": {
            "html_sha256": sha,
            "issues": [
                {
                    "category": "missing_warrant",
                    "resolution_route": "additional_research",
                    "severity": "warning",
                }
            ],
            "unit_complete": False,
            "coverage": [],
        },
        "v26_block_verification_records": [],
        "final_discourse_sha256_v26": sha,
        "final_discourse_review": [],
        "final_finding_audit": {
            "source_sha256": sha,
            "expected_ids": ["F1"],
            "verdicts": [{"unit_id": "F1", "status": "not_revalidated", "source_sha256": sha}],
        },
        "reference_quality_gate": {"passed": False, "target_html_sha256": sha},
        "manuscript_surface_quality_gate": {"passed": True, "target_html_sha256": sha},
    }
    result = worker_case.worker._manuscript_final_judgement_v26(task, context)
    assert result.outcome == "conditional_success"
    assert result.context["v26_audit_coverage"]["coherence_complete"] is False
    assert result.context["v26_audit_coverage"]["unreviewed_finding_ids"] == ["F1"]
    assert result.context["v26_audit_coverage"]["unchecked_block_ids"]
    assert any(
        item["code"] == "coherence.v26.missing_warrant"
        for item in result.context["completion_gate"]["blocking_reasons"]
    )


def test_v26_final_judgement_keeps_economy_surface_issue_reviewable(worker_case, monkeypatch):
    task = claimed(worker_case, "validate_final_coherence")
    sha = submission_validation.digest(HTML)
    monkeypatch.setattr(
        worker_case.worker,
        "_manuscript_completion_gate",
        lambda _context: {"quality_status": "ready", "blocking_count": 0},
    )
    context = {
        "proposed_html": HTML,
        "economy_priority": True,
        "frozen_candidate_v26": {"html_sha256": sha},
        "v26_coherence_report": {
            "html_sha256": sha,
            "passed": False,
            "issues": [],
            "unit_complete": False,
            "coverage": [],
        },
        "v26_block_verification_records": [],
        "final_discourse_sha256_v26": sha,
        "final_discourse_review": [],
        "final_finding_audit": {
            "source_sha256": sha,
            "expected_ids": [],
            "verdicts": [],
        },
        "reference_quality_gate": {"passed": True, "target_html_sha256": sha},
        "manuscript_surface_quality_gate": {
            "passed": False,
            "review_required": True,
            "target_html_sha256": sha,
            "remaining_issue_blocks": [{"block_id": "P1"}],
        },
    }

    result = worker_case.worker._manuscript_final_judgement_v26(task, context)

    assert result.outcome == "conditional_success"
    assert any(
        item["code"] == "surface.v26.human_review_required"
        for item in result.context["completion_gate"]["blocking_reasons"]
    )


def test_v26_replays_failed_structure_on_same_node_identity(worker_case):
    run_id = worker_case._create_run("manuscript_rewrite")
    error = {"type": "CoherenceResolutionRequired", "message": "rendered_structure.invalid: []"}
    with worker_case.worker.databases.state.connect() as connection:
        entry_id = connection.execute(
            "SELECT id FROM node_runs WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        connection.execute(
            "UPDATE node_runs SET node_id='validate_reference_quality', "
            "status='completed' WHERE id=?",
            (entry_id,),
        )
        connection.execute(
            "INSERT INTO node_runs(id,run_id,node_id,attempt,status,input_json,resource_lane,"
            "error_json,completed_at) VALUES (?,?,?,1,'failed',?,'ingestion_cpu',?,?)",
            (
                "v26-failed-structure",
                run_id,
                "validate_rendered_structure",
                json.dumps({"proposed_html": HTML}),
                json.dumps(error),
                "old-time",
            ),
        )
        connection.execute(
            "UPDATE runs SET workflow_version='26',status='failed',error_json=?,"
            "completed_at=? WHERE id=?",
            (json.dumps(error), "old-time", run_id),
        )
        connection.commit()
        resumed = RunRepository(connection).replay_failed_v26_structure(run_id)
        assert resumed["node_run_id"] == "v26-failed-structure"
        assert resumed["attempt"] == 1
        assert RunRepository(connection).get(run_id)["status"] == "queued"
        node = connection.execute(
            "SELECT status,error_json,started_at FROM node_runs WHERE id=?",
            (resumed["node_run_id"],),
        ).fetchone()
        assert (node["status"], node["error_json"], node["started_at"]) == ("queued", None, None)
        event = connection.execute(
            "SELECT payload_json FROM event_log WHERE run_id=? "
            "AND event_type='run.failed_node_replayed'",
            (run_id,),
        ).fetchone()
        assert json.loads(event[0])["previous_node_error"] == error
        with pytest.raises(Exception, match="isolated failed V26"):
            RunRepository(connection).replay_failed_v26_structure(run_id)


def test_v26_records_every_block_and_rechecks_after_local_repair(worker_case, monkeypatch):
    task = claimed(worker_case, "repair_final_coherence")
    calls = []

    def audit(_claimed, method, **kwargs):
        assert method == "audit_final_coherence_v26"
        calls.append((kwargs["mode"], list(kwargs["unit"]["block_ids"])))
        return fake_report(
            kwargs["mode"],
            kwargs["unit"],
            kwargs["categories"],
            issue=len(calls) <= 2,
        )

    monkeypatch.setattr(worker_case.worker, "_invoke_manuscript_inference", audit)
    context = {
        "proposed_html": HTML,
        "structure_plan": {},
        "paragraph_plan": {},
        "evidence_context": [],
        "reasoning_review": [],
        "applied_edits": [],
        "rewrite_warnings": [],
        "reference_rendered_html_v26": HTML,
    }
    updated, outcome = final_worker_v26.audit_coherence(
        worker_case.worker, task, context, stage="post_polish"
    )
    assert outcome == "success"
    assert "資料の範囲では" in updated["proposed_html"]
    assert updated["v26_coherence_report"]["pass_index"] >= 2
    assert updated["v26_coherence_report"]["html_sha256"] == submission_validation.digest(
        updated["proposed_html"]
    )
    assert submission_validation.verified_coverage(
        updated["v26_block_verification_records"],
        ["H", "P1", "P2", "RH", "RP"],
        submission_validation.digest(updated["proposed_html"]),
    )
    assert sum("P1" in ids for _, ids in calls) >= 2


def test_section_slice_projects_authoring_source_provenance():
    [body] = [
        item
        for item in submission_validation.section_slices(
            '<article id="D" lang="ja"><section id="S">'
            '<h2 id="H-plan-S">本文</h2><p id="P1">作品の配置は差異を見せる。</p>'
            "</section></article>",
            {
                "structure_plan": {},
                "paragraph_plan": {},
                "section_contracts": [
                    {
                        "section_id": "S",
                        "claim_ids": ["c1"],
                        "evidence_ids": ["e1"],
                        "warrant_ids": [],
                        "qualifier_ids": [],
                    }
                ],
                "claims": [
                    {
                        "id": "c1",
                        "proposition_id": "p1",
                        "provenance": {"source_ids": ["p1"]},
                    }
                ],
                "argument_nodes": [
                    {
                        "id": "e1",
                        "claim_id": "c1",
                        "proposition_id": "p1",
                        "node_type": "evidence",
                        "text": "作者による作品の読解",
                    }
                ],
                "propositions": [
                    {
                        "id": "p1",
                        "text": "作品の配置は差異を見せる。",
                        "source_span": "作品の配置は差異を見せる。",
                        "provenance": {"source": "memo"},
                    }
                ],
                "authoring_atomic_evidence_contracts": [
                    {
                        "proposition_id": "p1",
                        "claim_ids": ["c1"],
                        "realization": "attribute_to_memo",
                    }
                ],
            },
        )
        if item["section_id"].endswith("-S")
    ]

    provenance = body["authoring_source_provenance"]
    assert provenance["section_contract"]["claim_ids"] == ["c1"]
    assert [item["id"] for item in provenance["propositions"]] == ["p1"]
    assert provenance["atomic_evidence_contracts"][0]["realization"] == ("attribute_to_memo")


def test_coherence_packet_projects_only_paragraph_authoring_provenance():
    extra_ids = [f"p-extra-{index:03d}" for index in range(180)]
    context = {
        "structure_plan": {},
        "paragraph_plan": {
            "paragraphs": [
                {
                    "paragraph_id": "S:p1",
                    "output_block_id": "P1",
                    "source_block_ids": [],
                    "source_unit_ids": [],
                    "claim_ids": ["c1"],
                    "evidence_ids": [],
                    "argument_evidence_node_ids": ["e1"],
                    "warrant_ids": [],
                    "qualifier_ids": [],
                    "counterargument_ids": [],
                }
            ]
        },
        "section_contracts": [
            {
                "section_id": "S",
                "claim_ids": ["c1"],
                "evidence_ids": ["e1"],
                "warrant_ids": [],
                "qualifier_ids": [],
            }
        ],
        "claims": [
            {
                "id": "c1",
                "proposition_id": "p-root",
                "provenance": {"source_ids": extra_ids},
            }
        ],
        "argument_nodes": [
            {
                "id": "claim-node",
                "claim_id": "c1",
                "node_type": "claim",
                "proposition_id": "p-root",
                "provenance": {"source_ids": extra_ids},
            },
            {
                "id": "e1",
                "claim_id": "c1",
                "node_type": "evidence",
                "proposition_id": "p-evidence",
            },
            *[
                {
                    "id": f"unused-{index:03d}",
                    "claim_id": "c1",
                    "node_type": "evidence",
                    "proposition_id": proposition_id,
                }
                for index, proposition_id in enumerate(extra_ids)
            ],
        ],
        "propositions": [
            {"id": "p-root", "text": "主張"},
            {"id": "p-evidence", "text": "直接根拠"},
            *[{"id": proposition_id, "text": "長い補助命題" * 20} for proposition_id in extra_ids],
        ],
        "authoring_atomic_evidence_contracts": [
            {
                "proposition_id": proposition_id,
                "claim_ids": ["c1"],
                "assertion": "長い根拠契約" * 20,
                "realization": "qualified_inference",
            }
            for proposition_id in ["p-root", "p-evidence", *extra_ids]
        ],
    }
    [section] = [
        item
        for item in submission_validation.section_slices(
            '<article id="D" lang="ja"><section id="S">'
            '<h2 id="H-plan-S">本文</h2><p id="P1">主張を述べる。</p>'
            "</section></article>",
            context,
        )
        if item["section_id"].endswith("-S")
    ]
    paragraph = next(block for block in section["blocks"] if block["id"] == "P1")

    packet = final_worker_v26._packet(section, [paragraph])
    projected = packet["authoring_source_provenance"]

    assert len(json.dumps(packet, ensure_ascii=False)) < 90_000
    assert {item["id"] for item in projected["argument_nodes"]} == {
        "claim-node",
        "e1",
    }
    assert {item["id"] for item in projected["propositions"]} == {
        "p-root",
        "p-evidence",
    }
    assert {item["proposition_id"] for item in projected["atomic_evidence_contracts"]} == {
        "p-root",
        "p-evidence",
    }
    assert projected["unprojected_source_proposition_ids"] == extra_ids


def test_v26_conditional_audit_transports_unresolved_routes(worker_case, monkeypatch):
    task = claimed(worker_case, "repair_final_coherence")

    def audit(_task, _method, **kwargs):
        report = fake_report(kwargs["mode"], kwargs["unit"], kwargs["categories"])
        if kwargs["mode"] == "section" and "P1" in kwargs["unit"]["block_ids"]:
            report["issues"] = [
                {
                    "id": "research-1",
                    "category": "missing_warrant",
                    "severity": "blocking",
                    "block_ids": ["P1"],
                    "explanation": "受容資料が必要",
                    "resolution_route": "additional_research",
                    "research_query": "同時期の受容資料",
                }
            ]
        return report

    monkeypatch.setattr(worker_case.worker, "_invoke_manuscript_inference", audit)
    updated, outcome = final_worker_v26.audit_coherence(
        worker_case.worker,
        task,
        {
            "proposed_html": HTML,
            "structure_plan": {},
            "paragraph_plan": {},
            "evidence_context": [],
            "reasoning_review": [],
            "applied_edits": [],
        },
        stage="post_polish",
        allow_repairs=False,
    )

    assert outcome == "conditional_success"
    assert updated["completion_status"] == "conditional_success"
    assert updated["conditional_success"]["reason"] == "coherence_unresolved"
    assert updated["conditional_success"]["route_counts"] == {"additional_research": 1}
    assert updated["coherence_unresolved_routes"][0]["research_query"] == ("同時期の受容資料")


def test_three_coherence_passes_keep_each_unit_bounded(worker_case, monkeypatch):
    task = claimed(worker_case, "repair_final_coherence")
    calls = []

    def audit(_task, _method, **kwargs):
        unit = kwargs["unit"]
        calls.append((kwargs["mode"], len(json.dumps(unit, ensure_ascii=False))))
        report = fake_report(kwargs["mode"], unit, kwargs["categories"])
        if kwargs["mode"] != "section" or "P1" not in unit["block_ids"]:
            return report
        body = next(item["text"] for item in unit["blocks"] if item["id"] == "P1")
        replacement = (
            "一部の資料では主張が成り立つ。"
            if "主張を述べる" in body
            else "資料の範囲では主張が成り立つ。"
            if "一部の資料" in body
            else None
        )
        if replacement:
            report["issues"] = [
                {
                    "id": "I1",
                    "category": "missing_warrant",
                    "severity": "blocking",
                    "block_ids": ["P1"],
                    "explanation": "限定が必要",
                    "resolution_route": "claim_limitation",
                    "research_query": None,
                }
            ]
            report["repairs"] = [
                {
                    "block_id": "P1",
                    "text": replacement,
                    "issue_id": "I1",
                    "evidence_ids": [],
                }
            ]
        return report

    monkeypatch.setattr(worker_case.worker, "_invoke_manuscript_inference", audit)
    updated, outcome = final_worker_v26.audit_coherence(
        worker_case.worker,
        task,
        {
            "proposed_html": HTML,
            "structure_plan": {},
            "paragraph_plan": {},
            "evidence_context": [],
            "reasoning_review": [],
            "applied_edits": [],
        },
        stage="post_polish",
    )
    assert outcome == "success"
    assert updated["v26_coherence_report"]["pass_index"] == 3
    assert updated["v26_coherence_report"]["html_sha256"] == submission_validation.digest(
        updated["proposed_html"]
    )
    assert len([mode for mode, _ in calls if mode == "section"]) >= 3
    assert max(size for _, size in calls) < 90_000


def test_freeze_refuses_silent_surface_changes(worker_case):
    context = {
        "proposed_html": HTML.replace("主張を述べる。", "主張を述べる。。"),
        "v26_coherence_report": {"html_sha256": "wrong"},
    }
    with pytest.raises(CoherenceResolutionRequired):
        final_worker_v26.freeze(worker_case.worker, context)


def test_freeze_accepts_generated_reference_sections_after_surface_gate(worker_case):
    source = HTML
    final = HTML.replace(
        '<section id="R">',
        '<section id="FN" data-citation-section="evidence-footnotes-v1">'
        '<h2 id="FNH">注</h2><p id="FNP">注記。</p></section><section id="R">',
    )
    source_sha = submission_validation.digest(source)
    frozen = final_worker_v26.freeze(
        worker_case.worker,
        {
            "proposed_html": final,
            "pre_polish_html": source,
            "manuscript_surface_quality_gate": {
                "passed": True,
                "target_html_sha256": source_sha,
            },
            "evidence_footnote_render_report": {
                "source_html_sha256": source_sha,
            },
            "reference_render_report": {
                "surface_quality": {"passed": True},
            },
            "v26_coherence_report": {"html_sha256": submission_validation.digest(final)},
        },
    )

    assert frozen["frozen_candidate_v26"]["html_sha256"] == submission_validation.digest(final)


def test_freeze_preserves_economy_surface_issue_for_human_review(worker_case):
    html = HTML.replace("主張を述べる。", "主張を述べる。。")
    sha = submission_validation.digest(html)

    frozen = final_worker_v26.freeze(
        worker_case.worker,
        {
            "proposed_html": html,
            "pre_polish_html": html,
            "economy_priority": True,
            "manuscript_surface_quality_gate": {
                "passed": False,
                "target_html_sha256": sha,
            },
            "evidence_footnote_render_report": {"source_html_sha256": sha},
            "reference_render_report": {
                "surface_quality": {"passed": True},
            },
            "v26_coherence_report": {"html_sha256": sha},
        },
    )

    assert frozen["manuscript_surface_quality_gate"]["passed"] is False
    assert frozen["manuscript_surface_quality_gate"]["review_required"] is True
    assert frozen["required_user_information"][-1]["code"] == (
        "authoring.japanese_surface_review_required"
    )


def test_freeze_removes_stale_surface_review_after_clean_rescan(worker_case):
    sha = submission_validation.digest(HTML)

    frozen = final_worker_v26.freeze(
        worker_case.worker,
        {
            "proposed_html": HTML,
            "pre_polish_html": HTML,
            "economy_priority": True,
            "manuscript_surface_quality_gate": {
                "passed": True,
                "review_required": True,
                "target_html_sha256": sha,
            },
            "evidence_footnote_render_report": {"source_html_sha256": sha},
            "reference_render_report": {
                "surface_quality": {"passed": True},
            },
            "v26_coherence_report": {"html_sha256": sha},
            "required_user_information": [
                {
                    "code": "authoring.japanese_surface_review_required",
                    "affected_scope": "japanese_surface_quality",
                },
                {"code": "keep", "affected_scope": "content"},
            ],
        },
    )

    assert frozen["manuscript_surface_quality_gate"]["passed"] is True
    assert frozen["manuscript_surface_quality_gate"]["review_required"] is False
    assert frozen["required_user_information"] == [{"code": "keep", "affected_scope": "content"}]


def test_315_blocks_receive_application_records(worker_case, monkeypatch):
    task = claimed(worker_case, "repair_final_coherence")
    paragraphs = "".join(f'<p id="P{i}">検証対象{i}。</p>' for i in range(312))
    html = (
        '<article id="D" lang="ja"><section id="S"><h2 id="H">本文</h2>'
        + paragraphs
        + '</section><section id="R"><h2 id="RH">参考文献</h2>'
        '<p id="RP">著者『作品』、2020年。</p></section></article>'
    )
    monkeypatch.setattr(
        worker_case.worker,
        "_invoke_manuscript_inference",
        lambda _task, _method, **kwargs: fake_report(
            kwargs["mode"], kwargs["unit"], kwargs["categories"]
        ),
    )
    updated, outcome = final_worker_v26.audit_coherence(
        worker_case.worker,
        task,
        {
            "proposed_html": html,
            "structure_plan": {},
            "paragraph_plan": {},
            "evidence_context": [],
            "reasoning_review": [],
            "applied_edits": [],
        },
        stage="post_polish",
    )
    assert outcome == "success"
    assert len(updated["v26_block_verification_records"]) == 315
    assert submission_validation.verified_coverage(
        updated["v26_block_verification_records"],
        updated["v26_coherence_report"]["checked_block_ids"],
        submission_validation.digest(html),
    )


def test_final_judgement_keeps_unconfirmed_bibliography_conditional(worker_case, monkeypatch):
    task = claimed(worker_case, "validate_final_coherence")
    monkeypatch.setattr(
        worker_case.worker,
        "_invoke_manuscript_inference",
        lambda _task, _method, **kwargs: fake_report(
            kwargs["mode"], kwargs["unit"], kwargs["categories"]
        ),
    )
    context, _ = final_worker_v26.audit_coherence(
        worker_case.worker,
        task,
        {
            "proposed_html": HTML,
            "structure_plan": {},
            "paragraph_plan": {},
            "evidence_context": [],
            "reasoning_review": [],
            "applied_edits": [],
        },
        stage="post_polish",
    )
    context = final_worker_v26.freeze(worker_case.worker, context)
    context.update(
        {
            "final_discourse_sha256_v26": submission_validation.digest(HTML),
            "final_finding_audit": {
                "source_sha256": submission_validation.digest(HTML),
                "expected_ids": [],
                "verdicts": [],
            },
            "reference_quality_gate": {
                "passed": False,
                "issues": ["unconfirmed"],
                "target_html_sha256": submission_validation.digest(HTML),
            },
        }
    )
    monkeypatch.setattr(
        worker_case.worker,
        "_manuscript_completion_gate",
        lambda _context: {"quality_status": "ready", "blocking_count": 0},
    )
    result = worker_case.worker._manuscript_final_judgement_v26(task, context)
    assert result.outcome == "conditional_success"
    assert result.context["v26_final_judgement"]["html_sha256"] == submission_validation.digest(
        HTML
    )
    mismatched = {
        **context,
        "manuscript_surface_quality_gate": {
            **context["manuscript_surface_quality_gate"],
            "target_html_sha256": "stale",
        },
    }
    with pytest.raises(CoherenceResolutionRequired, match="surface_quality"):
        worker_case.worker._manuscript_final_judgement_v26(task, mismatched)


def test_full_prompt_budget_includes_common_and_supplement_contract(worker_case):
    adapter = CodexTranslationAdapter(worker_case.settings)
    schema = {"type": "object", "properties": {"items": {"type": "array"}}, "required": ["items"]}
    with CodexTranslationAdapter.mandatory_manuscript_execution(None):
        with pytest.raises(NonRetryableFukamiruError, match="prompt budget"):
            adapter._complete_json(
                "x" * 119_500,
                schema,
                operation="manuscript v26 probe",
                prompt_budget=120_000,
            )
        with pytest.raises(NonRetryableFukamiruError, match="prompt budget"):
            adapter._complete_json(
                "x" * 119_300,
                schema,
                operation="manuscript v26 probe",
                prompt_budget=120_000,
                partial_unit_arrays={"items": None},
            )


def test_v26_partial_response_supplements_only_missing_blocks(worker_case, monkeypatch):
    task = claimed(worker_case, "validate_rewrite_coherence")
    supplements = []

    def audit(_task, _method, **kwargs):
        unit = kwargs["unit"]
        if kwargs["mode"] == "section" and unit.get("role") == "argument":
            subset = {**unit, "block_ids": unit["block_ids"][:2], "blocks": unit["blocks"][:2]}
            return fake_report("section", subset, kwargs["categories"])
        if kwargs["mode"] == "section_supplement":
            supplements.append(list(unit["block_ids"]))
        return fake_report(kwargs["mode"], unit, kwargs["categories"])

    monkeypatch.setattr(worker_case.worker, "_invoke_manuscript_inference", audit)
    updated, outcome = final_worker_v26.audit_coherence(
        worker_case.worker,
        task,
        {
            "proposed_html": HTML,
            "structure_plan": {},
            "paragraph_plan": {},
            "evidence_context": [],
            "reasoning_review": [],
            "applied_edits": [],
        },
        stage="pre_polish",
    )
    assert outcome == "success"
    assert supplements == [["P2"]]
    assert set(updated["v26_coherence_report"]["checked_block_ids"]) == {
        "H",
        "P1",
        "P2",
        "RH",
        "RP",
    }


def test_cross_section_doubt_fetches_only_named_original_and_caches_by_html(
    worker_case, monkeypatch
):
    task = claimed(worker_case, "repair_final_coherence")
    calls = []

    def audit(_task, _method, **kwargs):
        calls.append((kwargs["mode"], kwargs["unit"]))
        report = fake_report(kwargs["mode"], kwargs["unit"], kwargs["categories"])
        if kwargs["mode"] == "cross_section":
            report["needs_original_block_ids"] = ["P1"]
        return report

    monkeypatch.setattr(worker_case.worker, "_invoke_manuscript_inference", audit)
    claims = [
        {
            "block_id": block_id,
            "section_id": "semantic:H",
            "claim": block_id,
            "temporal_scope": "",
            "depends_on": [],
            "evidence_block_ids": [],
        }
        for block_id in ("P1", "P2")
    ]
    reports, complete = final_worker_v26._cross_audit(
        worker_case.worker, task, claims, HTML, {"model": "test"}
    )
    assert complete and reports
    original_units = [unit for mode, unit in calls if mode == "cross_section_originals"]
    assert len(original_units) == 1
    assert [block["id"] for block in original_units[0]["original_blocks"]] == ["P1"]
    assert original_units[0]["block_ids"] == ["P1"]
    before = len(calls)
    final_worker_v26._cross_audit(worker_case.worker, task, claims, HTML, {"model": "test"})
    assert len(calls) == before
    changed = HTML.replace("主張を述べる。", "対象を限定して主張を述べる。")
    final_worker_v26._cross_audit(worker_case.worker, task, claims, changed, {"model": "test"})
    assert len(calls) > before


def test_unresolved_author_judgement_cannot_be_downgraded_to_warning(worker_case, monkeypatch):
    task = claimed(worker_case, "repair_final_coherence")

    def audit(_task, _method, **kwargs):
        report = fake_report(kwargs["mode"], kwargs["unit"], kwargs["categories"])
        if kwargs["mode"] == "section" and "P1" in kwargs["unit"]["block_ids"]:
            report["issues"] = [
                {
                    "id": "author-choice",
                    "category": "missing_warrant",
                    "severity": "warning",
                    "block_ids": ["P1"],
                    "explanation": "著者の判断が必要",
                    "resolution_route": "human_review",
                    "research_query": None,
                }
            ]
        return report

    monkeypatch.setattr(worker_case.worker, "_invoke_manuscript_inference", audit)
    with pytest.raises(CoherenceResolutionRequired, match="semantic_response_contract_invalid"):
        final_worker_v26.audit_coherence(
            worker_case.worker,
            task,
            {
                "proposed_html": HTML,
                "structure_plan": {},
                "paragraph_plan": {},
                "evidence_context": [],
                "reasoning_review": [],
                "applied_edits": [],
            },
            stage="post_polish",
        )


def test_unresolved_author_judgement_remains_conditional(worker_case, monkeypatch):
    task = claimed(worker_case, "repair_final_coherence")

    def audit(_task, _method, **kwargs):
        report = fake_report(kwargs["mode"], kwargs["unit"], kwargs["categories"])
        if kwargs["mode"] == "section" and "P1" in kwargs["unit"]["block_ids"]:
            report["issues"] = [
                {
                    "id": "author-choice",
                    "category": "missing_warrant",
                    "severity": "blocking",
                    "block_ids": ["P1"],
                    "explanation": "著者の判断が必要",
                    "resolution_route": "human_review",
                    "research_query": None,
                }
            ]
        return report

    monkeypatch.setattr(worker_case.worker, "_invoke_manuscript_inference", audit)
    updated, outcome = final_worker_v26.audit_coherence(
        worker_case.worker,
        task,
        {
            "proposed_html": HTML,
            "structure_plan": {},
            "paragraph_plan": {},
            "evidence_context": [],
            "reasoning_review": [],
            "applied_edits": [],
        },
        stage="post_polish",
    )
    assert outcome == "conditional_success"
    assert updated["v26_coherence_report"]["passed"] is False


def test_cross_section_audit_checks_nonadjacent_dependency(worker_case, monkeypatch):
    task = claimed(worker_case, "repair_final_coherence")
    html = HTML.replace(
        '<section id="R">',
        '<section id="M"><h2 id="MH">中間</h2><p id="MP">中間の説明。</p></section>'
        '<section id="T"><h2 id="TH">結論</h2><p id="TP">結論。</p></section>'
        '<section id="R">',
    )
    pairs = []

    def audit(_task, _method, **kwargs):
        pairs.append(tuple(kwargs["unit"]["related_sections"]))
        return fake_report(kwargs["mode"], kwargs["unit"], kwargs["categories"])

    monkeypatch.setattr(worker_case.worker, "_invoke_manuscript_inference", audit)
    claims = [
        {
            "block_id": "P1",
            "section_id": "semantic:H",
            "claim": "主張",
            "temporal_scope": "",
            "depends_on": ["TP"],
            "evidence_block_ids": [],
        },
        {
            "block_id": "TP",
            "section_id": "semantic:TH",
            "claim": "結論",
            "temporal_scope": "",
            "depends_on": [],
            "evidence_block_ids": [],
        },
    ]
    _, complete = final_worker_v26._cross_audit(
        worker_case.worker, task, claims, html, {"model": "test"}
    )
    assert complete
    assert ("semantic:H", "semantic:TH") in pairs
