from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from typing import Any

import pytest

from fukamiru.revisions.reference_inference import (
    batch_reference_inference_entries,
    project_reference_inference_entry,
)
from fukamiru.revisions.references import JapaneseReferenceService


@pytest.fixture
def worker_case():
    from test_worker import WorkerTests

    case = WorkerTests()
    case.setUp()
    try:
        yield case
    finally:
        case.tearDown()


def claimed_reference_node(case, *, workflow_version: int):
    run_id = case._create_run("manuscript_rewrite")
    claimed = case.worker._claim(lane="ingestion_cpu")
    assert claimed is not None and claimed.run_id == run_id
    return replace(
        claimed,
        node_id="resolve_reference_metadata",
        workflow_version=str(workflow_version),
        resource_lane="ingestion_cpu",
    )


def reference_entries() -> list[dict[str, Any]]:
    return [
        {
            "reference_id": reference_id,
            "parse_status": "parsed",
            "author": f"著者{suffix}",
            "authors": [f"著者{suffix}"],
            "title": f"著作{suffix}",
            "record_type": "book",
            "bibliography_kind": "book",
            "source_block_id": f"BLOCK-{suffix}",
            "entry_ordinal": index,
        }
        for index, (reference_id, suffix) in enumerate(
            (("REF-A", "A"), ("REF-B", "B"))
        )
    ]


def query_record(reference_id: str) -> dict[str, Any]:
    return {
        "reference_id": reference_id,
        "titles": [f"検索題名 {reference_id}"],
        "authors": [f"検索著者 {reference_id}"],
    }


def inference_record(reference_id: str) -> dict[str, Any]:
    return {
        "reference_id": reference_id,
        "fields": {
            "record_type": None,
            "publisher": f"出版社 {reference_id}",
            "publication_place": None,
            "series": None,
            "year": "2024",
            "container_title": None,
            "volume": None,
            "issue": None,
            "pages": None,
        },
        "rationale": "入力された著者・書名と照合した補充候補。",
        "confidence": 0.9,
    }


def test_reference_inference_projection_excludes_discovery_traces_and_bounds_batches():
    huge = "x" * 1_100_000
    entry = {
        **reference_entries()[0],
        "required_missing_fields": ["publisher", "year"],
        "web_discovery_trace": {"page_body": huge},
        "provider_payload": huge,
        "candidate_matches": [
            {
                "candidate_id": "ndl:1",
                "provider": "ndl",
                "title": "候補題名",
                "authors": ["候補著者"],
                "publisher": "候補出版社",
                "year": "2024",
                "provider_payload": huge,
            }
        ],
    }

    projected = project_reference_inference_entry(entry)
    encoded = json.dumps(projected, ensure_ascii=False)
    batches = batch_reference_inference_entries([entry] * 26, rule_set={})

    assert "web_discovery_trace" not in projected
    assert "provider_payload" not in projected
    assert "provider_payload" not in projected["candidate_matches"][0]
    assert "候補出版社" in encoded
    assert len(encoded) < 20_000
    assert [len(batch) for batch in batches] == [25, 1]


class QueryScriptTranslator:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = [deepcopy(item) for item in responses]
        self.calls: list[list[str]] = []

    def plan_reference_queries(
        self, entries: list[dict[str, Any]]
    ) -> dict[str, Any]:
        self.calls.append([str(item["reference_id"]) for item in entries])
        assert self.responses, "reference-query planner was called more than expected"
        return self.responses.pop(0)


class InferenceScriptTranslator:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = [deepcopy(item) for item in responses]
        self.calls: list[list[str]] = []

    def infer_reference_entries(
        self,
        *,
        entries: list[dict[str, Any]],
        rule_set: dict[str, Any],
    ) -> dict[str, Any]:
        del rule_set
        self.calls.append([str(item["reference_id"]) for item in entries])
        assert self.responses, "reference inference was called more than expected"
        return self.responses.pop(0)


def set_online_reference_research(case, *, enabled: bool) -> None:
    case.worker.settings = replace(
        case.worker.settings,
        translation=replace(
            case.worker.settings.translation,
            academic_research_online=enabled,
        ),
    )


