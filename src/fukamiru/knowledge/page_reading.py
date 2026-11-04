"""Read-only page recovery and contextual selection; never rewrites corpus text."""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

POLICY = "contextual-pages-v1"
_CJK = re.compile(r"[ぁ-ゖァ-ヺ一-鿿]")


def compact(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold()


def text_issues(text: str) -> list[str]:
    """Conservative damage signals, not a claim of semantic/OCR accuracy."""
    issues = []
    if re.search(r"(?:[ぁ-ゖァ-ヺ一-鿿]\s+){8,}", text):
        issues.append("fragmented_reading_order")
    def rare_han(char: str) -> bool:
        if not re.fullmatch(r"[一-鿿]", char):
            return False
        try:
            encoded = char.encode("euc_jp")
            return len(encoded) > 2 or encoded[0] >= 0xD0
        except UnicodeEncodeError:
            return True

    japanese = bool(re.search(r"[ぁ-ゖァ-ヺ]", text))
    rare = [i for i, char in enumerate(text) if japanese and rare_han(char)]
    # Dense unusual glyphs are a warning, never an automatic spelling correction.
    # Long legitimate compounds alone are insufficient to trigger OCR.
    dense = any(rare[i + 4] - start < 30 for i, start in enumerate(rare[:-4]))
    dense |= any(rare[i + 2] - start < 12 for i, start in enumerate(rare[:-2]))
    dense |= japanese and any(sum(rare_han(c) for c in m.group()) >= 2
                              for m in re.finditer(r"[一-鿿]{12,}", text))
    if "\ufffd" in text or dense:
        issues.append("suspect_characters")
    if re.search(r"([^\w\s])\1{8,}|(.{3,12})\2{3,}", text):
        issues.append("repetition")
    return issues


def vertical_columns(items: list[Any]) -> list[list[Any]] | None:
    """Detect vertical CJK from advances, including horizontal-font OCR glyphs.

    Language alone cannot distinguish Japanese horizontal and vertical writing.
    Preserve the horizontal path unless vertical advances dominate source order.
    """
    glyphs = [s for s in items if len(s.text.strip()) <= 3 and _CJK.search(s.text)]
    if len(glyphs) < 12:
        return None
    vertical = horizontal = 0
    for a, b in zip(glyphs, glyphs[1:], strict=False):
        size = max(a.font_size, b.font_size, 1)
        dx, dy = b.x0 - a.x0, b.top - a.top
        vertical += abs(dx) < size * .55 and size * .35 < dy < size * 2.5
        horizontal += abs(dy) < size * .55 and size * .35 < dx < size * 2.5
    if vertical < 8 or vertical <= horizontal * 2:
        return None
    # Punctuation and small kana have shifted x coordinates. Sorting individual
    # glyphs by x separates them from their words. Retain object order within a
    # column and sort only whole columns, detected by the vertical wrap.
    columns: list[list[Any]] = []
    for item in items:
        previous = columns[-1][-1] if columns else None
        wrap = previous is not None and (
            item.top < previous.top - max(item.font_size, previous.font_size) * 2
            and abs(item.x0 - previous.x0) > item.font_size * .5)
        if not columns or wrap:
            columns.append([item])
        else:
            columns[-1].append(item)
    return sorted(columns, key=lambda c: -c[0].x0)


def native_pdf_pages(path: Path, expected_sha256: str) -> list[dict[str, Any]]:
    """Use original text object order, with glyph geometry for vertical columns."""
    import pymupdf

    if not path.is_file() or path.stat().st_size > 512 * 1024 * 1024:
        return []
    with path.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != expected_sha256:
        raise ValueError("source_hash_mismatch")
    pages = []
    with pymupdf.open(path) as document:
        if document.needs_pass or len(document) > 3000:
            return []
        for number, page in enumerate(document, 1):
            # sort=True performs a horizontal row sort, which corrupts vertical
            # searchable scans. Keep the PDF text stream and join glyph newlines.
            text = page.get_text(sort=False)
            lines = text.splitlines()
            short = sum(len(s.strip()) <= 2 and bool(_CJK.search(s)) for s in lines)
            vertical = False
            if short >= 12:
                raw = page.get_text("rawdict", flags=pymupdf.TEXTFLAGS_RAWDICT
                                    & ~pymupdf.TEXT_PRESERVE_IMAGES)
                items = [SimpleNamespace(
                    text=char["c"], x0=char["bbox"][0], top=char["bbox"][1],
                    x1=char["bbox"][2], bottom=char["bbox"][3], font_size=span["size"],
                ) for block in raw["blocks"] if "lines" in block for line in block["lines"]
                         for span in line["spans"] for char in span["chars"]]
                columns = vertical_columns(items)
                if columns is not None:
                    vertical = True
                    # The source stream already contains punctuation, ruby and
                    # mixed Latin labels in order. Do not geometrically re-sort it.
                    text = "".join(s.strip() for s in lines)
            pages.append({"number": number, "text": text.strip(),
                          "extraction_method": "pdf_text_stream",
                          "writing_mode": "vertical-rl" if vertical else "source_order",
                          "width": page.rect.width, "height": page.rect.height})
    return pages


def page_passages(
    read: dict[str, Any], queries: list[str], *, limit: int, context_radius: int,
) -> dict[str, Any]:
    """Choose complete neighbouring pages instead of hundreds of tiny spans."""
    pages = read["pages"]
    terms = list(dict.fromkeys(t for q in queries for t in re.findall(
        r"[\wぁ-ゖァ-ヺ一-鿿]+", q.casefold()) if t))
    if not terms:
        return {}
    # Repeated short lines are running heads, not evidence of relevance.
    heads = Counter(line.strip() for p in pages for line in p["text"].splitlines()
                    if 0 < len(line.strip()) < 60)
    ranked = []
    for index, page in enumerate(pages):
        text = compact("\n".join(line for line in page["text"].splitlines()
                                 if heads[line.strip()] < max(3, len(pages) * .1)))
        sentences = len(re.findall(r"[。.!?！？]", text))
        navigation = (sentences < 2 and len(re.findall(r"\d+", text)) >= 5) or bool(re.search(
            r"^(?:目次|概念索引|人名索引|用語索引|index\b|contents\b)", text))
        navigation |= len(re.findall(r"[IⅠⅡⅢ12][-–][0-9]", text)) > 8
        matches = sum(t in text for t in terms)
        definitions = len(re.findall(
            r"とは[、,]?|という[のも]は|を意味|に対応|によって決ま|度合|基本的な", text))
        score = matches * 4 + sum(math.log1p(text.count(t)) for t in terms)
        score += min(definitions, 6) * 1.5
        score -= 30 if navigation else 0
        ranked.append((score, index, navigation, matches))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    seeds = [item for item in ranked if not item[2] and item[3]]
    if not seeds:
        return {}  # Index-only hits remain unresolved; they are never support.
    selected: dict[int, dict[str, Any]] = {}
    remaining = limit
    # A reading round takes a bounded continuous region. Later rounds can widen it.
    seed = next((i for _score, i, _nav, _matches in seeds
                 if pages[i]["number"] == read.get("preferred_seed_page")), seeds[0][1])
    radius = max(1, context_radius)
    neighbours = [seed]
    for distance in range(1, radius + 2):
        neighbours.extend([seed + distance, seed - distance])
    nonempty = [i for i in neighbours if 0 <= i < len(pages) and pages[i]["text"].strip()]
    for index in nonempty[:2 * radius + 1]:
        page = pages[index]
        text = page["text"]
        if len(text) > remaining:
            # Never clip before the seed. For oversized pages keep a continuous
            # sentence window around the match and report incomplete coverage.
            if index != seed:
                continue
            anchors = [m.start() for term in terms
                       for m in re.finditer(re.escape(term), text, flags=re.I)]
            starts = {0, max(0, len(text) - remaining), *(
                max(0, min(a - remaining // 3, len(text) - remaining)) for a in anchors)}

            def window_score(start: int, value: str = text, size: int = remaining) -> float:
                window = value[start:start + size].casefold()
                return (sum(t in window for t in terms) * 4
                        + min(6, len(re.findall(r"とは|を意味|に対応|条件|ただし", window))) * 2
                        - len(text_issues(window)) * 3)

            start = max(starts, key=lambda i: (window_score(i), -i))
            text = text[start:start + max(0, remaining)]
        if not text or len(text) > remaining:
            continue
        selected[index] = {**page, "text": text}
        remaining -= len(text) + 5
    if not selected:
        return {}
    ordered = [selected[i] for i in sorted(selected)]
    excerpts = [p["text"] for p in ordered]
    uncertain = []
    uncertain_ranges = []
    offset = 0
    for page in ordered:
        text = page["text"]
        fragments = [(0, len(text))] if page.get("requires_image_review") else [
            (m.start(), m.end()) for m in re.finditer(r"[^。！？!?]+[。！？!?]*", text)
            if text_issues(m.group(0))]
        for start, end in fragments:
            uncertain.append(text[start:end])
            uncertain_ranges.append([offset + start, offset + end])
        offset += len(text) + 5  # len("\n[…]\n")
    ids = list(dict.fromkeys(s for p in ordered for s in p.get("source_span_ids", [])))
    derived = any(p.get("extraction_method") != "canonical_db" for p in ordered)
    citation = {**read.get("bibliography", {}), "source_asset_id": read["source_asset_id"],
                "document_unit_id": read["document_unit_id"],
                "pdf_pages": [p["number"] for p in ordered]}
    if not derived and ids:
        citation.update(source_span_id=ids[0], source_span_ids=ids)
    return {
        "status": "retrieved", "source_level": "full_text", "source_origin": "local_rag",
        "content_sha256": read["content_sha256"],
        "excerpt": "\n[…]\n".join(excerpts),
        "locator": ", ".join(f"PDF p. {p['number']}" for p in ordered),
        "citation": citation, "selected_span_ids": ids, "pages": ordered,
        "reading_scope": "continuous_pages", "reader_policy": POLICY,
        "seed_page": pages[seed]["number"],
        "context_radius": radius, "pages_examined": len(pages),
        "corpus_scanned": True, "read_complete": True,
        "excerpt_complete": len(selected) == len(pages)
        and all(selected[i]["text"] == pages[i]["text"] for i in selected),
        "quality_status": "image_review_required" if uncertain else "no_damage_detected",
        "unverified_passages": uncertain,
        "uncertain_ranges": uncertain_ranges,
        "source_recovery": read.get("source_recovery", {}),
        "page_provenance": [{k: p.get(k) for k in (
            "number", "extraction_method", "writing_mode", "width", "height",
            "image_path", "ocr_artifact", "requires_image_review",
        )} for p in ordered],
        "extraction_sha256": hashlib.sha256("\n[…]\n".join(excerpts).encode()).hexdigest(),
    }
