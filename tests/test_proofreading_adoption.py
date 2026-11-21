from copy import deepcopy
from types import SimpleNamespace

import pytest

from fukamiru.authoring.codex import CodexAuthoringAdapter, _validate_codex_output_schema
from fukamiru.errors import FukamiruError, NonRetryableFukamiruError
from fukamiru.revisions import proofreading_adoption as rules
from fukamiru.revisions import proofreading_plain as plain
from fukamiru.revisions import proofreading_repair as repair
from fukamiru.worker import proofreading_adoption as nodes
from fukamiru.worker.runner import WorkerRunner
from fukamiru.workflow.display import workflow_node_metadata
from fukamiru.workflow.templates import authoring_workflow

HTML = (
    '<article id="a" lang="ja"><section id="s"><h2 id="h">節</h2>'
    '<p id="p">この視角から<cite id="c" data-reference-id="r">『書名』</cite>'
    'を参照する。</p><p id="human">人間の文章。</p></section></article>'
)


def test_inline_prose_is_editable_and_fixed_assets_and_human_block_are_not():
    frozen = rules.freeze(HTML, {"human"})
    assert len(frozen["paragraphs"]) == 1
    p = frozen["paragraphs"][0]
    assert p["text_slots"] == ["この視角から", "を参照する。"]
    candidate = rules.install(
        HTML, frozen, [{"paragraph_id": "p", "text_slots": ["この観点から", "を読む。"]}]
    )
    assert 'この観点から<cite id="c" data-reference-id="r">『書名』</cite>を読む。' in candidate
    assert '<p id="human">人間の文章。</p>' in candidate
    assert rules.validate_structure(HTML, candidate, {"human"})["DOM_and_IDs_preserved"]
    with pytest.raises(NonRetryableFukamiruError, match="DOM_or_asset"):
        rules.validate_structure(HTML, candidate.replace("『書名』", "『別の本』"), {"human"})


def test_whole_candidate_and_learned_frequency_are_allowed_without_necessary_flag():
    frozen = rules.freeze(HTML, {"human"})
    for kw in ({}, {"review": True}, {"formatting": True}):
        schema = rules.response_schema(frozen["paragraphs"], **kw)
        _validate_codex_output_schema(schema)
        assert "necessary" not in str(schema)
    for schema in (
        repair.repair_schema(frozen["paragraphs"]),
        repair.audit_schema(frozen["paragraphs"]),
    ):
        _validate_codex_output_schema(schema)
        assert '"checks"' not in str(schema)
    assert "実測値や統計的裏付けを" in rules.EXPRESSION_POLICY
    assert "硬い語の使用を認めます" in rules.EXPRESSION_POLICY
    assert "外来語的な印象" in rules.EDITOR_PROMPT
    contract = CodexAuthoringAdapter.execution_contract("format_adopted_japanese_v32")
    assert contract["model"] == "gpt-6-luna"
    _validate_codex_output_schema(contract["schema"])


def test_formatter_can_fix_brackets_but_cannot_rewrite_model_prose():
    p = [{"paragraph_id": "p", "text_slots": ["『ネット以前のネット』とみなせる。"]}]
    rows = [{"paragraph_id": "p", "text_slots": ["「ネット以前のネット」とみなせる。"]}]
    assert rules.format_rows(p, rows) == rows
    rows[0]["text_slots"] = ["「ネット以前のネット」と比較できる。"]
    with pytest.raises(NonRetryableFukamiruError, match="formatter_changed_prose"):
        rules.format_rows(p, rows)


def test_actual_fixed_title_boundaries_must_balance_even_when_model_checks_pass():
    frozen = rules.freeze(HTML, {"human"})
    broken = rules.install(
        HTML, frozen, [{"paragraph_id": "p", "text_slots": ["著者（", "は論じる。"]}]
    )
    # Canonical XML and the frozen DOM remain valid; the reconstructed Japanese is broken.
    assert rules.validate_structure(HTML, broken, {"human"})["DOM_and_IDs_preserved"]
    with pytest.raises(NonRetryableFukamiruError, match="unbalanced_punctuation"):
        rules.validate_paired_punctuation(broken, {"p"})
    fixed = rules.install(
        HTML, frozen, [{"paragraph_id": "p", "text_slots": ["著者（", "）は論じる。"]}]
    )
    rules.validate_paired_punctuation(fixed, {"p"})
    assert rules.format_rows(
        [{"paragraph_id": "p", "text_slots": ["（語句）"]}],
        [{"paragraph_id": "p", "text_slots": ["語句"]}],
    )[0]["text_slots"] == ["語句"]


