"""V31 prose contracts keep analysis in the article and inference in metadata."""

from __future__ import annotations

from copy import deepcopy

import pytest

from fukamiru.authoring.codex import (
    STAGE_GUIDANCE,
    STAGE_SCHEMAS,
    _validate_codex_output_schema,
    _validate_schema,
)
from fukamiru.errors import FukamiruError, NonRetryableFukamiruError
from fukamiru.worker.runner import WorkerRunner


def test_v31_draft_does_not_require_saved_example_or_evidence_quota() -> None:
    context = {
        "research_problem": {"research_question": "問い"},
        "section_contracts": [{
            "section_id": "s1", "article_role": "body", "claim_ids": ["c1"],
            "evidence_ids": ["e1"], "warrant_ids": ["w1"],
            "development_expectations": {
                "minimum_evidence_items": 8,
                "minimum_concrete_examples": 5,
                "required_argument_moves": [],
            },
        }],
        "argument_nodes": [{"id": "e1", "node_type": "evidence", "claim_id": "c1"}],
    }
    section = {
        "section_id": "s1", "article_role": "body", "claim_ids": ["c1"],
        "paragraphs": [{"text": "作品を読む。", "evidence_ids": ["e1"], "move_indices": []}],
        "realized_evidence_ids": ["e1"], "realized_warrant_ids": ["w1"],
        "evidence_treatment": [{"evidence_id": "e1", "basis": "memo_close_reading",
                                "verification_status": "unverified"}],
        "concrete_examples": [],
    }
    for validator in (
        WorkerRunner._authoring_draft_contract_violations,
        WorkerRunner._authoring_economy_draft_violations,
    ):
        problems = validator(context, [section])
        assert not any("too few concrete examples" in item for item in problems)
        assert not any("did not realize enough contract evidence" in item for item in problems)


def test_draft_inference_schema_is_strict_for_every_v31_role() -> None:
    for role in ("introduction", "body", "conclusion"):
        section = STAGE_SCHEMAS[f"draft_{role}_v31"]["properties"]["sections"]["items"]
        assert "inference_ledger" in section["required"]
        record_schema = section["properties"]["inference_ledger"]["items"]
        record = {
            "id": "i1",
            "judgment": "読者と人物の関係が変わる。",
            "reasoning": "見る側と見られる側の位置が動くため。",
            "basis_ids": ["e1"],
            "claim_id": "c1",
            "support_relation": "inferred_from_supplied_material",
            "paragraph_index": 1,
            "anchor_excerpt": "読者と人物の関係が変わる",
        }
        _validate_schema(record, record_schema)
        with pytest.raises(FukamiruError):
            _validate_schema({**record, "invented_citation": "r1"}, record_schema)


def test_inference_ledger_must_point_to_existing_prose_and_known_bases() -> None:
    context = {
        "propositions": [{"id": "p1"}],
        "claims": [{"id": "c1"}],
        "argument_nodes": [{"id": "e1"}],
    }
    section = {
        "section_id": "s1",
        "claim_ids": ["c1"],
        "paragraphs": [{"text": "人物が見られる側に回ると、読者と人物の関係が変わる。"}],
        "inference_ledger": [{
            "id": "i1",
            "judgment": "読者と人物の関係が変わる",
            "reasoning": "視線の向きが入れ替わるため",
            "basis_ids": ["e1", "p1"],
            "claim_id": "c1",
            "support_relation": "inferred_from_supplied_material",
            "paragraph_index": 1,
            "anchor_excerpt": "読者と人物の関係が変わる",
        }],
    }
    validate = WorkerRunner._authoring_v31_inference_ledger_violations
    assert validate(context, section) == []
    broken = deepcopy(section)
    broken["inference_ledger"][0]["basis_ids"] = ["invented-source"]
    broken["inference_ledger"][0]["anchor_excerpt"] = "資料にはない断定"
    assert any("unknown bases" in issue for issue in validate(context, broken))
    assert any("not anchored in prose" in issue for issue in validate(context, broken))
    without_basis = deepcopy(section)
    without_basis["inference_ledger"][0]["basis_ids"] = []
    assert any("no supplied basis" in issue for issue in validate(context, without_basis))
    without_basis["inference_ledger"][0][
        "support_relation"
    ] = "model_judgment_without_direct_statement"
    assert validate(context, without_basis) == []


def test_draft_repair_reconciles_only_exact_inference_anchors() -> None:
    section = {
        "section_id": "s1",
        "paragraphs": [{"text": "作品の反復から差異が生じる。理論を作品に用いる。"}],
        "inference_ledger": [{
            "id": "i-note", "paragraph_index": 1,
            "anchor_excerpt": "作品の反復から差異が生じる〔注1〕。",
            "basis_ids": ["e1"],
        }, {
            "id": "i-gone", "paragraph_index": 1,
            "anchor_excerpt": "旧稿だけにあった解釈。",
            "basis_ids": ["e2"],
        }],
    }
    problems = [
        "section s1 inference i-note is not anchored in prose",
        "section s1 inference i-gone is not anchored in prose",
    ]
    updated, remaining, retired = (
        WorkerRunner._authoring_v31_reconcile_inference_anchors_after_repair(
            [section], problems
        )
    )
    assert remaining == []
    assert updated[0]["paragraphs"] == section["paragraphs"]
    assert updated[0]["inference_ledger"] == [{
        **section["inference_ledger"][0],
        "anchor_excerpt": "作品の反復から差異が生じる。",
    }]
    assert [(item["inference_id"], item["record"]["basis_ids"]) for item in retired] == [
        ("i-gone", ["e2"])
    ]
    assert len(section["inference_ledger"]) == 2


def test_draft_repair_does_not_retire_an_inference_with_other_contract_errors() -> None:
    section = {
        "section_id": "s1", "paragraphs": [{"text": "現在の本文。"}],
        "inference_ledger": [{
            "id": "i1", "paragraph_index": 1,
            "anchor_excerpt": "消えた判断。", "basis_ids": ["unknown"],
        }],
    }
    problems = [
        "section s1 inference i1 is not anchored in prose",
        "section s1 inference i1 has unknown bases: ['unknown']",
    ]
    updated, remaining, retired = (
        WorkerRunner._authoring_v31_reconcile_inference_anchors_after_repair(
            [section], problems
        )
    )
    assert remaining == problems
    assert updated[0]["inference_ledger"] == section["inference_ledger"]
    assert retired == []


