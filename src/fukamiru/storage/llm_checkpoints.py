"""Run-scoped, validation-gated checkpoints for structured LLM results."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fukamiru.authoring.codex import AuthoringRequest


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def common_prefix_characters(values: list[str]) -> int:
    if not values:
        return 0
    first = min(values)
    last = max(values)
    limit = min(len(first), len(last))
    index = 0
    while index < limit and first[index] == last[index]:
        index += 1
    return index


@dataclass(frozen=True)
class LLMCheckpointKey:
    request_fingerprint_sha256: str
    cache_family_sha256: str
    full_prompt_sha256: str
    payload_sha256: str
    rag_context_sha256: str
    schema_sha256: str
    guidance_sha256: str
    execution_fingerprint_sha256: str
    prefix_1024_sha256: str
    prefix_4096_sha256: str
    prefix_16384_sha256: str


def build_checkpoint_key(
    *,
    run_id: str,
    workflow_version: str,
    node_id: str,
    operation_name: str,
    request: AuthoringRequest,
    plan_id: str | None,
    plan_revision: int | None,
    source_revision_id: str | None,
    checkpoint_variant: str = "normal",
) -> LLMCheckpointKey:
    payload_sha = sha256_text(canonical_json(request.payload))
    rag_sha = sha256_text(request.context)
    schema_sha = sha256_text(canonical_json(request.schema))
    guidance_sha = sha256_text(request.guidance)
    prompt_sha = sha256_text(request.prompt)
    execution = {
        "policy": request.execution_policy,
        "model": request.model,
        "reasoning_effort": request.reasoning_effort,
        "schema_sha256": schema_sha,
    }
    execution_sha = sha256_text(canonical_json(execution))
    components = {
        "run_id": run_id,
        "workflow_version": workflow_version,
        "node_id": node_id,
        "stage": request.stage,
        "canonical_stage": request.canonical_stage,
        "operation_name": operation_name,
        "model": request.model,
        "reasoning_effort": request.reasoning_effort,
        "prompt_policy": request.prompt_policy,
        "prompt_renderer_version": request.prompt_renderer_version,
        "schema_sha256": schema_sha,
        "guidance_sha256": guidance_sha,
        "payload_sha256": payload_sha,
        "rag_context_sha256": rag_sha,
        "full_prompt_sha256": prompt_sha,
        "plan_id": plan_id,
        "plan_revision": plan_revision,
        "source_revision_id": source_revision_id,
        "codex_execution_fingerprint_sha256": execution_sha,
        "checkpoint_variant": checkpoint_variant,
    }
    family = {
        key: components[key]
        for key in (
            "run_id", "workflow_version", "stage", "canonical_stage", "operation_name",
            "model", "reasoning_effort", "schema_sha256", "guidance_sha256",
            "codex_execution_fingerprint_sha256", "checkpoint_variant",
        )
    }
    return LLMCheckpointKey(
        request_fingerprint_sha256=sha256_text(canonical_json(components)),
        cache_family_sha256=sha256_text(canonical_json(family)),
        full_prompt_sha256=prompt_sha,
        payload_sha256=payload_sha,
        rag_context_sha256=rag_sha,
        schema_sha256=schema_sha,
        guidance_sha256=guidance_sha,
        execution_fingerprint_sha256=execution_sha,
        prefix_1024_sha256=sha256_text(request.prompt[:1024]),
        prefix_4096_sha256=sha256_text(request.prompt[:4096]),
        prefix_16384_sha256=sha256_text(request.prompt[:16384]),
    )


class LLMCheckpointRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def load(self, run_id: str, fingerprint: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT id,result_json FROM llm_result_checkpoints "
            "WHERE run_id=? AND request_fingerprint_sha256=? AND status='accepted'",
            (run_id, fingerprint),
        ).fetchone()
        if row is None:
            return None
        return {"id": str(row["id"]), "result": json.loads(str(row["result_json"]))}

    def save(
        self,
        *,
        run_id: str,
        node_run_id: str | None,
        node_id: str,
        operation_name: str,
        request: AuthoringRequest,
        key: LLMCheckpointKey,
        result: dict[str, Any],
        validation: dict[str, Any],
    ) -> None:
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            """
            INSERT INTO llm_result_checkpoints(
                id,run_id,node_run_id,node_id,stage,canonical_stage,operation_name,
                request_fingerprint_sha256,full_prompt_sha256,cache_family_sha256,
                result_json,validation_json,status,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'accepted',?,?)
            ON CONFLICT(run_id,request_fingerprint_sha256) DO UPDATE SET
                node_run_id=excluded.node_run_id,result_json=excluded.result_json,
                validation_json=excluded.validation_json,status='accepted',updated_at=excluded.updated_at
            """,
            (
                uuid.uuid4().hex, run_id, node_run_id, node_id, request.stage,
                request.canonical_stage, operation_name, key.request_fingerprint_sha256,
                key.full_prompt_sha256, key.cache_family_sha256, canonical_json(result),
                canonical_json(validation), now, now,
            ),
        )
        self.connection.commit()

    def invalidate(self, checkpoint_id: str, reason: str) -> None:
        self.connection.execute(
            "UPDATE llm_result_checkpoints SET status='invalidated',validation_json=?,updated_at=? "
            "WHERE id=?",
            (
                canonical_json({"valid": False, "reason": reason}),
                datetime.now(UTC).isoformat(),
                checkpoint_id,
            ),
        )
        self.connection.commit()
