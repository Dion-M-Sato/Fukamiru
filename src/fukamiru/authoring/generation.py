"""Invalidate results derived from a prior manuscript without changing its sources."""

from __future__ import annotations

import hashlib
from typing import Any

from fukamiru.authoring.evidence_contracts import (
    canonical_hash,
    compile_draft_evidence,
    evidence_use_identity_hash,
)
from fukamiru.errors import FukamiruError

_CANDIDATE_FIELDS = {
    "authoring_paragraph_citation_lineage", "authoring_citation_lineage_reconciliation",
    "authoring_citation_title_restorations", "authoring_v31_attribution_predicate_validation",
    "applied_edits", "evidence_use_citation_links", "citation_event_drafts",
    "citation_reference_filter_report", "reference_quality_gate", "reference_render_report",
    "reference_inventory", "submission_package",
    "audit_findings", "authoring_audit_section_reviews", "authoring_economy_audit_blockers",
    "draft_audit_join", "source_preservation_validation", "audit_bundle", "audit_manuscript_hash",
    "audit_version", "audited_section_ids", "clean_audited_section_ids",
    "audit_finding_target_normalizations", "ignored_invalid_llm_audit_findings",
    "v26_audit_coverage", "final_finding_audit", "v26_coherence_report",
    "coherence_repair_history", "manuscript_coherence_checks", "coherence_unresolved_routes",
    "japanese_redrafting", "polished_sections", "polish_preserved_role_sections",
    "polish_citation_title_restorations", "pre_proofreading_html", "proofreading_application",
    "prose_redraft_report_v31", "prose_redraft_history_v31", "heading_revision_report_v31",
}
_CANDIDATE_PREFIXES = (
    "footnoted_", "footnote_", "evidence_footnote_", "submission_validation_",
    "manuscript_proofreading_", "_proofreading_", "manuscript_polish",
)


def reset_candidate_generation(
    context: dict[str, Any], *, source_node_run_id: str,
) -> dict[str, Any]:
    """Old values remain in the immutable source node input; record their hashes."""
    cleared = {key: value for key, value in context.items()
               if (key in _CANDIDATE_FIELDS or key.startswith(_CANDIDATE_PREFIXES))
               and key != "manuscript_proofreading_preference"}
    if not cleared:
        return dict(context)
    return {**{key: value for key, value in context.items() if key not in cleared},
            "prior_candidate_generations": [
                *list(context.get("prior_candidate_generations") or []),
                {"source_node_run_id": source_node_run_id,
                 "candidate_sha256": hashlib.sha256(
                     str(context.get("proposed_html") or "").encode()).hexdigest(),
                 "cleared_fields": {key: canonical_hash(value) for key, value in cleared.items()}},
            ]}


def recover_stale_candidate_projection(
    context: dict[str, Any], reconciliation_input: dict[str, Any], *,
    error_message: str, source_node_run_id: str,
) -> dict[str, Any] | None:
    """Rebuild only an intact new draft that inherited a prior submission's results."""
    from fukamiru.revisions.footnote_citations import EvidenceFootnotePlanner
    from fukamiru.worker.runner import WorkerRunner

    if not error_message.startswith(
        "Authoring footnote needs a unique visible source mention in its cited sentence: "
    ):
        return None
    if any(context.get(key) != reconciliation_input.get(key) for key in (
        "proposed_html", "draft_sections", "evidence_uses", "fixed_assets", "draft_attempt",
    )):
        return None
    try:
        gate = dict(reconciliation_input.get("submission_validation_gate") or {})
        html = str(context.get("proposed_html") or "")
        if (int(context.get("draft_attempt", 0)) < 2 or not gate.get("failed_validations")
                or not gate.get("candidate_sha256")
                or gate["candidate_sha256"] == hashlib.sha256(html.encode()).hexdigest()
                or not reconciliation_input.get("evidence_footnote_render_report")
                or not context.get("authoring_paragraph_citation_lineage")):
            return None
        _, compiled, _, _ = compile_draft_evidence(context, context["draft_sections"])
        if ([item.model_dump(mode="json") for item in compiled] != context["evidence_uses"]
                or evidence_use_identity_hash(compiled) != context.get("evidence_identity_hash")):
            return None

        def footnotes(current: dict[str, Any]) -> None:
            EvidenceFootnotePlanner.plan(
                html, applied_edits=current["applied_edits"],
                evidence_context=context["evidence_context"],
                reference_entries=context["reference_entries"], require_distinct_anchors=True,
            )

        try:
            footnotes(context)
        except FukamiruError as exc:
            if str(exc) != error_message:
                return None
        else:
            return None
        cleaned = reset_candidate_generation(context, source_node_run_id=source_node_run_id)
        projected = WorkerRunner._authoring_v29_plan_bridge(
            cleaned, html, semantic_attribution=True,
        )
        if not projected["structure_plan_validation"]["valid"]:
            return None
        corrected = {**cleaned, **projected}
        footnotes(corrected)
        return corrected
    except (FukamiruError, ValueError, KeyError, TypeError):
        return None


