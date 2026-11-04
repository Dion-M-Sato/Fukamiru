"""Normalization and orchestration helpers for plan-first authoring."""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

from fukamiru.authoring.models import (
    ArgumentEdge,
    ArgumentNode,
    AuthoringPlan,
    ClaimNode,
    DiscourseNode,
    HypothesisCandidate,
    Proposition,
    ResearchQuestion,
    SectionContract,
    SectionDraft,
)
from fukamiru.authoring.repository import AuthoringRepository
from fukamiru.authoring.structure_aware import ArchitectureV31
from fukamiru.errors import FukamiruError
from fukamiru.revisions.service import RevisionService


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def _plan_scoped_id(plan_id: str, revision: int, value: object) -> str:
    """Keep model-generated local IDs unique across persisted plans and stable on retry."""
    raw = str(value)
    prefix = f"{plan_id}@{revision}:"
    return raw if raw.startswith(prefix) else f"{prefix}{raw}"


def _provenance(value: object) -> str:
    if isinstance(value, dict):
        value = value.get("source")
    known = {"memo", "rag", "llm_inference", "user", "mixed"}
    return str(value) if value in known else "llm_inference"


def build_plan(context: dict[str, Any]) -> AuthoringPlan:
    """Convert strict stage outputs into the canonical immutable plan model."""
    plan_id = str(context["plan_id"])
    revision = int(context.get("plan_revision", 1))
    classifications = {
        str(item["proposition_id"]): item for item in context.get("classifications", [])
    }
    propositions = []
    for item in context.get("propositions", []):
        classification = classifications.get(str(item["id"]), {})
        propositions.append(
            Proposition(
                id=_plan_scoped_id(plan_id, revision, item["id"]),
                text=str(item["text"]),
                proposition_type=classification.get("type", "interpretation"),
                argument_role=classification.get("argument_role", "author_claim"),
                provenance=_provenance(item.get("provenance")),
                source_quote=str(item.get("source_span") or item["text"]),
                confidence=classification.get("confidence"),
            )
        )

    raw_question = context["research_question"]
    question = ResearchQuestion(
        text=str(raw_question["text"]),
        provisional_answer=str(raw_question.get("provisional_answer", "")),
        research_gaps=tuple(raw_question.get("research_gaps", [])),
        rival_interpretations=tuple(raw_question.get("rival_interpretations", [])),
        scope={
            "description": str(raw_question.get("scope", "")),
            "out_of_scope": list(raw_question.get("out_of_scope", [])),
        },
        assumptions=tuple(raw_question.get("assumptions", [])),
    )

    level_map = {"main_claim": "main", "subclaim": "sub", "supporting_claim": "supporting"}
    raw_claims = list(context.get("claims", []))
    claim_ids = {
        str(item["id"]): _plan_scoped_id(plan_id, revision, item["id"]) for item in raw_claims
    }
    proposition_ids = {
        str(item["id"]): _plan_scoped_id(plan_id, revision, item["id"])
        for item in context.get("propositions", [])
    }
    claims = tuple(
        ClaimNode(
            id=claim_ids[str(item["id"])],
            parent_id=claim_ids.get(str(item["parent_id"]))
            if item.get("parent_id") is not None
            else None,
            level=level_map.get(str(item.get("level")), str(item.get("level"))),
            text=str(item["text"]),
            rationale="Directly answers the research question"
            if item.get("answers_question")
            else "",
            research_gap=str(item.get("research_gap", "")),
            rival_interpretation=str(item.get("rival_interpretation", "")),
            proposition_id=proposition_ids.get(str(item["proposition_id"]))
            if item.get("proposition_id") is not None
            else None,
            provenance=_provenance(item.get("provenance")),
        )
        for item in context.get("claims", [])
    )

    raw_argument_nodes = list(context.get("argument_nodes", []))
    argument_node_ids = {
        str(item["id"]): _plan_scoped_id(plan_id, revision, item["id"])
        for item in raw_argument_nodes
    }
    argument_nodes = tuple(
        ArgumentNode(
            id=argument_node_ids[str(item["id"])],
            node_type=item["node_type"],
            text=str(item["text"]),
            claim_tree_node_id=claim_ids.get(str(item.get("claim_id")))
            if item.get("claim_id") is not None
            else claim_ids.get(str(item["id"])),
            proposition_id=proposition_ids.get(str(item["proposition_id"]))
            if item.get("proposition_id") is not None
            else None,
            source_span_id=item.get("source_span_id")
            or next(iter((item.get("provenance") or {}).get("source_ids", [])), None),
            verification_status=(item.get("provenance") or {})
            .get("verification_status", "unverified")
            .replace("not_applicable", "unverified"),
            provenance=_provenance(item.get("provenance")),
            details={"source_ids": (item.get("provenance") or {}).get("source_ids", [])},
        )
        for item in context.get("argument_nodes", [])
    )
    argument_edges = tuple(
        ArgumentEdge(
            id=_plan_scoped_id(
                plan_id,
                revision,
                item.get("id")
                or f"edge-{item['source_id']}-{item['target_id']}-{item['relation']}",
            ),
            source_node_id=argument_node_ids.get(
                str(item["source_id"]),
                _plan_scoped_id(plan_id, revision, item["source_id"]),
            ),
            target_node_id=argument_node_ids.get(
                str(item["target_id"]),
                _plan_scoped_id(plan_id, revision, item["target_id"]),
            ),
            relation_type=item["relation"],
        )
        for item in context.get("argument_edges", [])
    )

    allowed_gaps = {
        "causal",
        "comparison",
        "logical_leap",
        "alternative_explanation",
        "falsification",
        "boundary",
        "other",
    }
    hypotheses = tuple(
        HypothesisCandidate(
            id=_plan_scoped_id(plan_id, revision, item["id"]),
            text=str(item["text"]),
            gap_type=item["gap_type"] if item.get("gap_type") in allowed_gaps else "other",
            status=item.get("status", "candidate"),
            rationale=str(item.get("rationale", "")),
            provenance=_provenance(item.get("provenance")),
        )
        for item in context.get("hypotheses", [])
    )

    raw_discourse = list(context.get("discourse_nodes", []))
    normalized_section_ids = {
        str(item["section_id"]): _plan_scoped_id(plan_id, revision, item["section_id"])
        for item in raw_discourse
        if item.get("section_id") and item.get("parent_id") is not None
    }
    discourse_nodes = tuple(
        DiscourseNode(
            id=_plan_scoped_id(plan_id, revision, item["id"]),
            parent_id=_plan_scoped_id(plan_id, revision, item["parent_id"])
            if item.get("parent_id") is not None
            else None,
            ordinal=index,
            node_kind="root" if item.get("parent_id") is None else "section",
            article_role=str(
                item.get("article_role") or ("root" if item.get("parent_id") is None else "body")
            ),
            rhetorical_relation="purpose"
            if item.get("parent_id") is None
            else item.get("relation", "elaboration"),
            attentional_focus=str(item.get("attentional_focus", "")),
            discourse_purpose=str(item.get("discourse_purpose", "")),
            section_answer=str(item.get("section_answer", "")),
            claim_ids=tuple(
                claim_ids.get(str(value), _plan_scoped_id(plan_id, revision, value))
                for value in item.get("claim_ids", [])
            ),
            section_id=normalized_section_ids.get(str(item.get("section_id")))
            if item.get("section_id") and item.get("parent_id") is not None
            else None,
        )
        for index, item in enumerate(raw_discourse)
    )
    discourse_by_section = {
        raw_section_id: next(
            item.id for item in discourse_nodes if item.section_id == normalized_section_id
        )
        for raw_section_id, normalized_section_id in normalized_section_ids.items()
    }
    contracts = tuple(
        SectionContract(
            id=normalized_section_ids[str(item["section_id"])],
            discourse_node_id=discourse_by_section.get(
                str(item["section_id"]),
                _plan_scoped_id(plan_id, revision, item["section_id"]),
            ),
            title=str(item["title"]),
            local_question=str(item["local_question"]),
            article_role=str(item.get("article_role") or "body"),
            expected_conclusion=str(item["expected_conclusion"]),
            section_answer=str(item.get("section_answer", item["expected_conclusion"])),
            section_output_contract=(
                item.get("section_output_contract")
                if item.get("article_role") == "body"
                else None
            ),
            claim_ids=tuple(
                claim_ids.get(str(value), _plan_scoped_id(plan_id, revision, value))
                for value in item.get("claim_ids", [])
            ),
            evidence_ids=tuple(
                argument_node_ids.get(str(value), _plan_scoped_id(plan_id, revision, value))
                for value in item.get("evidence_ids", [])
            ),
            warrant_ids=tuple(
                argument_node_ids.get(str(value), _plan_scoped_id(plan_id, revision, value))
                for value in item.get("warrant_ids", [])
            ),
            qualifiers=tuple(item.get("qualifiers", [])),
            counterarguments=tuple(item.get("counterarguments", [])),
            previous_bridge=str(item.get("previous_section_bridge", "")),
            next_bridge=str(item.get("next_section_bridge", "")),
            required_terms=tuple(item.get("required_terms", [])),
            forbidden_topics=tuple(item.get("forbidden_topics", [])),
            minimum_evidence_items=(
                0 if context.get("research_problem") is not None else int(
                    max(1, dict(item.get("development_expectations") or {}).get(
                        "minimum_evidence_items", 1
                    ))
                )
            ),
            minimum_concrete_examples=(
                0 if context.get("research_problem") is not None else int(
                    max(1, dict(item.get("development_expectations") or {}).get(
                        "minimum_concrete_examples", 1
                    ))
                )
            ),
            required_argument_moves=tuple(
                dict(item.get("development_expectations") or {}).get("required_argument_moves", [])
            ),
            target_length=(
                int(item["target_length"]) if item.get("target_length") is not None else None
            ),
        )
        for item in context.get("section_contracts", [])
    )
    architecture = None
    if context.get("research_problem") is not None:
        body_sections = [
            item
            for item in context.get("section_contracts", [])
            if item.get("article_role") == "body"
        ]
        architecture = ArchitectureV31.model_validate(
            {
                "research_problem": context["research_problem"],
                "claim_scopes": context.get("claim_scopes", []),
                "defense_obligations": context.get("defense_obligations", []),
                "introduction_contract": context["introduction_contract"],
                "body_section_contracts": [
                    {
                        "section_id": item["section_id"],
                        "section_task": item["section_task"],
                        "section_output": item["section_output"],
                        "section_output_contract": item.get("section_output_contract"),
                        "thesis_link": item["thesis_link"],
                        "required_input_section_ids": item["required_input_section_ids"],
                        "defense_obligation_ids": item["defense_obligation_ids"],
                    }
                    for item in body_sections
                ],
                "conclusion_contract": context["conclusion_contract"],
                "sections": context["section_contracts"],
            }
        )
    return AuthoringPlan(
        id=plan_id,
        document_id=str(context["document_id"]),
        source_revision_id=str(context["source_revision_id"]),
        revision=revision,
        parent_revision=context.get("parent_plan_revision"),
        schema_version=int(
            context.get("authoring_schema_version")
            or (4 if context.get("fixed_assets") else 3 if architecture is not None else 2)
        ),
        fixed_asset_hash=(
            str(dict(context.get("fixed_assets") or {}).get("asset_hash") or "") or None
        ),
        research_question=question,
        propositions=tuple(propositions),
        claims=claims,
        argument_nodes=argument_nodes,
        argument_edges=argument_edges,
        hypotheses=hypotheses,
        discourse_nodes=discourse_nodes,
        section_contracts=contracts,
        architecture_v31=architecture,
    )


