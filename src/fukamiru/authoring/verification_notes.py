"""Move verification-status prose to paragraph-addressed human review by exact deletion."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import Any
from xml.etree import ElementTree as ET

from fukamiru.authoring.evidence_contracts import HumanReviewItem
from fukamiru.documents.canonical_html import CanonicalHTMLValidator
from fukamiru.errors import NonRetryableFukamiruError
from fukamiru.revisions import prose_redraft as prose


def remove_excerpts(
    document: str, cards: list[dict[str, Any]], protected: set[str],
) -> tuple[str, list[dict[str, Any]]]:
    """Allow only verbatim deletions from editable slots; preserve all markup and notes."""
    paragraphs = prose.inventory(document, protected)
    rows = prose.exact_rows(cards, [p["paragraph_id"] for p in paragraphs], "verification notes")
    root = ET.fromstring(document)
    by_id = {p.get("id"): p for p in root.iter("p")}
    items = []
    for paragraph, card in zip(paragraphs, rows, strict=True):
        paragraph_id = paragraph["paragraph_id"]
        target = by_id[paragraph_id]
        paths = prose._slots(target, protected)
        for excerpt in card.get("editorial_only") or []:
            if not isinstance(excerpt, str) or not excerpt.strip():
                raise NonRetryableFukamiruError("Empty verification excerpt: " + paragraph_id)
            matches = [
                (path, field) for path, field in paths
                if excerpt in (getattr(prose._at(target, path), field) or "")
            ]
            if len(matches) != 1:
                raise NonRetryableFukamiruError(
                    "Verification excerpt must match one editable slot: " + paragraph_id
                )
            path, field = matches[0]
            node = prose._at(target, path)
            value = getattr(node, field) or ""
            if value.count(excerpt) != 1:
                raise NonRetryableFukamiruError("Ambiguous verification excerpt: " + paragraph_id)
            setattr(node, field, value.replace(excerpt, "", 1))
            key = hashlib.sha256((paragraph_id + "\n" + excerpt).encode()).hexdigest()[:20]
            items.append(HumanReviewItem.model_validate({
                "review_item_id": "verification-note:" + key,
                "section_id": paragraph["section_id"], "sentence_id": paragraph_id,
                "category": "verification_status", "reason": excerpt,
                "source_ids": [],
                "recommended_check": "この箇所の資料・出典・適用関係を確認してください。",
                "blocks_render": False, "status": "open",
            }).model_dump(mode="json"))
    if not items:
        return document, []
    revised = ET.tostring(root, encoding="unicode", short_empty_elements=False)
    CanonicalHTMLValidator().assert_valid(revised)
    if prose.manifest(document, protected) != prose.manifest(revised, protected):
        raise NonRetryableFukamiruError("Verification routing changed locked HTML")
    return revised, items


def route(worker: Any, claimed: Any, context: dict[str, Any], document: str,
          protected: set[str]) -> tuple[str, list[dict[str, Any]]]:
    sections: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for paragraph in prose.inventory(document, protected):
        sections[paragraph["section_id"]].append(paragraph)
    all_cards = []
    for section_id, paragraphs in sections.items():
        result = worker._authoring_invoke(
            claimed, stage="extract_paragraph_meaning_v31", context=context,
            payload={
                "output_language": "ja", "section_id": section_id,
                "purpose": "route_verification_notes_to_human_review",
                "paragraphs": paragraphs,
            },
            operation_name="route_verification_notes:" + section_id,
        )
        all_cards.extend(prose.exact_rows(
            result.get("paragraphs"), [p["paragraph_id"] for p in paragraphs],
            "verification notes",
        ))
    return remove_excerpts(document, all_cards, protected)
