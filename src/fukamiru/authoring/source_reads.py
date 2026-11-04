"""Run-scoped source reads; evidence judgments remain claim-specific."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import unicodedata
import urllib.parse
from collections.abc import Callable
from dataclasses import asdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from fukamiru.knowledge.bibliography import source_bibliography
from fukamiru.knowledge.page_reading import native_pdf_pages, page_passages
from fukamiru.literature.content import _query_terms
from fukamiru.literature.service import LiteratureCandidate
from fukamiru.storage.payloads import UnitCheckpoints


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class SourceReadCache:
    """Immutable read checkpoints in state.db, shared by stages of one run."""

    def __init__(self, database: Any, run_id: str, lock: threading.RLock):
        self.database = database
        self.run_id = run_id
        self.lock = lock
        self.flights: dict[str, threading.RLock] = {}

    def read(
        self,
        kind: str,
        identity: Any,
        load: Callable[[], Any],
        *,
        cacheable: Callable[[Any], bool] = lambda value: True,
    ) -> Any:
        signature = digest(identity)
        # The same PDF or embedding request can occur in parallel obligations.
        # Check-and-load is single flight; unrelated model audits run elsewhere.
        with self.lock:
            flight = self.flights.setdefault(kind + signature, threading.RLock())
        with flight:
            with self.database.connect() as connection:
                rows = UnitCheckpoints(connection).load(
                    self.run_id, "source-read-v1:" + kind, signature
                )
            if "result" in rows:
                return rows["result"]
            result = load()
            if cacheable(result):
                with self.database.connect() as connection:
                    UnitCheckpoints(connection).save(
                        self.run_id, "source-read-v1:" + kind, signature, {"result": result}
                    )
            return result


class CachedLiteratureProvider:
    """Successful provider queries survive a worker restart; failures stay retryable."""

    def __init__(self, provider: Any, cache: SourceReadCache):
        self.provider = provider
        self.cache = cache
        self.name = provider.name
        self.request_outcomes: list[dict[str, Any]] = []

    def search(self, query: str, *, limit: int) -> list[LiteratureCandidate]:
        loaded = False

        def load() -> list[dict[str, Any]]:
            nonlocal loaded
            loaded = True
            start = len(getattr(self.provider, "request_outcomes", []))
            try:
                return [asdict(candidate) for candidate in self.provider.search(query, limit=limit)]
            finally:
                self.request_outcomes.extend(getattr(self.provider, "request_outcomes", [])[start:])

        rows = self.cache.read(
            "literature-query",
            {
                "provider": self.name,
                "query": query,
                "limit": limit,
                "endpoint": getattr(self.provider, "base_url", None),
                "fields": getattr(self.provider, "fields", None),
                "contract": "academic-api-v32-1",
            },
            load,
        )
        if not loaded:
            self.request_outcomes.append(
                {"provider": self.name, "outcome": "cache_hit", "query": query}
            )
        return [LiteratureCandidate(**{**row, "authors": tuple(row["authors"])}) for row in rows]


def knowledge_revision(connection: sqlite3.Connection) -> str:
    """Cheap corpus and index revision, without scanning source text or vectors."""
    metadata = {}
    for table in ("source_assets", "document_units", "bibliographic_records"):
        metadata[table] = tuple(
            connection.execute(
                f"SELECT count(*), max(updated_at) FROM {table}"  # noqa: S608 - fixed table names
            ).fetchone()
        )
    metadata["locations"] = tuple(
        connection.execute(
            "SELECT count(*), max(last_verified_at), sum(is_available) FROM source_locations"
        ).fetchone()
    )
    metadata["index"] = [
        tuple(row)
        for row in connection.execute(
            "SELECT id, activated_at FROM vector_index_versions WHERE status='active' ORDER BY id"
        )
    ]
    metadata["last_chunk"] = connection.execute("SELECT max(rowid) FROM chunks").fetchone()[0]
    return digest(metadata)


def page_locator(citation: dict[str, Any]) -> str:
    if citation.get("locator"):
        return str(citation["locator"])
    start = citation.get("start_label") or citation.get("start_page")
    end = citation.get("end_label") or citation.get("end_page") or start
    if start is None:
        return ""
    return f"p. {start}" if str(start) == str(end) else f"pp. {start}–{end}"


def work_identifiers(record: dict[str, Any]) -> dict[str, str]:
    provenance = record.get("provenance") or {}
    citation = provenance.get("citation") or record.get("bibliography") or {}
    result = {}
    doi = str(provenance.get("doi") or citation.get("doi") or "").strip()
    if doi:
        doi = urllib.parse.unquote(
            re.sub(r"^(?:https?://(?:dx\.)?doi.org/|doi:\s*)", "", doi, flags=re.I)
        ).casefold()
        if re.fullmatch(r"10\.\d{4,9}/\S+", doi):
            result["doi"] = doi
    isbn = re.sub(r"[-\s]", "", str(provenance.get("isbn") or citation.get("isbn") or ""))
    if re.fullmatch(r"(?:\d{9}[\dXx]|\d{13})", isbn):
        result["isbn"] = isbn.upper()
    url = str(record.get("source_url") or citation.get("url") or "")
    if url.startswith(("https://", "http://")):
        parsed = urllib.parse.urlsplit(url)
        query = urllib.parse.urlencode([(k, v) for k, v in urllib.parse.parse_qsl(parsed.query)
                                       if not k.startswith("utm_")])
        result["url"] = urllib.parse.urlunsplit(
            (parsed.scheme.casefold(), parsed.netloc.casefold(), parsed.path, query, ""))
    return result


def reuse_local_passages(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Link exact work identities while retaining distinct local passages."""
    local: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in records:
        if record.get("source_type") != "local_rag" or not record.get("excerpt"):
            continue
        for key in _matching_identifiers(record).items():
            local.setdefault(key, []).append(record)
    result = []
    for record in records:
        matches = [item for key in _matching_identifiers(record).items()
                   for item in local.get(key, [])]
        identity = work_identifiers(record)
        matches = [item for item in matches if not any(
            identity.get(kind) and work_identifiers(item).get(kind)
            and identity[kind] != work_identifiers(item)[kind] for kind in ("doi", "isbn"))]
        if record.get("source_type") == "local_rag" or not matches:
            result.append(record)
            continue
        alias = {key: record.get(key) for key in ("provider", "provider_record_id", "source_url")}
        for match in matches:
            provenance = match.setdefault("provenance", {})
            aliases = provenance.setdefault("provider_aliases", [])
            if alias not in aliases:
                aliases.append(alias)
    return result


