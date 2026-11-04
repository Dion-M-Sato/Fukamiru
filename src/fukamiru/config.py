"""Layered configuration without hard-coded machine paths."""

from __future__ import annotations

import copy
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fukamiru.errors import ConfigurationError

CODEX_INFERENCE_MODEL = "gpt-5.6-sol"
CODEX_REASONING_EFFORT = "medium"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        return tomllib.load(handle)


def default_config_path() -> Path:
    explicit = os.environ.get("FUKAMIRU_CONFIG")
    if explicit:
        return Path(explicit).expanduser().resolve()
    return Path(__file__).resolve().parents[2] / "config" / "local.toml"


def _resolve_configured_path(value: str, repository_root: Path) -> Path:
    path = Path(value).expanduser()
    # Preserve absolute and drive-rooted paths; relative paths belong to this checkout.
    return path if path.anchor else (repository_root / path).resolve()


@dataclass(frozen=True)
class ServerSettings:
    host: str = "127.0.0.1"
    port: int = 8765
    open_browser: bool = True


@dataclass(frozen=True)
class PathSettings:
    state_db: Path
    knowledge_db: Path
    runtime_root: Path
    corpus_root: Path
    ocr_root: Path
    knowledge_artifacts_root: Path
    faiss_root: Path

    @classmethod
    def from_mapping(cls, values: dict[str, Any]) -> PathSettings:
        missing = [name for name in cls.__annotations__ if not values.get(name)]
        if missing:
            joined = ", ".join(missing)
            raise ConfigurationError(f"Missing required path settings: {joined}")
        return cls(**{name: Path(values[name]).expanduser() for name in cls.__annotations__})


@dataclass(frozen=True)
class ModelSettings:
    path: Path | None
    server_command: str
    context_size: int = 4096
    gpu_layers: int = 99
    host: str = "127.0.0.1"
    port: int = 18081
    startup_timeout_seconds: float = 600.0
    request_timeout_seconds: float = 300.0
    device: str | None = None
    fit: bool = True
    warmup: bool = True
    parallel_slots: int = 1
    http_threads: int = -1
    webui: bool = True

    @classmethod
    def from_mapping(cls, values: dict[str, Any]) -> ModelSettings:
        raw_path = values.get("path", "")
        return cls(
            path=Path(raw_path).expanduser() if raw_path else None,
            server_command=str(values.get("server_command", "llama-server")),
            context_size=int(values.get("context_size", 4096)),
            gpu_layers=int(values.get("gpu_layers", 99)),
            host=str(values.get("host", "127.0.0.1")),
            port=int(values.get("port", 18081)),
            startup_timeout_seconds=float(values.get("startup_timeout_seconds", 600.0)),
            request_timeout_seconds=float(values.get("request_timeout_seconds", 300.0)),
            device=str(values["device"]).strip() if values.get("device") else None,
            fit=bool(values.get("fit", True)),
            warmup=bool(values.get("warmup", True)),
            parallel_slots=int(values.get("parallel_slots", 1)),
            http_threads=int(values.get("http_threads", -1)),
            webui=bool(values.get("webui", True)),
        )


@dataclass(frozen=True)
class EventSettings:
    retention_days: int = 7
    max_events_per_run: int = 5000


@dataclass(frozen=True)
class RetrievalSettings:
    backend: str = "faiss"
    embedding_model: str = "BAAI/bge-m3"
    embedding_model_revision: str = "5617a9f61b028005a4858fdac845db406aefb181"
    embedding_cache_dir: Path = Path("runtime/cache/huggingface")
    embedding_device: str = "cuda"
    require_cuda: bool = True
    embedding_batch_size: int = 8
    embedding_batch_min_size: int = 1
    embedding_batch_strategy: str = "fixed"
    embedding_max_tokens_per_batch: int = 8192
    embedding_length_buckets: tuple[int, ...] = (256, 384, 512, 768, 1024)
    embedding_dtype: str = "float32"
    embedding_attention_backend: str = "auto"
    embedding_measurement_interval_batches: int = 16
    semantic_enabled: bool = True
    scalable_index_enabled: bool = False
    tokenizer_mode: str = "embedding"
    hybrid_rrf_k: int = 60
    target_tokens: int = 512
    soft_min_tokens: int = 384
    soft_max_tokens: int = 768
    hard_max_tokens: int = 1024
    overlap_source_spans: int = 1


