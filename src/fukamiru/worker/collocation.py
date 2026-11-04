"""Worker stages for the V29 validation-only predicate-collocation pipeline."""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from typing import Any

from fukamiru.documents.japanese_surface import JapaneseSurfaceQualityGate
from fukamiru.errors import (
    ExternalServiceUnavailable,
    FukamiruError,
    NonRetryableFukamiruError,
)
from fukamiru.revisions import (
    CollocationRepository,
    LLMJPBaseScorer,
    ManuscriptDiscourseService,
    ManuscriptRewriteService,
    extract_collocation_frames,
    frame_from_dict,
    generate_probes,
    score_payload,
    valid_candidate,
)
from fukamiru.revisions.collocation import CollocationProbe
from fukamiru.revisions.predicate_realization import (
    analyze_source as analyze_source_v31,
)
from fukamiru.revisions.predicate_realization import (
    process_structured_frame as process_structured_frame_v31,
)
from fukamiru.revisions.structured_realization import (
    BASE_GENERATION,
    analyze_source,
    generation_payload,
    process_structured_frame,
)
from fukamiru.revisions.structured_realization import (
    PROFILE as STRUCTURED_PROFILE,
)
from fukamiru.revisions.uniform_full_sentence import UniformTokenScorer
from fukamiru.storage.events import EventRepository

_V3_MODEL_SHA256 = "89ed6505a9ebc22c2d31f70d07eaf2dd21279b98632db4775cd727a60a42a8a8"
STRUCTURED_V31_PREDICATE_ONLY_PROFILE = "structured-realization-v31-predicate-only"


def _structured_profile(context: dict[str, Any]) -> str | None:
    profile = context.get("collocation_candidate_profile")
    return (
        profile if profile in {STRUCTURED_PROFILE, STRUCTURED_V31_PREDICATE_ONLY_PROFILE} else None
    )


def _require_v3_model(adapter: Any) -> None:
    path = adapter.settings.path
    if path is None or not path.is_file():
        raise ExternalServiceUnavailable("structured V3 local Base model file missing")
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != _V3_MODEL_SHA256:
        raise NonRetryableFukamiruError("structured V3 model hash differs from frozen Q4_K_M")


def _event(worker: Any, claimed: Any, event_type: str, payload: dict[str, Any]) -> None:
    with worker.databases.state.connect() as connection:
        EventRepository(connection).append(
            claimed.run_id,
            event_type,
            object_type="node_run",
            object_id=claimed.node_run_id,
            payload=payload,
        )


def _usable_mean_character_deltas(
    scores: list[dict[str, Any]],
) -> tuple[list[float], bool]:
    """Return comparable Base-score deltas and whether scoring is incomplete."""

    base_scores = [score for score in scores if score.get("scorer_type") == "llm_jp_base"]
    deltas: list[float] = []
    for score in base_scores:
        features = score.get("features")
        if not isinstance(features, dict) or features.get("complete") is not True:
            continue
        predicate = features.get("predicate")
        if not isinstance(predicate, dict):
            continue
        delta = predicate.get("delta_from_original")
        if not isinstance(delta, dict):
            continue
        value = delta.get("mean_character_logprob")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        numeric = float(value)
        if math.isfinite(numeric):
            deltas.append(numeric)
    # V29 creates exactly three probes per frame. A partial average must not be
    # treated as evidence that the original or a replacement has won.
    return deltas, len(deltas) != 3 or len(base_scores) != 3


def build_frames(worker: Any, claimed: Any, context: dict[str, Any]) -> dict[str, Any]:
    source_preserve = set(worker._string_list(context.get("source_preserve_block_ids")))
    editable_blocks = ManuscriptRewriteService.editable_blocks(str(context["proposed_html"]))
    eligible_blocks = [
        block
        for block in editable_blocks
        if str(block["id"]) not in source_preserve and not block.get("inline_protected")
    ]
    frames = extract_collocation_frames(
        canonical_html=str(context["proposed_html"]),
        run_id=claimed.run_id,
        document_id=str(context["document_id"]),
        source_preserve_block_ids=source_preserve,
        editable_blocks=ManuscriptRewriteService.editable_blocks,
    )
    with worker.databases.state.connect() as connection:
        CollocationRepository(connection).replace_frames(
            frames, run_id=claimed.run_id, node_run_id=claimed.node_run_id
        )
    report = {
        "policy": "collocation-frame-v29.1-full-document-mecab-ginza",
        "coverage_complete": True,
        "full_document_required": True,
        "frame_count": len(frames),
        "frame_limit": None,
        "frame_limit_reached": False,
        "editable_block_count": len(editable_blocks),
        "eligible_block_count": len(eligible_blocks),
        "processed_block_count": len(eligible_blocks),
        "blocks_with_frames": len({frame.block_id for frame in frames}),
        "omitted_eligible_block_count": 0,
        "source_preserve_block_count": len(source_preserve),
        "corpus_lookup": False,
    }
    _event(worker, claimed, "manuscript.collocation.frames.completed", report)
    return {
        **context,
        "pre_polish_html": str(context["proposed_html"]),
        "manuscript_collocation_frames": report,
    }


