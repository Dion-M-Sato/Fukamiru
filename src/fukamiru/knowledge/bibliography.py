"""Source-backed bibliography created while a document enters the knowledge base."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
import zipfile
from html.parser import HTMLParser
from pathlib import Path

from fukamiru.knowledge.epub import EPUBExtractor


def _clean(value: object) -> str | None:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text or None


def _identifier(value: str | None, kind: str) -> str | None:
    if not value:
        return None
    if kind == "doi":
        match = re.search(r"10\.\d{4,9}/[^\s<>\"']+", value, re.I)
        return match.group(0).rstrip(".,;)]}") if match else None
    match = re.search(r"(?:97[89][- ]?)?(?:\d[- ]?){9}[\dXx]", value)
    return re.sub(r"[- ]", "", match.group(0)).upper() if match else None


class _HTMLMetadata(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: dict[str, list[str]] = {}
        self.in_title = False
        self.title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self.in_title = True
        if tag != "meta":
            return
        attributes = {key.casefold(): value for key, value in attrs if value}
        key = (attributes.get("name") or attributes.get("property") or "").casefold()
        value = _clean(attributes.get("content"))
        if key and value:
            self.values.setdefault(key, []).append(value)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)


def _first(values: dict[str, list[str]], *keys: str) -> str | None:
    return next((items[0] for key in keys if (items := values.get(key))), None)


def extract_source_bibliography(path: Path, *, fallback_title: str) -> dict[str, object]:
    """Read explicit file metadata; leave absent bibliographic fields unknown."""

    suffix = path.suffix.casefold()
    values: dict[str, list[str]] = {}
    source = "filename"
    if suffix == ".epub":
        with zipfile.ZipFile(path) as archive:
            extractor = EPUBExtractor()
            extractor._validate_archive(archive)
            opf_path = extractor._opf_path(archive)
            root = extractor._parse_xml(archive.read(opf_path), opf_path)
        for node in root.iter():
            name = node.tag.rsplit("}", 1)[-1].casefold()
            value = _clean(node.text)
            if value and name in {
                "title", "creator", "publisher", "date", "identifier", "language"
            }:
                values.setdefault(name, []).append(value)
        source = "epub_opf"
    elif suffix == ".pdf":
        from pypdf import PdfReader

        try:
            metadata = PdfReader(path).metadata or {}
        except Exception:
            # Metadata is optional. An AES dependency or malformed document-info
            # dictionary must not invalidate OCR text already extracted from it.
            metadata = {}
        for key, name in (
            ("/Title", "title"), ("/Author", "author"),
            ("/Subject", "subject"), ("/DOI", "doi"),
            ("/ISBN", "isbn"),
        ):
            value = _clean(metadata.get(key))
            if value:
                values.setdefault(name, []).append(value)
        if metadata:
            source = "pdf_document_info"
    elif suffix in {".html", ".htm"}:
        parser = _HTMLMetadata()
        parser.feed(path.read_bytes()[:1_000_000].decode("utf-8-sig", errors="replace"))
        values = parser.values
        title = _clean("".join(parser.title_parts))
        if title:
            values.setdefault("html_title", []).append(title)
        source = "html_meta"
    elif suffix in {".md", ".markdown"}:
        prefix = (
            path.read_bytes()[:32_768]
            .decode("utf-8-sig", errors="replace")
            .replace("\r\n", "\n")
        )
        if prefix.startswith("---\n"):
            end = prefix.find("\n---", 4)
            if end > 0:
                for line in prefix[4:end].splitlines():
                    key, separator, value = line.partition(":")
                    key = key.strip().casefold()
                    if separator and key in {
                        "title", "author", "date", "publisher", "doi", "isbn", "url"
                    }:
                        cleaned = _clean(value.strip().strip('"\''))
                        if cleaned:
                            values.setdefault(key, []).append(cleaned)
                source = "markdown_front_matter"

    title = _first(values, "citation_title", "dc.title", "title", "og:title", "html_title")
    authors = [
        author for key in ("citation_author", "dc.creator", "creator", "author")
        for author in values.get(key, []) if author
    ]
    date = _first(values, "citation_publication_date", "citation_date", "dc.date", "date")
    identifiers = " ".join(values.get("identifier", []))
    doi = _identifier(_first(values, "citation_doi", "doi") or identifiers, "doi")
    isbn = _identifier(_first(values, "citation_isbn", "isbn") or identifiers, "isbn")
    publication_title = _first(values, "citation_journal_title", "citation_conference_title")
    record_type = "article" if publication_title else "book" if suffix == ".epub" else "document"
    publisher = _first(values, "citation_publisher", "dc.publisher", "publisher")
    complete = bool(
        title and authors and date
        and (record_type != "book" or publisher)
        and (record_type != "article" or publication_title)
    )
    return {
        "record_type": record_type,
        "title": title or fallback_title,
        "authors": list(dict.fromkeys(authors)),
        "publication_title": publication_title,
        "publisher": publisher,
        "publication_date": date,
        "doi": doi,
        "isbn": isbn,
        "url": _first(values, "citation_public_url", "citation_pdf_url", "og:url", "url"),
        "metadata_source": source if title or authors or date or doi or isbn else "filename",
        "metadata_status": "complete" if complete else "partial",
    }


def source_bibliography(
    connection: sqlite3.Connection, source_asset_id: str
) -> dict[str, object] | None:
    row = connection.execute(
        """
        SELECT b.*, link.metadata_source, link.metadata_status, link.source_fields_json
        FROM source_asset_bibliography link
        JOIN bibliographic_records b ON b.id = link.bibliographic_record_id
        WHERE link.source_asset_id = ?
        """,
        (source_asset_id,),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["authors"] = json.loads(result.pop("authors_json"))
    result["source_fields"] = json.loads(result.pop("source_fields_json"))
    result.pop("csl_json")
    return result


def ensure_source_bibliography(
    connection: sqlite3.Connection,
    *,
    source_asset_id: str,
    document_unit_id: str,
    path: Path,
    fallback_title: str,
    now: str,
) -> dict[str, object]:
    """Create once per immutable asset and bind each rebuilt DocumentUnit."""

    existing = source_bibliography(connection, source_asset_id)
    if existing is None:
        fields = extract_source_bibliography(path, fallback_title=fallback_title)
        doi = fields["doi"]
        doi_record = connection.execute(
            "SELECT id FROM bibliographic_records WHERE doi = ?", (doi,)
        ).fetchone() if doi else None
        record_id = str(doi_record["id"]) if doi_record else str(uuid.uuid4())
        if doi_record is None:
            connection.execute(
                """
                INSERT INTO bibliographic_records(
                    id, record_type, title, authors_json, publication_title,
                    publisher, publication_date, doi, isbn, url, csl_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?)
                """,
                (
                    record_id, fields["record_type"], fields["title"],
                    json.dumps(fields["authors"], ensure_ascii=False),
                    fields["publication_title"], fields["publisher"],
                    fields["publication_date"], doi, fields["isbn"],
                    fields["url"], now, now,
                ),
            )
        else:
            # An approved literature record may already own this DOI. Add only
            # fields it lacks; never replace previously reviewed values.
            connection.execute(
                """
                UPDATE bibliographic_records SET
                    authors_json = CASE WHEN authors_json = '[]' THEN ? ELSE authors_json END,
                    publication_title = COALESCE(publication_title, ?),
                    publisher = COALESCE(publisher, ?),
                    publication_date = COALESCE(publication_date, ?),
                    isbn = COALESCE(isbn, ?),
                    url = COALESCE(url, ?),
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    json.dumps(fields["authors"], ensure_ascii=False),
                    fields["publication_title"], fields["publisher"],
                    fields["publication_date"], fields["isbn"], fields["url"], now,
                    record_id,
                ),
            )
        connection.execute(
            """
            INSERT INTO source_asset_bibliography(
                source_asset_id, bibliographic_record_id, metadata_source,
                metadata_status, source_fields_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                source_asset_id, record_id, fields["metadata_source"],
                fields["metadata_status"], json.dumps(fields, ensure_ascii=False), now,
            ),
        )
        connection.execute(
            """
            INSERT INTO bibliographic_provenance(
                bibliographic_record_id, provider, provider_record_id,
                source_url, retrieved_at, provider_payload_json
            ) VALUES (?, 'source_file', ?, ?, ?, ?)
            """,
            (
                record_id, source_asset_id, fields["url"], now,
                json.dumps(fields, ensure_ascii=False),
            ),
        )
    else:
        record_id = str(existing["id"])
    connection.execute(
        """
        INSERT OR IGNORE INTO document_unit_bibliography(
            document_unit_id, bibliographic_record_id, relation_type
        ) VALUES (?, ?, 'describes')
        """,
        (document_unit_id, record_id),
    )
    result = source_bibliography(connection, source_asset_id)
    assert result is not None
    return result
