from __future__ import annotations

import threading
import time
from dataclasses import replace

import pytest

from fukamiru.errors import ExternalServiceUnavailable
from fukamiru.revisions import submission_validation
from fukamiru.worker import coherence_certification
from fukamiru.worker import submission_validation as coherence_core
from fukamiru.workflow.templates import (
    manuscript_rewrite_workflow,
    manuscript_rewrite_workflow_v26,
    manuscript_rewrite_workflow_v27,
    manuscript_rewrite_workflow_v28,
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
    return replace(task, node_id=node_id, workflow_version="27", resource_lane="codex_remote")


def fake_report(mode, unit, categories):
    ids = list(unit["block_ids"])
    return {
        "checked_block_ids": ids,
        "coverage": categories,
        "issues": [],
        "repairs": [],
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


def context(html: str = HTML):
    return {
        "proposed_html": html,
        "structure_plan": {},
        "paragraph_plan": {},
        "evidence_context": [],
        "reasoning_review": [],
        "applied_edits": [],
        "rewrite_warnings": [],
    }


def test_v27_is_preserved_and_v28_changes_no_graph_handlers():
    v26 = manuscript_rewrite_workflow_v26()
    v27 = manuscript_rewrite_workflow_v27()
    v28 = manuscript_rewrite_workflow_v28()
    assert manuscript_rewrite_workflow().version == "29"
    assert v26.version == "26"
    assert v27.version == "27"
    assert v28.version == "28"
    assert [node.id for node in v27.nodes] == [node.id for node in v26.nodes]
    assert [edge.model_dump() for edge in v27.edges] == [
        edge.model_dump() for edge in v26.edges
    ]
    handlers = {node.id: node.handler for node in v27.nodes}
    assert handlers["validate_rewrite_coherence"].endswith("validate_coherence_v27")
    assert handlers["repair_final_coherence"].endswith("repair_final_coherence_v27")
    assert [node.model_dump() for node in v28.nodes] == [
        node.model_dump() for node in v27.nodes
    ]
    assert [edge.model_dump() for edge in v28.edges] == [
        edge.model_dump() for edge in v27.edges
    ]


def test_v27_section_audits_run_concurrently(worker_case, monkeypatch):
    task = claimed(worker_case, "validate_rewrite_coherence")
    lock = threading.Lock()
    active = 0
    peak = 0

    def audit(_task, _method, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return fake_report(kwargs["mode"], kwargs["unit"], kwargs["categories"])

    monkeypatch.setattr(worker_case.worker, "_invoke_manuscript_inference", audit)
    updated, outcome = coherence_certification.audit_coherence(
        worker_case.worker, task, context(), stage="pre_polish"
    )
    assert outcome == "success"
    assert updated["v26_coherence_report"]["policy"] == coherence_certification.POLICY
    assert peak >= 2


def test_v27_skips_cross_audit_when_section_coverage_is_incomplete(
    worker_case, monkeypatch
):
    task = claimed(worker_case, "validate_rewrite_coherence")
    modes = []

    def audit(_task, _method, **kwargs):
        modes.append(kwargs["mode"])
        if kwargs["unit"].get("role") == "argument":
            return {}
        return fake_report(kwargs["mode"], kwargs["unit"], kwargs["categories"])

    monkeypatch.setattr(worker_case.worker, "_invoke_manuscript_inference", audit)
    updated, outcome = coherence_certification.audit_coherence(
        worker_case.worker, task, context(), stage="pre_polish"
    )
    assert outcome == "conditional_success"
    assert updated["v26_coherence_report"]["unit_complete"] is False
    assert "cross_section" not in modes


def test_v27_cross_section_audits_run_concurrently(worker_case, monkeypatch):
    task = claimed(worker_case, "repair_final_coherence")
    html = HTML.replace(
        '<section id="R">',
        '<section id="M"><h2 id="MH">中間</h2><p id="MP">中間の説明。</p></section>'
        '<section id="T"><h2 id="TH">結論</h2><p id="TP">結論。</p></section>'
        '<section id="R">',
    )
    lock = threading.Lock()
    active = 0
    peak = 0

    def audit(_task, _method, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return fake_report(kwargs["mode"], kwargs["unit"], kwargs["categories"])

    monkeypatch.setattr(worker_case.worker, "_invoke_manuscript_inference", audit)
    claims = [
        {
            "block_id": block_id,
            "section_id": section_id,
            "claim": block_id,
            "temporal_scope": "",
            "depends_on": [],
            "evidence_block_ids": [],
        }
        for block_id, section_id in (
            ("P1", "semantic:H"),
            ("MP", "semantic:MH"),
            ("TP", "semantic:TH"),
        )
    ]
    reports, complete = coherence_core._cross_audit(
        worker_case.worker,
        task,
        claims,
        html,
        {"model": "test"},
        policy=coherence_certification.POLICY,
        checkpoint_phase="coherence_cross:v27-test-parallel",
        parallelism=3,
        resume_partials=True,
    )
    assert complete
    assert reports
    assert peak >= 2


def test_v27_resumes_cross_section_original_supplement(worker_case, monkeypatch):
    task = claimed(worker_case, "repair_final_coherence")
    calls = []
    fail_once = True

    def audit(_task, _method, **kwargs):
        nonlocal fail_once
        mode = kwargs["mode"]
        calls.append(mode)
        if mode == "cross_section_originals" and fail_once:
            fail_once = False
            raise ExternalServiceUnavailable("temporary rate limit")
        report = fake_report(mode, kwargs["unit"], kwargs["categories"])
        if mode == "cross_section":
            report["needs_original_block_ids"] = ["P1"]
        return report

    monkeypatch.setattr(worker_case.worker, "_invoke_manuscript_inference", audit)
    claims = [
        {
            "block_id": "P1",
            "section_id": "semantic:H",
            "claim": "主張",
            "temporal_scope": "",
            "depends_on": [],
            "evidence_block_ids": [],
        }
    ]
    arguments = (
        worker_case.worker,
        task,
        claims,
        HTML,
        {"model": "test"},
    )
    options = {
        "policy": coherence_certification.POLICY,
        "checkpoint_phase": "coherence_cross:v27-test-resume",
        "parallelism": 1,
        "resume_partials": True,
    }
    with pytest.raises(ExternalServiceUnavailable):
        coherence_core._cross_audit(*arguments, **options)
    reports, complete = coherence_core._cross_audit(*arguments, **options)
    assert complete and reports
    assert calls == [
        "cross_section",
        "cross_section_originals",
        "cross_section_originals",
    ]


def test_v27_groups_original_blocks_for_one_cross_audit(worker_case, monkeypatch):
    task = claimed(worker_case, "repair_final_coherence")
    original_units = []

    def audit(_task, _method, **kwargs):
        mode = kwargs["mode"]
        unit = kwargs["unit"]
        if mode == "cross_section_originals":
            original_units.append(unit)
        report = fake_report(mode, unit, kwargs["categories"])
        if mode == "cross_section":
            report["needs_original_block_ids"] = ["P1", "P2"]
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
    arguments = (worker_case.worker, task, claims, HTML, {"model": "test"})
    options = {
        "policy": coherence_certification.POLICY,
        "checkpoint_phase": "coherence_cross:v27-test-group-originals",
        "parallelism": 1,
        "resume_partials": True,
        "group_original_supplements": True,
    }
    reports, complete = coherence_core._cross_audit(*arguments, **options)
    assert complete and reports
    assert len(original_units) == 1
    assert [block["id"] for block in original_units[0]["original_blocks"]] == [
        "P1", "P2",
    ]
    assert original_units[0]["block_ids"] == ["P1", "P2"]
    coherence_core._cross_audit(*arguments, **options)
    assert len(original_units) == 1


def test_v27_rechecks_after_serial_local_repair(worker_case, monkeypatch):
    task = claimed(worker_case, "repair_final_coherence")

    def audit(_task, _method, **kwargs):
        unit = kwargs["unit"]
        report = fake_report(kwargs["mode"], unit, kwargs["categories"])
        if kwargs["mode"] == "section" and "P1" in unit["block_ids"]:
            text = next(block["text"] for block in unit["blocks"] if block["id"] == "P1")
            if text == "主張を述べる。":
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
                        "text": "資料の範囲では主張が成り立つ。",
                        "issue_id": "I1",
                        "evidence_ids": [],
                    }
                ]
        return report

    monkeypatch.setattr(worker_case.worker, "_invoke_manuscript_inference", audit)
    updated, outcome = coherence_certification.audit_coherence(
        worker_case.worker, task, context(), stage="post_polish"
    )
    assert outcome == "success"
    assert "資料の範囲では" in updated["proposed_html"]
    assert updated["v26_coherence_report"]["pass_index"] == 2
    assert updated["v26_coherence_report"]["html_sha256"] == submission_validation.digest(
        updated["proposed_html"]
    )


def test_relationship_claim_rejects_missing_manuscript_block() -> None:
    packet = {"role": "body", "block_ids": ["P1"]}
    claim = {
        "block_id": "P1",
        "claim": "主張",
        "temporal_scope": "現在",
        "depends_on": ["P2"],
        "evidence_block_ids": [],
    }
    payload = {"claims": [claim]}
    assert coherence_core._claims_complete(
        payload, packet, valid_block_ids=frozenset({"P1", "P2"})
    )
    assert not coherence_core._claims_complete(
        payload, packet, valid_block_ids=frozenset({"P1"})
    )
