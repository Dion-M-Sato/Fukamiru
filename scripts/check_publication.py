"""Check distributable source and optional ZIP/TAR artifacts for private material."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".pytest-tmp",
    ".ruff_cache",
    ".local",
    ".codex",
    ".agents",
    ".idea",
    ".vscode",
    ".cache",
    ".mypy_cache",
    "runtime",
    "data",
    "corpus",
    "models",
    "backups",
    "private",
    "exports",
    "artifacts",
    "output",
    "build",
    "dist",
    "logs",
    "tmp",
    "worktrees",
}
PRIVATE_SUFFIXES = {
    ".db", ".sqlite", ".sqlite3", ".gguf", ".safetensors", ".docx", ".pdf", ".epub",
    ".onnx", ".pt", ".pth", ".bin", ".bundle", ".log", ".pem", ".key", ".p12", ".pfx",
    ".zip", ".tar", ".gz",
}
CHECKS = {
    "machine user path": re.compile(
        r"(?i)(?:[a-z]:[\\/](?:Users|Documents and Settings)[\\/][a-z0-9_. -]+[\\/]"
        r"|/(?:Users|home)/[a-z0-9_.-]+/)"
    ),
    "dated personal policy": re.compile(r"user[-_]20\d{6}"),
    "literal run identifier": re.compile(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I
    ),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "service token": re.compile(
        r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
        r"hf_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16}|sk-[A-Za-z0-9_-]{24,})"
    ),
    "live HF endpoint": re.compile(r"https://[0-9a-f]{24}--[0-9]+\.hf\.jobs\b", re.I),
    "old service name": re.compile("research" + r"[_ -]?" + "ai", re.I),
}


def source_files() -> list[tuple[str, bytes]]:
    tracked = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        capture_output=True,
    )
    if tracked.returncode == 0 and (ROOT / ".git").exists():
        paths = [ROOT / name.decode() for name in tracked.stdout.split(b"\0") if name]
    else:
        paths = []
        for directory, subdirectories, filenames in os.walk(ROOT):
            subdirectories[:] = [name for name in subdirectories if name not in EXCLUDED]
            paths.extend(Path(directory) / name for name in filenames)
    return [(str(path.relative_to(ROOT)).replace("\\", "/"), path.read_bytes()) for path in paths]


def check(entries: list[tuple[str, bytes]]) -> list[str]:
    failures = []
    for name, raw in entries:
        path = PurePosixPath(name)
        if (
            EXCLUDED.intersection(part.casefold() for part in path.parts)
            or path.suffix.casefold() in PRIVATE_SUFFIXES
            or re.search(r"\.(?:db|sqlite3?)-(?:wal|shm|journal)$", name, re.I)
        ):
            failures.append(f"{name}: private/generated file")
        filename = path.name.casefold()
        if (
            filename in {"local.toml", "auth.json", "credentials.json", "tokens.json",
                         "secrets.json", "secrets.toml"}
            or filename.endswith(".local.toml")
            or path.parts[:2] == ("config", "profiles")
            or filename.startswith(".env") and filename != ".env.example"
        ):
            failures.append(f"{name}: local configuration")
        if not name.startswith("migrations/") and re.search(
            r"(?i)(?:^|[_.\-/])v\d+(?:[_.\-/]|$)", name
        ):
            failures.append(f"{name}: historical filename")
        if b"\0" in raw[:8192]:
            continue
        text = raw.decode("utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), 1):
            for label, pattern in CHECKS.items():
                if pattern.search(line):
                    failures.append(f"{name}:{number}: {label}")
        if name.startswith("tests/fixtures/") and re.search(
            r'"(?:source_run_id|observed_run_id)"\s*:', text
        ):
            failures.append(f"{name}: production provenance in a fixture")
    return failures


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args()
    entries = source_files()
    if args.archive:
        if zipfile.is_zipfile(args.archive):
            with zipfile.ZipFile(args.archive) as archive:
                entries = [
                    (item.filename, archive.read(item))
                    for item in archive.infolist()
                    if not item.is_dir()
                ]
        else:
            with tarfile.open(args.archive) as archive:
                entries = [
                    (item.name, archive.extractfile(item).read())
                    for item in archive
                    if item.isfile()
                ]
    failures = check(entries)
    if failures:
        raise SystemExit("\n".join(failures))
    print(f"Publication checks passed: {len(entries)} files")


if __name__ == "__main__":
    main()
