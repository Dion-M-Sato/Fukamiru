"""Content-aware manuscript rewrite proposals over immutable human revisions."""

from __future__ import annotations

import difflib
import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from fukamiru.documents.canonical_html import CanonicalHTMLValidator
from fukamiru.documents.japanese_surface import (
    JapaneseSurfaceGateResult,
    JapaneseSurfaceQualityGate,
)
from fukamiru.errors import FukamiruError, NonRetryableFukamiruError
from fukamiru.projects.git_checkpoint import GitCheckpointError, GitCheckpointService
from fukamiru.revisions.coherence import ManuscriptCoherenceService
from fukamiru.revisions.discourse import ManuscriptDiscourseService
from fukamiru.revisions.references import JapaneseReferenceService
from fukamiru.revisions.service import RevisionService, utc_now
from fukamiru.revisions.structure_gate import heading_edit_issue, heading_issues

_EDITABLE_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "blockquote", "li"}
_DELETABLE_TAGS = {"p", "blockquote", "li"}
_INLINE_PROTECTED_TAGS = {"a", "cite", "q", "sup"}
_V22_SEMANTIC_REVIEW_DECISIONS = frozenset(
    {
        "confirm_current_interpretation",
        "clarify_interpretation",
        "not_applicable",
    }
)


class ManuscriptRewriteService:
    """Prepare context, apply bounded edits and confirm a human-approved proposal."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.validator = CanonicalHTMLValidator()

    def prepare(
        self,
        *,
        document_id: str,
        base_revision_id: str | None,
        outline_node_id: str | None,
        source_proposal_id: str | None = None,
    ) -> dict[str, Any]:
        document = self.connection.execute(
            """
            SELECT d.id, d.project_id, d.title, d.kind, d.current_revision_id,
                   p.root_path
            FROM content_documents d JOIN projects p ON p.id = d.project_id
            WHERE d.id = ?
            """,
            (document_id,),
        ).fetchone()
        if document is None:
            raise FukamiruError(f"Unknown document: {document_id}")
        current_revision_id = str(document["current_revision_id"] or "")
        if not current_revision_id:
            raise FukamiruError(
                "The document has no base Revision; confirm a manuscript Import Preview first"
            )
        requested_revision = base_revision_id or current_revision_id
        if requested_revision != current_revision_id:
            raise FukamiruError(
                "Rewrite must start from the current Revision; reload the document first"
            )
        source_lineage: list[dict[str, Any]] = []
        source_machine_reference_entries: list[dict[str, Any]] = []
        if source_proposal_id:
            proposal = self.connection.execute(
                """
                SELECT document_id, base_revision_id, proposed_html, status, context_json
                FROM manuscript_rewrite_proposals WHERE id = ?
                """,
                (source_proposal_id,),
            ).fetchone()
            if proposal is None:
                raise FukamiruError(f"Source rewrite proposal not found: {source_proposal_id}")
            if str(proposal["document_id"]) != document_id:
                raise FukamiruError("Source proposal does not belong to this document")
            if str(proposal["base_revision_id"]) != requested_revision:
                raise FukamiruError(
                    "Source proposal is stale because its base Revision is no longer current"
                )
            if str(proposal["status"]) not in {"pending", "rejected"}:
                raise FukamiruError(
                    "Only a pending or rejected proposal can be used as a rewrite source"
            )
            base_html = str(proposal["proposed_html"])
            try:
                source_context = json.loads(str(proposal["context_json"] or "{}"))
            except json.JSONDecodeError:
                source_context = {}
            if isinstance(source_context, dict):
                source_machine_reference_entries = [
                    dict(item)
                    for item in source_context.get("machine_reference_entries") or []
                    if isinstance(item, dict)
                ]
            source_lineage = self._source_proposal_lineage(
                source_proposal_id=source_proposal_id,
                document_id=document_id,
                base_revision_id=requested_revision,
            )
        else:
            revision = self.connection.execute(
                """
                SELECT rd.canonical_html
                FROM revisions r JOIN revision_documents rd ON rd.revision_id = r.id
                WHERE r.id = ? AND r.document_id = ?
                """,
                (requested_revision, document_id),
            ).fetchone()
            if revision is None:
                raise FukamiruError("Base Revision does not belong to this document")
            base_html = str(revision["canonical_html"])
        outline = self._outline_context(
            document_id=document_id,
            project_id=str(document["project_id"]),
            document_kind=str(document["kind"]),
            outline_node_id=outline_node_id,
        )
        self.validator.assert_valid(base_html)
        blocks = self.editable_blocks(base_html)
        if not blocks:
            raise FukamiruError("The base Revision contains no editable prose blocks")
        return {
            "document_id": document_id,
            "project_id": str(document["project_id"]),
            "project_root": str(document["root_path"]),
            "document_title": str(document["title"]),
            "document_kind": str(document["kind"]),
            "base_revision_id": requested_revision,
            "source_proposal_id": source_proposal_id,
            "source_lineage": source_lineage,
            "source_machine_reference_entries": source_machine_reference_entries,
            "base_html": base_html,
            "outline_node_id": outline["target_outline_node_id"],
            "outline_context": outline,
            "editable_blocks": blocks,
            "document_map": [
                {
                    "id": block["id"],
                    "tag": block["tag"],
                    "text": str(block["text"])[:180],
                    "inline_protected": block["inline_protected"],
                }
                for index, block in enumerate(blocks)
                if str(block["tag"]).startswith("h") or index % 10 == 0
            ][:200],
        }

    def _source_proposal_lineage(
        self,
        *,
        source_proposal_id: str,
        document_id: str,
        base_revision_id: str,
        max_depth: int = 16,
    ) -> list[dict[str, Any]]:
        """Return trusted workflow metadata for the supplied proposal ancestry.

        Proposal content is already the rewrite source.  This small lineage packet
        lets the reasoning model distinguish an ancestor that is present through
        that content from a Run ID that was never supplied.
        """

        lineage: list[dict[str, Any]] = []
        proposal_id: str | None = source_proposal_id
        visited: set[str] = set()
        while proposal_id and proposal_id not in visited and len(lineage) < max_depth:
            visited.add(proposal_id)
            row = self.connection.execute(
                """
                SELECT p.id, p.run_id, p.document_id, p.base_revision_id,
                       p.status AS proposal_status, p.quality_status, p.context_json,
                       r.workflow_version, r.status AS run_status, r.input_json
                FROM manuscript_rewrite_proposals p
                JOIN runs r ON r.id = p.run_id
                WHERE p.id = ?
                """,
                (proposal_id,),
            ).fetchone()
            if row is None:
                break
            if (
                str(row["document_id"]) != document_id
                or str(row["base_revision_id"]) != base_revision_id
            ):
                break
            try:
                proposal_context = json.loads(str(row["context_json"] or "{}"))
            except json.JSONDecodeError:
                proposal_context = {}
            if not isinstance(proposal_context, dict):
                proposal_context = {}
            raw_paragraph_plan = proposal_context.get("paragraph_plan")
            paragraph_plan = (
                dict(raw_paragraph_plan) if isinstance(raw_paragraph_plan, dict) else {}
            )
            raw_paragraph_contract = paragraph_plan.get("boundary_contract")
            paragraph_contract = (
                dict(raw_paragraph_contract) if isinstance(raw_paragraph_contract, dict) else {}
            )
            lineage.append(
                {
                    "proposal_id": str(row["id"]),
                    "run_id": str(row["run_id"]),
                    "workflow_version": str(row["workflow_version"]),
                    "run_status": str(row["run_status"]),
                    "proposal_status": str(row["proposal_status"]),
                    "quality_status": str(row["quality_status"]),
                    "paragraph_boundary_vector_sha256": str(
                        paragraph_contract.get("boundary_vector_sha256") or ""
                    ),
                    "paragraph_partition_signature_sha256": str(
                        paragraph_contract.get("partition_signature_sha256") or ""
                    ),
                }
            )
            try:
                run_input = json.loads(str(row["input_json"] or "{}"))
            except json.JSONDecodeError:
                break
            if not isinstance(run_input, dict):
                break
            parent = str(run_input.get("source_proposal_id") or "").strip()
            proposal_id = parent or None
        return lineage

    @staticmethod
    def editable_blocks(canonical_html: str) -> list[dict[str, Any]]:
        root = ET.fromstring(canonical_html)
        result: list[dict[str, Any]] = []
        for element in root.iter():
            if element.tag not in _EDITABLE_TAGS:
                continue
            text = "".join(element.itertext()).strip()
            if not text:
                continue
            result.append(
                {
                    "id": str(element.attrib["id"]),
                    "tag": element.tag,
                    "text": text,
                    "inline_protected": any(
                        child.tag in _INLINE_PROTECTED_TAGS for child in element.iter()
                    ),
                }
            )
        return result

    @staticmethod
    def batch_blocks(
        blocks: Iterable[dict[str, Any]],
        *,
        max_characters: int = 12000,
        max_blocks: int = 40,
    ) -> list[list[dict[str, Any]]]:
        batches: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        current_characters = 0
        for block in blocks:
            size = len(str(block.get("text") or ""))
            if current and (
                current_characters + size > max_characters or len(current) >= max_blocks
            ):
                batches.append(current)
                current = []
                current_characters = 0
            current.append(block)
            current_characters += size
        if current:
            batches.append(current)
        return batches

    def apply_edits(
        self,
        canonical_html: str,
        edits: Iterable[dict[str, Any]],
        *,
        run_id: str,
        batch_index: int,
    ) -> tuple[str, list[dict[str, Any]], list[str]]:
        root = ET.fromstring(canonical_html)
        applied: list[dict[str, Any]] = []
        warnings: list[str] = []
        seen: set[tuple[str, str]] = set()
        for edit_index, raw_edit in enumerate(edits):
            operation = str(raw_edit.get("operation") or "")
            block_id = str(raw_edit.get("block_id") or "")
            text = str(raw_edit.get("text") or "").strip()
            key = (operation, block_id)
            if key in seen:
                warnings.append(f"Duplicate rewrite edit was skipped: {operation} {block_id}")
                continue
            seen.add(key)
            elements = {
                str(item.attrib.get("id")): item for item in root.iter() if item.attrib.get("id")
            }
            target = elements.get(block_id)
            if target is None or target.tag not in _EDITABLE_TAGS:
                warnings.append(f"Rewrite edit target was unavailable: {block_id}")
                continue
            inline_protected = any(child.tag in _INLINE_PROTECTED_TAGS for child in target.iter())
            original_text = "".join(target.itertext()).strip()
            output_block_id = block_id
            parents = {child: parent for parent in root.iter() for child in parent}
            parent = parents.get(target)
            if operation == "replace_text":
                heading_issue = heading_edit_issue(original_text, text, target.tag)
                if heading_issue:
                    warnings.append(f"Structure gate skipped edit: {block_id} ({heading_issue})")
                    continue
                if inline_protected:
                    warnings.append(
                        f"Protected citation/quote markup prevented replacement: {block_id}"
                    )
                    continue
                if not text:
                    warnings.append(f"Empty replacement was skipped: {block_id}")
                    continue
                if text == original_text:
                    warnings.append(f"Japanese surface quality gate skipped no-op edit: {block_id}")
                    continue
                surface_issues = JapaneseSurfaceQualityGate.introduced(original_text, text)
                if surface_issues:
                    warnings.append(self._surface_warning(block_id, surface_issues))
                    continue
                for child in list(target):
                    target.remove(child)
                target.text = text
            elif operation == "merge_with_next":
                if (
                    inline_protected
                    or target.tag not in _DELETABLE_TAGS
                    or ManuscriptCoherenceService.heading(original_text, target.tag) is not None
                    or parent is None
                    or not text
                ):
                    warnings.append(f"Unsafe paragraph merge was skipped: {block_id}")
                    continue
                siblings = list(parent)
                position = siblings.index(target)
                next_target = siblings[position + 1] if position + 1 < len(siblings) else None
                if (
                    next_target is None
                    or next_target.tag != target.tag
                    or next_target.tag not in _DELETABLE_TAGS
                    or ManuscriptCoherenceService.heading(
                        "".join(next_target.itertext()).strip(), next_target.tag
                    )
                    is not None
                    or any(child.tag in _INLINE_PROTECTED_TAGS for child in next_target.iter())
                ):
                    warnings.append(f"Adjacent compatible paragraph was unavailable: {block_id}")
                    continue
                merged_source = original_text + "\n\n" + "".join(next_target.itertext()).strip()
                surface_issues = JapaneseSurfaceQualityGate.introduced(merged_source, text)
                if surface_issues:
                    warnings.append(self._surface_warning(block_id, surface_issues))
                    continue
                for child in list(target):
                    target.remove(child)
                target.text = text
                parent.remove(next_target)
            elif operation in {"move_before", "move_after"}:
                anchor_id = str(raw_edit.get("anchor_block_id") or "")
                anchor = elements.get(anchor_id)
                anchor_parent = parents.get(anchor) if anchor is not None else None
                structure = ManuscriptCoherenceService.semantic_structure(
                    ET.tostring(root, encoding="unicode", short_empty_elements=False)
                )
                section_by_block = {
                    str(block["id"]): str(block["section_id"]) for block in structure["blocks"]
                }
                if (
                    inline_protected
                    or parent is None
                    or anchor is None
                    or anchor is target
                    or anchor_parent is not parent
                    or ManuscriptCoherenceService.heading(original_text, target.tag) is not None
                    or section_by_block.get(block_id) != section_by_block.get(anchor_id)
                ):
                    warnings.append(f"Unsafe coherence move was skipped: {block_id}")
                    continue
                parent.remove(target)
                anchor_position = list(parent).index(anchor)
                if operation == "move_after":
                    anchor_position += 1
                parent.insert(anchor_position, target)
            elif operation == "delete_block":
                if ManuscriptCoherenceService.heading(original_text, target.tag):
                    warnings.append(f"Structure gate protected heading: {block_id}")
                    continue
                if inline_protected or target.tag not in _DELETABLE_TAGS or parent is None:
                    warnings.append(f"Protected structural block was not deleted: {block_id}")
                    continue
                parent.remove(target)
            elif operation == "insert_paragraph_after":
                if not text or parent is None or parent.tag != "section":
                    warnings.append(f"Paragraph insertion target was unsafe: {block_id}")
                    continue
                surface_issues = JapaneseSurfaceQualityGate.find(text)
                if surface_issues:
                    warnings.append(self._surface_warning(block_id, surface_issues))
                    continue
                insertion_id = (
                    "P-REWRITE-"
                    + uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"fukamiru:{run_id}:{batch_index}:{edit_index}:{block_id}",
                    ).hex.upper()
                )
                inserted = ET.Element(
                    "p",
                    {
                        "id": insertion_id,
                        "data-rewrite-run": run_id,
                    },
                )
                inserted.text = text
                position = list(parent).index(target) + 1
                parent.insert(position, inserted)
                output_block_id = insertion_id
            else:
                warnings.append(f"Unsupported rewrite operation was skipped: {operation}")
                continue
            applied.append(
                {
                    "operation": operation,
                    "block_id": block_id,
                    "output_block_id": output_block_id,
                    "text": text,
                    "anchor_block_id": raw_edit.get("anchor_block_id"),
                    "review_finding_ids": [
                        str(item)
                        for item in raw_edit.get("review_finding_ids", [])
                        if str(item).strip()
                    ],
                    "evidence_ids": [
                        str(item) for item in raw_edit.get("evidence_ids", []) if str(item).strip()
                    ],
                }
            )
        proposed_html = ET.tostring(root, encoding="unicode", short_empty_elements=False)
        self.validator.assert_valid(proposed_html)
        return proposed_html, applied, warnings

    @staticmethod
    def _surface_warning(block_id: str, issues: tuple[Any, ...]) -> str:
        codes = ", ".join(dict.fromkeys(str(issue.code) for issue in issues))
        return f"Japanese surface quality gate restored the source block: {block_id} ({codes})"

    @classmethod
    def _persistence_quality_context(
        cls,
        context: dict[str, Any],
        *,
        quality_status: str,
        surface_gate: JapaneseSurfaceGateResult,
    ) -> tuple[dict[str, Any], str]:
        """Validate ready provenance and fail closed on persistence-time restores."""

        if quality_status not in {"ready", "needs_human_review"}:
            raise FukamiruError("Unknown manuscript proposal quality status")
        stored_context = dict(context)
        bulk_review = cls._workflow_version_from_context(stored_context) >= 24
        completion_gate = dict(stored_context.get("completion_gate") or {})
        reference_blockers = cls._v22_reference_context_blockers(stored_context)
        if reference_blockers:
            quality_status = "needs_human_review"
            blocking_reasons = list(completion_gate.get("blocking_reasons") or [])
            blocker = {
                "code": (
                    "reference.verification_pending"
                    if bulk_review else "reference.final_revision_confirmation_required"
                ),
                "message": (
                    "Bibliography verification remains unresolved after bulk text approval."
                    if bulk_review else
                    "V22 bibliography metadata is sufficient for a conditional Proposal "
                    "but not for final Revision approval."
                ),
                "details": reference_blockers,
            }
            if not any(
                isinstance(item, dict) and item.get("code") == blocker["code"]
                for item in blocking_reasons
            ):
                blocking_reasons.append(blocker)
            completion_gate.update(
                {
                    "passed": False,
                    "quality_status": "needs_human_review",
                    "blocking_count": len(blocking_reasons),
                    "blocking_reasons": blocking_reasons,
                }
            )
            stored_context["completion_gate"] = completion_gate
            stored_context["reference_approval"] = {
                "status": "verification_pending" if bulk_review else "human_confirmation_required",
                "final_revision_blocked": not bulk_review,
                "blocking_reasons": reference_blockers,
            }
        if quality_status == "ready" and (
            completion_gate.get("passed") is not True
            or completion_gate.get("quality_status") != "ready"
            or int(completion_gate.get("blocking_count") or 0) != 0
            or completion_gate.get("blocking_reasons")
        ):
            raise FukamiruError(
                "A ready manuscript proposal requires a clean completion-gate record"
            )
        if surface_gate.reverted_blocks:
            quality_status = "needs_human_review"
            blocking_reasons = list(completion_gate.get("blocking_reasons") or [])
            blocker = {
                "code": "surface.persistence_reverted_blocks",
                "message": (
                    "The persistence boundary restored surface-invalid blocks; "
                    "the proposal requires human review."
                ),
                "details": surface_gate.as_dict(),
            }
            if not any(
                isinstance(item, dict) and item.get("code") == blocker["code"]
                for item in blocking_reasons
            ):
                blocking_reasons.append(blocker)
            completion_gate.update(
                {
                    "passed": False,
                    "quality_status": "needs_human_review",
                    "blocking_count": len(blocking_reasons),
                    "blocking_reasons": blocking_reasons,
                }
            )
            stored_context["completion_gate"] = completion_gate
        stored_context["japanese_surface_quality_gate"] = surface_gate.as_dict()
        return stored_context, quality_status

    @staticmethod
    def _workflow_version_from_context(context: dict[str, Any]) -> int:
        """Resolve an opt-in workflow version without consulting mutable Run state."""

        for value in (
            context.get("workflow_version"),
            (context.get("workflow") or {}).get("version")
            if isinstance(context.get("workflow"), dict)
            else None,
        ):
            try:
                return int(str(value))
            except (TypeError, ValueError):
                continue
        policy = context.get("manuscript_workflow_policy")
        if isinstance(policy, dict):
            try:
                return int(str(policy.get("workflow_version")))
            except (TypeError, ValueError):
                policy_id = str(policy.get("id") or "").lower()
                if policy_id == "manuscript-quality-policy" or "workflow-v22" in policy_id:
                    return 22
        return 0

    @staticmethod
    def _section_structure_signature(canonical_html: str) -> list[dict[str, Any]]:
        """Lock section identity and block membership without freezing wording."""

        manifest = ManuscriptCoherenceService.document_manifest(canonical_html)
        semantic_sections = [
            {
                "kind": "semantic",
                "section_id": item["section_id"],
                "level": item["level"],
                "document_role": item["document_role"],
                "block_ids": item["block_ids"],
            }
            for item in manifest["structural_signature"]
        ]
        root = ET.fromstring(canonical_html)
        parents = {child: parent for parent in root.iter() for child in parent}
        dom_sections = []
        for section in root.iter("section"):
            parent = parents.get(section)
            while parent is not None and parent.tag != "section":
                parent = parents.get(parent)
            dom_sections.append(
                {
                    "kind": "dom",
                    "section_id": section.get("id"),
                    "parent_section_id": parent.get("id") if parent is not None else None,
                    "block_ids": [
                        item.get("id")
                        for item in section.iter()
                        if item.tag in _EDITABLE_TAGS
                    ],
                }
            )
        return semantic_sections + dom_sections

    @staticmethod
    def _semantic_review_digest_v22(*parts: object) -> str:
        payload = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()[:24]

    @classmethod
    def _language_semantic_review_id_v22(cls, requirement: dict[str, Any]) -> str:
        """Return a stable UI/API identity without mutating the stored requirement."""

        source_id = str(
            requirement.get("requirement_id")
            or requirement.get("target_id")
            or cls._semantic_review_digest_v22(
                "language-requirement",
                requirement.get("origin"),
                requirement.get("phase"),
                requirement.get("block_id"),
                requirement.get("kind"),
                requirement.get("term"),
                requirement.get("cause"),
                requirement.get("effect"),
                requirement.get("source_sha256"),
            )
        )
        return "semantic-review-v22-" + cls._semantic_review_digest_v22(
            "language_requirement", source_id
        )

    @classmethod
    def _semantic_condition_key_v22(cls, condition: dict[str, Any]) -> str:
        explicit = str(
            condition.get("semantic_condition_id")
            or condition.get("condition_id")
            or ""
        ).strip()
        if explicit:
            return explicit
        return cls._semantic_review_digest_v22(
            "semantic-condition",
            condition.get("phase"),
            condition.get("category"),
            [str(value) for value in condition.get("unit_ids") or []],
            [str(value) for value in condition.get("block_ids") or []],
        )

    @classmethod
    def _condition_semantic_review_id_v22(
        cls, condition: dict[str, Any], unit_id: str
    ) -> str:
        return "semantic-review-v22-" + cls._semantic_review_digest_v22(
            "conditional_unit",
            cls._semantic_condition_key_v22(condition),
            unit_id,
        )

    @classmethod
    def _semantic_review_items_v22(
        cls, context: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Expose only unresolved items that require an explicit human judgement.

        A grouped semantic condition is deliberately expanded to one review item
        per unit.  Resolving one unit therefore cannot accidentally resolve its
        siblings, and the stable IDs do not depend on list order or mutable status.
        """

        if cls._workflow_version_from_context(context) < 22:
            return []
        items: list[dict[str, Any]] = []
        for requirement in context.get("language_review_requirements") or []:
            if not isinstance(requirement, dict):
                continue
            if requirement.get("review_class") != "human_semantic_judgement_required":
                continue
            if requirement.get("status") in {"resolved", "no_revision_needed"}:
                continue
            review_id = cls._language_semantic_review_id_v22(requirement)
            items.append(
                {
                    "review_id": review_id,
                    "source_type": "language_review_requirement",
                    "source_item_id": str(
                        requirement.get("requirement_id")
                        or requirement.get("target_id")
                        or review_id
                    ),
                    "review_class": "human_semantic_judgement_required",
                    "phase": str(requirement.get("phase") or "language_review"),
                    "kind": str(requirement.get("kind") or "semantic_review"),
                    "block_ids": [
                        str(requirement["block_id"])
                    ] if str(requirement.get("block_id") or "").strip() else [],
                    "term": str(requirement.get("term") or ""),
                    "cause": str(requirement.get("cause") or ""),
                    "effect": str(requirement.get("effect") or ""),
                    "explanation": str(requirement.get("explanation") or ""),
                    "status": "pending_human_judgement",
                }
            )

        for condition in context.get("conditional_conditions") or []:
            if not isinstance(condition, dict):
                continue
            if condition.get("reason") != "semantic_resolution_required":
                continue
            group_unit_ids = list(
                dict.fromkeys(
                    str(value).strip()
                    for value in condition.get("unit_ids") or []
                    if str(value).strip()
                )
            )
            condition_key = cls._semantic_condition_key_v22(condition)
            if not group_unit_ids:
                group_unit_ids = ["semantic-condition-unit:" + condition_key]
            resolved_unit_ids = {
                str(value).strip()
                for value in condition.get("resolved_unit_ids") or []
                if str(value).strip()
            }
            for unit_id in group_unit_ids:
                if unit_id in resolved_unit_ids:
                    continue
                review_id = cls._condition_semantic_review_id_v22(condition, unit_id)
                items.append(
                    {
                        "review_id": review_id,
                        "source_type": "semantic_condition_unit",
                        "source_item_id": condition_key,
                        "unit_id": unit_id,
                        "group_unit_ids": group_unit_ids,
                        "resolved_unit_ids": sorted(resolved_unit_ids),
                        "remaining_unit_count": len(
                            [value for value in group_unit_ids if value not in resolved_unit_ids]
                        ),
                        "review_class": "human_semantic_judgement_required",
                        "phase": str(condition.get("phase") or "reasoning"),
                        "kind": str(condition.get("category") or "claim_causality"),
                        "block_ids": [
                            str(value)
                            for value in condition.get("block_ids") or []
                            if str(value).strip()
                        ],
                        "explanation": str(
                            condition.get("explanation")
                            or condition.get("message")
                            or "主張・根拠関係の意味を人間が判断する必要があります。"
                        ),
                        "status": "pending_human_judgement",
                    }
                )
        return items

    @staticmethod
    def _language_requirement_final_blocked_v22(requirement: dict[str, Any]) -> bool:
        if requirement.get("status") in {"resolved", "no_revision_needed"}:
            return False
        return str(requirement.get("review_class") or "") not in {
            "minor_lexical_source_preserved",
            "polish_candidate_source_preserved",
        }

    @classmethod
    def _reference_render(
        cls,
        canonical_html: str,
        entries: list[dict[str, Any]],
        *,
        workflow_version: int,
        machine_entries: list[dict[str, Any]] | None = None,
    ) -> tuple[str, list[dict[str, Any]]]:
        if workflow_version >= 22:
            return JapaneseReferenceService.render_sections_v22(
                canonical_html,
                entries,
                machine_entries=machine_entries or [],
                render_machine_section=workflow_version < 26,
                workflow_version=workflow_version,
            )
        return JapaneseReferenceService.render(canonical_html, entries)

    @classmethod
    def _reference_format_issues(
        cls,
        canonical_html: str,
        entries: list[dict[str, Any]],
        *,
        workflow_version: int,
        machine_entries: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        if workflow_version >= 22:
            return JapaneseReferenceService.format_issues(
                canonical_html,
                entries,
                workflow_version=workflow_version,
                machine_entries=machine_entries or [],
            )
        return JapaneseReferenceService.format_issues(canonical_html, entries)

    @classmethod
    def _reference_quality_report(
        cls,
        canonical_html: str,
        entries: list[dict[str, Any]],
        *,
        workflow_version: int,
        machine_entries: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if workflow_version >= 22:
            return JapaneseReferenceService.quality_report(
                canonical_html,
                entries,
                workflow_version=workflow_version,
                machine_entries=machine_entries or [],
            )
        return JapaneseReferenceService.quality_report(canonical_html, entries)

    @staticmethod
    def _reference_records(context: dict[str, Any]) -> list[dict[str, Any]]:
        """Collect top-level approval records without treating candidates as sources."""

        pending: list[Any] = list(context.get("reference_entries") or [])
        pending.extend(context.get("machine_reference_entries") or [])
        inventory = context.get("reference_inventory")
        if isinstance(inventory, dict):
            pending.extend(inventory.get("entries") or [])
        records: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for item in pending:
            if not isinstance(item, dict):
                continue
            identity = (
                str(item.get("reference_id") or item.get("machine_reference_id") or ""),
                str(item.get("source_block_id") or ""),
                str(item.get("candidate_id") or item.get("provider_record_id") or ""),
            )
            if identity not in seen:
                seen.add(identity)
                records.append(item)
        return records

    @classmethod
    def _v22_reference_context_blockers(
        cls, context: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Return durable V22 confirmation blockers while permitting Proposal storage."""

        if cls._workflow_version_from_context(context) < 22:
            return []
        blockers: list[dict[str, Any]] = []
        quality_gate = context.get("reference_quality_gate")
        if not isinstance(quality_gate, dict):
            blockers.append(
                {
                    "code": "reference.quality_gate_missing",
                    "message": "参考文献品質ゲートの記録がありません。",
                }
            )
        elif quality_gate.get("passed") is not True:
            issues = [
                item
                for item in quality_gate.get("issues") or []
                if isinstance(item, dict) and item.get("severity", "error") == "error"
            ]
            blockers.append(
                {
                    "code": "reference.quality_gate_failed",
                    "message": "参考文献品質ゲートが未合格です。",
                    "issues": issues,
                }
            )

        unresolved_statuses = {
            "ambiguous",
            "low_match",
            "metadata_incomplete",
            "manifestations_retained",
            "manifestation_retained_unconfirmed",
            "pending",
            "unresolved",
            "user_input_required",
        }
        for entry in cls._reference_records(context):
            reference_id = str(
                entry.get("reference_id")
                or entry.get("machine_reference_id")
                or "reference:unknown"
            )
            title = str(entry.get("title") or entry.get("original_text") or reference_id)
            detail = {
                "reference_id": reference_id,
                "title": title,
            }
            explicitly_blocked = entry.get("final_revision_blocked") is True
            if explicitly_blocked:
                blockers.append(
                    {
                        **detail,
                        "code": "reference.final_revision_blocked",
                        "message": "書誌の未確定項目が残っています。",
                        "required_fields": list(
                            entry.get("required_missing_fields")
                            or entry.get("user_required_metadata")
                            or []
                        ),
                    }
                )
            translator_status = str(entry.get("translator_status") or "")
            if (
                translator_status == "required_missing"
                or entry.get("translator_required_missing") is True
                or (translator_status == "undetermined" and explicitly_blocked)
            ):
                blockers.append(
                    {
                        **detail,
                        "code": "reference.translator_unconfirmed",
                        "message": "翻訳書の訳者が未確認です。",
                    }
                )
            resolution_status = str(entry.get("resolution_status") or "")
            consulted_status = str(entry.get("consulted_status") or "")
            if explicitly_blocked and (
                resolution_status in unresolved_statuses or consulted_status == "unconfirmed"
            ):
                blockers.append(
                    {
                        **detail,
                        "code": "reference.manifestation_unconfirmed",
                        "message": "実際に参照した版・巻・原著／翻訳の確定が必要です。",
                        "resolution_status": resolution_status or None,
                        "consulted_status": consulted_status or None,
                    }
                )
            if resolution_status == "user_input_required" or entry.get(
                "user_required_metadata"
            ):
                blockers.append(
                    {
                        **detail,
                        "code": "reference.user_metadata_required",
                        "message": "自著・未刊資料の書誌情報をユーザーが指定する必要があります。",
                        "required_fields": list(entry.get("user_required_metadata") or []),
                    }
                )

        unique: list[dict[str, Any]] = []
        fingerprints: set[tuple[str, str]] = set()
        for blocker in blockers:
            fingerprint = (
                str(blocker.get("code") or ""),
                str(blocker.get("reference_id") or ""),
            )
            if fingerprint in fingerprints:
                continue
            fingerprints.add(fingerprint)
            unique.append(blocker)
        return unique

    @staticmethod
    def _reference_blocker_summary(blockers: list[dict[str, Any]]) -> str:
        summaries: list[str] = []
        for blocker in blockers:
            label = str(blocker.get("title") or blocker.get("reference_id") or "参考文献全体")
            message = str(blocker.get("message") or blocker.get("code") or "未確定")
            fields = blocker.get("required_fields") or []
            suffix = f"（必要項目: {', '.join(map(str, fields))}）" if fields else ""
            summaries.append(f"{label}: {message}{suffix}")
        return " / ".join(dict.fromkeys(summaries))

    @classmethod
    def _refresh_reference_completion_state_v22(
        cls, context: dict[str, Any]
    ) -> None:
        """Synchronize every Proposal projection after a reference decision.

        Reference selection and metadata confirmation happen after the worker has
        persisted its completion gate.  Recompute the reference-owned condition,
        approval projection, and gate entries together so the UI, audit record,
        and final approval cannot disagree about the same Proposal.
        """

        if cls._workflow_version_from_context(context) < 22:
            return
        blockers = cls._v22_reference_context_blockers(context)
        context["reference_approval"] = {
            "status": "human_confirmation_required" if blockers else "ready",
            "final_revision_blocked": bool(blockers),
            "blocking_reasons": blockers,
        }
        conditions = [
            dict(item)
            for item in context.get("conditional_conditions") or []
            if isinstance(item, dict)
            and item.get("reason") != "bibliography_confirmation_required"
        ]
        reference_condition: dict[str, Any] | None = None
        if blockers:
            reference_ids = list(
                dict.fromkeys(
                    str(item.get("reference_id") or "").strip()
                    for item in blockers
                    if str(item.get("reference_id") or "").strip()
                )
            )
            reference_condition = {
                "reason": "bibliography_confirmation_required",
                "reference_count": len(reference_ids) or len(blockers),
                "reference_ids": reference_ids,
                "disposition": "conditional_proposal_only",
                "final_revision_approval": "blocked",
            }
            conditions.append(reference_condition)
        context["conditional_conditions"] = conditions

        current = context.get("conditional_success")
        if (
            isinstance(current, dict)
            and current.get("reason") == "bibliography_confirmation_required"
        ):
            if reference_condition is not None:
                context["conditional_success"] = reference_condition
            elif conditions:
                context["conditional_success"] = dict(conditions[0])
            else:
                context.pop("conditional_success", None)
        elif reference_condition is not None and not isinstance(current, dict):
            context["conditional_success"] = reference_condition
        context["completion_status"] = (
            "conditional_success"
            if conditions or isinstance(context.get("conditional_success"), dict)
            else "success"
        )

        gate = context.get("completion_gate")
        if not isinstance(gate, dict):
            return
        blocking_reasons = [
            dict(item)
            for item in gate.get("blocking_reasons") or []
            if isinstance(item, dict)
            and not str(item.get("code") or "").startswith("reference.")
        ]
        if blockers:
            blocking_reasons.append(
                {
                    "code": "reference.final_revision_confirmation_required",
                    "message": (
                        "実際に参照した版・巻・原著／翻訳・訳者等の確定が必要です。"
                    ),
                    "details": blockers,
                }
            )
        refreshed_gate = {
            **gate,
            "passed": not blocking_reasons,
            "quality_status": (
                "needs_human_review" if blocking_reasons else "ready"
            ),
            "blocking_count": len(blocking_reasons),
            "blocking_reasons": blocking_reasons,
        }
        refreshed_gate.pop("grouped_blocking_reasons", None)
        context["completion_gate"] = refreshed_gate

    def persist_proposal(
        self,
        *,
        run_id: str,
        document_id: str,
        project_id: str,
        base_revision_id: str,
        outline_node_id: str | None,
        instructions: str,
        use_rag: bool,
        context: dict[str, Any],
        source_html: str,
        proposed_html: str,
        edit_summary: dict[str, Any],
        quality_status: str,
        expected_proposed_sha256: str | None = None,
    ) -> dict[str, Any]:
        self.validator.assert_valid(source_html)
        self.validator.assert_valid(proposed_html)
        if problems := heading_issues(source_html, proposed_html):
            raise NonRetryableFukamiruError(f"Heading integrity gate: {problems}")
        candidate_sha256 = hashlib.sha256(proposed_html.encode()).hexdigest()
        if expected_proposed_sha256 is not None and candidate_sha256 != expected_proposed_sha256:
            raise FukamiruError("The persistence candidate does not match the final-quality SHA")
        surface_gate = JapaneseSurfaceQualityGate.guard_canonical_html(source_html, proposed_html)
        proposed_html = surface_gate.canonical_html
        workflow_version = self._workflow_version_from_context(context)
        reference_entries = list(context.get("reference_entries") or [])
        machine_reference_entries = list(context.get("machine_reference_entries") or [])
        if reference_entries or (workflow_version >= 22 and machine_reference_entries):
            normalized, _ = self._reference_render(
                proposed_html,
                reference_entries,
                workflow_version=workflow_version,
                machine_entries=machine_reference_entries,
            )
            if normalized != proposed_html or self._reference_format_issues(
                proposed_html,
                reference_entries,
                workflow_version=workflow_version,
                machine_entries=machine_reference_entries,
            ):
                raise FukamiruError(
                    "Mandatory bibliography formatting changed before persistence"
                )
        guarded_sha256 = hashlib.sha256(proposed_html.encode()).hexdigest()
        if expected_proposed_sha256 is not None and guarded_sha256 != expected_proposed_sha256:
            raise FukamiruError("The persistence surface gate changed the frozen final candidate")
        validation = self._validation(proposed_html, surface_gate=surface_gate)
        if not validation["valid"]:
            raise FukamiruError("Rewrite proposal is not valid canonical HTML")
        stored_context, quality_status = self._persistence_quality_context(
            context,
            quality_status=quality_status,
            surface_gate=surface_gate,
        )
        if workflow_version >= 24:
            stored_context["approval_structure_lock"] = (
                ManuscriptDiscourseService.rendered_boundary_lock(
                    proposed_html, stored_context.get("paragraph_plan") or {}
                )
            )
            stored_context["approval_section_structure"] = self._section_structure_signature(
                proposed_html
            )
        stored_edit_summary = dict(edit_summary)
        stored_edit_summary["japanese_surface_quality_gate"] = surface_gate.as_dict()
        proposal_id = str(uuid.uuid4())
        self.connection.execute(
            """
            INSERT INTO manuscript_rewrite_proposals(
                id, run_id, project_id, document_id, base_revision_id,
                outline_node_id, instructions, use_rag, context_json,
                proposed_html, proposed_sha256, validation_json,
                edit_summary_json, status, quality_status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                proposal_id,
                run_id,
                project_id,
                document_id,
                base_revision_id,
                outline_node_id,
                instructions,
                int(use_rag),
                json.dumps(stored_context, ensure_ascii=False, separators=(",", ":")),
                proposed_html,
                hashlib.sha256(proposed_html.encode()).hexdigest(),
                json.dumps(validation, ensure_ascii=False),
                json.dumps(stored_edit_summary, ensure_ascii=False, separators=(",", ":")),
                quality_status,
                utc_now(),
            ),
        )
        self.connection.commit()
        return self.get(proposal_id)

    def refresh_pending_proposal(
        self,
        proposal_id: str,
        *,
        context: dict[str, Any],
        source_html: str,
        proposed_html: str,
        edit_summary: dict[str, Any],
        quality_status: str,
        expected_proposed_sha256: str | None = None,
    ) -> dict[str, Any]:
        """Replace a pending same-Run proposal after a stricter gate is deployed."""
        proposal = self._row(proposal_id)
        if proposal["status"] != "pending":
            raise FukamiruError("Only a pending rewrite proposal can be refreshed")
        self.validator.assert_valid(source_html)
        self.validator.assert_valid(proposed_html)
        if problems := heading_issues(source_html, proposed_html):
            raise NonRetryableFukamiruError(f"Heading integrity gate: {problems}")
        candidate_sha256 = hashlib.sha256(proposed_html.encode()).hexdigest()
        if expected_proposed_sha256 is not None and candidate_sha256 != expected_proposed_sha256:
            raise FukamiruError("The persistence candidate does not match the final-quality SHA")
        surface_gate = JapaneseSurfaceQualityGate.guard_canonical_html(source_html, proposed_html)
        proposed_html = surface_gate.canonical_html
        workflow_version = self._workflow_version_from_context(context)
        reference_entries = list(context.get("reference_entries") or [])
        machine_reference_entries = list(context.get("machine_reference_entries") or [])
        guarded_sha256 = hashlib.sha256(proposed_html.encode()).hexdigest()
        if expected_proposed_sha256 is not None and guarded_sha256 != expected_proposed_sha256:
            raise FukamiruError("The persistence surface gate changed the frozen final candidate")
        validation = self._validation(proposed_html, surface_gate=surface_gate)
        if not validation["valid"]:
            raise FukamiruError("Refreshed rewrite proposal is not valid canonical HTML")
        if reference_entries or (workflow_version >= 22 and machine_reference_entries):
            normalized, _ = self._reference_render(
                proposed_html,
                reference_entries,
                workflow_version=workflow_version,
                machine_entries=machine_reference_entries,
            )
            if normalized != proposed_html or self._reference_format_issues(
                proposed_html,
                reference_entries,
                workflow_version=workflow_version,
                machine_entries=machine_reference_entries,
            ):
                raise FukamiruError(
                    "Mandatory bibliography formatting changed before persistence"
                )
        stored_context, quality_status = self._persistence_quality_context(
            context,
            quality_status=quality_status,
            surface_gate=surface_gate,
        )
        if workflow_version >= 24:
            stored_context["approval_structure_lock"] = (
                ManuscriptDiscourseService.rendered_boundary_lock(
                    proposed_html, stored_context.get("paragraph_plan") or {}
                )
            )
            stored_context["approval_section_structure"] = self._section_structure_signature(
                proposed_html
            )
        stored_edit_summary = dict(edit_summary)
        stored_edit_summary["japanese_surface_quality_gate"] = surface_gate.as_dict()
        self.connection.execute(
            """
            UPDATE manuscript_rewrite_proposals
            SET context_json = ?, proposed_html = ?, proposed_sha256 = ?,
                validation_json = ?, edit_summary_json = ?, quality_status = ?,
                error_text = NULL
            WHERE id = ? AND status = 'pending'
            """,
            (
                json.dumps(stored_context, ensure_ascii=False, separators=(",", ":")),
                proposed_html,
                hashlib.sha256(proposed_html.encode()).hexdigest(),
                json.dumps(validation, ensure_ascii=False),
                json.dumps(stored_edit_summary, ensure_ascii=False, separators=(",", ":")),
                quality_status,
                proposal_id,
            ),
        )
        self.connection.commit()
        return self.get(proposal_id)

    def select_reference(
        self,
        proposal_id: str,
        reference_id: str,
        candidate_id: str | None,
        expected_sha256: str,
        *,
        candidate_ids: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """Atomically select one V21 candidate or one-or-more V22 manifestations."""

        row = self._row(proposal_id)
        if row["status"] != "pending" or row["proposed_sha256"] != expected_sha256:
            raise FukamiruError("Proposal changed or is not pending; reload before selecting")
        context = json.loads(row["context_json"])
        entries = list(context.get("reference_entries") or [])
        index = next(
            (i for i, e in enumerate(entries) if e.get("reference_id") == reference_id), None
        )
        if index is None:
            raise FukamiruError("Unknown reference")
        workflow_version = self._workflow_version_from_context(context)
        if isinstance(candidate_ids, (str, bytes)):
            raise FukamiruError("candidate_ids must be a list")
        plural_selection_supplied = candidate_ids is not None
        requested_candidate_ids = list(
            dict.fromkeys(
                str(value).strip()
                for value in (candidate_ids or [])
                if str(value).strip()
            )
        )
        selected_candidate_id = str(candidate_id or "").strip()
        try:
            if workflow_version >= 22:
                if selected_candidate_id and plural_selection_supplied:
                    raise FukamiruError(
                        "V22 reference selection accepts candidate_id or candidate_ids, not both"
                    )
                if plural_selection_supplied and not requested_candidate_ids:
                    raise FukamiruError("Select at least one V22 manifestation")
                if not plural_selection_supplied:
                    if not selected_candidate_id:
                        raise FukamiruError("Select at least one V22 manifestation")
                    # Preserve compatibility for older V22 clients while storing the
                    # same plural manifestation contract as the checkbox UI.
                    requested_candidate_ids = [selected_candidate_id]
                entries[index] = JapaneseReferenceService.confirm_manifestations_v22(
                    entries[index], requested_candidate_ids
                )
            else:
                if plural_selection_supplied:
                    raise FukamiruError(
                        "V21 reference selection accepts exactly one candidate_id"
                    )
                if not selected_candidate_id:
                    raise FukamiruError("Select one V21 reference candidate")
                entries[index] = JapaneseReferenceService.select_candidate(
                    entries[index], selected_candidate_id
                )
        except ValueError as exc:
            raise FukamiruError(str(exc)) from exc
        if workflow_version >= 22:
            inventory = context.get("reference_inventory")
            if isinstance(inventory, dict):
                inventory_entries = [
                    dict(entry) if isinstance(entry, dict) else entry
                    for entry in inventory.get("entries") or []
                ]
                for inventory_index, inventory_entry in enumerate(inventory_entries):
                    if (
                        isinstance(inventory_entry, dict)
                        and inventory_entry.get("reference_id") == reference_id
                    ):
                        inventory_entries[inventory_index] = entries[index]
                context["reference_inventory"] = {
                    **inventory,
                    "entries": inventory_entries,
                }
        rendered, edits = self._reference_render(
            str(row["proposed_html"]),
            entries,
            workflow_version=workflow_version,
            machine_entries=list(context.get("machine_reference_entries") or []),
        )
        self.validator.assert_valid(rendered)
        context["reference_entries"] = entries
        context["reference_quality_gate"] = self._reference_quality_report(
            rendered,
            entries,
            workflow_version=workflow_version,
            machine_entries=list(context.get("machine_reference_entries") or []),
        )
        selection_record: dict[str, Any] = {
            "reference_id": reference_id,
            "edits": edits,
            "before_sha256": expected_sha256,
            "workflow_version": workflow_version,
        }
        if workflow_version >= 22:
            confirmed_candidate_ids = list(entries[index]["selected_candidate_ids"])
            selection_record["candidate_ids"] = confirmed_candidate_ids
            selection_record["selected_manifestation_count"] = len(
                confirmed_candidate_ids
            )
            self._refresh_reference_completion_state_v22(context)
        else:
            selection_record["candidate_id"] = selected_candidate_id
        context.setdefault("reference_selection_history", []).append(selection_record)
        context.pop("final_quality_sha256", None)
        context["reference_selection_requires_revalidation"] = True
        cursor = self.connection.execute(
            "UPDATE manuscript_rewrite_proposals SET proposed_html=?,proposed_sha256=?,"
            "context_json=?,validation_json=?,quality_status='needs_human_review' "
            "WHERE id=? AND proposed_sha256=? AND status='pending'",
            (
                rendered,
                hashlib.sha256(rendered.encode()).hexdigest(),
                json.dumps(context, ensure_ascii=False),
                json.dumps(self._validation(rendered), ensure_ascii=False),
                proposal_id,
                expected_sha256,
            ),
        )
        if cursor.rowcount != 1:
            self.connection.rollback()
            raise FukamiruError("Proposal changed during reference selection; reload")
        self.connection.commit()
        return self.get(proposal_id)

    def confirm_reference_metadata_v22(
        self,
        proposal_id: str,
        reference_id: str,
        expected_sha256: str,
        *,
        is_translation: bool,
        translators: Iterable[str] = (),
        metadata: dict[str, Any] | None = None,
        is_self_authored: bool | None = None,
        is_unpublished: bool | None = None,
    ) -> dict[str, Any]:
        """Confirm V22 bibliography metadata without changing consultation status.

        Machine-detected references remain in their visibly separate section and
        remain ``not_consulted``.  The same endpoint can complete metadata for an
        explicitly cited self-authored/unpublished entry or one exact consulted
        manifestation.  Nested manifestations are addressed by their generated
        ``reference_id`` so sibling editions cannot be modified accidentally.
        """

        row = self._row(proposal_id)
        if row["status"] != "pending" or row["proposed_sha256"] != expected_sha256:
            raise FukamiruError("Proposal changed or is not pending; reload before confirming")
        context = json.loads(row["context_json"])
        if self._workflow_version_from_context(context) < 22:
            raise FukamiruError(
                "Machine-reference metadata confirmation is available only in V22"
            )

        cleaned_translators = list(
            dict.fromkeys(str(value).strip() for value in translators if str(value).strip())
        )
        if is_translation and not cleaned_translators:
            raise FukamiruError("A translated reference requires at least one translator")
        if not is_translation and cleaned_translators:
            raise FukamiruError(
                "An original-language reference cannot be confirmed with translators"
            )

        reference_entries = [
            dict(item)
            for item in context.get("reference_entries") or []
            if isinstance(item, dict)
        ]
        machine_sections = JapaneseReferenceService.bibliography_sections_v22(
            reference_entries,
            machine_entries=[
                dict(item)
                for item in context.get("machine_reference_entries") or []
                if isinstance(item, dict)
            ],
            workflow_version=self._workflow_version_from_context(context),
        )
        machine_entries = next(
            (
                [dict(item) for item in section.get("entries") or []]
                for section in machine_sections
                if section.get("section_key") == "machine_detected_references"
            ),
            [],
        )
        machine_index = next(
            (
                index
                for index, entry in enumerate(machine_entries)
                if str(entry.get("machine_reference_id") or entry.get("reference_id") or "")
                == reference_id
            ),
            None,
        )
        ordinary_index = next(
            (
                index
                for index, entry in enumerate(reference_entries)
                if str(entry.get("reference_id") or "") == reference_id
            ),
            None,
        )
        nested_location = next(
            (
                (parent_index, child_index)
                for parent_index, entry in enumerate(reference_entries)
                for child_index, child in enumerate(
                    entry.get("expanded_reference_entries") or []
                )
                if isinstance(child, dict)
                and str(child.get("reference_id") or "") == reference_id
            ),
            None,
        )
        classification_request = {
            "is_self_authored": is_self_authored,
            "is_unpublished": is_unpublished,
        }
        classification_was_supplied = any(
            value is not None for value in classification_request.values()
        )
        explicit_self_or_unpublished = any(
            value is True for value in classification_request.values()
        )
        manual_consulted_entry = False
        if machine_index is not None:
            if classification_was_supplied:
                raise FukamiruError(
                    "A machine-detected reference cannot be reclassified as "
                    "self-authored or unpublished through metadata confirmation"
                )
            target = dict(machine_entries[machine_index])
            target_kind = "machine_detected"
        elif nested_location is not None:
            if classification_was_supplied:
                raise FukamiruError(
                    "A consulted manifestation cannot be reclassified as "
                    "self-authored or unpublished through metadata confirmation"
                )
            parent_index, child_index = nested_location
            target = dict(
                reference_entries[parent_index]["expanded_reference_entries"][
                    child_index
                ]
            )
            target_kind = "consulted_manifestation"
        elif ordinary_index is not None:
            ordinary = reference_entries[ordinary_index]
            manual_consulted_entry = bool(
                ordinary.get("manual_consulted_manifestation_allowed") is True
                and ordinary.get("final_revision_blocked") is True
                and str(ordinary.get("resolution_status") or "")
                in {"specified_scope_unresolved", "ambiguous"}
            )
            already_self_or_unpublished = (
                ordinary.get("resolution_status") == "user_input_required"
                or "user_required_metadata" in ordinary
                or ordinary.get("is_self_authored") is True
                or ordinary.get("is_unpublished") is True
            )
            generic_unresolved = str(ordinary.get("resolution_status") or "") in {
                "not_found",
                "low_match",
                "unresolved",
            }
            consulted_metadata_incomplete = (
                not already_self_or_unpublished
                and ordinary.get("final_revision_blocked") is True
                and str(ordinary.get("consulted_status") or "")
                in {"user_confirmed", "support_audit_selected"}
                and str(ordinary.get("resolution_status") or "")
                in {"metadata_incomplete", "manifestation_confirmed", "resolved"}
                and not ordinary.get("expanded_reference_entries")
            )
            if manual_consulted_entry:
                if classification_was_supplied:
                    raise FukamiruError(
                        "A manually entered consulted manifestation cannot be "
                        "reclassified as self-authored or unpublished"
                    )
                target = dict(ordinary)
                target_kind = "consulted_manifestation"
            elif consulted_metadata_incomplete:
                if classification_was_supplied:
                    raise FukamiruError(
                        "A consulted manifestation cannot be reclassified as "
                        "self-authored or unpublished through metadata confirmation"
                    )
                target = dict(ordinary)
                target_kind = "consulted_manifestation"
            elif not already_self_or_unpublished and not (
                generic_unresolved and explicit_self_or_unpublished
            ):
                raise FukamiruError(
                    "An unresolved ordinary reference requires an explicit "
                    "self-authored or unpublished classification"
                )
            else:
                target = dict(ordinary)
                target_kind = "self_or_unpublished"
        else:
            raise FukamiruError(
                "Unknown machine-detected, consulted manifestation, or "
                "self-authored/unpublished reference"
            )

        supplied = {
            str(key): value
            for key, value in (metadata or {}).items()
            if value is not None and value != ""
        }
        allowed_fields = {
            "author",
            "title",
            "record_type",
            "year",
            "venue",
            "pages",
            "volume",
            "issue",
            "edition",
            "publisher",
            "container_title",
            "publication_place",
            "publication_language",
            "series",
            "original_title",
            "translated_title",
            "doi",
            "isbn",
            "url",
        }
        unexpected = sorted(set(supplied) - allowed_fields)
        if unexpected:
            raise FukamiruError(
                "Unsupported reference metadata fields: " + ", ".join(unexpected)
            )
        confirmed_at = utc_now()
        provenance = dict(target.get("provenance") or {})
        classification_before = {
            field: target.get(field) for field in classification_request
        }
        classification_fields: list[str] = []
        for field, value in classification_request.items():
            if value is None:
                continue
            target[field] = value
            provenance[field] = self._user_confirmed_reference_provenance(confirmed_at)
            classification_fields.append(field)
        for field, value in supplied.items():
            clean_value = str(value).strip() if isinstance(value, str) else value
            target[field] = clean_value
            provenance[field] = self._user_confirmed_reference_provenance(confirmed_at)
        if supplied.get("author"):
            target["authors"] = [str(supplied["author"]).strip()]
        venue = str(supplied.get("venue") or "").strip()
        record_kind = JapaneseReferenceService.bibliography_kind(target)
        if venue:
            venue_field = (
                "container_title"
                if record_kind in {"article", "chapter"}
                else "publisher"
            )
            if not str(target.get(venue_field) or "").strip():
                target[venue_field] = venue
                provenance[venue_field] = self._user_confirmed_reference_provenance(
                    confirmed_at
                )

        target["is_translation"] = is_translation
        target["translation_status"] = (
            "confirmed_translation" if is_translation else "confirmed_original"
        )
        target["translators"] = cleaned_translators
        target["translator_status"] = "verified" if is_translation else "not_required"
        for field in ("is_translation", "translation_status", "translators"):
            provenance[field] = self._user_confirmed_reference_provenance(confirmed_at)
        target["provenance"] = provenance
        target["metadata_confirmation_status"] = "user_confirmed"
        target["metadata_confirmed_at"] = confirmed_at
        classification_change = None
        if classification_fields:
            classification_change = {
                "source": "user_explicit_reference_classification",
                "before": classification_before,
                "after": {
                    field: target.get(field) for field in classification_request
                },
                "changed_fields": [
                    field
                    for field in classification_fields
                    if classification_before.get(field) != target.get(field)
                ],
                "confirmed_at": confirmed_at,
            }
            target.setdefault("reference_classification_history", []).append(
                classification_change
            )

        if target_kind == "machine_detected":
            # Metadata confirmation is not evidence of use.  Never move this
            # entry into the ordinary bibliography without a separate action.
            manifestation_axes, missing, identity_evidence = (
                JapaneseReferenceService.machine_manifestation_requirements_v22(
                    target
                )
            )
            target.update(
                reference_role="machine_detected_not_confirmed_consulted",
                consulted_status="not_consulted",
                reference_usage_disposition="machine_detected_related",
                human_confirmation_required=bool(missing),
                resolution_status=(
                    "metadata_incomplete" if missing else "machine_detected"
                ),
                manifestation_axes=manifestation_axes,
                manifestation_identity_evidence=identity_evidence,
                edition_status=manifestation_axes["edition"],
                volume_status=manifestation_axes["volume"],
                final_revision_blocked=bool(missing),
                conditional_proposal_allowed=bool(missing),
                required_missing_fields=missing,
            )
            if missing:
                target["user_action_required"] = {
                    "code": "confirm_machine_reference_manifestation_metadata",
                    "fields": missing,
                    "message": (
                        "機械判別参考文献の原著／翻訳・訳者・版・巻のうち、"
                        "未確定の項目を確認してください。"
                    ),
                }
            else:
                target.pop("user_action_required", None)
            machine_entries[machine_index] = target
        elif target_kind == "self_or_unpublished":
            missing = JapaneseReferenceService.required_missing_fields(target)
            user_missing = (
                JapaneseReferenceService.self_or_unpublished_required_metadata_v22(
                    target
                )
            )
            blocked = bool(missing or user_missing)
            target.update(
                consulted_status="user_confirmed",
                resolution_status="metadata_incomplete" if blocked else "resolved",
                final_revision_blocked=blocked,
                conditional_proposal_allowed=blocked,
                required_missing_fields=missing,
            )
            if blocked:
                target["user_required_metadata"] = user_missing
                target["user_action_required"] = {
                    "code": "complete_self_or_unpublished_metadata",
                    "fields": list(dict.fromkeys([*user_missing, *missing])),
                    "message": "自著・未刊資料の必須書誌情報を指定してください。",
                }
            else:
                target.pop("user_required_metadata", None)
                target.pop("user_action_required", None)
            reference_entries[ordinary_index] = target
            inventory = context.get("reference_inventory")
            if isinstance(inventory, dict):
                inventory_entries = [
                    dict(item) if isinstance(item, dict) else item
                    for item in inventory.get("entries") or []
                ]
                for index, item in enumerate(inventory_entries):
                    if (
                        isinstance(item, dict)
                        and str(item.get("reference_id") or "") == reference_id
                    ):
                        inventory_entries[index] = target
                context["reference_inventory"] = {
                    **inventory,
                    "entries": inventory_entries,
                }
        else:
            target = JapaneseReferenceService.refresh_consulted_manifestation_v22(
                target
            )
            if manual_consulted_entry:
                target.update(
                    manual_consulted_manifestation_allowed=False,
                    manual_consulted_manifestation_confirmed=True,
                    manual_consulted_manifestation_confirmed_at=confirmed_at,
                    work_identity_status="user_confirmed_manual_manifestation",
                    edition_status="user_entered_consulted_manifestation",
                )
            if nested_location is not None:
                parent_index, child_index = nested_location
                parent = dict(reference_entries[parent_index])
                children = [
                    dict(item) if isinstance(item, dict) else item
                    for item in parent.get("expanded_reference_entries") or []
                ]
                children[child_index] = target
                parent["expanded_reference_entries"] = children
                parent = JapaneseReferenceService.refresh_consulted_parent_v22(
                    parent
                )
                reference_entries[parent_index] = parent
                inventory_target = parent
                inventory_reference_id = str(parent.get("reference_id") or "")
            else:
                assert ordinary_index is not None
                reference_entries[ordinary_index] = target
                inventory_target = target
                inventory_reference_id = reference_id
            inventory = context.get("reference_inventory")
            if isinstance(inventory, dict):
                inventory_entries = [
                    dict(item) if isinstance(item, dict) else item
                    for item in inventory.get("entries") or []
                ]
                for index, item in enumerate(inventory_entries):
                    if (
                        isinstance(item, dict)
                        and str(item.get("reference_id") or "")
                        == inventory_reference_id
                    ):
                        inventory_entries[index] = inventory_target
                context["reference_inventory"] = {
                    **inventory,
                    "entries": inventory_entries,
                }

        rendered, edits = self._reference_render(
            str(row["proposed_html"]),
            reference_entries,
            workflow_version=22,
            machine_entries=machine_entries,
        )
        self.validator.assert_valid(rendered)
        context["reference_entries"] = reference_entries
        context["machine_reference_entries"] = machine_entries
        context["reference_quality_gate"] = self._reference_quality_report(
            rendered,
            reference_entries,
            workflow_version=22,
            machine_entries=machine_entries,
        )
        self._refresh_reference_completion_state_v22(context)
        normalized_machine_id = (
            str(target.get("machine_reference_id") or "")
            if target_kind == "machine_detected"
            else None
        )
        context.setdefault("reference_metadata_confirmation_history", []).append(
            {
                "reference_id": reference_id,
                "normalized_machine_reference_id": normalized_machine_id,
                "reference_kind": target_kind,
                "source_reference_id": target.get("source_reference_id"),
                "candidate_id": target.get("candidate_id"),
                "before_sha256": expected_sha256,
                "is_translation": is_translation,
                "translators": cleaned_translators,
                "updated_fields": sorted([*supplied, *classification_fields]),
                "classification_change": classification_change,
                "reference_role_after": target.get("reference_role"),
                "consulted_status_after": target.get("consulted_status"),
                "confirmed_at": confirmed_at,
                "edits": edits,
            }
        )
        context.pop("final_quality_sha256", None)
        context["reference_selection_requires_revalidation"] = True
        rendered_sha256 = hashlib.sha256(rendered.encode()).hexdigest()
        cursor = self.connection.execute(
            "UPDATE manuscript_rewrite_proposals SET proposed_html=?,proposed_sha256=?,"
            "context_json=?,validation_json=?,quality_status='needs_human_review' "
            "WHERE id=? AND proposed_sha256=? AND status='pending'",
            (
                rendered,
                rendered_sha256,
                json.dumps(context, ensure_ascii=False, separators=(",", ":")),
                json.dumps(self._validation(rendered), ensure_ascii=False),
                proposal_id,
                expected_sha256,
            ),
        )
        if cursor.rowcount != 1:
            self.connection.rollback()
            raise FukamiruError("Proposal changed during reference confirmation; reload")
        self.connection.commit()
        return self.get(proposal_id)

    @staticmethod
    def _user_confirmed_reference_provenance(confirmed_at: str) -> dict[str, Any]:
        return {
            "kind": "user_confirmed",
            "label": "ユーザーが書誌情報を確認",
            "source": "manuscript_rewrite_reference_metadata_confirmation",
            "confirmed_at": confirmed_at,
        }

    @classmethod
    def _refresh_language_review_condition_v22(
        cls, context: dict[str, Any]
    ) -> None:
        pending = [
            item
            for item in context.get("language_review_requirements") or []
            if isinstance(item, dict)
            and item.get("status") not in {"resolved", "no_revision_needed"}
        ]
        conditions: list[dict[str, Any]] = []
        for raw_condition in context.get("conditional_conditions") or []:
            if not isinstance(raw_condition, dict):
                continue
            condition = dict(raw_condition)
            if condition.get("reason") != "language_review_required":
                conditions.append(condition)
                continue
            if not pending:
                continue
            condition["requirement_count"] = len(pending)
            condition["requirement_ids"] = [
                str(
                    item.get("requirement_id")
                    or item.get("target_id")
                    or cls._language_semantic_review_id_v22(item)
                )
                for item in pending
            ]
            conditions.append(condition)
        context["conditional_conditions"] = conditions

    @classmethod
    def _refresh_semantic_completion_gate_v22(
        cls,
        context: dict[str, Any],
        *,
        resolved_review_id: str,
        resolved_source_type: str,
        resolved_source_item_id: str,
        updated_condition: dict[str, Any] | None,
    ) -> None:
        gate = context.get("completion_gate")
        if not isinstance(gate, dict):
            return
        refreshed: list[dict[str, Any]] = []
        for raw_blocker in gate.get("blocking_reasons") or []:
            if not isinstance(raw_blocker, dict):
                continue
            blocker = dict(raw_blocker)
            details = blocker.get("details")
            if (
                resolved_source_type == "language_review_requirement"
                and isinstance(details, dict)
                and cls._language_semantic_review_id_v22(details)
                == resolved_review_id
            ):
                continue
            if (
                resolved_source_type == "semantic_condition_unit"
                and str(blocker.get("code") or "").startswith(
                    "reasoning.semantic_resolution_required:"
                )
                and isinstance(details, dict)
                and cls._semantic_condition_key_v22(details) == resolved_source_item_id
            ):
                if updated_condition is None:
                    continue
                blocker["details"] = updated_condition
            refreshed.append(blocker)
        updated_gate = {**gate, "blocking_reasons": refreshed, "blocking_count": len(refreshed)}
        if "grouped_blocking_reasons" in updated_gate:
            # This projection is stale after an item-level human judgement.  It
            # must be regenerated by the next validation rather than displayed
            # as if the resolved issue remained current.
            updated_gate.pop("grouped_blocking_reasons", None)
        if not refreshed:
            updated_gate["passed"] = True
            updated_gate["quality_status"] = "ready"
        context["completion_gate"] = updated_gate

    def resolve_semantic_review_v22(
        self,
        proposal_id: str,
        review_id: str,
        expected_sha256: str,
        expected_context_sha256: str,
        *,
        decision: str,
        rationale: str,
    ) -> dict[str, Any]:
        """Record one explicit human semantic judgement without invoking an LLM."""

        review_id = review_id.strip()
        decision = decision.strip()
        rationale = rationale.strip()
        if not review_id or not decision or not rationale:
            raise FukamiruError(
                "Semantic review requires a review ID, decision, and non-empty rationale"
            )
        if decision not in _V22_SEMANTIC_REVIEW_DECISIONS:
            raise FukamiruError(
                "Unknown semantic review decision; reload and select an available decision"
            )
        row = self._row(proposal_id)
        raw_context = str(row["context_json"])
        context_sha256 = hashlib.sha256(raw_context.encode()).hexdigest()
        if (
            row["status"] != "pending"
            or str(row["proposed_sha256"]) != expected_sha256
            or context_sha256 != expected_context_sha256
        ):
            raise FukamiruError(
                "Proposal or semantic review state changed; reload before recording judgement"
            )
        context = json.loads(raw_context)
        if self._workflow_version_from_context(context) < 22:
            raise FukamiruError("Human semantic adjudication is available only in V22")

        pending_items = self._semantic_review_items_v22(context)
        target = next(
            (item for item in pending_items if item.get("review_id") == review_id),
            None,
        )
        if target is None:
            for requirement in context.get("language_review_requirements") or []:
                if not isinstance(requirement, dict):
                    continue
                if self._language_semantic_review_id_v22(requirement) != review_id:
                    continue
                if requirement.get("review_class") != "human_semantic_judgement_required":
                    raise FukamiruError(
                        "Only human_semantic_judgement_required items may be adjudicated"
                    )
                raise FukamiruError("This semantic review item is already resolved")
            raise FukamiruError("Unknown or already resolved semantic review item")

        resolved_at = utc_now()
        judgement = {
            "review_id": review_id,
            "source_type": target["source_type"],
            "source_item_id": target["source_item_id"],
            "unit_id": target.get("unit_id"),
            "decision": decision,
            "rationale": rationale,
            "resolved_at": resolved_at,
            "actor_type": "human",
        }
        updated_condition: dict[str, Any] | None = None
        if target["source_type"] == "language_review_requirement":
            requirements: list[Any] = []
            found = False
            for raw_requirement in context.get("language_review_requirements") or []:
                if not isinstance(raw_requirement, dict):
                    requirements.append(raw_requirement)
                    continue
                requirement = dict(raw_requirement)
                if self._language_semantic_review_id_v22(requirement) == review_id:
                    if requirement.get("review_class") != (
                        "human_semantic_judgement_required"
                    ):
                        raise FukamiruError(
                            "Only human_semantic_judgement_required items may be adjudicated"
                        )
                    requirement["status_before_human_judgement"] = requirement.get("status")
                    requirement["status"] = "resolved"
                    requirement["semantic_review_id"] = review_id
                    requirement["human_judgement"] = judgement
                    found = True
                requirements.append(requirement)
            if not found:
                raise FukamiruError("Semantic review target disappeared; reload")
            context["language_review_requirements"] = requirements
            self._refresh_language_review_condition_v22(context)
        else:
            conditions: list[dict[str, Any]] = []
            found = False
            for raw_condition in context.get("conditional_conditions") or []:
                if not isinstance(raw_condition, dict):
                    continue
                condition = dict(raw_condition)
                if condition.get("reason") != "semantic_resolution_required":
                    conditions.append(condition)
                    continue
                unit_id = str(target.get("unit_id") or "")
                candidate_id = self._condition_semantic_review_id_v22(condition, unit_id)
                if candidate_id != review_id:
                    conditions.append(condition)
                    continue
                group_unit_ids = list(
                    dict.fromkeys(
                        str(value).strip()
                        for value in condition.get("unit_ids") or []
                        if str(value).strip()
                    )
                )
                if not group_unit_ids:
                    group_unit_ids = [unit_id]
                resolved = list(
                    dict.fromkeys(
                        [
                            str(value).strip()
                            for value in condition.get("resolved_unit_ids") or []
                            if str(value).strip()
                        ]
                        + [unit_id]
                    )
                )
                remaining = [value for value in group_unit_ids if value not in resolved]
                found = True
                if remaining:
                    condition["resolved_unit_ids"] = resolved
                    condition["remaining_unit_ids"] = remaining
                    condition["resolution_status"] = "partially_resolved"
                    condition.setdefault("human_judgements", []).append(judgement)
                    updated_condition = condition
                    conditions.append(condition)
                # When no members remain, omit the condition.  Its full history
                # remains in semantic_adjudication_history and event_log.
            if not found:
                raise FukamiruError("Semantic review target disappeared; reload")
            context["conditional_conditions"] = conditions

        current_conditional = context.get("conditional_success")
        if (
            isinstance(current_conditional, dict)
            and current_conditional.get("reason")
            in {"semantic_resolution_required", "language_review_required"}
        ):
            current_reason = str(current_conditional.get("reason"))
            replacement = next(
                (
                    dict(item)
                    for item in context.get("conditional_conditions") or []
                    if isinstance(item, dict)
                    and item.get("reason") == current_reason
                ),
                None,
            )
            if replacement is None:
                fallback = next(
                    (
                        dict(item)
                        for item in context.get("conditional_conditions") or []
                        if isinstance(item, dict)
                    ),
                    None,
                )
                if fallback is None:
                    context.pop("conditional_success", None)
                else:
                    context["conditional_success"] = fallback
            else:
                context["conditional_success"] = replacement
        if not context.get("conditional_conditions") and not context.get(
            "conditional_success"
        ):
            context["completion_status"] = "success"
        else:
            context["completion_status"] = "conditional_success"

        context.setdefault("semantic_adjudication_history", []).append(judgement)
        self._refresh_semantic_completion_gate_v22(
            context,
            resolved_review_id=review_id,
            resolved_source_type=str(target["source_type"]),
            resolved_source_item_id=str(target["source_item_id"]),
            updated_condition=updated_condition,
        )
        context.pop("semantic_review_items", None)
        new_context_json = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        new_context_sha256 = hashlib.sha256(new_context_json.encode()).hexdigest()
        remaining_count = len(self._semantic_review_items_v22(context))
        cursor = self.connection.execute(
            "UPDATE manuscript_rewrite_proposals SET context_json=?, "
            "quality_status='needs_human_review' WHERE id=? AND status='pending' "
            "AND proposed_sha256=? AND context_json=?",
            (new_context_json, proposal_id, expected_sha256, raw_context),
        )
        if cursor.rowcount != 1:
            self.connection.rollback()
            raise FukamiruError(
                "Proposal or semantic review state changed; reload before recording judgement"
            )
        self.connection.execute(
            """
            INSERT INTO event_log(
                run_id, event_type, object_type, object_id, payload_json, created_at
            ) VALUES (?, 'manuscript.semantic_review.resolved',
                      'manuscript_rewrite_proposal', ?, ?, ?)
            """,
            (
                row["run_id"],
                proposal_id,
                json.dumps(
                    {
                        **judgement,
                        "context_sha256_before": context_sha256,
                        "context_sha256_after": new_context_sha256,
                        "remaining_semantic_review_count": remaining_count,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                resolved_at,
            ),
        )
        self.connection.commit()
        return self.get(proposal_id)

    def update_proposed(
        self,
        proposal_id: str,
        *,
        canonical_html: str,
        commit: bool = True,
    ) -> dict[str, Any]:
        proposal = self._row(proposal_id)
        if proposal["status"] != "pending":
            raise FukamiruError("Only a pending rewrite proposal can be edited")
        candidate_validation = self._validation(canonical_html)
        if candidate_validation["valid"]:
            surface_gate = JapaneseSurfaceQualityGate.guard_canonical_html(
                str(proposal["proposed_html"]), canonical_html
            )
            canonical_html = surface_gate.canonical_html
            validation = self._validation(canonical_html, surface_gate=surface_gate)
        else:
            validation = candidate_validation
        context = json.loads(proposal["context_json"])
        workflow_version = self._workflow_version_from_context(context)
        reference_entries = context.get("reference_entries") or []
        machine_reference_entries = list(context.get("machine_reference_entries") or [])
        if workflow_version >= 22 and validation["valid"] and (
            reference_entries or machine_reference_entries
        ):
            normalized, _ = self._reference_render(
                canonical_html,
                reference_entries,
                workflow_version=workflow_version,
                machine_entries=machine_reference_entries,
            )
            if normalized != canonical_html or self._reference_format_issues(
                canonical_html,
                reference_entries,
                workflow_version=workflow_version,
                machine_entries=machine_reference_entries,
            ):
                raise FukamiruError(
                    "Mandatory bibliography formatting changed before proposal update"
                )
        self.connection.execute(
            """
            UPDATE manuscript_rewrite_proposals
            SET proposed_html = ?, proposed_sha256 = ?, validation_json = ?
            WHERE id = ?
            """,
            (
                canonical_html,
                hashlib.sha256(canonical_html.encode()).hexdigest(),
                json.dumps(validation, ensure_ascii=False),
                proposal_id,
            ),
        )
        if commit:
            self.connection.commit()
        return self.get(proposal_id)

    def discard(self, proposal_id: str) -> dict[str, Any]:
        """Hide a pending generated draft from review without deleting its audit trail."""
        proposal = self._row(proposal_id)
        if proposal["status"] != "pending":
            raise FukamiruError("Only a pending rewrite proposal can be discarded")
        resolved_at = utc_now()
        self.connection.execute(
            """
            UPDATE manuscript_rewrite_proposals
            SET status = 'rejected', resolved_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (resolved_at, proposal_id),
        )
        self.connection.execute(
            """
            INSERT INTO event_log(
                run_id, event_type, object_type, object_id, payload_json, created_at
            ) VALUES (?, 'manuscript.rewrite.discarded',
                      'manuscript_rewrite_proposal', ?, ?, ?)
            """,
            (
                proposal["run_id"],
                proposal_id,
                json.dumps(
                    {"status": "rejected", "approved_revision_created": False},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                resolved_at,
            ),
        )
        self.connection.commit()
        return self.get(proposal_id)

    def confirm(
        self,
        proposal_id: str,
        *,
        expected_sha256: str | None = None,
        acknowledge_unresolved: bool = False,
    ) -> dict[str, Any]:
        proposal = self._row(proposal_id)
        if proposal["status"] != "pending":
            raise FukamiruError("Rewrite proposal is no longer pending")
        document = self.connection.execute(
            "SELECT current_revision_id FROM content_documents WHERE id = ?",
            (proposal["document_id"],),
        ).fetchone()
        if document is None or document["current_revision_id"] != proposal["base_revision_id"]:
            raise FukamiruError(
                "The document changed after rewrite generation; create a new proposal"
            )
        canonical_html = str(proposal["proposed_html"])
        context = json.loads(proposal["context_json"])
        workflow_version = self._workflow_version_from_context(context)
        if workflow_version >= 24:
            if expected_sha256 != str(proposal["proposed_sha256"]):
                raise FukamiruError(
                    "一括承認の対象原稿が更新されています。差分を読み直してください"
                )
            if not acknowledge_unresolved:
                raise FukamiruError(
                    "一括承認には未解決事項を確認した旨の明示が必要です"
                )
        self.validator.assert_valid(canonical_html)
        source_html = RevisionService(self.connection).canonical_html(
            str(proposal["base_revision_id"])
        )
        surface_gate = JapaneseSurfaceQualityGate.guard_canonical_html(source_html, canonical_html)
        canonical_html = surface_gate.canonical_html
        if (
            workflow_version >= 24
            and hashlib.sha256(canonical_html.encode()).hexdigest() != expected_sha256
        ):
            raise FukamiruError(
                "最終表面検査で原稿が変わりました。変更後の案を保存してから再確認してください"
            )
        if workflow_version >= 29:
            footnote_report = dict(context.get("evidence_footnote_render_report") or {})
            current_sha256 = hashlib.sha256(canonical_html.encode()).hexdigest()
            if (
                footnote_report.get("policy") != "evidence-footnotes-v1"
                or footnote_report.get("target_html_sha256") != current_sha256
                or int(footnote_report.get("citation_event_draft_count") or 0)
                != len(
                    [
                        item
                        for item in context.get("citation_event_drafts") or []
                        if isinstance(item, dict)
                    ]
                )
            ):
                raise FukamiruError(
                    "V29の最終Revision承認を停止しました。脚注生成記録が最終HTMLと一致しません"
                )
        if workflow_version >= 24:
            structure_validation = dict(context.get("structure_plan_validation") or {})
            paragraph_validation = dict(context.get("paragraph_plan_validation") or {})
            boundary_lock = dict(context.get("paragraph_boundary_lock") or {})
            approval_lock = dict(context.get("approval_structure_lock") or {})
            if not (
                (
                    structure_validation.get("valid") is True
                    or structure_validation.get("valid_for_conditional_execution") is True
                )
                and paragraph_validation.get("valid") is True
                and paragraph_validation.get("global_valid") is True
                and approval_lock.get("lock_status") == "locked"
                and context.get("approval_section_structure")
                == self._section_structure_signature(canonical_html)
                and ManuscriptDiscourseService.validate_rendered_boundary_lock(
                    canonical_html, approval_lock
                )["valid"]
                and (
                    not boundary_lock
                    or ManuscriptDiscourseService.validate_rendered_boundary_lock(
                        canonical_html, boundary_lock
                    )["valid"]
                )
            ):
                raise FukamiruError(
                    "V24の一括承認を停止しました。構造・段落境界の契約を再検証できません"
                )
        reference_entries = context.get("reference_entries") or []
        machine_reference_entries = context.get("machine_reference_entries") or []
        reference_format_issues = self._reference_format_issues(
            canonical_html,
            reference_entries,
            workflow_version=workflow_version,
            machine_entries=machine_reference_entries,
        )
        if 22 <= workflow_version < 24:
            response_contract_conditions = [
                item
                for item in context.get("conditional_conditions") or []
                if isinstance(item, dict)
                and item.get("reason")
                in {"response_contract_incomplete", "semantic_resolution_required"}
            ]
            if (
                dict(context.get("conditional_success") or {}).get("reason")
                == "response_contract_incomplete"
                and not response_contract_conditions
            ):
                response_contract_conditions.append(
                    dict(context.get("conditional_success") or {})
                )
            if response_contract_conditions:
                phases = ", ".join(
                    sorted(
                        {
                            str(item.get("phase") or "unknown")
                            for item in response_contract_conditions
                        }
                    )
                )
                semantic_pending = any(
                    item.get("reason") == "semantic_resolution_required"
                    for item in response_contract_conditions
                )
                raise FukamiruError(
                    (
                        "V22の最終Revision承認を停止しました。"
                        "主張・根拠関係の意味が未確定で、人間の判断が必要です: "
                        if semantic_pending
                        else "V22の最終Revision承認を停止しました。必須推論の部分応答が"
                        "1回の欠落補充後も未完了です: "
                    )
                    + phases
                )
            blocking_language_requirements = [
                item
                for item in context.get("language_review_requirements") or []
                if isinstance(item, dict)
                and self._language_requirement_final_blocked_v22(item)
            ]
            if blocking_language_requirements:
                classes = "、".join(
                    sorted(
                        {
                            str(item.get("review_class") or "unclassified_analysis_required")
                            for item in blocking_language_requirements
                        }
                    )
                )
                raise FukamiruError(
                    "V22の最終Revision承認を停止しました。"
                    "人間の判断が必要、または原文維持で完了扱いにできない"
                    "必須修正が未達の言語・意味項目があります: "
                    + classes
                )
            live_report = self._reference_quality_report(
                canonical_html,
                reference_entries,
                workflow_version=workflow_version,
                machine_entries=machine_reference_entries,
            )
            approval_context = {
                **context,
                "reference_quality_gate": live_report
                if live_report.get("passed") is not True
                else context.get("reference_quality_gate"),
            }
            blockers = self._v22_reference_context_blockers(approval_context)
            if blockers:
                raise FukamiruError(
                    "V22の最終Revision承認を停止しました。Proposalはpendingのままです: "
                    + self._reference_blocker_summary(blockers)
                )
        if reference_format_issues:
            raise FukamiruError(
                "参考文献の必須書式検査が未合格です。Proposalはpendingのままです。"
                if workflow_version >= 22
                else "Mandatory bibliography formatting changed before approval"
            )
        if workflow_version >= 24:
            completion_gate = dict(context.get("completion_gate") or {})
            conditional = dict(context.get("conditional_success") or {})
            pending_reasons = list(
                completion_gate.get("grouped_blocking_reasons")
                or completion_gate.get("blocking_reasons") or []
            )
            unresolved_present = bool(
                str(proposal["quality_status"]) == "needs_human_review"
                or conditional
                or pending_reasons
                or context.get("conditional_conditions")
                or context.get("language_review_requirements")
                or dict(context.get("reference_approval") or {}).get("blocking_reasons")
            )
            context["bulk_approval"] = {
                "status": "approved_with_unresolved" if unresolved_present else "approved",
                "approved_candidate_sha256": expected_sha256,
                "acknowledged_unresolved": True,
                "approved_at": utc_now(),
                "conditional_reason": conditional.get("reason"),
                "blocking_reasons": pending_reasons,
            }
        validation = self._validation(canonical_html, surface_gate=surface_gate)
        result = RevisionService(self.connection).commit_revision(
            document_id=str(proposal["document_id"]),
            canonical_html=canonical_html,
            editor_type="human",
            operation="human_edit",
            message=f"Approve manuscript rewrite proposal {proposal_id}",
            parent_revision_id=str(proposal["base_revision_id"]),
            citation_events=[
                dict(item)
                for item in context.get("citation_event_drafts") or []
                if isinstance(item, dict)
            ],
        )
        project = self.connection.execute(
            "SELECT root_path FROM projects WHERE id = ?", (proposal["project_id"],)
        ).fetchone()
        assert project is not None
        project_root = Path(str(project["root_path"])).resolve()
        artifact = (
            project_root / "documents" / str(proposal["document_id"]) / f"{result.revision_id}.html"
        )
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(canonical_html, encoding="utf-8")
        status = "confirmed"
        checkpoint_sha: str | None = None
        error_text: str | None = None
        try:
            checkpoint = GitCheckpointService(project_root).checkpoint(
                [artifact], f"Approve manuscript rewrite {result.revision_id}"
            )
            checkpoint_sha = checkpoint.commit_sha if checkpoint else None
        except GitCheckpointError as exc:
            status = "checkpoint_failed"
            error_text = str(exc)
        self.connection.execute(
            """
            UPDATE manuscript_rewrite_proposals
            SET proposed_html = ?, proposed_sha256 = ?, validation_json = ?,
                context_json = ?,
                status = ?, confirmed_revision_id = ?, artifact_path = ?,
                checkpoint_sha = ?, error_text = ?, resolved_at = ?
            WHERE id = ?
            """,
            (
                canonical_html,
                hashlib.sha256(canonical_html.encode()).hexdigest(),
                json.dumps(validation, ensure_ascii=False),
                json.dumps(context, ensure_ascii=False),
                status,
                result.revision_id,
                str(artifact),
                checkpoint_sha,
                error_text,
                utc_now(),
                proposal_id,
            ),
        )
        self.connection.commit()
        return self.get(proposal_id)

    def get(self, proposal_id: str) -> dict[str, Any]:
        result = dict(self._row(proposal_id))
        lifecycle_status = str(result["status"])
        result["lifecycle_status"] = lifecycle_status
        if (
            lifecycle_status == "pending"
            and str(result.get("quality_status") or "ready") == "needs_human_review"
        ):
            result["status"] = "needs_human_review"
        result["use_rag"] = bool(result["use_rag"])
        artifact_path = str(result.get("artifact_path") or "").strip()
        result["output_folder_path"] = str(Path(artifact_path).parent) if artifact_path else None
        raw_context = str(result.pop("context_json"))
        result["semantic_review_context_sha256"] = hashlib.sha256(
            raw_context.encode()
        ).hexdigest()
        result["context"] = json.loads(raw_context)
        context = result["context"]
        if self._workflow_version_from_context(context) >= 22:
            context["language_review_requirements"] = [
                {
                    **item,
                    "final_revision_blocked": self._language_requirement_final_blocked_v22(
                        item
                    ),
                }
                if isinstance(item, dict)
                else item
                for item in context.get("language_review_requirements") or []
            ]
            context["semantic_review_items"] = self._semantic_review_items_v22(context)
            references = [
                dict(item)
                for item in context.get("reference_entries") or []
                if isinstance(item, dict)
            ]
            sections = JapaneseReferenceService.bibliography_sections_v22(
                references,
                machine_entries=[
                    dict(item)
                    for item in context.get("machine_reference_entries") or []
                    if isinstance(item, dict)
                ],
                workflow_version=self._workflow_version_from_context(context),
            )
            machine_entries = next(
                (
                    list(section.get("entries") or [])
                    for section in sections
                    if section.get("section_key") == "machine_detected_references"
                ),
                [],
            )
            self_or_unpublished_items = [
                {**item, "confirmation_kind": "self_or_unpublished"}
                for item in references
                if item.get("final_revision_blocked") is True
                and (
                    item.get("resolution_status") == "user_input_required"
                    or "user_required_metadata" in item
                    or item.get("is_self_authored") is True
                    or item.get("is_unpublished") is True
                )
            ]
            classified_ids = {
                str(item.get("reference_id") or "")
                for item in self_or_unpublished_items
            }
            manual_consulted_manifestation_items = [
                {
                    **item,
                    "confirmation_kind": "consulted_manifestation",
                    "manual_manifestation_entry": True,
                    "parent_reference_id": item.get("reference_id"),
                }
                for item in references
                if item.get("final_revision_blocked") is True
                and item.get("manual_consulted_manifestation_allowed") is True
                and str(item.get("reference_id") or "") not in classified_ids
                and str(item.get("resolution_status") or "")
                in {"specified_scope_unresolved", "ambiguous"}
            ]
            manual_consulted_ids = {
                str(item.get("reference_id") or "")
                for item in manual_consulted_manifestation_items
            }
            unresolved_ordinary_items = [
                {**item, "confirmation_kind": "unresolved_reference"}
                for item in references
                if item.get("final_revision_blocked") is True
                and str(item.get("reference_id") or "") not in classified_ids
                and str(item.get("reference_id") or "") not in manual_consulted_ids
                and str(item.get("resolution_status") or "")
                in {"not_found", "low_match", "unresolved"}
            ]
            excluded_ordinary_ids = {
                *classified_ids,
                *{
                    str(item.get("reference_id") or "")
                    for item in unresolved_ordinary_items
                },
                *manual_consulted_ids,
            }
            consulted_manifestation_items: list[dict[str, Any]] = []
            for parent in references:
                expanded = parent.get("expanded_reference_entries") or []
                if expanded:
                    for raw_child in expanded:
                        if not isinstance(raw_child, dict):
                            continue
                        child = (
                            JapaneseReferenceService.refresh_consulted_manifestation_v22(
                                raw_child
                            )
                        )
                        if child.get("final_revision_blocked") is not True:
                            continue
                        consulted_manifestation_items.append(
                            {
                                **child,
                                "confirmation_kind": "consulted_manifestation",
                                "parent_reference_id": parent.get("reference_id"),
                                "parent_title": parent.get("title"),
                            }
                        )
                    continue
                reference_id = str(parent.get("reference_id") or "")
                if (
                    parent.get("final_revision_blocked") is True
                    and reference_id not in excluded_ordinary_ids
                    and str(parent.get("consulted_status") or "")
                    in {"user_confirmed", "support_audit_selected"}
                    and str(parent.get("resolution_status") or "")
                    in {"metadata_incomplete", "manifestation_confirmed", "resolved"}
                ):
                    consulted_manifestation_items.append(
                        {
                            **JapaneseReferenceService.refresh_consulted_manifestation_v22(
                                parent
                            ),
                            "confirmation_kind": "consulted_manifestation",
                            "parent_reference_id": reference_id,
                        }
                    )
            context["reference_metadata_confirmation_items"] = [
                {**item, "confirmation_kind": "machine_detected"}
                for item in machine_entries
                if item.get("final_revision_blocked") is True
            ] + [
                *manual_consulted_manifestation_items,
                *consulted_manifestation_items,
                *self_or_unpublished_items,
                *unresolved_ordinary_items,
            ]
        result["validation"] = json.loads(result.pop("validation_json"))
        result["edit_summary"] = json.loads(result.pop("edit_summary_json"))
        result["unified_diff"] = self._diff(
            str(result["base_revision_id"]), str(result["proposed_html"])
        )
        return result

    def get_by_run(self, run_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT id FROM manuscript_rewrite_proposals WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise FukamiruError(f"Rewrite proposal is not ready for Run: {run_id}")
        return self.get(str(row["id"]))

    def _row(self, proposal_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM manuscript_rewrite_proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
        if row is None:
            raise FukamiruError(f"Rewrite proposal not found: {proposal_id}")
        return row

    def _diff(self, base_revision_id: str, proposed_html: str) -> str:
        base_html = RevisionService(self.connection).canonical_html(base_revision_id)
        return "".join(
            difflib.unified_diff(
                base_html.splitlines(keepends=True),
                proposed_html.splitlines(keepends=True),
                fromfile=base_revision_id,
                tofile="rewrite-proposal",
            )
        )

    def _outline_context(
        self,
        *,
        document_id: str,
        project_id: str,
        document_kind: str,
        outline_node_id: str | None,
    ) -> dict[str, Any]:
        target = outline_node_id
        dissertation_id: str | None = document_id if document_kind == "dissertation" else None
        if target:
            row = self.connection.execute(
                """
                SELECT n.dissertation_document_id
                FROM dissertation_nodes n
                JOIN content_documents d ON d.id = n.dissertation_document_id
                WHERE n.id = ? AND d.project_id = ?
                """,
                (target, project_id),
            ).fetchone()
            if row is None:
                raise FukamiruError("Outline node does not belong to this Project")
            dissertation_id = str(row["dissertation_document_id"])
        elif document_kind != "dissertation":
            row = self.connection.execute(
                """
                SELECT dissertation_document_id, outline_node_id
                FROM dissertation_memberships
                WHERE content_document_id = ? AND status != 'detached'
                ORDER BY updated_at DESC LIMIT 1
                """,
                (document_id,),
            ).fetchone()
            if row is not None:
                dissertation_id = str(row["dissertation_document_id"])
                target = str(row["outline_node_id"])
        if not dissertation_id:
            return {
                "dissertation_document_id": None,
                "target_outline_node_id": None,
                "nodes": [],
            }
        rows = self.connection.execute(
            """
            SELECT n.id, n.parent_id, n.node_type, n.title, n.order_index,
                   d.summary, d.instructions, d.target_characters
            FROM dissertation_nodes n
            LEFT JOIN dissertation_outline_directives d ON d.outline_node_id = n.id
            WHERE n.dissertation_document_id = ?
            ORDER BY n.parent_id, n.order_index
            LIMIT 200
            """,
            (dissertation_id,),
        ).fetchall()
        return {
            "dissertation_document_id": dissertation_id,
            "target_outline_node_id": target,
            "nodes": [dict(row) for row in rows],
        }

    def _validation(
        self,
        canonical_html: str,
        *,
        surface_gate: JapaneseSurfaceGateResult | None = None,
    ) -> dict[str, Any]:
        report = self.validator.validate(canonical_html)
        result: dict[str, Any] = {
            "valid": report.valid,
            "element_count": report.element_count,
            "block_count": report.block_count,
            "issues": [
                {
                    "code": issue.code,
                    "message": issue.message,
                    "severity": issue.severity,
                    "element_id": issue.element_id,
                }
                for issue in report.issues
            ],
        }
        if surface_gate is not None:
            result["japanese_surface_quality_gate"] = surface_gate.as_dict()
        return result
