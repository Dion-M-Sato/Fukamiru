"""ChatGPT-authenticated Codex CLI warning batches with strict ID integrity."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import suppress
from pathlib import Path
from typing import Any

from fukamiru.errors import ExternalServiceUnavailable, NonRetryableFukamiruError
from fukamiru.llm_observability import extract_codex_usage

# Model availability on the API and on the ChatGPT-authenticated Codex transport
# are distinct. Runtime probes on the Fukamiru host reject the gpt-6 Luna/Sol names,
# while gpt-5.6-luna is available and supports the same economical ``none`` effort
# needed for this bounded classification task.
MODEL = "gpt-5.6-luna"
EFFORT = "none"
SUMMARY = "none"
MODEL_SELECTION_REASON = "chatgpt-codex-runtime-compatible-luna"
PROMPT_PATH = Path(__file__).parent / "assets" / "prompt_q_luna_final.md"
SCHEMA_PATH = Path(__file__).parent / "assets" / "luna_batch_response_schema.json"


class LunaBatchError(Exception):
    pass


class LunaTransportError(ExternalServiceUnavailable):
    """A transport failure that cannot be repaired by splitting content batches."""


class LunaConfigurationError(NonRetryableFukamiruError):
    """A model/auth configuration failure that repeating the Node cannot repair."""


def _codex_failure_detail(stdout: str, stderr: str) -> str:
    """Prefer structured Codex failures over incidental websocket warnings."""

    messages: list[str] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "turn.failed":
            error = event.get("error") or {}
            messages.append(str(error.get("message") or error))
        elif event.get("type") == "error":
            messages.append(str(event.get("message") or event))
        elif event.get("type") == "item.completed" and event.get("item", {}).get(
            "type"
        ) == "error":
            messages.append(str(event["item"].get("message") or event["item"]))
    detail = messages[-1] if messages else stderr[-1000:]
    return " ".join(detail.split())[-1000:]


def _raise_codex_failure(returncode: int, stdout: str, stderr: str) -> None:
    detail = _codex_failure_detail(stdout, stderr)
    normalized = detail.casefold()
    nonretryable_markers = (
        "not supported when using codex with a chatgpt account",
        "authentication required",
        "not authenticated",
        "invalid model",
        "model_not_found",
    )
    message = f"codex_exit_{returncode}:{detail}"
    if any(marker in normalized for marker in nonretryable_markers):
        raise LunaConfigurationError(message)
    raise LunaTransportError(message)


def validate_response(value: Any, expected_ids: list[str]) -> list[dict[str, Any]]:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    required = set(schema["properties"]["results"]["items"]["required"])
    if (
        not isinstance(value, dict)
        or set(value) != {"results"}
        or not isinstance(value["results"], list)
    ):
        raise LunaBatchError("response_schema_top_level")
    results = value["results"]
    for row in results:
        if not isinstance(row, dict) or set(row) != required:
            raise LunaBatchError("response_schema_item")
        if row["label"] not in {"clean", "suspicious"} or row["confidence"] not in {
            "low",
            "medium",
            "high",
        }:
            raise LunaBatchError("response_schema_enum")
        if not isinstance(row["id"], str) or any(
            not isinstance(row[key], list) or not all(isinstance(x, str) for x in row[key])
            for key in ("suspicious_spans", "reason_codes")
        ):
            raise LunaBatchError("response_schema_types")
    ids = [x["id"] for x in results]
    if len(ids) != len(set(ids)) or len(ids) != len(expected_ids) or set(ids) != set(expected_ids):
        raise LunaBatchError("missing_duplicate_or_unknown_result_id")
    by_id = {x["id"]: x for x in results}
    return [by_id[x] for x in expected_ids]


def _command(isolated: Path) -> list[str]:
    executable = shutil.which("codex")
    if not executable:
        raise ExternalServiceUnavailable("luna_unavailable: Codex CLI executable missing")
    return [
        executable,
        "exec",
        "-m",
        MODEL,
        "-c",
        f"model_reasoning_effort={EFFORT}",
        "-c",
        f"model_reasoning_summary={SUMMARY}",
        "-c",
        "features.fast_mode=false",
        "-s",
        "read-only",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
        "-C",
        str(isolated),
        "--output-schema",
        str(SCHEMA_PATH),
        "--json",
        "-",
    ]


def call_once(
    items: list[dict[str, Any]],
    *,
    isolated: Path,
    timeout_seconds: int = 180,
    observation_callback: Any = None,
    observation_metadata: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], float]:
    if not 1 <= len(items) <= 50:
        raise ValueError("Luna batch must contain 1-50 items")
    safe = [
        {key: x[key] for key in ("id", "original", "candidate", "changed_spans")} for x in items
    ]
    ids = [x["id"] for x in safe]
    if len(ids) != len(set(ids)):
        raise LunaBatchError("duplicate_input_id")
    prompt_prefix = PROMPT_PATH.read_text(encoding="utf-8")
    payload_json = json.dumps(
        {"items": safe}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    prompt = (
        prompt_prefix
        + "\n\n入力JSON:\n"
        + payload_json
    )
    environment = os.environ.copy()
    if "CODEX_HOME" not in environment and environment.get("USERPROFILE"):
        environment["CODEX_HOME"] = str(Path(environment["USERPROFILE"]) / ".codex")
    for key in (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_ORG_ID",
        "OPENAI_PROJECT_ID",
        "CODEX_API_KEY",
    ):
        environment.pop(key, None)
    isolated.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    stdout = ""
    output_characters = 0
    status = "failed"
    error_kind: str | None = None
    try:
        try:
            completed = subprocess.run(
                _command(isolated),
                input=prompt,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                env=environment,
                cwd=isolated,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise LunaTransportError("codex_timeout") from exc
        latency = time.perf_counter() - started
        stdout = completed.stdout or ""
        if completed.returncode:
            _raise_codex_failure(completed.returncode, stdout, completed.stderr or "")
        try:
            events = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        except json.JSONDecodeError as exc:
            raise LunaBatchError("codex_event_json_malformed") from exc
        if any(x.get("type") in {"turn.failed", "error"} for x in events):
            raise LunaBatchError("codex_turn_failed")
        if any(
            x.get("item", {}).get("type") not in {None, "agent_message"}
            for x in events
            if x.get("type", "").startswith("item.")
        ):
            raise LunaBatchError("unexpected_tool_or_nonmessage_item")
        messages = [
            x["item"]["text"]
            for x in events
            if x.get("type") == "item.completed"
            and x.get("item", {}).get("type") == "agent_message"
        ]
        if len(messages) != 1 or sum(
            x.get("type") == "turn.completed" for x in events
        ) != 1:
            raise LunaBatchError("codex_event_shape")
        output_characters = len(messages[0])
        try:
            parsed = json.loads(messages[0])
        except json.JSONDecodeError as exc:
            raise LunaBatchError("model_json_malformed") from exc
        result = validate_response(parsed, ids)
        status = "completed"
        return result, latency
    except Exception as exc:
        error_kind = type(exc).__name__
        raise
    finally:
        if observation_callback is not None:
            observation = {
                "stage": "screen_proofreading_with_luna",
                "model": MODEL,
                "reasoning_effort": EFFORT,
                "status": status,
                "prompt_characters": len(prompt),
                "prompt_utf8_bytes": len(prompt.encode("utf-8")),
                "payload_characters": len(payload_json),
                "context_characters": 0,
                "schema_characters": len(SCHEMA_PATH.read_text(encoding="utf-8")),
                "output_characters": output_characters,
                "payload_sha256": hashlib.sha256(payload_json.encode()).hexdigest(),
                "prompt_prefix_sha256": hashlib.sha256(
                    prompt_prefix.encode()
                ).hexdigest(),
                "latency_seconds": time.perf_counter() - started,
                "error_kind": error_kind,
                "usage": extract_codex_usage(stdout),
                "metadata": dict(observation_metadata or {}),
            }
            with suppress(Exception):
                observation_callback(observation)


def evaluate_batch(
    items: list[dict[str, Any]],
    *,
    isolated: Path,
    batch_index: int,
    record_attempt: Any,
    observation_callback: Any = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Retry malformed output, then split it; never multiply transport failures."""
    all_ids = [x["id"] for x in items]
    results: dict[str, dict[str, Any]] = {}
    attempt_index = 0

    def attempt(part: list[dict[str, Any]], kind: str) -> bool:
        nonlocal attempt_index
        attempt_index += 1
        started = time.perf_counter()
        try:
            received, latency = call_once(
                part,
                isolated=isolated,
                observation_callback=observation_callback,
                observation_metadata={
                    "invocation_kind": "proofreading_luna",
                    "batch_index": batch_index,
                    "attempt_index": attempt_index,
                    "attempt_kind": kind,
                },
            )
            record_attempt(
                batch_index, attempt_index, [x["id"] for x in part], "completed", "", latency, kind
            )
            results.update({x["id"]: x for x in received})
            return True
        except (LunaConfigurationError, LunaTransportError) as exc:
            record_attempt(
                batch_index,
                attempt_index,
                [x["id"] for x in part],
                "failed",
                f"{type(exc).__name__}:{exc}",
                time.perf_counter() - started,
                kind,
            )
            raise
        except (ExternalServiceUnavailable, OSError) as exc:
            record_attempt(
                batch_index,
                attempt_index,
                [x["id"] for x in part],
                "failed",
                f"{type(exc).__name__}:{exc}",
                time.perf_counter() - started,
                kind,
            )
            raise LunaTransportError(str(exc) or type(exc).__name__) from exc
        except LunaBatchError as exc:
            record_attempt(
                batch_index,
                attempt_index,
                [x["id"] for x in part],
                "failed",
                f"{type(exc).__name__}:{exc}",
                time.perf_counter() - started,
                kind,
            )
            return False

    if not attempt(items, "full_initial") and not attempt(items, "full_retry"):
        for offset in range(0, len(items), 5):
            part = items[offset : offset + 5]
            if not attempt(part, "split_five"):
                for item in part:
                    attempt([item], "single_retry")
    return [results[x] for x in all_ids if x in results], [x for x in all_ids if x not in results]


def run_batches(
    items: list[dict[str, Any]],
    *,
    isolated: Path,
    record_attempt: Any,
    concurrency: int = 3,
    batch_size: int = 50,
    observation_callback: Any = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    if len({x["id"] for x in items}) != len(items):
        raise ValueError("duplicate Luna target ID")
    if not 1 <= batch_size <= 50:
        raise ValueError("Luna batch_size must be between 1 and 50")
    grouped = [items[i : i + batch_size] for i in range(0, len(items), batch_size)]
    records = {}
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {
            pool.submit(
                evaluate_batch,
                part,
                isolated=isolated,
                batch_index=index,
                record_attempt=record_attempt,
                observation_callback=observation_callback,
            ): index
            for index, part in enumerate(grouped)
        }
        for future in as_completed(futures):
            try:
                records[futures[future]] = future.result()
            except (LunaConfigurationError, LunaTransportError):
                for pending in futures:
                    pending.cancel()
                raise
    output, missing = [], []
    for index in range(len(grouped)):
        received, lost = records[index]
        output.extend(received)
        missing.extend(lost)
    return output, missing
