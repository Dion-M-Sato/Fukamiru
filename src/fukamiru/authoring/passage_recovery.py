"""Bounded local OCR of selected damaged pages, with immutable provenance."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

from fukamiru.authoring.source_reads import digest
from fukamiru.knowledge.page_reading import text_issues
from fukamiru.ocr.engines.ndlocr_lite import (
    NDLOCRLiteEngine,
    _reserve_windows_cpu_for_server,
)

_RECOVERY_LOCK = threading.Lock()


def recover_pages(
    known: dict[str, Any], selected: dict[str, Any], settings: Any,
) -> dict[str, Any]:
    """OCR adds review candidates; it never silently validates repaired quotes."""
    if not _RECOVERY_LOCK.acquire(timeout=1):
        raise TimeoutError("Local passage OCR is busy; source remains unresolved")
    try:
        return _recover_pages(known, selected, settings)
    finally:
        _RECOVERY_LOCK.release()


def _recover_pages(
    known: dict[str, Any], selected: dict[str, Any], settings: Any,
) -> dict[str, Any]:
    bad = [p for p in selected.get("pages", []) if text_issues(p["text"])]
    if not bad or not known.get("source_path"):
        return known
    limit = settings.translation.academic_research_ocr_pages
    bad = bad[:limit]
    seconds = settings.translation.academic_research_ocr_seconds
    directory = settings.paths.runtime_root / "source_reads" / digest({
        "sha256": known["content_sha256"], "pages": [p["number"] for p in bad],
        "policy": "local-page-ocr-v1", "dpi": 200,
        "command": settings.ocr.ndlocr_command, "seconds": seconds,
    })
    directory.mkdir(parents=True, exist_ok=True)
    cache = directory / "candidates.json"
    if cache.is_file():
        candidates = json.loads(cache.read_text(encoding="utf-8"))
    else:
        import pymupdf

        started = time.monotonic()
        # The selected file is hash-verified by the reader. Recheck before OCR
        # because a source can change between a persisted read and a later round.
        import hashlib

        with Path(known["source_path"]).open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != known["content_sha256"]:
                raise ValueError("source_hash_mismatch")
        with tempfile.TemporaryDirectory(prefix="passage-", dir=directory) as temporary:
            source = Path(temporary) / "selected.pdf"
            with pymupdf.open(known["source_path"]) as pdf, pymupdf.open() as subset:
                for page in bad:
                    number = page["number"]
                    subset.insert_pdf(pdf, from_page=number - 1, to_page=number - 1)
                    image = directory / f"page-{number}.png"
                    pdf[number - 1].get_pixmap(dpi=200, alpha=False).save(image)
                subset.save(source)
            result = NDLOCRLiteEngine(
                command=settings.ocr.ndlocr_command,
                timeout_seconds=seconds, pdf_render_dpi=200,
            ).process(source, "ja")
        candidates = [{"number": page["number"], "image_path": str(
            directory / f"page-{page['number']}.png"), "lines": ocr.metadata.get("lines", []),
            "text": ocr.text} for page, ocr in zip(bad, result.pages, strict=True)]
        manifest = directory / "normal.json"
        manifest.write_text(json.dumps(candidates, ensure_ascii=False), encoding="utf-8")
        interpreter = Path(settings.ocr.ndlocr_command).with_name(
            "python.exe" if os.name == "nt" else "python")
        remaining = seconds - (time.monotonic() - started)
        segmented = directory / "segmented.json"
        if interpreter.is_file() and remaining > 2:
            env = {**os.environ, "PYTHONIOENCODING": "utf-8", "OMP_NUM_THREADS": "4",
                   "MKL_NUM_THREADS": "4", "OPENBLAS_NUM_THREADS": "4"}
            with (directory / "recognition.log").open("w", encoding="utf-8") as log:
                with subprocess.Popen(
                    [str(interpreter), str(Path(__file__).parents[1] / "ocr" /
                        "passage_segments.py"), str(manifest), str(segmented)],
                    stdout=log, stderr=subprocess.STDOUT, env=env,
                    creationflags=(subprocess.CREATE_NO_WINDOW |
                                   subprocess.BELOW_NORMAL_PRIORITY_CLASS
                                   if os.name == "nt" else 0),
                ) as process:
                    _reserve_windows_cpu_for_server(process)
                    try:
                        process.wait(timeout=remaining)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                if process.returncode == 0 and segmented.is_file():
                    segmented_pages = json.loads(segmented.read_text(encoding="utf-8"))
                    for normal, segmented_page in zip(candidates, segmented_pages, strict=True):
                        normal["segmented_text"] = "\n".join(
                            line["text"] for line in segmented_page["lines"])
        pending = directory / "candidates.partial"
        pending.write_text(json.dumps(candidates, ensure_ascii=False), encoding="utf-8")
        pending.replace(cache)
    recovered = deepcopy(known)
    recovered["preferred_seed_page"] = selected.get("seed_page")
    by_number = {p["number"]: p for p in candidates}
    for page in recovered["pages"]:
        if candidate := by_number.get(page["number"]):
            page["original_text"] = page["text"]
            page["text"] = candidate.get("segmented_text") or candidate["text"]
            page["requires_image_review"] = True
            page["extraction_method"] = "local_ocr_review_candidate"
            page["image_path"] = candidate["image_path"]
            page["ocr_artifact"] = str(cache)
    return recovered
