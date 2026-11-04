"""V27 coherence execution with resumable, ordered parallel audits."""

from __future__ import annotations

from typing import Any

from fukamiru.worker import submission_validation

POLICY = "manuscript-coherence-v27.0"


def audit_coherence(
    worker: Any,
    claimed: Any,
    context: dict[str, Any],
    *,
    stage: str,
    allow_repairs: bool = True,
) -> tuple[dict[str, Any], str]:
    """Audit one immutable candidate in parallel, then repair it serially."""

    parallelism = max(
        1,
        min(
            int(worker.settings.worker.codex_coherence_concurrency),
            int(worker.settings.worker.codex_max_concurrency),
        ),
    )
    return submission_validation.audit_coherence(
        worker,
        claimed,
        context,
        stage=stage,
        policy=POLICY,
        checkpoint_namespace="v27",
        parallelism=parallelism,
        resume_cross_partials=True,
        group_original_supplements=True,
        resume_dirty_units=True,
        skip_cross_when_sections_incomplete=True,
        allow_repairs=allow_repairs,
    )
