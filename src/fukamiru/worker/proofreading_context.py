"""Two contextual model passes with SQLite checkpoints and deterministic application."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from xml.etree import ElementTree as ET

from fukamiru.authoring.codex import _validate_codex_output_schema
from fukamiru.documents.canonical_html import CanonicalHTMLValidator
from fukamiru.documents.japanese_surface import JapaneseSurfaceQualityGate
from fukamiru.errors import NonRetryableFukamiruError
from fukamiru.revisions import proofreading_context as rules
from fukamiru.revisions import proofreading_local as local
from fukamiru.revisions import sentence_proofreading as legacy
from fukamiru.storage.proofreading_generations import generation_id
from fukamiru.worker import sentence_proofreading as pipeline


def prepare(worker: Any, claimed: Any, context: dict[str, Any]) -> dict[str, Any]:
    source = str(context["proposed_html"])
    CanonicalHTMLValidator().assert_valid(source)
    protected = set(context.get("source_preserve_block_ids") or []) | set(
        context.get("human_protected_block_ids") or []
    )
    protected.update(p.get("id") for p in ET.fromstring(source).iter("p") if len(p))
    for node in ET.fromstring(source).iter():
        if node.tag == "blockquote" or node.get("id") in protected:
            protected.update(p.get("id") for p in node.iter("p"))
    current = {**context, "source_preserve_block_ids": sorted(protected)}
    eligible = legacy.extract_sentences(
        source,
        run_id=claimed.run_id,
        document_id=str(context["document_id"]),
        source_preserve_block_ids=protected,
    )
    if eligible:
        current = pipeline.prepare_sentences(worker, claimed, current)
        with worker.databases.state.connect() as connection:
            sentences = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM manuscript_proofreading_sentences WHERE run_id=? AN"
                    "D generation_id=? "
                    "AND status!='segmentation_superseded' ORDER BY block_id,sentence_ordinal",
                    (claimed.run_id, generation_id(current)),
                )
            ]
    else:
        sentences = []
    if "source_preserve_block_ids" in context:
        current["source_preserve_block_ids"] = context["source_preserve_block_ids"]
    else:
        current.pop("source_preserve_block_ids", None)
    current.pop("_proofreading_local_contract", None)
    for name in (
        "manuscript_proofreading_generation",
        "manuscript_proofreading_preprocessing",
        "manuscript_proofreading_luna",
        "manuscript_proofreading_ranking",
        "manuscript_proofreading_selection",
        "manuscript_proofreading_shadow",
        "manuscript_surface_quality_gate",
        "_node14_checkpoint",
    ):
        current.pop(name, None)
    current["manuscript_proofreading_versions"] = {
        "generation_prompt_version": rules.CONTRACT,
        "review_prompt_version": rules.CONTRACT,
        "generation_model": rules.MODEL,
        "review_model": rules.MODEL,
        "reasoning_effort": rules.EFFORT,
    }
    frozen = rules.plan(source, sentences)
    frozen["enabled"] = bool(context.get("use_llm_jp", True))
    if not frozen["enabled"]:
        frozen["sections"] = []
    return {
        **current,
        "pre_proofreading_html": source,
        "japanese_proofreading_profile": rules.PROFILE,
        "japanese_proofreading_context": {"plan": frozen, "proposals": [], "reviews": []},
    }


def _state(context: dict[str, Any]) -> dict[str, Any]:
    state = context["japanese_proofreading_context"]
    if state["plan"]["contract"] != rules.CONTRACT or state["plan"][
        "source_sha256"
    ] != legacy.digest(context["proposed_html"]):
        raise NonRetryableFukamiruError("proofreading.context_source_changed")
    return state


def _sentences(target: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for p in target["paragraphs"] for s in p["sentences"]]


def _call(
    worker: Any,
    claimed: Any,
    stage: str,
    prompt: str,
    payload: dict[str, Any],
    schema: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    adapter = getattr(worker, "llm_jp_proofreading", None)
    _validate_codex_output_schema(schema)
    managed_key = None
    if adapter is not None and hasattr(adapter, "cached_result"):
        managed_key = legacy.digest(json.dumps({
            "contract": "hf-section-cache-v1", "stage": stage, "prompt": prompt,
            "payload": payload, "schema": schema, "model_identity": adapter.model_identity,
            "source_sha256": context["japanese_proofreading_context"]["plan"]["source_sha256"],
            "effort": rules.EFFORT, "decoding": rules.DECODING,
            "budget": "verified-65536-context-minus-input-256",
        }, ensure_ascii=False, sort_keys=True))
        cached = adapter.cached_result(stage, managed_key)
        if cached is not None:
            return cached
    if adapter is None or not adapter.configured or adapter.model_name != rules.MODEL:
        raise NonRetryableFukamiruError("proofreading.requires_llmjp41_33b_bf16_high")
    maximum = worker.settings.translation.japanese_proofreading_max_output_tokens
    user = json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def preflight() -> tuple[dict[str, Any], int]:
        proof = adapter.verify_contextual_proofreading_backend()
        rendered = adapter._request_json(
            "/apply-template",
            {
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": user},
                ],
                "reasoning_effort": rules.EFFORT,
            },
            timeout=30,
        )["prompt"]
        return proof, len(adapter.tokenize(rendered))

    capacity = None
    if context.get("japanese_proofreading_profile") == "llmjp33b_adopt":
        # Use the verified native high-model capacity without mutating shared settings.
        capacity = preflight()
        maximum = capacity[0]["slot_context"] - capacity[1] - 256
        if maximum <= 0:
            raise NonRetryableFukamiruError("proofreading.context_window_exceeded")
    key = rules.fingerprint(
        stage, prompt, payload, schema, maximum, rules.EFFORT, rules.DECODING,
        adapter.model_identity,
    )
    with worker.databases.state.connect() as connection:
        rows = connection.execute(
            "SELECT payload_json FROM manuscript_proofreading_logs WHERE run_i"
            "d=? AND stage=? AND version=?",
            (claimed.run_id, stage, rules.CONTRACT),
        ).fetchall()
    for row in rows:
        saved = json.loads(row[0])
        if saved.get("fingerprint") == key:
            return saved["result"]

    def operation() -> dict[str, Any]:
        worker._raise_if_run_cancelled(claimed, "contextual proofreading cancelled")
        proof, tokens = capacity or preflight()
        if tokens + maximum + 256 > proof["slot_context"]:
            raise NonRetryableFukamiruError("proofreading.context_window_exceeded")
        result = adapter.complete_json(
            system_prompt=prompt,
            user_prompt=user,
            json_schema=schema,
            reasoning_effort=rules.EFFORT,
            **rules.DECODING,
            seed=int(key[:8], 16) % (2**31),
            max_tokens=maximum,
        )
        if managed_key is not None:
            adapter.remember(result)
        with worker.databases.state.connect() as connection:
            pipeline._log(
                connection,
                claimed,
                stage=stage,
                status="completed",
                version=rules.CONTRACT,
                payload={
                    "fingerprint": key,
                    "logical_fingerprint": managed_key,
                    "result": result,
                    "backend": proof,
                    "source_sha256": context["japanese_proofreading_context"]["plan"][
                        "source_sha256"
                    ],
                    "generation_id": generation_id(context),
                    "node_run_id": claimed.node_run_id,
                    "model": rules.MODEL,
                    "reasoning_effort": rules.EFFORT,
                    "decoding": rules.DECODING,
                    "input_token_count": tokens,
                    "max_output_tokens": maximum,
                    "completion": getattr(adapter, "last_completion_metadata", {}),
                },
            )
        return result

    if getattr(getattr(adapter, "settings", None), "endpoint", ""):
        # A remote GPU must not hold the local embedding/LLM VRAM mutex.
        return operation()
    return worker.execute_local_llm(
        operation, model_name=rules.MODEL, release=getattr(adapter, "stop", None)
    )


def propose(worker: Any, claimed: Any, context: dict[str, Any]) -> dict[str, Any]:
    state = _state(context)

    def one(section: dict[str, Any]) -> dict[str, Any]:
        target = rules.targets(state["plan"], section)
        sentences = _sentences(target)
        payload = {
            "targets": target,
            # Sentence addresses already accompany the target text. Repeating all
            # document-wide UUID lists here can exhaust the context on short prose.
            "document_repeated_terms": [
                {"term": item["term"], "count": item["count"]}
                for item in state["plan"]["repeated_terms"]
            ],
            "document_outline": [
                {
                    "section_id": s["section_id"],
                    "paragraph_ids": s["paragraph_ids"],
                    "heading": next(
                        p["heading"]
                        for p in state["plan"]["paragraphs"]
                        if p["section_id"] == s["section_id"]
                    ),
                }
                for s in state["plan"]["sections"]
            ],
            "locked_section_contracts": [
                s
                for s in context.get("section_contracts") or []
                if s.get("section_id") == section["section_id"]
                or section["section_id"].endswith("-" + str(s.get("section_id") or ""))
            ],
        }
        result = _call(
            worker,
            claimed,
            "llmjp33b_context_proposal",
            rules.EDITOR_PROMPT,
            payload,
            rules.proposal_schema(sentences),
            context,
        )
        audit = rules.audit_proposal(sentences, result)
        return {"section_id": section["section_id"], **audit}

    proposals = _sections(worker, state["plan"]["sections"], one)
    return {**context, "japanese_proofreading_context": {**state, "proposals": proposals}}


def review(worker: Any, claimed: Any, context: dict[str, Any]) -> dict[str, Any]:
    state = _state(context)
    if len(state["proposals"]) != len(state["plan"]["sections"]):
        raise NonRetryableFukamiruError("proofreading.context_proposals_incomplete")

    def one(pair: tuple[dict[str, Any], dict[str, Any]]) -> dict[str, Any]:
        section, proposal = pair
        if section["section_id"] != proposal["section_id"]:
            raise NonRetryableFukamiruError("proofreading.context_section_mismatch")
        target = rules.targets(state["plan"], section)
        sentences = _sentences(target)
        by_id = {s["id"]: s for s in sentences}
        edits = [
            {
                **edit,
                "sentence_id": audit["sentence_id"],
                "original_text": by_id[audit["sentence_id"]]["original_text"],
            }
            for audit in proposal["audits"]
            for edit in audit["accepted_edits"]
        ]
        candidates = {a["sentence_id"]: a["candidate"] for a in proposal["audits"]}
        paragraphs = []
        for p in target["paragraphs"]:
            text = p["text"]
            for sentence in sorted(p["sentences"], key=lambda s: s["start_offset"], reverse=True):
                text = (
                    text[: sentence["start_offset"]]
                    + candidates.get(sentence["id"], sentence["original_text"])
                    + text[sentence["end_offset"] :]
                )
            paragraphs.append({"block_id": p["block_id"], "text": text})
        result = _call(
            worker,
            claimed,
            "llmjp33b_context_review",
            rules.REVIEW_PROMPT,
            {"targets": target, "candidate_paragraphs": paragraphs, "edits": edits},
            rules.review_schema(edits, sentences),
            context,
        )
        rules.validate_review(result, edits, sentences)
        return {"section_id": section["section_id"], **result}

    reviews = _sections(
        worker, list(zip(state["plan"]["sections"], state["proposals"], strict=True)), one
    )
    return {**context, "japanese_proofreading_context": {**state, "reviews": reviews}}


def apply(worker: Any, claimed: Any, context: dict[str, Any]) -> dict[str, Any]:
    state = _state(context)
    if len(state["reviews"]) != len(state["proposals"]):
        raise NonRetryableFukamiruError("proofreading.context_reviews_incomplete")
    root = ET.fromstring(context["proposed_html"])
    paragraphs = {p.get("id"): p for p in root.iter("p")}
    reviews = {r["section_id"]: r for r in state["reviews"]}
    decisions = {d["edit_id"]: d for r in state["reviews"] for d in r["decisions"]}
    accepted = []
    held = []
    human = []
    previews: dict[str, list[tuple[dict[str, Any], str]]] = {}
    sources = {s["id"]: s for p in state["plan"]["paragraphs"] for s in p["sentences"]}
    for proposal in state["proposals"]:
        for audit in proposal["audits"]:
            rules.verify_audit(sources[audit["sentence_id"]], audit)
        section = next(
            s for s in state["plan"]["sections"] if s["section_id"] == proposal["section_id"]
        )
        rules.validate_review(
            {k: v for k, v in reviews[proposal["section_id"]].items() if k != "section_id"},
            [e for a in proposal["audits"] for e in a["accepted_edits"]],
            _sentences(rules.targets(state["plan"], section)),
        )
        human += proposal["human_review"] + reviews[proposal["section_id"]]["human_review"]
        for audit in proposal["audits"]:
            sentence = sources[audit["sentence_id"]]
            held += [{**e, "sentence_id": sentence["id"]} for e in audit["held_edits"]]
            picked = []
            for edit in audit["accepted_edits"]:
                decision = decisions.get(edit["edit_id"])
                if (
                    decision
                    and decision["verdict"] == "accept"
                    and all(decision["checks"].values())
                ):
                    picked.append(edit)
                else:
                    held.append(
                        {
                            **edit,
                            "sentence_id": sentence["id"],
                            "issues": ["independent_review_not_accepted"],
                            "review_reason": decision["reason"] if decision else "確認結果なし",
                        }
                    )
            # Revalidate the approved subset, not the full first-pass candidate.
            checked = rules.audit_proposal(
                [sentence],
                {
                    "corrections": [
                        {
                            **local.identity(sentence),
                            "original_text": sentence["original_text"],
                            "edits": [
                                {k: e[k] for k in ("before", "after", "reason")} for e in picked
                            ],
                        }
                    ],
                    "human_review": [],
                },
            )["audits"][0]
            held += [{**e, "sentence_id": sentence["id"]} for e in checked["held_edits"]]
            accepted += [{**e, "sentence_id": sentence["id"]} for e in checked["accepted_edits"]]
            previews.setdefault(sentence["block_id"], []).append((sentence, checked["candidate"]))
    for block_id, changes in previews.items():
        paragraph = paragraphs[block_id]
        if len(paragraph):
            raise NonRetryableFukamiruError("proofreading.context_inline_structure_protected")
        text = paragraph.text or ""
        for sentence, candidate in sorted(
            changes, key=lambda x: x[0]["start_offset"], reverse=True
        ):
            if text[sentence["start_offset"] : sentence["end_offset"]] != sentence["original_text"]:
                raise NonRetryableFukamiruError("proofreading.context_paragraph_source_changed")
            text = text[: sentence["start_offset"]] + candidate + text[sentence["end_offset"] :]
        paragraph.text = text
    candidate_html = ET.tostring(root, encoding="unicode", short_empty_elements=False)
    CanonicalHTMLValidator().assert_valid(candidate_html)
    gate = JapaneseSurfaceQualityGate.guard_canonical_html(context["proposed_html"], candidate_html)
    checked_blocks = {p.get("id"): p for p in ET.fromstring(gate.canonical_html).iter("p")}
    source_blocks = {p.get("id"): p for p in ET.fromstring(context["proposed_html"]).iter("p")}
    reverted = set()
    for block_id in previews:
        if ET.tostring(checked_blocks[block_id]) != ET.tostring(paragraphs[block_id]):
            paragraphs[block_id].text = source_blocks[block_id].text
            reverted.add(block_id)
    for edit in accepted[:]:
        if sources[edit["sentence_id"]]["block_id"] in reverted:
            held.append({**edit, "issues": ["unrecorded_surface_change"]})
            accepted.remove(edit)
    candidate_html = ET.tostring(root, encoding="unicode", short_empty_elements=False)
    CanonicalHTMLValidator().assert_valid(candidate_html)
    items = {i["review_item_id"]: i for i in context.get("human_review_items") or []}
    for e in held:
        human.append(
            {
                "sentence_id": e["sentence_id"],
                "reason": "局所修正を保留：" + e["before"] + " → " + e["after"],
                "recommended_check": e.get("review_reason") or ";".join(e["issues"]),
            }
        )
    for item in human:
        sentence = sources[item["sentence_id"]]
        sid = next(
            p["section_id"]
            for p in state["plan"]["paragraphs"]
            if p["block_id"] == sentence["block_id"]
        )
        key = "proofreading-context:" + rules.fingerprint(item)
        items[key] = {
            "review_item_id": key,
            "section_id": sid,
            "sentence_id": sentence["block_id"],
            "category": "proofreading_style",
            "reason": item["reason"],
            "recommended_check": item["recommended_check"],
            "source_ids": [],
            "blocks_render": False,
            "status": "open",
        }
    report = {
        "contract": rules.CONTRACT,
        "model": rules.MODEL,
        "reasoning_effort": rules.EFFORT,
        "source_sha256": state["plan"]["source_sha256"],
        "target_sha256": legacy.digest(candidate_html),
        "accepted_edits": accepted,
        "held_edits": held,
    }
    with worker.databases.state.connect() as connection:
        pipeline._log(
            connection,
            claimed,
            stage="llmjp33b_context_application",
            status="completed",
            version=rules.CONTRACT,
            payload=report,
        )
    return {
        **context,
        "proposed_html": candidate_html,
        "human_review_items": list(items.values()),
        "japanese_proofreading_context": {**state, "application": report},
        "manuscript_proofreading_shadow": report,
    }


def _sections(worker: Any, items: list[Any], operation: Any) -> list[Any]:
    adapter = getattr(worker, "llm_jp_proofreading", None)
    remote = bool(getattr(getattr(adapter, "settings", None), "endpoint", ""))
    parallel = (
        min(
            3,
            max(
                1, int(getattr(worker.settings.translation, "japanese_proofreading_concurrency", 1))
            ),
        )
        if remote
        else 1
    )
    if parallel == 1:
        return [operation(item) for item in items]
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        return list(pool.map(operation, items))


def judge_attribution(
    worker: Any, claimed: Any, context: dict[str, Any], candidates: list[dict[str, Any]]
) -> dict[str, Any]:
    from copy import deepcopy

    from fukamiru.authoring.codex import STAGE_GUIDANCE, STAGE_SCHEMAS

    schema = deepcopy(STAGE_SCHEMAS["scholarship_attribution_judgment_v31"])
    array = schema["properties"]["judgments"]
    array.update(minItems=len(candidates), maxItems=len(candidates))
    array["items"]["properties"]["candidate_id"]["enum"] = [c["candidate_id"] for c in candidates]
    return _call(
        worker,
        claimed,
        "llmjp33b_attribution_review",
        STAGE_GUIDANCE["scholarship_attribution_judgment_v31"] + " 理由は日本語で記してください。",
        {"candidates": candidates},
        schema,
        context,
    )


def reconcile(worker: Any, claimed: Any, context: dict[str, Any]) -> dict[str, Any]:
    from fukamiru.worker.runner import WorkerRunner

    return WorkerRunner._authoring_reconcile_candidate_context(worker, claimed, context).context
