"""Bounded public-Web bibliography evidence discovery for V29.

Search results are leads, not facts. Only fetched, identity-matched page metadata
may enrich a manifestation; conflicting fields remain reviewable.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from html.parser import HTMLParser
from typing import Any

from fukamiru.revisions.reference_canonical import canonicalize_reference


def _relation_queries(title: str, candidates: list[dict[str, Any]]) -> list[str]:
    if not re.search(r"[ぁ-んァ-ヶ一-龥]", title):
        return []
    foreign = [
        str(item.get("title") or "") for item in candidates
        if isinstance(item, dict)
        and str(item.get("publication_language") or "").casefold()
        not in {"", "ja", "jpn"}
        and canonicalize_reference(item)["v29_render_status"] == "render_ready"
    ]
    if not foreign:
        return []
    return list(dict.fromkeys([
        f'"{title}" 原題',
        *(f'"{title}" "{value}"' for value in foreign[:2]),
    ]))


def _translation_side_discovery_plan(
    title: str,
    author: str,
    candidates: list[dict[str, Any]],
    hypotheses: dict[str, Any],
) -> dict[str, Any]:
    """Plan bounded Japanese-manifestation discovery after a foreign work match."""
    is_japanese_mention = bool(re.search(r"[ぁ-んァ-ヶ一-龥]", title))
    ready = [
        item for item in candidates if isinstance(item, dict)
        and canonicalize_reference(item)["v29_render_status"] == "render_ready"
    ]
    ready_japanese = [
        item for item in ready
        if str(item.get("publication_language") or "").casefold() in {"ja", "jpn", "jp"}
    ]
    ready_foreign = [
        item for item in ready
        if str(item.get("publication_language") or "").casefold()
        not in {"", "ja", "jpn", "jp", "unknown"}
    ]
    triggered = bool(is_japanese_mention and ready_foreign and not ready_japanese)
    if not triggered:
        return {
            "triggered": False, "mode": "not_applicable", "queries": [],
            "ready_foreign_count": len(ready_foreign),
            "ready_japanese_count": len(ready_japanese),
        }

    authors = list(dict.fromkeys(filter(None, (
        author, *(str(value).strip() for value in hypotheses.get("authors") or [])
    ))))
    original_titles = list(dict.fromkeys(
        str(item.get("title") or "").strip() for item in ready_foreign
        if str(item.get("title") or "").strip()
    ))
    query_specs: list[dict[str, str]] = []

    def add(tier: str, query: str) -> None:
        if query and not any(item["query"] == query for item in query_specs):
            query_specs.append({"tier": tier, "query": query})

    generic_short_title = len(_normalized(title)) <= 8
    if generic_short_title and authors:
        add("J1", f'"{title}" "{authors[0]}"')
    add("J1", f'"{title}"')
    if authors:
        add("J1", f'"{title}" "{authors[0]}"')
    add("J1", f'"{title}" 訳')
    for variant in list(hypotheses.get("titles") or [])[:2]:
        if _normalized(variant) != _normalized(title):
            add("J2", f'"{title}" "{variant}"')
    add("J3", f'"{title}" CiNii')
    add("J3", f'"{title}" OPAC')
    add("J4", f'"{title}" 出版社')
    for original in original_titles[:1]:
        add("J5", f'"{title}" "{original}"')
    return {
        "triggered": True,
        "mode": "translation_side_discovery",
        "queries": query_specs[:8],
        "ready_foreign_count": len(ready_foreign),
        "ready_japanese_count": 0,
        "original_titles": original_titles,
        "authors": authors,
    }


def _targeted_field_queries(
    title: str, candidates: list[dict[str, Any]], author: str,
) -> tuple[list[str], list[str]]:
    """Request only fields still required by otherwise identified manifestations."""
    missing = sorted({
        field
        for item in candidates
        if isinstance(item, dict)
        for field in canonicalize_reference(item).get("v29_required_missing_fields") or []
    })
    targeted = [field for field in missing if field in {"translators"}]
    if not targeted or any(field not in {"translators"} for field in missing):
        return [], targeted
    identifiers = list(dict.fromkeys(
        str(item.get("isbn") or "") for item in candidates if item.get("isbn")
    ))
    queries = [*(f'"{value}"' for value in identifiers)]
    if "translators" in targeted:
        queries.extend((
            f'"{title}" {author} 訳'.strip(),
            f'"{title}" 訳者',
        ))
    return list(dict.fromkeys(queries)), targeted


def _reuse_targeted_evidence(
    candidates: list[dict[str, Any]], targeted_fields: list[str],
) -> list[str]:
    recovered: set[str] = set()
    if "translators" not in targeted_fields:
        return []
    for candidate in candidates:
        if candidate.get("translators"):
            recovered.add("translators")
            continue
        for audit in candidate.get("original_title_audit") or []:
            raw = str(audit.get("original_title_raw") or "")
            match = re.search(r"[／/]\s*([一-龥ぁ-んァ-ヶー・]{2,30})\s+訳", raw)
            if not match:
                continue
            name = match.group(1)
            source_url = audit.get("source_url") or candidate.get("url")
            candidate["translators"] = [name]
            candidate.setdefault("responsibility_sources", []).append({
                "provider": candidate.get("provider") or "catalog",
                "url": source_url,
                "responsibility_raw": [f"{name} 訳"],
                "responsibility_source": "exact_record_detail",
                "translators": [name],
            })
            candidate.setdefault("field_evidence", {}).setdefault("translators", []).append({
                "value": [name], "source_url": source_url,
                "source_type": "reused_provider_detail_raw_evidence",
                "raw_value": raw, "parser": "targeted_responsibility_v29",
                "confidence": 0.95, "verification_status": "source_recorded",
            })
            recovered.add("translators")
            break
    return sorted(recovered)


def _canonical_contribution_title(mention_title: str, evidence_text: str) -> str | None:
    """Recover a subtitle only when the evidence prints it with the mention."""
    match = re.search(
        re.escape(mention_title)
        + r"\s*((?:[-–—―]​?){1,2}\s*.*?)(?=&nbsp;|\u00a0|[.。\n]|$)",
        evidence_text,
    )
    if not match:
        return None
    value = re.sub(r"\s+", " ", mention_title + match.group(1)).strip()
    return value if _normalized(value) != _normalized(mention_title) else None


def is_researcher_contribution_listing(page: dict[str, Any]) -> bool:
    """Researcher profiles contain multiple works, not one bibliographic record."""
    if page.get("contribution_listing") is True:
        return True
    parsed = urllib.parse.urlsplit(str(page.get("url") or ""))
    host = str(parsed.hostname or "").casefold()
    parts = [part for part in parsed.path.split("/") if part]
    if host == "researchmap.jp" or host.endswith(".researchmap.jp"):
        return bool(parts and len(parts) < 3)
    if host == "jglobal.jst.go.jp":
        return len(parts) == 2 and parts[0] == "public"
    return bool(
        any(part.casefold() in {
            "profile", "researcher", "researchers", "person", "people",
            "bibliography", "publications", "toc", "collections",
        } for part in parts)
        or re.search(
            r"研究者情報|researcher profile|author profile",
            str(page.get("title") or ""), re.I,
        )
    )


def _page_identifies_contribution(page: dict[str, Any], mention: str) -> bool:
    fields = page.get("structured_metadata") or {}
    page_title = str(page.get("title") or "")
    mention_key = _normalized(mention)
    return bool(
        mention_key and (
            mention_key in _normalized(page_title)
            or isinstance(fields, dict)
            and _normalized(fields.get("title")) == mention_key
        )
    )


def _contribution_title_matches(mention: str, item_title: str) -> bool:
    if _normalized(mention) == _normalized(item_title):
        return True
    if not item_title.startswith(mention):
        return False
    suffix = item_title[len(mention):].lstrip()
    return bool(suffix and suffix[0] in "-–—―:：")


class _ResearcherContributionParser(HTMLParser):
    """Keep researchmap and J-GLOBAL bibliography fields inside one list item."""

    def __init__(self, source_url: str, provider: str) -> None:
        super().__init__(convert_charrefs=True)
        self.source_url = source_url
        self.provider = provider
        self.items: list[dict[str, str]] = []
        self._in_mdisc = False
        self._item: dict[str, Any] | None = None
        self._li_depth = 0
        self._title_active = False
        self._author_active = False
        self._author_seen = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = set(str(values.get("class") or "").split())
        if self.provider == "jglobal" and tag == "ul" and "mdisc" in classes:
            self._in_mdisc = True
        if self._item is None:
            listed = (
                self.provider == "researchmap"
                and tag == "li" and "rm-cv-disclosed" in classes
            ) or (self.provider == "jglobal" and tag == "li" and self._in_mdisc)
            if listed:
                self._item = {"title": [], "author": [], "details": [], "raw": []}
                self._li_depth = 1
            return
        if tag == "li":
            self._li_depth += 1
        if self.provider == "researchmap":
            if tag == "a" and "rm-cv-list-title" in classes:
                self._item["href"] = str(values.get("href") or "")
                self._title_active = True
            elif tag == "div" and "rm-cv-list-author" in classes:
                self._author_active = True

    def handle_data(self, data: str) -> None:
        if self._item is None:
            return
        self._item["raw"].append(data)
        if self._title_active:
            self._item["title"].append(data)
        elif self._author_active:
            self._item["author"].append(data)
        elif self._author_seen:
            self._item["details"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._item is not None:
            if tag == "a":
                self._title_active = False
            elif tag == "div" and self._author_active:
                self._author_active = False
                self._author_seen = True
            elif tag == "li":
                self._li_depth -= 1
                if self._li_depth == 0:
                    self._finish_item()
        if self.provider == "jglobal" and tag == "ul":
            self._in_mdisc = False

    def _finish_item(self) -> None:
        item = self._item or {}
        self._item = None
        self._title_active = False
        self._author_active = False
        self._author_seen = False
        raw = re.sub(r"\s+", " ", " ".join(item.get("raw") or [])).strip()
        if self.provider == "researchmap":
            title = re.sub(r"\s+", " ", " ".join(item.get("title") or [])).strip()
            author = re.sub(r"\s+", " ", " ".join(item.get("author") or [])).strip()
            detail = re.sub(r"\s+", " ", " ".join(item.get("details") or [])).strip()
            url = urllib.parse.urljoin(self.source_url, item.get("href") or "")
            parsed = urllib.parse.urlsplit(url)
            source_path = urllib.parse.urlsplit(self.source_url).path
            source_parts = [part for part in source_path.split("/") if part]
            item_parts = [part for part in parsed.path.split("/") if part]
            if (
                parsed.hostname != "researchmap.jp" or not source_parts
                or len(item_parts) != 3 or item_parts[0] != source_parts[0]
                or not item_parts[2].isdigit() or not title or not author
            ):
                return
            year_match = re.search(r"(?:18|19|20)\d{2}", detail)
            pages_match = re.search(r"\b\d{1,4}\s*[-–—]\s*\d{1,4}\b", detail)
            if not year_match:
                return
            end = pages_match.start() if pages_match else year_match.start()
            container = detail[:end].strip(" ,、.。")
            if not container:
                return
            self.items.append({
                "title": title, "author": author, "container_title": container,
                "year": year_match.group(), "pages": pages_match.group() if pages_match else "",
                "source_url": url, "record_id": url, "raw_evidence": raw[:1000],
            })
            return
        match = re.match(
            r"\s*(?P<author>[^.。]{2,80})[.。]\s*"
            r"(?P<title>.+?)[.。]\s*(?P<container>[^.。]{2,120})[.。]\s*"
            r"(?P<year>(?:18|19|20)\d{2})"
            r"(?:[.。]\s*(?P<pages>\d{1,4}\s*[-–—]\s*\d{1,4}))?",
            raw,
        )
        if not match:
            return
        identity = hashlib.sha256(raw.encode()).hexdigest()[:16]
        self.items.append({
            "author": match.group("author").strip(),
            "title": match.group("title").strip(),
            "container_title": match.group("container").strip(),
            "year": match.group("year"),
            "pages": str(match.group("pages") or "").strip(),
            "source_url": self.source_url,
            "record_id": f"{self.source_url}#contribution-{identity}",
            "raw_evidence": raw[:1000],
        })


def parse_researcher_contribution_items(document: str, url: str) -> list[dict[str, str]]:
    """Extract bounded, individually scoped contributions from known registries."""
    host = str(urllib.parse.urlsplit(url).hostname or "").casefold()
    provider = (
        "researchmap" if host == "researchmap.jp"
        else "jglobal" if host == "jglobal.jst.go.jp" else ""
    )
    if not provider:
        return []
    parser = _ResearcherContributionParser(url, provider)
    parser.feed(document[:1_000_000])
    return parser.items[:300]


def _contribution_hypotheses(
    title: str, decisions: list[dict[str, Any]], pages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Extract search hypotheses, while keeping snippets non-canonical."""
    hypotheses: list[dict[str, Any]] = []
    title_key = _normalized(title)
    for item in decisions:
        text = " ".join((str(item.get("result_title") or ""), str(item.get("snippet") or "")))
        if title_key not in _normalized(text):
            continue
        containers = re.findall(r"[『「]([^』」]{2,80})[』」]\s*[（(]?((?:19|20)\d{2})", text)
        for container, year in containers:
            if _normalized(container) == title_key:
                continue
            hypotheses.append({
                "container_title": container, "year": year,
                "source_url": item.get("url"), "source_kind": "search_hypothesis_only",
                "raw_evidence": text[:1000], "verification_status": "hypothesis",
            })
        person = re.match(
            r"\s*([一-龥]{1,4})\s+([一-龥]{1,4})\s*(?:\(|\|)",
            str(item.get("result_title") or ""),
        )
        structured = re.search(
            re.escape(title)
            + r"[^.。]{0,160}[.。]\s*([一-龥]{1,4}\s*[一-龥]{1,4})\s*[.。]\s*"
            r"([^。\n]{2,80}?)\s+(\d{1,4}\s*[-–—]\s*\d{1,4})\s+"
            r"((?:19|20)\d{2})",
            text,
        )
        reverse = re.search(
            re.escape(title)
            + r"[^.。]{0,160}[.。]\s*([^。\n]{2,80})[.。]\s*"
            r"((?:19|20)\d{2})[.。]\s*(\d{1,4}\s*[-–—]\s*\d{1,4})"
            r"[^;；]{0,40}[;；]\s*([一-龥]{1,4}\s*[一-龥]{1,4})",
            text,
        )
        if structured or reverse:
            author_value = (
                re.sub(r"\s+", "", structured.group(1)) if structured
                else re.sub(r"\s+", "", reverse.group(4))
            )
            if person:
                author_value = person.group(1) + person.group(2)
            hypotheses.append({
                "author": author_value,
                "container_title": (
                    structured.group(2).strip() if structured else reverse.group(1).strip()
                ),
                "pages": structured.group(3) if structured else reverse.group(3),
                "year": structured.group(4) if structured else reverse.group(2),
                "source_url": item.get("url"), "source_kind": "search_hypothesis_only",
                "raw_evidence": text[:1000], "verification_status": "hypothesis",
                "canonical_title": _canonical_contribution_title(title, text),
            })
    for page in pages:
        if page.get("status") != "retrieved":
            continue
        if is_researcher_contribution_listing(page):
            items = page.get("contribution_items") or []
            matches = [
                item for item in items if isinstance(item, dict)
                and _contribution_title_matches(title, str(item.get("title") or ""))
            ]
            page["contribution_match_count"] = len(matches)
            if len(matches) == 1 and all(
                matches[0].get(field) for field in (
                    "author", "container_title", "year", "record_id", "source_url"
                )
            ):
                item = matches[0]
                item_author = str(item["author"])
                if re.fullmatch(r"[一-龥\s]+", item_author):
                    item_author = re.sub(r"\s+", "", item_author)
                hypotheses.append({
                    "author": item_author,
                    "container_title": item["container_title"],
                    "year": item["year"], "pages": item.get("pages") or None,
                    "canonical_title": item["title"],
                    "source_url": item["source_url"], "record_id": item["record_id"],
                    "source_kind": "fetched_contribution_item",
                    "raw_evidence": item.get("raw_evidence") or "",
                    "extractor": "researcher_contribution_item_v29",
                    "verification_status": "source_recorded",
                })
            continue
        if (
            (page.get("structured_metadata") or {}).get("record_type")
            == "nonstandard_publication"
            and (page.get("page_evidence") or {}).get("self_identification_as_work")
        ):
            # A work's own publication page is not a contribution listing.
            continue
        if not _page_identifies_contribution(page, title):
            continue
        text = html.unescape(str(page.get("metadata_excerpt") or "")).replace(
            "&nbsp;", "\u00a0"
        )
        if title_key not in _normalized(text):
            continue
        # Normalization changes offsets, so retain a bounded body excerpt around
        # each literal title occurrence when possible.
        literal = text.find(title)
        window = text[literal:literal + 700] if literal >= 0 else text[:900]
        author = None
        canonical_title = _canonical_contribution_title(title, window)
        tail_start = len(canonical_title or title)
        tail = window[tail_start:].lstrip(" .。\u00a0")
        # Researcher registries such as J-GLOBAL print contributions as
        # "author. title. container. year. pages". The quoted work inside a
        # subtitle is not the container.
        registry_sequence = re.match(
            r"([^。.\n\u00a0]{2,100})[.。]\s*((?:19|20)\d{2})[.。]\s*"
            r"(\d{1,4}\s*[-–—]\s*\d{1,4})\b",
            tail,
        )
        structured = re.search(
            r"([一-龥]{1,4}\s*[一-龥]{1,4})\s*[.。\u00a0]+\s*"
            r"([^。\n\u00a0]{2,80}?)\s+(\d{1,4}\s*[-–—]\s*\d{1,4})\s+"
            r"((?:19|20)\d{2})",
            tail,
        )
        if not structured:
            structured = re.search(
                r"([一-龥]{1,4}\s+[一-龥]{1,4})\s+"
                r"([^。\n\u00a0]{2,80}?)\u00a0"
                r"(\d{1,4}\s*[-–—]\s*\d{1,4})\u00a0((?:19|20)\d{2})",
                tail,
            )
        if structured:
            author = re.sub(r"\s+", "", structured.group(1))
        page_title = html.unescape(str(page.get("title") or ""))
        match = re.match(r"\s*([一-龥]{1,4})\s+([一-龥]{1,4})\s*(?:\(|[|｜-])", page_title)
        if match:
            author = match.group(1) + match.group(2)
        if not author:
            match = re.search(r"([一-龥]{2,6})\s*(?:著|執筆|寄稿|[（(]聞き手)", window)
            author = match.group(1) if match else None
        year = (
            registry_sequence.group(2) if registry_sequence
            else structured.group(4) if structured
            else next(iter(re.findall(r"(?:19|20)\d{2}", window)), None)
        )
        pages_match = registry_sequence or structured or re.search(
            r"\b(\d{1,4}\s*[-–—]\s*\d{1,4})\b", window
        )
        container = (
            registry_sequence.group(1).strip() if registry_sequence
            else structured.group(2).strip() if structured else None
        )
        if not container:
            for value in re.findall(r"[『「]([^』」]{2,100})[』」]", window):
                if _normalized(value) != title_key and title_key not in _normalized(value):
                    container = value
                    break
        if not container:
            match = re.search(
                r"([一-龥ァ-ヶA-Za-z0-9][^。\n]{2,70})\s+(?:\d{1,4}\s*[-–—]\s*\d{1,4}\s+)?(?:19|20)\d{2}",
                window,
            )
            container = match.group(1).strip(" -–—:：,.、") if match else None
            if container and re.search(r"\s著\s", container):
                container = re.split(r"\s著\s", container, maxsplit=1)[1].strip()
        if any((author, container, year, pages_match)):
            hypotheses.append({
                "author": author, "container_title": container, "year": year,
                "pages": (
                    registry_sequence.group(3) if registry_sequence
                    else structured.group(3) if structured
                    else pages_match.group(1) if pages_match else None
                ),
                "source_url": page.get("url"), "source_kind": "fetched_page_body",
                "raw_evidence": window, "extractor": "container_contribution_v29",
                "verification_status": "source_recorded",
                "canonical_title": canonical_title,
            })
    return hypotheses