def stub_successful_discovery(monkeypatch, captured_hypotheses: dict[str, Any]) -> None:
    def discover(
        entry: dict[str, Any],
        _providers: tuple[Any, ...],
        hypotheses: dict[str, Any] | None = None,
        *,
        workflow_version: int = 21,
    ) -> dict[str, Any]:
        del workflow_version
        reference_id = str(entry["reference_id"])
        captured_hypotheses[reference_id] = deepcopy(hypotheses)
        return {
            **deepcopy(entry),
            "publisher": "確認出版社",
            "year": "2024",
            "resolution_status": "resolved",
            "required_missing_fields": [],
            "candidate_matches": [],
            "final_revision_blocked": False,
            "conditional_proposal_allowed": False,
        }

    monkeypatch.setattr("fukamiru.worker.runner.discover_reference", discover)


def stub_unresolved_without_approval_side_effects(monkeypatch) -> None:
    def unresolved(
        cls,
        entry: dict[str, Any],
        _candidates: list[Any],
        *,
        workflow_version: int = 21,
    ) -> dict[str, Any]:
        del cls, workflow_version
        return {
            **deepcopy(entry),
            "resolution_status": "not_found",
            "candidate_matches": [],
            "final_revision_blocked": False,
            "conditional_proposal_allowed": False,
        }

    monkeypatch.setattr(
        JapaneseReferenceService,
        "resolve_with_candidates",
        classmethod(unresolved),
    )


def response_condition(result: dict[str, Any], phase: str) -> dict[str, Any] | None:
    return next(
        (
            item
            for item in result.get("conditional_conditions") or []
            if item.get("reason") == "response_contract_incomplete"
            and item.get("phase") == phase
        ),
        None,
    )


def test_v22_reference_query_checkpoints_valid_sibling_and_supplements_only_bad_id(
    worker_case, monkeypatch
) -> None:
    claimed = claimed_reference_node(worker_case, workflow_version=22)
    set_online_reference_research(worker_case, enabled=True)
    malformed = {"reference_id": "REF-B", "titles": ["題名B"]}  # authors missing
    translator = QueryScriptTranslator(
        [
            {"queries": [query_record("REF-A"), malformed]},
            {"queries": [query_record("REF-B")]},
        ]
    )
    worker_case.worker.translator = translator
    hypotheses: dict[str, Any] = {}
    stub_successful_discovery(monkeypatch, hypotheses)
    context = {
        "reference_entries": reference_entries(),
        "reference_rule_set": {},
        "use_online_research": True,
        "context_warnings": [],
    }

    first = worker_case.worker._manuscript_resolve_reference_metadata(
        claimed, context
    ).context
    second = worker_case.worker._manuscript_resolve_reference_metadata(
        claimed, context
    ).context

    assert translator.calls == [["REF-A", "REF-B"], ["REF-B"]]
    assert hypotheses == {
        "REF-A": query_record("REF-A"),
        "REF-B": query_record("REF-B"),
    }
    assert response_condition(first, "reference_query") is None
    assert response_condition(second, "reference_query") is None


def test_v22_reference_query_second_invalid_response_becomes_conditional_not_warning(
    worker_case, monkeypatch
) -> None:
    claimed = claimed_reference_node(worker_case, workflow_version=22)
    set_online_reference_research(worker_case, enabled=True)
    translator = QueryScriptTranslator(
        [
            {
                "queries": [
                    query_record("REF-A"),
                    {"reference_id": "REF-B", "titles": ["題名B"]},
                ]
            },
            {
                "queries": [
                    {
                        "reference_id": "REF-B",
                        "titles": ["題名B"],
                        "authors": "著者B",  # list[str] required
                    }
                ]
            },
        ]
    )
    worker_case.worker.translator = translator
    hypotheses: dict[str, Any] = {}
    stub_successful_discovery(monkeypatch, hypotheses)
    context = {
        "reference_entries": reference_entries(),
        "reference_rule_set": {},
        "use_online_research": True,
        "context_warnings": [],
    }

    result = worker_case.worker._manuscript_resolve_reference_metadata(
        claimed, context
    ).context
    rerun = worker_case.worker._manuscript_resolve_reference_metadata(
        claimed, context
    ).context

    assert translator.calls == [["REF-A", "REF-B"], ["REF-B"]]
    assert hypotheses["REF-A"] == query_record("REF-A")
    assert hypotheses["REF-B"] is None
    condition = response_condition(result, "reference_query")
    assert condition is not None
    assert condition["unit_ids"] == ["REF-B"]
    assert condition["retry_count"] == 1
    assert result["completion_status"] == "conditional_success"
    assert response_condition(rerun, "reference_query") == condition
    assert not any(
        "reference query planning unavailable" in warning
        for warning in result.get("context_warnings") or []
    )
    assert [item["reference_id"] for item in result["reference_entries"]] == [
        "REF-A",
        "REF-B",
    ]


