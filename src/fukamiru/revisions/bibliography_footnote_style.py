"""Bibliographic formatting for generated manuscript footnotes.

This profile is intentionally separate from the terminal bibliography style.
It follows the field order observed in the user-provided LaTeX footnotes while
leaving punctuation as a renderer detail.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree as ET

from fukamiru.revisions.bibliography_style import source_language_names

FOOTNOTE_REFERENCE_RULESET: dict[str, Any] = {
    "id": "latex_matiere_footnote_order_v1",
    "source": "user-provided matiere.tex footnote examples",
    "punctuation_is_normative": False,
    "repeat_citation_policy": "full_form_until_citation_history_is_explicit",
    "field_order": {
        "book": [
            "creator",
            "title",
            "translator_or_editor",
            "edition",
            "publication_place_for_non_japanese",
            "publisher",
            "year",
            "locator",
        ],
        "article": [
            "creator",
            "contribution_title",
            "container_title",
            "volume_and_issue",
            "year",
            "locator",
        ],
        "chapter": [
            "creator",
            "contribution_title",
            "editor",
            "container_title",
            "publication_place_for_non_japanese",
            "publisher",
            "year",
            "locator",
        ],
        "nonstandard_contribution": [
            "creator",
            "contribution_title",
            "editor",
            "container_title",
            "publisher_if_known",
            "year",
            "locator",
        ],
    },
}


@dataclass(frozen=True)
class StyledFootnoteReference:
    spans: tuple[tuple[str, bool], ...]
    supported: bool
    review_reasons: tuple[str, ...] = ()

    @property
    def text(self) -> str:
        return "".join(value for value, _italic in self.spans)

    def write_into(self, element: ET.Element) -> None:
        element[:] = []
        element.text = None
        for value, italic in self.spans:
            if italic:
                child = ET.SubElement(element, "em")
                child.text = value
            elif len(element):
                element[-1].tail = (element[-1].tail or "") + value
            else:
                element.text = (element.text or "") + value


def _value(entry: dict[str, Any], name: str) -> str:
    return str(entry.get(name) or "").strip()


def _language(entry: dict[str, Any]) -> str:
    raw = _value(entry, "publication_language").casefold()
    return {
        "japanese": "ja",
        "jpn": "ja",
        "ja-jp": "ja",
        "french": "fr",
        "fra": "fr",
        "fre": "fr",
        "fr-fr": "fr",
        "english": "en",
        "eng": "en",
        "en-us": "en",
        "en-gb": "en",
        "german": "de",
        "deu": "de",
        "ger": "de",
        "russian": "ru",
        "rus": "ru",
    }.get(raw, raw)


def _kind(entry: dict[str, Any]) -> str:
    raw = _value(entry, "record_type").casefold()
    if raw in {"", "book_or_journal", "article_poem_or_chapter"}:
        raw = _value(entry, "bibliography_kind").casefold()
    if raw in {"book", "monograph", "edited-book", "図書", "本"}:
        return "book"
    if raw in {"article", "journal-article", "journalarticle", "論文", "記事"}:
        return "article"
    if raw in {"chapter", "book-chapter", "book-section", "booksection", "章"}:
        return "chapter"
    if raw in {"nonstandard_contribution", "doujin_contribution", "web_contribution"}:
        return "nonstandard_contribution"
    return "unknown"


def _names(entry: dict[str, Any], role: str) -> list[str]:
    if localized := source_language_names(entry, role):
        return localized
    people = entry.get("bibliography_people") or []
    selected = [
        _person_name(person)
        for person in people
        if isinstance(person, dict) and person.get("role") == role
    ]
    if selected:
        return [name for name in selected if name]
    field = {
        "author": "authors",
        "translator": "translators",
        "editor": "editors",
    }[role]
    values = entry.get(field) or []
    if isinstance(values, str):
        values = [values]
    return [
        str(value).strip() for value in values
        if str(value).strip() and not (role == "author" and str(value).strip() == "自著")
    ]


def _person_name(person: dict[str, Any]) -> str:
    authority = _value(person, "authority_raw")
    literal = _value(person, "literal")
    family = _value(person, "family")
    given = _value(person, "given")
    profile = _value(person, "name_profile")
    if profile == "japanese_native":
        return family + given
    if profile in {"western_katakana", "western_initials_katakana"}:
        return authority or "・".join(value for value in (given, family) if value)
    if profile == "western_latin":
        return " ".join(value for value in (given, family) if value)
    return literal or authority or " ".join(value for value in (given, family) if value)


def _translator_groups(entry: dict[str, Any]) -> list[tuple[list[str], str]]:
    people = entry.get("bibliography_people") or []
    if not people:
        names = _names(entry, "translator")
        return [(names, "訳")] if names else []
    groups: list[tuple[list[str], str]] = []
    for label, subtype in (("監訳", "supervising_translator"), ("訳", None)):
        names = [
            _person_name(person)
            for person in people
            if isinstance(person, dict)
            and person.get("role") == "translator"
            and (
                person.get("role_subtype") == subtype
                if subtype
                else person.get("role_subtype") != "supervising_translator"
            )
        ]
        names = [name for name in names if name]
        if names:
            groups.append((names, label))
    return groups


def _pages(locator: str, *, language: str) -> str:
    value = locator.strip()
    if not value:
        return ""
    normalized = re.sub(
        r"\s*[-–—〜～]\s*",
        "-",
        value.translate(str.maketrans("０１２３４５６７８９", "0123456789")),
    )
    if not re.fullmatch(r"\d+(?:-\d+)?", normalized):
        return normalized
    if language == "ja":
        return normalized + "頁"
    return ("pp. " if "-" in normalized else "p. ") + normalized


def _join(spans: list[list[tuple[str, bool]]], *, separator: str) -> tuple[tuple[str, bool], ...]:
    result: list[tuple[str, bool]] = []
    for part in spans:
        if not part:
            continue
        if result:
            result.append((separator, False))
        result.extend(part)
    if result:
        result.append(("。" if separator == "、" else ".", False))
    return tuple(result)


def _plain(value: str) -> list[tuple[str, bool]]:
    return [(value, False)] if value else []


def format_footnote_reference(
    entry: dict[str, Any],
    *,
    locator: str = "",
) -> StyledFootnoteReference:
    """Format one full-form footnote citation without bibliography reuse.

    Field order is the contract: creator, work/contribution, responsibility,
    edition, publication facts, date, and the cited locator.
    """

    language = _language(entry)
    kind = _kind(entry)
    title = _value(entry, "title")
    authors = _names(entry, "author")
    if not title:
        return StyledFootnoteReference((), False, ("title_unconfirmed",))
    if not authors:
        return StyledFootnoteReference((), False, ("author_unconfirmed",))
    if language not in {"ja", "fr", "en", "de", "ru"}:
        return StyledFootnoteReference((), False, ("edition_language_unconfirmed",))
    if kind not in {"book", "article", "chapter", "nonstandard_contribution"}:
        if (
            entry.get("provisional_bibliography_review") is True
            and _value(entry, "reference_id")
            and entry.get("resolution_status") == "resolved"
            and entry.get("bibliography_people")
        ):
            separator = "・" if language == "ja" else "; "
            label = (
                "［書誌種別・刊行情報要確認］"
                if language == "ja"
                else " [bibliography details to verify]"
            )
            text = separator.join(authors) + "「" + title + "」" + label
            return StyledFootnoteReference(
                ((text + ("。" if language == "ja" else "."), False),),
                True,
                ("record_kind_unconfirmed",),
            )
        return StyledFootnoteReference((), False, ("record_kind_unconfirmed",))
    if not entry.get("bibliography_people"):
        return StyledFootnoteReference((), False, ("person_name_order_unconfirmed",))

    publisher = _value(entry, "publisher")
    place = _value(entry, "publication_place")
    year = _value(entry, "year")
    container = _value(entry, "container_title")
    volume = _value(entry, "volume")
    issue = _value(entry, "issue")
    edition = _value(entry, "edition")
    cited_pages = _pages(locator or _value(entry, "pages"), language=language)
    editors = _names(entry, "editor")
    missing = []
    if not year:
        missing.append("year_unconfirmed")
    if kind == "book" and not publisher:
        missing.append("publisher_unconfirmed")
    if kind in {"article", "chapter", "nonstandard_contribution"} and not container:
        missing.append("container_title_unconfirmed")
    if missing and kind == "nonstandard_contribution" and (
        entry.get("provisional_bibliography_review") is True
        and _value(entry, "reference_id")
        and entry.get("resolution_status") == "resolved"
        and entry.get("bibliography_people")
    ):
        separator = "・" if language == "ja" else "; "
        label = (
            "［書誌種別・刊行情報要確認］"
            if language == "ja" else " [bibliography details to verify]"
        )
        text = separator.join(authors) + "「" + title + "」" + label
        return StyledFootnoteReference(
            ((text + ("。" if language == "ja" else "."), False),),
            True,
            ("record_kind_unconfirmed",),
        )
    if missing:
        return StyledFootnoteReference((), False, tuple(missing))

    if language == "ja":
        creator = "・".join(authors)
        fields: list[list[tuple[str, bool]]] = []
        if kind == "book":
            fields.append(_plain(f"{creator}『{title}』"))
        else:
            fields.append(_plain(f"{creator}「{title}」"))
            if kind in {"chapter", "nonstandard_contribution"} and editors:
                fields.append(_plain("・".join(editors) + "編"))
            fields.append(_plain(f"『{container}』"))
        for names, label in _translator_groups(entry):
            fields.append(_plain("・".join(names) + label))
        if edition:
            fields.append(_plain(edition))
        if volume and issue:
            fields.append(_plain(f"{volume}({issue})"))
        elif volume or issue:
            fields.append(_plain(volume or issue))
        if publisher:
            fields.append(_plain(publisher))
        fields.append(_plain(year if year.endswith("年") else year + "年"))
        if cited_pages:
            fields.append(_plain(cited_pages))
        return StyledFootnoteReference(_join(fields, separator="、"), True)

    creator = "; ".join(authors)
    fields = [_plain(creator)]
    if kind == "book":
        fields.append([(title, True)])
    else:
        quote = f"« {title} »" if language == "fr" else f"“{title}”"
        fields.append(_plain(quote))
        if kind in {"chapter", "nonstandard_contribution"} and editors:
            fields.append(_plain("; ".join(editors)))
        fields.append([(container, True)])
    for names, label in _translator_groups(entry):
        fields.append(_plain("; ".join(names) + f" ({label})"))
    if edition:
        fields.append(_plain(edition))
    if volume and issue:
        fields.append(_plain(f"{volume}({issue})"))
    elif volume or issue:
        fields.append(_plain(volume or issue))
    if place:
        fields.append(_plain(place))
    if publisher:
        fields.append(_plain(publisher))
    fields.append(_plain(year))
    if cited_pages:
        fields.append(_plain(cited_pages))
    return StyledFootnoteReference(_join(fields, separator=", "), True)
