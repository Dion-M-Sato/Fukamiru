"""Local NP-slot probing and deterministic branch construction for V3.1.

Instruct output is stored as a hypothesis only.  This module never treats it
as a language-quality or meaning-preservation verdict.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from fukamiru.revisions import japanese_analysis
from fukamiru.revisions import predicate_realization as v31
from fukamiru.revisions import structured_realization as v3

_REFERENCE = re.compile(r"\{([A-Z]\d{2})\}")
_PROTECTED = set(v3.PROTECTED_TERMS)


def _schema_check(schema: dict[str, Any], value: Any, path: str = "$inp") -> list[str]:
    """Validate the fixed schema subset without an online/package dependency."""
    errors: list[str] = []
    expected = schema.get("type")
    kinds = expected if isinstance(expected, list) else [expected]
    type_matches = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "null": value is None,
    }
    if expected is not None and not any(type_matches.get(kind, False) for kind in kinds):
        return [path + ":type_mismatch"]
    if isinstance(value, str):
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(path + ":maxLength")
        if "enum" in schema and value not in schema["enum"]:
            errors.append(path + ":enum")
    if isinstance(value, list):
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(path + ":maxItems")
        if "items" in schema:
            for index, item in enumerate(value):
                errors.extend(_schema_check(schema["items"], item, f"{path}[{index}]"))
    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        for required in schema.get("required") or []:
            if required not in value:
                errors.append(f"{path}.{required}:required")
        if schema.get("additionalProperties") is False:
            for extra in value.keys() - properties.keys():
                errors.append(f"{path}.{extra}:additional_property")
        for key, field in properties.items():
            if key in value:
                errors.extend(_schema_check(field, value[key], f"{path}.{key}"))
    return errors


def validate_probe(response: Any, schema: dict[str, Any], request: dict[str, Any]) -> list[str]:
    errors = _schema_check(schema, response)
    if errors or not isinstance(response, dict):
        return errors
    if response.get("np_id") != request["np_id"]:
        errors.append("np_id_mismatch")
    entity_ids = {row["entity_ref"] for row in request["entities"]}
    analysis_ids = [row["analysis_id"] for row in response["interpretations"]]
    proposal_ids = [row["proposal_id"] for row in response["proposals"]]
    if len(analysis_ids) != len(set(analysis_ids)):
        errors.append("duplicate_analysis_id")
    if len(proposal_ids) != len(set(proposal_ids)):
        errors.append("duplicate_proposal_id")
    for observation in response["observations"]:
        evidence = observation["evidence_text"]
        if evidence and evidence not in request["source_sentence"]:
            errors.append("observation_evidence_not_in_source")
    for interpretation in response["interpretations"]:
        for participant in interpretation["participants"]:
            if participant["entity_ref"] not in entity_ids:
                errors.append("unrecognized_interpretation_entity_ref")
    for proposal in response["proposals"]:
        if proposal["analysis_id"] not in analysis_ids:
            errors.append("unrecognized_analysis_id")
        refs = set(_REFERENCE.findall(proposal["np_template"]))
        if refs - entity_ids:
            errors.append("unrecognized_template_entity_ref")
        if "{" in _REFERENCE.sub("", proposal["np_template"]):
            errors.append("malformed_template_reference")
    return sorted(set(errors))


def _path_to_predicate(
    token: Any, predicate_start: int, predicate_end: int
) -> list[dict[str, Any]] | None:
    path = []
    current = token
    for _ in range(8):
        path.append(
            {
                "text": current.text,
                "start": current.idx,
                "role": current.dep_,
                "head_start": current.head.idx,
            }
        )
        if predicate_start <= current.idx < predicate_end:
            return path
        if current.head == current:
            break
        if current.pos_ in {"VERB", "AUX"} and current.idx != token.idx:
            break
        current = current.head
    return None


def extract_slots(site: dict[str, Any], source: dict[str, Any]) -> list[dict[str, Any]]:
    if source["status"] != "constructed":
        return []
    site_id = site.get("site_id") or site["construction_id"]
    sentence = source["source_sentence"]
    with japanese_analysis._LOCK:
        doc = japanese_analysis._engines()[1](sentence)
    noun_chunks = list(doc.noun_chunks)
    provisional = []
    for chunk in noun_chunks:
        if chunk.end_char > source["edit_span"][0] or len(chunk.text) < 2:
            continue
        path = _path_to_predicate(chunk.root, *source["edit_span"])
        if not path:
            continue
        local = v31.tokens(chunk.text)
        if not any(v3._pos(item) == "名詞" for item in local):
            continue
        protected_reasons = []
        if v3._inside_quote(sentence, chunk.start_char):
            protected_reasons.append("inside_quotation")
        if chunk.root.pos_ == "PROPN" or any(token.pos_ == "PROPN" for token in chunk):
            protected_reasons.append("proper_name")
        if any(term in chunk.text for term in _PROTECTED):
            protected_reasons.append("versioned_protected_term")
        if any(
            chunk.start_char < protected["span"][1]
            and protected["span"][0] < chunk.end_char
            for protected in source.get("protected_spans") or []
        ):
            protected_reasons.append("source_protected_span_overlap")
        sahen_compound = (
            len(local) >= 2
            and v3._is_sahen(local[0])
            and all(v3._pos(token) == "名詞" for token in local[:2])
        )
        noun_compound = len(chunk) >= 2 and any(token.dep_ == "compound" for token in chunk)
        if not (sahen_compound or noun_compound):
            continue
        provisional.append(
            {
                "site_id": site_id,
                "source_np_span": [chunk.start_char, chunk.end_char],
                "source_np_text": chunk.text,
                "head_noun": chunk.root.text,
                "source_relation_evidence": path,
                "structural_features": {
                    "sahen_noun_compound": sahen_compound,
                    "noun_compound": noun_compound,
                    "dependency_path_length": len(path),
                    "before_target_predicate": True,
                },
                "protected_reasons": protected_reasons,
                "probe_status": "pending",
            }
        )
    provisional.sort(
        key=lambda row: (
            bool(row["protected_reasons"]),
            not row["structural_features"]["sahen_noun_compound"],
            row["structural_features"]["dependency_path_length"],
            row["source_np_span"][0],
        )
    )
    for index, slot in enumerate(provisional):
        slot["np_id"] = f"NP{index + 1:02d}"
        slot["probe_status"] = (
            "protected"
            if slot["protected_reasons"]
            else "selected"
            if index < 2
            else "not_probed_budget"
        )
        entities = []
        for chunk in noun_chunks:
            if chunk.start_char == slot["source_np_span"][0]:
                continue
            path = _path_to_predicate(chunk.root, *source["edit_span"])
            if not path or chunk.text == slot["source_np_text"]:
                continue
            entities.append(
                {
                    "source_text": chunk.text,
                    "source_span": [chunk.start_char, chunk.end_char],
                    "syntactic_relation": chunk.root.dep_,
                    "scope_note": "observed same local predicate dependency chain",
                }
            )
        entities.sort(
            key=lambda row: (
                0 if row["syntactic_relation"] == "nsubj" else 1,
                0 if row["syntactic_relation"] == "obj" else 1,
                row["source_span"][0],
            )
        )
        slot["entities"] = [
            {"entity_ref": f"E{number:02d}", **entity}
            for number, entity in enumerate(entities[:4], 1)
        ]
        slot["slot_id"] = v3.stable_id(
            "np-v31-", site_id, slot["source_np_span"], slot["source_np_text"]
        )
    return provisional


def probe_input(
    site: dict[str, Any], source: dict[str, Any], slot: dict[str, Any]
) -> dict[str, Any]:
    return {
        "np_id": slot["np_id"],
        "domain_or_section": site.get("section_heading"),
        "source_sentence": source["source_sentence"],
        "previous_sentence": site.get("previous_sentence"),
        "next_sentence": site.get("next_sentence"),
        "target_np": slot["source_np_text"],
        "target_predicate": source["E0"],
        "observed_connections": slot["source_relation_evidence"],
        "entities": [
            {
                "entity_ref": row["entity_ref"],
                "source_text": row["source_text"],
                "scope_note": row["scope_note"],
            }
            for row in slot["entities"]
        ],
        "protected_spans_and_terms": slot["protected_reasons"],
        "existing_definitions": [],
    }


def prompt_sections(path: Path) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8")
    system = text.split("## SYSTEM\n", 1)[1].split("\n## INPUT", 1)[0].strip()
    if not system or "あなたは日本語" not in system:
        raise RuntimeError("attached_np_prompt_structure_changed")
    return system, hashlib.sha256(text.encode("utf-8")).hexdigest()


def realize_expansion(
    site: dict[str, Any],
    source: dict[str, Any],
    slot: dict[str, Any],
    response: dict[str, Any],
    proposal: dict[str, Any],
) -> dict[str, Any]:
    site_id = site.get("site_id") or site["construction_id"]
    plan: dict[str, Any] = {
        "plan_id": v3.stable_id("np-plan-v31-", slot["slot_id"], proposal["proposal_id"]),
        "site_id": site_id,
        "np_id": slot["np_id"],
        "original_np_span": slot["source_np_span"],
        "original_np_text": slot["source_np_text"],
        "head_noun": slot["head_noun"],
        "source_entity_refs": slot["entities"],
        "source_relation_evidence": slot["source_relation_evidence"],
        "instruct_hypothesis": response,
        "branch_advice": response["branch_advice"],
        "expansion_text_or_template": proposal["np_template"],
        "mentioned_entity_bindings": {},
        "internal_event": {
            "voice": proposal["internal_voice"],
            "proposal_id": proposal["proposal_id"],
        },
        "participant_roles": [],
        "scope": {"internal_event": "inside_np", "main_predicate": "unchanged"},
        "edit_operations": [],
        "local_edit_envelope": None,
        "unchanged_anchors": [source["E0"], source["R"]],
        "referential_correspondence": [],
        "quantifier_scope": "original_unmodified",
        "structural_evidence": slot["structural_features"],
        "unresolved_content_notes": list(proposal["meaning_risks"]),
        "status": "pending",
        "reason": None,
    }

    def hold(reason: str) -> dict[str, Any]:
        plan.update(status="held", reason=reason)
        return plan

    if slot["protected_reasons"]:
        return hold("protected_np")
    if proposal["placement"] != "in_place":
        return hold("local_reordering_not_implemented:" + proposal["placement"])
    sentence = source["source_sentence"]
    start, end = slot["source_np_span"]
    if sentence[start:end] != slot["source_np_text"]:
        return hold("np_source_span_mismatch")
    template = proposal["np_template"]
    refs = set(_REFERENCE.findall(template))
    entities = {row["entity_ref"]: row for row in slot["entities"]}
    if refs - entities.keys():
        return hold("unrecognized_entity_ref")
    if (
        "使用" in slot["source_np_text"]
        and refs
        and not any(
            re.search(re.escape("{" + ref + "}") + pattern, template)
            for ref in refs
            for pattern in (r"を使用する", r"が使用される")
        )
    ):
        return hold("use_relation_theme_role_not_preserved")
    expanded = _REFERENCE.sub(lambda match: entities[match.group(1)]["source_text"], template)
    if expanded == slot["source_np_text"]:
        return hold("np_identity_proposal")
    for ref in refs:
        plan["mentioned_entity_bindings"][ref] = entities[ref]["source_text"]
    prefix = sentence[:start]
    suffix = sentence[end:]
    edit_start, edit_end = start, end
    if refs:
        duplicates = [
            entities[ref]
            for ref in refs
            if entities[ref]["source_span"][1] <= start and entities[ref]["source_text"] in prefix
        ]
        if duplicates:
            if len(duplicates) != 1:
                return hold("multiple_referential_duplicates_need_reordering")
            entity = duplicates[0]
            entity_start, entity_end = entity["source_span"]
            if (
                sentence[entity_end:start] != "が"
                or not entity["source_text"].startswith("各")
                or not suffix.startswith("のなかで")
            ):
                return hold("referential_duplicate_requires_unavailable_local_reorder")
            if proposal["extra_requirements"] and not any(
                "照応" in note or "主語" in note or "語順" in note
                for note in proposal["extra_requirements"]
            ):
                return hold("uninterpreted_extra_requirements")
            edit_start = entity_start
            edit_end = end + len("のなかで")
            replacement = expanded + "のなかで、それぞれが"
            prefix = sentence[:edit_start]
            suffix = sentence[edit_end:]
            plan["quantifier_scope"] = "each_entity_reintroduced_by_sorezore; human_unverified"
            plan["unresolved_content_notes"].append("referential_equivalence_not_human_verified")
            plan["referential_correspondence"] = [
                {
                    "source": entity["source_text"],
                    "inside_np": entity["source_text"],
                    "main_clause": "それぞれ",
                    "evidence": "local_each_subject_reconfiguration_rule",
                }
            ]
        else:
            if proposal["extra_requirements"]:
                return hold("uninterpreted_extra_requirements")
            replacement = expanded
    else:
        if proposal["extra_requirements"]:
            return hold("uninterpreted_extra_requirements")
        replacement = expanded
    branch_sentence = prefix + replacement + suffix
    old_start, old_end = source["edit_span"]
    if edit_end > old_start:
        return hold("np_edit_overlaps_main_predicate")
    shift = len(replacement) - (edit_end - edit_start)
    main_start, main_end = old_start + shift, old_end + shift
    if branch_sentence[main_start:main_end] != source["E0"]:
        return hold("main_predicate_changed_by_np_edit")
    branch_source = v31.analyze_source(
        branch_sentence,
        main_start,
        main_end,
        frame_id=source["construction_id"] + ":" + plan["plan_id"],
        source_snapshot_sha256=source.get("source_snapshot_sha256"),
    )
    if branch_source["status"] != "constructed":
        return hold("branch_main_predicate_reparse_unresolved:" + str(branch_source["reason"]))
    if branch_source["E0"] != source["E0"]:
        return hold("branch_main_predicate_expression_changed")
    source_operators = {tuple(row["operators"]) for row in source["interpretation_candidates"]}
    branch_operators = {
        tuple(row["operators"]) for row in branch_source["interpretation_candidates"]
    }
    if not source_operators & branch_operators:
        return hold("branch_main_predicate_function_scope_changed")
    plan.update(
        status="constructed",
        reason=None,
        expansion_text=expanded,
        edit_operations=[
            {"kind": "replace", "source_span": [edit_start, edit_end], "replacement": replacement}
        ],
        local_edit_envelope=[edit_start, edit_end],
        branch_baseline_sentence=branch_sentence,
        branch_main_predicate_span=[main_start, main_end],
        branch_source=branch_source,
        actual_generation_prefix=branch_source["P"],
        prefix_changed=branch_source["P"] != source["P"],
    )
    return plan


def select_branches(plans: list[dict[str, Any]], limit: int = 2) -> list[dict[str, Any]]:
    """Prefer structurally explicit proposals, then rotate NPs by source position."""
    ready = [plan for plan in plans if plan["status"] == "constructed"]
    ready.sort(
        key=lambda row: (
            bool(row["unresolved_content_notes"]),
            row["original_np_span"][0],
            row["np_id"],
            row["internal_event"]["proposal_id"],
        )
    )
    selected = []
    seen_np = set()
    for plan in ready:
        if plan["np_id"] not in seen_np and len(selected) < limit:
            selected.append(plan)
            seen_np.add(plan["np_id"])
    for plan in ready:
        if plan not in selected and len(selected) < limit:
            selected.append(plan)
    selected_ids = {id(plan) for plan in selected}
    for plan in ready:
        plan["status"] = "selected" if id(plan) in selected_ids else "not_selected_budget"
        if plan["status"] == "not_selected_budget":
            plan["reason"] = "np_branch_budget"
    return selected
