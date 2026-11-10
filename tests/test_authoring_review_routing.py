from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from fukamiru.authoring.evidence_contracts import (
    AuditFindingV31,
    EvidenceUse,
    HumanReviewItem,
    adapt_audit_finding,
    assert_repair_input,
    build_fixed_assets,
    canonical_hash,
    classify_findings,
    compile_draft_evidence,
    draft_source_use_constraints,
    draft_support_anchor_problems,
    project_argument_evidence_sources,
    result_status,
    synchronize_evidence_uses,
    validate_repair_history,
)
from fukamiru.errors import FukamiruError
from fukamiru.worker.runner import WorkerRunner


def test_mixed_findings_are_partitioned_without_human_review_entering_repair():
    digest = canonical_hash("本文")
    findings = [
        adapt_audit_finding(
            {
                "id": "AF-1",
                "code": "redundancy",
                "severity": "major",
                "object_id": "S014",
                "message": "重複文を削除する",
                "repair_scope": "local_section",
            },
            manuscript_hash=digest,
            ordinal=1,
        ),
        adapt_audit_finding(
            {
                "id": "AF-2",
                "code": "over_attribution",
                "severity": "major",
                "object_id": "S005",
                "message": "RAG断片だけでは原典への強い帰属を確定できない",
                "repair_scope": "user_input",
                "source_ids": ["SRC-014"],
            },
            manuscript_hash=digest,
            ordinal=2,
        ),
        adapt_audit_finding(
            {
                "id": "AF-3",
                "code": "unknown_source",
                "severity": "critical",
                "object_id": "S015",
                "message": "存在しないsource IDを参照した",
                "repair_scope": "none",
            },
            manuscript_hash=digest,
            ordinal=3,
        ),
    ]

    classified = classify_findings(findings)

    assert [item.finding_id for item in classified.auto_repair] == ["AF-1"]
    assert [item.finding_id for item in classified.human_review] == ["AF-2"]
    assert [item.finding_id for item in classified.blocking] == ["AF-3"]
    assert classified.review_items[0].blocks_render is False
    assert_repair_input(list(classified.auto_repair))
    with pytest.raises(FukamiruError, match="auto_repair"):
        assert_repair_input(list(classified.human_review))


def test_v31_semantic_major_findings_go_to_review_even_if_old_audit_says_auto():
    digest = canonical_hash("本文")
    for category in (
        "source_review_note_leak", "over_attribution", "evidence_mismatch",
        "audit_meta_prose", "claim_scope_overreach",
    ):
        finding = adapt_audit_finding(
            {
                "finding_id": f"AF-{category}", "section_id": "sec-body",
                "category": category, "severity": "major",
                "description": "文章の意味と射程を人間が確認する",
                "disposition": "auto_repair",
            },
            manuscript_hash=digest, ordinal=1,
        )
        assert finding.disposition == "human_review"
        assert finding.repair_operations == ()
        assert classify_findings([finding]).review_items[0].blocks_render is False

    missing = adapt_audit_finding(
        {"category": "unknown_source", "severity": "critical",
         "description": "unknown_source ID", "section_id": "sec-body"},
        manuscript_hash=digest, ordinal=2,
    )
    assert missing.disposition == "blocking"

    local = adapt_audit_finding(
        {"category": "redundancy", "severity": "major",
         "description": "重複文を削る", "repair_scope": "local_section",
         "section_id": "sec-body"},
        manuscript_hash=digest, ordinal=3,
    )
    assert local.disposition == "auto_repair"


def test_generated_finding_id_is_stable_across_audit_order_changes():
    raw = {
        "code": "redundancy",
        "severity": "major",
        "object_id": "S014",
        "message": "重複文を削除する",
        "repair_scope": "local_section",
    }
    digest = canonical_hash("本文")

    first = adapt_audit_finding(raw, manuscript_hash=digest, ordinal=1)
    reordered = adapt_audit_finding(raw, manuscript_hash=digest, ordinal=99)

    assert first.finding_id == reordered.finding_id


