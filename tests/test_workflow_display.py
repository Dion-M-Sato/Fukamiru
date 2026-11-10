from __future__ import annotations

import json
import re
from pathlib import Path

from fukamiru.api.app import _workflow_registry
from fukamiru.authoring.paragraph_moves import PARAGRAPH_ARGUMENT_MOVE_CATALOG
from fukamiru.workflow.display import (
    NODE_FUNCTION_NAMES_JA,
    workflow_node_metadata,
    workflow_supports_proofreading_patches,
)


def test_every_current_workflow_node_has_authoritative_japanese_metadata() -> None:
    workflows = _workflow_registry()
    node_ids = {node.id for definition in workflows.values() for node in definition.nodes}

    assert node_ids <= NODE_FUNCTION_NAMES_JA.keys()
    for definition in workflows.values():
        for node in definition.nodes:
            metadata = workflow_node_metadata(definition, node.id)
            assert metadata["known_in_workflow"] is True
            assert metadata["title"] == node.title
            assert re.search(r"[ぁ-んァ-ヶ一-龠]", str(metadata["function_name_ja"]))


def test_v32_diagram_serves_current_academic_search_policy() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "docs/diagrams/manuscript-generation-data.js").read_text(encoding="utf-8")
    payload = json.loads(source.split("= ", 1)[1].rsplit(";", 1)[0])
    assert set(payload["research_policy"]) == {
        "plan_research_queries", "resolve_manuscript_evidence", "validate_evidence_support",
        "bounded_research_retry",
    }
    assert "Retry-After" in payload["research_policy"]["resolve_manuscript_evidence"]["summary"]
    assert "preferred" in payload["research_policy"]["bounded_research_retry"]["summary"]
    assert "LLMの過剰要求である" in payload["review_policy"]["human_approval"]["summary"]
    assert ("human_approval", "start_paragraph_formation", "failure") in {
        (edge["from"], edge["to"], edge["condition"]) for edge in payload["edges"]
    }
    script = (root / "docs/diagrams/manuscript-generation.js").read_text(encoding="utf-8")
    assert "source.research_policy?.[current.id]" in script
    handoff = _workflow_registry()["research_source_ingestion"]
    diagram_handoff = payload["source_ingestion_workflow"]
    assert [n["id"] for n in diagram_handoff["nodes"]] == [n.id for n in handoff.nodes]
    assert [(e["from"], e["to"]) for e in diagram_handoff["edges"]] == [
        (e.source, e.target) for e in handoff.edges]
    retry = payload["research_policy"]["bounded_research_retry"]["summary"]
    assert "RustXNG v0.30.0（Standard）" in retry
    support = payload["research_policy"]["validate_evidence_support"]["summary"]
    assert "NDLOCR-Lite／Mistral" in support


def test_patch_capability_comes_from_the_current_flow_definition() -> None:
    workflows = _workflow_registry()

    assert workflow_supports_proofreading_patches(workflows["manuscript_generator"])
    assert workflow_supports_proofreading_patches(workflows["manuscript_rewrite"])
    assert not workflow_supports_proofreading_patches(workflows["translation"])


def test_v32_dag_displays_the_complete_current_paragraph_label_catalog() -> None:
    root = Path(__file__).resolve().parents[1]
    data_source = (root / "docs/diagrams/manuscript-generation-data.js").read_text(
        encoding="utf-8"
    )
    payload = data_source.split("= ", maxsplit=1)[1].rsplit(";", maxsplit=1)[0]
    display_catalog = json.loads(payload)["paragraph_argument_move_catalog"]
    dialog = (root / "docs/diagrams/manuscript-generation.html").read_text(
        encoding="utf-8"
    )

    assert display_catalog == [dict(item) for item in PARAGRAPH_ARGUMENT_MOVE_CATALOG]
    assert f"段落の標準ラベル · {len(PARAGRAPH_ARGUMENT_MOVE_CATALOG)}種" in dialog
    assert {item["id"] for item in display_catalog} >= {
        "concrete_case_or_example",
        "case_analysis_or_close_reading",
        "warrant_or_explanatory_mechanism",
        "parallel_branch_contribution",
        "cross_branch_synthesis",
    }