def test_schema_coverage_is_checked_not_filled_with_original_paragraphs():
    p = rules.freeze(HTML, {"human"})["paragraphs"]
    with pytest.raises(NonRetryableFukamiruError, match="coverage"):
        rules.exact_rows(p, {"paragraphs": []})


def test_node_chain_keeps_full_candidate_and_reconciles_without_legacy_reversion(monkeypatch):
    context = {
        "proposed_html": HTML,
        "human_protected_block_ids": ["human"],
        "authoring_paragraph_citation_lineage": [{"output_block_id": "p", "evidence_ids": ["e"]}],
    }

    def model(worker, claimed, stage, prompt, payload, schema, context):
        human = [
            {
                "paragraph_id": "p",
                "reason": "確認不足は本文に足さない。",
                "recommended_check": "元資料の確認を人間へ回す。",
            }
        ]
        if stage == "llmjp_adopt_anchored_repairs":
            return {"repairs": [], "human_review": human}
        if stage == "llmjp_adopt_plain_readonly_audit":
            assert "findings_to_repair" not in payload
            assert "repair_units" not in payload
            assert payload["applied_repairs"] == []
            assert all(p["text"] for p in payload["candidate_paragraphs"])
            rendered = payload["candidate_paragraphs"][0]
            assert rendered["text"] == "この観点から『書名』を読む。"
            return {
                "paragraphs": {"p": {"verdict": "問題なし", "diagnosis": "日本語として問題なし"}}
            }
        p = payload["targets"][0]
        row = {
            "paragraph_id": p["paragraph_id"],
            "text_slots": ["この観点から", "を読む。"],
            "reason": "語の結び付きと学習に基づく語感を改善",
        }
        return {
            "paragraphs": [row],
            "human_review": human,
        }

    monkeypatch.setattr(nodes.transport, "_call", model)

    def formatter(claimed, **kwargs):
        if kwargs["stage"] == "extract_paragraph_meaning_v31":
            return {"paragraphs": [
                {"paragraph_id": p["paragraph_id"], "editorial_only": []}
                for p in kwargs["payload"]["paragraphs"]
            ]}
        p = kwargs["payload"]["paragraphs"][0]
        return {
            "paragraphs": [
                {
                    "paragraph_id": "p",
                    "text_slots": p["text_slots"],
                    "reason": "既存規定を保持",
                    "applied_rule_ids": [],
                }
            ],
            "human_review": [],
        }

    worker = SimpleNamespace(_authoring_invoke=formatter)
    monkeypatch.setattr(
        WorkerRunner,
        "_authoring_v29_plan_bridge",
        staticmethod(
            lambda *a, **kw: {
                "structure_plan_validation": {"valid": True},
                "authoring_paragraph_citation_lineage": [],
            }
        ),
    )
    for op in (
        nodes.prepare,
        nodes.propose,
        nodes.review,
        nodes.apply,
        nodes.reconcile,
        nodes.finish,
    ):
        context = op(worker, SimpleNamespace(), context)
    assert "この観点から" in context["proposed_html"]
    assert "この視角から" not in context["proposed_html"]
    assert context["authoring_paragraph_citation_lineage"] == [
        {"output_block_id": "p", "evidence_ids": ["e"]}
    ]
    assert context["proofreading_adoption"]["application"]["reverted_blocks"] == []
    assert any(
        item["sentence_id"] == "p" and item["recommended_check"] == "元資料の確認を人間へ回す。"
        for item in context["human_review_items"]
    )


def test_repairs_apply_only_exact_nonoverlapping_candidate_spans():
    rows = [{"paragraph_id": "p", "text_slots": ["採用した自然な語句。著者（", "を論じる。"]}]
    edits = [
        {
            "paragraph_id": "p",
            "slot_index": 1,
            "before": "を論じる。",
            "after": "）は議論を示す。",
            "reason": "固定書名の後の閉じ括弧と主語を復元",
        }
    ]
    result = repair.apply_repairs(rows, edits)
    assert result[0]["text_slots"][0] == rows[0]["text_slots"][0]
    assert rows[0]["text_slots"][1] == "を論じる。"
    with pytest.raises(NonRetryableFukamiruError, match="overlapping"):
        repair.apply_repairs(rows, edits + edits)
    with pytest.raises(NonRetryableFukamiruError, match="exact_anchor"):
        repair.apply_repairs(rows, [{**edits[0], "before": "存在しない語句"}])


