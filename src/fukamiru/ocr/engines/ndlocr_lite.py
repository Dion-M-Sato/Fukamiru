"""NDLOCR-Lite command adapter for local Japanese OCR."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

from fukamiru.errors import FukamiruError
from fukamiru.ocr.models import OCRPage, OCRResult
from fukamiru.stage_metrics import StageMetrics


class NDLOCRLiteEngine:
    name = "ndlocr_lite"

    def __init__(
        self,
        *,
        command: str = "ndlocr-lite",
        timeout_seconds: int = 3600,
        pdf_render_dpi: int = 150,
    ) -> None:
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.pdf_render_dpi = pdf_render_dpi

    def process(
        self, source: Path, language: str, *, metrics: StageMetrics | None = None
    ) -> OCRResult:
        with tempfile.TemporaryDirectory(prefix="fukamiru-ndlocr-") as temp_name:
            temp = Path(temp_name)
            command = [self.command]
            # The Windows console launcher spawns Python as a grandchild. Calling
            # its documented module entry point directly saves the launcher and
            # makes the OCR process CPU visible to GetProcessTimes.
            if os.name == "nt" and Path(self.command).name.casefold() == "ndlocr-lite.exe":
                interpreter = Path(self.command).with_name("python.exe")
                if interpreter.is_file():
                    command = [str(interpreter), "-m", "ocr"]
            args = [
                *command,
                "--sourcepdf",
                str(source),
                "--output",
                str(temp),
                "--pdf-render-dpi",
                str(self.pdf_render_dpi),
                "--json-only",
                "--enable-tcy",
            ]
            ocr_env = os.environ.copy()
            if os.name == "nt":
                ocr_env["PYTHONIOENCODING"] = "utf-8"
                # Leave CPU capacity for the API and its watchdog while ONNX runs.
                for name in (
                    "OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                    "NUMEXPR_NUM_THREADS",
                ):
                    ocr_env[name] = "4"
            try:
                with subprocess.Popen(
                    args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8", errors="replace",
                    env=ocr_env,
                    creationflags=(
                        subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS
                        if os.name == "nt" else 0
                    ),
                ) as process:
                    try:
                        reserved_cores = _reserve_windows_cpu_for_server(process)
                    except OSError as exc:
                        process.kill()
                        process.communicate()
                        raise FukamiruError(
                            "NDLOCR-LiteのCPU使用枠を設定できませんでした"
                        ) from exc
                    if metrics is not None and reserved_cores is not None:
                        metrics.annotate(ocr_affinity_cores=reserved_cores)
                    try:
                        stdout, stderr = process.communicate(timeout=self.timeout_seconds)
                    except subprocess.TimeoutExpired as exc:
                        process.kill()
                        process.communicate()
                        raise FukamiruError("NDLOCR-Liteの実行がタイムアウトしました") from exc
                    finally:
                        cpu_seconds = _windows_process_cpu_seconds(process)
                        if metrics is not None and cpu_seconds is not None:
                            metrics.add_external_cpu("ocr_engine", cpu_seconds)
                    returncode = process.returncode
            except FileNotFoundError as exc:
                raise FukamiruError(
                    "NDLOCR-Liteが見つかりません。ocr.ndlocr_commandを設定してください。"
                ) from exc
            if returncode != 0:
                tail = (stderr or stdout or "").strip()[-1000:]
                raise FukamiruError(
                    f"NDLOCR-Liteの実行に失敗しました (終了コード {returncode}): {tail}"
                )
            result_path = temp / f"{source.stem}.json"
            if not result_path.is_file():
                candidates = list(temp.glob("*.json"))
                if len(candidates) != 1:
                    raise FukamiruError("NDLOCR-LiteのJSON出力を特定できませんでした")
                result_path = candidates[0]
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            contents = payload.get("contents")
            if not isinstance(contents, list):
                raise FukamiruError("NDLOCR-LiteのJSON出力にcontentsがありません")
            pages = tuple(self._page(index, lines) for index, lines in enumerate(contents, 1))
            expected = int((payload.get("pdfinfo") or {}).get("page_count") or len(pages))
            if len(pages) != expected:
                raise FukamiruError(
                    f"NDLOCR-Liteのページ数が一致しません: {expected} -> {len(pages)}"
                )
        return OCRResult(source, _sha256(source), language, self.name, pages)

    @staticmethod
    def _page(page_number: int, lines: object) -> OCRPage:
        values = lines if isinstance(lines, list) else []
        text = "\n".join(
            str(line.get("text", ""))
            for line in values
            if isinstance(line, dict) and line.get("text") is not None
        )
        return OCRPage(
            page_number=page_number,
            text=text,
            metadata={"lines": values, "reading_order": "engine"},
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _windows_process_cpu_seconds(process: subprocess.Popen[str]) -> float | None:
    """Read direct-child CPU while Popen still owns its Windows handle."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        api = ctypes.WinDLL("kernel32", use_last_error=True).GetProcessTimes
        api.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        api.restype = wintypes.BOOL
        if not api(
            int(process._handle), ctypes.byref(created), ctypes.byref(exited),
            ctypes.byref(kernel), ctypes.byref(user),
        ):
            return None

        def ticks(value: wintypes.FILETIME) -> int:
            return (value.dwHighDateTime << 32) | value.dwLowDateTime

        return (ticks(kernel) + ticks(user)) / 10_000_000
    except (AttributeError, OSError, TypeError, ValueError):
        # Measurement must never change the OCR result.
        return None


def _reserve_windows_cpu_for_server(process: subprocess.Popen[str]) -> int | None:
    """Limit the direct OCR child so the API retains schedulable CPU cores."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get_mask = kernel.GetProcessAffinityMask
    get_mask.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(ctypes.c_size_t),
        ctypes.POINTER(ctypes.c_size_t),
    ]
    get_mask.restype = wintypes.BOOL
    set_mask = kernel.SetProcessAffinityMask
    set_mask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
    set_mask.restype = wintypes.BOOL
    process_mask = ctypes.c_size_t()
    system_mask = ctypes.c_size_t()
    handle = wintypes.HANDLE(int(process._handle))
    if not get_mask(handle, ctypes.byref(process_mask), ctypes.byref(system_mask)):
        raise OSError(ctypes.get_last_error(), "GetProcessAffinityMask failed")
    available = [
        1 << bit for bit in range(ctypes.sizeof(ctypes.c_size_t) * 8)
        if process_mask.value & (1 << bit)
    ]
    if len(available) <= 2:
        return len(available)
    reserved = max(2, len(available) // 3)
    allowed = available[:len(available) - reserved]
    if not set_mask(handle, ctypes.c_size_t(sum(allowed))):
        raise OSError(ctypes.get_last_error(), "SetProcessAffinityMask failed")
    return len(allowed)
