"""Windows-friendly command line entry point."""

from __future__ import annotations

import argparse
import shutil
import sys
import threading
import webbrowser
from importlib.util import find_spec
from pathlib import Path

from fukamiru.bootstrap import bootstrap
from fukamiru.config import default_config_path, load_settings, validate_settings
from fukamiru.documents.canonical_html import CanonicalHTMLValidator
from fukamiru.errors import FukamiruError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fukamiru")
    parser.add_argument("--config", type=Path, default=None, help="Path to local.toml")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init", help="Create configured directories and migrate databases")
    start = subparsers.add_parser("start", help="Start the localhost Workbench")
    start.add_argument("--no-browser", action="store_true")
    subparsers.add_parser("status", help="Validate configuration and print readiness")
    validate = subparsers.add_parser("validate-html", help="Validate canonical HTML")
    validate.add_argument("path", type=Path)
    return parser


def _ensure_local_config(path: Path) -> None:
    if path.exists():
        return
    if sys.platform != "win32":
        raise FukamiruError(
            f"Configuration does not exist: {path}. Copy config/local.example.toml and edit paths."
        )
    repository_root = Path(__file__).resolve().parents[2]
    example = repository_root / "config" / "local.example.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(example, path)


def _settings(config_path: Path | None):
    resolved = config_path or default_config_path()
    _ensure_local_config(resolved)
    return load_settings(resolved)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate-html":
            report = CanonicalHTMLValidator().validate(args.path.read_text(encoding="utf-8"))
            for issue in report.issues:
                print(f"{issue.severity.upper()} {issue.code}: {issue.message}")
            return 0 if report.valid else 1

        settings = _settings(args.config)
        problems = validate_settings(settings)
        if problems:
            raise FukamiruError("; ".join(problems))

        if args.command == "init":
            result = bootstrap(settings)
            print(f"READY: {settings.config_path}")
            print(f"Created directories: {len(result.created_directories)}")
            print(f"Migrations: {result.migrations}")
            return 0
        if args.command == "status":
            print("READY" if settings.paths.state_db.exists() else "NOT INITIALIZED")
            print(f"Config: {settings.config_path}")
            print(f"State DB: {settings.paths.state_db}")
            print(f"Knowledge DB: {settings.paths.knowledge_db}")
            codex = shutil.which(settings.translation.codex_command)
            print(f"Worker: {'enabled' if settings.worker.enabled else 'disabled'}")
            print("Lexical RAG: SQLite FTS5 trigram")
            semantic_ready = all(
                find_spec(module) is not None
                for module in ("faiss", "numpy", "sentence_transformers")
            )
            print(f"Semantic RAG dependencies: {'ready' if semantic_ready else 'NOT INSTALLED'}")
            print(f"Embedding model: {settings.retrieval.embedding_model}")
            print(f"Codex CLI: {codex or 'NOT FOUND'}")
            print(f"OCR provider: {settings.ocr.provider}")
            return 0
        if args.command == "start":
            bootstrap(settings)
            if settings.server.open_browser and not args.no_browser:
                browser_timer = threading.Timer(
                    0.8,
                    webbrowser.open,
                    args=(f"http://{settings.server.host}:{settings.server.port}",),
                )
                browser_timer.daemon = True
                browser_timer.start()
            try:
                import uvicorn
            except ImportError as exc:
                raise FukamiruError(
                    "uvicorn is not installed; run scripts/windows/install.ps1"
                ) from exc
            from fukamiru.api import create_app

            uvicorn.run(
                create_app(settings),
                host=settings.server.host,
                port=settings.server.port,
                log_level="info",
            )
            return 0
    except (FukamiruError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
