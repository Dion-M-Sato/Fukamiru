from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from fukamiru.authoring import concept_fit as fit
from fukamiru.authoring.codex import (
    STAGE_GUIDANCE,
    STAGE_SCHEMAS,
    _validate_codex_output_schema,
)
from fukamiru.errors import NonRetryableFukamiruError
from fukamiru.worker.runner import WorkerRunner

BAD = "選定標本に限れば、異なる作品を比較できる。ハビトゥスは経験から形成される性向である。"
GOOD = "異なる作品を同じ誌面で比較できる。ハビトゥスは経験から形成される性向である。"
MATERIAL = {"propositions": [
    {"id": "p1", "text": "草稿には14作品のうち6作品を採用した。"},
    {"id": "p2", "text": "異なる作品を同じ誌面で読めることが力となる。"},
    {"id": "p3", "text": "ハビトゥスは経験から形成される性向を指す。"},
]}


@pytest.mark.parametrize("key", ["finding_id", "obligation_id"])
def test_review_accepts_supplied_evidence_identity_but_rejects_invented_id(key):
    payload = {"units": [{"unit_id": "/text", "text": "外部的事実性"}],
               "material": {"evidence": {"coverage": [{key: "evidence:known"}]}}}
    result = {"units": [{"unit_id": "/text", "concepts": [{
        "term": "外部的事実性", "basis_ids": ["evidence:known"],
        "verdict": "appropriate", "must_keep": False, "reason": "提示された確認項目。",
    }]}], "omissions": []}
    fit.validate_review(payload, result)
    result["units"][0]["concepts"][0]["basis_ids"] = ["evidence:invented"]
    with pytest.raises(NonRetryableFukamiruError, match="invented a basis ID"):
        fit.validate_review(payload, result)


@pytest.mark.parametrize("text", [
    "p0337の作家史上の時期と地位上昇は外部根拠が未解決であるため、この主張の根拠には含めない。",
    "個別作品の掲載期間、媒体、受容、成功の程度はこの比較命題では確定せず、作品名を比較候補として扱う。",
    "作品の精読による確認ができていない。",
])
def test_removed_non_theory_verification_status_is_saved_once(text):
    before = {"nodes": [{"id": "n1", "claim_id": "c1", "text": text}]}
    after = {"nodes": [{"id": "n1", "claim_id": "c1", "text": "作品の配置を比較する。"}]}
    context = {}
    items = fit.route_removed_verification(context, before, after)
    assert len(items) == 1
    assert items[0]["reason"] == text and items[0]["sentence_id"] == "c1"
    assert items[0]["blocks_render"] is False
    assert fit.route_removed_verification(context, before, after) == []
    assert fit.route_removed_verification({}, before, before) == []


def test_unresolved_subject_matter_is_not_a_verification_task():
    before = {"text": "作品は社会の未解決の問題を描く。"}
    assert fit.route_removed_verification({}, before, {"text": "作品は社会問題を描く。"}) == []


def verdict(payload, bad="unsupported_premise"):
    rows = []
    for unit in payload["units"]:
        concepts = []
        for term, status, ids, keep in (
            ("選定標本", bad, ["p1"], False),
            ("ハビトゥス", "appropriate", ["p3"], True),
        ):
            if term in unit["text"]:
                concepts.append({
                    "term": term, "meaning": "対象を規定する概念",
                    "application_basis": "元の記述と照合した。",
                    "argument_function": "対象を説明する。", "basis_ids": ids,
                    "verdict": status, "must_keep": keep,
                    "reason": "編集上の採用を研究上の標本と読めない。" if not keep
                    else "定義された性向を説明するために必要。",
                })
        rows.append({"unit_id": unit["unit_id"], "concepts": concepts})
    return {"units": rows, "omissions": []}


class Worker:
    def __init__(self, tmp_path, repaired=GOOD, bad="unsupported_premise"):
        self.settings = SimpleNamespace(paths=SimpleNamespace(runtime_root=tmp_path))
        self.calls = []
        self.repaired = repaired
        self.bad = bad

    def _register_artifact(self, *args, **kwargs):
        return "artifact"

    def _authoring_invoke_raw(self, claimed, **kwargs):
        self.calls.append(kwargs)
        if kwargs["stage"] == "review_concept_fit_v32":
            return verdict(kwargs["payload"], self.bad)
        return {"edits": [{
            "unit_id": row["unit_id"], "operation": "replace", "text": self.repaired,
        } for row in kwargs["payload"]["candidate_units"] if "選定標本" in row["text"]]}


