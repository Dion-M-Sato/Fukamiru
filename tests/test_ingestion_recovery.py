from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pymupdf
import pytest
from pypdf import PdfReader

from fukamiru.bootstrap import bootstrap
from fukamiru.config import load_settings
from fukamiru.errors import (
    EmptyEmbeddingOffsetsError,
    FukamiruError,
    SQLiteRecoveryExhaustedError,
)
from fukamiru.knowledge.corpus import CorpusSyncService
from fukamiru.knowledge.service import IngestionService, _token_offsets
from fukamiru.ocr.language_detection import (
    VISION_LANGUAGE_SCHEMA,
    identify_page_image_language,
)
from fukamiru.ocr.models import OCRPage, OCRResult
from fukamiru.ocr.pdf_recovery import file_sha256, prepare_image_pdf
from fukamiru.ocr.pipeline import OCRPipelineService
from fukamiru.ocr.service import DocumentOCRService, detect_pdf_language
from fukamiru.storage.database import DatabasePair, retry_sqlite_operation
from fukamiru.storage.runs import RunRepository
from fukamiru.translation.codex import CodexTranslationAdapter
from fukamiru.worker import WorkerRunner
from fukamiru.workflow.templates import corpus_sync_workflow, ingestion_workflow


@pytest.fixture
def environment(tmp_path):
    config = tmp_path / "local.toml"
    paths = {
        key: tmp_path / value
        for key, value in {
            "state_db": "state.db",
            "knowledge_db": "knowledge.db",
            "runtime_root": "runtime",
            "corpus_root": "corpus",
            "ocr_root": "ocr",
            "knowledge_artifacts_root": "artifacts",
            "faiss_root": "faiss",
        }.items()
    }
    config.write_text(
        "[paths]\n"
        + "\n".join(f'{key}="{value.as_posix()}"' for key, value in paths.items())
        + '\n[retrieval]\ntokenizer_mode="unicode"\nrequire_cuda=false\n',
        encoding="utf-8",
    )
    settings = load_settings(config)
    bootstrap(settings)
    db = DatabasePair.from_settings(settings)
    managed = settings.paths.corpus_root / "managed_sources"
    managed.mkdir(parents=True, exist_ok=True)
    with db.state.connect() as connection:
        run = (
            RunRepository(connection)
            .create(
                workflow_name="corpus_sync",
                workflow_version="2",
                entry_node="scan_sources",
                project_id=None,
                inputs={},
            )
            .run_id
        )
    return settings, db, managed, run


def make_pdf(path, *, encrypted=False, password="", rotated=False):
    with pymupdf.open() as document:
        for index in range(2):
            page = document.new_page(width=240, height=320)
            page.insert_text((24, 48), f"The original evidence is on page {index + 1}.")
            page.draw_rect((24, 80, 200, 160), color=(0, 0, 1), fill=(0.8, 0.9, 1))
            if rotated and index == 1:
                page.set_rotation(90)
        document.xref_set_key(document.pdf_catalog(), "Lang", "(en-US)")
        document.save(
            path,
            encryption=(pymupdf.PDF_ENCRYPT_AES_128 if encrypted else pymupdf.PDF_ENCRYPT_NONE),
            owner_pw="owner-secret" if encrypted else None,
            user_pw=password if encrypted else None,
        )


@pytest.mark.parametrize("kind", ["encrypted_pdf", "invalid_text_layer"])
def test_image_copy_preserves_pages_and_removes_text_and_encryption(tmp_path, kind):
    source = tmp_path / "original.pdf"
    make_pdf(source, encrypted=kind == "encrypted_pdf", rotated=True)
    digest = file_sha256(source)
    result = prepare_image_pdf(source, digest, root=tmp_path / "recovery", kind=kind, dpi=150)
    copy = Path(result["input_pdf_path"])
    assert file_sha256(source) == digest
    assert not PdfReader(copy).is_encrypted
    assert all(not page.extract_text() for page in PdfReader(copy).pages)
    with pymupdf.open(source) as original, pymupdf.open(copy) as image:
        assert len(image) == len(original) == 2
        for before, after in zip(original, image, strict=True):
            assert tuple(after.rect) == pytest.approx(tuple(before.rect))
            # Raster appearance is retained, including the rotated second page.
            a = before.get_pixmap(dpi=150, alpha=False).samples
            b = after.get_pixmap(dpi=150, alpha=False).samples
            assert len(a) == len(b)
            assert sum(abs(x - y) for x, y in zip(a, b, strict=True)) / len(a) < 3
    assert result["page_count"] == 2
    assert (
        prepare_image_pdf(source, digest, root=tmp_path / "recovery", kind=kind, dpi=150)[
            "input_pdf_sha256"
        ]
        == result["input_pdf_sha256"]
    )


