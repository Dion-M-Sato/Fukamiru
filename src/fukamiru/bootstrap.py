"""Create only the configured runtime directories and initialize databases."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fukamiru.config import Settings, validate_settings
from fukamiru.errors import ConfigurationError
from fukamiru.storage.database import DatabasePair


@dataclass(frozen=True)
class BootstrapResult:
    created_directories: tuple[Path, ...]
    migrations: dict[str, list[str]]


def bootstrap(settings: Settings) -> BootstrapResult:
    problems = validate_settings(settings)
    if problems:
        raise ConfigurationError("; ".join(problems))

    directories = (
        settings.paths.state_db.parent,
        settings.paths.knowledge_db.parent,
        settings.paths.runtime_root / "logs",
        settings.paths.runtime_root / "cache",
        settings.paths.runtime_root / "tmp",
        settings.paths.runtime_root / "run",
        settings.paths.runtime_root / "index-build",
        settings.paths.corpus_root / "managed_sources",
        settings.paths.corpus_root / "oa_downloads",
        settings.paths.corpus_root / "import_staging",
        settings.paths.ocr_root / "extracted_text",
        settings.paths.ocr_root / "job_artifacts",
        settings.paths.knowledge_artifacts_root / "normalized_text",
        settings.paths.knowledge_artifacts_root / "page_images",
        settings.paths.knowledge_artifacts_root / "source_manifests",
        settings.paths.knowledge_artifacts_root / "retrieval_artifacts",
        settings.paths.faiss_root / "snapshots",
        settings.paths.faiss_root / "manifests",
    )
    created: list[Path] = []
    for directory in directories:
        if not directory.exists():
            directory.mkdir(parents=True, exist_ok=True)
            created.append(directory)
    migrations = DatabasePair.from_settings(settings).migrate()
    return BootstrapResult(tuple(created), migrations)