def test_final_audit_excludes_note_labels_but_keeps_real_numbers_and_concepts(tmp_path):
    from fukamiru.revisions.prose_redraft import digest

    html = ('<article id="D" lang="ja"><section id="S"><h2 id="H">比較</h2>'
            '<p id="P">『1980年の第10巻<sup><a role="doc-noteref" href="#H">99</a>'
            '</sup>』を読む。' + BAD + '</p></section></article>')
    worker = Worker(tmp_path)
    context = {**deepcopy(MATERIAL), "proposed_html": html,
               "frozen_candidate_v26": {"html_sha256": digest(html)},
               "prose_redraft_report_v31": {"validated": True}}
    result = WorkerRunner._manuscript_audit_final_findings_v26(
        worker, SimpleNamespace(run_id="run", workflow_version="32"), context,
    ).context
    sent = worker.calls[0]["payload"]["units"][0]["text"]
    assert "『1980年の第10巻』" in sent and "99" not in sent
    assert result["proposed_html"] == html
    audit = result["concept_fit_final_audit_v32"]
    assert audit["source_sha256"] == digest(html)
    assert audit["status"] == "requires_review"
    assert audit["findings"][0]["term"] == "選定標本"
    assert fit.protected_terms(audit["reports"][0]["initial_review"]) == {"ハビトゥス"}


@pytest.mark.parametrize("stage", sorted(fit.PLAN_STAGES))
def test_plan_nodes_repair_premise_and_recheck_necessary_concept(tmp_path, stage):
    worker = Worker(tmp_path)
    context = deepcopy(MATERIAL)
    repaired = fit.guard(
        worker, SimpleNamespace(run_id="run"), stage=stage, context=context,
        payload=MATERIAL, candidate={"text": BAD}, operation=stage,
    )
    assert repaired["text"] == GOOD
    assert len(worker.calls) == 3
    assert worker.calls[1]["payload"]["concept_fit_repair"]["must_keep_terms"] == ["ハビトゥス"]
    assert worker.calls[2]["payload"]["previous_review"]
    assert context["concept_fit_reports_v32"][0]["status"] == "repaired"


@pytest.mark.parametrize("stage", sorted(fit.GENERATION_STAGES))
def test_all_generation_nodes_preserve_appropriate_specialist_term(tmp_path, stage):
    worker = Worker(tmp_path)
    source = {"text": GOOD}
    assert fit.guard(worker, SimpleNamespace(run_id="run"), stage=stage,
                     context=deepcopy(MATERIAL), payload=MATERIAL,
                     candidate=source, operation=stage) == source
    assert len(worker.calls) == 1


@pytest.mark.parametrize("stage", sorted(fit.GENERATION_STAGES - fit.PLAN_STAGES))
def test_prose_nodes_request_upstream_repair_instead_of_hiding_bad_premise(tmp_path, stage):
    worker = Worker(tmp_path)
    context = deepcopy(MATERIAL)
    with pytest.raises(NonRetryableFukamiruError, match="upstream"):
        fit.guard(worker, SimpleNamespace(run_id="run"), stage=stage, context=context,
                  payload=MATERIAL, candidate={"text": BAD}, operation=stage)
    assert len(worker.calls) == 1
    assert context["concept_fit_reports_v32"][0]["status"] == "plan_revision_required"


def test_a_synonym_does_not_resolve_an_unsupported_premise(tmp_path):
    worker = Worker(tmp_path, repaired=BAD)
    context = deepcopy(MATERIAL)
    with pytest.raises(NonRetryableFukamiruError, match="unresolved"):
        fit.guard(worker, SimpleNamespace(run_id="run"), stage="claim_tree", context=context,
                  payload=MATERIAL, candidate={"text": BAD}, operation="tree")
    assert len(worker.calls) == 2  # Never rejudge an unchanged rejected candidate into success.
    assert context["concept_fit_reports_v32"][0]["accepted_sha256"] is None


def test_repair_cannot_remove_a_necessary_concept(tmp_path):
    worker = Worker(tmp_path, repaired="異なる作品を同じ誌面で比較できる。")
    with pytest.raises(NonRetryableFukamiruError, match="lost required concepts"):
        fit.guard(worker, SimpleNamespace(run_id="run"), stage="claim_tree",
                  context=deepcopy(MATERIAL), payload=MATERIAL,
                  candidate={"text": BAD}, operation="tree")


