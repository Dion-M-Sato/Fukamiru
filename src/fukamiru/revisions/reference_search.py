"""Bounded parallel bibliography discovery; hypotheses never become metadata."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from fukamiru.revisions.reference_web import (
    enrich_from_web_v29,
    general_web_search,
    is_researcher_contribution_listing,
    parse_ndl_detail_metadata,
    parse_page_metadata,
    parse_researcher_contribution_items,
)
from fukamiru.revisions.references import (
    REFERENCE_RESOLUTION_POLICY_V22,
    JapaneseReferenceService,
)


def build_search_hypotheses(entry: dict, supplied: dict | None = None) -> dict:
    """Keep unverified aliases in the search plan, never in canonical fields."""
    supplied = supplied or {}
    known_titles = []
    known_authors = []
    titles = list(dict.fromkeys([
        *known_titles, *(str(entry.get(field)) for field in ("original_title", "translated_title")
                         if entry.get(field)), *(supplied.get("titles") or []),
    ]))
    authors = list(dict.fromkeys([
        *known_authors, *(supplied.get("authors") or []),
    ]))
    return {
        "titles": titles, "authors": authors,
        "title_hypotheses": [{"value": value, "type": "original_or_alternate_title_hypothesis"}
                             for value in titles],
        "author_hypotheses": [{"value": value, "type": "authority_name_hypothesis"}
                              for value in authors],
        "provenance": "search_hypothesis_only",
        "source": supplied.get("source") or "instruction_examples_and_source_fields_search_only",
    }


def query_variants(entry: dict, hypotheses: dict | None = None) -> list[str]:
    return [item["query"] for item in query_plan(entry, hypotheses)]


def _legacy_query_variants(entry: dict, hypotheses: dict) -> list[str]:
    titles = [entry.get("title"), entry.get("original_title"),
              entry.get("translated_title"), *hypotheses.get("titles", [])]
    authors = [entry.get("author"), *hypotheses.get("authors", [])]
    authors += [re.sub(r"[,，]\s*\d{4}.*$", "", str(a)) for a in authors if a]
    qualifiers = " ".join(str(entry.get(k) or "") for k in
                          ("volume", "edition", "translators"))
    variants = [
        " ".join((str(t), str(a), qualifiers)).strip()
        for t in titles if t for a in authors if a
    ]
    variants.extend(str(t) for t in titles if t)
    return list(dict.fromkeys(variants))[:8]


def query_plan(entry: dict, hypotheses: dict | None = None) -> list[dict]:
    """Recall-oriented, auditable queries; hypotheses never become field evidence."""
    hypotheses = hypotheses or {}
    title = str(entry.get("title") or "").strip()
    author = str(entry.get("author") or "").strip()
    alternate_titles = [
        str(value).strip() for value in (
            entry.get("original_title"), entry.get("translated_title"),
            *(hypotheses.get("titles") or []),
        ) if value
    ]
    names = [author, *(str(value) for value in hypotheses.get("authors") or [])]
    names = [re.sub(r"[,，]\s*\d{4}.*$", "", name).strip() for name in names if name]
    rows: list[dict] = []

    def add(query: str, reason: str, fields: tuple[str, ...]) -> None:
        query = re.sub(r"\s+", " ", query).strip()
        if query and all(row["query"] != query for row in rows):
            rows.append({
                "query": query, "query_reason": reason,
                "known_fields_used": list(fields),
                "target_missing_fields": [
                    field for field in ("authors", "translators", "editors", "publisher",
                                        "year", "isbn", "edition", "volume")
                    if not entry.get(field)
                ],
                "target_source_class": "structured_bibliography",
            })

    add(title, "exact_title", ("title",))
    if author and author != "自著":
        add(f"{title} {author}", "title_author", ("title", "author"))
    normalized = re.sub(r"[\s:：・（）()\[\]「」『』]+", " ", title).strip()
    add(normalized, "punctuation_normalized_title", ("title",))
    short_title = re.split(r"\s*[:：]\s*", title, maxsplit=1)[0].strip()
    add(short_title, "title_without_subtitle", ("title",))
    for field in ("translators", "publisher", "year", "volume", "edition"):
        value = entry.get(field)
        if isinstance(value, list):
            value = " ".join(str(item) for item in value)
        if value:
            add(f"{title} {value}", f"title_{field}", ("title", field))
    for name in names[1:3]:
        add(f"{title} {name}", "title_author_alias", ("title", "author_hypothesis"))
    for alternate in alternate_titles[:3]:
        add(alternate, "alternate_title", ("title_hypothesis",))
        if names:
            alternate_name = names[1] if len(names) > 1 else names[0]
            add(f"{alternate} {alternate_name}", "alternate_title_author",
                ("title_hypothesis", "author"))
    for field in ("isbn", "doi"):
        if entry.get(field):
            add(str(entry[field]), "exact_identifier", (field,))
    return rows[:16]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def publisher_page(url: str, timeout: float = 15) -> dict:
    """Inspect an external public metadata link, never a local/private address.

    Redirects are intentionally disabled: arbitrary metadata cannot redirect a
    worker into its own API or cloud metadata endpoints.
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return {"url": url, "status": "unsafe_url"}
    try:
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            return {"url": url, "status": "unsafe_url"}
        request = urllib.request.Request(url, headers={"User-Agent": "Fukamiru bibliography/21"})
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout) as response:
            data = response.read(1_000_001)
            charset = response.headers.get_content_charset() or "utf-8"
        if len(data) > 1_000_000:
            return {"url": url, "status": "page_too_large"}
        try:
            text = data.decode(charset, errors="replace")
        except LookupError:
            text = data.decode("utf-8", errors="replace")
        title = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
        metadata = (
            parse_ndl_detail_metadata(text)
            if parsed.hostname == "ndlsearch.ndl.go.jp"
            else parse_page_metadata(text)
        )
        contribution_listing = is_researcher_contribution_listing({
            "url": url, "title": title.group(1) if title else "",
        })
        return {
            "url": url,
            "status": "retrieved",
            "title": title.group(1) if title else "",
            "metadata_excerpt": re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text))[:50_000],
            "structured_metadata": metadata,
            "contribution_listing": contribution_listing,
            "contribution_items": (
                parse_researcher_contribution_items(text, url) if contribution_listing else []
            ),
        }
    except Exception as exc:
        return {"url": url, "status": "unavailable", "error": type(exc).__name__}


