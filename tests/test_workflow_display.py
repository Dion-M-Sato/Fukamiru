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
    script = (root / "docs/diagrams/manuscript-generation.js").read_text(encoding="utf-8")
    assert "source.research_policy?.[current.id]" in script
    handoff = _workflow_registry()["research_source_ingestion"]
    diagram_handoff = payload["source_ingestion_workflow"]
    assert [n["id"] for n in diagram_handoff["nodes"]] == [n.id for n in handoff.nodes]
    assert [(e["from"], e["to"]) for e in diagram_handoff["edges"]] == [
        (e.source, e.target) for e in handoff.edges]
    assert "RustXNG" in payload["research_policy"]["bounded_research_retry"]["summary"]
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