def test_repairs_change_text_only_and_remove_only_optional_string_items():
    candidate = {"claims": [{"id": "c1", "parent_id": None, "text": BAD}],
                 "sections": [{"required_terms": ["選定標本", "ハビトゥス"]}]}
    result = fit.apply_repairs(candidate, [
        {"unit_id": "/claims/0/text", "operation": "replace", "text": GOOD},
        {"unit_id": "/sections/0/required_terms/0", "operation": "remove", "text": ""},
    ])
    assert result["claims"][0]["id"] == "c1"
    assert result["claims"][0]["parent_id"] is None
    assert result["sections"][0]["required_terms"] == ["ハビトゥス"]
    assert candidate["claims"][0]["text"] == BAD
    with pytest.raises(NonRetryableFukamiruError):
        fit.apply_repairs(candidate, [{"unit_id": "/claims/0/id", "operation": "replace",
                                       "text": "changed"}])
    with pytest.raises(NonRetryableFukamiruError):
        fit.apply_repairs(candidate, [{"unit_id": "/claims/0/text", "operation": "remove",
                                       "text": ""}])


def test_omissions_are_bound_to_source_and_cannot_invent_absent_terms():
    payload = {"units": fit.units({"text": "趣味を判断する。"}), "material": MATERIAL,
               "source_units": fit.units({"text": GOOD})}
    result = {"units": [{"unit_id": "/text", "concepts": []}], "omissions": [{
        "term": "ハビトゥス", "source_unit_id": "/text", "reason": "必要な専門概念を落とした。",
    }]}
    fit.validate_review(payload, result)
    assert fit.findings(result)[0]["missing_required_term"]
    result["omissions"][0]["term"] = "原文にない語"
    with pytest.raises(NonRetryableFukamiruError, match="omitted"):
        fit.validate_review(payload, result)


@pytest.mark.parametrize("stage", ["diagnose_japanese_prose", "review_japanese_sections",
                                  "audit_final_findings"])
def test_diagnosis_and_independent_reviews_bind_findings_to_real_paragraph(tmp_path, stage):
    result = fit.check_paragraphs(
        Worker(tmp_path), SimpleNamespace(run_id="run"), deepcopy(MATERIAL),
        paragraphs=[{"block_id": "P1", "text": BAD}], stage=stage, operation=stage,
    )
    assert result["status"] == "requires_review"
    assert result["findings"][0]["paragraph_id"] == "P1"
    assert fit.protected_terms(result["initial_review"]) == {"ハビトゥス"}
    good = fit.check_paragraphs(
        Worker(tmp_path), SimpleNamespace(run_id="run"), deepcopy(MATERIAL),
        paragraphs=[{"block_id": "P1", "text": GOOD}], stage=stage, operation=stage + "-good",
    )
    assert good["status"] == "passed" and not good["findings"]
    assert fit.protected_terms(good["initial_review"]) == {"ハビトゥス"}


@pytest.mark.parametrize("stage", sorted(fit.GENERATION_STAGES - fit.PLAN_STAGES))
def test_wording_repairs_are_local_and_rechecked_in_both_prose_nodes(tmp_path, stage):
    worker = Worker(tmp_path, bad="wording")
    result = fit.guard(worker, SimpleNamespace(run_id="run"), stage=stage,
                       context=deepcopy(MATERIAL), payload=MATERIAL,
                       candidate={"text": BAD}, operation=stage)
    assert result == {"text": GOOD}
    assert worker.calls[1]["stage"] == "repair_concept_fit_v32"
    assert len(worker.calls) == 3


@pytest.mark.parametrize("stage", sorted(fit.GENERATION_STAGES))
def test_actual_worker_dispatches_review_after_generation(tmp_path, stage, monkeypatch):
    class RoutedWorker(Worker):
        _authoring_invoke = WorkerRunner._authoring_invoke

        def _authoring_invoke_raw(self, claimed, **kwargs):
            if kwargs["stage"] == stage:
                return {"text": GOOD}
            return super()._authoring_invoke_raw(claimed, **kwargs)

    worker = RoutedWorker(tmp_path)
    validated = []
    monkeypatch.setattr("fukamiru.authoring.codex.CodexAuthoringAdapter.validate_result",
                        lambda stage, result: validated.append(stage))
    worker._authoring_validate_checkpoint_result = lambda *args: {}
    result = worker._authoring_invoke(
        SimpleNamespace(run_id="run", workflow_version="32"), stage=stage,
        context=deepcopy(MATERIAL), payload=MATERIAL, operation_name=stage,
    )
    assert result == {"text": GOOD}
    assert worker.calls[0]["stage"] == "review_concept_fit_v32"
    assert validated == [stage]


