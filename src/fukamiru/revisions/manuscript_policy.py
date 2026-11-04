"""Versioned user policy for manuscript workflow V22.

The policy is data rather than control flow so every Proposal can retain the
quality assumptions under which it was produced.  Runtime failures remain
outside this policy: a transport failure must never become a content verdict.
"""

from __future__ import annotations

import copy
from typing import Any

MANUSCRIPT_WORKFLOW_V22_POLICY: dict[str, Any] = {
    "id": "manuscript-quality-policy",
    "workflow_version": 22,
    "semantic_uncertainty": {
        "conditional_success_allowed": True,
        "human_judgement_required_kinds": [
            "term_introduction",
            "citation_function",
            "causal_relation",
        ],
        "human_judgement_required_statuses": [
            "semantic_analysis_unresolved",
            "human_selection_pending",
        ],
        "minor_lexical_action": "preserve_source",
        "mandatory_revision_unmet_action": "block_revision_approval",
        "unclassified_analysis_action": "block_revision_approval",
        "nonblocking_source_preserved_classes": [
            "minor_lexical_source_preserved",
            "polish_candidate_source_preserved",
        ],
    },
    "external_reasoning": {
        "mandatory_nodes_may_be_skipped": False,
        "transport_failure_is_conditional_success": False,
        "automatic_fallback_engine": False,
        "engine_change_requires_explicit_user_authorization": True,
        "record_engine_change_as_quality_policy_change": True,
    },
    "partial_response": {
        "retry_scope": "missing_units_only",
        "max_targeted_retries": 1,
        "after_retry_exhaustion": "conditional_success",
    },
    "bibliography": {
        "consulted_references": "actual_manifestation_used",
        "discovered_related_references_section": "機械判別参考文献",
        "unmentioned_original_or_translation": "render_all_eligible_manifestations",
        "specified_volume": "render_specified_volume_only",
        "unspecified_volume": "render_complete_set",
        "specified_edition": "render_specified_edition_only",
        "unspecified_edition": "render_all_eligible_editions",
        "translator_required_for_translated_book": True,
        "unresolved_manifestation_allows_conditional_proposal": True,
        "unresolved_manifestation_blocks_revision_approval": True,
        "never_assume_currently_available_edition_was_consulted": True,
        "unidentified_self_or_unpublished_work_requires_user_metadata": True,
    },
}


def manuscript_workflow_v22_policy() -> dict[str, Any]:
    """Return an isolated copy suitable for durable Run/Proposal context."""

    return copy.deepcopy(MANUSCRIPT_WORKFLOW_V22_POLICY)