def test_review_required_completes_with_review_but_blocker_fails():
    review = HumanReviewItem(
        review_item_id="HR-1",
        section_id="S005",
        category="source_attribution",
        reason="原典確認が必要",
        source_ids=("SRC-014",),
        recommended_check="ページを確認する",
    )
    blocker = adapt_audit_finding(
        {
            "id": "AF-BLOCK",
            "code": "missing_bibliography",
            "severity": "critical",
            "message": "脚注に対応する参考文献がない",
        },
        manuscript_hash=canonical_hash("本文"),
        ordinal=1,
    )

    assert result_status(review_items=[review], blocking_findings=[]) == (
        "completed_with_review"
    )
    assert result_status(review_items=[], blocking_findings=[blocker]) == "failed"


def test_evidence_sync_updates_ids_deletes_explicitly_removed_uses_and_rejects_stale_ids():
    uses = [
        EvidenceUse(
            evidence_use_id="EU-1",
            section_id="S005",
            claim_id="C-1",
            sentence_id="S005-P01-S01",
            source_ids=("SRC-014",),
            claim_role="attributed_position",
            evidence_kind="rag_excerpt",
            verification_status="review_required",
        ),
        EvidenceUse(
            evidence_use_id="EU-2",
            section_id="S005",
            claim_id="C-2",
            sentence_id="S005-P01-S02",
            source_ids=("SRC-014",),
            claim_role="contextual",
            evidence_kind="rag_excerpt",
            verification_status="review_required",
        ),
    ]

    synced = synchronize_evidence_uses(
        uses,
        current_sentence_ids={"S005-P02-S01"},
        sentence_id_map={"S005-P01-S01": "S005-P02-S01"},
        deleted_sentence_ids={"S005-P01-S02"},
        allowed_source_ids={"SRC-014"},
    )

    assert synced.evidence_uses[0].sentence_id == "S005-P02-S01"
    assert synced.removed_evidence_use_ids == ("EU-2",)
    with pytest.raises(FukamiruError, match="unmapped sentence"):
        synchronize_evidence_uses(
            uses[:1],
            current_sentence_ids={"S005-P02-S01"},
            allowed_source_ids={"SRC-014"},
        )


def test_repair_history_is_bounded_for_same_set_and_section_migration():
    digest = canonical_hash("本文")
    finding = adapt_audit_finding(
        {
            "id": "AF-1",
            "code": "redundancy",
            "severity": "major",
            "object_id": "S005",
            "message": "重複文を削除する",
            "repair_scope": "local_section",
        },
        manuscript_hash=digest,
        ordinal=1,
    )
    fingerprint = validate_repair_history([], [finding], maximum_attempts=2)
    history = [
        {"fingerprint": fingerprint, "finding_sections": {"AF-1": "S005"}},
        {"fingerprint": fingerprint, "finding_sections": {"AF-1": "S005"}},
    ]

    with pytest.raises(FukamiruError, match="same finding set"):
        validate_repair_history(history, [finding], maximum_attempts=2)

    moved = AuditFindingV31(
        **{
            **finding.model_dump(mode="python"),
            "section_id": "S018",
            "target_ids": ("S018",),
        }
    )
    migrating_history = [
        {"fingerprint": "a", "finding_sections": {"AF-1": "S005"}},
    ]
    with pytest.raises(FukamiruError, match="moved across sections"):
        validate_repair_history(migrating_history, [moved], maximum_attempts=3)


def test_compiled_evidence_use_identity_survives_treatment_reordering():
    fixed = build_fixed_assets({
        "evidence_context": [
            {
                "id": "SRC-1",
                "text": "根拠一",
                "source_span_id": "SPAN-1",
                "verification_status": "verified",
            },
            {
                "id": "SRC-2",
                "text": "根拠二",
                "source_span_id": "SPAN-2",
                "verification_status": "verified",
            },
        ],
    })
    context = {"fixed_assets": fixed.model_dump(mode="json")}
    treatments = [
        {
            "evidence_id": "SRC-1",
            "claim_id": "C-1",
            "prose_treatment": "externally_supported_fact",
            "support_sentence": "根拠一。",
        },
        {
            "evidence_id": "SRC-2",
            "claim_id": "C-2",
            "prose_treatment": "externally_supported_fact",
            "support_sentence": "根拠二。",
        },
    ]
    section = {
        "section_id": "S1",
        "paragraphs": [{
            "text": "根拠一。根拠二。",
            "evidence_ids": ["SRC-1", "SRC-2"],
        }],
        "evidence_treatment": treatments,
    }

    _, first, _, _ = compile_draft_evidence(context, [section])
    _, reordered, _, _ = compile_draft_evidence(
        context,
        [{**section, "evidence_treatment": list(reversed(treatments))}],
    )

    assert {item.claim_id: item.evidence_use_id for item in first} == {
        item.claim_id: item.evidence_use_id for item in reordered
    }


