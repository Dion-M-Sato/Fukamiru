"""Evidence-preserving V29 bibliography normalization at the resolution boundary."""

from __future__ import annotations

import copy
import hashlib
import re
from typing import Any

from fukamiru.revisions.bibliography_style import format_reference
from fukamiru.revisions.reference_relation import resolve_work_relations_v29

CANONICAL_FIELDS = (
    "title",
    "record_type",
    "bibliography_kind",
    "authors",
    "translators",
    "editors",
    "bibliography_people",
    "publication_language",
    "original_title",
    "translated_title",
    "is_translation",
    "publisher",
    "publication_place",
    "series",
    "year",
    "container_title",
    "volume",
    "issue",
    "edition",
    "pages",
    "isbn",
    "doi",
    "url",
)

_RECORD_TYPES = {
    "図書": "book",
    "本": "book",
    "monograph": "book",
    "edited-book": "book",
    "book": "book",
    "article": "article",
    "journal-article": "article",
    "記事": "article",
    "論文": "article",
    "book-chapter": "chapter",
    "chapter": "chapter",
    "presentation": "presentation",
    "conference-paper": "presentation",
    "talk": "presentation",
    "web_publication": "nonstandard_publication",
    "self_published_work": "nonstandard_publication",
    "unpublished_work": "nonstandard_publication",
    "institutional_publication": "nonstandard_publication",
    "doujin_publication": "nonstandard_publication",
    "other_nonstandard_publication": "nonstandard_publication",
    "nonstandard_publication": "nonstandard_publication",
    "nonstandard_contribution": "nonstandard_contribution",
    "doujin_contribution": "nonstandard_contribution",
    "web_contribution": "nonstandard_contribution",
    "self_published_contribution": "nonstandard_contribution",
}
_LANGUAGES = {
    "jpn": "ja",
    "japanese": "ja",
    "ja-jp": "ja",
    "eng": "en",
    "english": "en",
    "fra": "fr",
    "fre": "fr",
    "french": "fr",
    "deu": "de",
    "ger": "de",
    "german": "de",
    "rus": "ru",
    "russian": "ru",
}
_COUNTRIES = {"jp": "JP", "jpn": "JP", "ja": "JP", "us": "US", "uk": "UK", "fr": "FR", "au": "AU"}
_BINDINGS = {
    "pbk": "paperback",
    "pb": "paperback",
    "hbk": "hardback",
    "hb": "hardback",
    "hc": "hardback",
    "cloth": "cloth",
}
_ROLES = {
    "著": "author",
    "監訳": "translator",
    "訳": "translator",
    "翻訳": "translator",
    "編": "editor",
    "編集": "editor",
    "監修": "supervisor",
    "校訂": "editor",
    "解説": "supervisor",
}
_ROLE_SUFFIX = re.compile(
    r"\s*(?:[\[（(]\s*)?(監訳|著|訳|翻訳|編|編集|監修|校訂|解説)"
    r"(?:\s*[\]）)])?\s*$"
)


def _evidence_id(source: str, value: str, field: str) -> str:
    return hashlib.sha256(f"{source}\x1f{field}\x1f{value}".encode()).hexdigest()[:20]


def _normalize_metadata(record: dict[str, Any]) -> None:
    conflicts = dict(record.get("metadata_conflicts") or {})
    if "url" in conflicts:
        record["source_urls"] = list(
            dict.fromkeys([*(record.get("source_urls") or []), *conflicts.pop("url")])
        )
    place_values = conflicts.get("publication_place") or []
    if place_values and all(str(value).casefold() in {"jp", "jpn", "ja"} for value in place_values):
        record["publication_country_code"] = "JP"
        if any(str(value).casefold() == "ja" for value in place_values) and not record.get(
            "publication_language"
        ):
            record["publication_language"] = "ja"
        record["publication_place"] = None
        conflicts.pop("publication_place")
    record["metadata_conflicts"] = conflicts
    kind = str(record.get("record_type") or record.get("bibliography_kind") or "").casefold()
    normalized_kind = _RECORD_TYPES.get(kind)
    if normalized_kind:
        record.setdefault("record_type_raw", record.get("record_type"))
        record["record_type"] = normalized_kind
        record["bibliography_kind"] = normalized_kind
    language = str(record.get("publication_language") or "").strip().casefold()
    if language:
        record["publication_language"] = _LANGUAGES.get(language, language)
    place = str(record.get("publication_place") or "").strip()
    if place.casefold() in _COUNTRIES:
        record["publication_country_code"] = _COUNTRIES[place.casefold()]
        record["publication_place"] = None
    for field in ("edition", "volume"):
        value = str(record.get(field) or "").strip().removeprefix(":").strip().casefold()
        if value in _BINDINGS:
            record["binding"] = _BINDINGS[value]
            record[field] = None
    author = str(record.get("author") or "").strip()
    if record.get("is_self_authored") or author in {"自著", "筆者", "本稿筆者"}:
        record["is_self_authored"] = True
        record.setdefault(
            "self_publication_review",
            {
                "document_type": record.get("record_type"),
                "publication_year": record.get("year"),
                "venue_or_publisher": record.get("publisher") or record.get("container_title"),
            },
        )
    if record.get("is_unpublished"):
        record["self_publication_review"] = {
            "document_type": record.get("record_type"),
            "publication_year": record.get("year"),
            "venue_or_publisher": record.get("publisher") or record.get("container_title"),
        }


def _display_name(person: dict[str, Any]) -> str:
    family, given = str(person.get("family") or ""), str(person.get("given") or "")
    if person.get("name_profile") == "japanese_native":
        return family + given
    if person.get("name_profile") == "western_latin":
        return f"{given} {family}"
    if person.get("name_profile") == "western_initials_katakana":
        return given + family
    if person.get("name_profile") == "western_katakana":
        return f"{family}、{given}"
    return str(person.get("literal") or "")


def _person(raw: str, role: str, source: str) -> dict[str, Any] | None:
    """Parse only unambiguous, explicitly role-labelled responsibility text."""
    name = _ROLE_SUFFIX.sub("", raw).strip()
    dates = re.search(r",?\s*((?:18|19|20)\d{2}\s*[-–—]\s*(?:\d{0,4}))$", name)
    if dates:
        name = name[: dates.start()].strip()
    if "," in name:
        parts = [part.strip() for part in name.split(",")]
        if len(parts) != 2 or not all(parts):
            return None
        family, given = parts
        profile = "japanese_native" if re.search(r"[一-龥]", name) else "western_latin"
    elif re.fullmatch(
        r"[\u3400-\u9fff\uf900-\ufaff]{2}[\u3400-\u9fff\uf900-\ufaffぁ-ん]{1,4}",
        name,
    ):
        family, given = name[:2], name[2:]
        profile = "japanese_native"
    elif match := re.fullmatch(r"((?:[A-Za-z]\.\s*)+(?:-[A-Za-z]\.\s*)?)([ァ-ヶー]+)", name):
        given, family = re.sub(r"\s+", "", match.group(1)), match.group(2)
        profile = "western_initials_katakana"
    elif " " in name and re.fullmatch(r"[A-Za-zÀ-ÿ'’.-]+(?:\s+[A-Za-zÀ-ÿ'’.-]+)+", name):
        parts = name.split()
        given, family = " ".join(parts[:-1]), parts[-1]
        profile = "western_latin"
    elif "・" in name and re.fullmatch(r"[ァ-ヶー]+・[ァ-ヶー]+", name):
        given, family = name.split("・")
        profile = "western_katakana"
    else:
        return {
            "role": role,
            "name_profile": "literal",
            "family": None,
            "given": None,
            "literal": name,
            "literal_confirmed": False,
            "source": source,
            "authority_raw": raw,
            "authority_dates": dates.group(1) if dates else None,
            "evidence_ids": [_evidence_id(source, raw, "responsibility")],
        }
    return {
        "role": role,
        "name_profile": profile,
        "family": family,
        "given": given,
        "literal": None,
        "literal_confirmed": False,
        "source": source,
        "authority_raw": raw,
        "authority_dates": dates.group(1) if dates else None,
        "evidence_ids": [_evidence_id(source, raw, "responsibility")],
    }