def test_internal_response_language_is_flagged_without_word_count_gate() -> None:
    sections = [{
        "section_id": "s1",
        "article_role": "body",
        "text": "この回答は仮説的な枝に限定される。",
    }]
    findings = WorkerRunner._authoring_reader_facing_meta_prose_findings(sections)
    assert len(findings) == 1
    assert findings[0]["object_id"] == "s1"
    assert not WorkerRunner._authoring_reader_facing_meta_prose_findings([{
        "section_id": "s1", "text": "人物が見られる側に回ると、読者との関係も変わる。"
    }])


def test_final_inference_anchor_reports_when_later_prose_changed() -> None:
    html = (
        '<article><section id="SEC-sec-body">'
        '<p id="P-sec-body-001">人物が見られる側に回ると読者との関係が変わる。</p>'
        '</section></article>'
    )
    ledger = [{
        "id": "i1", "section_id": "sec-body", "paragraph_index": 1,
        "anchor_excerpt": "読者との関係が変わる",
    }, {
        "id": "i2", "section_id": "sec-body", "paragraph_index": 1,
        "anchor_excerpt": "初稿から消えた判断",
    }]
    resolved = WorkerRunner._authoring_v31_final_inference_anchors(html, ledger)
    assert [item["final_anchor_status"] for item in resolved] == [
        "present", "changed_or_missing",
    ]
    assert {item["final_paragraph_id"] for item in resolved} == {"P-sec-body-001"}


def test_v31_prompts_prioritize_analysis_over_item_definitions() -> None:
    assert "do not give each term its own definition sentence" in STAGE_GUIDANCE[
        "semantic_paragraphs_v31"
    ]
    assert "Do not make one paragraph per claim" in STAGE_GUIDANCE["draft_body_v31"]
    assert "An auxiliary enumeration may be omitted" in STAGE_GUIDANCE["audit_v31"]
    assert "列挙された名詞を順番に定義しない" in STAGE_GUIDANCE["redraft_body_prose_v31"]


def test_reader_brief_keeps_claims_but_excludes_planning_language() -> None:
    context = {
        "claims": [{"id": "c1", "text": "誌面の配置は読まれ方に関わりうる"}],
        "argument_nodes": [
            {"id": "e1", "node_type": "evidence", "text": "異なる作品が隣り合う"},
            {"id": "w1", "node_type": "warrant", "text": "読者は順に作品へ接する"},
        ],
        "semantic_paragraph_plan": [{"section_id": "s1", "paragraphs": [{
            "development_instruction": "用語を順に説明する", "central_claim": "抽象的な主張",
        }]}],
    }
    section = {
        "section_id": "s1", "article_role": "body", "claim_ids": ["c1"],
        "evidence_ids": ["e1"], "warrant_ids": ["w1"],
        "section_task": "まず検討する", "section_output": "配置は読まれ方に関わりうる",
        "section_output_contract": {"proposition": "配置は読まれ方に関わりうる"},
    }
    brief = WorkerRunner._authoring_reader_content_brief_v31(context, [section])
    assert brief["sections"][0]["claims"][0]["id"] == "c1"
    assert brief["sections"][0]["evidence"][0]["id"] == "e1"
    assert brief["sections"][0]["warrants"][0]["id"] == "w1"
    assert "検討する" not in str(brief)
    assert "development_instruction" not in str(brief)


def test_body_reader_brief_keeps_the_section_reasoning_through_compaction() -> None:
    context = {
        "claims": [{"id": "c1", "text": "作品の配置は読む順序に関わる。"}],
        "argument_nodes": [],
        "section_contracts": [{
            "section_id": "s1", "article_role": "body",
            "local_question": "作品の配置は読者の理解にどう関わるか。",
            "thesis_link": "配置の差から、作品の意味が読む順序にも左右されると論じる。",
            "previous_section_bridge": "前節の制作条件から読者の経験へ移る。",
        }],
    }
    section = {
        "section_id": "s1", "article_role": "body", "claim_ids": ["c1"],
        "evidence_ids": [], "warrant_ids": [],
        "section_output_contract": {
            "proposition": "配置は作品の読まれ方を方向づける。",
            "output_type": "interpretive_result",
            "required_evidence_level": "reviewable",
        },
    }
    brief = WorkerRunner._authoring_reader_content_brief_v31(context, [section])

    class FakeGenerator:
        def _authoring_generate(self, _claimed, stage, _context, *, payload, **_kwargs):
            if stage == "naturalize_reader_brief_v31":
                return {"sections": payload["source_sections"]}
            return {"sections": [{
                "section_id": "s1", "meaning_preserved": "true",
                "natural_japanese": True, "internal_language_absent": True,
                "internal_issues": [], "marginal_inferences": [],
                "japanese_issues": [], "reason": "問題なし",
            }]}

    projected, report = WorkerRunner._authoring_naturalize_reader_content_brief_v31(
        FakeGenerator(), object(), context, brief
    )
    assert report["passed"] is True
    compact = WorkerRunner._authoring_compact_claim_citation_brief_v31(
        projected["sections"][0], []
    )
    assert compact["section_result"] == "配置は作品の読まれ方を方向づける。"
    assert "作品の意味が読む順序にも左右される" in compact["framing"]
    assert "前節の制作条件" in compact["framing"]


