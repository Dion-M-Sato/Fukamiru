"""Safe Git checkpoints for canonical project artifacts."""

from __future__ import annotations

import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from fukamiru.errors import GitCheckpointError


@dataclass(frozen=True)
class CheckpointResult:
    commit_sha: str
    changed_paths: tuple[str, ...]


class GitCheckpointService:
    ALLOWED_SUFFIXES = {".html", ".toml"}

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()

    def initialize(self, *, default_branch: str = "main") -> None:
        self.project_root.mkdir(parents=True, exist_ok=True)
        if not (self.project_root / ".git").exists():
            self._run("git", "init", "-b", default_branch)
        self._ensure_identity()

    def checkpoint(self, paths: Iterable[Path | str], message: str) -> CheckpointResult | None:
        if not message.strip():
            raise GitCheckpointError("Checkpoint message must not be empty")
        relative_paths = tuple(self._validate_path(path) for path in paths)
        if not relative_paths:
            raise GitCheckpointError("At least one canonical artifact is required")
        self._run("git", "add", "--", *relative_paths)
        staged = self._run("git", "diff", "--cached", "--name-only").stdout.splitlines()
        if not staged:
            return None
        self._run("git", "commit", "-m", message)
        commit_sha = self._run("git", "rev-parse", "HEAD").stdout.strip()
        return CheckpointResult(commit_sha=commit_sha, changed_paths=tuple(staged))

    def _validate_path(self, path: Path | str) -> str:
        candidate = Path(path)
        resolved = (
            candidate.resolve()
            if candidate.is_absolute()
            else (self.project_root / candidate).resolve()
        )
        try:
            relative = resolved.relative_to(self.project_root)
        except ValueError as exc:
            raise GitCheckpointError(f"Checkpoint path escapes project root: {path}") from exc
        if resolved.suffix.lower() not in self.ALLOWED_SUFFIXES and resolved.name != ".gitignore":
            raise GitCheckpointError(f"Not a canonical checkpoint artifact: {relative}")
        if not resolved.is_file():
            raise GitCheckpointError(f"Checkpoint artifact does not exist: {relative}")
        return relative.as_posix()

    def _ensure_identity(self) -> None:
        name = self._run("git", "config", "--local", "user.name", check=False).stdout.strip()
        email = self._run("git", "config", "--local", "user.email", check=False).stdout.strip()
        if not name:
            self._run("git", "config", "--local", "user.name", "Fukamiru")
        if not email:
            self._run("git", "config", "--local", "user.email", "fukamiru@localhost")

    def _run(self, *command: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            command,
            cwd=self.project_root,
            text=True,
            capture_output=True,
            check=False,
        )
        if check and result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise GitCheckpointError(f"{' '.join(command)} failed: {detail}")
        return result
