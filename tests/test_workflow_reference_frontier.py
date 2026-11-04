from __future__ import annotations

from dataclasses import replace


def _worker_case():
    from test_worker import WorkerTests

    case = WorkerTests()
    case.setUp()
    return case


def test_v22_quality_passed_discoveries_outside_audit_frontier_are_retained() -> None:
    case = _worker_case()
    try:
        run_id = case._create_run("manuscript_rewrite")
        claimed = case.worker._claim(lane="ingestion_cpu")
        assert claimed is not None and claimed.run_id == run_id
        claimed = replace(
            claimed,
            node_id="validate_evidence_support",
            workflow_version="22",
            resource_lane="codex_remote",
        )

        def insufficient_audit(_claimed, _phase, units, _sources):
            return {
                "verdicts": [
                    {
                        "unit_id": unit["unit_id"],
                        "status": "insufficient",
                        "rationale": "This source does not establish the claim.",
                        "citations": [],
                    }
                    for unit in units
                ]
            }

        case.worker._readonly_manuscript_audit = insufficient_audit
        candidates = [
            {
                "provider": "crossref",
                "provider_record_id": f"outside-{index:02d}",
                "source_type": "online_paper",
                "title": f"関連研究 {index:02d}",
                "authors": ["研究者"],
                "source_url": f"https://example.invalid/{index:02d}",
                "locator": f"10.0000/example.{index:02d}",
                "excerpt": f"関連研究 {index:02d} の要旨本文です。",
                "confidence": 0.9,
                "discovery_quality": {"passed": True},
                "provenance": {"source_level": "abstract"},
            }
            for index in range(14)
        ]
        candidates.append(
            {
                "provider": "ndl_search",
                "provider_record_id": "metadata-only",
                "source_type": "online_paper",
                "title": "書誌情報だけで確認された関連研究",
                "authors": ["別の研究者"],
                "source_url": "https://example.invalid/metadata-only",
                "locator": "9780000000000",
                "excerpt": "",
                "confidence": 0.88,
                "discovery_quality": {"passed": True},
                "provenance": {"source_level": "bibliographic_metadata"},
            }
        )
        result = case.worker._manuscript_validate_evidence_support(
            claimed,
            {
                "workflow_version": 22,
                "research_search_plan": [
                    {
                        "obligation_id": "OBL-OUTSIDE",
                        "query": "関連研究の検証",
                        "subclaims": ["関連研究の位置づけ"],
                    }
                ],
                "research_discoveries": [
                    {"query": "関連研究の検証", "candidates": candidates}
                ],
            },
        ).context

        usages = result["reference_usage_records"]
        assert len(usages) == 15
        assert sum(item["support_frontier"] is True for item in usages) == 12
        assert sum(item["support_frontier"] is False for item in usages) == 3
        outside = [item for item in usages if item["support_frontier"] is False]
        assert all(item["disposition"] == "machine_detected_related" for item in outside)
        assert all(item["selected_by_support_audit"] is False for item in outside)
        assert all(item["passed_to_draft"] is False for item in outside)
        assert all(item["discovery_quality_passed"] is True for item in outside)
        assert case.worker._support_selected_reference_sources_v22(result) == []
        machine = case.worker._validated_machine_reference_sources_v22(result, [])
        assert len(machine) == 15
        assert all(item["consulted_status"] == "not_consulted" for item in machine)

        report = result["reference_usage_classification_report"]
        assert report["support_frontier_count"] == 12
        assert report["machine_detected_related_count"] == 15
        assert report["quality_passed_outside_frontier_count"] == 3
        assert report["outside_frontier_retained_count"] == 3
    finally:
        case.tearDown()
