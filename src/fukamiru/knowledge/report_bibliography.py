"""Source-page proofs for report-local bibliographies; never edits corpus records."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fukamiru.authoring.codex import CodexAuthoringAdapter
from fukamiru.errors import FukamiruError
from fukamiru.knowledge.report_sources import ReportCorpus, normalized, quote_key

BIBLIOGRAPHY_STAGE = "knowledge_search_bibliography"
MAX_BIBLIOGRAPHY_REPAIRS = 1
FIELDS = (
    "title",
    "publisher",
    "publication_date",
    "publication_place",
    "publication_title",
    "volume",
    "issue",
    "pages",
    "doi",
    "isbn",
)


class BibliographyUnresolved(FukamiruError):
    """A source's bibliography lacks verifiable proofs, not a provider failure."""


def extract_bibliography(
    source: dict[str, Any],
    corpus: ReportCorpus,
    generate: Callable[..., dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Path]]:
    pages = corpus.bibliography_pages(source)
    if not pages:
        raise BibliographyUnresolved("書誌を照合できる表題紙・奥付・書誌ページがありません。")
    images = corpus.images(source, pages)
    payload = {
        "source_asset_id": source["source_asset_id"],
        "registered": source["bibliography"],
        "pages": [
            {"page_id": p["page_id"], "number": p["number"], "text": p["text"]} for p in pages
        ],
        "attached_image_page_ids_in_order": list(images),
    }
    options = {"image_paths": list(images.values())} if images else {}
    for attempt in range(MAX_BIBLIOGRAPHY_REPAIRS + 1):
        value = generate(BIBLIOGRAPHY_STAGE, payload, **options)
        CodexAuthoringAdapter.validate_result(BIBLIOGRAPHY_STAGE, value)
        try:
            return _validate_bibliography(value, source, pages, images)
        except BibliographyUnresolved as exc:
            if attempt == MAX_BIBLIOGRAPHY_REPAIRS:
                raise
            payload = {**payload, "previous_extraction": value, "validation_feedback": str(exc)}
    raise AssertionError("unreachable bibliography validation")


def _validate_bibliography(
    value: dict[str, Any],
    source: dict[str, Any],
    pages: list[dict[str, Any]],
    images: dict[str, Path],
) -> tuple[dict[str, Any], dict[str, Path]]:
    by_id = {page["page_id"]: page for page in pages}
    proofs = {}
    for field, items in [
        ("authors", value["authors"]),
        *((name, [value[name]]) for name in FIELDS),
    ]:
        for index, proof in enumerate(items):
            if not proof["value"].strip():
                continue
            page = by_id.get(proof["page_id"])
            if page is None or not proof["quote"].strip():
                raise BibliographyUnresolved(f"書誌項目 {field} の原資料参照が不正です。")
            quote = normalized(proof["quote"])
            if proof["method"] == "image":
                if proof["page_id"] not in images:
                    raise BibliographyUnresolved(
                        "添付されていない画像を根拠にした書誌項目があります。"
                    )
            else:
                if not quote or quote_key(proof["quote"]) not in quote_key(page["text"]):
                    raise BibliographyUnresolved(f"書誌項目 {field} の引用が原文にありません。")
            # Natural name order is allowed, expanding an initial without its
            # full-name source quote is not, including image-based proofs.
            tokens = [normalized(t) for t in re.findall(r"[\w]+", proof["value"])]
            if any(token not in quote for token in tokens if token):
                raise BibliographyUnresolved(
                    f"書誌項目 {field} に引用で確認できない値があります。"
                    f"値: {proof['value']} / 引用: {proof['quote']}"
                )
            key = f"authors-{index + 1}" if field == "authors" else field
            proofs[key] = {
                **proof,
                "page_number": page["number"],
                "page_text_sha256": page["text_sha256"],
            }
            if proof["method"] == "image":
                proofs[key]["image_sha256"] = hashlib.sha256(
                    images[proof["page_id"]].read_bytes()
                ).hexdigest()
    result = {name: value[name]["value"].strip() for name in FIELDS}
    result["authors"] = [p["value"].strip() for p in value["authors"] if p["value"].strip()]
    essential = [
        not result["authors"] and "著者",
        not result["title"] and "タイトル",
        not result["publication_date"] and "刊行年",
        not (result["publisher"] or result["publication_title"]) and "出版者・掲載誌",
    ]
    if missing := [field for field in essential if field]:
        raise BibliographyUnresolved("書誌未確定：" + "、".join(missing))
    if not re.fullmatch(r"\d{4}(?:-\d{2}(?:-\d{2})?)?", result["publication_date"]):
        raise BibliographyUnresolved("刊行日は確認できた西暦年またはISO日付が必要です。")
    if result["doi"] and not re.fullmatch(r"10\.\d{4,9}/\S+", result["doi"]):
        raise BibliographyUnresolved("確認済みDOIの形式が不正です。")
    if result["isbn"]:
        isbn = re.sub(r"[-\s]", "", result["isbn"]).upper()
        valid = (
            len(isbn) == 13
            and isbn.isdigit()
            and sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(isbn)) % 10 == 0
        )
        valid |= (
            bool(re.fullmatch(r"\d{9}[\dX]", isbn))
            and sum((10 if c == "X" else int(c)) * (10 - i) for i, c in enumerate(isbn)) % 11 == 0
        )
        if not valid:
            raise BibliographyUnresolved("確認済みISBNの検査数字が不正です。")
    return {
        **result,
        "record_type": value["record_type"],
        "source_asset_id": source["source_asset_id"],
        "corpus_record_id": source["bibliography"].get("id"),
        "content_sha256": source["sha256"],
        "proofs": proofs,
        "verification_pages": [
            {"page_id": p["page_id"], "number": p["number"], "text": p["text"]} for p in pages
        ],
        "verification_status": "pending_audit",
        "unresolved": value["unresolved"],
    }, images
