"""Edit rendered body text while keeping every existing DOM node and citation."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from typing import Any
from xml.etree import ElementTree as ET

from fukamiru.documents.canonical_html import CanonicalHTMLValidator
from fukamiru.errors import CanonicalHTMLValidationError, NonRetryableFukamiruError
from fukamiru.revisions.coherence import ManuscriptCoherenceService

LOCKED_INLINE_TAGS = {"sup", "a", "cite", "q", "code"}
_LITERALS = (
    re.compile(r"[+-]?\d[\d,._]*(?:%|％)?"),
    re.compile(r"「[^」]*」|『[^』]*』|“[^”]*”"),
    re.compile(r"\[\^[^\]]+\]|https?://[^\s]+"),
)


def _protected_literals(text: str, propositions: list[str]) -> tuple[Counter[str], ...]:
    found = [pattern.findall(text) for pattern in _LITERALS]
    found[1] = [
        token for token in found[1]
        if not (
            token in propositions
            or (
                token.startswith(("「", "“"))
                and len(token[1:-1].strip()) >= 24
                and any(
                    len(proposition.strip()) >= 24
                    and (
                        token[1:-1].strip() in proposition
                        or proposition.strip("「」『』“”") in token[1:-1]
                    )
                    for proposition in propositions
                )
            )
        )
    ]
    return tuple(Counter(tokens) for tokens in found)


def quoted_question_candidates(paragraphs: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Identify question literals in editable slots; provenance is judged separately."""
    return [
        {"paragraph_id": paragraph["paragraph_id"], "literal": match.group()}
        for paragraph in paragraphs
        for slot in paragraph["text_slots"]
        for match in re.finditer(r"「[^」]{2,80}か」", slot)
    ]


def digest(html: str) -> str:
    return hashlib.sha256(html.encode("utf-8")).hexdigest()


def reader_paragraph_text(html: str) -> dict[str, str]:
    """Keep superscript note labels out of the prose sent to the reader reviewer."""
    def text(node: ET.Element) -> str:
        if node.tag == "sup" and any(
            anchor.get("role") == "doc-noteref" for anchor in node.iter("a")
        ):
            return ""
        return (node.text or "") + "".join(text(child) + (child.tail or "") for child in node)

    return {str(p.get("id")): text(p) for p in _parse(html).iter("p")}


def validate_handoff(context: dict[str, Any]) -> None:
    report = context.get("prose_redraft_report_v31")
    if report is None:  # Already executed V31 runs remain readable.
        return
    candidate_sha = digest(str(context.get("proposed_html") or ""))
    prose_sha = report.get("target_html_sha256") if isinstance(report, dict) else None
    transitions = report.get("paragraph_transitions") if isinstance(report, dict) else None
    if transitions is not None and (
        transitions != context.get("paragraph_transition_report_v32")
        or transitions.get("target_html_sha256") != prose_sha
    ):
        raise NonRetryableFukamiruError("Paragraph transition handoff has a stale validation")
    heading = context.get("heading_revision_report_v31")
    heading_handoff_valid = (
        isinstance(heading, dict)
        and heading.get("source_html_sha256") == prose_sha
        and heading.get("target_html_sha256") == candidate_sha
        and heading.get("structure_preserved") is True
    )
    if (
        not isinstance(report, dict)
        or (prose_sha != candidate_sha and not heading_handoff_valid)
        or report.get("inference_ledger", []) != context.get(
            "prose_redraft_inference_ledger_v31", []
        )
        or not all(
            report.get(key) is True
            for key in (
                "structure_unchanged",
                "locked_content_unchanged",
                "anchors_valid",
            )
        )
    ):
        raise NonRetryableFukamiruError("Prose redraft handoff has a missing or stale validation")


def supersede_for_repair(context: dict[str, Any]) -> dict[str, Any]:
    """A deliberate upstream repair must return through the rendered prose gate."""
    if context.get("prose_redraft_report_v31") is None:
        return context
    validate_handoff(context)
    updated = dict(context)
    report = updated.pop("prose_redraft_report_v31")
    updated.pop("prose_redraft_inference_ledger_v31", None)
    updated.pop("paragraph_transition_report_v32", None)
    updated.pop("concept_fit_final_audit_v32", None)
    updated["prose_redraft_history_v31"] = [
        *list(context.get("prose_redraft_history_v31") or []),
        {
            key: report.get(key)
            for key in ("source_html_sha256", "target_html_sha256", "report_path", "status")
        },
    ]
    updated["human_review_items"] = [
        item
        for item in context.get("human_review_items") or []
        if not str(item.get("review_item_id") or "").startswith(
            ("prose-redraft:", "transition:", "concept-fit:")
        )
    ]
    updated["required_user_information"] = [
        item
        for item in context.get("required_user_information") or []
        if item.get("code") != "authoring.prose_redraft_review_required"
    ]
    return updated


def _parse(html: str) -> ET.Element:
    try:
        CanonicalHTMLValidator().assert_valid(html)
    except CanonicalHTMLValidationError as exc:
        raise NonRetryableFukamiruError(
            f"Prose redraft requires valid canonical HTML: {exc}"
        ) from exc
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    return ET.fromstring(html, parser=parser)


def _slots(paragraph: ET.Element, protected: set[str]) -> list[tuple[tuple[int, ...], str]]:
    slots: list[tuple[tuple[int, ...], str]] = []

    def walk(node: ET.Element, path: tuple[int, ...]) -> None:
        slots.append((path, "text"))
        for index, child in enumerate(node):
            child_path = (*path, index)
            if (
                isinstance(child.tag, str)
                and child.tag not in LOCKED_INLINE_TAGS
                and child.get("id") not in protected
            ):
                walk(child, child_path)
            slots.append((child_path, "tail"))

    walk(paragraph, ())
    return slots


