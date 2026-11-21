from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

from fukamiru.authoring.codex import (
    STAGE_GUIDANCE,
    STAGE_SCHEMAS,
    CodexAuthoringAdapter,
    describe_authoring_request,
)
from fukamiru.authoring.evidence_contracts import infer_disposition
from fukamiru.documents.japanese_surface import JapaneseSurfaceQualityGate
from fukamiru.errors import FukamiruError, NonRetryableFukamiruError
from fukamiru.revisions import prose_redraft as prose
from fukamiru.storage.runs import RunRepository
from fukamiru.worker import submission_validation as final_worker_v26
from fukamiru.worker.runner import ClaimedNode, HandlerResult, WorkerRunner
from fukamiru.workflow.graph import EdgeSpec, WorkflowDefinition
from fukamiru.workflow.templates import authoring_workflow, authoring_workflow_v30

HTML = (
    '<article id="D" lang="ja" data-policy="fixed"><section id="S" role="region">'
    '<h2 id="H">労働と理論</h2><p id="P" class="body" data-claim="claim1">'
    "工程・賃金・分業・承認の描写が観察指標として用いられる。"
    '<sup id="N"><a id="A" href="#F" role="doc-noteref">1</a></sup>'
    "作業の手順と役割分担の関係、技能の評価と賃金の違いを検討する。</p>"
    '<p id="Q">ラマールの<cite id="C" data-reference-id="ref">『アニメ・マシーン』</cite>'
    "は、観察単位を拡大するために用いられる。作品が置かれる媒体の関係を考える。</p>"
    '<p id="T">セネットは手仕事と思考を切り離せないと論じる。制度は第三者に採用された。</p>'
    '<p id="PROTECTED">人間が保護した本文。</p></section>'
    '<section id="NOTES" data-citation-section="evidence-footnotes-v1">'
    '<h2 id="NH">脚注</h2><ol id="OL"><li id="F" data-reference-id="ref">'
    '1. 著者、出典。<a id="BACK" href="#A">戻る</a></li></ol></section>'
    '<section id="REFS" data-reference-section="consulted-generated-v22">'
    '<h2 id="RH">参考文献</h2><p id="R">著者『書名』2026年。</p></section></article>'
)


def test_exact_rows_reorders_unique_ids_without_changing_content():
    rows = [{"paragraph_id": "Q", "text_slots": ["二文目"]},
            {"paragraph_id": "P", "text_slots": ["一文目"]}]
    assert prose.exact_rows(rows, ["P", "Q"], "checkpoint") == [rows[1], rows[0]]
    with pytest.raises(NonRetryableFukamiruError, match="IDs or order"):
        prose.exact_rows([rows[0], rows[0]], ["P", "Q"], "checkpoint")


def test_targeted_format_repair_retries_only_the_bad_paragraph():
    runner = WorkerRunner.__new__(WorkerRunner)
    calls = []

    def retry(_claimed, *, stage, context, payload, operation_name,
              checkpoint_variant, repair_prose_format):
        del stage, context, checkpoint_variant, repair_prose_format
        calls.append((operation_name, [p["paragraph_id"] for p in payload["paragraphs"]]))
        return {"paragraphs": [{"paragraph_id": "Q", "text_slots": ["改稿。", ""]}]}

    runner._authoring_invoke = retry
    result = WorkerRunner._authoring_reconcile_prose_response(
        runner, claimed(), stage="polish_final_japanese_v31", context={},
        payload={"paragraphs": [
            {"paragraph_id": "P", "text_slots": ["元稿。"]},
            {"paragraph_id": "Q", "text_slots": ["原文。", ""]},
        ]}, operation_name="polish:S",
        result={"paragraphs": [
            {"paragraph_id": "Q", "text_slots": ["スロット不足。"]},
            {"paragraph_id": "P", "text_slots": ["改善。"]},
        ]}, allow_retry=True,
    )
    assert result["paragraphs"] == [
        {"paragraph_id": "P", "text_slots": ["改善。"]},
        {"paragraph_id": "Q", "text_slots": ["改稿。", ""]},
    ]
    assert calls == [("polish:S:format:Q", ["Q"])]


def test_targeted_format_repair_retains_valid_sibling_when_retry_still_has_bad_slots():
    runner = WorkerRunner.__new__(WorkerRunner)
    calls = []

    def retry(_claimed, *, stage, context, payload, operation_name,
              checkpoint_variant, repair_prose_format):
        del stage, context, checkpoint_variant, repair_prose_format
        calls.append((operation_name, [p["paragraph_id"] for p in payload["paragraphs"]]))
        return {"paragraphs": [{"paragraph_id": "Q", "text_slots": ["まだ不足。"]}]}

    runner._authoring_invoke = retry
    result = WorkerRunner._authoring_reconcile_prose_response(
        runner, claimed(), stage="polish_final_japanese_v31", context={},
        payload={"paragraphs": [
            {"paragraph_id": "P", "text_slots": ["元稿。"]},
            {"paragraph_id": "Q", "text_slots": ["原文。", ""]},
        ]}, operation_name="polish:S",
        result={"paragraphs": [
            {"paragraph_id": "Q", "text_slots": ["不足。"]},
            {"paragraph_id": "P", "text_slots": ["改善。"]},
        ]}, allow_retry=True,
    )
    assert result["paragraphs"] == [
        {"paragraph_id": "P", "text_slots": ["改善。"]},
        {"paragraph_id": "Q", "text_slots": ["原文。", ""]},
    ]
    assert calls == [("polish:S:format:Q", ["Q"])]


def test_internal_scaffold_is_targeted_for_automatic_repair_but_self_citation_is_not():
    findings = WorkerRunner._authoring_reader_facing_v31_findings([{
        "section_id": "S",
        "paragraphs": [{
            "paragraph_id": "P",
            "text": "入力メモでは作品を論じる。自著『作品論』も参照する。",
        }],
    }])
    assert len(findings) == 1
    assert findings[0]["code"] == "reader_facing_internal_scaffold"
    assert findings[0]["target_ids"] == ["P"]
    assert "自著" not in findings[0]["matched_phrases"]
    assert infer_disposition(findings[0]) == "auto_repair"






def claimed() -> ClaimedNode:
    return ClaimedNode(
        node_run_id="node",
        run_id="run",
        node_id="redraft_body_prose",
        attempt=1,
        workflow_name="manuscript_generator",
        workflow_version="31",
        resource_lane="codex_remote",
        lease_owner="test",
        project_id=None,
        inputs={},
    )


def invoke_fixture(stage, payload, failure=""):
    if stage == "review_paragraph_transitions_v32":
        by_id = {p["paragraph_id"]: p for p in payload["paragraphs"]}
        return {
            "boundaries": [{
                "boundary_id": b["boundary_id"],
                "left_paragraph_id": b["left_paragraph_id"],
                "right_paragraph_id": b["right_paragraph_id"],
                "relation": "topic_shift", "carried_term": "", "connective": "",
                "left_evidence": by_id[b["left_paragraph_id"]]["visible_text"][:20],
                "right_evidence": by_id[b["right_paragraph_id"]]["visible_text"][:20],
                "central_term_continuity": True, "connective_appropriate": True,
                "reason": "話題の移行が明瞭。",
            } for b in payload["boundaries"]],
            "paragraphs": [], "meaning_preserved": True, "meaning_reason": "変更なし。",
        }
    if stage == "classify_quote_provenance_v31":
        return {"judgments": [{
            "paragraph_id": item["paragraph_id"], "literal": item["literal"],
            "author_analytical_question": True,
            "direct_source_quote": False,
            "reason": "筆者が分析のために立てた問い。",
        } for item in payload["quote_candidates"]]}
    if stage == "redraft_section_heading_v31":
        return {"headings": [{
            "section_id": item["section_id"], "heading_id": item["heading_id"],
            "text": item["text"], "reason": "元の見出しは自然。",
        } for item in payload["headings"]]}
    if stage == "audit_section_heading_v31":
        return {"judgments": [{
            "section_id": item["section_id"], "heading_id": item["heading_id"],
            "meaning_preserved": True, "citation_alignment_preserved": True,
            "natural_japanese": True, "reason": "見出しは本文に合う。",
        } for item in payload["headings"]]}
    paragraphs = payload["paragraphs"]
    if stage == "diagnose_body_prose_v31":
        return {
            "paragraphs": [
                {
                    "paragraph_id": p["paragraph_id"],
                    "issues": (
                        [
                            {
                                "issue_id": "issue1",
                                "category": "enumeration"
                                if p["paragraph_id"] == "P"
                                else "writer_agency",
                                "excerpt": p["visible_text"],
                                "description": "関係と主体",
                                "items": ["工程", "賃金", "分業", "承認"]
                                if p["paragraph_id"] == "P"
                                else [],
                                "context_relation": "原文の後続文に関係が説明されている。",
                            }
                        ]
                        if p["paragraph_id"] in {"P", "Q"}
                        else []
                    ),
                }
                for p in paragraphs
            ]
        }
    if stage == "extract_paragraph_meaning_v31":
        return {"paragraphs": [{
            "paragraph_id": p["paragraph_id"],
            "topic": "対象についての判断",
            "central_judgment": "原文の判断を保つ。",
            "supporting_observations": [],
            "inferences": [],
            "limits": [],
            "editorial_only": [],
            "citation_role": "元の脚注の対象を保つ。",
        } for p in payload["paragraphs"]]}
    if stage == "compose_body_prose_v31":
        recovered = [{
            "paragraph_id": p["paragraph_id"],
            "text_slots": list(p["source_text_slots"]),
        } for p in payload["paragraphs"]]
        return invoke_fixture(
            "redraft_body_prose_v31", {"paragraphs": recovered}, failure
        )
    if stage == "polish_final_japanese_v31":
        return {"paragraphs": [{
            "paragraph_id": item["paragraph_id"],
            "text_slots": list(item["text_slots"]),
        } for item in paragraphs]}
    if stage == "redraft_body_prose_v31":
        candidates = [
            {
                "paragraph_id": p["paragraph_id"],
                "text_slots": p["text_slots"],
                "addressed_issue_ids": [],
                "review_reason": "",
            }
            for p in paragraphs
        ]
        for candidate in candidates:
            if candidate["paragraph_id"] == "P":
                candidate["text_slots"] = [
                    "作業の手順を読み、労働者の役割分担がどこまで描かれるかを確かめる。",
                    "さらに、技能に対する承認を読み取り、その評価と賃金を同一視せず、"
                    "両者の違いを検討する。",
                ]
                candidate["addressed_issue_ids"] = ["issue1"]
                if failure == "punctuation":
                    candidate["text_slots"] = [
                        paragraphs[0]["text_slots"][0].replace("・", "、"),
                        paragraphs[0]["text_slots"][1],
                    ]
                if failure == "unchanged":
                    candidate["text_slots"] = paragraphs[0]["text_slots"]
                if failure == "missing_context":
                    candidate["text_slots"] = paragraphs[0]["text_slots"]
                    candidate["review_reason"] = "項目の関係を説明する根拠が足りない。"
                if failure == "slots":
                    candidate["text_slots"] = ["脚注を無視して全体を書き換える。"]
            if candidate["paragraph_id"] == "Q":
                candidate["text_slots"] = [
                    "ラマールの",
                    "を参照し、作品から、それらが置かれた媒体の関係へ観察を広げる。",
                ]
                candidate["addressed_issue_ids"] = ["issue1"]
        return {"paragraphs": candidates}
    if stage == "verify_redraft_claims_v31":
        return {
            "judgments": [
                {
                    "paragraph_id": p["paragraph_id"],
                    "all_changes_admissible": not (
                        failure == "new_claim" and p["paragraph_id"] == "P"
                    ),
                    "changed_claims": ([{
                        "candidate_excerpt": next(
                            item["candidate"]["visible_text"][:12]
                            for item in payload["comparisons"]
                            if item["paragraph_id"] == "P"
                        ),
                        "original_support_excerpt": "",
                        "support_relation": "unsupported",
                        "basis_ids": [], "reasoning": "",
                        "certainty_preserved": True,
                        "citation_alignment_preserved": True,
                        "reason": "候補の因果関係は原文にない。",
                    }] if failure == "new_claim" and p["paragraph_id"] == "P" else []),
                    "reason": "候補の命題を原文と照合した。",
                }
                for p in paragraphs
            ]
        }
    if stage == "audit_predicate_sentences_v31":
        return {"judgments": [{
            "paragraph_id": p["paragraph_id"],
            "sentences": [{
                "candidate_sentence": sentence,
                "predicate_excerpts": [sentence],
                "source_support_excerpt": "",
                "relation": "paraphrase",
                "actor_object_direction_preserved": True,
                "certainty_preserved": True,
                "citation_alignment_preserved": True,
                "reason": "候補文を原文と照合した。",
            } for sentence in prose.candidate_sentences(next(
                item["candidate"]["visible_text"]
                for item in payload["comparisons"]
                if item["paragraph_id"] == p["paragraph_id"]
            ))],
        } for p in paragraphs]}
    if stage == "review_reader_prose_v31":
        return {
            "judgments": [{
                "paragraph_id": p["paragraph_id"],
                "natural_for_reader": True,
                "argument_progresses": True,
                "topic_clear": True,
                "unnatural_spans": [],
                "reason": "自然に読める。",
            } for p in paragraphs],
            "section_flow_acceptable": True, "section_reason": "",
        }
    assert stage == "audit_redrafted_prose_v31"
    return {
        "judgments": [
            {
                "paragraph_id": p["paragraph_id"],
                "content_preserved": failure != "lost_meaning",
                "enumeration_resolved": not (failure == "punctuation" and p["paragraph_id"] == "P"),
                "writer_agency_clear": True,
                "footnote_alignment_preserved": failure != "footnote",
                "natural_japanese": True,
                "analysis_progresses": failure != "analysis_stall",
                "internal_language_absent": failure != "internal_language",
                "remaining_issues": [],
                "reason": "元の項目、留保と脚注の対応を照合した。",
            }
            for p in paragraphs
        ],
        "section_flow_acceptable": failure != "flow",
        "section_reason": "構文が画一化している。" if failure == "flow" else "",
    }


