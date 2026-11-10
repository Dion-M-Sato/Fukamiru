from copy import deepcopy
from types import SimpleNamespace

import pytest

from fukamiru.authoring.evidence_contracts import (
    build_fixed_assets,
    compile_draft_evidence,
    evidence_use_identity_hash,
)
from fukamiru.authoring.generation import (
    recover_empty_citation_projection,
    recover_submission_revalidation,
    reset_candidate_generation,
)
from fukamiru.worker.runner import WorkerRunner


def test_new_candidate_generation_archives_derived_results_and_preserves_authority():
    context = {
        "section_contracts": [{"section_id": "S1"}], "fixed_assets": {"id": "fixed"},
        "argument_nodes": [{"id": "E1"}], "reference_entries": [{"reference_id": "R1"}],
        "protected_human_blocks": ["原文"], "canonical_html": "<article>原文</article>",
        "human_question_feedback": {"answers": [{"answer": "資料は手元にない。"}]},
        "human_review_items": [{"status": "resolved"}], "_join_generations": {"join": "G1"},
        "auto_apply_japanese_proofreading": True,
        "authoring_submission_revalidation": {"review_id": "review", "reason": "human_feedback"},
        "manuscript_proofreading_preference": {"style": "丁寧"},
        "submission_validation_gate": {"failed_validations": ["references"]},
        "authoring_paragraph_citation_lineage": [{"evidence_ids": ["old-source"]}],
        "applied_edits": [{"evidence_ids": ["old-source"]}],
        "footnoted_coherence_report": {"passed": True},
        "manuscript_proofreading_shadow": {"id": "old"},
        "japanese_redrafting": {"candidate": "old"}, "audit_bundle": {"findings": []},
    }
    before = deepcopy(context)
    cleared = {"submission_validation_gate", "authoring_paragraph_citation_lineage",
               "applied_edits", "footnoted_coherence_report", "manuscript_proofreading_shadow",
               "japanese_redrafting", "audit_bundle"}
    result = WorkerRunner._authoring_start_paragraph_formation(
        object.__new__(WorkerRunner), SimpleNamespace(node_run_id="new-generation"), context,
    ).context
    assert not cleared & result.keys()
    assert set(result["prior_candidate_generations"][0]["cleared_fields"]) == cleared
    assert result["prior_candidate_generations"][0]["source_node_run_id"] == "new-generation"
    for key in context.keys() - cleared:
        assert result[key] == context[key]
    assert context == before
    assert reset_candidate_generation(result, source_node_run_id="again") == result


def test_uncited_section_stays_uncited_across_prose_only_polishing():
    context = {
        "plan_id": "plan", "language": "ja",
        "research_problem": {"research_question": "問い"},
        "section_contracts": [{"section_id": "conclusion", "title": "結論",
            "article_role": "conclusion", "claim_ids": ["C1"], "evidence_ids": ["E1"],
            "development_expectations": {"required_argument_moves": []}}],
        "argument_nodes": [{"id": "E1", "node_type": "evidence", "claim_id": "C1",
                            "provenance": {"source_ids": ["source"]}}],
        "evidence_context": [{"id": "source"}],
        "draft_sections": [{"section_id": "conclusion", "article_role": "conclusion",
            "paragraphs": [{"text": "筆者の結論である。", "evidence_ids": [],
                            "move_indices": []}], "text": "筆者の結論である。"}],
    }
    html = WorkerRunner._authoring_render_sections(context, context["draft_sections"])
    first = WorkerRunner._authoring_v29_plan_bridge(context, html)
    assert first["applied_edits"] == []
    assert first["authoring_paragraph_citation_lineage"] == []
    blocks = first["authoring_citation_lineage_reconciliation"]["projected_block_ids"]
    assert blocks == ["P-plan-conclusion-001"]
    prose = deepcopy(context["draft_sections"])
    prose[0]["paragraphs"] = [{"text": "筆者による結論である。", "move_indices": []}]
    prose[0]["text"] = "筆者による結論である。"
    polished_html = WorkerRunner._authoring_render_sections(context, prose)
    result = WorkerRunner._authoring_v29_plan_bridge(
        {**context, **first, "draft_sections": prose}, polished_html)
    assert result["applied_edits"] == []
    assert result["authoring_paragraph_citation_lineage"] == []
    assert result["paragraph_plan"]["paragraphs"][0]["evidence_ids"] == []
    # A new explicit source choice still takes effect; negative coverage is not a waiver.
    prose[0]["paragraphs"][0]["evidence_ids"] = ["E1"]
    explicit = WorkerRunner._authoring_v29_plan_bridge(
        {**context, **first, "draft_sections": prose}, polished_html)
    assert explicit["applied_edits"][0]["evidence_ids"] == ["source"]


