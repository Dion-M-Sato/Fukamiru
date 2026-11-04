"""Validated data structures for argument and discourse plans."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fukamiru.authoring.evidence_contracts import (
    FindingDisposition,
    RepairOperation,
    SectionOutput,
)
from fukamiru.authoring.structure_aware import ArchitectureV31

Provenance = Literal["memo", "rag", "llm_inference", "user", "mixed"]
PropositionType = Literal[
    "fact",
    "observation",
    "interpretation",
    "hypothesis",
    "definition",
    "method",
    "reservation",
    "example",
]
ClaimLevel = Literal["main", "sub", "supporting"]
ArgumentNodeType = Literal[
    "claim",
    "evidence",
    "warrant",
    "qualifier",
    "counterexample",
    "rebuttal",
]
ArgumentRelation = Literal[
    "supports",
    "warrants",
    "qualifies",
    "challenges",
    "rebuts",
    "depends_on",
    "alternative_to",
]
RhetoricalRelation = Literal[
    "purpose",
    "evidence",
    "explanation",
    "elaboration",
    "contrast",
    "concession",
    "cause",
    "condition",
    "background",
    "result",
    "sequence",
    "summary",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Proposition(StrictModel):
    id: str
    text: str = Field(min_length=1)
    proposition_type: PropositionType
    provenance: Provenance
    argument_role: Literal["author_claim", "research_lead", "context", "reservation"] = (
        "author_claim"
    )
    source_block_id: str | None = None
    source_start: int | None = Field(default=None, ge=0)
    source_end: int | None = Field(default=None, ge=0)
    source_quote: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def valid_span(self) -> Proposition:
        if (self.source_start is None) != (self.source_end is None):
            raise ValueError("source_start and source_end must be provided together")
        if (
            self.source_start is not None
            and self.source_end is not None
            and self.source_end < self.source_start
        ):
            raise ValueError("source_end must not precede source_start")
        return self


class ResearchQuestion(StrictModel):
    text: str = Field(min_length=1)
    provisional_answer: str = ""
    research_gaps: tuple[str, ...] = ()
    rival_interpretations: tuple[str, ...] = ()
    scope: dict[str, object] = Field(default_factory=dict)
    assumptions: tuple[str, ...] = ()


class ClaimNode(StrictModel):
    id: str
    parent_id: str | None = None
    level: ClaimLevel
    text: str = Field(min_length=1)
    proposition_id: str | None = None
    rationale: str = ""
    research_gap: str = ""
    rival_interpretation: str = ""
    provenance: Provenance = "llm_inference"


class ArgumentNode(StrictModel):
    id: str
    node_type: ArgumentNodeType
    text: str = Field(min_length=1)
    claim_tree_node_id: str | None = None
    proposition_id: str | None = None
    source_span_id: str | None = None
    source_uri: str | None = None
    verification_status: Literal["unverified", "verified", "rejected"] = "unverified"
    provenance: Provenance = "llm_inference"
    confidence: float | None = Field(default=None, ge=0, le=1)
    details: dict[str, object] = Field(default_factory=dict)


class ArgumentEdge(StrictModel):
    id: str
    source_node_id: str
    target_node_id: str
    relation_type: ArgumentRelation
    rationale: str = ""
    provenance: Provenance = "llm_inference"


class HypothesisCandidate(StrictModel):
    id: str
    text: str = Field(min_length=1)
    gap_type: Literal[
        "causal",
        "comparison",
        "logical_leap",
        "alternative_explanation",
        "falsification",
        "boundary",
        "other",
    ]
    status: Literal["candidate", "adopted", "rejected"] = "candidate"
    rationale: str = ""
    provenance: Provenance = "llm_inference"


class DiscourseNode(StrictModel):
    id: str
    parent_id: str | None = None
    ordinal: int = Field(ge=0)
    node_kind: Literal["root", "section", "group"]
    article_role: Literal["root", "introduction", "body", "conclusion"] = "body"
    rhetorical_relation: RhetoricalRelation
    attentional_focus: str = ""
    discourse_purpose: str = ""
    section_answer: str = ""
    claim_ids: tuple[str, ...] = ()
    section_id: str | None = None


class SectionContract(StrictModel):
    id: str
    discourse_node_id: str
    title: str
    local_question: str
    article_role: Literal["introduction", "body", "conclusion"] = "body"
    expected_conclusion: str
    section_answer: str = ""
    section_output_contract: SectionOutput | None = None
    claim_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    warrant_ids: tuple[str, ...] = ()
    qualifiers: tuple[str, ...] = ()
    counterarguments: tuple[str, ...] = ()
    previous_bridge: str = ""
    next_bridge: str = ""
    required_terms: tuple[str, ...] = ()
    forbidden_topics: tuple[str, ...] = ()
    minimum_evidence_items: int = Field(default=1, ge=0)
    minimum_concrete_examples: int = Field(default=1, ge=0)
    required_argument_moves: tuple[str, ...] = ()
    target_length: int | None = Field(default=None, gt=0)


class AuditFinding(StrictModel):
    id: str
    defect_type: Literal[
        "claim_drift",
        "topic_drift",
        "unsupported_claim",
        "missing_warrant",
        "evidence_mismatch",
        "qualifier_loss",
        "counterargument_omission",
        "conclusion_mismatch",
        "focus_break",
        "redundancy",
        "contradiction",
        "citation_overclaim",
        "structure_invalid",
        "other",
    ]
    severity: Literal["info", "minor", "major", "critical"]
    message: str
    object_type: str | None = None
    object_id: str | None = None
    details: dict[str, object] = Field(default_factory=dict)
    status: Literal["open", "repairing", "resolved", "accepted", "wont_fix"] = "open"
    audit_version: str | None = None
    manuscript_hash: str | None = None
    target_ids: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    disposition: FindingDisposition | None = None
    repair_operations: tuple[RepairOperation, ...] = ()


class SectionDraft(StrictModel):
    id: str
    contract_id: str
    attempt: int = Field(default=1, gt=0)
    body: str
    used_claim_ids: tuple[str, ...] = ()
    used_evidence_ids: tuple[str, ...] = ()
    new_propositions: tuple[str, ...] = ()
    content_sha256: str


class AuthoringPlan(StrictModel):
    id: str
    document_id: str
    source_revision_id: str
    revision: int = Field(gt=0)
    parent_revision: int | None = None
    schema_version: int = Field(default=3, ge=1)
    fixed_asset_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    research_question: ResearchQuestion
    propositions: tuple[Proposition, ...] = ()
    claims: tuple[ClaimNode, ...] = ()
    argument_nodes: tuple[ArgumentNode, ...] = ()
    argument_edges: tuple[ArgumentEdge, ...] = ()
    hypotheses: tuple[HypothesisCandidate, ...] = ()
    discourse_nodes: tuple[DiscourseNode, ...] = ()
    section_contracts: tuple[SectionContract, ...] = ()
    architecture_v31: ArchitectureV31 | None = None

    @model_validator(mode="after")
    def validate_structure(self) -> AuthoringPlan:
        def ids(values):
            return [value.id for value in values]

        for label, values in (
            ("proposition", self.propositions),
            ("claim", self.claims),
            ("argument node", self.argument_nodes),
            ("discourse node", self.discourse_nodes),
        ):
            if len(ids(values)) != len(set(ids(values))):
                raise ValueError(f"duplicate {label} id")
        claims = {item.id: item for item in self.claims}
        mains = [item for item in self.claims if item.level == "main"]
        if self.claims and len(mains) != 1:
            raise ValueError("claim tree must contain exactly one main claim")
        for item in self.claims:
            if item.level == "main" and item.parent_id is not None:
                raise ValueError("main claim cannot have a parent")
            if item.parent_id is not None and item.parent_id not in claims:
                raise ValueError(f"unknown parent claim {item.parent_id}")
            if item.proposition_id and item.proposition_id not in set(ids(self.propositions)):
                raise ValueError(f"unknown proposition {item.proposition_id}")
            if item.parent_id is not None:
                parent = claims[item.parent_id]
                if parent.level == "supporting" or (parent.level == "main" and item.level != "sub"):
                    raise ValueError("claim parent and child levels are inconsistent")
                if parent.level == "sub" and item.level != "supporting":
                    raise ValueError("subclaims may only contain supporting claims")
        _validate_acyclic({item.id: item.parent_id for item in self.claims if item.parent_id})
        argument_ids = set(ids(self.argument_nodes))
        proposition_ids = set(ids(self.propositions))
        for edge in self.argument_edges:
            if edge.source_node_id not in argument_ids or edge.target_node_id not in argument_ids:
                raise ValueError("argument edge references an unknown node")
            if edge.source_node_id == edge.target_node_id:
                raise ValueError("argument edge cannot reference itself")
        for item in self.argument_nodes:
            if item.claim_tree_node_id and item.claim_tree_node_id not in claims:
                raise ValueError(
                    f"argument node references an unknown claim {item.claim_tree_node_id}"
                )
            if item.proposition_id and item.proposition_id not in proposition_ids:
                raise ValueError(
                    f"argument node references an unknown proposition {item.proposition_id}"
                )
        discourse = {item.id: item for item in self.discourse_nodes}
        roots = [item for item in self.discourse_nodes if item.parent_id is None]
        if self.discourse_nodes and (len(roots) != 1 or roots[0].node_kind != "root"):
            raise ValueError("discourse tree must contain exactly one root node")
        for item in self.discourse_nodes:
            if item.parent_id is not None and item.parent_id not in discourse:
                raise ValueError(f"unknown discourse parent {item.parent_id}")
            if item.node_kind == "section" and not item.section_id:
                raise ValueError("section discourse nodes require section_id")
            if item.node_kind != "section" and item.section_id is not None:
                raise ValueError("only section discourse nodes may have section_id")
            if item.node_kind == "root" and item.article_role != "root":
                raise ValueError("discourse root must have the root article role")
            if item.node_kind == "section" and item.article_role == "root":
                raise ValueError("discourse sections cannot have the root article role")
            if any(claim_id not in claims for claim_id in item.claim_ids):
                raise ValueError("discourse node references an unknown claim")
        _validate_acyclic(
            {item.id: item.parent_id for item in self.discourse_nodes if item.parent_id}
        )
        discourse_ids = set(discourse)
        for contract in self.section_contracts:
            if contract.discourse_node_id not in discourse_ids:
                raise ValueError("section contract references an unknown discourse node")
            if any(claim_id not in claims for claim_id in contract.claim_ids):
                raise ValueError("section contract references an unknown claim")
            if any(
                node_id not in argument_ids
                for node_id in contract.evidence_ids + contract.warrant_ids
            ):
                raise ValueError("section contract references an unknown argument node")
            discourse_node = discourse[contract.discourse_node_id]
            if discourse_node.node_kind != "section" or discourse_node.section_id != contract.id:
                raise ValueError("section contract and discourse section id must match")
            if contract.article_role != discourse_node.article_role:
                raise ValueError("section contract changed the discourse article role")
            if set(contract.claim_ids) != set(discourse_node.claim_ids):
                raise ValueError("section contract changed the discourse claim assignment")
        return self


def _validate_acyclic(parents: dict[str, str]) -> None:
    for start in parents:
        seen: set[str] = set()
        current: str | None = start
        while current is not None and current in parents:
            if current in seen:
                raise ValueError("tree contains a cycle")
            seen.add(current)
            current = parents.get(current)