def mock_runner(tmp_path, failure=""):
    calls = []
    runner = SimpleNamespace(
        settings=SimpleNamespace(paths=SimpleNamespace(runtime_root=tmp_path)),
        _string_list=WorkerRunner._string_list,
        _register_artifact=lambda *args, **kwargs: args[2],
    )

    def invoke(_claimed, *, stage, context, payload, operation_name):
        calls.append(stage)
        return invoke_fixture(stage, payload, failure)

    runner._authoring_invoke = invoke
    return runner, calls


def test_dom_edits_preserve_nested_inline_nodes_titles_protected_and_terminal_sections():
    html = HTML.replace(
        "セネットは手仕事と思考を切り離せないと論じる。",
        'セネットは<span id="SP">手仕事と<em id="EM">思考</em>を</span>切り離せないと論じる。',
    )
    source = prose.inventory(html, {"PROTECTED"})
    assert [p["paragraph_id"] for p in source] == ["P", "Q", "T"]
    assert len(source[2]["text_slots"]) == 5
    edits = [
        {"paragraph_id": p["paragraph_id"], "text_slots": list(p["text_slots"])} for p in source
    ]
    edits[2]["text_slots"][0] = "セネットは、"
    edited, validation = prose.apply_edits(html, edits, protected_ids={"PROTECTED"})
    assert prose.manifest(html, {"PROTECTED"}) == prose.manifest(edited, {"PROTECTED"})
    assert validation["changed_paragraph_ids"] == ["T"]
    before, after = ET.fromstring(html), ET.fromstring(edited)
    for node_id in ("N", "C", "PROTECTED", "NOTES", "REFS"):
        assert ET.tostring(before.find(f".//*[@id='{node_id}']")) == ET.tostring(
            after.find(f".//*[@id='{node_id}']")
        )
    # Tail text around a locked footnote remains editable, but its number and link do not.
    assert source[0]["text_slots"] == [
        before.find(".//*[@id='P']").text,
        before.find(".//*[@id='N']").tail,
    ]


def test_named_question_can_be_reworded_without_unlocking_titles_or_dom():
    question = (
        "作品・ジャンルが社会的関係と創作技法を可視化し、配役し、"
        "読者に受容させる仕方をどのように変えてきたのか"
    )
    html = (
        '<article id="D" lang="ja"><section id="S"><h2 id="H">序論</h2>'
        f'<p id="P">問うのは「{question}」である。'
        '<cite id="C">『漫画の歴史』</cite>を参照する。</p>'
        '</section></article>'
    )
    revised_text = (
        "媒体の変化は、漫画に描かれる社会的関係や人物の役割にどう関わったのか。"
        "制作技法や読まれ方には、どのような変化が見られるのか。"
    )
    edited, validation = prose.apply_edits(
        html, [{"paragraph_id": "P", "text_slots": [revised_text, "を参照する。"]}],
        rephrasable_propositions=[question],
    )
    assert validation["changed_paragraph_ids"] == ["P"]
    assert prose.manifest(html) == prose.manifest(edited)
    assert ET.fromstring(edited).find(".//*[@id='C']").text == "『漫画の歴史』"
    with pytest.raises(NonRetryableFukamiruError, match="protected content"):
        prose.apply_edits(
            html, [{"paragraph_id": "P", "text_slots": [revised_text, "別の資料を参照する。"]}],
            rephrasable_propositions=[],
        )


def test_nested_sections_and_reference_descendants_are_not_edited_twice():
    html = HTML.replace('<p id="T">', '<section id="INNER"><h3 id="IH">内節</h3><p id="T">')
    html = html.replace('<p id="PROTECTED">', '</section><p id="PROTECTED">')
    html = html.replace('<p id="R">', '<section id="REFINNER"><h3 id="RIH">書誌</h3><p id="R">')
    html = html.replace("</p></section></article>", "</p></section></section></article>")
    assert [p["paragraph_id"] for p in prose.inventory(html)] == ["P", "Q", "T", "PROTECTED"]


def test_unmarked_terminal_sections_remain_read_only():
    html = (
        '<article id="D" lang="ja"><section id="S">'
        '<h2 id="H">本文</h2><p id="P">筆者の本文。</p></section>'
        '<section id="NOTES"><h2 id="NH">脚注</h2><p id="N">脚注の本文。</p></section>'
        '<section id="REFS"><h2 id="RH">参考文献</h2><p id="R">書誌の本文。</p>'
        "</section></article>"
    )
    assert [p["paragraph_id"] for p in prose.inventory(html)] == ["P"]
    revised, _ = prose.apply_edits(html, [{"paragraph_id": "P", "text_slots": ["改稿した本文。"]}])
    assert prose.manifest(html) == prose.manifest(revised)


def test_fixed_footnotes_and_bibliography_are_resolved_for_semantic_audit():
    citations = prose.citation_context(HTML, ["P", "Q"])
    assert citations[0]["footnotes"][0] == {
        "anchor_id": "A",
        "href": "#F",
        "target_id": "F",
        "target_text": "1. 著者、出典。戻る",
        "reference_id": "ref",
    }
    assert citations[1]["bibliography"][0]["reference_id"] == "ref"


def test_source_blind_composer_receives_slots_and_locked_literals_without_source_prose():
    original = "20世紀の『書名』を読む。筆者は「どこで読むか」を問う。"
    paragraph = {
        "paragraph_id": "P", "section_id": "S", "text_slots": [original],
        "html_template": '<p id="P">{{TEXT:0}}</p>',
    }
    blind = prose.source_blind_paragraphs(
        [paragraph], rephrasable_by_paragraph={"P": ["「どこで読むか」"]}
    )[0]
    assert blind["slot_count"] == 1
    assert blind["protected_literals"] == ["20", "『書名』"]
    assert original not in json.dumps(blind, ensure_ascii=False)
    assert "visible_text" not in blind and "text_slots" not in blind


def test_redraft_contract_preserves_unspecified_causal_direction():
    diagnosis = STAGE_GUIDANCE["diagnose_body_prose_v31"]
    redraft = STAGE_GUIDANCE["redraft_body_prose_v31"]
    audit = STAGE_GUIDANCE["audit_redrafted_prose_v31"]
    assert "診断者がそれを補って確定しない" in diagnosis
    assert "diagnosesは文章上の問題を見つける手掛かり" in redraft
    assert "作用の向きが未確定なのも保持すべき限定" in audit
    assert "原文の問いや可能性を外部的な事実の断定へ" in audit
    assert "節の議論から筆者の解釈として" in audit
    assert "model_judgment_without_direct_statement" in STAGE_GUIDANCE[
        "verify_redraft_claims_v31"
    ]


def test_redraft_hints_exclude_diagnosis_inferred_claim_relation():
    diagnoses = [{
        "paragraph_id": "age",
        "issues": [{
            "issue_id": "I01", "description": "不自然な連語",
            "excerpt": "組織する比重が大きい",
            "context_relation": "格差が行動範囲を強く条件づける。",
        }],
    }]
    hints = prose.redraft_issue_hints(diagnoses)
    assert hints == [{
        "paragraph_id": "age",
        "issues": [{
            "issue_id": "I01", "description": "不自然な連語",
            "excerpt": "組織する比重が大きい",
        }],
    }]
    assert diagnoses[0]["issues"][0]["context_relation"]


def test_composer_uses_original_and_diagnosis_while_audit_stays_independent(tmp_path):
    runner, _ = mock_runner(tmp_path)
    invoke = runner._authoring_invoke

    def inspect(*args, **kwargs):
        if kwargs["stage"] == "compose_body_prose_v31":
            payload = kwargs["payload"]
            assert "context_paragraphs" not in payload
            assert "diagnoses" not in payload
            assert {item["paragraph_id"] for item in payload["paragraphs"]} == {"P", "Q"}
            assert payload["untouched_context_paragraphs"] == [{
                "paragraph_id": "T", "text_slots": [
                    "セネットは手仕事と思考を切り離せないと論じる。制度は第三者に採用された。"
                ],
            }]
            assert all(
                paragraph["source_text_slots"]
                and paragraph["diagnosed_issues"]
                and "context_relation" not in paragraph["diagnosed_issues"][0]
                for paragraph in payload["paragraphs"]
            )
        if kwargs["stage"] == "audit_redrafted_prose_v31":
            payload = kwargs["payload"]
            assert "diagnoses" not in payload
            assert {item["paragraph_id"] for item in payload["comparisons"]} == {"P", "Q", "T"}
        if kwargs["stage"] == "verify_redraft_claims_v31":
            assert "diagnoses" not in kwargs["payload"]
            assert {item["paragraph_id"] for item in kwargs["payload"]["comparisons"]} == {
                "P", "Q"
            }
        return invoke(*args, **kwargs)

    runner._authoring_invoke = inspect
    WorkerRunner._authoring_redraft_body_prose(runner, claimed(), {"proposed_html": HTML})


