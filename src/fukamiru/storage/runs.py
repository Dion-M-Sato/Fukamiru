"""Run state and snapshot queries."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fukamiru.authoring.evidence_contracts import (
    EvidenceUse,
    FixedAssets,
    compile_draft_evidence,
    draft_source_use_constraints,
    evidence_use_identity_hash,
    localize_audit_finding,
    project_argument_evidence_sources,
    refresh_draft_evidence_identity,
)
from fukamiru.authoring.generation import (
    recover_empty_citation_projection,
    recover_stale_candidate_projection,
    recover_submission_revalidation,
    rendered_qualification_recovery,
)
from fukamiru.authoring.policy import authoring_policy
from fukamiru.bibliographic_metadata import reconcile_verified_publisher_metadata
from fukamiru.errors import FukamiruError
from fukamiru.storage.events import EventRepository
from fukamiru.storage.llm_observations import LLMObservationRepository
from fukamiru.storage.payloads import PayloadStore
from fukamiru.storage.proofreading_generations import checkpoint_recovery
from fukamiru.storage.recovery_contracts import (
    anchor_error_kind,
    recovery_available,
    verified_scholarship_anchor,
)
from fukamiru.workflow.identity import (
    MANUSCRIPT_GENERATOR,
    is_manuscript_generator_workflow,
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _v32_prose_basis_typo_is_replayable(
    candidate: dict[str, Any], review: dict[str, Any],
    context: dict[str, Any], payload: dict[str, Any],
) -> bool:
    """Prove a namespace typo before permitting one diagnostic-guidance replay.

    The shadow mapping is only an eligibility check. It is never an accepted
    audit response: the worker must obtain and validate a new model judgment.
    """
    from fukamiru.authoring import concept_fit as fit

    material = fit.material(context, payload)
    allowed = fit._ids(material)
    shadow = json.loads(_json_dump(review))
    corrections: dict[str, str] = {}
    for row in shadow.get("units") or []:
        for concept in row.get("concepts") or []:
            for unknown in set(concept.get("basis_ids") or []) - allowed:
                match = re.fullmatch(r"b\d{2}-(.+)", unknown)
                matches = [known for known in allowed if match and re.fullmatch(
                    r"b\d{2}-" + re.escape(match[1]), known,
                )]
                if len(matches) != 1:
                    return False
                corrections[unknown] = matches[0]
            concept["basis_ids"] = [corrections.get(item, item)
                                    for item in concept.get("basis_ids") or []]
    if not corrections or len(corrections) > 8:
        return False
    try:
        fit.validate_review({
            "units": fit.units(candidate), "material": material,
            "source_units": fit.units({"paragraphs": [
                {"text": row.get("text") or ""} for row in payload.get("paragraphs") or []
            ]}),
        }, shadow)
    except (FukamiruError, ValueError, TypeError, KeyError):
        return False
    return True


def _v32_internal_evidence_review_is_repairable(
    message: str, candidate: dict[str, Any],
) -> bool:
    prefix = "Concept repair remains unresolved: "
    if not message.startswith(prefix):
        return False
    try:
        findings = json.loads(message[len(prefix):])
        if not isinstance(findings, list) or not findings:
            return False
        for finding in findings:
            match = re.fullmatch(r"/nodes/(\d+)/text", finding.get("unit_id", ""))
            if (match is None or finding.get("verdict") != "wording"
                    or finding.get("must_keep") is not False):
                return False
            node = candidate["nodes"][int(match[1])]
            if (node.get("node_type") != "evidence" or not finding.get("term")
                    or finding["term"] not in node.get("text", "")):
                return False
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        return False
    return True


def _v32_bounded_concept_repair_is_replayable(
    message: str, native: dict[str, Any], changes: dict[str, Any], review: dict[str, Any],
) -> bool:
    from fukamiru.authoring import concept_fit

    prefix = "Concept repair remains unresolved: "
    if not message.startswith(prefix):
        return False
    try:
        errors = json.loads(message[len(prefix):])
        candidate = concept_fit.apply_repairs(native, changes["edits"])
        exposed = concept_fit.units(candidate)
        observed = [r["unit_id"] for r in review["units"]]
        expected = {r["unit_id"] for r in exposed}
        if (not errors or errors != concept_fit.findings(review)
                or len(observed) != len(expected) or set(observed) != expected):
            return False
        by_id = {r["unit_id"]: r["text"] for r in exposed}
        return all(error.get("term") and error["term"] in by_id[error["unit_id"]]
                   for error in errors)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, FukamiruError):
        return False


def _v32_basis_identity_review_is_replayable(
    message: str, native: dict[str, Any], review: dict[str, Any], context: dict[str, Any],
) -> bool:
    """Authorize revalidation, never acceptance, of a formerly unrecognized identity."""
    from fukamiru.authoring import concept_fit

    if message != "Concept review invented a basis ID":
        return False
    try:
        coverage = context.get("evidence_coverage") or []
        supported = {
            row[key] for row in coverage for key in ("finding_id", "obligation_id")
            if isinstance(row.get(key), str) and row[key]
        }
        referenced = {value for row in review["units"] for term in row["concepts"]
                      for value in term["basis_ids"]}
        if not supported.intersection(referenced):
            return False
        supplied = concept_fit.material(context, {"evidence": {"coverage": coverage}})
        concept_fit.validate_review({"units": concept_fit.units(native), "material": supplied},
                                    review)
    except (ValueError, TypeError, KeyError, AttributeError, FukamiruError):
        return False
    return True


def _v32_verification_routing_is_replayable(
    native: dict[str, Any], changes: dict[str, Any],
    earlier_changes: dict[str, Any] | None = None,
) -> bool:
    from fukamiru.authoring import concept_fit

    try:
        first = native
        if earlier_changes:
            first = concept_fit.apply_repairs(native, earlier_changes["edits"])
        repaired = concept_fit.apply_repairs(first, changes["edits"])
        routed = concept_fit.route_removed_verification({}, native, repaired)
        return any(re.search(
            r"外部根拠.{0,30}未解決|比較命題.{0,30}確定せず", row["reason"],
        ) for row in routed)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, FukamiruError):
        return False


def _v32_field_anchor_review_is_replayable(
    message: str, native: dict[str, Any], review: dict[str, Any], context: dict[str, Any],
) -> bool:
    from fukamiru.authoring import concept_fit

    if not message.startswith("Concept review excerpt is not in its field:"):
        return False
    try:
        supplied = concept_fit.material(context, {"evidence": {
            "coverage": context.get("evidence_coverage") or [],
        }})
        errors = concept_fit.field_anchor_errors(
            {"units": concept_fit.units(native), "material": supplied}, review,
        )
        return bool(errors) and next(iter(errors.values())) == message
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, FukamiruError):
        return False


def _v31_stale_draft_evidence_baseline(
    context: dict[str, Any], producer_input: dict[str, Any], producer_output: dict[str, Any],
) -> list[EvidenceUse] | None:
    """Recognize an old baseline inherited by an otherwise intact new draft."""
    if not context.get("evidence_uses") or not context.get("draft_sections"):
        return None
    if any(context.get(key) != producer_output.get(key) for key in (
        "proposed_html", "draft_sections", "evidence_uses", "fixed_assets",
        "draft_attempt", "evidence_identity_hash",
    )) or context.get("fixed_assets") != producer_input.get("fixed_assets"):
        return None
    try:
        if int(context.get("draft_attempt", 0)) <= int(producer_input.get("draft_attempt", 0)):
            return None
        old_uses = [EvidenceUse.model_validate(item)
                    for item in producer_input.get("evidence_uses") or []]
        old_identity = evidence_use_identity_hash(old_uses)
        validation = dict(producer_input.get("proofreading_evidence_identity_validation") or {})
        if (not old_uses or context.get("evidence_identity_hash") != old_identity
                or producer_input.get("evidence_identity_hash") != old_identity
                or validation.get("passed") is not True
                or validation.get("identity_hash") != old_identity):
            return None
        _, compiled, _, _ = compile_draft_evidence(context, context["draft_sections"])
    except (FukamiruError, ValueError, KeyError, TypeError):
        return None
    if ([item.model_dump(mode="json") for item in compiled] != context["evidence_uses"]
            or evidence_use_identity_hash(compiled) == old_identity):
        return None
    return compiled


def _v31_fixed_source_lineage_is_repairable(
    error_message: str, unpacked: dict[str, Any]
) -> bool:
    match = re.fullmatch(
        r"draft evidence ([A-Za-z0-9_-]+) references unknown or disallowed "
        r"fixed source IDs: ([A-Za-z0-9:_-]+(?:, [A-Za-z0-9:_-]+)*)",
        error_message,
    )
    if match is None:
        return False
    evidence_id = match.group(1)
    rejected_ids = set(match.group(2).split(", "))
    try:
        projected = project_argument_evidence_sources(unpacked)
    except (KeyError, TypeError, ValueError, FukamiruError):
        return False
    failed = projected.get(evidence_id)
    contracted_ids = {
        str(source_id)
        for contract in unpacked.get("section_contracts") or []
        if isinstance(contract, dict)
        for source_id in contract.get("evidence_ids") or []
        if str(source_id)
    }
    return bool(
        evidence_id in contracted_ids
        and failed
        and rejected_ids == set(failed["lineage_only_source_ids"])
        and failed["fixed_source_ids"]
        and failed["citation_source_ids"]
        and all(
            projected.get(source_id, {}).get("fixed_source_ids")
            or projected.get(source_id, {}).get("citation_source_ids")
            for source_id in contracted_ids
        )
    )


def _v31_missing_draft_source_is_reviewable(error_message: str, unpacked: dict[str, Any]) -> bool:
    prefix = (
        "V31 contracted evidence has no locked source; repair the argument graph before drafting: "
    )
    if not error_message.startswith(prefix):
        return False
    evidence_ids = error_message[len(prefix) :].split(", ")
    if not evidence_ids or any(
        re.fullmatch(r"[A-Za-z0-9:_-]+", value) is None for value in evidence_ids
    ):
        return False
    try:
        projected = project_argument_evidence_sources(unpacked)
        allowed = set(FixedAssets.model_validate(unpacked["fixed_assets"]).allowed_source_ids)
    except (KeyError, TypeError, ValueError, FukamiruError):
        return False
    contracted = {
        str(evidence_id)
        for contract in unpacked.get("section_contracts") or []
        if isinstance(contract, dict)
        for evidence_id in contract.get("evidence_ids") or []
        if str(evidence_id)
    }
    unresolved = sorted(
        evidence_id
        for evidence_id in contracted
        if evidence_id not in allowed
        and not projected.get(evidence_id, {}).get("fixed_source_ids")
        and not projected.get(evidence_id, {}).get("citation_source_ids")
    )
    return unresolved == sorted(evidence_ids)


def _v31_bibliographic_contextual_use_is_repairable(
    error_message: str, unpacked: dict[str, Any]
) -> bool:
    prefix = "V31 rendered citation predicate lacks semantic attribution: "
    if not error_message.startswith(prefix):
        return False
    entries = error_message[len(prefix) :].split("; ")
    if not entries:
        return False
    parsed = [
        re.fullmatch(
            r"([A-Za-z0-9_-]+):([A-Za-z0-9:_-]+) "
            r"\(classification=mere_reference\)",
            entry,
        )
        for entry in entries
    ]
    if any(match is None for match in parsed):
        return False
    nodes = {
        str(item.get("id") or ""): item
        for item in unpacked.get("argument_nodes") or []
        if isinstance(item, dict) and item.get("node_type") == "evidence"
    }
    citations = {
        str(item.get("id") or ""): item
        for key in ("evidence_context", "annotation_citation_evidence")
        for item in unpacked.get(key) or []
        if isinstance(item, dict) and item.get("id")
    }
    sections = {
        str(item.get("section_id") or ""): item
        for item in unpacked.get("draft_sections") or []
        if isinstance(item, dict) and item.get("section_id")
    }
    for match in parsed:
        assert match is not None
        section_id, evidence_id = match.groups()
        node = nodes.get(evidence_id)
        section = sections.get(section_id)
        if (
            not node
            or not section
            or not any(
                evidence_id in {str(value) for value in paragraph.get("evidence_ids") or []}
                for paragraph in section.get("paragraphs") or []
                if isinstance(paragraph, dict)
            )
        ):
            return False
        provenance = dict(node.get("provenance") or {})
        source_ids = {
            str(value)
            for value in [node.get("source_span_id"), *(provenance.get("source_ids") or [])]
            if str(value)
        }
        matched = [citations[source_id] for source_id in source_ids if source_id in citations]
        matched = [
            citation
            for citation in matched
            if citation.get("allowed_use") == "attributed_scholarship_citation"
            and citation.get("verification_scope") == "bibliographic_identity_only"
            and "自著" not in citation.get("authors", [])
            and "self_citation" not in citation.get("annotation_kinds", [])
            and dict(citation.get("provenance") or {}).get("substantive_support_verified")
            is not True
        ]
        if len({str(item.get("id") or "") for item in matched}) != 1:
            return False
    return True


def _v31_cancelled_self_citation_repair_is_replayable(
    prior_error: dict[str, Any], unpacked: dict[str, Any]
) -> bool:
    """Recognize only a memo-backed composite self-citation repair conflict."""

    if prior_error.get("type") != "FukamiruError":
        return False
    match = re.fullmatch(
        r"V31 targeted repair repeatedly broke its JSON section contract: "
        r"section ([A-Za-z0-9_-]+) scholarship evidence "
        r"([A-Za-z0-9:_-]+) has no semantically confirmed attribution "
        r"predicate \(classification=(?:contextual_use|mere_reference)\)",
        str(prior_error.get("message") or ""),
    )
    if match is None:
        return False
    section_id, evidence_id = match.groups()
    node = next(
        (
            item
            for item in unpacked.get("argument_nodes") or []
            if isinstance(item, dict)
            and item.get("id") == evidence_id
            and item.get("node_type") == "evidence"
        ),
        None,
    )
    if node is None:
        return False
    citation = next(
        (
            item
            for item in unpacked.get("annotation_citation_evidence") or []
            if isinstance(item, dict) and item.get("id") == node.get("source_span_id")
        ),
        None,
    )
    if citation is None or "自著" not in citation.get("authors", []):
        return False
    reference = next(
        (
            item
            for item in unpacked.get("reference_entries") or []
            if isinstance(item, dict) and item.get("reference_id") == citation.get("reference_id")
        ),
        None,
    )
    if reference is None:
        return False
    original = re.fullmatch(
        r"自著[「『]([^」』]+)[」』]",
        str(reference.get("original_text") or "").strip(),
    )
    title = str(reference.get("title") or "")
    if (
        original is None
        or not title.startswith(original.group(1))
        or "」、『" not in title
        or "・「" not in title
    ):
        return False
    return any(
        isinstance(item, dict)
        and item.get("section_id") == section_id
        and item.get("category") == "evidence_mismatch"
        and evidence_id in str(item.get("description") or "")
        and "複合文字列" in str(item.get("description") or "")
        for item in unpacked.get("audit_findings") or []
    )


def _v31_citation_source_claim_mapping_is_repairable(
    error_message: str,
    unpacked: dict[str, Any],
    *,
    expect_missing_mapping: bool = True,
) -> bool:
    match = re.fullmatch(
        r"Authoring citation JSON handoff lost required source-to-claim mapping "
        r"for section ([A-Za-z0-9_-]+): (\[.*\])",
        error_message,
    )
    if match is None:
        return False
    try:
        failed_source_ids = ast.literal_eval(match.group(2))
    except (SyntaxError, ValueError):
        return False
    if (
        not isinstance(failed_source_ids, list)
        or not failed_source_ids
        or any(
            not isinstance(source_id, str)
            or not re.fullmatch(r"annotation-citation:[A-Za-z0-9_-]+", source_id)
            for source_id in failed_source_ids
        )
        or failed_source_ids != sorted(set(failed_source_ids))
    ):
        return False

    section_id = match.group(1)
    sections = [
        item
        for item in unpacked.get("section_contracts") or []
        if isinstance(item, dict) and str(item.get("section_id") or "") == section_id
    ]
    if not sections:
        return False
    section_claim_sets = [
        {str(claim_id) for claim_id in section.get("claim_ids") or [] if str(claim_id)}
        for section in sections
    ]
    section_claim_ids = section_claim_sets[0]
    if any(claim_ids != section_claim_ids for claim_ids in section_claim_sets[1:]):
        return False
    if not section_claim_ids:
        return False

    citations_by_id = {
        str(item.get("id") or ""): item
        for item in unpacked.get("annotation_citation_evidence") or []
        if isinstance(item, dict) and item.get("id")
    }
    contracts = [
        item
        for item in unpacked.get("authoring_atomic_evidence_contracts") or []
        if isinstance(item, dict)
        and item.get("realization") == "attribute_theory_with_citation"
        and section_claim_ids.intersection(
            str(claim_id) for claim_id in item.get("claim_ids") or [] if str(claim_id)
        )
    ]
    required_source_ids = {
        str(source_id)
        for contract in contracts
        for source_id in contract.get("citation_evidence_ids") or []
        if str(source_id)
    }

    evidence_by_id = {
        str(item.get("id") or ""): item
        for key in ("evidence_context", "annotation_citation_evidence")
        for item in unpacked.get(key) or []
        if isinstance(item, dict)
        and item.get("id")
        and item.get("allowed_use") == "attributed_scholarship_citation"
    }
    mapped_source_ids: set[str] = set()
    argument_nodes = [
        item for item in unpacked.get("argument_nodes") or [] if isinstance(item, dict)
    ]
    claim_node_ids = {
        str(item.get("claim_id") or ""): str(item.get("id") or "")
        for item in argument_nodes
        if item.get("node_type") == "claim" and item.get("claim_id") and item.get("id")
    }
    for node in argument_nodes:
        if (
            node.get("node_type") != "evidence"
            or str(node.get("claim_id") or "") not in section_claim_ids
        ):
            continue
        provenance = node.get("provenance") or {}
        source_ids = [
            node.get("source_span_id"),
            *(provenance.get("source_ids") or [] if isinstance(provenance, dict) else []),
        ]
        mapped_source_ids.update(
            str(evidence_by_id[str(source_id)].get("id") or "")
            for source_id in source_ids
            if str(source_id) in evidence_by_id
        )
    if expect_missing_mapping:
        if required_source_ids - mapped_source_ids != set(failed_source_ids):
            return False
    elif not set(failed_source_ids).issubset(required_source_ids & mapped_source_ids):
        return False

    reference_ids = {
        str(item.get("reference_id") or "")
        for item in unpacked.get("reference_entries") or []
        if isinstance(item, dict) and item.get("reference_id")
    }
    for source_id in failed_source_ids:
        citation = citations_by_id.get(source_id)
        if (
            not citation
            or citation.get("allowed_use") != "attributed_scholarship_citation"
            or str(citation.get("reference_id") or "") not in reference_ids
        ):
            return False
        matching_contracts = [
            contract
            for contract in contracts
            if source_id in {str(value) for value in contract.get("citation_evidence_ids") or []}
            and str(contract.get("proposition_id") or "")
            in {str(value) for value in citation.get("annotation_proposition_ids") or []}
        ]
        if not matching_contracts:
            return False
        mapped_claim_ids = {
            str(claim_id)
            for contract in matching_contracts
            for claim_id in contract.get("claim_ids") or []
            if str(claim_id) in section_claim_ids
        }
        if (
            not mapped_claim_ids
            or (
                expect_missing_mapping
                and mapped_claim_ids.intersection(
                    str(claim_id) for claim_id in citation.get("claim_ids") or []
                )
            )
            or any(claim_id not in claim_node_ids for claim_id in mapped_claim_ids)
        ):
            return False
    return True


def _v31_has_mergeable_duplicate_section_contracts(value: object) -> bool:
    """Recognize section-ID duplicates differing only by advisory paragraph labels."""

    if not isinstance(value, list) or not value:
        return False
    rows_by_id: dict[str, list[dict[str, Any]]] = {}
    for item in value:
        if not isinstance(item, dict) or not str(item.get("section_id") or ""):
            return False
        section_id = str(item["section_id"])
        development = item.get("development_expectations") or {}
        if not isinstance(development, dict):
            return False
        moves = development.get("required_argument_moves", [])
        if moves is not None and not isinstance(moves, list):
            return False
        rows_by_id.setdefault(section_id, []).append(item)
    if not any(len(rows) > 1 for rows in rows_by_id.values()):
        return False

    def without_advisory_moves(item: dict[str, Any]) -> str:
        normalized = json.loads(json.dumps(item, ensure_ascii=False))
        development = normalized.get("development_expectations")
        if isinstance(development, dict):
            development.pop("required_argument_moves", None)
        return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    unique_contracts: list[dict[str, Any]] = []
    for rows in rows_by_id.values():
        if len({without_advisory_moves(item) for item in rows}) != 1:
            return False
        unique_contracts.append(rows[0])
    roles = [str(item.get("article_role") or "") for item in unique_contracts]
    return (
        roles.count("introduction") == 1
        and roles.count("conclusion") == 1
        and roles.count("body") >= 1
        and all(role in {"introduction", "body", "conclusion"} for role in roles)
    )


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _v31_checkpoint_has_explicit_cross_branch_synthesis(
    result_json: str,
    claims: object,
) -> bool:
    """Recognize a persisted, explicitly named V31 synthesis phase for one replay."""

    try:
        result = json.loads(result_json)
    except (TypeError, ValueError):
        return False
    if not isinstance(result, dict) or not isinstance(claims, list):
        return False
    claim_rows = [item for item in claims if isinstance(item, dict) and item.get("id")]
    claim_ids = {str(item["id"]) for item in claim_rows}
    root_claim_ids = {str(item["id"]) for item in claim_rows if item.get("parent_id") is None}
    direct_subclaims = {
        str(item["id"])
        for item in claim_rows
        if str(item.get("parent_id") or "") in root_claim_ids
        and item.get("level") in {"sub", "subclaim"}
    }
    parent_by_id = {str(item["id"]): str(item.get("parent_id") or "") for item in claim_rows}

    def branch_for(claim_id: str) -> str | None:
        current = claim_id
        seen: set[str] = set()
        while current and current not in seen:
            seen.add(current)
            if current in direct_subclaims:
                return current
            current = parent_by_id.get(current, "")
        return None

    for section in result.get("nodes") or []:
        if not isinstance(section, dict) or section.get("article_role") != "body":
            continue
        if section.get("relation") not in {"summary", "contrast", "synthesis"}:
            continue
        purpose = str(section.get("discourse_purpose") or "").casefold()
        if not any(
            marker in purpose
            for marker in (
                "独立した横断的な総合",
                "独立した横断的な検討",
                "explicitly separate synthesis phase",
            )
        ):
            continue
        branches = {
            branch
            for claim_id in section.get("claim_ids") or []
            if str(claim_id) in claim_ids and (branch := branch_for(str(claim_id))) is not None
        }
        if len(branches) > 1:
            return True
    return False


_ENGINE_AUTHORIZATION_CONTEXT_KEYS = (
    "_engine_authorization_id",
    "authorized_engine_fingerprint",
    "authorized_manuscript_inference_engine",
    "authorized_language_inference_engine",
)

_PROOFREADING_EVENT_STAGES = {
    "manuscript.proofreading.sentences.completed": "sentences_extracted",
    "manuscript.proofreading.generation.progress": "candidates_generating",
    "manuscript.proofreading.generation.completed": "candidates_generated",
    "manuscript.proofreading.preprocess.completed": "candidates_preprocessed",
    "manuscript.proofreading.luna.completed": "luna_screened",
    "manuscript.proofreading.preference.completed": "candidates_ranked",
    "manuscript.proofreading.selection.completed": "candidates_selected",
    "manuscript.proofreading.shadow_published": "shadow_published",
}

_PROOFREADING_NODE_IDS = {
    "redraft_body_prose",
    "diagnose_japanese_prose",
    "redraft_japanese_paragraphs",
    "review_japanese_sections",
    "extract_proofreading_sentences",
    "generate_proofreading_candidates",
    "preprocess_proofreading_candidates",
    "screen_proofreading_with_luna",
    "rank_proofreading_candidates",
    "select_proofreading_candidates",
    "publish_proofreading_shadow",
    "japanese_surface_quality_gate",
    "reconcile_candidate_context_after_proofreading",
}

_V31_EXTENDED_STRUCTURE_REPAIR_LIMIT = 4
_V31_LOCAL_SECTION_REPAIR_LIMIT = 6
_V31_STRUCTURE_REPAIR_POLICY = "history-aware-minimal-sentence-audit-repair-v8"


def _v31_audit_repair_signature(findings: object) -> str | None:
    if not isinstance(findings, list) or not findings:
        return None
    normalized = [
        {
            "code": str(item.get("code") or ""),
            "severity": str(item.get("severity") or ""),
            "object_type": str(item.get("object_type") or ""),
            "object_id": str(item.get("object_id") or ""),
            "repair_scope": str(item.get("repair_scope") or ""),
            "message": str(item.get("message") or ""),
        }
        for item in findings
        if isinstance(item, dict)
    ]
    if not normalized:
        return None
    normalized.sort(
        key=lambda item: (
            item["object_id"],
            item["code"],
            item["severity"],
            item["message"],
        )
    )
    return hashlib.sha256(_json_dump(normalized).encode("utf-8")).hexdigest()


def _v31_audit_findings_are_section_scoped(findings: object) -> bool:
    """Return whether every finding can be retried through a named section.

    An auditor may label a conclusion-answer correction ``plan_revision`` even
    when it identifies one existing section and supplies a replacement that is
    consistent with the locked contract. The V31 repair node already routes
    such findings to that section and validates the unchanged plan afterward,
    so count them as narrow repairs for the bounded replay limit. Document,
    plan and user-input findings remain outside this allowance.
    """

    return (
        bool(findings)
        and isinstance(findings, list)
        and all(
            isinstance(item, dict)
            and str(item.get("object_type") or "") == "section"
            and str(item.get("object_id") or "").startswith("section-")
            and str(item.get("repair_scope") or "") in {"local_section", "plan_revision"}
            for item in findings
        )
    )


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


@dataclass(frozen=True)
class RunCreated:
    run_id: str
    event_id: int


@dataclass(frozen=True)
class AuthoringContractRestartCreated:
    run_id: str
    event_id: int
    snapshot_run_id: str
    snapshot_node_run_id: str
    plan_id: str


class RunRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def create(
        self,
        *,
        workflow_name: str,
        workflow_version: str,
        entry_node: str,
        project_id: str | None,
        inputs: dict[str, Any],
        resource_lane: str = "ingestion_cpu",
    ) -> RunCreated:
        run_id = str(uuid.uuid4())
        node_run_id = str(uuid.uuid4())
        created_at = utc_now()
        self.connection.execute(
            """
            INSERT INTO runs(
                id, project_id, workflow_name, workflow_version,
                status, input_json, created_at
            ) VALUES (?, ?, ?, ?, 'queued', ?, ?)
            """,
            (run_id, project_id, workflow_name, workflow_version, _json_dump(inputs), created_at),
        )
        self.connection.execute(
            """
            INSERT INTO node_runs(
                id, run_id, node_id, attempt, status, input_json, resource_lane
            ) VALUES (?, ?, ?, 1, 'queued', ?, ?)
            """,
            (node_run_id, run_id, entry_node, _json_dump(inputs), resource_lane),
        )
        self.connection.commit()
        events = EventRepository(self.connection)
        event = events.append(
            run_id,
            "run.created",
            object_type="run",
            object_id=run_id,
            payload={"status": "queued", "workflow_name": workflow_name},
        )
        events.append(
            run_id,
            "node.queued",
            object_type="node_run",
            object_id=node_run_id,
            payload={"node_id": entry_node, "attempt": 1, "resource_lane": resource_lane},
        )
        return RunCreated(run_id=run_id, event_id=event.event_id)

    def create_authoring_contract_restart(
        self,
        source_run_id: str,
        *,
        workflow_version: str,
        entry_node: str = "lock_section_contracts",
        resource_lane: str = "codex_remote",
    ) -> AuthoringContractRestartCreated:
        """Branch from a saved contract or post-evidence argument boundary."""

        if entry_node not in {"lock_section_contracts", "build_argument_graph"}:
            raise FukamiruError("Unsupported authoring restart boundary")
        argument_restart = entry_node == "build_argument_graph"
        if argument_restart and str(workflow_version) != "32":
            raise FukamiruError("Argument restart requires manuscript generator V32")

        source_row = self.connection.execute(
            "SELECT id,workflow_name,workflow_version,status,project_id,input_json "
            "FROM runs WHERE id=?", (source_run_id,),
        ).fetchone()
        if source_row is None:
            raise FukamiruError(f"Unknown run: {source_run_id}")
        if not is_manuscript_generator_workflow(source_row["workflow_name"]) or str(
            source_row["workflow_version"]
        ) != str(workflow_version):
            raise FukamiruError("Contract restart requires the source authoring workflow version")
        # Waiting for human review is a stable, immutable candidate. A derived
        # restart gets its own plan and Run and does not act on that proposal.
        if source_row["status"] in {"queued", "running"}:
            raise FukamiruError("Cannot branch an active authoring run")
        project_id = str(source_row["project_id"] or "")
        if not project_id:
            raise FukamiruError("Authoring contract restart requires a project")

        payloads = PayloadStore(self.connection)

        def load_context(raw: str | None) -> dict[str, Any]:
            value = payloads.unpack(json.loads(raw or "{}"))
            if not isinstance(value, dict):
                raise FukamiruError("Authoring restart snapshot is not an object")
            return dict(value)

        source_context = load_context(source_row["input_json"])
        identity_fields = (
            "source_sha256",
            "source_revision_id",
            "document_id",
            "plan_id",
        )
        source_identity = {key: str(source_context.get(key) or "") for key in identity_fields}
        selected_row: sqlite3.Row | None = None
        selected_context: dict[str, Any] | None = None
        source_identity_inferred_from_completed_node = False
        has_initial_identity = bool(source_identity["plan_id"]) and bool(
            source_identity["source_sha256"]
            or (source_identity["source_revision_id"] and source_identity["document_id"])
        )
        if not has_initial_identity:
            # A full-path authoring Run starts before a plan exists, so its immutable
            # Run input cannot identify the later contract boundary. Its own completed
            # lock_section_contracts input is the authoritative boundary snapshot and
            # is safe to branch directly without matching another Run by partial data.
            completed_boundary = self.connection.execute(
                """
                SELECT id AS node_run_id, run_id, input_json, completed_at
                FROM node_runs
                WHERE run_id=? AND node_id=? AND status='completed'
                ORDER BY completed_at DESC, rowid DESC
                LIMIT 1
                """,
                (source_run_id, entry_node),
            ).fetchone()
            if completed_boundary is not None:
                boundary_context = load_context(completed_boundary["input_json"])
                boundary_identity = {
                    key: str(boundary_context.get(key) or "") for key in identity_fields
                }
                if boundary_identity["plan_id"]:
                    source_identity = boundary_identity
                    selected_row = completed_boundary
                    selected_context = boundary_context
                    source_identity_inferred_from_completed_node = True
            if selected_row is None:
                raise FukamiruError("Source run does not identify a restartable authoring lineage")
        else:
            candidates = self.connection.execute(
                """
                SELECT n.id AS node_run_id, n.run_id, n.input_json, n.completed_at
                FROM node_runs n
                JOIN runs r ON r.id=n.run_id
                WHERE r.project_id=?
                  AND r.workflow_name IN ('authoring', 'manuscript_generator')
                  AND r.workflow_version=?
                  AND n.node_id=?
                  AND n.status='completed'
                ORDER BY n.completed_at DESC, n.rowid DESC
                LIMIT 100
                """,
                (project_id, str(workflow_version), entry_node),
            ).fetchall()
            for candidate in candidates:
                candidate_context = load_context(candidate["input_json"])
                if all(
                    not expected or str(candidate_context.get(key) or "") == expected
                    for key, expected in source_identity.items()
                ):
                    selected_row = candidate
                    selected_context = candidate_context
                    break
        if selected_row is None or selected_context is None:
            raise FukamiruError(
                "No completed lock_section_contracts input matches this authoring lineage"
            )

        required_fields = (
            ("research_question", "claims", "propositions", "classifications")
            if argument_restart else (
            "research_question",
            "claims",
            "argument_nodes",
            "discourse_nodes",
            "manuscript_structure_plan",
            "annotation_citation_evidence",
            "reference_entries",
            )
        )
        missing = [key for key in required_fields if not selected_context.get(key)]
        if missing:
            raise FukamiruError(
                "Authoring contract restart snapshot is incomplete: " + ", ".join(missing)
            )

        downstream_fields = {
            "_authoring_checkpoint",
            "_join_generations",
            "applied_edits",
            "audit_findings",
            "body_section_outputs",
            "authoring_economy_audit_blockers",
            "authoring_economy_draft_warnings",
            "completion_status",
            "conclusion_contract",
            "conclusion_synthesis",
            "conditional_conditions",
            "conditional_success",
            "draft_attempt",
            "draft_audit_join",
            "draft_sections",
            "enumeration_context_contracts",
            "human_review",
            "introduction_contract",
            "locked_plan",
            "manuscript_coherence_checks",
            "paragraph_boundary_lock",
            "paragraph_plan",
            "paragraph_plan_validation",
            "parent_plan_revision",
            "polished_sections",
            "pre_polish_html",
            "proposed_html",
            "realized_paragraphs",
            "repair_count",
            "rewrite_batch_count",
            "rewrite_summary",
            "rewrite_warnings",
            "section_contracts",
            "semantic_paragraph_plan",
            "source_lineage_policy",
            "source_preservation_validation",
            "structure_plan",
            "structure_plan_validation",
            "term_introduction_plan",
            "unresolved_questions",
            "v26_block_verification_records",
            "v26_coherence_report",
        }
        if argument_restart:
            downstream_fields.update({
                "argument_nodes", "argument_edges", "claim_scopes", "defense_obligations",
                "hypotheses", "discourse_nodes", "discourse_edges", "manuscript_structure_plan",
                "concept_fit_reports_v32", "concept_fit_final_audit_v32",
            })
        restart_context = {
            key: value for key, value in selected_context.items() if key not in downstream_fields
        }
        original_plan_id = str(restart_context.get("plan_id") or "")
        restart_plan_id = str(uuid.uuid4())
        restart_context.update(
            {
                "plan_id": restart_plan_id,
                "plan_revision": 1,
                "project_id": project_id,
                "authoring_workflow_policy": authoring_policy(workflow_version),
                "authoring_restart_lineage": {
                    "policy": "authoring-contract-boundary-restart-v1",
                    "requested_from_run_id": source_run_id,
                    "snapshot_run_id": str(selected_row["run_id"]),
                    "snapshot_node_run_id": str(selected_row["node_run_id"]),
                    "restart_node_id": entry_node,
                    "source_plan_id": original_plan_id,
                    "checkpoint_reuse": False,
                    "source_identity_inferred_from_completed_node": (
                        source_identity_inferred_from_completed_node
                    ),
                },
            }
        )
        if argument_restart:
            restart_context["_join_generations"] = {
                "plan_manuscript_structure": str(uuid.uuid4()),
            }
            from fukamiru.review_feedback import apply_feedback, feedback_context

            submitted = feedback_context(self.connection, source_run_id)
            if submitted["answers"] or submitted["overall_comments"]:
                restart_context = apply_feedback(restart_context, submitted)
        created = self.create(
            workflow_name=(
                MANUSCRIPT_GENERATOR if str(workflow_version) in {"31", "32"} else "authoring"
            ),
            workflow_version=str(workflow_version),
            entry_node=entry_node,
            project_id=project_id,
            inputs=restart_context,
            resource_lane=resource_lane,
        )
        if argument_restart:
            preservation_node_id = str(uuid.uuid4())
            self.connection.execute(
                "INSERT INTO node_runs(id,run_id,node_id,attempt,status,input_json,resource_lane) "
                "VALUES (?,?,?,1,'queued',?,'ingestion_cpu')",
                (preservation_node_id, created.run_id, "build_source_preservation_map",
                 _json_dump(restart_context)),
            )
            EventRepository(self.connection).append(
                created.run_id, "node.queued", object_type="node_run",
                object_id=preservation_node_id,
                payload={"node_id": "build_source_preservation_map", "attempt": 1,
                         "reason": "rebuild_source_preservation_for_argument_restart"},
            )
        event_payload = dict(restart_context["authoring_restart_lineage"])
        event_payload["derived_run_id"] = created.run_id
        event = EventRepository(self.connection).append(
            created.run_id,
            "run.authoring_contract_restart_created",
            object_type="run",
            object_id=created.run_id,
            payload=event_payload,
        )
        EventRepository(self.connection).append(
            source_run_id,
            "run.authoring_contract_restart_spawned",
            object_type="run",
            object_id=created.run_id,
            payload=event_payload,
        )
        return AuthoringContractRestartCreated(
            run_id=created.run_id,
            event_id=event.event_id,
            snapshot_run_id=str(selected_row["run_id"]),
            snapshot_node_run_id=str(selected_row["node_run_id"]),
            plan_id=restart_plan_id,
        )

    def create_authoring_draft_restart(
        self,
        source_run_id: str,
        *,
        workflow_version: str,
        entry_node: str = "draft_rewrite",
        resource_lane: str = "codex_remote",
    ) -> AuthoringContractRestartCreated:
        """Branch from this Run's immutable pre-draft JSON handoff."""

        source_row = self.connection.execute(
            "SELECT * FROM runs WHERE id=?", (source_run_id,)
        ).fetchone()
        if source_row is None:
            raise FukamiruError(f"Unknown run: {source_run_id}")
        if (
            not is_manuscript_generator_workflow(source_row["workflow_name"])
            or str(source_row["workflow_version"]) != str(workflow_version)
            or str(workflow_version) not in {"31", "32"}
        ):
            raise FukamiruError("Draft restart requires a source authoring V31 or V32 workflow")
        if source_row["status"] in {"queued", "running"}:
            raise FukamiruError("Cannot branch an active authoring run")
        project_id = str(source_row["project_id"] or "")
        if not project_id:
            raise FukamiruError("Authoring draft restart requires a project")

        snapshot_row = self.connection.execute(
            """
            SELECT id AS node_run_id, run_id, input_json, completed_at, status
            FROM node_runs
            WHERE run_id=? AND node_id=? AND status IN ('completed','failed')
            ORDER BY COALESCE(completed_at, started_at) DESC, rowid DESC
            LIMIT 1
            """,
            (source_run_id, entry_node),
        ).fetchone()
        if snapshot_row is None:
            raise FukamiruError("Source run has no terminal pre-draft JSON handoff")
        payloads = PayloadStore(self.connection)
        snapshot_context = payloads.unpack(json.loads(snapshot_row["input_json"] or "{}"))
        if not isinstance(snapshot_context, dict):
            raise FukamiruError("Authoring draft restart snapshot is not an object")
        required_fields = (
            "plan_id",
            "plan_revision",
            "research_problem",
            "argument_nodes",
            "claim_scopes",
            "defense_obligations",
            "section_contracts",
            "semantic_paragraph_plan",
            "introduction_contract",
            "conclusion_contract",
            "body_section_outputs",
            "reference_entries",
        )
        missing = [key for key in required_fields if not snapshot_context.get(key)]
        if missing:
            raise FukamiruError(
                "Authoring draft restart snapshot is incomplete: " + ", ".join(missing)
            )

        downstream_fields = {
            "applied_edits",
            "audit_findings",
            "completion_status",
            "conditional_conditions",
            "conditional_success",
            "conclusion_synthesis",
            "draft_audit_join",
            "draft_sections",
            "final_quality_sha256",
            "frozen_candidate_v26",
            "human_review",
            "polished_sections",
            "pre_polish_html",
            "proposed_html",
            "realized_paragraphs",
            "repair_count",
            "rewrite_summary",
            "rewrite_warnings",
            "submission_validation_gate",
            "v26_block_verification_records",
            "v26_coherence_report",
        }
        restart_context = {
            key: value for key, value in snapshot_context.items() if key not in downstream_fields
        }
        audit_row = self.connection.execute(
            """
            SELECT output_json
            FROM node_runs
            WHERE run_id=? AND node_id='audit_structure' AND status='completed'
            ORDER BY completed_at DESC, rowid DESC
            LIMIT 1
            """,
            (source_run_id,),
        ).fetchone()
        restart_findings: list[dict[str, Any]] = []
        if audit_row is not None:
            audit_context = payloads.unpack(json.loads(audit_row["output_json"] or "{}"))
            if isinstance(audit_context, dict):
                restart_findings = [
                    {
                        "code": str(item.get("code") or item.get("defect_type") or ""),
                        "severity": str(item.get("severity") or ""),
                        "message": str(item.get("message") or ""),
                        "object_type": str(item.get("object_type") or ""),
                        "object_id": str(item.get("object_id") or ""),
                        "repair_scope": str(item.get("repair_scope") or ""),
                    }
                    for item in audit_context.get("audit_findings") or []
                    if isinstance(item, dict)
                    and str(item.get("severity") or "") in {"minor", "major", "critical"}
                    and str(item.get("message") or "").strip()
                ]
        if restart_findings:
            restart_context["authoring_draft_restart_findings"] = restart_findings
        plan_id = str(restart_context["plan_id"])
        plan_revision = int(restart_context["plan_revision"])
        draft_attempt = int(
            self.connection.execute(
                "SELECT COALESCE(MAX(attempt),0)+1 FROM section_drafts WHERE contract_id LIKE ?",
                (f"{plan_id}@{plan_revision}:%",),
            ).fetchone()[0]
        )
        restart_context.update(
            {
                "project_id": project_id,
                "draft_attempt": draft_attempt,
                "authoring_workflow_policy": authoring_policy(workflow_version),
                "authoring_draft_restart_lineage": {
                    "policy": "authoring-pre-draft-boundary-restart-v2",
                    "requested_from_run_id": source_run_id,
                    "snapshot_run_id": str(snapshot_row["run_id"]),
                    "snapshot_node_run_id": str(snapshot_row["node_run_id"]),
                    "restart_node_id": entry_node,
                    "source_plan_id": plan_id,
                    "draft_attempt": draft_attempt,
                    "checkpoint_reuse": False,
                    "inherited_audit_finding_count": len(restart_findings),
                },
            }
        )
        created = self.create(
            workflow_name=MANUSCRIPT_GENERATOR,
            workflow_version=str(workflow_version),
            entry_node=entry_node,
            project_id=project_id,
            inputs=restart_context,
            resource_lane=resource_lane,
        )
        event_payload = dict(restart_context["authoring_draft_restart_lineage"])
        event_payload["derived_run_id"] = created.run_id
        event = EventRepository(self.connection).append(
            created.run_id,
            "run.authoring_draft_restart_created",
            object_type="run",
            object_id=created.run_id,
            payload=event_payload,
        )
        EventRepository(self.connection).append(
            source_run_id,
            "run.authoring_draft_restart_spawned",
            object_type="run",
            object_id=created.run_id,
            payload=event_payload,
        )
        return AuthoringContractRestartCreated(
            run_id=created.run_id,
            event_id=event.event_id,
            snapshot_run_id=str(snapshot_row["run_id"]),
            snapshot_node_run_id=str(snapshot_row["node_run_id"]),
            plan_id=plan_id,
        )

    def create_authoring_render_restart(
        self, source_run_id: str, *, workflow_version: str,
    ) -> AuthoringContractRestartCreated:
        return self._create_authoring_tail_restart(
            source_run_id, workflow_version=workflow_version, boundary="render_evidence_footnotes",
        )

    def create_authoring_audit_restart(
        self, source_run_id: str, *, workflow_version: str,
    ) -> AuthoringContractRestartCreated:
        return self._create_authoring_tail_restart(
            source_run_id, workflow_version=workflow_version, boundary="audit_final_findings",
        )

    def _create_authoring_tail_restart(
        self, source_run_id: str, *, workflow_version: str, boundary: str,
    ) -> AuthoringContractRestartCreated:
        """Fork a validated saved boundary without mutating source history."""
        from fukamiru.documents.canonical_html import CanonicalHTMLValidator

        source = self.connection.execute(
            "SELECT * FROM runs WHERE id=?", (source_run_id,),
        ).fetchone()
        if source is None:
            raise FukamiruError(f"Unknown run: {source_run_id}")
        if (not is_manuscript_generator_workflow(source["workflow_name"])
                or str(source["workflow_version"]) != "32" or str(workflow_version) != "32"):
            raise FukamiruError("Render restart requires a V32 manuscript-generator Run")
        if source["status"] in {"queued", "running"}:
            raise FukamiruError("Cannot branch an active authoring run")
        if not source["project_id"]:
            raise FukamiruError("Authoring render restart requires a project")
        assert boundary in {"render_evidence_footnotes", "audit_final_findings"}
        row = self.connection.execute(
            "SELECT id,input_json,status FROM node_runs "
            "WHERE run_id=? AND node_id=? ORDER BY rowid DESC LIMIT 1",
            (source_run_id, boundary),
        ).fetchone()
        if row is None or row["status"] != "completed":
            raise FukamiruError(
                f"Authoring restart requires the latest successful {boundary} input"
            )
        try:
            context = PayloadStore(self.connection).unpack(json.loads(row["input_json"] or "{}"))
        except (TypeError, ValueError) as exc:
            raise FukamiruError("Authoring render restart snapshot is unreadable") from exc
        required = (
            "plan_id", "plan_revision", "source_document_id", "source_revision_id", "document_id",
            "research_problem", "section_contracts", "draft_sections", "proposed_html",
        )
        if not isinstance(context, dict) or any(not context.get(key) for key in required):
            raise FukamiruError("Authoring render restart snapshot is incomplete")
        CanonicalHTMLValidator().assert_valid(str(context["proposed_html"]))
        target = self.connection.execute(
            "SELECT project_id,current_revision_id FROM content_documents WHERE id=?",
            (str(context["document_id"]),),
        ).fetchone()
        if (target is None or target["project_id"] != source["project_id"]
                or target["current_revision_id"] is not None):
            raise FukamiruError("Render restart requires an unapproved target paper")
        downstream = {
            "reference_rendered_html_v26", "final_quality_sha256", "frozen_candidate_v26",
            "final_bibliography_entries", "reference_render_report", "reference_quality_gate",
            "reference_usage_reconciliation", "citation_event_drafts",
            "evidence_use_citation_links",
            "prose_redraft_report_v31", "prose_redraft_inference_ledger_v31",
            "paragraph_transition_report_v32",
            "heading_revision_report_v31", "verification_notes_routed_v32", "final_finding_audit",
            "concept_fit_final_audit_v32",
            "final_discourse_review", "final_discourse_sha256_v26", "completion_gate",
            "v26_final_judgement", "authoring_result_status", "human_revision_decision",
            "human_revision_items", "human_approval_required", "human_review",
            "rendered_structure_v26", "submission_package", "authoring_submission_revalidation",
            "authoring_review_id", "final_artifact_id", "final_path", "review_report_path",
            "review_report_artifact_id", "target_revision_id", "completion_status",
            "conditional_success", "authoring_reviewable_non_ready_persisted",
        }
        prefixes = ("authoring_proposal_", "footnoted_", "footnote_",
                    "evidence_footnote_", "submission_validation_")
        if boundary == "audit_final_findings":
            from fukamiru.revisions.prose_redraft import digest, validate_handoff

            sha = digest(context["proposed_html"])
            if (context.get("frozen_candidate_v26") or {}).get("html_sha256") != sha:
                raise FukamiruError("Audit restart requires the unchanged frozen HTML")
            if not context.get("prose_redraft_report_v31"):
                raise FukamiruError("Audit restart requires a validated prose handoff")
            validate_handoff(context)
            raw_output = json.loads(source["output_json"] or "{}")
            latest = PayloadStore(self.connection).unpack({
                key: raw_output[key] for key in ("proposed_html",) if key in raw_output
            })
            if latest.get("proposed_html") != context["proposed_html"]:
                raise FukamiruError("Audit restart snapshot differs from the saved final HTML")
            downstream = {
                "final_finding_audit", "concept_fit_final_audit_v32", "v26_final_judgement",
                "completion_gate", "authoring_result_status", "submission_package",
                "authoring_submission_revalidation", "authoring_review_id", "final_artifact_id",
                "final_path", "review_report_path", "review_report_artifact_id",
                "target_revision_id", "completion_status", "conditional_success",
                "authoring_reviewable_non_ready_persisted",
            }
            prefixes = ("authoring_proposal_", "submission_validation_")
        removed = {key: value for key, value in context.items()
                   if key in downstream or key.startswith(prefixes)}
        restarted = {key: value for key, value in context.items() if key not in removed}
        restarted.pop("authoring_run_id", None)
        restarted["project_id"] = str(source["project_id"])
        restarted["authoring_workflow_policy"] = authoring_policy(workflow_version)
        lineage = {
            "policy": (
                "authoring-frozen-final-audit-restart-v1" if boundary == "audit_final_findings"
                else "authoring-pre-footnote-boundary-restart-v1"
            ),
            "source_run_id": source_run_id, "snapshot_node_run_id": str(row["id"]),
            "restart_node_id": boundary, "source_plan_id": context["plan_id"],
            "source_html_sha256": hashlib.sha256(context["proposed_html"].encode()).hexdigest(),
            "discarded_result_hashes": {
                key: hashlib.sha256(_json_dump(value).encode()).hexdigest()
                for key, value in removed.items()
            },
        }
        restarted["authoring_tail_restart_lineage"] = lineage
        if boundary == "render_evidence_footnotes":
            restarted["authoring_render_restart_lineage"] = lineage
        created = self.create(
            workflow_name=MANUSCRIPT_GENERATOR, workflow_version="32",
            entry_node=boundary, project_id=str(source["project_id"]),
            inputs=PayloadStore(self.connection).pack(restarted), resource_lane="ingestion_cpu",
        )
        event = EventRepository(self.connection).append(
            created.run_id, ("run.authoring_audit_restart_created"
                             if boundary == "audit_final_findings"
                             else "run.authoring_render_restart_created"),
            object_type="run", object_id=created.run_id, payload=lineage,
        )
        return AuthoringContractRestartCreated(
            run_id=created.run_id, event_id=event.event_id, snapshot_run_id=source_run_id,
            snapshot_node_run_id=str(row["id"]), plan_id=str(context["plan_id"]),
        )

    def get(self, run_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise FukamiruError(f"Unknown run: {run_id}")
        result = dict(row)
        for key in ("input_json", "output_json", "error_json"):
            raw = result.pop(key)
            result[key.removesuffix("_json")] = (
                PayloadStore(self.connection).unpack(json.loads(raw)) if raw else None
            )
        return result

    def list_progress(
        self,
        *,
        limit: int = 25,
        active_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Return independently refreshable summaries for multiple recent Runs."""
        where = "WHERE status IN ('queued', 'running', 'waiting_human')" if active_only else ""
        rows = self.connection.execute(
            f"""
            SELECT id, project_id, workflow_name, workflow_version, status,
                   input_json, output_json, created_at, started_at, completed_at,
                   error_json
            FROM runs
            {where}
            ORDER BY
                CASE status
                    WHEN 'running' THEN 0
                    WHEN 'queued' THEN 1
                    WHEN 'waiting_human' THEN 2
                    ELSE 3
                END,
                COALESCE(started_at, created_at) DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        if not rows:
            return []
        runs: list[dict[str, Any]] = []
        for row in rows:
            run = dict(row)
            raw_input = run.pop("input_json")
            raw_output = run.pop("output_json")
            raw_error = run.pop("error_json")
            run["input"] = json.loads(raw_input) if raw_input else None
            run["output"] = (
                PayloadStore(self.connection).unpack(json.loads(raw_output)) if raw_output else None
            )
            run["error"] = json.loads(raw_error) if raw_error else None
            runs.append(run)
        run_ids = [str(run["id"]) for run in runs]
        placeholders = ",".join("?" for _ in run_ids)
        active_nodes: dict[str, list[dict[str, Any]]] = {run_id: [] for run_id in run_ids}
        for row in self.connection.execute(
            f"""
            SELECT run_id, id, node_id, attempt, status, resource_lane,
                   started_at, completed_at, lease_heartbeat_at
            FROM node_runs
            WHERE run_id IN ({placeholders})
              AND status IN ('queued', 'running', 'waiting_human')
            ORDER BY started_at, node_id, attempt
            """,
            run_ids,
        ):
            active_nodes[str(row["run_id"])].append(dict(row))
        terminal_nodes: dict[str, dict[str, Any]] = {}
        failed_run_ids = {str(run["id"]) for run in runs if run["status"] == "failed"}
        if failed_run_ids:
            for row in self.connection.execute(
                f"""
                SELECT run_id, id, node_id, attempt, status, resource_lane,
                       started_at, completed_at, lease_heartbeat_at
                FROM node_runs
                WHERE run_id IN ({placeholders}) AND status = 'failed'
                ORDER BY rowid DESC
                """,
                run_ids,
            ):
                run_id = str(row["run_id"])
                if run_id in failed_run_ids:
                    terminal_nodes.setdefault(run_id, dict(row))
        artifacts: dict[str, list[dict[str, Any]]] = {run_id: [] for run_id in run_ids}
        for row in self.connection.execute(
            f"""
            SELECT run_id, id, artifact_type, media_type, title, created_at
            FROM run_artifacts
            WHERE run_id IN ({placeholders})
            ORDER BY created_at
            """,
            run_ids,
        ):
            artifacts[str(row["run_id"])].append(dict(row))
        metric_names = (
            "translation.batch.total",
            "translation.batch.completed",
            "translation.characters.completed",
            "translation.post_edit.chunk.total",
            "translation.post_edit.chunk.completed",
            "translation.post_edit.characters.completed",
        )
        metric_placeholders = ",".join("?" for _ in metric_names)
        latest_metrics: dict[str, dict[str, dict[str, Any]]] = {run_id: {} for run_id in run_ids}
        for row in self.connection.execute(
            f"""
            SELECT m.run_id, m.metric_name, m.numeric_value, m.unit, m.created_at
            FROM metrics m
            JOIN (
                SELECT run_id, metric_name, MAX(id) AS id
                FROM metrics
                WHERE run_id IN ({placeholders})
                  AND metric_name IN ({metric_placeholders})
                GROUP BY run_id, metric_name
            ) latest ON latest.id = m.id
            """,
            (*run_ids, *metric_names),
        ):
            latest_metrics[str(row["run_id"])][str(row["metric_name"])] = dict(row)
        latest_activity: dict[str, dict[str, dict[str, Any]]] = {run_id: {} for run_id in run_ids}
        activity_types = (
            "ingestion.progress",
            "manuscript.reasoning.progress",
            "manuscript.structure_plan.progress",
            "manuscript.argument_plan.progress",
            "manuscript.evidence.progress",
            "manuscript.evidence_support.progress",
            "manuscript.query_plan.progress",
            "manuscript.final_findings.progress",
            "manuscript.certainty.progress",
            "manuscript.references.progress",
            "manuscript.enumeration_context.progress",
            "manuscript.paragraph_plan.progress",
            "manuscript.rewrite.progress",
            "manuscript.predicate_resolution.progress",
            "manuscript.coherence.progress",
            "manuscript.polish.progress",
            "manuscript.polish_target_detection.progress",
            "manuscript.polish_candidate_generation.progress",
            "manuscript.polish_candidate_selection.progress",
        )
        activity_placeholders = ",".join("?" for _ in activity_types)
        for row in self.connection.execute(
            f"""
            SELECT e.run_id, e.event_type, e.payload_json, e.created_at
            FROM event_log e
            JOIN (
                SELECT run_id, event_type, MAX(event_id) AS event_id
                FROM event_log
                WHERE run_id IN ({placeholders})
                  AND event_type IN ({activity_placeholders})
                GROUP BY run_id, event_type
            ) latest ON latest.event_id = e.event_id
            """,
            (*run_ids, *activity_types),
        ):
            latest_activity[str(row["run_id"])][str(row["event_type"])] = dict(row)
        latest_event_ids = {
            str(row["run_id"]): int(row["event_id"])
            for row in self.connection.execute(
                f"""
                SELECT run_id, MAX(event_id) AS event_id
                FROM event_log WHERE run_id IN ({placeholders}) GROUP BY run_id
                """,
                run_ids,
            )
        }
        return [
            {
                "run": run,
                "active_nodes": active_nodes[str(run["id"])],
                "current_nodes": active_nodes[str(run["id"])]
                or ([terminal_nodes[str(run["id"])]] if str(run["id"]) in terminal_nodes else []),
                "artifacts": artifacts[str(run["id"])],
                "progress": self._summary_progress(
                    run,
                    latest_metrics[str(run["id"])],
                    latest_activity[str(run["id"])],
                    active_nodes[str(run["id"])],
                ),
                "patch_processing": self._proofreading_patch_progress(
                    run,
                    active_nodes[str(run["id"])]
                    or (
                        [terminal_nodes[str(run["id"])]] if str(run["id"]) in terminal_nodes else []
                    ),
                ),
                "latest_event_id": latest_event_ids.get(str(run["id"]), 0),
            }
            for run in runs
        ]

    def _proofreading_patch_progress(
        self,
        run: dict[str, Any],
        current_nodes: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Project canonical proofreading rows and events into a compact Run status."""

        if not (
            is_manuscript_generator_workflow(str(run.get("workflow_name")))
            or run.get("workflow_name") == "manuscript_rewrite"
        ):
            return None
        run_id = str(run["id"])
        aggregate = self.connection.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN selected = 1 THEN 1 ELSE 0 END) AS selected,
                   SUM(CASE WHEN applied = 1 THEN 1 ELSE 0 END) AS applied,
                   MAX(updated_at) AS updated_at
            FROM manuscript_proofreading_patches
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        status_counts = {
            str(row["status"]): int(row["count"])
            for row in self.connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM manuscript_proofreading_patches
                WHERE run_id = ?
                GROUP BY status
                ORDER BY status
                """,
                (run_id,),
            )
        }
        event = self.connection.execute(
            """
            SELECT event_type, payload_json, created_at
            FROM event_log
            WHERE run_id = ? AND event_type IN ({})
            ORDER BY event_id DESC LIMIT 1
            """.format(",".join("?" for _ in _PROOFREADING_EVENT_STAGES)),
            (run_id, *_PROOFREADING_EVENT_STAGES),
        ).fetchone()
        event_type = str(event["event_type"]) if event is not None else None
        event_payload = json.loads(str(event["payload_json"])) if event is not None else {}
        current_node_ids = {str(node.get("node_id") or "") for node in current_nodes}
        patch_node_active = bool(current_node_ids.intersection(_PROOFREADING_NODE_IDS))
        total = int(aggregate["total"] or 0) if aggregate is not None else 0
        if run.get("status") == "failed" and patch_node_active:
            state = "failed"
        elif patch_node_active and run.get("status") in {"queued", "running", "waiting_human"}:
            state = "running"
        elif (
            event_type == "manuscript.proofreading.shadow_published"
            or run.get("status") == "completed"
        ):
            state = "completed"
        elif event_type or total:
            state = "waiting"
        else:
            state = "pending"
        return {
            "applicable": True,
            "state": state,
            "stage": _PROOFREADING_EVENT_STAGES.get(event_type),
            "event_type": event_type,
            "event": event_payload,
            "total": total,
            "selected": int(aggregate["selected"] or 0) if aggregate is not None else 0,
            "applied": int(aggregate["applied"] or 0) if aggregate is not None else 0,
            "status_counts": status_counts,
            "empty": total == 0 and state == "completed",
            "updated_at": (
                str(aggregate["updated_at"])
                if aggregate is not None and aggregate["updated_at"]
                else str(event["created_at"])
                if event is not None
                else None
            ),
        }

    @staticmethod
    def _summary_progress(
        run: dict[str, Any],
        metrics: dict[str, dict[str, Any]],
        activity: dict[str, dict[str, Any]],
        active_nodes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        draft_total = metrics.get("translation.batch.total")
        post_edit_total = metrics.get("translation.post_edit.chunk.total")
        post_edit_current = post_edit_total is not None and (
            draft_total is None
            or str(post_edit_total["created_at"]) >= str(draft_total["created_at"])
        )
        if post_edit_current:
            kind = "llm_jp_post_edit_chunks"
            phase = "llm_jp_post_edit"
            total_metric = post_edit_total
            completed_metric = metrics.get("translation.post_edit.chunk.completed")
            characters_metric = metrics.get("translation.post_edit.characters.completed")
        else:
            kind = "translation_batches"
            phase = "draft_translation"
            total_metric = draft_total
            completed_metric = metrics.get("translation.batch.completed")
            characters_metric = metrics.get("translation.characters.completed")
        total = int(total_metric["numeric_value"]) if total_metric else None
        completed = int(completed_metric["numeric_value"]) if completed_metric else None
        updated = [
            str(item["created_at"])
            for item in (total_metric, completed_metric, characters_metric)
            if item is not None
        ]
        payload: dict[str, Any] = {
            "kind": kind if total is not None else None,
            "phase": phase if total is not None else None,
            "completed": completed,
            "total": total,
            "percentage": (
                round(min(100.0, max(0.0, completed * 100.0 / total)), 1)
                if total and completed is not None
                else None
            ),
            "current": min(completed + 1, total) if total and completed is not None else None,
            "completed_characters": (
                int(characters_metric["numeric_value"]) if characters_metric else None
            ),
            "updated_at": max(updated) if updated else None,
        }
        if total is not None:
            return RunRepository._apply_active_progress_phase(payload, active_nodes)
        ingestion = activity.get("ingestion.progress")
        if ingestion is not None:
            event_payload = json.loads(str(ingestion["payload_json"]))
            activity_completed = event_payload.get("completed")
            activity_total = event_payload.get("total")
            return RunRepository._apply_active_progress_phase(
                {
                    **event_payload,
                    "kind": "ingestion_activity",
                    "percentage": (
                        round(
                            min(
                                100.0,
                                float(activity_completed) * 100.0 / float(activity_total),
                            ),
                            1,
                        )
                        if activity_total and activity_completed is not None
                        else None
                    ),
                    "current": activity_completed,
                    "updated_at": str(ingestion["created_at"]),
                    "completed_characters": None,
                },
                active_nodes,
            )
        if str(run["workflow_name"]) == "manuscript_rewrite":
            phase_events = {
                "manuscript.reasoning.progress": (
                    "manuscript_reasoning_batches",
                    "review_manuscript_reasoning",
                ),
                "manuscript.structure_plan.progress": (
                    "manuscript_structure_plan_passes",
                    "plan_manuscript_structure",
                ),
                "manuscript.argument_plan.progress": (
                    "manuscript_argument_processes",
                    "plan_manuscript_structure",
                ),
                "manuscript.evidence.progress": (
                    "manuscript_research_queries",
                    "resolve_manuscript_evidence",
                ),
                "manuscript.evidence_support.progress": (
                    "manuscript_evidence_support",
                    "validate_evidence_support",
                ),
                "manuscript.query_plan.progress": (
                    "manuscript_query_plan",
                    "plan_research_queries",
                ),
                "manuscript.certainty.progress": (
                    "manuscript_certainty",
                    "calibrate_manuscript_certainty",
                ),
                "manuscript.final_findings.progress": (
                    "manuscript_final_findings",
                    "persist_rewrite_proposal",
                ),
                "manuscript.references.progress": (
                    "manuscript_reference_entries",
                    "resolve_reference_metadata",
                ),
                "manuscript.enumeration_context.progress": (
                    "manuscript_enumeration_batches",
                    "resolve_enumeration_context",
                ),
                "manuscript.paragraph_plan.progress": (
                    "manuscript_paragraph_plan_passes",
                    "plan_semantic_paragraphs",
                ),
                "manuscript.rewrite.progress": (
                    "manuscript_rewrite_batches",
                    "draft_rewrite",
                ),
                "manuscript.predicate_resolution.progress": (
                    "manuscript_predicate_candidates",
                    "resolve_manuscript_predicate_relations",
                ),
                "manuscript.coherence.progress": (
                    "manuscript_coherence_passes",
                    "validate_rewrite_coherence",
                ),
                "manuscript.polish.progress": (
                    "manuscript_polish_chunks",
                    "polish_manuscript_japanese",
                ),
                "manuscript.polish_target_detection.progress": (
                    "manuscript_polish_targets",
                    "detect_japanese_polish_targets",
                ),
                "manuscript.polish_candidate_generation.progress": (
                    "manuscript_polish_candidates",
                    "polish_manuscript_japanese",
                ),
                "manuscript.polish_candidate_selection.progress": (
                    "manuscript_polish_selection",
                    "select_japanese_polish_candidates",
                ),
            }
            candidates = [
                (event_type, activity[event_type])
                for event_type in phase_events
                if event_type in activity
            ]
            latest = max(
                candidates,
                key=lambda item: str(item[1]["created_at"]),
                default=None,
            )
            if latest is None:
                return RunRepository._apply_active_progress_phase(payload, active_nodes)
            event_type, event = latest
            kind, phase = phase_events[event_type]
            event_payload = json.loads(str(event["payload_json"]))
            phase = str(event_payload.get("phase") or phase)
            rewrite_completed = int(event_payload.get("completed") or 0)
            rewrite_total = int(event_payload.get("total") or 0)
            return RunRepository._apply_active_progress_phase(
                {
                    **event_payload,
                    "kind": kind,
                    "phase": phase,
                    "percentage": (
                        round(min(100.0, rewrite_completed * 100.0 / rewrite_total), 1)
                        if rewrite_total
                        else None
                    ),
                    "current": rewrite_completed,
                    "updated_at": str(event["created_at"]),
                    "completed_characters": None,
                },
                active_nodes,
            )
        return RunRepository._apply_active_progress_phase(payload, active_nodes)

    @staticmethod
    def _apply_active_progress_phase(
        progress: dict[str, Any],
        active_nodes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Prefer the actual active node over a shared metric family's fallback phase."""

        active = next(
            (
                node
                for status in ("running", "waiting_human", "queued")
                for node in active_nodes
                if str(node.get("status")) == status
            ),
            None,
        )
        if active is None:
            return progress
        node_id = str(active.get("node_id") or "")
        if progress.get("kind") == "translation_batches" and node_id in {
            "draft_translation",
            "repair_translation",
        }:
            progress["phase"] = node_id
        elif str(progress.get("kind") or "").startswith("manuscript_"):
            allowed_nodes = {
                "manuscript_reasoning_batches": {"review_manuscript_reasoning"},
                "manuscript_structure_plan_passes": {"plan_manuscript_structure"},
                "manuscript_argument_processes": {"plan_manuscript_structure"},
                "manuscript_research_queries": {"resolve_manuscript_evidence"},
                "manuscript_evidence_support": {"validate_evidence_support"},
                "manuscript_query_plan": {"plan_research_queries"},
                "manuscript_certainty": {"calibrate_manuscript_certainty"},
                "manuscript_final_findings": {"persist_rewrite_proposal"},
                "manuscript_reference_entries": {"resolve_reference_metadata"},
                "manuscript_enumeration_batches": {"resolve_enumeration_context"},
                "manuscript_paragraph_plan_passes": {"plan_semantic_paragraphs"},
                "manuscript_rewrite_batches": {"draft_rewrite"},
                "manuscript_predicate_candidates": {"resolve_manuscript_predicate_relations"},
                "manuscript_coherence_passes": {
                    "validate_rewrite_coherence",
                    "validate_final_coherence",
                },
                "manuscript_polish_chunks": {"polish_manuscript_japanese"},
                "manuscript_polish_targets": {"detect_japanese_polish_targets"},
                "manuscript_polish_candidates": {"polish_manuscript_japanese"},
                "manuscript_polish_selection": {"select_japanese_polish_candidates"},
            }.get(str(progress.get("kind")))
            if allowed_nodes and node_id in allowed_nodes:
                progress["phase"] = node_id
            elif allowed_nodes:
                progress.update(
                    {
                        "kind": None,
                        "phase": node_id or None,
                        "completed": None,
                        "total": None,
                        "percentage": None,
                        "current": None,
                        "completed_characters": None,
                    }
                )
        elif progress.get("phase") is None:
            progress["phase"] = node_id or None
        return progress

    def cancel(self, run_id: str, *, reason: str = "Cancelled by operator") -> dict[str, Any]:
        """Atomically cancel a run and every claim that could otherwise be recovered."""
        now = utc_now()
        error = {"type": "RunCancelled", "message": reason}
        encoded_error = _json_dump(error)
        self.connection.execute("BEGIN IMMEDIATE")
        run = self.connection.execute(
            "SELECT workflow_name, status FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        if run is None:
            self.connection.rollback()
            raise FukamiruError(f"Unknown run: {run_id}")
        if str(run["status"]) in {"completed", "failed", "cancelled"}:
            self.connection.rollback()
            return self.get(run_id)
        nodes = self.connection.execute(
            """
            SELECT id, node_id FROM node_runs
            WHERE run_id = ? AND status IN ('queued', 'running', 'waiting_human')
            """,
            (run_id,),
        ).fetchall()
        self.connection.execute(
            """
            UPDATE node_runs
            SET status = 'cancelled', error_json = ?, completed_at = ?,
                lease_owner = NULL, lease_acquired_at = NULL,
                lease_heartbeat_at = NULL, lease_expires_at = NULL
            WHERE run_id = ? AND status IN ('queued', 'running', 'waiting_human')
            """,
            (encoded_error, now, run_id),
        )
        self.connection.execute(
            """
            UPDATE human_reviews SET status = 'cancelled', resolved_at = ?
            WHERE run_id = ? AND status = 'open'
            """,
            (now, run_id),
        )
        self.connection.execute(
            """
            UPDATE runs
            SET status = 'cancelled', error_json = ?, completed_at = ?
            WHERE id = ?
            """,
            (encoded_error, now, run_id),
        )
        if str(run["workflow_name"]) == "corpus_sync":
            self.connection.execute(
                """
                UPDATE corpus_sync_batches SET status = 'failed', updated_at = ?
                WHERE run_id = ? AND status != 'completed'
                """,
                (now, run_id),
            )
        self.connection.execute(
            """
            UPDATE pdf_ocr_jobs
            SET status = 'cancelled', completed_at = ?, updated_at = ?
            WHERE run_id = ? AND status IN ('needs_language', 'queued', 'processing')
            """,
            (now, now, run_id),
        )
        self.connection.commit()
        events = EventRepository(self.connection)
        for node in nodes:
            events.append(
                run_id,
                "node.cancelled",
                object_type="node_run",
                object_id=str(node["id"]),
                payload={"node_id": str(node["node_id"]), "reason": reason},
            )
        events.append(
            run_id,
            "run.cancelled",
            object_type="run",
            object_id=run_id,
            payload={"status": "cancelled", "reason": reason},
        )
        return self.get(run_id)

    def replay_failed_v26_structure(self, run_id: str) -> dict[str, Any]:
        """Replay the exact failed V26 structure node, retaining its identity and input.

        The superseded failure remains in the append-only event log.  No prior
        completed node or workflow version is touched.
        """
        now = utc_now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            run = self.connection.execute(
                "SELECT workflow_name,workflow_version,status,error_json,completed_at "
                "FROM runs WHERE id=?",
                (run_id,),
            ).fetchone()
            nodes = self.connection.execute(
                "SELECT id,node_id,attempt,status,input_json,error_json,completed_at "
                "FROM node_runs WHERE run_id=? ORDER BY rowid",
                (run_id,),
            ).fetchall()
            failed = [node for node in nodes if node["status"] == "failed"]
            if (
                run is None
                or run["workflow_name"] != "manuscript_rewrite"
                or run["workflow_version"] != "26"
                or run["status"] != "failed"
                or len(failed) != 1
                or failed[0]["node_id"] != "validate_rendered_structure"
                or any(node["status"] not in {"completed", "failed"} for node in nodes)
                or not any(
                    node["node_id"] == "validate_reference_quality"
                    and node["status"] == "completed"
                    for node in nodes
                )
                or self.connection.execute(
                    "SELECT COUNT(*) FROM manuscript_rewrite_proposals WHERE run_id=?",
                    (run_id,),
                ).fetchone()[0]
            ):
                raise FukamiruError("Run is not an isolated failed V26 structure replay")
            node = failed[0]
            error = json.loads(str(node["error_json"] or "{}"))
            if (
                error.get("type") != "CoherenceResolutionRequired"
                or not str(error.get("message") or "").startswith("rendered_structure.invalid:")
                or not isinstance(
                    PayloadStore(self.connection)
                    .unpack(json.loads(str(node["input_json"] or "{}")))
                    .get("proposed_html"),
                    str,
                )
            ):
                raise FukamiruError("Failed V26 structure node has no replayable input")
            self.connection.execute(
                "INSERT INTO event_log(run_id,event_type,object_type,object_id,"
                "payload_json,created_at) "
                "VALUES (?,'run.failed_node_replayed','node_run',?,?,?)",
                (
                    run_id,
                    node["id"],
                    _json_dump(
                        {
                            "node_id": node["node_id"],
                            "attempt": node["attempt"],
                            "previous_run_error": json.loads(str(run["error_json"] or "{}")),
                            "previous_node_error": error,
                            "previous_run_completed_at": run["completed_at"],
                            "previous_node_completed_at": node["completed_at"],
                            "workflow_version": "26",
                            "policy": "manuscript-final-v26.2",
                        }
                    ),
                    now,
                ),
            )
            self.connection.execute(
                "UPDATE node_runs SET status='queued', started_at=NULL, completed_at=NULL, "
                "error_json=NULL, output_json=NULL, lease_owner=NULL, lease_acquired_at=NULL, "
                "lease_heartbeat_at=NULL, lease_expires_at=NULL "
                "WHERE id=? AND status='failed'",
                (node["id"],),
            )
            self.connection.execute(
                "UPDATE runs SET status='queued', error_json=NULL, completed_at=NULL, "
                "output_json=NULL WHERE id=? AND status='failed'",
                (run_id,),
            )
            self.connection.commit()
            return {
                "run_id": run_id,
                "node_run_id": str(node["id"]),
                "node_id": str(node["node_id"]),
                "attempt": int(node["attempt"]),
                "status": "queued",
            }
        except Exception:
            self.connection.rollback()
            raise

    def replay_failed_authoring_node(
        self, run_id: str, *, verified_book_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Replay one eligible terminal authoring node without rerunning ancestors."""

        now = utc_now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            run = self.connection.execute(
                "SELECT workflow_name,workflow_version,status,error_json,completed_at "
                "FROM runs WHERE id=?",
                (run_id,),
            ).fetchone()
            nodes = self.connection.execute(
                "SELECT id,node_id,attempt,status,input_json,error_json,completed_at "
                "FROM node_runs WHERE run_id=? ORDER BY rowid",
                (run_id,),
            ).fetchall()
            failed = [node for node in nodes if node["status"] == "failed"]
            cancelled = [node for node in nodes if node["status"] == "cancelled"]
            workflow_version = str(run["workflow_version"]) if run is not None else ""
            cancelled_repair = bool(
                run is not None
                and run["status"] == "cancelled"
                and len(cancelled) == 1
                and cancelled[0]["node_id"] == "repair_failed_sections"
            )
            node = (
                cancelled[0]
                if cancelled_repair
                else failed[-1]
                if failed
                else cancelled[-1]
                if len(cancelled) == 1
                else None
            )
            if (
                run is None
                or not is_manuscript_generator_workflow(run["workflow_name"])
                or workflow_version not in {"30", "31", "32"}
                or node is None
            ):
                raise FukamiruError("Run is not an isolated failed authoring replay")
            error = json.loads(str(node["error_json"] or "{}"))
            unpacked = PayloadStore(self.connection).unpack(
                json.loads(str(node["input_json"] or "{}"))
            )
            prior_replay_rows = self.connection.execute(
                "SELECT payload_json,created_at FROM event_log WHERE run_id=? "
                "AND object_id=? AND event_type='run.failed_authoring_node_replayed' "
                "ORDER BY rowid",
                (run_id, str(node["id"])),
            ).fetchall()
            prior_replay_payloads = [
                json.loads(str(item["payload_json"] or "{}")) for item in prior_replay_rows
            ]
            prior_replay_policies = [
                str(item.get("policy") or "") for item in prior_replay_payloads
            ]
            prior_replays = len(prior_replay_rows)
            previous_repair_failures = [
                item for item in failed if item["node_id"] == "repair_failed_sections"
            ]
            completed_node_ids = {
                str(item["node_id"]) for item in nodes if item["status"] == "completed"
            }
            verified_metadata_recovery = (
                verified_book_metadata is not None and workflow_version == "32"
                and run["status"] == "failed" and prior_replays == 0
                and str(node["node_id"]) == "render_evidence_footnotes"
                and nodes[-1]["id"] == node["id"]
                and error.get("type") == "FukamiruError"
                and error.get("message") == (
                    "Footnote bibliography metadata requires review: author_unconfirmed"
                )
                and sorted(int(item["attempt"]) for item in failed) in ([1], [1, 2])
                and all(
                    item["status"] == "completed" or (
                        item["status"] == "failed"
                        and item["node_id"] == node["node_id"]
                        and item["input_json"] == node["input_json"]
                        and json.loads(str(item["error_json"] or "{}")) == error
                    ) for item in nodes
                )
                and self.connection.execute(
                    "SELECT COUNT(*) FROM event_log WHERE run_id=? AND "
                    "event_type='run.failed_authoring_node_replayed' AND "
                    "json_extract(payload_json,'$.policy')=?",
                    (run_id, "authoring-v32-verified-bibliography-recovery-v1"),
                ).fetchone()[0] == 0
            )
            correction_audit = None
            if verified_book_metadata is not None:
                if not verified_metadata_recovery:
                    raise FukamiruError("Run is not an isolated bibliography metadata repair")
                from fukamiru.revisions.verified_bibliography import verified_book_reference

                references = list(unpacked.get("reference_entries") or [])
                matches = [index for index, reference in enumerate(references)
                           if isinstance(reference, dict) and reference.get("reference_id")
                           == verified_book_metadata.get("reference_id")]
                if len(matches) != 1:
                    raise FukamiruError("Verified reference is missing or ambiguous")
                index = matches[0]
                before_reference = references[index]
                references[index] = verified_book_reference(
                    before_reference, verified_book_metadata, checked_at=now,
                )
                correction_audit = {
                    "reference_id": verified_book_metadata["reference_id"],
                    "source_kind": verified_book_metadata["source_kind"],
                    "source_url": verified_book_metadata["source_url"], "checked_at": now,
                    "metadata": verified_book_metadata,
                    "previous_reference_sha256": hashlib.sha256(
                        _json_dump(before_reference).encode()).hexdigest(),
                    "previous_reference_metadata": {
                        key: before_reference.get(key) for key in (
                            "title", "author", "authors", "translators", "publisher", "year",
                            "isbn", "publication_language", "record_type", "bibliography_people",
                            "provenance", "resolution_status", "canonicalization_status",
                            "source_import_title",
                        )
                    },
                    "previous_node_input_json": str(node["input_json"]),
                }
                corrected = {**unpacked, "reference_entries": references,
                             "verified_bibliography_metadata_corrections": [
                                 *list(unpacked.get("verified_bibliography_metadata_corrections")
                                       or []),
                                 {key: value for key, value in correction_audit.items()
                                  if key != "previous_node_input_json"},
                             ]}
                packed = PayloadStore(self.connection).pack(corrected)
                self.connection.execute("UPDATE node_runs SET input_json=? WHERE id=?",
                                        (_json_dump(packed), node["id"]))
            typed_source_recovery = (
                workflow_version == "32" and run["status"] == "failed"
                and str(node["node_id"]) == "bounded_research_retry"
                and nodes[-1]["id"] == node["id"] and prior_replays == 0
                and error.get("type") == "IntegrityError"
                and error.get("message") == (
                    "CHECK constraint failed: source_type IN ('local_rag', 'online_paper')"
                )
                and "validate_evidence_support" in completed_node_ids
                and all(item["id"] == node["id"] or item["status"] == "completed"
                        for item in nodes)
                and self.connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version=?",
                    ("0054_typed_manuscript_research_sources",),
                ).fetchone() is not None
            )
            closed_failure_history = all(
                item["id"] == node["id"] or item["status"] == "completed"
                or (item["status"] == "failed" and any(
                    later["node_id"] == item["node_id"] and later["status"] == "completed"
                    for later in nodes[index + 1:]
                )) for index, item in enumerate(nodes)
            )
            reader_brief_excerpt_recovery = (
                verified_book_metadata is None and workflow_version in {"31", "32"}
                and run["status"] == "failed" and prior_replays == 0
                and str(node["node_id"]) == "draft_rewrite"
                and nodes[-1]["id"] == node["id"]
                and error.get("type") == "NonRetryableFukamiruError"
                and error.get("message") == (
                    "V31 reader brief marginal inference excerpts do not match"
                )
                and closed_failure_history
                and self.connection.execute(
                    "SELECT 1 FROM llm_result_checkpoints WHERE run_id=? AND node_run_id=? "
                    "AND stage='audit_reader_brief_v31' AND status='accepted' LIMIT 1",
                    (run_id, str(node["id"])),
                ).fetchone() is not None
                and self.connection.execute(
                    "SELECT COUNT(*) FROM event_log WHERE run_id=? AND "
                    "event_type='run.failed_authoring_node_replayed' AND "
                    "json_extract(payload_json,'$.policy')=?",
                    (run_id, "authoring-v31-reader-brief-excerpt-recovery-v1"),
                ).fetchone()[0] == 0
            )
            initial_name_recovery = False
            handoff_prefix = "V31 role draft JSON handoff violation: "
            handoff_message = str(error.get("message") or "")
            handoff_problems = handoff_message.removeprefix(handoff_prefix).split("; ")
            if (
                verified_book_metadata is None and workflow_version in {"31", "32"}
                and run["status"] == "failed" and node["node_id"] == "draft_rewrite"
                and nodes[-1]["id"] == node["id"] and closed_failure_history
                and prior_replay_policies in (
                    [], ["authoring-v31-reader-brief-excerpt-recovery-v1"],
                )
                and error.get("type") == "NonRetryableFukamiruError"
                and handoff_message.startswith(handoff_prefix)
                and all(re.fullmatch(
                    r"section [\w.:-]+ scholarship evidence [\w.:-]+ has no semantically "
                    r"confirmed attribution predicate \(classification=unclear\)", problem,
                ) for problem in handoff_problems)
            ):
                saved_audit = self.connection.execute(
                    "SELECT result_json FROM llm_result_checkpoints WHERE run_id=? "
                    "AND node_run_id=? AND stage='scholarship_attribution_judgment_v31' "
                    "AND status='accepted' ORDER BY created_at DESC LIMIT 1",
                    (run_id, str(node["id"])),
                ).fetchone()
                saved_result = (PayloadStore(self.connection).unpack(json.loads(saved_audit[0]))
                                if saved_audit is not None else {})
                judgments = (saved_result.get("judgments", [])
                             if isinstance(saved_result, dict) else [])
                if not isinstance(judgments, list):
                    judgments = []
                unclear = [item for item in judgments if isinstance(item, dict)
                           and item.get("classification") == "unclear"]
                initial_name_recovery = (
                    len(unclear) == len(handoff_problems)
                    and len({item.get("candidate_id") for item in unclear}) == len(unclear)
                    and all(re.search(r"「[^」]*[A-Za-zＡ-Ｚａ-ｚ][.．]」",
                                      str(item.get("reason") or ""))
                            and re.search(r"途切|切れ|truncat", str(item.get("reason") or ""))
                            for item in unclear)
                    and self.connection.execute(
                        "SELECT COUNT(*) FROM event_log WHERE run_id=? AND "
                        "event_type='run.failed_authoring_node_replayed' AND "
                        "json_extract(payload_json,'$.policy')=?",
                        (run_id, "authoring-v31-initial-name-sentence-recovery-v1"),
                    ).fetchone()[0] == 0
                )
            source_use_recovery = False
            source_constraints: list[dict[str, Any]] = []
            role_error = re.fullmatch(
                r"draft evidence ([\w.:-]+) does not support claim role source_fact: "
                r"[\w.:-]+(?:, [\w.:-]+)*", str(error.get("message") or ""),
            )
            same_failures = [item for item in failed if item["node_id"] == node["node_id"]
                             and item["input_json"] == node["input_json"]
                             and json.loads(str(item["error_json"] or "{}")) == error]
            same_ids = {item["id"] for item in same_failures}
            same_attempts = sorted(int(item["attempt"]) for item in same_failures)
            if (
                verified_book_metadata is None and workflow_version in {"31", "32"}
                and run["status"] == "failed" and node["node_id"] == "draft_rewrite"
                and nodes[-1]["id"] == node["id"] and prior_replays == 0
                and error.get("type") == "FukamiruError" and role_error is not None
                and same_attempts and same_attempts[-1] == int(node["attempt"]) <= 3
                and same_attempts == list(range(same_attempts[0], same_attempts[-1] + 1))
                and all(
                    item["id"] in same_ids or item["status"] == "completed"
                    or (item["status"] == "failed" and any(
                        later["node_id"] == item["node_id"] and later["status"] == "completed"
                        for later in nodes[index + 1:]
                    )) for index, item in enumerate(nodes)
                )
                and self.connection.execute(
                    "SELECT COUNT(*) FROM event_log WHERE run_id=? AND "
                    "event_type='run.failed_authoring_node_replayed' AND "
                    "json_extract(payload_json,'$.policy')=?",
                    (run_id, "authoring-v31-source-use-contract-recovery-v1"),
                ).fetchone()[0] == 0
            ):
                try:
                    source_constraints = draft_source_use_constraints(unpacked, {
                        "evidence_treatment": [{"evidence_id": role_error[1],
                                                "prose_treatment": "externally_supported_fact"}],
                    })
                except (FukamiruError, ValueError, KeyError):
                    source_constraints = []
                source_use_recovery = (
                    len(source_constraints) == 1
                    and source_constraints[0]["error"] == error["message"]
                    and bool(source_constraints[0]["permitted_uses"])
                    and bool(source_constraints[0]["fixed_sources"])
                    and all(source["evidence_kind"] == "bibliographic_record"
                            for source in source_constraints[0]["fixed_sources"])
                    and all(use["claim_role"] == "contextual"
                            for use in source_constraints[0]["permitted_uses"])
                )
            generation_recovery = False
            generation_correction = None
            corrected_generation_input = None
            if (
                verified_book_metadata is None and workflow_version in {"31", "32"}
                and run["status"] == "failed" and prior_replays == 0
                and node["node_id"] == "reconcile_candidate_context"
                and nodes[-1]["id"] == node["id"]
                and error == {"type": "FukamiruError",
                              "message": "Proofreading changed claim/source evidence identity"}
                and all(item["id"] == node["id"] or item["status"] == "completed"
                        or (item["status"] == "failed" and any(
                            later["node_id"] == item["node_id"] and later["status"] == "completed"
                            for later in nodes[index + 1:]
                        )) for index, item in enumerate(nodes))
                and self.connection.execute(
                    "SELECT COUNT(*) FROM event_log WHERE run_id=? AND "
                    "event_type='run.failed_authoring_node_replayed' AND "
                    "json_extract(payload_json,'$.policy')=?",
                    (run_id, "authoring-v31-draft-evidence-generation-recovery-v1"),
                ).fetchone()[0] == 0
            ):
                producer = next((item for item in reversed(nodes[:-1])
                                 if item["node_id"] == "draft_rewrite"
                                 and item["status"] == "completed"), None)
                if producer is not None:
                    output = self.connection.execute(
                        "SELECT output_json FROM node_runs WHERE id=?", (producer["id"],),
                    ).fetchone()[0]
                    store = PayloadStore(self.connection)
                    compiled = _v31_stale_draft_evidence_baseline(
                        unpacked, store.unpack(json.loads(producer["input_json"] or "{}")),
                        store.unpack(json.loads(output or "{}")),
                    )
                    if compiled is not None:
                        corrected = refresh_draft_evidence_identity(
                            unpacked, compiled, producer_node_run_id=str(producer["id"]),
                        )
                        corrected_generation_input = _json_dump(store.pack(corrected))
                        generation_correction = {
                            "producer_node_run_id": producer["id"],
                            "previous_node_input": store.pack({"input_json": node["input_json"]}),
                            "previous_identity_hash": unpacked["evidence_identity_hash"],
                            "identity_hash": corrected["evidence_identity_hash"],
                            "corrected_input_sha256": hashlib.sha256(
                                corrected_generation_input.encode()).hexdigest(),
                            "changed_fields": sorted(key for key in set(unpacked) | set(corrected)
                                                     if unpacked.get(key) != corrected.get(key)),
                        }
                        generation_recovery = True
            if (typed_source_recovery or verified_metadata_recovery
                    or reader_brief_excerpt_recovery or initial_name_recovery
                    or source_use_recovery or generation_recovery):
                if verified_metadata_recovery:
                    policy = "authoring-v32-verified-bibliography-recovery-v1"
                elif typed_source_recovery:
                    policy = "authoring-v32-typed-research-source-recovery-v1"
                elif reader_brief_excerpt_recovery:
                    policy = "authoring-v31-reader-brief-excerpt-recovery-v1"
                elif initial_name_recovery:
                    policy = "authoring-v31-initial-name-sentence-recovery-v1"
                elif generation_recovery:
                    policy = "authoring-v31-draft-evidence-generation-recovery-v1"
                else:
                    policy = "authoring-v31-source-use-contract-recovery-v1"
                payload = {
                    "node_id": str(node["node_id"]), "attempt": int(node["attempt"]),
                    "workflow_version": workflow_version,
                    "policy": policy,
                    "previous_node_error": error,
                    "previous_node_completed_at": node["completed_at"],
                    "input_sha256": hashlib.sha256(str(node["input_json"]).encode()).hexdigest(),
                }
                if correction_audit is not None:
                    payload["verified_bibliography_correction"] = correction_audit
                if source_use_recovery:
                    payload["source_use_constraints"] = source_constraints
                if generation_correction is not None:
                    payload["evidence_generation_correction"] = generation_correction
                    self.connection.execute("UPDATE node_runs SET input_json=? WHERE id=?",
                                            (corrected_generation_input, node["id"]))
                self.connection.execute(
                    "INSERT INTO event_log(run_id,event_type,object_type,object_id,"
                    "payload_json,created_at) VALUES(?,?,'node_run',?,?,?)",
                    (run_id, "run.failed_authoring_node_replayed", str(node["id"]),
                     _json_dump(payload), now),
                )
                self.connection.execute(
                    "UPDATE node_runs SET status='queued',started_at=NULL,completed_at=NULL,"
                    "output_json=NULL,error_json=NULL,lease_owner=NULL,lease_acquired_at=NULL,"
                    "lease_heartbeat_at=NULL,lease_expires_at=NULL WHERE id=?",
                    (node["id"],),
                )
                self.connection.execute(
                    "UPDATE runs SET status='queued',error_json=NULL,completed_at=NULL WHERE id=?",
                    (run_id,),
                )
                self.connection.commit()
                return {"run_id": run_id, "node_run_id": str(node["id"]),
                        "node_id": str(node["node_id"]), "attempt": int(node["attempt"]),
                        "status": "queued", "manual_replay_count": prior_replays + 1}
            projection_failures = [item for item in failed
                                   if item["node_id"] == node["node_id"]
                                   and item["input_json"] == node["input_json"]
                                   and json.loads(item["error_json"] or "{}") == error]
            projection_ids = {item["id"] for item in projection_failures}
            projection_attempts = sorted(int(item["attempt"]) for item in projection_failures)
            if (
                verified_book_metadata is None and workflow_version in {"31", "32"}
                and run["status"] == "failed" and prior_replays == 0
                and node["node_id"] == "render_evidence_footnotes"
                and nodes[-1]["id"] == node["id"]
                and projection_attempts and projection_attempts[-1] <= 3
                and projection_attempts == list(range(
                    projection_attempts[0], projection_attempts[-1] + 1))
                and error.get("type") == "FukamiruError"
                and str(error.get("message") or "").startswith(
                    "Authoring footnote needs a unique visible source mention "
                    "in its cited sentence: "
                )
                and all(item["id"] in projection_ids or item["status"] == "completed"
                        or (item["status"] == "failed" and any(
                            later["node_id"] == item["node_id"] and later["status"] == "completed"
                            for later in nodes[index + 1:]
                        )) for index, item in enumerate(nodes))
                and self.connection.execute(
                    "SELECT COUNT(*) FROM event_log WHERE run_id=? AND "
                    "event_type='run.failed_authoring_node_replayed' AND "
                    "json_extract(payload_json,'$.policy')=?",
                    (run_id, "authoring-v31-stale-candidate-generation-recovery-v1"),
                ).fetchone()[0] == 0
            ):
                parent = next((item for item in reversed(nodes[:-1])
                               if item["node_id"] == "reconcile_candidate_context"
                               and item["status"] == "completed"), None)
                corrected = recover_stale_candidate_projection(
                    unpacked, PayloadStore(self.connection).unpack(
                        json.loads(parent["input_json"] or "{}")),
                    error_message=error["message"], source_node_run_id=str(node["id"]),
                ) if parent else None
                if corrected is not None:
                    target_id = str(uuid.uuid4())
                    attempt = max(int(item["attempt"]) for item in nodes
                                  if item["node_id"] == "reconcile_candidate_context") + 1
                    self.connection.execute(
                        "INSERT INTO node_runs(id,run_id,node_id,attempt,status,input_json,"
                        "resource_lane) VALUES(?,?,'reconcile_candidate_context',?,'queued',?,?)",
                        (target_id, run_id, attempt, _json_dump(
                            PayloadStore(self.connection).pack(corrected)), "ingestion_cpu"),
                    )
                    self.connection.execute("UPDATE runs SET status='queued',error_json=NULL,"
                                            "completed_at=NULL WHERE id=?", (run_id,))
                    payload = {"policy": "authoring-v31-stale-candidate-generation-recovery-v1",
                               "node_id": node["node_id"], "attempt": int(node["attempt"]),
                               "previous_node_error": error,
                               "input_sha256": hashlib.sha256(
                                   node["input_json"].encode()).hexdigest(),
                               "target_node_run_id": target_id,
                               "recovery_target_node_id": "reconcile_candidate_context",
                               "cleared_results": corrected["prior_candidate_generations"][-1]}
                    self.connection.execute(
                        "INSERT INTO event_log(run_id,event_type,object_type,object_id,"
                        "payload_json,created_at) VALUES(?,?,'node_run',?,?,?)",
                        (run_id, "run.failed_authoring_node_replayed", node["id"],
                         _json_dump(payload), now),
                    )
                    self.connection.commit()
                    return {"run_id": run_id, "node_run_id": target_id,
                            "node_id": "reconcile_candidate_context", "attempt": attempt,
                            "status": "queued", "manual_replay_count": 1}
            recovered_projection_history = []
            if node["node_id"] in {
                "extract_proofreading_sentences", "render_evidence_footnotes", "finalize_authoring",
            }:
                for row in self.connection.execute(
                    "SELECT payload_json FROM event_log WHERE run_id=? AND "
                    "event_type='run.failed_authoring_node_replayed' AND "
                    "json_extract(payload_json,'$.policy') IN (?,?)",
                    (run_id, "authoring-v31-stale-candidate-generation-recovery-v1",
                     "authoring-v31-empty-citation-projection-recovery-v1"),
                ):
                    history = json.loads(row[0])
                    if any(item["id"] == history.get("target_node_run_id")
                           and item["status"] == "completed" for item in nodes):
                        recovered_projection_history.append(history)

            def old_projection_failure(item: sqlite3.Row) -> bool:
                return any(item["node_id"] == history.get("node_id")
                           and hashlib.sha256(item["input_json"].encode()).hexdigest()
                           == history.get("input_sha256")
                           and json.loads(item["error_json"] or "{}")
                           == history.get("previous_node_error")
                           for history in recovered_projection_history)

            submission_policy = "authoring-v31-submission-marker-recovery-v1"
            if (
                verified_book_metadata is None and workflow_version in {"31", "32"}
                and run["status"] == "failed" and prior_replays == 0
                and nodes[-1]["id"] == node["id"] and node["node_id"] == "finalize_authoring"
                and error == {"type": "ValueError",
                              "message": "authoring run already has a pending proposal"}
                and all(item["id"] == node["id"] or item["status"] == "completed"
                        or (item["status"] == "failed" and (old_projection_failure(item) or any(
                            later["node_id"] == item["node_id"] and later["status"] == "completed"
                            for later in nodes[index + 1:]
                        ))) for index, item in enumerate(nodes))
                and not self.connection.execute(
                    "SELECT 1 FROM event_log WHERE run_id=? AND "
                    "event_type='run.failed_authoring_node_replayed' AND "
                    "json_extract(payload_json,'$.policy')=?", (run_id, submission_policy),
                ).fetchone()
                and self.connection.execute(
                    "SELECT 1 FROM authoring_pending_proposals WHERE run_id=? AND id=? "
                    "AND status='pending'", (run_id, unpacked.get("authoring_proposal_id")),
                ).fetchone()
            ):
                for history in unpacked.get("prior_candidate_generations") or []:
                    if "authoring_submission_revalidation" not in dict(
                        history.get("cleared_fields") or {}):
                        continue
                    source_node = self.connection.execute(
                        "SELECT input_json FROM node_runs WHERE run_id=? AND id=?",
                        (run_id, history.get("source_node_run_id")),
                    ).fetchone()
                    if source_node is None:
                        continue
                    corrected = recover_submission_revalidation(unpacked,
                        PayloadStore(self.connection).unpack(json.loads(source_node[0])),
                        str(history["source_node_run_id"]))
                    if corrected is None:
                        continue
                    marker = corrected["authoring_submission_revalidation"]
                    if not self.connection.execute(
                        "SELECT 1 FROM human_reviews h JOIN review_feedback f ON f.review_id=h.id "
                        "WHERE h.id=? AND h.run_id=? AND h.object_type='authoring_submission' "
                        "AND h.status='edited' AND f.submitted_revision IS NOT NULL",
                        (marker["review_id"], run_id),
                    ).fetchone():
                        continue
                    target_id = str(uuid.uuid4())
                    attempt = int(node["attempt"]) + 1
                    self.connection.execute(
                        "INSERT INTO node_runs(id,run_id,node_id,attempt,status,input_json,"
                        "resource_lane) VALUES(?,?,'finalize_authoring',?,'queued',?,?)",
                        (target_id, run_id, attempt,
                         _json_dump(PayloadStore(self.connection).pack(corrected)),
                         "ingestion_cpu"))
                    payload = {"policy": submission_policy, "node_id": node["node_id"],
                        "attempt": int(node["attempt"]), "previous_node_error": error,
                        "input_sha256": hashlib.sha256(node["input_json"].encode()).hexdigest(),
                        "target_node_run_id": target_id,
                        "recovery_target_node_id": "finalize_authoring",
                        "marker_recovery": corrected[
                            "authoring_submission_revalidation_recoveries"][-1]}
                    self.connection.execute(
                        "INSERT INTO event_log(run_id,event_type,object_type,object_id,"
                        "payload_json,created_at) VALUES(?,?,'node_run',?,?,?)",
                        (run_id, "run.failed_authoring_node_replayed", node["id"],
                         _json_dump(payload), now))
                    self.connection.execute("UPDATE runs SET status='queued',error_json=NULL,"
                                            "completed_at=NULL WHERE id=?", (run_id,))
                    self.connection.commit()
                    return {"run_id": run_id, "node_run_id": target_id,
                            "node_id": "finalize_authoring", "attempt": attempt,
                            "status": "queued", "manual_replay_count": 1}

            qualification_policy = "authoring-v31-rendered-qualification-recovery-v1"
            if (
                verified_book_metadata is None and workflow_version in {"31", "32"}
                and run["status"] == "failed" and prior_replays == 0
                and nodes[-1]["id"] == node["id"]
                and node["node_id"] == "render_evidence_footnotes"
                and error.get("type") == "FukamiruError"
                and all(item["id"] == node["id"] or item["status"] == "completed"
                        or (item["status"] == "failed" and (old_projection_failure(item) or any(
                            later["node_id"] == item["node_id"] and later["status"] == "completed"
                            for later in nodes[index + 1:]
                        ))) for index, item in enumerate(nodes))
                and not self.connection.execute(
                    "SELECT 1 FROM event_log WHERE run_id=? AND "
                    "event_type='run.failed_authoring_node_replayed' AND "
                    "json_extract(payload_json,'$.policy')=?",
                    (run_id, qualification_policy),
                ).fetchone()
            ):
                qualification = rendered_qualification_recovery(
                    unpacked, str(error.get("message") or ""))
                if qualification is not None:
                    payload = {"policy": qualification_policy, "node_id": node["node_id"],
                               "attempt": int(node["attempt"]), "previous_node_error": error,
                               "input_sha256": hashlib.sha256(
                                   node["input_json"].encode()).hexdigest(),
                               "qualification_review": qualification}
                    self.connection.execute(
                        "INSERT INTO event_log(run_id,event_type,object_type,object_id,"
                        "payload_json,created_at) VALUES(?,?,'node_run',?,?,?)",
                        (run_id, "run.failed_authoring_node_replayed", node["id"],
                         _json_dump(payload), now))
                    self.connection.execute("UPDATE node_runs SET status='queued',started_at=NULL,"
                        "completed_at=NULL,error_json=NULL,output_json=NULL,lease_owner=NULL,"
                        "lease_acquired_at=NULL,lease_heartbeat_at=NULL,lease_expires_at=NULL "
                        "WHERE id=?", (node["id"],))
                    self.connection.execute("UPDATE runs SET status='queued',error_json=NULL,"
                                            "completed_at=NULL WHERE id=?", (run_id,))
                    self.connection.commit()
                    return {"run_id": run_id, "node_run_id": str(node["id"]),
                            "node_id": str(node["node_id"]), "attempt": int(node["attempt"]),
                            "status": "queued", "manual_replay_count": 1}

            empty_projection_policy = "authoring-v31-empty-citation-projection-recovery-v1"
            if (
                verified_book_metadata is None and workflow_version in {"31", "32"}
                and run["status"] == "failed" and prior_replays == 0
                and nodes[-1]["id"] == node["id"]
                and node["node_id"] == "render_evidence_footnotes"
                and error.get("type") == "FukamiruError"
                and all(item["id"] == node["id"] or item["status"] == "completed"
                        or (item["status"] == "failed" and (old_projection_failure(item) or any(
                            later["node_id"] == item["node_id"] and later["status"] == "completed"
                            for later in nodes[index + 1:]
                        ))) for index, item in enumerate(nodes))
                and not self.connection.execute(
                    "SELECT 1 FROM event_log WHERE run_id=? AND "
                    "event_type='run.failed_authoring_node_replayed' AND "
                    "json_extract(payload_json,'$.policy')=?",
                    (run_id, empty_projection_policy),
                ).fetchone()
            ):
                corrected = recover_empty_citation_projection(
                    unpacked, error_message=str(error.get("message") or ""),
                    source_node_run_id=str(node["id"]))
                if corrected is not None:
                    target = "reconcile_candidate_context_after_proofreading"
                    target_id = str(uuid.uuid4())
                    attempt = max((int(item["attempt"]) for item in nodes
                                   if item["node_id"] == target), default=0) + 1
                    self.connection.execute(
                        "INSERT INTO node_runs(id,run_id,node_id,attempt,status,input_json,"
                        "resource_lane) VALUES(?,?,?,?,'queued',?,?)",
                        (target_id, run_id, target, attempt,
                         _json_dump(PayloadStore(self.connection).pack(corrected)),
                         "ingestion_cpu"))
                    payload = {"policy": empty_projection_policy, "node_id": node["node_id"],
                               "attempt": int(node["attempt"]), "previous_node_error": error,
                               "input_sha256": hashlib.sha256(
                                   node["input_json"].encode()).hexdigest(),
                               "target_node_run_id": target_id, "recovery_target_node_id": target,
                               "projection_recovery": corrected[
                                   "authoring_empty_citation_projection_recoveries"][-1]}
                    self.connection.execute(
                        "INSERT INTO event_log(run_id,event_type,object_type,object_id,"
                        "payload_json,created_at) VALUES(?,?,'node_run',?,?,?)",
                        (run_id, "run.failed_authoring_node_replayed", node["id"],
                         _json_dump(payload), now))
                    self.connection.execute("UPDATE runs SET status='queued',error_json=NULL,"
                                            "completed_at=NULL WHERE id=?", (run_id,))
                    self.connection.commit()
                    return {"run_id": run_id, "node_run_id": target_id, "node_id": target,
                            "attempt": attempt, "status": "queued", "manual_replay_count": 1}

            if (
                verified_book_metadata is None and workflow_version in {"31", "32"}
                and run["status"] == "failed" and prior_replays == 0
                and nodes[-1]["id"] == node["id"]
                and node["node_id"] == "extract_proofreading_sentences"
                and error == {"type": "FukamiruError",
                              "message": "proofreading.source_checkpoint_changed"}
                and all(item["id"] == node["id"] or item["status"] == "completed"
                        or (item["status"] == "failed" and (old_projection_failure(item) or any(
                            later["node_id"] == item["node_id"] and later["status"] == "completed"
                            for later in nodes[index + 1:]
                        ))) for index, item in enumerate(nodes))
                and self.connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version=?",
                    ("0055_manuscript_proofreading_generations",),
                ).fetchone()
                and self.connection.execute(
                    "SELECT COUNT(*) FROM event_log WHERE run_id=? AND "
                    "event_type='run.failed_authoring_node_replayed' AND "
                    "json_extract(payload_json,'$.policy')=?",
                    (run_id, "authoring-v31-proofreading-generation-recovery-v1"),
                ).fetchone()[0] == 0
            ):
                generation = checkpoint_recovery(
                    self.connection, run_id, str(run["workflow_name"]), workflow_version, unpacked,
                )
                if generation is not None:
                    payload = {"policy": "authoring-v31-proofreading-generation-recovery-v1",
                               "node_id": node["node_id"], "attempt": int(node["attempt"]),
                               "previous_node_error": error,
                               "input_sha256": hashlib.sha256(
                                   node["input_json"].encode()).hexdigest(),
                               "proofreading_generation": generation}
                    self.connection.execute(
                        "INSERT INTO event_log(run_id,event_type,object_type,object_id,"
                        "payload_json,created_at) VALUES(?,?,'node_run',?,?,?)",
                        (run_id, "run.failed_authoring_node_replayed", node["id"],
                         _json_dump(payload), now),
                    )
                    self.connection.execute("UPDATE node_runs SET status='queued',started_at=NULL,"
                        "completed_at=NULL,error_json=NULL,output_json=NULL,lease_owner=NULL,"
                        "lease_acquired_at=NULL,lease_heartbeat_at=NULL,lease_expires_at=NULL "
                        "WHERE id=?", (node["id"],))
                    self.connection.execute("UPDATE runs SET status='queued',error_json=NULL,"
                                            "completed_at=NULL WHERE id=?", (run_id,))
                    self.connection.commit()
                    return {"run_id": run_id, "node_run_id": str(node["id"]),
                            "node_id": str(node["node_id"]), "attempt": int(node["attempt"]),
                            "status": "queued", "manual_replay_count": 1}
            cancellation_event = self.connection.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.cancelled' ORDER BY rowid DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            cancellation_payload = (
                json.loads(str(cancellation_event["payload_json"] or "{}"))
                if cancellation_event is not None
                else {}
            )
            v31_cancelled_self_citation_recovery = bool(
                cancelled_repair
                and workflow_version in {"31", "32"}
                and prior_replays == 0
                and node["attempt"] == 2
                and len(previous_repair_failures) == 1
                and previous_repair_failures[0]["attempt"] == 1
                and all(
                    item in previous_repair_failures or str(item["node_id"]) in completed_node_ids
                    for item in failed
                )
                and nodes[-1]["id"] == node["id"]
                and error.get("type") == "RunCancelled"
                and str(error.get("message") or "") == "Cancelled by operator"
                and cancellation_payload.get("reason") == "Cancelled by operator"
                and isinstance(unpacked, dict)
                and _v31_cancelled_self_citation_repair_is_replayable(
                    json.loads(str(previous_repair_failures[0]["error_json"] or "{}")),
                    unpacked,
                )
            )
            v31_cancelled_semantic_audit_reroute = bool(
                run["status"] in {"cancelled", "failed"}
                and str(node["node_id"]) == "repair_failed_sections"
                and workflow_version in {"31", "32"}
                and int(node["attempt"]) == 4
                and prior_replay_policies
                == [
                    "authoring-v31-provisional-self-citation-repair-v1",
                    "authoring-v31-retired-source-human-review-v1",
                ]
                and sorted(int(item["attempt"]) for item in previous_repair_failures)
                in ([1], [1, 4])
                and nodes[-1]["id"] == node["id"]
                and (
                    (
                        error.get("type") == "RunCancelled"
                        and str(error.get("message") or "") == "Cancelled by operator"
                    )
                    or (
                        error.get("type") == "FukamiruError"
                        and str(error.get("message") or "")
                        == "V31 repair did not prove resolution for findings: "
                        "AF-511c0b779b5d5119, AF-4bb582d81f236411"
                    )
                )
                and cancellation_payload.get("reason") == "Cancelled by operator"
                and dict(unpacked.get("source_preservation_validation") or {}).get("passed") is True
                and bool(unpacked.get("draft_sections"))
                and bool(unpacked.get("audit_findings"))
                and all(
                    str(item.get("category") or "")
                    in {
                        "source_review_note_leak",
                        "over_attribution",
                        "evidence_mismatch",
                        "audit_meta_prose",
                        "claim_scope_overreach",
                    }
                    for item in unpacked.get("audit_findings") or []
                    if isinstance(item, dict)
                )
            )
            latest_classification_row = self.connection.execute(
                "SELECT output_json FROM node_runs WHERE run_id=? "
                "AND node_id='classify_audit_findings' AND status='completed' "
                "ORDER BY rowid DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            latest_classification = (
                PayloadStore(self.connection).unpack(
                    json.loads(str(latest_classification_row["output_json"] or "{}"))
                )
                if latest_classification_row is not None
                else {}
            )
            previous_semantic_reroutes = self.connection.execute(
                "SELECT COUNT(*) FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed' "
                "AND json_extract(payload_json,'$.policy')=?",
                (run_id, "authoring-v31-semantic-audit-review-reroute-v1"),
            ).fetchone()[0]
            v31_audit_join_classification_recovery = bool(
                run["status"] == "failed"
                and workflow_version in {"31", "32"}
                and str(node["node_id"]) == "repair_failed_sections"
                and int(node["attempt"]) == 8
                and error.get("type") == "FukamiruError"
                and str(error.get("message") or "")
                == "automatic repair limit reached for the same finding set"
                and previous_semantic_reroutes == 1
                and not prior_replays
                and sorted(int(item["attempt"]) for item in previous_repair_failures) == [1, 8]
                and nodes[-1]["id"] == node["id"]
                and isinstance(latest_classification, dict)
                and latest_classification.get("proposed_html") == unpacked.get("proposed_html")
                and latest_classification.get("auto_repair_findings") == []
                and bool(latest_classification.get("human_review_findings"))
                and bool(unpacked.get("auto_repair_findings"))
                and dict(unpacked.get("source_preservation_validation") or {}).get("passed") is True
            )
            v31_audit_join_dispatch_recovery = bool(
                run["status"] == "failed"
                and workflow_version in {"31", "32"}
                and str(node["node_id"]) == "repair_failed_sections"
                and int(node["attempt"]) == 9
                and error.get("type") == "FukamiruError"
                and str(error.get("message") or "")
                == "automatic repair limit reached for the same finding set"
                and prior_replay_policies == ["authoring-v31-audit-join-classification-recovery-v1"]
                and previous_semantic_reroutes == 1
                and sorted(int(item["attempt"]) for item in previous_repair_failures) == [1, 9]
                and nodes[-1]["id"] == node["id"]
                and dict(unpacked.get("authoring_recovery_lineage") or {}).get("policy")
                == "authoring-v31-audit-join-classification-recovery-v1"
                and isinstance(latest_classification, dict)
                and latest_classification.get("proposed_html") == unpacked.get("proposed_html")
                and latest_classification.get("auto_repair_findings") == []
                and bool(unpacked.get("auto_repair_findings"))
                and dict(unpacked.get("source_preservation_validation") or {}).get("passed") is True
            )
            cancelled_rag_context_recovery = False
            if (
                run["status"] == "cancelled"
                and node["status"] == "cancelled"
                and workflow_version in {"31", "32"}
                and not failed
                and len(cancelled) == 1
                and all(item["id"] == node["id"] or item["status"] == "completed" for item in nodes)
                and len(prior_replay_rows) == 2
            ):
                prior_replay_event = prior_replay_rows[-1]
                prior_replay_payload = json.loads(str(prior_replay_event["payload_json"] or "{}"))
                previous_node_error = dict(prior_replay_payload.get("previous_node_error") or {})
                previous_error_message = str(previous_node_error.get("message") or "")
                cancelled_event = self.connection.execute(
                    "SELECT payload_json,created_at FROM event_log WHERE run_id=? "
                    "AND event_type='run.cancelled' ORDER BY rowid DESC LIMIT 1",
                    (run_id,),
                ).fetchone()
                cancelled_payload = (
                    json.loads(str(cancelled_event["payload_json"] or "{}"))
                    if cancelled_event is not None
                    else {}
                )
                invocation_rows = self.connection.execute(
                    "SELECT DISTINCT operation_name FROM llm_invocation_observations "
                    "WHERE run_id=? AND created_at>? AND checkpoint_status='miss' "
                    "AND rag_context_characters=0 AND operation_name IN (?,?)",
                    (
                        run_id,
                        str(prior_replay_event["created_at"]),
                        "authoring_draft_introduction_1",
                        "authoring_draft_body_2",
                    ),
                ).fetchall()
                missing_rag_operations = {str(item["operation_name"]) for item in invocation_rows}
                prior_rag_hit_rows = self.connection.execute(
                    "SELECT DISTINCT operation_name FROM llm_invocation_observations "
                    "WHERE run_id=? AND created_at<? AND checkpoint_status='hit' "
                    "AND rag_context_characters>0 AND operation_name IN (?,?)",
                    (
                        run_id,
                        str(prior_replay_event["created_at"]),
                        "authoring_draft_introduction_1",
                        "authoring_draft_body_2",
                    ),
                ).fetchall()
                prior_rag_operations = {str(item["operation_name"]) for item in prior_rag_hit_rows}
                cancelled_rag_context_recovery = bool(
                    error.get("type") == "RunCancelled"
                    and str(error.get("message") or "") == "Cancelled by operator"
                    and cancelled_event is not None
                    and str(cancelled_event["created_at"]) > str(prior_replay_event["created_at"])
                    and cancelled_payload.get("reason") == "Cancelled by operator"
                    and prior_replay_payload.get("policy")
                    == "authoring-v31-citation-anchor-recovery-v1"
                    and prior_replay_payload.get("v31_footnote_anchor_recovery") is True
                    and previous_node_error.get("type") == "NonRetryableFukamiruError"
                    and previous_error_message.startswith(
                        "V31 role draft JSON handoff violation: section "
                    )
                    and previous_error_message.endswith(
                        "has no unique sentence-level footnote anchor"
                    )
                    and missing_rag_operations
                    == {
                        "authoring_draft_introduction_1",
                        "authoring_draft_body_2",
                    }
                    and prior_rag_operations
                    == {
                        "authoring_draft_introduction_1",
                        "authoring_draft_body_2",
                    }
                )
                if cancelled_rag_context_recovery:
                    error = previous_node_error
            v32_verification_routing_recovery = False
            if (workflow_version == "32" and str(node["node_id"]) == "build_argument_graph"
                    and run["status"] == node["status"] == "cancelled"
                    and error.get("type") == "RunCancelled"
                    and error.get("message") == "Cancelled by operator"
                    and prior_replays <= 2
                    and set(prior_replay_policies) <= {
                        "authoring-v32-bounded-concept-repair-v2",
                        "authoring-v32-basis-identity-review-v1",
                    }):
                route_repair = self.connection.execute(
                    "SELECT operation_name,result_json FROM llm_result_checkpoints "
                    "WHERE run_id=? AND node_id='build_argument_graph' "
                    "AND stage='repair_concept_fit_v32' AND status='accepted' "
                    "ORDER BY created_at DESC,rowid DESC LIMIT 1", (run_id,),
                ).fetchone()
                if route_repair:
                    operation = str(route_repair["operation_name"])
                    base = re.sub(r":concept-repair(?::2)?$", "", operation)
                    route_native = self.connection.execute(
                        "SELECT result_json FROM llm_result_checkpoints WHERE run_id=? "
                        "AND node_id='build_argument_graph' AND stage='argument_graph_v31' "
                        "AND operation_name=? AND status='accepted'", (run_id, base),
                    ).fetchone()
                    earlier = self.connection.execute(
                        "SELECT result_json FROM llm_result_checkpoints WHERE run_id=? "
                        "AND node_id='build_argument_graph' AND stage='repair_concept_fit_v32' "
                        "AND operation_name=? AND status='accepted'",
                        (run_id, base + ":concept-repair"),
                    ).fetchone() if operation.endswith(":2") else None
                    if route_native and (not operation.endswith(":2") or earlier):
                        v32_verification_routing_recovery = (
                            _v32_verification_routing_is_replayable(
                                json.loads(route_native["result_json"]),
                                json.loads(route_repair["result_json"]),
                                json.loads(earlier["result_json"]) if earlier else None,
                            )
                        )
            valid_failed_run = run["status"] == "failed" and bool(failed)
            if not (
                valid_failed_run
                or v32_verification_routing_recovery
                or cancelled_rag_context_recovery
                or v31_cancelled_self_citation_recovery
                or v31_cancelled_semantic_audit_reroute
                or v31_audit_join_classification_recovery
                or v31_audit_join_dispatch_recovery
            ):
                raise FukamiruError("Run is not an isolated failed authoring replay")
            error_message = str(error.get("message") or "")
            v31_repair_source_retirement_recovery = bool(
                workflow_version in {"31", "32"}
                and str(node["node_id"]) == "repair_failed_sections"
                and int(node["attempt"]) == 3
                and error.get("type") == "FukamiruError"
                and error_message
                == (
                    "V31 repair did not prove resolution for findings: "
                    "AF-62959996db8441d1, AF-2ef6eb8ac286b64c, "
                    "AF-a2a9c7e5f445dc32"
                )
                and prior_replay_policies == ["authoring-v31-provisional-self-citation-repair-v1"]
                and len(previous_repair_failures) == 2
                and {int(item["attempt"]) for item in previous_repair_failures} == {1, 3}
                and {
                    str(item.get("finding_id") or "")
                    for item in unpacked.get("audit_findings") or []
                    if isinstance(item, dict)
                }
                >= {
                    "AF-62959996db8441d1",
                    "AF-2ef6eb8ac286b64c",
                    "AF-a2a9c7e5f445dc32",
                }
            )
            codex_failure = error_message.startswith(
                f"Codex authoring stage {node['node_id']} failed"
            )
            semantic_coverage_failure = str(
                node["node_id"]
            ) == "plan_semantic_paragraphs" and error_message.startswith(
                "Semantic paragraph plan changed required move coverage:"
            )
            draft_metadata_failure = (
                str(node["node_id"]) == "draft_rewrite"
                and error.get("type") == "NonRetryableFukamiruError"
                and error_message.startswith(
                    "Draft section contract violation after one bounded retry:"
                )
            )
            v31_draft_handoff_failure = (
                workflow_version in {"31", "32"}
                and str(node["node_id"]) == "draft_rewrite"
                and error.get("type") == "NonRetryableFukamiruError"
                and error_message.startswith("V31 role draft JSON handoff violation: ")
                and bool(unpacked.get("plan_id"))
                and bool(unpacked.get("section_contracts"))
                and bool(unpacked.get("argument_nodes"))
                and bool(unpacked.get("research_problem"))
            )
            v31_citation_source_claim_mapping_failure = (
                workflow_version in {"31", "32"}
                and str(node["node_id"]) == "draft_rewrite"
                and error.get("type") in {"FukamiruError", "NonRetryableFukamiruError"}
                and bool(unpacked.get("research_problem"))
                and _v31_citation_source_claim_mapping_is_repairable(error_message, unpacked)
            )
            v31_citation_source_claim_mapping_post_fix_replay = bool(
                workflow_version in {"31", "32"}
                and str(node["node_id"]) == "draft_rewrite"
                and error.get("type") in {"FukamiruError", "NonRetryableFukamiruError"}
                and prior_replays == 1
                and prior_replay_policies
                == ["authoring-v31-single-self-citation-record-correction-v1"]
                and len(prior_replay_payloads) == 1
                and prior_replay_payloads[0].get("node_id") == "draft_rewrite"
                and bool(prior_replay_payloads[0].get("citation_evidence_id"))
                and bool(prior_replay_payloads[0].get("source_record_id"))
                and isinstance(unpacked, dict)
                and _v31_citation_source_claim_mapping_is_repairable(
                    error_message,
                    unpacked,
                    expect_missing_mapping=False,
                )
            )
            footnote_metadata_failure = (
                str(node["node_id"]) == "render_evidence_footnotes"
                and error.get("type") == "FukamiruError"
                and error_message.startswith(
                    "Evidence-linked generated prose has no final bibliography record:"
                )
            )
            v31_aggregated_self_citation_failure = (
                workflow_version in {"31", "32"}
                and str(node["node_id"]) == "render_evidence_footnotes"
                and error.get("type") == "FukamiruError"
                and error_message
                == ("Footnote bibliography metadata requires review: record_kind_unconfirmed")
                and any(
                    isinstance(reference, dict)
                    and reference.get("is_self_authored") is True
                    and reference.get("record_type") == "nonstandard_contribution"
                    and "」、『" in str(reference.get("title") or "")
                    and reference.get("container_title")
                    and reference.get("year")
                    and reference.get("pages")
                    and reference.get("bibliography_people")
                    for reference in unpacked.get("reference_entries") or []
                )
            )
            v31_provisional_bibliography_recovery = (
                workflow_version in {"31", "32"}
                and str(node["node_id"]) == "render_evidence_footnotes"
                and error.get("type") == "FukamiruError"
                and error_message
                == ("Footnote bibliography metadata requires review: record_kind_unconfirmed")
                and prior_replays == 0
                and sum(
                    1
                    for reference in unpacked.get("reference_entries") or []
                    if isinstance(reference, dict)
                    and str(reference.get("reference_id") or "")
                    and str(reference.get("title") or "")
                    and reference.get("resolution_status") == "resolved"
                    and reference.get("record_type") not in {"book", "article", "chapter"}
                    and any(
                        isinstance(person, dict)
                        and person.get("role") == "author"
                        and (str(person.get("family") or "") or str(person.get("literal") or ""))
                        for person in reference.get("bibliography_people") or []
                    )
                )
                == 1
            )
            proposal_revalidation_failure = (
                str(node["node_id"]) == "finalize_authoring"
                and (
                    (
                        error.get("type") == "IntegrityError"
                        and error_message.startswith(
                            "UNIQUE constraint failed: authoring_pending_proposals.run_id"
                        )
                    )
                    or (
                        error.get("type") == "ValueError"
                        and error_message == "authoring run already has a pending proposal"
                    )
                )
                and (
                    isinstance(unpacked.get("authoring_submission_revalidation"), dict)
                    or int(
                        self.connection.execute(
                            "SELECT COUNT(*) FROM event_log WHERE run_id=? AND "
                            "event_type='run.authoring_submission_revalidation_started'",
                            (run_id,),
                        ).fetchone()[0]
                    )
                    > 0
                )
            )
            checkpoint_plan_failure = (
                str(node["node_id"]) == "finalize_authoring"
                and error.get("type") == "ValueError"
                and error_message == "proposal plan does not belong to the target paper"
                and isinstance(unpacked.get("_authoring_checkpoint"), dict)
                and unpacked["_authoring_checkpoint"].get("plan_identity_preserved") is False
            )
            memo_annotation_preservation_failure = (
                str(node["node_id"]) == "decompose_memo"
                and error.get("type") == "NonRetryableFukamiruError"
                and error_message.startswith(
                    "Memo decomposition lost author-supplied source constraints:"
                )
            )
            claim_tree_provenance_failure = (
                str(node["node_id"]) == "build_claim_tree"
                and error.get("type") == "NonRetryableFukamiruError"
                and error_message.startswith(
                    "Claim tree contract violation after one bounded retry: "
                    "claim provenance omitted topic "
                )
                and all(
                    item.startswith("claim provenance omitted topic ")
                    for item in error_message.split(
                        "Claim tree contract violation after one bounded retry: ", 1
                    )[1].split("; ")
                )
            )
            v31_claim_tree_branch_failure = (
                workflow_version in {"31", "32"}
                and str(node["node_id"]) == "build_claim_tree"
                and error.get("type") == "NonRetryableFukamiruError"
                and error_message.startswith(
                    "Claim tree contract violation after one bounded retry: "
                )
                and all(
                    item.startswith("claim provenance omitted topic ")
                    or item.startswith("claim tree needs at least one distinct subclaim branch ")
                    or re.fullmatch(r"topic [A-Za-z0-9_-]+ has no direct subclaim branch", item)
                    is not None
                    or re.fullmatch(
                        r"claim [A-Za-z0-9_-]+ uses unknown proposition IDs: "
                        r"\['research_question_v31'\]",
                        item,
                    )
                    is not None
                    for item in error_message.split(
                        "Claim tree contract violation after one bounded retry: ", 1
                    )[1].split("; ")
                )
            )
            argument_graph_timeout_failure = (
                str(node["node_id"]) == "build_argument_graph"
                and error.get("type") == "FukamiruError"
                and error_message.startswith(
                    "Codex authoring stage build_argument_graph timed out after "
                )
            )
            argument_graph_attribution_failure = (
                str(node["node_id"]) == "build_argument_graph"
                and error.get("type") == "NonRetryableFukamiruError"
                and error_message.startswith(
                    "Argument graph contract violation after one bounded retry: atomic proposition "
                )
                and all(
                    item.startswith("atomic proposition ")
                    and item.endswith(" must link its attributed theory citation")
                    for item in error_message.split(
                        "Argument graph contract violation after one bounded retry: ", 1
                    )[1].split("; ")
                )
            )
            v31_graph_scope_basis_failure = (
                workflow_version in {"31", "32"}
                and str(node["node_id"]) == "build_argument_graph"
                and error.get("type") == "NonRetryableFukamiruError"
                and re.fullmatch(
                    r"Argument graph batch \d+ contract violation: "
                    r"claim scope handoff: Claim scope has unknown evidence basis: cl?\d+",
                    error_message,
                )
                is not None
            )
            v31_graph_core_atom_failure = (
                workflow_version in {"31", "32"}
                and str(node["node_id"]) == "build_argument_graph"
                and error.get("type") == "NonRetryableFukamiruError"
                and error_message.startswith(
                    "Argument graph contract violation after one bounded retry: "
                )
                and all(
                    re.fullmatch(
                        r"core atomic proposition p\d+ must be represented as "
                        r"qualified_inference",
                        problem,
                    )
                    is not None
                    for problem in error_message.split(
                        "Argument graph contract violation after one bounded retry: ", 1
                    )[1].split("; ")
                )
            )
            v31_graph_batch_attribution_failure = (
                workflow_version in {"31", "32"}
                and str(node["node_id"]) == "build_argument_graph"
                and error.get("type") == "NonRetryableFukamiruError"
                and re.fullmatch(
                    r"Argument graph batch \d+ contract violation: "
                    r"atomic proposition p\d+ must link its attributed theory citation",
                    error_message,
                )
                is not None
            )
            v31_graph_repair_drift_failure = (
                workflow_version in {"31", "32"}
                and str(node["node_id"]) == "build_argument_graph"
                and error.get("type") == "NonRetryableFukamiruError"
                and error_message.startswith("Argument graph batch 1 contract violation: ")
                and all(
                    re.fullmatch(
                        r"evidence node [A-Za-z0-9_-]+ claims verified status "
                        r"without an accepted external evidence ID|"
                        r"core atomic proposition p\d+ must be represented as "
                        r"qualified_inference",
                        problem,
                    )
                    is not None
                    for problem in error_message.split(
                        "Argument graph batch 1 contract violation: ", 1
                    )[1].split("; ")
                )
            )
            v31_graph_rebuttal_link_failure = (
                workflow_version in {"31", "32"}
                and str(node["node_id"]) == "build_argument_graph"
                and error.get("type") == "NonRetryableFukamiruError"
                and re.fullmatch(
                    r"Argument graph (?:batch \d+ contract violation|"
                    r"contract violation after one bounded retry): "
                    r"support nodes lack an edge to their claim node: "
                    r"\['node:rebuttal:[^']+'(?:, 'node:rebuttal:[^']+')*\]",
                    error_message,
                )
                is not None
                and bool(unpacked.get("claims"))
            )
            v31_graph_provenance_failure = (
                workflow_version in {"31", "32"}
                and str(node["node_id"]) == "build_argument_graph"
                and error.get("type") == "NonRetryableFukamiruError"
                and error_message.startswith(
                    "Argument graph contract violation after one bounded retry: "
                )
                and bool(unpacked.get("claims"))
                and all(
                    re.fullmatch(
                        r"claim cl\d+ has unsupported verified evidence nodes: "
                        r"\['[^']+'(?:, '[^']+')*\]|"
                        r"atomic proposition p\d+ must link its attributed "
                        r"theory citation|"
                        r"atomic proposition p\d+ must remain visibly "
                        r"memo-attributed",
                        problem,
                    )
                    is not None
                    for problem in error_message.split(
                        "Argument graph contract violation after one bounded retry: ", 1
                    )[1].split("; ")
                )
            )
            v31_defense_reference_failure = (
                workflow_version in {"31", "32"}
                and str(node["node_id"]) == "select_defense_obligations"
                and error.get("type") == "NonRetryableFukamiruError"
                and error_message
                == (
                    "Defense obligation JSON handoff failed after one bounded retry: "
                    "Defense cites counterevidence for another claim"
                )
            )
            v31_section_role_dependency_failure = (
                workflow_version in {"31", "32"}
                and str(node["node_id"]) == "lock_section_contracts"
                and error.get("type") == "NonRetryableFukamiruError"
                and error_message
                == (
                    "Section contract violation after one bounded retry: "
                    "role contracts: Body section depends on a later or unknown section"
                )
            )
            v31_global_defense_assignment_failure = (
                workflow_version in {"31", "32"}
                and str(node["node_id"]) == "lock_section_contracts"
                and error.get("type") == "NonRetryableFukamiruError"
                and error_message
                == (
                    "Section contract violation after one bounded retry: "
                    "role contracts: Body defense is assigned outside its target claim"
                )
            )
            atomic_evidence_policy_failure = (
                str(node["node_id"]) == "validate_evidence_ready"
                and error.get("type") == "NonRetryableFukamiruError"
                and error_message.startswith(
                    "Authoring atomic evidence gate failed before argument planning:"
                )
            )
            structural_repair_mapping_failure = (
                str(node["node_id"]) == "repair_failed_sections"
                and error.get("type") == "NonRetryableFukamiruError"
                and error_message.startswith(
                    "Structural repair violated the locked draft contract:"
                )
                and "changed its semantic paragraph move mapping:" in error_message
            )
            structural_repair_evidence_failure = (
                str(node["node_id"]) == "repair_failed_sections"
                and error.get("type") == "NonRetryableFukamiruError"
                and error_message.startswith(
                    "Structural repair violated the locked draft contract: section "
                )
                and " paragraphs use non-contract evidence: " in error_message
                and bool(unpacked.get("section_contracts"))
                and bool(unpacked.get("argument_nodes"))
                and any(
                    isinstance(item, dict)
                    and item.get("code") == "provenance_loss"
                    and "自著" in str(item.get("message") or "")
                    for item in unpacked.get("audit_findings") or []
                )
            )
            v31_repair_handoff_failure = (
                workflow_version in {"31", "32"}
                and str(node["node_id"]) == "repair_failed_sections"
                and (
                    (
                        error.get("type") in {"NonRetryableFukamiruError", "FukamiruError"}
                        and error_message.startswith(
                            (
                                "V31 repair broke a JSON section contract: ",
                                "V31 targeted repair repeatedly broke its JSON section contract: ",
                            )
                        )
                    )
                    or (
                        error.get("type") == "FukamiruError"
                        and error_message
                        == ("Conclusion new evidence metadata disagrees with realized evidence")
                    )
                )
                and bool(unpacked.get("section_contracts"))
                and bool(unpacked.get("argument_nodes"))
                and (
                    dict(unpacked.get("authoring_recovery_lineage") or {}).get("source_node_id")
                    == "join_draft_audits"
                    or (
                        bool(unpacked.get("draft_sections"))
                        and bool(unpacked.get("audit_findings"))
                        and bool(unpacked.get("conclusion_synthesis"))
                        and (
                            bool(unpacked.get("authoring_restart_lineage"))
                            or any(
                                prior["node_id"] == "join_draft_audits"
                                and prior["status"] == "completed"
                                for prior in nodes
                            )
                        )
                    )
                )
            )
            v31_repair_transport_failure = (
                workflow_version in {"31", "32"}
                and str(node["node_id"]) == "repair_failed_sections"
                and error.get("type") == "FukamiruError"
                and error_message.startswith("Codex authoring stage draft_")
                and any(
                    marker in error_message.casefold()
                    for marker in (
                        "at capacity",
                        "rate limit",
                        "rate_limit",
                        "too many requests",
                        "usage limit",
                        "quota exceeded",
                        "overloaded",
                    )
                )
                and bool(unpacked.get("section_contracts"))
                and bool(unpacked.get("argument_nodes"))
                and bool(unpacked.get("draft_sections"))
                and bool(unpacked.get("audit_findings"))
            )
            v31_required_citation_contract_failure = (
                workflow_version in {"31", "32"}
                and str(node["node_id"]) == "repair_failed_sections"
                and error.get("type") == "FukamiruError"
                and error_message.startswith(
                    "Authoring citation contract rejected required scholarship evidence "
                )
                and bool(unpacked.get("section_contracts"))
                and bool(unpacked.get("argument_nodes"))
                and bool(unpacked.get("draft_sections"))
                and bool(unpacked.get("audit_findings"))
            )
            v31_audit_repair_signature = _v31_audit_repair_signature(unpacked.get("audit_findings"))
            v31_seen_audit_repair_signatures = {
                str(value)
                for value in unpacked.get("authoring_extended_structure_repair_signatures", [])
                if str(value)
            }
            v31_structure_repair_limit = (
                _V31_LOCAL_SECTION_REPAIR_LIMIT
                if _v31_audit_findings_are_section_scoped(unpacked.get("audit_findings"))
                else _V31_EXTENDED_STRUCTURE_REPAIR_LIMIT
            )
            v31_structure_repair_policy_upgrade = (
                str(unpacked.get("authoring_structure_repair_policy") or "")
                != _V31_STRUCTURE_REPAIR_POLICY
            )
            v31_audit_repair_exhausted_failure = (
                workflow_version in {"31", "32"}
                and str(node["node_id"]) == "join_draft_audits"
                and error.get("type") == "NonRetryableFukamiruError"
                and error_message.startswith(
                    "V31 draft has unresolved major structural findings after bounded repair: "
                )
                and bool(unpacked.get("section_contracts"))
                and bool(unpacked.get("argument_nodes"))
                and bool(unpacked.get("draft_sections"))
                and bool(unpacked.get("audit_findings"))
                and (
                    int(unpacked.get("extended_structure_repair_count", 0))
                    < v31_structure_repair_limit
                    or v31_structure_repair_policy_upgrade
                )
                and (
                    v31_audit_repair_signature is None
                    or v31_audit_repair_signature not in v31_seen_audit_repair_signatures
                )
            )
            v31_polish_role_failure = (
                workflow_version in {"31", "32"}
                and str(node["node_id"]) == "polish_prose"
                and error.get("type") == "FukamiruError"
                and error_message.startswith("V31 polish changed role content: ")
                and bool(unpacked.get("draft_sections"))
                and bool(unpacked.get("conclusion_synthesis"))
                and dict(unpacked.get("authoring_recovery_lineage") or {}).get("source_node_id")
                == "join_draft_audits"
            )
            v31_coherence_packet_projection_failure = (
                workflow_version in {"31", "32"}
                and str(node["node_id"]) == "validate_draft_coherence"
                and error.get("type") == "CoherenceResolutionRequired"
                and error_message
                == (
                    "v26.prompt_component_too_large: section plan or a single "
                    "block must be split upstream"
                )
                and isinstance(unpacked.get("proposed_html"), str)
                and isinstance(unpacked.get("certainty_calibration"), dict)
            )
            v31_proofreading_locked_text_failure = (
                workflow_version in {"31", "32"}
                and str(node["node_id"]) == "publish_proofreading_shadow"
                and error.get("type") == "FukamiruError"
                and error_message
                == "V31 proofreading changed locked research-question or final-answer prose"
                and bool(unpacked.get("research_problem"))
                and bool(unpacked.get("conclusion_synthesis"))
            )
            v31_final_reader_facing_failure = (
                workflow_version in {"31", "32"}
                and str(node["node_id"]) == "finalize_authoring"
                and error.get("type") == "NonRetryableFukamiruError"
                and error_message.startswith(
                    "V31 final manuscript contains reader-facing input scaffolding: "
                )
                and bool(unpacked.get("section_contracts"))
                and bool(unpacked.get("argument_nodes"))
                and bool(unpacked.get("draft_sections"))
            )
            candidate_reconciliation_failure = (
                str(node["node_id"]) == "reconcile_candidate_context"
                and error.get("type") == "FukamiruError"
                and error_message
                == "Authoring candidate no longer matches the locked section contracts"
            )
            v31_bibliographic_contextual_recovery = bool(
                workflow_version in {"31", "32"}
                and str(node["node_id"]) == "reconcile_candidate_context"
                and error.get("type") == "FukamiruError"
                and prior_replays == 0
                and isinstance(unpacked, dict)
                and _v31_bibliographic_contextual_use_is_repairable(error_message, unpacked)
            )
            submission_reconciliation_failure = (
                candidate_reconciliation_failure
                and bool(
                    dict(unpacked.get("submission_validation_gate") or {}).get("failed_validations")
                )
                and isinstance(unpacked.get("evidence_footnote_render_report"), dict)
            )
            idempotent_footnote_reference_structure_failure = (
                str(node["node_id"]) == "validate_footnoted_structure"
                and error.get("type") == "CoherenceResolutionRequired"
                and error_message.startswith(
                    "rendered_structure.invalid: "
                    '[{"code": "terminal.reference_section_count_changed"'
                )
                and dict(unpacked.get("evidence_footnote_render_report") or {}).get("policy")
                == "evidence-footnotes-v1"
                and int(
                    dict(unpacked.get("evidence_footnote_render_report") or {}).get(
                        "cited_reference_count"
                    )
                    or 0
                )
                > 0
                and int(
                    dict(unpacked.get("reference_inventory") or {}).get("reference_section_count")
                    or 0
                )
                == 0
            )
            reference_render_report = dict(unpacked.get("reference_render_report") or {})
            reference_source_count = int(reference_render_report.get("source_reference_count") or 0)
            v31_uncited_reference_section_removal_failure = (
                workflow_version in {"31", "32"}
                and str(node["node_id"]) == "validate_footnoted_structure"
                and error.get("type") == "CoherenceResolutionRequired"
                and error_message.startswith(
                    "rendered_structure.invalid: "
                    '[{"code": "terminal.reference_section_count_changed"'
                )
                and '"expected": 1, "actual": 0' in error_message
                and dict(unpacked.get("evidence_footnote_render_report") or {}).get("policy")
                == "evidence-footnotes-v1"
                and int(
                    dict(unpacked.get("evidence_footnote_render_report") or {}).get(
                        "cited_reference_count"
                    )
                    or 0
                )
                == 0
                and reference_render_report.get("rule_set_id")
                and int(reference_render_report.get("entry_count") or 0) == 0
                and reference_render_report.get("machine_section_rendered") is False
                and reference_source_count > 0
                and int(reference_render_report.get("uncited_reference_review_count") or 0)
                == reference_source_count
            )
            transient_submission_database_lock = (
                str(node["node_id"]) == "render_manuscript_references"
                and error.get("type") == "OperationalError"
                and error_message == "database is locked"
                and isinstance(unpacked.get("evidence_footnote_render_report"), dict)
                and unpacked.get("completion_status") == "conditional_success"
                and isinstance(unpacked.get("conditional_success"), dict)
            )
            surface_gate = dict(unpacked.get("manuscript_surface_quality_gate") or {})
            surface_issue_codes = {
                str(issue.get("code") or "")
                for block in surface_gate.get("remaining_issue_blocks") or []
                if isinstance(block, dict)
                for issue in block.get("issues") or []
                if isinstance(issue, dict)
            }
            compound_coordination_surface_recheck = (
                str(node["node_id"]) == "freeze_final_candidate"
                and error.get("type") == "CoherenceResolutionRequired"
                and error_message == "v26.surface_candidate_requires_repair"
                and surface_issue_codes == {"surface.coordinated_lexical_repetition"}
                and not dict(unpacked.get("submission_validation_gate") or {}).get(
                    "failed_validations"
                )
            )
            v31_research_problem_ratification_failure = (
                workflow_version in {"31", "32"}
                and str(node["node_id"]) == "ratify_research_problem"
                and error.get("type") == "NonRetryableFukamiruError"
                and error_message.startswith(
                    "Research problem JSON handoff failed after one bounded retry: "
                    "Research problem changed the provisional answer; replan required"
                )
                and bool(dict(unpacked.get("research_question") or {}).get("text"))
                and bool(unpacked.get("propositions"))
                and bool(unpacked.get("claims"))
                and isinstance(unpacked.get("evidence_realization_decisions"), list)
                and isinstance(unpacked.get("authoring_atomic_evidence_contracts"), list)
            )
            discourse_branch_recombination_prefix = (
                "Discourse tree contract violation after one bounded retry: "
            )
            discourse_branch_recombination_pattern = re.compile(
                r"V31 body discourse section [A-Za-z0-9_-]+ recombined "
                r"distinct top-level topic branches: \[[^\]]*\]\. Keep one topic "
                r"branch per body phase; cross-branch synthesis belongs in the "
                r"conclusion or an explicitly separate synthesis phase\."
            )
            discourse_branch_recombination_details = error_message[
                len(discourse_branch_recombination_prefix) :
            ]
            recognized_discourse_branch_violations = list(
                discourse_branch_recombination_pattern.finditer(
                    discourse_branch_recombination_details
                )
            )
            v31_discourse_synthesis_phase_failure = (
                workflow_version in {"31", "32"}
                and str(node["node_id"]) == "build_discourse_tree"
                and error.get("type") == "NonRetryableFukamiruError"
                and error_message.startswith(discourse_branch_recombination_prefix)
                and bool(recognized_discourse_branch_violations)
                and not discourse_branch_recombination_pattern.sub(
                    "", discourse_branch_recombination_details
                ).strip(" ;")
                and bool(unpacked.get("research_problem"))
                and bool(unpacked.get("claims"))
                and bool(unpacked.get("argument_nodes"))
                and bool(unpacked.get("claim_scopes"))
                and bool(unpacked.get("defense_obligations"))
            )
            v31_duplicate_section_contract_identity_failure = (
                workflow_version in {"31", "32"}
                and str(node["node_id"]) == "draft_rewrite"
                and error.get("type") == "FukamiruError"
                and error_message
                == "V31 drafting requires Introduction, Body and Conclusion contracts"
                and _v31_has_mergeable_duplicate_section_contracts(
                    unpacked.get("section_contracts")
                )
            )
            v31_section_contract_identity_recovery = (
                prior_replays == 2
                and v31_duplicate_section_contract_identity_failure
                and prior_replay_policies
                == [
                    "authoring-v31-citation-source-claim-bridge-recovery-v1",
                    "authoring-v31-citation-source-claim-bridge-input-rebuild-v1",
                ]
                and len(prior_replay_payloads) == 2
                and prior_replay_payloads[0].get("v31_citation_source_claim_mapping_recovery")
                is True
                and prior_replay_payloads[1].get(
                    "v31_citation_source_claim_mapping_followup_recovery"
                )
                is True
                and all(
                    _v31_citation_source_claim_mapping_is_repairable(
                        str(dict(payload.get("previous_node_error") or {}).get("message") or ""),
                        unpacked,
                    )
                    for payload in prior_replay_payloads
                )
            )
            v31_missing_draft_source_review = (
                workflow_version in {"31", "32"}
                and str(node["node_id"]) == "draft_rewrite"
                and error.get("type") == "FukamiruError"
                and prior_replays == 0
                and isinstance(unpacked, dict)
                and _v31_missing_draft_source_is_reviewable(error_message, unpacked)
            )
            v32_internal_evidence_review_recovery = False
            v32_bounded_concept_repair_recovery = False
            v32_basis_identity_review_recovery = False
            v32_field_anchor_review_recovery = False
            if (workflow_version == "32" and str(node["node_id"]) == "build_argument_graph"
                    and error.get("type") == "NonRetryableFukamiruError"
                    and error_message.startswith("Concept review excerpt is not in its field:")
                    and prior_replay_policies in ([], [
                        "authoring-v32-bounded-concept-repair-v2",
                        "authoring-v32-basis-identity-review-v1",
                        "authoring-v32-verification-routing-recovery-v1",
                    ])):
                anchor_native = self.connection.execute(
                    "SELECT operation_name,result_json FROM llm_result_checkpoints WHERE run_id=? "
                    "AND node_id='build_argument_graph' AND stage='argument_graph_v31' "
                    "AND status='accepted' ORDER BY created_at DESC,rowid DESC LIMIT 1",
                    (run_id,),
                ).fetchone()
                anchor_report = self.connection.execute(
                    "SELECT path FROM run_artifacts WHERE run_id=? AND node_run_id=? "
                    "AND artifact_type='authoring_concept_fit_report' "
                    "ORDER BY created_at DESC,rowid DESC LIMIT 1", (run_id, node["id"]),
                ).fetchone()
                if anchor_native and anchor_report:
                    from fukamiru.authoring import concept_fit

                    try:
                        native = json.loads(anchor_native["result_json"])
                        report_path = Path(anchor_report["path"])
                        archived = json.loads(report_path.read_text("utf-8"))
                        candidate_sha = concept_fit.digest(native)
                        expected_name = concept_fit.digest([
                            str(anchor_native["operation_name"]) + ":concept-review:invalid:1",
                            candidate_sha,
                        ])[:24]
                        if (report_path.stem.split("-")[0] == expected_name
                                and archived.get("status") == "invalid_review"
                                and archived.get("stage") == "argument_graph_v31"
                                and archived.get("error") == error_message
                                and archived.get("candidate_sha256") == candidate_sha):
                            v32_field_anchor_review_recovery = (
                                _v32_field_anchor_review_is_replayable(
                                    error_message, native, archived["initial_review"], unpacked,
                                )
                            )
                    except (OSError, ValueError, TypeError, KeyError):
                        pass
            if (workflow_version == "32" and str(node["node_id"]) == "build_argument_graph"
                    and error.get("type") == "NonRetryableFukamiruError"
                    and error_message == "Concept review invented a basis ID"
                    and prior_replay_policies in (
                        [], ["authoring-v32-bounded-concept-repair-v2"],
                    )):
                basis_report = self.connection.execute(
                    "SELECT path FROM run_artifacts WHERE run_id=? AND node_run_id=? "
                    "AND artifact_type='authoring_concept_fit_report' "
                    "ORDER BY created_at DESC,rowid DESC LIMIT 1", (run_id, node["id"]),
                ).fetchone()
                basis_native = self.connection.execute(
                    "SELECT result_json FROM llm_result_checkpoints WHERE run_id=? "
                    "AND node_id='build_argument_graph' AND stage='argument_graph_v31' "
                    "AND status='accepted' ORDER BY created_at DESC,rowid DESC LIMIT 1",
                    (run_id,),
                ).fetchone()
                if basis_report and basis_native:
                    from fukamiru.authoring import concept_fit

                    try:
                        archived = json.loads(Path(basis_report["path"]).read_text("utf-8"))
                        native = json.loads(str(basis_native["result_json"]))
                        if (archived.get("status") == "invalid_review"
                                and archived.get("stage") == "argument_graph_v31"
                                and archived.get("error") == error_message
                                and archived.get("candidate_sha256") == concept_fit.digest(native)):
                            v32_basis_identity_review_recovery = (
                                _v32_basis_identity_review_is_replayable(
                                    error_message, native, archived["initial_review"], unpacked,
                                )
                            )
                    except (OSError, ValueError, TypeError, KeyError):
                        pass
            if (workflow_version == "32" and str(node["node_id"]) == "build_argument_graph"
                    and error.get("type") == "NonRetryableFukamiruError"
                    and prior_replays == 0
                    and error_message.startswith("Concept repair remains unresolved: ")):
                saved_graph = self.connection.execute(
                    "SELECT result_json FROM llm_result_checkpoints WHERE run_id=? "
                    "AND node_id='build_argument_graph' AND stage='argument_graph_v31' "
                    "AND status='accepted' ORDER BY created_at DESC,rowid DESC LIMIT 1",
                    (run_id,),
                ).fetchone()
                if saved_graph is not None:
                    v32_internal_evidence_review_recovery = (
                        _v32_internal_evidence_review_is_repairable(
                            error_message, json.loads(str(saved_graph["result_json"])),
                        )
                    )
                latest_review = self.connection.execute(
                    "SELECT operation_name,result_json FROM llm_result_checkpoints "
                    "WHERE run_id=? AND node_id='build_argument_graph' "
                    "AND stage='review_concept_fit_v32' AND status='accepted' "
                    "ORDER BY created_at DESC,rowid DESC LIMIT 1", (run_id,),
                ).fetchone()
                if (latest_review is not None and str(latest_review["operation_name"]).endswith(
                    ":concept-recheck"
                )):
                    base = str(latest_review["operation_name"]).removesuffix(":concept-recheck")
                    native = self.connection.execute(
                        "SELECT result_json FROM llm_result_checkpoints WHERE run_id=? "
                        "AND stage='argument_graph_v31' AND operation_name=? "
                        "AND status='accepted'", (run_id, base),
                    ).fetchone()
                    repair = self.connection.execute(
                        "SELECT result_json FROM llm_result_checkpoints WHERE run_id=? "
                        "AND stage='repair_concept_fit_v32' AND operation_name=? "
                        "AND status='accepted'", (run_id, base + ":concept-repair"),
                    ).fetchone()
                    second_repair = self.connection.execute(
                        "SELECT 1 FROM llm_result_checkpoints WHERE run_id=? "
                        "AND stage='repair_concept_fit_v32' AND operation_name=? "
                        "AND status='accepted'", (run_id, base + ":concept-repair:2"),
                    ).fetchone()
                    if native is not None and repair is not None and second_repair is None:
                        v32_bounded_concept_repair_recovery = (
                            _v32_bounded_concept_repair_is_replayable(
                                error_message, json.loads(str(native["result_json"])),
                                json.loads(str(repair["result_json"])),
                                json.loads(str(latest_review["result_json"])),
                            )
                        )
            prose_basis_diagnostic_recovery = False
            if (
                workflow_version == "32"
                and str(node["node_id"]) == "redraft_japanese_paragraphs"
                and error.get("type") == "NonRetryableFukamiruError"
                and error_message == "Concept review invented a basis ID"
                and prior_replays == 0 and isinstance(unpacked, dict)
            ):
                from fukamiru.authoring import concept_fit as fit

                native_row = self.connection.execute(
                    "SELECT operation_name,result_json FROM llm_result_checkpoints "
                    "WHERE run_id=? AND node_id='redraft_japanese_paragraphs' "
                    "AND stage='japanese_paragraph_redraft_v31' AND status='accepted' "
                    "ORDER BY updated_at DESC,rowid DESC LIMIT 1", (run_id,),
                ).fetchone()
                if native_row is not None:
                    operation = str(native_row["operation_name"])
                    match = re.fullmatch(r"japanese_paragraph_redraft_(\d+)_(\d+)", operation)
                    work_sections = dict(unpacked.get("japanese_redrafting_work") or {}).get(
                        "sections", [],
                    )
                    if match and int(match[1]) < len(work_sections):
                        section = work_sections[int(match[1])]
                        candidate = json.loads(str(native_row["result_json"]))
                        payload = {
                            **section["base"],
                            "locked_section_contracts": unpacked.get("section_contracts") or [],
                        }
                        key = fit.digest([operation + ":concept-review:invalid:1",
                                          fit.digest(candidate)])[:24]
                        for artifact in self.connection.execute(
                            "SELECT path FROM run_artifacts WHERE run_id=? AND node_run_id=? "
                            "AND artifact_type='authoring_concept_fit_report' "
                            "ORDER BY created_at DESC,rowid DESC", (run_id, node["id"]),
                        ):
                            path = Path(str(artifact["path"]))
                            if not path.name.startswith(key):
                                continue
                            try:
                                report = json.loads(path.read_text(encoding="utf-8"))
                                prose_basis_diagnostic_recovery = bool(
                                    report.get("candidate_sha256") == fit.digest(candidate)
                                    and report.get("stage") == "japanese_paragraph_redraft_v31"
                                    and report.get("status") == "invalid_review"
                                    and report.get("error") == error_message
                                    and _v32_prose_basis_typo_is_replayable(
                                        candidate, report["initial_review"], unpacked, payload,
                                    )
                                )
                            except (OSError, ValueError, TypeError, KeyError):
                                pass
                            break
            audit_claim_target_recovery = False
            if (
                workflow_version in {"31", "32"}
                and str(node["node_id"]) == "classify_audit_findings"
                and error.get("type") == "ValidationError"
                and "finding references a section outside the audit target" in error_message
                and prior_replays == 0
                and isinstance(unpacked, dict)
            ):
                contracts = unpacked.get("section_contracts") or []
                known = {str(item.get("section_id") or "") for item in contracts}
                raw_findings = unpacked.get("audit_findings") or []
                try:
                    localized = [
                        target for finding in raw_findings
                        for target in localize_audit_finding(
                            finding, section_contracts=contracts,
                            draft_sections=unpacked.get("draft_sections") or [],
                        )
                    ]
                    audit_claim_target_recovery = bool(
                        raw_findings and any(
                            item.get("object_type") in {"claim", "sentence"}
                            and str(item.get("object_id") or "") not in known
                            for item in raw_findings
                        ) and all(
                            str(item.get("section_id") or item.get("object_id") or "")
                            .rsplit(":", 1)[-1] in known
                            for item in localized
                        )
                    )
                except (ValueError, TypeError, KeyError):
                    audit_claim_target_recovery = False
            pending_nodes = [
                item
                for item in nodes
                if item["status"] not in {"completed", "failed"}
                and not (
                    (cancelled_rag_context_recovery
                     or v32_verification_routing_recovery
                     or v31_cancelled_self_citation_recovery
                     or v31_cancelled_semantic_audit_reroute)
                    and item["id"] == node["id"]
                )
            ]
            pending_parallel_submission_siblings_are_safe = bool(
                idempotent_footnote_reference_structure_failure
                and pending_nodes
                and all(
                    item["status"] == "queued"
                    and str(item["node_id"]) == "validate_footnoted_coherence"
                    for item in pending_nodes
                )
            )
            if (
                (pending_nodes and not pending_parallel_submission_siblings_are_safe)
                or not (
                    (
                        error.get("type") == "FukamiruError"
                        and (codex_failure or semantic_coverage_failure)
                    )
                    or draft_metadata_failure
                    or v31_draft_handoff_failure
                    or footnote_metadata_failure
                    or v31_aggregated_self_citation_failure
                    or v31_provisional_bibliography_recovery
                    or proposal_revalidation_failure
                    or checkpoint_plan_failure
                    or memo_annotation_preservation_failure
                    or claim_tree_provenance_failure
                    or v31_claim_tree_branch_failure
                    or argument_graph_timeout_failure
                    or argument_graph_attribution_failure
                    or v31_graph_scope_basis_failure
                    or v31_graph_core_atom_failure
                    or v31_graph_batch_attribution_failure
                    or v31_graph_repair_drift_failure
                    or v31_graph_rebuttal_link_failure
                    or v31_graph_provenance_failure
                    or v31_defense_reference_failure
                    or v31_section_role_dependency_failure
                    or v31_global_defense_assignment_failure
                    or atomic_evidence_policy_failure
                    or structural_repair_mapping_failure
                    or structural_repair_evidence_failure
                    or v31_repair_handoff_failure
                    or v31_repair_transport_failure
                    or v31_required_citation_contract_failure
                    or v31_citation_source_claim_mapping_failure
                    or v31_citation_source_claim_mapping_post_fix_replay
                    or (
                        error.get("type") == "FukamiruError"
                        and workflow_version in {"31", "32"}
                        and str(node["node_id"]) == "draft_rewrite"
                        and isinstance(unpacked, dict)
                        and _v31_fixed_source_lineage_is_repairable(error_message, unpacked)
                    )
                    or v31_audit_repair_exhausted_failure
                    or v31_polish_role_failure
                    or v31_coherence_packet_projection_failure
                    or v31_proofreading_locked_text_failure
                    or v31_final_reader_facing_failure
                    or candidate_reconciliation_failure
                    or v31_bibliographic_contextual_recovery
                    or v31_cancelled_self_citation_recovery
                    or v31_cancelled_semantic_audit_reroute
                    or v31_audit_join_classification_recovery
                    or v31_audit_join_dispatch_recovery
                    or v31_repair_source_retirement_recovery
                    or submission_reconciliation_failure
                    or idempotent_footnote_reference_structure_failure
                    or v31_uncited_reference_section_removal_failure
                    or transient_submission_database_lock
                    or compound_coordination_surface_recheck
                    or v31_research_problem_ratification_failure
                    or v31_discourse_synthesis_phase_failure
                    or v31_section_contract_identity_recovery
                    or v31_missing_draft_source_review
                    or v32_internal_evidence_review_recovery
                    or v32_bounded_concept_repair_recovery
                    or v32_basis_identity_review_recovery
                    or v32_verification_routing_recovery
                    or v32_field_anchor_review_recovery
                    or audit_claim_target_recovery
                    or prose_basis_diagnostic_recovery
                )
                or not isinstance(unpacked, dict)
                or (
                    not unpacked.get("authoring_restart_lineage")
                    and not (
                        (
                            draft_metadata_failure
                            and unpacked.get("plan_id")
                            and unpacked.get("section_contracts")
                            and unpacked.get("argument_nodes")
                        )
                        or v31_draft_handoff_failure
                        or v31_required_citation_contract_failure
                        or v31_citation_source_claim_mapping_failure
                        or v31_citation_source_claim_mapping_post_fix_replay
                    )
                    and not memo_annotation_preservation_failure
                    and not claim_tree_provenance_failure
                    and not v31_claim_tree_branch_failure
                    and not argument_graph_timeout_failure
                    and not argument_graph_attribution_failure
                    and not v31_graph_scope_basis_failure
                    and not v31_graph_core_atom_failure
                    and not v31_graph_batch_attribution_failure
                    and not v31_graph_repair_drift_failure
                    and not v31_graph_rebuttal_link_failure
                    and not v31_graph_provenance_failure
                    and not v31_defense_reference_failure
                    and not v31_section_role_dependency_failure
                    and not v31_global_defense_assignment_failure
                    and not v31_aggregated_self_citation_failure
                    and not v31_provisional_bibliography_recovery
                    and not atomic_evidence_policy_failure
                    and not structural_repair_mapping_failure
                    and not structural_repair_evidence_failure
                    and not v31_repair_handoff_failure
                    and not v31_repair_transport_failure
                    and not v31_audit_repair_exhausted_failure
                    and not v31_polish_role_failure
                    and not v31_coherence_packet_projection_failure
                    and not v31_proofreading_locked_text_failure
                    and not v31_final_reader_facing_failure
                    and not candidate_reconciliation_failure
                    and not v31_bibliographic_contextual_recovery
                    and not v31_cancelled_self_citation_recovery
                    and not v31_cancelled_semantic_audit_reroute
                    and not v31_audit_join_classification_recovery
                    and not v31_audit_join_dispatch_recovery
                    and not v31_repair_source_retirement_recovery
                    and not submission_reconciliation_failure
                    and not idempotent_footnote_reference_structure_failure
                    and not v31_uncited_reference_section_removal_failure
                    and not transient_submission_database_lock
                    and not compound_coordination_surface_recheck
                    and not v31_research_problem_ratification_failure
                    and not v31_discourse_synthesis_phase_failure
                    and not v31_section_contract_identity_recovery
                    and not v31_missing_draft_source_review
                    and not v32_internal_evidence_review_recovery
                    and not v32_bounded_concept_repair_recovery
                    and not v32_basis_identity_review_recovery
                    and not v32_verification_routing_recovery
                    and not v32_field_anchor_review_recovery
                    and not audit_claim_target_recovery
                    and not prose_basis_diagnostic_recovery
                    and not _v31_fixed_source_lineage_is_repairable(
                        error_message, unpacked
                    )
                )
            ):
                raise FukamiruError("Failed authoring node has no replayable input")
            schema_recovery = prior_replays == 1 and "invalid_json_schema" in error_message
            coverage_recovery = prior_replays == 2 and semantic_coverage_failure
            index_derivation_recovery = prior_replays == 3 and semantic_coverage_failure
            proposal_policy_recovery = prior_replays == 1 and proposal_revalidation_failure
            argument_graph_attribution_recovery = (
                prior_replays == 1 and argument_graph_attribution_failure
            )
            v31_graph_core_atom_recovery = prior_replays == 1 and v31_graph_core_atom_failure
            v31_graph_batch_attribution_recovery = (
                prior_replays == 2 and v31_graph_batch_attribution_failure
            )
            v31_graph_repair_drift_recovery = prior_replays == 3 and v31_graph_repair_drift_failure
            v31_graph_provenance_recovery = prior_replays == 1 and v31_graph_provenance_failure
            v31_global_defense_schema_recovery = (
                prior_replays == 1 and v31_global_defense_assignment_failure
            )
            draft_contract_evidence_recovery = (
                prior_replays == 1
                and draft_metadata_failure
                and all(
                    problem.startswith("section ")
                    and (
                        " paragraphs use non-contract evidence:" in problem
                        or " treated non-contract evidence " in problem
                    )
                    for problem in error_message.split(
                        "Draft section contract violation after one bounded retry: ", 1
                    )[1].split("; ")
                )
            )
            v31_repair_metadata_recovery = prior_replays == 1 and v31_repair_handoff_failure
            v31_required_citation_contract_recovery = (
                prior_replays == 0 and v31_required_citation_contract_failure
            )
            v31_citation_source_claim_mapping_recovery = (
                prior_replays == 0 and v31_citation_source_claim_mapping_failure
            )
            v31_footnote_anchor_recovery = (
                prior_replays == 1
                and v31_draft_handoff_failure
                and error_message.endswith("has no unique sentence-level footnote anchor")
            )
            citation_anchor_surface_prefix = "V31 role draft JSON handoff violation: "
            citation_anchor_surface_problems = (
                error_message.split(citation_anchor_surface_prefix, 1)[1].split("; ")
                if error_message.startswith(citation_anchor_surface_prefix)
                else []
            )
            v31_citation_anchor_surface_recovery = bool(
                workflow_version in {"31", "32"}
                and str(node["node_id"]) == "draft_rewrite"
                and error.get("type") == "NonRetryableFukamiruError"
                and anchor_error_kind(error_message) is None
                and recovery_available(
                    prior_replay_payloads, "authoring-v31-citation-anchor-surface-recovery-v1",
                    workflow_version=workflow_version,
                )
                and bool(citation_anchor_surface_problems)
                and all(
                    re.fullmatch(
                        r"section [^;]+ scholarship evidence [^;]+ (?:"
                        r"has no unique sentence-level footnote anchor|"
                        r"lacks an exact visible attribution sentence in its evidence paragraph|"
                        r"does not identify its author or work in the unique "
                        r"sentence-level citation anchor)",
                        problem,
                    )
                    for problem in citation_anchor_surface_problems
                )
            )
            support_sentence_hint_prefix = "V31 role draft JSON handoff violation: "
            support_sentence_hint_problems = (
                error_message.split(support_sentence_hint_prefix, 1)[1].split("; ")
                if error_message.startswith(support_sentence_hint_prefix)
                else []
            )
            v31_support_sentence_hint_recovery = bool(
                workflow_version in {"31", "32"}
                and str(node["node_id"]) == "draft_rewrite"
                and v31_draft_handoff_failure
                and recovery_available(
                    prior_replay_payloads, "authoring-v31-support-sentence-hint-recovery-v1",
                    workflow_version=workflow_version,
                )
                and bool(support_sentence_hint_problems)
                and all(
                    re.fullmatch(
                        r"section [^;]+ evidence [^;]+ lacks an exact support_sentence "
                        r"in a paragraph that lists the evidence ID",
                        problem,
                    )
                    for problem in support_sentence_hint_problems
                )
            )
            anchor_kind = verified_scholarship_anchor(unpacked, error_message)
            if anchor_kind is not None:
                v31_citation_anchor_surface_recovery = False
            def correction_available(policy: str) -> bool:
                return recovery_available(
                    prior_replay_payloads, policy, workflow_version=workflow_version
                )

            v31_conclusion_scholarship_conclusion_anchor_recovery = bool(
                workflow_version in {"31", "32"}
                and str(node["node_id"]) == "draft_rewrite"
                and v31_draft_handoff_failure
                and anchor_kind == "footnote"
                and correction_available(
                    "authoring-v31-conclusion_scholarship-conclusion-anchor-recovery-v1"
                )
            )
            v31_semantic_attribution_predicate_recovery = bool(
                workflow_version in {"31", "32"}
                and str(node["node_id"]) == "draft_rewrite"
                and v31_draft_handoff_failure
                and anchor_kind == "attribution"
                and correction_available("authoring-v31-attribution-predicate-semantic-recovery-v1")
            )
            v31_fixed_source_lineage_recovery = bool(
                workflow_version in {"31", "32"}
                and str(node["node_id"]) == "draft_rewrite"
                and error.get("type") == "FukamiruError"
                and correction_available("authoring-v31-fixed-source-lineage-recovery-v1")
                and _v31_fixed_source_lineage_is_repairable(error_message, unpacked)
            )
            v31_cancelled_rag_context_recovery = (
                prior_replays == 2 and cancelled_rag_context_recovery
            )
            prior_citation_bridge_error = dict(
                (prior_replay_payloads[-1].get("previous_node_error") or {})
                if prior_replay_payloads
                else {}
            )
            v31_citation_source_claim_mapping_followup_recovery = (
                prior_replays == 1
                and v31_citation_source_claim_mapping_failure
                and prior_replay_policies
                == ["authoring-v31-citation-source-claim-bridge-recovery-v1"]
                and prior_replay_payloads[0].get("v31_citation_source_claim_mapping_recovery")
                is True
                and prior_citation_bridge_error.get("type") == error.get("type")
                and str(prior_citation_bridge_error.get("message") or "") == error_message
            )
            v31_graph_scope_basis_followup_recovery = (
                v31_graph_scope_basis_failure
                and prior_replays == 2
                and prior_replay_policies
                == [
                    "authoring-v31-rebuttal-link-recovery-v1",
                    "authoring-v31-evidence-attribution-recovery-v1",
                ]
                and prior_replay_payloads[0].get("v31_graph_rebuttal_link_recovery") is True
                and prior_replay_payloads[1].get("v31_graph_provenance_recovery") is True
            )
            v31_structure_aware_draft_recovery = (
                prior_replays == 3
                and v31_draft_handoff_failure
                and error_message.endswith("has no unique sentence-level footnote anchor")
                and prior_replay_policies
                == [
                    "authoring-terminal-codex-replay-v1",
                    "authoring-v31-citation-anchor-recovery-v1",
                    "authoring-v31-rag-context-recovery-v1",
                ]
            )
            v31_research_problem_ratification_recovery = (
                prior_replays == 0 and v31_research_problem_ratification_failure
            )
            v31_discourse_synthesis_recovery = (
                prior_replays == 0 and v31_discourse_synthesis_phase_failure
            )
            discourse_checkpoint_row = None
            if prior_replays == 1 and v31_discourse_synthesis_phase_failure:
                discourse_checkpoint_row = self.connection.execute(
                    "SELECT result_json FROM llm_result_checkpoints "
                    "WHERE run_id=? AND node_id='build_discourse_tree' "
                    "AND stage='build_discourse_tree' AND status='accepted' "
                    "ORDER BY updated_at DESC, rowid DESC LIMIT 1",
                    (run_id,),
                ).fetchone()
            v31_discourse_synthesis_checkpoint_recovery = bool(
                discourse_checkpoint_row
                and _v31_checkpoint_has_explicit_cross_branch_synthesis(
                    str(discourse_checkpoint_row["result_json"] or ""),
                    unpacked.get("claims"),
                )
            )
            if prior_replays >= 1 and not (
                schema_recovery
                or v32_basis_identity_review_recovery
                or v32_verification_routing_recovery
                or v32_field_anchor_review_recovery
                or coverage_recovery
                or index_derivation_recovery
                or proposal_policy_recovery
                or argument_graph_attribution_recovery
                or v31_graph_core_atom_recovery
                or v31_graph_batch_attribution_recovery
                or v31_graph_repair_drift_recovery
                or v31_graph_provenance_recovery
                or v31_graph_scope_basis_followup_recovery
                or v31_global_defense_schema_recovery
                or draft_contract_evidence_recovery
                or v31_repair_metadata_recovery
                or v31_footnote_anchor_recovery
                or v31_citation_anchor_surface_recovery
                or v31_support_sentence_hint_recovery
                or v31_conclusion_scholarship_conclusion_anchor_recovery
                or v31_semantic_attribution_predicate_recovery
                or v31_fixed_source_lineage_recovery
                or v31_bibliographic_contextual_recovery
                or v31_cancelled_self_citation_recovery
                or v31_cancelled_semantic_audit_reroute
                or v31_audit_join_classification_recovery
                or v31_audit_join_dispatch_recovery
                or v31_repair_source_retirement_recovery
                or v31_cancelled_rag_context_recovery
                or v31_structure_aware_draft_recovery
                or v31_research_problem_ratification_recovery
                or v31_discourse_synthesis_recovery
                or v31_citation_source_claim_mapping_recovery
                or v31_citation_source_claim_mapping_followup_recovery
                or v31_citation_source_claim_mapping_post_fix_replay
                or v31_section_contract_identity_recovery
                or v31_discourse_synthesis_checkpoint_recovery
                or v31_repair_transport_failure
            ):
                raise FukamiruError("Failed authoring node already used its manual replay")
            if prose_basis_diagnostic_recovery:
                replay_policy = "authoring-v32-prose-basis-diagnostics-v1"
            elif audit_claim_target_recovery:
                replay_policy = "authoring-audit-object-section-recovery-v1"
            elif v32_field_anchor_review_recovery:
                replay_policy = "authoring-v32-field-anchor-review-v1"
            elif v32_verification_routing_recovery:
                replay_policy = "authoring-v32-verification-routing-recovery-v1"
            elif v32_basis_identity_review_recovery:
                replay_policy = "authoring-v32-basis-identity-review-v1"
            elif v32_bounded_concept_repair_recovery:
                replay_policy = "authoring-v32-bounded-concept-repair-v2"
            elif v32_internal_evidence_review_recovery:
                replay_policy = "authoring-v32-internal-evidence-role-review-v1"
            elif v31_audit_join_dispatch_recovery:
                replay_policy = "authoring-v31-audit-join-dispatch-recovery-v1"
            elif v31_audit_join_classification_recovery:
                replay_policy = "authoring-v31-audit-join-classification-recovery-v1"
            elif v31_cancelled_semantic_audit_reroute:
                replay_policy = "authoring-v31-semantic-audit-review-reroute-v1"
            elif v31_repair_source_retirement_recovery:
                replay_policy = "authoring-v31-retired-source-human-review-v1"
            elif v31_cancelled_self_citation_recovery:
                replay_policy = "authoring-v31-provisional-self-citation-repair-v1"
            elif v31_provisional_bibliography_recovery:
                replay_policy = "authoring-v31-provisional-bibliography-review-v1"
            elif v31_cancelled_rag_context_recovery:
                replay_policy = "authoring-v31-rag-context-recovery-v1"
            elif v31_repair_transport_failure:
                replay_policy = "authoring-v31-repair-transport-recovery-v1"
            elif v31_structure_aware_draft_recovery:
                replay_policy = "authoring-v31-structure-aware-draft-recovery-v1"
            elif v31_required_citation_contract_recovery:
                replay_policy = "authoring-v31-required-citation-placement-recovery-v1"
            elif v31_citation_source_claim_mapping_recovery:
                replay_policy = "authoring-v31-citation-source-claim-bridge-recovery-v1"
            elif v31_citation_source_claim_mapping_post_fix_replay:
                replay_policy = "authoring-v31-citation-source-claim-mapping-post-fix-replay-v1"
            elif v31_citation_source_claim_mapping_followup_recovery:
                replay_policy = "authoring-v31-citation-source-claim-bridge-input-rebuild-v1"
            elif v31_section_contract_identity_recovery:
                replay_policy = "authoring-v31-section-contract-identity-merge-v1"
            elif v31_missing_draft_source_review:
                replay_policy = "authoring-v31-unlocked-draft-source-human-review-v1"
            elif v31_support_sentence_hint_recovery:
                replay_policy = "authoring-v31-support-sentence-hint-recovery-v1"
            elif v31_conclusion_scholarship_conclusion_anchor_recovery:
                replay_policy = "authoring-v31-conclusion_scholarship-conclusion-anchor-recovery-v1"
            elif v31_semantic_attribution_predicate_recovery:
                replay_policy = "authoring-v31-attribution-predicate-semantic-recovery-v1"
            elif v31_fixed_source_lineage_recovery:
                replay_policy = "authoring-v31-fixed-source-lineage-recovery-v1"
            elif v31_bibliographic_contextual_recovery:
                replay_policy = "authoring-v31-bibliographic-contextual-use-recovery-v1"
            elif v31_citation_anchor_surface_recovery:
                replay_policy = "authoring-v31-citation-anchor-surface-recovery-v1"
            elif v31_footnote_anchor_recovery:
                replay_policy = "authoring-v31-citation-anchor-recovery-v1"
            elif v31_discourse_synthesis_checkpoint_recovery:
                replay_policy = "authoring-v31-discourse-synthesis-contract-alignment-v1"
            elif v31_discourse_synthesis_recovery:
                replay_policy = "authoring-v31-discourse-synthesis-validation-v1"
            elif v31_research_problem_ratification_recovery:
                replay_policy = "authoring-v31-evidence-informed-ratification-v1"
            elif v31_graph_rebuttal_link_failure:
                replay_policy = "authoring-v31-rebuttal-link-recovery-v1"
            elif v31_graph_provenance_failure:
                replay_policy = "authoring-v31-evidence-attribution-recovery-v1"
            elif v31_graph_scope_basis_followup_recovery:
                replay_policy = "authoring-v31-graph-scope-basis-recovery-v1"
            elif v31_audit_repair_exhausted_failure:
                replay_policy = "authoring-v31-extended-structure-repair-v1"
            elif v31_claim_tree_branch_failure:
                replay_policy = "authoring-v31-claim-tree-replay-v1"
            else:
                replay_policy = "authoring-terminal-codex-replay-v1"
            event_payload = {
                "node_id": str(node["node_id"]),
                "attempt": int(node["attempt"]),
                "failed_attempt_count": len(failed) + int(node["status"] == "cancelled"),
                "previous_run_error": json.loads(str(run["error_json"] or "{}")),
                "previous_node_error": error,
                "previous_run_completed_at": run["completed_at"],
                "previous_node_completed_at": node["completed_at"],
                "workflow_version": workflow_version,
                "policy": replay_policy,
                "schema_recovery": schema_recovery,
                "coverage_recovery": coverage_recovery,
                "index_derivation_recovery": index_derivation_recovery,
                "draft_metadata_recovery": draft_metadata_failure,
                "v31_draft_handoff_recovery": v31_draft_handoff_failure,
                "v31_footnote_anchor_recovery": v31_footnote_anchor_recovery,
                "v31_citation_anchor_surface_recovery": (v31_citation_anchor_surface_recovery),
                "v31_support_sentence_hint_recovery": (v31_support_sentence_hint_recovery),
                "v31_conclusion_scholarship_conclusion_anchor_recovery": (
                    v31_conclusion_scholarship_conclusion_anchor_recovery
                ),
                "v31_semantic_attribution_predicate_recovery": (
                    v31_semantic_attribution_predicate_recovery
                ),
                "v31_fixed_source_lineage_recovery": (v31_fixed_source_lineage_recovery),
                "v31_bibliographic_contextual_recovery": (v31_bibliographic_contextual_recovery),
                "v31_cancelled_self_citation_recovery": (v31_cancelled_self_citation_recovery),
                "v31_cancelled_semantic_audit_reroute": (v31_cancelled_semantic_audit_reroute),
                "v31_audit_join_classification_recovery": (v31_audit_join_classification_recovery),
                "v31_audit_join_dispatch_recovery": v31_audit_join_dispatch_recovery,
                "audit_claim_target_recovery": audit_claim_target_recovery,
                "prose_basis_diagnostic_recovery": prose_basis_diagnostic_recovery,
                "v31_repair_source_retirement_recovery": (
                    v31_repair_source_retirement_recovery
                ),
                "v31_cancelled_rag_context_recovery": (
                    v31_cancelled_rag_context_recovery
                ),
                "v31_structure_aware_draft_recovery": (
                    v31_structure_aware_draft_recovery
                ),
                "v31_repair_handoff_recovery": v31_repair_handoff_failure,
                "v31_repair_transport_recovery": v31_repair_transport_failure,
                "v31_required_citation_contract_recovery": (
                    v31_required_citation_contract_recovery
                ),
                "v31_citation_source_claim_mapping_recovery": (
                    v31_citation_source_claim_mapping_recovery
                ),
                "v31_citation_source_claim_mapping_followup_recovery": (
                    v31_citation_source_claim_mapping_followup_recovery
                ),
                "v31_citation_source_claim_mapping_post_fix_replay": (
                    v31_citation_source_claim_mapping_post_fix_replay
                ),
                "v31_section_contract_identity_recovery": (v31_section_contract_identity_recovery),
                "v31_missing_draft_source_review": v31_missing_draft_source_review,
                "v31_audit_repair_exhausted_recovery": (v31_audit_repair_exhausted_failure),
                "v31_polish_role_recovery": v31_polish_role_failure,
                "v31_coherence_packet_projection_recovery": (
                    v31_coherence_packet_projection_failure
                ),
                "v31_final_reader_facing_recovery": (v31_final_reader_facing_failure),
                "draft_contract_evidence_recovery": draft_contract_evidence_recovery,
                "v31_repair_metadata_recovery": v31_repair_metadata_recovery,
                "footnote_metadata_recovery": footnote_metadata_failure,
                "v31_aggregated_self_citation_recovery": (v31_aggregated_self_citation_failure),
                "v31_provisional_bibliography_recovery": (v31_provisional_bibliography_recovery),
                "proposal_revalidation_recovery": proposal_revalidation_failure,
                "proposal_policy_recovery": proposal_policy_recovery,
                "checkpoint_plan_recovery": checkpoint_plan_failure,
                "memo_annotation_preservation_recovery": (memo_annotation_preservation_failure),
                "claim_tree_provenance_recovery": claim_tree_provenance_failure,
                "v31_claim_tree_branch_recovery": v31_claim_tree_branch_failure,
                "argument_graph_timeout_recovery": argument_graph_timeout_failure,
                "argument_graph_attribution_recovery": (argument_graph_attribution_recovery),
                "v31_graph_scope_basis_recovery": v31_graph_scope_basis_failure,
                "v31_graph_scope_basis_followup_recovery": (
                    v31_graph_scope_basis_followup_recovery
                ),
                "v31_graph_core_atom_recovery": v31_graph_core_atom_failure,
                "v31_graph_batch_attribution_recovery": (v31_graph_batch_attribution_failure),
                "v31_graph_repair_drift_recovery": v31_graph_repair_drift_failure,
                "v31_graph_rebuttal_link_recovery": v31_graph_rebuttal_link_failure,
                "v31_graph_provenance_recovery": v31_graph_provenance_recovery,
                "v31_defense_reference_recovery": v31_defense_reference_failure,
                "v31_section_role_dependency_recovery": (v31_section_role_dependency_failure),
                "v31_global_defense_assignment_recovery": (v31_global_defense_assignment_failure),
                "atomic_evidence_policy_recovery": atomic_evidence_policy_failure,
                "structural_repair_mapping_recovery": structural_repair_mapping_failure,
                "candidate_reconciliation_recovery": (candidate_reconciliation_failure),
                "submission_reconciliation_recovery": (submission_reconciliation_failure),
                "idempotent_footnote_reference_structure_recovery": (
                    idempotent_footnote_reference_structure_failure
                ),
                "v31_uncited_reference_section_removal_recovery": (
                    v31_uncited_reference_section_removal_failure
                ),
                "transient_submission_database_lock_recovery": (transient_submission_database_lock),
                "compound_coordination_surface_recheck": (compound_coordination_surface_recheck),
                "v31_research_problem_ratification_recovery": (
                    v31_research_problem_ratification_recovery
                ),
                "v31_discourse_synthesis_recovery": v31_discourse_synthesis_recovery,
                "v31_discourse_synthesis_checkpoint_recovery": (
                    v31_discourse_synthesis_checkpoint_recovery
                ),
            }
            self.connection.execute(
                "INSERT INTO event_log(run_id,event_type,object_type,object_id,"
                "payload_json,created_at) "
                "VALUES (?,'run.failed_authoring_node_replayed','node_run',?,?,?)",
                (run_id, node["id"], _json_dump(event_payload), now),
            )
            replay_input_json: str | None = None
            replay_node_id = str(node["node_id"])
            replay_attempt = int(node["attempt"])
            replay_resource_lane: str | None = None
            if (
                v31_cancelled_semantic_audit_reroute
                or v31_audit_join_classification_recovery
                or v31_audit_join_dispatch_recovery
            ):
                replay_attempt = int(
                    self.connection.execute(
                        "SELECT COALESCE(MAX(attempt),0)+1 FROM node_runs "
                        "WHERE run_id=? AND node_id='repair_failed_sections'",
                        (run_id,),
                    ).fetchone()[0]
                )
                unpacked = {
                    **unpacked,
                    "authoring_recovery_lineage": {
                        "source_node_id": "repair_failed_sections",
                        "source_node_run_id": str(node["id"]),
                        "policy": replay_policy,
                    },
                }
                replay_input_json = _json_dump(PayloadStore(self.connection).pack(unpacked))
            if v31_repair_source_retirement_recovery:
                replay_attempt = int(
                    self.connection.execute(
                        "SELECT COALESCE(MAX(attempt),0)+1 FROM node_runs "
                        "WHERE run_id=? AND node_id='repair_failed_sections'",
                        (run_id,),
                    ).fetchone()[0]
                )
                replay_resource_lane = "codex_remote"
                unpacked = {
                    **unpacked,
                    "authoring_recovery_lineage": {
                        "source_node_id": "repair_failed_sections",
                        "source_node_run_id": str(node["id"]),
                        "policy": "authoring-v31-retired-source-human-review-v1",
                    },
                }
                replay_input_json = _json_dump(PayloadStore(self.connection).pack(unpacked))
            if v31_cancelled_self_citation_recovery:
                replay_attempt = int(
                    self.connection.execute(
                        "SELECT COALESCE(MAX(attempt),0)+1 FROM node_runs "
                        "WHERE run_id=? AND node_id='repair_failed_sections'",
                        (run_id,),
                    ).fetchone()[0]
                )
                replay_resource_lane = "codex_remote"
                unpacked = {
                    **unpacked,
                    "authoring_recovery_lineage": {
                        "source_node_id": "repair_failed_sections",
                        "source_node_run_id": str(node["id"]),
                        "policy": ("authoring-v31-provisional-self-citation-repair-v1"),
                    },
                }
                replay_input_json = _json_dump(PayloadStore(self.connection).pack(unpacked))
            if proposal_revalidation_failure:
                unpacked["authoring_submission_revalidation"] = {
                    "policy": "authoring-submission-citation-id-revalidation-v1",
                    "recovered_from_event": True,
                }
                replay_input_json = _json_dump(unpacked)
            if v31_audit_repair_exhausted_failure:
                replay_node_id = "repair_failed_sections"
                replay_attempt = int(
                    self.connection.execute(
                        "SELECT COALESCE(MAX(attempt),0)+1 FROM node_runs "
                        "WHERE run_id=? AND node_id='repair_failed_sections'",
                        (run_id,),
                    ).fetchone()[0]
                )
                replay_resource_lane = "codex_remote"
                unpacked = {
                    **unpacked,
                    "repair_count": 0,
                    "authoring_structure_repair_policy": (_V31_STRUCTURE_REPAIR_POLICY),
                    "extended_structure_repair_count": int(
                        unpacked.get("extended_structure_repair_count", 0)
                    )
                    + 1,
                    "authoring_extended_structure_repair_signatures": [
                        *sorted(v31_seen_audit_repair_signatures),
                        *(
                            [v31_audit_repair_signature]
                            if v31_audit_repair_signature is not None
                            else []
                        ),
                    ],
                    "authoring_recovery_lineage": {
                        "source_node_id": "join_draft_audits",
                        "source_node_run_id": str(node["id"]),
                        "policy": "authoring-v31-extended-structure-repair-v1",
                    },
                }
                replay_input_json = _json_dump(PayloadStore(self.connection).pack(unpacked))
            if v31_repair_handoff_failure:
                replay_attempt = int(
                    self.connection.execute(
                        "SELECT COALESCE(MAX(attempt),0)+1 FROM node_runs "
                        "WHERE run_id=? AND node_id='repair_failed_sections'",
                        (run_id,),
                    ).fetchone()[0]
                )
                replay_resource_lane = "codex_remote"
                unpacked = {
                    **unpacked,
                    "authoring_recovery_lineage": {
                        "source_node_id": "repair_failed_sections",
                        "source_node_run_id": str(node["id"]),
                        "policy": "authoring-v31-repair-handoff-replay-v1",
                    },
                }
                replay_input_json = _json_dump(PayloadStore(self.connection).pack(unpacked))
            if v31_required_citation_contract_recovery:
                replay_attempt = int(
                    self.connection.execute(
                        "SELECT COALESCE(MAX(attempt),0)+1 FROM node_runs "
                        "WHERE run_id=? AND node_id='repair_failed_sections'",
                        (run_id,),
                    ).fetchone()[0]
                )
                replay_resource_lane = "codex_remote"
                unpacked = {
                    **unpacked,
                    "authoring_recovery_lineage": {
                        "source_node_id": "repair_failed_sections",
                        "source_node_run_id": str(node["id"]),
                        "policy": ("authoring-v31-required-citation-placement-recovery-v1"),
                    },
                }
                replay_input_json = _json_dump(PayloadStore(self.connection).pack(unpacked))
            if v31_final_reader_facing_failure:
                leaked_section_ids = [
                    section_id.strip()
                    for section_id in error_message.split(": ", 1)[1].split(",")
                    if section_id.strip()
                ]
                replay_node_id = "repair_failed_sections"
                replay_attempt = int(
                    self.connection.execute(
                        "SELECT COALESCE(MAX(attempt),0)+1 FROM node_runs "
                        "WHERE run_id=? AND node_id='repair_failed_sections'",
                        (run_id,),
                    ).fetchone()[0]
                )
                replay_resource_lane = "codex_remote"
                unpacked = {
                    **unpacked,
                    "repair_count": 0,
                    "audit_findings": [
                        {
                            "code": "verification_language_leak",
                            "severity": "major",
                            "object_type": "section",
                            "object_id": section_id,
                            "repair_scope": "local_section",
                            "message": (
                                "Final validation found internal planning or verification "
                                "language in this reader-facing section. Preserve its "
                                "substantive answer and evidence scope, but state the subject "
                                "and relation directly without internal comparison labels."
                            ),
                        }
                        for section_id in leaked_section_ids
                    ],
                    "authoring_recovery_lineage": {
                        "source_node_id": "finalize_authoring",
                        "source_node_run_id": str(node["id"]),
                        "policy": "authoring-v31-final-reader-facing-repair-v1",
                    },
                }
                replay_input_json = _json_dump(PayloadStore(self.connection).pack(unpacked))
            self.connection.execute(
                "UPDATE node_runs SET node_id=?, attempt=?, "
                "resource_lane=COALESCE(?,resource_lane), "
                "status='queued', started_at=NULL, completed_at=NULL, "
                "error_json=NULL, output_json=NULL, input_json=COALESCE(?,input_json), "
                "lease_owner=NULL, lease_acquired_at=NULL, lease_heartbeat_at=NULL, "
                "lease_expires_at=NULL WHERE id=? AND status=?",
                (
                    replay_node_id,
                    replay_attempt,
                    replay_resource_lane,
                    replay_input_json,
                    node["id"],
                    node["status"],
                ),
            )
            self.connection.execute(
                "UPDATE runs SET status='queued', error_json=NULL, completed_at=NULL, "
                "output_json=NULL WHERE id=? AND status=?",
                (run_id, run["status"]),
            )
            self.connection.commit()
            return {
                "run_id": run_id,
                "node_run_id": str(node["id"]),
                "node_id": replay_node_id,
                "attempt": replay_attempt,
                "status": "queued",
                "manual_replay_count": prior_replays + 1,
            }
        except Exception:
            self.connection.rollback()
            raise

    def restart_authoring_from_node(
        self,
        run_id: str,
        *,
        entry_node: str,
        downstream_node_ids: set[str],
        resource_lane: str,
    ) -> dict[str, Any]:
        """Clear one terminal V31 authoring tail and queue it again in place.

        A draft or prose restart may also supersede an untouched human approval wait.
        """

        descendants = {str(item) for item in downstream_node_ids if str(item)}
        if entry_node not in descendants:
            raise FukamiruError("Restart entry node is missing from its downstream set")
        now = utc_now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            run = self.connection.execute(
                "SELECT workflow_name,workflow_version,status FROM runs WHERE id=?",
                (run_id,),
            ).fetchone()
            if (
                run is None
                or not is_manuscript_generator_workflow(run["workflow_name"])
                or str(run["workflow_version"]) not in {"31", "32"}
                or str(run["status"]) not in {"failed", "cancelled", "waiting_human"}
            ):
                raise FukamiruError(
                    "In-place restart requires a failed, cancelled, or "
                    "human-waiting V31 authoring Run"
                )
            active_nodes = self.connection.execute(
                "SELECT node_id,status FROM node_runs WHERE run_id=? "
                "AND status IN ('queued','running','waiting_human')",
                (run_id,),
            ).fetchall()
            approval_wait = (
                str(run["status"]) == "waiting_human"
                and entry_node in {"draft_rewrite", "redraft_body_prose"}
                and len(active_nodes) == 1
                and active_nodes[0]["node_id"] == "human_approval"
                and active_nodes[0]["status"] == "waiting_human"
                and "human_approval" in descendants
            )
            if active_nodes and not approval_wait:
                raise FukamiruError(
                    "Cannot restart a Run with an active node: "
                    f"{active_nodes[0]['node_id']} ({active_nodes[0]['status']})"
                )
            if str(run["status"]) == "waiting_human" and not approval_wait:
                raise FukamiruError(
                    "Only an untouched human approval may restart from draft or prose"
                )
            node_rows = self.connection.execute(
                "SELECT rowid AS sequence_id,id,node_id,attempt,status,input_json,started_at "
                "FROM node_runs WHERE run_id=? AND node_id=? "
                "ORDER BY rowid DESC",
                (run_id, entry_node),
            ).fetchall()
            source = next(
                (
                    row
                    for row in node_rows
                    if row["status"] in {"completed", "failed", "cancelled", "skipped"}
                ),
                None,
            )
            if source is None:
                raise FukamiruError(f"Run has no terminal input snapshot for {entry_node}")
            input_json = str(source["input_json"] or "{}")
            try:
                input_context = PayloadStore(self.connection).unpack(json.loads(input_json))
            except (TypeError, ValueError) as exc:
                raise FukamiruError(f"Saved input snapshot for {entry_node} is unreadable") from exc
            early_evidence_handoff = (
                entry_node == "decide_evidence_realization"
                and isinstance(input_context, dict)
                and all(
                    isinstance(input_context.get(key), list)
                    for key in (
                        "propositions",
                        "claims",
                        "claim_proposition_bindings",
                        "proposition_verification_scopes",
                        "atomic_evidence_obligations",
                        "evidence_context",
                        "evidence_coverage",
                    )
                )
            )
            later_handoff = (
                entry_node != "decide_evidence_realization"
                and isinstance(input_context, dict)
                and bool(input_context.get("section_contracts"))
                and bool(input_context.get("research_problem"))
            )
            if (
                not isinstance(input_context, dict)
                or not input_context.get("plan_id")
                or not input_context.get("plan_revision")
                or not (early_evidence_handoff or later_handoff)
            ):
                raise FukamiruError(
                    f"Saved input snapshot for {entry_node} is not a complete V31 handoff"
                )
            new_plan_revision: int | None = None
            if early_evidence_handoff:
                existing_plan = self.connection.execute(
                    "SELECT current_revision FROM authoring_plans WHERE id=?",
                    (str(input_context["plan_id"]),),
                ).fetchone()
                if existing_plan is not None:
                    parent_revision = int(existing_plan["current_revision"])
                    new_plan_revision = parent_revision + 1
                    input_context = {
                        **input_context,
                        "parent_plan_revision": parent_revision,
                        "plan_revision": new_plan_revision,
                    }

            placeholders = ",".join("?" for _ in descendants)
            # A full draft restart deliberately clears all draft attempts.
            # Later restarts preserve earlier executions reached by repair
            # cycles, including an earlier execution of the entry node.
            boundary = node_rows[-1] if entry_node == "draft_rewrite" else source
            first_node_sequence = int(boundary["sequence_id"])
            next_attempt = 1 + max(
                (
                    int(row["attempt"])
                    for row in node_rows
                    if int(row["sequence_id"]) < first_node_sequence
                ),
                default=0,
            )
            downstream_rows = self.connection.execute(
                f"SELECT id,node_id FROM node_runs WHERE run_id=? "
                f"AND rowid>=? AND node_id IN ({placeholders}) ORDER BY rowid",
                (run_id, first_node_sequence, *sorted(descendants)),
            ).fetchall()
            historical_node_run_ids: set[str] = set()
            if early_evidence_handoff:
                # Locked plans are immutable. Their producer node and its
                # event remain as historical lineage while the rerun creates
                # a new plan revision from the saved pre-structure context.
                candidate_ids = [str(row["id"]) for row in downstream_rows]
                candidate_placeholders = ",".join("?" for _ in candidate_ids)
                if candidate_ids:
                    for table in (
                        "authoring_plan_revisions",
                        "atomic_propositions",
                        "argument_nodes",
                    ):
                        exists = self.connection.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                            (table,),
                        ).fetchone()
                        if exists is None:
                            continue
                        historical_node_run_ids.update(
                            str(row[0])
                            for row in self.connection.execute(
                                f"SELECT DISTINCT producer_node_run_id FROM {table} "
                                f"WHERE plan_id=? AND producer_node_run_id IN "
                                f"({candidate_placeholders})",
                                (str(input_context["plan_id"]), *candidate_ids),
                            )
                            if row[0]
                        )
            node_run_ids = [
                str(row["id"])
                for row in downstream_rows
                if str(row["id"]) not in historical_node_run_ids
            ]
            if not node_run_ids:
                raise FukamiruError("No saved node records are eligible for restart")
            first_event = self.connection.execute(
                "SELECT event_id,created_at FROM event_log WHERE run_id=? "
                "AND object_type='node_run' AND object_id=? "
                "ORDER BY event_id LIMIT 1",
                (run_id, str(boundary["id"])),
            ).fetchone()
            if first_event is None:
                raise FukamiruError(f"Cannot determine the event boundary for {entry_node} restart")
            first_event_id = int(first_event["event_id"])
            cutoff_at = str(first_event["created_at"] or boundary["started_at"] or now)

            deleted_records: dict[str, int] = {}
            table_rows = self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            for table_row in table_rows:
                table = str(table_row["name"])
                if table in {"runs", "node_runs", "event_log"}:
                    continue
                if early_evidence_handoff and table in {
                    "authoring_plan_revisions",
                    "atomic_propositions",
                    "claim_tree_nodes",
                    "argument_nodes",
                    "argument_edges",
                    "hypothesis_candidates",
                    "discourse_nodes",
                    "section_contracts",
                }:
                    continue
                columns = {
                    str(row["name"])
                    for row in self.connection.execute(
                        f'PRAGMA table_info("{table.replace(chr(34), chr(34) * 2)}")'
                    ).fetchall()
                }
                conditions: list[str] = []
                parameters: list[Any] = []
                node_placeholders = ",".join("?" for _ in node_run_ids)
                for column in ("node_run_id", "producer_node_run_id"):
                    if column in columns:
                        conditions.append(f'"{column}" IN ({node_placeholders})')
                        parameters.extend(node_run_ids)
                time_columns = [
                    column
                    for column in (
                        "created_at",
                        "started_at",
                        "completed_at",
                        "updated_at",
                    )
                    if column in columns
                ]
                if "run_id" in columns and time_columns:
                    time_clause = " OR ".join(f'"{column}" >= ?' for column in time_columns)
                    conditions.append(f'("run_id"=? AND ({time_clause}))')
                    parameters.extend([run_id, *([cutoff_at] * len(time_columns))])
                if (
                    table == "manuscript_unit_checkpoints"
                    and entry_node == "draft_rewrite"
                    and {
                        "run_id",
                        "phase",
                    }.issubset(columns)
                ):
                    downstream_phases = (
                        "authoring-batch-v30:draft_body_v31",
                        "authoring-batch-v30:polish_v31",
                        "coherence:v27",
                        "coherence_candidate:v27",
                        "coherence_cross:v27_base",
                        "coherence_cross:v27_supplement",
                    )
                    phase_placeholders = ",".join("?" for _ in downstream_phases)
                    conditions.append(f'("run_id"=? AND "phase" IN ({phase_placeholders}))')
                    parameters.extend([run_id, *downstream_phases])
                if not conditions:
                    continue
                quoted_table = f'"{table.replace(chr(34), chr(34) * 2)}"'
                preserve_history = ""
                if historical_node_run_ids:
                    historical_placeholders = ",".join("?" for _ in historical_node_run_ids)
                    for column in ("node_run_id", "producer_node_run_id"):
                        if column in columns:
                            preserve_history += (
                                f' AND ("{column}" IS NULL OR "{column}" NOT IN '
                                f"({historical_placeholders}))"
                            )
                            parameters.extend(sorted(historical_node_run_ids))
                cursor = self.connection.execute(
                    f"DELETE FROM {quoted_table} WHERE ("
                    + " OR ".join(conditions)
                    + ")"
                    + preserve_history,
                    tuple(parameters),
                )
                if cursor.rowcount:
                    deleted_records[table] = int(cursor.rowcount)

            if historical_node_run_ids:
                historical_placeholders = ",".join("?" for _ in historical_node_run_ids)
                event_cursor = self.connection.execute(
                    "DELETE FROM event_log WHERE run_id=? AND event_id>=? "
                    "AND NOT (object_type='node_run' AND object_id IN "
                    f"({historical_placeholders}))",
                    (run_id, first_event_id, *sorted(historical_node_run_ids)),
                )
            else:
                event_cursor = self.connection.execute(
                    "DELETE FROM event_log WHERE run_id=? AND event_id>=?",
                    (run_id, first_event_id),
                )
            node_placeholders = ",".join("?" for _ in node_run_ids)
            self.connection.execute(
                f"DELETE FROM node_runs WHERE run_id=? AND id IN ({node_placeholders})",
                (run_id, *node_run_ids),
            )
            if new_plan_revision is not None:
                input_json = json.dumps(
                    PayloadStore(self.connection).pack(input_context),
                    ensure_ascii=False,
                )
            new_node_run_id = str(uuid.uuid4())
            self.connection.execute(
                "INSERT INTO node_runs("
                "id,run_id,node_id,attempt,status,input_json,resource_lane"
                ") VALUES(?,?,?,?, 'queued',?,?)",
                (
                    new_node_run_id,
                    run_id,
                    entry_node,
                    next_attempt,
                    input_json,
                    resource_lane,
                ),
            )
            self.connection.execute(
                "UPDATE runs SET status='queued',error_json=NULL,output_json=NULL,"
                "completed_at=NULL WHERE id=?",
                (run_id,),
            )
            events = EventRepository(self.connection)
            events.append(
                run_id,
                "run.authoring_restarted_from_node",
                object_type="run",
                object_id=run_id,
                payload={
                    "restart_node_id": entry_node,
                    "deleted_node_run_count": len(node_run_ids),
                    "deleted_event_count": int(event_cursor.rowcount),
                    "restart_policy": "v31-clear-current-tail-and-requeue-v2",
                    "new_plan_revision": new_plan_revision,
                    "preserved_historical_node_run_ids": sorted(historical_node_run_ids),
                },
            )
            queued_event = events.append(
                run_id,
                "node.queued",
                object_type="node_run",
                object_id=new_node_run_id,
                payload={
                    "node_id": entry_node,
                    "attempt": next_attempt,
                    "resource_lane": resource_lane,
                },
            )
            self.connection.commit()
            return {
                "run_id": run_id,
                "node_run_id": new_node_run_id,
                "node_id": entry_node,
                "attempt": next_attempt,
                "status": "queued",
                "event_id": queued_event.event_id,
                "deleted_node_run_count": len(node_run_ids),
                "deleted_event_count": int(event_cursor.rowcount),
                "deleted_record_counts": deleted_records,
                "new_plan_revision": new_plan_revision,
                "preserved_historical_node_run_ids": sorted(historical_node_run_ids),
            }
        except Exception:
            self.connection.rollback()
            raise

    def restart_v31_from_entry(
        self,
        run_id: str,
        *,
        entry_node: str,
        resource_lane: str,
    ) -> dict[str, Any]:
        """Clear one inactive waiting/failed V31 Run and restart its entry node."""
        now = utc_now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            run = self.connection.execute(
                "SELECT workflow_name,workflow_version,status,input_json FROM runs WHERE id=?",
                (run_id,),
            ).fetchone()
            if (
                run is None
                or not is_manuscript_generator_workflow(run["workflow_name"])
                or str(run["workflow_version"]) not in {"31", "32"}
                or str(run["status"]) not in {"waiting_human", "failed"}
            ):
                raise FukamiruError(
                    "Entry restart requires an inactive waiting or failed "
                    "V31 manuscript-generator Run"
                )
            active = self.connection.execute(
                "SELECT node_id,status FROM node_runs WHERE run_id=? "
                "AND status IN ('queued','running') LIMIT 1",
                (run_id,),
            ).fetchone()
            if active is not None:
                raise FukamiruError(
                    "Cannot restart a Run with an active node: "
                    f"{active['node_id']} ({active['status']})"
                )
            try:
                original_inputs = json.loads(str(run["input_json"] or "{}"))
            except (TypeError, ValueError) as exc:
                raise FukamiruError("Run's original input is unreadable") from exc
            if not isinstance(original_inputs, dict):
                raise FukamiruError("Run's original input is not an object")

            node_run_ids = [
                str(row["id"])
                for row in self.connection.execute(
                    "SELECT id FROM node_runs WHERE run_id=?", (run_id,)
                ).fetchall()
            ]
            table_rows = self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            immutable_delete_tables = {
                str(row["tbl_name"])
                for row in self.connection.execute(
                    "SELECT tbl_name, name, sql FROM sqlite_master WHERE type='trigger'"
                )
                if "before delete" in str(row["sql"] or "").lower()
                and (
                    "immutable" in str(row["name"] or "").lower()
                    or "immutable" in str(row["sql"] or "").lower()
                )
            }
            direct_tables: dict[str, set[str]] = {}
            for table_row in table_rows:
                table = str(table_row["name"])
                if table in {"runs", "node_runs", "event_log"}:
                    continue
                quoted = '"' + table.replace('"', '""') + '"'
                columns = {
                    str(row["name"])
                    for row in self.connection.execute(f"PRAGMA table_info({quoted})")
                }
                if not (
                    "run_id" in columns
                    or "node_run_id" in columns
                    or "producer_node_run_id" in columns
                ):
                    continue
                direct_tables[table] = columns

            protected_node_run_ids: set[str] = set()
            if node_run_ids:
                placeholders = ",".join("?" for _ in node_run_ids)
                for table in immutable_delete_tables:
                    if not self.connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (table,),
                    ).fetchone():
                        continue
                    quoted_table = '"' + table.replace('"', '""') + '"'
                    columns = {
                        str(row["name"])
                        for row in self.connection.execute(f"PRAGMA table_info({quoted_table})")
                    }
                    for column in ("node_run_id", "producer_node_run_id"):
                        if column not in columns:
                            continue
                        protected_node_run_ids.update(
                            str(row[0])
                            for row in self.connection.execute(
                                f'SELECT DISTINCT "{column}" FROM {quoted_table} '
                                f'WHERE "{column}" IN ({placeholders})',
                                tuple(node_run_ids),
                            )
                            if row[0]
                        )

            # Delete dependent records before their parent records, while keeping
            # the cleanup scoped to this Run and its node-run identifiers.
            children_by_parent: dict[str, set[str]] = {table: set() for table in direct_tables}
            for child in direct_tables:
                quoted = '"' + child.replace('"', '""') + '"'
                for fk in self.connection.execute(f"PRAGMA foreign_key_list({quoted})"):
                    parent = str(fk["table"])
                    if parent in children_by_parent:
                        children_by_parent[parent].add(child)
            delete_order: list[str] = []
            visited: set[str] = set()

            def visit(table: str) -> None:
                if table in visited:
                    return
                visited.add(table)
                for child in sorted(children_by_parent[table]):
                    visit(child)
                delete_order.append(table)

            for table in sorted(direct_tables):
                visit(table)

            deleted_records: dict[str, int] = {}
            for table in delete_order:
                if table in immutable_delete_tables:
                    continue
                columns = direct_tables[table]
                conditions: list[str] = []
                parameters: list[Any] = []
                if "run_id" in columns:
                    conditions.append('"run_id"=?')
                    parameters.append(run_id)
                if node_run_ids:
                    placeholders = ",".join("?" for _ in node_run_ids)
                    for column in ("node_run_id", "producer_node_run_id"):
                        if column in columns:
                            conditions.append(f'"{column}" IN ({placeholders})')
                            parameters.extend(node_run_ids)
                if not conditions:
                    continue
                quoted = '"' + table.replace('"', '""') + '"'
                cursor = self.connection.execute(
                    f"DELETE FROM {quoted} WHERE "
                    + " OR ".join(f"({condition})" for condition in conditions),
                    tuple(parameters),
                )
                if cursor.rowcount:
                    deleted_records[table] = int(cursor.rowcount)

            deleted_events = int(
                self.connection.execute("DELETE FROM event_log WHERE run_id=?", (run_id,)).rowcount
            )
            delete_nodes_sql = "DELETE FROM node_runs WHERE run_id=?"
            delete_nodes_parameters: tuple[Any, ...] = (run_id,)
            if protected_node_run_ids:
                placeholders = ",".join("?" for _ in protected_node_run_ids)
                delete_nodes_sql += f" AND id NOT IN ({placeholders})"
                delete_nodes_parameters = (run_id, *sorted(protected_node_run_ids))
            deleted_nodes = int(
                self.connection.execute(delete_nodes_sql, delete_nodes_parameters).rowcount
            )
            node_run_id = str(uuid.uuid4())
            entry_attempt = int(
                self.connection.execute(
                    "SELECT COALESCE(MAX(attempt),0)+1 FROM node_runs WHERE run_id=? AND node_id=?",
                    (run_id, entry_node),
                ).fetchone()[0]
            )
            self.connection.execute(
                "INSERT INTO node_runs("
                "id,run_id,node_id,attempt,status,input_json,resource_lane"
                ") VALUES(?,?,?,?,'queued',?,?)",
                (
                    node_run_id,
                    run_id,
                    entry_node,
                    entry_attempt,
                    _json_dump(original_inputs),
                    resource_lane,
                ),
            )
            self.connection.execute(
                "UPDATE runs SET status='queued',output_json=NULL,error_json=NULL,"
                "started_at=NULL,completed_at=NULL WHERE id=?",
                (run_id,),
            )
            reset_payload = {
                "restart_node_id": entry_node,
                "restart_node_attempt": entry_attempt,
                "deleted_node_run_count": deleted_nodes,
                "deleted_event_count": deleted_events,
                "deleted_record_counts": deleted_records,
                "retained_immutable_node_run_count": len(protected_node_run_ids),
                "restart_policy": "v31-clear-mutable-run-records-and-requeue-entry-v2",
            }
            self.connection.execute(
                "INSERT INTO event_log("
                "run_id,event_type,object_type,object_id,payload_json,created_at"
                ") VALUES(?,?,?,?,?,?)",
                (
                    run_id,
                    "run.authoring_restarted_from_entry",
                    "run",
                    run_id,
                    _json_dump(reset_payload),
                    now,
                ),
            )
            queued = self.connection.execute(
                "INSERT INTO event_log("
                "run_id,event_type,object_type,object_id,payload_json,created_at"
                ") VALUES(?,?,?,?,?,?)",
                (
                    run_id,
                    "node.queued",
                    "node_run",
                    node_run_id,
                    _json_dump(
                        {
                            "node_id": entry_node,
                            "attempt": entry_attempt,
                            "resource_lane": resource_lane,
                        }
                    ),
                    now,
                ),
            )
            self.connection.commit()
            return {
                "run_id": run_id,
                "node_run_id": node_run_id,
                "node_id": entry_node,
                "attempt": entry_attempt,
                "status": "queued",
                "event_id": int(queued.lastrowid),
                "deleted_node_run_count": deleted_nodes,
                "deleted_event_count": deleted_events,
                "deleted_record_counts": deleted_records,
                "retained_immutable_node_run_count": len(protected_node_run_ids),
            }
        except Exception:
            self.connection.rollback()
            raise

    def recover_v31_provisional_submission_join(self, run_id: str) -> dict[str, Any]:
        """Resume the saved V31 join after a concurrent structure DB lock.

        The previous successful structure validation is reused only for the
        identical candidate; a queued repair from the stale failed join is
        retired atomically before the new join can run.
        """
        now = utc_now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            run = self.connection.execute(
                "SELECT workflow_name,workflow_version,status,error_json FROM runs WHERE id=?",
                (run_id,),
            ).fetchone()
            if (
                run is None
                or not is_manuscript_generator_workflow(run["workflow_name"])
                or str(run["workflow_version"]) not in {"31", "32"}
                or run["status"] != "failed"
                or json.loads(run["error_json"] or "{}").get("message") != "database is locked"
            ):
                raise FukamiruError("Run is not the failed V31 submission lock recovery")
            if self.connection.execute(
                "SELECT COUNT(*) FROM event_log WHERE run_id=? "
                "AND event_type='run.v31_provisional_submission_join_recovered'",
                (run_id,),
            ).fetchone()[0]:
                raise FukamiruError("V31 provisional submission join was already recovered")
            if self.connection.execute(
                "SELECT COUNT(*) FROM authoring_pending_proposals WHERE run_id=?", (run_id,)
            ).fetchone()[0]:
                raise FukamiruError("Submission already has a proposal")
            active = self.connection.execute(
                "SELECT id,node_id,status FROM node_runs WHERE run_id=? "
                "AND status IN ('queued','running','waiting_human')",
                (run_id,),
            ).fetchall()
            if (
                len(active) != 1
                or active[0]["status"] != "queued"
                or active[0]["node_id"] != "repair_final_coherence"
            ):
                raise FukamiruError("Unexpected active nodes at V31 submission join")
            failed = self.connection.execute(
                "SELECT id,input_json,error_json FROM node_runs WHERE run_id=? "
                "AND node_id='validate_footnoted_structure' AND status='failed' "
                "ORDER BY attempt DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            previous_structure = self.connection.execute(
                "SELECT output_json FROM node_runs WHERE run_id=? "
                "AND node_id='validate_footnoted_structure' AND status='completed' "
                "ORDER BY attempt DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            join = self.connection.execute(
                "SELECT id,input_json,output_json,resource_lane FROM node_runs WHERE run_id=? "
                "AND node_id='join_submission_validations' AND status='completed' "
                "ORDER BY attempt DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            if not failed or not previous_structure or not join:
                raise FukamiruError("V31 submission checkpoints are incomplete")
            if json.loads(failed["error_json"] or "{}").get("message") != "database is locked":
                raise FukamiruError("V31 structure failure is not a transient DB lock")
            payloads = PayloadStore(self.connection)
            failed_context = payloads.unpack(json.loads(failed["input_json"] or "{}"))
            prior_context = payloads.unpack(json.loads(previous_structure["output_json"] or "{}"))
            join_context = payloads.unpack(json.loads(join["input_json"] or "{}"))
            joined = payloads.unpack(json.loads(join["output_json"] or "{}"))
            candidate = str(join_context.get("proposed_html") or "")
            candidate_sha = hashlib.sha256(candidate.encode()).hexdigest()
            gate = dict(joined.get("submission_validation_gate") or {})
            if (
                not candidate
                or str(failed_context.get("proposed_html") or "") != candidate
                or str(prior_context.get("proposed_html") or "") != candidate
                or dict(prior_context.get("rendered_structure_v26") or {}).get("valid") is not True
                or dict(join_context.get("rendered_structure_v26") or {}).get("valid") is not True
                or gate.get("candidate_sha256") != candidate_sha
                or gate.get("failed_validations") != ["reference_quality", "reference_integrity"]
            ):
                raise FukamiruError("V31 submission candidates or validation differ")
            from fukamiru.worker.runner import WorkerRunner

            report = dict(join_context.get("reference_quality_gate") or {})
            if not WorkerRunner._v31_reviewable_provisional_bibliography_ids(join_context, report):
                raise FukamiruError("V31 bibliography failure is not reviewable")
            generation = str(uuid.uuid4())
            join_context["_join_generations"] = {
                **dict(join_context.get("_join_generations") or {}),
                "join_submission_validations": generation,
            }
            attempt = int(
                self.connection.execute(
                    "SELECT COALESCE(MAX(attempt),0)+1 FROM node_runs "
                    "WHERE run_id=? AND node_id='join_submission_validations'",
                    (run_id,),
                ).fetchone()[0]
            )
            next_id = str(uuid.uuid4())
            self.connection.execute(
                "UPDATE node_runs SET status='cancelled',completed_at=?, "
                "error_json=? WHERE id=? AND status='queued'",
                (now, _json_dump({"type": "SupersededBySubmissionJoinRecovery"}), active[0]["id"]),
            )
            self.connection.execute(
                "INSERT INTO node_runs(id,run_id,node_id,attempt,status,input_json,resource_lane) "
                "VALUES (?,?,'join_submission_validations',?,'queued',?,?)",
                (
                    next_id,
                    run_id,
                    attempt,
                    _json_dump(payloads.pack(join_context)),
                    join["resource_lane"],
                ),
            )
            self.connection.execute(
                "UPDATE runs SET status='queued',error_json=NULL,completed_at=NULL,"
                "output_json=NULL "
                "WHERE id=? AND status='failed'",
                (run_id,),
            )
            event = {
                "policy": "v31-provisional-submission-join-v1",
                "candidate_sha256": candidate_sha,
                "retired_repair_node_run_id": active[0]["id"],
                "reused_structure": True,
                "queued_node_run_id": next_id,
            }
            self.connection.execute(
                "INSERT INTO event_log(run_id,event_type,object_type,object_id,"
                "payload_json,created_at) "
                "VALUES (?,'run.v31_provisional_submission_join_recovered','run',?,?,?)",
                (run_id, run_id, _json_dump(event), now),
            )
            self.connection.execute(
                "INSERT INTO event_log(run_id,event_type,object_type,object_id,"
                "payload_json,created_at) "
                "VALUES (?,'node.queued','node_run',?,?,?)",
                (
                    run_id,
                    next_id,
                    _json_dump({"node_id": "join_submission_validations", "attempt": attempt}),
                    now,
                ),
            )
            self.connection.commit()
            return {
                "run_id": run_id,
                "node_run_id": next_id,
                "node_id": "join_submission_validations",
                "attempt": attempt,
                "status": "queued",
            }
        except Exception:
            self.connection.rollback()
            raise

    def revalidate_authoring_submission(self, run_id: str) -> dict[str, Any]:
        """Re-run the V30 submission tail after the singular citation-ID gate fix.

        The three unaffected footnoted validators and the proofreading shadow are
        copied into a fresh join generation as explicit checkpoint reuses.  Only
        ``validate_reference_quality`` and its deterministic downstream tail are
        executed again; draft, proofreading, and coherence LLM work is retained.
        """

        now = utc_now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            run = self.connection.execute(
                "SELECT workflow_name,workflow_version,status,error_json FROM runs WHERE id=?",
                (run_id,),
            ).fetchone()
            prior_starts = int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM event_log WHERE run_id=? "
                    "AND event_type='run.authoring_submission_revalidation_started'",
                    (run_id,),
                ).fetchone()[0]
            )
            recovery = False
            if run is not None and run["status"] == "failed" and prior_starts == 1:
                failed_join = self.connection.execute(
                    "SELECT node_id,error_json FROM node_runs WHERE run_id=? "
                    "AND status='failed' ORDER BY rowid DESC LIMIT 1",
                    (run_id,),
                ).fetchone()
                failed_error = (
                    json.loads(str(failed_join["error_json"] or "{}"))
                    if failed_join is not None
                    else {}
                )
                recovery = bool(
                    failed_join is not None
                    and failed_join["node_id"] == "validate_reference_quality"
                    and str(failed_error.get("message") or "").startswith(
                        "Join context conflict at "
                        "validate_footnoted_references.reference_quality_gate"
                    )
                )
            if (
                run is None
                or not is_manuscript_generator_workflow(run["workflow_name"])
                or run["workflow_version"] != "30"
                or not (run["status"] == "waiting_human" or recovery)
            ):
                raise FukamiruError("Run is not a waiting-human V30 authoring submission")
            if prior_starts and not recovery:
                raise FukamiruError("Authoring submission revalidation already started")

            quality = self.connection.execute(
                "SELECT * FROM node_runs WHERE run_id=? "
                "AND node_id='validate_reference_quality' AND status='completed' "
                "ORDER BY attempt DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            human = self.connection.execute(
                "SELECT * FROM node_runs WHERE run_id=? "
                "AND node_id='human_approval' "
                "ORDER BY attempt DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            if quality is None or human is None:
                raise FukamiruError("Authoring submission has no replayable quality gate")
            quality_input = json.loads(str(quality["input_json"] or "{}"))
            quality_output = json.loads(str(quality["output_json"] or "{}"))
            issues = list((quality_output.get("reference_quality_gate") or {}).get("issues") or [])
            singular_events = [
                event
                for event in quality_input.get("citation_event_drafts") or []
                if isinstance(event, dict) and str(event.get("evidence_id") or "")
            ]
            if not singular_events or not any(
                str(issue.get("code") or "") == "reference.required_author_citation_missing"
                for issue in issues
                if isinstance(issue, dict)
            ):
                raise FukamiruError(
                    "Authoring submission is not affected by the singular citation-ID gate"
                )

            submission_generation = str(uuid.uuid4())
            finding_generation = str(uuid.uuid4())

            def with_generations(raw: str | None, *, drop_keys: tuple[str, ...] = ()) -> str:
                context = json.loads(str(raw or "{}"))
                for key in drop_keys:
                    context.pop(key, None)
                generations = dict(context.get("_join_generations") or {})
                generations["join_submission_validations"] = submission_generation
                generations["audit_final_findings"] = finding_generation
                context["_join_generations"] = generations
                context["authoring_submission_revalidation"] = {
                    "policy": "authoring-submission-citation-id-revalidation-v1",
                    "recovery": recovery,
                    "submission_join_generation": submission_generation,
                    "finding_join_generation": finding_generation,
                }
                return _json_dump(context)

            reused_nodes: list[dict[str, Any]] = []
            for node_id in (
                "validate_footnoted_references",
                "validate_footnoted_structure",
                "validate_footnoted_coherence",
                "publish_proofreading_shadow",
            ):
                source = self.connection.execute(
                    "SELECT * FROM node_runs WHERE run_id=? AND node_id=? "
                    "AND status='completed' ORDER BY attempt DESC LIMIT 1",
                    (run_id, node_id),
                ).fetchone()
                if source is None:
                    raise FukamiruError(f"Authoring submission checkpoint is missing: {node_id}")
                attempt = int(
                    self.connection.execute(
                        "SELECT COALESCE(MAX(attempt),0)+1 FROM node_runs "
                        "WHERE run_id=? AND node_id=?",
                        (run_id, node_id),
                    ).fetchone()[0]
                )
                node_run_id = str(uuid.uuid4())
                drop_keys = (
                    ("reference_quality_gate",)
                    if node_id == "validate_footnoted_references"
                    else ()
                )
                self.connection.execute(
                    "INSERT INTO node_runs("
                    "id,run_id,node_id,attempt,status,input_json,output_json,"
                    "started_at,completed_at,resource_lane) "
                    "VALUES (?,?,?,?, 'completed',?,?,?,?,?)",
                    (
                        node_run_id,
                        run_id,
                        node_id,
                        attempt,
                        with_generations(source["input_json"], drop_keys=drop_keys),
                        with_generations(source["output_json"], drop_keys=drop_keys),
                        now,
                        now,
                        source["resource_lane"],
                    ),
                )
                reused_nodes.append(
                    {
                        "node_run_id": node_run_id,
                        "node_id": node_id,
                        "attempt": attempt,
                    }
                )

            quality_attempt = int(
                self.connection.execute(
                    "SELECT COALESCE(MAX(attempt),0)+1 FROM node_runs "
                    "WHERE run_id=? AND node_id='validate_reference_quality'",
                    (run_id,),
                ).fetchone()[0]
            )
            quality_node_run_id = str(uuid.uuid4())
            self.connection.execute(
                "INSERT INTO node_runs("
                "id,run_id,node_id,attempt,status,input_json,resource_lane) "
                "VALUES (?,?, 'validate_reference_quality',?, 'queued',?,?)",
                (
                    quality_node_run_id,
                    run_id,
                    quality_attempt,
                    with_generations(quality["input_json"]),
                    quality["resource_lane"],
                ),
            )
            if human["status"] == "waiting_human":
                self.connection.execute(
                    "UPDATE node_runs SET status='completed',completed_at=? "
                    "WHERE id=? AND status='waiting_human'",
                    (now, human["id"]),
                )
            self.connection.execute(
                "UPDATE runs SET status='queued',error_json=NULL,completed_at=NULL,"
                "output_json=NULL WHERE id=? AND status IN ('waiting_human','failed')",
                (run_id,),
            )
            payload = {
                "policy": "authoring-submission-citation-id-revalidation-v1",
                "previous_human_approval_node_run_id": str(human["id"]),
                "quality_node_run_id": quality_node_run_id,
                "quality_attempt": quality_attempt,
                "singular_citation_event_count": len(singular_events),
                "submission_join_generation": submission_generation,
                "finding_join_generation": finding_generation,
                "reused_nodes": reused_nodes,
                "recovery": recovery,
            }
            self.connection.execute(
                "INSERT INTO event_log(run_id,event_type,object_type,object_id,"
                "payload_json,created_at) VALUES "
                "(?,'run.authoring_submission_revalidation_started','run',?,?,?)",
                (run_id, run_id, _json_dump(payload), now),
            )
            for reused in reused_nodes:
                self.connection.execute(
                    "INSERT INTO event_log(run_id,event_type,object_type,object_id,"
                    "payload_json,created_at) VALUES "
                    "(?,'node.checkpoint_reused','node_run',?,?,?)",
                    (
                        run_id,
                        reused["node_run_id"],
                        _json_dump(
                            {
                                "node_id": reused["node_id"],
                                "attempt": reused["attempt"],
                                "policy": payload["policy"],
                            }
                        ),
                        now,
                    ),
                )
            self.connection.execute(
                "INSERT INTO event_log(run_id,event_type,object_type,object_id,"
                "payload_json,created_at) VALUES "
                "(?,'node.queued','node_run',?,?,?)",
                (
                    run_id,
                    quality_node_run_id,
                    _json_dump(
                        {
                            "node_id": "validate_reference_quality",
                            "attempt": quality_attempt,
                            "resource_lane": quality["resource_lane"],
                            "policy": payload["policy"],
                        }
                    ),
                    now,
                ),
            )
            self.connection.commit()
            return {
                "run_id": run_id,
                "node_run_id": quality_node_run_id,
                "node_id": "validate_reference_quality",
                "attempt": quality_attempt,
                "status": "queued",
                "reused_node_count": len(reused_nodes),
                "singular_citation_event_count": len(singular_events),
            }
        except Exception:
            self.connection.rollback()
            raise

    def resume_v31_reader_facing_render_tail(
        self, run_id: str, *, allow_second_attempt: bool = False
    ) -> dict[str, Any]:
        """Re-render one failed V31 candidate after a proven self-label display bug.

        The saved pre-footnote input is reused. Earlier writing nodes are not
        replayed, and an already queued/running node or proposal blocks this path.
        """

        from fukamiru.revisions.bibliography_footnote_style import format_footnote_reference
        from fukamiru.revisions.bibliography_style import format_reference
        from fukamiru.worker.runner import WorkerRunner

        now = utc_now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            run = self.connection.execute(
                "SELECT workflow_name,workflow_version,status FROM runs WHERE id=?",
                (run_id,),
            ).fetchone()
            failed = self.connection.execute(
                "SELECT id,input_json,error_json FROM node_runs WHERE run_id=? "
                "AND node_id='finalize_authoring' AND status='failed' "
                "ORDER BY attempt DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            if (
                run is None
                or not is_manuscript_generator_workflow(run["workflow_name"])
                or str(run["workflow_version"]) not in {"31", "32"}
                or run["status"] != "failed"
                or failed is None
            ):
                raise FukamiruError("Run is not a failed V31 finalization")
            error = json.loads(str(failed["error_json"] or "{}"))
            if not str(error.get("message") or "").startswith(
                "V31 final manuscript contains reader-facing input scaffolding: "
            ):
                raise FukamiruError("Finalization failed for another reason")
            if self.connection.execute(
                "SELECT COUNT(*) FROM node_runs WHERE run_id=? "
                "AND status IN ('queued','running','waiting_human')",
                (run_id,),
            ).fetchone()[0]:
                raise FukamiruError("Run has active nodes")
            if self.connection.execute(
                "SELECT COUNT(*) FROM authoring_pending_proposals WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]:
                raise FukamiruError("Run has a pending proposal")
            prior_events = self.connection.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.v31_reader_facing_render_tail_resumed' "
                "ORDER BY rowid",
                (run_id,),
            ).fetchall()
            if prior_events:
                prior = json.loads(str(prior_events[-1]["payload_json"] or "{}"))
                latest_failed = self.connection.execute(
                    "SELECT id,node_id,attempt,error_json FROM node_runs WHERE run_id=? "
                    "AND status='failed' ORDER BY rowid DESC LIMIT 1",
                    (run_id,),
                ).fetchone()
                latest_error = (
                    json.loads(str(latest_failed["error_json"] or "{}")) if latest_failed else {}
                )
                if not (
                    allow_second_attempt
                    and len(prior_events) == 1
                    and prior.get("policy") == "v31-reader-facing-render-tail-v1"
                    and latest_failed is not None
                    and latest_failed["id"] == prior.get("queued_node_run_id")
                    and latest_failed["node_id"] == "render_evidence_footnotes"
                    and latest_failed["attempt"] == prior.get("attempt")
                    and latest_error.get("type") == "FukamiruError"
                    and str(latest_error.get("message") or "").startswith(
                        "V31 rendered manuscript has reader-facing scaffold: "
                    )
                ):
                    raise FukamiruError("Reader-facing render tail already used its recovery")
            footnote = self.connection.execute(
                "SELECT id,input_json,resource_lane FROM node_runs WHERE run_id=? "
                "AND node_id='render_evidence_footnotes' AND status='completed' "
                "ORDER BY attempt DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            if footnote is None:
                raise FukamiruError("Pre-render checkpoint is missing")
            payloads = PayloadStore(self.connection)
            failed_input = payloads.unpack(json.loads(str(failed["input_json"] or "{}")))
            render_input = payloads.unpack(json.loads(str(footnote["input_json"] or "{}")))
            if not isinstance(failed_input, dict) or not isinstance(render_input, dict):
                raise FukamiruError("Saved inputs are not replayable")
            sections = WorkerRunner._authoring_reader_facing_sections_from_html(
                str(failed_input.get("proposed_html") or "")
            )
            findings = WorkerRunner._authoring_reader_facing_v31_findings(sections)
            if not findings or any(
                item.get("code") != "bibliography_display_invalid" for item in findings
            ):
                raise FukamiruError("Final candidate has another reader-facing defect")
            repaired_entries = []
            for entry in render_input.get("reference_entries") or []:
                if not isinstance(entry, dict) or not (
                    entry.get("is_self_authored") is True
                    and entry.get("author") == "自著"
                    and entry.get("bibliography_people")
                ):
                    continue
                provisional = {**entry, "provisional_bibliography_review": True}
                reference = format_reference(provisional)
                footnote_display = format_footnote_reference(provisional)
                if (
                    reference.supported
                    and footnote_display.supported
                    and "自著" not in reference.text + footnote_display.text
                ):
                    repaired_entries.append(entry)
            if not repaired_entries:
                raise FukamiruError("No identified self-citation can be re-rendered")
            attempt = int(
                self.connection.execute(
                    "SELECT COALESCE(MAX(attempt),0)+1 FROM node_runs "
                    "WHERE run_id=? AND node_id='render_evidence_footnotes'",
                    (run_id,),
                ).fetchone()[0]
            )
            node_run_id = str(uuid.uuid4())
            self.connection.execute(
                "INSERT INTO node_runs(id,run_id,node_id,attempt,status,input_json,resource_lane) "
                "VALUES (?,?, 'render_evidence_footnotes',?, 'queued',?,?)",
                (
                    node_run_id,
                    run_id,
                    attempt,
                    str(footnote["input_json"]),
                    footnote["resource_lane"],
                ),
            )
            self.connection.execute(
                "UPDATE runs SET status='queued',error_json=NULL,completed_at=NULL,"
                "output_json=NULL WHERE id=? AND status='failed'",
                (run_id,),
            )
            payload = {
                "policy": (
                    "v31-reader-facing-render-tail-v2"
                    if prior_events
                    else "v31-reader-facing-render-tail-v1"
                ),
                "source_node_run_id": str(footnote["id"]),
                "failed_node_run_id": str(failed["id"]),
                "queued_node_run_id": node_run_id,
                "attempt": attempt,
                "identified_self_citation_count": len(repaired_entries),
                "recovery_ordinal": len(prior_events) + 1,
            }
            for event_type, object_type, object_id in (
                ("run.v31_reader_facing_render_tail_resumed", "run", run_id),
                ("node.queued", "node_run", node_run_id),
            ):
                self.connection.execute(
                    "INSERT INTO event_log(run_id,event_type,object_type,object_id,"
                    "payload_json,created_at) VALUES (?,?,?,?,?,?)",
                    (run_id, event_type, object_type, object_id, _json_dump(payload), now),
                )
            self.connection.commit()
            return {
                "run_id": run_id,
                "node_run_id": node_run_id,
                "node_id": "render_evidence_footnotes",
                "attempt": attempt,
                "status": "queued",
            }
        except Exception:
            self.connection.rollback()
            raise

    def restart_v31_verified_reference_tail(self, run_id: str) -> dict[str, Any]:
        """Requeue footnotes after a verified publisher repair, retaining prior work.

        This recovery is limited to an unapproved V31 Run whose only reference
        quality error is the publisher of the identified self-authored chapter.
        An operator must first stop the worker; no running claim is superseded.
        """

        now = utc_now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            run = self.connection.execute(
                "SELECT workflow_name,workflow_version,status FROM runs WHERE id=?",
                (run_id,),
            ).fetchone()
            if (
                run is None
                or not is_manuscript_generator_workflow(run["workflow_name"])
                or str(run["workflow_version"]) not in {"31", "32"}
                or run["status"] not in {"running", "queued", "failed"}
            ):
                raise FukamiruError("Run is not an eligible V31 reference-tail restart")
            active = self.connection.execute(
                "SELECT id,node_id,status FROM node_runs WHERE run_id=? "
                "AND status IN ('running','queued','waiting_human')",
                (run_id,),
            ).fetchall()
            if any(
                node["status"] != "queued"
                or node["node_id"]
                not in {
                    "validate_footnoted_references",
                    "validate_footnoted_structure",
                    "validate_footnoted_coherence",
                    "join_submission_validations",
                }
                for node in active
            ):
                raise FukamiruError("Reference-tail restart has an active claim")
            if self.connection.execute(
                "SELECT COUNT(*) FROM authoring_pending_proposals WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]:
                raise FukamiruError("Reference-tail restart cannot replace a proposal")
            if self.connection.execute(
                "SELECT COUNT(*) FROM event_log WHERE run_id=? AND "
                "event_type='run.v31_verified_reference_tail_restarted'",
                (run_id,),
            ).fetchone()[0]:
                raise FukamiruError("V31 reference tail was already restarted")

            quality = self.connection.execute(
                "SELECT output_json FROM node_runs WHERE run_id=? "
                "AND node_id='validate_reference_quality' AND status='completed' "
                "ORDER BY attempt DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            footnote = self.connection.execute(
                "SELECT id,attempt,input_json,resource_lane FROM node_runs "
                "WHERE run_id=? AND node_id='render_evidence_footnotes' "
                "AND status='completed' ORDER BY attempt DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            if quality is None or footnote is None:
                raise FukamiruError("Reference-tail restart lacks checkpoints")
            payloads = PayloadStore(self.connection)
            quality_context = payloads.unpack(json.loads(quality["output_json"] or "{}"))
            report = dict(quality_context.get("reference_quality_gate") or {})
            errors = [
                issue
                for issue in report.get("issues") or []
                if isinstance(issue, dict) and issue.get("severity") == "error"
            ]
            if (
                report.get("passed") is not False
                or len(errors) != 1
                or errors[0].get("code") != "reference.metadata_incomplete"
                or not str(errors[0].get("reference_id") or "").strip()
                or errors[0].get("fields") != ["publisher"]
            ):
                raise FukamiruError("Reference-tail restart has another quality error")
            input_context = payloads.unpack(json.loads(footnote["input_json"] or "{}"))
            if not isinstance(input_context, dict) or not any(
                isinstance(entry, dict)
                and entry.get("reference_id") == errors[0]["reference_id"]
                and entry.get("is_self_authored") is True
                and entry.get("record_type") == "chapter"
                and entry.get("publisher") is None
                for entry in input_context.get("reference_entries") or []
            ):
                raise FukamiruError("Reference-tail checkpoint does not match repair")

            input_context, publisher_corrections = reconcile_verified_publisher_metadata(
                input_context
            )
            if not any(
                item["reference_id"] == errors[0]["reference_id"] for item in publisher_corrections
            ):
                raise FukamiruError(
                    "Reference-tail restart requires identity-bound publisher evidence"
                )

            for key in (
                "reference_quality_gate",
                "reference_render_report",
                "rendered_structure_v26",
                "footnoted_coherence_report",
                "footnoted_block_verification_records",
                "submission_validation_gate",
                "v26_coherence_report",
                "v26_block_verification_records",
                "final_quality_sha256",
                "frozen_candidate_v26",
                "completion_gate",
                "v26_final_judgement",
                "human_revision_decision",
            ):
                input_context.pop(key, None)
            input_context["authoring_reference_tail_restart"] = {
                "policy": "verified-self-citation-publisher-tail-v1",
                "source_node_run_id": str(footnote["id"]),
                "reference_id": errors[0]["reference_id"],
                "publisher_corrections": publisher_corrections,
            }
            superseded = [str(node["id"]) for node in active]
            for node_id in superseded:
                self.connection.execute(
                    "UPDATE node_runs SET status='cancelled',completed_at=?,"
                    "error_json=? WHERE id=? AND status='queued'",
                    (
                        now,
                        _json_dump(
                            {
                                "type": "SupersededByReferenceTailRestart",
                                "message": (
                                    "Verified publisher metadata requires a new footnote tail"
                                ),
                            }
                        ),
                        node_id,
                    ),
                )
            attempt = int(
                self.connection.execute(
                    "SELECT COALESCE(MAX(attempt),0)+1 FROM node_runs "
                    "WHERE run_id=? AND node_id='render_evidence_footnotes'",
                    (run_id,),
                ).fetchone()[0]
            )
            node_run_id = str(uuid.uuid4())
            self.connection.execute(
                "INSERT INTO node_runs(id,run_id,node_id,attempt,status,input_json,"
                "resource_lane) VALUES (?,?, 'render_evidence_footnotes',?, 'queued',?,?)",
                (
                    node_run_id,
                    run_id,
                    attempt,
                    _json_dump(input_context),
                    footnote["resource_lane"],
                ),
            )
            self.connection.execute(
                "UPDATE runs SET status='queued',error_json=NULL,completed_at=NULL,"
                "output_json=NULL WHERE id=?",
                (run_id,),
            )
            payload = {
                "policy": "verified-self-citation-publisher-tail-v1",
                "source_node_run_id": str(footnote["id"]),
                "queued_node_run_id": node_run_id,
                "attempt": attempt,
                "superseded_queued_node_run_ids": superseded,
                "reference_id": errors[0]["reference_id"],
                "publisher_corrections": publisher_corrections,
            }
            self.connection.execute(
                "INSERT INTO event_log(run_id,event_type,object_type,object_id,"
                "payload_json,created_at) VALUES "
                "(?,'run.v31_verified_reference_tail_restarted','run',?,?,?)",
                (run_id, run_id, _json_dump(payload), now),
            )
            self.connection.execute(
                "INSERT INTO event_log(run_id,event_type,object_type,object_id,"
                "payload_json,created_at) VALUES "
                "(?,'node.queued','node_run',?,?,?)",
                (run_id, node_run_id, _json_dump(payload), now),
            )
            self.connection.commit()
            return {
                "run_id": run_id,
                "node_run_id": node_run_id,
                "node_id": "render_evidence_footnotes",
                "attempt": attempt,
                "status": "queued",
                "superseded_queued_node_count": len(superseded),
            }
        except Exception:
            self.connection.rollback()
            raise

    def recover_v31_verified_reference_join(self, run_id: str) -> dict[str, Any]:
        """Finish a validated fan-in after correcting one stale HTML snapshot.

        The four parent outputs are reused as saved. Their sole differing
        inherited reference HTML field is aligned to the identical rendered
        candidate already checked by the structure and coherence validators.
        """

        from fukamiru.worker.runner import WorkerRunner

        now = utc_now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            run = self.connection.execute(
                "SELECT workflow_name,workflow_version,status FROM runs WHERE id=?",
                (run_id,),
            ).fetchone()
            failed = self.connection.execute(
                "SELECT node_id,error_json FROM node_runs WHERE run_id=? "
                "AND status='failed' ORDER BY rowid DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            error = json.loads(str(failed["error_json"] or "{}")) if failed else {}
            if (
                run is None
                or not is_manuscript_generator_workflow(run["workflow_name"])
                or str(run["workflow_version"]) not in {"31", "32"}
                or run["status"] != "failed"
                or failed is None
                or failed["node_id"] != "validate_footnoted_coherence"
                or error.get("type") != "FukamiruError"
                or error.get("message")
                != (
                    "Join context conflict at "
                    "validate_footnoted_structure.reference_rendered_html_v26"
                )
                or self.connection.execute(
                    "SELECT COUNT(*) FROM node_runs WHERE run_id=? AND "
                    "status IN ('queued','running','waiting_human')",
                    (run_id,),
                ).fetchone()[0]
                or self.connection.execute(
                    "SELECT COUNT(*) FROM authoring_pending_proposals WHERE run_id=?",
                    (run_id,),
                ).fetchone()[0]
                or self.connection.execute(
                    "SELECT COUNT(*) FROM event_log WHERE run_id=? AND "
                    "event_type='run.v31_verified_reference_join_recovered'",
                    (run_id,),
                ).fetchone()[0]
            ):
                raise FukamiruError("Run is not an eligible V31 reference join")

            parent_ids = (
                "validate_reference_quality",
                "validate_footnoted_references",
                "validate_footnoted_structure",
                "validate_footnoted_coherence",
            )
            payloads = PayloadStore(self.connection)
            parents: dict[str, dict[str, Any]] = {}
            parent_node_run_ids: dict[str, str] = {}
            candidate_html: str | None = None
            generation: str | None = None
            for parent_id in parent_ids:
                required_status = (
                    "failed" if parent_id == "validate_footnoted_coherence" else "completed"
                )
                row = self.connection.execute(
                    "SELECT id,output_json FROM node_runs WHERE run_id=? AND "
                    "node_id=? AND status=? ORDER BY attempt DESC LIMIT 1",
                    (run_id, parent_id, required_status),
                ).fetchone()
                if row is None or not row["output_json"]:
                    raise FukamiruError("V31 reference join lacks a parent output")
                context = payloads.unpack(json.loads(row["output_json"]))
                if not isinstance(context, dict):
                    raise FukamiruError("V31 reference join parent is not an object")
                html = context.get("proposed_html")
                branch_generation = str(
                    (context.get("_join_generations") or {}).get("join_submission_validations")
                    or ""
                )
                if (
                    not isinstance(html, str)
                    or not html
                    or not branch_generation
                    or (candidate_html is not None and html != candidate_html)
                    or (generation is not None and branch_generation != generation)
                ):
                    raise FukamiruError("V31 reference join parents differ in candidate")
                candidate_html = html
                generation = branch_generation
                parents[parent_id] = context
                parent_node_run_ids[parent_id] = str(row["id"])
            assert candidate_html is not None and generation is not None
            candidate_sha = hashlib.sha256(candidate_html.encode()).hexdigest()
            quality = dict(parents[parent_ids[0]].get("reference_quality_gate") or {})
            structure = dict(parents[parent_ids[2]].get("rendered_structure_v26") or {})
            coherence = dict(parents[parent_ids[3]].get("footnoted_coherence_report") or {})
            if (
                quality.get("passed") is not True
                or quality.get("target_html_sha256") != candidate_sha
                or structure.get("valid") is not True
                or structure.get("html_sha256") != candidate_sha
                or coherence.get("html_sha256") != candidate_sha
                or not parents[parent_ids[3]].get("footnoted_block_verification_records")
                or dict(parents[parent_ids[1]].get("reference_quality_gate") or {}).get("passed")
                is not True
            ):
                raise FukamiruError("V31 reference join validation is incomplete")
            old_values = {
                str(context.get("reference_rendered_html_v26") or "")
                for context in parents.values()
            }
            if len(old_values) != 2 or candidate_html not in old_values or "" in old_values:
                raise FukamiruError("V31 reference join has another HTML mismatch")
            for context in parents.values():
                context["reference_rendered_html_v26"] = candidate_html

            merged: dict[str, Any] = {}
            for parent_id in parent_ids:
                merged = WorkerRunner._merge_join_contexts(
                    merged, parents[parent_id], path=parent_id
                )
            existing = self.connection.execute(
                "SELECT input_json FROM node_runs WHERE run_id=? AND "
                "node_id='join_submission_validations'",
                (run_id,),
            ).fetchall()
            if any(
                str(
                    (
                        payloads.unpack(json.loads(row["input_json"] or "{}")).get(
                            "_join_generations"
                        )
                        or {}
                    ).get("join_submission_validations")
                    or ""
                )
                == generation
                for row in existing
            ):
                raise FukamiruError("V31 reference join generation already exists")
            attempt = int(
                self.connection.execute(
                    "SELECT COALESCE(MAX(attempt),0)+1 FROM node_runs "
                    "WHERE run_id=? AND node_id='join_submission_validations'",
                    (run_id,),
                ).fetchone()[0]
            )
            resource = self.connection.execute(
                "SELECT resource_lane FROM node_runs WHERE run_id=? AND "
                "node_id='join_submission_validations' ORDER BY attempt DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            node_run_id = str(uuid.uuid4())
            self.connection.execute(
                "INSERT INTO node_runs(id,run_id,node_id,attempt,status,input_json,"
                "resource_lane) VALUES "
                "(?,?, 'join_submission_validations',?, 'queued',?,?)",
                (
                    node_run_id,
                    run_id,
                    attempt,
                    _json_dump(merged),
                    str(resource["resource_lane"]) if resource else "ingestion_cpu",
                ),
            )
            self.connection.execute(
                "UPDATE runs SET status='queued',error_json=NULL,completed_at=NULL,"
                "output_json=NULL WHERE id=? AND status='failed'",
                (run_id,),
            )
            event = {
                "policy": "verified-reference-html-snapshot-join-v1",
                "candidate_sha256": candidate_sha,
                "join_generation": generation,
                "parent_node_run_ids": parent_node_run_ids,
                "queued_node_run_id": node_run_id,
                "corrected_field": "reference_rendered_html_v26",
            }
            self.connection.execute(
                "INSERT INTO event_log(run_id,event_type,object_type,object_id,"
                "payload_json,created_at) VALUES "
                "(?,'run.v31_verified_reference_join_recovered','run',?,?,?)",
                (run_id, run_id, _json_dump(event), now),
            )
            self.connection.execute(
                "INSERT INTO event_log(run_id,event_type,object_type,object_id,"
                "payload_json,created_at) VALUES "
                "(?,'node.queued','node_run',?,?,?)",
                (run_id, node_run_id, _json_dump(event), now),
            )
            self.connection.commit()
            return {
                "run_id": run_id,
                "node_run_id": node_run_id,
                "node_id": "join_submission_validations",
                "attempt": attempt,
                "status": "queued",
                "candidate_sha256": candidate_sha,
            }
        except Exception:
            self.connection.rollback()
            raise

    def recover_v31_stale_footnoted_submission_join(self, run_id: str) -> dict[str, Any]:
        """Queue the saved four-way validation after discarding inherited old audits.

        Only the exact failed coherence fan-in is recoverable. The validators'
        outputs remain untouched; no LLM audit or citation check is bypassed.
        """

        from fukamiru.worker.runner import WorkerRunner

        now = utc_now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            run = self.connection.execute(
                "SELECT workflow_name,workflow_version,status FROM runs WHERE id=?",
                (run_id,),
            ).fetchone()
            failed = self.connection.execute(
                "SELECT node_id,error_json FROM node_runs WHERE run_id=? "
                "AND status='failed' ORDER BY rowid DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            error = json.loads(str(failed["error_json"] or "{}")) if failed else {}
            if (
                run is None
                or not is_manuscript_generator_workflow(run["workflow_name"])
                or str(run["workflow_version"]) not in {"31", "32"}
                or run["status"] != "failed"
                or failed is None
                or failed["node_id"] != "validate_footnoted_coherence"
                or error.get("message")
                != (
                    "Join context conflict at "
                    "validate_footnoted_coherence.footnoted_conditional_success.candidate_sha256"
                )
                or self.connection.execute(
                    "SELECT COUNT(*) FROM node_runs WHERE run_id=? AND "
                    "status IN ('queued','running','waiting_human')",
                    (run_id,),
                ).fetchone()[0]
                or self.connection.execute(
                    "SELECT COUNT(*) FROM authoring_pending_proposals WHERE run_id=?",
                    (run_id,),
                ).fetchone()[0]
                or self.connection.execute(
                    "SELECT COUNT(*) FROM event_log WHERE run_id=? AND "
                    "event_type='run.v31_stale_footnoted_join_recovered'",
                    (run_id,),
                ).fetchone()[0]
            ):
                raise FukamiruError("Run is not an eligible V31 footnoted join")

            parent_ids = (
                "validate_reference_quality",
                "validate_footnoted_references",
                "validate_footnoted_structure",
                "validate_footnoted_coherence",
            )
            payloads = PayloadStore(self.connection)
            parents: dict[str, dict[str, Any]] = {}
            node_run_ids: dict[str, str] = {}
            generation: str | None = None
            for parent_id in parent_ids:
                status = "failed" if parent_id == "validate_footnoted_coherence" else "completed"
                row = self.connection.execute(
                    "SELECT id,output_json FROM node_runs WHERE run_id=? AND "
                    "node_id=? AND status=? ORDER BY attempt DESC LIMIT 1",
                    (run_id, parent_id, status),
                ).fetchone()
                if row is None or not row["output_json"]:
                    raise FukamiruError("V31 footnoted join lacks a saved validator")
                context = payloads.unpack(json.loads(row["output_json"]))
                if not isinstance(context, dict):
                    raise FukamiruError("V31 footnoted validator output is invalid")
                branch_generation = str(
                    (context.get("_join_generations") or {}).get("join_submission_validations")
                    or ""
                )
                if not branch_generation or (
                    generation is not None and branch_generation != generation
                ):
                    raise FukamiruError("V31 footnoted validators differ in generation")
                generation = branch_generation
                parents[parent_id] = context
                node_run_ids[parent_id] = str(row["id"])
            merged = WorkerRunner._merge_v31_submission_validation_contexts(parents)
            candidate_sha = hashlib.sha256(str(merged["proposed_html"]).encode()).hexdigest()
            old_shas = {
                str(
                    (parents[parent_id].get("footnoted_conditional_success") or {}).get(
                        "candidate_sha256"
                    )
                    or ""
                )
                for parent_id in parent_ids[:3]
            }
            if len(old_shas) != 1 or not next(iter(old_shas)) or candidate_sha in old_shas:
                raise FukamiruError("V31 footnoted join is not the stale-audit case")
            if any(
                str(
                    (
                        payloads.unpack(json.loads(row["input_json"] or "{}")).get(
                            "_join_generations"
                        )
                        or {}
                    ).get("join_submission_validations")
                    or ""
                )
                == generation
                for row in self.connection.execute(
                    "SELECT input_json FROM node_runs WHERE run_id=? "
                    "AND node_id='join_submission_validations'",
                    (run_id,),
                ).fetchall()
            ):
                raise FukamiruError("V31 submission join generation already exists")
            attempt = int(
                self.connection.execute(
                    "SELECT COALESCE(MAX(attempt),0)+1 FROM node_runs "
                    "WHERE run_id=? AND node_id='join_submission_validations'",
                    (run_id,),
                ).fetchone()[0]
            )
            node_run_id = str(uuid.uuid4())
            self.connection.execute(
                "INSERT INTO node_runs(id,run_id,node_id,attempt,status,input_json,resource_lane) "
                "VALUES (?,?,'join_submission_validations',?,'queued',?,?)",
                (node_run_id, run_id, attempt, _json_dump(payloads.pack(merged)), "ingestion_cpu"),
            )
            self.connection.execute(
                "UPDATE runs SET status='queued',error_json=NULL,completed_at=NULL,"
                "output_json=NULL WHERE id=? AND status='failed'",
                (run_id,),
            )
            event = {
                "policy": "current-candidate-footnoted-audit-join-v1",
                "candidate_sha256": candidate_sha,
                "stale_candidate_sha256": next(iter(old_shas)),
                "join_generation": generation,
                "parent_node_run_ids": node_run_ids,
                "queued_node_run_id": node_run_id,
            }
            for event_type, object_type, object_id in (
                ("run.v31_stale_footnoted_join_recovered", "run", run_id),
                ("node.queued", "node_run", node_run_id),
            ):
                self.connection.execute(
                    "INSERT INTO event_log(run_id,event_type,object_type,object_id,"
                    "payload_json,created_at) VALUES (?,?,?,?,?,?)",
                    (run_id, event_type, object_type, object_id, _json_dump(event), now),
                )
            self.connection.commit()
            return {
                "run_id": run_id,
                "node_run_id": node_run_id,
                "node_id": "join_submission_validations",
                "attempt": attempt,
                "status": "queued",
                "candidate_sha256": candidate_sha,
            }
        except Exception:
            self.connection.rollback()
            raise

    def replay_failed_v29_collocation_scoring(self, run_id: str) -> dict[str, Any]:
        """Replay only an isolated failed V29 Base-scoring node.

        Frames, candidates, probes, and any score checkpoints remain attached to
        the same Run.  Completed upstream nodes and the original node attempt are
        not recreated or modified.
        """

        now = utc_now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            run = self.connection.execute(
                "SELECT workflow_name,workflow_version,status,error_json,completed_at "
                "FROM runs WHERE id=?",
                (run_id,),
            ).fetchone()
            nodes = self.connection.execute(
                "SELECT id,node_id,attempt,status,input_json,error_json,completed_at "
                "FROM node_runs WHERE run_id=? ORDER BY rowid",
                (run_id,),
            ).fetchall()
            failed = [node for node in nodes if node["status"] == "failed"]
            if (
                run is None
                or run["workflow_name"] != "manuscript_rewrite"
                or run["workflow_version"] != "29"
                or run["status"] != "failed"
                or len(failed) != 1
                or failed[0]["node_id"] != "score_with_llmjp_base"
                or any(node["status"] not in {"completed", "failed"} for node in nodes)
                or not any(
                    node["node_id"] == "generate_collocation_probes"
                    and node["status"] == "completed"
                    for node in nodes
                )
            ):
                raise FukamiruError("Run is not an isolated failed V29 scoring replay")
            node = failed[0]
            error = json.loads(str(node["error_json"] or "{}"))
            unpacked = PayloadStore(self.connection).unpack(
                json.loads(str(node["input_json"] or "{}"))
            )
            if not str(error.get("message") or "").startswith(
                "continuation_score."
            ) or not isinstance(unpacked.get("proposed_html"), str):
                raise FukamiruError("Failed V29 scoring node has no replayable input")
            counts = {
                "frame_count": int(
                    self.connection.execute(
                        "SELECT COUNT(*) FROM manuscript_collocation_frames WHERE run_id=?",
                        (run_id,),
                    ).fetchone()[0]
                ),
                "candidate_count": int(
                    self.connection.execute(
                        "SELECT COUNT(*) FROM manuscript_collocation_candidates WHERE run_id=?",
                        (run_id,),
                    ).fetchone()[0]
                ),
                "probe_count": int(
                    self.connection.execute(
                        "SELECT COUNT(*) FROM manuscript_collocation_probes WHERE run_id=?",
                        (run_id,),
                    ).fetchone()[0]
                ),
                "score_checkpoint_count": int(
                    self.connection.execute(
                        "SELECT COUNT(*) FROM manuscript_collocation_scores WHERE run_id=?",
                        (run_id,),
                    ).fetchone()[0]
                ),
            }
            required_assets = ("frame_count", "candidate_count", "probe_count")
            if not all(counts[key] > 0 for key in required_assets):
                raise FukamiruError("Failed V29 scoring node has no reusable assets")
            self.connection.execute(
                "INSERT INTO event_log(run_id,event_type,object_type,object_id,"
                "payload_json,created_at) "
                "VALUES (?,'run.failed_node_replayed','node_run',?,?,?)",
                (
                    run_id,
                    node["id"],
                    _json_dump(
                        {
                            "node_id": node["node_id"],
                            "attempt": node["attempt"],
                            "previous_run_error": json.loads(str(run["error_json"] or "{}")),
                            "previous_node_error": error,
                            "previous_run_completed_at": run["completed_at"],
                            "previous_node_completed_at": node["completed_at"],
                            "workflow_version": "29",
                            "policy": "collocation-score-checkpoint-replay-v29.1",
                            **counts,
                        }
                    ),
                    now,
                ),
            )
            self.connection.execute(
                "UPDATE node_runs SET status='queued', started_at=NULL, completed_at=NULL, "
                "error_json=NULL, output_json=NULL, lease_owner=NULL, lease_acquired_at=NULL, "
                "lease_heartbeat_at=NULL, lease_expires_at=NULL "
                "WHERE id=? AND status='failed'",
                (node["id"],),
            )
            self.connection.execute(
                "UPDATE runs SET status='queued', error_json=NULL, completed_at=NULL, "
                "output_json=NULL WHERE id=? AND status='failed'",
                (run_id,),
            )
            self.connection.commit()
            return {
                "run_id": run_id,
                "node_run_id": str(node["id"]),
                "node_id": str(node["node_id"]),
                "attempt": int(node["attempt"]),
                "status": "queued",
                **counts,
            }
        except Exception:
            self.connection.rollback()
            raise

    def replay_failed_corpus_index(self, run_id: str, batch_id: str) -> dict[str, Any]:
        """Explicitly replay only a failed Corpus index, preserving failed attempts."""
        now = utc_now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            run = self.connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            nodes = self.connection.execute(
                "SELECT * FROM node_runs WHERE run_id=? ORDER BY rowid", (run_id,)
            ).fetchall()
            node = nodes[-1] if nodes else None
            batch = self.connection.execute(
                "SELECT run_id,status FROM corpus_sync_batches WHERE id=?", (batch_id,)
            ).fetchone()
            if (
                run is None
                or run["workflow_name"] != "corpus_sync"
                or run["workflow_version"] != "2"
                or run["status"] != "failed"
                or batch is None
                or batch["run_id"] != run_id
                or batch["status"] != "failed"
                or node is None
                or node["node_id"] != "build_semantic_index"
                or node["status"] != "failed"
                or any(item["status"] not in {"completed", "failed"} for item in nodes)
                or not any(
                    item["node_id"] == "ingest_ocr_outputs" and item["status"] == "completed"
                    for item in nodes
                )
            ):
                raise FukamiruError("Run is not an inactive failed Corpus index")
            context = PayloadStore(self.connection).unpack(
                json.loads(str(node["input_json"] or "{}"))
            )
            if context.get("corpus_batch_id") != batch_id:
                raise FukamiruError("Corpus index input and batch do not match")
            node_run_id = str(uuid.uuid4())
            attempt = (
                max(
                    int(item["attempt"])
                    for item in nodes
                    if item["node_id"] == "build_semantic_index"
                )
                + 1
            )
            self.connection.execute(
                """INSERT INTO node_runs(
                id,run_id,node_id,attempt,status,input_json,resource_lane
                ) VALUES (?,?,'build_semantic_index',?,'queued',?,'embedding_gpu')""",
                (node_run_id, run_id, attempt, node["input_json"]),
            )
            self.connection.execute(
                "UPDATE runs SET status='queued',error_json=NULL,completed_at=NULL,"
                "output_json=NULL WHERE id=?",
                (run_id,),
            )
            self.connection.execute(
                "UPDATE corpus_sync_batches SET status='indexing',updated_at=? WHERE id=?",
                (now, batch_id),
            )
            events = EventRepository(self.connection)
            events.append(
                run_id,
                "corpus.index.replay_requested",
                object_type="node_run",
                object_id=node_run_id,
                payload={
                    "node_id": "build_semantic_index",
                    "attempt": attempt,
                    "previous_node_run_id": str(node["id"]),
                    "previous_run_error": json.loads(str(run["error_json"] or "{}")),
                    "previous_completed_at": run["completed_at"],
                    "batch_id": batch_id,
                    "policy": "token-bounded-source-ranges-v2",
                },
            )
            events.append(
                run_id,
                "node.queued",
                object_type="node_run",
                object_id=node_run_id,
                payload={
                    "node_id": "build_semantic_index",
                    "attempt": attempt,
                    "resource_lane": "embedding_gpu",
                },
            )
            self.connection.commit()
            return {
                "run_id": run_id,
                "node_run_id": node_run_id,
                "node_id": "build_semantic_index",
                "attempt": attempt,
                "status": "queued",
            }
        except Exception:
            self.connection.rollback()
            raise

    def resume_interrupted_corpus_ingestion(
        self, run_id: str, batch_id: str, *, expected_node_run_id: str,
    ) -> dict[str, Any]:
        """Resume an explicitly repaired, stopped V2 batch in a new attempt.

        The caller must stop workers first. Keep the old input, timestamps and
        lease as history; reject live leases and any ambiguous active scope.
        """
        now = utc_now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            run = self.connection.execute(
                "SELECT * FROM runs WHERE id=?", (run_id,),
            ).fetchone()
            batch = self.connection.execute(
                "SELECT * FROM corpus_sync_batches WHERE id=?", (batch_id,),
            ).fetchone()
            active = self.connection.execute(
                "SELECT * FROM node_runs WHERE run_id=? "
                "AND status IN ('queued','running','waiting_human')", (run_id,),
            ).fetchall()
            if (run is None or run["workflow_name"] != "corpus_sync"
                    or run["workflow_version"] != "2" or run["status"] != "running"
                    or batch is None or batch["run_id"] != run_id
                    or batch["status"] != "ingesting" or len(active) != 1):
                raise FukamiruError("Run is not an interrupted Corpus ingestion")
            node = active[0]
            if (node["id"] != expected_node_run_id or node["node_id"] != "ingest_ready_sources"
                    or node["status"] != "running" or not node["lease_expires_at"]
                    or node["lease_expires_at"] > now):
                raise FukamiruError("Expected stopped Corpus ingestion lease does not match")
            counts = {row["status"]: row["n"] for row in self.connection.execute(
                "SELECT status,COUNT(*) n FROM corpus_sync_items "
                "WHERE batch_id=? AND skip_reason IS NULL GROUP BY status", (batch_id,),
            )}
            if (set(counts) - {"discovered", "ready", "reused"} or not counts
                    or sum(counts.values()) != batch["discovered_count"]):
                raise FukamiruError("Effective Corpus scope has not been repaired")
            pending = self.connection.execute(
                "SELECT 1 FROM pdf_ocr_jobs j JOIN corpus_sync_items i ON i.id=j.corpus_item_id "
                "WHERE j.run_id=? AND i.skip_reason IS NOT NULL "
                "AND j.status IN ('queued','processing','needs_language') LIMIT 1", (run_id,),
            ).fetchone()
            if pending:
                raise FukamiruError("Excluded Corpus sources still have pending OCR jobs")
            inputs = PayloadStore(self.connection).unpack(json.loads(node["input_json"]))
            if inputs.get("corpus_batch_id") != batch_id:
                raise FukamiruError("Corpus node input and batch differ")
            excluded = self.connection.execute(
                "SELECT COUNT(*) FROM corpus_sync_items WHERE batch_id=? "
                "AND skip_reason IS NOT NULL", (batch_id,),
            ).fetchone()[0]
            inputs.update(discovered_count=batch["discovered_count"], existing_count=excluded,
                          scanned_count=batch["discovered_count"] + excluded,
                          ready_count=counts.get("ready", 0), reused_count=counts.get("reused", 0),
                          failed_count=0, ocr_required_count=0)
            attempt = self.connection.execute(
                "SELECT MAX(attempt)+1 FROM node_runs WHERE run_id=? "
                "AND node_id='ingest_ready_sources'", (run_id,),
            ).fetchone()[0]
            replacement = str(uuid.uuid4())
            self.connection.execute(
                "UPDATE node_runs SET status='cancelled',completed_at=?,error_json=? "
                "WHERE id=? AND status='running'",
                (now, _json_dump({"type": "InterruptedCorpusIngestion",
                                  "message": "Superseded after explicit batch repair",
                                  "replacement_node_run_id": replacement}), node["id"]),
            )
            self.connection.execute(
                "INSERT INTO node_runs(id,run_id,node_id,attempt,status,input_json,resource_lane) "
                "VALUES (?,?,'ingest_ready_sources',?,'queued',?,?)",
                (replacement, run_id, attempt,
                 _json_dump(PayloadStore(self.connection).pack(inputs)), node["resource_lane"]),
            )
            self.connection.execute("UPDATE runs SET status='queued' WHERE id=?", (run_id,))
            for event_type, object_id, payload in [
                ("corpus.ingestion.interrupted_attempt_superseded", node["id"], {
                    "replacement_node_run_id": replacement, "batch_id": batch_id,
                    "previous_status": node["status"],
                }),
                ("node.queued", replacement, {
                    "node_id": "ingest_ready_sources", "attempt": attempt,
                    "previous_node_run_id": node["id"], "resource_lane": node["resource_lane"],
                    "discovered_count": batch["discovered_count"], "existing_count": excluded,
                }),
            ]:
                self.connection.execute(
                    "INSERT INTO event_log(run_id,event_type,object_type,object_id,"
                    "payload_json,created_at) VALUES (?,?,'node_run',?,?,?)",
                    (run_id, event_type, object_id, _json_dump(payload), now),
                )
            self.connection.commit()
            return {"run_id": run_id, "node_run_id": replacement,
                    "node_id": "ingest_ready_sources", "attempt": attempt, "status": "queued"}
        except Exception:
            self.connection.rollback()
            raise

    def resume_corpus_index_guard(
        self,
        run_id: str,
        batch_id: str,
        *,
        expected_guard_reason: str,
    ) -> dict[str, Any]:
        """Start a new index attempt after an explicit operator guard repair.

        Retain the waiting attempt and its output as a superseded, cancelled
        attempt. Only a fully prepared effective Corpus scope may be resumed.
        """
        now = utc_now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            run = self.connection.execute(
                "SELECT * FROM runs WHERE id=?",
                (run_id,),
            ).fetchone()
            batch = self.connection.execute(
                "SELECT * FROM corpus_sync_batches WHERE id=?",
                (batch_id,),
            ).fetchone()
            active = self.connection.execute(
                "SELECT * FROM node_runs WHERE run_id=? "
                "AND status IN ('queued','running','waiting_human') ORDER BY rowid",
                (run_id,),
            ).fetchall()
            if (
                run is None
                or run["workflow_name"] != "corpus_sync"
                or run["workflow_version"] != "2"
                or run["status"] != "waiting_human"
                or batch is None
                or batch["run_id"] != run_id
                or batch["status"] != "indexing"
                or len(active) != 1
            ):
                raise FukamiruError("Run is not an inactive Corpus index guard wait")
            node = active[0]
            context = PayloadStore(self.connection).unpack(json.loads(node["output_json"] or "{}"))
            guard = context.get("semantic_index", {})
            if (
                node["node_id"] != "build_semantic_index"
                or node["status"] != "waiting_human"
                or guard.get("status") != "awaiting_index_guard"
                or not expected_guard_reason
                or guard.get("reason") != expected_guard_reason
            ):
                raise FukamiruError("The expected Corpus index guard does not match")
            counts = {
                row["status"]: row["n"]
                for row in self.connection.execute(
                    "SELECT status,COUNT(*) n FROM corpus_sync_items WHERE batch_id=? "
                    "AND skip_reason IS NULL GROUP BY status",
                    (batch_id,),
                )
            }
            if (
                set(counts) - {"ready", "reused"}
                or not counts
                or sum(counts.values()) != batch["discovered_count"]
            ):
                raise FukamiruError("Effective Corpus scope is not fully prepared")
            pending = self.connection.execute(
                "SELECT COUNT(*) FROM pdf_ocr_jobs WHERE run_id=? "
                "AND status IN ('queued','processing','needs_language')",
                (run_id,),
            ).fetchone()[0]
            if pending:
                raise FukamiruError("OCR jobs are still pending")
            inputs = PayloadStore(self.connection).unpack(json.loads(node["input_json"] or "{}"))
            if inputs.get("corpus_batch_id") != batch_id:
                raise FukamiruError("Corpus node input and batch differ")
            inputs.update(
                ready_count=counts.get("ready", 0),
                reused_count=counts.get("reused", 0),
                failed_count=0,
                ocr_required_count=0,
                discovered_count=batch["discovered_count"],
            )
            encoded = _json_dump(PayloadStore(self.connection).pack(inputs))
            attempt = self.connection.execute(
                "SELECT MAX(attempt)+1 FROM node_runs WHERE run_id=? AND node_id=?",
                (run_id, "build_semantic_index"),
            ).fetchone()[0]
            node_id = str(uuid.uuid4())
            self.connection.execute(
                "UPDATE node_runs SET status='cancelled',completed_at=?,lease_heartbeat_at=NULL,"
                "error_json=? WHERE id=? AND status='waiting_human'",
                (
                    now,
                    _json_dump({"type": "IndexGuardSuperseded", "message": expected_guard_reason}),
                    node["id"],
                ),
            )
            self.connection.execute(
                "INSERT INTO node_runs(id,run_id,node_id,attempt,status,input_json,resource_lane) "
                "VALUES (?,?,'build_semantic_index',?,'queued',?,'embedding_gpu')",
                (node_id, run_id, attempt, encoded),
            )
            self.connection.execute(
                "UPDATE runs SET status='queued',error_json=NULL,completed_at=NULL,"
                "output_json=NULL WHERE id=?",
                (run_id,),
            )
            self.connection.execute(
                "UPDATE corpus_sync_batches SET updated_at=? WHERE id=?",
                (now, batch_id),
            )
            for event_type, object_id, payload in [
                (
                    "corpus.index.guard_superseded",
                    node["id"],
                    {
                        "reason": expected_guard_reason,
                        "replacement_node_run_id": node_id,
                    },
                ),
                (
                    "corpus.index.replay_requested",
                    node_id,
                    {
                        "node_id": "build_semantic_index",
                        "attempt": attempt,
                        "previous_node_run_id": node["id"],
                        "batch_id": batch_id,
                        "policy": "explicit-ocr-closeout-control-only-span-repair",
                    },
                ),
                (
                    "node.queued",
                    node_id,
                    {
                        "node_id": "build_semantic_index",
                        "attempt": attempt,
                        "resource_lane": "embedding_gpu",
                    },
                ),
            ]:
                self.connection.execute(
                    "INSERT INTO event_log(run_id,event_type,object_type,object_id,"
                    "payload_json,created_at) VALUES (?,?,'node_run',?,?,?)",
                    (run_id, event_type, object_id, _json_dump(payload), now),
                )
            self.connection.commit()
            return {
                "run_id": run_id,
                "node_run_id": node_id,
                "node_id": "build_semantic_index",
                "attempt": attempt,
                "status": "queued",
            }
        except Exception:
            self.connection.rollback()
            raise

    def replay_failed_translation_post_edit(self, run_id: str) -> dict[str, Any]:
        """Replay the terminal llm-jp node after a bounded prompt-policy fix.

        Earlier failed attempts and all completed upstream nodes remain auditable. The
        latest failed node keeps its identity and input so durable chunk checkpoints can
        be reused instead of retranslating the document.
        """

        now = utc_now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            run = self.connection.execute(
                "SELECT workflow_name,workflow_version,status,error_json,completed_at "
                "FROM runs WHERE id=?",
                (run_id,),
            ).fetchone()
            nodes = self.connection.execute(
                "SELECT id,node_id,attempt,status,input_json,error_json,completed_at "
                "FROM node_runs WHERE run_id=? ORDER BY rowid",
                (run_id,),
            ).fetchall()
            node = nodes[-1] if nodes else None
            if (
                run is None
                or run["workflow_name"] != "translation"
                or run["workflow_version"] != "7"
                or run["status"] != "failed"
                or node is None
                or node["node_id"] != "llm_jp_post_edit"
                or node["status"] != "failed"
                or any(item["status"] not in {"completed", "failed"} for item in nodes)
            ):
                raise FukamiruError("Run is not a terminal failed V7 llm-jp replay")
            error = json.loads(str(node["error_json"] or "{}"))
            unpacked = PayloadStore(self.connection).unpack(
                json.loads(str(node["input_json"] or "{}"))
            )
            if (
                not error.get("type")
                or not str(unpacked.get("translation_version_id") or "").strip()
            ):
                raise FukamiruError("Failed llm-jp node has no replayable input")
            self.connection.execute(
                "INSERT INTO event_log(run_id,event_type,object_type,object_id,"
                "payload_json,created_at) "
                "VALUES (?,'run.failed_node_replayed','node_run',?,?,?)",
                (
                    run_id,
                    node["id"],
                    _json_dump(
                        {
                            "node_id": node["node_id"],
                            "attempt": node["attempt"],
                            "previous_run_error": json.loads(str(run["error_json"] or "{}")),
                            "previous_node_error": error,
                            "previous_run_completed_at": run["completed_at"],
                            "previous_node_completed_at": node["completed_at"],
                            "workflow_version": "7",
                            "policy": "llm-jp-bounded-repair-feedback-v1",
                        }
                    ),
                    now,
                ),
            )
            self.connection.execute(
                "UPDATE node_runs SET status='queued', started_at=NULL, completed_at=NULL, "
                "error_json=NULL, output_json=NULL, lease_owner=NULL, lease_acquired_at=NULL, "
                "lease_heartbeat_at=NULL, lease_expires_at=NULL "
                "WHERE id=? AND status='failed'",
                (node["id"],),
            )
            self.connection.execute(
                "UPDATE runs SET status='queued', error_json=NULL, completed_at=NULL, "
                "output_json=NULL WHERE id=? AND status='failed'",
                (run_id,),
            )
            self.connection.commit()
            return {
                "run_id": run_id,
                "node_run_id": str(node["id"]),
                "node_id": str(node["node_id"]),
                "attempt": int(node["attempt"]),
                "status": "queued",
            }
        except Exception:
            self.connection.rollback()
            raise

    def resume_external_wait(
        self,
        run_id: str,
        *,
        authorized_engine_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        """Explicitly requeue one V22 manuscript node after an external-service wait.

        External-service retries are deliberately bounded by the worker.  Once that
        budget is exhausted, only this operator action (or cancellation) may move the
        Run out of ``waiting_human``.  The original node attempt and any durable
        checkpoints are retained; only the transport retry budget is reset.
        """
        if authorized_engine_fingerprint is not None and not _is_sha256(
            authorized_engine_fingerprint
        ):
            raise FukamiruError(
                "authorized_engine_fingerprint must be an exact lowercase SHA-256 fingerprint"
            )
        now = utc_now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            run = self.connection.execute(
                """
                SELECT workflow_name, workflow_version, status, error_json
                FROM runs WHERE id = ?
                """,
                (run_id,),
            ).fetchone()
            if run is None:
                raise FukamiruError(f"Unknown run: {run_id}")
            try:
                workflow_version = int(str(run["workflow_version"]))
            except ValueError as exc:
                raise FukamiruError(f"Run {run_id} has an invalid workflow version") from exc
            workflow_name = str(run["workflow_name"])
            eligible_workflow = (
                workflow_name == "manuscript_rewrite" and workflow_version >= 22
            ) or (is_manuscript_generator_workflow(workflow_name) and workflow_version >= 31)
            if not eligible_workflow:
                raise FukamiruError(
                    "Manual external-service resume is available only for "
                    "manuscript_rewrite V22+ or manuscript_generator V31+"
                )
            if str(run["status"]) != "waiting_human":
                raise FukamiruError(
                    f"Run {run_id} is not waiting for manual external-service resume"
                )
            run_error = json.loads(str(run["error_json"] or "{}"))
            if run_error.get("type") != "ExternalServiceUnavailable":
                raise FukamiruError(
                    f"Run {run_id} is waiting for a reason other than an external-service outage"
                )
            engine_authorization_wait = (
                run_error.get("reason") == "inference_engine_authorization_required"
            )
            authorization_id: str | None = None
            previous_fingerprint: str | None = None
            target_fingerprint: str | None = None
            if engine_authorization_wait:
                previous_fingerprint = str(run_error.get("previous_engine_fingerprint") or "")
                target_fingerprint = str(run_error.get("target_engine_fingerprint") or "")
                if not _is_sha256(previous_fingerprint) or not _is_sha256(target_fingerprint):
                    raise FukamiruError(
                        f"Run {run_id} has an invalid engine-authorization wait record"
                    )
                if authorized_engine_fingerprint != target_fingerprint:
                    raise FukamiruError(
                        "authorized_engine_fingerprint must exactly match the pending "
                        "V22 inference-engine fingerprint"
                    )
            elif authorized_engine_fingerprint is not None:
                raise FukamiruError(
                    "authorized_engine_fingerprint is accepted only for an explicit "
                    "V22 inference-engine authorization wait"
                )

            active_nodes = self.connection.execute(
                """
                SELECT id, node_id, status, input_json, error_json
                FROM node_runs
                WHERE run_id = ? AND status IN ('queued', 'running', 'waiting_human')
                ORDER BY rowid
                """,
                (run_id,),
            ).fetchall()
            resumable = []
            for node in active_nodes:
                node_error = json.loads(str(node["error_json"] or "{}"))
                if (
                    str(node["status"]) == "waiting_human"
                    and node_error.get("type") == "ExternalServiceUnavailable"
                ):
                    resumable.append(node)
            if len(active_nodes) != 1 or len(resumable) != 1:
                raise FukamiruError(
                    f"Run {run_id} does not have exactly one externally blocked active node"
                )
            open_reviews = int(
                self.connection.execute(
                    """
                    SELECT COUNT(*) FROM human_reviews
                    WHERE run_id = ? AND status = 'open'
                    """,
                    (run_id,),
                ).fetchone()[0]
            )
            if open_reviews:
                raise FukamiruError(
                    f"Run {run_id} also has an unresolved human review and cannot be resumed here"
                )

            node = resumable[0]
            node_error = json.loads(str(node["error_json"] or "{}"))
            if engine_authorization_wait and (
                node_error.get("reason") != "inference_engine_authorization_required"
                or node_error.get("previous_engine_fingerprint") != previous_fingerprint
                or node_error.get("target_engine_fingerprint") != target_fingerprint
            ):
                raise FukamiruError(
                    f"Run {run_id} has inconsistent engine-authorization wait state"
                )
            packed_inputs = json.loads(str(node["input_json"] or "{}"))
            if not isinstance(packed_inputs, dict):
                raise FukamiruError(f"Run {run_id} has an invalid node input payload")
            for key in _ENGINE_AUTHORIZATION_CONTEXT_KEYS:
                packed_inputs.pop(key, None)
            previous_retry = packed_inputs.pop("_external_retry", {})
            previous_retry_count = (
                int(previous_retry.get("count") or 0) if isinstance(previous_retry, dict) else 0
            )
            if engine_authorization_wait:
                authorization_id = str(uuid.uuid4())
                self.connection.execute(
                    "UPDATE manuscript_engine_authorizations "
                    "SET status='superseded',superseded_at=? "
                    "WHERE run_id=? AND node_run_id=? AND status='pending'",
                    (now, run_id, str(node["id"])),
                )
                self.connection.execute(
                    "INSERT INTO manuscript_engine_authorizations "
                    "(id,run_id,node_run_id,from_fingerprint,target_fingerprint,status,created_at) "
                    "VALUES (?,?,?,?,?,'pending',?)",
                    (
                        authorization_id,
                        run_id,
                        str(node["id"]),
                        previous_fingerprint,
                        target_fingerprint,
                        now,
                    ),
                )
                packed_inputs["_engine_authorization_id"] = authorization_id
            prior_resumes = int(
                self.connection.execute(
                    """
                    SELECT COUNT(*) FROM event_log
                    WHERE run_id = ? AND event_type = 'worker.codex.manual_resume_requested'
                    """,
                    (run_id,),
                ).fetchone()[0]
            )

            updated_node = self.connection.execute(
                """
                UPDATE node_runs
                SET status = 'queued', input_json = ?, error_json = NULL,
                    completed_at = NULL, lease_owner = NULL,
                    lease_acquired_at = NULL, lease_heartbeat_at = NULL,
                    lease_expires_at = NULL
                WHERE id = ? AND status = 'waiting_human'
                """,
                (_json_dump(packed_inputs), str(node["id"])),
            ).rowcount
            updated_run = self.connection.execute(
                """
                UPDATE runs
                SET status = 'queued', error_json = NULL, completed_at = NULL
                WHERE id = ? AND status = 'waiting_human'
                """,
                (run_id,),
            ).rowcount
            if updated_node != 1 or updated_run != 1:
                raise FukamiruError(f"Run {run_id} changed while manual resume was being requested")

            EventRepository(self.connection).append(
                run_id,
                "worker.codex.manual_resume_requested",
                object_type="node_run",
                object_id=str(node["id"]),
                payload={
                    "node_id": str(node["node_id"]),
                    "workflow_version": str(workflow_version),
                    "previous_retry_count": previous_retry_count,
                    "manual_resume_count": prior_resumes + 1,
                    "transport_retry_budget_reset": True,
                    "authorized_engine_fingerprint": authorized_engine_fingerprint,
                    "engine_authorization_id": authorization_id,
                    "previous_engine_fingerprint": previous_fingerprint,
                    "target_engine_fingerprint": target_fingerprint,
                    "engine_change_authorization_recorded": (authorization_id is not None),
                    "requested_at": now,
                },
            )
        except Exception:
            self.connection.rollback()
            raise
        return {
            "run_id": run_id,
            "status": "queued",
            "node_run_id": str(node["id"]),
            "node_id": str(node["node_id"]),
            "manual_resume_count": prior_resumes + 1,
            "authorized_engine_fingerprint": authorized_engine_fingerprint,
            "engine_authorization_id": authorization_id,
        }

    def snapshot(self, run_id: str) -> dict[str, Any]:
        run = self.get(run_id)
        active_nodes = [
            self._decode_json_columns(dict(row), ("input_json", "output_json", "error_json"))
            for row in self.connection.execute(
                """
                SELECT * FROM node_runs
                WHERE run_id = ? AND status IN ('queued', 'running', 'waiting_human')
                ORDER BY started_at, node_id, attempt
                """,
                (run_id,),
            )
        ]
        current_nodes = active_nodes
        if not current_nodes and run["status"] == "failed":
            failed_node = self.connection.execute(
                """
                SELECT * FROM node_runs
                WHERE run_id = ? AND status = 'failed'
                ORDER BY rowid DESC LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            current_nodes = (
                [
                    self._decode_json_columns(
                        dict(failed_node),
                        ("input_json", "output_json", "error_json"),
                    )
                ]
                if failed_node is not None
                else []
            )
        metrics = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM metrics WHERE run_id = ? ORDER BY id",
                (run_id,),
            )
        ]
        defects = [
            self._decode_json_columns(dict(row), ("details_json",))
            for row in self.connection.execute(
                "SELECT * FROM defects WHERE run_id = ? ORDER BY created_at",
                (run_id,),
            )
        ]
        reviews = [
            self._decode_json_columns(dict(row), ("options_json", "response_json"))
            for row in self.connection.execute(
                "SELECT * FROM human_reviews WHERE run_id = ? ORDER BY created_at",
                (run_id,),
            )
        ]
        artifacts = [
            self._decode_json_columns(dict(row), ("metadata_json",))
            for row in self.connection.execute(
                "SELECT * FROM run_artifacts WHERE run_id = ? ORDER BY created_at",
                (run_id,),
            )
        ]
        _, latest_event_id = EventRepository(self.connection).bounds(run_id)
        return {
            "run": run,
            "active_nodes": active_nodes,
            "current_nodes": current_nodes,
            "metrics": metrics,
            "defects": defects,
            "reviews": reviews,
            "artifacts": artifacts,
            "llm_observability": LLMObservationRepository(self.connection).summary(run_id),
            "patch_processing": self._proofreading_patch_progress(run, current_nodes),
            "latest_event_id": latest_event_id or 0,
        }

    def progress(self, run_id: str, *, events_limit: int = 200) -> dict[str, Any]:
        """Return a lightweight, frequently pollable Run projection."""
        row = self.connection.execute(
            """
            SELECT id, project_id, workflow_name, workflow_version, status,
                   input_json, created_at, started_at, completed_at, error_json
            FROM runs WHERE id = ?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            raise FukamiruError(f"Unknown run: {run_id}")
        run = dict(row)
        raw_input = run.pop("input_json")
        run["input"] = json.loads(raw_input) if raw_input else None
        raw_error = run.pop("error_json")
        run["error"] = json.loads(raw_error) if raw_error else None
        active_nodes = [
            dict(item)
            for item in self.connection.execute(
                """
                SELECT id, node_id, attempt, status, resource_lane,
                       started_at, completed_at, lease_heartbeat_at
                FROM node_runs
                WHERE run_id = ? AND status IN ('queued', 'running', 'waiting_human')
                ORDER BY started_at, node_id, attempt
                """,
                (run_id,),
            )
        ]
        current_nodes = active_nodes
        if not current_nodes and run["status"] == "failed":
            failed_node = self.connection.execute(
                """
                SELECT id, node_id, attempt, status, resource_lane,
                       started_at, completed_at, lease_heartbeat_at
                FROM node_runs
                WHERE run_id = ? AND status = 'failed'
                ORDER BY rowid DESC LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            current_nodes = [dict(failed_node)] if failed_node is not None else []
        artifacts = [
            dict(item)
            for item in self.connection.execute(
                """
                SELECT id, artifact_type, media_type, title, created_at
                FROM run_artifacts WHERE run_id = ? ORDER BY created_at
                """,
                (run_id,),
            )
        ]
        metric_rows = self.connection.execute(
            """
            SELECT metric_name, numeric_value, unit, created_at
            FROM metrics
            WHERE run_id = ? AND metric_name IN (
                'translation.batch.total', 'translation.batch.completed',
                'translation.characters.completed',
                'translation.post_edit.chunk.total',
                'translation.post_edit.chunk.completed',
                'translation.post_edit.characters.completed'
            )
            ORDER BY id DESC
            """,
            (run_id,),
        ).fetchall()
        latest_metrics: dict[str, sqlite3.Row] = {}
        for metric in metric_rows:
            latest_metrics.setdefault(str(metric["metric_name"]), metric)
        draft_total_metric = latest_metrics.get("translation.batch.total")
        post_edit_total_metric = latest_metrics.get("translation.post_edit.chunk.total")
        post_edit_is_current = post_edit_total_metric is not None and (
            draft_total_metric is None
            or str(post_edit_total_metric["created_at"]) >= str(draft_total_metric["created_at"])
        )
        if post_edit_is_current:
            progress_kind = "llm_jp_post_edit_chunks"
            progress_phase = "llm_jp_post_edit"
            total_metric = latest_metrics.get("translation.post_edit.chunk.total")
            completed_metric = latest_metrics.get("translation.post_edit.chunk.completed")
            characters_metric = latest_metrics.get("translation.post_edit.characters.completed")
        else:
            progress_kind = "translation_batches"
            progress_phase = "draft_translation"
            total_metric = latest_metrics.get("translation.batch.total")
            completed_metric = latest_metrics.get("translation.batch.completed")
            characters_metric = latest_metrics.get("translation.characters.completed")
        total = int(total_metric["numeric_value"]) if total_metric else None
        if total is None and str(run["workflow_name"]) == "translation":
            completed_draft = self.connection.execute(
                """
                SELECT output_json FROM node_runs
                WHERE run_id = ? AND node_id = 'draft_translation'
                  AND status = 'completed' AND output_json IS NOT NULL
                ORDER BY attempt DESC LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            if completed_draft is not None:
                draft_output = PayloadStore(self.connection).unpack(
                    json.loads(str(completed_draft["output_json"]))
                )
                batch_count = draft_output.get("batch_count")
                total = int(batch_count) if batch_count is not None else None
        completed = int(completed_metric["numeric_value"]) if completed_metric else None
        percentage = (
            round(min(100.0, max(0.0, completed * 100.0 / total)), 1)
            if total and completed is not None
            else None
        )
        updated_candidates = [
            str(metric["created_at"])
            for metric in (total_metric, completed_metric, characters_metric)
            if metric is not None
        ]
        events = EventRepository(self.connection).recent(run_id, limit=events_limit)
        progress_payload: dict[str, Any] = {
            "kind": progress_kind if total is not None else None,
            "phase": progress_phase if total is not None else None,
            "completed": completed,
            "total": total,
            "percentage": percentage,
            "current": min(completed + 1, total) if total and completed is not None else None,
            "completed_characters": (
                int(characters_metric["numeric_value"]) if characters_metric else None
            ),
            "updated_at": max(updated_candidates) if updated_candidates else None,
        }
        if total is None:
            ingestion_event = self.connection.execute(
                """
                SELECT payload_json, created_at FROM event_log
                WHERE run_id = ? AND event_type = 'ingestion.progress'
                ORDER BY event_id DESC LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            if ingestion_event is not None:
                activity = json.loads(str(ingestion_event["payload_json"]))
                activity_completed = activity.get("completed")
                activity_total = activity.get("total")
                activity_percentage = (
                    round(
                        min(
                            100.0,
                            max(
                                0.0,
                                float(activity_completed) * 100.0 / float(activity_total),
                            ),
                        ),
                        1,
                    )
                    if activity_total and activity_completed is not None
                    else None
                )
                progress_payload = {
                    **activity,
                    "kind": "ingestion_activity",
                    "percentage": activity_percentage,
                    "current": activity_completed,
                    "updated_at": str(ingestion_event["created_at"]),
                    "completed_characters": None,
                }
            elif str(run["workflow_name"]) == "manuscript_rewrite":
                rewrite_event = self.connection.execute(
                    """
                    SELECT event_type, payload_json, created_at FROM event_log
                    WHERE run_id = ? AND event_type IN (
                        'manuscript.reasoning.progress',
                        'manuscript.structure_plan.progress',
                        'manuscript.argument_plan.progress',
                        'manuscript.evidence.progress',
                        'manuscript.evidence_support.progress',
                        'manuscript.query_plan.progress',
                        'manuscript.final_findings.progress',
                        'manuscript.certainty.progress',
                        'manuscript.references.progress',
                        'manuscript.enumeration_context.progress',
                        'manuscript.paragraph_plan.progress',
                        'manuscript.rewrite.progress',
                        'manuscript.predicate_resolution.progress',
                        'manuscript.coherence.progress',
                        'manuscript.polish.progress',
                        'manuscript.polish_target_detection.progress',
                        'manuscript.polish_candidate_generation.progress',
                        'manuscript.polish_candidate_selection.progress'
                    )
                    ORDER BY event_id DESC LIMIT 1
                    """,
                    (run_id,),
                ).fetchone()
                if rewrite_event is not None:
                    rewrite = json.loads(str(rewrite_event["payload_json"]))
                    rewrite_completed = int(rewrite.get("completed") or 0)
                    rewrite_total = int(rewrite.get("total") or 0)
                    phase_kinds = {
                        "manuscript.reasoning.progress": (
                            "manuscript_reasoning_batches",
                            "review_manuscript_reasoning",
                        ),
                        "manuscript.structure_plan.progress": (
                            "manuscript_structure_plan_passes",
                            "plan_manuscript_structure",
                        ),
                        "manuscript.argument_plan.progress": (
                            "manuscript_argument_processes",
                            "plan_manuscript_structure",
                        ),
                        "manuscript.evidence.progress": (
                            "manuscript_research_queries",
                            "resolve_manuscript_evidence",
                        ),
                        "manuscript.evidence_support.progress": (
                            "manuscript_evidence_support",
                            "validate_evidence_support",
                        ),
                        "manuscript.query_plan.progress": (
                            "manuscript_query_plan",
                            "plan_research_queries",
                        ),
                        "manuscript.certainty.progress": (
                            "manuscript_certainty",
                            "calibrate_manuscript_certainty",
                        ),
                        "manuscript.final_findings.progress": (
                            "manuscript_final_findings",
                            "persist_rewrite_proposal",
                        ),
                        "manuscript.references.progress": (
                            "manuscript_reference_entries",
                            "resolve_reference_metadata",
                        ),
                        "manuscript.enumeration_context.progress": (
                            "manuscript_enumeration_batches",
                            "resolve_enumeration_context",
                        ),
                        "manuscript.paragraph_plan.progress": (
                            "manuscript_paragraph_plan_passes",
                            "plan_semantic_paragraphs",
                        ),
                        "manuscript.rewrite.progress": (
                            "manuscript_rewrite_batches",
                            "draft_rewrite",
                        ),
                        "manuscript.predicate_resolution.progress": (
                            "manuscript_predicate_candidates",
                            "resolve_manuscript_predicate_relations",
                        ),
                        "manuscript.coherence.progress": (
                            "manuscript_coherence_passes",
                            "validate_rewrite_coherence",
                        ),
                        "manuscript.polish.progress": (
                            "manuscript_polish_chunks",
                            "polish_manuscript_japanese",
                        ),
                        "manuscript.polish_target_detection.progress": (
                            "manuscript_polish_targets",
                            "detect_japanese_polish_targets",
                        ),
                        "manuscript.polish_candidate_generation.progress": (
                            "manuscript_polish_candidates",
                            "polish_manuscript_japanese",
                        ),
                        "manuscript.polish_candidate_selection.progress": (
                            "manuscript_polish_selection",
                            "select_japanese_polish_candidates",
                        ),
                    }
                    kind, phase = phase_kinds[str(rewrite_event["event_type"])]
                    phase = str(rewrite.get("phase") or phase)
                    progress_payload = {
                        **rewrite,
                        "kind": kind,
                        "phase": phase,
                        "percentage": (
                            round(
                                min(100.0, rewrite_completed * 100.0 / rewrite_total),
                                1,
                            )
                            if rewrite_total
                            else None
                        ),
                        "current": rewrite_completed,
                        "updated_at": str(rewrite_event["created_at"]),
                        "completed_characters": None,
                    }
        progress_payload = self._apply_active_progress_phase(
            progress_payload,
            active_nodes,
        )
        return {
            "run": run,
            "active_nodes": active_nodes,
            "current_nodes": current_nodes,
            "artifacts": artifacts,
            "progress": progress_payload,
            "patch_processing": self._proofreading_patch_progress(run, current_nodes),
            "events": [
                {
                    "event_id": event.event_id,
                    "run_id": event.run_id,
                    "type": event.event_type,
                    "object_type": event.object_type,
                    "object_id": event.object_id,
                    "payload": event.payload,
                    "created_at": event.created_at,
                }
                for event in events
            ],
            "latest_event_id": events[-1].event_id if events else 0,
        }

    def _decode_json_columns(
        self, value: dict[str, Any], columns: tuple[str, ...]
    ) -> dict[str, Any]:
        for column in columns:
            raw = value.pop(column)
            value[column.removesuffix("_json")] = (
                PayloadStore(self.connection).unpack(json.loads(raw)) if raw else None
            )
        return value