def recover_empty_citation_projection(
    context: dict[str, Any], *, error_message: str, source_node_run_id: str,
) -> dict[str, Any] | None:
    """Recover only an added binding on a strictly compiled, uncited paragraph."""
    from fukamiru.revisions.footnote_citations import EvidenceFootnotePlanner
    from fukamiru.worker.runner import WorkerRunner

    prefix = "Authoring footnote needs a unique visible source mention in its cited sentence: "
    if (not error_message.startswith(prefix)
            or not context.get("manuscript_proofreading_shadow")
            or not dict(context.get("proofreading_evidence_identity_validation") or {}).get(
                "passed")
            or not dict(context.get("draft_evidence_identity_generation") or {}).get(
                "producer_node_run_id")):
        return None
    try:
        _, uses, _, _ = compile_draft_evidence(context, context["draft_sections"])
        if ([use.model_dump(mode="json") for use in uses] != context["evidence_uses"]
                or evidence_use_identity_hash(uses) != context.get("evidence_identity_hash")):
            return None
        clean = {key: value for key, value in context.items() if key not in {
            "authoring_paragraph_citation_lineage", "authoring_citation_lineage_reconciliation"}}
        source = WorkerRunner._authoring_render_sections(
            clean, context["draft_sections"], include_citation_mentions=True)
        base = WorkerRunner._authoring_v29_plan_bridge(clean, source, semantic_attribution=True)

        def pairs(current: dict[str, Any]) -> set[tuple[str, str]]:
            return {(str(item["output_block_id"]), str(evidence_id))
                    for item in current.get("authoring_paragraph_citation_lineage") or []
                    for evidence_id in item.get("evidence_ids") or []}

        prior_pairs, base_pairs = pairs(context), pairs(base)
        extra = prior_pairs - base_pairs
        empty_blocks = {item["output_block_id"]
                        for item in base["paragraph_plan"]["paragraphs"]
                        if not item["evidence_ids"]}
        if (base_pairs - prior_pairs or not extra
                or any(block != error_message.removeprefix(prefix) or block not in empty_blocks
                       for block, _ in extra)):
            return None

        def check(current: dict[str, Any]) -> None:
            EvidenceFootnotePlanner.plan(
                context["proposed_html"], applied_edits=current["applied_edits"],
                evidence_context=context["evidence_context"],
                reference_entries=context["reference_entries"], require_distinct_anchors=True)

        try:
            check(context)
        except FukamiruError as exc:
            if str(exc) != error_message:
                return None
        else:
            return None
        projected = WorkerRunner._authoring_v29_plan_bridge(
            {**context, **base}, context["proposed_html"], semantic_attribution=True)
        if (pairs(projected) != base_pairs
                or not projected["structure_plan_validation"]["valid"]
                or not projected["paragraph_plan_validation"]["valid"]
                or projected["authoring_citation_lineage_reconciliation"]["dropped_binding_count"]):
            return None
        check(projected)
        return {**context, **projected, "authoring_empty_citation_projection_recoveries": [
            *list(context.get("authoring_empty_citation_projection_recoveries") or []),
            {"source_node_run_id": source_node_run_id,
             "candidate_sha256": hashlib.sha256(context["proposed_html"].encode()).hexdigest(),
             "removed_bindings": sorted(extra),
             "prior_projection_sha256": {key: canonical_hash(context.get(key)) for key in (
                 "applied_edits", "authoring_paragraph_citation_lineage",
                 "authoring_citation_lineage_reconciliation")}},
        ]}
    except (FukamiruError, ValueError, KeyError, TypeError):
        return None


