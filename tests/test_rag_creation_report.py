from __future__ import annotations

import json
import re
import runpy
from pathlib import Path

from fukamiru.workflow.templates import corpus_sync_workflow, ingestion_workflow

ROOT = Path(__file__).resolve().parents[1]


def test_search_flow_regenerates_after_a_formatter_unindents_the_footer(tmp_path, monkeypatch):
    output = tmp_path / "docs" / "diagrams"
    output.mkdir(parents=True)
    source_name = "knowledge-ingestion.workflow.json"
    html_name = "knowledge-ingestion.html"
    (output / source_name).write_bytes((ROOT / "docs/diagrams" / source_name).read_bytes())
    document = (ROOT / "docs/diagrams" / html_name).read_text(encoding="utf-8")
    document = re.sub(
        r'<section id="knowledge-search-report-flow".*?</section>', "", document, flags=re.S
    )
    document = re.sub(r"(?m)^[ \t]*<footer>", "<footer>", document)
    (output / html_name).write_text(document, encoding="utf-8")
    script = ROOT / "scripts/sync_search_report_diagram.py"
    module = runpy.run_path(str(script))
    module["main"].__globals__["ROOT"] = tmp_path
    monkeypatch.setattr("sys.argv", [str(script)])
    module["main"]()
    regenerated = (output / html_name).read_text(encoding="utf-8")
    assert regenerated.count('id="knowledge-search-report-flow"') == 1
    assert "workflows" in regenerated
    monkeypatch.setattr("sys.argv", [str(script), "--check"])
    module["main"]()


def test_search_report_operation_matches_ui_linked_diagram() -> None:
    from fukamiru.authoring.codex import CodexAuthoringAdapter
    from fukamiru.knowledge.search_reports import FLOW, STAGE

    source = json.loads(
        (ROOT / "docs/diagrams/knowledge-ingestion.workflow.json").read_text(encoding="utf-8")
    )
    contract = source["searchReport"]
    assert [step["id"] for step in contract["steps"]] == FLOW
    assert contract["model"] == CodexAuthoringAdapter.execution_contract(STAGE)["model"]
    assert contract["starts_run"] is False
    assert contract["storage"] == "state.db:knowledge_search_reports"
    assert contract["bibliography"].startswith("used_sources_only")
    assert contract["repairs"] == {
        "bibliography_per_source": 1, "synthesis": 1, "provider_schema_failures": 0,
    }
    for stage, execution in contract["stages"].items():
        expected = CodexAuthoringAdapter.execution_contract(stage)
        assert execution == {key: expected[key] for key in ("model", "reasoning_effort")}
    report = (ROOT / "docs/diagrams/knowledge-ingestion.html").read_text(encoding="utf-8")
    assert 'id="knowledge-search-report-flow"' in report
    for step in contract["steps"]:
        assert step["label"] in report


def test_rag_report_embeds_current_source_and_runtime_versions() -> None:
    source = json.loads(
        (ROOT / "docs/diagrams/knowledge-ingestion.workflow.json").read_text(encoding="utf-8")
    )
    repair = source["retrievalInputRepair"]
    assert repair["version"] == 2 and repair["hard_max_tokens"] == 1024
    assert repair["canonical_sources_immutable"] is True
    assert repair["resume"] == "same_run_index_only_preserve_failed_attempts"
    scope = source["corpusSourceScope"]
    assert scope["existing_sources"] == "skip_ready_canonical_sha256"
    assert scope["parser_upgrade"] == "preserve_canonical_content"
    assert "already_ingested" in source["meta"]["note"]
    assert "未完了OCRジョブは履歴を残して取り消し" in source["meta"]["note"]
    display = source["indexProgressDisplay"]
    assert display["run_details"] == "numeric_phase_progress"
    assert display["unknown_total"] == "show_actual_count_without_percentage"
    assert display["numeric_units"] == "preserve_reported_unit"
    report = (ROOT / "docs/diagrams/knowledge-ingestion.html").read_text(encoding="utf-8")
    match = re.search(
        r'<script id="workflow-data" type="application/json">(.*?)</script>', report, re.DOTALL
    )
    assert match is not None
    embedded = json.loads(match.group(1))["workflows"]
    corpus = {
        key: value
        for key, value in source.items()
        if key not in {"relatedWorkflow", "singleWorkflow"}
    }
    assert embedded["corpus"] == corpus
    assert embedded["single"] == source["singleWorkflow"]
    for diagram, workflow in (
        (corpus, corpus_sync_workflow()),
        (source["singleWorkflow"], ingestion_workflow()),
    ):
        assert diagram["meta"]["workflowVersion"] == workflow.version
        runtime_ids = {node.id for node in workflow.nodes}
        for node in diagram["nodes"]:
            if "runtime_node" in node:
                assert node["runtime_node"] in runtime_ids
        assert [node["id"] for node in diagram["nodes"]] == diagram["mainPath"]
        for edge in diagram.get("recoveryEdges", []):
            assert edge["target"] in workflow.next_nodes(edge["source"], edge["condition"])
        expected_routes = {
            "sqlite_busy_or_locked",
            "encrypted_pdf",
            "empty_embedding_offsets",
            "unresolved_ocr_language",
        }
        if diagram is corpus:
            expected_routes.update(
                {
                    "layout_analysis_failed",
                    "ocr_completed_during_ingestion",
                    "llm_language_rate_limit",
                }
            )
        assert {route["cause"] for route in diagram["recoveryRoutes"]} == expected_routes
        preparation = diagram["pdfRecoveryPreparation"]
        assert preparation["execution"] == "subprocess"
        assert preparation["job_identity_committed_before_render"] is True
        from fukamiru.ocr.pdf_recovery_worker import (
            ASSEMBLY_BATCH_PAGES,
            MAX_PREPARATION_ATTEMPTS,
        )

        assert preparation["assembly_batch_pages"] == ASSEMBLY_BATCH_PAGES
        assert preparation["max_preparation_attempts"] == MAX_PREPARATION_ATTEMPTS
        assert preparation["phases"] == [
            "pdf_recovery_resume",
            "pdf_recovery_render",
            "pdf_recovery_save",
            "pdf_recovery_validate",
            "pdf_recovery_publish",
        ]