def test_reader_brief_projects_memo_attribution_without_losing_provenance() -> None:
    attributed = (
        "メモでは「作品の意味は配置によっても形づくられる。」と位置づけ、"
        "該当文献を理論的参照として挙げる。"
    )
    context = {
        "claims": [{"id": "c1", "text": "配置は読まれ方に関わりうる。"}],
        "argument_nodes": [{
            "id": "e1", "node_type": "evidence", "text": attributed,
            "proposition_id": "p1",
            "provenance": {
                "source": "memo", "source_ids": ["p1"],
                "verification_status": "unverified",
            },
        }],
        "research_problem": {"research_question": "配置は読まれ方にどう関わるか。"},
        "introduction_contract": {
            "problematisation": "採択済みの外部証拠はない。入力メモでは配置を問題にする。",
            "contribution_target": "配置と読まれ方の関係を考える。",
        },
    }
    section = {
        "section_id": "s1", "article_role": "introduction",
        "claim_ids": ["c1"], "evidence_ids": ["e1"], "warrant_ids": [],
        "section_output": "配置と読まれ方の関係を問う。",
    }
    brief = WorkerRunner._authoring_reader_content_brief_v31(context, [section])
    evidence = brief["sections"][0]["evidence"][0]
    assert evidence["text"] == "作品の意味は配置によっても形づくられる。"
    assert evidence["source_kind"] == "memo"
    assert evidence["verification_status"] == "unverified"
    assert evidence["source_ids"] == ["p1"]
    assert context["argument_nodes"][0]["text"] == attributed

    class FakeGenerator:
        def _authoring_generate(self, _claimed, stage, _context, *, payload, **_kwargs):
            if stage == "naturalize_reader_brief_v31":
                source = payload["source_sections"]
                assert "採択済み" not in str(source)
                assert "入力メモ" not in str(source)
                assert "メモでは" not in str(source)
                return {"sections": source}
            return {"sections": [{
                "section_id": "s1", "meaning_preserved": "true",
                "natural_japanese": True, "internal_language_absent": True,
                "internal_issues": [],
                "marginal_inferences": [],
                "japanese_issues": [],
                "reason": "問題なし",
            }]}

    projected, report = WorkerRunner._authoring_naturalize_reader_content_brief_v31(
        FakeGenerator(), object(), context, brief
    )
    assert report["passed"] is True
    assert projected["sections"][0]["evidence"][0]["text"] == evidence["text"]


def test_reader_brief_separates_body_editorial_claim_without_dropping_limits() -> None:
    context = {
        "claims": [
            {"id": "c10", "text": "街の噂は承認関係を示す可能性がある。"},
            {"id": "c11", "text": (
                "二つの周辺的論点を現行論証から除外し、検証後に採否を判断する"
                "研究候補として管理するという編集上の判断。"
            )},
        ],
        "argument_nodes": [
            {"id": "e10", "node_type": "evidence", "claim_id": "c10",
             "text": "噂と街の描写がある。"},
            {"id": "e11", "node_type": "evidence", "claim_id": "c11",
             "text": "メモに採否の記録がある。"},
            {"id": "w11", "node_type": "warrant", "claim_id": "c11",
             "text": "候補として管理する。"},
        ],
    }
    original = [{"section_id": "s11", "article_role": "body",
                 "claim_ids": ["c10", "c11"],
                 "evidence_ids": ["e10", "e11"], "warrant_ids": ["w11"],
                 "section_output": "街の噂は承認を示す可能性がある。"}]
    projected, exclusions = WorkerRunner._authoring_v31_separate_editorial_contract_items(
        context, original
    )
    assert projected[0]["claim_ids"] == ["c10"]
    assert projected[0]["evidence_ids"] == ["e10"]
    assert projected[0]["warrant_ids"] == []
    assert original[0]["claim_ids"] == ["c10", "c11"]
    assert {(x["field"], x["item_id"]) for x in exclusions} == {
        ("claim_ids", "c11"), ("evidence_ids", "e11"), ("warrant_ids", "w11")
    }
    brief = WorkerRunner._authoring_reader_content_brief_v31(context, projected)
    assert "編集上の判断" not in str(brief)
    assert "街の噂" in str(brief)


def test_reader_brief_keeps_conclusion_substantive_limit() -> None:
    context = {"claims": [{"id": "c16", "text": (
        "資料の範囲が限られるため、時期区分は仮説にとどまる。"
    )}], "argument_nodes": []}
    section = {"section_id": "s15", "article_role": "conclusion",
               "claim_ids": ["c16"], "evidence_ids": [], "warrant_ids": []}
    projected, exclusions = WorkerRunner._authoring_v31_separate_editorial_contract_items(
        context, [section]
    )
    assert not exclusions
    assert projected[0]["claim_ids"] == ["c16"]


def test_editorial_separation_cannot_drop_required_citation() -> None:
    context = {
        "claims": [{"id": "c1", "text": (
            "論点を現行論証から除外する編集上の判断。"
        )}],
        "argument_nodes": [],
        "authoring_atomic_evidence_contracts": [{
            "realization": "attribute_theory_with_citation",
            "claim_ids": ["c1"], "citation_evidence_ids": ["source1"],
        }],
    }
    contract = [{"section_id": "s1", "claim_ids": ["c1", "c2"]}]
    with pytest.raises(NonRetryableFukamiruError, match="required citation"):
        WorkerRunner._authoring_v31_separate_editorial_contract_items(
            context, contract
        )


def test_reader_brief_projects_mixed_reference_status_as_reader_note() -> None:
    mixed = {"node_type": "evidence", "text": (
        "著者メモは家具・台所を生活細部として列挙し、"
        "ホワイト『メタヒストリー』を、歴史記述と物語形式の関係を考える"
        "参照候補に挙げている。同書の書誌同一性のみ確認済みで、"
        "当該適用の妥当性は未検証である。"
    )}
    text = WorkerRunner._authoring_reader_evidence_text_v31(mixed)
    assert "家具・台所" in text and "未検証" in text
    assert "著者メモ" not in text and "書誌同一性" not in text