def test_paraphrased_support_hint_resolves_only_inside_its_evidence_paragraph():
    fixed = build_fixed_assets({
        "evidence_context": [
            {
                "id": "SRC-1",
                "text": "根拠一",
                "source_span_id": "SPAN-1",
                "verification_status": "verified",
            },
            {
                "id": "SRC-2",
                "text": "根拠二",
                "source_span_id": "SPAN-2",
                "verification_status": "verified",
            },
        ],
    })
    context = {"fixed_assets": fixed.model_dump(mode="json")}
    section = {
        "section_id": "S1",
        "paragraphs": [
            {"text": "別の根拠一。", "evidence_ids": ["SRC-2"]},
            {"text": "この段落は根拠二を論じる。", "evidence_ids": ["SRC-1"]},
        ],
        "evidence_treatment": [{
            "evidence_id": "SRC-1",
            "claim_id": "C-1",
            "prose_treatment": "externally_supported_fact",
            # This summary happens to match prose in an unrelated paragraph.
            "support_sentence": "別の根拠一。",
        }],
    }

    _, uses, _, _ = compile_draft_evidence(context, [section])

    assert len(uses) == 1
    assert uses[0].sentence_id == "S1-P02-S01"
    assert uses[0].supported_content == "この段落は根拠二を論じる。"


def test_multi_sentence_evidence_requires_an_unambiguous_support_location():
    fixed = build_fixed_assets({"evidence_context": [{
        "id": "SRC-1", "text": "確認済みの事実", "source_span_id": "SPAN-1",
        "verification_status": "verified",
    }]})
    context = {"fixed_assets": fixed.model_dump(mode="json")}
    section = {
        "section_id": "S1",
        "paragraphs": [{
            "text": "比較方法を述べる。確認済みの事実から結果を述べる。",
            "evidence_ids": ["SRC-1"],
        }],
        "evidence_treatment": [{
            "evidence_id": "SRC-1", "claim_id": "C-1",
            "prose_treatment": "externally_supported_fact",
            "support_sentence": "結果に関する記述",
        }],
    }
    assert draft_support_anchor_problems([section])
    with pytest.raises(FukamiruError, match="ambiguous sentence-level support anchor"):
        compile_draft_evidence(context, [section])

    section["evidence_treatment"][0].update({
        "support_paragraph_index": 1,
        "support_sentence_index": 2,
    })
    assert not draft_support_anchor_problems([section])
    _, uses, _, _ = compile_draft_evidence(context, [section])
    assert uses[0].sentence_id == "S1-P01-S02"
    assert uses[0].supported_content == "確認済みの事実から結果を述べる。"


def test_source_constraints_repair_memo_use_without_promoting_source():
    context = {"propositions": [{"id": "P1", "text": "著者による解釈。"}],
               "argument_nodes": [{"id": "E1", "node_type": "evidence",
                                   "provenance": {"source_ids": ["P1"]}}]}
    context["fixed_assets"] = build_fixed_assets(context).model_dump(mode="json")
    section = {"section_id": "S1", "paragraphs": [{"text": "著者は関係が変わったと考える。",
                "evidence_ids": ["E1"]}], "evidence_treatment": [{"evidence_id": "E1",
                "claim_id": "C1", "prose_treatment": "externally_supported_fact"}]}
    original = deepcopy(context)
    with pytest.raises(FukamiruError, match="does not support claim role source_fact"):
        compile_draft_evidence(context, [section])
    constraints = draft_source_use_constraints(context, section)
    permitted = {item["prose_treatment"]: item for item in constraints[0]["permitted_uses"]}
    assert "externally_supported_fact" not in permitted
    assert permitted["attributed_user_statement"]["claim_role"] == "memo_supported"
    assert permitted["qualified_interpretation"]["verification_status"] == {"P1": "review_required"}
    corrected = deepcopy(section)
    corrected["evidence_treatment"][0]["prose_treatment"] = "attributed_user_statement"
    assert draft_source_use_constraints(context, corrected) == []
    _, uses, _, _ = compile_draft_evidence(context, [corrected])
    assert uses[0].claim_role == "memo_supported"
    assert uses[0].verification_status == "review_required"
    assert context == original
    assert section["evidence_treatment"][0]["prose_treatment"] == "externally_supported_fact"