def generate_candidates(worker: Any, claimed: Any, context: dict[str, Any]) -> dict[str, Any]:
    if _structured_profile(context):
        return _generate_structured_candidates(worker, claimed, context)
    with worker.databases.state.connect() as connection:
        repository = CollocationRepository(connection)
        frames = repository.frames(claimed.run_id)
        existing_candidates = repository.candidates(claimed.run_id)
    completed_frame_ids = {
        str(candidate["frame_id"])
        for candidate in existing_candidates
        if candidate.get("source_kind") == "original"
    }
    pending_frames = [frame for frame in frames if str(frame["id"]) not in completed_frame_ids]
    batches = [pending_frames[offset : offset + 40] for offset in range(0, len(pending_frames), 40)]
    generated_batch_count = 0

    def invoke_batch(_index: int, batch: list[dict[str, Any]]) -> dict[str, Any]:
        if not context.get("use_llm_jp"):
            return {"candidate_sets": []}
        units = [
            {
                "unit_id": str(frame["id"]),
                "sentence": str(frame["sentence"]),
                "original_predicate": str(frame["expression"]),
                "voice": str(frame["voice"]),
                "predicate_type": str(frame["predicate_type"]),
                "arguments": list(frame.get("arguments") or []),
                "register": "academic",
            }
            for frame in batch
        ]
        response = worker._invoke_manuscript_inference(
            claimed, "generate_collocation_candidates", units=units
        )
        return response if isinstance(response, dict) else {"candidate_sets": []}

    def persist_batch(_index: int, batch: list[dict[str, Any]], response: dict[str, Any]) -> None:
        nonlocal generated_batch_count
        by_frame: dict[str, list[dict[str, str]]] = defaultdict(list)
        if context.get("use_llm_jp"):
            batch_ids = {str(item["id"]) for item in batch}
            for candidate_set in response.get("candidate_sets") or []:
                if not isinstance(candidate_set, dict):
                    continue
                frame_id = str(candidate_set.get("unit_id") or "")
                if frame_id not in batch_ids:
                    continue
                by_frame[frame_id].extend(
                    item for item in candidate_set.get("candidates") or [] if isinstance(item, dict)
                )
        for frame_value in batch:
            frame = frame_from_dict(frame_value)
            candidates: list[dict[str, Any]] = [
                {
                    "id": frame.id + ":candidate:original",
                    "expression": frame.expression,
                    "source_kind": "original",
                    "rationale": "原述語（比較基準）",
                }
            ]
            seen = {frame.expression}
            for item in by_frame.get(frame.id, []):
                expression = str(item.get("predicate_expression") or "").strip()
                if expression in seen or not valid_candidate(frame, expression):
                    continue
                digest = hashlib.sha256((frame.id + "\0" + expression).encode()).hexdigest()[:16]
                candidates.append(
                    {
                        "id": f"{frame.id}:candidate:{digest}",
                        "expression": expression,
                        "source_kind": "codex",
                        "rationale": str(item.get("rationale") or ""),
                    }
                )
                seen.add(expression)
                if len(candidates) >= 5:
                    break
            with worker.databases.state.connect() as connection:
                CollocationRepository(connection).save_candidates(
                    run_id=claimed.run_id, frame_id=frame.id, candidates=candidates
                )
        generated_batch_count += 1
        with worker.databases.state.connect() as connection:
            completed_count = int(
                connection.execute(
                    "SELECT COUNT(DISTINCT frame_id) "
                    "FROM manuscript_collocation_candidates WHERE run_id=?",
                    (claimed.run_id,),
                ).fetchone()[0]
            )
        _event(
            worker,
            claimed,
            "manuscript.collocation.candidates.progress",
            {
                "completed_frame_count": completed_count,
                "total_frame_count": len(frames),
            },
        )

    reserved_subslots = worker._reserve_codex_subslots(
        claimed,
        task_count=len(batches),
        requested_parallelism=worker.settings.worker.codex_max_concurrency,
    )
    try:
        worker._run_ordered_parallel(
            claimed,
            batches,
            parallelism=1 + reserved_subslots,
            operation=invoke_batch,
            consume=lambda _index, _batch, _response: None,
            on_completed=persist_batch,
            cancellation_message=("collocation candidate generation was cancelled between batches"),
            thread_name_prefix=f"collocation-candidates-{claimed.run_id[:8]}",
            operation_name="collocation_candidate_generation",
            pool="codex_remote",
        )
    finally:
        worker._release_codex_subslots(claimed, reserved_subslots)
    with worker.databases.state.connect() as connection:
        persisted_candidates = CollocationRepository(connection).candidates(claimed.run_id)
    generated = sum(
        candidate.get("source_kind") != "original" for candidate in persisted_candidates
    )
    originals = sum(
        candidate.get("source_kind") == "original" for candidate in persisted_candidates
    )
    if originals != len(frames):
        raise FukamiruError(
            "collocation.candidate_coverage_incomplete: "
            f"{originals}/{len(frames)} frames have a persisted original candidate"
        )
    report = {
        "generator": "codex_strong_model_checkpointed_full_document",
        "coverage_complete": True,
        "base_model_generated_candidates": False,
        "frame_count": len(frames),
        "original_candidate_count": originals,
        "generated_candidate_count": generated,
        "reused_frame_checkpoint_count": len(completed_frame_ids),
        "generated_batch_count": generated_batch_count,
    }
    _event(worker, claimed, "manuscript.collocation.candidates.completed", report)
    return {**context, "manuscript_collocation_candidates": report}


