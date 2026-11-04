"""Resolve and locally preflight the Codex CLI used by inference workflows."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from fukamiru.errors import FukamiruError

_DEFAULT_COMMAND = "codex"
_MODEL_MINIMUM_VERSION = {
    "gpt-6-astra": "0.158.0-alpha.2.1",
    "gpt-6-luna": "0.158.0-alpha.2.1",
    "gpt-6-sol": "0.158.0-alpha.2.1",
    "gpt-5.6-sol": "0.150.1",
}
_VERSION_PATTERN = re.compile(
    r"(?:codex-cli\s+)?(?P<version>\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CodexCLIProbe:
    version: str | None
    authenticated: bool | None
    account_type: str | None
    error_reason: str | None = None


@dataclass(frozen=True)
class CodexCLIResolution:
    configured_command: str
    configured_resolved: str | None
    resolved_executable: str | None
    source: str | None
    version: str | None
    available: bool
    authenticated: bool | None
    account_type: str | None
    required_models: tuple[str, ...]
    error_reason: str | None
    fallback_reason: str | None = None

    def health_payload(self) -> dict[str, object]:
        """Keep the legacy keys while exposing the actual selected runtime."""

        return {
            "command": self.configured_command,
            "available": self.available,
            "configured_command": self.configured_command,
            "configured_resolved": self.configured_resolved,
            "resolved_executable": self.resolved_executable,
            "source": self.source,
            "version": self.version,
            "authenticated": self.authenticated,
            "account_type": self.account_type,
            "required_models": list(self.required_models),
            "error_reason": self.error_reason,
            "fallback_reason": self.fallback_reason,
        }


@dataclass(frozen=True)
class _Candidate:
    path: str
    source: str


def _auth_stamp(environment: Mapping[str, str]) -> tuple[int, int]:
    configured_home = environment.get("CODEX_HOME")
    auth_path = (
        Path(configured_home).expanduser() if configured_home else Path.home() / ".codex"
    ) / "auth.json"
    try:
        stat = auth_path.stat()
    except OSError:
        return (0, 0)
    return (stat.st_mtime_ns, stat.st_size)


def _process_command(executable: str, *arguments: str) -> list[str]:
    args = [executable, *arguments]
    if os.name == "nt" and Path(executable).suffix.casefold() in {".cmd", ".bat"}:
        return [
            os.environ.get("COMSPEC", "cmd.exe"),
            "/d",
            "/s",
            "/c",
            subprocess.list2cmdline(args),
        ]
    return args


@lru_cache(maxsize=32)
def _cached_probe(
    executable: str,
    executable_mtime_ns: int,
    executable_size: int,
    auth_mtime_ns: int,
    auth_size: int,
) -> CodexCLIProbe:
    del executable_mtime_ns, executable_size, auth_mtime_ns, auth_size
    try:
        version_process = subprocess.run(
            _process_command(executable, "--version"),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CodexCLIProbe(None, None, None, f"version probe failed: {exc}")
    version_output = "\n".join(
        part.strip() for part in (version_process.stdout, version_process.stderr) if part.strip()
    )
    version_match = _VERSION_PATTERN.search(version_output)
    if version_process.returncode != 0 or version_match is None:
        return CodexCLIProbe(
            None,
            None,
            None,
            "version probe did not return a valid `codex-cli` version",
        )
    version = version_match.group("version")
    try:
        login_process = subprocess.run(
            _process_command(executable, "login", "status"),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CodexCLIProbe(version, None, None, f"login status probe failed: {exc}")
    login_output = "\n".join(
        part.strip() for part in (login_process.stdout, login_process.stderr) if part.strip()
    )
    login_match = re.search(r"logged in using\s+([^\r\n]+)", login_output, re.IGNORECASE)
    authenticated = login_process.returncode == 0 and login_match is not None
    if not authenticated:
        return CodexCLIProbe(
            version,
            False,
            None,
            "Codex authentication is unavailable; run `codex login` and retry",
        )
    return CodexCLIProbe(version, True, login_match.group(1).strip())


def probe_codex_cli(
    executable: str,
    *,
    environment: Mapping[str, str] | None = None,
) -> CodexCLIProbe:
    """Run cached, local-only version and authentication probes."""

    environment = os.environ if environment is None else environment
    try:
        stat = Path(executable).stat()
        mtime_ns, size = stat.st_mtime_ns, stat.st_size
    except OSError:
        # A command shim returned by a custom resolver may only be spawnable by name.
        mtime_ns, size = 0, 0
    auth_mtime_ns, auth_size = _auth_stamp(environment)
    return _cached_probe(executable, mtime_ns, size, auth_mtime_ns, auth_size)


def _version_key(version: str) -> tuple[int, int, int, int, tuple[tuple[int, object], ...]]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?", version)
    if match is None:
        raise ValueError(version)
    prerelease = match.group(4)
    prerelease_key: tuple[tuple[int, object], ...] = ()
    if prerelease:
        prerelease_key = tuple(
            (0, int(part)) if part.isdigit() else (1, part.casefold())
            for part in prerelease.split(".")
        )
    return (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
        1 if prerelease is None else 0,
        prerelease_key,
    )


def _unsupported_models(version: str, required_models: Iterable[str]) -> list[str]:
    current = _version_key(version)
    return sorted(
        model
        for model in required_models
        if (minimum := _MODEL_MINIMUM_VERSION.get(model)) is not None
        and current < _version_key(minimum)
    )


def _desktop_candidates(environment: Mapping[str, str]) -> list[str]:
    if os.name != "nt":
        return []
    local_app_data = environment.get("LOCALAPPDATA")
    if not local_app_data:
        return []
    bin_root = Path(local_app_data) / "OpenAI" / "Codex" / "bin"
    paths = list(bin_root.glob("*/codex.exe"))
    direct = bin_root / "codex.exe"
    if direct.is_file():
        paths.append(direct)

    def modified(path: Path) -> int:
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return 0

    return [str(path) for path in sorted(paths, key=modified, reverse=True)]


def _normalize_candidate(path: str) -> str:
    candidate = Path(path).expanduser()
    try:
        return str(candidate.resolve(strict=True))
    except OSError:
        return str(candidate)


def resolve_codex_cli(
    configured_command: str,
    *,
    required_models: Iterable[str] = (),
    which: Callable[[str], str | None] = shutil.which,
    desktop_candidates: Iterable[str] | None = None,
    probe: Callable[[str], CodexCLIProbe] = probe_codex_cli,
    environment: Mapping[str, str] | None = None,
) -> CodexCLIResolution:
    """Select the first valid CLI by explicit-config, Desktop, then PATH policy."""

    environment = os.environ if environment is None else environment
    configured_command = configured_command.strip() or _DEFAULT_COMMAND
    required = tuple(sorted(set(required_models)))
    configured_path = Path(configured_command).expanduser()
    configured_is_path = configured_path.is_absolute() or configured_path.parent != Path(".")
    configured_resolved = (
        str(configured_path.resolve()) if configured_is_path and configured_path.is_file()
        else which(configured_command)
    )
    candidates: list[_Candidate] = []
    generic_command = configured_command.casefold() in {"codex", "codex.exe"}
    if configured_resolved and not generic_command:
        candidates.append(_Candidate(_normalize_candidate(configured_resolved), "configured"))
    discovered_desktop = (
        list(desktop_candidates)
        if desktop_candidates is not None
        else _desktop_candidates(environment)
    )
    candidates.extend(
        _Candidate(_normalize_candidate(path), "desktop")
        for path in discovered_desktop
        if Path(path).is_file()
    )
    path_resolved = which(_DEFAULT_COMMAND)
    if generic_command:
        path_resolved = configured_resolved
    if path_resolved:
        candidates.append(_Candidate(_normalize_candidate(path_resolved), "path"))

    unique: list[_Candidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        identity = os.path.normcase(os.path.abspath(candidate.path))
        if identity not in seen:
            seen.add(identity)
            unique.append(candidate)

    failures: list[str] = []
    for candidate in unique:
        result = probe(candidate.path)
        if result.error_reason:
            failures.append(f"{candidate.source} {candidate.path}: {result.error_reason}")
            continue
        if result.version is None:
            failures.append(f"{candidate.source} {candidate.path}: version is unavailable")
            continue
        unsupported = _unsupported_models(result.version, required)
        if unsupported:
            minimum = max(_MODEL_MINIMUM_VERSION[model] for model in unsupported)
            failures.append(
                f"{candidate.source} {candidate.path}: Codex CLI {result.version} is too old "
                f"for {', '.join(unsupported)} (requires {minimum} or newer)"
            )
            continue
        if result.authenticated is not True:
            failures.append(
                f"{candidate.source} {candidate.path}: Codex authentication is unavailable; "
                "run `codex login` and retry"
            )
            continue
        if (result.account_type or "").casefold() != "chatgpt":
            failures.append(
                f"{candidate.source} {candidate.path}: Fukamiru requires a ChatGPT "
                f"Codex login, but `codex login status` reported "
                f"{result.account_type or 'an unknown account type'}"
            )
            continue
        fallback_reason = None
        if (
            candidate.source != "configured"
            and configured_is_path
            and not configured_path.is_file()
        ):
            fallback_reason = (
                f"Configured Codex command does not exist: {configured_command}; "
                f"using {candidate.source} candidate"
            )
        return CodexCLIResolution(
            configured_command=configured_command,
            configured_resolved=(
                _normalize_candidate(configured_resolved) if configured_resolved else None
            ),
            resolved_executable=candidate.path,
            source=candidate.source,
            version=result.version,
            available=True,
            authenticated=True,
            account_type=result.account_type,
            required_models=required,
            error_reason=None,
            fallback_reason=fallback_reason,
        )

    if failures:
        reason = "No compatible Codex CLI candidate was found. " + " | ".join(failures)
    else:
        reason = (
            "Codex CLI executable was not found. Install or update Codex Desktop, "
            "or configure translation.codex_command with a valid executable."
        )
    return CodexCLIResolution(
        configured_command=configured_command,
        configured_resolved=(
            _normalize_candidate(configured_resolved) if configured_resolved else None
        ),
        resolved_executable=None,
        source=None,
        version=None,
        available=False,
        authenticated=None,
        account_type=None,
        required_models=required,
        error_reason=reason,
    )


def require_codex_cli(
    configured_command: str,
    *,
    required_models: Iterable[str] = (),
    which: Callable[[str], str | None] = shutil.which,
) -> CodexCLIResolution:
    resolution = resolve_codex_cli(
        configured_command,
        required_models=required_models,
        which=which,
    )
    if not resolution.available or resolution.resolved_executable is None:
        raise FukamiruError(resolution.error_reason or "Codex CLI is unavailable")
    return resolution