def test_source_constraints_offer_only_context_for_bibliographic_records():
    context = {"evidence_context": [{
        "id": "B1", "title": "書名", "verification_status": "verified",
    }]}
    context["fixed_assets"] = build_fixed_assets(context).model_dump(mode="json")
    constraints = draft_source_use_constraints(context, {"evidence_treatment": [{
        "evidence_id": "B1", "prose_treatment": "externally_supported_fact",
    }]})
    assert constraints[0]["permitted_uses"] == [{
        "prose_treatment": "attributed_scholarship", "claim_role": "contextual",
        "source_ids": ["B1"], "verification_status": {"B1": "verified"},
    }]


def test_draft_cannot_promote_bibliographic_record_to_source_fact():
    fixed = build_fixed_assets({
        "evidence_context": [{
            "id": "BIB-1",
            "title": "書誌情報だけの資料",
            "verification_status": "verified",
        }],
    })

    with pytest.raises(FukamiruError, match="does not support claim role source_fact"):
        compile_draft_evidence(
            {"fixed_assets": fixed.model_dump(mode="json")},
            [{
                "section_id": "S1",
                "paragraphs": [{"text": "事実である。", "evidence_ids": ["BIB-1"]}],
                "evidence_treatment": [{
                    "evidence_id": "BIB-1",
                    "claim_id": "C1",
                    "prose_treatment": "externally_supported_fact",
                    "support_sentence": "事実である。",
                }],
            }],
        )


def test_v31_reference_lineage_resolves_to_contextual_citation_without_promoting_it():
    citation_id = "annotation-citation:book"
    fixed = build_fixed_assets({
        "evidence_context": [{
            "id": citation_id,
            "title": "著作の書誌情報",
            "verification_status": "review_required",
        }],
        "propositions": [{"id": "p0134", "text": "元メモの命題"}],
    })
    context = {
        "fixed_assets": fixed.model_dump(mode="json"),
        "annotation_citation_evidence": [{
            "id": citation_id, "reference_id": "reference:book",
            "verification_scope": "bibliographic_identity_only",
        }],
        "argument_nodes": [{
            "id": "e1", "node_type": "evidence",
            "provenance": {"source_ids": ["reference:book", "p0134"]},
        }],
    }
    projection = project_argument_evidence_sources(context)["e1"]
    assert projection["lineage_source_ids"] == ("reference:book", "p0134")
    assert projection["lineage_only_source_ids"] == ("reference:book",)
    assert projection["citation_source_ids"] == (citation_id,)

    _, uses, _, _ = compile_draft_evidence(context, [{
        "section_id": "S1",
        "paragraphs": [{
            "text": "著者『著作』を比較枠として参照する。",
            "evidence_ids": ["e1"],
        }],
        "evidence_treatment": [{
            "evidence_id": "e1", "claim_id": "cl0001",
            "prose_treatment": "attributed_scholarship",
            "support_sentence": "著者『著作』を比較枠として参照する。",
        }],
    }])
    assert uses[0].claim_role == "contextual"
    assert uses[0].source_ids == ("p0134", citation_id)
    assert uses[0].lineage_source_ids == ("reference:book", "p0134")
    assert uses[0].citation_source_ids == (citation_id,)
    assert uses[0].citation_required is True