@dataclass(frozen=True)
class WorkerSettings:
    enabled: bool = True
    poll_interval_seconds: float = 0.25
    lease_seconds: float = 90.0
    heartbeat_seconds: float = 15.0
    ingestion_cpu_max_concurrency: int = 3
    codex_max_concurrency: int = 5
    codex_checkpoint_seed_prefix_characters: int = 8192
    codex_review_concurrency: int = 3
    codex_reference_inference_concurrency: int = 2
    codex_enumeration_concurrency: int = 5
    codex_paragraph_concurrency: int = 5
    codex_rewrite_concurrency: int = 5
    codex_predicate_concurrency: int = 5
    codex_coherence_concurrency: int = 3
    codex_discourse_concurrency: int = 5
    codex_japanese_analysis_concurrency: int = 5
    evidence_search_concurrency: int = 5
    reference_lookup_concurrency: int = 5
    codex_translation_yield_batches: int = 20
    local_llm_yield_chunks: int = 20
    codex_rate_limit_retry_delay_seconds: float = 30.0
    codex_rate_limit_cooldown_seconds: float = 300.0


@dataclass(frozen=True)
class TranslationSettings:
    provider: str = "codex"
    codex_command: str = "codex"
    manuscript_codex_model: str = CODEX_INFERENCE_MODEL
    timeout_seconds: int = 900
    manuscript_structure_timeout_seconds: int = 1800
    manuscript_structure_timeout_grace_seconds: int = 30
    batch_characters: int = 12000
    post_edit_lines_per_chunk: int = 5
    post_edit_max_characters: int = 400
    post_edit_context_characters: int = 600
    post_edit_max_output_tokens: int = 512
    post_edit_max_edits_per_chunk: int = 2
    post_edit_max_repair_rounds: int = 3
    manuscript_polish_max_output_tokens: int = 1536
    manuscript_polish_max_edits_per_chunk: int = 6
    manuscript_candidate_count: int = 6
    manuscript_candidate_top_k: int = 3
    manuscript_candidate_generation_attempts: int = 2
    manuscript_candidate_human_training_target: int = 100
    manuscript_candidate_auto_apply_margin: float = 0.12
    manuscript_candidate_max_targets: int = 400
    manuscript_candidate_beam_width: int = 24
    academic_research_enabled: bool = True
    academic_research_online: bool = True
    academic_research_local_results: int = 3
    academic_research_provider_results: int = 3
    academic_research_sources: int = 3
    academic_research_max_queries: int = 8
    academic_research_timeout_seconds: int = 45
    academic_research_max_pdf_bytes: int = 50 * 1024 * 1024
    academic_research_reading_rounds: int = 2
    academic_research_reading_input_tokens: int = 120_000
    academic_research_reading_seconds: int = 180
    academic_research_ocr_pages: int = 3
    academic_research_ocr_seconds: int = 60
    research_web_enabled: bool = True
    rustxng_url: str = "http://127.0.0.1:29483"
    research_pdf_ocr_enabled: bool = True


@dataclass(frozen=True)
class OCRSettings:
    provider: str = "language_router"
    output_format: str = "txt"
    mistral_model: str = "mistral-ocr-latest"
    mistral_max_attempts: int = 4
    ndlocr_command: str = "ndlocr-lite"
    ndlocr_timeout_seconds: int = 3600
    ndlocr_pdf_render_dpi: int = 150
    vision_language_detection: bool = False