def test_composer_input_retains_source_and_explicit_issue_provenance():
    source = (
        "筆者が『架空資料の比較分析——『星の記録』における行為と竜』"
        "で論じた「セノグラフィー」は、二者の関係に接続する。"
    )
    paragraphs = [{
        "paragraph_id": "P", "section_id": "S", "text_slots": [source],
        "html_template": '<p id="P">{{TEXT:0}}</p>',
    }]
    diagnoses = [{
        "paragraph_id": "P", "issues": [{
            "issue_id": "I01", "excerpt": "関係に接続する",
            "description": "二者がどう関わるかが不明確",
            "context_relation": "資料は相互に作用すると推測できる。",
        }],
    }]
    edited = prose.compose_edit_paragraphs(paragraphs, diagnoses)
    assert edited[0]["source_text_slots"] == [source]
    assert edited[0]["diagnosed_issues"] == [{
        "issue_id": "I01", "excerpt": "関係に接続する",
        "description": "二者がどう関わるかが不明確",
    }]
    assert edited[0]["writer_attribution_anchors"] == [
        "筆者が『架空資料の比較分析——『星の記録』における行為と竜』で論じた"
    ]
    assert prose.restore_writer_attribution(
        [source], [source.replace("筆者が", "自著")]
    ) == [source]
    assert not prose.compose_edit_paragraphs(paragraphs, [{
        "paragraph_id": "P", "issues": [],
    }])


def test_composer_guidance_keeps_explanatory_prose_and_original_author_agency():
    guidance = STAGE_GUIDANCE["compose_body_prose_v31"]
    assert "節ごとに一度だけ行う最終改稿" in guidance
    assert "書誌同定のみの引用段落に限って採否の読後評価" in guidance
    assert "引用済みの" in guidance and "参照候補" in guidance
    assert "source_text_slotsが現在の本文" in guidance
    assert "修正不要な段落は原文のtext_slotsをそのまま返す" in guidance
    assert "列挙された要素を短い名詞列や抽象語へ圧縮しない" in guidance
    assert "筆者が『書名』で論じた" in guidance
    assert "閉じ括弧と次の開き括弧の間に「・」" in guidance
    assert "曖昧な「接続する」で済ませず" in guidance
    assert "「作品同士の隣接関係」のような名詞へ圧縮しない" in guidance
    assert "誰が欲望し、誰の身体が見られ" in guidance
    assert "「通過儀礼」などの専門用語" in guidance


def test_semantic_audit_records_restrictive_causal_direction_as_marginal():
    paragraph = {
        "paragraph_id": "age",
        "section_id": "age-section",
        "text_slots": ["地域の格差が主人公の行動範囲を組織する比重が大きい。"],
    }
    diagnosis = {"paragraph_id": "age", "issues": []}
    candidate = {
        "paragraph_id": "age",
        "text_slots": ["地域の格差が主人公の行動範囲を狭める。"],
        "addressed_issue_ids": [],
        "review_reason": "",
    }
    audit = {
        "judgments": [{
            "paragraph_id": "age", "content_preserved": False,
            "enumeration_resolved": True, "writer_agency_clear": True,
            "footnote_alignment_preserved": True, "natural_japanese": True,
            "analysis_progresses": True, "internal_language_absent": True,
            "remaining_issues": ["原文では行動範囲を狭めるとは述べていない。"],
            "reason": "相対的な比重を制限方向の因果へ強めている。",
        }],
        "section_flow_acceptable": True,
        "section_reason": "",
    }
    edits, outcomes = prose.select_edits([paragraph], [diagnosis], [candidate], audit)
    assert edits[0]["text_slots"] == candidate["text_slots"]
    assert outcomes[0]["accepted"] is True
    assert outcomes[0]["review_status"] == "marginal"


def test_prose_repair_never_trades_preserved_content_for_claim_score():
    current = {
        "accepted": True, "changed": True, "reasons": ["命題の根拠を再確認する。"],
        "audit": {"content_preserved": True, "footnote_alignment_preserved": True},
        "claim_verification": {"all_changes_admissible": False},
    }
    trial = {
        "accepted": True, "changed": True, "reasons": [],
        "audit": {"content_preserved": False, "footnote_alignment_preserved": True},
        "claim_verification": {"all_changes_admissible": True},
    }
    assert prose.prefer_repaired_prose(current, trial) is False
    unchanged = {
        **current, "changed": False, "reasons": [],
        "claim_verification": {"all_changes_admissible": True},
    }
    assert prose.prefer_repaired_prose(current, unchanged) is False


def test_nonessential_old_enumeration_can_be_omitted_from_accepted_paragraph():
    paragraph = {
        "paragraph_id": "intro", "section_id": "s01",
        "text_slots": [
            "媒体レギュレーションは明示的規則と暗黙の慣行を含み、"
            "作品と記事の配置に関わる。"
        ],
    }
    diagnosis = {"paragraph_id": "intro", "issues": []}
    candidate = {
        "paragraph_id": "intro",
        "text_slots": ["雑誌で何が隣り合うかは、読者が作品に出会う機会を変える。"],
        "addressed_issue_ids": [], "review_reason": "",
    }
    audit = {"judgments": [{
        "paragraph_id": "intro", "content_preserved": True,
        "enumeration_resolved": True, "writer_agency_clear": True,
        "footnote_alignment_preserved": True, "natural_japanese": True,
        "analysis_progresses": True, "internal_language_absent": True,
        "remaining_issues": [], "reason": "",
    }], "section_flow_acceptable": True, "section_reason": ""}

    edits, outcomes = prose.select_edits(
        [paragraph], [diagnosis], [candidate], audit
    )

    assert edits[0]["text_slots"] == candidate["text_slots"]
    assert outcomes[0]["accepted"] is True
    assert "同じ段落へ残すことは求めない" in STAGE_GUIDANCE[
        "audit_redrafted_prose_v31"
    ]


def test_claim_verification_fails_closed_on_unsupported_or_invalid_evidence():
    original = {"paragraph_id": "age", "visible_text": "格差は行動範囲と課題に大きく関わる。"}
    candidate = {"age": {
        "paragraph_id": "age", "visible_text": "格差が行動範囲を狭める。",
    }}
    result = {"judgments": [{
        "paragraph_id": "age", "all_changes_admissible": False,
        "changed_claims": [{
            "candidate_excerpt": "行動範囲を狭める",
            "original_support_excerpt": "格差は行動範囲と課題に大きく関わる",
            "support_relation": "unsupported", "basis_ids": [], "reasoning": "",
            "certainty_preserved": False,
            "citation_alignment_preserved": True,
            "reason": "制限する方向は原文にない。",
        }],
        "reason": "内容を保持した。",
    }]}
    issues = prose.claim_verification_issues([original], candidate, result)
    assert any("断定が強く" in reason for reason in issues["age"])
    result["judgments"][0]["changed_claims"][0]["support_relation"] = "paraphrase"
    result["judgments"][0]["changed_claims"][0]["certainty_preserved"] = True
    result["judgments"][0]["changed_claims"][0]["original_support_excerpt"] = ""
    assert any("言い換えを支持する原文" in reason for reason in (
        prose.claim_verification_issues([original], candidate, result)["age"]
    ))


def test_inferred_concretization_is_accepted_only_with_trace_and_unchanged_certainty():
    original = {
        "paragraph_id": "age", "section_id": "S",
        "visible_text": "格差は行動範囲と課題に関わる。",
    }
    candidates = {"age": {
        "paragraph_id": "age", "visible_text": "格差は通学先の選択にも関わりうる。",
    }}
    change = {
        "candidate_excerpt": "通学先の選択にも関わりうる",
        "original_support_excerpt": "格差は行動範囲と課題に関わる",
        "support_relation": "model_judgment_without_direct_statement",
        "basis_ids": ["claim1"],
        "reasoning": "行動範囲という抽象表現を、進学先の選択という例として解釈した。",
        "certainty_preserved": True,
        "citation_alignment_preserved": True,
        "reason": "本文で可能性として示した。",
    }
    verification = {"judgments": [{
        "paragraph_id": "age", "all_changes_admissible": True,
        "changed_claims": [change], "reason": "推論として妥当。",
    }]}
    assert prose.claim_verification_issues(
        [original], candidates, verification, {"claim1"}
    ) == {}
    records = prose.inference_records([original], verification, {"age"})
    assert records[0]["anchor_excerpt"] in candidates["age"]["visible_text"]
    assert records[0]["support_relation"] == "model_judgment_without_direct_statement"
    change["certainty_preserved"] = False
    assert prose.claim_verification_issues(
        [original], candidates, verification, {"claim1"}
    )["age"]
    change["certainty_preserved"] = True
    change["basis_ids"] = ["invented-source"]
    assert "未知の根拠ID" in prose.claim_verification_issues(
        [original], candidates, verification, {"claim1"}
    )["age"][0]


def test_reader_review_is_an_independent_gate():
    paragraph = {"paragraph_id": "P"}
    review = {
        "judgments": [{
            "paragraph_id": "P", "natural_for_reader": False,
            "argument_progresses": False, "topic_clear": True,
            "reason": "定義文が続き、対象について論じていない。",
        }],
        "section_flow_acceptable": True, "section_reason": "",
    }
    assert "定義文" in prose.reader_review_issues([paragraph], review)["P"][0]


def test_reader_review_reports_exact_unnatural_noun_connection():
    paragraph = {"paragraph_id": "P", "visible_text": "漫画形式が隣接文化を変える。"}
    review = {"judgments": [{
        "paragraph_id": "P", "natural_for_reader": True,
        "argument_progresses": True, "topic_clear": True,
        "unnatural_spans": [{
            "excerpt": "隣接文化", "reason": "何に隣接する文化か不明。",
            "intended_referent": "漫画と関わる文化領域",
            "natural_alternative": "漫画と関わる文化領域",
        }], "reason": "",
    }], "section_flow_acceptable": True, "section_reason": ""}
    assert "隣接文化" in prose.reader_review_issues([paragraph], review)["P"][0]
    review["judgments"][0]["unnatural_spans"][0]["excerpt"] = "原稿にない語"
    assert "一致しません" in prose.reader_review_issues([paragraph], review)["P"][0]