def test_v22_reference_inference_checkpoints_valid_sibling_and_supplements_only_bad_id(
    worker_case, monkeypatch
) -> None:
    claimed = claimed_reference_node(worker_case, workflow_version=22)
    set_online_reference_research(worker_case, enabled=False)
    malformed = inference_record("REF-B")
    malformed.pop("rationale")
    translator = InferenceScriptTranslator(
        [
            {"inferences": [inference_record("REF-A"), malformed]},
            {"inferences": [inference_record("REF-B")]},
        ]
    )
    worker_case.worker.translator = translator
    stub_unresolved_without_approval_side_effects(monkeypatch)
    context = {
        "reference_entries": reference_entries(),
        "reference_rule_set": {},
        "use_online_research": False,
        "context_warnings": [],
    }

    first = worker_case.worker._manuscript_resolve_reference_metadata(
        claimed, context
    ).context
    second = worker_case.worker._manuscript_resolve_reference_metadata(
        claimed, context
    ).context

    assert translator.calls == [["REF-A", "REF-B"], ["REF-B"]]
    assert response_condition(first, "reference_inference") is None
    assert response_condition(second, "reference_inference") is None
    assert [item["resolution_status"] for item in first["reference_entries"]] == [
        "inferred",
        "inferred",
    ]
    assert [item["publisher"] for item in first["reference_entries"]] == [
        "出版社 REF-A",
        "出版社 REF-B",
    ]


def test_v22_reference_inference_second_invalid_response_becomes_conditional_not_warning(
    worker_case, monkeypatch
) -> None:
    claimed = claimed_reference_node(worker_case, workflow_version=22)
    set_online_reference_research(worker_case, enabled=False)
    missing_field = inference_record("REF-B")
    missing_field.pop("rationale")
    wrong_type = inference_record("REF-B")
    wrong_type["fields"] = "publisher=出版社B"  # object with exact fields required
    translator = InferenceScriptTranslator(
        [
            {"inferences": [inference_record("REF-A"), missing_field]},
            {"inferences": [wrong_type]},
        ]
    )
    worker_case.worker.translator = translator
    stub_unresolved_without_approval_side_effects(monkeypatch)
    context = {
        "reference_entries": reference_entries(),
        "reference_rule_set": {},
        "use_online_research": False,
        "context_warnings": [],
    }

    result = worker_case.worker._manuscript_resolve_reference_metadata(
        claimed, context
    ).context
    rerun = worker_case.worker._manuscript_resolve_reference_metadata(
        claimed, context
    ).context

    assert translator.calls == [["REF-A", "REF-B"], ["REF-B"]]
    condition = response_condition(result, "reference_inference")
    assert condition is not None
    assert condition["unit_ids"] == ["REF-B"]
    assert condition["retry_count"] == 1
    assert result["completion_status"] == "conditional_success"
    assert response_condition(rerun, "reference_inference") == condition
    assert not any(
        warning.startswith("Reference inference failed:")
        for warning in result.get("context_warnings") or []
    )
    by_id = {item["reference_id"]: item for item in result["reference_entries"]}
    assert by_id["REF-A"]["resolution_status"] == "inferred"
    assert by_id["REF-A"]["publisher"] == "出版社 REF-A"
    assert by_id["REF-B"]["resolution_status"] == "not_found"
    assert "publisher" not in by_id["REF-B"]


