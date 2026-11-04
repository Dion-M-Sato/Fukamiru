"""Deterministic structural audit for plan-first authoring."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Literal

Severity = Literal["info", "minor", "major", "critical"]


@dataclass(frozen=True)
class AuditFinding:
    code: str
    severity: Severity
    message: str
    object_type: str | None = None
    object_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "object_type": self.object_type,
            "object_id": self.object_id,
        }


def _tree_findings(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    *,
    prefix: str,
) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    ids = {str(item.get("id", "")) for item in nodes if item.get("id")}
    incoming: dict[str, int] = defaultdict(int)
    adjacency: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        source = str(edge.get("source_id", ""))
        target = str(edge.get("target_id", ""))
        if source not in ids or target not in ids:
            findings.append(
                AuditFinding(
                    f"{prefix}.unknown_endpoint",
                    "critical",
                    f"Tree edge references an unknown node: {source} -> {target}",
                    prefix,
                    target or source or None,
                )
            )
            continue
        adjacency[source].append(target)
        incoming[target] += 1
    roots = [node_id for node_id in ids if incoming[node_id] == 0]
    if len(roots) != 1:
        findings.append(
            AuditFinding(
                f"{prefix}.root_count",
                "critical",
                f"Expected exactly one root, found {len(roots)}",
                prefix,
            )
        )
        return findings
    visited: set[str] = set()
    queue = deque([roots[0]])
    while queue:
        current = queue.popleft()
        if current in visited:
            findings.append(
                AuditFinding(
                    f"{prefix}.cycle",
                    "critical",
                    "Tree contains a cycle or a node with multiple paths",
                    prefix,
                    current,
                )
            )
            continue
        visited.add(current)
        queue.extend(adjacency[current])
    missing = ids - visited
    if missing:
        findings.append(
            AuditFinding(
                f"{prefix}.disconnected",
                "critical",
                "Tree contains disconnected nodes",
                prefix,
                sorted(missing)[0],
            )
        )
    return findings


def audit_plan(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Return deterministic findings without trusting an LLM self-assessment."""
    findings: list[AuditFinding] = []
    question = plan.get("research_question") or {}
    if not str(question.get("question", "")).strip():
        findings.append(
            AuditFinding(
                "research_question.missing",
                "critical",
                "The plan must contain exactly one non-empty research question",
                "research_question",
            )
        )

    claims = list(plan.get("claims") or [])
    claim_edges = list(plan.get("claim_edges") or [])
    findings.extend(_tree_findings(claims, claim_edges, prefix="claim_tree"))
    main_claims = [item for item in claims if item.get("level") == "main"]
    if len(main_claims) != 1:
        findings.append(
            AuditFinding(
                "claim_tree.main_count",
                "critical",
                f"Expected exactly one Main Claim, found {len(main_claims)}",
                "claim",
            )
        )

    argument_nodes = list(plan.get("argument_nodes") or [])
    argument_edges = list(plan.get("argument_edges") or [])
    known_argument_ids = {str(item.get("id")) for item in argument_nodes}
    for edge in argument_edges:
        if edge.get("source_id") not in known_argument_ids or edge.get(
            "target_id"
        ) not in known_argument_ids:
            findings.append(
                AuditFinding(
                    "argument_graph.unknown_endpoint",
                    "critical",
                    "Argument edge references an unknown node",
                    "argument_edge",
                )
            )
    supported_claims = {
        str(edge.get("target_id"))
        for edge in argument_edges
        if edge.get("relation_type") in {"supports", "warrants", "rebuts"}
    }
    for node in argument_nodes:
        if node.get("node_type") == "claim" and node.get("id") not in supported_claims:
            findings.append(
                AuditFinding(
                    "argument_graph.unsupported_claim",
                    "major",
                    "Claim has no supporting evidence or warrant",
                    "argument_node",
                    str(node.get("id")),
                )
            )

    discourse_nodes = list(plan.get("discourse_nodes") or [])
    discourse_edges = list(plan.get("discourse_edges") or [])
    findings.extend(_tree_findings(discourse_nodes, discourse_edges, prefix="discourse_tree"))

    contracts = list(plan.get("section_contracts") or [])
    contract_sections = {str(item.get("section_id")) for item in contracts}
    for node in discourse_nodes:
        if not node.get("section_id"):
            continue
        section_id = str(node.get("section_id") or node.get("id") or "")
        if section_id not in contract_sections:
            findings.append(
                AuditFinding(
                    "section_contract.missing",
                    "critical",
                    "Discourse section has no locked section contract",
                    "section",
                    section_id,
                )
            )
    allocated_claims = {
        str(claim_id)
        for contract in contracts
        for claim_id in (contract.get("claim_ids") or [])
    }
    for claim in claims:
        claim_id = str(claim.get("id"))
        if claim_id not in allocated_claims:
            findings.append(
                AuditFinding(
                    "section_contract.unallocated_claim",
                    "major",
                    "Claim is not assigned to any section contract",
                    "claim",
                    claim_id,
                )
            )
    return [finding.as_dict() for finding in findings]
