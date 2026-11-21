"""Stable authoring workflow policy identifiers shared by runs and workers."""

from __future__ import annotations

AUTHORING_V30_POLICY = {
    "id": "fukamiru-authoring-v30",
    "workflow_version": 30,
    "japanese_revision_pipeline": "full-sentence-proofreading-v29.1",
    "quality_gate_mode": "fail_closed",
    "economy_priority_supported": False,
}
AUTHORING_V31_POLICY = {
    **AUTHORING_V30_POLICY,
    "id": "fukamiru-authoring-v31",
    "workflow_version": 31,
    "research_argument_architecture": "role-specific-v31",
}
AUTHORING_V32_POLICY = {
    **AUTHORING_V31_POLICY,
    "id": "fukamiru-authoring-v32",
    "workflow_version": 32,
    "footnote_marker_placement": "before-comma-and-period",
    "concept_fit": "contextual-concept-fit-v1",
}


def authoring_policy(workflow_version: str | int = 30) -> dict[str, object]:
    """Return a fresh policy value and reject incompatible restart versions."""

    version = int(workflow_version)
    if version == 32:
        return dict(AUTHORING_V32_POLICY)
    if version == 31:
        return dict(AUTHORING_V31_POLICY)
    if version != int(AUTHORING_V30_POLICY["workflow_version"]):
        raise ValueError(f"Unsupported authoring workflow policy version: {version}")
    return dict(AUTHORING_V30_POLICY)


def enforce_authoring_quality_policy(
    context: dict[str, object], workflow_version: str | int = 30
) -> dict[str, object]:
    """Remove legacy economy bypass state from a V30 authoring context.

    ``economy_priority`` was inherited from the rewrite workflow, where it can
    turn failed evidence, discourse, coherence, and Japanese-quality checks
    into warnings or skip them. That is incompatible with V30 authoring's
    submission contract. Normalize both new inputs and checkpoint restarts so
    the bypass cannot be reintroduced accidentally.
    """

    normalized = {
        key: value
        for key, value in context.items()
        if key != "economy_priority"
        and not key.startswith("authoring_economy_")
        and not key.startswith("economy_")
    }
    normalized["authoring_workflow_policy"] = authoring_policy(workflow_version)
    return normalized
