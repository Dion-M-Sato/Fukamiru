"""Local-only HTTP API and static Workbench host."""

from __future__ import annotations

import asyncio
import json
import uuid
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path, PureWindowsPath
from typing import Any, Literal

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from fukamiru import __version__
from fukamiru.authoring.decision_ledger import build_decision_ledger
from fukamiru.authoring.folder_input import FolderNoteService
from fukamiru.authoring.policy import enforce_authoring_quality_policy
from fukamiru.authoring.repository import AuthoringRepository
from fukamiru.authoring.service import AuthoringProposalService
from fukamiru.authoring.source_reads import known_work_passages, known_work_read
from fukamiru.bootstrap import bootstrap
from fukamiru.codex_cli import require_codex_cli, resolve_codex_cli
from fukamiru.config import Settings
from fukamiru.defects import DefectRepository
from fukamiru.documents.html_preview import document_response_headers, render_document_html
from fukamiru.errors import FukamiruError
from fukamiru.evaluation import EvaluationService, RepairService
from fukamiru.knowledge.bibliography import source_bibliography
from fukamiru.knowledge.corpus import CorpusSyncService
from fukamiru.knowledge.page_reading import POLICY as PAGE_READING_POLICY
from fukamiru.knowledge.semantic import FaissSemanticIndex
from fukamiru.knowledge.service import KnowledgeSearchService, preflight_embedding_tokenizer
from fukamiru.literature import LiteratureDiscoveryService, OAAcquisitionService
from fukamiru.local_llm import LlamaServerAdapter
from fukamiru.ocr import DocumentOCRService, OCRCanonicalHTMLRenderer, OCRRevisionRepository
from fukamiru.projects.dissertation import DissertationService
from fukamiru.projects.service import ProjectService
from fukamiru.review_feedback import ReviewFeedbackService, feedback_context
from fukamiru.revisions import (
    CandidateApplicationService,
    CollocationRepository,
    HTMLImportService,
    ManuscriptImportService,
    ManuscriptReasoningRepository,
    ManuscriptRewriteService,
    RevisionService,
)
from fukamiru.revisions.sentence_proofreading import CONTRACT as PROOFREADING_CONTRACT
from fukamiru.revisions.sentence_proofreading import digest as proofreading_digest
from fukamiru.storage.database import DatabasePair
from fukamiru.storage.events import Event, EventRepository
from fukamiru.storage.payloads import PayloadStore
from fukamiru.storage.runs import RunRepository, utc_now
from fukamiru.translation import (
    AcademicResearchRepository,
    TranslationAlignmentService,
    TranslationMemoryService,
)
from fukamiru.worker import GpuMutex, WorkerRunner
from fukamiru.workflow.display import (
    workflow_node_metadata,
    workflow_supports_proofreading_patches,
)
from fukamiru.workflow.graph import WorkflowDefinition
from fukamiru.workflow.identity import (
    MANUSCRIPT_GENERATOR,
    is_manuscript_generator_workflow,
)
from fukamiru.workflow.templates import (
    authoring_workflow,
    authoring_workflow_v30,
    authoring_workflow_v31_legacy,
    corpus_sync_workflow,
    corpus_sync_workflow_v1,
    ingestion_workflow,
    ingestion_workflow_v1,
    manuscript_rewrite_workflow,
    manuscript_rewrite_workflow_v2,
    manuscript_rewrite_workflow_v3,
    manuscript_rewrite_workflow_v4,
    manuscript_rewrite_workflow_v5,
    manuscript_rewrite_workflow_v6,
    manuscript_rewrite_workflow_v7,
    manuscript_rewrite_workflow_v8,
    manuscript_rewrite_workflow_v9,
    manuscript_rewrite_workflow_v10,
    manuscript_rewrite_workflow_v11,
    manuscript_rewrite_workflow_v12,
    manuscript_rewrite_workflow_v13,
    manuscript_rewrite_workflow_v16,
    manuscript_rewrite_workflow_v17,
    manuscript_rewrite_workflow_v18,
    manuscript_rewrite_workflow_v19,
    manuscript_rewrite_workflow_v20,
    manuscript_rewrite_workflow_v21,
    manuscript_rewrite_workflow_v22,
    manuscript_rewrite_workflow_v23,
    manuscript_rewrite_workflow_v24,
    manuscript_rewrite_workflow_v25,
    manuscript_rewrite_workflow_v26,
    manuscript_rewrite_workflow_v27,
    manuscript_rewrite_workflow_v28,
    manuscript_rewrite_workflow_v29,
    research_source_ingestion_workflow,
    translation_workflow,
    translation_workflow_v4,
    translation_workflow_v5,
    translation_workflow_v6,
)


class VerifiedBookMetadataCorrectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reference_id: str = Field(min_length=1, max_length=1000)
    title: str = Field(min_length=1, max_length=4000)
    authors: list[str] = Field(min_length=1, max_length=50)
    translators: list[str] = Field(default_factory=list, max_length=50)
    publisher: str = Field(min_length=1, max_length=2000)
    year: str = Field(pattern=r"^\d{4}$")
    isbn: str = Field(min_length=13, max_length=30)
    publication_language: Literal["ja", "en", "fr", "de", "ru"]
    source_kind: Literal["publisher", "library_catalog"]
    source_url: str = Field(min_length=1, max_length=8000)


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_name: str
    project_id: str | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)


class AuthoringFolderPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    folder_path: str = Field(min_length=1)


class ResumeExternalWaitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    authorized_engine_fingerprint: str | None = Field(
        default=None,
        pattern="^[0-9a-f]{64}$",
    )


class CreateProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    root_path: str = Field(min_length=1)


class ProjectPathRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root_path: str = Field(min_length=1)


class ResolveReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: str = Field(pattern="^(approve|reject|revise|defer)$")
    feedback: str = Field(default="", max_length=10000)
    corrected_text: str | None = Field(default=None, max_length=2000000)
    claim_id: str | None = Field(default=None, max_length=200)


class ReviewAnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_id: str = Field(min_length=1, max_length=500)
    answer: str = Field(default="", max_length=10000)
    llm_overreach: bool = False


class ReviewFeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=0)
    answers: list[ReviewAnswerRequest] = Field(default_factory=list, max_length=5000)
    overall_comment: str = Field(default="", max_length=10000)
    submit: bool = False


class CorrectOCRRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    corrections: dict[str, str]
    message: str = Field(default="", max_length=2000)


class StartEvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    revision_id: str = Field(min_length=1)
    max_repair_attempts: int = Field(default=2, ge=0, le=10)


class RepairDefectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: str = Field(default="restore_source_quote", min_length=1, max_length=100)


class LiteratureSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(pattern="^(openalex|crossref|semantic_scholar)$")
    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=20, ge=1, le=100)


class LiteratureReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: str = Field(pattern="^(approve|reject)$")


class CreateDissertationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=500)


class AddOutlineNodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_id: str = Field(min_length=1)
    node_type: str = Field(pattern="^(part|chapter|section|appendix|supplement)$")
    title: str = Field(min_length=1, max_length=500)
    order_index: int = Field(ge=0)


class IncludeRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outline_node_id: str = Field(min_length=1)
    content_document_id: str = Field(min_length=1)
    included_revision_id: str = Field(min_length=1)
    role: str = Field(pattern="^(chapter|appendix|supplement)$")
    order_index: int = Field(ge=0)


class EvaluateDissertationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)


class PreviewDissertationOutlineCSVRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_path: str = Field(min_length=1)
    total_target_characters: int | None = Field(default=None, ge=1)


class CreateHTMLImportPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(min_length=1)
    source_path: str = Field(min_length=1)


class UpdateHTMLImportPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_html: str = Field(min_length=1)


class CreateManuscriptImportPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str | None = Field(default=None, min_length=1)
    project_id: str | None = Field(default=None, min_length=1)
    title: str | None = Field(default=None, min_length=1, max_length=500)
    document_kind: str | None = Field(
        default=None,
        pattern="^(paper|dissertation|translation|note|appendix|supplement)$",
    )
    source_path: str = Field(min_length=1)


class UpdateManuscriptImportPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_html: str = Field(min_length=1)


class CreateManuscriptRewriteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_version: str | None = Field(default=None, pattern="^(22|23|24|25|26|27|28|29)$")
    document_id: str = Field(min_length=1)
    base_revision_id: str | None = Field(default=None, min_length=1)
    source_proposal_id: str | None = Field(default=None, min_length=1)
    outline_node_id: str | None = Field(default=None, min_length=1)
    instructions: str = Field(min_length=1, max_length=20000)
    human_review: str = Field(default="", max_length=50000)
    use_rag: bool = True
    use_online_research: bool = True
    use_llm_jp: bool = True


class SelectReferenceCandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reference_id: str = Field(min_length=1)
    candidate_id: str | None = Field(default=None, min_length=1)
    candidate_ids: list[str] | None = Field(default=None, min_length=1, max_length=100)
    expected_sha256: str = Field(min_length=1)


class ConfirmMachineReferenceMetadataRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_sha256: str = Field(min_length=1)
    is_translation: bool
    is_self_authored: bool | None = None
    is_unpublished: bool | None = None
    translators: list[str] = Field(default_factory=list, max_length=100)
    author: str | None = Field(default=None, min_length=1, max_length=1000)
    title: str | None = Field(default=None, min_length=1, max_length=4000)
    record_type: str | None = Field(default=None, min_length=1, max_length=200)
    year: str | None = Field(default=None, min_length=1, max_length=100)
    venue: str | None = Field(default=None, min_length=1, max_length=2000)
    pages: str | None = Field(default=None, min_length=1, max_length=200)
    volume: str | None = Field(default=None, min_length=1, max_length=200)
    issue: str | None = Field(default=None, min_length=1, max_length=200)
    edition: str | None = Field(default=None, min_length=1, max_length=500)
    publisher: str | None = Field(default=None, min_length=1, max_length=2000)
    container_title: str | None = Field(default=None, min_length=1, max_length=4000)
    publication_place: str | None = Field(default=None, min_length=1, max_length=1000)
    publication_language: str | None = Field(default=None, min_length=2, max_length=100)
    series: str | None = Field(default=None, min_length=1, max_length=2000)
    original_title: str | None = Field(default=None, min_length=1, max_length=4000)
    translated_title: str | None = Field(default=None, min_length=1, max_length=4000)
    doi: str | None = Field(default=None, min_length=1, max_length=500)
    isbn: str | None = Field(default=None, min_length=1, max_length=100)
    url: str | None = Field(default=None, min_length=1, max_length=8000)


class ResolveManuscriptSemanticReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_sha256: str = Field(pattern="^[0-9a-f]{64}$")
    expected_context_sha256: str = Field(pattern="^[0-9a-f]{64}$")
    decision: Literal[
        "confirm_current_interpretation",
        "clarify_interpretation",
        "not_applicable",
    ]
    rationale: str = Field(min_length=1, max_length=10000)


class UpdateManuscriptRewriteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_html: str = Field(min_length=1)


class ConfirmManuscriptRewriteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_sha256: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")
    acknowledge_unresolved: bool = False


class ConfirmAuthoringProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_sha256: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")


class ReviseManuscriptRewriteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_version: str | None = Field(default=None, pattern="^(22|23|24|25|26|27|28|29)$")
    human_review: str = Field(default="", max_length=50000)


class SelectManuscriptPolishCandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(min_length=1)
    scope_type: str = Field(default="document", pattern="^(document|research|author)$")
    scope_key: str | None = Field(default=None, min_length=1, max_length=500)


class SubmitCollocationJudgmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(min_length=1)
    judgment: Literal["accept", "reject"]
    comment: str = Field(default="", max_length=5000)


class AssignOCRLanguageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: Literal["ja", "en", "fr", "ru", "zh", "ko", "de"]


def _workflow_registry() -> dict[str, WorkflowDefinition]:
    definitions = (
        corpus_sync_workflow(),
        ingestion_workflow(),
        research_source_ingestion_workflow(),
        manuscript_rewrite_workflow(),
        translation_workflow(),
        authoring_workflow(),
    )
    return {definition.name: definition for definition in definitions}


def _event_frame(event: Event) -> str:
    data = {
        "event_id": event.event_id,
        "run_id": event.run_id,
        "type": event.event_type,
        "object_type": event.object_type,
        "object_id": event.object_id,
        "payload": event.payload,
        "created_at": event.created_at,
    }
    return (
        f"id: {event.event_id}\n"
        f"event: {event.event_type}\n"
        f"data: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"
    )


