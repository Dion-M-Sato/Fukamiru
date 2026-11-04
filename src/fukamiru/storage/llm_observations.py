"""Canonical persistence for content-free LLM invocation observations."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any


class LLMObservationRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def record(
        self,
        *,
        run_id: str,
        node_run_id: str | None,
        observation: dict[str, Any] | None = None,
        node_id: str = "",
        stage: str = "",
        operation_name: str = "",
        model: str = "",
        reasoning_effort: str = "",
        key: Any = None,
        prompt_characters: int = 0,
        payload_characters: int = 0,
        rag_context_characters: int = 0,
        common_prefix_characters: int = 0,
        checkpoint_status: str = "saved",
        seed_role: str = "normal",
        reported_counts: dict[str, int | None] | None = None,
    ) -> None:
        metadata: dict[str, Any] = {}
        runtime_values: tuple[Any, ...] | None = None
        if observation is not None:
            usage = dict(observation.get("usage") or {})
            metadata = dict(observation.get("metadata") or {})
            stage = str(observation.get("stage") or stage)
            operation_name = str(
                metadata.get("operation_name")
                or metadata.get("invocation_kind")
                or operation_name
                or "llm_invocation"
            )
            model = str(observation.get("model") or model)
            reasoning_effort = str(
                observation.get("reasoning_effort") or reasoning_effort
            )
            prompt_characters = int(
                observation.get("prompt_characters") or prompt_characters
            )
            payload_characters = int(
                observation.get("payload_characters") or payload_characters
            )
            rag_context_characters = int(
                observation.get("context_characters") or rag_context_characters
            )
            execution_status = str(observation.get("status") or "failed")
            checkpoint_status = (
                "saved" if execution_status == "completed" else "invalidated"
            )
            seed_role = str(metadata.get("seed_role") or seed_role)
            full_prompt_sha256 = str(
                observation.get("full_prompt_sha256")
                or observation.get("prompt_prefix_sha256")
                or ""
            )
            request_fingerprint_sha256 = str(
                observation.get("payload_sha256") or full_prompt_sha256
            )
            cache_family_sha256 = str(
                metadata.get("cache_family_sha256") or request_fingerprint_sha256
            )
            prefix_1024_sha256 = str(
                observation.get("prompt_prefix_sha256") or full_prompt_sha256
            )
            prefix_4096_sha256 = prefix_1024_sha256
            prefix_16384_sha256 = prefix_1024_sha256
            runtime_values = (
                str(metadata.get("invocation_kind") or "codex"),
                metadata.get("batch_index"),
                execution_status,
                int(observation.get("prompt_utf8_bytes") or 0),
                int(observation.get("schema_characters") or 0),
                int(observation.get("output_characters") or 0),
                int(any(value is not None for value in usage.values())),
                observation.get("payload_sha256"),
                observation.get("prompt_prefix_sha256"),
                float(observation.get("latency_seconds") or 0.0),
                observation.get("error_kind"),
                json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
            )
        else:
            usage = dict(reported_counts or {})
            full_prompt_sha256 = str(getattr(key, "full_prompt_sha256", ""))
            request_fingerprint_sha256 = str(
                getattr(key, "request_fingerprint_sha256", full_prompt_sha256)
            )
            cache_family_sha256 = str(
                getattr(key, "cache_family_sha256", request_fingerprint_sha256)
            )
            prefix_1024_sha256 = str(getattr(key, "prefix_1024_sha256", ""))
            prefix_4096_sha256 = str(getattr(key, "prefix_4096_sha256", ""))
            prefix_16384_sha256 = str(getattr(key, "prefix_16384_sha256", ""))
        observation_id = uuid.uuid4().hex
        cached_input_tokens = usage.get("cached_input_tokens")
        self.connection.execute(
            """
            INSERT INTO llm_invocation_observations(
                id,run_id,node_run_id,node_id,stage,operation_name,model,reasoning_effort,
                full_prompt_sha256,prefix_1024_sha256,prefix_4096_sha256,
                prefix_16384_sha256,request_fingerprint_sha256,cache_family_sha256,
                prompt_characters,payload_characters,rag_context_characters,
                common_prefix_characters,checkpoint_status,seed_role,
                input_tokens_reported_count,cached_input_tokens_reported_count,
                cache_write_tokens_reported_count,output_tokens_reported_count,
                reasoning_tokens_reported_count,zero_cached_input_call_count,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                observation_id,
                run_id,
                node_run_id,
                node_id,
                stage,
                operation_name,
                model,
                reasoning_effort,
                full_prompt_sha256,
                prefix_1024_sha256,
                prefix_4096_sha256,
                prefix_16384_sha256,
                request_fingerprint_sha256,
                cache_family_sha256,
                prompt_characters,
                payload_characters,
                rag_context_characters,
                common_prefix_characters,
                checkpoint_status,
                seed_role,
                usage.get("input_tokens"),
                cached_input_tokens,
                usage.get("cache_write_tokens"),
                usage.get("output_tokens"),
                usage.get("reasoning_tokens"),
                int(cached_input_tokens == 0),
                datetime.now(UTC).isoformat(),
            ),
        )
        available_columns = {
            str(row[1])
            for row in self.connection.execute("PRAGMA table_info(llm_invocation_observations)")
        }
        if runtime_values is not None and "invocation_kind" in available_columns:
            self.connection.execute(
                """
                UPDATE llm_invocation_observations
                SET invocation_kind=?,batch_index=?,execution_status=?,prompt_utf8_bytes=?,
                    schema_characters=?,output_characters=?,usage_reported=?,payload_sha256=?,
                    prompt_prefix_sha256=?,latency_seconds=?,error_kind=?,metadata_json=?
                WHERE id=?
                """,
                (*runtime_values, observation_id),
            )
        self.connection.commit()

    def summary(self, run_id: str) -> dict[str, Any]:
        columns = (
            "COUNT(*) AS invocation_count, "
            "SUM(prompt_characters) AS prompt_characters, "
            "SUM(input_tokens_reported_count) AS input_tokens, "
            "SUM(cached_input_tokens_reported_count) AS cached_input_tokens, "
            "SUM(cache_write_tokens_reported_count) AS cache_write_tokens, "
            "SUM(output_tokens_reported_count) AS output_tokens, "
            "SUM(reasoning_tokens_reported_count) AS reasoning_tokens"
        )

        def normalize(row: sqlite3.Row) -> dict[str, Any]:
            result = dict(row)
            for key in (
                "invocation_count",
                "prompt_characters",
                "input_tokens",
                "cached_input_tokens",
                "cache_write_tokens",
                "output_tokens",
                "reasoning_tokens",
            ):
                result[key] = int(result.get(key) or 0)
            input_tokens = result["input_tokens"]
            result["cached_input_ratio"] = (
                round(result["cached_input_tokens"] / input_tokens, 6)
                if input_tokens
                else None
            )
            return result

        total = self.connection.execute(
            f"SELECT {columns} FROM llm_invocation_observations WHERE run_id=?",
            (run_id,),
        ).fetchone()
        by_stage = [
            normalize(row)
            for row in self.connection.execute(
                f"SELECT stage,model,reasoning_effort,{columns} "
                "FROM llm_invocation_observations WHERE run_id=? "
                "GROUP BY stage,model,reasoning_effort ORDER BY stage,model,reasoning_effort",
                (run_id,),
            )
        ]
        checkpoint_rows = self.connection.execute(
            "SELECT checkpoint_status,COUNT(*) AS count "
            "FROM llm_invocation_observations WHERE run_id=? GROUP BY checkpoint_status",
            (run_id,),
        ).fetchall()
        checkpoints = {"hit": 0, "miss": 0, "saved": 0, "invalidated": 0}
        for row in checkpoint_rows:
            checkpoints[str(row["checkpoint_status"])] = int(row["count"])
        zero_row = self.connection.execute(
            "SELECT SUM(zero_cached_input_call_count) FROM llm_invocation_observations "
            "WHERE run_id=?",
            (run_id,),
        ).fetchone()
        return {
            "policy": "content-free-llm-observability-v30",
            "total": normalize(total),
            "by_stage": by_stage,
            "checkpoints": checkpoints,
            "zero_cached_input_call_count": int(zero_row[0] or 0),
        }
