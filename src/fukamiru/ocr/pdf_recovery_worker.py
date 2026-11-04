"""Native PDF work in a separate process, with durable per-page checkpoints."""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fukamiru.errors import FukamiruError
from fukamiru.ocr.pdf_recovery import file_sha256, recovery_directory

MAX_PREPARATION_ATTEMPTS = 4
ASSEMBLY_BATCH_PAGES = 16
_WORKER_STARTED = time.monotonic()


def _replace_after_transient_lock(source: Path, target: Path) -> None:
    """Allow a recently closed Windows PDF handle to release before publishing."""
    for attempt in range(5):
        try:
            os.replace(source, target)
            return
        except OSError as exc:
            if getattr(exc, "winerror", None) not in {32, 33} or attempt == 4:
                raise
            time.sleep(0.1 * 2**attempt)


def _emit(phase: str, label: str, completed: int, total: int, **details: Any) -> None:
    print(
        json.dumps(
            {
                "type": "progress",
                "payload": {
                    "phase": phase,
                    "label": label,
                    "completed": completed,
                    "total": total,
                    "unit": "pages",
                    "worker_pid": os.getpid(),
                    "worker_cpu_seconds": round(time.process_time(), 3),
                    "worker_elapsed_seconds": round(time.monotonic() - _WORKER_STARTED, 3),
                    **details,
                },
            },
            ensure_ascii=True,
        ),
        flush=True,
    )


