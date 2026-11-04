"""Auxiliary public-Web discovery; snippets never become verified evidence."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any

from fukamiru.literature.content import _NoRedirect, public_https, source_passages
from fukamiru.literature.service import _system_ssl_context
from fukamiru.revisions.reference_web import parse_page_metadata
from fukamiru.revisions.research_validation import academic_variants, compact_query_fallback


def supplement_queries(plan: dict[str, Any]) -> list[str]:
    """Use short supplied concepts and independently planned synonyms, not a claim sentence."""
    variants = academic_variants(plan.get("variants") or [])
    original = academic_variants([str(plan.get("query") or "")])
    broad = [" ".join(q.split()[:2]) for q in [*original, *variants] if len(q.split()) > 2]
    japanese = sorted((q for q in variants if re.search(r"[ぁ-んァ-ヶ一-龥]", q)), key=len)
    return list(dict.fromkeys(academic_variants([
        *broad[:1], *japanese[:1], *sorted(variants, key=len),
        *compact_query_fallback(str(plan.get("query") or "")),
    ])))[:3]


def validate_endpoint(url: str) -> str:
    """The configured proxy is local; public source URLs are validated separately."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        raise ValueError("RustXNG URL must be a local HTTP(S) endpoint")
    try:
        local = ipaddress.ip_address(parsed.hostname or "").is_loopback
    except ValueError:
        local = parsed.hostname == "localhost"
    if not local or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("RustXNG URL must use a loopback address without a path or query")
    return url.rstrip("/")


def rustxng_search(query: str, *, endpoint: str, timeout: float) -> dict[str, Any]:
    parameters = urllib.parse.urlencode({
        "q": query, "format": "json", "full": "1", "max_results": 8,
        "strategy": "parallel", "categories": "general",
    })
    request = urllib.request.Request(validate_endpoint(endpoint) + "/search?" + parameters,
                                     headers={"Accept": "application/json"})
    opener = urllib.request.build_opener(_NoRedirect())
    with opener.open(request, timeout=timeout) as response:
        data = response.read(2_000_001)
    if len(data) > 2_000_000:
        raise ValueError("RustXNG response exceeds the configured read budget")
    payload = json.loads(data)
    if not isinstance(payload.get("results"), list):
        raise ValueError("RustXNG response has no results array")
    if payload.get("error"):
        raise ValueError("RustXNG: " + str(payload["error"])[:500])
    unresponsive = payload.get("unresponsive_engines") or []
    if not payload["results"] and unresponsive:
        raise ValueError("RustXNG returned no candidates while engines were unavailable: "
                         + str(unresponsive)[:1000])
    results = []
    for item in payload["results"][:8]:
        url = str(item.get("url") or "")
        if urllib.parse.urlsplit(url).scheme != "https":
            continue
        results.append({"url": url, "title": str(item.get("title") or ""),
                        "snippet": str(item.get("content") or ""),
                        "engines": item.get("engines") or []})
    return {"results": results, "unresponsive_engines": unresponsive}


class _Body(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg", "nav", "header", "footer"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg", "nav", "header", "footer"}:
            self.hidden = max(0, self.hidden - 1)
        elif tag in {"p", "div", "li", "section", "article", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def read_web_source(url: str, queries: list[str], *, timeout: float) -> dict[str, Any]:
    """Fetch the identified page rather than trusting a search result's body field."""
    opener = urllib.request.build_opener(
        _NoRedirect(), urllib.request.HTTPSHandler(context=_system_ssl_context())
    )
    for _ in range(4):
        if not public_https(url):
            return {"status": "unsafe_url"}
        try:
            with opener.open(urllib.request.Request(url, headers={
                "User-Agent": "Fukamiru research/32", "Accept": "text/html",
            }), timeout=timeout) as response:
                if response.status != 200:
                    return {"status": "http_not_content", "http_status": response.status}
                prefix = response.read(16)
                if prefix.startswith(b"%PDF-"):
                    return {"status": "pdf", "source_url": url}
                data = prefix + response.read(2_000_001 - len(prefix))
                charset = response.headers.get_content_charset() or "utf-8"
            break
        except urllib.error.HTTPError as exc:
            location = exc.headers.get("Location")
            if exc.code not in {301, 302, 303, 307, 308} or not location:
                raise
            url = urllib.parse.urljoin(url, location)
    else:
        return {"status": "redirect_limit"}
    if len(data) > 2_000_000:
        return {"status": "size_limit"}
    if data.startswith(b"%PDF-"):
        return {"status": "pdf", "source_url": url}
    document = data.decode(charset, errors="replace")
    parser = _Body()
    parser.feed(document)
    text = re.sub(r"[ \t]+", " ", "".join(parser.parts)).strip()
    metadata = parse_page_metadata(document)
    # Publisher catalogues often print ISBN only in visible bibliographic fields.
    isbn = re.search(r"ISBN(?:-1[03])?\s*[:：]?\s*((?:97[89][- ]?)?[\dXx][-\dXx ]{8,20})",
                     text, flags=re.I)
    if isbn and not metadata.get("isbn"):
        value = re.sub(r"[-\s]", "", isbn.group(1))
        if re.fullmatch(r"(?:\d{9}[\dXx]|\d{13})", value):
            metadata.update(isbn=value, record_type="book")
    return {"status": "retrieved" if text else "empty", "source_url": url,
            "text": text, "excerpt": source_passages(text, queries),
            "metadata": metadata,
            "content_sha256": hashlib.sha256(data).hexdigest()}
