from __future__ import annotations

import hashlib
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_japanese_polish import target_fixture

from fukamiru.revisions.japanese_polish import CandidateCorpusScorer, WordingCandidate
from fukamiru.revisions.reasoning import ManuscriptReasoningRepository
from fukamiru.storage.database import SQLiteDatabase
from fukamiru.worker.runner import WorkerRunner

HTML = (
    '<article id="D" lang="ja"><section id="S"><h2 id="H">本文</h2>'
    '<p id="P">位置づける。取り扱う。</p></section></article>'
)


@pytest.fixture
def selection_case(monkeypatch):
    from test_manuscript_rewrite import ManuscriptRewriteTests

    case = ManuscriptRewriteTests()
    case.setUp()
    case.run_id = case._run_row()
    case.claimed = SimpleNamespace(run_id=case.run_id, workflow_version="22")
    database = SQLiteDatabase(case.root / "state.db", case.root / "unused-migrations")
    worker = object.__new__(WorkerRunner)
    worker.databases = SimpleNamespace(state=database, knowledge=database)
    worker.settings = SimpleNamespace(
        translation=SimpleNamespace(
            manuscript_candidate_beam_width=8,
            manuscript_candidate_human_training_target=100,
            manuscript_candidate_auto_apply_margin=0.12,
        )
    )
    worker._record_manuscript_progress = lambda *args, **kwargs: None
    worker._manuscript_candidate_corpus_query = lambda text: ""
    monkeypatch.setattr(
        CandidateCorpusScorer, "score", staticmethod(lambda target, candidate, **_: candidate)
    )
    case.worker = worker
    case.context = {
        "proposed_html": HTML,
        "use_llm_jp": True,
        "project_id": case.project["id"],
        "document_id": case.document_id,
    }
    try:
        yield case
    finally:
        case.tearDown()


def target(
    target_id="first",
    term="位置づける",
    start=0,
    *,
    unresolved=False,
    issue_codes=("lexical.context_inappropriate", "scope.phrase_only"),
):
    return target_fixture(
        id=target_id,
        source_text=term,
        start_offset=start,
        end_offset=start + len(term),
        issue_codes=issue_codes,
        relation_type="surface",
        allowed_relation_types=("surface", "preserve"),
        semantic_roles={"_analysis_status": "unresolved" if unresolved else "resolved"},
        terminology=(),
        request_signature="request:" + target_id,
    )


def save_targets(case, targets):
    ManuscriptReasoningRepository(case.connection).replace_polish_targets(
        tuple(targets),
        run_id=case.run_id,
        node_run_id=None,
        document_id=case.document_id,
        project_id=case.project["id"],
    )


def save_candidates(case, polish_target, mode):
    original = WordingCandidate(
        "original:" + polish_target.id, polish_target.id, 0, polish_target.source_text,
        "preserve", "原文", 1.0, "original", total_score=0.0,
    )
    candidates = [original]
    if mode not in {"candidate_generation_failed", "missing_original"}:
        alternative_text = (
            "位置付ける"
            if polish_target.source_text == "位置づける"
            else "取りあつかう"
        )
        candidates.append(replace(
            original, id="alternative:" + polish_target.id, ordinal=1,
            text=alternative_text, source_kind="llm_jp", relation_type="surface",
            total_score=(
                -1.0 if mode == "mandatory_revision_unmet"
                else -0.17 if mode == "human_selection_pending" else 10.0
            ),
            hard_veto_reasons=("unsafe_candidate",) if mode == "candidates_inappropriate" else (),
        ))
    ManuscriptReasoningRepository(case.connection).save_polish_candidates(
        tuple(candidates), run_id=case.run_id, target_id=polish_target.id,
        model_identity="local-test-fixture", expected_candidate_count=6, issues=(),
    )
    if mode == "missing_original":
        case.connection.execute(
            "DELETE FROM manuscript_polish_candidates WHERE target_id=?",
            (polish_target.id,),
        )
        case.connection.commit()