@dataclass(frozen=True)
class Settings:
    server: ServerSettings
    paths: PathSettings
    llm_jp: ModelSettings
    llm_jp_base: ModelSettings
    gaperon: ModelSettings
    events: EventSettings
    retrieval: RetrievalSettings
    worker: WorkerSettings
    translation: TranslationSettings
    ocr: OCRSettings
    config_path: Path


def load_settings(
    config_path: Path | None = None,
    defaults_path: Path | None = None,
) -> Settings:
    repository_root = Path(__file__).resolve().parents[2]
    defaults_path = defaults_path or repository_root / "config" / "defaults.toml"
    config_path = config_path or default_config_path()
    merged = _deep_merge(_read_toml(defaults_path), _read_toml(config_path))

    server = merged.get("server", {})
    models = merged.get("models", {})
    events = merged.get("events", {})
    retrieval = merged.get("retrieval", {})
    worker = merged.get("worker", {})
    translation = merged.get("translation", {})
    ocr = merged.get("ocr", {})
    path_values = {
        key: _resolve_configured_path(str(value), repository_root) if value else value
        for key, value in merged.get("paths", {}).items()
    }
    paths = PathSettings.from_mapping(path_values)
    for model in models.values():
        if model.get("path"):
            model["path"] = _resolve_configured_path(str(model["path"]), repository_root)
    raw_embedding_cache_dir = str(retrieval.get("embedding_cache_dir", "")).strip()
    embedding_cache_dir = (
        _resolve_configured_path(raw_embedding_cache_dir, repository_root)
        if raw_embedding_cache_dir
        else paths.runtime_root / "cache" / "huggingface"
    )
    return Settings(
        server=ServerSettings(
            host=str(server.get("host", "127.0.0.1")),
            port=int(server.get("port", 8765)),
            open_browser=bool(server.get("open_browser", True)),
        ),
        paths=paths,
        llm_jp=ModelSettings.from_mapping(models.get("llm_jp", {})),
        llm_jp_base=ModelSettings.from_mapping(models.get("llm_jp_base", {})),
        gaperon=ModelSettings.from_mapping(models.get("gaperon", {})),
        events=EventSettings(
            retention_days=int(events.get("retention_days", 7)),
            max_events_per_run=int(events.get("max_events_per_run", 5000)),
        ),
        retrieval=RetrievalSettings(
            backend=str(retrieval.get("backend", "faiss")),
            embedding_model=str(retrieval.get("embedding_model", "BAAI/bge-m3")),
            embedding_model_revision=str(
                retrieval.get(
                    "embedding_model_revision",
                    "5617a9f61b028005a4858fdac845db406aefb181",
                )
            ),
            embedding_cache_dir=embedding_cache_dir,
            embedding_device=str(retrieval.get("embedding_device", "cuda")),
            require_cuda=bool(retrieval.get("require_cuda", True)),
            embedding_batch_size=int(retrieval.get("embedding_batch_size", 8)),
            embedding_batch_min_size=int(retrieval.get("embedding_batch_min_size", 1)),
            embedding_batch_strategy=str(retrieval.get("embedding_batch_strategy", "fixed")),
            embedding_max_tokens_per_batch=int(
                retrieval.get("embedding_max_tokens_per_batch", 8192)
            ),
            embedding_length_buckets=tuple(
                int(value)
                for value in retrieval.get("embedding_length_buckets", [256, 384, 512, 768, 1024])
            ),
            embedding_dtype=str(retrieval.get("embedding_dtype", "float32")),
            embedding_attention_backend=str(retrieval.get("embedding_attention_backend", "auto")),
            embedding_measurement_interval_batches=int(
                retrieval.get("embedding_measurement_interval_batches", 16)
            ),
            semantic_enabled=bool(retrieval.get("semantic_enabled", True)),
            scalable_index_enabled=bool(retrieval.get("scalable_index_enabled", False)),
            tokenizer_mode=str(retrieval.get("tokenizer_mode", "embedding")),
            hybrid_rrf_k=int(retrieval.get("hybrid_rrf_k", 60)),
            target_tokens=int(retrieval.get("target_tokens", 512)),
            soft_min_tokens=int(retrieval.get("soft_min_tokens", 384)),
            soft_max_tokens=int(retrieval.get("soft_max_tokens", 768)),
            hard_max_tokens=int(retrieval.get("hard_max_tokens", 1024)),
            overlap_source_spans=int(retrieval.get("overlap_source_spans", 1)),
        ),
        worker=WorkerSettings(
            enabled=bool(worker.get("enabled", True)),
            poll_interval_seconds=float(worker.get("poll_interval_seconds", 0.25)),
            lease_seconds=float(worker.get("lease_seconds", 90.0)),
            heartbeat_seconds=float(worker.get("heartbeat_seconds", 15.0)),
            ingestion_cpu_max_concurrency=int(
                worker.get("ingestion_cpu_max_concurrency", 3)
            ),
            codex_max_concurrency=int(worker.get("codex_max_concurrency", 5)),
            codex_checkpoint_seed_prefix_characters=int(
                worker.get("codex_checkpoint_seed_prefix_characters", 8192)
            ),
            codex_review_concurrency=int(worker.get("codex_review_concurrency", 3)),
            codex_reference_inference_concurrency=int(
                worker.get("codex_reference_inference_concurrency", 2)
            ),
            codex_enumeration_concurrency=int(
                worker.get("codex_enumeration_concurrency", 5)
            ),
            codex_paragraph_concurrency=int(
                worker.get("codex_paragraph_concurrency", 5)
            ),
            codex_rewrite_concurrency=int(worker.get("codex_rewrite_concurrency", 5)),
            codex_predicate_concurrency=int(
                worker.get("codex_predicate_concurrency", 5)
            ),
            codex_coherence_concurrency=int(
                worker.get("codex_coherence_concurrency", 3)
            ),
            codex_discourse_concurrency=int(worker.get("codex_discourse_concurrency", 5)),
            codex_japanese_analysis_concurrency=int(
                worker.get("codex_japanese_analysis_concurrency", 5)
            ),
            evidence_search_concurrency=int(
                worker.get("evidence_search_concurrency", 5)
            ),
            reference_lookup_concurrency=int(
                worker.get("reference_lookup_concurrency", 5)
            ),
            codex_translation_yield_batches=int(
                worker.get("codex_translation_yield_batches", 20)
            ),
            local_llm_yield_chunks=int(worker.get("local_llm_yield_chunks", 20)),
            codex_rate_limit_retry_delay_seconds=float(
                worker.get("codex_rate_limit_retry_delay_seconds", 30.0)
            ),
            codex_rate_limit_cooldown_seconds=float(
                worker.get("codex_rate_limit_cooldown_seconds", 300.0)
            ),
        ),
        translation=TranslationSettings(
            provider=str(translation.get("provider", "codex")),
            codex_command=str(translation.get("codex_command", "codex")),
            manuscript_codex_model=str(
                translation.get("manuscript_codex_model", CODEX_INFERENCE_MODEL)
            ),
            timeout_seconds=int(translation.get("timeout_seconds", 900)),
            manuscript_structure_timeout_seconds=int(
                translation.get("manuscript_structure_timeout_seconds", 1800)
            ),
            manuscript_structure_timeout_grace_seconds=int(
                translation.get("manuscript_structure_timeout_grace_seconds", 30)
            ),
            batch_characters=int(translation.get("batch_characters", 12000)),
            post_edit_lines_per_chunk=int(
                translation.get("post_edit_lines_per_chunk", 5)
            ),
            post_edit_max_characters=int(
                translation.get("post_edit_max_characters", 400)
            ),
            post_edit_context_characters=int(
                translation.get("post_edit_context_characters", 600)
            ),
            post_edit_max_output_tokens=int(
                translation.get("post_edit_max_output_tokens", 512)
            ),
            post_edit_max_edits_per_chunk=int(
                translation.get("post_edit_max_edits_per_chunk", 2)
            ),
            post_edit_max_repair_rounds=int(
                translation.get("post_edit_max_repair_rounds", 3)
            ),
            manuscript_polish_max_output_tokens=int(
                translation.get("manuscript_polish_max_output_tokens", 1536)
            ),
            manuscript_polish_max_edits_per_chunk=int(
                translation.get("manuscript_polish_max_edits_per_chunk", 6)
            ),
            manuscript_candidate_count=int(
                translation.get("manuscript_candidate_count", 6)
            ),
            manuscript_candidate_top_k=int(
                translation.get("manuscript_candidate_top_k", 3)
            ),
            manuscript_candidate_generation_attempts=int(
                translation.get("manuscript_candidate_generation_attempts", 2)
            ),
            manuscript_candidate_human_training_target=int(
                translation.get("manuscript_candidate_human_training_target", 100)
            ),
            manuscript_candidate_auto_apply_margin=float(
                translation.get("manuscript_candidate_auto_apply_margin", 0.12)
            ),
            manuscript_candidate_max_targets=int(
                translation.get("manuscript_candidate_max_targets", 400)
            ),
            manuscript_candidate_beam_width=int(
                translation.get("manuscript_candidate_beam_width", 24)
            ),
            academic_research_enabled=bool(
                translation.get("academic_research_enabled", True)
            ),
            academic_research_online=bool(
                translation.get("academic_research_online", True)
            ),
            academic_research_local_results=int(
                translation.get("academic_research_local_results", 3)
            ),
            academic_research_provider_results=int(
                translation.get("academic_research_provider_results", 3)
            ),
            academic_research_sources=int(
                translation.get("academic_research_sources", 3)
            ),
            academic_research_max_queries=int(
                translation.get("academic_research_max_queries", 8)
            ),
            academic_research_timeout_seconds=int(
                translation.get("academic_research_timeout_seconds", 45)
            ),
            academic_research_max_pdf_bytes=int(
                translation.get("academic_research_max_pdf_bytes", 50 * 1024 * 1024)
            ),
            academic_research_reading_rounds=int(
                translation.get("academic_research_reading_rounds", 2)
            ),
            academic_research_reading_input_tokens=int(
                translation.get("academic_research_reading_input_tokens", 120_000)
            ),
            academic_research_reading_seconds=int(
                translation.get("academic_research_reading_seconds", 180)
            ),
            academic_research_ocr_pages=int(translation.get("academic_research_ocr_pages", 3)),
            academic_research_ocr_seconds=int(translation.get("academic_research_ocr_seconds", 60)),
            research_web_enabled=bool(translation.get("research_web_enabled", True)),
            rustxng_url=str(translation.get("rustxng_url", "http://127.0.0.1:29483")),
            research_pdf_ocr_enabled=bool(translation.get("research_pdf_ocr_enabled", True)),
        ),
        ocr=OCRSettings(
            provider=str(ocr.get("provider", "language_router")),
            output_format=str(ocr.get("output_format", "txt")),
            mistral_model=str(ocr.get("mistral_model", "mistral-ocr-latest")),
            mistral_max_attempts=int(ocr.get("mistral_max_attempts", 4)),
            ndlocr_command=str(ocr.get("ndlocr_command", "ndlocr-lite")),
            ndlocr_timeout_seconds=int(ocr.get("ndlocr_timeout_seconds", 3600)),
            ndlocr_pdf_render_dpi=int(ocr.get("ndlocr_pdf_render_dpi", 150)),
            vision_language_detection=bool(ocr.get("vision_language_detection", False)),
        ),
        config_path=config_path,
    )