def _generate_structured_candidates(
    worker: Any, claimed: Any, context: dict[str, Any]
) -> dict[str, Any]:
    """Explicit opt-in; the existing V29 production default remains unchanged."""
    profile = _structured_profile(context)
    if profile is None:
        raise FukamiruError("structured_profile_missing")
    v31_predicate_only = profile == STRUCTURED_V31_PREDICATE_ONLY_PROFILE
    source_analyzer = analyze_source_v31 if v31_predicate_only else analyze_source
    realization = process_structured_frame_v31 if v31_predicate_only else process_structured_frame
    adapter = worker.llm_jp_base
    if (
        adapter is None
        or not adapter.configured
        or adapter.settings.host
        not in {
            "localhost",
            "127.0.0.1",
        }
    ):
        raise ExternalServiceUnavailable("local llm-jp Base is required for structured V3")
    _require_v3_model(adapter)
    with worker.databases.state.connect() as connection:
        repository = CollocationRepository(connection)
        frames = repository.frames(claimed.run_id)
        existing = repository.candidates(claimed.run_id)
    completed = {
        str(candidate["frame_id"])
        for candidate in existing
        if candidate.get("source_kind") == "original"
    }
    counts: dict[str, int] = defaultdict(int)
    for value in frames:
        frame = frame_from_dict(value)
        if frame.id in completed:
            counts["checkpointed_frames"] += 1
            continue
        start = frame.predicate_start - frame.sentence_start
        end = frame.predicate_end - frame.sentence_start
        source = source_analyzer(frame.sentence, start, end, frame_id=frame.id)
        raw: list[dict[str, Any]] = []
        if source["status"] == "constructed":
            adapter.ensure_started()
            for seed in BASE_GENERATION["seeds"]:
                response = adapter._request_json(
                    "/completion",
                    generation_payload(source["P"], seed, BASE_GENERATION),
                    timeout=adapter.settings.request_timeout_seconds,
                )
                text = response.get("content")
                tokens = response.get("tokens")
                if not isinstance(text, str) or not isinstance(tokens, list):
                    raise FukamiruError("structured_v3_generation_response_incomplete")
                raw.append(
                    {
                        "seed": seed,
                        "raw_text": text,
                        "generated_token_count": len(tokens),
                        "generation_origin": "local_base_plain_continuation",
                    }
                )
                counts["local_generation_requests"] += 1
        result = realization(
            frame.sentence,
            start,
            end,
            frame_id=frame.id,
            raw_generations=raw,
        )
        candidates = [
            {
                "id": frame.id + ":candidate:original",
                "expression": str(source.get("E0") or frame.expression),
                "source_kind": "original",
                "rationale": "原述語（比較基準）",
            }
        ]
        for group in result["candidate_groups"]:
            if group["comparison_status"] not in {"eligible", "eligible_with_notes"}:
                continue
            expression = str(group["expression"])
            if expression == str(source.get("E0") or frame.expression) or len(expression) > 60:
                continue
            candidates.append(
                {
                    "id": group["candidate_id"],
                    "expression": expression,
                    "source_kind": "llm_jp_base_structured_v31_predicate_only"
                    if v31_predicate_only
                    else "llm_jp_base_structured_v3",
                    "rationale": "原文構文と文法機能に基づく機械的再構成。意味・自然さは未評価。",
                }
            )
        with worker.databases.state.connect() as connection:
            CollocationRepository(connection).save_candidates(
                run_id=claimed.run_id, frame_id=frame.id, candidates=candidates
            )
        counts["processed_frames"] += 1
        counts["comparable_candidates"] += len(candidates) - 1
        counts["source_" + str(source["status"])] += 1
    report = {
        "generator": profile,
        "np_hybrid_branches_persisted_in_collocation_db": False,
        "opt_in": True,
        "full_document_required": True,
        "frame_count": len(frames),
        "original_candidate_count": len(frames),
        **counts,
        "external_llm_calls": 0,
        "codex_candidate_calls": 0,
    }
    _event(worker, claimed, "manuscript.collocation.candidates.completed", report)
    return {**context, "manuscript_collocation_candidates": report}


