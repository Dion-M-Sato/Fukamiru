"""SQLite persistence for immutable authoring-plan revisions."""

# SQL statements are kept as single strings for sqlite3's parameterized execute API.
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime

from fukamiru.authoring.models import (
    ArgumentEdge,
    ArgumentNode,
    AuditFinding,
    AuthoringPlan,
    ClaimNode,
    DiscourseNode,
    HypothesisCandidate,
    Proposition,
    SectionContract,
    SectionDraft,
)
from fukamiru.documents.canonical_html import CanonicalHTMLValidator
from fukamiru.storage.database import SQLiteDatabase
from fukamiru.workflow.identity import is_manuscript_generator_workflow


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class AuthoringRepository:
    """Stores plans and their structured components without mutating old revisions."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    def create_plan(self, plan: AuthoringPlan, *, producer_node_run_id: str | None = None) -> None:
        """Persist one plan revision; the plan id may already exist for a new branch revision."""
        if plan.revision == 1 and plan.parent_revision is not None:
            raise ValueError("revision 1 cannot have a parent")
        if plan.revision > 1 and plan.parent_revision is None:
            raise ValueError("subsequent revisions require a parent")
        created_at = _now()
        question = plan.research_question
        digest_payload = plan.model_dump(mode="json")
        if plan.architecture_v31 is None:
            # Preserve the V30 content digest for exact historical revision replay.
            digest_payload.pop("architecture_v31")
        digest = hashlib.sha256(_json(digest_payload).encode("utf-8")).hexdigest()
        with closing(self.database.connect()) as connection, connection:
            existing = connection.execute(
                "SELECT document_id, source_revision_id FROM authoring_plans WHERE id = ?",
                (plan.id,),
            ).fetchone()
            if existing is None:
                if plan.revision != 1:
                    raise ValueError("a new plan must start at revision 1")
                connection.execute(
                    "INSERT INTO authoring_plans(id, document_id, source_revision_id, current_revision, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (plan.id, plan.document_id, plan.source_revision_id, 1, created_at, created_at),
                )
            else:
                if (
                    existing["document_id"] != plan.document_id
                    or existing["source_revision_id"] != plan.source_revision_id
                ):
                    raise ValueError("plan document and source revision are immutable")
                same_revision = connection.execute(
                    "SELECT parent_revision, content_sha256 FROM authoring_plan_revisions WHERE plan_id = ? AND revision = ?",
                    (plan.id, plan.revision),
                ).fetchone()
                if same_revision is not None:
                    if (
                        same_revision["parent_revision"] == plan.parent_revision
                        and same_revision["content_sha256"] == digest
                    ):
                        # A resumed workflow may replay the successful persistence node
                        # after its downstream node failed. Only an exact immutable
                        # revision replay is safe to treat as already persisted.
                        return
                    raise ValueError(
                        f"authoring plan revision already exists with different content: {plan.id}@{plan.revision}"
                    )
                if (
                    connection.execute(
                        "SELECT 1 FROM authoring_plan_revisions WHERE plan_id = ? AND revision = ?",
                        (plan.id, plan.parent_revision),
                    ).fetchone()
                    is None
                ):
                    raise ValueError("parent plan revision does not exist")
                connection.execute(
                    "UPDATE authoring_plans SET current_revision = ?, updated_at = ? WHERE id = ?",
                    (plan.revision, created_at, plan.id),
                )
            # Child object IDs are global primary keys. Check the most common
            # cross-run collision explicitly so retries cannot accidentally adopt
            # IDs owned by another plan/revision.
            for proposition in plan.propositions:
                owner = connection.execute(
                    "SELECT plan_id, plan_revision FROM atomic_propositions WHERE id = ?",
                    (proposition.id,),
                ).fetchone()
                if owner is not None:
                    raise ValueError(
                        "atomic proposition id is already owned by another plan revision: "
                        f"{proposition.id} ({owner['plan_id']}@{owner['plan_revision']})"
                    )
            connection.execute(
                "INSERT INTO authoring_plan_revisions(plan_id, revision, parent_revision, research_question, scope_json, assumptions_json, provisional_answer, research_gaps_json, rival_interpretations_json, schema_version, architecture_json, fixed_asset_hash, content_sha256, producer_node_run_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    plan.id,
                    plan.revision,
                    plan.parent_revision,
                    question.text,
                    _json(question.scope),
                    _json(question.assumptions),
                    question.provisional_answer,
                    _json(question.research_gaps),
                    _json(question.rival_interpretations),
                    plan.schema_version,
                    _json(plan.architecture_v31.model_dump(mode="json"))
                    if plan.architecture_v31 is not None
                    else "{}",
                    plan.fixed_asset_hash,
                    digest,
                    producer_node_run_id,
                    created_at,
                ),
            )
            self._insert_children(connection, plan, created_at, producer_node_run_id)

    def load_plan(self, plan_id: str, revision: int | None = None) -> AuthoringPlan:
        with closing(self.database.connect()) as connection, connection:
            if revision is None:
                row = connection.execute(
                    "SELECT p.document_id, p.source_revision_id, r.* FROM authoring_plans p JOIN authoring_plan_revisions r ON r.plan_id = p.id AND r.revision = p.current_revision WHERE p.id = ?",
                    (plan_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT p.document_id, p.source_revision_id, r.* FROM authoring_plans p JOIN authoring_plan_revisions r ON r.plan_id = p.id WHERE p.id = ? AND r.revision = ?",
                    (plan_id, revision),
                ).fetchone()
            if row is None:
                raise KeyError(
                    f"unknown authoring plan revision: {plan_id}@{revision or 'current'}"
                )
            key = (plan_id, row["revision"])
            propositions = tuple(
                Proposition.model_validate(
                    {
                        "id": item["id"],
                        "text": item["text"],
                        "proposition_type": item["proposition_type"],
                        "provenance": item["provenance"],
                        "argument_role": item["argument_role"],
                        "source_block_id": item["source_block_id"],
                        "source_start": item["source_start"],
                        "source_end": item["source_end"],
                        "source_quote": item["source_quote"],
                        "confidence": item["confidence"],
                    }
                )
                for item in connection.execute(
                    "SELECT * FROM atomic_propositions WHERE plan_id = ? AND plan_revision = ? ORDER BY rowid",
                    key,
                )
            )
            claims = tuple(
                ClaimNode.model_validate(
                    {
                        "id": item["id"],
                        "parent_id": item["parent_id"],
                        "level": item["claim_level"],
                        "text": item["text"],
                        "proposition_id": item["proposition_id"],
                        "rationale": item["rationale"],
                        "research_gap": item["research_gap"],
                        "rival_interpretation": item["rival_interpretation"],
                        "provenance": item["provenance"],
                    }
                )
                for item in connection.execute(
                    "SELECT * FROM claim_tree_nodes WHERE plan_id = ? AND plan_revision = ? ORDER BY rowid",
                    key,
                )
            )
            argument_nodes = tuple(
                ArgumentNode.model_validate(
                    {
                        "id": item["id"],
                        "node_type": item["node_type"],
                        "text": item["text"],
                        "claim_tree_node_id": item["claim_tree_node_id"],
                        "proposition_id": item["proposition_id"],
                        "source_span_id": item["source_span_id"],
                        "source_uri": item["source_uri"],
                        "verification_status": item["verification_status"],
                        "provenance": item["provenance"],
                        "confidence": item["confidence"],
                        "details": json.loads(item["details_json"]),
                    }
                )
                for item in connection.execute(
                    "SELECT * FROM argument_nodes WHERE plan_id = ? AND plan_revision = ? ORDER BY rowid",
                    key,
                )
            )
            argument_edges = tuple(
                ArgumentEdge.model_validate(
                    {
                        "id": item["id"],
                        "source_node_id": item["source_node_id"],
                        "target_node_id": item["target_node_id"],
                        "relation_type": item["relation_type"],
                        "rationale": item["rationale"],
                        "provenance": item["provenance"],
                    }
                )
                for item in connection.execute(
                    "SELECT * FROM argument_edges WHERE plan_id = ? AND plan_revision = ? ORDER BY rowid",
                    key,
                )
            )
            hypotheses = tuple(
                HypothesisCandidate.model_validate(
                    {
                        "id": item["id"],
                        "text": item["text"],
                        "gap_type": item["gap_type"],
                        "status": item["status"],
                        "rationale": item["rationale"],
                        "provenance": item["provenance"],
                    }
                )
                for item in connection.execute(
                    "SELECT * FROM hypothesis_candidates WHERE plan_id = ? AND plan_revision = ? ORDER BY rowid",
                    key,
                )
            )
            discourse = tuple(
                DiscourseNode.model_validate(
                    {
                        "id": item["id"],
                        "parent_id": item["parent_id"],
                        "ordinal": item["ordinal"],
                        "node_kind": item["node_kind"],
                        "article_role": item["article_role"],
                        "rhetorical_relation": item["rhetorical_relation"],
                        "attentional_focus": item["attentional_focus"],
                        "discourse_purpose": item["discourse_purpose"],
                        "section_answer": item["section_answer"],
                        "claim_ids": json.loads(item["claim_ids_json"]),
                        "section_id": item["section_id"],
                    }
                )
                for item in connection.execute(
                    "SELECT * FROM discourse_nodes WHERE plan_id = ? AND plan_revision = ? ORDER BY rowid",
                    key,
                )
            )
            contracts = tuple(
                self._contract_from_row(item)
                for item in connection.execute(
                    "SELECT * FROM section_contracts WHERE plan_id = ? AND plan_revision = ? ORDER BY rowid",
                    key,
                )
            )
            return AuthoringPlan(
                id=plan_id,
                document_id=row["document_id"],
                source_revision_id=row["source_revision_id"],
                revision=row["revision"],
                parent_revision=row["parent_revision"],
                schema_version=row["schema_version"],
                fixed_asset_hash=row["fixed_asset_hash"],
                research_question={
                    "text": row["research_question"],
                    "provisional_answer": row["provisional_answer"],
                    "research_gaps": json.loads(row["research_gaps_json"]),
                    "rival_interpretations": json.loads(row["rival_interpretations_json"]),
                    "scope": json.loads(row["scope_json"]),
                    "assumptions": json.loads(row["assumptions_json"]),
                },
                propositions=propositions,
                claims=claims,
                argument_nodes=argument_nodes,
                argument_edges=argument_edges,
                hypotheses=hypotheses,
                discourse_nodes=discourse,
                section_contracts=contracts,
                architecture_v31=json.loads(row["architecture_json"])
                if row["architecture_json"] != "{}"
                else None,
            )

    def save_draft(self, draft: SectionDraft, *, producer_node_run_id: str | None = None) -> None:
        with closing(self.database.connect()) as connection, connection:
            connection.execute(
                "INSERT INTO section_drafts(id, contract_id, attempt, body, used_claim_ids_json, used_evidence_ids_json, new_propositions_json, producer_node_run_id, content_sha256, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    draft.id,
                    draft.contract_id,
                    draft.attempt,
                    draft.body,
                    _json(draft.used_claim_ids),
                    _json(draft.used_evidence_ids),
                    _json(draft.new_propositions),
                    producer_node_run_id,
                    draft.content_sha256,
                    _now(),
                ),
            )

    def list_drafts(self, contract_id: str) -> list[SectionDraft]:
        with closing(self.database.connect()) as connection, connection:
            return [
                SectionDraft(
                    id=row["id"],
                    contract_id=row["contract_id"],
                    attempt=row["attempt"],
                    body=row["body"],
                    used_claim_ids=tuple(json.loads(row["used_claim_ids_json"])),
                    used_evidence_ids=tuple(json.loads(row["used_evidence_ids_json"])),
                    new_propositions=tuple(json.loads(row["new_propositions_json"])),
                    content_sha256=row["content_sha256"],
                )
                for row in connection.execute(
                    "SELECT * FROM section_drafts WHERE contract_id = ? ORDER BY attempt",
                    (contract_id,),
                )
            ]

    def next_draft_attempt(self, contract_ids: list[str]) -> int:
        """Return an unused attempt number across a continued plan's contracts."""

        unique_ids = sorted({str(item) for item in contract_ids if str(item)})
        if not unique_ids:
            return 1
        placeholders = ", ".join("?" for _ in unique_ids)
        with closing(self.database.connect()) as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(attempt), 0) AS maximum_attempt "
                f"FROM section_drafts WHERE contract_id IN ({placeholders})",
                tuple(unique_ids),
            ).fetchone()
        return int(row["maximum_attempt"] or 0) + 1

    def add_audit_finding(self, plan_id: str, revision: int, finding: AuditFinding) -> None:
        with closing(self.database.connect()) as connection, connection:
            connection.execute(
                "INSERT INTO audit_findings(id, plan_id, plan_revision, defect_type, severity, object_type, object_id, message, details_json, status, audit_version, manuscript_hash, target_ids_json, source_ids_json, disposition, repair_operations_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    finding.id,
                    plan_id,
                    revision,
                    finding.defect_type,
                    finding.severity,
                    finding.object_type,
                    finding.object_id,
                    finding.message,
                    _json(finding.details),
                    finding.status,
                    finding.audit_version,
                    finding.manuscript_hash,
                    _json(finding.target_ids),
                    _json(finding.source_ids),
                    finding.disposition,
                    _json(
                        [item.model_dump(mode="json") for item in finding.repair_operations]
                    ),
                    _now(),
                ),
            )

    def list_audit_findings(
        self, plan_id: str, revision: int, *, status: str | None = None
    ) -> list[AuditFinding]:
        query = "SELECT * FROM audit_findings WHERE plan_id = ? AND plan_revision = ?"
        params: tuple[object, ...] = (plan_id, revision)
        if status is not None:
            query += " AND status = ?"
            params += (status,)
        query += " ORDER BY created_at, id"
        with closing(self.database.connect()) as connection, connection:
            return [
                AuditFinding(
                    id=row["id"],
                    defect_type=row["defect_type"],
                    severity=row["severity"],
                    object_type=row["object_type"],
                    object_id=row["object_id"],
                    message=row["message"],
                    details=json.loads(row["details_json"]),
                    status=row["status"],
                    audit_version=row["audit_version"],
                    manuscript_hash=row["manuscript_hash"],
                    target_ids=tuple(json.loads(row["target_ids_json"])),
                    source_ids=tuple(json.loads(row["source_ids_json"])),
                    disposition=row["disposition"],
                    repair_operations=tuple(json.loads(row["repair_operations_json"])),
                )
                for row in connection.execute(query, params)
            ]

    def save_evidence_state(
        self,
        *,
        run_id: str,
        fixed_assets: dict[str, object],
        evidence_uses: list[dict[str, object]],
        review_items: list[dict[str, object]],
    ) -> None:
        """Persist the current V31.1 checkpoint without mutating fixed assets."""

        asset_hash = str(fixed_assets.get("asset_hash") or "")
        schema_version = str(fixed_assets.get("schema_version") or "")
        with closing(self.database.connect()) as connection, connection:
            existing = connection.execute(
                "SELECT asset_hash,payload_json FROM authoring_fixed_asset_snapshots "
                "WHERE run_id=?",
                (run_id,),
            ).fetchone()
            payload_json = _json(fixed_assets)
            if existing is None:
                connection.execute(
                    "INSERT INTO authoring_fixed_asset_snapshots("
                    "run_id,schema_version,asset_hash,payload_json,created_at"
                    ") VALUES (?,?,?,?,?)",
                    (run_id, schema_version, asset_hash, payload_json, _now()),
                )
            elif existing["asset_hash"] != asset_hash or existing["payload_json"] != payload_json:
                raise ValueError("fixed asset snapshot is immutable")
            connection.execute(
                "DELETE FROM authoring_evidence_uses WHERE run_id=?", (run_id,)
            )
            connection.executemany(
                "INSERT INTO authoring_evidence_uses("
                "run_id,evidence_use_id,section_id,sentence_id,claim_id,payload_json,created_at"
                ") VALUES (?,?,?,?,?,?,?)",
                [
                    (
                        run_id,
                        str(item["evidence_use_id"]),
                        str(item["section_id"]),
                        str(item["sentence_id"]),
                        str(item["claim_id"]),
                        _json(item),
                        _now(),
                    )
                    for item in evidence_uses
                ],
            )
            connection.execute(
                "DELETE FROM authoring_review_items WHERE run_id=?", (run_id,)
            )
            connection.executemany(
                "INSERT INTO authoring_review_items("
                "run_id,review_item_id,section_id,sentence_id,status,payload_json,created_at"
                ") VALUES (?,?,?,?,?,?,?)",
                [
                    (
                        run_id,
                        str(item["review_item_id"]),
                        str(item["section_id"]),
                        str(item.get("sentence_id") or "") or None,
                        str(item.get("status") or "open"),
                        _json(item),
                        _now(),
                    )
                    for item in review_items
                ],
            )

    def load_evidence_state(self, run_id: str) -> dict[str, object] | None:
        """Restore the exact evidence/review JSON saved at a checkpoint."""

        with closing(self.database.connect()) as connection:
            fixed = connection.execute(
                "SELECT payload_json FROM authoring_fixed_asset_snapshots WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if fixed is None:
                return None
            evidence_uses = [
                json.loads(row["payload_json"])
                for row in connection.execute(
                    "SELECT payload_json FROM authoring_evidence_uses "
                    "WHERE run_id=? ORDER BY evidence_use_id",
                    (run_id,),
                )
            ]
            review_items = [
                json.loads(row["payload_json"])
                for row in connection.execute(
                    "SELECT payload_json FROM authoring_review_items "
                    "WHERE run_id=? ORDER BY review_item_id",
                    (run_id,),
                )
            ]
            return {
                "fixed_assets": json.loads(fixed["payload_json"]),
                "evidence_uses": evidence_uses,
                "human_review_items": review_items,
            }

    def create_pending_proposal(
        self,
        *,
        proposal_id: str,
        run_id: str,
        project_id: str,
        source_document_id: str,
        source_revision_id: str,
        target_document_id: str,
        plan_id: str,
        plan_revision: int,
        title: str,
        proposed_html: str,
        artifact_path: str | None = None,
        quality_status: str = "needs_human_review",
        review_context: dict[str, object] | None = None,
        replace_existing_pending: bool = False,
    ) -> dict[str, object]:
        """Save a final authoring candidate without creating its first paper revision."""
        CanonicalHTMLValidator().assert_valid(proposed_html)
        if quality_status not in {"ready", "needs_human_review"}:
            raise ValueError("invalid authoring proposal quality status")
        content_hash = hashlib.sha256(proposed_html.encode("utf-8")).hexdigest()
        review_context_json = json.dumps(review_context or {}, ensure_ascii=False, sort_keys=True)
        created_at = _now()
        with closing(self.database.connect()) as connection, connection:
            source = connection.execute(
                """
                SELECT d.project_id, d.kind, d.current_revision_id
                FROM content_documents d
                WHERE d.id = ?
                """,
                (source_document_id,),
            ).fetchone()
            source_revision = connection.execute(
                "SELECT document_id FROM revisions WHERE id = ?",
                (source_revision_id,),
            ).fetchone()
            target = connection.execute(
                """
                SELECT project_id, kind, current_revision_id
                FROM content_documents WHERE id = ?
                """,
                (target_document_id,),
            ).fetchone()
            run = connection.execute(
                "SELECT project_id, workflow_name FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            plan = connection.execute(
                """
                SELECT p.document_id, p.source_revision_id
                FROM authoring_plans p
                JOIN authoring_plan_revisions r ON r.plan_id = p.id
                WHERE p.id = ? AND r.revision = ?
                """,
                (plan_id, plan_revision),
            ).fetchone()
            if source is None or source_revision is None:
                raise ValueError("source note or revision does not exist")
            if source["kind"] != "note" or source_revision["document_id"] != source_document_id:
                raise ValueError("proposal source must be a revision of the source note")
            if source["project_id"] != project_id:
                raise ValueError("source note belongs to a different project")
            if target is None or target["project_id"] != project_id or target["kind"] != "paper":
                raise ValueError("target document must be a paper in the same project")
            if target["current_revision_id"] is not None:
                raise ValueError("target paper must not have a revision before confirmation")
            if run is None or not is_manuscript_generator_workflow(run["workflow_name"]):
                raise ValueError("pending proposal must belong to an authoring run")
            if run["project_id"] != project_id:
                raise ValueError("authoring run belongs to a different project")
            if plan is None or plan["document_id"] != target_document_id:
                raise ValueError("proposal plan does not belong to the target paper")
            if plan["source_revision_id"] != source_revision_id:
                raise ValueError("proposal plan is for a different source revision")
            existing = connection.execute(
                "SELECT * FROM authoring_pending_proposals WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if existing is not None:
                if not replace_existing_pending:
                    raise ValueError("authoring run already has a pending proposal")
                immutable = {
                    "id": proposal_id,
                    "project_id": project_id,
                    "source_document_id": source_document_id,
                    "source_revision_id": source_revision_id,
                    "target_document_id": target_document_id,
                    "plan_id": plan_id,
                    "plan_revision": plan_revision,
                }
                if existing["status"] != "pending" or any(
                    existing[key] != value for key, value in immutable.items()
                ):
                    raise ValueError(
                        "existing authoring proposal cannot be replaced by revalidation"
                    )
                ordinal = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(revalidation_ordinal),0)+1 "
                        "FROM authoring_proposal_revalidations WHERE proposal_id=?",
                        (proposal_id,),
                    ).fetchone()[0]
                )
                connection.execute(
                    "INSERT INTO authoring_proposal_revalidations("
                    "id,proposal_id,run_id,revalidation_ordinal,title,proposed_html,"
                    "proposed_sha256,quality_status,review_context_json,artifact_path,"
                    "archived_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        f"{proposal_id}:revalidation:{ordinal}",
                        proposal_id,
                        run_id,
                        ordinal,
                        existing["title"],
                        existing["proposed_html"],
                        existing["proposed_sha256"],
                        existing["quality_status"],
                        existing["review_context_json"],
                        existing["artifact_path"],
                        created_at,
                    ),
                )
                connection.execute(
                    "DELETE FROM authoring_pending_proposals WHERE id=? AND status='pending'",
                    (proposal_id,),
                )
            connection.execute(
                """
                INSERT INTO authoring_pending_proposals(
                    id, run_id, project_id, source_document_id, source_revision_id,
                    target_document_id, plan_id, plan_revision, title, proposed_html,
                    proposed_sha256, status, quality_status, review_context_json,
                    artifact_path, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                """,
                (
                    proposal_id,
                    run_id,
                    project_id,
                    source_document_id,
                    source_revision_id,
                    target_document_id,
                    plan_id,
                    plan_revision,
                    title,
                    proposed_html,
                    content_hash,
                    quality_status,
                    review_context_json,
                    artifact_path,
                    created_at,
                ),
            )
        return self.get_pending_proposal(proposal_id) or {}

    def get_pending_proposal(self, proposal_id: str) -> dict[str, object] | None:
        with closing(self.database.connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT id, run_id, project_id, source_document_id, source_revision_id,
                       target_document_id, plan_id, plan_revision, title, proposed_html,
                       proposed_sha256, status, quality_status, review_context_json,
                       confirmed_revision_id, artifact_path, created_at, resolved_at
                FROM authoring_pending_proposals WHERE id = ?
                """,
                (proposal_id,),
            ).fetchone()
            if row is None:
                return None
            proposal = dict(row)
            proposal["review_context"] = json.loads(
                str(proposal.pop("review_context_json") or "{}")
            )
            return proposal

    @staticmethod
    def mark_pending_proposal_confirmed(
        connection: sqlite3.Connection,
        *,
        proposal_id: str,
        revision_id: str,
        resolved_at: str,
    ) -> None:
        cursor = connection.execute(
            """
            UPDATE authoring_pending_proposals
            SET status = 'confirmed', confirmed_revision_id = ?, resolved_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (revision_id, resolved_at, proposal_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("proposal is no longer pending")

    @staticmethod
    def _insert_children(
        connection: sqlite3.Connection,
        plan: AuthoringPlan,
        created_at: str,
        producer_node_run_id: str | None,
    ) -> None:
        key = (plan.id, plan.revision)
        for item in plan.propositions:
            connection.execute(
                "INSERT INTO atomic_propositions(id, plan_id, plan_revision, text, proposition_type, source_block_id, source_start, source_end, source_quote, confidence, provenance, argument_role, producer_node_run_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.id,
                    *key,
                    item.text,
                    item.proposition_type,
                    item.source_block_id,
                    item.source_start,
                    item.source_end,
                    item.source_quote,
                    item.confidence,
                    item.provenance,
                    item.argument_role,
                    producer_node_run_id,
                    created_at,
                ),
            )
        for item in _parents_first(plan.claims):
            connection.execute(
                "INSERT INTO claim_tree_nodes(id, plan_id, plan_revision, parent_id, claim_level, text, proposition_id, rationale, provenance, research_gap, rival_interpretation, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.id,
                    *key,
                    item.parent_id,
                    item.level,
                    item.text,
                    item.proposition_id,
                    item.rationale,
                    item.provenance,
                    item.research_gap,
                    item.rival_interpretation,
                    created_at,
                ),
            )
        for item in plan.argument_nodes:
            connection.execute(
                "INSERT INTO argument_nodes(id, plan_id, plan_revision, node_type, text, claim_tree_node_id, proposition_id, source_span_id, source_uri, verification_status, provenance, confidence, details_json, producer_node_run_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.id,
                    *key,
                    item.node_type,
                    item.text,
                    item.claim_tree_node_id,
                    item.proposition_id,
                    item.source_span_id,
                    item.source_uri,
                    item.verification_status,
                    item.provenance,
                    item.confidence,
                    _json(item.details),
                    producer_node_run_id,
                    created_at,
                ),
            )
        for item in plan.argument_edges:
            connection.execute(
                "INSERT INTO argument_edges(id, plan_id, plan_revision, source_node_id, target_node_id, relation_type, rationale, provenance, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.id,
                    *key,
                    item.source_node_id,
                    item.target_node_id,
                    item.relation_type,
                    item.rationale,
                    item.provenance,
                    created_at,
                ),
            )
        for item in plan.hypotheses:
            connection.execute(
                "INSERT INTO hypothesis_candidates(id, plan_id, plan_revision, text, gap_type, status, rationale, provenance, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.id,
                    *key,
                    item.text,
                    item.gap_type,
                    item.status,
                    item.rationale,
                    item.provenance,
                    created_at,
                ),
            )
        for item in _parents_first(plan.discourse_nodes):
            connection.execute(
                "INSERT INTO discourse_nodes(id, plan_id, plan_revision, parent_id, ordinal, node_kind, article_role, rhetorical_relation, attentional_focus, discourse_purpose, claim_ids_json, section_id, section_answer, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.id,
                    *key,
                    item.parent_id,
                    item.ordinal,
                    item.node_kind,
                    item.article_role,
                    item.rhetorical_relation,
                    item.attentional_focus,
                    item.discourse_purpose,
                    _json(item.claim_ids),
                    item.section_id,
                    item.section_answer,
                    created_at,
                ),
            )
        for item in plan.section_contracts:
            connection.execute(
                "INSERT INTO section_contracts(id, plan_id, plan_revision, discourse_node_id, title, local_question, article_role, expected_conclusion, claim_ids_json, evidence_ids_json, warrant_ids_json, qualifiers_json, counterarguments_json, previous_bridge, next_bridge, required_terms_json, forbidden_topics_json, minimum_evidence_items, minimum_concrete_examples, required_argument_moves_json, target_length, section_answer, section_output_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.id,
                    *key,
                    item.discourse_node_id,
                    item.title,
                    item.local_question,
                    item.article_role,
                    item.expected_conclusion,
                    _json(item.claim_ids),
                    _json(item.evidence_ids),
                    _json(item.warrant_ids),
                    _json(item.qualifiers),
                    _json(item.counterarguments),
                    item.previous_bridge,
                    item.next_bridge,
                    _json(item.required_terms),
                    _json(item.forbidden_topics),
                    item.minimum_evidence_items,
                    item.minimum_concrete_examples,
                    _json(item.required_argument_moves),
                    item.target_length,
                    item.section_answer,
                    _json(item.section_output_contract.model_dump(mode="json"))
                    if item.section_output_contract is not None
                    else "{}",
                    created_at,
                ),
            )

    @staticmethod
    def _contract_from_row(row: sqlite3.Row) -> SectionContract:
        return SectionContract(
            id=row["id"],
            discourse_node_id=row["discourse_node_id"],
            title=row["title"],
            local_question=row["local_question"],
            article_role=row["article_role"],
            expected_conclusion=row["expected_conclusion"],
            section_answer=row["section_answer"],
            section_output_contract=(
                json.loads(row["section_output_json"])
                if row["section_output_json"] != "{}"
                else None
            ),
            claim_ids=tuple(json.loads(row["claim_ids_json"])),
            evidence_ids=tuple(json.loads(row["evidence_ids_json"])),
            warrant_ids=tuple(json.loads(row["warrant_ids_json"])),
            qualifiers=tuple(json.loads(row["qualifiers_json"])),
            counterarguments=tuple(json.loads(row["counterarguments_json"])),
            previous_bridge=row["previous_bridge"],
            next_bridge=row["next_bridge"],
            required_terms=tuple(json.loads(row["required_terms_json"])),
            forbidden_topics=tuple(json.loads(row["forbidden_topics_json"])),
            minimum_evidence_items=row["minimum_evidence_items"],
            minimum_concrete_examples=row["minimum_concrete_examples"],
            required_argument_moves=tuple(json.loads(row["required_argument_moves_json"])),
            target_length=row["target_length"],
        )


def _parents_first(items):
    """Return tree nodes in FK-safe order without depending on model input ordering."""
    pending = list(items)
    emitted: set[str] = set()
    ordered = []
    while pending:
        ready = [item for item in pending if item.parent_id is None or item.parent_id in emitted]
        if not ready:
            raise ValueError("tree contains a cycle or missing parent")
        for item in ready:
            ordered.append(item)
            emitted.add(item.id)
            pending.remove(item)
    return ordered
