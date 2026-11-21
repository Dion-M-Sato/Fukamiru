"""Bounded, read-only discovery and contextual reading for search reports."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fukamiru.errors import FukamiruError
from fukamiru.knowledge.bibliography import source_bibliography

MAX_SOURCES = 5
MAX_SOURCE_PAGES = 3000
MAX_SOURCE_CHARACTERS = 2_000_000
MAX_SOURCE_CONTEXT = 10_000
_STOPWORDS = set(
    [
        "a",
        "au",
        "aux",
        "de",
        "des",
        "du",
        "d",
        "et",
        "en",
        "la",
        "le",
        "les",
        "l",
        "un",
        "une",
        "dans",
        "pour",
        "par",
        "sur",
        "ses",
        "sa",
        "the",
        "of",
        "and",
        "in",
        "on",
        "for",
        "to",
        "with",
        "from",
        "its",
        "is",
    ]
)


def normalized(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    folded = []
    for char in text:
        if unicodedata.name(char, "").startswith("LATIN"):
            char = "".join(
                c for c in unicodedata.normalize("NFKD", char) if not unicodedata.combining(c)
            )
        folded.extend(c for c in char if c.isalnum())
    return "".join(folded)


def quote_key(value: str) -> str:
    """Quotes may normalize Unicode presentation and whitespace, not letters."""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).casefold()


def author_key(value: str) -> str:
    try:
        authors = json.loads(value)
    except (TypeError, ValueError):
        return ""
    return normalized(" ".join(author for author in authors if isinstance(author, str)))


def query_terms(query: str) -> list[str]:
    return list(
        dict.fromkeys(
            normalized(t)
            for t in re.findall(r"[\w’'-]+", query)
            if len(normalized(t)) > 2 and normalized(t) not in _STOPWORDS
        )
    )


def looks_like_name(query: str) -> bool:
    return bool(re.fullmatch(r"[A-ZÀ-Þ][a-zà-ÿ]+(?:[-' ][A-ZÀ-Þ][a-zà-ÿ]+){0,3}", query))


def page_kind(page: dict[str, Any]) -> str:
    text = page["text"]
    key = normalized(text)
    if not text.strip():
        return "empty"
    if (
        "auteurdutexte" in key[:800]
        or "sourcegallicabnffr" in key
        or "isbn" in key and len(text) < 2000
    ):
        return "identity"
    if "reutilisationnoncommerciale" in key or "conditionsdutilisation" in key:
        return "rights"
    if (re.search(r"(?im)^\s*(?:A MES|A MON|TO MY)\b", text) or "献辞" in text) and len(
        text
    ) < 2200:
        return "dedication"
    if (
        "tabledesmatieres" in key
        or "目次" in text[:100]
        or "人名索引" in text[:100]
        or len(re.findall(r"\bp\.\s*\d", text)) >= 3
        or re.search(r"(?i)^\s*(?:index|contents)\b", text)
        or re.search(r"(?i)^\s*errata\b", text)
        or (
            len(re.findall(r"(?m)\d+\s*$", text)) >= 8
            and re.search(r"(?i)\bchapitre\b", text)
        )
    ):
        return "navigation"
    imprint = any(
        t in key for t in ("imprimerie", "presenteepar", "publishedby", "出版者", "発行所")
    )
    imprint |= bool(re.search(r"\b(?:MAISON|LIBRAIRIE|VERLAG|PUBLISHER)\b", text))
    if len(text) < 180 or len(text) < 1800 and imprint:
        return "identity"
    return "body"


class ReportCorpus:
    def __init__(
        self,
        connect: Callable[..., sqlite3.Connection],
        image_root: Path | None = None,
    ) -> None:
        self.connect = connect
        self.image_root = image_root

    def candidates(self, query: str, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Search metadata as well as passages; preserve discovery provenance."""
        grouped: dict[str, dict[str, Any]] = {}
        with self.connect() as conn:
            conn.create_function("report_fold", 1, normalized, deterministic=True)
            conn.create_function("report_author_fold", 1, author_key, deterministic=True)
            pattern = "%" + normalized(query) + "%"
            metadata = conn.execute(
                "SELECT DISTINCT a.id AS source_asset_id, a.sha256, a.media_type, l.path "
                "FROM source_assets a JOIN source_locations l ON l.source_asset_id=a.id "
                "AND l.is_primary=1 AND l.is_available=1 "
                "JOIN source_asset_bibliography link ON link.source_asset_id=a.id "
                "JOIN bibliographic_records b ON b.id=link.bibliographic_record_id "
                "WHERE a.ingestion_status='ready' AND ?<>'' AND "
                "(report_fold(b.title) LIKE ? OR report_author_fold(b.authors_json) LIKE ?) "
                "ORDER BY a.id LIMIT 100",
                (normalized(query), pattern, pattern),
            ).fetchall()
            for row in metadata:
                asset = str(row["source_asset_id"])
                grouped[asset] = {
                    **dict(row),
                    "metadata_match": True,
                    "hits": [],
                    "bibliography": source_bibliography(conn, asset) or {},
                }
            for rank, hit in enumerate(results, 1):
                asset = str(hit.get("source_asset_id") or "")
                if not asset:
                    continue
                if asset not in grouped:
                    row = conn.execute(
                        "SELECT a.id AS source_asset_id,a.sha256,a.media_type,l.path "
                        "FROM source_assets a JOIN source_locations l ON l.source_asset_id=a.id "
                        "AND l.is_primary=1 AND l.is_available=1 "
                        "WHERE a.id=? AND a.ingestion_status='ready'",
                        (asset,),
                    ).fetchone()
                    if row is None:
                        continue
                    grouped[asset] = {
                        **dict(row),
                        "metadata_match": False,
                        "hits": [],
                        "bibliography": source_bibliography(conn, asset) or {},
                    }
                if len(grouped[asset]["hits"]) < 4:
                    text = str(hit.get("text") or "")
                    match = re.search(re.escape(query), text, flags=re.I)
                    start = max(0, match.start() - 500) if match else 0
                    grouped[asset]["hits"].append(
                        {
                            "rank": rank,
                            "text": text[start : start + 1800],
                            "literal_match": normalized(query) in normalized(text),
                            "retrieval": hit.get("retrieval"),
                            "citation": hit.get("citation"),
                        }
                    )
        candidates = []
        for row in grouped.values():
            title = str(row["bibliography"].get("title") or Path(row["path"]).stem)
            matching_text = " ".join(
                [title, *row["bibliography"].get("authors", []), *(h["text"] for h in row["hits"])]
            )
            candidates.append(
                {
                    **row,
                    "candidate_id": f"C{len(candidates) + 1}",
                    "title": title,
                    "literal_match": normalized(query) in normalized(matching_text)
                    or any(hit["literal_match"] for hit in row["hits"]),
                }
            )
        return candidates

    def read(self, candidate: dict[str, Any]) -> dict[str, Any]:
        """Read page text without replacing OCR body with a native PDF cover only."""
        asset = candidate["source_asset_id"]
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT p.id AS page_id,p.document_unit_id,p.page_number AS number,"
                "p.printed_label,p.text,u.title AS unit_title "
                "FROM pages p JOIN document_units u ON u.id=p.document_unit_id "
                "WHERE u.source_asset_id=? ORDER BY u.sequence_index,p.page_number LIMIT ?",
                (asset, MAX_SOURCE_PAGES + 1),
            ).fetchall()
            if len(rows) > MAX_SOURCE_PAGES:
                raise FukamiruError("Source exceeds the bounded page-reading limit")
            pages = [dict(row) for row in rows]
            if not pages:
                units = conn.execute(
                    "SELECT id,title FROM document_units WHERE source_asset_id=? "
                    "ORDER BY sequence_index LIMIT 100",
                    (asset,),
                ).fetchall()
                for unit in units:
                    spans = conn.execute(
                        "SELECT text FROM source_spans WHERE document_unit_id=? ORDER BY ordinal",
                        (unit["id"],),
                    )
                    text = "\n".join(row["text"] for row in spans)
                    pages.append(
                        {
                            "page_id": str(unit["id"]),
                            "document_unit_id": unit["id"],
                            "number": None,
                            "printed_label": unit["title"],
                            "unit_title": unit["title"],
                            "text": text,
                        }
                    )
        if sum(len(page["text"]) for page in pages) > MAX_SOURCE_CHARACTERS:
            raise FukamiruError("Source exceeds the bounded text-reading limit")
        for page in pages:
            page["kind"] = page_kind(page)
            page["text_sha256"] = hashlib.sha256(page["text"].encode()).hexdigest()
        return {**candidate, "pages": pages}

    def bibliography_pages(self, source: dict[str, Any]) -> list[dict[str, Any]]:
        pages = source["pages"]
        title_terms = query_terms(source["title"])
        pool = list(
            dict.fromkeys([*range(min(24, len(pages))), *range(max(0, len(pages) - 4), len(pages))])
        )
        scored = []
        for index in pool:
            page = pages[index]
            key = normalized(page["text"])
            if not key or page["kind"] in {"dedication", "navigation"}:
                continue
            overlap = sum(term in key for term in title_terms)
            publication = sum(
                term in key
                for term in (
                    "imprimerie",
                    "publisher",
                    "maison",
                    "librairie",
                    "発行",
                    "出版",
                    "isbn",
                )
            )
            # A wrapper may combine a catalogue citation with a rights notice.
            identity = int("auteurdutexte" in key[:800])
            if not (overlap >= 2 or publication and page["kind"] == "identity" or identity):
                continue
            score = overlap + publication * 4 + identity * 8 - index / 100
            scored.append((score, index))
        indexes = sorted(index for _, index in sorted(scored, reverse=True)[:3])
        return [pages[index] for index in indexes]

    def images(self, source: dict[str, Any], pages: list[dict[str, Any]]) -> dict[str, Path]:
        path = Path(source["path"])
        if (
            self.image_root is None
            or path.suffix.casefold() != ".pdf"
            or not path.is_file()
            or path.stat().st_size > 512 * 1024 * 1024
        ):
            return {}
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != source["sha256"]:
                raise FukamiruError("Source file changed since corpus registration")
        import pymupdf

        self.image_root.mkdir(parents=True, exist_ok=True)
        result = {}
        with pymupdf.open(path) as document:
            for page in pages:
                number = page["number"]
                if number is None or not 1 <= number <= len(document):
                    continue
                key = hashlib.sha256((source["sha256"] + page["page_id"]).encode()).hexdigest()[:24]
                target = self.image_root / f"{key}.png"
                pdf_page = document[number - 1]
                scale = min(1.2, 2400 / max(pdf_page.rect.width, pdf_page.rect.height))
                pdf_page.get_pixmap(matrix=pymupdf.Matrix(scale, scale)).save(target)
                result[page["page_id"]] = target
        return result

    def body(self, source: dict[str, Any], queries: list[str]) -> list[dict[str, Any]]:
        pages = source["pages"]
        body = [i for i, page in enumerate(pages) if page["kind"] == "body"]
        if not body:
            return []
        terms = list(dict.fromkeys(t for query in queries for t in query_terms(query)))

        def score(index: int) -> float:
            key = normalized(pages[index]["text"])
            return sum(min(key.count(term), 4) for term in terms) + 3 * any(
                t in key[:500] for t in ("introduction", "definition", "conclusion")
            )

        seeds = [body[0], *sorted(body, key=lambda i: (-score(i), i))]
        used: set[int] = set()
        result = []
        remaining = MAX_SOURCE_CONTEXT
        for seed in seeds:
            if seed in used:
                continue
            unit = pages[seed]["document_unit_id"]
            region = [
                i
                for i in [seed, seed - 1, seed + 1]
                if 0 <= i < len(pages)
                and i in body
                and i not in used
                and pages[i]["document_unit_id"] == unit
            ]
            selected = []
            window_remaining = min(remaining, MAX_SOURCE_CONTEXT // 2)
            for index in region:
                available = min(remaining, window_remaining)
                if len(pages[index]["text"]) <= available:
                    selected.append(pages[index])
                    remaining -= len(pages[index]["text"])
                    window_remaining -= len(pages[index]["text"])
                    used.add(index)
                elif available >= 1000 and index == seed:
                    text = pages[index]["text"]
                    anchors = [
                        match.start()
                        for query in queries
                        for match in re.finditer(re.escape(query), text, flags=re.I)
                    ]
                    start = max(0, (min(anchors) if anchors else 0) - available // 3)
                    start = min(start, max(0, len(text) - available))
                    selected.append(
                        {
                            **pages[index],
                            "text": text[start : start + available],
                            "start_char": start,
                            "end_char": start + available,
                            "context_truncated": True,
                        }
                    )
                    used.add(index)
                    remaining -= available
                    window_remaining = 0
            if selected:
                order = {pages[index]["page_id"]: index for index in region}
                selected.sort(key=lambda page: order[page["page_id"]])
                result.append(
                    {
                        "kind": "body",
                        "source_asset_id": source["source_asset_id"],
                        "title": source["title"],
                        "path": source["path"],
                        "content_sha256": source["sha256"],
                        "pages": selected,
                        "text": "\n\n".join(p["text"] for p in selected),
                        "entity": source.get("entity", ""),
                        "reading_scope": "continuous_pages",
                        "context_truncated": any(p.get("context_truncated") for p in selected),
                    }
                )
            if len(result) >= 3 or remaining < 500:
                break
        return result