def _sync_required_frame(run_id: str, oldest: int, newest: int) -> str:
    data = json.dumps(
        {
            "type": "sync.required",
            "run_id": run_id,
            "reason": "event_history_pruned",
            "oldest_event_id": oldest,
            "latest_event_id": newest,
            "snapshot_url": f"/api/runs/{run_id}/snapshot",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"event: sync.required\ndata: {data}\n\n"


def create_app(settings: Settings, *, start_worker: bool | None = None) -> FastAPI:
    databases = DatabasePair.from_settings(settings)
    workflows = _workflow_registry()
    gpu_mutex = GpuMutex()
    semantic_index = FaissSemanticIndex(databases.knowledge.connect, settings, gpu_mutex=gpu_mutex)
    llm_jp = LlamaServerAdapter(
        settings.llm_jp,
        runtime_root=settings.paths.runtime_root,
        model_name="llm-jp",
    )
    gaperon = LlamaServerAdapter(
        settings.gaperon,
        runtime_root=settings.paths.runtime_root,
        model_name="gaperon",
    )
    llm_jp_base = LlamaServerAdapter(
        settings.llm_jp_base,
        runtime_root=settings.paths.runtime_root,
        model_name="llm-jp-4-8b-base-Q4_K_M",
    )
    worker = WorkerRunner(
        settings,
        databases,
        workflows,
        workflow_versions={
            ("translation", "4"): translation_workflow_v4(),
            ("translation", "5"): translation_workflow_v5(),
            ("translation", "6"): translation_workflow_v6(),
            ("translation", workflows["translation"].version): workflows["translation"],
            ("ingestion", "1"): ingestion_workflow_v1(),
            ("ingestion", workflows["ingestion"].version): workflows["ingestion"],
            ("research_source_ingestion", "1"): workflows["research_source_ingestion"],
            ("corpus_sync", "1"): corpus_sync_workflow_v1(),
            ("corpus_sync", workflows["corpus_sync"].version): workflows["corpus_sync"],
            ("manuscript_rewrite", "2"): manuscript_rewrite_workflow_v2(),
            ("manuscript_rewrite", "3"): manuscript_rewrite_workflow_v3(),
            ("manuscript_rewrite", "4"): manuscript_rewrite_workflow_v4(),
            ("manuscript_rewrite", "5"): manuscript_rewrite_workflow_v5(),
            ("manuscript_rewrite", "6"): manuscript_rewrite_workflow_v6(),
            ("manuscript_rewrite", "7"): manuscript_rewrite_workflow_v7(),
            ("manuscript_rewrite", "8"): manuscript_rewrite_workflow_v8(),
            ("manuscript_rewrite", "9"): manuscript_rewrite_workflow_v9(),
            ("manuscript_rewrite", "10"): manuscript_rewrite_workflow_v10(),
            ("manuscript_rewrite", "11"): manuscript_rewrite_workflow_v11(),
            ("manuscript_rewrite", "12"): manuscript_rewrite_workflow_v12(),
            ("manuscript_rewrite", "13"): manuscript_rewrite_workflow_v13(),
            ("manuscript_rewrite", "16"): manuscript_rewrite_workflow_v16(),
            ("manuscript_rewrite", "17"): manuscript_rewrite_workflow_v17(),
            ("manuscript_rewrite", "18"): manuscript_rewrite_workflow_v18(),
            ("manuscript_rewrite", "19"): manuscript_rewrite_workflow_v19(),
            ("manuscript_rewrite", "20"): manuscript_rewrite_workflow_v20(),
            ("manuscript_rewrite", "21"): manuscript_rewrite_workflow_v21(),
            ("manuscript_rewrite", "22"): manuscript_rewrite_workflow_v22(),
            ("manuscript_rewrite", "23"): manuscript_rewrite_workflow_v23(),
            ("manuscript_rewrite", "24"): manuscript_rewrite_workflow_v24(),
            ("manuscript_rewrite", "25"): manuscript_rewrite_workflow_v25(),
            ("manuscript_rewrite", "26"): manuscript_rewrite_workflow_v26(),
            ("manuscript_rewrite", "27"): manuscript_rewrite_workflow_v27(),
            ("manuscript_rewrite", "28"): manuscript_rewrite_workflow_v28(),
            ("manuscript_rewrite", "29"): manuscript_rewrite_workflow_v29(),
            ("manuscript_rewrite", workflows["manuscript_rewrite"].version): workflows[
                "manuscript_rewrite"
            ],
            ("authoring", "30"): authoring_workflow_v30(),
            ("authoring", "31"): authoring_workflow_v31_legacy(),
            (MANUSCRIPT_GENERATOR, "31"): authoring_workflow(version="31"),
            (MANUSCRIPT_GENERATOR, workflows[MANUSCRIPT_GENERATOR].version): workflows[
                MANUSCRIPT_GENERATOR
            ],
        },
        semantic_index=semantic_index,
        gpu_mutex=gpu_mutex,
        llm_jp=llm_jp,
        llm_jp_base=llm_jp_base,
        gaperon=gaperon,
    )
    should_start_worker = settings.worker.enabled if start_worker is None else start_worker

    def preflight_run_model_dependencies(
        workflow_name: str,
        run_inputs: dict[str, Any],
    ) -> None:
        """Reject Runs whose pinned local embedding dependency cannot be used."""

        ingestion_uses_embedding_tokenizer = (
            workflow_name in {"translation", "ingestion", "corpus_sync"}
            and settings.retrieval.tokenizer_mode == "embedding"
        )
        semantic_requested = settings.retrieval.semantic_enabled and (
            workflow_name == "corpus_sync"
            or workflow_name == "translation"
            or (
                (
                    is_manuscript_generator_workflow(workflow_name)
                    or workflow_name == "manuscript_rewrite"
                )
                and bool(run_inputs.get("use_rag", True))
            )
        )
        semantic_snapshot_exists = False
        if semantic_requested and workflow_name != "corpus_sync":
            semantic_snapshot_exists = semantic_index.status().get("index") == "ready"
        try:
            if ingestion_uses_embedding_tokenizer:
                preflight_embedding_tokenizer(settings)
            if (workflow_name == "corpus_sync" and semantic_requested) or (
                semantic_snapshot_exists
            ):
                semantic_index.preflight()
        except FukamiruError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"Local embedding dependency preflight failed: {exc}",
            ) from exc

        required_codex_models: set[str] = set()
        if is_manuscript_generator_workflow(workflow_name):
            required_codex_models = {"gpt-6-luna", "gpt-5.6-sol"}
        elif workflow_name == "manuscript_rewrite":
            required_codex_models = {settings.translation.manuscript_codex_model.strip()}
        if required_codex_models:
            try:
                require_codex_cli(
                    settings.translation.codex_command,
                    required_models=required_codex_models,
                )
            except FukamiruError as exc:
                raise HTTPException(
                    status_code=503,
                    detail=f"Codex CLI preflight failed: {exc}",
                ) from exc

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        bootstrap(settings)
        if should_start_worker:
            worker.start()
        try:
            yield
        finally:
            worker.stop()
            llm_jp.stop()
            llm_jp_base.stop()
            gaperon.stop()

    app = FastAPI(
        title="Fukamiru",
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.databases = databases
    app.state.workflows = workflows
    app.state.worker = worker
    app.state.gpu_mutex = gpu_mutex

    @app.middleware("http")
    async def disable_workbench_asset_cache(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        if request.method in {"GET", "HEAD"} and not request.url.path.startswith("/api/"):
            # Fukamiru is a locally hosted workbench whose UI is updated in place.
            # A remote/mobile browser must not keep an older index, JavaScript bundle,
            # or stylesheet after the service has been upgraded.
            response.headers["Cache-Control"] = "no-store, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    def apply_translation_checkpoint(
        progress: dict[str, Any], checkpoint: dict[str, Any]
    ) -> dict[str, Any]:
        persisted_completed = int(checkpoint["completed"])
        current_completed = progress["progress"]["completed"]
        if current_completed is None and progress["progress"]["kind"] not in {
            "llm_jp_post_edit_chunks",
            "ingestion_activity",
        }:
            progress["progress"]["completed"] = persisted_completed
            progress["progress"]["completed_characters"] = int(checkpoint["characters"])
            progress["progress"]["updated_at"] = checkpoint["updated_at"]
            total = progress["progress"]["total"]
            if total:
                progress["progress"]["current"] = min(persisted_completed + 1, total)
                progress["progress"]["percentage"] = round(
                    min(100.0, persisted_completed * 100.0 / total), 1
                )
        return progress

    def hydrate_translation_checkpoint(progress: dict[str, Any]) -> dict[str, Any]:
        if progress["run"]["workflow_name"] != "translation":
            return progress
        with databases.knowledge.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS completed,
                       COALESCE(SUM(LENGTH(translated_text)), 0) AS characters,
                       MAX(updated_at) AS updated_at
                FROM translation_batch_checkpoints
                WHERE run_id = ? AND status = 'completed'
                """,
                (progress["run"]["id"],),
            ).fetchone()
        return apply_translation_checkpoint(progress, dict(row))

    def hydrate_translation_checkpoints(
        progress_items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        translation_ids = [
            str(item["run"]["id"])
            for item in progress_items
            if item["run"]["workflow_name"] == "translation"
        ]
        if not translation_ids:
            return progress_items
        placeholders = ",".join("?" for _ in translation_ids)
        with databases.knowledge.connect() as connection:
            checkpoints = {
                str(row["run_id"]): dict(row)
                for row in connection.execute(
                    f"""
                    SELECT run_id, COUNT(*) AS completed,
                           COALESCE(SUM(LENGTH(translated_text)), 0) AS characters,
                           MAX(updated_at) AS updated_at
                    FROM translation_batch_checkpoints
                    WHERE run_id IN ({placeholders}) AND status = 'completed'
                    GROUP BY run_id
                    """,
                    translation_ids,
                )
            }
        for item in progress_items:
            if item["run"]["workflow_name"] != "translation":
                continue
            checkpoint = checkpoints.get(
                str(item["run"]["id"]),
                {"completed": 0, "characters": 0, "updated_at": None},
            )
            apply_translation_checkpoint(item, checkpoint)
        return progress_items

    def workflow_for_run(run: dict[str, Any]) -> WorkflowDefinition | None:
        name = str(run.get("workflow_name") or "")
        definition = workflows.get(name)
        if definition is None or str(definition.version) != str(run.get("workflow_version")):
            return worker.workflow_versions.get((name, str(run.get("workflow_version") or "")))
        return definition

    def workflow_payload(definition: WorkflowDefinition) -> dict[str, Any]:
        payload = definition.model_dump(mode="json")
        for node in payload["nodes"]:
            node.update(workflow_node_metadata(definition, str(node["id"])))
        payload["supports_proofreading_patches"] = workflow_supports_proofreading_patches(
            definition
        )
        return payload

    def hydrate_execution_projection(projection: dict[str, Any]) -> dict[str, Any]:
        run = projection.get("run") or projection
        definition = workflow_for_run(run)
        for key in ("active_nodes", "current_nodes"):
            nodes = projection.get(key) or []
            projection[key] = [
                {
                    **node,
                    **workflow_node_metadata(definition, str(node.get("node_id") or "")),
                }
                for node in nodes
            ]
        progress = projection.get("progress")
        if isinstance(progress, dict) and progress.get("phase"):
            progress["node"] = workflow_node_metadata(definition, str(progress["phase"]))
        if projection.get("patch_processing") is None:
            applicable = workflow_supports_proofreading_patches(definition)
            projection["patch_processing"] = {
                "applicable": applicable,
                "state": "pending" if applicable else "not_applicable",
                "stage": None,
                "total": 0,
                "selected": 0,
                "applied": 0,
                "status_counts": {},
                "empty": False,
                "updated_at": None,
            }
        return projection

    @app.get("/api/liveness")
    async def liveness() -> dict[str, str]:
        """Cheap watchdog probe that never touches databases, CUDA, or model servers."""
        return {"status": "alive", "version": __version__}

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        state_ready = settings.paths.state_db.exists()
        knowledge_ready = settings.paths.knowledge_db.exists()
        embedding_runtime = semantic_index.diagnostics()
        worker_runtime = worker.status()
        codex_runtime = resolve_codex_cli(
            settings.translation.codex_command,
            required_models={"gpt-6-luna", settings.translation.manuscript_codex_model.strip()},
        )
        return {
            "status": "ready" if state_ready and knowledge_ready else "initializing",
            "version": __version__,
            "host": settings.server.host,
            "port": settings.server.port,
            "state_db": "ready" if state_ready else "missing",
            "knowledge_db": "ready" if knowledge_ready else "missing",
            "retrieval_backend": settings.retrieval.backend,
            "lexical_retrieval": "ready",
            "source_reading_policy": PAGE_READING_POLICY,
            "semantic_retrieval": semantic_index.status(),
            "configured_device": embedding_runtime["configured_device"],
            "actual_embedding_device": embedding_runtime["actual_embedding_device"],
            "torch_version": embedding_runtime["torch_version"],
            "torch_cuda_version": embedding_runtime["torch_cuda_version"],
            "cuda_available": embedding_runtime["cuda_available"],
            "gpu_name": embedding_runtime["gpu_name"],
            "managed_sources": {
                "path": str(settings.paths.corpus_root / "managed_sources"),
                "available": (settings.paths.corpus_root / "managed_sources").is_dir(),
            },
            "corpus_sources": {
                "default_root": str(settings.paths.corpus_root.resolve()),
                "directories": ["managed_sources", "import_staging"],
            },
            "features": {
                "manuscript_import": True,
                "manuscript_rewrite": True,
                "semantic_paragraph_planning": True,
                "predicate_relation_resolution": True,
                "constrained_japanese_candidates": True,
                "section_candidate_reranking": True,
                "human_wording_preferences": True,
                "predicate_collocation_v29": True,
                "llm_jp_base_collocation_scorer": True,
                "corpus_collocation_scorer": False,
                "multi_run_progress": True,
                "project_lifecycle": True,
                "project_path_preflight": True,
                "html_preview": True,
            },
            "worker": "running" if should_start_worker else "disabled",
            "workers": worker_runtime,
            "gpu_worker": {
                "status": (
                    "running"
                    if any(
                        worker_runtime["lanes"][lane]["status"] == "running"
                        for lane in ("embedding_gpu", "local_llm_gpu")
                    )
                    else "idle"
                ),
                "embedding_gpu": worker_runtime["lanes"]["embedding_gpu"],
                "local_llm_gpu": worker_runtime["lanes"]["local_llm_gpu"],
                "mutex": worker_runtime["gpu_mutex"],
            },
            "codex": codex_runtime.health_payload(),
            "models": {
                "llm_jp": bool(settings.llm_jp.path and settings.llm_jp.path.exists()),
                "gaperon": bool(settings.gaperon.path and settings.gaperon.path.exists()),
                "llm_jp_base": bool(
                    settings.llm_jp_base.path and settings.llm_jp_base.path.exists()
                ),
            },
            "local_models": {
                "llm_jp": llm_jp.status(),
                "gaperon": gaperon.status(),
                "llm_jp_base": llm_jp_base.status(),
            },
        }

    @app.get("/api/workflows")
    def list_workflows() -> list[dict[str, Any]]:
        return [workflow_payload(definition) for definition in workflows.values()]

    @app.get("/api/projects")
    def list_projects(
        include_archived: bool = Query(default=False),
    ) -> list[dict[str, Any]]:
        with databases.state.connect() as connection:
            return ProjectService(connection).list_projects(include_archived=include_archived)

    @app.post("/api/projects/preflight")
    def preflight_project_path(request: ProjectPathRequest) -> dict[str, Any]:
        with databases.state.connect() as connection:
            try:
                return ProjectService(connection).preflight(root_path=Path(request.root_path))
            except FukamiruError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/projects", status_code=201)
    def create_project(request: CreateProjectRequest) -> dict[str, Any]:
        with databases.state.connect() as connection:
            try:
                project = ProjectService(connection).create(
                    name=request.name,
                    root_path=Path(request.root_path),
                )
            except FukamiruError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return project

    @app.delete("/api/projects/{project_id}")
    def unregister_project(project_id: str) -> dict[str, Any]:
        with databases.state.connect() as connection:
            try:
                return ProjectService(connection).archive(project_id)
            except FukamiruError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/projects/{project_id}/restore")
    def restore_project(project_id: str) -> dict[str, Any]:
        with databases.state.connect() as connection:
            try:
                return ProjectService(connection).restore(project_id)
            except FukamiruError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/dissertations", status_code=201)
    def create_dissertation(request: CreateDissertationRequest) -> dict[str, Any]:
        with databases.state.connect() as connection:
            try:
                return DissertationService(connection).create(
                    project_id=request.project_id, title=request.title
                )
            except FukamiruError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/dissertations/{document_id}")
    def get_dissertation(document_id: str) -> dict[str, Any]:
        with databases.state.connect() as connection:
            try:
                return DissertationService(connection).snapshot(document_id)
            except FukamiruError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/dissertations/{document_id}/outline", status_code=201)
    def add_dissertation_outline_node(
        document_id: str, request: AddOutlineNodeRequest
    ) -> dict[str, Any]:
        with databases.state.connect() as connection:
            try:
                return DissertationService(connection).add_outline_node(
                    dissertation_document_id=document_id,
                    parent_id=request.parent_id,
                    node_type=request.node_type,
                    title=request.title,
                    order_index=request.order_index,
                )
            except FukamiruError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/dissertations/{document_id}/outline-imports/preview", status_code=201)
    def preview_dissertation_outline_csv(
        document_id: str, request: PreviewDissertationOutlineCSVRequest
    ) -> dict[str, Any]:
        with databases.state.connect() as connection:
            try:
                return DissertationService(connection).preview_outline_csv(
                    dissertation_document_id=document_id,
                    source_path=request.source_path,
                    total_target_characters=request.total_target_characters,
                )
            except FukamiruError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post(
        "/api/dissertations/{document_id}/outline-imports/{import_id}/confirm",
        status_code=201,
    )
    def confirm_dissertation_outline_csv(document_id: str, import_id: str) -> dict[str, Any]:
        with databases.state.connect() as connection:
            try:
                return DissertationService(connection).confirm_outline_csv(
                    dissertation_document_id=document_id,
                    import_id=import_id,
                )
            except FukamiruError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/dissertations/{document_id}/memberships", status_code=201)
    def include_dissertation_revision(
        document_id: str, request: IncludeRevisionRequest
    ) -> dict[str, Any]:
        with databases.state.connect() as connection:
            try:
                return DissertationService(connection).include_revision(
                    dissertation_document_id=document_id,
                    outline_node_id=request.outline_node_id,
                    content_document_id=request.content_document_id,
                    included_revision_id=request.included_revision_id,
                    role=request.role,
                    order_index=request.order_index,
                )
            except FukamiruError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/dissertations/{document_id}/refresh")
    def refresh_dissertation(document_id: str) -> dict[str, Any]:
        with databases.state.connect() as connection:
            try:
                changes = DissertationService(connection).refresh_updates(document_id)
            except FukamiruError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"document_id": document_id, "changes": changes}

    @app.post("/api/dissertations/{document_id}/evaluations", status_code=201)
    def evaluate_dissertation(
        document_id: str, request: EvaluateDissertationRequest
    ) -> dict[str, Any]:
        with databases.state.connect() as connection:
            try:
                return DissertationService(connection).evaluate_consistency(
                    run_id=request.run_id,
                    dissertation_document_id=document_id,
                )
            except FukamiruError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/dissertations/{document_id}/evaluations/{evaluation_id}")
    def get_dissertation_evaluation(document_id: str, evaluation_id: str) -> dict[str, Any]:
        with databases.state.connect() as connection:
            try:
                evaluation = DissertationService(connection).get_consistency_evaluation(
                    evaluation_id
                )
            except FukamiruError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        if evaluation["dissertation_document_id"] != document_id:
            raise HTTPException(status_code=404, detail="Evaluation does not belong here")
        return evaluation

    @app.post("/api/html-import-previews", status_code=201)
    def create_html_import_preview(
        request: CreateHTMLImportPreviewRequest,
    ) -> dict[str, Any]:
        with databases.state.connect() as connection:
            try:
                return HTMLImportService(connection).preview(
                    document_id=request.document_id,
                    source_path=Path(request.source_path),
                )
            except FukamiruError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/html-import-previews/{preview_id}")
    def get_html_import_preview(preview_id: str) -> dict[str, Any]:
        with databases.state.connect() as connection:
            try:
                return HTMLImportService(connection).get(preview_id)
            except FukamiruError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.put("/api/html-import-previews/{preview_id}")
    def update_html_import_preview(
        preview_id: str, request: UpdateHTMLImportPreviewRequest
    ) -> dict[str, Any]:
        with databases.state.connect() as connection:
            try:
                return HTMLImportService(connection).update_proposed(
                    preview_id, canonical_html=request.canonical_html
                )
            except FukamiruError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/html-import-previews/{preview_id}/confirm")
    def confirm_html_import_preview(preview_id: str) -> dict[str, Any]:
        with databases.state.connect() as connection:
            try:
                return HTMLImportService(connection).confirm(preview_id)
            except FukamiruError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/manuscript-import-previews", status_code=201)
    def create_manuscript_import_preview(
        request: CreateManuscriptImportPreviewRequest,
    ) -> dict[str, Any]:
        with databases.state.connect() as connection:
            try:
                service = ManuscriptImportService(connection)
                if request.document_id:
                    if request.project_id or request.title or request.document_kind:
                        raise FukamiruError(
                            "Use either an existing document_id or Project manuscript fields"
                        )
                    return service.preview(
                        document_id=request.document_id,
                        source_path=Path(request.source_path),
                    )
                if not request.project_id or not request.title or not request.document_kind:
                    raise FukamiruError(
                        "Project, manuscript title and Content Document kind are required"
                    )
                return service.preview_project_manuscript(
                    project_id=request.project_id,
                    source_path=Path(request.source_path),
                    title=request.title,
                    document_kind=request.document_kind,
                )
            except FukamiruError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/projects/{project_id}/manuscripts")
    def list_project_manuscripts(project_id: str) -> dict[str, Any]:
        with databases.state.connect() as connection:
            try:
                return ManuscriptImportService(connection).list_project_sources(project_id)
            except FukamiruError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/projects/{project_id}/authoring-folders")
    def list_authoring_folders(project_id: str) -> dict[str, Any]:
        with databases.state.connect() as connection:
            try:
                return FolderNoteService(connection).list_folders(project_id)
            except FukamiruError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/projects/{project_id}/authoring-input-preview")
    def preview_authoring_folder(
        project_id: str, request: AuthoringFolderPreviewRequest
    ) -> dict[str, Any]:
        with databases.state.connect() as connection:
            try:
                return FolderNoteService(connection).build(project_id, request.folder_path)
            except FukamiruError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/manuscript-import-previews/{preview_id}")
    def get_manuscript_import_preview(preview_id: str) -> dict[str, Any]:
        with databases.state.connect() as connection:
            try:
                return ManuscriptImportService(connection).get(preview_id)
            except FukamiruError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.put("/api/manuscript-import-previews/{preview_id}")
    def update_manuscript_import_preview(
        preview_id: str, request: UpdateManuscriptImportPreviewRequest
    ) -> dict[str, Any]:
        with databases.state.connect() as connection:
            try:
                return ManuscriptImportService(connection).update_proposed(
                    preview_id, canonical_html=request.canonical_html
                )
            except FukamiruError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/manuscript-import-previews/{preview_id}/confirm")
    def confirm_manuscript_import_preview(preview_id: str) -> dict[str, Any]:
        with databases.state.connect() as connection:
            try:
                return ManuscriptImportService(connection).confirm(preview_id)
            except FukamiruError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/manuscript-rewrites", status_code=202)
    def create_manuscript_rewrite(
        request: CreateManuscriptRewriteRequest,
    ) -> dict[str, Any]:
        workflow = (
            worker.workflow_versions[("manuscript_rewrite", request.workflow_version)]
            if request.workflow_version is not None
            else workflows["manuscript_rewrite"]
        )
        preflight_run_model_dependencies(
            "manuscript_rewrite",
            {"use_rag": request.use_rag},
        )
        with databases.state.connect() as connection:
            try:
                prepared = ManuscriptRewriteService(connection).prepare(
                    document_id=request.document_id,
                    base_revision_id=request.base_revision_id,
                    outline_node_id=request.outline_node_id,
                    source_proposal_id=request.source_proposal_id,
                )
                created = RunRepository(connection).create(
                    workflow_name=workflow.name,
                    workflow_version=workflow.version,
                    entry_node=workflow.entry_node,
                    project_id=str(prepared["project_id"]),
                    inputs={
                        "document_id": request.document_id,
                        "base_revision_id": prepared["base_revision_id"],
                        "source_proposal_id": request.source_proposal_id,
                        "outline_node_id": prepared["outline_node_id"],
                        "instructions": request.instructions,
                        "human_review": request.human_review,
                        "use_rag": request.use_rag,
                        "use_online_research": request.use_online_research,
                        "use_llm_jp": request.use_llm_jp,
                    },
                    resource_lane=next(
                        node.resource_lane
                        for node in workflow.nodes
                        if node.id == workflow.entry_node
                    ),
                )
            except FukamiruError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "run_id": created.run_id,
            "status": "queued",
            "event_id": created.event_id,
            "snapshot_url": f"/api/runs/{created.run_id}/snapshot",
            "proposal_url": f"/api/manuscript-rewrites/by-run/{created.run_id}",
        }

    @app.get("/api/manuscript-rewrites/by-run/{run_id}")
    def get_manuscript_rewrite_by_run(run_id: str) -> dict[str, Any]:
        with databases.state.connect() as connection:
            try:
                return ManuscriptRewriteService(connection).get_by_run(run_id)
            except FukamiruError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/manuscript-rewrites/{proposal_id}")
    def get_manuscript_rewrite(proposal_id: str) -> dict[str, Any]:
        with databases.state.connect() as connection:
            try:
                return ManuscriptRewriteService(connection).get(proposal_id)
            except FukamiruError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/manuscript-rewrites/{proposal_id}/reference-selection")
    def select_reference_candidate(proposal_id: str, request: SelectReferenceCandidateRequest):
        with databases.state.connect() as connection:
            try:
                return ManuscriptRewriteService(connection).select_reference(
                    proposal_id,
                    request.reference_id,
                    request.candidate_id,
                    request.expected_sha256,
                    candidate_ids=request.candidate_ids,
                )
            except FukamiruError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.patch("/api/manuscript-rewrites/{proposal_id}/machine-references/{machine_reference_id}")
    def confirm_machine_reference_metadata(
        proposal_id: str,
        machine_reference_id: str,
        request: ConfirmMachineReferenceMetadataRequest,
    ) -> dict[str, Any]:
        metadata = request.model_dump(
            exclude={
                "expected_sha256",
                "is_translation",
                "is_self_authored",
                "is_unpublished",
                "translators",
            },
            exclude_none=True,
        )
        with databases.state.connect() as connection:
            try:
                return ManuscriptRewriteService(connection).confirm_reference_metadata_v22(
                    proposal_id,
                    machine_reference_id,
                    request.expected_sha256,
                    is_translation=request.is_translation,
                    translators=request.translators,
                    metadata=metadata,
                    is_self_authored=request.is_self_authored,
                    is_unpublished=request.is_unpublished,
                )
            except FukamiruError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.patch("/api/manuscript-rewrites/{proposal_id}/semantic-reviews/{review_id}")
    def resolve_manuscript_semantic_review(
        proposal_id: str,
        review_id: str,
        request: ResolveManuscriptSemanticReviewRequest,
    ) -> dict[str, Any]:
        with databases.state.connect() as connection:
            try:
                return ManuscriptRewriteService(connection).resolve_semantic_review_v22(
                    proposal_id,
                    review_id,
                    request.expected_sha256,
                    request.expected_context_sha256,
                    decision=request.decision,
                    rationale=request.rationale,
                )
            except FukamiruError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/manuscript-rewrites/{proposal_id}/polish-candidates")
    def list_manuscript_polish_candidates(
        proposal_id: str,
        limit: int = Query(default=200, ge=1, le=400),
    ) -> dict[str, Any]:
        with databases.state.connect() as connection:
            service = ManuscriptRewriteService(connection)
            try:
                proposal = service.get(proposal_id)
                repository = ManuscriptReasoningRepository(connection)
                targets = repository.candidate_reviews_for_run(str(proposal["run_id"]), limit=limit)
                preference_count = repository.preference_count(
                    (
                        ("document", str(proposal["document_id"])),
                        ("research", str(proposal["project_id"])),
                    )
                )
            except (FukamiruError, LookupError) as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            "proposal_id": proposal_id,
            "run_id": proposal["run_id"],
            "targets": targets,
            "target_count": len(targets),
            "review_required_count": sum(bool(target.get("review_required")) for target in targets),
            "preference_count": preference_count,
            "human_training_target": (
                settings.translation.manuscript_candidate_human_training_target
            ),
        }

    @app.post("/api/manuscript-polish-targets/{target_id}/select")
    def select_manuscript_polish_candidate(
        target_id: str,
        request: SelectManuscriptPolishCandidateRequest,
    ) -> dict[str, Any]:
        with databases.state.connect() as connection:
            repository = ManuscriptReasoningRepository(connection)
            try:
                target, candidates = repository.polish_target_review(target_id)
            except LookupError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            selected_candidate = next(
                (candidate for candidate in candidates if candidate.id == request.candidate_id),
                None,
            )
            if selected_candidate is None:
                raise HTTPException(status_code=404, detail="Wording candidate not found")
            if selected_candidate.hard_vetoed:
                raise HTTPException(
                    status_code=400,
                    detail="A hard-vetoed wording candidate cannot be selected",
                )
            current_candidate = next(
                (candidate for candidate in candidates if candidate.selected),
                None,
            )
            previous_text = (
                current_candidate.text
                if current_candidate is not None
                else str(target["source_text"])
            )
            try:
                proposal = ManuscriptRewriteService(connection).get_by_run(str(target["run_id"]))
            except FukamiruError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            if str(proposal["status"]) not in {"pending", "needs_human_review"}:
                raise HTTPException(
                    status_code=400,
                    detail="Only a pending manuscript proposal can accept a choice",
                )
            try:
                updated_html = CandidateApplicationService.replace_one_in_proposal(
                    str(proposal["proposed_html"]),
                    block_id=str(target["block_id"]),
                    previous_text=previous_text,
                    candidate_text=selected_candidate.text,
                    context_before=str(target["context_before"]),
                    context_after=str(target["context_after"]),
                )
                updated_proposal = ManuscriptRewriteService(connection).update_proposed(
                    str(proposal["id"]), canonical_html=updated_html, commit=False
                )
                if request.scope_type == "document":
                    scope_key = str(target["document_id"])
                elif request.scope_type == "research":
                    scope_key = str(target["project_id"])
                else:
                    scope_key = str(request.scope_key or "").strip()
                    if not scope_key:
                        raise FukamiruError("An author scope requires an explicit author key")
                preference = repository.record_polish_preference(
                    target_id=target_id,
                    candidate_id=request.candidate_id,
                    scope_type=request.scope_type,
                    scope_key=scope_key,
                    commit=False,
                )
                connection.commit()
            except (FukamiruError, ValueError, LookupError) as exc:
                connection.rollback()
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "target_id": target_id,
            "candidate_id": request.candidate_id,
            "proposal_id": proposal["id"],
            "proposal_sha256": updated_proposal["proposed_sha256"],
            "preference": preference,
        }

    @app.put("/api/manuscript-rewrites/{proposal_id}")
    def update_manuscript_rewrite(
        proposal_id: str, request: UpdateManuscriptRewriteRequest
    ) -> dict[str, Any]:
        with databases.state.connect() as connection:
            try:
                return ManuscriptRewriteService(connection).update_proposed(
                    proposal_id, canonical_html=request.canonical_html
                )
            except FukamiruError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/manuscript-rewrites/{proposal_id}/revise", status_code=202)
    def revise_manuscript_rewrite(
        proposal_id: str, request: ReviseManuscriptRewriteRequest
    ) -> dict[str, Any]:
        """Queue a new rewrite from the current proposal and a human comment."""

        workflow = workflows["manuscript_rewrite"]
        human_review = request.human_review.strip()
        with databases.state.connect() as connection:
            service = ManuscriptRewriteService(connection)
            try:
                source = service.get(proposal_id)
                question_feedback = feedback_context(connection, str(source["run_id"]))
                if not human_review and not (
                    question_feedback["answers"] or question_feedback["overall_comments"]
                ):
                    raise HTTPException(status_code=422, detail="A revision comment is required")
                if question_feedback["answers"] or question_feedback["overall_comments"]:
                    human_review += "\n" + json.dumps(question_feedback, ensure_ascii=False)
                if str(source["lifecycle_status"]) != "pending":
                    raise FukamiruError("Only a pending manuscript proposal can be revised")
                parent_run = connection.execute(
                    "SELECT input_json, workflow_version FROM runs WHERE id = ?",
                    (source["run_id"],),
                ).fetchone()
                if parent_run is None:
                    raise FukamiruError("The source proposal Run no longer exists")
                inherited_version = str(parent_run["workflow_version"])
                selected_version = request.workflow_version or (
                    inherited_version
                    if inherited_version in {"23", "24", "25", "26", "27", "28", "29"}
                    else None
                )
                if selected_version is not None:
                    workflow = worker.workflow_versions[("manuscript_rewrite", selected_version)]
                try:
                    parent_inputs = json.loads(str(parent_run["input_json"] or "{}"))
                except json.JSONDecodeError as exc:
                    raise FukamiruError(
                        "The source proposal Run has invalid input metadata"
                    ) from exc
                prepared = service.prepare(
                    document_id=str(source["document_id"]),
                    base_revision_id=str(source["base_revision_id"]),
                    outline_node_id=(
                        str(source["outline_node_id"]) if source.get("outline_node_id") else None
                    ),
                    source_proposal_id=proposal_id,
                )
                created = RunRepository(connection).create(
                    workflow_name=workflow.name,
                    workflow_version=workflow.version,
                    entry_node=workflow.entry_node,
                    project_id=str(prepared["project_id"]),
                    inputs={
                        "document_id": str(source["document_id"]),
                        "base_revision_id": prepared["base_revision_id"],
                        "source_proposal_id": proposal_id,
                        "outline_node_id": prepared["outline_node_id"],
                        "instructions": str(
                            parent_inputs.get("instructions")
                            or source.get("instructions")
                            or "人間の修正コメントに基づいて原稿を改稿する。"
                        ),
                        "human_review": human_review,
                        "human_question_feedback": question_feedback,
                        "use_rag": bool(parent_inputs.get("use_rag", source.get("use_rag", True))),
                        "use_online_research": bool(parent_inputs.get("use_online_research", True)),
                        "use_llm_jp": bool(parent_inputs.get("use_llm_jp", True)),
                    },
                    resource_lane=next(
                        node.resource_lane
                        for node in workflow.nodes
                        if node.id == workflow.entry_node
                    ),
                )
            except FukamiruError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "run_id": created.run_id,
            "status": "queued",
            "event_id": created.event_id,
            "source_proposal_id": proposal_id,
            "snapshot_url": f"/api/runs/{created.run_id}/snapshot",
            "proposal_url": f"/api/manuscript-rewrites/by-run/{created.run_id}",
        }

    @app.post("/api/manuscript-rewrites/{proposal_id}/confirm")
    def confirm_manuscript_rewrite(
        proposal_id: str,
        request: ConfirmManuscriptRewriteRequest | None = None,
    ) -> dict[str, Any]:
        with databases.state.connect() as connection:
            try:
                return ManuscriptRewriteService(connection).confirm(
                    proposal_id,
                    expected_sha256=request.expected_sha256 if request else None,
                    acknowledge_unresolved=(request.acknowledge_unresolved if request else False),
                )
            except FukamiruError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/manuscript-rewrites/{proposal_id}/discard")
    def discard_manuscript_rewrite(proposal_id: str) -> dict[str, Any]:
        with databases.state.connect() as connection:
            try:
                return ManuscriptRewriteService(connection).discard(proposal_id)
            except FukamiruError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get(
        "/api/manuscript-rewrites/{proposal_id}/draft.html",
        response_class=HTMLResponse,
    )
    def get_manuscript_rewrite_draft_html(proposal_id: str) -> HTMLResponse:
        with databases.state.connect() as connection:
            row = connection.execute(
                """
                SELECT p.proposed_html, d.title
                FROM manuscript_rewrite_proposals p
                JOIN content_documents d ON d.id = p.document_id
                WHERE p.id = ?
                """,
                (proposal_id,),
            ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Rewrite proposal not found")
        return HTMLResponse(
            render_document_html(str(row["proposed_html"]), str(row["title"])),
            headers=document_response_headers(),
        )


    @app.get("/api/documents/{document_id}/revisions")
    def list_document_revisions(document_id: str) -> list[dict[str, Any]]:
        with databases.state.connect() as connection:
            try:
                return RevisionService(connection).history(document_id)
            except FukamiruError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/revisions/{revision_id}/diff")
    def get_revision_diff(revision_id: str) -> dict[str, Any]:
        with databases.state.connect() as connection:
            try:
                return RevisionService(connection).diff(revision_id)
            except FukamiruError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/runs")
    def list_runs(
        limit: int = Query(default=25, ge=1, le=100),
        active_only: bool = Query(default=False),
    ) -> dict[str, Any]:
        with databases.state.connect() as connection:
            runs = RunRepository(connection).list_progress(
                limit=limit,
                active_only=active_only,
            )
        return {
            "runs": [
                hydrate_execution_projection(item) for item in hydrate_translation_checkpoints(runs)
            ],
            "refresh_interval_seconds": 10,
        }

    @app.get("/api/translation/source-files")
    def list_translation_source_files() -> dict[str, Any]:
        """List translated source files independently of the Recent Runs limit."""

        with databases.state.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, input_json FROM runs
                WHERE workflow_name = 'translation'
                ORDER BY created_at DESC, rowid DESC
                """
            ).fetchall()
        files: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            inputs = json.loads(row["input_json"] or "{}")
            source_path = str(inputs.get("source_path") or "").strip().strip('"')
            if not source_path or source_path in seen:
                continue
            seen.add(source_path)
            files.append(
                {
                    "id": str(row["id"]),
                    "workflow_name": "translation",
                    "input": {"source_path": source_path},
                }
            )
        return {"runs": files}

    @app.post("/api/runs", status_code=201)
    def create_run(request: CreateRunRequest) -> dict[str, Any]:
        workflow_name = (
            MANUSCRIPT_GENERATOR
            if request.workflow_name == "authoring"
            else request.workflow_name
        )
        workflow = workflows.get(workflow_name)
        if workflow is None:
            raise HTTPException(status_code=422, detail="Unknown workflow")
        run_inputs = dict(request.inputs)
        if workflow_name == MANUSCRIPT_GENERATOR:
            if not request.project_id:
                raise HTTPException(
                    status_code=422, detail="manuscript_generator requires project_id"
                )
            source_folder = str(run_inputs.get("source_folder") or "").strip()
            if source_folder:
                if str(run_inputs.get("note_text") or "").strip():
                    raise HTTPException(
                        status_code=422,
                        detail="Use either source_folder or note_text",
                    )
                with databases.state.connect() as connection:
                    try:
                        folder_note = FolderNoteService(connection).build(
                            request.project_id, source_folder
                        )
                    except FukamiruError as exc:
                        raise HTTPException(status_code=400, detail=str(exc)) from exc
                run_inputs["note_text"] = folder_note["note_text"]
                run_inputs["source_sections"] = folder_note["sections"]
                run_inputs["source_section_manifest"] = [
                    {key: value for key, value in section.items() if key != "content"}
                    for section in folder_note["sections"]
                ]
                run_inputs["source_toc_path"] = folder_note["toc_path"]
            elif "source_sections" in run_inputs or "source_section_manifest" in run_inputs:
                raise HTTPException(
                    status_code=422,
                    detail="source_sections are created from source_folder",
                )
            if not str(run_inputs.get("note_text") or "").strip():
                raise HTTPException(
                    status_code=422,
                    detail="manuscript_generator requires non-empty note_text",
                )
            requested_total_length = run_inputs.get("requested_total_length")
            if requested_total_length not in (None, ""):
                try:
                    requested_total_length = int(requested_total_length)
                except (TypeError, ValueError) as exc:
                    raise HTTPException(
                        status_code=422,
                        detail="requested_total_length must be a positive integer",
                    ) from exc
                if requested_total_length <= 0:
                    raise HTTPException(
                        status_code=422,
                        detail="requested_total_length must be a positive integer",
                    )
                run_inputs["requested_total_length"] = requested_total_length
            run_inputs = enforce_authoring_quality_policy(run_inputs, workflow.version)
        preflight_run_model_dependencies(workflow_name, run_inputs)
        if workflow_name == MANUSCRIPT_GENERATOR and run_inputs.get("source_folder"):
            with databases.state.connect() as connection:
                try:
                    run_inputs["generated_note_path"] = FolderNoteService(connection).save(
                        request.project_id, str(run_inputs["note_text"])
                    )
                except (FukamiruError, OSError) as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
        if request.workflow_name == "corpus_sync":
            requested_root = str(run_inputs.get("root_path") or "").strip() or None
            try:
                run_inputs["root_path"] = str(
                    CorpusSyncService(databases, settings).resolve_root(requested_root)
                )
            except FukamiruError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        with databases.state.connect() as connection:
            if request.workflow_name == "corpus_sync":
                active = connection.execute(
                    """
                    SELECT id FROM runs
                    WHERE workflow_name = 'corpus_sync'
                      AND status IN ('queued', 'running', 'waiting_human')
                    ORDER BY created_at LIMIT 1
                    """
                ).fetchone()
                if active:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Corpus sync is already active: {active['id']}",
                    )
            try:
                created = RunRepository(connection).create(
                    workflow_name=workflow.name,
                    workflow_version=workflow.version,
                    entry_node=workflow.entry_node,
                    project_id=request.project_id,
                    inputs=run_inputs,
                    resource_lane=next(
                        node.resource_lane
                        for node in workflow.nodes
                        if node.id == workflow.entry_node
                    ),
                )
            except FukamiruError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "run_id": created.run_id,
            "status": "queued",
            "event_id": created.event_id,
            "generated_note_path": run_inputs.get("generated_note_path"),
            "snapshot_url": f"/api/runs/{created.run_id}/snapshot",
            "events_url": f"/api/runs/{created.run_id}/events",
        }

    @app.post(
        "/api/runs/{run_id}/restart-authoring-from-contracts",
        status_code=201,
    )
    def restart_authoring_from_contracts(run_id: str) -> dict[str, Any]:
        """Create a same-version Run from the matching pre-contract snapshot."""

        preflight_run_model_dependencies(MANUSCRIPT_GENERATOR, {})
        entry_node = "lock_section_contracts"
        with databases.state.connect() as connection:
            try:
                source = connection.execute(
                    "SELECT workflow_name,workflow_version FROM runs WHERE id=?",
                    (run_id,),
                ).fetchone()
                if source is not None and not is_manuscript_generator_workflow(
                    source["workflow_name"]
                ):
                    source = None
                if source is None:
                    raise FukamiruError(f"Unknown run: {run_id}")
                version = str(source["workflow_version"])
                workflow = worker.workflow_versions.get((source["workflow_name"], version))
                if workflow is None:
                    raise FukamiruError(f"Unsupported authoring workflow version: {version}")
                resource_lane = next(
                    node.resource_lane for node in workflow.nodes if node.id == entry_node
                )
                created = RunRepository(connection).create_authoring_contract_restart(
                    run_id,
                    workflow_version=workflow.version,
                    entry_node=entry_node,
                    resource_lane=resource_lane,
                )
            except FukamiruError as exc:
                status_code = 404 if str(exc).startswith("Unknown run:") else 409
                raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        return {
            "run_id": created.run_id,
            "status": "queued",
            "event_id": created.event_id,
            "restart_node_id": entry_node,
            "source_run_id": run_id,
            "snapshot_run_id": created.snapshot_run_id,
            "snapshot_node_run_id": created.snapshot_node_run_id,
            "plan_id": created.plan_id,
            "checkpoint_reuse": False,
            "snapshot_url": f"/api/runs/{created.run_id}/snapshot",
            "events_url": f"/api/runs/{created.run_id}/events",
        }

    @app.post(
        "/api/runs/{run_id}/restart-authoring-from-draft",
        status_code=201,
    )
    def restart_authoring_from_draft(run_id: str) -> dict[str, Any]:
        """Create a V31 Run from the source Run's completed pre-draft handoff."""

        preflight_run_model_dependencies(MANUSCRIPT_GENERATOR, {})
        entry_node = "draft_rewrite"
        with databases.state.connect() as connection:
            try:
                source = connection.execute(
                    "SELECT workflow_name,workflow_version FROM runs WHERE id=?",
                    (run_id,),
                ).fetchone()
                if source is not None and not is_manuscript_generator_workflow(
                    source["workflow_name"]
                ):
                    source = None
                if source is None:
                    raise FukamiruError(f"Unknown run: {run_id}")
                version = str(source["workflow_version"])
                workflow = worker.workflow_versions.get((source["workflow_name"], version))
                if workflow is None:
                    raise FukamiruError(f"Unsupported authoring workflow version: {version}")
                resource_lane = next(
                    node.resource_lane for node in workflow.nodes if node.id == entry_node
                )
                created = RunRepository(connection).create_authoring_draft_restart(
                    run_id,
                    workflow_version=workflow.version,
                    entry_node=entry_node,
                    resource_lane=resource_lane,
                )
            except FukamiruError as exc:
                status_code = 404 if str(exc).startswith("Unknown run:") else 409
                raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        return {
            "run_id": created.run_id,
            "status": "queued",
            "event_id": created.event_id,
            "restart_node_id": entry_node,
            "source_run_id": run_id,
            "snapshot_run_id": created.snapshot_run_id,
            "snapshot_node_run_id": created.snapshot_node_run_id,
            "plan_id": created.plan_id,
            "checkpoint_reuse": False,
            "snapshot_url": f"/api/runs/{created.run_id}/snapshot",
            "events_url": f"/api/runs/{created.run_id}/events",
        }

    @app.post("/api/runs/{run_id}/restart-v31-from-draft")
    def restart_v31_run_from_draft(run_id: str) -> dict[str, Any]:
        """Replace an inactive V31 draft tail and restart the same Run in place."""

        preflight_run_model_dependencies(MANUSCRIPT_GENERATOR, {})
        entry_node = "draft_rewrite"
        with databases.state.connect() as connection:
            try:
                source = connection.execute(
                    "SELECT workflow_name,workflow_version FROM runs WHERE id=?",
                    (run_id,),
                ).fetchone()
                if source is None or not is_manuscript_generator_workflow(
                    source["workflow_name"]
                ):
                    raise FukamiruError(f"Unknown run: {run_id}")
                workflow = worker.workflow_versions.get(
                    (source["workflow_name"], str(source["workflow_version"]))
                )
                if workflow is None or str(workflow.version) not in {"31", "32"}:
                    raise FukamiruError(
                        "In-place draft restart requires a V31 or V32 authoring workflow"
                    )
                nodes_by_id = {node.id: node for node in workflow.nodes}
                if entry_node not in nodes_by_id:
                    raise FukamiruError("Authoring workflow has no draft_rewrite node")
                downstream = {entry_node}
                frontier = [entry_node]
                while frontier:
                    source_id = frontier.pop()
                    for edge in workflow.edges:
                        if edge.source == source_id and edge.target not in downstream:
                            downstream.add(edge.target)
                            frontier.append(edge.target)
                result = RunRepository(connection).restart_authoring_from_node(
                    run_id,
                    entry_node=entry_node,
                    downstream_node_ids=downstream,
                    resource_lane=nodes_by_id[entry_node].resource_lane,
                )
            except FukamiruError as exc:
                status_code = 404 if str(exc).startswith("Unknown run:") else 409
                raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        return {
            **result,
            "restart_node_id": entry_node,
            "source_run_id": run_id,
            "snapshot_url": f"/api/runs/{run_id}/snapshot",
            "events_url": f"/api/runs/{run_id}/events",
        }

    @app.post("/api/runs/{run_id}/restart-v31-from-prose")
    def restart_v31_run_from_prose(run_id: str) -> dict[str, Any]:
        """Supersede a completed prose tail and its untouched approval wait."""
        preflight_run_model_dependencies(MANUSCRIPT_GENERATOR, {})
        entry_node = "redraft_body_prose"
        with databases.state.connect() as connection:
            try:
                source = connection.execute(
                    "SELECT workflow_name,workflow_version FROM runs WHERE id=?",
                    (run_id,),
                ).fetchone()
                if source is None or not is_manuscript_generator_workflow(
                    source["workflow_name"]
                ):
                    raise FukamiruError(f"Unknown run: {run_id}")
                workflow = worker.workflow_versions.get(
                    (source["workflow_name"], str(source["workflow_version"]))
                )
                if workflow is None or str(workflow.version) not in {"31", "32"}:
                    raise FukamiruError("Prose restart requires V31 or V32 authoring")
                nodes_by_id = {node.id: node for node in workflow.nodes}
                if entry_node not in nodes_by_id:
                    raise FukamiruError("Authoring workflow has no prose redraft node")
                downstream = {entry_node}
                frontier = [entry_node]
                while frontier:
                    source_id = frontier.pop()
                    for edge in workflow.edges:
                        if edge.source == source_id and edge.target not in downstream:
                            downstream.add(edge.target)
                            frontier.append(edge.target)
                result = RunRepository(connection).restart_authoring_from_node(
                    run_id,
                    entry_node=entry_node,
                    downstream_node_ids=downstream,
                    resource_lane=nodes_by_id[entry_node].resource_lane,
                )
            except FukamiruError as exc:
                status_code = 404 if str(exc).startswith("Unknown run:") else 409
                raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        return {
            **result,
            "restart_node_id": entry_node,
            "source_run_id": run_id,
            "snapshot_url": f"/api/runs/{run_id}/snapshot",
            "events_url": f"/api/runs/{run_id}/events",
        }

    @app.post("/api/runs/{run_id}/restart-v31-from-entry")
    def restart_v31_run_from_entry(run_id: str) -> dict[str, Any]:
        """Clear an inactive waiting/failed V31 Run and restart its first node."""
        preflight_run_model_dependencies(MANUSCRIPT_GENERATOR, {})
        with databases.state.connect() as connection:
            try:
                source = connection.execute(
                    "SELECT workflow_name,workflow_version FROM runs WHERE id=?",
                    (run_id,),
                ).fetchone()
                if source is None or not is_manuscript_generator_workflow(
                    source["workflow_name"]
                ):
                    raise FukamiruError(f"Unknown run: {run_id}")
                workflow = worker.workflow_versions.get(
                    (source["workflow_name"], str(source["workflow_version"]))
                )
                if workflow is None or str(workflow.version) not in {"31", "32"}:
                    raise FukamiruError(
                        "Entry restart requires the V31 or V32 manuscript-generator workflow"
                    )
                entry_node = str(workflow.entry_node)
                entry = next(
                    (node for node in workflow.nodes if node.id == entry_node), None
                )
                if entry is None:
                    raise FukamiruError("Manuscript-generator workflow has no entry node")
                result = RunRepository(connection).restart_v31_from_entry(
                    run_id,
                    entry_node=entry_node,
                    resource_lane=entry.resource_lane,
                )
            except FukamiruError as exc:
                status_code = 404 if str(exc).startswith("Unknown run:") else 409
                raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        return {
            **result,
            "restart_node_id": str(workflow.entry_node),
            "source_run_id": run_id,
            "snapshot_url": f"/api/runs/{run_id}/snapshot",
            "events_url": f"/api/runs/{run_id}/events",
        }

    @app.get("/api/authoring/plans/{plan_id}")
    def authoring_plan(
        plan_id: str,
        revision: int | None = Query(default=None, ge=1),
    ) -> dict[str, Any]:
        try:
            plan = AuthoringRepository(databases.state).load_plan(plan_id, revision)
        except (FukamiruError, KeyError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return plan.model_dump(mode="json")

    @app.get("/api/authoring/proposals/{proposal_id}")
    def authoring_proposal(proposal_id: str) -> dict[str, Any]:
        proposal = AuthoringRepository(databases.state).get_pending_proposal(proposal_id)
        if proposal is None:
            raise HTTPException(status_code=404, detail="Authoring proposal not found")
        proposal.pop("proposed_html", None)
        proposal["draft_html_url"] = (
            f"/api/authoring/proposals/{proposal_id}/draft.html"
            if proposal["status"] == "pending"
            else None
        )
        return proposal

    @app.get(
        "/api/authoring/proposals/{proposal_id}/draft.html",
        response_class=HTMLResponse,
    )
    def authoring_proposal_draft_html(proposal_id: str) -> HTMLResponse:
        proposal = AuthoringRepository(databases.state).get_pending_proposal(proposal_id)
        if proposal is None:
            raise HTTPException(status_code=404, detail="Authoring proposal not found")
        if proposal["status"] != "pending":
            raise HTTPException(status_code=409, detail="Authoring proposal is no longer pending")
        return HTMLResponse(
            render_document_html(str(proposal["proposed_html"]), "原稿案"),
            headers=document_response_headers(),
        )

    @app.post("/api/authoring/proposals/{proposal_id}/confirm")
    def confirm_authoring_proposal(
        proposal_id: str,
        request: ConfirmAuthoringProposalRequest | None = None,
    ) -> dict[str, Any]:
        with databases.state.connect() as connection:
            try:
                return AuthoringProposalService(connection).confirm(
                    proposal_id,
                    expected_sha256=request.expected_sha256 if request else None,
                )
            except FukamiruError as exc:
                if str(exc).startswith("Unknown authoring proposal:"):
                    raise HTTPException(status_code=404, detail=str(exc)) from exc
                raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/authoring/proposals/{proposal_id}/reject")
    def reject_authoring_proposal(proposal_id: str) -> dict[str, Any]:
        with databases.state.connect() as connection:
            try:
                return AuthoringProposalService(connection).reject(proposal_id)
            except FukamiruError as exc:
                if str(exc).startswith("Unknown authoring proposal:"):
                    raise HTTPException(status_code=404, detail=str(exc)) from exc
                raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/runs/{run_id}/snapshot")
    def run_snapshot(run_id: str) -> dict[str, Any]:
        with databases.state.connect() as connection:
            try:
                return hydrate_execution_projection(RunRepository(connection).snapshot(run_id))
            except FukamiruError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/runs/{run_id}/proofreading-candidates")
    def list_proofreading_candidates(
        run_id: str,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        with databases.state.connect() as connection:
            run = connection.execute(
                "SELECT workflow_name,workflow_version,input_json,output_json FROM runs WHERE id=?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise HTTPException(status_code=404, detail="Run not found")
            if run["workflow_name"] != "manuscript_rewrite" or run["workflow_version"] != "29":
                raise HTTPException(status_code=409, detail="Proofreading requires manuscript V29")
            total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM manuscript_proofreading_patches WHERE run_id=?", (run_id,)
                ).fetchone()[0]
            )
            rows = connection.execute(
                "SELECT p.*,s.document_id,s.source_sentence_id,s.block_id,"
                "s.start_offset,s.end_offset "
                "FROM manuscript_proofreading_patches p "
                "JOIN manuscript_proofreading_sentences s ON s.id=p.sentence_id "
                "WHERE p.run_id=? ORDER BY s.block_id,s.sentence_ordinal,p.candidate_ordinal "
                "LIMIT ? OFFSET ?",
                (run_id, limit, offset),
            ).fetchall()
            source_json = run["output_json"] or run["input_json"]
            context = PayloadStore(connection).unpack(json.loads(source_json or "{}"))
        source_html = str(context.get("proposed_html") or "")
        block_text = {}
        if source_html:
            root = ET.fromstring(source_html)
            block_text = {
                str(element.attrib["id"]): "".join(element.itertext())
                for element in root.iter("p")
                if "id" in element.attrib
            }
        records = []
        for row in rows:
            record = dict(row)
            original_block = block_text.get(record["block_id"], "")
            start, end = record["start_offset"], record["end_offset"]
            record["preview_text"] = (
                original_block[:start] + record["normalized_candidate_text"] + original_block[end:]
                if original_block[start:end] == record["original_text"]
                else None
            )
            record["effective_status"] = (
                "stale"
                if record["preview_text"] is None and record["status"] == "clean"
                else record["status"]
            )
            record["effective_apply_allowed"] = bool(
                record["apply_allowed"] and record["preview_text"] is not None
            )
            for key in (
                "generation_seeds_json",
                "raw_changed_spans_json",
                "status_reasons_json",
                "numeric_result_json",
                "attribution_result_json",
                "bibliographic_title_result_json",
                "lint_result_json",
                "luna_result_json",
            ):
                record[key.removesuffix("_json")] = json.loads(record.pop(key))
            records.append(record)
        return {
            "run_id": run_id,
            "total": total,
            "offset": offset,
            "limit": limit,
            "auto_apply": False,
            "records": records,
        }

    @app.post("/api/runs/{run_id}/proofreading-candidates/{patch_id}/select")
    def select_proofreading_candidate(run_id: str, patch_id: str) -> dict[str, Any]:
        with databases.state.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT status,input_json,output_json FROM runs WHERE id=? "
                "AND workflow_name='manuscript_rewrite' "
                "AND workflow_version='29'",
                (run_id,),
            ).fetchone()
            if run is None:
                raise HTTPException(status_code=404, detail="Manuscript V29 Run not found")
            if run["status"] != "completed":
                raise HTTPException(status_code=409, detail="Run has not completed")
            patch = connection.execute(
                "SELECT p.id,p.sentence_id,p.original_text,p.original_hash,p.status,"
                "p.apply_allowed,s.block_id,s.start_offset,s.end_offset "
                "FROM manuscript_proofreading_patches p "
                "JOIN manuscript_proofreading_sentences s ON s.id=p.sentence_id "
                "WHERE p.run_id=? AND p.id=?",
                (run_id, patch_id),
            ).fetchone()
            if patch is None:
                raise HTTPException(status_code=404, detail="Candidate not found")
            if patch["status"] != "clean" or not patch["apply_allowed"]:
                raise HTTPException(status_code=409, detail="Candidate is not clean")
            context = PayloadStore(connection).unpack(
                json.loads(run["output_json"] or run["input_json"] or "{}")
            )
            root = ET.fromstring(str(context["proposed_html"]))
            block = next(
                (
                    element
                    for element in root.iter("p")
                    if element.attrib.get("id") == patch["block_id"]
                ),
                None,
            )
            if block is None:
                raise HTTPException(status_code=409, detail="Source block is missing")
            block_text = "".join(block.itertext())
            original = block_text[patch["start_offset"] : patch["end_offset"]]
            if (
                original != patch["original_text"]
                or proofreading_digest(original) != patch["original_hash"]
            ):
                raise HTTPException(status_code=409, detail="Candidate source is stale")
            connection.execute(
                "UPDATE manuscript_proofreading_patches "
                "SET selected=CASE WHEN id=? THEN 1 ELSE 0 END,updated_at=? "
                "WHERE sentence_id=?",
                (patch_id, utc_now(), patch["sentence_id"]),
            )
            connection.execute(
                "INSERT INTO manuscript_proofreading_logs "
                "(id,run_id,patch_id,sentence_id,stage,status,version,payload_json,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    str(uuid.uuid4()),
                    run_id,
                    patch_id,
                    patch["sentence_id"],
                    "selection",
                    "selected",
                    PROOFREADING_CONTRACT,
                    '{"mode":"explicit_selection"}',
                    utc_now(),
                ),
            )
            EventRepository(connection).append(
                run_id,
                "manuscript.proofreading.candidate.selected",
                object_type="run",
                object_id=run_id,
                payload={"patch_id": patch_id},
            )
        return {"run_id": run_id, "patch_id": patch_id, "selected": True, "auto_applied": False}

    @app.get("/api/runs/{run_id}/collocations")
    def list_run_collocations(run_id: str) -> dict[str, Any]:
        with databases.state.connect() as connection:
            run = connection.execute(
                "SELECT workflow_name, workflow_version FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            if run is None:
                raise HTTPException(status_code=404, detail="Run not found")
            if run["workflow_name"] != "manuscript_rewrite" or int(run["workflow_version"]) < 29:
                raise HTTPException(
                    status_code=409,
                    detail="Collocation evidence is available for manuscript V29 or later",
                )
            records = CollocationRepository(connection).list_run(run_id)
        return {
            "run_id": run_id,
            "workflow_version": str(run["workflow_version"]),
            "auto_apply": False,
            "records": records,
        }

    @app.post("/api/runs/{run_id}/collocations/{frame_id}/judgments", status_code=201)
    def submit_collocation_judgment(
        run_id: str,
        frame_id: str,
        request: SubmitCollocationJudgmentRequest,
    ) -> dict[str, str]:
        with databases.state.connect() as connection:
            try:
                judgment_id = CollocationRepository(connection).save_human_judgment(
                    run_id=run_id,
                    frame_id=frame_id,
                    candidate_id=request.candidate_id,
                    judgment=request.judgment,
                    comment=request.comment,
                )
            except KeyError as exc:
                raise HTTPException(
                    status_code=404, detail="Collocation candidate not found"
                ) from exc
        return {"id": judgment_id, "judgment": request.judgment}

    @app.get("/api/runs/{run_id}/progress")
    def run_progress(
        run_id: str,
        events_limit: int = Query(default=200, ge=1, le=500),
    ) -> dict[str, Any]:
        with databases.state.connect() as connection:
            try:
                progress = RunRepository(connection).progress(run_id, events_limit=events_limit)
            except FukamiruError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        return hydrate_execution_projection(hydrate_translation_checkpoint(progress))

    @app.get("/api/runs/{run_id}/translation-research")
    def translation_research(run_id: str) -> dict[str, Any]:
        with databases.state.connect() as connection:
            row = connection.execute(
                "SELECT workflow_name, input_json, output_json FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        if row is None or str(row["workflow_name"]) != "translation":
            raise HTTPException(status_code=404, detail="Translation run not found")
        inputs = json.loads(row["input_json"] or "{}")
        outputs = json.loads(row["output_json"] or "{}")
        document_unit_id = str(
            outputs.get("document_unit_id") or inputs.get("document_unit_id") or ""
        )
        if not document_unit_id:
            return {
                "run_id": run_id,
                "triggers": [],
                "evidence": [],
                "decisions": [],
                "compliance_findings": [],
            }
        with databases.knowledge.connect() as connection:
            return AcademicResearchRepository(connection).snapshot(
                run_id=run_id,
                document_unit_id=document_unit_id,
                target_language=str(
                    outputs.get("target_language") or inputs.get("target_language") or "ja"
                ),
            )

    @app.post("/api/runs/{run_id}/cancel")
    def cancel_run(run_id: str) -> dict[str, Any]:
        with databases.state.connect() as connection:
            try:
                cancelled = RunRepository(connection).cancel(run_id)
            except FukamiruError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"run_id": run_id, "status": cancelled["status"]}

    @app.post("/api/runs/{run_id}/replay-failed-authoring-node")
    def replay_failed_authoring_node(
        run_id: str, correction: VerifiedBookMetadataCorrectionRequest | None = None,
    ) -> dict[str, Any]:
        with databases.state.connect() as connection:
            repository = RunRepository(connection)
            try:
                repository.get(run_id)
            except FukamiruError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            try:
                if correction is not None:
                    return repository.replay_failed_authoring_node(
                        run_id, verified_book_metadata=correction.model_dump(),
                    )
                return repository.replay_failed_authoring_node(run_id)
            except FukamiruError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/runs/{run_id}/revalidate-authoring-submission")
    def revalidate_authoring_submission(run_id: str) -> dict[str, Any]:
        with databases.state.connect() as connection:
            repository = RunRepository(connection)
            try:
                repository.get(run_id)
            except FukamiruError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            try:
                return repository.revalidate_authoring_submission(run_id)
            except FukamiruError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/runs/{run_id}/recover-v31-provisional-submission-join")
    def recover_v31_provisional_submission_join(run_id: str) -> dict[str, Any]:
        with databases.state.connect() as connection:
            repository = RunRepository(connection)
            try:
                repository.get(run_id)
            except FukamiruError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            try:
                return repository.recover_v31_provisional_submission_join(run_id)
            except FukamiruError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/runs/{run_id}/resume-v31-reader-facing-render-tail")
    def resume_v31_reader_facing_render_tail(run_id: str) -> dict[str, Any]:
        with databases.state.connect() as connection:
            try:
                return RunRepository(connection).resume_v31_reader_facing_render_tail(run_id)
            except FukamiruError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/runs/{run_id}/resume-external")
    def resume_external_wait(
        run_id: str,
        request: ResumeExternalWaitRequest | None = None,
    ) -> dict[str, Any]:
        with databases.state.connect() as connection:
            repository = RunRepository(connection)
            try:
                repository.get(run_id)
            except FukamiruError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            try:
                return repository.resume_external_wait(
                    run_id,
                    authorized_engine_fingerprint=(
                        request.authorized_engine_fingerprint if request else None
                    ),
                )
            except FukamiruError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/search")
    def search_knowledge(
        q: str = Query(min_length=1, max_length=1000),
        limit: int = Query(default=20, ge=1, le=100),
        source_asset_id: str | None = Query(default=None),
        document_unit_id: str | None = Query(default=None),
        unit_type: str | None = Query(default=None),
        language: str | None = Query(default=None),
        material_role: str | None = Query(default=None),
        permission: str | None = Query(default=None),
    ) -> dict[str, Any]:
        metadata_filters = {
            key: value
            for key, value in {
                "source_asset_id": source_asset_id,
                "document_unit_id": document_unit_id,
                "unit_type": unit_type,
                "language": language,
                "material_role": material_role,
                "permission": permission,
            }.items()
            if value is not None
        }
        with databases.knowledge.connect() as connection:
            try:
                results = KnowledgeSearchService(
                    connection,
                    semantic_index=semantic_index,
                    rrf_k=settings.retrieval.hybrid_rrf_k,
                ).search(q, limit=limit, metadata_filters=metadata_filters)
            except FukamiruError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"query": q, "count": len(results), "results": results}

    @app.get("/api/source-assets/{source_asset_id}/passages")
    def read_source_passages(
        source_asset_id: str,
        q: str = Query(min_length=1, max_length=1000),
        limit: int = Query(default=6000, ge=200, le=24000),
    ) -> dict[str, Any]:
        # Same reader used by manuscript research, without starting a Run, OCR,
        # ingestion or embedding. Recovered text retains its own page provenance.
        with databases.knowledge.connect() as connection:
            known = known_work_read(connection, {"source_asset_id": source_asset_id})
        if not known:
            raise HTTPException(status_code=404, detail="Source text not found")
        content = known_work_passages(known, [q], limit=limit)
        return content or {"status": "unavailable", "reason": "no_body_passage"}

    @app.get("/api/source-assets/{source_asset_id}/bibliography")
    def get_source_bibliography(source_asset_id: str) -> dict[str, Any]:
        with databases.knowledge.connect() as connection:
            bibliography = source_bibliography(connection, source_asset_id)
        if bibliography is None:
            raise HTTPException(status_code=404, detail="Source bibliography not found")
        return bibliography

    @app.get("/api/translation-memory")
    def search_translation_memory(
        q: str = Query(min_length=1, max_length=200000),
        source_language: str | None = Query(default=None, max_length=20),
        target_language: str | None = Query(default=None, max_length=20),
        limit: int = Query(default=20, ge=1, le=100),
    ) -> dict[str, Any]:
        with databases.knowledge.connect() as connection:
            results = TranslationMemoryService(connection).search(
                q,
                source_language=source_language,
                target_language=target_language,
                limit=limit,
            )
        return {"query": q, "count": len(results), "results": results}

    @app.get("/api/translation-versions/{version_id}/alignments")
    def inspect_translation_alignments(version_id: str) -> dict[str, Any]:
        with databases.knowledge.connect() as connection:
            try:
                return TranslationAlignmentService(connection).inspect(version_id)
            except FukamiruError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/literature/searches", status_code=201)
    def search_literature(payload: LiteratureSearchRequest) -> dict[str, Any]:
        with databases.knowledge.connect() as connection:
            try:
                return LiteratureDiscoveryService(connection).search(
                    provider=payload.provider,
                    query=payload.query,
                    limit=payload.limit,
                )
            except FukamiruError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/api/literature/candidates")
    def list_literature_candidates(
        status: str = Query(default="pending", pattern="^(pending|approved|rejected|acquired)$"),
    ) -> list[dict[str, Any]]:
        with databases.knowledge.connect() as connection:
            return LiteratureDiscoveryService(connection).list_candidates(status=status)

    @app.post("/api/literature/candidates/{candidate_id}/review")
    def review_literature_candidate(
        candidate_id: str, payload: LiteratureReviewRequest
    ) -> dict[str, Any]:
        with databases.knowledge.connect() as connection:
            try:
                return LiteratureDiscoveryService(connection).review(
                    candidate_id, decision=payload.decision
                )
            except FukamiruError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/literature/candidates/{candidate_id}/acquire", status_code=201)
    def acquire_literature_candidate(candidate_id: str) -> dict[str, Any]:
        with databases.knowledge.connect() as connection:
            try:
                return OAAcquisitionService(
                    connection,
                    corpus_root=settings.paths.corpus_root,
                ).acquire(candidate_id)
            except FukamiruError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/literature/acquisitions/{acquisition_id}")
    def get_literature_acquisition(acquisition_id: str) -> dict[str, Any]:
        with databases.knowledge.connect() as connection:
            try:
                return OAAcquisitionService(
                    connection,
                    corpus_root=settings.paths.corpus_root,
                ).get(acquisition_id)
            except FukamiruError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/corpus/latest")
    def latest_corpus_sync() -> dict[str, Any]:
        snapshot = CorpusSyncService(databases, settings).latest_snapshot()
        return snapshot or {"batch": None, "items": []}

    @app.get("/api/corpus/directories")
    def corpus_directories() -> dict[str, Any]:
        service = CorpusSyncService(databases, settings)
        try:
            directories = service.directories()
        except FukamiruError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "root": str(service.default_root),
            "source_roots": [str(root) for root in service.source_roots(service.default_root)],
            "directories": directories,
        }

    @app.get("/api/corpus/batches/{batch_id}")
    def corpus_sync_snapshot(batch_id: str) -> dict[str, Any]:
        try:
            return CorpusSyncService(databases, settings).snapshot(batch_id)
        except FukamiruError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/ocr/jobs")
    def list_ocr_jobs(run_id: str | None = None) -> dict[str, Any]:
        service = DocumentOCRService(databases, settings)
        return {"root": str(service.root), "jobs": service.jobs(run_id=run_id)}

    @app.post("/api/ocr/jobs/{job_id}/language")
    def assign_ocr_language(job_id: str, payload: AssignOCRLanguageRequest) -> dict[str, Any]:
        try:
            return DocumentOCRService(databases, settings).assign_language(job_id, payload.language)
        except FukamiruError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/ocr/jobs/{job_id}/run")
    def run_ocr_job(job_id: str) -> dict[str, Any]:
        try:
            return DocumentOCRService(databases, settings).process_job(job_id)
        except FukamiruError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/reviews")
    def list_reviews(status: str = Query(default="open")) -> list[dict[str, Any]]:
        with databases.state.connect() as connection:
            rows = connection.execute(
                """
                SELECT q.*, n.status AS node_status
                FROM review_queue_entries q
                LEFT JOIN node_runs n ON n.id = q.node_run_id
                WHERE q.status = ?
                  AND (
                    q.entry_kind != 'human_review' OR q.status != 'open'
                    OR q.node_run_id IS NULL
                    OR n.status = 'waiting_human'
                  )
                ORDER BY
                    CASE WHEN q.status = 'approved'
                        THEN COALESCE(q.resolved_at, q.created_at) END DESC,
                    CASE WHEN q.status != 'approved' THEN q.created_at END
                """,
                (status,),
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item.pop("entry_kind", None)
                item.pop("node_status", None)
                source_path = str(item.pop("source_path") or "").strip().strip('"')
                if not item.get("source_title") and source_path:
                    item["source_title"] = (
                        PureWindowsPath(source_path).name
                        if "\\" in source_path
                        else Path(source_path).name
                    )
                output_path = str(item.get("output_path") or "").strip()
                item["output_folder_path"] = (
                    str(
                        PureWindowsPath(output_path).parent
                        if "\\" in output_path
                        else Path(output_path).parent
                    )
                    if output_path
                    else None
                )
                item["options"] = json.loads(item.pop("options_json"))
                raw_response = item.pop("response_json")
                item["response"] = json.loads(raw_response) if raw_response else None
                item["validation_valid"] = bool(item.get("validation_valid"))
                if item.get("object_type") == "authoring_graph_invalid_reference":
                    node_output = connection.execute(
                        "SELECT output_json FROM node_runs WHERE id=?",
                        (item["node_run_id"],),
                    ).fetchone()
                    if node_output and node_output["output_json"]:
                        packet = json.loads(node_output["output_json"]).get(
                            "argument_graph_human_review", {}
                        )
                        item["graph_review"] = {
                            "batch_number": packet.get("batch_number"),
                            "problems": packet.get("problems", []),
                            "invalid_scopes": packet.get("invalid_scopes", []),
                            "invalid_edges": packet.get("invalid_edges", []),
                            "invalid_node_claims": packet.get("invalid_node_claims", []),
                        }
                if item.get("object_type") == "authoring_draft_source_unresolved":
                    node_output = connection.execute(
                        "SELECT output_json FROM node_runs WHERE id=?",
                        (item["node_run_id"],),
                    ).fetchone()
                    if node_output and node_output["output_json"]:
                        packet = json.loads(node_output["output_json"]).get(
                            "authoring_draft_source_review", {}
                        )
                        item["draft_source_review"] = {
                            "evidence_nodes": packet.get("evidence_nodes", []),
                        }
                if item.get("object_type") == "authoring_plan":
                    target = connection.execute(
                        """
                        SELECT d.id AS document_id, d.title AS source_title,
                               p.name AS project_name
                        FROM authoring_plans a
                        JOIN content_documents d ON d.id = a.document_id
                        LEFT JOIN projects p ON p.id = d.project_id
                        WHERE a.id = ?
                        """,
                        (item["object_id"],),
                    ).fetchone()
                    if target:
                        item.update(dict(target))
                    if status != "open":
                        result.append(item)
                        continue
                    audit = connection.execute(
                        """
                        SELECT id, attempt, output_json
                        FROM node_runs
                        WHERE run_id = ? AND node_id = 'audit_structure'
                          AND started_at <= ?
                        ORDER BY started_at DESC
                        LIMIT 1
                        """,
                        (item["run_id"], item["created_at"]),
                    ).fetchone()
                    item["audit_findings"] = []
                    if audit and audit["output_json"]:
                        output = json.loads(audit["output_json"])
                        context = PayloadStore(connection).unpack(
                            {
                                "audit_findings": output.get("audit_findings"),
                                "section_contracts": output.get("section_contracts"),
                            }
                        )
                        section_titles = {
                            str(section.get("section_id")): str(section.get("title") or "")
                            for section in context.get("section_contracts") or []
                            if isinstance(section, dict)
                        }
                        item["audit_attempt"] = audit["attempt"]
                        item["audit_findings"] = [
                            {
                                "severity": finding.get("severity"),
                                "code": finding.get("code"),
                                "object_id": finding.get("object_id"),
                                "section_title": section_titles.get(
                                    str(finding.get("object_id")), ""
                                ),
                                "message": finding.get("message"),
                            }
                            for finding in context.get("audit_findings") or []
                            if isinstance(finding, dict)
                        ]
                result.append(item)
            return result

    @app.post("/api/evaluations", status_code=201)
    def start_evaluation(payload: StartEvaluationRequest) -> dict[str, Any]:
        with (
            databases.state.connect() as state_connection,
            databases.knowledge.connect() as knowledge_connection,
        ):
            try:
                return EvaluationService(state_connection, knowledge_connection).evaluate(
                    run_id=payload.run_id,
                    revision_id=payload.revision_id,
                    max_repair_attempts=payload.max_repair_attempts,
                )
            except FukamiruError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/evaluations/{evaluation_id}")
    def get_evaluation(evaluation_id: str) -> dict[str, Any]:
        with (
            databases.state.connect() as state_connection,
            databases.knowledge.connect() as knowledge_connection,
        ):
            try:
                return EvaluationService(state_connection, knowledge_connection).get(evaluation_id)
            except FukamiruError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/evaluations/{evaluation_id}/defects/{defect_id}/repair")
    def repair_evaluation_defect(
        evaluation_id: str,
        defect_id: str,
        payload: RepairDefectRequest,
    ) -> dict[str, Any]:
        with (
            databases.state.connect() as state_connection,
            databases.knowledge.connect() as knowledge_connection,
        ):
            try:
                return RepairService(state_connection, knowledge_connection).repair(
                    evaluation_id=evaluation_id,
                    defect_id=defect_id,
                    strategy=payload.strategy,
                )
            except FukamiruError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/defects")
    def list_defects(run_id: str | None = Query(default=None)) -> list[dict[str, Any]]:
        with databases.state.connect() as connection:
            defects = DefectRepository(connection).list_open_summaries(run_id=run_id)
            run_ids = sorted({str(item["run_id"]) for item in defects})
            run_metadata: dict[str, dict[str, Any]] = {}
            if run_ids:
                placeholders = ",".join("?" for _ in run_ids)
                rows = connection.execute(
                    f"""
                    SELECT id, workflow_name, status, input_json
                    FROM runs WHERE id IN ({placeholders})
                    """,
                    run_ids,
                ).fetchall()
                run_metadata = {
                    str(row["id"]): {
                        "workflow_name": row["workflow_name"],
                        "run_status": row["status"],
                        "input": json.loads(row["input_json"] or "{}"),
                    }
                    for row in rows
                }
            document_ids = sorted(
                {
                    str(metadata["input"].get("document_id"))
                    for metadata in run_metadata.values()
                    if metadata["input"].get("document_id")
                }
            )
            document_titles: dict[str, str] = {}
            if document_ids:
                placeholders = ",".join("?" for _ in document_ids)
                rows = connection.execute(
                    f"SELECT id, title FROM content_documents WHERE id IN ({placeholders})",
                    document_ids,
                ).fetchall()
                document_titles = {str(row["id"]): str(row["title"]) for row in rows}

        source_asset_ids = sorted(
            {str(item["source_asset_id"]) for item in defects if item.get("source_asset_id")}
        )
        source_paths: dict[str, str] = {}
        if source_asset_ids:
            placeholders = ",".join("?" for _ in source_asset_ids)
            with databases.knowledge.connect() as connection:
                rows = connection.execute(
                    f"""
                    SELECT source_asset_id, path FROM source_locations
                    WHERE source_asset_id IN ({placeholders}) AND is_primary = 1
                    """,
                    source_asset_ids,
                ).fetchall()
                source_paths = {str(row["source_asset_id"]): str(row["path"]) for row in rows}

        for defect in defects:
            metadata = run_metadata.get(str(defect["run_id"]), {})
            inputs = metadata.get("input") or {}
            document_id = str(inputs.get("document_id") or "")
            source_asset_id = str(defect.get("source_asset_id") or "")
            source_path = (
                str(inputs.get("source_path") or source_paths.get(source_asset_id) or "")
                .strip()
                .strip('"')
            )
            path_title = (
                PureWindowsPath(source_path).name if "\\" in source_path else Path(source_path).name
            )
            source_title = document_titles.get(document_id) or path_title
            if not source_title:
                source_title = f"{defect.get('object_type') or '対象'} {defect.get('object_id')}"
            defect["workflow_name"] = metadata.get("workflow_name") or "unknown"
            defect["run_status"] = metadata.get("run_status") or "unknown"
            defect["source_title"] = source_title
            defect["source_group_id"] = (
                f"document:{document_id}"
                if document_id
                else f"source_asset:{source_asset_id}"
                if source_asset_id
                else f"source_path:{source_path.casefold()}"
                if source_path
                else f"object:{defect.get('object_type')}:{defect.get('object_id')}"
            )
        return defects

    @app.get("/api/defects/{defect_id}")
    def get_defect(defect_id: str) -> dict[str, Any]:
        with databases.state.connect() as connection:
            try:
                defect = DefectRepository(connection).get(defect_id)
            except FukamiruError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        source_asset_id = defect.get("source_asset_id")
        source: dict[str, Any] | None = None
        if source_asset_id:
            with databases.knowledge.connect() as connection:
                asset = connection.execute(
                    """
                    SELECT a.id, a.media_type, a.source_language, a.ingestion_status,
                           l.path
                    FROM source_assets a
                    LEFT JOIN source_locations l
                      ON l.source_asset_id = a.id AND l.is_primary = 1
                    WHERE a.id = ? LIMIT 1
                    """,
                    (source_asset_id,),
                ).fetchone()
                if asset is not None:
                    source = dict(asset)
                    page_available = None
                    if defect.get("page_start"):
                        page_available = connection.execute(
                            """
                            SELECT 1
                            FROM ocr_revision_pages p
                            JOIN ocr_text_revisions r ON r.id = p.revision_id
                            WHERE r.source_asset_id = ? AND p.page_number = ? LIMIT 1
                            """,
                            (source_asset_id, defect["page_start"]),
                        ).fetchone()
                    source["page_url"] = (
                        f"/api/source-assets/{source_asset_id}/pages/{defect['page_start']}"
                        if page_available
                        else None
                    )
        return {**defect, "source": source}

    @app.get("/api/review-feedback-history")
    def review_feedback_history() -> list[dict[str, Any]]:
        with databases.state.connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT f.review_id AS id,f.run_id,COALESCE(h.object_type,q.object_type) "
                "AS object_type,COALESCE(h.object_id,q.object_id) AS object_id,"
                "COALESCE(h.status,q.status) AS status,COALESCE(h.question,q.question) "
                "AS question,q.source_title FROM review_feedback f "
                "LEFT JOIN human_reviews h ON h.id=f.review_id "
                "LEFT JOIN review_queue_entries q ON q.id=f.review_id "
                "WHERE f.submitted_revision IS NOT NULL ORDER BY f.submitted_at DESC"
            )]

    @app.get("/api/reviews/{review_id}/feedback")
    def get_review_feedback(review_id: str) -> dict[str, Any]:
        with databases.state.connect() as connection:
            try:
                result = ReviewFeedbackService(connection).get(review_id)
                connection.commit()
                return result
            except FukamiruError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.put("/api/reviews/{review_id}/feedback")
    def save_review_feedback(review_id: str, request: ReviewFeedbackRequest) -> dict[str, Any]:
        with databases.state.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                result = ReviewFeedbackService(connection).save(
                    review_id, revision=request.revision,
                    answers=[item.model_dump() for item in request.answers],
                    overall_comment=request.overall_comment, submit=request.submit,
                )
                connection.commit()
                return result
            except FukamiruError as exc:
                connection.rollback()
                raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/reviews/{review_id}/resolve")
    def resolve_review(review_id: str, request: ResolveReviewRequest) -> dict[str, Any]:
        try:
            result = worker.resolve_review(
                review_id,
                request.decision,
                feedback=request.feedback,
                corrected_text=request.corrected_text,
                claim_id=request.claim_id,
            )
        except FukamiruError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"review_id": review_id, "decision": request.decision, **result}

    @app.get("/api/reviews/{review_id}/argument-ledger")
    def authoring_argument_ledger(review_id: str) -> dict[str, Any]:
        with databases.state.connect() as connection:
            row = connection.execute(
                """SELECT n.output_json FROM human_reviews h
                   JOIN node_runs n ON n.id = h.node_run_id
                   WHERE h.id = ? AND h.object_type = 'authoring_submission'""",
                (review_id,),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Authoring review not found")
            context = PayloadStore(connection).unpack(json.loads(row["output_json"] or "{}"))
            return build_decision_ledger(context)

    @app.get("/api/reviews/{review_id}/evidence-review")
    def authoring_evidence_review(review_id: str) -> dict[str, Any]:
        with databases.state.connect() as connection:
            row = connection.execute(
                """SELECT n.output_json FROM human_reviews h
                   JOIN node_runs n ON n.id = h.node_run_id
                   WHERE h.id = ? AND h.object_type = 'authoring_submission'""",
                (review_id,),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Authoring review not found")
            context = PayloadStore(connection).unpack(json.loads(row["output_json"] or "{}"))
            review_items = [
                item
                for item in context.get("human_review_items") or []
                if isinstance(item, dict)
            ]
            return {
                "schema_version": str(
                    dict(context.get("fixed_assets") or {}).get("schema_version")
                    or "31.1"
                ),
                "result_status": str(context.get("authoring_result_status") or "completed"),
                "manuscript_hash": str(context.get("authoring_proposal_sha256") or ""),
                "review_items": review_items,
                "review_report_artifact_id": context.get("review_report_artifact_id"),
                "review_report_path": context.get("review_report_path"),
            }

    @app.post("/api/reviews/{review_id}/recover-output")
    def recover_approved_review_output(review_id: str) -> dict[str, Any]:
        try:
            return worker.recover_approved_review_output(review_id)
        except FukamiruError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/artifacts/{artifact_id}", response_model=None)
    def get_artifact(artifact_id: str) -> FileResponse | HTMLResponse:
        with databases.state.connect() as connection:
            row = connection.execute(
                "SELECT path, media_type, title FROM run_artifacts WHERE id = ?", (artifact_id,)
            ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Artifact not found")
        path = Path(row["path"])
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Artifact file is missing")
        if row["media_type"] == "text/html":
            return HTMLResponse(
                render_document_html(path.read_text(encoding="utf-8"), str(row["title"])),
                headers=document_response_headers(),
            )
        return FileResponse(path, media_type=row["media_type"])


    @app.get("/api/ocr/revisions/{revision_id}")
    def get_ocr_revision(revision_id: str) -> dict[str, Any]:
        with databases.knowledge.connect() as connection:
            try:
                return OCRRevisionRepository(connection).snapshot(revision_id)
            except FukamiruError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/ocr/revisions/{revision_id}/html", response_class=HTMLResponse)
    def get_ocr_revision_html(
        revision_id: str,
        title: str = Query(default="Source document", min_length=1, max_length=500),
    ) -> HTMLResponse:
        with databases.knowledge.connect() as connection:
            try:
                revision = OCRRevisionRepository(connection).snapshot(revision_id)
            except FukamiruError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        rendered = OCRCanonicalHTMLRenderer().render(revision, title=title)
        return HTMLResponse(rendered)

    @app.post("/api/ocr/revisions/{revision_id}/corrections", status_code=201)
    def correct_ocr_revision(revision_id: str, request: CorrectOCRRequest) -> dict[str, Any]:
        if any(len(text) > 500000 for text in request.corrections.values()):
            raise HTTPException(status_code=422, detail="An OCR block correction is too large")
        with databases.knowledge.connect() as connection:
            repository = OCRRevisionRepository(connection)
            try:
                corrected_id = repository.create_human_correction(
                    parent_revision_id=revision_id,
                    corrections=request.corrections,
                    message=request.message,
                )
                corrected = repository.snapshot(corrected_id)
            except FukamiruError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "revision_id": corrected_id,
            "parent_revision_id": revision_id,
            "layer": corrected["layer"],
            "protected_block_ids": [
                block["stable_block_id"]
                for block in corrected["blocks"]
                if block["is_human_protected"]
            ],
            "html_url": f"/api/ocr/revisions/{corrected_id}/html",
        }

    @app.get("/api/source-assets/{source_asset_id}/pages/{page_number}")
    def open_source_page(source_asset_id: str, page_number: int) -> RedirectResponse:
        if page_number < 1:
            raise HTTPException(status_code=404, detail="Source page not found")
        with databases.knowledge.connect() as connection:
            exists = connection.execute(
                """
                SELECT 1
                FROM ocr_revision_pages p
                JOIN ocr_text_revisions r ON r.id = p.revision_id
                WHERE r.source_asset_id = ? AND p.page_number = ?
                LIMIT 1
                """,
                (source_asset_id, page_number),
            ).fetchone()
        if exists is None:
            raise HTTPException(status_code=404, detail="Source page not found")
        return RedirectResponse(
            f"/api/source-assets/{source_asset_id}/file#page={page_number}",
            status_code=307,
        )

    @app.get("/api/source-assets/{source_asset_id}/file")
    def get_source_file(source_asset_id: str) -> FileResponse:
        with databases.knowledge.connect() as connection:
            row = connection.execute(
                """
                SELECT l.path, a.media_type
                FROM source_assets a
                JOIN source_locations l ON l.source_asset_id = a.id
                WHERE a.id = ? AND l.is_primary = 1 AND l.is_available = 1
                ORDER BY l.id DESC LIMIT 1
                """,
                (source_asset_id,),
            ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Available source file not found")
        path = Path(str(row["path"]))
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Source file is missing")
        return FileResponse(path, media_type=str(row["media_type"]))

    @app.get("/api/runs/{run_id}/events")
    async def run_events(
        run_id: str,
        request: Request,
        after: int | None = Query(default=None, ge=0),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        try:
            cursor = int(last_event_id) if last_event_id is not None else int(after or 0)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Last-Event-ID") from exc

        with databases.state.connect() as connection:
            try:
                RunRepository(connection).get(run_id)
            except FukamiruError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

        async def stream() -> AsyncIterator[str]:
            current = cursor
            with databases.state.connect() as connection:
                events = EventRepository(connection)
                oldest, newest = events.bounds(run_id)
                if current and oldest is not None and current < oldest - 1:
                    yield _sync_required_frame(run_id, oldest, newest or oldest)
                    return
                while not await request.is_disconnected():
                    pending = events.after(run_id, current, limit=500)
                    if pending:
                        for event in pending:
                            current = event.event_id
                            yield _event_frame(event)
                        continue
                    yield ": keep-alive\n\n"
                    await asyncio.sleep(1.0)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    repository_root = Path(__file__).resolve().parents[3]
    ui_root = repository_root / "ui"
    workflow_diagram_root = repository_root / "docs" / "diagrams"
    rag_workflow_report = workflow_diagram_root / "knowledge-ingestion.html"

    @app.get("/workflow-reports/knowledge-ingestion.html")
    def get_rag_creation_workflow_report() -> FileResponse:
        if not rag_workflow_report.is_file():
            raise HTTPException(status_code=404, detail="RAG workflow diagram not found")
        return FileResponse(rag_workflow_report, media_type="text/html; charset=utf-8")

    app.mount(
        "/workflow-diagrams",
        StaticFiles(directory=workflow_diagram_root),
        name="workflow-diagrams",
    )
    app.mount("/", StaticFiles(directory=ui_root, html=True), name="workbench")
    return app
