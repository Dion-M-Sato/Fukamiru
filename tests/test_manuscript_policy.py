from fukamiru.revisions.manuscript_policy import manuscript_workflow_v22_policy


def test_v22_policy_preserves_user_defined_failure_boundaries() -> None:
    policy = manuscript_workflow_v22_policy()

    assert policy["semantic_uncertainty"] == {
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
    }
    assert policy["external_reasoning"] == {
        "mandatory_nodes_may_be_skipped": False,
        "transport_failure_is_conditional_success": False,
        "automatic_fallback_engine": False,
        "engine_change_requires_explicit_user_authorization": True,
        "record_engine_change_as_quality_policy_change": True,
    }
    assert policy["partial_response"] == {
        "retry_scope": "missing_units_only",
        "max_targeted_retries": 1,
        "after_retry_exhaustion": "conditional_success",
    }


def test_v22_policy_preserves_bibliography_decisions() -> None:
    bibliography = manuscript_workflow_v22_policy()["bibliography"]

    assert bibliography["consulted_references"] == "actual_manifestation_used"
    assert bibliography["discovered_related_references_section"] == "機械判別参考文献"
    assert bibliography["unmentioned_original_or_translation"] == (
        "render_all_eligible_manifestations"
    )
    assert bibliography["specified_volume"] == "render_specified_volume_only"
    assert bibliography["unspecified_volume"] == "render_complete_set"
    assert bibliography["specified_edition"] == "render_specified_edition_only"
    assert bibliography["unspecified_edition"] == "render_all_eligible_editions"
    assert bibliography["translator_required_for_translated_book"] is True
    assert bibliography["unresolved_manifestation_allows_conditional_proposal"] is True
    assert bibliography["unresolved_manifestation_blocks_revision_approval"] is True
    assert bibliography["never_assume_currently_available_edition_was_consulted"] is True
    assert bibliography["unidentified_self_or_unpublished_work_requires_user_metadata"] is True


def test_v22_policy_returns_an_isolated_copy() -> None:
    first = manuscript_workflow_v22_policy()
    first["partial_response"]["max_targeted_retries"] = 99

    assert manuscript_workflow_v22_policy()["partial_response"]["max_targeted_retries"] == 1
