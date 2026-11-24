"""Prepare the installed NDLOCR-Lite release's hash-verified default models."""

from __future__ import annotations

import importlib.util
import json
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace


def main() -> None:
    from model_download import MODELS, resolve_model_paths

    spec = importlib.util.find_spec("ocr")
    if spec is None or spec.origin is None:
        raise RuntimeError("Installed NDLOCR-Lite ocr module is missing")
    args = SimpleNamespace(**dict.fromkeys(MODELS))
    resolve_model_paths(args, Path(spec.origin).parent / "model")
    print(json.dumps({"version": version("ndlocr-lite"), "models": vars(args)}))


if __name__ == "__main__":
    main()
