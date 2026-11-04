"""Deterministic Japanese surface-quality checks for generated prose."""

from __future__ import annotations

import copy
import re
from collections import Counter
from dataclasses import dataclass
from xml.etree import ElementTree as ET

_PROSE_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "blockquote", "li"}
_SINGLE_CHARACTER_BOUNDARY_OVERLAPS = set("、。，．！？!?なはがをにへでともしるうくすつぬぶむ")


@dataclass(frozen=True)
class JapaneseSurfaceIssue:
    """One deterministic surface defect in visible Japanese text."""

    code: str
    message: str
    start: int
    end: int
    token: str
    excerpt: str

    def as_dict(self) -> dict[str, str | int]:
        return {
            "code": self.code,
            "message": self.message,
            "start": self.start,
            "end": self.end,
            "token": self.token,
            "excerpt": self.excerpt,
        }


@dataclass(frozen=True)
class JapaneseSurfaceGateResult:
    """Canonical HTML after invalid generated blocks have been restored."""

    canonical_html: str
    checked_block_count: int
    reverted_blocks: tuple[dict[str, object], ...]
    normalized_blocks: tuple[dict[str, object], ...] = ()

    def as_dict(self) -> dict[str, object]:
        issue_count = sum(len(item.get("issues", [])) for item in self.reverted_blocks)
        return {
            "gate": "japanese_surface_quality",
            "passed_after_revert": True,
            "checked_block_count": self.checked_block_count,
            "detected_issue_count": issue_count,
            "reverted_block_count": len(self.reverted_blocks),
            "reverted_blocks": list(self.reverted_blocks),
            "normalized_block_count": len(self.normalized_blocks),
            "normalized_blocks": list(self.normalized_blocks),
        }