def test_v28_requires_a_score_improvement_before_auto_apply(selection_case):
    case = selection_case
    case.claimed = SimpleNamespace(run_id=case.run_id, workflow_version="28")
    polish_target = target()
    save_targets(case, [polish_target])
    original = WordingCandidate(
        "original:v28", polish_target.id, 0, polish_target.source_text,
        "preserve", "原文", 1.0, "original", total_score=0.0,
    )
    alternative = replace(
        original,
        id="alternative:v28",
        ordinal=1,
        text="位置付ける",
        relation_type="surface",
        source_kind="llm_jp",
        total_score=0.05,
    )
    ManuscriptReasoningRepository(case.connection).save_polish_candidates(
        (original, alternative),
        run_id=case.run_id,
        target_id=polish_target.id,
        model_identity="local-test-fixture",
        expected_candidate_count=3,
        issues=(),
    )

    context = case.worker._manuscript_select_polish_candidates(
        case.claimed, case.context
    ).context

    selection = context["manuscript_candidate_selection"]
    assert context["proposed_html"] == HTML
    assert selection["minimum_original_improvement"] == 0.12
    assert selection["original_score_guard_count"] == 1
    assert selection["selection_modes"] == {"mandatory_revision_unmet": 1}


@pytest.mark.parametrize("mode", [
    "candidate_generation_failed", "candidates_inappropriate",
    "human_selection_pending", "missing_original",
])
def test_actual_selection_preserves_granular_conditions_through_final_review_and_save(
    selection_case, mode
):
    case = selection_case
    polish_target = target()
    save_targets(case, [polish_target])
    save_candidates(case, polish_target, mode)

    context = case.worker._manuscript_select_polish_candidates(case.claimed, case.context).context

    assert context["proposed_html"] == HTML
    assert context["completion_status"] == "conditional_success"
    requirement = context["language_review_requirements"][0]
    assert requirement["target_id"] == polish_target.id
    assert requirement["term"] == polish_target.source_text
    assert requirement["source_sha256"] == hashlib.sha256(
        polish_target.source_text.encode()
    ).hexdigest()
    assert requirement["start_offset"] == polish_target.start_offset
    assert requirement["end_offset"] == polish_target.end_offset
    assert requirement["request_signature"] == polish_target.request_signature
    assert requirement["origin"] == "candidate_selection"
    assert requirement["source_preserved"] is True
    assert requirement["disposition"] == "source_preserved"
    assert requirement["review_class"] == "minor_lexical_source_preserved"
    expected_mode = "candidate_generation_failed" if mode == "missing_original" else mode
    assert requirement["status"] == expected_mode

    # A remote content judgement is not evidence that the application applied a candidate.
    case.worker._language_plan = lambda *args: [{
        "block_id": "P", "kind": "contextual_lexicon", "term": polish_target.source_text,
        "status": "no_revision_needed", "explanation": "問題を解消したと判断した。",
    }]
    final = case.worker._manuscript_validate_terms(case.claimed, context).context
    assert final["language_review_requirements"] == [requirement]
    assert final["completion_status"] == "conditional_success"
    proposal = case.service.persist_proposal(
        run_id=case.run_id, document_id=case.document_id, project_id=case.project["id"],
        base_revision_id=case.base.revision_id, outline_node_id=None, instructions="語彙を確認",
        use_rag=False, context=final, source_html=HTML, proposed_html=final["proposed_html"],
        edit_summary={}, quality_status="needs_human_review",
    )
    assert proposal["context"]["completion_status"] == "conditional_success"
    assert proposal["context"]["language_review_requirements"] == [requirement]


def test_contextual_lexical_uncertainty_preserves_source_without_human_semantic_review(
    selection_case,
):
    case = selection_case
    polish_target = target(unresolved=True)
    save_targets(case, [polish_target])
    save_candidates(case, polish_target, "independent_auto")

    context = case.worker._manuscript_select_polish_candidates(case.claimed, case.context).context

    requirement = context["language_review_requirements"][0]
    assert requirement["status"] == "semantic_analysis_unresolved"
    assert requirement["semantic_uncertainty"] is True
    assert requirement["review_class"] == "minor_lexical_source_preserved"
    assert requirement["disposition"] == "source_preserved"
    assert requirement["final_revision_blocked"] is False
    assert requirement["source_preserved"] is True


def test_mandatory_revision_unmet_is_not_counted_as_minor_source_preservation(
    selection_case,
):
    case = selection_case
    polish_target = target()
    save_targets(case, [polish_target])
    save_candidates(case, polish_target, "mandatory_revision_unmet")

    context = case.worker._manuscript_select_polish_candidates(
        case.claimed, case.context
    ).context

    requirement = context["language_review_requirements"][0]
    assert requirement["status"] == "mandatory_revision_unmet"
    assert requirement["review_class"] == "mandatory_revision_unmet"
    assert requirement["disposition"] == "mandatory_revision_required"
    assert requirement["final_revision_blocked"] is True
    assert requirement["source_preserved"] is True


