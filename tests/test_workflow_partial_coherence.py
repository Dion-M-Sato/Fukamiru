from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest

HTML = (
    '<article id="D" lang="ja"><section id="S"><h2 id="H">本文</h2>'
    '<p id="P1">第一の主張。</p><p id="P2">第二の説明。</p>'
    "</section></article>"
)

CATEGORIES = (
    "temporal_conflict",
    "connector_mismatch",
    "orphan_example",
    "missing_warrant",
    "argument_gap",
    "section_transition",
    "duplication",
    "ordering",
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


def claimed_coherence(case, *, version: int):
    run_id = case._create_run("manuscript_rewrite")
    claimed = case.worker._claim(lane="ingestion_cpu")
    assert claimed is not None and claimed.run_id == run_id
    return replace(
        claimed,
        node_id="validate_rewrite_coherence",
        workflow_version=str(version),
        resource_lane="codex_remote",
    )


def context() -> dict[str, Any]:
    return {
        "document_id": "D",
        "base_revision_id": "R",
        "document_title": "部分応答テスト",
        "instructions": "論理的一貫性を検証する。",
        "human_review": "",
        "proposed_html": HTML,
        "structure_plan": {},
        "structure_plan_validation": {"valid": True},
        "paragraph_plan": {},
        "paragraph_plan_validation": {"valid": True, "global_valid": True},
    }


def complete_report(worker, kwargs: dict[str, Any]) -> dict[str, Any]:
    manifest = dict(kwargs["document_manifest"])
    return {
        "passed": True,
        "summary": "全文と全カテゴリーを検証した。",
        "document_assertions": worker._coherence_manifest_assertions(manifest),
        "checked_block_ids": [str(block["id"]) for block in kwargs["blocks"]],
        "coverage": [
            {"category": category, "checked": True, "note": "検証済み。"}
            for category in CATEGORIES
        ],
        "issues": [],
        "repair_edits": [],
        "section_moves": [],
    }


def incomplete_global_report(worker, kwargs: dict[str, Any]) -> dict[str, Any]:
    report = complete_report(worker, kwargs)
    # Block coverage is complete, but the application-owned document contract is
    # not.  These global records must not leak from this rejected response slice.
    report["summary"] = ""
    report["issues"] = [
        {
            "id": "must-not-survive",
            "category": "duplication",
            "severity": "warning",
            "verification_scope": "semantic_content",
            "block_ids": ["P1"],
            "explanation": "不完全な全文応答にだけ含まれる指摘。",
            "recommendation": "採用してはならない。",
            "resolution_route": "none",
            "research_query": None,
        }
    ]
    report["repair_edits"] = [
        {
            "operation": "replace_text",
            "block_id": "P1",
            "text": "採用してはならない修復。",
            "review_finding_ids": ["must-not-survive"],
            "evidence_ids": [],
        }
    ]
    report["section_moves"] = [
        {
            "issue_id": "must-not-survive",
            "section_id": "semantic:H",
            "anchor_section_id": "semantic:H",
            "position": "before",
        }
    ]
    return report


def trace_partial_unit_calls(worker, monkeypatch) -> list[list[dict[str, Any]]]:
    original = worker._complete_partial_response_v22
    calls: list[list[dict[str, Any]]] = []

    def traced(*args: Any, **kwargs: Any):
        invoke: Callable[[list[dict[str, Any]]], dict[str, Any]] = kwargs["invoke"]
        unit_id: Callable[[dict[str, Any]], str] = kwargs["unit_id"]

        def observed(selected: list[dict[str, Any]]) -> dict[str, Any]:
            calls.append(
                [
                    {
                        "unit_id": str(unit_id(unit)),
                        "synthetic": unit.get("synthetic"),
                    }
                    for unit in selected
                ]
            )
            return invoke(selected)

        return original(*args, **{**kwargs, "invoke": observed})

    monkeypatch.setattr(worker, "_complete_partial_response_v22", traced)
    return calls


def test_v22_coherence_retries_only_synthetic_global_contract_and_keeps_siblings(
    worker_case, monkeypatch
):
    worker = worker_case.worker
    claimed = claimed_coherence(worker_case, version=22)
    unit_calls = trace_partial_unit_calls(worker, monkeypatch)
    responses = [incomplete_global_report, complete_report]
    inference_calls: list[dict[str, Any]] = []

    def invoke(_claimed, method_name: str, **kwargs: Any) -> dict[str, Any]:
        assert method_name == "validate_manuscript_coherence"
        inference_calls.append(kwargs)
        return responses.pop(0)(worker, kwargs)

    monkeypatch.setattr(worker, "_invoke_manuscript_inference", invoke)

    result = worker._manuscript_validate_coherence(claimed, context())

    assert result.outcome == "success"
    assert len(inference_calls) == 2
    assert len(unit_calls) == 2
    first_ids = [item["unit_id"] for item in unit_calls[0]]
    block_ids = {"H", "P1", "P2"}
    assert block_ids.issubset(first_ids)
    synthetic = [
        item
        for item in unit_calls[0]
        if item["synthetic"] == "document_contract"
    ]
    assert len(synthetic) == 1
    synthetic_id = synthetic[0]["unit_id"]
    assert synthetic_id.startswith("coherence-document:pre_polish:")
    assert unit_calls[1] == [
        {"unit_id": synthetic_id, "synthetic": "document_contract"}
    ]
    # Valid block siblings were checkpointed once.  The incomplete response's
    # global issue/repair/move arrays were owned by the synthetic unit and discarded.
    assert all(first_ids.count(block_id) == 1 for block_id in block_ids)
    final_report = result.context["manuscript_coherence_checks"][-1]
    assert final_report["passed"] is True
    assert final_report["summary"] == "全文と全カテゴリーを検証した。"
    assert final_report["issues"] == []
    assert final_report["repair_edits"] == []
    assert final_report["section_moves"] == []


def test_v22_coherence_second_incomplete_global_response_is_terminal(
    worker_case, monkeypatch
):
    worker = worker_case.worker
    claimed = claimed_coherence(worker_case, version=22)
    unit_calls = trace_partial_unit_calls(worker, monkeypatch)
    inference_calls: list[dict[str, Any]] = []

    def invoke(_claimed, method_name: str, **kwargs: Any) -> dict[str, Any]:
        assert method_name == "validate_manuscript_coherence"
        inference_calls.append(kwargs)
        return incomplete_global_report(worker, kwargs)

    monkeypatch.setattr(worker, "_invoke_manuscript_inference", invoke)

    first = worker._manuscript_validate_coherence(claimed, context())

    assert first.outcome == "conditional_success"
    assert first.context["completion_status"] == "conditional_success"
    condition = next(
        item
        for item in first.context["conditional_conditions"]
        if item["reason"] == "response_contract_incomplete"
        and item["phase"] == "coherence:pre_polish"
    )
    assert len(unit_calls) == 2
    synthetic_id = unit_calls[1][0]["unit_id"]
    assert unit_calls[1] == [
        {"unit_id": synthetic_id, "synthetic": "document_contract"}
    ]
    assert condition["unit_ids"] == [synthetic_id]
    assert len(inference_calls) == 2

    # The terminal content result is durable: replaying the handler neither sends
    # valid siblings nor spends a third content attempt.
    repeated = worker._manuscript_validate_coherence(claimed, context())
    assert repeated.outcome == "conditional_success"
    assert len(inference_calls) == 2
    assert len(unit_calls) == 2


def test_v21_coherence_does_not_use_synthetic_partial_response_unit(
    worker_case, monkeypatch
):
    worker = worker_case.worker
    claimed = claimed_coherence(worker_case, version=21)
    calls: list[dict[str, Any]] = []

    def forbidden(*_args: Any, **_kwargs: Any):
        raise AssertionError("V21 must not enter the V22 partial-response protocol")

    def invoke(_claimed, method_name: str, **kwargs: Any) -> dict[str, Any]:
        assert method_name == "validate_manuscript_coherence"
        calls.append(kwargs)
        return complete_report(worker, kwargs)

    monkeypatch.setattr(worker, "_complete_partial_response_v22", forbidden)
    monkeypatch.setattr(worker, "_invoke_manuscript_inference", invoke)

    result = worker._manuscript_validate_coherence(claimed, context())

    assert result.outcome == "success"
    assert len(calls) == 1
    assert {str(block["id"]) for block in calls[0]["blocks"]} == {"H", "P1", "P2"}