def test_password_protected_pdf_does_not_publish_a_copy(tmp_path):
    source = tmp_path / "password.pdf"
    make_pdf(source, encrypted=True, password="required")
    with pytest.raises(FukamiruError, match="パスワード"):
        prepare_image_pdf(
            source, file_sha256(source), root=tmp_path / "recovery", kind="encrypted_pdf", dpi=150
        )
    assert not list((tmp_path / "recovery").rglob("image-only.pdf"))


def test_recovery_runs_in_child_and_reports_all_publication_phases(tmp_path):
    source = tmp_path / "source.pdf"
    make_pdf(source)
    events = []
    result = prepare_image_pdf(
        source,
        file_sha256(source),
        root=tmp_path / "copies",
        kind="invalid_text_layer",
        dpi=72,
        progress=events.append,
    )
    assert result["execution"] == "subprocess"
    assert all(event["worker_pid"] != os.getpid() for event in events)
    assert {e["phase"] for e in events} >= {
        "pdf_recovery_render",
        "pdf_recovery_save",
        "pdf_recovery_validate",
        "pdf_recovery_publish",
    }


def test_recovery_resumes_validated_pages_after_parent_interruption(tmp_path):
    source = tmp_path / "source.pdf"
    make_pdf(source)
    digest = file_sha256(source)
    root = tmp_path / "copies"

    class Interrupted(BaseException):
        pass

    def interrupt(event):
        if event["phase"] == "pdf_recovery_render":
            raise Interrupted

    with pytest.raises(Interrupted):
        prepare_image_pdf(
            source, digest, root=root, kind="invalid_text_layer", dpi=72, progress=interrupt
        )
    page = next(root.rglob("page-000001.pdf"))
    before = (page.stat().st_mtime_ns, file_sha256(page))
    result = prepare_image_pdf(source, digest, root=root, kind="invalid_text_layer", dpi=72)
    assert (page.stat().st_mtime_ns, file_sha256(page)) == before
    assert result["reused_pages"] >= 1
    assert file_sha256(source) == digest


def test_corrupt_page_checkpoint_is_rebuilt_without_rerendering_other_pages(tmp_path):
    source = tmp_path / "source.pdf"
    make_pdf(source)
    digest = file_sha256(source)
    root = tmp_path / "copies"
    result = prepare_image_pdf(source, digest, root=root, kind="invalid_text_layer", dpi=72)
    directory = Path(result["manifest_path"]).parent
    Path(result["manifest_path"]).unlink()
    first = directory / "pages/page-000001.pdf"
    second = directory / "pages/page-000002.pdf"
    unchanged = (second.stat().st_mtime_ns, file_sha256(second))
    first.write_bytes(b"partial PDF")
    recovered = prepare_image_pdf(source, digest, root=root, kind="invalid_text_layer", dpi=72)
    assert recovered["reused_pages"] == 1
    assert (second.stat().st_mtime_ns, file_sha256(second)) == unchanged
    assert len(PdfReader(first).pages) == 1


def test_preparation_attempt_limit_survives_restarts(tmp_path):
    from fukamiru.ocr.pdf_recovery import recovery_directory

    source = tmp_path / "source.pdf"
    make_pdf(source)
    digest = file_sha256(source)
    root = tmp_path / "copies"
    directory = recovery_directory(root, digest, "invalid_text_layer", 72)
    directory.mkdir(parents=True)
    state = {
        "identity": {"original_sha256": digest, "kind": "invalid_text_layer", "dpi": 72},
        "attempts": 4,
        "status": "preparing",
    }
    (directory / "preparation.json").write_text(json.dumps(state), encoding="utf-8")
    for _ in range(2):
        with pytest.raises(FukamiruError, match="exceeded four"):
            prepare_image_pdf(source, digest, root=root, kind="invalid_text_layer", dpi=72)
    assert not (directory / "image-only.pdf").exists()
    assert json.loads((directory / "preparation.json").read_text())["attempts"] == 4


