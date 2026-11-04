"""Offline bibliography normalization, rendering and validation contracts."""

from __future__ import annotations

import copy
import html
import json
import re
from typing import Any

from fukamiru.revisions.reference_canonical import finalize_reference_v29
from fukamiru.revisions.references import JapaneseReferenceService

IDENTITY_FIELDS = (
    "title",
    "authors",
    "translators",
    "publisher",
    "year",
    "edition",
    "volume",
    "isbn",
    "record_type",
    "publication_language",
)


def _text_identity(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def _identifier_identity(value: Any) -> str:
    return re.sub(r"[^0-9a-z]", "", str(value or "").casefold())


def _candidate_identity(candidate: dict[str, Any]) -> dict[str, Any]:
    identity: dict[str, Any] = {}
    for field in IDENTITY_FIELDS:
        value = candidate.get(field)
        if field in {"authors", "translators"}:
            identity[field] = sorted({_text_identity(item) for item in value or []})
        elif field == "isbn":
            identity[field] = _identifier_identity(value)
        else:
            identity[field] = _text_identity(value)
    return identity


def _candidate_key(candidate: dict[str, Any]) -> str:
    return json.dumps(_candidate_identity(candidate), ensure_ascii=False, sort_keys=True)


def _actual_candidates(entry: dict[str, Any]) -> list[dict[str, Any]]:
    mode = entry.get("reference_output_mode_v29")
    if mode == "SINGLE":
        return [entry]
    if mode == "MULTIPLE":
        return list(entry.get("expanded_reference_entries") or [])
    return []


def _synthetic_html(source: dict[str, Any]) -> str:
    block_id = html.escape(str(source.get("source_block_id") or "REF-ROW"), quote=True)
    text = html.escape(str(source.get("original_text") or source.get("title") or ""))
    return (
        '<article id="DOC" lang="ja"><section id="REF-SEC">'
        '<h2 id="REF-HEAD">参考文献</h2>'
        f'<p id="{block_id}">{text}</p></section></article>'
    )


def evaluate_gold_reference(item: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Evaluate one frozen provider fixture without network access."""
    source = copy.deepcopy(item["source_entry"])
    actual = finalize_reference_v29(source)
    actual_candidates = _actual_candidates(actual)
    expected_candidates = item.get("expected_candidates") or []
    state_match = actual.get("reference_output_mode_v29") == item["expected_state"]
    candidate_match = sorted(map(_candidate_key, actual_candidates)) == sorted(
        map(_candidate_key, expected_candidates)
    )
    required_fields_ok = all(
        candidate.get("v29_render_status") == "render_ready"
        and not candidate.get("v29_required_missing_fields")
        and len(candidate.get("authors") or [])
        == len({_text_identity(name) for name in candidate.get("authors") or []})
        for candidate in actual_candidates
    )
    rendered, _ = JapaneseReferenceService.render_sections_v22(
        _synthetic_html(source),
        [actual],
        workflow_version=29,
        render_machine_section=False,
    )
    rendered_text = " ".join(re.sub(r"<[^>]+>", " ", rendered).split()).casefold()
    node21_ok = (
        bool(actual_candidates)
        and all(
            _text_identity(candidate.get("title")) in rendered_text
            for candidate in expected_candidates
        )
        and "要確認" not in rendered
        and "未解決" not in rendered
    )
    quality = JapaneseReferenceService.quality_report(
        rendered,
        [actual],
        workflow_version=29,
    )
    blocking = [issue for issue in quality.get("issues") or [] if issue.get("severity") == "error"]
    node22_ok = bool(item.get("expected_node22_blocking")) == bool(blocking)
    summary = {
        "index": int(item["index"]),
        "expected_state": item["expected_state"],
        "actual_state": actual.get("reference_output_mode_v29"),
        "expected_candidate_count": len(expected_candidates),
        "actual_candidate_count": len(actual_candidates),
        "state_match": state_match,
        "candidate_match": candidate_match,
        "required_fields_ok": required_fields_ok,
        "node21_ok": node21_ok,
        "node22_ok": node22_ok,
    }
    summary["passed"] = all(
        summary[key]
        for key in (
            "state_match",
            "candidate_match",
            "required_fields_ok",
            "node21_ok",
            "node22_ok",
        )
    )
    detail = {
        "actual": actual,
        "actual_candidates": actual_candidates,
        "expected_candidates": expected_candidates,
        "rendered_html": rendered,
        "node22_quality": quality,
    }
    return summary, detail


def run_gold_regression(fixture: dict[str, Any]) -> tuple[dict[str, Any], dict[int, Any]]:
    """Return the compact normal report and failure-only details."""
    results = []
    failures: dict[int, Any] = {}
    for item in fixture.get("references") or []:
        summary, detail = evaluate_gold_reference(item)
        results.append(summary)
        if not summary["passed"]:
            failures[int(item["index"])] = detail
    passed = sum(item["passed"] for item in results)
    fallback_indices = {
        item["index"] for item in fixture.get("references", []) if item.get("foreign_fallback")
    }
    fallback = [item for item in results if item["index"] in fallback_indices]
    report = {
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "foreign_fallback_triggered": len(fallback),
        "foreign_fallback_single": sum(item["actual_state"] == "SINGLE" for item in fallback),
        "foreign_fallback_multiple": sum(item["actual_state"] == "MULTIPLE" for item in fallback),
        "foreign_fallback_blocked_due_to_ambiguity": 0,
        "live_network_executed": False,
        "results": results,
    }
    return report, failures


def materialize_gold_entries(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    """Materialize the frozen provider records for a combined offline replay."""
    return [
        finalize_reference_v29(copy.deepcopy(item["source_entry"]))
        for item in fixture.get("references") or []
    ]