def test_actual_application_resolves_only_same_target_and_preserves_other_conditions(
    selection_case,
):
    case = selection_case
    first = target()
    second = target("second", "取り扱う", 6)
    save_targets(case, [first, second])
    for item in (first, second):
        save_candidates(case, item, "candidate_generation_failed")
    context = case.worker._manuscript_select_polish_candidates(case.claimed, case.context).context
    before_ids = [item["requirement_id"] for item in context["language_review_requirements"]]
    replay = case.worker._manuscript_select_polish_candidates(case.claimed, context).context
    assert [item["requirement_id"] for item in replay["language_review_requirements"]] == before_ids

    save_candidates(case, first, "independent_auto")
    updated = case.worker._manuscript_select_polish_candidates(case.claimed, context).context
    assert "位置付ける" in updated["proposed_html"]
    assert "取り扱う" in updated["proposed_html"]
    assert updated["completion_status"] == "conditional_success"
    assert [item["target_id"] for item in updated["language_review_requirements"]] == [second.id]


def test_actual_application_can_clear_its_own_last_condition(selection_case):
    case = selection_case
    polish_target = target()
    save_targets(case, [polish_target])
    save_candidates(case, polish_target, "candidate_generation_failed")
    context = case.worker._manuscript_select_polish_candidates(case.claimed, case.context).context
    save_candidates(case, polish_target, "independent_auto")

    updated = case.worker._manuscript_select_polish_candidates(case.claimed, context).context

    assert "位置付ける" in updated["proposed_html"]
    assert updated["language_review_requirements"] == []
    assert updated["completion_status"] == "success"
    assert "conditional_success" not in updated


@pytest.mark.parametrize(
    "mode",
    ["candidate_generation_failed", "candidates_inappropriate"],
)
def test_nonlexical_polish_failure_is_source_preserved_conditional(selection_case, mode):
    case = selection_case
    first = target(issue_codes=("grammar.predicate_relation", "scope.phrase_only"))
    second = target(
        "second",
        "取り扱う",
        6,
        issue_codes=("surface.repetition", "scope.phrase_only"),
    )
    save_targets(case, [first, second])
    save_candidates(case, first, mode)
    save_candidates(case, second, mode)

    context = case.worker._manuscript_select_polish_candidates(
        case.claimed, case.context
    ).context

    requirements = context["language_review_requirements"]
    assert [item["target_id"] for item in requirements] == [first.id, second.id]
    assert {item["status"] for item in requirements} == {mode}
    assert {item["disposition"] for item in requirements} == {"source_preserved"}
    assert {item["review_class"] for item in requirements} == {
        "polish_candidate_source_preserved"
    }
    assert context["completion_status"] == "conditional_success"


def test_nonlexical_human_selection_pending_preserves_source(selection_case):
    case = selection_case
    polish_target = target(
        issue_codes=("grammar.predicate_relation", "scope.phrase_only")
    )
    save_targets(case, [polish_target])
    save_candidates(case, polish_target, "human_selection_pending")

    context = case.worker._manuscript_select_polish_candidates(
        case.claimed, case.context
    ).context

    requirement = context["language_review_requirements"][0]
    assert requirement["status"] == "human_selection_pending"
    assert requirement["review_class"] == "polish_candidate_source_preserved"
    assert requirement["disposition"] == "source_preserved"
    assert requirement["final_revision_blocked"] is False


def test_v21_actual_selection_does_not_adopt_v22_completion_policy(selection_case):
    case = selection_case
    polish_target = target()
    save_targets(case, [polish_target])
    save_candidates(case, polish_target, "candidate_generation_failed")
    case.claimed.workflow_version = "21"

    context = case.worker._manuscript_select_polish_candidates(case.claimed, case.context).context

    assert context["proposed_html"] == HTML
    assert "language_review_requirements" not in context
    assert "completion_status" not in context


def test_resolving_one_remote_term_does_not_clear_other_terms_or_generic_issue(selection_case):
    case = selection_case
    records = [{
        "block_id": "P", "kind": "contextual_lexicon", "term": term,
        "status": "semantic_analysis_unresolved", "explanation": "語義を特定できない。",
    } for term in ("位置づける", "取り扱う", "")]
    context = case.worker._language_condition_context_v22(
        case.claimed, {}, phase="plan", records=records
    )
    resolved = {**records[0], "status": "resolved"}

    updated = case.worker._language_condition_context_v22(
        case.claimed, context, phase="final", records=[resolved]
    )

    assert {item["term"] for item in updated["language_review_requirements"]} == {"取り扱う", ""}
    assert updated["completion_status"] == "conditional_success"