def test_incremental_assembly_resumes_after_saved_batch(tmp_path):
    source = tmp_path / "large.pdf"
    with pymupdf.open() as document:
        for index in range(19):
            page = document.new_page(width=120, height=160)
            page.insert_text((10, 40), f"Evidence page {index + 1}.")
        document.save(source)
    digest = file_sha256(source)
    root = tmp_path / "copies"

    class Interrupted(BaseException):
        pass

    def interrupt(event):
        if event["phase"] == "pdf_recovery_save" and event["completed"] == 16:
            raise Interrupted

    with pytest.raises(Interrupted):
        prepare_image_pdf(
            source, digest, root=root, kind="invalid_text_layer", dpi=72, progress=interrupt
        )
    result = prepare_image_pdf(source, digest, root=root, kind="invalid_text_layer", dpi=72)
    assert result["page_count"] == 19 and result["reused_pages"] == 19
    assert len(PdfReader(result["input_pdf_path"]).pages) == 19
    assert all(not p.extract_text() for p in PdfReader(result["input_pdf_path"]).pages)


def test_recovery_job_exists_before_native_work_and_retains_manual_choice(environment):
    settings, db, managed, run = environment
    source = managed / "source.pdf"
    make_pdf(source)
    service = DocumentOCRService(db, settings)
    args = dict(
        run_id=run,
        source_path=source,
        source_sha256=file_sha256(source),
        source_language="auto",
        recovery_kind="invalid_text_layer",
    )

    def interrupt(*args, **kwargs):
        jobs = service.jobs(run_id=run)
        assert len(jobs) == 1 and service.recovery_pending(jobs[0])
        assert jobs[0]["status"] == "needs_language"
        raise KeyboardInterrupt

    with (
        patch("fukamiru.ocr.service.prepare_image_pdf", side_effect=interrupt),
        pytest.raises(KeyboardInterrupt),
    ):
        service.enqueue(**args)
    pending = service.jobs(run_id=run)[0]
    manual = service.assign_language(pending["id"], "ja")
    assert manual["status"] == "needs_language"  # Input still unprepared.
    ready = service.enqueue(**args)
    assert ready["id"] == pending["id"] and ready["status"] == "queued"
    assert ready["source_language"] == "ja" and ready["language_origin"] == "manual"


def test_interrupted_explicit_language_is_preserved_on_recovery_resume(environment):
    settings, db, managed, run = environment
    source = managed / "source.pdf"
    make_pdf(source)
    service = DocumentOCRService(db, settings)
    with (
        patch("fukamiru.ocr.service.prepare_image_pdf", side_effect=KeyboardInterrupt),
        pytest.raises(KeyboardInterrupt),
    ):
        service.enqueue(
            run_id=run,
            source_path=source,
            source_sha256=file_sha256(source),
            source_language="ja",
            recovery_kind="invalid_text_layer",
        )
    pending = service.jobs(run_id=run)[0]
    ready = service._resume_recovery(pending)
    assert ready["id"] == pending["id"] and ready["source_language"] == "ja"
    assert ready["language_origin"] == "explicit"


def test_failed_native_preparation_records_a_durable_failed_job(environment):
    settings, db, managed, run = environment
    source = managed / "source.pdf"
    make_pdf(source)
    service = DocumentOCRService(db, settings)
    with (
        patch("fukamiru.ocr.service.prepare_image_pdf", side_effect=FukamiruError("broken")),
        pytest.raises(FukamiruError, match="broken"),
    ):
        service.enqueue(
            run_id=run,
            source_path=source,
            source_sha256=file_sha256(source),
            source_language="auto",
            recovery_kind="invalid_text_layer",
        )
    job = service.jobs(run_id=run)[0]
    assert job["status"] == "failed" and job["error"]["type"] == "PDFRecoveryFailed"


def test_corpus_resumes_pending_recovery_without_parsing_original_again(environment):
    settings, db, managed, run = environment
    source = managed / "source.pdf"
    make_pdf(source)
    corpus = CorpusSyncService(db, settings)
    batch = corpus.scan(run)["corpus_batch_id"]
    ocr = DocumentOCRService(db, settings)
    with (
        patch("fukamiru.ocr.service.prepare_image_pdf", side_effect=KeyboardInterrupt),
        pytest.raises(KeyboardInterrupt),
    ):
        ocr.enqueue(
            run_id=run,
            source_path=source,
            source_sha256=file_sha256(source),
            source_language="auto",
            recovery_kind="invalid_text_layer",
        )
    def fresh_ocr(path, language, *, metrics=None, checkpoint_dir=None):
        assert path.name == "image-only.pdf" and language == "en"
        return OCRResult(
            path, file_sha256(path), language, "mistral",
            tuple(OCRPage(n, f"Fresh recovered evidence on page {n}.") for n in (1, 2)),
        )

    with (
        patch("fukamiru.knowledge.corpus.PDFLayoutAnalyzer.prepare") as original_parse,
        patch(
            "fukamiru.ocr.engines.mistral.MistralOCREngine.process", side_effect=fresh_ocr,
        ) as engine,
    ):
        result = corpus.ingest_ready_sources(run, batch)
    original_parse.assert_not_called()
    assert engine.call_count == 1
    assert ocr.jobs(run_id=run)[0]["status"] == "completed"
    assert result["ready_count"] == 1 and result["failed_count"] == 0


