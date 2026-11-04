"""Deterministic Japanese bibliography extraction, enrichment, and rendering."""

from __future__ import annotations

import copy
import hashlib
import re
import unicodedata
from collections.abc import Callable, Iterable
from difflib import SequenceMatcher
from typing import Any
from xml.etree import ElementTree as ET

from fukamiru.documents.canonical_html import CanonicalHTMLValidator
from fukamiru.errors import FukamiruError
from fukamiru.literature.service import LiteratureCandidate
from fukamiru.revisions.bibliography_style import format_reference
from fukamiru.revisions.coherence import ManuscriptCoherenceService

SJLLF_JA_REFERENCE_RULESET: dict[str, Any] = {
    "id": "japanese-reference-policy",
    "user_overrides": {
        "japanese_terminal": "。",
        "display_country_codes": False,
        "one_reference_per_block": True,
    },
    "language": "ja",
    "title": "『フランス語フランス文学研究』執筆要項から抽出した日本語文献規則",
    "source_url": "https://www.sjllf.org/file/1707",
    "source_revision": "2024-03-09",
    "scope_note": (
        "日本語論文または日本語文献に明示された条項だけを収録する。"
        "検索、同定、来歴管理はFukamiruの運用規則であり、SJLLF規則とは区別する。"
    ),
    "rules": [
        {
            "code": "sjllf.ja.title.book_or_journal",
            "section": "6.A",
            "rule": "書籍・雑誌名は『 』に入れる。",
        },
        {
            "code": "sjllf.ja.title.article_poem_chapter",
            "section": "6.A",
            "rule": "論文・詩・章などの題名は「 」に入れる。",
        },
        {
            "code": "sjllf.reference.monograph_order",
            "section": "10",
            "rule": (
                "著者名、書名、編著者名、出版地、出版社名、叢書名、刊行年、ページの順に記す。"
            ),
        },
        {
            "code": "sjllf.reference.journal_article_order",
            "section": "10",
            "rule": "執筆者名、論文名、雑誌名、巻号、刊行年、ページの順に記す。",
        },
        {
            "code": "sjllf.reference.page_format",
            "section": "10",
            "rule": "ページ番号はp. 32-33.の形式とし、pp.は用いない。",
        },
        {
            "code": "sjllf.ja.note_number_position",
            "section": "9.A",
            "rule": "日本語論文の注番号は句読点または閉じ括弧の前に置く。",
        },
    ],
}

BIBLIOGRAPHY_HOUSE_STYLE_V29: dict[str, Any] = {
    "id": "bibliography-house-style",
    "title": "利用者指定の参考文献表記",
    "source": "docs/specification/bibliography-style.md",
    "scope_note": (
        "添付ファイルに明記された基本形を適用する。"
        "未添付の詳細規則に依存する言語・資料種別は未確定として報告する。"
    ),
}

_REFERENCE_HEADING = re.compile(
    r"^\s*(?:#{1,6}\s+)?"
    r"(?:主要参照文献|主要参考文献|参考文献|参照文献|引用文献|文献一覧)"
)
_ENTRY_END = re.compile(r"。+")
_SHARED_AUTHOR_TITLE = re.compile(r"[／/](?=[『「])")
_AUTHOR_TITLE = re.compile(
    r"^\s*(?P<author>.*?)(?P<open>[『「])(?P<title>[^』」]{1,500})(?P<close>[』」])"
    r"(?P<tail>.*)\s*$",
    re.DOTALL,
)
_YEAR = re.compile(r"(?<!\d)((?:18|19|20)\d{2})(?!\d)")
_PAGES = re.compile(r"\bpp?\.\s*([0-9０-９]+(?:\s*[-–—〜～]\s*[0-9０-９]+)?)", re.I)
_DOI = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.I)
_ISBN = re.compile(r"\b(?:97[89][- ]?)?[0-9Xx][-0-9Xx ]{8,16}\b")
_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")
_INFERENCE_FIELDS = {
    "record_type",
    "publisher",
    "publication_place",
    "series",
    "year",
    "container_title",
    "volume",
    "issue",
    "pages",
}

# Version 21 keeps its historical resolver for resumability.  The V22 policy is
# deliberately data-only: callers opt in and may persist the returned audit
# structures after the active V21 run has finished.
REFERENCE_RESOLUTION_POLICY_V22: dict[str, Any] = {
    "id": "japanese_reference_resolution_v22",
    "minimum_identity_score": 0.86,
    "consulted_edition_only": True,
    "unspecified_original_or_translation": "retain_all",
    "unspecified_volume": "retain_complete_set",
    "unspecified_edition": "retain_all",
    "bounded_search_claims_exhaustiveness": False,
    "search_incomplete_display_label": "確認できた候補すべて（網羅性未確認）",
    "translation_requires_translator": True,
    "unresolved_edition_allows_conditional_proposal": True,
    "unresolved_edition_blocks_final_revision": True,
    "discovered_only_section_title": "機械判別参考文献",
}


def _stable_id(*parts: object) -> str:
    digest = hashlib.sha256("\x1f".join(map(str, parts)).encode()).hexdigest()
    return f"reference:{digest[:24]}"


def _normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return "".join(
        character
        for character in value
        if character.isalnum() or "ぁ" <= character <= "ヿ" or "一" <= character <= "龥"
    )


def _publisher_identity(value: str) -> str:
    """Ignore an explicitly labelled distributor when comparing one imprint."""
    value = re.sub(
        r"\s+\S+(?:\s+\S+)*\s*[（(](?:発売|取次|頒布|販売)[）)]\s*$", "", value.strip()
    )
    value = re.sub(r"\s*[（(](?:出版|発行)[）)]\s*$", "", value)
    value = re.sub(r"^(?:la|le|les|the)\s+", "", value, flags=re.I)
    value = re.sub(r"^(?:éditions|editions|édition|edition)\s+", "", value, flags=re.I)
    value = re.sub(r"\s+(?:éditions|editions|édition|edition)$", "", value, flags=re.I)
    value = re.sub(r",\s*(?:India|USA|UK|Japan|France)$", "", value, flags=re.I)
    return _normalize(value)


def _provenance(
    kind: str,
    *,
    label: str,
    source: str,
    confidence: float = 1.0,
    source_url: str | None = None,
    provider_record_id: str | None = None,
    rationale: str | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "label": label,
        "source": source,
        "source_url": source_url,
        "provider_record_id": provider_record_id,
        "confidence": round(float(confidence), 4),
        "rationale": rationale,
    }