class JapaneseSurfaceQualityGate:
    """Find newly introduced Japanese surface defects and restore bad blocks."""

    _PATTERNS = (
        (
            "surface.missing_middle_dot_between_quoted_items",
            re.compile(r"(?:」[ \t\u3000]*「|』[ \t\u3000]*『)"),
            "Parallel quoted items must be separated with a Japanese middle dot.",
        ),
        (
            "surface.consecutive_punctuation",
            re.compile(r"[、。，．]{2,}"),
            "Consecutive Japanese punctuation was introduced.",
        ),
        (
            "surface.dangling_shi_period",
            re.compile(r"(?<!ただ)(?<!しか)(?<!よ)(?<!も)し。"),
            "A conjunctive verb form ending in 'し。' was introduced.",
        ),
        (
            "surface.particle_or_inflection_repetition",
            re.compile(
                r"(?:"
                r"としてして|についてについて|においてにおいて|におけるにおける|"
                r"によってによって|によるよる|に対してに対して|に対するに対する|"
                r"(?:し|る)する|するする|でありある|であるある|限りて|"
                r"しやすくい|しす(?=[一-龥々〆ヶァ-ヴー、。])|である。る。|"
                r"(?<![ぁ-ん])(?:はは|がが|をを|にに|へへ|でで|とと|のの)(?![ぁ-ん])|"
                r"なな(?=[一-龥々〆ヶァ-ヴー])|"
                r"(?:るる|しし)(?=[一-龥々〆ヶァ-ヴー、。])"
                r")"
            ),
            "A particle or inflection appears duplicated or malformed.",
        ),
        (
            "surface.repeated_lexical_unit",
            re.compile(r"(?!(?:ニコニコ)(?![ァ-ヴー]))([一-龥々〆ヶァ-ヴー]{2,12})\1"),
            "An adjacent lexical unit was duplicated.",
        ),
        (
            "surface.coordinated_lexical_repetition",
            re.compile(
                r"(?P<coordinated_unit>[一-龥々〆ヶァ-ヴーA-Za-z]{2,20})"
                r"と(?P=coordinated_unit)"
                r"(?=[一-龥々〆ヶァ-ヴーA-Za-z、。])"
            ),
            "The same lexical unit appears on both sides of a coordination.",
        ),
        (
            "surface.discourse_phrase_repetition",
            re.compile(
                r"(?P<discourse_phrase>それは|これは|そのため|このため|"
                r"しかし|ただし|もっとも|したがって|そこで|さらに|一方)、"
                r"(?P=discourse_phrase)"
            ),
            "A discourse phrase was repeated across a punctuation boundary.",
        ),
        (
            "surface.repeated_inflectional_stem",
            re.compile(
                r"(?<![一-龥々〆ヶ])"
                r"([一-龥々〆ヶ])(?:[いきしちにびみり])?\1[うくすつぬぶむる]"
            ),
            "A verb stem appears duplicated at an edit boundary.",
        ),
        (
            "surface.punctuation_before_closer",
            re.compile(r"、(?=[）)])"),
            "A Japanese comma immediately before a closing parenthesis was introduced.",
        ),
        (
            "surface.middle_dot_before_particle",
            re.compile(
                r"・(?=(?:に|を|が|は|へ|で|と|の|も)"
                r"(?:注目|着目|関係|ついて|おいて|よって|基づ|伴|向け|置|含|"
                r"なる|する|した|ある|ない|示|扱|検討|分析|意味|指))"
            ),
            "A stray middle dot immediately before a Japanese particle was introduced.",
        ),
    )

    # One light-verb construction can be legitimate academic Japanese. Flag
    # only the second and later occurrence in a sentence: this is a conservative
    # signal that nominalization has become a repeated mannerism.
    _LIGHT_VERB_NOMINALIZATION = re.compile(
        r"(?:分析|検討|考察|説明|確認|提示|整理|改善|調整|評価|測定|比較|検証|"
        r"観察|記録|報告|選択|決定|設定|構築|形成|把握|理解|認識|使用|利用|適用)"
        r"(?:を|の)(?:実施(?:し|する|した|され)|行[いうえっわ]|図[るりっ])"
    )

    @classmethod
    def find(cls, text: str) -> tuple[JapaneseSurfaceIssue, ...]:
        issues: list[JapaneseSurfaceIssue] = []
        for code, pattern, message in cls._PATTERNS:
            for match in pattern.finditer(text):
                token = match.group(0)
                start, end = match.span()
                if code == "surface.repeated_lexical_unit" and cls._is_allowed_repetition(
                    text, match
                ):
                    continue
                if (
                    code == "surface.coordinated_lexical_repetition"
                    and cls._is_compound_coordination_overlap(text, match)
                ):
                    continue
                issues.append(
                    JapaneseSurfaceIssue(
                        code=code,
                        message=message,
                        start=start,
                        end=end,
                        token=token,
                        excerpt=cls._excerpt(text, start, end),
                    )
                )
        for sentence in re.finditer(r"[^。！？!?\r\n]+[。！？!?]?", text):
            matches = list(cls._LIGHT_VERB_NOMINALIZATION.finditer(sentence.group(0)))
            for match in matches[1:]:
                start = sentence.start() + match.start()
                end = sentence.start() + match.end()
                issues.append(
                    JapaneseSurfaceIssue(
                        code="style.excessive_light_verb_nominalization",
                        message=(
                            "A sentence repeatedly stacks verbal nouns with generic "
                            "light verbs; prefer a direct predicate where meaning permits."
                        ),
                        start=start,
                        end=end,
                        token=match.group(0),
                        excerpt=cls._excerpt(text, start, end),
                    )
                )
        return tuple(sorted(issues, key=lambda item: (item.start, item.end, item.code)))

    @staticmethod
    def _is_compound_coordination_overlap(text: str, match: re.Match[str]) -> bool:
        """Allow ``AXとXB`` compounds while retaining standalone ``XとXB`` defects."""

        start, end = match.span()
        if start == 0 or end >= len(text):
            return False
        lexical = re.compile(r"[一-龥々〆ヶァ-ヴーA-Za-z]")
        return bool(lexical.fullmatch(text[start - 1]) and lexical.fullmatch(text[end]))

    @staticmethod
    def normalize_quoted_enumerations(text: str) -> tuple[str, int]:
        """Normalize adjacent Japanese quoted items without changing their content."""

        normalized, corner_count = re.subn(r"」[ \t\u3000]*「", "」・「", text)
        normalized, book_count = re.subn(r"』[ \t\u3000]*『", "』・『", normalized)
        return normalized, corner_count + book_count

    @classmethod
    def _normalize_element_text(cls, element: ET.Element) -> int:
        count = 0
        for node in element.iter():
            if node.text:
                node.text, changed = cls.normalize_quoted_enumerations(node.text)
                count += changed
            if node.tail:
                node.tail, changed = cls.normalize_quoted_enumerations(node.tail)
                count += changed
        return count

    @staticmethod
    def _is_allowed_repetition(text: str, match: re.Match[str]) -> bool:
        """Exclude lexical reduplication and substrings inside katakana words."""
        token = match.group(0)
        unit = match.group(1)
        if unit.startswith("一"):
            return True
        if all("ァ" <= character <= "ヴ" or character == "ー" for character in unit):
            previous = text[match.start() - 1] if match.start() else ""
            following = text[match.end()] if match.end() < len(text) else ""
            embedded = any(
                "ァ" <= character <= "ヴ" or character == "ー"
                for character in (previous, following)
            )
            return embedded or len(unit) == 2
        return token == "ニコニコ"

    @classmethod
    def introduced(cls, source: str, candidate: str) -> tuple[JapaneseSurfaceIssue, ...]:
        """Return only issue occurrences added relative to the source text."""
        source_counts = Counter(cls._fingerprint(issue) for issue in cls.find(source))
        introduced: list[JapaneseSurfaceIssue] = []
        seen: Counter[tuple[str, str]] = Counter()
        for issue in cls.find(candidate):
            fingerprint = cls._fingerprint(issue)
            seen[fingerprint] += 1
            if seen[fingerprint] > source_counts[fingerprint]:
                introduced.append(issue)
        return tuple(introduced)

    @classmethod
    def edit_contract_issues(
        cls,
        current: str,
        *,
        before: str,
        after: str,
        start: int,
    ) -> tuple[JapaneseSurfaceIssue, ...]:
        """Check no-op edits and duplication across both replacement boundaries."""
        if before == after:
            return (
                JapaneseSurfaceIssue(
                    code="surface.noop_edit",
                    message="The edit does not change the source text.",
                    start=start,
                    end=start + len(before),
                    token=before,
                    excerpt=cls._excerpt(current, start, start + len(before)),
                ),
            )

        left = current[:start]
        right = current[start + len(before) :]
        candidate = left + after + right
        issues: list[JapaneseSurfaceIssue] = list(cls.introduced(current, candidate))
        boundaries = (
            (left, after, start, "left"),
            (after, right, start + len(after), "right"),
        )
        old_boundaries = ((left, before), (before, right))
        for (first, second, boundary, side), (old_first, old_second) in zip(
            boundaries, old_boundaries, strict=True
        ):
            overlap = cls._longest_suspicious_overlap(first, second)
            if not overlap or overlap == cls._longest_suspicious_overlap(old_first, old_second):
                continue
            issue_start = max(0, boundary - len(overlap))
            issues.append(
                JapaneseSurfaceIssue(
                    code="surface.replacement_boundary_repetition",
                    message=(f"The replacement repeats text across its {side} boundary: {overlap}"),
                    start=issue_start,
                    end=min(len(candidate), boundary + len(overlap)),
                    token=overlap,
                    excerpt=cls._excerpt(
                        candidate,
                        issue_start,
                        min(len(candidate), boundary + len(overlap)),
                    ),
                )
            )
        return cls._deduplicate(issues)

    @classmethod
    def guard_canonical_html(
        cls, source_html: str, candidate_html: str
    ) -> JapaneseSurfaceGateResult:
        """Restore each candidate prose block that introduces a surface defect."""
        source_root = ET.fromstring(source_html)
        candidate_root = ET.fromstring(candidate_html)
        source_by_id = {
            str(element.attrib["id"]): element
            for element in source_root.iter()
            if element.tag in _PROSE_TAGS and element.attrib.get("id")
        }
        candidate_blocks = [
            element
            for element in candidate_root.iter()
            if element.tag in _PROSE_TAGS and element.attrib.get("id")
        ]
        reverted: list[dict[str, object]] = []
        normalized: list[dict[str, object]] = []
        for candidate in candidate_blocks:
            block_id = str(candidate.attrib["id"])
            source = source_by_id.get(block_id)
            normalization_count = cls._normalize_element_text(candidate)
            if normalization_count:
                normalized.append(
                    {
                        "block_id": block_id,
                        "action": "insert_middle_dot_between_quoted_items",
                        "replacement_count": normalization_count,
                    }
                )
            candidate_text = "".join(candidate.itertext()).strip()
            if source is None:
                issues = cls.find(candidate_text)
            else:
                source_text = "".join(source.itertext()).strip()
                issues = cls.introduced(source_text, candidate_text)
            if not issues:
                continue
            parents = {child: parent for parent in candidate_root.iter() for child in parent}
            parent = parents.get(candidate)
            if parent is None:
                continue
            position = list(parent).index(candidate)
            if source is None:
                parent.remove(candidate)
                action = "removed_inserted_block"
            else:
                parent.remove(candidate)
                parent.insert(position, copy.deepcopy(source))
                action = "restored_source_block"
            reverted.append(
                {
                    "block_id": block_id,
                    "action": action,
                    "issues": [issue.as_dict() for issue in issues],
                }
            )
        guarded_html = ET.tostring(candidate_root, encoding="unicode", short_empty_elements=False)
        return JapaneseSurfaceGateResult(
            canonical_html=guarded_html,
            checked_block_count=len(candidate_blocks),
            reverted_blocks=tuple(reverted),
            normalized_blocks=tuple(normalized),
        )

    @classmethod
    def find_canonical_html(cls, canonical_html: str) -> tuple[dict[str, object], ...]:
        root = ET.fromstring(canonical_html)
        findings: list[dict[str, object]] = []
        for element in root.iter():
            if element.tag not in _PROSE_TAGS or not element.attrib.get("id"):
                continue
            issues = cls.find("".join(element.itertext()).strip())
            if issues:
                findings.append(
                    {
                        "block_id": str(element.attrib["id"]),
                        "issues": [issue.as_dict() for issue in issues],
                    }
                )
        return tuple(findings)

    @staticmethod
    def _excerpt(text: str, start: int, end: int) -> str:
        return text[max(0, start - 24) : min(len(text), end + 24)]

    @staticmethod
    def _fingerprint(issue: JapaneseSurfaceIssue) -> tuple[str, str]:
        return issue.code, issue.token

    @staticmethod
    def _longest_suspicious_overlap(first: str, second: str) -> str:
        maximum = min(len(first), len(second), 12)
        for size in range(maximum, 0, -1):
            overlap = first[-size:]
            if overlap != second[:size]:
                continue
            if size >= 2 and re.search(r"[ぁ-んァ-ヴー一-龥々〆ヶ]", overlap):
                return overlap
            if size == 1 and overlap in _SINGLE_CHARACTER_BOUNDARY_OVERLAPS:
                return overlap
        return ""

    @staticmethod
    def _deduplicate(
        issues: list[JapaneseSurfaceIssue],
    ) -> tuple[JapaneseSurfaceIssue, ...]:
        result: list[JapaneseSurfaceIssue] = []
        seen: set[tuple[str, int, int, str]] = set()
        for issue in issues:
            key = (issue.code, issue.start, issue.end, issue.token)
            if key in seen:
                continue
            seen.add(key)
            result.append(issue)
        return tuple(result)