def test_v31_orphan_lineage_is_not_licensed_but_memo_inference_can_continue():
    fixed = build_fixed_assets({
        "propositions": [{"id": "p1", "text": "入力メモの命題"}],
    })
    context = {
        "fixed_assets": fixed.model_dump(mode="json"),
        "argument_nodes": [{
            "id": "e1", "node_type": "evidence",
            "provenance": {"source_ids": ["evidence:orphan", "p1"]},
        }],
    }
    _, uses, claims, _ = compile_draft_evidence(context, [{
        "section_id": "S1",
        "paragraphs": [{"text": "元メモから推論する。", "evidence_ids": ["e1"]}],
        "evidence_treatment": [{
            "evidence_id": "e1", "claim_id": "cl0001",
            "prose_treatment": "qualified_interpretation",
            "support_sentence": "元メモから推論する。",
        }],
    }])
    assert uses[0].source_ids == ("p1",)
    assert uses[0].lineage_source_ids == ("evidence:orphan", "p1")
    assert uses[0].claim_role == "author_inference"
    assert uses[0].verification_status == "review_required"
    assert claims[0].verification_status == "review_required"


def test_v31_bibliography_and_memo_cannot_be_promoted_to_external_fact():
    citation_id = "annotation-citation:book"
    fixed = build_fixed_assets({
        "evidence_context": [{"id": citation_id, "title": "書誌のみ"}],
        "propositions": [{"id": "p1", "text": "メモ"}],
    })
    context = {
        "fixed_assets": fixed.model_dump(mode="json"),
        "annotation_citation_evidence": [{
            "id": citation_id, "reference_id": "reference:book",
        }],
        "argument_nodes": [{
            "id": "e1", "node_type": "evidence",
            "provenance": {"source_ids": ["reference:book", "p1"]},
        }],
    }
    with pytest.raises(FukamiruError, match="does not support claim role source_fact"):
        compile_draft_evidence(context, [{
            "section_id": "S1",
            "paragraphs": [{"text": "外部事実である。", "evidence_ids": ["e1"]}],
            "evidence_treatment": [{
                "evidence_id": "e1", "claim_id": "cl0001",
                "prose_treatment": "externally_supported_fact",
                "support_sentence": "外部事実である。",
            }],
        }])


def test_v31_unmaterialized_evidence_id_cannot_become_its_own_source():
    fixed = build_fixed_assets({"propositions": [{"id": "p1", "text": "既存命題"}]})
    context = {
        "fixed_assets": fixed.model_dump(mode="json"),
        "argument_nodes": [{
            "id": "e1", "node_type": "evidence",
            "provenance": {"source_ids": ["evidence:unmaterialized"]},
        }],
    }
    with pytest.raises(FukamiruError, match="no permitted fixed source"):
        compile_draft_evidence(context, [{
            "section_id": "S1",
            "paragraphs": [{"text": "根拠がある。", "evidence_ids": ["e1"]}],
            "evidence_treatment": [{
                "evidence_id": "e1", "claim_id": "cl0001",
                "prose_treatment": "qualified_interpretation",
                "support_sentence": "根拠がある。",
            }],
        }])


def test_v31_verified_excerpt_supports_attribution_without_promoting_bibliography():
    citation_id = "annotation-citation:book"
    fixed = build_fixed_assets({"evidence_context": [
        {"id": citation_id, "title": "文献の書誌情報"},
        {
            "id": "excerpt-1", "text": "著作の確認済み本文",
            "source_span_id": "span-1", "verification_status": "verified",
        },
    ]})
    context = {
        "fixed_assets": fixed.model_dump(mode="json"),
        "annotation_citation_evidence": [{
            "id": citation_id, "reference_id": "reference:book",
        }],
        "argument_nodes": [{
            "id": "e1", "node_type": "evidence",
            "provenance": {
                "source_ids": ["reference:book", "excerpt-1"],
            },
        }],
    }
    _, uses, _, _ = compile_draft_evidence(context, [{
        "section_id": "S1",
        "paragraphs": [{"text": "著者の議論を紹介する。", "evidence_ids": ["e1"]}],
        "evidence_treatment": [{
            "evidence_id": "e1", "claim_id": "cl0001",
            "prose_treatment": "attributed_scholarship",
            "support_sentence": "著者の議論を紹介する。",
        }],
    }])
    assert uses[0].claim_role == "attributed_position"
    assert uses[0].source_ids == ("excerpt-1",)
    assert uses[0].citation_source_ids == (citation_id,)