def _matching_identifiers(record: dict[str, Any]) -> dict[str, str]:
    identifiers = work_identifiers(record)
    citation = (record.get("provenance") or {}).get("citation") or record.get("bibliography") or {}
    title = str(citation.get("title") or record.get("title") or "").strip().casefold()
    authors = citation.get("authors") or record.get("authors") or []
    date = citation.get("publication_date") or citation.get("year")
    edition = citation.get("edition") or ""
    if title and authors and date:
        identifiers["bibliography"] = digest([title, sorted(authors), str(date), str(edition)])
    return identifiers


def quoted_observations(
    verdict: dict[str, Any], sources: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Keep exact related/partial passages without promoting an insufficient verdict."""
    by_key = {source["candidate_key"]: source for source in sources}
    observations = []
    for citation in verdict.get("citations") or []:
        source = by_key.get(citation.get("candidate_key"))
        quote = str(citation.get("quote") or "")
        if (
            source
            and quote.strip()
            and "[…]" not in quote
            and quote in str(source.get("excerpt") or "")
            and source.get("provenance", {}).get("source_level") != "bibliographic_metadata"
        ):
            observations.append(
                {
                    "candidate_key": source["candidate_key"],
                    "quote": quote,
                    "title": source.get("title"),
                    "locator": source.get("locator"),
                    "status": verdict.get("status"),
                    "support_gap": verdict.get("rationale"),
                }
            )
    return observations


def known_work_read(connection: sqlite3.Connection, identifiers: dict[str, str]) -> dict[str, Any]:
    """Read canonical text of an exactly identified local work, without PDF/OCR work."""
    if not identifiers:
        return {}
    rows = connection.execute(
        "SELECT d.id, d.source_asset_id, d.language, a.sha256, a.media_type FROM document_units d "
        "JOIN source_assets a ON a.id=d.source_asset_id "
        "LEFT JOIN source_asset_bibliography l ON l.source_asset_id=a.id "
        "LEFT JOIN bibliographic_records b ON b.id=l.bibliographic_record_id "
        "WHERE a.ingestion_status='ready' AND ((?<>'' AND lower(b.doi)=?) OR "
        "(?<>'' AND replace(replace(upper(b.isbn),'-',''),' ','')=?) OR a.id=? OR "
        "(?<>'' AND b.url=?)) "
        "AND EXISTS (SELECT 1 FROM source_locations s WHERE s.source_asset_id=a.id "
        "AND s.is_available=1) ORDER BY d.id",
        (
            identifiers.get("doi", ""),
            identifiers.get("doi", ""),
            identifiers.get("isbn", ""),
            identifiers.get("isbn", ""),
            identifiers.get("source_asset_id", ""),
            identifiers.get("url", ""),
            identifiers.get("url", ""),
        ),
    ).fetchall()
    if not rows:
        return {}
    # Keep the selected manifestation and its own page numbering; do not mix editions.
    for row in rows:
        spans = connection.execute(
            "SELECT s.id, s.text, s.start_char, s.end_char, "
            "p.page_number AS start_page, e.page_number AS end_page, "
            "p.printed_label AS start_label, e.printed_label AS end_label "
            "FROM source_spans s LEFT JOIN pages p ON p.id=s.start_page_id "
            "LEFT JOIN pages e ON e.id=s.end_page_id "
            "WHERE s.document_unit_id=? ORDER BY s.ordinal",
            (row["id"],),
        ).fetchall()
        if spans:
            result = {
                "document_unit_id": row["id"],
                "source_asset_id": row["source_asset_id"],
                "content_sha256": row["sha256"],
                "language": row["language"],
                "spans": [dict(s) for s in spans],
                "bibliography": source_bibliography(connection, row["source_asset_id"]) or {},
            }
            pages = [dict(p) for p in connection.execute(
                "SELECT id, page_number AS number, printed_label, text FROM pages "
                "WHERE document_unit_id=? ORDER BY page_number", (row["id"],),
            )]
            ids_by_page: dict[int, list[str]] = {}
            for span in spans:
                if span["start_page"] is not None:
                    ids_by_page.setdefault(span["start_page"], []).append(span["id"])
            for page in pages:
                page.update(extraction_method="canonical_db",
                            source_span_ids=ids_by_page.get(page["number"], []))
            locations = connection.execute(
                "SELECT path FROM source_locations WHERE source_asset_id=? AND is_available=1 "
                "ORDER BY is_primary DESC, id", (row["source_asset_id"],),
            ).fetchall()
            corrected = connection.execute(
                "SELECT 1 FROM ocr_text_revisions WHERE source_asset_id=? "
                "AND layer='human_corrected_ocr' LIMIT 1", (row["source_asset_id"],),
            ).fetchone()
            if corrected:
                locations = []
                result["source_recovery"] = {"status": "human_corrected_preserved"}
            for location in locations:
                path = Path(location["path"])
                if path.suffix.casefold() != ".pdf" or not path.is_file():
                    continue
                try:
                    native = native_pdf_pages(path, row["sha256"])
                except (OSError, ValueError, RuntimeError) as exc:
                    result["source_recovery"] = {"status": "unavailable", "reason": str(exc)}
                    continue
                if native and any(p["text"] for p in native):
                    for page in native:
                        # IDs identify discovery provenance only. The text was
                        # re-extracted and must never inherit old character offsets.
                        page["source_span_ids"] = ids_by_page.get(page["number"], [])
                    pages = native
                    result["source_path"] = str(path)
                    result["source_recovery"] = {"status": "original_text_layer",
                                                 "hash_verified": True}
                    break
            if pages and row["media_type"] == "application/pdf":
                result["pages"] = pages
            return result
    return {}


def known_work_passages(
    read: dict[str, Any], queries: list[str], *, limit: int = 6000,
    context_radius: int = 1,
) -> dict[str, Any]:
    if not read.get("spans"):
        return {}
    if read.get("pages"):
        return page_passages(read, queries, limit=limit, context_radius=context_radius)
    terms = _query_terms(queries)
    ranked = sorted(
        enumerate(read["spans"]),
        key=lambda pair: (
            -sum(20 for query in queries if query.strip()
                 and query.casefold() in pair[1]["text"].casefold()),
            -sum(term in pair[1]["text"].casefold() for term in terms),
            -min(len(pair[1]["text"]), 500), pair[0],
        ),
    )
    selected = {}
    remaining = limit
    order = []
    for index, _span in ranked:
        order.extend([index, *range(max(0, index - context_radius), index),
                      *range(index + 1, min(len(read["spans"]), index + context_radius + 1))])
    for index in dict.fromkeys(order):
        if remaining <= 0:
            break
        span = read["spans"][index]
        if not span["text"].strip():
            continue
        text = span["text"]
        # A contiguous window keeps definitions together with nearby limitations.
        start = 0
        if len(text) > remaining:
            anchors = [(
                sum(20 for query in queries if query.strip()
                    and query.casefold() in text[i:i + 750].casefold())
                + sum(term in text[i:i + 750].casefold() for term in terms), i,
            ) for i in range(0, len(text), 750)]
            _, anchor = max(anchors, key=lambda item: (item[0], -item[1]))
            start = max(0, min(anchor - remaining // 3, len(text) - remaining))
        excerpt = text[start:start + remaining]
        selected[index] = {**span, "text": excerpt}
        remaining -= len(excerpt) + 5  # Account for the discontinuity marker too.
    spans = [selected[index] for index in sorted(selected)]
    if not spans:
        return {}
    return {
        "status": "retrieved",
        "source_level": "full_text",
        "source_origin": "local_rag",
        "content_sha256": read["content_sha256"],
        "excerpt": "\n[…]\n".join(span["text"] for span in spans),
        "locator": ", ".join(dict.fromkeys(page_locator(s) for s in spans)),
        "citation": {
            **read["bibliography"],
            "source_asset_id": read["source_asset_id"],
            "document_unit_id": read["document_unit_id"],
            "source_span_id": spans[0]["id"],
            "source_span_ids": [span["id"] for span in spans],
        },
        "spans_examined": len(read["spans"]),
        "selected_span_ids": [s["id"] for s in spans],
        "context_radius": context_radius,
        "reading_scope": "selected_passages",
        "corpus_scanned": True,
        # Extraction/search completeness is distinct from the passages sent to a model.
        "read_complete": True,
        "excerpt_complete": len(spans) == len(read["spans"])
        and all(selected[index]["text"] == span["text"]
                for index, span in enumerate(read["spans"])),
    }


def named_work_hits(
    connection: sqlite3.Connection, titles: list[str], queries: list[str], *, limit: int = 3,
) -> list[dict[str, Any]]:
    """Locate title candidates, then read concepts inside each canonical manifestation."""
    if not titles:
        return []

    def normalized(value: str) -> str:
        return "".join(c for c in unicodedata.normalize("NFKD", value).casefold()
                       if c.isalnum() and not unicodedata.combining(c))

    def catalogue_titles(value: str) -> set[str]:
        # Corpus filenames often append a volume, series, author, page count or ISBN.
        # Keep whole-title comparison and explicitly bounded title components;
        # arbitrary occurrences in a commentary title are not work identities.
        parts = [value, *value.split("_")]
        parts.extend(re.split(r"[〈《（(\[【]", part, maxsplit=1)[0].strip()
                     for part in list(parts))
        return {normalized(part) for part in parts
                if len(normalized(part)) >= 2 and not normalized(part).isdecimal()}

    targets = [normalized(title) for title in titles]
    rows = connection.execute(
        "SELECT DISTINCT d.id, d.source_asset_id, d.title, b.title AS bibliography_title "
        "FROM document_units d LEFT JOIN source_asset_bibliography l ON l.source_asset_id="
        "d.source_asset_id LEFT JOIN bibliographic_records b ON b.id=l.bibliographic_record_id"
    ).fetchall()
    ranked = []
    for row in rows:
        candidates = set().union(*(catalogue_titles(title or "")
                                   for title in (row["title"], row["bibliography_title"])))
        score = max((SequenceMatcher(None, target, candidate).ratio()
                     for target in targets for candidate in candidates), default=0.0)
        if score >= 0.88:
            mapped_pages = connection.execute(
                "SELECT count(DISTINCT start_page_id) FROM source_spans WHERE document_unit_id=?",
                (row["id"],),
            ).fetchone()[0]
            ranked.append((score, mapped_pages, row))
    result = []
    assets = set()
    for score, _pages, row in sorted(ranked, key=lambda item: (-item[0], -item[1], item[2]["id"])):
        if row["source_asset_id"] in assets:
            continue
        assets.add(row["source_asset_id"])
        read = known_work_read(connection, {"source_asset_id": row["source_asset_id"]})
        content = known_work_passages(read, queries)
        if not content:
            continue
        # Use an actual canonical chunk identity; never create a fake chunk or DB row.
        if not content["selected_span_ids"]:
            continue
        chunk = connection.execute(
            "SELECT id FROM chunks WHERE primary_source_span_id=? ORDER BY id LIMIT 1",
            (content["selected_span_ids"][0],),
        ).fetchone()
        if chunk is None:
            continue
        result.append({
            "chunk_id": chunk["id"], "source_asset_id": read["source_asset_id"],
            "document_unit_id": read["document_unit_id"], "title": row["title"],
            "text": content["excerpt"], "retrieval": "named_work",
            "bibliography": read["bibliography"],
            "citation": {**content["citation"], "locator": content["locator"]},
            "content_acquisition": {key: value for key, value in content.items()
                                    if key not in {"excerpt", "citation", "pages"}},
            "title_match": {"status": "candidate_unverified", "score": score,
                            "requested_titles": titles},
        })
        if len(result) >= limit:
            break
    return result
