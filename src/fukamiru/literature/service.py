"""Provider-backed literature discovery with durable human Candidate Review."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sqlite3
import ssl
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from xml.etree import ElementTree as ET

import truststore

from fukamiru.errors import FukamiruError
from fukamiru.literature.transport import get_json
from fukamiru.storage.runs import utc_now


@dataclass(frozen=True)
class LiteratureCandidate:
    provider: str
    provider_record_id: str
    title: str
    authors: tuple[str, ...]
    publication_title: str | None
    publisher: str | None
    publication_date: str | None
    record_type: str
    doi: str | None
    url: str | None
    oa_url: str | None
    oa_status: str | None
    license_url: str | None
    provider_payload: dict[str, Any]


class LiteratureProvider(Protocol):
    name: str

    def search(self, query: str, *, limit: int) -> list[LiteratureCandidate]: ...


class _JSONProvider:
    name = "provider"
    base_url = ""

    def __init__(
        self,
        *,
        user_agent: str = "Fukamiru/0.4",
        timeout_seconds: float = 30,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.timeout_seconds = timeout_seconds
        self.ssl_context = ssl_context or _system_ssl_context()
        self.request_outcomes: list[dict[str, Any]] = []

    def _get(self, parameters: dict[str, Any]) -> dict[str, Any]:
        return get_json(self, parameters)

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": self.user_agent}
        if self.name == "openalex" and os.environ.get("OPENALEX_API_KEY"):
            headers["Authorization"] = "Bearer " + os.environ["OPENALEX_API_KEY"]
        return headers


class OpenAlexProvider(_JSONProvider):
    name = "openalex"
    base_url = "https://api.openalex.org/works"
    minimum_interval_seconds = 1.0

    def search(self, query: str, *, limit: int) -> list[LiteratureCandidate]:
        payload = self._get({"search": query, "per-page": limit})
        return [self._candidate(item) for item in payload.get("results", []) if item.get("id")]

    def _candidate(self, item: dict[str, Any]) -> LiteratureCandidate:
        authors = tuple(
            str(authorship.get("author", {}).get("display_name"))
            for authorship in item.get("authorships", [])
            if authorship.get("author", {}).get("display_name")
        )
        location = item.get("best_oa_location") or {}
        primary = item.get("primary_location") or {}
        source = primary.get("source") or {}
        open_access = item.get("open_access") or {}
        return LiteratureCandidate(
            provider=self.name,
            provider_record_id=str(item["id"]),
            title=str(item.get("display_name") or item.get("title") or "Untitled"),
            authors=authors,
            publication_title=source.get("display_name"),
            publisher=source.get("host_organization_name"),
            publication_date=item.get("publication_date")
            or (str(item["publication_year"]) if item.get("publication_year") else None),
            record_type=str(item.get("type") or "work"),
            doi=_normalize_doi(item.get("doi")),
            url=item.get("id"),
            oa_url=location.get("pdf_url") or location.get("landing_page_url"),
            oa_status=open_access.get("oa_status"),
            license_url=location.get("license"),
            provider_payload=item,
        )


class CrossrefProvider(_JSONProvider):
    name = "crossref"
    base_url = "https://api.crossref.org/works"

    def search(self, query: str, *, limit: int) -> list[LiteratureCandidate]:
        params: dict[str, Any] = {"query.bibliographic": query, "rows": limit}
        if os.environ.get("FUKAMIRU_CONTACT_EMAIL"):
            params["mailto"] = os.environ["FUKAMIRU_CONTACT_EMAIL"]
        payload = self._get(params)
        message = payload.get("message") or {}
        return [self._candidate(item) for item in message.get("items", []) if item.get("DOI")]

    def _candidate(self, item: dict[str, Any]) -> LiteratureCandidate:
        authors = tuple(
            " ".join(
                value
                for value in (str(author.get("given") or ""), str(author.get("family") or ""))
                if value
            )
            for author in item.get("author", [])
        )
        date_parts = (item.get("published") or {}).get("date-parts") or []
        publication_date = "-".join(str(value) for value in date_parts[0]) if date_parts else None
        licenses = item.get("license") or []
        links = item.get("link") or []
        oa_url = next(
            (link.get("URL") for link in links if "pdf" in str(link.get("content-type", ""))),
            None,
        )
        return LiteratureCandidate(
            provider=self.name,
            provider_record_id=str(item["DOI"]),
            title=_plain_text((item.get("title") or ["Untitled"])[0]),
            authors=tuple(author for author in authors if author),
            publication_title=(item.get("container-title") or [None])[0],
            publisher=item.get("publisher"),
            publication_date=publication_date,
            record_type=str(item.get("type") or "work"),
            doi=_normalize_doi(item.get("DOI")),
            url=item.get("URL"),
            oa_url=oa_url,
            oa_status="unknown",
            license_url=licenses[0].get("URL") if licenses else None,
            provider_payload=item,
        )


class NDLSearchProvider:
    """Rate-limited Japanese bibliographic search over the official NDL API."""

    name = "ndl_search"
    base_url = "https://ndlsearch.ndl.go.jp/api/opensearch"

    def __init__(
        self,
        *,
        user_agent: str = "Fukamiru/0.4",
        timeout_seconds: float = 30,
        minimum_interval_seconds: float = 1.1,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.timeout_seconds = timeout_seconds
        self.minimum_interval_seconds = max(0.0, minimum_interval_seconds)
        self.ssl_context = ssl_context or _system_ssl_context()
        self.request_outcomes: list[dict[str, Any]] = []

    def _headers(self) -> dict[str, str]:
        return {"Accept": "application/rss+xml", "User-Agent": self.user_agent}

    @staticmethod
    def decode_response(data: bytes) -> dict[str, Any]:
        return {"xml": data.decode("utf-8-sig")}

    def search(self, query: str, *, limit: int) -> list[LiteratureCandidate]:
        return self._search_parameters(
            {
                "any": query,
                "cnt": max(1, min(int(limit), 100)),
            }
        )

    def search_bibliographic(
        self,
        title: str,
        author: str = "",
        *,
        limit: int = 8,
    ) -> list[LiteratureCandidate]:
        parameters: dict[str, Any] = {"title": title, "cnt": max(1, min(limit, 100))}
        if author and author != "自著":
            parameters["creator"] = author
        candidates = self._search_parameters(parameters)
        for candidate in candidates:
            candidate.provider_payload["fielded_query"] = parameters
        return candidates

    def _search_parameters(self, parameters: dict[str, Any]) -> list[LiteratureCandidate]:
        payload = get_json(self, parameters)
        try:
            root = ET.fromstring(payload["xml"])
        except ET.ParseError as exc:
            raise FukamiruError(f"{self.name} returned invalid XML") from exc
        return [self._candidate(item) for item in root.findall("./channel/item")]

    def _candidate(self, item: ET.Element) -> LiteratureCandidate:
        namespaces = {
            "dc": "http://purl.org/dc/elements/1.1/",
            "dcterms": "http://purl.org/dc/terms/",
            "dcndl": "http://ndl.go.jp/dcndl/terms/",
            "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
            "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
            "xsi": "http://www.w3.org/2001/XMLSchema-instance",
        }

        def text(path: str) -> str | None:
            value = item.findtext(path, namespaces=namespaces)
            return value.strip() if value and value.strip() else None

        creators = tuple(
            value.text.strip()
            for value in item.findall("dc:creator", namespaces)
            if value.text and value.text.strip()
        )
        contributors = tuple(
            value.text.strip()
            for value in item.findall("dc:contributor", namespaces)
            if value.text and value.text.strip()
        )
        identifiers = []
        for identifier in item.findall("dc:identifier", namespaces):
            value = (identifier.text or "").strip()
            if not value:
                continue
            raw_type = identifier.attrib.get(f"{{{namespaces['xsi']}}}type", "identifier")
            identifiers.append({"type": raw_type, "value": value})
        links = [
            node.attrib.get(f"{{{namespaces['rdf']}}}resource")
            for node in item.findall("rdfs:seeAlso", namespaces)
        ]
        links = [str(value) for value in links if value]
        doi_url = next((value for value in links if "doi.org/" in value), None)
        categories = [
            node.text.strip()
            for node in item.findall("category")
            if node.text and node.text.strip()
        ]
        record_type = next(
            (
                category
                for category in categories
                if category in {"図書", "記事", "博士論文", "雑誌"}
            ),
            categories[0] if categories else "work",
        )
        url = text("link") or text("guid")
        payload = {
            "creators_raw": list(creators),
            "contributors_raw": list(contributors),
            "responsibility_raw": list(dict.fromkeys((*creators, *contributors))),
            "description": text("dc:description") or text("description"),
            "volume": text("dcndl:volume"),
            "edition": text("dcndl:edition"),
            "original_title": text("dcterms:alternative"),
            "language": text("dc:language"),
            "translators": [
                re.sub(r"[,， ]*(?:訳|翻訳)$", "", v)
                for v in creators
                if re.search(r"(?:訳|翻訳)$", v)
            ],
            "publisher_urls": [
                v
                for v in links
                if not any(host in v for host in ("ndl.go.jp", "doi.org", "ci.nii.ac.jp"))
            ],
            "series_title": text("dcndl:seriesTitle"),
            "publication_place": text("dcndl:publicationPlace"),
            "identifiers": identifiers,
            "categories": categories,
        }
        return LiteratureCandidate(
            provider=self.name,
            provider_record_id=(
                next((item["value"] for item in identifiers), None)
                or url
                or hashlib.sha256(ET.tostring(item)).hexdigest()
            ),
            title=text("dc:title") or text("title") or "Untitled",
            authors=tuple(dict.fromkeys(v for v in creators if not re.search(r"(?:訳|翻訳)$", v))),
            publication_title=text("dcndl:seriesTitle"),
            publisher=text("dc:publisher"),
            publication_date=text("dcterms:issued") or text("dc:date"),
            record_type=record_type,
            doi=_normalize_doi(doi_url),
            url=url,
            oa_url=None,
            oa_status="unknown",
            license_url=None,
            provider_payload=payload,
        )


class OpenLibraryProvider(_JSONProvider):
    """Book-specific work search followed by explicit edition-record retrieval."""

    name = "open_library"
    base_url = "https://openlibrary.org/search.json"

    def search(self, query: str, *, limit: int) -> list[LiteratureCandidate]:
        payload = self._get({
            "title": query, "limit": min(max(limit, 1), 10),
            "fields": "key,title,author_name,editions,editions.key,editions.title",
        })
        candidates: list[LiteratureCandidate] = []
        for work in payload.get("docs") or []:
            if not isinstance(work, dict):
                continue
            editions = (work.get("editions") or {}).get("docs") or []
            for edition in editions[:2]:
                key = str(edition.get("key") or "")
                if not re.fullmatch(r"/books/OL\d+M", key):
                    continue
                detail_url = "https://openlibrary.org" + key + ".json"
                detail: dict[str, Any] = {}
                detail_status = "unavailable"
                try:
                    request = urllib.request.Request(detail_url, headers=self._headers())
                    with urllib.request.urlopen(
                        request, timeout=self.timeout_seconds, context=self.ssl_context,
                    ) as response:
                        data = response.read(1_000_001)
                    if len(data) > 1_000_000:
                        detail_status = "too_large"
                    else:
                        detail = json.loads(data)
                        detail_status = "retrieved"
                except (OSError, ValueError, json.JSONDecodeError):
                    pass
                languages = [
                    str(item.get("key") or "").rsplit("/", 1)[-1]
                    for item in detail.get("languages") or [] if isinstance(item, dict)
                ]
                isbn = next(iter(detail.get("isbn_13") or detail.get("isbn_10") or []), None)
                publishers = detail.get("publishers") or []
                record_url = "https://openlibrary.org" + key
                candidates.append(LiteratureCandidate(
                    provider=self.name,
                    provider_record_id=key.rsplit("/", 1)[-1],
                    title=str(detail.get("title") or edition.get("title")
                              or work.get("title") or "Untitled"),
                    authors=tuple(str(name) for name in work.get("author_name") or []),
                    publication_title=None,
                    publisher=str(publishers[0]) if publishers else None,
                    publication_date=str(detail.get("publish_date") or "") or None,
                    record_type="book", doi=None, url=record_url,
                    oa_url=None, oa_status="unknown", license_url=None,
                    provider_payload={
                        "isbn": isbn, "language": languages[0] if languages else None,
                        "detail_record_url": detail_url, "detail_record_status": detail_status,
                        "work_key": work.get("key"),
                        "source_type": "structured_bibliography",
                    },
                ))
        return candidates


class SemanticScholarProvider(_JSONProvider):
    name = "semantic_scholar"
    base_url = "https://api.semanticscholar.org/graph/v1/paper/search"
    minimum_interval_seconds = 1.0
    fields = (
        "paperId,title,authors,venue,publicationVenue,year,publicationDate,"
        "externalIds,url,openAccessPdf,publicationTypes,abstract"
    )

    def __init__(self, *, api_key: str | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.api_key = api_key or os.environ.get("SEMANTIC_SCHOLAR_API_KEY")

    def _headers(self) -> dict[str, str]:
        headers = super()._headers()
        if self.api_key:
            headers["x-api-key"] = self.api_key
        return headers

    def search(self, query: str, *, limit: int) -> list[LiteratureCandidate]:
        payload = self._get({"query": query, "limit": limit, "fields": self.fields})
        return [self._candidate(item) for item in payload.get("data", []) if item.get("paperId")]

    def _candidate(self, item: dict[str, Any]) -> LiteratureCandidate:
        publication_venue = item.get("publicationVenue") or {}
        open_access = item.get("openAccessPdf") or {}
        external_ids = item.get("externalIds") or {}
        publication_types = item.get("publicationTypes") or []
        return LiteratureCandidate(
            provider=self.name,
            provider_record_id=str(item["paperId"]),
            title=_plain_text(item.get("title") or "Untitled"),
            authors=tuple(
                str(author["name"]) for author in item.get("authors", []) if author.get("name")
            ),
            publication_title=item.get("venue") or publication_venue.get("name"),
            publisher=None,
            publication_date=item.get("publicationDate")
            or (str(item["year"]) if item.get("year") else None),
            record_type=str(publication_types[0] if publication_types else "paper"),
            doi=_normalize_doi(external_ids.get("DOI")),
            url=item.get("url"),
            oa_url=open_access.get("url"),
            oa_status=open_access.get("status"),
            license_url=open_access.get("license"),
            provider_payload=item,
        )


class LiteratureDiscoveryService:
    def __init__(
        self,
        connection: sqlite3.Connection,
        providers: dict[str, LiteratureProvider] | None = None,
    ) -> None:
        self.connection = connection
        self.providers = providers or {
            "openalex": OpenAlexProvider(),
            "crossref": CrossrefProvider(),
            "semantic_scholar": SemanticScholarProvider(),
        }

    def search(self, *, provider: str, query: str, limit: int = 20) -> dict[str, Any]:
        if provider not in self.providers:
            raise FukamiruError(f"Unsupported literature provider: {provider}")
        if not query.strip():
            raise FukamiruError("Literature query must not be empty")
        if not 1 <= limit <= 100:
            raise FukamiruError("Literature result limit must be between 1 and 100")
        search_id = str(uuid.uuid4())
        created = utc_now()
        self.connection.execute(
            """
            INSERT INTO literature_searches(
                id, provider, query, status, requested_limit, created_at
            ) VALUES (?, ?, ?, 'running', ?, ?)
            """,
            (search_id, provider, query.strip(), limit, created),
        )
        self.connection.commit()
        try:
            candidates = self.providers[provider].search(query.strip(), limit=limit)
            for candidate in candidates:
                self._save_candidate(search_id, candidate)
            self.connection.execute(
                """
                UPDATE literature_searches SET status = 'completed', completed_at = ?
                WHERE id = ?
                """,
                (utc_now(), search_id),
            )
            self.connection.commit()
            return self.get_search(search_id)
        except Exception as exc:
            self.connection.execute(
                """
                UPDATE literature_searches
                SET status = 'failed', error_text = ?, completed_at = ? WHERE id = ?
                """,
                (str(exc), utc_now(), search_id),
            )
            self.connection.commit()
            raise

    def list_candidates(self, *, status: str = "pending") -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT * FROM literature_candidates
            WHERE review_status = ? ORDER BY created_at, id
            """,
            (status,),
        ).fetchall()
        return [self._decode_candidate(row) for row in rows]

    def review(self, candidate_id: str, *, decision: str) -> dict[str, Any]:
        if decision not in {"approve", "reject"}:
            raise FukamiruError("Candidate decision must be approve or reject")
        candidate = self.connection.execute(
            "SELECT * FROM literature_candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
        if candidate is None:
            raise FukamiruError(f"Literature candidate not found: {candidate_id}")
        if candidate["review_status"] != "pending":
            raise FukamiruError("Literature candidate has already been reviewed")
        if decision == "reject":
            self.connection.execute(
                """
                UPDATE literature_candidates SET review_status = 'rejected', reviewed_at = ?
                WHERE id = ?
                """,
                (utc_now(), candidate_id),
            )
            self.connection.commit()
            return self._decode_candidate(
                self.connection.execute(
                    "SELECT * FROM literature_candidates WHERE id = ?", (candidate_id,)
                ).fetchone()
            )
        record_id = self._approve(candidate)
        self.connection.execute(
            """
            UPDATE literature_candidates
            SET review_status = 'approved', bibliographic_record_id = ?, reviewed_at = ?
            WHERE id = ?
            """,
            (record_id, utc_now(), candidate_id),
        )
        self.connection.commit()
        return self._decode_candidate(
            self.connection.execute(
                "SELECT * FROM literature_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
        )

    def get_search(self, search_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM literature_searches WHERE id = ?", (search_id,)
        ).fetchone()
        if row is None:
            raise FukamiruError(f"Literature search not found: {search_id}")
        result = dict(row)
        candidates = self.connection.execute(
            "SELECT * FROM literature_candidates WHERE search_id = ? ORDER BY created_at, id",
            (search_id,),
        ).fetchall()
        result["candidates"] = [self._decode_candidate(item) for item in candidates]
        return result

    def _save_candidate(self, search_id: str, candidate: LiteratureCandidate) -> None:
        self.connection.execute(
            """
            INSERT INTO literature_candidates(
                id, search_id, provider, provider_record_id, title, authors_json,
                publication_title, publisher, publication_date, record_type, doi,
                url, oa_url, oa_status, license_url, provider_payload_json,
                review_status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                str(uuid.uuid4()),
                search_id,
                candidate.provider,
                candidate.provider_record_id,
                candidate.title,
                json.dumps(candidate.authors, ensure_ascii=False),
                candidate.publication_title,
                candidate.publisher,
                candidate.publication_date,
                candidate.record_type,
                candidate.doi,
                candidate.url,
                candidate.oa_url,
                candidate.oa_status,
                candidate.license_url,
                json.dumps(candidate.provider_payload, ensure_ascii=False),
                utc_now(),
            ),
        )

    def _approve(self, candidate: sqlite3.Row) -> str:
        existing = None
        if candidate["doi"]:
            existing = self.connection.execute(
                "SELECT id FROM bibliographic_records WHERE doi = ?", (candidate["doi"],)
            ).fetchone()
        record_id = str(existing["id"]) if existing else str(uuid.uuid4())
        now = utc_now()
        if existing is None:
            self.connection.execute(
                """
                INSERT INTO bibliographic_records(
                    id, record_type, title, authors_json, publication_title,
                    publisher, publication_date, doi, url, csl_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    candidate["record_type"],
                    candidate["title"],
                    candidate["authors_json"],
                    candidate["publication_title"],
                    candidate["publisher"],
                    candidate["publication_date"],
                    candidate["doi"],
                    candidate["url"],
                    "{}",
                    now,
                    now,
                ),
            )
        self.connection.execute(
            """
            INSERT INTO bibliographic_provenance(
                bibliographic_record_id, provider, provider_record_id,
                source_url, license_url, retrieved_at, provider_payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(bibliographic_record_id, provider, provider_record_id) DO UPDATE SET
                source_url = excluded.source_url,
                license_url = excluded.license_url,
                retrieved_at = excluded.retrieved_at,
                provider_payload_json = excluded.provider_payload_json
            """,
            (
                record_id,
                candidate["provider"],
                candidate["provider_record_id"],
                candidate["url"],
                candidate["license_url"],
                now,
                candidate["provider_payload_json"],
            ),
        )
        return record_id

    @staticmethod
    def _decode_candidate(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["authors"] = json.loads(result.pop("authors_json"))
        result["provider_payload"] = json.loads(result.pop("provider_payload_json"))
        return result


class OAAcquisitionService:
    """Acquire only human-approved OA PDFs through staging and atomic publication."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        corpus_root: Path,
        timeout_seconds: float = 120,
        max_bytes: int = 200 * 1024 * 1024,
        opener: Any = urllib.request.urlopen,
    ) -> None:
        self.connection = connection
        self.corpus_root = corpus_root.resolve()
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self.opener = opener
        self.ssl_context = _system_ssl_context()

    def acquire(self, candidate_id: str) -> dict[str, Any]:
        candidate = self.connection.execute(
            "SELECT * FROM literature_candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
        if candidate is None:
            raise FukamiruError(f"Literature candidate not found: {candidate_id}")
        if candidate["review_status"] == "acquired":
            existing = self.connection.execute(
                """
                SELECT * FROM oa_acquisitions
                WHERE candidate_id = ? AND status = 'completed'
                """,
                (candidate_id,),
            ).fetchone()
            if existing is not None:
                return dict(existing)
        if candidate["review_status"] != "approved":
            raise FukamiruError("OA acquisition requires an approved Candidate")
        if not candidate["bibliographic_record_id"]:
            raise FukamiruError("Approved Candidate has no BibliographicRecord")
        source_url = str(candidate["oa_url"] or "")
        parsed = urllib.parse.urlparse(source_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise FukamiruError("OA source must be an HTTPS URL without credentials")
        acquisition_id = str(uuid.uuid4())
        staging_root = (self.corpus_root / "import_staging" / "oa").resolve()
        output_root = (self.corpus_root / "oa_downloads").resolve()
        for target in (staging_root, output_root):
            if self.corpus_root not in target.parents:
                raise FukamiruError("OA storage path escaped the configured Corpus root")
            target.mkdir(parents=True, exist_ok=True)
        staged = staging_root / f"{acquisition_id}.partial"
        self.connection.execute(
            """
            INSERT INTO oa_acquisitions(
                id, candidate_id, bibliographic_record_id, source_url,
                license_url, status, staged_path, created_at
            ) VALUES (?, ?, ?, ?, ?, 'downloading', ?, ?)
            """,
            (
                acquisition_id,
                candidate_id,
                candidate["bibliographic_record_id"],
                source_url,
                candidate["license_url"],
                str(staged),
                utc_now(),
            ),
        )
        self.connection.commit()
        try:
            digest, byte_size, content_type = self._download(source_url, staged)
            stored = output_root / f"{digest}.pdf"
            if not stored.exists():
                staged.rename(stored)
            self.connection.execute(
                """
                UPDATE oa_acquisitions
                SET status = 'completed', stored_path = ?, content_sha256 = ?,
                    content_type = ?, byte_size = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    str(stored),
                    digest,
                    content_type,
                    byte_size,
                    utc_now(),
                    acquisition_id,
                ),
            )
            self.connection.execute(
                """
                UPDATE literature_candidates SET review_status = 'acquired'
                WHERE id = ?
                """,
                (candidate_id,),
            )
            self.connection.commit()
        except Exception as exc:
            self.connection.execute(
                """
                UPDATE oa_acquisitions
                SET status = 'failed', error_text = ?, completed_at = ? WHERE id = ?
                """,
                (str(exc), utc_now(), acquisition_id),
            )
            self.connection.commit()
            if isinstance(exc, FukamiruError):
                raise
            raise FukamiruError(f"OA acquisition failed: {exc}") from exc
        return self.get(acquisition_id)

    def get(self, acquisition_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM oa_acquisitions WHERE id = ?", (acquisition_id,)
        ).fetchone()
        if row is None:
            raise FukamiruError(f"OA acquisition not found: {acquisition_id}")
        return dict(row)

    def _download(self, source_url: str, staged: Path) -> tuple[str, int, str]:
        request = urllib.request.Request(
            source_url,
            headers={"Accept": "application/pdf", "User-Agent": "Fukamiru/0.4"},
        )
        digest = hashlib.sha256()
        byte_size = 0
        prefix = b""
        with self.opener(
            request,
            timeout=self.timeout_seconds,
            context=self.ssl_context,
        ) as response:
            final_url = urllib.parse.urlparse(response.geturl())
            if final_url.scheme != "https" or not final_url.netloc:
                raise FukamiruError("OA download redirected outside HTTPS")
            content_type = str(response.headers.get_content_type())
            with staged.open("xb") as destination:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    byte_size += len(chunk)
                    if byte_size > self.max_bytes:
                        raise FukamiruError("OA PDF exceeds the configured size limit")
                    if len(prefix) < 5:
                        prefix += chunk[: 5 - len(prefix)]
                    digest.update(chunk)
                    destination.write(chunk)
        if prefix != b"%PDF-":
            raise FukamiruError("OA response is not a PDF; staged bytes were preserved")
        return digest.hexdigest(), byte_size, content_type


def _normalize_doi(value: Any) -> str | None:
    if not value:
        return None
    normalized = str(value).strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if normalized.lower().startswith(prefix):
            normalized = normalized[len(prefix) :]
            break
    return normalized.lower()


def _plain_text(value: Any) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", str(value))).strip()


def _system_ssl_context() -> ssl.SSLContext:
    if os.name == "nt":
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    return ssl.create_default_context()