def test_original_language_is_available_without_pypdf_aes_backend(tmp_path):
    source = tmp_path / "aes.pdf"
    make_pdf(source, encrypted=True)
    with patch("fukamiru.ocr.service.PdfReader", side_effect=RuntimeError("AES backend")):
        decision = detect_pdf_language(source)
    assert decision.routable and decision.language == "en"
    assert decision.method == "pdf_renderable_declared_language"


def test_sqlite_retry_is_bounded_and_does_not_retry_other_errors():
    calls = []

    def locked():
        calls.append(1)
        raise sqlite3.OperationalError("database is locked")

    with (
        patch("fukamiru.storage.database.time.sleep"),
        pytest.raises(SQLiteRecoveryExhaustedError),
    ):
        retry_sqlite_operation(locked)
    assert len(calls) == 4
    calls.clear()

    def invalid():
        calls.append(1)
        raise sqlite3.OperationalError("no such table: missing")

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        retry_sqlite_operation(invalid)
    assert len(calls) == 1


def test_corpus_lock_recovery_keeps_the_same_item_and_audits_retry(environment):
    settings, db, managed, run = environment
    source = managed / "text.md"
    source.write_text("The evidence for this example is in the document.", encoding="utf-8")
    service = CorpusSyncService(db, settings)
    batch = service.scan(run)["corpus_batch_id"]
    with db.state.connect() as connection:
        item = connection.execute(
            "SELECT id FROM corpus_sync_items WHERE batch_id=?", (batch,)
        ).fetchone()[0]
    original = service._ingest_ready_item_once
    calls = 0

    def operation(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("database is locked")
        return original(*args, **kwargs)

    with (
        patch.object(service, "_ingest_ready_item_once", side_effect=operation),
        patch("fukamiru.storage.database.time.sleep"),
    ):
        service.ingest_ready_sources(run, batch)
    with db.state.connect() as connection:
        assert (
            connection.execute(
                "SELECT status FROM corpus_sync_items WHERE id=?", (item,)
            ).fetchone()[0]
            == "ready"
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM event_log WHERE run_id=? "
                "AND event_type='corpus.item.recovery_retry'",
                (run,),
            ).fetchone()[0]
            == 1
        )
    assert calls == 2