def test_reader_brief_rewording_repairs_abstract_input_before_draft() -> None:
    for stage in STAGE_SCHEMAS:
        _validate_codex_output_schema(STAGE_SCHEMAS[stage])
    context = {"output_language": "ja"}
    raw = {"policy": "v31-reader-content-only-v1", "sections": [{
        "section_id": "s1", "article_role": "body",
        "section_result": "漫画技法は共有知との相互作用で成立する。",
        "claims": [{"id": "c1", "text": "漫画技法は共有知に依存する。", "level": "main"}],
        "evidence": [{"id": "e1", "text": "編集者と作画担当が協働する。"}],
        "warrants": [],
    }]}
    proposed = {"sections": [{
        "section_id": "s1", "framing": "",
        "section_result": "漫画を描く技術は、作り手が受け継いだ知識と自分の選択から育つ。",
        "claims": [{"id": "c1", "text": "作り手は共有された知識を使って漫画を描く。"}],
        "evidence": [{"id": "e1", "text": "編集者と作画担当は一緒に制作する。"}],
        "warrants": [],
    }]}

    class FakeGenerator:
        calls = []

        def _authoring_generate(self, _claimed, stage, _context, *, payload, **_kwargs):
            self.calls.append((stage, payload))
            if stage == "naturalize_reader_brief_v31":
                return proposed
            return {"sections": [{
                "section_id": "s1", "meaning_preserved": "true",
                "natural_japanese": True, "internal_language_absent": True,
                "internal_issues": [],
                "marginal_inferences": [],
                "japanese_issues": [],
                "reason": "意味を保った自然な言い換え",
            }]}

    fake = FakeGenerator()
    projected, report = WorkerRunner._authoring_naturalize_reader_content_brief_v31(
        fake, object(), context, raw
    )
    assert report["passed"] is True
    assert any(
        item["item_id"] == "c1"
        and item["before"] == "漫画技法は共有知に依存する。"
        and item["after"] == "作り手は共有された知識を使って漫画を描く。"
        for item in report["changes"]
    )
    assert projected["sections"][0]["claims"][0] == {
        "id": "c1", "text": "作り手は共有された知識を使って漫画を描く。",
        "level": "main",
    }
    assert raw["sections"][0]["claims"][0]["text"] == "漫画技法は共有知に依存する。"
    assert [stage for stage, _ in fake.calls] == [
        "naturalize_reader_brief_v31", "audit_reader_brief_v31",
        "audit_reader_brief_v31",
    ]
    assert "漫画技法" in str(fake.calls[0][1])
    assert "漫画技法" not in str(projected)


def test_reader_brief_meaning_change_passes_as_marginal_for_human_review() -> None:
    schema = STAGE_SCHEMAS["audit_reader_brief_v31"]
    meaning_schema = schema["properties"]["sections"]["items"]["properties"][
        "meaning_preserved"
    ]
    assert meaning_schema["enum"] == ["true", "marginal"]
    with pytest.raises(FukamiruError):
        _validate_schema({"sections": [{
            "section_id": "s1", "meaning_preserved": "false",
            "natural_japanese": True, "internal_language_absent": True,
            "marginal_inferences": [], "japanese_issues": [],
            "internal_issues": [], "reason": "意味が変わる",
        }]}, schema)
    raw = {"sections": [{
        "section_id": "s1", "article_role": "body", "section_result": "可能性がある。",
        "claims": [], "evidence": [], "warrants": [],
    }]}

    class FakeGenerator:
        attempts = 0

        def _authoring_generate(self, _claimed, stage, _context, *, payload, **_kwargs):
            if stage == "naturalize_reader_brief_v31":
                self.attempts += 1
                return {"sections": [{
                    "section_id": "s1", "section_result": "確実に起きる。",
                    "framing": "", "claims": [], "evidence": [], "warrants": [],
                }]}
            return {"sections": [{
                "section_id": "s1", "meaning_preserved": "marginal",
                "natural_japanese": True, "internal_language_absent": True,
                "internal_issues": [],
                "marginal_inferences": [],
                "japanese_issues": [],
                "reason": "可能性を断定に強めた。",
            }]}

    fake = FakeGenerator()
    projected, report = WorkerRunner._authoring_naturalize_reader_content_brief_v31(
        fake, object(), {}, raw
    )
    assert fake.attempts == 1
    assert projected["sections"][0]["section_result"] == "確実に起きる。"
    assert report["marginal_audit_decisions"][0]["reason"] == "可能性を断定に強めた。"
    _, reviews = WorkerRunner._authoring_v31_reader_brief_marginal_reviews([report])
    assert reviews[0]["category"] == "reader_brief_marginal_decision"
    assert reviews[0]["blocks_render"] is False


def test_reader_brief_conflicting_audits_pass_as_marginal_without_rewrite() -> None:
    source = "表示形式を比較する。"
    candidate_text = "媒体や仕組みを比較する。"
    raw = {"sections": [{
        "section_id": "s14", "article_role": "body",
        "section_result": source, "claims": [], "evidence": [], "warrants": [],
    }]}

    class FakeGenerator:
        calls = []

        def _authoring_generate(self, _claimed, stage, _context, *, payload, **kwargs):
            self.calls.append(kwargs["operation_name"])
            if stage == "naturalize_reader_brief_v31":
                return {"sections": [{
                    "section_id": "s14", "section_result": candidate_text,
                    "framing": "", "claims": [], "evidence": [], "warrants": [],
                }]}
            second = kwargs["operation_name"].endswith("round_2")
            return {"sections": [{
                "section_id": "s14",
                "meaning_preserved": "marginal" if second else "true",
                "natural_japanese": not second,
                "internal_language_absent": True,
                "internal_issues": [], "marginal_inferences": [],
                "japanese_issues": [{
                    "field": "section_result", "item_id": "",
                    "excerpt": "媒体や仕組み", "reason": "対象が変わる",
                    "suggested_rewrite": "表示の仕方",
                }] if second else [],
                "reason": "対象が変わる" if second else "意味を保つ",
            }]}

    fake = FakeGenerator()
    projected, report = WorkerRunner._authoring_naturalize_reader_content_brief_v31(
        fake, object(), {}, raw
    )
    assert projected["sections"][0]["section_result"] == candidate_text
    assert report["audit"][0]["meaning_preserved"] == "marginal"
    assert report["audit_history"][0][0]["meaning_preserved"] == "true"
    assert report["audit_history"][1][0]["meaning_preserved"] == "marginal"
    assert report["audit_disagreements"][0]["section_id"] == "s14"
    assert report["focused_repair_audit"] == []
    assert len(fake.calls) == 3
    _, reviews = WorkerRunner._authoring_v31_reader_brief_marginal_reviews([report])
    assert reviews[0]["category"] == "reader_brief_audit_disagreement"
    assert reviews[0]["blocks_render"] is False