@pytest.mark.parametrize("change", ["missing_field", "invented_term", "invented_basis", "bad_keep"])
def test_semantic_audit_rejects_bad_identity_before_checkpoint(change):
    payload = {"units": fit.units({"text": BAD}), "material": MATERIAL}
    result = verdict(payload)
    if change == "missing_field":
        result["units"] = []
    elif change == "invented_term":
        result["units"][0]["concepts"][0]["term"] = "架空の語"
    elif change == "invented_basis":
        result["units"][0]["concepts"][0]["basis_ids"] = ["missing"]
    else:
        result["units"][0]["concepts"][0]["must_keep"] = True
    with pytest.raises(NonRetryableFukamiruError):
        WorkerRunner._authoring_validate_checkpoint_result(
            "review_concept_fit_v32", payload, result,
        )


@pytest.mark.parametrize("repeat_invalid", [False, True])
def test_basis_id_repair_names_exact_invalid_ids_without_relaxing_validation(
    tmp_path, repeat_invalid,
):
    worker = Worker(tmp_path)

    def invoke(_claimed, **kwargs):
        worker.calls.append(kwargs)
        payload = kwargs["payload"]
        result = verdict(payload)
        repair = payload.get("review_contract_repair")
        if repair:
            assert repair["invalid_basis_ids"] == [
                {"unit_id": "/text", "term": "ハビトゥス", "ids": ["p9"]},
            ]
            assert repair["allowed_basis_ids"] == ["p1", "p2", "p3"]
        if not repair or repeat_invalid:
            result["units"][0]["concepts"][0]["basis_ids"] = ["p9"]
        return result

    worker._authoring_invoke_raw = invoke
    if repeat_invalid:
        with pytest.raises(NonRetryableFukamiruError, match="invented a basis ID"):
            fit.review(worker, SimpleNamespace(run_id="r"), {}, stage="test",
                       candidate={"text": GOOD}, supplied=MATERIAL, operation="test")
    else:
        reviewed = fit.review(worker, SimpleNamespace(run_id="r"), {}, stage="test",
                              candidate={"text": GOOD}, supplied=MATERIAL, operation="test")
        assert fit.protected_terms(reviewed) == {"ハビトゥス"}
    assert len(worker.calls) == 2


def test_basis_diagnostics_do_not_change_other_contract_repair_fingerprints():
    repaired = fit._contract_repair_payload(
        {"material": MATERIAL}, "Concept review omitted an explicit concept candidate",
        {"units": []},
    )
    assert set(repaired["review_contract_repair"]) == {"error", "rejected_review", "instruction"}


def test_all_final_provider_schemas_are_strict_and_concept_policy_is_shared():
    for schema in STAGE_SCHEMAS.values():
        _validate_codex_output_schema(schema)
    for stage in fit.GENERATION_STAGES | {"japanese_diagnosis_v31", "japanese_section_review_v31"}:
        assert "概念を導入・採用" in STAGE_GUIDANCE[stage]
    assert "同義語への置換だけ" in STAGE_GUIDANCE["review_concept_fit_v32"]


def test_audit_material_keeps_original_span_instead_of_only_the_locked_contract():
    context = {**MATERIAL, "section_contracts": [{"required_terms": ["選定標本"]}],
               "note_text": "duplicate memo"}
    supplied = fit.material(context, {})
    assert supplied["propositions"][0]["text"].startswith("草稿")
    assert "note_text" not in supplied
    assert supplied["section_contracts"]


def test_original_spans_are_deduplicated_without_discarding_their_contents():
    original = {"propositions": [
        {"id": "p1", "text": "草稿では省略した。", "source_span": "作品の説明。要：草稿で省略。"},
        {"id": "p2", "text": "作品を比較する。", "source_span": "作品の説明。要：草稿で省略。"},
    ]}
    supplied = fit.material(original, {})
    assert len(supplied["source_spans"]) == 1
    assert supplied["source_spans"][0]["text"] == original["propositions"][0]["source_span"]
    assert supplied["propositions"][0]["source_span_ref"] == supplied["source_spans"][0]["id"]
    assert "source_span" in original["propositions"][0]