def test_readonly_audit_cannot_rewrite_and_must_point_to_actual_candidate():
    from fukamiru.authoring.codex import _validate_schema

    p = [{"paragraph_id": "p", "visible_text": "現在の本文。"}]
    result = {"paragraphs": [{"paragraph_id": "p", "findings": []}], "human_review": []}
    _validate_schema(result, repair.audit_schema(p))
    result["paragraphs"][0]["text_slots"] = ["勝手な改稿"]
    with pytest.raises(FukamiruError, match="unexpected text_slots"):
        _validate_schema(result, repair.audit_schema(p))
    result["paragraphs"][0].pop("text_slots")
    result["paragraphs"][0]["findings"] = [{"unit_ids": ["UNKNOWN"], "reason": "誤り"}]
    with pytest.raises(NonRetryableFukamiruError, match="audit_unknown_unit"):
        repair.audit_findings(result, p, [])


def test_model_selects_locally_hashed_unit_without_copying_a_hallucinated_space():
    rows = [{"paragraph_id": "p", "text_slots": ["ランシエール", "は述べる。"]}]
    units = repair.repair_units(rows)
    target = next(u for u in units if u["text"] == "ランシエール")
    change = {"unit_ids": [target["id"]], "after": "ランシエールの", "reason": "著者と書名をつなぐ"}
    result, log = repair.apply_unit_repairs(rows, units, [change])
    assert result[0]["text_slots"] == ["ランシエールの", "は述べる。"]
    assert log[0]["before"] == "ランシエール"
    assert rows[0]["text_slots"][0] == "ランシエール"
    mutated = deepcopy(rows)
    mutated[0]["text_slots"][0] += " "
    with pytest.raises(NonRetryableFukamiruError, match="candidate_changed"):
        repair.apply_unit_repairs(mutated, units, [change])
    other = next(u for u in units if u["slot_index"] == 1 and u["kind"] == "replace")
    with pytest.raises(NonRetryableFukamiruError, match="crosses_fixed_asset"):
        repair.apply_unit_repairs(
            rows, units, [{**change, "unit_ids": [target["id"], other["id"]]}]
        )


def test_readonly_prose_receives_only_actual_text_and_committed_repairs(monkeypatch):
    context = nodes.prepare(
        None, None, {"proposed_html": HTML, "human_protected_block_ids": ["human"]}
    )
    paragraph = context["japanese_proofreading_context"]["plan"]["paragraphs"][0]
    row = {
        "paragraph_id": "p",
        "text_slots": ["この観点から", "を読む。"],
        "reason": "日本語の改善",
    }
    context["japanese_proofreading_context"]["proposals"] = [
        {"section_id": paragraph["section_id"], "paragraphs": [row], "human_review": []}
    ]
    calls = []

    def model(worker, claimed, stage, prompt, payload, schema, context):
        calls.append(stage)
        assert "repair_units" not in payload and "passages" not in payload
        if stage == "llmjp_adopt_plain_readonly_audit":
            assert "validation_error" not in payload and "findings" not in payload
            if calls.count(stage) == 1:
                assert payload["applied_repairs"] == []
                return {
                    "paragraphs": {
                        "p": {"verdict": "修正が必要", "diagnosis": "語句のつながりを整える"}
                    }
                }
            assert len(payload["applied_repairs"]) == 1
            assert payload["applied_repairs"][0]["before"] == "この観点から"
            return {"paragraphs": {"p": {"verdict": "問題なし", "diagnosis": "修正後は自然"}}}
        before = "この観点から " if calls.count(stage) == 1 else "この観点から"
        return {
            "repairs": [
                {
                    "paragraph_id": "p",
                    "before": before,
                    "after": "この観点に立って",
                    "reason": "つながりを整える",
                }
            ],
            "human_review": [],
        }

    monkeypatch.setattr(nodes.transport, "_call", model)
    out = nodes.review(SimpleNamespace(), SimpleNamespace(), context)
    assert calls == [
        "llmjp_adopt_plain_readonly_audit",
        "llmjp_adopt_plain_repairs",
        "llmjp_adopt_plain_repairs",
        "llmjp_adopt_plain_readonly_audit",
    ]
    assert out["japanese_proofreading_context"]["reviews"][0]["paragraphs"][0]["text_slots"] == [
        "この観点に立って",
        "を読む。",
    ]