@pytest.mark.parametrize("kind", ["encrypted_pdf", "invalid_text_layer"])
def test_corpus_pdf_failure_joins_durable_ocr_and_returns_to_original_asset(environment, kind):
    settings, db, managed, run = environment
    source = managed / "original.pdf"
    make_pdf(source, encrypted=kind == "encrypted_pdf")
    digest = file_sha256(source)
    service = CorpusSyncService(db, settings)
    batch = service.scan(run)["corpus_batch_id"]
    if kind == "encrypted_pdf":
        target = "fukamiru.knowledge.corpus.PDFLayoutAnalyzer.prepare"
        error = FukamiruError("cryptography>=3.1 is required for AES algorithm")
    else:
        target = "fukamiru.knowledge.service.IngestionService.build_chunks"
        error = EmptyEmbeddingOffsetsError("Embedding tokenizer produced no offsets")
    from fukamiru.knowledge.pdf_layout import PDFLayoutAnalyzer

    original = (PDFLayoutAnalyzer.prepare if kind == "encrypted_pdf"
                else IngestionService.build_chunks)
    failures = []

    def fail_original_once(*args, **kwargs):
        if not failures:
            failures.append(kind)
            raise error
        return original(*args, **kwargs)

    ocr = DocumentOCRService(db, settings)

    def fake_ocr(path, language, *, metrics=None, checkpoint_dir=None):
        assert path != source and path.name == "image-only.pdf" and language == "en"
        assert all(not page.extract_text() for page in PdfReader(path).pages)
        return OCRResult(
            path, file_sha256(path), language, "mistral",
            tuple(OCRPage(n, f"The fresh OCR evidence is on page {n}.") for n in (1, 2)),
        )

    with (
        patch(target, autospec=True, side_effect=fail_original_once),
        patch(
            "fukamiru.ocr.engines.mistral.MistralOCREngine.process", side_effect=fake_ocr,
        ) as engine,
    ):
        result = service.ingest_ready_sources(run, batch)
    completed = ocr.jobs(run_id=run)[0]
    assert completed["source_path"] == str(source.resolve())
    assert completed["source_sha256"] == digest
    assert completed["force_ocr"] == 1 and completed["recovery_kind"] == kind
    assert completed["status"] == "completed" and engine.call_count == 1
    assert failures == [kind]
    assert result["ready_count"] == 1 and result["failed_count"] == 0
    copy = Path(completed["input_pdf_path"])
    assert copy != source and not PdfReader(copy).is_encrypted
    payload = json.loads(Path(completed["output_json_path"]).read_text(encoding="utf-8"))
    assert payload["original_source_sha256"] == digest
    service.ingest_ocr_outputs(run, batch)
    with db.state.connect() as connection:
        item = connection.execute(
            "SELECT * FROM corpus_sync_items WHERE batch_id=?", (batch,)
        ).fetchone()
        assert item["status"] == "ready"
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM pdf_ocr_jobs WHERE run_id=?", (run,)
            ).fetchone()[0]
            == 1
        )
    with db.knowledge.connect() as connection:
        asset = connection.execute(
            "SELECT * FROM source_assets WHERE id=?", (item["source_asset_id"],)
        ).fetchone()
        assert asset["sha256"] == digest and asset["managed_path"] == str(source.resolve())
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM chunks WHERE document_unit_id=?", (item["document_unit_id"],)
            ).fetchone()[0]
            > 0
        )
    assert file_sha256(source) == digest


def test_unresolved_language_stays_waiting_and_duplicate_enqueue_reuses_job(environment):
    settings, db, managed, run = environment
    source = managed / "blank.pdf"
    with pymupdf.open() as doc:
        doc.new_page()
        doc.save(source)
    ocr = DocumentOCRService(db, settings)
    args = dict(
        run_id=run,
        source_path=source,
        source_sha256=file_sha256(source),
        source_language="auto",
        recovery_kind="invalid_text_layer",
    )
    first = ocr.enqueue(**args)
    second = ocr.enqueue(**args)
    assert first["id"] == second["id"]
    assert first["status"] == "needs_language" and first["engine"] is None


def test_zero_offsets_has_a_distinct_recovery_exception():
    with patch("fukamiru.knowledge.service._embedding_tokenizer") as tokenizer:
        tokenizer.return_value.return_value = {"offset_mapping": [], "input_ids": []}
        with pytest.raises(EmptyEmbeddingOffsetsError):
            _token_offsets("\x12 \x11", "model", use_embedding=True, snapshot_path="local")


def test_writer_is_reserved_before_ocr_reads_and_released_after_extraction(environment):
    settings, db, managed, _ = environment
    source = managed / "source.md"
    source.write_text("The original evidence is in this document.", encoding="utf-8")
    process = OCRPipelineService.process
    checks = []

    def inspect_writer(pipeline, **kwargs):
        assert pipeline.connection.in_transaction
        with (
            db.knowledge.connect(busy_timeout_ms=1) as competing_writer,
            pytest.raises(sqlite3.OperationalError, match="locked"),
        ):
            competing_writer.execute("BEGIN IMMEDIATE")
        checks.append(True)
        return process(pipeline, **kwargs)

    with db.knowledge.connect() as connection:
        ingestion = IngestionService(connection, settings)
        asset = ingestion.register(ingestion.locate(str(source)), "en")
        with patch.object(OCRPipelineService, "process", autospec=True, side_effect=inspect_writer):
            artifact = ingestion.extract(asset, source)
        assert artifact.is_file() and not connection.in_transaction
    assert checks == [True]


