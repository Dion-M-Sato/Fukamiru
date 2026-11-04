"""Lazy, owned-process llama-server adapter using OpenAI-compatible JSON responses."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fukamiru.config import ModelSettings
from fukamiru.errors import FukamiruError, NonRetryableFukamiruError


@dataclass(frozen=True)
class ContinuationScore:
    """Raw base-model likelihood for one exact continuation."""

    text: str
    token_ids: tuple[int, ...]
    token_pieces: tuple[str, ...]
    token_logprobs: tuple[float, ...]
    raw_logprob_sum: float
    mean_token_logprob: float
    mean_character_logprob: float


class LlamaServerAdapter:
    _STARTUP_HEALTH_POLL_SECONDS = 3.0

    def __init__(
        self,
        settings: ModelSettings,
        *,
        runtime_root: Path,
        model_name: str,
    ) -> None:
        self.settings = settings
        self.runtime_root = runtime_root
        self.model_name = model_name
        self._lock = threading.RLock()
        self._process: subprocess.Popen[bytes] | None = None
        self._log_handle: Any | None = None
        self._state = "stopped" if self.configured else "not_configured"
        self._last_error: str | None = None
        self._owned = False
        self._token_cache: dict[tuple[str, bool], tuple[tuple[int, str], ...]] = {}
        self._detokenize_cache: dict[tuple[int, ...], str] = {}

    @property
    def configured(self) -> bool:
        return bool(
            self.settings.path
            and self.settings.path.is_file()
            and shutil.which(self.settings.server_command)
        )

    @property
    def base_url(self) -> str:
        host = "127.0.0.1" if self.settings.host == "localhost" else self.settings.host
        return f"http://{host}:{self.settings.port}"

    @property
    def model_identity(self) -> str:
        if self.settings.path is None or not self.settings.path.is_file():
            return f"{self.model_name}:not-configured"
        stat = self.settings.path.stat()
        identity = f"{self.settings.path.name}:{stat.st_size}:{stat.st_mtime_ns}"
        command = shutil.which(self.settings.server_command)
        if command:
            backend = Path(command).stat()
            identity += f":{backend.st_size}:{backend.st_mtime_ns}"
        identity += f":{self.settings.context_size}:adapter-v3-raw-continuation"
        return hashlib.sha256(identity.encode()).hexdigest()

    def status(self) -> dict[str, Any]:
        with self._lock:
            if self._process is not None and self._process.poll() is not None:
                self._state = "error" if self._process.returncode else "stopped"
            return {
                "configured": self.configured,
                "status": self._state,
                "model": self.model_name,
                "model_path": str(self.settings.path) if self.settings.path else None,
                "server_command": self.settings.server_command,
                "endpoint": self.base_url,
                "configured_device": self.settings.device or "auto",
                "context_size": self.settings.context_size,
                "gpu_layers": self.settings.gpu_layers,
                "fit": self.settings.fit,
                "warmup": self.settings.warmup,
                "parallel_slots": self.settings.parallel_slots,
                "http_threads": self.settings.http_threads,
                "webui": self.settings.webui,
                "pid": (
                    self._process.pid if self._process and self._process.poll() is None else None
                ),
                "owned_process": self._owned,
                "last_error": self._last_error,
            }

    def ensure_started(self) -> None:
        with self._lock:
            if not self.configured:
                raise FukamiruError(
                    f"{self.model_name} is not configured with an existing model and llama-server"
                )
            if self._health(timeout=0.5):
                self._state = "ready"
                return
            executable = shutil.which(self.settings.server_command)
            assert executable is not None
            assert self.settings.path is not None
            log_root = self.runtime_root / "logs" / "local-llm"
            log_root.mkdir(parents=True, exist_ok=True)
            log_path = log_root / f"{self.model_name}.log"
            self._log_handle = log_path.open("ab", buffering=0)
            command = [
                executable,
                "--model",
                str(self.settings.path),
                "--host",
                self.settings.host,
                "--port",
                str(self.settings.port),
                "--ctx-size",
                str(self.settings.context_size),
                "--n-gpu-layers",
                str(self.settings.gpu_layers),
            ]
            if self.settings.device:
                command.extend(["--device", self.settings.device])
            command.extend(["--fit", "on" if self.settings.fit else "off"])
            if not self.settings.warmup:
                command.append("--no-warmup")
            command.extend(["--parallel", str(self.settings.parallel_slots)])
            if self.settings.http_threads >= 0:
                command.extend(["--threads-http", str(self.settings.http_threads)])
            if not self.settings.webui:
                command.append("--no-webui")
            creation_flags = (
                int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0
            )
            self._state = "starting"
            self._last_error = None
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=self._log_handle,
                stderr=subprocess.STDOUT,
                cwd=self.runtime_root,
                creationflags=creation_flags,
            )
            self._owned = True
        deadline = time.monotonic() + self.settings.startup_timeout_seconds
        while time.monotonic() < deadline:
            with self._lock:
                process = self._process
                if process is None:
                    break
                return_code = process.poll()
                if return_code is not None:
                    self._state = "error"
                    self._last_error = f"llama-server exited during startup ({return_code})"
                    raise FukamiruError(self._last_error)
            if self._health(timeout=0.5):
                with self._lock:
                    self._state = "ready"
                return
            time.sleep(
                min(
                    self._STARTUP_HEALTH_POLL_SECONDS,
                    max(0.0, deadline - time.monotonic()),
                )
            )
        with self._lock:
            self._last_error = "llama-server startup timed out"
        self.stop()
        with self._lock:
            self._state = "error"
        raise FukamiruError(f"{self.model_name} llama-server startup timed out")

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.0,
        max_tokens: int = 2048,
        json_schema: dict[str, Any] | None = None,
        top_p: float | None = None,
        top_k: int | None = None,
        repeat_penalty: float | None = None,
        seed: int | None = None,
    ) -> dict[str, Any]:
        self.ensure_started()
        body = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": (
                {
                    "type": "json_schema",
                    "json_schema": {
                        "name": f"{self.model_name}-response",
                        "strict": True,
                        "schema": json_schema,
                    },
                }
                if json_schema is not None
                else {"type": "json_object"}
            ),
        }
        if top_p is not None:
            body["top_p"] = top_p
        if top_k is not None:
            body["top_k"] = top_k
        if repeat_penalty is not None:
            body["repeat_penalty"] = repeat_penalty
        if seed is not None:
            body["seed"] = seed
        with self._lock:
            self._state = "busy"
        try:
            for attempt in range(2):
                response = self._request_json(
                    "/v1/chat/completions",
                    body,
                    timeout=self.settings.request_timeout_seconds,
                )
                try:
                    choice = response["choices"][0]
                    content = str(choice["message"]["content"]).strip()
                except (KeyError, IndexError, TypeError) as exc:
                    raise FukamiruError(
                        f"{self.model_name} returned an invalid completion response"
                    ) from exc
                if content.startswith("```"):
                    content = content.removeprefix("```json").removeprefix("```")
                    content = content.removesuffix("```").strip()
                finish_reason = str(choice.get("finish_reason") or "").lower()
                usage = response.get("usage") or {}
                completion_tokens = int(usage.get("completion_tokens") or 0)
                requested_tokens = int(body["max_tokens"])
                truncated = finish_reason in {"length", "max_tokens"} or (
                    completion_tokens >= requested_tokens > 0
                )
                try:
                    parsed = json.loads(content)
                except json.JSONDecodeError as exc:
                    retry_limit = max(
                        requested_tokens,
                        min(self.settings.context_size // 2, requested_tokens * 2),
                    )
                    if truncated and attempt == 0 and retry_limit > requested_tokens:
                        body = {**body, "max_tokens": retry_limit}
                        continue
                    detail = (
                        f" (truncated at {requested_tokens} tokens; "
                        f"finish_reason={finish_reason or 'unknown'})"
                        if truncated
                        else f" (finish_reason={finish_reason or 'unknown'})"
                    )
                    raise FukamiruError(
                        f"{self.model_name} returned invalid structured output{detail}"
                    ) from exc
                if not isinstance(parsed, dict):
                    raise FukamiruError(f"{self.model_name} returned non-object JSON")
                return parsed
            raise FukamiruError(f"{self.model_name} returned invalid structured output")
        finally:
            with self._lock:
                if self._state != "error":
                    self._state = "ready"

    def tokenize(
        self,
        text: str,
        *,
        add_special: bool = False,
        with_pieces: bool = True,
    ) -> tuple[tuple[int, str], ...]:
        """Tokenize through the model server so scores use its exact vocabulary."""

        self.ensure_started()
        cache_key = (text, add_special)
        if with_pieces and cache_key in self._token_cache:
            return self._token_cache[cache_key]
        response = self._request_json(
            "/tokenize",
            {
                "content": text,
                "add_special": add_special,
                "with_pieces": with_pieces,
            },
            timeout=self.settings.request_timeout_seconds,
        )
        raw_tokens = response.get("tokens")
        if not isinstance(raw_tokens, list):
            raise FukamiruError(f"{self.model_name} returned invalid tokenization")
        result: list[tuple[int, str]] = []
        for item in raw_tokens:
            if isinstance(item, dict):
                token_id = item.get("id", item.get("token"))
                piece = item.get("piece", item.get("content", ""))
            else:
                token_id, piece = item, ""
            if not isinstance(token_id, int):
                raise FukamiruError(f"{self.model_name} returned an invalid token id")
            result.append((token_id, str(piece or "")))
        resolved = tuple(result)
        if with_pieces:
            self._token_cache[cache_key] = resolved
        return resolved

    def detokenize(self, token_ids: list[int] | tuple[int, ...]) -> str:
        self.ensure_started()
        cache_key = tuple(token_ids)
        if cache_key in self._detokenize_cache:
            return self._detokenize_cache[cache_key]
        response = self._request_json(
            "/detokenize",
            {"tokens": list(token_ids)},
            timeout=self.settings.request_timeout_seconds,
        )
        content = response.get("content")
        if not isinstance(content, str):
            raise FukamiruError(f"{self.model_name} returned invalid detokenization")
        self._detokenize_cache[cache_key] = content
        return content

    def score_continuation(self, *, prefix: str, continuation: str) -> ContinuationScore:
        """Score an exact continuation with raw, pre-bias llama.cpp log probabilities.

        Grammar and logit bias force the requested token sequence. They are only a
        decoding constraint: ``post_sampling_probs=false`` keeps the outer generated
        token log probabilities on the unmodified base-model distribution.  A literal
        grammar constrains bytes rather than tokenizer identity, so an ambiguous
        segmentation may still return different token ids.  In that case the adapter
        falls back to teacher-forcing one canonical token at a time.
        """

        if not continuation:
            raise NonRetryableFukamiruError("continuation_score.empty_continuation")
        if any(character in continuation for character in "\r\n\t"):
            raise NonRetryableFukamiruError("continuation_score.control_character")
        self.ensure_started()
        prefix_tokens = self.tokenize(prefix, add_special=True)
        continuation_tokens = list(self.tokenize(continuation, add_special=False))
        # SentencePiece exposes its dummy standalone leading space as a real token.
        while continuation_tokens and continuation_tokens[0][1].isspace():
            continuation_tokens.pop(0)
        if not continuation_tokens:
            raise NonRetryableFukamiruError("continuation_score.no_candidate_tokens")
        prefix_ids = tuple(item[0] for item in prefix_tokens)
        candidate_ids = tuple(item[0] for item in continuation_tokens)
        candidate_pieces = tuple(item[1] for item in continuation_tokens)
        joined = self.detokenize((*prefix_ids, *candidate_ids))
        prefix_rendered = self.detokenize(prefix_ids)
        if joined != prefix_rendered + continuation:
            raise NonRetryableFukamiruError("continuation_score.token_boundary_mismatch")
        body = {
            "prompt": list(prefix_ids),
            "n_predict": len(candidate_ids) + 1,
            "temperature": -1.0,
            "top_k": 0,
            "top_p": 1.0,
            "min_p": 0.0,
            "typical_p": 1.0,
            "repeat_penalty": 1.0,
            "presence_penalty": 0.0,
            "frequency_penalty": 0.0,
            "dry_multiplier": 0.0,
            "n_probs": 1,
            "return_tokens": True,
            "cache_prompt": True,
            "post_sampling_probs": False,
            "seed": 0,
            "grammar": "root ::= " + json.dumps(continuation, ensure_ascii=False),
            "logit_bias": [[token_id, 100.0] for token_id in sorted(set(candidate_ids))],
        }

        def probability_logprob(expected: int, item: Any) -> float:
            if not isinstance(item, dict):
                raise FukamiruError(
                    f"{self.model_name} returned an invalid token probability"
                )
            item_id = item.get("id", item.get("token"))
            if item_id is not None and int(item_id) != expected:
                raise NonRetryableFukamiruError(
                    "continuation_score.probability_token_mismatch"
                )
            value = float(item.get("logprob"))
            if not (-1.0e10 < value <= 0.0):
                raise FukamiruError(
                    f"{self.model_name} returned a non-finite raw log probability"
                )
            return value

        def teacher_forced_logprobs() -> list[float]:
            forced: list[float] = []
            for index, expected in enumerate(candidate_ids):
                remaining = "".join(candidate_pieces[index:])
                if not remaining:
                    raise NonRetryableFukamiruError(
                        "continuation_score.empty_teacher_forcing_suffix"
                    )
                step_body = {
                    **body,
                    "prompt": [*prefix_ids, *candidate_ids[:index]],
                    "n_predict": 1,
                    "grammar": "root ::= " + json.dumps(remaining, ensure_ascii=False),
                    "logit_bias": [[expected, 100.0]],
                }
                step = self._request_json(
                    "/completion",
                    step_body,
                    timeout=self.settings.request_timeout_seconds,
                )
                step_probabilities = step.get("completion_probabilities")
                step_tokens = step.get("tokens")
                if not isinstance(step_probabilities, list) or not isinstance(
                    step_tokens, list
                ):
                    raise FukamiruError(
                        f"{self.model_name} returned invalid continuation probabilities"
                    )
                returned_id = (
                    int(step_tokens[0].get("id", step_tokens[0].get("token")))
                    if step_tokens and isinstance(step_tokens[0], dict)
                    else int(step_tokens[0])
                    if step_tokens
                    else None
                )
                if returned_id != expected:
                    raise NonRetryableFukamiruError(
                        "continuation_score.forced_token_mismatch:"
                        f"index={index}:expected={expected}:returned={returned_id}"
                    )
                if not step_probabilities:
                    raise FukamiruError(
                        f"{self.model_name} returned incomplete continuation probabilities"
                    )
                forced.append(probability_logprob(expected, step_probabilities[0]))
            return forced

        with self._lock:
            self._state = "busy"
        try:
            response = self._request_json(
                "/completion",
                body,
                timeout=self.settings.request_timeout_seconds,
            )
            probabilities = response.get("completion_probabilities")
            returned = response.get("tokens")
            if not isinstance(probabilities, list) or not isinstance(returned, list):
                raise FukamiruError(
                    f"{self.model_name} returned invalid continuation probabilities"
                )
            returned_ids = tuple(
                int(item.get("id", item.get("token"))) if isinstance(item, dict) else int(item)
                for item in returned[: len(candidate_ids)]
            )
            if returned_ids != candidate_ids:
                logprobs = teacher_forced_logprobs()
            else:
                logprobs = [
                    probability_logprob(expected, item)
                    for expected, item in zip(candidate_ids, probabilities, strict=False)
                ]
            if len(logprobs) != len(candidate_ids):
                raise FukamiruError(
                    f"{self.model_name} returned incomplete continuation probabilities"
                )
        finally:
            with self._lock:
                if self._state != "error":
                    self._state = "ready"
        total = sum(logprobs)
        return ContinuationScore(
            text=continuation,
            token_ids=candidate_ids,
            token_pieces=candidate_pieces,
            token_logprobs=tuple(logprobs),
            raw_logprob_sum=total,
            mean_token_logprob=total / len(logprobs),
            mean_character_logprob=total / max(1, len(continuation)),
        )

    def stop(self) -> None:
        with self._lock:
            process = self._process
            if process is not None and self._owned and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
            self._process = None
            self._owned = False
            self._state = "stopped" if self.configured else "not_configured"
            if self._log_handle is not None:
                self._log_handle.close()
                self._log_handle = None

    def _health(self, *, timeout: float) -> bool:
        try:
            request = urllib.request.Request(f"{self.base_url}/health", method="GET")
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return 200 <= response.status < 300
        except (OSError, urllib.error.URLError):
            return False

    def _request_json(
        self, path: str, payload: dict[str, Any], *, timeout: float
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[-2000:]
            if any(
                marker in detail.lower()
                for marker in (
                    "repetition exceeds",
                    "failed to parse grammar",
                    "invalid grammar",
                    "failed to initialize grammar",
                )
            ):
                raise NonRetryableFukamiruError(
                    f"grammar_compile_error: {self.model_name} HTTP {exc.code}: {detail}"
                ) from exc
            raise FukamiruError(f"{self.model_name} server HTTP {exc.code}: {detail}") from exc
        except (OSError, urllib.error.URLError) as exc:
            raise FukamiruError(f"{self.model_name} server request failed: {exc}") from exc
        if not isinstance(result, dict):
            raise FukamiruError(f"{self.model_name} server returned an invalid response")
        return result
