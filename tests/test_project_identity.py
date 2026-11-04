from __future__ import annotations

import importlib
import os
import subprocess
import sys
import tomllib
from pathlib import Path


def test_package_metadata_and_entrypoints_use_fukamiru():
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["name"] == "fukamiru"
    assert project["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["src/fukamiru"]
    for command, target in project["project"]["scripts"].items():
        module, function = target.split(":")
        assert command in {"fukamiru", "fukamiru-ocr"}
        assert module.startswith("fukamiru.")
        assert callable(getattr(importlib.import_module(module), function))


def test_module_cli_works_outside_the_repository(tmp_path):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-m", "fukamiru.cli", "--help"],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(root / "src"), "PYTHONIOENCODING": "utf-8"},
        capture_output=True, text=True, encoding="utf-8", check=True,
    )
    assert "fukamiru" in result.stdout
    assert "start" in result.stdout and "init" in result.stdout
