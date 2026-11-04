"""One bounded V31 heading edit after the final body prose is available."""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any

from fukamiru.documents.canonical_html import CanonicalHTMLValidator
from fukamiru.errors import RunCancelled
from fukamiru.revisions import prose_redraft as prose

_HEADING = re.compile(
    r'(?P<before><section id="(?P<section_id>[^"]+)"><h2 '
    r'id="(?P<heading_id>[^"]+)"[^>]*data-authoring-role="'
    r'(?P<role>introduction|body|conclusion)"[^>]*>)'
    r'(?P<text>[^<]*)(?P<after></h2>)'
)


def run(worker: Any, claimed: Any, context: dict[str, Any]) -> dict[str, Any]:
    original = str(context.get("proposed_html") or "")
    matches = list(_HEADING.finditer(original))
    if not matches:
        raise ValueError("V31 heading edit requires rendered article section headings")
    paragraphs = prose.inventory(original)
    first_paragraph = {}
    last_paragraph = {}
    for paragraph in paragraphs:
        first_paragraph.setdefault(
            str(paragraph["section_id"]), str(paragraph["visible_text"])[:500]
        )
        last_paragraph[str(paragraph["section_id"])] = str(
            paragraph["visible_text"]
        )[:380]
    contracts = {
        str(item.get("section_id") or ""): item
        for item in context.get("section_contracts") or []
        if isinstance(item, dict)
    }
    payload_rows = []
    for match in matches:
        rendered_id = match.group("section_id")
        source_id = worker._authoring_redraft_section_key_v31(
            rendered_id, context
        )
        contract = contracts.get(source_id, {})
        payload_rows.append({
            "section_id": rendered_id,
            "heading_id": match.group("heading_id"),
            "article_role": match.group("role"),
            "text": html.unescape(match.group("text")),
            "first_paragraph": first_paragraph.get(rendered_id, ""),
            "last_paragraph": last_paragraph.get(rendered_id, ""),
            "section_result": str(contract.get("section_output") or "")[:300],
        })
    failure = ""
    try:
        answer = worker._authoring_invoke(
            claimed, stage="redraft_section_heading_v31", context=context,
            payload={"headings": payload_rows},
            operation_name="redraft_section_headings:single_pass",
        )
        proposed = answer.get("headings")
        if not isinstance(proposed, list) or len(proposed) != len(payload_rows):
            raise ValueError("heading output changed section coverage")
        for source, row in zip(payload_rows, proposed, strict=True):
            if (
                not isinstance(row, dict)
                or str(row.get("section_id") or "") != source["section_id"]
                or str(row.get("heading_id") or "") != source["heading_id"]
            ):
                raise ValueError("heading output changed section or heading ID/order")
    except RunCancelled:
        raise
    except Exception as exc:
        proposed = [
            {"section_id": row["section_id"], "heading_id": row["heading_id"],
             "text": row["text"], "reason": ""}
            for row in payload_rows
        ]
        failure = f"{type(exc).__name__}: {str(exc)[:300]}"

    reviews = []
    substitutions = {}
    for source, row in zip(payload_rows, proposed, strict=True):
        candidate = str(row.get("text") or "").strip()
        accepted = bool(candidate and len(candidate) <= 120 and "\n" not in candidate)
        if not accepted:
            candidate = source["text"]
        substitutions[source["heading_id"]] = candidate
        reviews.append({
            "section_id": source["section_id"],
            "heading_id": source["heading_id"],
            "before": source["text"], "after": candidate,
            "changed": candidate != source["text"], "accepted": accepted,
            "reason": (
                str(row.get("reason") or "") if accepted
                else "見出し出力が空または過長です。"
            ),
        })

    # The existing heading editor checks fixed literals and the DOM manifest.
    # Recover a bad suggestion locally so one heading cannot discard the other
    # useful edits or trigger another model call.
    protected_ids = set(context.get("human_protected_block_ids") or [])
    protected_ids.update(
        item["heading_id"] for item in prose.heading_inventory(original)
        if item["heading_id"] not in substitutions
    )
    editable = prose.heading_inventory(original, protected_ids)
    for index, review in enumerate(reviews):
        trial = [
            {"heading_id": item["heading_id"], "text": (
                substitutions[item["heading_id"]]
                if item["heading_id"] == review["heading_id"] else item["text"]
            )}
            for item in editable
        ]
        try:
            prose.apply_heading_edits(original, trial, protected_ids)
        except Exception as exc:
            substitutions[review["heading_id"]] = review["before"]
            reviews[index]["after"] = review["before"]
            reviews[index]["changed"] = False
            reviews[index]["accepted"] = False
            reviews[index]["reason"] = f"固定要素との不整合: {type(exc).__name__}"
    edits = [
        {"heading_id": item["heading_id"], "text": substitutions[item["heading_id"]]}
        for item in editable
    ]
    try:
        revised, _ = prose.apply_heading_edits(original, edits, protected_ids)
        CanonicalHTMLValidator().assert_valid(revised)
    except Exception as exc:
        failure = f"{type(exc).__name__}: {str(exc)[:300]}"
        revised = original
        for review in reviews:
            review["after"] = review["before"]
            review["changed"] = False
            review["accepted"] = False
            review["reason"] = failure

    report = {
        "policy": "v31-heading-single-pass-after-body-v1",
        "source_html_sha256": prose.digest(original),
        "target_html_sha256": prose.digest(revised),
        "structure_preserved": True,
        "heading_reviews": reviews,
        "changed_heading_ids": [
            item["heading_id"] for item in reviews if item["changed"]
        ],
        "failure": failure,
    }
    report_root = (
        Path(worker.settings.paths.runtime_root) / "outputs" / claimed.run_id
        / "heading-revision" / claimed.node_run_id
    )
    report_root.mkdir(parents=True, exist_ok=True)
    report_path = report_root / "heading-revision.report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report_path"] = str(report_path)
    report["report_artifact_id"] = worker._register_artifact(
        claimed, report_path, "authoring_heading_revision_report",
        "節見出しの最終編集記録", media_type="application/json",
    )
    review_items = list(context.get("human_review_items") or [])
    if failure:
        review_items.append({
            "review_item_id": "heading-revision:single-pass",
            "section_id": "manuscript", "sentence_id": "",
            "category": "japanese_heading_revision", "reason": failure,
            "source_ids": [], "recommended_check": "節見出しの自然さを確認",
            "blocks_render": False, "status": "open",
        })
    surface = dict(context.get("manuscript_surface_quality_gate") or {})
    if surface:
        surface["target_html_sha256"] = prose.digest(revised)
    return {
        **context,
        "proposed_html": revised,
        "reference_rendered_html_v26": revised,
        "final_quality_sha256": prose.digest(revised),
        "heading_revision_report_v31": report,
        "manuscript_surface_quality_gate": surface,
        "human_review_items": review_items,
    }
