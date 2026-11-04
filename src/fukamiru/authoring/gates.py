"""Fail-closed, deterministic gates for final authoring eligibility."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

_HEX_DIGITS = frozenset("0123456789abcdef")


def _valid_sha256(value: str) -> bool:
    return len(value) == 64 and set(value) <= _HEX_DIGITS


def _evidence_requirement(obligation: Mapping[str, Any]) -> str:
    """Read the new enum, falling back to the legacy boolean when it is absent."""
    value = obligation.get("evidence_requirement")
    if value in {"required", "preferred", "none"}:
        return str(value)
    if value is not None:
        # Unknown values fail closed as required; callers still get a missing-evidence
        # finding rather than accidentally weakening the hard gate.
        return "required"
    return "required" if obligation.get("requires_evidence", True) is True else "none"


def _is_evidence_requiring(obligation: Mapping[str, Any]) -> bool:
    return _evidence_requirement(obligation) == "required"


def _accepted_evidence(item: Mapping[str, Any]) -> bool:
    return item.get("accepted") is True or item.get("status") in {
        "accepted",
        "verified",
    }


def evaluate_required_evidence_obligations(
    obligations: Sequence[Mapping[str, Any]],
    accepted_evidence: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Fail any evidence-required obligation that has no accepted evidence linked to it.

    Obligations use ``id`` and ``evidence_requirement`` (required/preferred/none).
    When the enum is absent, the legacy ``requires_evidence`` boolean is honored
    and defaults to true. Preferred obligations are reported for auditing but do
    not fail this hard gate.
    Evidence records use ``obligation_id`` or ``obligation_ids`` and must be
    explicitly accepted/verified. Merely having an evidence candidate is not enough.
    """
    accepted_by_obligation: dict[str, list[str]] = {}
    for item in accepted_evidence:
        if not _accepted_evidence(item):
            continue
        evidence_id = str(item.get("id") or "")
        linked = item.get("obligation_ids")
        obligation_ids = (
            [str(value) for value in linked]
            if isinstance(linked, (list, tuple, set))
            else []
        )
        if item.get("obligation_id") is not None:
            obligation_ids.append(str(item["obligation_id"]))
        for obligation_id in obligation_ids:
            accepted_by_obligation.setdefault(obligation_id, []).append(evidence_id)

    reasons: list[dict[str, str]] = []
    requirement_counts = {"required": 0, "preferred": 0, "none": 0}
    missing_required: list[str] = []
    preferred_without_evidence: list[str] = []
    for index, obligation in enumerate(obligations):
        requirement = _evidence_requirement(obligation)
        requirement_counts[requirement] += 1
        obligation_id = str(obligation.get("id") or f"obligation-{index + 1}")
        has_accepted_evidence = bool(accepted_by_obligation.get(obligation_id))
        if requirement == "required" and not has_accepted_evidence:
            missing_required.append(obligation_id)
            reasons.append(
                {
                    "code": "required_evidence_missing",
                    "obligation_id": obligation_id,
                    "message": "Evidence is required but no accepted evidence is linked.",
                }
            )
        elif requirement == "preferred" and not has_accepted_evidence:
            preferred_without_evidence.append(obligation_id)
    return {
        "passed": not reasons,
        "required_obligation_count": requirement_counts["required"],
        "required_count": requirement_counts["required"],
        "preferred_count": requirement_counts["preferred"],
        "none_count": requirement_counts["none"],
        "missing_required_count": len(missing_required),
        "missing_required_obligation_ids": missing_required,
        "preferred_without_evidence_count": len(preferred_without_evidence),
        "preferred_without_evidence_obligation_ids": preferred_without_evidence,
        "accepted_evidence_count": sum(map(len, accepted_by_obligation.values())),
        "reasons": reasons,
    }


