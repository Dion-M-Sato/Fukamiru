from __future__ import annotations

import hashlib
import json
import re
import tempfile
import threading
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pypdf import PdfWriter
from pypdf.generic import NameObject, TextStringObject

from fukamiru.authoring.evidence_contracts import build_fixed_assets
from fukamiru.authoring.research import (
    build_atomic_evidence_obligations,
    build_claim_proposition_bindings,
    classify_proposition_verification_scopes,
)
from fukamiru.bootstrap import bootstrap
from fukamiru.config import load_settings
from fukamiru.errors import FukamiruError, NonRetryableFukamiruError
from fukamiru.knowledge import CorpusSyncService, KnowledgeSearchService
from fukamiru.ocr import OCRRevisionRepository
from fukamiru.projects.service import ProjectService
from fukamiru.revisions import (
    ManuscriptCoherenceService,
    ManuscriptDiscourseService,
    ManuscriptRewriteService,
    RevisionService,
)
from fukamiru.revisions.references import JapaneseReferenceService
from fukamiru.storage.database import DatabasePair
from fukamiru.storage.runs import RunRepository, utc_now
from fukamiru.worker import WorkerRunner
from fukamiru.worker.runner import ClaimedNode, HandlerResult
from fukamiru.workflow.graph import (
    EdgeSpec,
    NodeSpec,
    RetryPolicy,
    WorkflowDefinition,
)
from fukamiru.workflow.templates import (
    authoring_workflow,
    corpus_sync_workflow,
    ingestion_workflow,
    manuscript_rewrite_workflow_v21,
    manuscript_rewrite_workflow_v22,
    manuscript_rewrite_workflow_v23,
    translation_workflow,
)


def coherence_audit_fixture(
    blocks: list[dict[str, object]], document_manifest: dict[str, object]
) -> dict[str, object]:
    """Return the complete V22 document/global coherence contract."""

    return {
        "document_assertions": WorkerRunner._coherence_manifest_assertions(
            document_manifest
        ),
        "checked_block_ids": [str(block["id"]) for block in blocks],
        "coverage": [
            {"category": category, "checked": True, "note": "Fixture audit."}
            for category in (
                "temporal_conflict",
                "connector_mismatch",
                "orphan_example",
                "missing_warrant",
                "argument_gap",
                "section_transition",
                "duplication",
                "ordering",
            )
        ],
    }


class FakeTranslator:
    def translate(self, source_text: str, **_: str) -> str:
        return "翻訳結果です。\n\n" + source_text.replace("Bonjour", "こんにちは")

    def plan_manuscript_language(self, *, blocks, **_):
        return {
            "records": [
                {
                    "block_id": str(block["id"]),
                    "kind": "block_review",
                    "status": "no_revision_needed",
                    "explanation": "The test fixture found no discourse revision requirement.",
                }
                for block in blocks
            ]
        }

    def resolve_japanese_ambiguity(self, **_):
        return {
            "status": "resolved",
            "explanation": "The test fixture resolved the local grammatical roles.",
            "roles": ["predicate_scope_preserved"],
        }

    def audit_manuscript_records(self, *, phase, units, **_):
        """Satisfy the mandatory V22 read-only inference contract in graph tests."""

        status = (
            "established"
            if phase == "certainty"
            else "still_open"
            if phase == "final_findings"
            else "insufficient"
        )
        return {
            "verdicts": [
                {
                    "unit_id": str(unit["unit_id"]),
                    "status": status,
                    "rationale": "The test fixture completed the mandatory read-only audit.",
                    "citations": [],
                }
                for unit in units
            ]
        }

    def plan_manuscript_search_queries(self, *, units, **_):
        return {
            "plans": [
                {
                    "unit_id": str(unit["unit_id"]),
                    "subclaims": [str(unit.get("query") or unit["unit_id"])],
                    "source_kind": "mixed",
                    "variants": [str(unit.get("query") or unit["unit_id"])],
                }
                for unit in units
            ]
        }

    def resolve_manuscript_enumeration_context(self, *, candidates, **_):
        return {
            "contracts": [
                {
                    "candidate_id": str(candidate["candidate_id"]),
                    "block_id": str(candidate["block_id"]),
                    "action": "preserve",
                    "item_roles": [
                        {"item": str(item), "role": "source_item"}
                        for item in candidate.get("items", [])
                    ],
                }
                for candidate in candidates
            ]
        }

    def rewrite_manuscript_blocks(
        self,
        *,
        blocks: list[dict[str, object]],
        reasoning_review: list[dict[str, object]],
        **_: object,
    ) -> dict[str, object]:
        target = next(
            block for block in blocks if block["tag"] == "p" and not block["inline_protected"]
        )
        result = {
            "summary": "Clarified the working argument.",
            "edits": [
                {
                    "operation": "replace_text",
                    "block_id": target["id"],
                    "text": str(target["text"]) + " Revised with the requested focus.",
                    "review_finding_ids": [
                        str(finding["id"])
                        for review in reasoning_review
                        for finding in review.get("findings", [])
                        if isinstance(finding, dict)
                        and str(target["id"]) in set(map(str, finding.get("block_ids", [])))
                    ],
                    "evidence_ids": [],
                }
            ],
            "unresolved_questions": [],
        }
        if int(_.get("workflow_version", 21)) >= 22:
            result["checked_block_ids"] = [str(block["id"]) for block in blocks]
        return result

    def review_manuscript_blocks(
        self, *, blocks: list[dict[str, object]], **_: object
    ) -> dict[str, object]:
        target = next(block for block in blocks if block["tag"] == "p")
        return {
            "assessment": "The working claim needs a more explicit logical relation.",
            "checked_block_ids": [str(block["id"]) for block in blocks],
            "findings": [
                {
                    "id": "logic-1",
                    "category": "logic",
                    "severity": "major",
                    "block_ids": [target["id"]],
                    "explanation": "The claim is underspecified.",
                    "recommendation": "State the relation explicitly.",
                    "requires_research": False,
                    "research_queries": [],
                }
            ],
            "rewrite_priorities": ["Clarify the central relation."],
            "research_queries": [],
        }

    def plan_manuscript_structure(
        self, *, semantic_structure: dict[str, object], **_: object
    ) -> dict[str, object]:
        source_sections = semantic_structure.get("sections") or []
        source_blocks = semantic_structure.get("blocks") or []
        assert isinstance(source_sections, list)
        assert isinstance(source_blocks, list)
        blocks = {str(block["id"]): block for block in source_blocks if isinstance(block, dict)}
        sections: list[dict[str, object]] = []
        roles: list[dict[str, object]] = []
        links: list[dict[str, object]] = []
        temporal: list[dict[str, object]] = []
        connectives: list[dict[str, object]] = []
        for section in source_sections:
            if not isinstance(section, dict) or not section.get("plannable"):
                continue
            block_ids = [str(item) for item in section.get("block_ids", [])]
            prose_ids = [str(item) for item in section.get("prose_block_ids", [])]
            claim_id = prose_ids[0]
            sections.append(
                {
                    "section_id": str(section["section_id"]),
                    "title": str(section["title"]),
                    "central_claim": "The section's first prose block states its claim.",
                    "central_claim_block_id": claim_id,
                    "ordering_policy": "claim_evidence",
                    "ordered_block_ids": block_ids,
                    "transition_needed": False,
                    "transition_from_previous": None,
                    "transition_reason": "The source order is already coherent.",
                }
            )
            for block_id in block_ids:
                block = blocks[block_id]
                if block.get("is_heading"):
                    role = "heading"
                    parent = None
                elif block_id == claim_id:
                    role = "claim"
                    parent = None
                elif block.get("example_candidate"):
                    role = "example"
                    parent = claim_id
                else:
                    role = "premise"
                    parent = claim_id
                roles.append(
                    {
                        "block_id": block_id,
                        "role": role,
                        "parent_claim_block_id": parent,
                        "relation_to_previous": ("none" if block_id == block_ids[0] else "support"),
                    }
                )
                if block.get("example_candidate"):
                    links.append(
                        {
                            "source_block_id": block_id,
                            "target_claim_block_id": claim_id,
                            "relation": "exemplifies",
                            "observed_property": "A concrete feature named in the block.",
                            "warrant": "The feature illustrates the section claim.",
                            "evidence_required": False,
                            "research_query": None,
                        }
                    )
                years = block.get("years") or []
                if years:
                    temporal.append(
                        {
                            "block_id": block_id,
                            "start_year": min(int(year) for year in years),
                            "end_year": max(int(year) for year in years),
                            "temporal_kind": "historical_context",
                            "relation_to_previous": "background",
                        }
                    )
                connective = block.get("leading_connective")
                if connective:
                    position = block_ids.index(block_id)
                    target_id = block_ids[position - 1] if position else claim_id
                    connectives.append(
                        {
                            "block_id": block_id,
                            "connective": str(connective),
                            "relation": ("qualification" if connective == "ただし" else "contrast"),
                            "target_block_id": target_id,
                            "explanation": "The connective attaches to the prior claim.",
                        }
                    )
        return {
            "document_thesis": "Fixture thesis.",
            "global_ordering_policy": "claim_evidence",
            "introduction_contract": {
                "section_id": sections[0]["section_id"] if sections else None,
                "genre_profile": "fixture scholarly manuscript",
                "moves": [
                    {
                        "move": "positive_purpose_or_thesis",
                        "block_ids": (
                            [str(sections[0]["central_claim_block_id"])] if sections else []
                        ),
                        "required": True,
                        "purpose": "State the fixture thesis positively.",
                    }
                ],
                "positive_purpose_block_id": (
                    str(sections[0]["central_claim_block_id"]) if sections else None
                ),
                "negative_scope_records": [],
            },
            "sections": sections,
            "block_roles": roles,
            "argument_links": links,
            "temporal_constraints": temporal,
            "connective_relations": connectives,
            "merge_groups": [],
            "research_queries": [],
            "unresolved_questions": [],
        }

    def plan_semantic_paragraphs(
        self, *, discourse_structure: dict[str, object], **_: object
    ) -> dict[str, object]:
        return ManuscriptDiscourseService.default_boundary_analysis(discourse_structure)

    def resolve_manuscript_predicate_relations(
        self, *, candidates: list[dict[str, object]], **_: object
    ) -> dict[str, object]:
        def grounded_roles(candidate: dict[str, object]) -> dict[str, object]:
            context = " ".join(
                str(candidate.get(field) or "")
                for field in ("source_text", "previous_text", "next_text", "section_claim")
            )
            anchors = list(
                dict.fromkeys(
                    re.findall(
                        r"[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ'’-]{2,}|"
                        r"[一-龯々〆ヵヶァ-ヴー]{2,}",
                        context,
                    )
                )
            )
            if not anchors:
                anchors = [str(candidate["source_text"]).strip("。！？ ")]
            return {
                "subject": anchors[0],
                "affected_entity": (
                    anchors[1]
                    if str(candidate.get("risk_tier")) == "underspecified_relation"
                    and len(anchors) > 1
                    else None
                ),
                "initial_state": None,
                "target_state": None,
                "mediator": None,
                "mechanism": "A contextually literal or already explicit relation",
            }

        return {
            "summary": "Fixture preserves generic predicates.",
            "decisions": [
                {
                    "candidate_id": candidate["candidate_id"],
                    "block_id": candidate["block_id"],
                    "action": "preserve",
                    "relation_type": (
                        "association"
                        if str(candidate.get("risk_tier")) == "underspecified_relation"
                        and float(candidate.get("abstract_likelihood") or 0) > 0
                        else "literal_connection"
                    ),
                    "semantic_roles": grounded_roles(candidate),
                    "replacement_text": "",
                    "reason": "Fixture decision.",
                    "confidence": 1.0,
                    "meaning_preserved": True,
                    "modality_preserved": True,
                    "citation_preserved": True,
                    "review_finding_ids": [],
                    "evidence_ids": [],
                }
                for candidate in candidates
            ],
        }

    def validate_manuscript_coherence(
        self,
        *,
        blocks: list[dict[str, object]],
        document_manifest: dict[str, object],
        **_: object,
    ) -> dict[str, object]:
        return {
            **coherence_audit_fixture(blocks, document_manifest),
            "passed": True,
            "summary": "The fixture manuscript is coherent.",
            "issues": [],
            "repair_edits": [],
            "section_moves": [],
        }


class InvalidParagraphPlannerTranslator(FakeTranslator):
    def __init__(self) -> None:
        self.paragraph_plan_calls = 0

    def plan_semantic_paragraphs(self, **_: object) -> dict[str, object]:
        self.paragraph_plan_calls += 1
        return {
            "policy": "invalid-fixture",
            "unit_annotations": [],
            "boundary_decisions": [],
        }


class CoherenceVisibilityTranslator(FakeTranslator):
    def __init__(self) -> None:
        self.coherence_inputs: list[dict[str, object]] = []

    def validate_manuscript_coherence(
        self,
        *,
        blocks: list[dict[str, object]],
        document_manifest: dict[str, object],
        editable_block_ids: list[str],
        stage: str,
        **_: object,
    ) -> dict[str, object]:
        self.coherence_inputs.append(
            {
                "stage": stage,
                "block_ids": [str(item["id"]) for item in blocks],
                "document_manifest": document_manifest,
                "editable_block_ids": editable_block_ids,
            }
        )
        return super().validate_manuscript_coherence(
            blocks=blocks, document_manifest=document_manifest
        )


class StagedRepairTranslator(FakeTranslator):
    def __init__(self) -> None:
        self.stage_calls: list[dict[str, object]] = []
        self.argument_calls = 0

    def plan_manuscript_structure_stage(
        self,
        *,
        stage: str,
        semantic_structure: dict[str, object],
        repair_targets: dict[str, object],
        **_: object,
    ) -> dict[str, object]:
        self.stage_calls.append({"stage": stage, "repair_targets": dict(repair_targets)})
        complete = super().plan_manuscript_structure(semantic_structure=semantic_structure)
        if stage == "skeleton":
            return {
                key: complete[key]
                for key in (
                    "document_thesis",
                    "global_ordering_policy",
                    "introduction_contract",
                    "sections",
                )
            }
        if stage == "argument":
            self.argument_calls += 1
            roles = list(complete["block_roles"])
            if self.argument_calls == 1:
                roles = roles[:-1]
            else:
                # The repair deliberately emits an invalid change outside its target.
                # The runner must discard it and preserve the validated heading record.
                roles[0] = {**roles[0], "role": "claim"}
            sections = {
                str(item["section_id"]): item
                for item in complete["sections"]
                if isinstance(item, dict)
            }
            classifications: list[dict[str, object]] = []
            links: list[dict[str, object]] = []
            for block in semantic_structure.get("blocks", []):
                if not isinstance(block, dict):
                    continue
                block_id = str(block["id"])
                section = sections.get(str(block["section_id"])) or {}
                for reference in block.get("reference_candidates", []):
                    reference_id = str(reference["reference_id"])
                    kind = str(reference["preliminary_kind"])
                    classifications.append(
                        {
                            "reference_id": reference_id,
                            "block_id": block_id,
                            "kind": kind,
                            "rationale": "Fixture classification.",
                        }
                    )
                    if kind == "work_example":
                        links.append(
                            {
                                "source_block_id": block_id,
                                "source_reference_id": reference_id,
                                "target_claim_block_id": section.get("central_claim_block_id"),
                                "target_claim_anchor": section.get("central_claim"),
                                "relation": "exemplifies",
                                "observed_property": "Fixture observation.",
                                "warrant": "Fixture warrant.",
                                "evidence_required": False,
                                "research_query": None,
                            }
                        )
            return {
                "block_roles": roles,
                "reference_classifications": classifications,
                "argument_links": links,
                "research_queries": [],
                "unresolved_questions": [],
            }
        if stage == "temporal":
            mentions: list[dict[str, object]] = []
            scopes: list[dict[str, object]] = []
            for block in semantic_structure.get("blocks", []):
                if not isinstance(block, dict):
                    continue
                block_mentions = list(block.get("temporal_mentions", []))
                for mention in block_mentions:
                    mentions.append(
                        {
                            "mention_id": mention["mention_id"],
                            "block_id": block["id"],
                            "temporal_kind": "historical_context",
                        }
                    )
                if block_mentions:
                    scopes.append(
                        {
                            "block_id": block["id"],
                            "start_year": min(int(item["start_year"]) for item in block_mentions),
                            "end_year": max(int(item["end_year"]) for item in block_mentions),
                            "temporal_kind": "historical_context",
                            "relation_to_previous": "background",
                        }
                    )
            return {
                "temporal_mentions": mentions,
                "block_temporal_scopes": scopes,
            }
        if stage == "discourse":
            return {key: complete[key] for key in ("connective_relations", "merge_groups")}
        raise AssertionError(stage)


class SplitArgumentTranslator(StagedRepairTranslator):
    def __init__(self) -> None:
        super().__init__()
        # The inherited fixture's first argument response is intentionally invalid.
        # This fixture instead starts from its complete response.
        self.argument_calls = 1
        self.argument_process_calls: list[dict[str, object]] = []

    def plan_manuscript_argument_process(
        self,
        *,
        process_name: str,
        semantic_structure: dict[str, object],
        repair_targets: dict[str, object],
        workflow_version: int = 21,
        timeout_grace_callback: object = None,
        **_: object,
    ) -> dict[str, object]:
        del timeout_grace_callback
        self.argument_process_calls.append(
            {"process_name": process_name, "repair_targets": dict(repair_targets)}
        )
        if process_name == "block_roles":
            whole_plan = FakeTranslator.plan_manuscript_structure(
                self, semantic_structure=semantic_structure
            )
            complete = {"block_roles": whole_plan["block_roles"]}
        else:
            complete = super().plan_manuscript_structure_stage(
                stage="argument",
                semantic_structure=semantic_structure,
                repair_targets=repair_targets,
            )
        fields = {
            "block_roles": ("block_roles",),
            "reference_classification": ("reference_classifications",),
            "claim_evidence_links": (
                "argument_links",
                "research_queries",
                "unresolved_questions",
            ),
        }[process_name]
        result = {field: complete[field] for field in fields}
        if workflow_version >= 22 and process_name == "claim_evidence_links":
            links = [
                item for item in result["argument_links"] if isinstance(item, dict)
            ]
            dispositions = []
            for unit in repair_targets.get("link_source_units", []):
                if not isinstance(unit, dict):
                    continue
                block_id = str(unit.get("source_block_id") or "")
                reference_id = str(unit.get("source_reference_id") or "") or None
                linked = any(
                    str(item.get("source_block_id") or "") == block_id
                    and (str(item.get("source_reference_id") or "") or None)
                    == reference_id
                    for item in links
                )
                dispositions.append(
                    {
                        "unit_id": str(unit["unit_id"]),
                        "source_block_id": block_id,
                        "source_reference_id": reference_id,
                        "status": "linked" if linked else "not_applicable",
                        "explanation": (
                            "Fixture emits the matching argument link."
                            if linked
                            else "Fixture finds no argument link necessary."
                        ),
                    }
                )
            result["link_dispositions"] = dispositions
        return result


class ConflictingReferenceLinkTranslator(SplitArgumentTranslator):
    """Emit the exact cross-process contradiction seen in Run 0f1ea40f."""

    def __init__(self) -> None:
        super().__init__()
        self.conflicting_reference_id: str | None = None
        self.conflicting_block_id: str | None = None

    def plan_manuscript_argument_process(
        self,
        *,
        process_name: str,
        semantic_structure: dict[str, object],
        **kwargs: object,
    ) -> dict[str, object]:
        payload = super().plan_manuscript_argument_process(
            process_name=process_name,
            semantic_structure=semantic_structure,
            **kwargs,
        )
        reference_blocks = {
            str(block["id"])
            for block in semantic_structure.get("blocks", [])
            if isinstance(block, dict) and block.get("reference_candidates")
        }
        if process_name == "block_roles":
            payload["block_roles"] = [
                {
                    **role,
                    "role": "premise",
                    "relation_to_previous": "support",
                }
                if isinstance(role, dict) and str(role.get("block_id") or "") in reference_blocks
                else role
                for role in payload["block_roles"]
            ]
        elif process_name == "reference_classification":
            classifications = list(payload["reference_classifications"])
            for classification in classifications:
                if isinstance(classification, dict):
                    classification["kind"] = "comparison_target"
                    classification["rationale"] = "Fixture comparison target."
                    self.conflicting_reference_id = str(classification["reference_id"])
                    self.conflicting_block_id = str(classification["block_id"])
                    break
            payload["reference_classifications"] = classifications
        elif (
            process_name == "claim_evidence_links"
            and self.conflicting_reference_id
            and self.conflicting_block_id
        ):
            current_plan = kwargs["current_plan"]
            assert isinstance(current_plan, dict)
            section = next(
                item
                for item in current_plan["sections"]
                if self.conflicting_block_id in item["ordered_block_ids"]
            )
            links = list(payload["argument_links"])
            links.append(
                {
                    "source_block_id": self.conflicting_block_id,
                    "source_reference_id": self.conflicting_reference_id,
                    "target_claim_block_id": section["central_claim_block_id"],
                    "target_claim_anchor": section["central_claim"],
                    "relation": "exemplifies",
                    "observed_property": "Fixture observation.",
                    "warrant": "Fixture warrant.",
                    "evidence_required": False,
                    "research_query": None,
                }
            )
            payload["argument_links"] = links
        return payload


class CapturingManuscriptTranslator(FakeTranslator):
    def __init__(self) -> None:
        self.review_calls: list[dict[str, object]] = []
        self.rewrite_calls: list[dict[str, object]] = []

    def review_manuscript_blocks(self, **kwargs: object) -> dict[str, object]:
        self.review_calls.append(kwargs)
        blocks = kwargs["blocks"]
        assert isinstance(blocks, list)
        return super().review_manuscript_blocks(blocks=blocks)

    def rewrite_manuscript_blocks(self, **kwargs: object) -> dict[str, object]:
        self.rewrite_calls.append(kwargs)
        blocks = kwargs["blocks"]
        assert isinstance(blocks, list)
        return super().rewrite_manuscript_blocks(**kwargs)


class ParallelManuscriptTranslator(CapturingManuscriptTranslator):
    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self.active_calls = 0
        self.max_active_calls = 0

    def rewrite_manuscript_blocks(self, **kwargs: object) -> dict[str, object]:
        with self._lock:
            self.active_calls += 1
            self.max_active_calls = max(self.max_active_calls, self.active_calls)
        try:
            # Leave enough overlap for all five submitted workers even on a
            # loaded CI/desktop host; the assertion tests pool width, not
            # thread-start latency.
            time.sleep(0.25)
            return super().rewrite_manuscript_blocks(**kwargs)
        finally:
            with self._lock:
                self.active_calls -= 1


class PredicateResolvingTranslator(FakeTranslator):
    def rewrite_manuscript_blocks(self, **_: object) -> dict[str, object]:
        return {"summary": "No structural prose edit.", "edits": [], "unresolved_questions": []}

    def resolve_manuscript_predicate_relations(
        self, *, candidates: list[dict[str, object]], **_: object
    ) -> dict[str, object]:
        return {
            "summary": "Resolve abstract connection as contribution.",
            "decisions": [
                {
                    "candidate_id": candidate["candidate_id"],
                    "block_id": candidate["block_id"],
                    "action": "rewrite",
                    "relation_type": "contribution",
                    "semantic_roles": {
                        "subject": "羞恥と支配",
                        "affected_entity": "傷ついた主体",
                        "initial_state": "傷ついた状態",
                        "target_state": "回復",
                        "mediator": None,
                        "mechanism": "回復への寄与",
                    },
                    "replacement_text": str(candidate["source_text"]).replace(
                        "回復に接続される", "回復に寄与する"
                    ),
                    "reason": "The surrounding argument specifies contribution.",
                    "confidence": 0.95,
                    "meaning_preserved": True,
                    "modality_preserved": True,
                    "citation_preserved": True,
                    "review_finding_ids": [],
                    "evidence_ids": [],
                }
                for candidate in candidates
            ],
        }


class CascadingCoherenceTranslator(FakeTranslator):
    def __init__(self) -> None:
        self.coherence_calls = 0

    def validate_manuscript_coherence(
        self,
        *,
        blocks: list[dict[str, object]],
        document_manifest: dict[str, object],
        **_: object,
    ) -> dict[str, object]:
        self.coherence_calls += 1
        audit = coherence_audit_fixture(blocks, document_manifest)
        if self.coherence_calls >= 3:
            return {
                **audit,
                "passed": True,
                "summary": "All bounded repairs now pass.",
                "issues": [],
                "repair_edits": [],
                "section_moves": [],
            }
        block_id = "P-1" if self.coherence_calls == 1 else "P-2"
        target = next(block for block in blocks if block["id"] == block_id)
        return {
            **audit,
            "passed": False,
            "summary": "One bounded repair remains.",
            "issues": [
                {
                    "id": f"cascade-{self.coherence_calls}",
                    "category": "ordering",
                    "severity": "blocking",
                    "verification_scope": "semantic_content",
                    "block_ids": [block_id],
                    "explanation": "Fixture issue.",
                    "recommendation": "Apply the bounded fixture repair.",
                    "resolution_route": "bounded_rewrite",
                    "research_query": None,
                }
            ],
            "repair_edits": [
                {
                    "operation": "replace_text",
                    "block_id": block_id,
                    "anchor_block_id": None,
                    "text": str(target["text"]) + " 整合済み。",
                    "review_finding_ids": [f"cascade-{self.coherence_calls}"],
                    "evidence_ids": [],
                }
            ],
            "section_moves": [],
        }


class LockedStructureMoveTranslator(FakeTranslator):
    def validate_manuscript_coherence(
        self,
        *,
        blocks: list[dict[str, object]],
        document_manifest: dict[str, object],
        **_: object,
    ) -> dict[str, object]:
        return {
            **coherence_audit_fixture(blocks, document_manifest),
            "passed": False,
            "summary": "The locked paragraph plan would require reordering.",
            "issues": [
                {
                    "id": "locked-structure-1",
                    "category": "ordering",
                    "severity": "blocking",
                    "verification_scope": "semantic_content",
                    "block_ids": ["P-1", "P-2"],
                    "explanation": "The sections are in the wrong order.",
                    "recommendation": "Replan the discourse boundaries.",
                    "resolution_route": "bounded_rewrite",
                    "research_query": None,
                }
            ],
            "repair_edits": [],
            "section_moves": [
                {
                    "operation": "move_section_before",
                    "section_id": "semantic:H-2",
                    "anchor_section_id": "semantic:H-1",
                    "issue_id": "locked-structure-1",
                }
            ],
        }


class RepeatingCoherenceTranslator(FakeTranslator):
    def __init__(self) -> None:
        self.pre_polish_calls = 0
        self.replacement = ""

    def validate_manuscript_coherence(
        self,
        *,
        blocks: list[dict[str, object]],
        document_manifest: dict[str, object],
        stage: str,
        evidence_context: list[dict[str, object]] | None = None,
        **_: object,
    ) -> dict[str, object]:
        if stage == "post_polish":
            return super().validate_manuscript_coherence(
                blocks=blocks, document_manifest=document_manifest
            )
        self.pre_polish_calls += 1
        target = next(block for block in blocks if block["tag"] == "p")
        if not self.replacement:
            self.replacement = str(target["text"]) + " 整合済み。"
        audit = coherence_audit_fixture(blocks, document_manifest)
        return {
            **audit,
            "passed": False,
            "summary": "The same repair is requested again.",
            "issues": [
                {
                    "id": "repeat-transition",
                    "category": "section_transition",
                    "severity": "blocking",
                    "verification_scope": "semantic_content",
                    "block_ids": [target["id"]],
                    "explanation": "Fixture issue.",
                    "recommendation": "Apply the fixture repair.",
                    "resolution_route": "bounded_rewrite",
                    "research_query": None,
                }
            ],
            "repair_edits": [
                {
                    "operation": "replace_text",
                    "block_id": target["id"],
                    "anchor_block_id": None,
                    "text": self.replacement,
                    "review_finding_ids": ["repeat-transition"],
                    "evidence_ids": [],
                }
            ],
            "section_moves": [],
        }


class CheckpointResumeCoherenceTranslator(FakeTranslator):
    def __init__(self) -> None:
        self.pre_polish_calls = 0
        self.replacement = ""

    def validate_manuscript_coherence(
        self,
        *,
        blocks: list[dict[str, object]],
        document_manifest: dict[str, object],
        stage: str,
        **_: object,
    ) -> dict[str, object]:
        if stage == "post_polish":
            return super().validate_manuscript_coherence(
                blocks=blocks, document_manifest=document_manifest
            )
        self.pre_polish_calls += 1
        if self.pre_polish_calls == 2:
            raise RuntimeError("Transient coherence service failure")
        target = next(block for block in blocks if block["tag"] == "p")
        audit = coherence_audit_fixture(blocks, document_manifest)
        if self.pre_polish_calls >= 3:
            assert str(target["text"]) == self.replacement
            return {
                **audit,
                "passed": True,
                "summary": "The checkpointed repair passes after retry.",
                "issues": [],
                "repair_edits": [],
                "section_moves": [],
            }
        self.replacement = str(target["text"]) + " チェックポイント済み。"
        return {
            **audit,
            "passed": False,
            "summary": "One repair is needed before the transient failure.",
            "issues": [
                {
                    "id": "checkpoint-transition",
                    "category": "section_transition",
                    "severity": "blocking",
                    "verification_scope": "semantic_content",
                    "block_ids": [target["id"]],
                    "explanation": "Fixture issue.",
                    "recommendation": "Apply the fixture repair.",
                    "resolution_route": "bounded_rewrite",
                    "research_query": None,
                }
            ],
            "repair_edits": [
                {
                    "operation": "replace_text",
                    "block_id": target["id"],
                    "anchor_block_id": None,
                    "text": self.replacement,
                    "review_finding_ids": ["checkpoint-transition"],
                    "evidence_ids": [],
                }
            ],
            "section_moves": [],
        }


class AdditionalResearchCoherenceTranslator(FakeTranslator):
    def __init__(self) -> None:
        self.pre_polish_calls = 0
        self.replacement = ""

    def validate_manuscript_coherence(
        self,
        *,
        blocks: list[dict[str, object]],
        document_manifest: dict[str, object],
        stage: str,
        evidence_context: list[dict[str, object]] | None = None,
        **_: object,
    ) -> dict[str, object]:
        if stage == "post_polish":
            return super().validate_manuscript_coherence(
                blocks=blocks, document_manifest=document_manifest
            )
        self.pre_polish_calls += 1
        target = next(block for block in blocks if block["tag"] == "p")
        audit = coherence_audit_fixture(blocks, document_manifest)
        if any(str(item.get("id") or "") == "E-NEW" for item in evidence_context or []):
            assert self.replacement in str(target["text"])
            return {
                **audit,
                "passed": True,
                "summary": "The added evidence resolves the warrant.",
                "issues": [],
                "repair_edits": [],
                "section_moves": [],
            }
        if self.pre_polish_calls >= 2:
            assert self.replacement in str(target["text"])
            return {
                **audit,
                "passed": False,
                "summary": "Only an external scholarly warrant remains.",
                "issues": [
                    {
                        "id": "missing-comparison-warrant",
                        "category": "missing_warrant",
                        "severity": "blocking",
                        "verification_scope": "semantic_content",
                        "block_ids": [target["id"]],
                        "explanation": "The comparison lacks reception evidence.",
                        "recommendation": "Research comparable reception records.",
                        "resolution_route": "additional_research",
                        "research_query": "作品比較 読者反応 受容史",
                    }
                ],
                "repair_edits": [],
                "section_moves": [],
            }
        self.replacement = str(target["text"]) + " 条件付き修復済み。"
        return {
            **audit,
            "passed": False,
            "summary": "One repair is safe, but one warrant needs research.",
            "issues": [
                {
                    "id": "bounded-transition",
                    "category": "section_transition",
                    "severity": "blocking",
                    "verification_scope": "semantic_content",
                    "block_ids": [target["id"]],
                    "explanation": "The transition is implicit.",
                    "recommendation": "Make the transition explicit.",
                    "resolution_route": "bounded_rewrite",
                    "research_query": None,
                },
                {
                    "id": "missing-comparison-warrant",
                    "category": "missing_warrant",
                    "severity": "blocking",
                    "verification_scope": "semantic_content",
                    "block_ids": [target["id"]],
                    "explanation": "The comparison lacks reception evidence.",
                    "recommendation": "Research comparable reception records.",
                    "resolution_route": "additional_research",
                    "research_query": "作品比較 読者反応 受容史",
                },
            ],
            "repair_edits": [
                {
                    "operation": "replace_text",
                    "block_id": target["id"],
                    "anchor_block_id": None,
                    "text": self.replacement,
                    "review_finding_ids": ["bounded-transition"],
                    "evidence_ids": [],
                }
            ],
            "section_moves": [],
        }


class RepairThenHumanReviewCoherenceTranslator(FakeTranslator):
    def __init__(self) -> None:
        self.pre_polish_calls = 0

    def validate_manuscript_coherence(
        self,
        *,
        blocks: list[dict[str, object]],
        document_manifest: dict[str, object],
        stage: str,
        **_: object,
    ) -> dict[str, object]:
        if stage == "post_polish":
            return super().validate_manuscript_coherence(
                blocks=blocks, document_manifest=document_manifest
            )
        self.pre_polish_calls += 1
        target = next(block for block in blocks if block["tag"] == "p")
        audit = coherence_audit_fixture(blocks, document_manifest)
        human_issue = {
            "id": "scholarly-choice",
            "category": "ordering",
            "severity": "blocking",
            "verification_scope": "semantic_content",
            "block_ids": [target["id"]],
            "explanation": "Two scholarly framings remain possible.",
            "recommendation": "Ask the author which framing to retain.",
            "resolution_route": "human_review",
            "research_query": None,
        }
        if self.pre_polish_calls > 1:
            return {
                **audit,
                "passed": False,
                "summary": "Only the authorial choice remains.",
                "issues": [human_issue],
                "repair_edits": [],
                "section_moves": [],
            }
        return {
            **audit,
            "passed": False,
            "summary": "A local repair and an authorial choice remain.",
            "issues": [
                {
                    "id": "local-transition",
                    "category": "section_transition",
                    "severity": "blocking",
                    "verification_scope": "semantic_content",
                    "block_ids": [target["id"]],
                    "explanation": "The forecast names the wrong destination.",
                    "recommendation": "Name the actual destination locally.",
                    "resolution_route": "bounded_rewrite",
                    "research_query": None,
                },
                human_issue,
            ],
            "repair_edits": [
                {
                    "operation": "replace_text",
                    "block_id": target["id"],
                    "anchor_block_id": None,
                    "text": str(target["text"]) + " 接続修正済み。",
                    "review_finding_ids": ["local-transition"],
                    "evidence_ids": [],
                }
            ],
            "section_moves": [],
        }


class LateFinalClaimCoherenceTranslator(FakeTranslator):
    def __init__(self) -> None:
        self.post_polish_calls = 0

    def validate_manuscript_coherence(
        self,
        *,
        blocks: list[dict[str, object]],
        document_manifest: dict[str, object],
        stage: str,
        **_: object,
    ) -> dict[str, object]:
        if stage == "pre_polish":
            return super().validate_manuscript_coherence(
                blocks=blocks, document_manifest=document_manifest
            )
        self.post_polish_calls += 1
        target = next(block for block in blocks if block["tag"] == "p")
        audit = coherence_audit_fixture(blocks, document_manifest)
        if "今後検証されるべき仮説" in str(target["text"]):
            return {
                **audit,
                "passed": True,
                "summary": "The late-discovered claim was safely limited.",
                "issues": [],
                "repair_edits": [],
                "section_moves": [],
            }
        replacement = str(target["text"]).replace(
            "組み替えられてきた。",
            "組み替えられてきたかは、今後検証されるべき仮説である。",
        )
        return {
            **audit,
            "passed": False,
            "summary": "A late-discovered unsupported historical assertion remains.",
            "issues": [
                {
                    "id": "late-claim",
                    "category": "argument_gap",
                    "severity": "blocking",
                    "verification_scope": "semantic_content",
                    "block_ids": [target["id"]],
                    "explanation": "The historical process is asserted as established.",
                    "recommendation": "Limit the process to a research hypothesis.",
                    "resolution_route": "claim_limitation",
                    "research_query": None,
                }
            ],
            "repair_edits": [
                {
                    "operation": "replace_text",
                    "block_id": target["id"],
                    "anchor_block_id": None,
                    "text": replacement,
                    "review_finding_ids": ["late-claim"],
                    "evidence_ids": [],
                }
            ],
            "section_moves": [],
        }


class SurfaceInvalidLateFinalClaimCoherenceTranslator(LateFinalClaimCoherenceTranslator):
    def validate_manuscript_coherence(self, **kwargs: object) -> dict[str, object]:
        report = super().validate_manuscript_coherence(**kwargs)
        if kwargs.get("stage") == "post_polish" and report.get("repair_edits"):
            repair = dict(report["repair_edits"][0])
            repair["text"] = str(repair["text"]) + "考察し。そのうえで、"
            report["repair_edits"] = [repair]
        return report


class FailOnceTranslator(FakeTranslator):
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.failed = False

    def translate(self, source_text: str, **kwargs: str) -> str:
        self.calls.append(source_text)
        if len(self.calls) == 2 and not self.failed:
            self.failed = True
            raise RuntimeError("fixture transient translation failure")
        return super().translate(source_text, **kwargs)


class CapturingTranslator(FakeTranslator):
    def __init__(self) -> None:
        self.calls: list[str] = []

    def translate(self, source_text: str, **kwargs: str) -> str:
        self.calls.append(source_text)
        return super().translate(source_text, **kwargs)


class ComplianceRepairTranslator(FakeTranslator):
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def translate(self, source_text: str, **kwargs: str) -> str:
        self.calls.append({"source": source_text, **kwargs})
        if "chair émotionnelle" not in source_text:
            return super().translate(source_text, **kwargs)
        if "failed mandatory academic compliance" in kwargs.get("instructions", ""):
            return (
                "デロンシャン=ガニョンによれば、倫理的な働きはゲームの情動を"
                "血肉化することで生じる。"
            )
        return (
            "デロンシャン=ガニョンによれば、それどころか、倫理的作用はゲームの"
            "感情的な肉を通じて生じる。"
        )


class OrderPreservingTranslator:
    def translate(self, source_text: str, **_: str) -> str:
        return source_text.replace("Bonjour", "こんにちは")


class FakeLocalLLM:
    configured = True
    model_identity = "fixture-llm-jp"

    def complete_json(self, **kwargs: object) -> dict[str, object]:
        prompt = str(kwargs["user_prompt"])
        source = prompt.split("TEXT:\n", 1)[1]
        return {
            "edited_text": source.replace("document de recherche", "document scientifique"),
            "edits": [
                {
                    "before": "document de recherche",
                    "after": "document scientifique",
                    "reason": "自然なコロケーション",
                    "category": "collocation",
                }
            ],
            "reasons": ["自然なコロケーション"],
            "confidence": 0.99,
        }


class NoopLocalLLM:
    configured = True
    model_identity = "fixture-llm-jp-noop"

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def complete_json(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return {"edits": [], "confidence": 0.99}


class RejectOnceThenNoopLocalLLM(NoopLocalLLM):
    model_identity = "fixture-llm-jp-zero-rejection-loop"

    def __init__(self) -> None:
        super().__init__()
        self.rejected_once = False

    def complete_json(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        prompt = str(kwargs["user_prompt"])
        source = prompt.split("TEXT:\n", 1)[1]
        if not self.rejected_once:
            self.rejected_once = True
            fragment = source[: min(4, len(source))]
            return {
                "edits": [
                    {
                        "before": fragment,
                        "after": fragment,
                        "reason": "invalid no-op fixture",
                        "category": "grammar",
                    }
                ],
                "confidence": 0.99,
            }
        return {"edits": [], "confidence": 0.99}


class RejectFourThenNoopLocalLLM(NoopLocalLLM):
    model_identity = "fixture-llm-jp-multi-yield-zero-rejection-loop"

    def complete_json(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        prompt = str(kwargs["user_prompt"])
        source = prompt.split("TEXT:\n", 1)[1]
        if len(self.calls) <= 4:
            fragment = source[: min(4, len(source))]
            return {
                "edits": [
                    {
                        "before": fragment,
                        "after": fragment,
                        "reason": "invalid repeated no-op fixture",
                        "category": "grammar",
                    }
                ],
                "confidence": 0.99,
            }
        return {"edits": [], "confidence": 0.99}


class AlwaysInvalidEditLocalLLM(NoopLocalLLM):
    model_identity = "fixture-llm-jp-finite-invalid-edit"

    def complete_json(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return {
            "edits": [
                {
                    "before": "__fragment_not_in_source__",
                    "after": "修正",
                    "reason": "invalid source anchor fixture",
                    "category": "grammar",
                }
            ],
            "confidence": 0.99,
        }


class FailOnceChunkLocalLLM:
    configured = True
    model_identity = "fixture-llm-jp-q4"

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.failed = False

    def complete_json(self, **kwargs: object) -> dict[str, object]:
        prompt = str(kwargs["user_prompt"])
        source = prompt.split("TEXT:\n", 1)[1]
        self.calls.append(source)
        if len(self.calls) == 2 and not self.failed:
            self.failed = True
            raise RuntimeError("fixture transient llm-jp chunk failure")
        return {
            "edited_text": source,
            "edits": [],
            "reasons": ["修正不要"],
            "confidence": 0.99,
        }


class ContextOverflowLocalLLM:
    configured = True
    model_identity = "fixture-llm-jp-context-overflow"

    def complete_json(self, **_: object) -> dict[str, object]:
        raise FukamiruError(
            'llm-jp server HTTP 400: {"error":{"message":'
            '"request exceeds the available context size"}}'
        )


class FakeGaperon:
    configured = True
    model_identity = "fixture-gaperon"

    def complete_json(self, **_: object) -> dict[str, object]:
        return {
            "issues": [
                {
                    "excerpt": "document de recherche",
                    "reason": "fixture French diagnostic",
                    "severity": "minor",
                    "suggestion": "document scientifique",
                    "confidence": 0.9,
                }
            ]
        }


class FakeSemanticIndex:
    def __init__(self) -> None:
        self.build_count = 0
        self.results: list[dict[str, object]] = []
        self.active_index_version_id: str | None = None
        self.searched_index_version_ids: list[str | None] = []

    def available(self) -> bool:
        return True

    def build(self, progress: object = None) -> dict[str, object]:
        self.build_count += 1
        if callable(progress):
            progress({"embedded_count": 1, "total_count": 1})
        return {
            "index_version_id": "fake-index",
            "model": "fixture-embedding",
            "dimensions": 3,
            "chunk_count": 1,
        }

    def status(self) -> dict[str, object]:
        return {
            "index": "ready" if self.active_index_version_id else "not_built",
            "index_version_id": self.active_index_version_id,
        }

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        index_version_id: str | None = None,
    ) -> list[dict[str, object]]:
        del query
        self.searched_index_version_ids.append(index_version_id)
        return self.results[:limit]


class FakeAuthoringAdapter:
    def generate(
        self,
        stage: str,
        payload: dict[str, object],
        context: str = "",
    ) -> dict[str, object]:
        del context
        provenance = {
            "source": "llm_inference",
            "source_ids": [],
            "verification_status": "unverified",
        }
        outputs: dict[str, dict[str, object]] = {
            "decompose_memo": {
                "propositions": [
                    {"id": "p1", "text": "Evidence supports the conclusion.",
                     "source_span": "Evidence supports the conclusion.",
                     "provenance": {**provenance, "source": "memo"}}
                ]
            },
            "classify_propositions": {
                "classifications": [
                    {"proposition_id": "p1", "type": "interpretation", "confidence": 0.9,
                     "provenance": provenance}
                ]
            },
            "review_manuscript_reasoning": {
                "findings": [
                    {
                        "id": "rr1",
                        "finding_type": "reasoning_asset",
                        "severity": "info",
                        "proposition_ids": ["p1"],
                        "description": "The memo states an evidential relation.",
                        "recommended_use": "Preserve it as a qualified argument.",
                        "requires_research": False,
                    }
                ],
                "reasoning_summary": "The memo contains one usable inferential link.",
            },
            "formulate_research_question": {
                "research_question": {"id": "rq1", "text": "結論はなぜ正当化できるのか。",
                                      "scope": "入力されたメモ。", "out_of_scope": [],
                                      "assumptions": [], "provenance": provenance},
                "topic_coverage": [
                    {
                        "topic_id": "t1",
                        "label": "結論の正当化",
                        "proposition_ids": ["p1"],
                        "disposition": "context",
                        "relation_to_question": "問いの素材を示す。",
                    }
                ],
                "required_user_information": [],
            },
            "build_claim_tree": {
                "claims": [{"id": "c1", "level": "main_claim",
                            "text": "The conclusion is justified.", "parent_id": None,
                            "proposition_id": "p1", "answers_question": True,
                            "provenance": provenance}]
            },
            "build_argument_graph": {
                "nodes": [
                    {"id": "c1", "node_type": "claim", "text": "The conclusion is justified.",
                     "claim_id": "c1", "proposition_id": "p1", "source_span_id": None,
                     "provenance": provenance},
                    {"id": "e1", "node_type": "evidence", "text": "Supporting evidence.",
                     "claim_id": "c1", "proposition_id": None, "source_span_id": None,
                     "provenance": provenance},
                    {"id": "w1", "node_type": "warrant", "text": "Evidence warrants claim.",
                     "claim_id": "c1", "proposition_id": None, "source_span_id": None,
                     "provenance": provenance},
                    {"id": "q1", "node_type": "qualifier", "text": "Within this note.",
                     "claim_id": "c1", "proposition_id": None, "source_span_id": None,
                     "provenance": provenance},
                    {"id": "x1", "node_type": "counterexample", "text": "A rival account.",
                     "claim_id": "c1", "proposition_id": None, "source_span_id": None,
                     "provenance": provenance},
                ],
                "edges": [
                    {"id": "ae1", "source_id": "e1", "target_id": "c1",
                     "relation": "supports"},
                    {"id": "aw1", "source_id": "w1", "target_id": "c1",
                     "relation": "warrants"},
                    {"id": "aq1", "source_id": "q1", "target_id": "c1",
                     "relation": "qualifies"},
                    {"id": "ax1", "source_id": "x1", "target_id": "c1",
                     "relation": "challenges"},
                ],
            },
            "generate_hypotheses": {"candidates": []},
            "build_discourse_tree": {
                "nodes": [
                    {"id": "d0", "section_id": "", "relation": "root",
                     "article_role": "root", "discourse_purpose": "統括",
                     "claim_ids": ["c1"],
                     "parent_id": None, "child_ids": ["d1"],
                     "attentional_focus": "research question"},
                    {"id": "d1", "section_id": "s1", "relation": "evidence",
                     "article_role": "conclusion", "discourse_purpose": "結論を示す",
                     "claim_ids": ["c1"],
                     "parent_id": "d0", "child_ids": [],
                     "attentional_focus": "justification"},
                ]
            },
            "plan_manuscript_structure": {
                "sections": [
                    {
                        "section_id": "s1",
                        "article_role": "conclusion",
                        "relation": "evidence",
                        "reader_purpose": "結論と根拠の関係を示す。",
                        "claim_ids": ["c1"],
                        "source_proposition_ids": ["p1"],
                    }
                ]
            },
            "lock_section_contracts": {
                "sections": [{"section_id": "s1", "title": "結論", "local_question": "Why?",
                              "expected_conclusion": "Justified", "claim_ids": ["c1"],
                              "evidence_ids": ["e1"], "warrant_ids": [], "qualifiers": [],
                              "counterarguments": [], "discourse_relation_to_parent": "root",
                              "previous_section_bridge": "", "next_section_bridge": "",
                              "required_terms": [], "forbidden_topics": [], "target_length": 300}]
            },
            "plan_semantic_paragraphs": {
                "sections": [
                    {
                        "section_id": "s1",
                        "paragraphs": [
                            {
                                "move_indices": [index],
                                "rhetorical_moves": [move],
                                "claim_ids": ["c1"],
                                "evidence_ids": ["e1"],
                                "development_instruction": f"Develop {move}.",
                            }
                            for index, move in enumerate(
                                [
                                    "state the question",
                                    "state the claim",
                                    "present evidence",
                                    "explain the warrant",
                                    "state the qualification",
                                ],
                                start=1,
                            )
                        ],
                    }
                ]
            },
            "draft_sections": {
                "sections": [{"section_id": "s1", "text": "証拠により結論は正当化される。",
                              "claim_ids": ["c1"], "evidence_ids": ["e1"],
                              "new_propositions": []}]
            },
            "audit_structure": {"findings": []},
            "polish_prose": {
                "sections": [{"section_id": "s1", "text": "証拠により結論は正当化される。",
                              "changed_claim_ids": []}]
            },
        }
        return outputs[stage]


class WorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.root = root
        config = root / "local.toml"
        config.write_text(
            f"""
[server]
host = "127.0.0.1"
port = 9876
open_browser = false
[paths]
state_db = "{(root / "data/state.db").as_posix()}"
knowledge_db = "{(root / "data/knowledge.db").as_posix()}"
runtime_root = "{(root / "runtime").as_posix()}"
corpus_root = "{(root / "corpus").as_posix()}"
ocr_root = "{(root / "ocr").as_posix()}"
knowledge_artifacts_root = "{(root / "kb").as_posix()}"
faiss_root = "{(root / "faiss").as_posix()}"
[worker]
enabled = false
[retrieval]
tokenizer_mode = "unicode"
[translation]
batch_characters = 1000
academic_research_online = false
""",
            encoding="utf-8",
        )
        self.settings = load_settings(config)
        bootstrap(self.settings)
        self.databases = DatabasePair.from_settings(self.settings)
        definitions = (
            corpus_sync_workflow(),
            ingestion_workflow(),
            manuscript_rewrite_workflow_v22(),
            translation_workflow(),
            authoring_workflow(),
        )
        self.workflows = {item.name: item for item in definitions}
        self.semantic_index = FakeSemanticIndex()
        self.worker = WorkerRunner(
            self.settings,
            self.databases,
            self.workflows,
            translator=FakeTranslator(),
            authoring_adapter=FakeAuthoringAdapter(),
            semantic_index=self.semantic_index,
        )
        self.source = root / "source.txt"
        self.source.write_text(
            "Bonjour. Ceci est un document de recherche.\n\n"
            "La modalité doit être conservée dans la traduction.",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _create_run(
        self,
        workflow: str,
        *,
        project_id: str | None = None,
        instructions: str = "学術的な文体を保つ",
        extra_inputs: dict[str, object] | None = None,
    ) -> str:
        definition = self.workflows[workflow]
        with self.databases.state.connect() as connection:
            result = RunRepository(connection).create(
                workflow_name=definition.name,
                workflow_version=definition.version,
                entry_node=definition.entry_node,
                project_id=project_id,
                inputs={
                    "source_path": str(self.source),
                    "source_language": "fr",
                    "target_language": "ja",
                    "instructions": instructions,
                    **(extra_inputs or {}),
                },
            )
        return result.run_id

    def test_ordered_parallel_uses_sliding_window_and_records_metrics(self) -> None:
        run_id = self._create_run("translation")
        definition = self.workflows["translation"]
        with self.databases.state.connect() as connection:
            node = connection.execute(
                """
                SELECT id, node_id, attempt, resource_lane
                FROM node_runs
                WHERE run_id = ?
                ORDER BY rowid
                LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        claimed = ClaimedNode(
            node_run_id=str(node["id"]),
            run_id=run_id,
            node_id=str(node["node_id"]),
            attempt=int(node["attempt"]),
            workflow_name=definition.name,
            workflow_version=definition.version,
            resource_lane=str(node["resource_lane"]),
            lease_owner="test",
            project_id=None,
            inputs={},
        )
        release_first = threading.Event()
        third_started = threading.Event()
        consumed: list[tuple[int, str]] = []

        def operation(item_index: int, item: str) -> str:
            if item_index == 0 and not release_first.wait(2):
                raise AssertionError("sliding window did not refill after item 2")
            if item_index == 1:
                time.sleep(0.02)
            if item_index == 2:
                third_started.set()
                release_first.set()
            return item.upper()

        self.worker._run_ordered_parallel(
            claimed,
            ["a", "b", "c"],
            parallelism=2,
            operation=operation,
            consume=lambda index, _item, result: consumed.append((index, result)),
            cancellation_message="Parallel test was cancelled",
            thread_name_prefix="parallel-test",
            operation_name="test_sliding_window",
            pool="test_pool",
        )

        self.assertTrue(third_started.is_set())
        self.assertEqual(consumed, [(0, "A"), (1, "B"), (2, "C")])
        with self.databases.state.connect() as connection:
            events = connection.execute(
                """
                SELECT event_type, payload_json
                FROM event_log
                WHERE run_id = ? AND event_type LIKE 'worker.parallel.%'
                ORDER BY rowid
                """,
                (run_id,),
            ).fetchall()

        completed = [
            json.loads(row["payload_json"])
            for row in events
            if row["event_type"] == "worker.parallel.item.completed"
        ]
        summaries = [
            json.loads(row["payload_json"])
            for row in events
            if row["event_type"] == "worker.parallel.summary"
        ]
        self.assertEqual(sorted(item["item_index"] for item in completed), [1, 2, 3])
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["execution_mode"], "sliding_window")
        self.assertEqual(summaries[0]["parallelism"], 2)
        self.assertEqual(summaries[0]["submitted_items"], 3)
        self.assertEqual(summaries[0]["completed_items"], 3)
        self.assertEqual(summaries[0]["failed_items"], 0)
        self.assertEqual(summaries[0]["peak_active_count"], 2)
        self.assertGreaterEqual(summaries[0]["merge_seconds"], 0)
        third_event = next(item for item in completed if item["item_index"] == 3)
        self.assertGreater(third_event["queue_wait_seconds"], 0.01)
        for item in completed:
            self.assertIn("target_count", item)
            self.assertIn("queued_at", item)
            self.assertIn("started_at", item)
            self.assertIn("completed_at", item)
            self.assertIn("duration_seconds", item)
            self.assertIn("effective_limit_at_start", item)

    def _create_corpus_run(self, root_path: Path | None = None) -> str:
        definition = self.workflows["corpus_sync"]
        with self.databases.state.connect() as connection:
            result = RunRepository(connection).create(
                workflow_name=definition.name,
                workflow_version=definition.version,
                entry_node=definition.entry_node,
                project_id=None,
                inputs={"root_path": str(root_path)} if root_path else {},
            )
        return result.run_id

    def _create_authoring_run(self) -> str:
        definition = self.workflows["manuscript_generator"]
        with self.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="Authoring test",
                root_path=str(self.root / "project"),
            )
            result = RunRepository(connection).create(
                workflow_name=definition.name,
                workflow_version=definition.version,
                entry_node=definition.entry_node,
                project_id=project_id,
                inputs={"title": "Argument", "note_text": "Evidence supports a conclusion."},
            )
        return result.run_id

    def _drain(self, limit: int = 30) -> None:
        for _ in range(limit):
            if not self.worker.run_once():
                break

    def test_nonretryable_citation_mapping_failure_is_not_requeued(self) -> None:
        draft_node = NodeSpec(
            id="draft_rewrite",
            handler="authoring.draft",
            title="Draft rewrite",
            resource_lane="codex_remote",
            retry=RetryPolicy(max_attempts=3),
        )
        definition = WorkflowDefinition(
            name="manuscript_generator",
            version="31",
            title="Citation mapping retry test",
            entry_node="draft_rewrite",
            nodes=[draft_node],
            edges=[],
        )
        with self.databases.state.connect() as connection:
            created = RunRepository(connection).create(
                workflow_name=definition.name,
                workflow_version=definition.version,
                entry_node=definition.entry_node,
                project_id=None,
                inputs={},
            )
            node_run = connection.execute(
                """
                SELECT id, node_id, attempt, resource_lane
                FROM node_runs WHERE run_id = ?
                """,
                (created.run_id,),
            ).fetchone()

        claimed = ClaimedNode(
            node_run_id=str(node_run["id"]),
            run_id=created.run_id,
            node_id=str(node_run["node_id"]),
            attempt=int(node_run["attempt"]),
            workflow_name=definition.name,
            workflow_version=definition.version,
            resource_lane=str(node_run["resource_lane"]),
            lease_owner="test",
            project_id=None,
            inputs={},
        )
        self.worker._fail(
            claimed,
            draft_node,
            NonRetryableFukamiruError(
                "Authoring citation JSON handoff lost required source-to-claim "
                "mapping for section sec-magazine"
            ),
        )

        with self.databases.state.connect() as connection:
            snapshot = RunRepository(connection).snapshot(created.run_id)
            attempt_count = connection.execute(
                "SELECT COUNT(*) AS count FROM node_runs WHERE run_id = ?",
                (created.run_id,),
            ).fetchone()["count"]
            retry_event_count = connection.execute(
                """
                SELECT COUNT(*) AS count FROM event_log
                WHERE run_id = ? AND event_type = 'node.retry_queued'
                """,
                (created.run_id,),
            ).fetchone()["count"]

        self.assertEqual(snapshot["run"]["status"], "failed")
        self.assertEqual(
            snapshot["run"]["error"]["type"], "NonRetryableFukamiruError"
        )
        self.assertEqual(attempt_count, 1)
        self.assertEqual(retry_event_count, 0)

    def test_fan_in_queues_once_after_every_parent_in_same_generation(self) -> None:
        workflow = WorkflowDefinition(
            name="fan_in_test",
            version="1",
            title="Fan-in test",
            entry_node="source",
            nodes=[
                NodeSpec(id="source", handler="test.source", title="Source"),
                NodeSpec(id="branch_a", handler="test.branch_a", title="A"),
                NodeSpec(id="branch_b", handler="test.branch_b", title="B"),
                NodeSpec(
                    id="joined",
                    handler="test.joined",
                    title="Join",
                    join_from=["branch_a", "branch_b"],
                    join_generation_source="source",
                ),
            ],
            edges=[
                EdgeSpec(source="source", target="branch_a"),
                EdgeSpec(source="source", target="branch_b"),
                EdgeSpec(source="branch_a", target="joined"),
                EdgeSpec(source="branch_b", target="joined"),
            ],
        )
        self.worker.workflows[workflow.name] = workflow
        self.worker.workflow_versions[(workflow.name, workflow.version)] = workflow
        self.worker.handlers.update(
            {
                "test.source": lambda _claimed, context: HandlerResult(context),
                "test.branch_a": lambda _claimed, context: HandlerResult(
                    {**context, "branch_a_result": ["a"]}
                ),
                "test.branch_b": lambda _claimed, context: HandlerResult(
                    {**context, "branch_b_result": ["b"]}
                ),
                "test.joined": lambda _claimed, context: HandlerResult(context),
            }
        )
        with self.databases.state.connect() as connection:
            run_id = RunRepository(connection).create(
                workflow_name=workflow.name,
                workflow_version=workflow.version,
                entry_node=workflow.entry_node,
                project_id=None,
                inputs={"shared": True},
            ).run_id

        self.assertTrue(self.worker.run_once(lane="ingestion_cpu"))
        self.assertTrue(self.worker.run_once(lane="ingestion_cpu"))
        with self.databases.state.connect() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM node_runs WHERE run_id=? AND node_id='joined'",
                    (run_id,),
                ).fetchone()[0],
                0,
            )
        self.assertTrue(self.worker.run_once(lane="ingestion_cpu"))
        with self.databases.state.connect() as connection:
            joined = connection.execute(
                "SELECT input_json FROM node_runs WHERE run_id=? AND node_id='joined'",
                (run_id,),
            ).fetchall()
        self.assertEqual(len(joined), 1)
        joined_context = json.loads(joined[0]["input_json"])
        self.assertEqual(joined_context["branch_a_result"], ["a"])
        self.assertEqual(joined_context["branch_b_result"], ["b"])

        self.assertTrue(self.worker.run_once(lane="ingestion_cpu"))
        with self.databases.state.connect() as connection:
            snapshot = RunRepository(connection).snapshot(run_id)
        self.assertEqual(snapshot["run"]["status"], "completed")

    def test_authoring_structure_join_passes_current_claim_json_once(self) -> None:
        workflow = WorkflowDefinition(
            name="authoring",
            version="30",
            title="Authoring JSON handoff",
            entry_node="source",
            nodes=[
                NodeSpec(id="source", handler="handoff.source", title="Source"),
                NodeSpec(
                    id="build_source_preservation_map",
                    handler="handoff.preservation",
                    title="Preservation",
                ),
                NodeSpec(
                    id="build_discourse_tree", handler="handoff.discourse", title="Discourse"
                ),
                NodeSpec(
                    id="plan_manuscript_structure",
                    handler="handoff.structure",
                    title="Structure",
                    join_from=["build_source_preservation_map", "build_discourse_tree"],
                    join_generation_source="source",
                ),
            ],
            edges=[
                EdgeSpec(source="source", target="build_source_preservation_map"),
                EdgeSpec(source="source", target="build_discourse_tree"),
                EdgeSpec(
                    source="build_source_preservation_map",
                    target="plan_manuscript_structure",
                ),
                EdgeSpec(source="build_discourse_tree", target="plan_manuscript_structure"),
            ],
        )
        self.worker.workflows[workflow.name] = workflow
        self.worker.workflow_versions[(workflow.name, workflow.version)] = workflow
        self.worker.handlers.update({
            "handoff.source": lambda _claimed, context: HandlerResult({
                **context,
                "claims": [{"id": "c1", "text": "元の主張"}],
            }),
            "handoff.preservation": lambda _claimed, context: HandlerResult({
                **context,
                "source_preservation_map": [{"proposition_id": "p1", "claim_ids": ["c1"]}],
                "source_preservation_contract": {"record_count": 1},
            }),
            "handoff.discourse": lambda _claimed, context: HandlerResult({
                **context,
                "claims": [{"id": "c1", "text": "範囲を限定した主張"}],
                "post_research_claim_reconstruction": {"narrowed_claim_ids": ["c1"]},
            }),
            "handoff.structure": lambda _claimed, context: HandlerResult(context),
        })
        with self.databases.state.connect() as connection:
            run_id = RunRepository(connection).create(
                workflow_name=workflow.name,
                workflow_version=workflow.version,
                entry_node=workflow.entry_node,
                project_id=None,
                inputs={},
            ).run_id

        for _ in range(3):
            self.assertTrue(self.worker.run_once(lane="ingestion_cpu"))
        with self.databases.state.connect() as connection:
            row = connection.execute(
                "SELECT input_json FROM node_runs WHERE run_id=? AND node_id=?",
                (run_id, "plan_manuscript_structure"),
            ).fetchone()
        self.assertIsNotNone(row)
        handed_off = json.loads(row["input_json"])
        self.assertEqual(handed_off["claims"], [{"id": "c1", "text": "範囲を限定した主張"}])
        self.assertEqual(
            handed_off["source_preservation_map"],
            [{"proposition_id": "p1", "claim_ids": ["c1"]}],
        )
        self.assertEqual(
            handed_off["post_research_claim_reconstruction"],
            {"narrowed_claim_ids": ["c1"]},
        )

    def test_authoring_retry_and_reference_json_reach_the_same_join(self) -> None:
        workflow = WorkflowDefinition(
            name="authoring",
            version="30",
            title="Authoring reference JSON handoff",
            entry_node="prepare_note",
            nodes=[
                NodeSpec(id="prepare_note", handler="handoff.note", title="Note"),
                NodeSpec(
                    id="extract_manuscript_references",
                    handler="handoff.references",
                    title="References",
                ),
                NodeSpec(
                    id="bounded_research_retry", handler="handoff.retry", title="Retry"
                ),
                NodeSpec(
                    id="resolve_reference_metadata",
                    handler="handoff.resolve",
                    title="Resolve",
                    join_from=[
                        "extract_manuscript_references",
                        "bounded_research_retry",
                    ],
                    join_generation_source="prepare_note",
                ),
            ],
            edges=[
                EdgeSpec(source="prepare_note", target="extract_manuscript_references"),
                EdgeSpec(source="prepare_note", target="bounded_research_retry"),
                EdgeSpec(
                    source="extract_manuscript_references",
                    target="resolve_reference_metadata",
                ),
                EdgeSpec(source="bounded_research_retry", target="resolve_reference_metadata"),
            ],
        )
        self.worker.workflows[workflow.name] = workflow
        self.worker.workflow_versions[(workflow.name, workflow.version)] = workflow
        self.worker.handlers.update({
            "handoff.note": lambda _claimed, context: HandlerResult(context),
            "handoff.references": lambda _claimed, context: HandlerResult({
                **context,
                "reference_entries": [{"id": "r1", "title": "資料A"}],
                "reference_inventory": {"entry_count": 1},
            }),
            "handoff.retry": lambda _claimed, context: HandlerResult({
                **context,
                "bounded_research_retry_report": {"attempted": True, "obligation_ids": ["o1"]},
                "research_discoveries": [{
                    "query": "地域資料", "candidates": [{"provider_record_id": "source-1"}],
                }],
                "evidence_coverage": [{"obligation_id": "o1", "covered": True}],
            }),
            "handoff.resolve": lambda _claimed, context: HandlerResult(context),
        })
        with self.databases.state.connect() as connection:
            run_id = RunRepository(connection).create(
                workflow_name=workflow.name,
                workflow_version=workflow.version,
                entry_node=workflow.entry_node,
                project_id=None,
                inputs={},
            ).run_id

        for _ in range(3):
            self.assertTrue(self.worker.run_once(lane="ingestion_cpu"))
        with self.databases.state.connect() as connection:
            row = connection.execute(
                "SELECT input_json FROM node_runs WHERE run_id=? AND node_id=?",
                (run_id, "resolve_reference_metadata"),
            ).fetchone()
        self.assertIsNotNone(row)
        handed_off = json.loads(row["input_json"])
        self.assertEqual(handed_off["reference_entries"][0]["id"], "r1")
        self.assertEqual(handed_off["research_discoveries"][0]["query"], "地域資料")
        self.assertEqual(handed_off["bounded_research_retry_report"]["obligation_ids"], ["o1"])
        self.assertEqual(handed_off["evidence_coverage"][0]["covered"], True)

    def test_every_authoring_v30_node_has_an_executable_handler(self) -> None:
        workflow = authoring_workflow(version="30")
        missing = [
            node.id for node in workflow.nodes if node.handler not in self.worker.handlers
        ]
        self.assertEqual(missing, [])

    def test_ingestion_indexes_real_source_text(self) -> None:
        run_id = self._create_run("ingestion")
        self._drain()
        with self.databases.state.connect() as connection:
            snapshot = RunRepository(connection).snapshot(run_id)
        self.assertEqual(snapshot["run"]["status"], "completed")
        with self.databases.knowledge.connect() as connection:
            results = KnowledgeSearchService(connection).search("document de recherche")
        self.assertGreaterEqual(len(results), 1)
        self.assertIn("Bonjour", results[0]["text"])
        self.assertTrue(results[0]["citation"]["source_span_id"])
        self.assertEqual(results[0]["material_role"], "unknown")
        with self.databases.knowledge.connect() as connection:
            filtered = KnowledgeSearchService(connection).search(
                "document de recherche",
                metadata_filters={"material_role": "primary"},
            )
        self.assertEqual(filtered, [])

    def test_image_only_pdf_uses_language_router_and_keeps_original_asset(self) -> None:
        source = Path(self.temp.name) / "japanese-scan.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        with source.open("wb") as handle:
            writer.write(handle)
        original_sha = hashlib.sha256(source.read_bytes()).hexdigest()

        def fake_ocr(
            _service: object,
            source_path: Path,
            *,
            language: str,
            output_dir: Path,
            **_kwargs: object,
        ) -> dict[str, object]:
            output_dir.mkdir(parents=True, exist_ok=True)
            text_path = output_dir / f"{source_path.stem}.txt"
            json_path = output_dir / f"{source_path.stem}.ocr.json"
            text = "--- PAGE 1 ---\n\n日本語の画像資料を言語別OCRから取り込みます。\n"
            text_path.write_text(text, encoding="utf-8")
            json_path.write_text(
                json.dumps(
                    {
                        "source": str(source_path),
                        "source_sha256": original_sha,
                        "language": language,
                        "engine": "ndlocr_lite",
                        "page_count": 1,
                        "pages": [{"page_number": 1, "text": text.split("\n\n", 1)[1]}],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            return {
                "status": "completed",
                "engine": "ndlocr_lite",
                "page_count": 1,
                "text_path": str(text_path),
                "json_path": str(json_path),
            }

        run_id = self._create_run(
            "ingestion",
            extra_inputs={"source_path": str(source), "source_language": "ja"},
        )
        with patch("fukamiru.ocr.service.PDFOCRService.process_file", new=fake_ocr):
            self._drain()

        with self.databases.state.connect() as connection:
            snapshot = RunRepository(connection).snapshot(run_id)
        self.assertEqual(snapshot["run"]["status"], "completed")
        self.assertEqual(snapshot["run"]["output"]["ocr_engine"], "ndlocr_lite")
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original_sha)
        with self.databases.knowledge.connect() as connection:
            results = KnowledgeSearchService(connection).search("言語別OCR")
            location = connection.execute(
                "SELECT path FROM source_locations WHERE source_asset_id=? AND is_primary=1",
                (snapshot["run"]["output"]["source_asset_id"],),
            ).fetchone()
        self.assertTrue(results)
        self.assertEqual(Path(str(location["path"])), source.resolve())


    def test_manuscript_rewrite_is_proposed_before_human_confirmation(self) -> None:
        project_root = Path(self.temp.name) / "rewrite-project"
        with self.databases.state.connect() as connection:
            project = ProjectService(connection).create(
                name="Rewrite project", root_path=project_root
            )
            revisions = RevisionService(connection)
            document_id = revisions.create_document(
                project_id=project["id"], kind="paper", title="Working draft"
            )
            base = revisions.commit_revision(
                document_id=document_id,
                canonical_html=(
                    '<article id="DOC-REWRITE" lang="en">'
                    '<section id="SEC-REWRITE"><h2 id="H-REWRITE">Argument</h2>'
                    '<p id="P-REWRITE">Initial working claim.</p></section></article>'
                ),
                editor_type="human",
                operation="human_import",
            )
            definition = self.workflows["manuscript_rewrite"]
            run = RunRepository(connection).create(
                workflow_name=definition.name,
                workflow_version=definition.version,
                entry_node=definition.entry_node,
                project_id=project["id"],
                inputs={
                    "document_id": document_id,
                    "base_revision_id": base.revision_id,
                    "instructions": "Make the central claim more explicit.",
                    "use_rag": False,
                },
            )

        self._drain()

        with self.databases.state.connect() as connection:
            snapshot = RunRepository(connection).snapshot(run.run_id)
            proposal = ManuscriptRewriteService(connection).get_by_run(run.run_id)
            current_before_approval = connection.execute(
                "SELECT current_revision_id FROM content_documents WHERE id = ?",
                (document_id,),
            ).fetchone()[0]
            self.assertEqual(snapshot["run"]["status"], "completed")
            self.assertEqual(proposal["status"], "needs_human_review")
            self.assertEqual(proposal["quality_status"], "needs_human_review")
            self.assertIn("Revised with the requested focus", proposal["proposed_html"])
            self.assertTrue(proposal["unified_diff"])
            self.assertEqual(current_before_approval, base.revision_id)

            confirmed = ManuscriptRewriteService(connection).confirm(proposal["id"])
            revision = connection.execute(
                "SELECT editor_type, operation FROM revisions WHERE id = ?",
                (confirmed["confirmed_revision_id"],),
            ).fetchone()
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(
            (revision["editor_type"], revision["operation"]),
            ("human", "human_edit"),
        )

    def test_coherence_receives_full_reference_blocks_with_separate_edit_scope(
        self,
    ) -> None:
        project_root = Path(self.temp.name) / "coherence-visibility-project"
        translator = CoherenceVisibilityTranslator()
        self.worker.translator = translator
        with self.databases.state.connect() as connection:
            project = ProjectService(connection).create(
                name="Coherence visibility", root_path=project_root
            )
            revisions = RevisionService(connection)
            document_id = revisions.create_document(
                project_id=project["id"], kind="paper", title="Reference-visible draft"
            )
            base = revisions.commit_revision(
                document_id=document_id,
                canonical_html=(
                    '<article id="DOC-VISIBLE" lang="ja">'
                    '<section id="SEC-BODY"><h2 id="H-BODY">第1節　論点</h2>'
                    '<p id="P-BODY">中心的な主張を示す。</p></section>'
                    '<section id="SEC-REF"><h2 id="H-REF">主要参照文献</h2>'
                    '<p id="P-REF">文献項目。</p></section></article>'
                ),
                editor_type="human",
                operation="human_import",
            )
            definition = self.workflows["manuscript_rewrite"]
            run = RunRepository(connection).create(
                workflow_name=definition.name,
                workflow_version=definition.version,
                entry_node=definition.entry_node,
                project_id=project["id"],
                inputs={
                    "document_id": document_id,
                    "base_revision_id": base.revision_id,
                    "instructions": "論旨を明確にする。",
                    "use_rag": False,
                    "use_llm_jp": False,
                },
            )

        self._drain(limit=50)

        with self.databases.state.connect() as connection:
            snapshot = RunRepository(connection).snapshot(run.run_id)
        self.assertEqual(snapshot["run"]["status"], "completed", msg=str(snapshot))
        self.assertGreaterEqual(len(translator.coherence_inputs), 2)
        for call in translator.coherence_inputs:
            self.assertIn("H-REF", call["block_ids"])
            self.assertIn("P-REF", call["block_ids"])
            self.assertNotIn("H-REF", call["editable_block_ids"])
            self.assertNotIn("P-REF", call["editable_block_ids"])
            manifest = call["document_manifest"]
            self.assertEqual(manifest["reference_section_count"], 1)
            self.assertEqual(manifest["reference_block_count"], 2)

    def test_invalid_paragraph_judgment_gets_one_targeted_supplement_then_falls_back(
        self,
    ) -> None:
        project_root = Path(self.temp.name) / "one-shot-paragraph-project"
        translator = InvalidParagraphPlannerTranslator()
        self.worker.translator = translator
        with self.databases.state.connect() as connection:
            project = ProjectService(connection).create(
                name="One-shot paragraphs", root_path=project_root
            )
            revisions = RevisionService(connection)
            document_id = revisions.create_document(
                project_id=project["id"], kind="paper", title="One-shot draft"
            )
            base = revisions.commit_revision(
                document_id=document_id,
                canonical_html=(
                    '<article id="DOC-ONE-SHOT" lang="ja">'
                    '<section id="SEC-ONE-SHOT"><h2 id="H-ONE-SHOT">論点</h2>'
                    '<p id="P-ONE-SHOT">第一の主張。第二の説明。</p>'
                    "</section></article>"
                ),
                editor_type="human",
                operation="human_import",
            )
            definition = self.workflows["manuscript_rewrite"]
            run = RunRepository(connection).create(
                workflow_name=definition.name,
                workflow_version=definition.version,
                entry_node=definition.entry_node,
                project_id=project["id"],
                inputs={
                    "document_id": document_id,
                    "base_revision_id": base.revision_id,
                    "instructions": "意味段落を一度だけ判定する。",
                    "use_rag": False,
                    "use_online_research": False,
                    "use_llm_jp": False,
                },
            )

        self._drain(limit=40)

        with self.databases.state.connect() as connection:
            snapshot = RunRepository(connection).snapshot(run.run_id)
            proposal = ManuscriptRewriteService(connection).get_by_run(run.run_id)
            paragraph_attempts = connection.execute(
                """
                SELECT COUNT(*) AS count FROM node_runs
                WHERE run_id = ? AND node_id = 'plan_semantic_paragraphs'
                """,
                (run.run_id,),
            ).fetchone()["count"]
            paragraph_event = connection.execute(
                """
                SELECT payload_json FROM event_log
                WHERE run_id = ?
                  AND event_type = 'manuscript.paragraph_plan.completed'
                ORDER BY event_id DESC LIMIT 1
                """,
                (run.run_id,),
            ).fetchone()
        validation = json.loads(str(paragraph_event["payload_json"]))
        self.assertEqual(snapshot["run"]["status"], "completed")
        self.assertEqual(proposal["quality_status"], "needs_human_review")
        self.assertIn(
            "paragraph.fallback_used",
            {item["code"] for item in proposal["context"]["completion_gate"]["blocking_reasons"]},
        )
        self.assertEqual(translator.paragraph_plan_calls, 2)
        self.assertEqual(paragraph_attempts, 1)
        self.assertTrue(validation["fallback_used"])
        self.assertEqual(validation["fallback_batch_count"], 1)
        self.assertTrue(validation["semantic_boundary_diagnostics"]["advisory_only"])
        self.assertFalse(validation["semantic_boundary_diagnostics"]["fed_back_to_planner"])

    def test_paragraph_cycle_detector_distinguishes_source_from_ancestor(self) -> None:
        plan = {
            "boundary_contract": {
                "partition_signature_sha256": "partition-a",
            }
        }
        cycle = self.worker._manuscript_boundary_cycle_report(
            {
                "source_lineage": [
                    {"paragraph_partition_signature_sha256": "partition-b"},
                    {"paragraph_partition_signature_sha256": "partition-a"},
                ]
            },
            plan,
        )
        stable_source = self.worker._manuscript_boundary_cycle_report(
            {
                "source_lineage": [
                    {"paragraph_partition_signature_sha256": "partition-a"},
                    {"paragraph_partition_signature_sha256": "partition-b"},
                ]
            },
            plan,
        )

        self.assertTrue(cycle["detected"])
        self.assertEqual(cycle["action"], "preserve_direct_source_and_require_review")
        self.assertFalse(stable_source["detected"])

    def test_staged_structure_planner_repairs_only_the_invalid_domain(self) -> None:
        project_root = Path(self.temp.name) / "staged-structure-project"
        translator = StagedRepairTranslator()
        self.worker.translator = translator
        v21_definition = manuscript_rewrite_workflow_v21()
        self.worker.workflow_versions[(v21_definition.name, v21_definition.version)] = (
            v21_definition
        )
        with self.databases.state.connect() as connection:
            project = ProjectService(connection).create(
                name="Staged structure", root_path=project_root
            )
            revisions = RevisionService(connection)
            document_id = revisions.create_document(
                project_id=project["id"], kind="paper", title="Staged draft"
            )
            base = revisions.commit_revision(
                document_id=document_id,
                canonical_html=(
                    '<article id="DOC-STAGED" lang="ja"><section id="SEC-STAGED">'
                    '<h2 id="H-STAGED">論点</h2>'
                    '<p id="P-STAGED">中心命題を検証する。</p>'
                    "</section></article>"
                ),
                editor_type="human",
                operation="human_import",
            )
            definition = v21_definition
            run = RunRepository(connection).create(
                workflow_name=definition.name,
                # This fixture exercises the legacy stage-level argument
                # repair. V22 requires the four dedicated argument processes
                # and deliberately does not fall back to this planner.
                workflow_version="21",
                entry_node=definition.entry_node,
                project_id=project["id"],
                inputs={
                    "document_id": document_id,
                    "base_revision_id": base.revision_id,
                    "instructions": "論証を段階的に計画する。",
                    "use_rag": False,
                    "use_online_research": False,
                    "use_llm_jp": False,
                },
            )

        self._drain(limit=40)

        with self.databases.state.connect() as connection:
            snapshot = RunRepository(connection).snapshot(run.run_id)
            stored_plan = connection.execute(
                "SELECT plan_json, validation_json FROM manuscript_structure_plans "
                "WHERE run_id = ?",
                (run.run_id,),
            ).fetchone()
            progress_rows = connection.execute(
                "SELECT payload_json FROM event_log WHERE run_id = ? AND "
                "event_type = 'manuscript.structure_plan.progress' ORDER BY event_id",
                (run.run_id,),
            ).fetchall()
        self.assertEqual(snapshot["run"]["status"], "completed")
        self.assertEqual(
            [str(item["stage"]) for item in translator.stage_calls],
            ["skeleton", "argument", "temporal", "discourse", "argument"],
        )
        repair_target = translator.stage_calls[-1]["repair_targets"]
        self.assertFalse(repair_target["full_stage"])
        self.assertEqual(repair_target["block_ids"], ["P-STAGED"])
        plan = json.loads(stored_plan["plan_json"])
        validation = json.loads(stored_plan["validation_json"])
        self.assertEqual(plan["planner_version"], 2)
        self.assertEqual(
            plan["_completed_structure_stages"],
            ["skeleton", "argument", "temporal", "discourse"],
        )
        self.assertTrue(validation["valid"])
        progress = [json.loads(row["payload_json"]) for row in progress_rows]
        self.assertTrue(
            any(
                item.get("stage") == "argument" and item.get("repair_mode") is True
                for item in progress
            )
        )

    def test_argument_planner_checkpoints_four_independent_processes(self) -> None:
        project_root = Path(self.temp.name) / "split-argument-project"
        translator = SplitArgumentTranslator()
        self.worker.translator = translator
        with self.databases.state.connect() as connection:
            project = ProjectService(connection).create(
                name="Split argument", root_path=project_root
            )
            revisions = RevisionService(connection)
            document_id = revisions.create_document(
                project_id=project["id"], kind="paper", title="Split draft"
            )
            base = revisions.commit_revision(
                document_id=document_id,
                canonical_html=(
                    '<article id="DOC-SPLIT" lang="ja"><section id="SEC-SPLIT">'
                    '<h2 id="H-SPLIT">論点</h2>'
                    '<p id="P-SPLIT-CLAIM">中心命題を検証する。</p>'
                    '<p id="P-SPLIT-EXAMPLE">『作品A』に現れる特徴を例示する。</p>'
                    "</section></article>"
                ),
                editor_type="human",
                operation="human_import",
            )
            definition = self.workflows["manuscript_rewrite"]
            run = RunRepository(connection).create(
                workflow_name=definition.name,
                workflow_version=definition.version,
                entry_node=definition.entry_node,
                project_id=project["id"],
                inputs={
                    "document_id": document_id,
                    "base_revision_id": base.revision_id,
                    "instructions": "論証を分割して計画する。",
                    "use_rag": False,
                    "use_online_research": False,
                    "use_llm_jp": False,
                },
            )

        self._drain(limit=40)

        with self.databases.state.connect() as connection:
            snapshot = RunRepository(connection).snapshot(run.run_id)
            checkpoints = connection.execute(
                "SELECT process_name, status, target_count, completed_count, "
                "reused_count, unresolved_count FROM manuscript_argument_checkpoints "
                "WHERE run_id = ? ORDER BY created_at",
                (run.run_id,),
            ).fetchall()
            progress_rows = connection.execute(
                "SELECT payload_json FROM event_log WHERE run_id = ? AND "
                "event_type = 'manuscript.argument_plan.progress' ORDER BY event_id",
                (run.run_id,),
            ).fetchall()
        self.assertEqual(snapshot["run"]["status"], "completed", snapshot["run"].get("error"))
        self.assertEqual(
            {row["process_name"] for row in checkpoints},
            {
                "block_roles",
                "reference_classification",
                "claim_evidence_links",
                "integration_validation",
            },
        )
        self.assertTrue(all(row["status"] == "completed" for row in checkpoints))
        self.assertTrue(all(row["unresolved_count"] == 0 for row in checkpoints))
        called = {str(item["process_name"]) for item in translator.argument_process_calls}
        self.assertEqual(
            called,
            {"block_roles", "reference_classification", "claim_evidence_links"},
        )
        progress = [json.loads(row["payload_json"]) for row in progress_rows]
        self.assertTrue(progress)
        self.assertTrue(all(len(item.get("argument_processes", [])) == 4 for item in progress))
        self.assertEqual(progress[-1]["completed"], 4)
        self.assertEqual(progress[-1]["unresolved_count"], 0)

    def test_mixed_link_checkpoint_persists_only_good_units_then_completes(self) -> None:
        class MixedLinks(SplitArgumentTranslator):
            def __init__(self):
                super().__init__()
                self.link_calls = 0

            def plan_manuscript_argument_process(self, *, process_name, **kwargs):
                payload = super().plan_manuscript_argument_process(
                    process_name=process_name, **kwargs
                )
                if process_name == "claim_evidence_links":
                    self.link_calls += 1
                    if self.link_calls == 1:
                        payload["argument_links"][-1]["target_claim_block_id"] = "C-OTHER"
                        payload["argument_links"][-1]["target_claim_anchor"] = "別の節の主張。"
                return payload

        translator = MixedLinks()
        self.worker.translator = translator
        with self.databases.state.connect() as connection:
            project = ProjectService(connection).create(
                name="Mixed links", root_path=Path(self.temp.name) / "mixed-links"
            )
            revisions = RevisionService(connection)
            document_id = revisions.create_document(
                project_id=project["id"], kind="paper", title="Links"
            )
            base = revisions.commit_revision(
                document_id=document_id,
                canonical_html=(
                    '<article id="DOC-LINKS" lang="ja"><section id="S-LINKS">'
                    '<h2 id="H-LINKS">論点</h2><p id="C-LINKS">中心命題を検証する。</p>'
                    '<p id="E-LINKS">『作品A』・『作品B』に現れる特徴を例示する。</p></section>'
                    '<section id="S-OTHER"><h2 id="H-OTHER">別の論点</h2>'
                    '<p id="C-OTHER">別の節の主張。</p></section></article>'
                ),
                editor_type="human",
                operation="human_import",
            )
            definition = self.workflows["manuscript_rewrite"]
            run = RunRepository(connection).create(
                workflow_name=definition.name,
                workflow_version=definition.version,
                entry_node=definition.entry_node,
                project_id=project["id"],
                inputs={
                    "document_id": document_id,
                    "base_revision_id": base.revision_id,
                    "instructions": "論証の整合性を保つ。",
                    "use_rag": False,
                    "use_online_research": False,
                    "use_llm_jp": False,
                },
            )
        self._drain(limit=40)
        with self.databases.state.connect() as connection:
            snapshot = RunRepository(connection).snapshot(run.run_id)
            checkpoints = connection.execute(
                "SELECT output_json, validation_json, status FROM manuscript_argument_checkpoints "
                "WHERE run_id=? AND process_name=? ORDER BY created_at",
                (run.run_id, "claim_evidence_links"),
            ).fetchall()
        self.assertEqual(snapshot["run"]["status"], "completed", snapshot["run"].get("error"))
        self.assertEqual(translator.link_calls, 2)
        self.assertEqual([row["status"] for row in checkpoints], ["invalid", "completed"])
        first_output = json.loads(checkpoints[0]["output_json"])
        self.assertEqual(len(first_output["argument_links"]), 1)
        self.assertEqual(first_output["argument_links"][0]["target_claim_block_id"], "C-LINKS")
        decision = json.loads(checkpoints[0]["validation_json"])["precommit"]
        self.assertEqual(decision["accepted_unit_count"], 1)
        self.assertEqual(decision["rejected_unit_count"], 1)
        last_call = translator.argument_process_calls[-1]
        self.assertEqual(last_call["process_name"], "claim_evidence_links")
        self.assertEqual(len(last_call["repair_targets"]["link_source_units"]), 1)

    def test_argument_reference_links_are_vetoed_before_plan_commit(self) -> None:
        structure = {
            "blocks": [
                {
                    "id": "B-1",
                    "reference_candidates": [
                        {"reference_id": "R-COMPARISON"},
                        {"reference_id": "R-WORK"},
                    ],
                }
            ]
        }
        plan = {
            "reference_classifications": [
                {
                    "reference_id": "R-COMPARISON",
                    "block_id": "B-1",
                    "kind": "comparison_target",
                },
                {
                    "reference_id": "R-WORK",
                    "block_id": "B-1",
                    "kind": "work_example",
                },
            ],
            "argument_links": [
                {
                    "source_block_id": "B-1",
                    "source_reference_id": "R-COMPARISON",
                },
                {
                    "source_block_id": "B-1",
                    "source_reference_id": "R-WORK",
                },
            ],
        }

        sanitized, vetoes = self.worker._sanitize_argument_reference_links(structure, plan)

        self.assertEqual(
            [item["source_reference_id"] for item in sanitized["argument_links"]],
            ["R-WORK"],
        )
        self.assertEqual(len(vetoes), 1)
        self.assertEqual(vetoes[0]["code"], "argument.reference_not_example")
        self.assertEqual(vetoes[0]["action"], "drop_argument_link")

    @staticmethod
    def _argument_link_repair_fixture():
        structure = ManuscriptCoherenceService.semantic_structure(
            '<article id="DOC-UNITS" lang="ja">'
            '<section id="S-1"><h2 id="H-1">第一節</h2>'
            '<p id="C-1">地域の経験を作品が表す。</p>'
            '<p id="E-1">『作品A』・『作品B』が地域の経験を描く。</p>'
            '<p id="E-2">経験の相違を場面の描写で示す。</p></section>'
            '<section id="S-2"><h2 id="H-2">第二節</h2>'
            '<p id="C-2">別の節での主張。</p></section></article>'
        )
        plan = FakeTranslator().plan_manuscript_structure(semantic_structure=structure)
        plan["reference_classifications"] = [
            {
                "reference_id": ref["reference_id"],
                "block_id": block["id"],
                "kind": "work_example",
                "rationale": "具体的な作品の例。",
            }
            for block in structure["blocks"]
            for ref in block["reference_candidates"]
        ]
        for role in plan["block_roles"]:
            if role["block_id"] == "E-2":
                role["role"] = "example"
        refs = [item["reference_id"] for item in plan["reference_classifications"]]
        links = [
            {
                "source_block_id": source,
                "source_reference_id": reference,
                "target_claim_block_id": "C-1",
                "target_claim_anchor": "地域の経験を作品が表す。",
                "relation": "exemplifies",
                "observed_property": "地域の経験の描写。",
                "warrant": "経験が描写の差異として表れる。",
                "evidence_required": False,
                "research_query": None,
            }
            for source, reference in [("E-1", refs[0]), ("E-1", refs[1]), ("E-2", None)]
        ]
        plan["argument_links"] = [links[0]]
        return structure, plan, links

    def test_mixed_argument_repairs_keep_good_units_and_retry_only_bad_unit(self) -> None:
        structure, plan, links = self._argument_link_repair_fixture()
        bad = {
            **links[1],
            "target_claim_block_id": "C-2",
            "target_claim_anchor": "別の節での主張。",
        }
        candidate = {**plan, "argument_links": [links[0], bad, links[2]]}
        retained, decisions = self.worker._commit_argument_link_units(structure, plan, candidate)
        self.assertEqual(retained["argument_links"], [links[0], links[2]])
        self.assertEqual(sum(item["accepted"] for item in decisions), 1)
        after = self.worker._argument_process_validation(
            structure, retained, "claim_evidence_links", {"full_stage": True}
        )
        self.assertEqual([item["code"] for item in after["issues"]], ["argument.example_unlinked"])
        # Avoid other missing stages: this test concerns the argument target contract.
        retained["_completed_structure_stages"] = ["skeleton", "argument", "temporal", "discourse"]
        targets = self.worker._manuscript_structure_repair_targets(structure, retained, after)
        target = self.worker._argument_process_targets(targets["argument"], "claim_evidence_links")
        self.assertEqual(
            target["link_source_units"],
            [{"source_block_id": "E-1", "source_reference_id": links[1]["source_reference_id"]}],
        )
        self.assertEqual(
            self.worker._argument_process_target_count(
                structure, retained, target, "claim_evidence_links"
            ),
            1,
        )
        fixed = self.worker._merge_argument_process(
            retained,
            "claim_evidence_links",
            {"argument_links": [links[1], {**links[0], "warrant": "OFF-TARGET"}]},
            target,
        )
        self.assertEqual(fixed["argument_links"], [links[0], links[2], links[1]])
        self.assertTrue(
            self.worker._argument_process_validation(
                structure, fixed, "claim_evidence_links", target
            )["valid"]
        )

    def test_argument_unit_gate_rejects_non_claim_self_link_and_missing_warrant(self) -> None:
        structure, plan, links = self._argument_link_repair_fixture()
        bad_units = [
            {**links[1], "target_claim_block_id": "H-1", "target_claim_anchor": "第一節"},
            {**links[2], "target_claim_block_id": "E-2"},
            {**links[1], "warrant": ""},
            {**links[1], "target_claim_anchor": "存在しないアンカー"},
        ]
        for bad in bad_units:
            with self.subTest(candidate=bad):
                retained, decisions = self.worker._commit_argument_link_units(
                    structure, plan, {**plan, "argument_links": [links[0], bad]}
                )
                self.assertEqual(retained["argument_links"], plan["argument_links"])
                self.assertTrue(decisions)
                self.assertFalse(any(item["accepted"] for item in decisions))

    def test_argument_link_omission_preserves_existing_units(self) -> None:
        _, plan, _ = self._argument_link_repair_fixture()
        for target in ({"full_stage": True}, {"block_ids": ["E-1"]}):
            retained = self.worker._merge_argument_process(
                plan, "claim_evidence_links", {"argument_links": []}, target
            )
            self.assertEqual(retained["argument_links"], plan["argument_links"])

    def test_argument_errors_on_sibling_references_have_distinct_identities(self) -> None:
        structure, plan, links = self._argument_link_repair_fixture()
        plan["argument_links"] = [
            {**link, "target_claim_block_id": "C-2", "target_claim_anchor": "別の節での主張。"}
            for link in links[:2]
        ] + [links[2]]
        validation = self.worker._argument_process_validation(
            structure, plan, "claim_evidence_links", {"full_stage": True}
        )
        atoms = self.worker._argument_validation_issue_atoms(validation)
        self.assertEqual(len(atoms), 2)
        self.assertEqual({item[0] for item in atoms}, {"argument.cross_section_example"})

    def test_typed_example_can_anchor_to_claim_in_same_paragraph(self) -> None:
        structure, plan, links = self._argument_link_repair_fixture()
        for role in plan["block_roles"]:
            if role["block_id"] == "E-1":
                role["role"] = "claim"
        plan["argument_links"] = [links[0], links[2]]
        intra_block = {
            **links[1],
            "target_claim_block_id": "E-1",
            "target_claim_anchor": "地域の経験を描く。",
        }
        retained, decisions = self.worker._commit_argument_link_units(
            structure, plan, {**plan, "argument_links": [links[0], links[2], intra_block]}
        )
        self.assertEqual(len(decisions), 1)
        self.assertTrue(decisions[0]["accepted"], decisions)
        self.assertEqual(retained["argument_links"][-1], intra_block)

    def test_argument_precommit_rejects_a_worse_repair(self) -> None:
        baseline = {
            "issues": [
                {
                    "code": "argument.example_unlinked",
                    "block_id": "B-1",
                    "reference_id": "R-1",
                }
            ]
        }
        candidate = {
            "issues": [
                {
                    "code": "argument.reference_not_example",
                    "block_id": "B-1",
                    "reference_id": "R-1",
                },
                {
                    "code": "argument.reference_not_example",
                    "block_id": "B-1",
                    "reference_id": "R-2",
                },
            ]
        }

        decision = self.worker._argument_candidate_precommit(
            baseline,
            candidate,
            {"full_stage": False, "block_ids": ["B-1"]},
        )

        self.assertFalse(decision["accepted"])
        self.assertEqual(decision["reason"], "no_monotonic_improvement")
        self.assertEqual(decision["baseline_issue_count"], 1)
        self.assertEqual(decision["candidate_issue_count"], 2)

    def test_full_block_role_process_targets_only_plannable_blocks(self) -> None:
        structure = ManuscriptCoherenceService.semantic_structure(
            '<article id="DOC-ROLE-SCOPE" lang="ja">'
            '<section id="SEC-BODY"><h2 id="H-BODY">第1節　論点</h2>'
            '<p id="P-BODY">中心命題を示す。</p></section>'
            '<section id="SEC-REF"><h2 id="H-REF">主要参照文献</h2>'
            '<p id="P-REF">著者（2024）『文献』。</p></section></article>'
        )
        targets = {
            "full_stage": True,
            "block_ids": [],
            "section_ids": [],
            "reference_ids": [],
            "mention_ids": [],
            "global_fields": [],
        }
        plan = {
            "block_roles": [
                {
                    "block_id": "H-BODY",
                    "role": "heading",
                    "parent_claim_block_id": None,
                    "relation_to_previous": "none",
                },
                {
                    "block_id": "P-BODY",
                    "role": "claim",
                    "parent_claim_block_id": None,
                    "relation_to_previous": "support",
                },
            ]
        }

        validation = self.worker._argument_process_validation(
            structure, plan, "block_roles", targets
        )
        target_count = self.worker._argument_process_target_count(
            structure, plan, targets, "block_roles"
        )

        self.assertTrue(validation["valid"], validation["issues"])
        self.assertEqual(target_count, 2)

    def test_role_sanitizer_removes_non_plannable_role_without_rollback(
        self,
    ) -> None:
        structure = ManuscriptCoherenceService.semantic_structure(
            '<article id="DOC-ROLE-REPAIR" lang="ja">'
            '<section id="SEC-BODY"><h2 id="H-BODY">第1節　論点</h2>'
            '<p id="P-BODY">中心命題を示す。</p></section>'
            '<section id="SEC-REF"><h2 id="H-REF">主要参照文献</h2>'
            '<p id="P-REF">著者（2024）『文献』。</p></section></article>'
        )
        baseline_plan = {
            "block_roles": [
                {
                    "block_id": block_id,
                    "role": "heading" if block_id.startswith("H-") else "claim",
                    "parent_claim_block_id": None,
                    "relation_to_previous": "none",
                }
                for block_id in ("H-BODY", "P-BODY", "H-REF", "P-REF")
            ]
        }

        candidate_plan, actions = self.worker._sanitize_argument_roles(structure, baseline_plan)
        candidate_validation = self.worker._argument_process_validation(
            structure,
            candidate_plan,
            "block_roles",
            {
                "full_stage": True,
                "block_ids": [],
                "section_ids": [],
                "reference_ids": [],
                "mention_ids": [],
                "global_fields": [],
            },
        )
        decision = self.worker._argument_candidate_precommit(
            {
                "issues": [
                    {
                        "code": "argument.role_unknown_block",
                        "block_ids": ["H-REF", "P-REF"],
                    }
                ]
            },
            candidate_validation,
            {"full_stage": True},
        )

        self.assertEqual(
            [item["block_id"] for item in candidate_plan["block_roles"]],
            ["H-BODY", "P-BODY"],
        )
        self.assertEqual(
            {str(item.get("block_id")) for item in actions},
            {"H-REF", "P-REF"},
        )
        self.assertTrue(candidate_validation["valid"], candidate_validation["issues"])
        self.assertTrue(decision["accepted"], decision)

    def test_argument_precommit_rejects_new_hard_issue_despite_lower_count(
        self,
    ) -> None:
        baseline = {
            "issues": [
                {
                    "code": "plan.block_role_missing",
                    "block_ids": ["B-1", "B-2", "B-3"],
                }
            ]
        }
        candidate = {
            "issues": [
                {
                    "code": "argument.claim_anchor_unresolved",
                    "block_id": "B-1",
                }
            ]
        }

        decision = self.worker._argument_candidate_precommit(
            baseline,
            candidate,
            {"full_stage": True},
        )

        self.assertFalse(decision["accepted"], decision)
        self.assertEqual(decision["candidate_issue_count"], 1)
        self.assertEqual(decision["baseline_issue_count"], 3)
        self.assertIn(
            "argument.claim_anchor_unresolved",
            decision["introduced_issue_codes"],
        )

    def test_argument_claim_anchor_is_canonicalized_to_planned_central_claim(
        self,
    ) -> None:
        structure = {
            "sections": [
                {
                    "section_id": "semantic:H-1",
                    "plannable": True,
                    "block_ids": ["H-1", "P-CLAIM", "P-EXAMPLE"],
                }
            ],
            "blocks": [
                {
                    "id": "H-1",
                    "section_id": "semantic:H-1",
                    "text": "論点",
                },
                {
                    "id": "P-CLAIM",
                    "section_id": "semantic:H-1",
                    "text": "中心命題の原文である。",
                },
                {
                    "id": "P-EXAMPLE",
                    "section_id": "semantic:H-1",
                    "text": "具体例を示す。",
                },
            ],
        }
        plan = {
            "sections": [
                {
                    "section_id": "semantic:H-1",
                    "central_claim_block_id": "P-CLAIM",
                    "central_claim": "作品例は中心命題を具体化する。",
                }
            ],
            "argument_links": [
                {
                    "source_block_id": "P-EXAMPLE",
                    "target_claim_block_id": "P-CLAIM",
                    "target_claim_anchor": "中心命題",
                }
            ],
        }

        canonical, actions = self.worker._canonicalize_argument_claim_anchors(structure, plan)

        self.assertEqual(
            canonical["argument_links"][0]["target_claim_anchor"],
            "作品例は中心命題を具体化する。",
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["source_block_id"], "P-EXAMPLE")

    def test_introduction_rewrite_action_is_a_valid_deferred_edit_contract(
        self,
    ) -> None:
        class ValidReport:
            valid = True

            @staticmethod
            def as_dict() -> dict[str, object]:
                return {"valid": True, "issues": []}

        validation = self.worker._manuscript_structure_validation(
            ValidReport(),
            [],
            plan={
                "introduction_contract": {
                    "moves": [
                        {
                            "move": "positive_purpose_or_thesis",
                            "block_ids": ["P-INTRO"],
                            "required": True,
                        }
                    ],
                    "negative_scope_records": [
                        {
                            "block_id": "P-INTRO",
                            "justified": False,
                            "antecedent_block_id": None,
                            "action": "rewrite",
                        }
                    ],
                }
            },
            require_introduction_contract=True,
        )

        self.assertTrue(validation["valid"], validation["issues"])
        self.assertNotIn(
            "introduction.negative_scope_unanchored",
            {item["code"] for item in validation["issues"]},
        )

    def test_introduction_issue_targets_contract_field_during_partial_repair(
        self,
    ) -> None:
        plan = {
            "document_thesis": "論旨",
            "global_ordering_policy": "claim_evidence",
            "introduction_contract": {},
            "sections": [],
            "block_roles": [],
            "reference_classifications": [],
            "argument_links": [],
            "research_queries": [],
            "unresolved_questions": [],
            "temporal_mentions": [],
            "block_temporal_scopes": [],
            "connective_relations": [],
            "merge_groups": [],
        }
        targets = self.worker._manuscript_structure_repair_targets(
            {
                "sections": [
                    {
                        "section_id": "semantic:H-INTRO",
                        "block_ids": ["H-INTRO", "P-INTRO"],
                    }
                ],
                "blocks": [
                    {
                        "id": "P-INTRO",
                        "section_id": "semantic:H-INTRO",
                        "reference_candidates": [],
                        "temporal_mentions": [],
                    }
                ],
            },
            plan,
            {
                "issues": [
                    {
                        "code": "introduction.negative_scope_unanchored",
                        "block_id": "P-INTRO",
                    }
                ],
                "blocking_normalization_actions": [],
            },
        )

        self.assertIn("skeleton", targets)
        self.assertFalse(targets["skeleton"]["full_stage"])
        self.assertIn("introduction_contract", targets["skeleton"]["global_fields"])

    def test_terminal_section_order_normalization_routes_to_skeleton(self) -> None:
        plan = {
            "document_thesis": "論旨",
            "global_ordering_policy": "claim_evidence",
            "introduction_contract": {},
            "sections": [],
            "block_roles": [],
            "reference_classifications": [],
            "argument_links": [],
            "research_queries": [],
            "unresolved_questions": [],
            "temporal_mentions": [],
            "block_temporal_scopes": [],
            "connective_relations": [],
            "merge_groups": [],
        }
        action = {
            "action": "normalize_terminal_section_order",
            "from_section_ids": ["semantic:H-LIMIT", "semantic:H-CONCLUSION"],
            "to_section_ids": ["semantic:H-CONCLUSION", "semantic:H-LIMIT"],
        }
        targets = self.worker._manuscript_structure_repair_targets(
            {
                "sections": [
                    {
                        "section_id": "semantic:H-CONCLUSION",
                        "block_ids": ["H-CONCLUSION", "P-CONCLUSION"],
                    },
                    {
                        "section_id": "semantic:H-LIMIT",
                        "block_ids": ["H-LIMIT", "P-LIMIT"],
                    },
                ],
                "blocks": [],
            },
            plan,
            {
                "issues": [],
                "blocking_normalization_actions": [action],
            },
        )
        feedback = self.worker._manuscript_structure_feedback_for_stage(
            [
                {
                    "code": "plan.normalization_required",
                    "details": action,
                }
            ],
            "skeleton",
        )

        self.assertIn("skeleton", targets)
        skeleton = targets["skeleton"]
        self.assertTrue(
            skeleton["full_stage"]
            or {
                "semantic:H-CONCLUSION",
                "semantic:H-LIMIT",
            }.issubset(set(skeleton["section_ids"])),
            skeleton,
        )
        self.assertEqual(len(feedback), 1)

    def test_argument_repair_targets_are_isolated_by_process(self) -> None:
        structure = ManuscriptCoherenceService.semantic_structure(
            '<article id="DOC-PROCESS-SCOPE" lang="ja">'
            '<section id="SEC-BODY"><h2 id="H-BODY">論点</h2>'
            '<p id="P-BODY">『作品』を例に中心命題を示す。</p></section>'
            '<section id="SEC-REF"><h2 id="H-REF">主要参照文献</h2>'
            '<p id="P-REF">著者『文献』。</p></section></article>'
        )
        body_reference = structure["blocks"][1]["reference_candidates"][0]
        reference_reference = structure["blocks"][3]["reference_candidates"][0]
        plan = {
            "document_thesis": "論旨",
            "global_ordering_policy": "claim_evidence",
            "introduction_contract": {},
            "sections": [],
            "block_roles": [],
            "reference_classifications": [],
            "argument_links": [],
            "research_queries": [],
            "unresolved_questions": [],
            "temporal_mentions": [],
            "block_temporal_scopes": [],
            "connective_relations": [],
            "merge_groups": [],
        }

        targets = self.worker._manuscript_structure_repair_targets(
            structure,
            plan,
            {
                "issues": [
                    {
                        "code": "reference.classification_missing",
                        "block_id": "P-REF",
                        "reference_id": reference_reference["reference_id"],
                    },
                    {
                        "code": "argument.claim_anchor_unresolved",
                        "block_id": "P-BODY",
                        "reference_id": body_reference["reference_id"],
                    },
                ],
                "blocking_normalization_actions": [],
            },
        )["argument"]

        role_targets = self.worker._argument_process_targets(targets, "block_roles")
        reference_targets = self.worker._argument_process_targets(
            targets, "reference_classification"
        )
        link_targets = self.worker._argument_process_targets(targets, "claim_evidence_links")

        self.assertEqual(role_targets["block_ids"], [])
        self.assertEqual(
            reference_targets["reference_ids"],
            [reference_reference["reference_id"]],
        )
        self.assertEqual(link_targets["block_ids"], ["P-BODY"])
        self.assertIn(body_reference["reference_id"], link_targets["reference_ids"])
        self.assertNotIn(reference_reference["reference_id"], link_targets["reference_ids"])

    def test_introduction_rewrite_obligation_is_stable_and_blocks_until_applied(
        self,
    ) -> None:
        plan = {
            "introduction_contract": {
                "negative_scope_records": [
                    {"block_id": "P-INTRO", "action": "rewrite"},
                    {"block_id": "P-KEEP", "action": "preserve"},
                ]
            }
        }
        obligations = self.worker._introduction_rewrite_obligations(plan)

        self.assertEqual(len(obligations), 1)
        self.assertEqual(obligations[0]["block_id"], "P-INTRO")
        self.assertEqual(
            obligations,
            self.worker._introduction_rewrite_obligations(plan),
        )

        base_context = {
            "introduction_rewrite_obligations": obligations,
            "proposed_html": (
                '<article id="DOC-INTRO-GATE" lang="ja"><section id="SEC">'
                '<p id="P-INTRO">目的を示す。</p></section></article>'
            ),
        }
        unresolved = self.worker._manuscript_completion_gate(base_context)
        resolved = self.worker._manuscript_completion_gate(
            {
                **base_context,
                "applied_edits": [
                    {
                        "block_id": "P-INTRO",
                        "review_finding_ids": [obligations[0]["finding_id"]],
                    }
                ],
            }
        )

        self.assertIn(
            "introduction.rewrite_not_applied",
            {item["code"] for item in unresolved["blocking_reasons"]},
        )
        self.assertNotIn(
            "introduction.rewrite_not_applied",
            {item["code"] for item in resolved["blocking_reasons"]},
        )

    def test_v18_terminal_order_normalization_is_audited_but_not_blocking(
        self,
    ) -> None:
        class ValidReport:
            valid = True

            @staticmethod
            def as_dict() -> dict[str, object]:
                return {"valid": True, "issues": []}

        action = {"action": "normalize_terminal_section_order"}
        legacy = self.worker._manuscript_structure_validation(ValidReport(), [action])
        current = self.worker._manuscript_structure_validation(
            ValidReport(),
            [action],
            allow_terminal_order_normalization=True,
        )

        self.assertFalse(legacy["valid"])
        self.assertTrue(current["valid"])
        self.assertEqual(current["normalization_actions"], [action])
        self.assertEqual(current["blocking_normalization_actions"], [])

    def test_conflicting_reference_link_cannot_poison_structure_plan(self) -> None:
        project_root = Path(self.temp.name) / "precommit-argument-project"
        translator = ConflictingReferenceLinkTranslator()
        self.worker.translator = translator
        with self.databases.state.connect() as connection:
            project = ProjectService(connection).create(
                name="Precommit argument", root_path=project_root
            )
            revisions = RevisionService(connection)
            document_id = revisions.create_document(
                project_id=project["id"], kind="paper", title="Precommit draft"
            )
            base = revisions.commit_revision(
                document_id=document_id,
                canonical_html=(
                    '<article id="DOC-PRECOMMIT" lang="ja">'
                    '<section id="SEC-PRECOMMIT"><h2 id="H-PRECOMMIT">論点</h2>'
                    '<p id="P-PRECOMMIT-CLAIM">中心命題を検証する。</p>'
                    '<p id="P-PRECOMMIT-EXAMPLE">『作品A』との比較を記述する。</p>'
                    "</section></article>"
                ),
                editor_type="human",
                operation="human_import",
            )
            definition = self.workflows["manuscript_rewrite"]
            run = RunRepository(connection).create(
                workflow_name=definition.name,
                workflow_version=definition.version,
                entry_node=definition.entry_node,
                project_id=project["id"],
                inputs={
                    "document_id": document_id,
                    "base_revision_id": base.revision_id,
                    "instructions": "参照分類と論証リンクを分離して検証する。",
                    "use_rag": False,
                    "use_online_research": False,
                    "use_llm_jp": False,
                },
            )

        self._drain(limit=40)

        with self.databases.state.connect() as connection:
            snapshot = RunRepository(connection).snapshot(run.run_id)
            stored = connection.execute(
                "SELECT plan_json FROM manuscript_structure_plans WHERE run_id = ?",
                (run.run_id,),
            ).fetchone()
            precommit_events = connection.execute(
                "SELECT payload_json FROM event_log WHERE run_id = ? AND "
                "event_type = 'manuscript.argument_plan.precommit' ORDER BY event_id",
                (run.run_id,),
            ).fetchall()
        self.assertEqual(snapshot["run"]["status"], "completed", snapshot["run"].get("error"))
        self.assertIsNotNone(translator.conflicting_reference_id)
        plan = json.loads(stored["plan_json"])
        conflicting_links = [
            item
            for item in plan["argument_links"]
            if item.get("source_reference_id") == translator.conflicting_reference_id
        ]
        self.assertEqual(conflicting_links, [])
        payloads = [json.loads(row["payload_json"]) for row in precommit_events]
        # V22's application-owned disposition contract drops a duplicate or
        # unknown link before semantic precommit, so no rollback event is
        # required and the committed plan remains unpoisoned.
        self.assertEqual(payloads, [])

    def test_manuscript_sections_use_available_codex_slots_in_parallel(self) -> None:
        project_root = Path(self.temp.name) / "parallel-rewrite-project"
        translator = ParallelManuscriptTranslator()
        self.worker.translator = translator
        with self.databases.state.connect() as connection:
            project = ProjectService(connection).create(
                name="Parallel rewrite", root_path=project_root
            )
            revisions = RevisionService(connection)
            document_id = revisions.create_document(
                project_id=project["id"], kind="paper", title="Parallel draft"
            )
            sections = "".join(
                f'<section id="SEC-{index}"><h2 id="H-{index}">節{index}</h2>'
                f'<p id="P-{index}">主張{index}。</p></section>'
                for index in range(5)
            )
            base = revisions.commit_revision(
                document_id=document_id,
                canonical_html=f'<article id="DOC-PARALLEL" lang="ja">{sections}</article>',
                editor_type="human",
                operation="human_import",
            )
            definition = self.workflows["manuscript_rewrite"]
            run = RunRepository(connection).create(
                workflow_name=definition.name,
                workflow_version=definition.version,
                entry_node=definition.entry_node,
                project_id=project["id"],
                inputs={
                    "document_id": document_id,
                    "base_revision_id": base.revision_id,
                    "instructions": "各節の論証を明確にする。",
                    "use_rag": False,
                    "use_online_research": False,
                    "use_llm_jp": False,
                },
            )

        self._drain(limit=30)

        with self.databases.state.connect() as connection:
            snapshot = RunRepository(connection).snapshot(run.run_id)
            event = connection.execute(
                """
                SELECT payload_json FROM event_log
                WHERE run_id = ? AND event_type = 'manuscript.rewrite.parallelism'
                ORDER BY rowid DESC LIMIT 1
                """,
                (run.run_id,),
            ).fetchone()
            slot_event = connection.execute(
                """
                SELECT payload_json FROM event_log
                WHERE run_id = ?
                  AND event_type = 'worker.codex.subslots.reserved'
                ORDER BY rowid DESC LIMIT 1
                """,
                (run.run_id,),
            ).fetchone()
        self.assertEqual(snapshot["run"]["status"], "completed")
        self.assertEqual(len(translator.rewrite_calls), 5)
        self.assertEqual(translator.max_active_calls, 5)
        self.assertEqual(json.loads(event["payload_json"])["parallelism"], 5)
        slot_payload = json.loads(slot_event["payload_json"])
        self.assertEqual(slot_payload["requested_subslots"], 4)
        self.assertEqual(slot_payload["reserved_subslots"], 4)
        self.assertGreaterEqual(slot_payload["slot_wait_seconds"], 0)

    def test_contextual_predicate_resolution_is_applied_before_coherence(self) -> None:
        project_root = Path(self.temp.name) / "predicate-rewrite-project"
        self.worker.translator = PredicateResolvingTranslator()
        with self.databases.state.connect() as connection:
            project = ProjectService(connection).create(
                name="Predicate rewrite", root_path=project_root
            )
            revisions = RevisionService(connection)
            document_id = revisions.create_document(
                project_id=project["id"], kind="paper", title="Predicate draft"
            )
            base = revisions.commit_revision(
                document_id=document_id,
                canonical_html=(
                    '<article id="DOC-PREDICATE" lang="ja">'
                    '<section id="SEC-PREDICATE"><h2 id="H-PREDICATE">論点</h2>'
                    '<p id="P-PREDICATE">羞恥と支配は、傷ついた主体の回復に'
                    "接続される。</p></section></article>"
                ),
                editor_type="human",
                operation="human_import",
            )
            definition = self.workflows["manuscript_rewrite"]
            run = RunRepository(connection).create(
                workflow_name=definition.name,
                workflow_version=definition.version,
                entry_node=definition.entry_node,
                project_id=project["id"],
                inputs={
                    "document_id": document_id,
                    "base_revision_id": base.revision_id,
                    "instructions": "抽象的な関係を明示する。",
                    "use_rag": False,
                    "use_online_research": False,
                    "use_llm_jp": False,
                },
            )

        self._drain(limit=30)

        with self.databases.state.connect() as connection:
            snapshot = RunRepository(connection).snapshot(run.run_id)
            proposal = ManuscriptRewriteService(connection).get_by_run(run.run_id)
        self.assertEqual(snapshot["run"]["status"], "completed")
        self.assertIn("回復に寄与する", proposal["proposed_html"])
        resolution = proposal["context"]["predicate_relation_resolution"]
        self.assertEqual(resolution["candidate_count"], 1)
        self.assertEqual(resolution["applied_count"], 1)

    def test_manuscript_coherence_allows_two_bounded_repair_rounds(self) -> None:
        project_root = Path(self.temp.name) / "cascading-coherence-project"
        translator = CascadingCoherenceTranslator()
        self.worker.translator = translator
        with self.databases.state.connect() as connection:
            project = ProjectService(connection).create(
                name="Cascading coherence", root_path=project_root
            )
            revisions = RevisionService(connection)
            document_id = revisions.create_document(
                project_id=project["id"], kind="paper", title="Cascading draft"
            )
            base = revisions.commit_revision(
                document_id=document_id,
                canonical_html=(
                    '<article id="DOC-CASCADE" lang="ja"><section id="SEC-CASCADE">'
                    '<h2 id="H-TITLE">試験原稿</h2>'
                    '<p id="H-1">### 1　第一節</p><p id="P-1">第一の主張。</p>'
                    '<p id="H-2">### 2　第二節</p><p id="P-2">第二の主張。</p>'
                    "</section></article>"
                ),
                editor_type="human",
                operation="human_import",
            )
            definition = self.workflows["manuscript_rewrite"]
            run = RunRepository(connection).create(
                workflow_name=definition.name,
                workflow_version=definition.version,
                entry_node=definition.entry_node,
                project_id=project["id"],
                inputs={
                    "document_id": document_id,
                    "base_revision_id": base.revision_id,
                    "instructions": "論証順序を整える。",
                    "use_rag": False,
                    "use_online_research": False,
                    "use_llm_jp": False,
                },
            )

        self._drain(limit=30)

        with self.databases.state.connect() as connection:
            snapshot = RunRepository(connection).snapshot(run.run_id)
            proposal = ManuscriptRewriteService(connection).get_by_run(run.run_id)
            checks = connection.execute(
                """
                SELECT stage, pass_index, status
                FROM manuscript_coherence_checks
                WHERE run_id = ? ORDER BY created_at
                """,
                (run.run_id,),
            ).fetchall()
        self.assertEqual(snapshot["run"]["status"], "completed")
        self.assertLess(
            proposal["proposed_html"].index('id="H-1"'),
            proposal["proposed_html"].index('id="H-2"'),
        )
        self.assertIn(
            "第一の主張。 整合済み。",
            proposal["proposed_html"],
        )
        self.assertIn(
            "第二の主張。 整合済み。",
            proposal["proposed_html"],
        )
        self.assertEqual(
            [(row["stage"], row["pass_index"], row["status"]) for row in checks],
            [
                ("pre_polish", 1, "failed"),
                ("pre_polish", 2, "failed"),
                ("pre_polish", 3, "passed"),
                ("post_polish", 1, "passed"),
            ],
        )

    def test_manuscript_coherence_requires_replan_for_post_lock_moves(self) -> None:
        project_root = Path(self.temp.name) / "locked-structure-project"
        self.worker.translator = LockedStructureMoveTranslator()
        with self.databases.state.connect() as connection:
            project = ProjectService(connection).create(
                name="Locked structure", root_path=project_root
            )
            revisions = RevisionService(connection)
            document_id = revisions.create_document(
                project_id=project["id"], kind="paper", title="Locked draft"
            )
            base = revisions.commit_revision(
                document_id=document_id,
                canonical_html=(
                    '<article id="DOC-LOCKED" lang="ja"><section id="SEC-LOCKED">'
                    '<h2 id="H-TITLE">試験原稿</h2>'
                    '<p id="H-1">### 1　第一節</p><p id="P-1">第一の主張。</p>'
                    '<p id="H-2">### 2　第二節</p><p id="P-2">第二の主張。</p>'
                    "</section></article>"
                ),
                editor_type="human",
                operation="human_import",
            )
            definition = self.workflows["manuscript_rewrite"]
            run = RunRepository(connection).create(
                workflow_name=definition.name,
                workflow_version=definition.version,
                entry_node=definition.entry_node,
                project_id=project["id"],
                inputs={
                    "document_id": document_id,
                    "base_revision_id": base.revision_id,
                    "instructions": "順序を監査する。",
                    "use_rag": False,
                    "use_online_research": False,
                    "use_llm_jp": False,
                },
            )

        self._drain(limit=30)

        with self.databases.state.connect() as connection:
            snapshot = RunRepository(connection).snapshot(run.run_id)
            checkpoint = connection.execute(
                """
                SELECT status, candidate_html
                FROM manuscript_coherence_checkpoints
                WHERE run_id = ? AND stage = 'pre_polish'
                ORDER BY updated_at DESC LIMIT 1
                """,
                (run.run_id,),
            ).fetchone()
        self.assertEqual(snapshot["run"]["status"], "failed")
        self.assertEqual(snapshot["run"]["error"]["type"], "CoherenceResolutionRequired")
        self.assertIn(
            "paragraph.structure_replan_required",
            snapshot["run"]["error"]["message"],
        )
        self.assertEqual(checkpoint["status"], "escalated")
        self.assertLess(
            checkpoint["candidate_html"].index('id="H-1"'),
            checkpoint["candidate_html"].index('id="H-2"'),
        )

    def test_manuscript_coherence_stops_repeated_noop_without_node_retry(self) -> None:
        project_root = Path(self.temp.name) / "repeating-coherence-project"
        translator = RepeatingCoherenceTranslator()
        self.worker.translator = translator
        with self.databases.state.connect() as connection:
            project = ProjectService(connection).create(
                name="Repeating coherence", root_path=project_root
            )
            revisions = RevisionService(connection)
            document_id = revisions.create_document(
                project_id=project["id"], kind="paper", title="Repeating draft"
            )
            base = revisions.commit_revision(
                document_id=document_id,
                canonical_html=(
                    '<article id="DOC-REPEAT" lang="ja"><section id="SEC-REPEAT">'
                    '<h2 id="H-REPEAT">試験原稿</h2>'
                    '<p id="P-REPEAT">節間の説明。</p></section></article>'
                ),
                editor_type="human",
                operation="human_import",
            )
            definition = self.workflows["manuscript_rewrite"]
            run = RunRepository(connection).create(
                workflow_name=definition.name,
                workflow_version=definition.version,
                entry_node=definition.entry_node,
                project_id=project["id"],
                inputs={
                    "document_id": document_id,
                    "base_revision_id": base.revision_id,
                    "instructions": "循環せずに節間接続を直す。",
                    "use_rag": False,
                    "use_online_research": False,
                    "use_llm_jp": False,
                },
            )

        self._drain(limit=30)

        with self.databases.state.connect() as connection:
            snapshot = RunRepository(connection).snapshot(run.run_id)
            attempts = connection.execute(
                """
                SELECT COUNT(*) AS count FROM node_runs
                WHERE run_id = ? AND node_id = 'validate_rewrite_coherence'
                """,
                (run.run_id,),
            ).fetchone()["count"]
            checkpoint = connection.execute(
                """
                SELECT status, round_index, candidate_html
                FROM manuscript_coherence_checkpoints
                WHERE run_id = ? AND stage = 'pre_polish'
                ORDER BY updated_at DESC LIMIT 1
                """,
                (run.run_id,),
            ).fetchone()
            stalled_events = connection.execute(
                """
                SELECT COUNT(*) AS count FROM event_log
                WHERE run_id = ?
                  AND event_type = 'manuscript.coherence.repair_stalled'
                """,
                (run.run_id,),
            ).fetchone()["count"]
        self.assertEqual(snapshot["run"]["status"], "failed")
        self.assertEqual(snapshot["run"]["error"]["type"], "CoherenceResolutionRequired")
        self.assertIn("coherence.repair_stalled", snapshot["run"]["error"]["message"])
        self.assertEqual(translator.pre_polish_calls, 2)
        self.assertEqual(attempts, 1)
        self.assertEqual(checkpoint["status"], "stalled")
        self.assertEqual(checkpoint["round_index"], 2)
        self.assertIn("整合済み", checkpoint["candidate_html"])
        self.assertEqual(stalled_events, 1)

    def test_manuscript_coherence_retry_resumes_durable_checkpoint(self) -> None:
        project_root = Path(self.temp.name) / "checkpoint-coherence-project"
        translator = CheckpointResumeCoherenceTranslator()
        self.worker.translator = translator
        with self.databases.state.connect() as connection:
            project = ProjectService(connection).create(
                name="Checkpoint coherence", root_path=project_root
            )
            revisions = RevisionService(connection)
            document_id = revisions.create_document(
                project_id=project["id"], kind="paper", title="Checkpoint draft"
            )
            base = revisions.commit_revision(
                document_id=document_id,
                canonical_html=(
                    '<article id="DOC-CHECKPOINT" lang="ja">'
                    '<section id="SEC-CHECKPOINT">'
                    '<h2 id="H-CHECKPOINT">試験原稿</h2>'
                    '<p id="P-CHECKPOINT">節間の説明。</p></section></article>'
                ),
                editor_type="human",
                operation="human_import",
            )
            definition = self.workflows["manuscript_rewrite"]
            run = RunRepository(connection).create(
                workflow_name=definition.name,
                workflow_version=definition.version,
                entry_node=definition.entry_node,
                project_id=project["id"],
                inputs={
                    "document_id": document_id,
                    "base_revision_id": base.revision_id,
                    "instructions": "修復済みHTMLを再試行で維持する。",
                    "use_rag": False,
                    "use_online_research": False,
                    "use_llm_jp": False,
                },
            )

        self._drain(limit=40)

        with self.databases.state.connect() as connection:
            snapshot = RunRepository(connection).snapshot(run.run_id)
            proposal = ManuscriptRewriteService(connection).get_by_run(run.run_id)
            attempts = connection.execute(
                """
                SELECT COUNT(*) AS count FROM node_runs
                WHERE run_id = ? AND node_id = 'validate_rewrite_coherence'
                """,
                (run.run_id,),
            ).fetchone()["count"]
            resumed_events = connection.execute(
                """
                SELECT COUNT(*) AS count FROM event_log
                WHERE run_id = ?
                  AND event_type = 'manuscript.coherence.checkpoint_resumed'
                """,
                (run.run_id,),
            ).fetchone()["count"]
        self.assertEqual(snapshot["run"]["status"], "completed")
        self.assertIn("チェックポイント済み", proposal["proposed_html"])
        self.assertEqual(translator.pre_polish_calls, 3)
        self.assertEqual(attempts, 2)
        self.assertEqual(resumed_events, 1)

    def test_coherence_revalidates_repairs_then_waits_for_real_human_review(
        self,
    ) -> None:
        project_root = Path(self.temp.name) / "coherence-human-review-project"
        translator = RepairThenHumanReviewCoherenceTranslator()
        self.worker.translator = translator
        with self.databases.state.connect() as connection:
            project = ProjectService(connection).create(
                name="Coherence human review", root_path=project_root
            )
            revisions = RevisionService(connection)
            document_id = revisions.create_document(
                project_id=project["id"], kind="paper", title="Reviewable draft"
            )
            base = revisions.commit_revision(
                document_id=document_id,
                canonical_html=(
                    '<article id="DOC-REVIEWABLE" lang="ja">'
                    '<section id="SEC-REVIEWABLE">'
                    '<h2 id="H-REVIEWABLE">試験原稿</h2>'
                    '<p id="P-REVIEWABLE">次節では結論を検討する。</p>'
                    "</section></article>"
                ),
                editor_type="human",
                operation="human_import",
            )
            definition = self.workflows["manuscript_rewrite"]
            run = RunRepository(connection).create(
                workflow_name=definition.name,
                workflow_version=definition.version,
                entry_node=definition.entry_node,
                project_id=project["id"],
                inputs={
                    "document_id": document_id,
                    "base_revision_id": base.revision_id,
                    "instructions": "局所修正後に著者判断を求める。",
                    "use_rag": False,
                    "use_online_research": False,
                    "use_llm_jp": False,
                },
            )

        self._drain(limit=40)

        with self.databases.state.connect() as connection:
            waiting = RunRepository(connection).snapshot(run.run_id)
            review = connection.execute(
                """
                SELECT * FROM human_reviews
                WHERE run_id = ? AND object_type = 'manuscript_coherence_resolution'
                """,
                (run.run_id,),
            ).fetchone()
        self.assertEqual(waiting["run"]["status"], "waiting_human")
        self.assertIsNotNone(review)
        self.assertEqual(translator.pre_polish_calls, 2)
        self.assertIn("接続修正済み", waiting["run"]["output"]["proposed_html"])

        resumed = self.worker.resolve_review(str(review["id"]), "approved")
        self.assertEqual(resumed["status"], "queued")
        self._drain(limit=40)

        with self.databases.state.connect() as connection:
            completed = RunRepository(connection).snapshot(run.run_id)
            proposal = ManuscriptRewriteService(connection).get_by_run(run.run_id)
        self.assertEqual(completed["run"]["status"], "completed")
        self.assertIn("接続修正済み", proposal["proposed_html"])
        self.assertEqual(translator.pre_polish_calls, 3)
        accepted = proposal["context"]["coherence_human_acceptances"]
        self.assertEqual(accepted[0]["category"], "ordering")

    def test_v23_coherence_editorial_judgment_is_deferred_to_final_proposal(self) -> None:
        definition = manuscript_rewrite_workflow_v23()
        with self.databases.state.connect() as connection:
            run = RunRepository(connection).create(
                workflow_name=definition.name,
                workflow_version=definition.version,
                entry_node=definition.entry_node,
                project_id=None,
                inputs={},
            )
        claimed = ClaimedNode(
            node_run_id="coherence-node",
            run_id=run.run_id,
            node_id="validate_rewrite_coherence",
            attempt=1,
            workflow_name=definition.name,
            workflow_version=definition.version,
            resource_lane="codex_remote",
            lease_owner="test",
            project_id=None,
            inputs={},
        )
        route = {
            "category": "argument_gap",
            "resolution_route": "human_review",
            "block_ids": ["P-1"],
            "explanation": "The argument needs an editorial decision.",
        }
        candidate = '<article id="DOC-1" lang="ja"><p id="P-1">本文。</p></article>'
        kwargs = dict(
            context={},
            candidate_html=candidate,
            reports=[{"stage": "pre_polish", "passed": False}],
            applied_edits=[],
            warnings=[],
            restored_ids=[],
            repair_history=[],
            routes=[route],
            pass_index=2,
        )
        result = self.worker._coherence_human_review_result(
            claimed, stage="pre_polish", **kwargs
        )
        self.assertFalse(result.waiting_human)
        self.assertEqual(result.outcome, "conditional_success")
        self.assertEqual(result.context["proposed_html"], candidate)
        self.assertEqual(result.context["coherence_deferred_reviews"][0]["routes"], [route])
        with self.assertRaisesRegex(Exception, "coherence.repair_stalled"):
            self.worker._coherence_human_review_result(
                claimed,
                stage="pre_polish",
                **{
                    **kwargs,
                    "routes": [
                        route,
                        {"category": "ordering", "resolution_route": "bounded_rewrite"},
                    ],
                },
            )
        self.assertEqual(
            definition.next_nodes("validate_rewrite_coherence", result.outcome),
            ["resolve_manuscript_predicate_relations"],
        )
        with patch.object(
            self.worker, "_final_quality_context", side_effect=lambda value, _: value
        ):
            final = self.worker._coherence_human_review_result(
                claimed, stage="post_polish", **{**kwargs, "context": result.context}
            )
        self.assertFalse(final.waiting_human)
        self.assertEqual(len(final.context["coherence_deferred_reviews"]), 2)
        with self.databases.state.connect() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM human_reviews WHERE run_id = ?", (run.run_id,)
                ).fetchone()[0],
                0,
            )

    def test_additional_research_persists_conditional_draft_and_resumes_lineage(
        self,
    ) -> None:
        project_root = Path(self.temp.name) / "conditional-coherence-project"
        translator = AdditionalResearchCoherenceTranslator()
        self.worker.translator = translator
        with self.databases.state.connect() as connection:
            project = ProjectService(connection).create(
                name="Conditional coherence", root_path=project_root
            )
            revisions = RevisionService(connection)
            document_id = revisions.create_document(
                project_id=project["id"], kind="paper", title="Conditional draft"
            )
            base = revisions.commit_revision(
                document_id=document_id,
                canonical_html=(
                    '<article id="DOC-CONDITIONAL" lang="ja">'
                    '<section id="SEC-CONDITIONAL">'
                    '<h2 id="H-CONDITIONAL">試験原稿</h2>'
                    '<p id="P-CONDITIONAL">比較の論証。</p></section></article>'
                ),
                editor_type="human",
                operation="human_import",
            )
            definition = self.workflows["manuscript_rewrite"]
            run = RunRepository(connection).create(
                workflow_name=definition.name,
                workflow_version=definition.version,
                entry_node=definition.entry_node,
                project_id=project["id"],
                inputs={
                    "document_id": document_id,
                    "base_revision_id": base.revision_id,
                    "instructions": "安全な改稿を残し、根拠不足を明示する。",
                    "use_rag": False,
                    "use_online_research": False,
                    "use_llm_jp": False,
                },
            )

        self._drain(limit=40)

        with self.databases.state.connect() as connection:
            snapshot = RunRepository(connection).snapshot(run.run_id)
            proposal = ManuscriptRewriteService(connection).get_by_run(run.run_id)
            coherence_node = connection.execute(
                """
                SELECT id, input_json, attempt, resource_lane
                FROM node_runs
                WHERE run_id = ? AND node_id = 'validate_rewrite_coherence'
                ORDER BY attempt DESC LIMIT 1
                """,
                (run.run_id,),
            ).fetchone()
            conditional_events = connection.execute(
                """
                SELECT COUNT(*) AS count FROM event_log
                WHERE run_id = ?
                  AND event_type = 'manuscript.coherence.conditional_success'
                """,
                (run.run_id,),
            ).fetchone()["count"]
        self.assertEqual(snapshot["run"]["status"], "completed")
        self.assertEqual(snapshot["run"]["output"]["completion_status"], "conditional_success")
        self.assertEqual(proposal["quality_status"], "needs_human_review")
        self.assertEqual(proposal["context"]["completion_status"], "conditional_success")
        self.assertIn("条件付き修復済み", proposal["proposed_html"])
        self.assertEqual(
            proposal["context"]["conditional_success"]["research_requirements"][0][
                "research_query"
            ],
            "作品比較 読者反応 受容史",
        )
        self.assertEqual(translator.pre_polish_calls, 2)
        self.assertEqual(conditional_events, 1)

        source_context = json.loads(str(coherence_node["input_json"]))
        with self.databases.state.connect() as connection:
            connection.execute(
                """
                UPDATE manuscript_coherence_checkpoints
                SET lineage_signature = NULL, evidence_signature = NULL,
                    source_sha256 = NULL
                WHERE run_id = ? AND stage = 'pre_polish'
                """,
                (run.run_id,),
            )
            connection.commit()
        claimed = ClaimedNode(
            node_run_id=str(coherence_node["id"]),
            run_id=run.run_id,
            node_id="validate_rewrite_coherence",
            attempt=int(coherence_node["attempt"]),
            workflow_name=definition.name,
            workflow_version=definition.version,
            resource_lane=str(coherence_node["resource_lane"]),
            lease_owner="test",
            project_id=str(project["id"]),
            inputs=source_context,
        )
        same_evidence = self.worker._manuscript_validate_coherence(claimed, dict(source_context))
        self.assertEqual(same_evidence.outcome, "conditional_success")
        self.assertEqual(translator.pre_polish_calls, 2)

        refreshed_context = {
            **source_context,
            "evidence_context": [
                *list(source_context.get("evidence_context") or []),
                {
                    "id": "E-NEW",
                    "query": "作品比較 読者反応 受容史",
                    "provider": "test",
                    "provider_record_id": "new-evidence",
                },
            ],
        }
        refreshed = self.worker._manuscript_validate_coherence(claimed, refreshed_context)
        self.assertEqual(refreshed.outcome, "success")
        self.assertIn("条件付き修復済み", refreshed.context["proposed_html"])
        self.assertEqual(translator.pre_polish_calls, 3)

        with self.databases.state.connect() as connection:
            checkpoints = connection.execute(
                """
                SELECT lineage_signature, evidence_signature, evidence_round_index,
                       round_index, candidate_html
                FROM manuscript_coherence_checkpoints
                WHERE run_id = ? AND stage = 'pre_polish'
                ORDER BY round_index
                """,
                (run.run_id,),
            ).fetchall()
        self.assertEqual(len(checkpoints), 3)
        self.assertIsNone(checkpoints[0]["lineage_signature"])
        self.assertIsNone(checkpoints[0]["evidence_signature"])
        self.assertIsNotNone(checkpoints[1]["lineage_signature"])
        self.assertEqual(checkpoints[1]["lineage_signature"], checkpoints[2]["lineage_signature"])
        self.assertNotEqual(
            checkpoints[1]["evidence_signature"], checkpoints[2]["evidence_signature"]
        )
        self.assertEqual([row["evidence_round_index"] for row in checkpoints], [1, 2, 1])
        self.assertEqual([row["round_index"] for row in checkpoints], [1, 2, 3])
        self.assertIn("条件付き修復済み", checkpoints[2]["candidate_html"])

    def test_final_coherence_preserves_verdicts_without_inferring_regressions(
        self,
    ) -> None:
        pre_polish = (
            '<article id="DOC" lang="ja"><section id="SEC">'
            '<h2 id="H">節</h2><p id="P-OLD">既存の不足。</p>'
            '<p id="P-NEW">推敲前の文章。</p></section></article>'
        )
        candidate = pre_polish.replace("推敲前の文章。", "推敲後の重複重複。")
        report = {
            "passed": False,
            "issues": [
                {
                    "id": "old",
                    "severity": "blocking",
                    "block_ids": ["P-OLD"],
                },
                {
                    "id": "new",
                    "severity": "warning",
                    "block_ids": ["P-NEW"],
                },
            ],
        }

        classified = self.worker._classify_final_coherence_issues(
            report,
            pre_polish_html=pre_polish,
            candidate_html=candidate,
        )

        self.assertEqual(classified["issues"][0]["severity"], "blocking")
        self.assertEqual(
            classified["issues"][0]["final_gate_disposition"],
            "late_discovered_blocker",
        )
        self.assertEqual(classified["issues"][1]["severity"], "warning")
        self.assertEqual(
            classified["issues"][1]["final_gate_disposition"],
            "post_edit_finding",
        )
        self.assertEqual(classified["preexisting_blocking_downgraded"], 0)
        self.assertEqual(classified["polish_warnings_escalated"], 0)
        self.assertEqual(classified["late_discovered_blocking_count"], 1)

    def test_machine_fact_adjudication_rejects_false_missing_reference_issue(
        self,
    ) -> None:
        canonical_html = (
            '<article id="DOC-FACT" lang="ja"><section id="SEC-FACT">'
            '<h2 id="H19">第19節　AI生成環境</h2><p id="P19">事例。</p>'
            '<h2 id="HC">結論</h2><p id="PC">結論。</p>'
            '<h3 id="HL">適用範囲と検証課題</h3><p id="PL">課題。</p>'
            '<h2 id="HR">主要参照文献</h2><p id="R1">文献。</p>'
            "</section></article>"
        )
        manifest = ManuscriptCoherenceService.document_manifest(canonical_html)
        terminal = ManuscriptCoherenceService.terminal_contract(canonical_html)
        report = {
            "passed": False,
            "document_assertions": {
                "block_count": 6,
                "section_count": 3,
                "reference_section_count": 0,
                "reference_block_count": 0,
                "terminal_section_ids": ["semantic:HC", "semantic:HL"],
                "terminal_roles": ["conclusion", "limitations"],
                "last_section_id": "semantic:HL",
                "last_section_role": "limitations",
            },
            "issues": [
                {
                    "id": "false-terminal",
                    "category": "ordering",
                    "severity": "blocking",
                    "verification_scope": "machine_structure",
                    "block_ids": ["HL", "PL"],
                    "explanation": ("原稿は適用範囲と検証課題で終わり、主要参照文献が存在しない。"),
                    "recommendation": "主要参照文献を末尾に追加する。",
                    "resolution_route": "human_review",
                    "research_query": None,
                },
                {
                    "id": "oblique-terminal",
                    "category": "section_transition",
                    "severity": "blocking",
                    "verification_scope": "machine_structure",
                    "block_ids": ["HC", "PC"],
                    "explanation": ("The physical section arrangement violates the contract."),
                    "recommendation": "Choose another physical arrangement.",
                    "resolution_route": "human_review",
                    "research_query": None,
                },
            ],
            "repair_edits": [],
            "section_moves": [],
        }

        adjudicated = self.worker._adjudicate_coherence_machine_facts(
            report,
            manifest=manifest,
            terminal_contract=terminal,
        )

        self.assertTrue(adjudicated["passed"])
        self.assertEqual(adjudicated["issues"], [])
        rejection_reasons = {
            item["id"]: item["rejection_reason"]
            for item in adjudicated["rejected_machine_fact_issues"]
        }
        self.assertEqual(
            rejection_reasons["false-terminal"],
            "reference_section_reported_absent_but_present",
        )
        self.assertEqual(
            rejection_reasons["oblique-terminal"],
            "application_structure_contracts_are_valid",
        )
        self.assertGreaterEqual(len(adjudicated["model_assertion_mismatches"]), 1)
        self.assertEqual(adjudicated["document_assertions"]["reference_section_count"], 1)
        self.assertEqual(adjudicated["document_assertions"]["last_section_role"], "references")

    def test_coherence_contract_forbids_repairing_observable_read_only_block(
        self,
    ) -> None:
        report = {
            "passed": False,
            "summary": "Reference prose was flagged.",
            "checked_block_ids": ["P1", "R1"],
            "coverage": [
                {"category": category, "checked": True, "note": "checked"}
                for category in (
                    "temporal_conflict",
                    "connector_mismatch",
                    "orphan_example",
                    "missing_warrant",
                    "argument_gap",
                    "section_transition",
                    "duplication",
                    "ordering",
                )
            ],
            "issues": [
                {
                    "id": "readonly",
                    "category": "ordering",
                    "severity": "blocking",
                    "verification_scope": "semantic_content",
                    "block_ids": ["R1"],
                    "explanation": "Fixture.",
                    "recommendation": "Fixture repair.",
                    "resolution_route": "bounded_rewrite",
                    "research_query": None,
                }
            ],
            "repair_edits": [
                {
                    "operation": "replace_text",
                    "block_id": "R1",
                    "anchor_block_id": None,
                    "text": "Changed reference.",
                    "review_finding_ids": ["readonly"],
                    "evidence_ids": [],
                }
            ],
            "section_moves": [],
        }

        issues = self.worker._coherence_contract_issues(
            report,
            expected_block_ids={"P1", "R1"},
            editable_block_ids={"P1"},
            editable_section_ids={"semantic:H1"},
        )

        self.assertIn(
            "coherence.repair_read_only_block",
            {item["code"] for item in issues},
        )

    def test_machine_fact_adjudication_blocks_false_pass_on_invalid_terminal_order(
        self,
    ) -> None:
        canonical_html = (
            '<article id="DOC-INVALID-FACT" lang="ja">'
            '<section id="SEC-INVALID-FACT">'
            '<h2 id="HC">結論</h2><p id="PC">結論。</p>'
            '<h2 id="HR">主要参照文献</h2><p id="R1">文献。</p>'
            '<h3 id="HL">適用範囲と検証課題</h3><p id="PL">課題。</p>'
            "</section></article>"
        )
        manifest = ManuscriptCoherenceService.document_manifest(canonical_html)
        terminal = ManuscriptCoherenceService.terminal_contract(canonical_html)
        report = {
            "passed": True,
            "document_assertions": self.worker._coherence_manifest_assertions(manifest),
            "issues": [],
            "repair_edits": [],
            "section_moves": [],
        }

        adjudicated = self.worker._adjudicate_coherence_machine_facts(
            report,
            manifest=manifest,
            terminal_contract=terminal,
        )

        self.assertFalse(adjudicated["passed"])
        deterministic = [
            item
            for item in adjudicated["issues"]
            if item.get("verification_scope") == "machine_structure"
        ]
        self.assertGreaterEqual(len(deterministic), 2)
        self.assertTrue(all(item["severity"] == "blocking" for item in deterministic))

    def test_final_coherence_applies_late_claim_limitation_without_restore_loop(
        self,
    ) -> None:
        project_root = Path(self.temp.name) / "late-final-claim-project"
        translator = LateFinalClaimCoherenceTranslator()
        self.worker.translator = translator
        with self.databases.state.connect() as connection:
            project = ProjectService(connection).create(
                name="Late final claim", root_path=project_root
            )
            revisions = RevisionService(connection)
            document_id = revisions.create_document(
                project_id=project["id"], kind="paper", title="Late final claim draft"
            )
            base = revisions.commit_revision(
                document_id=document_id,
                canonical_html=(
                    '<article id="DOC-LATE" lang="ja"><section id="SEC-LATE">'
                    '<h2 id="H-LATE">結論</h2>'
                    '<p id="P-LATE">媒体規則は歴史的に組み替えられてきた。</p>'
                    "</section></article>"
                ),
                editor_type="human",
                operation="human_import",
            )
            definition = self.workflows["manuscript_rewrite"]
            run = RunRepository(connection).create(
                workflow_name=definition.name,
                workflow_version=definition.version,
                entry_node=definition.entry_node,
                project_id=project["id"],
                inputs={
                    "document_id": document_id,
                    "base_revision_id": base.revision_id,
                    "instructions": "断定の根拠範囲を維持する。",
                    "use_rag": False,
                    "use_online_research": False,
                    "use_llm_jp": False,
                },
            )

        self._drain(limit=40)

        with self.databases.state.connect() as connection:
            snapshot = RunRepository(connection).snapshot(run.run_id)
            proposal = ManuscriptRewriteService(connection).get_by_run(run.run_id)
            rows = connection.execute(
                """
                SELECT stage, pass_index, status, report_json
                FROM manuscript_coherence_checks
                WHERE run_id = ? ORDER BY created_at
                """,
                (run.run_id,),
            ).fetchall()
            checkpoints = connection.execute(
                """
                SELECT candidate_sha256, restored_block_ids_json
                FROM manuscript_coherence_checkpoints
                WHERE run_id = ? AND stage = 'post_polish'
                ORDER BY round_index
                """,
                (run.run_id,),
            ).fetchall()
        self.assertEqual(snapshot["run"]["status"], "completed")
        self.assertEqual(translator.post_polish_calls, 2)
        self.assertIn("今後検証されるべき仮説", proposal["proposed_html"])
        self.assertEqual(
            proposal["context"]["final_quality_sha256"],
            hashlib.sha256(proposal["proposed_html"].encode()).hexdigest(),
        )
        post_rows = [row for row in rows if row["stage"] == "post_polish"]
        self.assertEqual(
            [(row["pass_index"], row["status"]) for row in post_rows],
            [(1, "failed"), (2, "passed")],
        )
        first_report = json.loads(str(post_rows[0]["report_json"]))
        self.assertEqual(
            first_report["issues"][0]["final_gate_disposition"],
            "late_discovered_blocker",
        )
        self.assertEqual(len({row["candidate_sha256"] for row in checkpoints}), 1)
        self.assertEqual(json.loads(checkpoints[-1]["restored_block_ids_json"]), [])

    def test_final_coherence_surface_rejection_stops_without_repair_loop(self) -> None:
        project_root = Path(self.temp.name) / "surface-rejected-final-claim-project"
        translator = SurfaceInvalidLateFinalClaimCoherenceTranslator()
        self.worker.translator = translator
        with self.databases.state.connect() as connection:
            project = ProjectService(connection).create(
                name="Surface rejected final claim", root_path=project_root
            )
            revisions = RevisionService(connection)
            document_id = revisions.create_document(
                project_id=project["id"], kind="paper", title="Surface rejection draft"
            )
            base = revisions.commit_revision(
                document_id=document_id,
                canonical_html=(
                    '<article id="DOC-SURFACE" lang="ja"><section id="SEC-SURFACE">'
                    '<h2 id="H-SURFACE">結論</h2>'
                    '<p id="P-SURFACE">媒体規則は歴史的に組み替えられてきた。</p>'
                    "</section></article>"
                ),
                editor_type="human",
                operation="human_import",
            )
            definition = self.workflows["manuscript_rewrite"]
            run = RunRepository(connection).create(
                workflow_name=definition.name,
                workflow_version=definition.version,
                entry_node=definition.entry_node,
                project_id=project["id"],
                inputs={
                    "document_id": document_id,
                    "base_revision_id": base.revision_id,
                    "instructions": "断定の根拠範囲を維持する。",
                    "use_rag": False,
                    "use_online_research": False,
                    "use_llm_jp": False,
                },
            )

        self._drain(limit=40)

        with self.databases.state.connect() as connection:
            snapshot = RunRepository(connection).snapshot(run.run_id)
            no_change_events = connection.execute(
                """
                SELECT COUNT(*) AS count FROM event_log
                WHERE run_id = ?
                  AND event_type = 'manuscript.coherence.no_state_change'
                """,
                (run.run_id,),
            ).fetchone()["count"]
            checkpoints = connection.execute(
                """
                SELECT status, candidate_sha256, repair_history_json
                FROM manuscript_coherence_checkpoints
                WHERE run_id = ? AND stage = 'post_polish'
                ORDER BY round_index
                """,
                (run.run_id,),
            ).fetchall()

        self.assertEqual(snapshot["run"]["status"], "failed")
        self.assertEqual(translator.post_polish_calls, 1)
        self.assertEqual(no_change_events, 1)
        self.assertEqual(len(checkpoints), 1)
        self.assertEqual(checkpoints[0]["status"], "stalled")
        history = json.loads(checkpoints[0]["repair_history_json"])
        self.assertEqual(history[-1]["outcome"], "surface_rejected")

    def test_research_query_budget_round_robins_across_scopes(self) -> None:
        candidates = [
            {
                "query": f"scope-one-{index}",
                "scope_key": "section-1",
                "severity": "critical",
                "requires_research": True,
            }
            for index in range(4)
        ]
        candidates.extend(
            {
                "query": f"scope-{scope}",
                "scope_key": f"section-{scope}",
                "severity": "major",
                "requires_research": True,
            }
            for scope in range(2, 5)
        )

        selected = self.worker._allocate_manuscript_research_queries(candidates, limit=4)

        self.assertEqual(
            {item["scope_key"] for item in selected[:4]},
            {"section-1", "section-2", "section-3", "section-4"},
        )
        self.assertEqual(len(selected), 7)
        self.assertTrue(all(item["requires_research"] for item in selected))

    def test_required_research_exceeds_cap_without_admitting_optional_queries(self) -> None:
        candidates = [
            {
                "query": f"required-{index}",
                "scope_key": f"section-{index}",
                "severity": "major",
                "requires_research": True,
            }
            for index in range(3)
        ]
        candidates.append(
            {
                "query": "optional-discovery",
                "scope_key": "document",
                "severity": "minor",
                "requires_research": False,
            }
        )

        selected = self.worker._allocate_manuscript_research_queries(candidates, limit=2)

        self.assertEqual(len(selected), 3)
        self.assertTrue(all(item["requires_research"] for item in selected))

    def test_evidence_reranking_rejects_provider_rank_without_term_overlap(self) -> None:
        relevant, matched = self.worker._manuscript_relevance_score(
            "Honneth recognition theory third place",
            title="Recognition and the social value of third places",
            authors=("Axel Honneth",),
            excerpt="A study of recognition theory.",
            rank=3,
            semantic=False,
        )
        unrelated, unrelated_terms = self.worker._manuscript_relevance_score(
            "Honneth recognition theory third place",
            title="Marine sediment chemistry",
            authors=("Example Author",),
            excerpt="An unrelated result returned at rank one.",
            rank=1,
            semantic=False,
        )

        self.assertGreaterEqual(relevant, 0.2)
        self.assertTrue(matched)
        self.assertLess(unrelated, 0.2)
        self.assertFalse(unrelated_terms)

    def test_evidence_quality_requires_claim_anchor_and_material_type(self) -> None:
        query = "矢口高雄 原稿研究 制作証言"
        unrelated = self.worker._manuscript_evidence_quality(
            query,
            {
                "title": "Art Worlds",
                "authors": ["Howard S. Becker"],
                "excerpt": "A general account of artistic production and cooperation.",
                "confidence": 0.82,
            },
        )
        relevant = self.worker._manuscript_evidence_quality(
            query,
            {
                "title": "矢口高雄の原稿研究",
                "authors": ["研究者"],
                "excerpt": "制作証言と作画原稿を比較する。",
                "confidence": 0.82,
            },
        )

        self.assertFalse(unrelated["passed"])
        self.assertIn("claim_anchor_missing", unrelated["reasons"])
        self.assertTrue(relevant["passed"])
        self.assertEqual(relevant["matched_anchors"], ["矢口高雄"])

    def test_authoring_evidence_quality_uses_bounded_retrieval_query(self) -> None:
        verification_query = (
            "80年代のヤング誌は、少年誌と古参青年誌のあいだに若い大人向けの場を"
            "作り、都市・恋愛・仕事・消費・サブカルを同一誌面に混在させた"
        )
        score, matched, retrieval_query = self.worker._manuscript_best_relevance_score(
            [verification_query, "1980年代 ヤング誌 編成史"],
            title="1980年代ヤング誌の編成史",
            authors=(),
            excerpt="青年漫画雑誌の誌面編成と読者層を検討する。",
            rank=1,
            semantic=False,
        )
        quality = self.worker._manuscript_evidence_quality(
            verification_query,
            {
                "title": "1980年代ヤング誌の編成史",
                "authors": [],
                "excerpt": "青年漫画雑誌の誌面編成と読者層を検討する。",
                "confidence": score,
                "provenance": {"retrieval_query": retrieval_query},
            },
        )

        self.assertEqual(retrieval_query, "1980年代 ヤング誌 編成史")
        self.assertTrue(matched)
        self.assertTrue(quality["passed"])
        self.assertEqual(quality["verification_query"], verification_query)

    def test_completion_gate_never_hides_unallocated_required_evidence(self) -> None:
        context = {
            "proposed_html": (
                '<article id="D" lang="ja"><section id="S">'
                '<p id="P">整った文章である。</p></section></article>'
            ),
            "research_query_candidates": [
                {
                    "query": "required academic evidence",
                    "query_valid": True,
                    "scope_key": "section-1",
                    "requires_research": True,
                }
            ],
            "research_query_assignments": [],
            "paragraph_plan_validation": {
                "fallback_used": False,
                "issues": [],
                "global_boundary_validation": {"valid": True, "issues": []},
            },
            "predicate_relation_resolution": {"human_review_count": 0},
            "manuscript_polish": {"rejected_block_count": 0},
            "manuscript_surface_quality_gate": {
                "reverted_blocks": [],
                "remaining_issue_blocks": [],
            },
            "manuscript_coherence_checks": [
                {
                    "passed": True,
                    "checked_block_ids": ["P"],
                    "coverage": [
                        {
                            "category": category,
                            "checked": True,
                            "note": "Fixture audit.",
                        }
                        for category in (
                            "temporal_conflict",
                            "connector_mismatch",
                            "orphan_example",
                            "missing_warrant",
                            "argument_gap",
                            "section_transition",
                            "duplication",
                            "ordering",
                        )
                    ],
                    "issues": [],
                    "repair_edits": [],
                    "section_moves": [],
                }
            ],
        }

        gate = self.worker._manuscript_completion_gate(context)

        self.assertEqual(gate["quality_status"], "needs_human_review")
        self.assertIn(
            "evidence.query_quota_exhausted",
            {item["code"] for item in gate["blocking_reasons"]},
        )

    def test_completion_gate_does_not_promote_preferred_inference_to_required(self) -> None:
        gate = self.worker._manuscript_completion_gate(
            {
                "proposed_html": (
                    '<article id="D" lang="ja"><section id="S">'
                    '<p id="P">限定を明示した解釈である。</p></section></article>'
                ),
                "research_query_candidates": [
                    {
                        "query": "interpretive claim",
                        "query_valid": True,
                        "scope_key": "claim:c1",
                        "finding_id": "c1",
                        "requires_research": True,
                        "evidence_requirement": "preferred",
                    }
                ],
                "research_query_assignments": [
                    {
                        "query": "interpretive claim",
                        "scope_key": "claim:c1",
                        "finding_id": "c1",
                        "requires_research": True,
                        "evidence_requirement": "preferred",
                    }
                ],
            }
        )

        evidence_codes = {
            item["code"]
            for item in gate["blocking_reasons"]
            if str(item["code"]).startswith("evidence.")
        }
        self.assertEqual(evidence_codes, set())

    def test_completion_gate_fails_closed_when_stage_records_are_absent(self) -> None:
        gate = self.worker._manuscript_completion_gate(
            {
                "proposed_html": (
                    '<article id="D" lang="ja"><section id="S">'
                    '<p id="P">整った文章である。</p></section></article>'
                )
            }
        )

        codes = {item["code"] for item in gate["blocking_reasons"]}
        self.assertEqual(gate["quality_status"], "needs_human_review")
        self.assertTrue(
            {
                "review.execution_record_missing",
                "structure.execution_record_missing_or_invalid",
                "paragraph.execution_record_missing_or_invalid",
                "predicate.execution_record_missing",
                "polish.execution_record_missing_or_invalid",
                "surface.execution_record_missing_or_failed",
                "coherence.final_not_passed",
            }.issubset(codes)
        )

    def test_completion_gate_accepts_post_footnote_as_final_coherence_stage(self) -> None:
        canonical = (
            '<article id="D" lang="ja"><section id="S">'
            '<p id="P">整った文章である。</p></section></article>'
        )
        coverage = [
            {"category": category, "checked": True, "note": "Fixture audit."}
            for category in (
                "temporal_conflict",
                "connector_mismatch",
                "orphan_example",
                "missing_warrant",
                "argument_gap",
                "section_transition",
                "duplication",
                "ordering",
            )
        ]
        checks = [
            {
                "stage": stage,
                "passed": True,
                "checked_block_ids": ["P"],
                "coverage": coverage,
                "issues": [],
                "repair_edits": [],
                "section_moves": [],
            }
            for stage in ("pre_polish", "post_footnote")
        ]

        gate = self.worker._manuscript_completion_gate(
            {"proposed_html": canonical, "manuscript_coherence_checks": checks}
        )

        codes = {item["code"] for item in gate["blocking_reasons"]}
        self.assertNotIn("coherence.stage_coverage_missing", codes)

    def test_completion_gate_requires_v29_japanese_audits_with_v30_proofreading(self) -> None:
        canonical = (
            '<article id="D" lang="ja"><section id="S">'
            '<p id="P">整った文章である。</p></section></article>'
        )
        gate = self.worker._manuscript_completion_gate(
            {
                "proposed_html": canonical,
                "authoring_workflow_policy": {
                    "id": "fukamiru-authoring-v30",
                    "workflow_version": 30,
                },
                "manuscript_proofreading_shadow": {
                    "policy": "production-shadow-full-sentence-v29.1",
                    "generation_error_sentence_count": 0,
                    "selected_count": 1,
                },
                "manuscript_proofreading_selection": {
                    "policy": "independent-candidate-selection-v29.1",
                    "selected_candidate_count": 1,
                },
                "manuscript_proofreading_luna": {"missing_result_count": 0},
                "manuscript_proofreading_selected_preview_text": {"P": "整った文章である。"},
                "proofreading_application": {
                    "selected_count": 1,
                    "applied_count": 1,
                    "source_html_sha256": "source",
                    "target_html_sha256": "target",
                },
            }
        )

        codes = {item["code"] for item in gate["blocking_reasons"]}
        self.assertIn("predicate.execution_record_missing", codes)
        self.assertIn("polish.execution_record_missing_or_invalid", codes)
        self.assertNotIn("proofreading.execution_record_missing_or_invalid", codes)

    def test_completion_gate_accepts_v29_japanese_audits_with_v30_proofreading(self) -> None:
        canonical = (
            '<article id="D" lang="ja"><section id="S">'
            '<p id="P">整った文章である。</p></section></article>'
        )
        gate = self.worker._manuscript_completion_gate(
            {
                "proposed_html": canonical,
                "authoring_workflow_policy": {
                    "id": "fukamiru-authoring-v30",
                    "workflow_version": 30,
                },
                "predicate_relation_resolution": {
                    "candidate_count": 0,
                    "decision_count": 0,
                    "human_review_count": 0,
                    "decisions": [],
                    "issues": [],
                    "post_resolution_candidate_count": 0,
                    "post_resolution_inventory": [],
                },
                "manuscript_polish": {
                    "enabled": True,
                    "zero_rejection_required": True,
                    "rejection_rate": 0.0,
                    "rejected_block_count": 0,
                    "waiting_human_block_count": 0,
                },
                "manuscript_proofreading_shadow": {
                    "policy": "production-shadow-full-sentence-v29.1",
                    "generation_error_sentence_count": 0,
                    "selected_count": 1,
                },
                "manuscript_proofreading_selection": {
                    "policy": "independent-candidate-selection-v29.1",
                    "selected_candidate_count": 1,
                },
                "manuscript_proofreading_luna": {"missing_result_count": 0},
                "manuscript_proofreading_selected_preview_text": {"P": "整った文章である。"},
                "proofreading_application": {
                    "selected_count": 1,
                    "applied_count": 1,
                    "source_html_sha256": "source",
                    "target_html_sha256": "target",
                },
            }
        )

        codes = {item["code"] for item in gate["blocking_reasons"]}
        self.assertNotIn("predicate.execution_record_missing", codes)
        self.assertNotIn("predicate.post_polish_roles_ungrounded", codes)
        self.assertNotIn("polish.execution_record_missing_or_invalid", codes)
        self.assertNotIn("proofreading.execution_record_missing_or_invalid", codes)

    def test_v22_completion_gate_blocks_semantic_resolution_required(self) -> None:
        condition = {
            "reason": "semantic_resolution_required",
            "phase": "argument_process:claim_evidence_links",
            "unit_ids": ["argument-link-unit:fixture"],
            "block_ids": ["P"],
            "disposition": "human_judgement_required",
        }

        gate = self.worker._manuscript_completion_gate(
            {
                "workflow_version": 22,
                "proposed_html": (
                    '<article id="D" lang="ja"><section id="S">'
                    '<p id="P">整った文章である。</p></section></article>'
                ),
                "conditional_conditions": [condition],
            }
        )

        self.assertEqual(gate["quality_status"], "needs_human_review")
        self.assertIn(
            "reasoning.semantic_resolution_required:argument_process:claim_evidence_links",
            {item["code"] for item in gate["blocking_reasons"]},
        )

    def test_completion_gate_rejects_covered_flag_without_evidence_record(self) -> None:
        assignment = {
            "query": "Honneth recognition third place",
            "scope_key": "section-1",
            "finding_id": "F-1",
            "requires_research": True,
        }
        gate = self.worker._manuscript_completion_gate(
            {
                "proposed_html": (
                    '<article id="D" lang="ja"><section id="S">'
                    '<p id="P">整った文章である。</p></section></article>'
                ),
                "research_query_assignments": [assignment],
                "evidence_coverage": [
                    {
                        **assignment,
                        "covered": True,
                        "accepted_evidence_count": 1,
                    }
                ],
                "evidence_context": [],
            }
        )

        self.assertIn(
            "evidence.coverage_missing",
            {item["code"] for item in gate["blocking_reasons"]},
        )

    def test_completion_gate_detects_post_polish_relation_inventory_change(self) -> None:
        baseline_html = (
            '<article id="D" lang="ja"><section id="S">'
            '<p id="P">この結果は前提を説明する。</p></section></article>'
        )
        final_html = baseline_html.replace(
            "この結果は前提を説明する。",
            "しかし、この結果は前提を説明する。",
        )
        candidate = ManuscriptDiscourseService.predicate_candidates(baseline_html)[0]
        baseline = self.worker._manuscript_predicate_inventory_record(candidate)
        gate = self.worker._manuscript_completion_gate(
            {
                "proposed_html": final_html,
                "predicate_relation_resolution": {
                    "candidate_count": 1,
                    "decision_count": 1,
                    "human_review_count": 0,
                    "issues": [],
                    "post_resolution_candidate_count": 1,
                    "post_resolution_inventory": [baseline],
                    "decisions": [
                        {
                            "block_id": "P",
                            "action": "preserve",
                            "semantic_roles": {
                                "subject": "この結果",
                                "affected_entity": "前提",
                            },
                        }
                    ],
                },
            }
        )

        self.assertIn(
            "predicate.post_polish_inventory_changed_without_candidate_audit",
            {item["code"] for item in gate["blocking_reasons"]},
        )

    def test_completion_gate_rechecks_predicate_role_shape_after_polish(self) -> None:
        html = (
            '<article id="D" lang="ja"><section id="S">'
            '<p id="P">羞恥と支配が主体の回復に接続される。</p></section></article>'
        )
        candidate = ManuscriptDiscourseService.predicate_candidates(html)[0]
        baseline = self.worker._manuscript_predicate_inventory_record(candidate)
        source = str(candidate["source_text"])
        gate = self.worker._manuscript_completion_gate(
            {
                "proposed_html": html,
                "predicate_relation_resolution": {
                    "candidate_count": 1,
                    "decision_count": 1,
                    "human_review_count": 0,
                    "issues": [],
                    "post_resolution_candidate_count": 1,
                    "post_resolution_inventory": [baseline],
                    "decisions": [
                        {
                            "block_id": "P",
                            "action": "preserve",
                            "semantic_roles": {
                                "subject": source,
                                "affected_entity": source,
                            },
                        }
                    ],
                },
            }
        )

        self.assertIn(
            "predicate.post_polish_roles_ungrounded",
            {item["code"] for item in gate["blocking_reasons"]},
        )

    def test_coherence_contract_requires_full_block_and_category_coverage(self) -> None:
        incomplete = {
            "passed": True,
            "checked_block_ids": ["P-1", "UNKNOWN"],
            "coverage": [
                {
                    "category": "ordering",
                    "checked": True,
                    "note": "Only one category was checked.",
                }
            ],
            "issues": [],
            "repair_edits": [],
            "section_moves": [],
        }

        issues = self.worker._coherence_contract_issues(
            incomplete, expected_block_ids={"P-1", "P-2"}
        )

        codes = {item["code"] for item in issues}
        self.assertIn("coherence.block_coverage_missing", codes)
        self.assertIn("coherence.unknown_checked_block", codes)
        self.assertIn("coherence.category_coverage_missing", codes)
        self.assertFalse(
            self.worker._coherence_report_passed(incomplete, expected_block_ids={"P-1", "P-2"})
        )

    def test_coherence_repair_requires_a_reported_issue(self) -> None:
        report = {
            "passed": False,
            "summary": "A repair was proposed without identifying a defect.",
            "checked_block_ids": ["P-1"],
            "coverage": [
                {"category": category, "checked": True, "note": "Fixture audit."}
                for category in (
                    "temporal_conflict",
                    "connector_mismatch",
                    "orphan_example",
                    "missing_warrant",
                    "argument_gap",
                    "section_transition",
                    "duplication",
                    "ordering",
                )
            ],
            "issues": [],
            "repair_edits": [
                {
                    "operation": "replace_text",
                    "block_id": "P-1",
                    "anchor_block_id": None,
                    "text": "Untraceable repair.",
                    "review_finding_ids": [],
                    "evidence_ids": [],
                }
            ],
            "section_moves": [],
        }

        codes = {
            item["code"]
            for item in self.worker._coherence_contract_issues(
                report,
                expected_block_ids={"P-1"},
            )
        }

        self.assertIn("coherence.repair_without_issue", codes)

    def test_missing_warrant_without_repair_routes_to_additional_research(self) -> None:
        report = self.worker._normalize_coherence_resolution_routes(
            {
                "passed": False,
                "summary": "A comparison lacks a scholarly warrant.",
                "checked_block_ids": ["P-1"],
                "coverage": [
                    {"category": category, "checked": True, "note": "Fixture audit."}
                    for category in (
                        "temporal_conflict",
                        "connector_mismatch",
                        "orphan_example",
                        "missing_warrant",
                        "argument_gap",
                        "section_transition",
                        "duplication",
                        "ordering",
                    )
                ],
                "issues": [
                    {
                        "id": "warrant-1",
                        "category": "missing_warrant",
                        "severity": "blocking",
                        "block_ids": ["P-1"],
                        "explanation": "The comparison has no common evidence base.",
                        "recommendation": "Research comparable works and reception data.",
                    }
                ],
                "repair_edits": [],
                "section_moves": [],
            }
        )

        issue = report["issues"][0]
        self.assertEqual(issue["resolution_route"], "additional_research")
        self.assertTrue(issue["research_query"])
        self.assertFalse(self.worker._coherence_contract_issues(report, expected_block_ids={"P-1"}))

    def test_reasoning_review_contract_rejects_silent_id_and_query_bypasses(self) -> None:
        result = {
            "assessment": "Checked.",
            "checked_block_ids": ["P-1"],
            "findings": [
                {
                    "id": "F-1",
                    "category": "evidence",
                    "severity": "major",
                    "block_ids": ["UNKNOWN"],
                    "explanation": "Evidence is missing.",
                    "recommendation": "Research it.",
                    "requires_research": True,
                    "research_queries": ["研究"],
                }
            ],
            "rewrite_priorities": [],
            "research_queries": [],
        }

        issues = self.worker._manuscript_review_contract_issues(
            result,
            expected_block_ids={"P-1", "P-2"},
            batch_index=1,
        )

        codes = {item["code"] for item in issues}
        self.assertIn("review.block_coverage_missing", codes)
        self.assertIn("review.finding_unknown_block", codes)
        self.assertIn("review.required_query_not_specific", codes)

    def test_model_contract_values_are_validated_without_schema_trust(self) -> None:
        review = {
            "assessment": "Checked.",
            "checked_block_ids": ["P-1"],
            "findings": [
                {
                    "id": "F-1",
                    "category": "invented-category",
                    "severity": "negligible",
                    "block_ids": ["P-1"],
                    "explanation": "Fixture.",
                    "recommendation": "Fixture.",
                    "requires_research": "false",
                    "research_queries": [],
                }
            ],
            "rewrite_priorities": [],
            "research_queries": [],
        }
        review_codes = {
            item["code"]
            for item in self.worker._manuscript_review_contract_issues(
                review,
                expected_block_ids={"P-1"},
                batch_index=1,
            )
        }
        coherence = {
            "passed": "true",
            "summary": "Checked.",
            "checked_block_ids": ["P-1"],
            "coverage": [
                {"category": category, "checked": True, "note": "Fixture audit."}
                for category in (
                    "temporal_conflict",
                    "connector_mismatch",
                    "orphan_example",
                    "missing_warrant",
                    "argument_gap",
                    "section_transition",
                    "duplication",
                    "ordering",
                )
            ],
            "issues": [
                {
                    "id": "C-1",
                    "category": "invented-category",
                    "severity": "minor",
                    "block_ids": ["P-1"],
                    "explanation": "Fixture.",
                    "recommendation": "Fixture.",
                }
            ],
            "repair_edits": [],
            "section_moves": [],
        }
        coherence_codes = {
            item["code"]
            for item in self.worker._coherence_contract_issues(
                coherence,
                expected_block_ids={"P-1"},
            )
        }

        self.assertTrue(
            {
                "review.finding_category_invalid",
                "review.finding_severity_invalid",
                "review.finding_research_flag_invalid",
            }.issubset(review_codes)
        )
        self.assertTrue(
            {
                "coherence.passed_flag_invalid",
                "coherence.issue_category_invalid",
                "coherence.issue_severity_invalid",
            }.issubset(coherence_codes)
        )

    def test_evidence_finding_cannot_opt_out_of_research(self) -> None:
        result = {
            "assessment": "Checked.",
            "checked_block_ids": ["P-1"],
            "findings": [
                {
                    "id": "F-1",
                    "category": "evidence",
                    "severity": "major",
                    "block_ids": ["P-1"],
                    "explanation": "The claim is unsupported.",
                    "recommendation": "Find a scholarly source.",
                    "requires_research": False,
                    "research_queries": [],
                }
            ],
            "rewrite_priorities": [],
            "research_queries": [],
        }

        codes = {
            item["code"]
            for item in self.worker._manuscript_review_contract_issues(
                result,
                expected_block_ids={"P-1"},
                batch_index=1,
            )
        }

        self.assertIn("review.evidence_finding_research_bypassed", codes)

    def test_edit_cannot_claim_a_finding_from_an_unrelated_block(self) -> None:
        edits, warnings = self.worker._normalize_manuscript_edits(
            [
                {
                    "operation": "replace_text",
                    "block_id": "P-2",
                    "anchor_block_id": None,
                    "text": "Unrelated edit.",
                    "review_finding_ids": ["F-1"],
                    "evidence_ids": [],
                }
            ],
            reasoning_review=[
                {
                    "findings": [
                        {
                            "id": "F-1",
                            "block_ids": ["P-1"],
                            "requires_research": False,
                        }
                    ]
                }
            ],
            evidence_context=[],
        )

        self.assertEqual(edits, [])
        self.assertTrue(any("unrelated reasoning findings" in item for item in warnings))

    def test_draft_edit_cannot_omit_its_reasoning_finding(self) -> None:
        edits, warnings = self.worker._normalize_manuscript_edits(
            [
                {
                    "operation": "replace_text",
                    "block_id": "P-1",
                    "text": "Untraceable edit.",
                    "review_finding_ids": [],
                    "evidence_ids": [],
                }
            ],
            reasoning_review=[],
            evidence_context=[],
            require_finding_reference=True,
        )

        self.assertEqual(edits, [])
        self.assertTrue(any("no applicable reasoning finding" in item for item in warnings))

    def test_edit_cannot_attach_evidence_from_another_finding(self) -> None:
        reviews = [
            {
                "findings": [
                    {
                        "id": "F-1",
                        "block_ids": ["P-1"],
                        "requires_research": False,
                    },
                    {
                        "id": "F-2",
                        "block_ids": ["P-1"],
                        "requires_research": True,
                    },
                ]
            }
        ]
        edits, warnings = self.worker._normalize_manuscript_edits(
            [
                {
                    "operation": "replace_text",
                    "block_id": "P-1",
                    "text": "Misattributed evidence.",
                    "review_finding_ids": ["F-1"],
                    "evidence_ids": ["E-2"],
                }
            ],
            reasoning_review=reviews,
            evidence_context=[{"id": "E-2", "finding_key": "F-2"}],
            require_finding_reference=True,
        )

        self.assertEqual(edits, [])
        self.assertTrue(any("different finding" in item for item in warnings))

    def test_duplicate_research_query_does_not_cover_a_second_obligation(self) -> None:
        candidates = [
            {
                "query": "Honneth recognition third place",
                "scope_key": "section:1",
                "finding_id": "F-1",
                "requires_research": True,
                "query_valid": True,
            },
            {
                "query": "Honneth recognition third place",
                "scope_key": "section:2",
                "finding_id": "F-2",
                "requires_research": True,
                "query_valid": True,
            },
        ]
        assignments = self.worker._allocate_manuscript_research_queries(candidates, limit=8)
        context = {
            "proposed_html": (
                '<article id="D" lang="ja"><section id="S">'
                '<p id="P">整った文章である。</p></section></article>'
            ),
            "research_query_candidates": candidates,
            "research_query_assignments": assignments,
            "paragraph_plan_validation": {
                "fallback_used": False,
                "issues": [],
                "global_boundary_validation": {"valid": True, "issues": []},
            },
            "predicate_relation_resolution": {"human_review_count": 0},
            "manuscript_polish": {"rejected_block_count": 0},
            "manuscript_surface_quality_gate": {
                "reverted_blocks": [],
                "remaining_issue_blocks": [],
            },
            "manuscript_coherence_checks": [
                {
                    "passed": True,
                    "checked_block_ids": ["P"],
                    "coverage": [
                        {
                            "category": category,
                            "checked": True,
                            "note": "Fixture audit.",
                        }
                        for category in (
                            "temporal_conflict",
                            "connector_mismatch",
                            "orphan_example",
                            "missing_warrant",
                            "argument_gap",
                            "section_transition",
                            "duplication",
                            "ordering",
                        )
                    ],
                    "issues": [],
                    "repair_edits": [],
                    "section_moves": [],
                }
            ],
        }

        gate = self.worker._manuscript_completion_gate(context)

        self.assertEqual(len(assignments), 1)
        self.assertIn(
            "evidence.query_quota_exhausted",
            {item["code"] for item in gate["blocking_reasons"]},
        )

    def test_manuscript_human_review_precedes_research_rewrite_and_llm_jp(self) -> None:
        project_root = Path(self.temp.name) / "reasoned-rewrite-project"
        translator = CapturingManuscriptTranslator()
        self.worker.translator = translator
        local_llm = RejectOnceThenNoopLocalLLM()
        self.worker.llm_jp = local_llm
        human_review = (
            "段落を区切りすぎない。サードプレイス論とホネットの承認論の関連性を調べて繋げて。"
        )
        with self.databases.state.connect() as connection:
            project = ProjectService(connection).create(
                name="Reasoned rewrite", root_path=project_root
            )
            revisions = RevisionService(connection)
            document_id = revisions.create_document(
                project_id=project["id"], kind="paper", title="Reasoned draft"
            )
            base = revisions.commit_revision(
                document_id=document_id,
                canonical_html=(
                    '<article id="DOC-REASONED" lang="ja">'
                    '<section id="SEC-REASONED"><h2 id="H-REASONED">論点</h2>'
                    '<p id="P-REASONED">羞恥や支配が主体の回復に接続されるという主張。</p>'
                    '<p id="P-REASONED-2">隣接する論証。</p></section></article>'
                ),
                editor_type="human",
                operation="human_import",
            )
            definition = self.workflows["manuscript_rewrite"]
            run = RunRepository(connection).create(
                workflow_name=definition.name,
                workflow_version=definition.version,
                entry_node=definition.entry_node,
                project_id=project["id"],
                inputs={
                    "document_id": document_id,
                    "base_revision_id": base.revision_id,
                    "instructions": "人間レビューを最優先して論理構造から改稿する。",
                    "human_review": human_review,
                    "use_rag": True,
                    "use_online_research": False,
                    "use_llm_jp": True,
                },
            )

        self._drain(limit=30)

        with self.databases.state.connect() as connection:
            repository = RunRepository(connection)
            snapshot = repository.snapshot(run.run_id)
            progress = next(
                item["progress"]
                for item in repository.list_progress(limit=20)
                if item["run"]["id"] == run.run_id
            )
            review = connection.execute(
                """
                SELECT human_review, research_queries_json
                FROM manuscript_reasoning_reviews WHERE run_id = ?
                """,
                (run.run_id,),
            ).fetchone()
            candidate_targets = connection.execute(
                """
                SELECT COUNT(*) AS count FROM manuscript_polish_targets
                WHERE run_id = ?
                """,
                (run.run_id,),
            ).fetchone()["count"]
            proposal = ManuscriptRewriteService(connection).get_by_run(run.run_id)
        self.assertEqual(snapshot["run"]["status"], "completed")
        self.assertEqual(progress["phase"], "persist_rewrite_proposal")
        self.assertEqual(progress["percentage"], 100.0)
        self.assertEqual(review["human_review"], human_review)
        self.assertIn("サードプレイス", review["research_queries_json"])
        self.assertGreater(candidate_targets, 0)
        self.assertEqual(proposal["status"], "needs_human_review")
        self.assertEqual(proposal["context"]["llm_jp_polish"]["rejected_block_count"], 0)
        self.assertEqual(proposal["context"]["llm_jp_polish"]["rejection_attempt_count"], 0)
        self.assertEqual(
            proposal["context"]["llm_jp_polish"]["mode"],
            "constrained_candidate_generation",
        )
        self.assertTrue(translator.review_calls)
        self.assertEqual(translator.review_calls[0]["human_review"], human_review)
        self.assertTrue(translator.rewrite_calls)
        self.assertEqual(translator.rewrite_calls[0]["human_review"], human_review)
        self.assertTrue(translator.rewrite_calls[0]["paragraph_plan"])
        self.assertTrue(local_llm.calls)
        self.assertTrue(all(int(call["max_tokens"]) >= 1536 for call in local_llm.calls))

    def test_manuscript_polish_skips_heading_and_falls_back_after_candidate_limit(
        self,
    ) -> None:
        project_root = Path(self.temp.name) / "finite-polish-project"
        self.worker.translator = PredicateResolvingTranslator()
        local_llm = AlwaysInvalidEditLocalLLM()
        self.worker.llm_jp = local_llm
        with self.databases.state.connect() as connection:
            project = ProjectService(connection).create(
                name="Finite polish", root_path=project_root
            )
            revisions = RevisionService(connection)
            document_id = revisions.create_document(
                project_id=project["id"], kind="paper", title="Finite polish draft"
            )
            base = revisions.commit_revision(
                document_id=document_id,
                canonical_html=(
                    '<article id="DOC-FINITE" lang="ja">'
                    '<section id="SEC-FINITE">'
                    '<h2 id="H-FINITE-STRUCTURAL">章見出し</h2>'
                    '<p id="H-FINITE">1　意味上の見出し</p>'
                    '<p id="P-FINITE">本文は論旨を示している。</p>'
                    "</section></article>"
                ),
                editor_type="human",
                operation="human_import",
            )
            definition = self.workflows["manuscript_rewrite"]
            run = RunRepository(connection).create(
                workflow_name=definition.name,
                workflow_version=definition.version,
                entry_node=definition.entry_node,
                project_id=project["id"],
                inputs={
                    "document_id": document_id,
                    "base_revision_id": base.revision_id,
                    "instructions": "論旨を変えずに推敲する。",
                    "human_review": "見出しは本文として推敲しない。",
                    "use_rag": False,
                    "use_online_research": False,
                    "use_llm_jp": True,
                },
            )

        self._drain(limit=50)

        with self.databases.state.connect() as connection:
            snapshot = RunRepository(connection).snapshot(run.run_id)
            checkpoint_sources = [
                str(row["source_text"])
                for row in connection.execute(
                    """
                    SELECT source_text FROM manuscript_polish_targets
                    WHERE run_id = ? ORDER BY created_at
                    """,
                    (run.run_id,),
                )
            ]
            review = connection.execute(
                """
                SELECT id, object_type FROM human_reviews
                WHERE run_id = ? AND status = 'open'
                """,
                (run.run_id,),
            ).fetchone()
        self.assertEqual(snapshot["run"]["status"], "completed")
        self.assertIsNone(review)
        self.assertFalse(any("意味上の見出し" in text for text in checkpoint_sources))
        self.assertEqual(
            len(local_llm.calls),
            self.settings.translation.manuscript_candidate_generation_attempts,
        )
        with self.databases.state.connect() as connection:
            proposal = ManuscriptRewriteService(connection).get_by_run(run.run_id)
        polish = proposal["context"]["llm_jp_polish"]
        self.assertEqual(polish["excluded_semantic_heading_block_count"], 1)
        self.assertEqual(polish["mode"], "constrained_candidate_generation")
        self.assertGreaterEqual(polish["generation_issue_target_count"], 1)
        self.assertEqual(polish["rejected_block_count"], 0)
        self.assertGreaterEqual(polish["final_unresolved_defect_count"], 1)
        self.assertGreaterEqual(
            proposal["context"]["japanese_candidate_selection"]["original_preserved_target_count"],
            1,
        )
        self.assertIn("本文は論旨を示している。", proposal["proposed_html"])

    def test_resource_lanes_keep_translation_ingestion_independent_from_embedding(self) -> None:
        corpus_run = self._create_corpus_run()
        for _ in range(4):
            self.assertTrue(self.worker.run_once(lane="ingestion_cpu"))

        translation_run = self._create_run("translation")
        self.assertTrue(self.worker.run_once(lane="ingestion_cpu"))
        self.assertTrue(self.worker.run_once(lane="local_llm_gpu"))

        with self.databases.state.connect() as connection:
            corpus = RunRepository(connection).snapshot(corpus_run)
            translation = RunRepository(connection).snapshot(translation_run)
        self.assertEqual(corpus["run"]["status"], "running")
        self.assertEqual(corpus["active_nodes"][0]["node_id"], "build_semantic_index")
        self.assertEqual(corpus["active_nodes"][0]["resource_lane"], "embedding_gpu")
        self.assertEqual(translation["run"]["status"], "running")
        self.assertEqual(translation["active_nodes"][0]["node_id"], "research_trigger_detection")
        self.assertEqual(
            set(self.worker.status()["lanes"]),
            {"ingestion_cpu", "embedding_gpu", "codex_remote", "local_llm_gpu"},
        )

    def test_codex_pool_dynamically_reserves_a_slot_for_each_workload(self) -> None:
        translation_run = self._create_run("translation")
        manuscript_run = self._create_run("manuscript_rewrite")
        with self.databases.state.connect() as connection:
            connection.execute(
                """
                UPDATE node_runs SET node_id = 'draft_translation',
                    resource_lane = 'codex_remote'
                WHERE run_id = ?
                """,
                (translation_run,),
            )
            connection.execute(
                """
                UPDATE node_runs SET node_id = 'review_manuscript_reasoning',
                    resource_lane = 'codex_remote'
                WHERE run_id = ?
                """,
                (manuscript_run,),
            )
            connection.commit()

        first = self.worker._claim(lane="codex_remote")
        second = self.worker._claim(lane="codex_remote")

        assert first is not None
        assert second is not None
        self.assertEqual(first.workflow_name, "translation")
        self.assertEqual(second.workflow_name, "manuscript_rewrite")
        codex_status = self.worker.status()["lanes"]["codex_remote"]
        self.assertEqual(codex_status["configured_limit"], 5)
        self.assertEqual(codex_status["allocation"], "shared_dynamic")
        self.assertEqual(len(codex_status["slots"]), 5)

    def test_codex_rate_limit_requeues_same_attempt_and_degrades_pool(self) -> None:
        run_id = self._create_run("translation")
        with self.databases.state.connect() as connection:
            connection.execute(
                """
                UPDATE node_runs SET node_id = 'draft_translation',
                    resource_lane = 'codex_remote'
                WHERE run_id = ?
                """,
                (run_id,),
            )
            connection.commit()
        claimed = self.worker._claim(lane="codex_remote")
        assert claimed is not None

        error = RuntimeError("429 Too Many Requests: rate limit exceeded")
        self.worker._degrade_codex_concurrency(claimed, error)
        self.worker._yield_node(
            claimed,
            reason="codex_rate_limited",
            payload={"message": str(error)},
        )

        with self.databases.state.connect() as connection:
            node = connection.execute(
                "SELECT id, status, attempt FROM node_runs WHERE id = ?",
                (claimed.node_run_id,),
            ).fetchone()
            yielded = connection.execute(
                """
                SELECT payload_json FROM event_log
                WHERE run_id = ? AND event_type = 'node.yielded'
                ORDER BY event_id DESC LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        self.assertEqual(node["id"], claimed.node_run_id)
        self.assertEqual(node["status"], "queued")
        self.assertEqual(node["attempt"], claimed.attempt)
        self.assertEqual(json.loads(yielded["payload_json"])["reason"], "codex_rate_limited")
        codex_status = self.worker.status()["lanes"]["codex_remote"]
        self.assertEqual(codex_status["effective_limit"], 1)
        self.assertEqual(codex_status["recovery_limit"], 1)
        self.assertGreater(codex_status["retry_after_seconds"], 0)

        self.worker._codex_retry_after = 0
        self.worker._codex_degraded_until = time.monotonic() - 0.01
        recovering_status = self.worker.status()["lanes"]["codex_remote"]
        self.assertEqual(recovering_status["effective_limit"], 2)
        self.assertEqual(recovering_status["recovery_limit"], 2)
        self.assertGreater(recovering_status["next_recovery_in_seconds"], 0)

    def test_local_llm_uses_mutex_shared_with_embedding(self) -> None:
        observed: dict[str, object] = {}
        released: list[str] = []

        def operation() -> str:
            observed.update(self.worker.gpu_mutex.status())
            return "generated"

        result = self.worker.execute_local_llm(
            operation,
            model_name="llm-jp",
            release=lambda: released.append("stopped"),
        )

        self.assertEqual(result, "generated")
        self.assertTrue(observed["locked"])
        self.assertEqual(observed["owner"], "local_llm:llm-jp")
        self.assertFalse(self.worker.gpu_mutex.status()["locked"])
        self.assertEqual(released, ["stopped"])

    def test_only_expired_node_lease_is_recovered(self) -> None:
        run_id = self._create_run("ingestion")
        claimed = self.worker._claim(lane="ingestion_cpu")
        assert claimed is not None

        self.worker._recover_orphaned_nodes()
        with self.databases.state.connect() as connection:
            active = connection.execute(
                "SELECT status, lease_owner FROM node_runs WHERE id = ?",
                (claimed.node_run_id,),
            ).fetchone()
            self.assertEqual(active["status"], "running")
            self.assertEqual(active["lease_owner"], self.worker.worker_id)
            connection.execute(
                "UPDATE node_runs SET lease_expires_at = ? WHERE id = ?",
                ("1970-01-01T00:00:00.000Z", claimed.node_run_id),
            )
            connection.commit()

        self.worker._recover_orphaned_nodes()
        with self.databases.state.connect() as connection:
            recovered = connection.execute(
                "SELECT status, lease_owner, claim_generation FROM node_runs WHERE id = ?",
                (claimed.node_run_id,),
            ).fetchone()
            run = connection.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
        self.assertEqual(recovered["status"], "queued")
        self.assertIsNone(recovered["lease_owner"])
        self.assertEqual(recovered["claim_generation"], 1)
        self.assertEqual(run["status"], "queued")

    def test_expired_foreign_lease_is_recovered_after_worker_startup_window(self) -> None:
        run_id = self._create_run("ingestion")
        claimed = self.worker._claim(lane="ingestion_cpu")
        assert claimed is not None
        with self.databases.state.connect() as connection:
            connection.execute(
                """
                UPDATE node_runs
                SET lease_owner = 'worker-from-stopped-process',
                    lease_expires_at = '1970-01-01T00:00:00.000Z'
                WHERE id = ?
                """,
                (claimed.node_run_id,),
            )
            connection.commit()

        self.worker._next_recovery_check = 0.0
        recovered_count = self.worker._recover_expired_nodes_if_due()

        with self.databases.state.connect() as connection:
            recovered = connection.execute(
                "SELECT status, lease_owner FROM node_runs WHERE id = ?",
                (claimed.node_run_id,),
            ).fetchone()
            run = connection.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
        self.assertEqual(recovered_count, 1)
        self.assertEqual(recovered["status"], "queued")
        self.assertIsNone(recovered["lease_owner"])
        self.assertEqual(run["status"], "queued")

    def test_critical_ocr_defect_waits_for_review_and_resumes_same_run(self) -> None:
        run_id = self._create_run("ingestion")
        for _ in range(5):
            self.assertTrue(self.worker.run_once(lane="ingestion_cpu"))
        with self.databases.state.connect() as connection:
            snapshot = RunRepository(connection).snapshot(run_id)
        artifact = Path(snapshot["active_nodes"][0]["input"]["normalized_text_path"])
        payload = json.loads(artifact.read_text(encoding="utf-8"))
        payload["ocr_audit"]["findings"] = [
            {
                "defect_type": "page_missing",
                "severity": "critical",
                "page_start": 2,
                "page_end": 2,
                "excerpt": "2",
                "context": "pages=[1, 3]",
                "rationale": "Source page 2 is missing.",
                "repair_candidates": [],
                "confidence": 1.0,
                "automatic_repair_allowed": False,
            }
        ]
        payload["ocr_audit"]["has_critical"] = True
        artifact.write_text(json.dumps(payload), encoding="utf-8")

        self.assertTrue(self.worker.run_once(lane="ingestion_cpu"))
        with self.databases.state.connect() as connection:
            waiting = RunRepository(connection).snapshot(run_id)
        self.assertEqual(waiting["run"]["status"], "waiting_human")
        self.assertEqual(waiting["defects"][0]["defect_type"], "page_missing")
        self.assertEqual(waiting["reviews"][0]["object_type"], "ocr_revision")

        result = self.worker.resolve_review(waiting["reviews"][0]["id"], "approve")
        self.assertEqual(result["run_id"], run_id)
        with self.databases.state.connect() as connection:
            resumed = RunRepository(connection).snapshot(run_id)
        self.assertEqual(resumed["run"]["status"], "running")
        self.assertEqual(resumed["active_nodes"][0]["node_id"], "infer_structure")
        self.assertEqual(resumed["defects"][0]["status"], "accepted")

    def test_translation_creates_reviewable_then_final_artifact(self) -> None:
        run_id = self._create_run("translation")
        self._drain()
        with self.databases.state.connect() as connection:
            snapshot = RunRepository(connection).snapshot(run_id)
            ingestion_events = connection.execute(
                """
                SELECT COUNT(*) AS count FROM event_log
                WHERE run_id = ? AND event_type = 'ingestion.progress'
                """,
                (run_id,),
            ).fetchone()["count"]
        self.assertEqual(snapshot["run"]["status"], "waiting_human")
        self.assertGreater(ingestion_events, 0)
        self.assertEqual(snapshot["run"]["output"]["retrieval_mode"], "lexical_fallback")
        self.assertIsNone(snapshot["run"]["output"]["semantic_index_snapshot_id"])
        self.assertTrue(Path(snapshot["artifacts"][0]["path"]).is_file())
        review_id = snapshot["reviews"][0]["id"]

        approval = self.worker.resolve_review(review_id, "approve")
        self.assertTrue(Path(approval["output_path"]).is_file())
        self.assertEqual(
            Path(approval["output_path"]).parent,
            Path(approval["output_folder_path"]),
        )
        with self.databases.state.connect() as connection:
            approved = RunRepository(connection).snapshot(run_id)
        self.assertEqual(
            approved["run"]["output"]["approved_snapshot_path"],
            approval["output_path"],
        )
        self._drain()
        with self.databases.state.connect() as connection:
            snapshot = RunRepository(connection).snapshot(run_id)
        self.assertEqual(snapshot["run"]["status"], "completed")
        self.assertEqual(
            [item["artifact_type"] for item in snapshot["artifacts"]],
            ["translation_draft", "translation_approved", "translation_final"],
        )
        self.assertTrue(Path(snapshot["run"]["output"]["translation_final_path"]).is_file())

    def test_legacy_approval_without_output_can_be_recovered(self) -> None:
        run_id = self._create_run("translation")
        self._drain()
        with self.databases.state.connect() as connection:
            waiting = RunRepository(connection).snapshot(run_id)
            review_id = waiting["reviews"][0]["id"]
            node_id = waiting["reviews"][0]["node_run_id"]
            now = utc_now()
            connection.execute(
                """
                UPDATE human_reviews
                SET status = 'approved', response_json = '{"decision":"approved"}',
                    resolved_at = ?
                WHERE id = ?
                """,
                (now, review_id),
            )
            connection.execute(
                "UPDATE node_runs SET status = 'completed', completed_at = ? WHERE id = ?",
                (now, node_id),
            )
            connection.execute(
                """
                UPDATE runs SET status = 'failed', error_json = ?, completed_at = ?
                WHERE id = ?
                """,
                ('{"type":"LegacyFinalizationFailure"}', now, run_id),
            )
            connection.commit()

        recovered = self.worker.recover_approved_review_output(review_id)

        self.assertEqual(recovered["status"], "running")
        self.assertTrue(Path(recovered["output_path"]).is_file())
        self._drain()
        with self.databases.state.connect() as connection:
            completed = RunRepository(connection).snapshot(run_id)
        self.assertEqual(completed["run"]["status"], "completed")
        self.assertTrue(Path(completed["run"]["output"]["translation_final_path"]).is_file())

    def test_translation_persists_sentence_order_alignments(self) -> None:
        self.worker.translator = OrderPreservingTranslator()
        run_id = self._create_run("translation")
        self._drain()
        with self.databases.state.connect() as state:
            snapshot = RunRepository(state).snapshot(run_id)
        with self.databases.knowledge.connect() as knowledge:
            rows = knowledge.execute(
                """
                SELECT alignment_status, source_text, target_text, source_span_ids_json
                FROM translation_segment_alignments
                WHERE translation_version_id = ? ORDER BY segment_ordinal
                """,
                (snapshot["run"]["output"]["translation_version_id"],),
            ).fetchall()
        self.assertGreaterEqual(len(rows), 3)
        self.assertEqual({row["alignment_status"] for row in rows}, {"sentence_order"})
        self.assertEqual(rows[0]["source_text"], "Bonjour.")
        self.assertEqual(rows[0]["target_text"], "こんにちは.")
        self.assertTrue(json.loads(rows[0]["source_span_ids_json"]))

    def test_project_translation_finalization_creates_revision_and_git_checkpoint(self) -> None:
        project_root = Path(self.temp.name) / "project"
        with self.databases.state.connect() as connection:
            project = ProjectService(connection).create(
                name="Fixture project", root_path=project_root
            )
        run_id = self._create_run("translation", project_id=project["id"])
        self._drain()
        with self.databases.state.connect() as connection:
            waiting = RunRepository(connection).snapshot(run_id)
        self.worker.resolve_review(waiting["reviews"][0]["id"], "approve")
        self._drain()

        with self.databases.state.connect() as connection:
            completed = RunRepository(connection).snapshot(run_id)
            mapping = connection.execute(
                "SELECT * FROM run_documents WHERE run_id = ?", (run_id,)
            ).fetchone()
            revision = connection.execute(
                "SELECT * FROM revisions WHERE id = ?", (mapping["final_revision_id"],)
            ).fetchone()
        self.assertEqual(completed["run"]["status"], "completed")
        self.assertEqual(revision["operation"], "translation")
        self.assertEqual(
            completed["run"]["output"]["git_checkpoint_sha"],
            mapping["git_commit_sha"],
        )
        project_html = Path(completed["run"]["output"]["canonical_project_path"])
        self.assertTrue(project_html.is_file())
        self.assertTrue(mapping["git_commit_sha"])

    def test_translation_review_revise_keeps_run_and_persists_human_memory(self) -> None:
        run_id = self._create_run("translation")
        self._drain()
        with self.databases.state.connect() as connection:
            first = RunRepository(connection).snapshot(run_id)
        review_id = first["reviews"][0]["id"]
        original_version_id = first["run"]["output"]["translation_version_id"]
        corrected = (
            "これは人間が訂正した日本語訳です。原文の主張、数値、引用、"
            "モダリティを保ったまま自然な表現にしています。"
        )

        result = self.worker.resolve_review(
            review_id,
            "revise",
            feedback="語順とコロケーションを修正",
            corrected_text=corrected,
        )
        self.assertEqual(result["run_id"], run_id)
        self.assertEqual(result["next_node"], "apply_human_revision")
        self._drain()

        with self.databases.state.connect() as connection:
            revised = RunRepository(connection).snapshot(run_id)
        self.assertEqual(revised["run"]["status"], "waiting_human")
        self.assertEqual(revised["reviews"][0]["status"], "edited")
        self.assertEqual(revised["reviews"][-1]["status"], "open")
        self.assertNotEqual(revised["run"]["output"]["translation_version_id"], original_version_id)
        self.assertIn(
            "translation_human_edit",
            [artifact["artifact_type"] for artifact in revised["artifacts"]],
        )
        with self.databases.knowledge.connect() as connection:
            memory = connection.execute("SELECT * FROM translation_memory_entries").fetchone()
            versions = connection.execute(
                """
                SELECT id, parent_version_id, editor_type, operation, is_current
                FROM translation_versions ORDER BY created_at
                """
            ).fetchall()
            source_revision = connection.execute(
                """
                SELECT layer, parent_revision_id FROM ocr_text_revisions
                WHERE id = ?
                """,
                (revised["run"]["output"]["translation_source_revision_id"],),
            ).fetchone()
            provenance_count = connection.execute(
                """
                SELECT COUNT(*) AS count FROM ocr_provenance_links
                WHERE descendant_revision_id = ?
                """,
                (revised["run"]["output"]["translation_source_revision_id"],),
            ).fetchone()
            human_alignment = connection.execute(
                """
                SELECT alignment_status, source_span_ids_json
                FROM translation_batch_alignments
                WHERE translation_version_id = ?
                """,
                (revised["run"]["output"]["translation_version_id"],),
            ).fetchone()
            human_segment = connection.execute(
                """
                SELECT alignment_status, target_text FROM translation_segment_alignments
                WHERE translation_version_id = ?
                """,
                (revised["run"]["output"]["translation_version_id"],),
            ).fetchone()
        self.assertEqual(memory["target_text"], corrected)
        self.assertEqual(memory["quality_status"], "human_corrected")
        human = next(row for row in versions if row["editor_type"] == "human")
        self.assertEqual(human["parent_version_id"], original_version_id)
        self.assertEqual(human["operation"], "human_edit")
        self.assertEqual(human["is_current"], 1)
        self.assertEqual(source_revision["layer"], "translation_source")
        self.assertEqual(
            source_revision["parent_revision_id"],
            revised["run"]["output"]["normalized_ocr_revision_id"],
        )
        self.assertGreater(provenance_count["count"], 0)
        self.assertEqual(human_alignment["alignment_status"], "human_confirmed")
        self.assertTrue(json.loads(human_alignment["source_span_ids_json"]))
        self.assertEqual(human_segment["alignment_status"], "human_confirmed")
        self.assertEqual(human_segment["target_text"], corrected)

        memory_run_id = self._create_run("translation")
        self._drain()
        with self.databases.state.connect() as connection:
            memory_run = RunRepository(connection).snapshot(memory_run_id)
        self.assertEqual(memory_run["run"]["output"]["translation_memory_match_count"], 1)
        self.assertIn(
            corrected,
            memory_run["run"]["output"]["translation_context"],
        )

    def test_rejected_translation_can_revise_under_the_same_run_id(self) -> None:
        run_id = self._create_run("translation")
        self._drain()
        with self.databases.state.connect() as connection:
            first = RunRepository(connection).snapshot(run_id)
        review_id = first["reviews"][0]["id"]

        rejected = self.worker.resolve_review(review_id, "reject")
        self.assertEqual(rejected, {"run_id": run_id, "status": "failed"})
        recovered = self.worker.resolve_review(
            review_id,
            "revise",
            feedback="却下理由を反映し、学術文体で再翻訳する",
        )

        self.assertEqual(recovered["run_id"], run_id)
        self.assertEqual(recovered["next_node"], "repair_translation")
        self._drain()
        with self.databases.state.connect() as connection:
            revised = RunRepository(connection).snapshot(run_id)
        self.assertEqual(revised["run"]["id"], run_id)
        self.assertEqual(revised["run"]["status"], "waiting_human")
        self.assertEqual(revised["reviews"][0]["status"], "edited")
        self.assertEqual(revised["reviews"][-1]["status"], "open")
        self.assertIsNone(revised["run"]["error"])

    def test_translation_uses_latest_human_corrected_ocr_text(self) -> None:
        ingestion_run_id = self._create_run("ingestion")
        self._drain()
        with self.databases.state.connect() as connection:
            ingested = RunRepository(connection).snapshot(ingestion_run_id)
        normalized_id = ingested["run"]["output"]["normalized_ocr_revision_id"]
        with self.databases.knowledge.connect() as connection:
            repository = OCRRevisionRepository(connection)
            normalized = repository.snapshot(normalized_id)
            source_block = next(
                block for block in normalized["blocks"] if "Bonjour" in block["text"]
            )
            corrected_text = str(source_block["text"]).replace("Bonjour", "Salut corrigé")
            corrected_id = repository.create_human_correction(
                parent_revision_id=normalized_id,
                corrections={str(source_block["stable_block_id"]): corrected_text},
                message="fixture human OCR correction",
            )

        translator = CapturingTranslator()
        self.worker.translator = translator
        translation_run_id = self._create_run("translation")
        self._drain()

        with self.databases.state.connect() as connection:
            translated = RunRepository(connection).snapshot(translation_run_id)
        self.assertEqual(translated["run"]["status"], "waiting_human")
        self.assertEqual(translated["run"]["output"]["audited_ocr_revision_id"], corrected_id)
        self.assertTrue(any("Salut corrigé" in call for call in translator.calls))
        self.assertFalse(any("Bonjour" in call for call in translator.calls))
        translation_source_id = translated["run"]["output"]["translation_source_revision_id"]
        with self.databases.knowledge.connect() as connection:
            source_revision = connection.execute(
                "SELECT parent_revision_id FROM ocr_text_revisions WHERE id = ?",
                (translation_source_id,),
            ).fetchone()
            source_blocks = connection.execute(
                "SELECT text FROM ocr_blocks WHERE revision_id = ? ORDER BY ordinal",
                (translation_source_id,),
            ).fetchall()
            alignment = connection.execute(
                """
                SELECT source_span_ids_json FROM translation_batch_alignments
                WHERE translation_version_id = ? ORDER BY batch_ordinal LIMIT 1
                """,
                (translated["run"]["output"]["translation_version_id"],),
            ).fetchone()
            segment_alignment = connection.execute(
                """
                SELECT alignment_status, source_span_ids_json, source_text, target_text
                FROM translation_segment_alignments
                WHERE translation_version_id = ? ORDER BY segment_ordinal LIMIT 1
                """,
                (translated["run"]["output"]["translation_version_id"],),
            ).fetchone()
        self.assertEqual(source_revision["parent_revision_id"], corrected_id)
        self.assertIn("Salut corrigé", "\n".join(row["text"] for row in source_blocks))
        self.assertTrue(json.loads(alignment["source_span_ids_json"]))
        self.assertEqual(segment_alignment["alignment_status"], "ambiguous")
        self.assertTrue(json.loads(segment_alignment["source_span_ids_json"]))
        self.assertIn("Salut corrigé", segment_alignment["source_text"])
        self.assertIn("翻訳結果", segment_alignment["target_text"])

    def test_translation_retry_resumes_completed_batches_without_retranslating(self) -> None:
        paragraphs = [
            f"Bonjour {index}. " + ("phrase longue de recherche " * 25) + "." for index in range(3)
        ]
        self.source.write_text("\n\n".join(paragraphs), encoding="utf-8")
        translator = FailOnceTranslator()
        self.worker.translator = translator
        run_id = self._create_run("translation")

        self._drain(limit=50)

        with self.databases.state.connect() as connection:
            snapshot = RunRepository(connection).snapshot(run_id)
            draft_attempts = connection.execute(
                """
                SELECT status FROM node_runs
                WHERE run_id = ? AND node_id = 'draft_translation'
                ORDER BY attempt
                """,
                (run_id,),
            ).fetchall()
            latest_progress = {
                row["metric_name"]: int(row["numeric_value"])
                for row in connection.execute(
                    """
                    SELECT metric_name, numeric_value FROM metrics
                    WHERE run_id = ? AND metric_name IN (
                        'translation.batch.total', 'translation.batch.completed'
                    )
                    ORDER BY id
                    """,
                    (run_id,),
                )
            }
            progress_events = connection.execute(
                """
                SELECT COUNT(*) AS count FROM event_log
                WHERE run_id = ? AND event_type = 'translation.batch.progress'
                """,
                (run_id,),
            ).fetchone()["count"]
            failed_batch = connection.execute(
                """
                SELECT payload_json FROM event_log
                WHERE run_id = ? AND event_type = 'translation.batch.failed'
                ORDER BY event_id LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        self.assertEqual(snapshot["run"]["status"], "waiting_human")
        with self.databases.knowledge.connect() as connection:
            checkpoints = connection.execute(
                """
                SELECT batch_ordinal, status FROM translation_batch_checkpoints
                WHERE run_id = ? ORDER BY batch_ordinal
                """,
                (run_id,),
            ).fetchall()
        self.assertEqual(len(checkpoints), 3)
        self.assertTrue(all(row["status"] == "completed" for row in checkpoints))
        self.assertEqual(latest_progress["translation.batch.total"], 3)
        self.assertEqual(latest_progress["translation.batch.completed"], 3)
        self.assertGreaterEqual(progress_events, 4)
        self.assertIsNotNone(failed_batch)
        failed_payload = json.loads(failed_batch["payload_json"])
        self.assertEqual(failed_payload["batch_ordinal"], 1)
        self.assertIn("fixture transient translation failure", failed_payload["message"])
        self.assertEqual(translator.calls.count(paragraphs[0]), 1)
        self.assertEqual([node["status"] for node in draft_attempts], ["failed", "completed"])

    def test_translation_research_decisions_repair_literal_academic_translation(
        self,
    ) -> None:
        source_phrase = "L’opération éthique a lieu à travers la chair émotionnelle du jeu"
        self.source.write_text(
            "Deslongchamps-Gagnon développe cette approche. " + source_phrase + ".",
            encoding="utf-8",
        )
        instructions = (
            "Maxime Deslongchamps-Gagnon の博士論文 Jeux édifiants, vertu et "
            "émotion : une éthique vidéoludique に関連する。"
            "Deslongchamps-Gagnonは、デロンシャン=ガニョンというカタカナにすべき。"
            "説明のある「倫理的作用はゲームの感情的な肉を通じて生じる」="
            f" {source_phrase}という一文は直訳しない。"
            "よって「倫理的な働きはゲームの情動を血肉化することで生じる」"
            "といった文にすべき。"
        )
        translator = ComplianceRepairTranslator()
        self.worker.translator = translator
        run_id = self._create_run(
            "translation",
            instructions=instructions,
            extra_inputs={"academic_research_online": False},
        )

        self._drain(limit=80)

        with self.databases.state.connect() as connection:
            snapshot = RunRepository(connection).snapshot(run_id)
            node_ids = {
                row["node_id"]
                for row in connection.execute(
                    "SELECT node_id FROM node_runs WHERE run_id = ? AND status = 'completed'",
                    (run_id,),
                )
            }
            open_compliance_defects = connection.execute(
                """
                SELECT COUNT(*) AS count FROM defects
                WHERE run_id = ? AND status = 'open'
                  AND defect_type IN (
                    'forbidden_translation', 'person_name_remaining',
                    'decision_not_applied'
                  )
                """,
                (run_id,),
            ).fetchone()["count"]
        with self.databases.knowledge.connect() as connection:
            trigger_count = connection.execute(
                "SELECT COUNT(*) AS count FROM translation_research_triggers WHERE run_id = ?",
                (run_id,),
            ).fetchone()["count"]
            decisions = connection.execute(
                """
                SELECT source_term, target_term, scope_type, status
                FROM translation_term_decisions WHERE origin_run_id = ?
                """,
                (run_id,),
            ).fetchall()
            current = connection.execute(
                """
                SELECT translated_text FROM translation_versions
                WHERE is_current = 1 ORDER BY created_at DESC LIMIT 1
                """
            ).fetchone()

        self.assertEqual(snapshot["run"]["status"], "waiting_human")
        self.assertTrue(
            {
                "research_trigger_detection",
                "academic_evidence_resolution",
                "translation_compliance_validation",
            }.issubset(node_ids)
        )
        self.assertGreaterEqual(trigger_count, 3)
        self.assertTrue(all(row["status"] == "approved" for row in decisions))
        self.assertEqual({row["scope_type"] for row in decisions}, {"author", "document"})
        self.assertIn("情動を血肉化", current["translated_text"])
        self.assertNotIn("感情的な肉", current["translated_text"])
        self.assertEqual(open_compliance_defects, 0)
        self.assertEqual(len(translator.calls), 2)

    def test_translation_batching_keeps_sentence_boundaries_and_oversize_sentence(self) -> None:
        text = "Première phrase assez longue. Deuxième phrase assez longue. Troisième phrase."
        pieces = self.worker._sentence_safe_pieces(text, 40)
        self.assertEqual(" ".join(pieces), text)
        self.assertTrue(all(piece.endswith(".") for piece in pieces))

        oversize = "Mot " * 100 + "."
        self.assertEqual(self.worker._sentence_safe_pieces(oversize, 30), [oversize])

        records = self.worker._translation_batch_records(
            [
                {
                    "text": '""',
                    "source_span_id": "noise-span",
                    "source_stable_block_id": "PDFB-noise",
                    "structural_role": "artifact",
                    "start_page": 18,
                    "end_page": 18,
                    "translatable": False,
                }
            ],
            preserve_structure=True,
        )
        self.assertEqual(records[0]["text"], '""')
        self.assertFalse(records[0]["translatable"])

    def test_llm_jp_chunk_planner_uses_five_lines_and_hard_character_guard(self) -> None:
        text = "".join(f"行{index}\n" for index in range(12))
        chunks = self.worker._proofreading_chunks(text)

        self.assertEqual("".join(chunks), text)
        self.assertEqual(len(chunks), 3)
        self.assertTrue(all(len(chunk.splitlines()) <= 5 for chunk in chunks))

        long_line = "長" * 2501 + "\n末尾"
        guarded = self.worker._proofreading_chunks(long_line)
        self.assertEqual("".join(guarded), long_line)
        self.assertTrue(
            all(
                len(chunk) <= self.settings.translation.post_edit_max_characters
                for chunk in guarded
            )
        )

    def test_llm_jp_chunk_retry_resumes_persisted_results_and_reports_progress(self) -> None:
        self.source.write_text(
            "\n".join(f"Ligne française {index}." for index in range(12)),
            encoding="utf-8",
        )
        llm = FailOnceChunkLocalLLM()
        self.worker.llm_jp = llm
        run_id = self._create_run("translation")

        self._drain(limit=50)

        with self.databases.state.connect() as connection:
            snapshot = RunRepository(connection).snapshot(run_id)
            post_edit_attempts = connection.execute(
                """
                SELECT status FROM node_runs
                WHERE run_id = ? AND node_id = 'llm_jp_post_edit'
                ORDER BY attempt
                """,
                (run_id,),
            ).fetchall()
            latest_progress = {
                row["metric_name"]: int(row["numeric_value"])
                for row in connection.execute(
                    """
                    SELECT metric_name, numeric_value FROM metrics
                    WHERE run_id = ? AND metric_name IN (
                        'translation.post_edit.chunk.total',
                        'translation.post_edit.chunk.completed'
                    ) ORDER BY id
                    """,
                    (run_id,),
                )
            }
            completed_progress = [
                int(row["numeric_value"])
                for row in connection.execute(
                    """
                    SELECT numeric_value FROM metrics
                    WHERE run_id = ?
                      AND metric_name = 'translation.post_edit.chunk.completed'
                    ORDER BY id
                    """,
                    (run_id,),
                )
            ]
            progress_events = connection.execute(
                """
                SELECT COUNT(*) AS count FROM event_log
                WHERE run_id = ? AND event_type = 'translation.post_edit.progress'
                """,
                (run_id,),
            ).fetchone()["count"]
        with self.databases.knowledge.connect() as connection:
            checkpoints = connection.execute(
                """
                SELECT chunk_ordinal, result_status
                FROM proofreading_chunk_checkpoints
                WHERE run_id = ? ORDER BY chunk_ordinal
                """,
                (run_id,),
            ).fetchall()

        self.assertEqual(snapshot["run"]["status"], "waiting_human")
        self.assertEqual([row["status"] for row in post_edit_attempts], ["failed", "completed"])
        self.assertGreater(len(checkpoints), 1)
        self.assertTrue(all(row["result_status"] == "accepted" for row in checkpoints))
        self.assertEqual(latest_progress["translation.post_edit.chunk.completed"], len(checkpoints))
        self.assertEqual(latest_progress["translation.post_edit.chunk.total"], len(checkpoints))
        self.assertGreaterEqual(progress_events, len(checkpoints) + 1)
        self.assertEqual(completed_progress, sorted(completed_progress))
        self.assertEqual(completed_progress[-1], len(checkpoints))
        self.assertEqual(llm.calls[1], llm.calls[2])
        self.assertEqual(llm.calls.count(llm.calls[0]), 1)

    def test_failed_translation_post_edit_can_replay_same_node_and_input(self) -> None:
        self.worker.llm_jp = ContextOverflowLocalLLM()
        run_id = self._create_run("translation")
        self._drain(limit=50)

        with self.databases.state.connect() as connection:
            before = RunRepository(connection).snapshot(run_id)
            failed_node = connection.execute(
                """
                SELECT id, input_json, attempt FROM node_runs
                WHERE run_id = ? AND node_id = 'llm_jp_post_edit'
                ORDER BY rowid DESC LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            replayed = RunRepository(connection).replay_failed_translation_post_edit(run_id)
            after = RunRepository(connection).snapshot(run_id)
            queued_node = connection.execute(
                "SELECT id, input_json, attempt, status FROM node_runs WHERE id = ?",
                (failed_node["id"],),
            ).fetchone()
            event = connection.execute(
                """
                SELECT payload_json FROM event_log
                WHERE run_id = ? AND event_type = 'run.failed_node_replayed'
                ORDER BY rowid DESC LIMIT 1
                """,
                (run_id,),
            ).fetchone()

        self.assertEqual(before["run"]["status"], "failed")
        self.assertEqual(replayed["node_run_id"], failed_node["id"])
        self.assertEqual(after["run"]["status"], "queued")
        self.assertEqual(queued_node["status"], "queued")
        self.assertEqual(queued_node["attempt"], failed_node["attempt"])
        self.assertEqual(queued_node["input_json"], failed_node["input_json"])
        self.assertEqual(
            json.loads(event["payload_json"])["policy"],
            "llm-jp-bounded-repair-feedback-v1",
        )

    def test_translation_runs_llm_jp_in_local_gpu_lane_and_saves_revision(self) -> None:
        self.worker.llm_jp = FakeLocalLLM()
        run_id = self._create_run("translation")
        self._drain()
        with self.databases.state.connect() as connection:
            snapshot = RunRepository(connection).snapshot(run_id)
        self.assertEqual(snapshot["run"]["status"], "waiting_human")
        self.assertTrue(snapshot["run"]["output"]["proofreading_applied"])
        self.assertEqual(snapshot["run"]["output"]["proofreading_status"], "accepted")
        self.assertIn(
            "translation_proofread",
            [artifact["artifact_type"] for artifact in snapshot["artifacts"]],
        )
        with self.databases.knowledge.connect() as connection:
            proofread = connection.execute(
                "SELECT status FROM proofreading_revisions ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            current = connection.execute(
                "SELECT COUNT(*) AS count FROM translation_versions WHERE is_current = 1"
            ).fetchone()
            proofread_segment = connection.execute(
                """
                SELECT alignment_status FROM translation_segment_alignments s
                JOIN translation_versions v ON v.id = s.translation_version_id
                WHERE v.is_current = 1
                """
            ).fetchone()
        self.assertEqual(proofread["status"], "accepted")
        self.assertEqual(current["count"], 1)
        self.assertEqual(proofread_segment["alignment_status"], "ambiguous")

    def test_translation_llm_jp_preserves_safe_noop_without_retry(self) -> None:
        llm = RejectOnceThenNoopLocalLLM()
        self.worker.llm_jp = llm
        run_id = self._create_run("translation")

        self._drain(limit=50)

        with self.databases.state.connect() as connection:
            snapshot = RunRepository(connection).snapshot(run_id)
        with self.databases.knowledge.connect() as connection:
            statuses = [
                str(row["result_status"])
                for row in connection.execute(
                    """
                    SELECT result_status FROM proofreading_chunk_checkpoints
                    WHERE run_id = ? ORDER BY created_at
                    """,
                    (run_id,),
                )
            ]

        output = snapshot["run"]["output"]
        self.assertEqual(snapshot["run"]["status"], "waiting_human")
        self.assertIn("rejected", statuses)
        self.assertEqual(output["proofreading_status"], "accepted")
        self.assertEqual(output["proofreading_rejection_rate"], 0.0)
        self.assertEqual(output["proofreading_rejection_attempt_count"], 0)
        self.assertGreater(output["proofreading_noop_preserved_chunk_count"], 0)
        self.assertTrue(output["proofreading_zero_rejection_required"])

    def test_translation_repeated_safe_noop_never_enters_repair_loop(self) -> None:
        llm = RejectFourThenNoopLocalLLM()
        self.worker.llm_jp = llm
        run_id = self._create_run("translation")

        self._drain(limit=80)

        with self.databases.state.connect() as connection:
            snapshot = RunRepository(connection).snapshot(run_id)
        output = snapshot["run"]["output"]
        self.assertEqual(snapshot["run"]["status"], "waiting_human")
        self.assertEqual(output["proofreading_status"], "accepted")
        self.assertEqual(output["proofreading_rejection_rate"], 0.0)
        self.assertEqual(output["proofreading_rejection_attempt_count"], 0)
        self.assertGreater(output["proofreading_noop_preserved_chunk_count"], 0)
        self.assertLessEqual(len(llm.calls), output["proofreading_chunk_count"])

    def test_translation_invalid_candidate_falls_back_after_finite_repair_budget(
        self,
    ) -> None:
        llm = AlwaysInvalidEditLocalLLM()
        self.worker.llm_jp = llm
        run_id = self._create_run("translation")

        self._drain(limit=80)

        with self.databases.state.connect() as connection:
            snapshot = RunRepository(connection).snapshot(run_id)
        output = snapshot["run"]["output"]
        self.assertEqual(snapshot["run"]["status"], "waiting_human")
        self.assertEqual(snapshot["active_nodes"][0]["node_id"], "human_review")
        self.assertEqual(output["proofreading_status"], "rejected_original_preserved")
        self.assertTrue(output["proofreading_candidate_fallback"])
        self.assertEqual(output["proofreading_candidate_status"], "rejected")
        self.assertEqual(output["proofreading_rejection_rate"], 0.0)
        self.assertEqual(output["proofreading_candidate_rejection_rate"], 1.0)
        self.assertEqual(output["proofreading_final_unresolved_defect_count"], 0)
        self.assertTrue(output["proofreading_repair_exhausted"])
        self.assertEqual(
            output["proofreading_repair_round"],
            self.settings.translation.post_edit_max_repair_rounds,
        )
        self.assertEqual(
            output["proofreading_rejection_attempt_count"],
            self.settings.translation.post_edit_max_repair_rounds + 1,
        )
        self.assertEqual(
            len(llm.calls),
            output["proofreading_chunk_count"]
            * (self.settings.translation.post_edit_max_repair_rounds + 1),
        )
        self.assertEqual(len(snapshot["reviews"]), 1)
        self.assertEqual(snapshot["reviews"][0]["object_type"], "run_artifact")

    def test_translation_runs_configured_gaperon_and_persists_findings(self) -> None:
        self.worker.gaperon = FakeGaperon()
        run_id = self._create_run("translation")
        self._drain()
        with self.databases.state.connect() as connection:
            snapshot = RunRepository(connection).snapshot(run_id)
        self.assertEqual(snapshot["run"]["status"], "waiting_human")
        self.assertEqual(snapshot["run"]["output"]["gaperon_status"], "completed")
        self.assertEqual(snapshot["run"]["output"]["gaperon_issue_count"], 1)
        finding = next(
            defect
            for defect in snapshot["defects"]
            if defect["defect_type"] == "gaperon_french_source_issue"
        )
        self.assertEqual(finding["severity"], "minor")
        self.assertEqual(finding["source_asset_id"], snapshot["run"]["output"]["source_asset_id"])

    def test_translation_pins_active_snapshot_when_ensure_ingested_starts(self) -> None:
        self.semantic_index.active_index_version_id = "snapshot-before-gpu-build"
        run_id = self._create_run("translation")

        self.assertTrue(self.worker.run_once(lane="ingestion_cpu"))
        self.semantic_index.active_index_version_id = "snapshot-after-gpu-build"
        self.assertTrue(self.worker.run_once(lane="local_llm_gpu"))
        self.assertTrue(self.worker.run_once(lane="ingestion_cpu"))
        self.assertTrue(self.worker.run_once(lane="codex_remote"))
        self.assertTrue(self.worker.run_once(lane="ingestion_cpu"))

        with self.databases.state.connect() as connection:
            snapshot = RunRepository(connection).snapshot(run_id)
        context = snapshot["active_nodes"][0]["input"]
        self.assertEqual(context["retrieval_mode"], "semantic_snapshot")
        self.assertEqual(context["semantic_index_snapshot_id"], "snapshot-before-gpu-build")
        self.assertEqual(
            self.semantic_index.searched_index_version_ids[-1],
            "snapshot-before-gpu-build",
        )

    def test_authoring_registers_source_and_empty_target_before_planning(self) -> None:
        run_id = self._create_authoring_run()
        self.assertTrue(self.worker.run_once(lane="ingestion_cpu"))
        with self.databases.state.connect() as connection:
            snapshot = RunRepository(connection).snapshot(run_id)
        self.assertEqual(snapshot["run"]["status"], "running", snapshot["run"])
        self.assertEqual(
            {item["node_id"] for item in snapshot["active_nodes"]},
            {"prepare_rewrite_context", "extract_manuscript_references"},
        )
        context = snapshot["active_nodes"][0]["input"]
        self.assertNotEqual(context["source_document_id"], context["document_id"])
        self.assertTrue(context["source_revision_id"])
        self.assertNotIn("base_revision_id", context)
        self.assertEqual(context["note_text"], "Evidence supports a conclusion.")
        with self.databases.state.connect() as connection:
            target = connection.execute(
                "SELECT current_revision_id FROM content_documents WHERE id = ?",
                (context["document_id"],),
            ).fetchone()
        self.assertIsNone(target["current_revision_id"])
        self.assertEqual(context["reference_entries"], [])

    def test_authoring_prepare_note_keeps_folder_sections_in_source_revision(self) -> None:
        with self.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="Folder source", root_path=str(self.root / "folder-project")
            )
        claimed = ClaimedNode(
            node_run_id="folder-entry-node",
            run_id="folder-entry-run",
            node_id="prepare_note",
            attempt=1,
            workflow_name="manuscript_generator",
            workflow_version="32",
            resource_lane="ingestion_cpu",
            lease_owner="test-worker",
            project_id=project_id,
            inputs={},
        )
        context = self.worker._authoring_prepare_note(
            claimed,
            {
                "title": "統合ノート",
                "note_text": "# 目次\n- 序論\n- 分析\n# 本文\n...",
                "source_sections": [
                    {"title": "序論", "content": "最初の主張。", "source_path": "notes/序論.md"},
                    {"title": "分析", "content": "次の根拠。", "source_path": "notes/分析.md"},
                ],
            },
        ).context
        source_html = context["source_canonical_html"]
        self.assertIn("<h2 id=\"H-folderentryr-0001\">序論</h2>", source_html)
        self.assertIn("data-source-path=\"notes/分析.md\"", source_html)
        self.assertIn("次の根拠。", source_html)
        self.assertNotIn("# 目次", source_html)

    def test_authoring_finalize_persists_pending_proposal_without_revision(self) -> None:
        claimed = ClaimedNode(
            node_run_id="authoring-finalize-node",
            run_id="authoring-finalize-run",
            node_id="finalize_authoring",
            attempt=1,
            workflow_name="authoring",
            workflow_version="30",
            resource_lane="ingestion_cpu",
            lease_owner="test-worker",
            project_id="project-1",
            inputs={},
        )
        canonical = (
            '<article id="paper" lang="ja"><section id="section">'
            '<h2 id="heading">結論</h2><p id="paragraph">本文。</p>'
            "</section></article>"
        )
        with (
            patch.object(self.worker, "_register_artifact", return_value="artifact-1"),
            patch(
                "fukamiru.worker.runner.AuthoringRepository.create_pending_proposal",
                return_value={"status": "pending", "proposed_sha256": hashlib.sha256(
                    canonical.encode()
                ).hexdigest()},
            ) as create_proposal,
        ):
            result = self.worker._authoring_finalize(
                claimed,
                {
                    "proposed_html": canonical,
                    "frozen_candidate_v26": {
                        "html_sha256": hashlib.sha256(canonical.encode()).hexdigest()
                    },
                    "source_document_id": "source-note",
                    "source_revision_id": "source-revision",
                    "document_id": "target-paper",
                    "plan_id": "plan-1",
                    "plan_revision": 1,
                    "title": "Generated paper",
                    "completion_gate": {
                        "passed": True,
                        "quality_status": "ready",
                        "blocking_reasons": [],
                    },
                    "v26_final_judgement": {
                        "html_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
                        "quality_status": "ready",
                        "completion_status": "success",
                    },
                },
            ).context
        self.assertIsNone(result["target_revision_id"])
        self.assertEqual(result["authoring_proposal_status"], "pending")
        self.assertEqual(
            create_proposal.call_args.kwargs["target_document_id"], "target-paper"
        )
        self.assertEqual(Path(result["final_path"]).name, "authoring.submission.html")

    def test_authoring_submission_always_waits_for_explicit_confirmation(self) -> None:
        run_id = self._create_run("manuscript_generator")
        with self.databases.state.connect() as connection:
            node = connection.execute(
                "SELECT id FROM node_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            connection.execute(
                "UPDATE node_runs SET node_id='human_approval', status='running' "
                "WHERE id = ?",
                (node["id"],),
            )
            connection.commit()
        claimed = ClaimedNode(
            node_run_id=str(node["id"]),
            run_id=run_id,
            node_id="human_approval",
            attempt=1,
            workflow_name="authoring",
            workflow_version="30",
            resource_lane="ingestion_cpu",
            lease_owner="test-worker",
            project_id=None,
            inputs={},
        )

        result = self.worker._authoring_human_approval(
            claimed,
            {
                "human_revision_decision": {"required": False},
                "final_artifact_id": "authoring-artifact",
            },
        )

        self.assertTrue(result.waiting_human)
        self.assertTrue(result.context["human_approval_required"])
        with self.databases.state.connect() as connection:
            review = connection.execute(
                "SELECT object_type,object_id,question,status FROM human_reviews "
                "WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        self.assertEqual(review["object_type"], "authoring_submission")
        self.assertEqual(review["object_id"], "authoring-artifact")
        self.assertEqual(review["status"], "open")
        self.assertIn("提出原稿", review["question"])

    def test_authoring_finalize_rejects_non_ready_candidate_without_artifact(self) -> None:
        claimed = ClaimedNode(
            node_run_id="authoring-finalize-node",
            run_id="authoring-finalize-run",
            node_id="finalize_authoring",
            attempt=1,
            workflow_name="authoring",
            workflow_version="30",
            resource_lane="ingestion_cpu",
            lease_owner="test-worker",
            project_id="project-1",
            inputs={},
        )
        canonical = (
            '<article id="paper" lang="ja"><section id="section">'
            '<h2 id="heading">結論</h2><p id="paragraph">本文。</p>'
            "</section></article>"
        )
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        with (
            patch.object(self.worker, "_register_artifact") as register_artifact,
            self.assertRaisesRegex(FukamiruError, "completion_gate_failed"),
        ):
            self.worker._authoring_finalize(
                claimed,
                {
                    "proposed_html": canonical,
                    "frozen_candidate_v26": {"html_sha256": digest},
                    "completion_gate": {
                        "passed": False,
                        "quality_status": "needs_human_review",
                        "blocking_reasons": [{"code": "evidence.coverage_missing"}],
                    },
                    "v26_final_judgement": {
                        "html_sha256": digest,
                        "quality_status": "needs_human_review",
                        "completion_status": "conditional_success",
                    },
                },
            )
        register_artifact.assert_not_called()

    def test_authoring_finalize_persists_non_ready_candidate_in_economy_mode(self) -> None:
        claimed = ClaimedNode(
            node_run_id="authoring-finalize-node",
            run_id="authoring-finalize-economy-run",
            node_id="finalize_authoring",
            attempt=1,
            workflow_name="authoring",
            workflow_version="30",
            resource_lane="ingestion_cpu",
            lease_owner="test-worker",
            project_id="project-1",
            inputs={},
        )
        canonical = (
            '<article id="paper" lang="ja"><section id="section">'
            '<h2 id="heading">結論</h2><p id="paragraph">本文。</p>'
            "</section></article>"
        )
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        with (
            patch.object(self.worker, "_register_artifact", return_value="artifact-1"),
            patch(
                "fukamiru.worker.runner.AuthoringRepository.create_pending_proposal",
                return_value={"status": "pending", "proposed_sha256": digest},
            ) as create_proposal,
        ):
            result = self.worker._authoring_finalize(
                claimed,
                {
                    "economy_priority": True,
                    "proposed_html": canonical,
                    "frozen_candidate_v26": {"html_sha256": digest},
                    "source_document_id": "source-note",
                    "source_revision_id": "source-revision",
                    "document_id": "target-paper",
                    "plan_id": "plan-1",
                    "plan_revision": 1,
                    "title": "Generated paper",
                    "completion_gate": {
                        "passed": False,
                        "quality_status": "needs_human_review",
                        "blocking_reasons": [{"code": "coherence.final_not_passed"}],
                    },
                    "v26_final_judgement": {
                        "html_sha256": digest,
                        "quality_status": "needs_human_review",
                        "completion_status": "conditional_success",
                    },
                },
            ).context

        self.assertTrue(result["authoring_economy_non_ready_persisted"])
        self.assertEqual(result["authoring_proposal_status"], "pending")
        self.assertEqual(
            create_proposal.call_args.kwargs["quality_status"],
            "needs_human_review",
        )

    def test_economy_coherence_reuse_preserves_unresolved_verdict(self) -> None:
        canonical = (
            '<article id="paper" lang="ja"><section id="section">'
            '<h2 id="heading">結論</h2><p id="paragraph">本文。</p>'
            "</section></article>"
        )
        unresolved = {
            "id": "issue-1",
            "category": "claim_transition",
            "severity": "blocking",
            "verification_scope": "semantic_content",
            "block_ids": ["paragraph"],
            "explanation": "未解決です。",
            "recommendation": "人が確認してください。",
            "resolution_route": "human_review",
        }

        result = self.worker._economy_reuse_final_coherence(
            {
                "proposed_html": canonical,
                "manuscript_coherence_checks": [
                    {
                        "stage": "post_polish",
                        "passed": False,
                        "issues": [unresolved],
                    }
                ],
            },
            required_stage="post_polish",
            after_footnotes=True,
        )

        report = result["v26_coherence_report"]
        self.assertFalse(report["passed"])
        self.assertEqual(report["issues"], [unresolved])
        self.assertEqual(report["html_sha256"], hashlib.sha256(canonical.encode()).hexdigest())
        self.assertTrue(result["authoring_economy_footnoted_coherence_reused"])

    def test_authoring_evidence_gate_requires_grounded_coverage(self) -> None:
        claimed = ClaimedNode(
            node_run_id="authoring-evidence-node",
            run_id="authoring-run",
            node_id="validate_evidence_ready",
            attempt=1,
            workflow_name="authoring",
            workflow_version="30",
            resource_lane="ingestion_cpu",
            lease_owner="test-worker",
            project_id="project-1",
            inputs={},
        )
        base = {
            "research_search_plan": [
                {
                    "obligation_id": "obligation-c1",
                    "finding_id": "c1",
                    "requires_research": True,
                }
            ],
            "evidence_coverage": [
                {
                    "obligation_id": "obligation-c1",
                    "finding_id": "c1",
                    "covered": True,
                    "accepted_evidence_count": 1,
                }
            ],
            "evidence_context": [
                {
                    "id": "evidence-1",
                    "provenance": {
                        "support_audits": [
                            {
                                "obligation_id": "obligation-c1",
                                "host_grounded": True,
                            }
                        ]
                    },
                }
            ],
        }
        passed = self.worker._authoring_validate_evidence_ready(
            claimed, base
        ).context
        self.assertTrue(passed["authoring_evidence_gate"]["passed"])
        self.assertEqual(
            [item["id"] for item in passed["evidence_context"]],
            ["evidence-1"],
        )
        with self.assertRaisesRegex(FukamiruError, "obligation-c1"):
            self.worker._authoring_validate_evidence_ready(
                claimed,
                {**base, "evidence_context": [], "evidence_coverage": []},
            )

    def test_authoring_atomic_gate_attributes_supporting_unverified_fact(self) -> None:
        claimed = ClaimedNode(
            node_run_id="authoring-atomic-node",
            run_id="authoring-run",
            node_id="decide_evidence_realization",
            attempt=1,
            workflow_name="authoring",
            workflow_version="30",
            resource_lane="ingestion_cpu",
            lease_owner="test-worker",
            project_id="project-1",
            inputs={},
        )
        base = {
            "claims": [{"id": "c1"}],
            "claim_proposition_bindings": [
                {"claim_id": "c1", "proposition_id": "p1"}
            ],
            "atomic_evidence_obligations": [
                {
                    "obligation_id": "evidence:p1",
                    "proposition_id": "p1",
                    "assertion": "1998年に制度Aが導入された。",
                    "source_span": "1998年に制度Aが導入された。",
                    "source_origin": "memo",
                    "verification_scope": "external_historical_fact",
                    "verification_requirement": "external_required",
                    "argument_necessity": "supporting",
                    "claim_ids": ["c1"],
                }
            ],
            "evidence_context": [],
            "evidence_coverage": [],
        }

        decided = self.worker._authoring_decide_evidence_realization(
            claimed, base
        ).context
        result = self.worker._authoring_validate_evidence_ready(
            claimed, decided
        ).context

        self.assertTrue(result["authoring_evidence_gate"]["passed"])
        [contract] = result["authoring_atomic_evidence_contracts"]
        self.assertEqual(contract["realization"], "attribute_to_memo")
        self.assertFalse(contract["may_assert_as_external_fact"])

    def test_authoring_atomic_gate_attributes_core_unverified_fact(self) -> None:
        claimed = ClaimedNode(
            node_run_id="authoring-atomic-node",
            run_id="authoring-run",
            node_id="validate_evidence_ready",
            attempt=1,
            workflow_name="authoring",
            workflow_version="30",
            resource_lane="ingestion_cpu",
            lease_owner="test-worker",
            project_id="project-1",
            inputs={},
        )
        context = self.worker._authoring_decide_evidence_realization(
            claimed,
            {
                "claims": [{"id": "c1"}],
                "claim_proposition_bindings": [
                    {"claim_id": "c1", "proposition_id": "p1"}
                ],
                "atomic_evidence_obligations": [
                    {
                        "obligation_id": "evidence:p1",
                        "proposition_id": "p1",
                        "assertion": "1998年に制度Aが導入された。",
                        "source_span": "1998年に制度Aが導入された。",
                        "source_origin": "memo",
                        "verification_scope": "external_historical_fact",
                        "verification_requirement": "external_required",
                        "argument_necessity": "core",
                        "claim_ids": ["c1"],
                    }
                ],
                "evidence_context": [],
                "evidence_coverage": [],
            },
        ).context

        result = self.worker._authoring_validate_evidence_ready(
            claimed, context
        ).context

        self.assertTrue(result["authoring_evidence_gate"]["passed"])
        [contract] = result["authoring_atomic_evidence_contracts"]
        self.assertEqual(contract["realization"], "attribute_to_memo")
        self.assertEqual(contract["support_status"], "attributed_unverified_memo")
        self.assertTrue(contract["visible_attribution_required"])
        self.assertFalse(contract["may_assert_as_external_fact"])

    def test_v31_atomic_gate_omits_unverified_core_fact_and_continues(
        self,
    ) -> None:
        claimed = ClaimedNode(
            node_run_id="authoring-atomic-node",
            run_id="authoring-run",
            node_id="validate_evidence_ready",
            attempt=1,
            workflow_name="authoring",
            workflow_version="31",
            resource_lane="ingestion_cpu",
            lease_owner="test-worker",
            project_id="project-1",
            inputs={},
        )
        context = self.worker._authoring_decide_evidence_realization(
            claimed,
            {
                "claims": [{"id": "c1"}],
                "claim_proposition_bindings": [
                    {"claim_id": "c1", "proposition_id": "p1"}
                ],
                "atomic_evidence_obligations": [
                    {
                        "obligation_id": "evidence:p1",
                        "proposition_id": "p1",
                        "assertion": "1998年に制度Aが導入された。",
                        "source_span": "1998年に制度Aが導入された。",
                        "source_origin": "memo",
                        "verification_scope": "external_historical_fact",
                        "verification_requirement": "external_required",
                        "argument_necessity": "core",
                        "claim_ids": ["c1"],
                    }
                ],
                "evidence_context": [],
                "evidence_coverage": [],
            },
        ).context

        context["evidence_realization_decisions"] = [{
            "obligation_id": "evidence:p1",
            "proposition_id": "p1",
            "realization": "attribute_to_memo",
            "support_status": "attributed_unverified_memo",
            "accepted_evidence_ids": [],
            "citation_evidence_ids": [],
            "claim_ids": ["c1"],
        }]
        context["evidence_realization_report"] = {
            "policy": "authoring-atomic-evidence-realization-v2"
        }

        result = self.worker._authoring_validate_evidence_ready(claimed, context).context
        self.assertTrue(result["authoring_evidence_gate"]["passed"])
        self.assertEqual(result["authoring_evidence_gate"]["blocked_obligation_ids"], [])
        self.assertEqual(
            result["authoring_evidence_gate"]["advisory_obligation_ids"],
            ["evidence:p1"],
        )
        [atomic] = result["authoring_atomic_evidence_contracts"]
        self.assertEqual(atomic["realization"], "omit")
        self.assertEqual(atomic["support_status"], "omitted_unverified_core")
        self.assertFalse(atomic["may_assert_as_external_fact"])
        [claim_contract] = result["authoring_evidence_contracts"]
        self.assertEqual(claim_contract["support_status"], "qualified_inference_only")
        self.assertEqual(claim_contract["accepted_evidence_ids"], [])

    def test_v31_authorial_inference_is_internal_without_paragraph_hedge(self) -> None:
        claimed = ClaimedNode(
            node_run_id="authoring-interpretation-node",
            run_id="authoring-run",
            node_id="decide_evidence_realization",
            attempt=1,
            workflow_name="authoring",
            workflow_version="31",
            resource_lane="ingestion_cpu",
            lease_owner="test-worker",
            project_id="project-1",
            inputs={},
        )
        context = {
            "claims": [{"id": "c1"}, {"id": "c2"}],
            "claim_proposition_bindings": [
                {"claim_id": "c1", "proposition_id": "p1"},
                {"claim_id": "c2", "proposition_id": "p2"},
                {"claim_id": "c2", "proposition_id": "p3"},
            ],
            "atomic_evidence_obligations": [
                {
                    "obligation_id": f"evidence:{proposition_id}",
                    "proposition_id": proposition_id,
                    "assertion": assertion,
                    "verification_scope": scope,
                    "verification_requirement": requirement,
                    "argument_necessity": "core",
                    "claim_ids": [claim_id],
                }
                for proposition_id, claim_id, assertion, scope, requirement in (
                    ("p1", "c1", "二つの配置は選択を異なる形で導く。",
                     "analytic_inference", "preferred"),
                    ("p2", "c2", "配置は受容の向きを変える。",
                     "analytic_inference", "preferred"),
                    ("p3", "c2", "公開年は一九八〇年である。",
                     "external_historical_fact", "external_required"),
                )
            ],
            "evidence_context": [],
            "evidence_coverage": [],
        }
        decided = self.worker._authoring_decide_evidence_realization(
            claimed, context
        ).context
        decision = decided["evidence_realization_decisions"][0]
        self.assertEqual(decision["realization"], "qualified_inference")
        self.assertFalse(decision["visible_qualification_required"])
        self.assertFalse(decision["may_assert_as_external_fact"])
        ready = self.worker._authoring_validate_evidence_ready(claimed, decided).context
        contracts = {
            item["claim_id"]: item
            for item in ready["authoring_evidence_contracts"]
        }
        contract = contracts["c1"]
        self.assertIn("argue_as_authorial_interpretation", contract["prose_policy"])
        self.assertIn(
            "argue_interpretive_atoms_as_authorial_interpretation",
            contracts["c2"]["prose_policy"],
        )
        self.assertIn("never_promote_wrapper_to_fact", contracts["c2"]["prose_policy"])
        self.assertFalse(ready["authoring_atomic_evidence_contracts"][0][
            "visible_qualification_required"
        ])

        older = ClaimedNode(
            node_run_id="authoring-interpretation-v30-node",
            run_id="authoring-run",
            node_id="decide_evidence_realization",
            attempt=1,
            workflow_name="authoring",
            workflow_version="30",
            resource_lane="ingestion_cpu",
            lease_owner="test-worker",
            project_id="project-1",
            inputs={},
        )
        legacy = self.worker._authoring_decide_evidence_realization(
            older, context
        ).context
        self.assertTrue(legacy["evidence_realization_decisions"][0][
            "visible_qualification_required"
        ])

    def test_v31_four_unverified_core_atoms_are_advisories_not_run_blockers(self) -> None:
        claimed = ClaimedNode(
            node_run_id="authoring-atomic-node",
            run_id="authoring-run",
            node_id="validate_evidence_ready",
            attempt=1,
            workflow_name="authoring",
            workflow_version="31",
            resource_lane="ingestion_cpu",
            lease_owner="test-worker",
            project_id="project-1",
            inputs={},
        )
        atoms = [
            ("p0126", "user", "external_historical_fact"),
            ("p0153", "memo", "external_historical_fact"),
            ("p0409", "memo", "bibliographic_identity"),
            ("p0459", "memo", "external_historical_fact"),
        ]
        context = self.worker._authoring_decide_evidence_realization(
            claimed,
            {
                "claims": [{"id": f"c{index}"} for index in range(4)],
                "claim_proposition_bindings": [
                    {"claim_id": f"c{index}", "proposition_id": proposition_id}
                    for index, (proposition_id, _, _) in enumerate(atoms)
                ],
                "atomic_evidence_obligations": [
                    {
                        "obligation_id": f"evidence:{proposition_id}",
                        "proposition_id": proposition_id,
                        "assertion": f"未検証の事実{proposition_id}",
                        "source_span": f"元の記述{proposition_id}",
                        "source_origin": origin,
                        "verification_scope": scope,
                        "verification_requirement": "external_required",
                        "argument_necessity": "core",
                        "claim_ids": [f"c{index}"],
                    }
                    for index, (proposition_id, origin, scope) in enumerate(atoms)
                ],
                "evidence_context": [],
                "evidence_coverage": [],
            },
        ).context
        result = self.worker._authoring_validate_evidence_ready(claimed, context).context
        self.assertTrue(result["authoring_evidence_gate"]["passed"])
        self.assertEqual(len(result["authoring_evidence_advisories"]), 4)
        self.assertEqual(
            {item["proposition_id"] for item in result["authoring_evidence_advisories"]},
            {item[0] for item in atoms},
        )
        self.assertTrue(all(
            item["realization"] == "omit" and not item["may_assert_as_external_fact"]
            for item in result["authoring_atomic_evidence_contracts"]
        ))

    def test_v31_memo_provenance_is_internal_except_for_speaker_annotations(self) -> None:
        claimed = ClaimedNode(
            node_run_id="authoring-atomic-node",
            run_id="authoring-run",
            node_id="validate_evidence_ready",
            attempt=1,
            workflow_name="authoring",
            workflow_version="31",
            resource_lane="ingestion_cpu",
            lease_owner="test-worker",
            project_id="project-1",
            inputs={},
        )
        base = {
            "claims": [{"id": "c1"}],
            "atomic_evidence_obligations": [
                {
                    "obligation_id": "evidence:p1",
                    "proposition_id": "p1",
                    "assertion": "作品の配置は相互の差異を見せる。",
                    "source_span": "作品の配置は相互の差異を見せる。",
                    "source_origin": "memo",
                    "verification_scope": "source_internal",
                    "verification_requirement": "none",
                    "argument_necessity": "core",
                    "claim_ids": ["c1"],
                    "memo_annotation_kinds": [],
                }
            ],
            "evidence_context": [],
        }

        direct = self.worker._authoring_decide_evidence_realization(claimed, base).context
        speaker = self.worker._authoring_decide_evidence_realization(
            claimed,
            {
                **base,
                "atomic_evidence_obligations": [
                    {
                        **base["atomic_evidence_obligations"][0],
                        "memo_annotation_kinds": ["other_speaker"],
                    }
                ],
            },
        ).context

        self.assertFalse(
            direct["evidence_realization_decisions"][0]["visible_attribution_required"]
        )
        self.assertTrue(
            speaker["evidence_realization_decisions"][0]["visible_attribution_required"]
        )
        self.assertEqual(
            speaker["evidence_realization_decisions"][0]["memo_annotation_kinds"],
            ["other_speaker"],
        )

    def test_authoring_atomic_gate_refreshes_stale_core_decision_under_v3_policy(
        self,
    ) -> None:
        claimed = ClaimedNode(
            node_run_id="authoring-atomic-node",
            run_id="authoring-run",
            node_id="validate_evidence_ready",
            attempt=1,
            workflow_name="authoring",
            workflow_version="30",
            resource_lane="ingestion_cpu",
            lease_owner="test-worker",
            project_id="project-1",
            inputs={},
        )
        proposition = {
            "id": "p1",
            "text": "1998年に制度Aが導入された。",
            "source_span": "1998年に制度Aが導入された。",
            "provenance": {
                "source": "memo",
                "source_ids": ["input.note_text"],
                "verification_status": "unverified",
            },
        }
        classification = {"proposition_id": "p1", "type": "fact"}
        scopes = classify_proposition_verification_scopes(
            [proposition], [classification]
        )
        bindings = build_claim_proposition_bindings(
            [{"id": "c1", "level": "subclaim", "proposition_id": "p1"}],
            [proposition],
            [classification],
            scopes,
        )
        obligations = build_atomic_evidence_obligations(
            [proposition], scopes, bindings
        )
        result = self.worker._authoring_validate_evidence_ready(
            claimed,
            {
                "propositions": [proposition],
                "classifications": [classification],
                "claims": [
                    {"id": "c1", "level": "subclaim", "proposition_id": "p1"}
                ],
                "proposition_verification_scopes": scopes,
                "claim_proposition_bindings": bindings,
                "atomic_evidence_obligations": obligations,
                "evidence_realization_decisions": [
                    {
                        "obligation_id": obligations[0]["obligation_id"],
                        "proposition_id": "p1",
                        "realization": "blocked_missing_required",
                    }
                ],
                "evidence_realization_report": {
                    "policy": "authoring-atomic-evidence-realization-v1"
                },
                "evidence_context": [],
                "evidence_coverage": [],
            },
        ).context

        self.assertTrue(result["authoring_evidence_gate"]["passed"])
        self.assertEqual(
            result["evidence_realization_report"]["policy"],
            "authoring-atomic-evidence-realization-v4",
        )
        [decision] = result["evidence_realization_decisions"]
        self.assertEqual(decision["realization"], "attribute_to_memo")

    def test_authoring_atomic_gate_reclassifies_scope_and_attributes_unverified_fact(
        self,
    ) -> None:
        claimed = ClaimedNode(
            node_run_id="authoring-atomic-node",
            run_id="authoring-run",
            node_id="validate_evidence_ready",
            attempt=1,
            workflow_name="authoring",
            workflow_version="30",
            resource_lane="ingestion_cpu",
            lease_owner="test-worker",
            project_id="project-1",
            inputs={},
        )
        propositions = [
            {
                "id": "p-scope",
                "text": "論じる話題には青年誌配置の変容がある。",
                "source_span": "青年誌配置の変容",
                "provenance": {"source": "memo"},
            },
            {
                "id": "p-fact",
                "text": "『作品A』は『雑誌B』に掲載された。",
                "source_span": "『作品A』は『雑誌B』に掲載された。",
                "provenance": {"source": "memo"},
            },
        ]
        classifications = [
            {"proposition_id": "p-scope", "type": "example"},
            {"proposition_id": "p-fact", "type": "fact"},
        ]
        stale_scopes = [
            {
                "proposition_id": item["id"],
                "verification_scope": "external_historical_fact",
                "verification_requirement": "external_required",
            }
            for item in propositions
        ]

        result = self.worker._authoring_validate_evidence_ready(
            claimed,
            {
                "propositions": propositions,
                "classifications": classifications,
                "claims": [
                    {
                        "id": "c-scope",
                        "level": "subclaim",
                        "proposition_id": "p-scope",
                    },
                    {
                        "id": "c-fact",
                        "level": "subclaim",
                        "proposition_id": "p-fact",
                    },
                ],
                "proposition_verification_scopes": stale_scopes,
                "claim_proposition_bindings": [],
                "atomic_evidence_obligations": [{"obligation_id": "stale"}],
                "evidence_realization_decisions": [],
                "evidence_context": [],
                "evidence_coverage": [],
            },
        ).context

        self.assertTrue(result["authoring_evidence_gate"]["passed"])
        decisions = {
            item["proposition_id"]: item
            for item in result["evidence_realization_decisions"]
        }
        self.assertEqual(decisions["p-scope"]["realization"], "assert_method_or_scope")
        self.assertEqual(decisions["p-fact"]["realization"], "attribute_to_memo")
        self.assertFalse(decisions["p-fact"]["may_assert_as_external_fact"])

    def test_authoring_atomic_gate_links_verified_evidence_to_exact_obligation(
        self,
    ) -> None:
        claimed = ClaimedNode(
            node_run_id="authoring-atomic-node",
            run_id="authoring-run",
            node_id="validate_evidence_ready",
            attempt=1,
            workflow_name="authoring",
            workflow_version="30",
            resource_lane="ingestion_cpu",
            lease_owner="test-worker",
            project_id="project-1",
            inputs={},
        )
        evidence = {
            "id": "source-1",
            "provenance": {
                "support_audits": [
                    {"obligation_id": "evidence:p1", "host_grounded": True}
                ]
            },
        }
        base = {
            "claims": [{"id": "c1"}],
            "claim_proposition_bindings": [
                {"claim_id": "c1", "proposition_id": "p1"}
            ],
            "atomic_evidence_obligations": [
                {
                    "obligation_id": "evidence:p1",
                    "proposition_id": "p1",
                    "assertion": "1998年に制度Aが導入された。",
                    "source_span": "1998年に制度Aが導入された。",
                    "source_origin": "memo",
                    "verification_scope": "external_historical_fact",
                    "verification_requirement": "external_required",
                    "argument_necessity": "core",
                    "claim_ids": ["c1"],
                }
            ],
            "evidence_context": [evidence],
            "evidence_coverage": [
                {
                    "obligation_id": "evidence:p1",
                    "finding_id": "evidence:p1",
                    "covered": True,
                    "accepted_evidence_count": 1,
                }
            ],
        }
        decided = self.worker._authoring_decide_evidence_realization(
            claimed, base
        ).context
        result = self.worker._authoring_validate_evidence_ready(
            claimed, decided
        ).context

        [contract] = result["authoring_atomic_evidence_contracts"]
        self.assertEqual(contract["realization"], "assert_with_citation")
        self.assertEqual(contract["accepted_evidence_ids"], ["source-1"])
        self.assertTrue(result["authoring_evidence_gate"]["passed"])

    def test_authoring_evidence_gate_prunes_parallel_branch_discovery_only_evidence(
        self,
    ) -> None:
        claimed = ClaimedNode(
            node_run_id="authoring-evidence-filter-node",
            run_id="authoring-run",
            node_id="validate_evidence_ready",
            attempt=1,
            workflow_name="authoring",
            workflow_version="30",
            resource_lane="ingestion_cpu",
            lease_owner="test-worker",
            project_id="project-1",
            inputs={},
        )
        annotation = {
            "id": "annotation-citation:r1",
            "verification_status": "unverified",
            "verification_scope": "bibliographic_identity_only",
        }

        result = self.worker._authoring_validate_evidence_ready(
            claimed,
            {
                "research_search_plan": [],
                "research_query_candidates": [],
                "evidence_coverage": [],
                "evidence_context": [
                    {
                        "id": "discovery-only",
                        "provenance": {
                            "evidence_quality": {"passed": True},
                            "support_audits": [],
                        },
                    },
                    annotation,
                ],
                "annotation_citation_evidence": [annotation],
            },
        ).context

        self.assertEqual(
            [item["id"] for item in result["evidence_context"]],
            ["annotation-citation:r1"],
        )
        self.assertEqual(
            result["authoring_evidence_context_filter"],
            {
                "policy": "authoring-post-support-evidence-context-v1",
                "input_count": 2,
                "host_grounded_count": 0,
                "annotation_citation_count": 1,
                "output_count": 1,
            },
        )

    def test_authoring_structure_relocks_evidence_after_parallel_join(self) -> None:
        claimed = ClaimedNode(
            node_run_id="authoring-structure-node",
            run_id="authoring-run",
            node_id="plan_manuscript_structure",
            attempt=1,
            workflow_name="authoring",
            workflow_version="30",
            resource_lane="codex_remote",
            lease_owner="test-worker",
            project_id="project-1",
            inputs={},
        )
        verified = {
            "id": "verified",
            "provenance": {"support_audits": [{"host_grounded": True}]},
        }
        stale = {"id": "discovery-only", "provenance": {"support_audits": []}}
        discourse = {
            "section_id": "s1",
            "article_role": "body",
            "claim_ids": ["c1"],
        }
        with patch.object(
            self.worker,
            "_authoring_generate",
            return_value={"sections": [dict(discourse)]},
        ):
            result = self.worker._authoring_plan_manuscript_structure(
                claimed,
                {
                    "evidence_context": [verified, stale],
                    "annotation_citation_evidence": [],
                    "research_search_plan": [
                        {"obligation_id": "obligation-c1"}
                    ],
                    "evidence_coverage": [
                        {
                            "query": "query",
                            "scope_key": "claim:c1",
                            "finding_id": "c1",
                            "covered": False,
                        },
                        {
                            "query": "query",
                            "scope_key": "claim:c1",
                            "finding_id": "c1",
                            "obligation_id": "obligation-c1",
                            "covered": True,
                            "accepted_evidence_count": 1,
                        },
                    ],
                    "discourse_nodes": [discourse],
                    "claims": [{"id": "c1"}],
                    "source_preservation_map": [],
                },
            ).context

        self.assertEqual(
            [item["id"] for item in result["evidence_context"]], ["verified"]
        )
        self.assertEqual(
            result["authoring_evidence_context_filter"]["relocked_at"],
            "plan_manuscript_structure",
        )
        self.assertEqual(len(result["evidence_coverage"]), 1)
        self.assertEqual(
            result["evidence_coverage"][0]["obligation_id"], "obligation-c1"
        )

    def test_authoring_economy_reconciles_required_contract_after_fanin(self) -> None:
        required = {
            "obligation_id": "obligation-c1",
            "finding_id": "c1",
            "claim_id": "c1",
            "query": "検証対象",
            "scope_key": "claim:c1",
            "requires_research": True,
            "evidence_requirement": "required",
            "allowed_support_modes": ["external_evidence"],
        }
        reconciled = self.worker._authoring_reconcile_economy_evidence_contracts(
            {
                "economy_priority": True,
                "research_search_plan": [required],
                "research_query_candidates": [required],
                "research_query_assignments": [required],
                "evidence_coverage": [
                    {
                        **required,
                        "covered": False,
                        "accepted_evidence_count": 0,
                    }
                ],
                "authoring_evidence_contracts": [
                    {
                        "claim_id": "c1",
                        "obligation_id": "obligation-c1",
                        "evidence_requirement": "required",
                        "allowed_support_modes": ["external_evidence"],
                        "support_status": "missing_required_external",
                        "prose_policy": "prohibited_until_verified",
                    }
                ],
                "authoring_evidence_gate": {
                    "passed": False,
                    "coverage_failure_ids": ["obligation-c1"],
                },
            }
        )

        for key in (
            "research_search_plan",
            "research_query_candidates",
            "research_query_assignments",
            "evidence_coverage",
        ):
            self.assertEqual(reconciled[key][0]["evidence_requirement"], "preferred")
            self.assertFalse(reconciled[key][0]["requires_research"])
        contract = reconciled["authoring_evidence_contracts"][0]
        self.assertEqual(contract["support_status"], "qualified_inference_only")
        self.assertEqual(
            contract["prose_policy"],
            "attribute_to_memo_or_llm_and_state_limitation",
        )
        self.assertTrue(reconciled["authoring_evidence_gate"]["passed"])
        self.assertEqual(
            reconciled["authoring_evidence_contract_reconciliation"][
                "downgraded_obligation_ids"
            ],
            ["obligation-c1"],
        )

    def test_authoring_economy_syncs_stale_coverage_to_preferred_plan(self) -> None:
        preferred = {
            "obligation_id": "obligation-c1",
            "finding_id": "c1",
            "query": "検証対象",
            "scope_key": "claim:c1",
            "requires_research": False,
            "evidence_requirement": "preferred",
            "allowed_support_modes": [
                "external_evidence",
                "qualified_memo_inference",
                "qualified_llm_inference",
            ],
            "research_selection_reason": "economy_downgraded_after_search",
        }
        reconciled = self.worker._authoring_reconcile_economy_evidence_contracts(
            {
                "economy_priority": True,
                "research_search_plan": [preferred],
                "evidence_coverage": [
                    {
                        **preferred,
                        "requires_research": True,
                        "evidence_requirement": "required",
                        "allowed_support_modes": ["external_evidence"],
                        "covered": False,
                        "accepted_evidence_count": 0,
                    }
                ],
            }
        )

        coverage = reconciled["evidence_coverage"][0]
        self.assertEqual(coverage["evidence_requirement"], "preferred")
        self.assertFalse(coverage["requires_research"])
        self.assertEqual(
            reconciled["authoring_evidence_contract_reconciliation"][
                "coverage_contract_sync_ids"
            ],
            ["obligation-c1"],
        )

    def test_authoring_evidence_gate_keeps_unsupported_interpretation_qualified(self) -> None:
        claimed = ClaimedNode(
            node_run_id="authoring-preferred-evidence-node",
            run_id="authoring-run",
            node_id="validate_evidence_ready",
            attempt=1,
            workflow_name="authoring",
            workflow_version="30",
            resource_lane="ingestion_cpu",
            lease_owner="test-worker",
            project_id="project-1",
            inputs={},
        )
        context = self.worker._authoring_validate_evidence_ready(
            claimed,
            {
                "research_query_candidates": [
                    {
                        "claim_id": "c1",
                        "finding_id": "c1",
                        "requires_research": True,
                        "evidence_requirement": "preferred",
                        "allowed_support_modes": [
                            "external_evidence",
                            "qualified_memo_inference",
                            "qualified_llm_inference",
                        ],
                    }
                ],
                "research_search_plan": [
                    {
                        "obligation_id": "obligation-c1",
                        "claim_id": "c1",
                        "finding_id": "c1",
                        "requires_research": True,
                        "evidence_requirement": "preferred",
                    }
                ],
                "evidence_coverage": [
                    {
                        "obligation_id": "obligation-c1",
                        "finding_id": "c1",
                        "requires_research": True,
                        "evidence_requirement": "preferred",
                        "covered": False,
                        "accepted_evidence_count": 0,
                    }
                ],
                "evidence_context": [],
            },
        ).context

        self.assertTrue(context["authoring_evidence_gate"]["passed"])
        [contract] = context["authoring_evidence_contracts"]
        self.assertEqual(contract["support_status"], "qualified_inference_only")
        self.assertEqual(
            contract["prose_policy"],
            "attribute_to_memo_or_llm_and_state_limitation",
        )

    def test_authoring_evidence_gate_can_downgrade_missing_required_in_economy_mode(
        self,
    ) -> None:
        claimed = ClaimedNode(
            node_run_id="authoring-economy-evidence-node",
            run_id="authoring-run",
            node_id="validate_evidence_ready",
            attempt=1,
            workflow_name="authoring",
            workflow_version="30",
            resource_lane="ingestion_cpu",
            lease_owner="test-worker",
            project_id="project-1",
            inputs={},
        )
        result = self.worker._authoring_validate_evidence_ready(
            claimed,
            {
                "economy_priority": True,
                "research_query_candidates": [
                    {
                        "claim_id": "c1",
                        "finding_id": "c1",
                        "requires_research": True,
                        "evidence_requirement": "required",
                    }
                ],
                "research_query_assignments": [
                    {
                        "claim_id": "c1",
                        "finding_id": "c1",
                        "requires_research": True,
                        "evidence_requirement": "required",
                    }
                ],
                "research_search_plan": [
                    {
                        "obligation_id": "obligation-c1",
                        "claim_id": "c1",
                        "finding_id": "c1",
                        "requires_research": True,
                        "evidence_requirement": "required",
                    }
                ],
                "evidence_coverage": [],
                "evidence_context": [],
            },
        ).context

        self.assertTrue(result["authoring_evidence_gate"]["passed"])
        self.assertTrue(result["authoring_evidence_gate"]["economy_override"])
        self.assertEqual(
            result["research_query_candidates"][0]["evidence_requirement"],
            "preferred",
        )
        self.assertEqual(
            result["authoring_evidence_contracts"][0]["support_status"],
            "qualified_inference_only",
        )

    def test_authoring_decomposition_batches_paragraphs_and_rebases_duplicate_ids(self) -> None:
        note = "第一の命題。\n第二の命題は少し長い。\n第三の命題。"
        batches = self.worker._authoring_note_batches(note, max_chars=16)
        self.assertEqual("\n".join(batches), note)
        self.assertGreater(len(batches), 1)
        claimed = ClaimedNode(
            node_run_id="authoring-node",
            run_id="authoring-run",
            node_id="decompose_memo",
            attempt=1,
            workflow_name="authoring",
            workflow_version="30",
            resource_lane="codex_remote",
            lease_owner="test-worker",
            project_id="project-1",
            inputs={},
        )
        provenance = {
            "source": "memo",
            "source_ids": [],
            "verification_status": "verified",
        }
        parallel_results = [
            {
                "propositions": [
                    {
                        "id": "p1",
                        "text": batch,
                        "source_span": batch,
                        "provenance": provenance,
                    }
                ]
            }
            for batch in batches
        ]
        with patch.object(
            self.worker,
            "_authoring_parallel_generate",
            return_value=parallel_results,
        ) as generate:
            result = self.worker._authoring_decompose_memo(
                claimed,
                {"note_text": note, "language": "ja", "title": "test"},
            ).context
        self.assertEqual(
            [item["id"] for item in result["propositions"]],
            [f"p{index:04d}" for index in range(1, len(batches) + 1)],
        )
        self.assertEqual(generate.call_args.kwargs["operation_name"], "authoring_decompose_memo")

    def test_authoring_decomposition_sends_only_folder_body_sections(self) -> None:
        claimed = ClaimedNode(
            node_run_id="folder-node",
            run_id="folder-run",
            node_id="decompose_memo",
            attempt=1,
            workflow_name="manuscript_generator",
            workflow_version="32",
            resource_lane="codex_remote",
            lease_owner="test-worker",
            project_id="project-1",
            inputs={},
        )
        sections = [
            {"title": "序論", "content": "最初の主張。", "source_path": "notes/序論.md"},
            {"title": "分析", "content": "次の根拠。", "source_path": "notes/分析.md"},
        ]
        response = {"propositions": [{
            "id": "p1", "text": "最初の主張。", "source_span": "最初の主張。",
            "provenance": {"source": "memo", "source_ids": [], "verification_status": "verified"},
        }]}
        with patch.object(
            self.worker, "_authoring_parallel_generate", return_value=[response, response]
        ) as generate:
            self.worker._authoring_decompose_memo(
                claimed,
                {
                    "title": "統合ノート", "language": "ja",
                    "note_text": "# 目次\n- 序論\n- 分析\n<!-- source: notes/序論.md -->",
                    "source_sections": sections,
                },
            )
        payloads = generate.call_args.kwargs["payloads"]
        self.assertEqual(len(payloads), 2)
        self.assertIn("## 序論\n\n最初の主張。", payloads[0]["note_text"])
        self.assertIn("## 分析\n\n次の根拠。", payloads[1]["note_text"])
        self.assertTrue(all("# 目次" not in item["note_text"] for item in payloads))
        self.assertTrue(all("source:" not in item["note_text"] for item in payloads))

    def test_authoring_decomposition_excludes_declared_document_scaffolding(self) -> None:
        note = (
            "# 論文題名\n\n## この文書について\n"
            "AI整形版から接続表現を外した骨組み版である。\n\n"
            "**タグの凡例**\n- (要: …) は確認事項。\n\n---\n\n"
            "## 序\n媒体は欲望を配置する。"
        )
        content, record = self.worker._authoring_strip_leading_scaffolding(note)
        self.assertEqual(content, "## 序\n媒体は欲望を配置する。")
        self.assertTrue(record["excluded"])
        self.assertEqual(record["reason"], "declared_document_scaffolding")

    def test_authoring_research_lead_is_preserved_without_becoming_a_claim(self) -> None:
        result = self.worker._authoring_build_source_preservation_map(
            None,
            {
                "claims": [{"id": "c1", "proposition_id": "p1"}],
                "propositions": [
                    {"id": "p1", "text": "Author argument"},
                    {"id": "p2", "text": "Rival search lead"},
                ],
                "topic_coverage": [
                    {"proposition_ids": ["p1"], "disposition": "central"},
                    {"proposition_ids": ["p2"], "disposition": "research_lead"},
                ],
            },
        ).context
        assert result["source_preservation_map"][1]["disposition"] == "research_lead"
        assert result["source_preservation_contract"]["unplaced_proposition_ids"] == []

    def test_authoring_bounded_retry_researches_only_unresolved_duties_once(self) -> None:
        claimed = ClaimedNode(
            node_run_id="retry-node", run_id="retry-run", node_id="bounded_research_retry",
            attempt=1, workflow_name="authoring", workflow_version="30",
            resource_lane="codex_remote", lease_owner="test-worker",
            project_id="project-1", inputs={},
        )
        context = {
            "use_rag": True,
            "research_queries": ["claim query", "covered query"],
            "research_query_assignments": [
                {"obligation_id": "o1", "query": "claim query", "evidence_requirement": "required"},
                {
                    "obligation_id": "o2",
                    "query": "covered query",
                    "evidence_requirement": "required",
                },
            ],
            "research_search_plan": [
                {"obligation_id": "o1", "query": "claim query", "variants": ["old variant"]},
                {"obligation_id": "o2", "query": "covered query", "variants": ["covered"]},
            ],
            "evidence_coverage": [
                {"obligation_id": "o1", "covered": False, "support_status": "insufficient"},
                {"obligation_id": "o2", "covered": True},
            ],
            "research_discoveries": [
                {"query": "claim query", "candidates": [{"provider_record_id": "old"}]}
            ],
        }

        def search(_claimed, retry_context):
            self.assertEqual(retry_context["research_queries"], ["claim query"])
            self.assertEqual(
                retry_context["research_search_plan"][0]["variants"], ["new variant"]
            )
            return HandlerResult({
                **retry_context,
                "research_discoveries": [
                    {"query": "claim query", "candidates": [{"provider_record_id": "new"}]}
                ],
            })

        with (
            patch.object(self.worker, "_authoring_generate", return_value={
                "plans": [{"unit_id": "o1", "variants": ["old variant", "new variant"]}]
            }),
            patch.object(self.worker, "_manuscript_rewrite_evidence", side_effect=search),
            patch.object(
                self.worker, "_manuscript_validate_evidence_support",
                side_effect=lambda _claimed, value: HandlerResult(value),
            ),
        ):
            result = self.worker._authoring_bounded_research_retry(claimed, context).context
        assert result["research_queries"] == context["research_queries"]
        assert len(result["research_discoveries"][0]["candidates"]) == 2
        assert result["bounded_research_retry_report"]["obligation_ids"] == ["o1"]

    def test_authoring_prose_audit_flags_workflow_status_without_erasing_limits(self) -> None:
        findings = self.worker._authoring_reader_facing_scaffold_findings([
            {
                "section_id": "workflow-status",
                "prose": "提示されたメモではこの資料は参照候補として扱う。今後検討が必要。",
            },
            {
                "section_id": "substantive-limit",
                "prose": "この結論は調査対象の一地域に限られ、他地域には直ちに一般化できない。",
            },
        ])
        self.assertEqual([item["object_id"] for item in findings], ["workflow-status"])
        self.assertEqual(findings[0]["repair_scope"], "local_section")

    def test_authoring_question_requires_japanese_when_output_language_is_ja(self) -> None:
        claimed = ClaimedNode(
            node_run_id="authoring-node",
            run_id="authoring-run",
            node_id="formulate_research_question",
            attempt=1,
            workflow_name="authoring",
            workflow_version="30",
            resource_lane="codex_remote",
            lease_owner="test-worker",
            project_id="project-1",
            inputs={},
        )
        result = {
            "research_question": {
                "id": "rq1",
                "text": "How did media arrangements reshape desire?",
                "scope": "manga",
                "out_of_scope": [],
                "assumptions": [],
                "provisional_answer": "A provisional interpretation",
                "research_gaps": [],
                "rival_interpretations": [],
                "provenance": {
                    "source": "memo",
                    "source_ids": ["p1"],
                    "verification_status": "verified",
                },
            },
            "topic_coverage": [
                {
                    "topic_id": "t1",
                    "label": "媒体配置",
                    "proposition_ids": ["p1"],
                    "disposition": "central",
                    "relation_to_question": "問いの中心を定める。",
                }
            ],
            "required_user_information": [],
        }
        with (
            patch.object(self.worker.authoring_adapter, "generate", return_value=result),
            self.assertRaisesRegex(FukamiruError, "must be Japanese"),
        ):
            self.worker._authoring_formulate_question(
                claimed,
                {
                    "language": "ja",
                    "output_language": "ja",
                    "propositions": [{"id": "p1", "text": "媒体は欲望を配置する。"}],
                    "classifications": [],
                },
            )

    def test_authoring_bounds_oversized_topic_coverage_without_losing_ids(self) -> None:
        result, normalizations = self.worker._authoring_bound_topic_coverage(
            {
                "topic_coverage": [
                    {
                        "topic_id": "t1",
                        "label": "媒体配置",
                        "proposition_ids": [f"p{index}" for index in range(121)],
                        "disposition": "central",
                        "relation_to_question": "中心的な素材。",
                    }
                ]
            }
        )

        topics = result["topic_coverage"]
        self.assertEqual([len(item["proposition_ids"]) for item in topics], [60, 60, 1])
        self.assertEqual(
            [item for topic in topics for item in topic["proposition_ids"]],
            [f"p{index}" for index in range(121)],
        )
        self.assertEqual(normalizations[0]["bounded_topic_count"], 3)

    def test_authoring_question_retries_invalid_provenance_without_previous_output(self) -> None:
        claimed = ClaimedNode(
            node_run_id="authoring-node",
            run_id="authoring-run",
            node_id="formulate_research_question",
            attempt=1,
            workflow_name="authoring",
            workflow_version="30",
            resource_lane="codex_remote",
            lease_owner="test-worker",
            project_id="project-1",
            inputs={},
        )

        def generated(source_id: str) -> dict[str, object]:
            return {
                "research_question": {
                    "id": "rq1",
                    "text": "媒体配置は作品の受容をどう変えるのか。",
                    "scope": "媒体配置。",
                    "out_of_scope": [],
                    "assumptions": [],
                    "provisional_answer": "暫定的な解釈を提示する。",
                    "research_gaps": [],
                    "rival_interpretations": [],
                    "provenance": {
                        "source": "memo",
                        "source_ids": [source_id],
                        "verification_status": "verified",
                    },
                },
                "topic_coverage": [
                    {
                        "topic_id": "t1",
                        "label": "媒体配置",
                        "proposition_ids": ["p1"],
                        "disposition": "central",
                        "relation_to_question": "問いの中心を定める。",
                    }
                ],
                "required_user_information": [],
            }

        with patch.object(
            self.worker.authoring_adapter,
            "generate",
            side_effect=[generated("chunk3"), generated("p1")],
        ) as generate:
            result = self.worker._authoring_formulate_question(
                claimed,
                {
                    "language": "ja",
                    "output_language": "ja",
                    "propositions": [{"id": "p1", "text": "媒体は欲望を配置する。"}],
                    "classifications": [],
                },
            ).context
        self.assertEqual(result["research_question"]["provenance"]["source_ids"], ["p1"])
        self.assertEqual(generate.call_count, 2)
        retry_payload = generate.call_args_list[1].args[1]
        self.assertIn("invalid=['chunk3']", retry_payload["repair_constraints"][0])
        self.assertNotIn("previous_output", retry_payload)

    def test_authoring_repair_only_allows_matching_self_authored_memo_evidence(self) -> None:
        context = {
            "audit_findings": [{
                "code": "provenance_loss", "severity": "major",
                "object_id": "sec-14", "message": "自著の由来を明示する",
            }],
            "reference_entries": [{
                "reference_id": "self-work", "is_self_authored": True,
                "title": "架空資料の比較分析",
                "annotation_proposition_ids": ["p0138"],
            }],
            "argument_nodes": [
                {
                    "id": "b05-e0138", "node_type": "evidence", "claim_id": "c0017",
                    "proposition_id": "p0138",
                    "text": "メモは自著「架空資料の比較分析」を挙げる。",
                    "provenance": {"source": "memo", "verification_status": "unverified"},
                },
                {
                    "id": "unrelated", "node_type": "evidence", "claim_id": "c0017",
                    "proposition_id": "p9999", "text": "自著の別の証拠",
                    "provenance": {"source": "memo", "verification_status": "unverified"},
                },
            ],
        }
        contracts = [{"section_id": "sec-14", "claim_ids": ["c0017"],
                      "evidence_ids": []}]

        allowances = self.worker._authoring_repair_provenance_allowances(
            context, contracts
        )

        self.assertEqual([item["node_id"] for item in allowances], ["b05-e0138"])
        self.assertEqual(allowances[0]["reference_id"], "self-work")

    def test_authoring_paragraph_move_mapping_allows_repair_regrouping(self) -> None:
        problems = self.worker._authoring_paragraph_move_mapping_violations(
            "s-intro",
            [
                {"move_indices": [1, 2], "text": "第一段落。"},
                {"move_indices": [2, 3, 4, 5], "text": "第二段落。"},
                {"move_indices": [3, 5, 6], "text": "第三段落。"},
                {"move_indices": [4, 5, 6], "text": "第四段落。"},
            ],
            required_move_count=6,
            expected_groups=[[1, 2], [3, 4, 5], [6]],
        )

        self.assertEqual(problems, [])

    def test_v31_paragraph_move_labels_are_optional_metadata(self) -> None:
        self.assertEqual(
            self.worker._authoring_v31_paragraph_mapping_violations(
                "s-body", [{"move_indices": [], "text": "節の結論を示す。"}], 6
            ),
            [],
        )
        self.assertEqual(
            self.worker._authoring_v31_paragraph_mapping_violations(
                "s-body", [{"move_indices": [7], "text": "節の結論を示す。"}], 6
            ),
            [],
        )

    def test_v31_branch_labels_flow_from_catalog_to_draft_move_indices(self) -> None:
        claimed = ClaimedNode(
            node_run_id="authoring-node",
            run_id="authoring-run",
            node_id="plan_semantic_paragraphs",
            attempt=1,
            workflow_name="authoring",
            workflow_version="31",
            resource_lane="codex_remote",
            lease_owner="test-worker",
            project_id="project-1",
            inputs={},
        )
        generated = {
            "sections": [
                {
                    "section_id": "s-multibranch",
                    "paragraphs": [
                        {
                            "rhetorical_moves": ["parallel_branch_contribution"],
                            "claim_ids": ["branch-a", "branch-b"],
                            "evidence_ids": [],
                            "central_claim": None,
                            "consequence": None,
                            "development_instruction": (
                                "各枝の寄与を区別し、関係は未確定のまま置く。"
                            ),
                        },
                        {
                            "rhetorical_moves": ["cross_branch_synthesis"],
                            "claim_ids": ["branch-a", "branch-b"],
                            "evidence_ids": ["evidence-link"],
                            "central_claim": "二つの枝には資料で支えられた関係がある。",
                            "consequence": "その関係の含意を限定して示す。",
                            "development_instruction": "根拠のある関係とその射程を説明する。",
                        },
                    ],
                }
            ]
        }
        context = {
            "output_language": "ja",
            "research_problem": {"research_question": "RQ"},
            "conclusion_contract": {},
            "section_contracts": [
                {
                    "section_id": "s-multibranch",
                    "development_expectations": {"required_argument_moves": []},
                }
            ],
            "manuscript_structure_plan": {},
            "argument_nodes": [],
        }

        with patch.object(self.worker, "_authoring_generate", return_value=generated) as generate:
            result = self.worker._authoring_plan_semantic_paragraphs(claimed, context).context

        self.assertEqual(generate.call_args.args[1], "semantic_paragraphs_v31")
        supplied_catalog = generate.call_args.kwargs["payload"][
            "paragraph_argument_move_catalog"
        ]
        branch_labels = {
            item["id"]
            for item in supplied_catalog
            if item["id"]
            in {"parallel_branch_contribution", "cross_branch_synthesis"}
        }
        self.assertEqual(
            branch_labels, {"parallel_branch_contribution", "cross_branch_synthesis"}
        )
        plan = result["semantic_paragraph_plan"][0]
        self.assertEqual([item["move_indices"] for item in plan["paragraphs"]], [[1], [2]])
        self.assertEqual(
            result["section_contracts"][0]["development_expectations"][
                "required_argument_moves"
            ],
            ["parallel_branch_contribution", "cross_branch_synthesis"],
        )

        normalized = self.worker._authoring_normalize_generated_section_ids(
            {
                **result,
                "plan_id": "plan-1",
                "plan_revision": 1,
                "language": "ja",
            },
            [
                {
                    "section_id": "s-multibranch",
                    "paragraphs": [
                        {
                            "move_indices": [1],
                            "text": "各枝の寄与を分け、未解決の関係を留保する。",
                        },
                        {
                            "move_indices": [2],
                            "text": "根拠で確認できる枝間の関係と含意を示す。",
                        },
                    ],
                }
            ],
        )[0]
        self.assertEqual(
            set(normalized["paragraph_moves_realized"]),
            {"parallel_branch_contribution", "cross_branch_synthesis"},
        )

    def test_v31_required_scholarship_citations_come_from_locked_obligations(self) -> None:
        context = {
            "research_problem": {
                "prior_positions": [{"source_ids": ["cite-intro"]}]
            },
            "section_contracts": [
                {
                    "section_id": "s-intro",
                    "article_role": "introduction",
                    "claim_ids": ["c-intro"],
                },
                {
                    "section_id": "s-body",
                    "article_role": "body",
                    "claim_ids": ["c-body"],
                },
                {
                    "section_id": "s-conclusion",
                    "article_role": "conclusion",
                    "claim_ids": ["c-conclusion"],
                },
            ],
            "authoring_atomic_evidence_contracts": [
                {
                    "realization": "attribute_theory_with_citation",
                    "claim_ids": ["c-body"],
                    "citation_evidence_ids": ["cite-body"],
                }
            ],
            "authoring_rejected_evidence_ids_by_section": {
                "s-body": ["arg-body"]
            },
            "audit_findings": [
                {
                    "code": "evidence_mismatch",
                    "severity": "major",
                    "object_id": "s-body",
                    "message": (
                        "arg-body support_sentence names a different work; "
                        "correct this citation placement."
                    ),
                }
            ],
            "evidence_context": [
                {
                    "id": "cite-intro",
                    "allowed_use": "attributed_scholarship_citation",
                    "reference_id": "ref-intro",
                },
                {
                    "id": "cite-body",
                    "allowed_use": "attributed_scholarship_citation",
                    "reference_id": "ref-body",
                },
                {
                    "id": "related-candidate",
                    "allowed_use": "attributed_scholarship_citation",
                    "reference_id": "ref-other",
                },
            ],
            "reference_entries": [
                {"reference_id": "ref-intro"},
                {"reference_id": "ref-body"},
                {"reference_id": "ref-other"},
            ],
            "argument_nodes": [
                {
                    "id": "arg-intro",
                    "node_type": "evidence",
                    "claim_id": "c-intro",
                    "source_span_id": "cite-intro",
                },
                {
                    "id": "arg-body",
                    "node_type": "evidence",
                    "claim_id": "c-body",
                    "source_span_id": "cite-body",
                },
                {
                    "id": "arg-other",
                    "node_type": "evidence",
                    "claim_id": "c-body",
                    "source_span_id": "related-candidate",
                },
            ],
        }

        self.assertEqual(
            self.worker._authoring_required_scholarship_citation_ids(context),
            {"cite-intro", "cite-body"},
        )
        self.assertEqual(
            self.worker._authoring_rejected_evidence_ids_by_section(context)[
                "s-body"
            ],
            set(),
        )
        body_contract = {
            "section_id": "s-body",
            "article_role": "body",
            "claim_ids": ["c-body"],
            "evidence_ids": [],
        }
        projected = self.worker._authoring_draft_contracts_with_scholarship(
            context, [body_contract]
        )[0]
        self.assertEqual(projected["required_scholarship_evidence_ids"], ["arg-body"])
        self.assertEqual(
            projected["citation_placement_repair_evidence_ids"], ["arg-body"]
        )
        required_handoff, required_guidance = (
            self.worker._authoring_v31_required_scholarship_handoff(projected)
        )
        self.assertEqual(
            required_handoff,
            {"required_scholarship_evidence_ids": ["arg-body"]},
        )
        self.assertIn("arg-body", required_guidance)
        self.assertIn("mandatory obligations", required_guidance)
        self.assertIn("evidence_treatment", required_guidance)

    def test_v31_required_citation_handoff_carries_bounded_author_attribution(self) -> None:
        citation_id = "annotation-citation:white"
        argument_evidence_id = "arg-white"
        context = {
            "research_problem": {},
            "section_contracts": [
                {
                    "section_id": "s-white",
                    "article_role": "body",
                    "claim_ids": ["c-white"],
                    "evidence_ids": [],
                }
            ],
            "authoring_atomic_evidence_contracts": [
                {
                    "realization": "attribute_theory_with_citation",
                    "support_status": "verified_attribution_only",
                    "verification_scope": "theoretical_attribution",
                    "claim_ids": ["c-white"],
                    "citation_evidence_ids": [citation_id],
                    "assertion": "フィクションも時代の生活を考える資料となりうる",
                    "source_span": (
                        "ホワイト『メタヒストリー』："
                        "フィクションも時代の生活を考える資料となりうる。"
                    ),
                    "may_assert_as_external_fact": False,
                }
            ],
            "evidence_context": [
                {
                    "id": citation_id,
                    "allowed_use": "attributed_scholarship_citation",
                    "authors": ["ホワイト、ヘイドン"],
                    "title": "メタヒストリー",
                    "reference_id": "ref-white",
                    "verification_scope": "bibliographic_identity_only",
                    "provenance": {
                        "bibliographic_identity_verified": True,
                        "substantive_support_verified": False,
                    },
                }
            ],
            "annotation_citation_evidence": [],
            "reference_entries": [
                {"reference_id": "ref-white", "title": "メタヒストリー"}
            ],
            "argument_nodes": [
                {
                    "id": argument_evidence_id,
                    "node_type": "evidence",
                    "claim_id": "c-white",
                    "source_span_id": citation_id,
                    "provenance": {"source_ids": [citation_id]},
                }
            ],
        }

        obligations = self.worker._authoring_v31_required_scholarship_obligations(
            context, context["section_contracts"]
        )

        self.assertEqual(len(obligations), 1)
        obligation = obligations[0]
        self.assertEqual(obligation["evidence_id"], argument_evidence_id)
        self.assertEqual(
            obligation["author_supplied_assertions"],
            ["フィクションも時代の生活を考える資料となりうる"],
        )
        self.assertEqual(obligation["support_status"], "verified_attribution_only")
        self.assertFalse(
            obligation["may_assert_as_independently_verified_external_fact"]
        )

        guidance = self.worker._authoring_v31_required_scholarship_guidance(
            [argument_evidence_id]
        )
        self.assertIn("author_supplied_assertion", guidance)
        self.assertIn("explicit author/work attribution", guidance)
        self.assertNotIn("remove the unsupported description", guidance)

    def test_v31_citation_purpose_alignment_extracts_visible_contrast_clause(self) -> None:
        evidence_id = "annotation-citation-node:halberstam"
        citation_id = "annotation-citation:halberstam"
        sentence = (
            "筆者がハルバースタム『失敗のクィアアート : 反乱するアニメーション』"
            "から引き取る忘却は、家族・異性愛・資本主義的成功・主体性の連続から"
            "外れる契機であり、身分上昇以外の結末を読む対照となる。"
        )
        section = {
            "section_id": "s009",
            "article_role": "body",
            "paragraphs": [
                {"evidence_ids": [evidence_id], "text": sentence}
            ],
            "evidence_treatment": [
                {
                    "evidence_id": evidence_id,
                    "prose_treatment": "attributed_scholarship",
                    "support_sentence": sentence,
                    "attribution_sentence": sentence,
                    "citation_purpose": "忘却の議論を階級上昇以外の結末を読むために用いる。",
                    "purpose_clause": "身分上昇以外の結末を読む対照として",
                }
            ],
        }
        context = {
            "evidence_context": [
                {
                    "id": citation_id,
                    "allowed_use": "attributed_scholarship_citation",
                    "authors": ["ハルバースタム、ジュディス"],
                    "title": "失敗のクィアアート : 反乱するアニメーション",
                    "reference_id": "ref-halberstam",
                }
            ],
            "annotation_citation_evidence": [],
            "reference_entries": [
                {
                    "reference_id": "ref-halberstam",
                    "title": "失敗のクィアアート : 反乱するアニメーション",
                }
            ],
            "argument_nodes": [
                {
                    "id": evidence_id,
                    "node_type": "evidence",
                    "claim_id": "c007",
                    "source_span_id": citation_id,
                }
            ],
        }

        normalized, corrections = self.worker._authoring_align_visible_citation_metadata(
            context, [section]
        )

        treatment = normalized[0]["evidence_treatment"][0]
        self.assertEqual(
            treatment["purpose_clause"], "身分上昇以外の結末を読む対照となる。"
        )
        self.assertIn("visible-extractive-citation-metadata-v1", [
            item["policy"] for item in corrections
        ])

    def test_v31_failed_repair_reuses_only_prior_same_run_section_checkpoint(self) -> None:
        run_id = self._create_run("manuscript_generator")
        operation_name = WorkerRunner._authoring_v31_repair_operation_name(
            node_attempt=4,
            role="body",
            section_id="s002",
            contract_retry=1,
        )
        result = {
            "sections": [
                {
                    "section_id": "s002",
                    "article_role": "body",
                    "paragraphs": [{"text": "A section."}],
                }
            ]
        }
        now = utc_now()
        with self.databases.state.connect() as connection:
            connection.execute(
                """
                INSERT INTO llm_result_checkpoints(
                    id, run_id, node_run_id, node_id, stage, canonical_stage,
                    operation_name, request_fingerprint_sha256, full_prompt_sha256,
                    cache_family_sha256, result_json, validation_json, status,
                    created_at, updated_at
                ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'accepted', ?, ?)
                """,
                (
                    "test-prior-repair-section",
                    run_id,
                    "repair_failed_sections",
                    "draft_body_v31",
                    "draft_body_v31",
                    operation_name,
                    "test-request-fingerprint",
                    "test-full-prompt-hash",
                    "test-cache-family-hash",
                    json.dumps(result),
                    "{}",
                    now,
                    now,
                ),
            )
            connection.commit()
        claimed = ClaimedNode(
            node_run_id="new-node-run",
            run_id=run_id,
            node_id="repair_failed_sections",
            attempt=5,
            workflow_name="authoring",
            workflow_version="31",
            resource_lane="codex_remote",
            lease_owner="worker",
            project_id=None,
            inputs={},
        )

        reused, reused_operation = self.worker._authoring_v31_previous_repair_checkpoint(
            claimed,
            {
                "authoring_recovery_lineage": {
                    "source_node_id": "repair_failed_sections"
                }
            },
            role="body",
            section_id="s002",
        )
        self.assertEqual(reused, result)
        self.assertEqual(reused_operation, operation_name)

        finding_not_reused, finding_operation = (
            self.worker._authoring_v31_previous_repair_checkpoint(
                claimed,
                {
                    "authoring_recovery_lineage": {
                        "source_node_id": "repair_failed_sections"
                    }
                },
                role="body",
                section_id="s002",
                audit_findings=[
                    {
                        "code": "bare_bibliographic_citation",
                        "object_id": "s002",
                        "severity": "major",
                    }
                ],
            )
        )
        self.assertIsNone(finding_not_reused)
        self.assertIsNone(finding_operation)

        not_reused, not_reused_operation = self.worker._authoring_v31_previous_repair_checkpoint(
            claimed,
            {"authoring_recovery_lineage": {"source_node_id": "join_draft_audits"}},
            role="body",
            section_id="s002",
        )
        self.assertIsNone(not_reused)
        self.assertIsNone(not_reused_operation)

    def test_v31_audit_resolution_requires_explicit_section_output_replacement(
        self,
    ) -> None:
        replacement = (
            "異なる作風の作品が『アフタヌーン』でどう並んだかを、"
            "前二作の掲載号から調べる。"
            "誌面上の配置と別媒体での接触は別の比較項目である"
        )
        resolutions = self.worker._authoring_v31_mandatory_audit_resolutions(
            [
                {
                    "code": "section_output_gap",
                    "severity": "major",
                    "object_id": "s005",
                    "message": (
                        f"「{replacement}」と、未確認の並びを断定せずに"
                        "節の比較上の成果を示す。"
                    ),
                }
            ]
        )

        self.assertEqual(
            resolutions[0]["required_replacement_texts"],
            [replacement],
        )

    def test_v31_conclusion_claim_id_handoff_fills_contract_metadata_only(self) -> None:
        sections = [
            {
                "section_id": "s-body",
                "article_role": "body",
                "claim_ids": ["c-body-kept"],
            },
            {
                "section_id": "s-conclusion",
                "article_role": "conclusion",
                "claim_ids": ["c-existing", "c-extra"],
            },
        ]
        contracts = [
            {
                "section_id": "s-body",
                "article_role": "body",
                "claim_ids": ["c-body-required"],
            },
            {
                "section_id": "s-conclusion",
                "article_role": "conclusion",
                "claim_ids": ["c-existing", "c-required", "c-required"],
            },
        ]

        normalized, corrections = (
            self.worker._authoring_v31_normalize_conclusion_claim_ids(
                sections, contracts
            )
        )

        self.assertIs(normalized, sections)
        self.assertEqual(normalized[0]["claim_ids"], ["c-body-kept"])
        self.assertEqual(
            normalized[1]["claim_ids"],
            ["c-existing", "c-extra", "c-required"],
        )
        self.assertEqual(
            corrections,
            [
                {
                    "section_id": "s-conclusion",
                    "policy": "conclusion-contract-claim-id-union-v1",
                    "added_claim_ids": ["c-required"],
                }
            ],
        )

    def test_authoring_restores_only_citation_paragraph_from_preproof_html(self) -> None:
        source_html = (
            '<article><section><p id="p-cite">著者名『著作』はこの点を論じる。</p>'
            '<p id="p-other">元の文章。</p></section></article>'
        )
        candidate_html = (
            '<article><section><p id="p-cite">この点が論じられる。</p>'
            '<p id="p-other">校正で整えた文章。</p></section></article>'
        )

        restored = self.worker._authoring_restore_paragraph_blocks(
            candidate_html, source_html, ["p-cite"]
        )
        root = ET.fromstring(restored)
        paragraphs = {item.get("id"): "".join(item.itertext()) for item in root.iter("p")}

        self.assertEqual(paragraphs["p-cite"], "著者名『著作』はこの点を論じる。")
        self.assertEqual(paragraphs["p-other"], "校正で整えた文章。")

    def test_v31_graph_findings_continue_without_midrun_human_review(self) -> None:
        with self.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="Graph basis review", root_path=str(self.root / "graph-basis-review")
            )
            run = RunRepository(connection).create(
                workflow_name="manuscript_generator", workflow_version="31",
                entry_node="build_argument_graph", project_id=project_id,
                inputs={"project_id": project_id}, resource_lane="codex_remote",
            )
            node_id = connection.execute(
                "SELECT id FROM node_runs WHERE run_id=? AND node_id='build_argument_graph'",
                (run.run_id,),
            ).fetchone()["id"]
        claimed = ClaimedNode(
            node_run_id=node_id, run_id=run.run_id, node_id="build_argument_graph",
            attempt=1, workflow_name="manuscript_generator", workflow_version="31",
            resource_lane="codex_remote", lease_owner="test-worker",
            project_id=project_id, inputs={},
        )
        context = {
            "claims": [{"id": "c0001", "text": "主張", "proposition_id": "p1"}],
            "propositions": [{"id": "p1", "text": "命題"}],
            "classifications": [{"proposition_id": "p1", "type": "interpretation"}],
            "evidence_context": [],
        }

        def graph(basis_id: str) -> dict[str, object]:
            return {
                "nodes": [
                    {"id": node_id, "node_type": kind, "text": kind,
                     "claim_id": "c0001", "proposition_id": "p1" if kind == "evidence" else None,
                     "provenance": {"source": "memo", "source_ids": ["p1"],
                                    "verification_status": "unverified"}}
                    for node_id, kind in (
                        ("claim-1", "claim"), ("evidence-1", "evidence"),
                        ("warrant-1", "warrant"),
                    )
                ],
                "edges": [
                    {"id": "support-1", "source_id": "evidence-1",
                     "target_id": "claim-1", "relation": "supports"},
                    {"id": "warrant-edge-1", "source_id": "warrant-1",
                     "target_id": "claim-1", "relation": "warrants"},
                ],
                "claim_decisions": [{
                    "claim_id": "c0001", "disposition": "adopt", "reason": "根拠を確認",
                    "counterinterpretation": "別の解釈", "adopted_text": "主張",
                }],
                "claim_scopes": [{
                    "claim_id": "c0001", "proposition": "主張", "scope_conditions": [],
                    "epistemic_status": "supported_interpretation",
                    "evidence_basis_ids": [basis_id],
                }],
            }

        with patch.object(
            self.worker, "_authoring_generate", return_value=graph("missing-1")
        ) as generate:
            result = self.worker._authoring_build_argument_graph(claimed, context)
        self.assertFalse(result.waiting_human)
        self.assertEqual(generate.call_count, 2)
        packet = result.context["argument_graph_validation_findings"][0]
        self.assertEqual(packet["invalid_scopes"][0]["invalid_evidence_basis_ids"], ["missing-1"])
        self.assertEqual(
            packet["invalid_scopes"][0]["available_evidence_nodes"][0]["id"],
            "evidence-1",
        )
        self.assertEqual(
            packet["candidate_graph"]["claim_scopes"][0]["evidence_basis_ids"],
            ["missing-1"],
        )
        with self.databases.state.connect() as connection:
            review_count = connection.execute(
                "SELECT count(*) FROM human_reviews WHERE run_id=?", (run.run_id,)
            ).fetchone()[0]
        self.assertEqual(review_count, 0)
        self.assertEqual(result.context["claim_scopes"][0]["evidence_basis_ids"], [])

        routed = graph("evidence-1")
        routed["edges"][0]["target_id"] = "warrant-1"
        with patch.object(self.worker, "_authoring_generate", return_value=routed) as generate:
            routed_result = self.worker._authoring_build_argument_graph(claimed, context)
        self.assertFalse(routed_result.waiting_human)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(routed_result.context["argument_graph_validation_findings"], [])

        cross_claim_route = graph("evidence-1")
        cross_claim_route["edges"][0]["target_id"] = "warrant-1"
        cross_claim_route["nodes"][2]["claim_id"] = "other-claim"
        with patch.object(
            self.worker, "_authoring_generate", return_value=cross_claim_route
        ):
            cross_claim_result = self.worker._authoring_build_argument_graph(
                claimed, context
            )
        self.assertFalse(cross_claim_result.waiting_human)
        self.assertTrue(any(
            "support nodes lack an edge to their claim node" in problem
            for problem in cross_claim_result.context[
                "argument_graph_validation_findings"
            ][0]["problems"]
        ))

        invalid_edge = graph("evidence-1")
        invalid_edge["edges"][0]["source_id"] = "missing-node"
        with patch.object(self.worker, "_authoring_generate", return_value=invalid_edge):
            edge_result = self.worker._authoring_build_argument_graph(claimed, context)
        self.assertFalse(edge_result.waiting_human)
        self.assertEqual(
            edge_result.context["argument_graph_validation_findings"][0]
            ["invalid_edges"][0]["source_id"],
            "missing-node",
        )
        known_node_ids = {item["id"] for item in edge_result.context["argument_nodes"]}
        self.assertTrue(all(
            edge["source_id"] in known_node_ids and edge["target_id"] in known_node_ids
            for edge in edge_result.context["argument_edges"]
        ))

        shared_citation = graph("evidence-1")
        shared_citation["nodes"].append({
            "id": "annotation-citation:shared", "node_type": "evidence",
            "text": "理論的な参照", "claim_id": None, "proposition_id": "p1",
            "provenance": {"source": "memo", "source_ids": ["annotation-citation:shared"],
                           "verification_status": "unverified"},
        })
        shared_citation["edges"].append({
            "id": "citation-edge", "source_id": "annotation-citation:shared",
            "target_id": "warrant-1", "relation": "supports",
        })
        shared_citation["claim_scopes"][0]["evidence_basis_ids"].append(
            "annotation-citation:shared"
        )
        with patch.object(self.worker, "_authoring_generate", return_value=shared_citation):
            shared_result = self.worker._authoring_build_argument_graph(claimed, context)
        self.assertFalse(shared_result.waiting_human)
        self.assertIn(
            "annotation-citation:shared",
            shared_result.context["claim_scopes"][0]["evidence_basis_ids"],
        )

        missing_rag_source = graph("evidence-1")
        missing_rag_source["nodes"][1]["provenance"] = {
            "source": "rag", "source_ids": [], "verification_status": "unverified",
        }
        with patch.object(self.worker, "_authoring_generate", return_value=missing_rag_source):
            source_result = self.worker._authoring_build_argument_graph(claimed, context)
        self.assertFalse(source_result.waiting_human)
        self.assertIn(
            "evidence node evidence-1 claims retrieved-source support without a source ID",
            source_result.context["argument_graph_validation_findings"][0]["problems"],
        )

        with patch.object(self.worker, "_authoring_generate", return_value=graph("warrant-1")):
            wrong_type_result = self.worker._authoring_build_argument_graph(claimed, context)
        self.assertFalse(wrong_type_result.waiting_human)
        self.assertEqual(
            wrong_type_result.context["claim_scopes"][0]["evidence_basis_ids"], [],
        )

        missing_decision = graph("evidence-1")
        missing_decision["claim_decisions"] = []
        with patch.object(self.worker, "_authoring_generate", return_value=missing_decision):
            contract_result = self.worker._authoring_build_argument_graph(claimed, context)
        self.assertFalse(contract_result.waiting_human)
        self.assertIn(
            "argument graph omitted claim decisions",
            contract_result.context["argument_graph_validation_findings"][0]["problems"],
        )
        self.assertEqual(
            contract_result.context["argument_claim_decisions"][0]["disposition"],
            "research_again",
        )

    def test_v31_draft_missing_required_source_continues_to_final_review(self) -> None:
        fixed = build_fixed_assets({
            "evidence_context": [{"id": "source-1", "text": "確認済みの原典"}],
        })
        claimed = ClaimedNode(
            node_run_id="node", run_id="run", node_id="draft_rewrite", attempt=1,
            workflow_name="manuscript_generator", workflow_version="31",
            resource_lane="codex_remote", lease_owner="test-worker",
            project_id="project", inputs={},
        )
        context = {
            "fixed_assets": fixed.model_dump(mode="json"),
            "research_problem": {"research_question": "問い"},
            "introduction_contract": {}, "conclusion_contract": {},
            "authoring_atomic_evidence_contracts": [{
                "realization": "attribute_theory_with_citation",
                "claim_ids": ["c1"], "citation_evidence_ids": ["book"],
                "assertion": "著者が記録した理論上の区別",
            }],
            "evidence_context": [{
                "id": "book", "reference_id": "reference:book",
                "allowed_use": "attributed_scholarship_citation",
                "title": "A Book", "authors": ["Scholar"],
            }],
            "reference_entries": [],
            "argument_nodes": [
                {"id": "e1", "node_type": "evidence", "claim_id": "c1",
                 "text": "理論上の区別", "provenance": {"source_ids": ["book"]}},
                {"id": "e2", "node_type": "evidence", "claim_id": "c1",
                 "text": "確認できる別の資料",
                 "provenance": {"source_ids": ["source-1"]}},
            ],
            "section_contracts": [
                {"section_id": "intro", "article_role": "introduction",
                 "evidence_ids": []},
                {"section_id": "body", "article_role": "body",
                 "claim_ids": ["c1"], "evidence_ids": ["e1", "e2"]},
                {"section_id": "end", "article_role": "conclusion",
                 "evidence_ids": []},
            ],
        }
        observed = {}

        class DraftGenerationReached(Exception):
            pass

        def inspect_draft_plan(value, sections):
            observed["context"] = value
            observed["sections"] = sections
            raise DraftGenerationReached

        with (
            patch.object(self.worker, "_register_artifact", return_value=None),
            patch.object(self.worker, "_authoring_reconcile_theory_citation_titles",
                         side_effect=lambda item, **_kwargs: item),
            patch.object(self.worker, "_authoring_drop_unusable_aggregated_self_citation_support",
                         side_effect=lambda item: (item, [])),
            patch.object(self.worker, "_authoring_attach_annotation_citations",
                         side_effect=lambda item: item),
            patch.object(self.worker, "_authoring_normalize_reader_facing_contract_language",
                         side_effect=lambda item: (item, [])),
            patch.object(self.worker, "_authoring_reader_content_brief_v31",
                         side_effect=inspect_draft_plan),
            self.assertRaises(DraftGenerationReached),
        ):
            self.worker._authoring_draft_sections_v31(claimed, context)

        candidate = observed["context"]
        self.assertEqual(candidate["section_contracts"], context["section_contracts"])
        self.assertEqual(candidate["authoring_deferred_evidence_ids_by_section"]["body"], ["e1"])
        self.assertEqual(candidate["authoring_draft_source_deferrals"][0]["original_node"]["text"],
                         "理論上の区別")
        self.assertEqual(candidate["authoring_unresolved_required_citations"][0]["source_id"],
                         "book")
        self.assertEqual(
            candidate["authoring_unresolved_required_citations"][0]["author_supplied_assertions"],
            ["著者が記録した理論上の区別"],
        )
        self.assertTrue(any(
            item["category"] == "required_citation_source_unresolved"
            and item["source_ids"] == ["book"] and item["status"] == "open"
            for item in candidate["human_review_items"]
        ))
        effective = self.worker._authoring_draft_contracts_with_scholarship(
            candidate, [dict(item) for item in candidate["section_contracts"]]
        )
        self.assertEqual(effective[1]["evidence_ids"], ["e2"])
        self.assertEqual(effective[1]["required_scholarship_evidence_ids"], [])
        from fukamiru.authoring.evidence_contracts import HumanReviewItem, result_status

        self.assertEqual(
            result_status(
                review_items=[
                    HumanReviewItem.model_validate(item)
                    for item in candidate["human_review_items"]
                ],
                blocking_findings=[],
            ),
            "completed_with_review",
        )
    def test_authoring_argument_graph_retries_until_every_claim_has_support(self) -> None:
        claimed = ClaimedNode(
            node_run_id="authoring-node",
            run_id="authoring-run",
            node_id="build_argument_graph",
            attempt=1,
            workflow_name="authoring",
            workflow_version="30",
            resource_lane="codex_remote",
            lease_owner="test-worker",
            project_id="project-1",
            inputs={},
        )
        provenance = {
            "source": "llm_inference",
            "source_ids": [],
            "verification_status": "unverified",
        }

        def node(node_id: str, node_type: str) -> dict[str, object]:
            return {
                "id": node_id,
                "node_type": node_type,
                "text": node_type,
                "claim_id": "c1",
                "proposition_id": None,
                "source_span_id": None,
                "provenance": provenance,
            }

        invalid = {"nodes": [node("claim-c1", "claim")], "edges": []}
        valid_nodes = [
            node("claim-c1", "claim"),
            node("e-c1", "evidence"),
            node("w-c1", "warrant"),
            node("q-c1", "qualifier"),
            node("x-c1", "counterexample"),
        ]
        valid = {
            "nodes": valid_nodes,
            "claim_decisions": [{
                "claim_id": "c1", "disposition": "adopt", "reason": "Evidence and warrant align",
                "counterinterpretation": "A competing reading", "adopted_text": "主張",
            }],
            "edges": [
                {
                    "id": f"edge-{item['id']}",
                    "source_id": item["id"],
                    "target_id": "claim-c1",
                    "relation": relation,
                }
                for item, relation in zip(
                    valid_nodes[1:],
                    ["supports", "warrants", "qualifies", "challenges"],
                    strict=True,
                )
            ],
        }
        context = {
            "language": "ja",
            "claims": [{"id": "c1", "text": "主張", "proposition_id": "p1"}],
            "propositions": [{"id": "p1", "text": "命題"}],
            "classifications": [{"proposition_id": "p1", "type": "fact"}],
            "evidence_context": [],
        }
        with (
            patch.object(
                self.worker.authoring_adapter,
                "generate",
                side_effect=[invalid, valid],
            ) as generate,
            patch.object(self.worker, "_authoring_retrieval_context", return_value=""),
        ):
            result = self.worker._authoring_build_argument_graph(claimed, context).context
        self.assertEqual(len(result["argument_nodes"]), 5)
        self.assertEqual(generate.call_count, 2)
        retry_payload = generate.call_args_list[1].args[1]
        self.assertTrue(
            any(
                "claim c1 missing evidence,warrant" in constraint
                for constraint in retry_payload["repair_constraints"]
            )
        )
        self.assertNotIn("previous_output", retry_payload)

    def test_authoring_argument_graph_batches_large_claim_sets(self) -> None:
        claimed = ClaimedNode(
            node_run_id="authoring-node", run_id="authoring-run",
            node_id="build_argument_graph", attempt=1,
            workflow_name="authoring", workflow_version="30",
            resource_lane="codex_remote", lease_owner="test-worker",
            project_id="project-1", inputs={},
        )
        context = {
            "claims": [
                {"id": f"c{i}", "text": f"Claim {i}",
                 "provenance": {"source_ids": [f"p{i}"]}}
                for i in range(9)
            ],
            "propositions": [
                {"id": f"p{i}", "text": f"Premise {i}"}
                for i in range(9)
            ],
            "classifications": [
                {"proposition_id": f"p{i}", "type": "fact"}
                for i in range(9)
            ],
            "evidence_context": [{"id": "citation-8"}],
            "authoring_atomic_evidence_contracts": [{
                "proposition_id": "p8", "claim_ids": ["c8"],
                "realization": "attribute_theory_with_citation",
                "citation_evidence_ids": ["citation-8"],
            }],
        }
        batch_sizes = []

        def generate(*args, **kwargs):
            batch = kwargs["payload"]
            batch_sizes.append(len(batch["claims"]))
            self.assertEqual(
                {item["id"] for item in batch["propositions"]},
                {f"p{claim['id'][1:]}" for claim in batch["claims"]},
            )
            nodes, edges, decisions = [], [], []
            for index, claim in enumerate(batch["claims"]):
                roles = ["claim", "evidence", "warrant", "counterexample"]
                if claim["id"] == "c8":
                    roles.append("qualifier")
                node_ids = [f"n{index}-{role}" for role in roles]
                nodes.extend({
                    "id": node_id, "node_type": role,
                    "text": role, "claim_id": claim["id"],
                    "proposition_id": (
                        f"p{claim['id'][1:]}" if role == "evidence" else None
                    ),
                    "provenance": {"source": "memo",
                                   "source_ids": [],
                                   "verification_status": "unverified"},
                } for node_id, role in zip(node_ids, roles, strict=True))
                edges.extend({
                    "id": f"edge-{node_id}", "source_id": node_id,
                    "target_id": node_ids[0], "relation": "supports",
                } for node_id in node_ids[1:])
                decisions.append({
                    "claim_id": claim["id"],
                    "disposition": (
                        "research_again" if claim["id"] == "c8" else "adopt"
                    ),
                    "reason": "Supported", "counterinterpretation": "Alternative",
                    "adopted_text": (
                        "仮説として検討する" if claim["id"] == "c8"
                        else claim["text"]
                    ),
                })
            return {"nodes": nodes, "edges": edges,
                    "claim_decisions": decisions}

        with patch.object(self.worker, "_authoring_generate", side_effect=generate):
            result = self.worker._authoring_build_argument_graph(claimed, context).context
        self.assertEqual(batch_sizes, [4, 4, 1])
        node_ids = [item["id"] for item in result["argument_nodes"]]
        self.assertEqual(len(node_ids), 37)
        self.assertEqual(len(node_ids), len(set(node_ids)))
        self.assertEqual(len(result["argument_edges"]), 28)
        attributed = next(
            item for item in result["argument_nodes"]
            if item.get("proposition_id") == "p8"
        )
        self.assertIn("citation-8", attributed["provenance"]["source_ids"])
        self.assertEqual(
            result["argument_graph_citation_normalizations"][0]["proposition_id"],
            "p8",
        )
        self.assertEqual(
            result["post_research_claim_reconstruction"]["provisional_claim_ids"],
            ["c8"],
        )
        self.assertEqual(
            result["required_user_information"][0]["affected_scope"], "c8"
        )
        self.assertEqual(result["claims"][8]["text"], "仮説として検討する")

    def test_v31_graph_repairs_bibliographic_and_memo_attribution_per_batch(self) -> None:
        claimed = ClaimedNode(
            node_run_id="graph-node", run_id="graph-run",
            node_id="build_argument_graph", attempt=1,
            workflow_name="authoring", workflow_version="31",
            resource_lane="codex_remote", lease_owner="test-worker",
            project_id="project-1", inputs={},
        )
        claims = [
            {"id": f"c{i}", "text": f"Claim {i}",
             "provenance": {"source_ids": [f"p{i}"]}}
            for i in range(9)
        ]
        context = {
            "claims": claims,
            "propositions": [
                {"id": f"p{i}", "text": f"Memo proposition {i}"}
                for i in range(9)
            ],
            "classifications": [
                {"proposition_id": f"p{i}", "type": "interpretation"}
                for i in range(9)
            ],
            "evidence_context": [
                {"id": evidence_id} for evidence_id in
                ("cite-0", "cite-5", "cite-6", "external-7")
            ],
            "authoring_atomic_evidence_contracts": [
                {"proposition_id": "p0", "claim_ids": ["c0"],
                 "realization": "attribute_theory_with_citation",
                 "citation_evidence_ids": ["cite-0"]},
                {"proposition_id": "p1", "claim_ids": ["c1"],
                 "realization": "attribute_to_memo"},
                {"proposition_id": "p5", "claim_ids": ["c5"],
                 "realization": "attribute_theory_with_citation",
                 "citation_evidence_ids": ["cite-5"]},
            ],
            "authoring_evidence_contracts": [
                {"claim_id": "c5", "support_status": "qualified_inference_only"},
                {"claim_id": "c6", "support_status": "qualified_inference_only"},
                {"claim_id": "c7", "support_status": "verified_external",
                 "accepted_evidence_ids": ["external-7"]},
            ],
        }

        def generate(*args, **kwargs):
            batch_claims = kwargs["payload"]["claims"]
            self.assertEqual(
                {item["claim_id"] for item in args[2]["authoring_evidence_contracts"]},
                {item["claim_id"] for item in context["authoring_evidence_contracts"]
                 if item["claim_id"] in {claim["id"] for claim in batch_claims}},
            )
            nodes, edges, decisions, scopes = [], [], [], []
            for claim in batch_claims:
                claim_id = claim["id"]
                index = int(claim_id[1:])
                evidence_id = f"e-{claim_id}"
                source_ids = {
                    0: ["reference-0"], 5: ["cite-5"],
                    6: ["cite-6"], 7: ["external-7"],
                }.get(index, [])
                verified = index in {5, 6, 7}
                source = "rag" if index in {0, 1, 5, 6, 7} else "memo"
                nodes.extend([
                    {"id": f"claim-{claim_id}", "node_type": "claim",
                     "text": claim["text"], "claim_id": claim_id,
                     "provenance": {"source": "memo", "source_ids": [],
                                    "verification_status": "unverified"}},
                    {"id": evidence_id, "node_type": "evidence",
                     "text": "Unattributed model wording", "claim_id": claim_id,
                     "proposition_id": f"p{index}",
                     "provenance": {"source": source, "source_ids": source_ids,
                                    "verification_status": (
                                        "verified" if verified else "unverified"
                                    )}},
                    {"id": f"w-{claim_id}", "node_type": "warrant",
                     "text": "Warrant", "claim_id": claim_id,
                     "provenance": {"source": "llm_inference", "source_ids": [],
                                    "verification_status": "unverified"}},
                ])
                edges.extend([
                    {"id": f"edge-e-{claim_id}", "source_id": evidence_id,
                     "target_id": f"claim-{claim_id}", "relation": "supports"},
                    {"id": f"edge-w-{claim_id}", "source_id": f"w-{claim_id}",
                     "target_id": f"claim-{claim_id}", "relation": "warrants"},
                ])
                decisions.append({
                    "claim_id": claim_id, "disposition": "adopt",
                    "reason": "A provisional reading", "counterinterpretation": "Alternative",
                    "adopted_text": claim["text"],
                })
                scopes.append({
                    "claim_id": claim_id, "proposition": claim["text"],
                    "scope_conditions": [], "epistemic_status": "provisional",
                    "evidence_basis_ids": [evidence_id],
                })
            return {"nodes": nodes, "edges": edges,
                    "claim_decisions": decisions, "claim_scopes": scopes}

        with patch.object(self.worker, "_authoring_generate", side_effect=generate) as call:
            result = self.worker._authoring_build_argument_graph(claimed, context).context

        self.assertEqual(call.call_count, 3)
        evidence = {
            item["claim_id"]: item for item in result["argument_nodes"]
            if item["node_type"] == "evidence"
        }
        for claim_id, citation_id in (("c0", "cite-0"), ("c5", "cite-5")):
            node = evidence[claim_id]
            self.assertIn(citation_id, node["provenance"]["source_ids"])
            self.assertEqual(node["provenance"]["verification_status"], "unverified")
            self.assertIn("メモでは", node["text"])
        self.assertEqual(evidence["c1"]["provenance"]["source"], "memo")
        self.assertIn("Memo proposition 1", evidence["c1"]["text"])
        self.assertEqual(evidence["c6"]["provenance"]["verification_status"], "unverified")
        self.assertEqual(evidence["c7"]["provenance"]["verification_status"], "verified")
        self.assertTrue(result["argument_graph_provenance_normalizations"])

    def test_v31_graph_restores_locked_core_inference_without_model_repair(self) -> None:
        claimed = ClaimedNode(
            node_run_id="authoring-node", run_id="authoring-run",
            node_id="build_argument_graph", attempt=1,
            workflow_name="authoring", workflow_version="31",
            resource_lane="codex_remote", lease_owner="test-worker",
            project_id="project-1", inputs={},
        )
        claims = [
            {
                "id": f"c{i}", "text": f"Claim {i}", "proposition_id": f"p{i}",
                "provenance": {
                    "source_ids": [f"p{i}", "p-extra"] if i == 1 else [f"p{i}"],
                },
            }
            for i in range(9)
        ]
        context = {
            "claims": claims,
            "propositions": [
                {"id": f"p{i}", "text": f"Premise {i}"} for i in range(9)
            ] + [{"id": "p-extra", "text": "Core inference"}],
            "classifications": [
                {"proposition_id": f"p{i}", "type": "interpretation"}
                for i in range(9)
            ] + [{"proposition_id": "p-extra", "type": "interpretation"}],
            "authoring_atomic_evidence_contracts": [
                {
                    "proposition_id": "p-extra", "claim_ids": ["c1"],
                    "argument_necessity": "core", "realization": "qualified_inference",
                },
                {
                    "proposition_id": "p0", "claim_ids": ["c0"],
                    "argument_necessity": "core",
                    "realization": "attribute_theory_with_citation",
                    "citation_evidence_ids": ["cite-0"],
                },
            ],
            "evidence_context": [{"id": "cite-0"}],
        }
        calls: list[tuple[int, bool]] = []

        def generate(*args, **kwargs):
            batch = kwargs["payload"]
            repairing = bool(batch.get("repair_constraints"))
            self.assertEqual(
                {
                    item.get("realization")
                    for item in args[2]["authoring_atomic_evidence_contracts"]
                },
                {item.get("realization") for item in batch["evidence"]["atomic_contracts"]},
            )
            calls.append((len(batch["claims"]), repairing))
            nodes, edges, decisions, scopes = [], [], [], []
            for index, claim in enumerate(batch["claims"]):
                claim_id = claim["id"]
                claim_node = f"n{index}-claim"
                evidence_node = f"n{index}-evidence"
                nodes.extend([
                    {
                        "id": claim_node, "node_type": "claim", "text": claim["text"],
                        "claim_id": claim_id, "proposition_id": None,
                        "provenance": {"source": "memo", "source_ids": [],
                                       "verification_status": "unverified"},
                    },
                    {
                        "id": evidence_node, "node_type": "evidence", "text": "inference",
                        "claim_id": claim_id, "proposition_id": claim["proposition_id"],
                        "provenance": {
                            "source": "memo" if claim_id == "c0" else "llm_inference",
                            "source_ids": [], "verification_status": "unverified",
                        },
                    },
                    {
                        "id": f"n{index}-warrant", "node_type": "warrant",
                        "text": "reason", "claim_id": claim_id,
                        "proposition_id": None,
                        "provenance": {"source": "llm_inference", "source_ids": [],
                                       "verification_status": "unverified"},
                    },
                ])
                edges.extend([
                    {"id": f"edge-{index}-e", "source_id": evidence_node,
                     "target_id": claim_node, "relation": "supports"},
                    {"id": f"edge-{index}-w", "source_id": f"n{index}-warrant",
                     "target_id": claim_node, "relation": "warrants"},
                ])
                if claim_id == "c1":
                    nodes.extend([
                        {
                            "id": "counterexample-c1", "node_type": "counterexample",
                            "text": "objection", "claim_id": claim_id,
                            "provenance": {"source": "llm_inference", "source_ids": [],
                                           "verification_status": "unverified"},
                        },
                        {
                            "id": "rebuttal-c1", "node_type": "rebuttal",
                            "text": "answer", "claim_id": claim_id,
                            "provenance": {"source": "llm_inference", "source_ids": [],
                                           "verification_status": "unverified"},
                        },
                    ])
                    edges.extend([
                        {"id": "edge-c1-challenge", "source_id": "counterexample-c1",
                         "target_id": claim_node, "relation": "challenges"},
                        {"id": "edge-c1-rebuttal", "source_id": "rebuttal-c1",
                         "target_id": "counterexample-c1", "relation": "rebuts"},
                    ])
                decisions.append({
                    "claim_id": claim_id, "disposition": "adopt",
                    "reason": "Supported", "counterinterpretation": "Alternative",
                    "adopted_text": claim["text"],
                })
                scopes.append({
                    "claim_id": claim_id, "proposition": claim["text"],
                    "scope_conditions": [], "epistemic_status": "provisional",
                    "evidence_basis_ids": [evidence_node],
                })
            return {"nodes": nodes, "edges": edges,
                    "claim_decisions": decisions, "claim_scopes": scopes}

        with patch.object(self.worker, "_authoring_generate", side_effect=generate):
            result = self.worker._authoring_build_argument_graph(claimed, context).context

        self.assertEqual(calls, [(4, False), (4, False), (1, False)])
        self.assertTrue(any(
            edge["relation"] == "rebuts"
            for edge in result["argument_edges"]
        ))
        restored = next(
            node for node in result["argument_nodes"]
            if node.get("proposition_id") == "p-extra"
        )
        self.assertEqual(restored["text"], "Core inference")
        self.assertEqual(restored["provenance"]["verification_status"], "unverified")
        self.assertEqual(
            result["core_qualified_inference_restorations"][0]["proposition_id"],
            "p-extra",
        )

    def test_v31_argument_graph_repairs_optional_rebuttal_links(self) -> None:
        context = {
            "claims": [{"id": "c1", "text": "主張", "proposition_id": "p1"}],
            "propositions": [{"id": "p1", "text": "根拠"}],
            "classifications": [{"proposition_id": "p1", "type": "interpretation"}],
            "evidence_context": [],
        }

        def claimed(version: str) -> ClaimedNode:
            return ClaimedNode(
                node_run_id="graph-node", run_id="graph-run",
                node_id="build_argument_graph", attempt=1,
                workflow_name="authoring", workflow_version=version,
                resource_lane="codex_remote", lease_owner="test-worker",
                project_id="project-1", inputs={},
            )

        def graph(mode: str, version: str) -> dict[str, object]:
            def node(node_id: str, kind: str) -> dict[str, object]:
                return {
                    "id": node_id, "node_type": kind, "text": kind,
                    "claim_id": "c1", "proposition_id": "p1" if kind == "evidence" else None,
                    "provenance": {
                        "source": "memo" if kind == "evidence" else "llm_inference",
                        "source_ids": ["p1"] if kind == "evidence" else [],
                        "verification_status": "unverified",
                    },
                }

            edges = [
                {"id": "e-evidence", "source_id": "e1", "target_id": "claim-c1",
                 "relation": "supports"},
                {"id": "e-warrant", "source_id": "w1", "target_id": "claim-c1",
                 "relation": "warrants"},
            ]
            if mode != "missing_challenge":
                edges.append({
                    "id": "e-challenge", "source_id": "x1", "target_id": "claim-c1",
                    "relation": "challenges",
                })
            if mode != "orphan":
                edges.append({
                    "id": "e-rebuttal", "source_id": "r1", "target_id": "x1",
                    "relation": "rebuts",
                })
            result: dict[str, object] = {
                "nodes": [
                    node("claim-c1", "claim"), node("e1", "evidence"),
                    node("w1", "warrant"), node("x1", "counterexample"),
                    node("r1", "rebuttal"),
                ],
                "edges": edges,
                "claim_decisions": [{
                    "claim_id": "c1", "disposition": "adopt", "reason": "論証できる",
                    "counterinterpretation": "別の解釈", "adopted_text": "主張",
                }],
            }
            if version == "31":
                result["claim_scopes"] = [{
                    "claim_id": "c1", "proposition": "主張", "scope_conditions": [],
                    "epistemic_status": "provisional", "evidence_basis_ids": ["e1"],
                }]
            return result

        for mode in ("chain", "missing_challenge", "orphan"):
            with self.subTest(mode=mode), patch.object(
                self.worker, "_authoring_generate",
                side_effect=lambda *_args, _mode=mode, **_kwargs: graph(_mode, "31"),
            ) as generate:
                result = self.worker._authoring_build_argument_graph(
                    claimed("31"), context
                ).context
            self.assertEqual(generate.call_count, 2 if mode == "orphan" else 1)
            rebuttals = [
                item for item in result["argument_nodes"]
                if item["node_type"] == "rebuttal"
            ]
            self.assertEqual(len(rebuttals), 0 if mode == "orphan" else 1)
            repairs = result["argument_graph_rebuttal_normalizations"]
            if mode == "chain":
                self.assertEqual(repairs, [])
            elif mode == "missing_challenge":
                self.assertEqual(repairs[0]["action"], "connected_counterexample_to_claim")
                self.assertTrue(any(
                    edge["source_id"] == "x1"
                    and edge["target_id"] == "claim-c1"
                    and edge["relation"] == "challenges"
                    for edge in result["argument_edges"]
                ))
            else:
                self.assertEqual(repairs[0]["action"], "dropped_unattached_optional_rebuttal")
                self.assertFalse(any(
                    "r1" in (edge["source_id"], edge["target_id"])
                    for edge in result["argument_edges"]
                ))

        with patch.object(
            self.worker, "_authoring_generate",
            side_effect=lambda *_args, **_kwargs: graph("chain", "30"),
        ) as generate, self.assertRaisesRegex(
            FukamiruError, "support nodes lack an edge to their claim node"
        ):
            self.worker._authoring_build_argument_graph(claimed("30"), context)
        self.assertEqual(generate.call_count, 2)

    def test_authoring_argument_graph_allows_unrealized_supporting_atoms(self) -> None:
        claimed = ClaimedNode(
            node_run_id="authoring-node",
            run_id="authoring-run",
            node_id="build_argument_graph",
            attempt=1,
            workflow_name="authoring",
            workflow_version="30",
            resource_lane="codex_remote",
            lease_owner="test-worker",
            project_id="project-1",
            inputs={},
        )
        provenance = {
            "source": "llm_inference",
            "source_ids": [],
            "verification_status": "unverified",
        }

        def node(
            node_id: str, node_type: str, proposition_id: str | None = None
        ) -> dict[str, object]:
            return {
                "id": node_id,
                "node_type": node_type,
                "text": node_type,
                "claim_id": "c1",
                "proposition_id": proposition_id,
                "source_span_id": None,
                "provenance": provenance,
            }

        nodes = [
            node("claim-c1", "claim"),
            node("e-c1", "evidence", "p-core"),
            node("w-c1", "warrant"),
            node("q-c1", "qualifier"),
            node("x-c1", "counterexample"),
        ]
        generated = {
            "nodes": nodes,
            "claim_decisions": [{
                "claim_id": "c1", "disposition": "narrow", "reason": "A bounded inference",
                "counterinterpretation": "Another reading", "adopted_text": "範囲を縮めた主張",
            }],
            "edges": [
                {
                    "id": f"edge-{item['id']}",
                    "source_id": item["id"],
                    "target_id": "claim-c1",
                    "relation": relation,
                }
                for item, relation in zip(
                    nodes[1:],
                    ["supports", "warrants", "qualifies", "challenges"],
                    strict=True,
                )
            ],
        }
        context = {
            "language": "ja",
            "claims": [{"id": "c1", "text": "主張", "proposition_id": "p-core"}],
            "propositions": [
                {"id": "p-core", "text": "中心命題"},
                {"id": "p-support", "text": "補助命題"},
            ],
            "classifications": [
                {"proposition_id": "p-core", "type": "interpretation"},
                {"proposition_id": "p-support", "type": "interpretation"},
            ],
            "evidence_context": [],
            "authoring_atomic_evidence_contracts": [
                {
                    "proposition_id": "p-core",
                    "claim_ids": ["c1"],
                    "argument_necessity": "core",
                    "realization": "qualified_inference",
                },
                {
                    "proposition_id": "p-support",
                    "claim_ids": ["c1"],
                    "argument_necessity": "supporting",
                    "realization": "qualified_inference",
                },
            ],
        }
        with (
            patch.object(
                self.worker.authoring_adapter,
                "generate",
                return_value=generated,
            ) as generate,
            patch.object(self.worker, "_authoring_retrieval_context", return_value=""),
        ):
            result = self.worker._authoring_build_argument_graph(claimed, context).context

        self.assertEqual(len(result["argument_nodes"]), 5)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(result["claims"][0]["text"], "範囲を縮めた主張")
        self.assertEqual(result["argument_nodes"][0]["text"], "範囲を縮めた主張")

        rejected = {
            **generated,
            "claim_decisions": [
                {**generated["claim_decisions"][0], "disposition": "reject"}
            ],
        }
        with (
            patch.object(self.worker.authoring_adapter, "generate", return_value=rejected),
            patch.object(self.worker, "_authoring_retrieval_context", return_value=""),
            self.assertRaisesRegex(FukamiruError, "cannot draft rejected claims"),
        ):
            self.worker._authoring_build_argument_graph(claimed, context)

    def test_authoring_discourse_retries_compressed_subclaim_structure(self) -> None:
        claimed = ClaimedNode(
            node_run_id="authoring-node",
            run_id="authoring-run",
            node_id="build_discourse_tree",
            attempt=1,
            workflow_name="authoring",
            workflow_version="30",
            resource_lane="codex_remote",
            lease_owner="test-worker",
            project_id="project-1",
            inputs={},
        )

        def tree(section_count: int) -> dict[str, object]:
            child_ids = [f"d{index}" for index in range(1, section_count + 1)]
            section_claims = (
                [["main", "sub-1"], ["main", "sub-2"]]
                if section_count == 2
                else [
                    ["main"],
                    ["sub-1"],
                    ["sub-1", "sub-2"],
                    ["main", "sub-1", "sub-2"],
                ]
            )
            return {
                "nodes": [
                    {
                        "id": "d0",
                        "section_id": None,
                        "article_role": "root",
                        "relation": "root",
                        "parent_id": None,
                        "child_ids": child_ids,
                        "section_answer": "",
                        "attentional_focus": "問い",
                        "discourse_purpose": "全体の論証を統括する",
                        "claim_ids": ["main"],
                    },
                    *[
                        {
                            "id": f"d{index}",
                            "section_id": f"s{index}",
                            "article_role": (
                                "introduction"
                                if index == 1
                                else "conclusion"
                                if index == section_count
                                else "body"
                            ),
                            "relation": "contrast" if index == 3 else "elaboration",
                            "parent_id": "d0",
                            "child_ids": [],
                            "section_answer": f"Section {index} establishes its conclusion.",
                            "attentional_focus": f"節{index}",
                            "discourse_purpose": f"推論段階{index}",
                            "claim_ids": section_claims[index - 1],
                        }
                        for index in range(1, section_count + 1)
                    ],
                ]
            }

        context = {
            "claims": [
                {"id": "main", "level": "main_claim", "parent_id": None},
                {"id": "sub-1", "level": "subclaim", "parent_id": "main"},
                {"id": "sub-2", "level": "subclaim", "parent_id": "main"},
            ],
            "argument_nodes": [],
            "argument_edges": [],
            "hypotheses": [],
        }
        with patch.object(
            self.worker.authoring_adapter,
            "generate",
            side_effect=[tree(2), tree(4)],
        ) as generate:
            result = self.worker._authoring_build_discourse_tree(
                claimed, context
            ).context
        self.assertEqual(len(result["discourse_nodes"]), 5)
        self.assertEqual(generate.call_count, 2)
        self.assertIn(
            "required_body=2, actual=0",
            generate.call_args_list[1].args[1]["repair_constraints"][0],
        )

    def test_v30_discourse_retains_its_rhetorical_relation_variety_check(self) -> None:
        claimed = ClaimedNode(
            node_run_id="authoring-node",
            run_id="authoring-run",
            node_id="build_discourse_tree",
            attempt=1,
            workflow_name="authoring",
            workflow_version="30",
            resource_lane="codex_remote",
            lease_owner="test-worker",
            project_id="project-1",
            inputs={},
        )

        def tree(*, diverse: bool) -> dict[str, object]:
            sections = [
                ("s-intro", "introduction", "background", ["main"]),
                ("s-body1", "body", "elaboration", ["sub-1"]),
                (
                    "s-body2",
                    "body",
                    "contrast" if diverse else "elaboration",
                    ["sub-2"],
                ),
                ("s-body3", "body", "elaboration", ["sub-2"]),
                ("s-conclusion", "conclusion", "result", ["main"]),
            ]
            nodes = [
                {
                    "id": "root",
                    "section_id": None,
                    "article_role": "root",
                    "relation": "root",
                    "parent_id": None,
                    "child_ids": [f"d{index}" for index in range(len(sections))],
                    "section_answer": "",
                    "attentional_focus": "global",
                    "discourse_purpose": "global argument",
                    "claim_ids": ["main"],
                }
            ]
            for index, (section_id, role, relation, claim_ids) in enumerate(sections):
                nodes.append(
                    {
                        "id": f"d{index}",
                        "section_id": section_id,
                        "article_role": role,
                        "relation": relation,
                        "parent_id": "root",
                        "child_ids": [],
                        "section_answer": f"Section {index} establishes a result.",
                        "attentional_focus": f"focus {index}",
                        "discourse_purpose": f"purpose {index}",
                        "claim_ids": claim_ids,
                    }
                )
            return {"nodes": nodes}

        context = {
            "claims": [
                {"id": "main", "level": "main_claim", "parent_id": None},
                {"id": "sub-1", "level": "subclaim", "parent_id": "main"},
                {"id": "sub-2", "level": "subclaim", "parent_id": "main"},
            ],
            "argument_nodes": [],
            "argument_edges": [],
            "hypotheses": [],
        }
        with patch.object(
            self.worker.authoring_adapter,
            "generate",
            side_effect=[tree(diverse=False), tree(diverse=True)],
        ) as generate:
            self.worker._authoring_build_discourse_tree(claimed, context)

        self.assertEqual(generate.call_count, 2)
        self.assertIn(
            "developed body discourse must use more than one rhetorical relation",
            generate.call_args_list[1].args[1]["repair_constraints"],
        )

    def test_v31_promoted_topic_can_share_a_multibranch_body_phase(self) -> None:
        claimed = ClaimedNode(
            node_run_id="authoring-node",
            run_id="authoring-run",
            node_id="build_discourse_tree",
            attempt=1,
            workflow_name="authoring",
            workflow_version="31",
            resource_lane="codex_remote",
            lease_owner="test-worker",
            project_id="project-1",
            inputs={},
        )

        def tree() -> dict[str, object]:
            body_claims = [
                [f"sub-{index}" for index in range(1, 6)],
                ["sub-6"],
                ["sub-6"],
            ]
            section_claims = [
                ["main"],
                *body_claims,
                ["main", *[f"sub-{index}" for index in range(1, 7)]],
            ]
            nodes = [
                {
                    "id": "root",
                    "section_id": None,
                    "article_role": "root",
                    "relation": "root",
                    "parent_id": None,
                    "child_ids": [f"d{index}" for index in range(len(section_claims))],
                    "section_answer": "",
                    "attentional_focus": "global",
                    "discourse_purpose": "global argument",
                    "claim_ids": ["main"],
                }
            ]
            for index, claim_ids in enumerate(section_claims):
                role = (
                    "introduction"
                    if index == 0
                    else "conclusion"
                    if index == len(section_claims) - 1
                    else "body"
                )
                nodes.append(
                    {
                        "id": f"d{index}",
                        "section_id": f"s{index}",
                        "article_role": role,
                        "relation": "elaboration",
                        "parent_id": "root",
                        "child_ids": [],
                        "section_answer": f"Section {index} establishes a result.",
                        "attentional_focus": f"focus {index}",
                        "discourse_purpose": f"purpose {index}",
                        "claim_ids": claim_ids,
                    }
                )
            return {"nodes": nodes}

        context = {
            "claims": [
                {"id": "main", "level": "main_claim", "parent_id": None},
                *[
                    {"id": f"sub-{index}", "level": "subclaim", "parent_id": "main"}
                    for index in range(1, 7)
                ],
            ],
            "argument_nodes": [],
            "argument_edges": [],
            "hypotheses": [],
            "claim_hierarchy_normalizations": [
                {
                    "claim_id": "sub-2",
                    "reason": "promote_grounded_unrepresented_topic",
                }
            ],
        }
        with patch.object(
            self.worker.authoring_adapter,
            "generate",
            side_effect=[tree()],
        ) as generate:
            result = self.worker._authoring_build_discourse_tree(claimed, context).context

        self.assertEqual(generate.call_count, 1)
        promoted_sections = [
            node
            for node in result["discourse_nodes"]
            if node.get("article_role") == "body" and "sub-2" in node.get("claim_ids", [])
        ]
        self.assertEqual(
            [node["claim_ids"] for node in promoted_sections],
            [[f"sub-{index}" for index in range(1, 6)]],
        )

    def test_v31_discourse_does_not_require_cross_branch_synthesis(self) -> None:
        claimed = ClaimedNode(
            node_run_id="authoring-node",
            run_id="authoring-run",
            node_id="build_discourse_tree",
            attempt=1,
            workflow_name="authoring",
            workflow_version="31",
            resource_lane="codex_remote",
            lease_owner="test-worker",
            project_id="project-1",
            inputs={},
        )

        def tree(*, omit_subclaim: str | None = None) -> dict[str, object]:
            sections = [
                ("s-intro", "introduction", "background", ["main"], "問いを示す"),
                ("s-body1", "body", "elaboration", ["sub-1"], "第一の枝の結果を示す"),
                ("s-body2", "body", "elaboration", ["sub-2"], "第二の枝の結果を示す"),
                (
                    "s-body3",
                    "body",
                    "elaboration",
                    ["sub-2"] if omit_subclaim == "sub-3" else ["sub-3"],
                    "第三の枝の結果を示す",
                ),
                (
                    "s-conclusion",
                    "conclusion",
                    "result",
                    ["main"],
                    "枝間の関係を確定せず、現時点の全体回答を示す",
                ),
            ]
            nodes = [
                {
                    "id": "root",
                    "section_id": None,
                    "article_role": "root",
                    "relation": "root",
                    "parent_id": None,
                    "child_ids": [f"d{index}" for index in range(len(sections))],
                    "section_answer": "",
                    "attentional_focus": "論文全体",
                    "discourse_purpose": "全体の論証を統括する",
                    "claim_ids": ["main"],
                }
            ]
            for index, (section_id, role, relation, claim_ids, purpose) in enumerate(
                sections
            ):
                nodes.append(
                    {
                        "id": f"d{index}",
                        "section_id": section_id,
                        "article_role": role,
                        "relation": relation,
                        "parent_id": "root",
                        "child_ids": [],
                        "section_answer": f"節{index}は論証上の結果を確立する。",
                        "attentional_focus": f"論点{index}",
                        "discourse_purpose": purpose,
                        "claim_ids": claim_ids,
                    }
                )
            return {"nodes": nodes}

        context = {
            "research_problem": {"research_question": "RQ"},
            "topic_coverage": [],
            "claims": [
                {"id": "main", "level": "main_claim", "parent_id": None},
                {"id": "sub-1", "level": "subclaim", "parent_id": "main"},
                {"id": "sub-2", "level": "subclaim", "parent_id": "main"},
                {"id": "sub-3", "level": "subclaim", "parent_id": "main"},
            ],
            "argument_nodes": [],
            "argument_edges": [],
            "hypotheses": [],
        }
        with patch.object(
            self.worker.authoring_adapter,
            "generate",
            side_effect=[tree()],
        ) as generate:
            result = self.worker._authoring_build_discourse_tree(
                claimed, context
            ).context

        self.assertEqual(generate.call_count, 1)
        body_sections = [
            node
            for node in result["discourse_nodes"]
            if node.get("article_role") == "body"
        ]
        self.assertEqual(
            [node["claim_ids"] for node in body_sections],
            [["sub-1"], ["sub-2"], ["sub-3"]],
        )
        conclusion = next(
            node
            for node in result["discourse_nodes"]
            if node.get("article_role") == "conclusion"
        )
        self.assertEqual(conclusion["claim_ids"], ["main"])

        with patch.object(
            self.worker.authoring_adapter,
            "generate",
            side_effect=[tree(omit_subclaim="sub-3"), tree(omit_subclaim="sub-3")],
        ) as generate, self.assertRaisesRegex(
            FukamiruError, "claims absent from discourse reconstruction"
        ):
            self.worker._authoring_build_discourse_tree(claimed, context)
        self.assertEqual(generate.call_count, 2)

    def test_authoring_section_contracts_retry_invented_section_id(self) -> None:
        claimed = ClaimedNode(
            node_run_id="authoring-node",
            run_id="authoring-run",
            node_id="lock_section_contracts",
            attempt=1,
            workflow_name="authoring",
            workflow_version="30",
            resource_lane="codex_remote",
            lease_owner="test-worker",
            project_id="project-1",
            inputs={},
        )

        def section(section_id: str, article_role: str) -> dict[str, object]:
            return {
                "section_id": section_id,
                "section_answer": "The section establishes a conclusion.",
                "title": "節",
                "local_question": "問い",
                "article_role": article_role,
                "expected_conclusion": "結論",
                "claim_ids": ["c1"],
                "evidence_ids": ["e1"],
                "warrant_ids": ["w1"],
                "qualifiers": ["q1"],
                "counterarguments": ["x1"],
                "discourse_relation_to_parent": "evidence",
                "previous_section_bridge": "",
                "next_section_bridge": "",
                "required_terms": [],
                "forbidden_topics": [],
                "development_expectations": {
                    "minimum_evidence_items": 1,
                    "minimum_concrete_examples": 1,
                    "required_argument_moves": [
                        "orientation",
                        "claim",
                        "evidence",
                        "warrant",
                        "qualification",
                    ],
                },
                "target_length": None,
            }

        def introduction(section_id: str) -> dict[str, object]:
            return {
                "section_id": section_id,
                "genre_profile": "Japanese humanities article",
                "moves": [
                    {"move": move, "purpose": purpose, "claim_ids": ["c1"]}
                    for move, purpose in (
                        ("research_object_background", "対象の歴史的背景を示す"),
                        ("problem_or_question", "現在見えにくい問題を示す"),
                        ("positive_purpose_or_thesis", "本稿の目的を示す"),
                        ("scope_and_materials", "資料と範囲を示す"),
                        ("method_or_analytic_axis", "比較軸を示す"),
                    )
                ],
            }

        provenance = {
            "source": "llm_inference",
            "source_ids": [],
            "verification_status": "unverified",
        }
        context = {
            "plan_id": "plan-1",
            "plan_revision": 1,
            "document_id": "document-1",
            "source_revision_id": "revision-1",
            "language": "ja",
            "research_question": {
                "text": "問い",
                "scope": "範囲",
                "out_of_scope": [],
                "assumptions": [],
            },
            "propositions": [],
            "classifications": [],
            "claims": [
                {
                    "id": "c1",
                    "parent_id": None,
                    "level": "main_claim",
                    "text": "主張",
                    "proposition_id": None,
                    "answers_question": True,
                    "provenance": provenance,
                }
            ],
            "argument_nodes": [
                {
                    "id": node_id,
                    "node_type": node_type,
                    "text": node_type,
                    "claim_id": "c1",
                    "proposition_id": None,
                    "source_span_id": None,
                    "provenance": provenance,
                }
                for node_id, node_type in (
                    ("claim-c1", "claim"),
                    ("e1", "evidence"),
                    ("w1", "warrant"),
                    ("q1", "qualifier"),
                    ("x1", "counterexample"),
                )
            ],
            "argument_edges": [],
            "hypotheses": [],
            "discourse_nodes": [
                {
                    "id": "d0",
                    "section_id": None,
                    "article_role": "root",
                    "relation": "root",
                    "parent_id": None,
                    "child_ids": ["d1", "d2", "d3", "d4"],
                    "attentional_focus": "問い",
                    "discourse_purpose": "全体を統括する",
                    "claim_ids": ["c1"],
                },
                *[
                    {
                        "id": f"d{index}",
                        "section_id": f"s{index}",
                        "article_role": (
                            "introduction"
                            if index == 1
                            else "conclusion"
                            if index == 4
                            else "body"
                        ),
                        "relation": "evidence",
                        "parent_id": "d0",
                        "child_ids": [],
                        "attentional_focus": "主張",
                        "discourse_purpose": "主張を読者向けに展開する",
                        "claim_ids": ["c1"],
                    }
                    for index in range(1, 5)
                ],
            ],
            "requested_total_length": None,
        }
        with (
            patch.object(
                self.worker.authoring_adapter,
                "generate",
                side_effect=[
                    {
                        "introduction_contract": {
                            **introduction("section_framework"),
                            "moves": introduction("section_framework")["moves"][1:],
                        },
                        "sections": [
                            section("section_framework", "introduction"),
                            section("s2", "body"),
                            section("s3", "body"),
                            section("s4", "conclusion"),
                        ]
                    },
                    {
                        "introduction_contract": introduction("s1"),
                        "sections": [
                            section("s1", "introduction"),
                            section("s2", "body"),
                            section("s3", "body"),
                            section("s4", "conclusion"),
                        ]
                    },
                ],
            ) as generate,
            patch(
                "fukamiru.worker.runner.AuthoringRepository.create_plan"
            ) as create_plan,
        ):
            result = self.worker._authoring_lock_contracts(claimed, context).context
        self.assertEqual(result["section_contracts"][0]["section_id"], "s1")
        self.assertEqual(result["introduction_contract"]["section_id"], "s1")
        self.assertIsNone(result["section_contracts"][0]["target_length"])
        self.assertEqual(generate.call_count, 2)
        self.assertIn(
            "invented section IDs: ['section_framework']",
            generate.call_args_list[1].args[1]["repair_constraints"],
        )
        self.assertTrue(
            any(
                "introduction contract is missing required moves" in item
                for item in generate.call_args_list[1].args[1]["repair_constraints"]
            )
        )
        create_plan.assert_called_once()

    def test_authoring_argument_payload_excludes_unlinked_and_provider_diagnostics(self) -> None:
        context = {
            "research_question": {"text": "Why?"},
            "claims": [
                {
                    "id": "c1",
                    "proposition_id": "p1",
                    "provenance": {"source_ids": ["p1"]},
                }
            ],
            "propositions": [{"id": "p1", "text": "keep"}, {"id": "p2", "text": "drop"}],
            "classifications": [
                {"proposition_id": "p1", "type": "fact"},
                {"proposition_id": "p2", "type": "example"},
            ],
            "evidence_coverage": [
                {
                    "finding_id": "c1",
                    "scope_key": "claim:c1",
                    "support_status": "supported",
                    "accepted_evidence_count": 1,
                    "covered": True,
                    "provider_diagnostics": "drop",
                }
            ],
            "research_discoveries": [
                {
                    "query": "q1",
                    "provider_outcomes": ["drop"],
                    "candidates": [
                        {
                            "title": "keep",
                            "excerpt": "x" * 2000,
                            "discovery_quality": {"passed": True},
                            "provenance": {"citation": {"source_span_id": "s1"}},
                        },
                        {
                            "title": "drop",
                            "discovery_quality": {"passed": False},
                        },
                    ],
                }
            ],
            "note_text": "never forward the full note",
        }
        payload = self.worker._authoring_argument_payload(context)
        self.assertEqual([item["id"] for item in payload["propositions"]], ["p1"])
        self.assertEqual(
            [item["proposition_id"] for item in payload["classifications"]], ["p1"]
        )
        self.assertEqual(
            payload["evidence"]["discoveries"][0]["candidates"][0]["title"], "keep"
        )
        self.assertEqual(
            len(payload["evidence"]["discoveries"][0]["candidates"][0]["excerpt"]),
            1200,
        )
        self.assertNotIn("note_text", payload)
        self.assertNotIn("provider_outcomes", json.dumps(payload))

    def test_authoring_atomic_obligation_researches_empirical_memo_provenance(self) -> None:
        claimed = ClaimedNode(
            node_run_id="authoring-node",
            run_id="authoring-run",
            node_id="build_claim_tree",
            attempt=1,
            workflow_name="authoring",
            workflow_version="30",
            resource_lane="codex_remote",
            lease_owner="test-worker",
            project_id="project-1",
            inputs={},
        )
        result = {
            "claims": [
                {
                    "id": "c1",
                    "level": "main_claim",
                    "text": "The memo directly supports this claim.",
                    "parent_id": None,
                    "proposition_id": "p1",
                    "answers_question": True,
                    "provenance": {
                        "source": "memo",
                        "source_ids": ["p1"],
                        "verification_status": "verified",
                    },
                }
            ]
        }
        with patch.object(
            self.worker.authoring_adapter, "generate", return_value=result
        ):
            context = self.worker._authoring_build_claim_tree(
                claimed,
                {
                    "document_id": "document-1",
                    "propositions": [
                        {
                            "id": "p1",
                            "text": "1998年に制度Aが導入された。",
                            "source_span": "1998年に制度Aが導入された。",
                            "provenance": {"source": "memo"},
                        }
                    ],
                    "classifications": [
                        {"proposition_id": "p1", "type": "fact"}
                    ],
                    "research_question": {"text": "Why?"},
                },
            ).context
        context = self.worker._authoring_classify_verification_scopes(
            claimed, context
        ).context
        context = self.worker._authoring_bind_claims_to_propositions(
            claimed, context
        ).context
        with patch(
            "fukamiru.worker.runner.ManuscriptReasoningRepository.save_review",
            return_value={"id": "authoring-review"},
        ):
            context = self.worker._authoring_derive_atomic_evidence_obligations(
                claimed, context
            ).context
        self.assertTrue(context["research_query_candidates"][0]["requires_research"])
        self.assertEqual(len(context["research_query_assignments"]), 1)
        self.assertEqual(
            context["research_query_candidates"][0]["proposition_id"], "p1"
        )

    def test_authoring_normalizes_claim_level_to_explicit_parent_contract(self) -> None:
        claims, changes = self.worker._authoring_normalize_claim_hierarchy(
            [
                {"id": "main", "level": "main_claim", "parent_id": None},
                {"id": "child", "level": "supporting_claim", "parent_id": "main"},
                {"id": "leaf", "level": "supporting_claim", "parent_id": "child"},
            ]
        )
        self.assertEqual([item["level"] for item in claims], [
            "main_claim", "subclaim", "supporting_claim"
        ])
        self.assertEqual(changes[0]["claim_id"], "child")
        self.assertEqual(changes[0]["reason"], "explicit_parent_level_contract")

    def test_v31_join_merges_section_contracts_by_id_and_preserves_move_labels(self) -> None:
        left = {
            "section_contracts": [
                {
                    "section_id": "sec-intro",
                    "article_role": "introduction",
                    "title": "問いを置く",
                    "development_expectations": {
                        "required_argument_moves": ["research_question"],
                        "minimum_evidence_items": 0,
                    },
                },
                {
                    "section_id": "sec-body",
                    "article_role": "body",
                    "claim_ids": ["cl1"],
                    "development_expectations": {
                        "required_argument_moves": [],
                    },
                },
            ]
        }
        right = {
            "section_contracts": [
                {
                    "section_id": "sec-intro",
                    "article_role": "introduction",
                    "title": "問いを置く",
                    "development_expectations": {
                        "required_argument_moves": ["research_question", "contribution"],
                        "minimum_evidence_items": 0,
                    },
                },
                {
                    "section_id": "sec-body",
                    "article_role": "body",
                    "claim_ids": ["cl1"],
                    "development_expectations": {
                        "required_argument_moves": ["case_analysis"],
                    },
                },
            ]
        }

        merged = self.worker._merge_join_contexts(left, right, path="join")

        self.assertEqual(
            [item["section_id"] for item in merged["section_contracts"]],
            ["sec-intro", "sec-body"],
        )
        self.assertEqual(
            merged["section_contracts"][0]["development_expectations"][
                "required_argument_moves"
            ],
            ["research_question", "contribution"],
        )
        self.assertEqual(
            merged["section_contracts"][1]["development_expectations"][
                "required_argument_moves"
            ],
            ["case_analysis"],
        )

    def test_v31_section_contract_join_rejects_non_label_conflicts(self) -> None:
        with self.assertRaisesRegex(
            FukamiruError,
            r"Section contract join conflict at join\.section_contracts\.sec-body\.title",
        ):
            self.worker._merge_join_contexts(
                {
                    "section_contracts": [
                        {"section_id": "sec-body", "title": "A"}
                    ]
                },
                {
                    "section_contracts": [
                        {"section_id": "sec-body", "title": "B"}
                    ]
                },
                path="join",
            )

    def test_v31_paragraph_retry_join_uses_current_plan(self) -> None:
        base = {"section_contracts": [{"section_id": "s01"}]}
        enumerated = {
            **base,
            "paragraph_plan": {"policy": "old-manuscript-plan"},
            "semantic_paragraph_plan": [{"section_id": "s01", "paragraphs": ["old"]}],
            "enumeration_context_contracts": [{"section_id": "s01", "focus": "new"}],
        }
        planned = {
            **base,
            "paragraph_plan": {"policy": "semantic-move-variable-length-v30"},
            "semantic_paragraph_plan": [{"section_id": "s01", "paragraphs": ["new"]}],
            "enumeration_context_contracts": [{"section_id": "s01", "focus": "old"}],
        }

        merged = self.worker._merge_v31_paragraph_formation_contexts(
            enumerated, planned
        )

        self.assertEqual(merged["paragraph_plan"], planned["paragraph_plan"])
        self.assertEqual(
            merged["semantic_paragraph_plan"], planned["semantic_paragraph_plan"]
        )
        self.assertEqual(
            merged["enumeration_context_contracts"],
            enumerated["enumeration_context_contracts"],
        )

    def test_v31_paragraph_retry_join_rejects_section_mismatch(self) -> None:
        with self.assertRaisesRegex(FukamiruError, "mismatched section coverage"):
            self.worker._merge_v31_paragraph_formation_contexts(
                {
                    "section_contracts": [{"section_id": "s01"}],
                    "enumeration_context_contracts": [],
                },
                {"section_contracts": [{"section_id": "s02"}]},
            )

    def test_v31_claim_tree_may_formulate_claim_absent_verbatim_from_source(self) -> None:
        claimed = ClaimedNode(
            node_run_id="authoring-node",
            run_id="authoring-run",
            node_id="build_claim_tree",
            attempt=1,
            workflow_name="authoring",
            workflow_version="31",
            resource_lane="codex_remote",
            lease_owner="test-worker",
            project_id="project-1",
            inputs={},
        )
        source_text = "雑誌ごとに掲載作品の傾向が違う。読者は媒体を通じて作品に接する。"
        synthesized_claim = "雑誌の編成は、作品を読む際の欲望や位置づけを方向づける。"
        generated = {
            "claims": [
                {
                    "id": "cl-main",
                    "level": "main_claim",
                    "text": synthesized_claim,
                    "parent_id": None,
                    "proposition_id": None,
                    "answers_question": True,
                    "provenance": {
                        "source": "llm_inference",
                        "source_ids": ["p1"],
                        "verification_status": "unverified",
                    },
                },
                {
                    "id": "cl-body",
                    "level": "subclaim",
                    "text": "媒体ごとの編成差が、作品の読まれ方に影響する。",
                    "parent_id": "cl-main",
                    "proposition_id": "p1",
                    "answers_question": False,
                    "provenance": {
                        "source": "llm_inference",
                        "source_ids": ["p1"],
                        "verification_status": "unverified",
                    },
                },
            ]
        }
        context = {
            "research_question": {"text": "媒体は作品の読まれ方にどう関わるか。"},
            "topic_coverage": [
                {"topic_id": "t1", "disposition": "central", "proposition_ids": ["p1"]}
            ],
            "propositions": [
                {"id": "p1", "text": source_text, "source_span": source_text}
            ],
            "classifications": [{"proposition_id": "p1", "type": "observation"}],
        }

        with patch.object(
            self.worker, "_authoring_generate", return_value=generated
        ) as generate:
            planned = self.worker._authoring_build_claim_tree(claimed, context).context

        self.assertEqual(planned["claims"][0]["text"], synthesized_claim)
        self.assertNotIn(synthesized_claim, source_text)
        self.assertEqual(planned["claims"][0]["provenance"]["source_ids"], ["p1"])
        policy = generate.call_args.kwargs["payload"]["claim_generation_policy"]
        self.assertTrue(policy["allow_llm_formulation_when_no_explicit_claim_sentence"])
        self.assertIn("Do not invent source IDs", policy["instruction"])

    def test_claim_tree_preserves_omitted_sources_on_existing_topic_branch(self) -> None:
        claimed = ClaimedNode(
            node_run_id="authoring-node",
            run_id="authoring-run",
            node_id="build_claim_tree",
            attempt=1,
            workflow_name="authoring",
            workflow_version="30",
            resource_lane="codex_remote",
            lease_owner="test-worker",
            project_id="project-1",
            inputs={},
        )
        context = {
            "propositions": [
                {"id": "p1", "text": "Topic one premise."},
                {"id": "p2", "text": "Topic one detail."},
                {"id": "p3", "text": "Topic two premise."},
            ],
            "topic_coverage": [
                {"topic_id": "t1", "disposition": "central", "proposition_ids": ["p1", "p2"]},
                {"topic_id": "t2", "disposition": "supporting", "proposition_ids": ["p3"]},
            ],
            "research_question": {},
        }
        result = {"claims": [
            {"id": "main", "level": "main_claim", "parent_id": None,
             "provenance": {"source_ids": ["p1"]}},
            {"id": "topic-one", "level": "subclaim", "parent_id": "main",
             "provenance": {"source_ids": ["p1"]}},
            {"id": "topic-two", "level": "subclaim", "parent_id": "main",
             "provenance": {"source_ids": ["p3"]}},
        ]}
        with patch.object(self.worker, "_authoring_generate", return_value=result):
            planned = self.worker._authoring_build_claim_tree(claimed, context).context
        self.assertEqual(
            planned["claims"][1]["provenance"]["source_ids"], ["p1", "p2"]
        )
        preserved = self.worker._authoring_build_source_preservation_map(
            claimed, planned
        ).context
        self.assertEqual(
            preserved["source_preservation_contract"]["unplaced_proposition_ids"], []
        )

    def test_v31_claim_tree_splits_existing_child_when_two_topics_share_a_branch(self) -> None:
        claimed = ClaimedNode(
            node_run_id="authoring-node",
            run_id="authoring-run",
            node_id="build_claim_tree",
            attempt=1,
            workflow_name="authoring",
            workflow_version="31",
            resource_lane="codex_remote",
            lease_owner="test-worker",
            project_id="project-1",
            inputs={},
        )
        context = {
            "propositions": [
                {"id": item, "text": item}
                for item in ("p1", "p1b", "p2", "p2b")
            ],
            "topic_coverage": [
                {"topic_id": "t1", "disposition": "central", "proposition_ids": ["p1", "p1b"]},
                {"topic_id": "t2", "disposition": "supporting", "proposition_ids": ["p2", "p2b"]},
            ],
            "research_question": {},
        }
        candidate = {
            "claims": [
                {
                    "id": "main",
                    "level": "main_claim",
                    "parent_id": None,
                    "text": "Main claim",
                    "provenance": {"source_ids": ["p1"]},
                },
                {
                    "id": "merged",
                    "level": "subclaim",
                    "parent_id": "main",
                    "text": "Topic one and topic two",
                    "provenance": {"source_ids": ["p1", "p2"]},
                },
                {
                    "id": "existing-child",
                    "level": "supporting_claim",
                    "parent_id": "merged",
                    "text": "Topic two",
                    "provenance": {"source_ids": ["p2", "p2b"]},
                },
            ]
        }
        with patch.object(self.worker, "_authoring_generate", return_value=candidate):
            planned = self.worker._authoring_build_claim_tree(claimed, context).context
        by_id = {item["id"]: item for item in planned["claims"]}
        self.assertEqual(by_id["existing-child"]["level"], "subclaim")
        self.assertEqual(by_id["existing-child"]["parent_id"], "main")
        self.assertIn("p1b", by_id["merged"]["provenance"]["source_ids"])
        self.assertEqual(
            {item["reason"] for item in planned["claim_hierarchy_normalizations"]},
            {"preserve_substantive_topic_sources", "promote_grounded_unrepresented_topic"},
        )
        ungrounded = json.loads(json.dumps(candidate))
        ungrounded["claims"][2]["provenance"]["source_ids"] = ["p2"]
        with (
            patch.object(self.worker, "_authoring_generate", return_value=ungrounded),
            self.assertRaisesRegex(FukamiruError, "distinct subclaim branch"),
        ):
            self.worker._authoring_build_claim_tree(claimed, context)

    def test_v31_ratified_provisional_answer_is_forwarded_to_later_nodes(self) -> None:
        claimed = ClaimedNode(
            node_run_id="authoring-node",
            run_id="authoring-run",
            node_id="ratify_research_problem",
            attempt=1,
            workflow_name="authoring",
            workflow_version="31",
            resource_lane="codex_remote",
            lease_owner="test-worker",
            project_id="project-1",
            inputs={},
        )
        context = {
            "research_question": {
                "text": "資料内の差異はどう説明できるか",
                "provisional_answer": "初期回答",
            },
            "propositions": [{"id": "p1", "text": "同一資料に二例がある"}],
            "claims": [],
            "evidence_context": [],
        }
        ratified_answer = "資料に示された二例の差は掲載位置から説明できる。"
        generated = {
            "research_problem": {
                "research_question": context["research_question"]["text"],
                "prior_positions": [],
                "problematisation": {
                    "type": "source_internal",
                    "description": "同一資料内の二例が異なる。",
                    "evidence_ids": [],
                    "proposition_ids": ["p1"],
                },
                "provisional_answer": ratified_answer,
                "contribution_target": "資料内の差異を説明する。",
            }
        }
        with patch.object(self.worker, "_authoring_generate", return_value=generated):
            result = self.worker._authoring_ratify_research_problem(claimed, context).context

        self.assertEqual(result["research_problem"]["provisional_answer"], ratified_answer)
        self.assertEqual(result["research_question"]["provisional_answer"], ratified_answer)
        self.assertEqual(result["research_question"]["text"], context["research_question"]["text"])
        self.assertTrue(result["research_problem_ratification"]["changed"])
        self.assertEqual(
            result["research_problem_ratification"]["previous_provisional_answer"],
            "初期回答",
        )

    def test_v31_claim_tree_rejects_topic_represented_only_by_mixed_branch(self) -> None:
        claimed = ClaimedNode(
            node_run_id="authoring-node", run_id="authoring-run",
            node_id="build_claim_tree", attempt=1,
            workflow_name="authoring", workflow_version="31",
            resource_lane="codex_remote", lease_owner="test-worker",
            project_id="project-1", inputs={},
        )
        context = {
            "propositions": [
                {"id": item, "text": item} for item in ("p1", "p1b", "p2", "p2b")
            ],
            "topic_coverage": [
                {"topic_id": "t1", "disposition": "central", "proposition_ids": ["p1", "p1b"]},
                {"topic_id": "t2", "disposition": "central", "proposition_ids": ["p2", "p2b"]},
            ],
            "research_question": {},
        }
        candidate = {"claims": [
            {"id": "main", "level": "main_claim", "parent_id": None,
             "provenance": {"source_ids": ["p1"]}},
            {"id": "first", "level": "subclaim", "parent_id": "main",
             "provenance": {"source_ids": ["p1", "p1b", "p2"]}},
            {"id": "second", "level": "subclaim", "parent_id": "main",
             "provenance": {"source_ids": ["p1b", "p2b"]}},
        ]}
        with (
            patch.object(self.worker, "_authoring_generate", return_value=candidate),
            self.assertRaisesRegex(FukamiruError, "topic t2 has no direct subclaim branch"),
        ):
            self.worker._authoring_build_claim_tree(claimed, context)

    def test_v31_claim_tree_promotes_grounded_unrepresented_topic_child(self) -> None:
        claimed = ClaimedNode(
            node_run_id="authoring-node", run_id="authoring-run",
            node_id="build_claim_tree", attempt=1,
            workflow_name="authoring", workflow_version="31",
            resource_lane="codex_remote", lease_owner="test-worker",
            project_id="project-1", inputs={},
        )
        context = {
            "propositions": [{"id": f"p{i}", "text": f"Source {i}"} for i in range(1, 7)],
            "topic_coverage": [
                {"topic_id": "t1", "disposition": "central", "proposition_ids": ["p1", "p2"]},
                {
                    "topic_id": "t2", "disposition": "central",
                    "proposition_ids": ["p3", "p4", "p5", "p6"],
                },
            ],
            "research_question": {
                "id": "research_question_v31",
                "provenance": {"source_ids": ["p1"]},
            },
        }
        candidate = {"claims": [
            {"id": "main", "level": "main_claim", "parent_id": None,
             "provenance": {"source_ids": ["research_question_v31"]}},
            {"id": "first", "level": "subclaim", "parent_id": "main",
             "provenance": {"source_ids": ["p1", "p2"]}},
            {"id": "second", "level": "supporting_claim", "parent_id": "first",
             "provenance": {"source_ids": ["p3", "p4", "p5"]}},
        ]}
        with patch.object(self.worker, "_authoring_generate", return_value=candidate):
            planned = self.worker._authoring_build_claim_tree(claimed, context).context
        second = next(item for item in planned["claims"] if item["id"] == "second")
        self.assertEqual((second["level"], second["parent_id"]), ("subclaim", "main"))
        self.assertEqual(second["provenance"]["source_ids"], ["p3", "p4", "p5", "p6"])
        self.assertIn(
            "promote_grounded_unrepresented_topic",
            {item["reason"] for item in planned["claim_hierarchy_normalizations"]},
        )
        main = next(item for item in planned["claims"] if item["id"] == "main")
        self.assertEqual(main["provenance"]["source_ids"], ["p1"])

    def test_authoring_handoffs_queries_and_canonical_html_to_v29_nodes(self) -> None:
        claimed = ClaimedNode(
            node_run_id="authoring-node",
            run_id="authoring-run",
            node_id="build_claim_tree",
            attempt=1,
            workflow_name="authoring",
            workflow_version="30",
            resource_lane="codex_remote",
            lease_owner="test-worker",
            project_id="project-1",
            inputs={},
        )
        planned = self.worker._authoring_build_claim_tree(
            claimed,
            {
                "document_id": "document-1",
                "propositions": [
                    {
                        "id": "p1",
                        "text": "Evidence supports it.",
                        "source_span": "Evidence supports it.",
                        "provenance": {"source": "memo"},
                    }
                ],
                "classifications": [{"proposition_id": "p1", "type": "fact"}],
                "research_question": {},
            },
        ).context
        planned = self.worker._authoring_classify_verification_scopes(
            claimed, planned
        ).context
        planned = self.worker._authoring_bind_claims_to_propositions(
            claimed, planned
        ).context
        with patch(
            "fukamiru.worker.runner.ManuscriptReasoningRepository.save_review",
            return_value={"id": "authoring-review"},
        ):
            planned = self.worker._authoring_derive_atomic_evidence_obligations(
                claimed, planned
            ).context
        self.assertEqual(planned["reasoning_review_id"], "authoring-review")
        self.assertEqual(
            planned["research_query_assignments"][0]["scope_key"],
            "proposition:p1",
        )
        self.assertEqual(
            planned["research_query_assignments"][0]["subclaims"],
            ["Evidence supports it."],
        )

        polished = self.worker._authoring_polish(
            claimed,
            {
                "plan_id": "plan-1",
                "locked_plan": {},
                "section_contracts": [{"section_id": "s1", "title": "結論"}],
                "draft_sections": [
                    {"section_id": "s1", "text": "証拠により結論は正当化される。"}
                ],
                "proposed_html": '<article id="old"><p id="old-p">旧稿</p></article>',
                "language": "ja",
            },
        ).context
        self.assertIn('id="DOC-plan-1"', polished["proposed_html"])
        self.assertIn("証拠により結論は正当化される。", polished["proposed_html"])
        self.assertEqual(
            polished["pre_polish_html"],
            '<article id="old"><p id="old-p">旧稿</p></article>',
        )

    def test_authoring_bridges_locked_sections_to_v29_structure_contract(self) -> None:
        html = self.worker._authoring_render_sections(
            {
                "plan_id": "plan-1",
                "language": "ja",
                "section_contracts": [{"section_id": "s1", "title": "結論"}],
            },
            [
                {
                    "section_id": "s1",
                    "text": "第一段落。\n\n第二段落。\n\n第三段落。\n\n第四段落。\n\n第五段落。",
                }
            ],
        )
        bridged = self.worker._authoring_v29_plan_bridge(
            {
                "structure_plan": {"id": "plan-1"},
                "structure_plan_validation": {
                    "valid": True,
                    "validator": "authoring-plan-pydantic-v1",
                },
                "section_contracts": [
                    {
                        "section_id": "s1",
                        "title": "結論",
                        "local_question": "なぜか",
                        "expected_conclusion": "正当化される",
                        "claim_ids": ["c1"],
                        "evidence_ids": ["e1"],
                        "warrant_ids": ["w1"],
                        "development_expectations": {
                            "required_argument_moves": [
                                "orientation",
                                "claim",
                                "evidence",
                                "warrant",
                                "qualification",
                            ]
                        },
                    }
                ],
                "argument_nodes": [
                    {
                        "id": "e1",
                        "node_type": "evidence",
                        "claim_id": "c1",
                    },
                    {
                        "id": "w1",
                        "node_type": "warrant",
                        "claim_id": "c1",
                    },
                    {
                        "id": "q1",
                        "node_type": "qualifier",
                        "claim_id": "c1",
                    },
                ],
                "draft_sections": [
                    {
                        "section_id": "s1",
                        "text": "本文",
                        "paragraph_moves_realized": [
                            "orientation",
                            "claim",
                            "evidence",
                            "warrant",
                            "qualification",
                        ],
                    }
                ],
            },
            html,
        )
        self.assertTrue(bridged["structure_plan_validation"]["valid"])
        self.assertEqual(
            bridged["structure_plan"]["sections"][0]["claim_ids"], ["c1"]
        )
        self.assertEqual(len(bridged["paragraph_plan"]["paragraphs"]), 5)
        self.assertEqual(len(bridged["realized_paragraphs"]), 5)
        self.assertTrue(bridged["paragraph_plan_validation"]["valid"])
        self.assertTrue(bridged["paragraph_plan_validation"]["global_valid"])
        self.assertEqual(
            bridged["paragraph_plan"]["paragraphs"][0][
                "required_argument_move_ids"
            ],
            ["orientation"],
        )
        self.assertEqual(
            bridged["source_lineage_policy"], "authoring-whole-document-v1"
        )
        self.assertTrue(bridged["paragraph_boundary_lock"]["structural_sequence"])
        metadata = self.worker._authoring_persistence_metadata(
            {**bridged, "draft_sections": [{"section_id": "s1"}]}
        )
        self.assertEqual(metadata["rewrite_batch_count"], 1)
        self.assertEqual(
            metadata["rewrite_summary"],
            ["Rendered 1 locked authoring section contracts."],
        )

    def test_authoring_bridge_excludes_generated_terminal_sections(self) -> None:
        html = self.worker._authoring_render_sections(
            {
                "plan_id": "plan-1",
                "language": "ja",
                "section_contracts": [{"section_id": "s1", "title": "本文"}],
            },
            [{"section_id": "s1", "text": "本文段落。"}],
        )
        html = html.replace(
            "</article>",
            (
                '<section id="N" data-citation-section="evidence-footnotes-v1">'
                '<h2 id="NH">注</h2><ol id="NL"></ol></section>'
                '<section id="R" data-reference-section="consulted-generated-v22">'
                '<h2 id="RH">参考文献</h2></section></article>'
            ),
        )

        bridged = self.worker._authoring_v29_plan_bridge(
            {
                "structure_plan": {"id": "plan-1"},
                "structure_plan_validation": {"valid": True},
                "section_contracts": [
                    {
                        "section_id": "s1",
                        "title": "本文",
                        "claim_ids": ["c1"],
                        "development_expectations": {
                            "required_argument_moves": ["claim"]
                        },
                    }
                ],
                "argument_nodes": [],
                "draft_sections": [
                    {
                        "section_id": "s1",
                        "paragraph_moves_realized": ["claim"],
                    }
                ],
            },
            html,
        )

        self.assertTrue(bridged["structure_plan_validation"]["valid"])
        self.assertEqual(bridged["structure_plan_validation"]["rendered_section_count"], 1)
        self.assertEqual(len(bridged["structure_plan"]["sections"]), 1)

    def test_authoring_bridge_maps_multiple_claims_to_synthesis_paragraph(self) -> None:
        moves = [
            "orientation",
            "claim",
            "evidence",
            "warrant",
            "qualification",
            "synthesis",
        ]
        claim_ids = [f"c{index}" for index in range(1, 8)]
        html = self.worker._authoring_render_sections(
            {
                "plan_id": "plan-1",
                "language": "ja",
                "section_contracts": [{"section_id": "s1", "title": "統合"}],
            },
            [
                {
                    "section_id": "s1",
                    "text": "\n\n".join(f"第{index}段落。" for index in range(1, 7)),
                }
            ],
        )
        bridged = self.worker._authoring_v29_plan_bridge(
            {
                "structure_plan": {"id": "plan-1"},
                "structure_plan_validation": {"valid": True},
                "section_contracts": [
                    {
                        "section_id": "s1",
                        "title": "統合",
                        "claim_ids": claim_ids,
                        "development_expectations": {
                            "required_argument_moves": moves
                        },
                    }
                ],
                "argument_nodes": [],
                "draft_sections": [
                    {
                        "section_id": "s1",
                        "paragraph_moves_realized": moves,
                    }
                ],
            },
            html,
        )

        paragraphs = bridged["paragraph_plan"]["paragraphs"]
        mapped_claims = [
            claim_id for paragraph in paragraphs for claim_id in paragraph["claim_ids"]
        ]
        self.assertTrue(bridged["paragraph_plan_validation"]["valid"])
        self.assertEqual(mapped_claims, claim_ids)
        self.assertEqual(paragraphs[-1]["claim_ids"], ["c6", "c7"])

    def test_authoring_bridge_includes_late_bound_claim_citations(self) -> None:
        moves = ["orientation", "claim", "evidence", "warrant", "qualification"]
        draft_paragraphs = [
            {
                "move_indices": [1, 2],
                "evidence_ids": [],
                "text": "問いを置き、主張を示す。",
            },
            {
                "move_indices": [2, 3, 4],
                "evidence_ids": ["annotation-citation-node:r1"],
                "text": (
                    "ラマール『アニメ・マシーン』の媒体論を参照して、"
                    "主張を根拠で支え、その理由づけを説明する。"
                ),
            },
            {
                "move_indices": [4, 5],
                "evidence_ids": [],
                "text": "理由づけの限界を示す。",
            },
        ]
        html = self.worker._authoring_render_sections(
            {
                "plan_id": "plan-1",
                "language": "ja",
                "section_contracts": [{"section_id": "s1", "title": "検討"}],
            },
            [
                {
                    "section_id": "s1",
                    "text": "\n\n".join(item["text"] for item in draft_paragraphs),
                }
            ],
        )
        citation_id = "annotation-citation:r1"

        bridge_context = {
                "structure_plan": {"id": "plan-1"},
                "structure_plan_validation": {"valid": True},
                "section_contracts": [
                    {
                        "section_id": "s1",
                        "title": "検討",
                        "claim_ids": ["c1"],
                        "evidence_ids": [],
                        "development_expectations": {
                            "required_argument_moves": moves
                        },
                    }
                ],
                "argument_nodes": [
                    {
                        "id": "annotation-citation-node:r1",
                        "node_type": "evidence",
                        "claim_id": "c1",
                        "source_span_id": citation_id,
                        "provenance": {"source_ids": [citation_id]},
                    }
                ],
                "evidence_context": [{
                    "id": citation_id,
                    "allowed_use": "attributed_scholarship_citation",
                    "authors": ["トーマス・ラマール"],
                    "title": "アニメ・マシーン",
                }],
                "draft_sections": [
                    {
                        "section_id": "s1",
                        "paragraphs": draft_paragraphs,
                        "paragraph_moves_realized": moves,
                        "evidence_treatment": [{
                            "evidence_id": "annotation-citation-node:r1",
                            "prose_treatment": "attributed_scholarship",
                        }],
                    }
                ],
            }
        bridged = self.worker._authoring_v29_plan_bridge(
            bridge_context,
            html,
        )

        self.assertIn(
            "annotation-citation-node:r1",
            bridged["structure_plan"]["sections"][0][
                "argument_evidence_node_ids"
            ],
        )
        self.assertTrue(
            any(
                citation_id in edit.get("evidence_ids", [])
                for edit in bridged["applied_edits"]
            )
        )
        self.assertEqual(
            bridged["authoring_paragraph_citation_lineage"],
            [
                {
                    "paragraph_id": "s1:p2",
                    "section_id": "semantic:H-plan-1-s1",
                    "authoring_section_id": "s1",
                    "output_block_id": "P-plan-1-s1-002",
                    "claim_ids": ["c1"],
                    "argument_evidence_node_ids": [
                        "annotation-citation-node:r1"
                    ],
                    "evidence_ids": [citation_id],
                }
            ],
        )
        self.assertEqual(
            [
                item["required_argument_move_ids"]
                for item in bridged["realized_paragraphs"]
            ],
            [
                ["orientation", "claim"],
                ["claim", "evidence", "warrant"],
                ["warrant", "qualification"],
            ],
        )
        self.assertTrue(bridged["paragraph_plan_validation"]["valid"])

        polished_bridge = self.worker._authoring_v29_plan_bridge(
            {
                **bridge_context,
                **bridged,
                "draft_sections": [
                    {
                        "section_id": "s1",
                        "paragraph_moves_realized": moves,
                    }
                ],
            },
            html,
        )
        self.assertEqual(
            polished_bridge["authoring_paragraph_citation_lineage"],
            bridged["authoring_paragraph_citation_lineage"],
        )

        without_explicit_use = {
            **bridge_context,
            "draft_sections": [
                {
                    "section_id": "s1",
                    "paragraphs": [
                        {**paragraph, "evidence_ids": []}
                        for paragraph in draft_paragraphs
                    ],
                    "paragraph_moves_realized": moves,
                }
            ],
        }
        bridged_without_citation = self.worker._authoring_v29_plan_bridge(
            without_explicit_use,
            html,
        )
        self.assertNotIn(
            "annotation-citation-node:r1",
            bridged_without_citation["structure_plan"]["sections"][0][
                "argument_evidence_node_ids"
            ],
        )
        self.assertFalse(
            any(
                citation_id in edit.get("evidence_ids", [])
                for edit in bridged_without_citation["applied_edits"]
            )
        )

    def test_authoring_accepts_prompt_plan_section_ids_at_draft_handoff(self) -> None:
        normalized = self.worker._authoring_normalize_generated_section_ids(
            {
                "plan_id": "plan-1",
                "section_contracts": [
                    {"section_id": "intro"},
                    {"section_id": "conclusion"},
                ],
            },
            [
                {"section_id": "plan-1@1:intro", "text": "導入"},
                {"section_id": "conclusion", "text": "結論"},
            ],
        )
        self.assertEqual(
            [item["section_id"] for item in normalized], ["intro", "conclusion"]
        )
        revision_two = self.worker._authoring_normalize_generated_section_ids(
            {
                "plan_id": "plan-1",
                "plan_revision": 2,
                "section_contracts": [{"section_id": "intro"}],
            },
            [{"section_id": "plan-1@2:intro", "text": "導入"}],
        )
        self.assertEqual("intro", revision_two[0]["section_id"])
        repaired = self.worker._authoring_normalize_generated_section_ids(
            {
                "plan_id": "plan-1",
                "section_contracts": [
                    {"section_id": "intro"},
                    {"section_id": "conclusion"},
                ],
            },
                [{"section_id": "plan-1@1:conclusion", "text": "修正版"}],
            require_full=False,
        )
        self.assertEqual(repaired, [{"section_id": "conclusion", "text": "修正版"}])

    def test_authoring_targeted_batches_restore_locked_section_order(self) -> None:
        contracts = [
            {"section_id": f"s{index}"}
            for index in range(1, 8)
        ]
        batches = self.worker._authoring_section_batches(contracts, batch_size=3)
        self.assertEqual([len(batch) for batch in batches], [3, 3, 1])
        results = [
            {
                "sections": [
                    {"section_id": item["section_id"], "text": item["section_id"]}
                    for item in reversed(batch)
                ]
            }
            for batch in batches
        ]
        merged = self.worker._authoring_merge_targeted_sections(
            {"plan_id": "plan-1", "section_contracts": contracts},
            batches,
            results,
        )
        self.assertEqual(
            [item["section_id"] for item in merged],
            [item["section_id"] for item in contracts],
        )

    def test_authoring_draft_projection_omits_unrelated_plan_state(self) -> None:
        projected = self.worker._authoring_draft_plan_projection(
            {
                "research_question": {"text": "why"},
                "propositions": [{"id": "p1", "text": "large source"}],
                "claims": [
                    {"id": "root", "parent_id": None},
                    {"id": "c1", "parent_id": "root"},
                    {"id": "c2", "parent_id": "root"},
                ],
                "argument_nodes": [
                    {"id": "e1", "claim_id": "c1"},
                    {"id": "e2", "claim_id": "c2"},
                ],
                "argument_edges": [],
                "discourse_nodes": [
                    {"id": "d0", "parent_id": None, "section_id": None},
                    {"id": "d1", "parent_id": "d0", "section_id": "s1"},
                    {"id": "d2", "parent_id": "d0", "section_id": "s2"},
                ],
            },
            [{"section_id": "s1", "claim_ids": ["c1"]}],
        )

        self.assertEqual([item["id"] for item in projected["claims"]], ["root", "c1"])
        self.assertEqual([item["id"] for item in projected["argument_nodes"]], ["e1"])
        self.assertEqual(
            [item["id"] for item in projected["discourse_nodes"]], ["d0", "d1"]
        )
        self.assertNotIn("propositions", projected)

    def test_authoring_normalizes_prompt_plan_ids_inside_draft_metadata(self) -> None:
        [section] = self.worker._authoring_normalize_generated_section_ids(
            {
                "plan_id": "plan-1",
                "plan_revision": 1,
                "section_contracts": [{"section_id": "s1"}],
            },
            [
                {
                    "section_id": "plan-1@1:s1",
                    "claim_ids": ["plan-1@1:c1"],
                    "realized_evidence_ids": ["plan-1@1:e1"],
                    "realized_warrant_ids": ["plan-1@1:w1"],
                    "realized_qualifier_ids": ["plan-1@1:q1"],
                    "evidence_treatment": [
                        {
                            "evidence_id": "plan-1@1:e1",
                            "claim_id": "plan-1@1:c1",
                            "qualifier_id": "plan-1@1:q1",
                        }
                    ],
                }
            ],
        )

        self.assertEqual(section["claim_ids"], ["c1"])
        self.assertEqual(section["realized_evidence_ids"], ["e1"])
        self.assertEqual(section["evidence_treatment"][0]["qualifier_id"], "q1")

    def test_authoring_removes_schema_markers_from_visible_japanese_prose(self) -> None:
        text = (
            "この推論を本文で展開する（c10-e、c11-w）。"
            "結論は memo_close_reading と close reading に基づく。"
            "『第一の著作』『第二の著作』を比較する。"
        )

        sanitized = self.worker._authoring_sanitize_visible_prose(
            text, language="ja"
        )

        self.assertEqual(
            sanitized,
            "この推論を本文で展開する。結論はメモの精読と精読に基づく。"
            "『第一の著作』・『第二の著作』を比較する。",
        )
        self.assertNotIn("c10-e", sanitized)
        self.assertNotIn("close reading", sanitized.casefold())
        self.assertEqual(
            self.worker._authoring_sanitize_visible_prose(
                'ランシエールの<a href="https://example.org/book">『著作』</a>を参照する。',
                language="ja",
            ),
            'ランシエールの『著作』を参照する。',
        )
        self.assertEqual(
            self.worker._authoring_sanitize_visible_prose(
                'ウィリアムズ[『マルクス主義と文学』](https://example.org/book)を参照する。',
                language="ja",
            ),
            'ウィリアムズ『マルクス主義と文学』を参照する。',
        )

    def test_authoring_detects_reader_facing_input_scaffolding(self) -> None:
        findings = self.worker._authoring_reader_facing_scaffold_findings(
            [
                {"section_id": "sec-1", "text": "提示メモはこの点を示している。"},
                {"section_id": "sec-2", "text": "本稿ではこの点を仮説として扱う。"},
                {"section_id": "sec-3", "text": "著者メモがこの作品を挙げる。"},
                {"section_id": "sec-4", "text": "提示された命題から答えが導かれる。"},
            ]
        )

        self.assertEqual(len(findings), 3)
        self.assertEqual(findings[0]["object_id"], "sec-1")
        self.assertEqual([item["object_id"] for item in findings], ["sec-1", "sec-3", "sec-4"])
        self.assertEqual(findings[0]["severity"], "major")
        self.assertEqual(findings[0]["repair_scope"], "local_section")

    def test_authoring_detects_reader_facing_verification_workflow_phrases(self) -> None:
        findings = self.worker._authoring_reader_facing_scaffold_findings(
            [
                {"section_id": "sec-1", "text": "作品一覧の採否を照合する。"},
                {"section_id": "sec-2", "text": "発言原文との照合を待つ。"},
                {"section_id": "sec-3", "text": "この役割は元の発言では未照合である。"},
                {"section_id": "sec-4", "text": "挙げられた作品の採否には差がある。"},
                {"section_id": "sec-5", "text": "該当画面は未照合である。"},
            ]
        )

        self.assertEqual(
            [item["object_id"] for item in findings],
            ["sec-1", "sec-2", "sec-3", "sec-4", "sec-5"],
        )
        self.assertTrue(all(item["severity"] == "major" for item in findings))

    def test_authoring_leaves_context_dependent_scope_phrases_for_semantic_audit(self) -> None:
        findings = self.worker._authoring_reader_facing_meta_prose_findings(
            [
                {
                    "section_id": "sec-1",
                    "text": "ここで扱えるのは、共通する型の記述である。",
                },
                {
                    "section_id": "sec-2",
                    "text": "ここで論じられるのは、資料が示す配置である。",
                },
                {
                    "section_id": "sec-3",
                    "text": "比較の枠組みの有効範囲",
                },
            ]
        )

        self.assertEqual(findings, [])

    def test_authoring_projects_workflow_terms_without_rewriting_locked_meaning(
        self,
    ) -> None:
        source = {
            "research_problem": {
                "research_question": "作業概念の適用範囲は何か。",
                "provisional_answer": "比較仮説の有効範囲は資料によって異なる。",
                "description": "メモが挙げる事例は未照合である。",
                "provenance": {"source_label": "メモ", "source_ids": ["memo-1"]},
            },
            "section_contracts": [
                {
                    "section_id": "section-11",
                    "title": "比較仮説の有効範囲",
                    "section_answer": "メモから導けるのは比較仮説である。",
                    "required_terms": ["媒体", "比較仮説", "生ログ", "草稿", "メモ"],
                },
                {
                    "section_id": "section-12",
                    "title": "媒体レギュレーションという作業概念",
                    "section_output": "場ごとの慣行を比較するための作業概念である。",
                    "development_expectations": {
                        "required_argument_moves": [
                            "作業概念を定義する",
                            "概念の適用限界を示す",
                        ]
                    },
                },
            ],
            "semantic_paragraph_plan": [
                {
                    "section_id": "section-11",
                    "paragraphs": [
                        {
                            "development_instruction": (
                                "現段階で示せるのは比較仮説であり、"
                                "生ログと草稿は未照合である。"
                            )
                        }
                    ],
                }
            ],
            "claims": [{"id": "claim-1", "text": "メモという語を含む原資料"}],
        }

        normalized, corrections = (
            self.worker._authoring_normalize_reader_facing_contract_language(source)
        )

        self.assertEqual(
            source["section_contracts"][0]["title"], "比較仮説の有効範囲"
        )
        self.assertEqual(
            normalized["section_contracts"][0]["title"], "比較仮説の有効範囲"
        )
        self.assertEqual(
            normalized["section_contracts"][0]["required_terms"],
            ["媒体", "比較仮説"],
        )
        self.assertEqual(
            normalized["section_contracts"][1]["title"],
            "媒体レギュレーションという作業概念",
        )
        self.assertEqual(normalized["research_problem"], source["research_problem"])
        self.assertEqual(
            normalized["semantic_paragraph_plan"], source["semantic_paragraph_plan"]
        )
        self.assertEqual(source["section_contracts"][0]["required_terms"],
                         ["媒体", "比較仮説", "生ログ", "草稿", "メモ"])
        self.assertEqual(
            normalized["research_problem"]["provenance"],
            {"source_label": "メモ", "source_ids": ["memo-1"]},
        )
        self.assertEqual(normalized["claims"], source["claims"])
        self.assertTrue(
            any(item["action"] == "drop_workflow_required_term" for item in corrections)
        )
        self.assertTrue(all(item["action"] == "drop_workflow_required_term"
                            for item in corrections))

    def test_authoring_audit_preserves_every_affected_section_id(self) -> None:
        expanded = self.worker._authoring_expand_llm_audit_finding(
            {
                "finding_type": "citation_mismatch",
                "severity": "major",
                "message": "複数節で引用位置が一致しない。",
                "affected_section_ids": ["section-01", "section-02", "section-06"],
                "repair_scope": "local_section",
            }
        )

        self.assertEqual(
            [item["object_id"] for item in expanded],
            ["section-01", "section-02", "section-06"],
        )
        self.assertTrue(all(item["object_type"] == "section" for item in expanded))

    def test_authoring_audit_localizes_conclusion_only_findings(self) -> None:
        expanded = self.worker._authoring_expand_llm_audit_finding(
            {
                "finding_type": "conclusion_synthesis_gap",
                "severity": "major",
                "message": "本文の複数節を結論で統合する必要がある。",
                "affected_section_ids": [
                    "section-02",
                    "section-06",
                    "section-conclusion",
                ],
                "repair_scope": "local_section",
            }
        )

        self.assertEqual(
            [item["object_id"] for item in expanded], ["section-conclusion"]
        )

    def test_authoring_v31_section_plan_revision_stays_local_and_refreshes_conclusion(
        self,
    ) -> None:
        contracts = [
            {"section_id": "section-introduction", "article_role": "introduction"},
            {"section_id": "section-01", "article_role": "body"},
            {"section_id": "section-02", "article_role": "body"},
            {"section_id": "section-conclusion", "article_role": "conclusion"},
        ]
        affected = self.worker._authoring_v31_repair_section_ids(
            contracts,
            [
                {
                    "severity": "major",
                    "object_id": "section-01",
                    "repair_scope": "plan_revision",
                }
            ],
        )

        self.assertEqual(affected, {"section-01", "section-conclusion"})

    def test_authoring_v31_document_plan_revision_targets_every_section(self) -> None:
        contracts = [
            {"section_id": "section-introduction", "article_role": "introduction"},
            {"section_id": "section-01", "article_role": "body"},
            {"section_id": "section-conclusion", "article_role": "conclusion"},
        ]
        affected = self.worker._authoring_v31_repair_section_ids(
            contracts,
            [
                {
                    "severity": "major",
                    "object_type": "plan",
                    "object_id": "plan-1",
                    "repair_scope": "plan_revision",
                }
            ],
        )

        self.assertEqual(affected, {item["section_id"] for item in contracts})

    def test_authoring_v31_blocking_repair_also_resolves_local_minor_findings(self) -> None:
        contracts = [
            {"section_id": "section-introduction", "article_role": "introduction"},
            {"section_id": "section-01", "article_role": "body"},
            {"section_id": "section-conclusion", "article_role": "conclusion"},
        ]
        affected = self.worker._authoring_v31_repair_section_ids(
            contracts,
            [
                {
                    "severity": "major",
                    "object_id": "section-01",
                    "repair_scope": "local_section",
                },
                {
                    "severity": "minor",
                    "object_id": "section-introduction",
                    "repair_scope": "local_section",
                },
            ],
        )

        self.assertEqual(
            affected,
            {"section-introduction", "section-01", "section-conclusion"},
        )

    def test_authoring_v31_section_plan_revision_finding_stays_with_target(self) -> None:
        findings = [
            {
                "severity": "major",
                "object_id": "section-01",
                "repair_scope": "plan_revision",
                "message": "引用位置を全節で点検する。",
            },
            {
                "severity": "minor",
                "object_id": "section-02",
                "repair_scope": "local_section",
                "message": "第二節だけを直す。",
            },
        ]

        self.assertEqual(
            self.worker._authoring_v31_findings_for_section(findings, "section-03"),
            [],
        )
        self.assertEqual(
            self.worker._authoring_v31_findings_for_section(findings, "section-02"),
            [findings[1]],
        )
        self.assertEqual(
            self.worker._authoring_v31_findings_for_section(findings, "section-01"),
            [findings[0]],
        )

    def test_authoring_v31_repair_checkpoint_name_changes_with_node_attempt(self) -> None:
        first = self.worker._authoring_v31_repair_operation_name(
            node_attempt=1,
            role="conclusion",
            section_id="section-conclusion",
            contract_retry=2,
        )
        second = self.worker._authoring_v31_repair_operation_name(
            node_attempt=2,
            role="conclusion",
            section_id="section-conclusion",
            contract_retry=2,
        )

        self.assertNotEqual(first, second)
        self.assertIn("attempt_1", first)
        self.assertIn("attempt_2", second)
        self.assertTrue(first.endswith("_contract_retry_2"))

    def test_authoring_promotes_explicit_theory_annotations_to_reference_candidates(
        self,
    ) -> None:
        entries = self.worker._authoring_annotation_reference_entries(
            {
                "propositions": [
                    {
                        "id": "p1",
                        "memo_annotations": [
                            {
                                "raw": (
                                    "(理: ブルデュー『芸術の規則』、"
                                    "ランシエール『感性的なもののパルタージュ』)"
                                ),
                                "detail": (
                                    "ブルデュー『芸術の規則』、"
                                    "ランシエール『感性的なもののパルタージュ』"
                                ),
                                "kinds": ["added_theory"],
                            }
                        ],
                    },
                    {
                        "id": "p2",
                        "memo_annotations": [
                            {
                                "raw": "(理: ラマール『アニメ・マシーン』『アニメ・エコロジー』)",
                                "detail": "ラマール『アニメ・マシーン』『アニメ・エコロジー』",
                                "kinds": ["added_theory"],
                            }
                        ],
                    },
                ]
            }
        )

        self.assertEqual(
            {(item["author"], item["title"]) for item in entries},
            {
                ("ブルデュー", "芸術の規則"),
                ("ランシエール", "感性的なもののパルタージュ"),
                ("ラマール", "アニメ・マシーン"),
                ("ラマール", "アニメ・エコロジー"),
            },
        )
        self.assertTrue(all(item["consulted_status"] == "source_declared" for item in entries))
        self.assertTrue(all(item["annotation_proposition_ids"] for item in entries))

    def test_authoring_self_citation_is_parsed_and_searched_by_title(self) -> None:
        entries = self.worker._authoring_annotation_reference_entries(
            {
                "propositions": [
                    {
                        "id": "p-self",
                        "memo_annotations": [
                            {
                                "raw": "(自: 『架空資料の比較分析』)",
                                "detail": "『架空資料の比較分析』",
                                "kinds": ["self_citation"],
                            }
                        ],
                    }
                ]
            }
        )

        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["author"], "自著")
        self.assertTrue(entry["is_self_authored"])
        self.assertEqual(entry["parse_status"], "parsed")
        self.assertEqual(entry["unparsed_fields"], [])
        self.assertEqual(
            JapaneseReferenceService.search_query(entry),
            "架空資料の比較分析",
        )

    def test_authoring_merges_annotation_lineage_into_existing_reference(self) -> None:
        existing = {
            "reference_id": "source-reference",
            "author": "山本和明",
            "title": "架空資料の比較分析",
            "parse_status": "parsed",
            "reference_role": "consulted_or_cited",
        }
        annotation = {
            "reference_id": "annotation-reference:self",
            "author": "自著",
            "title": "架空資料の比較分析",
            "parse_status": "parsed",
            "is_self_authored": True,
            "annotation_kinds": ["self_citation"],
            "annotation_raws": ["(自: 『架空資料の比較分析』)"],
            "annotation_proposition_ids": ["p-self"],
        }

        merged = self.worker._merge_authoring_annotation_reference_entries(
            [existing], [annotation]
        )

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["reference_id"], "source-reference")
        self.assertEqual(merged[0]["author"], "山本和明")
        self.assertTrue(merged[0]["is_self_authored"])
        self.assertEqual(merged[0]["annotation_kinds"], ["self_citation"])
        self.assertEqual(merged[0]["annotation_proposition_ids"], ["p-self"])

    def test_authoring_bridges_only_render_ready_annotation_references_to_footnotes(
        self,
    ) -> None:
        context = {
            "claims": [
                {
                    "id": "c1",
                    "proposition_id": "p1",
                    "provenance": {"source_ids": ["p1"]},
                }
            ],
            "propositions": [{"id": "p1", "text": "理論的命題"}],
            "classifications": [{"proposition_id": "p1", "type": "interpretation"}],
        }
        ready = {
            "reference_id": "annotation-reference:ready",
            "reference_role": "author_supplied_citation_candidate",
            "author": "著者",
            "title": "著作",
            "original_text": "著者『著作』",
            "v29_render_status": "render_ready",
            "final_revision_blocked": False,
            "annotation_kinds": ["added_theory"],
            "annotation_proposition_ids": ["p1"],
        }
        blocked = {
            **ready,
            "reference_id": "annotation-reference:blocked",
            "title": "未確定著作",
            "v29_render_status": "human_review_required",
            "final_revision_blocked": True,
        }

        entries, evidence = self.worker._authoring_annotation_citation_evidence(
            context, [ready, blocked]
        )

        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["verification_status"], "unverified")
        self.assertEqual(evidence[0]["verification_scope"], "bibliographic_identity_only")
        self.assertEqual(evidence[0]["claim_ids"], ["c1"])
        self.assertEqual(entries[0]["evidence_ids"], [evidence[0]["id"]])
        self.assertNotIn("evidence_ids", entries[1])

        payload = self.worker._authoring_argument_payload(
            {
                **context,
                "reference_entries": entries,
                "evidence_context": evidence,
            }
        )
        self.assertEqual(payload["citation_references"][0]["evidence_ids"], [evidence[0]["id"]])
        self.assertEqual(
            payload["evidence"]["accepted_evidence"][0]["allowed_use"],
            "attributed_scholarship_citation",
        )

    def test_authoring_attaches_annotation_citations_to_argument_claims_once(
        self,
    ) -> None:
        context = {
            "claims": [
                {
                    "id": "c1",
                    "proposition_id": "p2",
                    "provenance": {"source_ids": ["p2", "p3"]},
                }
            ],
            "argument_nodes": [
                {
                    "id": "claim-node",
                    "node_type": "claim",
                    "claim_id": "c1",
                    "text": "主張",
                }
            ],
            "argument_edges": [],
            "annotation_citation_evidence": [
                {
                    "id": "annotation-citation:r1",
                    "authors": ["著者"],
                    "title": "著作",
                    "claim_ids": ["c1"],
                    "annotation_proposition_ids": ["p1", "p2"],
                }
            ],
        }

        attached = self.worker._authoring_attach_annotation_citations(context)
        evidence_nodes = [
            item
            for item in attached["argument_nodes"]
            if item.get("node_type") == "evidence"
        ]
        self.assertEqual(len(evidence_nodes), 1)
        self.assertEqual(evidence_nodes[0]["source_span_id"], "annotation-citation:r1")
        self.assertEqual(evidence_nodes[0]["proposition_id"], "p2")
        self.assertEqual(
            evidence_nodes[0]["provenance"]["verification_status"], "unverified"
        )
        self.assertEqual(
            evidence_nodes[0]["provenance"]["source_ids"],
            ["annotation-citation:r1"],
        )
        self.assertEqual(len(attached["argument_edges"]), 1)
        self.assertEqual(attached["argument_edges"][0]["relation"], "supports")
        self.assertEqual(
            attached["annotation_citation_argument_bridge"][
                "substantive_support_verified"
            ],
            False,
        )

        repeated = self.worker._authoring_attach_annotation_citations(attached)
        self.assertEqual(repeated, attached)

        legacy = {
            **attached,
            "argument_nodes": [
                {
                    **item,
                    **(
                        {"proposition_id": "p1"}
                        if item.get("node_type") == "evidence"
                        else {}
                    ),
                }
                for item in attached["argument_nodes"]
            ],
        }
        realigned = self.worker._authoring_attach_annotation_citations(legacy)
        realigned_evidence = next(
            item
            for item in realigned["argument_nodes"]
            if item.get("node_type") == "evidence"
        )
        self.assertEqual(realigned_evidence["proposition_id"], "p2")
        self.assertEqual(
            realigned["annotation_citation_argument_bridge"][
                "realigned_claim_proposition_count"
            ],
            1,
        )

        excluded = self.worker._authoring_attach_annotation_citations(
            {
                **context,
                "invalid_aggregated_self_citation_normalizations": [
                    {"removed_node_ids": [evidence_nodes[0]["id"]]}
                ],
            }
        )
        self.assertEqual(excluded["argument_nodes"], context["argument_nodes"])
        self.assertEqual(excluded["argument_edges"], [])
        self.assertNotIn("annotation_citation_argument_bridge", excluded)

    def test_authoring_citation_contracts_preserve_narrow_proposition_links(
        self,
    ) -> None:
        citation_ids = ["annotation-citation:r2", "annotation-citation:r3"]
        context = {
            "research_problem": {"id": "problem-1"},
            "claims": [{"id": "c1", "proposition_id": "p1"}],
            "argument_nodes": [
                {
                    "id": "claim-node",
                    "node_type": "claim",
                    "claim_id": "c1",
                    "proposition_id": "p1",
                    "provenance": {"source_ids": ["p1"]},
                }
            ],
            "argument_edges": [],
            "annotation_citation_evidence": [
                {
                    "id": citation_ids[0],
                    "allowed_use": "attributed_scholarship_citation",
                    "authors": ["筆者A"],
                    "title": "研究A",
                    "reference_id": "reference:r2",
                    "annotation_proposition_ids": ["p2"],
                },
                {
                    "id": citation_ids[1],
                    "allowed_use": "attributed_scholarship_citation",
                    "authors": ["筆者B"],
                    "title": "研究B",
                    "reference_id": "reference:r3",
                    "annotation_proposition_ids": ["p3"],
                },
            ],
            "evidence_context": [
                {
                    "id": citation_ids[0],
                    "allowed_use": "attributed_scholarship_citation",
                    "reference_id": "reference:r2",
                },
                {
                    "id": citation_ids[1],
                    "allowed_use": "attributed_scholarship_citation",
                    "reference_id": "reference:r3",
                },
            ],
            "reference_entries": [
                {"reference_id": "reference:r2"},
                {"reference_id": "reference:r3"},
            ],
            "authoring_atomic_evidence_contracts": [
                {
                    "realization": "attribute_theory_with_citation",
                    "proposition_id": proposition_id,
                    "claim_ids": ["c1"],
                    "citation_evidence_ids": [citation_id],
                }
                for proposition_id, citation_id in zip(
                    ["p2", "p3"], citation_ids, strict=True
                )
            ],
        }

        attached = self.worker._authoring_attach_annotation_citations(context)
        citation_nodes = [
            item
            for item in attached["argument_nodes"]
            if item.get("id", "").startswith("annotation-citation-node:")
        ]

        self.assertEqual(
            {item["source_span_id"]: item["proposition_id"] for item in citation_nodes},
            dict(zip(citation_ids, ["p2", "p3"], strict=True)),
        )
        self.assertEqual(
            len(
                [
                    edge
                    for edge in attached["argument_edges"]
                    if edge["relation"] == "supports"
                ]
            ),
            2,
        )
        self.assertEqual(
            attached["annotation_citation_argument_bridge"][
                "attached_claim_evidence_count"
            ],
            2,
        )
        projected = self.worker._authoring_draft_contracts_with_scholarship(
            attached,
            [
                {
                    "section_id": "sec-body",
                    "article_role": "body",
                    "claim_ids": ["c1"],
                    "evidence_ids": [],
                }
            ],
        )
        self.assertEqual(
            set(projected[0]["required_scholarship_evidence_ids"]),
            {item["id"] for item in citation_nodes},
        )

        mismatched = {
            **context,
            "annotation_citation_evidence": [
                {**item, "annotation_proposition_ids": ["unmatched-proposition"]}
                for item in context["annotation_citation_evidence"]
            ],
        }
        not_attached = self.worker._authoring_attach_annotation_citations(
            mismatched
        )
        self.assertEqual(not_attached, mismatched)

    def test_v31_draft_replay_rebuilds_explicit_citation_bridge_before_projection(
        self,
    ) -> None:
        citation_id = "annotation-citation:source-a"
        citation = {
            "id": citation_id,
            "allowed_use": "attributed_scholarship_citation",
            "authors": ["筆者A"],
            "title": "研究A",
            "reference_id": "reference:r1",
            "claim_ids": [],
            "annotation_proposition_ids": ["p1"],
        }
        context = {
            "research_problem": {"research_question": "RQ"},
            "section_contracts": [],
            "claims": [{"id": "c1", "proposition_id": "p1"}],
            "argument_nodes": [
                {
                    "id": "claim-node-c1",
                    "node_type": "claim",
                    "claim_id": "c1",
                    "proposition_id": "p1",
                }
            ],
            "argument_edges": [],
            "annotation_citation_evidence": [citation],
            "evidence_context": [citation],
            "authoring_atomic_evidence_contracts": [
                {
                    "realization": "attribute_theory_with_citation",
                    "proposition_id": "p1",
                    "claim_ids": ["c1"],
                    "citation_evidence_ids": [citation_id],
                }
            ],
        }
        observed: dict[str, object] = {}

        class DraftProjectionReached(Exception):
            pass

        def stop_at_contract_projection(
            draft_context: dict[str, object], _contracts: list[dict[str, object]]
        ) -> list[dict[str, object]]:
            observed["context"] = draft_context
            raise DraftProjectionReached

        with (
            patch.object(self.worker, "_register_artifact", return_value=None),
            patch.object(
                self.worker,
                "_authoring_reconcile_theory_citation_titles",
                side_effect=lambda value, **_kwargs: value,
            ),
            patch.object(
                self.worker,
                "_authoring_drop_unusable_aggregated_self_citation_support",
                side_effect=lambda value: (value, []),
            ),
            patch.object(
                self.worker,
                "_authoring_normalize_reader_facing_contract_language",
                side_effect=lambda value: (value, []),
            ),
            patch.object(
                self.worker,
                "_authoring_draft_contracts_with_scholarship",
                side_effect=stop_at_contract_projection,
            ),
            self.assertRaises(DraftProjectionReached),
        ):
            self.worker._authoring_draft_sections_v31(
                SimpleNamespace(run_id="run-1", workflow_version="31"), context
            )

        replay_context = observed["context"]
        self.assertIsInstance(replay_context, dict)
        citation_nodes = [
            item
            for item in replay_context["argument_nodes"]
            if item.get("source_span_id") == citation_id
        ]
        self.assertEqual(len(citation_nodes), 1)
        citation_node = citation_nodes[0]
        self.assertEqual(citation_node["claim_id"], "c1")
        self.assertEqual(
            citation_node["provenance"]["verification_status"], "unverified"
        )
        self.assertIn(
            {
                "source_id": citation_node["id"],
                "target_id": "claim-node-c1",
                "relation": "supports",
            },
            [
                {
                    "source_id": edge["source_id"],
                    "target_id": edge["target_id"],
                    "relation": edge["relation"],
                }
                for edge in replay_context["argument_edges"]
            ],
        )
        self.assertFalse(
            replay_context["annotation_citation_argument_bridge"][
                "substantive_support_verified"
            ]
        )

    def test_v31_citation_without_direct_claim_id_links_only_by_shared_proposition(self) -> None:
        citation_id = "annotation-citation:source-b"
        citation = {
            "id": citation_id,
            "allowed_use": "attributed_scholarship_citation",
            "authors": ["筆者B"],
            "title": "研究B",
            "reference_id": "reference:r2",
            "claim_ids": [],
            "annotation_proposition_ids": ["p2"],
        }
        context = {
            "claims": [
                {
                    "id": "cl2",
                    "proposition_id": "p2",
                    "provenance": {"source_ids": ["p2"]},
                }
            ],
            "argument_nodes": [
                {
                    "id": "claim-node-cl2",
                    "node_type": "claim",
                    "claim_id": "cl2",
                    "proposition_id": "p2",
                }
            ],
            "argument_edges": [],
            "annotation_citation_evidence": [citation],
            "authoring_atomic_evidence_contracts": [],
        }

        linked = self.worker._authoring_attach_annotation_citations(context)

        citation_nodes = [
            item
            for item in linked["argument_nodes"]
            if item.get("source_span_id") == citation_id
        ]
        self.assertEqual(len(citation_nodes), 1)
        self.assertEqual(citation_nodes[0]["claim_id"], "cl2")
        self.assertEqual(
            citation_nodes[0]["provenance"]["verification_status"], "unverified"
        )
        self.assertIn(
            {
                "source_id": citation_nodes[0]["id"],
                "target_id": "claim-node-cl2",
                "relation": "supports",
            },
            [
                {
                    "source_id": edge["source_id"],
                    "target_id": edge["target_id"],
                    "relation": edge["relation"],
                }
                for edge in linked["argument_edges"]
            ],
        )

    def test_v31_draft_replay_canonicalizes_saved_duplicate_section_contracts(self) -> None:
        claimed = ClaimedNode(
            node_run_id="authoring-node",
            run_id="authoring-run",
            node_id="draft_rewrite",
            attempt=4,
            workflow_name="authoring",
            workflow_version="31",
            resource_lane="codex_remote",
            lease_owner="test-worker",
            project_id="project-1",
            inputs={},
        )
        duplicate_body_ids = [
            "sec-method",
            "sec-magazine",
            "sec-gender",
            "sec-discovery",
            "sec-power",
            "sec-labor",
            "sec-life-age",
            "sec-recognition",
        ]
        section_contracts = []
        for section_id, article_role in [
            ("sec-introduction", "introduction"),
            *((section_id, "body") for section_id in duplicate_body_ids),
        ]:
            for move in ("planned_move", "paragraph_move"):
                section_contracts.append(
                    {
                        "section_id": section_id,
                        "article_role": article_role,
                        "title": section_id,
                        "development_expectations": {
                            "required_argument_moves": [move]
                        },
                    }
                )
        section_contracts.extend(
            [
                {
                    "section_id": "sec-ai",
                    "article_role": "body",
                    "title": "sec-ai",
                    "development_expectations": {"required_argument_moves": []},
                },
                {
                    "section_id": "sec-conclusion",
                    "article_role": "conclusion",
                    "title": "sec-conclusion",
                    "development_expectations": {"required_argument_moves": []},
                },
            ]
        )
        context = {"section_contracts": section_contracts}
        observed: dict[str, object] = {}

        class DraftProjectionReached(Exception):
            pass

        def stop_after_contract_normalization(
            draft_context: dict[str, object], contracts: list[dict[str, object]]
        ) -> list[dict[str, object]]:
            observed["context"] = draft_context
            observed["contracts"] = contracts
            raise DraftProjectionReached

        with (
            patch.object(self.worker, "_register_artifact", return_value=None),
            patch.object(
                self.worker,
                "_authoring_reconcile_theory_citation_titles",
                side_effect=lambda value, **_kwargs: value,
            ),
            patch.object(
                self.worker,
                "_authoring_drop_unusable_aggregated_self_citation_support",
                side_effect=lambda value: (value, []),
            ),
            patch.object(
                self.worker,
                "_authoring_normalize_reader_facing_contract_language",
                side_effect=lambda value: (value, []),
            ),
            patch.object(
                self.worker,
                "_authoring_draft_contracts_with_scholarship",
                side_effect=stop_after_contract_normalization,
            ),
            self.assertRaises(DraftProjectionReached),
        ):
            self.worker._authoring_draft_sections_v31(claimed, context)

        normalized_context = observed["context"]
        normalized_contracts = observed["contracts"]
        self.assertEqual(len(section_contracts), 20)
        self.assertEqual(len(normalized_contracts), 11)
        self.assertEqual(
            len({item["section_id"] for item in normalized_contracts}), 11
        )
        self.assertEqual(
            normalized_context["section_contract_identity_reconciliation"][
                "merged_duplicate_section_ids"
            ],
            sorted(["sec-introduction", *duplicate_body_ids]),
        )
        introduction = next(
            item
            for item in normalized_contracts
            if item["section_id"] == "sec-introduction"
        )
        self.assertEqual(
            introduction["development_expectations"]["required_argument_moves"],
            ["planned_move", "paragraph_move"],
        )

    def test_authoring_routes_required_citation_into_body_generation_payload(self) -> None:
        citation_id = "annotation-citation:source-a"
        citation_node_id = "annotation-citation-node:source-a"
        citation = {
            "id": citation_id,
            "allowed_use": "attributed_scholarship_citation",
            "authors": ["筆者A"],
            "title": "研究A",
            "excerpt": "筆者Aの議論",
            "reference_id": "reference-a",
            "annotation_proposition_ids": ["p1"],
            "claim_ids": ["c1"],
        }
        contracts = [
            {
                "section_id": "s-body",
                "article_role": "body",
                "claim_ids": ["c1"],
                "evidence_ids": ["e1"],
            },
            {
                "section_id": "s-conclusion",
                "article_role": "conclusion",
                "claim_ids": ["c1"],
                "evidence_ids": [],
            },
        ]
        context = {
            "research_problem": {"id": "rp1"},
            "section_contracts": contracts,
            "argument_nodes": [
                {
                    "id": citation_node_id,
                    "node_type": "evidence",
                    "claim_id": "c1",
                    "source_span_id": citation_id,
                    "provenance": {"source_ids": [citation_id]},
                },
                {
                    "id": "e1",
                    "node_type": "evidence",
                    "claim_id": "c1",
                    "provenance": {"source_ids": ["p1"]},
                },
            ],
            "annotation_citation_evidence": [citation],
            "evidence_context": [citation],
            "reference_entries": [
                {
                    "reference_id": "reference-a",
                    "author": "筆者A",
                    "title": "研究A",
                }
            ],
            "authoring_atomic_evidence_contracts": [
                {
                    "claim_ids": ["c1"],
                    "realization": "attribute_theory_with_citation",
                    "citation_evidence_ids": [citation_id],
                }
            ],
        }

        projected = self.worker._authoring_draft_contracts_with_scholarship(
            context, contracts
        )
        body_contract, conclusion_contract = projected

        self.assertEqual(contracts[0]["evidence_ids"], ["e1"])
        self.assertEqual(
            body_contract["required_scholarship_evidence_ids"],
            [citation_node_id],
        )
        self.assertEqual(
            body_contract["evidence_ids"], ["e1", citation_node_id]
        )
        self.assertEqual(
            conclusion_contract["required_scholarship_evidence_ids"], []
        )
        self.assertEqual(conclusion_contract["evidence_ids"], [])

        evidence_payload = self.worker._authoring_evidence_projection(
            context, [body_contract]
        )
        projected_sources = {
            str(item.get("id") or "")
            for item in evidence_payload["accepted_evidence"]
        }
        self.assertIn(citation_id, projected_sources)

    def test_authoring_detaches_annotation_citation_outside_argument_claim_scope(
        self,
    ) -> None:
        citation_id = "annotation-citation:r1"
        citation_node_id = "annotation-citation-node:legacy"
        context = {
            "claims": [
                {
                    "id": "c1",
                    "proposition_id": "p1",
                    "provenance": {"source_ids": ["p1", "p2"]},
                }
            ],
            "argument_nodes": [
                {
                    "id": "claim-node",
                    "node_type": "claim",
                    "claim_id": "c1",
                    "proposition_id": "p1",
                    "provenance": {"source_ids": ["p1"]},
                },
                {
                    "id": citation_node_id,
                    "node_type": "evidence",
                    "claim_id": "c1",
                    "proposition_id": "p2",
                    "source_span_id": citation_id,
                    "provenance": {"source_ids": [citation_id]},
                },
            ],
            "argument_edges": [
                {
                    "id": "citation-edge",
                    "source_id": citation_node_id,
                    "target_id": "claim-node",
                    "relation": "supports",
                }
            ],
            "annotation_citation_evidence": [
                {
                    "id": citation_id,
                    "authors": ["著者"],
                    "title": "理論書",
                    "claim_ids": ["c1"],
                    "annotation_proposition_ids": ["p2"],
                }
            ],
        }

        attached = self.worker._authoring_attach_annotation_citations(context)

        self.assertEqual(
            [item["id"] for item in attached["argument_nodes"]],
            ["claim-node"],
        )
        self.assertEqual(attached["argument_edges"], [])
        self.assertEqual(
            attached["annotation_citation_argument_bridge"][
                "detached_unrelated_claim_evidence_count"
            ],
            1,
        )
        self.assertEqual(
            attached["annotation_citation_argument_bridge"][
                "attached_claim_evidence_count"
            ],
            0,
        )

    def test_authoring_compacts_reference_diagnostics_without_losing_manifestations(
        self,
    ) -> None:
        diagnostic = {"provider": "provider", "payload": "x" * 2000}
        manifestation = {
            "reference_id": "r1:m1",
            "title": "著作",
            "year": "2000",
            "candidate_matches": [diagnostic],
            "search_attempts": [diagnostic],
            "expanded_reference_entries": [{"reference_id": "recursive"}],
            "final_revision_blocked": False,
        }
        entry = {
            "reference_id": "r1",
            "title": "著作",
            "raw_candidate_hits": [diagnostic],
            "candidate_matches": [diagnostic],
            "search_completeness": diagnostic,
            "expanded_reference_entries": [manifestation],
            "annotation_proposition_ids": ["p1"],
        }

        compacted = self.worker._authoring_compact_reference_context(
            {
                "reference_entries": [entry],
                "reference_sections": [
                    {"section_key": "consulted", "entries": [entry]}
                ],
                "machine_reference_entries": [],
            }
        )

        resolved = compacted["reference_entries"][0]
        self.assertNotIn("candidate_matches", resolved)
        self.assertNotIn("raw_candidate_hits", resolved)
        self.assertEqual(resolved["annotation_proposition_ids"], ["p1"])
        self.assertEqual(len(resolved["expanded_reference_entries"]), 1)
        child = resolved["expanded_reference_entries"][0]
        self.assertEqual(child["reference_id"], "r1:m1")
        self.assertNotIn("candidate_matches", child)
        self.assertNotIn("search_attempts", child)
        self.assertNotIn("expanded_reference_entries", child)
        self.assertEqual(
            compacted["reference_context_compaction"]["expanded_manifestation_depth"],
            1,
        )

    def test_authoring_audit_uses_raw_workflow_ids_consistently(self) -> None:
        plan = type("Plan", (), {"id": "plan-1", "revision": 2})()
        persisted = {
            "claims": [{"id": "plan-1@2:c1"}],
            "section_contracts": [
                {
                    "section_id": "plan-1@2:s1",
                    "claim_ids": ["plan-1@2:c1"],
                    "external_source_id": "annotation-citation:r1",
                }
            ],
        }

        with patch.object(WorkerRunner, "_audit_payload", return_value=persisted):
            projected = self.worker._authoring_audit_llm_payload(plan)

        self.assertEqual(projected["claims"][0]["id"], "c1")
        self.assertEqual(projected["section_contracts"][0]["section_id"], "s1")
        self.assertEqual(projected["section_contracts"][0]["claim_ids"], ["c1"])
        self.assertEqual(
            projected["section_contracts"][0]["external_source_id"],
            "annotation-citation:r1",
        )

    def test_authoring_economy_polish_still_runs_quality_stage(self) -> None:
        context = {
            "plan_id": "plan-1",
            "plan_revision": 1,
            "economy_priority": True,
            "language": "ja",
            "locked_plan": {},
            "section_contracts": [{"section_id": "s1", "title": "結論"}],
            "draft_sections": [
                {
                    "section_id": "s1",
                    "text": "元の本文である。",
                }
            ],
            "proposed_html": '<article id="old"><p id="old-p">旧稿</p></article>',
        }

        generated = [
            {
                "sections": [
                    {
                        "section_id": "s1",
                        "text": "校正済みの本文である。",
                        "changed_claim_ids": [],
                    }
                ]
            }
        ]
        with patch.object(
            self.worker, "_authoring_parallel_generate", return_value=generated
        ) as parallel_generate, patch.object(
            self.worker, "_authoring_v29_plan_bridge", return_value={}
        ):
            result = self.worker._authoring_polish(object(), context).context

        parallel_generate.assert_called_once()
        self.assertEqual(
            parallel_generate.call_args.kwargs["stage"], "polish_prose"
        )
        self.assertFalse(result["authoring_economy_polish_skipped"])
        self.assertEqual(
            result["polished_sections"],
            [{"section_id": "s1", "text": "校正済みの本文である。"}],
        )
        self.assertIn("校正済みの本文である。", result["proposed_html"])

    def test_authoring_draft_contract_requires_realized_development(self) -> None:
        context = {
            "argument_nodes": [
                {
                    "id": "e1",
                    "node_type": "evidence",
                    "claim_id": "c1",
                    "provenance": {
                        "source": "memo",
                        "source_ids": ["p1"],
                        "verification_status": "unverified",
                    },
                },
                {"id": "w1", "node_type": "warrant", "claim_id": "c1"},
                {"id": "q1", "node_type": "qualifier", "claim_id": "c1"},
            ],
            "section_contracts": [
                {
                    "section_id": "s1",
                    "article_role": "body",
                    "claim_ids": ["c1"],
                    "evidence_ids": ["e1"],
                    "warrant_ids": ["w1"],
                    "qualifiers": ["範囲内に限る"],
                    "development_expectations": {
                        "minimum_evidence_items": 1,
                        "minimum_concrete_examples": 1,
                        "required_argument_moves": [
                            "claim",
                            "evidence",
                            "warrant",
                            "qualification",
                            "implication",
                        ],
                    },
                }
            ],
            "semantic_paragraph_plan": [
                {
                    "section_id": "s1",
                    "paragraphs": [
                        {"move_indices": [1, 2]},
                        {"move_indices": [2, 3, 4]},
                        {"move_indices": [4, 5]},
                    ],
                }
            ],
        }
        section = {
            "section_id": "s1",
            "article_role": "body",
            "paragraphs": [
                {
                    "move_indices": [1, 2],
                    "evidence_ids": ["e1"],
                    "text": "主張から根拠へ移る。",
                },
                {
                    "move_indices": [2, 3, 4],
                    "evidence_ids": [],
                    "text": "根拠を示し、その理由づけと適用範囲を説明する。",
                },
                {
                    "move_indices": [4, 5],
                    "evidence_ids": [],
                    "text": "限定を踏まえて含意を示す。",
                },
            ],
            "text": (
                "主張から根拠へ移る。\n\n"
                "根拠を示し、その理由づけと適用範囲を説明する。\n\n"
                "限定を踏まえて含意を示す。"
            ),
            "claim_ids": ["c1"],
            "evidence_ids": ["e1"],
            "realized_evidence_ids": ["e1"],
            "realized_warrant_ids": ["w1"],
            "realized_qualifier_ids": ["q1"],
            "evidence_treatment": [
                {
                    "evidence_id": "e1",
                    "claim_id": "c1",
                    "basis": "memo_close_reading",
                    "verification_status": "unverified",
                    "prose_treatment": "attributed_close_reading",
                    "qualifier_id": "q1",
                }
            ],
            "paragraph_moves_realized": [
                "claim",
                "evidence",
                "warrant",
                "qualification",
                "implication",
            ],
            "concrete_examples": ["具体例"],
        }
        self.assertEqual(
            self.worker._authoring_draft_contract_violations(context, [section]),
            [],
        )
        duplicate_treatment = {
            **section,
            "evidence_treatment": [
                *section["evidence_treatment"],
                dict(section["evidence_treatment"][0]),
            ],
        }
        self.assertTrue(
            any(
                "duplicated evidence treatment records" in problem
                for problem in self.worker._authoring_draft_contract_violations(
                    context, [duplicate_treatment]
                )
            )
        )
        self.assertEqual(
            self.worker._authoring_economy_draft_violations(context, [section]),
            [],
        )

        audit_context = {
            **context,
            "argument_nodes": [
                *context["argument_nodes"],
                {
                    "id": "e2",
                    "node_type": "evidence",
                    "claim_id": "c1",
                    "provenance": {
                        "source": "memo",
                        "source_ids": ["p2"],
                        "verification_status": "unverified",
                    },
                },
                {
                    "id": "e3",
                    "node_type": "evidence",
                    "claim_id": "c1",
                    "provenance": {
                        "source": "memo",
                        "source_ids": ["p3"],
                        "verification_status": "unverified",
                    },
                },
            ],
            "section_contracts": [
                {
                    **context["section_contracts"][0],
                    "evidence_ids": ["e1", "e2", "e3"],
                    "development_expectations": {
                        **context["section_contracts"][0]["development_expectations"],
                        "minimum_evidence_items": 3,
                    },
                }
            ],
            "audit_findings": [
                {
                    "code": "evidence_mismatch",
                    "severity": "major",
                    "object_id": "section:s1",
                    "message": "e3 does not support the sentence and must be removed.",
                }
            ],
        }
        repaired_section = {
            **section,
            "paragraphs": [
                {**section["paragraphs"][0], "evidence_ids": ["e1", "e2"]},
                *section["paragraphs"][1:],
            ],
            "evidence_ids": ["e1", "e2"],
            "realized_evidence_ids": ["e1", "e2"],
            "evidence_treatment": [
                *section["evidence_treatment"],
                {
                    **section["evidence_treatment"][0],
                    "evidence_id": "e2",
                },
            ],
        }
        for validator in (
            self.worker._authoring_draft_contract_violations,
            self.worker._authoring_economy_draft_violations,
        ):
            repaired_problems = validator(audit_context, [repaired_section])
            self.assertFalse(
                any(
                    "did not realize enough contract evidence" in item
                    for item in repaired_problems
                ),
                repaired_problems,
            )
            underfilled = {
                **repaired_section,
                "paragraphs": [
                    {**repaired_section["paragraphs"][0], "evidence_ids": ["e1"]},
                    *repaired_section["paragraphs"][1:],
                ],
                "evidence_ids": ["e1"],
                "realized_evidence_ids": ["e1"],
                "evidence_treatment": [section["evidence_treatment"][0]],
            }
            self.assertTrue(
                any(
                    "did not realize enough contract evidence" in item
                    for item in validator(audit_context, [underfilled])
                )
            )
        rejected_retained = {
            **repaired_section,
            "paragraphs": [
                {
                    **repaired_section["paragraphs"][0],
                    "evidence_ids": ["e1", "e2", "e3"],
                },
                *repaired_section["paragraphs"][1:],
            ],
            "evidence_ids": ["e1", "e2", "e3"],
            "realized_evidence_ids": ["e1", "e2", "e3"],
            "evidence_treatment": [
                *repaired_section["evidence_treatment"],
                {
                    **section["evidence_treatment"][0],
                    "evidence_id": "e3",
                },
            ],
        }
        self.assertTrue(
            any(
                "treated non-contract evidence e3" in item
                for item in self.worker._authoring_draft_contract_violations(
                    audit_context, [rejected_retained]
                )
            )
        )
        persisted_context, _allowances = (
            self.worker._authoring_expand_evidence_gap_repair_context(audit_context)
        )
        self.assertEqual(
            persisted_context["authoring_rejected_evidence_ids_by_section"],
            {"s1": ["e3"]},
        )
        later_audit_context = {**persisted_context, "audit_findings": []}
        self.assertEqual(
            self.worker._authoring_rejected_evidence_ids_by_section(
                later_audit_context
            ),
            {"s1": {"e3"}},
        )
        japanese_adjacent_context = {
            **audit_context,
            "audit_findings": [
                {
                    "code": "evidence_mismatch",
                    "severity": "major",
                    "object_id": "section:s1",
                    "message": (
                        "第二段落に付いたe3はこの文を支えない。"
                        "この添付を外して推論として述べる。"
                    ),
                }
            ],
        }
        self.assertEqual(
            self.worker._authoring_rejected_evidence_ids_by_section(
                japanese_adjacent_context
            )["s1"],
            {"e3"},
        )
        projected_contract = self.worker._authoring_draft_contracts_with_scholarship(
            later_audit_context,
            later_audit_context["section_contracts"],
        )[0]
        self.assertEqual(projected_contract["evidence_ids"], ["e1", "e2"])
        allowance_context = {
            **later_audit_context,
            "draft_sections": [
                {"section_id": "s1", "realized_evidence_ids": ["e1", "e2", "e3"]}
            ],
            "argument_nodes": [
                *later_audit_context["argument_nodes"],
                {
                    "id": "e4",
                    "node_type": "evidence",
                    "claim_id": "c1",
                    "provenance": {
                        "source": "memo",
                        "source_ids": ["p4"],
                        "verification_status": "unverified",
                    },
                },
            ],
            "authoring_repair_evidence_allowances": [
                {"section_id": "s1", "node_id": "e4", "claim_id": "c1"}
            ],
        }
        eligible, effective_minimum = (
            self.worker._authoring_effective_contract_evidence(
                allowance_context,
                "s1",
                {"e1", "e2", "e3", "e4"},
                3,
            )
        )
        self.assertEqual(eligible, {"e1", "e2", "e4"})
        self.assertEqual(effective_minimum, 2)
        self.assertTrue(
            any(
                "treated non-contract evidence e3" in item
                for item in self.worker._authoring_draft_contract_violations(
                    later_audit_context, [rejected_retained]
                )
            )
        )

        rag_context = {
            **context,
            "argument_nodes": [
                {
                    "id": "e1",
                    "node_type": "evidence",
                    "claim_id": "c1",
                    "provenance": {
                        "source": "rag",
                        "source_ids": ["retrieval-candidate"],
                        "verification_status": "unverified",
                    },
                },
                {"id": "w1", "node_type": "warrant", "claim_id": "c1"},
                {"id": "q1", "node_type": "qualifier", "claim_id": "c1"},
            ],
        }
        mislabeled = {
            **section,
            "evidence_treatment": [
                {
                    **section["evidence_treatment"][0],
                    "basis": "external_source",
                    "verification_status": "unverified",
                    "prose_treatment": "qualified_interpretation",
                }
            ],
        }
        normalized, normalizations = (
            self.worker._authoring_normalize_evidence_treatments(
                rag_context, [mislabeled]
            )
        )
        treatment = normalized[0]["evidence_treatment"][0]
        self.assertEqual(treatment["basis"], "llm_inference")
        self.assertEqual(treatment["verification_status"], "unverified")
        self.assertEqual(treatment["prose_treatment"], "qualified_interpretation")
        self.assertEqual(treatment["qualifier_id"], "q1")
        self.assertEqual(len(normalizations), 1)
        self.assertFalse(
            any(
                "marked unverified material" in problem
                for problem in self.worker._authoring_draft_contract_violations(
                    rag_context, normalized
                )
            )
        )

        verified_context = {
            **rag_context,
            "argument_nodes": [
                {
                    **rag_context["argument_nodes"][0],
                    "provenance": {
                        "source": "rag",
                        "source_ids": ["accepted-source"],
                        "verification_status": "verified",
                    },
                },
                *rag_context["argument_nodes"][1:],
            ],
        }
        verified, verified_normalizations = (
            self.worker._authoring_normalize_evidence_treatments(
                verified_context,
                [
                    {
                        **mislabeled,
                        "evidence_treatment": [
                            {
                                **mislabeled["evidence_treatment"][0],
                                "verification_status": "verified",
                            }
                        ],
                    }
                ],
            )
        )
        self.assertEqual(
            verified[0]["evidence_treatment"][0]["basis"], "external_source"
        )
        self.assertEqual(
            verified[0]["evidence_treatment"][0]["support_sentence"],
            section["paragraphs"][0]["text"],
        )
        self.assertEqual(len(verified_normalizations), 1)
        self.assertEqual(
            verified_normalizations[0]["policy"],
            "canonical-argument-provenance-v1",
        )
        violations = self.worker._authoring_draft_contract_violations(
            context,
            [
                {
                    **section,
                    "paragraphs": [
                        {
                            "move_indices": [7, 8],
                            "evidence_ids": ["e1"],
                            "text": "主張と根拠。",
                        },
                        {
                            "move_indices": [3, 4],
                            "evidence_ids": [],
                            "text": "理由づけと限定。",
                        },
                    ],
                    "paragraph_moves_realized": [
                        "claim",
                        "evidence",
                        "warrant",
                        "qualification",
                    ],
                }
            ],
        )
        self.assertTrue(
            any(
                "omitted required argument-move indices: [1, 2, 5]" in item
                for item in violations
            )
        )
        economy_violations = self.worker._authoring_economy_draft_violations(
            context,
            [
                {
                    **section,
                    "paragraphs": [
                        {
                            "move_indices": [1, 2, 3],
                            "evidence_ids": ["e1"],
                            "text": "主張・根拠・理由。",
                        },
                        {
                            "move_indices": [4],
                            "evidence_ids": [],
                            "text": "限定。",
                        },
                    ],
                    "paragraph_moves_realized": [
                        "claim",
                        "evidence",
                        "warrant",
                        "qualification",
                    ],
                }
            ],
        )
        self.assertTrue(
            any("argument-move indices: [5]" in item for item in economy_violations)
        )

    def test_authoring_reconciles_only_mismatched_theory_titles(self) -> None:
        citation_id = "annotation-citation:conclusion_scholarship"
        translated_id = "annotation-citation:williams"
        generic_id = "annotation-citation:conclusion_scholarship-generic"
        context = {
            "argument_nodes": [
                {
                    "id": "e-other",
                    "node_type": "evidence",
                    "claim_id": "c1",
                    "proposition_id": "p1",
                    "text": "ブルデュー『芸術の規則』を比較候補にする。",
                    "provenance": {
                        "source": "mixed",
                        "source_ids": ["p1", citation_id],
                        "verification_status": "unverified",
                    },
                },
                {
                    "id": "e-matching",
                    "node_type": "evidence",
                    "claim_id": "c1",
                    "proposition_id": "p2",
                    "text": "ランシエール『感性的なもののパルタージュ』を参照する。",
                    "provenance": {
                        "source": "mixed",
                        "source_ids": ["p2", citation_id],
                        "verification_status": "unverified",
                    },
                },
                {
                    "id": "e-translated",
                    "node_type": "evidence",
                    "claim_id": "c2",
                    "proposition_id": "p3",
                    "text": "ウィリアムズ『マルクス主義と文学』を参照する。",
                    "provenance": {
                        "source": "mixed",
                        "source_ids": ["p3", translated_id],
                        "verification_status": "unverified",
                    },
                },
                {
                    "id": "e-generic-work",
                    "node_type": "evidence",
                    "claim_id": "c1",
                    "proposition_id": "p4",
                    "text": (
                        "ランシエールの著作は参考文献として同定されているが、"
                        "この定義を裏づける本文の確認はない。"
                    ),
                    "provenance": {
                        "source": "mixed",
                        "source_ids": ["p4", generic_id],
                        "verification_status": "unverified",
                    },
                },
            ],
            "annotation_citation_evidence": [
                {
                    "id": citation_id,
                    "allowed_use": "attributed_scholarship_citation",
                    "title": "感性的なもののパルタージュ : 美学と政治",
                    "authors": ["ランシエール"],
                    "excerpt": "ランシエール『感性的なもののパルタージュ』",
                    "annotation_proposition_ids": ["p1", "p2"],
                    "claim_ids": ["c1"],
                },
                {
                    "id": translated_id,
                    "allowed_use": "attributed_scholarship_citation",
                    "title": "Marxism and literature",
                    "authors": ["ウィリアムズ"],
                    "excerpt": "ウィリアムズ『マルクス主義と文学』",
                    "annotation_proposition_ids": ["p3"],
                    "claim_ids": ["c2"],
                },
                {
                    "id": generic_id,
                    "allowed_use": "attributed_scholarship_citation",
                    "verification_scope": "bibliographic_identity_only",
                    "title": "感性的なもののパルタージュ : 美学と政治",
                    "authors": ["ランシエール"],
                    "excerpt": "ランシエール『感性的なもののパルタージュ』",
                    "annotation_proposition_ids": ["p4"],
                    "claim_ids": ["c1"],
                },
            ],
            "authoring_atomic_evidence_contracts": [
                {
                    "proposition_id": "p1",
                    "claim_ids": ["c1"],
                    "realization": "attribute_theory_with_citation",
                    "citation_evidence_ids": [citation_id],
                }
            ],
            "evidence_realization_decisions": [
                {
                    "proposition_id": "p1",
                    "realization": "attribute_theory_with_citation",
                    "citation_evidence_ids": [citation_id],
                }
            ],
            "section_contracts": [
                {"section_id": "s1", "claim_ids": ["c1"], "evidence_ids": []}
            ],
        }
        result = self.worker._authoring_reconcile_theory_citation_titles(context)
        self.assertEqual(
            [item["node_id"] for item in result["argument_graph_citation_title_corrections"]],
            ["e-other", "e-generic-work"],
        )
        self.assertEqual(
            result["argument_nodes"][0]["provenance"]["source_ids"], ["p1"]
        )
        self.assertEqual(result["argument_nodes"][0]["provenance"]["source"], "memo")
        self.assertEqual(
            result["authoring_atomic_evidence_contracts"][0]["realization"],
            "attribute_to_memo",
        )
        self.assertEqual(
            result["evidence_realization_decisions"][0]["citation_evidence_ids"],
            [],
        )
        self.assertEqual(
            result["annotation_citation_evidence"][0]["annotation_proposition_ids"],
            ["p2"],
        )
        self.assertEqual(
            result["argument_nodes"][2]["provenance"]["source_ids"],
            ["p3", translated_id],
        )
        self.assertEqual(
            result["argument_nodes"][3]["provenance"]["source_ids"], ["p4"]
        )
        self.assertEqual(
            result["argument_graph_citation_title_corrections"][1]["reason"],
            "specific_work_not_named_in_evidence_node",
        )
        self.assertEqual(
            self.worker._authoring_draft_contracts_with_scholarship(
                result, result["section_contracts"]
            )[0]["evidence_ids"],
            ["e-matching", "e-other", "e-generic-work"],
        )
        draft_problems = self.worker._authoring_draft_contract_violations(
            result,
            [
                {
                    "section_id": "s1",
                    "claim_ids": ["c1"],
                    "paragraphs": [
                        {"move_indices": [], "evidence_ids": ["e-other"], "text": "本文"}
                    ],
                    "realized_evidence_ids": ["e-other"],
                    "evidence_treatment": [
                        {"evidence_id": "e-other", "basis": "memo_close_reading"}
                    ],
                }
            ],
        )
        self.assertFalse(
            any("non-contract evidence" in problem for problem in draft_problems),
            draft_problems,
        )
        self.assertEqual(
            self.worker._authoring_reconcile_theory_citation_titles(result), result
        )

    def test_authoring_requires_visible_reason_for_scholarship_citation(self) -> None:
        attribution = (
            "東浩紀が『動物化するポストモダン』で示したデータベース的消費の議論を、"
            "雑誌から検索へ配置単位が変わる局面を捉える比較枠として用いる。"
        )
        context = {
            "argument_nodes": [
                {
                    "id": "e-theory",
                    "node_type": "evidence",
                    "claim_id": "c1",
                    "provenance": {"source_ids": ["annotation-citation:azuma"]},
                },
                {"id": "w1", "node_type": "warrant", "claim_id": "c1"},
                {"id": "q1", "node_type": "qualifier", "claim_id": "c1"},
            ],
            "evidence_context": [
                {
                    "id": "annotation-citation:azuma",
                    "allowed_use": "attributed_scholarship_citation",
                    "authors": ["東浩紀"],
                    "title": "動物化するポストモダン",
                }
            ],
            "section_contracts": [
                {
                    "section_id": "s1",
                    "article_role": "body",
                    "claim_ids": ["c1"],
                    "evidence_ids": [],
                    "warrant_ids": ["w1"],
                    "development_expectations": {
                        "minimum_evidence_items": 1,
                        "minimum_concrete_examples": 1,
                        "required_argument_moves": [
                            "orientation", "claim", "evidence", "warrant",
                            "qualification",
                        ],
                    },
                }
            ],
            "semantic_paragraph_plan": [
                {
                    "section_id": "s1",
                    "paragraphs": [{"move_indices": [1, 2, 3, 4, 5]}],
                }
            ],
        }
        section = {
            "section_id": "s1",
            "article_role": "body",
            "paragraphs": [
                {
                    "move_indices": [1, 2, 3, 4, 5],
                    "evidence_ids": ["e-theory"],
                    "text": attribution,
                }
            ],
            "claim_ids": ["c1"],
            "evidence_ids": ["e-theory"],
            "realized_evidence_ids": ["e-theory"],
            "realized_warrant_ids": ["w1"],
            "realized_qualifier_ids": ["q1"],
            "evidence_treatment": [
                {
                    "evidence_id": "e-theory",
                    "claim_id": "c1",
                    "basis": "external_source",
                    "verification_status": "verified",
                    "prose_treatment": "attributed_scholarship",
                    "qualifier_id": None,
                    "citation_purpose": "媒体配置の変化を比較する理論枠として用いる",
                    "attribution_sentence": attribution,
                    "purpose_clause": (
                        "雑誌から検索へ配置単位が変わる局面を捉える比較枠として用いる"
                    ),
                }
            ],
            "paragraph_moves_realized": [
                "orientation", "claim", "evidence", "warrant", "qualification",
            ],
            "concrete_examples": ["雑誌から検索への配置単位の変化"],
        }

        self.assertEqual(
            self.worker._authoring_draft_contract_violations(context, [section]),
            [],
        )
        self.assertEqual(
            self.worker._authoring_economy_draft_violations(context, [section]),
            [],
        )

        bare_citation = {
            **section,
            "paragraphs": [
                {**section["paragraphs"][0], "text": "媒体の変化は重要である。"}
            ],
            "evidence_treatment": [
                {
                    **section["evidence_treatment"][0],
                    "citation_purpose": "",
                    "attribution_sentence": "",
                }
            ],
        }
        for validator in (
            self.worker._authoring_draft_contract_violations,
            self.worker._authoring_economy_draft_violations,
        ):
            violations = validator(context, [bare_citation])
            self.assertTrue(any("no citation purpose" in item for item in violations))
            self.assertTrue(
                any("no unique sentence-level footnote anchor" in item for item in violations)
            )

    def test_v31_semantic_plan_preserves_unselected_catalog_labels_without_quota(self) -> None:
        claimed = ClaimedNode(
            node_run_id="authoring-node",
            run_id="authoring-run",
            node_id="plan_semantic_paragraphs",
            attempt=1,
            workflow_name="authoring",
            workflow_version="31",
            resource_lane="codex_remote",
            lease_owner="test-worker",
            project_id="project-1",
            inputs={},
        )
        context = {
            "output_language": "ja",
            "section_contracts": [{
                "section_id": "s1",
                "development_expectations": {
                    "required_argument_moves": ["research_question"],
                },
            }],
            "manuscript_structure_plan": {},
            "argument_nodes": [],
        }
        generated = {"sections": [{
            "section_id": "s1",
            "paragraphs": [
                {
                    "rhetorical_moves": ["key_concept_or_distinction", "unknown_label"],
                    "claim_ids": [],
                    "evidence_ids": [],
                    "development_instruction": "分析上の区別を説明する。",
                },
                {
                    "rhetorical_moves": [],
                    "claim_ids": [],
                    "evidence_ids": [],
                    "development_instruction": "次の論点へ進む。",
                },
            ],
        }]}
        with patch.object(self.worker, "_authoring_generate", return_value=generated):
            result = self.worker._authoring_plan_semantic_paragraphs(claimed, context).context
        [contract] = result["section_contracts"]
        self.assertEqual(
            contract["development_expectations"]["required_argument_moves"],
            ["research_question", "key_concept_or_distinction"],
        )
        paragraphs = result["semantic_paragraph_plan"][0]["paragraphs"]
        self.assertEqual([item["move_indices"] for item in paragraphs], [[2], []])
        self.assertEqual(paragraphs[0]["rhetorical_moves"], ["key_concept_or_distinction"])

    def test_authoring_semantic_paragraph_plan_allows_many_to_many_moves(self) -> None:
        claimed = ClaimedNode(
            node_run_id="authoring-node",
            run_id="authoring-run",
            node_id="plan_semantic_paragraphs",
            attempt=1,
            workflow_name="authoring",
            workflow_version="30",
            resource_lane="codex_remote",
            lease_owner="test-worker",
            project_id="project-1",
            inputs={},
        )
        moves = ["orientation", "claim", "evidence", "warrant", "qualification"]
        generated = {
            "sections": [
                {
                    "section_id": "s1",
                    "paragraphs": [
                        {
                            "move_indices": [1, 2],
                            "central_claim": "The section opens with its thesis.",
                            "consequence": "The evidence question follows.",
                            "rhetorical_moves": moves[:2],
                            "claim_ids": ["c1"],
                            "evidence_ids": [],
                            "development_instruction": "短く導入する。",
                        },
                        {
                            "move_indices": [8, 9, 10],
                            "central_claim": "The source supports the thesis.",
                            "consequence": "Its warrant sets the scope.",
                            "rhetorical_moves": moves[1:4],
                            "claim_ids": ["c1"],
                            "evidence_ids": ["e1"],
                            "development_instruction": "根拠を詳しく説明する。",
                        },
                        {
                            "move_indices": [10, 11],
                            "central_claim": "The claim has a boundary.",
                            "consequence": "The conclusion remains provisional.",
                            "rhetorical_moves": moves[3:],
                            "claim_ids": ["c1"],
                            "evidence_ids": [],
                            "development_instruction": "限定を示す。",
                        },
                    ],
                }
            ]
        }
        context = {
            "output_language": "ja",
            "section_contracts": [
                {
                    "section_id": "s1",
                    "development_expectations": {
                        "required_argument_moves": moves,
                    },
                }
            ],
            "manuscript_structure_plan": {},
            "argument_nodes": [],
        }

        with patch.object(self.worker, "_authoring_generate", return_value=generated):
            result = self.worker._authoring_plan_semantic_paragraphs(claimed, context).context

        paragraphs = result["semantic_paragraph_plan"][0]["paragraphs"]
        self.assertEqual(
            [item["move_indices"] for item in paragraphs],
            [[1, 2], [2, 3, 4], [4, 5]],
        )
        self.assertTrue(all(item["uniform_length_required"] is False for item in paragraphs))

    def test_authoring_semantic_paragraph_plan_fills_missing_move_locally(self) -> None:
        claimed = ClaimedNode(
            node_run_id="authoring-node",
            run_id="authoring-run",
            node_id="plan_semantic_paragraphs",
            attempt=1,
            workflow_name="authoring",
            workflow_version="30",
            resource_lane="codex_remote",
            lease_owner="test-worker",
            project_id="project-1",
            inputs={},
        )
        moves = ["orientation", "claim", "evidence", "warrant", "qualification"]
        generated = {
            "sections": [
                {
                    "section_id": "s1",
                    "paragraphs": [
                        {
                            "move_indices": [1, 2],
                            "central_claim": "The section states its thesis.",
                            "consequence": "Evidence must follow.",
                            "rhetorical_moves": moves[:2],
                            "claim_ids": ["c1"],
                            "evidence_ids": [],
                            "development_instruction": "導入する。",
                        },
                        {
                            "move_indices": [3, 4],
                            "central_claim": "The evidence develops the thesis.",
                            "consequence": "The warrant limits the claim.",
                            "rhetorical_moves": moves[2:4],
                            "claim_ids": ["c1"],
                            "evidence_ids": ["e1"],
                            "development_instruction": "根拠を説明する。",
                        },
                    ],
                }
            ]
        }
        context = {
            "output_language": "ja",
            "section_contracts": [
                {
                    "section_id": "s1",
                    "development_expectations": {
                        "required_argument_moves": moves,
                    },
                }
            ],
            "manuscript_structure_plan": {},
            "argument_nodes": [],
        }

        with patch.object(self.worker, "_authoring_generate", return_value=generated):
            result = self.worker._authoring_plan_semantic_paragraphs(claimed, context).context

        paragraphs = result["semantic_paragraph_plan"][0]["paragraphs"]
        self.assertEqual([item["move_indices"] for item in paragraphs], [[1, 2], [3, 4, 5]])
        self.assertIn("qualification", paragraphs[-1]["rhetorical_moves"])
        self.assertIn("qualification", paragraphs[-1]["development_instruction"])
        self.assertEqual(
            result["semantic_paragraph_plan_normalizations"],
            [
                {
                    "section_id": "s1",
                    "move_index": 5,
                    "move": "qualification",
                    "paragraph_index": 2,
                    "policy": "nearest-semantic-paragraph-v1",
                }
            ],
        )

    def test_authoring_normalizes_structured_draft_paragraphs_to_locked_moves(
        self,
    ) -> None:
        moves = ["claim", "evidence", "warrant", "qualification", "implication"]
        context = {
            "plan_id": "plan-1",
            "plan_revision": 1,
            "language": "ja",
            "section_contracts": [
                {
                    "section_id": "s1",
                    "development_expectations": {
                        "required_argument_moves": moves,
                    },
                }
            ],
        }

        normalized = self.worker._authoring_normalize_generated_section_ids(
            context,
            [
                {
                    "section_id": "s1",
                    "paragraphs": [
                        {"move_indices": [1, 2], "text": "短い導入。"},
                        {
                            "move_indices": [2, 3, 4],
                            "text": "根拠を詳しく説明し、理由づけと限定を接続する。",
                        },
                        {"move_indices": [4, 5], "text": "含意。"},
                    ],
                }
            ],
        )[0]

        self.assertEqual(
            normalized["paragraph_move_index_groups"],
            [[1, 2], [2, 3, 4], [4, 5]],
        )
        self.assertEqual(normalized["paragraph_move_indices"], [1, 2, 2, 3, 4, 4, 5])
        self.assertEqual(set(normalized["paragraph_moves_realized"]), set(moves))
        self.assertEqual(normalized["text"].count("\n\n"), 2)
        self.assertLess(len(normalized["paragraphs"][0]["text"]), 80)

    def test_authoring_polish_batches_sections_with_read_only_neighbours(self) -> None:
        claimed = ClaimedNode(
            node_run_id="authoring-node",
            run_id="authoring-run",
            node_id="polish_prose",
            attempt=1,
            workflow_name="authoring",
            workflow_version="30",
            resource_lane="codex_remote",
            lease_owner="test-worker",
            project_id="project-1",
            inputs={},
        )
        contracts = [
            {"section_id": f"s{index}", "title": f"Section {index}"} for index in range(1, 7)
        ]
        drafts = [
            {"section_id": item["section_id"], "text": item["section_id"]} for item in contracts
        ]
        captured: list[dict[str, object]] = []

        def generate_batches(*_args, **kwargs):
            payloads = list(kwargs["payloads"])
            captured.extend(payloads)
            return [
                {
                    "sections": [
                        {
                            "section_id": section["section_id"],
                            "text": section["text"],
                            "changed_claim_ids": [],
                        }
                        for section in payload["sections"]
                    ]
                }
                for payload in payloads
            ]

        with patch.object(
            self.worker, "_authoring_parallel_generate", side_effect=generate_batches
        ):
            result = self.worker._authoring_polish(
                claimed,
                {
                    "plan_id": "plan-1",
                    "locked_plan": {"sections": contracts},
                    "section_contracts": contracts,
                    "draft_sections": drafts,
                    "proposed_html": '<article id="old"><p id="old-p">旧稿</p></article>',
                    "language": "ja",
                },
            ).context
        self.assertEqual(len(captured), 2)
        self.assertEqual(captured[0]["target_section_ids"], [f"s{i}" for i in range(1, 6)])
        self.assertEqual(captured[0]["adjacent_sections"][0]["section_id"], "s6")
        self.assertEqual(captured[1]["adjacent_sections"][0]["section_id"], "s5")
        self.assertEqual(
            [item["section_id"] for item in result["polished_sections"]],
            [item["section_id"] for item in contracts],
        )

    def test_corpus_sync_ingests_nested_sources_and_publishes_index(self) -> None:
        managed = self.settings.paths.corpus_root / "managed_sources" / "collection" / "author"
        managed.mkdir(parents=True)
        sample = managed / "sample.txt"
        sample.write_text(
            "La recherche documentaire exige une provenance vérifiable.",
            encoding="utf-8",
        )
        excluded = self.settings.paths.corpus_root / "managed_sources" / "other" / "ignored.txt"
        excluded.parent.mkdir(parents=True)
        excluded.write_text("Ce document ne fait pas partie du dossier choisi.", encoding="utf-8")
        selected_root = self.settings.paths.corpus_root / "managed_sources" / "collection"
        run_id = self._create_corpus_run(selected_root)
        self._drain()

        with self.databases.state.connect() as connection:
            snapshot = RunRepository(connection).snapshot(run_id)
        self.assertEqual(snapshot["run"]["status"], "completed")
        self.assertEqual(self.semantic_index.build_count, 1)
        corpus = CorpusSyncService(self.databases, self.settings).latest_snapshot()
        assert corpus is not None
        self.assertEqual(corpus["batch"]["status"], "completed")
        self.assertEqual(corpus["batch"]["ready_count"], 1)
        self.assertEqual(corpus["batch"]["discovered_count"], 1)
        self.assertEqual(corpus["items"][0]["relative_path"], "author/sample.txt")

        with self.databases.knowledge.connect() as connection:
            results = KnowledgeSearchService(connection).search("provenance vérifiable")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["retrieval"], "lexical")

        second_run = self._create_corpus_run(selected_root)
        self._drain()
        with self.databases.state.connect() as connection:
            second_snapshot = RunRepository(connection).snapshot(second_run)
        self.assertEqual(second_snapshot["run"]["status"], "completed")
        second_corpus = CorpusSyncService(self.databases, self.settings).latest_snapshot()
        assert second_corpus is not None
        self.assertEqual(second_corpus["batch"]["reused_count"], 1)

    def test_corpus_sync_auto_routes_declared_pdf_language(self) -> None:
        managed = self.settings.paths.corpus_root / "managed_sources" / "scans"
        managed.mkdir(parents=True)
        source = managed / "scan.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        writer._root_object[NameObject("/Lang")] = TextStringObject("fr-FR")
        with source.open("wb") as handle:
            writer.write(handle)
        run_id = self._create_corpus_run(managed)
        service = CorpusSyncService(self.databases, self.settings)

        from fukamiru.ocr.models import OCRPage, OCRResult

        def french_ocr(path, language, *, metrics=None, checkpoint_dir=None):
            self.assertEqual(language, "fr")
            return OCRResult(
                path, hashlib.sha256(path.read_bytes()).hexdigest(), language, "mistral",
                (OCRPage(1, "Un document de recherche avec ses résultats."),),
            )

        scanned = service.scan(run_id, str(managed))
        with patch(
            "fukamiru.ocr.engines.mistral.MistralOCREngine.process", side_effect=french_ocr,
        ) as engine:
            result = service.ingest_ready_sources(run_id, scanned["corpus_batch_id"])
        jobs = service.snapshot(scanned["corpus_batch_id"])["ocr_jobs"]

        self.assertEqual(result["ready_count"], 1)
        self.assertEqual(result["failed_count"], 0)
        self.assertEqual(engine.call_count, 1)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["status"], "completed")
        self.assertEqual(jobs[0]["source_language"], "fr")
        self.assertEqual(jobs[0]["engine"], "mistral")
        self.assertEqual(jobs[0]["language_origin"], "detected")
        self.assertEqual(jobs[0]["language_decision"]["method"], "pdf_declared_language")

    def test_hybrid_search_fuses_lexical_and_semantic_hits(self) -> None:
        self._create_run("ingestion")
        self._drain()
        with self.databases.knowledge.connect() as connection:
            lexical = KnowledgeSearchService(connection).search("document de recherche")
            self.assertTrue(lexical)
            semantic_hit = {**lexical[0], "score": 0.91, "retrieval": "semantic"}
            self.semantic_index.results = [semantic_hit]
            results = KnowledgeSearchService(
                connection,
                semantic_index=self.semantic_index,
            ).search("document de recherche")
        self.assertEqual(results[0]["retrieval"], "hybrid")
        self.assertIn("lexical_score", results[0])
        self.assertIn("semantic_score", results[0])


if __name__ == "__main__":
    unittest.main()