def validate_settings(settings: Settings, *, require_models: bool = False) -> list[str]:
    problems: list[str] = []
    if settings.server.host not in {"127.0.0.1", "localhost", "::1"}:
        problems.append("server.host must remain loopback-only in the local-first profile")
    if not 1 <= settings.server.port <= 65535:
        problems.append("server.port must be between 1 and 65535")
    if settings.retrieval.backend not in {"faiss", "qdrant"}:
        problems.append("retrieval.backend must be 'faiss' or 'qdrant'")
    if not (
        settings.retrieval.soft_min_tokens
        <= settings.retrieval.target_tokens
        <= settings.retrieval.hard_max_tokens
    ):
        problems.append("retrieval token limits are inconsistent")
    if settings.retrieval.embedding_batch_size < 1:
        problems.append("retrieval.embedding_batch_size must be positive")
    if not (
        1 <= settings.retrieval.embedding_batch_min_size <= settings.retrieval.embedding_batch_size
    ):
        problems.append(
            "retrieval.embedding_batch_min_size must be between 1 and embedding_batch_size"
        )
    if settings.retrieval.embedding_batch_strategy not in {
        "fixed",
        "length_bucket",
        "token_budget",
    }:
        problems.append(
            "retrieval.embedding_batch_strategy must be 'fixed', 'length_bucket', or 'token_budget'"
        )
    if settings.retrieval.embedding_max_tokens_per_batch < 1:
        problems.append("retrieval.embedding_max_tokens_per_batch must be positive")
    boundaries = settings.retrieval.embedding_length_buckets
    if not boundaries or any(value < 1 for value in boundaries):
        problems.append("retrieval.embedding_length_buckets must contain positive values")
    elif tuple(sorted(set(boundaries))) != boundaries:
        problems.append("retrieval.embedding_length_buckets must be unique and strictly increasing")
    if settings.retrieval.embedding_dtype not in {"float32", "float16", "bfloat16"}:
        problems.append("retrieval.embedding_dtype must be 'float32', 'float16', or 'bfloat16'")
    if settings.retrieval.embedding_attention_backend not in {
        "auto",
        "eager",
        "sdpa",
        "flash_attention_2",
    }:
        problems.append(
            "retrieval.embedding_attention_backend must be 'auto', 'eager', 'sdpa', "
            "or 'flash_attention_2'"
        )
    if settings.retrieval.embedding_measurement_interval_batches < 1:
        problems.append("retrieval.embedding_measurement_interval_batches must be positive")
    if settings.retrieval.embedding_device not in {"auto", "cpu", "cuda"}:
        problems.append("retrieval.embedding_device must be 'auto', 'cpu', or 'cuda'")
    if settings.retrieval.require_cuda and settings.retrieval.embedding_device != "cuda":
        problems.append("retrieval.require_cuda requires embedding_device = 'cuda'")
    if not settings.retrieval.embedding_model_revision.strip():
        problems.append("retrieval.embedding_model_revision must not be empty")
    if not str(settings.retrieval.embedding_cache_dir).strip():
        problems.append("retrieval.embedding_cache_dir must not be empty")
    if settings.retrieval.tokenizer_mode not in {"embedding", "unicode"}:
        problems.append("retrieval.tokenizer_mode must be 'embedding' or 'unicode'")
    if settings.retrieval.hybrid_rrf_k < 1:
        problems.append("retrieval.hybrid_rrf_k must be positive")
    if settings.worker.poll_interval_seconds <= 0:
        problems.append("worker.poll_interval_seconds must be positive")
    if settings.worker.lease_seconds <= 0:
        problems.append("worker.lease_seconds must be positive")
    if not 0 < settings.worker.heartbeat_seconds < settings.worker.lease_seconds:
        problems.append("worker.heartbeat_seconds must be positive and shorter than lease_seconds")
    if not 1 <= settings.worker.ingestion_cpu_max_concurrency <= 5:
        problems.append("worker.ingestion_cpu_max_concurrency must be between 1 and 5")
    if not 1 <= settings.worker.codex_max_concurrency <= 5:
        problems.append("worker.codex_max_concurrency must be between 1 and 5")
    if settings.worker.codex_checkpoint_seed_prefix_characters < 1024:
        problems.append(
            "worker.codex_checkpoint_seed_prefix_characters must be at least 1024"
        )
    bounded_concurrency = {
        "codex_review_concurrency": settings.worker.codex_review_concurrency,
        "codex_reference_inference_concurrency": (
            settings.worker.codex_reference_inference_concurrency
        ),
        "codex_enumeration_concurrency": settings.worker.codex_enumeration_concurrency,
        "codex_paragraph_concurrency": settings.worker.codex_paragraph_concurrency,
        "codex_rewrite_concurrency": settings.worker.codex_rewrite_concurrency,
        "codex_predicate_concurrency": settings.worker.codex_predicate_concurrency,
        "codex_coherence_concurrency": settings.worker.codex_coherence_concurrency,
        "codex_discourse_concurrency": settings.worker.codex_discourse_concurrency,
        "codex_japanese_analysis_concurrency": (
            settings.worker.codex_japanese_analysis_concurrency
        ),
        "evidence_search_concurrency": settings.worker.evidence_search_concurrency,
        "reference_lookup_concurrency": settings.worker.reference_lookup_concurrency,
    }
    for field_name, concurrency in bounded_concurrency.items():
        if not 1 <= concurrency <= 5:
            problems.append(f"worker.{field_name} must be between 1 and 5")
    if settings.worker.codex_translation_yield_batches < 1:
        problems.append("worker.codex_translation_yield_batches must be positive")
    if settings.worker.local_llm_yield_chunks < 1:
        problems.append("worker.local_llm_yield_chunks must be positive")
    if settings.worker.codex_rate_limit_retry_delay_seconds < 0:
        problems.append("worker.codex_rate_limit_retry_delay_seconds must not be negative")
    if settings.worker.codex_rate_limit_cooldown_seconds <= 0:
        problems.append("worker.codex_rate_limit_cooldown_seconds must be positive")
    if settings.translation.provider not in {"codex"}:
        problems.append("translation.provider must be 'codex'")
    if settings.translation.manuscript_codex_model != CODEX_INFERENCE_MODEL:
        problems.append(
            f"translation.manuscript_codex_model must be '{CODEX_INFERENCE_MODEL}'"
        )
    if settings.translation.batch_characters < 1000:
        problems.append("translation.batch_characters must be at least 1000")
    if settings.translation.post_edit_lines_per_chunk < 1:
        problems.append("translation.post_edit_lines_per_chunk must be positive")
    if settings.translation.post_edit_max_characters < 200:
        problems.append("translation.post_edit_max_characters must be at least 200")
    if settings.translation.post_edit_context_characters < 0:
        problems.append("translation.post_edit_context_characters must not be negative")
    if settings.translation.post_edit_max_output_tokens < 128:
        problems.append("translation.post_edit_max_output_tokens must be at least 128")
    if settings.translation.post_edit_max_edits_per_chunk < 1:
        problems.append("translation.post_edit_max_edits_per_chunk must be positive")
    if not 0 <= settings.translation.post_edit_max_repair_rounds <= 10:
        problems.append(
            "translation.post_edit_max_repair_rounds must be between 0 and 10"
        )
    if settings.translation.manuscript_polish_max_output_tokens < 512:
        problems.append(
            "translation.manuscript_polish_max_output_tokens must be at least 512"
        )
    if settings.translation.manuscript_polish_max_edits_per_chunk < 1:
        problems.append(
            "translation.manuscript_polish_max_edits_per_chunk must be positive"
        )
    if not 5 <= settings.translation.manuscript_candidate_count <= 8:
        problems.append(
            "translation.manuscript_candidate_count must be between 5 and 8"
        )
    if not (
        1
        <= settings.translation.manuscript_candidate_top_k
        <= settings.translation.manuscript_candidate_count
    ):
        problems.append(
            "translation.manuscript_candidate_top_k must be between 1 and candidate_count"
        )
    if not 1 <= settings.translation.manuscript_candidate_generation_attempts <= 3:
        problems.append(
            "translation.manuscript_candidate_generation_attempts must be between 1 and 3"
        )
    if not 100 <= settings.translation.manuscript_candidate_human_training_target <= 200:
        problems.append(
            "translation.manuscript_candidate_human_training_target must be between 100 and 200"
        )
    if not 0 <= settings.translation.manuscript_candidate_auto_apply_margin <= 1:
        problems.append(
            "translation.manuscript_candidate_auto_apply_margin must be between 0 and 1"
        )
    if settings.translation.manuscript_candidate_max_targets < 1:
        problems.append(
            "translation.manuscript_candidate_max_targets must be positive"
        )
    if settings.translation.manuscript_candidate_beam_width < 1:
        problems.append(
            "translation.manuscript_candidate_beam_width must be positive"
        )
    if settings.translation.manuscript_structure_timeout_seconds < 1:
        problems.append(
            "translation.manuscript_structure_timeout_seconds must be positive"
        )
    if not 0 <= settings.translation.manuscript_structure_timeout_grace_seconds <= 300:
        problems.append(
            "translation.manuscript_structure_timeout_grace_seconds must be between 0 and 300"
        )
    if settings.translation.academic_research_local_results < 1:
        problems.append("translation.academic_research_local_results must be positive")
    if settings.translation.academic_research_provider_results < 1:
        problems.append("translation.academic_research_provider_results must be positive")
    if settings.translation.academic_research_sources < 1:
        problems.append("translation.academic_research_sources must be positive")
    if settings.translation.academic_research_max_queries < 1:
        problems.append("translation.academic_research_max_queries must be positive")
    if settings.translation.academic_research_timeout_seconds < 1:
        problems.append("translation.academic_research_timeout_seconds must be positive")
    for name in ("academic_research_reading_rounds", "academic_research_reading_input_tokens",
                 "academic_research_reading_seconds", "academic_research_ocr_pages",
                 "academic_research_ocr_seconds"):
        if getattr(settings.translation, name) < 1:
            problems.append(f"translation.{name} must be positive")
    if settings.translation.research_web_enabled:
        from fukamiru.literature.web import validate_endpoint

        try:
            validate_endpoint(settings.translation.rustxng_url)
        except ValueError as exc:
            problems.append(f"translation.rustxng_url: {exc}")
    if settings.translation.academic_research_max_pdf_bytes < 1024 * 1024:
        problems.append(
            "translation.academic_research_max_pdf_bytes must be at least 1 MiB"
        )
    if settings.ocr.provider != "language_router":
        problems.append("ocr.provider must be 'language_router'")
    if settings.ocr.output_format not in {"txt", "md"}:
        problems.append("ocr.output_format must be 'txt' or 'md'")
    if settings.ocr.mistral_max_attempts < 1:
        problems.append("ocr.mistral_max_attempts must be positive")
    if settings.ocr.ndlocr_timeout_seconds < 1:
        problems.append("ocr.ndlocr_timeout_seconds must be positive")
    if settings.ocr.ndlocr_pdf_render_dpi < 72:
        problems.append("ocr.ndlocr_pdf_render_dpi must be at least 72")
    if require_models and (settings.llm_jp.path is None or not settings.llm_jp.path.exists()):
        problems.append("llm-jp model path does not exist")
    for model_name, model in (
        ("llm_jp", settings.llm_jp),
        ("llm_jp_base", settings.llm_jp_base),
        ("gaperon", settings.gaperon),
    ):
        if model.host not in {"127.0.0.1", "localhost", "::1"}:
            problems.append(f"models.{model_name}.host must remain loopback-only")
        if not 1 <= model.port <= 65535:
            problems.append(f"models.{model_name}.port must be between 1 and 65535")
        if model.startup_timeout_seconds <= 0 or model.request_timeout_seconds <= 0:
            problems.append(f"models.{model_name} timeouts must be positive")
        if model.parallel_slots < 1:
            problems.append(f"models.{model_name}.parallel_slots must be positive")
        if model.http_threads == 0 or model.http_threads < -1:
            problems.append(
                f"models.{model_name}.http_threads must be -1 (auto) or positive"
            )
    return problems