def test_reader_brief_repair_does_not_block_on_disputed_other_section() -> None:
    raw = {"sections": [
        {
            "section_id": section_id, "article_role": "body",
            "section_result": text, "claims": [], "evidence": [], "warrants": [],
        }
        for section_id, text in (
            ("s04", "比較を検討する。"),
            ("s05", "人物の関係を検討する。"),
        )
    ]}
    awkward_s04 = "比較的に検討する。"
    awkward_s05 = "欲望の対象と回復を担う人物"
    fixed_s04 = "作品を比較して検討する。"

    def candidate(section_id, wording):
        return {
            "section_id": section_id, "section_result": wording,
            "framing": "", "claims": [], "evidence": [], "warrants": [],
        }

    def verdict(section_id, issue=None):
        return {
            "section_id": section_id, "meaning_preserved": "true",
            "natural_japanese": issue is None,
            "internal_language_absent": True,
            "internal_issues": [], "marginal_inferences": [],
            "japanese_issues": [] if issue is None else [{
                "field": "section_result", "item_id": "",
                "excerpt": issue, "reason": "語のつながりが不自然",
                "suggested_rewrite": fixed_s04,
            }],
            "reason": "問題なし" if issue is None else "語のつながりが不自然",
        }

    class FakeGenerator:
        def __init__(self):
            self.calls = []

        def _authoring_generate(self, _claimed, stage, _context, *, payload, **kwargs):
            operation = kwargs["operation_name"]
            self.calls.append(operation)
            if stage == "naturalize_reader_brief_v31":
                if operation.endswith("issue_repair"):
                    assert [s["section_id"] for s in payload["source_sections"]] == ["s04"]
                    assert [i["section_id"] for i in payload["repair_issues"]] == ["s04"]
                    return {"sections": [candidate("s04", fixed_s04)]}
                return {"sections": [
                    candidate(s["section_id"],
                              awkward_s04 if s["section_id"] == "s04" else awkward_s05)
                    for s in payload["source_sections"]
                ]}
            if operation.endswith("issue_repair"):
                return {"sections": [verdict("s04")]}
            if operation.endswith("round_1"):
                return {"sections": [
                    verdict("s04", awkward_s04), verdict("s05"),
                ]}
            return {"sections": [
                verdict("s04", awkward_s04), verdict("s05", awkward_s05),
            ]}

    fake = FakeGenerator()
    projected, report = WorkerRunner._authoring_naturalize_reader_content_brief_v31(
        fake, object(), {}, raw
    )
    assert [s["section_result"] for s in projected["sections"]] == [
        fixed_s04, awkward_s05,
    ]
    assert [item["section_id"] for item in report["audit_disagreements"]] == ["s05"]
    assert report["passed"] is True
    assert "authoring_audit_reader_brief_issue_repair" in fake.calls


@pytest.mark.parametrize("marginal_round", [1, 2])
def test_reader_brief_accepts_recorded_marginal_inference_for_human_review(
    marginal_round: int,
) -> None:
    source_excerpt = "複数の流れから成る"
    candidate_excerpt = "複数の流れが交わって生じた"
    raw = {"sections": [{
        "section_id": "s03", "article_role": "body",
        "section_result": f"この時期の変化を{source_excerpt}ものとして比較する。",
        "claims": [], "evidence": [], "warrants": [],
    }]}
    candidate = {
        "section_id": "s03", "framing": "",
        "section_result": (
            f"この時期の変化を{candidate_excerpt}ものとみるのは比較仮説である。"
        ),
        "claims": [], "evidence": [], "warrants": [],
    }
    marginal = {
        "source_field": "section_result", "source_item_id": "",
        "field": "section_result", "item_id": "",
        "source_excerpt": source_excerpt,
        "candidate_excerpt": candidate_excerpt,
        "reason": "別々の流れの関係を、断定せず比較仮説として述べている。",
    }
    schema = STAGE_SCHEMAS["audit_reader_brief_v31"]
    _validate_codex_output_schema(schema)
    verdict = {
        "section_id": "s03", "meaning_preserved": "marginal",
        "natural_japanese": True, "internal_language_absent": True,
                "internal_issues": [],
        "marginal_inferences": [marginal], "japanese_issues": [],
        "reason": "仮説として許容できる推論",
    }
    _validate_schema({"sections": [verdict]}, schema)
    with pytest.raises(FukamiruError):
        _validate_schema({"sections": [{**verdict, "meaning_preserved": True}]}, schema)

    class FakeGenerator:
        audit_count = 0

        def _authoring_generate(self, _claimed, stage, _context, *, payload, **_kwargs):
            if stage == "naturalize_reader_brief_v31":
                return {"sections": [candidate]}
            self.audit_count += 1
            if self.audit_count != marginal_round:
                return {"sections": [{
                    **verdict, "meaning_preserved": "true",
                    "marginal_inferences": [], "reason": "意味を保持",
                }]}
            return {"sections": [verdict]}

    fake = FakeGenerator()
    projected, report = WorkerRunner._authoring_naturalize_reader_content_brief_v31(
        fake, object(), {}, raw
    )
    assert fake.audit_count == 2
    assert projected["sections"][0]["section_result"] == candidate["section_result"]
    assert report["passed"] is True
    assert report["marginal_inferences"] == [{"section_id": "s03", **marginal}]
    recorded, reviews = WorkerRunner._authoring_v31_reader_brief_marginal_reviews(
        [report]
    )
    assert recorded == report["marginal_inferences"]
    assert {item["category"] for item in reviews} == {
        "reader_brief_marginal_inference", "reader_brief_audit_disagreement",
    }
    assert reviews[0]["section_id"] == "s03"
    assert source_excerpt in reviews[0]["reason"]
    assert candidate_excerpt in reviews[0]["reason"]
    assert reviews[0]["blocks_render"] is False