def test_plain_repairs_preserve_fixed_titles_even_when_the_quote_includes_them():
    context = nodes.prepare(
        None, None, {"proposed_html": HTML, "human_protected_block_ids": ["human"]}
    )
    rows = [{"paragraph_id": "p", "text_slots": ["この観点から", "を参照する。"], "reason": "元案"}]
    patch = {
        "paragraph_id": "p",
        "before": "『書名』を参照する。",
        "after": "『書名』を読む。",
        "reason": "述語を整える",
    }
    changed, edits = plain.apply_repairs(context, rows, [patch])
    assert changed[0]["text_slots"] == ["この観点から", "を読む。"]
    assert edits == [patch]
    assert rows[0]["text_slots"][1] == "を参照する。"
    with pytest.raises(NonRetryableFukamiruError, match="changed_fixed_asset"):
        plain.apply_repairs(context, rows, [{**patch, "after": "『別の書名』を読む。"}])
    for schema in (
        plain.audit_schema([{"paragraph_id": "p"}]),
        plain.repair_schema([{"paragraph_id": "p"}]),
    ):
        _validate_codex_output_schema(schema)


def test_actual_evidence_uses_do_not_turn_conclusion_inference_into_a_citation():
    context = {
        "proposed_html": HTML,
        "plan_id": "plan",
        "draft_sections": [
            {"section_id": "intro", "paragraphs": [{"sentence_ids": ["s1"]}]},
            {"section_id": "conclusion", "paragraphs": [{"sentence_ids": ["s2"]}]},
        ],
        "evidence_uses": [
            {
                "evidence_use_id": "u1",
                "sentence_id": "s1",
                "section_id": "intro",
                "claim_id": "claim",
                "citation_required": True,
                "citation_source_ids": ["book"],
            },
            {
                "evidence_use_id": "u2",
                "sentence_id": "s2",
                "section_id": "conclusion",
                "claim_id": "claim",
                "citation_required": False,
                "citation_source_ids": [],
                "author_inference": "筆者の結論",
            },
        ],
        "authoring_paragraph_citation_lineage": [
            {"output_block_id": "human", "evidence_ids": ["book"]}
        ],
    }
    context["proposed_html"] = HTML.replace('id="p"', 'id="P-plan-intro-001"').replace(
        'id="human"', 'id="P-plan-conclusion-001"'
    )
    frozen_uses = deepcopy(context["evidence_uses"])
    lineage, report = repair.actual_citation_lineage(context)
    assert [r["output_block_id"] for r in lineage] == ["P-plan-intro-001"]
    assert report["required_use_ids"] == ["u1"]
    assert context["evidence_uses"] == frozen_uses
    for field, invalid in (("citation_source_ids", []), ("sentence_id", "missing")):
        broken = deepcopy(context)
        broken["evidence_uses"][0][field] = invalid
        with pytest.raises(NonRetryableFukamiruError, match="required_evidence_use_unresolved"):
            repair.actual_citation_lineage(broken)


def test_audited_body_cannot_be_changed_before_formatting():
    context = nodes.prepare(
        None, None, {"proposed_html": HTML, "human_protected_block_ids": ["human"]}
    )
    row = {"paragraph_id": "p", "text_slots": ["この観点から", "を参照する。"], "reason": "改稿"}
    group = {"paragraphs": [row], "audited_candidate_sha256": rules.digest([row])}
    context["japanese_proofreading_context"]["reviews"] = [group]
    row["text_slots"][1] = "を参照するのはない。"
    with pytest.raises(NonRetryableFukamiruError, match="candidate_changed_after_readonly_audit"):
        nodes.apply(SimpleNamespace(), SimpleNamespace(), context)