def rendered_qualification_recovery(
    context: dict[str, Any], error_message: str,
) -> dict[str, Any] | None:
    """Identify a render failure consisting only of reviewable uncertainty wording."""
    from fukamiru.documents.canonical_html import CanonicalHTMLValidator
    from fukamiru.revisions.footnote_citations import EvidenceFootnotePlanner
    from fukamiru.worker.runner import WorkerRunner

    prefix = "V31 rendered manuscript has reader-facing scaffold: "
    if (not error_message.startswith(prefix)
            or not dict(context.get("proofreading_evidence_identity_validation") or {}).get(
                "passed")):
        return None
    try:
        html = context["proposed_html"]
        CanonicalHTMLValidator().assert_valid(html)
        _, uses, _, _ = compile_draft_evidence(context, context["draft_sections"])
        if ([use.model_dump(mode="json") for use in uses] != context["evidence_uses"]
                or evidence_use_identity_hash(uses) != context.get("evidence_identity_hash")):
            return None
        findings = WorkerRunner._authoring_reader_facing_v31_findings(
            WorkerRunner._authoring_reader_facing_sections_from_html(html))
        hard = [item for item in findings if item["code"] != "reader_facing_internal_scaffold"]
        if (not hard or not all(WorkerRunner._authoring_reviewable_scholarly_qualification(item)
                                for item in hard)
                or ", ".join(str(item.get("object_id") or "") for item in hard)
                != error_message.removeprefix(prefix)):
            return None
        EvidenceFootnotePlanner.plan(html, applied_edits=context["applied_edits"],
            evidence_context=context["evidence_context"],
            reference_entries=context["reference_entries"],
            require_distinct_anchors=True)
        return {"candidate_sha256": hashlib.sha256(html.encode()).hexdigest(),
                "qualification_findings": hard,
                "resolution_route": "prose_redraft_then_human_review"}
    except (FukamiruError, ValueError, KeyError, TypeError):
        return None


def recover_submission_revalidation(
    context: dict[str, Any], source: dict[str, Any], source_node_run_id: str,
) -> dict[str, Any] | None:
    """Restore only a human-requested replacement marker archived by old resets."""
    from fukamiru.documents.canonical_html import CanonicalHTMLValidator

    marker = source.get("authoring_submission_revalidation")
    html = str(context.get("proposed_html") or "")
    if (context.get("authoring_submission_revalidation") or not isinstance(marker, dict)
            or any(not context.get(key) for key in (
                "source_document_id", "source_revision_id", "document_id", "plan_id",
                "plan_revision"))
            or marker.get("reason") != "human_question_feedback" or not marker.get("review_id")
            or not CanonicalHTMLValidator().validate(html).valid
            or dict(context.get("frozen_candidate_v26") or {}).get("html_sha256")
            != hashlib.sha256(html.encode()).hexdigest()
            or any(context.get(key) != source.get(key) for key in (
                "source_document_id", "source_revision_id", "document_id", "plan_id",
                "plan_revision", "fixed_assets", "human_question_feedback"))
            or not any(item.get("source_node_run_id") == source_node_run_id
                       and dict(item.get("cleared_fields") or {}).get(
                           "authoring_submission_revalidation") == canonical_hash(marker)
                       for item in context.get("prior_candidate_generations") or [])):
        return None
    return {**context, "authoring_submission_revalidation": dict(marker),
            "authoring_submission_revalidation_recoveries": [
                *list(context.get("authoring_submission_revalidation_recoveries") or []),
                {"source_node_run_id": source_node_run_id, "marker_sha256": canonical_hash(marker)},
            ]}
