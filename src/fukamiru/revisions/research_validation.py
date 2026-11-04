"""Stable research obligations and source-grounded, read-only adjudication."""

from __future__ import annotations

import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fukamiru.errors import ExternalServiceUnavailable
from fukamiru.literature.transport import ProviderRequestError


class LiteratureSearchDeferred(ExternalServiceUnavailable):
    """Carry completed searches into the worker's durable delayed retry."""

    def __init__(self, context: dict[str, Any], retry_after_seconds: float):
        self.context = context
        self.retry_after_seconds = retry_after_seconds
        super().__init__("Literature provider capacity unavailable; partial searches preserved")


def academic_variants(values: list[str]) -> list[str]:
    """Accept compact database queries, without truncating a claim into a query."""
    queries = []
    for value in values:
        if not isinstance(value, str) or re.search(r"[\r\n]", value):
            continue
        query = re.sub(r"\s+", " ", value).strip()
        if (
            query and len(query) <= 96 and len(query.split()) <= 12
            and not re.search(r"[。！？;\n]|に関する|について|における|としての", query)
        ):
            queries.append(query)
    return list(dict.fromkeys(queries))[:6]


def compact_query_fallback(query: str) -> list[str]:
    """Reuse only supplied terms when the planner returns explanatory prose."""
    parts = re.split(r"に関する|について|における|としての|[、。・；;：:\n]|の|と", query)
    terms = [part.strip(" 『』「」\"") for part in parts if part.strip()]
    return academic_variants([" ".join(terms[:4]), *terms[:3]])


def search_all_providers(
    providers: tuple[Any, ...], variants: list[str], limit: int,
    *, completed_queries: dict[str, list[str]] | None = None,
    pending_queries: dict[str, list[str]] | None = None,
) -> list[dict]:
    """Independent provider failures cannot cancel other providers' searches."""

    def search(provider: Any) -> dict:
        candidates = []
        attempts = 0
        error = None
        completed = list((completed_queries or {}).get(provider.name, []))
        pending = []
        retry_at = None
        ordered = [
            query for query in dict.fromkeys([
                *((pending_queries or {}).get(provider.name, [])), *variants,
            ]) if query not in completed
        ][:6]
        # NDL indexes Japanese bibliography; international indexes also need the
        # independently planned English variants, rather than a translated claim.
        ordered.sort(key=lambda q: (
            not bool(re.search(r"[ぁ-んァ-ヶ一-龥]", q))
            if provider.name == "ndl_search" else bool(re.search(r"[ぁ-んァ-ヶ一-龥]", q))
        ))
        for index, variant in enumerate(ordered):
            if variant in completed:
                continue
            attempts += 1
            try:
                candidates.extend(provider.search(variant, limit=limit))
                completed.append(variant)
            except Exception as exc:
                error = str(exc)
                if isinstance(exc, ProviderRequestError) and exc.outcome in {
                    "rate_limited", "budget_exhausted", "temporary_failure",
                }:
                    pending = [q for q in ordered[index:] if q not in completed]
                    retry_at = time.time() + max(2, exc.retry_after_seconds)
                    break
                # A failed variant must not suppress unrelated variants.
        unique = {}
        for candidate in candidates:
            key = getattr(candidate, "provider_record_id", None) or str(candidate)
            unique.setdefault(key, candidate)
        return {
            "provider": provider.name,
            "candidates": list(unique.values()),
            "query_count": attempts,
            "error": error,
            "attempts": getattr(provider, "request_outcomes", []),
            "completed_queries": completed,
            "pending_queries": pending,
            "retry_at": retry_at,
        }

    if not providers:
        return []
    with ThreadPoolExecutor(
        max_workers=len(providers), thread_name_prefix="research-provider"
    ) as pool:
        return list(pool.map(search, providers))


