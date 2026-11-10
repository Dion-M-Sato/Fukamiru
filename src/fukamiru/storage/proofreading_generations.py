"""Stable proofreading scopes for distinct authoring manuscript generations."""

from __future__ import annotations

import sqlite3
from typing import Any

from fukamiru.workflow.identity import is_manuscript_generator_workflow

LEGACY_GENERATION = "legacy"


def generation_id(context: dict[str, Any]) -> str:
    return str(context.get("_proofreading_generation_id") or LEGACY_GENERATION)


def draft_generation_id(workflow_name: str, workflow_version: str, context: dict[str, Any]) -> str:
    producer = str(
        dict(context.get("draft_evidence_identity_generation") or {}).get("producer_node_run_id")
        or ""
    )
    if (
        is_manuscript_generator_workflow(workflow_name)
        and workflow_version in {"31", "32"}
        and producer
    ):
        return "authoring:" + producer
    return LEGACY_GENERATION


def checkpoint_recovery(
    connection: sqlite3.Connection,
    run_id: str,
    workflow_name: str,
    workflow_version: str,
    context: dict[str, Any],
) -> dict[str, Any] | None:
    from fukamiru.revisions.sentence_proofreading import digest, extract_sentences

    generation = draft_generation_id(workflow_name, workflow_version, context)
    if generation == LEGACY_GENERATION or not context.get("prior_candidate_generations"):
        return None
    if connection.execute(
        "SELECT 1 FROM manuscript_proofreading_generations WHERE run_id=? AND generation_id=?",
        (run_id, generation),
    ).fetchone():
        return None
    prior = {
        row[0]: row[1]
        for row in connection.execute(
            "SELECT source_sentence_id,original_hash FROM manuscript_proofreading_sentences "
            "WHERE run_id=? AND generation_id='legacy'",
            (run_id,),
        )
    }
    html = str(context.get("proposed_html") or "")
    sentences = extract_sentences(
        html,
        run_id=run_id,
        document_id=str(context.get("document_id")),
        source_preserve_block_ids=set(context.get("source_preserve_block_ids") or []),
    )
    current = {row["source_sentence_id"]: row["original_hash"] for row in sentences}
    if not prior or not current or prior == current:
        return None
    return {
        "generation_id": generation,
        "source_html_sha256": digest(html),
        "retained_legacy_sentence_count": len(prior),
        "new_sentence_count": len(current),
    }