def empty_projection_fixture():
    context = {"plan_id": "plan", "language": "ja", "draft_attempt": 2,
        "research_problem": {"research_question": "問い"},
        "propositions": [{"id": "P1", "text": "著者の考え。"}],
        "evidence_context": [{"id": "B1", "title": "資料名", "author": "著者B",
            "reference_id": "R1", "verification_status": "verified"}],
        "reference_entries": [{"reference_id": "R1", "title": "資料名", "author": "著者B",
                               "year": "2020", "evidence_ids": ["B1"]}],
        "argument_nodes": [{"id": "E1", "node_type": "evidence", "claim_id": "C1",
                            "provenance": {"source_ids": ["B1"]}},
                           {"id": "E2", "node_type": "evidence", "claim_id": "C2",
                            "provenance": {"source_ids": ["P1"]}}],
        "human_question_feedback": {"answers": [{"answer": "資料は手元にない。"}]}}
    context["fixed_assets"] = build_fixed_assets(context).model_dump(mode="json")
    context["section_contracts"] = [{"section_id": sid, "title": sid, "article_role": role,
        "claim_ids": [claim], "evidence_ids": [evidence],
        "development_expectations": {"required_argument_moves": []}}
        for sid, role, claim, evidence in [("s1", "body", "C1", "E1"),
                                           ("s2", "conclusion", "C2", "E2")]]
    sections = [{"section_id": sid, "article_role": role, "claim_ids": [claim],
        "text": text, "paragraphs": [{"text": text, "evidence_ids": [evidence],
                                      "move_indices": []}],
        "evidence_treatment": [{"evidence_id": evidence, "claim_id": claim,
            "prose_treatment": treatment, "support_sentence": text}]}
        for sid, role, claim, evidence, text, treatment in [
            ("s1", "body", "C1", "E1", "著者Bの『資料名』を比較の枠として参照する。",
             "attributed_scholarship"),
            ("s2", "conclusion", "C2", "E2", "著者は二つの条件を区別する。",
             "qualified_interpretation")]]
    sections, uses, _, _ = compile_draft_evidence(context, sections)
    context.update({"draft_sections": sections,
        "evidence_uses": [use.model_dump(mode="json") for use in uses],
        "evidence_identity_hash": evidence_use_identity_hash(uses),
        "draft_evidence_identity_generation": {"producer_node_run_id": "producer"},
        "proofreading_evidence_identity_validation": {"passed": True},
        "manuscript_proofreading_shadow": {"applied_count": 1}})
    html = WorkerRunner._authoring_render_sections(context, sections)
    context = {**context, **WorkerRunner._authoring_v29_plan_bridge(context, html),
               "proposed_html": html}
    context["authoring_citation_lineage_reconciliation"].pop("projected_block_ids")
    context["authoring_paragraph_citation_lineage"].append({
        "output_block_id": "P-plan-s2-001", "evidence_ids": ["B1"],
        "argument_evidence_node_ids": ["E1"]})
    context["applied_edits"].append({"operation": "replace_text",
        "block_id": "P-plan-s2-001", "output_block_id": "P-plan-s2-001",
        "evidence_ids": ["B1"]})
    return context


EMPTY_PROJECTION_ERROR = (
    "Authoring footnote needs a unique visible source mention in its cited sentence: P-plan-s2-001")


