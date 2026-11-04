from __future__ import annotations

import json
from dataclasses import replace

import pytest

from fukamiru.errors import ExternalServiceUnavailable, FukamiruError


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
    return replace(claimed, workflow_version="22", resource_lane="codex_remote")


def run_records(worker, claimed, script, calls):
    units = [{"unit_id": "A"}, {"unit_id": "B"}]

    def invoke(selected):
        calls.append([item["unit_id"] for item in selected])
        response = script.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    return worker._complete_partial_response_v22(
        claimed,
        phase="fixture",
        signature_payload={"source": "same"},
        units=units,
        unit_id=lambda item: item["unit_id"],
        invoke=invoke,
        covered_unit_ids=lambda payload, _allowed: [
            item.get("unit_id", "")
            for item in payload.get("records", [])
            if isinstance(item, dict)
        ],
        select_accepted_payload=lambda payload, accepted, _allowed: {
            "records": [
                item
                for item in payload.get("records", [])
                if isinstance(item, dict) and item.get("unit_id") in accepted
            ]
        },
    )


def test_v22_partial_response_keeps_sibling_and_excludes_duplicate_unknown_records(
    worker_case,
):
    claimed = claimed_v22(worker_case)
    calls = []
    script = [
        {
            "records": [
                {"unit_id": "A", "value": "accepted"},
                {"unit_id": "B", "value": "duplicate-one"},
                {"unit_id": "B", "value": "duplicate-two"},
                {"unit_id": "UNKNOWN", "value": "outside-domain"},
            ]
        },
        {
            "records": [
                {"unit_id": "B", "value": "supplement"},
                {"unit_id": "A", "value": "must-not-reenter"},
            ]
        },
    ]

    payloads, terminal = run_records(worker_case.worker, claimed, script, calls)

    records = [record for payload in payloads for record in payload["records"]]
    assert calls == [["A", "B"], ["B"]]
    assert records == [
        {"unit_id": "A", "value": "accepted"},
        {"unit_id": "B", "value": "supplement"},
    ]
    assert terminal == []

    calls.clear()
    assert run_records(worker_case.worker, claimed, [], calls) == (payloads, terminal)
    assert calls == []


def test_v22_partial_response_retries_missing_ids_once_and_is_terminal(worker_case):
    claimed = claimed_v22(worker_case)
    calls = []
    script = [
        {"records": [{"unit_id": "A", "value": "accepted"}]},
        {"records": []},
    ]

    payloads, terminal = run_records(worker_case.worker, claimed, script, calls)

    assert calls == [["A", "B"], ["B"]]
    assert [record for payload in payloads for record in payload["records"]] == [
        {"unit_id": "A", "value": "accepted"}
    ]
    assert terminal == ["B"]

    calls.clear()
    assert run_records(worker_case.worker, claimed, [], calls) == (payloads, terminal)
    assert calls == []


def test_v22_transport_does_not_consume_targeted_supplement(worker_case):
    claimed = claimed_v22(worker_case)
    calls = []
    script = [
        {"records": [{"unit_id": "A", "value": "accepted"}]},
        ExternalServiceUnavailable("capacity"),
    ]

    with pytest.raises(ExternalServiceUnavailable):
        run_records(worker_case.worker, claimed, script, calls)
    assert calls == [["A", "B"], ["B"]]

    calls.clear()
    payloads, terminal = run_records(
        worker_case.worker,
        claimed,
        [{"records": [{"unit_id": "B", "value": "after-resume"}]}],
        calls,
    )
    assert calls == [["B"]]
    assert terminal == []
    assert [record["unit_id"] for payload in payloads for record in payload["records"]] == [
        "A",
        "B",
    ]