class _MetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.language: str | None = None
        self.meta: dict[str, str] = {}
        self.json_ld: list[dict[str, Any]] = []
        self._json_script = False
        self._script_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "html":
            self.language = values.get("lang") or self.language
        elif tag == "meta":
            key = str(values.get("name") or values.get("property") or "").casefold()
            content = values.get("content")
            if key and content:
                self.meta[key] = content.strip()
        elif tag == "script" and values.get("type") == "application/ld+json":
            self._json_script = True
            self._script_parts = []

    def handle_data(self, data: str) -> None:
        if self._json_script:
            self._script_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "script" or not self._json_script:
            return
        self._json_script = False
        try:
            parsed = json.loads("".join(self._script_parts))
        except (ValueError, TypeError):
            return
        objects = parsed if isinstance(parsed, list) else [parsed]
        for item in objects:
            if not isinstance(item, dict):
                continue
            graph = item.get("@graph")
            self.json_ld.extend(
                value
                for value in (graph if isinstance(graph, list) else [item])
                if isinstance(value, dict)
            )


def _name(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("name")
    if isinstance(value, list):
        return "; ".join(filter(None, (_name(item) for item in value))) or None
    return str(value).strip() if value else None


def parse_page_metadata(document: str) -> dict[str, Any]:
    parser = _MetadataParser()
    parser.feed(document[:256_000])
    fields: dict[str, Any] = {}
    for item in parser.json_ld:
        kind = str(item.get("@type") or "").casefold()
        if kind not in {
            "book", "scholarlyarticle", "article", "product", "creativework",
            "blogposting", "webpage", "digitaldocument",
        }:
            continue
        for source, target in (
            ("name", "title"),
            ("headline", "title"),
            ("isbn", "isbn"),
            ("publisher", "publisher"),
            ("inLanguage", "publication_language"),
            ("datePublished", "year"),
            ("bookEdition", "edition"),
            ("author", "authors"),
            ("translator", "translators"),
            ("editor", "editors"),
            ("translationOfWork", "original_title"),
        ):
            value = _name(item.get(source))
            if value and target not in fields:
                fields[target] = value
        if kind == "book":
            fields.setdefault("record_type", "book")
            if fields.get("original_title"):
                fields["original_title_relation_verified"] = True
        elif kind in {"scholarlyarticle", "article"}:
            fields.setdefault("record_type", "article")
        elif kind in {"creativework", "blogposting", "webpage", "digitaldocument"}:
            fields.setdefault("record_type", "nonstandard_publication")
    meta_map = {
        "citation_title": "title",
        "citation_author": "authors",
        "citation_language": "publication_language",
        "citation_date": "year",
        "citation_doi": "doi",
        "citation_isbn": "isbn",
        "book:isbn": "isbn",
        "book:author": "authors",
        "author": "authors",
        "article:published_time": "year",
        "date": "year",
        "og:site_name": "page_site_name",
    }
    for source, target in meta_map.items():
        if parser.meta.get(source) and target not in fields:
            fields[target] = parser.meta[source]
    if parser.language:
        # A site's UI language does not prove a book's publication language.
        fields["page_language"] = parser.language
    if fields.get("year"):
        match = re.search(r"(?:18|19|20)\d{2}", str(fields["year"]))
        if match:
            fields["year"] = match.group()
    for field in ("authors", "translators", "editors"):
        if isinstance(fields.get(field), str):
            fields[field] = [part.strip() for part in fields[field].split(";") if part.strip()]
    return fields


def parse_ndl_detail_metadata(document: str) -> dict[str, Any]:
    """Extract labelled bibliography evidence from a public NDL detail page."""
    visible = re.sub(
        r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", document))
    )
    fields: dict[str, Any] = {}
    isbn = re.search(r"\bISBN\s+([0-9Xx-]{10,20})\b", visible)
    if isbn:
        fields["isbn"] = isbn.group(1)
    title = re.search(r"\bタイトル\s+(.{2,240}?)\s+タイトルよみ\b", visible)
    if title:
        fields["title"] = title.group(1).strip()
    publication = re.search(r"出版事項\s+(.{2,180}?)\s+出版年月日等\b", visible)
    if publication:
        statement = publication.group(1).strip()
        fields["publication_statement_raw"] = statement
        if ":" in statement or "：" in statement:
            place, publisher = re.split(r"\s*[:：]\s*", statement, maxsplit=1)
            fields["publication_place"] = place.strip()
            fields["publisher"] = publisher.strip()
        else:
            fields["publisher"] = statement
    date = re.search(r"出版年月日等\s+((?:18|19|20)\d{2})(?:[.年\-/]\d{1,2})?", visible)
    if date:
        fields["year"] = date.group(1)
    responsibility = re.search(
        r"著者・編者\s+(.{1,350}?)\s+(?:シリーズタイトル|著者標目|出版事項)", visible
    )
    if responsibility:
        labelled = [
            value.strip()
            for value in re.findall(
                r"(.+?(?:\[\s*著\s*\]|著|訳|編))(?=\s|$)",
                responsibility.group(1),
            )
        ]
        if labelled:
            fields["responsibility_raw"] = labelled
            for role_field, suffix in (("authors", r"\[\s*著\s*\]|著"),
                                       ("translators", r"訳"), ("editors", r"編")):
                people = [
                    re.sub(rf"\s*(?:{suffix})$", "", value).strip()
                    for value in labelled if re.search(rf"(?:{suffix})$", value)
                ]
                if people:
                    fields[role_field] = people
    content_translators = [
        value.strip()
        for value in re.findall(
            r"(?:内容細目\s*[:：]?[^。]{0,180}?[／/]\s*)"
            r"([一-龥ぁ-んァ-ヶー・]{2,30})\s+訳",
            visible,
        )
    ]
    if content_translators:
        fields["translators"] = list(dict.fromkeys(content_translators))
        raw = list(fields.get("responsibility_raw") or [])
        raw.extend(f"{name} 訳" for name in content_translators)
        fields["responsibility_raw"] = list(dict.fromkeys(raw))
    language = re.search(r"本文の言語コード\s+([a-z]{2,3})\b", visible)
    if language:
        fields["publication_language"] = language.group(1)
    for original in re.finditer(r"原タイトル\s*[:：]?\s*", visible[:100_000]):
        tail = visible[original.end():original.end() + 400]
        section = re.search(
            r"\s+(?:書誌注記|一般注記|書店で探す|全国の図書館|"
            r"資料詳細|資料に関する注記|内容細目|"
            r"要約等|著者紹介|入手条件|所蔵機関|対象利用者|"
            r"NDC\d*版?|NDLC|本文の言語コード|原文の言語コード|ISBN|内容紹介)\s*",
            tail,
        )
        clean = (tail[:section.start()] if section else tail).strip()
        if section and clean and len(clean) <= 180:
            clean = re.split(r"\s+(?=[ぁ-んァ-ヶ一-龥])", clean, maxsplit=1)[0].strip()
            clean = re.sub(r":(?=\S)", ": ", clean)
            fields["original_title_raw"] = tail[:200].strip()
            fields["original_title"] = clean
            fields["original_title_parser_rule"] = "ndl_original_title_section"
            fields["original_title_relation_verified"] = True
            break
    return fields


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class SearchLead(str):
    """A URL with RSS metadata; remains compatible with URL-only search callers."""

    def __new__(cls, url: str, title: str, snippet: str, rank: int):
        lead = super().__new__(cls, url)
        lead.result_title = title
        lead.snippet = snippet
        lead.rank = rank
        return lead


class _YahooResultsParser(HTMLParser):
    """Read public result links only; titles/snippets remain untrusted leads."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.href = ""
        self.anchor = False
        self.title_parts: list[str] = []
        self.snippet_parts: list[str] = []
        self.results: list[SearchLead] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "li":
            self.depth += 1
            if self.depth == 1:
                self.href = ""
                self.title_parts = []
                self.snippet_parts = []
        elif tag == "a" and self.depth and not self.href:
            href = str(dict(attrs).get("href") or "")
            host = str(urllib.parse.urlsplit(href).hostname or "")
            if href.startswith("https://") and not host.endswith("yahoo.co.jp"):
                self.href = href
                self.anchor = True

    def handle_data(self, data: str) -> None:
        if self.depth and self.href:
            self.snippet_parts.append(data)
            if self.anchor:
                self.title_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self.anchor = False
        elif tag == "li" and self.depth:
            self.depth -= 1
            if self.depth == 0 and self.href and self.title_parts:
                self.results.append(SearchLead(
                    self.href,
                    " ".join(self.title_parts).strip(),
                    " ".join(self.snippet_parts).strip(),
                    len(self.results) + 1,
                ))


def _yahoo_exact_title_search(
    title: str, timeout: float, *, site: str | None = None,
) -> list[str]:
    query = f'"{title}"' + (f" site:{site}" if site else "")
    url = "https://search.yahoo.co.jp/search?" + urllib.parse.urlencode({"p": query})
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout) as response:
            document = response.read(300_001)
        if len(document) > 300_000:
            return []
        parser = _YahooResultsParser()
        parser.feed(document.decode("utf-8", errors="replace"))
        key = _normalized(title)
        return list(dict.fromkeys(
            lead for lead in parser.results
            if key in _normalized(lead.result_title + lead.snippet)
            and (
                not site
                or urllib.parse.urlsplit(lead).hostname == site
                or str(urllib.parse.urlsplit(lead).hostname or "").endswith("." + site)
            )
        ))[:8]
    except (OSError, ValueError):
        return []


def general_web_search(query: str, *, timeout: float = 12) -> list[str]:
    """Search a fixed public endpoint; treat returned links as untrusted leads."""
    url = "https://www.bing.com/search?" + urllib.parse.urlencode({"q": query, "format": "rss"})
    request = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (compatible; Fukamiru-bibliography/1)"}
    )
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout) as response:
            document = response.read(256_001)
        if len(document) > 256_000:
            return []
        root = ET.fromstring(document)
        urls = [
            SearchLead(
                html.unescape(str(item.findtext("link") or "")),
                html.unescape(str(item.findtext("title") or "")),
                html.unescape(str(item.findtext("description") or "")),
                rank,
            )
            for rank, item in enumerate(root.findall("./channel/item"), 1)
        ]
        leads = list(
            dict.fromkeys(value for value in urls if urllib.parse.urlsplit(value).scheme == "https")
        )[:8]
        site_query = re.fullmatch(
            r"(.+?)\s+site:(researchmap\.jp|jglobal\.jst\.go\.jp|booth\.pm)",
            query.strip(), re.I,
        )
        if site_query and not any(
            (str(urllib.parse.urlsplit(lead).hostname or "") == site_query.group(2)
             or str(urllib.parse.urlsplit(lead).hostname or "").endswith(
                 "." + site_query.group(2)
             ))
            and _normalized(site_query.group(1)) in _normalized(
                lead.result_title + lead.snippet
            )
            for lead in leads
        ):
            return _yahoo_exact_title_search(
                site_query.group(1), timeout, site=site_query.group(2)
            ) or leads
        exact = re.fullmatch(r'"(.+)"', query.strip())
        if exact and not any(
            _normalized(exact.group(1)) in _normalized(
                getattr(lead, "result_title", "") + getattr(lead, "snippet", "")
            ) for lead in leads
        ):
            return _yahoo_exact_title_search(exact.group(1), timeout) or leads
        return leads
    except (OSError, ValueError, ET.ParseError):
        return []


def _normalized(value: Any) -> str:
    return re.sub(r"[^\w]", "", str(value or "").casefold())


def _primary_publisher(value: Any) -> str:
    """Compare an imprint with its distribution statement without discarding raw evidence."""
    text = str(value or "").strip()
    text = re.sub(
        r"\s+\S+(?:\s+\S+)*\s*[（(](?:発売|取次|頒布|販売)[）)]\s*$", "", text
    )
    return re.sub(r"\s*[（(](?:出版|発行)[）)]\s*$", "", text).strip()


def _publisher_brand(value: Any) -> str:
    text = _primary_publisher(value).casefold()
    text = re.sub(r"^(?:la|le|les|the)\s+", "", text)
    text = re.sub(r"^(?:éditions|editions|édition|edition)\s+", "", text)
    text = re.sub(r"\s+(?:éditions|editions|édition|edition)$", "", text)
    return _normalized(text)


def _people_values_equivalent(left: Any, right: Any) -> bool:
    def keys(value: Any) -> set[tuple[str, ...]]:
        values = value if isinstance(value, list) else [value]
        return {
            tuple(sorted(filter(None, (
                _normalized(part) for part in re.split(r"[・、,\s]+", str(item))
            ))))
            for item in values if item
        }

    return bool(keys(left)) and keys(left) == keys(right)


def _relevant_url(url: str, title: str, candidates: list[dict[str, Any]]) -> bool:
    return _web_relevance_decision(url, title, "", candidates)["decision"] == "accept"


def _web_relevance_decision(
    url: str, title: str, author: str, candidates: list[dict[str, Any]],
    *, title_hypotheses: tuple[str, ...] = (),
) -> dict[str, Any]:
    parsed = urllib.parse.urlsplit(url)
    host = str(parsed.hostname or "").casefold()
    path = _normalized(urllib.parse.unquote(parsed.path))
    result_title = str(getattr(url, "result_title", "") or "")
    snippet = str(getattr(url, "snippet", "") or "")
    normalized_titles = tuple(
        value for value in (_normalized(title), *map(_normalized, title_hypotheses)) if value
    )
    title_exact = any(value in _normalized(result_title) for value in normalized_titles)
    title_snippet = any(value in _normalized(snippet) for value in normalized_titles)
    title_path = any(value in path for value in normalized_titles)
    author_match = bool(author and _normalized(author) in _normalized(result_title + snippet))
    identifier_match = any(
        _normalized(candidate.get("isbn")) in path + _normalized(snippet)
        for candidate in candidates
        if candidate.get("isbn") and len(_normalized(candidate.get("isbn"))) >= 10
    )
    catalog_domain = any(host == domain or host.endswith("." + domain) for domain in (
        "ndlsearch.ndl.go.jp", "worldcat.org", "books.google.com", "openlibrary.org",
    )) or host.startswith("books.google.")
    bibliographic_path = bool(
        re.search(r"/(?:books?|products?|isbn|catalog)(?:/|$)", parsed.path, re.I)
    )
    url_only_bibliographic_lead = not result_title and bibliographic_path
    stable_canonical_page = bool(parsed.scheme == "https" and host and not parsed.query)
    institution_match = bool(re.search(r"\.(?:ac|edu|go|gov)\.(?:jp|uk|au)$|\.edu$|\.gov$", host))
    low_authority = any(host == domain or host.endswith("." + domain) for domain in (
        "reddit.com", "x.com", "twitter.com", "facebook.com",
    ))
    dictionary = any(host == domain or host.endswith("." + domain) for domain in (
        "wikipedia.org", "kotobank.jp", "weblio.jp", "jitenon.jp",
        "dictionary.com", "merriam-webster.com", "collinsdictionary.com",
        "dictionary.cambridge.org", "vocabulary.com", "thesaurus.com",
        "wordreference.com", "linguee.com",
    ))
    features = {
        "title_exact": title_exact, "title_in_path": title_path,
        "author_match": author_match, "identifier_match": identifier_match,
        "catalog_domain": catalog_domain, "bibliographic_path": bibliographic_path,
        "exact_title_in_page_title": title_exact,
        "exact_title_in_snippet": title_snippet,
        "exact_title_in_page_body": False,
        "author_or_creator_match": author_match,
        "institution_match": institution_match,
        "self_identification_as_work": False,
        "publication_date_evidence": False,
        "stable_canonical_page": stable_canonical_page,
    }
    score = sum((0.55 if title_exact or title_path or url_only_bibliographic_lead else
                 0.50 if title_snippet else 0.0,
                 0.30 if identifier_match else 0.0,
                 0.15 if author_match else 0.0,
                 0.20 if catalog_domain or bibliographic_path or institution_match else 0.0,
                 0.20 if stable_canonical_page and (title_snippet or title_exact) else 0.0))
    threshold = 0.70
    author_is_specific = bool(
        author and author.strip() not in {"自著", "筆者", "本稿筆者"}
    )
    exact_only_author_mismatch = bool(
        author_is_specific and (title_exact or title_snippet or title_path)
        and not author_match and not identifier_match
        and not catalog_domain and not institution_match and not bibliographic_path
    )
    if dictionary:
        reason = "dictionary_or_encyclopedia"
    elif low_authority:
        reason = "social_or_forum_low_authority"
    elif exact_only_author_mismatch:
        reason = "generic_exact_title_author_mismatch"
    elif not any((title_exact, title_snippet, title_path, identifier_match,
                  url_only_bibliographic_lead)):
        reason = "title_no_match"
    elif score < threshold:
        reason = "insufficient_bibliographic_signal"
    else:
        reason = ""
    source_class = (
        "irrelevant_lexical_hit" if dictionary else
        "unrelated_page" if reason in {"title_no_match", "social_or_forum_low_authority"} else
        "bibliographic_source" if catalog_domain else
        "publisher_source" if bibliographic_path and "publish" in host else
        "bookseller_source" if bibliographic_path else
        "insufficient_evidence" if reason else "bibliographic_source"
    )
    return {
        "url": str(url), "domain": host,
        "source_class": source_class,
        "rank": getattr(url, "rank", None),
        "result_title": result_title, "snippet": snippet,
        "deterministic_features": features,
        "llm_label": None, "relevance_score": round(score, 3),
        "threshold": threshold,
        "decision": "reject" if reason else "accept", "reject_reason": reason or None,
    }


def _page_matches(candidate: dict[str, Any], fields: dict[str, Any]) -> bool:
    kind_aliases = {
        "図書": "book", "本": "book", "monograph": "book",
        "scholarlyarticle": "article", "journal-article": "article",
        "論文": "article", "記事": "article",
    }
    candidate_kind = str(candidate.get("record_type") or "").casefold()
    page_kind = str(fields.get("record_type") or "").casefold()
    if candidate_kind and page_kind and (
        kind_aliases.get(candidate_kind, candidate_kind)
        != kind_aliases.get(page_kind, page_kind)
    ):
        return False
    if candidate.get("isbn") and fields.get("isbn"):
        return _normalized(candidate["isbn"]) == _normalized(fields["isbn"])
    if not fields.get("title") or _normalized(candidate.get("title")) != _normalized(
        fields["title"]
    ):
        return False
    return all(
        candidate.get(field)
        and fields.get(field)
        and _normalized(candidate[field]) == _normalized(fields[field])
        for field in ("publisher", "year")
    )


def _inspect_page_evidence(page: dict[str, Any], title: str, author: str) -> None:
    """Record page-body evidence without treating a search snippet as bibliography."""
    if page.get("status") != "retrieved":
        return
    if page.get("reused_phase_e_page_evidence") and page.get("page_evidence"):
        return
    fields = dict(page.get("structured_metadata") or {})
    parsed_url = urllib.parse.urlsplit(str(page.get("url") or ""))
    host = str(parsed_url.hostname or "")
    catalog_page = any(host == domain or host.endswith("." + domain) for domain in (
        "ndlsearch.ndl.go.jp", "worldcat.org", "openlibrary.org", "books.google.com",
    )) or bool(re.search(r"(?:^|\.)opac\.", host))
    established_book = bool(
        fields.get("isbn") or fields.get("publication_statement_raw")
        or fields.get("record_type") in {"book", "article", "chapter"}
    )
    body = str(page.get("metadata_excerpt") or "")
    page_title = str(page.get("title") or "")
    title_key = _normalized(title)
    title_in_page_title = bool(title_key and title_key in _normalized(page_title))
    title_in_body = bool(title_key and title_key in _normalized(body))
    page_title_start = page_title.strip().casefold()
    title_prefix = title.strip().casefold()
    title_remainder = page_title_start[len(title_prefix):].lstrip() if (
        title_prefix and page_title_start.startswith(title_prefix)
    ) else ""
    self_identifying_title = bool(
        title_prefix and page_title_start.startswith(title_prefix)
        and (not title_remainder or title_remainder[0] in "-|｜—–:：")
    )
    explicit_creator = re.search(
        r"(?:著者|執筆者|作者|制作者|Author)\s*[:：]\s*([^\s、,，|]{2,40})",
        body, re.I,
    )
    explicit_date = re.search(
        r"(?:公開日|掲載日|発行日|投稿日|Published)\s*[:：]?\s*((?:18|19|20)\d{2})",
        body, re.I,
    )
    body_fields: dict[str, Any] = dict(page.get("body_extracted_fields") or {})
    explicit_translator = re.search(
        r"(?:訳者|翻訳|訳)\s*[:：／/]\s*([一-龥]{2,8})", body
    )
    if title_in_body and explicit_translator:
        body_fields["translators"] = [explicit_translator.group(1)]
        body_fields["responsibility_raw"] = [f"{explicit_translator.group(1)} 訳"]
    if self_identifying_title and not (catalog_page or established_book):
        fields.setdefault("title", title)
        if not fields.get("authors") and explicit_creator:
            body_fields["authors"] = [explicit_creator.group(1)]
        if not fields.get("year") and explicit_date:
            body_fields["year"] = explicit_date.group(1)
        if not fields.get("record_type") and title_in_body:
            body_fields["record_type"] = "nonstandard_publication"
    fields.update(body_fields)
    page["structured_metadata"] = fields
    page["body_extracted_fields"] = body_fields
    page["page_evidence"] = {
        "exact_title_in_page_title": title_in_page_title,
        "exact_title_in_page_body": title_in_body,
        "author_or_creator_match": bool(
            author and _normalized(author) in _normalized(body + str(fields.get("authors") or ""))
        ),
        "institution_match": bool(re.search(r"\.(?:ac|edu|go|gov)\.", host)),
        "self_identification_as_work": bool(not catalog_page and not established_book and (
            (self_identifying_title and title_in_body)
            or (
                fields.get("record_type") == "nonstandard_publication"
                and _normalized(fields.get("title")) == title_key
                and fields.get("authors") and fields.get("year")
            )
        )),
        "publication_date_evidence": bool(fields.get("year")),
        "stable_canonical_page": bool(
            urllib.parse.urlsplit(str(page.get("url") or "")).scheme == "https"
            and not urllib.parse.urlsplit(str(page.get("url") or "")).query
        ),
    }


def _enrich_candidates_from_pages(
    candidates: list[dict[str, Any]], pages: list[dict[str, Any]]
) -> None:
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        evidence = dict(candidate.get("field_evidence") or {})
        conflicts = dict(candidate.get("metadata_conflicts") or {})
        known_urls = {str(candidate.get("url") or "")}
        for page in pages:
            if page.get("status") != "retrieved" or is_researcher_contribution_listing(page):
                continue
            fields = page.get("structured_metadata") or {}
            if not isinstance(fields, dict) or not (
                _page_matches(candidate, fields)
                or str(page.get("url") or "") in known_urls
                or (
                    page.get("target_fields")
                    and _normalized(fields.get("title")) == _normalized(candidate.get("title"))
                )
            ):
                continue
            if fields.get("responsibility_raw"):
                sources = list(candidate.get("responsibility_sources") or [])
                sources.append(
                    {
                        "provider": (
                            "ndl_search"
                            if "ndlsearch.ndl.go.jp" in str(page.get("url"))
                            else "web"
                        ),
                        "url": page.get("url"),
                        "responsibility_raw": fields["responsibility_raw"],
                        "responsibility_source": "exact_record_detail",
                    }
                )
                candidate["responsibility_sources"] = sources
            if fields.get("original_title_raw"):
                candidate.setdefault("original_title_audit", []).append(
                    {
                        "original_title_raw": fields["original_title_raw"],
                        "original_title": fields.get("original_title"),
                        "source_url": page.get("url"),
                        "parser_rule": fields.get("original_title_parser_rule"),
                    }
                )
            for field, value in fields.items():
                if page.get("target_fields") and field not in {
                    *page["target_fields"], "responsibility_raw",
                }:
                    continue
                if field in {
                    "title", "page_language", "responsibility_raw",
                    "publication_statement_raw",
                    "original_title_raw", "original_title_parser_rule", "page_site_name",
                } or value in (
                    None, "", []
                ):
                    continue
                if field in {"authors", "translators", "editors"} and not fields.get(
                    "responsibility_raw"
                ):
                    continue
                if field == "original_title_relation_verified" and value is True:
                    candidate[field] = True
                    continue
                is_ndl = "ndlsearch.ndl.go.jp" in str(page.get("url"))
                proof = {
                    "value": value,
                    "source_url": page.get("url"),
                    "source_type": (
                        "provider_detail_record" if is_ndl else "web_structured_metadata"
                    ),
                    "provider": "ndl_search" if is_ndl else "web",
                    "provider_record_id": candidate.get("provider_record_id") if is_ndl else None,
                    "raw_value": str(value),
                    "parser": "parse_ndl_detail_metadata" if is_ndl else "parse_page_metadata",
                    "evidence_text": str(value),
                    "confidence": 0.9,
                    "verification_status": (
                        "corroborated" if candidate.get(field) == value else "candidate"
                    ),
                }
                evidence.setdefault(field, []).append(proof)
                current = candidate.get(field)
                ndl_mixed_creator = (
                    is_ndl and field == "authors"
                    and candidate.get("responsibility_source") == "ndl_search:dc_creator_mixed"
                    and fields.get("responsibility_raw")
                )
                ndl_country_code = (
                    is_ndl and field == "publication_place"
                    and re.fullmatch(r"[A-Z]{2,3}", str(current or ""))
                )
                language_equivalent = (
                    field == "publication_language"
                    and {str(current or "").casefold(), str(value).casefold()}
                    in ({"en", "eng"}, {"ja", "jpn"}, {"fr", "fre"})
                )
                publisher_role_equivalent = (
                    field == "publisher" and (
                        _normalized(_primary_publisher(current))
                        == _normalized(_primary_publisher(value))
                        or (
                            len(_publisher_brand(current)) >= 6
                            and _publisher_brand(current) == _publisher_brand(value)
                        )
                    )
                )
                people_equivalent = (
                    field in {"authors", "translators", "editors"}
                    and _people_values_equivalent(current, value)
                )
                if ndl_mixed_creator or ndl_country_code:
                    # RSS dc:creator mixes translators into authors, while the
                    # exact NDL detail labels roles. Preserve raw RSS above.
                    candidate[field] = value
                elif current in (None, "", []):
                    candidate[field] = value
                elif not (
                    language_equivalent or publisher_role_equivalent or people_equivalent
                ) and (
                    _normalized(current) != _normalized(value)
                ):
                    conflicts[field] = [current, value]
        candidate["field_evidence"] = evidence
        candidate["metadata_conflicts"] = conflicts


def enrich_from_web_v29(
    result: dict[str, Any],
    *,
    search: Callable[[str], list[str]],
    fetch: Callable[[str], dict[str, Any]],
    seed_urls: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Enrich identity-matched candidates, preserving conflicts and search limits."""
    title = str(result.get("title") or "").strip()
    if not title:
        return result
    candidates = result.get("candidate_matches") or []
    author = str(result.get("author") or "").strip()
    hypotheses = result.get("search_hypotheses") or {}
    translation_plan = _translation_side_discovery_plan(
        title, author, candidates, hypotheses,
    )
    translation_queries = [item["query"] for item in translation_plan["queries"]]
    relation_queries = list(dict.fromkeys([
        *translation_queries, *_relation_queries(title, candidates),
    ]))
    result["translation_side_discovery_triggered"] = translation_plan["triggered"]
    result["translation_side_discovery_queries"] = translation_plan["queries"]
    result["translation_side_discovery_ready_foreign_count"] = translation_plan[
        "ready_foreign_count"
    ]
    result["translation_side_discovery_ready_japanese_count_before"] = translation_plan[
        "ready_japanese_count"
    ]
    if translation_plan["triggered"]:
        result["relation_resolution_mode"] = "translation_side_discovery"
    unresolved = not candidates or any(
        canonicalize_reference(item)["v29_render_status"] != "render_ready"
        for item in candidates[:4]
        if isinstance(item, dict)
    )
    if not unresolved and not relation_queries:
        return result
    targeted_queries, targeted_fields = _targeted_field_queries(title, candidates, author)
    reused_targeted_fields = _reuse_targeted_evidence(candidates, targeted_fields)
    alternative_titles = tuple(hypotheses.get("titles") or [])
    alternative_authors = tuple(hypotheses.get("authors") or [])
    self_authored_discovery_queries = (
        [
            f'"{title}"',
            f'{title} site:jglobal.jst.go.jp',
            f'{title} site:researchmap.jp',
            f'{title} site:booth.pm',
            title,
        ]
        if result.get("is_self_authored") and not candidates else []
    )
    queries = list(
        dict.fromkeys(
            [
                *self_authored_discovery_queries,
                *relation_queries,
                *targeted_queries,
                f'"{title}"',
                *( [f'"{title}" {author}']
                   if author and author not in {"自著", "筆者", "本稿筆者"} else [] ),
                *(f'"{title}" {name}' for name in alternative_authors[:2]),
                *(f'"{value}" {alternative_authors[0] if alternative_authors else author}'
                  for value in alternative_titles[:2]),
                *( [f'"{title}" ISBN', f'"{title}" 訳 出版社']
                   if not targeted_queries else [] ),
                *( [f'"{title}" BOOTH 頒布'] if result.get("is_self_authored") else [] ),
            ]
        )
    )[:8]
    result["targeted_fields_requested"] = targeted_fields
    exact_links: list[str] = list(dict.fromkeys(seed_urls))[:12]
    pages: list[dict[str, Any]] = [fetch(url) for url in exact_links]
    for page in pages:
        _inspect_page_evidence(page, title, author)
    result["exact_record_enrichment_attempts"] = [
        {
            "url": page.get("url"), "status": page.get("status"),
            "source_class": (
                "bibliographic_source"
                if "ndlsearch.ndl.go.jp" in str(page.get("url")) else "institutional_source"
            ),
            "detail_fields": sorted((page.get("structured_metadata") or {}).keys()),
        }
        for page in pages
    ]
    _enrich_candidates_from_pages(candidates, pages)
    exact_ready = bool(candidates) and all(
        canonicalize_reference(item)["v29_render_status"] == "render_ready"
        for item in candidates[:4]
        if isinstance(item, dict)
    )
    if exact_ready and not relation_queries:
        result["targeted_fields_recovered"] = reused_targeted_fields
        result["web_search_attempts"] = []
        result["web_relevance_rejected_count"] = 0
        result["web_evidence_pages"] = [
            {key: value for key, value in page.items() if key != "metadata_excerpt"}
            for page in pages
        ]
        return result
    links: list[str] = []
    attempts: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    rejected_count = 0
    relation_pages: list[dict[str, Any]] = []
    relation_resolved = False
    title_first_resolved = False
    exact_title_query = f'"{title}"'
    for query in queries:
        found = search(query)[:8]
        evaluated = [
            {"query": query, **_web_relevance_decision(
                url, title, author, candidates, title_hypotheses=alternative_titles,
            )}
            for url in found
        ]
        decisions.extend(evaluated)
        accepted = [item["url"] for item in evaluated if item["decision"] == "accept"]
        rejected_count += len(found) - len(accepted)
        attempts.append({
            "query": query,
            "purpose": (
                "self_authored_source_discovery"
                if query in self_authored_discovery_queries
                else "translation_side_discovery" if query in translation_queries
                else "relation_resolution" if query in relation_queries
                else "targeted_field_enrichment" if query in targeted_queries
                else "metadata_enrichment"
            ),
            "target_fields": targeted_fields if query in targeted_queries else [],
            "result_count": len(found), "relevant_count": len(accepted),
        })
        links.extend(accepted)
        if query in relation_queries:
            for url in accepted[:3]:
                if url in exact_links or any(page.get("url") == url for page in relation_pages):
                    continue
                page = fetch(url)
                if query in targeted_queries:
                    page["target_fields"] = targeted_fields
                _inspect_page_evidence(page, title, author)
                relation_pages.append(page)
                fields = page.get("structured_metadata") or {}
                if (
                    page.get("status") == "retrieved"
                    and not is_researcher_contribution_listing(page)
                    and _normalized(fields.get("title")) == _normalized(title)
                    and fields.get("original_title_relation_verified") is True
                    and any(
                        _normalized(fields.get("original_title")) == _normalized(item.get("title"))
                        for item in candidates if isinstance(item, dict)
                    )
                ):
                    relation_resolved = True
                    break
        if relation_resolved:
            break
        if query == exact_title_query:
            for url in accepted[:3]:
                if url in exact_links or any(page.get("url") == url for page in relation_pages):
                    continue
                page = fetch(url)
                _inspect_page_evidence(page, title, author)
                relation_pages.append(page)
                fields = page.get("structured_metadata") or {}
                if (
                    page.get("status") == "retrieved"
                    and not is_researcher_contribution_listing(page)
                    and _normalized(fields.get("title")) == _normalized(title)
                    and (
                        fields.get("isbn")
                        or (fields.get("publisher") and fields.get("year"))
                        or (
                            fields.get("record_type") == "nonstandard_publication"
                            and fields.get("authors") and fields.get("year")
                            and fields.get("publication_language")
                        )
                    )
                ):
                    title_first_resolved = True
                    break
        if title_first_resolved:
            break
    result["web_relevance_rejected_count"] = rejected_count
    result["generic_exact_title_author_mismatch_rejected"] = sum(
        item.get("reject_reason") == "generic_exact_title_author_mismatch"
        for item in decisions
    )
    result["lexical_source_rejected"] = sum(
        item.get("reject_reason") == "dictionary_or_encyclopedia" for item in decisions
    )
    result["false_bibliographic_accept_count"] = sum(
        item.get("decision") == "accept"
        and item.get("reject_reason") in {
            "generic_exact_title_author_mismatch", "dictionary_or_encyclopedia",
        }
        for item in decisions
    )
    result["web_relevance_decisions"] = decisions
    general_pages: list[dict[str, Any]] = list(relation_pages)
    targeted_urls = {
        str(item.get("url") or "")
        for item in decisions
        if item.get("query") in targeted_queries and item.get("decision") == "accept"
    }
    for url in list(dict.fromkeys(links))[:6]:
        if url not in exact_links and not any(page.get("url") == url for page in general_pages):
            page = fetch(url)
            if url in targeted_urls:
                page["target_fields"] = targeted_fields
            general_pages.append(page)
    for page in general_pages:
        if str(page.get("url") or "") in targeted_urls:
            page["target_fields"] = targeted_fields
    result["relation_query_stopped_on_evidence"] = relation_resolved
    result["title_first_web_search_count"] = sum(
        item["query"] == exact_title_query for item in attempts
    )
    result["exact_title_web_hit_count"] = sum(
        item["query"] == exact_title_query
        and (item["deterministic_features"]["exact_title_in_page_title"]
             or item["deterministic_features"]["exact_title_in_snippet"])
        for item in decisions
    )
    pages.extend(general_pages)
    for page in pages:
        _inspect_page_evidence(page, title, author)
    result["web_page_fetch_count"] = len(general_pages)
    result["web_search_attempts"] = attempts
    result["web_evidence_pages"] = [
        {key: value for key, value in page.items() if key != "metadata_excerpt"} for page in pages
    ]
    _enrich_candidates_from_pages(candidates, general_pages)
    recovered = sorted(set(reused_targeted_fields) | {
        field for field in targeted_fields
        if any(not canonicalize_reference(item).get("v29_required_missing_fields")
               or field not in canonicalize_reference(item)["v29_required_missing_fields"]
               for item in candidates if isinstance(item, dict))
    })
    result["targeted_fields_recovered"] = recovered
    contribution_hypotheses = _contribution_hypotheses(title, decisions, pages)
    result["container_hypotheses"] = contribution_hypotheses
    confirmed_hypotheses = [
        item for item in contribution_hypotheses
        if item.get("source_kind") in {"fetched_page_body", "fetched_contribution_item"}
    ]
    cached_pages = {
        str(page.get("url") or ""): page for page in pages
        if page.get("reused_phase_e_page_evidence") is True
        and (page.get("page_evidence") or {}).get("exact_title_in_page_body") is True
        and not is_researcher_contribution_listing(page)
        and _page_identifies_contribution(page, title)
    }
    cached_confirmed = []
    for item in contribution_hypotheses:
        if (
            item.get("source_kind") == "search_hypothesis_only"
            and str(item.get("source_url") or "") in cached_pages
            and item.get("author") and item.get("container_title") and item.get("year")
        ):
            cached_confirmed.append({
                **item,
                "source_kind": "cached_page_body_with_search_hypothesis",
                "verification_status": "corroborated_against_prior_page_fetch",
            })
    corroborated_groups: dict[tuple[str, str, str], set[str]] = {}
    for item in cached_confirmed:
        key = (
            _normalized(item.get("author")), _normalized(item.get("container_title")),
            str(item.get("year") or ""),
        )
        corroborated_groups.setdefault(key, set()).add(str(item.get("source_url") or ""))
    confirmed_hypotheses.extend(
        item for item in cached_confirmed
        if len(corroborated_groups.get((
            _normalized(item.get("author")), _normalized(item.get("container_title")),
            str(item.get("year") or ""),
        ), set())) >= 2
    )
    container_values = [
        str(item.get("container_title") or "") for item in contribution_hypotheses
        if item.get("container_title")
    ]
    if not candidates and container_values and not title_first_resolved:
        container = max(set(container_values), key=container_values.count)
        container_query = f'"{title}" "{container}"'
        if not any(item["query"] == container_query for item in attempts):
            found = search(container_query)[:8]
            evaluated = [
                {"query": container_query, **_web_relevance_decision(
                    url, title, author, candidates, title_hypotheses=alternative_titles,
                )}
                for url in found
            ]
            decisions.extend(evaluated)
            attempts.append({
                "query": container_query, "purpose": "container_confirmation",
                "result_count": len(found),
                "relevant_count": sum(item["decision"] == "accept" for item in evaluated),
            })
            extra_pages = []
            for item in evaluated:
                if item["decision"] != "accept" or any(
                    page.get("url") == item["url"] for page in pages
                ):
                    continue
                page = fetch(item["url"])
                _inspect_page_evidence(page, title, author)
                extra_pages.append(page)
            pages.extend(extra_pages)
            extra_hypotheses = _contribution_hypotheses(title, evaluated, extra_pages)
            contribution_hypotheses.extend(extra_hypotheses)
            confirmed_hypotheses.extend(
                item for item in extra_hypotheses
                if item.get("source_kind") in {
                    "fetched_page_body", "fetched_contribution_item"
                }
            )
            result["container_hypotheses"] = contribution_hypotheses
            result["web_page_fetch_count"] += len(extra_pages)
    result["contribution_listing_audit"] = [
        {
            "url": page.get("url"),
            "item_count": len(page.get("contribution_items") or []),
            "matching_item_count": page.get("contribution_match_count", 0),
        }
        for page in pages if is_researcher_contribution_listing(page)
    ]
    result["container_hypothesis_count"] = len(contribution_hypotheses)
    complete_hypotheses = [
        item for item in confirmed_hypotheses
        if all(item.get(field) for field in ("author", "container_title", "year"))
    ]
    work_signatures = {
        (
            _normalized(item.get("canonical_title") or title),
            _normalized(item.get("author")),
            _normalized(item.get("container_title")),
            str(item.get("year")),
        )
        for item in complete_hypotheses
    }
    # A bibliography candidate must have all fields from one work. Conflicting
    # records cannot be settled by taking the longest title or majority fields.
    selected = max(
        complete_hypotheses,
        key=lambda item: (
            item.get("source_kind") == "fetched_contribution_item",
            bool(item.get("pages")), bool(item.get("record_id")),
        ),
    ) if complete_hypotheses and len(work_signatures) == 1 else None
    result["container_confirmed_count"] = int(selected is not None)
    aggregate: dict[str, Any] = {}
    aggregate_provenance: dict[str, list[dict[str, Any]]] = {}
    if selected:
        for field in ("author", "container_title", "year", "pages", "canonical_title"):
            value = str(selected.get(field) or "").strip()
            if not value:
                continue
            aggregate[field] = value
            aggregate_provenance[field] = [{
                "value": value, "source_url": selected.get("source_url"),
                "source_type": selected.get("source_kind"),
                "raw_value": selected.get("raw_evidence"),
                "parser": selected.get("extractor"), "confidence": 0.8,
                "verification_status": selected.get("verification_status"),
            }]
    new_candidates = 0
    if (
        not candidates and aggregate.get("author") and aggregate.get("container_title")
        and aggregate.get("year") and selected
    ):
        url = str(selected.get("source_url") or "")
        record_id = str(selected.get("record_id") or url)
        canonical_title = aggregate.get("canonical_title") or title
        identity = hashlib.sha256(
            f"{canonical_title}\x1f{aggregate['author']}\x1f{aggregate['container_title']}".encode()
        ).hexdigest()[:20]
        candidate = {
            "candidate_id": f"web-contribution:{identity}", "provider": "web_aggregate",
            "provider_record_id": record_id, "url": url, "title": canonical_title,
            "mention_title": title,
            "record_type": "nonstandard_contribution",
            "bibliography_kind": "nonstandard_contribution",
            "publication_language": "ja", "authors": [aggregate["author"]],
            "container_title": aggregate["container_title"], "year": aggregate["year"],
            "pages": aggregate.get("pages"), "pages_required": False,
            "pages_status": "present" if aggregate.get("pages") else "unknown_allowed",
            "field_evidence": aggregate_provenance,
            "field_provenance": aggregate_provenance,
            "responsibility_sources": [{
                "provider": "web_aggregate", "url": url,
                "responsibility_raw": [f"{aggregate['author']} 著"],
                "responsibility_source": "web_explicit_author_metadata",
                "authors": [aggregate["author"]],
            }],
            "metadata_conflicts": {}, "identity_score": 0.9,
            "container_evidence_aggregation": [selected],
        }
        candidates.append(candidate)
        result["nonstandard_candidate_count"] = 1
        result["canonical_full_title_applied"] = int(canonical_title != title)
        new_candidates = 1
    else:
        result["nonstandard_candidate_count"] = 0
        result["canonical_full_title_applied"] = 0
    for page in pages:
            if page.get("status") != "retrieved" or is_researcher_contribution_listing(page):
                continue
            fields = page.get("structured_metadata") or {}
            if not isinstance(fields, dict) or _normalized(fields.get("title")) != _normalized(
                title
            ):
                continue
            nonstandard_complete = (
                fields.get("record_type") == "nonstandard_publication"
                and fields.get("authors") and fields.get("year")
                and fields.get("publication_language")
                and (page.get("page_evidence") or {}).get("self_identification_as_work")
            )
            if not fields.get("isbn") and not (
                fields.get("publisher") and fields.get("year")
            ) and not nonstandard_complete:
                continue
            if candidates and not (
                relation_queries
                and fields.get("original_title_relation_verified") is True
                and any(
                    _normalized(fields.get("original_title")) == _normalized(item.get("title"))
                    for item in candidates if isinstance(item, dict)
                )
            ):
                continue
            url = str(page.get("url") or "")
            if any(str(item.get("url") or "") == url for item in candidates):
                continue
            identity = hashlib.sha256(url.encode()).hexdigest()[:20]
            candidate = {
                **{
                    key: value for key, value in fields.items()
                    if key not in {"page_language", "page_site_name"}
                },
                "candidate_id": f"web:{identity}",
                "provider": "web",
                "provider_record_id": url,
                "url": url,
                "identity_score": 1.0,
                "responsibility_sources": [
                    {
                        "provider": "web",
                        "url": url,
                        "responsibility_raw": [
                            f"{name} 著" for name in fields.get("authors") or []
                        ],
                        "responsibility_source": "web_explicit_author_metadata",
                        "authors": fields.get("authors") or [],
                        "translators": fields.get("translators") or [],
                        "editors": fields.get("editors") or [],
                    }
                ],
                "field_evidence": {
                    field: [
                        {
                            "value": value,
                            "source_url": url,
                            "source_type": (
                                "web_page_body" if field in page.get("body_extracted_fields", {})
                                else "web_structured_metadata"
                            ),
                            "provider": "web",
                            "evidence_text": str(value),
                            "confidence": 0.9,
                            "verification_status": "candidate",
                        }
                    ]
                    for field, value in fields.items()
                    if field not in {"page_language", "page_site_name"}
                },
                "metadata_conflicts": {},
                "web_evidence_features": page.get("page_evidence") or {},
            }
            if candidate.get("record_type") == "nonstandard_publication" and fields.get(
                "page_site_name"
            ):
                candidate["container_title"] = fields["page_site_name"]
            if fields.get("original_title_relation_verified") is True:
                candidate["original_title_audit"] = [{
                    "original_title": fields.get("original_title"),
                    "source_url": url,
                    "parser_rule": "ndl_original_title_section" if "ndlsearch.ndl.go.jp" in url
                    else "publisher_explicit_relation",
                }]
            candidates.append(candidate)
            new_candidates += 1
    if new_candidates:
        result["candidate_matches"] = candidates
        result["work_identity_status"] = "matched"
        result["resolution_status"] = "web_candidates_retained_unconfirmed"
        result["edition_status"] = "unconfirmed"
        result["consulted_status"] = "unconfirmed"
        result["final_revision_blocked"] = True
        result["conditional_proposal_allowed"] = True
        result["expanded_reference_entries"] = [
            {
                **candidate,
                "reference_id": f"web-manifestation:{candidate['candidate_id']}",
                "source_reference_id": result.get("reference_id"),
                "original_text": result.get("original_text"),
                "consulted_status": "unconfirmed",
                "reference_role": "unconfirmed_manifestation",
                "final_revision_blocked": True,
                "parse_status": "parsed",
            }
            for candidate in candidates
        ]
    ready_japanese_after = [
        item for item in candidates if isinstance(item, dict)
        and str(item.get("publication_language") or "").casefold() in {"ja", "jpn", "jp"}
        and canonicalize_reference(item)["v29_render_status"] == "render_ready"
    ]
    result["japanese_manifestation_found"] = bool(
        translation_plan["triggered"] and any(
            isinstance(item, dict)
            and str(item.get("publication_language") or "").casefold() in {"ja", "jpn", "jp"}
            for item in candidates
        )
    )
    result["japanese_manifestation_ready"] = bool(ready_japanese_after)
    result["translation_side_discovery_ready_japanese_count_after"] = len(
        ready_japanese_after
    )
    attempted_queries = {str(item.get("query") or "") for item in attempts}
    result["translation_side_discovery_completed"] = bool(
        translation_plan["triggered"]
        and all(query in attempted_queries for query in translation_queries)
    )
    result["web_search_attempts"] = attempts
    result["web_relevance_decisions"] = decisions
    result["web_evidence_pages"] = [
        {key: value for key, value in page.items() if key != "metadata_excerpt"}
        for page in pages
    ]
    return result
