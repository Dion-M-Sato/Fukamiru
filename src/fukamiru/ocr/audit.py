"""Deterministic OCR integrity and cross-page continuity checks."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field


@dataclass(frozen=True)
class AuditPage:
    page_number: int
    text: str
    printed_label: str | None = None


@dataclass(frozen=True)
class NoteCandidate:
    scope_id: str
    label: str
    page_number: int
    kind: str = "footnote"


@dataclass(frozen=True)
class NoteReferenceCandidate:
    scope_id: str
    label: str
    page_number: int


@dataclass(frozen=True)
class OCRFinding:
    defect_type: str
    severity: str
    page_start: int | None
    page_end: int | None
    excerpt: str
    context: str
    rationale: str
    repair_candidates: tuple[str, ...] = ()
    confidence: float = 1.0
    automatic_repair_allowed: bool = False


@dataclass(frozen=True)
class BoundaryContext:
    previous_page: int
    next_page: int
    previous_tail: str
    next_head: str

    @property
    def combined(self) -> str:
        return f"{self.previous_tail}\n���PAGE_BOUNDARY���\n{self.next_head}"


@dataclass(frozen=True)
class OCRAuditResult:
    findings: tuple[OCRFinding, ...]
    boundary_contexts: tuple[BoundaryContext, ...]
    normalized_pages: tuple[AuditPage, ...]
    applied_repairs: tuple[dict[str, object], ...] = field(default_factory=tuple)

    @property
    def has_critical(self) -> bool:
        return any(finding.severity == "critical" for finding in self.findings)


class OCRIntegrityAuditor:
    def __init__(self, *, boundary_context_characters: int = 240) -> None:
        self.boundary_context_characters = boundary_context_characters

    def audit(
        self,
        pages: list[AuditPage],
        *,
        language: str | None = None,
        notes: list[NoteCandidate] | None = None,
        note_references: list[NoteReferenceCandidate] | None = None,
    ) -> OCRAuditResult:
        findings: list[OCRFinding] = []
        contexts = self._page_sequence_findings(pages, findings)
        repeated_headers, repeated_footers = self._repeated_margins(pages, findings)
        normalized_pages, repairs = self._normalize_allowed(
            pages, repeated_headers, repeated_footers, contexts, findings
        )
        for page in pages:
            self._text_integrity(page, language, findings)
        self._note_integrity(notes or [], note_references or [], findings)
        return OCRAuditResult(
            findings=tuple(findings),
            boundary_contexts=tuple(contexts),
            normalized_pages=tuple(normalized_pages),
            applied_repairs=tuple(repairs),
        )

    def _page_sequence_findings(
        self, pages: list[AuditPage], findings: list[OCRFinding]
    ) -> list[BoundaryContext]:
        numbers = [page.page_number for page in pages]
        duplicates = sorted(number for number, count in Counter(numbers).items() if count > 1)
        for number in duplicates:
            findings.append(
                OCRFinding(
                    "page_duplicate",
                    "critical",
                    number,
                    number,
                    str(number),
                    repr(numbers),
                    f"Source page {number} occurs more than once.",
                )
            )
        if numbers != sorted(numbers):
            findings.append(
                OCRFinding(
                    "page_order_reversed",
                    "critical",
                    min(numbers, default=0) or None,
                    max(numbers, default=0) or None,
                    repr(numbers),
                    repr(numbers),
                    "Source pages are not in ascending order.",
                )
            )
        unique = sorted(set(numbers))
        if unique:
            missing = sorted(set(range(unique[0], unique[-1] + 1)) - set(unique))
            for number in missing:
                findings.append(
                    OCRFinding(
                        "page_missing",
                        "critical",
                        number,
                        number,
                        str(number),
                        repr(numbers),
                        f"Source page {number} is missing from the OCR sequence.",
                    )
                )
        contexts: list[BoundaryContext] = []
        for previous, following in zip(pages, pages[1:], strict=False):
            context = BoundaryContext(
                previous.page_number,
                following.page_number,
                previous.text[-self.boundary_context_characters :],
                following.text[: self.boundary_context_characters],
            )
            contexts.append(context)
            tail = previous.text.rstrip()
            head = following.text.lstrip()
            if re.search(r"[A-Za-zÀ-ÖØ-öø-ÿ]-$", tail) and re.match(
                r"[a-zà-öø-ÿ]", head
            ):
                left = re.search(r"([A-Za-zÀ-ÖØ-öø-ÿ]+)-$", tail)
                right = re.match(r"([A-Za-zÀ-ÖØ-öø-ÿ]+)", head)
                joined = f"{left.group(1)}{right.group(1)}" if left and right else ""
                findings.append(
                    OCRFinding(
                        "page_boundary_hyphenation",
                        "minor",
                        previous.page_number,
                        following.page_number,
                        f"{left.group(0) if left else '-'} / {right.group(0) if right else ''}",
                        context.combined,
                        "A lowercase word is split by a line-end hyphen at the page boundary.",
                        (joined,),
                        0.99,
                        True,
                    )
                )
        return contexts

    @staticmethod
    def _repeated_margins(
        pages: list[AuditPage], findings: list[OCRFinding]
    ) -> tuple[set[str], set[str]]:
        first_lines: list[str] = []
        last_lines: list[str] = []
        for page in pages:
            lines = [line.strip() for line in page.text.splitlines() if line.strip()]
            if lines:
                first_lines.append(lines[0])
                last_lines.append(lines[-1])
        threshold = max(2, (len(pages) + 1) // 2)
        headers = {line for line, count in Counter(first_lines).items() if count >= threshold}
        footers = {line for line, count in Counter(last_lines).items() if count >= threshold}
        for kind, values in (("header", headers), ("footer", footers)):
            for value in values:
                findings.append(
                    OCRFinding(
                        f"repeated_{kind}_contamination",
                        "minor",
                        min((page.page_number for page in pages), default=None),
                        max((page.page_number for page in pages), default=None),
                        value,
                        value,
                        f"The same {kind} text is repeated across source pages.",
                        ("remove repeated margin line",),
                        0.98,
                        True,
                    )
                )
        return headers, footers

    @staticmethod
    def _normalize_allowed(
        pages: list[AuditPage],
        headers: set[str],
        footers: set[str],
        contexts: list[BoundaryContext],
        findings: list[OCRFinding],
    ) -> tuple[list[AuditPage], list[dict[str, object]]]:
        normalized: list[AuditPage] = []
        repairs: list[dict[str, object]] = []
        for page in pages:
            original = page.text
            text = unicodedata.normalize("NFC", original.replace("\r\n", "\n").replace("\r", "\n"))
            lines = text.splitlines()
            if lines and lines[0].strip() in headers:
                removed = lines.pop(0)
                repairs.append(
                    {"type": "remove_repeated_header", "page": page.page_number, "text": removed}
                )
            if lines and lines[-1].strip() in footers:
                removed = lines.pop()
                repairs.append(
                    {"type": "remove_repeated_footer", "page": page.page_number, "text": removed}
                )
            text = "\n".join(lines)
            text = re.sub(r"(?<![.!?。！？:;])\n(?=[a-zà-öø-ÿ])", " ", text)
            normalized.append(AuditPage(page.page_number, text, page.printed_label))
            if text != original and not any(
                repair.get("page") == page.page_number for repair in repairs
            ):
                repairs.append(
                    {"type": "unicode_or_linebreak_normalization", "page": page.page_number}
                )
        for context in contexts:
            previous_index = next(
                (
                    i
                    for i, page in enumerate(normalized)
                    if page.page_number == context.previous_page
                ),
                None,
            )
            next_index = next(
                (i for i, page in enumerate(normalized) if page.page_number == context.next_page),
                None,
            )
            if previous_index is None or next_index is None:
                continue
            previous = normalized[previous_index]
            following = normalized[next_index]
            if re.search(r"[A-Za-zÀ-ÖØ-öø-ÿ]-\s*$", previous.text) and re.match(
                r"\s*[a-zà-öø-ÿ]", following.text
            ):
                normalized[previous_index] = AuditPage(
                    previous.page_number,
                    re.sub(r"-\s*$", "", previous.text),
                    previous.printed_label,
                )
                repairs.append(
                    {
                        "type": "join_page_boundary_hyphenation",
                        "page_start": previous.page_number,
                        "page_end": following.page_number,
                    }
                )
        del findings
        return normalized, repairs

    @staticmethod
    def _text_integrity(
        page: AuditPage, language: str | None, findings: list[OCRFinding]
    ) -> None:
        text = page.text
        mojibake = re.search(r"(?:�|Ã.|Â.|â€™|â€œ|â€|ï¬|\uFFFD)", text)
        if mojibake:
            findings.append(
                OCRFinding(
                    "ocr_mojibake",
                    "major",
                    page.page_number,
                    page.page_number,
                    mojibake.group(0),
                    text[max(0, mojibake.start() - 80) : mojibake.end() + 80],
                    "Replacement or mis-decoded byte sequences were found.",
                    confidence=0.99,
                )
            )
        symbols = re.search(r"[^\w\sÀ-ÖØ-öø-ÿ。、，．！？「」『』（）()'’\-]{5,}", text)
        if symbols:
            findings.append(
                OCRFinding(
                    "abnormal_symbol_run",
                    "major",
                    page.page_number,
                    page.page_number,
                    symbols.group(0),
                    text[max(0, symbols.start() - 80) : symbols.end() + 80],
                    "An unusually long symbol sequence suggests OCR corruption.",
                    confidence=0.9,
                )
            )
        for opening, closing, label in (("(", ")", "parenthesis"), ("[", "]", "bracket")):
            if text.count(opening) != text.count(closing):
                findings.append(
                    OCRFinding(
                        "delimiter_unbalanced",
                        "major",
                        page.page_number,
                        page.page_number,
                        label,
                        text[-240:],
                        f"OCR page has unbalanced {label} delimiters.",
                        confidence=0.9,
                    )
                )
        french_ligature = re.search(r"\b(?:oeuvre|coeur|soeur)\b", text, re.I)
        if language and language.lower().startswith("fr") and french_ligature:
            suspect = re.search(r"\b(?:oeuvre|coeur|soeur)\b", text, re.I)
            assert suspect is not None
            findings.append(
                OCRFinding(
                    "french_ligature_suspect",
                    "minor",
                    page.page_number,
                    page.page_number,
                    suspect.group(0),
                    text[max(0, suspect.start() - 80) : suspect.end() + 80],
                    "A French oe sequence may be a damaged œ ligature; "
                    "human confirmation is required.",
                    (suspect.group(0).replace("oe", "œ").replace("Oe", "Œ"),),
                    0.7,
                    False,
                )
            )
        if language and language.lower().startswith("ja"):
            vertical = re.search(r"(?:[ぁ-んァ-ヶ一-龯]\s+){4,}[ぁ-んァ-ヶ一-龯]", text)
            if vertical:
                findings.append(
                    OCRFinding(
                        "japanese_vertical_ocr_damage",
                        "major",
                        page.page_number,
                        page.page_number,
                        vertical.group(0),
                        text[max(0, vertical.start() - 80) : vertical.end() + 80],
                        "Japanese characters are separated like a damaged "
                        "vertical-text OCR stream.",
                        confidence=0.85,
                    )
                )

    @staticmethod
    def _note_integrity(
        notes: list[NoteCandidate],
        references: list[NoteReferenceCandidate],
        findings: list[OCRFinding],
    ) -> None:
        note_keys = {(note.scope_id, note.label) for note in notes}
        reference_keys = {(reference.scope_id, reference.label) for reference in references}
        for reference in references:
            if (reference.scope_id, reference.label) not in note_keys:
                findings.append(
                    OCRFinding(
                        "note_reference_without_body",
                        "major",
                        reference.page_number,
                        reference.page_number,
                        reference.label,
                        f"scope={reference.scope_id}; label={reference.label}",
                        "A note reference has no matching note body in the same scope.",
                        confidence=1.0,
                    )
                )
        for note in notes:
            if (note.scope_id, note.label) not in reference_keys:
                findings.append(
                    OCRFinding(
                        "note_body_without_reference",
                        "major",
                        note.page_number,
                        note.page_number,
                        note.label,
                        f"scope={note.scope_id}; label={note.label}",
                        "A note body has no matching text reference in the same scope.",
                        confidence=1.0,
                    )
                )
