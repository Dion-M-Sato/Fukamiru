"""Distributable source checks must reject private material before staging."""

from __future__ import annotations

import subprocess

import pytest

from scripts import check_publication as guard


@pytest.mark.parametrize("path", [
    "config/local.toml", "runtime/receipt.json", "data/state.db", "models/model.gguf",
    "tests/fixtures/input.docx", "auth.json", ".env", "backup.bundle",
])
def test_private_files_are_rejected(path):
    assert guard.check([(path, b"synthetic content")])


@pytest.mark.parametrize("content", [
    "hf_" + "a" * 40,
    "https://" + "a" * 24 + "--8080.hf.jobs",
    "-".join(["a" * 8, "a" * 4, "a" * 4, "a" * 4, "a" * 12]),
])
def test_tokens_live_endpoints_and_run_identifiers_are_rejected(content):
    assert guard.check([("src/example.py", content.encode())])


def test_model_pins_and_portable_example_are_allowed():
    entries = [
        ("src/model_identity.py", ("revision = '" + "a" * 40 + "'").encode()),
        ("config/local.example.toml", b'[paths]\nstate_db = "data/state.db"\n'),
    ]
    assert not guard.check(entries)


def test_untracked_nonignored_source_is_inspected_before_staging(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("runtime/\n", encoding="utf-8")
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime/receipt.json").write_text("private ignored data", encoding="utf-8")
    token = "hf_" + "b" * 40
    (tmp_path / "new.py").write_text(token, encoding="utf-8")
    monkeypatch.setattr(guard, "ROOT", tmp_path)
    entries = guard.source_files()
    assert "new.py" in {name for name, _ in entries}
    assert not any(name.startswith("runtime/") for name, _ in entries)
    assert any("service token" in failure for failure in guard.check(entries))