class JapaneseReferenceService:
    """Keep bibliography prose outside argument rewriting while repairing its records."""

    @classmethod
    def select_candidate(
        cls,
        entry: dict[str, Any],
        candidate_id: str,
        *,
        workflow_version: int = 21,
    ) -> dict[str, Any]:
        selected = next(
            (
                c
                for c in entry.get("candidate_matches", [])
                if c.get("candidate_id") == candidate_id
            ),
            None,
        )
        if selected is None:
            raise ValueError("This candidate does not belong to the reference")
        result = copy.deepcopy(entry)
        if selected.get("title"):
            result["title"] = selected["title"]
        result["selected_authors"] = selected.get("authors") or []
        if workflow_version >= 29 and selected.get("record_type"):
            result["record_type"] = selected["record_type"]
        result["previous_unparsed_fields"] = result.pop("unparsed_fields", [])
        for field in (
            "publisher",
            "year",
            "volume",
            "issue",
            "edition",
            "pages",
            "isbn",
            "doi",
            "url",
            "translators",
            "original_title",
            "translated_title",
            "publication_place",
            "series",
            "container_title",
            "editors",
            "publication_language",
        ):
            # A selected edition replaces its metadata as one unit; no mixing editions.
            result[field] = selected.get(field)
            result.setdefault("provenance", {})[field] = _provenance(
                "human_selection",
                label="ユーザーが書誌候補を指定",
                source=str(selected.get("provider") or ""),
                source_url=selected.get("url"),
                confidence=1.0,
            )
        result.update(
            selected_candidate_id=candidate_id,
            resolution_status="resolved",
            edition_status="user_selected",
            work_identity_status="user_selected",
            bibliography_kind=cls.bibliography_kind(selected),
        )
        if workflow_version >= 22:
            for stale_field in (
                "expanded_reference_entries",
                "rendered_manifestations",
                "selected_candidate_ids",
            ):
                result.pop(stale_field, None)
            result["translation_status"] = selected.get(
                "translation_status"
            ) or cls._translation_status(result, selected)
            result["bibliography_kind"] = cls._bibliography_kind_v22(selected)
            result["translator_status"] = selected.get(
                "translator_status"
            ) or cls._translator_status(result, selected)
            missing = cls._candidate_required_missing_fields(
                {**selected, "translator_status": result["translator_status"]}
            )
            result.update(
                consulted_status="user_confirmed",
                required_missing_fields=missing,
                conditional_proposal_allowed=bool(missing),
                final_revision_blocked=bool(missing),
            )
            if missing:
                result["resolution_status"] = "metadata_incomplete"
                result["user_action_required"] = {
                    "code": "complete_required_bibliographic_metadata",
                    "fields": missing,
                    "message": "最終Revisionの前に必須書誌情報を確認してください。",
                }
            else:
                result.pop("user_action_required", None)
        return result

    @classmethod
    def confirm_manifestations_v22(
        cls, entry: dict[str, Any], candidate_ids: Iterable[str]
    ) -> dict[str, Any]:
        """Confirm one or more actually consulted manifestations explicitly."""

        candidates = [
            copy.deepcopy(candidate)
            for candidate in entry.get("candidate_matches") or []
            if str(candidate.get("candidate_id") or "")
        ]
        available = {
            str(candidate["candidate_id"]): candidate for candidate in candidates
        }
        requested_set = {
            str(value).strip() for value in candidate_ids if str(value).strip()
        }
        if not requested_set or any(
            candidate_id not in available for candidate_id in requested_set
        ):
            raise ValueError("A selected manifestation does not belong to the reference")
        # Candidate order is the post-dedup/resort order, not request ordering.  This
        # keeps canonical HTML stable for equivalent checkbox selections.
        requested = [
            str(candidate["candidate_id"])
            for candidate in candidates
            if str(candidate["candidate_id"]) in requested_set
        ]
        selected: list[dict[str, Any]] = []
        for candidate_id in requested:
            candidate = available[candidate_id]
            candidate["translation_status"] = candidate.get(
                "translation_status"
            ) or cls._translation_status(entry, candidate)
            candidate["translator_status"] = candidate.get(
                "translator_status"
            ) or cls._translator_status(entry, candidate)
            selected.append(candidate)
        result = copy.deepcopy(entry)
        expanded = [
            cls.refresh_consulted_manifestation_v22(
                cls._manifestation_entry(
                    result,
                    candidate,
                    index=index,
                    consulted_status="user_confirmed",
                    final_revision_blocked=bool(
                        cls._candidate_required_missing_fields(candidate)
                    ),
                )
            )
            for index, candidate in enumerate(selected)
        ]
        result.pop("selected_candidate_id", None)
        result.update(
            selected_candidate_ids=requested,
            edition_status="user_selected",
            work_identity_status="user_selected",
        )
        result["expanded_reference_entries"] = expanded
        return cls.refresh_consulted_parent_v22(result)

    @classmethod
    def refresh_consulted_manifestation_v22(
        cls, entry: dict[str, Any]
    ) -> dict[str, Any]:
        """Recompute one user-confirmed manifestation after scoped metadata edits.

        The generated ``reference_id``, parent ``source_reference_id`` and provider
        ``candidate_id`` are intentionally copied, never regenerated.  This makes a
        metadata PATCH address one selected edition/translation without leaking the
        edit into sibling manifestations.
        """

        result = copy.deepcopy(entry)
        authors = result.get("authors") or []
        if isinstance(authors, str):
            authors = [authors]
        authors = [str(value).strip() for value in authors if str(value).strip()]
        author = str(result.get("author") or "").strip()
        if not authors and author:
            authors = [author]
        if authors:
            result["authors"] = authors
            result["author"] = "・".join(authors)

        translation_status = str(result.get("translation_status") or "")
        if translation_status not in {"confirmed_translation", "confirmed_original"}:
            translation_status = cls._translation_status({}, result)
        result["translation_status"] = translation_status
        result["translator_status"] = cls._translator_status({}, result)
        missing = cls._candidate_required_missing_fields(result)
        result.update(
            consulted_status="user_confirmed",
            reference_role=(
                "confirmed_consulted_manifestation"
                if result.get("source_reference_id")
                else str(result.get("reference_role") or "consulted_or_cited")
            ),
            required_missing_fields=missing,
            resolution_status=(
                "metadata_incomplete" if missing else "manifestation_confirmed"
            ),
            conditional_proposal_allowed=bool(missing),
            final_revision_blocked=bool(missing),
        )
        if missing:
            result["user_action_required"] = {
                "code": "complete_consulted_manifestation_metadata",
                "fields": missing,
                "reference_id": result.get("reference_id"),
                "source_reference_id": result.get("source_reference_id"),
                "candidate_id": result.get("candidate_id"),
                "message": (
                    "実際に参照したこの版・巻について、原著／翻訳・訳者を含む"
                    "必須書誌情報を確認してください。"
                ),
            }
        else:
            result.pop("user_action_required", None)
        return result

    @classmethod
    def refresh_consulted_parent_v22(
        cls, entry: dict[str, Any]
    ) -> dict[str, Any]:
        """Recompute aggregate approval state from independently editable children."""

        result = copy.deepcopy(entry)
        expanded = [
            cls.refresh_consulted_manifestation_v22(item)
            for item in result.get("expanded_reference_entries") or []
            if isinstance(item, dict)
        ]
        if not expanded:
            return cls.refresh_consulted_manifestation_v22(result)

        missing = sorted(
            {
                str(field)
                for item in expanded
                for field in item.get("required_missing_fields") or []
                if str(field)
            }
        )
        blocked = any(item.get("final_revision_blocked") is True for item in expanded)
        translation_statuses = {
            str(item.get("translation_status") or "undetermined") for item in expanded
        }
        translator_statuses = {
            str(item.get("translator_status") or "undetermined") for item in expanded
        }
        result.update(
            expanded_reference_entries=expanded,
            rendered_manifestations=[
                cls.render_entry(item, workflow_version=22) for item in expanded
            ],
            consulted_status="user_confirmed",
            required_missing_fields=missing,
            resolution_status="metadata_incomplete" if blocked else "resolved",
            conditional_proposal_allowed=blocked,
            final_revision_blocked=blocked,
            translation_status=(
                next(iter(translation_statuses))
                if len(translation_statuses) == 1
                else "multiple_manifestation_states"
            ),
            translator_status=(
                "required_missing"
                if "required_missing" in translator_statuses
                else "undetermined"
                if "undetermined" in translator_statuses
                else next(iter(translator_statuses))
                if len(translator_statuses) == 1
                else "verified_or_not_required"
            ),
        )
        if blocked:
            result["user_action_required"] = {
                "code": "complete_required_bibliographic_metadata",
                "fields": missing,
                "reference_ids": [
                    str(item.get("reference_id") or "")
                    for item in expanded
                    if item.get("final_revision_blocked") is True
                ],
                "candidate_ids": [
                    str(item.get("candidate_id") or "")
                    for item in expanded
                    if item.get("final_revision_blocked") is True
                    and str(item.get("candidate_id") or "")
                ],
                "message": "最終Revisionの前に、未確定の版ごとに書誌情報を確認してください。",
            }
        else:
            result.pop("user_action_required", None)
        return result

    @classmethod
    def extract(
        cls, canonical_html: str, *, workflow_version: int = 21
    ) -> dict[str, Any]:
        machine_block_ids: set[str] = set()
        if workflow_version >= 22:
            parsed_root = ET.fromstring(canonical_html)
            for section in parsed_root.iter("section"):
                if section.attrib.get("data-reference-section") != "machine-detected-v22":
                    continue
                machine_block_ids.update(
                    str(element.attrib["id"])
                    for element in section.iter()
                    if element.attrib.get("id")
                )
        structure = ManuscriptCoherenceService.semantic_structure(canonical_html)
        reference_sections = {
            str(section["section_id"]): section
            for section in structure.get("sections", [])
            if isinstance(section, dict)
            and str(section.get("document_role")) == "references"
            and str(section.get("heading_block_id") or "") not in machine_block_ids
        }
        entries: list[dict[str, Any]] = []
        block_ids: list[str] = []
        for block in structure.get("blocks", []):
            if not isinstance(block, dict):
                continue
            section_id = str(block.get("section_id") or "")
            if section_id not in reference_sections:
                continue
            block_id = str(block.get("id") or "")
            if block_id in machine_block_ids:
                continue
            text = str(block.get("text") or "").strip()
            is_heading = bool(block.get("is_heading"))
            if is_heading:
                text = _REFERENCE_HEADING.sub("", text, count=1).strip(" ：:")
                if not text:
                    continue
            block_ids.append(block_id)
            entries.extend(
                cls._extract_block_entries(
                    block_id=block_id,
                    section_id=section_id,
                    text=text,
                    block_ordinal=len(block_ids) - 1,
                )
            )
        result = {
            "rule_set": copy.deepcopy(
                BIBLIOGRAPHY_HOUSE_STYLE_V29
                if workflow_version >= 29 else SJLLF_JA_REFERENCE_RULESET
            ),
            "reference_section_count": len(reference_sections),
            "reference_block_ids": list(dict.fromkeys(block_ids)),
            "entry_count": len(entries),
            "entries": entries,
        }
        if workflow_version >= 22:
            result["machine_reference_block_ids"] = sorted(machine_block_ids)
        return result

    @classmethod
    def _extract_block_entries(
        cls,
        *,
        block_id: str,
        section_id: str,
        text: str,
        block_ordinal: int,
    ) -> list[dict[str, Any]]:
        # Full stops inside book/article titles are part of the title.
        fragments, current, stack = [], [], []
        for character in text:
            if character in "『「":
                stack.append("』" if character == "『" else "」")
            elif stack and character == stack[-1]:
                stack.pop()
            if character == "。" and not stack:
                if "".join(current).strip():
                    fragments.append("".join(current).strip())
                current = []
            else:
                current.append(character)
        if "".join(current).strip():
            fragments.append("".join(current).strip())
        result: list[dict[str, Any]] = []
        for fragment_ordinal, fragment in enumerate(fragments):
            shared_author: str | None = None
            for shared_ordinal, piece in enumerate(_SHARED_AUTHOR_TITLE.split(fragment)):
                piece = piece.strip()
                match = _AUTHOR_TITLE.match(piece)
                entry_id = _stable_id(block_id, fragment_ordinal, shared_ordinal, piece)
                if match is None or (
                    (match.group("open") == "『") != (match.group("close") == "』")
                ):
                    result.append(
                        {
                            "reference_id": entry_id,
                            "section_id": section_id,
                            "source_block_id": block_id,
                            "block_ordinal": block_ordinal,
                            "entry_ordinal": len(result),
                            "original_text": piece,
                            "language": "ja",
                            "record_type": "unknown",
                            "author": shared_author,
                            "title": None,
                            "container_title": None,
                            "publisher": None,
                            "publication_place": None,
                            "series": None,
                            "year": None,
                            "volume": None,
                            "issue": None,
                            "pages": None,
                            "doi": None,
                            "isbn": None,
                            "url": None,
                            "provenance": {},
                            "parse_status": "unresolved",
                            "resolution_status": "unresolved",
                            "candidate_matches": [],
                        }
                    )
                    continue
                author = match.group("author").strip(" 、,") or shared_author
                if author:
                    shared_author = author
                title = match.group("title").strip()
                tail = match.group("tail").strip(" 、,")
                record_type = (
                    "book_or_journal" if match.group("open") == "『" else "article_poem_or_chapter"
                )
                year_match = _YEAR.search(tail)
                page_match = _PAGES.search(tail)
                doi_match = _DOI.search(tail)
                isbn_match = _ISBN.search(tail)
                provenance: dict[str, Any] = {
                    "title": _provenance(
                        "source_explicit",
                        label="原稿に明記",
                        source="manuscript",
                    ),
                    "record_type": _provenance(
                        "source_explicit",
                        label="原稿に明記",
                        source=("『』" if record_type == "book_or_journal" else "「」"),
                    ),
                }
                if author:
                    provenance["author"] = _provenance(
                        "source_explicit", label="原稿に明記", source="manuscript"
                    )
                fields = {
                    "year": year_match.group(1) if year_match else None,
                    "pages": (
                        page_match.group(1).translate(_FULLWIDTH_DIGITS).replace(" ", "")
                        if page_match
                        else None
                    ),
                    "doi": doi_match.group(0).rstrip(".") if doi_match else None,
                    "isbn": isbn_match.group(0).replace(" ", "") if isbn_match else None,
                }
                tail_fields = [p.strip(" .。") for p in tail.split("、") if p.strip(" .。")]
                descriptive_fields = [
                    p
                    for p in tail_fields
                    if not _YEAR.fullmatch(p)
                    and not re.fullmatch(r"[A-Z]{2,3}", p)
                    and not _PAGES.search(p)
                    and not _DOI.search(p)
                    and not _ISBN.search(p)
                ]
                remaining = []
                for item in descriptive_fields:
                    if item.endswith("訳"):
                        fields["translators"] = item[:-1].split("・")
                    elif item.endswith("編"):
                        fields["editors"] = item[:-1].split("・")
                    elif re.fullmatch(
                        r"(?:[上中下](?:巻)?|第?[0-9０-９一二三四五六七八九十]+巻)", item
                    ):
                        fields["volume"] = item
                    elif re.fullmatch(r"(?:(?:増補)?改訂版|新版|初版|第.+版)", item):
                        fields["edition"] = item
                    elif (
                        item.startswith("『")
                        and item.endswith("』")
                        and record_type == "article_poem_or_chapter"
                    ):
                        fields["container_title"] = item[1:-1]
                    else:
                        remaining.append(item)
                if record_type == "book_or_journal" and len(remaining) == 1:
                    fields["publisher"] = remaining.pop()
                # Preserve explicit metadata until fields/edition can be resolved.
                fields["unparsed_fields"] = remaining
                for field, value in fields.items():
                    if value:
                        provenance[field] = _provenance(
                            "source_explicit", label="原稿に明記", source="manuscript"
                        )
                result.append(
                    {
                        "reference_id": entry_id,
                        "section_id": section_id,
                        "source_block_id": block_id,
                        "block_ordinal": block_ordinal,
                        "entry_ordinal": len(result),
                        "original_text": piece,
                        "language": "ja",
                        "record_type": record_type,
                        "author": author,
                        "title": title,
                        "container_title": None,
                        "publisher": None,
                        "publication_place": None,
                        "series": None,
                        "year": fields["year"],
                        "volume": None,
                        "issue": None,
                        "pages": fields["pages"],
                        "doi": fields["doi"],
                        "isbn": fields["isbn"],
                        "url": None,
                        "provenance": provenance,
                        "parse_status": "parsed",
                        "resolution_status": "pending",
                        "candidate_matches": [],
                        "original_title": None,
                        "translated_title": None,
                        "translators": [],
                        "editors": [],
                        "edition": None,
                        **fields,
                    }
                )
        return result

    @staticmethod
    def search_query(entry: dict[str, Any]) -> str:
        author = str(entry.get("author") or "").strip()
        if entry.get("is_self_authored") is True or author in {
            "自著",
            "筆者",
            "本稿筆者",
            "著者本人",
        }:
            author = ""
        return " ".join(item for item in (str(entry.get("title") or "").strip(), author) if item)

    @classmethod
    def candidate_record(
        cls, entry: dict[str, Any], candidate: LiteratureCandidate
    ) -> dict[str, Any]:
        payload = candidate.provider_payload or {}
        direct_title_score = cls._title_score(str(entry.get("title") or ""), candidate.title)
        aliases = entry.get("search_aliases") or {}
        title_score = max(
            [
                direct_title_score,
                *(
                    cls._title_score(str(t), candidate.title)
                    for t in (
                        entry.get("original_title"),
                        entry.get("translated_title"),
                        *aliases.get("titles", []),
                    )
                    if t
                ),
            ]
        )
        author_score = max(
            [
                cls._author_score(str(entry.get("author") or ""), candidate.authors),
                *(
                    cls._author_score(str(a), candidate.authors)
                    for a in aliases.get("authors", [])
                    if a
                ),
            ]
        )
        fielded = payload.get("fielded_query") or {}
        if (
            candidate.provider == "ndl_search"
            and fielded.get("creator")
            and _normalize(str(fielded["creator"])) == _normalize(str(entry.get("author")))
            and candidate.authors
        ):
            # The provider's creator field, unlike a generic any-field query,
            # resolves name variants against its bibliographic creator index.
            author_score = max(author_score, 1.0)
        type_score = cls._type_score(str(entry.get("record_type") or ""), candidate.record_type)
        identity_score = round(title_score * 0.78 + author_score * 0.17 + type_score * 0.05, 4)
        conflicts = [
            field
            for field, value in (
                ("publisher", candidate.publisher),
                ("year", candidate.publication_date),
            )
            if entry.get(field)
            and value
            and (entry.get("provenance", {}).get(field, {}).get("kind") != "llm_inference")
            and _normalize(str(entry[field])) not in _normalize(str(value))
        ]
        if type_score == 0:
            identity_score = 0.0
        publication_date = str(candidate.publication_date or "")
        year_match = _YEAR.search(publication_date)
        pages = cls._first(payload, "page", "pages")
        if not pages and candidate.provider == "openalex":
            biblio = payload.get("biblio") or {}
            if isinstance(biblio, dict):
                first = biblio.get("first_page")
                last = biblio.get("last_page")
                pages = f"{first}-{last}" if first and last else first
        description = str(payload.get("description") or "")
        if not pages:
            description_pages = re.search(
                r"\bp\.\s*([0-9０-９]+(?:\s*[-–—〜～]\s*[0-9０-９]+)?)",
                description,
                re.I,
            )
            pages = description_pages.group(1) if description_pages else None
        identifiers = payload.get("identifiers") or []
        isbn = next(
            (
                str(item.get("value"))
                for item in identifiers
                if isinstance(item, dict)
                and "isbn" in str(item.get("type") or "").casefold()
                and item.get("value")
            ),
            None,
        )
        return {
            "candidate_id": f"{candidate.provider}:{candidate.provider_record_id}",
            "provider": candidate.provider,
            "provider_record_id": candidate.provider_record_id,
            "title": candidate.title,
            "authors": list(candidate.authors),
            "responsibility_source": (
                "ndl_search:dc_creator_mixed" if candidate.provider == "ndl_search"
                else f"{candidate.provider}:authors"
            ),
            "responsibility_raw": (
                list(payload.get("responsibility_raw") or payload.get("creators_raw") or [])
                if candidate.provider == "ndl_search" else list(candidate.authors)
            ),
            "original_title": cls._first(payload, "original_title", "alternative"),
            "original_title_relation_verified": (
                bool(payload.get("original_title")) and candidate.provider != "ndl_search"
            ),
            "translated_title": cls._first(payload, "translated_title"),
            "translators": payload.get("translators") or [],
            "publication_language": cls._first(payload, "language", "publication_language"),
            "publication_language_source_field": (
                "dc:language" if candidate.provider == "ndl_search" else "provider_payload"
            ),
            "is_translation": payload.get("is_translation"),
            "edition": cls._first(payload, "edition"),
            "editors": payload.get("editors") or [],
            "container_title": candidate.publication_title,
            "publisher": candidate.publisher,
            "publication_place": cls._first(payload, "publication_place", "place"),
            "series": cls._first(payload, "series", "series_title"),
            "year": year_match.group(1) if year_match else None,
            "volume": cls._first(payload, "volume"),
            "issue": cls._first(payload, "issue", "number"),
            "pages": str(pages).translate(_FULLWIDTH_DIGITS).replace(" ", "") if pages else None,
            "doi": candidate.doi,
            "isbn": isbn or cls._first(payload, "isbn"),
            "url": candidate.url,
            "record_type": candidate.record_type,
            "bibliography_kind": cls.bibliography_kind({"record_type": candidate.record_type}),
            "identity_score": identity_score,
            "identity_conflicts": conflicts,
            "title_similarity": title_score,
            "alias_only_match": direct_title_score < 0.70 and title_score >= 0.70,
            "edition_conflicts": [
                field
                for field in ("volume", "edition", "isbn")
                if entry.get(field)
                and payload.get(field)
                and _normalize(str(entry[field])) != _normalize(str(payload[field]))
            ],
            "identity_query_provenance": fielded,
            "score_components": {
                "title": round(title_score, 4),
                "author": round(author_score, 4),
                "type": round(type_score, 4),
            },
        }

    @classmethod
    def resolve_with_candidates(
        cls,
        entry: dict[str, Any],
        candidates: Iterable[LiteratureCandidate],
        *,
        workflow_version: int = 21,
    ) -> dict[str, Any]:
        if workflow_version >= 22:
            return cls.resolve_with_candidates_v22(
                entry, candidates, workflow_version=workflow_version
            )
        resolved = copy.deepcopy(entry)
        ranked = sorted(
            (cls.candidate_record(entry, candidate) for candidate in candidates),
            key=lambda item: (-float(item["identity_score"]), str(item["candidate_id"])),
        )
        ranked = list({str(item["candidate_id"]): item for item in ranked}.values())
        resolved["candidate_matches"] = ranked
        resolved["candidate_display"] = "／".join(cls.candidate_label(item) for item in ranked)
        if not ranked:
            resolved["resolution_status"] = "not_found"
            return resolved
        best = ranked[0]
        score = float(best["identity_score"])
        # Work/title similarity never settles the edition. Even apparently
        # identical records from different sources remain available for choice.
        close_candidates_conflict = len(ranked) > 1
        if score < 0.86:
            resolved["resolution_status"] = "low_match"
            resolved["work_identity_status"] = "unconfirmed"
            return resolved
        edition_conflicts = [
            field
            for field in ("publisher", "year", "volume", "edition", "isbn", "translators")
            if entry.get(field)
            and best.get(field)
            and _normalize(str(entry[field])) != _normalize(str(best[field]))
        ]
        if close_candidates_conflict or edition_conflicts or best.get("alias_only_match"):
            resolved.pop("selected_candidate_id", None)
            resolved["resolution_status"] = "ambiguous"
            resolved["work_identity_status"] = "matched"
            resolved["edition_status"] = "ambiguous"
            resolved["edition_conflicts"] = edition_conflicts
            return resolved
        for field in (
            "publisher",
            "original_title",
            "translated_title",
            "translators",
            "editors",
            "bibliography_kind",
            "edition",
            "publication_place",
            "publication_language",
            "series",
            "year",
            "container_title",
            "volume",
            "issue",
            "pages",
            "doi",
            "isbn",
            "url",
        ):
            value = best.get(field)
            if resolved.get(field) or value is None or value == "" or value == []:
                continue
            resolved[field] = value
            resolved.setdefault("provenance", {})[field] = _provenance(
                "retrieved_metadata",
                label="外部資料で確認",
                source=str(best["provider"]),
                source_url=str(best.get("url") or "") or None,
                provider_record_id=str(best["provider_record_id"]),
                confidence=score,
            )
        resolved["selected_candidate_id"] = best["candidate_id"]
        resolved["identity_confidence"] = score
        resolved["resolution_status"] = "resolved"
        resolved["work_identity_status"] = "matched"
        resolved["edition_status"] = "resolved"
        return resolved

    @classmethod
    def resolve_with_candidates_v22(
        cls,
        entry: dict[str, Any],
        candidates: Iterable[LiteratureCandidate],
        *,
        workflow_version: int = 22,
    ) -> dict[str, Any]:
        """Resolve work identity separately from a consulted manifestation.

        Raw provider hits are retained for audit, but only candidates that pass
        the work-identity gate can become selectable bibliography records.  A
        second, deterministic pass then deduplicates identical manifestations
        across providers before edition/translation/volume policy is applied.
        The function has no persistence or network side effects.
        """

        resolved = copy.deepcopy(entry)
        candidate_records = [cls.candidate_record(entry, candidate) for candidate in candidates]
        for item in candidate_records:
            item["bibliography_kind"] = cls._bibliography_kind_v22(item)
        raw_hit_total = len(candidate_records)
        raw_hits = cls._deduplicate_provider_hits(candidate_records)
        minimum = float(REFERENCE_RESOLUTION_POLICY_V22["minimum_identity_score"])
        for item in raw_hits:
            reasons = cls._identity_rejection_reasons(item, minimum=minimum)
            item["eligibility_status"] = "eligible_work" if not reasons else "excluded"
            item["exclusion_reasons"] = reasons

        eligible_raw = [item for item in raw_hits if item["eligibility_status"] == "eligible_work"]
        eligible_manifestations = cls._deduplicate_manifestations(eligible_raw)
        # Low-scoring bibliography hits are audit evidence, not related
        # literature.  The machine-detected section accepts only externally
        # validated evidence records through ``bibliography_sections_v22``.
        machine_detected: list[dict[str, Any]] = []

        scoped: list[dict[str, Any]] = []
        unverified_scope: list[dict[str, Any]] = []
        for item in eligible_manifestations:
            scope_issues = cls._manifestation_scope_issues(entry, item)
            item["manifestation_scope_issues"] = scope_issues
            item["translation_status"] = cls._translation_status(entry, item)
            item["translator_status"] = cls._translator_status(entry, item)
            item["required_missing_fields"] = cls._candidate_required_missing_fields(item)
            if scope_issues:
                item["manifestation_eligibility"] = "outside_or_unverified_explicit_scope"
                unverified_scope.append(item)
            else:
                item["manifestation_eligibility"] = "selectable"
                scoped.append(item)

        scoped.sort(key=cls._candidate_sort_key)
        eligible_manifestations.sort(key=cls._candidate_sort_key)
        raw_hits.sort(key=cls._candidate_sort_key)
        manifestation_axes = cls._manifestation_axis_state(entry)
        unspecified_axes = [
            axis for axis, state in manifestation_axes.items() if state == "unspecified"
        ]
        resolved.update(
            reference_policy_id=REFERENCE_RESOLUTION_POLICY_V22["id"],
            reference_role="consulted_or_cited",
            raw_hit_total=raw_hit_total,
            raw_candidate_hits=raw_hits,
            rejected_candidate_hits=[
                item for item in raw_hits if item["eligibility_status"] == "excluded"
            ],
            eligible_work_candidates=eligible_manifestations,
            unverified_scope_candidates=unverified_scope,
            candidate_matches=scoped,
            candidate_display="／".join(cls.candidate_label(item) for item in scoped),
            machine_detected_references=machine_detected,
            manifestation_axes=manifestation_axes,
            unspecified_manifestation_axes=unspecified_axes,
            manifestation_policy={
                "original_or_translation": "explicit_when_present_otherwise_all",
                "volume": "explicit_when_present_otherwise_complete_set",
                "edition": "explicit_when_present_otherwise_all",
                "automatic_current_edition_assumption": False,
            },
        )

        if not raw_hits:
            return cls._unresolved_v22_result(resolved, no_hits=True)
        if not eligible_manifestations:
            return cls._unresolved_v22_result(resolved, no_hits=False)

        resolved["work_identity_status"] = "matched"
        if not scoped:
            resolved.update(
                resolution_status="ambiguous",
                edition_status="specified_scope_unresolved",
                consulted_status="unconfirmed",
                manual_consulted_manifestation_allowed=True,
                final_revision_blocked=True,
                conditional_proposal_allowed=True,
                user_action_required={
                    "code": "select_or_enter_consulted_manifestation",
                    "fields": cls.manual_consulted_manifestation_fields_v22(
                        resolved
                    ),
                    "message": (
                        "原稿に明示された巻・版・訳者等と照合できる版がありません。"
                        "実際に参照した版を選択または入力してください。"
                    ),
                },
            )
            return resolved
        if unspecified_axes:
            expanded = [
                cls._manifestation_entry(entry, candidate, index=index)
                for index, candidate in enumerate(scoped)
            ]
            missing = sorted(
                {
                    field
                    for candidate in scoped
                    for field in cls._candidate_required_missing_fields(candidate)
                }
            )
            translator_statuses = {
                str(candidate.get("translator_status") or "undetermined")
                for candidate in scoped
            }
            translation_statuses = {
                str(candidate.get("translation_status") or "undetermined")
                for candidate in scoped
            }
            resolved.update(
                resolution_status="manifestations_retained",
                edition_status="all_unspecified_manifestations_retained",
                consulted_status="unconfirmed",
                expanded_reference_entries=expanded,
                rendered_manifestations=[
                    cls.render_entry(item, workflow_version=22) for item in expanded
                ],
                required_missing_fields=missing,
                translator_status=(
                    "required_missing"
                    if "required_missing" in translator_statuses
                    else "undetermined"
                    if "undetermined" in translator_statuses
                    else "verified_or_not_required"
                ),
                translation_status=(
                    next(iter(translation_statuses))
                    if len(translation_statuses) == 1
                    else "multiple_manifestation_states"
                ),
                final_revision_blocked=True,
                conditional_proposal_allowed=True,
                user_action_required={
                    "code": "confirm_consulted_manifestations",
                    "candidate_ids": [item["candidate_id"] for item in scoped],
                    "message": (
                        "一部のmanifestation軸が原稿で明示されていないため、"
                        "適格な全manifestationを独立項目として掲載しました。"
                        "最終Revisionの前に実際に参照した項目を確認してください。"
                    ),
                    "unspecified_axes": unspecified_axes,
                },
            )
            return resolved
        if len(scoped) > 1:
            resolved.update(
                resolution_status="ambiguous",
                edition_status="multiple_manifestations",
                final_revision_blocked=True,
                conditional_proposal_allowed=True,
                user_action_required={
                    "code": "select_consulted_manifestation",
                    "message": (
                        "原著・翻訳・巻・版の実在候補をすべて保持しています。"
                        "実際に参照した版を指定してください。"
                    ),
                },
            )
            return resolved

        selected = scoped[0]
        cls._apply_retrieved_candidate(
            resolved, selected, workflow_version=workflow_version
        )
        missing = cls._candidate_required_missing_fields(selected)
        resolved.update(
            selected_candidate_id=selected["candidate_id"],
            identity_confidence=float(selected["identity_score"]),
            edition_status="single_verified_manifestation",
            translator_status=selected["translator_status"],
            required_missing_fields=missing,
            conditional_proposal_allowed=bool(missing),
            final_revision_blocked=bool(missing),
        )
        if missing:
            resolved["resolution_status"] = "metadata_incomplete"
            resolved["user_action_required"] = {
                "code": "complete_required_bibliographic_metadata",
                "fields": missing,
                "message": "最終Revisionの前に必須書誌情報を確認してください。",
            }
        else:
            resolved["resolution_status"] = "resolved"
        return resolved

    @classmethod
    def reference_from_usage_v22(cls, source: dict[str, Any]) -> dict[str, Any]:
        """Create a bibliography record from a durable evidence-usage record.

        This conversion does not decide whether the source was consulted.  It
        preserves the classifier's disposition and records bibliographic
        metadata separately from retrieval/support provenance so the ordinary
        reference provenance validator does not mistake audit payloads for
        inferred fields.
        """

        usage = copy.deepcopy(source)
        retrieval = dict(usage.get("provenance") or {})
        citation = (
            dict(retrieval.get("citation") or {})
            if isinstance(retrieval.get("citation"), dict)
            else {}
        )
        authors = usage.get("authors") or citation.get("authors") or []
        if isinstance(authors, str):
            authors = [authors]
        authors = [str(value).strip() for value in authors if str(value).strip()]
        publication_date = str(
            usage.get("year")
            or retrieval.get("publication_date")
            or citation.get("publication_date")
            or citation.get("year")
            or ""
        )
        year_match = re.search(r"(?:18|19|20)\d{2}", publication_date)
        container_title = str(
            usage.get("container_title")
            or retrieval.get("publication_title")
            or citation.get("publication_title")
            or citation.get("container_title")
            or ""
        ).strip()
        doi = str(
            usage.get("doi") or retrieval.get("doi") or citation.get("doi") or ""
        ).strip()
        isbn = str(usage.get("isbn") or retrieval.get("isbn") or citation.get("isbn") or "").strip()
        source_type = str(usage.get("source_type") or "")
        record_type = str(
            usage.get("record_type")
            or citation.get("record_type")
            or retrieval.get("record_type")
            or (
                "article"
                if container_title or source_type == "online_paper"
                else "unknown"
            )
        )
        disposition = str(usage.get("disposition") or "machine_detected_related")
        reference_id = str(usage.get("reference_id") or "") or _stable_id(
            "evidence_usage",
            usage.get("usage_id"),
            usage.get("provider"),
            usage.get("provider_record_id"),
        )
        item: dict[str, Any] = {
            "reference_id": reference_id,
            "parse_status": "parsed",
            "reference_role": (
                "consulted_or_cited"
                if disposition == "consulted_or_cited"
                else "machine_detected_not_confirmed_consulted"
            ),
            "consulted_status": (
                "support_audit_selected"
                if disposition == "consulted_or_cited"
                else "not_consulted"
            ),
            "reference_usage_disposition": disposition,
            "reference_usage": {
                key: copy.deepcopy(value)
                for key, value in usage.items()
                if key
                in {
                    "policy_id",
                    "usage_id",
                    "candidate_key",
                    "query",
                    "finding_key",
                    "obligation_ids",
                    "selected_by_support_audit",
                    "passed_to_draft",
                    "support_frontier",
                    "support_frontier_reason",
                    "discovery_quality_passed",
                    "support_audits",
                    "evidence_id",
                    "excerpt_sha256",
                }
            },
            "provider": usage.get("provider"),
            "provider_record_id": usage.get("provider_record_id"),
            "title": str(usage.get("title") or citation.get("title") or "").strip(),
            "authors": authors,
            "author": "・".join(authors),
            "record_type": record_type,
            "bibliography_kind": cls._bibliography_kind_v22(
                {"record_type": record_type}
            ),
            "container_title": container_title or None,
            "publisher": (usage.get("publisher") or citation.get("publisher")
                          or retrieval.get("publisher")),
            "year": year_match.group(0) if year_match else None,
            "volume": usage.get("volume") or citation.get("volume"),
            "issue": usage.get("issue") or citation.get("issue"),
            "edition": usage.get("edition") or citation.get("edition"),
            "pages": usage.get("pages") or citation.get("pages"),
            "doi": doi or None,
            "isbn": isbn or None,
            "url": usage.get("source_url") or usage.get("url"),
            "original_title": usage.get("original_title")
            or citation.get("original_title"),
            "translated_title": usage.get("translated_title")
            or citation.get("translated_title"),
            "translators": list(
                usage.get("translators") or citation.get("translators") or []
            ),
            "publication_language": (
                usage.get("publication_language")
                or usage.get("language")
                or citation.get("publication_language")
                or citation.get("language")
            ),
            "is_translation": usage.get("is_translation")
            if isinstance(usage.get("is_translation"), bool)
            else citation.get("is_translation"),
            "retrieval_provenance": retrieval,
            "provider_records": [
                {
                    "provider": str(usage.get("provider") or ""),
                    "provider_record_id": str(usage.get("provider_record_id") or ""),
                    "url": usage.get("source_url") or usage.get("url"),
                }
            ],
        }
        item["translation_status"] = cls._translation_status({}, item)
        item["translator_status"] = cls._translator_status({}, item)
        missing = cls._candidate_required_missing_fields(item)
        item["required_missing_fields"] = missing
        if disposition == "consulted_or_cited":
            item.update(
                resolution_status="metadata_incomplete" if missing else "resolved",
                final_revision_blocked=bool(missing),
                conditional_proposal_allowed=bool(missing),
            )
        else:
            translator_missing = item["translator_status"] == "required_missing"
            translation_status_unconfirmed = (
                cls._is_book_record(item)
                and item["translator_status"] == "undetermined"
            )
            approval_missing = (
                ["translators"]
                if translator_missing
                else ["translation_status_confirmation"]
                if translation_status_unconfirmed
                else []
            )
            item.update(
                resolution_status=(
                    "metadata_incomplete"
                    if approval_missing
                    else "machine_detected"
                ),
                metadata_missing_fields=missing,
                required_missing_fields=approval_missing,
                final_revision_blocked=bool(approval_missing),
                conditional_proposal_allowed=bool(approval_missing),
                human_confirmation_required=True,
                inference_label="機械判別参考文献",
            )
            if approval_missing:
                item["user_action_required"] = {
                    "code": "confirm_machine_reference_translation_metadata",
                    "fields": approval_missing,
                    "message": (
                        "機械判別参考文献が原著か翻訳書かを確認し、"
                        "翻訳書であれば訳者を指定してください。"
                    ),
                }
        metadata_source = str(usage.get("provider") or "evidence_retrieval")
        item["provenance"] = {
            field: _provenance(
                "support_selected_evidence"
                if disposition == "consulted_or_cited"
                else "machine_detected_evidence",
                label=(
                    "論証根拠として使用"
                    if disposition == "consulted_or_cited"
                    else "検索候補として機械判別"
                ),
                source=metadata_source,
                source_url=str(item.get("url") or "") or None,
                provider_record_id=str(item.get("provider_record_id") or "") or None,
                confidence=float(usage.get("confidence") or 0.0),
            )
            for field in (
                "title",
                "authors",
                "container_title",
                "publisher",
                "year",
                "volume",
                "issue",
                "edition",
                "pages",
                "doi",
                "isbn",
                "original_title",
                "translated_title",
                "translators",
                "is_translation",
            )
            if item.get(field) not in (None, "", [])
        }
        return item

    @classmethod
    def _reference_manifestation_identity_v22(
        cls, entry: dict[str, Any]
    ) -> tuple[str, str]:
        """Return an exact manifestation identity, never a title-only work key."""

        manifestation_suffix = "|".join(
            [
                _normalize(str(entry.get(field) or ""))
                for field in (
                    "volume",
                    "issue",
                    "edition",
                    "original_title",
                    "translated_title",
                )
            ]
            + [
                str(entry.get("translation_status") or ""),
                "|".join(
                    sorted(
                        _normalize(str(value))
                        for value in entry.get("translators") or []
                    )
                ),
            ]
        )
        doi = cls._normalized_doi(entry.get("doi"))
        if doi:
            return ("doi", doi + "|" + manifestation_suffix)
        isbn = cls._normalized_isbn(entry.get("isbn"))
        if isbn:
            return ("isbn", isbn + "|" + manifestation_suffix)
        title = _normalize(str(entry.get("title") or ""))
        authors = "|".join(
            sorted(
                _normalize(str(value))
                for value in (
                    entry.get("authors")
                    or ([entry.get("author")] if entry.get("author") else [])
                )
                if _normalize(str(value))
            )
        )
        translation_status = str(entry.get("translation_status") or "")
        if translation_status in {"undetermined", "multiple_manifestation_states"}:
            translation_status = ""
        manifestation_axes = [
            _normalize(str(entry.get(field) or ""))
            for field in (
                "year",
                "publisher",
                "container_title",
                "volume",
                "issue",
                "edition",
                "original_title",
                "translated_title",
            )
        ]
        manifestation_axes.append(_normalize(translation_status))
        manifestation_axes.append(
            "|".join(
                sorted(
                    _normalize(str(value))
                    for value in entry.get("translators") or []
                    if _normalize(str(value))
                )
            )
        )
        # A title/author alone establishes only a work.  Without at least one
        # manifestation axis, retain provider records independently rather than
        # risk merging an original, translation, volume, or edition.
        if title and authors and any(manifestation_axes):
            return (
                "manifestation",
                "|".join(
                    [cls._bibliography_kind_v22(entry), title, authors, *manifestation_axes]
                ),
            )
        provider_identity = "|".join(
            str(entry.get(field) or "")
            for field in ("provider", "provider_record_id", "url", "reference_id")
        )
        return ("provider", provider_identity)

    @classmethod
    def _deduplicate_reference_records_v22(
        cls, records: Iterable[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Deduplicate records with the same complete-link manifestation rule."""

        groups: list[list[dict[str, Any]]] = []
        for source in records:
            record = copy.deepcopy(source)
            group = next(
                (
                    candidate_group
                    for candidate_group in groups
                    if all(
                        cls._same_manifestation(record, member)
                        for member in candidate_group
                    )
                ),
                None,
            )
            if group is None:
                groups.append([record])
            else:
                group.append(record)
        deduplicated: list[dict[str, Any]] = []
        for group in groups:
            merged = group[0]
            for duplicate in group[1:]:
                merged = cls._merge_usage_reference_v22(merged, duplicate)
            deduplicated.append(merged)
        return deduplicated

    @classmethod
    def _merge_usage_reference_v22(
        cls,
        current: dict[str, Any],
        incoming: dict[str, Any],
        *,
        preserve_current_approval: bool = False,
    ) -> dict[str, Any]:
        merged = copy.deepcopy(current)
        metadata_supplements: dict[str, Any] = {}
        metadata_conflicts: dict[str, dict[str, Any]] = {}
        for field in (
            "author",
            "authors",
            "container_title",
            "publisher",
            "year",
            "volume",
            "issue",
            "edition",
            "pages",
            "doi",
            "isbn",
            "url",
            "original_title",
            "translated_title",
            "translators",
            "is_translation",
            "publication_language",
        ):
            current_value, incoming_value = merged.get(field), incoming.get(field)
            if incoming_value in (None, "", []):
                continue
            if current_value in (None, "", []):
                if preserve_current_approval:
                    metadata_supplements[field] = copy.deepcopy(incoming_value)
                else:
                    merged[field] = copy.deepcopy(incoming_value)
                continue
            current_values = cls._normalized_field_values(current_value)
            incoming_values = cls._normalized_field_values(incoming_value)
            if current_values != incoming_values:
                metadata_conflicts[field] = {
                    "consulted_value": copy.deepcopy(current_value),
                    "machine_value": copy.deepcopy(incoming_value),
                }
        merged["provider_records"] = list(
            {
                (
                    str(item.get("provider") or ""),
                    str(item.get("provider_record_id") or ""),
                    str(item.get("url") or ""),
                ): copy.deepcopy(item)
                for item in [
                    *(merged.get("provider_records") or []),
                    *(incoming.get("provider_records") or []),
                ]
                if isinstance(item, dict)
            }.values()
        )
        usages = [
            item
            for item in [merged.get("reference_usage"), incoming.get("reference_usage")]
            if isinstance(item, dict)
        ]
        if usages:
            merged["reference_usages"] = list(
                {
                    str(item.get("usage_id") or index): copy.deepcopy(item)
                    for index, item in enumerate(
                        [*(merged.get("reference_usages") or []), *usages]
                    )
                }.values()
            )
        if preserve_current_approval:
            reconciliation = {
                "incoming_reference_id": incoming.get("machine_reference_id")
                or incoming.get("reference_id"),
                "incoming_provider_records": copy.deepcopy(
                    incoming.get("provider_records") or []
                ),
                "metadata_supplements": metadata_supplements,
                "metadata_conflicts": metadata_conflicts,
                "incoming_final_revision_blocked": bool(
                    incoming.get("final_revision_blocked")
                ),
                "classification_action": "promoted_to_consulted_without_approval_downgrade",
            }
            merged["machine_reconciliation_records"] = [
                *(merged.get("machine_reconciliation_records") or []),
                reconciliation,
            ]
        else:
            merged["final_revision_blocked"] = bool(
                merged.get("final_revision_blocked")
                or incoming.get("final_revision_blocked")
            )
            if (
                merged.get("translator_status") != "required_missing"
                and incoming.get("translator_status") == "required_missing"
            ):
                merged["translator_status"] = "required_missing"
        return merged

    @classmethod
    def bibliography_sections_v22(
        cls,
        entries: Iterable[dict[str, Any]],
        *,
        machine_entries: Iterable[dict[str, Any]] = (),
        workflow_version: int = 22,
    ) -> list[dict[str, Any]]:
        """Return render-ready consulted and machine-detected reference groups."""

        source_entries = [copy.deepcopy(entry) for entry in entries]
        consulted: list[dict[str, Any]] = []
        for entry in source_entries:
            expanded = entry.get("expanded_reference_entries") or []
            if workflow_version >= 29 and entry.get("reference_output_mode_v29") in {
                "MULTIPLE", "REVIEW"
            } and expanded:
                consulted.extend(copy.deepcopy(expanded))
            elif workflow_version >= 29 and expanded:
                confirmed = [
                    item for item in expanded
                    if item.get("consulted_status") == "user_confirmed"
                ]
                # Unconfirmed edition matches are search evidence, not works
                # proven to have been consulted. Keep the cited source row.
                consulted.extend(copy.deepcopy(confirmed) if confirmed else [entry])
            elif expanded:
                consulted.extend(copy.deepcopy(expanded))
            else:
                consulted.append(entry)
        consulted = cls._deduplicate_reference_records_v22(consulted)
        discovered: list[dict[str, Any]] = []
        for source in machine_entries:
            item = cls._external_machine_reference(source)
            consulted_index = next(
                (
                    index
                    for index, consulted_item in enumerate(consulted)
                    if cls._same_manifestation(item, consulted_item)
                ),
                None,
            )
            if consulted_index is not None:
                consulted[consulted_index] = cls._merge_usage_reference_v22(
                    consulted[consulted_index],
                    item,
                    preserve_current_approval=True,
                )
                continue
            discovered.append(item)
        discovered = cls._deduplicate_reference_records_v22(discovered)
        sections = [
            {
                "section_key": "consulted_references",
                "title": "参考文献",
                "reference_role": "consulted_or_cited",
                "entries": consulted,
                "source_entries": source_entries,
                "rendered_entries": [
                    cls.render_entry(entry, workflow_version=workflow_version)
                    for entry in consulted
                ],
            }
        ]
        if discovered:
            discovered = sorted(discovered, key=cls._candidate_sort_key)
            sections.append(
                {
                    "section_key": "machine_detected_references",
                    "title": str(
                        REFERENCE_RESOLUTION_POLICY_V22["discovered_only_section_title"]
                    ),
                    "reference_role": "machine_detected_not_confirmed_consulted",
                    "disclaimer": (
                        "検索により関連候補として機械判別された文献であり、"
                        "実際に参照したことを意味しません。"
                    ),
                    "requires_human_confirmation": True,
                    "entries": discovered,
                    "rendered_entries": [
                        cls.render_machine_detected_entry_v22(
                            entry, workflow_version=workflow_version
                        )
                        for entry in discovered
                    ],
                }
            )
        return sections

    @classmethod
    def machine_manifestations_from_resolution_v22(
        cls, source: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Return every retained manifestation for a machine-only work.

        Machine-detected related literature is not a consulted-edition choice.
        It therefore follows the same work/manifestation discovery path as an
        ordinary reference, but every eligible original, translation, volume,
        and edition remains an independent machine-only record.  In
        particular, an ambiguous result must not be collapsed back to its work
        stub merely because no human has selected one manifestation.
        """

        expanded = [
            copy.deepcopy(item)
            for item in source.get("expanded_reference_entries") or []
            if isinstance(item, dict)
        ]
        if not expanded:
            candidates = [
                item
                for item in source.get("candidate_matches") or []
                if isinstance(item, dict)
            ]
            expanded = [
                cls._manifestation_entry(source, item, index=index)
                for index, item in enumerate(candidates)
            ]
        if not expanded:
            expanded = [copy.deepcopy(source)]

        records: list[dict[str, Any]] = []
        for item in expanded:
            item["reference_role"] = "machine_detected_not_confirmed_consulted"
            item["consulted_status"] = "not_consulted"
            records.append(item)
        return records

    @classmethod
    def _external_machine_reference(cls, source: dict[str, Any]) -> dict[str, Any]:
        """Normalize an admitted machine-only record by exact manifestation."""

        item = (
            cls.reference_from_usage_v22(source)
            if source.get("policy_id") == "reference-usage-v22"
            and not source.get("reference_id")
            else copy.deepcopy(source)
        )
        if isinstance(item.get("authors"), str):
            item["authors"] = [str(item["authors"])]
        if not item.get("author") and item.get("authors"):
            item["author"] = "・".join(item["authors"])
        if not item.get("parse_status"):
            item["parse_status"] = "parsed"
        if not item.get("record_type") and not item.get("bibliography_kind"):
            item["record_type"] = "unknown"
        item["bibliography_kind"] = cls._bibliography_kind_v22(item)
        item.setdefault("translation_status", cls._translation_status({}, item))
        item.setdefault("translator_status", cls._translator_status({}, item))
        item.setdefault(
            "provider_records",
            [
                {
                    "provider": str(item.get("provider") or ""),
                    "provider_record_id": str(item.get("provider_record_id") or ""),
                    "url": item.get("url"),
                }
            ],
        )
        manifestation_axes, approval_missing, identity_evidence = (
            cls.machine_manifestation_requirements_v22(item)
        )
        identity = cls._reference_manifestation_identity_v22(item)
        item.update(
            machine_reference_id=_stable_id(
                "validated_evidence_manifestation", identity[0], identity[1]
            ),
            reference_role="machine_detected_not_confirmed_consulted",
            consulted_status="not_consulted",
            human_confirmation_required=True,
            inference_label="機械判別参考文献",
            manifestation_axes=manifestation_axes,
            manifestation_identity_evidence=identity_evidence,
            final_revision_blocked=bool(
                approval_missing
            ),
            conditional_proposal_allowed=bool(
                approval_missing
            ),
            required_missing_fields=approval_missing,
        )
        if approval_missing:
            item.setdefault(
                "metadata_missing_fields",
                list(item.get("required_missing_fields") or []),
            )
            item["resolution_status"] = "metadata_incomplete"
            item["user_action_required"] = {
                "code": "confirm_machine_reference_manifestation_metadata",
                "fields": approval_missing,
                "message": (
                    "機械判別参考文献の原著／翻訳・訳者・版・巻のうち、"
                    "未確定の項目を確認してください。"
                ),
            }
        else:
            item.pop("user_action_required", None)
        return item

    @classmethod
    def machine_manifestation_requirements_v22(
        cls, entry: dict[str, Any]
    ) -> tuple[dict[str, str], list[str], dict[str, Any]]:
        """Recompute machine-only manifestation state without guessing a used edition.

        An absent edition or volume is not itself an instruction to select the
        currently available one.  When the search frontier supplied no explicit
        scope conflict, ``all_known_manifestations_retained`` is allowed only when
        a durable search-completeness record names the retained candidates.  A lone
        blank field is never treated as proof of exhaustive search.  Conversely, a
        manifestation-level identifier may identify one manifestation without
        inventing an edition label, but only when durable evidence also records
        whether that identifier is edition-, volume-, or item-specific.  A bare
        ISBN and a year/publisher tuple remain useful context, not resolution.
        """

        prior_axes = dict(entry.get("manifestation_axes") or {})
        prior_missing = {
            str(value)
            for value in [
                *(entry.get("required_missing_fields") or []),
                *(entry.get("manifestation_missing_fields") or []),
            ]
            if str(value)
        }
        unresolved_states = {
            "ambiguous",
            "not_evaluated",
            "pending",
            "specified_scope_unresolved",
            "unknown",
            "unresolved",
        }
        axes: dict[str, str] = {}
        missing: list[str] = []
        identity_evidence: dict[str, Any] = {}
        prior_identity_evidence = (
            dict(entry.get("manifestation_identity_evidence") or {})
            if isinstance(entry.get("manifestation_identity_evidence"), dict)
            else {}
        )
        identity_basis: list[str] = []
        if str(entry.get("isbn") or "").strip():
            identity_basis.append("isbn")
        if str(entry.get("doi") or "").strip():
            identity_basis.append("doi")
        if str(entry.get("year") or "").strip() and any(
            str(entry.get(field) or "").strip()
            for field in ("publisher", "container_title")
        ):
            identity_basis.extend(
                field
                for field in ("year", "publisher", "container_title")
                if str(entry.get(field) or "").strip()
            )
        identity_basis = list(dict.fromkeys(identity_basis))
        identity_basis_values = {
            field: copy.deepcopy(entry.get(field)) for field in identity_basis
        }
        bibliography_kind = cls._bibliography_kind_v22(entry)
        known_non_book = bibliography_kind in {"article", "chapter"}
        scope_evidence = (
            dict(entry.get("manifestation_scope_evidence") or {})
            if isinstance(entry.get("manifestation_scope_evidence"), dict)
            else {}
        )
        for axis, field in (("edition", "edition"), ("volume", "volume")):
            value = str(entry.get(field) or "").strip()
            prior_state = str(
                prior_axes.get(axis) or entry.get(f"{axis}_status") or ""
            ).strip()
            prior_axis_evidence = (
                dict(prior_identity_evidence.get(axis) or {})
                if isinstance(prior_identity_evidence.get(axis), dict)
                else {}
            )
            prior_unresolved_was_computed = (
                prior_axis_evidence.get("reason")
                == "insufficient_manifestation_identity_metadata"
            )
            axis_scope = scope_evidence.get(axis)
            eligible_manifestation_ids = (
                list(
                    dict.fromkeys(
                        str(candidate_id).strip()
                        for candidate_id in axis_scope.get(
                            "eligible_manifestation_ids", []
                        )
                        if str(candidate_id).strip()
                    )
                )
                if isinstance(axis_scope, dict)
                and isinstance(axis_scope.get("eligible_manifestation_ids"), list)
                else []
            )
            identifier_type = (
                str(axis_scope.get("identifier_type") or "").strip().casefold()
                if isinstance(axis_scope, dict)
                else ""
            )
            identifier_value = (
                str(axis_scope.get("identifier_value") or "").strip()
                if isinstance(axis_scope, dict)
                else ""
            )
            identifier_scope = (
                str(axis_scope.get("identifier_scope") or "").strip().casefold()
                if isinstance(axis_scope, dict)
                else ""
            )
            verification_source = (
                str(axis_scope.get("verification_source") or "").strip()
                if isinstance(axis_scope, dict)
                else ""
            )
            record_identifier = str(entry.get(identifier_type) or "").strip()
            if identifier_type == "isbn":
                normalized_identifier = cls._normalized_isbn(identifier_value)
                normalized_record_identifier = cls._normalized_isbn(record_identifier)
            elif identifier_type == "doi":
                normalized_identifier = cls._normalized_doi(identifier_value)
                normalized_record_identifier = cls._normalized_doi(record_identifier)
            else:
                normalized_identifier = identifier_value
                normalized_record_identifier = record_identifier
            allowed_identifier_scopes = {
                "edition": {"single_manifestation", "edition_specific"},
                "volume": {"single_manifestation", "volume_specific"},
            }
            manifestation_identifier_proven = (
                isinstance(axis_scope, dict)
                and axis_scope.get("identifier_verified") is True
                and identifier_type in {"isbn", "doi", "provider_record_id"}
                and bool(normalized_identifier)
                and normalized_identifier == normalized_record_identifier
                and identifier_scope in allowed_identifier_scopes[axis]
                and bool(verification_source)
            )
            complete_set_proven = (
                isinstance(axis_scope, dict)
                and axis_scope.get("complete_set_retained") is True
                and bool(str(axis_scope.get("search_signature") or "").strip())
                and bool(eligible_manifestation_ids)
            )
            if value:
                axes[axis] = "identified"
                identity_evidence[axis] = {
                    "status": "identified",
                    "basis_fields": [field],
                    "basis_values": {field: copy.deepcopy(entry.get(field))},
                    "source": "explicit_manifestation_metadata",
                }
            elif axis == "edition" and known_non_book:
                axes[axis] = "not_applicable"
                identity_evidence[axis] = {
                    "status": "not_applicable",
                    "record_kind": bibliography_kind,
                    "source": "reference_type_policy",
                }
            elif complete_set_proven:
                axes[axis] = "all_known_manifestations_retained"
                identity_evidence[axis] = {
                    "status": "all_known_manifestations_retained",
                    "search_signature": str(axis_scope["search_signature"]),
                    "eligible_manifestation_ids": eligible_manifestation_ids,
                    "source": "exhaustive_search_scope_checkpoint",
                }
            elif manifestation_identifier_proven:
                axes[axis] = "identified_by_manifestation_identifier"
                identity_evidence[axis] = {
                    "status": "identified_by_manifestation_identifier",
                    "identifier_type": identifier_type,
                    "identifier_value": identifier_value,
                    "identifier_scope": identifier_scope,
                    "verification_source": verification_source,
                    "source": "verified_manifestation_identifier",
                }
            elif (
                axis == "volume"
                and known_non_book
                and bool(cls._normalized_doi(entry.get("doi")))
            ):
                axes[axis] = "identified_by_manifestation_identifier"
                identity_evidence[axis] = {
                    "status": "identified_by_manifestation_identifier",
                    "identifier_type": "doi",
                    "identifier_value": str(entry.get("doi") or "").strip(),
                    "identifier_scope": "single_manifestation",
                    "verification_source": "doi_record",
                    "source": "intrinsically_manifestation_level_identifier",
                }
            elif field in prior_missing or prior_state in unresolved_states:
                axes[axis] = "unresolved"
                missing.append(field)
                identity_evidence[axis] = {
                    "status": "unresolved",
                    "reason": (
                        "insufficient_manifestation_identity_metadata"
                        if prior_unresolved_was_computed
                        else "explicit_unresolved_manifestation_axis"
                    ),
                    "observed_basis_fields": identity_basis,
                    "observed_basis_values": identity_basis_values,
                }
            else:
                axes[axis] = "unresolved"
                missing.append(field)
                identity_evidence[axis] = {
                    "status": "unresolved",
                    "reason": "insufficient_manifestation_identity_metadata",
                    "observed_basis_fields": identity_basis,
                    "observed_basis_values": identity_basis_values,
                }

        translation_status = str(
            entry.get("translation_status") or cls._translation_status({}, entry)
        )
        translator_status = str(
            entry.get("translator_status") or cls._translator_status({}, entry)
        )
        if translation_status in {"confirmed_original", "confirmed_translation"}:
            axes["original_or_translation"] = "identified"
            identity_evidence["original_or_translation"] = {
                "status": "identified",
                "basis_fields": ["is_translation", "translation_status"],
                "basis_values": {
                    "is_translation": entry.get("is_translation"),
                    "translation_status": translation_status,
                },
                "source": "confirmed_translation_metadata",
            }
        elif cls._is_book_record(entry):
            axes["original_or_translation"] = "unresolved"
            missing.append("translation_status_confirmation")
            identity_evidence["original_or_translation"] = {
                "status": "unresolved",
                "reason": "translation_status_unconfirmed",
            }
        else:
            axes["original_or_translation"] = "not_applicable"
            identity_evidence["original_or_translation"] = {
                "status": "not_applicable",
                "source": "non_book_translation_policy",
            }
        if translation_status == "confirmed_translation" and not (
            entry.get("translators") or []
        ):
            translator_status = "required_missing"
        if translator_status == "required_missing":
            missing.append("translators")

        def field_present(field: str) -> bool:
            if field == "authors":
                return bool(entry.get("authors") or entry.get("author"))
            if field == "translation_status_confirmation":
                return translation_status in {
                    "confirmed_original",
                    "confirmed_translation",
                }
            return bool(str(entry.get(field) or "").strip())

        # Preserve explicit non-axis blockers supplied by an earlier resolver.
        for field in prior_missing:
            if field in {"edition", "volume", "translation_status_confirmation", "translators"}:
                continue
            if not field_present(field):
                missing.append(field)
        return axes, list(dict.fromkeys(missing)), identity_evidence

    @classmethod
    def self_or_unpublished_required_metadata_v22(
        cls, entry: dict[str, Any]
    ) -> list[str]:
        """Map the user-facing self/unpublished contract to concrete fields."""

        raw_kind = str(entry.get("record_type") or entry.get("bibliography_kind") or "")
        normalized_kind = raw_kind.casefold()
        known_kinds = {
            "book",
            "monograph",
            "article",
            "journal-article",
            "article_poem_or_chapter",
            "chapter",
            "book-chapter",
            "book-section",
            "unpublished",
            "未刊原稿",
        }
        missing: list[str] = []
        if normalized_kind not in {value.casefold() for value in known_kinds}:
            missing.append("document_type")
        if not str(entry.get("year") or "").strip():
            missing.append("publication_year")
        if not any(
            str(entry.get(field) or "").strip()
            for field in ("venue", "container_title", "publisher")
        ):
            missing.append("venue_or_publisher")
        bibliography_kind = cls._bibliography_kind_v22(entry)
        if bibliography_kind == "article" and not any(
            str(entry.get(field) or "").strip() for field in ("volume", "issue")
        ):
            missing.append("volume_issue_if_applicable")
        if bibliography_kind in {"article", "chapter"} and not str(
            entry.get("pages") or ""
        ).strip():
            missing.append("pages_if_applicable")
        return missing

    @classmethod
    def _unresolved_v22_result(
        cls, resolved: dict[str, Any], *, no_hits: bool
    ) -> dict[str, Any]:
        if cls._is_self_authored_or_unpublished(resolved):
            user_missing = cls.self_or_unpublished_required_metadata_v22(resolved)
            labels = {
                "document_type": "資料種別",
                "publication_year": "発表年",
                "venue_or_publisher": "掲載先または出版社",
                "volume_issue_if_applicable": "該当する巻号",
                "pages_if_applicable": "該当するページ",
            }
            if user_missing:
                missing_label = "、".join(labels.get(field, field) for field in user_missing)
                action_message = (
                    "外部DBで同定できない自著・未刊資料です。"
                    f"未確認の{missing_label}を指定してください。"
                )
            else:
                action_message = (
                    "外部DBで同定できない自著・未刊資料です。"
                    "登録済みの書誌情報と分類を確認してください。"
                )
            resolved.update(
                resolution_status="user_input_required",
                work_identity_status="external_identity_unavailable",
                edition_status="user_metadata_required",
                user_required_metadata=user_missing,
                user_action_required={
                    "code": "describe_self_authored_or_unpublished_material",
                    "fields": user_missing,
                    "message": action_message,
                },
            )
        else:
            manual_fields = cls.manual_consulted_manifestation_fields_v22(resolved)
            if manual_fields:
                resolved.update(
                    resolution_status="specified_scope_unresolved",
                    work_identity_status=(
                        "no_raw_hits" if no_hits else "no_eligible_identity"
                    ),
                    edition_status="specified_scope_unresolved",
                    consulted_status="unconfirmed",
                    manual_consulted_manifestation_allowed=True,
                    user_action_required={
                        "code": "enter_consulted_manifestation",
                        "fields": manual_fields,
                        "message": (
                            "原稿に明示された版・巻等と一致する検索候補がありません。"
                            "実際に参照したmanifestationの書誌情報を入力してください。"
                        ),
                    },
                )
            else:
                resolved.update(
                    resolution_status="not_found" if no_hits else "low_match",
                    work_identity_status=(
                        "no_raw_hits" if no_hits else "no_eligible_identity"
                    ),
                    edition_status="not_evaluated",
                    user_action_required={
                        "code": "identify_work_or_correct_query",
                        "message": (
                            "著者・著作を確認し、識別子または正しい書誌情報を入力してください。"
                        ),
                    },
                )
        resolved["conditional_proposal_allowed"] = True
        resolved["final_revision_blocked"] = True
        return resolved

    @classmethod
    def manual_consulted_manifestation_fields_v22(
        cls, entry: dict[str, Any]
    ) -> list[str]:
        """Return source-explicit manifestation fields that justify manual entry.

        This is deliberately narrower than a generic unresolved bibliography
        record.  A source that explicitly names a volume, edition, translation,
        translator, or manifestation identifier describes an actually consulted
        item even when bounded provider search cannot reproduce that exact item.
        It may therefore be completed manually without falsely classifying it as
        self-authored or unpublished.
        """

        fields = (
            "doi",
            "isbn",
            "is_translation",
            "original_title",
            "translated_title",
            "translators",
            "volume",
            "edition",
        )
        return [field for field in fields if cls._field_is_explicit(entry, field)]

    @staticmethod
    def _is_self_authored_or_unpublished(entry: dict[str, Any]) -> bool:
        if entry.get("is_self_authored") is True or entry.get("is_unpublished") is True:
            return True
        author = _normalize(str(entry.get("author") or ""))
        if author in {_normalize(value) for value in ("自著", "筆者", "本稿筆者", "著者本人")}:
            return True
        record_type = _normalize(
            str(entry.get("bibliography_kind") or entry.get("record_type") or "")
        )
        if record_type in {
            _normalize(value)
            for value in ("unpublished", "unpublished manuscript", "未刊原稿", "未公刊資料")
        }:
            return True
        declaration = " ".join(
            str(entry.get(field) or "") for field in ("original_text", "title")
        )
        return bool(re.search(r"(?:未刊(?:行)?|未公刊|未発表|未出版|草稿)", declaration))

    @staticmethod
    def _candidate_sort_key(item: dict[str, Any]) -> tuple[float, str, str]:
        return (
            -float(item.get("identity_score") or 0.0),
            str(item.get("title") or ""),
            str(item.get("candidate_id") or ""),
        )

    @classmethod
    def _deduplicate_provider_hits(
        cls, records: Iterable[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            grouped.setdefault(str(record.get("candidate_id") or ""), []).append(record)
        result = []
        for candidate_id, duplicates in grouped.items():
            duplicates.sort(key=cls._candidate_sort_key)
            chosen = copy.deepcopy(duplicates[0])
            chosen["candidate_id"] = candidate_id
            chosen["raw_hit_count"] = len(duplicates)
            result.append(chosen)
        result.sort(key=cls._candidate_sort_key)
        return result

    @staticmethod
    def _identity_rejection_reasons(
        candidate: dict[str, Any], *, minimum: float
    ) -> list[str]:
        reasons: list[str] = []
        if not _normalize(str(candidate.get("title") or "")):
            reasons.append("title_missing")
        if float((candidate.get("score_components") or {}).get("type") or 0.0) <= 0.0:
            reasons.append("record_type_mismatch")
        if float(candidate.get("identity_score") or 0.0) < minimum:
            reasons.append("identity_score_below_eligible_threshold")
        return reasons

    @classmethod
    def _deduplicate_manifestations(
        cls, records: Iterable[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        records = [copy.deepcopy(item) for item in records]
        if not records:
            return []
        records.sort(key=cls._candidate_sort_key)
        groups: list[list[dict[str, Any]]] = []
        for record in records:
            # Complete-link clustering is deliberate.  Pairwise union-find can
            # collapse an initial edition and a revised edition through a third
            # provider hit that omits the edition field (A~B and B~C, although
            # A and C conflict).  A record joins a manifestation only when it is
            # compatible with every record already in that group.
            group = next(
                (
                    candidate_group
                    for candidate_group in groups
                    if all(
                        cls._same_manifestation(record, member)
                        for member in candidate_group
                    )
                ),
                None,
            )
            if group is None:
                groups.append([record])
            else:
                group.append(record)
        merged = [cls._merge_manifestation_group(group) for group in groups]
        merged.sort(key=cls._candidate_sort_key)
        return merged

    @classmethod
    def _same_manifestation(cls, left: dict[str, Any], right: dict[str, Any]) -> bool:
        left_doi, right_doi = cls._normalized_doi(left.get("doi")), cls._normalized_doi(
            right.get("doi")
        )
        left_isbn, right_isbn = cls._normalized_isbn(left.get("isbn")), cls._normalized_isbn(
            right.get("isbn")
        )
        # Different strong identifiers are different manifestations even when a
        # provider gives them the same work title/year.  Conversely, a shared
        # identifier may merge provider duplicates only when no explicit
        # manifestation axis contradicts it.  Some provider records expose a
        # work-level DOI for several volumes/editions, so the identifier alone
        # must not erase a clear volume, edition, or translation distinction.
        if left_doi and right_doi and left_doi != right_doi:
            return False
        if left_isbn and right_isbn and left_isbn != right_isbn:
            return False
        manifestation_conflicts = cls._manifestation_conflicts(left, right)
        if (left_doi and left_doi == right_doi) or (
            left_isbn and left_isbn == right_isbn
        ):
            return not manifestation_conflicts
        if manifestation_conflicts:
            return False
        left_fingerprint = cls._manifestation_fingerprint(left)
        if left_fingerprint and left_fingerprint == cls._manifestation_fingerprint(right):
            return True
        if _normalize(str(left.get("title") or "")) != _normalize(
            str(right.get("title") or "")
        ):
            return False
        left_authors = {
            _normalize(str(value))
            for value in (
                left.get("authors")
                or ([left.get("author")] if left.get("author") else [])
            )
        }
        right_authors = {
            _normalize(str(value))
            for value in (
                right.get("authors")
                or ([right.get("author")] if right.get("author") else [])
            )
        }
        if left_authors and right_authors and not left_authors.intersection(right_authors):
            return False
        matched_fields: set[str] = set()
        for field in (
            "year",
            "publisher",
            "volume",
            "issue",
            "edition",
            "original_title",
            "translated_title",
            "is_translation",
            "translators",
        ):
            left_value = left.get(field)
            right_value = right.get(field)
            if not left_value or not right_value:
                continue
            if isinstance(left_value, list) or isinstance(right_value, list):
                left_normalized = {
                    _normalize(str(value)) for value in left_value if _normalize(str(value))
                }
                right_normalized = {
                    _normalize(str(value)) for value in right_value if _normalize(str(value))
                }
                if left_normalized != right_normalized:
                    return False
            elif _normalize(str(left_value)) != _normalize(str(right_value)):
                return False
            matched_fields.add(field)
        return {"year", "publisher"}.issubset(matched_fields) or (
            "year" in matched_fields and bool(matched_fields.intersection({"volume", "edition"}))
        )

    @classmethod
    def _manifestation_conflicts(
        cls, left: dict[str, Any], right: dict[str, Any]
    ) -> bool:
        """Return whether two records explicitly describe different manifestations.

        Missing provider metadata is not a conflict.  The comparison is
        intentionally conservative where both sides do have data: different
        volume/edition axes, incompatible original or translated titles, an
        original-versus-translation distinction, or disjoint translator sets
        must remain available as separate choices.
        """

        for field in ("volume", "issue", "edition"):
            left_value, right_value = left.get(field), right.get(field)
            if (
                left_value not in (None, "")
                and right_value not in (None, "")
                and _normalize(str(left_value)) != _normalize(str(right_value))
            ):
                return True
        for field in ("original_title", "translated_title"):
            left_value, right_value = str(left.get(field) or ""), str(right.get(field) or "")
            if left_value and right_value and cls._title_score(left_value, right_value) < 0.86:
                return True
        left_translation = cls._translation_status({}, left)
        right_translation = cls._translation_status({}, right)
        if (
            left_translation != "undetermined"
            and right_translation != "undetermined"
            and left_translation != right_translation
        ):
            return True
        left_translators = cls._normalized_field_values(left.get("translators") or [])
        right_translators = cls._normalized_field_values(right.get("translators") or [])
        return bool(
            left_translators
            and right_translators
            and left_translators.isdisjoint(right_translators)
        )

    @classmethod
    def _manifestation_fingerprint(cls, item: dict[str, Any]) -> str:
        # Without year/publisher/volume/edition, equal titles only establish a
        # work, not an edition; keep those records separate for human review.
        edition_fields = [
            item.get(key)
            for key in ("year", "publisher", "volume", "issue", "edition")
        ]
        if not any(edition_fields):
            return ""
        values = [
            cls._bibliography_kind_v22(item),
            _normalize(str(item.get("title") or "")),
            "|".join(
                sorted(
                    _normalize(str(value))
                    for value in (
                        item.get("authors")
                        or ([item.get("author")] if item.get("author") else [])
                    )
                )
            ),
            *(_normalize(str(value or "")) for value in edition_fields),
            _normalize(str(item.get("original_title") or "")),
            _normalize(str(item.get("translated_title") or "")),
            cls._translation_status({}, item),
            "|".join(sorted(_normalize(str(value)) for value in item.get("translators") or [])),
        ]
        return "manifestation:" + hashlib.sha256("\x1f".join(values).encode()).hexdigest()[:24]

    @classmethod
    def _merge_manifestation_group(cls, group: list[dict[str, Any]]) -> dict[str, Any]:
        group.sort(key=cls._candidate_sort_key)
        merged = copy.deepcopy(group[0])
        scalar_fields = (
            "original_title",
            "translated_title",
            "publication_language",
            "edition",
            "container_title",
            "publisher",
            "publication_place",
            "publication_language",
            "series",
            "year",
            "volume",
            "issue",
            "pages",
            "doi",
            "isbn",
        )
        metadata_conflicts: dict[str, list[str]] = {}
        for field in scalar_fields:
            values = list(
                dict.fromkeys(
                    str(item.get(field)).strip()
                    for item in group
                    if item.get(field) is not None
                    and item.get(field) != ""
                    and not isinstance(item.get(field), (dict, list, set, tuple))
                )
            )
            if isinstance(merged.get(field), (dict, list, set, tuple)):
                merged[field] = None
            if not merged.get(field) and values:
                merged[field] = values[0]
            if field == "publication_place" and values and all(
                value.casefold() in {"jp", "ja", "jpn"} for value in values
            ):
                merged["publication_place"] = None
                merged["publication_country_code"] = "JP"
                if any(value.casefold() == "ja" for value in values):
                    merged.setdefault("publication_language", "ja")
                continue
            normalized_values = {
                (_publisher_identity(value) if field == "publisher" else _normalize(value))
                for value in values if _normalize(value)
            }
            if len(normalized_values) > 1:
                metadata_conflicts[field] = values
        for field in ("authors", "translators", "editors"):
            merged[field] = list(
                dict.fromkeys(str(value) for item in group for value in item.get(field) or [])
            )
        translation_flags = [
            item.get("is_translation")
            for item in group
            if isinstance(item.get("is_translation"), bool)
        ]
        if translation_flags:
            merged["is_translation"] = translation_flags[0]
            if len(set(translation_flags)) > 1:
                metadata_conflicts["is_translation"] = [
                    str(value) for value in translation_flags
                ]
        source_records = [
            {
                "candidate_id": str(item.get("candidate_id") or ""),
                "provider": str(item.get("provider") or ""),
                "provider_record_id": str(item.get("provider_record_id") or ""),
                "url": item.get("url"),
            }
            for item in group
        ]
        merged["source_records"] = source_records
        merged["source_urls"] = list(
            dict.fromkeys(str(item["url"]) for item in source_records if item.get("url"))
        )
        merged["responsibility_sources"] = [
            {
                "provider": item.get("provider"),
                "provider_record_id": item.get("provider_record_id"),
                "url": item.get("url"),
                "responsibility_raw": list(item.get("responsibility_raw") or []),
                "responsibility_source": item.get("responsibility_source"),
                "authors": list(item.get("authors") or []),
                "translators": list(item.get("translators") or []),
                "editors": list(item.get("editors") or []),
            }
            for item in group
        ]
        merged["providers"] = list(
            dict.fromkeys(str(item["provider"]) for item in source_records if item["provider"])
        )
        merged["metadata_conflicts"] = metadata_conflicts
        merged["duplicate_record_count"] = len(group)
        identifiers = [
            value
            for value in (
                cls._normalized_doi(merged.get("doi")),
                cls._normalized_isbn(merged.get("isbn")),
                cls._manifestation_fingerprint(merged),
            )
            if value
        ]
        manifestation_axes = "|".join(
            [
                _normalize(str(merged.get(field) or ""))
                for field in (
                    "volume",
                    "issue",
                    "edition",
                    "original_title",
                    "translated_title",
                )
            ]
            + [
                cls._translation_status({}, merged),
                "|".join(
                    sorted(
                        _normalize(str(value))
                        for value in merged.get("translators") or []
                    )
                ),
            ]
        )
        identity = (
            "\x1f".join([*identifiers, manifestation_axes])
            if identifiers
            else "|".join(sorted(str(item["candidate_id"]) for item in source_records))
        )
        merged["candidate_id"] = (
            "manifestation:" + hashlib.sha256(identity.encode()).hexdigest()[:24]
        )
        merged["manifestation_fingerprint"] = cls._manifestation_fingerprint(merged) or None
        merged["identity_score"] = max(float(item.get("identity_score") or 0.0) for item in group)
        return merged

    @classmethod
    def _manifestation_scope_issues(
        cls, entry: dict[str, Any], candidate: dict[str, Any]
    ) -> list[str]:
        issues: list[str] = []
        for field in (
            "doi",
            "isbn",
            "original_title",
            "translated_title",
            "is_translation",
            "volume",
            "edition",
            "year",
            "publisher",
            "translators",
        ):
            expected = entry.get(field)
            if not cls._field_is_explicit(entry, field):
                continue
            actual = candidate.get(field)
            if actual is None or actual == "" or actual == []:
                issues.append(f"{field}_not_verified")
                continue
            if isinstance(expected, bool) or isinstance(actual, bool):
                if expected is not actual:
                    issues.append(f"{field}_conflict")
                continue
            if isinstance(expected, list) or isinstance(actual, list):
                expected_values = cls._normalized_field_values(expected)
                actual_values = cls._normalized_field_values(actual)
                if expected_values != actual_values:
                    issues.append(f"{field}_conflict")
            elif _normalize(str(expected)) != _normalize(str(actual)):
                issues.append(f"{field}_conflict")
        return issues

    @classmethod
    def _manifestation_axis_state(cls, entry: dict[str, Any]) -> dict[str, str]:
        axes = {
            "identifier": ("doi", "isbn"),
            "original_or_translation": (
                "is_translation",
                "original_title",
                "translated_title",
                "translators",
            ),
            "volume": ("volume",),
            "edition": ("edition",),
            "publication_year": ("year",),
            "publisher": ("publisher",),
        }
        return {
            axis: (
                "explicit"
                if any(cls._field_is_explicit(entry, field) for field in fields)
                else "unspecified"
            )
            for axis, fields in axes.items()
        }

    @staticmethod
    def _field_is_explicit(entry: dict[str, Any], field: str) -> bool:
        value = entry.get(field)
        if value is None or value == "" or value == []:
            return False
        provenance = (entry.get("provenance") or {}).get(field) or {}
        return provenance.get("kind") in {
            "source_explicit",
            "human_selection",
            "user_input",
            "user_confirmed",
        }

    @staticmethod
    def _normalized_field_values(value: object) -> set[str]:
        values = value if isinstance(value, (list, tuple, set)) else [value]
        return {_normalize(str(item)) for item in values if _normalize(str(item))}

    @classmethod
    def _manifestation_entry(
        cls,
        source_entry: dict[str, Any],
        candidate: dict[str, Any],
        *,
        index: int,
        consulted_status: str = "unconfirmed",
        final_revision_blocked: bool = True,
    ) -> dict[str, Any]:
        entry = {
            **copy.deepcopy(candidate),
            "reference_id": _stable_id(
                "expanded_manifestation",
                source_entry.get("reference_id"),
                candidate.get("candidate_id"),
            ),
            "source_reference_id": source_entry.get("reference_id"),
            "source_block_id": source_entry.get("source_block_id"),
            "original_text": source_entry.get("original_text"),
            "entry_ordinal": int(source_entry.get("entry_ordinal") or 0) + index,
            "parse_status": "parsed",
            "resolution_status": (
                "manifestation_confirmed"
                if consulted_status == "user_confirmed" and not final_revision_blocked
                else "metadata_incomplete"
                if consulted_status == "user_confirmed"
                else "manifestation_retained_unconfirmed"
            ),
            "reference_role": (
                "confirmed_consulted_manifestation"
                if consulted_status == "user_confirmed"
                else "unconfirmed_manifestation"
            ),
            "author": "・".join(candidate.get("authors") or [])
            or str(source_entry.get("author") or ""),
            "record_type": candidate.get("record_type")
            or source_entry.get("record_type"),
            "provenance": copy.deepcopy(source_entry.get("provenance") or {}),
            "reference_usage": copy.deepcopy(source_entry.get("reference_usage") or {}),
            "reference_usages": copy.deepcopy(source_entry.get("reference_usages") or []),
            "provider_records": copy.deepcopy(source_entry.get("provider_records") or []),
            "consulted_status": consulted_status,
            "final_revision_blocked": final_revision_blocked,
        }
        return entry

    @classmethod
    def _translator_status(cls, entry: dict[str, Any], candidate: dict[str, Any]) -> str:
        translators = candidate.get("translators") or entry.get("translators") or []
        if translators:
            return "verified"
        translation_status = candidate.get("translation_status") or cls._translation_status(
            entry, candidate
        )
        if translation_status == "confirmed_translation":
            return "required_missing"
        if translation_status == "confirmed_original":
            return "not_required"
        return "undetermined"

    @classmethod
    def _translation_status(cls, entry: dict[str, Any], candidate: dict[str, Any]) -> str:
        declared = candidate.get("is_translation")
        if declared is None:
            declared = entry.get("is_translation")
        if declared is True:
            return "confirmed_translation"
        if declared is False:
            return "confirmed_original"
        if candidate.get("translators") or entry.get("translators"):
            return "confirmed_translation"

        title = str(candidate.get("title") or entry.get("title") or "")
        original = str(candidate.get("original_title") or entry.get("original_title") or "")
        translated = str(candidate.get("translated_title") or entry.get("translated_title") or "")
        if original:
            if _normalize(title) == _normalize(original):
                return "confirmed_original"
            if _normalize(title) != _normalize(original):
                return "confirmed_translation"
        if translated and _normalize(title) == _normalize(translated):
            return "confirmed_translation"

        # Search aliases are hypotheses, and publication language identifies the
        # language of this manifestation rather than whether it is the original.
        # Neither can safely promote a record to confirmed original/translation.
        # Keep it undetermined until provider metadata or a user supplies the
        # actual original/translated-title relationship.
        return "undetermined"

    @staticmethod
    def _contains_japanese(value: str) -> bool:
        return bool(re.search(r"[ぁ-んァ-ヶ一-龥]", value))

    @staticmethod
    def _contains_latin(value: str) -> bool:
        return bool(re.search(r"[A-Za-z]", value))

    @classmethod
    def _candidate_required_missing_fields(cls, candidate: dict[str, Any]) -> list[str]:
        kind = cls._bibliography_kind_v22(candidate)
        fields = ["authors", "title"]
        if kind == "book":
            fields.extend(("publisher", "year"))
        elif kind in {"article", "chapter"}:
            fields.extend(("container_title", "year", "pages"))
            if kind == "chapter":
                fields.append("publisher")
        if candidate.get("translator_status") == "required_missing":
            fields.append("translators")
        elif candidate.get("translator_status") == "undetermined" and kind == "book":
            fields.append("translation_status_confirmation")
        return [field for field in fields if not candidate.get(field)]

    @classmethod
    def _apply_retrieved_candidate(
        cls,
        resolved: dict[str, Any],
        candidate: dict[str, Any],
        *,
        workflow_version: int = 22,
    ) -> None:
        resolved["selected_authors"] = list(candidate.get("authors") or [])
        if workflow_version >= 29 and candidate.get("record_type"):
            resolved["record_type"] = candidate["record_type"]
        for field in (
            "publisher",
            "original_title",
            "translated_title",
            "translators",
            "editors",
            "bibliography_kind",
            "edition",
            "publication_place",
            "publication_language",
            "series",
            "year",
            "container_title",
            "volume",
            "issue",
            "pages",
            "doi",
            "isbn",
            "url",
        ):
            value = candidate.get(field)
            if resolved.get(field) or value is None or value == "" or value == []:
                continue
            resolved[field] = value
            resolved.setdefault("provenance", {})[field] = _provenance(
                "retrieved_metadata",
                label="外部資料で確認",
                source="/".join(
                    candidate.get("providers") or [str(candidate.get("provider") or "")]
                ),
                source_url=str(candidate.get("url") or "") or None,
                provider_record_id=str(candidate.get("provider_record_id") or ""),
                confidence=float(candidate.get("identity_score") or 0.0),
            )

    @staticmethod
    def _normalized_doi(value: object) -> str:
        return re.sub(
            r"^https?://(?:dx\.)?doi\.org/",
            "",
            str(value or "").strip(),
            flags=re.I,
        ).lower()

    @staticmethod
    def _normalized_isbn(value: object) -> str:
        return re.sub(r"[^0-9X]", "", str(value or "").upper())

    @classmethod
    def apply_inferences(
        cls,
        entries: list[dict[str, Any]],
        payload: dict[str, Any],
        *,
        workflow_version: int = 21,
    ) -> list[dict[str, Any]]:
        by_id = {str(entry["reference_id"]): copy.deepcopy(entry) for entry in entries}
        for raw in payload.get("inferences") or []:
            if not isinstance(raw, dict):
                continue
            entry = by_id.get(str(raw.get("reference_id") or ""))
            if entry is None or entry.get("resolution_status") == "ambiguous":
                continue
            if workflow_version >= 22 and (
                entry.get("final_revision_blocked")
                or entry.get("resolution_status")
                in {"user_input_required", "manifestations_retained", "metadata_incomplete"}
                or entry.get("consulted_status") == "unconfirmed"
            ):
                continue
            try:
                confidence = float(raw.get("confidence"))
            except (TypeError, ValueError):
                confidence = 0.0
            if confidence < 0.8:
                continue
            fields = raw.get("fields")
            if not isinstance(fields, dict):
                continue
            for field, value in fields.items():
                if field not in _INFERENCE_FIELDS or entry.get(field):
                    continue
                clean = str(value or "").strip()
                if not clean:
                    continue
                entry[field] = clean
                entry.setdefault("provenance", {})[field] = _provenance(
                    "llm_inference",
                    label="LLMによる推論",
                    source="codex_reference_inference",
                    confidence=confidence,
                    rationale=str(raw.get("rationale") or "").strip() or None,
                )
            if (
                entry.get("resolution_status") not in {"ambiguous", "low_match"}
                and (workflow_version < 22 or not entry.get("final_revision_blocked"))
                and any(
                isinstance(item, dict) and item.get("kind") == "llm_inference"
                for item in entry.get("provenance", {}).values()
                )
            ):
                entry["resolution_status"] = "inferred"
        return [by_id[str(entry["reference_id"])] for entry in entries]

    @classmethod
    def render(
        cls,
        canonical_html: str,
        entries: list[dict[str, Any]],
        *,
        workflow_version: int = 21,
    ) -> tuple[str, list[dict[str, Any]]]:
        root = ET.fromstring(canonical_html)
        elements = {
            str(element.attrib.get("id")): element
            for element in root.iter()
            if element.attrib.get("id")
        }
        grouped: dict[str, list[dict[str, Any]]] = {}
        for entry in entries:
            grouped.setdefault(str(entry.get("source_block_id") or ""), []).append(entry)
        edits: list[dict[str, Any]] = []
        parents = {id(child): parent for parent in root.iter() for child in parent}
        for block_id, records in grouped.items():
            element = elements.get(block_id)
            if element is None or list(element):
                continue
            records.sort(key=lambda item: int(item.get("entry_ordinal") or 0))
            original = "".join(element.itertext()).strip()
            for index, record in enumerate(records):
                target_id = (
                    block_id
                    if index == 0
                    else block_id
                    + ":ref:"
                    + hashlib.sha256(str(record["reference_id"]).encode()).hexdigest()[:12]
                )
                target = elements.get(target_id)
                if target is None:
                    parent = parents.get(id(element))
                    if parent is None:
                        continue
                    target = ET.Element(element.tag, {**element.attrib, "id": target_id})
                    parent.insert(list(parent).index(element) + index, target)
                    elements[target_id] = target
                rendered = cls.render_entry(record, workflow_version=workflow_version)
                before = "".join(target.itertext()).strip()
                if rendered == before:
                    continue
                target.text = rendered
                edits.append(
                    {
                        "operation": "normalize_reference_block",
                        "block_id": target_id,
                        "before": before or original,
                        "after": rendered,
                        "reference_ids": [str(record["reference_id"])],
                    }
                )
        result = ET.tostring(root, encoding="unicode", short_empty_elements=False)
        CanonicalHTMLValidator().assert_valid(result)
        return result, edits

    @classmethod
    def render_sections_v22(
        cls,
        canonical_html: str,
        entries: Iterable[dict[str, Any]],
        *,
        machine_entries: Iterable[dict[str, Any]] = (),
        render_machine_section: bool = True,
        on_rendered_entry: Callable[[str, str, str], None] | None = None,
        workflow_version: int = 22,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Render V22 consulted and machine-detected sections idempotently.

        The caller must pass only evidence-validator-admitted records as
        ``machine_entries``.  Those records are visibly separated from works
        represented as actually consulted or cited.
        ``on_rendered_entry`` receives each reference ID, block ID and rendered
        text, including unchanged rows. It does not alter the stored HTML format.
        """

        entry_list = [copy.deepcopy(entry) for entry in entries]
        machine_list = [copy.deepcopy(entry) for entry in machine_entries]
        section_data = cls.bibliography_sections_v22(
            entry_list, machine_entries=machine_list, workflow_version=workflow_version
        )
        consulted = section_data[0]["entries"]
        root = ET.fromstring(canonical_html)
        existing_rows = {
            str(element.attrib["id"]): copy.deepcopy(element)
            for element in root.iter()
            if element.attrib.get("id") and element.attrib.get("data-reference-generated") == "v22"
        } if workflow_version >= 29 else {}
        # Rebuild system-generated consulted rows from the reconciled records.
        # They may live inside a source bibliography section, so removal must
        # walk all parents rather than only direct article children.
        for parent in root.iter():
            for child in list(parent):
                if child.attrib.get("data-reference-generated") == "v22-support-selected":
                    parent.remove(child)
        for candidate in list(root):
            if candidate.attrib.get("data-reference-section") == "consulted-generated-v22":
                root.remove(candidate)
        elements = {
            str(element.attrib.get("id")): element
            for element in root.iter()
            if element.attrib.get("id")
        }
        parents = {id(child): parent for parent in root.iter() for child in parent}
        grouped: dict[str, list[dict[str, Any]]] = {}
        for entry in consulted:
            grouped.setdefault(str(entry.get("source_block_id") or ""), []).append(entry)
        edits: list[dict[str, Any]] = []

        for block_id, records in grouped.items():
            element = elements.get(block_id)
            parent = parents.get(id(element)) if element is not None else None
            has_non_italic_markup = bool(
                element is not None
                and any(child.tag != "em" for child in element.iter() if child is not element)
            )
            if (
                element is None
                or parent is None
                or (list(element) and workflow_version < 29)
                or (workflow_version >= 29 and has_non_italic_markup)
            ):
                continue
            prefix = block_id + ":ref:"
            for sibling in list(parent):
                sibling_id = str(sibling.attrib.get("id") or "")
                if sibling is element:
                    continue
                if (
                    sibling.attrib.get("data-reference-source-id") == block_id
                    or sibling_id.startswith(prefix)
                ):
                    parent.remove(sibling)
                    elements.pop(sibling_id, None)
            records.sort(
                key=lambda item: (
                    int(item.get("entry_ordinal") or 0),
                    str(item.get("reference_id") or ""),
                )
            )
            insertion_index = list(parent).index(element)
            for index, record in enumerate(records):
                target_id = (
                    block_id
                    if index == 0
                    else block_id
                    + ":ref:"
                    + hashlib.sha256(str(record["reference_id"]).encode()).hexdigest()[:12]
                )
                if index == 0:
                    target = element
                else:
                    attributes = {
                        key: value
                        for key, value in element.attrib.items()
                        if key not in {"id", "data-reference-generated", "data-reference-source-id"}
                    }
                    attributes.update(
                        {
                            "id": target_id,
                            "data-reference-generated": "v22",
                            "data-reference-source-id": block_id,
                        }
                    )
                    target = ET.Element(element.tag, attributes)
                    parent.insert(insertion_index + index, target)
                    elements[target_id] = target
                previous = existing_rows.get(target_id) if index else None
                before = "".join((previous if previous is not None else target).itertext()).strip()
                before_html = ET.tostring(
                    previous if previous is not None else target, encoding="unicode"
                )
                if workflow_version >= 29 and (
                    record.get("reference_display_status") == "review"
                    or record.get("reference_output_mode_v29") in {"REVIEW", "UNRESOLVED"}
                ):
                    rendered = cls._write_rendered_entry(target, record, workflow_version)
                elif workflow_version >= 29 and not format_reference(record).supported:
                    if index == 0:
                        # Keep the actual source row, including links and markup.
                        rendered = before
                    else:
                        prior = existing_rows.get(target_id)
                        if prior is not None:
                            parent.remove(target)
                            parent.insert(insertion_index + index, prior)
                            target = prior
                            elements[target_id] = target
                            rendered = "".join(target.itertext()).strip()
                        else:
                            rendered = "書誌要確認：" + str(
                                record.get("title") or record.get("candidate_id") or "資料"
                            )
                            target.text = rendered
                else:
                    rendered = cls._write_rendered_entry(target, record, workflow_version)
                if on_rendered_entry:
                    on_rendered_entry(str(record.get("reference_id") or ""), target_id, rendered)
                if before != rendered or (
                    workflow_version >= 29
                    and before_html != ET.tostring(target, encoding="unicode")
                ):
                    edits.append(
                        {
                            "operation": "render_v22_reference_manifestation",
                            "block_id": target_id,
                            "before": before,
                            "after": rendered,
                            "reference_ids": [str(record.get("reference_id") or "")],
                        }
                    )

        article_id = str(root.attrib.get("id") or "ARTICLE")
        generated_consulted = [
            record
            for record in consulted
            if not str(record.get("source_block_id") or "")
            or str(record.get("source_block_id") or "") not in elements
        ]
        if generated_consulted:
            parents = {id(child): parent for parent in root.iter() for child in parent}
            consulted_parent = next(
                (
                    parents.get(id(elements[block_id]))
                    for block_id in grouped
                    if block_id in elements and parents.get(id(elements[block_id])) is not None
                ),
                None,
            )
            generated_section_id: str | None = None
            if consulted_parent is None:
                consulted_parent = next(
                    (
                        section
                        for section in root.iter("section")
                        if section.attrib.get("data-reference-section")
                        != "machine-detected-v22"
                        and any(
                            _REFERENCE_HEADING.match("".join(child.itertext()).strip())
                            for child in list(section)
                            if child.tag in {"h1", "h2", "h3", "h4", "h5", "h6"}
                        )
                    ),
                    None,
                )
            if consulted_parent is None:
                generated_section_id = f"{article_id}.consulted-references"
                consulted_parent = ET.Element(
                    "section",
                    {
                        "id": generated_section_id,
                        "data-reference-section": "consulted-generated-v22",
                        "data-reference-role": "consulted-or-cited",
                    },
                )
                heading = ET.SubElement(
                    consulted_parent,
                    "h2",
                    {"id": f"{generated_section_id}.heading"},
                )
                heading.text = "参考文献"
                root.append(consulted_parent)
            for record in sorted(
                generated_consulted,
                key=lambda item: (
                    int(item.get("entry_ordinal") or 0),
                    str(item.get("reference_id") or ""),
                ),
            ):
                reference_id = str(record.get("reference_id") or "")
                stable = hashlib.sha256(reference_id.encode()).hexdigest()[:16]
                block_id = f"{article_id}.consulted-references:ref:{stable}"
                paragraph = ET.SubElement(
                    consulted_parent,
                    "p",
                    {
                        "id": block_id,
                        "data-reference-generated": "v22-support-selected",
                        "data-reference-id": reference_id,
                    },
                )
                rendered = cls._write_rendered_entry(paragraph, record, workflow_version)
                if on_rendered_entry:
                    on_rendered_entry(reference_id, block_id, rendered)
                edits.append(
                    {
                        "operation": "render_generated_consulted_reference",
                        "block_id": block_id,
                        "after": rendered,
                        "reference_ids": [reference_id],
                    }
                )
            if generated_section_id:
                edits.append(
                    {
                        "operation": "render_generated_consulted_reference_section",
                        "section_id": generated_section_id,
                        "entry_count": len(generated_consulted),
                    }
                )

        machine_section_id = f"{article_id}.machine-detected-references"
        for candidate in list(root):
            if candidate.attrib.get("data-reference-section") == "machine-detected-v22" or (
                candidate.attrib.get("id") == machine_section_id
            ):
                root.remove(candidate)
        if len(section_data) > 1 and not render_machine_section:
            # V26 keeps the reviewable candidate rows inside the one canonical
            # bibliography section. They remain explicitly unconfirmed and never
            # become a second argument section after the terminal bibliography.
            semantic_references = [
                section
                for section in ManuscriptCoherenceService.semantic_structure(
                    ET.tostring(root, encoding="unicode", short_empty_elements=False)
                )["sections"]
                if section["document_role"] == "references"
            ]
            if not semantic_references:
                # A Fukamiru authoring Run may start from notes rather than a
                # manuscript that already owns a bibliography. Machine-detected
                # review rows still need a terminal container, but must not be
                # promoted to consulted references. Create the same canonical
                # bibliography shell used for generated consulted entries and
                # keep every machine row explicitly marked as unconfirmed.
                generated_section_id = f"{article_id}.consulted-references"
                bibliography = ET.Element(
                    "section",
                    {
                        "id": generated_section_id,
                        "data-reference-section": "consulted-generated-v22",
                        "data-reference-role": "consulted-or-cited",
                    },
                )
                heading = ET.SubElement(
                    bibliography,
                    "h2",
                    {"id": f"{generated_section_id}.heading"},
                )
                heading.text = "参考文献"
                root.append(bibliography)
                edits.append(
                    {
                        "operation": "render_generated_consulted_reference_section",
                        "section_id": generated_section_id,
                        "entry_count": 0,
                    }
                )
                semantic_references = [
                    section
                    for section in ManuscriptCoherenceService.semantic_structure(
                        ET.tostring(root, encoding="unicode", short_empty_elements=False)
                    )["sections"]
                    if section["document_role"] == "references"
                ]
            final_reference_id = (
                str(semantic_references[-1]["block_ids"][-1])
                if semantic_references else ""
            )
            reference_element = next(
                (element for element in root.iter()
                 if element.attrib.get("id") == final_reference_id),
                None,
            )
            bibliography = next(
                (parent for parent in root.iter() if reference_element in list(parent)),
                None,
            ) if reference_element is not None else None
            if bibliography is None:
                raise FukamiruError(
                    "V26 machine-detected reference review requires a bibliography section"
                )
            for child in list(bibliography):
                if child.attrib.get("data-reference-generated") == "v26-machine-candidate":
                    bibliography.remove(child)
            machine_section = section_data[1]
            heading = ET.SubElement(
                bibliography, "p",
                {"id": f"{machine_section_id}.heading",
                 "data-reference-generated": "v26-machine-candidate"},
            )
            heading.text = "機械判別候補（未確認・参照済みではありません）"
            disclaimer = ET.SubElement(
                bibliography, "p",
                {"id": f"{machine_section_id}.disclaimer",
                 "data-reference-generated": "v26-machine-candidate"},
            )
            disclaimer.text = str(machine_section["disclaimer"])
            for item, rendered in zip(
                machine_section["entries"], machine_section["rendered_entries"], strict=True
            ):
                stable = hashlib.sha256(str(item["machine_reference_id"]).encode()).hexdigest()[:16]
                paragraph = ET.SubElement(
                    bibliography, "p",
                    {"id": f"{machine_section_id}.item.{stable}",
                     "data-reference-generated": "v26-machine-candidate",
                     "data-machine-reference-id": str(item["machine_reference_id"])},
                )
                if workflow_version >= 29:
                    rendered = cls._write_rendered_entry(paragraph, item, workflow_version)
                else:
                    paragraph.text = rendered
                if on_rendered_entry:
                    on_rendered_entry(
                        str(item["machine_reference_id"]), str(paragraph.attrib["id"]), rendered
                    )
            edits.append(
                {"operation": "render_machine_detected_reference_review",
                 "section_id": str(bibliography.attrib["id"]),
                 "entry_count": len(machine_section["entries"])}
            )
        if len(section_data) > 1 and render_machine_section:
            machine_section = section_data[1]
            section = ET.Element(
                "section",
                {
                    "id": machine_section_id,
                    "data-reference-section": "machine-detected-v22",
                    "data-reference-role": "machine-detected-not-confirmed-consulted",
                },
            )
            heading = ET.SubElement(section, "h2", {"id": f"{machine_section_id}.heading"})
            heading.text = str(machine_section["title"])
            disclaimer = ET.SubElement(
                section,
                "p",
                {
                    "id": f"{machine_section_id}.disclaimer",
                    "data-reference-disclaimer": "true",
                },
            )
            disclaimer.text = str(machine_section["disclaimer"])
            for item, rendered in zip(
                machine_section["entries"],
                machine_section["rendered_entries"],
                strict=True,
            ):
                stable = hashlib.sha256(
                    str(item["machine_reference_id"]).encode()
                ).hexdigest()[:16]
                paragraph = ET.SubElement(
                    section,
                    "p",
                    {
                        "id": f"{machine_section_id}.item.{stable}",
                        "data-reference-generated": "v22",
                        "data-machine-reference-id": str(item["machine_reference_id"]),
                    },
                )
                if workflow_version >= 29:
                    cls._write_rendered_entry(paragraph, item, workflow_version)
                else:
                    paragraph.text = rendered
                if on_rendered_entry:
                    on_rendered_entry(
                        str(item["machine_reference_id"]), str(paragraph.attrib["id"]), rendered
                    )
            root.append(section)
            edits.append(
                {
                    "operation": "render_machine_detected_reference_section",
                    "section_id": machine_section_id,
                    "entry_count": len(machine_section["entries"]),
                }
            )

        result = ET.tostring(root, encoding="unicode", short_empty_elements=False)
        CanonicalHTMLValidator().assert_valid(result)
        if result == canonical_html:
            return result, []
        return result, edits

    @classmethod
    def _write_rendered_entry(
        cls, element: ET.Element, entry: dict[str, Any], workflow_version: int
    ) -> str:
        if (
            workflow_version >= 29
            and entry.get("provisional_bibliography_review") is True
            and entry.get("author") == "自著"
        ):
            styled = format_reference(entry)
            if styled.supported:
                styled.write_into(element)
                return styled.text
        if workflow_version >= 29 and (
            entry.get("reference_display_status") == "review"
            or entry.get("reference_output_mode_v29") in {"REVIEW", "UNRESOLVED"}
        ):
            rendered = cls.render_entry(entry, workflow_version=workflow_version)
            element[:] = []
            element.text = rendered
            return rendered
        if workflow_version >= 29 and entry.get("parse_status") == "parsed" and entry.get("title"):
            styled = format_reference(entry)
            if not styled.supported:
                original = "".join(element.itertext()).strip()
                if original:
                    return original
                element.text = "書誌要確認：" + str(entry.get("title") or "資料")
                return element.text
            styled.write_into(element)
            label = str(entry.get("reference_candidate_label_v29") or "")
            if label:
                element.text = label + "\n" + (element.text or "")
            if entry.get("resolution_status") == "user_input_required" or entry.get(
                "user_required_metadata"
            ):
                note = cls._user_metadata_note(entry)
                if len(element):
                    element[-1].tail = (element[-1].tail or "") + note
                else:
                    element.text = (element.text or "") + note
                return (label + "\n" if label else "") + styled.text + note
            return (label + "\n" if label else "") + styled.text
        rendered = cls.render_entry(entry, workflow_version=workflow_version)
        element[:] = []
        element.text = rendered
        return rendered

    @classmethod
    def render_entry(cls, entry: dict[str, Any], *, workflow_version: int = 21) -> str:
        if (
            workflow_version >= 29
            and entry.get("provisional_bibliography_review") is True
            and entry.get("author") == "自著"
        ):
            styled = format_reference(entry)
            if styled.supported:
                return styled.text
        if workflow_version >= 29 and entry.get("reference_output_mode_v29") == "UNRESOLVED":
            return "未解決：" + str(entry.get("original_text") or entry.get("title") or "資料")
        if workflow_version >= 29 and (
            entry.get("reference_display_status") == "review"
            or entry.get("reference_output_mode_v29") == "REVIEW"
            and not entry.get("expanded_reference_entries")
        ):
            label = str(entry.get("reference_candidate_label_v29") or "")
            return (label + "\n" if label else "") + "要確認：" + str(
                entry.get("title") or entry.get("original_text") or "資料"
            )
        if entry.get("parse_status") != "parsed" or not entry.get("title"):
            raw = str(entry.get("original_text") or "").strip()
            if workflow_version >= 29 and str(
                entry.get("publication_language") or entry.get("language") or ""
            ).casefold() not in {"ja", "jpn", "japanese"}:
                if entry.get("resolution_status") == "user_input_required" or entry.get(
                    "user_required_metadata"
                ):
                    raw += cls._user_metadata_note(entry)
                return raw
            # Remove a standalone country field only outside quoted titles.
            pieces = re.split(r"(『[^』]*』|「[^」]*」)", raw)
            raw = "".join(
                piece if index % 2 else re.sub(r"([、,])\s*(?:JP|JPN)\s*([、,])", r"\1", piece)
                for index, piece in enumerate(pieces)
            )
            rendered = raw.rstrip("。.") + "。"
            if workflow_version >= 22 and (
                entry.get("resolution_status") == "user_input_required"
                or entry.get("user_required_metadata")
            ):
                rendered += cls._user_metadata_note(entry)
            return rendered
        if workflow_version >= 29:
            styled = format_reference(entry)
            rendered = styled.text if styled.supported else (
                str(entry.get("original_text") or "").strip()
                or "書誌要確認：" + str(entry.get("title") or "資料")
            )
            label = str(entry.get("reference_candidate_label_v29") or "")
            if label:
                rendered = label + "\n" + rendered
            if entry.get("resolution_status") == "user_input_required" or entry.get(
                "user_required_metadata"
            ):
                rendered += cls._user_metadata_note(entry)
            return rendered
        author = str(entry.get("author") or "").strip()
        title = str(entry.get("title") or "").strip()
        record_type = (
            cls._bibliography_kind_v22(entry)
            if workflow_version >= 22
            else cls.bibliography_kind(entry)
        )
        if record_type in {"article", "chapter"}:
            parts = [f"{author}「{title}」" if author else f"「{title}」"]
            if record_type == "chapter" and entry.get("editors"):
                parts.append("・".join(entry["editors"]) + "編")
            if entry.get("container_title"):
                parts.append(f"『{entry['container_title']}』")
            volume = str(entry.get("volume") or "").strip()
            issue = str(entry.get("issue") or "").strip()
            if volume and issue:
                parts.append(f"{volume}({issue})")
            elif volume or issue:
                parts.append(volume or f"no {issue}")
            if record_type == "chapter" and entry.get("publisher"):
                parts.append(str(entry["publisher"]))
        else:
            parts = [f"{author}『{title}』" if author else f"『{title}』"]
            translators = entry.get("translators") or []
            if translators:
                parts.append("・".join(translators) + "訳")
            if entry.get("editors"):
                parts.append("・".join(entry["editors"]) + "編")
            for field in ("volume", "edition", "publication_place", "publisher", "series"):
                if entry.get(field):
                    value = str(entry[field]).strip()
                    if field != "publication_place" or not re.fullmatch(r"[A-Z]{2,3}", value):
                        parts.append(value)
        for field in entry.get("unparsed_fields") or []:
            if _normalize(str(field)) not in {_normalize(part) for part in parts}:
                parts.append(str(field))
        if entry.get("year"):
            parts.append(str(entry["year"]).strip())
        if entry.get("pages"):
            pages = cls._normalized_pages(str(entry["pages"]))
            parts.append(f"p. {pages}")
        if workflow_version >= 22:
            fields = [item.strip() for item in parts if item.strip()]
            rendered = "".join(
                (" " if fields[index - 1].endswith((".", "．")) else "、") + item
                if index else item
                for index, item in enumerate(fields)
            ).rstrip("。")
            # Keep abbreviation periods, including when the last metadata field
            # is abbreviated; the Japanese item terminator remains separate.
            rendered += " 。" if rendered.endswith((".", "．")) else "。"
        else:
            rendered = "、".join(item for item in parts if item).rstrip("。.") + "。"
        if workflow_version >= 22 and (
            entry.get("resolution_status") == "user_input_required"
            or entry.get("user_required_metadata")
        ):
            rendered += cls._user_metadata_note(entry)
        return rendered

    @classmethod
    def render_machine_detected_entry_v22(
        cls, candidate: dict[str, Any], *, workflow_version: int = 22
    ) -> str:
        """Render a discovered-only candidate without claiming it was consulted."""

        entry = {
            **candidate,
            "parse_status": "parsed",
            "author": "・".join(candidate.get("authors") or []),
            "record_type": candidate.get("record_type") or candidate.get("bibliography_kind"),
        }
        return cls.render_entry(entry, workflow_version=workflow_version)

    @staticmethod
    def _user_metadata_note(entry: dict[str, Any]) -> str:
        fields = entry.get("user_required_metadata")
        if fields is None:
            fields = [
                "document_type",
                "publication_year",
                "venue_or_publisher",
                "volume_issue_if_applicable",
                "pages_if_applicable",
            ]
        if not fields:
            return "［要ユーザー確認：登録済み書誌情報と資料分類］"
        labels = {
            "document_type": "資料種別（書籍・論文・章・未刊原稿）",
            "publication_year": "発表年",
            "venue_or_publisher": "掲載先または出版社",
            "volume_issue_if_applicable": "巻号",
            "pages_if_applicable": "ページ",
        }
        required = "・".join(labels.get(str(field), str(field)) for field in fields)
        return f"［要ユーザー指定：{required}］"

    @staticmethod
    def bibliography_kind(entry: dict) -> str:
        kind = str(entry.get("bibliography_kind") or entry.get("record_type") or "")
        if kind in {
            "nonstandard_contribution", "doujin_contribution", "web_contribution",
            "self_published_contribution", "nonstandard_publication",
            "web_publication", "doujin_publication",
        }:
            return "nonstandard_contribution"
        if kind in {"chapter", "book-chapter", "book-section", "BookSection"}:
            return "chapter"
        if kind in {"article", "journal-article", "article_poem_or_chapter", "JournalArticle"}:
            return "article"
        return "book"

    @staticmethod
    def _bibliography_kind_v22(entry: dict[str, Any]) -> str:
        """Normalize provider-specific record kinds without changing V21 output."""

        kinds = [
            str(entry.get("record_type") or "").casefold(),
            str(entry.get("bibliography_kind") or "").casefold(),
        ]
        if any(
            kind
            in {
                "article",
                "journal-article",
                "article_poem_or_chapter",
                "journalarticle",
                "記事",
                "論文",
            }
            for kind in kinds
        ):
            return "article"
        if any(
            kind
            in {
                "chapter",
                "book-chapter",
                "book-section",
                "booksection",
                "章",
            }
            for kind in kinds
        ):
            return "chapter"
        if any(
            kind in {
                "nonstandard_contribution", "doujin_contribution", "web_contribution",
                "self_published_contribution", "nonstandard_publication",
                "web_publication", "doujin_publication",
            }
            for kind in kinds
        ):
            return "nonstandard_contribution"
        return "book"

    @staticmethod
    def _is_book_record(entry: dict[str, Any]) -> bool:
        """Require positive book classification before applying translator policy."""

        known_book_kinds = {
            "book",
            "book_or_journal",
            "monograph",
            "edited-book",
            "図書",
            "本",
        }
        record_type = str(entry.get("record_type") or "").casefold()
        if record_type:
            return record_type in known_book_kinds
        return str(entry.get("bibliography_kind") or "").casefold() in known_book_kinds

    @staticmethod
    def candidate_label(item: dict[str, Any]) -> str:
        authors = "・".join(item.get("authors") or [])
        translators = "・".join(item.get("translators") or []) or "訳者未確認"
        parts = [
            authors,
            str(item.get("title") or ""),
            translators,
            str(item.get("publisher") or "出版社未確認"),
            str(item.get("year") or "年未確認"),
            str(item.get("volume") or "巻次未確認"),
            str(item.get("edition") or "版未確認"),
            str(item.get("url") or ""),
        ]
        return "候補〔" + "・".join(parts) + "〕"

    @classmethod
    def quality_report(
        cls,
        canonical_html: str,
        entries: list[dict[str, Any]],
        *,
        workflow_version: int = 21,
        machine_entries: Iterable[dict[str, Any]] = (),
    ) -> dict[str, Any]:
        machine_list = [copy.deepcopy(entry) for entry in machine_entries]
        issues: list[dict[str, Any]] = cls.format_issues(
            canonical_html,
            entries,
            workflow_version=workflow_version,
            machine_entries=machine_list,
        )
        quality_entries = entries
        machine_quality_entries: list[dict[str, Any]] = []
        if workflow_version >= 22:
            section_data = cls.bibliography_sections_v22(
                entries,
                machine_entries=machine_list,
                workflow_version=workflow_version,
            )
            quality_entries = section_data[0]["entries"]
            machine_quality_entries = next(
                (
                    list(section.get("entries") or [])
                    for section in section_data
                    if section.get("section_key") == "machine_detected_references"
                ),
                [],
            )
        for entry in quality_entries:
            reference_id = str(entry.get("reference_id") or "")
            selection_ready = (
                workflow_version >= 29
                and entry.get("reference_state_v29")
                == "RESOLVED_CANDIDATES_SELECTION_REQUIRED"
                and int(entry.get("v29_ready_manifestation_count") or 0) > 0
                and not entry.get("consulted_manifestation_id")
            )
            display_ready = workflow_version >= 29 and entry.get(
                "reference_display_status"
            ) == "ready"
            if display_ready:
                selection_ready = True
            if workflow_version >= 29 and entry.get("reference_output_mode_v29") == "UNRESOLVED":
                issues.append({
                    "code": "reference.identity_unresolved", "severity": "error",
                    "reference_id": reference_id, "message": "書誌同定が未解決です。",
                })
            if workflow_version >= 29 and not entry.get("reference_display_status") and (
                selection_ready
                or any(
                    item.get("consulted_status") != "user_confirmed"
                    for item in entry.get("expanded_reference_entries") or []
                )
            ) and not display_ready:
                issues.append(
                    {
                        "code": "reference.manifestation_selection_unconfirmed",
                        "severity": "error",
                        "reference_id": reference_id,
                        "message": "引用した版の候補が未確定です。",
                    }
                )
            styled = format_reference(entry) if workflow_version >= 29 else None
            if (
                workflow_version >= 29
                and entry.get("parse_status") == "parsed"
                and styled
                and not styled.supported
                and not selection_ready
            ):
                issues.append(
                    {
                        "code": "reference.house_style_undetermined",
                        "severity": "error",
                        "reference_id": reference_id,
                        "message": "引用版の言語・責任表示・資料種別などの書誌事項が未確定です。",
                        "fields": list(styled.review_reasons),
                    }
                )
            if entry.get("parse_status") != "parsed":
                issues.append(
                    {
                        "code": "reference.parse_unresolved",
                        "severity": "error",
                        "reference_id": reference_id,
                        "message": "参考文献項目を著者・題名へ分解できませんでした。",
                    }
                )
                continue
            missing = cls.required_missing_fields(entry)
            known = {
                _normalize(str(entry.get(k) or ""))
                for k in (
                    "publisher",
                    "publication_place",
                    "series",
                    "container_title",
                    "volume",
                    "edition",
                )
            }
            unclassified = [
                v for v in entry.get("unparsed_fields", []) if _normalize(str(v)) not in known
            ]
            if unclassified:
                issues.append(
                    {
                        "code": "reference.metadata_unclassified",
                        "severity": "error",
                        "reference_id": reference_id,
                        "fields": unclassified,
                        "message": (
                            "原稿の書誌情報を保持していますが、フィールドの分類が未確定です。"
                        ),
                    }
                )
            source_language_missing = bool(
                workflow_version >= 29
                and entry.get("source_language_manifestation_missing")
            )
            foreign_fallback_used = bool(
                workflow_version >= 29
                and entry.get("foreign_manifestation_fallback_used")
            )
            if source_language_missing and foreign_fallback_used:
                issues.extend([
                    {
                        "code": "reference.source_language_manifestation_missing",
                        "severity": "info",
                        "reference_id": reference_id,
                        "message": (
                            "日本語 manifestation は確認されず、"
                            "確認済み外国語版を表示します。"
                        ),
                    },
                    {
                        "code": "reference.foreign_manifestation_fallback_used",
                        "severity": "info",
                        "reference_id": reference_id,
                        "message": (
                            "完了した翻訳側探索に基づき"
                            "外国語 manifestation を採用しました。"
                        ),
                    },
                ])
            elif source_language_missing and not selection_ready:
                issues.append(
                    {
                        "code": "reference.source_language_manifestation_missing",
                        "severity": "error",
                        "reference_id": reference_id,
                        "message": (
                            "原稿が示す言語の出版 manifestation を確認できません。"
                            "確認済み外国語版の出版社・刊年不足とは区別して扱います。"
                        ),
                    }
                )
            if missing and not selection_ready and not source_language_missing:
                issues.append(
                    {
                        "code": "reference.metadata_incomplete",
                        "severity": "error",
                        "reference_id": reference_id,
                        "message": "必須書誌情報が不足しています。",
                        "fields": missing,
                    }
                )
            if entry.get("resolution_status") in {"ambiguous", "low_match"}:
                issues.append(
                    {
                        "code": "reference.identity_ambiguous",
                        "severity": "error",
                        "reference_id": reference_id,
                        "message": (
                            "複数の書誌候補を安全に区別できませんでした。"
                            if entry.get("resolution_status") == "ambiguous"
                            else "著者・著作を同定できる候補がありません。"
                        ),
                    }
                )
            if workflow_version >= 22:
                if entry.get("translator_status") == "required_missing":
                    issues.append(
                        {
                            "code": "reference.translator_required",
                            "severity": "error",
                            "reference_id": reference_id,
                            "message": "翻訳書の訳者が未確認のため最終Revisionを承認できません。",
                        }
                    )
                if entry.get("final_revision_blocked") and entry.get("resolution_status") not in {
                    "ambiguous",
                    "low_match",
                }:
                    issues.append(
                        {
                            "code": "reference.final_revision_blocked",
                            "severity": "error",
                            "reference_id": reference_id,
                            "message": (
                                "書誌の未確定項目を解消するまで"
                                "最終Revisionを承認できません。"
                            ),
                        }
                    )
            for field, provenance in (entry.get("provenance") or {}).items():
                if not isinstance(provenance, dict) or not provenance.get("label"):
                    issues.append(
                        {
                            "code": "reference.provenance_missing",
                            "severity": "error",
                            "reference_id": reference_id,
                            "field": field,
                            "message": "変更フィールドの来歴がありません。",
                        }
                    )
        if workflow_version >= 22:
            for entry in machine_quality_entries:
                translator_status = str(entry.get("translator_status") or "")
                reference_id = str(
                    entry.get("machine_reference_id")
                    or entry.get("reference_id")
                    or ""
                )
                if workflow_version >= 29:
                    styled = format_reference(entry)
                    if not styled.supported:
                        issues.append(
                            {
                                "code": "reference.machine_house_style_undetermined",
                                "severity": "error",
                                "reference_id": reference_id,
                                "reference_role": "machine_detected_not_confirmed_consulted",
                                "fields": list(styled.review_reasons),
                                "message": "機械判別候補の書誌事項は未確認です。",
                            }
                        )
                translation_undetermined = (
                    translator_status == "undetermined"
                    and cls._is_book_record(entry)
                )
                if translator_status == "required_missing" or translation_undetermined:
                    issues.append(
                        {
                            "code": (
                                "reference.translation_status_confirmation_required"
                                if translation_undetermined
                                else "reference.translator_required"
                            ),
                            "severity": "error",
                            "reference_id": reference_id,
                            "reference_role": "machine_detected_not_confirmed_consulted",
                            "message": (
                                "機械判別参考文献が原著か翻訳書か未確認のため"
                                "最終Revisionを承認できません。"
                                if translation_undetermined
                                else "機械判別参考文献の翻訳書で訳者が未確認のため"
                                "最終Revisionを承認できません。"
                            ),
                        }
                    )
                remaining = [
                    str(value)
                    for value in entry.get("required_missing_fields") or []
                    if str(value)
                    not in {"translation_status_confirmation", "translators"}
                ]
                if entry.get("final_revision_blocked") is True and remaining:
                    issues.append(
                        {
                            "code": "reference.machine_manifestation_unconfirmed",
                            "severity": "error",
                            "reference_id": reference_id,
                            "reference_role": "machine_detected_not_confirmed_consulted",
                            "fields": remaining,
                            "message": (
                                "機械判別参考文献の版・巻等の未確定項目があるため"
                                "最終Revisionを承認できません。"
                            ),
                        }
                    )
        if workflow_version >= 22:
            rerendered, _ = cls.render_sections_v22(
                canonical_html, entries, machine_entries=machine_list,
                render_machine_section=workflow_version < 26,
                workflow_version=workflow_version,
            )
            rerendered_twice, _ = cls.render_sections_v22(
                rerendered, entries, machine_entries=machine_list,
                render_machine_section=workflow_version < 26,
                workflow_version=workflow_version,
            )
            if rerendered_twice != rerendered:
                issues.append(
                    {
                        "code": "reference.sections_v22_not_idempotent",
                        "severity": "error",
                        "reference_id": None,
                        "message": "V22参考文献節レンダリングが冪等ではありません。",
                    }
                )
        else:
            rerendered, _ = cls.render(
                canonical_html, entries, workflow_version=workflow_version
            )
        if rerendered != canonical_html:
            issues.append(
                {
                    "code": "reference.render_not_idempotent",
                    "severity": "error",
                    "reference_id": None,
                    "message": "参考文献レンダリングが冪等ではありません。",
                }
            )
        return {
            "rule_set_id": (
                BIBLIOGRAPHY_HOUSE_STYLE_V29["id"]
                if workflow_version >= 29 else SJLLF_JA_REFERENCE_RULESET["id"]
            ),
            "entry_count": len(entries),
            "machine_detected_entry_count": len(machine_quality_entries),
            "resolved_count": sum(
                entry.get("resolution_status") in {"resolved", "inferred"} for entry in entries
            ),
            "llm_inferred_field_count": sum(
                provenance.get("kind") == "llm_inference"
                for entry in entries
                for provenance in (entry.get("provenance") or {}).values()
                if isinstance(provenance, dict)
            ),
            "issue_count": len(issues),
            "passed": not any(item["severity"] == "error" for item in issues),
            "issues": issues,
        }

    @classmethod
    def format_issues(
        cls,
        canonical_html: str,
        entries: list[dict],
        *,
        workflow_version: int = 21,
        machine_entries: Iterable[dict[str, Any]] = (),
    ) -> list[dict]:
        elements = {e.get("id"): e for e in ET.fromstring(canonical_html).iter()}
        generated_by_reference_id = {
            str(element.get("data-reference-id")): element
            for element in elements.values()
            if element is not None and element.get("data-reference-id")
        }
        grouped = {}
        validation_entries = entries
        if workflow_version >= 22:
            validation_entries = cls.bibliography_sections_v22(
                entries, workflow_version=workflow_version
            )[0]["entries"]
        for entry in validation_entries:
            source_block_id = str(entry.get("source_block_id") or "")
            # Generated support-selected references have no source block.  The
            # deterministic whole-section rerender check below validates their
            # stable generated IDs and exact text.
            if workflow_version >= 22 and not source_block_id:
                continue
            grouped.setdefault(source_block_id, []).append(entry)
        issues = []
        for block_id, records in grouped.items():
            for index, record in enumerate(
                sorted(records, key=lambda r: int(r.get("entry_ordinal") or 0))
            ):
                target_id = (
                    block_id
                    if index == 0
                    else block_id
                    + ":ref:"
                    + hashlib.sha256(str(record["reference_id"]).encode()).hexdigest()[:12]
                )
                element = elements.get(target_id)
                if workflow_version >= 22 and element is None:
                    # Authoring sources such as memo annotations can retain a
                    # provenance-only source_block_id even though that source
                    # node is not part of the generated manuscript.  In that
                    # case render_sections_v22 creates the canonical row and
                    # identifies it by reference_id.  Validate that row rather
                    # than reporting the absent provenance block as a format
                    # failure.
                    element = generated_by_reference_id.get(
                        str(record.get("reference_id") or "")
                    )
                    if element is not None:
                        target_id = str(element.get("id") or target_id)
                if (
                    workflow_version >= 29
                    and element is not None
                    and any(child.tag != "em" for child in element.iter() if child is not element)
                ):
                    issues.append(
                        {
                            "code": "reference.inline_markup_requires_review",
                            "severity": "error",
                            "reference_id": record["reference_id"],
                            "block_id": target_id,
                            "message": (
                                "上付き・リンク等の既存書式を保つため、"
                                "この文献行は自動整形していません。"
                            ),
                        }
                    )
                    continue
                if (
                    workflow_version >= 29
                    and element is not None
                    and not format_reference(record).supported
                    and record.get("reference_display_status") != "review"
                    and record.get("reference_output_mode_v29") not in {"REVIEW", "UNRESOLVED"}
                ):
                    # V29 deliberately preserves an unsupported source row (or
                    # its previously generated review placeholder). Its metadata
                    # is reported by quality_report as a review blocker, so it
                    # cannot also be required to equal an unavailable house-style
                    # rendering here.
                    continue
                if element is None or "".join(element.itertext()).strip() != cls.render_entry(
                    record, workflow_version=workflow_version
                ):
                    issues.append(
                        {
                            "code": "reference.mandatory_format_unmet",
                            "severity": "error",
                            "reference_id": record["reference_id"],
                            "block_id": target_id,
                            "message": "必須の書誌書式が最終本文に反映されていません",
                        }
                    )
        if workflow_version >= 22:
            expected, _ = cls.render_sections_v22(
                canonical_html,
                entries,
                machine_entries=machine_entries,
                render_machine_section=workflow_version < 26,
                workflow_version=workflow_version,
            )
            if expected != canonical_html:
                issues.append(
                    {
                        "code": "reference.sections_v22_not_applied",
                        "severity": "error",
                        "reference_id": None,
                        "message": (
                            "展開済み参考文献または機械判別参考文献節が"
                            "最終HTMLへ反映されていません。"
                        ),
                    }
                )
        return issues

    @staticmethod
    def required_missing_fields(entry: dict[str, Any]) -> list[str]:
        record_type = JapaneseReferenceService.bibliography_kind(entry)
        fields = ["author", "title"]
        if record_type == "book":
            fields.extend(("publisher", "year"))
        elif record_type in {"article", "chapter"}:
            fields.extend(("container_title", "year", "pages"))
            if record_type == "chapter":
                fields.append("publisher")
        elif record_type == "nonstandard_contribution":
            fields.extend(("container_title", "year", "url"))
        if entry.get("record_type") in {None, "unknown"}:
            fields.append("record_type")
        return [
            field for field in fields
            if not (
                field == "author" and entry.get("authors")
                or str(entry.get(field) or "").strip()
            )
        ]

    @staticmethod
    def _normalized_pages(value: str) -> str:
        return re.sub(r"\s*[-–—〜～]\s*", "-", value.translate(_FULLWIDTH_DIGITS))

    @staticmethod
    def _first(payload: dict[str, Any], *fields: str) -> str | None:
        for field in fields:
            value = payload.get(field)
            if isinstance(value, list):
                value = value[0] if value else None
            if value is not None and value != "" and not isinstance(value, dict):
                return str(value)
        return None

    @staticmethod
    def _title_score(source: str, candidate: str) -> float:
        # Explicit subtitle delimiters only; do not strip arbitrary Japanese text.
        source = re.split(r"\s+[:：]\s*|[：]", source, maxsplit=1)[0]
        candidate = re.split(r"\s+[:：]\s*|[：]", candidate, maxsplit=1)[0]
        # A parenthesized kana reading immediately after the exact source title
        # is a ruby-style annotation, not a different work title.
        if candidate.startswith(source) and re.match(
            r"^[（(][ァ-ヶー]+[）)](?:\s|$)", candidate[len(source):]
        ):
            candidate = source
        left = _normalize(source)
        right = _normalize(candidate)
        if not left or not right:
            return 0.0
        if left == right:
            return 1.0
        similarity = SequenceMatcher(None, left, right).ratio()
        if left in right or right in left:
            coverage = min(len(left), len(right)) / max(len(left), len(right))
            similarity = max(similarity, coverage * 0.95)
        return similarity

    @staticmethod
    def _author_score(source: str, candidates: Iterable[str]) -> float:
        normalized_source = _normalize(source.replace("自著", ""))
        if not normalized_source:
            return 0.0
        normalized_candidates = [_normalize(item) for item in candidates]
        if any(
            normalized_source in item or item in normalized_source
            for item in normalized_candidates
            if item
        ):
            return 1.0
        source_parts = {
            _normalize(item) for item in re.split(r"[・=＝\s,、]+", source) if _normalize(item)
        }
        if any(part in item for part in source_parts for item in normalized_candidates):
            return 0.75
        return 0.0

    @staticmethod
    def _type_score(source_type: str, candidate_type: str) -> float:
        lowered = candidate_type.casefold()
        article = lowered in {
            "journal-article",
            "article",
            "book-chapter",
            "chapter",
            "記事",
            "論文",
        }
        book = lowered in {"book", "monograph", "edited-book", "図書", "本"}
        if source_type == "article_poem_or_chapter":
            return 1.0 if article else 0.0
        if source_type == "book_or_journal":
            return 1.0 if book else 0.0
        return 0.5