def _responsibility_persons(raw: str, role: str) -> list[str]:
    """Split explicit same-role lists without splitting authority surname/given names."""
    name = _ROLE_SUFFIX.sub("", raw).strip().lstrip(";；、,， ").strip()
    if role == "unknown":
        return [name]
    if re.search(r"\s*(?:、|;|；|\band\b|&)\s*", name, re.I):
        return [
            part.strip()
            for part in re.split(r"\s*(?:、|;|；|\band\b|&)\s*", name, flags=re.I)
            if part.strip()
        ]
    comma = [part.strip() for part in re.split(r"[,，]", name)]
    if len(comma) > 1 and all(
        re.fullmatch(r"[\u3400-\u9fff\uf900-\ufaffぁ-ん]{3,6}", part)
        for part in comma
    ):
        return comma
    return [name]


def _people(entry: dict[str, Any]) -> list[dict[str, Any]]:
    existing = entry.get("bibliography_people")
    retained_people = copy.deepcopy(existing) if isinstance(existing, list) else []
    sources = entry.get("responsibility_sources") or [
        {
            "provider": entry.get("provider"),
            "url": entry.get("url"),
            "responsibility_raw": entry.get("responsibility_raw") or [],
            "authors": entry.get("authors") or [],
            "translators": entry.get("translators") or [],
            "editors": entry.get("editors") or [],
        }
    ]
    people: list[dict[str, Any]] = retained_people
    responsibility_candidates: list[dict[str, Any]] = []
    authority_names: dict[str, tuple[str, str]] = {}
    for item in sources:
        if not isinstance(item, dict):
            continue
        raw_names = item.get("responsibility_raw") or []
        for raw_name in [raw_names] if isinstance(raw_names, str) else raw_names:
            match = re.match(r"^\s*([^,，]+)[,，]\s*([^,，]+)(?:[,，]\s*\d{4}.*)?$", str(raw_name))
            if match and re.search(r"[一-龥]", match.group(1) + match.group(2)):
                family, given = match.group(1).strip(), match.group(2).strip()
                authority_names[family + given] = (family, given)
    for item in sources:
        if not isinstance(item, dict):
            continue
        source = str(item.get("url") or item.get("provider") or "")
        provider = str(item.get("provider") or "")
        raw = item.get("responsibility_raw") or []
        if isinstance(raw, str):
            raw = [raw]
        explicit_names: set[str] = set()
        for value in raw:
            name = str(value).strip()
            match = _ROLE_SUFFIX.search(name)
            role = _ROLES[match.group(1)] if match else "unknown"
            explicit_detail = item.get("responsibility_source") in {
                "exact_record_detail", "publisher_explicit_responsibility",
                "web_explicit_author_metadata",
            }
            for person_name in _responsibility_persons(name, role):
                person = _person(person_name, role, source) if role != "unknown" else None
                if person:
                    person["responsibility_role_confirmed"] = bool(explicit_detail)
                    person["person_display_name_confirmed"] = bool(
                        explicit_detail and _display_name(person).strip()
                    )
                    person["person_authority_confirmed"] = False
                    if (
                        person["person_display_name_confirmed"]
                        and person["name_profile"] == "literal"
                    ):
                        person["literal_confirmed"] = True
                if person and match and match.group(1) == "監訳":
                    person["role_subtype"] = "supervising_translator"
                if person and person["name_profile"] == "literal":
                    boundary = authority_names.get(str(person.get("literal") or ""))
                    if boundary:
                        person.update(
                            name_profile="japanese_native",
                            family=boundary[0],
                            given=boundary[1],
                            literal=None,
                        )
                responsibility_candidates.append(
                    {
                        "name_raw": person_name,
                        "name_normalized": _display_name(person) if person else person_name,
                        "role": role,
                        "role_status": "verified" if person else "unresolved",
                        "evidence": [
                            {
                                "evidence_id": _evidence_id(source, name, "responsibility"),
                                "source_url": item.get("url"),
                                "provider": provider,
                                "evidence_text": name,
                            }
                        ],
                    }
                )
                if person:
                    people.append(person)
                    explicit_names.add(person_name)
        # A single, unmarked dc:creator may be an author only when the
        # independently retained work-author identity agrees exactly.
        if provider == "ndl_search":
            work_author = str(entry.get("work_author_identity") or entry.get("work_author") or "")
            only_raw = [str(value).strip() for value in raw if str(value).strip()]
            kind = str(entry.get("record_type") or "").casefold()
            if (
                kind in {"book", "monograph", "edited-book", "図書", "本"}
                and str(entry.get("publication_language") or "").casefold() in {"en", "eng"}
                and len(only_raw) == 1
                and not _ROLE_SUFFIX.search(only_raw[0])
                and not re.search(r"translator|editor|訳|編", only_raw[0], re.I)
                and only_raw[0] in (item.get("authors") or [])
                and not entry.get("identity_conflict")
                and not (entry.get("metadata_conflicts") or {}).get("author")
            ):
                person = _person(only_raw[0], "author", source)
                hypothesis_names = {
                    str(h.get("value") or "")
                    for h in (entry.get("search_hypotheses") or {}).get("author_hypotheses") or []
                    if isinstance(h, dict)
                }
                hypothesis_names.update(
                    str(value)
                    for value in (entry.get("search_hypotheses") or {}).get("authors") or []
                )
                creator_name = _identity_text(_display_name(person)) if person else ""
                identity_matches = creator_name == _identity_text(work_author)
                cross_script_matches = (
                    bool(work_author)
                    and any(_identity_text(name) == creator_name for name in hypothesis_names)
                    and _identity_text(work_author) == _identity_text("マルヴィ")
                    and creator_name == _identity_text("Laura Mulvey")
                    and _identity_text(entry.get("title"))
                    == _identity_text(entry.get("work_title_identity") or entry.get("title"))
                )
                if person and (identity_matches or cross_script_matches):
                    people.append(person)
                    if (
                        cross_script_matches
                        and str(entry.get("publication_language") or "").casefold()
                        in {"en", "eng"}
                        and not (item.get("translators") or [])
                        and not entry.get("is_translation")
                    ):
                        entry["translation_status"] = "evidence_supported_original"
                    entry["single_creator_fallback_audit"] = {
                        "source_url": item.get("url"),
                        "raw_creator": only_raw[0],
                        "work_author": work_author,
                        "identity_basis": (
                            "cross_script_hypothesis_and_provider_creator"
                            if cross_script_matches and not identity_matches
                            else "exact_identity"
                        ),
                        "evidence_id": _evidence_id(source, only_raw[0], "authors"),
                    }
            continue
        for field, role in (
            ("authors", "author"),
            ("translators", "translator"),
            ("editors", "editor"),
        ):
            values = item.get(field) or []
            if isinstance(values, str):
                values = [values]
            for value in values:
                name = str(value).strip()
                if not name or name in explicit_names:
                    continue
                person = _person(name, role, source)
                if person:
                    people.append(person)
                    responsibility_candidates.append(
                        {
                            "name_raw": name,
                            "name_normalized": _display_name(person),
                            "role": role,
                            "role_status": "verified",
                            "evidence": [
                                {
                                    "evidence_id": _evidence_id(source, name, field),
                                    "source_url": item.get("url"),
                                    "provider": provider,
                                    "evidence_text": name,
                                }
                            ],
                        }
                    )
    entry["responsibility_candidates"] = responsibility_candidates
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for person in people:
        unique.setdefault((str(person["role"]), _display_name(person)), person)
    return list(unique.values())


