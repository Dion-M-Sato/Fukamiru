"""Recover citation anchors through structural identity and verified provenance."""

from __future__ import annotations

import hashlib
import re
from typing import Any

_ANCHOR_ERROR = re.compile(
    r"V\d+ role draft JSON handoff violation: section (?P<section>\S+) "
    r"scholarship evidence (?P<evidence>\S+) (?P<reason>"
    r"has no unique sentence-level footnote anchor|"
    r"does not identify its author or work in the unique sentence-level citation anchor)"
)


def anchor_error_kind(message: str) -> str | None:
    match = _ANCHOR_ERROR.fullmatch(message)
    if match is None:
        return None
    return "footnote" if match["reason"].startswith("has no unique") else "attribution"


def verified_scholarship_anchor(context: dict[str, Any], message: str) -> str | None:
    """Require a unique source linked to the failing section and evidence ID."""
    match = _ANCHOR_ERROR.fullmatch(message)
    if match is None:
        return None
    contracts = [
        item
        for item in context.get("section_contracts") or []
        if isinstance(item, dict) and item.get("section_id") == match["section"]
    ]
    if len(contracts) != 1:
        return None
    contract = contracts[0]
    claims = set(contract.get("claim_ids") or [])
    nodes = [
        item
        for item in context.get("argument_nodes") or []
        if isinstance(item, dict) and item.get("node_type") == "evidence"
    ]
    kind = anchor_error_kind(message)
    matched = []
    for citation in context.get("annotation_citation_evidence") or []:
        if not isinstance(citation, dict):
            continue
        provenance = dict(citation.get("provenance") or {})
        if (
            citation.get("allowed_use") != "attributed_scholarship_citation"
            or citation.get("verification_scope") != "bibliographic_identity_only"
            or provenance.get("bibliographic_identity_verified") is not True
            or provenance.get("substantive_support_verified") is not False
        ):
            continue
        citation_id = str(citation.get("id") or "")
        if kind == "footnote":
            if contract.get("article_role") != "conclusion":
                continue
            eligible = [
                node
                for node in nodes
                if node.get("id") == match["evidence"]
                and node.get("id") in (contract.get("evidence_ids") or [])
                and node.get("claim_id") in claims
                and citation_id in dict(node.get("provenance") or {}).get("source_ids", [])
            ]
            if len(eligible) != 1 or not citation.get("title") or not citation.get("authors"):
                continue
        else:
            if (
                citation.get("source_type") != "author_supplied_reference"
                or citation.get("authors") != ["自著"]
                or not citation.get("annotation_proposition_ids")
                or not citation.get("reference_id")
                or citation["reference_id"] not in (provenance.get("source_ids") or [])
            ):
                continue
            expected_ids = {
                "annotation-citation-node:"
                + hashlib.sha256(f"{claim}\0{citation_id}".encode()).hexdigest()[:16]
                for claim in claims
            }
            if match["evidence"] not in expected_ids:
                continue
            proposition_ids = set(citation["annotation_proposition_ids"])
            if not any(
                node.get("claim_id") in claims
                and proposition_ids.intersection(
                    dict(node.get("provenance") or {}).get("source_ids") or []
                )
                for node in nodes
            ):
                continue
        matched.append(citation)
    return kind if len(matched) == 1 else None


# Persisted recovery identifiers are a protocol, not an ordered replay recipe.
_RECOVERY_FLAGS = {
    "authoring-v31-citation-source-claim-bridge-recovery-v1": (
        "v31_citation_source_claim_mapping_recovery"
    ),
    "authoring-v31-citation-source-claim-bridge-input-rebuild-v1": (
        "v31_citation_source_claim_mapping_followup_recovery"
    ),
    "authoring-v31-section-contract-identity-merge-v1": "v31_section_contract_identity_recovery",
    "authoring-v31-citation-anchor-surface-recovery-v1": "v31_citation_anchor_surface_recovery",
    "authoring-v31-support-sentence-hint-recovery-v1": "v31_support_sentence_hint_recovery",
    "authoring-v31-conclusion_scholarship-conclusion-anchor-recovery-v1": (
        "v31_conclusion_scholarship_conclusion_anchor_recovery"
    ),
    "authoring-v31-attribution-predicate-semantic-recovery-v1": (
        "v31_semantic_attribution_predicate_recovery"
    ),
    "authoring-v31-fixed-source-lineage-recovery-v1": "v31_fixed_source_lineage_recovery",
}


def recovery_available(
    payloads: list[dict[str, Any]], policy: str, *, workflow_version: str
) -> bool:
    """Bound each correction once; require a consistent ledger in any order."""
    if policy not in _RECOVERY_FLAGS or len(payloads) >= len(_RECOVERY_FLAGS):
        return False
    seen = set()
    for item in payloads:
        previous = item.get("policy")
        flag = _RECOVERY_FLAGS.get(previous)
        if (
            not flag
            or item.get(flag) is not True
            or previous in seen
            or item.get("node_id", "draft_rewrite") != "draft_rewrite"
            or item.get("workflow_version", workflow_version) != workflow_version
        ):
            return False
        seen.add(previous)
    return policy not in seen