def test_review_contract_repair_is_bounded_and_saves_the_rejected_response(tmp_path):
    class FlakyWorker(Worker):
        def _authoring_invoke_raw(self, claimed, **kwargs):
            result = super()._authoring_invoke_raw(claimed, **kwargs)
            if len(self.calls) == 1:
                result["units"][0]["concepts"][0]["term"] = "原文にない語"
            return result

    worker = FlakyWorker(tmp_path)
    context = deepcopy(MATERIAL)
    result = fit.guard(worker, SimpleNamespace(run_id="run"), stage="claim_tree",
                       context=context, payload=MATERIAL, candidate={"text": BAD}, operation="tree")
    assert result == {"text": GOOD}
    assert worker.calls[1]["payload"]["review_contract_repair"]["rejected_review"]
    assert worker.calls[1]["payload"]["review_contract_repair"]["error"]
    assert context["concept_fit_reports_v32"][0]["status"] == "invalid_review"
    assert len(worker.calls) == 4


def test_review_contract_failure_never_retries_more_than_once(tmp_path):
    class InvalidWorker(Worker):
        def _authoring_invoke_raw(self, claimed, **kwargs):
            result = super()._authoring_invoke_raw(claimed, **kwargs)
            result["units"][0]["concepts"][0]["term"] = "原文にない語"
            return result

    worker = InvalidWorker(tmp_path)
    with pytest.raises(NonRetryableFukamiruError, match="excerpt"):
        fit.guard(worker, SimpleNamespace(run_id="run"), stage="claim_tree",
                  context=deepcopy(MATERIAL), payload=MATERIAL,
                  candidate={"claims": [{"text": BAD}, {"text": GOOD}]}, operation="tree")
    assert len(worker.calls) == 3
    assert [len(call["payload"]["units"]) for call in worker.calls] == [2, 2, 1]
    assert ":field-anchor-repair:" in worker.calls[-1]["operation_name"]


def test_isolated_field_repair_keeps_bad_premise_findings_and_other_specialist_verdicts(tmp_path):
    class FieldWorker(Worker):
        def _authoring_invoke_raw(self, claimed, **kwargs):
            result = super()._authoring_invoke_raw(claimed, **kwargs)
            if len(self.calls) <= 2:
                result["units"][0]["concepts"][0]["term"] = "別のフィールドの語"
            return result

    worker = FieldWorker(tmp_path)
    candidate = {"claims": [{"text": BAD}, {"text": GOOD}]}
    snapshot = deepcopy(candidate)
    context = deepcopy(MATERIAL)
    result = fit.review(worker, SimpleNamespace(run_id="run"), context, stage="claim_tree",
                        candidate=candidate, supplied=MATERIAL, operation="tree")
    assert candidate == snapshot
    assert len(worker.calls) == 3
    assert fit.findings(result)[0]["term"] == "選定標本"
    assert fit.findings(result)[0]["verdict"] == "unsupported_premise"
    assert fit.protected_terms(result) == {"ハビトゥス"}
    assert len(result["units"]) == 2
    assert context["concept_field_anchor_recoveries_v32"][0]["unit_ids"] == ["/claims/0/text"]
    assert worker.calls[-1]["payload"]["context_units"] == fit.units(candidate)


def test_field_anchor_fallback_refuses_wrong_coverage_and_large_failure_sets():
    candidate = {"claims": [{"text": BAD} for _ in range(9)]}
    payload = {"units": fit.units(candidate), "material": MATERIAL}
    rejected = verdict(payload)
    for row in rejected["units"]:
        row["concepts"][0]["term"] = "他の語"
    assert fit.field_anchor_errors(payload, rejected) == {}
    rejected["units"].pop()
    assert fit.field_anchor_errors(payload, rejected) == {}


def test_field_anchor_fallback_does_not_accept_invented_basis_ids():
    payload = {"units": fit.units({"claims": [{"text": BAD}, {"text": GOOD}]}),
               "material": MATERIAL}
    rejected = verdict(payload)
    rejected["units"][0]["concepts"][0]["term"] = "他の語"
    rejected["units"][1]["concepts"][0]["basis_ids"] = ["invented"]
    assert fit.field_anchor_errors(payload, rejected) == {}


def test_needed_specialist_concept_is_protected_when_its_explanation_needs_wording():
    payload = {"units": fit.units({"text": GOOD}), "material": MATERIAL}
    result = verdict(payload)
    concept = result["units"][0]["concepts"][0]
    assert concept["term"] == "ハビトゥス"
    concept["verdict"] = "wording"
    concept["reason"] = "概念名を保持して説明を明確にする。"
    fit.validate_review(payload, result)
    assert fit.protected_terms(result) == {"ハビトゥス"}


