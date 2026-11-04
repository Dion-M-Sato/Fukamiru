"""Trace provisional AI argument decisions to source records for human review."""

from __future__ import annotations

from typing import Any


def build_decision_ledger(context: dict[str, Any]) -> dict[str, Any]:
    """Keep source identity checks separate from the human support judgment."""

    sources = {
        str(item["id"]): item
        for item in context.get("evidence_context") or []
        if isinstance(item, dict) and item.get("id")
    }
    nodes = [item for item in context.get("argument_nodes") or [] if isinstance(item, dict)]
    decisions = {
        str(item["claim_id"]): item
        for item in context.get("argument_claim_decisions") or []
        if isinstance(item, dict) and item.get("claim_id")
    }
    entries = []
    for claim in context.get("claims") or []:
        if not isinstance(claim, dict) or not claim.get("id"):
            continue
        claim_id = str(claim["id"])
        attached = [item for item in nodes if str(item.get("claim_id") or "") == claim_id]
        evidence = []
        for node in attached:
            if node.get("node_type") != "evidence":
                continue
            provenance = dict(node.get("provenance") or {})
            for source_id in provenance.get("source_ids") or []:
                source = sources.get(str(source_id))
                if source is None:
                    continue
                evidence.append(
                    {
                        "argument_node_id": node.get("id"),
                        "source_id": source_id,
                        "title": source.get("title"),
                        "authors": source.get("authors") or [],
                        "source_url": source.get("source_url"),
                        "locator": source.get("locator"),
                        "excerpt": source.get("excerpt"),
                        "bibliographic_verification_status": source.get(
                            "bibliographic_verification_status"
                        ),
                        "source_verification_status": source.get("verification_status"),
                        "ai_proposed_link": node.get("text"),
                    }
                )
        decision = decisions.get(claim_id, {})
        entries.append(
            {
                "claim_id": claim_id,
                "claim": claim.get("text"),
                "provenance": claim.get("provenance"),
                "source_passages": evidence,
                "warrants": [
                    {"node_id": item.get("id"), "text": item.get("text")}
                    for item in attached
                    if item.get("node_type") == "warrant"
                ],
                "counterinterpretations": [
                    {"node_id": item.get("id"), "text": item.get("text")}
                    for item in attached
                    if item.get("node_type") in {"counterexample", "rebuttal"}
                ],
                "ai_decision": {
                    "disposition": decision.get("disposition", "not_recorded"),
                    "reason": decision.get("reason", ""),
                    "counterinterpretation": decision.get("counterinterpretation", ""),
                    "adopted_text": decision.get("adopted_text", ""),
                },
                "human_support_decision": "pending",
            }
        )
    return {
        "schema": "authoring-argument-decision-ledger-v1",
        "support_assessment_authority": "ai_proposal_pending_human_review",
        "claims": entries,
    }