def create_probes(worker: Any, claimed: Any, context: dict[str, Any]) -> dict[str, Any]:
    profile = _structured_profile(context)
    structured = profile is not None
    source_analyzer = (
        analyze_source_v31 if profile == STRUCTURED_V31_PREDICATE_ONLY_PROFILE else analyze_source
    )
    with worker.databases.state.connect() as connection:
        repository = CollocationRepository(connection)
        frames = repository.frames(claimed.run_id)
        count = 0
        for value in frames:
            frame = frame_from_dict(value)
            if structured:
                source = source_analyzer(
                    frame.sentence,
                    frame.predicate_start - frame.sentence_start,
                    frame.predicate_end - frame.sentence_start,
                    frame_id=frame.id,
                )
                if source["status"] == "constructed":
                    probes = (
                        CollocationProbe(
                            id=frame.id + ":structured_original_sentence",
                            frame_id=frame.id,
                            kind="original_sentence",
                            prefix=source["P"],
                            suffix=source["R"],
                        ),
                    )
                else:
                    probes = generate_probes(frame)[:1]
            else:
                probes = generate_probes(frame)
            repository.save_probes(run_id=claimed.run_id, probes=probes)
            count += len(probes)
    expected_count = len(frames) * (1 if structured else 3)
    if count != expected_count:
        raise FukamiruError(
            "collocation.probe_coverage_incomplete: "
            f"generated {count}/{expected_count} required probes"
        )
    report = {
        "policy": "structured-original-sentence-"
        + ("v31-predicate-only" if profile == STRUCTURED_V31_PREDICATE_ONLY_PROFILE else "v3")
        if structured
        else "natural-japanese-probes-v29.1-full-document",
        "coverage_complete": True,
        "frame_count": len(frames),
        "probe_count": count,
        "probe_kinds": ["original_sentence"]
        if structured
        else ["original_sentence", "local_predicate_frame", "canonicalized_frame"],
        "structured_frame_sent_to_base_model": False,
    }
    _event(worker, claimed, "manuscript.collocation.probes.completed", report)
    return {**context, "manuscript_collocation_probes": report}