def test_changed_candidate_is_provisional_until_reader_issue_is_repaired():
    paragraph = {
        "paragraph_id": "P", "section_id": "S", "text_slots": ["元の文章。"],
    }
    diagnosis = {"paragraph_id": "P", "issues": [{
        "issue_id": "japanese-1", "category": "japanese_prose",
        "description": "不自然な連語。",
    }]}
    audit = {"judgments": [{
        "paragraph_id": "P", "content_preserved": True,
        "enumeration_resolved": True, "writer_agency_clear": True,
        "footnote_alignment_preserved": True, "natural_japanese": True,
        "analysis_progresses": True, "internal_language_absent": True,
        "remaining_issues": [], "reason": "",
    }], "section_flow_acceptable": True}
    candidate = {
        "paragraph_id": "P", "text_slots": ["修正した文章。"],
        "addressed_issue_ids": [], "review_reason": "",
    }
    edits, outcomes = prose.select_edits(
        [paragraph], [diagnosis], [candidate], audit,
        {"P": ["語のつながりが不自然。"]},
    )
    current = outcomes[0]
    current["reader_review"] = {
        "natural_for_reader": False, "argument_progresses": True,
        "topic_clear": True, "unnatural_spans": [],
    }
    assert edits[0]["text_slots"] == candidate["text_slots"]
    assert current["accepted"] is True
    assert current["unresolved_diagnosed_issue_ids"] == []
    assert current["review_status"] == "marginal"
    assert prose.prose_repair_needed(current, []) is True

    trial = {
        **current, "reader_review": {
            "natural_for_reader": True, "argument_progresses": True,
            "topic_clear": True, "unnatural_spans": [],
        }, "reasons": [], "review_status": "true",
    }
    assert prose.prefer_repaired_prose(current, trial) is True
    assert prose.prose_repair_needed(trial, []) is False


def test_diagnosis_is_checked_by_audit_not_by_reported_issue_ids():
    issue = {"issue_id": "japanese-1", "category": "japanese_prose"}
    assert prose.diagnosed_issue_resolution(
        [issue], {"natural_japanese": False}, changed=True,
    )[0]["resolved"] is False
    assert prose.diagnosed_issue_resolution(
        [issue], {"natural_japanese": True}, changed=True,
    )[0]["resolved"] is True
    assert prose.section_flow_repair_ids([
        {"paragraph_id": "P", "reader_review": {"natural_for_reader": True}},
        {"paragraph_id": "Q", "reader_review": {"natural_for_reader": True}},
        {"paragraph_id": "T", "reader_review": {"natural_for_reader": True}},
    ], {"section_flow_acceptable": False}) == {"P", "T"}


def test_sentence_audit_covers_every_candidate_sentence_and_causal_strength():
    original = {"paragraph_id": "age", "visible_text": "年齢は行動範囲との関係で比重が大きい。"}
    candidate_text = "年齢は行動範囲を狭める。読者はそれを知る。"
    candidate = {"age": {"visible_text": candidate_text}}
    verification = {"judgments": [{
        "paragraph_id": "age", "all_changes_admissible": True,
        "changed_claims": [], "reason": "問題なし",
    }]}
    sentence = prose.candidate_sentences(candidate_text)[0]
    audit = {"judgments": [{"paragraph_id": "age", "sentences": [{
        "candidate_sentence": sentence,
        "predicate_excerpts": ["狭める"],
        "source_support_excerpt": "行動範囲との関係で比重が大きい",
        "relation": "paraphrase",
        "actor_object_direction_preserved": False,
        "certainty_preserved": False,
        "citation_alignment_preserved": True,
        "reason": "比重の説明を制限方向の断定に変えた。",
    }]}]}
    issues = prose.predicate_sentence_issues([original], candidate, audit, verification)
    assert any("全文" in value for value in issues["age"])
    assert any("actor_object_direction" in value for value in issues["age"])
    assert any("certainty" in value for value in issues["age"])




def test_redraft_routes_through_one_heading_edit_to_final_validations():
    workflow = authoring_workflow()
    redraft = next(node for node in workflow.nodes if node.id == "redraft_body_prose")
    assert redraft.retry.max_attempts == 1
    assert "verify_prose_completion" not in {node.id for node in workflow.nodes}
    assert workflow.next_nodes("redraft_body_prose", "success") == [
        "finalize_section_headings"
    ]
    assert set(workflow.next_nodes("finalize_section_headings", "success")) == {
        "validate_reference_quality", "validate_footnoted_references",
        "validate_footnoted_structure", "validate_footnoted_coherence",
    }
    assert workflow.next_nodes("redraft_body_prose", "failure") == []

def test_recomposition_replans_only_sections_with_style_findings():
    calls = []
    def generate(_claimed, _stage, _context, *, payload):
        calls.append(payload)
        return {"sections": [{
            "section_id": "S2", "paragraphs": [{
                "rhetorical_moves": [], "development_instruction": "新しい段落形成",
            }],
        }]}

    runner = SimpleNamespace(_authoring_generate=generate)
    context = {
        "plan_id": "plan",
        "section_contracts": [
            {"section_id": "S1"}, {"section_id": "S2"},
        ],
        "semantic_paragraph_plan": [
            {"section_id": "S1", "paragraphs": [{"development_instruction": "維持"}]},
            {"section_id": "S2", "paragraphs": [{"development_instruction": "旧計画"}]},
        ],
        "prose_recomposition_feedback_v31": [{
            "section_id": "SEC-plan-S2", "paragraph_id": "P2",
            "reasons": ["不自然な名詞連結"],
        }],
    }
    result = WorkerRunner._authoring_plan_semantic_paragraphs(
        runner, claimed(), context
    ).context
    assert [item["section_id"] for item in calls[0]["section_contracts"]] == ["S2"]
    assert calls[0]["prose_recomposition_feedback_v31"][0]["section_id"] == "S2"
    assert result["prose_recomposition_feedback_v31"][0]["section_id"] == "S2"
    assert result["semantic_paragraph_plan"][0] == context["semantic_paragraph_plan"][0]
    assert result["semantic_paragraph_plan"][1]["paragraphs"][0][
        "development_instruction"
    ] == "新しい段落形成"


def test_authored_short_question_can_be_rephrased_but_direct_quote_stays_locked():
    html = (
        '<article id="D" lang="ja"><section id="S"><h2 id="H">視線</h2>'
        '<p id="P">この議論は「誰が誰を見るか」を問う。'
        '<q id="Q">誰が見ているか</q>という引用は残す。</p></section></article>'
    )
    edit = prose.inventory(html)[0]
    edit["text_slots"][0] = "この議論から、見る者と見られる者の位置がどう変わるかを考える。"
    assert prose.quoted_question_candidates(prose.inventory(html)) == [{
        "paragraph_id": "P", "literal": "「誰が誰を見るか」",
    }]
    with pytest.raises(NonRetryableFukamiruError):
        prose.apply_edits(html, [{
            "paragraph_id": "P", "text_slots": edit["text_slots"],
        }])
    revised, _ = prose.apply_edits(html, [{
        "paragraph_id": "P", "text_slots": edit["text_slots"],
    }], rephrasable_by_paragraph={"P": ["「誰が誰を見るか」"]})
    assert "<q id=\"Q\">誰が見ているか</q>" in revised
    assert "見る者と見られる者" in revised








def test_repair_feedback_keeps_all_distinct_reader_findings_and_section_flow():
    outcome = {
        "paragraph_id": "P", "audit": {}, "claim_verification": {},
        "semantic_issues": [], "diagnosed_issues": [],
    }
    findings = [
        "第一文の主述が不自然。", "第二文の比較水準が違う。",
        "第三文の指示語が不明。", "第四文は論述が進まない。",
        "節全体で前段と後段がつながらない。",
    ]
    feedback = prose.prose_repair_feedback(outcome, ["候補本文。"], findings)
    assert feedback["reader_issues"] == findings


def test_retry_candidate_only_needs_expensive_claim_checks_when_quality_can_improve():
    current = {
        "changed": True,
        "audit": {
            "content_preserved": True, "footnote_alignment_preserved": True,
            "natural_japanese": False, "remaining_issues": ["語法が不自然。"],
        },
        "reader_review": {
            "natural_for_reader": False, "argument_progresses": True,
            "topic_clear": True, "unnatural_spans": [{"excerpt": "不自然"}],
        },
        "section_flow_repair": True,
    }
    trial_audit = dict(current["audit"])
    trial_reader = dict(current["reader_review"])
    assert not prose.prose_candidate_worth_verifying(
        current, trial_audit, trial_reader, section_flow_repair=True,
        changed=True,
    )
    improved_reader = {**trial_reader, "natural_for_reader": True, "unnatural_spans": []}
    assert prose.prose_candidate_worth_verifying(
        current, trial_audit, improved_reader, section_flow_repair=False,
        changed=True,
    )
    assert not prose.prose_candidate_worth_verifying(
        current, {**trial_audit, "content_preserved": False}, improved_reader,
        section_flow_repair=False, changed=True,
    )





def test_heading_text_can_change_without_moving_nodes_or_citations():
    headings = prose.heading_inventory(HTML, {"PROTECTED"})
    assert [item["heading_id"] for item in headings] == ["H"]
    revised, changed = prose.apply_heading_edits(
        HTML, [{"heading_id": "H", "text": "労働を描く理論"}], {"PROTECTED"}
    )
    assert changed == ["H"]
    assert prose.manifest(HTML, {"PROTECTED"}, {"H"}) == prose.manifest(
        revised, {"PROTECTED"}, {"H"}
    )
    assert ET.fromstring(revised).find(".//*[@id='NH']").text == "脚注"
    assert ET.fromstring(revised).find(".//*[@id='RH']").text == "参考文献"
    sourced, sourced_changes = prose.apply_heading_edits(
        HTML, [{"heading_id": "H", "text": "『アニメ・マシーン』と労働"}],
        {"PROTECTED"},
    )
    assert sourced_changes == ["H"]
    assert ET.fromstring(sourced).find(".//*[@id='H']").text == (
        "『アニメ・マシーン』と労働"
    )
    with pytest.raises(NonRetryableFukamiruError):
        prose.apply_heading_edits(
            HTML, [{"heading_id": "H", "text": "『新しい本』と労働"}]
        )










def test_success_saves_original_dom_revised_and_report_and_updates_validation_candidate(tmp_path):
    runner, calls = mock_runner(tmp_path)
    context = {
        "proposed_html": HTML,
        "reference_rendered_html_v26": HTML,
        "human_protected_block_ids": ["PROTECTED"],
        "claims": [{"id": "claim1"}],
    }
    result = WorkerRunner._authoring_redraft_body_prose(runner, claimed(), context)
    report = result.context["prose_redraft_report_v31"]
    revised = result.context["proposed_html"]
    assert report["status"] == "completed"
    assert result.context["claims"] == context["claims"]
    assert result.context["reference_rendered_html_v26"] == revised
    assert result.context["manuscript_surface_quality_gate"]["target_html_sha256"] == prose.digest(
        revised
    )
    assert Path(report["original_path"]).read_text(encoding="utf-8") == HTML
    assert Path(report["revised_path"]).read_text(encoding="utf-8") == revised
    assert json.loads(Path(report["dom_manifest_path"]).read_text(encoding="utf-8")) == (
        prose.manifest(HTML, {"PROTECTED"})
    )
    assert (
        json.loads(Path(report["report_path"]).read_text(encoding="utf-8"))["status"] == "completed"
    )
    assert set(report["representative_examples"]) == {"enumeration", "writer_agency"}
    assert report["changed_paragraph_ids"] == ["P", "Q"]
    assert calls == ["compose_body_prose_v31"]
    assert report["quality_assessment"] == "not_llm_audited"
    assert "制度は第三者に採用された。" in revised
    prose.validate_handoff(result.context)
    with pytest.raises(NonRetryableFukamiruError, match="stale"):
        prose.validate_handoff({**result.context, "proposed_html": HTML})
    with pytest.raises(NonRetryableFukamiruError, match="stale"):
        prose.validate_handoff({
            **result.context,
            "prose_redraft_inference_ledger_v31": [{"id": "stale"}],
        })