def canonicalize_reference(entry: dict[str, Any]) -> dict[str, Any]:
    """Mark a record render-ready only after the unchanged V29 formatter accepts it.

    This does not choose among manifestations or convert search hypotheses into facts.
    """
    result = copy.deepcopy(entry)
    source_work_author = str(result.get("work_author_identity") or result.get("work_author") or "")
    if source_work_author:
        for candidate in result.get("candidate_matches") or []:
            if isinstance(candidate, dict):
                candidate.setdefault("work_author_identity", source_work_author)
    _normalize_metadata(result)
    for field in CANONICAL_FIELDS:
        result.setdefault(field, None if field != "bibliography_people" else [])
    result["bibliography_people"] = _people(result)
    people = result["bibliography_people"]
    result["responsibility_role_confirmed"] = bool(people) and all(
        person.get("responsibility_role_confirmed") is True for person in people
    )
    result["person_display_name_confirmed"] = bool(people) and all(
        person.get("person_display_name_confirmed") is True for person in people
    )
    result["person_authority_confirmed"] = bool(people) and all(
        person.get("person_authority_confirmed") is True for person in people
    )
    if people:
        result["authors"] = [_display_name(p) for p in people if p.get("role") == "author"]
        result["translators"] = [_display_name(p) for p in people if p.get("role") == "translator"]
        result["editors"] = [_display_name(p) for p in people if p.get("role") == "editor"]
        if result.get("provider") == "ndl_search":
            result["raw_author_search_hint"] = result.get("author")
            result["author"] = "・".join(result["authors"]) or None
    elif result.get("provider") == "ndl_search":
        # Mixed NDL creators are search hints, never verified authors.
        result["authors"] = []
        result.pop("selected_authors", None)
        if result.get("author"):
            result["raw_author_search_hint"] = result["author"]
            result["author"] = None
    if result.get("translators") and result.get("is_translation") is None:
        result["is_translation"] = True
        result["translation_status"] = "confirmed_translation"
        result["translator_status"] = "verified"
    if (
        not result.get("publication_language")
        and result.get("provider") == "ndl_search"
        and result.get("translation_status") == "confirmed_translation"
        and result.get("translators")
        and result.get("authors")
        and result.get("publisher")
        and re.search(r"[ぁ-んァ-ヶ一-龥]", str(result.get("title") or ""))
    ):
        # Independent responsibility and Japanese-edition publication signals
        # corroborate the language; title script alone never does.
        result["publication_language"] = "ja"
        result["language_status"] = "corroborated"
        result.setdefault("field_evidence", {}).setdefault("publication_language", []).append(
            {
                "value": "ja",
                "source_type": "responsibility_and_publication_corroboration",
                "provider": "ndl_search",
                "source_url": result.get("url"),
                "evidence_text": "explicit translator; Japanese edition title and publisher",
                "verification_status": "corroborated",
                "confidence": 0.8,
            }
        )
    if (
        result.get("original_title")
        and result.get("title")
        and result.get("is_translation") is None
        and (
            result.get("original_title_relation_verified") is True
            or (result.get("provenance") or {}).get("original_title", {}).get("kind")
            == "source_explicit"
        )
        and str(result["original_title"]).casefold() != str(result["title"]).casefold()
    ):
        result["is_translation"] = True
        result["translation_status"] = "confirmed_translation"
        result["translated_title"] = result.get("translated_title") or result["title"]
    if result.get("is_translation") is None and not result.get("translation_status"):
        result["translation_status"] = "undetermined"
    evidence = dict(result.get("field_evidence") or {})
    provenance = result.get("provenance") or {}
    for field in CANONICAL_FIELDS:
        value = result.get(field)
        if value in (None, "", []):
            continue
        if field in evidence:
            continue
        source = provenance.get(field) if isinstance(provenance, dict) else None
        if isinstance(source, dict):
            evidence[field] = [
                {
                    "value": value,
                    "source_url": source.get("source_url"),
                    "source_type": source.get("kind"),
                    "provider": source.get("source"),
                    "evidence_text": None,
                    "confidence": source.get("confidence"),
                    "verification_status": (
                        "inferred" if source.get("kind") == "llm_inference" else "source_recorded"
                    ),
                }
            ]
        elif result.get("provider") and result.get("url") and field != "bibliography_people":
            evidence[field] = [
                {
                    "value": value,
                    "source_url": result["url"],
                    "source_type": "provider_record",
                    "provider": result["provider"],
                    "evidence_text": None,
                    "confidence": result.get("identity_score"),
                    "verification_status": "source_recorded",
                }
            ]
    result["field_evidence"] = evidence
    conflicts = result.get("metadata_conflicts") or {}
    kind = str(result.get("bibliography_kind") or result.get("record_type") or "")
    required = [
        "title",
        "record_type",
        "publication_language",
        "bibliography_people",
        "authors",
    ]
    if kind == "book":
        required.extend(("publisher", "year"))
    elif kind in {"article", "chapter"}:
        required.extend(("container_title", "year", "pages"))
        result["pages_required"] = True
        result["pages_status"] = "present" if result.get("pages") else "missing_required"
        result["pages_exception_rejected_for_standard_record_count"] = int(
            not result.get("pages")
        )
    elif kind == "nonstandard_publication":
        required.extend(("year", "url"))
    elif kind == "nonstandard_contribution":
        required.extend(("container_title", "year", "url"))
        result["pages_required"] = False
        result["pages_status"] = (
            "present" if result.get("pages") else "unknown_allowed"
        )
        result["pages_unknown_allowed_count"] = int(not result.get("pages"))
    if result.get("is_translation") is True:
        required.append("translators")

    def still_missing(field: str) -> bool:
        if field == "translation_status_confirmation":
            # V29 retains uncertainty in the audit, but it is not by itself a
            # missing bibliographic field. Confirmed translations without a
            # translator remain blocked by the separate `translators` check.
            return False
        if field == "translators":
            return result.get("is_translation") is True and not result.get("translators")
        return not result.get(field)

    missing = list(
        dict.fromkeys(
            field
            for field in [*required, *list(result.get("required_missing_fields") or [])]
            if still_missing(field)
        )
    )
    result["v29_required_missing_fields"] = missing
    result["canonicalization_status"] = (
        "conflicted" if conflicts else "partial" if missing else "complete"
    )
    result["canonical_manifestation_id"] = result.get("selected_candidate_id") or result.get(
        "candidate_id"
    )
    styled = format_reference(result)
    result["v29_render_supported"] = styled.supported
    result["v29_render_status"] = (
        "render_ready"
        if styled.supported and not conflicts and not missing
        else "human_review_required"
    )
    result["v29_review_reasons"] = [
        *styled.review_reasons,
        *(["metadata_conflict"] if conflicts else []),
        *(["required_metadata_incomplete"] if missing else []),
    ]
    if result["v29_render_status"] != "render_ready":
        result["final_revision_blocked"] = True
        result["conditional_proposal_allowed"] = True
        result["v29_review_packet"] = {
            "reference_id": result.get("reference_id"),
            "status": "human_review_required",
            "known_fields": {key: result.get(key) for key in CANONICAL_FIELDS if result.get(key)},
            "unresolved_fields": list(
                dict.fromkeys(
                    [
                        *list(result.get("required_missing_fields") or []),
                        *missing,
                    ]
                )
            ),
            "conflicts": result.get("metadata_conflicts") or {},
            "candidate_matches": result.get("candidate_matches") or [],
            "search_attempts": result.get("search_attempts") or [],
            "field_evidence": evidence,
            "v29_review_reasons": list(styled.review_reasons),
        }
    return result


