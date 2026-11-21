"""Contextual concept review, separate from prose fluency and source verification.

Reviews are bound to exact candidate fields. A failed premise cannot be repaired
by merely substituting a synonym in prose whose argument is already locked.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from fukamiru.errors import NonRetryableFukamiruError

POLICY = "contextual-concept-fit-v1"
GENERATION_STAGES = {
    "claim_tree", "argument_graph_v31", "defense_obligations_v31",
    "section_contracts_v31", "japanese_paragraph_redraft_v31", "compose_body_prose_v31",
}
PLAN_STAGES = GENERATION_STAGES - {
    "japanese_paragraph_redraft_v31", "compose_body_prose_v31",
}
TEXT_FIELDS = {
    "text", "text_slots", "proposition", "adopted_text", "scope_conditions",
    "qualifiers", "required_terms", "research_gaps", "research_gap", "rival_interpretation",
    "challenge", "response",
    "local_question", "local_conclusion", "section_output", "thesis_link",
    "rationale", "description", "definition", "consequence", "conclusion",
    "reason",
}
MATERIAL_FIELDS = (
    "note_text", "propositions", "research_question", "research_problem", "claims",
    "claim_scopes", "argument_nodes", "evidence", "evidence_context",
    "section_contracts", "supplied_material",
    "locked_section_contracts", "paragraphs", "context_paragraphs",
    "previous_section_excerpt", "next_section_excerpt",
)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def units(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    """Expose prose fields, never machine IDs or verification labels as prose."""
    result = []

    def walk(value: Any, path: str, selected: bool = False, role: str = "claim") -> None:
        if isinstance(value, dict):
            if value.get("node_type") == "evidence":
                role = "evidence_record"
            for key in sorted(value):
                child = value[key]
                field_role = {
                    "research_gap": "internal_material_gap",
                    "research_gaps": "internal_material_gap",
                    "rival_interpretation": "rival_hypothesis", "required_terms": "required_term",
                    "reason": "internal_decision_reason",
                }.get(key, role)
                walk(child, path + "/" + key, key in TEXT_FIELDS, field_role)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, path + "/" + str(index), selected, role)
        elif selected and isinstance(value, str) and value.strip():
            row: dict[str, Any] = {"unit_id": path, "text": value, "field_role": role}
            quoted = list(dict.fromkeys(
                match[1].strip() for match in re.finditer(r"「([^「」]+)」", value)
                if match[1].strip()
            ))
            if quoted:
                row["concept_candidates"] = quoted
            result.append(row)

    walk(candidate, "")
    return result


def material(context: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    # Original memo spans remain visible even when a later locked contract
    # already contains the concept being challenged. A contract is no evidence
    # that a research procedure was actually performed.
    result = {
        key: payload[key] if key in payload else context[key]
        for key in MATERIAL_FIELDS if key in payload or key in context
    }
    if result.get("propositions"):
        result.pop("note_text", None)
        source_spans = {}
        propositions = []
        for original in result["propositions"]:
            row = {key: original[key] for key in (
                "id", "text", "provenance", "memo_annotations", "memo_annotation_kinds",
            ) if key in original}
            span = original.get("source_span")
            if span:
                span_id = "span:" + digest(span)[:20]
                source_spans[span_id] = {"id": span_id, "text": span}
                row["source_span_ref"] = span_id
            propositions.append(row)
        result["propositions"] = propositions
        result["source_spans"] = list(source_spans.values())
    for key in ("evidence_context",):
        if isinstance(result.get(key), list):
            result[key] = [{
                field: row[field] for field in (
                    "id", "proposition_id", "claim_ids", "title", "text", "excerpt",
                    "source_span", "assertion", "verification_scope", "realization",
                ) if field in row
            } for row in result[key] if isinstance(row, dict)]
    result["theory_attribution_treatments"] = [{
        key: row.get(key) for key in (
            "proposition_id", "verification_scope", "realization",
            "may_assert_as_external_fact", "visible_attribution_required",
            "visible_qualification_required",
        )
    } for row in context.get("authoring_atomic_evidence_contracts") or []
        if row.get("verification_scope") == "theoretical_attribution"]
    return result


def _ids(value: Any) -> set[str]:
    if isinstance(value, dict):
        return {
            str(child) for key, child in value.items()
            if key in {"id", "proposition_id", "claim_id", "section_id", "paragraph_id",
                       "block_id", "evidence_id", "source_id", "finding_id", "obligation_id"}
            and isinstance(child, str) and child
        } | set().union(*(_ids(child) for child in value.values()), set())
    if isinstance(value, list):
        return set().union(*(_ids(child) for child in value), set())
    return set()


def validate_review(payload: dict[str, Any], result: dict[str, Any]) -> None:
    expected = payload["units"]
    rows = result.get("units")
    if not isinstance(rows, list) or [r.get("unit_id") for r in rows] != [
        row["unit_id"] for row in expected
    ]:
        raise NonRetryableFukamiruError("Concept review changed field IDs or omitted coverage")
    allowed_ids = _ids(payload.get("material") or {})
    for source, row in zip(expected, rows, strict=True):
        seen = set()
        for concept in row.get("concepts") or []:
            term = concept.get("term")
            if not isinstance(term, str) or not term.strip() or term not in source["text"]:
                raise NonRetryableFukamiruError(
                    f"Concept review excerpt is not in its field: {source['unit_id']}: {term!r}"
                )
            if term in seen:
                raise NonRetryableFukamiruError("Concept review duplicated a term")
            seen.add(term)
            if not set(concept.get("basis_ids") or []) <= allowed_ids:
                raise NonRetryableFukamiruError("Concept review invented a basis ID")
            verdict = concept.get("verdict")
            if verdict not in {"appropriate", "wording", "unsupported_premise", "unclear"}:
                raise NonRetryableFukamiruError("Concept review has an unknown verdict")
            if not str(concept.get("reason") or "").strip():
                raise NonRetryableFukamiruError("Concept review lacks a reason")
            if concept.get("must_keep") and verdict not in {"appropriate", "wording"}:
                raise NonRetryableFukamiruError("Concept review protects a rejected concept")
        if not set(source.get("concept_candidates") or []) <= seen:
            raise NonRetryableFukamiruError("Concept review omitted an explicit concept candidate")
    originals = {row["unit_id"]: row["text"] for row in (
        payload.get("source_units") or payload.get("previous_candidate_units") or []
    )}
    present = "\n".join(row["text"] for row in expected)
    for omission in result.get("omissions") or []:
        term = omission.get("term")
        original = originals.get(omission.get("source_unit_id"), "")
        if not term or term not in original or term in present or not omission.get("reason"):
            raise NonRetryableFukamiruError("Concept review invented an omitted specialist term")


def findings(review: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"unit_id": row["unit_id"], **concept}
        for row in review["units"] for concept in row["concepts"]
        if concept["verdict"] != "appropriate"
    ] + [{
        "unit_id": row["source_unit_id"], "term": row["term"], "verdict": "wording",
        "reason": row["reason"], "basis_ids": [], "must_keep": False,
        "missing_required_term": True,
    } for row in review.get("omissions") or []]


def field_anchor_errors(payload: dict[str, Any], result: dict[str, Any]) -> dict[str, str]:
    """Only isolate verbatim-anchor errors after exact, ordered field coverage."""
    if not isinstance(result, dict):
        return {}
    sources, rows = payload["units"], result.get("units")
    if not isinstance(rows, list) or [r.get("unit_id") for r in rows] != [
        r["unit_id"] for r in sources
    ]:
        return {}
    errors = {}
    for source, row in zip(sources, rows, strict=True):
        try:
            validate_review({**payload, "units": [source]}, {"units": [row], "omissions": []})
        except NonRetryableFukamiruError as exc:
            if not str(exc).startswith("Concept review excerpt is not in its field:"):
                return {}
            errors[source["unit_id"]] = str(exc)
    return errors if 0 < len(errors) <= 8 else {}


def repair_field_anchors(worker: Any, claimed: Any, context: dict[str, Any], *,
                         payload: dict[str, Any], rejected: dict[str, Any],
                         operation: str) -> dict[str, Any] | None:
    errors = field_anchor_errors(payload, rejected)
    if not errors:
        return None
    merged = deepcopy(rejected)
    repaired_ids = []
    for index, source in enumerate(payload["units"]):
        unit_id = source["unit_id"]
        if unit_id not in errors:
            continue
        field_payload = {**payload, "units": [source], "context_units": payload["units"],
                         "review_contract_repair": {
                             "error": errors[unit_id], "rejected_review": rejected,
                             "target_unit_id": unit_id,
                             "instruction": (
                                 "Review only the one supplied unit, using context_units for "
                                 "context. Repair its verbatim anchors without changing the "
                                 "candidate or hiding a semantic problem. Every term must be "
                                 "copied exactly from this unit's text, including particles. "
                                 "Do not copy another field's wording. Preserve justified "
                                 "specialist concepts and unsupported-premise findings; if an "
                                 "earlier judgment was assigned to the wrong text, explain the "
                                 "judgment for this actual text. Return omissions as an empty "
                                 "array; the whole review's existing omissions are retained."
                             ),
                         }}
        result = worker._authoring_invoke_raw(
            claimed, stage="review_concept_fit_v32", context=context, payload=field_payload,
            operation_name=operation + ":field-anchor-repair:" + digest(unit_id)[:12],
            checkpoint_variant=POLICY + ":field-anchors-v1",
        )
        validate_review(field_payload, result)
        if result.get("omissions"):
            raise NonRetryableFukamiruError("Concept review field repair changed global omissions")
        merged["units"][index] = result["units"][0]
        repaired_ids.append(unit_id)
    validate_review(payload, merged)
    context.setdefault("concept_field_anchor_recoveries_v32", []).append({
        "operation": operation, "unit_ids": repaired_ids, "errors": errors,
    })
    return merged


def review(worker: Any, claimed: Any, context: dict[str, Any], *, stage: str,
           candidate: dict[str, Any], supplied: dict[str, Any], operation: str,
           previous: dict[str, Any] | None = None,
           previous_candidate: dict[str, Any] | None = None,
           source_candidate: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {
        "policy": POLICY, "purpose": stage, "units": units(candidate),
        "material": supplied, "previous_review": previous or {},
        "previous_candidate_units": units(previous_candidate or {}),
        "source_units": units(source_candidate or {}),
    }
    if any(row["field_role"] in {"evidence_record", "internal_decision_reason"}
           for row in payload["units"]):
        payload["unit_role_policy"] = (
            "evidence_recordは内部の根拠カード、internal_decision_reasonは内部の採否理由"
            "であり、どちらも本文ではない。書誌・原典・適用の"
            "確認範囲を正確に記録すること自体は適切であり、その作業状態だけをwording"
            "問題にしない。内部の記録や採否理由でも、対象・方法・分類・検証義務を根拠なく加える概念や"
            "誤った適用は通常どおり検査する。claim等の対象分析を内部記録だと読み替え"
            "ない。読者向けの本文では確認不足を述べる留保を除き、人間確認へ保存する。"
        )
    load_cached = getattr(worker, "_authoring_load_concept_review_checkpoint", None)
    seed_path = _report_path(worker, claimed, operation + ":invalid:0", digest(candidate))
    if callable(load_cached) and seed_path.is_file():
        try:
            seed = json.loads(seed_path.read_text(encoding="utf-8"))
            if (seed.get("policy") == POLICY and seed.get("stage") == stage
                    and seed.get("status") == "invalid_review"
                    and seed.get("candidate_sha256") == digest(candidate)):
                rejected = seed.get("initial_review")
                try:
                    validate_review(payload, rejected)
                except NonRetryableFukamiruError as exc:
                    if str(exc).startswith("Concept review "):
                        repaired_payload = _contract_repair_payload(payload, str(exc), rejected)
                        cached = load_cached(
                            claimed, context=context, payload=repaired_payload,
                            operation_name=operation + ":contract-repair",
                            checkpoint_variant=POLICY,
                        )
                        if cached is not None:
                            validate_review(repaired_payload, cached)
                            context.setdefault("concept_review_recovery_seeds_v32", []).append({
                                "operation": operation, "path": str(seed_path),
                                "candidate_sha256": digest(candidate), "error": str(exc),
                            })
                            return cached
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            # A seed is only a rejected-response input, never an accepted result.
            # An unusable archive cannot replace a fresh, validated review.
            pass
    # This raw call still runs provider/schema/checkpoint validation. It avoids
    # recursively reviewing the review or trusting an older node's approval.
    base_payload = payload
    for attempt in range(2):
        result = None
        try:
            result = worker._authoring_invoke_raw(
                claimed, stage="review_concept_fit_v32", context=context, payload=payload,
                operation_name=operation + (":contract-repair" if attempt else ""),
                checkpoint_variant=POLICY,
            )
            validate_review(payload, result)
            return result
        except NonRetryableFukamiruError as exc:
            # A provider schema rejection is not retried. Only our explicit
            # result identity/coverage checks get one new, corrective request.
            if not str(exc).startswith("Concept review "):
                raise
            rejected = getattr(exc, "concept_review_result", None) or result
            save_report(worker, claimed, context, operation + f":invalid:{attempt}", {
                "policy": POLICY, "stage": stage, "status": "invalid_review",
                "candidate_sha256": digest(candidate), "accepted_sha256": None,
                "initial_review": rejected, "error": str(exc),
            })
            if attempt:
                recovered = repair_field_anchors(
                    worker, claimed, context, payload=base_payload,
                    rejected=rejected, operation=operation,
                )
                if recovered is not None:
                    return recovered
                raise
            payload = _contract_repair_payload(payload, str(exc), rejected)
    raise AssertionError("Unreachable concept review retry")


def _contract_repair_payload(payload: dict[str, Any], error: str,
                             rejected: dict[str, Any]) -> dict[str, Any]:
    repair: dict[str, Any] = {
        "error": error, "rejected_review": rejected,
        "instruction": (
            "Repair only the review contract. Cover every supplied unit_id exactly once. "
            "Every term must be a verbatim substring of that unit's text; no paraphrase, "
            "normalization, ellipsis or another field's wording. Use real material IDs. "
            "must_keep is true only for necessary named concepts judged appropriate "
            "or requiring wording clarification. "
            "Cover every explicit concept_candidates item as an individual exact term. "
            "Do not hide an actual concept problem or rewrite the candidate to pass."
        ),
    }
    if error == "Concept review invented a basis ID":
        allowed = _ids(payload.get("material") or {})
        repair["invalid_basis_ids"] = [
            {"unit_id": row["unit_id"], "term": concept["term"],
             "ids": sorted(set(concept.get("basis_ids") or []) - allowed)}
            for row in rejected.get("units") or []
            for concept in row.get("concepts") or []
            if set(concept.get("basis_ids") or []) - allowed
        ]
        repair["allowed_basis_ids"] = sorted(allowed)
        repair["instruction"] += (
            " The listed invalid_basis_ids are not defined in the supplied material. "
            "Use only exact IDs from allowed_basis_ids, and verify that the selected "
            "material actually supports the judgment; similar-looking IDs are not "
            "interchangeable. Do not repeat an invalid ID merely because it appears "
            "in rejected_review. Preserve real concept problems and necessary terms."
        )
    return {**payload, "review_contract_repair": repair}


def _report_path(worker: Any, claimed: Any, operation: str, candidate_sha: str) -> Path:
    key = digest([operation, candidate_sha])[:24]
    return worker.settings.paths.runtime_root / "outputs" / claimed.run_id / "concept-fit" / (
        key + ".json"
    )


def protected_terms(review_result: dict[str, Any]) -> set[str]:
    return {
        c["term"] for row in review_result["units"] for c in row["concepts"]
        if c["verdict"] in {"appropriate", "wording"} and c["must_keep"]
    }


def preserve_terms(after: dict[str, Any], terms: set[str]) -> None:
    after_text = "\n".join(row["text"] for row in units(after))
    # Terms may be consolidated in a section, but a necessary named concept
    # cannot silently disappear under an ordinary-language replacement.
    missing = [term for term in terms if term not in after_text]
    if missing:
        raise NonRetryableFukamiruError(
            "Concept repair lost required concepts: " + ", ".join(missing)
        )


def save_report(worker: Any, claimed: Any, context: dict[str, Any], operation: str,
                report: dict[str, Any]) -> None:
    path = _report_path(worker, claimed, operation, report["candidate_sha256"])
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    if path.exists() and path.read_text(encoding="utf-8") != serialized:
        version = hashlib.sha256(serialized.encode()).hexdigest()[:16]
        path = path.with_stem(path.stem + "-" + version)
    if path.exists():
        if path.read_text(encoding="utf-8") != serialized:
            raise NonRetryableFukamiruError("Concept report content address collision")
    else:
        path.write_text(serialized, encoding="utf-8")
    worker._register_artifact(
        claimed, path, "authoring_concept_fit_report", "概念の文脈適合と専門語の保持",
        media_type="application/json",
    )
    context.setdefault("concept_fit_reports_v32", []).append({
        "operation": operation, "path": str(path), "status": report["status"],
        "candidate_sha256": report["candidate_sha256"],
        "accepted_sha256": report.get("accepted_sha256"),
    })


def apply_repairs(candidate: dict[str, Any], edits: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply only addressed prose fields; IDs, topology and citations stay fixed."""
    validate_repairs({"candidate_units": units(candidate)}, {"edits": edits})
    allowed = {row["unit_id"] for row in units(candidate)}
    seen = set()
    revised = deepcopy(candidate)
    removals = []
    for edit in edits:
        path = edit["unit_id"]
        if path not in allowed or path in seen:
            raise NonRetryableFukamiruError("Concept repair changed an unknown or duplicate field")
        seen.add(path)
        parts = path.split("/")[1:]
        parent = revised
        for part in parts[:-1]:
            parent = parent[int(part)] if isinstance(parent, list) else parent[part]
        key = int(parts[-1]) if isinstance(parent, list) else parts[-1]
        if edit["operation"] == "replace":
            parent[key] = edit["text"]
        elif (edit["operation"] == "remove" and isinstance(parent, list)
              and parts[-2] in {"required_terms", "qualifiers", "scope_conditions"}
              and edit["text"] == ""):
            removals.append((path, parent, key))
        else:
            raise NonRetryableFukamiruError("Concept repair cannot remove locked fields")
    # Numeric order, including 10+, is required when several list items go away.
    for _, parent, key in sorted(removals, key=lambda row: row[2], reverse=True):
        del parent[key]
    return revised


