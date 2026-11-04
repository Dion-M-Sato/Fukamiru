"""Deterministic evidence-linked footnote rendering for canonical HTML."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Literal
from xml.etree import ElementTree as ET

from fukamiru.documents.canonical_html import CanonicalHTMLValidator
from fukamiru.errors import FukamiruError
from fukamiru.revisions.bibliography_footnote_style import (
    FOOTNOTE_REFERENCE_RULESET,
    format_footnote_reference,
)
from fukamiru.revisions.bibliography_style import format_reference

CitationKind = Literal["quote", "paraphrase", "reference"]
_TERMINAL_PUNCTUATION = "。．.!?！？"
_ANCHOR_PUNCTUATION = _TERMINAL_PUNCTUATION + "、，,"
_SENTENCE_PUNCTUATION = "。．.!?！？"
_CLAUSE_PUNCTUATION = "、，,"
_REFERENCE_HEADINGS = {"参考文献", "参照文献", "引用文献", "文献一覧", "主要参考文献"}


@dataclass(frozen=True)
class EvidenceFootnote:
    block_id: str
    evidence_ids: tuple[str, ...]
    reference: dict[str, Any]
    anchor_text: str = ""
    locator: str = ""
    citation_kind: CitationKind = "paraphrase"
    source_span_ids: tuple[str, ...] = ()
    references: tuple[dict[str, Any], ...] = ()
    reference_locators: tuple[str, ...] = ()
    anchor_reference_id: str = ""


@dataclass(frozen=True)
class FootnoteRenderResult:
    html: str
    edits: tuple[dict[str, Any], ...]
    citation_event_drafts: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class FootnotePlanResult:
    citations: tuple[EvidenceFootnote, ...]
    evidence_edit_count: int
    cited_evidence_count: int
    cited_reference_count: int


class EvidenceFootnotePlanner:
    """Resolve applied evidence IDs to final paragraph and bibliography records."""

    _CITABLE_OPERATIONS = {
        "replace_text",
        "insert_paragraph_after",
        "merge_with_next",
    }

    @classmethod
    def plan(
        cls,
        canonical_html: str,
        *,
        applied_edits: list[dict[str, Any]],
        evidence_context: list[dict[str, Any]],
        reference_entries: list[dict[str, Any]],
        require_distinct_anchors: bool = False,
    ) -> FootnotePlanResult:
        root = ET.fromstring(canonical_html)
        paragraphs = {
            str(element.get("id")): element for element in root.iter("p") if element.get("id")
        }
        evidence_by_id = {
            str(item.get("id")): dict(item)
            for item in evidence_context
            if isinstance(item, dict) and item.get("id")
        }
        references = cls._flatten_references(reference_entries)
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        evidence_edit_count = 0

        for edit in applied_edits:
            if not isinstance(edit, dict):
                continue
            evidence_ids = cls._strings(edit.get("evidence_ids"))
            if not evidence_ids or edit.get("operation") not in cls._CITABLE_OPERATIONS:
                continue
            evidence_edit_count += 1
            block_id = str(edit.get("output_block_id") or edit.get("block_id") or "")
            if block_id not in paragraphs:
                raise FukamiruError(
                    "Evidence-linked generated prose has no final paragraph: " + block_id
                )
            unknown = [value for value in evidence_ids if value not in evidence_by_id]
            if unknown:
                raise FukamiruError(
                    "Evidence-linked generated prose references unknown evidence: "
                    + ", ".join(unknown)
                )
            for evidence_id in evidence_ids:
                evidence = evidence_by_id[evidence_id]
                reference = cls._reference_for_evidence(evidence, references)
                reference_key = EvidenceFootnoteRenderer._reference_key(reference)
                group = grouped.setdefault(
                    (block_id, reference_key),
                    {
                        "block_id": block_id,
                        "reference": reference,
                        "evidence_ids": [],
                        "source_span_ids": [],
                        "evidence": [],
                    },
                )
                if evidence_id in group["evidence_ids"]:
                    continue
                group["evidence_ids"].append(evidence_id)
                group["source_span_ids"].append(cls._source_span_id(evidence) or evidence_id)
                group["evidence"].append(evidence)

        citations: list[EvidenceFootnote] = []
        placement_by_block: dict[str, list[dict[str, Any]]] = {}
        for group in grouped.values():
            evidence_ids = tuple(group["evidence_ids"])
            source_span_ids = tuple(group["source_span_ids"])
            reference = {
                **dict(group["reference"]),
                "evidence_ids": list(
                    dict.fromkeys(
                        [
                            *cls._strings(group["reference"].get("evidence_ids")),
                            *evidence_ids,
                        ]
                    )
                ),
                "source_span_ids": list(
                    dict.fromkeys(
                        [
                            *cls._strings(group["reference"].get("source_span_ids")),
                            *source_span_ids,
                        ]
                    )
                ),
            }
            source_asset_ids = list(
                dict.fromkeys(
                    value for item in group["evidence"] if (value := cls._source_asset_id(item))
                )
            )
            if len(source_asset_ids) == 1:
                reference["source_asset_id"] = source_asset_ids[0]
            block_id = str(group["block_id"])
            if require_distinct_anchors:
                reference_id = EvidenceFootnoteRenderer._reference_id(reference)
                explicit_reference_id = cls._explicit_reference_mention(
                    paragraphs[block_id], reference_id
                )
                if not explicit_reference_id:
                    placement = cls.visible_citation_units(
                        "".join(paragraphs[block_id].itertext()),
                        group["evidence"],
                        reference,
                    )
                    if placement is None:
                        raise FukamiruError(
                            "Authoring footnote needs a unique visible source mention "
                            "in its cited sentence: " + block_id
                        )
                    placement_by_block.setdefault(block_id, []).append(placement)
                else:
                    group["anchor_reference_id"] = explicit_reference_id
            citations.append(
                EvidenceFootnote(
                    block_id=block_id,
                    evidence_ids=evidence_ids,
                    source_span_ids=source_span_ids,
                    reference=reference,
                    anchor_text="",
                    locator=cls._locator(group["evidence"], reference),
                    citation_kind="paraphrase",
                    references=(reference,),
                    reference_locators=(cls._locator(group["evidence"], reference),),
                    anchor_reference_id=str(group.get("anchor_reference_id") or ""),
                )
            )

        if require_distinct_anchors:
            positioned: list[tuple[EvidenceFootnote, dict[str, Any]]] = []
            indices_by_block: dict[str, int] = {}
            explicitly_anchored: list[EvidenceFootnote] = []
            for citation in citations:
                if citation.anchor_reference_id:
                    explicitly_anchored.append(citation)
                    continue
                index = indices_by_block.get(citation.block_id, 0)
                indices_by_block[citation.block_id] = index + 1
                positioned.append(
                    (citation, placement_by_block[citation.block_id][index])
                )
            grouped_units: dict[
                tuple[str, int, int], list[tuple[EvidenceFootnote, dict[str, Any]]]
            ] = {}
            for citation, placement in positioned:
                grouped_units.setdefault(
                    (
                        citation.block_id,
                        int(placement["sentence_start"]),
                        int(placement["clause_start"]),
                    ),
                    [],
                ).append((citation, placement))

            merged_units: list[tuple[EvidenceFootnote, dict[str, Any]]] = []
            for entries in grouped_units.values():
                first, placement = entries[0]
                evidence_ids = tuple(
                    dict.fromkeys(
                        evidence_id
                        for citation, _ in entries
                        for evidence_id in citation.evidence_ids
                    )
                )
                span_by_evidence = {
                    evidence_id: source_span_id
                    for citation, _ in entries
                    for evidence_id, source_span_id in zip(
                        citation.evidence_ids,
                        citation.source_span_ids or citation.evidence_ids,
                        strict=True,
                    )
                }
                references: list[dict[str, Any]] = []
                reference_locators: list[str] = []
                reference_keys: set[str] = set()
                for citation, _ in entries:
                    citation_references = citation.references or (citation.reference,)
                    citation_locators = citation.reference_locators or (citation.locator,)
                    for reference, locator in zip(
                        citation_references, citation_locators, strict=True
                    ):
                        reference_key = EvidenceFootnoteRenderer._reference_key(reference)
                        if reference_key in reference_keys:
                            continue
                        reference_keys.add(reference_key)
                        references.append(reference)
                        reference_locators.append(locator)
                merged_units.append(
                    (
                        EvidenceFootnote(
                            block_id=first.block_id,
                            evidence_ids=evidence_ids,
                            source_span_ids=tuple(
                                span_by_evidence[evidence_id]
                                for evidence_id in evidence_ids
                            ),
                            reference=references[0],
                            references=tuple(references),
                            reference_locators=tuple(reference_locators),
                            anchor_text="",
                            locator=reference_locators[0],
                            citation_kind=first.citation_kind,
                        ),
                        placement,
                    )
                )

            anchored: list[EvidenceFootnote] = list(explicitly_anchored)
            for citation, placement in merged_units:
                sentence_peers = [
                    peer_placement
                    for peer_citation, peer_placement in merged_units
                    if peer_citation.block_id == citation.block_id
                    and peer_placement["sentence_start"] == placement["sentence_start"]
                ]
                anchor_text = str(
                    placement["clause"]
                    if len(sentence_peers) > 1
                    else placement["sentence"]
                )
                block_id = citation.block_id
                if "".join(paragraphs[block_id].itertext()).count(anchor_text) != 1:
                    raise FukamiruError(
                        "Authoring footnote sentence/clause anchor is ambiguous: " + block_id
                    )
                anchored.append(
                    EvidenceFootnote(
                        block_id=citation.block_id,
                        evidence_ids=citation.evidence_ids,
                        reference=citation.reference,
                        anchor_text=anchor_text,
                        locator=citation.locator,
                        citation_kind=citation.citation_kind,
                        source_span_ids=citation.source_span_ids,
                        references=citation.references,
                        reference_locators=citation.reference_locators,
                    )
                )
            paragraph_order = {
                str(paragraph.get("id")): index
                for index, paragraph in enumerate(root.iter("p"))
                if paragraph.get("id")
            }
            citations = sorted(
                anchored,
                key=lambda item: (
                    paragraph_order[item.block_id],
                    cls._citation_anchor_position(paragraphs[item.block_id], item),
                ),
            )

        return FootnotePlanResult(
            citations=tuple(citations),
            evidence_edit_count=evidence_edit_count,
            cited_evidence_count=len(
                {evidence_id for citation in citations for evidence_id in citation.evidence_ids}
            ),
            cited_reference_count=len(
                {
                    EvidenceFootnoteRenderer._reference_key(reference)
                    for citation in citations
                    for reference in (citation.references or (citation.reference,))
                }
            ),
        )

    @staticmethod
    def _explicit_reference_mention(paragraph: ET.Element, reference_id: str) -> str:
        if not reference_id:
            return ""
        matches = [
            cite
            for cite in paragraph.iter("cite")
            if cite.get("data-reference-id") == reference_id
        ]
        if len(matches) > 1:
            raise FukamiruError(
                "A paragraph must contain one explicit title mention per reference ID: "
                + reference_id
            )
        if matches and not "".join(matches[0].itertext()).strip():
            raise FukamiruError("Explicit citation title mention is empty: " + reference_id)
        return reference_id if matches else ""

    @staticmethod
    def _citation_anchor_position(
        paragraph: ET.Element, citation: EvidenceFootnote
    ) -> int:
        visible = "".join(paragraph.itertext())
        if citation.anchor_reference_id:
            matches = [
                cite
                for cite in paragraph.iter("cite")
                if cite.get("data-reference-id") == citation.anchor_reference_id
            ]
            if len(matches) != 1:
                raise FukamiruError(
                    "Explicit citation title mention disappeared before rendering: "
                    + citation.anchor_reference_id
                )
            target = matches[0]
            offset = 0
            target_position: int | None = None

            def visit(element: ET.Element) -> None:
                nonlocal offset, target_position
                offset += len(element.text or "")
                for child in element:
                    if child is target:
                        target_position = offset
                        return
                    visit(child)
                    if target_position is not None:
                        return
                    offset += len(child.tail or "")

            visit(paragraph)
            if target_position is None:
                raise FukamiruError(
                    "Explicit citation title has no visible position: "
                    + citation.anchor_reference_id
                )
            return target_position
        return visible.index(citation.anchor_text)

    @staticmethod
    def visible_citation_units(
        text: str,
        evidence: list[dict[str, Any]],
        reference: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Locate a unique sentence and comma clause that visibly names a source.

        Bibliographic metadata and prose often differ only in Japanese punctuation,
        spacing, or subtitle marks. Normalize those surface differences while
        retaining offsets into the original prose; do not accept repeated anchors.
        """

        candidates: list[str] = []
        for item in evidence:
            excerpt = str(item.get("excerpt") or "")
            for quoted in re.findall(r"[『「][^『「』」]{2,}[』」]", excerpt):
                candidates.extend((quoted, f"『{quoted[1:-1]}』", f"「{quoted[1:-1]}」"))
            title = str(item.get("title") or "").strip()
            if title:
                candidates.extend((f"『{title}』", f"「{title}」"))
        title = str(reference.get("title") or "").strip()
        if title:
            candidates.extend((f"『{title}』", f"「{title}」"))
        for item in evidence:
            candidates.extend(
                str(author) for author in item.get("authors") or []
                if str(author) not in {"自著", "筆者", "本稿筆者"}
            )

        def normalized_with_offsets(value: str, offset: int = 0) -> tuple[str, list[int]]:
            normalized_chars: list[str] = []
            offsets: list[int] = []
            for index, original_char in enumerate(value):
                for char in unicodedata.normalize("NFKC", original_char).casefold():
                    if char.isspace() or unicodedata.category(char).startswith(("P", "Z")):
                        continue
                    normalized_chars.append(char)
                    offsets.append(offset + index)
            return "".join(normalized_chars), offsets

        # A model may include a Markdown footnote definition in the paragraph
        # text. It is source metadata, not a second reader-facing citation
        # sentence. Mask it only for matching so returned offsets and the
        # original note text remain intact for subsequent processing.
        search_text = re.sub(
            r"(?m)^[ \t]*\[\^[^\]\r\n]+\]:[^\r\n]*(?:\r?\n[ \t]+[^\r\n]*)*",
            lambda match: re.sub(r"[^\r\n]", " ", match.group()),
            text,
        )
        sentence_ranges: list[tuple[int, int]] = []
        sentence_start = 0
        for index, char in enumerate(search_text):
            if char in _SENTENCE_PUNCTUATION:
                sentence_ranges.append((sentence_start, index + 1))
                sentence_start = index + 1
        if sentence_start < len(search_text):
            # A footnote anchor must remain a complete reader-facing sentence.
            sentence_ranges.append((sentence_start, len(search_text)))

        for candidate in dict.fromkeys(candidates):
            normalized_candidate, _ = normalized_with_offsets(candidate)
            if not normalized_candidate:
                continue
            matches: list[tuple[int, int, int, int]] = []
            for raw_sentence_start, raw_sentence_end in sentence_ranges:
                normalized_sentence, offsets = normalized_with_offsets(
                    search_text[raw_sentence_start:raw_sentence_end], raw_sentence_start
                )
                search_from = 0
                while True:
                    match_start = normalized_sentence.find(
                        normalized_candidate, search_from
                    )
                    if match_start < 0:
                        break
                    match_end = match_start + len(normalized_candidate)
                    matches.append((
                        offsets[match_start],
                        offsets[match_end - 1] + 1,
                        raw_sentence_start,
                        raw_sentence_end,
                    ))
                    search_from = match_start + 1
            if len(matches) == 1:
                position, candidate_end, sentence_start, sentence_end = matches[0]
                if (
                    sentence_end <= sentence_start
                    or text[sentence_end - 1] not in _SENTENCE_PUNCTUATION
                ):
                    continue
                sentence_end -= 1
                clause_start = max(
                    [sentence_start - 1]
                    + [
                        text.rfind(mark, sentence_start, position)
                        for mark in _CLAUSE_PUNCTUATION
                    ],
                ) + 1
                clause_ends = [
                    text.find(mark, candidate_end, sentence_end)
                    for mark in _CLAUSE_PUNCTUATION
                ]
                clause_end = min((value for value in clause_ends if value >= 0), default=-1)
                if clause_end < 0:
                    clause_end = sentence_end
                return {
                    "sentence_start": sentence_start,
                    "sentence": text[sentence_start : sentence_end + 1].strip(),
                    "clause_start": clause_start,
                    "clause": text[clause_start : clause_end + 1].strip(),
                }
        return None

    @classmethod
    def _flatten_references(cls, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        flattened: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            expanded = [
                dict(item)
                for item in entry.get("expanded_reference_entries") or []
                if isinstance(item, dict)
            ]
            flattened.extend(expanded or [dict(entry)])
        return flattened

    @classmethod
    def _reference_for_evidence(
        cls,
        evidence: dict[str, Any],
        references: list[dict[str, Any]],
    ) -> dict[str, Any]:
        evidence_id = str(evidence.get("id") or "")
        source_span_id = cls._source_span_id(evidence)
        provider_key = (
            str(evidence.get("provider") or ""),
            str(evidence.get("provider_record_id") or ""),
        )

        def direct(reference: dict[str, Any]) -> bool:
            usage = reference.get("reference_usage")
            usage = usage if isinstance(usage, dict) else {}
            return evidence_id in cls._strings(reference.get("evidence_ids")) or (
                evidence_id and evidence_id == str(usage.get("evidence_id") or "")
            )

        def by_span(reference: dict[str, Any]) -> bool:
            return bool(source_span_id) and source_span_id in cls._reference_source_spans(reference)

        def by_provider(reference: dict[str, Any]) -> bool:
            return bool(all(provider_key)) and provider_key == (
                str(reference.get("provider") or ""),
                str(reference.get("provider_record_id") or ""),
            )

        for matcher in (direct, by_span, by_provider):
            matched = [reference for reference in references if matcher(reference)]
            if len(matched) == 1:
                return matched[0]
            if len(matched) > 1:
                raise FukamiruError(
                    "Evidence maps to multiple final bibliography records: " + evidence_id
                )
        raise FukamiruError(
            "Evidence-linked generated prose has no final bibliography record: " + evidence_id
        )

    @staticmethod
    def _strings(value: Any) -> list[str]:
        if not isinstance(value, (list, tuple, set)):
            return []
        return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))

    @staticmethod
    def _citation(evidence: dict[str, Any]) -> dict[str, Any]:
        provenance = evidence.get("provenance")
        provenance = provenance if isinstance(provenance, dict) else {}
        citation = provenance.get("citation")
        return dict(citation) if isinstance(citation, dict) else {}

    @classmethod
    def _source_span_id(cls, evidence: dict[str, Any]) -> str:
        return str(
            evidence.get("source_span_id") or cls._citation(evidence).get("source_span_id") or ""
        )

    @classmethod
    def _source_asset_id(cls, evidence: dict[str, Any]) -> str:
        return str(
            evidence.get("source_asset_id") or cls._citation(evidence).get("source_asset_id") or ""
        )

    @classmethod
    def _reference_source_spans(cls, reference: dict[str, Any]) -> set[str]:
        retrieval = reference.get("retrieval_provenance")
        retrieval = retrieval if isinstance(retrieval, dict) else {}
        citation = retrieval.get("citation")
        citation = citation if isinstance(citation, dict) else {}
        return {
            *cls._strings(reference.get("source_span_ids")),
            *({str(citation["source_span_id"])} if citation.get("source_span_id") else set()),
        }

    @classmethod
    def _locator(
        cls,
        evidence: list[dict[str, Any]],
        reference: dict[str, Any],
    ) -> str:
        pages: list[int] = []
        for item in evidence:
            citation = cls._citation(item)
            for field in ("start_page", "end_page"):
                value = citation.get(field)
                if isinstance(value, int) or str(value or "").isdigit():
                    pages.append(int(value))
        if pages:
            start, end = min(pages), max(pages)
            return str(start) if start == end else f"{start}-{end}"
        for item in evidence:
            value = str(item.get("locator") or "").strip()
            if re.fullmatch(r"(?:p{1,2}\.\s*)?\d+(?:\s*[-–—〜～]\s*\d+)?", value, re.I):
                return re.sub(r"^p{1,2}\.\s*", "", value, flags=re.I)
        return str(reference.get("pages") or "").strip()