def test_worker_classifier_freezes_complete_audit_and_keeps_review_out_of_repair(
    monkeypatch,
):
    fixed = build_fixed_assets({
        "evidence_context": [{
            "id": "SRC-1",
            "text": "根拠",
            "verification_status": "verified",
        }],
    })
    saved: dict[str, object] = {}
    monkeypatch.setattr(
        "fukamiru.worker.runner.AuthoringRepository",
        lambda _state: SimpleNamespace(
            save_evidence_state=lambda **kwargs: saved.update(kwargs)
        ),
    )
    runner = object.__new__(WorkerRunner)
    runner.databases = SimpleNamespace(state=object())
    context = {
        "proposed_html": "<article>本文</article>",
        "fixed_assets": fixed.model_dump(mode="json"),
        "section_contracts": [
            {"section_id": "S005"},
            {"section_id": "S014"},
            {"section_id": "S018"},
        ],
        "draft_sections": [
            {
                "section_id": "S005",
                "paragraphs": [{"sentence_ids": ["S005-P01-S01"]}],
            },
            {
                "section_id": "S014",
                "paragraphs": [{"sentence_ids": ["S014-P01-S01"]}],
            },
            {
                "section_id": "S018",
                "paragraphs": [{"sentence_ids": ["S018-P01-S01"]}],
            },
        ],
        "evidence_uses": [],
        "audit_findings": [
            {
                "id": "AF-AUTO",
                "code": "redundancy",
                "severity": "major",
                "object_id": "section:S014",
                "message": "重複文を削除する",
                "repair_scope": "local_section",
            },
            {
                "id": "AF-REVIEW",
                "code": "over_attribution",
                "severity": "major",
                "object_id": "S005",
                "message": "原典への帰属範囲を確認する",
                "repair_scope": "user_input",
                "source_ids": ["SRC-1"],
            },
        ],
    }

    result = runner._authoring_classify_audit_findings(
        SimpleNamespace(run_id="run-1"), context
    ).context

    assert [item["finding_id"] for item in result["auto_repair_findings"]] == [
        "AF-AUTO"
    ]
    assert [item["finding_id"] for item in result["human_review_findings"]] == [
        "AF-REVIEW"
    ]
    assert result["human_review_items"][0]["sentence_id"] == "S005-P01-S01"
    assert result["audit_bundle"]["audited_section_ids"] == ["S005", "S014", "S018"]
    assert result["audit_bundle"]["clean_section_ids"] == ["S018"]
    assert saved["review_items"] == result["human_review_items"]


def test_worker_classifier_records_blocking_reference_defect(monkeypatch):
    fixed = build_fixed_assets({"evidence_context": []})
    monkeypatch.setattr(
        "fukamiru.worker.runner.AuthoringRepository",
        lambda _state: SimpleNamespace(save_evidence_state=lambda **_kwargs: None),
    )
    runner = object.__new__(WorkerRunner)
    runner.databases = SimpleNamespace(state=object())

    classified = runner._authoring_classify_audit_findings(
            SimpleNamespace(run_id="run-1"),
            {
                "proposed_html": "<article>本文</article>",
                "fixed_assets": fixed.model_dump(mode="json"),
                "section_contracts": [{"section_id": "S015"}],
                "draft_sections": [{
                    "section_id": "S015",
                    "paragraphs": [{"sentence_ids": ["S015-P01-S01"]}],
                }],
                "evidence_uses": [],
                "audit_findings": [{
                    "id": "AF-BLOCK",
                    "code": "missing_bibliography",
                    "severity": "critical",
                    "object_id": "S015",
                    "message": "脚注に対応する参考文献がない",
                    "repair_scope": "none",
                }],
            },
    ).context
    assert [item["finding_id"] for item in classified["blocking_findings"]] == [
        "AF-BLOCK"
    ]