def _at(node: ET.Element, path: tuple[int, ...]) -> ET.Element:
    for index in path:
        node = node[index]
    return node


def inventory(html: str, protected_ids: set[str] | None = None, *,
              include_empty: bool = False) -> list[dict[str, Any]]:
    """Visit each paragraph once, including nested inline text and nested sections."""
    root = _parse(html)
    protected = protected_ids or set()
    terminal_ids = {
        str(block["id"])
        for block in ManuscriptCoherenceService.semantic_structure(html)["blocks"]
        if block.get("document_role") in {"references", "notes"}
    }
    result: list[dict[str, Any]] = []

    def walk(node: ET.Element, section: ET.Element | None, excluded: bool) -> None:
        excluded = excluded or bool(
            node.get("data-reference-section")
            or node.get("data-citation-section")
            or node.get("id") in protected
            or node.get("id") in terminal_ids
        )
        if node.tag == "section":
            section = node
        if node.tag == "p" and section is not None and not excluded:
            visible = "".join(node.itertext())
            if visible.strip() or include_empty:
                paths = _slots(node, protected)
                template = ET.fromstring(
                    ET.tostring(node, encoding="unicode"),
                    parser=ET.XMLParser(target=ET.TreeBuilder(insert_comments=True)),
                )
                for index, (path, field) in enumerate(paths):
                    setattr(_at(template, path), field, f"{{{{TEXT:{index}}}}}")
                result.append(
                    {
                        "paragraph_id": str(node.get("id") or ""),
                        "section_id": str(section.get("id") or ""),
                        "heading": next(
                            (
                                "".join(h.itertext())
                                for h in section
                                if h.tag in {"h2", "h3", "h4", "h5", "h6"}
                            ),
                            "",
                        ),
                        "visible_text": visible,
                        "text_slots": [
                            getattr(_at(node, path), field) or "" for path, field in paths
                        ],
                        "html_template": ET.tostring(template, encoding="unicode"),
                    }
                )
        for child in node:
            walk(child, section, excluded)

    walk(root, root, False)
    return result