def test_v32_dag_tracks_current_workflow_and_one_pass_prose() -> None:
    root = Path(__file__).resolve().parents[1]
    data_source = (root / "docs/diagrams/manuscript-generation-data.js").read_text(
        encoding="utf-8"
    )
    payload = json.loads(data_source.split("= ", maxsplit=1)[1].rsplit(";", maxsplit=1)[0])
    workflow = _workflow_registry()["manuscript_generator"]
    assert payload["diagram_version"] == "32"
    assert payload["version"] == workflow.version == "32"
    research = payload["research_policy"]
    assert "生成済みの本文と索引を先に検索" in research["resolve_manuscript_evidence"]["summary"]
    assert "300字条件は使いません" in research["validate_evidence_support"]["summary"]
    assert "書名と引用された概念を別々" in research["plan_research_queries"]["summary"]
    assert "命題別の入力推計トークン枠" in research["validate_evidence_support"]["summary"]
    assert "予算終了は未解決" in research["validate_evidence_support"]["summary"]
    assert "前後2～3ページ" in research["validate_evidence_support"]["summary"]
    assert "画像確認待ち" in research["validate_evidence_support"]["summary"]
    assert "RAGで確認済みの義務は外部調査を省き" in research["bounded_research_retry"]["summary"]

    assert [node["id"] for node in payload["nodes"]] == [
        node.id for node in workflow.nodes
    ]
    assert {
        (edge["from"], edge["to"], edge["condition"]) for edge in payload["edges"]
    } == {(edge.source, edge.target, edge.condition) for edge in workflow.edges}
    assert {
        ("diagnose_japanese_prose", "redraft_japanese_paragraphs", "success"),
        ("redraft_japanese_paragraphs", "review_japanese_sections", "success"),
        ("review_japanese_sections", "redraft_japanese_paragraphs", "failure"),
        ("review_japanese_sections", "extract_proofreading_sentences", "success"),
        ("render_manuscript_references", "redraft_body_prose", "success"),
        ("start_paragraph_formation", "plan_semantic_paragraphs", "success"),
        ("redraft_body_prose", "finalize_section_headings", "success"),
        ("finalize_section_headings", "validate_footnoted_structure", "success"),
    } <= {(edge["from"], edge["to"], edge["condition"]) for edge in payload["edges"]}

    diagram = (root / "docs/diagrams/manuscript-generation.html").read_text(
        encoding="utf-8"
    )
    diagram_script = (root / "docs/diagrams/manuscript-generation.js").read_text(
        encoding="utf-8"
    )
    assert "目次付きフォルダの統合Markdown" in diagram
    assert "source_sections" in diagram_script
    assert "目次一覧とファイル名を命題化せず" in diagram_script
    copy_source = (root / "docs/diagrams/manuscript-generator-node-copy.js").read_text(
        encoding="utf-8"
    )
    copy_payload = json.loads(copy_source.split("= ", maxsplit=1)[1].rsplit(";", maxsplit=1)[0])
    new_lane_source = re.search(r"const newLane = \{(.*?)\};", diagram_script, re.S)
    assert new_lane_source is not None
    newly_assigned_ids = set(re.findall(r"^\s+([a-z_]+):", new_lane_source.group(1), re.M))
    assert {node["id"] for node in payload["nodes"]} <= (
        {node["id"] for node in copy_payload["nodes"]} | newly_assigned_ids
    )
    menu = (root / "ui/index.html").read_text(encoding="utf-8")
    assert "Fukamiru — ワークフロー全体図 V32" in diagram
    assert "Manuscript Generator V32 実行定義" in diagram
    assert "Fukamiru · 全体図 V32" in menu
    assert "脚注番号を読点・句点の手前に置きます" in diagram_script
    assert "MANUSCRIPT_GRAPH_V32" in diagram_script
    assert "書誌同定のみの引用がある変更段落は一度だけ読後評価" in diagram_script
    assert "引用済み文献を参照候補と呼び直す" in diagram_script
    assert (
        "reverted_scaffold_candidates、citation_reader_reviews、reverted_reader_candidates"
        in diagram_script
    )
    assert "段落計画の検証状況や編集用指示は本文執筆用の指示として渡しません" in diagram_script
    assert "再起草ループは行いません" in diagram_script
    assert "start_paragraph_formation: \"discourse\"" in diagram_script
    assert "finalize_section_headings: \"submission\"" in diagram_script
    assert {
        node["id"]: node["max_attempts"] for node in payload["nodes"]
    }["redraft_body_prose"] == 1


def test_reader_brief_excerpt_policy_is_displayed_in_current_graph() -> None:
    root = Path(__file__).resolve().parents[1] / "docs/diagrams"
    policies = []
    data = (root / "manuscript-generation-data.js").read_text(encoding="utf-8")
    payload = json.loads(data.split("= ", 1)[1].rsplit(";", 1)[0])
    policies.append(payload["reader_brief_excerpt_policy"])
    assert "完全一致" in policies[-1]["summary"]
    assert "監査判断・理由・対象ID・指摘件数は変更できず" in policies[-1]["summary"]
    assert "人名の頭文字" in policies[-1]["summary"]
    assert "不明判定は引き続き停止" in policies[-1]["summary"]
    assert "固定資料の支援役割" in policies[-1]["summary"]
    assert "対象節の修正指示へ返" in policies[-1]["summary"]
    script = (root / "manuscript-generation.js").read_text(encoding="utf-8")
    assert 'current.id === "draft_rewrite" ? source.reader_brief_excerpt_policy' in script