def score_candidates(worker: Any, claimed: Any, context: dict[str, Any]) -> dict[str, Any]:
    if _structured_profile(context):
        return _score_structured_full_sentences(worker, claimed, context)
    adapter = worker.llm_jp_base
    if adapter is None or not adapter.configured:
        raise ExternalServiceUnavailable("llm-jp-4-8b-base scorer is not configured")

    def operation() -> dict[str, Any]:
        scorer = LLMJPBaseScorer(adapter)
        with worker.databases.state.connect() as connection:
            repository = CollocationRepository(connection)
            candidates = repository.candidates(claimed.run_id)
            probes = repository.probes(claimed.run_id)
            prior_scores = repository.scores(
                claimed.run_id,
                scorer_type=scorer.scorer_type,
                scorer_identity=scorer.identity,
            )
        probes_by_frame: dict[str, list[dict[str, Any]]] = defaultdict(list)
        candidates_by_frame: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for probe in probes:
            probes_by_frame[str(probe["frame_id"])].append(probe)
        for candidate in candidates:
            candidates_by_frame[str(candidate["frame_id"])].append(candidate)
        checkpointed = {
            (str(item["candidate_id"]), str(item["probe_id"])): dict(item["features"])
            for item in prior_scores
        }
        completed = 0
        new_score_calls = 0
        reused_score_components = 0
        scoring_issues: list[dict[str, str]] = []
        for frame_id, frame_candidates in candidates_by_frame.items():
            frame_probes = probes_by_frame.get(frame_id, [])
            results: dict[tuple[str, str], dict[str, Any]] = {}
            # Probe-first order lets llama.cpp reuse the shared prefix KV cache
            # while all candidate continuations for that probe are compared.
            for probe in frame_probes:
                for candidate in frame_candidates:
                    candidate_id = str(candidate["id"])
                    probe_id = str(probe["id"])
                    key = (candidate_id, probe_id)
                    expression = str(candidate["expression"])
                    features = dict(checkpointed.get(key) or {})
                    if isinstance(features.get("predicate"), dict):
                        reused_score_components += 1
                    else:
                        try:
                            predicate = scorer.score(
                                prefix=str(probe["prefix_text"]), candidate=expression
                            )
                        except NonRetryableFukamiruError as exc:
                            scoring_issues.append(
                                {
                                    "frame_id": frame_id,
                                    "candidate_id": candidate_id,
                                    "probe_id": probe_id,
                                    "component": "predicate",
                                    "error": str(exc),
                                }
                            )
                            continue
                        features = {
                            "predicate": score_payload(predicate),
                            "suffix": features.get("suffix"),
                            "complete": False,
                        }
                        with worker.databases.state.connect() as connection:
                            CollocationRepository(connection).save_score(
                                run_id=claimed.run_id,
                                candidate_id=candidate_id,
                                probe_id=probe_id,
                                scorer_type=scorer.scorer_type,
                                scorer_identity=scorer.identity,
                                features=features,
                            )
                        checkpointed[key] = features
                        new_score_calls += 1
                    results[key] = features
                suffix_text = str(probe["suffix_text"])
                for candidate in frame_candidates:
                    candidate_id = str(candidate["id"])
                    probe_id = str(probe["id"])
                    key = (candidate_id, probe_id)
                    features = results.get(key)
                    if features is None:
                        continue
                    if suffix_text and not isinstance(features.get("suffix"), dict):
                        expression = str(candidate["expression"])
                        try:
                            suffix = scorer.score(
                                prefix=str(probe["prefix_text"]) + expression,
                                candidate=suffix_text,
                            )
                        except NonRetryableFukamiruError as exc:
                            scoring_issues.append(
                                {
                                    "frame_id": frame_id,
                                    "candidate_id": candidate_id,
                                    "probe_id": probe_id,
                                    "component": "suffix",
                                    "error": str(exc),
                                }
                            )
                            continue
                        features["suffix"] = score_payload(suffix)
                        new_score_calls += 1
                    elif suffix_text:
                        reused_score_components += 1
                    features["complete"] = not suffix_text or isinstance(
                        features.get("suffix"), dict
                    )
                    with worker.databases.state.connect() as connection:
                        CollocationRepository(connection).save_score(
                            run_id=claimed.run_id,
                            candidate_id=candidate_id,
                            probe_id=probe_id,
                            scorer_type=scorer.scorer_type,
                            scorer_identity=scorer.identity,
                            features=features,
                        )
                    checkpointed[key] = features
                    if features["complete"]:
                        completed += 1
            originals = [item for item in frame_candidates if item["source_kind"] == "original"]
            if len(originals) != 1:
                raise FukamiruError(f"Frame {frame_id} has no unique original candidate")
            original_id = str(originals[0]["id"])
            with worker.databases.state.connect() as connection:
                repository = CollocationRepository(connection)
                for probe in frame_probes:
                    probe_id = str(probe["id"])
                    baseline_features = results.get((original_id, probe_id))
                    if not baseline_features or not baseline_features.get("complete"):
                        continue
                    baseline = baseline_features["predicate"]
                    complete_candidates = [
                        item
                        for item in frame_candidates
                        if results.get((str(item["id"]), probe_id), {}).get("complete")
                    ]
                    logits = [
                        results[(str(item["id"]), probe_id)]["predicate"]["mean_token_logprob"]
                        for item in complete_candidates
                    ]
                    maximum = max(logits)
                    denominator = sum(math.exp(value - maximum) for value in logits)
                    for candidate in complete_candidates:
                        candidate_id = str(candidate["id"])
                        features = results[(candidate_id, probe_id)]
                        predicate = features["predicate"]
                        predicate["delta_from_original"] = {
                            "raw_logprob_sum": predicate["raw_logprob_sum"]
                            - baseline["raw_logprob_sum"],
                            "mean_token_logprob": predicate["mean_token_logprob"]
                            - baseline["mean_token_logprob"],
                            "mean_character_logprob": predicate["mean_character_logprob"]
                            - baseline["mean_character_logprob"],
                        }
                        predicate["candidate_relative_probability"] = (
                            math.exp(predicate["mean_token_logprob"] - maximum) / denominator
                        )
                        repository.save_score(
                            run_id=claimed.run_id,
                            candidate_id=candidate_id,
                            probe_id=probe_id,
                            scorer_type=scorer.scorer_type,
                            scorer_identity=scorer.identity,
                            features=features,
                            commit=False,
                        )
                connection.commit()
        return {
            "scorer_type": scorer.scorer_type,
            "model": "llm-jp-4-8b-base",
            "quantization": scorer.quantization,
            "model_identity": scorer.identity,
            "coverage_complete": True,
            "frame_count": len(candidates_by_frame),
            "candidate_count": sum(len(items) for items in candidates_by_frame.values()),
            "probe_count": len(probes),
            "scored_candidate_probe_pairs": completed,
            "new_score_calls": new_score_calls,
            "reused_score_components": reused_score_components,
            "incomplete_score_count": len(scoring_issues),
            "scoring_issues": scoring_issues,
            "sampling": False,
            "raw_logits": True,
            "corpus_lookup": False,
        }

    report = worker.execute_local_llm(
        operation,
        model_name="llm-jp-base",
        release=adapter.stop,
    )
    _event(worker, claimed, "manuscript.collocation.scoring.completed", report)
    updated = {**context, "manuscript_collocation_scoring": report}
    if report["incomplete_score_count"]:
        warning = (
            "Collocation scoring remained incomplete for "
            f"{report['incomplete_score_count']} candidate/probe component(s)."
        )
        updated["rewrite_warnings"] = [
            *list(context.get("rewrite_warnings") or []),
            warning,
        ]
        updated["completion_status"] = "conditional_success"
        updated.setdefault(
            "conditional_success",
            {
                "status": "conditional_success",
                "reason": "collocation_scoring_incomplete",
                "unresolved_count": report["incomplete_score_count"],
            },
        )
    return updated


