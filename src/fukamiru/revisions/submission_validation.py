"""V26 final-candidate projections and deterministic structure certificates.

These helpers deliberately know nothing about model assertions.  A certificate is
valid only for its exact canonical HTML digest; semantic checks are separate.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from fukamiru.documents.canonical_html import CanonicalHTMLValidator, structural_block_fragments
from fukamiru.revisions.coherence import ManuscriptCoherenceService
from fukamiru.revisions.discourse import ManuscriptDiscourseService

POLICY = "manuscript-final-v26.2"
PROMPT_BUDGET = 120_000
CATEGORIES = (
    "temporal_conflict",
    "connector_mismatch",
    "orphan_example",
    "missing_warrant",
    "argument_gap",
    "section_transition",
    "duplication",
    "ordering",
)


def digest(value: Any) -> str:
    raw = (
        value
        if isinstance(value, str)
        else json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def expected_reference_section_count(context: dict[str, Any]) -> int | None:
    """Return the post-footnote bibliography-section contract.

    A source with no bibliography may legitimately acquire exactly one terminal
    bibliography when verified evidence footnotes are rendered. V31 may also
    remove one imported bibliography when the reference renderer proves that
    every imported entry is uncited and renders no bibliography. Other count
    changes remain forbidden.
    """

    inventory = context.get("reference_inventory")
    inventory = inventory if isinstance(inventory, dict) else {}
    if "reference_section_count" not in inventory:
        return None
    expected = int(inventory.get("reference_section_count") or 0)
    footnotes = context.get("evidence_footnote_render_report")
    footnotes = footnotes if isinstance(footnotes, dict) else {}
    if (
        expected == 0
        and footnotes.get("policy") == "evidence-footnotes-v1"
        and (
            int(footnotes.get("terminal_bibliography_addition_count") or 0) > 0
            or int(footnotes.get("cited_reference_count") or 0) > 0
        )
    ):
        return 1
    authoring_policy = context.get("authoring_workflow_policy")
    authoring_policy = authoring_policy if isinstance(authoring_policy, dict) else {}
    reference_render = context.get("reference_render_report")
    reference_render = reference_render if isinstance(reference_render, dict) else {}
    source_reference_count = int(
        reference_render.get("source_reference_count") or 0
    )
    if (
        expected > 0
        and int(authoring_policy.get("workflow_version") or 0) >= 31
        and footnotes.get("policy") == "evidence-footnotes-v1"
        and int(footnotes.get("cited_reference_count") or 0) == 0
        and int(footnotes.get("terminal_bibliography_addition_count") or 0) == 0
        and reference_render.get("rule_set_id")
        and int(reference_render.get("entry_count") or 0) == 0
        and reference_render.get("machine_section_rendered") is False
        and source_reference_count > 0
        and int(reference_render.get("uncited_reference_review_count") or 0)
        == source_reference_count
    ):
        return 0
    return expected


def element_texts(html: str) -> dict[str, str]:
    # Container fragments include only their own opening tag.  Serializing the
    # entire subtree counts every legitimate child edit again at article/section.
    return {block_id: fragment for block_id, _, fragment in structural_block_fragments(html)}


def rendered_structure_report(
    html: str,
    *,
    boundary_lock: dict[str, Any] | None = None,
    protected_html: str | None = None,
    protected_ids: list[str] | None = None,
    human_protected_html: str | None = None,
    human_protected_ids: list[str] | None = None,
    source_html: str | None = None,
    source_lineage: list[dict[str, Any]] | None = None,
    reference_html: str | None = None,
    expected_reference_count: int | None = None,
    human_review: str = "",
) -> dict[str, Any]:
    sha = digest(html)
    canonical = CanonicalHTMLValidator().validate(html)
    issues = [
        {"code": issue.code, "message": issue.message, "block_id": issue.element_id}
        for issue in canonical.issues
        if issue.severity == "error"
    ]
    if not canonical.valid:
        return {
            "policy": POLICY,
            "html_sha256": sha,
            "valid": False,
            "issues": issues,
            "block_ids": [],
            "block_count": 0,
        }
    structure = ManuscriptCoherenceService.semantic_structure(html)
    manifest = ManuscriptCoherenceService.document_manifest(html)
    terminal = ManuscriptCoherenceService.terminal_contract(
        html,
        human_review=human_review,
        expected_reference_section_count=expected_reference_count,
    )
    issues.extend(dict(item) for item in terminal.get("issues") or [])
    ids = [str(block["id"]) for block in structure["blocks"]]
    if len(ids) != len(set(ids)) or set(ids) != set(manifest["observable_block_ids"]):
        issues.append({"code": "structure.block_coverage_invalid"})
    if protected_html:
        before_ids = {
            str(block["id"])
            for block in ManuscriptCoherenceService.semantic_structure(protected_html)["blocks"]
        }
        missing = sorted(before_ids - set(ids))
        if missing:
            issues.append({"code": "structure.source_block_missing", "block_ids": missing})
    lineage_report: dict[str, Any] | None = None
    if source_html:
        source_ids = {
            str(block["id"])
            for block in ManuscriptCoherenceService.semantic_structure(source_html)["blocks"]
        }
        mapped = {
            str(block_id)
            for item in source_lineage or []
            if isinstance(item, dict) and str(item.get("output_block_id") or "") in ids
            for block_id in item.get("source_block_ids") or []
        }
        missing = sorted(source_ids - set(ids))
        unmapped = sorted(set(missing) - mapped)
        lineage_report = {
            "source_block_count": len(source_ids),
            "retained_block_count": len(source_ids & set(ids)),
            "mapped_removed_block_ids": sorted(set(missing) & mapped),
            "unmapped_removed_block_ids": unmapped,
        }
        if unmapped:
            issues.append({"code": "structure.unmapped_source_block", "block_ids": unmapped})
    if boundary_lock:
        lock = ManuscriptDiscourseService.validate_rendered_boundary_lock(html, boundary_lock)
        if not lock["valid"]:
            issues.append({"code": "paragraph.boundary_lock_violated", "details": lock["issues"]})
    else:
        lock = {"valid": True, "issues": []}
    current = element_texts(html)
    for source_html, ids_to_protect, code in (
        (protected_html, protected_ids or [], "structure.protected_block_changed"),
        (
            human_protected_html,
            human_protected_ids or [],
            "structure.human_protected_block_changed",
        ),
        (
            reference_html,
            manifest["reference_block_ids"] if reference_html else [],
            "structure.read_only_reference_changed",
        ),
    ):
        if not source_html:
            continue
        before = element_texts(source_html)
        for block_id in ids_to_protect:
            if before.get(block_id) != current.get(block_id):
                issues.append({"code": code, "block_id": block_id})
    return {
        "policy": POLICY,
        "html_sha256": sha,
        "valid": not issues,
        "issues": issues,
        "block_ids": ids,
        "block_count": len(ids),
        "manifest": manifest,
        "terminal_contract": terminal,
        "paragraph_boundary_lock": lock,
        "source_lineage": lineage_report,
    }


def section_slices(html: str, context: dict[str, Any]) -> list[dict[str, Any]]:
    """Project only each section's text, related plans, and linked evidence."""
    structure = ManuscriptCoherenceService.semantic_structure(html)
    blocks = {str(block["id"]): block for block in structure["blocks"]}
    plan = dict(context.get("structure_plan") or {})
    paragraph_plan = dict(context.get("paragraph_plan") or {})
    evidence = [
        dict(item) for item in context.get("evidence_context") or [] if isinstance(item, dict)
    ]
    realized = [
        dict(item) for item in context.get("realized_paragraphs") or [] if isinstance(item, dict)
    ]
    findings = [
        finding
        for review in context.get("reasoning_review") or []
        if isinstance(review, dict)
        for finding in review.get("findings") or []
        if isinstance(finding, dict)
    ]
    section_contracts = [
        dict(item)
        for item in context.get("section_contracts") or []
        if isinstance(item, dict)
    ]
    authoring_claims = [
        dict(item) for item in context.get("claims") or [] if isinstance(item, dict)
    ]
    authoring_nodes = [
        dict(item)
        for item in context.get("argument_nodes") or []
        if isinstance(item, dict)
    ]
    authoring_propositions = [
        dict(item)
        for item in context.get("propositions") or []
        if isinstance(item, dict)
    ]
    atomic_contracts = [
        dict(item)
        for item in context.get("authoring_atomic_evidence_contracts") or []
        if isinstance(item, dict)
    ]
    slices = []
    for section in structure["sections"]:
        ids = [str(value) for value in section["block_ids"]]
        related_realized = [item for item in realized if item.get("output_block_id") in ids]
        source_ids = set(ids)
        for item in related_realized:
            source_ids.update(str(value) for value in item.get("source_block_ids") or [])
        related = [
            item
            for item in plan.get("sections") or []
            if isinstance(item, dict)
            and (
                item.get("section_id") == section["section_id"]
                or item.get("heading_block_id") == section.get("heading_block_id")
                or set(item.get("ordered_block_ids") or []) & source_ids
            )
        ]
        plan_slice = {"sections": related}
        for key in (
            "argument_links",
            "block_roles",
            "block_temporal_scopes",
            "connective_relations",
            "link_dispositions",
            "reference_classifications",
            "temporal_mentions",
            "merge_groups",
        ):
            plan_slice[key] = [
                item
                for item in plan.get(key) or []
                if isinstance(item, dict)
                and (
                    any(
                        str(item.get(field) or "") in source_ids
                        for field in (
                            "block_id",
                            "source_block_id",
                            "target_claim_block_id",
                            "central_claim_block_id",
                            "target_block_id",
                        )
                    )
                    or bool(set(item.get("block_ids") or []) & source_ids)
                )
            ]
        planned_paragraph_ids = {str(item.get("paragraph_id")) for item in related_realized}
        paragraphs = [
            item
            for item in paragraph_plan.get("paragraphs") or []
            if isinstance(item, dict)
            and (
                str(item.get("paragraph_id")) in planned_paragraph_ids
                or any(
                    str(unit_id).split(":u", 1)[0] in source_ids
                    for unit_id in item.get("source_unit_ids") or []
                )
                or item.get("output_block_id") in ids
                or set(item.get("source_block_ids") or []) & set(ids)
            )
        ]
        source_unit_ids = {
            str(unit_id) for item in paragraphs for unit_id in item.get("source_unit_ids") or []
        }
        unit_annotations = [
            item
            for item in paragraph_plan.get("unit_annotations") or []
            if isinstance(item, dict) and str(item.get("unit_id")) in source_unit_ids
        ]
        links = set()
        queries = set()
        for item in [*related, *paragraphs, *plan_slice["argument_links"]]:
            for key in ("evidence_ids", "supporting_evidence_ids", "source_ids"):
                links.update(str(value) for value in item.get(key) or [])
            if item.get("research_query"):
                queries.add(str(item["research_query"]))
        linked_evidence = [
            item
            for item in evidence
            if str(item.get("id")) in links
            or str(item.get("query") or "") in queries
            or any(
                str(finding.get("id") or "") == str(item.get("finding_key") or "")
                and bool(set(finding.get("block_ids") or []) & source_ids)
                for finding in findings
            )
        ]
        rendered_section_id = str(section["section_id"])
        rendered_heading_id = str(section.get("heading_block_id") or "")

        def matches_authoring_section(
            item: dict[str, Any],
            section_id: str = rendered_section_id,
            heading_id: str = rendered_heading_id,
        ) -> bool:
            contract_section_id = str(item.get("section_id") or "")
            return bool(
                contract_section_id
                and (
                    contract_section_id in (section_id, heading_id)
                    or section_id.endswith(f"-{contract_section_id}")
                    or heading_id.endswith(f"-{contract_section_id}")
                )
            )

        authoring_contract = next(
            (
                item
                for item in section_contracts
                if matches_authoring_section(item)
            ),
            {},
        )
        authoring_claim_ids = {
            str(item) for item in authoring_contract.get("claim_ids") or [] if str(item)
        }
        authoring_argument_ids = {
            str(item)
            for key in ("evidence_ids", "warrant_ids", "qualifier_ids")
            for item in authoring_contract.get(key) or []
            if str(item)
        }
        related_authoring_claims = [
            item
            for item in authoring_claims
            if str(item.get("id") or "") in authoring_claim_ids
        ]
        related_authoring_nodes = [
            item
            for item in authoring_nodes
            if str(item.get("claim_id") or "") in authoring_claim_ids
            or str(item.get("id") or "") in authoring_argument_ids
        ]
        authoring_proposition_ids = {
            str(item.get("proposition_id") or "")
            for item in [*related_authoring_claims, *related_authoring_nodes]
            if str(item.get("proposition_id") or "")
        }
        for claim in related_authoring_claims:
            authoring_proposition_ids.update(
                str(item)
                for item in dict(claim.get("provenance") or {}).get("source_ids") or []
                if str(item)
            )
        related_authoring_propositions = [
            {
                key: item.get(key)
                for key in (
                    "id",
                    "text",
                    "source_span",
                    "provenance",
                    "memo_annotation_kinds",
                    "memo_annotations",
                )
                if key in item
            }
            for item in authoring_propositions
            if str(item.get("id") or "") in authoring_proposition_ids
        ]
        related_atomic_contracts = [
            item
            for item in atomic_contracts
            if str(item.get("proposition_id") or "") in authoring_proposition_ids
            or bool(authoring_claim_ids.intersection(item.get("claim_ids") or []))
        ]
        authoring_source_provenance = {
            "policy": "authoring-source-provenance-v31",
            "section_contract": authoring_contract,
            "claims": related_authoring_claims,
            "argument_nodes": related_authoring_nodes,
            "propositions": related_authoring_propositions,
            "atomic_evidence_contracts": related_atomic_contracts,
        }
        # Unlinked evidence is not silently discarded: its IDs are carried as an
        # explicit unresolved inventory for semantic adjudication.
        unlinked_ids = [str(item.get("id")) for item in evidence if item not in linked_evidence]
        slices.append(
            {
                "section_id": str(section["section_id"]),
                "role": str(section["document_role"]),
                "block_ids": ids,
                "blocks": [blocks[block_id] for block_id in ids],
                "structure_plan": plan_slice,
                "paragraph_plan": paragraphs,
                "unit_annotations": unit_annotations,
                "evidence": linked_evidence,
                "authoring_source_provenance": authoring_source_provenance,
                "unlinked_evidence_ids": unlinked_ids,
                "human_review": str(context.get("human_review") or ""),
            }
        )
    return slices


def unit_signature(
    unit: dict[str, Any], model_conditions: Any, *, policy: str = POLICY
) -> dict[str, str]:
    return {
        "policy": policy,
        "target_html_sha256": digest(unit["blocks"]),
        "adjacent_html_sha256": str(unit.get("adjacent_html_sha256") or ""),
        "plan_sha256": digest(
            [
                unit.get("structure_plan"),
                unit.get("paragraph_plan"),
                unit.get("unit_annotations"),
            ]
        ),
        "evidence_sha256": digest(
            [
                unit.get("evidence"),
                unit.get("authoring_source_provenance"),
                unit.get("unlinked_evidence_ids"),
            ]
        ),
        "human_review_sha256": digest(str(unit.get("human_review") or "")),
        "model_sha256": digest(model_conditions),
    }


def verified_coverage(
    records: list[dict[str, Any]], expected_ids: list[str], html_sha256: str
) -> bool:
    by_id = {str(record.get("block_id")): record for record in records}
    return (
        len(by_id) == len(expected_ids)
        and set(by_id) == set(expected_ids)
        and all(
            record.get("html_sha256") == html_sha256
            and set(record.get("categories") or []) == set(CATEGORIES)
            and record.get("verified") is True
            for record in by_id.values()
        )
    )