class EvidenceFootnoteRenderer:
    """Turn explicit evidence links into visible notes without inferring quotes."""

    profile_id = str(FOOTNOTE_REFERENCE_RULESET["id"])

    @staticmethod
    def align_existing_markers_before_punctuation(
        canonical_html: str,
    ) -> tuple[str, tuple[str, ...]]:
        """Keep V32 markers next to punctuation after prose text slots are rewritten."""
        root = ET.fromstring(canonical_html)
        unresolved: list[str] = []
        changed = False
        for paragraph in root.iter("p"):
            for parent in paragraph.iter():
                for marker in list(parent):
                    if marker.tag != "sup" or not str(marker.get("id") or "").startswith(
                        "FNREF-"
                    ):
                        continue
                    tail = marker.tail or ""
                    punctuation = next(
                        (index for index, char in enumerate(tail) if char in _ANCHOR_PUNCTUATION),
                        None,
                    )
                    if punctuation is None:
                        following = list(parent)
                        index = following.index(marker)
                        if (
                            not tail.strip()
                            and index + 1 < len(following)
                            and following[index + 1].tag == "sup"
                            and str(following[index + 1].get("id") or "").startswith("FNREF-")
                        ):
                            continue
                        unresolved.append(str(paragraph.get("id") or ""))
                        continue
                    if punctuation == 0:
                        continue
                    prefix = tail[:punctuation]
                    siblings = list(parent)
                    index = siblings.index(marker)
                    if index:
                        previous = siblings[index - 1]
                        if previous.tag == "sup" and str(
                            previous.get("id") or ""
                        ).startswith("FNREF-"):
                            unresolved.append(str(paragraph.get("id") or ""))
                            continue
                        previous.tail = (previous.tail or "") + prefix
                    else:
                        parent.text = (parent.text or "") + prefix
                    marker.tail = tail[punctuation:]
                    changed = True
        if not changed:
            return canonical_html, tuple(dict.fromkeys(unresolved))
        aligned = ET.tostring(root, encoding="unicode", short_empty_elements=False)
        CanonicalHTMLValidator().assert_valid(aligned)
        return aligned, tuple(dict.fromkeys(unresolved))

    @classmethod
    def render(
        cls,
        canonical_html: str,
        citations: list[EvidenceFootnote],
        *,
        place_before_punctuation: bool = False,
    ) -> FootnoteRenderResult:
        if not citations:
            CanonicalHTMLValidator().assert_valid(canonical_html)
            return FootnoteRenderResult(canonical_html, (), ())
        root = ET.fromstring(canonical_html)
        elements = {str(element.get("id")): element for element in root.iter() if element.get("id")}
        edits: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        bibliography = cls._terminal_reference_section(root)
        notes = cls._note_section(root)
        ordered_list = next((child for child in notes if child.tag == "ol"), None)
        if ordered_list is None:
            ordered_list = ET.SubElement(notes, "ol", {"id": notes.get("id") + ".list"})

        note_numbers = [
            int(item.get("data-note-number"))
            for item in ordered_list
            if str(item.get("data-note-number") or "").isdigit()
        ]
        next_number = max(note_numbers, default=0) + 1

        for citation in citations:
            cls._validate_citation(citation)
            target = elements.get(citation.block_id)
            if target is None or target.tag != "p":
                raise FukamiruError(
                    f"Footnote target is not a canonical paragraph: {citation.block_id}"
                )
            references = cls._citation_references(citation)
            locators = cls._citation_reference_locators(citation, references)
            styled_references: list[Any] = []
            for reference, locator in zip(references, locators, strict=True):
                styled = format_footnote_reference(reference, locator=locator)
                if not styled.supported:
                    raise FukamiruError(
                        "Footnote bibliography metadata requires review: "
                        + ", ".join(styled.review_reasons)
                    )
                bibliography_style = format_reference(reference)
                if not bibliography_style.supported:
                    raise FukamiruError(
                        "Terminal bibliography metadata requires review: "
                        + ", ".join(bibliography_style.review_reasons)
                    )
                cls._ensure_terminal_reference(
                    bibliography,
                    citation,
                    reference,
                    bibliography_style,
                    edits,
                )
                styled_references.append(styled)
            rendered_note_text = "；".join(
                styled.text for styled in styled_references
            )
            stable = cls._stable_key(citation)
            marker_id = f"FNREF-{stable}"
            note_id = f"FN-{stable}"
            marker = elements.get(marker_id)
            note = elements.get(note_id)
            if note is not None:
                number = int(note.get("data-note-number") or "0")
            else:
                number = next_number
                next_number += 1
            if marker is None:
                marker = cls._marker(
                    marker_id,
                    note_id,
                    number,
                    citation.evidence_ids,
                )
                if place_before_punctuation:
                    cls._insert_marker_before_next_punctuation(target, marker, citation)
                elif citation.anchor_reference_id:
                    cls._insert_marker_after_reference_cite(
                        target, marker, citation.anchor_reference_id
                    )
                else:
                    cls._insert_marker(
                        target, marker, citation.anchor_text,
                        place_before_punctuation=place_before_punctuation,
                    )
                elements[marker_id] = marker
                edits.append(
                    {
                        "operation": "insert_evidence_footnote_marker",
                        "block_id": citation.block_id,
                        "marker_id": marker_id,
                        "note_number": number,
                        "evidence_ids": list(citation.evidence_ids),
                    }
                )
            if note is None:
                note = cls._note_item(
                    note_id,
                    marker_id,
                    number,
                    citation,
                    styled_references,
                )
                ordered_list.append(note)
                elements[note_id] = note
                edits.append(
                    {
                        "operation": "insert_evidence_footnote_body",
                        "block_id": citation.block_id,
                        "note_id": note_id,
                        "note_number": number,
                        "rendered_text": rendered_note_text,
                        "reference_id": (
                            cls._reference_id(references[0])
                            if len(references) == 1
                            else None
                        ),
                        "reference_ids": [
                            value
                            for reference in references
                            if (value := cls._reference_id(reference))
                        ],
                        "evidence_ids": list(citation.evidence_ids),
                    }
                )
            elif "；".join(
                "".join(cite.itertext()).strip() for cite in note.iter("cite")
            ) != rendered_note_text:
                # A saved candidate may already contain generated notes. Refresh
                # their display when the identified bibliography record changes,
                # while retaining the stable marker, note number and evidence IDs.
                refreshed = cls._note_item(
                    note_id, marker_id, number, citation, styled_references
                )
                note.attrib.clear()
                note.attrib.update(refreshed.attrib)
                note.text = refreshed.text
                note[:] = list(refreshed)
                edits.append(
                    {
                        "operation": "refresh_evidence_footnote_body",
                        "block_id": citation.block_id,
                        "note_id": note_id,
                        "note_number": number,
                        "rendered_text": rendered_note_text,
                        "evidence_ids": list(citation.evidence_ids),
                    }
                )
            events.extend(
                cls._event_drafts(
                    citation, marker_id, note_id, rendered_note_text
                )
            )

        if len(ordered_list) == 0:
            root.remove(notes)
        result = ET.tostring(root, encoding="unicode", short_empty_elements=False)
        CanonicalHTMLValidator().assert_valid(result)
        return FootnoteRenderResult(result, tuple(edits), tuple(events))

    @classmethod
    def _terminal_reference_section(cls, root: ET.Element) -> ET.Element:
        existing = next(
            (section for section in reversed(list(root)) if cls._is_reference_section(section)),
            None,
        )
        if existing is not None:
            return existing
        article_id = str(root.get("id") or "ARTICLE")
        section_id = article_id + ".consulted-references"
        section = ET.Element(
            "section",
            {
                "id": section_id,
                "data-reference-section": "consulted-generated-v22",
                "data-reference-role": "consulted-or-cited",
            },
        )
        heading = ET.SubElement(section, "h2", {"id": section_id + ".heading"})
        heading.text = "参考文献"
        root.append(section)
        return section

    @classmethod
    def _ensure_terminal_reference(
        cls,
        section: ET.Element,
        citation: EvidenceFootnote,
        reference: dict[str, Any],
        styled: Any,
        edits: list[dict[str, Any]],
    ) -> None:
        reference_key = cls._reference_key(reference)
        rendered_text = styled.text.strip()
        reference_id = cls._reference_id(reference)
        for element in section.iter():
            if element.get("data-reference-key") == reference_key:
                return
            if reference_id and element.get("data-reference-id") == reference_id:
                return
            if element.tag in {"p", "li"} and "".join(element.itertext()).strip() == rendered_text:
                return

        article_id = str(section.get("id") or "ARTICLE.consulted-references")
        block_id = f"{article_id}:footnote-ref:{reference_key[:16]}"
        attributes = {
            "id": block_id,
            "data-reference-generated": "evidence-footnote-v1",
            "data-reference-key": reference_key,
        }
        if reference_id:
            attributes["data-reference-id"] = reference_id
        paragraph = ET.SubElement(section, "p", attributes)
        cite = ET.SubElement(paragraph, "cite")
        styled.write_into(cite)
        edits.append(
            {
                "operation": "insert_footnote_reference_in_terminal_bibliography",
                "block_id": block_id,
                "rendered_text": styled.text,
                "reference_id": reference_id or None,
                "evidence_ids": [
                    evidence_id
                    for evidence_id in citation.evidence_ids
                    if evidence_id
                    in {str(item) for item in reference.get("evidence_ids") or []}
                ],
            }
        )

    @classmethod
    def _note_section(cls, root: ET.Element) -> ET.Element:
        article_id = str(root.get("id") or "ARTICLE")
        section_id = article_id + ".evidence-footnotes"
        existing = next(
            (
                section
                for section in root
                if section.get("data-citation-section") == "evidence-footnotes-v1"
            ),
            None,
        )
        if existing is not None:
            return existing
        section = ET.Element(
            "section",
            {
                "id": section_id,
                "data-citation-section": "evidence-footnotes-v1",
                "data-footnote-style": cls.profile_id,
            },
        )
        heading = ET.SubElement(section, "h2", {"id": section_id + ".heading"})
        heading.text = "注"
        insertion_index = cls._reference_section_index(root)
        root.insert(insertion_index, section)
        return section

    @staticmethod
    def _reference_section_index(root: ET.Element) -> int:
        for index, section in enumerate(list(root)):
            if EvidenceFootnoteRenderer._is_reference_section(section):
                return index
        return len(root)

    @staticmethod
    def _is_reference_section(section: ET.Element) -> bool:
        if section.tag != "section":
            return False
        if section.get("data-reference-section"):
            return True
        first = next(iter(section), None)
        heading = "".join(first.itertext()).strip() if first is not None else ""
        return heading in _REFERENCE_HEADINGS

    @staticmethod
    def _validate_citation(citation: EvidenceFootnote) -> None:
        if not citation.evidence_ids:
            raise FukamiruError("Generated footnotes require at least one evidence ID")
        if len(citation.evidence_ids) != len(set(citation.evidence_ids)):
            raise FukamiruError("Generated footnote evidence IDs must be unique")
        if citation.source_span_ids and len(citation.source_span_ids) != len(citation.evidence_ids):
            raise FukamiruError("Each evidence ID requires one source span ID")
        if citation.citation_kind not in {"quote", "paraphrase", "reference"}:
            raise FukamiruError("Unknown citation kind")
        references = EvidenceFootnoteRenderer._citation_references(citation)
        declared_evidence = {
            str(value)
            for reference in references
            for value in reference.get("evidence_ids") or []
        }
        declared_spans = {
            str(value)
            for reference in references
            for value in reference.get("source_span_ids") or []
        }
        if not (
            set(citation.evidence_ids).issubset(declared_evidence)
            or (citation.source_span_ids and set(citation.source_span_ids).issubset(declared_spans))
        ):
            raise FukamiruError(
                "Footnote reference is not explicitly linked to the supplied evidence"
            )

    @staticmethod
    def _stable_key(citation: EvidenceFootnote) -> str:
        references = EvidenceFootnoteRenderer._citation_references(citation)
        payload = "\x1f".join(
            (
                citation.block_id,
                *citation.evidence_ids,
                *(
                    EvidenceFootnoteRenderer._reference_id(reference)
                    or EvidenceFootnoteRenderer._reference_key(reference)
                    for reference in references
                ),
            )
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16].upper()

    @staticmethod
    def _reference_id(reference: dict[str, Any]) -> str:
        return str(reference.get("reference_id") or reference.get("bibliographic_record_id") or "")

    @staticmethod
    def _reference_key(reference: dict[str, Any]) -> str:
        identity_fields = {
            key: reference.get(key)
            for key in (
                "record_type",
                "bibliography_kind",
                "title",
                "container_title",
                "volume",
                "issue",
                "edition",
                "publication_place",
                "publisher",
                "year",
                "url",
                "bibliography_people",
            )
            if reference.get(key) not in (None, "", [])
        }
        payload = json.dumps(
            identity_fields,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest().upper()

    @staticmethod
    def _marker(
        marker_id: str,
        note_id: str,
        number: int,
        evidence_ids: tuple[str, ...],
    ) -> ET.Element:
        marker = ET.Element(
            "sup",
            {
                "id": marker_id,
                "data-citation-generated": "evidence-footnote-v1",
                "data-evidence-ids": " ".join(evidence_ids),
            },
        )
        link = ET.SubElement(
            marker,
            "a",
            {
                "href": "#" + note_id,
                "role": "doc-noteref",
                "aria-label": f"脚注{number}",
            },
        )
        link.text = str(number)
        return marker

    @staticmethod
    def _insert_marker(
        paragraph: ET.Element,
        marker: ET.Element,
        anchor_text: str,
        *,
        place_before_punctuation: bool = False,
    ) -> None:
        if not anchor_text:
            EvidenceFootnoteRenderer._append_before_terminal_punctuation(
                paragraph, marker, place_before_punctuation=place_before_punctuation
            )
            return
        matches: list[tuple[ET.Element, str, int]] = []
        for node in paragraph.iter():
            if node.text and anchor_text in node.text:
                matches.append((node, "text", node.text.index(anchor_text)))
            if node is not paragraph and node.tail and anchor_text in node.tail:
                matches.append((node, "tail", node.tail.index(anchor_text)))
        if len(matches) != 1:
            raise FukamiruError(
                "Footnote anchor must occur exactly once inside one text node: " + anchor_text
            )
        node, slot, start = matches[0]
        if slot == "text" and node.tag in {"a", "cite", "code", "q", "sup"}:
            raise FukamiruError("Footnote anchor cannot modify protected inline citation markup")
        value = str(getattr(node, slot))
        stripped = anchor_text.rstrip(_ANCHOR_PUNCTUATION)
        insertion = start + len(stripped)
        before, after = value[:insertion], value[insertion:]
        setattr(node, slot, before)
        marker.tail = after
        if slot == "text":
            node.insert(0, marker)
            return
        parent = next(candidate for candidate in paragraph.iter() if node in list(candidate))
        parent.insert(list(parent).index(node) + 1, marker)

    @staticmethod
    def _insert_marker_after_reference_cite(
        paragraph: ET.Element,
        marker: ET.Element,
        reference_id: str,
    ) -> None:
        matches = [
            cite
            for cite in paragraph.iter("cite")
            if cite.get("data-reference-id") == reference_id
        ]
        if len(matches) != 1:
            raise FukamiruError(
                "Footnote needs one semantic title tag for reference ID: " + reference_id
            )
        cite = matches[0]
        parent = next(candidate for candidate in paragraph.iter() if cite in list(candidate))
        marker.tail = cite.tail or ""
        cite.tail = None
        parent.insert(list(parent).index(cite) + 1, marker)

    @classmethod
    def _insert_marker_before_next_punctuation(
        cls,
        paragraph: ET.Element,
        marker: ET.Element,
        citation: EvidenceFootnote,
    ) -> None:
        """Place a V32 marker at the end of its cited clause or sentence."""
        if not citation.anchor_reference_id and not citation.anchor_text:
            cls._append_before_terminal_punctuation(
                paragraph, marker, place_before_punctuation=True
            )
            return
        protected = {"a", "cite", "code", "q", "sup"}
        slots: list[tuple[ET.Element, str]] = []

        def collect(node: ET.Element) -> None:
            if node.tag not in protected:
                slots.append((node, "text"))
                for child in node:
                    collect(child)
                    slots.append((child, "tail"))

        collect(paragraph)
        if citation.anchor_reference_id:
            matches = [
                cite
                for cite in paragraph.iter("cite")
                if cite.get("data-reference-id") == citation.anchor_reference_id
            ]
            if len(matches) != 1:
                raise FukamiruError(
                    "Footnote needs one semantic title tag for reference ID: "
                    + citation.anchor_reference_id
                )
            start_slot = slots.index((matches[0], "tail"))
            start_offset = 0
        else:
            anchor = citation.anchor_text
            matches = [
                (index, value.index(anchor) + len(anchor.rstrip(_ANCHOR_PUNCTUATION)))
                for index, (node, slot) in enumerate(slots)
                if (value := str(getattr(node, slot) or "")) and anchor in value
            ]
            if len(matches) != 1:
                raise FukamiruError(
                    "Footnote anchor must occur exactly once inside one text node: "
                    + anchor
                )
            start_slot, start_offset = matches[0]

        for node, slot in slots[start_slot:]:
            value = str(getattr(node, slot) or "")
            offset = start_offset if (node, slot) == slots[start_slot] else 0
            punctuation = next(
                (
                    index
                    for index in range(offset, len(value))
                    if value[index] in _ANCHOR_PUNCTUATION
                ),
                None,
            )
            if punctuation is None:
                continue
            setattr(node, slot, value[:punctuation])
            marker.tail = value[punctuation:]
            if slot == "text":
                node.insert(0, marker)
            else:
                parent = next(
                    candidate for candidate in paragraph.iter() if node in list(candidate)
                )
                parent.insert(list(parent).index(node) + 1, marker)
            return
        cls._append_before_terminal_punctuation(
            paragraph, marker, place_before_punctuation=True
        )

    @staticmethod
    def _append_before_terminal_punctuation(
        paragraph: ET.Element,
        marker: ET.Element,
        *,
        place_before_punctuation: bool = False,
    ) -> None:
        children = list(paragraph)
        if children and not str(children[-1].tail or "").strip():
            paragraph.append(marker)
            return
        slots: list[tuple[ET.Element, str]] = [(paragraph, "text")]
        slots.extend((node, "tail") for node in paragraph.iter() if node is not paragraph)
        node, slot = next(
            (item for item in reversed(slots) if str(getattr(item[0], item[1]) or "").strip()),
            (paragraph, "text"),
        )
        value = str(getattr(node, slot) or "")
        stripped = value.rstrip()
        whitespace = value[len(stripped) :]
        punctuation = ""
        punctuation_chars = (
            _ANCHOR_PUNCTUATION if place_before_punctuation else _TERMINAL_PUNCTUATION
        )
        if stripped and stripped[-1] in punctuation_chars:
            punctuation = stripped[-1]
            stripped = stripped[:-1]
        setattr(node, slot, stripped)
        marker.tail = punctuation + whitespace
        if slot == "text":
            node.append(marker)
            return
        parent = next(candidate for candidate in paragraph.iter() if node in list(candidate))
        parent.insert(list(parent).index(node) + 1, marker)

    @staticmethod
    def _note_item(
        note_id: str,
        marker_id: str,
        number: int,
        citation: EvidenceFootnote,
        styled_references: list[Any],
    ) -> ET.Element:
        references = EvidenceFootnoteRenderer._citation_references(citation)
        reference_ids = [
            value
            for reference in references
            if (value := EvidenceFootnoteRenderer._reference_id(reference))
        ]
        attributes = {
            "id": note_id,
            "data-note-number": str(number),
            "data-evidence-ids": " ".join(citation.evidence_ids),
        }
        if len(reference_ids) == 1:
            attributes["data-reference-id"] = reference_ids[0]
        elif reference_ids:
            attributes["data-reference-ids"] = " ".join(reference_ids)
        source_asset_ids = list(
            dict.fromkeys(
                str(reference.get("source_asset_id") or "")
                for reference in references
                if str(reference.get("source_asset_id") or "")
            )
        )
        if len(source_asset_ids) == 1:
            attributes["data-source-asset-id"] = source_asset_ids[0]
        elif source_asset_ids:
            attributes["data-source-asset-ids"] = " ".join(source_asset_ids)
        item = ET.Element(
            "li",
            attributes,
        )
        paragraph = ET.SubElement(item, "p", {"id": note_id + ".body"})
        for index, styled in enumerate(styled_references):
            cite = ET.SubElement(paragraph, "cite")
            styled.write_into(cite)
            cite.tail = "；" if index < len(styled_references) - 1 else " "
        backlink = ET.SubElement(
            paragraph,
            "a",
            {
                "href": "#" + marker_id,
                "role": "doc-backlink",
                "aria-label": f"脚注{number}の参照位置へ戻る",
            },
        )
        backlink.text = "↩"
        return item

    @staticmethod
    def _event_drafts(
        citation: EvidenceFootnote,
        marker_id: str,
        note_id: str,
        rendered_text: str,
    ) -> list[dict[str, Any]]:
        source_span_ids = citation.source_span_ids or citation.evidence_ids
        events = []
        references = EvidenceFootnoteRenderer._citation_references(citation)
        for evidence_id, source_span_id in zip(
            citation.evidence_ids,
            source_span_ids,
            strict=True,
        ):
            matching_references = [
                reference
                for reference in references
                if evidence_id
                in {str(item) for item in reference.get("evidence_ids") or []}
                or source_span_id
                in {str(item) for item in reference.get("source_span_ids") or []}
            ]
            if len(matching_references) != 1:
                raise FukamiruError(
                    "Each footnote evidence ID must map to one bibliography record"
                )
            reference = matching_references[0]
            event_id = (
                "citation-event:"
                + hashlib.sha256(
                    "\x1f".join((citation.block_id, evidence_id, marker_id, note_id)).encode()
                ).hexdigest()[:24]
            )
            events.append(
                {
                    "id": event_id,
                    "block_id": citation.block_id,
                    "source_span_id": source_span_id,
                    "bibliographic_record_id": EvidenceFootnoteRenderer._reference_id(
                        reference
                    )
                    or None,
                    "citation_kind": citation.citation_kind,
                    "locator": citation.locator or None,
                    "rendered_text": rendered_text,
                    "evidence_id": evidence_id,
                    "marker_id": marker_id,
                    "note_id": note_id,
                }
            )
        return events

    @staticmethod
    def _citation_references(
        citation: EvidenceFootnote,
    ) -> tuple[dict[str, Any], ...]:
        return citation.references or (citation.reference,)

    @staticmethod
    def _citation_reference_locators(
        citation: EvidenceFootnote,
        references: tuple[dict[str, Any], ...],
    ) -> tuple[str, ...]:
        if citation.reference_locators:
            if len(citation.reference_locators) != len(references):
                raise FukamiruError(
                    "Grouped footnote reference locators do not match references"
                )
            return citation.reference_locators
        return (citation.locator, *([""] * (len(references) - 1)))