@pytest.mark.parametrize("text", [
    "場と感情の構造を分析に用いるが、適用根拠が確認されるまでは採用を確定できない。",
    "場と感情の構造を分析に用いるが、青年誌史への適用は実質的に検証されていない。",
    "場と感情の構造を分析に用いるが、原典の概念を確認したことにはならない。",
])
def test_removed_theory_verification_closure_is_saved_once_for_human_review(text):
    revised = "場の移動と感情の構造から、雑誌の編成を分析する。"
    before = {"claims": [{"id": "c4", "text": text}],
              "nodes": [{"id": "g4", "claim_id": "c4", "text": text}]}
    after = {"claims": [{"id": "c4", "text": revised}],
             "nodes": [{"id": "g4", "claim_id": "c4", "text": revised}]}
    context = {}
    routed = fit.route_removed_verification(context, before, after)
    assert len(routed) == 1
    assert routed[0]["reason"] == text
    assert routed[0]["sentence_id"] == "c4" and routed[0]["status"] == "open"
    assert routed[0]["blocks_render"] is False
    assert fit.route_removed_verification(context, before, after) == []
    assert len(context["human_review_items"]) == 1


def test_theory_treatment_keeps_attribution_permission_separate_from_verification():
    context = {**MATERIAL, "authoring_atomic_evidence_contracts": [{
        "proposition_id": "p3", "verification_scope": "theoretical_attribution",
        "realization": "attribute_to_memo", "may_assert_as_external_fact": False,
        "visible_attribution_required": True, "visible_qualification_required": False,
        "source_span": "This duplicate is not needed in the review projection.",
    }]}
    row = fit.material(context, {})["theory_attribution_treatments"][0]
    assert row["proposition_id"] == "p3"
    assert row["may_assert_as_external_fact"] is False
    assert row["visible_qualification_required"] is False
    assert "source_span" not in row


def test_evidence_record_role_does_not_hide_the_text_or_reclassify_the_claim():
    text = "原典の適用内容は未確認である。"
    candidate = {"nodes": [
        {"id": "e1", "node_type": "evidence", "text": text},
        {"id": "c1", "node_type": "claim", "text": text},
    ]}
    assert fit.units(candidate) == [
        {"unit_id": "/nodes/0/text", "text": text, "field_role": "evidence_record"},
        {"unit_id": "/nodes/1/text", "text": text, "field_role": "claim"},
    ]


def test_internal_evidence_still_repairs_a_false_premise_and_preserves_its_identity(tmp_path):
    worker = Worker(tmp_path)
    candidate = {"nodes": [{"id": "e1", "node_type": "evidence", "text": BAD}]}
    result = fit.guard(worker, SimpleNamespace(run_id="run"), stage="argument_graph_v31",
                       context=deepcopy(MATERIAL), payload=MATERIAL,
                       candidate=candidate, operation="evidence")
    assert result == {"nodes": [{"id": "e1", "node_type": "evidence", "text": GOOD}]}
    assert "内部の根拠カード" in worker.calls[0]["payload"]["unit_role_policy"]
    assert len(worker.calls) == 3


def test_reader_prose_review_does_not_receive_internal_evidence_permission(tmp_path):
    worker = Worker(tmp_path)
    fit.guard(worker, SimpleNamespace(run_id="run"), stage="compose_body_prose_v31",
              context=deepcopy(MATERIAL), payload=MATERIAL,
              candidate={"paragraphs": [{"text": GOOD}]}, operation="body")
    assert "unit_role_policy" not in worker.calls[0]["payload"]


def test_adoption_reason_is_inspected_for_an_unsupported_method_premise(tmp_path):
    worker = Worker(tmp_path)
    candidate = {"claim_decisions": [{"claim_id": "c1", "reason": BAD}]}
    result = fit.guard(worker, SimpleNamespace(run_id="run"), stage="argument_graph_v31",
                       context=deepcopy(MATERIAL), payload=MATERIAL,
                       candidate=candidate, operation="reason")
    assert result["claim_decisions"][0]["reason"] == GOOD
    assert result["claim_decisions"][0]["claim_id"] == "c1"
    unit = worker.calls[0]["payload"]["units"][0]
    assert unit["field_role"] == "internal_decision_reason"
    assert "内部の採否理由" in worker.calls[0]["payload"]["unit_role_policy"]


def test_research_gap_questions_do_not_gain_methodological_authority():
    guidance = STAGE_GUIDANCE["review_concept_fit_v32"]
    assert "研究方法・分析対象・検証義務を新設" in guidance
    assert "疑問文や今後の確認事項にしただけ" in guidance
    assert "元の資料が明示した調査設計・対象条件による限定は保持" in guidance


