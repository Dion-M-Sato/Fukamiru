"""Small, source-labelled work cards for V31 prose and its works cited list.

These cards describe a *work*, never a manifestation that the author has been
proved to consult.  Unmatched catalogue searches are deliberately not promoted
to bibliographic facts.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
import xml.etree.ElementTree as ET
from collections.abc import Callable
from typing import Any

_PERIODICAL = re.compile(
    r"^(?:月刊|週刊|季刊|別冊)|"
    r"(?:アフタヌーン|ヤングアニマル|ヤングキングアワーズ|ジュニアそれいゆ)$"
)


def quoted_titles(text: str) -> list[str]:
    """Return complete outer titles; nested titles are part of that title."""
    depth = 0
    start = 0
    titles: list[str] = []
    for index, character in enumerate(text):
        if character == "『":
            if depth == 0:
                start = index + 1
            depth += 1
        elif character == "』" and depth:
            depth -= 1
            if depth == 0:
                title = text[start:index].strip()
                if 2 <= len(title) <= 150 and "\n" not in title:
                    titles.append(title)
    return list(dict.fromkeys(titles))


def _key(value: str) -> str:
    return re.sub(
        r"[\s・:：―－—−–()（）『』「」]+", "",
        unicodedata.normalize("NFKC", value),
    ).casefold()


def _matching_catalogue_title(wanted: str, found: str) -> bool:
    found = found.strip().strip("『』")
    if _key(wanted) == _key(found):
        return True
    return bool(re.match(
        r"^" + re.escape(wanted) + r"(?:\s*[（(]?\d|\s*第\d|\s*[上下巻]|\s*[:：])",
        found,
    ))


def _scholarly_title(title: str, references: list[str], *, nested: bool) -> bool:
    wanted = _key(title)
    for reference in references:
        full = _key(reference)
        short = _key(re.split(r"\s*[:：]\s*|——", reference, maxsplit=1)[0])
        if wanted in {full, short} or (len(short) > 5 and wanted.startswith(short)):
            return True
        # A creative work nested in a scholarship title is not thereby a work
        # discussed in the manuscript. A separate paragraph-plan mention can
        # still admit it as an analysis object.
        if nested and wanted and wanted in full:
            return True
    return False


def candidate_titles(context: dict[str, Any]) -> list[dict[str, Any]]:
    """Inventory contracted works, excluding titles only in research citations."""
    scholarly = [
        str(entry.get("title") or entry.get("work_title") or "")
        for entry in context.get("reference_entries") or []
        if isinstance(entry, dict)
    ]
    by_title: dict[str, dict[str, Any]] = {}
    for plan in context.get("semantic_paragraph_plan") or []:
        if not isinstance(plan, dict):
            continue
        section_id = str(plan.get("section_id") or "")
        for title in quoted_titles(str(plan.get("paragraphs") or "")):
            if _scholarly_title(title, scholarly, nested=False):
                continue
            row = by_title.setdefault(title, {"title": title, "section_ids": [], "locations": []})
            if section_id and section_id not in row["section_ids"]:
                row["section_ids"].append(section_id)
            row["locations"].append("semantic_paragraph_plan")
    # Argument nodes sometimes name a comparison that the short paragraph plan
    # omits.  Keep it available, but do not turn every title in the input memo
    # or bibliography into a work-card search.
    contracts = [item for item in context.get("section_contracts") or [] if isinstance(item, dict)]
    for node in context.get("argument_nodes") or []:
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("id") or "")
        claim_id = str(node.get("claim_id") or "")
        sections = [
            str(contract.get("section_id") or "") for contract in contracts
            if node_id in (contract.get("evidence_ids") or [])
            or node_id in (contract.get("warrant_ids") or [])
            or claim_id in (contract.get("claim_ids") or [])
        ]
        if not sections:
            continue
        for title in quoted_titles(str(node.get("text") or "")):
            if _scholarly_title(title, scholarly, nested=True):
                continue
            row = by_title.setdefault(title, {"title": title, "section_ids": [], "locations": []})
            for section_id in sections:
                if section_id and section_id not in row["section_ids"]:
                    row["section_ids"].append(section_id)
            row["locations"].append("argument_node")
    return list(by_title.values())


def build_work_cards(
    context: dict[str, Any], *,
    catalogue_search: Callable[[str], list[Any]] | None = None,
    corpus_search: Callable[[str], list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Look up each candidate once and keep only exact-title evidence.

    Network/corpus failures are recorded on the card; they cannot create a
    fabricated synopsis or abort the manuscript Run.
    """
    cards: list[dict[str, Any]] = []
    for row in candidate_titles(context):
        title = row["title"]
        card: dict[str, Any] = {
            **row,
            "kind": "periodical" if _PERIODICAL.search(title) else "creative_work",
            "creator": None,
            "publisher": None,
            "publication_date": None,
            "catalogue_description": None,
            "catalogue_record_title": None,
            "catalogue_url": None,
            "corpus_excerpt": None,
            "corpus_source": None,
            "lookup_status": "unsearched",
        }
        if catalogue_search:
            try:
                matches = [
                    item for item in catalogue_search(title)
                    if _matching_catalogue_title(title, str(getattr(item, "title", "")))
                ]
                if matches:
                    item = matches[0]
                    card.update(
                        creator=(list(item.authors)[0] if item.authors else None),
                        publisher=item.publisher,
                        publication_date=item.publication_date,
                        catalogue_record_title=item.title,
                        catalogue_description=(
                            str(item.provider_payload.get("description") or "")[:500] or None
                        ),
                        catalogue_url=item.url,
                        lookup_status="catalogue_match",
                    )
                else:
                    card["lookup_status"] = "catalogue_unmatched"
            except Exception as exc:  # Preserve an inspectable unresolved card.
                card["lookup_status"] = "catalogue_error"
                card["lookup_error"] = type(exc).__name__ + ": " + str(exc)[:160]
        if corpus_search:
            try:
                hit = next(
                    (item for item in corpus_search(title)
                     if title in str(item.get("text") or "")),
                    None,
                )
                if hit:
                    card["corpus_excerpt"] = str(hit.get("text") or "")[:500]
                    card["corpus_source"] = str(
                        hit.get("source_span_id") or hit.get("chunk_id") or ""
                    ) or None
            except Exception as exc:
                card["corpus_error"] = type(exc).__name__ + ": " + str(exc)[:160]
        cards.append(card)
    return cards


