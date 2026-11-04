"""Create non-destructive image-only OCR inputs with original-page provenance."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from typing import Any

from fukamiru.errors import FukamiruError

RECOVERY_KINDS = frozenset({"encrypted_pdf", "invalid_text_layer", "layout_analysis_failed"})
_render_lock = Lock()  # MuPDF is not safe to call concurrently in one process.


def recovery_directory(root: Path, digest: str, kind: str, dpi: int) -> Path:
    if (
        kind not in RECOVERY_KINDS
        or len(digest) != 64
        or any(char not in "0123456789abcdef" for char in digest)
    ):
        raise FukamiruError("Invalid PDF recovery identity")
    return root.resolve() / digest / f"{kind}-{max(72, min(int(dpi), 600))}dpi"


@contextmanager
def _child_lifetime(process: subprocess.Popen[str]):
    """Windows kills the native worker too if the service is forcibly stopped."""
    handle = None
    kernel = None
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class BasicLimits(ctypes.Structure):
                _fields_ = [
                    ("process_time", ctypes.c_int64),
                    ("job_time", ctypes.c_int64),
                    ("flags", wintypes.DWORD),
                    ("minimum", ctypes.c_size_t),
                    ("maximum", ctypes.c_size_t),
                    ("active", wintypes.DWORD),
                    ("affinity", ctypes.c_size_t),
                    ("priority", wintypes.DWORD),
                    ("scheduling", wintypes.DWORD),
                ]

            class Limits(ctypes.Structure):
                _fields_ = [
                    ("basic", BasicLimits),
                    ("io", ctypes.c_uint64 * 6),
                    ("process_memory", ctypes.c_size_t),
                    ("job_memory", ctypes.c_size_t),
                    ("peak_process", ctypes.c_size_t),
                    ("peak_job", ctypes.c_size_t),
                ]

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            kernel.CreateJobObjectW.restype = wintypes.HANDLE
            kernel.SetInformationJobObject.argtypes = [
                wintypes.HANDLE,
                ctypes.c_int,
                ctypes.c_void_p,
                wintypes.DWORD,
            ]
            kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.CreateJobObjectW(None, None)
            limits = Limits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if (
                not handle
                or not kernel.SetInformationJobObject(
                    handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
                )
                or not kernel.AssignProcessToJobObject(handle, int(process._handle))
            ):
                raise FukamiruError(
                    f"Cannot isolate PDF worker lifetime: {ctypes.get_last_error()}"
                )
        yield
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if handle and kernel is not None:
            kernel.CloseHandle(handle)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def is_encrypted_pdf_error(exc: BaseException) -> bool:
    message = str(exc).casefold()
    return any(
        term in message
        for term in (
            "required for aes algorithm",
            "file has not been decrypted",
            "password",
            "暗号化されており",
        )
    )


def is_pdf_layout_error(exc: BaseException) -> bool:
    """Identify PDF layout failures that can be retried from rendered pages."""
    return str(exc).startswith("PDF layout analysis failed:")


def renderable_language_evidence(source: Path) -> tuple[str | None, str]:
    """Read original language evidence even when pypdf lacks its AES backend."""
    import pymupdf

    with _render_lock, pymupdf.open(source) as document:
        if document.needs_pass and not document.authenticate(""):
            return None, ""
        value_type, value = document.xref_get_key(document.pdf_catalog(), "Lang")
        declared = value if value_type == "string" else None
        text = []
        length = 0
        for page in document:
            value = page.get_text()
            text.append(value)
            length += len(value)
            if length >= 100_000:
                break
        return declared, "\n".join(text)


def prepare_image_pdf(
    source: Path,
    source_sha256: str,
    *,
    root: Path,
    kind: str,
    dpi: int,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Delegate all rendering/saving to a resumable native worker process."""
    source = source.resolve()
    if file_sha256(source) != source_sha256:
        raise FukamiruError("PDF recovery source changed before rendering")
    dpi = max(72, min(int(dpi), 600))
    destination = recovery_directory(root, source_sha256, kind, dpi)
    destination.mkdir(parents=True, exist_ok=True)
    with _render_lock:
        environment = os.environ.copy()
        # A feature checkout must also be the import root of its subprocess.
        environment["PYTHONPATH"] = os.pathsep.join(
            [str(Path(__file__).resolve().parents[2]), environment.get("PYTHONPATH", "")]
        )
        environment["PYTHONIOENCODING"] = "utf-8"
        command = [
            sys.executable,
            "-u",
            "-m",
            "fukamiru.ocr.pdf_recovery_worker",
            str(source),
            source_sha256,
            str(root.resolve()),
            kind,
            str(dpi),
        ]
        messages: Queue[str | None] = Queue(maxsize=64)
        stop = Event()
        with (destination / "worker.stderr.log").open("a", encoding="utf-8") as errors:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=errors,
                text=True,
                encoding="utf-8",
                env=environment,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            with _child_lifetime(process):

                def read_messages() -> None:
                    assert process.stdout is not None
                    try:
                        for line in process.stdout:
                            while not stop.is_set():
                                try:
                                    messages.put(line, timeout=0.2)
                                    break
                                except Full:
                                    continue
                            if stop.is_set():
                                break
                    finally:
                        while not stop.is_set():
                            try:
                                messages.put(None, timeout=0.2)
                                break
                            except Full:
                                continue

                reader = Thread(target=read_messages, daemon=True, name="pdf-recovery-progress")
                reader.start()
                result = None
                failure = None
                last_message = time.monotonic()
                try:
                    while True:
                        try:
                            line = messages.get(timeout=1)
                        except Empty:
                            if process.poll() is not None and not reader.is_alive():
                                break
                            if time.monotonic() - last_message > 300:
                                raise FukamiruError(
                                    "PDF recovery worker made no progress for 300s"
                                ) from None
                            continue
                        if line is None:
                            break
                        last_message = time.monotonic()
                        try:
                            message = json.loads(line)
                        except json.JSONDecodeError:
                            continue  # Native library diagnostics are not protocol messages.
                        if message.get("type") == "progress" and progress is not None:
                            progress(message["payload"])
                        elif message.get("type") == "result":
                            result = message["payload"]
                        elif message.get("type") == "error":
                            failure = message["message"]
                    code = process.wait(timeout=5)
                    if code or result is None:
                        raise FukamiruError(failure or f"PDF recovery worker exited ({code})")
                    return result
                finally:
                    stop.set()