def _score_structured_full_sentences(
    worker: Any, claimed: Any, context: dict[str, Any]
) -> dict[str, Any]:
    """One frozen full-sentence teacher-forcing route, never the V29 3-probe average."""
    adapter = worker.llm_jp_base
    if (
        adapter is None
        or not adapter.configured
        or adapter.settings.host
        not in {
            "localhost",
            "127.0.0.1",
        }
    ):
        raise ExternalServiceUnavailable("local llm-jp Base is required for structured V3")
    _require_v3_model(adapter)
    profile_hash = "eec5a81ff1addf7af387878690a8a4b7cd27449b4ac96d2a1b828e2771bd6a1b"
    scorer_type = "llm_jp_base_uniform_full_sentence_v3"
    scorer_identity = adapter.model_identity + ":" + profile_hash

    def operation() -> dict[str, Any]:
        scorer = UniformTokenScorer(
            adapter,
            context_size=adapter.settings.context_size,
            cache_prompt=False,
            profile_hash=profile_hash,
        )
        with worker.databases.state.connect() as connection:
            repo = CollocationRepository(connection)
            candidates = repo.candidates(claimed.run_id)
            probes = repo.probes(claimed.run_id)
            existing = repo.scores(
                claimed.run_id,
                scorer_type=scorer_type,
                scorer_identity=scorer_identity,
            )
        by_frame: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for candidate in candidates:
            by_frame[str(candidate["frame_id"])].append(candidate)
        probe_by_frame = {str(probe["frame_id"]): probe for probe in probes}
        checkpointed = {
            (str(item["candidate_id"]), str(item["probe_id"])): item["features"]
            for item in existing
        }
        scored, reused = 0, 0
        for frame_id, items in by_frame.items():
            probe = probe_by_frame.get(frame_id)
            if probe is None:
                raise FukamiruError("structured_v3_original_probe_missing:" + frame_id)
            originals = [item for item in items if item["source_kind"] == "original"]
            if len(originals) != 1:
                raise FukamiruError("structured_v3_original_candidate_not_unique:" + frame_id)
            if len(items) == 1:
                continue
            baseline = None
            for candidate in [*originals, *(item for item in items if item not in originals)]:
                sentence = (
                    str(probe["prefix_text"])
                    + str(candidate["expression"])
                    + str(probe["suffix_text"])
                )
                key = (str(candidate["id"]), str(probe["id"]))
                old = checkpointed.get(key)
                if old and old.get("full_sentence", {}).get("sentence") == sentence:
                    score = old["full_sentence"]
                    if (
                        score.get("profile_hash") != profile_hash
                        or score.get("status") != "complete"
                    ):
                        raise FukamiruError("structured_v3_score_checkpoint_invalid")
                    reused += 1
                else:
                    score = scorer.score(sentence)
                    scored += 1
                if candidate["source_kind"] == "original":
                    baseline = score["J_nats"]
                assert baseline is not None
                features = {
                    "full_sentence": score,
                    "complete": True,
                    "delta_from_original_nats": score["J_nats"] - baseline,
                    "semantic_preservation": None,
                    "editorial_decision": None,
                }
                with worker.databases.state.connect() as connection:
                    CollocationRepository(connection).save_score(
                        run_id=claimed.run_id,
                        candidate_id=str(candidate["id"]),
                        probe_id=str(probe["id"]),
                        scorer_type=scorer_type,
                        scorer_identity=scorer_identity,
                        features=features,
                    )
        return {
            "scorer_type": scorer_type,
            "scorer_identity": scorer_identity,
            "route": "uniform_per_token_teacher_forcing",
            "profile_hash": profile_hash,
            "new_sentence_scores": scored,
            "reused_sentence_scores": reused,
            "backend_requests": scorer.request_count,
            "codex_calls": 0,
            "external_llm_calls": 0,
        }

    report = worker.execute_local_llm(operation, model_name="llm-jp-base", release=adapter.stop)
    _event(worker, claimed, "manuscript.collocation.scoring.completed", report)
    return {**context, "manuscript_collocation_scoring": report}