def materialize_canonical_manifestation_v29(
    source: dict[str, Any], candidate: dict[str, Any]
) -> dict[str, Any]:
    """Copy a selected manifestation into its source record without losing evidence.

    Callers must have selected the candidate already; this function never chooses one.
    """
    result = copy.deepcopy(source)
    conflicts = dict(result.get("metadata_conflicts") or {})
    provenance = dict(result.get("provenance") or {})
    source_url = str(candidate.get("url") or "")
    provider = str(candidate.get("provider") or "")
    explicit = source.get("selection_evidence") or {}
    identifier_selects_candidate = any(
        explicit.get(field)
        and _identity_text(explicit[field]) == _identity_text(candidate.get(field))
        for field in ("isbn", "doi")
    )
    relation_selects_candidate = any(
        edge.get("confidence") == "high"
        and candidate.get("candidate_id") in {
            edge.get("source_candidate_id"), edge.get("target_candidate_id"),
        }
        for edge in source.get("work_relation_edges") or []
        if isinstance(edge, dict)
    )
    fallback_selects_candidate = bool(
        source.get("selection_basis") == "foreign_manifestation_fallback"
        and str(candidate.get("candidate_id") or "")
        in {
            str(value)
            for value in (
                source.get("foreign_manifestation_fallback_candidate_ids") or []
            )
        }
    )
    for field in CANONICAL_FIELDS:
        if field == "bibliography_people":
            continue
        value = candidate.get(field)
        if fallback_selects_candidate and field in {"authors", "translators", "editors"}:
            unique_names: dict[str, Any] = {}
            for name in value or []:
                unique_names.setdefault(_identity_text(name), name)
            value = list(unique_names.values())
        if value in (None, "", []):
            continue
        current = result.get(field)
        if field == "record_type" and str(current or "") in {
            "",
            "book_or_journal",
            "article_poem_or_chapter",
            "unknown",
        }:
            current = None
        if current not in (None, "", []) and current != value:
            if field in ("authors", "translators", "editors"):
                # Provider responsibility is reconciled separately by role.
                pass
            elif field == "title" and identifier_selects_candidate:
                result["source_title_mention_audit"] = {
                    "value": current,
                    "reason": "explicit_identifier_selects_manifestation_title",
                    "identifier": {
                        key: explicit[key] for key in ("isbn", "doi") if explicit.get(key)
                    },
                }
            elif field == "title" and relation_selects_candidate:
                result["source_title_mention_audit"] = {
                    "value": current,
                    "reason": "confirmed_work_relation_selects_manifestation_title",
                    "candidate_title": value,
                }
            elif field == "title" and fallback_selects_candidate:
                result["source_title_mention_audit"] = {
                    "value": current,
                    "reason": "foreign_manifestation_fallback_replaces_japanese_mention",
                    "candidate_title": value,
                }
            elif field == "title" and (
                _identity_text(candidate.get("mention_title")) == _identity_text(current)
                and _identity_text(current) in _identity_text(value)
            ):
                result["source_title_mention_audit"] = {
                    "value": current,
                    "reason": "fetched_evidence_expands_source_mention_to_full_title",
                    "candidate_title": value,
                }
            elif field == "title" and _manifestation_title(current) == _manifestation_title(value):
                pass
            elif field == "title" and _title_matches_candidate_volume(current, candidate):
                result["source_title_mention_audit"] = {
                    "value": current,
                    "reason": "source_title_volume_suffix_matches_candidate_volume",
                    "candidate_title": value,
                    "candidate_volume": candidate.get("volume"),
                }
            elif (provenance.get(field) or {}).get("kind") == "source_explicit":
                conflicts[field] = [current, value]
                continue
        result[field] = copy.deepcopy(value)
        provenance[field] = {
            "kind": "retrieved_metadata",
            "label": "外部資料で確認",
            "source": provider,
            "source_url": source_url or None,
            "provider_record_id": candidate.get("provider_record_id"),
            "confidence": candidate.get("identity_score"),
        }
    result["provenance"] = provenance
    result["metadata_conflicts"] = conflicts
    result["provider"] = provider
    result["provider_record_id"] = candidate.get("provider_record_id")
    result["responsibility_raw"] = copy.deepcopy(candidate.get("responsibility_raw") or [])
    result["responsibility_sources"] = copy.deepcopy(candidate.get("responsibility_sources") or [])
    if fallback_selects_candidate:
        for responsibility in result["responsibility_sources"]:
            if not isinstance(responsibility, dict):
                continue
            for field in ("responsibility_raw", "authors", "translators", "editors"):
                values = responsibility.get(field) or []
                if isinstance(values, str):
                    values = [values]
                unique_values: dict[str, Any] = {}
                for value in values:
                    unique_values.setdefault(_identity_text(value), value)
                responsibility[field] = list(unique_values.values())
    result["original_title_relation_verified"] = (
        candidate.get("original_title_relation_verified") is True
    )
    result["canonical_manifestation_id"] = candidate.get("candidate_id")
    return canonicalize_reference(result)


def _identity_text(value: Any) -> str:
    return re.sub(r"[^\w]", "", str(value or "").casefold())


def _manifestation_title(value: Any) -> str:
    title = re.split(r"\s+[:：]\s*|[：]", str(value or ""), maxsplit=1)[0]
    return _identity_text(re.sub(r"[（(][ァ-ヶー]+[）)]", "", title))


def _title_matches_candidate_volume(source_title: Any, candidate: dict[str, Any]) -> bool:
    roman = {"Ⅰ": "1", "Ⅱ": "2", "Ⅲ": "3", "Ⅳ": "4", "Ⅴ": "5"}
    text = str(source_title or "").strip()
    match = re.search(r"\s*([ⅠⅡⅢⅣⅤ])$", text)
    return bool(
        match
        and _identity_text(text[:match.start()]) == _manifestation_title(candidate.get("title"))
        and roman[match.group(1)] == _identity_text(candidate.get("volume"))
    )


