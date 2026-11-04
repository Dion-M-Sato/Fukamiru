"""BGE-M3 embeddings and immutable FAISS snapshot management."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from contextlib import nullcontext
from functools import lru_cache
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Protocol

from fukamiru.config import Settings
from fukamiru.errors import FukamiruError, NonRetryableFukamiruError
from fukamiru.knowledge.batching import EmbeddingBatchItem, plan_embedding_batches
from fukamiru.knowledge.model_cache import (
    configure_huggingface_offline,
    resolve_local_model_snapshot,
)
from fukamiru.knowledge.service import utc_now


class EmbeddingProvider(Protocol):
    @property
    def model_name(self) -> str: ...

    def encode(self, texts: Sequence[str]) -> list[list[float]]: ...


@lru_cache(maxsize=1)
def torch_runtime_status() -> dict[str, Any]:
    """Return import-safe CUDA diagnostics without loading the embedding model."""
    try:
        import torch
    except Exception as exc:
        return {
            "torch_version": None,
            "torch_cuda_version": None,
            "cuda_available": False,
            "gpu_name": None,
            "torch_error": str(exc) or exc.__class__.__name__,
        }
    cuda_available = bool(torch.cuda.is_available())
    return {
        "torch_version": str(torch.__version__),
        "torch_cuda_version": str(torch.version.cuda) if torch.version.cuda else None,
        "cuda_available": cuda_available,
        "gpu_name": str(torch.cuda.get_device_name(0)) if cuda_available else None,
        "torch_error": None,
    }


class SemanticIndex(Protocol):
    def build(
        self,
        progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]: ...

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        index_version_id: str | None = None,
    ) -> list[dict[str, Any]]: ...

    def available(self) -> bool: ...

    def status(self) -> dict[str, Any]: ...


class BgeM3EmbeddingProvider:
    """Lazy SentenceTransformers adapter so startup does not load the model."""

    def __init__(self, settings: Settings, *, gpu_mutex: Any | None = None) -> None:
        self.settings = settings
        self.gpu_mutex = gpu_mutex
        self._model: Any | None = None
        self._actual_device: str | None = None
        self._last_input_device: str | None = None
        self._batch_size = settings.retrieval.embedding_batch_size
        self._last_metrics: dict[str, float | int] = {}
        self._encode_call_count = 0
        self._actual_dtype: str | None = None
        self._actual_attention_backend: str | None = None

    @property
    def model_name(self) -> str:
        return self.settings.retrieval.embedding_model

    @property
    def model_revision(self) -> str:
        return self.settings.retrieval.embedding_model_revision

    @property
    def cache_identity(self) -> str:
        """Keep non-default numeric profiles out of the existing FP32 cache."""
        dtype_name = self.settings.retrieval.embedding_dtype
        attention = self.settings.retrieval.embedding_attention_backend
        if dtype_name == "float32" and attention == "auto":
            return self.model_name
        return f"{self.model_name}#dtype={dtype_name};attention={attention}"

    @property
    def actual_device(self) -> str | None:
        return self._actual_device

    @property
    def batch_size(self) -> int:
        return self._batch_size

    @property
    def last_metrics(self) -> dict[str, float | int]:
        return dict(self._last_metrics)

    def available(self) -> bool:
        return find_spec("sentence_transformers") is not None

    def preflight(self) -> dict[str, Any]:
        status = torch_runtime_status()
        if self.settings.retrieval.require_cuda and not status["cuda_available"]:
            detail = status["torch_error"] or (
                f"torch={status['torch_version']}, torch CUDA={status['torch_cuda_version']}"
            )
            raise NonRetryableFukamiruError(
                "CUDA is required for Corpus Sync, but torch.cuda.is_available() is false "
                f"({detail}). CPU fallback is disabled."
            )
        if (
            self.settings.retrieval.require_cuda
            and self.settings.retrieval.embedding_device != "cuda"
        ):
            raise NonRetryableFukamiruError(
                "CUDA is required for Corpus Sync, but embedding_device is not 'cuda'."
            )
        snapshot = resolve_local_model_snapshot(
            cache_dir=self.settings.retrieval.embedding_cache_dir,
            model_name=self.model_name,
            revision=self.model_revision,
        )
        return {
            **status,
            "embedding_cache_dir": str(self.settings.retrieval.embedding_cache_dir),
            "local_model_path": str(snapshot),
            "local_model_ready": True,
        }

    def diagnostics(self) -> dict[str, Any]:
        try:
            local_model_path = str(
                resolve_local_model_snapshot(
                    cache_dir=self.settings.retrieval.embedding_cache_dir,
                    model_name=self.model_name,
                    revision=self.model_revision,
                )
            )
            local_model_ready = True
        except NonRetryableFukamiruError:
            local_model_path = None
            local_model_ready = False
        return {
            **torch_runtime_status(),
            "configured_device": self.settings.retrieval.embedding_device,
            "actual_embedding_device": self.actual_device or "not_loaded",
            "embedding_model": self.model_name,
            "embedding_model_revision": self.model_revision,
            "embedding_cache_dir": str(self.settings.retrieval.embedding_cache_dir),
            "local_model_path": local_model_path,
            "local_model_ready": local_model_ready,
            "embedding_batch_size": self.batch_size,
            "configured_batch_strategy": (self.settings.retrieval.embedding_batch_strategy),
            "configured_max_tokens_per_batch": (
                self.settings.retrieval.embedding_max_tokens_per_batch
            ),
            "configured_dtype": self.settings.retrieval.embedding_dtype,
            "actual_embedding_dtype": self._actual_dtype or "not_loaded",
            "configured_attention_backend": (self.settings.retrieval.embedding_attention_backend),
            "actual_attention_backend": self._actual_attention_backend or "not_loaded",
            "measurement_interval_batches": (
                self.settings.retrieval.embedding_measurement_interval_batches
            ),
            "last_input_device": self._last_input_device,
            "require_cuda": self.settings.retrieval.require_cuda,
        }

    def _load(self) -> Any:
        if self._model is not None:
            self._restore_cuda_model(self._model)
            return self._model
        # Resolve the pinned snapshot ourselves. Passing a repository ID to the
        # library would still enter Hub resolution and emit misleading network
        # errors when an isolated runtime has no cache.
        cache_folder = self.settings.retrieval.embedding_cache_dir
        configure_huggingface_offline(cache_folder)
        snapshot = resolve_local_model_snapshot(
            cache_dir=cache_folder,
            model_name=self.model_name,
            revision=self.model_revision,
        )
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise NonRetryableFukamiruError(
                "Semantic RAG dependencies are unavailable; install the 'rag' extra"
            ) from exc
        device = self.settings.retrieval.embedding_device
        import torch

        dtype_by_name = {
            "float32": torch.float32,
            "float16": torch.float16,
            "bfloat16": torch.bfloat16,
        }
        model_kwargs: dict[str, Any] = {
            "dtype": dtype_by_name[self.settings.retrieval.embedding_dtype]
        }
        attention_backend = self.settings.retrieval.embedding_attention_backend
        if attention_backend != "auto":
            model_kwargs["attn_implementation"] = attention_backend
        try:
            model = SentenceTransformer(
                str(snapshot),
                device=None if device == "auto" else device,
                local_files_only=True,
                token=False,
                model_kwargs=model_kwargs,
            )
        except (OSError, ValueError) as exc:
            raise NonRetryableFukamiruError(
                f"Pinned embedding model could not be loaded from {snapshot}: {exc}"
            ) from exc
        model_device = getattr(model, "device", None)
        if model_device is None:
            try:
                model_device = next(model.parameters()).device
            except (StopIteration, AttributeError):
                model_device = None
        self._actual_device = str(model_device) if model_device is not None else "unknown"
        try:
            self._actual_dtype = str(next(model.parameters()).dtype).removeprefix("torch.")
        except (StopIteration, AttributeError):
            self._actual_dtype = "unknown"
        try:
            auto_model = model[0].auto_model
            self._actual_attention_backend = str(
                getattr(auto_model.config, "_attn_implementation", None) or "unknown"
            )
        except (AttributeError, IndexError, KeyError, TypeError):
            self._actual_attention_backend = "unknown"
        if self.settings.retrieval.require_cuda and not self._actual_device.startswith("cuda"):
            raise NonRetryableFukamiruError(
                "BGE-M3 loaded on "
                f"{self._actual_device!r} while CUDA is required. CPU fallback is disabled."
            )
        self._model = model
        return model

    def _restore_cuda_model(self, model: Any) -> None:
        if self.settings.retrieval.embedding_device != "cuda":
            return
        try:
            device = str(next(model.parameters()).device)
        except (StopIteration, AttributeError):
            device = str(getattr(model, "device", "unknown"))
        if device.startswith("cuda"):
            self._actual_device = device
            return
        model.to("cuda")
        try:
            device = str(next(model.parameters()).device)
        except (StopIteration, AttributeError):
            device = str(getattr(model, "device", "cuda"))
        self._actual_device = device
        if self.settings.retrieval.require_cuda and not device.startswith("cuda"):
            raise NonRetryableFukamiruError(
                "BGE-M3 could not resume on CUDA after Local LLM yield"
            )

    def _yield_cuda_model(self) -> None:
        if self._model is None or self.settings.retrieval.embedding_device != "cuda":
            return
        import torch

        torch.cuda.synchronize()
        self._model.to("cpu")
        torch.cuda.empty_cache()
        self._actual_device = "cpu (yielded for local LLM)"

    @staticmethod
    def _collect_tensor_devices(value: Any, devices: set[str]) -> None:
        if isinstance(value, dict):
            for nested in value.values():
                BgeM3EmbeddingProvider._collect_tensor_devices(nested, devices)
        elif isinstance(value, (tuple, list)):
            for nested in value:
                BgeM3EmbeddingProvider._collect_tensor_devices(nested, devices)
        elif hasattr(value, "device"):
            devices.add(str(value.device))

    def _gpu_guard(self) -> Any:
        if self.gpu_mutex is None or self.settings.retrieval.embedding_device != "cuda":
            return nullcontext()
        return self.gpu_mutex.hold(
            "corpus:bge-m3", on_priority_waiter=self._yield_cuda_model
        )

    @staticmethod
    def _input_observer_module(model: Any) -> Any:
        """Return the module that receives token tensors during encode.

        SentenceTransformer.encode calls its child modules directly, so a hook on the
        outer SentenceTransformer is not guaranteed to run.  The first Transformer
        module's auto_model receives input_ids/attention_mask on every forward pass.
        """
        try:
            transformer = model[0]
        except (IndexError, KeyError, TypeError):
            transformer = None
        auto_model = getattr(transformer, "auto_model", None)
        if auto_model is None:
            return model
        # Transformers 5 may call auto_model.forward directly, bypassing hooks on
        # auto_model itself.  Its embeddings child is still invoked through Module
        # and receives input_ids on the selected execution device.
        return getattr(auto_model, "embeddings", None) or auto_model

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        self.preflight()
        with self._gpu_guard():
            model = self._load()
            self._encode_call_count += 1
            measurement_interval = self.settings.retrieval.embedding_measurement_interval_batches
            measure_gpu_timing = (self._encode_call_count - 1) % measurement_interval == 0
            while True:
                input_devices: set[str] = set()
                token_count = 0
                padded_token_count = 0
                tokenized_text_count = 0
                tokenization_seconds = 0.0
                gpu_start: Any | None = None
                gpu_end: Any | None = None

                original_tokenize = model.tokenize

                def measured_tokenize(
                    batch_texts: Any,
                    tokenize: Callable[[Any], Any] = original_tokenize,
                    **kwargs: Any,
                ) -> Any:
                    nonlocal token_count, padded_token_count
                    nonlocal tokenized_text_count, tokenization_seconds
                    tokenization_started = time.perf_counter()
                    features = tokenize(batch_texts, **kwargs)
                    tokenization_seconds += time.perf_counter() - tokenization_started
                    attention_mask = features.get("attention_mask")
                    input_ids = features.get("input_ids")
                    if attention_mask is not None:
                        token_count += int(attention_mask.sum().item())
                        tokenized_text_count += int(attention_mask.shape[0])
                        padded_token_count += int(
                            input_ids.numel() if input_ids is not None else attention_mask.numel()
                        )
                    elif input_ids is not None:
                        token_count += int(input_ids.numel())
                        padded_token_count += int(input_ids.numel())
                        tokenized_text_count += int(input_ids.shape[0])
                    return features

                def capture_devices(
                    _: Any,
                    args: Any,
                    kwargs: Any,
                    devices: set[str] = input_devices,
                ) -> None:
                    nonlocal gpu_start
                    self._collect_tensor_devices(args, devices)
                    self._collect_tensor_devices(kwargs, devices)
                    if (
                        measure_gpu_timing
                        and gpu_start is None
                        and any(device.startswith("cuda") for device in devices)
                    ):
                        import torch

                        gpu_start = torch.cuda.Event(enable_timing=True)
                        gpu_start.record()

                observer = self._input_observer_module(model)
                hook = observer.register_forward_pre_hook(
                    capture_devices,
                    with_kwargs=True,
                )
                model.tokenize = measured_tokenize
                encode_started = time.perf_counter()
                try:
                    vectors = model.encode(
                        list(texts),
                        batch_size=self._batch_size,
                        normalize_embeddings=True,
                        show_progress_bar=False,
                        convert_to_numpy=True,
                    )
                    if gpu_start is not None and measure_gpu_timing:
                        import torch

                        gpu_end = torch.cuda.Event(enable_timing=True)
                        gpu_end.record()
                        torch.cuda.synchronize()
                    break
                except Exception as exc:
                    try:
                        import torch
                    except ImportError:
                        raise
                    is_oom = isinstance(exc, torch.cuda.OutOfMemoryError) or (
                        "CUDA out of memory" in str(exc)
                    )
                    if not is_oom:
                        raise
                    torch.cuda.empty_cache()
                    minimum = self.settings.retrieval.embedding_batch_min_size
                    if self._batch_size <= minimum:
                        raise FukamiruError(
                            "CUDA OOM persisted at the minimum embedding batch size. "
                            "The completed embedding cache is preserved; CPU fallback was not run."
                        ) from exc
                    self._batch_size = max(minimum, self._batch_size // 2)
                finally:
                    model.tokenize = original_tokenize
                    hook.remove()
            encode_seconds = time.perf_counter() - encode_started
            gpu_inference_seconds = (
                float(gpu_start.elapsed_time(gpu_end)) / 1000.0
                if gpu_start is not None and gpu_end is not None
                else 0.0
            )
            self._last_metrics = {
                "text_count": tokenized_text_count or len(texts),
                "token_count": token_count,
                "actual_token_count": token_count,
                "padded_token_count": padded_token_count,
                "padding_token_count": max(padded_token_count - token_count, 0),
                "padding_ratio": (padded_token_count / token_count if token_count > 0 else 0.0),
                "average_tokens_per_chunk": (
                    token_count / tokenized_text_count if tokenized_text_count else 0.0
                ),
                "tokenization_seconds": tokenization_seconds,
                "gpu_inference_seconds": gpu_inference_seconds,
                "gpu_timing_sampled": int(measure_gpu_timing),
                "encode_seconds": encode_seconds,
                "inference_tokens_per_second": (
                    token_count / gpu_inference_seconds if gpu_inference_seconds > 0 else 0.0
                ),
            }
            self._last_input_device = ",".join(sorted(input_devices)) or "not_observed"
            if self.settings.retrieval.require_cuda and not any(
                device.startswith("cuda") for device in input_devices
            ):
                raise FukamiruError(
                    "BGE-M3 model is on CUDA, but encode inputs were not observed on CUDA."
                )
        return vectors.astype("float32", copy=False).tolist()


class EmbeddingResourceSampler:
    """Persist low-frequency process/GPU samples without blocking embedding batches."""

    def __init__(self, connection_factory: Any, build_id: str, interval_seconds: float = 2.0):
        self.connection_factory = connection_factory
        self.build_id = build_id
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"embedding-metrics-{build_id[:8]}",
            daemon=True,
        )

    def __enter__(self) -> EmbeddingResourceSampler:
        self._thread.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self._stop.set()
        self._thread.join(timeout=self.interval_seconds + 2.0)

    @staticmethod
    def _gpu_sample() -> tuple[float, float, float]:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            check=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        values = [float(value.strip()) for value in result.stdout.splitlines()[0].split(",")]
        return values[0], values[1], values[2]

    def _run(self) -> None:
        logical_cpus = max(os.cpu_count() or 1, 1)
        previous_wall = time.perf_counter()
        previous_cpu = time.process_time()
        while not self._stop.is_set():
            sampled_wall = time.perf_counter()
            sampled_cpu = time.process_time()
            wall_delta = max(sampled_wall - previous_wall, 0.001)
            cpu_percent = max(
                0.0,
                (sampled_cpu - previous_cpu) / wall_delta / logical_cpus * 100.0,
            )
            previous_wall = sampled_wall
            previous_cpu = sampled_cpu
            try:
                gpu_percent, vram_used, vram_total = self._gpu_sample()
                with self.connection_factory() as connection:
                    connection.execute(
                        """
                        INSERT INTO embedding_resource_samples(
                            build_id, sampled_at, gpu_utilization_percent,
                            vram_used_mib, vram_total_mib, cpu_utilization_percent
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            self.build_id,
                            utc_now(),
                            gpu_percent,
                            vram_used,
                            vram_total,
                            cpu_percent,
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE embedding_builds SET
                            gpu_utilization_avg =
                                (COALESCE(gpu_utilization_avg, 0) * resource_sample_count + ?)
                                / (resource_sample_count + 1),
                            gpu_utilization_max = MAX(COALESCE(gpu_utilization_max, ?), ?),
                            vram_used_avg_mib =
                                (COALESCE(vram_used_avg_mib, 0) * resource_sample_count + ?)
                                / (resource_sample_count + 1),
                            vram_used_max_mib = MAX(COALESCE(vram_used_max_mib, ?), ?),
                            vram_total_mib = ?,
                            cpu_utilization_avg =
                                (COALESCE(cpu_utilization_avg, 0) * resource_sample_count + ?)
                                / (resource_sample_count + 1),
                            cpu_utilization_max = MAX(COALESCE(cpu_utilization_max, ?), ?),
                            resource_sample_count = resource_sample_count + 1
                        WHERE id = ?
                        """,
                        (
                            gpu_percent,
                            gpu_percent,
                            gpu_percent,
                            vram_used,
                            vram_used,
                            vram_used,
                            vram_total,
                            cpu_percent,
                            cpu_percent,
                            cpu_percent,
                            self.build_id,
                        ),
                    )
                    connection.commit()
            except (
                OSError,
                sqlite3.OperationalError,
                subprocess.SubprocessError,
                ValueError,
                IndexError,
            ):
                pass
            self._stop.wait(self.interval_seconds)


class FaissSemanticIndex:
    """Build an atomic IndexFlatIP snapshot and publish it through Knowledge DB."""

    def __init__(
        self,
        connection_factory: Any,
        settings: Settings,
        provider: EmbeddingProvider | None = None,
        gpu_mutex: Any | None = None,
    ) -> None:
        self.connection_factory = connection_factory
        self.settings = settings
        self.provider = provider or BgeM3EmbeddingProvider(settings, gpu_mutex=gpu_mutex)

    @property
    def model_revision(self) -> str:
        return str(
            getattr(
                self.provider,
                "model_revision",
                self.settings.retrieval.embedding_model_revision,
            )
        )

    @property
    def model_storage_name(self) -> str:
        return f"{self.model_identity_name}@{self.model_revision}"

    @property
    def model_identity_name(self) -> str:
        return str(getattr(self.provider, "cache_identity", self.provider.model_name))

    def preflight(self) -> dict[str, Any]:
        if not self.available():
            raise NonRetryableFukamiruError(
                "Semantic RAG dependencies are unavailable; install the 'rag' extra"
            )
        provider_preflight = getattr(self.provider, "preflight", None)
        return provider_preflight() if provider_preflight else torch_runtime_status()

    def diagnostics(self) -> dict[str, Any]:
        provider_diagnostics = getattr(self.provider, "diagnostics", None)
        if provider_diagnostics:
            return dict(provider_diagnostics())
        return {
            **torch_runtime_status(),
            "configured_device": self.settings.retrieval.embedding_device,
            "actual_embedding_device": str(
                getattr(self.provider, "actual_device", "provider_managed")
            ),
            "embedding_model": self.provider.model_name,
            "embedding_model_revision": self.model_revision,
            "embedding_batch_size": int(
                getattr(
                    self.provider,
                    "batch_size",
                    self.settings.retrieval.embedding_batch_size,
                )
            ),
            "configured_dtype": self.settings.retrieval.embedding_dtype,
            "actual_embedding_dtype": "provider_managed",
            "configured_attention_backend": (self.settings.retrieval.embedding_attention_backend),
            "actual_attention_backend": "provider_managed",
            "measurement_interval_batches": (
                self.settings.retrieval.embedding_measurement_interval_batches
            ),
            "configured_batch_strategy": (self.settings.retrieval.embedding_batch_strategy),
            "configured_max_tokens_per_batch": (
                self.settings.retrieval.embedding_max_tokens_per_batch
            ),
            "last_input_device": None,
            "require_cuda": self.settings.retrieval.require_cuda,
        }

    def available(self) -> bool:
        if not self.settings.retrieval.semantic_enabled:
            return False
        if find_spec("faiss") is None or find_spec("numpy") is None:
            return False
        provider_available = getattr(self.provider, "available", None)
        return bool(provider_available()) if provider_available else True

    def status(self) -> dict[str, Any]:
        with self.connection_factory() as connection:
            row = connection.execute(
                """
                SELECT id, embedding_model, dimensions, chunk_count, activated_at
                FROM vector_index_versions
                WHERE status = 'active' AND backend = 'faiss'
                ORDER BY activated_at DESC LIMIT 1
                """
            ).fetchone()
            build = connection.execute(
                """
                SELECT id, status, completed_count, total_count, chunks_per_second,
                       eta_seconds, batch_size, actual_device, updated_at,
                       sampled_text_count, sampled_token_count,
                       sampled_padded_token_count, sampled_padding_token_count,
                       padding_ratio, gpu_timing_sample_count,
                       average_tokens_per_chunk, tokens_per_second,
                       tokenization_seconds, gpu_inference_seconds, db_write_seconds,
                       measured_batch_seconds,
                       resource_sample_count, gpu_utilization_avg, gpu_utilization_max,
                       vram_used_avg_mib, vram_used_max_mib, vram_total_mib,
                       cpu_utilization_avg, cpu_utilization_max
                FROM embedding_builds ORDER BY updated_at DESC LIMIT 1
                """
            ).fetchone()
            latest_batch = (
                connection.execute(
                    """
                    SELECT batch_ordinal, processed_count, chunk_count, encoded_chunk_count,
                           cache_hit_count, token_count, average_tokens_per_chunk, batch_size,
                           padded_token_count, padding_token_count, padding_ratio,
                           gpu_timing_sampled,
                           batch_seconds, tokenization_seconds, gpu_inference_seconds,
                           db_write_seconds, chunks_per_second, tokens_per_second, created_at
                    FROM embedding_batch_metrics
                    WHERE build_id = ? ORDER BY batch_ordinal DESC LIMIT 1
                    """,
                    (str(build["id"]),),
                ).fetchone()
                if build
                else None
            )
        diagnostics = self.diagnostics()
        build_payload = dict(build) if build else None
        if build_payload is not None:
            build_payload["latest_batch"] = dict(latest_batch) if latest_batch else None
        return {
            "enabled": self.settings.retrieval.semantic_enabled,
            "dependencies": "ready" if self.available() else "missing",
            "model": self.provider.model_name,
            "index": "ready" if row else "not_built",
            "index_version_id": str(row["id"]) if row else None,
            "dimensions": int(row["dimensions"]) if row else None,
            "chunk_count": int(row["chunk_count"]) if row else 0,
            "activated_at": str(row["activated_at"]) if row else None,
            "configured_device": diagnostics["configured_device"],
            "actual_embedding_device": diagnostics["actual_embedding_device"],
            "configured_dtype": diagnostics["configured_dtype"],
            "actual_embedding_dtype": diagnostics["actual_embedding_dtype"],
            "configured_attention_backend": diagnostics["configured_attention_backend"],
            "actual_attention_backend": diagnostics["actual_attention_backend"],
            "configured_batch_strategy": diagnostics["configured_batch_strategy"],
            "configured_max_tokens_per_batch": diagnostics["configured_max_tokens_per_batch"],
            "embedding_cache_dir": diagnostics.get("embedding_cache_dir"),
            "local_model_path": diagnostics.get("local_model_path"),
            "local_model_ready": diagnostics.get("local_model_ready"),
            "model_revision": self.model_revision,
            "build": build_payload,
        }

    @staticmethod
    def _libraries() -> tuple[Any, Any]:
        try:
            import faiss
            import numpy as np
        except ImportError as exc:
            raise FukamiruError(
                "FAISS RAG dependencies are unavailable; install the 'rag' extra"
            ) from exc
        return faiss, np

    def build(
        self,
        progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        if self.settings.retrieval.scalable_index_enabled:
            from fukamiru.knowledge.scalable_index import build_scalable_index

            self.preflight()
            return build_scalable_index(self, progress=progress)
        self.preflight()
        faiss, np = self._libraries()
        corpus_hasher = hashlib.sha256()
        total = 0
        with self.connection_factory() as connection:
            signature_cursor = connection.execute(
                """
                SELECT c.id, c.text
                FROM chunks c
                JOIN document_units d ON d.id = c.document_unit_id
                JOIN source_locations l
                  ON l.source_asset_id = d.source_asset_id
                 AND l.is_primary = 1 AND l.is_available = 1
                ORDER BY c.document_unit_id, c.ordinal
                """
            )
            for row in signature_cursor:
                content_hash = hashlib.sha256(str(row["text"]).encode("utf-8")).hexdigest()
                corpus_hasher.update(str(row["id"]).encode("utf-8"))
                corpus_hasher.update(content_hash.encode("ascii"))
                total += 1
        if total == 0:
            raise FukamiruError("No chunks are available for semantic indexing")

        corpus_signature = corpus_hasher.hexdigest()
        now = utc_now()
        build_id = str(uuid.uuid4())
        version_id: str | None = None
        model_id: str | None = None
        dimensions = 0
        completed_floor = 0
        cache_hit_floor = 0
        encoded_floor = 0
        sampled_text_count = 0
        sampled_token_count = 0
        sampled_padded_token_count = 0
        sampled_padding_token_count = 0
        gpu_timing_sample_count = 0
        total_tokenization_seconds = 0.0
        total_gpu_inference_seconds = 0.0
        total_db_write_seconds = 0.0
        measured_batch_seconds = 0.0
        prior_chunks_per_second: float | None = None
        with self.connection_factory() as connection:
            existing_model = connection.execute(
                "SELECT id, dimensions FROM embedding_models WHERE model_name = ?",
                (self.model_storage_name,),
            ).fetchone()
            if existing_model:
                model_id = str(existing_model["id"])
                dimensions = int(existing_model["dimensions"])
            resumable = connection.execute(
                """
                SELECT * FROM embedding_builds
                WHERE corpus_signature = ? AND model_name = ? AND model_revision = ?
                  AND status IN ('building', 'failed')
                ORDER BY updated_at DESC LIMIT 1
                """,
                (corpus_signature, self.model_identity_name, self.model_revision),
            ).fetchone()
            if resumable:
                build_id = str(resumable["id"])
                version_id = (
                    str(resumable["index_version_id"]) if resumable["index_version_id"] else None
                )
                model_id = (
                    str(resumable["embedding_model_id"])
                    if resumable["embedding_model_id"]
                    else model_id
                )
                completed_floor = int(resumable["completed_count"])
                cache_hit_floor = int(resumable["cache_hit_count"])
                encoded_floor = int(resumable["encoded_count"])
                if model_id:
                    cached_count = connection.execute(
                        """
                        SELECT COUNT(*) AS value FROM embedding_cache
                        WHERE embedding_model_id = ? AND model_revision = ?
                        """,
                        (model_id, self.model_revision),
                    ).fetchone()
                    encoded_floor = max(encoded_floor, int(cached_count["value"]))
                sampled_text_count = int(resumable["sampled_text_count"])
                sampled_token_count = int(resumable["sampled_token_count"])
                sampled_padded_token_count = int(resumable["sampled_padded_token_count"])
                sampled_padding_token_count = int(resumable["sampled_padding_token_count"])
                gpu_timing_sample_count = int(resumable["gpu_timing_sample_count"])
                total_tokenization_seconds = float(resumable["tokenization_seconds"])
                total_gpu_inference_seconds = float(resumable["gpu_inference_seconds"])
                total_db_write_seconds = float(resumable["db_write_seconds"])
                measured_batch_seconds = float(resumable["measured_batch_seconds"])
                prior_chunks_per_second = (
                    float(resumable["chunks_per_second"])
                    if resumable["chunks_per_second"] is not None
                    else None
                )
                connection.execute(
                    """
                    UPDATE embedding_builds
                    SET status = 'building', configured_device = ?, error_json = NULL,
                        updated_at = ? WHERE id = ?
                    """,
                    (self.settings.retrieval.embedding_device, now, build_id),
                )
                if version_id:
                    connection.execute(
                        "UPDATE vector_index_versions SET status = 'building' WHERE id = ?",
                        (version_id,),
                    )
            else:
                connection.execute(
                    """
                    INSERT INTO embedding_builds(
                        id, corpus_signature, embedding_model_id, model_name, model_revision,
                        configured_device, actual_device, status, total_count, batch_size,
                        started_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'building', ?, ?, ?, ?)
                    """,
                    (
                        build_id,
                        corpus_signature,
                        model_id,
                        self.model_identity_name,
                        self.model_revision,
                        self.settings.retrieval.embedding_device,
                        getattr(self.provider, "actual_device", None),
                        total,
                        int(
                            getattr(
                                self.provider,
                                "batch_size",
                                self.settings.retrieval.embedding_batch_size,
                            )
                        ),
                        now,
                        now,
                    ),
                )
            connection.commit()

        chunk_ids: list[str] = []
        index: Any | None = None
        processed_count = 0
        cache_hit_count = cache_hit_floor
        newly_encoded_count = encoded_floor
        with self.connection_factory() as connection:
            metric_row = connection.execute(
                """
                SELECT COALESCE(MAX(batch_ordinal), 0) AS value
                FROM embedding_batch_metrics WHERE build_id = ?
                """,
                (build_id,),
            ).fetchone()
        batch_ordinal = int(metric_row["value"])
        started = time.perf_counter()
        forward_started: float | None = None
        resource_sampler = EmbeddingResourceSampler(self.connection_factory, build_id)
        resource_sampler.__enter__()
        try:
            with (
                self.connection_factory() as read_connection,
                self.connection_factory() as write_connection,
            ):
                # Each fetch is one GPU ownership quantum. Cache progress is committed and
                # the mutex is released at this boundary so a queued local LLM can run.
                # Cache lookup and FAISS assembly are CPU/SQLite work.  Keeping them at
                # the much smaller CUDA micro-batch size causes hundreds of thousands
                # of tiny cache queries on large corpora.  SentenceTransformer.encode
                # still uses the provider's configured CUDA batch size internally.
                gpu_batch_size = max(1, self.settings.retrieval.embedding_batch_size)
                processing_batch_size = (
                    max(gpu_batch_size, 256)
                    if self.settings.retrieval.embedding_batch_strategy == "fixed"
                    else gpu_batch_size
                )
                page_size = max(processing_batch_size, 2048)
                last_document_unit_id: str | None = None
                last_ordinal = -1
                pending_batches: list[list[EmbeddingBatchItem]] = []
                while True:
                    if not pending_batches:
                        batch_cursor = read_connection.execute(
                            """
                            SELECT c.id, c.text, c.token_count,
                                   c.document_unit_id, c.ordinal
                            FROM chunks c
                            JOIN document_units d ON d.id = c.document_unit_id
                            JOIN source_locations l
                              ON l.source_asset_id = d.source_asset_id
                             AND l.is_primary = 1 AND l.is_available = 1
                            WHERE ? IS NULL
                               OR c.document_unit_id > ?
                               OR (c.document_unit_id = ? AND c.ordinal > ?)
                            ORDER BY c.document_unit_id, c.ordinal
                            LIMIT ?
                            """,
                            (
                                last_document_unit_id,
                                last_document_unit_id,
                                last_document_unit_id,
                                last_ordinal,
                                page_size,
                            ),
                        )
                        pending_rows = batch_cursor.fetchall()
                        batch_cursor.close()
                        if not pending_rows:
                            break
                        last_document_unit_id = str(pending_rows[-1]["document_unit_id"])
                        last_ordinal = int(pending_rows[-1]["ordinal"])
                        planned = plan_embedding_batches(
                            (
                                EmbeddingBatchItem(
                                    chunk_id=str(row["id"]),
                                    text=str(row["text"]),
                                    token_count=int(row["token_count"]),
                                    ordinal=offset,
                                )
                                for offset, row in enumerate(pending_rows)
                            ),
                            strategy=self.settings.retrieval.embedding_batch_strategy,
                            batch_size=processing_batch_size,
                            max_tokens_per_batch=(
                                self.settings.retrieval.embedding_max_tokens_per_batch
                            ),
                            bucket_boundaries=self.settings.retrieval.embedding_length_buckets,
                        )
                        pending_batches = [list(batch.items) for batch in planned]
                    batch = pending_batches.pop(0)
                    batch_started = time.perf_counter()
                    batch_ordinal += 1
                    batch_token_count = 0
                    batch_padded_token_count = 0
                    batch_padding_token_count = 0
                    batch_padding_ratio: float | None = None
                    batch_gpu_timing_sampled = 0
                    batch_sampled_text_count = 0
                    batch_tokenization_seconds = 0.0
                    batch_gpu_inference_seconds = 0.0
                    batch_db_write_seconds = 0.0
                    records = [
                        (
                            row.chunk_id,
                            row.text,
                            hashlib.sha256(row.text.encode("utf-8")).hexdigest(),
                        )
                        for row in batch
                    ]
                    unique_text_by_hash: dict[str, str] = {}
                    for _, text_value, content_hash in records:
                        unique_text_by_hash.setdefault(content_hash, text_value)

                    vectors_by_hash: dict[str, Any] = {}
                    cached_hashes: set[str] = set()
                    if model_id:
                        hashes = list(unique_text_by_hash)
                        placeholders = ",".join("?" for _ in hashes)
                        cached_rows = write_connection.execute(
                            f"""
                            SELECT content_sha256, dimensions, vector_blob
                            FROM embedding_cache
                            WHERE embedding_model_id = ? AND model_revision = ?
                              AND content_sha256 IN ({placeholders})
                            """,  # noqa: S608 - placeholders are generated, not user input
                            (model_id, self.model_revision, *hashes),
                        ).fetchall()
                        for cached in cached_rows:
                            cached_dimensions = int(cached["dimensions"])
                            if dimensions and cached_dimensions != dimensions:
                                raise FukamiruError(
                                    "Cached embedding dimensions do not match the model"
                                )
                            dimensions = dimensions or cached_dimensions
                            vector = np.frombuffer(cached["vector_blob"], dtype="float32").copy()
                            if int(vector.size) != dimensions:
                                raise FukamiruError("Cached embedding vector is corrupt")
                            content_hash = str(cached["content_sha256"])
                            vectors_by_hash[content_hash] = vector
                            cached_hashes.add(content_hash)

                    missing_hashes = [
                        content_hash
                        for content_hash in unique_text_by_hash
                        if content_hash not in vectors_by_hash
                    ]
                    if missing_hashes:
                        encoded = self.provider.encode(
                            [unique_text_by_hash[content_hash] for content_hash in missing_hashes]
                        )
                        provider_metrics = dict(getattr(self.provider, "last_metrics", {}) or {})
                        batch_token_count = int(provider_metrics.get("token_count", 0))
                        batch_padded_token_count = int(
                            provider_metrics.get("padded_token_count", batch_token_count)
                        )
                        batch_padding_token_count = int(
                            provider_metrics.get(
                                "padding_token_count",
                                max(batch_padded_token_count - batch_token_count, 0),
                            )
                        )
                        batch_padding_ratio = (
                            batch_padded_token_count / batch_token_count
                            if batch_token_count > 0
                            else None
                        )
                        batch_gpu_timing_sampled = int(
                            provider_metrics.get("gpu_timing_sampled", 1)
                        )
                        batch_sampled_text_count = int(
                            provider_metrics.get("text_count", len(missing_hashes))
                        )
                        batch_tokenization_seconds = float(
                            provider_metrics.get("tokenization_seconds", 0.0)
                        )
                        batch_gpu_inference_seconds = float(
                            provider_metrics.get("gpu_inference_seconds", 0.0)
                        )
                        if len(encoded) != len(missing_hashes) or not encoded or not encoded[0]:
                            raise FukamiruError(
                                "Embedding provider returned an invalid vector batch"
                            )
                        encoded_matrix = np.asarray(encoded, dtype="float32")
                        if encoded_matrix.ndim != 2:
                            raise FukamiruError(
                                "Embedding vectors must form a two-dimensional matrix"
                            )
                        if dimensions and int(encoded_matrix.shape[1]) != dimensions:
                            raise FukamiruError(
                                "Embedding provider changed dimensions within a build"
                            )
                        dimensions = dimensions or int(encoded_matrix.shape[1])
                        faiss.normalize_L2(encoded_matrix)

                        db_write_started = time.perf_counter()
                        if model_id is None:
                            model_id = str(uuid.uuid4())
                            write_connection.execute(
                                """
                                INSERT INTO embedding_models(
                                    id, model_name, dimensions, backend, created_at, model_revision
                                ) VALUES (?, ?, ?, 'sentence-transformers', ?, ?)
                                """,
                                (
                                    model_id,
                                    self.model_storage_name,
                                    dimensions,
                                    utc_now(),
                                    self.model_revision,
                                ),
                            )
                        if version_id is None:
                            version_id = str(uuid.uuid4())
                            final_path = (
                                self.settings.paths.faiss_root / "snapshots" / f"{version_id}.faiss"
                            )
                            write_connection.execute(
                                """
                                INSERT INTO vector_index_versions(
                                    id, backend, embedding_model, dimensions, index_path,
                                    status, chunk_count, created_at
                                ) VALUES (?, 'faiss', ?, ?, ?, 'building', 0, ?)
                                """,
                                (
                                    version_id,
                                    self.model_identity_name,
                                    dimensions,
                                    str(final_path),
                                    utc_now(),
                                ),
                            )
                        write_connection.execute(
                            """
                            UPDATE embedding_builds
                            SET index_version_id = ?, embedding_model_id = ?, actual_device = ?
                            WHERE id = ?
                            """,
                            (
                                version_id,
                                model_id,
                                str(getattr(self.provider, "actual_device", "provider_managed")),
                                build_id,
                            ),
                        )
                        timestamp = utc_now()
                        for offset, content_hash in enumerate(missing_hashes):
                            vector = encoded_matrix[offset]
                            vectors_by_hash[content_hash] = vector
                            write_connection.execute(
                                """
                                INSERT INTO embedding_cache(
                                    content_sha256, embedding_model_id, model_revision,
                                    dimensions, vector_blob, created_at, last_used_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                                ON CONFLICT(content_sha256, embedding_model_id, model_revision)
                                DO UPDATE SET last_used_at = excluded.last_used_at
                                """,
                                (
                                    content_hash,
                                    model_id,
                                    self.model_revision,
                                    dimensions,
                                    vector.astype("float32", copy=False).tobytes(),
                                    timestamp,
                                    timestamp,
                                ),
                            )
                        newly_encoded_count += len(missing_hashes)
                        batch_db_write_seconds += time.perf_counter() - db_write_started

                    if version_id is None and model_id is not None and dimensions:
                        db_write_started = time.perf_counter()
                        version_id = str(uuid.uuid4())
                        final_path = (
                            self.settings.paths.faiss_root / "snapshots" / f"{version_id}.faiss"
                        )
                        write_connection.execute(
                            """
                            INSERT INTO vector_index_versions(
                                id, backend, embedding_model, dimensions, index_path,
                                status, chunk_count, created_at
                            ) VALUES (?, 'faiss', ?, ?, ?, 'building', 0, ?)
                            """,
                            (
                                version_id,
                                self.model_identity_name,
                                dimensions,
                                str(final_path),
                                utc_now(),
                            ),
                        )
                        write_connection.execute(
                            """
                            UPDATE embedding_builds
                            SET index_version_id = ?, embedding_model_id = ?, actual_device = ?
                            WHERE id = ?
                            """,
                            (
                                version_id,
                                model_id,
                                str(getattr(self.provider, "actual_device", "provider_managed")),
                                build_id,
                            ),
                        )
                        batch_db_write_seconds += time.perf_counter() - db_write_started

                    matrix = np.asarray(
                        [vectors_by_hash[content_hash] for _, _, content_hash in records],
                        dtype="float32",
                    )
                    if matrix.ndim != 2 or int(matrix.shape[1]) != dimensions:
                        raise FukamiruError("Embedding vectors have inconsistent dimensions")
                    faiss.normalize_L2(matrix)
                    if index is None:
                        index = faiss.IndexFlatIP(dimensions)
                    index.add(matrix)
                    chunk_ids.extend(chunk_id for chunk_id, _, _ in records)
                    processed_count += len(records)
                    cache_hit_count += sum(
                        1 for _, _, content_hash in records if content_hash in cached_hashes
                    )
                    effective_batch_size = int(
                        getattr(
                            self.provider,
                            "batch_size",
                            self.settings.retrieval.embedding_batch_size,
                        )
                    )
                    observed_batch_size = (
                        min(batch_sampled_text_count, effective_batch_size)
                        if batch_sampled_text_count
                        else min(len(records), effective_batch_size)
                    )
                    persisted_completed = max(completed_floor, processed_count)
                    if processed_count <= completed_floor and prior_chunks_per_second:
                        rate = prior_chunks_per_second
                    else:
                        if forward_started is None:
                            forward_started = batch_started
                        forward_count = max(processed_count - completed_floor, len(records))
                        forward_elapsed = max(time.perf_counter() - forward_started, 0.001)
                        rate = forward_count / forward_elapsed
                    eta = max(total - persisted_completed, 0) / rate if rate > 0 else None
                    actual_device = str(getattr(self.provider, "actual_device", "provider_managed"))
                    progress_write_started = time.perf_counter()
                    write_connection.execute(
                        """
                        UPDATE embedding_builds SET
                            completed_count = ?, cache_hit_count = ?, encoded_count = ?,
                            batch_size = ?, chunks_per_second = ?, eta_seconds = ?,
                            actual_device = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            persisted_completed,
                            cache_hit_count,
                            newly_encoded_count,
                            effective_batch_size,
                            rate,
                            eta,
                            actual_device,
                            utc_now(),
                            build_id,
                        ),
                    )
                    write_connection.commit()
                    batch_db_write_seconds += time.perf_counter() - progress_write_started
                    batch_seconds = max(time.perf_counter() - batch_started, 0.000001)
                    batch_chunks_per_second = len(records) / batch_seconds
                    batch_tokens_per_second = (
                        batch_token_count / batch_seconds if batch_token_count else 0.0
                    )
                    batch_average_tokens = (
                        batch_token_count / batch_sampled_text_count
                        if batch_sampled_text_count
                        else None
                    )
                    sampled_text_count += batch_sampled_text_count
                    sampled_token_count += batch_token_count
                    sampled_padded_token_count += batch_padded_token_count
                    sampled_padding_token_count += batch_padding_token_count
                    gpu_timing_sample_count += batch_gpu_timing_sampled
                    total_tokenization_seconds += batch_tokenization_seconds
                    total_gpu_inference_seconds += batch_gpu_inference_seconds
                    total_db_write_seconds += batch_db_write_seconds
                    if batch_sampled_text_count:
                        measured_batch_seconds += batch_seconds
                    average_tokens = (
                        sampled_token_count / sampled_text_count if sampled_text_count else None
                    )
                    aggregate_padding_ratio = (
                        sampled_padded_token_count / sampled_token_count
                        if sampled_token_count > 0
                        else None
                    )
                    aggregate_tokens_per_second = (
                        sampled_token_count / measured_batch_seconds
                        if measured_batch_seconds > 0
                        else None
                    )
                    metrics_created_at = utc_now()
                    write_connection.execute(
                        """
                        INSERT INTO embedding_batch_metrics(
                            build_id, batch_ordinal, processed_count, chunk_count,
                            encoded_chunk_count, cache_hit_count, token_count,
                            padded_token_count, padding_token_count, padding_ratio,
                            gpu_timing_sampled,
                            average_tokens_per_chunk, batch_size, batch_seconds,
                            tokenization_seconds, gpu_inference_seconds, db_write_seconds,
                            chunks_per_second, tokens_per_second, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            build_id,
                            batch_ordinal,
                            persisted_completed,
                            len(records),
                            len(missing_hashes),
                            sum(
                                1 for _, _, content_hash in records if content_hash in cached_hashes
                            ),
                            batch_token_count,
                            batch_padded_token_count,
                            batch_padding_token_count,
                            batch_padding_ratio,
                            batch_gpu_timing_sampled,
                            batch_average_tokens,
                            observed_batch_size,
                            batch_seconds,
                            batch_tokenization_seconds,
                            batch_gpu_inference_seconds,
                            batch_db_write_seconds,
                            batch_chunks_per_second,
                            batch_tokens_per_second,
                            metrics_created_at,
                        ),
                    )
                    write_connection.execute(
                        """
                        UPDATE embedding_builds SET
                            sampled_text_count = ?, sampled_token_count = ?,
                            sampled_padded_token_count = ?, sampled_padding_token_count = ?,
                            padding_ratio = ?, gpu_timing_sample_count = ?,
                            average_tokens_per_chunk = ?, tokens_per_second = ?,
                            tokenization_seconds = ?, gpu_inference_seconds = ?,
                            db_write_seconds = ?, measured_batch_seconds = ?
                        WHERE id = ?
                        """,
                        (
                            sampled_text_count,
                            sampled_token_count,
                            sampled_padded_token_count,
                            sampled_padding_token_count,
                            aggregate_padding_ratio,
                            gpu_timing_sample_count,
                            average_tokens,
                            aggregate_tokens_per_second,
                            total_tokenization_seconds,
                            total_gpu_inference_seconds,
                            total_db_write_seconds,
                            measured_batch_seconds,
                            build_id,
                        ),
                    )
                    write_connection.commit()
                    resource = write_connection.execute(
                        """
                        SELECT gpu_utilization_avg, gpu_utilization_max,
                               vram_used_avg_mib, vram_used_max_mib, vram_total_mib,
                               cpu_utilization_avg, cpu_utilization_max
                        FROM embedding_builds WHERE id = ?
                        """,
                        (build_id,),
                    ).fetchone()
                    payload = {
                        "embedding_build_id": build_id,
                        "embedded_count": persisted_completed,
                        "total_count": total,
                        "chunks_per_second": rate,
                        "tokens_per_second": aggregate_tokens_per_second,
                        "average_tokens_per_chunk": average_tokens,
                        "actual_token_count": sampled_token_count,
                        "padded_token_count": sampled_padded_token_count,
                        "padding_token_count": sampled_padding_token_count,
                        "padding_ratio": aggregate_padding_ratio,
                        "eta_seconds": eta,
                        "cache_hit_count": cache_hit_count,
                        "newly_encoded_count": newly_encoded_count,
                        "batch_size": effective_batch_size,
                        "batch_strategy": (self.settings.retrieval.embedding_batch_strategy),
                        "max_tokens_per_batch": (
                            self.settings.retrieval.embedding_max_tokens_per_batch
                        ),
                        "tokenization_seconds": total_tokenization_seconds,
                        "gpu_inference_seconds": total_gpu_inference_seconds,
                        "db_write_seconds": total_db_write_seconds,
                        "gpu_utilization_percent": resource["gpu_utilization_avg"],
                        "gpu_utilization_max_percent": resource["gpu_utilization_max"],
                        "vram_used_mib": resource["vram_used_avg_mib"],
                        "vram_used_max_mib": resource["vram_used_max_mib"],
                        "vram_total_mib": resource["vram_total_mib"],
                        "cpu_utilization_percent": resource["cpu_utilization_avg"],
                        "cpu_utilization_max_percent": resource["cpu_utilization_max"],
                        "latest_batch": {
                            "batch_size": observed_batch_size,
                            "token_count": batch_token_count,
                            "actual_token_count": batch_token_count,
                            "padded_token_count": batch_padded_token_count,
                            "padding_token_count": batch_padding_token_count,
                            "padding_ratio": batch_padding_ratio,
                            "gpu_timing_sampled": bool(batch_gpu_timing_sampled),
                            "average_tokens_per_chunk": batch_average_tokens,
                            "chunks_per_second": batch_chunks_per_second,
                            "tokens_per_second": batch_tokens_per_second,
                            "tokenization_seconds": batch_tokenization_seconds,
                            "gpu_inference_seconds": batch_gpu_inference_seconds,
                            "db_write_seconds": batch_db_write_seconds,
                        },
                        "model": self.provider.model_name,
                        "model_revision": self.model_revision,
                        "configured_device": self.settings.retrieval.embedding_device,
                        "actual_embedding_device": actual_device,
                    }
                    if progress:
                        progress(payload)

            if index is None or not chunk_ids or version_id is None or model_id is None:
                raise FukamiruError("No chunks are available for semantic indexing")

            snapshots = self.settings.paths.faiss_root / "snapshots"
            snapshots.mkdir(parents=True, exist_ok=True)
            final_path = snapshots / f"{version_id}.faiss"
            temporary_path = snapshots / f"{version_id}.faiss.tmp"
            build_root = self.settings.paths.runtime_root / "index-build"
            build_root.mkdir(parents=True, exist_ok=True)
            build_path = build_root / f"{version_id}.faiss"
            try:
                faiss.write_index(index, str(build_path))
                shutil.copyfile(build_path, temporary_path)
                published = faiss.read_index(str(temporary_path))
                if int(published.ntotal) != len(chunk_ids) or int(published.d) != dimensions:
                    raise FukamiruError("Published FAISS snapshot failed integrity validation")
                os.replace(temporary_path, final_path)
            except Exception:
                temporary_path.unlink(missing_ok=True)
                raise
            finally:
                build_path.unlink(missing_ok=True)

            completed_at = utc_now()
            try:
                with self.connection_factory() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        """
                        UPDATE vector_index_versions
                        SET status = 'superseded' WHERE status = 'active'
                        """
                    )
                    connection.execute(
                        """
                        UPDATE vector_index_versions
                        SET status = 'active', chunk_count = ?, index_path = ?, activated_at = ?
                        WHERE id = ?
                        """,
                        (len(chunk_ids), str(final_path), completed_at, version_id),
                    )
                    connection.execute(
                        "DELETE FROM chunk_embeddings WHERE embedding_model_id = ?", (model_id,)
                    )
                    connection.executemany(
                        """
                        INSERT INTO chunk_embeddings(
                            chunk_id, embedding_model_id, vector_key, created_at
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            (chunk_id, model_id, str(vector_key), completed_at)
                            for vector_key, chunk_id in enumerate(chunk_ids)
                        ),
                    )
                    connection.executemany(
                        """
                        INSERT INTO chunk_index_records(
                            index_version_id, chunk_id, vector_key
                        ) VALUES (?, ?, ?)
                        """,
                        (
                            (version_id, chunk_id, str(vector_key))
                            for vector_key, chunk_id in enumerate(chunk_ids)
                        ),
                    )
                    elapsed = max(time.perf_counter() - started, 0.001)
                    connection.execute(
                        """
                        UPDATE embedding_builds SET
                            status = 'completed', completed_count = ?, chunks_per_second = ?,
                            eta_seconds = 0, updated_at = ?, completed_at = ?
                        WHERE id = ?
                        """,
                        (total, total / elapsed, completed_at, completed_at, build_id),
                    )
                    connection.commit()
            except Exception:
                final_path.unlink(missing_ok=True)
                raise
        except Exception as exc:
            failed_at = utc_now()
            with self.connection_factory() as connection:
                connection.execute(
                    """
                    UPDATE embedding_builds
                    SET status = 'failed', error_json = ?, updated_at = ? WHERE id = ?
                    """,
                    (
                        json.dumps(
                            {"type": exc.__class__.__name__, "message": str(exc)},
                            ensure_ascii=False,
                        ),
                        failed_at,
                        build_id,
                    ),
                )
                if version_id:
                    connection.execute(
                        "UPDATE vector_index_versions SET status = 'failed' WHERE id = ?",
                        (version_id,),
                    )
                connection.commit()
            raise
        finally:
            resource_sampler.__exit__()

        return {
            "embedding_build_id": build_id,
            "index_version_id": version_id,
            "model": self.provider.model_name,
            "model_revision": self.model_revision,
            "dimensions": dimensions,
            "chunk_count": len(chunk_ids),
            "index_path": str(final_path),
            "configured_device": self.settings.retrieval.embedding_device,
            "actual_embedding_device": str(
                getattr(self.provider, "actual_device", "provider_managed")
            ),
            "cache_hit_count": cache_hit_count,
            "newly_encoded_count": newly_encoded_count,
            "batch_size": int(
                getattr(
                    self.provider,
                    "batch_size",
                    self.settings.retrieval.embedding_batch_size,
                )
            ),
        }

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        index_version_id: str | None = None,
    ) -> list[dict[str, Any]]:
        query = query.strip()
        if not query:
            return []
        faiss, np = self._libraries()
        with self.connection_factory() as connection:
            if index_version_id:
                version = connection.execute(
                    """
                    SELECT * FROM vector_index_versions
                    WHERE id = ? AND backend = 'faiss'
                      AND status IN ('active', 'superseded')
                    """,
                    (index_version_id,),
                ).fetchone()
            else:
                version = connection.execute(
                    """
                    SELECT * FROM vector_index_versions
                    WHERE status = 'active' AND backend = 'faiss'
                    ORDER BY activated_at DESC LIMIT 1
                    """
                ).fetchone()
            if version is None:
                return []
            resolved_version_id = str(version["id"])
            try:
                scalable = connection.execute(
                    "SELECT 1 FROM retrieval_index_groups WHERE index_version_id=? LIMIT 1",
                    (resolved_version_id,),
                ).fetchone()
            except sqlite3.OperationalError:
                scalable = None
        self.preflight()
        if scalable:
            from fukamiru.knowledge.scalable_index import search_scalable_index

            return search_scalable_index(self, version, query, limit)
        index_path = Path(version["index_path"])
        if not index_path.is_file():
            raise FukamiruError(f"Active FAISS snapshot is missing: {index_path}")
        vector = np.asarray(self.provider.encode([query]), dtype="float32")
        faiss.normalize_L2(vector)
        index = faiss.read_index(str(index_path))
        scores, keys = index.search(vector, min(limit, int(index.ntotal)))
        ranked_keys = [
            (int(key), float(score))
            for key, score in zip(keys[0], scores[0], strict=True)
            if int(key) >= 0
        ]
        if not ranked_keys:
            return []
        selected_keys = list(dict.fromkeys(key for key, _ in ranked_keys))
        placeholders = ",".join("?" for _ in selected_keys)
        with self.connection_factory() as connection:
            mapping = {
                int(row["vector_key"]): str(row["chunk_id"])
                for row in connection.execute(
                    f"""
                    SELECT vector_key, chunk_id FROM chunk_index_records
                    WHERE index_version_id = ?
                      AND vector_key IN ({placeholders})
                    """,  # noqa: S608 - placeholders are generated, values are bound
                    (resolved_version_id, *(str(key) for key in selected_keys)),
                )
            }
        ranked = [
            (mapping[key], score)
            for key, score in ranked_keys
            if key in mapping
        ]
        if not ranked:
            return []
        chunk_ids = [chunk_id for chunk_id, _ in ranked]
        placeholders = ",".join("?" for _ in chunk_ids)
        with self.connection_factory() as connection:
            rows = connection.execute(
                f"""
                SELECT c.id AS chunk_id, c.document_unit_id, d.source_asset_id,
                       c.text, d.title, l.path
                FROM chunks c
                JOIN document_units d ON d.id = c.document_unit_id
                JOIN source_locations l
                  ON l.source_asset_id = d.source_asset_id
                 AND l.is_primary = 1 AND l.is_available = 1
                WHERE c.id IN ({placeholders})
                """,  # noqa: S608 - placeholders are generated, not user input
                chunk_ids,
            ).fetchall()
        by_id = {str(row["chunk_id"]): row for row in rows}
        return [
            {
                "chunk_id": chunk_id,
                "document_unit_id": by_id[chunk_id]["document_unit_id"],
                "source_asset_id": by_id[chunk_id]["source_asset_id"],
                "title": by_id[chunk_id]["title"],
                "path": by_id[chunk_id]["path"],
                "text": by_id[chunk_id]["text"],
                "score": score,
                "retrieval": "semantic",
            }
            for chunk_id, score in ranked
            if chunk_id in by_id
        ]