def _write_json(path: Path, data: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=True, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    _replace_after_transient_lock(temporary, path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


@contextmanager
def _exclusive_preparation(destination: Path):
    """Kernel locks are released even if the service/worker is forcibly killed."""
    with (destination / "preparation.lock").open("a+b") as stream:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def _check_page(page: Any, geometry: dict[str, Any]) -> None:
    if page.get_text().strip():
        raise FukamiruError("PDF recovery still contains a text layer")
    if (
        abs(page.rect.width - geometry["width"]) > 0.01
        or abs(page.rect.height - geometry["height"]) > 0.01
    ):
        raise FukamiruError("PDF recovery page dimensions changed")


def _cached_page(path: Path, geometry: dict[str, Any], identity: dict[str, Any]) -> bool:
    import pymupdf

    info = _read_json(path.with_suffix(".json"))
    if (
        not path.is_file()
        or any(info.get(k) != v for k, v in identity.items())
        or info.get("geometry") != geometry
        or info.get("sha256") != file_sha256(path)
    ):
        return False
    try:
        with pymupdf.open(path) as document:
            if len(document) != 1 or document.is_encrypted:
                return False
            _check_page(document[0], geometry)
        return True
    except Exception:
        return False


def _render_page(page: Any, path: Path, geometry: dict[str, Any], identity: dict[str, Any]) -> None:
    import pymupdf

    # A prior worker may have left its temporary PDF open at interruption.
    temporary = path.with_name(f"{path.stem}.{uuid.uuid4().hex}.tmp.pdf")
    pixmap = page.get_pixmap(dpi=identity["dpi"], alpha=False, annots=True)
    with pymupdf.open() as document:
        image = document.new_page(width=geometry["width"], height=geometry["height"])
        image.insert_image(image.rect, stream=pixmap.tobytes("png"))
        del pixmap
        # Compress one page; never compare all image streams with garbage=4.
        document.save(temporary, deflate=True, garbage=0, encryption=pymupdf.PDF_ENCRYPT_NONE)
    with pymupdf.open(temporary) as document:
        _check_page(document[0], geometry)
    with temporary.open("r+b") as stream:
        os.fsync(stream.fileno())
    digest = file_sha256(temporary)
    _replace_after_transient_lock(temporary, path)
    _write_json(
        path.with_suffix(".json"),
        {
            **identity,
            "geometry": geometry,
            "sha256": digest,
        },
    )


def _assemble(destination: Path, geometry: list[dict[str, Any]], identity: dict[str, Any]) -> Path:
    import pymupdf

    output = destination / "assembly.pdf"
    checkpoint = destination / "assembly.json"
    saved = _read_json(checkpoint)
    completed = 0
    if (
        output.is_file()
        and saved.get("identity") == identity
        and isinstance(saved.get("completed"), int)
        and 0 < saved["completed"] <= len(geometry)
        and saved.get("sha256") == file_sha256(output)
    ):
        try:
            with pymupdf.open(output) as document:
                if len(document) == saved["completed"] and not document.is_encrypted:
                    for page, expected in zip(document, geometry, strict=False):
                        _check_page(page, expected)
                    completed = saved["completed"]
        except Exception:
            completed = 0
    _emit("pdf_recovery_save", "保存済みPDFの結合位置を確認", completed, len(geometry))
    while completed < len(geometry):
        end = min(completed + ASSEMBLY_BATCH_PAGES, len(geometry))
        with pymupdf.open(output) if completed else pymupdf.open() as document:
            for index in range(completed, end):
                with pymupdf.open(destination / "pages" / f"page-{index + 1:06d}.pdf") as page:
                    document.insert_pdf(page)
            if completed:
                document.save(output, incremental=True, encryption=pymupdf.PDF_ENCRYPT_KEEP)
            else:
                document.save(output, garbage=0, encryption=pymupdf.PDF_ENCRYPT_NONE)
        with output.open("r+b") as stream:
            os.fsync(stream.fileno())
        completed = end
        _write_json(
            checkpoint,
            {"identity": identity, "completed": completed, "sha256": file_sha256(output)},
        )
        _emit("pdf_recovery_save", "画像PDFを分割保存・結合中", completed, len(geometry))
    return output


def prepare(source: Path, digest: str, root: Path, kind: str, dpi: int) -> dict[str, Any]:
    import pymupdf

    dpi = max(72, min(int(dpi), 600))
    destination = recovery_directory(root, digest, kind, dpi)
    destination.mkdir(parents=True, exist_ok=True)
    if file_sha256(source) != digest:
        raise FukamiruError("PDF recovery source changed before rendering")
    identity = {"original_sha256": digest, "kind": kind, "dpi": dpi}
    target = destination / "image-only.pdf"
    manifest_path = destination / "manifest.json"
    state_path = destination / "preparation.json"
    with _exclusive_preparation(destination):
        manifest = _read_json(manifest_path)
        if (
            target.is_file()
            and all(manifest.get(k) == v for k, v in identity.items())
            and manifest.get("input_pdf_sha256") == file_sha256(target)
        ):
            return {**manifest, "manifest_path": str(manifest_path)}
        state = _read_json(state_path)
        attempts = state.get("attempts", 0) if state.get("identity") == identity else 0
        if attempts >= MAX_PREPARATION_ATTEMPTS:
            raise FukamiruError(
                "PDF recovery exceeded four preparation attempts; review required"
            )
        _write_json(
            state_path, {"identity": identity, "attempts": attempts + 1, "status": "preparing"}
        )
        pages = destination / "pages"
        pages.mkdir(exist_ok=True)
        geometry = []
        reused = 0
        with pymupdf.open(source) as original:
            if original.needs_pass and not original.authenticate(""):
                raise FukamiruError("PDFを表示するパスワードが必要です。原本は変更していません")
            if not len(original):
                raise FukamiruError("PDF recovery source has no pages")
            for index, page in enumerate(original):
                rect = page.rect
                expected = {
                    "page": index + 1,
                    "width": rect.width,
                    "height": rect.height,
                    "source_rotation": page.rotation,
                }
                geometry.append(expected)
                path = pages / f"page-{index + 1:06d}.pdf"
                if _cached_page(path, expected, identity):
                    reused += 1
                    if reused % ASSEMBLY_BATCH_PAGES == 0 or index + 1 == len(original):
                        _emit(
                            "pdf_recovery_resume",
                            "保存済みページを再利用中",
                            index + 1,
                            len(original),
                            reused_pages=reused,
                        )
                else:
                    _render_page(page, path, expected, identity)
                    _emit(
                        "pdf_recovery_render",
                        "OCR用ページを画像化・保存中",
                        index + 1,
                        len(original),
                        reused_pages=reused,
                        recovery_kind=kind,
                    )
        temporary = _assemble(destination, geometry, identity)
        _emit("pdf_recovery_validate", "ページ数・文字層・暗号化を検証中", 0, len(geometry))
        with pymupdf.open(temporary) as document:
            if len(document) != len(geometry) or document.is_encrypted:
                raise FukamiruError("PDF recovery page count/encryption mismatch")
            for index, (page, expected) in enumerate(zip(document, geometry, strict=True)):
                _check_page(page, expected)
                if (index + 1) % ASSEMBLY_BATCH_PAGES == 0 or index + 1 == len(geometry):
                    _emit("pdf_recovery_validate", "OCR用PDFを検証中", index + 1, len(geometry))
        if file_sha256(source) != digest:
            raise FukamiruError("PDF recovery source changed while rendering")
        manifest = {
            "version": 2,
            **identity,
            "original_path": str(source),
            "input_pdf_path": str(target),
            "input_pdf_sha256": file_sha256(temporary),
            "page_count": len(geometry),
            "pages": geometry,
            "encrypted": False,
            "text_layer": False,
            "reused_pages": reused,
            "preparation_attempt": attempts + 1,
            "execution": "subprocess",
        }
        _emit("pdf_recovery_publish", "検証済みPDFを公開中", len(geometry), len(geometry))
        _replace_after_transient_lock(temporary, target)
        _write_json(manifest_path, manifest)
        _write_json(
            state_path, {"identity": identity, "attempts": attempts + 1, "status": "completed"}
        )
        return {**manifest, "manifest_path": str(manifest_path)}


def main() -> int:
    try:
        source, digest, root, kind, dpi = sys.argv[1:]
        result = prepare(Path(source), digest, Path(root), kind, int(dpi))
        print(json.dumps({"type": "result", "payload": result}, ensure_ascii=True), flush=True)
        return 0
    except Exception as exc:
        print(json.dumps({"type": "error", "message": str(exc)}, ensure_ascii=True), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
