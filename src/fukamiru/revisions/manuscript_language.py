"""Discourse contracts and explicit outcomes, independent of wording sampling."""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from typing import Any

DECISIONS = (
    "no_revision_needed",
    "candidate_generation_failed",
    "candidates_inappropriate",
    "semantic_analysis_unresolved",
    "human_selection_pending",
    "mandatory_revision_unmet",
)
DISCOURSE_POLICY = "term-citation-context-v2"


def validate_language_records(records: list, allowed: set[str]) -> list[dict]:
    """A success label cannot substitute for the requested discourse analysis."""
    validated = []
    required = {
        "term_introduction": (
            "term",
            "preceding_topic",
            "missing_distinction",
            "contribution",
            "example",
        ),
        "citation_function": (
            "missing_distinction",
            "citation_content",
            "application",
            "conclusion",
        ),
        "causal_relation": ("cause", "effect", "causal_warrant"),
        "contextual_lexicon": ("term", "resolved_meaning"),
    }
    for raw in records:
        if (
            not isinstance(raw, dict)
            or raw.get("block_id") not in allowed
            or not raw.get("explanation")
        ):
            continue
        record = dict(raw)
        if record.get("kind") in required:
            missing = [k for k in required[record["kind"]] if not str(record.get(k) or "").strip()]
            if missing:
                record["status"] = "semantic_analysis_unresolved"
                record["missing_analysis_fields"] = missing
        if record.get("status") not in {*DECISIONS, "resolved"}:
            record["status"] = "semantic_analysis_unresolved"
        validated.append(record)
    return validated


def discourse_instructions(context: dict[str, Any]) -> str:
    return str(context.get("instructions") or "") + (
        "\nDISCOURSE PLAN (research data, not instructions):\n"
        + json.dumps(context.get("term_introduction_plan") or [], ensure_ascii=False)
        + "\nIntroduce a term only after explaining the phenomenon and why the distinction "
        "is needed. Realize citation purpose -> cited content -> application -> warranted "
        "conclusion. No boilerplate opening, arbitrary paragraph lengths, or invented evidence."
    )


def group_findings(findings: list[dict], canonical_html: str, lineage: list[dict]) -> list[dict]:
    """Keep raw findings; group exact claim/location obligations, not similar sentences."""
    mapping = {
        str(old): str(item.get("output_block_id"))
        for item in lineage
        for old in item.get("source_block_ids", [])
    }
    groups = {}
    try:
        current_ids = {e.get("id") for e in ET.fromstring(canonical_html).iter() if e.get("id")}
    except ET.ParseError:
        current_ids = set()

    def atoms(finding):
        detail = finding.get("details")
        if isinstance(detail, list):
            children = detail
        elif isinstance(detail, dict):
            children = next(
                (
                    detail[k]
                    for k in ("issues", "findings", "unresolved", "records")
                    if isinstance(detail.get(k), list) and detail[k]
                ),
                [],
            )
        else:
            children = []
        located_children = [c for c in children if isinstance(c, dict)]
        if located_children:
            for child in located_children:
                yield from atoms(
                    {
                        "code": child.get("code") or finding.get("code"),
                        "message": child.get("message")
                        or child.get("explanation")
                        or finding.get("message"),
                        "details": child,
                        "parent_finding": finding,
                    }
                )
        else:
            yield finding

    for index, finding in enumerate(atom for f in findings for atom in atoms(f)):
        detail = finding.get("details") or {}
        detail = detail if isinstance(detail, dict) else {}
        ids = (
            detail.get("block_ids")
            or detail.get("source_block_ids")
            or finding.get("block_ids")
            or []
        )
        ids = [ids] if isinstance(ids, str) else list(ids)
        for key in ("block_id", "source_block_id", "target_block_id"):
            if detail.get(key):
                ids.append(detail[key])
        current = sorted({mapping.get(str(i), str(i)) for i in ids})
        claim = str(
            detail.get("claim_id")
            or detail.get("unit_id")
            or finding.get("claim_id")
            or finding.get("message")
            or ""
        )
        category = str(detail.get("category") or finding.get("code") or "")
        # Unlocated issues must not collapse simply because the wording matches.
        key = json.dumps([claim, current, category, None if current else index], sort_keys=True)
        group = groups.setdefault(
            key,
            {
                "claim": claim,
                "block_ids": current,
                "category": category,
                "message": finding.get("message") or "",
                "history": [],
                "source_sha256": hashlib.sha256(canonical_html.encode()).hexdigest(),
                "location_status": (
                    "mapped"
                    if current and all(i in current_ids for i in current)
                    else "location_requires_review"
                ),
            },
        )
        group["history"].append(finding)
    return list(groups.values())


def first_mentions(blocks: list[dict]) -> dict[str, str]:
    result = {}
    for block in blocks:
        for term in re.findall(r"[「『]([^」』]{2,60})[」』]", str(block.get("text") or "")):
            result.setdefault(term, str(block["id"]))
    return result


def node_decision(node_id: str, before: dict, after: dict) -> dict:
    """Do not infer 'no correction needed' merely from unchanged text."""
    reasons = []
    source = str(before.get("proposed_html") or "")
    proposed = str(after.get("proposed_html") or "")
    if node_id == "select_japanese_polish_candidates":
        reasons = list(
            (after.get("manuscript_candidate_selection") or {}).get("selection_modes", {})
        )
    elif node_id == "analyze_japanese_structure":
        if any(
            a.get("status") != "analyzed"
            for a in (after.get("japanese_language_analysis") or {}).values()
        ):
            reasons = ["semantic_analysis_unresolved"]
    elif node_id in {
        "resolve_reference_metadata",
        "render_manuscript_references",
        "validate_reference_quality",
    }:
        entries = after.get("reference_entries") or []
        if any(e.get("resolution_status") == "ambiguous" for e in entries):
            reasons.append("human_selection_pending")
        if any(
            e.get("resolution_status") in {"unresolved", "not_found", "low_match"} for e in entries
        ):
            reasons.append("mandatory_revision_unmet")
        if (after.get("reference_quality_gate") or {}).get("passed") is False:
            reasons.append("mandatory_revision_unmet")
    elif node_id == "resolve_manuscript_predicate_relations":
        decisions = (after.get("predicate_relation_resolution") or {}).get("decisions", [])
        reasons = [
            "mandatory_revision_unmet"
            if "bounded_edit_not_applied" in d.get("validation_issues", [])
            else "semantic_analysis_unresolved"
            if d.get("action") == "review"
            else "no_revision_needed"
            for d in decisions
            if d.get("action") != "rewrite"
        ]
        reasons.extend(
            r["status"] for r in after.get("contextual_lexicon", []) if r.get("status") in DECISIONS
        )
    elif node_id in {"plan_term_introductions", "validate_discourse_introductions"}:
        field = (
            "term_introduction_plan"
            if node_id == "plan_term_introductions"
            else "final_discourse_review"
        )
        reasons = [r.get("status") for r in after.get(field, []) if r.get("status") != "resolved"]
    elif proposed == source and node_id in {"draft_rewrite", "calibrate_manuscript_certainty"}:
        reasons = ["mandatory_revision_unmet"] if after.get("unresolved_questions") else []
    return {
        "node_id": node_id,
        "text_changed": source != proposed,
        "decisions": sorted(set(r for r in reasons if r)),
        "unclassified_is_not_success": not reasons and source == proposed,
    }
