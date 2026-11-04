"""The user-supplied bibliography house style for V29 manuscript references.

The supported rules are specified in docs/specification/bibliography-style.md.
Unknown languages and
record types retain their supplied fields without claiming a house-style match.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree as ET


@dataclass(frozen=True)
class StyledReference:
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


def _names(entry: dict[str, Any], name: str, singular: str = "") -> list[str]:
    values = entry.get(name) or []
    if isinstance(values, str):
        values = [values]
    result = [str(value).strip() for value in values if str(value).strip()]
    if not result and singular and _value(entry, singular):
        result = [_value(entry, singular)]
    if name in {"authors", "selected_authors"}:
        result = [value for value in result if value != "自著"]
    return result


def _language(entry: dict[str, Any]) -> str:
    # `language` on an extracted reference is the manuscript's language.
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


def source_language_names(entry: dict[str, Any], role: str) -> list[str]:
    """Prefer an explicit Japanese display name for a Japanese manifestation.

    A provider's romanized access point remains in ``bibliography_people`` for
    identity auditing, but must not replace a Japanese name supplied by the source.
    """

    if role != "author" or _language(entry) != "ja":
        return []
    name = _value(entry, "author")
    provenance = entry.get("provenance") or {}
    author_provenance = provenance.get("author") if isinstance(provenance, dict) else {}
    source_explicit = isinstance(author_provenance, dict) and (
        author_provenance.get("kind") == "source_explicit"
    )
    if source_explicit and name != "自著" and re.search(r"[ぁ-んァ-ヶ一-龠々〆ヵヶ]", name):
        return [name]
    return []


def _kind(entry: dict[str, Any]) -> str:
    raw = _value(entry, "record_type").casefold()
    if raw in {"", "book_or_journal", "article_poem_or_chapter"}:
        raw = _value(entry, "bibliography_kind").casefold()
    if raw in {"book", "monograph", "edited-book", "図書", "本"}:
        return "book"
    if raw in {
        "article",
        "journal-article",
        "journalarticle",
        "論文",
        "記事",
    }:
        return "article"
    if raw in {"chapter", "book-chapter", "book-section", "booksection", "章"}:
        return "chapter"
    if raw in {"presentation", "conference-presentation", "conference-paper", "talk", "発表"}:
        return "presentation"
    if raw in {
        "web_publication",
        "self_published_work",
        "unpublished_work",
        "institutional_publication",
        "doujin_publication",
        "other_nonstandard_publication",
        "nonstandard_publication",
    }:
        return "nonstandard_publication"
    if raw in {
        "nonstandard_contribution",
        "doujin_contribution",
        "web_contribution",
        "self_published_contribution",
    }:
        return "nonstandard_contribution"
    return "unknown"


def _ja(value: str) -> str:
    return value.replace("，", "、").replace("．", "。")


def _pages(value: str) -> str:
    return re.sub(
        r"\s*[-–—〜～]\s*",
        "-",
        value.translate(str.maketrans("０１２３４５６７８９", "0123456789")),
    )


_EDITION_LABELS = ("新装版", "改訂版", "増補版", "新版", "新訳版", "文庫版")


def _edition_label(value: str) -> str:
    normalized = re.sub(r"^[\s:：、，・]+|[\s:：、，・]+$", "", value)
    return normalized if normalized in _EDITION_LABELS else ""


def _parts_join(parts: list[list[tuple[str, bool]]], separator: str, end: str) -> StyledReference:
    spans: list[tuple[str, bool]] = []
    for part in parts:
        if not part:
            continue
        if spans:
            spans.append((separator, False))
        spans.extend(part)
    if spans and not spans[-1][0].endswith(end):
        spans.append((end, False))
    return StyledReference(tuple(spans), True)


def _ja_join(parts: list[list[tuple[str, bool]]]) -> StyledReference:
    spans: list[tuple[str, bool]] = []
    for part in parts:
        if not part:
            continue
        if spans:
            spans.append((" " if spans[-1][0].endswith((".", "．", "。")) else "、", False))
        spans.extend(part)
    if spans:
        spans.append((" 。" if spans[-1][0].endswith((".", "．", "。")) else "。", False))
    return StyledReference(tuple(spans), True)


def _plain(value: str) -> list[tuple[str, bool]]:
    return [(value, False)] if value else []


def format_reference(entry: dict[str, Any]) -> StyledReference:
    """Return visible text plus italic spans, without changing metadata."""
    language = _language(entry)
    kind = _kind(entry)
    title = _value(entry, "title")
    if not title:
        return _unconfirmed(entry, "title_unconfirmed")

    reasons = _review_reasons(entry)
    if (
        entry.get("provisional_bibliography_review") is True
        and reasons == ("record_kind_unconfirmed",)
        and _value(entry, "reference_id")
        and entry.get("resolution_status") == "resolved"
    ):
        people = entry.get("bibliography_people") or []
        authors = source_language_names(entry, "author") or [
            _person_name(person)
            for person in people
            if isinstance(person, dict) and person.get("role") == "author"
        ]
        authors = [name for name in authors if name]
        if authors:
            separator = "・" if language == "ja" else "; "
            label = (
                "［書誌種別・刊行情報要確認］"
                if language == "ja"
                else " [bibliography details to verify]"
            )
            text = separator.join(authors) + "「" + title + "」" + label
            return StyledReference(
                ((text + ("。" if language == "ja" else "."), False),),
                True,
                reasons,
            )
    if reasons:
        return _unconfirmed(entry, *reasons)

    people = entry.get("bibliography_people")
    if people:
        authors = [_person_name(p) for p in people if p.get("role") == "author"]
        translators = [_person_name(p) for p in people if p.get("role") == "translator"]
        editors = [_person_name(p) for p in people if p.get("role") == "editor"]
    else:
        authors = _names(entry, "selected_authors") or _names(entry, "authors", "author")
        translators = _names(entry, "translators")
        editors = _names(entry, "editors")
    authors = source_language_names(entry, "author") or authors
    container = _value(entry, "container_title")
    year = _value(entry, "year")
    pages = _pages(_value(entry, "pages"))
    volume = _value(entry, "volume")
    issue = _value(entry, "issue")
    publisher = _value(entry, "publisher")
    place = _value(entry, "publication_place")
    edition = _value(entry, "edition")
    series = _value(entry, "series")
    title_edition_label = next(
        (
            label
            for label in _EDITION_LABELS
            if re.search(rf"(?:^|[\s:：、，・]){re.escape(label)}$", title)
        ),
        "",
    )
    if _edition_label(volume) and _edition_label(volume) == _edition_label(edition):
        volume = ""
    if title_edition_label:
        if _edition_label(volume) == title_edition_label:
            volume = ""
        if _edition_label(edition) == title_edition_label:
            edition = ""
    recognized = {
        value
        for value in (container, volume, issue, publisher, place, edition, series, year, pages)
        if value
    }
    unparsed = []
    seen_labels = {
        label
        for label in (title_edition_label, _edition_label(volume), _edition_label(edition))
        if label
    }
    for value in entry.get("unparsed_fields") or []:
        raw = str(value).strip()
        label = _edition_label(raw)
        if not raw or raw in recognized or (label and label in seen_labels):
            continue
        unparsed.append(raw)
        if label:
            seen_labels.add(label)

    if language == "ja" and kind in {"book", "article", "chapter"}:
        name = "・".join(_ja(value) for value in authors)
        title = _ja(title)
        container = _ja(container)
        fields: list[list[tuple[str, bool]]] = []
        if kind in {"article", "chapter"}:
            fields.append(_plain(f"{name}「{title}」"))
            if kind == "chapter" and editors:
                fields.append(_plain("・".join(_ja(value) for value in editors) + "編"))
            if container:
                fields.append(_plain(f"『{container}』"))
            if volume and issue:
                fields.append(_plain(f"{volume}({issue})"))
            elif volume or issue:
                fields.append(_plain(volume or issue))
            if kind == "chapter" and publisher:
                fields.append(_plain(publisher))
        else:
            fields.append(_plain(f"{name}『{title}』"))
            if translators:
                fields.append(_plain("・".join(_ja(value) for value in translators) + "訳"))
            if editors:
                fields.append(_plain("・".join(_ja(value) for value in editors) + "編"))
            for value in (volume, edition, publisher, series):
                if value:
                    fields.append(_plain(value))
        fields.extend(_plain(value) for value in unparsed)
        if year:
            fields.append(_plain(_ja(year) if year.endswith("年") else f"{year}年"))
        if pages:
            fields.append(_plain(f"{pages}頁"))
        return _ja_join(fields)

    if language == "ja" and kind in {
        "nonstandard_publication",
        "nonstandard_contribution",
    }:
        name = "・".join(_ja(value) for value in authors)
        fields = [_plain(f"{name}「{_ja(title)}」")]
        if container:
            fields.append(_plain(f"『{_ja(container)}』"))
        if pages:
            fields.append(_plain(f"{pages}頁"))
        if year:
            fields.append(_plain(f"{year}年" if not year.endswith("年") else year))
        if _value(entry, "url"):
            fields.append(_plain(_value(entry, "url")))
        return _ja_join(fields)

    if language in {"fr", "en", "de", "ru"} and kind in {"book", "article", "chapter"}:
        if kind in {"article", "chapter"} and language not in {"fr", "en"}:
            return _unconfirmed(entry, "style_not_defined_for_language_or_kind")
        name = "; ".join(authors)
        fields = [_plain(name)] if name else []
        if kind == "book":
            fields.append([(title, True)])
            fields.extend(
                _plain(value)
                for value in [
                    *editors,
                    *translators,
                    volume,
                    edition,
                    place,
                    publisher,
                    series,
                    *unparsed,
                ]
                if value
            )
        else:
            quoted = f"« {title} »" if language == "fr" else f"“{title}”"
            fields.append(_plain(quoted))
            if editors:
                fields.append(_plain("; ".join(editors)))
            if container:
                fields.append([(container, True)])
            if volume and issue:
                fields.append(_plain(f"{volume}({issue})"))
            elif volume or issue:
                fields.append(_plain(volume or issue))
            if kind == "chapter":
                fields.extend(_plain(value) for value in (place, publisher) if value)
            fields.extend(_plain(value) for value in unparsed)
        if year:
            fields.append(_plain(year))
        if pages:
            fields.append(_plain(f"pp. {pages}" if "-" in pages else f"p. {pages}"))
        return _parts_join(fields, ", ", ".")

    if language == "fr" and kind == "presentation":
        names = []
        for author in authors:
            if "," in author:
                surname, given = author.split(",", 1)
                names.append(f"{given.strip()} {surname.strip()}")
            else:
                names.append(author)
        name = "; ".join(names)
        fields = [_plain(name), _plain(f"« {title} »")]
        fields.extend(
            _plain(_value(entry, field))
            for field in ("conference_name", "venue", "institution", "date")
            if _value(entry, field)
        )
        if not _value(entry, "date") and year:
            fields.append(_plain(year))
        return _parts_join(fields, ", ", ".")

    return _unconfirmed(entry, "style_not_defined_for_language_or_kind")


def _person_name(person: dict[str, Any]) -> str:
    profile = person.get("name_profile")
    family = str(person.get("family") or "").strip()
    given = str(person.get("given") or "").strip()
    if profile == "japanese_native":
        return family + given
    if profile == "western_katakana":
        return family + "、" + given
    if profile == "western_latin":
        return family + ", " + given
    if profile == "western_initials_katakana":
        return family + "、" + given
    return str(person.get("literal") or "").strip()


def _review_reasons(entry: dict[str, Any]) -> tuple[str, ...]:
    reasons: list[str] = []
    if not _value(entry, "publication_language"):
        reasons.append("edition_language_unconfirmed")
    if _kind(entry) == "unknown":
        reasons.append("record_kind_unconfirmed")
    if entry.get("provider") == "ndl_search" and not entry.get("bibliography_people"):
        reasons.append("ndl_responsibility_unconfirmed")
    elif entry.get("provider") and not entry.get("bibliography_people"):
        reasons.append("provider_name_order_unconfirmed")
    people = entry.get("bibliography_people")
    if people:
        for person in people:
            if not isinstance(person, dict) or person.get("role") not in {
                "author",
                "translator",
                "editor",
                "supervisor",
                "corporate_author",
            }:
                reasons.append("person_role_unconfirmed")
                continue
            profile = person.get("name_profile")
            if profile in {
                "japanese_native",
                "western_katakana",
                "western_latin",
                "western_initials_katakana",
            }:
                if not person.get("family") or not person.get("given"):
                    reasons.append("person_name_unconfirmed")
            elif (
                profile != "literal"
                or not person.get("literal")
                or not (
                    person.get("literal_confirmed")
                    or (
                        person.get("responsibility_role_confirmed") is True
                        and person.get("person_display_name_confirmed") is True
                    )
                )
            ):
                reasons.append("person_name_unconfirmed")
            if person.get("role") in {"supervisor", "corporate_author"}:
                reasons.append("person_role_style_unconfirmed")
    else:
        names = _names(entry, "selected_authors") or _names(entry, "authors", "author")
        names += _names(entry, "translators") + _names(entry, "editors")
        if len(_names(entry, "selected_authors") or _names(entry, "authors", "author")) > 1:
            reasons.append("multiple_author_roles_unconfirmed")
        if any("・" in name for name in names):
            reasons.append("person_boundary_unconfirmed")
        if any(
            re.search(r"\b(?:18|19|20)\d{2}\s*[-–—]", name)
            or re.search(r"(?:[、,，]\s*)?(?:著|訳|翻訳|編|監修)$", name)
            for name in names
        ):
            reasons.append("person_authority_or_role_in_name")
    for field in ("publication_place", "edition", "volume"):
        value = _value(entry, field)
        if field == "publication_place" and value.casefold() in {
            "jp",
            "jpn",
            "uk",
            "us",
            "fr",
            "au",
            "ja",
        }:
            reasons.append("country_code_in_place")
        if field in {"edition", "volume"} and re.fullmatch(
            r"\s*:?\s*(?:pbk|hbk|pb|hb|cloth)\s*", value, re.I
        ):
            reasons.append("binding_code_in_" + field)
    return tuple(dict.fromkeys(reasons))


def _unconfirmed(entry: dict[str, Any], *reasons: str) -> StyledReference:
    """A review row is never constructed from unverified provider fields."""
    raw = _value(entry, "original_text")
    return StyledReference(((raw, False),) if raw else (), False, tuple(reasons))
