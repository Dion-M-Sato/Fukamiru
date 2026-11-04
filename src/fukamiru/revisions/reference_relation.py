"""Evidence-bound links between translated manifestations and original works."""

from __future__ import annotations

import re
from typing import Any

_RELATION_TYPES = {
    "translated_from", "translation_of", "original_title", "uniform_title",
    "alternate_title", "edition_of", "work_equivalent",
}


def _key(value: Any) -> str:
    return re.sub(r"[^\w]", "", str(value or "").casefold())


def _explicit_title_matches(value: str, original_title: str) -> bool:
    if _key(value) == _key(original_title):
        return True
    prefix = original_title.strip().rstrip(".").casefold()
    text = value.strip().casefold()
    if not prefix or not text.startswith(prefix):
        return False
    tail = text[len(prefix):].strip(" .:：")
    # NDL may append a Japanese note to the labelled original-title field.
    # Another Latin subtitle is not an exact original-title match.
    return bool(tail and not tail[0].isascii())


def _explicit_original_title(candidate: dict[str, Any]) -> tuple[str, str] | None:
    """Accept a labelled original-title field, never a title-search hypothesis."""
    for item in candidate.get("original_title_audit") or []:
        if not isinstance(item, dict) or item.get("parser_rule") not in {
            "ndl_original_title_section", "publisher_explicit_relation",
        }:
            continue
        if item.get("source_url") != candidate.get("url"):
            continue
        value = str(item.get("original_title") or "").strip()
        if value:
            return value, str(item["source_url"])
    if candidate.get("original_title_relation_verified") is True:
        value = str(candidate.get("original_title") or "").strip()
        for proof in (candidate.get("field_evidence") or {}).get("original_title") or []:
            if not isinstance(proof, dict) or proof.get("source_url") != candidate.get("url"):
                continue
            if proof.get("source_type") in {
                "provider_detail_record", "publisher_explicit_relation",
                "catalog_explicit_relation",
            } and value:
                return value, str(proof["source_url"])
    return None


def resolve_work_relations_v29(
    source: dict[str, Any], candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return auditable relation edges; never infer translation from title similarity."""
    japanese = [
        item for item in candidates
        if str(item.get("publication_language") or "").casefold() in {"ja", "jpn"}
    ]
    originals = [
        item for item in candidates
        if str(item.get("publication_language") or "").casefold()
        not in {"ja", "jpn", "", "unknown"}
        and item.get("v29_render_status") == "render_ready"
        and item.get("url") and item.get("authors")
    ]
    attempts: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    for translation in japanese:
        explicit = _explicit_original_title(translation)
        for original in originals:
            attempt = {
                "translation_candidate_id": translation.get("candidate_id"),
                "original_candidate_id": original.get("candidate_id"),
                "mode": "relation_resolution",
                "relation_types_checked": sorted(_RELATION_TYPES),
                "status": "missing_explicit_original_title",
            }
            if explicit and _explicit_title_matches(
                explicit[0], str(original.get("title") or "")
            ):
                attempt["status"] = "confirmed"
                edges.append({
                    "type": "translation_of",
                    "source_candidate_id": translation.get("candidate_id"),
                    "target_candidate_id": original.get("candidate_id"),
                    "source": explicit[1],
                    "evidence_fields": {
                        "original_title": explicit[0],
                        "original_record_title": original.get("title"),
                        "translation_title": translation.get("title"),
                        "original_authors": list(original.get("authors") or []),
                    },
                    "original_record_source": original.get("url"),
                    "confidence": "high",
                    "source_mention_matches_translation": (
                        _key(source.get("title")) == _key(translation.get("title"))
                    ),
                })
            elif explicit:
                attempt["status"] = "explicit_original_title_mismatch"
            attempts.append(attempt)
    return {
        "relation_resolution_mode": "relation_resolution" if originals else "not_applicable",
        "relation_resolution_attempt_count": len(attempts),
        "relation_resolution_success_count": len(edges),
        "relation_resolution_attempts": attempts,
        "work_relation_edges": edges,
    }
