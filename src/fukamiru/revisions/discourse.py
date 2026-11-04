"""Semantic paragraph composition and contextual predicate resolution."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from statistics import mean, median, pstdev
from typing import Any, Literal
from xml.etree import ElementTree as ET

from fukamiru.documents.canonical_html import CanonicalHTMLValidator
from fukamiru.local_llm.proofreading import (
    ProofreadingEdit,
    ProofreadingInvariantValidator,
)
from fukamiru.revisions.academic_connectives import (
    ACADEMIC_CONNECTIVE_SOURCE_URL,
    ACADEMIC_CONNECTIVE_VERSION,
    ACADEMIC_CONNECTIVES,
    academic_connective_matches,
)
from fukamiru.revisions.academic_lexicon import (
    HIGH_RISK_RELATIONAL_TERMS,
    JAWL_ACADEMIC_PREDICATES,
    JAWL_SOURCE_URL,
    JAWL_VERSION,
    academic_modality_matches,
    academic_predicate_matches,
    discourse_marker_matches,
)
from fukamiru.revisions.coherence import ManuscriptCoherenceService

_SENTENCE_ENDINGS = "。！？!?"
_QUOTE_PAIRS = {"「": "」", "『": "』", "（": "）", "(": ")", "［": "］", "[": "]"}
_CENTRAL_POSITIONS = {"initial", "medial", "final", "distributed", "implicit"}
_ENTRY_RELATIONS = {
    "section_opening",
    "continuation",
    "elaboration",
    "evidence",
    "example",
    "contrast",
    "qualification",
    "result",
    "transition",
}
_DISCOURSE_RELATIONS = {
    "supports",
    "elaborates",
    "defines",
    "exemplifies",
    "explains",
    "causes",
    "results",
    "contrasts",
    "qualifies",
    "concludes",
    "transitions",
}
_DISCOURSE_ROLES = {
    "claim",
    "explanation",
    "evidence",
    "example",
    "interpretation",
    "qualification",
    "transition",
    "conclusion",
    "definition",
    "context",
    "other",
}
_BOUNDARY_DECISIONS = {"continue", "break", "unresolved"}
_LOCKED_BLOCK_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "blockquote", "li"}
_RELATION_TYPES = {
    "cause",
    "condition",
    "contribution",
    "concession_adversative",
    "facilitation",
    "mediation",
    "representation",
    "association",
    "contrast",
    "evidence",
    "purpose",
    "elaboration",
    "restatement",
    "enumeration",
    "addition",
    "alternative",
    "example",
    "conclusion",
    "transition",
    "situation",
    "temporal",
    "constitutive",
    "literal_connection",
    "uncertain",
}
_GENERIC_RELATIONAL_TERMS = HIGH_RISK_RELATIONAL_TERMS
_ABSTRACT_NOUNS = (
    "回復",
    "羞恥",
    "支配",
    "欲望",
    "主体",
    "論理",
    "倫理",
    "感情",
    "情動",
    "制度",
    "表象",
    "経験",
    "概念",
    "議論",
    "問題",
    "関係",
    "ケア",
)


@dataclass(frozen=True)
class ParagraphPlanIssue:
    code: str
    message: str
    severity: Literal["error", "warning"] = "error"
    paragraph_id: str | None = None
    span_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
            "paragraph_id": self.paragraph_id,
            "span_id": self.span_id,
        }


@dataclass(frozen=True)
class ParagraphPlanReport:
    issues: tuple[ParagraphPlanIssue, ...]
    unit_count: int
    paragraph_count: int
    source_paragraph_count: int

    @property
    def valid(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "issues": [issue.as_dict() for issue in self.issues],
            "unit_count": self.unit_count,
            "paragraph_count": self.paragraph_count,
            "source_paragraph_count": self.source_paragraph_count,
        }


class ManuscriptDiscourseService:
    """Separate sentence/EDU identity from rendered paragraph boundaries."""

    @classmethod
    def discourse_structure(
        cls, canonical_html: str, semantic_structure: dict[str, Any]
    ) -> dict[str, Any]:
        root = ET.fromstring(canonical_html)
        block_by_id = {
            str(block["id"]): block
            for block in semantic_structure.get("blocks", [])
            if isinstance(block, dict) and block.get("id")
        }
        xref_targets = {
            href[1:]
            for element in root.iter("a")
            if (href := str(element.attrib.get("href") or "")).startswith("#")
        }
        spans: list[dict[str, Any]] = []
        fixed_block_ids: list[str] = []

        def flush(parent: ET.Element, eligible: list[ET.Element]) -> None:
            if not eligible:
                return
            first_id = str(eligible[0].attrib["id"])
            section_id = str(block_by_id[first_id]["section_id"])
            units: list[dict[str, Any]] = []
            source_block_ids: list[str] = []
            for element in eligible:
                block_id = str(element.attrib["id"])
                source_block_ids.append(block_id)
                text = "".join(element.itertext()).strip()
                for sentence_index, sentence in enumerate(cls.split_sentences(text)):
                    units.append(
                        {
                            "unit_id": f"{block_id}:u{sentence_index + 1:03d}",
                            "source_block_id": block_id,
                            "source_sentence_index": sentence_index,
                            "text": sentence,
                            "years": list(block_by_id[block_id].get("years") or []),
                            "leading_connective": (
                                block_by_id[block_id].get("leading_connective")
                                if sentence_index == 0
                                else None
                            ),
                        }
                    )
            spans.append(
                {
                    "span_id": f"paragraph-span:{first_id}",
                    "section_id": section_id,
                    "parent_id": str(parent.attrib.get("id") or ""),
                    "source_block_ids": source_block_ids,
                    "units": units,
                }
            )

        for parent in root.iter():
            eligible: list[ET.Element] = []
            active_section_id: str | None = None
            for child in list(parent):
                block_id = str(child.attrib.get("id") or "")
                block = block_by_id.get(block_id)
                section_id = str(block.get("section_id") or "") if block else ""
                recomposable = bool(
                    block
                    and block.get("rewritable_as_argument", True)
                    and child.tag == "p"
                    and not block.get("is_heading")
                    and not list(child)
                    and block_id not in xref_targets
                    and "".join(child.itertext()).strip()
                )
                if recomposable and (active_section_id is None or section_id == active_section_id):
                    eligible.append(child)
                    active_section_id = section_id
                    continue
                flush(parent, eligible)
                eligible = []
                active_section_id = None
                if (
                    block
                    and block.get("rewritable_as_argument", True)
                    and not block.get("is_heading")
                ):
                    fixed_block_ids.append(block_id)
                if recomposable:
                    eligible = [child]
                    active_section_id = section_id
            flush(parent, eligible)

        lengths = [
            sum(
                len(str(unit["text"]))
                for unit in span["units"]
                if str(unit["source_block_id"]) == block_id
            )
            for span in spans
            for block_id in span["source_block_ids"]
        ]
        baseline = {
            "paragraph_count": len(lengths),
            "median_characters": median(lengths) if lengths else 0,
            "mean_characters": mean(lengths) if lengths else 0,
            "coefficient_of_variation": (
                pstdev(lengths) / mean(lengths) if lengths and mean(lengths) else 0
            ),
        }
        return {
            "spans": spans,
            "fixed_block_ids": fixed_block_ids,
            "unit_count": sum(len(span["units"]) for span in spans),
            "source_paragraph_count": sum(len(span["source_block_ids"]) for span in spans),
            "source_distribution": baseline,
            "boundaries": cls.boundary_inventory({"spans": spans}),
        }

    @classmethod
    def boundary_inventory(cls, structure: dict[str, Any]) -> list[dict[str, Any]]:
        """Return every editable sentence adjacency exactly once.

        Boundary annotation is intentionally independent from paragraph grouping.  A
        model therefore pays the same annotation cost whether a paragraph eventually
        contains two sentences or twenty, avoiding a validator incentive to emit
        uniformly small groups.
        """

        boundaries: list[dict[str, Any]] = []
        for span in structure.get("spans", []):
            span_id = str(span.get("span_id") or "")
            units = [item for item in span.get("units", []) if isinstance(item, dict)]
            for left, right in zip(units, units[1:], strict=False):
                left_id = str(left.get("unit_id") or "")
                right_id = str(right.get("unit_id") or "")
                digest = hashlib.sha256(f"{span_id}|{left_id}|{right_id}".encode()).hexdigest()[:20]
                boundaries.append(
                    {
                        "boundary_id": f"sentence-boundary:{digest}",
                        "span_id": span_id,
                        "left_unit_id": left_id,
                        "right_unit_id": right_id,
                        "source_boundary": str(left.get("source_block_id") or "")
                        != str(right.get("source_block_id") or ""),
                    }
                )
        return boundaries

    @staticmethod
    def substructure(structure: dict[str, Any], spans: list[dict[str, Any]]) -> dict[str, Any]:
        """Create a bounded planner packet without inventing paragraph boundaries."""

        selected_ids = {str(span.get("span_id") or "") for span in spans}
        selected = [
            span
            for span in structure.get("spans", [])
            if str(span.get("span_id") or "") in selected_ids
        ]
        return {
            "spans": selected,
            "boundaries": ManuscriptDiscourseService.boundary_inventory({"spans": selected}),
        }

    @classmethod
    def default_boundary_analysis(
        cls,
        structure: dict[str, Any],
        *,
        reason: str = "Preserve the source boundary as a deterministic fallback.",
    ) -> dict[str, Any]:
        """Describe source boundaries without claiming a new semantic judgment."""

        annotations: list[dict[str, Any]] = []
        for span in structure.get("spans", []):
            for unit in span.get("units", []):
                if not isinstance(unit, dict):
                    continue
                years = [str(item) for item in unit.get("years", []) if str(item)]
                annotations.append(
                    {
                        "unit_id": str(unit.get("unit_id") or ""),
                        "discourse_role": "other",
                        "local_purpose": "Preserve the source sentence function.",
                        "governing_proposition": "Preserve the source proposition.",
                        "topic_entities": [],
                        "temporal_scope": "–".join(years) if years else None,
                    }
                )
        decisions = [
            {
                **boundary,
                "decision": "break" if boundary["source_boundary"] else "continue",
                "relation": ("transitions" if boundary["source_boundary"] else "elaborates"),
                "reason": reason,
                "confidence": 1.0,
            }
            for boundary in cls.boundary_inventory(structure)
        ]
        return {
            "policy": "source_boundary_fallback",
            "unit_annotations": annotations,
            "boundary_decisions": decisions,
        }

    @classmethod
    def validate_boundary_analysis(
        cls, structure: dict[str, Any], analysis: dict[str, Any]
    ) -> dict[str, Any]:
        """Validate one-shot unit and adjacency annotations without scoring length."""

        issues: list[dict[str, Any]] = []
        expected_units = [
            str(unit.get("unit_id") or "")
            for span in structure.get("spans", [])
            for unit in span.get("units", [])
            if isinstance(unit, dict)
        ]
        expected_boundaries = cls.boundary_inventory(structure)
        expected_by_id = {str(item["boundary_id"]): item for item in expected_boundaries}
        annotations = (
            analysis.get("unit_annotations")
            if isinstance(analysis.get("unit_annotations"), list)
            else []
        )
        annotation_ids: list[str] = []
        for item in annotations:
            if not isinstance(item, dict):
                issues.append(
                    {
                        "code": "paragraph.unit_annotation_invalid",
                        "message": "A discourse-unit annotation is not an object.",
                    }
                )
                continue
            unit_id = str(item.get("unit_id") or "")
            annotation_ids.append(unit_id)
            if str(item.get("discourse_role") or "") not in _DISCOURSE_ROLES:
                issues.append(
                    {
                        "code": "paragraph.discourse_role_invalid",
                        "message": "A discourse unit has an unknown rhetorical role.",
                        "unit_id": unit_id,
                    }
                )
            for key in ("local_purpose", "governing_proposition"):
                if not str(item.get(key) or "").strip():
                    issues.append(
                        {
                            "code": f"paragraph.{key}_missing",
                            "message": f"A discourse unit requires {key}.",
                            "unit_id": unit_id,
                        }
                    )
            topic_entities = item.get("topic_entities")
            if not isinstance(topic_entities, list) or any(
                not isinstance(entity, str) for entity in topic_entities
            ):
                issues.append(
                    {
                        "code": "paragraph.topic_entities_invalid",
                        "message": "Topic entities must be an ordered string list.",
                        "unit_id": unit_id,
                    }
                )
            temporal_scope = item.get("temporal_scope")
            if temporal_scope is not None and not isinstance(temporal_scope, str):
                issues.append(
                    {
                        "code": "paragraph.temporal_scope_invalid",
                        "message": "Temporal scope must be text or null.",
                        "unit_id": unit_id,
                    }
                )
        if annotation_ids != expected_units:
            issues.append(
                {
                    "code": "paragraph.unit_annotation_coverage",
                    "message": (
                        "Every discourse unit must be annotated exactly once in source order."
                    ),
                }
            )

        decisions = (
            analysis.get("boundary_decisions")
            if isinstance(analysis.get("boundary_decisions"), list)
            else []
        )
        decision_ids: list[str] = []
        for item in decisions:
            if not isinstance(item, dict):
                issues.append(
                    {
                        "code": "paragraph.boundary_decision_invalid",
                        "message": "A sentence-boundary decision is not an object.",
                    }
                )
                continue
            boundary_id = str(item.get("boundary_id") or "")
            decision_ids.append(boundary_id)
            expected = expected_by_id.get(boundary_id)
            if expected is None:
                issues.append(
                    {
                        "code": "paragraph.boundary_unknown",
                        "message": "A decision refers to an unknown sentence adjacency.",
                        "boundary_id": boundary_id,
                    }
                )
                continue
            if any(
                str(item.get(key) or "") != str(expected.get(key) or "")
                for key in ("span_id", "left_unit_id", "right_unit_id")
            ):
                issues.append(
                    {
                        "code": "paragraph.boundary_adjacency_changed",
                        "message": "A model may not change application-owned adjacency IDs.",
                        "boundary_id": boundary_id,
                    }
                )
            if str(item.get("decision") or "") not in _BOUNDARY_DECISIONS:
                issues.append(
                    {
                        "code": "paragraph.boundary_decision_unknown",
                        "message": "A boundary decision must be continue, break, or unresolved.",
                        "boundary_id": boundary_id,
                    }
                )
            if str(item.get("relation") or "") not in _DISCOURSE_RELATIONS:
                issues.append(
                    {
                        "code": "paragraph.boundary_relation_unknown",
                        "message": "A sentence adjacency has an unknown discourse relation.",
                        "boundary_id": boundary_id,
                    }
                )
            if not str(item.get("reason") or "").strip():
                issues.append(
                    {
                        "code": "paragraph.boundary_reason_missing",
                        "message": "Every sentence adjacency requires a semantic warrant.",
                        "boundary_id": boundary_id,
                    }
                )
            raw_confidence = item.get("confidence")
            try:
                confidence = -1 if isinstance(raw_confidence, bool) else float(raw_confidence)
            except (TypeError, ValueError):
                confidence = -1
            if not math.isfinite(confidence) or not 0 <= confidence <= 1:
                issues.append(
                    {
                        "code": "paragraph.boundary_confidence_invalid",
                        "message": "Boundary confidence must be finite and between zero and one.",
                        "boundary_id": boundary_id,
                    }
                )
        if decision_ids != list(expected_by_id):
            issues.append(
                {
                    "code": "paragraph.boundary_decision_coverage",
                    "message": (
                        "Every editable sentence adjacency must be decided exactly once in "
                        "source order."
                    ),
                }
            )
        return {
            "valid": not issues,
            "issues": issues,
            "unit_count": len(expected_units),
            "boundary_count": len(expected_boundaries),
            "unresolved_count": sum(
                isinstance(item, dict) and item.get("decision") == "unresolved"
                for item in decisions
            ),
        }

    @classmethod
    def compose_locked_paragraph_plan(
        cls,
        structure: dict[str, Any],
        analysis: dict[str, Any],
        *,
        source_sha256: str,
        source_boundary_provenance: str,
    ) -> dict[str, Any]:
        """Commit one immutable boundary decision per source sentence adjacency."""

        validation = cls.validate_boundary_analysis(structure, analysis)
        if not validation["valid"]:
            raise ValueError("Cannot lock an invalid sentence-boundary analysis")
        annotations = {
            str(item["unit_id"]): dict(item)
            for item in analysis.get("unit_annotations", [])
            if isinstance(item, dict)
        }
        raw_decisions = {
            str(item["boundary_id"]): dict(item)
            for item in analysis.get("boundary_decisions", [])
            if isinstance(item, dict)
        }
        inventory = cls.boundary_inventory(structure)
        locked: list[dict[str, Any]] = []
        unresolved: list[str] = []
        for boundary in inventory:
            boundary_id = str(boundary["boundary_id"])
            raw = raw_decisions[boundary_id]
            requested = str(raw.get("decision") or "unresolved")
            if requested == "unresolved":
                effective = "break" if boundary["source_boundary"] else "continue"
                lock_status = "locked_source_fallback"
                unresolved.append(
                    f"{boundary_id}: semantic boundary unresolved; preserved the "
                    f"{source_boundary_provenance} source boundary"
                )
            else:
                effective = requested
                lock_status = "locked_model_decision"
            locked.append(
                {
                    **boundary,
                    "decision": requested,
                    "requested_decision": requested,
                    "effective_decision": effective,
                    "relation": str(raw.get("relation") or "elaborates"),
                    "reason": str(raw.get("reason") or "").strip(),
                    "confidence": float(raw.get("confidence") or 0),
                    "lock_status": lock_status,
                }
            )

        decision_by_left = {
            (str(item["span_id"]), str(item["left_unit_id"])): item for item in locked
        }
        paragraphs: list[dict[str, Any]] = []
        for span in structure.get("spans", []):
            span_id = str(span.get("span_id") or "")
            units = [dict(item) for item in span.get("units", []) if isinstance(item, dict)]
            groups: list[list[dict[str, Any]]] = []
            current: list[dict[str, Any]] = []
            for index, unit in enumerate(units):
                current.append(unit)
                if index + 1 == len(units):
                    groups.append(current)
                    break
                decision = decision_by_left[(span_id, str(unit["unit_id"]))]
                if decision["effective_decision"] == "break":
                    groups.append(current)
                    current = []
            for group_index, group in enumerate(groups):
                unit_ids = [str(item["unit_id"]) for item in group]
                group_annotations = [annotations[item] for item in unit_ids]
                claim_indexes = [
                    index
                    for index, item in enumerate(group_annotations)
                    if item.get("discourse_role") in {"claim", "conclusion"}
                ]
                central_index = claim_indexes[0] if claim_indexes else 0
                if len(claim_indexes) > 1:
                    central_position = "distributed"
                elif not claim_indexes:
                    central_position = "implicit"
                elif central_index == 0:
                    central_position = "initial"
                elif central_index == len(group) - 1:
                    central_position = "final"
                else:
                    central_position = "medial"
                internal_edges = [decision_by_left[(span_id, left)] for left in unit_ids[:-1]]
                prior_edge = None
                if group_index:
                    prior_left = str(groups[group_index - 1][-1]["unit_id"])
                    prior_edge = decision_by_left[(span_id, prior_left)]
                entry_relation = cls._paragraph_entry_relation(
                    str(prior_edge.get("relation") or "") if prior_edge else "",
                    first=group_index == 0,
                )
                digest = hashlib.sha256("|".join(unit_ids).encode()).hexdigest()[:20]
                central_annotation = group_annotations[central_index]
                entities = list(
                    dict.fromkeys(
                        str(entity).strip()
                        for item in group_annotations
                        for entity in item.get("topic_entities", [])
                        if str(entity).strip()
                    )
                )
                temporal_scopes = list(
                    dict.fromkeys(
                        str(item.get("temporal_scope") or "").strip()
                        for item in group_annotations
                        if str(item.get("temporal_scope") or "").strip()
                    )
                )
                paragraphs.append(
                    {
                        "paragraph_id": f"semantic-paragraph:{digest}",
                        "span_id": span_id,
                        "source_unit_ids": unit_ids,
                        "governing_topic": str(
                            central_annotation.get("local_purpose") or ""
                        ).strip(),
                        "central_proposition": str(
                            central_annotation.get("governing_proposition") or ""
                        ).strip(),
                        "central_position": central_position,
                        "rhetorical_chain": [
                            {
                                "source_unit_id": str(edge["left_unit_id"]),
                                "target_unit_id": str(edge["right_unit_id"]),
                                "relation": str(edge["relation"]),
                                "explanation": str(edge["reason"]),
                            }
                            for edge in internal_edges
                        ],
                        "topic_entities": entities,
                        "temporal_scope": " / ".join(temporal_scopes) or None,
                        "entry_relation": entry_relation,
                        "boundary_reason": (
                            str(prior_edge.get("reason") or "")
                            if prior_edge
                            else "Start of an application-owned recomposable span."
                        ),
                        "boundary_confidence": (
                            float(prior_edge.get("confidence") or 0) if prior_edge else 1.0
                        ),
                    }
                )

        vector = "".join("1" if item["effective_decision"] == "break" else "0" for item in locked)
        vector_payload = [[item["boundary_id"], item["effective_decision"]] for item in locked]
        vector_sha256 = hashlib.sha256(
            json.dumps(vector_payload, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()
        span_boundary_counts = [
            max(
                0,
                len([unit for unit in span.get("units", []) if isinstance(unit, dict)]) - 1,
            )
            for span in structure.get("spans", [])
            if isinstance(span, dict)
        ]
        partition_signature_sha256 = hashlib.sha256(
            json.dumps(
                {
                    "span_boundary_counts": span_boundary_counts,
                    "boundary_vector": vector,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return {
            "policy": "one_shot_locked_discourse_boundaries_v1",
            "unit_annotations": list(annotations.values()),
            "boundary_decisions": locked,
            "paragraphs": paragraphs,
            "unresolved_boundaries": unresolved,
            "boundary_contract": {
                "version": "1",
                "lock_status": "locked",
                "source_sha256": source_sha256,
                "source_boundary_provenance": source_boundary_provenance,
                "boundary_count": len(locked),
                "boundary_vector": vector,
                "boundary_vector_sha256": vector_sha256,
                "span_boundary_counts": span_boundary_counts,
                "partition_signature_sha256": partition_signature_sha256,
                "unresolved_count": len(unresolved),
                "automatic_decisions_per_boundary": 1,
            },
        }

    @staticmethod
    def _paragraph_entry_relation(relation: str, *, first: bool) -> str:
        if first:
            return "section_opening"
        return {
            "elaborates": "elaboration",
            "supports": "evidence",
            "exemplifies": "example",
            "contrasts": "contrast",
            "qualifies": "qualification",
            "results": "result",
            "concludes": "result",
            "transitions": "transition",
        }.get(relation, "continuation")

    @classmethod
    def validate_locked_paragraph_plan(
        cls,
        structure: dict[str, Any],
        plan: dict[str, Any],
        *,
        source_sha256: str | None = None,
    ) -> dict[str, Any]:
        """Verify that paragraph rendering is the exact projection of locked edges."""

        issues: list[dict[str, Any]] = []
        analysis_report = cls.validate_boundary_analysis(
            structure,
            {
                "unit_annotations": plan.get("unit_annotations", []),
                "boundary_decisions": plan.get("boundary_decisions", []),
            },
        )
        issues.extend(analysis_report["issues"])
        contract = (
            dict(plan.get("boundary_contract") or {})
            if isinstance(plan.get("boundary_contract"), dict)
            else {}
        )
        if contract.get("lock_status") != "locked":
            issues.append(
                {
                    "code": "paragraph.boundary_contract_unlocked",
                    "message": "The sentence-boundary vector was not locked.",
                }
            )
        if contract.get("version") != "1":
            issues.append(
                {
                    "code": "paragraph.boundary_contract_version_invalid",
                    "message": "The sentence-boundary contract version is unsupported.",
                }
            )
        if contract.get("source_boundary_provenance") not in {
            "approved_revision",
            "unapproved_machine_proposal",
        }:
            issues.append(
                {
                    "code": "paragraph.boundary_provenance_invalid",
                    "message": "The source paragraph-boundary provenance is missing.",
                }
            )
        if contract.get("automatic_decisions_per_boundary") != 1:
            issues.append(
                {
                    "code": "paragraph.boundary_decision_count_invalid",
                    "message": "Each sentence adjacency permits one automatic decision.",
                }
            )
        if source_sha256 is not None and contract.get("source_sha256") != source_sha256:
            issues.append(
                {
                    "code": "paragraph.source_snapshot_changed",
                    "message": "The ordered source changed after boundary annotation.",
                }
            )
        inventory = cls.boundary_inventory(structure)
        decisions = [item for item in plan.get("boundary_decisions", []) if isinstance(item, dict)]
        decision_by_id = {str(item.get("boundary_id") or ""): item for item in decisions}
        effective: list[str] = []
        for boundary in inventory:
            item = decision_by_id.get(str(boundary["boundary_id"])) or {}
            value = str(item.get("effective_decision") or "")
            effective.append(value)
            if value not in {"continue", "break"}:
                issues.append(
                    {
                        "code": "paragraph.effective_boundary_invalid",
                        "message": "A locked boundary lacks a concrete effective decision.",
                        "boundary_id": boundary["boundary_id"],
                    }
                )
            if str(item.get("lock_status") or "") not in {
                "locked_model_decision",
                "locked_source_fallback",
            }:
                issues.append(
                    {
                        "code": "paragraph.boundary_status_invalid",
                        "message": "A boundary was not committed to a terminal lock state.",
                        "boundary_id": boundary["boundary_id"],
                    }
                )
        vector = "".join("1" if item == "break" else "0" for item in effective)
        vector_payload = [
            [boundary["boundary_id"], value]
            for boundary, value in zip(inventory, effective, strict=True)
        ]
        vector_sha256 = hashlib.sha256(
            json.dumps(vector_payload, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()
        span_boundary_counts = [
            max(
                0,
                len([unit for unit in span.get("units", []) if isinstance(unit, dict)]) - 1,
            )
            for span in structure.get("spans", [])
            if isinstance(span, dict)
        ]
        partition_signature_sha256 = hashlib.sha256(
            json.dumps(
                {
                    "span_boundary_counts": span_boundary_counts,
                    "boundary_vector": vector,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        try:
            recorded_boundary_count = int(contract.get("boundary_count"))
        except (TypeError, ValueError):
            recorded_boundary_count = -1
        try:
            recorded_unresolved_count = int(contract.get("unresolved_count"))
        except (TypeError, ValueError):
            recorded_unresolved_count = -1
        unresolved_count = len(cls._strings(plan.get("unresolved_boundaries")))
        if (
            recorded_boundary_count != len(inventory)
            or contract.get("boundary_vector") != vector
            or contract.get("boundary_vector_sha256") != vector_sha256
            or contract.get("span_boundary_counts") != span_boundary_counts
            or contract.get("partition_signature_sha256") != partition_signature_sha256
        ):
            issues.append(
                {
                    "code": "paragraph.boundary_vector_mismatch",
                    "message": "The canonical boundary vector does not match its lock record.",
                }
            )
        if recorded_unresolved_count != unresolved_count:
            issues.append(
                {
                    "code": "paragraph.unresolved_count_mismatch",
                    "message": "The unresolved-boundary count differs from its lock record.",
                }
            )

        paragraph_by_unit: dict[str, str] = {}
        for paragraph in plan.get("paragraphs", []):
            if not isinstance(paragraph, dict):
                continue
            paragraph_id = str(paragraph.get("paragraph_id") or "")
            for unit_id in cls._strings(paragraph.get("source_unit_ids")):
                paragraph_by_unit[unit_id] = paragraph_id
        for boundary, decision in zip(inventory, effective, strict=True):
            rendered_break = paragraph_by_unit.get(
                str(boundary["left_unit_id"])
            ) != paragraph_by_unit.get(str(boundary["right_unit_id"]))
            if rendered_break != (decision == "break"):
                issues.append(
                    {
                        "code": "paragraph.boundary_projection_mismatch",
                        "message": "Paragraph grouping differs from the locked sentence edge.",
                        "boundary_id": boundary["boundary_id"],
                    }
                )
        return {
            "valid": not issues,
            "issues": issues,
            "boundary_count": len(inventory),
            "boundary_vector_sha256": vector_sha256,
            "unresolved_count": unresolved_count,
        }

    @classmethod
    def rendered_boundary_lock(
        cls,
        canonical_html: str,
        plan: dict[str, Any],
    ) -> dict[str, Any]:
        """Snapshot application-owned block order after paragraph realization."""

        root = ET.fromstring(canonical_html)
        sequence = [
            [element.tag, str(element.attrib.get("id") or "")]
            for element in root.iter()
            if element.tag in _LOCKED_BLOCK_TAGS and element.attrib.get("id")
        ]
        sequence_sha256 = hashlib.sha256(
            json.dumps(sequence, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()
        contract = dict(plan.get("boundary_contract") or {})
        return {
            "version": "1",
            "lock_status": "locked",
            "boundary_vector_sha256": contract.get("boundary_vector_sha256"),
            "structural_sequence": sequence,
            "structural_sequence_sha256": sequence_sha256,
        }

    @classmethod
    def validate_rendered_boundary_lock(
        cls, canonical_html: str, lock: dict[str, Any]
    ) -> dict[str, Any]:
        current = cls.rendered_boundary_lock(
            canonical_html,
            {"boundary_contract": {"boundary_vector_sha256": lock.get("boundary_vector_sha256")}},
        )
        issues: list[dict[str, Any]] = []
        if lock.get("lock_status") != "locked":
            issues.append(
                {
                    "code": "paragraph.rendered_lock_missing",
                    "message": "The rendered paragraph boundary lock is missing.",
                }
            )
        if current["structural_sequence_sha256"] != lock.get(
            "structural_sequence_sha256"
        ) or current["structural_sequence"] != lock.get("structural_sequence"):
            issues.append(
                {
                    "code": "paragraph.rendered_order_changed",
                    "message": (
                        "A downstream stage changed block order after semantic boundaries "
                        "were locked."
                    ),
                }
            )
        return {"valid": not issues, "issues": issues, "current": current}

    @classmethod
    def paragraph_regularity_diagnostics(cls, plan: dict[str, Any]) -> dict[str, Any]:
        """Observe planner regularity without using it as a rewrite objective."""

        counts = [
            len(cls._strings(item.get("source_unit_ids")))
            for item in plan.get("paragraphs", [])
            if isinstance(item, dict)
        ]
        middle_band = sum(3 <= count <= 6 for count in counts)
        longest_same_run = 0
        longest_singleton_run = 0
        current_singleton_run = 0
        current_run = 0
        previous: int | None = None
        for count in counts:
            if count == previous:
                current_run += 1
            else:
                current_run = 1
                previous = count
            longest_same_run = max(longest_same_run, current_run)
            if count == 1:
                current_singleton_run += 1
                longest_singleton_run = max(longest_singleton_run, current_singleton_run)
            else:
                current_singleton_run = 0
        ratio = middle_band / len(counts) if counts else 0.0
        observations: list[str] = []
        if len(counts) >= 12 and ratio >= 0.75:
            observations.append("three_to_six_unit_concentration")
        if longest_same_run >= 5:
            observations.append("repeated_identical_unit_count")
        if longest_singleton_run >= 3:
            observations.append("consecutive_single_unit_paragraphs")
        return {
            "advisory_only": True,
            "fed_back_to_planner": False,
            "paragraph_count": len(counts),
            "three_to_six_unit_ratio": ratio,
            "longest_identical_unit_count_run": longest_same_run,
            "longest_single_unit_paragraph_run": longest_singleton_run,
            "suspected_regularization": bool(observations),
            "observations": observations,
        }

    @staticmethod
    def split_sentences(text: str) -> list[str]:
        """Conservatively split Japanese prose while keeping quoted punctuation intact."""

        normalized = text.strip()
        if not normalized:
            return []
        sentences: list[str] = []
        buffer: list[str] = []
        quote_stack: list[str] = []
        for character in normalized:
            buffer.append(character)
            if character in _QUOTE_PAIRS:
                quote_stack.append(_QUOTE_PAIRS[character])
            elif quote_stack and character == quote_stack[-1]:
                quote_stack.pop()
            if character in _SENTENCE_ENDINGS and not quote_stack:
                sentence = "".join(buffer).strip()
                if sentence:
                    sentences.append(sentence)
                buffer = []
        remainder = "".join(buffer).strip()
        if remainder:
            if sentences and remainder in {"」", "』", "）", ")", "］", "]"}:
                sentences[-1] += remainder
            else:
                sentences.append(remainder)
        return sentences or [normalized]

    @classmethod
    def default_paragraph_plan(cls, structure: dict[str, Any]) -> dict[str, Any]:
        paragraphs: list[dict[str, Any]] = []
        for span in structure.get("spans", []):
            by_block: dict[str, list[str]] = {}
            for unit in span.get("units", []):
                by_block.setdefault(str(unit["source_block_id"]), []).append(str(unit["unit_id"]))
            for ordinal, block_id in enumerate(span.get("source_block_ids", [])):
                unit_ids = by_block[str(block_id)]
                paragraphs.append(
                    {
                        "paragraph_id": f"paragraph:{unit_ids[0]}",
                        "span_id": str(span["span_id"]),
                        "source_unit_ids": unit_ids,
                        "governing_topic": "Preserve the source paragraph topic.",
                        "central_proposition": "Preserve the source paragraph claim.",
                        "central_position": "distributed",
                        "rhetorical_chain": [
                            {
                                "source_unit_id": left,
                                "target_unit_id": right,
                                "relation": "elaborates",
                                "explanation": (
                                    "Fallback preserves the reviewed source adjacency; "
                                    "it does not authorize semantic rewriting."
                                ),
                            }
                            for left, right in zip(unit_ids, unit_ids[1:], strict=False)
                        ],
                        "topic_entities": [],
                        "temporal_scope": None,
                        "entry_relation": ("section_opening" if ordinal == 0 else "continuation"),
                        "boundary_reason": "Preserve the reviewed source boundary.",
                        "boundary_confidence": 1.0,
                    }
                )
        return {
            "policy": "semantic_coherence_not_fixed_length",
            "paragraphs": paragraphs,
            "unresolved_boundaries": [],
        }

    @classmethod
    def validate_paragraph_plan(
        cls, structure: dict[str, Any], plan: dict[str, Any]
    ) -> ParagraphPlanReport:
        issues: list[ParagraphPlanIssue] = []
        spans = {
            str(span["span_id"]): span
            for span in structure.get("spans", [])
            if isinstance(span, dict) and span.get("span_id")
        }
        expected = {
            span_id: [str(unit["unit_id"]) for unit in span.get("units", [])]
            for span_id, span in spans.items()
        }
        paragraphs = plan.get("paragraphs") if isinstance(plan.get("paragraphs"), list) else []
        seen_paragraph_ids: set[str] = set()
        planned_by_span: dict[str, list[str]] = {span_id: [] for span_id in spans}
        for raw in paragraphs:
            if not isinstance(raw, dict):
                issues.append(
                    ParagraphPlanIssue(
                        "paragraph.invalid_record", "A paragraph plan record is not an object."
                    )
                )
                continue
            paragraph_id = str(raw.get("paragraph_id") or "")
            span_id = str(raw.get("span_id") or "")
            unit_ids = cls._strings(raw.get("source_unit_ids"))
            if not paragraph_id or paragraph_id in seen_paragraph_ids:
                issues.append(
                    ParagraphPlanIssue(
                        "paragraph.duplicate_id",
                        "Every semantic paragraph requires one unique plan id.",
                        paragraph_id=paragraph_id or None,
                        span_id=span_id or None,
                    )
                )
            seen_paragraph_ids.add(paragraph_id)
            if span_id not in spans:
                issues.append(
                    ParagraphPlanIssue(
                        "paragraph.unknown_span",
                        "A semantic paragraph refers to an unknown recomposable span.",
                        paragraph_id=paragraph_id or None,
                        span_id=span_id or None,
                    )
                )
                continue
            if not unit_ids:
                issues.append(
                    ParagraphPlanIssue(
                        "paragraph.empty",
                        "A semantic paragraph must contain at least one discourse unit.",
                        paragraph_id=paragraph_id,
                        span_id=span_id,
                    )
                )
            planned_by_span[span_id].extend(unit_ids)
            if any(unit_id not in expected[span_id] for unit_id in unit_ids):
                issues.append(
                    ParagraphPlanIssue(
                        "paragraph.cross_span_unit",
                        "A paragraph may contain only units from its contiguous HTML span.",
                        paragraph_id=paragraph_id,
                        span_id=span_id,
                    )
                )
            if len(unit_ids) != len(set(unit_ids)):
                issues.append(
                    ParagraphPlanIssue(
                        "paragraph.duplicate_unit",
                        "A paragraph repeats a discourse unit.",
                        paragraph_id=paragraph_id,
                        span_id=span_id,
                    )
                )
            if str(raw.get("central_position") or "") not in _CENTRAL_POSITIONS:
                issues.append(
                    ParagraphPlanIssue(
                        "paragraph.central_position",
                        "The central proposition position is invalid.",
                        paragraph_id=paragraph_id,
                        span_id=span_id,
                    )
                )
            if str(raw.get("entry_relation") or "") not in _ENTRY_RELATIONS:
                issues.append(
                    ParagraphPlanIssue(
                        "paragraph.entry_relation",
                        "The paragraph entry relation is invalid.",
                        paragraph_id=paragraph_id,
                        span_id=span_id,
                    )
                )
            for key in ("governing_topic", "central_proposition", "boundary_reason"):
                if not str(raw.get(key) or "").strip():
                    issues.append(
                        ParagraphPlanIssue(
                            f"paragraph.{key}_missing",
                            f"A semantic paragraph requires {key}.",
                            paragraph_id=paragraph_id,
                            span_id=span_id,
                        )
                    )
            try:
                confidence = float(raw.get("boundary_confidence"))
            except (TypeError, ValueError):
                confidence = -1
            if not 0 <= confidence <= 1:
                issues.append(
                    ParagraphPlanIssue(
                        "paragraph.boundary_confidence",
                        "Boundary confidence must be between zero and one.",
                        paragraph_id=paragraph_id,
                        span_id=span_id,
                    )
                )
            raw_chain = raw.get("rhetorical_chain")
            rhetorical_chain = raw_chain if isinstance(raw_chain, list) else []
            if not isinstance(raw_chain, list):
                issues.append(
                    ParagraphPlanIssue(
                        "paragraph.rhetorical_chain_invalid",
                        "The rhetorical chain must be a list of explicit relations.",
                        paragraph_id=paragraph_id,
                        span_id=span_id,
                    )
                )
            relation_edges: list[tuple[str, str]] = []
            for relation in rhetorical_chain:
                if not isinstance(relation, dict):
                    issues.append(
                        ParagraphPlanIssue(
                            "paragraph.rhetorical_relation_invalid",
                            "A rhetorical relation must be an object.",
                            paragraph_id=paragraph_id,
                            span_id=span_id,
                        )
                    )
                    continue
                if str(relation.get("relation") or "") not in _DISCOURSE_RELATIONS:
                    issues.append(
                        ParagraphPlanIssue(
                            "paragraph.rhetorical_relation",
                            "A paragraph contains an unknown rhetorical relation.",
                            paragraph_id=paragraph_id,
                            span_id=span_id,
                        )
                    )
                relation_units = {
                    str(relation.get("source_unit_id") or ""),
                    str(relation.get("target_unit_id") or ""),
                }
                source_unit_id = str(relation.get("source_unit_id") or "")
                target_unit_id = str(relation.get("target_unit_id") or "")
                if not source_unit_id or source_unit_id == target_unit_id:
                    issues.append(
                        ParagraphPlanIssue(
                            "paragraph.rhetorical_self_or_missing_link",
                            "A rhetorical relation must connect two distinct units.",
                            paragraph_id=paragraph_id,
                            span_id=span_id,
                        )
                    )
                else:
                    relation_edges.append((source_unit_id, target_unit_id))
                if not relation_units.issubset(set(unit_ids)):
                    issues.append(
                        ParagraphPlanIssue(
                            "paragraph.rhetorical_unit_outside_paragraph",
                            "Rhetorical links must connect units in the same paragraph.",
                            paragraph_id=paragraph_id,
                            span_id=span_id,
                        )
                    )
                if not str(relation.get("explanation") or "").strip():
                    issues.append(
                        ParagraphPlanIssue(
                            "paragraph.rhetorical_explanation_missing",
                            "Each rhetorical relation requires an explicit warrant.",
                            paragraph_id=paragraph_id,
                            span_id=span_id,
                        )
                    )
            if len(relation_edges) != len(set(relation_edges)):
                issues.append(
                    ParagraphPlanIssue(
                        "paragraph.rhetorical_link_duplicated",
                        "A rhetorical relation pair was repeated.",
                        paragraph_id=paragraph_id,
                        span_id=span_id,
                    )
                )
            if len(unit_ids) > 1:
                graph = {unit_id: set() for unit_id in unit_ids}
                for source_unit_id, target_unit_id in set(relation_edges):
                    if source_unit_id in graph and target_unit_id in graph:
                        graph[source_unit_id].add(target_unit_id)
                        graph[target_unit_id].add(source_unit_id)
                reached = {unit_ids[0]}
                frontier = [unit_ids[0]]
                while frontier:
                    current = frontier.pop()
                    for neighbor in graph[current] - reached:
                        reached.add(neighbor)
                        frontier.append(neighbor)
                if reached != set(unit_ids):
                    issues.append(
                        ParagraphPlanIssue(
                            "paragraph.rhetorical_chain_disconnected",
                            (
                                "Every sentence unit in a composed paragraph must be "
                                "connected by an explicit rhetorical dependency."
                            ),
                            paragraph_id=paragraph_id,
                            span_id=span_id,
                        )
                    )
        for span_id, expected_order in expected.items():
            planned_order = planned_by_span.get(span_id, [])
            if planned_order != expected_order:
                issues.append(
                    ParagraphPlanIssue(
                        "paragraph.incomplete_or_reordered_span",
                        "Every discourse unit must occur once in source order within its span.",
                        span_id=span_id,
                    )
                )
        return ParagraphPlanReport(
            tuple(issues),
            sum(len(unit_ids) for unit_ids in expected.values()),
            len([item for item in paragraphs if isinstance(item, dict)]),
            int(structure.get("source_paragraph_count") or 0),
        )

    @classmethod
    def validate_global_paragraph_boundaries(cls, plan: dict[str, Any]) -> dict[str, Any]:
        """Revalidate the concatenated plan, including planner-batch seams."""

        paragraphs = [item for item in plan.get("paragraphs", []) if isinstance(item, dict)]
        issues: list[dict[str, Any]] = []
        for boundary in cls._strings(plan.get("unresolved_boundaries")):
            issues.append(
                {
                    "code": "paragraph.unresolved_boundary",
                    "severity": "blocking",
                    "message": boundary,
                    "paragraph_ids": [],
                }
            )
        for previous, current in zip(paragraphs, paragraphs[1:], strict=False):
            previous_batch = previous.get("planner_batch_index")
            current_batch = current.get("planner_batch_index")
            cross_batch = (
                previous_batch is not None
                and current_batch is not None
                and int(previous_batch) != int(current_batch)
            )
            code_scope = "cross_batch" if cross_batch else "global"
            paragraph_ids = [
                str(previous.get("paragraph_id") or ""),
                str(current.get("paragraph_id") or ""),
            ]
            reason = str(current.get("boundary_reason") or "").strip()
            try:
                confidence = float(current.get("boundary_confidence") or 0)
            except (TypeError, ValueError):
                confidence = 0
            if confidence < 0.7:
                issues.append(
                    {
                        "code": f"paragraph.{code_scope}_low_confidence",
                        "severity": "blocking",
                        "message": (
                            "A paragraph boundary was not justified with sufficient confidence."
                        ),
                        "paragraph_ids": paragraph_ids,
                    }
                )
            if len(reason) < 12 or reason.casefold() in {
                "preserve the reviewed source boundary.",
                "source boundary.",
            }:
                issues.append(
                    {
                        "code": f"paragraph.{code_scope}_reason_missing",
                        "severity": "blocking",
                        "message": (
                            "A paragraph boundary requires a manuscript-level "
                            "warrant, not a mechanical source-boundary statement."
                        ),
                        "paragraph_ids": paragraph_ids,
                    }
                )

            entry_relation = str(current.get("entry_relation") or "")
            continuation = entry_relation in {
                "continuation",
                "elaboration",
                "evidence",
                "example",
                "qualification",
                "result",
            }
            previous_entities = {
                str(item).casefold()
                for item in previous.get("topic_entities", [])
                if str(item).strip()
            }
            current_entities = {
                str(item).casefold()
                for item in current.get("topic_entities", [])
                if str(item).strip()
            }
            if continuation and not previous_entities.intersection(current_entities):
                issues.append(
                    {
                        "code": f"paragraph.{code_scope}_entity_chain_gap",
                        "severity": "blocking",
                        "message": (
                            "A continuation-like boundary has no shared topic entity; "
                            "the relation may be a concealed topic shift."
                        ),
                        "paragraph_ids": paragraph_ids,
                    }
                )

            previous_years = cls._years(str(previous.get("temporal_scope") or ""))
            current_years = cls._years(str(current.get("temporal_scope") or ""))
            if (
                continuation
                and previous_years
                and current_years
                and min(current_years) < max(previous_years)
                and not re.search(
                    r"遡|回顧|比較|背景|先行|retrospective|comparison|background",
                    reason,
                    re.IGNORECASE,
                )
            ):
                issues.append(
                    {
                        "code": f"paragraph.{code_scope}_temporal_conflict",
                        "severity": "blocking",
                        "message": (
                            "The plan moves backward in time across a batch seam without "
                            "an explicit retrospective, comparison, or background warrant."
                        ),
                        "paragraph_ids": paragraph_ids,
                    }
                )
        return {
            "valid": not any(item["severity"] == "blocking" for item in issues),
            "boundary_count": max(0, len(paragraphs) - 1),
            "cross_batch_boundary_count": sum(
                previous.get("planner_batch_index") != current.get("planner_batch_index")
                for previous, current in zip(paragraphs, paragraphs[1:], strict=False)
                if previous.get("planner_batch_index") is not None
                and current.get("planner_batch_index") is not None
            ),
            "issues": issues,
        }

    @staticmethod
    def _years(value: str) -> tuple[int, ...]:
        return tuple(int(item) for item in re.findall(r"(?<!\d)(?:1[5-9]|20)\d{2}(?!\d)", value))

    @classmethod
    def apply_paragraph_plan(
        cls,
        canonical_html: str,
        structure: dict[str, Any],
        plan: dict[str, Any],
    ) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
        report = cls.validate_paragraph_plan(structure, plan)
        if not report.valid:
            raise ValueError("Cannot apply an invalid semantic paragraph plan")
        root = ET.fromstring(canonical_html)
        elements = {
            str(element.attrib.get("id")): element
            for element in root.iter()
            if element.attrib.get("id")
        }
        parents = {child: parent for parent in root.iter() for child in parent}
        units = {
            str(unit["unit_id"]): unit
            for span in structure.get("spans", [])
            for unit in span.get("units", [])
        }
        paragraphs_by_span: dict[str, list[dict[str, Any]]] = {}
        for item in plan.get("paragraphs", []):
            if isinstance(item, dict):
                paragraphs_by_span.setdefault(str(item["span_id"]), []).append(item)
        existing_ids = set(elements)
        realized: list[dict[str, Any]] = []
        edits: list[dict[str, Any]] = []
        used_output_ids: set[str] = set()
        source_group_counts: dict[str, int] = {}
        for item in plan.get("paragraphs", []):
            if not isinstance(item, dict):
                continue
            for block_id in {
                str(units[unit_id]["source_block_id"])
                for unit_id in cls._strings(item.get("source_unit_ids"))
            }:
                source_group_counts[block_id] = source_group_counts.get(block_id, 0) + 1

        for span in structure.get("spans", []):
            span_id = str(span["span_id"])
            source_block_ids = cls._strings(span.get("source_block_ids"))
            source_elements = [elements[block_id] for block_id in source_block_ids]
            parent = parents[source_elements[0]]
            siblings = list(parent)
            positions = [siblings.index(element) for element in source_elements]
            if positions != list(range(positions[0], positions[0] + len(positions))):
                raise ValueError("Semantic paragraph span is no longer contiguous")
            for element in source_elements:
                parent.remove(element)
                existing_ids.discard(str(element.attrib["id"]))
            insertion_index = positions[0]
            for offset, paragraph in enumerate(paragraphs_by_span.get(span_id, [])):
                unit_ids = cls._strings(paragraph.get("source_unit_ids"))
                paragraph_units = [units[unit_id] for unit_id in unit_ids]
                group_source_ids = list(
                    dict.fromkeys(str(unit["source_block_id"]) for unit in paragraph_units)
                )
                reusable_id = next(
                    (
                        source_id
                        for source_id in group_source_ids
                        if source_id not in existing_ids and source_id not in used_output_ids
                    ),
                    group_source_ids[0],
                )
                if reusable_id in existing_ids or reusable_id in used_output_ids:
                    digest = hashlib.sha256("|".join(unit_ids).encode()).hexdigest()[:16]
                    reusable_id = f"P-SEMPARA-{digest.upper()}"
                while reusable_id in existing_ids or reusable_id in used_output_ids:
                    reusable_id += "X"
                used_output_ids.add(reusable_id)
                existing_ids.add(reusable_id)
                original_units = [
                    str(unit["unit_id"])
                    for unit in span.get("units", [])
                    if str(unit["source_block_id"]) == group_source_ids[0]
                ]
                unchanged_single = (
                    len(group_source_ids) == 1
                    and unit_ids == original_units
                    and source_group_counts[group_source_ids[0]] == 1
                )
                if unchanged_single:
                    output = copy.deepcopy(elements[group_source_ids[0]])
                    output.attrib["id"] = reusable_id
                else:
                    source_attributes = [
                        {
                            key: value
                            for key, value in elements[source_id].attrib.items()
                            if key != "id"
                        }
                        for source_id in group_source_ids
                    ]
                    common_attributes = {
                        key: value
                        for key, value in source_attributes[0].items()
                        if all(attributes.get(key) == value for attributes in source_attributes[1:])
                    }
                    source_pages = sorted(
                        {
                            int(value)
                            for attributes in source_attributes
                            if (value := attributes.get("data-source-page"))
                            and str(value).isdigit()
                        }
                    )
                    if len(source_pages) > 1:
                        common_attributes["data-source-page-start"] = str(source_pages[0])
                        common_attributes["data-source-page-end"] = str(source_pages[-1])
                    output = ET.Element("p", {"id": reusable_id, **common_attributes})
                    output.text = cls._join_sentences(
                        [str(unit["text"]) for unit in paragraph_units]
                    )
                parent.insert(insertion_index + offset, output)
                realized_item = {
                    **paragraph,
                    "output_block_id": reusable_id,
                    "source_block_ids": group_source_ids,
                    "character_count": len("".join(output.itertext()).strip()),
                }
                realized.append(realized_item)
                operation = "resegment_paragraph"
                if len(group_source_ids) > 1:
                    operation = "compose_semantic_paragraph"
                elif source_group_counts[group_source_ids[0]] > 1:
                    operation = "split_semantic_paragraph"
                if not unchanged_single:
                    edits.append(
                        {
                            "operation": operation,
                            "block_id": reusable_id,
                            "source_block_ids": group_source_ids,
                            "source_unit_ids": unit_ids,
                            "paragraph_plan_id": str(paragraph["paragraph_id"]),
                        }
                    )
        rendered = ET.tostring(root, encoding="unicode", short_empty_elements=False)
        CanonicalHTMLValidator().assert_valid(rendered)
        return rendered, realized, edits

    @classmethod
    def predicate_candidates(
        cls,
        canonical_html: str,
        *,
        realized_paragraphs: list[dict[str, Any]] | None = None,
        structure_plan: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        root = ET.fromstring(canonical_html)
        semantic_structure = ManuscriptCoherenceService.semantic_structure(canonical_html)
        argument_block_ids = {
            str(block.get("id") or "")
            for block in semantic_structure.get("blocks", [])
            if isinstance(block, dict)
            and block.get("rewritable_as_argument", True)
            and not block.get("is_heading")
        }
        paragraphs = {
            str(item.get("output_block_id")): item
            for item in realized_paragraphs or []
            if isinstance(item, dict) and item.get("output_block_id")
        }
        section_claim_by_block: dict[str, str] = {}
        for section in (structure_plan or {}).get("sections", []):
            if not isinstance(section, dict):
                continue
            for block_id in cls._strings(section.get("ordered_block_ids")):
                section_claim_by_block[block_id] = str(section.get("central_claim") or "")
        blocks = [
            {
                "block_id": str(element.attrib["id"]),
                "tag": element.tag,
                "text": "".join(element.itertext()).strip(),
                "protected": bool(list(element)),
            }
            for element in root.iter()
            if element.tag in {"p", "blockquote", "li"}
            and element.attrib.get("id")
            and str(element.attrib.get("id")) in argument_block_ids
            and "".join(element.itertext()).strip()
        ]
        result: list[dict[str, Any]] = []
        for index, block in enumerate(blocks):
            text = str(block["text"])
            if ManuscriptCoherenceService.heading(text, str(block["tag"])) is not None:
                continue
            triggers = [
                term
                for term in _GENERIC_RELATIONAL_TERMS
                if (
                    re.search(r"(?<![一-龯々])" + re.escape(term) + r"[ぁ-ん]", text)
                    if len(term) == 1
                    else term in text
                )
            ]
            lexicon_matches = list(academic_predicate_matches(text))
            connective_matches = list(academic_connective_matches(text))
            if not triggers and not lexicon_matches and not connective_matches:
                continue
            block_id = str(block["block_id"])
            paragraph = paragraphs.get(block_id) or {}
            source_block_ids = cls._strings(paragraph.get("source_block_ids"))
            section_claim = next(
                (
                    section_claim_by_block[item]
                    for item in source_block_ids
                    if item in section_claim_by_block
                ),
                "",
            )
            candidate_id = (
                "predicate:" + hashlib.sha256(f"{block_id}|{text}".encode()).hexdigest()[:20]
            )
            result.append(
                {
                    "candidate_id": candidate_id,
                    "block_id": block_id,
                    "triggers": triggers,
                    "risk_tier": (
                        "underspecified_relation"
                        if triggers
                        else "academic_connective"
                        if connective_matches
                        else "academic_predicate"
                    ),
                    "academic_predicates": lexicon_matches,
                    "academic_modalities": list(academic_modality_matches(text)),
                    "academic_connectives": connective_matches,
                    "discourse_markers": discourse_marker_matches(text),
                    "lexicon": {
                        "name": "Japanese Academic Word List",
                        "version": JAWL_VERSION,
                        "source_url": JAWL_SOURCE_URL,
                        "predicate_entry_count": len(JAWL_ACADEMIC_PREDICATES),
                    },
                    "connective_inventory": {
                        "name": "Japanese academic connective expressions",
                        "version": ACADEMIC_CONNECTIVE_VERSION,
                        "source_url": ACADEMIC_CONNECTIVE_SOURCE_URL,
                        "entry_count": len(ACADEMIC_CONNECTIVES),
                        "match_is_allow_list": False,
                    },
                    "source_text": text,
                    "inline_protected": bool(block["protected"]),
                    "trigger_sentences": [
                        sentence
                        for sentence in cls.split_sentences(text)
                        if any(term in sentence for term in triggers)
                    ],
                    "previous_text": str(blocks[index - 1]["text"])[:1200] if index else "",
                    "next_text": str(blocks[index + 1]["text"])[:1200]
                    if index + 1 < len(blocks)
                    else "",
                    "paragraph_contract": {
                        key: paragraph.get(key)
                        for key in (
                            "governing_topic",
                            "central_proposition",
                            "central_position",
                            "topic_entities",
                            "temporal_scope",
                            "entry_relation",
                        )
                    },
                    "section_claim": section_claim,
                    "abstract_likelihood": round(
                        min(
                            1.0,
                            sum(noun in text for noun in _ABSTRACT_NOUNS) / 3,
                        ),
                        2,
                    ),
                }
            )
        return result

    @classmethod
    def realized_distribution(
        cls,
        canonical_html: str,
        realized_paragraphs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Report paragraph shape without turning length into a pass/fail target."""

        root = ET.fromstring(canonical_html)
        text_by_id = {
            str(element.attrib.get("id") or ""): "".join(element.itertext()).strip()
            for element in root.iter("p")
            if element.attrib.get("id")
        }
        lengths = [
            len(text_by_id[block_id])
            for item in realized_paragraphs
            if (block_id := str(item.get("output_block_id") or "")) in text_by_id
        ]
        singleton_count = sum(
            len(cls._strings(item.get("source_unit_ids"))) == 1
            for item in realized_paragraphs
            if str(item.get("output_block_id") or "") in text_by_id
        )
        count = len(lengths)
        return {
            "paragraph_count": count,
            "minimum_characters": min(lengths) if lengths else 0,
            "maximum_characters": max(lengths) if lengths else 0,
            "median_characters": median(lengths) if lengths else 0,
            "mean_characters": mean(lengths) if lengths else 0,
            "coefficient_of_variation": (
                pstdev(lengths) / mean(lengths) if lengths and mean(lengths) else 0
            ),
            "single_unit_paragraph_count": singleton_count,
            "single_unit_paragraph_ratio": (singleton_count / count if count else 0),
        }

    @classmethod
    def assess_predicate_decisions(
        cls,
        candidates: list[dict[str, Any]],
        payload: dict[str, Any],
        *,
        minimum_confidence: float = 0.8,
    ) -> dict[str, Any]:
        by_id = {str(item["candidate_id"]): item for item in candidates}
        decisions = payload.get("decisions")
        raw_decisions = decisions if isinstance(decisions, list) else []
        seen: set[str] = set()
        safe_edits: list[dict[str, Any]] = []
        records: list[dict[str, Any]] = []
        issues: list[dict[str, Any]] = []
        for raw in raw_decisions:
            if not isinstance(raw, dict):
                continue
            candidate_id = str(raw.get("candidate_id") or "")
            candidate = by_id.get(candidate_id)
            if candidate is None or candidate_id in seen:
                issues.append(
                    {
                        "code": "predicate.unknown_or_duplicate_candidate",
                        "candidate_id": candidate_id,
                        "message": "A predicate decision was unknown or duplicated.",
                    }
                )
                continue
            seen.add(candidate_id)
            action = str(raw.get("action") or "review")
            relation_type = str(raw.get("relation_type") or "uncertain")
            try:
                confidence = float(raw.get("confidence") or 0)
            except (TypeError, ValueError):
                confidence = 0
            confidence_valid = (
                not isinstance(raw.get("confidence"), bool)
                and math.isfinite(confidence)
                and 0 <= confidence <= 1
            )
            if not confidence_valid:
                confidence = 0
            record = {
                **raw,
                "candidate_id": candidate_id,
                "block_id": str(candidate["block_id"]),
                "source_text": str(candidate["source_text"]),
                "risk_tier": str(candidate.get("risk_tier") or "academic_predicate"),
                "triggers": list(candidate.get("triggers") or []),
                "academic_predicates": list(candidate.get("academic_predicates") or []),
                "academic_connectives": list(candidate.get("academic_connectives") or []),
                "applied": False,
                "validation_issues": [],
            }
            if action == "rewrite":
                replacement = str(raw.get("replacement_text") or "").strip()
                semantic_roles = raw.get("semantic_roles")
                semantic_roles = semantic_roles if isinstance(semantic_roles, dict) else {}
                decision_issues: list[str] = []
                if not confidence_valid:
                    decision_issues.append("confidence_invalid")
                if str(raw.get("block_id") or "") != str(candidate["block_id"]):
                    decision_issues.append("block_id_mismatch")
                if relation_type not in _RELATION_TYPES or relation_type in {
                    "uncertain",
                    "literal_connection",
                }:
                    decision_issues.append("relation_type_not_resolved")
                if confidence < minimum_confidence:
                    decision_issues.append("confidence_below_auto_apply_threshold")
                if not replacement or replacement == str(candidate["source_text"]):
                    decision_issues.append("replacement_missing_or_unchanged")
                if not str(raw.get("reason") or "").strip():
                    decision_issues.append("rewrite_reason_missing")
                if not any(str(value or "").strip() for value in semantic_roles.values()):
                    decision_issues.append("semantic_roles_missing")
                decision_issues.extend(cls._semantic_role_shape_issues(candidate, semantic_roles))
                populated_roles = {
                    key for key, value in semantic_roles.items() if str(value or "").strip()
                }
                if len(populated_roles) < 2:
                    decision_issues.append("semantic_roles_incomplete")
                grounding = cls._predicate_role_grounding(candidate, semantic_roles)
                record["semantic_role_grounding"] = {
                    "grounded_roles": sorted(grounding["grounded_roles"]),
                    "role_anchors": grounding["role_anchors"],
                }
                if "subject" not in grounding["grounded_roles"]:
                    decision_issues.append("semantic_subject_not_grounded")
                if not {
                    "affected_entity",
                    "initial_state",
                    "target_state",
                    "mediator",
                }.intersection(grounding["grounded_roles"]):
                    decision_issues.append("semantic_counterpart_not_grounded")
                if candidate.get("inline_protected"):
                    decision_issues.append("inline_markup_requires_human_review")
                for flag in (
                    "meaning_preserved",
                    "modality_preserved",
                    "citation_preserved",
                ):
                    if raw.get(flag) is not True:
                        decision_issues.append(f"{flag}_not_confirmed")
                if replacement and replacement != str(candidate["source_text"]):
                    invariant_issues = ProofreadingInvariantValidator.validate(
                        str(candidate["source_text"]),
                        replacement,
                        (
                            ProofreadingEdit(
                                str(candidate["source_text"]),
                                replacement,
                                str(raw.get("reason") or "Contextual predicate resolution."),
                                "collocation",
                            ),
                        ),
                    )
                    decision_issues.extend(issue.code for issue in invariant_issues)
                if not decision_issues:
                    safe_edits.append(
                        {
                            "operation": "replace_text",
                            "block_id": str(candidate["block_id"]),
                            "text": replacement,
                            "review_finding_ids": cls._strings(raw.get("review_finding_ids")),
                            "evidence_ids": cls._strings(raw.get("evidence_ids")),
                            "predicate_candidate_id": candidate_id,
                            "predicate_relation_type": relation_type,
                        }
                    )
                    record["applied"] = True
                else:
                    record["action"] = "review"
                    record["validation_issues"] = list(dict.fromkeys(decision_issues))
                    issues.append(
                        {
                            "code": "predicate.rewrite_not_safe",
                            "candidate_id": candidate_id,
                            "block_id": str(candidate["block_id"]),
                            "message": "The semantic rewrite was retained for human review.",
                            "details": record["validation_issues"],
                        }
                    )
            elif action == "preserve":
                semantic_roles = raw.get("semantic_roles")
                semantic_roles = semantic_roles if isinstance(semantic_roles, dict) else {}
                populated_roles = {
                    key for key, value in semantic_roles.items() if str(value or "").strip()
                }
                decision_issues: list[str] = []
                if not confidence_valid:
                    decision_issues.append("confidence_invalid")
                if confidence < minimum_confidence:
                    decision_issues.append("confidence_below_review_threshold")
                if relation_type not in _RELATION_TYPES or relation_type == "uncertain":
                    decision_issues.append("relation_type_not_resolved")
                if (
                    relation_type == "literal_connection"
                    and str(candidate.get("risk_tier")) == "underspecified_relation"
                    and float(candidate.get("abstract_likelihood") or 0) > 0
                ):
                    decision_issues.append("abstract_relation_mislabeled_as_literal")
                if str(raw.get("block_id") or "") != str(candidate["block_id"]):
                    decision_issues.append("block_id_mismatch")
                if not str(raw.get("reason") or "").strip():
                    decision_issues.append("preservation_reason_missing")
                decision_issues.extend(cls._semantic_role_shape_issues(candidate, semantic_roles))
                for flag in (
                    "meaning_preserved",
                    "modality_preserved",
                    "citation_preserved",
                ):
                    if raw.get(flag) is not True:
                        decision_issues.append(f"{flag}_not_confirmed")
                required_roles = (
                    2 if str(candidate.get("risk_tier")) == "underspecified_relation" else 1
                )
                if len(populated_roles) < required_roles:
                    decision_issues.append("semantic_roles_incomplete")
                grounding = cls._predicate_role_grounding(candidate, semantic_roles)
                record["semantic_role_grounding"] = {
                    "grounded_roles": sorted(grounding["grounded_roles"]),
                    "role_anchors": grounding["role_anchors"],
                }
                required_grounded = (
                    2 if str(candidate.get("risk_tier")) == "underspecified_relation" else 1
                )
                if len(grounding["grounded_roles"]) < required_grounded:
                    decision_issues.append("semantic_roles_not_grounded")
                if "subject" not in grounding["grounded_roles"]:
                    decision_issues.append("semantic_subject_not_grounded")
                if str(candidate.get("risk_tier")) == "underspecified_relation" and not {
                    "affected_entity",
                    "initial_state",
                    "target_state",
                    "mediator",
                }.intersection(grounding["grounded_roles"]):
                    decision_issues.append("semantic_counterpart_not_grounded")
                if decision_issues:
                    record["action"] = "review"
                    record["validation_issues"] = list(dict.fromkeys(decision_issues))
                    issues.append(
                        {
                            "code": "predicate.preservation_not_demonstrated",
                            "candidate_id": candidate_id,
                            "block_id": str(candidate["block_id"]),
                            "message": (
                                "The model did not demonstrate that preserving this "
                                "predicate is contextually safe."
                            ),
                            "details": record["validation_issues"],
                        }
                    )
            elif action not in {"preserve", "review"}:
                record["action"] = "review"
                record["validation_issues"] = ["unknown_action"]
            records.append(record)
        for candidate_id, candidate in by_id.items():
            if candidate_id in seen:
                continue
            records.append(
                {
                    "candidate_id": candidate_id,
                    "block_id": str(candidate["block_id"]),
                    "source_text": str(candidate["source_text"]),
                    "action": "review",
                    "relation_type": "uncertain",
                    "replacement_text": "",
                    "reason": "No complete decision was returned.",
                    "confidence": 0,
                    "applied": False,
                    "validation_issues": ["decision_missing"],
                }
            )
            issues.append(
                {
                    "code": "predicate.decision_missing",
                    "candidate_id": candidate_id,
                    "block_id": str(candidate["block_id"]),
                    "message": "A generic relational predicate requires human review.",
                }
            )
        return {
            "lexicon_name": "Japanese Academic Word List",
            "lexicon_version": JAWL_VERSION,
            "lexicon_entry_count": len(JAWL_ACADEMIC_PREDICATES),
            "connective_inventory_version": ACADEMIC_CONNECTIVE_VERSION,
            "connective_inventory_entry_count": len(ACADEMIC_CONNECTIVES),
            "candidate_count": len(candidates),
            "decision_count": len(records),
            "applied_count": len(safe_edits),
            "human_review_count": sum(str(item.get("action")) == "review" for item in records),
            "preserved_count": sum(str(item.get("action")) == "preserve" for item in records),
            "decisions": records,
            "issues": issues,
            "safe_edits": safe_edits,
        }

    @staticmethod
    def _semantic_role_shape_issues(
        candidate: dict[str, Any], semantic_roles: dict[str, Any]
    ) -> list[str]:
        source = (
            unicodedata.normalize("NFKC", str(candidate.get("source_text") or ""))
            .strip()
            .casefold()
        )
        core_roles = (
            "subject",
            "affected_entity",
            "initial_state",
            "target_state",
            "mediator",
        )
        populated = {
            role: unicodedata.normalize("NFKC", str(semantic_roles.get(role) or ""))
            .strip()
            .casefold()
            for role in core_roles
            if str(semantic_roles.get(role) or "").strip()
        }
        issues: list[str] = []
        if len(populated.values()) != len(set(populated.values())):
            issues.append("semantic_roles_duplicated")
        if source and any(value == source for value in populated.values()):
            issues.append("semantic_role_copies_full_source")
        return issues

    @staticmethod
    def _predicate_role_grounding(
        candidate: dict[str, Any], semantic_roles: dict[str, Any]
    ) -> dict[str, Any]:
        """Verify that claimed semantic roles are anchored in supplied prose.

        The relation mechanism may be an inference, but its participants cannot be
        invented solely inside the decision payload. Japanese content-word runs and
        Latin tokens are matched against the source block and its supplied context.
        """

        context_parts = [
            str(candidate.get("source_text") or ""),
            str(candidate.get("previous_text") or ""),
            str(candidate.get("next_text") or ""),
            str(candidate.get("section_claim") or ""),
        ]
        paragraph_contract = candidate.get("paragraph_contract")
        if isinstance(paragraph_contract, dict):
            context_parts.extend(str(value or "") for value in paragraph_contract.values())
        context = unicodedata.normalize("NFKC", " ".join(context_parts)).casefold()
        generic = {
            "こと",
            "もの",
            "ため",
            "状態",
            "関係",
            "作用",
            "機能",
            "主体",
            "対象",
            "文脈",
            "議論",
            "表現",
        }

        def anchors(value: Any) -> set[str]:
            normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
            words = {
                token
                for token in re.findall(
                    r"[a-zà-öø-ÿ][a-zà-öø-ÿ'’-]{2,}|[一-龯々〆ヵヶァ-ヴー]{2,}",
                    normalized,
                )
                if token not in generic
            }
            if not words and len(normalized.strip()) >= 2:
                words.add(normalized.strip())
            return words

        grounded: set[str] = set()
        role_anchors: dict[str, list[str]] = {}
        for role, value in semantic_roles.items():
            values = anchors(value)
            matched = sorted(item for item in values if item in context)
            role_anchors[str(role)] = matched
            if matched:
                grounded.add(str(role))
        return {
            "grounded_roles": grounded,
            "role_anchors": role_anchors,
        }

    @staticmethod
    def _join_sentences(sentences: list[str]) -> str:
        result = ""
        for sentence in sentences:
            value = sentence.strip()
            if not value:
                continue
            if result and result[-1].isascii() and value[0].isascii():
                result += " "
            result += value
        return result

    @staticmethod
    def _strings(value: Any) -> list[str]:
        if not isinstance(value, (list, tuple)):
            return []
        return [str(item) for item in value if str(item).strip()]