def source_blind_paragraphs(
    paragraphs: list[dict[str, Any]],
    *,
    rephrasable_propositions: list[str] | None = None,
    rephrasable_by_paragraph: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    """Expose DOM slots to a composer without editable source wording."""
    return [
        {
            "paragraph_id": paragraph["paragraph_id"],
            "section_id": paragraph["section_id"],
            "slot_count": len(paragraph["text_slots"]),
            "html_template": paragraph["html_template"],
            "protected_literals": [
                literal
                for counter in _protected_literals(
                    "".join(paragraph["text_slots"]),
                    [
                        *(rephrasable_propositions or []),
                        *(rephrasable_by_paragraph or {}).get(
                            paragraph["paragraph_id"], []
                        ),
                    ],
                )
                for literal, count in counter.items()
                for _ in range(count)
            ],
        }
        for paragraph in paragraphs
    ]


_WRITER_ATTRIBUTION = re.compile(r"筆者(?:が|は)(『[^。]{1,240}?』)で論じた")


def compose_edit_paragraphs(
    paragraphs: list[dict[str, Any]],
    diagnoses: list[dict[str, Any]],
    *,
    rephrasable_propositions: list[str] | None = None,
    rephrasable_by_paragraph: dict[str, list[str]] | None = None,
    include_without_issues: bool = False,
) -> list[dict[str, Any]]:
    """Give the writer the actual prose and actionable defects for affected paragraphs."""
    ids = [str(item["paragraph_id"]) for item in paragraphs]
    exact_rows(diagnoses, ids, "diagnosis")
    blind = source_blind_paragraphs(
        paragraphs,
        rephrasable_propositions=rephrasable_propositions,
        rephrasable_by_paragraph=rephrasable_by_paragraph,
    )
    hints = redraft_issue_hints(diagnoses)
    return [
        {
            **structure,
            "source_text_slots": list(paragraph["text_slots"]),
            "diagnosed_issues": hint["issues"],
            "writer_attribution_anchors": [
                match.group(0)
                for slot in paragraph["text_slots"]
                for match in _WRITER_ATTRIBUTION.finditer(slot)
            ],
        }
        for paragraph, structure, hint in zip(paragraphs, blind, hints, strict=True)
        if include_without_issues or hint["issues"]
    ]


def restore_writer_attribution(source_slots: list[str], candidate_slots: list[str]) -> list[str]:
    """Keep an existing first-person scholarly attribution from becoming 自著 as actor."""
    if len(source_slots) != len(candidate_slots):
        return candidate_slots
    restored = list(candidate_slots)
    for index, (source, candidate) in enumerate(zip(source_slots, restored, strict=True)):
        for match in _WRITER_ATTRIBUTION.finditer(source):
            phrase = match.group(0)
            if phrase in candidate:
                continue
            candidate = candidate.replace("自著" + match.group(1) + "で論じた", phrase)
        restored[index] = candidate
    return restored


def heading_inventory(html: str, protected_ids: set[str] | None = None) -> list[dict[str, str]]:
    """Collect plain-text body section headings, leaving note/reference headings locked."""
    root = _parse(html)
    protected = protected_ids or set()
    result: list[dict[str, str]] = []
    for section in root.iter("section"):
        if section.get("data-citation-section") or section.get("data-reference-section"):
            continue
        section_id = str(section.get("id") or "")
        for child in section:
            if child.tag not in {"h2", "h3", "h4", "h5", "h6"}:
                continue
            heading_id = str(child.get("id") or "")
            if not heading_id or heading_id in protected or len(child):
                continue
            result.append({
                "section_id": section_id, "heading_id": heading_id,
                "text": child.text or "",
            })
    return result


def apply_heading_edits(
    html: str, edits: list[dict[str, str]], protected_ids: set[str] | None = None,
) -> tuple[str, list[str]]:
    """Change heading text only, with the same DOM and literal preservation gate."""
    source = heading_inventory(html, protected_ids)
    ids = [item["heading_id"] for item in source]
    if [str(item.get("heading_id") or "") for item in edits] != ids:
        raise NonRetryableFukamiruError("Heading redraft changed IDs or order")
    root = _parse(html)
    by_id = {node.get("id"): node for node in root.iter() if node.get("id")}
    sections_by_heading = {
        child.get("id"): section
        for section in root.iter("section")
        for child in section
        if isinstance(child.tag, str) and child.tag in {"h2", "h3", "h4", "h5", "h6"}
        and child.get("id")
    }
    changed: list[str] = []
    for original, edit in zip(source, edits, strict=True):
        heading_id = original["heading_id"]
        replacement = edit.get("text")
        if not isinstance(replacement, str) or not replacement.strip() or re.search(
            r"</?[A-Za-z][^>]*>", replacement
        ):
            raise NonRetryableFukamiruError(f"Invalid heading text: {heading_id}")
        before_literals = _protected_literals(original["text"], [])
        after_literals = _protected_literals(replacement, [])
        if any(before - after for before, after in zip(
            before_literals, after_literals, strict=True
        )):
            raise NonRetryableFukamiruError(f"Heading removed protected content: {heading_id}")
        section = sections_by_heading[heading_id]
        body_text = "".join(
            "".join(child.itertext()) for child in section
            if isinstance(child.tag, str)
            and child.tag not in {"h2", "h3", "h4", "h5", "h6"}
        )
        if any(
            token not in body_text
            for before, after in zip(before_literals, after_literals, strict=True)
            for token in (after - before).elements()
        ):
            raise NonRetryableFukamiruError(
                f"Heading introduced an unsupported literal: {heading_id}"
            )
        by_id[heading_id].text = replacement
        if replacement != original["text"]:
            changed.append(heading_id)
    revised = ET.tostring(root, encoding="unicode") if changed else html
    if manifest(html, protected_ids, set(ids)) != manifest(
        revised, protected_ids, set(ids)
    ):
        raise NonRetryableFukamiruError("Heading redraft changed locked DOM content")
    return revised, changed


def citation_context(html: str, paragraph_ids: list[str]) -> list[dict[str, Any]]:
    """Resolve fixed citation elements to their existing footnote/bibliography text."""
    root = _parse(html)
    by_id = {node.get("id"): node for node in root.iter() if node.get("id")}
    result: list[dict[str, Any]] = []
    for paragraph_id in paragraph_ids:
        paragraph = by_id[paragraph_id]
        notes = []
        for anchor in paragraph.iter("a"):
            href = anchor.get("href", "")
            target = by_id.get(href[1:]) if href.startswith("#") else None
            if target is not None:
                notes.append(
                    {
                        "anchor_id": anchor.get("id"),
                        "href": href,
                        "target_id": target.get("id"),
                        "target_text": "".join(target.itertext()),
                        "reference_id": target.get("data-reference-id"),
                    }
                )
        reference_ids = {
            node.get("data-reference-id")
            for node in paragraph.iter()
            if node.get("data-reference-id")
        } | {note["reference_id"] for note in notes if note["reference_id"]}
        bibliography = [
            {
                "element_id": node.get("id"),
                "reference_id": node.get("data-reference-id"),
                "text": "".join(node.itertext()),
            }
            for node in root.iter()
            if node.tag in {"li", "p"} and node.get("data-reference-id") in reference_ids
        ]
        result.append(
            {
                "paragraph_id": paragraph_id,
                "footnotes": notes,
                "bibliography": bibliography,
            }
        )
    return result


def manifest(
    html: str, protected_ids: set[str] | None = None,
    editable_heading_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Record all structure and all text outside the eligible body text slots."""
    root = _parse(html)
    editable_ids = {p["paragraph_id"] for p in inventory(html, protected_ids, include_empty=True)}
    protected = protected_ids or set()
    mutable: set[tuple[int, str]] = set()
    for paragraph in root.iter("p"):
        if paragraph.get("id") in editable_ids:
            mutable.update(
                (id(_at(paragraph, path)), field) for path, field in _slots(paragraph, protected)
            )
    for heading in root.iter():
        if heading.get("id") in (editable_heading_ids or set()):
            mutable.add((id(heading), "text"))
    rows: list[dict[str, Any]] = []

    def walk(node: ET.Element, path: tuple[int, ...]) -> None:
        row: dict[str, Any] = {
            "path": list(path),
            "tag": node.tag if isinstance(node.tag, str) else "#comment",
            "attributes": dict(sorted(node.attrib.items())),
        }
        for field in ("text", "tail"):
            row[field] = None if (id(node), field) in mutable else getattr(node, field) or ""
        rows.append(row)
        for index, child in enumerate(node):
            walk(child, (*path, index))

    walk(root, ())
    return rows


def exact_rows(rows: object, ids: list[str], label: str) -> list[dict[str, Any]]:
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise NonRetryableFukamiruError(f"Prose redraft {label} has invalid records")
    returned = [str(row.get("paragraph_id") or "") for row in rows]
    if (
        len(returned) != len(ids)
        or len(set(returned)) != len(returned)
        or set(returned) != set(ids)
    ):
        raise NonRetryableFukamiruError(f"Prose redraft {label} changed paragraph IDs or order")
    by_id = {str(row["paragraph_id"]): row for row in rows}
    return [by_id[paragraph_id] for paragraph_id in ids]


def apply_edits(
    html: str, edits: list[dict[str, Any]], *, protected_ids: set[str] | None = None,
    rephrasable_propositions: list[str] | None = None,
    rephrasable_by_paragraph: dict[str, list[str]] | None = None,
) -> tuple[str, dict[str, Any]]:
    source = inventory(html, protected_ids)
    exact_rows(edits, [p["paragraph_id"] for p in source], "edits")
    root = _parse(html)
    targets = {str(p.get("id")): p for p in root.iter("p")}
    changed: list[str] = []
    rephrasable = rephrasable_propositions or []
    for before, edit in zip(source, edits, strict=True):
        paragraph_id = before["paragraph_id"]
        values = edit.get("text_slots")
        if (
            not isinstance(values, list)
            or len(values) != len(before["text_slots"])
            or not all(isinstance(value, str) for value in values)
            or any(re.search(r"</?[A-Za-z][^>]*>", value) for value in values)
        ):
            raise NonRetryableFukamiruError(f"Prose redraft invalid text slots: {paragraph_id}")
        target = targets[paragraph_id]
        for (path, field), value in zip(
            _slots(target, protected_ids or set()), values, strict=True
        ):
            setattr(_at(target, path), field, value)
        text = "".join(target.itertext())
        paragraph_rephrasable = [
            *rephrasable, *(rephrasable_by_paragraph or {}).get(paragraph_id, []),
        ]
        before_literals = _protected_literals(before["visible_text"], paragraph_rephrasable)
        after_literals = _protected_literals(text, paragraph_rephrasable)
        if not text.strip() or any(
            original - revised if index == 1 else original != revised
            for index, (original, revised) in enumerate(
                zip(before_literals, after_literals, strict=True)
            )
        ):
            raise NonRetryableFukamiruError(
                f"Prose redraft removed protected content: {paragraph_id}"
            )
        if values != before["text_slots"]:
            changed.append(paragraph_id)
    revised = ET.tostring(root, encoding="unicode") if changed else html
    if manifest(html, protected_ids) != manifest(revised, protected_ids):
        raise NonRetryableFukamiruError("Prose redraft changed locked DOM structure or content")
    _parse(revised)
    ids = {node.get("id") for node in root.iter() if node.get("id")}
    for anchor in root.iter("a"):
        href = anchor.get("href", "")
        if href.startswith("#") and href[1:] not in ids:
            raise NonRetryableFukamiruError(f"Prose redraft has a broken anchor: {href}")
    return revised, {
        "source_html_sha256": digest(html),
        "target_html_sha256": digest(revised),
        "paragraph_count": len(source),
        "changed_paragraph_ids": changed,
        "structure_unchanged": True,
        "locked_content_unchanged": True,
        "anchors_valid": True,
    }


def apply_edits_recovering(
    html: str, edits: list[dict[str, Any]], *, protected_ids: set[str] | None = None,
    rephrasable_propositions: list[str] | None = None,
    rephrasable_by_paragraph: dict[str, list[str]] | None = None,
) -> tuple[str, dict[str, Any], list[dict[str, str]], list[dict[str, Any]]]:
    """Apply safe paragraph edits and retain the original DOM for bad candidates.

    Each proposed paragraph is validated against the original DOM and all edits
    accepted so far. A malformed text-slot candidate is reverted only for that
    paragraph; the returned HTML is always validated from the original tree.
    """
    source = inventory(html, protected_ids)
    source_ids = [str(item["paragraph_id"]) for item in source]
    rows_by_id: dict[str, dict[str, Any]] = {}
    duplicate_ids: set[str] = set()
    findings: list[dict[str, str]] = []
    allowed = set(source_ids)
    for row in edits if isinstance(edits, list) else []:
        if not isinstance(row, dict):
            findings.append(
                {
                    "paragraph_id": "",
                    "reason": "改稿レコードがオブジェクトではありません。",
                }
            )
            continue
        paragraph_id = str(row.get("paragraph_id") or "")
        if paragraph_id not in allowed:
            findings.append({"paragraph_id": paragraph_id, "reason": "元DOMにない段落IDです。"})
            continue
        if paragraph_id in rows_by_id:
            duplicate_ids.add(paragraph_id)
        rows_by_id[paragraph_id] = row

    effective: list[dict[str, Any]] = [
        {"paragraph_id": item["paragraph_id"], "text_slots": list(item["text_slots"])}
        for item in source
    ]
    positions = {paragraph_id: index for index, paragraph_id in enumerate(source_ids)}
    for paragraph_id in duplicate_ids:
        findings.append({"paragraph_id": paragraph_id, "reason": "改稿レコードが重複しています。"})
    for before in source:
        paragraph_id = str(before["paragraph_id"])
        index = positions[paragraph_id]
        row = rows_by_id.get(paragraph_id)
        if row is None:
            findings.append({"paragraph_id": paragraph_id, "reason": "改稿レコードがありません。"})
            continue
        if paragraph_id in duplicate_ids:
            continue
        candidate = {"paragraph_id": paragraph_id, "text_slots": row.get("text_slots")}
        trial = list(effective)
        trial[index] = candidate
        try:
            apply_edits(
                html, trial, protected_ids=protected_ids,
                rephrasable_propositions=rephrasable_propositions,
                rephrasable_by_paragraph=rephrasable_by_paragraph,
            )
        except Exception as exc:
            findings.append(
                {
                    "paragraph_id": paragraph_id,
                    "reason": (
                        "段落候補を元のDOMへ安全に適用できません: "
                        f"{type(exc).__name__}: {str(exc)[:220]}"
                    ),
                }
            )
            continue
        effective[index] = candidate
    revised, validation = apply_edits(
        html, effective, protected_ids=protected_ids,
        rephrasable_propositions=rephrasable_propositions,
        rephrasable_by_paragraph=rephrasable_by_paragraph,
    )
    return revised, validation, findings, effective


def redraft_issue_hints(diagnoses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pass prose defects to the writer without diagnosis-inferred claim relations."""
    return [
        {
            "paragraph_id": diagnosis["paragraph_id"],
            "issues": [
                {key: value for key, value in issue.items() if key != "context_relation"}
                for issue in diagnosis.get("issues") or []
            ],
        }
        for diagnosis in diagnoses
    ]


def claim_verification_issues(
    originals: list[dict[str, Any]],
    candidates: dict[str, dict[str, Any]],
    verification: dict[str, Any],
    known_basis_ids: set[str] | None = None,
) -> dict[str, list[str]]:
    """Accept traceable interpretation, while rejecting false certainty and citations."""
    ids = [item["paragraph_id"] for item in originals]
    judgments = exact_rows(verification.get("judgments"), ids, "claim verification")
    issues: dict[str, list[str]] = {}
    for original, judgment in zip(originals, judgments, strict=True):
        paragraph_id = original["paragraph_id"]
        source_text = str(original.get("visible_text") or "")
        candidate_text = str(candidates[paragraph_id].get("visible_text") or "")
        reasons: list[str] = []
        changes = judgment.get("changed_claims") or []
        for change in changes:
            candidate_excerpt = str(change.get("candidate_excerpt") or "")
            support_excerpt = str(change.get("original_support_excerpt") or "")
            if not candidate_excerpt or candidate_excerpt not in candidate_text:
                reasons.append("命題照合の候補引用が改稿本文に一致しません。")
            if support_excerpt and support_excerpt not in source_text:
                reasons.append("命題照合の根拠引用が原文に一致しません。")
            relation = change.get("support_relation")
            bases = [str(value) for value in change.get("basis_ids") or []]
            if relation not in {
                "paraphrase", "inferred_from_supplied_material",
                "model_judgment_without_direct_statement", "unsupported",
            }:
                reasons.append("命題の支持関係が不明です。")
            if relation == "paraphrase" and not support_excerpt:
                reasons.append("言い換えを支持する原文の箇所がありません。")
            if relation == "inferred_from_supplied_material" and not bases:
                reasons.append("材料からの推論に根拠IDがありません。")
            if any(value not in (known_basis_ids or set()) for value in bases):
                reasons.append("推論記録に未知の根拠IDがあります。")
            if relation in {
                "inferred_from_supplied_material",
                "model_judgment_without_direct_statement",
            } and not str(change.get("reasoning") or "").strip():
                reasons.append("推論の過程が記録されていません。")
            if change.get("certainty_preserved") is not True:
                reasons.append("原文の問い・仮説・限定より断定が強くなっています。")
            if change.get("citation_alignment_preserved") is not True:
                reasons.append("脚注と主張の対応が変わっています。")
            if relation == "unsupported":
                reasons.append(
                    f"根拠のない命題: {candidate_excerpt or '候補の変更箇所'}。"
                    f" {str(change.get('reason') or judgment.get('reason') or '')}"
                )
        if judgment.get("all_changes_admissible") is not True and not reasons:
            reasons.append(str(judgment.get("reason") or "命題の保持を確認できません。"))
        if reasons:
            issues[paragraph_id] = list(dict.fromkeys(reasons))
    return issues


def candidate_sentences(text: str) -> list[str]:
    """Return complete visible sentences for an exhaustive independent audit."""
    return [part.strip() for part in re.findall(r"[^。！？]+[。！？]?", text) if part.strip()]


def predicate_sentence_issues(
    originals: list[dict[str, Any]],
    candidates: dict[str, dict[str, Any]],
    audit: dict[str, Any],
    verification: dict[str, Any],
) -> dict[str, list[str]]:
    """Require a verdict for every candidate sentence, including omitted claims."""
    ids = [item["paragraph_id"] for item in originals]
    rows = exact_rows(audit.get("judgments"), ids, "predicate sentence audit")
    claim_rows = exact_rows(verification.get("judgments"), ids, "claim verification")
    issues: dict[str, list[str]] = {}
    for original, row, claim_row in zip(originals, rows, claim_rows, strict=True):
        paragraph_id = original["paragraph_id"]
        source_text = str(original.get("visible_text") or "")
        candidate_text = str(candidates[paragraph_id].get("visible_text") or "")
        expected = candidate_sentences(candidate_text)
        sentences = list(row.get("sentences") or [])
        actual = [str(item.get("candidate_sentence") or "") for item in sentences]
        reasons: list[str] = []
        if actual != expected:
            reasons.append("命題監査が改稿本文の全文を文単位で網羅していません。")
        for item in sentences:
            sentence = str(item.get("candidate_sentence") or "")
            predicates = [str(value) for value in item.get("predicate_excerpts") or []]
            if not predicates or any(not value or value not in sentence for value in predicates):
                reasons.append("命題監査の述語引用が候補文に一致しません。")
            support = str(item.get("source_support_excerpt") or "")
            if support and support not in source_text:
                reasons.append("文単位監査の原文引用が一致しません。")
            for key in (
                "actor_object_direction_preserved", "certainty_preserved",
                "citation_alignment_preserved",
            ):
                if item.get(key) is not True:
                    reasons.append(f"文単位監査で{key}を確認できません: {sentence}")
            relation = item.get("relation")
            if relation == "unsupported":
                reasons.append(f"文単位監査で根拠のない命題を検出: {sentence}")
            if relation in {
                "inferred_from_supplied_material",
                "model_judgment_without_direct_statement",
            } and not any(
                change.get("support_relation") == relation
                and str(change.get("candidate_excerpt") or "") in sentence
                for change in claim_row.get("changed_claims") or []
            ):
                reasons.append(f"推論された命題が本文外の記録にありません: {sentence}")
        if reasons:
            issues[paragraph_id] = list(dict.fromkeys(reasons))
    return issues


def inference_records(
    originals: list[dict[str, Any]], verification: dict[str, Any],
    accepted_ids: set[str],
) -> list[dict[str, Any]]:
    """Keep model inference outside article prose and anchor it to an accepted paragraph."""
    judgments = exact_rows(
        verification.get("judgments"), [p["paragraph_id"] for p in originals],
        "claim verification",
    )
    records = []
    for original, judgment in zip(originals, judgments, strict=True):
        paragraph_id = original["paragraph_id"]
        if paragraph_id not in accepted_ids:
            continue
        for index, change in enumerate(judgment.get("changed_claims") or [], 1):
            relation = change.get("support_relation")
            if relation not in {
                "inferred_from_supplied_material",
                "model_judgment_without_direct_statement",
                "unsupported",
            }:
                continue
            records.append({
                "id": f"redraft-{paragraph_id}-{index}",
                "paragraph_id": paragraph_id,
                "section_id": original["section_id"],
                "anchor_excerpt": change["candidate_excerpt"],
                "judgment": change["candidate_excerpt"],
                "original_support_excerpt": change.get("original_support_excerpt") or "",
                "basis_ids": list(change.get("basis_ids") or []),
                "support_relation": relation,
                "reasoning": change.get("reasoning") or change.get("reason") or "要確認",
                "certainty_preserved": change.get("certainty_preserved") is True,
                "citation_alignment_preserved": change.get("citation_alignment_preserved") is True,
            })
    return records


def reader_review_issues(
    paragraphs: list[dict[str, Any]], review: dict[str, Any]
) -> dict[str, list[str]]:
    judgments = exact_rows(
        review.get("judgments"), [p["paragraph_id"] for p in paragraphs],
        "reader review",
    )
    result: dict[str, list[str]] = {}
    for paragraph, judgment in zip(paragraphs, judgments, strict=True):
        spans = list(judgment.get("unnatural_spans") or [])
        for span in spans:
            excerpt = str(span.get("excerpt") or "")
            if not excerpt or excerpt not in str(paragraph.get("visible_text") or ""):
                result.setdefault(paragraph["paragraph_id"], []).append(
                    "読者監査の引用語句が候補本文と一致しません。"
                    + (" 指摘: " + excerpt if excerpt else "")
                    + (" 理由: " + str(span.get("reason") or "")
                       if span.get("reason") else "")
                )
                continue
        if spans:
            result.setdefault(paragraph["paragraph_id"], []).extend(
                "不自然な語の結び付き: " + str(span["excerpt"]) + "。"
                + str(span.get("reason") or "")
                + (" 言い換え案: " + str(span["natural_alternative"])
                   if span.get("natural_alternative") else "")
                for span in spans
                if str(span.get("excerpt") or "") in str(paragraph.get("visible_text") or "")
            )
        if not all(judgment.get(key) is True for key in (
            "natural_for_reader", "argument_progresses", "topic_clear",
        )):
            result.setdefault(paragraph["paragraph_id"], []).append(
                str(judgment.get("reason") or "読者として自然に読めません。")
            )
    if review.get("section_flow_acceptable") is not True:
        for paragraph in paragraphs:
            result.setdefault(paragraph["paragraph_id"], []).append(
                str(review.get("section_reason") or "節全体の流れが不自然です。")
            )
    return result


_DIAGNOSIS_AUDIT_FLAGS = {
    "enumeration": "enumeration_resolved",
    "writer_agency": "writer_agency_clear",
    "japanese_prose": "natural_japanese",
    "analysis_stall": "analysis_progresses",
    "internal_language": "internal_language_absent",
}


def diagnosed_issue_resolution(
    issues: list[dict[str, Any]], audit: dict[str, Any], changed: bool,
) -> list[dict[str, Any]]:
    """Use the independent prose audit, not a candidate's claim of completion."""
    return [{
        "issue_id": str(issue["issue_id"]),
        "category": str(issue["category"]),
        "resolved": changed and audit.get(
            _DIAGNOSIS_AUDIT_FLAGS.get(str(issue["category"]))
        ) is True,
        "evidence": _DIAGNOSIS_AUDIT_FLAGS.get(str(issue["category"])),
    } for issue in issues]


def section_flow_repair_ids(
    outcomes: list[dict[str, Any]], review: dict[str, Any],
) -> set[str]:
    """Target paragraphs with reader defects, or the section boundaries."""
    if review.get("section_flow_acceptable") is not False or not outcomes:
        return set()
    direct = {
        item["paragraph_id"] for item in outcomes
        if any((item.get("reader_review") or {}).get(key) is False for key in (
            "natural_for_reader", "argument_progresses", "topic_clear",
        )) or (item.get("reader_review") or {}).get("unnatural_spans")
    }
    if direct:
        return direct
    return {outcomes[0]["paragraph_id"], outcomes[-1]["paragraph_id"]}


def select_edits(
    paragraphs: list[dict[str, Any]],
    diagnoses: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    audit: dict[str, Any],
    claim_issues: dict[str, list[str]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ids = [p["paragraph_id"] for p in paragraphs]
    exact_rows(diagnoses, ids, "diagnosis")
    exact_rows(candidates, ids, "candidates")
    judgments = exact_rows(audit.get("judgments"), ids, "audit")
    edits: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    for before, diagnosis, candidate, judgment in zip(
        paragraphs, diagnoses, candidates, judgments, strict=True
    ):
        issues = diagnosis.get("issues") or []
        issue_ids = [str(issue.get("issue_id") or "") for issue in issues]
        addressed = candidate.get("addressed_issue_ids") or []
        if (
            any(not value for value in issue_ids)
            or len(set(issue_ids)) != len(issue_ids)
            or len(set(addressed)) != len(addressed)
            or not set(addressed) <= set(issue_ids)
        ):
            raise NonRetryableFukamiruError("Prose redraft has inconsistent diagnosed issue IDs")
        changed = candidate["text_slots"] != before["text_slots"]
        issue_resolution = diagnosed_issue_resolution(issues, judgment, changed)
        unresolved_issue_ids = [
            item["issue_id"] for item in issue_resolution if not item["resolved"]
        ]
        reasons = list(judgment.get("remaining_issues") or [])
        reasons.extend((claim_issues or {}).get(before["paragraph_id"], []))
        if candidate.get("review_reason"):
            reasons.append(str(candidate["review_reason"]))
        if unresolved_issue_ids:
            reasons.append("診断した問題に対応する改稿を確認できませんでした。")
        if not all(
            judgment.get(key) is True
            for key in (
                "content_preserved",
                "enumeration_resolved",
                "writer_agency_clear",
                "footnote_alignment_preserved",
                "natural_japanese",
                "analysis_progresses",
                "internal_language_absent",
            )
        ):
            reasons.append(
                str(judgment.get("reason") or "内容・日本語・脚注対応の監査が不合格です。")
            )
        if audit.get("section_flow_acceptable") is not True and (changed or issue_ids):
            reasons.append(
                str(audit.get("section_reason") or "節全体の文章の流れを確認できませんでした。")
            )
        reasons = list(dict.fromkeys(reasons))
        # A model's editorial disagreement is reviewable, not a reason to
        # restore the old sentence. DOM and protected-content checks run when
        # the selected text is applied below.
        accepted = changed or not reasons
        edits.append(
            {
                "paragraph_id": before["paragraph_id"],
                "text_slots": (candidate["text_slots"] if accepted else before["text_slots"]),
            }
        )
        outcomes.append(
            {
                "paragraph_id": before["paragraph_id"],
                "section_id": before["section_id"],
                "diagnosed_issues": issues,
                "diagnosed_issue_resolution": issue_resolution,
                "unresolved_diagnosed_issue_ids": unresolved_issue_ids,
                "reported_addressed_issue_ids": list(addressed),
                "candidate_review_reason": str(candidate.get("review_reason") or ""),
                "accepted": accepted,
                "changed": changed and accepted,
                "review_status": "marginal" if reasons else "true",
                "categories": sorted({str(issue.get("category")) for issue in issues}),
                "reasons": reasons,
                "audit": judgment,
            }
        )
    if audit.get("section_flow_acceptable") is not True and not any(o["reasons"] for o in outcomes):
        outcomes[0]["review_status"] = "marginal"
        outcomes[0]["reasons"] = [
            str(audit.get("section_reason") or "節全体の品質に要確認事項があります。")
        ]
    return edits, outcomes


def prose_repair_needed(
    outcome: dict[str, Any], semantic_issues: list[str],
) -> bool:
    """Repair concrete defects even when a changed draft stays in the body."""
    audit = outcome.get("audit") or {}
    claim = outcome.get("claim_verification") or {}
    reader = outcome.get("reader_review") or {}
    return bool(
        (not outcome.get("accepted") and outcome.get("diagnosed_issues"))
        or outcome.get("unresolved_diagnosed_issue_ids")
        or outcome.get("candidate_review_reason")
        or semantic_issues
        or audit.get("content_preserved") is False
        or audit.get("footnote_alignment_preserved") is False
        or claim.get("all_changes_admissible") is False
        or any(audit.get(key) is False for key in (
            "enumeration_resolved", "writer_agency_clear", "natural_japanese",
            "analysis_progresses", "internal_language_absent",
        ))
        or bool(audit.get("remaining_issues"))
        or any(reader.get(key) is False for key in (
            "natural_for_reader", "argument_progresses", "topic_clear",
        ))
        or bool(reader.get("unnatural_spans"))
        or outcome.get("section_flow_repair")
    )


def prose_repair_judgment(outcome: dict[str, Any]) -> dict[str, Any]:
    """Store the actual verdict behind each candidate selection."""
    audit = outcome.get("audit") or {}
    claim = outcome.get("claim_verification") or {}
    return {
        "accepted": bool(outcome.get("accepted")),
        "changed": bool(outcome.get("changed")),
        "content_preserved": audit.get("content_preserved"),
        "footnote_alignment_preserved": audit.get("footnote_alignment_preserved"),
        "all_changes_admissible": claim.get("all_changes_admissible"),
        "unresolved_diagnosed_issue_ids": list(
            outcome.get("unresolved_diagnosed_issue_ids") or []
        ),
        "reader_review": {
            key: (outcome.get("reader_review") or {}).get(key) for key in (
                "natural_for_reader", "argument_progresses", "topic_clear",
                "unnatural_spans",
            )
        },
        "section_flow_repair": bool(outcome.get("section_flow_repair")),
        "reasons": list(outcome.get("reasons") or []),
    }


def prose_repair_feedback(
    outcome: dict[str, Any], text_slots: list[str],
    reader_issues: list[str],
) -> dict[str, Any]:
    """Give the repairer concrete defects without replaying whole audit payloads."""
    audit = outcome.get("audit") or {}
    claim = outcome.get("claim_verification") or {}
    return {
        "paragraph_id": outcome["paragraph_id"],
        "candidate_text_slots": text_slots,
        "audit": {
            key: audit.get(key) for key in (
                "content_preserved", "footnote_alignment_preserved",
                "natural_japanese", "internal_language_absent",
            )
        } | {
            "remaining_issues": [str(value)[:240] for value in (
                audit.get("remaining_issues") or []
            )[:4]],
            "reason": str(audit.get("reason") or "")[:320],
        },
        "claim_verification": {
            "all_changes_admissible": claim.get("all_changes_admissible"),
            "changed_claims": [{
                key: item.get(key) for key in (
                    "candidate_excerpt", "support_relation", "certainty_preserved",
                    "citation_alignment_preserved", "reason",
                )
            } for item in (claim.get("changed_claims") or [])[:4]],
        },
        "semantic_issues": [str(value)[:240] for value in (
            outcome.get("semantic_issues") or []
        )[:4]],
        "diagnosed_issues": [{
            key: issue.get(key) for key in ("issue_id", "category", "description")
        } for issue in (outcome.get("diagnosed_issues") or [])[:4]],
        # Every distinct reader finding is actionable. In particular, the
        # section-flow finding is appended after local spans and must not be
        # lost merely because a paragraph has several local defects.
        "reader_issues": list(dict.fromkeys(
            str(value)[:240] for value in reader_issues if str(value).strip()
        )),
    }


def prose_candidate_worth_verifying(
    current: dict[str, Any], trial_audit: dict[str, Any],
    trial_reader: dict[str, Any], *, section_flow_repair: bool,
    changed: bool,
) -> bool:
    """Triage a retry before the costly claim and predicate checks.

    A candidate with no independently observed gain cannot replace the
    current text. Any candidate that can replace it still receives the full
    claim and predicate checks before selection.
    """
    if not changed:
        return False
    if current.get("accepted") is False:
        return True
    before_audit = current.get("audit") or {}
    for key in ("content_preserved", "footnote_alignment_preserved"):
        if before_audit.get(key) is True and trial_audit.get(key) is not True:
            return False
    if any(
        trial_audit.get(key) is True and before_audit.get(key) is not True
        for key in ("content_preserved", "footnote_alignment_preserved")
    ):
        return True
    if (
        (current.get("claim_verification") or {}).get("all_changes_admissible") is False
        or current.get("semantic_issues")
        or current.get("candidate_review_reason")
    ):
        # Only the claim/predicate stages can establish whether these were
        # repaired; a reader-only comparison must not suppress that check.
        return True

    def quality(
        audit: dict[str, Any], reader: dict[str, Any], flow_defect: bool,
    ) -> tuple[int, int]:
        reader_defects = sum(reader.get(key) is False for key in (
            "natural_for_reader", "argument_progresses", "topic_clear",
        )) + len(reader.get("unnatural_spans") or []) + int(flow_defect)
        prose_defects = sum(audit.get(key) is False for key in (
            "enumeration_resolved", "writer_agency_clear", "natural_japanese",
            "analysis_progresses", "internal_language_absent",
        )) + len(audit.get("remaining_issues") or [])
        return reader_defects, prose_defects

    return quality(trial_audit, trial_reader, section_flow_repair) < quality(
        before_audit, current.get("reader_review") or {},
        bool(current.get("section_flow_repair")),
    )


def prefer_repaired_prose(
    current: dict[str, Any], trial: dict[str, Any],
) -> bool:
    """Prefer a checked improvement without losing a passed semantic guard."""
    if not trial.get("accepted"):
        return False
    if current.get("changed") and not trial.get("changed"):
        return False
    before = prose_repair_judgment(current)
    after = prose_repair_judgment(trial)
    guards = (
        "content_preserved", "footnote_alignment_preserved", "all_changes_admissible",
    )
    if any(before[key] is True and after[key] is not True for key in guards):
        return False
    gains = sum(after[key] is True and before[key] is not True for key in guards)
    if gains:
        return True
    if not current.get("accepted"):
        return True
    def quality(row: dict[str, Any]) -> tuple[int, int, int, int]:
        reader = row.get("reader_review") or {}
        reader_defects = sum(reader.get(key) is False for key in (
            "natural_for_reader", "argument_progresses", "topic_clear",
        )) + len(reader.get("unnatural_spans") or [])
        reader_defects += int(row.get("section_flow_repair") is True)
        audit = row.get("audit") or {}
        prose_defects = sum(audit.get(key) is False for key in (
            "enumeration_resolved", "writer_agency_clear", "natural_japanese",
            "analysis_progresses", "internal_language_absent",
        )) + len(audit.get("remaining_issues") or [])
        return (
            len(row.get("unresolved_diagnosed_issue_ids") or []),
            reader_defects,
            prose_defects,
            len(row.get("reasons") or []),
        )

    return quality(trial) < quality(current)