def test_resolving_one_causal_pair_does_not_clear_another(selection_case):
    case = selection_case
    records = [{
        "block_id": "P", "kind": "causal_relation", "term": "",
        "cause": cause, "effect": "読者経験", "status": "semantic_analysis_unresolved",
        "explanation": "因果の根拠が不足している。",
    } for cause in ("媒体編成", "流通経路")]
    context = case.worker._language_condition_context_v22(
        case.claimed, {}, phase="plan", records=records
    )

    updated = case.worker._language_condition_context_v22(
        case.claimed, context, phase="final", records=[{**records[0], "status": "resolved"}]
    )

    assert [item["cause"] for item in updated["language_review_requirements"]] == ["流通経路"]


@pytest.mark.parametrize("kind", ["term_introduction", "citation_function", "causal_relation"])
@pytest.mark.parametrize("status", ["semantic_analysis_unresolved", "human_selection_pending"])
def test_only_policy_semantic_kinds_and_statuses_require_human_judgement(
    selection_case, kind, status
):
    case = selection_case
    record = {
        "block_id": "P",
        "kind": kind,
        "term": "媒体レギュレーション" if kind == "term_introduction" else "",
        "cause": "媒体編成" if kind == "causal_relation" else "",
        "effect": "読者経験" if kind == "causal_relation" else "",
        "status": status,
        "explanation": "意味上の判断が未確定である。",
    }

    context = case.worker._language_condition_context_v22(
        case.claimed, {}, phase="final", records=[record]
    )

    requirement = context["language_review_requirements"][0]
    assert requirement["review_class"] == "human_semantic_judgement_required"
    assert requirement["disposition"] == "human_review_required"
    assert requirement["final_revision_blocked"] is True


def test_block_review_uncertainty_is_fail_closed_but_not_called_true_semantic(
    selection_case,
):
    case = selection_case
    context = case.worker._language_condition_context_v22(
        case.claimed,
        {},
        phase="final",
        records=[{
            "block_id": "P",
            "kind": "block_review",
            "status": "semantic_analysis_unresolved",
            "explanation": "型が不足している。",
        }],
    )

    requirement = context["language_review_requirements"][0]
    assert requirement["review_class"] == "unclassified_analysis_required"
    assert requirement["disposition"] == "analysis_classification_required"
    assert requirement["final_revision_blocked"] is True


def test_semantic_kind_with_mandatory_revision_unmet_remains_mandatory_repair(
    selection_case,
):
    case = selection_case
    context = case.worker._language_condition_context_v22(
        case.claimed,
        {},
        phase="final",
        records=[{
            "block_id": "P",
            "kind": "citation_function",
            "status": "mandatory_revision_unmet",
            "explanation": "引用目的の必須修正が本文へ反映されていない。",
        }],
    )

    requirement = context["language_review_requirements"][0]
    assert requirement["review_class"] == "mandatory_revision_unmet"
    assert requirement["disposition"] == "mandatory_revision_required"
    assert requirement["final_revision_blocked"] is True


def test_completion_gate_does_not_reblock_v22_source_preservation() -> None:
    minor = {
        "requirement_id": "minor-1",
        "status": "candidate_generation_failed",
        "review_class": "minor_lexical_source_preserved",
        "explanation": "安全な候補がないため原文を維持した。",
    }

    gate = WorkerRunner._manuscript_completion_gate({
        "workflow_version": 22,
        "proposed_html": HTML,
        "completion_status": "conditional_success",
        "language_review_requirements": [minor],
        "manuscript_candidate_selection": {
            "selection_modes": {"candidate_generation_failed": 1}
        },
    })

    codes = {item["code"] for item in gate["blocking_reasons"]}
    assert "polish.candidate_generation_failed" not in codes
    assert "discourse.candidate_generation_failed" not in codes


def test_completion_gate_keeps_v22_mandatory_revision_blocking() -> None:
    mandatory = {
        "requirement_id": "mandatory-1",
        "status": "mandatory_revision_unmet",
        "review_class": "mandatory_revision_unmet",
        "explanation": "既知の問題が原文に残っている。",
    }

    gate = WorkerRunner._manuscript_completion_gate({
        "workflow_version": 22,
        "proposed_html": HTML,
        "completion_status": "conditional_success",
        "language_review_requirements": [mandatory],
        "manuscript_candidate_selection": {
            "selection_modes": {"mandatory_revision_unmet": 1}
        },
    })

    codes = {item["code"] for item in gate["blocking_reasons"]}
    assert "polish.mandatory_revision_unmet" in codes
    assert "discourse.mandatory_revision_unmet" in codes
