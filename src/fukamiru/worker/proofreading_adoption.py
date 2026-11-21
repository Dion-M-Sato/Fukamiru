"""Five real adoption nodes; results are checkpointed and candidates never rolled back."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from fukamiru.errors import NonRetryableFukamiruError
from fukamiru.revisions import proofreading_adoption as rules
from fukamiru.revisions import proofreading_plain as plain
from fukamiru.revisions import proofreading_repair as repair
from fukamiru.worker import proofreading_context as transport


def prepare(worker: Any, claimed: Any, context: dict[str, Any]) -> dict[str, Any]:
    protected = set(context.get("human_protected_block_ids") or []) | set(
        context.get("source_preserve_block_ids") or []
    )
    document_context = [
        {k: p[k] for k in ("paragraph_id", "visible_text", "section_id")}
        for p in rules.prose.inventory(context["proposed_html"], protected)
    ]
    targets = context.get("proofreading_target_paragraph_ids")
    if targets is not None:
        from xml.etree import ElementTree as ET

        all_ids = {p.get("id") for p in ET.fromstring(context["proposed_html"]).iter("p")}
        if not set(targets).issubset(all_ids):
            raise NonRetryableFukamiruError("adoption.unknown_target_paragraph")
        protected.update(all_ids - set(targets))
    frozen = rules.freeze(
        context["proposed_html"],
        protected,
    )
    frozen["document_context"] = document_context
    lineage, realization = repair.actual_citation_lineage(context)
    return {
        **context,
        "pre_proofreading_html": context["proposed_html"],
        "japanese_proofreading_profile": rules.PROFILE,
        "japanese_proofreading_context": {"plan": frozen, "proposals": [], "reviews": []},
        "proofreading_adoption": {
            "contract": rules.CONTRACT,
            "rules": rules.supplied_rules(context),
            "source_context_lineage": deepcopy(lineage),
            "original_context_lineage": deepcopy(
                context.get("authoring_paragraph_citation_lineage") or []
            ),
            "original_applied_edits": deepcopy(context.get("applied_edits") or []),
            "citation_realization": realization,
        },
    }


def _call(
    worker: Any,
    claimed: Any,
    context: dict[str, Any],
    stage: str,
    prompt: str,
    payload: dict[str, Any],
    schema: dict[str, Any],
) -> dict[str, Any]:
    from fukamiru.authoring.codex import _validate_schema

    result = transport._call(worker, claimed, stage, prompt, payload, schema, context)
    _validate_schema(result, schema)
    return result


def _groups(context: dict[str, Any]):
    plan = context["japanese_proofreading_context"]["plan"]
    for section in plan["sections"]:
        yield (
            section,
            [p for p in plan["paragraphs"] if p["paragraph_id"] in section["paragraph_ids"]],
        )


def propose(worker: Any, claimed: Any, context: dict[str, Any]) -> dict[str, Any]:
    state = context["japanese_proofreading_context"]
    if rules.digest(context["proposed_html"]) != state["plan"]["source_sha256"]:
        raise NonRetryableFukamiruError("adoption.source_changed")
    proposals = []
    saved = context.get("japanese_proofreading_saved_candidates") or {}
    if saved and saved.get("source_sha256") != state["plan"]["source_sha256"]:
        raise NonRetryableFukamiruError("adoption.saved_candidate_source_mismatch")
    saved_rows = {r["paragraph_id"]: r for r in saved.get("paragraphs") or []}
    for section, paragraphs in _groups(context):
        pending = [p for p in paragraphs if p["paragraph_id"] not in saved_rows]
        result = (
            _call(
                worker,
                claimed,
                context,
                "llmjp_adopt_proposal",
                rules.EDITOR_PROMPT,
                {
                    "targets": pending,
                    "document_context": state["plan"]["document_context"],
                    "user_language_feedback": context.get("japanese_language_feedback") or "",
                    "citation_bindings": context["proofreading_adoption"]["source_context_lineage"],
                },
                rules.response_schema(pending),
            )
            if pending
            else {"paragraphs": [], "human_review": []}
        )
        rows = rules.exact_rows(
            paragraphs,
            {
                "paragraphs": [
                    *result["paragraphs"],
                    *[
                        saved_rows[p["paragraph_id"]]
                        for p in paragraphs
                        if p["paragraph_id"] in saved_rows
                    ],
                ]
            },
        )
        proposals.append(
            {
                "section_id": section["section_id"],
                "paragraphs": rows,
                "human_review": result["human_review"],
                "saved_candidate_provenance": saved.get("provenance")
                if len(pending) != len(paragraphs)
                else None,
            }
        )
    return {**context, "japanese_proofreading_context": {**state, "proposals": proposals}}


def review(worker: Any, claimed: Any, context: dict[str, Any]) -> dict[str, Any]:
    state = context["japanese_proofreading_context"]
    reviews = []
    groups = list(_groups(context))
    if len(groups) != len(state["proposals"]):
        raise NonRetryableFukamiruError("adoption.proposals_incomplete")
    for (section, paragraphs), proposed in zip(groups, state["proposals"], strict=True):
        rows, projection = repair.project_fixed_assets(context, proposed["paragraphs"])
        history, human, seen = [], [], set()
        while True:
            key = rules.digest(rows)
            if key in seen:
                raise NonRetryableFukamiruError("adoption.repair_no_progress_candidate_retained")
            seen.add(key)
            prose = plain.input_prose(context, paragraphs, rows, history)
            try:
                repair.validate_candidate(context, rows)
            except NonRetryableFukamiruError as exc:
                issues = [{"mechanical_error": str(exc)}]
            else:
                audit = _call(
                    worker,
                    claimed,
                    context,
                    "llmjp_adopt_plain_readonly_audit",
                    plain.AUDIT_PROMPT,
                    prose,
                    plain.audit_schema(paragraphs),
                )
                issues = plain.findings(audit, prose)
                history.append(
                    {
                        "input_candidate_sha256": key,
                        "output_candidate_sha256": key,
                        "applied": False,
                        "repairs": [],
                        "audit": audit,
                    }
                )
                human.extend(audit.get("human_review") or [])
                if not issues:
                    break
            validation_error = None
            rejected = set()
            while True:
                payload = {
                    **plain.input_prose(context, paragraphs, rows, history),
                    "findings": issues,
                }
                if validation_error:
                    payload["validation_error"] = validation_error
                answer = _call(
                    worker,
                    claimed,
                    context,
                    "llmjp_adopt_plain_repairs",
                    plain.REPAIR_PROMPT,
                    payload,
                    plain.repair_schema(paragraphs),
                )
                human.extend(answer["human_review"])
                try:
                    tentative, actual = plain.apply_repairs(context, rows, answer["repairs"])
                    repair.validate_candidate(context, tentative)
                    if rules.digest(tentative) == rules.digest(rows):
                        raise NonRetryableFukamiruError(
                            "adoption.repair_no_progress_candidate_retained"
                        )
                except NonRetryableFukamiruError as exc:
                    validation_error = str(exc)
                    invalid_key = rules.digest(
                        {
                            "error": validation_error,
                            "changes": [
                                {k: e[k] for k in ("paragraph_id", "before", "after")}
                                for e in answer["repairs"]
                            ],
                        }
                    )
                    history.append(
                        {
                            "input_candidate_sha256": key,
                            "applied": False,
                            "repairs": answer["repairs"],
                            "validation_error": validation_error,
                        }
                    )
                    if invalid_key in rejected or "no_progress" in validation_error:
                        raise
                    rejected.add(invalid_key)
                    continue
                history.append(
                    {
                        "input_candidate_sha256": key,
                        "output_candidate_sha256": rules.digest(tentative),
                        "repairs": actual,
                        "applied": True,
                    }
                )
                rows = tentative
                break
        reviews.append(
            {
                "section_id": section["section_id"],
                "paragraphs": rows,
                "human_review": human,
                "repair_history": history,
                "fixed_asset_projection": projection,
                "audited_candidate_sha256": rules.digest(rows),
            }
        )
    return {**context, "japanese_proofreading_context": {**state, "reviews": reviews}}


def apply(worker: Any, claimed: Any, context: dict[str, Any]) -> dict[str, Any]:
    state = context["japanese_proofreading_context"]
    rows = [r for group in state["reviews"] for r in group["paragraphs"]]
    original = state["plan"]["paragraphs"]
    rules.exact_rows(original, {"paragraphs": rows})
    for group in state["reviews"]:
        if group.get("audited_candidate_sha256") != rules.digest(group["paragraphs"]):
            raise NonRetryableFukamiruError("adoption.candidate_changed_after_readonly_audit")
    repair.validate_candidate(context, rows)
    adopted = rules.install(context["pre_proofreading_html"], state["plan"], rows)
    formatting_sources = rules.freeze(adopted, set(state["plan"]["protected_ids"]))["paragraphs"]
    result = worker._authoring_invoke(
        claimed,
        stage="format_adopted_japanese_v32",
        context=context,
        payload={
            "paragraphs": formatting_sources,
            "supplied_rules": context["proofreading_adoption"]["rules"],
        },
        operation_name="format_adopted_japanese_v32",
    )
    formatted = rules.format_rows(
        formatting_sources,
        rules.exact_rows(formatting_sources, result),
        rules.original_quote_roles(context["pre_proofreading_html"]),
    )
    candidate = rules.install(adopted, state["plan"], formatted)
    rules.validate_paired_punctuation(candidate, {p["paragraph_id"] for p in original})
    validation = rules.validate_structure(
        context["pre_proofreading_html"], candidate, set(state["plan"]["protected_ids"])
    )
    report = {
        "contract": rules.CONTRACT,
        "adopted_model_html": adopted,
        "formatted_html": candidate,
        "formatter_result": result,
        "formatted_rows": formatted,
        "validation": validation,
        "changed_paragraphs": [
            p["paragraph_id"]
            for p, row in zip(original, rows, strict=True)
            if p["text_slots"] != row["text_slots"]
        ],
        "changes": [
            {
                "paragraph_id": p["paragraph_id"],
                "reason": row["reason"],
                "changes": rules.changes(p["text_slots"], row["text_slots"]),
            }
            for p, row in zip(original, rows, strict=True)
        ],
        "reverted_blocks": [],
    }
    human_rows = [
        item
        for group in [*state["proposals"], *state["reviews"]]
        for item in group.get("human_review", [])
    ] + list(result.get("human_review") or [])
    by_id = {p["paragraph_id"]: p for p in original}
    human_items = {item["review_item_id"]: item for item in context.get("human_review_items") or []}
    for item in human_rows:
        paragraph = by_id.get(item["paragraph_id"])
        key = "llmjp-adopt:" + rules.digest(item)
        human_items[key] = {
            "review_item_id": key,
            "section_id": paragraph["section_id"] if paragraph else "manuscript",
            "sentence_id": paragraph["paragraph_id"] if paragraph else None,
            "category": "proofreading_style",
            "reason": item.get("reason") or "【システム通知】確認項目の説明が未記入です。",
            "recommended_check": item.get("recommended_check")
            or "受信した原項目と候補を確認してください。",
            "source_ids": [],
            "blocks_render": False,
            "status": "open",
        }
    report["human_review_original_items"] = deepcopy(human_rows)
    return {
        **context,
        "proposed_html": candidate,
        "human_review_items": list(human_items.values()),
        "manuscript_proofreading_shadow": report,
        "proofreading_adoption": {**context["proofreading_adoption"], "application": report},
    }


def reconcile(worker: Any, claimed: Any, context: dict[str, Any]) -> dict[str, Any]:
    from fukamiru.worker.runner import WorkerRunner

    state = context["japanese_proofreading_context"]
    validation = rules.validate_structure(
        context["pre_proofreading_html"],
        context["proposed_html"],
        set(state["plan"]["protected_ids"]),
    )
    # The same frozen cite/footnote assets and reviewed supporting statements are authoritative.
    # Do not infer different sources from mutable wording, or restore entire original paragraphs.
    bridge = WorkerRunner._authoring_v29_plan_bridge(
        context, context["proposed_html"], semantic_attribution=False
    )
    if not dict(bridge.get("structure_plan_validation") or {}).get("valid"):
        raise NonRetryableFukamiruError("adoption.section_projection_unresolved_candidate_retained")
    bridge["authoring_paragraph_citation_lineage"] = deepcopy(
        context["proofreading_adoption"]["source_context_lineage"]
    )
    if context["proofreading_adoption"]["citation_realization"]["evidence_uses_available"]:
        bridge["applied_edits"] = [
            e for e in context.get("applied_edits") or [] if not e.get("evidence_ids")
        ]
        bridge["applied_edits"].extend(
            {
                "operation": "replace_text",
                "block_id": row["output_block_id"],
                "output_block_id": row["output_block_id"],
                "evidence_ids": row["evidence_ids"],
                "claim_ids": row["claim_ids"],
                "source": "actual_sentence_evidence_uses",
            }
            for row in bridge["authoring_paragraph_citation_lineage"]
        )
    bridge["authoring_citation_lineage_reconciliation"] = {
        "policy": rules.CONTRACT,
        "dropped_bindings": [],
        "projected_block_ids": [p["paragraph_id"] for p in state["plan"]["paragraphs"]],
    }
    return {
        **context,
        **bridge,
        "authoring_v31_attribution_predicate_validation": {
            "passed": True,
            "policy": rules.CONTRACT,
            "reviewed_paragraph_count": sum(len(r["paragraphs"]) for r in state["reviews"]),
        },
        "proofreading_adoption": {**context["proofreading_adoption"], "reconciliation": validation},
    }


def finish(worker: Any, claimed: Any, context: dict[str, Any]) -> dict[str, Any]:
    """Retain adopted prose and route editorial caveats by recorded exact deletion."""
    from fukamiru.authoring.verification_notes import remove_excerpts, route

    adopted = context["proofreading_adoption"]["application"]["formatted_html"]
    source = context["proposed_html"]
    rules.validate_finalized(adopted, source)
    protected = set(context["japanese_proofreading_context"]["plan"]["protected_ids"])
    revised, items = route(worker, claimed, context, source, protected)
    cards = [
        {"paragraph_id": p["paragraph_id"], "editorial_only": [
            i["reason"] for i in items if i["sentence_id"] == p["paragraph_id"]]}
        for p in rules.prose.inventory(adopted, protected)
    ]
    expected, _ = remove_excerpts(adopted, cards, protected)
    validation = rules.validate_finalized(expected, revised)
    human = {i["review_item_id"]: i for i in [
        *list(context.get("human_review_items") or []), *items,
    ]}
    inference_ledger = list(context.get("prose_redraft_inference_ledger_v31") or [])
    return {
        **context,
        "proposed_html": revised,
        "human_review_items": list(human.values()),
        "verification_notes_routed_v32": items,
        "proofreading_adoption": {**context["proofreading_adoption"],
            "verification_routing": {"source_sha256": rules.digest(source),
                "target_sha256": rules.digest(revised), "cards": cards, "items": items}},
        "prose_redraft_inference_ledger_v31": inference_ledger,
        "reference_rendered_html_v26": revised,
        "prose_redraft_report_v31": {
            "status": "llmjp_candidate_adopted",
            "target_html_sha256": rules.digest(revised),
            "verification_notes_routed": items,
            "structure_unchanged": validation["existing_IDs_preserved"],
            "locked_content_unchanged": validation["adopted_prose_preserved"],
            "anchors_valid": validation["canonical_html_valid"],
            "inference_ledger": inference_ledger,
            "expression_policy": rules.EXPRESSION_POLICY,
        },
    }
