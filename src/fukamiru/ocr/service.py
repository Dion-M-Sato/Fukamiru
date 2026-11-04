"""Language routing, durable jobs, and non-destructive OCR output writing."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from queue import Queue
from threading import Lock
from typing import Any

from pypdf import PdfReader

from fukamiru.config import Settings
from fukamiru.errors import FukamiruError
from fukamiru.knowledge.language import classify_language
from fukamiru.ocr.engines import DirectTextEngine, MistralOCREngine, NDLOCRLiteEngine
from fukamiru.ocr.models import OCRPage, OCRResult
from fukamiru.ocr.pdf_recovery import (
    prepare_image_pdf,
    renderable_language_evidence,
)
from fukamiru.stage_metrics import StageMetrics
from fukamiru.storage.database import DatabasePair
from fukamiru.storage.events import EventRepository
from fukamiru.storage.runs import utc_now

SUPPORTED_OCR_LANGUAGES = frozenset({"ja", "en", "fr", "ru", "zh", "ko", "de"})
OCR_LANGUAGE_ERROR = "OCR言語は ja、en、fr、ru、zh、ko、de のいずれかを指定してください"
ACTIVE_OCR_STATUSES = frozenset({"needs_language", "queued", "processing"})
_MISTRAL_OCR_LOCK = Lock()
_NDLOCR_LITE_LOCK = Lock()


@dataclass(frozen=True)
class OCRLanguageDecision:
    """Auditable, conservative language-routing decision for an OCR job."""

    language: str
    confidence: float
    method: str
    evidence: dict[str, Any]

    @property
    def routable(self) -> bool:
        return self.language in SUPPORTED_OCR_LANGUAGES and self.confidence >= 0.90


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_language(value: str | None) -> str:
    normalized = str(value or "").strip().casefold().replace("_", "-")
    aliases = {
        "jpn": "ja",
        "japanese": "ja",
        "日本語": "ja",
        "eng": "en",
        "english": "en",
        "英語": "en",
        "fra": "fr",
        "fre": "fr",
        "french": "fr",
        "français": "fr",
        "フランス語": "fr",
        "rus": "ru",
        "russian": "ru",
        "русский": "ru",
        "ロシア語": "ru",
        "zho": "zh",
        "chi": "zh",
        "chinese": "zh",
        "中文": "zh",
        "中国語": "zh",
        "kor": "ko",
        "korean": "ko",
        "한국어": "ko",
        "韓国語": "ko",
        "deu": "de",
        "ger": "de",
        "german": "de",
        "deutsch": "de",
        "ドイツ語": "de",
    }
    normalized = aliases.get(normalized, normalized.split("-", 1)[0])
    return normalized if normalized in SUPPORTED_OCR_LANGUAGES else "und"


def routed_engine(language: str) -> str:
    normalized = normalize_language(language)
    if normalized in {"en", "fr", "ru", "zh", "ko", "de"}:
        return "mistral"
    if normalized == "ja":
        return "ndlocr_lite"
    raise FukamiruError(OCR_LANGUAGE_ERROR)


def detect_pdf_language(path: Path) -> OCRLanguageDecision:
    """Detect OCR routing language without modifying or OCRing the source PDF.

    Only standards-based PDF language metadata and conservative residual-text
    evidence may auto-route. A filename is deliberately never treated as proof.
    """
    try:
        reader = PdfReader(path)
        declared: list[tuple[str, str]] = []
        root = reader.trailer.get("/Root")
        if root is not None:
            root_language = root.get("/Lang")
            if root_language:
                declared.append(("catalog_lang", str(root_language)))
        xmp = reader.xmp_metadata
        xmp_languages = getattr(xmp, "dc_language", ()) or ()
        if isinstance(xmp_languages, str):
            xmp_languages = (xmp_languages,)
        for value in xmp_languages:
            declared.append(("xmp_dc_language", str(value)))
        metadata = reader.metadata or {}
        for key in ("/Lang", "/Language"):
            if metadata.get(key):
                declared.append(("document_metadata", str(metadata[key])))
        normalized = [(source, raw, normalize_language(raw)) for source, raw in declared]
        supported = [
            (source, raw, language) for source, raw, language in normalized if language != "und"
        ]
        unsupported = [
            {"source": source, "value": raw}
            for source, raw, language in normalized
            if language == "und"
        ]
        if unsupported:
            return OCRLanguageDecision(
                "und",
                0.0,
                "unsupported_pdf_language_metadata",
                {"signals": unsupported, "threshold": 0.90},
            )
        languages = {language for _, _, language in supported}
        if len(languages) == 1:
            language = next(iter(languages))
            return OCRLanguageDecision(
                language,
                0.99,
                "pdf_declared_language",
                {
                    "signals": [
                        {"source": source, "value": raw, "language": detected}
                        for source, raw, detected in supported
                    ],
                    "threshold": 0.90,
                },
            )
        if len(languages) > 1:
            return OCRLanguageDecision(
                "und",
                0.0,
                "conflicting_pdf_language_metadata",
                {
                    "signals": [
                        {"source": source, "value": raw, "language": detected}
                        for source, raw, detected in supported
                    ],
                    "threshold": 0.90,
                },
            )
        residual_text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:
        try:
            declared, text = renderable_language_evidence(path)
            if declared:
                language = normalize_language(declared)
                return OCRLanguageDecision(
                    language,
                    0.99 if language != "und" else 0.0,
                    "pdf_renderable_declared_language",
                    {"value": declared, "threshold": 0.90},
                )
            return _detect_residual_text_language(text)
        except Exception:
            pass
        return OCRLanguageDecision(
            "und",
            0.0,
            "pdf_inspection_failed",
            {"error_type": exc.__class__.__name__, "threshold": 0.90},
        )
    return _detect_residual_text_language(residual_text)


def _detect_residual_text_language(text: str) -> OCRLanguageDecision:
    language, signals = classify_language(text, strict=True)
    evidence = {"signals": signals, "threshold": 0.90}
    if language != "und":
        method = (
            "residual_text_script" if language in {"ja", "zh", "ko"} else "residual_text_lexical"
        )
        return OCRLanguageDecision(language, 0.98, method, evidence)
    return OCRLanguageDecision("und", 0.0, "insufficient_language_evidence", evidence)


class PDFOCRService:
    """Process one PDF into page-aligned UTF-8 text and JSON."""

    def __init__(self, settings: Settings, *, engine_factory: Any | None = None) -> None:
        self.settings = settings
        self.engine_factory = engine_factory or self._engine

    def _engine(self, name: str) -> Any:
        if name == "direct":
            return DirectTextEngine()
        if name == "mistral":
            return MistralOCREngine(
                model=self.settings.ocr.mistral_model,
                max_attempts=self.settings.ocr.mistral_max_attempts,
            )
        if name == "ndlocr_lite":
            return NDLOCRLiteEngine(
                command=self.settings.ocr.ndlocr_command,
                timeout_seconds=self.settings.ocr.ndlocr_timeout_seconds,
                pdf_render_dpi=self.settings.ocr.ndlocr_pdf_render_dpi,
            )
        raise FukamiruError(f"未対応のOCRエンジンです: {name}")

    def process_file(
        self,
        source_path: Path,
        *,
        language: str,
        output_dir: Path,
        force_ocr: bool = False,
        overwrite: bool = False,
        output_format: str = "txt",
        metrics: StageMetrics | None = None,
    ) -> dict[str, Any]:
        source = source_path.expanduser().resolve()
        if not source.is_file() or source.suffix.casefold() != ".pdf":
            raise FukamiruError(f"既存のPDFを指定してください: {source}")
        normalized = normalize_language(language)
        if normalized == "und":
            raise FukamiruError(OCR_LANGUAGE_ERROR)
        if output_format not in {"txt", "md"}:
            raise FukamiruError("出力形式は txt または md を指定してください")
        output_dir.mkdir(parents=True, exist_ok=True)
        text_path = output_dir / f"{source.stem}.{output_format}"
        json_path = output_dir / f"{source.stem}.ocr.json"
        if not overwrite and (text_path.exists() or json_path.exists()):
            return {
                "status": "skipped",
                "source": str(source),
                "text_path": str(text_path),
                "json_path": str(json_path),
                "reason": "output already exists",
            }
        from fukamiru.knowledge.pdf_text_layer import inspect_pdf_text_layer

        if metrics is not None:
            metrics.transition("ocr_text_layer_check")
        layer = inspect_pdf_text_layer(source)
        engine_name = routed_engine(normalized) if force_ocr or not layer.usable else "direct"
        console_encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(
            (
                "[OCR]\n"
                f"source: {source.name}\n"
                f"pages: {layer.page_count}\n"
                f"language: {normalized}\n"
                f"engine: {engine_name}"
            )
            .encode(console_encoding, errors="backslashreplace")
            .decode(console_encoding)
        )
        if metrics is not None:
            metrics.transition("ocr_engine")
        engine = self.engine_factory(engine_name)
        try:
            if type(engine) is NDLOCRLiteEngine:
                with _NDLOCR_LITE_LOCK:
                    result: OCRResult = engine.process(source, normalized, metrics=metrics)
            elif type(engine) is MistralOCREngine:
                with _MISTRAL_OCR_LOCK:
                    result = engine.process(
                        source, normalized, checkpoint_dir=output_dir / "mistral-chunks"
                    )
            else:
                result = engine.process(source, normalized)
        except FukamiruError as exc:
            if engine_name != "ndlocr_lite" or "ElementTree.ParseError" not in str(exc):
                raise
            # A malformed ALTO fragment from NDLOCR is an engine output failure.
            # Retry the same original pages through the independently configured OCR API.
            fallback = self.engine_factory("mistral")
            if type(fallback) is not MistralOCREngine:
                raise
            with _MISTRAL_OCR_LOCK:
                result = fallback.process(
                    source, normalized, checkpoint_dir=output_dir / "mistral-chunks"
                )
        if len(result.pages) != layer.page_count:
            raise FukamiruError(
                f"OCRのページ数が原本と一致しません: {layer.page_count} -> {len(result.pages)}"
            )
        if not any(page.text.strip() for page in result.pages):
            raise FukamiruError("OCR結果に本文がありません")
        if metrics is not None:
            metrics.transition("ocr_output_write")
        text = render_pages(result.pages, markdown=output_format == "md")
        _atomic_write(text_path, text)
        _atomic_write(
            json_path,
            json.dumps(result.as_dict(), ensure_ascii=False, indent=2) + "\n",
        )
        return {
            "status": "completed",
            "source": str(source),
            "language": normalized,
            "engine": result.engine,
            "page_count": len(result.pages),
            "text_path": str(text_path),
            "json_path": str(json_path),
        }


def render_pages(pages: tuple[OCRPage, ...], *, markdown: bool = False) -> str:
    return (
        "\n\n".join(
            (
                f"## Page {page.page_number}\n\n{page.text.rstrip()}"
                if markdown
                else f"--- PAGE {page.page_number} ---\n\n{page.text.rstrip()}"
            )
            for page in pages
        ).rstrip()
        + "\n"
    )


def load_ocr_pages(json_path: Path) -> list[str]:
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    pages = payload.get("pages")
    if not isinstance(pages, list):
        raise FukamiruError(f"OCR JSONにpagesがありません: {json_path}")
    return [str(page.get("text", "")) for page in pages if isinstance(page, dict)]


def _atomic_write(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise FukamiruError(f"OCR出力を書き込めません: {path}: {exc}") from exc


def _decode_job(row: Any) -> dict[str, Any]:
    value = dict(row)
    raw = value.pop("error_json", None)
    value["error"] = json.loads(raw) if raw else None
    raw_decision = value.pop("language_decision_json", None)
    value["language_decision"] = json.loads(raw_decision) if raw_decision else None
    return value


class DocumentOCRService:
    """Persist and execute per-document OCR jobs for workflow resumption."""

    def __init__(self, databases: DatabasePair, settings: Settings) -> None:
        self.databases = databases
        self.settings = settings

    @property
    def root(self) -> Path:
        return (self.settings.paths.ocr_root / "extracted_text").resolve()

    @staticmethod
    def recovery_pending(job: dict[str, Any]) -> bool:
        return bool(job.get("recovery_kind") and not job.get("input_pdf_sha256"))

    @staticmethod
    def language_retry_delay(job: dict[str, Any]) -> float | None:
        """Seconds until automatic image-language detection may run; None needs review."""
        if job["status"] != "needs_language" or job.get("language_origin") == "manual":
            return None
        decision = job.get("language_decision") or {}
        method = str(decision.get("method") or "")
        evidence = decision.get("evidence") or {}
        if method == "pdf_page_image_llm_pending":
            updated = datetime.fromisoformat(str(job["updated_at"]).replace("Z", "+00:00"))
            return max(0.0, 300.0 - (datetime.now(UTC) - updated).total_seconds())
        if method == "pdf_page_image_llm_rate_limited":
            retry_at = evidence.get("retry_after")
            if retry_at:
                due = datetime.fromisoformat(str(retry_at).replace("Z", "+00:00"))
                return max(0.0, (due - datetime.now(UTC)).total_seconds())
            return 0.0
        if method == "pdf_page_image_llm_failed":
            error = str(evidence.get("error") or "")
            return 0.0 if "429" in error or "Too Many Requests" in error else None
        if method.startswith("pdf_page_image_llm") or method == "unsupported_explicit_language":
            return None
        return 0.0

    def _reserve_recovery(
        self,
        *,
        run_id: str,
        source: Path,
        digest: str,
        item_id: str | None,
        kind: str,
        requested_language: str | None,
    ) -> dict[str, Any]:
        """Commit identity before native work; never expose unfinished input as queued."""
        now = utc_now()
        with self.databases.state.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM pdf_ocr_jobs WHERE run_id=? AND source_sha256=?", (run_id, digest)
            ).fetchone()
            if row is not None and (
                row["status"] in {"processing", "cancelled"}
                or (row["recovery_kind"] and row["input_pdf_sha256"])
            ):
                return _decode_job(row)
            if row is not None and row["recovery_kind"] and not row["input_pdf_sha256"]:
                return _decode_job(row)
            pending = json.dumps(
                asdict(
                    OCRLanguageDecision(
                        "und",
                        0.0,
                        "pdf_recovery_pending",
                        {"recovery_kind": kind, "threshold": 0.90},
                    )
                ),
                ensure_ascii=False,
            )
            job_id = str(row["id"]) if row is not None else str(uuid.uuid4())
            if row is None:
                connection.execute(
                    """INSERT INTO pdf_ocr_jobs(id,run_id,corpus_item_id,source_path,
                        source_sha256,source_language,status,force_ocr,language_origin,
                        language_confidence,language_decision_json,recovery_kind,created_at,updated_at)
                        VALUES (?,?,?,?,?,'und','needs_language',1,'unresolved',0,?,?,?,?)""",
                    (job_id, run_id, item_id, str(source), digest, pending, kind, now, now),
                )
            else:
                connection.execute(
                    """UPDATE pdf_ocr_jobs SET status='needs_language',force_ocr=1,recovery_kind=?,
                        input_pdf_path=NULL,input_pdf_sha256=NULL,recovery_manifest_path=NULL,
                        output_text_path=NULL,output_json_path=NULL,error_json=NULL,
                        completed_at=NULL,updated_at=? WHERE id=?""",
                    (kind, now, job_id),
                )
            requested = normalize_language(str(requested_language or ""))
            if requested != "und" and (row is None or row["language_origin"] != "manual"):
                connection.execute(
                    """UPDATE pdf_ocr_jobs SET source_language=?,engine=?,
                        language_origin='explicit',language_confidence=1.0,
                        language_decision_json=? WHERE id=?""",
                    (
                        requested,
                        routed_engine(requested),
                        json.dumps(
                            asdict(
                                OCRLanguageDecision(
                                    requested, 1.0, "explicit_request", {"threshold": 0.90}
                                )
                            )
                        ),
                        job_id,
                    ),
                )
            connection.commit()
            EventRepository(connection).append(
                run_id,
                "ocr.job.recovery_started",
                object_type="pdf_ocr_job",
                object_id=job_id,
                payload={
                    "recovery_kind": kind,
                    "status": "preparing",
                    "original_source_path": str(source),
                },
            )
            return _decode_job(
                connection.execute("SELECT * FROM pdf_ocr_jobs WHERE id=?", (job_id,)).fetchone()
            )

    def _resume_recovery(self, job: dict[str, Any]) -> dict[str, Any]:
        return self.enqueue(
            run_id=str(job["run_id"]),
            source_path=Path(str(job["source_path"])),
            source_sha256=str(job["source_sha256"]),
            source_language=(
                str(job["source_language"]) if job.get("language_origin") == "explicit" else "auto"
            ),
            corpus_item_id=job.get("corpus_item_id"),
            recovery_kind=job.get("recovery_kind"),
        )

    def enqueue(
        self,
        *,
        run_id: str,
        source_path: Path,
        source_sha256: str,
        source_language: str | None,
        corpus_item_id: str | None = None,
        force_ocr: bool = False,
        recovery_kind: str | None = None,
        progress: Any | None = None,
    ) -> dict[str, Any]:
        source = source_path.resolve()
        if not source.is_file() or source.suffix.casefold() != ".pdf":
            raise FukamiruError(f"既存のPDFを指定してください: {source}")
        if _sha256_file(source) != source_sha256:
            raise FukamiruError("OCR対象PDFが判定後に変更されています")
        with self.databases.state.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM pdf_ocr_jobs WHERE run_id = ? AND source_sha256 = ?",
                (run_id, source_sha256),
            ).fetchone()
        pending_recovery = existing is not None and self.recovery_pending(dict(existing))
        if pending_recovery:
            recovery_kind = str(existing["recovery_kind"])
        if (
            existing is not None
            and not pending_recovery
            and (
                not recovery_kind or existing["recovery_kind"] or existing["status"] == "cancelled"
            )
        ):
            return _decode_job(existing)
        if existing is not None and existing["status"] == "processing":
            raise FukamiruError("実行中のOCRジョブの入力PDFは変更できません")
        recovery = None
        if recovery_kind:
            existing = self._reserve_recovery(
                run_id=run_id,
                source=source,
                digest=source_sha256,
                item_id=corpus_item_id,
                kind=recovery_kind,
                requested_language=source_language,
            )
            if existing["status"] == "cancelled" or not self.recovery_pending(existing):
                return existing
            try:
                recovery = prepare_image_pdf(
                    source,
                    source_sha256,
                    root=self.settings.paths.ocr_root / "recovery_inputs",
                    kind=recovery_kind,
                    dpi=self.settings.ocr.ndlocr_pdf_render_dpi,
                    progress=progress,
                )
            except Exception as exc:
                with self.databases.state.connect() as connection:
                    connection.execute(
                        "UPDATE pdf_ocr_jobs SET status='failed',error_json=?,updated_at=? "
                        "WHERE id=? AND status!='cancelled' AND input_pdf_sha256 IS NULL",
                        (
                            json.dumps({"type": "PDFRecoveryFailed", "message": str(exc)}),
                            utc_now(),
                            existing["id"],
                        ),
                    )
                    connection.commit()
                raise
            force_ocr = True
        raw_language = str(source_language or "").strip()
        requested_language = normalize_language(raw_language)
        if requested_language != "und":
            decision = OCRLanguageDecision(
                requested_language,
                1.0,
                "explicit_request",
                {"requested_value": str(source_language), "threshold": 0.90},
            )
            language_origin = "explicit"
        elif raw_language.casefold() not in {"", "auto"}:
            decision = OCRLanguageDecision(
                "und",
                0.0,
                "unsupported_explicit_language",
                {"requested_value": raw_language, "threshold": 0.90},
            )
            language_origin = "unresolved"
        else:
            decision = detect_pdf_language(source)
            language_origin = "detected" if decision.routable else "unresolved"
        if existing is not None and existing["language_origin"] == "manual":
            decision = OCRLanguageDecision(
                str(existing["source_language"]),
                1.0,
                "manual_assignment",
                {"previous_job_id": str(existing["id"]), "threshold": 0.90},
            )
            language_origin = "manual"
        language = decision.language if decision.routable else "und"
        status = "queued" if decision.routable else "needs_language"
        engine = routed_engine(language) if language != "und" else None
        now = utc_now()
        input_source = Path(recovery["input_pdf_path"]) if recovery else source
        page_count = recovery["page_count"] if recovery else len(PdfReader(input_source).pages)
        recovery_values = (
            str(input_source) if recovery else None,
            recovery["input_pdf_sha256"] if recovery else None,
            recovery_kind,
            recovery["manifest_path"] if recovery else None,
        )
        with self.databases.state.connect() as connection:
            job_id = str(existing["id"]) if existing is not None else str(uuid.uuid4())
            # Serialize the unique identity check with insertion/upgrading.
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM pdf_ocr_jobs WHERE run_id=? AND source_sha256=?",
                (run_id, source_sha256),
            ).fetchone()
            if current is not None and (
                existing is None
                or current["status"] == "cancelled"
                or (current["recovery_kind"] and current["input_pdf_sha256"])
            ):
                return _decode_job(current)
            if current is not None and current["language_origin"] == "manual":
                language = str(current["source_language"])
                engine = routed_engine(language)
                language_origin = "manual"
                decision = OCRLanguageDecision(
                    language, 1.0, "manual_assignment", {"threshold": 0.90}
                )
                status = "queued"
            if existing is not None:
                connection.execute(
                    """UPDATE pdf_ocr_jobs SET input_pdf_path=?,input_pdf_sha256=?,
                        recovery_kind=?,recovery_manifest_path=?,force_ocr=1,
                        source_language=?,engine=?,status=?,source_page_count=?,
                        language_origin=?,language_confidence=?,language_decision_json=?,
                        output_text_path=NULL,output_json_path=NULL,error_json=NULL,
                        completed_at=NULL,updated_at=? WHERE id=? AND status!='processing'""",
                    (
                        *recovery_values,
                        language,
                        engine,
                        status,
                        page_count,
                        language_origin,
                        decision.confidence,
                        json.dumps(asdict(decision), ensure_ascii=False),
                        now,
                        job_id,
                    ),
                )
                if connection.execute("SELECT changes()").fetchone()[0] != 1:
                    raise FukamiruError("OCRジョブが変更されたため復帰入力を更新できません")
            else:
                connection.execute(
                    """
                    INSERT INTO pdf_ocr_jobs(
                        id,run_id,corpus_item_id,source_path,source_sha256,source_language,
                        engine,status,force_ocr,source_page_count,language_origin,
                        language_confidence,language_decision_json,created_at,updated_at,
                        input_pdf_path,input_pdf_sha256,recovery_kind,recovery_manifest_path
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        job_id,
                        run_id,
                        corpus_item_id,
                        str(source),
                        source_sha256,
                        language,
                        engine,
                        status,
                        int(force_ocr),
                        page_count,
                        language_origin,
                        decision.confidence,
                        json.dumps(asdict(decision), ensure_ascii=False, separators=(",", ":")),
                        now,
                        now,
                        *recovery_values,
                    ),
                )
            connection.commit()
            EventRepository(connection).append(
                run_id,
                "ocr.job.recovery_prepared" if recovery else "ocr.job.created",
                object_type="pdf_ocr_job",
                object_id=job_id,
                payload={
                    "status": status,
                    "language": language,
                    "engine": engine,
                    "language_origin": language_origin,
                    "language_confidence": decision.confidence,
                    "language_method": decision.method,
                    "recovery_kind": recovery_kind,
                    "input_pdf_path": str(input_source),
                    "original_source_path": str(source),
                },
            )
            row = connection.execute(
                "SELECT * FROM pdf_ocr_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return _decode_job(row)

    def assign_language(self, job_id: str, language: str) -> dict[str, Any]:
        normalized = normalize_language(language)
        engine = routed_engine(normalized)
        now = utc_now()
        with self.databases.state.connect() as connection:
            row = connection.execute(
                "SELECT * FROM pdf_ocr_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise FukamiruError(f"OCRジョブが見つかりません: {job_id}")
            if str(row["status"]) in {"processing", "completed", "cancelled"}:
                raise FukamiruError("現在の状態ではOCR言語を変更できません")
            connection.execute(
                """
                UPDATE pdf_ocr_jobs SET source_language=?,engine=?,status=?,
                    output_text_path=NULL,output_json_path=NULL,error_json=NULL,
                    completed_at=NULL,language_origin='manual',language_confidence=1.0,
                    language_decision_json=?,updated_at=? WHERE id=?
                """,
                (
                    normalized,
                    engine,
                    "needs_language" if self.recovery_pending(dict(row)) else "queued",
                    json.dumps(
                        asdict(
                            OCRLanguageDecision(
                                normalized,
                                1.0,
                                "manual_assignment",
                                {"threshold": 0.90},
                            )
                        ),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    now,
                    job_id,
                ),
            )
            connection.commit()
            run_id = str(row["run_id"])
            EventRepository(connection).append(
                run_id,
                "ocr.job.language_assigned",
                object_type="pdf_ocr_job",
                object_id=job_id,
                payload={
                    "language": normalized,
                    "engine": engine,
                    "language_origin": "manual",
                    "language_confidence": 1.0,
                    "language_method": "manual_assignment",
                },
            )
        self._resume_language_wait(run_id)
        return self.job(job_id)

    def ensure_markdown(self, job_id: str) -> dict[str, Any]:
        """Keep a page-marked Markdown artifact for a completed OCR job."""
        from fukamiru.ocr.models import OCRPage

        job = self.job(job_id)
        if job["status"] != "completed":
            return job
        current = Path(str(job["output_text_path"]))
        if current.suffix.casefold() == ".md" and current.is_file():
            return job
        json_path = Path(str(job["output_json_path"]))
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        pages = tuple(
            OCRPage(page_number=int(page["page_number"]), text=str(page["text"]))
            for page in payload["pages"]
        )
        markdown_path = current.with_suffix(".md")
        _atomic_write(markdown_path, render_pages(pages, markdown=True))
        with self.databases.state.connect() as connection:
            connection.execute(
                "UPDATE pdf_ocr_jobs SET output_text_path=?,updated_at=? WHERE id=?",
                (str(markdown_path), utc_now(), job_id),
            )
            connection.commit()
        return self.job(job_id)

    def process_job(self, job_id: str, *, output_format: str | None = None) -> dict[str, Any]:
        job = self.job(job_id)
        if self.recovery_pending(job) and job["status"] not in {"cancelled", "completed"}:
            job = self._resume_recovery(job)
        if job["status"] == "completed":
            return self.ensure_markdown(job_id) if output_format == "md" else job
        if job["status"] == "failed":
            with self.databases.state.connect() as connection:
                connection.execute(
                    "UPDATE pdf_ocr_jobs SET status='queued',error_json=NULL,updated_at=? "
                    "WHERE id=? AND status='failed'",
                    (utc_now(), job_id),
                )
                connection.commit()
            job = self.job(job_id)
        if job["status"] != "queued":
            raise FukamiruError(f"OCRジョブを実行できない状態です: {job['status']}")
        now = utc_now()
        with self.databases.state.connect() as connection:
            claimed = connection.execute(
                "UPDATE pdf_ocr_jobs SET status='processing',attempt=attempt+1,"
                "started_at=COALESCE(started_at,?),updated_at=? "
                "WHERE id=? AND status='queued'",
                (now, now, job_id),
            )
            if claimed.rowcount != 1:
                connection.rollback()
                raise FukamiruError("OCRジョブは別のワーカーが取得しました")
            connection.commit()
        output_dir = self.root / job_id
        metrics = StageMetrics("ocr_input_verification")
        outcome = "failed"
        try:
            source = Path(str(job["source_path"]))
            if _sha256_file(source) != str(job["source_sha256"]):
                raise FukamiruError("OCR対象PDFがジョブ登録後に変更されています")
            input_source = Path(str(job.get("input_pdf_path") or source))
            input_sha256 = str(job.get("input_pdf_sha256") or job["source_sha256"])
            if _sha256_file(input_source) != input_sha256:
                raise FukamiruError("OCR用複製PDFがジョブ登録後に変更されています")
            result = PDFOCRService(self.settings).process_file(
                input_source,
                language=str(job["source_language"]),
                output_dir=output_dir,
                force_ocr=bool(job["force_ocr"]),
                overwrite=bool(job["attempt"]),
                output_format=output_format or self.settings.ocr.output_format,
                metrics=metrics,
            )
            metrics.transition("ocr_output_verification")
            if result["status"] == "skipped":
                if not Path(result["json_path"]).is_file():
                    raise FukamiruError("既存OCR出力のJSONがありません")
                payload = json.loads(Path(result["json_path"]).read_text(encoding="utf-8"))
                result.update(engine=payload.get("engine"), page_count=payload.get("page_count"))
            if _sha256_file(source) != str(job["source_sha256"]):
                raise FukamiruError("OCR実行中に原本PDFが変更されました")
            if _sha256_file(input_source) != input_sha256:
                raise FukamiruError("OCR実行中に複製PDFが変更されました")
            if job.get("recovery_kind"):
                payload = json.loads(Path(result["json_path"]).read_text(encoding="utf-8"))
                payload["original_source_path"] = str(source)
                payload["original_source_sha256"] = job["source_sha256"]
                payload["recovery_kind"] = job["recovery_kind"]
                payload["recovery_manifest_path"] = job["recovery_manifest_path"]
                _atomic_write(
                    Path(result["json_path"]), json.dumps(payload, ensure_ascii=False, indent=2)
                )
            completed = utc_now()
            with self.databases.state.connect() as connection:
                connection.execute(
                    """
                    UPDATE pdf_ocr_jobs SET status='completed',engine=?,output_text_path=?,
                        output_json_path=?,output_page_count=?,error_json=NULL,
                        completed_at=?,updated_at=? WHERE id=?
                    """,
                    (
                        result.get("engine"),
                        result["text_path"],
                        result["json_path"],
                        result.get("page_count"),
                        completed,
                        completed,
                        job_id,
                    ),
                )
                connection.commit()
                EventRepository(connection).append(
                    str(job["run_id"]),
                    "ocr.job.completed",
                    object_type="pdf_ocr_job",
                    object_id=job_id,
                    payload={"engine": result.get("engine")},
                )
            outcome = "completed"
        except Exception as exc:
            failed = utc_now()
            with self.databases.state.connect() as connection:
                connection.execute(
                    "UPDATE pdf_ocr_jobs SET status='failed',error_json=?,completed_at=?,"
                    "updated_at=? WHERE id=?",
                    (
                        json.dumps(
                            {"type": exc.__class__.__name__, "message": str(exc)},
                            ensure_ascii=False,
                        ),
                        failed,
                        failed,
                        job_id,
                    ),
                )
                connection.commit()
            raise
        finally:
            timing = metrics.finish(outcome=outcome)
            try:
                with self.databases.state.connect(busy_timeout_ms=100) as connection:
                    EventRepository(connection).append(
                        str(job["run_id"]),
                        "ocr.job.stage_metrics",
                        object_type="pdf_ocr_job",
                        object_id=job_id,
                        payload={
                            "attempt": int(job["attempt"]) + 1,
                            "engine": (
                                result.get("engine")
                                if outcome == "completed" else job["engine"]
                            ),
                            "page_count": (
                                result.get("page_count")
                                if outcome == "completed" else job["source_page_count"]
                            ),
                            "timing": timing,
                        },
                    )
            except Exception:
                pass
        return self.job(job_id)

    def process_run_jobs(
        self, run_id: str, *, output_format: str | None = None
    ) -> list[dict[str, Any]]:
        with self.databases.state.connect() as connection:
            node = connection.execute(
                "SELECT started_at FROM node_runs WHERE run_id=? "
                "AND node_id='run_ocr_jobs' AND status='running' "
                "ORDER BY started_at DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            if node is not None:
                interrupted = connection.execute(
                    "SELECT id FROM pdf_ocr_jobs WHERE run_id=? AND status='processing' "
                    "AND updated_at < ?",
                    (run_id, str(node["started_at"])),
                ).fetchall()
                for job in interrupted:
                    job_id = str(job["id"])
                    connection.execute(
                        "UPDATE pdf_ocr_jobs SET status='queued',updated_at=? "
                        "WHERE id=? AND status='processing'",
                        (utc_now(), job_id),
                    )
                    EventRepository(connection).append(
                        run_id,
                        "ocr.job.requeued_after_restart",
                        object_type="pdf_ocr_job",
                        object_id=job_id,
                        payload={"reason": "interrupted_process"},
                    )
                connection.commit()
        lanes: dict[str, Queue[str | None]] = {
            "mistral": Queue(),
            "ndlocr_lite": Queue(),
        }

        def process_lane(lane: Queue[str | None]) -> dict[str, dict[str, Any]]:
            outcomes = {}
            while (job_id := lane.get()) is not None:
                try:
                    outcomes[job_id] = self.process_job(job_id, output_format=output_format)
                except Exception:
                    outcomes[job_id] = self.job(job_id)
            return outcomes

        outcomes: dict[str, dict[str, Any]] = {}
        prepared = []
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="corpus-ocr") as pool:
            futures = [pool.submit(process_lane, lane) for lane in lanes.values()]
            try:
                for job in self.jobs(run_id=run_id):
                    if job["status"] == "needs_language":
                        job = self.resolve_language_from_images(str(job["id"]))
                    if job["status"] == "completed" and output_format == "md":
                        job = self.ensure_markdown(str(job["id"]))
                    if job["status"] == "queued":
                        job_id = str(job["id"])
                        engine = str(job.get("engine") or "")
                        if engine not in lanes:
                            with suppress(FukamiruError):
                                engine = routed_engine(str(job.get("source_language") or ""))
                        lane = lanes.get(engine)
                        if lane is not None:
                            lane.put(job_id)
                        else:
                            try:
                                outcomes[job_id] = self.process_job(
                                    job_id, output_format=output_format
                                )
                            except Exception:
                                outcomes[job_id] = self.job(job_id)
                    prepared.append(job)
            finally:
                for lane in lanes.values():
                    lane.put(None)
            for future in futures:
                outcomes.update(future.result())
        return [outcomes.get(str(job["id"]), job) for job in prepared]

    def resolve_language_from_images(self, job_id: str) -> dict[str, Any]:
        """Try visual LLM language identification once without overriding a human choice."""
        from fukamiru.ocr.language_detection import identify_page_image_language

        job = self.job(job_id)
        if self.recovery_pending(job) and job["status"] == "needs_language":
            job = self._resume_recovery(job)
        if not self.settings.ocr.vision_language_detection or job["status"] != "needs_language":
            return job
        with self.databases.state.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM pdf_ocr_jobs WHERE id=?", (job_id,)).fetchone()
            prior = json.loads(row["language_decision_json"] or "{}")
            retry_delay = self.language_retry_delay(_decode_job(row))
            if retry_delay is None or retry_delay > 0:
                return _decode_job(row)
            previous_evidence = prior.get("evidence") or {}
            retry_attempts = int(previous_evidence.get("retry_attempts") or 0)
            original_decision = previous_evidence.get("prior_decision", prior)
            pending = json.dumps(
                asdict(
                    OCRLanguageDecision(
                        "und",
                        0.0,
                        "pdf_page_image_llm_pending",
                        {"prior_decision": original_decision, "threshold": 0.90,
                         "attempted": True, "retry_attempts": retry_attempts},
                    )
                ),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            connection.execute(
                "UPDATE pdf_ocr_jobs SET language_decision_json=?,updated_at=? WHERE id=?",
                (pending, utc_now(), job_id),
            )
            connection.commit()
            EventRepository(connection).append(
                str(row["run_id"]),
                "ocr.language.vision_started",
                object_type="pdf_ocr_job",
                object_id=job_id,
                payload={"model": self.settings.translation.manuscript_codex_model},
            )
            job = _decode_job(row)
        input_source = Path(str(job.get("input_pdf_path") or job["source_path"]))
        expected_sha = str(job.get("input_pdf_sha256") or job["source_sha256"])
        try:
            if (
                _sha256_file(input_source) != expected_sha
                or _sha256_file(Path(job["source_path"])) != job["source_sha256"]
            ):
                raise FukamiruError("言語判定前にOCR対象PDFが変更されています")
            response = identify_page_image_language(
                self.settings,
                input_source,
                root=self.root / job_id / "language-images",
            )
            if (
                _sha256_file(input_source) != expected_sha
                or _sha256_file(Path(job["source_path"])) != job["source_sha256"]
            ):
                raise FukamiruError("言語判定中にOCR対象PDFが変更されました")
            confidence = float(response["confidence"])
            if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
                raise FukamiruError("LLM言語判定の確信度が0〜1の範囲外です")
            language = normalize_language(str(response["language"]))
            pages = response["page_languages"]
            supported_pages = sum(page["language"] == language for page in pages)
            eligible = (
                response["readable"]
                and not response["mixed_languages"]
                and supported_pages >= max(1, (len(pages) + 1) // 2)
            )
            decision = OCRLanguageDecision(
                language if eligible else "und",
                confidence,
                "pdf_page_image_llm",
                {"prior_decision": original_decision, "threshold": 0.90,
                 "response": response},
            )
            event_type = "ocr.language.vision_completed"
        except Exception as exc:
            rate_limited = "429" in str(exc) or "Too Many Requests" in str(exc)
            retry_attempts += 1 if rate_limited else 0
            retry_seconds = min(30 * 2 ** min(retry_attempts - 1, 5), 900) if rate_limited else 0
            retry_after = (
                (datetime.now(UTC) + timedelta(seconds=retry_seconds))
                .isoformat(timespec="seconds").replace("+00:00", "Z")
                if rate_limited else None
            )
            decision = OCRLanguageDecision(
                "und",
                0.0,
                "pdf_page_image_llm_rate_limited" if rate_limited else "pdf_page_image_llm_failed",
                {
                    "prior_decision": original_decision,
                    "threshold": 0.90,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:1000],
                    "retry_attempts": retry_attempts,
                    "retry_after": retry_after,
                },
            )
            event_type = (
                "ocr.language.vision_rate_limited"
                if rate_limited else "ocr.language.vision_failed"
            )
        language = decision.language if decision.routable else "und"
        status = "queued" if decision.routable else "needs_language"
        with self.databases.state.connect() as connection:
            connection.execute(
                """UPDATE pdf_ocr_jobs SET source_language=?,engine=?,status=?,language_origin=?,
                    language_confidence=?,language_decision_json=?,updated_at=?
                    WHERE id=? AND status='needs_language' AND language_decision_json=?""",
                (
                    language,
                    routed_engine(language) if decision.routable else None,
                    status,
                    "detected" if decision.routable else "unresolved",
                    decision.confidence,
                    json.dumps(asdict(decision), ensure_ascii=False, separators=(",", ":")),
                    utc_now(),
                    job_id,
                    pending,
                ),
            )
            changed = connection.execute("SELECT changes()").fetchone()[0]
            connection.commit()
            if changed:
                EventRepository(connection).append(
                    str(job["run_id"]),
                    event_type,
                    object_type="pdf_ocr_job",
                    object_id=job_id,
                    payload={
                        "language": language,
                        "status": status,
                        "confidence": decision.confidence,
                        "method": decision.method,
                        "retry_after": decision.evidence.get("retry_after"),
                    },
                )
        return self.job(job_id)

    def jobs(self, *, run_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM pdf_ocr_jobs"
        params: tuple[Any, ...] = ()
        if run_id:
            query += " WHERE run_id=?"
            params = (run_id,)
        query += " ORDER BY created_at,id"
        with self.databases.state.connect() as connection:
            return [_decode_job(row) for row in connection.execute(query, params)]

    def job(self, job_id: str) -> dict[str, Any]:
        with self.databases.state.connect() as connection:
            row = connection.execute("SELECT * FROM pdf_ocr_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise FukamiruError(f"OCRジョブが見つかりません: {job_id}")
        return _decode_job(row)

    def _resume_language_wait(self, run_id: str) -> None:
        with self.databases.state.connect() as connection:
            pending = int(
                connection.execute(
                    "SELECT COUNT(*) FROM pdf_ocr_jobs WHERE run_id=? AND status='needs_language'",
                    (run_id,),
                ).fetchone()[0]
            )
            if pending:
                return
            nodes = connection.execute(
                "SELECT id,input_json,output_json FROM node_runs "
                "WHERE run_id=? AND status='waiting_human' ORDER BY rowid",
                (run_id,),
            ).fetchall()
            resumable = []
            for node in nodes:
                context = json.loads(str(node["output_json"] or node["input_json"] or "{}"))
                if context.get("ocr_wait_kind") == "ocr_language":
                    resumable.append((node, context))
            if len(resumable) != 1:
                return
            node, context = resumable[0]
            context.pop("ocr_wait_kind", None)
            encoded = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE node_runs SET status='queued',input_json=?,output_json=NULL,"
                "started_at=NULL,completed_at=NULL WHERE id=? AND status='waiting_human'",
                (encoded, str(node["id"])),
            )
            connection.execute(
                "UPDATE runs SET status='queued',output_json=NULL "
                "WHERE id=? AND status='waiting_human'",
                (run_id,),
            )
            connection.commit()