def test_historic_lock_failure_is_recovered_once_then_exhaustion_is_persistent(environment):
    settings, db, managed, run = environment
    source = managed / "locked.md"
    source.write_text("Original evidence.", encoding="utf-8")
    corpus = CorpusSyncService(db, settings)
    batch = corpus.scan(run)["corpus_batch_id"]
    with db.state.connect() as connection:
        connection.execute(
            "UPDATE corpus_sync_items SET status='failed',error_json=? WHERE batch_id=?",
            (json.dumps({"type": "OperationalError", "message": "database is locked"}), batch),
        )
        connection.commit()
    with (
        patch.object(
            corpus,
            "_ingest_ready_item_once",
            side_effect=sqlite3.OperationalError("database is locked"),
        ) as operation,
        patch("fukamiru.storage.database.time.sleep"),
    ):
        corpus.ingest_ready_sources(run, batch)
        assert operation.call_count == 4
        corpus.ingest_ready_sources(run, batch)
        assert operation.call_count == 4
    with db.state.connect() as connection:
        error = json.loads(
            connection.execute(
                "SELECT error_json FROM corpus_sync_items WHERE batch_id=?",
                (batch,),
            ).fetchone()[0]
        )
        assert error["type"] == "SQLiteRecoveryExhaustedError"


def test_historic_recovery_items_are_prioritized_before_new_files(environment):
    settings, db, managed, run = environment
    for name in ["a-new.md", "z-recovery.md"]:
        (managed / name).write_text("Original evidence.", encoding="utf-8")
    corpus = CorpusSyncService(db, settings)
    batch = corpus.scan(run)["corpus_batch_id"]
    with db.state.connect() as connection:
        changed = connection.execute(
            "UPDATE corpus_sync_items SET status='failed',error_json=? "
            "WHERE batch_id=? AND source_path=?",
            (json.dumps({"type": "OperationalError", "message": "database is locked"}),
             batch, str((managed / "z-recovery.md").resolve())),
        )
        assert changed.rowcount == 1
        connection.commit()
    paths = []

    def record(run_id, item_id, source_path, *, metrics=None):
        paths.append(Path(source_path).name)
        return {"status": "ready"}

    with patch.object(corpus, "_ingest_ready_item_once", side_effect=record):
        corpus.ingest_ready_sources(run, batch)
    assert paths == ["z-recovery.md", "a-new.md"]


def test_recovery_upgrade_preserves_manual_language_and_job_identity(environment):
    settings, db, managed, run = environment
    source = managed / "manual.pdf"
    make_pdf(source)
    ocr = DocumentOCRService(db, settings)
    args = dict(
        run_id=run, source_path=source, source_sha256=file_sha256(source), source_language="auto"
    )
    initial = ocr.enqueue(**args)
    ocr.assign_language(initial["id"], "ja")
    upgraded = ocr.enqueue(**args, recovery_kind="invalid_text_layer")
    assert upgraded["id"] == initial["id"]
    assert upgraded["source_language"] == "ja"
    assert upgraded["language_origin"] == "manual"
    assert upgraded["force_ocr"] == 1 and upgraded["engine"] == "ndlocr_lite"


def test_modified_recovery_input_is_rejected_before_external_ocr(environment):
    settings, db, managed, run = environment
    source = managed / "modified.pdf"
    make_pdf(source)
    ocr = DocumentOCRService(db, settings)
    job = ocr.enqueue(
        run_id=run,
        source_path=source,
        source_sha256=file_sha256(source),
        source_language="en",
        recovery_kind="invalid_text_layer",
    )
    copy = Path(job["input_pdf_path"])
    copy.write_bytes(copy.read_bytes() + b"changed")
    with patch("fukamiru.ocr.service.PDFOCRService.process_file") as process:
        with pytest.raises(FukamiruError, match="変更されています"):
            ocr.process_job(job["id"])
        process.assert_not_called()
    assert ocr.job(job["id"])["status"] == "failed"


def vision_job(environment, *, language="auto"):
    settings, db, managed, run = environment
    settings = replace(settings, ocr=replace(settings.ocr, vision_language_detection=True))
    source = managed / "unreadable.pdf"
    with pymupdf.open() as document:
        document.new_page()
        document.save(source)
    service = DocumentOCRService(db, settings)
    job = service.enqueue(
        run_id=run, source_path=source, source_sha256=file_sha256(source), source_language=language
    )
    return service, job