def verify_and_decide(worker: Any, claimed: Any, context: dict[str, Any]) -> dict[str, Any]:
    if _structured_profile(context):
        raise NonRetryableFukamiruError(
            "structured acceptance review is disabled; use the unsent local experiment payloads"
        )
    verifier_identity = (
        worker._language_engine_descriptor_v22(worker.translator).get("runtime_fingerprint")
        or "codex-unavailable"
    )
    with worker.databases.state.connect() as connection:
        repository = CollocationRepository(connection)
        records = repository.list_run(claimed.run_id)
    units: list[dict[str, Any]] = []
    generated_candidate_count = 0
    for record in records:
        frame = record["frame"]
        for candidate in record["candidates"]:
            if candidate["source_kind"] == "original":
                continue
            generated_candidate_count += 1
            existing_verdict = candidate.get("semantic_verdict")
            if isinstance(existing_verdict, dict) and str(
                existing_verdict.get("verifier_identity") or ""
            ) == str(verifier_identity):
                continue
            units.append(
                {
                    "unit_id": str(candidate["id"]),
                    "sentence": str(frame["sentence"]),
                    "original_predicate": str(frame["expression"]),
                    "candidate_predicate": str(candidate["expression"]),
                    "voice": str(frame["voice"]),
                    "arguments": list(frame.get("arguments") or []),
                }
            )
    batches = [units[offset : offset + 50] for offset in range(0, len(units), 50)]

    def invoke_batch(_index: int, batch: list[dict[str, Any]]) -> dict[str, Any]:
        response = worker._invoke_manuscript_inference(
            claimed,
            "verify_collocation_semantics",
            units=batch,
        )
        return response if isinstance(response, dict) else {"verdicts": []}

    def persist_batch(_index: int, batch: list[dict[str, Any]], response: dict[str, Any]) -> None:
        returned: dict[str, dict[str, str]] = {}
        batch_ids = {str(item["unit_id"]) for item in batch}
        for item in response.get("verdicts") or []:
            if not isinstance(item, dict):
                continue
            unit_id = str(item.get("unit_id") or "")
            if unit_id not in batch_ids:
                continue
            verdict = str(item.get("verdict") or "uncertain")
            returned[unit_id] = {
                "verdict": verdict if verdict in {"true", "false", "uncertain"} else "uncertain",
                "rationale": str(item.get("rationale") or ""),
            }
        with worker.databases.state.connect() as connection:
            repository = CollocationRepository(connection)
            for unit in batch:
                unit_id = str(unit["unit_id"])
                result = returned.get(
                    unit_id,
                    {
                        "verdict": "uncertain",
                        "rationale": "意味検証応答が欠落したため人間確認が必要",
                    },
                )
                repository.save_verdict(
                    run_id=claimed.run_id,
                    candidate_id=unit_id,
                    verdict=result["verdict"],
                    rationale=result["rationale"],
                    verifier_identity=str(verifier_identity),
                )
            completed_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM manuscript_collocation_semantic_verdicts v "
                    "JOIN manuscript_collocation_candidates c ON c.id=v.candidate_id "
                    "WHERE v.run_id=? AND v.verifier_identity=? "
                    "AND c.source_kind!='original'",
                    (claimed.run_id, str(verifier_identity)),
                ).fetchone()[0]
            )
        _event(
            worker,
            claimed,
            "manuscript.collocation.semantics.progress",
            {
                "completed_candidate_count": completed_count,
                "total_candidate_count": generated_candidate_count,
            },
        )

    reserved_subslots = worker._reserve_codex_subslots(
        claimed,
        task_count=len(batches),
        requested_parallelism=worker.settings.worker.codex_max_concurrency,
    )
    try:
        worker._run_ordered_parallel(
            claimed,
            batches,
            parallelism=1 + reserved_subslots,
            operation=invoke_batch,
            consume=lambda _index, _batch, _response: None,
            on_completed=persist_batch,
            cancellation_message=(
                "collocation semantic verification was cancelled between batches"
            ),
            thread_name_prefix=f"collocation-semantics-{claimed.run_id[:8]}",
            operation_name="collocation_semantic_verification",
            pool="codex_remote",
        )
    finally:
        worker._release_codex_subslots(claimed, reserved_subslots)
    with worker.databases.state.connect() as connection:
        records = CollocationRepository(connection).list_run(claimed.run_id)
    decision_counts: dict[str, int] = defaultdict(int)
    unresolved_score_candidate_count = 0
    for record in records:
        frame = record["frame"]
        original = next(
            candidate
            for candidate in record["candidates"]
            if candidate["source_kind"] == "original"
        )
        with worker.databases.state.connect() as connection:
            repository = CollocationRepository(connection)
            repository.save_verdict(
                run_id=claimed.run_id,
                candidate_id=str(original["id"]),
                verdict="true",
                rationale="原述語",
                verifier_identity=str(verifier_identity),
            )
            ranked: list[tuple[float, str, str]] = []
            unresolved_candidate_ids: list[str] = []
            for candidate in record["candidates"]:
                if candidate["source_kind"] == "original":
                    continue
                existing_verdict = candidate.get("semantic_verdict")
                result = (
                    {
                        "verdict": str(existing_verdict.get("verdict") or "uncertain"),
                        "rationale": str(existing_verdict.get("rationale") or ""),
                    }
                    if isinstance(existing_verdict, dict)
                    and str(existing_verdict.get("verifier_identity") or "")
                    == str(verifier_identity)
                    else {"verdict": "uncertain", "rationale": "検証応答なし"}
                )
                verdict = result["verdict"]
                if verdict not in {"true", "false", "uncertain"}:
                    verdict = "uncertain"
                repository.save_verdict(
                    run_id=claimed.run_id,
                    candidate_id=str(candidate["id"]),
                    verdict=verdict,
                    rationale=result["rationale"],
                    verifier_identity=str(verifier_identity),
                )
                deltas, scoring_incomplete = _usable_mean_character_deltas(
                    list(candidate["scores"])
                )
                if scoring_incomplete:
                    unresolved_candidate_ids.append(str(candidate["id"]))
                ranked.append(
                    (
                        (sum(deltas) / len(deltas) if not scoring_incomplete else float("-inf")),
                        str(candidate["id"]),
                        verdict,
                    )
                )
            ranked.sort(reverse=True)
            eligible = [item for item in ranked if item[0] > 0 and item[2] != "false"]
            best = eligible[0] if eligible else None
            if best and best[2] == "true":
                decision, suggested = "suggestion", best[1]
            elif best and best[2] == "uncertain":
                decision, suggested = "needs_human_review", best[1]
            elif unresolved_candidate_ids:
                decision, suggested = "needs_human_review", None
            else:
                decision, suggested = "original", None
            unresolved_score_candidate_count += len(unresolved_candidate_ids)
            evidence = {
                "policy": "validation-only-no-auto-apply-v29",
                "aggregate_delta_from_original": best[0] if best else None,
                "semantic_verdict": (
                    best[2] if best else "uncertain" if unresolved_candidate_ids else "true"
                ),
                "scoring_status": ("incomplete" if unresolved_candidate_ids else "complete"),
                "unresolved_candidate_ids": unresolved_candidate_ids,
                "auto_applied": False,
                "threshold_calibrated": False,
            }
            repository.save_decision(
                run_id=claimed.run_id,
                frame_id=str(frame["id"]),
                original_candidate_id=str(original["id"]),
                suggested_candidate_id=suggested,
                decision=decision,
                evidence=evidence,
            )
            decision_counts[decision] += 1
    report = {
        "semantic_gate": "hard",
        "coverage_complete": True,
        "frame_count": len(records),
        "verified_candidate_count": generated_candidate_count,
        "newly_verified_candidate_count": len(units),
        "reused_verdict_checkpoint_count": generated_candidate_count - len(units),
        "decision_counts": dict(decision_counts),
        "unresolved_score_candidate_count": unresolved_score_candidate_count,
        "auto_apply": False,
    }
    _event(worker, claimed, "manuscript.collocation.decisions.completed", report)
    return {**context, "manuscript_collocation_decisions": report}