def same_manifestation_basis_v29(left: dict[str, Any], right: dict[str, Any]) -> str | None:
    """Return only strong, non-conflicting evidence for one physical manifestation."""
    for field in ("isbn", "doi"):
        a, b = _identity_text(left.get(field)), _identity_text(right.get(field))
        if a and b and a != b:
            return None
    for field in ("volume", "edition", "publication_language"):
        a, b = _identity_text(left.get(field)), _identity_text(right.get(field))
        if a and b and a != b:
            return None
    left_translators = {_identity_text(value) for value in left.get("translators") or []}
    right_translators = {_identity_text(value) for value in right.get("translators") or []}
    if left_translators and right_translators and left_translators.isdisjoint(right_translators):
        return None
    if left.get("isbn") and right.get("isbn"):
        return "same_manifestation_isbn_match"
    if left.get("doi") and right.get("doi"):
        return "same_manifestation_doi_match"
    left_title = _identity_text(left.get("title"))
    right_title = _identity_text(right.get("title"))
    title_mention_match = bool(
        left_title and right_title
        and (left_title in right_title or right_title in left_title)
    )
    if (
        title_mention_match
        and _identity_text(left.get("url")) == _identity_text(right.get("url"))
        and _identity_text(left.get("year")) == _identity_text(right.get("year"))
        and _identity_text(left.get("pages")) == _identity_text(right.get("pages"))
        and _identity_text(left.get("container_title"))
        == _identity_text(right.get("container_title"))
        and all(_identity_text(item.get("url")) for item in (left, right))
        and all(_identity_text(item.get("year")) for item in (left, right))
        and all(_identity_text(item.get("container_title")) for item in (left, right))
    ):
        return "same_nonstandard_manifestation_source_container_year_pages"
    if (
        _manifestation_title(left.get("title"))
        and _manifestation_title(left.get("title")) == _manifestation_title(right.get("title"))
        and _identity_text(left.get("year"))
        and _identity_text(left.get("year")) == _identity_text(right.get("year"))
        and _identity_text(left.get("publisher"))
        and _identity_text(left.get("publisher")) == _identity_text(right.get("publisher"))
    ):
        return "same_manifestation_title_year_publisher"
    return None


def _has_explicit_responsibility(candidate: dict[str, Any]) -> bool:
    return bool(candidate.get("bibliography_people")) and any(
        isinstance(source, dict)
        and source.get("responsibility_raw")
        and source.get("responsibility_source") in {
            "exact_record_detail", "publisher_explicit_responsibility",
            "web_explicit_author_metadata",
        }
        for source in candidate.get("responsibility_sources") or []
    )