def test_v32_prose_redraft_keeps_generated_footnote_before_punctuation(tmp_path):
    html = HTML.replace('id="N"', 'id="FNREF-N"').replace(
        "</sup>作業の手順", "</sup>、作業の手順"
    )
    runner, _ = mock_runner(tmp_path)
    result = WorkerRunner._authoring_redraft_body_prose(
        runner,
        replace(claimed(), workflow_version="32"),
        {"proposed_html": html, "human_protected_block_ids": ["PROTECTED"]},
    )
    paragraph = next(
        item for item in ET.fromstring(result.context["proposed_html"]).iter("p")
        if item.get("id") == "P"
    )
    marker = next(paragraph.iter("sup"))
    assert marker.tail[:1] in "、。"
    assert prose.manifest(html, {"PROTECTED"}) == prose.manifest(
        result.context["proposed_html"], {"PROTECTED"}
    )
    prose.validate_handoff(result.context)


def test_one_pass_edits_each_section_once_without_any_llm_audit(tmp_path):
    html = HTML.replace(
        '<section id="NOTES"',
        '<section id="S2"><h2 id="H2">次節</h2>'
        '<p id="P2">対象と結論の関係を説明する。</p></section>'
        '<section id="NOTES"',
    )
    runner, calls = mock_runner(tmp_path)
    result = WorkerRunner._authoring_redraft_body_prose(
        runner, claimed(), {"proposed_html": html, "human_protected_block_ids": ["PROTECTED"]}
    )
    report = result.context["prose_redraft_report_v31"]
    assert calls == ["compose_body_prose_v31", "compose_body_prose_v31"]
    assert [item["composition_call_count"] for item in report["section_reviews"]] == [1, 1]
    assert report["quality_assessment"] == "not_llm_audited"
    assert report["final_naturalness_reviewed_count"] == 0
    assert report["inference_ledger"] == []
    assert report["changed_heading_ids"] == []
    assert prose.manifest(html, {"PROTECTED"}) == prose.manifest(
        result.context["proposed_html"], {"PROTECTED"}
    )


def test_v32_rejects_new_reference_candidate_prose_without_losing_citation(tmp_path):
    runner, calls = mock_runner(tmp_path)
    original_invoke = runner._authoring_invoke

    def introduce_scaffold(*args, **kwargs):
        result = original_invoke(*args, **kwargs)
        if kwargs["stage"] == "compose_body_prose_v31":
            row = next(item for item in result["paragraphs"]
                       if item["paragraph_id"] == "Q")
            row["text_slots"] = [
                "この仮説を考えるための理論的な参照候補としてラマールの",
                "がある。適用可能性は未検証である。",
            ]
        return result

    runner._authoring_invoke = introduce_scaffold
    result = WorkerRunner._authoring_redraft_body_prose(
        runner, replace(claimed(), workflow_version="32"),
        {"proposed_html": HTML, "human_protected_block_ids": ["PROTECTED"]},
    )
    revised = result.context["proposed_html"]
    report = result.context["prose_redraft_report_v31"]
    source_q = ET.fromstring(HTML).find(".//*[@id='Q']")
    revised_q = ET.fromstring(revised).find(".//*[@id='Q']")
    assert ET.tostring(revised_q) == ET.tostring(source_q)
    assert "参照候補" not in revised
    assert "P" in report["changed_paragraph_ids"]
    assert "Q" not in report["changed_paragraph_ids"]
    assert report["reverted_scaffold_candidates"] == [{
        "paragraph_id": "Q",
        "introduced_phrases": ["参照候補として"],
        "reason": "改稿が読者向け本文へ作業用語を新たに加えたため元稿を保持しました。",
    }]
    assert calls == ["compose_body_prose_v31", "extract_paragraph_meaning_v31",
                     "review_paragraph_transitions_v32"]
    assert prose.manifest(HTML, {"PROTECTED"}) == prose.manifest(
        revised, {"PROTECTED"}
    )


def test_one_pass_keeps_locked_quote_without_classifying_or_regenerating(tmp_path):
    html = HTML.replace(
        "工程・賃金・分業・承認の描写が観察指標として用いられる。",
        "この議論は「誰が誰を見るか」を問う。工程と賃金の関係を読む。",
    )
    runner, calls = mock_runner(tmp_path)
    original_invoke = runner._authoring_invoke

    def invoke(*args, **kwargs):
        result = original_invoke(*args, **kwargs)
        result["paragraphs"][0]["text_slots"][0] = "見る者と見られる者の関係を考える。"
        return result

    runner._authoring_invoke = invoke
    result = WorkerRunner._authoring_redraft_body_prose(
        runner, claimed(), {"proposed_html": html}
    )
    assert calls == ["compose_body_prose_v31"]
    assert "「誰が誰を見るか」" in result.context["proposed_html"]
    assert any(
        item["paragraph_id"] == "P"
        for item in result.context["prose_redraft_report_v31"]["unresolved"]
    )


def test_single_pass_format_recovery_uses_no_second_model_call():
    runner = WorkerRunner.__new__(WorkerRunner)
    runner._authoring_invoke = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("A second model call is forbidden")
    )
    result = WorkerRunner._authoring_reconcile_prose_response(
        runner, claimed(), stage="compose_body_prose_v31", context={},
        payload={"composition_strategy": "single_pass", "paragraphs": [
            {"paragraph_id": "P", "slot_count": 1, "source_text_slots": ["原文P"]},
            {"paragraph_id": "Q", "slot_count": 2, "source_text_slots": ["原文Q", ""]},
        ]}, operation_name="compose:S", result={"paragraphs": [
            {"paragraph_id": "P", "text_slots": ["改善P"],
             "addressed_issue_ids": [], "review_reason": ""},
            {"paragraph_id": "Q", "text_slots": ["スロット不足"],
             "addressed_issue_ids": [], "review_reason": ""},
        ]}, allow_retry=True,
    )
    assert result["paragraphs"][0]["text_slots"] == ["改善P"]
    assert result["paragraphs"][1]["text_slots"] == ["原文Q", ""]
    assert "スロット数" in result["paragraphs"][1]["review_reason"]


def test_redraft_uses_compact_card_for_rendered_section_id(tmp_path):
    runner, _ = mock_runner(tmp_path)
    original_invoke = runner._authoring_invoke
    html = HTML.replace('<section id="S"', '<section id="SEC-plan-s01"')
    card = {
        "section_id": "s01", "section_result": "作業と評価の関係を読む。",
        "framing": "工程・賃金・分業・承認の範囲で作業を論じる。",
        "claims": [{"id": "claim1", "text": "作業と評価を区別する。"}],
        "evidence": [], "warrants": [], "citation_anchors": [],
    }
    inspected = []

    def invoke(*args, **kwargs):
        if kwargs["stage"] == "compose_body_prose_v31":
            payload = kwargs["payload"]
            assert payload["section_id"] == "SEC-plan-s01"
            assert payload["supplied_material"]["section_result"] == card["section_result"]
            assert payload["supplied_material"]["draft_inference_ledger"] == [{
                "section_id": "s01", "basis_ids": ["basis1"],
            }]
            assert "UNRELATED_GLOBAL_CLAIM" not in json.dumps(
                payload["supplied_material"], ensure_ascii=False
            )
            inspected.append(kwargs["stage"])
            assert payload["section_scope"]["section_result"] == card["section_result"]
            assert payload["section_scope"]["framing"] == card["framing"]
            assert [item["paragraph_id"] for item in payload["paragraphs"]] == [
                "P", "Q", "T",
            ]
        return original_invoke(*args, **kwargs)

    runner._authoring_invoke = invoke
    result = WorkerRunner._authoring_redraft_body_prose(runner, claimed(), {
        "proposed_html": html,
        "human_protected_block_ids": ["PROTECTED"],
        "section_contracts": [{"section_id": "s01", "claim_ids": ["claim1"]}],
        "authoring_compact_claim_citation_v31": {"s01": card},
        "authoring_inference_ledger_v31": [{
            "section_id": "s01", "basis_ids": ["basis1"],
        }],
        "claims": [{"id": "unrelated", "text": "UNRELATED_GLOBAL_CLAIM"}],
    })
    assert inspected == ["compose_body_prose_v31"], result.context[
        "prose_redraft_report_v31"
    ]["section_reviews"]


def test_final_editor_does_not_receive_bibliographic_only_audit_metadata(tmp_path):
    runner, _ = mock_runner(tmp_path)
    original_invoke = runner._authoring_invoke
    html = HTML.replace('<section id="S"', '<section id="SEC-plan-s01"')
    card = {
        "section_id": "s01", "claims": [{
            "id": "claim", "text": "作品を比較する。", "verification_status": "unverified",
        }],
        "evidence": [{
            "id": "theory", "text": "ラマールの理論を参照する場合、適用は未検証。",
            "source_ids": ["annotation-citation:source"],
            "verification_status": "unverified",
        }, {
            "id": "annotation-citation-node:source", "text": "参照候補。",
            "source_ids": ["annotation-citation:source"],
            "verification_status": "unverified",
        }, {
            "id": "observation", "text": "誌面の配置を比較する。",
            "source_ids": ["span"], "verification_status": "unverified",
        }],
        "warrants": [],
        "citation_anchors": [{
            "evidence_id": "theory", "author": "ラマール",
            "work": "アニメ・マシーン", "substantive_support_verified": False,
        }],
    }

    def inspect(*args, **kwargs):
        if kwargs["stage"] == "compose_body_prose_v31":
            material = kwargs["payload"]["supplied_material"]
            assert [item["id"] for item in material["evidence"]] == ["observation"]
            assert "verification_status" not in material["claims"][0]
            assert "verification_status" not in material["evidence"][0]
            assert "substantive_support_verified" not in material[
                "citation_anchors"
            ][0]
        return original_invoke(*args, **kwargs)

    runner._authoring_invoke = inspect
    WorkerRunner._authoring_redraft_body_prose(runner, claimed(), {
        "proposed_html": html,
        "authoring_compact_claim_citation_v31": {"s01": card},
    })
    assert card["evidence"][0]["verification_status"] == "unverified"