def test_quoted_candidates_require_individual_inspection_without_automatic_protection():
    candidate = {"text": "「ハビトゥス」と「選定標本」を用いる。"}
    exposed = fit.units(candidate)
    assert exposed[0]["concept_candidates"] == ["ハビトゥス", "選定標本"]
    payload = {"units": exposed, "material": MATERIAL}
    reviewed = verdict(payload)
    fit.validate_review(payload, reviewed)
    assert fit.protected_terms(reviewed) == {"ハビトゥス"}
    reviewed["units"][0]["concepts"].pop()
    with pytest.raises(NonRetryableFukamiruError, match="explicit concept candidate"):
        fit.validate_review(payload, reviewed)


def test_a_bundle_verdict_cannot_replace_the_individual_concept_candidates():
    exposed = fit.units({"text": "「場」と「感情の構造」は分析枠となる。"})
    payload = {"units": exposed, "material": MATERIAL}
    bundled = {"units": [{"unit_id": "/text", "concepts": [{
        "term": "分析枠", "meaning": "理論的な枠組み", "application_basis": "提供メモ",
        "argument_function": "対象を読む", "basis_ids": [], "verdict": "appropriate",
        "must_keep": False, "reason": "まとめた枠組みとして適切。",
    }]}], "omissions": []}
    with pytest.raises(NonRetryableFukamiruError, match="explicit concept candidate"):
        fit.validate_review(payload, bundled)


@pytest.mark.parametrize("stage", sorted(fit.PLAN_STAGES))
def test_quoted_candidate_is_reviewed_without_forcing_an_invalid_term_to_survive(tmp_path, stage):
    worker = Worker(tmp_path)
    source = {"text": "「選定標本」を用いる。「ハビトゥス」は経験から形成される性向である。"}
    result = fit.guard(worker, SimpleNamespace(run_id="run"), stage=stage,
                       context=deepcopy(MATERIAL), payload=MATERIAL,
                       candidate=source, operation="quoted")
    assert result == {"text": GOOD}
    assert worker.calls[0]["payload"]["units"][0]["concept_candidates"] == [
        "選定標本", "ハビトゥス",
    ]
    assert worker.calls[1]["payload"]["concept_fit_repair"]["must_keep_terms"] == ["ハビトゥス"]


class RegressionWorker(Worker):
    def __init__(self, tmp_path, *, final=GOOD):
        super().__init__(tmp_path)
        self.repairs = 0
        self.final = final

    def _authoring_invoke_raw(self, claimed, **kwargs):
        self.calls.append(kwargs)
        if kwargs["stage"] == "repair_concept_fit_v32":
            self.repairs += 1
            return {"edits": [{"unit_id": "/text", "operation": "replace", "text":
                ("当初から" + GOOD if self.repairs == 1 else self.final)}]}
        result = verdict(kwargs["payload"])
        for source, row in zip(kwargs["payload"]["units"], result["units"], strict=True):
            for term in ("当初から", "全作品で"):
                if term in source["text"]:
                    row["concepts"].append({
                        "term": term, "meaning": "追加された歴史的・全体的な前提",
                        "application_basis": "p2は共存を示すだけである。",
                        "argument_function": "対象範囲を拡大する。", "basis_ids": ["p2"],
                        "verdict": "unsupported_premise", "must_keep": False,
                        "reason": "元資料にない前提を修復で追加した。",
                    })
        return result


def test_plan_repairs_a_newly_introduced_premise_and_independently_rechecks_twice(tmp_path):
    worker = RegressionWorker(tmp_path)
    context = deepcopy(MATERIAL)
    result = fit.guard(worker, SimpleNamespace(run_id="run"), stage="argument_graph_v31",
                       context=context, payload=MATERIAL, candidate={"text": BAD},
                       operation="graph")
    assert result == {"text": GOOD}
    assert len(worker.calls) == 5
    assert worker.calls[3]["operation_name"] == "graph:concept-repair:2"
    assert worker.calls[3]["payload"]["previous_failed_repair"]
    report = json.loads(Path(context["concept_fit_reports_v32"][-1]["path"]).read_text(
        encoding="utf-8"
    ))
    assert len(report["repairs"]) == len(report["repair_reviews"]) == 2
    assert not fit.findings(report["final_review"])
    assert "ハビトゥス" in report["repairs"][0]["text"]


