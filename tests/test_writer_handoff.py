"""Regression checks for the compact V31 writer handoff and final headings."""

from __future__ import annotations

from types import SimpleNamespace

from fukamiru.authoring.codex import STAGE_GUIDANCE
from fukamiru.revisions import prose_redraft as prose
from fukamiru.worker import section_heading
from fukamiru.worker.runner import WorkerRunner

HTML = (
    '<article id="DOC-x" lang="ja"><section id="SEC-x-s1">'
    '<h2 id="H-x-s1" data-authoring-role="body">再束縛</h2>'
    '<p id="P-x-s1-001">棚での分類は作品の見つけ方を変える。</p>'
    '</section></article>'
)


def test_compact_directives_carry_intro_argument_and_selected_list_relation():
    context = {
        "research_problem": {"scope": "1980年代以降の日本漫画を扱う。"},
        "introduction_contract": {
            "research_question": "媒体の配置は作品の読まれ方をどう変えるか。",
            "provisional_answer": "配置は発見の経路を変える。検証は後続節で行う。",
            "contribution_target": "媒体と作品の関係を説明する。",
            "roadmap_section_ids": ["s1"],
        },
        "section_contracts": [
            {"section_id": "intro", "article_role": "introduction"},
            {"section_id": "s1", "article_role": "body", "title": "棚と検索"},
        ],
        "semantic_paragraph_plan": [{
            "section_id": "s1", "paragraphs": [{
                "central_claim": "棚の分類は作品の発見経路を変える。",
                "development_instruction": (
                    "媒体を列挙せず、棚の分類が読者の発見をどう変えるか説明する。"
                ),
            }],
        }],
        "central_term_use_plan_v31": [{
            "term": "媒体レギュレーション",
            "applications": [{
                "section_id": "s1",
                "analytical_relation": "棚の分類は作品の発見を方向づける。",
            }],
        }],
    }
    intro = WorkerRunner._authoring_writer_directives_v31(
        context, "intro", "introduction"
    )
    body = WorkerRunner._authoring_writer_directives_v31(context, "s1", "body")
    assert intro["introduction"]["provisional_answer"] == "配置は発見の経路を変える"
    assert intro["introduction"]["first_body_topic"] == "棚と検索"
    assert intro["introduction"]["body_section_count"] == 1
    assert "roadmap" not in intro["introduction"]
    assert body["enumeration_resolution"][0]["central_relation"] == (
        "棚の分類は作品の発見経路を変える。"
    )
    assert "writing_decision" not in body["enumeration_resolution"][0]
    assert body["central_concept_application"][0]["term"] == "媒体レギュレーション"


def test_writer_directives_do_not_copy_reference_verification_status():
    context = {"semantic_paragraph_plan": [{
        "section_id": "s1", "paragraphs": [{
            "central_claim": "ラマールの視角を漫画誌の配置に用いる。",
            "development_instruction": (
                "作品の配置を比較する。ラマールの著作は書誌同定済みの参照候補だが、"
                "具体的な歴史仮説への適用は未検証とする。"
            ),
        }],
    }]}
    directives = WorkerRunner._authoring_writer_directives_v31(
        context, "s1", "body"
    )
    assert directives == {"enumeration_resolution": [{
        "central_relation": "ラマールの視角を漫画誌の配置に用いる。"
    }]}


def test_central_concept_plan_targets_body_analysis_without_all_terms():
    context = {
        "research_question": {"text": "媒体レギュレーションはどう働くか。"},
        "section_contracts": [
            {"section_id": "intro", "article_role": "introduction",
             "required_terms": ["媒体レギュレーション", "制作条件"]},
            {"section_id": "s1", "article_role": "body", "title": "棚と検索",
             "section_output": "棚の配置が作品の発見を変える。"},
            {"section_id": "s2", "article_role": "body", "title": "作者性",
             "section_output": "制作の共同性を示す。"},
            {"section_id": "end", "article_role": "conclusion"},
        ],
    }
    result = WorkerRunner._authoring_plan_term_introductions(
        None, SimpleNamespace(workflow_version="31"), context
    ).context
    plan = result["central_term_use_plan_v31"]
    assert [item["term"] for item in plan] == ["媒体レギュレーション"]
    assert [item["section_id"] for item in plan[0]["applications"]] == ["s1"]
    assert plan[0]["conclusion_section_id"] == "end"