def test_v32_reader_review_restores_cited_paragraph_that_stops_arguing(tmp_path):
    runner, calls = mock_runner(tmp_path)
    original_invoke = runner._authoring_invoke
    html = HTML.replace('<section id="S"', '<section id="SEC-plan-s01"')

    def review(*args, **kwargs):
        if kwargs["stage"] == "review_reader_prose_v31":
            return {
                "judgments": [{
                    "paragraph_id": "Q", "natural_for_reader": False,
                    "argument_progresses": False, "topic_clear": True,
                    "unnatural_spans": [{
                        "excerpt": "検討するための文献",
                        "reason": "引用の役割を説明せず作業予定を述べる。",
                        "intended_referent": "ラマールの視角",
                        "natural_alternative": "媒体の関係を読む視角",
                    }],
                    "reason": "文献を引用しているのに論へ用いていない。",
                }],
                "section_flow_acceptable": True,
                "section_reason": "",
            }
        return original_invoke(*args, **kwargs)

    runner._authoring_invoke = review
    result = WorkerRunner._authoring_redraft_body_prose(
        runner, replace(claimed(), workflow_version="32"), {
            "proposed_html": html,
            "authoring_compact_claim_citation_v31": {"s01": {
                "section_id": "s01",
                "citation_anchors": [{
                    "evidence_id": "theory", "substantive_support_verified": False,
                }],
            }},
            "authoring_paragraph_citation_lineage": [{
                "output_block_id": "Q",
                "argument_evidence_node_ids": ["theory"],
            }],
        },
    )
    report = result.context["prose_redraft_report_v31"]
    original_q = ET.fromstring(html).find(".//*[@id='Q']")
    revised_q = ET.fromstring(result.context["proposed_html"]).find(".//*[@id='Q']")
    assert ET.tostring(revised_q) == ET.tostring(original_q)
    assert report["citation_reader_reviews"][0]["rejected_paragraph_ids"] == ["Q"]
    assert report["reverted_reader_candidates"][0]["paragraph_id"] == "Q"
    assert calls == ["compose_body_prose_v31", "extract_paragraph_meaning_v31",
                     "review_paragraph_transitions_v32"]
    assert prose.manifest(html) == prose.manifest(result.context["proposed_html"])


def test_v32_verification_note_is_removed_after_reader_review_restores_old_paragraph(tmp_path):
    runner, _calls = mock_runner(tmp_path)
    original_invoke = runner._authoring_invoke
    note = "ただし、原典との対応は未確認である。"
    html = HTML.replace("作品が置かれる媒体の関係を考える。", note)

    def invoke(*args, **kwargs):
        result = original_invoke(*args, **kwargs)
        if kwargs["stage"] == "compose_body_prose_v31":
            source = next(p for p in kwargs["payload"]["paragraphs"] if p["paragraph_id"] == "Q")
            row = next(p for p in result["paragraphs"] if p["paragraph_id"] == "Q")
            row["text_slots"] = list(source["source_text_slots"])
            row["text_slots"][0] = "比較に使うラマールの"
        if kwargs["stage"] == "review_reader_prose_v31":
            for row in result["judgments"]:
                row["natural_for_reader"] = False
        if kwargs["stage"] == "extract_paragraph_meaning_v31":
            assert kwargs["payload"]["purpose"] == "route_verification_notes_to_human_review"
            for row in result["paragraphs"]:
                if row["paragraph_id"] == "Q":
                    row["editorial_only"] = [note]
        return result

    runner._authoring_invoke = invoke
    # The final extraction sees the text retained by the preceding editor.
    result = WorkerRunner._authoring_redraft_body_prose(
        runner, replace(claimed(), workflow_version="32"),
        {"proposed_html": html, "human_protected_block_ids": ["PROTECTED"],
         "authoring_compact_claim_citation_v31": {"S": {
             "section_id": "S", "citation_anchors": [{
                 "evidence_id": "theory", "substantive_support_verified": False,
             }],
         }}},
    ).context
    assert note not in result["proposed_html"]
    items = result["verification_notes_routed_v32"]
    assert len(items) == 1
    assert items[0]["reason"] == note
    assert items[0] in result["human_review_items"]
    assert result["prose_redraft_report_v31"]["reverted_reader_candidates"]
    assert result["final_quality_sha256"] == prose.digest(result["proposed_html"])
    prose.validate_handoff(result)
    decision = WorkerRunner._authoring_evaluate_human_revision_requirement(
        WorkerRunner.__new__(WorkerRunner), replace(claimed(), workflow_version="32"), result,
    ).context
    assert decision["human_revision_decision"]["required"] is True
    assert items[0] in decision["human_review_items"]


def test_redraft_continues_with_recorded_fallback_for_ambiguous_or_missing_card(tmp_path):
    runner, calls = mock_runner(tmp_path)
    html = HTML.replace('<section id="S"', '<section id="SEC-plan-s01"')
    for cards in ({"s02": {"section_id": "s02"}}, {
        "s01": {"section_id": "s01"}, "plan-s01": {"section_id": "plan-s01"},
    }):
        result = WorkerRunner._authoring_redraft_body_prose(runner, claimed(), {
            "proposed_html": html,
            "authoring_compact_claim_citation_v31": cards,
            "claims": [{"id": "legacy-claim", "text": "元の資料"}],
        })
        [resolution] = result.context["prose_redraft_report_v31"]["material_resolutions"]
        assert resolution["source"] == "legacy_projection"
        assert resolution["reason"]
    assert calls == ["compose_body_prose_v31"] * 2


def test_redraft_can_recover_card_key_from_citation_lineage():
    assert WorkerRunner._authoring_redraft_section_key_v31(
        "SEC-renamed", {
            "authoring_compact_claim_citation_v31": {"s01": {"section_id": "s01"}},
            "authoring_paragraph_citation_lineage": [{
                "authoring_section_id": "s01", "output_block_id": "P",
            }],
        }, {"P"},
    ) == "s01"






def test_reader_misquoted_span_does_not_restore_whole_section(tmp_path):
    runner, _ = mock_runner(tmp_path)
    original_invoke = runner._authoring_invoke

    def invoke(*args, **kwargs):
        result = original_invoke(*args, **kwargs)
        if (kwargs["stage"] == "review_reader_prose_v31"
                and kwargs["operation_name"].startswith("review_reader_prose:")):
            result["judgments"][0]["unnatural_spans"] = [{
                "excerpt": "本文にない言い回し", "reason": "不自然",
                "intended_referent": "", "natural_alternative": "",
            }]
        return result

    runner._authoring_invoke = invoke
    context = WorkerRunner._authoring_redraft_body_prose(
        runner, claimed(), {"proposed_html": HTML}
    ).context
    assert "作業の手順を読み" in context["proposed_html"]
    assert not any(
        item.get("failed_stage") == "audit"
        for item in context["prose_redraft_report_v31"]["section_reviews"]
    )


def test_structure_failure_keeps_only_invalid_paragraph_without_regeneration(tmp_path):
    runner, calls = mock_runner(tmp_path, "slots")
    result = WorkerRunner._authoring_redraft_body_prose(
        runner, claimed(), {"proposed_html": HTML}
    )
    report = result.context["prose_redraft_report_v31"]
    assert report["status"] == "completed_with_review"
    assert prose.manifest(HTML) == prose.manifest(result.context["proposed_html"])
    assert prose.inventory(result.context["proposed_html"])[0]["visible_text"] == (
        prose.inventory(HTML)[0]["visible_text"]
    )
    assert any("DOM" in reason or "スロット" in reason
               for item in report["unresolved"] for reason in item["reasons"])
    assert list(tmp_path.rglob("*.original.html"))
    assert list(tmp_path.rglob("*.revised.html"))
    assert calls == ["compose_body_prose_v31"]


def test_dom_recovery_reverts_only_the_invalid_paragraph_and_keeps_note_anchors():
    paragraphs = prose.inventory(HTML)
    edits = [
        {"paragraph_id": p["paragraph_id"], "text_slots": list(p["text_slots"])}
        for p in paragraphs
    ]
    edits[0]["text_slots"][0] = "<p id=\"injected\">不正な候補</p>"
    edits[1]["text_slots"] = ["ラマールの", "を比較の観点に置く。"]
    revised, validation, findings, effective = prose.apply_edits_recovering(HTML, edits)
    assert validation["structure_unchanged"] is True
    assert {item["paragraph_id"] for item in findings} == {"P"}
    assert revised != HTML
    assert ET.fromstring(revised).find(".//*[@id='P']").itertext().__next__().startswith("工程")
    assert ET.fromstring(revised).find(".//*[@id='Q']").find("cite").get(
        "data-reference-id"
    ) == "ref"
    assert ET.fromstring(revised).find(".//*[@id='A']").get("href") == "#F"
    assert prose.manifest(HTML) == prose.manifest(revised)
    assert next(item for item in effective if item["paragraph_id"] == "P")["text_slots"] == (
        paragraphs[0]["text_slots"]
    )


def test_prose_redraft_connection_failure_saves_original_candidate_for_review(tmp_path):
    runner, calls = mock_runner(tmp_path)

    def fail_connection(*args, operation_name, **kwargs):
        del args
        if operation_name.startswith("compose_body_prose:"):
            raise ConnectionError("local inference endpoint refused connection")
        return invoke_fixture(kwargs["stage"], kwargs["payload"])

    runner._authoring_invoke = fail_connection
    result = WorkerRunner._authoring_redraft_body_prose(
        runner, claimed(), {"proposed_html": HTML}
    )
    report = result.context["prose_redraft_report_v31"]
    assert report["status"] == "completed_with_review"
    assert result.context["proposed_html"] == HTML
    assert {item["failed_stage"] for item in report["section_reviews"]} == {
        "single_pass_composition"
    }
    assert {item["category"] for item in result.context["human_review_items"]} == {
        "japanese_prose_redraft"
    }
    assert len(calls) == 0


@pytest.mark.parametrize("field", ["reference_rendered_html_v26", "final_quality_sha256"])
def test_stale_upstream_candidate_stops_before_inference(tmp_path, field):
    runner, calls = mock_runner(tmp_path)
    with pytest.raises(NonRetryableFukamiruError, match="stale"):
        WorkerRunner._authoring_redraft_body_prose(
            runner, claimed(), {"proposed_html": HTML, field: "stale"}
        )
    assert not calls


@pytest.mark.parametrize(
    "stage", [
        "diagnose_body_prose_v31", "redraft_body_prose_v31",
        "audit_redrafted_prose_v31", "verify_redraft_claims_v31",
        "review_reader_prose_v31",
    ]
)
def test_checkpoint_cannot_cache_incomplete_paragraph_coverage(stage):
    payload = {"paragraphs": prose.inventory(HTML)}
    result = invoke_fixture(stage, payload)
    field = "judgments" if stage in {
        "audit_redrafted_prose_v31", "verify_redraft_claims_v31",
        "review_reader_prose_v31",
    } else "paragraphs"
    result[field].pop()
    with pytest.raises(NonRetryableFukamiruError, match="IDs"):
        WorkerRunner._authoring_validate_checkpoint_result(stage, payload, result)