def subclaim_units(plan: dict[str, Any]) -> list[dict[str, Any]]:
    claims = list(
        dict.fromkeys(str(c).strip() for c in plan.get("subclaims", []) if str(c).strip())
    )
    claims = claims or [str(plan["query"])]
    return [
        {
            "unit_id": plan["obligation_id"]
            + ":"
            + hashlib.sha256(claim.encode()).hexdigest()[:16],
            "query": claim,
            "parent_obligation": plan["query"],
        }
        for claim in claims
    ]


def search_variants(query: str) -> list[str]:
    titles = re.findall(r"[『「\"]([^』」\"]+)[』」\"]", query)
    clauses = [part.strip() for part in re.split(r"[。；;\n]|(?:について|に関する)", query)]
    values = [*titles, *clauses, query]
    return list(dict.fromkeys(value for value in values if value))[:6]


def requires_semantic_query_planning(query: str) -> bool:
    """Escalate compound claims whose subclaims cannot be split mechanically."""
    clauses = [part.strip() for part in re.split(r"[。；;\n]", query) if part.strip()]
    if len(clauses) > 1:
        return True
    return bool(
        re.search(
            r"比較|対比|相違|共通|関係|関連|因果|影響|媒介|接続|連関|"
            r"どのように|いかに|なぜ|理由|を通じて|を介して|"
            r"\b(?:compare|versus|between|relationship|impact|effect|cause|why|how)\b",
            query,
            flags=re.IGNORECASE,
        )
    )


def plan_queries(assignments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            **item,
            # V30 authoring supplies a stable atomic-proposition obligation id.
            # Legacy manuscript workflows do not, so retain their historical
            # deterministic research:* identity unchanged.
            "obligation_id": str(item.get("obligation_id") or "research:")
            + (
                ""
                if item.get("obligation_id")
                else hashlib.sha256(
                    json.dumps(item, sort_keys=True, ensure_ascii=False).encode()
                ).hexdigest()[:24]
            ),
            "variants": search_variants(str(item.get("query") or "")),
            "policy": (
                "authoring-atomic-evidence-v1"
                if item.get("obligation_id")
                else "research-obligations-v2"
            ),
        }
        for item in assignments
    ]


def grounded_support(verdict: dict[str, Any], records: list[dict[str, Any]]) -> bool:
    """Model verdicts cannot turn absent excerpts into confirmed evidence."""
    if verdict.get("status") not in {"supports_subclaim", "inference_bridge"}:
        return False
    supplied = {str(item["candidate_key"]): item for item in records}
    citations = verdict.get("citations")
    if not isinstance(citations, list) or not citations:
        return False
    for citation in citations:
        if not isinstance(citation, dict):
            return False
        source = supplied.get(str(citation.get("candidate_key") or ""))
        quote = citation.get("quote")
        if (
            source is None
            or not isinstance(quote, str)
            or not quote.strip()
            or "[…]" in quote
            or quote not in str(source.get("excerpt") or "")
        ):
            return False
        if (source.get("provenance") or {}).get("source_level") == "bibliographic_metadata":
            return False
        acquisition = (source.get("provenance") or {}).get("content_acquisition") or {}
        uncertain = acquisition.get("unverified_passages") or []
        if acquisition.get("quality_status") == "image_review_required":
            excerpt = str(source.get("excerpt") or "")
            ranges = acquisition.get("uncertain_ranges")
            if ranges and acquisition.get("extraction_sha256") == hashlib.sha256(
                excerpt.encode()).hexdigest():
                occurrences = [m.start() for m in re.finditer(re.escape(quote), excerpt)]
                if not any(not any(start < position + len(quote) and end > position
                                   for start, end in ranges) for position in occurrences):
                    return False
            elif not uncertain or any(quote in text or text in quote for text in uncertain):
                return False
            elif acquisition.get("extraction_sha256"):
                # An admission-budget trim changed the offsets. Do not guess.
                return False
        if quote.strip() == str(source.get("title") or "").strip():
            return False
        if not re.search(r"[ぁ-ん\s。.!?]", quote.strip()):
            # A generic word/heading (e.g. Machine or 日本) is not a proposition.
            return False
    return bool(str(verdict.get("rationale") or "").strip())