def test_fixed_asset_projection_preserves_native_words_and_repairs_only_partition():
    html = (
        '<article id="a" lang="ja"><section id="s"><h2 id="h">節</h2>'
        '<p id="p">この視角で論じる。甲<cite id="c1" data-reference-id="a">『甲の書』</cite>'
        'は第一の点を、乙<cite id="c2" data-reference-id="b">『乙の書』</cite>'
        "は第二の点を示す。</p></section></article>"
    )
    context = nodes.prepare(None, None, {"proposed_html": html})
    row = {
        "paragraph_id": "p",
        "text_slots": [
            "この観点で論じる。",
            "甲",
            "は第一の点を示し、乙『乙の書』は第二の点を示す。",
        ],
        "reason": "語感の改善",
    }
    projected, reports = repair.project_fixed_assets(context, [row])
    assert projected[0]["text_slots"] == [
        "この観点で論じる。甲",
        "は第一の点を示し、乙",
        "は第二の点を示す。",
    ]
    assert "".join(projected[0]["text_slots"]) == "".join(row["text_slots"]).replace(
        "『乙の書』", ""
    )
    assert reports[0]["native_word_order_preserved"] and not reports[0]["old_prose_substituted"]
    rendered = rules.install(html, context["japanese_proofreading_context"]["plan"], projected)
    assert '甲<cite id="c1"' in rendered and '乙<cite id="c2"' in rendered
    assert "この観点" in rendered and "この視角" not in rendered
    repair.validate_candidate(context, projected)
    correct = {
        **row,
        "text_slots": ["この観点で論じる。甲の", "は第一の点を示し、乙の", "は第二の点を示す。"],
    }
    unchanged, _ = repair.project_fixed_assets(context, [correct])
    assert unchanged == [correct]


def test_exact_delta_can_ignore_inaccurate_unchanged_context_without_copying_it():
    context = nodes.prepare(None, None, {"proposed_html": HTML})
    rows = [{"paragraph_id": "p", "text_slots": ["男性同間の関係を論じる。", "を読む。"]}]
    patch = {
        "paragraph_id": "p",
        "before": "男性同間の関係の",
        "after": "男性同士の関係の",
        "reason": "誤字を修正",
    }
    changed, records = plain.apply_repairs(context, rows, [patch])
    assert changed[0]["text_slots"][0] == "男性同士の関係を論じる。"
    assert records[0]["before"] == "間" and records[0]["after"] == "士"
    assert records[0]["requested_before"] == patch["before"]
    assert records[0]["matched_source_anchor"] == "男性同間の関係"
    for before, after in (("男性 ", "男性の"), ("男性同間 ", "男性同士 ")):
        repeated = [{**rows[0], "text_slots": ["男性同間の関係と男性同間の関係。", "を読む。"]}]
        with pytest.raises(NonRetryableFukamiruError, match="exact_delta_anchor"):
            plain.apply_repairs(context, repeated, [{**patch, "before": before, "after": after}])


def test_projection_removes_only_new_full_predicate_wrapper_after_fixed_cite():
    html = HTML.replace("を参照する。", "は論じる。")
    context = nodes.prepare(None, None, {"proposed_html": html})
    row = {"paragraph_id": "p", "text_slots": ["この観点から", "「は詳しく論じる。」"]}
    projected, reports = repair.project_fixed_assets(context, [row])
    assert projected[0]["text_slots"] == ["この観点から", "は詳しく論じる。"]
    assert reports[0]["removed_new_predicate_quote_wrapper"]
    assert reports[0]["projected_editable_text_sha256"] == rules.digest(
        "この観点からは詳しく論じる。"
    )
    assert row["text_slots"][1] == "「は詳しく論じる。」"
    quoted = nodes.prepare(
        None, None, {"proposed_html": html.replace("は論じる。", "「は論じる。」")}
    )
    assert repair.project_fixed_assets(quoted, [row])[0] == [row]


def test_final_renderer_can_add_notes_but_cannot_replace_adopted_words():
    rendered = HTML.replace(
        "を参照する。", 'を参照する<sup id="n"><a href="#note" role="doc-noteref">1</a></sup>。'
    ).replace(
        "</article>",
        '<section id="notes" data-citation-section="evidence-footnotes-v1"><h2 id="nh">注</h2>'
        '<p id="note">注。</p></section></article>',
    )
    assert rules.validate_finalized(HTML, rendered)["adopted_prose_preserved"]
    with pytest.raises(NonRetryableFukamiruError, match="final_prose_changed"):
        rules.validate_finalized(HTML, rendered.replace("この視角から", "別の説明で"))


def test_new_profile_preserves_old_graph_and_displays_actual_roles():
    old = authoring_workflow(proofreading_profile="llmjp33b_high")
    new = authoring_workflow(proofreading_profile=rules.PROFILE)
    assert [n.id for n in new.nodes] == [n.id for n in old.nodes]
    gate = next(n for n in new.nodes if n.id == "japanese_surface_quality_gate")
    assert gate.handler == "authoring.llmjp_adopt_apply"
    assert gate.resource_lane == "codex_remote"
    assert "Luna" in workflow_node_metadata(new, gate.id)["function_name_ja"]
    assert next(n for n in old.nodes if n.id == gate.id).handler == "authoring.llmjp33b_apply"
    assert deepcopy(new).version == "32"
