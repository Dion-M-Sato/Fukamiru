"""Paragraph-level Japanese redrafting with immutable citation and HTML structure.

The authoring model only returns plain paragraph text.  Application code owns the
canonical HTML, stable IDs, footnote nodes, and evidence lineage.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any
from xml.etree import ElementTree as ET

from fukamiru.documents.canonical_html import CanonicalHTMLValidator
from fukamiru.errors import FukamiruError
from fukamiru.revisions.footnote_citations import EvidenceFootnotePlanner

QUALITY_DIMENSIONS = (
    "vocabulary",
    "collocation",
    "syntax",
    "reference_clarity",
    "abstraction",
    "rhythm",
    "academic_register",
)

_PROTECTED_PATTERNS = (
    re.compile(r"[+-]?\d[\d,._]*(?:%|％)?"),
    re.compile(r"「[^」]*」|『[^』]*』|“[^”]*”"),
    re.compile(r"\[\^[^\]]+\]|\[[0-9,;\- ]+\]"),
    re.compile(r"https?://[^\s]+"),
)


def editable_sections(
    html: str, *, protected_block_ids: set[str] | None = None
) -> list[dict[str, Any]]:
    """Read only authored sections and paragraphs that contain plain text."""

    CanonicalHTMLValidator().assert_valid(html)
    root = ET.fromstring(html)
    protected = protected_block_ids or set()
    sections: list[dict[str, Any]] = []
    for section in root.iter("section"):
        if section.get("data-citation-section") or section.get("data-reference-section"):
            continue
        paragraphs = [
            {"block_id": str(node.get("id")), "text": str(node.text or "")}
            for node in section.iter("p")
            if node.get("id") and node.get("id") not in protected
            and not list(node) and str(node.text or "").strip()
        ]
        if paragraphs:
            sections.append({"section_id": str(section.get("id")), "paragraphs": paragraphs})
    return sections


def _rephrasable_quote(token: str, propositions: list[str]) -> bool:
    """Only a named, substantial proposition may lose its exact quoted wording."""
    if not token.startswith(("「", "“")):
        return False  # Book and article titles in 『...』 stay literal.
    inner = token[1:-1].strip()
    return len(inner) >= 24 and any(
        len(proposition.strip()) >= 24
        and (inner in proposition or proposition.strip("「」『』“”") in inner)
        for proposition in propositions
    )


def _protected_tokens(
    text: str, rephrasable_propositions: list[str]
) -> tuple[Counter[str], ...]:
    tokens = [pattern.findall(text) for pattern in _PROTECTED_PATTERNS]
    tokens[1] = [
        token for token in tokens[1]
        if not _rephrasable_quote(token, rephrasable_propositions)
    ]
    return tuple(Counter(found) for found in tokens)


def _protected_information_preserved(
    original: str, revised: str, rephrasable_propositions: list[str]
) -> bool:
    before = _protected_tokens(original, rephrasable_propositions)
    after = _protected_tokens(revised, rephrasable_propositions)
    # Existing direct quotations and titles survive verbatim. Only explicitly
    # named prose propositions may lose their original quoted wording.
    return (
        before[0] == after[0]
        and not before[1] - after[1]
        and before[2] == after[2]
        and before[3] == after[3]
    )


def _citation_anchor_preserved(
    original: str,
    revised: str,
    *,
    block_id: str,
    citation_lineage: list[dict[str, Any]],
    evidence_context: list[dict[str, Any]],
) -> bool:
    evidence_by_id = {
        str(item.get("id")): item
        for item in evidence_context
        if isinstance(item, dict) and item.get("id")
    }
    for binding in citation_lineage:
        if str(binding.get("output_block_id") or binding.get("block_id") or "") != block_id:
            continue
        for evidence_id in binding.get("evidence_ids") or []:
            evidence = evidence_by_id.get(str(evidence_id))
            if evidence is None:
                raise FukamiruError(
                    f"Japanese redrafting has unknown citation evidence: {evidence_id}"
                )
            authors = evidence.get("authors") or []
            if isinstance(authors, str):
                authors = [authors]
            source_terms = [
                str(value) for value in [
                    evidence.get("title"), *authors
                ] if value and str(value) not in {"自著", "筆者", "本稿筆者"}
            ]
            if any(term in original and term not in revised for term in source_terms):
                return False
            before = EvidenceFootnotePlanner.visible_citation_units(
                original, [evidence], evidence
            )
            if before is not None and EvidenceFootnotePlanner.visible_citation_units(
                revised, [evidence], evidence
            ) is None:
                return False
    return True


def apply_revisions(
    html: str,
    revisions: list[dict[str, Any]],
    *,
    citation_lineage: list[dict[str, Any]],
    evidence_context: list[dict[str, Any]],
    locked_texts: list[str],
    protected_block_ids: set[str] | None = None,
    rephrasable_propositions: list[str] | None = None,
) -> tuple[str, list[dict[str, str]]]:
    """Apply safe plain-text edits; reject any that could erase citation anchors."""

    CanonicalHTMLValidator().assert_valid(html)
    rephrasable = rephrasable_propositions or []
    root = ET.fromstring(html)
    editable = {
        item["block_id"]: item["text"]
        for section in editable_sections(
            html, protected_block_ids=protected_block_ids
        )
        for item in section["paragraphs"]
    }
    paragraphs = {
        str(node.get("id")): node for node in root.iter("p") if node.get("id")
    }
    ids = [str(item.get("block_id") or "") for item in revisions]
    if len(ids) != len(set(ids)) or not set(ids).issubset(editable):
        raise FukamiruError("Japanese redrafting returned duplicate or unknown paragraph IDs")
    decisions: list[dict[str, str]] = []
    for item in revisions:
        block_id = str(item["block_id"])
        original = editable[block_id]
        revised = str(item.get("text") or "")
        reason = ""
        if not revised.strip() or "<" in revised or ">" in revised:
            reason = "invalid_plain_text"
        elif not _protected_information_preserved(original, revised, rephrasable):
            reason = "protected_token_changed"
        elif not _citation_anchor_preserved(
            original,
            revised,
            block_id=block_id,
            citation_lineage=citation_lineage,
            evidence_context=evidence_context,
        ):
            reason = "citation_anchor_removed"
        elif any(
            locked in original and locked not in revised
            for locked in locked_texts
            if locked and locked not in rephrasable
        ):
            reason = "locked_argument_text_changed"
        if reason:
            decisions.append({"block_id": block_id, "status": "rejected", "reason": reason})
            continue
        paragraphs[block_id].text = revised
        decisions.append({"block_id": block_id, "status": "accepted", "reason": ""})
    candidate = ET.tostring(root, encoding="unicode", short_empty_elements=False)
    CanonicalHTMLValidator().assert_valid(candidate)
    for locked in locked_texts:
        if locked and locked not in rephrasable and locked in html and locked not in candidate:
            raise FukamiruError("Japanese redrafting removed locked argument text")
    return candidate, decisions