def evaluate_reference_quality_gate(
    quality_gate: Mapping[str, Any],
    evidence_requiring_obligations: Sequence[Mapping[str, Any]],
    accepted_references: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Prevent a nominally passing quality report from succeeding vacuously."""
    reasons: list[dict[str, str]] = []
    if quality_gate.get("passed") is not True:
        reasons.append(
            {
                "code": "reference_quality_failed",
                "message": "The reference quality gate did not pass.",
            }
        )
    required_count = sum(
        1 for item in evidence_requiring_obligations if _is_evidence_requiring(item)
    )
    accepted_count = sum(1 for item in accepted_references if _accepted_evidence(item))
    if required_count and accepted_count == 0:
        reasons.append(
            {
                "code": "reference_quality_vacuous",
                "message": (
                    "Evidence-requiring claims or research obligations exist, "
                    "but no accepted reference is available."
                ),
            }
        )
    return {
        "passed": not reasons,
        "evidence_requiring_obligation_count": required_count,
        "accepted_reference_count": accepted_count,
        "reasons": reasons,
    }


def evaluate_finalization_eligibility(
    *,
    completion_gate: Mapping[str, Any],
    final_coherence_gate: Mapping[str, Any],
    blocking_findings: Sequence[Mapping[str, Any]],
    candidate_sha256: str | None,
    expected_candidate_sha256: str | None = None,
) -> dict[str, Any]:
    """Return whether a candidate may be finalized, with machine-readable reasons."""
    reasons: list[dict[str, str]] = []
    if completion_gate.get("passed") is not True:
        reasons.append(
            {
                "code": "completion_gate_failed",
                "message": "The completion gate must explicitly pass.",
            }
        )
    if final_coherence_gate.get("passed") is not True:
        reasons.append(
            {
                "code": "final_coherence_failed",
                "message": "Final coherence must explicitly pass.",
            }
        )
    active_blockers = [
        item
        for item in blocking_findings
        if item.get("blocking") is True
        or (
            item.get("severity") in {"major", "critical"}
            and item.get("status") not in {"resolved", "accepted", "wont_fix"}
        )
    ]
    if active_blockers:
        reasons.append(
            {
                "code": "blocking_findings_present",
                "message": f"{len(active_blockers)} blocking finding(s) remain.",
            }
        )

    candidate_hash = (candidate_sha256 or "").strip().lower()
    if not candidate_hash:
        reasons.append(
            {
                "code": "candidate_sha_missing",
                "message": "A candidate SHA-256 is required for finalization.",
            }
        )
    elif not _valid_sha256(candidate_hash):
        reasons.append(
            {
                "code": "candidate_sha_invalid",
                "message": "Candidate digest must be a 64-character hexadecimal SHA-256.",
            }
        )
    gate_hashes: list[str] = []
    for label, gate in (
        ("completion", completion_gate),
        ("final_coherence", final_coherence_gate),
    ):
        value = gate.get("candidate_sha256") or gate.get("target_html_sha256")
        if isinstance(value, str) and value.strip():
            gate_hash = value.strip().lower()
            if not _valid_sha256(gate_hash):
                reasons.append(
                    {
                        "code": f"{label}_sha_invalid",
                        "message": f"The {label.replace('_', ' ')} SHA-256 is malformed.",
                    }
                )
            gate_hashes.append(gate_hash)
        else:
            reasons.append(
                {
                    "code": f"{label}_sha_missing",
                    "message": f"The {label.replace('_', ' ')} gate has no candidate SHA-256.",
                }
            )
    hashes_to_match = set(gate_hashes)
    if expected_candidate_sha256 and expected_candidate_sha256.strip():
        hashes_to_match.add(expected_candidate_sha256.strip().lower())
    if expected_candidate_sha256 is not None and not expected_candidate_sha256.strip():
        reasons.append(
            {
                "code": "expected_candidate_sha_missing",
                "message": "The expected candidate SHA-256 is empty.",
            }
        )
    if candidate_hash and any(value != candidate_hash for value in hashes_to_match):
        reasons.append(
            {
                "code": "candidate_sha_mismatch",
                "message": "Candidate SHA-256 does not match a gate or expected digest.",
            }
        )
    return {
        "eligible": not reasons,
        "blocking_finding_count": len(active_blockers),
        "candidate_sha256": candidate_hash or None,
        "reasons": reasons,
    }
