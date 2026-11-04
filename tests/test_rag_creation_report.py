from __future__ import annotations

import json
import re
from pathlib import Path

from fukamiru.workflow.templates import corpus_sync_workflow, ingestion_workflow

ROOT = Path(__file__).resolve().parents[1]


def test_rag_report_embeds_current_source_and_runtime_versions() -> None:
    source = json.loads(
        (ROOT / "docs/diagrams/knowledge-ingestion.workflow.json").read_text(encoding="utf-8")
    )
    repair = source["retrievalInputRepair"]
    assert repair["version"] == 2 and repair["hard_max_tokens"] == 1024
    assert repair["canonical_sources_immutable"] is True
    assert repair["resume"] == "same_run_index_only_preserve_failed_attempts"
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
