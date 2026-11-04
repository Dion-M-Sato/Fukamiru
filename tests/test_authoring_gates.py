from __future__ import annotations

from fukamiru.authoring.gates import (
    evaluate_finalization_eligibility,
    evaluate_reference_quality_gate,
    evaluate_required_evidence_obligations,
)


def test_required_evidence_obligation_fails_without_accepted_evidence() -> None:
    result = evaluate_required_evidence_obligations(
        [{"id": "claim-1", "requires_evidence": True}], []
    )
    assert result["passed"] is False
    assert result["reasons"][0]["code"] == "required_evidence_missing"


def test_required_evidence_obligation_passes_only_with_linked_accepted_evidence() -> None:
    obligation = [{"id": "claim-1", "requires_evidence": True}]
    assert not evaluate_required_evidence_obligations(
        obligation, [{"id": "e1", "obligation_id": "claim-1", "status": "candidate"}]
    )["passed"]
    accepted = evaluate_required_evidence_obligations(
        obligation,
        [{"id": "e1", "obligation_id": "claim-1", "status": "verified"}],
    )
    assert accepted["passed"] is True


def test_requirement_levels_count_required_preferred_and_none() -> None:
    result = evaluate_required_evidence_obligations(
        [
            {"id": "required", "evidence_requirement": "required"},
            {"id": "preferred", "evidence_requirement": "preferred"},
            {"id": "none", "evidence_requirement": "none"},
        ],
        [],
    )
    assert result["passed"] is False
    assert result["required_count"] == 1
    assert result["preferred_count"] == 1
    assert result["none_count"] == 1
    assert result["missing_required_count"] == 1
    assert result["missing_required_obligation_ids"] == ["required"]
    assert result["preferred_without_evidence_obligation_ids"] == ["preferred"]


def test_preferred_evidence_is_auditable_but_does_not_fail_gate() -> None:
    result = evaluate_required_evidence_obligations(
        [{"id": "claim-1", "evidence_requirement": "preferred"}], []
    )
    assert result["passed"] is True
    assert result["preferred_count"] == 1
    assert result["preferred_without_evidence_count"] == 1
    assert result["missing_required_count"] == 0


def test_evidence_requirement_enum_overrides_legacy_boolean() -> None:
    result = evaluate_required_evidence_obligations(
        [
            {"id": "preferred", "evidence_requirement": "preferred", "requires_evidence": True},
            {"id": "required", "evidence_requirement": "required", "requires_evidence": False},
        ],
        [],
    )
    assert result["preferred_count"] == 1
    assert result["required_count"] == 1
    assert result["missing_required_count"] == 1
    assert result["passed"] is False


def test_legacy_requires_evidence_boolean_is_preserved() -> None:
    result = evaluate_required_evidence_obligations(
        [
            {"id": "legacy-required", "requires_evidence": True},
            {"id": "legacy-none", "requires_evidence": False},
            {"id": "legacy-default"},
        ],
        [],
    )
    assert result["required_count"] == 2
    assert result["none_count"] == 1
    assert result["missing_required_count"] == 2


def test_reference_quality_cannot_pass_vacuously_for_required_evidence() -> None:
    result = evaluate_reference_quality_gate(
        {"passed": True, "issues": []},
        [{"id": "research-question", "requires_evidence": True}],
        [],
    )
    assert result["passed"] is False
    assert result["reasons"][0]["code"] == "reference_quality_vacuous"


def test_reference_quality_can_pass_without_evidence_obligations() -> None:
    result = evaluate_reference_quality_gate({"passed": True}, [], [])
    assert result["passed"] is True


def test_finalization_requires_all_gates_no_blockers_and_consistent_hashes() -> None:
    candidate_sha = "a" * 64
    result = evaluate_finalization_eligibility(
        completion_gate={"passed": True, "target_html_sha256": candidate_sha},
        final_coherence_gate={"passed": True, "candidate_sha256": candidate_sha},
        blocking_findings=[],
        candidate_sha256=candidate_sha,
        expected_candidate_sha256=candidate_sha,
    )
    assert result["eligible"] is True
    assert result["reasons"] == []


def test_finalization_returns_explicit_reasons_for_each_failed_condition() -> None:
    result = evaluate_finalization_eligibility(
        completion_gate={"passed": False},
        final_coherence_gate={"passed": False},
        blocking_findings=[{"severity": "critical", "status": "open"}],
        candidate_sha256="a" * 64,
        expected_candidate_sha256="b" * 64,
    )
    codes = {item["code"] for item in result["reasons"]}
    assert result["eligible"] is False
    assert {
        "completion_gate_failed",
        "final_coherence_failed",
        "blocking_findings_present",
        "completion_sha_missing",
        "final_coherence_sha_missing",
    }.issubset(codes)


def test_resolved_findings_do_not_block_but_hash_mismatch_does() -> None:
    result = evaluate_finalization_eligibility(
        completion_gate={"passed": True, "candidate_sha256": "a" * 64},
        final_coherence_gate={"passed": True, "target_html_sha256": "a" * 64},
        blocking_findings=[{"severity": "major", "status": "resolved"}],
        candidate_sha256="a" * 64,
        expected_candidate_sha256="b" * 64,
    )
    assert result["blocking_finding_count"] == 0
    assert result["eligible"] is False
    assert "candidate_sha_mismatch" in {item["code"] for item in result["reasons"]}


def test_finalization_rejects_malformed_sha256() -> None:
    result = evaluate_finalization_eligibility(
        completion_gate={"passed": True, "candidate_sha256": "bad"},
        final_coherence_gate={"passed": True, "candidate_sha256": "bad"},
        blocking_findings=[],
        candidate_sha256="bad",
    )
    assert result["eligible"] is False
    assert "candidate_sha_invalid" in {item["code"] for item in result["reasons"]}
