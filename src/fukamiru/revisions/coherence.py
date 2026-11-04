"""Deterministic guardrails for document-level manuscript coherence plans."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any, Literal
from xml.etree import ElementTree as ET

from fukamiru.documents.japanese_surface import JapaneseSurfaceQualityGate

_EDITABLE_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "blockquote", "li"}
_HEADING_TAGS = {f"h{level}" for level in range(1, 7)}
_MARKDOWN_HEADING = re.compile(r"^\s*(#{1,6})\s+(.+?)\s*$", re.DOTALL)
_NUMBERED_HEADING = re.compile(
    r"^\s*(?:(?:第[一二三四五六七八九十百0-9０-９]+[部章節])|"
    r"(?:[0-9０-９]+|[一二三四五六七八九十]+)[\s　]+)[^。！？]{1,120}$"
)
_NAMED_SECTION_HEADING = re.compile(
    r"^\s*(?:序|序論|緒言|はじめに|結論|終章|おわりに|まとめ)"
    r"(?:[\s　]+[^。！？]{1,120})?\s*$"
)
_REFERENCE_SECTION_HEADING = re.compile(
    r"^\s*(?:#{1,6}\s+)?"
    r"(主要参照文献一覧|主要参考文献一覧|参考文献一覧|参照文献一覧|"
    r"主要参照文献|主要参考文献|参考文献|参照文献|引用文献|文献一覧)"
)
_REFERENCE_PROSE_CONTINUATION = re.compile(
    r"^(?:の|を|が|は|に|で|と|へ|も|や|から|より|について|に関して)"
)
_NOTE_SECTION_TITLE = re.compile(r"^\s*(?:注|脚注|註)\s*$")
_CONCLUSION_SECTION_TITLE = re.compile(r"^\s*(?:結(?:[\s　]|$)|結論|終章|おわりに|まとめ)")
_LIMITATIONS_SECTION_TITLE = re.compile(
    r"(?:適用範囲|検証課題|今後の課題|今後の研究|研究課題|研究の限界|限界と展望)"
)
_TERMINAL_SEQUENCE_ARROW = re.compile(r"\s*(?:→|⇒|➝|->|—>)\s*")
_TERMINAL_ROLE_ORDER = {
    "argument": 0,
    "conclusion": 1,
    "limitations": 2,
    "notes": 3,
    "references": 4,
}
_ARABIC_YEAR = re.compile(r"(?<![0-9０-９])((?:1[89]|20|１[８９]|２０)[0-9０-９]{2})(年代|年)")
_KANJI_YEAR = re.compile(r"(?<![一二三四五六七八九〇零])([一二三四五六七八九〇零]{2,4})(年代|年)")
_TITLE_REFERENCE = re.compile(r"『([^』]{1,240})』")
_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")
_KANJI_DIGITS = {
    "〇": "0",
    "零": "0",
    "一": "1",
    "二": "2",
    "三": "3",
    "四": "4",
    "五": "5",
    "六": "6",
    "七": "7",
    "八": "8",
    "九": "9",
}
_CONNECTIVES = (
    "ただし",
    "しかし",
    "だが",
    "一方で",
    "一方",
    "したがって",
    "ゆえに",
    "なぜなら",
    "そのため",
    "ところが",
    "もっとも",
)
_ROLES = {
    "heading",
    "claim",
    "premise",
    "evidence",
    "example",
    "definition",
    "background",
    "qualification",
    "counterargument",
    "conclusion",
}
_ORDERING_POLICIES = {
    "chronological",
    "causal",
    "abstract_to_concrete",
    "claim_evidence",
    "comparison",
    "thematic",
    "problem_solution",
}
_BLOCK_RELATIONS = {
    "none",
    "continuation",
    "support",
    "elaboration",
    "example",
    "background",
    "cause",
    "result",
    "contrast",
    "qualification",
    "exception",
    "conclusion",
    "transition",
}
_ARGUMENT_RELATIONS = {
    "supports",
    "exemplifies",
    "illustrates",
    "background",
    "contrasts",
    "qualifies",
    "counterexample",
}
_REFERENCE_KINDS = {
    "work_example",
    "theory_reference",
    "bibliographic_reference",
    "comparison_target",
    "title_mention",
}
_TEMPORAL_KINDS = {
    "publication",
    "production",
    "reception",
    "historical_context",
    "retrospective_reference",
    "mixed",
}
_TEMPORAL_RELATIONS = {
    "none",
    "after",
    "before",
    "overlaps",
    "contains",
    "retrospective",
    "comparison",
    "background",
}
_CONNECTIVE_RELATIONS = {
    "continuation",
    "cause",
    "result",
    "contrast",
    "concession",
    "qualification",
    "exception",
    "transition",
}


@dataclass(frozen=True)
class CoherenceIssue:
    code: str
    message: str
    severity: Literal["error", "warning"] = "error"
    block_id: str | None = None
    section_id: str | None = None
    reference_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
            "block_id": self.block_id,
            "section_id": self.section_id,
            "reference_id": self.reference_id,
        }


@dataclass(frozen=True)
class CoherencePlanReport:
    issues: tuple[CoherenceIssue, ...]
    section_count: int
    planned_block_count: int
    example_link_count: int
    reference_classification_count: int
    temporal_mention_count: int
    temporal_scope_count: int
    temporal_constraint_count: int
    connective_relation_count: int
    merge_group_count: int

    @property
    def valid(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "issues": [issue.as_dict() for issue in self.issues],
            "section_count": self.section_count,
            "planned_block_count": self.planned_block_count,
            "example_link_count": self.example_link_count,
            "reference_classification_count": self.reference_classification_count,
            "temporal_mention_count": self.temporal_mention_count,
            "temporal_scope_count": self.temporal_scope_count,
            "temporal_constraint_count": self.temporal_constraint_count,
            "connective_relation_count": self.connective_relation_count,
            "merge_group_count": self.merge_group_count,
        }


class ManuscriptCoherenceService:
    """Build and enforce a section-level plan before prose rewriting starts."""

    @staticmethod
    def _year_number(raw: str) -> int | None:
        normalized = raw.translate(_FULLWIDTH_DIGITS)
        if normalized.isdigit():
            value = int(normalized)
        elif all(character in _KANJI_DIGITS for character in normalized):
            digits = "".join(_KANJI_DIGITS[character] for character in normalized)
            value = int(digits)
        else:
            return None
        if len(normalized) == 2:
            value += 2000 if value <= 29 else 1900
        if value < 1800 or value > 2099:
            return None
        return value

    @classmethod
    def _temporal_mentions(cls, text: str, block_id: str) -> list[dict[str, Any]]:
        matches: list[tuple[int, int, str, str, int]] = []
        for pattern in (_ARABIC_YEAR, _KANJI_YEAR):
            for match in pattern.finditer(text):
                year = cls._year_number(match.group(1))
                if year is None:
                    continue
                matches.append((match.start(), match.end(), match.group(0), match.group(2), year))
        matches.sort(key=lambda item: (item[0], item[1]))
        mentions: list[dict[str, Any]] = []
        occupied: set[tuple[int, int]] = set()
        for start, end, surface, suffix, year in matches:
            if (start, end) in occupied:
                continue
            occupied.add((start, end))
            qualifier = text[end : end + 2]
            start_year = year
            end_year = year
            granularity = "year"
            if suffix == "年代":
                start_year = year - (year % 10)
                end_year = start_year + 9
                granularity = "decade"
                if qualifier == "前半":
                    end_year = start_year + 4
                    surface += qualifier
                    end += 2
                    granularity = "half_decade"
                elif qualifier == "後半":
                    start_year += 5
                    surface += qualifier
                    end += 2
                    granularity = "half_decade"
            mentions.append(
                {
                    "mention_id": f"{block_id}:T{len(mentions) + 1:03d}",
                    "block_id": block_id,
                    "text": surface,
                    "start": start,
                    "end": end,
                    "start_year": start_year,
                    "end_year": end_year,
                    "granularity": granularity,
                }
            )
        return mentions

    @staticmethod
    def _reference_hint(text: str, start: int, end: int, *, section_title: str) -> str:
        if "参考文献" in section_title or "文献一覧" in section_title:
            return "bibliographic_reference"
        window = text[max(0, start - 80) : min(len(text), end + 80)]
        theory_cues = (
            "を参照",
            "を手がかり",
            "が論じ",
            "による",
            "の議論",
            "の概念",
            "の理論",
            "理論的",
            "援用",
        )
        if any(cue in window for cue in theory_cues):
            return "theory_reference"
        comparison_cues = ("比較", "対照", "想起", "類似", "影響関係")
        if any(cue in window for cue in comparison_cues):
            return "comparison_target"
        return "work_example"

    @classmethod
    def _reference_candidates(
        cls, text: str, block_id: str, *, section_title: str
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for match in _TITLE_REFERENCE.finditer(text):
            candidates.append(
                {
                    "reference_id": f"{block_id}:R{len(candidates) + 1:03d}",
                    "block_id": block_id,
                    "text": match.group(1).strip(),
                    "start": match.start(),
                    "end": match.end(),
                    "preliminary_kind": cls._reference_hint(
                        text,
                        match.start(),
                        match.end(),
                        section_title=section_title,
                    ),
                }
            )
        return candidates

    @classmethod
    def semantic_structure(cls, canonical_html: str) -> dict[str, Any]:
        root = ET.fromstring(canonical_html)
        sections: list[dict[str, Any]] = []
        blocks: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        for index, element in enumerate(item for item in root.iter() if item.tag in _EDITABLE_TAGS):
            text = "".join(element.itertext()).strip()
            if not text:
                continue
            heading = cls.heading(text, element.tag)
            if heading is not None:
                explicit_role = str(element.attrib.get("data-authoring-role") or "")
                document_role = (
                    "conclusion" if explicit_role == "conclusion"
                    else "argument" if explicit_role in {"introduction", "body"}
                    else cls._terminal_section_kind(heading[1]) or "argument"
                )
                current = {
                    "section_id": f"semantic:{element.attrib['id']}",
                    "heading_block_id": str(element.attrib["id"]),
                    "title": heading[1],
                    "level": heading[0],
                    "document_role": document_role,
                    "bibliography_processable": document_role == "references",
                    "block_ids": [],
                    "prose_block_ids": [],
                    "character_count": 0,
                }
                sections.append(current)
            elif current is None:
                current = {
                    "section_id": "semantic:preface",
                    "heading_block_id": None,
                    "title": "Preface",
                    "level": 1,
                    "document_role": "argument",
                    "bibliography_processable": False,
                    "block_ids": [],
                    "prose_block_ids": [],
                    "character_count": 0,
                }
                sections.append(current)
            assert current is not None
            block_id = str(element.attrib["id"])
            temporal_mentions = cls._temporal_mentions(text, block_id)
            reference_candidates = (
                []
                if heading is not None
                else cls._reference_candidates(
                    text,
                    block_id,
                    section_title=str(current["title"]),
                )
            )
            years = sorted(
                {
                    int(year)
                    for mention in temporal_mentions
                    for year in (mention["start_year"], mention["end_year"])
                }
            )
            connective = next((item for item in _CONNECTIVES if text.startswith(item)), None)
            block = {
                "id": block_id,
                "tag": element.tag,
                "text": text,
                "index": index,
                "section_id": current["section_id"],
                "document_role": current["document_role"],
                "rewritable_as_argument": current["document_role"] not in {"references", "notes"},
                "bibliography_processable": current["document_role"] == "references",
                "is_heading": heading is not None,
                "inline_protected": any(
                    child.tag in {"a", "cite", "q", "sup"} for child in element.iter()
                ),
                # Kept for legacy planners. Typed reference_candidates are authoritative
                # for V8+ plans and avoid treating every Japanese title as an example.
                "example_candidate": heading is None
                and any(
                    item["preliminary_kind"] == "work_example" for item in reference_candidates
                ),
                "reference_candidates": reference_candidates,
                "temporal_candidate": bool(temporal_mentions),
                "temporal_mentions": temporal_mentions,
                "years": years,
                "leading_connective": connective,
            }
            blocks.append(block)
            current["block_ids"].append(block_id)
            current["character_count"] += len(text)
            if heading is None:
                current["prose_block_ids"].append(block_id)
        for section in sections:
            section["plannable"] = bool(section["prose_block_ids"]) and (
                section["document_role"] not in {"references", "notes"}
            )
        return {
            "sections": sections,
            "blocks": blocks,
            "block_count": len(blocks),
            "plannable_section_count": sum(bool(section["plannable"]) for section in sections),
            "reference_section_count": sum(
                section["document_role"] == "references" for section in sections
            ),
            "markdown_heading_count": sum(
                bool(_MARKDOWN_HEADING.match(str(block["text"]))) for block in blocks
            ),
        }

    @classmethod
    def document_manifest(cls, canonical_html: str) -> dict[str, Any]:
        """Describe the complete document independently from its edit permissions.

        Coherence validators must be able to observe bibliography and other protected
        blocks even though those blocks are never valid prose-rewrite targets.  Keeping
        the two ID sets explicit prevents an edit-scope filter from silently becoming a
        document-visibility filter.
        """

        structure = cls.semantic_structure(canonical_html)
        source_blocks = [
            dict(block) for block in structure.get("blocks", []) if isinstance(block, dict)
        ]
        observable_block_ids = [
            str(block.get("id") or "")
            for block in source_blocks
            if str(block.get("id") or "").strip()
        ]
        editable_block_ids = [
            str(block.get("id") or "")
            for block in source_blocks
            if str(block.get("id") or "").strip()
            and bool(block.get("rewritable_as_argument", True))
        ]
        editable_set = set(editable_block_ids)
        sections: list[dict[str, Any]] = []
        for position, raw_section in enumerate(structure.get("sections", [])):
            if not isinstance(raw_section, dict):
                continue
            block_ids = [
                str(item) for item in raw_section.get("block_ids", []) if str(item).strip()
            ]
            role = str(raw_section.get("document_role") or "argument")
            sections.append(
                {
                    "position": position,
                    "section_id": str(raw_section.get("section_id") or ""),
                    "heading_block_id": raw_section.get("heading_block_id"),
                    "title": str(raw_section.get("title") or ""),
                    "level": int(raw_section.get("level") or 1),
                    "document_role": role,
                    "block_ids": block_ids,
                    "block_count": len(block_ids),
                    "editable_block_ids": [
                        block_id for block_id in block_ids if block_id in editable_set
                    ],
                    "observable": True,
                    "editable": bool(block_ids)
                    and all(block_id in editable_set for block_id in block_ids),
                    "bibliography_processable": bool(raw_section.get("bibliography_processable")),
                }
            )

        first_terminal = next(
            (
                index
                for index, section in enumerate(sections)
                if section["document_role"] != "argument"
            ),
            len(sections),
        )
        terminal_sections = [dict(item) for item in sections[first_terminal:]]
        reference_sections = [item for item in sections if item["document_role"] == "references"]
        reference_block_ids = [
            block_id for section in reference_sections for block_id in section["block_ids"]
        ]
        return {
            "schema_version": 1,
            "block_count": len(observable_block_ids),
            "observable_block_ids": observable_block_ids,
            "editable_block_ids": editable_block_ids,
            "read_only_block_ids": [
                block_id for block_id in observable_block_ids if block_id not in editable_set
            ],
            "section_count": len(sections),
            "sections": sections,
            "terminal_sections": terminal_sections,
            "terminal_section_ids": [str(item["section_id"]) for item in terminal_sections],
            "terminal_roles": [str(item["document_role"]) for item in terminal_sections],
            "last_section_id": (str(sections[-1]["section_id"]) if sections else None),
            "last_section_role": (str(sections[-1]["document_role"]) if sections else None),
            "reference_section_count": len(reference_sections),
            "reference_block_count": len(reference_block_ids),
            "reference_block_ids": reference_block_ids,
            "structural_signature": [
                {
                    "section_id": str(item["section_id"]),
                    "title": str(item["title"]),
                    "level": int(item["level"]),
                    "document_role": str(item["document_role"]),
                    "block_ids": list(item["block_ids"]),
                }
                for item in sections
            ],
        }

    @staticmethod
    def _terminal_roles_in_label(label: str) -> list[str]:
        matches: list[tuple[int, str]] = []
        patterns = (
            (r"主要参照文献|主要参考文献|参考文献|参照文献|引用文献|文献一覧", "references"),
            (
                r"適用範囲|検証課題|今後の課題|今後の研究|研究課題|"
                r"研究の限界|限界と展望",
                "limitations",
            ),
            (r"結論|終章|おわりに|まとめ|(?:^|[\s　+])結(?:$|[\s　+])", "conclusion"),
            (r"第[一二三四五六七八九十百0-9０-９]+節", "argument"),
        )
        for pattern, role in patterns:
            match = re.search(pattern, label)
            if match is not None:
                matches.append((match.start(), role))
        return [role for _, role in sorted(matches)]

    @classmethod
    def terminal_contract(
        cls,
        canonical_html: str,
        *,
        human_review: str = "",
        expected_reference_section_count: int | None = None,
    ) -> dict[str, Any]:
        """Validate terminal section facts without delegating them to an LLM.

        The application owns the conventional terminal order: concluding synthesis,
        limitations/applicability/future work, then bibliography.  Human prose may name
        those parts in another order while discussing a requested change; it is therefore
        used to determine required roles, but never to invert this structural invariant.
        """

        manifest = cls.document_manifest(canonical_html)
        sections = list(manifest["sections"])
        terminal_sections = list(manifest["terminal_sections"])
        terminal_roles = [str(item["document_role"]) for item in terminal_sections]
        issues: list[dict[str, Any]] = []

        def add(code: str, message: str, *, details: Any = None) -> None:
            issues.append({"code": code, "message": message, "details": details})

        seen_terminal = False
        last_rank = -1
        for section in sections:
            role = str(section["document_role"])
            if role != "argument":
                seen_terminal = True
            elif seen_terminal:
                add(
                    "terminal.argument_after_terminal",
                    "An argument section appears after the terminal section sequence.",
                    details=section["section_id"],
                )
                continue
            if not seen_terminal or role == "argument":
                continue
            rank = _TERMINAL_ROLE_ORDER.get(role, -1)
            if rank < last_rank:
                add(
                    "terminal.role_order_invalid",
                    "Terminal sections are not in conclusion, limitations, references order.",
                    details=terminal_roles,
                )
                break
            last_rank = rank

        if (
            int(manifest["reference_section_count"]) > 0
            and manifest["last_section_role"] != "references"
        ):
            add(
                "terminal.references_not_last",
                "The bibliography exists but is not the final document section.",
                details=manifest["last_section_id"],
            )
        if int(manifest["reference_section_count"]) > 1:
            add(
                "terminal.multiple_reference_sections",
                "The document contains more than one bibliography section.",
                details=manifest["reference_section_count"],
            )
        if expected_reference_section_count is not None and int(
            manifest["reference_section_count"]
        ) != int(expected_reference_section_count):
            add(
                "terminal.reference_section_count_changed",
                "The final document changed the source bibliography section count.",
                details={
                    "expected": int(expected_reference_section_count),
                    "actual": int(manifest["reference_section_count"]),
                },
            )

        conclusion_levels = [
            int(item["level"])
            for item in terminal_sections
            if item["document_role"] == "conclusion"
        ]
        limitation_levels = [
            int(item["level"])
            for item in terminal_sections
            if item["document_role"] == "limitations"
        ]
        if (
            conclusion_levels
            and limitation_levels
            and min(limitation_levels) <= max(conclusion_levels)
        ):
            add(
                "terminal.limitations_not_nested",
                "Applicability and verification tasks must be a subsection of the conclusion.",
                details={
                    "conclusion_levels": conclusion_levels,
                    "limitations_levels": limitation_levels,
                },
            )

        requested_roles: list[str] = []
        for line in str(human_review or "").splitlines():
            if not _TERMINAL_SEQUENCE_ARROW.search(line):
                continue
            for segment in _TERMINAL_SEQUENCE_ARROW.split(line):
                requested_roles.extend(cls._terminal_roles_in_label(segment))
        if not requested_roles and re.search(
            r"(?:主要参照文献|主要参考文献|参考文献|参照文献).{0,20}(?:最後|末尾|終端)",
            str(human_review or ""),
        ):
            requested_roles.append("references")
        requested_roles = list(dict.fromkeys(requested_roles))
        required_roles = sorted(
            requested_roles,
            key=lambda role: _TERMINAL_ROLE_ORDER.get(role, 99),
        )
        if required_roles:
            actual_roles = [str(item["document_role"]) for item in sections]
            actual_suffix = actual_roles[-len(required_roles) :]
            if actual_suffix != required_roles:
                add(
                    "terminal.human_required_sequence_missing",
                    "The normalized terminal section sequence does not contain "
                    "every role requested by the human review.",
                    details={
                        "requested_as_written": requested_roles,
                        "required_normalized": required_roles,
                        "actual_suffix": actual_suffix,
                    },
                )

        return {
            "valid": not issues,
            "issues": issues,
            "actual_roles": terminal_roles,
            "actual_section_ids": list(manifest["terminal_section_ids"]),
            "required_roles": required_roles,
            "human_requested_roles": requested_roles,
            "reference_section_count": manifest["reference_section_count"],
            "reference_block_count": manifest["reference_block_count"],
            "last_section_id": manifest["last_section_id"],
            "last_section_role": manifest["last_section_role"],
        }

    @staticmethod
    def heading(text: str, tag: str) -> tuple[int, str] | None:
        reference = _REFERENCE_SECTION_HEADING.match(text)
        if reference and tag not in _HEADING_TAGS:
            # Legacy imports sometimes collapse a bibliography heading and its
            # first entry into one paragraph, so a strict end anchor would lose
            # those headings.  A grammatical continuation, however, is normal
            # prose (for example, ``参照文献一覧の来歴については…``) and must
            # not split a contract-bound authored section in two.
            tail = text[reference.end() :].lstrip()
            if tail and _REFERENCE_PROSE_CONTINUATION.match(tail):
                reference = None
        if reference:
            markdown = _MARKDOWN_HEADING.match(text)
            return (
                len(markdown.group(1)) if markdown else 2,
                reference.group(1),
            )
        if tag in _HEADING_TAGS:
            return int(tag[1]), text
        markdown = _MARKDOWN_HEADING.match(text)
        if markdown:
            return len(markdown.group(1)), markdown.group(2).strip()
        if tag == "p" and (_NUMBERED_HEADING.match(text) or _NAMED_SECTION_HEADING.match(text)):
            return 2, text.strip()
        return None

    @staticmethod
    def _terminal_section_kind(title: str) -> str | None:
        if _REFERENCE_SECTION_HEADING.match(title):
            return "references"
        if _NOTE_SECTION_TITLE.match(title):
            return "notes"
        if _LIMITATIONS_SECTION_TITLE.search(title):
            return "limitations"
        if _CONCLUSION_SECTION_TITLE.match(title):
            return "conclusion"
        return None

    @classmethod
    def normalize_terminal_sections(
        cls, canonical_html: str
    ) -> tuple[str, list[dict[str, Any]], list[str]]:
        """Put conclusion limitations before a terminal bibliography, before locking.

        The operation is deliberately narrow: it runs only when every semantic section
        from the first detected terminal section to the document end is a conclusion,
        limitations/future-work section, notes, or bibliography. This prevents a
        conventional conclusion from being reordered around an unrelated appendix.
        """

        root = ET.fromstring(canonical_html)
        structure = cls.semantic_structure(canonical_html)
        sections = list(structure.get("sections") or [])
        typed = [
            (index, section, cls._terminal_section_kind(str(section.get("title") or "")))
            for index, section in enumerate(sections)
        ]
        terminal = [item for item in typed if item[2] is not None]
        kinds = {str(item[2]) for item in terminal}
        if not {"conclusion", "limitations", "references"}.issubset(kinds):
            return canonical_html, [], []
        first_index = min(item[0] for item in terminal)
        tail = typed[first_index:]
        if any(kind is None for _, _, kind in tail):
            return (
                canonical_html,
                [],
                [
                    "Terminal section normalization was skipped because an unrelated "
                    "section follows the conclusion."
                ],
            )
        desired_sections = [
            section
            for kind in ("conclusion", "limitations", "notes", "references")
            for _, section, section_kind in tail
            if section_kind == kind
        ]
        elements = {
            str(element.attrib.get("id")): element
            for element in root.iter()
            if element.attrib.get("id")
        }
        parents = {child: parent for parent in root.iter() for child in parent}
        grouped_nodes: list[tuple[dict[str, Any], list[ET.Element]]] = []
        for section in desired_sections:
            nodes = [elements.get(block_id) for block_id in cls._strings(section.get("block_ids"))]
            if not nodes or any(node is None for node in nodes):
                return canonical_html, [], ["Terminal section normalization found a missing block."]
            grouped_nodes.append((section, [node for node in nodes if node is not None]))
        all_nodes = [node for _, nodes in grouped_nodes for node in nodes]
        parent_set = {parents.get(node) for node in all_nodes}
        if len(parent_set) != 1 or None in parent_set:
            return (
                canonical_html,
                [],
                ["Terminal section normalization crossed HTML parents and was skipped."],
            )
        parent = next(iter(parent_set))
        assert parent is not None
        siblings = list(parent)
        positions = sorted(siblings.index(node) for node in all_nodes)
        if positions != list(range(positions[0], positions[0] + len(positions))):
            return (
                canonical_html,
                [],
                ["Terminal sections were not contiguous; their order was preserved."],
            )

        original_section_ids = [str(section.get("section_id") or "") for _, section, _ in tail]
        desired_section_ids = [str(section.get("section_id") or "") for section in desired_sections]
        edits: list[dict[str, Any]] = []
        if original_section_ids != desired_section_ids:
            for node in all_nodes:
                parent.remove(node)
            insertion = positions[0]
            for _, nodes in grouped_nodes:
                for node in nodes:
                    parent.insert(insertion, node)
                    insertion += 1
            edits.append(
                {
                    "operation": "normalize_terminal_section_order",
                    "from_section_ids": original_section_ids,
                    "to_section_ids": desired_section_ids,
                }
            )

        conclusion_levels = [
            int(section.get("level") or 2)
            for section in desired_sections
            if cls._terminal_section_kind(str(section.get("title") or "")) == "conclusion"
        ]
        conclusion_level = conclusion_levels[-1] if conclusion_levels else 2
        target_level = min(6, conclusion_level + 1)
        for section, _nodes in grouped_nodes:
            if (
                cls._terminal_section_kind(str(section.get("title") or "")) != "limitations"
                or int(section.get("level") or 2) > conclusion_level
            ):
                continue
            heading_id = str(section.get("heading_block_id") or "")
            heading = elements.get(heading_id)
            if heading is None:
                continue
            changed = False
            if heading.tag in _HEADING_TAGS:
                heading.tag = f"h{target_level}"
                changed = True
            elif heading.tag == "p" and heading.text and not list(heading):
                markdown = _MARKDOWN_HEADING.match(heading.text)
                if markdown:
                    heading.text = "#" * target_level + " " + markdown.group(2).strip()
                    changed = True
            if changed:
                edits.append(
                    {
                        "operation": "demote_terminal_subsection",
                        "block_id": heading_id,
                        "level": target_level,
                    }
                )

        return (
            ET.tostring(root, encoding="unicode", short_empty_elements=False),
            edits,
            [],
        )

    @classmethod
    def validate_plan(cls, structure: dict[str, Any], plan: dict[str, Any]) -> CoherencePlanReport:
        issues: list[CoherenceIssue] = []
        if not str(plan.get("document_thesis") or "").strip():
            issues.append(
                CoherenceIssue(
                    "plan.document_thesis_missing",
                    "The document-level plan requires an explicit thesis.",
                )
            )
        if str(plan.get("global_ordering_policy") or "") not in _ORDERING_POLICIES:
            issues.append(
                CoherenceIssue(
                    "plan.global_ordering_policy",
                    "The document-level ordering policy is missing or invalid.",
                )
            )
        source_sections = {
            str(section["section_id"]): section
            for section in structure.get("sections", [])
            if section.get("plannable")
        }
        source_blocks = {str(block["id"]): block for block in structure.get("blocks", [])}
        raw_sections = plan.get("sections")
        planned_sections = raw_sections if isinstance(raw_sections, list) else []
        plan_by_section: dict[str, dict[str, Any]] = {}
        all_ordered: list[str] = []
        for section_index, raw in enumerate(planned_sections):
            if not isinstance(raw, dict):
                continue
            section_id = str(raw.get("section_id") or "")
            if not section_id or section_id in plan_by_section:
                issues.append(
                    CoherenceIssue(
                        "plan.duplicate_section",
                        f"Structure plan repeats section {section_id or '(missing)'}.",
                        section_id=section_id or None,
                    )
                )
                continue
            plan_by_section[section_id] = raw
            order = cls._strings(raw.get("ordered_block_ids"))
            all_ordered.extend(order)
            source = source_sections.get(section_id)
            if source is None:
                issues.append(
                    CoherenceIssue(
                        "plan.unknown_section",
                        f"Structure plan refers to unknown section {section_id}.",
                        section_id=section_id,
                    )
                )
                continue
            expected = cls._strings(source.get("block_ids"))
            if len(order) != len(set(order)):
                issues.append(
                    CoherenceIssue(
                        "plan.duplicate_block",
                        "A section order contains a block more than once.",
                        section_id=section_id,
                    )
                )
            if set(order) != set(expected) or len(order) != len(expected):
                issues.append(
                    CoherenceIssue(
                        "plan.incomplete_section_order",
                        "Every source block must occur exactly once in its planned section.",
                        section_id=section_id,
                    )
                )
            heading_id = str(source.get("heading_block_id") or "")
            if heading_id and order and order[0] != heading_id:
                issues.append(
                    CoherenceIssue(
                        "plan.heading_moved",
                        "A section heading must remain the first block of its section.",
                        block_id=heading_id,
                        section_id=section_id,
                    )
                )
            policy = str(raw.get("ordering_policy") or "")
            if policy not in _ORDERING_POLICIES:
                issues.append(
                    CoherenceIssue(
                        "plan.ordering_policy",
                        f"Unknown section ordering policy: {policy or '(missing)'}.",
                        section_id=section_id,
                    )
                )
            claim_id = str(raw.get("central_claim_block_id") or "")
            if claim_id not in set(expected):
                issues.append(
                    CoherenceIssue(
                        "plan.central_claim",
                        "The central claim must point to a block in the same section.",
                        block_id=claim_id or None,
                        section_id=section_id,
                    )
                )
            elif claim_id not in set(cls._strings(source.get("prose_block_ids"))):
                issues.append(
                    CoherenceIssue(
                        "plan.central_claim_not_prose",
                        "The central claim must point to non-heading prose in the same section.",
                        block_id=claim_id or None,
                        section_id=section_id,
                    )
                )
            if not str(raw.get("central_claim") or "").strip():
                issues.append(
                    CoherenceIssue(
                        "plan.central_claim_missing",
                        "Each section requires an explicit central claim.",
                        section_id=section_id,
                    )
                )
            if not str(raw.get("title") or "").strip():
                issues.append(
                    CoherenceIssue(
                        "plan.section_title_missing",
                        "Each planned section requires a title.",
                        section_id=section_id,
                    )
                )
            if not isinstance(raw.get("transition_needed"), bool):
                issues.append(
                    CoherenceIssue(
                        "plan.transition_flag_invalid",
                        "The section transition flag must be a boolean.",
                        section_id=section_id,
                    )
                )
            if not str(raw.get("transition_reason") or "").strip():
                issues.append(
                    CoherenceIssue(
                        "plan.transition_reason_missing",
                        "Each section must explain its relation to the preceding section.",
                        section_id=section_id,
                    )
                )
            if (
                section_index > 0
                and raw.get("transition_needed")
                and not str(raw.get("transition_from_previous") or "").strip()
            ):
                issues.append(
                    CoherenceIssue(
                        "plan.transition_missing",
                        "A required section transition has no planned bridge.",
                        section_id=section_id,
                    )
                )
        for section_id in source_sections:
            if section_id not in plan_by_section:
                issues.append(
                    CoherenceIssue(
                        "plan.section_missing",
                        "A source section is missing from the document-level plan.",
                        section_id=section_id,
                    )
                )

        expected_planned_blocks = {
            block_id
            for section in source_sections.values()
            for block_id in cls._strings(section.get("block_ids"))
        }
        raw_roles = plan.get("block_roles")
        roles = raw_roles if isinstance(raw_roles, list) else []
        role_by_block: dict[str, dict[str, Any]] = {}
        for raw in roles:
            if not isinstance(raw, dict):
                continue
            block_id = str(raw.get("block_id") or "")
            if block_id in role_by_block:
                issues.append(
                    CoherenceIssue(
                        "argument.duplicate_role",
                        "A block has more than one rhetorical-role record.",
                        block_id=block_id or None,
                    )
                )
                continue
            role_by_block[block_id] = raw
            role = str(raw.get("role") or "")
            if block_id not in expected_planned_blocks:
                issues.append(
                    CoherenceIssue(
                        "argument.role_unknown_block",
                        "A rhetorical role refers to a block outside a plannable section.",
                        block_id=block_id or None,
                    )
                )
            if role not in _ROLES:
                issues.append(
                    CoherenceIssue(
                        "argument.role_unknown",
                        f"Unknown rhetorical role: {role or '(missing)'}.",
                        block_id=block_id or None,
                    )
                )
            if str(raw.get("relation_to_previous") or "") not in _BLOCK_RELATIONS:
                issues.append(
                    CoherenceIssue(
                        "argument.relation_unknown",
                        "A block has an invalid relation to the preceding block.",
                        block_id=block_id or None,
                    )
                )
            source = source_blocks.get(block_id)
            if source is not None and source["is_heading"] and role != "heading":
                issues.append(
                    CoherenceIssue(
                        "argument.heading_role",
                        "A semantic heading must retain the heading role.",
                        block_id=block_id,
                        section_id=str(source["section_id"]),
                    )
                )
            parent_id = str(raw.get("parent_claim_block_id") or "")
            if (
                role
                in {
                    "premise",
                    "evidence",
                    "example",
                    "qualification",
                    "counterargument",
                }
                and parent_id not in expected_planned_blocks
            ):
                issues.append(
                    CoherenceIssue(
                        "argument.parent_claim_missing",
                        "A dependent argumentative role requires an existing parent claim.",
                        block_id=block_id or None,
                    )
                )
        for block_id in expected_planned_blocks:
            if block_id not in role_by_block:
                issues.append(
                    CoherenceIssue(
                        "argument.role_missing",
                        "Every planned block requires one rhetorical role.",
                        block_id=block_id,
                        section_id=str(source_blocks[block_id]["section_id"]),
                    )
                )
        for section_id, section_plan in plan_by_section.items():
            claim_id = str(section_plan.get("central_claim_block_id") or "")
            claim_role = role_by_block.get(claim_id) or {}
            if str(claim_role.get("role") or "") != "claim":
                issues.append(
                    CoherenceIssue(
                        "argument.central_claim_role",
                        "The section's central-claim block must have the claim role.",
                        block_id=claim_id or None,
                        section_id=section_id,
                    )
                )
        for block_id, raw in role_by_block.items():
            role = str(raw.get("role") or "")
            if role not in {
                "premise",
                "evidence",
                "example",
                "qualification",
                "counterargument",
            }:
                continue
            parent_id = str(raw.get("parent_claim_block_id") or "")
            parent_role = role_by_block.get(parent_id) or {}
            if str(parent_role.get("role") or "") != "claim":
                issues.append(
                    CoherenceIssue(
                        "argument.parent_not_claim",
                        "A dependent block must attach to a block with the claim role.",
                        block_id=block_id,
                    )
                )
            elif source_blocks.get(block_id, {}).get("section_id") != source_blocks.get(
                parent_id, {}
            ).get("section_id"):
                issues.append(
                    CoherenceIssue(
                        "argument.parent_cross_section",
                        "A dependent block must attach to a claim in its own section.",
                        block_id=block_id,
                    )
                )

        source_references = {
            str(reference["reference_id"]): reference
            for block in source_blocks.values()
            for reference in block.get("reference_candidates", [])
            if isinstance(reference, dict) and reference.get("reference_id")
        }
        typed_reference_mode = "reference_classifications" in plan
        raw_classifications = plan.get("reference_classifications")
        classifications = raw_classifications if isinstance(raw_classifications, list) else []
        classification_by_reference: dict[str, dict[str, Any]] = {}
        if typed_reference_mode and not isinstance(raw_classifications, list):
            issues.append(
                CoherenceIssue(
                    "reference.classifications_invalid",
                    "Typed reference classifications must be an array.",
                )
            )
        for raw in classifications:
            if not isinstance(raw, dict):
                issues.append(
                    CoherenceIssue(
                        "reference.classification_invalid",
                        "A reference classification must be an object.",
                    )
                )
                continue
            reference_id = str(raw.get("reference_id") or "")
            block_id = str(raw.get("block_id") or "")
            if reference_id in classification_by_reference:
                issues.append(
                    CoherenceIssue(
                        "reference.duplicate_classification",
                        "A title reference was classified more than once.",
                        block_id=block_id or None,
                        reference_id=reference_id or None,
                    )
                )
                continue
            classification_by_reference[reference_id] = raw
            source_reference = source_references.get(reference_id)
            if source_reference is None:
                issues.append(
                    CoherenceIssue(
                        "reference.unknown_reference",
                        "A classification refers to an unknown title reference.",
                        block_id=block_id or None,
                        reference_id=reference_id or None,
                    )
                )
            elif block_id != str(source_reference.get("block_id") or ""):
                issues.append(
                    CoherenceIssue(
                        "reference.block_mismatch",
                        "A reference classification names the wrong source block.",
                        block_id=block_id or None,
                        reference_id=reference_id,
                    )
                )
            if str(raw.get("kind") or "") not in _REFERENCE_KINDS:
                issues.append(
                    CoherenceIssue(
                        "reference.kind_unknown",
                        "A title reference has an invalid semantic kind.",
                        block_id=block_id or None,
                        reference_id=reference_id or None,
                    )
                )
            if not str(raw.get("rationale") or "").strip():
                issues.append(
                    CoherenceIssue(
                        "reference.rationale_missing",
                        "A title-reference classification requires a rationale.",
                        block_id=block_id or None,
                        reference_id=reference_id or None,
                    )
                )
        if typed_reference_mode:
            for reference_id, source_reference in source_references.items():
                if reference_id not in classification_by_reference:
                    issues.append(
                        CoherenceIssue(
                            "reference.classification_missing",
                            "Every detected title reference requires one semantic kind.",
                            block_id=str(source_reference["block_id"]),
                            section_id=str(
                                source_blocks[str(source_reference["block_id"])]["section_id"]
                            ),
                            reference_id=reference_id,
                        )
                    )

        raw_links = plan.get("argument_links")
        links = raw_links if isinstance(raw_links, list) else []
        links_by_source: dict[str, list[dict[str, Any]]] = {}
        links_by_reference: dict[str, list[dict[str, Any]]] = {}
        seen_links: set[tuple[str, str]] = set()
        for raw in links:
            if not isinstance(raw, dict):
                continue
            source_id = str(raw.get("source_block_id") or "")
            target_id = str(raw.get("target_claim_block_id") or "")
            source_reference_id = str(raw.get("source_reference_id") or "")
            links_by_source.setdefault(source_id, []).append(raw)
            if source_reference_id:
                links_by_reference.setdefault(source_reference_id, []).append(raw)
            link_key = (source_reference_id or f"block:{source_id}", target_id)
            if link_key in seen_links:
                issues.append(
                    CoherenceIssue(
                        "argument.duplicate_link",
                        "An argument link pair was repeated.",
                        block_id=source_id or None,
                        reference_id=source_reference_id or None,
                    )
                )
            seen_links.add(link_key)
            if typed_reference_mode:
                if source_reference_id:
                    classification = classification_by_reference.get(source_reference_id)
                    source_reference = source_references.get(source_reference_id)
                    if classification is None or source_reference is None:
                        issues.append(
                            CoherenceIssue(
                                "argument.reference_unknown",
                                "An argument link refers to an unclassified title reference.",
                                block_id=source_id or None,
                                reference_id=source_reference_id,
                            )
                        )
                    elif source_id != str(source_reference.get("block_id") or ""):
                        issues.append(
                            CoherenceIssue(
                                "argument.reference_block_mismatch",
                                "An argument-link reference belongs to another block.",
                                block_id=source_id or None,
                                reference_id=source_reference_id,
                            )
                        )
                    elif str(classification.get("kind") or "") != "work_example":
                        issues.append(
                            CoherenceIssue(
                                "argument.reference_not_example",
                                "Only a work_example reference may source an example link.",
                                block_id=source_id or None,
                                reference_id=source_reference_id,
                            )
                        )
                target_claim_anchor = str(raw.get("target_claim_anchor") or "").strip()
                if not target_claim_anchor:
                    issues.append(
                        CoherenceIssue(
                            "argument.claim_anchor_missing",
                            "A typed argument link must identify the target claim text.",
                            block_id=source_id or None,
                            reference_id=source_reference_id or None,
                        )
                    )
                elif target_id in source_blocks:
                    target_section_id = str(source_blocks[target_id].get("section_id") or "")
                    target_section_plan = plan_by_section.get(target_section_id) or {}
                    planned_central_claim = str(
                        target_section_plan.get("central_claim") or ""
                    ).strip()
                    target_source_text = str(source_blocks[target_id].get("text") or "").strip()
                    anchored_to_plan = (
                        target_id == str(target_section_plan.get("central_claim_block_id") or "")
                        and target_claim_anchor == planned_central_claim
                    )
                    anchored_to_source = target_claim_anchor in target_source_text
                    if not anchored_to_plan and not anchored_to_source:
                        issues.append(
                            CoherenceIssue(
                                "argument.claim_anchor_unresolved",
                                "A claim anchor must be the section's planned central "
                                "claim or literal text from the target claim block.",
                                block_id=source_id or None,
                                reference_id=source_reference_id or None,
                            )
                        )
            if source_id == target_id and not source_reference_id:
                issues.append(
                    CoherenceIssue(
                        "argument.self_link",
                        "A block cannot serve as evidence for itself without a distinct "
                        "intra-block reference unit.",
                        block_id=source_id or None,
                    )
                )
            if str(raw.get("relation") or "") not in _ARGUMENT_RELATIONS:
                issues.append(
                    CoherenceIssue(
                        "argument.link_relation_unknown",
                        "An argument link has an invalid relation.",
                        block_id=source_id or None,
                        reference_id=source_reference_id or None,
                    )
                )
            if not isinstance(raw.get("evidence_required"), bool):
                issues.append(
                    CoherenceIssue(
                        "argument.evidence_flag_invalid",
                        "The argument-link evidence flag must be a boolean.",
                        block_id=source_id or None,
                        reference_id=source_reference_id or None,
                    )
                )
            elif raw.get("evidence_required") and not str(raw.get("research_query") or "").strip():
                issues.append(
                    CoherenceIssue(
                        "argument.required_query_missing",
                        "An evidence-required argument link requires a research query.",
                        block_id=source_id or None,
                        reference_id=source_reference_id or None,
                    )
                )
            if source_id not in source_blocks or target_id not in source_blocks:
                issues.append(
                    CoherenceIssue(
                        "argument.unknown_block",
                        "Argument links must use existing source and target block IDs.",
                        block_id=source_id or None,
                        reference_id=source_reference_id or None,
                    )
                )
            elif str((role_by_block.get(target_id) or {}).get("role") or "") != "claim":
                issues.append(
                    CoherenceIssue(
                        "argument.link_target_not_claim",
                        "An argument link must terminate at a block with the claim role.",
                        block_id=source_id or None,
                        reference_id=source_reference_id or None,
                    )
                )
            elif source_blocks[source_id]["section_id"] != source_blocks[target_id]["section_id"]:
                issues.append(
                    CoherenceIssue(
                        "argument.cross_section_example",
                        "Examples must attach to a claim in the same section.",
                        block_id=source_id,
                        section_id=str(source_blocks[source_id]["section_id"]),
                        reference_id=source_reference_id or None,
                    )
                )
            if not str(raw.get("observed_property") or "").strip():
                issues.append(
                    CoherenceIssue(
                        "argument.observation_missing",
                        "The observable feature of an example or evidence block is missing.",
                        block_id=source_id or None,
                        reference_id=source_reference_id or None,
                    )
                )
            if not str(raw.get("warrant") or "").strip():
                issues.append(
                    CoherenceIssue(
                        "argument.warrant_missing",
                        "The warrant connecting an example to its claim is missing.",
                        block_id=source_id or None,
                        reference_id=source_reference_id or None,
                    )
                )
        if typed_reference_mode:
            for reference_id, classification in classification_by_reference.items():
                if str(classification.get("kind") or "") != "work_example":
                    continue
                if reference_id in links_by_reference:
                    continue
                block_id = str(classification.get("block_id") or "")
                issues.append(
                    CoherenceIssue(
                        "argument.example_unlinked",
                        "A classified concrete-work example has no claim, observable "
                        "property, or warrant.",
                        block_id=block_id or None,
                        section_id=str(source_blocks.get(block_id, {}).get("section_id") or "")
                        or None,
                        reference_id=reference_id,
                    )
                )
            for block_id, role_record in role_by_block.items():
                if str(role_record.get("role") or "") != "example":
                    continue
                if block_id not in links_by_source:
                    issues.append(
                        CoherenceIssue(
                            "argument.example_role_unlinked",
                            "A block assigned the example role requires an argument link.",
                            block_id=block_id,
                            section_id=str(source_blocks[block_id]["section_id"]),
                        )
                    )
        else:
            for block in source_blocks.values():
                if block.get("example_candidate") and str(block["id"]) not in links_by_source:
                    issues.append(
                        CoherenceIssue(
                            "argument.example_unlinked",
                            "A concrete-work example has no claim, observable property, "
                            "or warrant.",
                            block_id=str(block["id"]),
                            section_id=str(block["section_id"]),
                        )
                    )

        typed_temporal_mode = "temporal_mentions" in plan or "block_temporal_scopes" in plan
        temporal_by_block: dict[str, dict[str, Any]] = {}
        temporal_mentions: list[dict[str, Any]] = []
        temporal_scopes: list[dict[str, Any]] = []
        legacy_temporal: list[dict[str, Any]] = []
        if typed_temporal_mode:
            source_mentions = {
                str(mention["mention_id"]): mention
                for block in source_blocks.values()
                for mention in block.get("temporal_mentions", [])
                if isinstance(mention, dict) and mention.get("mention_id")
            }
            raw_mentions = plan.get("temporal_mentions")
            temporal_mentions = raw_mentions if isinstance(raw_mentions, list) else []
            if not isinstance(raw_mentions, list):
                issues.append(
                    CoherenceIssue(
                        "temporal.mentions_invalid",
                        "Temporal mention annotations must be an array.",
                    )
                )
            mention_by_id: dict[str, dict[str, Any]] = {}
            for item in temporal_mentions:
                if not isinstance(item, dict):
                    issues.append(
                        CoherenceIssue(
                            "temporal.mention_invalid",
                            "A temporal mention annotation must be an object.",
                        )
                    )
                    continue
                mention_id = str(item.get("mention_id") or "")
                block_id = str(item.get("block_id") or "")
                if mention_id in mention_by_id:
                    issues.append(
                        CoherenceIssue(
                            "temporal.duplicate_mention",
                            "An extracted temporal mention was annotated more than once.",
                            block_id=block_id or None,
                            reference_id=mention_id or None,
                        )
                    )
                    continue
                mention_by_id[mention_id] = item
                source_mention = source_mentions.get(mention_id)
                if source_mention is None:
                    issues.append(
                        CoherenceIssue(
                            "temporal.unknown_mention",
                            "A temporal annotation refers to an unknown source mention.",
                            block_id=block_id or None,
                            reference_id=mention_id or None,
                        )
                    )
                elif block_id != str(source_mention.get("block_id") or ""):
                    issues.append(
                        CoherenceIssue(
                            "temporal.mention_block_mismatch",
                            "A temporal mention annotation names the wrong block.",
                            block_id=block_id or None,
                            reference_id=mention_id,
                        )
                    )
                if str(item.get("temporal_kind") or "") not in _TEMPORAL_KINDS:
                    issues.append(
                        CoherenceIssue(
                            "temporal.mention_kind_unknown",
                            "A temporal mention has an invalid semantic kind.",
                            block_id=block_id or None,
                            reference_id=mention_id or None,
                        )
                    )
                if source_mention is not None:
                    for field in ("start_year", "end_year"):
                        if field in item and item.get(field) != source_mention.get(field):
                            issues.append(
                                CoherenceIssue(
                                    "temporal.mention_range_mismatch",
                                    "The model may classify but may not alter an extracted "
                                    "year range.",
                                    block_id=block_id or None,
                                    reference_id=mention_id,
                                )
                            )
            for mention_id, source_mention in source_mentions.items():
                if mention_id not in mention_by_id:
                    issues.append(
                        CoherenceIssue(
                            "temporal.mention_unplanned",
                            "Every extracted temporal mention requires one annotation.",
                            block_id=str(source_mention["block_id"]),
                            section_id=str(
                                source_blocks[str(source_mention["block_id"])]["section_id"]
                            ),
                            reference_id=mention_id,
                        )
                    )

            raw_scopes = plan.get("block_temporal_scopes")
            temporal_scopes = raw_scopes if isinstance(raw_scopes, list) else []
            if not isinstance(raw_scopes, list):
                issues.append(
                    CoherenceIssue(
                        "temporal.scopes_invalid",
                        "Block temporal scopes must be an array.",
                    )
                )
            for item in temporal_scopes:
                if not isinstance(item, dict):
                    issues.append(
                        CoherenceIssue(
                            "temporal.scope_invalid_record",
                            "A block temporal scope must be an object.",
                        )
                    )
                    continue
                block_id = str(item.get("block_id") or "")
                if not block_id or block_id not in source_blocks:
                    issues.append(
                        CoherenceIssue(
                            "temporal.unknown_block",
                            "A block temporal scope refers to an unknown block.",
                            block_id=block_id or None,
                        )
                    )
                    continue
                if block_id in temporal_by_block:
                    issues.append(
                        CoherenceIssue(
                            "temporal.duplicate_scope",
                            "A block has more than one aggregate temporal scope.",
                            block_id=block_id,
                        )
                    )
                    continue
                temporal_by_block[block_id] = item
                if str(item.get("temporal_kind") or "") not in _TEMPORAL_KINDS:
                    issues.append(
                        CoherenceIssue(
                            "temporal.kind_unknown",
                            "A block temporal scope has an invalid kind.",
                            block_id=block_id,
                        )
                    )
                if str(item.get("relation_to_previous") or "") not in _TEMPORAL_RELATIONS:
                    issues.append(
                        CoherenceIssue(
                            "temporal.relation_unknown",
                            "A block temporal scope has an invalid prior relation.",
                            block_id=block_id,
                        )
                    )
                for field in ("start_year", "end_year"):
                    value = item.get(field)
                    if value is not None and (
                        not isinstance(value, int) or isinstance(value, bool)
                    ):
                        issues.append(
                            CoherenceIssue(
                                "temporal.year_invalid",
                                "Temporal interval endpoints must be integer years or null.",
                                block_id=block_id,
                            )
                        )
            for block in source_blocks.values():
                source_block_mentions = list(block.get("temporal_mentions", []))
                if not source_block_mentions:
                    continue
                block_id = str(block["id"])
                item = temporal_by_block.get(block_id)
                if item is None:
                    issues.append(
                        CoherenceIssue(
                            "temporal.scope_missing",
                            "A block with temporal mentions requires one aggregate scope.",
                            block_id=block_id,
                            section_id=str(block["section_id"]),
                        )
                    )
                    continue
                start = item.get("start_year")
                end = item.get("end_year")
                minimum = min(int(x["start_year"]) for x in source_block_mentions)
                maximum = max(int(x["end_year"]) for x in source_block_mentions)
                if (
                    not isinstance(start, int)
                    or isinstance(start, bool)
                    or not isinstance(end, int)
                    or isinstance(end, bool)
                    or int(start) > minimum
                    or int(end) < maximum
                ):
                    issues.append(
                        CoherenceIssue(
                            "temporal.scope_incomplete",
                            "The aggregate scope must cover every temporal mention in the "
                            "paragraph.",
                            block_id=block_id,
                            section_id=str(block["section_id"]),
                        )
                    )
        else:
            raw_temporal = plan.get("temporal_constraints")
            legacy_temporal = raw_temporal if isinstance(raw_temporal, list) else []
            for item in legacy_temporal:
                if not isinstance(item, dict):
                    issues.append(
                        CoherenceIssue(
                            "temporal.invalid_record",
                            "A temporal constraint must be an object.",
                        )
                    )
                    continue
                block_id = str(item.get("block_id") or "")
                if not block_id or block_id not in source_blocks:
                    issues.append(
                        CoherenceIssue(
                            "temporal.unknown_block",
                            "A temporal constraint refers to an unknown block.",
                            block_id=block_id or None,
                        )
                    )
                    continue
                if block_id in temporal_by_block:
                    issues.append(
                        CoherenceIssue(
                            "temporal.duplicate_block",
                            "A legacy block has more than one temporal constraint.",
                            block_id=block_id,
                        )
                    )
                    continue
                temporal_by_block[block_id] = item
                if str(item.get("temporal_kind") or "") not in _TEMPORAL_KINDS:
                    issues.append(
                        CoherenceIssue(
                            "temporal.kind_unknown",
                            "A temporal constraint has an invalid kind.",
                            block_id=block_id,
                        )
                    )
                if str(item.get("relation_to_previous") or "") not in _TEMPORAL_RELATIONS:
                    issues.append(
                        CoherenceIssue(
                            "temporal.relation_unknown",
                            "A temporal constraint has an invalid prior relation.",
                            block_id=block_id,
                        )
                    )
                for field in ("start_year", "end_year"):
                    value = item.get(field)
                    if value is not None and (
                        not isinstance(value, int) or isinstance(value, bool)
                    ):
                        issues.append(
                            CoherenceIssue(
                                "temporal.year_invalid",
                                "Temporal interval endpoints must be integer years or null.",
                                block_id=block_id,
                            )
                        )
            for block in source_blocks.values():
                if block.get("temporal_candidate") and str(block["id"]) not in temporal_by_block:
                    issues.append(
                        CoherenceIssue(
                            "temporal.mention_unplanned",
                            "A paragraph containing an explicit year is absent from the "
                            "legacy timeline.",
                            block_id=str(block["id"]),
                            section_id=str(block["section_id"]),
                        )
                    )
                elif block.get("temporal_candidate"):
                    item = temporal_by_block[str(block["id"])]
                    start = item.get("start_year")
                    end = item.get("end_year")
                    years = [int(year) for year in block.get("years", [])]
                    if (
                        not isinstance(start, int)
                        or isinstance(start, bool)
                        or not isinstance(end, int)
                        or isinstance(end, bool)
                        or int(start) > min(years)
                        or int(end) < max(years)
                    ):
                        issues.append(
                            CoherenceIssue(
                                "temporal.scope_incomplete",
                                "The planned interval must cover every explicit year in the "
                                "paragraph.",
                                block_id=str(block["id"]),
                                section_id=str(block["section_id"]),
                            )
                        )
        for section_id, section_plan in plan_by_section.items():
            previous_start: int | None = None
            for block_id in cls._strings(section_plan.get("ordered_block_ids")):
                item = temporal_by_block.get(block_id)
                if item is None:
                    continue
                start = item.get("start_year")
                end = item.get("end_year")
                valid_start = isinstance(start, int) and not isinstance(start, bool)
                valid_end = isinstance(end, int) and not isinstance(end, bool)
                if valid_start and valid_end and int(end) < int(start):
                    issues.append(
                        CoherenceIssue(
                            "temporal.invalid_interval",
                            "A temporal interval ends before it starts.",
                            block_id=block_id,
                            section_id=section_id,
                        )
                    )
                if not valid_start:
                    continue
                start_int = int(start)
                relation = str(item.get("relation_to_previous") or "none")
                if (
                    previous_start is not None
                    and start_int < previous_start
                    and relation not in {"retrospective", "comparison", "background"}
                ):
                    issues.append(
                        CoherenceIssue(
                            "temporal.unexplained_regression",
                            "The planned order moves backwards in time without an explicit "
                            "rhetorical reason.",
                            block_id=block_id,
                            section_id=section_id,
                        )
                    )
                previous_start = start_int

        raw_connectives = plan.get("connective_relations")
        connective_relations = raw_connectives if isinstance(raw_connectives, list) else []
        connective_by_block: dict[str, dict[str, Any]] = {}
        for item in connective_relations:
            if not isinstance(item, dict):
                issues.append(
                    CoherenceIssue(
                        "discourse.invalid_connective_record",
                        "A connective relation must be an object.",
                    )
                )
                continue
            block_id = str(item.get("block_id") or "")
            if not block_id or block_id not in source_blocks:
                issues.append(
                    CoherenceIssue(
                        "discourse.connective_unknown_block",
                        "A connective relation refers to an unknown block.",
                        block_id=block_id or None,
                    )
                )
                continue
            if block_id in connective_by_block:
                issues.append(
                    CoherenceIssue(
                        "discourse.duplicate_connective",
                        "A block has more than one connective relation.",
                        block_id=block_id,
                    )
                )
                continue
            connective_by_block[block_id] = item
            if str(item.get("relation") or "") not in _CONNECTIVE_RELATIONS:
                issues.append(
                    CoherenceIssue(
                        "discourse.relation_unknown",
                        "A connective has an invalid discourse relation.",
                        block_id=block_id,
                    )
                )
        for block in source_blocks.values():
            connective = block.get("leading_connective")
            if not connective:
                continue
            item = connective_by_block.get(str(block["id"]))
            if item is None:
                issues.append(
                    CoherenceIssue(
                        "discourse.connective_unresolved",
                        f"Leading connective {connective} has no discourse relation.",
                        block_id=str(block["id"]),
                        section_id=str(block["section_id"]),
                    )
                )
                continue
            if str(item.get("connective") or "") != str(connective):
                issues.append(
                    CoherenceIssue(
                        "discourse.connective_mismatch",
                        "The planned connective does not match the detected source marker.",
                        block_id=str(block["id"]),
                        section_id=str(block["section_id"]),
                    )
                )
            target_id = str(item.get("target_block_id") or "")
            if target_id not in source_blocks:
                issues.append(
                    CoherenceIssue(
                        "discourse.target_missing",
                        "A discourse connective has no valid attachment target.",
                        block_id=str(block["id"]),
                    )
                )
            else:
                section_plan = plan_by_section.get(str(block["section_id"])) or {}
                order = cls._strings(section_plan.get("ordered_block_ids"))
                if (
                    target_id not in order
                    or str(block["id"]) not in order
                    or order.index(target_id) >= order.index(str(block["id"]))
                ):
                    issues.append(
                        CoherenceIssue(
                            "discourse.target_not_prior",
                            "A leading connective must attach to a prior block in the same "
                            "section.",
                            block_id=str(block["id"]),
                            section_id=str(block["section_id"]),
                        )
                    )
            if connective == "ただし" and str(item.get("relation") or "") not in {
                "qualification",
                "exception",
            }:
                issues.append(
                    CoherenceIssue(
                        "discourse.tadashi_mismatch",
                        "ただし may only introduce a recoverable qualification or exception.",
                        block_id=str(block["id"]),
                        section_id=str(block["section_id"]),
                    )
                )
            if not str(item.get("explanation") or "").strip():
                issues.append(
                    CoherenceIssue(
                        "discourse.explanation_missing",
                        "A connective relation requires an explicit attachment explanation.",
                        block_id=str(block["id"]),
                    )
                )

        raw_groups = plan.get("merge_groups")
        merge_groups = raw_groups if isinstance(raw_groups, list) else []
        merged_block_ids: set[str] = set()
        for group in merge_groups:
            if not isinstance(group, dict):
                issues.append(
                    CoherenceIssue(
                        "merge.invalid_record",
                        "A merge group must be an object.",
                    )
                )
                continue
            block_ids = cls._strings(group.get("block_ids"))
            if len(block_ids) != 2 or len(set(block_ids)) != 2:
                issues.append(
                    CoherenceIssue(
                        "merge.pair_required",
                        "Every approved merge must name exactly two distinct blocks.",
                    )
                )
                continue
            left, right = block_ids
            overlap = merged_block_ids.intersection(block_ids)
            if overlap:
                issues.append(
                    CoherenceIssue(
                        "merge.overlapping_groups",
                        "A block cannot participate in more than one merge group.",
                        block_id=sorted(overlap)[0],
                    )
                )
            merged_block_ids.update(block_ids)
            if left not in source_blocks or right not in source_blocks:
                issues.append(
                    CoherenceIssue(
                        "merge.unknown_block",
                        "A merge group refers to an unknown block.",
                        block_id=left,
                    )
                )
                continue
            section_id = str(source_blocks[left]["section_id"])
            section_plan = plan_by_section.get(section_id) or {}
            order = cls._strings(section_plan.get("ordered_block_ids"))
            if (
                source_blocks[left]["section_id"] != source_blocks[right]["section_id"]
                or left not in order
                or order.index(left) + 1 >= len(order)
                or order[order.index(left) + 1] != right
            ):
                issues.append(
                    CoherenceIssue(
                        "merge.not_adjacent",
                        "Approved merges must remain adjacent within one planned section.",
                        block_id=left,
                        section_id=section_id,
                    )
                )
            if source_blocks[left]["is_heading"] or source_blocks[right]["is_heading"]:
                issues.append(
                    CoherenceIssue(
                        "merge.heading",
                        "Semantic section headings cannot be merged into prose.",
                        block_id=left,
                        section_id=section_id,
                    )
                )
            if group.get("preserves_temporal_scope") is not True:
                issues.append(
                    CoherenceIssue(
                        "merge.temporal_scope_unverified",
                        "A merge must explicitly preserve the temporal scope of both paragraphs.",
                        block_id=left,
                        section_id=section_id,
                    )
                )
            if not str(group.get("reason") or "").strip():
                issues.append(
                    CoherenceIssue(
                        "merge.reason_missing",
                        "An approved merge requires an explicit semantic rationale.",
                        block_id=left,
                        section_id=section_id,
                    )
                )
            if not str(group.get("shared_parent_claim_id") or "").strip():
                issues.append(
                    CoherenceIssue(
                        "merge.parent_claim_missing",
                        "A merge requires a shared parent claim.",
                        block_id=left,
                        section_id=section_id,
                    )
                )
            else:
                shared_parent = str(group["shared_parent_claim_id"])
                left_parent = str(
                    (role_by_block.get(left) or {}).get("parent_claim_block_id") or left
                )
                right_parent = str(
                    (role_by_block.get(right) or {}).get("parent_claim_block_id") or right
                )
                if (
                    shared_parent not in {left, right, left_parent}
                    or shared_parent not in {left, right, right_parent}
                    or str((role_by_block.get(shared_parent) or {}).get("role") or "") != "claim"
                ):
                    issues.append(
                        CoherenceIssue(
                            "merge.parent_claim_mismatch",
                            "Both merged paragraphs must share the named claim parent.",
                            block_id=left,
                            section_id=section_id,
                        )
                    )

        return CoherencePlanReport(
            issues=tuple(issues),
            section_count=len(plan_by_section),
            planned_block_count=len(all_ordered),
            example_link_count=len(links),
            reference_classification_count=len(classifications),
            temporal_mention_count=len(temporal_mentions),
            temporal_scope_count=(
                len(temporal_scopes) if typed_temporal_mode else len(legacy_temporal)
            ),
            temporal_constraint_count=(
                len(temporal_scopes) if typed_temporal_mode else len(legacy_temporal)
            ),
            connective_relation_count=len(connective_relations),
            merge_group_count=len(merge_groups),
        )

    @classmethod
    def normalize_plan(
        cls,
        structure: dict[str, Any],
        plan: dict[str, Any],
        *,
        repair_connective_targets: bool = True,
        repair_argument_roles: bool = True,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Repair referential mechanics without inventing claims, warrants, or prose."""
        normalized = copy.deepcopy(plan)
        actions: list[dict[str, Any]] = []
        source_blocks = {str(block["id"]): block for block in structure.get("blocks", [])}
        source_sections = {
            str(section["section_id"]): section
            for section in structure.get("sections", [])
            if section.get("plannable")
        }
        raw_sections = normalized.get("sections")
        sections = raw_sections if isinstance(raw_sections, list) else []
        all_source_sections = list(structure.get("sections") or [])
        terminal_source = [
            (
                index,
                section,
                cls._terminal_section_kind(str(section.get("title") or "")),
            )
            for index, section in enumerate(all_source_sections)
        ]
        detected_terminal = [item for item in terminal_source if item[2] is not None]
        if detected_terminal:
            terminal_start = min(item[0] for item in detected_terminal)
            terminal_tail = terminal_source[terminal_start:]
            terminal_kinds = {str(item[2]) for item in terminal_tail}
            if all(kind is not None for _, _, kind in terminal_tail) and {
                "conclusion",
                "limitations",
                "references",
            }.issubset(terminal_kinds):
                section_kind = {
                    str(section.get("section_id") or ""): str(kind)
                    for _, section, kind in terminal_tail
                    if kind is not None
                }
                desired_terminal_ids = [
                    section_id
                    for kind in ("conclusion", "limitations")
                    for _, source_section, source_kind in terminal_tail
                    if source_kind == kind
                    and (section_id := str(source_section.get("section_id") or ""))
                ]
                plan_by_id = {
                    str(section.get("section_id") or ""): section
                    for section in sections
                    if isinstance(section, dict) and str(section.get("section_id") or "")
                }
                ordered_sections = [
                    section
                    for section in sections
                    if isinstance(section, dict)
                    and str(section.get("section_id") or "") not in section_kind
                ]
                ordered_sections.extend(
                    plan_by_id[section_id]
                    for section_id in desired_terminal_ids
                    if section_id in plan_by_id
                )
                before = [
                    str(section.get("section_id") or "")
                    for section in sections
                    if isinstance(section, dict)
                ]
                after = [str(section.get("section_id") or "") for section in ordered_sections]
                if before != after and len(before) == len(after):
                    sections = ordered_sections
                    normalized["sections"] = sections
                    actions.append(
                        {
                            "action": "normalize_terminal_section_order",
                            "from_section_ids": before,
                            "to_section_ids": after,
                        }
                    )
        plan_by_section = {
            str(section.get("section_id")): section
            for section in sections
            if isinstance(section, dict) and section.get("section_id")
        }
        central_by_section = {
            section_id: str(section.get("central_claim_block_id") or "")
            for section_id, section in plan_by_section.items()
        }

        raw_roles = normalized.get("block_roles")
        roles = raw_roles if isinstance(raw_roles, list) else []
        role_by_block: dict[str, dict[str, Any]] = {}
        for role in roles:
            if not isinstance(role, dict):
                continue
            block_id = str(role.get("block_id") or "")
            if block_id in source_blocks and block_id not in role_by_block:
                role_by_block[block_id] = role
        for section_id, section in source_sections.items():
            central_id = central_by_section.get(section_id, "")
            for block_id in cls._strings(section.get("block_ids")):
                if block_id in role_by_block:
                    continue
                block = source_blocks[block_id]
                role = {
                    "block_id": block_id,
                    "role": (
                        "heading"
                        if block.get("is_heading")
                        else "claim"
                        if block_id == central_id
                        else "premise"
                    ),
                    "parent_claim_block_id": (
                        None
                        if block.get("is_heading") or block_id == central_id
                        else central_id or None
                    ),
                    "relation_to_previous": ("none" if block.get("is_heading") else "support"),
                }
                roles.append(role)
                role_by_block[block_id] = role
                actions.append({"action": "add_missing_role", "block_id": block_id})
        for central_id in central_by_section.values() if repair_argument_roles else ():
            role = role_by_block.get(central_id)
            if role is not None and str(role.get("role") or "") != "claim":
                role["role"] = "claim"
                role["parent_claim_block_id"] = None
                actions.append({"action": "set_central_claim_role", "block_id": central_id})
        for block_id, role in role_by_block.items() if repair_argument_roles else ():
            if str(role.get("role") or "") not in {
                "premise",
                "evidence",
                "example",
                "qualification",
                "counterargument",
            }:
                continue
            section_id = str(source_blocks[block_id]["section_id"])
            central_id = central_by_section.get(section_id, "")
            parent_id = str(role.get("parent_claim_block_id") or "")
            parent_role = role_by_block.get(parent_id) or {}
            if (
                str(parent_role.get("role") or "") != "claim"
                or source_blocks.get(parent_id, {}).get("section_id") != section_id
            ) and central_id:
                role["parent_claim_block_id"] = central_id
                actions.append(
                    {
                        "action": "reattach_role_to_central_claim",
                        "block_id": block_id,
                        "target_block_id": central_id,
                    }
                )
        normalized["block_roles"] = list(role_by_block.values())

        for link in normalized.get("argument_links", []):
            if not isinstance(link, dict):
                continue
            source_id = str(link.get("source_block_id") or "")
            source = source_blocks.get(source_id)
            if source is None:
                continue
            section_id = str(source["section_id"])
            target_id = str(link.get("target_claim_block_id") or "")
            target_role = role_by_block.get(target_id) or {}
            if (
                str(target_role.get("role") or "") != "claim"
                or source_blocks.get(target_id, {}).get("section_id") != section_id
            ):
                central_id = central_by_section.get(section_id, "")
                source_reference_id = str(link.get("source_reference_id") or "")
                if central_id and (central_id != source_id or source_reference_id):
                    link["target_claim_block_id"] = central_id
                    actions.append(
                        {
                            "action": "reattach_argument_to_central_claim",
                            "block_id": source_id,
                            "target_block_id": central_id,
                        }
                    )

        typed_temporal_mode = (
            "temporal_mentions" in normalized or "block_temporal_scopes" in normalized
        )
        if typed_temporal_mode:
            source_mentions = {
                str(mention["mention_id"]): mention
                for block in source_blocks.values()
                for mention in block.get("temporal_mentions", [])
                if isinstance(mention, dict) and mention.get("mention_id")
            }
            hydrated_count = 0
            raw_temporal_mentions = normalized.get("temporal_mentions")
            normalized_temporal_mentions = (
                raw_temporal_mentions if isinstance(raw_temporal_mentions, list) else []
            )
            for item in normalized_temporal_mentions:
                if not isinstance(item, dict):
                    continue
                source_mention = source_mentions.get(str(item.get("mention_id") or ""))
                if source_mention is None or str(item.get("block_id") or "") != str(
                    source_mention.get("block_id") or ""
                ):
                    continue
                changed = False
                for field in (
                    "text",
                    "start",
                    "end",
                    "start_year",
                    "end_year",
                    "granularity",
                ):
                    if item.get(field) != source_mention.get(field):
                        item[field] = source_mention.get(field)
                        changed = True
                if changed:
                    hydrated_count += 1
            if hydrated_count:
                actions.append(
                    {
                        "action": "hydrate_temporal_mentions",
                        "mention_count": hydrated_count,
                    }
                )
            raw_temporal_records = normalized.get("block_temporal_scopes")
        else:
            raw_temporal_records = normalized.get("temporal_constraints")
        temporal_records = raw_temporal_records if isinstance(raw_temporal_records, list) else []
        temporal_by_block: dict[str, dict[str, Any]] = {}
        for item in temporal_records:
            if not isinstance(item, dict) or not item.get("block_id"):
                continue
            # Validator and normalizer deliberately use the same first record.
            temporal_by_block.setdefault(str(item["block_id"]), item)
        for block_id, block in source_blocks.items():
            if typed_temporal_mode:
                source_block_mentions = list(block.get("temporal_mentions", []))
                years = [
                    int(year)
                    for mention in source_block_mentions
                    for year in (mention["start_year"], mention["end_year"])
                ]
            else:
                years = [int(year) for year in block.get("years", [])]
            item = temporal_by_block.get(block_id)
            if not years or item is None:
                continue
            start = item.get("start_year")
            end = item.get("end_year")
            if start is None or end is None or int(start) > min(years) or int(end) < max(years):
                item["start_year"] = min(years)
                item["end_year"] = max(years)
                actions.append({"action": "cover_explicit_years", "block_id": block_id})
        for section in plan_by_section.values():
            previous_start: int | None = None
            for block_id in cls._strings(section.get("ordered_block_ids")):
                item = temporal_by_block.get(block_id)
                if item is None or item.get("start_year") is None:
                    continue
                start = int(item["start_year"])
                if (
                    previous_start is not None
                    and start < previous_start
                    and str(item.get("relation_to_previous") or "")
                    not in {"retrospective", "comparison", "background"}
                ):
                    item["relation_to_previous"] = "retrospective"
                    actions.append({"action": "label_temporal_regression", "block_id": block_id})
                previous_start = start

        unique_connectives: list[dict[str, Any]] = []
        seen_connective_blocks: set[str] = set()
        for item in normalized.get("connective_relations", []):
            if not isinstance(item, dict):
                continue
            block_id = str(item.get("block_id") or "")
            if block_id and block_id in seen_connective_blocks:
                actions.append(
                    {
                        "action": "drop_duplicate_connective",
                        "block_id": block_id,
                    }
                )
                continue
            if block_id:
                seen_connective_blocks.add(block_id)
            unique_connectives.append(item)
            block = source_blocks.get(block_id)
            if block is None:
                continue
            section_id = str(block["section_id"])
            order = cls._strings((plan_by_section.get(section_id) or {}).get("ordered_block_ids"))
            target_id = str(item.get("target_block_id") or "")
            if block_id in order:
                position = order.index(block_id)
                if repair_connective_targets and target_id not in order[:position] and position:
                    item["target_block_id"] = order[position - 1]
                    actions.append({"action": "reattach_connective", "block_id": block_id})
            if block.get("leading_connective") == "ただし" and str(
                item.get("relation") or ""
            ) not in {"qualification", "exception"}:
                item["relation"] = "qualification"
                actions.append({"action": "classify_tadashi", "block_id": block_id})
        normalized["connective_relations"] = unique_connectives

        safe_groups: list[dict[str, Any]] = []
        for group in normalized.get("merge_groups", []):
            if not isinstance(group, dict):
                continue
            block_ids = cls._strings(group.get("block_ids"))
            if len(block_ids) != 2 or any(item not in role_by_block for item in block_ids):
                safe_groups.append(group)
                continue
            parents = {
                str(role_by_block[item].get("parent_claim_block_id") or item) for item in block_ids
            }
            parent_id = next(iter(parents)) if len(parents) == 1 else ""
            if parent_id and str((role_by_block.get(parent_id) or {}).get("role") or "") == "claim":
                group["shared_parent_claim_id"] = parent_id
                safe_groups.append(group)
            else:
                actions.append({"action": "drop_unsafe_merge", "block_ids": block_ids})
        normalized["merge_groups"] = safe_groups
        normalized["normalization_actions"] = actions
        return normalized, actions

    @classmethod
    def apply_structure_plan(
        cls,
        canonical_html: str,
        structure: dict[str, Any],
        plan: dict[str, Any],
    ) -> tuple[str, list[dict[str, Any]], list[str]]:
        root = ET.fromstring(canonical_html)
        elements = {
            str(element.attrib.get("id")): element
            for element in root.iter()
            if element.attrib.get("id")
        }
        parents = {child: parent for parent in root.iter() for child in parent}
        source_sections = {
            str(section["section_id"]): section for section in structure.get("sections", [])
        }
        edits: list[dict[str, Any]] = []
        warnings: list[str] = []
        for section_plan in plan.get("sections", []):
            if not isinstance(section_plan, dict):
                continue
            section_id = str(section_plan.get("section_id") or "")
            source = source_sections.get(section_id)
            if source is None:
                continue
            original = cls._strings(source.get("block_ids"))
            desired = cls._strings(section_plan.get("ordered_block_ids"))
            if original == desired:
                continue
            nodes = [elements.get(block_id) for block_id in original]
            if any(node is None for node in nodes):
                warnings.append(f"Structure order skipped missing blocks in {section_id}")
                continue
            typed_nodes = [node for node in nodes if node is not None]
            parent_set = {parents.get(node) for node in typed_nodes}
            if len(parent_set) != 1 or None in parent_set:
                warnings.append(f"Structure order crossed HTML parents in {section_id}")
                continue
            parent = next(iter(parent_set))
            assert parent is not None
            siblings = list(parent)
            positions = sorted(siblings.index(node) for node in typed_nodes)
            if positions != list(range(positions[0], positions[0] + len(positions))):
                warnings.append(f"Structure order was not a contiguous HTML range in {section_id}")
                continue
            by_id = {str(node.attrib["id"]): node for node in typed_nodes}
            for node in typed_nodes:
                parent.remove(node)
            start = positions[0]
            for offset, block_id in enumerate(desired):
                parent.insert(start + offset, by_id[block_id])
            for new_index, block_id in enumerate(desired):
                old_index = original.index(block_id)
                if new_index != old_index:
                    edits.append(
                        {
                            "operation": "reorder_block",
                            "block_id": block_id,
                            "section_id": section_id,
                            "from_index": old_index,
                            "to_index": new_index,
                        }
                    )
        return (
            ET.tostring(root, encoding="unicode", short_empty_elements=False),
            edits,
            warnings,
        )

    @classmethod
    def section_batches(
        cls,
        canonical_html: str,
        *,
        max_characters: int,
        max_blocks: int = 40,
    ) -> list[list[dict[str, Any]]]:
        structure = cls.semantic_structure(canonical_html)
        block_by_id = {str(block["id"]): block for block in structure["blocks"]}
        batches: list[list[dict[str, Any]]] = []
        for section in structure["sections"]:
            if not section.get("plannable"):
                continue
            current: list[dict[str, Any]] = []
            characters = 0
            for block_id in cls._strings(section.get("block_ids")):
                block = dict(block_by_id[block_id])
                size = len(str(block["text"]))
                if current and (characters + size > max_characters or len(current) >= max_blocks):
                    batches.append(current)
                    current = []
                    characters = 0
                current.append(block)
                characters += size
            if current:
                batches.append(current)
        return batches

    @classmethod
    def apply_section_moves(
        cls,
        canonical_html: str,
        moves: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]], list[str]]:
        """Move complete semantic sections without splitting their block ranges."""
        root = ET.fromstring(canonical_html)
        applied: list[dict[str, Any]] = []
        warnings: list[str] = []
        for move in moves:
            operation = str(move.get("operation") or "")
            section_id = str(move.get("section_id") or "")
            anchor_section_id = str(move.get("anchor_section_id") or "")
            if operation not in {"move_section_before", "move_section_after"}:
                warnings.append(f"Unsupported semantic section move: {operation}")
                continue
            if not section_id or section_id == anchor_section_id:
                warnings.append("Semantic section move requires two distinct section IDs")
                continue
            structure = cls.semantic_structure(
                ET.tostring(root, encoding="unicode", short_empty_elements=False)
            )
            sections = {
                str(section["section_id"]): section for section in structure.get("sections", [])
            }
            source = sections.get(section_id)
            anchor = sections.get(anchor_section_id)
            if source is None or anchor is None:
                warnings.append(f"Semantic section move referenced a missing section: {section_id}")
                continue
            elements = {
                str(element.attrib.get("id")): element
                for element in root.iter()
                if element.attrib.get("id")
            }
            parents = {child: parent for parent in root.iter() for child in parent}
            source_nodes = [
                elements.get(block_id) for block_id in cls._strings(source.get("block_ids"))
            ]
            anchor_nodes = [
                elements.get(block_id) for block_id in cls._strings(anchor.get("block_ids"))
            ]
            if (
                not source_nodes
                or not anchor_nodes
                or any(node is None for node in source_nodes + anchor_nodes)
            ):
                warnings.append(f"Semantic section move had missing blocks: {section_id}")
                continue
            typed_source = [node for node in source_nodes if node is not None]
            typed_anchor = [node for node in anchor_nodes if node is not None]
            parent_set = {parents.get(node) for node in [*typed_source, *typed_anchor]}
            if len(parent_set) != 1 or None in parent_set:
                warnings.append(f"Semantic section move crossed HTML parents: {section_id}")
                continue
            parent = next(iter(parent_set))
            assert parent is not None
            siblings = list(parent)
            source_positions = sorted(siblings.index(node) for node in typed_source)
            anchor_positions = sorted(siblings.index(node) for node in typed_anchor)
            source_contiguous = source_positions == list(
                range(source_positions[0], source_positions[0] + len(source_positions))
            )
            anchor_contiguous = anchor_positions == list(
                range(anchor_positions[0], anchor_positions[0] + len(anchor_positions))
            )
            if not source_contiguous or not anchor_contiguous:
                warnings.append(f"Semantic section move found a non-contiguous range: {section_id}")
                continue
            for node in typed_source:
                parent.remove(node)
            remaining = list(parent)
            anchor_positions = sorted(remaining.index(node) for node in typed_anchor)
            insertion = (
                anchor_positions[0]
                if operation == "move_section_before"
                else anchor_positions[-1] + 1
            )
            for offset, node in enumerate(typed_source):
                parent.insert(insertion + offset, node)
            applied.append(
                {
                    "operation": operation,
                    "section_id": section_id,
                    "anchor_section_id": anchor_section_id,
                    "issue_id": str(move.get("issue_id") or ""),
                    "block_ids": cls._strings(source.get("block_ids")),
                }
            )
        return (
            ET.tostring(root, encoding="unicode", short_empty_elements=False),
            applied,
            warnings,
        )

    @staticmethod
    def restore_blocks(
        source_html: str, candidate_html: str, block_ids: set[str]
    ) -> tuple[str, list[str]]:
        source_root = ET.fromstring(source_html)
        candidate_root = ET.fromstring(candidate_html)
        sources = {
            str(element.attrib.get("id")): element
            for element in source_root.iter()
            if element.attrib.get("id")
        }
        candidates = {
            str(element.attrib.get("id")): element
            for element in candidate_root.iter()
            if element.attrib.get("id")
        }
        parents = {child: parent for parent in candidate_root.iter() for child in parent}
        restored: list[str] = []
        for block_id in sorted(block_ids):
            source = sources.get(block_id)
            target = candidates.get(block_id)
            parent = parents.get(target) if target is not None else None
            if source is None or target is None or parent is None:
                continue
            source = copy.deepcopy(source)
            # Restoring prose must not undo the user's mandatory quote separators.
            # Keep this operation idempotent with the downstream surface gate.
            JapaneseSurfaceQualityGate._normalize_element_text(source)
            normalized_target = copy.deepcopy(target)
            JapaneseSurfaceQualityGate._normalize_element_text(normalized_target)
            source_serialized = ET.tostring(source, encoding="unicode", short_empty_elements=False)
            target_serialized = ET.tostring(
                normalized_target, encoding="unicode", short_empty_elements=False
            )
            if source_serialized == target_serialized:
                continue
            position = list(parent).index(target)
            parent.remove(target)
            parent.insert(position, copy.deepcopy(source))
            restored.append(block_id)
        return (
            ET.tostring(candidate_root, encoding="unicode", short_empty_elements=False),
            restored,
        )

    @staticmethod
    def _strings(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item) for item in value if str(item).strip()]
