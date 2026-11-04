"""Strictly local resolution for pinned Hugging Face model snapshots."""

from __future__ import annotations

import os
from pathlib import Path

from fukamiru.errors import NonRetryableFukamiruError

_MODEL_DESCRIPTOR_FILES = ("config.json", "modules.json", "tokenizer_config.json")
_MODEL_WEIGHT_FILES = (
    "model.safetensors",
    "model.safetensors.index.json",
    "pytorch_model.bin",
    "pytorch_model.bin.index.json",
)


def configure_huggingface_offline(cache_dir: Path) -> None:
    """Disable Hub access before importing Transformers-family libraries."""

    os.environ["HF_HOME"] = str(cache_dir)
    os.environ["HF_HUB_CACHE"] = str(cache_dir)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    os.environ["HF_HUB_DISABLE_XET"] = "1"


def resolve_local_model_snapshot(
    *,
    cache_dir: Path,
    model_name: str,
    revision: str,
) -> Path:
    """Return an exact local snapshot path without invoking Hub resolution."""

    configured_model = Path(model_name).expanduser()
    if configured_model.is_dir():
        snapshot = configured_model.resolve()
    else:
        storage_name = f"models--{model_name.replace('/', '--')}"
        snapshot = (cache_dir / storage_name / "snapshots" / revision).resolve()

    missing = [name for name in _MODEL_DESCRIPTOR_FILES if not (snapshot / name).is_file()]
    if not any((snapshot / name).is_file() for name in _MODEL_WEIGHT_FILES):
        missing.append("model weights")
    if missing:
        missing_text = ", ".join(missing)
        raise NonRetryableFukamiruError(
            "Pinned embedding model is unavailable in the configured local cache. "
            f"Expected snapshot: {snapshot}; missing: {missing_text}. "
            "Fukamiru does not download models during a Run; set "
            "retrieval.embedding_cache_dir to a prepared shared cache."
        )
    return snapshot
