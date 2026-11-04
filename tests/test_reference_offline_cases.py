"""Independent fictional bibliography cases, intentionally offline."""

from __future__ import annotations

import json
import socket
from pathlib import Path

from fukamiru.revisions.reference_gold_regression import run_gold_regression

FIXTURE = Path(__file__).parent / "fixtures" / "bibliography_cases.json"


def test_fictional_bibliography_cases_are_fully_offline_and_pass(monkeypatch) -> None:
    def reject_network(*_args, **_kwargs):
        raise AssertionError("Offline regression must not access the network")

    monkeypatch.setattr(socket.socket, "connect", reject_network)
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    report, failures = run_gold_regression(fixture)
    assert fixture["network_required"] is False
    assert report["total"] == 35
    assert report["passed"] == 35, {
        index: {
            "actual_state": detail["actual"].get("reference_output_mode_v29"),
            "actual_candidates": detail["actual_candidates"],
            "expected_candidates": detail["expected_candidates"],
            "node22_issues": detail["node22_quality"].get("issues"),
        }
        for index, detail in failures.items()
    }
    assert report["failed"] == 0
    assert report["foreign_fallback_single"] == 1
    assert report["foreign_fallback_multiple"] == 1
    assert report["live_network_executed"] is False
