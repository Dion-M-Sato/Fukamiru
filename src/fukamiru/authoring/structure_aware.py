"""Versioned discourse contracts and deterministic V31 JSON handoff checks."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fukamiru.authoring.evidence_contracts import SectionOutput
from fukamiru.authoring.paragraph_moves import PARAGRAPH_ARGUMENT_MOVE_IDS
from fukamiru.errors import FukamiruError


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PriorPosition(ContractModel):
    proposition: str = Field(min_length=1)
    source_ids: tuple[str, ...] = Field(min_length=1)


class Problematisation(ContractModel):
    type: Literal[
        "contradiction",
        "gap",
        "unexplained_case",
        "theoretical_tension",
        "assumption_challenge",
        "source_internal",
        "other",
    ]
    description: str = Field(min_length=1)
    evidence_ids: tuple[str, ...] = ()
    proposition_ids: tuple[str, ...] = ()


class ResearchProblem(ContractModel):
    research_question: str = Field(min_length=1)
    prior_positions: tuple[PriorPosition, ...] = ()
    problematisation: Problematisation
    provisional_answer: str = Field(min_length=1)
    contribution_target: str = Field(min_length=1)


class ClaimScope(ContractModel):
    claim_id: str
    proposition: str = Field(min_length=1)
    scope_conditions: tuple[str, ...] = ()
    epistemic_status: Literal[
        "established", "supported_interpretation", "provisional", "unresolved"
    ]
    evidence_basis_ids: tuple[str, ...] = ()


class DefenseObligation(ContractModel):
    id: str
    target_claim_id: str
    challenge: str
    basis_ids: tuple[str, ...] = ()
    materiality: str = ""
    response: str = ""
    resolution_location: Literal[
        "same_section", "later_section", "note_candidate", "scope_absorbed", "unresolved"
    ]
    disposition: Literal["body", "note_candidate", "scope_absorbed", "internal_only", "drop"]
    source_argument_node_ids: tuple[str, ...] = ()


class IntroductionRoleContract(ContractModel):
    section_id: str
    genre_profile: str
    moves: tuple[dict[str, Any], ...]
    research_question: str
    documented_prior_positions: tuple[PriorPosition, ...] = ()
    problematisation: str
    provisional_answer: str
    contribution_target: str
    roadmap_section_ids: tuple[str, ...]


class BodyRoleContract(ContractModel):
    section_id: str
    section_task: str = Field(min_length=1)
    section_output: str = Field(min_length=1)
    section_output_contract: SectionOutput | None = None
    thesis_link: str = Field(min_length=1)
    required_input_section_ids: tuple[str, ...] = ()
    defense_obligation_ids: tuple[str, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def adapt_legacy_section_output(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        adapted = dict(value)
        raw_output = adapted.get("section_output")
        if isinstance(raw_output, dict):
            adapted["section_output_contract"] = raw_output
            adapted["section_output"] = raw_output.get("proposition", "")
        elif not adapted.get("section_output_contract") and str(raw_output or "").strip():
            adapted["section_output_contract"] = {
                "proposition": str(raw_output),
                "output_type": "interpretive_result",
                "required_evidence_level": "traceable",
            }
        return adapted


class ConclusionRoleContract(ContractModel):
    section_id: str
    research_question: str
    established_section_ids: tuple[str, ...]
    synthesis_goal: str = Field(min_length=1)
    contribution_target: str
    scope: str
    unresolved_questions: tuple[str, ...] = ()


class ConclusionSynthesis(ContractModel):
    final_answer: str = Field(min_length=1)
    synthesis: str = Field(min_length=1)
    contribution: str = Field(min_length=1)
    established_section_ids: tuple[str, ...]
    scope: str
    unresolved_questions: tuple[str, ...] = ()
    new_evidence_ids: tuple[str, ...] = ()
    new_evidence_rhetorical_reason: str = ""


class ArchitectureV31(ContractModel):
    research_problem: ResearchProblem
    claim_scopes: tuple[ClaimScope, ...]
    defense_obligations: tuple[DefenseObligation, ...]
    introduction_contract: IntroductionRoleContract
    body_section_contracts: tuple[BodyRoleContract, ...]
    conclusion_contract: ConclusionRoleContract
    sections: tuple[dict[str, Any], ...]


_PROCEDURAL_SECTION_OUTPUT_PATTERNS = (
    re.compile(
        r"(?:比較|分析|検討|確認|調査|照合|検証)(?:の|を|に|上の)?"
        r"(?:枠組み|枠|手順|項目|単位|方法|方針|観点)"
        r"(?:を)?(?:示す|確立する|定める|作る|設定する|特定する|確定する)"
    ),
    re.compile(r"(?:資料|史料|証拠|データ).{0,30}(?:必要|要る|不足|欠け|揃っていない)"),
    re.compile(
        r"(?:を|について)?(?:調べる|確かめる|確認する|比較する|照合する|"
        r"特定する|整理する|検討する|検証する|明らかにする|確定する)"
        r"[。．.]?$"
    ),
    re.compile(
        r"(?:identify|determine|examine|investigate|compare|verify|establish)\b"
        r".{0,50}\b(?:framework|procedure|method|items?|materials?|sources?)\b",
        re.IGNORECASE,
    ),
)


def body_section_output_problems(sections: list[dict[str, Any]]) -> list[str]:
    """Reject V31 body outputs that describe future research instead of a result."""

    problems: list[str] = []
    for section in sections:
        if not isinstance(section, dict) or section.get("article_role") != "body":
            continue
        section_id = str(section.get("section_id") or "")
        raw_output = section.get("section_output")
        output = str(
            raw_output.get("proposition")
            if isinstance(raw_output, dict)
            else raw_output or ""
        ).strip()
        if not output:
            continue
        matched = [
            pattern.pattern
            for pattern in _PROCEDURAL_SECTION_OUTPUT_PATTERNS
            if pattern.search(output)
        ]
        if matched:
            problems.append(
                f"section {section_id} section_output must state an established "
                "object-level result, not a future comparison, verification task, "
                "research procedure, or list of missing materials"
            )
    return problems


def normalize_section_output_contracts(
    sections: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Normalize V31 section outputs while preserving legacy operational text."""

    normalized: list[dict[str, Any]] = []
    adaptations: list[dict[str, Any]] = []
    for raw in sections:
        section = dict(raw)
        if section.get("article_role") != "body":
            normalized.append(section)
            continue
        section_id = str(section.get("section_id") or "")
        output = section.get("section_output")
        contract = section.get("section_output_contract")
        if isinstance(output, dict):
            contract = dict(output)
            section["section_output"] = str(contract.get("proposition") or "")
            adaptations.append(
                {
                    "section_id": section_id,
                    "policy": "structured-section-output-to-operational-text-v31.1",
                }
            )
        elif not isinstance(contract, dict):
            contract = {
                "proposition": str(output or ""),
                "output_type": "interpretive_result",
                "required_evidence_level": "traceable",
            }
            adaptations.append(
                {
                    "section_id": section_id,
                    "policy": "legacy-section-output-compatibility-v31.1",
                }
            )
        parsed = SectionOutput.model_validate(contract)
        if parsed.proposition != str(section.get("section_output") or ""):
            raise FukamiruError(
                f"section {section_id} output proposition differs from operational text"
            )
        section["section_output_contract"] = parsed.model_dump(mode="json")
        normalized.append(section)
    return normalized, adaptations