def test_meaning_card_projection_discards_only_extra_context_rows():
    payload = {
        "paragraphs": [{"paragraph_id": "P"}],
        "context_paragraphs": [
            {"paragraph_id": "P"}, {"paragraph_id": "Q"},
        ],
    }
    result = {"paragraphs": [
        {"paragraph_id": "P", "topic": "対象"},
        {"paragraph_id": "Q", "topic": "文脈"},
    ]}
    projected = WorkerRunner._authoring_project_meaning_cards(
        "extract_paragraph_meaning_v31", payload, result
    )
    assert [item["paragraph_id"] for item in projected["paragraphs"]] == ["P"]
    WorkerRunner._authoring_validate_checkpoint_result(
        "extract_paragraph_meaning_v31", payload, projected
    )
    unknown = {"paragraphs": [*result["paragraphs"], {"paragraph_id": "X"}]}
    assert WorkerRunner._authoring_project_meaning_cards(
        "extract_paragraph_meaning_v31", payload, unknown
    ) == unknown
    missing = {"paragraphs": [{"paragraph_id": "Q"}]}
    assert WorkerRunner._authoring_project_meaning_cards(
        "extract_paragraph_meaning_v31", payload, missing
    ) == missing


def test_quote_and_heading_checkpoints_reject_changed_identity():
    quote_payload = {
        "quote_candidates": [{"paragraph_id": "P", "literal": "「誰が見るか」"}],
    }
    quote_result = invoke_fixture("classify_quote_provenance_v31", quote_payload)
    quote_result["judgments"][0]["literal"] = "「誰を見るか」"
    with pytest.raises(NonRetryableFukamiruError, match="Quote provenance"):
        WorkerRunner._authoring_validate_checkpoint_result(
            "classify_quote_provenance_v31", quote_payload, quote_result
        )
    heading_payload = {"headings": [{
        "section_id": "S", "heading_id": "H", "text": "元の見出し",
    }]}
    heading_result = invoke_fixture("redraft_section_heading_v31", heading_payload)
    heading_result["headings"][0]["heading_id"] = "other"
    with pytest.raises(NonRetryableFukamiruError, match="Heading checkpoint"):
        WorkerRunner._authoring_validate_checkpoint_result(
            "redraft_section_heading_v31", heading_payload, heading_result
        )




def test_final_surface_findings_are_review_items_without_blocking_ready(tmp_path, monkeypatch):
    runner, _ = mock_runner(tmp_path)
    finding = {"block_id": "P", "issues": [{"code": "surface.test", "message": "検査指摘"}]}
    monkeypatch.setattr(JapaneseSurfaceQualityGate, "find_canonical_html", lambda _: (finding,))
    context = WorkerRunner._authoring_redraft_body_prose(
        runner, claimed(), {"proposed_html": HTML}
    ).context
    assert context["prose_redraft_report_v31"]["japanese_quality_passed"] is False
    assert context["human_review_items"][0]["section_id"] == "S"
    gate = WorkerRunner._manuscript_completion_gate(context)
    assert "prose_redraft.review_required" not in {i["code"] for i in gate["blocking_reasons"]}
    assert "surface.remaining_defects" not in {i["code"] for i in gate["blocking_reasons"]}


def test_final_reader_finding_reaches_human_review_at_the_actual_paragraph(
    tmp_path, monkeypatch,
):
    html = (
        '<article id="D" lang="ja"><section id="S">'
        '<h2 id="H">本文</h2><p id="P">資料は参照候補にとどまる。</p>'
        '<p id="Q">ここでは関係を論じる。</p></section></article>'
    )
    sha = hashlib.sha256(html.encode()).hexdigest()
    runner = WorkerRunner.__new__(WorkerRunner)
    runner.settings = SimpleNamespace(paths=SimpleNamespace(runtime_root=tmp_path))
    runner.databases = SimpleNamespace(state=object())
    runner._register_artifact = lambda _claimed, path, *_args, **_kwargs: str(path)
    monkeypatch.setattr(
        "fukamiru.worker.runner.AuthoringRepository.create_pending_proposal",
        lambda _self, **_kwargs: {"status": "pending", "proposed_sha256": sha},
    )
    context = {
        "proposed_html": html,
        "frozen_candidate_v26": {"html_sha256": sha},
        "source_document_id": "source",
        "source_revision_id": "revision",
        "document_id": "target",
        "plan_id": "plan",
        "plan_revision": 1,
        "completion_gate": {
            "passed": True, "quality_status": "ready", "candidate_sha256": sha,
        },
        "v26_final_judgement": {
            "completion_status": "success", "quality_status": "ready",
            "html_sha256": sha,
        },
        "human_review_items": [{
            "review_item_id": "prose-redraft:S", "section_id": "manuscript",
            "sentence_id": "S", "category": "japanese_prose_redraft",
            "reason": "旧スコープ", "recommended_check": "本文を確認",
        }],
        "draft_sections": [{"section_id": "S", "text": "無関係な旧稿。"}],
    }
    review = runner._authoring_evaluate_human_revision_requirement(claimed(), context).context
    items = review["human_review_items"]
    assert review["human_revision_decision"]["required"] is True
    assert not any(item.get("review_item_id") == "prose-redraft:S" for item in items)
    assert len(items) == 1
    assert items[0]["section_id"] == "S"
    assert items[0]["sentence_id"] == "P"
    assert items[0]["blocks_render"] is False

    final = runner._authoring_finalize(claimed(), review).context
    assert final["authoring_proposal_status"] == "pending"
    assert final["authoring_result_status"] == "completed_with_review"
    report = json.loads(Path(final["review_report_path"]).read_text(encoding="utf-8"))
    assert report["review_items"][0]["sentence_id"] == "P"
    assert final["human_revision_decision"]["required"] is True
    assert final["completion_gate"]["quality_status"] == "needs_human_review"


def test_final_reader_finding_is_recovered_without_prior_review_stage(tmp_path, monkeypatch):
    html = (
        '<article id="D" lang="ja"><section id="S">'
        '<h2 id="H">本文</h2><p id="P">入力メモを参照する。</p>'
        '</section></article>'
    )
    sha = hashlib.sha256(html.encode()).hexdigest()
    runner = WorkerRunner.__new__(WorkerRunner)
    runner.settings = SimpleNamespace(paths=SimpleNamespace(runtime_root=tmp_path))
    runner.databases = SimpleNamespace(state=object())
    runner._register_artifact = lambda _claimed, path, *_args, **_kwargs: str(path)
    monkeypatch.setattr(
        "fukamiru.worker.runner.AuthoringRepository.create_pending_proposal",
        lambda _self, **_kwargs: {"status": "pending", "proposed_sha256": sha},
    )
    context = {
        "proposed_html": html,
        "frozen_candidate_v26": {"html_sha256": sha},
        "source_document_id": "source", "source_revision_id": "revision",
        "document_id": "target", "plan_id": "plan", "plan_revision": 1,
        "completion_gate": {"passed": True, "quality_status": "ready", "candidate_sha256": sha},
        "v26_final_judgement": {
            "completion_status": "success", "quality_status": "ready", "html_sha256": sha,
        },
    }
    final = runner._authoring_finalize(claimed(), context).context
    assert final["authoring_proposal_status"] == "pending"
    assert final["human_revision_decision"]["required"] is True
    assert final["human_review_items"][0]["sentence_id"] == "P"


def test_changed_prose_uses_only_deterministic_post_edit_validation(tmp_path, monkeypatch):
    runner, _ = mock_runner(tmp_path)
    context = WorkerRunner._authoring_redraft_body_prose(
        runner, claimed(), {"proposed_html": HTML, "economy_priority": True}
    ).context
    def forbidden(*args, **kwargs):
        raise AssertionError("A second LLM audit is forbidden after V31 prose editing")

    monkeypatch.setattr("fukamiru.worker.coherence_certification.audit_coherence", forbidden)
    result = WorkerRunner._manuscript_validate_footnoted_coherence_v29(runner, claimed(), context)
    report = result.context["footnoted_coherence_report"]
    assert report["html_sha256"] == prose.digest(context["proposed_html"])
    assert report["semantic_audit_performed"] is False
    assert report["validation_scope"] == "rendered_structure_and_locked_references"


def test_one_pass_skips_later_llm_discourse_and_finding_audits(tmp_path, monkeypatch):
    runner, calls = mock_runner(tmp_path)
    context = WorkerRunner._authoring_redraft_body_prose(
        runner, claimed(), {"proposed_html": HTML}
    ).context
    sha = prose.digest(context["proposed_html"])
    context["frozen_candidate_v26"] = {"html_sha256": sha}

    def forbidden(*args, **kwargs):
        raise AssertionError("V31 one-pass prose must not launch a later model audit")

    runner._language_plan = forbidden
    runner._manuscript_revalidate_findings = forbidden
    runner._language_condition_context_v22 = lambda _c, c, **_kw: c
    runner._manuscript_validate_terms = (
        lambda claim, candidate: WorkerRunner._manuscript_validate_terms(
            runner, claim, candidate
        )
    )
    discourse = WorkerRunner._manuscript_validate_terms_v26(runner, claimed(), context).context
    findings = WorkerRunner._manuscript_audit_final_findings_v26(
        runner, claimed(), discourse
    ).context
    assert calls == ["compose_body_prose_v31"]
    assert discourse["prose_one_pass_discourse_review_skipped"] is True
    assert findings["final_finding_audit"]["audit_performed"] is False
    assert findings["final_finding_audit"]["source_sha256"] == sha


def test_one_pass_submission_failure_goes_to_human_review_without_repair(tmp_path):
    runner, calls = mock_runner(tmp_path)
    context = WorkerRunner._authoring_redraft_body_prose(
        runner, claimed(), {"proposed_html": HTML}
    ).context
    sha = prose.digest(context["proposed_html"])
    context.update({
        "reference_quality_gate": {"passed": False, "target_html_sha256": sha},
        "rendered_structure_v26": {"valid": True, "html_sha256": sha},
        "footnoted_coherence_report": {"passed": True, "html_sha256": sha},
    })
    runner._v31_reviewable_provisional_bibliography_ids = (
        WorkerRunner._v31_reviewable_provisional_bibliography_ids
    )
    result = WorkerRunner._authoring_join_submission_validations(
        runner, claimed(), context
    )
    assert calls == ["compose_body_prose_v31"]
    assert result.outcome == "conditional_success"
    assert result.context["submission_validation_gate"]["failed_validations"] == [
        "reference_quality", "reference_integrity"
    ]
    assert result.context["submission_validation_gate"]["continued_for_human_review"]


def test_deliberate_upstream_repair_supersedes_old_report_and_review_items(tmp_path):
    runner, _ = mock_runner(tmp_path, "missing_context")
    context = WorkerRunner._authoring_redraft_body_prose(
        runner, claimed(), {"proposed_html": HTML}
    ).context
    context["human_review_items"].append({"review_item_id": "unrelated"})
    updated = prose.supersede_for_repair(context)
    assert "prose_redraft_report_v31" not in updated
    assert "prose_redraft_inference_ledger_v31" not in updated
    assert updated["prose_redraft_history_v31"][0]["report_path"]
    assert updated["human_review_items"] == [{"review_item_id": "unrelated"}]
    assert updated["required_user_information"] == []