@pytest.mark.parametrize(
    ("language", "confidence", "readable", "mixed", "expected"),
    [
        ("ja", 0.98, True, False, "queued"),
        ("ja", 0.40, True, False, "needs_language"),
        ("other", 0.99, True, False, "needs_language"),
        ("ja", 0.99, False, False, "needs_language"),
        ("ja", 0.99, True, True, "needs_language"),
        ("ja", 2.0, True, False, "needs_language"),
    ],
)
def test_vision_language_routes_only_high_confidence_readable_supported_body(
    environment, language, confidence, readable, mixed, expected
):
    service, job = vision_job(environment)
    response = {
        "language": language,
        "confidence": confidence,
        "readable": readable,
        "mixed_languages": mixed,
        "evidence": "Visible body prose.",
        "page_languages": [{"page_number": 1, "language": language}],
    }
    with patch(
        "fukamiru.ocr.language_detection.identify_page_image_language", return_value=response
    ) as detector:
        first = service.resolve_language_from_images(job["id"])
        second = DocumentOCRService(
            service.databases, service.settings
        ).resolve_language_from_images(job["id"])
    assert first["status"] == expected and second["status"] == expected
    assert detector.call_count == 1
    if expected == "queued":
        assert first["source_language"] == "ja" and first["engine"] == "ndlocr_lite"
        assert first["language_origin"] == "detected"
    else:
        assert first["source_language"] == "und" and first["engine"] is None


def test_vision_failure_is_persisted_without_repeated_provider_calls(environment):
    service, job = vision_job(environment)
    with patch(
        "fukamiru.ocr.language_detection.identify_page_image_language",
        side_effect=FukamiruError("provider schema refused"),
    ) as detector:
        result = service.resolve_language_from_images(job["id"])
        service.resolve_language_from_images(job["id"])
    assert detector.call_count == 1
    assert result["status"] == "needs_language"
    assert result["language_decision"]["method"] == "pdf_page_image_llm_failed"


def test_vision_does_not_override_explicit_unsupported_or_concurrent_manual_language(environment):
    service, job = vision_job(environment, language="es")
    with patch("fukamiru.ocr.language_detection.identify_page_image_language") as detector:
        assert service.resolve_language_from_images(job["id"])["status"] == "needs_language"
        detector.assert_not_called()
    with service.databases.state.connect() as connection:
        connection.execute(
            "UPDATE pdf_ocr_jobs SET language_decision_json=NULL WHERE id=?", (job["id"],)
        )
        connection.commit()

    def concurrent_human(*args, **kwargs):
        service.assign_language(job["id"], "fr")
        return {
            "language": "ja",
            "confidence": 0.99,
            "readable": True,
            "mixed_languages": False,
            "page_languages": [{"page_number": 1, "language": "ja"}],
        }

    with patch(
        "fukamiru.ocr.language_detection.identify_page_image_language",
        side_effect=concurrent_human,
    ):
        result = service.resolve_language_from_images(job["id"])
    assert result["source_language"] == "fr" and result["language_origin"] == "manual"


def test_vision_schema_is_strict_and_actual_page_images_are_passed_to_transport(environment):
    settings, _, managed, _ = environment
    source = managed / "samples.pdf"
    make_pdf(source)

    def strict(schema):
        if schema.get("type") == "object":
            assert set(schema["required"]) == set(schema["properties"])
            assert schema["additionalProperties"] is False
            for child in schema["properties"].values():
                strict(child)
        if schema.get("type") == "array":
            strict(schema["items"])

    strict(VISION_LANGUAGE_SCHEMA)
    response = {
        "language": "en",
        "confidence": 0.98,
        "readable": True,
        "mixed_languages": False,
        "evidence": "English body text.",
        "page_languages": [{"page_number": n, "language": "en"} for n in (1, 2)],
    }
    with patch.object(CodexTranslationAdapter, "_complete_json", return_value=response) as complete:
        result = identify_page_image_language(settings, source, root=managed / "samples")
    options = complete.call_args.kwargs
    assert options["operation"] == "OCR image language detection"
    assert len(options["image_paths"]) == 2
    assert all(path.read_bytes().startswith(b"\x89PNG") for path in options["image_paths"])
    assert source.name not in complete.call_args.args[0]
    assert result["source_sha256"] == file_sha256(source)


