"""Managed corpus discovery and resumable batch ingestion."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from itertools import chain
from pathlib import Path
from threading import Lock
from typing import Any

from fukamiru.config import Settings
from fukamiru.errors import EmptyEmbeddingOffsetsError, FukamiruError
from fukamiru.knowledge.pdf_layout import PDFLayoutAnalyzer
from fukamiru.knowledge.service import SUPPORTED_SUFFIXES, IngestionService, utc_now
from fukamiru.ocr.pdf_recovery import is_encrypted_pdf_error, is_pdf_layout_error
from fukamiru.stage_metrics import StageMetrics
from fukamiru.storage.database import DatabasePair, retry_sqlite_operation
from fukamiru.storage.events import EventRepository

_INGEST_CACHE_KIB = 64 * 1024


class CorpusSyncService:
    def __init__(self, databases: DatabasePair, settings: Settings) -> None:
        self.databases = databases
        self.settings = settings
        self._asset_locks_guard = Lock()
        self._asset_locks: dict[str, Lock] = {}

    @property
    def managed_root(self) -> Path:
        return (self.settings.paths.corpus_root / "managed_sources").resolve()

    @property
    def staging_root(self) -> Path:
        return (self.settings.paths.corpus_root / "import_staging").resolve()

    @property
    def default_root(self) -> Path:
        return self.settings.paths.corpus_root.resolve()

    def resolve_root(self, requested_root: str | None = None) -> Path:
        root = Path(requested_root).expanduser().resolve() if requested_root else self.default_root
        allowed = (self.managed_root, self.staging_root)
        if any(not base.is_relative_to(self.default_root) for base in allowed):
            raise FukamiruError("Corpus source directories must remain inside the corpus root")
        if root != self.default_root and not any(root.is_relative_to(base) for base in allowed):
            raise FukamiruError(
                "Corpus sync root must be the corpus root, managed_sources, "
                f"import_staging, or their subdirectories: {self.default_root}"
            )
        if not root.is_dir():
            raise FukamiruError(f"Corpus source directory does not exist: {root}")
        return root

    def source_roots(self, root: Path) -> list[Path]:
        """The corpus parent selects only the two explicit input directories."""
        if root == self.default_root:
            roots = (self.managed_root, self.staging_root)
            if any(not path.is_relative_to(self.default_root) for path in roots):
                raise FukamiruError(
                    "Corpus source directories must remain inside the corpus root"
                )
            return [path for path in roots if path.is_dir()]
        return [root]

    def _source_files(self, root: Path) -> list[Path]:
        return sorted(
            {
                resolved
                for base in self.source_roots(root)
                for path in base.rglob("*")
                if path.is_file()
                and not path.name.startswith("._")
                and (resolved := path.resolve()).is_relative_to(base)
                and resolved.suffix.lower() in SUPPORTED_SUFFIXES
                and not self._empty_pdf(resolved)
            }
        )

    @staticmethod
    def _empty_pdf(path: Path) -> bool:
        if path.suffix.casefold() != ".pdf":
            return False
        try:
            return path.stat().st_size == 0
        except OSError:
            # Let ingestion report inaccessible files instead of hiding them.
            return False

    def directories(self) -> list[str]:
        root = self.resolve_root()
        directories = [root]
        directories.extend(
            resolved
            for base in self.source_roots(root)
            for path in chain((base,), base.rglob("*"))
            if path.is_dir() and (resolved := path.resolve()).is_relative_to(base)
        )
        return [str(path) for path in sorted(set(directories))]

    def scan(self, run_id: str, requested_root: str | None = None) -> dict[str, Any]:
        root = self.resolve_root(requested_root)
        files = self._source_files(root)
        batch_id = str(uuid.uuid4())
        now = utc_now()
        with self.databases.state.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO corpus_sync_batches(
                    id, run_id, root_path, status, discovered_count, created_at, updated_at
                ) VALUES (?, ?, ?, 'scanning', ?, ?, ?)
                """,
                (batch_id, run_id, str(root), len(files), now, now),
            )
            connection.executemany(
                """
                INSERT INTO corpus_sync_items(
                    id, batch_id, source_path, relative_path, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'discovered', ?, ?)
                """,
                [
                    (
                        str(uuid.uuid4()),
                        batch_id,
                        str(path),
                        path.relative_to(root).as_posix(),
                        now,
                        now,
                    )
                    for path in files
                ],
            )
            connection.execute(
                "UPDATE corpus_sync_batches SET status = 'ingesting', updated_at = ? WHERE id = ?",
                (now, batch_id),
            )
            connection.commit()
        for base in self.source_roots(root):
            self._mark_locations(base, [path for path in files if path.is_relative_to(base)])
        return {
            "corpus_batch_id": batch_id,
            "corpus_root": str(root),
            "corpus_source_roots": [str(base) for base in self.source_roots(root)],
            "discovered_count": len(files),
        }

    def include_import_staging(self, batch_id: str) -> dict[str, Any]:
        """Extend a full managed batch while its ingestion workers are stopped.

        Preserve the original Run input and existing item identities. The batch's
        current scope and an additive event record the explicitly requested change.
        Resuming ingest_ready_sources reloads the expanded list of pending items.
        """
        staging = self.resolve_root(str(self.staging_root))
        files = self._source_files(staging)
        now = utc_now()
        with self.databases.state.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            batch = connection.execute(
                """
                SELECT b.*, r.status AS run_status, r.workflow_name
                FROM corpus_sync_batches b JOIN runs r ON r.id = b.run_id
                WHERE b.id = ?
                """,
                (batch_id,),
            ).fetchone()
            if batch is None:
                raise FukamiruError(f"Unknown corpus sync batch: {batch_id}")
            if (
                batch["workflow_name"] != "corpus_sync"
                or batch["status"] != "ingesting"
                or batch["run_status"] not in {"queued", "running"}
            ):
                raise FukamiruError("Only an active ingestion batch can include import_staging")
            ingestion = connection.execute(
                """
                SELECT id FROM node_runs WHERE run_id = ?
                AND node_id = 'ingest_ready_sources' AND status = 'running'
                """,
                (batch["run_id"],),
            ).fetchone()
            if ingestion is None:
                raise FukamiruError("Stop workers during ingest_ready_sources before expansion")
            previous_root = Path(str(batch["root_path"])).resolve()
            if previous_root not in {self.managed_root, self.default_root}:
                raise FukamiruError("Staging expansion requires a full managed corpus batch")
            existing = {
                Path(str(row["source_path"])).resolve()
                for row in connection.execute(
                    "SELECT source_path FROM corpus_sync_items WHERE batch_id = ?", (batch_id,)
                )
            }
            added = [path for path in files if path not in existing]
            connection.executemany(
                """
                INSERT INTO corpus_sync_items(
                    id, batch_id, source_path, relative_path, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'discovered', ?, ?)
                """,
                [
                    (
                        str(uuid.uuid4()),
                        batch_id,
                        str(path),
                        path.relative_to(self.default_root).as_posix(),
                        now,
                        now,
                    )
                    for path in added
                ],
            )
            total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM corpus_sync_items "
                    "WHERE batch_id = ? AND skip_reason IS NULL", (batch_id,)
                ).fetchone()[0]
            )
            connection.execute(
                """
                UPDATE corpus_sync_batches
                SET root_path = ?, discovered_count = ?, updated_at = ? WHERE id = ?
                """,
                (str(self.default_root), total, now, batch_id),
            )
            if added or previous_root != self.default_root:
                EventRepository(connection).append(
                    str(batch["run_id"]),
                    "corpus.scope.expanded",
                    object_type="corpus_sync_batch",
                    object_id=batch_id,
                    payload={
                        "previous_root_path": str(previous_root),
                        "root_path": str(self.default_root),
                        "source_roots": [str(self.managed_root), str(staging)],
                        "added_root": str(staging),
                        "added_count": len(added),
                        "discovered_count": total,
                    },
                )
            connection.commit()
        self._mark_locations(staging, files)
        return {
            "corpus_batch_id": batch_id,
            "added_count": len(added),
            "discovered_count": total,
            "corpus_source_roots": [str(self.managed_root), str(staging)],
        }

    def skip_unusable_sources(self, batch_id: str) -> dict[str, Any]:
        """Exclude empty PDFs and AppleDouble sidecars without deleting Item history.

        Call while ingestion workers are stopped. The skipped Item retains its
        original status and error; skip_reason is the effective status override.
        """
        with self.databases.state.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")

            def append_event(run_id: str, event_type: str, object_type: str,
                             object_id: str, payload: dict[str, Any]) -> None:
                connection.execute(
                    "INSERT INTO event_log(run_id,event_type,object_type,object_id,"
                    "payload_json,created_at) VALUES (?,?,?,?,?,?)",
                    (run_id, event_type, object_type, object_id,
                     json.dumps(payload, ensure_ascii=False, separators=(",", ":")), now),
                )

            batch = connection.execute(
                "SELECT b.*, r.status AS run_status FROM corpus_sync_batches b "
                "JOIN runs r ON r.id=b.run_id WHERE b.id=?", (batch_id,)
            ).fetchone()
            if batch is None or batch["status"] != "ingesting" or batch["run_status"] != "running":
                raise FukamiruError("Skipping sources requires an active ingestion batch")
            rows = connection.execute(
                "SELECT id,source_path,status,error_json FROM corpus_sync_items "
                "WHERE batch_id=? AND status='failed' AND skip_reason IS NULL", (batch_id,)
            ).fetchall()
            skipped: list[dict[str, str]] = []
            now = utc_now()
            for row in rows:
                path = Path(str(row["source_path"]))
                if path.suffix.casefold() != ".pdf":
                    continue
                if path.name.startswith("._"):
                    sibling = path.with_name(path.name[2:])
                    try:
                        sibling_present = sibling.is_file()
                    except OSError:
                        sibling_present = False
                    if not sibling_present:
                        continue
                    reason = "appledouble_sidecar"
                elif self._empty_pdf(path):
                    reason = "zero_byte_pdf"
                else:
                    continue
                connection.execute(
                    "UPDATE corpus_sync_items SET skip_reason=?,skipped_at=?,updated_at=? "
                    "WHERE id=? AND skip_reason IS NULL",
                    (reason, now, now, str(row["id"])),
                )
                payload = {
                    "source_path": str(path),
                    "reason": reason,
                    "previous_status": str(row["status"]),
                    "previous_error": json.loads(row["error_json"] or "{}"),
                }
                append_event(str(batch["run_id"]), "corpus.item.skipped",
                             "corpus_sync_item", str(row["id"]), payload)
                skipped.append({"id": str(row["id"]), "reason": reason})
            total = int(connection.execute(
                "SELECT COUNT(*) FROM corpus_sync_items "
                "WHERE batch_id=? AND skip_reason IS NULL", (batch_id,)
            ).fetchone()[0])
            failed = int(connection.execute(
                "SELECT COUNT(*) FROM corpus_sync_items "
                "WHERE batch_id=? AND status='failed' AND skip_reason IS NULL", (batch_id,)
            ).fetchone()[0])
            connection.execute(
                "UPDATE corpus_sync_batches SET discovered_count=?,failed_count=?,updated_at=? "
                "WHERE id=?", (total, failed, now, batch_id),
            )
            if skipped:
                append_event(str(batch["run_id"]), "corpus.scope.skipped",
                             "corpus_sync_batch", batch_id,
                             {"items": skipped, "skipped_count": len(skipped),
                              "discovered_count": total})
            connection.commit()
        return {"skipped": skipped, "discovered_count": total, "failed_count": failed}

    def _mark_locations(self, root: Path, files: list[Path]) -> None:
        current_paths = {str(path) for path in files}
        with self.databases.knowledge.connect() as connection:
            rows = connection.execute("SELECT id, path FROM source_locations").fetchall()
            managed_ids = [
                int(row["id"])
                for row in rows
                if Path(str(row["path"])) == root or Path(str(row["path"])).is_relative_to(root)
            ]
            if managed_ids:
                connection.executemany(
                    "UPDATE source_locations SET is_available = ? WHERE id = ?",
                    [
                        (int(str(row["path"]) in current_paths), int(row["id"]))
                        for row in rows
                        if int(row["id"]) in managed_ids
                    ],
                )
            connection.commit()

    def ingest(self, run_id: str, batch_id: str) -> dict[str, Any]:
        with self.databases.state.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, source_path FROM corpus_sync_items
                WHERE batch_id = ? AND skip_reason IS NULL
                  AND status IN ('discovered', 'processing')
                ORDER BY relative_path
                """,
                (batch_id,),
            ).fetchall()
        for position, row in enumerate(rows, start=1):
            self._set_item(row["id"], "processing")
            try:
                with self.databases.knowledge.connect() as connection:
                    result = IngestionService(connection, self.settings).ingest_path(
                        str(row["source_path"]),
                        source_language="auto",
                        material_role="unknown",
                        permission="unknown",
                    )
                status = "reused" if result.get("reused") else "ready"
                self._set_item(
                    row["id"],
                    status,
                    source_asset_id=result.get("source_asset_id"),
                    document_unit_id=result.get("document_unit_id"),
                )
                payload = {
                    "position": position,
                    "total": len(rows),
                    "path": row["source_path"],
                    "status": status,
                }
            except Exception as exc:
                message = str(exc) or exc.__class__.__name__
                status = "ocr_required" if "OCR_REQUIRED" in message else "failed"
                self._set_item(
                    row["id"],
                    status,
                    error={"type": exc.__class__.__name__, "message": message},
                )
                payload = {
                    "position": position,
                    "total": len(rows),
                    "path": row["source_path"],
                    "status": status,
                    "message": message,
                }
            payload.update(self.refresh_counts(batch_id))
            with self.databases.state.connect() as connection:
                EventRepository(connection).append(
                    run_id,
                    "corpus.item.updated",
                    object_type="corpus_sync_item",
                    object_id=row["id"],
                    payload=payload,
                )
        summary = self.refresh_counts(batch_id)
        with self.databases.state.connect() as connection:
            connection.execute(
                "UPDATE corpus_sync_batches SET status = 'indexing', updated_at = ? WHERE id = ?",
                (utc_now(), batch_id),
            )
            connection.commit()
        return summary

    def ingest_ready_sources(self, run_id: str, batch_id: str) -> dict[str, Any]:
        """Ingest sources while OCR returns Markdown to the same item queue."""
        from fukamiru.ocr.service import DocumentOCRService, routed_engine

        ocr_service = DocumentOCRService(self.databases, self.settings)
        with self.databases.state.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, source_path, status, error_json FROM corpus_sync_items
                WHERE batch_id = ? AND skip_reason IS NULL ORDER BY relative_path
                """,
                (batch_id,),
            ).fetchall()
            recovered_items = {
                str(row[0])
                for row in connection.execute(
                    "SELECT corpus_item_id FROM pdf_ocr_jobs "
                    "WHERE run_id=? AND recovery_kind IS NOT NULL",
                    (run_id,),
                )
            }
            jobs = {
                str(job["id"]): dict(job)
                for job in connection.execute(
                    "SELECT id,corpus_item_id,status,engine,source_language,"
                    "source_page_count,attempt,error_json,"
                    "recovery_kind,input_pdf_sha256 "
                    "FROM pdf_ocr_jobs "
                    "WHERE run_id=?",
                    (run_id,),
                )
            }
            node = connection.execute(
                "SELECT started_at FROM node_runs WHERE run_id=? "
                "AND node_id='ingest_ready_sources' AND status='running' "
                "ORDER BY started_at DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            if node is not None:
                stale = connection.execute(
                    "SELECT id FROM pdf_ocr_jobs WHERE run_id=? AND status='processing' "
                    "AND updated_at < ?",
                    (run_id, str(node["started_at"])),
                ).fetchall()
                for job in stale:
                    connection.execute(
                        "UPDATE pdf_ocr_jobs SET status='queued',updated_at=? "
                        "WHERE id=? AND status='processing'",
                        (utc_now(), str(job["id"])),
                    )
                    jobs[str(job["id"])]["status"] = "queued"
                    EventRepository(connection).append(
                        run_id,
                        "ocr.job.requeued_after_restart",
                        object_type="pdf_ocr_job",
                        object_id=str(job["id"]),
                        payload={"reason": "interrupted_process"},
                    )
                connection.commit()
        jobs_by_item = {
            str(job["corpus_item_id"]): job for job in jobs.values() if job["corpus_item_id"]
        }
        pending: dict[str, deque[tuple[str, str, str | None]]] = {
            "large_pdf": deque(),
            "light": deque(),
        }
        ocr_pending: dict[str, deque[tuple[str, str, str]]] = {
            "ocr_mistral": deque(),
            "ocr_ndlocr_lite": deque(),
        }
        language_pending: deque[tuple[str, str, str]] = deque()
        queued_items: set[str] = set()

        def ocr_lane_for(job: dict[str, Any]) -> str:
            engine = str(job.get("engine") or "")
            if engine not in {"mistral", "ndlocr_lite"}:
                try:
                    engine = routed_engine(str(job.get("source_language") or ""))
                except FukamiruError:
                    # A recovery job can still be unrouted until its input is rebuilt.
                    engine = "mistral"
            return f"ocr_{engine}"

        def enqueue_ocr(
            item_id: str, source_path: str, job_id: str, job: dict[str, Any]
        ) -> None:
            ocr_pending[ocr_lane_for(job)].append((item_id, source_path, job_id))

        def lane_for(source_path: str) -> str:
            path = Path(source_path)
            try:
                large_pdf = path.suffix.casefold() == ".pdf" and path.stat().st_size > 2 * 1024**2
            except OSError:
                large_pdf = path.suffix.casefold() == ".pdf"
            return "large_pdf" if large_pdf else "light"

        initial_ocr: list[tuple[str, str, str]] = []
        initial_language: list[tuple[str, str, str]] = []
        for row in rows:
            item_id = str(row["id"])
            source_path = str(row["source_path"])
            error = json.loads(row["error_json"] or "{}")
            job_id = str(error.get("ocr_job_id") or "")
            job = jobs.get(job_id) or jobs_by_item.get(item_id)
            message = str(error.get("message", ""))
            if job and row["status"] in {"discovered", "processing", "ocr_required"}:
                job_id = str(job["id"])
                if job["status"] == "completed":
                    pending[lane_for(source_path)].append((item_id, source_path, job_id))
                    queued_items.add(item_id)
                    continue
                if job["status"] == "queued":
                    initial_ocr.append((item_id, source_path, job_id))
                    queued_items.add(item_id)
                    continue
                if job["status"] == "needs_language":
                    if ocr_service.language_retry_delay(ocr_service.job(job_id)) is not None:
                        initial_language.append((item_id, source_path, job_id))
                    queued_items.add(item_id)
                    continue
                if job["status"] in {"processing", "failed", "cancelled"}:
                    continue
            if job and row["status"] == "failed" and job["status"] == "completed":
                # OCR output is durable even when the following source ingestion failed.
                # Reuse the same job and Item instead of opening or OCRing the PDF again.
                pending[lane_for(source_path)].append((item_id, source_path, str(job["id"])))
                queued_items.add(item_id)
                continue
            if job and row["status"] == "failed" and job["status"] == "failed":
                job_error = json.loads(job["error_json"] or "{}")
                console_failure = (
                    job_error.get("type") in {"UnicodeEncodeError", "FukamiruError"}
                    and "'cp932' codec can't encode" in str(job_error.get("message", ""))
                    and int(job["attempt"]) == 1
                )
                ocr_engine_failure = (
                    job_error.get("type") == "FukamiruError"
                    and int(job["attempt"]) < 4
                    and (
                        "The write operation timed out" in str(job_error.get("message", ""))
                        or "ElementTree.ParseError" in str(job_error.get("message", ""))
                    )
                )
                transient_recovery_lock = (
                    job_error.get("type") == "PDFRecoveryFailed"
                    and "WinError 32" in str(job_error.get("message", ""))
                    and job.get("recovery_kind")
                    and not job.get("input_pdf_sha256")
                    and int(job["attempt"]) == 0
                )
                if console_failure or transient_recovery_lock or ocr_engine_failure:
                    initial_ocr.append((item_id, source_path, str(job["id"])))
                    queued_items.add(item_id)
                    continue
            if row["status"] not in {"discovered", "processing"}:
                lock_failure = error.get("type") == "OperationalError" and message in {
                    "database is locked",
                    "database table is locked",
                }
                pdf_failure = (
                    item_id not in recovered_items
                    and Path(source_path).suffix.casefold() == ".pdf"
                    and (
                        is_encrypted_pdf_error(FukamiruError(message))
                        or "Embedding tokenizer produced no offsets" in message
                        or is_pdf_layout_error(FukamiruError(message))
                        or message == "No SourceSpan could be reconstructed"
                    )
                )
                tokenizer_failure = (
                    error.get("type") == "NonRetryableFukamiruError"
                    and "Embedding tokenizer dependency is unavailable" in message
                )
                epub_offsets = (
                    Path(source_path).suffix.casefold() == ".epub"
                    and error.get("type") == "EmptyEmbeddingOffsetsError"
                )
                text_encoding = (
                    Path(source_path).suffix.casefold() in {".txt", ".md", ".markdown"}
                    and message.startswith("Text encoding could not be detected:")
                )
                if row["status"] != "failed" or not (
                    lock_failure
                    or pdf_failure
                    or tokenizer_failure
                    or epub_offsets
                    or text_encoding
                ):
                    continue
                with self.databases.state.connect() as state:
                    EventRepository(state).append(
                        run_id,
                        "corpus.item.recovery_requested",
                        object_type="corpus_sync_item",
                        object_id=item_id,
                        payload={"previous_error": error},
                    )
            item = (item_id, source_path, None)
            lane = lane_for(source_path)
            if row["status"] == "failed":
                pending[lane].appendleft(item)
            else:
                pending[lane].append(item)

        initial_ocr.sort(
            key=lambda item: (
                jobs[item[2]]["status"] not in {"queued", "failed"},
                int(jobs[item[2]]["source_page_count"] or 0),
            )
        )
        for item_id, source_path, job_id in initial_ocr:
            enqueue_ocr(item_id, source_path, job_id, jobs[job_id])
        initial_language.sort(key=lambda item: int(jobs[item[2]]["source_page_count"] or 0))
        language_pending.extend(initial_language)
        # Keep image-language calls out of the OCR engine's dedicated worker.
        language_max_workers = min(4, self.settings.worker.codex_max_concurrency)
        with (
            ThreadPoolExecutor(max_workers=2, thread_name_prefix="corpus-source") as source_pool,
            ThreadPoolExecutor(max_workers=2, thread_name_prefix="corpus-ocr") as ocr_pool,
            ThreadPoolExecutor(
                max_workers=language_max_workers, thread_name_prefix="corpus-language"
            ) as language_pool,
        ):
            active: dict[Any, tuple[str, str, str, str | None]] = {}
            language_next_at = 0.0
            language_retry_at = 0.0
            language_active_limit = min(2, language_max_workers)
            language_success_streak = 0

            def submit_next(lane: str) -> None:
                nonlocal language_retry_at
                if lane != "language" and any(task[0] == lane for task in active.values()):
                    return
                if lane == "language":
                    if (
                        not language_pending
                        or sum(task[0] == "language" for task in active.values())
                        >= language_active_limit
                        or time.monotonic() < max(language_next_at, language_retry_at)
                    ):
                        return
                    next_delay: float | None = None
                    for _ in range(len(language_pending)):
                        item_id, source_path, job_id = language_pending.popleft()
                        job = ocr_service.job(job_id)
                        delay = ocr_service.language_retry_delay(job)
                        if delay is None:
                            if job["status"] == "queued":
                                enqueue_ocr(item_id, source_path, job_id, job)
                            continue
                        if delay > 0:
                            next_delay = delay if next_delay is None else min(next_delay, delay)
                            language_pending.append((item_id, source_path, job_id))
                            continue
                        self._set_item(item_id, "processing")
                        future = language_pool.submit(
                            ocr_service.resolve_language_from_images, job_id
                        )
                        active[future] = (lane, item_id, source_path, job_id)
                        language_retry_at = 0.0
                        return
                    language_retry_at = (
                        time.monotonic() + next_delay if next_delay is not None else 0.0
                    )
                    return
                if lane in ocr_pending:
                    if not ocr_pending[lane]:
                        return
                    item_id, source_path, job_id = ocr_pending[lane].popleft()
                    self._set_item(item_id, "processing")
                    future = ocr_pool.submit(self._run_ocr_for_item, job_id)
                    active[future] = (lane, item_id, source_path, job_id)
                    return
                if not pending[lane]:
                    return
                item_id, source_path, job_id = pending[lane].popleft()
                self._set_item(item_id, "processing")
                if job_id:
                    future = source_pool.submit(
                        self._ingest_completed_ocr_item, item_id, source_path, job_id
                    )
                else:
                    future = source_pool.submit(
                        self._ingest_ready_item, run_id, item_id, source_path
                    )
                active[future] = (lane, item_id, source_path, job_id)

            def fill_language() -> None:
                while (
                    language_pending
                    and sum(task[0] == "language" for task in active.values())
                    < language_active_limit
                ):
                    before = len(active)
                    submit_next("language")
                    if len(active) == before:
                        break

            submit_next("large_pdf")
            submit_next("light")
            for lane in ocr_pending:
                submit_next(lane)
            fill_language()
            while active or language_pending or any(ocr_pending.values()) or any(pending.values()):
                fill_language()
                for lane in ocr_pending:
                    submit_next(lane)
                if not active:
                    next_language_at = max(language_next_at, language_retry_at)
                    time.sleep(min(max(next_language_at - time.monotonic(), 0.1), 30.0))
                    fill_language()
                    for lane in ocr_pending:
                        submit_next(lane)
                    submit_next("large_pdf")
                    submit_next("light")
                    continue
                timeout = (
                    min(
                        max(
                            max(language_next_at, language_retry_at) - time.monotonic(),
                            0.1,
                        ),
                        30.0,
                    )
                    if language_pending
                    and sum(task[0] == "language" for task in active.values())
                    < language_active_limit
                    else None
                )
                completed, _ = wait(tuple(active), timeout=timeout, return_when=FIRST_COMPLETED)
                for future in completed:
                    lane, item_id, source_path, job_id = active.pop(future)
                    try:
                        payload = future.result()
                        if lane == "language":
                            delay = ocr_service.language_retry_delay(payload)
                            decision = payload.get("language_decision") or {}
                            if decision.get("method") == "pdf_page_image_llm":
                                language_success_streak += 1
                                if language_success_streak >= 2 * language_active_limit:
                                    language_active_limit = min(
                                        language_active_limit + 1, language_max_workers
                                    )
                                    language_success_streak = 0
                            if payload["status"] == "queued":
                                enqueue_ocr(item_id, source_path, str(job_id), payload)
                            elif delay is not None:
                                language_pending.append((item_id, source_path, str(job_id)))
                            if decision.get("method") == "pdf_page_image_llm_rate_limited":
                                language_active_limit = max(1, language_active_limit // 2)
                                language_success_streak = 0
                                language_next_at = max(
                                    language_next_at,
                                    time.monotonic() + max(30.0, delay or 0.0),
                                )
                            language_retry_at = 0.0
                            error_type = (
                                "OCRRequired" if payload["status"] == "queued"
                                else "OCRRetryScheduled" if delay is not None
                                else "OCRNeedsReview"
                            )
                            item_error = {
                                "type": error_type,
                                "ocr_job_id": job_id,
                                "ocr_status": payload["status"],
                                "language_decision": decision,
                            }
                            self._set_item(item_id, "ocr_required", error=item_error)
                            payload = {
                                "status": "ocr_required", "ocr_job_id": job_id,
                                "ocr_status": payload["status"],
                                "language_decision": decision,
                                "error": item_error,
                            }
                        elif lane in ocr_pending and payload["status"] == "completed":
                            self._set_item(
                                item_id,
                                "discovered",
                                error={
                                    "type": "OCRReady",
                                    "ocr_job_id": job_id,
                                    "output_md_path": payload.get("output_text_path"),
                                },
                            )
                            pending[lane_for(source_path)].append((item_id, source_path, job_id))
                            payload = {
                                "status": "discovered",
                                "ocr_job_id": job_id,
                                "ocr_status": "completed",
                                "output_md_path": payload.get("output_text_path"),
                            }
                        else:
                            self._set_item(
                                item_id,
                                str(payload["status"]),
                                source_asset_id=payload.get("source_asset_id"),
                                document_unit_id=payload.get("document_unit_id"),
                                error=payload.get("error"),
                            )
                    except Exception as exc:
                        message = str(exc) or exc.__class__.__name__
                        payload = {"status": "failed", "message": message}
                        self._set_item(
                            item_id,
                            "failed",
                            error={"type": exc.__class__.__name__, "message": message},
                        )
                    counts = self.refresh_counts(batch_id)
                    payload.update(counts)
                    payload.update(
                        {
                            "position": sum(counts.values()),
                            "total": len(rows),
                            "path": source_path,
                            "lane": "ocr" if lane in ocr_pending else lane,
                        }
                    )
                    if lane in ocr_pending:
                        payload["ocr_lane"] = lane.removeprefix("ocr_")
                    with self.databases.state.connect() as connection:
                        EventRepository(connection).append(
                            run_id,
                            "corpus.item.updated",
                            object_type="corpus_sync_item",
                            object_id=item_id,
                            payload=payload,
                        )
                    if (
                        lane not in ocr_pending
                        and lane != "language"
                        and payload["status"] == "ocr_required"
                    ):
                        new_job_id = str(payload.get("ocr_job_id") or "")
                        if new_job_id and item_id not in queued_items:
                            queued_items.add(item_id)
                            job = ocr_service.job(new_job_id)
                            if job["status"] == "queued":
                                enqueue_ocr(item_id, source_path, new_job_id, job)
                            elif ocr_service.language_retry_delay(job) is not None:
                                language_pending.append((item_id, source_path, new_job_id))
                                language_retry_at = 0.0
                    submit_next(lane)
                    if lane in ocr_pending:
                        submit_next(lane_for(source_path))
                    for ocr_lane in ocr_pending:
                        submit_next(ocr_lane)
                    fill_language()
        return self.refresh_counts(batch_id)

    def _run_ocr_for_item(self, job_id: str) -> dict[str, Any]:
        from fukamiru.ocr.service import DocumentOCRService

        service = DocumentOCRService(self.databases, self.settings)
        job = service.job(job_id)
        if job["status"] in {"queued", "failed"}:
            job = service.process_job(job_id, output_format="md")
        elif job["status"] == "completed":
            job = service.ensure_markdown(job_id)
        if job["status"] == "completed":
            return job
        return {
            "status": "ocr_required" if job["status"] == "needs_language" else "failed",
            "ocr_job_id": job_id,
            "ocr_status": job["status"],
            "error": {
                "type": "OCRNeedsReview" if job["status"] == "needs_language" else "OCRFailed",
                "message": (
                    "OCR言語の確認待ち"
                    if job["status"] == "needs_language"
                    else "OCR処理に失敗しました"
                ),
                "ocr_job_id": job_id,
                "details": job.get("error"),
            },
        }

    def _ingest_completed_ocr_item(
        self, item_id: str, source_path: str, job_id: str
    ) -> dict[str, Any]:
        metrics = StageMetrics("ocr_output_validation")
        outcome = "failed"
        try:
            result = self._ingest_completed_ocr_item_inner(
                item_id, source_path, job_id, metrics=metrics
            )
            outcome = str(result["status"])
            return result
        finally:
            timing = metrics.finish(outcome=outcome)
            try:
                with self.databases.state.connect(busy_timeout_ms=100) as state:
                    job = state.execute(
                        "SELECT run_id,attempt FROM pdf_ocr_jobs WHERE id=?", (job_id,)
                    ).fetchone()
                    if job is not None:
                        EventRepository(state).append(
                            str(job["run_id"]),
                            "corpus.item.stage_metrics",
                            object_type="corpus_sync_item",
                            object_id=item_id,
                            payload={
                                "lane": "ocr_output_ingestion",
                                "ocr_attempt": int(job["attempt"]),
                                "timing": timing,
                            },
                        )
            except Exception:
                pass

    def _ingest_completed_ocr_item_inner(
        self,
        item_id: str,
        source_path: str,
        job_id: str,
        *,
        metrics: StageMetrics,
    ) -> dict[str, Any]:
        from fukamiru.ocr.service import (
            DocumentOCRService,
            _sha256_file,
            load_ocr_pages,
        )

        job = DocumentOCRService(self.databases, self.settings).ensure_markdown(job_id)
        metrics.annotate(page_count=job.get("source_page_count"))
        if job["status"] != "completed":
            raise FukamiruError(f"OCRジョブが未完了です: {job_id}")
        if _sha256_file(Path(source_path)) != str(job["source_sha256"]):
            raise FukamiruError("OCR完了後に原本PDFが変更されました")
        if Path(str(job["output_text_path"])).suffix.casefold() != ".md":
            raise FukamiruError("OCRのMarkdown出力がありません")

        def ingest_output() -> dict[str, Any]:
            metrics.transition("ocr_output_ingestion")
            with self.databases.knowledge.connect(
                busy_timeout_ms=20 * 60 * 1000, cache_size_kib=_INGEST_CACHE_KIB
            ) as connection:
                return IngestionService(connection, self.settings).ingest_ocr_pages(
                    source_path,
                    load_ocr_pages(Path(str(job["output_json_path"]))),
                    source_language=str(job["source_language"]),
                    engine=str(job["engine"]),
                    json_path=str(job["output_json_path"]),
                    material_role="unknown",
                    permission="unknown",
                )

        result = retry_sqlite_operation(ingest_output)
        return {
            "status": "reused" if result.get("reused") else "ready",
            "source_asset_id": result.get("source_asset_id"),
            "document_unit_id": result.get("document_unit_id"),
            "ocr_job_id": job_id,
            "output_md_path": job["output_text_path"],
        }

    def _ingest_ready_item(self, run_id: str, item_id: str, source_path: str) -> dict[str, Any]:
        def on_retry(attempt: int, exc: BaseException) -> None:
            with self.databases.state.connect() as state:
                EventRepository(state).append(
                    run_id,
                    "corpus.item.recovery_retry",
                    object_type="corpus_sync_item",
                    object_id=item_id,
                    payload={
                        "attempt": attempt,
                        "max_attempts": 4,
                        "message": str(exc),
                        "path": source_path,
                    },
                )

        attempt_number = 0

        def operation() -> dict[str, Any]:
            nonlocal attempt_number
            attempt_number += 1
            metrics = StageMetrics("identify_source")
            outcome = "failed"
            try:
                try:
                    result = self._ingest_ready_item_once(
                        run_id, item_id, source_path, metrics=metrics
                    )
                except EmptyEmbeddingOffsetsError:
                    if Path(source_path).suffix.casefold() != ".pdf":
                        raise
                    metrics.transition("pdf_recovery")
                    result = self._queue_pdf_recovery(
                        run_id, item_id, source_path, "invalid_text_layer", metrics=metrics
                    )
                except FukamiruError as exc:
                    if Path(source_path).suffix.casefold() != ".pdf":
                        raise
                    if is_encrypted_pdf_error(exc):
                        kind = "encrypted_pdf"
                    elif is_pdf_layout_error(exc):
                        kind = "layout_analysis_failed"
                    elif str(exc) == "No SourceSpan could be reconstructed":
                        kind = "invalid_text_layer"
                    else:
                        raise
                    metrics.transition("pdf_recovery")
                    result = self._queue_pdf_recovery(
                        run_id, item_id, source_path, kind, metrics=metrics
                    )
                outcome = str(result["status"])
                return result
            finally:
                timing = metrics.finish(outcome=outcome)
                try:
                    with self.databases.state.connect(busy_timeout_ms=100) as state:
                        EventRepository(state).append(
                            run_id,
                            "corpus.item.stage_metrics",
                            object_type="corpus_sync_item",
                            object_id=item_id,
                            payload={
                                "lane": "source",
                                "attempt": attempt_number,
                                "timing": timing,
                            },
                        )
                except Exception:
                    # Diagnostics must not fail or retry an otherwise valid item.
                    pass

        return retry_sqlite_operation(operation, on_retry=on_retry)

    def _queue_pdf_recovery(
        self,
        run_id: str,
        item_id: str,
        source_path: str,
        kind: str,
        *,
        metrics: StageMetrics | None = None,
    ) -> dict[str, Any]:
        from fukamiru.ocr.service import DocumentOCRService

        with self.databases.knowledge.connect() as connection:
            source = IngestionService(connection, self.settings).locate(source_path)

        last_worker_cpu = 0.0
        last_worker_wall = 0.0
        last_worker_pid: int | None = None
        last_worker_phase: str | None = None

        def progress(payload: dict[str, Any]) -> None:
            nonlocal last_worker_cpu, last_worker_wall, last_worker_pid, last_worker_phase
            if metrics is not None and "worker_cpu_seconds" in payload:
                worker_cpu = float(payload["worker_cpu_seconds"])
                worker_wall = float(payload.get("worker_elapsed_seconds") or 0)
                worker_pid = int(payload.get("worker_pid") or 0)
                phase = str(payload.get("phase") or "pdf_recovery")
                if worker_pid != last_worker_pid:
                    cpu_delta = worker_cpu
                    wall_delta = worker_wall
                else:
                    cpu_delta = max(0.0, worker_cpu - last_worker_cpu)
                    wall_delta = max(0.0, worker_wall - last_worker_wall)
                if cpu_delta:
                    metrics.add_external_cpu(
                        last_worker_phase or phase,
                        cpu_delta,
                    )
                if wall_delta:
                    metrics.add_external_wall(last_worker_phase or phase, wall_delta)
                last_worker_cpu = worker_cpu
                last_worker_wall = worker_wall
                last_worker_pid = worker_pid
                last_worker_phase = phase
            with self.databases.state.connect() as state:
                EventRepository(state).append(
                    run_id,
                    "corpus.item.progress",
                    object_type="corpus_sync_item",
                    object_id=item_id,
                    payload={**payload, "path": str(source.path)},
                )

        job = DocumentOCRService(self.databases, self.settings).enqueue(
            run_id=run_id,
            source_path=source.path,
            source_sha256=source.sha256,
            source_language="auto",
            corpus_item_id=item_id,
            recovery_kind=kind,
            force_ocr=True,
            progress=progress,
        )
        return {
            "status": "ocr_required",
            "ocr_job_id": job["id"],
            "ocr_status": job["status"],
            "recovery_kind": kind,
            "error": {
                "type": "PDFRecoveryRequired",
                "message": "画像PDFからOCRを再作成します",
                "ocr_job_id": job["id"],
                "ocr_status": job["status"],
                "recovery_kind": kind,
                "input_pdf_path": job.get("input_pdf_path"),
            },
        }

    def _ingest_ready_item_once(
        self,
        run_id: str,
        item_id: str,
        source_path: str,
        *,
        metrics: StageMetrics | None = None,
    ) -> dict[str, Any]:
        from fukamiru.ocr.service import DocumentOCRService

        path = Path(source_path).resolve()
        item_started = time.monotonic()

        def report_progress(payload: dict[str, Any]) -> None:
            if metrics is not None and payload.get("phase"):
                metrics.transition(str(payload["phase"]))
                if payload.get("unit") == "pages" and isinstance(payload.get("total"), int):
                    metrics.annotate(page_count=payload["total"])
            with self.databases.state.connect() as state:
                EventRepository(state).append(
                    run_id,
                    "corpus.item.progress",
                    object_type="corpus_sync_item",
                    object_id=item_id,
                    payload={
                        **payload,
                        "path": str(path),
                        "item_elapsed_seconds": round(time.monotonic() - item_started, 3),
                    },
                )

        # A restart must resume native preparation, not parse/tokenize the known
        # invalid original again before discovering the same recovery route.
        with self.databases.state.connect() as state:
            recovery_job = state.execute(
                "SELECT recovery_kind,status FROM pdf_ocr_jobs "
                "WHERE run_id=? AND source_path=? AND recovery_kind IS NOT NULL",
                (run_id, str(path)),
            ).fetchone()
        if recovery_job is not None:
            if metrics is not None:
                metrics.transition("pdf_recovery")
            return self._queue_pdf_recovery(
                run_id,
                item_id,
                str(path),
                str(recovery_job["recovery_kind"]),
                metrics=metrics,
            )

        with self.databases.knowledge.connect(
            busy_timeout_ms=20 * 60 * 1000, cache_size_kib=_INGEST_CACHE_KIB
        ) as connection:
            ingestion = IngestionService(connection, self.settings)
            located = ingestion.locate(str(path))
            with self._asset_locks_guard:
                asset_lock = self._asset_locks.setdefault(located.sha256, Lock())
            with asset_lock:
                prepared_pdf = None
                layer = None
                if path.suffix.casefold() == ".pdf" and not ingestion.has_reusable_source(located):
                    report_progress(
                        {"phase": "prepare_pdf", "label": "PDF本文と座標を一度に解析中"}
                    )
                    prepared_pdf = PDFLayoutAnalyzer().prepare(path)
                    layer = prepared_pdf.text_layer
                    report_progress(
                        {
                            "phase": "prepare_pdf",
                            "label": "PDF本文と座標の解析完了",
                            "completed": layer.page_count,
                            "total": layer.page_count,
                            "unit": "pages",
                        }
                    )
                if layer is not None and not layer.usable:
                    job = DocumentOCRService(self.databases, self.settings).enqueue(
                        run_id=run_id,
                        source_path=path,
                        source_sha256=located.sha256,
                        source_language="auto",
                        corpus_item_id=item_id,
                    )
                    return {
                        "status": "ocr_required",
                        "error": {
                            "type": "OCRRequired",
                            "message": (
                                "OCR言語を自動判定済みです。実行を待機しています"
                                if job["status"] == "queued"
                                else "OCR言語の確認を待機しています"
                            ),
                            "ocr_job_id": job["id"],
                            "ocr_status": job["status"],
                            "language": job["source_language"],
                            "language_origin": job.get("language_origin"),
                            "language_confidence": job.get("language_confidence"),
                            "language_decision": job.get("language_decision"),
                        },
                        "ocr_job_id": job["id"],
                        "ocr_status": job["status"],
                        "ocr_language": job["source_language"],
                        "ocr_language_origin": job.get("language_origin"),
                        "ocr_language_confidence": job.get("language_confidence"),
                    }
                result = ingestion.ingest_path(
                    str(path),
                    located_source=located,
                    inspected_pdf_text_layer=layer,
                    prepared_pdf=prepared_pdf,
                    progress=report_progress,
                    source_language="auto",
                    material_role="unknown",
                    permission="unknown",
                )
        return {
            "status": "reused" if result.get("reused") else "ready",
            "source_asset_id": result.get("source_asset_id"),
            "document_unit_id": result.get("document_unit_id"),
        }

    def ingest_ocr_outputs(self, run_id: str, batch_id: str) -> dict[str, Any]:
        """Ingest completed page-oriented OCR outputs while retaining original PDFs."""
        from fukamiru.ocr.service import DocumentOCRService, load_ocr_pages

        ocr = DocumentOCRService(self.databases, self.settings)
        for job in ocr.jobs(run_id=run_id):
            item_id = job.get("corpus_item_id")
            if not item_id:
                continue
            with self.databases.state.connect() as state:
                item = state.execute(
                    "SELECT status,skip_reason FROM corpus_sync_items WHERE id=? AND batch_id=?",
                    (str(item_id), batch_id),
                ).fetchone()
            if (
                item is None
                or item["skip_reason"]
                or item["status"] in {"ready", "reused", "failed"}
            ):
                continue
            if job["status"] != "completed":
                self._set_item(
                    str(item_id),
                    "ocr_required",
                    error={
                        "type": "OCRNeedsReview",
                        "message": "言語別OCRの結果を確認してください",
                        "ocr_job_id": job["id"],
                        "ocr_status": job["status"],
                        "details": job.get("error"),
                    },
                )
                continue
            try:

                def ingest_output(job: dict[str, Any] = job) -> dict[str, Any]:
                    with self.databases.knowledge.connect(
                        busy_timeout_ms=20 * 60 * 1000
                    ) as connection:
                        return IngestionService(connection, self.settings).ingest_ocr_pages(
                            str(job["source_path"]),
                            load_ocr_pages(Path(str(job["output_json_path"]))),
                            source_language=str(job["source_language"]),
                            engine=str(job["engine"]),
                            json_path=str(job["output_json_path"]),
                            material_role="unknown",
                            permission="unknown",
                        )

                result = retry_sqlite_operation(ingest_output)
                status = "reused" if result.get("reused") else "ready"
                self._set_item(
                    str(item_id),
                    status,
                    source_asset_id=result.get("source_asset_id"),
                    document_unit_id=result.get("document_unit_id"),
                )
            except Exception as exc:
                self._set_item(
                    str(item_id),
                    "failed",
                    error={"type": exc.__class__.__name__, "message": str(exc)},
                )
        self.reconcile_completed_ocr_items(run_id, batch_id)
        summary = self.refresh_counts(batch_id)
        with self.databases.state.connect() as connection:
            connection.execute(
                "UPDATE corpus_sync_batches SET status = 'indexing', updated_at = ? WHERE id = ?",
                (utc_now(), batch_id),
            )
            connection.commit()
        return summary

    def reconcile_completed_ocr_items(self, run_id: str, batch_id: str) -> int:
        """Resolve aliases of a shared completed job without repeating OCR.

        A Run has one job per original SHA, while the batch contains one Item
        per file. Only reuse a ready canonical asset after checking the alias's
        current bytes; preserve its primary source location and all revisions.
        """
        with self.databases.state.connect() as state:
            batch = state.execute(
                "SELECT run_id FROM corpus_sync_batches WHERE id=?", (batch_id,)
            ).fetchone()
            if batch is None or batch["run_id"] != run_id:
                raise FukamiruError("OCR reconciliation Run and batch do not match")
            jobs = {
                str(row["id"]): dict(row) for row in state.execute(
                    """SELECT j.id,j.source_sha256,i.source_asset_id,i.document_unit_id
                    FROM pdf_ocr_jobs j JOIN corpus_sync_items i ON i.id=j.corpus_item_id
                    WHERE j.run_id=? AND j.status='completed' AND i.batch_id=?
                      AND i.skip_reason IS NULL AND i.status IN ('ready','reused')""",
                    (run_id, batch_id),
                )
            }
            items = [dict(row) for row in state.execute(
                "SELECT id,source_path,error_json FROM corpus_sync_items "
                "WHERE batch_id=? AND skip_reason IS NULL AND status='ocr_required'",
                (batch_id,),
            )]
        reused = 0
        for item in items:
            error = json.loads(item["error_json"] or "{}")
            job = jobs.get(str(error.get("ocr_job_id") or ""))
            if job is None:
                continue
            path = Path(item["source_path"]).resolve()
            if not path.is_relative_to(self.default_root) or not path.is_file():
                continue
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != job["source_sha256"]:
                continue
            now = utc_now()
            with self.databases.knowledge.connect() as knowledge:
                asset = knowledge.execute(
                    """SELECT a.id FROM source_assets a JOIN document_units d
                    ON d.source_asset_id=a.id WHERE a.id=? AND a.sha256=?
                    AND a.ingestion_status='ready' AND d.id=?
                    AND EXISTS(SELECT 1 FROM chunks WHERE document_unit_id=d.id)""",
                    (job["source_asset_id"], digest, job["document_unit_id"]),
                ).fetchone()
                if asset is None:
                    continue
                knowledge.execute(
                    "UPDATE source_locations SET is_primary=0,is_available=0 "
                    "WHERE path=? AND source_asset_id<>?", (str(path), asset["id"]),
                )
                knowledge.execute(
                    """INSERT INTO source_locations(
                    source_asset_id,path,is_primary,first_seen_at,last_verified_at
                    ) VALUES (?,?,0,?,?) ON CONFLICT(source_asset_id,path) DO UPDATE SET
                    is_available=1,last_verified_at=excluded.last_verified_at""",
                    (asset["id"], str(path), now, now),
                )
                knowledge.commit()
            self._set_item(
                item["id"], "reused", source_asset_id=job["source_asset_id"],
                document_unit_id=job["document_unit_id"],
            )
            with self.databases.state.connect() as state:
                EventRepository(state).append(
                    run_id, "corpus.item.ocr_reused", object_type="corpus_sync_item",
                    object_id=item["id"], payload={
                        "ocr_job_id": job["id"], "source_sha256": digest,
                        "source_asset_id": job["source_asset_id"],
                        "document_unit_id": job["document_unit_id"],
                    },
                )
            reused += 1
        if reused:
            self.refresh_counts(batch_id)
        return reused

    def _set_item(
        self,
        item_id: str,
        status: str,
        *,
        source_asset_id: str | None = None,
        document_unit_id: str | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        with self.databases.state.connect() as connection:
            connection.execute(
                """
                UPDATE corpus_sync_items
                SET status = ?, source_asset_id = COALESCE(?, source_asset_id),
                    document_unit_id = COALESCE(?, document_unit_id),
                    error_json = ?, updated_at = ?
                WHERE id = ? AND skip_reason IS NULL
                """,
                (
                    status,
                    source_asset_id,
                    document_unit_id,
                    json.dumps(error, ensure_ascii=False) if error else None,
                    utc_now(),
                    item_id,
                ),
            )
            connection.commit()

    def refresh_counts(self, batch_id: str) -> dict[str, Any]:
        with self.databases.state.connect() as connection:
            rows = connection.execute(
                """
                SELECT status, COUNT(*) AS count FROM corpus_sync_items
                WHERE batch_id = ? AND skip_reason IS NULL GROUP BY status
                """,
                (batch_id,),
            ).fetchall()
            counts = {str(row["status"]): int(row["count"]) for row in rows}
            connection.execute(
                """
                UPDATE corpus_sync_batches SET
                    discovered_count = ?, ready_count = ?, reused_count = ?, ocr_required_count = ?,
                    failed_count = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    sum(counts.values()),
                    counts.get("ready", 0),
                    counts.get("reused", 0),
                    counts.get("ocr_required", 0),
                    counts.get("failed", 0),
                    utc_now(),
                    batch_id,
                ),
            )
            connection.commit()
        return {
            "ready_count": counts.get("ready", 0),
            "reused_count": counts.get("reused", 0),
            "ocr_required_count": counts.get("ocr_required", 0),
            "failed_count": counts.get("failed", 0),
        }

    def complete(self, batch_id: str) -> None:
        with self.databases.state.connect() as connection:
            connection.execute(
                "UPDATE corpus_sync_batches SET status = 'completed', updated_at = ? WHERE id = ?",
                (utc_now(), batch_id),
            )
            connection.commit()

    def update_index_progress(self, batch_id: str, payload: dict[str, Any]) -> None:
        """Persist canonical index progress; SSE remains a notification channel."""
        with self.databases.state.connect() as connection:
            connection.execute(
                """
                UPDATE corpus_sync_batches SET
                    embedding_build_id = ?, embedding_completed_count = ?,
                    embedding_total_count = ?, embedding_chunks_per_second = ?,
                    embedding_eta_seconds = ?, embedding_batch_size = ?,
                    embedding_model = ?, embedding_model_revision = ?,
                    configured_device = ?, actual_embedding_device = ?,
                    embedding_average_tokens_per_chunk = ?,
                    embedding_tokens_per_second = ?,
                    embedding_padded_token_count = ?,
                    embedding_padding_token_count = ?, embedding_padding_ratio = ?,
                    embedding_tokenization_seconds = ?,
                    embedding_gpu_inference_seconds = ?,
                    embedding_db_write_seconds = ?,
                    embedding_gpu_utilization_percent = ?,
                    embedding_vram_used_mib = ?, embedding_vram_total_mib = ?,
                    embedding_cpu_utilization_percent = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    payload.get("embedding_build_id"),
                    int(payload.get("embedded_count", 0)),
                    int(payload.get("total_count", 0)),
                    payload.get("chunks_per_second"),
                    payload.get("eta_seconds"),
                    payload.get("batch_size"),
                    payload.get("model"),
                    payload.get("model_revision"),
                    payload.get("configured_device"),
                    payload.get("actual_embedding_device"),
                    payload.get("average_tokens_per_chunk"),
                    payload.get("tokens_per_second"),
                    payload.get("padded_token_count"),
                    payload.get("padding_token_count"),
                    payload.get("padding_ratio"),
                    payload.get("tokenization_seconds"),
                    payload.get("gpu_inference_seconds"),
                    payload.get("db_write_seconds"),
                    payload.get("gpu_utilization_percent"),
                    payload.get("vram_used_mib"),
                    payload.get("vram_total_mib"),
                    payload.get("cpu_utilization_percent"),
                    utc_now(),
                    batch_id,
                ),
            )
            connection.commit()

    def snapshot(self, batch_id: str) -> dict[str, Any]:
        from fukamiru.ocr.service import DocumentOCRService

        with self.databases.state.connect() as connection:
            batch = connection.execute(
                "SELECT * FROM corpus_sync_batches WHERE id = ?", (batch_id,)
            ).fetchone()
            if batch is None:
                raise FukamiruError(f"Unknown corpus sync batch: {batch_id}")
            items = connection.execute(
                """
                SELECT * FROM corpus_sync_items
                WHERE batch_id = ? ORDER BY relative_path
                """,
                (batch_id,),
            ).fetchall()
        decoded = []
        for row in items:
            item = dict(row)
            raw_error = item.pop("error_json")
            item["error"] = json.loads(raw_error) if raw_error else None
            if item["skip_reason"]:
                item["previous_status"] = item["status"]
                item["status"] = "skipped"
            decoded.append(item)
        ocr_jobs = DocumentOCRService(self.databases, self.settings).jobs(
            run_id=str(batch["run_id"])
        )
        batch_data = dict(batch)
        batch_data["source_roots"] = [
            str(root) for root in self.source_roots(Path(str(batch["root_path"])).resolve())
        ]
        return {"batch": batch_data, "items": decoded, "ocr_jobs": ocr_jobs}

    def latest_snapshot(self) -> dict[str, Any] | None:
        with self.databases.state.connect() as connection:
            row = connection.execute(
                "SELECT id FROM corpus_sync_batches ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        return self.snapshot(str(row["id"])) if row else None