def test_empty_citation_recovery_changes_only_projection_and_preserves_cited_paragraphs():
    context = empty_projection_fixture()
    before = deepcopy(context)
    corrected = recover_empty_citation_projection(
        context, error_message=EMPTY_PROJECTION_ERROR, source_node_run_id="failed")
    assert corrected is not None
    assert context == before
    changed = {key for key in context if context[key] != corrected[key]}
    assert changed <= {"applied_edits", "authoring_paragraph_citation_lineage",
                       "authoring_citation_lineage_reconciliation", "paragraph_plan",
                       "realized_paragraphs", "structure_plan"}
    assert [item["output_block_id"] for item in corrected["applied_edits"]] == ["P-plan-s1-001"]
    assert corrected["authoring_empty_citation_projection_recoveries"][-1][
        "removed_bindings"] == [("P-plan-s2-001", "B1")]


@pytest.mark.parametrize("kind", ["missing_shadow", "changed_identity", "missing_citation",
                                  "different_error", "no_extra_binding"])
def test_empty_citation_recovery_rejects_unproven_or_real_source_changes(kind):
    context = empty_projection_fixture()
    error = EMPTY_PROJECTION_ERROR
    if kind == "missing_shadow":
        context.pop("manuscript_proofreading_shadow")
    elif kind == "changed_identity":
        context["evidence_identity_hash"] = "changed"
    elif kind == "missing_citation":
        context["authoring_paragraph_citation_lineage"] = context[
            "authoring_paragraph_citation_lineage"][1:]
    elif kind == "different_error":
        error = error.replace("s2", "s1")
    else:
        context["authoring_paragraph_citation_lineage"] = context[
            "authoring_paragraph_citation_lineage"][:1]
    assert recover_empty_citation_projection(
        context, error_message=error, source_node_run_id="failed") is None


def submission_marker_fixture():
    import hashlib

    from fukamiru.authoring.evidence_contracts import canonical_hash

    marker = {"review_id": "review", "reason": "human_question_feedback", "started_at": "then"}
    source = {"source_document_id": "source", "source_revision_id": "revision",
        "document_id": "paper", "plan_id": "plan", "plan_revision": 1,
        "fixed_assets": {"asset_hash": "fixed"},
        "human_question_feedback": {"answers": [{"answer": "資料は手元にない。"}]},
        "authoring_submission_revalidation": marker}
    html = ('<article id="D" lang="ja"><section id="S"><h2 id="H">本文</h2>'
            '<p id="P">回答を反映した本文。</p></section></article>')
    current = {key: value for key, value in source.items()
               if key != "authoring_submission_revalidation"}
    current.update({"proposed_html": html,
        "frozen_candidate_v26": {"html_sha256": hashlib.sha256(html.encode()).hexdigest()},
        "prior_candidate_generations": [{"source_node_run_id": "archived", "cleared_fields": {
            "authoring_submission_revalidation": canonical_hash(marker)}}]})
    return current, source


def test_submission_revalidation_restores_only_archived_human_intent():
    current, source = submission_marker_fixture()
    before = deepcopy(current)
    result = recover_submission_revalidation(current, source, "archived")
    assert result is not None and current == before
    assert all(result[key] == value for key, value in current.items())
    assert result["authoring_submission_revalidation"] == source[
        "authoring_submission_revalidation"]


@pytest.mark.parametrize("kind", ["wrong_archive", "changed_answer", "changed_assets",
                                  "changed_candidate", "missing_identity"])
def test_submission_marker_recovery_rejects_changed_or_unproven_inputs(kind):
    current, source = submission_marker_fixture()
    source_id = "archived"
    if kind == "wrong_archive":
        source_id = "other"
    elif kind == "changed_answer":
        current["human_question_feedback"] = {"answers": []}
    elif kind == "changed_assets":
        current["fixed_assets"] = {"asset_hash": "changed"}
    elif kind == "changed_candidate":
        current["proposed_html"] = current["proposed_html"].replace("本文", "変更")
    else:
        current.pop("source_revision_id")
    assert recover_submission_revalidation(current, source, source_id) is None
