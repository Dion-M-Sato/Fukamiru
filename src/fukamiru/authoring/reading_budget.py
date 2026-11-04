"""Fair, question-specific reading admission budgets (not billing estimates)."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Callable
from copy import deepcopy
from typing import Any

from fukamiru.literature.content import source_passages
from fukamiru.revisions.research_validation import grounded_support

POLICY = "argument-reading-budget-v1"


def source_hints(source_context: dict[str, Any]) -> list[str]:
    """Keep quoted names as retrieval leads, without inventing title aliases."""
    text = "\n".join(str(source_context.get(key) or "") for key in (
        "original_note_paragraph", "memo_annotations",
    ))
    titles = re.findall(r"『([^『』\n]{2,80})』", text)
    concepts = re.findall(r"「([^「」\n]{2,40})」", text)
    return list(dict.fromkeys([*titles, *concepts]))[:4]


def allocations(plans: list[dict[str, Any]], total: int) -> dict[str, int]:
    """Reserve every selected question's share before any parallel work starts."""
    weights = {
        str(p["obligation_id"]): 2 if (
            p.get("evidence_requirement") == "required"
            or p.get("argument_necessity") == "core"
            or p.get("memo_annotation_kinds")
            or p.get("source_hints")
        ) else 1
        for p in plans
    }
    denominator = sum(weights.values()) or 1
    result = {key: total * weight // denominator for key, weight in weights.items()}
    for key in list(result)[:total - sum(result.values())]:
        result[key] += 1
    return result


def audit_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The actual V32 audit projection, shared with admission accounting."""
    result = []
    for source in sources:
        provenance = source.get("provenance") or {}
        content = provenance.get("content_acquisition") or {}
        result.append({
            **{key: source.get(key) for key in (
                "candidate_key", "title", "authors", "excerpt", "locator",
                "source_url", "source_type",
            )},
            "provenance": {
                **{key: provenance.get(key) for key in (
                    "source_level", "doi", "record_type", "publication_date",
                    "publication_title", "material_role", "permission", "oa_url",
                )},
                "content_acquisition": {key: content.get(key) for key in (
                    "status", "read_complete", "excerpt_complete", "stopped_reason",
                    "total_pages", "pages_examined", "source_origin",
                    "reading_scope", "context_radius",
                    "quality_status", "unverified_passages", "page_provenance", "reader_policy",
                )},
            },
        })
    return result


def estimated_tokens(units: list[dict[str, Any]], sources: list[dict[str, Any]]) -> int:
    # UTF-8 bytes / 3 is deliberately an estimate. Include a fixed prompt reserve.
    # Actual provider usage, output tokens and OCR billing are recorded elsewhere.
    payload = {"units": units, "sources": audit_sources(sources)}
    return 1024 + math.ceil(len(json.dumps(payload, ensure_ascii=False).encode()) / 3)


def fit_sources(
    units: list[dict[str, Any]], sources: list[dict[str, Any]], limit: int,
    queries: list[str],
) -> list[dict[str, Any]]:
    """Keep verbatim excerpts and distribute context across the selected works."""
    fitted = deepcopy(sources)
    while fitted and estimated_tokens(units, fitted) > limit:
        longest = max(fitted, key=lambda s: len(str(s.get("excerpt") or "")))
        text = str(longest.get("excerpt") or "")
        if len(text) <= 160:
            fitted.pop()  # An unaudited candidate remains a discovery, never negative evidence.
            continue
        longest["excerpt"] = source_passages(text, queries, limit=max(160, len(text) // 2))
        # source_passages retains 1500-character windows; enforce the admission cap too.
        longest["excerpt"] = longest["excerpt"][:max(160, len(text) // 2)]
        provenance = longest.setdefault("provenance", {})
        content = provenance.setdefault("content_acquisition", {})
        content["excerpt_complete"] = False
        content["stopped_reason"] = "audit_context_budget"
    return fitted


def work_key(source: dict[str, Any]) -> str:
    provenance = source.get("provenance") or {}
    citation = provenance.get("citation") or {}
    for key in ("source_asset_id", "doi", "isbn"):
        if value := provenance.get(key) or citation.get(key):
            return key + ":" + str(value).casefold()
    return str(provenance.get("oa_url") or source.get("source_url")
               or source["candidate_key"])


def reading_session(
    *, plan: dict[str, Any], units: list[dict[str, Any]], sources: list[dict[str, Any]],
    audit: Callable[[list[dict[str, Any]]], dict[str, Any]],
    read: Callable[[dict[str, Any], list[str], int], dict[str, Any]],
    token_limit: int, max_rounds: int, max_works: int, seconds: float,
    previous: dict[str, Any] | None = None,
    audit_identity: dict[str, Any] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Resolve a duty or preserve its gap with an explicit admission stop reason."""
    started = clock()
    ledger = {"policy": POLICY, "estimated_input_tokens": 0, "audit_submissions": 0,
              "source_read_attempts": 0, "expansion_rounds": 0, "elapsed_seconds": 0.0,
              "audited_excerpt_characters": 0, "source_read_events": [],
              **deepcopy(previous or {}), "token_limit": token_limit}
    old_elapsed = float(ledger["elapsed_seconds"])

    def fingerprint(selected: list[dict[str, Any]]) -> str:
        return hashlib.sha256(json.dumps({
            "policy": POLICY, "units": units, "sources": audit_sources(selected),
            "audit_identity": audit_identity,
        }, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    if (ledger.get("stop_reason") == "resolved"
            and ledger.get("last_audit_fingerprint") == fingerprint(sources)):
        # Revalidating all questions after another question's search must not erase
        # an unchanged resolution or charge its reserved share again.
        ledger["resolution_reuses"] = int(ledger.get("resolution_reuses", 0)) + 1
        return {"sources": sources, "units": units,
                "verdicts": ledger["last_audit_verdicts"],
                "response_contract_incomplete": [], "reading_budget": ledger}
    verdicts: list[dict[str, Any]] = []
    incomplete: list[str] = []
    audited_sources: list[dict[str, Any]] = []
    queries = list(dict.fromkeys([
        *plan.get("source_hints", []), *plan.get("variants", []), str(plan["query"]),
    ]))
    titles = re.findall(r"『([^『』\n]{2,80})』", str(
        plan.get("source_context", {}).get("original_note_paragraph") or ""))
    # Once a work is located, its title must not pull internal reading back to its index.
    queries = [query for query in queries if query not in titles] or [str(plan["query"])]
    reason = "no_candidates"

    def time_left() -> bool:
        return old_elapsed + clock() - started < seconds

    while sources:
        if not time_left():
            reason = "time_budget"
            break
        remaining = token_limit - int(ledger["estimated_input_tokens"])
        rounds_left = max(0, max_rounds - int(ledger["expansion_rounds"]))
        # Reserve space for follow-up reading; unused shares never leak to early questions.
        admission = remaining // (rounds_left + 1)
        fitted = fit_sources(units, sources, admission, queries)
        if not fitted and admission < remaining:
            # A long source paragraph can cost more than the reserved initial share.
            # Use this question's remaining share before forfeiting its first review.
            fitted = fit_sources(units, sources, remaining, queries)
        if not fitted:
            reason = "input_token_budget"
            break
        result = audit(fitted)
        ledger["estimated_input_tokens"] += estimated_tokens(units, fitted)
        ledger["audit_submissions"] += 1
        ledger["audited_excerpt_characters"] += sum(len(s.get("excerpt") or "") for s in fitted)
        audited_sources = fitted
        verdicts = result.get("verdicts", [])
        incomplete.extend(result.get("_response_contract_incomplete_unit_ids", []))
        by_unit = {v["unit_id"]: v for v in verdicts}
        resolved = []
        for unit in units:
            verdict = by_unit.get(unit["unit_id"], {})
            status = verdict.get("status")
            grounded = grounded_support(
                {**verdict, "status": "supports_subclaim"}
                if status == "contradicts" else verdict, fitted,
            )
            resolved.append(grounded and status in {
                "supports_subclaim", "inference_bridge", "contradicts",
            })
        if all(resolved):
            reason = "resolved"
            ledger["last_audit_fingerprint"] = fingerprint(fitted)
            ledger["last_audit_verdicts"] = verdicts
            break
        if len(by_unit) != len(units) or incomplete:
            reason = "audit_incomplete"
            break
        if rounds_left == 0:
            reason = "expansion_budget"
            break
        if not time_left():
            reason = "time_budget"
            break
        gaps = [str(v.get("rationale") or "") for v in verdicts
                if v.get("status") == "insufficient"]
        requested = [c.get("candidate_key") for v in verdicts
                     for c in v.get("citations", [])]
        ranked = sorted(sources, key=lambda s: s["candidate_key"] not in requested)
        works: set[str] = set()
        changed = False
        inaccessible = False
        refreshed = {s["candidate_key"]: s for s in sources}
        round_index = int(ledger["expansion_rounds"])
        for source in ranked:
            if not time_left():
                reason = "time_budget"
                break
            identity = work_key(source)
            if identity in works:
                continue
            works.add(identity)
            if len(works) > max_works:
                break
            after = read(source, [*queries, *gaps], round_index)
            ledger["source_read_attempts"] += 1
            refreshed[source["candidate_key"]] = after
            changed |= after.get("excerpt") != source.get("excerpt")
            acquisition = after.get("provenance", {}).get("content_acquisition", {})
            ledger["source_read_events"].append({
                "candidate_key": source["candidate_key"], "work": identity,
                "round": round_index + 1, "excerpt_characters": len(after.get("excerpt") or ""),
                "locator": after.get("locator"), "status": acquisition.get("status"),
                "excerpt_complete": acquisition.get("excerpt_complete"),
            })
            inaccessible |= acquisition.get("status") not in {None, "retrieved"}
        ledger["expansion_rounds"] += 1
        if reason == "time_budget":
            break
        can_expand = any(s.get("provenance", {}).get("content_acquisition", {})
                         .get("excerpt_complete") is False for s in refreshed.values())
        if not changed and not (can_expand and round_index + 1 < max_rounds):
            reason = "source_inaccessible" if inaccessible else "no_new_information"
            break
        sources = list(refreshed.values())
    ledger.update(stop_reason=reason, unresolved=reason != "resolved",
                  elapsed_seconds=round(old_elapsed + clock() - started, 3),
                  accounting="estimated audit input; excludes output and OCR billing")
    if not verdicts:
        verdicts = [{"unit_id": u["unit_id"], "status": "insufficient",
                     "rationale": "Reading unresolved: " + reason, "citations": []}
                    for u in units]
    return {"sources": audited_sources, "units": units, "verdicts": verdicts,
            "response_contract_incomplete": list(dict.fromkeys(incomplete)),
            "reading_budget": ledger}