def test_reader_brief_rejects_untraceable_marginal_inference() -> None:
    raw = {"sections": [{
        "section_id": "s1", "article_role": "body",
        "section_result": "複数の流れを比較する。",
        "claims": [], "evidence": [], "warrants": [],
    }]}

    class FakeGenerator:
        def _authoring_generate(self, _claimed, stage, _context, *, payload, **_kwargs):
            if stage == "naturalize_reader_brief_v31":
                return {"sections": [{
                    "section_id": "s1", "section_result": "流れが交わったという仮説。",
                    "framing": "", "claims": [], "evidence": [], "warrants": [],
                }]}
            return {"sections": [{
                "section_id": "s1", "meaning_preserved": "marginal",
                "natural_japanese": True, "internal_language_absent": True,
                "internal_issues": [],
                "marginal_inferences": [{
                    "source_field": "section_result", "source_item_id": "",
                    "field": "section_result", "item_id": "",
                    "source_excerpt": "原文にない語句",
                    "candidate_excerpt": "流れが交わった",
                    "reason": "推論として許容",
                }],
                "japanese_issues": [], "reason": "推論として許容",
            }]}

    with pytest.raises(NonRetryableFukamiruError, match="excerpts do not match"):
        WorkerRunner._authoring_naturalize_reader_content_brief_v31(
            FakeGenerator(), object(), {}, raw
        )


def test_reader_brief_marginal_record_can_cross_source_fields() -> None:
    source = {
        "section_id": "s1", "article_role": "body",
        "section_result": "複数の流れを比較する。",
        "claims": [], "evidence": [],
        "warrants": [{"id": "w1", "text": "流れは別々に比較する。"}],
    }
    candidate = {
        "section_id": "s1", "section_result": "流れが交わるという比較仮説を置く。",
        "framing": "", "claims": [], "evidence": [],
        "warrants": source["warrants"],
    }
    inference = {
        "source_field": "warrants", "source_item_id": "w1",
        "field": "section_result", "item_id": "",
        "source_excerpt": "流れは別々に比較する",
        "candidate_excerpt": "流れが交わる",
        "reason": "別々の流れの接点を仮説として考える。",
    }

    class FakeGenerator:
        def _authoring_generate(self, _claimed, stage, _context, *, payload, **_kwargs):
            if stage == "naturalize_reader_brief_v31":
                return {"sections": [candidate]}
            return {"sections": [{
                "section_id": "s1", "meaning_preserved": "marginal",
                "natural_japanese": True, "internal_language_absent": True,
                "internal_issues": [],
                "marginal_inferences": [inference], "japanese_issues": [],
                "reason": "仮説を記録して採用",
            }]}

    _, report = WorkerRunner._authoring_naturalize_reader_content_brief_v31(
        FakeGenerator(), object(), {}, {"sections": [source]}
    )
    assert report["marginal_inferences"] == [{"section_id": "s1", **inference}]
    _, reviews = WorkerRunner._authoring_v31_reader_brief_marginal_reviews([report])
    assert "warrants/w1" in reviews[0]["reason"]
    assert "section_result/-" in reviews[0]["reason"]


def test_reader_brief_focused_repair_may_keep_a_recorded_inference() -> None:
    raw = {"sections": [{
        "section_id": "s1", "article_role": "body",
        "section_result": "複数の流れを比較する。",
        "claims": [], "evidence": [], "warrants": [],
    }]}
    awkward = "複数の流れを比較的にみる。"
    revised = "複数の流れが交わるとみる比較仮説である。"

    class FakeGenerator:
        def _authoring_generate(self, _claimed, stage, _context, *, payload, **kwargs):
            operation = kwargs["operation_name"]
            if stage == "naturalize_reader_brief_v31":
                return {"sections": [{
                    "section_id": "s1", "section_result": (
                        revised if operation.endswith("issue_repair") else awkward
                    ),
                    "framing": "", "claims": [], "evidence": [], "warrants": [],
                }]}
            if operation.endswith("issue_repair"):
                return {"sections": [{
                    "section_id": "s1", "meaning_preserved": "marginal",
                    "natural_japanese": True, "internal_language_absent": True,
                "internal_issues": [],
                    "marginal_inferences": [{
                        "source_field": "section_result", "source_item_id": "",
                        "field": "section_result", "item_id": "",
                        "source_excerpt": "複数の流れを比較する",
                        "candidate_excerpt": "複数の流れが交わる",
                        "reason": "交錯を比較仮説として提案する。",
                    }],
                    "japanese_issues": [], "reason": "記録された仮説を許容",
                }]}
            return {"sections": [{
                "section_id": "s1", "meaning_preserved": "true",
                "natural_japanese": False, "internal_language_absent": True,
                "internal_issues": [],
                "marginal_inferences": [],
                "japanese_issues": [{
                    "field": "section_result", "item_id": "", "excerpt": awkward,
                    "reason": "不自然な連語", "suggested_rewrite": revised,
                }],
                "reason": "不自然な連語",
            }]}

    projected, report = WorkerRunner._authoring_naturalize_reader_content_brief_v31(
        FakeGenerator(), object(), {}, raw
    )
    assert projected["sections"][0]["section_result"] == revised
    assert report["passed"] is True
    assert report["focused_repair_audit"][0]["meaning_preserved"] == "marginal"
    assert report["marginal_inferences"][0]["candidate_excerpt"] == (
        "複数の流れが交わる"
    )


