"""V26 manuscript finalization.  All model units are independently bounded."""

from __future__ import annotations

import json
from typing import Any

from fukamiru.documents.japanese_surface import JapaneseSurfaceQualityGate
from fukamiru.errors import CoherenceResolutionRequired
from fukamiru.revisions import prose_redraft, submission_validation
from fukamiru.revisions.coherence import ManuscriptCoherenceService
from fukamiru.revisions.references import JapaneseReferenceService
from fukamiru.revisions.rewrite import ManuscriptRewriteService
from fukamiru.storage.payloads import UnitCheckpoints


def _structure(
    worker: Any, context: dict[str, Any], html: str, *, reference_html: str | None = None
) -> dict[str, Any]:
    source_html: str | None = None
    if context.get("document_id") and context.get("base_revision_id"):
        with worker.databases.state.connect() as connection:
            source_html = str(
                ManuscriptRewriteService(connection).prepare(
                    document_id=str(context["document_id"]),
                    base_revision_id=str(context["base_revision_id"]),
                    outline_node_id=context.get("outline_node_id"),
                    source_proposal_id=context.get("source_proposal_id"),
                )["base_html"]
            )
    source_lineage = list(context.get("realized_paragraphs") or [])
    lineage_policy = str(context.get("source_lineage_policy") or "")
    paragraph_plan_policy = str(dict(context.get("paragraph_plan") or {}).get("policy") or "")
    if source_html and (
        lineage_policy == "authoring-whole-document-v1"
        or paragraph_plan_policy == "authoring-section-contract-v1"
    ):
        source_lineage = _complete_authoring_source_lineage(
            source_html=source_html,
            output_html=html,
            source_lineage=source_lineage,
        )
    return submission_validation.rendered_structure_report(
        html,
        boundary_lock=dict(context.get("paragraph_boundary_lock") or {}),
        protected_html=str(context.get("pre_polish_html") or "") or None,
        protected_ids=worker._string_list(context.get("source_preserve_block_ids")),
        source_html=source_html,
        source_lineage=source_lineage,
        reference_html=reference_html,
        expected_reference_count=submission_validation.expected_reference_section_count(context),
        human_review=str(context.get("human_review") or ""),
    )