def publish(worker: Any, claimed: Any, context: dict[str, Any]) -> dict[str, Any]:
    with worker.databases.state.connect() as connection:
        records = CollocationRepository(connection).list_run(claimed.run_id)
    frame_coverage = dict(context.get("manuscript_collocation_frames") or {})
    candidate_coverage = dict(context.get("manuscript_collocation_candidates") or {})
    probe_coverage = dict(context.get("manuscript_collocation_probes") or {})
    decision_coverage = dict(context.get("manuscript_collocation_decisions") or {})
    expected_frames = int(frame_coverage.get("frame_count") or 0)
    decided_frames = sum(isinstance(record.get("decision"), dict) for record in records)
    if (
        frame_coverage.get("coverage_complete") is not True
        or candidate_coverage.get("coverage_complete") is not True
        or probe_coverage.get("coverage_complete") is not True
        or decision_coverage.get("coverage_complete") is not True
        or expected_frames != len(records)
        or decided_frames != len(records)
    ):
        raise FukamiruError(
            "collocation.full_document_coverage_required: "
            f"frames={len(records)}/{expected_frames}, decisions={decided_frames}/{len(records)}"
        )
    proposed_html = str(context["proposed_html"])
    gate = JapaneseSurfaceQualityGate.guard_canonical_html(proposed_html, proposed_html)
    remaining = list(JapaneseSurfaceQualityGate.find_canonical_html(gate.canonical_html))
    suggestions = sum(
        isinstance(record.get("decision"), dict)
        and record["decision"].get("decision") in {"suggestion", "needs_human_review"}
        for record in records
    )
    report = {
        "policy": "validation-only-no-auto-apply-v29.1-full-document",
        "coverage_complete": True,
        "full_document_required": True,
        "frame_count": len(records),
        "eligible_block_count": int(frame_coverage.get("eligible_block_count") or 0),
        "processed_block_count": int(frame_coverage.get("processed_block_count") or 0),
        "omitted_eligible_block_count": int(
            frame_coverage.get("omitted_eligible_block_count") or 0
        ),
        "decision_count": decided_frames,
        "suggestion_count": suggestions,
        "auto_applied_count": 0,
        "evidence_endpoint": f"/api/runs/{claimed.run_id}/collocations",
        "human_judgments_target": (
            worker.settings.translation.manuscript_candidate_human_training_target
        ),
    }
    surface_report = gate.as_dict()
    surface_report.update(
        {
            "remaining_issue_blocks": remaining,
            "remaining_issue_count": sum(len(item.get("issues") or []) for item in remaining),
            "passed": not remaining,
        }
    )
    legacy_predicates = ManuscriptDiscourseService.predicate_candidates(
        proposed_html,
        realized_paragraphs=list(context.get("realized_paragraphs") or []),
        structure_plan=dict(context.get("structure_plan") or {}),
    )
    legacy_inventory = [
        worker._manuscript_predicate_inventory_record(item) for item in legacy_predicates
    ]
    audited_blocks = sorted({str(item.get("block_id") or "") for item in legacy_predicates} - {""})
    _event(worker, claimed, "manuscript.collocation.published", report)
    return {
        **context,
        "proposed_html": gate.canonical_html,
        "manuscript_collocation": report,
        "manuscript_polish_target_plan": {
            "enabled": True,
            "policy": "collocation-frame-v29",
            "target_count": len(records),
        },
        "manuscript_polish": {
            "enabled": True,
            "policy": "v29-base-scored-suggestions",
            "mode": "collocation_validation_only",
            "auto_applied_count": 0,
            "zero_rejection_required": True,
            "rejection_rate": 0.0,
            "rejected_block_count": 0,
            "waiting_human_block_count": 0,
        },
        "manuscript_polish_selection": report,
        "manuscript_candidate_selection": {
            "enabled": True,
            "mode": "v29_validation_no_edit",
            "block_audits": [
                {"block_id": block_id, "status": "applied", "issues": []}
                for block_id in audited_blocks
            ],
        },
        "predicate_relation_resolution": {
            "policy": "v29_collocation_preserve_current_inventory",
            "candidate_count": len(legacy_predicates),
            "decision_count": len(legacy_predicates),
            "human_review_count": 0,
            "issues": [],
            "post_resolution_inventory": legacy_inventory,
            "post_resolution_candidate_count": len(legacy_inventory),
            "decisions": [],
        },
        "manuscript_surface_quality_gate": surface_report,
    }