def build_drafts(
    plan: AuthoringPlan,
    sections: list[dict[str, Any]],
    attempt: int,
) -> list[SectionDraft]:
    contracts_by_section = {
        str(node.section_id).split(":", 1)[-1]: contract
        for contract in plan.section_contracts
        for node in plan.discourse_nodes
        if node.id == contract.discourse_node_id and node.section_id
    }
    drafts: list[SectionDraft] = []
    for section in sections:
        section_id = str(section["section_id"])
        contract = contracts_by_section[section_id]
        body = str(section.get("text") or section.get("prose") or "")
        drafts.append(
            SectionDraft(
                id=_id("draft"),
                contract_id=contract.id,
                attempt=attempt,
                body=body,
                used_claim_ids=tuple(section.get("claim_ids", [])),
                used_evidence_ids=tuple(section.get("evidence_ids", [])),
                new_propositions=tuple(
                    str(item.get("text", "")) for item in section.get("new_propositions", [])
                ),
                content_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            )
        )
    return drafts


def plan_for_prompt(plan: AuthoringPlan) -> dict[str, Any]:
    projected = plan.model_dump(mode="json")
    if plan.architecture_v31 is not None:
        projected["section_contracts"] = list(plan.architecture_v31.sections)
    return projected


class AuthoringProposalService:
    """Human confirmation lifecycle for generated paper candidates."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def confirm(
        self,
        proposal_id: str,
        *,
        expected_sha256: str | None = None,
        commit_transaction: bool = True,
    ) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM authoring_pending_proposals WHERE id = ?",
            (proposal_id,),
        ).fetchone()
        if row is None:
            raise FukamiruError(f"Unknown authoring proposal: {proposal_id}")
        if row["status"] == "confirmed":
            return {
                "proposal_id": proposal_id,
                "status": "confirmed",
                "document_id": str(row["target_document_id"]),
                "revision_id": str(row["confirmed_revision_id"]),
                "sha256": str(row["proposed_sha256"]),
            }
        if row["status"] != "pending":
            raise FukamiruError("Only a pending authoring proposal can be confirmed")

        proposed_html = str(row["proposed_html"])
        actual_sha256 = hashlib.sha256(proposed_html.encode("utf-8")).hexdigest()
        if actual_sha256 != str(row["proposed_sha256"]):
            raise FukamiruError("Authoring proposal content hash does not match")
        if expected_sha256 is not None and expected_sha256 != actual_sha256:
            raise FukamiruError("Authoring proposal changed; reload before confirming")

        target = self.connection.execute(
            "SELECT current_revision_id FROM content_documents WHERE id = ?",
            (row["target_document_id"],),
        ).fetchone()
        if target is None:
            raise FukamiruError("Authoring proposal target document no longer exists")
        deterministic_revision_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"fukamiru:authoring-proposal:{proposal_id}")
        )
        if target["current_revision_id"] is not None:
            if target["current_revision_id"] == deterministic_revision_id:
                self._mark_confirmed(
                    proposal_id,
                    deterministic_revision_id,
                    commit_transaction=commit_transaction,
                )
                return self._confirmed_result(row, deterministic_revision_id)
            raise FukamiruError("Target paper already has a revision; refusing to overwrite it")

        try:
            revision = RevisionService(self.connection).commit_revision(
                document_id=str(row["target_document_id"]),
                canonical_html=proposed_html,
                editor_type="human",
                operation="create",
                message=f"Human-confirmed authoring proposal {proposal_id}",
                revision_id=deterministic_revision_id,
                commit_transaction=False,
            )
            AuthoringRepository.mark_pending_proposal_confirmed(
                self.connection,
                proposal_id=proposal_id,
                revision_id=revision.revision_id,
                resolved_at=datetime.now(UTC)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
            )
            if commit_transaction:
                self.connection.commit()
        except Exception as exc:
            if commit_transaction:
                self.connection.rollback()
            if isinstance(exc, sqlite3.IntegrityError):
                confirmed = self.connection.execute(
                    """
                    SELECT status, target_document_id, confirmed_revision_id, proposed_sha256
                    FROM authoring_pending_proposals WHERE id = ?
                    """,
                    (proposal_id,),
                ).fetchone()
                if confirmed is not None and confirmed["status"] == "confirmed":
                    return {
                        "proposal_id": proposal_id,
                        "status": "confirmed",
                        "document_id": str(confirmed["target_document_id"]),
                        "revision_id": str(confirmed["confirmed_revision_id"]),
                        "sha256": str(confirmed["proposed_sha256"]),
                    }
            raise
        return self._confirmed_result(row, revision.revision_id)

    def reject(self, proposal_id: str, *, commit_transaction: bool = True) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM authoring_pending_proposals WHERE id = ?",
            (proposal_id,),
        ).fetchone()
        if row is None:
            raise FukamiruError(f"Unknown authoring proposal: {proposal_id}")
        if row["status"] == "rejected":
            return {
                "proposal_id": proposal_id,
                "status": "rejected",
                "document_id": str(row["target_document_id"]),
                "revision_id": None,
                "sha256": str(row["proposed_sha256"]),
            }
        if row["status"] != "pending":
            raise FukamiruError("Only a pending authoring proposal can be rejected")
        resolved_at = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        cursor = self.connection.execute(
            """
            UPDATE authoring_pending_proposals
            SET status = 'rejected', resolved_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (resolved_at, proposal_id),
        )
        if cursor.rowcount != 1:
            if commit_transaction:
                self.connection.rollback()
            raise FukamiruError("Authoring proposal is no longer pending")
        if commit_transaction:
            self.connection.commit()
        return {
            "proposal_id": proposal_id,
            "status": "rejected",
            "document_id": str(row["target_document_id"]),
            "revision_id": None,
            "sha256": str(row["proposed_sha256"]),
        }

    def _mark_confirmed(
        self,
        proposal_id: str,
        revision_id: str,
        *,
        commit_transaction: bool = True,
    ) -> None:
        AuthoringRepository.mark_pending_proposal_confirmed(
            self.connection,
            proposal_id=proposal_id,
            revision_id=revision_id,
            resolved_at=datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        )
        if commit_transaction:
            self.connection.commit()

    @staticmethod
    def _confirmed_result(row: sqlite3.Row, revision_id: str) -> dict[str, Any]:
        return {
            "proposal_id": str(row["id"]),
            "status": "confirmed",
            "document_id": str(row["target_document_id"]),
            "revision_id": revision_id,
            "sha256": str(row["proposed_sha256"]),
        }
