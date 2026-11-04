"""V23 discourse batches; V22 transport and checkpoint semantics stay isolated."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from functools import partial
from typing import TYPE_CHECKING, Any

from fukamiru.revisions.coherence import ManuscriptCoherenceService
from fukamiru.revisions.manuscript_language import DISCOURSE_POLICY, first_mentions
from fukamiru.revisions.rewrite import ManuscriptRewriteService
from fukamiru.storage.payloads import UnitCheckpoints

if TYPE_CHECKING:
    from fukamiru.worker.runner import ClaimedNode, WorkerRunner

LANGUAGE_EXECUTION_POLICY = "language-execution-v23-parallel-1"


def language_plan(self: WorkerRunner, claimed: ClaimedNode, context: dict, phase: str):
    """Run discourse analysis without caching transport failures as meaning.

    Results are checkpointed per block. Missing or malformed block verdicts
    receive one targeted supplement request. A second incomplete response is
    a durable ``response_contract_incomplete`` result suitable for a
    conditional proposal; exceptions continue to the worker's normal wait or
    failure path and never become semantic decisions.
    """

    context = deepcopy(context)
    with self.databases.state.connect() as connection:
        source = (
            context.get("proposed_html")
            or ManuscriptRewriteService(connection).prepare(
                document_id=str(context["document_id"]),
                base_revision_id=str(context["base_revision_id"]),
                outline_node_id=context.get("outline_node_id"),
                source_proposal_id=context.get("source_proposal_id"),
            )["base_html"]
        )
    source_preserve_block_ids = set(self._string_list(context.get("source_preserve_block_ids")))
    blocks = [
        block
        for block in ManuscriptCoherenceService.semantic_structure(source)["blocks"]
        if block.get("rewritable_as_argument", True)
        and not block.get("is_heading")
        and str(block.get("id") or "") not in source_preserve_block_ids
    ]
    if not blocks:
        return []
    self._manuscript_inference_backend(claimed, "plan_manuscript_language", context)
    resolver = partial(self._invoke_manuscript_inference, claimed, "plan_manuscript_language")
    context_signature = hashlib.sha256(
        json.dumps(
            [
                source,
                DISCOURSE_POLICY,
                LANGUAGE_EXECUTION_POLICY,
                phase,
                context.get("term_introduction_plan"),
                context.get("evidence_context"),
                context.get("instructions"),
                context.get("human_review"),
                sorted(source_preserve_block_ids),
            ],
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
    ).hexdigest()
    engine = self._register_language_engine_v22(
        claimed, context, phase=phase, context_signature=context_signature
    )
    signature = hashlib.sha256(
        json.dumps(
            [context_signature, engine["fingerprint"]],
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
    ).hexdigest()
    checkpoint_phase = "language-v23:" + phase
    with self.databases.state.connect() as connection:
        cached = UnitCheckpoints(connection).load(claimed.run_id, checkpoint_phase, signature)
    positions = {str(block["id"]): index for index, block in enumerate(blocks)}
    by_id = {str(block["id"]): block for block in blocks}

    def merge_records(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        for record in (item for group in groups for item in group):
            key = json.dumps(record, ensure_ascii=False, sort_keys=True)
            if key not in seen:
                seen.add(key)
                merged.append(record)
        return merged

    results: dict[str, list[dict[str, Any]]] = {
        key.removeprefix("result:"): list(value)
        for key, value in cached.items()
        if key.startswith("result:") and isinstance(value, list)
    }
    partial_valid: dict[str, list[dict[str, Any]]] = {
        key.removeprefix("partial-valid:"): list(value)
        for key, value in cached.items()
        if key.startswith("partial-valid:") and isinstance(value, list)
    }
    retry_due = {
        key.removeprefix("partial:") for key in cached if key.startswith("partial:")
    } - set(results)
    retry_finished = {key.removeprefix("retry:") for key in cached if key.startswith("retry:")}
    # A pre-call reservation without a result means the process stopped while
    # the external outcome was unknowable. Persist a terminal review record;
    # never repeat that supplement after restart.
    interrupted = retry_due & retry_finished
    if interrupted:
        recovered_results = {
            block_id: merge_records(
                partial_valid.get(block_id, []),
                [
                    {
                        "block_id": block_id,
                        "kind": "block_review",
                        "status": "response_contract_incomplete",
                        "explanation": (
                            "targeted supplement execution was interrupted before "
                            "its result could be committed"
                        ),
                        "contract_issues": ["supplement_execution_interrupted"],
                        "retry_count": 1,
                    }
                ],
            )
            for block_id in sorted(interrupted)
        }
        self._persist_missing_unit_supplement_outcome_v22(
            claimed,
            checkpoint_phase=checkpoint_phase,
            signature=signature,
            records={
                "result:" + block_id: records for block_id, records in recovered_results.items()
            },
            outcomes={block_id: "supplement_execution_interrupted" for block_id in interrupted},
            attempt_id=None,
        )
        results.update(recovered_results)
    retry_due -= retry_finished

    completed_or_retrying = set(results) | retry_due
    fresh = [block for block in blocks if str(block["id"]) not in completed_or_retrying]
    mentions = first_mentions(blocks)

    def call(batch):
        selected_ids = {str(block["id"]) for block in batch}
        neighbours = sorted(
            {
                i
                for block_id in selected_ids
                for i in range(
                    max(0, positions[block_id] - 2), min(len(blocks), positions[block_id] + 3)
                )
                if str(blocks[i]["id"]) not in selected_ids
            }
        )
        neighbouring_blocks = [blocks[i] for i in neighbours]
        local_context = self._manuscript_language_context_for_blocks(
            context, batch, neighbouring_blocks, mentions
        )
        return resolver(
            phase=phase,
            blocks=deepcopy(batch),
            first_mentions=deepcopy(local_context["first_mentions"]),
            evidence=deepcopy(local_context["evidence"]),
            plan=deepcopy(local_context["plan"]),
            neighbouring_blocks=deepcopy(neighbouring_blocks),
            workflow_version=23,
        )

    def evaluate_fresh(_index, batch):
        return call(batch)

    def save_fresh(_index, batch, payload):
        allowed = {str(block["id"]) for block in batch}
        valid, issues = self._validate_language_payload_v22(payload, allowed)
        incomplete = {block_id for block_id in allowed if issues.get(block_id)}
        complete = set(valid) - incomplete
        checkpoint_records: dict[str, Any] = {
            "result:" + block_id: records
            for block_id, records in valid.items()
            if block_id in complete
        }
        checkpoint_records.update(
            {
                "partial-valid:" + block_id: records
                for block_id, records in valid.items()
                if block_id in incomplete
            }
        )
        checkpoint_records.update(
            {
                "partial:" + block_id: {
                    "status": "response_contract_incomplete",
                    "issues": issues.get(block_id) or ["missing_block_verdict"],
                    "supplement_attempts_allowed": 1,
                }
                for block_id in incomplete
            }
        )
        with self.databases.state.connect() as connection:
            UnitCheckpoints(connection).save(
                claimed.run_id, checkpoint_phase, signature, checkpoint_records
            )
        results.update({block_id: valid[block_id] for block_id in complete})
        partial_valid.update(
            {block_id: valid[block_id] for block_id in incomplete if block_id in valid}
        )
        retry_due.update(incomplete)
        self._record_manuscript_progress(
            claimed,
            event_type="manuscript.discourse.progress",
            completed=len(results),
            total=len(blocks),
            extra={
                "phase": phase,
                "execution_status": ("partial_response" if incomplete else "content_complete"),
                "missing_block_count": len(incomplete),
            },
        )

    def run_batches(batches, evaluate, save, suffix):
        self._run_ordered_parallel(
            claimed,
            batches,
            parallelism=min(
                self.settings.worker.codex_discourse_concurrency,
                self.settings.worker.codex_max_concurrency,
            ),
            operation=evaluate,
            consume=lambda _index, _batch, _result: None,
            on_completed=save,
            cancellation_message="V23 discourse analysis cancelled",
            thread_name_prefix="discourse-v23",
            operation_name=f"discourse_{phase}_{suffix}",
            pool="codex_remote",
        )

    # Save completed batches immediately even if an earlier batch is slow or fails.
    # Only the final flattening below follows manuscript order.
    run_batches(
        [fresh[i : i + 5] for i in range(0, len(fresh), 5)], evaluate_fresh, save_fresh, "initial"
    )

    # Exactly one targeted supplement is allowed for each missing unit.
    retry_list = [by_id[block_id] for block_id in sorted(retry_due) if block_id in by_id]

    def evaluate_retry(_index, batch):
        requested = [str(block["id"]) for block in batch]
        reserved, attempt_id = self._reserve_missing_unit_supplement_v22(
            claimed,
            checkpoint_phase=checkpoint_phase,
            signature=signature,
            unit_ids=requested,
        )
        if not reserved:
            return None
        selected = [by_id[block_id] for block_id in reserved]
        try:
            payload = call(selected)
        except Exception:
            self._release_missing_unit_supplement_v22(
                claimed,
                checkpoint_phase=checkpoint_phase,
                signature=signature,
                unit_ids=reserved,
                attempt_id=attempt_id,
            )
            raise
        return reserved, attempt_id, payload

    def save_retry(_index, _batch, outcome):
        if outcome is None:
            return
        reserved, attempt_id, payload = outcome
        allowed = set(reserved)
        valid, issues = self._validate_language_payload_v22(payload, allowed)
        incomplete = {block_id for block_id in allowed if issues.get(block_id)}
        complete = set(valid) - incomplete
        merged_valid = {
            block_id: merge_records(partial_valid.get(block_id, []), valid.get(block_id, []))
            for block_id in allowed
        }
        terminal = {
            block_id: merge_records(
                merged_valid[block_id],
                [
                    {
                        "block_id": block_id,
                        "kind": "block_review",
                        "status": "response_contract_incomplete",
                        "explanation": "one targeted supplement did not return a valid verdict",
                        "contract_issues": issues.get(block_id) or ["missing_block_verdict"],
                        "retry_count": 1,
                    }
                ],
            )
            for block_id in incomplete
        }
        checkpoint_records = {
            **{"result:" + block_id: merged_valid[block_id] for block_id in complete},
            **{"result:" + block_id: records for block_id, records in terminal.items()},
        }
        self._persist_missing_unit_supplement_outcome_v22(
            claimed,
            checkpoint_phase=checkpoint_phase,
            signature=signature,
            records=checkpoint_records,
            outcomes={
                block_id: (
                    "content_complete" if block_id in complete else "response_contract_incomplete"
                )
                for block_id in allowed
            },
            attempt_id=attempt_id,
        )
        results.update({block_id: merged_valid[block_id] for block_id in complete})
        results.update(terminal)
        self._record_manuscript_progress(
            claimed,
            event_type="manuscript.discourse.progress",
            completed=len(results),
            total=len(blocks),
            extra={
                "phase": phase,
                "execution_status": (
                    "response_contract_incomplete" if incomplete else "content_complete"
                ),
                "targeted_supplement_count": len(allowed),
                "remaining_incomplete_count": len(incomplete),
            },
        )

    run_batches(
        [retry_list[i : i + 5] for i in range(0, len(retry_list), 5)],
        evaluate_retry,
        save_retry,
        "supplement",
    )
    return [record for block in blocks for record in results.get(str(block["id"]), [])]