def test_codex_transport_attaches_images_to_provider_command(environment, tmp_path):
    settings, _, _, _ = environment
    adapter = CodexTranslationAdapter(settings)
    image = tmp_path / "page.png"
    with pymupdf.open() as document:
        document.new_page().get_pixmap().save(image)
    commands = []

    class Process:
        returncode = 0

        def __init__(self, args, **kwargs):
            commands.append(args)
            self.output = Path(args[args.index("-o") + 1])

        def communicate(self, prompt, *, timeout):
            self.output.write_text('{"language":"ja"}', encoding="utf-8")
            return "", ""

    with (
        patch(
            "fukamiru.translation.codex.require_codex_cli",
            return_value=SimpleNamespace(resolved_executable="codex.exe"),
        ),
        patch("fukamiru.translation.codex.subprocess.Popen", Process),
        patch.object(adapter, "_prepare_worker_home", return_value=tmp_path),
        patch.object(adapter, "_prepare_windows_ca_bundle", return_value=None),
    ):
        result = adapter._complete_json(
            "Identify language.",
            {
                "type": "object",
                "properties": {"language": {"type": "string"}},
                "required": ["language"],
                "additionalProperties": False,
            },
            operation="OCR image language detection",
            image_paths=[image],
        )
    assert result == {"language": "ja"}
    assert commands[0][commands[0].index("--image") + 1] == str(image.resolve())


@pytest.mark.parametrize("workflow_name", ["corpus_sync", "ingestion"])
def test_ocr_nodes_resolve_language_with_llm_before_waiting_for_human(environment, workflow_name):
    service, job = vision_job(environment)
    response = {
        "language": "ja",
        "confidence": 0.99,
        "readable": True,
        "mixed_languages": False,
        "page_languages": [{"page_number": 1, "language": "ja"}],
    }

    def fake_ocr(path, language, *, metrics=None, checkpoint_dir=None):
        return OCRResult(
            path,
            file_sha256(path),
            language,
            "ndlocr_lite",
            (OCRPage(1, "Fresh Japanese OCR body."),),
        )

    with (
        patch(
            "fukamiru.ocr.language_detection.identify_page_image_language", return_value=response
        ),
        patch("fukamiru.ocr.engines.ndlocr_lite.NDLOCRLiteEngine.process", side_effect=fake_ocr),
    ):
        if workflow_name == "corpus_sync":
            jobs = service.process_run_jobs(job["run_id"])
            assert jobs[0]["status"] == "completed"
        else:
            worker = WorkerRunner(
                service.settings, service.databases, {"ingestion": ingestion_workflow()}
            )
            result = worker._ingestion_run_ocr(SimpleNamespace(), {"ocr_job_id": job["id"]})
            assert not result.waiting_human
            assert result.context["source_language"] == "ja"
            assert result.context["ocr_job_status"] == "completed"


def test_single_pdf_recovery_uses_graph_loop_and_repeats_ocr_audit(environment):
    settings, db, managed, unused_run = environment
    source = managed / "single.pdf"
    make_pdf(source)
    workflow = ingestion_workflow()
    assert workflow.next_nodes("build_chunks", "conditional_success") == ["run_ocr"]
    assert workflow.next_nodes("build_chunks", "success") == ["index_chunks"]
    with db.state.connect() as connection:
        connection.execute("UPDATE runs SET status='cancelled' WHERE id=?", (unused_run,))
        connection.commit()
        run = (
            RunRepository(connection)
            .create(
                workflow_name="ingestion",
                workflow_version="2",
                entry_node="locate_source",
                project_id=None,
                inputs={"source_path": str(source), "source_language": "en"},
            )
            .run_id
        )
    worker = WorkerRunner(
        settings, db, {"ingestion": workflow, "corpus_sync": corpus_sync_workflow()}
    )
    original_chunks = IngestionService.build_chunks
    calls = 0

    def chunks(service, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise EmptyEmbeddingOffsetsError("Embedding tokenizer produced no offsets")
        return original_chunks(service, *args, **kwargs)

    def fake_ocr(path, language, *, metrics=None, checkpoint_dir=None):
        return OCRResult(
            path,
            file_sha256(path),
            language,
            "mistral",
            tuple(OCRPage(n, f"The fresh evidence is on page {n}.") for n in (1, 2)),
        )

    with (
        patch.object(IngestionService, "build_chunks", autospec=True, side_effect=chunks),
        patch("fukamiru.ocr.engines.mistral.MistralOCREngine.process", side_effect=fake_ocr),
    ):
        for _ in range(24):
            if not worker.run_once(lane="ingestion_cpu"):
                break
    with db.state.connect() as connection:
        assert (
            connection.execute("SELECT status FROM runs WHERE id=?", (run,)).fetchone()[0]
            == "completed"
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM node_runs WHERE run_id=? "
                "AND node_id='audit_ocr' AND status='completed'",
                (run,),
            ).fetchone()[0]
            == 2
        )
    assert calls == 2
