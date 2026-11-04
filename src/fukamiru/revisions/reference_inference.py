"""Bounded inputs for the bibliography inference fallback.

Reference discovery keeps complete provider and Web traces for audit.  Those
traces are not model inputs: the fallback only needs the source bibliography
fields and a compact view of the best candidate records.
"""

from __future__ import annotations

import json
from typing import Any

_ENTRY_FIELDS = (
    "reference_id",
    "source_reference_id",
    "original_text",
    "parse_status",
    "resolution_status",
    "required_missing_fields",
    "author",
    "authors",
    "title",
    "record_type",
    "bibliography_kind",
    "publisher",
    "publication_place",
    "series",
    "year",
    "container_title",
    "volume",
    "issue",
    "pages",
    "edition",
    "isbn",
    "doi",
    "url",
    "original_title",
    "translated_title",
    "is_translation",
    "translators",
    "editors",
    "publication_language",
    "work_identity_status",
    "edition_status",
    "consulted_status",
)

_CANDIDATE_FIELDS = (
    "candidate_id",
    "provider",
    "provider_record_id",
    "title",
    "authors",
    "responsibility_raw",
    "responsibility_source",
    "original_title",
    "translated_title",
    "translators",
    "editors",
    "publication_language",
    "is_translation",
    "edition",
    "container_title",
    "publisher",
    "publication_place",
    "series",
    "year",
    "volume",
    "issue",
    "pages",
    "doi",
    "isbn",
    "url",
    "record_type",
    "bibliography_kind",
    "identity_score",
    "identity_conflicts",
    "edition_conflicts",
    "metadata_conflicts",
    "translator_status",
    "required_missing_fields",
)

_MAX_CANDIDATES = 12
_MAX_LIST_ITEMS = 16
_MAX_STRING_CHARS = 4_000


def _bounded(value: Any) -> Any:
    if isinstance(value, str):
        return value[:_MAX_STRING_CHARS]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_bounded(item) for item in value[:_MAX_LIST_ITEMS]]
    if isinstance(value, dict):
        return {str(key): _bounded(item) for key, item in list(value.items())[:_MAX_LIST_ITEMS]}
    return str(value)[:_MAX_STRING_CHARS]


def _selected(source: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {
        field: _bounded(source[field])
        for field in fields
        if field in source and source[field] not in (None, "", [], {})
    }


def project_reference_inference_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Return only evidence needed to infer missing display fields.

    Full discovery traces, fetched page bodies, provider payloads, and query
    histories deliberately stay in the durable Node 8 result rather than the
    Codex prompt.  Candidate order is retained because discovery already ranks
    it deterministically.
    """

    projected = _selected(entry, _ENTRY_FIELDS)
    candidates = entry.get("candidate_matches")
    if isinstance(candidates, list):
        projected["candidate_matches"] = [
            _selected(candidate, _CANDIDATE_FIELDS)
            for candidate in candidates[:_MAX_CANDIDATES]
            if isinstance(candidate, dict)
        ]
    provenance = entry.get("provenance")
    if isinstance(provenance, dict):
        relevant = set(_ENTRY_FIELDS)
        projected["provenance"] = {
            str(field): _bounded(value) for field, value in provenance.items() if field in relevant
        }
    return projected


def batch_reference_inference_entries(
    entries: list[dict[str, Any]],
    *,
    rule_set: dict[str, Any],
    max_items: int = 25,
    max_serialized_chars: int = 320_000,
) -> list[list[dict[str, Any]]]:
    """Project and batch entries under a conservative prompt-input budget."""

    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for entry in entries:
        projected = project_reference_inference_entry(entry)
        proposed = [*current, projected]
        size = len(
            json.dumps(
                {"entries": proposed, "rule_set": rule_set},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        if current and (len(proposed) > max_items or size > max_serialized_chars):
            batches.append(current)
            current = [projected]
        else:
            current = proposed
    if current:
        batches.append(current)
    return batches
