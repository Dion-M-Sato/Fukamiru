"""Make NDLOCR-Lite's JSON-only PDF path avoid text-layer PDF generation."""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
from pathlib import Path


def patch_module(path: Path) -> bool:
    source = path.read_text(encoding="utf-8")
    marker = '        output_pdf = getattr(args, "pdf_output", None)\n'
    guard = '        if not getattr(args, "json_only", False):\n'
    if guard + "    " + marker in source:
        return False
    start = source.find(marker)
    if start < 0:
        raise RuntimeError(f"Unrecognized NDLOCR PDF output implementation: {path}")
    end = source.find('        print("Total PDF calculation time:', start)
    if end < 0:
        raise RuntimeError(f"Missing NDLOCR PDF output boundary: {path}")
    block = source[start:end]
    if "embed_text_layer_pdf(" not in block:
        raise RuntimeError(f"Missing NDLOCR text-layer call: {path}")
    replacement = guard + "".join("    " + line if line.strip() else line
                                  for line in block.splitlines(keepends=True))
    updated = source[:start] + replacement + source[end:]
    ast.parse(updated, filename=str(path))
    path.write_text(updated, encoding="utf-8", newline="\n")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module-path", type=Path)
    args = parser.parse_args()
    path = args.module_path
    if path is None:
        spec = importlib.util.find_spec("ocr")
        if spec is None or spec.origin is None:
            raise RuntimeError("Installed NDLOCR-Lite ocr module is missing")
        path = Path(spec.origin)
    print(json.dumps({"path": str(path), "changed": patch_module(path)}))


if __name__ == "__main__":
    main()