def cards_for_section(cards: list[dict[str, Any]], section_id: str) -> list[dict[str, Any]]:
    return [card for card in cards if section_id in card.get("section_ids", [])]


def mentioned_creative_works(html: str, cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Only works actually named in argument prose belong in the appendix."""
    titles = set(titles_in_argument_prose(html))
    return [
        card for card in cards
        if card.get("kind") == "creative_work" and card.get("title") in titles
    ]


def titles_in_argument_prose(html: str) -> list[str]:
    root = ET.fromstring(html)
    titles: list[str] = []

    def text(node: ET.Element) -> str:
        if node.tag == "sup" and (
            node.get("data-citation-generated") == "evidence-footnote-v1"
            or any(anchor.get("role") == "doc-noteref" for anchor in node.iter("a"))
        ):
            return ""
        return (node.text or "") + "".join(text(child) + (child.tail or "") for child in node)

    for section in root.iter("section"):
        if section.get("data-reference-section") or section.get("data-citation-section"):
            continue
        for paragraph in section.iter("p"):
            titles.extend(quoted_titles(text(paragraph)))
    return list(dict.fromkeys(titles))


def append_primary_work_rows(
    html: str, cards: list[dict[str, Any]]
) -> tuple[str, list[dict[str, Any]]]:
    """Add a distinct, idempotent works list inside the terminal bibliography.

    The catalogue record identifies a work for readers. It does not establish
    which edition the author consulted, so no inferred volume/edition is added.
    """
    root = ET.fromstring(html)
    for parent in root.iter():
        for child in list(parent):
            if child.get("data-reference-generated") == "v31-primary-work":
                parent.remove(child)
    bibliography = next((
        section for section in reversed(list(root.iter("section")))
        if section.get("data-reference-section")
        or any("参考文献" in "".join(heading.itertext()) for heading in section.iter("h2"))
    ), None)
    if bibliography is None or not cards:
        return ET.tostring(root, encoding="unicode", short_empty_elements=False), []
    section_id = str(bibliography.get("id") or "bibliography")
    header = ET.SubElement(bibliography, "p", {
        "id": section_id + ":ref:primary-works-heading",
        "data-reference-generated": "v31-primary-work",
    })
    header.text = "対象作品（使用版は未確定）"
    edits: list[dict[str, Any]] = [{
        "operation": "render_primary_work_heading", "block_id": header.get("id"),
    }]
    for card in cards:
        title = str(card["title"])
        block_id = (
            section_id + ":ref:primary-work:"
            + hashlib.sha256(title.encode("utf-8")).hexdigest()[:16]
        )
        row = ET.SubElement(bibliography, "p", {
            "id": block_id,
            "data-reference-generated": "v31-primary-work",
            "data-primary-work-title": title,
        })
        creator = str(card.get("creator") or "").strip()
        publisher = str(card.get("publisher") or "").strip()
        date = str(card.get("publication_date") or "").strip()
        parts = [creator + "『" + title + "』" if creator else "『" + title + "』"]
        record_title = str(card.get("catalogue_record_title") or "").strip()
        if record_title and _key(record_title) != _key(title):
            parts.append("確認レコード『" + record_title + "』")
        if publisher:
            parts.append(publisher)
        if date:
            parts.append(date)
        if card.get("lookup_status") != "catalogue_match":
            parts.append("書誌未確認")
        row.text = "、".join(parts) + "。"
        edits.append({
            "operation": "render_primary_work_entry", "block_id": block_id,
            "title": title, "lookup_status": card.get("lookup_status"),
        })
    return ET.tostring(root, encoding="unicode", short_empty_elements=False), edits