def _complete_authoring_source_lineage(
    *, source_html: str, output_html: str, source_lineage: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Cover source blocks deterministically when V29 replaces the document outline.

    Authoring V29 is permitted to discard the imported section boundaries.  Its
    ordinary paragraph plan therefore cannot retain the original block IDs.  Map
    each otherwise-unmapped source block to an output block of the same structural
    kind so final validation records the wholesale rewrite instead of treating it
    as silent source loss.
    """

    source_blocks = list(ManuscriptCoherenceService.semantic_structure(source_html)["blocks"])
    output_blocks = list(ManuscriptCoherenceService.semantic_structure(output_html)["blocks"])
    mapped = {
        str(block_id)
        for item in source_lineage
        if isinstance(item, dict)
        for block_id in item.get("source_block_ids") or []
    }
    targets_by_kind = {
        True: [item for item in output_blocks if item.get("is_heading")],
        False: [item for item in output_blocks if not item.get("is_heading")],
    }
    counters = {True: 0, False: 0}
    completed = [dict(item) for item in source_lineage]
    for source in source_blocks:
        source_id = str(source.get("id") or "")
        if not source_id or source_id in mapped:
            continue
        kind = bool(source.get("is_heading"))
        targets = targets_by_kind[kind] or output_blocks
        if not targets:
            continue
        target = targets[counters[kind] % len(targets)]
        counters[kind] += 1
        completed.append(
            {
                "output_block_id": str(target.get("id") or ""),
                "source_block_ids": [source_id],
                "source_unit_ids": [],
                "role": "authoring_whole_document_lineage",
            }
        )
    return completed


def validate_rendered(worker: Any, context: dict[str, Any]) -> dict[str, Any]:
    html = str(context["proposed_html"])
    report = _structure(worker, context, html)
    # Structural errors created by rendering must be attributed to the renderer;
    # they are never sent to a semantic model as editorial uncertainty.
    if not report["valid"]:
        raise CoherenceResolutionRequired(
            "rendered_structure.invalid: " + json.dumps(report["issues"], ensure_ascii=False)
        )
    return {**context, "rendered_structure_v26": report, "reference_rendered_html_v26": html}


def _packets(html: str, context: dict[str, Any]) -> list[dict[str, Any]]:
    packets: list[dict[str, Any]] = []
    sections = submission_validation.section_slices(html, context)
    for section_index, section in enumerate(sections):
        adjacent = [
            sections[index]["blocks"]
            for index in (section_index - 1, section_index + 1)
            if 0 <= index < len(sections)
        ]
        section = {**section, "adjacent_html_sha256": submission_validation.digest(adjacent)}
        blocks = list(section["blocks"])
        if not blocks:
            continue
        # Related plan entries stay with their blocks. A large section is split
        # into contiguous units; no body or evidence text is shortened.
        current: list[dict[str, Any]] = []
        for block in blocks:
            trial = [*current, block]
            unit = {**section, "blocks": trial, "block_ids": [str(b["id"]) for b in trial]}
            unit["paragraph_plan"] = [
                p
                for p in section["paragraph_plan"]
                if p.get("output_block_id") in unit["block_ids"]
                or set(p.get("source_block_ids") or []) & set(unit["block_ids"])
            ]
            if current and len(json.dumps(unit, ensure_ascii=False)) > 48_000:
                packets.append(_packet(section, current))
                current = [block]
            else:
                current = trial
        if current:
            packets.append(_packet(section, current))
    return packets


def _packet(section: dict[str, Any], blocks: list[dict[str, Any]]) -> dict[str, Any]:
    ids = [str(block["id"]) for block in blocks]
    paragraph_plan = [
        p
        for p in section["paragraph_plan"]
        if p.get("output_block_id") in ids or set(p.get("source_block_ids") or []) & set(ids)
    ]
    packet = {
        **section,
        "blocks": blocks,
        "block_ids": ids,
        "paragraph_plan": paragraph_plan,
    }
    source_units = {
        str(unit_id)
        for item in packet["paragraph_plan"]
        for unit_id in item.get("source_unit_ids") or []
    }
    packet["unit_annotations"] = [
        item
        for item in section.get("unit_annotations") or []
        if str(item.get("unit_id")) in source_units
    ]
    provenance = dict(section.get("authoring_source_provenance") or {})
    claim_ids = {
        str(value) for plan in paragraph_plan for value in plan.get("claim_ids") or [] if str(value)
    }
    argument_ids = {
        str(value)
        for plan in paragraph_plan
        for key in (
            "evidence_ids",
            "argument_evidence_node_ids",
            "warrant_ids",
            "qualifier_ids",
            "counterargument_ids",
        )
        for value in plan.get(key) or []
        if str(value)
    }
    claims = [
        dict(item)
        for item in provenance.get("claims") or []
        if isinstance(item, dict) and str(item.get("id") or "") in claim_ids
    ]
    argument_nodes = [
        dict(item)
        for item in provenance.get("argument_nodes") or []
        if isinstance(item, dict)
        and (
            str(item.get("id") or "") in argument_ids
            or (
                str(item.get("node_type") or "") == "claim"
                and str(item.get("claim_id") or "") in claim_ids
            )
        )
    ]
    direct_proposition_ids = {
        str(item.get("proposition_id") or "")
        for item in [*claims, *argument_nodes]
        if str(item.get("proposition_id") or "")
    }
    source_proposition_ids = {
        str(value)
        for item in [*claims, *argument_nodes]
        for value in dict(item.get("provenance") or {}).get("source_ids") or []
        if str(value)
    }
    packet["authoring_source_provenance"] = {
        **provenance,
        "claims": claims,
        "argument_nodes": argument_nodes,
        "propositions": [
            dict(item)
            for item in provenance.get("propositions") or []
            if isinstance(item, dict) and str(item.get("id") or "") in direct_proposition_ids
        ],
        "atomic_evidence_contracts": [
            dict(item)
            for item in provenance.get("atomic_evidence_contracts") or []
            if isinstance(item, dict)
            and str(item.get("proposition_id") or "") in direct_proposition_ids
        ],
        "unprojected_source_proposition_ids": sorted(
            source_proposition_ids - direct_proposition_ids
        ),
    }
    paragraph_evidence_ids = {
        str(value)
        for plan in paragraph_plan
        for key in ("evidence_ids", "argument_evidence_node_ids")
        for value in plan.get(key) or []
        if str(value)
    }
    packet["evidence"] = [
        item
        for item in section.get("evidence") or []
        if str(item.get("id") or item.get("candidate_key") or "") in paragraph_evidence_ids
    ]
    if len(json.dumps(packet, ensure_ascii=False)) > 90_000:
        raise CoherenceResolutionRequired(
            "v26.prompt_component_too_large: section plan or a single block must be split upstream"
        )
    packet["unit_id"] = f"{section['section_id']}:{ids[0]}:{ids[-1]}"
    return packet


def _checked(payload: dict[str, Any], ids: list[str]) -> bool:
    checked = payload.get("checked_block_ids")
    categories = payload.get("coverage")
    return (
        isinstance(checked, list)
        and len(checked) == len(ids)
        and set(checked) == set(ids)
        and isinstance(categories, list)
        and len(categories) == len(submission_validation.CATEGORIES)
        and set(categories) == set(submission_validation.CATEGORIES)
        and isinstance(payload.get("issues"), list)
        and isinstance(payload.get("repairs"), list)
        and isinstance(payload.get("claims"), list)
        and all(
            isinstance(issue, dict)
            and issue.get("category") in submission_validation.CATEGORIES
            and issue.get("severity") in {"warning", "blocking"}
            and set(issue.get("block_ids") or []).issubset(ids)
            and bool(issue.get("block_ids"))
            and issue.get("resolution_route")
            in {
                "none",
                "bounded_rewrite",
                "claim_limitation",
                "additional_research",
                "human_review",
            }
            and (
                issue.get("resolution_route") != "additional_research"
                or bool(str(issue.get("research_query") or "").strip())
            )
            for issue in payload.get("issues") or []
        )
    )


def _claims_complete(
    payload: dict[str, Any],
    packet: dict[str, Any],
    *,
    valid_block_ids: frozenset[str] | None = None,
) -> bool:
    if packet["role"] in {"references", "notes"}:
        return True
    claims = payload.get("claims") or []
    ids = [str(item.get("block_id")) for item in claims if isinstance(item, dict)]
    return (
        len(ids) == len(packet["block_ids"])
        and set(ids) == set(packet["block_ids"])
        and all(
            isinstance(item, dict)
            and isinstance(item.get("claim"), str)
            and isinstance(item.get("temporal_scope"), str)
            and isinstance(item.get("depends_on"), list)
            and all(isinstance(value, str) for value in item["depends_on"])
            and isinstance(item.get("evidence_block_ids"), list)
            and all(isinstance(value, str) for value in item["evidence_block_ids"])
            and (
                valid_block_ids is None
                or set(item["depends_on"] + item["evidence_block_ids"]).issubset(valid_block_ids)
            )
            for item in claims
        )
    )


def _audit_packet(
    worker: Any,
    claimed: Any,
    packet: dict[str, Any],
    *,
    model_conditions: Any,
    force: bool = False,
    policy: str = submission_validation.POLICY,
    checkpoint_phase: str = "coherence:v26",
    validation_context: str = "",
    valid_block_ids: frozenset[str] | None = None,
) -> dict[str, Any]:
    signatures = submission_validation.unit_signature(packet, model_conditions, policy=policy)
    if validation_context:
        signatures["validation_context_sha256"] = submission_validation.digest(validation_context)
    signature = submission_validation.digest(signatures)
    if not force:
        with worker.databases.state.connect() as connection:
            cached = UnitCheckpoints(connection).load(claimed.run_id, checkpoint_phase, signature)
        result = cached.get(packet["unit_id"])
        if (
            isinstance(result, dict)
            and result.get("signatures") == signatures
            and _checked(dict(result.get("report") or {}), packet["block_ids"])
            and _claims_complete(
                dict(result.get("report") or {}), packet, valid_block_ids=valid_block_ids
            )
        ):
            return result
    unit = {key: value for key, value in packet.items() if key != "unit_id"}
    reports: list[dict[str, Any]] = []
    merge_first = False
    for attempt in range(2):
        if attempt == 0:
            selected = unit
        else:
            first_checked = (
                {
                    str(block_id)
                    for block_id in reports[0].get("checked_block_ids") or []
                    if str(block_id) in packet["block_ids"]
                }
                if reports
                else set()
            )
            merge_first = bool(first_checked and len(first_checked) < len(packet["block_ids"]))
            missing = [
                block_id for block_id in packet["block_ids"] if block_id not in first_checked
            ] or list(packet["block_ids"])
            selected = {
                **unit,
                "block_ids": missing,
                "blocks": [block for block in unit["blocks"] if block["id"] in missing],
                "supplement_for_ids": missing,
            }
        response = worker._invoke_manuscript_inference(
            claimed,
            "audit_final_coherence_v26",
            mode="section" if attempt == 0 else "section_supplement",
            unit=selected,
            categories=list(submission_validation.CATEGORIES),
            policy=policy,
        )
        if isinstance(response, dict):
            reports.append(response)
            if attempt == 1 and len(reports) == 2 and merge_first:
                first = reports[0]
                response = {
                    **response,
                    "checked_block_ids": list(
                        dict.fromkeys(
                            [
                                *first.get("checked_block_ids", []),
                                *response.get("checked_block_ids", []),
                            ]
                        )
                    ),
                    "coverage": list(
                        dict.fromkeys(
                            [
                                *first.get("coverage", []),
                                *response.get("coverage", []),
                            ]
                        )
                    ),
                    "issues": [*first.get("issues", []), *response.get("issues", [])],
                    "repairs": [*first.get("repairs", []), *response.get("repairs", [])],
                    "claims": [*first.get("claims", []), *response.get("claims", [])],
                }
            if _checked(response, packet["block_ids"]) and _claims_complete(
                response, packet, valid_block_ids=valid_block_ids
            ):
                result = {"signatures": signatures, "report": response}
                with worker.databases.state.connect() as connection:
                    connection.execute(
                        "INSERT OR REPLACE INTO manuscript_unit_checkpoints "
                        "(run_id,phase,signature,unit_id,payload_json) VALUES (?,?,?,?,?)",
                        (
                            claimed.run_id,
                            checkpoint_phase,
                            signature,
                            packet["unit_id"],
                            json.dumps(result, ensure_ascii=False),
                        ),
                    )
                    connection.commit()
                return result
    return {"signatures": signatures, "report": reports[-1] if reports else {}, "incomplete": True}


def _cross_audit(
    worker: Any,
    claimed: Any,
    claims: list[dict[str, Any]],
    html: str,
    model_conditions: Any,
    *,
    policy: str = submission_validation.POLICY,
    checkpoint_phase: str = "coherence_cross:v26",
    parallelism: int = 1,
    resume_partials: bool = False,
    group_original_supplements: bool = False,
) -> tuple[list[dict[str, Any]], bool]:
    structure = ManuscriptCoherenceService.semantic_structure(html)
    blocks = {str(block["id"]): block for block in structure["blocks"]}
    for claim in claims:
        if any(
            str(value) not in blocks
            for value in [
                *(claim.get("depends_on") or []),
                *(claim.get("evidence_block_ids") or []),
            ]
        ):
            raise CoherenceResolutionRequired("v26.relationship_summary_unknown_block_id")
    section_order = [str(s["section_id"]) for s in structure["sections"]]
    grouped = {
        section_id: [c for c in claims if c.get("section_id") == section_id]
        for section_id in section_order
    }
    claim_section = {str(item["block_id"]): str(item["section_id"]) for item in claims}
    section_pairs = list(zip(section_order, section_order[1:], strict=False))
    for item in claims:
        left = str(item["section_id"])
        for dependency in item.get("depends_on") or []:
            right = claim_section.get(str(dependency))
            if right and right != left:
                pair = tuple(sorted((left, right), key=section_order.index))
                if pair not in section_pairs:
                    section_pairs.append(pair)
    jobs: list[dict[str, Any]] = []
    for left, right in section_pairs:
        relationship = grouped[left] + grouped[right]
        if not relationship:
            continue
        # Keep every declared dependency in one component. Independent
        # components may share a bounded prompt; the section boundary gets an
        # explicit comparison when it falls between those prompts.
        by_id = {str(item["block_id"]): item for item in relationship}
        parent = {block_id: block_id for block_id in by_id}

        def root(block_id: str, parent: dict[str, str] = parent) -> str:
            while parent[block_id] != block_id:
                block_id = parent[block_id]
            return block_id

        for item in relationship:
            block_id = str(item["block_id"])
            for dependency in item.get("depends_on") or []:
                dependency = str(dependency)
                if dependency in parent:
                    parent[root(block_id)] = root(dependency)
        components: dict[str, list[dict[str, Any]]] = {}
        for item in relationship:
            components.setdefault(root(str(item["block_id"])), []).append(item)
        chunks: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        for component in components.values():
            if len(json.dumps(component, ensure_ascii=False)) > 45_000:
                if current:
                    chunks.append(current)
                    current = []
                part: list[dict[str, Any]] = []
                for item in component:
                    trial = [*part, item]
                    if part and len(json.dumps(trial, ensure_ascii=False)) > 45_000:
                        chunks.append(part)
                        part = [item]
                    else:
                        part = trial
                if part:
                    chunks.append(part)
                for item in component:
                    for dependency in item.get("depends_on") or []:
                        dependency = str(dependency)
                        if dependency in by_id and not any(
                            {str(item["block_id"]), dependency}
                            <= {str(member["block_id"]) for member in part}
                            for part in chunks
                        ):
                            chunks.append([item, by_id[dependency]])
                continue
            trial = [*current, *component]
            if current and len(json.dumps(trial, ensure_ascii=False)) > 45_000:
                chunks.append(current)
                current = list(component)
            else:
                current = trial
        if current:
            chunks.append(current)
        if (
            grouped[left]
            and grouped[right]
            and not any(
                any(item["section_id"] == left for item in chunk)
                and any(item["section_id"] == right for item in chunk)
                for chunk in chunks
            )
        ):
            chunks.append([grouped[left][-1], grouped[right][0]])
        if any(len(json.dumps(chunk, ensure_ascii=False)) > 90_000 for chunk in chunks):
            raise CoherenceResolutionRequired("v26.relationship_component_too_large")
        for chunk in chunks:
            ids = [str(item["block_id"]) for item in chunk]
            signatures = {
                "policy": policy,
                "target_html_sha256": submission_validation.digest(
                    [blocks[block_id] for block_id in ids]
                ),
                "relationship_sha256": submission_validation.digest([left, right, chunk]),
                "model_sha256": submission_validation.digest(model_conditions),
            }
            signature = submission_validation.digest(signatures)
            unit_id = f"{left}:{right}:{submission_validation.digest(ids)}"
            jobs.append(
                {
                    "left": left,
                    "right": right,
                    "chunk": chunk,
                    "ids": ids,
                    "signatures": signatures,
                    "signature": signature,
                    "unit_id": unit_id,
                }
            )

    def load_record(phase: str, signature: str, unit_id: str) -> dict[str, Any] | None:
        with worker.databases.state.connect() as connection:
            value = UnitCheckpoints(connection).load(claimed.run_id, phase, signature).get(unit_id)
        return dict(value) if isinstance(value, dict) else None

    def save_record(phase: str, signature: str, unit_id: str, payload: dict[str, Any]) -> None:
        with worker.databases.state.connect() as connection:
            UnitCheckpoints(connection).save(claimed.run_id, phase, signature, {unit_id: payload})

    def audit_relationship(_index: int, job: dict[str, Any]) -> dict[str, Any]:
        ids = list(job["ids"])
        signatures = dict(job["signatures"])
        signature = str(job["signature"])
        unit_id = str(job["unit_id"])
        cached = load_record(checkpoint_phase, signature, unit_id)
        if (
            cached is not None
            and cached.get("signatures") == signatures
            and _checked(dict(cached.get("report") or {}), ids)
            and not cached["report"].get("needs_original_block_ids")
            and all(
                block_id in blocks
                and submission_validation.digest(blocks[block_id]) == original_sha
                for block_id, original_sha in dict(
                    cached.get("original_block_hashes") or {}
                ).items()
            )
        ):
            return {"complete": True, "report": cached["report"]}

        response: dict[str, Any] | None = None
        if resume_partials:
            base = load_record(checkpoint_phase + "_base", signature, unit_id)
            if (
                base is not None
                and base.get("signatures") == signatures
                and _checked(dict(base.get("report") or {}), ids)
            ):
                response = dict(base["report"])
        if response is None:
            value = worker._invoke_manuscript_inference(
                claimed,
                "audit_final_coherence_v26",
                mode="cross_section",
                unit={
                    "related_sections": [job["left"], job["right"]],
                    "claims": job["chunk"],
                    "block_ids": ids,
                },
                categories=list(submission_validation.CATEGORIES),
                policy=policy,
            )
            if not isinstance(value, dict) or not _checked(value, ids):
                return {"complete": False, "report": None}
            response = value

        originals = [str(value) for value in response.get("needs_original_block_ids") or []]
        if not set(originals).issubset(blocks):
            return {"complete": False, "report": None}
        if resume_partials:
            save_record(
                checkpoint_phase + "_base",
                signature,
                unit_id,
                {"signatures": signatures, "report": response},
            )

        original_groups: list[list[str]] = []

        def related_to(original_ids: list[str]) -> list[dict[str, Any]]:
            wanted = set(original_ids)
            return [
                item
                for item in claims
                if str(item["block_id"]) in wanted
                or wanted.intersection(str(value) for value in item.get("depends_on") or [])
                or wanted.intersection(str(value) for value in item.get("evidence_block_ids") or [])
            ]

        for block_id in originals:
            if group_original_supplements and original_groups:
                trial = [*original_groups[-1], block_id]
                related_trial = related_to(trial)
                trial_unit = {
                    "related_sections": [job["left"], job["right"]],
                    "claims": related_trial,
                    "block_ids": list(
                        dict.fromkeys(
                            [
                                *trial,
                                *(str(item["block_id"]) for item in related_trial),
                            ]
                        )
                    ),
                    "original_blocks": [blocks[value] for value in trial],
                }
                if len(trial) <= 4 and len(json.dumps(trial_unit, ensure_ascii=False)) <= 90_000:
                    original_groups[-1] = trial
                    continue
            original_groups.append([block_id])
        supplements = []
        for original_group in original_groups:
            related = related_to(original_group)
            targeted_ids = list(
                dict.fromkeys(
                    [
                        *original_group,
                        *(str(item["block_id"]) for item in related),
                    ]
                )
            )
            targeted = {
                "related_sections": [job["left"], job["right"]],
                "claims": related,
                "block_ids": targeted_ids,
                "original_blocks": [blocks[block_id] for block_id in original_group],
            }
            if len(json.dumps(targeted, ensure_ascii=False)) > 90_000:
                raise CoherenceResolutionRequired("v26.original_component_too_large")
            supplement_signature = submission_validation.digest(
                [
                    policy,
                    signature,
                    original_group if group_original_supplements else original_group[0],
                    targeted,
                ]
            )
            supplement_record = (
                load_record(
                    checkpoint_phase + "_supplement",
                    supplement_signature,
                    unit_id,
                )
                if resume_partials
                else None
            )
            supplement = (
                dict(supplement_record.get("report") or {}) if supplement_record is not None else {}
            )
            if not _checked(supplement, targeted_ids) or supplement.get("needs_original_block_ids"):
                value = worker._invoke_manuscript_inference(
                    claimed,
                    "audit_final_coherence_v26",
                    mode="cross_section_originals",
                    unit=targeted,
                    categories=list(submission_validation.CATEGORIES),
                    policy=policy,
                )
                if (
                    not isinstance(value, dict)
                    or not _checked(value, targeted_ids)
                    or value.get("needs_original_block_ids")
                ):
                    return {"complete": False, "report": None}
                supplement = value
                if resume_partials:
                    save_record(
                        checkpoint_phase + "_supplement",
                        supplement_signature,
                        unit_id,
                        {"report": supplement},
                    )
            supplements.append(supplement)
        if supplements:
            response = {
                **response,
                "issues": [issue for item in supplements for issue in item["issues"]],
                "repairs": [repair for item in supplements for repair in item["repairs"]],
                "needs_original_block_ids": [],
            }
        save_record(
            checkpoint_phase,
            signature,
            unit_id,
            {
                "signatures": signatures,
                "report": response,
                "original_block_hashes": {
                    block_id: submission_validation.digest(blocks[block_id])
                    for block_id in originals
                },
            },
        )
        return {"complete": True, "report": response}

    relationship_results: list[dict[str, Any] | None] = [None] * len(jobs)

    def collect_relationship(index: int, _job: dict[str, Any], result: dict[str, Any]) -> None:
        relationship_results[index] = result

    if parallelism > 1:
        worker._run_ordered_parallel(
            claimed,
            jobs,
            parallelism=parallelism,
            operation=audit_relationship,
            consume=collect_relationship,
            cancellation_message="Coherence audit was cancelled between relationship units",
            thread_name_prefix="coherence-cross",
            operation_name="coherence_cross_audit",
            pool="codex_remote",
        )
    else:
        for index, job in enumerate(jobs):
            collect_relationship(index, job, audit_relationship(index, job))
    complete = all(
        isinstance(result, dict) and result.get("complete") is True
        for result in relationship_results
    )
    reports = [
        dict(result["report"])
        for result in relationship_results
        if isinstance(result, dict) and isinstance(result.get("report"), dict)
    ]
    return reports, complete


def audit_coherence(
    worker: Any,
    claimed: Any,
    context: dict[str, Any],
    *,
    stage: str,
    policy: str = submission_validation.POLICY,
    checkpoint_namespace: str = "v26",
    parallelism: int = 1,
    resume_cross_partials: bool = False,
    group_original_supplements: bool = False,
    resume_dirty_units: bool = False,
    skip_cross_when_sections_incomplete: bool = False,
    allow_repairs: bool = True,
) -> tuple[dict[str, Any], str]:
    html = str(context["proposed_html"])
    history = list(context.get("coherence_repair_history") or [])
    reports = list(context.get("manuscript_coherence_checks") or [])
    edits = list(context.get("applied_edits") or [])
    warnings = list(context.get("rewrite_warnings") or [])
    model_conditions = {
        "policy": policy,
        "execution_policy": context.get("manuscript_execution_policy"),
        "requested_model": worker.settings.translation.manuscript_codex_model,
        "reasoning_effort": "high",
        "engine_fingerprint": worker._language_engine_descriptor_v22().get("fingerprint"),
        "authorized_engine_fingerprint": context.get("authorized_engine_fingerprint"),
        "authorized_engine": context.get("authorized_manuscript_inference_engine"),
    }
    checkpoint_identity = {
        "policy": policy,
        "stage": stage,
        "source_html_sha256": submission_validation.digest(html),
        "plan_sha256": submission_validation.digest(
            [context.get("structure_plan"), context.get("paragraph_plan")]
        ),
        "evidence_sha256": submission_validation.digest(context.get("evidence_context")),
        "human_review_sha256": submission_validation.digest(str(context.get("human_review") or "")),
        "model_sha256": submission_validation.digest(model_conditions),
        "reference_html_sha256": submission_validation.digest(
            str(context.get("reference_rendered_html_v26") or "")
        ),
    }
    checkpoint_key = submission_validation.digest(checkpoint_identity)
    final_report: dict[str, Any] = {}
    dirty_sections: set[str] = set()
    candidate_checkpoint_phase = f"coherence_candidate:{checkpoint_namespace}"
    unit_checkpoint_phase = f"coherence:{checkpoint_namespace}"
    cross_checkpoint_phase = f"coherence_cross:{checkpoint_namespace}"
    with worker.databases.state.connect() as connection:
        prior = (
            UnitCheckpoints(connection)
            .load(claimed.run_id, candidate_checkpoint_phase, checkpoint_key)
            .get("state")
        )
    start_pass = 1
    if isinstance(prior, dict) and prior.get("identity") == checkpoint_identity:
        candidate = str(prior.get("candidate_html") or "")
        if candidate and submission_validation.digest(candidate) == prior.get("candidate_sha256"):
            if not _structure(
                worker,
                context,
                candidate,
                reference_html=str(context.get("reference_rendered_html_v26") or "") or None,
            )["valid"]:
                raise CoherenceResolutionRequired("v26.checkpoint_candidate_structure_invalid")
            html = candidate
            history = list(prior.get("repair_history") or history)
            reports = list(prior.get("reports") or reports)
            edits = list(prior.get("applied_edits") or edits)
            warnings = list(prior.get("warnings") or warnings)
            dirty_sections = set(prior.get("dirty_sections") or [])
            start_pass = (
                4 if prior.get("status") == "completed" else int(prior.get("round_index") or 0) + 1
            )
            final_report = dict(prior.get("report") or {})

    def save_candidate(round_index: int, status: str) -> None:
        payload = {
            "identity": checkpoint_identity,
            "status": status,
            "round_index": round_index,
            "candidate_html": html,
            "candidate_sha256": submission_validation.digest(html),
            "report": final_report,
            "reports": reports,
            "repair_history": history,
            "applied_edits": edits,
            "warnings": warnings,
            "dirty_sections": sorted(dirty_sections),
        }
        with worker.databases.state.connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO manuscript_unit_checkpoints "
                "(run_id,phase,signature,unit_id,payload_json) VALUES (?,?,?,?,?)",
                (
                    claimed.run_id,
                    candidate_checkpoint_phase,
                    checkpoint_key,
                    "state",
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            connection.commit()

    for pass_index in range(start_pass, 4):
        packets = _packets(html, context)
        checked: list[str] = []
        issues: list[dict[str, Any]] = []
        repairs: list[dict[str, Any]] = []
        claims: list[dict[str, Any]] = []
        complete = True
        packet_results: list[dict[str, Any] | None] = [None] * len(packets)
        dirty_section_ids = frozenset(dirty_sections)
        candidate_sha256 = submission_validation.digest(html)
        valid_block_ids = frozenset(
            str(block["id"])
            for block in ManuscriptCoherenceService.semantic_structure(html)["blocks"]
        )

        def audit_packet(
            _index: int,
            packet: dict[str, Any],
            _dirty_section_ids: frozenset[str] = dirty_section_ids,
            _candidate_sha256: str = candidate_sha256,
            _pass_index: int = pass_index,
            _valid_block_ids: frozenset[str] = valid_block_ids,
        ) -> dict[str, Any]:
            dirty = packet["section_id"] in _dirty_section_ids
            return _audit_packet(
                worker,
                claimed,
                packet,
                model_conditions=model_conditions,
                force=dirty and not resume_dirty_units,
                policy=policy,
                checkpoint_phase=unit_checkpoint_phase,
                validation_context=(
                    f"{_candidate_sha256}:{_pass_index}" if dirty and resume_dirty_units else ""
                ),
                valid_block_ids=_valid_block_ids,
            )

        def collect_packet(
            index: int,
            _packet: dict[str, Any],
            result: dict[str, Any],
            _packet_results: list[dict[str, Any] | None] = packet_results,
        ) -> None:
            _packet_results[index] = result

        if parallelism > 1:
            worker._run_ordered_parallel(
                claimed,
                packets,
                parallelism=parallelism,
                operation=audit_packet,
                consume=collect_packet,
                cancellation_message="Coherence audit was cancelled between section units",
                thread_name_prefix="coherence-section",
                operation_name="coherence_section_audit",
                pool="codex_remote",
            )
        else:
            for index, packet in enumerate(packets):
                collect_packet(index, packet, audit_packet(index, packet))

        for packet, result in zip(packets, packet_results, strict=True):
            if not isinstance(result, dict):
                complete = False
                continue
            payload = dict(result.get("report") or {})
            if not _checked(payload, packet["block_ids"]) or not _claims_complete(
                payload, packet, valid_block_ids=valid_block_ids
            ):
                complete = False
                continue
            checked.extend(packet["block_ids"])
            prefix = str(packet["unit_id"]) + ":"
            issues.extend(
                {**item, "id": prefix + str(item.get("id") or "")}
                for item in payload["issues"]
                if isinstance(item, dict)
            )
            repairs.extend(
                {**item, "issue_id": prefix + str(item.get("issue_id") or "")}
                for item in payload["repairs"]
                if isinstance(item, dict)
            )
            claims.extend(
                {**item, "section_id": packet["section_id"]}
                for item in payload["claims"]
                if isinstance(item, dict)
            )
        if complete or not skip_cross_when_sections_incomplete:
            cross, cross_complete = _cross_audit(
                worker,
                claimed,
                claims,
                html,
                model_conditions,
                policy=policy,
                checkpoint_phase=cross_checkpoint_phase,
                parallelism=parallelism,
                resume_partials=resume_cross_partials,
                group_original_supplements=group_original_supplements,
            )
        else:
            cross, cross_complete = [], False
        complete = complete and cross_complete
        for cross_index, payload in enumerate(cross):
            prefix = f"cross:{cross_index}:"
            issues.extend(
                {**item, "id": prefix + str(item.get("id") or "")}
                for item in payload["issues"]
                if isinstance(item, dict)
            )
            repairs.extend(
                {**item, "issue_id": prefix + str(item.get("issue_id") or "")}
                for item in payload["repairs"]
                if isinstance(item, dict)
            )
        expected = [
            str(block["id"])
            for block in ManuscriptCoherenceService.semantic_structure(html)["blocks"]
        ]
        complete = complete and len(checked) == len(expected) and set(checked) == set(expected)
        blocking = [item for item in issues if item.get("severity") == "blocking"]
        unresolved_routes = [
            item
            for item in issues
            if item.get("resolution_route") in {"additional_research", "human_review"}
        ]
        for issue in issues:
            issue.setdefault("recommendation", str(issue.get("explanation") or "Review issue"))
            issue.setdefault("verification_scope", "semantic_content")
        final_report = {
            "policy": policy,
            "stage": stage,
            "pass_index": pass_index,
            "html_sha256": submission_validation.digest(html),
            "passed": complete and not blocking and not unresolved_routes,
            "summary": "Application-verified bounded section and adjacent-section audit",
            "checked_block_ids": checked,
            "coverage": [
                {"category": category, "checked": complete, "note": "bounded unit records"}
                for category in submission_validation.CATEGORIES
            ],
            "issues": issues,
            "repair_edits": [],
            "section_moves": [],
            "cross_section_reports": cross,
            "unit_complete": complete,
            "block_verification_records": [
                {
                    "block_id": block_id,
                    "html_sha256": submission_validation.digest(html),
                    "categories": list(submission_validation.CATEGORIES),
                    "verified": True,
                }
                for block_id in checked
            ],
        }
        if complete:
            violations = worker._coherence_contract_issues(
                final_report,
                expected_block_ids=set(expected),
                editable_block_ids=set(
                    ManuscriptCoherenceService.document_manifest(html)["editable_block_ids"]
                ),
            )
            final_report["contract_issues"] = violations
            if violations:
                raise CoherenceResolutionRequired(
                    "v26.semantic_response_contract_invalid: "
                    + json.dumps(violations, ensure_ascii=False)
                )
        reports.append(final_report)
        if not complete or not blocking or pass_index == 3 or not allow_repairs:
            break
        issue_by_id = {str(item.get("id")): item for item in blocking}
        manifest = ManuscriptCoherenceService.document_manifest(html)
        editable = set(manifest["editable_block_ids"])
        if stage == "post_polish" and context.get("pre_polish_html"):
            before = submission_validation.element_texts(str(context["pre_polish_html"]))
            current = submission_validation.element_texts(html)
            pre_issues = {
                str(block_id)
                for prior in reports
                if prior.get("stage") == "pre_polish"
                for issue in prior.get("issues") or []
                if issue.get("severity") == "blocking"
                for block_id in issue.get("block_ids") or []
            }
            regression_ids = {
                str(block_id)
                for issue in blocking
                for block_id in issue.get("block_ids") or []
                if block_id in editable
                and block_id in before
                and before[block_id] != current.get(block_id)
                and block_id not in pre_issues
            }
            if regression_ids:
                restored_html, restored = ManuscriptCoherenceService.restore_blocks(
                    str(context["pre_polish_html"]), html, regression_ids
                )
                if restored and restored_html != html:
                    structural = _structure(
                        worker,
                        context,
                        restored_html,
                        reference_html=str(context.get("reference_rendered_html_v26") or "")
                        or None,
                    )
                    if not structural["valid"]:
                        raise CoherenceResolutionRequired("v26.restoration_structure_invalid")
                    history.append(
                        {
                            "pass_index": pass_index,
                            "kind": "restore_pre_polish_blocks",
                            "target_ids": sorted(restored),
                            "input_sha256": submission_validation.digest(html),
                            "output_sha256": submission_validation.digest(restored_html),
                            "outcome": "applied",
                        }
                    )
                    html = restored_html
                    section_ids = [
                        str(section["section_id"])
                        for section in ManuscriptCoherenceService.semantic_structure(html)[
                            "sections"
                        ]
                    ]
                    changed_sections = {
                        packet["section_id"]
                        for packet in packets
                        if set(packet["block_ids"]) & set(restored)
                    }
                    dirty_sections = set(changed_sections)
                    for section_id in changed_sections:
                        index = section_ids.index(section_id)
                        dirty_sections.update(section_ids[max(0, index - 1) : index + 2])
                    save_candidate(pass_index, "progress")
                    continue
        proposed = []
        for item in repairs:
            block_id = str(item.get("block_id") or "")
            issue_id = str(item.get("issue_id") or "")
            issue = issue_by_id.get(issue_id)
            if (
                block_id not in editable
                or issue is None
                or block_id not in issue.get("block_ids", [])
                or issue.get("resolution_route") not in {"bounded_rewrite", "claim_limitation"}
            ):
                continue
            proposed.append(
                {
                    "operation": "replace_text",
                    "block_id": block_id,
                    "anchor_block_id": None,
                    "text": str(item.get("text") or ""),
                    "review_finding_ids": [issue_id],
                    "evidence_ids": list(item.get("evidence_ids") or []),
                }
            )
        if not proposed:
            break
        normalized, normalize_warnings = worker._normalize_manuscript_edits(
            proposed,
            reasoning_review=[
                *list(context.get("reasoning_review") or []),
                {
                    "findings": [
                        {
                            "id": issue_id,
                            "block_ids": issue["block_ids"],
                            "requires_research": False,
                        }
                        for issue_id, issue in issue_by_id.items()
                    ]
                },
            ],
            evidence_context=list(context.get("evidence_context") or []),
            allowed_operations={"replace_text"},
            allowed_block_ids=editable,
            allow_evidence_cross_finding=True,
        )
        previous_identities = {str(item.get("identity")) for item in history}
        accepted = []
        audits = []
        for item in normalized:
            audit = worker._coherence_repair_history_record(
                html, item, pass_index=pass_index, kind="v26_final_repair"
            )
            if audit["identity"] in previous_identities:
                audit["outcome"] = "repeated"
            else:
                accepted.append(item)
            audits.append(audit)
        if not accepted:
            history.extend(audits)
            break
        with worker.databases.state.connect() as connection:
            changed_html, applied, repair_warnings = ManuscriptRewriteService(
                connection
            ).apply_edits(html, accepted, run_id=claimed.run_id, batch_index=30_000 + pass_index)
        guarded = JapaneseSurfaceQualityGate.guard_canonical_html(html, changed_html)
        candidate = guarded.canonical_html
        structural = _structure(
            worker,
            context,
            candidate,
            reference_html=str(context.get("reference_rendered_html_v26") or "") or None,
        )
        if not structural["valid"]:
            raise CoherenceResolutionRequired(
                "v26.repair_structure_invalid: "
                + json.dumps(structural["issues"], ensure_ascii=False)
            )
        actual = {str(item.get("block_id")) for item in applied}
        reverted = {str(item.get("block_id")) for item in guarded.reverted_blocks}
        for audit in audits:
            if audit.get("outcome") == "pending":
                audit["outcome"] = (
                    "applied" if audit["target_id"] in actual - reverted else "rejected"
                )
        history.extend(audits)
        if candidate == html:
            break
        edits.extend(item for item in applied if str(item.get("block_id")) not in reverted)
        warnings.extend([*normalize_warnings, *repair_warnings])
        changed_ids = actual - reverted
        section_ids = [
            str(section["section_id"])
            for section in ManuscriptCoherenceService.semantic_structure(html)["sections"]
        ]
        changed_sections = {
            packet["section_id"] for packet in packets if set(packet["block_ids"]) & changed_ids
        }
        dirty_sections = set(changed_sections)
        for section_id in changed_sections:
            index = section_ids.index(section_id)
            dirty_sections.update(section_ids[max(0, index - 1) : index + 2])
        for claim in claims:
            if set(claim.get("depends_on") or []) & changed_ids:
                dirty_sections.add(str(claim.get("section_id") or ""))
        html = candidate
        save_candidate(pass_index, "progress")
        # A successful edit is not a successful audit. The next pass validates
        # this exact candidate before the node may return.
    final_report["passed"] = bool(
        final_report["unit_complete"]
        and not any(item.get("severity") == "blocking" for item in final_report["issues"])
        and not any(
            item.get("resolution_route") in {"additional_research", "human_review"}
            for item in final_report["issues"]
        )
    )
    updated = {
        **context,
        "proposed_html": html,
        "applied_edits": edits,
        "rewrite_warnings": warnings,
        "coherence_repair_history": history,
        "manuscript_coherence_checks": reports,
        "v26_coherence_report": final_report,
        "v26_block_verification_records": final_report["block_verification_records"],
    }
    if not final_report["passed"]:
        unresolved_routes = [
            dict(item)
            for item in final_report["issues"]
            if item.get("severity") == "blocking"
            or item.get("resolution_route") in {"additional_research", "human_review"}
        ]
        route_counts: dict[str, int] = {}
        for item in unresolved_routes:
            route = str(item.get("resolution_route") or "unknown")
            route_counts[route] = route_counts.get(route, 0) + 1
        research_requirements = [
            item
            for item in unresolved_routes
            if item.get("resolution_route") == "additional_research"
        ]
        research_questions = [
            str(item.get("research_query") or "").strip()
            for item in research_requirements
            if str(item.get("research_query") or "").strip()
        ]
        conditional = {
            "status": "conditional_success",
            "reason": "coherence_unresolved",
            "stage": stage,
            "pass_index": int(final_report["pass_index"]),
            "candidate_sha256": submission_validation.digest(html),
            "research_requirements": research_requirements,
            "unresolved_routes": unresolved_routes,
            "route_counts": route_counts,
        }
        updated.update(
            {
                "completion_status": "conditional_success",
                "conditional_success": conditional,
                "coherence_unresolved_routes": unresolved_routes,
                "additional_research_requirements": research_requirements,
                "unresolved_questions": list(
                    dict.fromkeys(
                        [
                            *list(context.get("unresolved_questions") or []),
                            *research_questions,
                        ]
                    )
                ),
            }
        )
    if (
        stage == "post_polish"
        and final_report["passed"]
        and dict(context.get("conditional_success") or {}).get("reason") == "coherence_unresolved"
    ):
        updated.pop("conditional_success", None)
        updated.pop("coherence_unresolved_routes", None)
        updated["completion_status"] = "success"
    if final_report["html_sha256"] != submission_validation.digest(html):
        raise CoherenceResolutionRequired("v26.last_edit_unverified")
    save_candidate(int(final_report["pass_index"]), "completed")
    return updated, "success" if final_report["passed"] else "conditional_success"


def freeze(worker: Any, context: dict[str, Any]) -> dict[str, Any]:
    html = str(context["proposed_html"])
    redrafted = context.get("prose_redraft_report_v31") is not None
    if redrafted:
        prose_redraft.validate_handoff(context)
    prior_surface = dict(context.get("manuscript_surface_quality_gate") or {})
    footnote_render = dict(context.get("evidence_footnote_render_report") or {})
    reference_render = dict(context.get("reference_render_report") or {})
    reference_surface = dict(reference_render.get("surface_quality") or {})
    surface_hash_chain_matches = bool(
        str(prior_surface.get("target_html_sha256") or "")
        and str(prior_surface.get("target_html_sha256") or "")
        == str(footnote_render.get("source_html_sha256") or "")
        and (not reference_render or reference_surface.get("passed") is True)
    )
    generated_sections_follow_validated_surface = bool(
        prior_surface.get("passed") is True and surface_hash_chain_matches
    )
    reviewable_surface = bool(
        redrafted or (context.get("economy_priority") is True and surface_hash_chain_matches)
    )
    # Footnotes and bibliography rows are added after the Japanese prose gate.
    # Their renderers have dedicated structure and surface checks, so comparing
    # the final document against ``pre_polish_html`` would misclassify those
    # generated sections as silent prose changes. Re-scan the complete final
    # surface while retaining the earlier hash chain for manuscript prose.
    source = (
        html
        if generated_sections_follow_validated_surface or reviewable_surface
        else str(context.get("pre_polish_html") or html)
    )
    # A rendered DOM redraft has already locked its exact candidate with
    # deterministic checks. Normalizing or restoring text here would invalidate
    # that candidate and could move note support.
    gate = None if redrafted else JapaneseSurfaceQualityGate.guard_canonical_html(source, html)
    remaining_surface_issues = JapaneseSurfaceQualityGate.find_canonical_html(html)
    if redrafted:
        from fukamiru.worker.runner import WorkerRunner

        reader_facing_findings = WorkerRunner._authoring_reader_facing_v31_findings(
            WorkerRunner._authoring_reader_facing_sections_from_html(html)
        )
    else:
        reader_facing_findings = []
    if gate is not None and (gate.canonical_html != html or gate.reverted_blocks):
        raise CoherenceResolutionRequired("v26.surface_candidate_requires_repair")
    if remaining_surface_issues and not reviewable_surface:
        raise CoherenceResolutionRequired("v26.surface_candidate_requires_repair")
    structure = _structure(
        worker,
        context,
        html,
        reference_html=str(context.get("reference_rendered_html_v26") or "") or None,
    )
    if not structure["valid"]:
        raise CoherenceResolutionRequired(
            "v26.freeze_structure_invalid: " + json.dumps(structure["issues"], ensure_ascii=False)
        )
    coherence = dict(context.get("v26_coherence_report") or {})
    if coherence.get("html_sha256") != submission_validation.digest(html):
        raise CoherenceResolutionRequired("v26.freeze_coherence_hash_mismatch")
    reference_quality = dict(context.get("reference_quality_gate") or {})
    if reference_quality and reference_quality.get(
        "target_html_sha256"
    ) != submission_validation.digest(html):
        entries = [
            dict(item) for item in context.get("reference_entries") or [] if isinstance(item, dict)
        ]
        machine_entries = [
            dict(item)
            for item in context.get("machine_reference_entries") or []
            if isinstance(item, dict)
        ]
        reference_quality = JapaneseReferenceService.quality_report(
            html, entries, workflow_version=26, machine_entries=machine_entries
        )
        if (
            int(dict(context.get("reference_inventory") or {}).get("reference_section_count") or 0)
            and not entries
        ):
            reference_quality["issues"] = [
                *list(reference_quality.get("issues") or []),
                {
                    "code": "reference.section_without_entries",
                    "severity": "error",
                    "reference_id": None,
                },
            ]
            reference_quality["issue_count"] = len(reference_quality["issues"])
            reference_quality["passed"] = False
        reference_quality["target_html_sha256"] = submission_validation.digest(html)
    updated = {
        **context,
        "frozen_candidate_v26": {
            "policy": submission_validation.POLICY,
            "html_sha256": submission_validation.digest(html),
            "structure": structure,
        },
        "final_quality_sha256": submission_validation.digest(html),
        "reference_quality_gate": reference_quality,
        "manuscript_surface_quality_gate": (
            {
                **prior_surface,
                "passed": True,
                "review_required": bool(remaining_surface_issues or reader_facing_findings),
                "remaining_issue_blocks": list(remaining_surface_issues),
                "reader_facing_review_findings": reader_facing_findings,
                "remaining_issue_count": sum(
                    len(item.get("issues") or []) for item in remaining_surface_issues
                ),
                "target_html_sha256": submission_validation.digest(html),
            }
            if redrafted
            else {
                **prior_surface,
                "passed": False,
                "review_required": True,
                "target_html_sha256": submission_validation.digest(html),
            }
            if reviewable_surface and remaining_surface_issues
            else {
                **gate.as_dict(),
                "passed": True,
                "review_required": False,
                "remaining_issue_blocks": [],
                "remaining_issue_count": 0,
                "target_html_sha256": submission_validation.digest(html),
            }
        ),
    }
    if reviewable_surface and remaining_surface_issues:
        required = [
            dict(item)
            for item in context.get("required_user_information") or []
            if isinstance(item, dict)
        ]
        if not any(
            item.get("code") == "authoring.japanese_surface_review_required" for item in required
        ):
            required.append(
                {
                    "code": "authoring.japanese_surface_review_required",
                    "reason": (
                        "日本語表面品質ゲートに未解決候補が残っているため、"
                        "提出前に人間の確認が必要です。"
                    ),
                    "affected_scope": "japanese_surface_quality",
                    "issue_count": len(remaining_surface_issues),
                }
            )
        updated["required_user_information"] = required
    else:
        updated["required_user_information"] = [
            dict(item)
            for item in context.get("required_user_information") or []
            if isinstance(item, dict)
            and item.get("code") != "authoring.japanese_surface_review_required"
        ]
    return updated