def test_evidence_identity_generation_policy_is_displayed_in_reconciliation_nodes() -> None:
    root = Path(__file__).resolve().parents[1] / "docs/diagrams"
    policies = []
    data = (root / "manuscript-generation-data.js").read_text(encoding="utf-8")
    payload = json.loads(data.split("= ", 1)[1].rsplit(";", 1)[0])
    policies.append(payload["evidence_identity_policy"])
    assert "校正での対応変更を拒否" in policies[-1]["summary"]
    assert "直前の完了済み起草結果との完全一致" in policies[-1]["summary"]
    assert "校正後の同じエラーはこの復旧の対象外" in policies[-1]["summary"]
    script = (root / "manuscript-generation.js").read_text(encoding="utf-8")
    nodes = '["reconcile_candidate_context", "reconcile_candidate_context_after_proofreading"]'
    assert nodes in script
    assert '.includes(current.id) ? source.evidence_identity_policy' in script


def test_candidate_generation_reset_and_recovery_are_displayed_in_current_graph() -> None:
    root = Path(__file__).resolve().parents[1] / "docs/diagrams"
    data = (root / "manuscript-generation-data.js").read_text(encoding="utf-8")
    payload = json.loads(data.split("= ", 1)[1].rsplit(";", 1)[0])
    assert "人間の回答を保って" in payload["candidate_generation_policy"]["summary"]
    assert "一度本文統合へ戻" in payload["candidate_generation_policy"]["summary"]
    script = (root / "manuscript-generation.js").read_text(encoding="utf-8")
    assert 'source.candidate_generation_policy' in script


def test_proofreading_generation_policy_is_displayed_in_current_graph() -> None:
    root = Path(__file__).resolve().parents[1] / "docs/diagrams"
    data = (root / "manuscript-generation-data.js").read_text(encoding="utf-8")
    payload = json.loads(data.split("= ", 1)[1].rsplit(";", 1)[0])
    policy = payload["proofreading_generation_policy"]["summary"]
    assert "同じ世代の元本文変更は停止" in policy
    assert "旧候補・選択・適用・ログを保持" in policy
    assert "校正抽出から一度再開" in policy
    script = (root / "manuscript-generation.js").read_text(encoding="utf-8")
    assert "source.proofreading_generation_policy" in script


def test_empty_citation_projection_and_limited_recovery_are_displayed() -> None:
    root = Path(__file__).resolve().parents[1] / "docs/diagrams"
    data = (root / "manuscript-generation-data.js").read_text(encoding="utf-8")
    payload = json.loads(data.split("= ", 1)[1].rsplit(";", 1)[0])
    policy = payload["empty_citation_projection_policy"]["summary"]
    assert "出典対応がないと確定した段落も投影済み" in policy
    assert "既存の引用を全て保ち" in policy
    assert "一度校正後統合へ戻" in policy
    assert "不確実性の指摘を残して後段の本文修正へ" in policy
    assert "未確認を検証済みへ変更しません" in policy
    script = (root / "manuscript-generation.js").read_text(encoding="utf-8")
    assert "source.empty_citation_projection_policy" in script


def test_submission_revalidation_intent_and_recovery_are_displayed() -> None:
    root = Path(__file__).resolve().parents[1] / "docs/diagrams"
    data = (root / "manuscript-generation-data.js").read_text(encoding="utf-8")
    payload = json.loads(data.split("= ", 1)[1].rsplit(";", 1)[0])
    policy = payload["submission_revalidation_policy"]["summary"]
    assert "再生成依頼を本文世代をまたいで保持" in policy
    assert "承認済み候補は置き換えません" in policy
    assert "印だけを復元して一度最終保存へ" in policy
    assert "送信済み回答と対象・要求文が一致する" in policy
    assert "現在の下書きは隠しません" in policy
    script = (root / "manuscript-generation.js").read_text(encoding="utf-8")
    assert "source.submission_revalidation_policy" in script


def test_every_current_diagram_node_belongs_to_an_existing_lane() -> None:
    root = Path(__file__).resolve().parents[1] / "docs/diagrams"
    source = (root / "manuscript-generator-node-copy.js").read_text(encoding="utf-8")
    shared = json.loads(source.split("= ", 1)[1].rsplit(";", 1)[0])
    shared_lanes = {node["id"]: node["lane"] for node in shared["nodes"]}
    lane_ids = {lane["id"] for lane in shared["lanes"]}
    source = (root / "manuscript-generation-data.js").read_text(encoding="utf-8")
    graph = json.loads(source.split("= ", 1)[1].rsplit(";", 1)[0])
    script = (root / "manuscript-generation.js").read_text(encoding="utf-8")
    match = re.search(r"const newLane = \{(.*?)\};", script, re.DOTALL)
    assert match is not None
    added_lanes = dict(re.findall(r'(\w+): "(\w+)"', match[1]))
    for node in graph["nodes"]:
        lane = shared_lanes.get(node["id"]) or added_lanes.get(node["id"])
        assert lane in lane_ids, (node["id"], lane)