def _ids(items: list[dict[str, Any]], key: str = "id") -> set[str]:
    return {str(item.get(key) or "") for item in items if isinstance(item, dict) and item.get(key)}


def validate_research_problem(
    raw: dict[str, Any],
    *,
    question: dict[str, Any],
    accepted_evidence: list[dict[str, Any]],
    propositions: list[dict[str, Any]],
) -> ResearchProblem:
    problem = ResearchProblem.model_validate(raw)
    if problem.research_question != str(question.get("text") or ""):
        raise FukamiruError("Research problem changed the provisional RQ; replan required")
    evidence_ids = _ids(accepted_evidence)
    proposition_ids = _ids(propositions)
    for position in problem.prior_positions:
        if not set(position.source_ids).issubset(evidence_ids):
            raise FukamiruError("Prior position lacks accepted source evidence")
    if not set(problem.problematisation.evidence_ids).issubset(evidence_ids):
        raise FukamiruError("Research problem refers to unaccepted evidence")
    if not set(problem.problematisation.proposition_ids).issubset(proposition_ids):
        raise FukamiruError("Research problem refers to unknown memo propositions")
    if problem.prior_positions and not problem.problematisation.evidence_ids:
        raise FukamiruError("Documented prior positions need a grounded tension or gap")
    if not problem.prior_positions and problem.problematisation.type != "source_internal":
        raise FukamiruError("An external research gap needs documented prior positions")
    if (
        problem.problematisation.type == "source_internal"
        and not problem.problematisation.proposition_ids
    ):
        raise FukamiruError("Source-internal research problem needs memo provenance")
    return problem


def validate_claim_scopes(
    raw: list[dict[str, Any]],
    *,
    claims: list[dict[str, Any]],
    argument_nodes: list[dict[str, Any]],
) -> tuple[ClaimScope, ...]:
    scopes = tuple(ClaimScope.model_validate(item) for item in raw)
    expected = _ids(claims)
    actual = [item.claim_id for item in scopes]
    if len(actual) != len(expected) or set(actual) != expected:
        raise FukamiruError("Claim scopes must cover every claim exactly once")
    evidence_ids = _ids([item for item in argument_nodes if item.get("node_type") == "evidence"])
    for scope in scopes:
        if not set(scope.evidence_basis_ids).issubset(evidence_ids):
            raise FukamiruError(f"Claim scope has unknown evidence basis: {scope.claim_id}")
        if scope.epistemic_status == "established" and not scope.evidence_basis_ids:
            raise FukamiruError(f"Established claim lacks evidence basis: {scope.claim_id}")
    return scopes