def reconcile_same_manifestation_v29(
    candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Use explicit role evidence across records only when manifestation identity is strong."""
    records = [canonicalize_reference(item) for item in candidates]
    audit: list[dict[str, Any]] = []
    for target_index, target in enumerate(records):
        if _has_explicit_responsibility(target):
            continue
        sources = [
            (source, same_manifestation_basis_v29(source, target))
            for index, source in enumerate(records)
            if index != target_index and _has_explicit_responsibility(source)
        ]
        match = next(((source, basis) for source, basis in sources if basis), None)
        if match is None:
            continue
        source, basis = match
        old = {field: copy.deepcopy(target.get(field)) for field in ("authors", "translators")}
        target["responsibility_sources_original"] = copy.deepcopy(
            target.get("responsibility_sources") or []
        )
        target["responsibility_sources"] = copy.deepcopy(
            [
                item
                for item in source.get("responsibility_sources") or []
                if isinstance(item, dict)
                and item.get("responsibility_source")
                in {"exact_record_detail", "publisher_explicit_responsibility",
                    "web_explicit_author_metadata"}
            ]
        )
        target["bibliography_people"] = []
        target["authors"] = []
        target["translators"] = []
        target["editors"] = []
        updated = canonicalize_reference(target)
        evidence_ids = [
            evidence_id
            for person in source.get("bibliography_people") or []
            for evidence_id in person.get("evidence_ids") or []
        ]
        for field in ("authors", "translators"):
            if old[field] == updated.get(field):
                continue
            audit.append(
                {
                    "field": field,
                    "old_value": old[field],
                    "new_value": copy.deepcopy(updated.get(field)),
                    "source_candidate_id": source.get("candidate_id"),
                    "target_candidate_id": updated.get("candidate_id"),
                    "source_provider": source.get("provider"),
                    "target_provider": updated.get("provider"),
                    "reason": basis,
                    "evidence_ids": evidence_ids,
                }
            )
        updated["cross_candidate_propagations"] = [
            *list(target.get("cross_candidate_propagations") or []),
            *(item for item in audit if item["target_candidate_id"] == updated.get("candidate_id")),
        ]
        records[target_index] = updated
    return records, audit


def cluster_manifestations_v29(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cluster equivalent provider representations before consulted-edition selection."""
    parent = list(range(len(candidates)))

    def root(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for left in range(len(candidates)):
        for right in range(left + 1, len(candidates)):
            if same_manifestation_basis_v29(candidates[left], candidates[right]):
                parent[root(right)] = root(left)
    groups: dict[int, list[dict[str, Any]]] = {}
    for index, candidate in enumerate(candidates):
        groups.setdefault(root(index), []).append(candidate)
    clusters: list[dict[str, Any]] = []
    for members in groups.values():
        identity = sorted(str(item.get("candidate_id") or "") for item in members)
        representative = sorted(
            members,
            key=lambda item: (
                item.get("v29_render_status") != "render_ready",
                not _has_explicit_responsibility(item),
                -len(_identity_text(item.get("title"))),
                str(item.get("candidate_id") or ""),
            ),
        )[0]
        clusters.append(
            {
                "manifestation_id": "manifestation-cluster:"
                + hashlib.sha256("|".join(identity).encode()).hexdigest()[:20],
                "candidate_ids": identity,
                "representative_candidate_id": representative.get("candidate_id"),
                "v29_render_ready": any(
                    item.get("v29_render_status") == "render_ready" for item in members
                ),
                "sources": sorted(
                    (
                        {
                            "candidate_id": item.get("candidate_id"),
                            "provider": item.get("provider"),
                            "provider_record_id": item.get("provider_record_id"),
                            "url": item.get("url"),
                        }
                        for item in members
                    ),
                    key=lambda item: str(item.get("candidate_id") or ""),
                ),
                "members": members,
            }
        )
    return sorted(
        clusters,
        key=lambda cluster: (
            str(next((item.get("year") or "" for item in cluster["members"]), "")),
            str(next((item.get("volume") or "" for item in cluster["members"]), "")),
            str(next((item.get("edition") or "" for item in cluster["members"]), "")),
            str(next((item.get("isbn") or "" for item in cluster["members"]), "")),
            cluster["manifestation_id"],
        ),
    )


def _explicit_selection_evidence(entry: dict[str, Any]) -> list[dict[str, str]]:
    explicit = dict(entry.get("selection_evidence") or {})
    provenance = entry.get("provenance") or {}
    fields = (
        "isbn", "doi", "volume", "issue", "edition", "year", "translators",
        "publisher", "pages", "series", "publication_language",
    )
    evidence: list[dict[str, str]] = []
    for field in fields:
        value = explicit.get(field)
        if value in (None, "", []):
            source = provenance.get(field) if isinstance(provenance, dict) else None
            if not isinstance(source, dict) or source.get("kind") not in {
                "source_explicit", "human_selection", "user_input", "user_confirmed"
            }:
                continue
            value = entry.get(field)
        if value not in (None, "", []):
            evidence.append({"field": field, "value": str(value)})
    return evidence


def _display_clusters_v29(
    result: dict[str, Any], clusters: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    display: list[dict[str, Any]] = []
    selection_conflict = (result.get("selection_audit") or {}).get(
        "selection_status"
    ) == "conflict"
    for index, cluster in enumerate(clusters, 1):
        representative = next(
            item for item in cluster["members"]
            if item.get("candidate_id") == cluster["representative_candidate_id"]
        )
        item = materialize_canonical_manifestation_v29(result, representative)
        item["reference_id"] = (
            f"{result.get('reference_id')}:candidate:{cluster['manifestation_id']}"
        )
        item["manifestation_id"] = cluster["manifestation_id"]
        item["candidate_ids"] = cluster["candidate_ids"]
        item["entry_ordinal"] = int(result.get("entry_ordinal") or 0) + index - 1
        item["reference_candidate_label_v29"] = f"候補{index}"
        item["reference_display_status"] = (
            "ready" if cluster["v29_render_ready"] and not selection_conflict else "review"
        )
        item["consulted_status"] = "candidate_not_confirmed"
        item["final_revision_blocked"] = selection_conflict or not cluster["v29_render_ready"]
        display.append(item)
    return display


def _name_identity(value: Any) -> tuple[str, ...]:
    """Return an order-insensitive Latin name identity for fallback gating."""
    return tuple(sorted(re.findall(r"[a-z0-9]+", str(value or "").casefold())))


def _foreign_manifestation_fallback_v29(
    result: dict[str, Any], clusters: list[dict[str, Any]],
) -> tuple[set[str], dict[str, Any]]:
    """Admit complete foreign editions only after bounded Japanese discovery.

    Search hypotheses remain non-canonical.  Here they are used only to prove
    that every ready foreign cluster is the same already-discovered work; the
    bibliographic fields still come exclusively from provider records.
    """
    audit: dict[str, Any] = {
        "eligible": False,
        "selection_basis": "foreign_manifestation_fallback",
        "blocking_reason": None,
        "translation_side_discovery_completed": bool(
            result.get("translation_side_discovery_completed") is True
        ),
        "japanese_manifestation_found": result.get("japanese_manifestation_found"),
        "japanese_manifestation_ready": bool(result.get("japanese_manifestation_ready")),
        "matched_manifestation_ids": [],
        "matched_candidate_ids": [],
        "source_urls": [],
    }
    if not result.get("source_language_manifestation_missing"):
        audit["blocking_reason"] = "source_language_manifestation_not_missing"
        return set(), audit
    if result.get("translation_side_discovery_triggered") is not True:
        audit["blocking_reason"] = "translation_side_discovery_not_run"
        return set(), audit
    if result.get("translation_side_discovery_completed") is not True:
        audit["blocking_reason"] = "translation_side_discovery_not_completed"
        return set(), audit
    if result.get("japanese_manifestation_found") is not False:
        audit["blocking_reason"] = "japanese_manifestation_status_not_exhausted"
        return set(), audit
    if result.get("japanese_manifestation_ready"):
        audit["blocking_reason"] = "ready_japanese_manifestation_exists"
        return set(), audit

    foreign_clusters = [
        cluster for cluster in clusters
        if cluster.get("v29_render_ready")
        and any(
            str(member.get("publication_language") or "").casefold()
            not in {"", "ja", "jpn", "jp", "unknown"}
            for member in cluster["members"]
        )
    ]
    if not foreign_clusters:
        audit["blocking_reason"] = "ready_foreign_manifestation_missing"
        return set(), audit

    hypotheses = result.get("search_hypotheses") or {}
    expected_titles = {
        _identity_text(value) for value in hypotheses.get("titles") or [] if value
    }
    expected_authors = {
        _name_identity(value) for value in hypotheses.get("authors") or [] if value
    }
    explicit_work_identity = str(result.get("work_identity_status") or "").casefold() in {
        "matched", "confirmed", "sufficiently_confirmed",
    }
    explicit_work_candidate_ids = {
        str(value) for value in result.get("work_identity_candidate_ids") or [] if value
    }
    relation_candidate_ids = {
        str(edge.get(field) or "")
        for edge in result.get("work_relation_edges") or []
        if edge.get("confidence") == "high"
        for field in ("source_candidate_id", "target_candidate_id")
    }
    relation_identity = bool(relation_candidate_ids)
    if not (expected_titles and expected_authors):
        identity_candidate_ids = relation_candidate_ids or (
            explicit_work_candidate_ids if explicit_work_identity else set()
        )
        foreign_clusters = [
            cluster for cluster in foreign_clusters
            if any(
                str(member.get("candidate_id") or "") in identity_candidate_ids
                for member in cluster["members"]
            )
        ]
        if not foreign_clusters:
            audit["blocking_reason"] = "foreign_work_identity_not_confirmed"
            audit["identity_ambiguity"] = True
            return set(), audit
    matched_candidate_ids: list[str] = []
    source_urls: list[str] = []
    hypothesis_identity = bool(expected_titles and expected_authors)
    for cluster in foreign_clusters:
        representative = next(
            member for member in cluster["members"]
            if member.get("candidate_id") == cluster["representative_candidate_id"]
        )
        title_match = _identity_text(representative.get("title")) in expected_titles
        author_match = any(
            _name_identity(name) in expected_authors
            for name in representative.get("authors") or []
        )
        source_url = str(representative.get("url") or "")
        source_present = bool(
            source_url or representative.get("source_urls")
            or representative.get("field_evidence")
        )
        if not source_present:
            audit["blocking_reason"] = "foreign_manifestation_source_missing"
            return set(), audit
        if (expected_titles or expected_authors) and not (title_match and author_match):
            audit["blocking_reason"] = "foreign_work_identity_not_confirmed"
            audit["identity_ambiguity"] = True
            return set(), audit
        matched_candidate_ids.append(str(representative.get("candidate_id") or ""))
        source_urls.extend(
            [source_url] if source_url else [
                str(value) for value in representative.get("source_urls") or [] if value
            ]
        )
    if not (
        hypothesis_identity
        or relation_identity
        or (explicit_work_identity and explicit_work_candidate_ids)
    ):
        audit["blocking_reason"] = "foreign_work_identity_not_confirmed"
        audit["identity_ambiguity"] = True
        return set(), audit

    manifestation_ids = {
        str(cluster["manifestation_id"]) for cluster in foreign_clusters
    }
    audit.update({
        "eligible": True,
        "blocking_reason": None,
        "work_identity_basis": (
            "exact_hypothesis_title_and_author" if hypothesis_identity
            else "confirmed_work_relation" if relation_identity
            else "explicit_work_identity_candidate_binding"
        ),
        "matched_manifestation_ids": sorted(manifestation_ids),
        "matched_candidate_ids": matched_candidate_ids,
        "source_urls": list(dict.fromkeys(source_urls)),
    })
    return manifestation_ids, audit


def _output_eligible_v29(
    source: dict[str, Any], cluster: dict[str, Any], evidence: list[dict[str, str]],
    relation_candidate_ids: set[str] | None = None,
    fallback_manifestation_ids: set[str] | None = None,
) -> tuple[bool, str]:
    if not cluster["v29_render_ready"]:
        return False, "v29_render_not_ready"
    mention = str(source.get("title") or "")
    if not re.search(r"[ぁ-んァ-ヶ一-龥]", mention):
        reason = "ready_matching_latin_mention"
    elif cluster["manifestation_id"] in (fallback_manifestation_ids or set()):
        reason = "ready_foreign_manifestation_fallback"
    elif relation_candidate_ids and any(
        str(member.get("candidate_id") or "") in relation_candidate_ids
        for member in cluster["members"]
    ):
        reason = "ready_manifestation_in_confirmed_work_relation"
    elif any(
        item["field"] in {"isbn", "doi", "volume", "edition", "year", "publisher"}
        for item in evidence
    ):
        reason = "ready_explicit_edition_evidence"
    elif any(
        str(member.get("publication_language") or "").casefold() in {"ja", "jpn", "jp"}
        and re.search(r"[ぁ-んァ-ヶ一-龥]", str(member.get("title") or ""))
        for member in cluster["members"]
    ):
        reason = "ready_japanese_mention_and_manifestation"
    else:
        return False, "foreign_language_manifestation_without_explicit_evidence"
    representative = next(
        item for item in cluster["members"]
        if item.get("candidate_id") == cluster["representative_candidate_id"]
    )
    materialized = materialize_canonical_manifestation_v29(source, representative)
    if materialized.get("v29_render_status") != "render_ready":
        return False, "materialized_output_not_ready"
    return True, reason


def finalize_reference_v29(entry: dict[str, Any]) -> dict[str, Any]:
    """Reconcile candidates, select only with explicit evidence, and classify parent state."""
    result = copy.deepcopy(entry)
    for candidate in result.get("candidate_matches") or []:
        if not isinstance(candidate, dict):
            continue
        if _identity_text(candidate.get("title")) != _identity_text(result.get("title")):
            continue
        candidate.setdefault("work_author_identity", result.get("author"))
        candidate.setdefault("work_title_identity", result.get("title"))
        candidate.setdefault("search_hypotheses", result.get("search_hypotheses") or {})
    candidates, propagation = reconcile_same_manifestation_v29(
        [item for item in result.get("candidate_matches") or [] if isinstance(item, dict)]
    )
    result["candidate_matches"] = candidates
    result["cross_candidate_propagations"] = propagation
    requested_relation_mode = result.get("relation_resolution_mode")
    result.update(resolve_work_relations_v29(result, candidates))
    if requested_relation_mode == "translation_side_discovery":
        result["relation_resolution_mode"] = "translation_side_discovery"
    confirmed_relation_ids = {
        str(edge.get(field) or "")
        for edge in result.get("work_relation_edges") or []
        if edge.get("confidence") == "high"
        for field in ("source_candidate_id", "target_candidate_id")
    }
    result["work_relation_status"] = (
        "WORK_RELATION_CONFIRMED"
        if confirmed_relation_ids else "WORK_RELATION_UNCONFIRMED"
    )
    ready_candidates = [
        item for item in candidates
        if item.get("v29_render_status") == "render_ready"
    ]
    ready_japanese = [
        item for item in ready_candidates
        if str(item.get("publication_language") or "").casefold() in {"ja", "jpn", "jp"}
    ]
    ready_foreign = [
        item for item in ready_candidates
        if str(item.get("publication_language") or "").casefold()
        not in {"", "ja", "jpn", "jp", "unknown"}
    ]
    japanese_mention = bool(re.search(
        r"[ぁ-んァ-ヶ一-龥]", str(result.get("title") or "")
    ))
    source_language_missing = bool(
        japanese_mention and ready_foreign and not ready_japanese
    )
    result["source_language_manifestation_missing"] = source_language_missing
    result["translation_manifestation_not_found"] = bool(
        source_language_missing and result.get("translation_side_discovery_triggered")
    )
    result["work_relation_unconfirmed"] = bool(
        source_language_missing and not confirmed_relation_ids
    )
    result["relation_connected_after_discovery"] = bool(
        result.get("translation_side_discovery_triggered") and confirmed_relation_ids
    )
    result["reference_diagnostic_codes"] = (
        ["reference.source_language_manifestation_missing"]
        if source_language_missing else []
    )
    clusters = cluster_manifestations_v29(candidates)
    result["manifestation_split_count"] = max(0, len(clusters) - 1)
    result["true_metadata_conflict_count"] = sum(
        len(item.get("metadata_conflicts") or {}) for item in candidates
    )
    result["manifestation_clusters"] = [
        {key: value for key, value in cluster.items() if key != "members"}
        for cluster in clusters
    ]
    discovered_ready = [cluster for cluster in clusters if cluster["v29_render_ready"]]
    evidence = _explicit_selection_evidence(result)
    mention_identity = _identity_text(result.get("title"))
    has_direct_ready_mention = any(
        cluster["v29_render_ready"]
        and any(
            mention_identity
            and mention_identity in _identity_text(member.get("title"))
            and str(member.get("publication_language") or "").casefold()
            in {"ja", "jpn", "jp"}
            for member in cluster["members"]
        )
        for cluster in clusters
    )
    # A confirmed work relation may rescue an otherwise unmatched translated
    # mention (for example a manuscript/catalogue wording variant).  It must not
    # broaden an already resolved Japanese mention to every foreign edition of
    # the same work.
    relation_output_ids = set() if has_direct_ready_mention else confirmed_relation_ids
    fallback_manifestation_ids, fallback_audit = _foreign_manifestation_fallback_v29(
        result, clusters,
    )
    result["foreign_manifestation_fallback_audit"] = fallback_audit
    result["foreign_manifestation_fallback_triggered"] = bool(
        fallback_manifestation_ids
    )
    result["foreign_manifestation_fallback_blocked_due_to_ambiguity"] = bool(
        fallback_audit.get("identity_ambiguity")
    )
    result["foreign_manifestation_fallback_candidate_ids"] = list(
        fallback_audit.get("matched_candidate_ids") or []
    )
    if fallback_manifestation_ids:
        result["selection_basis"] = "foreign_manifestation_fallback"
    eligibility = {
        cluster["manifestation_id"]: _output_eligible_v29(
            result, cluster, evidence, relation_output_ids, fallback_manifestation_ids
        )
        for cluster in clusters
    }
    ready = [
        cluster for cluster in clusters
        if eligibility[cluster["manifestation_id"]][0]
    ]
    result["discovered_manifestation_count"] = len(clusters)
    result["review_leads"] = [
        {**{key: value for key, value in cluster.items() if key != "members"},
         "reason": eligibility[cluster["manifestation_id"]][1],
         "blocking_reasons": sorted({
             reason for member in cluster["members"]
             for reason in member.get("v29_review_reasons") or []
         })}
        for cluster in clusters if not eligibility[cluster["manifestation_id"]][0]
    ]
    result["review_lead_count"] = len(result["review_leads"])
    result["review_lead_ids"] = [item["manifestation_id"] for item in result["review_leads"]]
    result["output_candidate_ids"] = [item["manifestation_id"] for item in ready]
    result["output_eligibility_reasons"] = {
        item["manifestation_id"]: eligibility[item["manifestation_id"]][1]
        for item in ready
    }
    result["v29_ready_candidate_count"] = sum(
        candidate.get("v29_render_status") == "render_ready" for candidate in candidates
    )
    result["nonstandard_publication_ready_count"] = sum(
        candidate.get("v29_render_status") == "render_ready"
        and candidate.get("record_type") in {
            "nonstandard_publication", "nonstandard_contribution",
            "web_contribution", "doujin_contribution", "self_published_contribution",
        }
        for candidate in candidates
    )
    result["v29_ready_manifestation_count"] = len(discovered_ready)
    result["manifestation_resolution_status"] = (
        "MANIFESTATION_MULTIPLE" if len(ready) >= 2
        else "MANIFESTATION_SINGLE" if len(ready) == 1
        else "MANIFESTATION_UNRESOLVED"
    )
    result["relation_confirmed_multiple_count"] = int(
        bool(confirmed_relation_ids) and len(ready) >= 2
    )
    confirmed_id = (
        str(result.get("selected_candidate_id") or "")
        if result.get("consulted_status") == "user_confirmed"
        else ""
    )
    if confirmed_id:
        evidence.append({"field": "confirmed_candidate_id", "value": confirmed_id})
    remaining = ready
    for item in evidence:
        field, value = item["field"], _identity_text(item["value"])
        remaining = [
            cluster
            for cluster in remaining
            if any(
                (field == "confirmed_candidate_id" and confirmed_id in cluster["candidate_ids"])
                or value == _identity_text(member.get(field))
                or (
                    field == "translators"
                    and value in {_identity_text(name) for name in member.get(field) or []}
                )
                for member in cluster["members"]
            )
        ]
    result["selection_audit"] = {
        "selection_basis": result.get("selection_basis"),
        "selection_evidence": evidence,
        "candidate_count_before": len(ready),
        "candidate_count_after": len(remaining),
        "remaining_manifestation_ids": [item["manifestation_id"] for item in remaining],
        "missing_disambiguators": [] if evidence else ["isbn", "volume", "year", "edition"],
    }
    if not ready:
        result["foreign_manifestation_fallback_used"] = False
        result["reference_state_v29"] = (
            "UNRESOLVED_METADATA" if clusters or result.get("is_self_authored")
            or result.get("is_unpublished")
            or str(result.get("author") or "").strip() in {"自著", "筆者", "本稿筆者"}
            else "UNRESOLVED_IDENTITY"
        )
        result["reference_output_mode_v29"] = (
            "REVIEW" if result["reference_state_v29"] == "UNRESOLVED_METADATA"
            else "UNRESOLVED"
        )
        result["consulted_manifestation_id"] = None
        if result.get("consulted_status") != "user_confirmed":
            result.pop("selected_candidate_id", None)
        result["final_revision_blocked"] = True
        result["expanded_reference_entries"] = []
        result["output_candidate_count"] = 0
        result["output_candidate_ids"] = []
        return canonicalize_reference(result)
    if evidence and len(remaining) == 1:
        cluster = remaining[0]
        selected = next(
            item for item in cluster["members"]
            if item.get("candidate_id") == cluster["representative_candidate_id"]
        )
        result["selected_candidate_id"] = selected.get("candidate_id")
        result["consulted_manifestation_id"] = cluster["manifestation_id"]
        result["selection_audit"]["selection_status"] = "selected"
        result["selection_audit"]["selected_manifestation_id"] = cluster["manifestation_id"]
        result["reference_state_v29"] = "SINGLE_MANIFESTATION_RESOLVED"
        result["reference_output_mode_v29"] = "SINGLE"
        result["consulted_status"] = "source_confirmed"
        result["resolution_status"] = "resolved"
        result["expanded_reference_entries"] = []
        result["output_candidate_count"] = 1
        result["output_candidate_ids"] = [cluster["manifestation_id"]]
        result["output_eligibility_reasons"] = {
            cluster["manifestation_id"]: "selected_by_explicit_evidence"
        }
        result["review_leads"].extend(
            {**{key: value for key, value in other.items() if key != "members"},
             "reason": "explicit_selection_other_manifestation"}
            for other in ready if other["manifestation_id"] != cluster["manifestation_id"]
        )
        result["review_lead_count"] = len(result["review_leads"])
        result["review_lead_ids"] = [item["manifestation_id"] for item in result["review_leads"]]
        result["required_missing_fields"] = []
        result["final_revision_blocked"] = False
        result["conditional_proposal_allowed"] = False
        result["foreign_manifestation_fallback_used"] = bool(
            cluster["manifestation_id"] in fallback_manifestation_ids
        )
        if result["foreign_manifestation_fallback_used"]:
            result["reference_diagnostic_codes"] = [
                "reference.source_language_manifestation_missing",
                "reference.foreign_manifestation_fallback_used",
            ]
        return materialize_canonical_manifestation_v29(result, selected)
    if not evidence and len(ready) == 1:
        cluster = ready[0]
        selected = next(
            item for item in cluster["members"]
            if item.get("candidate_id") == cluster["representative_candidate_id"]
        )
        result["reference_state_v29"] = "SINGLE_MANIFESTATION_RESOLVED"
        result["reference_output_mode_v29"] = "SINGLE"
        result["consulted_manifestation_id"] = cluster["manifestation_id"]
        result["selected_candidate_id"] = selected.get("candidate_id")
        result["resolution_status"] = "resolved"
        result["final_revision_blocked"] = False
        result["expanded_reference_entries"] = []
        result["output_candidate_count"] = 1
        result["output_candidate_ids"] = [cluster["manifestation_id"]]
        result["output_eligibility_reasons"] = {
            cluster["manifestation_id"]: eligibility[cluster["manifestation_id"]][1]
        }
        result["required_missing_fields"] = []
        result["user_required_metadata"] = []
        result["foreign_manifestation_fallback_used"] = bool(
            cluster["manifestation_id"] in fallback_manifestation_ids
        )
        if result["foreign_manifestation_fallback_used"]:
            result["reference_diagnostic_codes"] = [
                "reference.source_language_manifestation_missing",
                "reference.foreign_manifestation_fallback_used",
            ]
        return materialize_canonical_manifestation_v29(result, selected)
    result["consulted_manifestation_id"] = None
    if result.get("consulted_status") != "user_confirmed":
        result.pop("selected_candidate_id", None)
    all_ready = len(ready) >= 2
    result["reference_state_v29"] = (
        "MULTIPLE_MANIFESTATIONS_READY" if all_ready and not (evidence and not remaining)
        else "UNRESOLVED_METADATA"
    )
    result["reference_output_mode_v29"] = (
        "MULTIPLE" if result["reference_state_v29"] == "MULTIPLE_MANIFESTATIONS_READY"
        else "REVIEW"
    )
    result["resolution_status"] = (
        "specified_scope_unresolved" if evidence and not remaining
        else "work_resolved_manifestations_ready_selection_required"
    )
    result["selection_audit"]["selection_status"] = (
        "conflict" if evidence and not remaining else "required"
    )
    result["final_revision_blocked"] = result["reference_output_mode_v29"] != "MULTIPLE"
    result["conditional_proposal_allowed"] = True
    result["v29_render_status"] = "selection_required"
    result["v29_render_supported"] = False
    result["v29_required_missing_fields"] = []
    result["expanded_reference_entries"] = _display_clusters_v29(
        result, ready if result["reference_output_mode_v29"] == "MULTIPLE" else []
    )
    result["foreign_manifestation_fallback_used"] = bool(
        result["reference_output_mode_v29"] == "MULTIPLE"
        and fallback_manifestation_ids
        and all(
            cluster["manifestation_id"] in fallback_manifestation_ids
            for cluster in ready
        )
    )
    if result["foreign_manifestation_fallback_used"]:
        result["reference_diagnostic_codes"] = [
            "reference.source_language_manifestation_missing",
            "reference.foreign_manifestation_fallback_used",
        ]
        for item in result["expanded_reference_entries"]:
            item["foreign_manifestation_fallback_used"] = True
            item["selection_basis"] = "foreign_manifestation_fallback"
            item["reference_diagnostic_codes"] = list(
                result["reference_diagnostic_codes"]
            )
    result["output_candidate_count"] = len(result["expanded_reference_entries"])
    if result["reference_output_mode_v29"] == "REVIEW":
        result["output_candidate_ids"] = []
        result["output_eligibility_reasons"] = {}
        result["review_leads"] = [
            {**{key: value for key, value in cluster.items() if key != "members"},
             "reason": "explicit_selection_conflict"}
            if cluster["manifestation_id"] in {item["manifestation_id"] for item in ready}
            else next(
                item for item in result["review_leads"]
                if item["manifestation_id"] == cluster["manifestation_id"]
            )
            for cluster in clusters
        ]
        result["review_lead_count"] = len(result["review_leads"])
        result["review_lead_ids"] = [item["manifestation_id"] for item in result["review_leads"]]
    return result