def validate_repairs(payload: dict[str, Any], result: dict[str, Any]) -> None:
    allowed = {row["unit_id"] for row in payload["candidate_units"]}
    seen = set()
    for edit in result["edits"]:
        path = edit["unit_id"]
        if path not in allowed or path in seen:
            raise NonRetryableFukamiruError("Concept repair changed an unknown or duplicate field")
        seen.add(path)
        if edit["operation"] == "remove":
            parts = path.split("/")
            if (parts[-2] not in {"required_terms", "qualifiers", "scope_conditions"}
                    or not parts[-1].isdigit() or edit["text"] != ""):
                raise NonRetryableFukamiruError("Concept repair cannot remove locked fields")
        elif edit["operation"] != "replace":
            raise NonRetryableFukamiruError("Concept repair has an unknown operation")


def route_removed_verification(
    context: dict[str, Any], before: dict[str, Any], after: dict[str, Any],
) -> list[dict[str, Any]]:
    """Preserve removed work-status conditions as review tasks, never as facts."""
    from fukamiru.authoring.evidence_contracts import HumanReviewItem

    pattern = re.compile(
        r"(?:原典|原著|出典|引用箇所|概念内容|適用|資料|外部根拠|"
        r"掲載(?:期間|史|年|媒体)|精読|読者調査|比較命題).{0,60}"
        r"(?:未確認|未検証|確認する必要|確認でき|確認されるまで|照合|"
        r"未解決|未了|未実施|確定せず|確定していない|"
        r"(?:確認|検証)(?:が|は)?(?:されて|できて)?いない|"
        r"(?:確認|検証)(?:した|できた)こと(?:には|に)?ならない)|採用を確定できない"
    )
    after_texts = {row["unit_id"]: row["text"] for row in units(after)}
    items = context.setdefault("human_review_items", [])
    existing = {row["review_item_id"] for row in items}
    routed = []
    for row in units(before):
        if not pattern.search(row["text"]) or pattern.search(after_texts.get(row["unit_id"], "")):
            continue
        parent = before
        parts = row["unit_id"].split("/")[1:]
        target = row["unit_id"]
        for part in parts[:-1]:
            parent = parent[int(part)] if isinstance(parent, list) else parent[part]
            if isinstance(parent, dict):
                target = str(parent.get("claim_id") or parent.get("paragraph_id")
                             or parent.get("section_id") or parent.get("id") or target)
        key = "concept-verification:" + digest([target, row["text"]])[:20]
        if key in existing:
            continue
        item = HumanReviewItem.model_validate({
            "review_item_id": key, "section_id": "manuscript", "sentence_id": target,
            "category": "verification_status", "reason": row["text"], "source_ids": [],
            "recommended_check": (
                "この箇所の資料・出典・適用関係を本文外で確認してください。"
            ),
            "blocks_render": False, "status": "open",
        }).model_dump(mode="json")
        items.append(item)
        existing.add(key)
        routed.append(item)
    return routed


