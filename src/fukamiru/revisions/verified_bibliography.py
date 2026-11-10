"""Operator-verified bibliographic repairs, separate from author review judgments."""

from __future__ import annotations

import re
import unicodedata
from copy import deepcopy
from typing import Any
from urllib.parse import urlsplit

from fukamiru.errors import FukamiruError
from fukamiru.revisions.bibliography_footnote_style import format_footnote_reference
from fukamiru.revisions.bibliography_style import format_reference


def _identity(value: str) -> str:
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", value)).casefold()


def verified_book_reference(
    reference: dict[str, Any], correction: dict[str, Any], *, checked_at: str,
) -> dict[str, Any]:
    """Apply one primary-source check to an unambiguous RAG filename reference.

    The caller is an operator who has checked the publisher/catalogue listing.
    A review answer or an LLM-overreach checkbox never invokes this operation.
    """
    fields = {"reference_id", "title", "authors", "translators", "publisher", "year",
              "isbn", "publication_language", "source_kind", "source_url"}
    if set(correction) != fields or reference.get("reference_id") != correction["reference_id"]:
        raise FukamiruError("Verified book correction has another reference or field set")
    try:
        url = urlsplit(str(correction["source_url"]))
    except ValueError as exc:
        raise FukamiruError("Invalid primary-source URL") from exc
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or correction["source_kind"] not in {"publisher", "library_catalog"}):
        raise FukamiruError("A primary publisher/catalogue HTTPS source is required")
    isbn = re.sub(r"[-\s]", "", str(correction["isbn"]))
    if (not re.fullmatch(r"\d{13}", isbn)
            or sum(int(value) * (1 if index % 2 == 0 else 3)
                   for index, value in enumerate(isbn)) % 10):
        raise FukamiruError("Verified book correction needs a valid ISBN-13")
    authors, translators = correction["authors"], correction["translators"]
    if (not isinstance(authors, list) or not authors or not isinstance(translators, list)
            or any(not isinstance(name, str) or not name.strip() or name == "自著"
                   for name in [*authors, *translators])):
        raise FukamiruError("Verified names and responsibility roles are required")
    if (reference.get("provider") != "knowledge_base"
            or format_footnote_reference(reference).review_reasons != ("author_unconfirmed",)):
        raise FukamiruError("Reference is not an author-unconfirmed RAG source")
    old_title = str(reference.get("title") or "")
    identity = _identity(old_title)
    old_isbn = re.sub(r"[-\s]", "", str(reference.get("isbn") or ""))
    same_isbn = old_isbn == isbn
    same_filename = (
        bool(_identity(str(correction["title"])))
        and _identity(str(correction["title"])) in identity
        and str(correction["year"]) in old_title
        and _identity(str(correction["publisher"])) in identity
        and any(_identity(name) in identity for name in authors)
    )
    if (old_isbn and not same_isbn) or not (same_isbn or same_filename):
        raise FukamiruError("Verified book does not match the saved source identity")
    for key in ("publisher", "year"):
        old = str(reference.get(key) or "")
        if old and _identity(old) != _identity(str(correction[key])):
            raise FukamiruError("Verified book conflicts with the saved " + key)
    updated = deepcopy(reference)
    metadata = {key: deepcopy(correction[key]) for key in (
        "title", "authors", "translators", "publisher", "year", "publication_language",
    )}
    metadata.update({
        "isbn": isbn, "author": authors[0], "record_type": "book",
        "bibliography_people": [
            {"role": role, "name_profile": "literal", "literal": name,
             "literal_confirmed": True, "source": correction["source_url"]}
            for role, names in (("author", authors), ("translator", translators))
            for name in names
        ],
    })
    updated.update(metadata)
    updated.update({"source_import_title": reference.get("source_import_title") or old_title,
                    "resolution_status": "resolved", "canonicalization_status": "complete"})
    provenance = deepcopy(reference.get("provenance") or {})
    for field in metadata:
        provenance[field] = {"kind": "retrieved_metadata", "verified_by": "operator",
                             "source": correction["source_kind"],
                             "source_url": correction["source_url"], "checked_at": checked_at}
    updated["provenance"] = provenance
    if not format_footnote_reference(updated).supported or not format_reference(updated).supported:
        raise FukamiruError("Verified book still fails the strict bibliography format contract")
    return updated
