from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from fukamiru import codex_cli
from fukamiru.codex_cli import CodexCLIProbe, probe_codex_cli, resolve_codex_cli


def _touch(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fixture")
    return str(path)


def _probe(versions: dict[str, str], *, authenticated: bool = True):
    def probe(path: str) -> CodexCLIProbe:
        return CodexCLIProbe(
            versions[path],
            authenticated,
            "ChatGPT" if authenticated else None,
            None if authenticated else "Codex authentication is unavailable",
        )

    return probe


def test_stale_configured_desktop_hash_falls_back_to_current_bundle(tmp_path: Path) -> None:
    stale = tmp_path / "bin" / "deleted-hash" / "codex.exe"
    current = _touch(tmp_path / "bin" / "current-hash" / "codex.exe")

    result = resolve_codex_cli(
        str(stale),
        required_models={"gpt-6-luna"},
        which=lambda _command: None,
        desktop_candidates=[current],
        probe=_probe({current: "0.158.0-alpha.2.1"}),
    )

    assert result.available is True
    assert result.configured_resolved is None
    assert result.resolved_executable == str(Path(current).resolve())
    assert result.source == "desktop"
    assert "does not exist" in str(result.fallback_reason)


def test_explicit_valid_configuration_precedes_newer_desktop_candidate(tmp_path: Path) -> None:
    configured = _touch(tmp_path / "custom" / "codex.exe")
    desktop = _touch(tmp_path / "desktop" / "codex.exe")
    versions = {
        str(Path(configured).resolve()): "0.158.0-alpha.2.1",
        str(Path(desktop).resolve()): "0.200.0",
    }

    result = resolve_codex_cli(
        configured,
        required_models={"gpt-6-sol"},
        which=lambda _command: None,
        desktop_candidates=[desktop],
        probe=_probe(versions),
    )

    assert result.source == "configured"
    assert result.resolved_executable == str(Path(configured).resolve())


def test_desktop_candidate_precedes_old_path_cli_for_generic_command(tmp_path: Path) -> None:
    desktop = _touch(tmp_path / "desktop" / "codex.exe")
    path_cli = _touch(tmp_path / "npm" / "codex.cmd")
    desktop = str(Path(desktop).resolve())
    path_cli = str(Path(path_cli).resolve())

    result = resolve_codex_cli(
        "codex",
        required_models={"gpt-6-luna", "gpt-6-sol"},
        which=lambda _command: path_cli,
        desktop_candidates=[desktop],
        probe=_probe({desktop: "0.158.0-alpha.2.1", path_cli: "0.150.1"}),
    )

    assert result.available is True
    assert result.source == "desktop"
    assert result.configured_resolved == path_cli
    assert result.resolved_executable == desktop


def test_old_path_cli_fails_fast_for_known_unsupported_model(tmp_path: Path) -> None:
    path_cli = str(Path(_touch(tmp_path / "npm" / "codex.cmd")).resolve())

    result = resolve_codex_cli(
        "codex",
        required_models={"gpt-6-luna"},
        which=lambda _command: path_cli,
        desktop_candidates=[],
        probe=_probe({path_cli: "0.150.1"}),
    )

    assert result.available is False
    assert result.resolved_executable is None
    assert "0.150.1 is too old" in str(result.error_reason)
    assert "gpt-6-luna" in str(result.error_reason)


def test_gpt_56_sol_is_supported_by_verified_cli_floor(tmp_path: Path) -> None:
    path_cli = str(Path(_touch(tmp_path / "codex.cmd")).resolve())

    result = resolve_codex_cli(
        "codex",
        required_models={"gpt-5.6-sol"},
        which=lambda _command: path_cli,
        desktop_candidates=[],
        probe=_probe({path_cli: "0.150.1"}),
    )

    assert result.available is True
    assert result.required_models == ("gpt-5.6-sol",)


def test_cli_below_verified_gpt_56_sol_floor_fails_fast(tmp_path: Path) -> None:
    path_cli = str(Path(_touch(tmp_path / "codex.cmd")).resolve())

    result = resolve_codex_cli(
        "codex",
        required_models={"gpt-5.6-sol"},
        which=lambda _command: path_cli,
        desktop_candidates=[],
        probe=_probe({path_cli: "0.150.0"}),
    )

    assert result.available is False
    assert "0.150.0 is too old" in str(result.error_reason)
    assert "gpt-5.6-sol" in str(result.error_reason)


def test_cli_absence_returns_actionable_status() -> None:
    result = resolve_codex_cli(
        "codex",
        required_models={"gpt-6-sol"},
        which=lambda _command: None,
        desktop_candidates=[],
        probe=lambda _path: pytest.fail("probe must not run without candidates"),
    )

    assert result.available is False
    assert result.configured_resolved is None
    assert result.resolved_executable is None
    assert "Install or update Codex Desktop" in str(result.error_reason)


def test_non_chatgpt_login_is_rejected_before_run(tmp_path: Path) -> None:
    executable = str(Path(_touch(tmp_path / "codex.exe")).resolve())

    result = resolve_codex_cli(
        executable,
        required_models={"gpt-6-sol"},
        which=lambda _command: None,
        desktop_candidates=[],
        probe=lambda _path: CodexCLIProbe("0.200.0", True, "API key"),
    )

    assert result.available is False
    assert "requires a ChatGPT Codex login" in str(result.error_reason)
    assert "API key" in str(result.error_reason)


def test_local_probe_cache_invalidates_when_auth_file_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = Path(_touch(tmp_path / "codex.exe")).resolve()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    auth = codex_home / "auth.json"
    auth.write_text("{}", encoding="utf-8")
    calls: list[tuple[str, ...]] = []

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(tuple(command))
        output = "codex-cli 0.200.0" if "--version" in command else "Logged in using ChatGPT"
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    codex_cli._cached_probe.cache_clear()
    monkeypatch.setattr(codex_cli.subprocess, "run", run)
    environment = {"CODEX_HOME": str(codex_home)}

    first = probe_codex_cli(str(executable), environment=environment)
    second = probe_codex_cli(str(executable), environment=environment)
    auth.write_text('{"changed":true}', encoding="utf-8")
    third = probe_codex_cli(str(executable), environment=environment)

    assert first == second == third
    assert len(calls) == 4