def guard(worker: Any, claimed: Any, *, stage: str, context: dict[str, Any],
          payload: dict[str, Any], candidate: dict[str, Any], operation: str,
          retrieval_context: str = "") -> dict[str, Any]:
    supplied = material(context, payload)
    source = {}
    if stage in {"japanese_paragraph_redraft_v31", "compose_body_prose_v31"}:
        rows = payload.get("paragraphs") or []
        source = {"paragraphs": [{"text": row.get("text") or ""} for row in rows]}
        if stage == "compose_body_prose_v31":
            source = {"paragraphs": [{"text_slots": row.get("source_text_slots") or []}
                                      for row in rows]}
    first = review(worker, claimed, context, stage=stage, candidate=candidate,
                   supplied=supplied, operation=operation + ":concept-review",
                   source_candidate=source)
    problems = findings(first)
    report = {
        "policy": POLICY, "stage": stage, "operation": operation,
        "candidate_sha256": digest(candidate), "before": candidate,
        "initial_review": first, "status": "passed", "repairs": [],
    }
    if not problems:
        report["accepted_sha256"] = digest(candidate)
        save_report(worker, claimed, context, operation, report)
        return candidate
    if stage not in PLAN_STAGES and any(
        item["verdict"] in {"unsupported_premise", "unclear"} for item in problems
    ):
        report.update(status="plan_revision_required", findings=problems)
        save_report(worker, claimed, context, operation, report)
        raise NonRetryableFukamiruError(
            "Concept premise requires upstream claim/contract repair: "
            + json.dumps(problems, ensure_ascii=False)
        )
    current, current_review = candidate, first
    keep = protected_terms(first)
    seen = {digest(candidate)}
    remaining = problems
    for repair_index in range(2 if stage in PLAN_STAGES else 1):
        restore = {row["term"] for row in current_review.get("omissions") or []}
        keep.update(protected_terms(current_review))
        repair_payload = {
            "purpose": stage, "candidate_units": units(current), "material": supplied,
            "source_units": units(source), "concept_fit_repair": {
                "findings": remaining, "must_keep_terms": sorted(keep),
                "restore_terms": sorted(restore) if repair_index else [
                    row["term"] for row in first.get("omissions") or []
                ],
                "instruction": (
                    "Correct the flagged wording or concept premise using the original material. "
                    "Preserve IDs, valid claims, necessary technical concepts, "
                    "citations and objects. "
                    "An unsupported premise needs argument repair, not a synonym or a new caveat. "
                    "Keep editorial/verification tasks outside reader-facing argument. "
                    "Restore the object and scope supported by original material. An unsupported "
                    "restriction is not a scope to preserve; do not invent a different research "
                    "corpus to retain it. Do not generalize beyond the original material. "
                    "Change only affected fields and their logically dependent fields."
                ),
            },
        }
        if repair_index:
            repair_payload["previous_failed_repair"] = {
                "original_units": units(candidate), "rejected_units": units(current),
                "instruction": (
                    "The first repair failed independent review. Correct the remaining or newly "
                    "introduced premises without adding historical, causal or verification claims. "
                    "Do not return an earlier rejected candidate. This is the final repair."
                ),
            }
        suffix = f":{repair_index + 1}" if repair_index else ""
        changes = worker._authoring_invoke_raw(
            claimed, stage="repair_concept_fit_v32", context=context,
            payload=repair_payload, operation_name=operation + ":concept-repair" + suffix,
            retrieval_context=retrieval_context, checkpoint_variant=POLICY,
        )
        repaired = apply_repairs(current, changes["edits"])
        report["repairs"].append(repaired)
        try:
            preserve_terms(repaired, keep | restore)
        except NonRetryableFukamiruError:
            report.update(status="required_concept_lost", accepted_sha256=None)
            save_report(worker, claimed, context, operation, report)
            raise
        keep.update(restore)
        if digest(repaired) in seen:
            report.update(status="unresolved", findings=remaining, accepted_sha256=None,
                          repeated_candidate=True)
            break
        seen.add(digest(repaired))
        inspected = review(
            worker, claimed, context, stage=stage, candidate=repaired,
            supplied=supplied, operation=operation + ":concept-recheck" + suffix,
            previous=current_review, previous_candidate=current, source_candidate=source,
        )
        report.setdefault("repair_reviews", []).append(inspected)
        report["final_review"] = inspected
        remaining = findings(inspected)
        if not remaining:
            report.update(status="repaired", accepted_sha256=digest(repaired))
            report["verification_notes_routed"] = route_removed_verification(
                context, candidate, repaired,
            )
            save_report(worker, claimed, context, operation, report)
            return repaired
        current, current_review = repaired, inspected
    report.update(status="unresolved", findings=remaining, accepted_sha256=None)
    save_report(worker, claimed, context, operation, report)
    raise NonRetryableFukamiruError(
        "Concept repair remains unresolved: " + json.dumps(remaining, ensure_ascii=False)
    )


def check_paragraphs(worker: Any, claimed: Any, context: dict[str, Any], *,
                     paragraphs: list[dict[str, Any]], stage: str,
                     operation: str) -> dict[str, Any]:
    """Audit actual prose without authorizing changes to a locked premise."""
    candidate = {"paragraphs": [{"text": str(
        row.get("visible_text") or row.get("text") or ""
    )} for row in paragraphs]}
    result = review(
        worker, claimed, context, stage=stage, candidate=candidate,
        supplied=material(context, {}), operation=operation,
    )
    problems = findings(result)
    for problem in problems:
        index = int(problem["unit_id"].split("/")[2])
        row = paragraphs[index]
        problem["paragraph_id"] = str(row.get("paragraph_id") or row.get("block_id") or "")
    report = {
        "policy": POLICY, "stage": stage, "candidate_sha256": digest(candidate),
        "accepted_sha256": digest(candidate) if not problems else None,
        "status": "passed" if not problems else "requires_review",
        "initial_review": result, "findings": problems,
        "paragraph_ids": [row.get("paragraph_id") or row.get("block_id") for row in paragraphs],
    }
    save_report(worker, claimed, context, operation, report)
    return report
