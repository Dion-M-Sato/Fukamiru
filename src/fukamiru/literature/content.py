"""Bounded source reading for research, separate from approved corpus imports."""

from __future__ import annotations

import hashlib
import html
import io
import ipaddress
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from fukamiru.literature.service import LiteratureCandidate, _system_ssl_context


def merge_source_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A repeated metadata hit must not replace an already retrieved source body."""
    levels = {"full_text": 2, "abstract": 1, "bibliographic_metadata": 0}
    merged = {}

    def priority(record):
        return (
            levels.get(record.get("provenance", {}).get("source_level"), 0),
            len(str(record.get("excerpt") or "")),
            float(record.get("confidence") or 0),
        )

    for record in records:
        key = (str(record.get("provider")), str(record.get("provider_record_id")))
        if not record.get("provider") or not record.get("provider_record_id"):
            key = ("unidentified", hashlib.sha256(
                json.dumps(record, sort_keys=True, default=str).encode()
            ).hexdigest())
        if key not in merged or priority(record) > priority(merged[key]):
            merged[key] = record
    return list(merged.values())


def abstract_text(payload: dict[str, Any]) -> str:
    inverted = payload.get("abstract_inverted_index")
    if isinstance(inverted, dict):
        positioned = [
            (position, str(word))
            for word, positions in inverted.items()
            if isinstance(positions, list)
            for position in positions
            if isinstance(position, int)
        ]
        if positioned:
            return " ".join(word for _, word in sorted(positioned))[:100_000]
    return html.unescape(re.sub(r"<[^>]+>", " ", str(payload.get("abstract") or "")))[:100_000]


def _query_terms(queries: list[str]) -> set[str]:
    terms = set()
    for query in queries:
        terms.update(re.findall(r"[a-zA-Z]{3,}", query.casefold()))
        for run in re.findall(r"[ぁ-んァ-ヶ一-龥]{2,}", query):
            terms.update(run[i : i + 2] for i in range(len(run) - 1))
    return terms


def source_passages(text: str, queries: list[str], *, limit: int = 6000) -> str:
    """Select verbatim windows throughout the source, rather than its prefix."""
    if len(text) <= limit:
        return text
    terms = _query_terms(queries)
    windows = [(i, text[i : i + 1500]) for i in range(0, len(text), 1500)]
    selected = sorted(
        windows, key=lambda pair: (-sum(term in pair[1].casefold() for term in terms), pair[0])
    )[: max(1, limit // 1500)]
    return "\n[…]\n".join(value for _, value in sorted(selected))


def pdf_passages(
    content: dict[str, Any], queries: list[str], *, limit: int = 6000
) -> dict[str, Any]:
    """Project a cached full-source read onto this question, retaining page boundaries."""
    pages = [page for page in content.get("pages", []) if page.get("text", "").strip()]
    if not pages:
        return content
    terms = _query_terms(queries)
    ranked = sorted(
        pages,
        key=lambda page: (-sum(term in page["text"].casefold() for term in terms), page["number"]),
    )
    selected: dict[int, str] = {}
    remaining = limit
    by_number = {page["number"]: page for page in pages}
    for page in ranked:
        if page["number"] in selected:
            continue
        if remaining <= 0:
            break
        text = source_passages(page["text"], queries, limit=max(1, remaining))[:remaining]
        selected[page["number"]] = text
        remaining -= len(text)
        # Spend remaining context budget around the relevant passage, not on
        # four predetermined pages or only the front matter of a long work.
        for number in (page["number"] - 1, page["number"] + 1):
            neighbor = by_number.get(number)
            if neighbor and number not in selected and len(neighbor["text"]) <= remaining:
                selected[number] = neighbor["text"]
                remaining -= len(neighbor["text"])
    selected_pages = sorted(selected)
    return {
        **content,
        "excerpt": "\n[…]\n".join(selected[number] for number in selected_pages),
        "locator": ", ".join(f"p. {number}" for number in selected_pages),
        "selected_pages": selected_pages,
        "excerpt_complete": content.get("read_complete", False)
        and all(selected.get(page["number"]) == page["text"] for page in pages),
    }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def public_https(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
    ):
        return False
    addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    return bool(addresses) and all(ipaddress.ip_address(row[4][0]).is_global for row in addresses)


def read_oa_pdf(
    candidate: LiteratureCandidate,
    queries: list[str],
    *,
    timeout: float,
    max_bytes: int,
    max_text_characters: int = 2_000_000,
    max_parse_seconds: float = 30.0,
    download_dir: Path | None = None,
    recover: Callable[[Path, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Read a public OA PDF, optionally reusing the ingestion OCR recovery service."""
    result: dict[str, Any] = {"status": "not_open_access"}
    if not candidate.oa_url or str(candidate.oa_status).lower() not in {
        "gold",
        "green",
        "hybrid",
        "bronze",
        "diamond",
        "open",
        "publicly_accessible",
    }:
        return result
    url = candidate.oa_url
    try:
        opener = urllib.request.build_opener(
            _NoRedirect(), urllib.request.HTTPSHandler(context=_system_ssl_context())
        )
        for _ in range(4):
            if not public_https(url):
                return {"status": "unsafe_url"}
            request = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/pdf",
                    "User-Agent": "Fukamiru research/32",
                },
            )
            try:
                with opener.open(request, timeout=timeout) as response:
                    data = response.read(max_bytes + 1)
                break
            except urllib.error.HTTPError as exc:
                location = exc.headers.get("Location")
                if exc.code not in {301, 302, 303, 307, 308} or not location:
                    raise
                url = urllib.parse.urljoin(url, location)
        else:
            return {"status": "redirect_limit"}
        if len(data) > max_bytes:
            return {"status": "size_limit"}
        if not data.startswith(b"%PDF-"):
            return {"status": "not_pdf"}
        digest = hashlib.sha256(data).hexdigest()
        local_path = None
        if download_dir is not None:
            download_dir.mkdir(parents=True, exist_ok=True)
            local_path = download_dir / (digest + ".pdf")
            if (not local_path.exists()
                    or hashlib.sha256(local_path.read_bytes()).hexdigest() != digest):
                import os
                import tempfile

                with tempfile.NamedTemporaryFile(dir=download_dir, delete=False) as stream:
                    stream.write(data)
                    temporary = Path(stream.name)
                os.replace(temporary, local_path)

        def recover_content(kind: str) -> dict[str, Any]:
            if recover is None or local_path is None:
                return {"status": "encrypted" if kind == "encrypted_pdf" else "no_extractable_text"}
            recovered = recover(local_path, kind)
            return pdf_passages({**recovered, "source_url": url,
                                 "content_sha256": digest, "source_path": str(local_path)}, queries)

        try:
            reader = PdfReader(io.BytesIO(data))
        except Exception as exc:
            from fukamiru.ocr.pdf_recovery import is_encrypted_pdf_error

            if is_encrypted_pdf_error(exc):
                return recover_content("encrypted_pdf")
            raise
        if reader.is_encrypted:
            return recover_content("encrypted_pdf")
        pages = []
        characters = 0
        stopped_reason = None
        deadline = time.monotonic() + max_parse_seconds
        for number, page in enumerate(reader.pages, start=1):
            if time.monotonic() >= deadline:
                stopped_reason = "parse_time_budget"
                break
            text = page.extract_text() or ""
            remaining = max_text_characters - characters
            pages.append({"number": number, "text": text[:remaining]})
            characters += len(text[:remaining])
            if len(text) > remaining or (
                characters >= max_text_characters and number < len(reader.pages)
            ):
                stopped_reason = "text_size_budget"
                break
        content = {
            "status": "retrieved"
            if any(p["text"].strip() for p in pages)
            else "no_extractable_text",
            "source_level": "full_text",
            "source_url": url,
            "pages": pages,
            "content_sha256": digest,
            "source_path": str(local_path) if local_path else None,
            "pages_examined": len(pages),
            "total_pages": len(reader.pages),
            "read_complete": stopped_reason is None,
            "stopped_reason": stopped_reason,
        }
        if content["status"] == "no_extractable_text" and recover is not None:
            return recover_content("invalid_text_layer")
        return pdf_passages(content, queries)
    except Exception as exc:
        return {"status": "unavailable", "error": type(exc).__name__, "reason": str(exc)[:1000]}
