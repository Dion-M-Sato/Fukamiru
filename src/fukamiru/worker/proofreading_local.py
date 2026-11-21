"""Persist ID-addressed local corrections in the existing proofreading generation."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from typing import Any
from xml.etree import ElementTree as ET

from fukamiru.authoring.evidence_contracts import HumanReviewItem
from fukamiru.errors import NonRetryableFukamiruError
from fukamiru.revisions import proofreading_local as local
from fukamiru.revisions import sentence_proofreading as rules
from fukamiru.storage.proofreading_generations import generation_id
from fukamiru.storage.runs import utc_now


def enabled(context: dict[str, Any]) -> bool:
    return context.get("_proofreading_local_contract") == local.CONTRACT


def candidate_audit(connection: Any, patch_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT payload_json FROM manuscript_proofreading_logs "
        "WHERE patch_id=? AND stage='local_edit_candidate' AND version=? "
        "ORDER BY created_at,id LIMIT 1",
        (patch_id, local.CONTRACT),
    ).fetchone()
    return json.loads(row[0])["audit"] if row else None


def candidate_issues(connection: Any, patch: dict[str, Any], candidate: str) -> list[str]:
    source = connection.execute(
        "SELECT * FROM manuscript_proofreading_sentences "
        "WHERE id=? AND run_id=? AND generation_id=?",
        (patch["sentence_id"], patch["run_id"], patch["generation_id"]),
    ).fetchone()
    audit = candidate_audit(connection, patch["id"])
    if source is None or audit is None:
        return ["local_edit_audit_missing"]
    return local.verify_candidate(dict(source), candidate, audit)


def _review_items(
    context: dict[str, Any], sentence: dict[str, Any], audit: dict[str, Any]
) -> list[dict[str, Any]]:
    root = ET.fromstring(context["proposed_html"])
    section_by_block = {}

    def walk(node: ET.Element, section: str) -> None:
        if node.tag == "section":
            section = node.get("id") or section
        if node.tag == "p":
            section_by_block[node.get("id")] = section
        for child in node:
            walk(child, section)

    walk(root, root.get("id") or str(context["document_id"]))
    items = []
    for held in audit["held_edits"]:
        items.append(
            HumanReviewItem.model_validate(
                {
                    "review_item_id": "proofreading-local:" + held["edit_id"],
                    "section_id": section_by_block.get(sentence["block_id"], sentence["block_id"]),
                    # Reader review locates paragraphs; the proofreading sentence namespace
                    # and offsets remain in the SQLite audit instead of masquerading as prose IDs.
                    "sentence_id": sentence["block_id"],
                    "category": "proofreading_style",
                    "reason": "原文の文体・保護表記・修正位置の検査を通らなかった局所修正："
                    + held["before"]
                    + " → "
                    + held["after"],
                    "recommended_check": (
                        "元の段落と置換案を確認してください。適合した他の置換は別に保持しています。"
                    ),
                    "blocks_render": False,
                    "status": "open",
                }
            ).model_dump(mode="json")
        )
    return items


def application_review(
    context: dict[str, Any], sentence: dict[str, Any], patch: dict[str, Any], issues: list[str]
) -> dict[str, Any]:
    """Keep a rejected downstream application visible at its original paragraph."""
    audit = {"held_edits": [{
        "edit_id": patch["id"], "before": sentence["original_text"],
        "after": patch["normalized_candidate_text"], "issues": issues,
    }]}
    item = _review_items(context, sentence, audit)[0]
    item["recommended_check"] = "原文と候補を確認してください。適用時の検査：" + ";".join(issues)
    return item


def generate(worker: Any, claimed: Any, context: dict[str, Any]) -> dict[str, Any]:
    from fukamiru.authoring.codex import _validate_codex_output_schema
    from fukamiru.worker import sentence_proofreading as pipeline

    if not context.get("use_llm_jp"):
        return {
            **context,
            "manuscript_proofreading_generation": {"enabled": False, "candidate_count": 0},
        }
    if worker.llm_jp is None:
        raise NonRetryableFukamiruError("proofreading.llm_jp_unavailable")
    with worker.databases.state.connect() as connection:
        sentences = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM manuscript_proofreading_sentences "
                "WHERE run_id=? AND generation_id=? "
                "AND status!='segmentation_superseded' ORDER BY block_id,sentence_ordinal",
                (claimed.run_id, generation_id(context)),
            )
        ]
    stats: Counter[str] = Counter()
    review_items = {
        item["review_item_id"]: item for item in context.get("human_review_items") or []
    }

    def operation() -> dict[str, int]:
        for sentence in sentences:
            source_id = sentence["id"]
            schema = local.response_schema(sentence)
            _validate_codex_output_schema(schema)
            payload = local.prompt_payload(sentence)
            user_prompt = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            base_seed = int(
                hashlib.sha256(sentence["source_sentence_id"].encode()).hexdigest()[:8], 16
            ) % (2**31)
            generated = []
            for attempt in range(3):
                worker._raise_if_run_cancelled(claimed, "local proofreading cancelled")
                seed = base_seed + attempt
                fingerprint = rules.digest(
                    json.dumps(
                        [
                            local.CONTRACT,
                            local.SYSTEM_PROMPT,
                            payload,
                            schema,
                            seed,
                            worker.llm_jp.model_identity,
                        ],
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
                with worker.databases.state.connect() as connection:
                    saved = [
                        json.loads(row[0])
                        for row in connection.execute(
                            "SELECT payload_json FROM manuscript_proofreading_logs "
                            "WHERE sentence_id=? AND stage='local_edit_sample' AND version=?",
                            (source_id, local.CONTRACT),
                        )
                    ]
                checkpoint = next(
                    (row for row in saved if row.get("request_fingerprint") == fingerprint), None
                )
                if checkpoint:
                    stats["reused_sample_count"] += 1
                    if checkpoint.get("error"):
                        stats["generation_call_error_count"] += 1
                        continue
                    audit = checkpoint["audit"]
                    if local.verify_candidate(sentence, audit["candidate"], audit):
                        raise NonRetryableFukamiruError("proofreading.local_checkpoint_invalid")
                else:
                    stats["generation_call_count"] += 1
                    try:
                        response = worker.llm_jp.complete_json(
                            system_prompt=local.SYSTEM_PROMPT,
                            user_prompt=user_prompt,
                            temperature=0.4,
                            top_p=0.9,
                            top_k=40,
                            repeat_penalty=1.0,
                            seed=seed,
                            max_tokens=worker.settings.translation.manuscript_polish_max_output_tokens,
                            json_schema=schema,
                        )
                        audit = local.apply_response(sentence, response)
                    except NonRetryableFukamiruError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - retain every failed sample
                        error = f"{type(exc).__name__}:{exc}"
                        if "schema" in error.lower() and any(
                            word in error.lower() for word in ("invalid", "unsupported", "400")
                        ):
                            raise NonRetryableFukamiruError(error) from exc
                        stats["generation_call_error_count"] += 1
                        with worker.databases.state.connect() as connection:
                            pipeline._log(
                                connection,
                                claimed,
                                stage="local_edit_sample",
                                status="processing_error",
                                version=local.CONTRACT,
                                sentence_id=source_id,
                                reason=error[:500],
                                payload={
                                    "request_fingerprint": fingerprint,
                                    "seed": seed,
                                    "error": error[:500],
                                },
                            )
                        continue
                    with worker.databases.state.connect() as connection:
                        pipeline._log(
                            connection,
                            claimed,
                            stage="local_edit_sample",
                            status="completed",
                            version=local.CONTRACT,
                            sentence_id=source_id,
                            payload={
                                "request_fingerprint": fingerprint,
                                "seed": seed,
                                "audit": audit,
                            },
                        )
                generated.append((audit, seed))
                stats["held_edit_count"] += len(audit["held_edits"])
                stats["accepted_edit_count"] += len(audit["accepted_edits"])
                for item in _review_items(context, sentence, audit):
                    review_items[item["review_item_id"]] = item

            distinct: dict[str, dict[str, Any]] = {}
            for audit, seed in generated:
                if audit["candidate"] == sentence["original_text"]:
                    stats["unchanged_generation_count"] += 1
                    continue
                record = distinct.setdefault(audit["candidate"], {"audit": audit, "seeds": []})
                record["seeds"].append(seed)
            with worker.databases.state.connect() as connection:
                for ordinal, (text, record) in enumerate(distinct.items()):
                    patch_id = str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL, claimed.run_id + "\0" + source_id + "\0" + text
                        )
                    )
                    connection.execute(
                        "INSERT OR IGNORE INTO manuscript_proofreading_patches "
                        "(id,run_id,sentence_id,candidate_ordinal,original_text,raw_candidate_text,"
                        "normalized_candidate_text,original_hash,candidate_hash,generation_seed,"
                        "generation_model,generation_prompt_version,frequency,generation_seeds_json,"
                        "status,created_at,updated_at,generation_id) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'generated',?,?,?)",
                        (
                            patch_id,
                            claimed.run_id,
                            source_id,
                            ordinal,
                            sentence["original_text"],
                            text,
                            text,
                            sentence["original_hash"],
                            rules.digest(text),
                            record["seeds"][0],
                            worker.llm_jp.model_identity,
                            local.CONTRACT,
                            len(record["seeds"]),
                            pipeline._json(record["seeds"]),
                            utc_now(),
                            utc_now(),
                            generation_id(context),
                        ),
                    )
                    if candidate_audit(connection, patch_id) is None:
                        pipeline._log(
                            connection,
                            claimed,
                            stage="local_edit_candidate",
                            status="generated",
                            version=local.CONTRACT,
                            patch_id=patch_id,
                            sentence_id=source_id,
                            payload={"audit": record["audit"], "seeds": record["seeds"]},
                        )
                connection.execute(
                    "UPDATE manuscript_proofreading_sentences SET status=?,error_text=? WHERE id=?",
                    (
                        "generated" if generated else "processing_error",
                        "" if generated else "local_edit_generation_failed",
                        source_id,
                    ),
                )
            stats["generated_sentence_count"] += bool(generated)
            stats["generation_error_sentence_count"] += not bool(generated)
            stats["candidate_count"] += len(distinct)
        return dict(stats)

    counts = worker.execute_local_llm(
        operation, model_name="llm-jp", release=getattr(worker.llm_jp, "stop", None)
    )
    report = {
        "enabled": True,
        "policy": local.CONTRACT,
        "model_identity": worker.llm_jp.model_identity,
        "sentence_count": len(sentences),
        "generation_count": 3,
        **counts,
    }
    pipeline._event(worker, claimed, "manuscript.proofreading.generation.completed", report)
    return {
        **context,
        "human_review_items": list(review_items.values()),
        "manuscript_proofreading_generation": report,
    }