def test_v22_process_death_after_supplement_reservation_cannot_repeat_on_restart(
    worker_case,
):
    claimed = claimed_v22(worker_case)
    calls = []

    class ProcessDeath(SystemExit):
        pass

    with pytest.raises(ProcessDeath):
        run_records(
            worker_case.worker,
            claimed,
            [
                {"records": [{"unit_id": "A", "value": "accepted"}]},
                ProcessDeath("simulated process death"),
            ],
            calls,
        )
    assert calls == [["A", "B"], ["B"]]
    with worker_case.databases.state.connect() as connection:
        marker = json.loads(
            connection.execute(
                "SELECT payload_json FROM manuscript_unit_checkpoints "
                "WHERE run_id=? AND phase='partial-response-v22:fixture' "
                "AND unit_id='retry:B'",
                (claimed.run_id,),
            ).fetchone()[0]
        )
    assert marker["status"] == "in_progress"

    restart_calls = []
    payloads, terminal = run_records(
        worker_case.worker,
        claimed,
        [{"records": [{"unit_id": "B", "value": "must-not-run"}]}],
        restart_calls,
    )
    assert restart_calls == []
    assert terminal == ["B"]
    assert [record["unit_id"] for payload in payloads for record in payload["records"]] == [
        "A"
    ]
    with worker_case.databases.state.connect() as connection:
        marker = json.loads(
            connection.execute(
                "SELECT payload_json FROM manuscript_unit_checkpoints "
                "WHERE run_id=? AND phase='partial-response-v22:fixture' "
                "AND unit_id='retry:B'",
                (claimed.run_id,),
            ).fetchone()[0]
        )
    assert marker["status"] == "consumed"
    assert marker["outcome"] == "supplement_execution_interrupted"


def test_v22_partial_response_rejects_non_unique_application_ids(worker_case):
    claimed = claimed_v22(worker_case)
    with pytest.raises(FukamiruError, match="unique, nonempty"):
        worker_case.worker._complete_partial_response_v22(
            claimed,
            phase="fixture",
            signature_payload={},
            units=[{"id": "A"}, {"id": "A"}],
            unit_id=lambda item: item["id"],
            invoke=lambda _selected: {},
            covered_unit_ids=lambda _payload, _allowed: [],
            select_accepted_payload=lambda payload, _accepted, _allowed: payload,
        )


def test_v22_completion_gate_blocks_generic_response_contract_condition(worker_case):
    condition = {
        "reason": "response_contract_incomplete",
        "phase": "reference_inference",
        "unit_ids": ["REF-1"],
    }
    v22 = worker_case.worker._manuscript_completion_gate(
        {
            "workflow_version": 22,
            "conditional_conditions": [condition],
            "proposed_html": (
                '<article id="D" lang="ja"><section id="S">'
                '<h2 id="H">本文</h2><p id="P">本文。</p></section></article>'
            ),
        }
    )
    assert v22["quality_status"] == "needs_human_review"
    assert any(
        item["code"] == "response_contract.incomplete:reference_inference"
        for item in v22["blocking_reasons"]
    )

    v21 = worker_case.worker._manuscript_completion_gate(
        {
            "workflow_version": 21,
            "conditional_conditions": [condition],
            "proposed_html": (
                '<article id="D" lang="ja"><section id="S">'
                '<h2 id="H">本文</h2><p id="P">本文。</p></section></article>'
            ),
        }
    )
    assert not any(
        str(item["code"]).startswith("response_contract.incomplete:")
        for item in v21["blocking_reasons"]
    )


def test_v22_resolved_phase_clears_its_stale_response_contract_marker(worker_case):
    claimed = claimed_v22(worker_case)
    stale = {
        "reason": "response_contract_incomplete",
        "phase": "rewrite",
        "unit_ids": ["P-1"],
    }

    context = worker_case.worker._response_contract_context_v22(
        claimed,
        {
            "completion_status": "conditional_success",
            "conditional_success": stale,
            "conditional_conditions": [stale],
        },
        phase="rewrite",
        unit_ids=[],
    )

    assert context["completion_status"] == "success"
    assert "conditional_success" not in context
    assert context["conditional_conditions"] == []