@pytest.mark.parametrize("final", [BAD, "全作品で" + GOOD])
def test_plan_repair_is_bounded_and_never_returns_a_bad_candidate(tmp_path, final):
    worker = RegressionWorker(tmp_path, final=final)
    context = deepcopy(MATERIAL)
    with pytest.raises(NonRetryableFukamiruError, match="remains unresolved"):
        fit.guard(worker, SimpleNamespace(run_id="run"), stage="argument_graph_v31",
                  context=context, payload=MATERIAL, candidate={"text": BAD}, operation="graph")
    assert worker.repairs == 2
    assert len(worker.calls) == (4 if final == BAD else 5)
    assert context["concept_fit_reports_v32"][-1]["accepted_sha256"] is None


def test_replayed_report_preserves_the_previous_failure_file(tmp_path):
    worker = Worker(tmp_path)
    context = {}
    report = {"policy": fit.POLICY, "stage": "claim_tree", "status": "unresolved",
              "candidate_sha256": fit.digest({"text": BAD}), "accepted_sha256": None}
    fit.save_report(worker, SimpleNamespace(run_id="run"), context, "tree", report)
    original = Path(context["concept_fit_reports_v32"][-1]["path"])
    original_bytes = original.read_bytes()
    fit.save_report(worker, SimpleNamespace(run_id="run"), context, "tree", {
        **report, "status": "repaired", "accepted_sha256": fit.digest({"text": GOOD}),
    })
    revised = Path(context["concept_fit_reports_v32"][-1]["path"])
    assert revised != original and original.read_bytes() == original_bytes
    assert json.loads(revised.read_text(encoding="utf-8"))["status"] == "repaired"


def test_unit_identity_and_order_survive_canonical_checkpoint_serialization():
    candidate = {"nodes": [{"text": "「ハビトゥス」を保持する。", "node_type": "warrant"}],
                 "claim_scopes": [{"proposition": "作品を比較する。"}],
                 "claim_decisions": [{"reason": "資料に基づく。", "adopted_text": "作品を読む。"}]}
    serialized = json.loads(json.dumps(candidate, ensure_ascii=False, sort_keys=True))
    assert fit.units(candidate) == fit.units(serialized)


@pytest.mark.parametrize("cache_hit", [False, True])
def test_rejected_review_seed_only_reuses_an_exact_accepted_checkpoint(tmp_path, cache_hit):
    worker = Worker(tmp_path)
    candidate = {"text": BAD}
    payload = {"units": fit.units(candidate), "material": MATERIAL}
    rejected = verdict(payload)
    rejected["units"][0]["concepts"][0]["term"] = "原文にない語"
    fit.save_report(worker, SimpleNamespace(run_id="run"), {}, "tree:invalid:0", {
        "policy": fit.POLICY, "stage": "claim_tree", "status": "invalid_review",
        "candidate_sha256": fit.digest(candidate), "accepted_sha256": None,
        "initial_review": rejected, "error": "Concept review excerpt is not in its field",
    })
    def load(_claimed, **kwargs):
        assert kwargs["operation_name"] == "tree:contract-repair"
        assert kwargs["payload"]["review_contract_repair"]["rejected_review"] == rejected
        return verdict(kwargs["payload"]) if cache_hit else None

    worker._authoring_load_concept_review_checkpoint = load
    context = {}
    reviewed = fit.review(worker, SimpleNamespace(run_id="run"), context, stage="claim_tree",
                          candidate=candidate, supplied=MATERIAL, operation="tree")
    assert fit.findings(reviewed)[0]["verdict"] == "unsupported_premise"
    assert len(worker.calls) == (0 if cache_hit else 1)
    assert bool(context.get("concept_review_recovery_seeds_v32")) == cache_hit


@pytest.mark.parametrize("problem", ["premise", "claim", "unknown", "term", "protected"])
def test_evidence_role_replay_never_bypasses_an_actual_concept_failure(problem):
    from fukamiru.storage.runs import _v32_internal_evidence_review_is_repairable

    candidate = {"nodes": [{"node_type": "evidence", "text": "適用内容は未検証。"}]}
    finding = {"unit_id": "/nodes/0/text", "term": "未検証",
               "verdict": "wording", "must_keep": False}
    if problem == "premise":
        finding["verdict"] = "unsupported_premise"
    elif problem == "claim":
        candidate["nodes"][0]["node_type"] = "claim"
    elif problem == "unknown":
        finding["unit_id"] = "/nodes/4/text"
    elif problem == "term":
        finding["term"] = "候補にない語"
    else:
        finding["must_keep"] = True
    import json

    assert not _v32_internal_evidence_review_is_repairable(
        "Concept repair remains unresolved: " + json.dumps([finding]), candidate,
    )