def test_reader_brief_repairs_issue_found_only_on_second_audit() -> None:
    raw = {"sections": [
        {
            "section_id": "s12", "article_role": "body",
            "section_result": "子どもの生活を比較する。", "claims": [
                {"id": "c1102", "text": "作品を比較して読む。"},
            ], "evidence": [], "warrants": [],
        },
        {
            "section_id": "s13", "article_role": "body",
            "section_result": "短い漫画を比較する。", "claims": [
                {"id": "c1202", "text": "社会への批評的距離を比較する。"},
            ], "evidence": [{"id": "e13", "text": "掲載場所が異なる。"}],
            "warrants": [],
        },
    ]}
    first = {
        "s12": "作品を比較的に読んでいる。",
        "s13": "社会を批評対象として距離を取る語りを比べる。",
    }
    repaired = {
        "s12": "作品を比較して読んでいる。",
        "s13": "社会に対して批評的な距離を取る語りを比べる。",
    }

    def row(source, wording):
        return {
            "section_id": source["section_id"],
            "section_result": source["section_result"], "framing": "",
            "claims": [{"id": source["claims"][0]["id"], "text": wording}],
            "evidence": source["evidence"], "warrants": [],
        }

    def verdict(section_id, issue=None):
        return {
            "section_id": section_id, "meaning_preserved": "true",
            "natural_japanese": issue is None, "internal_language_absent": True,
                "internal_issues": [],
            "marginal_inferences": [],
            "japanese_issues": [] if issue is None else [{
                "field": "claims", "item_id": issue[0], "excerpt": issue[1],
                "reason": "連語が不自然", "suggested_rewrite": issue[2],
            }],
            "reason": "問題なし" if issue is None else "連語が不自然",
        }

    class FakeGenerator:
        calls = []

        def _authoring_generate(self, _claimed, stage, _context, *, payload, **kwargs):
            operation = kwargs["operation_name"]
            self.calls.append((stage, operation, payload))
            if stage == "naturalize_reader_brief_v31":
                sources = payload["source_sections"]
                if operation.endswith("round_1"):
                    assert [s["section_id"] for s in sources] == ["s12", "s13"]
                    return {"sections": [row(s, first[s["section_id"]]) for s in sources]}
                if operation.endswith("round_2"):
                    assert [s["section_id"] for s in sources] == ["s12"]
                    assert [v["section_id"] for v in payload["audit_feedback"]] == ["s12"]
                    return {"sections": [row(sources[0], repaired["s12"])]}
                assert operation.endswith("issue_repair")
                assert [s["section_id"] for s in sources] == ["s13"]
                assert [i["excerpt"] for i in payload["repair_issues"]] == [
                    "社会を批評対象として距離を取る語り",
                ]
                return {"sections": [row(sources[0], repaired["s13"])]}
            if operation.endswith("round_1"):
                return {"sections": [
                    verdict("s12", (
                        "c1102", "比較的に読んでいる", "比較して読んでいる",
                    )), verdict("s13"),
                ]}
            if operation.endswith("round_2"):
                assert payload["candidate_sections"][0]["claims"][0]["text"] == (
                    repaired["s12"]
                )
                assert payload["candidate_sections"][1]["claims"][0]["text"] == (
                    first["s13"]
                )
                return {"sections": [
                    verdict("s12"), verdict("s13", (
                        "c1202", "社会を批評対象として距離を取る語り",
                        "社会に対して批評的な距離を取る語り",
                    )),
                ]}
            assert operation.endswith("issue_repair")
            assert payload["source_sections"][0]["evidence"] == []
            assert payload["candidate_sections"][0]["evidence"] == []
            assert payload["candidate_sections"][0]["claims"][0]["id"] == "c1202"
            return {"sections": [verdict("s13")]}

    fake = FakeGenerator()
    projected, report = WorkerRunner._authoring_naturalize_reader_content_brief_v31(
        fake, object(), {}, raw
    )
    assert [s["claims"][0]["text"] for s in projected["sections"]] == [
        repaired["s12"], first["s13"],
    ]
    assert [i["section_id"] for i in report["japanese_issue_history"]] == [
        "s12", "s13",
    ]
    assert report["audit_history"][0][1]["natural_japanese"] is True
    assert report["audit_history"][1][1]["natural_japanese"] is False
    assert report["passed"] is True
    assert report["focused_repair_audit"] == []
    assert [item["section_id"] for item in report["audit_disagreements"]] == [
        "s12", "s13",
    ]
    assert all(
        item["meaning_preserved"] == "marginal" for item in report["audit"]
    )
    assert [call[1] for call in fake.calls] == [
        "authoring_naturalize_reader_brief_round_1",
        "authoring_audit_reader_brief_round_1",
        "authoring_naturalize_reader_brief_round_2",
        "authoring_audit_reader_brief_round_2",
    ]


def test_reader_brief_repairs_internal_phrase_found_on_second_audit() -> None:
    raw = {"sections": [{
        "section_id": "s10", "article_role": "body",
        "section_result": "生活描写は時代の手がかりになる可能性がある。",
        "claims": [{"id": "c10", "text": "生活描写は手がかりになる。"}],
        "evidence": [{"id": "e10", "text": "家具と台所の描写がある。"}],
        "warrants": [],
    }]}
    internal = "今回の入力では"
    def candidate(text):
        return {"sections": [{
            "section_id": "s10", "section_result": raw["sections"][0]["section_result"],
            "framing": "", "claims": raw["sections"][0]["claims"],
            "evidence": [{"id": "e10", "text": text}], "warrants": [],
        }]}
    issue = {"field": "evidence", "item_id": "e10", "excerpt": internal,
             "reason": "入力工程の言葉", "suggested_rewrite": "作品には"}
    def verdict(bad):
        return {"sections": [{
            "section_id": "s10", "meaning_preserved": "true",
            "natural_japanese": True, "internal_language_absent": not bad,
            "marginal_inferences": [], "japanese_issues": [],
            "internal_issues": [issue] if bad else [],
            "reason": "内部語が残る" if bad else "問題なし",
        }]}
    class FakeGenerator:
        def _authoring_generate(self, _claimed, stage, _context, *, payload, **kwargs):
            operation = kwargs["operation_name"]
            if stage == "naturalize_reader_brief_v31":
                if operation.endswith("issue_repair"):
                    assert payload["repair_issues"][0]["excerpt"] == internal
                    return candidate("作品には家具と台所の描写がある。")
                return candidate("今回の入力では家具と台所の描写がある。")
            return verdict(operation.endswith("round_2"))
    projected, report = WorkerRunner._authoring_naturalize_reader_content_brief_v31(
        FakeGenerator(), object(), {}, raw
    )
    assert internal in str(projected)
    assert report["internal_issue_history"][0]["item_id"] == "e10"
    assert report["audit"][0]["meaning_preserved"] == "marginal"
    assert report["audit_disagreements"][0]["section_id"] == "s10"