def test_v22_bibliography_blocker_preserves_partial_response_condition(
    worker_case, monkeypatch
) -> None:
    claimed = claimed_reference_node(worker_case, workflow_version=22)
    set_online_reference_research(worker_case, enabled=True)
    malformed = {"reference_id": "REF-B", "titles": ["題名B"]}
    translator = QueryScriptTranslator(
        [
            {"queries": [query_record("REF-A"), malformed]},
            {"queries": [{"reference_id": "REF-B", "titles": ["題名B"]}]},
        ]
    )
    worker_case.worker.translator = translator

    def discover(
        entry: dict[str, Any],
        _providers: tuple[Any, ...],
        hypotheses: dict[str, Any] | None = None,
        *,
        workflow_version: int = 21,
    ) -> dict[str, Any]:
        del hypotheses, workflow_version
        return {
            **deepcopy(entry),
            "publisher": "確認出版社",
            "year": "2024",
            "resolution_status": "ambiguous",
            "required_missing_fields": [],
            "candidate_matches": [{"title": "候補"}],
            "final_revision_blocked": True,
            "conditional_proposal_allowed": True,
        }

    monkeypatch.setattr("fukamiru.worker.runner.discover_reference", discover)
    result = worker_case.worker._manuscript_resolve_reference_metadata(
        claimed,
        {
            "reference_entries": reference_entries(),
            "reference_rule_set": {},
            "use_online_research": True,
            "context_warnings": [],
        },
    ).context

    reasons = {
        str(item.get("reason"))
        for item in result.get("conditional_conditions") or []
        if isinstance(item, dict)
    }
    assert reasons == {
        "response_contract_incomplete",
        "bibliography_confirmation_required",
    }


def test_v21_reference_query_keeps_legacy_single_call_without_partial_contract(
    worker_case, monkeypatch
) -> None:
    claimed = claimed_reference_node(worker_case, workflow_version=21)
    set_online_reference_research(worker_case, enabled=True)
    malformed = {
        "reference_id": "REF-B",
        "titles": ["題名B"],
        "authors": "legacy string",
    }
    translator = QueryScriptTranslator(
        [{"queries": [query_record("REF-A"), malformed]}]
    )
    worker_case.worker.translator = translator
    hypotheses: dict[str, Any] = {}
    stub_successful_discovery(monkeypatch, hypotheses)

    result = worker_case.worker._manuscript_resolve_reference_metadata(
        claimed,
        {
            "reference_entries": reference_entries(),
            "use_online_research": True,
            "context_warnings": [],
        },
    ).context

    assert translator.calls == [["REF-A", "REF-B"]]
    assert hypotheses["REF-B"] == malformed
    assert response_condition(result, "reference_query") is None
    assert "conditional_conditions" not in result


def test_v21_reference_inference_keeps_legacy_single_call_without_partial_contract(
    worker_case, monkeypatch
) -> None:
    claimed = claimed_reference_node(worker_case, workflow_version=21)
    set_online_reference_research(worker_case, enabled=False)
    malformed = inference_record("REF-B")
    malformed["fields"] = "legacy invalid fields"
    translator = InferenceScriptTranslator(
        [{"inferences": [inference_record("REF-A"), malformed]}]
    )
    worker_case.worker.translator = translator
    stub_unresolved_without_approval_side_effects(monkeypatch)

    result = worker_case.worker._manuscript_resolve_reference_metadata(
        claimed,
        {
            "reference_entries": reference_entries(),
            "reference_rule_set": {},
            "use_online_research": False,
            "context_warnings": [],
        },
    ).context

    assert translator.calls == [["REF-A", "REF-B"]]
    assert response_condition(result, "reference_inference") is None
    assert "conditional_conditions" not in result
    by_id = {item["reference_id"]: item for item in result["reference_entries"]}
    assert by_id["REF-A"]["resolution_status"] == "inferred"
    assert by_id["REF-B"]["resolution_status"] == "not_found"
