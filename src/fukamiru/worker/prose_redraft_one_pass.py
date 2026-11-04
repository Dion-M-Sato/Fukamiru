"""One-pass V31 prose editing with deterministic HTML safeguards.

The model sees the source prose and its section evidence once.  It does not
receive a post-generation audit or a second opportunity to rewrite the same
section; unresolved editorial questions stay visible in the report.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any

from fukamiru.documents.japanese_surface import JapaneseSurfaceQualityGate
from fukamiru.errors import NonRetryableFukamiruError, RunCancelled
from fukamiru.revisions import ManuscriptDiscourseService
from fukamiru.revisions import prose_redraft as prose
from fukamiru.revisions.footnote_citations import EvidenceFootnoteRenderer
from fukamiru.workflow.identity import is_manuscript_generator_workflow

_WRITER_ATTRIBUTION = re.compile(r"筆者(?:が|は)『[^。]{1,240}?』で論じた")


def _section_material(
    worker: Any, section_id: str, paragraph_ids: set[str], context: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, str]]:
    from fukamiru.worker.runner import WorkerRunner

    source_section_id = WorkerRunner._authoring_redraft_section_key_v31(
        section_id, context, paragraph_ids
    )
    cards = context.get("authoring_compact_claim_citation_v31") or {}
    compact = dict(cards.get(source_section_id) or {})
    resolution = {
        "section_id": section_id,
        "source_section_id": source_section_id,
        "source": "compact_card" if compact else "legacy_projection",
        "reason": "" if compact else (
            "保存済みの短い命題・引用カードと節を一意に対応付けられませんでした。"
            if cards else "短い命題・引用カードが保存されていません。"
        ),
    }
    inferences = [
        item for item in context.get("authoring_inference_ledger_v31") or []
        if isinstance(item, dict) and item.get("section_id") == source_section_id
    ]
    if compact:
        # The fully cited source prose is already present in this final edit.
        # Bibliographic-only citation records and verification flags are audit
        # metadata; giving them to the editor makes it recast used scholarship
        # as a prospective reference.
        unverified_citation_ids = {
            str(anchor.get("evidence_id") or "")
            for anchor in compact.get("citation_anchors") or []
            if anchor.get("substantive_support_verified") is False
        }
        citation_source_ids = {
            str(source_id)
            for item in compact.get("evidence") or []
            if str(item.get("id") or "") in unverified_citation_ids
            for source_id in item.get("source_ids") or []
            if str(source_id).startswith("annotation-citation:")
        }
        writer_material = dict(compact)
        for key in ("claims", "evidence", "warrants"):
            writer_material[key] = [
                {field: value for field, value in item.items()
                 if field != "verification_status"}
                for item in compact.get(key) or []
                if not (
                    key == "evidence"
                    and (
                        str(item.get("id") or "") in unverified_citation_ids
                        or (
                            str(item.get("id") or "").startswith(
                                "annotation-citation-node:"
                            )
                            and citation_source_ids.intersection(
                                str(value) for value in item.get("source_ids") or []
                            ))
                    )
                )
            ]
        writer_material["citation_anchors"] = [
            {field: value for field, value in anchor.items()
             if field != "substantive_support_verified"}
            for anchor in compact.get("citation_anchors") or []
        ]
        return {**writer_material, "draft_inference_ledger": inferences}, resolution
    contract = next((
        item for item in context.get("section_contracts") or []
        if isinstance(item, dict)
        and str(item.get("section_id") or item.get("id") or "") == source_section_id
    ), {})
    claim_ids = set(contract.get("claim_ids") or [])
    argument_ids = claim_ids | set(contract.get("evidence_ids") or []) | set(
        contract.get("warrant_ids") or []
    )
    proposition_ids = {
        str(value) for item in inferences for value in item.get("basis_ids") or []
    } | {str(value) for value in contract.get("source_proposition_ids") or []}
    return {
        "research_problem": context.get("research_problem") or {},
        "section_contract": contract,
        "propositions": [
            item for item in context.get("propositions") or []
            if item.get("id") in proposition_ids
        ],
        "claims": [
            item for item in context.get("claims") or []
            if item.get("id") in claim_ids
        ],
        "argument_nodes": [
            item for item in context.get("argument_nodes") or []
            if item.get("id") in argument_ids
            or item.get("claim_id") in claim_ids
        ],
        "draft_inference_ledger": inferences,
    }, resolution


def _compose_paragraphs(
    section_paragraphs: list[dict[str, Any]], rephrasable: list[str]
) -> list[dict[str, Any]]:
    blind = prose.source_blind_paragraphs(
        section_paragraphs, rephrasable_propositions=rephrasable
    )
    return [
        {
            **structure,
            "source_text_slots": list(paragraph["text_slots"]),
            "diagnosed_issues": [],
            "writer_attribution_anchors": [
                match.group(0)
                for slot in paragraph["text_slots"]
                for match in _WRITER_ATTRIBUTION.finditer(slot)
            ],
        }
        for paragraph, structure in zip(section_paragraphs, blind, strict=True)
    ]


def _outcome(
    before: dict[str, Any], after_slots: list[str], *,
    reasons: list[str] | None = None, accepted: bool = True,
) -> dict[str, Any]:
    changed = after_slots != before["text_slots"]
    reasons = list(dict.fromkeys(reasons or []))
    return {
        "paragraph_id": before["paragraph_id"],
        "section_id": before["section_id"],
        "accepted": accepted,
        "changed": changed,
        "review_status": "marginal" if reasons else "not_audited",
        "categories": [],
        "reasons": reasons,
        "audit": {"content_preserved": None, "footnote_alignment_preserved": None},
        "reported_addressed_issue_ids": [],
        "repair_attempts": [],
    }


def run(worker: Any, claimed: Any, context: dict[str, Any]) -> dict[str, Any]:
    if str(claimed.workflow_version) not in {"31", "32"} or not is_manuscript_generator_workflow(
        claimed.workflow_name
    ):
        raise NonRetryableFukamiruError("Rendered body prose redrafting requires V31 or V32")
    original = str(context.get("proposed_html") or "")
    rendered = context.get("reference_rendered_html_v26")
    if rendered is not None and rendered != original:
        raise NonRetryableFukamiruError("Prose redraft received a stale rendered HTML snapshot")
    if context.get("final_quality_sha256") not in (None, prose.digest(original)):
        raise NonRetryableFukamiruError("Prose redraft received a stale candidate digest")

    protected = set(worker._string_list(context.get("source_preserve_block_ids")))
    protected.update(worker._string_list(context.get("human_protected_block_ids")))
    rephrasable = [
        str(value) for value in (
            dict(context.get("research_problem") or {}).get("research_question"),
            dict(context.get("conclusion_synthesis") or {}).get("final_answer"),
        ) if value
    ]
    output_root = (
        worker.settings.paths.runtime_root / "outputs" / claimed.run_id
        / "prose-redraft" / claimed.node_run_id
    )
    output_root.mkdir(parents=True, exist_ok=True)
    original_path = output_root / "authoring.prose-redraft.original.html"
    if original_path.exists() and original_path.read_text(encoding="utf-8") != original:
        raise NonRetryableFukamiruError("Prose redraft saved original differs from node input")
    original_path.write_text(original, encoding="utf-8")
    paragraphs = prose.inventory(original, protected)
    full_inventory = prose.inventory(original)
    source_manifest = prose.manifest(original, protected)
    manifest_path = output_root / "authoring.prose-redraft.dom.json"
    manifest_path.write_text(
        json.dumps(source_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    original_id = worker._register_artifact(
        claimed, original_path, "authoring_prose_redraft_original", "日本語再起草前のHTML"
    )
    manifest_id = worker._register_artifact(
        claimed, manifest_path, "authoring_prose_redraft_dom", "再起草前のDOM記録",
        media_type="application/json",
    )

    by_section: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for paragraph in paragraphs:
        by_section[paragraph["section_id"]].append(paragraph)
    sections = list(by_section.items()) if str(
        context.get("output_language") or context.get("language") or "ja"
    ) == "ja" else []
    selected = {
        item["paragraph_id"]: {
            "paragraph_id": item["paragraph_id"],
            "text_slots": list(item["text_slots"]),
        }
        for item in paragraphs
    }
    outcomes: list[dict[str, Any]] = []
    section_reviews: list[dict[str, Any]] = []
    material_resolutions: list[dict[str, str]] = []
    citation_review_sections: set[str] = set()

    for index, (section_id, section_paragraphs) in enumerate(sections):
        ids = [item["paragraph_id"] for item in section_paragraphs]
        material, resolution = _section_material(worker, section_id, set(ids), context)
        material_resolutions.append(resolution)
        source_card = dict(
            (context.get("authoring_compact_claim_citation_v31") or {}).get(
                resolution["source_section_id"]
            ) or {}
        )
        if str(claimed.workflow_version) == "32" and any(
            anchor.get("substantive_support_verified") is False
            for anchor in source_card.get("citation_anchors") or []
        ):
            citation_review_sections.add(section_id)
        citation_bindings = [
            item for item in context.get("authoring_paragraph_citation_lineage") or []
            if str(item.get("output_block_id") or item.get("block_id") or "") in ids
        ]
        payload = {
            "output_language": "ja",
            "section_id": section_id,
            "paragraphs": _compose_paragraphs(section_paragraphs, rephrasable),
            "composition_strategy": "single_pass",
            "section_scope": {
                key: str(material.get(key) or "")
                for key in ("section_result", "framing")
            },
            "supplied_material": material,
            "previous_section_end": (
                sections[index - 1][1][-1]["visible_text"][-240:] if index else ""
            ),
            "next_section_start": (
                sections[index + 1][1][0]["visible_text"][:240]
                if index + 1 < len(sections) else ""
            ),
            "fixed_citation_context": prose.citation_context(original, ids),
            "citation_bindings": citation_bindings,
        }
        try:
            generated = worker._authoring_invoke(
                claimed, stage="compose_body_prose_v31", context=context,
                payload=payload, operation_name="compose_body_prose:" + section_id,
            )
            rows = prose.exact_rows(generated.get("paragraphs"), ids, "one-pass prose")
            candidate_by_id = {}
            for before, row in zip(section_paragraphs, rows, strict=True):
                slots = prose.restore_writer_attribution(
                    before["text_slots"], list(row["text_slots"])
                )
                slots = [
                    JapaneseSurfaceQualityGate.normalize_quoted_enumerations(slot)[0]
                    for slot in slots
                ]
                candidate_by_id[before["paragraph_id"]] = {
                    "paragraph_id": before["paragraph_id"],
                    "text_slots": slots,
                    "review_reason": str(row.get("review_reason") or ""),
                }
            proposed = {**selected, **candidate_by_id}
            _, _, findings, safe_edits = prose.apply_edits_recovering(
                original, [proposed[item["paragraph_id"]] for item in paragraphs],
                protected_ids=protected, rephrasable_propositions=rephrasable,
            )
            safe_by_id = {item["paragraph_id"]: item for item in safe_edits}
            reasons_by_id: dict[str, list[str]] = defaultdict(list)
            for finding in findings:
                reasons_by_id[str(finding.get("paragraph_id") or "")].append(
                    str(finding.get("reason") or "DOMへ安全に適用できませんでした。")
                )
            for before in section_paragraphs:
                paragraph_id = before["paragraph_id"]
                safe = safe_by_id[paragraph_id]
                selected[paragraph_id] = safe
                reasons = list(reasons_by_id.get(paragraph_id, []))
                if candidate_by_id[paragraph_id]["review_reason"]:
                    reasons.append(candidate_by_id[paragraph_id]["review_reason"])
                outcomes.append(_outcome(
                    before, safe["text_slots"], reasons=reasons,
                    accepted=not bool(reasons_by_id.get(paragraph_id)),
                ))
            section_reviews.append({
                "section_id": section_id, "status": "single_pass",
                "composition_call_count": 1,
                "changed_paragraph_ids": [
                    item["paragraph_id"] for item in section_paragraphs
                    if selected[item["paragraph_id"]]["text_slots"] != item["text_slots"]
                ],
            })
        except RunCancelled:
            raise
        except Exception as exc:
            reason = (
                "一度の起草を適用できなかったため元稿を保持しました。"
                f" {type(exc).__name__}: {str(exc)[:220]}"
            )
            outcomes.extend(
                _outcome(item, list(item["text_slots"]), reasons=[reason], accepted=False)
                for item in section_paragraphs
            )
            section_reviews.append({
                "section_id": section_id, "status": "continued_with_original_candidate",
                "failed_stage": "single_pass_composition", "reason": reason,
                "composition_call_count": 1,
            })

    revised, validation, final_findings, _ = prose.apply_edits_recovering(
        original, [selected[item["paragraph_id"]] for item in paragraphs],
        protected_ids=protected, rephrasable_propositions=rephrasable,
    )
    from fukamiru.worker.runner import WorkerRunner

    def internal_scaffold_phrases(document: str) -> dict[str, set[str]]:
        findings = WorkerRunner._authoring_reader_facing_v31_findings(
            WorkerRunner._authoring_reader_facing_sections_from_html(document)
        )
        phrases: dict[str, set[str]] = defaultdict(set)
        for finding in findings:
            if finding.get("code") != "reader_facing_internal_scaffold":
                continue
            paragraph_id = str(finding.get("paragraph_id") or "")
            phrases[paragraph_id].update(
                str(phrase) for phrase in finding.get("matched_phrases") or []
            )
        return phrases

    source_scaffold = internal_scaffold_phrases(original)
    candidate_scaffold = internal_scaffold_phrases(revised)
    original_by_id = {item["paragraph_id"]: item for item in paragraphs}
    reverted_scaffold_candidates = []
    for paragraph_id, phrases in candidate_scaffold.items():
        introduced = sorted(phrases - source_scaffold.get(paragraph_id, set()))
        if not introduced or paragraph_id not in selected:
            continue
        selected[paragraph_id] = {
            "paragraph_id": paragraph_id,
            "text_slots": list(original_by_id[paragraph_id]["text_slots"]),
        }
        reverted_scaffold_candidates.append({
            "paragraph_id": paragraph_id,
            "introduced_phrases": introduced,
            "reason": "改稿が読者向け本文へ作業用語を新たに加えたため元稿を保持しました。",
        })
    if reverted_scaffold_candidates:
        revised, validation, final_findings, _ = prose.apply_edits_recovering(
            original, [selected[item["paragraph_id"]] for item in paragraphs],
            protected_ids=protected, rephrasable_propositions=rephrasable,
        )
    if str(claimed.workflow_version) == "32":
        revised, unaligned = (
            EvidenceFootnoteRenderer.align_existing_markers_before_punctuation(revised)
        )
        if unaligned:
            source_by_id = {item["paragraph_id"]: item for item in paragraphs}
            for paragraph_id in unaligned:
                if paragraph_id not in source_by_id:
                    raise NonRetryableFukamiruError(
                        "V32 footnote marker has no editable paragraph: " + paragraph_id
                    )
                selected[paragraph_id] = {
                    "paragraph_id": paragraph_id,
                    "text_slots": source_by_id[paragraph_id]["text_slots"],
                }
                final_findings.append({
                    "paragraph_id": paragraph_id,
                    "reason": "改稿後に脚注を句読点の直前へ配置できず、元の段落を保持しました。",
                })
            revised, validation, retry_findings, _ = prose.apply_edits_recovering(
                original, [selected[item["paragraph_id"]] for item in paragraphs],
                protected_ids=protected, rephrasable_propositions=rephrasable,
            )
            final_findings.extend(retry_findings)
            revised, unaligned = (
                EvidenceFootnoteRenderer.align_existing_markers_before_punctuation(revised)
            )
            if unaligned:
                raise NonRetryableFukamiruError(
                    "V32 source footnotes are not adjacent to punctuation"
                )
    for finding in final_findings:
        paragraph_id = str(finding.get("paragraph_id") or "")
        outcome = next((
            item for item in outcomes if item["paragraph_id"] == paragraph_id
        ), None)
        if outcome is not None:
            outcome["accepted"] = False
            outcome["changed"] = False
            outcome["reasons"].append(str(finding.get("reason") or "DOM適用エラー"))
            outcome["review_status"] = "marginal"
    boundary_lock = context.get("paragraph_boundary_lock")
    if boundary_lock and not ManuscriptDiscourseService.validate_rendered_boundary_lock(
        revised, dict(boundary_lock)
    )["valid"]:
        revised, validation = prose.apply_edits(original, [
            {"paragraph_id": item["paragraph_id"], "text_slots": item["text_slots"]}
            for item in paragraphs
        ], protected_ids=protected, rephrasable_propositions=rephrasable)
        for outcome in outcomes:
            outcome["accepted"] = False
            outcome["changed"] = False
            outcome["review_status"] = "marginal"
            outcome["reasons"].append("段落境界契約に合わず元HTMLを保持しました。")
    citation_reader_reviews: list[dict[str, Any]] = []
    reverted_reader_candidates: list[dict[str, Any]] = []
    reviewed_inventory = {
        item["paragraph_id"]: item for item in prose.inventory(revised, protected)
    }
    for section_id, section_paragraphs in sections:
        if section_id not in citation_review_sections:
            continue
        source_section_id = next(
            item["source_section_id"] for item in material_resolutions
            if item["section_id"] == section_id
        )
        source_card = dict(
            (context.get("authoring_compact_claim_citation_v31") or {}).get(
                source_section_id
            ) or {}
        )
        unverified_citation_ids = {
            str(anchor.get("evidence_id") or "")
            for anchor in source_card.get("citation_anchors") or []
            if anchor.get("substantive_support_verified") is False
        }
        bound_paragraph_ids = {
            str(binding.get("output_block_id") or binding.get("block_id") or "")
            for binding in context.get("authoring_paragraph_citation_lineage") or []
            if unverified_citation_ids.intersection(
                str(value) for value in (
                    binding.get("argument_evidence_node_ids") or []
                )
            )
        }
        section_ids = [item["paragraph_id"] for item in section_paragraphs]
        citation_by_id = {
            item["paragraph_id"]: item
            for item in prose.citation_context(original, section_ids)
        }
        target_ids = [
            item["paragraph_id"] for item in section_paragraphs
            if reviewed_inventory[item["paragraph_id"]]["text_slots"] != item["text_slots"]
            and (not bound_paragraph_ids or item["paragraph_id"] in bound_paragraph_ids)
            and (
                citation_by_id[item["paragraph_id"]]["footnotes"]
                or citation_by_id[item["paragraph_id"]]["bibliography"]
            )
        ]
        if not target_ids:
            continue
        payload = {
            "output_language": "ja",
            "section_id": section_id,
            "paragraphs": [
                {"paragraph_id": paragraph_id,
                 "visible_text": reviewed_inventory[paragraph_id]["visible_text"]}
                for paragraph_id in target_ids
            ],
            "candidate_context_paragraphs": [
                {"paragraph_id": paragraph_id,
                 "visible_text": reviewed_inventory[paragraph_id]["visible_text"]}
                for paragraph_id in section_ids
            ],
        }
        try:
            reviewed = worker._authoring_invoke(
                claimed, stage="review_reader_prose_v31", context=context,
                payload=payload, operation_name="review_cited_prose:" + section_id,
            )
            judgments = prose.exact_rows(
                reviewed.get("judgments"), target_ids, "citation reader review"
            )
            section_flow_acceptable = reviewed.get("section_flow_acceptable") is True
            rejected = [
                str(row["paragraph_id"]) for row in judgments
                if not section_flow_acceptable
                or row.get("natural_for_reader") is not True
                or row.get("argument_progresses") is not True
                or row.get("topic_clear") is not True
                or row.get("unnatural_spans")
            ]
            citation_reader_reviews.append({
                "section_id": section_id,
                "target_paragraph_ids": target_ids,
                "rejected_paragraph_ids": rejected,
                "section_flow_acceptable": section_flow_acceptable,
                "status": "reviewed",
            })
        except RunCancelled:
            raise
        except Exception as exc:
            rejected = target_ids
            citation_reader_reviews.append({
                "section_id": section_id,
                "target_paragraph_ids": target_ids,
                "rejected_paragraph_ids": rejected,
                "section_flow_acceptable": None,
                "status": "review_unavailable",
                "reason": f"{type(exc).__name__}: {str(exc)[:220]}",
            })
        for paragraph_id in rejected:
            selected[paragraph_id] = {
                "paragraph_id": paragraph_id,
                "text_slots": list(original_by_id[paragraph_id]["text_slots"]),
            }
            reverted_reader_candidates.append({
                "paragraph_id": paragraph_id,
                "reason": "引用段落の読後評価に合格せず元稿を保持しました。",
            })
    if reverted_reader_candidates:
        revised, validation, final_findings, _ = prose.apply_edits_recovering(
            original, [selected[item["paragraph_id"]] for item in paragraphs],
            protected_ids=protected, rephrasable_propositions=rephrasable,
        )
        if str(claimed.workflow_version) == "32":
            revised, unaligned = (
                EvidenceFootnoteRenderer.align_existing_markers_before_punctuation(revised)
            )
            if unaligned:
                raise NonRetryableFukamiruError(
                    "V32 cited prose rollback left an unaligned footnote marker"
                )
    if prose.manifest(original, protected) != prose.manifest(revised, protected):
        raise NonRetryableFukamiruError("Prose redraft changed locked DOM content")

    static_findings = list(JapaneseSurfaceQualityGate.find_canonical_html(revised))
    scaffold_findings = WorkerRunner._authoring_reader_facing_v31_findings(
        WorkerRunner._authoring_reader_facing_sections_from_html(revised)
    )
    after = {item["paragraph_id"]: item for item in prose.inventory(revised, protected)}
    before_by_id = {item["paragraph_id"]: item for item in paragraphs}
    for outcome in outcomes:
        before = before_by_id[outcome["paragraph_id"]]
        outcome["changed"] = after[outcome["paragraph_id"]]["text_slots"] != before["text_slots"]
    for section_review in section_reviews:
        section_review["changed_paragraph_ids"] = [
            item["paragraph_id"] for item in paragraphs
            if item["section_id"] == section_review["section_id"]
            and after[item["paragraph_id"]]["text_slots"] != item["text_slots"]
        ]
    validation["changed_paragraph_ids"] = [
        item["paragraph_id"] for item in paragraphs
        if after[item["paragraph_id"]]["text_slots"] != item["text_slots"]
    ]
    validation["target_html_sha256"] = prose.digest(revised)
    unresolved = [
        item for item in outcomes if not item["accepted"] or item["reasons"]
    ]
    unresolved.extend({
        "paragraph_id": item["paragraph_id"],
        "section_id": original_by_id[item["paragraph_id"]]["section_id"],
        "reasons": [
            "引用段落の改稿候補を元稿へ戻しました。元稿の日本語と引用の役割を確認してください。"
        ],
    } for item in reverted_reader_candidates)
    section_id_by_paragraph = {
        item["paragraph_id"]: item["section_id"] for item in full_inventory
    }
    for finding in static_findings:
        paragraph_id = str(finding.get("block_id") or "")
        reasons = [str(issue.get("code") or issue.get("message") or "要確認")
                   for issue in finding.get("issues") or []]
        unresolved.append({
            "paragraph_id": paragraph_id,
            "section_id": section_id_by_paragraph.get(paragraph_id, "manuscript"),
            "reasons": ["改稿後の決定的な日本語検査: " + ", ".join(reasons)],
            "static_finding": finding,
        })
    for finding in scaffold_findings:
        paragraph_id = str(finding.get("paragraph_id") or finding.get("object_id") or "")
        unresolved.append({
            "paragraph_id": paragraph_id,
            "section_id": section_id_by_paragraph.get(
                paragraph_id, str(finding.get("object_id") or "manuscript")
            ),
            "reasons": [str(finding.get("message") or "内部語が残っています。")],
            "static_finding": finding,
        })
    changes = [
        {
            **outcome,
            "before": before_by_id[outcome["paragraph_id"]]["visible_text"],
            "after": after[outcome["paragraph_id"]]["visible_text"],
        }
        for outcome in outcomes if outcome["changed"]
    ]
    revised_path = output_root / "authoring.prose-redraft.revised.html"
    revised_path.write_text(revised, encoding="utf-8")
    revised_id = worker._register_artifact(
        claimed, revised_path, "authoring_prose_redraft_revised", "一回改稿後のHTML"
    )
    report = {
        "policy": "v31-rendered-dom-prose-redraft-single-pass-v1",
        **validation,
        "status": (
            "skipped_non_japanese" if not sections else
            "completed_with_review" if unresolved else "completed"
        ),
        "quality_assessment": (
            "citation_reader_review_unavailable" if any(
                item["status"] == "review_unavailable"
                for item in citation_reader_reviews
            ) else "citation_reader_review_only" if citation_reader_reviews
            else "not_llm_audited"
        ),
        "japanese_quality_passed": False if static_findings else None,
        "content_preservation_passed": None,
        "candidate_audit_all_passed": None,
        "reviewed_paragraph_count": sum(
            len(item["target_paragraph_ids"]) for item in citation_reader_reviews
            if item["status"] == "reviewed"
        ),
        "outcomes": outcomes, "changes": changes, "unresolved": unresolved,
        "reverted_scaffold_candidates": reverted_scaffold_candidates,
        "citation_reader_reviews": citation_reader_reviews,
        "reverted_reader_candidates": reverted_reader_candidates,
        "inference_ledger": [], "section_reviews": section_reviews,
        "static_findings": static_findings,
        "material_resolutions": material_resolutions,
        "natural_polish_reviews": [], "internal_scaffold_repairs": [],
        "final_naturalness_reviewed_count": sum(
            len(item["target_paragraph_ids"]) for item in citation_reader_reviews
            if item["status"] == "reviewed"
        ),
        "final_naturalness_issue_count": len(reverted_reader_candidates),
        "heading_reviews": [], "changed_heading_ids": [], "quote_reviews": [],
        "protected_paragraph_ids": [
            item["paragraph_id"] for item in full_inventory
            if item["paragraph_id"] not in selected
        ],
        "original_path": str(original_path), "revised_path": str(revised_path),
        "dom_manifest_path": str(manifest_path),
        "original_artifact_id": original_id, "revised_artifact_id": revised_id,
        "dom_manifest_artifact_id": manifest_id,
        "representative_examples": {category: [
            change for change in changes if category in change["categories"]
        ][:3] for category in ("enumeration", "writer_agency")},
    }
    report_path = output_root / "authoring.prose-redraft.report.json"
    report["report_path"] = str(report_path)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report_artifact_id"] = worker._register_artifact(
        claimed, report_path, "authoring_prose_redraft_report", "日本語改稿の報告",
        media_type="application/json",
    )
    human_items = [{
        "review_item_id": "prose-redraft:" + item["paragraph_id"],
        "section_id": item["section_id"],
        "sentence_id": item["paragraph_id"],
        "category": "japanese_prose_redraft",
        "reason": " / ".join(item["reasons"]),
        "source_ids": [],
        "recommended_check": "本文の意味、引用、脚注、日本語を確認",
        "blocks_render": False, "status": "open",
    } for item in unresolved if item.get("paragraph_id")]
    return {
        **context,
        "proposed_html": revised,
        "reference_rendered_html_v26": revised,
        "final_quality_sha256": prose.digest(revised),
        "prose_redraft_report_v31": report,
        "prose_redraft_inference_ledger_v31": [],
        "manuscript_surface_quality_gate": {
            "gate": "read-only-after-rendered-prose-redraft",
            "passed": True,
            "review_required": bool(static_findings or scaffold_findings),
            "remaining_issue_blocks": static_findings,
            "remaining_issue_count": sum(
                len(item.get("issues") or []) for item in static_findings
            ),
            "target_html_sha256": prose.digest(revised),
        },
        "human_review_items": [
            *list(context.get("human_review_items") or []), *human_items
        ],
        "required_user_information": [
            *list(context.get("required_user_information") or []),
            *[{
                "code": "authoring.prose_redraft_review_required",
                "reason": item["reason"], "affected_scope": item["sentence_id"],
            } for item in human_items],
        ],
    }