def test_freeze_preserves_audited_candidate_and_rejects_stale_redraft(tmp_path, monkeypatch):
    runner, _ = mock_runner(tmp_path)
    context = WorkerRunner._authoring_redraft_body_prose(
        runner, claimed(), {"proposed_html": HTML, "pre_polish_html": HTML}
    ).context
    context["v26_coherence_report"] = {"html_sha256": prose.digest(context["proposed_html"])}

    def forbidden(*args):
        raise AssertionError("The audited candidate must not be normalized or restored")

    monkeypatch.setattr(JapaneseSurfaceQualityGate, "guard_canonical_html", forbidden)
    frozen = final_worker_v26.freeze(runner, context)
    assert frozen["proposed_html"] == context["proposed_html"]
    assert frozen["frozen_candidate_v26"]["html_sha256"] == prose.digest(context["proposed_html"])
    with pytest.raises(NonRetryableFukamiruError, match="stale"):
        final_worker_v26.freeze(runner, {**context, "proposed_html": HTML})


@pytest.mark.parametrize("failure", ["missing_report", "changed_report", "changed_candidate"])
def test_final_branch_join_refuses_mixed_prose_generation(tmp_path, failure):
    runner, _ = mock_runner(tmp_path)
    context = WorkerRunner._authoring_redraft_body_prose(
        runner, claimed(), {"proposed_html": HTML}
    ).context
    sha = prose.digest(context["proposed_html"])
    parents = {
        node_id: deepcopy(context)
        for node_id in (
            "validate_reference_quality",
            "validate_footnoted_references",
            "validate_footnoted_structure",
            "validate_footnoted_coherence",
        )
    }
    for parent in parents.values():
        parent["reference_quality_gate"] = {"target_html_sha256": sha, "passed": True}
        parent["rendered_structure_v26"] = {"html_sha256": sha, "valid": True}
        parent["footnoted_coherence_report"] = {"html_sha256": sha, "passed": True}
    damaged = parents["validate_footnoted_structure"]
    if failure == "missing_report":
        damaged.pop("prose_redraft_report_v31")
    elif failure == "changed_report":
        damaged["prose_redraft_report_v31"]["japanese_quality_passed"] = False
    else:
        damaged["proposed_html"] = HTML
    with pytest.raises(NonRetryableFukamiruError, match="differ"):
        WorkerRunner._merge_v31_submission_validation_contexts(parents)


@pytest.mark.parametrize("kind", ["id", "markup", "number", "title", "link"])
def test_invalid_edit_contract_is_nonretryable(kind):
    html = HTML if kind != "link" else HTML.replace('href="#F"', 'href="#MISSING"')
    paragraphs = prose.inventory(HTML)
    edits = [
        {"paragraph_id": p["paragraph_id"], "text_slots": list(p["text_slots"])} for p in paragraphs
    ]
    if kind == "id":
        edits[0]["paragraph_id"] = "unknown"
    if kind == "markup":
        edits[0]["text_slots"][0] = '<p id="new">全体を再生成</p>'
    if kind == "number":
        edits[0]["text_slots"][0] += "2027年。"
    if kind == "title":
        html = html.replace("工程・賃金", "『固定書名』の工程・賃金")
    with pytest.raises(NonRetryableFukamiruError):
        prose.apply_edits(html, edits)


def test_model_contract_and_graph_have_the_required_rendered_html_handoff():
    current, previous = authoring_workflow(), authoring_workflow_v30()
    assert "redraft_body_prose" not in {n.id for n in previous.nodes}
    assert current.next_nodes("render_manuscript_references", "success") == ["redraft_body_prose"]
    assert "verify_prose_completion" not in {node.id for node in current.nodes}
    assert current.next_nodes("start_paragraph_formation", "success") == [
        "resolve_enumeration_context", "plan_semantic_paragraphs"
    ]
    assert next(n for n in current.nodes if n.id == "draft_rewrite").join_generation_source == (
        "start_paragraph_formation"
    )
    assert current.next_nodes("redraft_body_prose", "success") == [
        "finalize_section_headings"
    ]
    assert set(current.next_nodes("finalize_section_headings", "success")) == {
        "validate_reference_quality",
        "validate_footnoted_references",
        "validate_footnoted_structure",
        "validate_footnoted_coherence",
    }
    assert next(
        n for n in current.nodes if n.id == "join_submission_validations"
    ).join_generation_source == ("finalize_section_headings")
    for stage in (
        "diagnose_body_prose_v31", "redraft_body_prose_v31",
        "audit_redrafted_prose_v31",
    ):
        request = describe_authoring_request(stage, {"output_language": "ja"})
        assert "列挙された名詞を順番に定義しない" in request.guidance
        assert "段落の分析より分析計画の" in request.guidance
        assert "sup/aの脚注" in request.guidance
        with pytest.raises(FukamiruError):
            CodexAuthoringAdapter.validate_result(stage, {})
    claim_request = describe_authoring_request(
        "verify_redraft_claims_v31", {"output_language": "ja"}
    )
    assert "命題変化だけを独立に検査" in claim_request.guidance
    with pytest.raises(FukamiruError):
        CodexAuthoringAdapter.validate_result("verify_redraft_claims_v31", {})


def test_new_audit_schemas_are_strict_recursively():
    def visit(value):
        if isinstance(value, dict):
            if value.get("type") == "object":
                assert value.get("additionalProperties") is False
                assert set(value.get("required") or []) == set(value.get("properties") or {})
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    for stage in (
        "audit_predicate_sentences_v31", "review_reader_prose_v31",
        "polish_final_japanese_v31",
    ):
        visit(STAGE_SCHEMAS[stage])


@pytest.mark.parametrize("version", ["31", "32"])
def test_persisted_fanout_fanin_and_submission_keep_the_exact_redrafted_html(monkeypatch, version):
    from test_worker import WorkerTests

    case = WorkerTests()
    case.setUp()
    try:
        worker = case.worker
        original_graph = authoring_workflow()
        original_specs = {node.id: node for node in original_graph.nodes}
        branches = (
            "validate_reference_quality",
            "validate_footnoted_references",
            "validate_footnoted_structure",
            "validate_footnoted_coherence",
        )
        ids = [
                "render_manuscript_references",
                "redraft_body_prose",
                "finalize_section_headings",
            *branches,
            "join_submission_validations",
            "freeze_final_candidate",
            "finalize_authoring",
            "submission_package",
        ]
        nodes = [
            original_specs[n].model_copy(update={"resource_lane": "ingestion_cpu"}) for n in ids
        ]
        edges = [e for e in original_graph.edges if e.source in ids and e.target in ids]
        edges.append(EdgeSpec(source="freeze_final_candidate", target="finalize_authoring"))
        workflow = WorkflowDefinition(
            name="manuscript_generator",
            version=version,
            title="Smoke",
            entry_node=ids[0],
            nodes=nodes,
            edges=edges,
        )
        worker.workflow_versions[(workflow.name, workflow.version)] = workflow
        worker.workflows[workflow.name] = workflow
        # This test intentionally runs only the submission subgraph. The V32
        # production resolver reconstructs the full graph from each Run's pinned
        # profile, so inject this exact cutout rather than accidentally scheduling
        # production nodes outside the test.
        original_workflow_for = worker._workflow_for

        def isolated_workflow(name, requested_version, *, run_id=None):
            if (name, requested_version) == (workflow.name, workflow.version):
                return workflow
            return original_workflow_for(name, requested_version, run_id=run_id)

        monkeypatch.setattr(worker, "_workflow_for", isolated_workflow)
        specs = {node.id: node for node in workflow.nodes}
        seen = []

        def invoke(_claimed, *, stage, context, payload, operation_name):
            return invoke_fixture(stage, payload)

        monkeypatch.setattr(worker, "_authoring_invoke", invoke)
        worker.handlers[specs[ids[0]].handler] = lambda _, c: HandlerResult(c)
        worker.handlers[specs["finalize_section_headings"].handler] = (
            lambda _, c: HandlerResult(c)
        )

        def forbid_mutating_surface_gate(*args, **kwargs):
            raise AssertionError("Freeze must not rewrite the audited DOM redraft")

        monkeypatch.setattr(
            JapaneseSurfaceQualityGate, "guard_canonical_html", forbid_mutating_surface_gate
        )
        for node_id in branches:

            def validate(_claimed, c, node_id=node_id):
                prose.validate_handoff(c)
                assert c["reference_rendered_html_v26"] == c["proposed_html"]
                seen.append(c["proposed_html"])
                sha = prose.digest(c["proposed_html"])
                key = (
                    "rendered_structure_v26"
                    if node_id == "validate_footnoted_structure"
                    else "footnoted_coherence_report"
                    if node_id == "validate_footnoted_coherence"
                    else "reference_quality_gate"
                )
                return HandlerResult(
                    {
                        **c,
                        key: {
                            "passed": True,
                            "valid": True,
                            "target_html_sha256": sha,
                            "html_sha256": sha,
                        },
                    }
                )

            worker.handlers[specs[node_id].handler] = validate

        def finalize(claim, context):
            prose.validate_handoff(context)
            target = (
                worker.settings.paths.runtime_root / "outputs" / claim.run_id / "submission.html"
            )
            target.write_text(context["proposed_html"], encoding="utf-8")
            return HandlerResult(
                {
                    **context,
                    "v26_coherence_report": {
                        "html_sha256": prose.digest(context["proposed_html"]),
                        "passed": True,
                    },
                    "final_path": str(target),
                    "authoring_proposal_sha256": prose.digest(context["proposed_html"]),
                }
            )

        worker.handlers[specs["finalize_authoring"].handler] = finalize
        with case.databases.state.connect() as connection:
            run_id = (
                RunRepository(connection)
                .create(
                    workflow_name=workflow.name,
                    workflow_version=version,
                    entry_node=workflow.entry_node,
                    project_id=None,
                    inputs={"proposed_html": HTML, "human_protected_block_ids": ["PROTECTED"]},
                )
                .run_id
            )
        for _ in range(15):
            if not worker.run_once(lane="ingestion_cpu"):
                break
        with case.databases.state.connect() as connection:
            snapshot = RunRepository(connection).snapshot(run_id)
            row = connection.execute(
                "SELECT output_json FROM node_runs WHERE run_id=? AND node_id='submission_package'",
                (run_id,),
            ).fetchone()
        assert snapshot["run"]["status"] == "completed", snapshot["run"].get("error_json")
        assert len(seen) == 4 and all(html == seen[0] and html != HTML for html in seen)
        output = json.loads(row["output_json"])
        manifest = json.loads(
            Path(output["submission_package"]["manifest_path"]).read_text(encoding="utf-8")
        )
        assert manifest["manuscript_sha256"] == prose.digest(seen[0])
        assert manifest["prose_redraft_report_v31"]["target_html_sha256"] == prose.digest(seen[0])
        assert "prose_redraft_inference_ledger_v31" in manifest
        assert Path(output["final_path"]).read_text(encoding="utf-8") == seen[0]
        stale = deepcopy(output)
        stale["proposed_html"] = HTML
        with pytest.raises(NonRetryableFukamiruError, match="stale"):
            worker._authoring_submission_package(claimed(), stale)
        # A stale file cannot be packaged even if another field supplies its hash.
        Path(output["final_path"]).write_text(HTML, encoding="utf-8")
        stale_file = {**output, "authoring_proposal_sha256": prose.digest(HTML)}
        with pytest.raises(NonRetryableFukamiruError, match="stale"):
            worker._authoring_submission_package(claimed(), stale_file)
    finally:
        case.tearDown()