def test_reader_brief_cannot_pass_by_keeping_known_unnatural_excerpt() -> None:
    raw = {"sections": [{
        "section_id": "s1", "article_role": "body",
        "section_result": "作品を比較する。", "claims": [
            {"id": "c1", "text": "社会への批評的距離を比較する。"},
        ], "evidence": [], "warrants": [],
    }]}
    awkward = "社会を批評対象として距離を取る語り"

    class FakeGenerator:
        def _authoring_generate(self, _claimed, stage, _context, *, payload, **_kwargs):
            if stage == "naturalize_reader_brief_v31":
                return {"sections": [{
                    "section_id": "s1", "section_result": "作品を比較する。",
                    "framing": "", "claims": [{"id": "c1", "text": awkward}],
                    "evidence": [], "warrants": [],
                }]}
            return {"sections": [{
                "section_id": "s1", "meaning_preserved": "true",
                "natural_japanese": False, "internal_language_absent": True,
                "internal_issues": [],
                "marginal_inferences": [],
                "japanese_issues": [{
                    "field": "claims", "item_id": "c1", "excerpt": awkward,
                    "reason": "不自然", "suggested_rewrite": "社会に対して批評的な距離を取る語り",
                }], "reason": "不自然",
            }]}

    with pytest.raises(NonRetryableFukamiruError, match="known unnatural"):
        WorkerRunner._authoring_naturalize_reader_content_brief_v31(
            FakeGenerator(), object(), {}, raw
        )


def test_reader_brief_focused_repair_records_meaning_change_as_marginal() -> None:
    raw = {"sections": [{
        "section_id": "s1", "article_role": "body",
        "section_result": "配置によって読まれ方が変わる可能性がある。",
        "claims": [], "evidence": [], "warrants": [],
    }]}
    awkward = "配置が読まれ方を可能性として変える。"

    class FakeGenerator:
        def _authoring_generate(self, _claimed, stage, _context, *, payload, **kwargs):
            operation = kwargs["operation_name"]
            if stage == "naturalize_reader_brief_v31":
                text = (
                    "配置が読まれ方を必ず変える。"
                    if operation.endswith("issue_repair") else awkward
                )
                return {"sections": [{
                    "section_id": "s1", "section_result": text, "framing": "",
                    "claims": [], "evidence": [], "warrants": [],
                }]}
            if operation.endswith("issue_repair"):
                return {"sections": [{
                    "section_id": "s1", "meaning_preserved": "marginal",
                    "natural_japanese": True, "internal_language_absent": True,
                "internal_issues": [],
                    "marginal_inferences": [],
                    "japanese_issues": [], "reason": "可能性が必然性へ強められた",
                }]}
            return {"sections": [{
                "section_id": "s1", "meaning_preserved": "true",
                "natural_japanese": False, "internal_language_absent": True,
                "internal_issues": [],
                "marginal_inferences": [],
                "japanese_issues": [{
                    "field": "section_result", "item_id": "", "excerpt": awkward,
                    "reason": "不自然", "suggested_rewrite": "配置によって読まれ方が変わりうる。",
                }], "reason": "不自然",
            }]}

    projected, report = WorkerRunner._authoring_naturalize_reader_content_brief_v31(
        FakeGenerator(), object(), {}, raw
    )
    assert projected["sections"][0]["section_result"] == (
        "配置が読まれ方を必ず変える。"
    )
    assert report["focused_repair_audit"][0]["meaning_preserved"] == "marginal"
    assert report["marginal_audit_decisions"][0]["section_id"] == "s1"
    _, reviews = WorkerRunner._authoring_v31_reader_brief_marginal_reviews([report])
    assert reviews[0]["category"] == "reader_brief_marginal_decision"


def test_meaning_card_separates_editorial_notes_from_substantive_limits() -> None:
    schema = STAGE_SCHEMAS["extract_paragraph_meaning_v31"]["properties"][
        "paragraphs"
    ]["items"]
    card = {
        "paragraph_id": "p1", "topic": "雑誌", "central_judgment": "配置の効果を問う",
        "supporting_observations": ["異なる作品が隣り合う"],
        "inferences": [], "limits": ["因果の向きは確定していない"],
        "editorial_only": ["書誌の確認は未了"], "citation_role": "none",
    }
    _validate_schema(card, schema)
    with pytest.raises(FukamiruError):
        incomplete = {
            key: value for key, value in card.items() if key != "editorial_only"
        }
        _validate_schema(incomplete, schema)


def test_identity_only_optional_conclusion_reference_is_not_draft_quota() -> None:
    context = {
        "research_problem": {"research_question": "作品と媒体はどう関わるか"},
        "argument_nodes": [{
            "id": "e-theory", "node_type": "evidence", "claim_id": "c1",
            "provenance": {"source_ids": ["source-theory"]},
        }],
        "evidence_context": [{
            "id": "source-theory", "allowed_use": "attributed_scholarship_citation",
            "reference_id": "ref-theory", "substantive_support_verified": False,
        }],
        "reference_entries": [{"reference_id": "ref-theory"}],
    }
    conclusion = {
        "section_id": "conclusion", "article_role": "conclusion",
        "claim_ids": ["c1"], "evidence_ids": ["e-theory"],
    }
    projected = WorkerRunner._authoring_draft_contracts_with_scholarship(
        context, [conclusion]
    )
    assert projected[0]["evidence_ids"] == []
    assert projected[0]["required_scholarship_evidence_ids"] == []
def test_compact_claim_citation_brief_keeps_section_judgment_and_source_binding():
    brief = WorkerRunner._authoring_compact_claim_citation_brief_v31(
        {
            "section_id": "s10", "article_role": "body",
            "section_result": "生活の細部は選択された表象である。",
            "claims": [{"id": "c1", "text": "描写には選択がある。", "internal_note": "omit"}],
            "evidence": [{
                "id": "e1", "text": "作品の描写", "source_ids": ["src1"],
                "internal_note": "omit",
            }],
            "warrants": [],
        },
        [{
            "section_id": "s10", "evidence_id": "e1", "reference_id": "ref1",
            "author": "山田花子", "work": "論考", "internal_note": "omit",
        }],
    )
    assert brief["section_result"] == "生活の細部は選択された表象である。"
    assert brief["claims"] == [{"id": "c1", "text": "描写には選択がある。"}]
    assert brief["evidence"][0]["source_ids"] == ["src1"]
    assert brief["citation_anchors"][0]["reference_id"] == "ref1"
    assert "internal_note" not in str(brief)