def _heading_context():
    return {
        "proposed_html": HTML,
        "reference_rendered_html_v26": HTML,
        "section_contracts": [{
            "section_id": "s1", "article_role": "body",
            "section_output": "棚での分類が作品の発見を変える。",
        }],
        "prose_redraft_report_v31": {
            "target_html_sha256": prose.digest(HTML),
            "inference_ledger": [],
            "structure_unchanged": True,
            "locked_content_unchanged": True,
            "anchors_valid": True,
        },
        "prose_redraft_inference_ledger_v31": [],
    }


def _heading_worker(tmp_path, *, mismatched_id=False):
    calls = []

    def invoke(_claimed, *, stage, context, payload, operation_name):
        calls.append((stage, operation_name, payload))
        row = payload["headings"][0]
        return {"headings": [{
            "section_id": row["section_id"],
            "heading_id": "wrong" if mismatched_id else row["heading_id"],
            "text": "棚の分類と作品の見つけ方", "reason": "節の対象を明示する。",
        }]}

    return SimpleNamespace(
        settings=SimpleNamespace(paths=SimpleNamespace(runtime_root=tmp_path)),
        _authoring_invoke=invoke,
        _authoring_redraft_section_key_v31=(
            WorkerRunner._authoring_redraft_section_key_v31
        ),
        _register_artifact=lambda *_args, **_kwargs: "artifact",
    ), calls


def test_heading_edit_changes_only_heading_and_keeps_prose_handoff(tmp_path):
    worker, calls = _heading_worker(tmp_path)
    claimed = SimpleNamespace(run_id="run", node_run_id="node")
    result = section_heading.run(worker, claimed, _heading_context())
    assert len(calls) == 1
    assert "棚の分類と作品の見つけ方" in result["proposed_html"]
    assert '<p id="P-x-s1-001">棚での分類は作品の見つけ方を変える。</p>' in (
        result["proposed_html"]
    )
    assert result["heading_revision_report_v31"]["changed_heading_ids"] == ["H-x-s1"]
    prose.validate_handoff(result)


def test_heading_uses_concluding_relation_without_another_model_call(tmp_path):
    html = (
        '<article id="DOC-x" lang="ja"><section id="SEC-x-s1">'
        '<h2 id="H-x-s1" data-authoring-role="body">欲望表現は移ったのか</h2>'
        '<p id="P-x-s1-001">三作品の配役を比べる。</p>'
        '<p id="P-x-s1-002">誰が欲望し、誰の身体が見られるかが異なる。</p>'
        '</section></article>'
    )
    context = _heading_context()
    context["proposed_html"] = html
    context["reference_rendered_html_v26"] = html
    worker, calls = _heading_worker(tmp_path)
    section_heading.run(
        worker, SimpleNamespace(run_id="run", node_run_id="node"), context
    )
    assert len(calls) == 1
    row = calls[0][2]["headings"][0]
    assert row["first_paragraph"] == "三作品の配役を比べる。"
    assert row["last_paragraph"] == "誰が欲望し、誰の身体が見られるかが異なる。"
    assert "抽象物が移動する見出しは避ける" in STAGE_GUIDANCE[
        "redraft_section_heading_v31"
    ]


def test_heading_id_mismatch_recovers_without_second_generation(tmp_path):
    worker, calls = _heading_worker(tmp_path, mismatched_id=True)
    claimed = SimpleNamespace(run_id="run", node_run_id="node")
    result = section_heading.run(worker, claimed, _heading_context())
    assert len(calls) == 1
    assert result["proposed_html"] == HTML
    assert "heading ID/order" in result["heading_revision_report_v31"]["failure"]
    assert result["human_review_items"][0]["blocks_render"] is False
    prose.validate_handoff(result)
