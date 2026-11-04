"""Apply independently verified metadata bound to a complete record identity."""

from __future__ import annotations

import unicodedata
from typing import Any
from urllib.parse import urlsplit


def _identity(value: Any) -> Any:
    if isinstance(value, list):
        return tuple(_identity(item) for item in value)
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def reconcile_verified_publisher_metadata(
    context: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Fill missing publishers only from unambiguous, identity-matched evidence.

    Evidence must name the reference, title and author, carry corroborated
    provenance and a public HTTP(S) source, and match every supplied identity
    field. A conflicting value is left for human review.
    """
    references = []
    corrections = []
    for source in context.get("reference_entries") or []:
        if not isinstance(source, dict) or source.get("publisher"):
            references.append(source)
            continue
        candidates = []
        for evidence in dict(source.get("field_evidence") or {}).get("publisher") or []:
            if not isinstance(evidence, dict):
                continue
            identity = evidence.get("bibliographic_identity")
            if not isinstance(identity, dict) or not all(
                identity.get(key) for key in ("reference_id", "title", "author")
            ):
                continue
            if any(
                _identity(source.get(key)) != _identity(value) for key, value in identity.items()
            ):
                continue
            url = str(evidence.get("source_url") or "")
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                continue
            if parsed.username or parsed.password:
                continue
            if evidence.get("verification_status") != "corroborated":
                continue
            if not str(evidence.get("value") or "").strip():
                continue
            candidates.append(evidence)
        values = {_identity(item["value"]) for item in candidates}
        if len(values) != 1:
            references.append(source)
            continue
        evidence = candidates[0]
        reference = dict(source)
        reference["publisher"] = str(evidence["value"]).strip()
        reference["provenance"] = {
            **dict(reference.get("provenance") or {}),
            "publisher": {
                "kind": "retrieved_metadata",
                "source": "publisher_listing",
                "source_url": evidence["source_url"],
                "confidence": 1.0,
            },
        }
        corrections.append(
            {
                "reference_id": str(reference.get("reference_id") or ""),
                "field": "publisher",
                "value": reference["publisher"],
                "source_url": evidence["source_url"],
                "policy": "identity-bound-publisher-evidence",
            }
        )
        references.append(reference)
    if not corrections:
        return context, []
    return {
        **context,
        "reference_entries": references,
        "bibliography_reference_normalizations": [
            *list(context.get("bibliography_reference_normalizations") or []),
            *corrections,
        ],
    }, corrections