def normalize_claim_scope_basis_ids(
    raw: list[dict[str, Any]],
    *,
    argument_nodes: list[dict[str, Any]],
    known_obligation_ids: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Discard known non-evidence references from scope evidence references.

    Known non-evidence graph nodes and upstream evidence obligations that were
    not materialized as graph evidence cannot ground a claim scope. Arbitrary
    unknown IDs remain in place so the strict handoff validator can reject
    them. This never adds an evidence basis that the generator did not select.
    """

    node_types = {
        str(node.get("id") or ""): str(node.get("node_type") or "")
        for node in argument_nodes
    }
    known_obligations = {str(value) for value in known_obligation_ids or set()}
    normalized: list[dict[str, Any]] = []
    corrections: list[dict[str, Any]] = []
    for item in raw:
        scope = dict(item)
        basis_ids = [str(value) for value in scope.get("evidence_basis_ids") or []]
        removed = [
            value
            for value in basis_ids
            if value in node_types and node_types[value] != "evidence"
        ]
        removed_obligations = [
            value
            for value in basis_ids
            if value in known_obligations and value not in node_types
        ]
        removed_ids = set(removed) | set(removed_obligations)
        if removed_ids:
            scope["evidence_basis_ids"] = [
                value for value in basis_ids if value not in removed_ids
            ]
            correction = {
                "claim_id": str(scope.get("claim_id") or ""),
                "reason": "claim_scope_basis_requires_graph_evidence_nodes",
            }
            if removed:
                correction["removed_non_evidence_node_ids"] = removed
            if removed_obligations:
                correction["removed_non_graph_obligation_ids"] = removed_obligations
            corrections.append(correction)
        normalized.append(scope)
    return normalized, corrections


def restore_core_qualified_inferences(
    *,
    argument_nodes: list[dict[str, Any]],
    argument_edges: list[dict[str, Any]],
    propositions: list[dict[str, Any]],
    atomic_contracts: list[dict[str, Any]],
    selected_supporting_claim_ids: dict[str, list[str]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep locked memo propositions represented without inventing evidence.

    A restored node repeats an existing proposition exactly and remains unverified.
    Core qualified inferences are restored automatically. Audit-guided repair may
    also select a supporting qualified inference or an ``attribute_to_memo``
    proposition that was locked upstream but omitted from the argument graph.
    Scholarship and externally verified realization types still require their
    normal source and citation checks.
    """

    nodes = [dict(item) for item in argument_nodes]
    edges = [dict(item) for item in argument_edges]
    proposition_by_id = {
        str(item.get("id") or ""): item for item in propositions if item.get("id")
    }
    claim_node_by_id = {
        str(item.get("claim_id") or ""): str(item.get("id") or "")
        for item in nodes
        if item.get("node_type") == "claim" and item.get("claim_id") and item.get("id")
    }
    node_ids = {str(item.get("id") or "") for item in nodes}
    edge_ids = {str(item.get("id") or "") for item in edges}
    restored: list[dict[str, Any]] = []
    selected_supporting_claim_ids = selected_supporting_claim_ids or {}
    for contract in atomic_contracts:
        proposition_id = str(contract.get("proposition_id") or "")
        selected_claim_ids = [
            str(item)
            for item in selected_supporting_claim_ids.get(proposition_id, [])
            if str(item)
        ]
        realization = str(contract.get("realization") or "")
        if realization not in {"qualified_inference", "attribute_to_memo"} or (
            (
                realization != "qualified_inference"
                or contract.get("argument_necessity") != "core"
            )
            and not selected_claim_ids
        ):
            continue
        contract_claim_ids = [str(item) for item in contract.get("claim_ids") or []]
        claim_ids = (
            [item for item in selected_claim_ids if item in contract_claim_ids]
            if selected_claim_ids
            else contract_claim_ids
        )
        if any(
            node.get("node_type") == "evidence"
            and str(node.get("proposition_id") or "") == proposition_id
            and str(node.get("claim_id") or "") in claim_ids
            for node in nodes
        ):
            continue
        proposition = proposition_by_id.get(proposition_id)
        claim_id = next((item for item in claim_ids if item in claim_node_by_id), None)
        if (
            proposition is None
            or not str(proposition.get("text") or "").strip()
            or claim_id is None
        ):
            raise FukamiruError(
                f"Core qualified inference lacks a locked proposition or claim: {proposition_id}"
            )
        prefix = (
            "core-qualified"
            if realization == "qualified_inference"
            and contract.get("argument_necessity") == "core"
            else (
                "repair-memo"
                if realization == "attribute_to_memo"
                else "repair-qualified"
            )
        )
        base_id = f"{prefix}-{claim_id}-{proposition_id}"
        node_id = base_id
        suffix = 2
        while node_id in node_ids:
            node_id = f"{base_id}-{suffix}"
            suffix += 1
        edge_id = f"edge-{node_id}"
        while edge_id in edge_ids:
            edge_id = f"edge-{edge_id}"
        source = str(dict(proposition.get("provenance") or {}).get("source") or "")
        if source not in {"memo", "user", "mixed"}:
            source = "llm_inference"
        nodes.append(
            {
                "id": node_id,
                "node_type": "evidence",
                "text": str(proposition["text"]),
                "claim_id": claim_id,
                "proposition_id": proposition_id,
                "source_span_id": None,
                "provenance": {
                    "source": source,
                    "source_ids": [proposition_id],
                    "verification_status": "unverified",
                },
            }
        )
        edges.append(
            {
                "id": edge_id,
                "source_id": node_id,
                "target_id": claim_node_by_id[claim_id],
                "relation": "supports",
            }
        )
        node_ids.add(node_id)
        edge_ids.add(edge_id)
        restored.append(
            {
                "claim_id": claim_id,
                "proposition_id": proposition_id,
                "argument_node_id": node_id,
                "source": source,
                "reason": (
                    "restore_selected_memo_attribution"
                    if realization == "attribute_to_memo"
                    else (
                        "restore_selected_supporting_qualified_inference"
                        if contract.get("argument_necessity") != "core"
                        else "restore_locked_core_qualified_inference"
                    )
                ),
            }
        )
    return nodes, edges, restored


def validate_defense_obligations(
    raw: list[dict[str, Any]],
    *,
    claims: list[dict[str, Any]],
    argument_nodes: list[dict[str, Any]],
    accepted_evidence: list[dict[str, Any]],
) -> tuple[DefenseObligation, ...]:
    obligations = tuple(DefenseObligation.model_validate(item) for item in raw)
    if len({item.id for item in obligations}) != len(obligations):
        raise FukamiruError("Defense obligation IDs must be unique")
    claim_ids = _ids(claims)
    nodes = {str(item.get("id")): item for item in argument_nodes}
    basis_ids = set(nodes) | _ids(accepted_evidence)
    for obligation in obligations:
        if obligation.target_claim_id not in claim_ids:
            raise FukamiruError("Defense targets an unknown claim")
        if not set(obligation.basis_ids).issubset(basis_ids):
            raise FukamiruError("Defense uses an unknown basis ID")
        if any(
            node_id not in nodes
            or nodes[node_id].get("node_type") not in {"counterexample", "rebuttal"}
            or str(nodes[node_id].get("claim_id") or "") != obligation.target_claim_id
            for node_id in obligation.source_argument_node_ids
        ):
            raise FukamiruError("Defense cites counterevidence for another claim")
        if obligation.disposition == "body" and not all(
            (
                obligation.challenge.strip(),
                obligation.basis_ids,
                obligation.materiality.strip(),
                obligation.response.strip(),
            )
        ):
            raise FukamiruError("A body defense needs target, basis, materiality and response")
        if obligation.disposition == "body" and obligation.resolution_location not in {
            "same_section", "later_section", "unresolved"
        }:
            raise FukamiruError("Body defense has an incompatible resolution location")
        if (
            obligation.disposition == "scope_absorbed"
            and obligation.resolution_location != "scope_absorbed"
        ):
            raise FukamiruError("Scope-absorbed defense must resolve in claim scope")
    return obligations


def normalize_defense_obligation_references(
    raw: list[dict[str, Any]],
    *,
    argument_nodes: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep basis references while correcting known source-node role mismatches."""

    nodes = {str(item.get("id") or ""): item for item in argument_nodes}
    normalized: list[dict[str, Any]] = []
    corrections: list[dict[str, Any]] = []
    for item in raw:
        obligation = dict(item)
        target = str(obligation.get("target_claim_id") or "")
        source_ids = [str(value) for value in obligation.get("source_argument_node_ids") or []]
        removed = [
            value
            for value in source_ids
            if value in nodes
            and (
                nodes[value].get("node_type") not in {"counterexample", "rebuttal"}
                or str(nodes[value].get("claim_id") or "") != target
            )
        ]
        if removed:
            obligation["source_argument_node_ids"] = [
                value for value in source_ids if value not in removed
            ]
            corrections.append(
                {
                    "obligation_id": str(obligation.get("id") or ""),
                    "removed_nonmatching_source_node_ids": removed,
                    "reason": "defense_source_nodes_must_be_target_counterevidence",
                }
            )
        if (
            obligation.get("disposition") == "body"
            and obligation.get("resolution_location") == "scope_absorbed"
        ):
            obligation["disposition"] = "scope_absorbed"
            corrections.append(
                {
                    "obligation_id": str(obligation.get("id") or ""),
                    "from_disposition": "body",
                    "to_disposition": "scope_absorbed",
                    "reason": "scope_absorbed_resolution_is_not_body_defense",
                }
            )
        normalized.append(obligation)
    return normalized, corrections


def normalize_defense_obligations_for_discourse_roles(
    raw: list[dict[str, Any]],
    *,
    discourse_nodes: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Prevent a synthesized Conclusion claim from creating a Body defense move.

    A claim that exists only in the Conclusion is established by cross-section
    synthesis, so there is no earlier Body section in which a challenge to that
    claim can be answered. Keep the challenge as internal scope information by
    absorbing it into the synthesized claim instead of inventing a Body location.
    Introduction-level thesis defenses remain eligible for the final Body section.
    """

    body_claim_ids = {
        str(claim_id)
        for node in discourse_nodes
        if node.get("article_role") == "body"
        for claim_id in node.get("claim_ids") or []
    }
    introduction_claim_ids = {
        str(claim_id)
        for node in discourse_nodes
        if node.get("article_role") == "introduction"
        for claim_id in node.get("claim_ids") or []
    }
    conclusion_claim_ids = {
        str(claim_id)
        for node in discourse_nodes
        if node.get("article_role") == "conclusion"
        for claim_id in node.get("claim_ids") or []
    }
    normalized: list[dict[str, Any]] = []
    corrections: list[dict[str, Any]] = []
    for item in raw:
        obligation = dict(item)
        target_claim_id = str(obligation.get("target_claim_id") or "")
        conclusion_only = (
            target_claim_id in conclusion_claim_ids
            and target_claim_id not in body_claim_ids
            and target_claim_id not in introduction_claim_ids
        )
        if obligation.get("disposition") == "body" and conclusion_only:
            obligation["disposition"] = "scope_absorbed"
            obligation["resolution_location"] = "scope_absorbed"
            corrections.append(
                {
                    "obligation_id": str(obligation.get("id") or ""),
                    "target_claim_id": target_claim_id,
                    "from_disposition": "body",
                    "to_disposition": "scope_absorbed",
                    "reason": "conclusion_only_claim_has_no_body_resolution_location",
                }
            )
        normalized.append(obligation)
    return normalized, corrections


def validate_role_contracts(
    *,
    sections: list[dict[str, Any]],
    introduction: dict[str, Any],
    conclusion: dict[str, Any],
    problem: ResearchProblem,
    defenses: tuple[DefenseObligation, ...],
) -> tuple[IntroductionRoleContract, tuple[BodyRoleContract, ...], ConclusionRoleContract]:
    intro = IntroductionRoleContract.model_validate(introduction)
    ending = ConclusionRoleContract.model_validate(conclusion)
    bodies = [item for item in sections if item.get("article_role") == "body"]
    body_ids = [str(item.get("section_id") or "") for item in bodies]
    intro_ids = [
        str(item.get("section_id") or "")
        for item in sections
        if item.get("article_role") == "introduction"
    ]
    ending_ids = [
        str(item.get("section_id") or "")
        for item in sections
        if item.get("article_role") == "conclusion"
    ]
    if intro_ids != [intro.section_id] or ending_ids != [ending.section_id]:
        raise FukamiruError("Role contracts do not match Introduction/Conclusion sections")
    if (
        intro.research_question != problem.research_question
        or ending.research_question != problem.research_question
    ):
        raise FukamiruError("Role contracts changed the ratified research question")
    if intro.provisional_answer != problem.provisional_answer:
        raise FukamiruError("Introduction changed the provisional answer")
    if tuple(intro.roadmap_section_ids) != tuple(body_ids):
        raise FukamiruError("Introduction roadmap must match body section order")
    if tuple(ending.established_section_ids) != tuple(body_ids):
        raise FukamiruError("Conclusion must depend on every body output in order")
    permitted_prior = {(item.proposition, item.source_ids) for item in problem.prior_positions}
    if any(
        (item.proposition, item.source_ids) not in permitted_prior
        for item in intro.documented_prior_positions
    ):
        raise FukamiruError("Introduction invented a prior position")
    body_contracts = tuple(
        BodyRoleContract.model_validate(
            {
                "section_id": item["section_id"],
                "section_task": item["section_task"],
                "section_output": item["section_output"],
                "section_output_contract": item.get("section_output_contract"),
                "thesis_link": item["thesis_link"],
                "required_input_section_ids": item["required_input_section_ids"],
                "defense_obligation_ids": item["defense_obligation_ids"],
            }
        )
        for item in bodies
    )
    defense_by_id = {item.id: item for item in defenses if item.disposition == "body"}
    introduction_claim_ids = {
        str(claim_id)
        for section in sections
        if section.get("article_role") == "introduction"
        for claim_id in section.get("claim_ids") or []
    }
    body_claim_ids = {
        str(claim_id)
        for section in bodies
        for claim_id in section.get("claim_ids") or []
    }
    assigned: list[str] = []
    seen_body_ids: set[str] = set()
    for body, raw_section in zip(body_contracts, bodies, strict=True):
        if not set(body.required_input_section_ids).issubset(seen_body_ids):
            raise FukamiruError("Body section depends on a later or unknown section")
        seen_body_ids.add(body.section_id)
        for defense_id in body.defense_obligation_ids:
            obligation = defense_by_id.get(defense_id)
            if obligation is None:
                raise FukamiruError("Body section assigned a non-body defense")
            global_thesis_defense = (
                obligation.target_claim_id in introduction_claim_ids
                and obligation.target_claim_id not in body_claim_ids
            )
            if (
                obligation.target_claim_id not in set(raw_section.get("claim_ids") or [])
                and not global_thesis_defense
            ):
                raise FukamiruError("Body defense is assigned outside its target claim")
            assigned.append(defense_id)
    if len(assigned) != len(set(assigned)) or set(assigned) != set(defense_by_id):
        raise FukamiruError("Every material defense must be assigned exactly once")
    for section in sections:
        if section.get("counterarguments") and not (
            section.get("article_role") == "body"
            and section.get("defense_obligation_ids")
        ):
            raise FukamiruError(
                "Section contract surfaces a counterargument without a body defense obligation"
            )
    return intro, body_contracts, ending


def _semantic_resolution_ngrams(value: Any) -> set[str]:
    """Return content overlap units for deterministic cross-section routing."""

    compact = re.sub(r"\s+", "", str(value or "")).lower()
    segments = [
        segment
        for segment in re.split(
            r"[\n\r、。・,.;:：；!?！？/／()（）\[\]{}「」『』〈〉《》=＝→]+",
            compact,
        )
        if segment
    ]
    return {
        segment[start : start + size]
        for segment in segments
        for size in range(3, min(8, len(segment)) + 1)
        for start in range(0, len(segment) - size + 1)
    }


def _global_defense_resolution_section(
    defense: dict[str, Any],
    body_sections: list[dict[str, Any]],
) -> str:
    """Choose the Body section whose stated work resolves a global defense."""

    if not body_sections:
        return ""
    defense_ngrams = _semantic_resolution_ngrams(
        " ".join(
            str(defense.get(field) or "")
            for field in ("challenge", "materiality", "response")
        )
    )
    section_ngrams = [
        _semantic_resolution_ngrams(
            " ".join(
                str(section.get(field) or "")
                for field in ("title", "section_task", "section_output", "thesis_link")
            )
        )
        for section in body_sections
    ]
    document_frequency = {
        gram: sum(gram in grams for grams in section_ngrams)
        for gram in defense_ngrams
    }
    scores = [
        sum(
            (len(gram) - 2) ** 2 * (len(body_sections) + 1 - document_frequency[gram])
            for gram in defense_ngrams & grams
            if document_frequency[gram] <= max(1, len(body_sections) // 3)
        )
        for grams in section_ngrams
    ]
    if scores and max(scores) > 0:
        return str(body_sections[scores.index(max(scores))].get("section_id") or "")

    defense_id = str(defense.get("id") or "")
    existing = [
        str(section.get("section_id") or "")
        for section in body_sections
        if defense_id in {
            str(value) for value in section.get("defense_obligation_ids") or []
        }
    ]
    if len(existing) == 1:
        return existing[0]
    return str(body_sections[-1].get("section_id") or "")


def normalize_body_section_dependencies(
    raw: list[dict[str, Any]],
    *,
    defenses: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Normalize Body dependencies and place defenses with their target claims.

    Introduction frames the argument but does not establish a prior Body result.
    Scope-absorbed and internal defenses are not prose moves. Unknown, Conclusion,
    and forward Body references remain for strict validation. A material defense is
    assigned deterministically to the Body section containing its target claim. A
    defense of the Introduction's global thesis is assigned to the Body section
    whose task, output, title, or thesis link most directly matches the challenge
    and response. Existing explicit placement is retained when there is no semantic
    signal; the final Body section is only the compatibility fallback.
    """

    introduction_ids = {
        str(item.get("section_id") or "")
        for item in raw
        if item.get("article_role") == "introduction"
    }
    nonbody_defense_ids = {
        str(item.get("id") or "")
        for item in defenses or []
        if item.get("disposition") != "body"
    }
    body_sections = [item for item in raw if item.get("article_role") == "body"]
    body_claim_sections: dict[str, list[str]] = {}
    for item in body_sections:
        section_id = str(item.get("section_id") or "")
        for claim_id in item.get("claim_ids") or []:
            body_claim_sections.setdefault(str(claim_id), []).append(section_id)
    introduction_claim_ids = {
        str(claim_id)
        for item in raw
        if item.get("article_role") == "introduction"
        for claim_id in item.get("claim_ids") or []
    }
    expected_defenses_by_section: dict[str, list[str]] = {
        str(item.get("section_id") or ""): [] for item in body_sections
    }
    assignable_body_defense_ids: set[str] = set()
    for defense in defenses or []:
        if defense.get("disposition") != "body":
            continue
        defense_id = str(defense.get("id") or "")
        target_claim_id = str(defense.get("target_claim_id") or "")
        target_sections = body_claim_sections.get(target_claim_id, [])
        target_section_id = target_sections[0] if len(target_sections) == 1 else ""
        if (
            not target_section_id
            and target_claim_id in introduction_claim_ids
            and target_claim_id not in body_claim_sections
        ):
            target_section_id = _global_defense_resolution_section(
                defense, body_sections
            )
        if defense_id and target_section_id:
            expected_defenses_by_section[target_section_id].append(defense_id)
            assignable_body_defense_ids.add(defense_id)
    normalized: list[dict[str, Any]] = []
    corrections: list[dict[str, Any]] = []
    for item in raw:
        section = dict(item)
        if section.get("article_role") == "body":
            dependencies = list(section.get("required_input_section_ids") or [])
            removed = [value for value in dependencies if str(value) in introduction_ids]
            if removed:
                section["required_input_section_ids"] = [
                    value for value in dependencies if str(value) not in introduction_ids
                ]
                corrections.append(
                    {
                        "section_id": str(section.get("section_id") or ""),
                        "removed_introduction_ids": removed,
                        "reason": "introduction_is_not_established_body_output",
                    }
                )
            assigned = list(section.get("defense_obligation_ids") or [])
            removed_defenses = [
                value for value in assigned if str(value) in nonbody_defense_ids
            ]
            if removed_defenses:
                section["defense_obligation_ids"] = [
                    value for value in assigned
                    if str(value) not in nonbody_defense_ids
                ]
                corrections.append(
                    {
                        "section_id": str(section.get("section_id") or ""),
                        "removed_nonbody_defense_ids": removed_defenses,
                        "reason": "only_body_defenses_are_section_moves",
                    }
                )
            current_body_defenses = [
                str(value)
                for value in section.get("defense_obligation_ids") or []
                if str(value) not in nonbody_defense_ids
            ]
            expected_defenses = [
                value
                for value in current_body_defenses
                if value not in assignable_body_defense_ids
            ] + expected_defenses_by_section.get(
                str(section.get("section_id") or ""), []
            )
            if current_body_defenses != expected_defenses:
                section["defense_obligation_ids"] = expected_defenses
                stale_counterargument_count = len(section.get("counterarguments") or [])
                if stale_counterargument_count:
                    section["counterarguments"] = []
                corrections.append(
                    {
                        "section_id": str(section.get("section_id") or ""),
                        "from_defense_obligation_ids": current_body_defenses,
                        "to_defense_obligation_ids": expected_defenses,
                        "removed_stale_counterargument_count": (
                            stale_counterargument_count
                        ),
                        "reason": "place_body_defense_with_target_claim",
                    }
                )
        if section.get("counterarguments") and not (
            section.get("article_role") == "body"
            and section.get("defense_obligation_ids")
        ):
            count = len(section["counterarguments"])
            section["counterarguments"] = []
            corrections.append(
                {
                    "section_id": str(section.get("section_id") or ""),
                    "removed_unassigned_counterargument_count": count,
                    "reason": "counterarguments_require_body_defense_obligation",
                }
            )
        normalized.append(section)
    return normalized, corrections


def normalize_v31_draft_references(
    sections: list[dict[str, Any]],
    *,
    contracts: list[dict[str, Any]],
    argument_nodes: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Correct role draft ID metadata without changing prose or adding evidence."""

    by_id = {str(item.get("id") or ""): item for item in argument_nodes}
    contracts_by_id = {str(item.get("section_id") or ""): item for item in contracts}
    citation_nodes: dict[str, list[dict[str, Any]]] = {}
    for node in argument_nodes:
        if (
            node.get("node_type") != "evidence"
            or not str(node.get("id") or "").startswith("annotation-citation-node:")
        ):
            continue
        for source_id in dict(node.get("provenance") or {}).get("source_ids") or []:
            citation_nodes.setdefault(str(source_id), []).append(node)

    normalized: list[dict[str, Any]] = []
    corrections: list[dict[str, Any]] = []
    for source in sections:
        section = dict(source)
        section_id = str(section.get("section_id") or "")
        declared_evidence_ids = list(section.get("evidence_ids") or [])
        contract = contracts_by_id.get(section_id, {})
        claim_ids = {str(value) for value in contract.get("claim_ids") or []}
        move_ids = list(dict(contract.get("development_expectations") or {}).get(
            "required_argument_moves"
        ) or [])
        mapped: dict[str, str] = {}
        removed: set[str] = set()
        qualifier_ids = list(section.get("realized_qualifier_ids") or [])

        def evidence_id(
            value: Any,
            *,
            removed: set[str] = removed,
            qualifier_ids: list[str] = qualifier_ids,
            claim_ids: set[str] = claim_ids,
            mapped: dict[str, str] = mapped,
        ) -> str | None:
            original = str(value)
            node = by_id.get(original)
            if node is not None and node.get("node_type") != "evidence":
                removed.add(original)
                if node.get("node_type") == "qualifier" and original not in qualifier_ids:
                    qualifier_ids.append(original)
                return None
            if node is None:
                candidates = [
                    item for item in citation_nodes.get(original, [])
                    if str(item.get("claim_id") or "") in claim_ids
                ]
                if len(candidates) == 1:
                    replacement = str(candidates[0]["id"])
                    mapped[original] = replacement
                    return replacement
            return original

        paragraphs: list[dict[str, Any]] = []
        for source_paragraph in section.get("paragraphs") or []:
            paragraph = dict(source_paragraph)
            indices = list(paragraph.get("move_indices") or [])
            valid_indices = [
                value for value in indices
                if str(value).isdigit()
                and 1 <= int(value) <= len(move_ids)
                and move_ids[int(value) - 1] in PARAGRAPH_ARGUMENT_MOVE_IDS
            ]
            if indices != valid_indices:
                paragraph["move_indices"] = valid_indices
                corrections.append({
                    "section_id": section_id,
                    "removed_out_of_contract_move_indices": [
                        value for value in indices if value not in valid_indices
                    ],
                    "reason": "paragraph_move_indices_must_resolve_to_catalog_ids",
                })
            paragraph["evidence_ids"] = list(dict.fromkeys(
                replacement
                for value in paragraph.get("evidence_ids") or []
                if (replacement := evidence_id(value)) is not None
            ))
            paragraphs.append(paragraph)
        section["paragraphs"] = paragraphs
        section["realized_evidence_ids"] = list(dict.fromkeys(
            value for paragraph in paragraphs for value in paragraph["evidence_ids"]
        ))
        section["evidence_ids"] = list(section["realized_evidence_ids"])
        section["realized_qualifier_ids"] = qualifier_ids
        treatments: list[dict[str, Any]] = []
        dropped_treatment_ids: set[str] = set()
        realized_evidence_ids = set(section["realized_evidence_ids"])
        for source_treatment in section.get("evidence_treatment") or []:
            treatment = dict(source_treatment)
            replacement = evidence_id(treatment.get("evidence_id") or "")
            if replacement is not None and replacement in realized_evidence_ids:
                treatment["evidence_id"] = replacement
                treatments.append(treatment)
            elif replacement is not None:
                dropped_treatment_ids.add(replacement)
        section["evidence_treatment"] = treatments
        if declared_evidence_ids != section["evidence_ids"] or dropped_treatment_ids:
            corrections.append({
                "section_id": section_id,
                "previous_section_evidence_ids": declared_evidence_ids,
                "normalized_section_evidence_ids": list(section["evidence_ids"]),
                "removed_unrealized_treatment_ids": sorted(dropped_treatment_ids),
                "reason": "section_evidence_metadata_must_match_paragraph_realization",
            })
        if mapped or removed:
            corrections.append({
                "section_id": section_id,
                "mapped_citation_ids": mapped,
                "removed_non_evidence_node_ids": sorted(removed),
                "reason": "draft_evidence_ids_must_reference_graph_evidence",
            })
        normalized.append(section)
    return normalized, corrections


def normalize_conclusion_evidence_declaration(
    sections: list[dict[str, Any]],
    *,
    conclusion_synthesis: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Remove undeclared new evidence metadata from a synthesis-only Conclusion.

    Earlier-section evidence may be reused in the Conclusion. Evidence first
    realized there is retained only when ``new_evidence_ids`` declares it; the
    subsequent synthesis validator still requires a rhetorical reason. When the
    model declares no new evidence, a high-level synthesis sentence is supported
    by the established Body outputs and must not acquire Conclusion-only evidence
    IDs merely because those IDs were available in its section contract.
    """

    prior_evidence = {
        str(evidence_id)
        for section in sections
        if section.get("article_role") in {"introduction", "body"}
        for evidence_id in section.get("realized_evidence_ids") or []
    }
    declared_new = {
        str(evidence_id)
        for evidence_id in conclusion_synthesis.get("new_evidence_ids") or []
        if str(evidence_id)
    }
    allowed = prior_evidence | declared_new
    normalized: list[dict[str, Any]] = []
    corrections: list[dict[str, Any]] = []
    for source in sections:
        section = dict(source)
        if section.get("article_role") != "conclusion":
            normalized.append(section)
            continue
        removed: set[str] = set()
        for field in ("evidence_ids", "realized_evidence_ids"):
            values = [str(value) for value in section.get(field) or [] if str(value)]
            removed.update(value for value in values if value not in allowed)
            section[field] = [value for value in values if value in allowed]
        paragraphs: list[dict[str, Any]] = []
        for source_paragraph in section.get("paragraphs") or []:
            paragraph = dict(source_paragraph)
            paragraph_evidence = [
                str(value) for value in paragraph.get("evidence_ids") or [] if str(value)
            ]
            removed.update(value for value in paragraph_evidence if value not in allowed)
            paragraph["evidence_ids"] = [
                value for value in paragraph_evidence if value in allowed
            ]
            paragraphs.append(paragraph)
        section["paragraphs"] = paragraphs
        treatments: list[dict[str, Any]] = []
        for source_treatment in section.get("evidence_treatment") or []:
            treatment = dict(source_treatment)
            evidence_id = str(treatment.get("evidence_id") or "")
            if evidence_id and evidence_id not in allowed:
                removed.add(evidence_id)
            else:
                treatments.append(treatment)
        section["evidence_treatment"] = treatments
        if removed:
            corrections.append(
                {
                    "section_id": str(section.get("section_id") or ""),
                    "removed_undeclared_new_evidence_ids": sorted(removed),
                    "declared_new_evidence_ids": sorted(declared_new),
                    "reason": "conclusion_new_evidence_requires_explicit_declaration",
                }
            )
        normalized.append(section)
    return normalized, corrections


def validate_conclusion_synthesis(
    raw: dict[str, Any],
    *,
    conclusion_contract: ConclusionRoleContract,
    conclusion_section: dict[str, Any],
    body_sections: list[dict[str, Any]],
    introduction_section: dict[str, Any] | None = None,
) -> ConclusionSynthesis:
    synthesis = ConclusionSynthesis.model_validate(raw)
    body_ids = tuple(str(item.get("section_id") or "") for item in body_sections)
    if synthesis.established_section_ids != body_ids:
        raise FukamiruError("Conclusion synthesis lost or invented body outputs")
    if synthesis.established_section_ids != conclusion_contract.established_section_ids:
        raise FukamiruError("Conclusion synthesis disagrees with its locked contract")
    used_evidence = set(conclusion_section.get("realized_evidence_ids") or [])
    previous_sections = [*body_sections]
    if introduction_section is not None:
        previous_sections.append(introduction_section)
    previous_evidence = {
        str(evidence_id)
        for section in previous_sections
        for evidence_id in section.get("realized_evidence_ids") or []
    }
    actual_new = used_evidence - previous_evidence
    if set(synthesis.new_evidence_ids) != actual_new:
        raise FukamiruError("Conclusion new evidence metadata disagrees with realized evidence")
    if actual_new and not synthesis.new_evidence_rhetorical_reason.strip():
        raise FukamiruError("Conclusion introduced new evidence without a rhetorical reason")
    return synthesis