def discover_reference(
    entry: dict,
    providers: tuple,
    hypotheses: dict | None = None,
    *,
    workflow_version: int = 21,
) -> dict:
    hypotheses = build_search_hypotheses(entry, hypotheses)
    entry = {**entry, "search_aliases": hypotheses}
    plan = query_plan(entry, hypotheses) if workflow_version >= 29 else [
        {"query": query, "query_reason": "legacy_bibliographic_query",
         "known_fields_used": [], "target_missing_fields": []}
        for query in _legacy_query_variants(entry, hypotheses)
    ]
    variants = [item["query"] for item in plan]

    def search(provider):
        candidates, attempts = [], []
        pending, seen = list(variants), set()
        if provider.name == "ndl_search":
            constrained = next(
                (row["query"] for row in plan if row["query_reason"] == "title_author"), None
            )
            if constrained:
                pending.remove(constrained)
                pending.insert(0, constrained)
        elif provider.name == "open_library":
            pending = [
                row["query"] for row in plan
                if row["query_reason"] in {
                    "exact_title", "alternate_title", "title_without_subtitle",
                }
            ]
        reason = "initial_bibliographic_query"
        information_fields = (
            "authors", "translators", "editors", "responsibility_raw", "language",
            "publisher", "publication_date", "isbn", "edition", "volume",
            "series_title", "original_title", "provider_record_id",
        )
        observed_information: set[tuple[str, str]] = set()
        if workflow_version < 29:
            query_budget = 8
        elif provider.name == "ndl_search":
            query_budget = 16
        elif provider.name == "open_library":
            query_budget = 3
        else:
            query_budget = 5
        while pending and len(seen) < query_budget:
            query = pending.pop(0)
            if query in seen:
                continue
            index = len(seen)
            seen.add(query)
            try:
                for retry in range(2):
                    try:
                        if index == 0 and hasattr(provider, "search_bibliographic"):
                            hits = provider.search_bibliographic(
                                entry["title"],
                                entry.get("author", "") if query != entry["title"] else "",
                                limit=20,
                            )
                            search_mode = "bibliographic_fields"
                        else:
                            hits = provider.search(query, limit=20)
                            search_mode = "query_string"
                        break
                    except Exception as exc:
                        transient = "timed out" in str(exc).casefold() or isinstance(
                            exc, TimeoutError
                        )
                        if provider.name != "ndl_search" or not transient or retry:
                            raise
                        attempts.append({
                            "query": query, "status": "timeout_retry",
                            "error_class": type(exc).__name__, "retry_number": 1,
                            "backoff_seconds": 0.25,
                        })
                        time.sleep(0.25)
                gained: list[dict[str, str]] = []
                for hit in hits:
                    payload = hit.provider_payload or {}
                    for field in information_fields:
                        value = getattr(hit, field, None)
                        if value in (None, "", (), []):
                            value = payload.get(field)
                        if value in (None, "", (), []):
                            continue
                        marker = (field, str(value))
                        if marker not in observed_information:
                            gained.append({"field": field, "value": str(value)[:160]})
                            observed_information.add(marker)
                candidates.extend(hits)
                state = JapaneseReferenceService.resolve_with_candidates(
                    entry, candidates, workflow_version=workflow_version
                )
                attempt = {
                    "query": query,
                    "query_reason": next((row["query_reason"] for row in plan
                                          if row["query"] == query), reason),
                    "known_fields_used": next((row["known_fields_used"] for row in plan
                                                if row["query"] == query), []),
                    "target_missing_fields": next((row["target_missing_fields"] for row in plan
                                                   if row["query"] == query), []),
                    "target_source_class": "structured_bibliography",
                    "provider": provider.name,
                    "attempt_number": len(attempts) + 1,
                    "status": state["resolution_status"],
                    "candidate_count": len(hits),
                    "result_count": len(hits),
                    "eligible_count": len(state.get("eligible_work_candidates") or []),
                    "new_information_gain": gained,
                }
                if workflow_version >= 22:
                    attempt.update(
                        search_mode=search_mode,
                        per_query_limit=20,
                        limit_saturated=len(hits) >= 20,
                        pagination_enabled=False,
                    )
                attempts.append(attempt)
                if workflow_version >= 22:
                    # An early exact hit does not prove that every translation,
                    # volume, or edition has been enumerated.  Run the bounded
                    # query plan once and retain every eligible manifestation.
                    reason = "enumerate_unspecified_manifestations"
                    continue
                if state["resolution_status"] == "resolved":
                    break
                status = state["resolution_status"]
                if status == "ambiguous":
                    qualifiers = " ".join(
                        str(entry.get(k) or "") for k in ("volume", "edition", "translators")
                    )
                    refined = f"{entry['title']} {entry.get('author', '')} {qualifiers}".strip()
                    if not qualifiers.strip() or refined in seen:
                        break  # preserve all versions for human choice, never guess
                    pending.insert(0, refined)
                    reason = "edition_or_volume_ambiguous"
                elif status == "low_match":
                    pending = [q for q in variants if q not in seen] + pending
                    reason = "identity_low_match_author_or_title_variant"
                else:
                    title_queries = [
                        entry.get("original_title"),
                        entry.get("translated_title"),
                        entry.get("title"),
                    ]
                    pending = [str(q) for q in title_queries if q and str(q) not in seen] + pending
                    reason = "no_hits_relax_author_and_try_original_translated_title"
            except Exception as exc:
                attempt = {
                    "query": query,
                    "status": "provider_unavailable",
                    "error_class": type(exc).__name__,
                    "error": str(exc),
                }
                if workflow_version >= 22:
                    attempt.update(
                        query_reason=reason,
                        per_query_limit=20,
                        limit_saturated=False,
                        pagination_enabled=False,
                    )
                attempts.append(attempt)
                if provider.name == "ndl_search" and "timed out" in str(exc).casefold():
                    reason = "recover_from_query_timeout"
                    continue
                break
        successful = [item for item in attempts if "candidate_count" in item]
        return {
            "provider": provider.name,
            "candidates": candidates,
            "attempts": attempts,
            "provider_status": (
                "partial_success" if successful and any(
                    attempt.get("status") == "provider_unavailable" for attempt in attempts
                ) else "available_with_hits"
                if any(int(item.get("candidate_count") or 0) for item in successful)
                else "available_no_hits"
                if successful
                else "provider_unavailable"
            ),
        }

    primary = [provider for provider in providers if provider.name == "ndl_search"]
    secondary = [provider for provider in providers if provider.name != "ndl_search"]
    searches = [search(provider) for provider in primary]
    if secondary:
        with ThreadPoolExecutor(
            max_workers=len(secondary), thread_name_prefix="bibliography-provider"
        ) as pool:
            searches.extend(pool.map(search, secondary))
    candidates = [c for result in searches for c in result["candidates"]]
    result = JapaneseReferenceService.resolve_with_candidates(
        entry, candidates, workflow_version=workflow_version
    )
    result["search_attempts"] = [
        {k: v for k, v in r.items() if k != "candidates"} for r in searches
    ]
    result["search_hypotheses"] = hypotheses
    result["structured_admission_audit"] = [
        {
            key: item.get(key)
            for key in (
                "provider", "candidate_id", "title", "authors", "year",
                "container_title", "doi", "isbn", "identity_score",
                "score_components", "eligibility_status", "exclusion_reasons",
                "identity_query_provenance",
            )
        }
        for item in result.get("raw_candidate_hits") or []
        if isinstance(item, dict)
    ]
    if workflow_version >= 22:
        result["provider_statuses"] = [
            {
                "provider": search["provider"],
                "status": search["provider_status"],
                "attempt_count": len(search["attempts"]),
                "successful_attempt_count": sum(
                    "candidate_count" in attempt for attempt in search["attempts"]
                ),
                "failed_attempt_count": sum(
                    attempt.get("status") == "provider_unavailable"
                    for attempt in search["attempts"]
                ),
                "per_query_limit_saturated": any(
                    attempt.get("limit_saturated") is True for attempt in search["attempts"]
                ),
                "pagination_enabled": False,
            }
            for search in searches
        ]
        completeness = {
            "policy_id": "bounded_manifestation_search_v22",
            "status": "search_incomplete",
            "display_label": str(
                REFERENCE_RESOLUTION_POLICY_V22["search_incomplete_display_label"]
            ),
            "query_variant_limit": 16 if workflow_version >= 29 else 8,
            "generated_query_variant_count": len(variants),
            "query_variant_limit_saturated": len(variants) >= (
                16 if workflow_version >= 29 else 8
            ),
            "per_query_limit": 20,
            "pagination_enabled": False,
            "pagination_followed": False,
            "raw_hit_total": int(result.get("raw_hit_total") or 0),
            "observed_provider_record_ids": sorted(
                {f"{candidate.provider}:{candidate.provider_record_id}" for candidate in candidates}
            ),
            "eligible_manifestation_ids": [
                str(item.get("candidate_id") or "")
                for item in result.get("eligible_work_candidates") or []
                if str(item.get("candidate_id") or "")
            ],
            "provider_states": result["provider_statuses"],
            "query_attempts": [
                {
                    "provider": search["provider"],
                    **{key: value for key, value in attempt.items() if key != "error"},
                }
                for search in searches
                for attempt in search["attempts"]
            ],
            "provider_failure_detected": any(
                search["provider_status"] == "provider_unavailable" for search in searches
            ),
            "per_query_limit_saturated": any(
                attempt.get("limit_saturated") is True
                for search in searches
                for attempt in search["attempts"]
            ),
            "retained_candidate_ids": [
                str(item.get("candidate_id") or "")
                for item in result.get("candidate_matches") or []
                if str(item.get("candidate_id") or "")
            ],
        }
        reason_codes = ["pagination_disabled"]
        if completeness["provider_failure_detected"]:
            reason_codes.append("provider_unavailable")
        if completeness["per_query_limit_saturated"]:
            reason_codes.append("per_query_limit_saturated")
        if completeness["query_variant_limit_saturated"]:
            reason_codes.append("query_variant_limit_saturated")
        completeness["incompleteness_reasons"] = reason_codes
        completeness["truncation_indicators"] = {
            "query_variant_limit_saturated": completeness["query_variant_limit_saturated"],
            "per_query_limit_saturated": completeness["per_query_limit_saturated"],
            "provider_failure_detected": completeness["provider_failure_detected"],
            "pagination_disabled": True,
        }
        signature_payload = {
            key: value for key, value in completeness.items() if key != "display_label"
        }
        completeness["search_signature"] = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    signature_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
        )
        result["search_completeness"] = completeness
        for manifestation in result.get("expanded_reference_entries") or []:
            if isinstance(manifestation, dict):
                manifestation["search_completeness"] = dict(completeness)
        if result.get("resolution_status") == "manifestations_retained":
            result["resolution_status"] = "found_manifestations_retained_search_incomplete"
            result["edition_status"] = "found_unspecified_manifestations_retained_search_incomplete"
            action = dict(result.get("user_action_required") or {})
            action.update(
                code="confirm_consulted_manifestations_search_incomplete",
                message=(
                    completeness["display_label"] + "を独立項目として保持しました。"
                    "検索は件数制限付きでページ送りを行わないため、最終Revisionの前に"
                    "実際に参照した版・巻を確認してください。"
                ),
                search_signature=completeness["search_signature"],
                search_incompleteness_reasons=reason_codes,
            )
            result["user_action_required"] = action
            result["final_revision_blocked"] = True
            result["conditional_proposal_allowed"] = True

    def publisher_urls(candidate):
        payload = candidate.provider_payload or {}
        yield from payload.get("publisher_urls") or []
        resource = payload.get("resource") or {}
        yield (resource.get("primary") or {}).get("URL")
        primary = payload.get("primary_location") or {}
        yield primary.get("landing_page_url")

    publisher_candidates = candidates
    if workflow_version >= 22:
        eligible_source_ids = {
            str(source.get("candidate_id") or "")
            for item in result.get("eligible_work_candidates") or []
            for source in item.get("source_records") or []
        }
        publisher_candidates = [
            candidate
            for candidate in candidates
            if f"{candidate.provider}:{candidate.provider_record_id}" in eligible_source_ids
        ]
    urls = list(
        dict.fromkeys(
            str(url)
            for c in publisher_candidates
            for url in publisher_urls(c)
            if url and urllib.parse.urlsplit(str(url)).hostname not in {"doi.org", "dx.doi.org"}
        )
    )
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="publisher-page") as pool:
        result["publisher_checks"] = list(pool.map(publisher_page, urls[:12]))
    result["publisher_check_status"] = "attempted" if urls else "publisher_url_not_found"
    if workflow_version >= 29:
        checked_pages = {
            str(page.get("url")): page
            for page in result["publisher_checks"]
            if isinstance(page, dict)
        }

        def fetch_web_page(url: str) -> dict:
            return checked_pages.get(url) or publisher_page(url)

        result = enrich_from_web_v29(
            result,
            search=general_web_search,
            fetch=fetch_web_page,
            seed_urls=tuple(
                dict.fromkeys(
                    [
                        *[
                            str(item.get("url"))
                            for item in result.get("candidate_matches") or []
                            if isinstance(item, dict) and item.get("url")
                        ],
                        *checked_pages,
                        *[
                            str(item.get("url"))
                            for item in result.get("raw_candidate_hits") or []
                            if isinstance(item, dict)
                            and "ndlsearch.ndl.go.jp/books/" in str(item.get("url") or "")
                        ],
                    ]
                )
            ),
        )
        by_id = {
            str(item.get("candidate_id")): item
            for item in result.get("candidate_matches") or []
            if isinstance(item, dict)
        }
        for expanded in result.get("expanded_reference_entries") or []:
            if not isinstance(expanded, dict):
                continue
            candidate = by_id.get(str(expanded.get("candidate_id")))
            if candidate:
                for field in (
                    "authors",
                    "translators",
                    "editors",
                    "publication_language",
                    "record_type",
                    "bibliography_kind",
                    "publisher",
                    "year",
                    "original_title",
                    "original_title_relation_verified",
                    "translated_title",
                    "is_translation",
                    "publication_place",
                    "series",
                    "edition",
                    "volume",
                    "pages",
                    "doi",
                    "isbn",
                    "url",
                    "field_evidence",
                    "metadata_conflicts",
                    "responsibility_sources",
                ):
                    if field in candidate:
                        expanded[field] = candidate[field]
    return result
