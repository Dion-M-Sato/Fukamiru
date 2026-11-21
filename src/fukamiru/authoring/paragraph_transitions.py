"""Review paragraph transitions separately from citation quality and prose fluency."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from fukamiru.authoring.evidence_contracts import HumanReviewItem
from fukamiru.errors import NonRetryableFukamiruError
from fukamiru.revisions import prose_redraft as prose
from fukamiru.revisions.footnote_citations import EvidenceFootnoteRenderer

BATCH_SIZE = 8


def inventory(document: str, protected: set[str]) -> tuple[list[dict], list[dict]]:
    paragraphs = prose.inventory(document)
    editable = {p["paragraph_id"] for p in prose.inventory(document, protected)}
    reader_text = prose.reader_paragraph_text(document)
    paragraphs = [{**p, "visible_text": reader_text[p["paragraph_id"]],
                   "editable": p["paragraph_id"] in editable} for p in paragraphs]
    boundaries = []
    for left, right in zip(paragraphs, paragraphs[1:], strict=False):
        key = left["paragraph_id"] + "\n" + right["paragraph_id"]
        boundaries.append({
            "boundary_id": "transition:" + hashlib.sha256(key.encode()).hexdigest()[:20],
            "left_paragraph_id": left["paragraph_id"],
            "right_paragraph_id": right["paragraph_id"],
            "crosses_section": left["section_id"] != right["section_id"],
        })
    return paragraphs, boundaries


def validate_result(payload: dict, result: dict) -> dict:
    """Reject incomplete coverage, invented evidence and out-of-scope edits before caching."""
    expected = {b["boundary_id"]: b for b in payload["boundaries"]}
    rows = result.get("boundaries")
    if (not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows)
            or len(rows) != len(expected)
            or {r.get("boundary_id") for r in rows} != set(expected)):
        raise NonRetryableFukamiruError("Paragraph transition review changed boundary IDs")
    paragraphs = {p["paragraph_id"]: p for p in payload["paragraphs"]}
    for row in rows:
        boundary = expected[row["boundary_id"]]
        for side in ("left", "right"):
            paragraph_id = boundary[side + "_paragraph_id"]
            evidence = row.get(side + "_evidence")
            if (row.get(side + "_paragraph_id") != paragraph_id or not evidence
                    or evidence not in paragraphs[paragraph_id]["visible_text"]):
                raise NonRetryableFukamiruError("Paragraph transition has invalid text evidence")
        connective = row.get("connective") or ""
        if connective not in paragraphs[boundary["right_paragraph_id"]]["visible_text"]:
            raise NonRetryableFukamiruError("Paragraph transition invented a connective")
    edits = result.get("paragraphs")
    if (not isinstance(edits, list) or not all(isinstance(e, dict) for e in edits)
            or not isinstance(result.get("meaning_preserved"), bool)):
        raise NonRetryableFukamiruError("Paragraph transition has an invalid result")
    allowed = {r["right_paragraph_id"] for r in rows
               if not (r["central_term_continuity"] and r["connective_appropriate"])}
    ids = [e.get("paragraph_id") for e in edits]
    if (len(set(ids)) != len(ids) or not set(ids) <= allowed
            or any(not paragraphs[i]["editable"] for i in ids)
            or (payload["mode"] == "verify" and edits)):
        raise NonRetryableFukamiruError("Paragraph transition edited an unrelated paragraph")
    for edit in edits:
        before = paragraphs[edit["paragraph_id"]]["text_slots"]
        after = edit.get("text_slots")
        if not isinstance(after, list) or len(after) != len(before):
            raise NonRetryableFukamiruError("Paragraph transition changed text slot count")
        closed = False
        for original, revised in zip(before, after, strict=True):
            if not isinstance(revised, str) or (closed and original != revised):
                raise NonRetryableFukamiruError("Paragraph transition changed later sentences")
            end = re.search(r"[。！？]", original)
            if not closed and end:
                suffix = original[end.end():]
                if suffix and not revised.endswith(suffix):
                    raise NonRetryableFukamiruError("Paragraph transition changed later sentences")
                closed = True
    return {**result, "boundaries": sorted(
        rows, key=lambda r: list(expected).index(r["boundary_id"])
    )}


def run(worker: Any, claimed: Any, context: dict, document: str,
        protected: set[str]) -> tuple[str, dict, list[dict]]:
    paragraphs, boundaries = inventory(document, protected)
    source_by_id = {p["paragraph_id"]: p for p in paragraphs}

    def review(candidate: str, mode: str) -> tuple[list[dict], list[dict], list[dict]]:
        current, current_boundaries = inventory(candidate, protected)
        if current_boundaries != boundaries:
            raise NonRetryableFukamiruError(
                "Paragraph transition repair changed paragraph boundaries"
            )
        reviews, edits, meaning = [], [], []
        for index in range(0, len(boundaries), BATCH_SIZE):
            batch = boundaries[index:index + BATCH_SIZE]
            ids = {b[side + "_paragraph_id"] for b in batch for side in ("left", "right")}
            payload = {
                "output_language": "ja", "mode": mode, "boundaries": batch,
                "paragraphs": [p for p in current if p["paragraph_id"] in ids],
                "original_paragraphs": [
                    {"paragraph_id": p["paragraph_id"], "visible_text": p["visible_text"]}
                    for p in paragraphs if p["paragraph_id"] in ids
                ] if mode == "verify" else [],
            }
            result = worker._authoring_invoke(
                claimed, stage="review_paragraph_transitions_v32", context=context, payload=payload,
                operation_name=f"paragraph_transitions:{mode}:{index // BATCH_SIZE}",
            )
            result = validate_result(payload, result)
            reviews.extend(result["boundaries"])
            edits.extend(result["paragraphs"])
            meaning.append({"batch": index // BATCH_SIZE,
                            "meaning_preserved": result["meaning_preserved"],
                            "reason": result.get("meaning_reason", "")})
        return reviews, edits, meaning

    initial, edits, _ = review(document, "review")
    revised = document
    final = initial
    meaning_checks = []
    rejected = False
    if edits:
        by_id = {edit["paragraph_id"]: edit for edit in edits}
        all_edits = [{"paragraph_id": p["paragraph_id"],
                      "text_slots": by_id.get(p["paragraph_id"], p)["text_slots"]}
                     for p in prose.inventory(document, protected)]
        candidate, _ = prose.apply_edits(document, all_edits, protected_ids=protected)
        candidate, unaligned = EvidenceFootnoteRenderer.align_existing_markers_before_punctuation(
            candidate
        )
        if unaligned:
            raise NonRetryableFukamiruError("Paragraph transition left an unaligned footnote")
        final, _, meaning_checks = review(candidate, "verify")
        if all(check["meaning_preserved"] for check in meaning_checks):
            revised = candidate
        else:
            rejected = True
            final = initial
    unresolved = [r for r in final
                  if not (r["central_term_continuity"] and r["connective_appropriate"])]
    human = []
    for row in unresolved:
        paragraph = source_by_id[row["right_paragraph_id"]]
        human.append(HumanReviewItem.model_validate({
            "review_item_id": row["boundary_id"], "section_id": paragraph["section_id"],
            "sentence_id": paragraph["paragraph_id"], "category": "paragraph_transition",
            "reason": row["reason"] + (
                " 冒頭文の修正は意味保存の再検査に通らず採用しませんでした。" if rejected else ""
            ),
            "source_ids": [], "recommended_check": "前段の中心語と、付加・対比などの関係を確認",
            "blocks_render": False, "status": "open",
        }).model_dump(mode="json"))
    changed = [p["paragraph_id"] for p in prose.inventory(revised, protected)
               if p["text_slots"] != source_by_id[p["paragraph_id"]]["text_slots"]]
    report = {
        "policy": "v32-paragraph-central-term-and-discourse-relation-v1",
        "status": "needs_human_review" if unresolved else "passed",
        "source_html_sha256": prose.digest(document), "target_html_sha256": prose.digest(revised),
        "boundary_count": len(boundaries),
        "cross_section_boundary_count": sum(b["crosses_section"] for b in boundaries),
        "initial_reviews": initial, "final_reviews": final,
        "meaning_checks": meaning_checks, "rejected_meaning_change": rejected,
        "changed_paragraph_ids": changed, "unresolved": unresolved,
    }
    return revised, report, human
