"""Reuse corpus OCR immediately; persist useful sources through independent runs."""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from pathlib import Path
from typing import Any

from fukamiru.ocr.service import DocumentOCRService
from fukamiru.storage.runs import utc_now

_locks_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}


class ResearchSourceIngestion:
    def __init__(self, databases: Any, settings: Any):
        self.databases = databases
        self.settings = settings

    def read(self, run_id: str, source: Path, kind: str) -> dict[str, Any]:
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        with _locks_guard:
            lock = _locks.setdefault(str(self.settings.paths.state_db) + digest, threading.RLock())
        with lock:
            service = DocumentOCRService(self.databases, self.settings)
            with self.databases.state.connect() as connection:
                rows = connection.execute(
                    "SELECT id FROM pdf_ocr_jobs WHERE source_sha256=? AND status='completed' "
                    "ORDER BY completed_at DESC", (digest,),
                ).fetchall()
            job = None
            for row in rows:
                candidate = service.job(row["id"])
                path = Path(str(candidate.get("output_json_path") or ""))
                if path.is_file():
                    job = candidate
                    break
            if job is None:
                job = service.enqueue(run_id=run_id, source_path=source, source_sha256=digest,
                                      source_language="auto", recovery_kind=kind, force_ocr=True)
                if job["status"] == "needs_language":
                    job = service.resolve_language_from_images(job["id"])
                if job["status"] == "queued":
                    job = service.process_job(job["id"])
            if job["status"] != "completed":
                return {"status": "ocr_" + job["status"], "ocr_job_id": job["id"],
                        "error": job.get("error"), "read_complete": False}
            payload = json.loads(Path(job["output_json_path"]).read_text(encoding="utf-8"))
            if payload.get("source_sha256") != (job.get("input_pdf_sha256") or digest):
                raise ValueError("OCR output does not match its recorded PDF")
            pages = [{"number": int(p["page_number"]), "text": str(p["text"])}
                     for p in payload["pages"]]
            if [p["number"] for p in pages] != list(range(1, int(job["source_page_count"]) + 1)):
                raise ValueError("OCR output is not page aligned")
            return {"status": "retrieved", "source_level": "full_text", "pages": pages,
                    "ocr_job_id": job["id"], "ocr_engine": job["engine"],
                    "source_language": job["source_language"],
                    "source_path": str(source), "content_sha256": digest,
                    "pages_examined": len(pages), "total_pages": len(pages),
                    "read_complete": True}

    def enqueue_useful(self, parent_run_id: str, record: dict[str, Any]) -> str | None:
        acquisition = (record.get("provenance") or {}).get("content_acquisition") or {}
        if acquisition.get("status") != "retrieved" or not acquisition.get("ocr_job_id"):
            return None
        digest = acquisition["content_sha256"]
        service = DocumentOCRService(self.databases, self.settings)
        job = service.job(acquisition["ocr_job_id"])
        source = Path(acquisition["source_path"])
        if (job["status"] != "completed"
                or hashlib.sha256(source.read_bytes()).hexdigest() != digest):
            raise ValueError("Useful OCR source changed before ingestion handoff")
        with self.databases.state.connect() as connection:
            with _locks_guard:
                lock = _locks.setdefault(str(self.settings.paths.state_db) + digest,
                                         threading.RLock())
            with lock:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT ingestion_run_id FROM research_source_handoffs WHERE source_sha256=?",
                    (digest,),
                ).fetchone()
                if existing:
                    return str(existing["ingestion_run_id"])
                parent = connection.execute("SELECT project_id FROM runs WHERE id=?",
                                            (parent_run_id,)).fetchone()
                inputs = {
                    "research_source": record, "research_parent_run_id": parent_run_id,
                    "source_path": str(source), "sha256": digest,
                    "byte_size": source.stat().st_size, "media_type": "application/pdf",
                    "source_language": job["source_language"], "ocr_required": True,
                    "ocr_job_id": job["id"], "ocr_job_status": "completed",
                    "ocr_engine": job["engine"], "ocr_json_path": job["output_json_path"],
                    "ocr_text_path": job["output_text_path"], "material_role": "secondary",
                    "permission": "unknown",
                }
                run_id, node_id, now = str(uuid.uuid4()), str(uuid.uuid4()), utc_now()
                packed = json.dumps(inputs, ensure_ascii=False)
                connection.execute(
                    "INSERT INTO runs(id,project_id,workflow_name,workflow_version,status,"
                    "input_json,created_at) VALUES "
                    "(?,?,'research_source_ingestion','1','queued',?,?)",
                    (run_id, parent["project_id"], packed, now),
                )
                connection.execute(
                    "INSERT INTO node_runs(id,run_id,node_id,attempt,status,input_json,"
                    "resource_lane) "
                    "VALUES (?,?,'register_asset',1,'queued',?,'ingestion_cpu')",
                    (node_id, run_id, packed),
                )
                connection.execute(
                    "INSERT INTO research_source_handoffs VALUES (?,?,?,?,?,?)",
                    (digest, parent_run_id, run_id, job["id"],
                     json.dumps(record, ensure_ascii=False), utc_now()),
                )
                connection.execute(
                    "INSERT INTO event_log(run_id,event_type,object_type,object_id,payload_json,"
                    "created_at) VALUES (?,'run.created','run',?,?,?)",
                    (run_id, run_id, json.dumps({"status": "queued",
                     "workflow_name": "research_source_ingestion",
                     "parent_run_id": parent_run_id}), now),
                )
                connection.commit()
                return run_id
