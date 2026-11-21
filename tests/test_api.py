from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pypdf import PdfWriter
from pypdf.generic import NameObject, TextStringObject

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover - dependency installation is tested in the dev environment
    TestClient = None

from fukamiru.api.app import create_app
from fukamiru.authoring.models import AuthoringPlan, ResearchQuestion
from fukamiru.authoring.repository import AuthoringRepository
from fukamiru.codex_cli import CodexCLIResolution
from fukamiru.config import load_settings
from fukamiru.defects import DefectRepository
from fukamiru.errors import FukamiruError, GitCheckpointError
from fukamiru.knowledge.service import IngestionService
from fukamiru.literature import LiteratureCandidate, LiteratureDiscoveryService
from fukamiru.ocr import (
    DocumentOCRService,
    OCRBlockInput,
    OCRPageInput,
    OCRRevisionRepository,
)
from fukamiru.ocr.audit import OCRFinding
from fukamiru.revisions.references import JapaneseReferenceService
from fukamiru.revisions.rewrite import ManuscriptRewriteService
from fukamiru.revisions.service import RevisionService
from fukamiru.storage.events import EventRepository
from fukamiru.storage.payloads import PayloadStore
from fukamiru.storage.runs import RunRepository, utc_now
from fukamiru.translation import TranslationMemoryService
from fukamiru.workflow.templates import (
    manuscript_rewrite_workflow,
    manuscript_rewrite_workflow_v21,
)


@unittest.skipIf(TestClient is None, "FastAPI test dependencies are not installed")
class APITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.root = root
        config = root / "local.toml"
        config.write_text(
            f"""
[server]
host = "127.0.0.1"
port = 9876
open_browser = false
[paths]
state_db = "{(root / "data/state.db").as_posix()}"
knowledge_db = "{(root / "data/knowledge.db").as_posix()}"
runtime_root = "{(root / "runtime").as_posix()}"
corpus_root = "{(root / "corpus").as_posix()}"
ocr_root = "{(root / "ocr").as_posix()}"
knowledge_artifacts_root = "{(root / "kb").as_posix()}"
faiss_root = "{(root / "faiss").as_posix()}"
[retrieval]
require_cuda = false
embedding_device = "cpu"
tokenizer_mode = "unicode"
semantic_enabled = false
""",
            encoding="utf-8",
        )
        self.settings = load_settings(config)
        self.codex_resolution = CodexCLIResolution(
            configured_command="codex",
            configured_resolved="C:/fixture/codex.exe",
            resolved_executable="C:/fixture/codex.exe",
            source="desktop",
            version="0.158.0-alpha.2.1",
            available=True,
            authenticated=True,
            account_type="ChatGPT",
            required_models=("gpt-6-luna", "gpt-5.6-sol"),
            error_reason=None,
        )
        self.codex_resolve_patcher = patch(
            "fukamiru.api.app.resolve_codex_cli",
            return_value=self.codex_resolution,
        )
        self.codex_require_patcher = patch(
            "fukamiru.api.app.require_codex_cli",
            return_value=self.codex_resolution,
        )
        self.mock_resolve_codex = self.codex_resolve_patcher.start()
        self.mock_require_codex = self.codex_require_patcher.start()
        self.app = create_app(self.settings, start_worker=False)
        self.client_context = TestClient(self.app)
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.codex_require_patcher.stop()
        self.codex_resolve_patcher.stop()
        self.temp.cleanup()

    def test_workflow_diagram_links_are_served_locally(self) -> None:
        manuscript = self.client.get("/workflow-diagrams/manuscript-generation.html")
        self.assertEqual(manuscript.status_code, 200)
        self.assertIn("段落ラベル一覧", manuscript.text)

        translation = self.client.get("/workflow-diagrams/translation-workflow.html")
        self.assertEqual(translation.status_code, 200)
        self.assertIn("LIVE DEFINITION", translation.text)
        translation_script = self.client.get("/workflow-diagrams/translation-workflow.js")
        self.assertEqual(translation_script.status_code, 200)

    def test_profile_contextual_proofreading_graph_and_diagram_match(self) -> None:
        configured = replace(self.settings, translation=replace(
            self.settings.translation, japanese_proofreading_profile="llmjp33b_high"))
        with TestClient(create_app(configured, start_worker=False)) as client:
            diagram = client.get("/workflow-diagrams/manuscript-generation.html")
            self.assertEqual(diagram.status_code, 200)
            self.assertIn("manuscript-proofreading-high-data.js", diagram.text)
            data = client.get("/workflow-diagrams/manuscript-proofreading-high-data.js")
            self.assertIn('"proofreading_profile": "llmjp33b_high"', data.text)
            self.assertIn('"handler": "authoring.llmjp33b_propose"', data.text)

    def test_new_profile_runs_pin_profile_and_do_not_fall_back_to_another_model(self) -> None:
        from fukamiru.storage.database import DatabasePair

        configured = replace(self.settings, translation=replace(
            self.settings.translation, japanese_proofreading_profile="llmjp33b_high"))
        databases = DatabasePair.from_settings(configured)
        with databases.state.connect() as connection:
            project = RevisionService(connection).register_project(
                name="Contextual proofreading", root_path=str(self.root))
        with TestClient(create_app(configured, start_worker=False)) as client:
            request = {"workflow_name": "manuscript_generator", "project_id": project, "inputs": {
                "note_text": "日本語の校正を確認する。", "use_rag": False, "use_llm_jp": True}}
            rejected = client.post("/api/runs", json=request)
            self.assertEqual(rejected.status_code, 503, rejected.text)
            self.assertIn("dedicated endpoint", rejected.text)
            request["inputs"]["use_llm_jp"] = False
            created = client.post("/api/runs", json=request)
            self.assertEqual(created.status_code, 201, created.text)
            with databases.state.connect() as connection:
                row = connection.execute("SELECT input_json FROM runs WHERE id=?",
                                         (created.json()["run_id"],)).fetchone()
            self.assertEqual(json.loads(row[0])["japanese_proofreading_profile"], "llmjp33b_high")

    def test_profile_adoption_profile_serves_matching_graph_and_reports_backend_requirement(self):
        configured = replace(self.settings, translation=replace(
            self.settings.translation, japanese_proofreading_profile="llmjp33b_adopt"))
        with TestClient(create_app(configured, start_worker=False)) as client:
            with client.app.state.databases.state.connect() as connection:
                project = RevisionService(connection).register_project(
                    name="Daily adoption", root_path=str(self.root))
            diagram = client.get("/workflow-diagrams/manuscript-generation.html")
            self.assertIn("manuscript-proofreading-adopt-data.js", diagram.text)
            graph = client.get("/workflow-diagrams/manuscript-proofreading-adopt-data.js")
            self.assertIn('"handler": "authoring.llmjp_adopt_correct_sections_once"', graph.text)
            self.assertIn('"model": "gpt-5.6-sol"', graph.text)
            self.assertEqual(client.get("/api/health").json()["japanese_proofreading_profile"],
                             "llmjp33b_adopt")
            workflow = next(w for w in client.get("/api/workflows").json()
                            if w["name"] == "manuscript_generator")
            self.assertEqual(workflow["japanese_proofreading_profile"], "llmjp33b_adopt")
            self.assertEqual(len(workflow["nodes"]), 64)
            rejected = client.post("/api/runs", json={
                "workflow_name": "manuscript_generator", "project_id": project, "inputs": {
                    "note_text": "対象を分析する。", "use_rag": False, "use_llm_jp": True}})
            self.assertEqual(rejected.status_code, 503, rejected.text)
            self.assertIn("dedicated endpoint", rejected.text)

    def test_managed_hf_backend_is_lazy_and_checks_credentials_before_run_creation(self):
        from fukamiru.config import HFProofreadingSettings

        configured = replace(self.settings,
            translation=replace(self.settings.translation,
                                japanese_proofreading_profile="llmjp33b_adopt"),
            hf_proofreading=HFProofreadingSettings(enabled=True, namespace="tester"))
        with (patch("fukamiru.local_llm.hf_jobs.token_available", return_value=None),
              patch("fukamiru.local_llm.hf_api.HFJobsAPI.launch") as launch,
              TestClient(create_app(configured, start_worker=False)) as client):
            with client.app.state.databases.state.connect() as connection:
                project = RevisionService(connection).register_project(
                    name="Managed proofreading", root_path=str(self.root))
            request = {"workflow_name": "manuscript_generator", "project_id": project,
                       "inputs": {"note_text": "対象を分析する。", "use_rag": False,
                                  "use_llm_jp": True}}
            denied = client.post("/api/runs", json=request)
            self.assertEqual(denied.status_code, 503, denied.text)
            self.assertIn("HF Jobs authentication", denied.text)
            with patch("fukamiru.local_llm.hf_jobs.token_available", return_value="test-token"):
                ready = client.get("/api/health").json()["local_models"]["llm_jp_proofreading"]
                self.assertEqual(ready["status"], "ready_on_demand")
                self.assertTrue(ready["configured"])
                created = client.post("/api/runs", json=request)
                self.assertEqual(created.status_code, 201, created.text)
            launch.assert_not_called()

    def test_read_source_passages_without_starting_a_run_or_index_rebuild(self) -> None:
        source = self.root / "passage.txt"
        source.write_text(
            "労働とは生命を維持する活動である。仕事との違いを述べる。", encoding="utf-8")
        with self.app.state.databases.knowledge.connect() as connection:
            asset = IngestionService(connection, self.settings).ingest_path(str(source))
        response = self.client.get(
            f"/api/source-assets/{asset['source_asset_id']}/passages", params={"q": "労働"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("生命を維持", response.json()["excerpt"])
        self.assertEqual(self.client.get("/api/runs").json()["runs"], [])
        self.assertEqual(self.client.get(
            "/api/health").json()["source_reading_policy"], "contextual-pages-v1")
        self.assertEqual(self.client.get(
            "/api/source-assets/missing/passages", params={"q": "労働"}).status_code, 404)

        rag = self.client.get("/workflow-reports/knowledge-ingestion.html")
        self.assertEqual(rag.status_code, 200)
        self.assertIn("RAG作成ワークフロー", rag.text)
        self.assertIn("文書構造・書誌を保存", rag.text)

    def test_health_workflows_run_and_snapshot(self) -> None:
        liveness = self.client.get("/api/liveness")
        self.assertEqual(liveness.status_code, 200)
        self.assertEqual(liveness.json()["status"], "alive")
        self.assertEqual(liveness.json()["version"], "0.33.0")

        health = self.client.get("/api/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["status"], "ready")
        self.assertEqual(health.json()["version"], "0.33.0")
        self.assertEqual(health.json()["configured_device"], "cpu")
        self.assertIn("actual_embedding_device", health.json())
        self.assertIn("torch_version", health.json())
        self.assertIn("gpu_worker", health.json())
        self.assertIn("llm_jp", health.json()["local_models"])
        self.assertIn("status", health.json()["local_models"]["llm_jp"])
        self.assertEqual(health.json()["codex"]["command"], "codex")
        self.assertEqual(
            health.json()["codex"]["resolved_executable"],
            "C:/fixture/codex.exe",
        )
        self.assertEqual(health.json()["codex"]["version"], "0.158.0-alpha.2.1")
        self.assertTrue(health.json()["codex"]["available"])
        self.assertEqual(
            set(health.json()["workers"]["lanes"]),
            {"ingestion_cpu", "embedding_gpu", "codex_remote", "local_llm_gpu"},
        )

        self.assertIn("embedding_gpu", health.json()["gpu_worker"])
        self.assertIn("local_llm_gpu", health.json()["gpu_worker"])
        self.assertTrue(health.json()["features"]["project_lifecycle"])
        self.assertTrue(health.json()["features"]["project_path_preflight"])
        self.assertTrue(health.json()["features"]["manuscript_import"])
        self.assertTrue(health.json()["features"]["manuscript_rewrite"])
        self.assertTrue(health.json()["features"]["constrained_japanese_candidates"])
        self.assertTrue(health.json()["features"]["section_candidate_reranking"])
        self.assertTrue(health.json()["features"]["human_wording_preferences"])
        self.assertTrue(health.json()["features"]["multi_run_progress"])
        self.assertIn("html_preview", health.json()["features"])
        self.assertEqual(self.app.state.workflows["manuscript_rewrite"].version, "29")
        self.assertIn(
            ("manuscript_rewrite", "25"), self.app.state.worker.workflow_versions
        )
        self.assertIn(
            ("manuscript_rewrite", "26"), self.app.state.worker.workflow_versions
        )
        self.assertIn(
            ("manuscript_rewrite", "27"), self.app.state.worker.workflow_versions
        )
        self.assertIn(
            ("manuscript_rewrite", "28"), self.app.state.worker.workflow_versions
        )
        self.assertIn(
            ("manuscript_rewrite", "29"), self.app.state.worker.workflow_versions
        )
        self.assertIn(
            ("manuscript_rewrite", "21"),
            self.app.state.worker.workflow_versions,
        )
        self.assertIn(
            ("manuscript_rewrite", "22"),
            self.app.state.worker.workflow_versions,
        )
        self.assertIn(
            ("authoring", "30"),
            self.app.state.worker.workflow_versions,
        )
        self.assertIn(
            ("authoring", "31"),
            self.app.state.worker.workflow_versions,
        )
        self.assertIn(
            ("manuscript_generator", "31"),
            self.app.state.worker.workflow_versions,
        )
        self.assertIn(
            ("manuscript_generator", "32"),
            self.app.state.worker.workflow_versions,
        )

        workflows = self.client.get("/api/workflows").json()
        self.assertEqual(
            {item["name"] for item in workflows},
            {
                "manuscript_generator",
                "corpus_sync",
                "ingestion",
                "research_source_ingestion",
                "manuscript_rewrite",
                "translation",
            },
        )
        authoring = next(item for item in workflows if item["name"] == "manuscript_generator")
        self.assertEqual(authoring["title"], "Manuscript Generator")
        self.assertEqual(authoring["version"], "32")
        authoring_nodes = {node["id"]: node for node in authoring["nodes"]}
        self.assertTrue(authoring["supports_proofreading_patches"])
        self.assertEqual(
            authoring_nodes["rank_proofreading_candidates"]["function_name_ja"],
            "原文と局所修正候補を比較して選択",
        )
        self.assertEqual(health.json()["semantic_retrieval"]["index"], "not_built")
        self.assertFalse(health.json()["semantic_retrieval"]["local_model_ready"])
        corpus = self.client.get("/api/corpus/latest")
        self.assertEqual(corpus.status_code, 200)
        self.assertIsNone(corpus.json()["batch"])

        created_response = self.client.post(
            "/api/runs",
            json={
                "workflow_name": "translation",
                "inputs": {"source_path": "D:/Fukamiru/source.pdf"},
            },
        )
        self.assertEqual(created_response.status_code, 201)
        created = created_response.json()
        snapshot = self.client.get(created["snapshot_url"])
        self.assertEqual(snapshot.status_code, 200)
        self.assertEqual(snapshot.json()["run"]["status"], "queued")
        self.assertEqual(snapshot.json()["active_nodes"][0]["node_id"], "ensure_ingested")
        self.assertEqual(
            snapshot.json()["current_nodes"][0]["function_name_ja"],
            "資料の取り込み状態を確認",
        )
        self.assertEqual(snapshot.json()["patch_processing"]["state"], "not_applicable")
        self.assertGreater(snapshot.json()["latest_event_id"], created["event_id"])

    def test_run_creation_rejects_missing_pinned_embedding_cache(self) -> None:
        strict_settings = replace(
            self.settings,
            retrieval=replace(
                self.settings.retrieval,
                tokenizer_mode="embedding",
                semantic_enabled=True,
            ),
        )
        strict_app = create_app(strict_settings, start_worker=False)
        with TestClient(strict_app) as client:
            response = client.post(
                "/api/runs",
                json={"workflow_name": "corpus_sync", "inputs": {}},
            )

        self.assertEqual(response.status_code, 503)
        self.assertIn("does not download models during a Run", response.text)

    def test_cancel_run_is_terminal_and_does_not_cancel_another_run(self) -> None:
        corpus = self.client.post(
            "/api/runs",
            json={"workflow_name": "corpus_sync", "inputs": {}},
        ).json()
        translation = self.client.post(
            "/api/runs",
            json={
                "workflow_name": "translation",
                "inputs": {"source_path": "D:/Fukamiru/source.pdf"},
            },
        ).json()

        response = self.client.post(f"/api/runs/{corpus['run_id']}/cancel")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "cancelled")
        cancelled = self.client.get(corpus["snapshot_url"]).json()
        queued = self.client.get(translation["snapshot_url"]).json()
        self.assertEqual(cancelled["run"]["status"], "cancelled")
        self.assertEqual(cancelled["active_nodes"], [])
        self.assertEqual(queued["run"]["status"], "queued")
        self.assertEqual(queued["active_nodes"][0]["status"], "queued")

    def test_ocr_api_assigns_language_and_cancel_stops_generic_job(self) -> None:
        source = self.root / "scan.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        with source.open("wb") as handle:
            writer.write(handle)
        source_bytes = source.read_bytes()
        created = self.client.post(
            "/api/runs",
            json={"workflow_name": "ingestion", "inputs": {"source_path": str(source)}},
        ).json()
        job = DocumentOCRService(self.app.state.databases, self.settings).enqueue(
            run_id=created["run_id"],
            source_path=source,
            source_sha256=hashlib.sha256(source_bytes).hexdigest(),
            source_language="auto",
        )

        listed = self.client.get("/api/ocr/jobs", params={"run_id": created["run_id"]})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["jobs"][0]["status"], "needs_language")
        assigned = self.client.post(
            f"/api/ocr/jobs/{job['id']}/language", json={"language": "ja"}
        )
        self.assertEqual(assigned.status_code, 200)
        self.assertEqual(assigned.json()["engine"], "ndlocr_lite")
        self.assertEqual(assigned.json()["status"], "queued")

        self.client.post(f"/api/runs/{created['run_id']}/cancel")
        cancelled = self.client.get(
            "/api/ocr/jobs", params={"run_id": created["run_id"]}
        ).json()["jobs"][0]
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(source.read_bytes(), source_bytes)

    def test_ocr_auto_language_is_persisted_and_exposed_by_api(self) -> None:
        source = self.root / "declared-japanese.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        writer._root_object[NameObject("/Lang")] = TextStringObject("ja-JP")
        with source.open("wb") as handle:
            writer.write(handle)
        source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
        created = self.client.post(
            "/api/runs",
            json={"workflow_name": "ingestion", "inputs": {"source_path": str(source)}},
        ).json()
        service = DocumentOCRService(self.app.state.databases, self.settings)

        job = service.enqueue(
            run_id=created["run_id"],
            source_path=source,
            source_sha256=source_sha256,
            source_language="auto",
        )
        resumed = service.enqueue(
            run_id=created["run_id"],
            source_path=source,
            source_sha256=source_sha256,
            source_language="auto",
        )
        listed = self.client.get(
            "/api/ocr/jobs", params={"run_id": created["run_id"]}
        ).json()["jobs"][0]

        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["source_language"], "ja")
        self.assertEqual(job["engine"], "ndlocr_lite")
        self.assertEqual(job["language_origin"], "detected")
        self.assertGreaterEqual(job["language_confidence"], 0.90)
        self.assertEqual(job["language_decision"]["method"], "pdf_declared_language")
        self.assertEqual(resumed["id"], job["id"])
        self.assertEqual(resumed["source_language"], "ja")
        self.assertEqual(listed["language_decision"]["language"], "ja")

        explicit_run = self.client.post(
            "/api/runs",
            json={"workflow_name": "ingestion", "inputs": {"source_path": str(source)}},
        ).json()
        explicit = service.enqueue(
            run_id=explicit_run["run_id"],
            source_path=source,
            source_sha256=source_sha256,
            source_language="en",
        )
        self.assertEqual(explicit["source_language"], "en")
        self.assertEqual(explicit["language_origin"], "explicit")
        self.assertEqual(explicit["language_confidence"], 1.0)

        unsupported_run = self.client.post(
            "/api/runs",
            json={"workflow_name": "ingestion", "inputs": {"source_path": str(source)}},
        ).json()
        unsupported = service.enqueue(
            run_id=unsupported_run["run_id"],
            source_path=source,
            source_sha256=source_sha256,
            source_language="es",
        )
        self.assertEqual(unsupported["status"], "needs_language")
        self.assertEqual(unsupported["source_language"], "und")
        self.assertEqual(
            unsupported["language_decision"]["method"],
            "unsupported_explicit_language",
        )
        assigned = self.client.post(
            f"/api/ocr/jobs/{unsupported['id']}/language", json={"language": "de"}
        )
        self.assertEqual(assigned.status_code, 200)
        self.assertEqual(assigned.json()["source_language"], "de")
        self.assertEqual(assigned.json()["engine"], "mistral")



    def test_v22_external_wait_can_be_manually_resumed_without_losing_payloads(self) -> None:
        definition = manuscript_rewrite_workflow()
        with self.app.state.databases.state.connect() as connection:
            created = RunRepository(connection).create(
                workflow_name=definition.name,
                workflow_version=definition.version,
                entry_node=definition.entry_node,
                project_id=None,
                inputs={"document_id": "DOC-RESUME"},
            )
            node = connection.execute(
                "SELECT id FROM node_runs WHERE run_id = ?", (created.run_id,)
            ).fetchone()
            packed = PayloadStore(connection).pack(
                {
                    "document_id": "DOC-RESUME",
                    "large_checkpoint_context": "x" * 9000,
                    "_external_retry": {
                        "count": 4,
                        "not_before": None,
                        "reason": "external_service_unavailable",
                    },
                }
            )
            error = json.dumps(
                {"type": "ExternalServiceUnavailable", "count": 4},
                ensure_ascii=False,
            )
            connection.execute(
                """
                UPDATE node_runs
                SET status = 'waiting_human', input_json = ?, error_json = ?
                WHERE id = ?
                """,
                (json.dumps(packed, ensure_ascii=False), error, node["id"]),
            )
            connection.execute(
                "UPDATE runs SET status = 'waiting_human', error_json = ? WHERE id = ?",
                (error, created.run_id),
            )
            connection.commit()

        response = self.client.post(f"/api/runs/{created.run_id}/resume-external")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "queued")
        self.assertEqual(response.json()["manual_resume_count"], 1)
        self.assertIsNone(response.json()["authorized_engine_fingerprint"])
        with self.app.state.databases.state.connect() as connection:
            run = connection.execute(
                "SELECT status, error_json FROM runs WHERE id = ?", (created.run_id,)
            ).fetchone()
            resumed = connection.execute(
                "SELECT status, attempt, input_json, error_json FROM node_runs WHERE id = ?",
                (node["id"],),
            ).fetchone()
            raw_inputs = json.loads(resumed["input_json"])
            inputs = PayloadStore(connection).unpack(raw_inputs)
            event = connection.execute(
                """
                SELECT payload_json FROM event_log
                WHERE run_id = ? AND event_type = 'worker.codex.manual_resume_requested'
                """,
                (created.run_id,),
            ).fetchone()
        self.assertEqual(run["status"], "queued")
        self.assertIsNone(run["error_json"])
        self.assertEqual(resumed["status"], "queued")
        self.assertEqual(resumed["attempt"], 1)
        self.assertIsNone(resumed["error_json"])
        self.assertNotIn("_external_retry", raw_inputs)
        self.assertNotIn("authorized_engine_fingerprint", inputs)
        self.assertEqual(inputs["large_checkpoint_context"], "x" * 9000)
        event_payload = json.loads(event["payload_json"])
        self.assertEqual(event_payload["previous_retry_count"], 4)
        self.assertIsNone(event_payload["authorized_engine_fingerprint"])
        self.assertFalse(event_payload["engine_change_authorization_recorded"])

    def test_v31_generator_engine_wait_requires_exact_target_authorization(self) -> None:
        previous = "a" * 64
        target = "b" * 64
        with self.app.state.databases.state.connect() as connection:
            created = RunRepository(connection).create(
                workflow_name="manuscript_generator", workflow_version="31",
                entry_node="calibrate_manuscript_certainty", project_id=None,
                inputs={"document_id": "DOC-V31-ENGINE"},
            )
            node = connection.execute(
                "SELECT id FROM node_runs WHERE run_id=?", (created.run_id,)
            ).fetchone()
            error = json.dumps({
                "type": "ExternalServiceUnavailable",
                "reason": "inference_engine_authorization_required",
                "previous_engine_fingerprint": previous,
                "target_engine_fingerprint": target,
            })
            connection.execute(
                "UPDATE node_runs SET status='waiting_human',error_json=? WHERE id=?",
                (error, node["id"]),
            )
            connection.execute(
                "UPDATE runs SET status='waiting_human',error_json=? WHERE id=?",
                (error, created.run_id),
            )
            connection.commit()

        endpoint = f"/api/runs/{created.run_id}/resume-external"
        self.assertEqual(self.client.post(
            endpoint, json={"authorized_engine_fingerprint": previous}
        ).status_code, 409)
        self.assertEqual(self.client.post(endpoint).status_code, 409)
        resumed = self.client.post(
            endpoint, json={"authorized_engine_fingerprint": target}
        )
        self.assertEqual(resumed.status_code, 200, resumed.text)
        self.assertEqual(resumed.json()["status"], "queued")
        self.assertEqual(resumed.json()["authorized_engine_fingerprint"], target)
        with self.app.state.databases.state.connect() as connection:
            authorization = connection.execute(
                "SELECT target_fingerprint,status FROM manuscript_engine_authorizations "
                "WHERE run_id=?", (created.run_id,),
            ).fetchone()
        self.assertEqual(authorization["target_fingerprint"], target)
        self.assertEqual(authorization["status"], "pending")
        self.assertEqual(self.client.post(
            endpoint, json={"authorized_engine_fingerprint": target}
        ).status_code, 409)

    def test_external_resume_rejects_v21_without_mutating_it(self) -> None:
        definition = manuscript_rewrite_workflow_v21()
        with self.app.state.databases.state.connect() as connection:
            created = RunRepository(connection).create(
                workflow_name=definition.name,
                workflow_version=definition.version,
                entry_node=definition.entry_node,
                project_id=None,
                inputs={"document_id": "DOC-V21"},
            )
            error = json.dumps({"type": "ExternalServiceUnavailable", "count": 4})
            connection.execute(
                """
                UPDATE node_runs
                SET status = 'waiting_human', error_json = ?
                WHERE run_id = ?
                """,
                (error, created.run_id),
            )
            connection.execute(
                "UPDATE runs SET status = 'waiting_human', error_json = ? WHERE id = ?",
                (error, created.run_id),
            )
            connection.commit()

        response = self.client.post(f"/api/runs/{created.run_id}/resume-external")

        self.assertEqual(response.status_code, 409, response.text)
        with self.app.state.databases.state.connect() as connection:
            run = connection.execute(
                "SELECT status, error_json FROM runs WHERE id = ?", (created.run_id,)
            ).fetchone()
            node = connection.execute(
                "SELECT status, error_json FROM node_runs WHERE run_id = ?",
                (created.run_id,),
            ).fetchone()
        self.assertEqual(run["status"], "waiting_human")
        self.assertIsNotNone(run["error_json"])
        self.assertEqual(node["status"], "waiting_human")
        self.assertIsNotNone(node["error_json"])

    def test_external_resume_requires_exact_lowercase_sha256_authorization(self) -> None:
        response = self.client.post(
            "/api/runs/not-a-run/resume-external",
            json={"authorized_engine_fingerprint": "A" * 64},
        )

        self.assertEqual(response.status_code, 422, response.text)

    def test_external_resume_requires_external_service_wait(self) -> None:
        definition = manuscript_rewrite_workflow()
        with self.app.state.databases.state.connect() as connection:
            created = RunRepository(connection).create(
                workflow_name=definition.name,
                workflow_version=definition.version,
                entry_node=definition.entry_node,
                project_id=None,
                inputs={"document_id": "DOC-HUMAN-REVIEW"},
            )
            error = json.dumps({"type": "CoherenceResolutionRequired"})
            connection.execute(
                "UPDATE node_runs SET status = 'waiting_human', error_json = ? WHERE run_id = ?",
                (error, created.run_id),
            )
            connection.execute(
                "UPDATE runs SET status = 'waiting_human', error_json = ? WHERE id = ?",
                (error, created.run_id),
            )
            connection.commit()

        response = self.client.post(f"/api/runs/{created.run_id}/resume-external")

        self.assertEqual(response.status_code, 409, response.text)
        snapshot = self.client.get(f"/api/runs/{created.run_id}/snapshot").json()
        self.assertEqual(snapshot["run"]["status"], "waiting_human")
        self.assertEqual(snapshot["active_nodes"][0]["status"], "waiting_human")

    def test_run_progress_returns_batch_percentage_and_recent_event_history(self) -> None:
        created = self.client.post(
            "/api/runs",
            json={
                "workflow_name": "translation",
                "inputs": {"source_path": "D:/Fukamiru/source.pdf"},
            },
        ).json()
        now = utc_now()
        with self.app.state.databases.state.connect() as connection:
            node_run_id = connection.execute(
                "SELECT id FROM node_runs WHERE run_id = ?",
                (created["run_id"],),
            ).fetchone()["id"]
            connection.executemany(
                """
                INSERT INTO metrics(
                    run_id, node_run_id, metric_name, numeric_value, unit, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        created["run_id"],
                        node_run_id,
                        "translation.batch.total",
                        10,
                        "batches",
                        now,
                    ),
                    (
                        created["run_id"],
                        node_run_id,
                        "translation.batch.completed",
                        4,
                        "batches",
                        now,
                    ),
                    (
                        created["run_id"],
                        node_run_id,
                        "translation.characters.completed",
                        1234,
                        "characters",
                        now,
                    ),
                ),
            )
            connection.commit()
            EventRepository(connection).append(
                created["run_id"],
                "translation.batch.progress",
                payload={"completed": 4, "total": 10, "percentage": 40.0},
            )

        response = self.client.get(f"/api/runs/{created['run_id']}/progress")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["progress"]["completed"], 4)
        self.assertEqual(payload["progress"]["total"], 10)
        self.assertEqual(payload["progress"]["percentage"], 40.0)
        self.assertEqual(payload["progress"]["completed_characters"], 1234)
        self.assertIn(
            "translation.batch.progress",
            [event["type"] for event in payload["events"]],
        )
        self.assertNotIn("input", payload["active_nodes"][0])

    def test_manuscript_argument_progress_exposes_all_process_counts(self) -> None:
        definition = manuscript_rewrite_workflow()
        with self.app.state.databases.state.connect() as connection:
            created = RunRepository(connection).create(
                workflow_name=definition.name,
                workflow_version=definition.version,
                entry_node="plan_manuscript_structure",
                project_id=None,
                inputs={"document_id": "DOC-FIXTURE"},
            )
            processes = [
                {
                    "process_name": "block_roles",
                    "status": "completed",
                    "target_count": 10,
                    "completed_count": 10,
                    "reused_count": 0,
                    "unresolved_count": 0,
                    "grace_state": "none",
                },
                {
                    "process_name": "reference_classification",
                    "status": "reused",
                    "target_count": 4,
                    "completed_count": 4,
                    "reused_count": 4,
                    "unresolved_count": 0,
                    "grace_state": "none",
                },
                {
                    "process_name": "claim_evidence_links",
                    "status": "running",
                    "target_count": 3,
                    "completed_count": 1,
                    "reused_count": 8,
                    "unresolved_count": 2,
                    "grace_state": "waiting",
                },
                {
                    "process_name": "integration_validation",
                    "status": "pending",
                    "target_count": 1,
                    "completed_count": 0,
                    "reused_count": 0,
                    "unresolved_count": 1,
                    "grace_state": "none",
                },
            ]
            EventRepository(connection).append(
                created.run_id,
                "manuscript.argument_plan.progress",
                payload={
                    "completed": 2,
                    "total": 4,
                    "percentage": 50.0,
                    "target_count": 18,
                    "completed_count": 15,
                    "reused_count": 12,
                    "unresolved_count": 3,
                    "grace_state": "waiting",
                    "argument_processes": processes,
                },
            )

        response = self.client.get(f"/api/runs/{created.run_id}/progress")

        self.assertEqual(response.status_code, 200, response.text)
        progress = response.json()["progress"]
        self.assertEqual(progress["kind"], "manuscript_argument_processes")
        self.assertEqual(progress["current"], 2)
        self.assertEqual(progress["total"], 4)
        self.assertEqual(progress["reused_count"], 12)
        self.assertEqual(progress["unresolved_count"], 3)
        self.assertEqual(progress["grace_state"], "waiting")
        self.assertEqual(progress["argument_processes"], processes)

    def test_manuscript_enumeration_progress_uses_the_map_node_identity(self) -> None:
        definition = manuscript_rewrite_workflow()
        with self.app.state.databases.state.connect() as connection:
            created = RunRepository(connection).create(
                workflow_name=definition.name,
                workflow_version=definition.version,
                entry_node="resolve_enumeration_context",
                project_id=None,
                inputs={"document_id": "DOC-ENUMERATION"},
            )
            EventRepository(connection).append(
                created.run_id,
                "manuscript.enumeration_context.progress",
                payload={"completed": 2, "total": 5, "percentage": 40.0},
            )

        detail = self.client.get(f"/api/runs/{created.run_id}/progress")
        listed = {
            item["run"]["id"]: item for item in self.client.get("/api/runs?limit=10").json()["runs"]
        }

        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(
            detail.json()["progress"]["kind"],
            "manuscript_enumeration_batches",
        )
        self.assertEqual(
            detail.json()["progress"]["phase"],
            "resolve_enumeration_context",
        )
        self.assertEqual(
            listed[created.run_id]["progress"]["kind"],
            "manuscript_enumeration_batches",
        )
        self.assertEqual(listed[created.run_id]["progress"]["completed"], 2)

    def test_manuscript_progress_does_not_reuse_a_previous_node_name(self) -> None:
        definition = manuscript_rewrite_workflow()
        with self.app.state.databases.state.connect() as connection:
            created = RunRepository(connection).create(
                workflow_name=definition.name,
                workflow_version=definition.version,
                entry_node="resolve_enumeration_context",
                project_id=None,
                inputs={"document_id": "DOC-STALE-PROGRESS"},
            )
            EventRepository(connection).append(
                created.run_id,
                "manuscript.references.progress",
                payload={"completed": 35, "total": 35, "percentage": 100.0},
            )

        detail = self.client.get(f"/api/runs/{created.run_id}/progress").json()
        listed = {
            item["run"]["id"]: item for item in self.client.get("/api/runs?limit=10").json()["runs"]
        }

        self.assertIsNone(detail["progress"]["kind"])
        self.assertEqual(
            detail["progress"]["phase"],
            "resolve_enumeration_context",
        )
        self.assertIsNone(detail["progress"]["percentage"])
        self.assertIsNone(listed[created.run_id]["progress"]["kind"])
        self.assertEqual(
            listed[created.run_id]["progress"]["phase"],
            "resolve_enumeration_context",
        )

    def test_translation_progress_uses_active_repair_phase(self) -> None:
        created = self.client.post(
            "/api/runs",
            json={
                "workflow_name": "translation",
                "inputs": {"source_path": "D:/Fukamiru/source.pdf"},
            },
        ).json()
        now = utc_now()
        with self.app.state.databases.state.connect() as connection:
            node_run_id = connection.execute(
                "SELECT id FROM node_runs WHERE run_id = ?",
                (created["run_id"],),
            ).fetchone()["id"]
            connection.execute(
                """
                UPDATE node_runs
                SET node_id = 'repair_translation', status = 'running', started_at = ?
                WHERE id = ?
                """,
                (now, node_run_id),
            )
            connection.executemany(
                """
                INSERT INTO metrics(
                    run_id, node_run_id, metric_name, numeric_value, unit, created_at
                ) VALUES (?, ?, ?, ?, 'batches', ?)
                """,
                (
                    (
                        created["run_id"],
                        node_run_id,
                        "translation.batch.total",
                        10,
                        now,
                    ),
                    (
                        created["run_id"],
                        node_run_id,
                        "translation.batch.completed",
                        4,
                        now,
                    ),
                ),
            )
            connection.commit()

        detail = self.client.get(f"/api/runs/{created['run_id']}/progress").json()
        listed = {
            item["run"]["id"]: item for item in self.client.get("/api/runs?limit=10").json()["runs"]
        }

        self.assertEqual(detail["progress"]["phase"], "repair_translation")
        self.assertEqual(
            listed[created["run_id"]]["progress"]["phase"],
            "repair_translation",
        )

    def test_run_list_returns_independent_progress_for_multiple_runs(self) -> None:
        first = self.client.post(
            "/api/runs",
            json={
                "workflow_name": "translation",
                "inputs": {"source_path": "D:/Fukamiru/first.pdf"},
            },
        ).json()
        second = self.client.post(
            "/api/runs",
            json={
                "workflow_name": "translation",
                "inputs": {"source_path": "D:/Fukamiru/second.pdf"},
            },
        ).json()
        now = utc_now()
        with self.app.state.databases.state.connect() as connection:
            for created, completed in ((first, 2), (second, 7)):
                node_run_id = connection.execute(
                    "SELECT id FROM node_runs WHERE run_id = ?",
                    (created["run_id"],),
                ).fetchone()["id"]
                connection.executemany(
                    """
                    INSERT INTO metrics(
                        run_id, node_run_id, metric_name, numeric_value, unit, created_at
                    ) VALUES (?, ?, ?, ?, 'batches', ?)
                    """,
                    (
                        (
                            created["run_id"],
                            node_run_id,
                            "translation.batch.total",
                            10,
                            now,
                        ),
                        (
                            created["run_id"],
                            node_run_id,
                            "translation.batch.completed",
                            completed,
                            now,
                        ),
                    ),
                )
            connection.commit()
        self.client.post(f"/api/runs/{first['run_id']}/cancel")

        response = self.client.get("/api/runs?limit=10")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["refresh_interval_seconds"], 10)
        runs = {item["run"]["id"]: item for item in response.json()["runs"]}
        self.assertEqual(runs[first["run_id"]]["run"]["status"], "cancelled")
        self.assertEqual(runs[first["run_id"]]["progress"]["percentage"], 20.0)
        self.assertEqual(runs[second["run_id"]]["run"]["status"], "queued")
        self.assertEqual(runs[second["run_id"]]["progress"]["percentage"], 70.0)
        self.assertEqual(
            runs[second["run_id"]]["run"]["input"]["source_path"],
            "D:/Fukamiru/second.pdf",
        )
        active = self.client.get("/api/runs?active_only=true").json()["runs"]
        self.assertEqual([item["run"]["id"] for item in active], [second["run_id"]])

    def test_run_progress_reports_indeterminate_ingestion_activity(self) -> None:
        created = self.client.post(
            "/api/runs",
            json={
                "workflow_name": "translation",
                "inputs": {"source_path": "D:/Fukamiru/source.pdf"},
            },
        ).json()
        with self.app.state.databases.state.connect() as connection:
            EventRepository(connection).append(
                created["run_id"],
                "ingestion.progress",
                payload={
                    "kind": "ingestion_activity",
                    "phase": "replace_existing_structure",
                    "label": "既存の文書構造を置換中（SQLite全表走査）",
                    "stage": 3,
                    "stage_total": 6,
                    "completed": None,
                    "total": None,
                    "unit": None,
                    "indeterminate": True,
                    "details": {
                        "elapsed_seconds": 25.0,
                        "activity_count": 42,
                        "existing_chunks": 3712,
                        "corpus_chunks": 820844,
                    },
                },
            )

        response = self.client.get(f"/api/runs/{created['run_id']}/progress")

        self.assertEqual(response.status_code, 200, response.text)
        progress = response.json()["progress"]
        self.assertEqual(progress["kind"], "ingestion_activity")
        self.assertEqual(progress["phase"], "replace_existing_structure")
        self.assertTrue(progress["indeterminate"])
        self.assertIsNone(progress["completed"])
        self.assertEqual(progress["details"]["activity_count"], 42)

    def test_run_progress_recovers_total_from_completed_legacy_draft(self) -> None:
        created = self.client.post(
            "/api/runs",
            json={
                "workflow_name": "translation",
                "inputs": {"source_path": "D:/Fukamiru/source.pdf"},
            },
        ).json()
        now = utc_now()
        with self.app.state.databases.state.connect() as connection:
            connection.execute(
                """
                UPDATE node_runs
                SET node_id = 'draft_translation', status = 'completed',
                    output_json = '{"batch_count":7}', completed_at = ?
                WHERE run_id = ?
                """,
                (now, created["run_id"]),
            )
            connection.commit()
        with self.app.state.databases.knowledge.connect() as connection:
            connection.execute(
                """
                INSERT INTO translation_batch_checkpoints(
                    id, run_id, batch_ordinal, request_signature,
                    source_text_sha256, source_span_ids_json, translated_text,
                    translator_identity, status, created_at, updated_at
                ) VALUES ('checkpoint-1', ?, 0, 'signature', 'source-hash', '[]',
                          '訳文', 'fixture', 'completed', ?, ?)
                """,
                (created["run_id"], now, now),
            )
            connection.commit()

        payload = self.client.get(f"/api/runs/{created['run_id']}/progress").json()

        self.assertEqual(payload["progress"]["completed"], 1)
        self.assertEqual(payload["progress"]["total"], 7)
        self.assertEqual(payload["progress"]["percentage"], 14.3)

    def test_run_progress_switches_to_llm_jp_chunk_phase(self) -> None:
        created = self.client.post(
            "/api/runs",
            json={
                "workflow_name": "translation",
                "inputs": {"source_path": "D:/Fukamiru/source.pdf"},
            },
        ).json()
        now = utc_now()
        with self.app.state.databases.state.connect() as connection:
            node_run_id = connection.execute(
                "SELECT id FROM node_runs WHERE run_id = ?",
                (created["run_id"],),
            ).fetchone()["id"]
            connection.executemany(
                """
                INSERT INTO metrics(
                    run_id, node_run_id, metric_name, numeric_value, unit, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        created["run_id"],
                        node_run_id,
                        "translation.post_edit.chunk.total",
                        20,
                        "chunks",
                        now,
                    ),
                    (
                        created["run_id"],
                        node_run_id,
                        "translation.post_edit.chunk.completed",
                        7,
                        "chunks",
                        now,
                    ),
                ),
            )
            connection.commit()

        payload = self.client.get(f"/api/runs/{created['run_id']}/progress").json()

        self.assertEqual(payload["progress"]["kind"], "llm_jp_post_edit_chunks")
        self.assertEqual(payload["progress"]["phase"], "llm_jp_post_edit")
        self.assertEqual(payload["progress"]["completed"], 7)
        self.assertEqual(payload["progress"]["total"], 20)
        self.assertEqual(payload["progress"]["percentage"], 35.0)

    def test_review_is_hidden_until_node_reaches_waiting_human(self) -> None:
        created = self.client.post(
            "/api/runs",
            json={
                "workflow_name": "translation",
                "inputs": {"source_path": "D:/Fukamiru/source.pdf"},
            },
        ).json()
        with self.app.state.databases.state.connect() as connection:
            node = connection.execute(
                "SELECT id FROM node_runs WHERE run_id = ?", (created["run_id"],)
            ).fetchone()
            connection.execute(
                """
                INSERT INTO human_reviews(
                    id, run_id, node_run_id, object_type, object_id, question,
                    options_json, status, created_at
                ) VALUES ('premature-review', ?, ?, 'run_artifact', 'artifact',
                          'Not ready', '["approved"]', 'open', ?)
                """,
                (created["run_id"], node["id"], utc_now()),
            )
            connection.commit()

        reviews = self.client.get("/api/reviews?status=open")

        self.assertEqual(reviews.status_code, 200, reviews.text)
        self.assertNotIn("premature-review", {review["id"] for review in reviews.json()})

    def test_rejected_review_includes_pdf_title_and_source_id(self) -> None:
        created = self.client.post(
            "/api/runs",
            json={
                "workflow_name": "translation",
                "inputs": {
                    "source_path": "D:/Fukamiru/library/Synthetic Research Notes.pdf",
                },
            },
        ).json()
        with self.app.state.databases.state.connect() as connection:
            node = connection.execute(
                "SELECT id FROM node_runs WHERE run_id = ?", (created["run_id"],)
            ).fetchone()
            connection.execute(
                "UPDATE runs SET output_json = ? WHERE id = ?",
                (
                    '{"source_asset_id":"asset-pdf-123",'
                    '"source_path":"D:/Fukamiru/library/Synthetic Research Notes.pdf"}',
                    created["run_id"],
                ),
            )
            connection.execute(
                """
                INSERT INTO human_reviews(
                    id, run_id, node_run_id, object_type, object_id, question,
                    options_json, status, created_at
                ) VALUES ('rejected-review', ?, ?, 'run_artifact', 'artifact-123',
                          'Review translation', '["approved","rejected"]',
                          'rejected', ?)
                """,
                (created["run_id"], node["id"], utc_now()),
            )
            connection.commit()

        response = self.client.get("/api/reviews?status=rejected")

        self.assertEqual(response.status_code, 200, response.text)
        review = response.json()[0]
        self.assertEqual(review["source_title"], "Synthetic Research Notes.pdf")
        self.assertEqual(review["source_asset_id"], "asset-pdf-123")
        self.assertEqual(review["run_id"], created["run_id"])

    def test_approved_review_keeps_durable_output_destination(self) -> None:
        created = self.client.post(
            "/api/runs",
            json={
                "workflow_name": "translation",
                "inputs": {"source_path": "D:/Fukamiru/library/source.pdf"},
            },
        ).json()
        output_path = self.root / "approved" / "translation.final.html"
        output_path.parent.mkdir(parents=True)
        output_path.write_text("<article></article>", encoding="utf-8")
        with self.app.state.databases.state.connect() as connection:
            node = connection.execute(
                "SELECT id FROM node_runs WHERE run_id = ?", (created["run_id"],)
            ).fetchone()
            connection.execute(
                """
                INSERT INTO human_reviews(
                    id, run_id, node_run_id, object_type, object_id, question,
                    options_json, status, created_at, resolved_at
                ) VALUES ('approved-review', ?, ?, 'run_artifact', 'artifact-final',
                          'Review translation', '["approved","rejected"]',
                          'approved', ?, ?)
                """,
                (created["run_id"], node["id"], utc_now(), utc_now()),
            )
            connection.execute(
                "UPDATE runs SET status = 'completed', output_json = ? WHERE id = ?",
                (
                    json.dumps({"translation_final_path": str(output_path)}),
                    created["run_id"],
                ),
            )
            connection.commit()
            projection = connection.execute(
                "SELECT output_path, output_status, run_status "
                "FROM review_queue_entries WHERE id = 'approved-review'"
            ).fetchone()

        self.assertEqual(projection["output_path"], str(output_path))
        self.assertEqual(projection["output_status"], "ready")
        self.assertEqual(projection["run_status"], "completed")

        response = self.client.get("/api/reviews?status=approved")

        self.assertEqual(response.status_code, 200, response.text)
        review = next(item for item in response.json() if item["id"] == "approved-review")
        self.assertEqual(review["output_status"], "ready")
        self.assertEqual(review["output_path"], str(output_path))
        self.assertEqual(review["output_folder_path"], str(output_path.parent))
        self.assertEqual(review["run_status"], "completed")

    def test_project_is_created_at_explicit_path_and_registered(self) -> None:
        project_root = Path(self.temp.name) / "chosen" / "project"
        response = self.client.post(
            "/api/projects",
            json={"name": "Pawlowski", "root_path": str(project_root)},
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertTrue((project_root / ".git").is_dir())
        self.assertTrue((project_root / "fukamiru.project.toml").is_file())
        projects = self.client.get("/api/projects").json()
        self.assertEqual(projects[0]["root_path"], str(project_root.resolve()))
        self.assertIsNone(projects[0]["archived_at"])
        dissertation = self.client.post(
            "/api/dissertations",
            json={"project_id": projects[0]["id"], "title": "Fixture thesis"},
        )
        self.assertEqual(dissertation.status_code, 201, dissertation.text)
        dissertation_payload = dissertation.json()
        document_id = dissertation_payload["document"]["id"]
        root_id = dissertation_payload["nodes"][0]["id"]
        chapter = self.client.post(
            f"/api/dissertations/{document_id}/outline",
            json={
                "parent_id": root_id,
                "node_type": "chapter",
                "title": "Chapter 1",
                "order_index": 0,
            },
        )
        self.assertEqual(chapter.status_code, 201, chapter.text)
        snapshot = self.client.get(f"/api/dissertations/{document_id}")
        self.assertEqual(snapshot.status_code, 200)
        self.assertEqual(len(snapshot.json()["nodes"]), 2)
        run = self.client.post(
            "/api/runs",
            json={
                "workflow_name": "translation",
                "project_id": projects[0]["id"],
                "inputs": {"source_path": "D:/Fukamiru/fixture.pdf"},
            },
        ).json()
        evaluation = self.client.post(
            f"/api/dissertations/{document_id}/evaluations",
            json={"run_id": run["run_id"]},
        )
        self.assertEqual(evaluation.status_code, 201, evaluation.text)
        self.assertEqual(evaluation.json()["summary"]["finding_count"], 1)
        restored = self.client.get(
            f"/api/dissertations/{document_id}/evaluations/{evaluation.json()['id']}"
        )
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(
            restored.json()["defects"][0]["defect_type"],
            "dissertation_outline_content_missing",
        )

    def test_project_path_preflight_accepts_existing_directory_without_modifying_it(
        self,
    ) -> None:
        project_root = self.root / "existing-project-root"
        project_root.mkdir()
        source = project_root / "chapter.docx"
        source.write_bytes(b"human-owned")

        response = self.client.post(
            "/api/projects/preflight",
            json={"root_path": str(project_root)},
        )

        self.assertEqual(response.status_code, 200, response.text)
        assessment = response.json()
        self.assertTrue(assessment["writable"])
        self.assertTrue(assessment["root_exists"])
        self.assertEqual(assessment["existing_entries"], 1)
        self.assertEqual(list(project_root.iterdir()), [source])

    def test_project_registration_preserves_existing_files_and_gitignore(self) -> None:
        project_root = self.root / "existing-content-project"
        project_root.mkdir()
        source = project_root / "chapter.docx"
        source.write_bytes(b"human-owned")
        ignore = project_root / ".gitignore"
        ignore.write_text("private-notes/\n", encoding="utf-8")

        response = self.client.post(
            "/api/projects",
            json={"name": "Existing content", "root_path": str(project_root)},
        )

        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(source.read_bytes(), b"human-owned")
        self.assertEqual(ignore.read_text(encoding="utf-8"), "private-notes/\n")
        self.assertTrue((project_root / "fukamiru.project.toml").is_file())

    def test_project_permission_failure_is_actionable_and_leaves_no_registration(
        self,
    ) -> None:
        project_root = self.root / "protected-project"
        project_root.mkdir()
        source = project_root / "chapter.docx"
        source.write_bytes(b"human-owned")

        with patch(
            "fukamiru.projects.service.os.open",
            side_effect=PermissionError(5, "Access denied"),
        ):
            response = self.client.post(
                "/api/projects",
                json={"name": "Protected", "root_path": str(project_root)},
            )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("書き込めません", response.json()["detail"])
        self.assertEqual(list(project_root.iterdir()), [source])
        projects = self.client.get("/api/projects").json()
        self.assertFalse(any(project["name"] == "Protected" for project in projects))

    def test_project_creation_failure_rolls_back_only_generated_files(self) -> None:
        project_root = self.root / "rollback-project"
        project_root.mkdir()
        source = project_root / "chapter.docx"
        source.write_bytes(b"human-owned")

        with patch(
            "fukamiru.projects.service.GitCheckpointService.checkpoint",
            side_effect=GitCheckpointError("checkpoint failed"),
        ):
            response = self.client.post(
                "/api/projects",
                json={"name": "Rollback", "root_path": str(project_root)},
            )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(list(project_root.iterdir()), [source])
        projects = self.client.get("/api/projects").json()
        self.assertFalse(any(project["name"] == "Rollback" for project in projects))

    def test_project_unregister_preserves_files_and_data_and_can_be_restored(self) -> None:
        project_root = self.root / "lifecycle-project"
        project = self.client.post(
            "/api/projects",
            json={"name": "Lifecycle", "root_path": str(project_root)},
        ).json()
        with self.app.state.databases.state.connect() as connection:
            document_id = RevisionService(connection).create_document(
                project_id=project["id"], kind="paper", title="Human paper"
            )

        response = self.client.delete(f"/api/projects/{project['id']}")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["filesystem_preserved"])
        self.assertTrue(response.json()["data_preserved"])
        self.assertTrue((project_root / "fukamiru.project.toml").is_file())
        active_projects = self.client.get("/api/projects").json()
        self.assertFalse(any(item["id"] == project["id"] for item in active_projects))
        archived = self.client.get("/api/projects?include_archived=true").json()
        archived_project = next(item for item in archived if item["id"] == project["id"])
        self.assertIsNotNone(archived_project["archived_at"])
        with self.app.state.databases.state.connect() as connection:
            self.assertIsNotNone(
                connection.execute(
                    "SELECT id FROM content_documents WHERE id = ?", (document_id,)
                ).fetchone()
            )

        restored = self.client.post(f"/api/projects/{project['id']}/restore")

        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertIsNone(restored.json()["archived_at"])
        active_projects = self.client.get("/api/projects").json()
        self.assertTrue(any(item["id"] == project["id"] for item in active_projects))

    def test_project_unregister_is_blocked_while_a_run_is_active(self) -> None:
        project_root = self.root / "active-run-project"
        project = self.client.post(
            "/api/projects",
            json={"name": "Active run", "root_path": str(project_root)},
        ).json()
        run = self.client.post(
            "/api/runs",
            json={
                "workflow_name": "translation",
                "project_id": project["id"],
                "inputs": {"source_path": "D:/Fukamiru/source.pdf"},
            },
        ).json()

        response = self.client.delete(f"/api/projects/{project['id']}")

        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("Run", response.json()["detail"])
        snapshot = self.client.get(run["snapshot_url"]).json()
        self.assertEqual(snapshot["run"]["status"], "queued")
        active_projects = self.client.get("/api/projects").json()
        self.assertTrue(any(item["id"] == project["id"] for item in active_projects))

    def test_dissertation_outline_csv_preview_and_confirm(self) -> None:
        project_root = Path(self.temp.name) / "csv-project"
        project = self.client.post(
            "/api/projects",
            json={"name": "CSV project", "root_path": str(project_root)},
        ).json()
        dissertation = self.client.post(
            "/api/dissertations",
            json={"project_id": project["id"], "title": "CSV thesis"},
        ).json()
        document_id = dissertation["document"]["id"]
        csv_path = Path(self.temp.name) / "outline.csv"
        csv_path.write_text(
            "type,title,summary,instructions,target_characters\n"
            "chapter,Introduction,Frame the question,Define the problem,12000\n"
            "chapter,Conclusion,Synthesize findings,Do not add new evidence,8000\n",
            encoding="utf-8",
        )

        preview = self.client.post(
            f"/api/dissertations/{document_id}/outline-imports/preview",
            json={"source_path": str(csv_path)},
        )
        self.assertEqual(preview.status_code, 201, preview.text)
        self.assertEqual(preview.json()["target_characters"], 20000)
        unchanged = self.client.get(f"/api/dissertations/{document_id}").json()
        self.assertEqual(len(unchanged["nodes"]), 1)

        confirmed = self.client.post(
            f"/api/dissertations/{document_id}/outline-imports/{preview.json()['id']}/confirm"
        )
        self.assertEqual(confirmed.status_code, 201, confirmed.text)
        self.assertEqual(len(confirmed.json()["dissertation"]["nodes"]), 3)
        directives = [
            node["directive"]
            for node in confirmed.json()["dissertation"]["nodes"]
            if node["directive"]
        ]
        self.assertEqual([item["target_characters"] for item in directives], [12000, 8000])

    def test_corpus_folder_can_be_selected_and_is_confined_to_managed_sources(self) -> None:
        selected = self.settings.paths.corpus_root / "managed_sources" / "collection" / "author"
        selected.mkdir(parents=True)
        directories = self.client.get("/api/corpus/directories")
        self.assertEqual(directories.status_code, 200)
        self.assertIn(str(selected.resolve()), directories.json()["directories"])

        created = self.client.post(
            "/api/runs",
            json={
                "workflow_name": "corpus_sync",
                "inputs": {"root_path": str(selected)},
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        snapshot = self.client.get(created.json()["snapshot_url"]).json()
        self.assertEqual(snapshot["run"]["input"]["root_path"], str(selected.resolve()))

        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        rejected = self.client.post(
            "/api/runs",
            json={
                "workflow_name": "corpus_sync",
                "inputs": {"root_path": str(outside)},
            },
        )
        self.assertEqual(rejected.status_code, 400)

    def test_workbench_is_served(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("ふかみる research workspace", response.text)
        self.assertIn("ふかみる Fukamiru", response.text)
        self.assertIn(
            "styles.css?v=0.33.0",
            response.text,
        )
        self.assertIn(
            "app.js?v=0.33.0",
            response.text,
        )
        self.assertIn('data-activity="review" title="レビュー待ち一覧"', response.text)
        self.assertIn("DOCUMENT · HTML", response.text)
        self.assertIn("HTML原稿を表示します。", response.text)
        self.assertIn('id="workflow-launcher"', response.text)
        self.assertIn('href="/workflow-diagrams/manuscript-generation.html"', response.text)
        self.assertIn('href="/workflow-diagrams/translation-workflow.html"', response.text)
        self.assertIn('href="/workflow-reports/knowledge-ingestion.html"', response.text)
        self.assertEqual(response.headers["cache-control"], "no-store, max-age=0")
        self.assertEqual(response.headers["pragma"], "no-cache")

        javascript = self.client.get("/src/app.js?v=0.33.0")
        self.assertEqual(javascript.status_code, 200)
        self.assertEqual(javascript.headers["cache-control"], "no-store, max-age=0")
        self.assertIn('from "./api.js"', javascript.text)
        self.assertIn(
            'from "./run-labels.js"',
            javascript.text,
        )
        self.assertIn('from "./run-progress.js"', javascript.text)
        numeric_progress = self.client.get("/src/run-progress.js?v=source")
        self.assertEqual(numeric_progress.status_code, 200)
        self.assertIn("javascript", numeric_progress.headers["content-type"])
        self.assertEqual(numeric_progress.headers["cache-control"], "no-store, max-age=0")
        self.assertIn('from "./run-recovery.js"', javascript.text)
        self.assertIn("/feedback`)", javascript.text)
        self.assertIn("LLMの過剰要求である", javascript.text)
        self.assertIn('id="review-inspector"', response.text)

    def test_translation_source_files_are_independent_of_recent_manuscript_runs(self) -> None:
        with self.app.state.databases.state.connect() as connection:
            translation = RunRepository(connection).create(
                workflow_name="translation",
                workflow_version="1",
                entry_node="translation_draft",
                project_id=None,
                inputs={"source_path": r"C:\research\paper.pdf"},
            )
            for index in range(30):
                RunRepository(connection).create(
                    workflow_name="manuscript_rewrite",
                    workflow_version="23",
                    entry_node="prepare_rewrite_context",
                    project_id=None,
                    inputs={"document_id": f"DOC-{index}"},
                )
        recent = self.client.get("/api/runs?limit=25")
        self.assertNotIn(
            translation.run_id,
            [item["run"]["id"] for item in recent.json()["runs"]],
        )
        response = self.client.get("/api/translation/source-files")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["runs"],
            [{
                "id": translation.run_id,
                "workflow_name": "translation",
                "input": {"source_path": r"C:\research\paper.pdf"},
            }],
        )

    def test_html_artifact_uses_workspace_style_without_mutating_source(self) -> None:
        run = self.client.post(
            "/api/runs",
            json={
                "workflow_name": "translation",
                "inputs": {"source_path": "D:/Fukamiru/source.pdf"},
            },
        ).json()
        html_path = self.root / "translation.html"
        html_path.write_text(
            '<article id="DOC-1" lang="ja"><section id="SEC-1">'
            '<p id="P-1">草稿</p></section></article>',
            encoding="utf-8",
        )
        artifact_id = "artifact-typeset-api"
        with self.app.state.databases.state.connect() as connection:
            connection.execute(
                """
                INSERT INTO run_artifacts(
                    id, run_id, node_run_id, artifact_type, path,
                    media_type, title, created_at
                ) VALUES (?, ?, NULL, 'translation_draft', ?, 'text/html', ?, ?)
                """,
                (artifact_id, run["run_id"], str(html_path), "Translation draft", utc_now()),
            )
            connection.commit()

        response = self.client.get(f"/api/artifacts/{artifact_id}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn('id="P-1"', response.text)
        self.assertIn("--workspace: #172713", response.text)
        self.assertIn("cite { font-style: normal; }", response.text)
        self.assertIn("script-src 'none'", response.headers["content-security-policy"])
        self.assertNotIn("<style", html_path.read_text(encoding="utf-8"))

    def test_html_import_preview_api_preserves_source_and_commits_human_revision(self) -> None:
        project_root = self.root / "html-import-project"
        project = self.client.post(
            "/api/projects",
            json={"name": "HTML import", "root_path": str(project_root)},
        ).json()
        with self.app.state.databases.state.connect() as connection:
            document_id = RevisionService(connection).create_document(
                project_id=project["id"], kind="paper", title="Imported"
            )
        source = self.root / "source-import.html"
        canonical = (
            '<article id="DOC-API-IMPORT" lang="en">'
            '<section id="SEC-API-IMPORT"><h2 id="H-API-IMPORT">Imported</h2>'
            '<p id="P-API-IMPORT">Exact source.</p></section></article>'
        )
        source.write_text(canonical, encoding="utf-8")

        preview = self.client.post(
            "/api/html-import-previews",
            json={"document_id": document_id, "source_path": str(source)},
        )
        self.assertEqual(preview.status_code, 201, preview.text)
        self.assertTrue(preview.json()["validation"]["valid"])
        confirmed = self.client.post(f"/api/html-import-previews/{preview.json()['id']}/confirm")
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual(confirmed.json()["status"], "imported")
        self.assertEqual(source.read_text(encoding="utf-8"), canonical)
        history = self.client.get(f"/api/documents/{document_id}/revisions")
        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual(len(history.json()), 1)

        diff = self.client.get(f"/api/revisions/{confirmed.json()['imported_revision_id']}/diff")
        self.assertEqual(diff.status_code, 200, diff.text)
        self.assertIn("DOC-API-IMPORT", diff.json()["unified_diff"])

    def test_manuscript_import_preview_api_creates_canonical_preview(self) -> None:
        project_root = self.root / "manuscript-import-project"
        project = self.client.post(
            "/api/projects",
            json={"name": "Manuscript import", "root_path": str(project_root)},
        ).json()
        with self.app.state.databases.state.connect() as connection:
            document_id = RevisionService(connection).create_document(
                project_id=project["id"], kind="paper", title="Working draft"
            )
        source = self.root / "working-draft.md"
        source.write_text("# Draft chapter\n\nWork in progress.", encoding="utf-8")

        preview = self.client.post(
            "/api/manuscript-import-previews",
            json={"document_id": document_id, "source_path": str(source)},
        )

        self.assertEqual(preview.status_code, 201, preview.text)
        self.assertEqual(preview.json()["source_format"], "markdown")
        self.assertTrue(preview.json()["validation"]["valid"])
        confirmed = self.client.post(
            f"/api/manuscript-import-previews/{preview.json()['id']}/confirm"
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual(confirmed.json()["status"], "imported")

    def test_project_manuscript_import_auto_creates_content_document(self) -> None:
        project_root = self.root / "project-manuscript-selection"
        project = self.client.post(
            "/api/projects",
            json={"name": "Project manuscript selection", "root_path": str(project_root)},
        ).json()
        source = project_root / "drafts" / "chapter.txt"
        source.parent.mkdir()
        source.write_text("First draft paragraph.", encoding="utf-8")

        listing = self.client.get(f"/api/projects/{project['id']}/manuscripts")

        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual(
            listing.json()["files"][0]["relative_path"],
            str(Path("drafts") / "chapter.txt"),
        )
        preview = self.client.post(
            "/api/manuscript-import-previews",
            json={
                "project_id": project["id"],
                "source_path": str(Path("drafts") / "chapter.txt"),
                "title": "Chapter Draft",
                "document_kind": "paper",
            },
        )

        self.assertEqual(preview.status_code, 201, preview.text)
        document_id = preview.json()["document_id"]
        with self.app.state.databases.state.connect() as connection:
            document = connection.execute(
                "SELECT project_id, kind, title, current_revision_id "
                "FROM content_documents WHERE id = ?",
                (document_id,),
            ).fetchone()
        self.assertEqual(document["project_id"], project["id"])
        self.assertEqual(document["kind"], "paper")
        self.assertEqual(document["title"], "Chapter Draft")
        self.assertIsNone(document["current_revision_id"])

        confirmed = self.client.post(
            f"/api/manuscript-import-previews/{preview.json()['id']}/confirm"
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual(confirmed.json()["document_id"], document_id)

    def test_manuscript_rewrite_api_queues_content_aware_workflow(self) -> None:
        project_root = self.root / "manuscript-rewrite-project"
        project = self.client.post(
            "/api/projects",
            json={"name": "Manuscript rewrite", "root_path": str(project_root)},
        ).json()
        with self.app.state.databases.state.connect() as connection:
            revisions = RevisionService(connection)
            document_id = revisions.create_document(
                project_id=project["id"], kind="paper", title="Draft argument"
            )
            base = revisions.commit_revision(
                document_id=document_id,
                canonical_html=(
                    '<article id="DOC-API-REWRITE" lang="ja">'
                    '<section id="SEC-API-REWRITE">'
                    '<h2 id="H-API-REWRITE">論点</h2>'
                    '<p id="P-API-REWRITE">検討中の議論。</p>'
                    "</section></article>"
                ),
                editor_type="human",
                operation="human_import",
            )

        created = self.client.post(
            "/api/manuscript-rewrites",
            json={
                "document_id": document_id,
                "base_revision_id": base.revision_id,
                "instructions": "章の中心的な主張を明確にする。",
                "use_rag": True,
            },
        )

        self.assertEqual(created.status_code, 202, created.text)
        snapshot = self.client.get(created.json()["snapshot_url"])
        self.assertEqual(snapshot.status_code, 200, snapshot.text)
        self.assertEqual(snapshot.json()["run"]["workflow_name"], "manuscript_rewrite")
        self.assertEqual(snapshot.json()["active_nodes"][0]["node_id"], "prepare_rewrite_context")

        run_id = created.json()["run_id"]
        base_html = (
            '<article id="DOC-API-REWRITE" lang="ja">'
            '<section id="SEC-API-REWRITE">'
            '<h2 id="H-API-REWRITE">論点</h2>'
            '<p id="P-API-REWRITE">検討中の議論。</p>'
            "</section></article>"
        )
        with self.app.state.databases.state.connect() as connection:
            proposal = ManuscriptRewriteService(connection).persist_proposal(
                run_id=run_id,
                document_id=document_id,
                project_id=project["id"],
                base_revision_id=base.revision_id,
                outline_node_id=None,
                instructions="章の中心的な主張を明確にする。",
                use_rag=True,
                context={},
                source_html=base_html,
                proposed_html=base_html.replace("検討中の議論。", "中心的な主張を明示した議論。"),
                edit_summary={"applied_edit_count": 1, "warnings": []},
                quality_status="needs_human_review",
            )
            projection = connection.execute(
                "SELECT status, source_title, applied_edit_count "
                "FROM review_queue_entries WHERE object_id = ?",
                (proposal["id"],),
            ).fetchone()

        self.assertEqual(projection["status"], "open")
        self.assertEqual(projection["source_title"], "Draft argument")
        self.assertEqual(projection["applied_edit_count"], 1)

        reviews = self.client.get("/api/reviews?status=open")
        self.assertEqual(reviews.status_code, 200, reviews.text)
        queued = next(
            item for item in reviews.json() if item["object_type"] == "manuscript_rewrite_proposal"
        )
        self.assertEqual(queued["object_id"], proposal["id"])
        self.assertEqual(queued["run_id"], run_id)
        self.assertEqual(queued["source_title"], "Draft argument")
        self.assertEqual(queued["applied_edit_count"], 1)
        self.assertEqual(queued["coherence_warning_count"], 0)
        self.assertEqual(queued["skipped_edit_count"], 0)

        missing_comment = self.client.post(
            f"/api/manuscript-rewrites/{proposal['id']}/revise",
            json={"human_review": "   "},
        )
        self.assertEqual(missing_comment.status_code, 422, missing_comment.text)

        revised = self.client.post(
            f"/api/manuscript-rewrites/{proposal['id']}/revise",
            json={"human_review": ("結論の前にある短い移行段落を直前の意味段落へ統合する。")},
        )
        self.assertEqual(revised.status_code, 202, revised.text)
        self.assertEqual(revised.json()["source_proposal_id"], proposal["id"])
        child_run_id = revised.json()["run_id"]
        with self.app.state.databases.state.connect() as connection:
            child_run = connection.execute(
                "SELECT workflow_name, workflow_version, input_json FROM runs WHERE id = ?",
                (child_run_id,),
            ).fetchone()
        self.assertEqual(child_run["workflow_name"], "manuscript_rewrite")
        self.assertEqual(child_run["workflow_version"], "29")
        child_inputs = json.loads(child_run["input_json"])
        self.assertEqual(child_inputs["document_id"], document_id)
        self.assertEqual(child_inputs["base_revision_id"], base.revision_id)
        self.assertEqual(child_inputs["source_proposal_id"], proposal["id"])
        self.assertEqual(
            child_inputs["human_review"],
            "結論の前にある短い移行段落を直前の意味段落へ統合する。",
        )
        self.assertEqual(child_inputs["instructions"], "章の中心的な主張を明確にする。")
        feedback_url = f"/api/reviews/manuscript-rewrite:{proposal['id']}/feedback"
        packet_response = self.client.get(feedback_url)
        self.assertEqual(packet_response.status_code, 200, packet_response.text)
        packet = packet_response.json()
        self.assertIn("中心的な主張を明示した議論。", packet["preview_html"])
        saved = self.client.put(feedback_url, json={
            "revision": packet["revision"], "submit": True,
            "answers": [{"question_id": packet["questions"][0]["question_id"],
                         "llm_overreach": True}],
        })
        self.assertEqual(saved.status_code, 200, saved.text)
        checkbox_only = self.client.post(
            f"/api/manuscript-rewrites/{proposal['id']}/revise", json={},
        )
        self.assertEqual(checkbox_only.status_code, 202, checkbox_only.text)
        with self.app.state.databases.state.connect() as connection:
            revised_inputs = json.loads(connection.execute(
                "SELECT input_json FROM runs WHERE id=?", (checkbox_only.json()["run_id"],),
            ).fetchone()[0])
        self.assertTrue(revised_inputs["human_question_feedback"]["answers"][0]["llm_overreach"])
        self.assertIn("llm_overreach", revised_inputs["human_review"])
        child_snapshot = self.client.get(revised.json()["snapshot_url"])
        self.assertEqual(child_snapshot.status_code, 200, child_snapshot.text)
        self.assertEqual(
            child_snapshot.json()["active_nodes"][0]["node_id"],
            "prepare_rewrite_context",
        )

        html = self.client.get(f"/api/manuscript-rewrites/{proposal['id']}/draft.html")
        self.assertEqual(html.status_code, 200, html.text)
        self.assertIn("中心的な主張を明示した議論。", html.text)
        self.assertIn("--workspace: #172713", html.text)
        self.assertIn("cite { font-style: normal; }", html.text)
        self.assertIn("script-src 'none'", html.headers["content-security-policy"])

        discarded = self.client.post(f"/api/manuscript-rewrites/{proposal['id']}/discard")
        self.assertEqual(discarded.status_code, 200, discarded.text)
        self.assertEqual(discarded.json()["status"], "rejected")
        self.assertIsNone(discarded.json()["confirmed_revision_id"])
        with self.app.state.databases.state.connect() as connection:
            projection_status = connection.execute(
                "SELECT status FROM review_queue_entries WHERE object_id = ?",
                (proposal["id"],),
            ).fetchone()["status"]
        self.assertEqual(projection_status, "rejected")
        revise_after_discard = self.client.post(
            f"/api/manuscript-rewrites/{proposal['id']}/revise",
            json={"human_review": "再度修正する。"},
        )
        self.assertEqual(revise_after_discard.status_code, 400)
        open_after_discard = self.client.get("/api/reviews?status=open").json()
        self.assertFalse(
            any(
                item["object_id"] == proposal["id"]
                for item in open_after_discard
                if item["object_type"] == "manuscript_rewrite_proposal"
            )
        )
        with self.app.state.databases.state.connect() as connection:
            detailed_json = connection.execute(
                """
                SELECT context_json, validation_json, edit_summary_json
                FROM manuscript_rewrite_proposals WHERE id = ?
                """,
                (proposal["id"],),
            ).fetchone()
            connection.execute(
                """
                UPDATE manuscript_rewrite_proposals
                SET context_json = '{not-json', validation_json = '{not-json',
                    edit_summary_json = '{not-json'
                WHERE id = ?
                """,
                (proposal["id"],),
            )
            connection.commit()
        rejected_after_discard = self.client.get("/api/reviews?status=rejected")
        self.assertEqual(rejected_after_discard.status_code, 200)
        archived = next(
            item for item in rejected_after_discard.json() if item["object_id"] == proposal["id"]
        )
        self.assertEqual(archived["proposal_status"], "rejected")
        self.assertEqual(archived["source_title"], "Draft argument")
        with self.app.state.databases.state.connect() as connection:
            connection.execute(
                """
                UPDATE manuscript_rewrite_proposals
                SET context_json = ?, validation_json = ?, edit_summary_json = ?
                WHERE id = ?
                """,
                (
                    detailed_json["context_json"],
                    detailed_json["validation_json"],
                    detailed_json["edit_summary_json"],
                    proposal["id"],
                ),
            )
            connection.commit()

        approved_path = project_root / "documents" / document_id / "approved.html"
        approved_path.parent.mkdir(parents=True, exist_ok=True)
        approved_path.write_text(proposal["proposed_html"], encoding="utf-8")
        with self.app.state.databases.state.connect() as connection:
            connection.execute(
                """
                UPDATE manuscript_rewrite_proposals
                SET status = 'confirmed', confirmed_revision_id = ?,
                    artifact_path = ?, resolved_at = ?
                WHERE id = ?
                """,
                (
                    base.revision_id,
                    str(approved_path),
                    utc_now(),
                    proposal["id"],
                ),
            )
            connection.commit()
        approved_reviews = self.client.get("/api/reviews?status=approved")
        self.assertEqual(approved_reviews.status_code, 200, approved_reviews.text)
        approved = next(
            item for item in approved_reviews.json() if item["object_id"] == proposal["id"]
        )
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["confirmed_revision_id"], base.revision_id)
        self.assertEqual(approved["output_path"], str(approved_path))
        self.assertEqual(approved["output_folder_path"], str(approved_path.parent))

    def test_reference_selection_api_adds_v22_multiple_without_changing_v21_single(
        self,
    ) -> None:
        project_root = self.root / "reference-selection-project"
        project = self.client.post(
            "/api/projects",
            json={"name": "Reference selection", "root_path": str(project_root)},
        ).json()
        base_html = (
            '<article id="DOC-REF-API" lang="ja"><section id="REFS">'
            '<h2 id="REFS-H">参考文献</h2>'
            '<p id="REF-ITEM">ヘイドン・ホワイト『メタヒストリー』。</p>'
            "</section></article>"
        )
        with self.app.state.databases.state.connect() as connection:
            revisions = RevisionService(connection)
            document_id = revisions.create_document(
                project_id=project["id"], kind="paper", title="Reference draft"
            )
            base = revisions.commit_revision(
                document_id=document_id,
                canonical_html=base_html,
                editor_type="human",
                operation="human_import",
            )

            def insert_run(run_id: str, version: int) -> None:
                connection.execute(
                    """
                    INSERT INTO runs(
                        id, project_id, workflow_name, workflow_version,
                        status, input_json, created_at
                    ) VALUES (?, ?, 'manuscript_rewrite', ?, 'completed', '{}', ?)
                    """,
                    (run_id, project["id"], str(version), utc_now()),
                )
                connection.commit()

            def candidate(record_id: str, edition: str) -> LiteratureCandidate:
                return LiteratureCandidate(
                    provider="ndl_search",
                    provider_record_id=record_id,
                    title="メタヒストリー",
                    authors=("ヘイドン・ホワイト",),
                    publication_title=None,
                    publisher="作品社",
                    publication_date="2017",
                    record_type="図書",
                    doi=None,
                    url=f"https://example.invalid/{record_id}",
                    oa_url=None,
                    oa_status=None,
                    license_url=None,
                    provider_payload={
                        "isbn": (
                            "9780000000001"
                            if record_id.endswith("a")
                            else "9780000000002"
                        ),
                        "edition": edition,
                        "is_translation": False,
                    },
                )

            extracted = JapaneseReferenceService.extract(base_html)["entries"][0]
            resolved = JapaneseReferenceService.resolve_with_candidates(
                extracted,
                [candidate("v22-a", "初版"), candidate("v22-b", "改訂版")],
                workflow_version=22,
            )
            v22_html, _ = JapaneseReferenceService.render_sections_v22(
                base_html, [resolved]
            )
            insert_run("reference-selection-v22", 22)
            v22_proposal = ManuscriptRewriteService(connection).persist_proposal(
                run_id="reference-selection-v22",
                document_id=document_id,
                project_id=project["id"],
                base_revision_id=base.revision_id,
                outline_node_id=None,
                instructions="Select manifestations",
                use_rag=False,
                context={
                    "workflow_version": 22,
                    "reference_entries": [resolved],
                    "reference_quality_gate": JapaneseReferenceService.quality_report(
                        v22_html, [resolved], workflow_version=22
                    ),
                },
                source_html=base_html,
                proposed_html=v22_html,
                edit_summary={},
                quality_status="needs_human_review",
            )
            candidate_ids = [
                item["candidate_id"] for item in resolved["candidate_matches"]
            ]

        selected = self.client.post(
            f"/api/manuscript-rewrites/{v22_proposal['id']}/reference-selection",
            json={
                "reference_id": resolved["reference_id"],
                "candidate_ids": list(reversed(candidate_ids)),
                "expected_sha256": v22_proposal["proposed_sha256"],
            },
        )

        self.assertEqual(selected.status_code, 200, selected.text)
        selected_entry = selected.json()["context"]["reference_entries"][0]
        self.assertEqual(selected_entry["selected_candidate_ids"], candidate_ids)
        self.assertEqual(len(selected_entry["expanded_reference_entries"]), 2)
        self.assertEqual(
            selected.json()["proposed_html"].count(
                "ヘイドン・ホワイト『メタヒストリー』"
            ),
            2,
        )

        with self.app.state.databases.state.connect() as connection:
            legacy_entry = JapaneseReferenceService.extract(base_html)["entries"][0]
            legacy_entry["candidate_matches"] = [
                {
                    "candidate_id": "legacy-one",
                    "authors": ["ヘイドン・ホワイト"],
                    "title": "メタヒストリー",
                    "publisher": "作品社",
                    "year": "2017",
                    "record_type": "book",
                }
            ]
            legacy_html, _ = JapaneseReferenceService.render(base_html, [legacy_entry])
            insert_run("reference-selection-v21", 21)
            v21_proposal = ManuscriptRewriteService(connection).persist_proposal(
                run_id="reference-selection-v21",
                document_id=document_id,
                project_id=project["id"],
                base_revision_id=base.revision_id,
                outline_node_id=None,
                instructions="Select one reference",
                use_rag=False,
                context={"workflow_version": 21, "reference_entries": [legacy_entry]},
                source_html=base_html,
                proposed_html=legacy_html,
                edit_summary={},
                quality_status="needs_human_review",
            )

        rejected_plural = self.client.post(
            f"/api/manuscript-rewrites/{v21_proposal['id']}/reference-selection",
            json={
                "reference_id": legacy_entry["reference_id"],
                "candidate_ids": ["legacy-one"],
                "expected_sha256": v21_proposal["proposed_sha256"],
            },
        )
        self.assertEqual(rejected_plural.status_code, 409, rejected_plural.text)

        selected_legacy = self.client.post(
            f"/api/manuscript-rewrites/{v21_proposal['id']}/reference-selection",
            json={
                "reference_id": legacy_entry["reference_id"],
                "candidate_id": "legacy-one",
                "expected_sha256": v21_proposal["proposed_sha256"],
            },
        )
        self.assertEqual(selected_legacy.status_code, 200, selected_legacy.text)
        self.assertEqual(
            selected_legacy.json()["context"]["reference_entries"][0][
                "selected_candidate_id"
            ],
            "legacy-one",
        )

    def test_v22_semantic_review_api_is_item_scoped_and_context_cas_protected(
        self,
    ) -> None:
        project_root = self.root / "semantic-review-project"
        project = self.client.post(
            "/api/projects",
            json={"name": "Semantic review", "root_path": str(project_root)},
        ).json()
        base_html = (
            '<article id="DOC-SEM-API" lang="ja"><section id="SEC-SEM-API">'
            '<h2 id="H-SEM-API">意味判定</h2>'
            '<p id="P-SEM-API">意味関係を確認する本文。</p>'
            "</section></article>"
        )
        condition = {
            "reason": "semantic_resolution_required",
            "phase": "argument_process:claim_evidence_links",
            "unit_ids": ["link-a", "link-b"],
            "block_ids": ["P-SEM-API"],
        }
        with self.app.state.databases.state.connect() as connection:
            revisions = RevisionService(connection)
            document_id = revisions.create_document(
                project_id=project["id"], kind="paper", title="Semantic draft"
            )
            base = revisions.commit_revision(
                document_id=document_id,
                canonical_html=base_html,
                editor_type="human",
                operation="human_import",
            )
            connection.execute(
                """
                INSERT INTO runs(
                    id, project_id, workflow_name, workflow_version,
                    status, input_json, created_at
                ) VALUES ('semantic-review-api-run', ?, 'manuscript_rewrite',
                          '22', 'completed', '{}', ?)
                """,
                (project["id"], utc_now()),
            )
            connection.commit()
            proposal = ManuscriptRewriteService(connection).persist_proposal(
                run_id="semantic-review-api-run",
                document_id=document_id,
                project_id=project["id"],
                base_revision_id=base.revision_id,
                outline_node_id=None,
                instructions="Resolve one semantic unit",
                use_rag=False,
                context={
                    "workflow_version": 22,
                    "completion_status": "conditional_success",
                    "conditional_success": condition,
                    "conditional_conditions": [condition],
                    "reference_entries": [],
                    "machine_reference_entries": [],
                    "reference_quality_gate": {"passed": True, "issues": []},
                },
                source_html=base_html,
                proposed_html=base_html,
                edit_summary={},
                quality_status="needs_human_review",
            )

        fetched = self.client.get(
            f"/api/manuscript-rewrites/{proposal['id']}"
        ).json()
        review_item = fetched["context"]["semantic_review_items"][0]
        invalid = self.client.patch(
            f"/api/manuscript-rewrites/{proposal['id']}/semantic-reviews/"
            f"{review_item['review_id']}",
            json={
                "expected_sha256": fetched["proposed_sha256"],
                "expected_context_sha256": fetched[
                    "semantic_review_context_sha256"
                ],
                "decision": "arbitrary_unreviewed_value",
                "rationale": "許可されていない判断値では解消できない。",
            },
        )
        self.assertEqual(invalid.status_code, 422, invalid.text)
        payload = {
            "expected_sha256": fetched["proposed_sha256"],
            "expected_context_sha256": fetched["semantic_review_context_sha256"],
            "decision": "clarify_interpretation",
            "rationale": "第一の関係は限定された因果関係として成立する。",
        }
        resolved = self.client.patch(
            f"/api/manuscript-rewrites/{proposal['id']}/semantic-reviews/"
            f"{review_item['review_id']}",
            json=payload,
        )

        self.assertEqual(resolved.status_code, 200, resolved.text)
        remaining = resolved.json()["context"]["semantic_review_items"]
        self.assertEqual([item["unit_id"] for item in remaining], ["link-b"])
        stale = self.client.patch(
            f"/api/manuscript-rewrites/{proposal['id']}/semantic-reviews/"
            f"{remaining[0]['review_id']}",
            json={
                **payload,
                "rationale": "古いcontext SHAを再利用してはならない。",
            },
        )
        self.assertEqual(stale.status_code, 409, stale.text)
        with self.app.state.databases.state.connect() as connection:
            event = connection.execute(
                "SELECT payload_json FROM event_log "
                "WHERE run_id='semantic-review-api-run' "
                "AND event_type='manuscript.semantic_review.resolved'"
            ).fetchone()
        self.assertIsNotNone(event)
        self.assertEqual(json.loads(event[0])["unit_id"], "link-a")

    def test_v22_consulted_manifestation_patch_is_child_scoped_and_cas_guarded(
        self,
    ) -> None:
        project_root = self.root / "consulted-manifestation-confirmation"
        project = self.client.post(
            "/api/projects",
            json={"name": "Consulted manifestations", "root_path": str(project_root)},
        ).json()
        base_html = (
            '<article id="DOC-CONSULTED-API" lang="ja"><section id="REFS">'
            '<h2 id="REFS-H">参考文献</h2>'
            '<p id="REF-ITEM">原著者『複数版翻訳書』。</p>'
            "</section></article>"
        )
        source = JapaneseReferenceService.extract(base_html)["entries"][0]
        source["candidate_matches"] = [
            {
                "candidate_id": f"translation:{label}",
                "provider": "ndl_search",
                "provider_record_id": f"ndl:{label}",
                "record_type": "book",
                "title": "複数版翻訳書",
                "authors": ["原著者"],
                "publisher": "研究出版社",
                "year": year,
                "edition": edition,
                "volume": volume,
                "is_translation": True,
                "translators": [],
            }
            for label, year, edition, volume in (
                ("first", "2020", "初版", "上巻"),
                ("revised", "2024", "改訂版", "下巻"),
            )
        ]
        selected = JapaneseReferenceService.confirm_manifestations_v22(
            source, ["translation:first", "translation:revised"]
        )
        rendered, _ = JapaneseReferenceService.render_sections_v22(
            base_html, [selected]
        )
        with self.app.state.databases.state.connect() as connection:
            revisions = RevisionService(connection)
            document_id = revisions.create_document(
                project_id=project["id"], kind="paper", title="Draft"
            )
            base = revisions.commit_revision(
                document_id=document_id,
                canonical_html=base_html,
                editor_type="human",
                operation="human_import",
            )
            connection.execute(
                "INSERT INTO runs(id,project_id,workflow_name,workflow_version,status,"
                "input_json,created_at) VALUES "
                "('consulted-manifestations-v22',?,'manuscript_rewrite','22',"
                "'completed','{}',?)",
                (project["id"], utc_now()),
            )
            connection.commit()
            proposal = ManuscriptRewriteService(connection).persist_proposal(
                run_id="consulted-manifestations-v22",
                document_id=document_id,
                project_id=project["id"],
                base_revision_id=base.revision_id,
                outline_node_id=None,
                instructions="Complete selected manifestation metadata",
                use_rag=True,
                context={
                    "workflow_version": 22,
                    "reference_entries": [selected],
                    "machine_reference_entries": [],
                    "reference_quality_gate": JapaneseReferenceService.quality_report(
                        rendered, [selected], workflow_version=22
                    ),
                },
                source_html=base_html,
                proposed_html=rendered,
                edit_summary={},
                quality_status="needs_human_review",
            )

        fetched = self.client.get(f"/api/manuscript-rewrites/{proposal['id']}")
        self.assertEqual(fetched.status_code, 200, fetched.text)
        items = fetched.json()["context"]["reference_metadata_confirmation_items"]
        self.assertEqual(len(items), 2)
        self.assertTrue(
            all(item["confirmation_kind"] == "consulted_manifestation" for item in items)
        )
        first, second = items
        endpoint = (
            f"/api/manuscript-rewrites/{proposal['id']}/machine-references/"
            f"{first['reference_id']}"
        )
        patched = self.client.patch(
            endpoint,
            json={
                "expected_sha256": proposal["proposed_sha256"],
                "is_translation": True,
                "translators": ["第一版訳者"],
            },
        )
        self.assertEqual(patched.status_code, 200, patched.text)
        body = patched.json()
        parent = body["context"]["reference_entries"][0]
        children = parent["expanded_reference_entries"]
        self.assertEqual(children[0]["translators"], ["第一版訳者"])
        self.assertEqual(children[1]["translators"], [])
        self.assertTrue(parent["final_revision_blocked"])
        self.assertEqual(
            body["context"]["reference_metadata_confirmation_items"][0][
                "reference_id"
            ],
            second["reference_id"],
        )
        self.assertEqual(
            children[0]["source_reference_id"], children[1]["source_reference_id"]
        )
        self.assertNotEqual(children[0]["candidate_id"], children[1]["candidate_id"])

        stale = self.client.patch(
            endpoint,
            json={
                "expected_sha256": proposal["proposed_sha256"],
                "is_translation": True,
                "translators": ["第一版訳者"],
            },
        )
        self.assertEqual(stale.status_code, 409, stale.text)

    def test_v22_machine_reference_metadata_patch_is_cas_guarded(self) -> None:
        project_root = self.root / "machine-reference-confirmation"
        project = self.client.post(
            "/api/projects",
            json={"name": "Machine reference", "root_path": str(project_root)},
        ).json()
        base_html = (
            '<article id="DOC-MACHINE-API" lang="ja"><section id="BODY">'
            '<h2 id="BODY-H">本文</h2><p id="BODY-P">本文。</p>'
            "</section></article>"
        )
        machine_entries = [
            {
                "provider": "ndl_search",
                "provider_record_id": "machine-book",
                "title": "機械判別された翻訳書",
                "authors": ["研究者"],
                "record_type": "book",
                "publisher": "研究出版社",
                "year": "2025",
            }
        ]
        machine = JapaneseReferenceService.bibliography_sections_v22(
            [], machine_entries=machine_entries
        )[1]["entries"][0]
        rendered, _ = JapaneseReferenceService.render_sections_v22(
            base_html, [], machine_entries=machine_entries
        )
        with self.app.state.databases.state.connect() as connection:
            revisions = RevisionService(connection)
            document_id = revisions.create_document(
                project_id=project["id"], kind="paper", title="Draft"
            )
            base = revisions.commit_revision(
                document_id=document_id,
                canonical_html=base_html,
                editor_type="human",
                operation="human_import",
            )
            connection.execute(
                "INSERT INTO runs(id,project_id,workflow_name,workflow_version,status,"
                "input_json,created_at) VALUES "
                "('machine-reference-v22',?,'manuscript_rewrite','22','completed','{}',?)",
                (project["id"], utc_now()),
            )
            connection.commit()
            proposal = ManuscriptRewriteService(connection).persist_proposal(
                run_id="machine-reference-v22",
                document_id=document_id,
                project_id=project["id"],
                base_revision_id=base.revision_id,
                outline_node_id=None,
                instructions="Confirm machine metadata",
                use_rag=True,
                context={
                    "workflow_version": 22,
                    "reference_entries": [],
                    "machine_reference_entries": machine_entries,
                    "reference_quality_gate": JapaneseReferenceService.quality_report(
                        rendered,
                        [],
                        workflow_version=22,
                        machine_entries=machine_entries,
                    ),
                },
                source_html=base_html,
                proposed_html=rendered,
                edit_summary={},
                quality_status="needs_human_review",
            )

        endpoint = (
            f"/api/manuscript-rewrites/{proposal['id']}/machine-references/"
            f"{machine['machine_reference_id']}"
        )
        patched = self.client.patch(
            endpoint,
            json={
                "expected_sha256": proposal["proposed_sha256"],
                "is_translation": True,
                "translators": ["訳者甲"],
                "edition": "改訳版",
            },
        )

        self.assertEqual(patched.status_code, 200, patched.text)
        body = patched.json()
        confirmed = body["context"]["machine_reference_entries"][0]
        self.assertEqual(confirmed["consulted_status"], "not_consulted")
        self.assertEqual(
            confirmed["reference_role"], "machine_detected_not_confirmed_consulted"
        )
        self.assertEqual(confirmed["translators"], ["訳者甲"])
        self.assertEqual(confirmed["manifestation_axes"]["edition"], "identified")
        self.assertEqual(
            confirmed["manifestation_axes"]["volume"],
            "unresolved",
        )
        self.assertEqual(
            confirmed["manifestation_identity_evidence"]["volume"][
                "observed_basis_fields"
            ],
            ["year", "publisher"],
        )
        self.assertEqual(
            confirmed["manifestation_identity_evidence"]["volume"][
                "observed_basis_values"
            ],
            {"year": "2025", "publisher": "研究出版社"},
        )
        self.assertIsNone(confirmed.get("volume"))
        self.assertIn("volume", confirmed["required_missing_fields"])
        self.assertTrue(confirmed["final_revision_blocked"])
        stale = self.client.patch(
            endpoint,
            json={
                "expected_sha256": proposal["proposed_sha256"],
                "is_translation": True,
                "translators": ["訳者甲"],
            },
        )
        self.assertEqual(stale.status_code, 409, stale.text)

    def test_v22_reference_metadata_patch_explicitly_reclassifies_ordinary_no_hit(
        self,
    ) -> None:
        project_root = self.root / "ordinary-reference-reclassification"
        project = self.client.post(
            "/api/projects",
            json={"name": "Ordinary reference", "root_path": str(project_root)},
        ).json()
        base_html = (
            '<article id="DOC-ORDINARY-REF" lang="ja"><section id="REFS">'
            '<h2 id="REFS-H">参考文献</h2>'
            '<p id="REF-ITEM">研究者『調査ノート』。</p>'
            "</section></article>"
        )
        entry = JapaneseReferenceService.extract(base_html)["entries"][0]
        unresolved = JapaneseReferenceService.resolve_with_candidates(
            entry, [], workflow_version=22
        )
        rendered, _ = JapaneseReferenceService.render_sections_v22(
            base_html, [unresolved]
        )
        with self.app.state.databases.state.connect() as connection:
            revisions = RevisionService(connection)
            document_id = revisions.create_document(
                project_id=project["id"], kind="paper", title="Draft"
            )
            base = revisions.commit_revision(
                document_id=document_id,
                canonical_html=base_html,
                editor_type="human",
                operation="human_import",
            )
            connection.execute(
                "INSERT INTO runs(id,project_id,workflow_name,workflow_version,status,"
                "input_json,created_at) VALUES "
                "('ordinary-reference-v22',?,'manuscript_rewrite','22','completed','{}',?)",
                (project["id"], utc_now()),
            )
            connection.commit()
            proposal = ManuscriptRewriteService(connection).persist_proposal(
                run_id="ordinary-reference-v22",
                document_id=document_id,
                project_id=project["id"],
                base_revision_id=base.revision_id,
                outline_node_id=None,
                instructions="Classify an unresolved ordinary bibliography item",
                use_rag=True,
                context={
                    "workflow_version": 22,
                    "reference_entries": [unresolved],
                    "machine_reference_entries": [],
                    "reference_quality_gate": JapaneseReferenceService.quality_report(
                        rendered, [unresolved], workflow_version=22
                    ),
                },
                source_html=base_html,
                proposed_html=rendered,
                edit_summary={},
                quality_status="needs_human_review",
            )

        endpoint = (
            f"/api/manuscript-rewrites/{proposal['id']}/machine-references/"
            f"{unresolved['reference_id']}"
        )
        rejected = self.client.patch(
            endpoint,
            json={
                "expected_sha256": proposal["proposed_sha256"],
                "is_translation": False,
            },
        )
        self.assertEqual(rejected.status_code, 409, rejected.text)

        patched = self.client.patch(
            endpoint,
            json={
                "expected_sha256": proposal["proposed_sha256"],
                "is_translation": False,
                "is_unpublished": True,
                "record_type": "unpublished",
                "year": "2026",
                "venue": "研究会配布資料",
            },
        )

        self.assertEqual(patched.status_code, 200, patched.text)
        body = patched.json()
        confirmed = body["context"]["reference_entries"][0]
        self.assertTrue(confirmed["is_unpublished"])
        self.assertEqual(confirmed["resolution_status"], "resolved")
        self.assertEqual(confirmed["consulted_status"], "user_confirmed")
        self.assertFalse(confirmed["final_revision_blocked"])
        self.assertEqual(
            confirmed["provenance"]["is_unpublished"]["kind"], "user_confirmed"
        )
        self.assertEqual(body["context"]["machine_reference_entries"], [])
        history = body["context"]["reference_metadata_confirmation_history"][-1]
        self.assertEqual(
            history["classification_change"]["changed_fields"], ["is_unpublished"]
        )
        stale = self.client.patch(
            endpoint,
            json={
                "expected_sha256": proposal["proposed_sha256"],
                "is_translation": False,
                "is_unpublished": True,
            },
        )
        self.assertEqual(stale.status_code, 409, stale.text)

    def test_search_metadata_filter_returns_sourcespan_citation(self) -> None:
        source = self.root / "rag-source.txt"
        source.write_text("Distinctive evidence passage.", encoding="utf-8")
        with self.app.state.databases.knowledge.connect() as connection:
            IngestionService(connection, self.settings).ingest_path(
                str(source),
                source_language="en",
                material_role="primary",
                permission="quote",
            )

        response = self.client.get(
            "/api/search",
            params={
                "q": "Distinctive evidence",
                "material_role": "primary",
                "permission": "quote",
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["count"], 1)
        result = response.json()["results"][0]
        self.assertEqual(result["material_role"], "primary")
        self.assertEqual(result["permission"], "quote")
        self.assertTrue(result["citation"]["source_span_id"])
        bibliography = result["bibliography"]
        self.assertEqual(bibliography["title"], "rag-source")
        self.assertEqual(bibliography["authors"], [])
        self.assertEqual(bibliography["metadata_status"], "partial")
        detail = self.client.get(
            f"/api/source-assets/{result['source_asset_id']}/bibliography"
        )
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json()["id"], bibliography["id"])
        self.assertEqual(result["citation"]["bibliographic_record_id"], bibliography["id"])

    def test_ocr_revision_html_and_source_page_navigation(self) -> None:
        source = self.root / "source.pdf"
        source.write_bytes(b"%PDF fixture")
        with self.app.state.databases.knowledge.connect() as connection:
            connection.execute(
                """
                INSERT INTO source_assets(
                    id, sha256, byte_size, media_type, ingestion_status,
                    created_at, updated_at
                ) VALUES ('asset-api', 'hash-api', 12, 'application/pdf', 'ready', 'now', 'now')
                """
            )
            connection.execute(
                """
                INSERT INTO source_locations(
                    source_asset_id, path, is_primary, is_available,
                    first_seen_at, last_verified_at
                ) VALUES ('asset-api', ?, 1, 1, 'now', 'now')
                """,
                (str(source),),
            )
            connection.commit()
            revision_id = OCRRevisionRepository(connection).create_revision(
                source_asset_id="asset-api",
                layer="normalized_ocr",
                content="Page text",
                pages=[OCRPageInput(12, 0, 9, "Page text")],
                blocks=[OCRBlockInput("P-API", "paragraph", 0, 0, 9, "Page text")],
            )

        snapshot = self.client.get(f"/api/ocr/revisions/{revision_id}")
        self.assertEqual(snapshot.status_code, 200)
        rendered = self.client.get(f"/api/ocr/revisions/{revision_id}/html?title=Fixture")
        self.assertEqual(rendered.status_code, 200)
        self.assertIn('class="source-page-boundary"', rendered.text)
        page = self.client.get("/api/source-assets/asset-api/pages/12", follow_redirects=False)
        self.assertEqual(page.status_code, 307)
        self.assertEqual(page.headers["location"], "/api/source-assets/asset-api/file#page=12")
        source_response = self.client.get("/api/source-assets/asset-api/file")
        self.assertEqual(source_response.status_code, 200)
        self.assertEqual(source_response.content, b"%PDF fixture")
        correction = self.client.post(
            f"/api/ocr/revisions/{revision_id}/corrections",
            json={
                "corrections": {"P-API": "Corrected page text"},
                "message": "Human OCR correction",
            },
        )
        self.assertEqual(correction.status_code, 201, correction.text)
        self.assertEqual(correction.json()["layer"], "human_corrected_ocr")
        self.assertEqual(correction.json()["protected_block_ids"], ["P-API"])
        original = self.client.get(f"/api/ocr/revisions/{revision_id}").json()
        corrected = self.client.get(f"/api/ocr/revisions/{correction.json()['revision_id']}").json()
        self.assertEqual(original["content_text"], "Page text")
        self.assertEqual(corrected["content_text"], "Corrected page text")

    def test_defect_detail_resolves_state_id_to_knowledge_source_page(self) -> None:
        source = self.root / "defect-source.pdf"
        source.write_bytes(b"%PDF fixture")
        with self.app.state.databases.knowledge.connect() as connection:
            connection.execute(
                """
                INSERT INTO source_assets(
                    id, sha256, byte_size, media_type, ingestion_status,
                    created_at, updated_at
                ) VALUES ('asset-defect', 'hash-defect', 12, 'application/pdf',
                          'ready', 'now', 'now')
                """
            )
            connection.execute(
                """
                INSERT INTO source_locations(
                    source_asset_id, path, is_primary, is_available, first_seen_at
                ) VALUES ('asset-defect', ?, 1, 1, 'now')
                """,
                (str(source),),
            )
            connection.commit()
            OCRRevisionRepository(connection).create_revision(
                source_asset_id="asset-defect",
                layer="normalized_ocr",
                content="Page 12",
                pages=[OCRPageInput(12, 0, 7, "Page 12")],
                blocks=[OCRBlockInput("P-DEFECT", "paragraph", 0, 0, 7, "Page 12")],
            )
        created = self.client.post(
            "/api/runs",
            json={"workflow_name": "translation", "inputs": {"source_path": str(source)}},
        ).json()
        finding = OCRFinding(
            defect_type="page_missing",
            severity="critical",
            page_start=12,
            page_end=12,
            excerpt="p. 12",
            context="p. 11 -> p. 13",
            rationale="Page 12 is missing.",
        )
        with self.app.state.databases.state.connect() as connection:
            defect_id = DefectRepository(connection).record(
                run_id=created["run_id"],
                finding=finding,
                source_asset_id="asset-defect",
            )

        listing = self.client.get("/api/defects")
        detail = self.client.get(f"/api/defects/{defect_id}")

        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.json()[0]["id"], defect_id)
        self.assertEqual(listing.json()[0]["source_title"], "defect-source.pdf")
        self.assertEqual(listing.json()[0]["source_group_id"], "source_asset:asset-defect")
        self.assertEqual(listing.json()[0]["workflow_name"], "translation")
        self.assertNotIn("details", listing.json()[0])
        self.assertNotIn("context_text", listing.json()[0])
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["source"]["path"], str(source))
        self.assertEqual(
            detail.json()["source"]["page_url"],
            "/api/source-assets/asset-defect/pages/12",
        )

    def test_translation_memory_search_returns_human_corrected_matches(self) -> None:
        source_text = "Bonjour, monde scientifique."
        source_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        with self.app.state.databases.knowledge.connect() as connection:
            connection.execute(
                """
                INSERT INTO translation_units(
                    id, source_text, source_text_sha256, source_language,
                    target_language, created_at
                ) VALUES ('tm-unit', ?, ?, 'fr', 'ja', 'now')
                """,
                (source_text, source_hash),
            )
            connection.execute(
                """
                INSERT INTO translation_versions(
                    id, translation_unit_id, translated_text, editor_type,
                    operation, is_current, is_approved, created_at
                ) VALUES ('tm-version', 'tm-unit', 'こんにちは、科学の世界。',
                          'human', 'human_edit', 1, 1, 'now')
                """
            )
            connection.execute(
                """
                INSERT INTO translation_memory_entries(
                    id, source_text_sha256, source_text, source_language,
                    target_language, target_text, translation_unit_id,
                    translation_version_id, created_by, quality_status, created_at
                ) VALUES ('tm-entry', ?, ?, 'fr', 'ja', 'こんにちは、科学の世界。',
                          'tm-unit', 'tm-version', 'human', 'human_corrected', 'now')
                """,
                (source_hash, source_text),
            )
            connection.execute(
                """
                INSERT INTO translation_batch_alignments(
                    id, translation_version_id, batch_ordinal,
                    target_start_char, target_end_char, source_span_ids_json,
                    alignment_status, created_at
                ) VALUES ('tm-batch', 'tm-version', 0, 0, 12, '[]',
                          'exact_batch', 'now')
                """
            )
            connection.execute(
                """
                INSERT INTO translation_segment_alignments(
                    id, translation_version_id, batch_alignment_id, segment_ordinal,
                    source_batch_start_char, source_batch_end_char,
                    target_start_char, target_end_char, source_text, target_text,
                    source_span_ids_json, alignment_status, created_at
                ) VALUES ('tm-segment', 'tm-version', 'tm-batch', 0,
                          0, 28, 0, 12, ?, 'こんにちは、科学の世界。', '[]',
                          'human_confirmed', 'now')
                """,
                (source_text,),
            )
            connection.commit()

        response = self.client.get(
            "/api/translation-memory",
            params={
                "q": source_text,
                "source_language": "fr",
                "target_language": "ja",
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(response.json()["results"][0]["match_type"], "exact")
        self.assertEqual(response.json()["results"][0]["quality_status"], "human_corrected")
        with (
            self.app.state.databases.knowledge.connect() as connection,
            patch("fukamiru.translation.memory.SequenceMatcher") as matcher,
        ):
            oversized = TranslationMemoryService(connection).search(
                "長" * 20_001,
                source_language="fr",
                target_language="ja",
            )
        self.assertEqual(oversized, [])
        matcher.assert_not_called()
        alignment = self.client.get("/api/translation-versions/tm-version/alignments")
        self.assertEqual(alignment.status_code, 200, alignment.text)
        self.assertEqual(alignment.json()["segment_count"], 1)
        self.assertEqual(alignment.json()["segments"][0]["source_text"], source_text)

    def test_literature_candidate_review_api_registers_bibliographic_record(self) -> None:
        candidate = LiteratureCandidate(
            provider="openalex",
            provider_record_id="https://openalex.org/W-API",
            title="API literature fixture",
            authors=("Fixture Author",),
            publication_title="Fixture Journal",
            publisher="Fixture Publisher",
            publication_date="2026",
            record_type="article",
            doi="10.1234/api-fixture",
            url="https://openalex.org/W-API",
            oa_url="https://example.org/api-fixture.pdf",
            oa_status="gold",
            license_url="cc-by",
            provider_payload={"id": "W-API"},
        )

        class Provider:
            name = "openalex"

            def search(self, query: str, *, limit: int) -> list[LiteratureCandidate]:
                del query, limit
                return [candidate]

        with self.app.state.databases.knowledge.connect() as connection:
            LiteratureDiscoveryService(connection, providers={"openalex": Provider()}).search(
                provider="openalex", query="fixture", limit=1
            )

        pending = self.client.get("/api/literature/candidates?status=pending")
        self.assertEqual(pending.status_code, 200, pending.text)
        approved = self.client.post(
            f"/api/literature/candidates/{pending.json()[0]['id']}/review",
            json={"decision": "approve"},
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()["review_status"], "approved")
        with self.app.state.databases.knowledge.connect() as connection:
            provenance = connection.execute(
                "SELECT provider, license_url FROM bibliographic_provenance"
            ).fetchone()
        self.assertEqual(provenance["provider"], "openalex")
        self.assertEqual(provenance["license_url"], "cc-by")

    def test_deterministic_evaluation_api_persists_claim_defect(self) -> None:
        with self.app.state.databases.state.connect() as connection:
            revisions = RevisionService(connection)
            project_id = revisions.register_project(
                name="API evaluation", root_path=str(self.root / "evaluation-project")
            )
            document_id = revisions.create_document(
                project_id=project_id, kind="paper", title="Evaluation"
            )
            revision = revisions.commit_revision(
                document_id=document_id,
                canonical_html=(
                    '<article id="DOC-API-EVAL" lang="en">'
                    '<section id="SEC-API-EVAL"><h2 id="H-API-EVAL">Claim</h2>'
                    '<p id="P-API-EVAL">Unsupported.</p></section></article>'
                ),
                editor_type="agent",
                operation="create",
            )
            run = RunRepository(connection).create(
                workflow_name="translation",
                workflow_version="fixture",
                entry_node="fixture",
                project_id=project_id,
                inputs={},
            )
            connection.execute(
                """
                INSERT INTO claims(
                    id, document_id, revision_id, block_id, claim_type,
                    text, status, created_at
                ) VALUES ('claim-api-eval', ?, ?, 'P-API-EVAL', 'assertion',
                          'Unsupported.', 'proposed', ?)
                """,
                (document_id, revision.revision_id, utc_now()),
            )
            connection.commit()

        response = self.client.post(
            "/api/evaluations",
            json={"run_id": run.run_id, "revision_id": revision.revision_id},
        )

        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["status"], "completed")
        self.assertEqual(response.json()["summary"]["finding_count"], 1)
        evaluation_id = response.json()["id"]
        fetched = self.client.get(f"/api/evaluations/{evaluation_id}")
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.json()["defects"][0]["defect_type"], "unsupported_claim")

    def test_direct_quote_repair_api_creates_new_immutable_revision(self) -> None:
        source_path = self.root / "quote-source.txt"
        source_path.write_text("Original exact quotation.", encoding="utf-8")
        with self.app.state.databases.knowledge.connect() as knowledge:
            ingested = IngestionService(knowledge, self.app.state.settings).ingest_path(
                str(source_path), source_language="en"
            )
            span_id = str(
                knowledge.execute(
                    "SELECT id FROM source_spans WHERE document_unit_id = ? LIMIT 1",
                    (ingested["document_unit_id"],),
                ).fetchone()["id"]
            )
        with self.app.state.databases.state.connect() as state:
            revisions = RevisionService(state)
            project_id = revisions.register_project(
                name="Quote repair", root_path=str(self.root / "quote-repair")
            )
            document_id = revisions.create_document(
                project_id=project_id, kind="paper", title="Quote"
            )
            revision = revisions.commit_revision(
                document_id=document_id,
                canonical_html=(
                    '<article id="DOC-QUOTE" lang="en"><section id="SEC-QUOTE">'
                    '<h2 id="H-QUOTE">Quote</h2>'
                    '<p id="P-QUOTE">Altered quotation.</p></section></article>'
                ),
                editor_type="agent",
                operation="create",
            )
            run = RunRepository(state).create(
                workflow_name="translation",
                workflow_version="fixture",
                entry_node="fixture",
                project_id=project_id,
                inputs={},
            )
            state.execute(
                """
                INSERT INTO citation_events(
                    id, revision_id, block_id, source_span_id, citation_kind,
                    rendered_text, created_at
                ) VALUES ('citation-api-repair', ?, 'P-QUOTE', ?, 'quote',
                          'Altered quotation.', ?)
                """,
                (revision.revision_id, span_id, utc_now()),
            )
            state.commit()

        evaluation = self.client.post(
            "/api/evaluations",
            json={"run_id": run.run_id, "revision_id": revision.revision_id},
        )
        self.assertEqual(evaluation.status_code, 201, evaluation.text)
        defect = evaluation.json()["defects"][0]
        repaired = self.client.post(
            f"/api/evaluations/{evaluation.json()['id']}/defects/{defect['id']}/repair",
            json={"strategy": "restore_source_quote"},
        )

        self.assertEqual(repaired.status_code, 200, repaired.text)
        self.assertEqual(repaired.json()["status"], "completed")
        with self.app.state.databases.state.connect() as state:
            old_html = RevisionService(state).canonical_html(revision.revision_id)
            new_html = RevisionService(state).canonical_html(repaired.json()["output_revision_id"])
        self.assertIn("Altered quotation.", old_html)
        self.assertIn("Original exact quotation.", new_html)

    def test_authoring_run_requires_project_and_note_then_queues_prepare_node(self) -> None:
        missing_project = self.client.post(
            "/api/runs",
            json={"workflow_name": "authoring", "inputs": {"note_text": "memo"}},
        )
        self.assertEqual(missing_project.status_code, 422)

        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="Authoring API",
                root_path=str(Path(self.temp.name) / "authoring-project"),
            )
        created = self.client.post(
            "/api/runs",
            json={
                "workflow_name": "authoring",
                "project_id": project_id,
                "inputs": {"title": "Memo", "note_text": "An atomic proposition."},
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        snapshot = self.client.get(created.json()["snapshot_url"]).json()
        self.assertEqual(snapshot["active_nodes"][0]["node_id"], "prepare_note")
        self.mock_require_codex.assert_called_with(
            "codex",
            required_models={"gpt-6-luna", "gpt-5.6-sol"},
        )

    def test_authoring_folder_preview_and_run_save_structured_markdown(self) -> None:
        project_root = self.root / "authoring-folder-project"
        folder = project_root / "notes"
        folder.mkdir(parents=True)
        (folder / "目次.md").write_text("# 目次\n- 序論\n- 分析\n", encoding="utf-8")
        (folder / "序論.md").write_text("最初の主張。", encoding="utf-8")
        (folder / "分析.txt").write_text("次の根拠。", encoding="utf-8")
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="Folder notes", root_path=str(project_root)
            )

        folders = self.client.get(f"/api/projects/{project_id}/authoring-folders")
        self.assertEqual(folders.status_code, 200, folders.text)
        self.assertEqual(folders.json()["folders"], ["notes"])
        preview = self.client.post(
            f"/api/projects/{project_id}/authoring-input-preview",
            json={"folder_path": "notes"},
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(
            [item["title"] for item in preview.json()["sections"]], ["序論", "分析"]
        )

        created = self.client.post(
            "/api/runs",
            json={
                "workflow_name": "manuscript_generator",
                "project_id": project_id,
                "inputs": {"title": "統合ノート", "source_folder": "notes"},
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        output = project_root / created.json()["generated_note_path"]
        self.assertEqual(output.read_text(encoding="utf-8"), preview.json()["note_text"])
        with self.app.state.databases.state.connect() as connection:
            row = connection.execute(
                "SELECT input_json FROM runs WHERE id=?", (created.json()["run_id"],)
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertIn("最初の主張。", row["input_json"])

    def test_authoring_codex_preflight_fails_before_persisting_run(self) -> None:
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="Authoring preflight",
                root_path=str(Path(self.temp.name) / "authoring-preflight"),
            )
            before = connection.execute("SELECT COUNT(*) AS count FROM runs").fetchone()[
                "count"
            ]
        self.mock_require_codex.side_effect = FukamiruError(
            "Codex CLI 0.150.1 is too old for gpt-6-luna"
        )

        response = self.client.post(
            "/api/runs",
            json={
                "workflow_name": "authoring",
                "project_id": project_id,
                "inputs": {"note_text": "memo"},
            },
        )

        self.assertEqual(response.status_code, 503, response.text)
        self.assertIn("Codex CLI preflight failed", response.json()["detail"])
        self.assertIn("gpt-6-luna", response.json()["detail"])
        with self.app.state.databases.state.connect() as connection:
            after = connection.execute("SELECT COUNT(*) AS count FROM runs").fetchone()[
                "count"
            ]
        self.assertEqual(after, before)

    def test_manuscript_rewrite_codex_preflight_fails_before_persisting_run(self) -> None:
        with self.app.state.databases.state.connect() as connection:
            before = connection.execute("SELECT COUNT(*) AS count FROM runs").fetchone()[
                "count"
            ]
        self.mock_require_codex.side_effect = FukamiruError(
            "Codex CLI executable was not found"
        )

        response = self.client.post(
            "/api/runs",
            json={
                "workflow_name": "manuscript_rewrite",
                "inputs": {"source_path": "D:/missing.html"},
            },
        )

        self.assertEqual(response.status_code, 503, response.text)
        self.assertIn("Codex CLI executable was not found", response.json()["detail"])
        with self.app.state.databases.state.connect() as connection:
            after = connection.execute("SELECT COUNT(*) AS count FROM runs").fetchone()[
                "count"
            ]
        self.assertEqual(after, before)

    def test_authoring_contract_restart_uses_clean_snapshot_and_new_lineage(self) -> None:
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="Authoring contract restart",
                root_path=str(Path(self.temp.name) / "authoring-contract-restart"),
            )
            source_context = {
                "project_id": project_id,
                "source_revision_id": "source-revision",
                "document_id": "source-document",
                "plan_id": "source-plan",
                "plan_revision": 1,
                "note_text": "Source note.",
                "research_question": {"text": "What does the manuscript explain?"},
                "claims": [{"claim_id": "c001", "text": "Main claim"}],
                "argument_nodes": [{"node_id": "a001", "claim_id": "c001"}],
                "discourse_nodes": [{"node_id": "d001", "claim_ids": ["c001"]}],
                "manuscript_structure_plan": {
                    "sections": [{"section_id": "s001", "claim_ids": ["c001"]}]
                },
                "annotation_citation_evidence": [{"citation_id": "cite-001"}],
                "reference_entries": [{"reference_id": "ref-001"}],
                "section_contracts": [{"section_id": "stale"}],
                "body_section_outputs": [{"section_id": "stale"}],
                "conclusion_contract": {"section_id": "stale"},
                "conclusion_synthesis": {"final_answer": "stale"},
                "draft_sections": [{"section_id": "stale"}],
                "proposed_html": "<article>stale</article>",
                "_join_generations": {"draft_rewrite": 9},
            }
            source = RunRepository(connection).create(
                workflow_name="manuscript_generator",
                workflow_version=self.app.state.workflows["manuscript_generator"].version,
                entry_node="lock_section_contracts",
                project_id=project_id,
                inputs=source_context,
                resource_lane="codex_remote",
            )
            connection.execute(
                """
                UPDATE node_runs
                SET status='completed', started_at=?, completed_at=?, output_json=?
                WHERE run_id=? AND node_id='lock_section_contracts'
                """,
                (
                    utc_now(),
                    utc_now(),
                    json.dumps(source_context, ensure_ascii=False),
                    source.run_id,
                ),
            )
            connection.execute(
                "UPDATE runs SET status='completed', completed_at=? WHERE id=?",
                (utc_now(), source.run_id),
            )
            connection.commit()

        response = self.client.post(f"/api/runs/{source.run_id}/restart-authoring-from-contracts")

        self.assertEqual(response.status_code, 201, response.text)
        created = response.json()
        self.assertEqual(created["restart_node_id"], "lock_section_contracts")
        self.assertEqual(created["snapshot_run_id"], source.run_id)
        self.assertFalse(created["checkpoint_reuse"])
        self.assertNotEqual(created["plan_id"], "source-plan")
        with self.app.state.databases.state.connect() as connection:
            restarted = RunRepository(connection).get(created["run_id"])
            original = RunRepository(connection).get(source.run_id)
            active = connection.execute(
                "SELECT * FROM node_runs WHERE run_id=?",
                (created["run_id"],),
            ).fetchone()
            checkpoints = connection.execute(
                "SELECT COUNT(*) AS count FROM manuscript_unit_checkpoints WHERE run_id=?",
                (created["run_id"],),
            ).fetchone()
            event_types = {
                row["event_type"]
                for row in connection.execute(
                    "SELECT event_type FROM event_log WHERE run_id=?",
                    (created["run_id"],),
                ).fetchall()
            }
        self.assertEqual(original["status"], "completed")
        self.assertEqual(restarted["status"], "queued")
        self.assertEqual(active["node_id"], "lock_section_contracts")
        self.assertEqual(active["resource_lane"], "codex_remote")
        self.assertEqual(restarted["input"]["plan_revision"], 1)
        self.assertEqual(
            restarted["input"]["authoring_workflow_policy"],
            {
                "id": "fukamiru-authoring-v32",
                "workflow_version": 32,
                "japanese_revision_pipeline": "full-sentence-proofreading-v29.1",
                "quality_gate_mode": "fail_closed",
                "economy_priority_supported": False,
                "research_argument_architecture": "role-specific-v31",
                "footnote_marker_placement": "before-comma-and-period",
                "concept_fit": "contextual-concept-fit-v1",
            },
        )
        self.assertEqual(
            restarted["input"]["authoring_restart_lineage"]["source_plan_id"],
            "source-plan",
        )
        self.assertFalse(restarted["input"]["authoring_restart_lineage"]["checkpoint_reuse"])
        for stale_key in (
            "section_contracts",
            "body_section_outputs",
            "conclusion_contract",
            "conclusion_synthesis",
            "draft_sections",
            "proposed_html",
            "_join_generations",
        ):
            self.assertNotIn(stale_key, restarted["input"])
        self.assertEqual(checkpoints["count"], 0)
        self.assertIn("run.authoring_contract_restart_created", event_types)

        with self.app.state.databases.state.connect() as connection:
            connection.execute(
                "UPDATE runs SET status='waiting_human' WHERE id=?", (source.run_id,)
            )
            connection.commit()
        pending_response = self.client.post(
            f"/api/runs/{source.run_id}/restart-authoring-from-contracts"
        )
        self.assertEqual(pending_response.status_code, 201, pending_response.text)
        self.assertNotEqual(pending_response.json()["run_id"], created["run_id"])
        with self.app.state.databases.state.connect() as connection:
            pending_source = RunRepository(connection).get(source.run_id)
        self.assertEqual(pending_source["status"], "waiting_human")

    def test_authoring_contract_restart_rejects_unknown_source(self) -> None:
        response = self.client.post("/api/runs/missing-run/restart-authoring-from-contracts")

        self.assertEqual(response.status_code, 404)

    def test_authoring_contract_restart_uses_full_path_completed_boundary(self) -> None:
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="Authoring full-path contract restart",
                root_path=str(Path(self.temp.name) / "authoring-full-path-contract-restart"),
            )
            source = RunRepository(connection).create(
                workflow_name="authoring",
                workflow_version="31",
                entry_node="prepare_note",
                project_id=project_id,
                inputs={
                    "project_id": project_id,
                    "note_text": "A full-path source creates its plan after Run creation.",
                },
                resource_lane="codex_remote",
            )
            boundary_context = {
                "project_id": project_id,
                "plan_id": "full-path-plan",
                "plan_revision": 2,
                "parent_plan_revision": 1,
                "research_question": {"text": "What does the manuscript explain?"},
                "claims": [{"claim_id": "c001", "text": "Main claim"}],
                "argument_nodes": [{"node_id": "a001", "claim_id": "c001"}],
                "discourse_nodes": [{"node_id": "d001", "claim_ids": ["c001"]}],
                "manuscript_structure_plan": {
                    "sections": [{"section_id": "s001", "claim_ids": ["c001"]}]
                },
                "annotation_citation_evidence": [{"citation_id": "cite-001"}],
                "reference_entries": [{"reference_id": "ref-001"}],
                "section_contracts": [{"section_id": "stale"}],
                "draft_sections": [{"section_id": "stale"}],
            }
            connection.execute(
                """
                UPDATE node_runs
                SET status='completed', started_at=?, completed_at=?, output_json=?
                WHERE run_id=? AND node_id='prepare_note'
                """,
                (utc_now(), utc_now(), json.dumps({}, ensure_ascii=False), source.run_id),
            )
            connection.execute(
                """
                INSERT INTO node_runs(
                    id,run_id,node_id,attempt,status,input_json,output_json,
                    resource_lane,started_at,completed_at
                ) VALUES (?,?, 'lock_section_contracts',1,'completed',?,?,
                          'codex_remote',?,?)
                """,
                (
                    "full-path-contract-boundary",
                    source.run_id,
                    json.dumps(boundary_context, ensure_ascii=False),
                    json.dumps(boundary_context, ensure_ascii=False),
                    utc_now(),
                    utc_now(),
                ),
            )
            connection.execute(
                "UPDATE runs SET status='failed', completed_at=? WHERE id=?",
                (utc_now(), source.run_id),
            )
            connection.commit()

        response = self.client.post(f"/api/runs/{source.run_id}/restart-authoring-from-contracts")

        self.assertEqual(response.status_code, 201, response.text)
        created = response.json()
        self.assertEqual(created["snapshot_run_id"], source.run_id)
        with self.app.state.databases.state.connect() as connection:
            restarted = RunRepository(connection).get(created["run_id"])
        lineage = restarted["input"]["authoring_restart_lineage"]
        self.assertEqual(lineage["source_plan_id"], "full-path-plan")
        self.assertTrue(lineage["source_identity_inferred_from_completed_node"])
        self.assertNotEqual(restarted["input"]["plan_id"], "full-path-plan")
        self.assertEqual(restarted["input"]["plan_revision"], 1)
        self.assertNotIn("parent_plan_revision", restarted["input"])
        self.assertNotIn("section_contracts", restarted["input"])
        self.assertNotIn("draft_sections", restarted["input"])

    def test_authoring_draft_restart_uses_completed_pre_draft_handoff(self) -> None:
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="Authoring pre-draft restart",
                root_path=str(Path(self.temp.name) / "authoring-pre-draft-restart"),
            )
            source_context = {
                "project_id": project_id,
                "plan_id": "shared-locked-plan",
                "plan_revision": 1,
                "research_problem": {"research_question": "Question"},
                "argument_nodes": [{"id": "a1"}],
                "claim_scopes": [{"claim_id": "c1"}],
                "defense_obligations": [{"id": "d1"}],
                "section_contracts": [{"section_id": "s1"}],
                "semantic_paragraph_plan": [{"section_id": "s1"}],
                "introduction_contract": {"section_id": "intro"},
                "conclusion_contract": {"section_id": "conclusion"},
                "body_section_outputs": [{"section_id": "s1"}],
                "reference_entries": [{"reference_id": "r1"}],
                "draft_sections": [{"section_id": "stale"}],
                "proposed_html": "<article>stale</article>",
                "audit_findings": [{"code": "stale"}],
            }
            source = RunRepository(connection).create(
                workflow_name="authoring",
                workflow_version="31",
                entry_node="draft_rewrite",
                project_id=project_id,
                inputs=source_context,
                resource_lane="codex_remote",
            )
            connection.commit()
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute(
                """
                INSERT INTO section_drafts(
                    id,contract_id,attempt,body,used_claim_ids_json,
                    used_evidence_ids_json,new_propositions_json,content_sha256,created_at
                ) VALUES (?,?,1,'old','[]','[]','[]','old-sha',?)
                """,
                ("existing-draft", "shared-locked-plan@1:s1", utc_now()),
            )
            connection.commit()
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                """
                UPDATE node_runs
                SET status='completed', started_at=?, completed_at=?, output_json=?
                WHERE run_id=? AND node_id='draft_rewrite'
                """,
                (
                    utc_now(),
                    utc_now(),
                    json.dumps(source_context, ensure_ascii=False),
                    source.run_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO node_runs(
                    id,run_id,node_id,attempt,status,input_json,output_json,
                    resource_lane,started_at,completed_at
                ) VALUES (?,?,'audit_structure',1,'completed','{}',?,
                          'codex_remote',?,?)
                """,
                (
                    f"{source.run_id}-audit",
                    source.run_id,
                    json.dumps(
                        {
                            "audit_findings": [
                                {
                                    "code": "evidence_mismatch",
                                    "severity": "major",
                                    "message": "Evidence supports a different proposition.",
                                    "object_type": "section",
                                    "object_id": "s1",
                                    "repair_scope": "targeted",
                                    "unsafe_internal_detail": "must not cross the restart boundary",
                                },
                                {
                                    "code": "informational",
                                    "severity": "info",
                                    "message": "No repair required.",
                                },
                            ]
                        },
                        ensure_ascii=False,
                    ),
                    utc_now(),
                    utc_now(),
                ),
            )
            connection.execute(
                "UPDATE runs SET status='completed', completed_at=? WHERE id=?",
                (utc_now(), source.run_id),
            )
            connection.commit()

        response = self.client.post(f"/api/runs/{source.run_id}/restart-authoring-from-draft")

        self.assertEqual(response.status_code, 201, response.text)
        created = response.json()
        self.assertEqual(created["restart_node_id"], "draft_rewrite")
        self.assertEqual(created["snapshot_run_id"], source.run_id)
        self.assertEqual(created["plan_id"], "shared-locked-plan")
        with self.app.state.databases.state.connect() as connection:
            restarted = RunRepository(connection).get(created["run_id"])
        self.assertEqual(restarted["input"]["plan_id"], "shared-locked-plan")
        self.assertEqual(restarted["input"]["draft_attempt"], 2)
        self.assertEqual(
            restarted["input"]["authoring_draft_restart_lineage"]["source_plan_id"],
            "shared-locked-plan",
        )
        self.assertEqual(
            restarted["input"]["authoring_draft_restart_lineage"]["draft_attempt"],
            2,
        )
        self.assertEqual(
            restarted["input"]["authoring_draft_restart_lineage"]["policy"],
            "authoring-pre-draft-boundary-restart-v2",
        )
        self.assertEqual(
            restarted["input"]["authoring_draft_restart_lineage"][
                "inherited_audit_finding_count"
            ],
            1,
        )
        self.assertEqual(
            restarted["input"]["authoring_draft_restart_findings"],
            [
                {
                    "code": "evidence_mismatch",
                    "severity": "major",
                    "message": "Evidence supports a different proposition.",
                    "object_type": "section",
                    "object_id": "s1",
                    "repair_scope": "targeted",
                }
            ],
        )
        self.assertNotIn("draft_sections", restarted["input"])
        self.assertNotIn("proposed_html", restarted["input"])
        self.assertNotIn("audit_findings", restarted["input"])

    def test_authoring_draft_restart_uses_failed_draft_input_handoff(self) -> None:
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="Failed authoring draft restart",
                root_path=str(Path(self.temp.name) / "failed-authoring-draft-restart"),
            )
            source_context = {
                "project_id": project_id,
                "plan_id": "failed-draft-plan",
                "plan_revision": 1,
                "research_problem": {"research_question": "Question"},
                "argument_nodes": [{"id": "a1"}],
                "claim_scopes": [{"claim_id": "c1"}],
                "defense_obligations": [{"id": "d1"}],
                "section_contracts": [{"section_id": "s1"}],
                "semantic_paragraph_plan": [{"section_id": "s1"}],
                "introduction_contract": {"section_id": "intro"},
                "conclusion_contract": {"section_id": "conclusion"},
                "body_section_outputs": [{"section_id": "s1"}],
                "reference_entries": [{"reference_id": "r1"}],
            }
            source = RunRepository(connection).create(
                workflow_name="authoring",
                workflow_version="31",
                entry_node="draft_rewrite",
                project_id=project_id,
                inputs=source_context,
                resource_lane="codex_remote",
            )
            failed_node = connection.execute(
                "SELECT id FROM node_runs WHERE run_id=? AND node_id='draft_rewrite'",
                (source.run_id,),
            ).fetchone()
            connection.execute(
                """
                UPDATE node_runs SET status='failed', started_at=?, completed_at=?,
                    error_json=? WHERE id=?
                """,
                (
                    utc_now(),
                    utc_now(),
                    json.dumps(
                        {
                            "type": "NonRetryableFukamiruError",
                            "message": "V31 role draft JSON handoff violation: missing citation",
                        }
                    ),
                    failed_node["id"],
                ),
            )
            connection.execute(
                "UPDATE runs SET status='failed', completed_at=? WHERE id=?",
                (utc_now(), source.run_id),
            )
            connection.commit()

        response = self.client.post(
            f"/api/runs/{source.run_id}/restart-authoring-from-draft"
        )

        self.assertEqual(response.status_code, 201, response.text)
        created = response.json()
        self.assertEqual(created["restart_node_id"], "draft_rewrite")
        self.assertEqual(created["snapshot_node_run_id"], failed_node["id"])
        with self.app.state.databases.state.connect() as connection:
            restarted = RunRepository(connection).get(created["run_id"])
        self.assertEqual(restarted["input"]["research_problem"], source_context["research_problem"])
        self.assertEqual(
            restarted["input"]["section_contracts"], source_context["section_contracts"]
        )
        self.assertEqual(
            restarted["input"]["semantic_paragraph_plan"],
            source_context["semantic_paragraph_plan"],
        )
        self.assertNotIn("draft_sections", restarted["input"])

    def test_failed_derived_authoring_node_can_be_replayed_once_in_place(self) -> None:
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="Authoring failed-node replay",
                root_path=str(Path(self.temp.name) / "authoring-failed-node-replay"),
            )
            node_input = {
                "project_id": project_id,
                "plan_id": "derived-plan",
                "authoring_restart_lineage": {"policy": "authoring-contract-boundary-restart-v1"},
                "section_contracts": [{"section_id": "s001"}],
            }
            run = RunRepository(connection).create(
                workflow_name="authoring",
                workflow_version="30",
                entry_node="lock_section_contracts",
                project_id=project_id,
                inputs=node_input,
                resource_lane="codex_remote",
            )
            connection.execute(
                """
                UPDATE node_runs
                SET status='completed', started_at=?, completed_at=?, output_json=?
                WHERE run_id=? AND node_id='lock_section_contracts'
                """,
                (utc_now(), utc_now(), json.dumps(node_input), run.run_id),
            )
            error = json.dumps(
                {
                    "type": "FukamiruError",
                    "message": "Codex authoring stage plan_semantic_paragraphs failed: transport",
                }
            )
            for attempt in (1, 2, 3):
                connection.execute(
                    """
                    INSERT INTO node_runs(
                        id,run_id,node_id,attempt,status,input_json,error_json,
                        resource_lane,started_at,completed_at
                    ) VALUES (?,?,?,?, 'failed',?,?, 'codex_remote',?,?)
                    """,
                    (
                        f"semantic-attempt-{attempt}",
                        run.run_id,
                        "plan_semantic_paragraphs",
                        attempt,
                        json.dumps(node_input),
                        error,
                        utc_now(),
                        utc_now(),
                    ),
                )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                (error, utc_now(), run.run_id),
            )
            connection.commit()

        response = self.client.post(f"/api/runs/{run.run_id}/replay-failed-authoring-node")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["node_id"], "plan_semantic_paragraphs")
        self.assertEqual(response.json()["attempt"], 3)
        self.assertEqual(response.json()["manual_replay_count"], 1)
        with self.app.state.databases.state.connect() as connection:
            statuses = [
                (row["attempt"], row["status"])
                for row in connection.execute(
                    """
                    SELECT attempt,status FROM node_runs
                    WHERE run_id=? AND node_id='plan_semantic_paragraphs'
                    ORDER BY attempt
                    """,
                    (run.run_id,),
                ).fetchall()
            ]
            run_status = connection.execute(
                "SELECT status FROM runs WHERE id=?", (run.run_id,)
            ).fetchone()["status"]
        self.assertEqual(statuses, [(1, "failed"), (2, "failed"), (3, "queued")])
        self.assertEqual(run_status, "queued")

    def test_failed_derived_draft_metadata_node_can_be_replayed_in_place(self) -> None:
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="Authoring draft metadata replay",
                root_path=str(Path(self.temp.name) / "authoring-draft-metadata-replay"),
            )
            node_input = {
                "project_id": project_id,
                "plan_id": "derived-plan",
                "authoring_restart_lineage": {
                    "policy": "authoring-contract-boundary-restart-v1"
                },
                "section_contracts": [{"section_id": "s004"}],
            }
            run = RunRepository(connection).create(
                workflow_name="authoring",
                workflow_version="30",
                entry_node="draft_rewrite",
                project_id=project_id,
                inputs=node_input,
                resource_lane="codex_remote",
            )
            error = json.dumps(
                {
                    "type": "NonRetryableFukamiruError",
                    "message": (
                        "Draft section contract violation after one bounded retry: "
                        "section s004 marked unverified material as external-source evidence"
                    ),
                }
            )
            connection.execute(
                """
                UPDATE node_runs
                SET status='failed', started_at=?, completed_at=?, error_json=?
                WHERE run_id=? AND node_id='draft_rewrite'
                """,
                (utc_now(), utc_now(), error, run.run_id),
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                (error, utc_now(), run.run_id),
            )
            connection.commit()

        response = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["node_id"], "draft_rewrite")
        with self.app.state.databases.state.connect() as connection:
            event = connection.execute(
                """
                SELECT payload_json FROM event_log
                WHERE run_id=? AND event_type='run.failed_authoring_node_replayed'
                """,
                (run.run_id,),
            ).fetchone()
        self.assertTrue(json.loads(event["payload_json"])["draft_metadata_recovery"])

    def test_v31_checkpoint_repair_json_handoff_can_replay_once(self) -> None:
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="Authoring V31 checkpoint repair replay",
                root_path=str(Path(self.temp.name) / "authoring-v31-repair-replay"),
            )
            run = RunRepository(connection).create(
                workflow_name="authoring",
                workflow_version="31",
                entry_node="repair_failed_sections",
                project_id=project_id,
                inputs={
                    "section_contracts": [{"section_id": "s01"}],
                    "argument_nodes": [{"id": "e1"}],
                    "authoring_recovery_lineage": {
                        "source_node_id": "join_draft_audits"
                    },
                },
                resource_lane="codex_remote",
            )
            error = json.dumps({
                "type": "FukamiruError",
                "message": (
                    "V31 targeted repair repeatedly broke its JSON section contract: "
                    "section s01 did not realize enough contract evidence"
                ),
            })
            connection.execute(
                "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                "error_json=? WHERE run_id=? AND node_id='repair_failed_sections'",
                (utc_now(), utc_now(), error, run.run_id),
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                (error, utc_now(), run.run_id),
            )
            connection.commit()

        response = self.client.post(f"/api/runs/{run.run_id}/replay-failed-authoring-node")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["node_id"], "repair_failed_sections")
        self.assertEqual(response.json()["attempt"], 2)
        with self.app.state.databases.state.connect() as connection:
            event = connection.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed'",
                (run.run_id,),
            ).fetchone()
        self.assertTrue(json.loads(event["payload_json"])["v31_repair_handoff_recovery"])

    def test_v31_repair_capacity_failure_requeues_same_attempt_and_input(self) -> None:
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="Authoring V31 repair capacity replay",
                root_path=str(Path(self.temp.name) / "authoring-v31-repair-capacity"),
            )
            inputs = {
                "section_contracts": [{"section_id": "s01"}],
                "argument_nodes": [{"id": "e1"}],
                "draft_sections": [{"section_id": "s01"}],
                "audit_findings": [
                    {
                        "code": "section_output_gap",
                        "object_id": "s01",
                        "severity": "major",
                    }
                ],
                "authoring_recovery_lineage": {
                    "source_node_id": "join_draft_audits"
                },
            }
            run = RunRepository(connection).create(
                workflow_name="authoring",
                workflow_version="31",
                entry_node="repair_failed_sections",
                project_id=project_id,
                inputs=inputs,
                resource_lane="codex_remote",
            )
            node = connection.execute(
                "SELECT id,input_json FROM node_runs WHERE run_id=?",
                (run.run_id,),
            ).fetchone()
            error = json.dumps(
                {
                    "type": "FukamiruError",
                    "message": (
                        "Codex authoring stage draft_body_v31 failed (exit 1): "
                        "Selected model is at capacity. Please try again."
                    ),
                }
            )
            connection.execute(
                "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                "error_json=? WHERE id=?",
                (utc_now(), utc_now(), error, node["id"]),
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                (error, utc_now(), run.run_id),
            )
            connection.execute(
                "INSERT INTO event_log(run_id,event_type,object_type,object_id,"
                "payload_json,created_at) VALUES "
                "(?,'run.failed_authoring_node_replayed','node_run',?,'{}',?)",
                (run.run_id, node["id"], utc_now()),
            )
            connection.commit()

        response = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["node_id"], "repair_failed_sections")
        self.assertEqual(response.json()["attempt"], 1)
        with self.app.state.databases.state.connect() as connection:
            replayed = connection.execute(
                "SELECT status,input_json FROM node_runs WHERE id=?",
                (node["id"],),
            ).fetchone()
            event = connection.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? AND object_id=? "
                "AND event_type='run.failed_authoring_node_replayed' "
                "ORDER BY rowid DESC LIMIT 1",
                (run.run_id, node["id"]),
            ).fetchone()
        self.assertEqual(replayed["status"], "queued")
        self.assertEqual(replayed["input_json"], node["input_json"])
        self.assertTrue(
            json.loads(event["payload_json"])["v31_repair_transport_recovery"]
        )

    def test_v31_required_citation_placement_contract_can_replay_once(self) -> None:
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="Authoring V31 required citation placement replay",
                root_path=str(Path(self.temp.name) / "authoring-v31-citation-placement"),
            )
            run = RunRepository(connection).create(
                workflow_name="authoring",
                workflow_version="31",
                entry_node="repair_failed_sections",
                project_id=project_id,
                inputs={
                    "section_contracts": [{"section_id": "s002"}],
                    "argument_nodes": [{"id": "citation-1", "node_type": "evidence"}],
                    "draft_sections": [{"section_id": "s002"}],
                    "audit_findings": [
                        {
                            "code": "evidence_mismatch",
                            "severity": "major",
                            "object_id": "s002",
                            "message": "citation-1 is attached to the wrong work.",
                        }
                    ],
                },
                resource_lane="codex_remote",
            )
            error = json.dumps(
                {
                    "type": "FukamiruError",
                    "message": (
                        "Authoring citation contract rejected required scholarship evidence "
                        "for section s002: ['citation-1']"
                    ),
                }
            )
            connection.execute(
                "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                "error_json=? WHERE run_id=? AND node_id='repair_failed_sections'",
                (utc_now(), utc_now(), error, run.run_id),
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                (error, utc_now(), run.run_id),
            )
            connection.commit()

        response = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["node_id"], "repair_failed_sections")
        with self.app.state.databases.state.connect() as connection:
            event = connection.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed'",
                (run.run_id,),
            ).fetchone()
        payload = json.loads(event["payload_json"])
        self.assertEqual(
            payload["policy"],
            "authoring-v31-required-citation-placement-recovery-v1",
        )
        self.assertTrue(payload["v31_required_citation_contract_recovery"])

    def test_v31_full_path_repair_json_handoff_can_replay(self) -> None:
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="Authoring V31 full path repair replay",
                root_path=str(Path(self.temp.name) / "authoring-v31-full-repair"),
            )
            run = RunRepository(connection).create(
                workflow_name="authoring",
                workflow_version="31",
                entry_node="repair_failed_sections",
                project_id=project_id,
                inputs={
                    "section_contracts": [{"section_id": "s01"}],
                    "argument_nodes": [{"id": "e1"}],
                    "draft_sections": [{"section_id": "s01"}],
                    "audit_findings": [{"code": "citation_anchor", "severity": "major"}],
                    "conclusion_synthesis": {"final_answer": "答え"},
                },
                resource_lane="codex_remote",
            )
            connection.execute(
                "INSERT INTO node_runs(id,run_id,node_id,attempt,status,input_json,"
                "output_json,resource_lane) VALUES (?,?,?,?,?,?,?,?)",
                (
                    f"{run.run_id}-join",
                    run.run_id,
                    "join_draft_audits",
                    1,
                    "completed",
                    "{}",
                    "{}",
                    "ingestion_cpu",
                ),
            )
            error = json.dumps({
                "type": "NonRetryableFukamiruError",
                "message": (
                    "V31 repair broke a JSON section contract: "
                    "section s01 scholarship evidence e1 has no unique sentence-level "
                    "footnote anchor"
                ),
            })
            connection.execute(
                "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                "error_json=? WHERE run_id=? AND node_id='repair_failed_sections'",
                (utc_now(), utc_now(), error, run.run_id),
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                (error, utc_now(), run.run_id),
            )
            connection.commit()

        response = self.client.post(f"/api/runs/{run.run_id}/replay-failed-authoring-node")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["node_id"], "repair_failed_sections")

        with self.app.state.databases.state.connect() as connection:
            connection.execute(
                "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                "error_json=? WHERE run_id=? AND node_id='repair_failed_sections'",
                (utc_now(), utc_now(), error, run.run_id),
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                (error, utc_now(), run.run_id),
            )
            connection.commit()

        response = self.client.post(f"/api/runs/{run.run_id}/replay-failed-authoring-node")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["manual_replay_count"], 2)

    def test_v31_restarted_run_repair_json_handoff_can_replay(self) -> None:
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="Authoring V31 restarted repair replay",
                root_path=str(Path(self.temp.name) / "authoring-v31-restarted-repair"),
            )
            run = RunRepository(connection).create(
                workflow_name="authoring",
                workflow_version="31",
                entry_node="repair_failed_sections",
                project_id=project_id,
                inputs={
                    "section_contracts": [{"section_id": "s01"}],
                    "argument_nodes": [{"id": "e1"}],
                    "draft_sections": [{"section_id": "s01"}],
                    "audit_findings": [{"section_id": "s01", "code": "repair"}],
                    "conclusion_synthesis": {"final_answer": "answer"},
                    "authoring_restart_lineage": {
                        "restart_node_id": "lock_section_contracts"
                    },
                },
                resource_lane="codex_remote",
            )
            error = json.dumps({
                "type": "FukamiruError",
                "message": "Conclusion new evidence metadata disagrees with realized evidence",
            })
            connection.execute(
                "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                "error_json=? WHERE run_id=? AND node_id='repair_failed_sections'",
                (utc_now(), utc_now(), error, run.run_id),
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                (error, utc_now(), run.run_id),
            )
            connection.commit()

        response = self.client.post(f"/api/runs/{run.run_id}/replay-failed-authoring-node")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["node_id"], "repair_failed_sections")

    def test_v31_exhausted_structure_audit_can_start_an_additional_repair_cycle(
        self,
    ) -> None:
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="Authoring V31 exhausted audit repair",
                root_path=str(Path(self.temp.name) / "authoring-v31-exhausted-audit"),
            )
            inputs = {
                "section_contracts": [{"section_id": "s01"}],
                "argument_nodes": [{"id": "e1"}],
                "draft_sections": [{"section_id": "s01"}],
                "audit_findings": [
                    {
                        "code": "evidence_mismatch",
                        "severity": "major",
                        "object_id": "s01",
                    }
                ],
                "repair_count": 2,
                "extended_structure_repair_count": 2,
            }
            run = RunRepository(connection).create(
                workflow_name="authoring",
                workflow_version="31",
                entry_node="join_draft_audits",
                project_id=project_id,
                inputs=inputs,
                resource_lane="ingestion_cpu",
            )
            error = json.dumps(
                {
                    "type": "NonRetryableFukamiruError",
                    "message": (
                        "V31 draft has unresolved major structural findings after "
                        "bounded repair: evidence_mismatch"
                    ),
                }
            )
            connection.execute(
                "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                "error_json=? WHERE run_id=? AND node_id='join_draft_audits'",
                (utc_now(), utc_now(), error, run.run_id),
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                (error, utc_now(), run.run_id),
            )
            connection.commit()

        response = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["node_id"], "repair_failed_sections")
        self.assertEqual(response.json()["attempt"], 1)
        with self.app.state.databases.state.connect() as connection:
            row = connection.execute(
                "SELECT node_id,attempt,status,resource_lane,input_json FROM node_runs "
                "WHERE id=?",
                (response.json()["node_run_id"],),
            ).fetchone()
            replay_input = PayloadStore(connection).unpack(
                json.loads(row["input_json"])
            )
            event = connection.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed'",
                (run.run_id,),
            ).fetchone()
        self.assertEqual(
            (row["node_id"], row["attempt"], row["status"], row["resource_lane"]),
            ("repair_failed_sections", 1, "queued", "codex_remote"),
        )
        self.assertEqual(replay_input["repair_count"], 0)
        self.assertEqual(replay_input["extended_structure_repair_count"], 3)
        self.assertEqual(
            replay_input["authoring_recovery_lineage"]["source_node_id"],
            "join_draft_audits",
        )
        self.assertTrue(
            json.loads(event["payload_json"])[
                "v31_audit_repair_exhausted_recovery"
            ]
        )

    def test_v31_local_section_audit_can_use_two_narrow_repair_cycles(self) -> None:
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="Authoring V31 local audit repair",
                root_path=str(Path(self.temp.name) / "authoring-v31-local-audit"),
            )
            finding = {
                "code": "evidence_mismatch",
                "severity": "major",
                "object_type": "section",
                "object_id": "section-02",
                "repair_scope": "local_section",
                "message": "Move the evidence treatment to the sentence it supports.",
            }
            inputs = {
                "section_contracts": [{"section_id": "section-02"}],
                "argument_nodes": [{"id": "e1"}],
                "draft_sections": [{"section_id": "section-02"}],
                "audit_findings": [finding],
                "repair_count": 2,
                "extended_structure_repair_count": 4,
            }
            run = RunRepository(connection).create(
                workflow_name="authoring",
                workflow_version="31",
                entry_node="join_draft_audits",
                project_id=project_id,
                inputs=inputs,
                resource_lane="ingestion_cpu",
            )
            error = json.dumps(
                {
                    "type": "NonRetryableFukamiruError",
                    "message": (
                        "V31 draft has unresolved major structural findings after "
                        "bounded repair: evidence_mismatch"
                    ),
                }
            )
            connection.execute(
                "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                "error_json=? WHERE run_id=? AND node_id='join_draft_audits'",
                (utc_now(), utc_now(), error, run.run_id),
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                (error, utc_now(), run.run_id),
            )
            connection.commit()

        response = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )

        self.assertEqual(response.status_code, 200, response.text)
        with self.app.state.databases.state.connect() as connection:
            row = connection.execute(
                "SELECT input_json FROM node_runs WHERE id=?",
                (response.json()["node_run_id"],),
            ).fetchone()
            replay_input = PayloadStore(connection).unpack(
                json.loads(row["input_json"])
            )
        self.assertEqual(replay_input["extended_structure_repair_count"], 5)
        self.assertEqual(
            len(replay_input["authoring_extended_structure_repair_signatures"]),
            1,
        )

    def test_v31_local_section_audit_will_not_replay_same_findings_twice(
        self,
    ) -> None:
        finding = {
            "code": "evidence_mismatch",
            "severity": "major",
            "object_type": "section",
            "object_id": "section-02",
            "repair_scope": "local_section",
            "message": "Move the evidence treatment to the sentence it supports.",
        }
        signature = hashlib.sha256(
            json.dumps(
                [finding],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="Authoring V31 repeated local audit",
                root_path=str(Path(self.temp.name) / "authoring-v31-repeated-audit"),
            )
            run = RunRepository(connection).create(
                workflow_name="authoring",
                workflow_version="31",
                entry_node="join_draft_audits",
                project_id=project_id,
                inputs={
                    "section_contracts": [{"section_id": "section-02"}],
                    "argument_nodes": [{"id": "e1"}],
                    "draft_sections": [{"section_id": "section-02"}],
                    "audit_findings": [finding],
                    "extended_structure_repair_count": 5,
                    "authoring_extended_structure_repair_signatures": [signature],
                },
                resource_lane="ingestion_cpu",
            )
            error = json.dumps(
                {
                    "type": "NonRetryableFukamiruError",
                    "message": (
                        "V31 draft has unresolved major structural findings after "
                        "bounded repair: evidence_mismatch"
                    ),
                }
            )
            connection.execute(
                "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                "error_json=? WHERE run_id=? AND node_id='join_draft_audits'",
                (utc_now(), utc_now(), error, run.run_id),
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                (error, utc_now(), run.run_id),
            )
            connection.commit()

        response = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )

        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("no replayable input", response.text)

    def test_v31_section_scoped_plan_revision_can_use_last_narrow_cycle(
        self,
    ) -> None:
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="Authoring V31 section-scoped conclusion repair",
                root_path=str(
                    Path(self.temp.name) / "authoring-v31-section-plan-repair"
                ),
            )
            finding = {
                "code": "conclusion_derivation_gap",
                "severity": "major",
                "object_type": "section",
                "object_id": "section-conclusion",
                "repair_scope": "plan_revision",
                "message": (
                    "Narrow the existing conclusion answer to the established section outputs."
                ),
            }
            inputs = {
                "section_contracts": [{"section_id": "section-conclusion"}],
                "argument_nodes": [{"id": "e1"}],
                "draft_sections": [{"section_id": "section-conclusion"}],
                "audit_findings": [finding],
                "extended_structure_repair_count": 5,
            }
            run = RunRepository(connection).create(
                workflow_name="authoring",
                workflow_version="31",
                entry_node="join_draft_audits",
                project_id=project_id,
                inputs=inputs,
                resource_lane="ingestion_cpu",
            )
            error = json.dumps(
                {
                    "type": "NonRetryableFukamiruError",
                    "message": (
                        "V31 draft has unresolved major structural findings after "
                        "bounded repair: conclusion_derivation_gap"
                    ),
                }
            )
            connection.execute(
                "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                "error_json=? WHERE run_id=? AND node_id='join_draft_audits'",
                (utc_now(), utc_now(), error, run.run_id),
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                (error, utc_now(), run.run_id),
            )
            connection.commit()

        response = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )

        self.assertEqual(response.status_code, 200, response.text)
        with self.app.state.databases.state.connect() as connection:
            row = connection.execute(
                "SELECT input_json FROM node_runs WHERE id=?",
                (response.json()["node_run_id"],),
            ).fetchone()
            replay_input = PayloadStore(connection).unpack(
                json.loads(row["input_json"])
            )
        self.assertEqual(replay_input["extended_structure_repair_count"], 6)

    def test_v31_structure_repair_policy_upgrade_can_replay_at_cycle_limit_once(
        self,
    ) -> None:
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="Authoring V31 repair policy upgrade",
                root_path=str(Path(self.temp.name) / "authoring-v31-policy-upgrade"),
            )
            inputs = {
                "section_contracts": [{"section_id": "section-03"}],
                "argument_nodes": [{"id": "e1"}],
                "draft_sections": [{"section_id": "section-03"}],
                "audit_findings": [
                    {
                        "code": "evidence_mismatch",
                        "severity": "major",
                        "object_type": "section",
                        "object_id": "section-03",
                        "repair_scope": "local_section",
                        "message": "Remove the unsupported evidence attachment.",
                    }
                ],
                "extended_structure_repair_count": 6,
            }
            run = RunRepository(connection).create(
                workflow_name="authoring",
                workflow_version="31",
                entry_node="join_draft_audits",
                project_id=project_id,
                inputs=inputs,
                resource_lane="ingestion_cpu",
            )
            error = json.dumps(
                {
                    "type": "NonRetryableFukamiruError",
                    "message": (
                        "V31 draft has unresolved major structural findings after "
                        "bounded repair: evidence_mismatch"
                    ),
                }
            )
            connection.execute(
                "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                "error_json=? WHERE run_id=? AND node_id='join_draft_audits'",
                (utc_now(), utc_now(), error, run.run_id),
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                (error, utc_now(), run.run_id),
            )
            connection.commit()

        response = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )

        self.assertEqual(response.status_code, 200, response.text)
        with self.app.state.databases.state.connect() as connection:
            row = connection.execute(
                "SELECT input_json FROM node_runs WHERE id=?",
                (response.json()["node_run_id"],),
            ).fetchone()
            replay_input = PayloadStore(connection).unpack(
                json.loads(row["input_json"])
            )
        self.assertEqual(replay_input["extended_structure_repair_count"], 7)
        self.assertEqual(
            replay_input["authoring_structure_repair_policy"],
            "history-aware-minimal-sentence-audit-repair-v8",
        )

    def test_v31_coherence_packet_projection_failure_can_replay_in_place(self) -> None:
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="Authoring V31 coherence packet projection",
                root_path=str(Path(self.temp.name) / "authoring-v31-packet-projection"),
            )
            inputs = {
                "proposed_html": "<article><p>本文</p></article>",
                "certainty_calibration": {"policy": "scope-first-v31"},
                "authoring_restart_lineage": {"policy": "authoring-v31"},
            }
            run = RunRepository(connection).create(
                workflow_name="authoring",
                workflow_version="31",
                entry_node="validate_draft_coherence",
                project_id=project_id,
                inputs=inputs,
                resource_lane="codex_remote",
            )
            error = json.dumps(
                {
                    "type": "CoherenceResolutionRequired",
                    "message": (
                        "v26.prompt_component_too_large: section plan or a single "
                        "block must be split upstream"
                    ),
                }
            )
            connection.execute(
                "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                "error_json=? WHERE run_id=? AND node_id='validate_draft_coherence'",
                (utc_now(), utc_now(), error, run.run_id),
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                (error, utc_now(), run.run_id),
            )
            connection.commit()

        response = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["node_id"], "validate_draft_coherence")
        self.assertEqual(response.json()["attempt"], 1)

    def test_v31_checkpoint_polish_role_failure_can_replay_once(self) -> None:
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="Authoring V31 polish role replay",
                root_path=str(Path(self.temp.name) / "authoring-v31-polish-replay"),
            )
            run = RunRepository(connection).create(
                workflow_name="authoring",
                workflow_version="31",
                entry_node="polish_prose",
                project_id=project_id,
                inputs={
                    "draft_sections": [{"section_id": "s1"}],
                    "conclusion_synthesis": {"final_answer": "Answer"},
                    "authoring_recovery_lineage": {
                        "source_node_id": "join_draft_audits"
                    },
                },
                resource_lane="codex_remote",
            )
            error = json.dumps({
                "type": "FukamiruError",
                "message": (
                    "V31 polish changed role content: Conclusion prose omitted "
                    "its synthesized final answer"
                ),
            })
            connection.execute(
                "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                "error_json=? WHERE run_id=? AND node_id='polish_prose'",
                (utc_now(), utc_now(), error, run.run_id),
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                (error, utc_now(), run.run_id),
            )
            connection.commit()

        response = self.client.post(f"/api/runs/{run.run_id}/replay-failed-authoring-node")

        self.assertEqual(response.status_code, 200, response.text)
        with self.app.state.databases.state.connect() as connection:
            event = connection.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed'",
                (run.run_id,),
            ).fetchone()
        self.assertTrue(json.loads(event["payload_json"])["v31_polish_role_recovery"])

    def test_v31_final_reader_facing_failure_replays_targeted_section(self) -> None:
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="Authoring V31 final reader-facing replay",
                root_path=str(Path(self.temp.name) / "authoring-v31-final-replay"),
            )
            run = RunRepository(connection).create(
                workflow_name="authoring",
                workflow_version="31",
                entry_node="finalize_authoring",
                project_id=project_id,
                inputs={
                    "section_contracts": [
                        {
                            "section_id": "section-conclusion",
                            "article_role": "conclusion",
                        }
                    ],
                    "argument_nodes": [{"id": "e1"}],
                    "draft_sections": [
                        {
                            "section_id": "section-conclusion",
                            "article_role": "conclusion",
                            "text": "これらの比較項目だけからは定まらない。",
                        }
                    ],
                },
                resource_lane="ingestion_cpu",
            )
            error = json.dumps(
                {
                    "type": "NonRetryableFukamiruError",
                    "message": (
                        "V31 final manuscript contains reader-facing input scaffolding: "
                        "section-conclusion"
                    ),
                }
            )
            connection.execute(
                "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                "error_json=? WHERE run_id=? AND node_id='finalize_authoring'",
                (utc_now(), utc_now(), error, run.run_id),
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                (error, utc_now(), run.run_id),
            )
            connection.commit()

        response = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["node_id"], "repair_failed_sections")
        with self.app.state.databases.state.connect() as connection:
            row = connection.execute(
                "SELECT input_json FROM node_runs WHERE id=?",
                (response.json()["node_run_id"],),
            ).fetchone()
            replay_input = PayloadStore(connection).unpack(
                json.loads(row["input_json"])
            )
        self.assertEqual(
            replay_input["audit_findings"][0]["object_id"],
            "section-conclusion",
        )
        self.assertEqual(
            replay_input["authoring_recovery_lineage"]["source_node_id"],
            "finalize_authoring",
        )

    def test_v31_proofreading_locked_text_failure_replays_publish_node(self) -> None:
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="Authoring V31 proofreading lock replay",
                root_path=str(Path(self.temp.name) / "authoring-v31-proofreading-lock"),
            )
            inputs = {
                "proposed_html": "<article><p>問いA。結論B。</p></article>",
                "research_problem": {"research_question": "問いA。"},
                "conclusion_synthesis": {"final_answer": "結論B。"},
            }
            run = RunRepository(connection).create(
                workflow_name="authoring",
                workflow_version="31",
                entry_node="publish_proofreading_shadow",
                project_id=project_id,
                inputs=inputs,
                resource_lane="ingestion_cpu",
            )
            error = json.dumps(
                {
                    "type": "FukamiruError",
                    "message": (
                        "V31 proofreading changed locked research-question or "
                        "final-answer prose"
                    ),
                }
            )
            connection.execute(
                "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                "error_json=? WHERE run_id=? AND node_id='publish_proofreading_shadow'",
                (utc_now(), utc_now(), error, run.run_id),
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                (error, utc_now(), run.run_id),
            )
            connection.commit()

        response = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["node_id"], "publish_proofreading_shadow")
        with self.app.state.databases.state.connect() as connection:
            row = connection.execute(
                "SELECT status,input_json FROM node_runs WHERE id=?",
                (response.json()["node_run_id"],),
            ).fetchone()
            replay_input = PayloadStore(connection).unpack(
                json.loads(row["input_json"])
            )
        self.assertEqual(row["status"], "queued")
        self.assertEqual(
            replay_input["research_problem"]["research_question"], "問いA。"
        )

    def test_v31_research_problem_ratification_failure_replays_once(self) -> None:
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="Authoring V31 ratification replay",
                root_path=str(Path(self.temp.name) / "authoring-v31-ratification"),
            )
            inputs = {
                "research_question": {
                    "text": "資料内の差異はどう説明できるか",
                    "provisional_answer": "初期回答",
                },
                "propositions": [{"id": "p1", "text": "二例が異なる"}],
                "claims": [{"id": "c1", "text": "掲載位置が異なる"}],
                "evidence_realization_decisions": [],
                "authoring_atomic_evidence_contracts": [],
            }
            run = RunRepository(connection).create(
                workflow_name="authoring",
                workflow_version="31",
                entry_node="ratify_research_problem",
                project_id=project_id,
                inputs=inputs,
                resource_lane="codex_remote",
            )
            error = json.dumps(
                {
                    "type": "NonRetryableFukamiruError",
                    "message": (
                        "Research problem JSON handoff failed after one bounded retry: "
                        "Research problem changed the provisional answer; replan required"
                    ),
                }
            )
            connection.execute(
                "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                "error_json=? WHERE run_id=? AND node_id='ratify_research_problem'",
                (utc_now(), utc_now(), error, run.run_id),
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                (error, utc_now(), run.run_id),
            )
            connection.commit()

        response = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["node_id"], "ratify_research_problem")
        with self.app.state.databases.state.connect() as connection:
            row = connection.execute(
                "SELECT status,input_json FROM node_runs WHERE id=?",
                (response.json()["node_run_id"],),
            ).fetchone()
            replay_input = PayloadStore(connection).unpack(json.loads(row["input_json"]))
            event = connection.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed'",
                (run.run_id,),
            ).fetchone()
        self.assertEqual(row["status"], "queued")
        self.assertEqual(replay_input["research_question"], inputs["research_question"])
        self.assertEqual(
            json.loads(event["payload_json"])["policy"],
            "authoring-v31-evidence-informed-ratification-v1",
        )

        with self.app.state.databases.state.connect() as connection:
            connection.execute(
                "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                "error_json=? WHERE id=?",
                (utc_now(), utc_now(), error, response.json()["node_run_id"]),
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                (error, utc_now(), run.run_id),
            )
            connection.commit()
        repeated = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(repeated.status_code, 409)
        self.assertIn("already used its manual replay", repeated.json()["detail"])

    def test_v31_uncited_reference_section_failure_replays_validation(self) -> None:
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="Authoring V31 uncited reference replay",
                root_path=str(Path(self.temp.name) / "authoring-v31-uncited-references"),
            )
            inputs = {
                "authoring_workflow_policy": {"workflow_version": 31},
                "authoring_restart_lineage": {"policy": "test"},
                "reference_inventory": {"reference_section_count": 1},
                "evidence_footnote_render_report": {
                    "policy": "evidence-footnotes-v1",
                    "cited_reference_count": 0,
                    "terminal_bibliography_addition_count": 0,
                },
                "reference_render_report": {
                    "rule_set_id": "house-style",
                    "entry_count": 0,
                    "source_reference_count": 37,
                    "uncited_reference_review_count": 37,
                    "machine_section_rendered": False,
                },
            }
            run = RunRepository(connection).create(
                workflow_name="authoring",
                workflow_version="31",
                entry_node="validate_footnoted_structure",
                project_id=project_id,
                inputs=inputs,
                resource_lane="codex_remote",
            )
            error = json.dumps(
                {
                    "type": "CoherenceResolutionRequired",
                    "message": (
                        "rendered_structure.invalid: "
                        '[{"code": "terminal.reference_section_count_changed", '
                        '"message": "The final document changed the source bibliography '
                        'section count.", "details": {"expected": 1, "actual": 0}}]'
                    ),
                }
            )
            connection.execute(
                "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                "error_json=? WHERE run_id=? AND node_id='validate_footnoted_structure'",
                (utc_now(), utc_now(), error, run.run_id),
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                (error, utc_now(), run.run_id),
            )
            connection.commit()

        response = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["node_id"], "validate_footnoted_structure")

    def test_failed_initial_draft_contract_can_replay_saved_input(self) -> None:
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="Initial authoring draft replay",
                root_path=str(Path(self.temp.name) / "initial-draft-replay"),
            )
            run = RunRepository(connection).create(
                workflow_name="authoring",
                workflow_version="30",
                entry_node="draft_rewrite",
                project_id=project_id,
                inputs={
                    "project_id": project_id,
                    "plan_id": "saved-plan",
                    "section_contracts": [{"section_id": "s1"}],
                    "argument_nodes": [{"id": "e1", "node_type": "evidence"}],
                },
                resource_lane="codex_remote",
            )
            error = json.dumps(
                {
                    "type": "NonRetryableFukamiruError",
                    "message": (
                        "Draft section contract violation after one bounded retry: "
                        "section s1 scholarship evidence e1 does not name its author "
                        "or work in prose"
                    ),
                }
            )
            connection.execute(
                "UPDATE node_runs SET status='failed', started_at=?, "
                "completed_at=?, error_json=? WHERE run_id=? AND node_id='draft_rewrite'",
                (utc_now(), utc_now(), error, run.run_id),
            )
            connection.execute(
                "UPDATE runs SET status='failed', error_json=?, completed_at=? WHERE id=?",
                (error, utc_now(), run.run_id),
            )
            connection.commit()

        response = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["node_id"], "draft_rewrite")

        mismatch = json.dumps(
            {
                "type": "NonRetryableFukamiruError",
                "message": (
                    "Draft section contract violation after one bounded retry: "
                    "section s1 paragraphs use non-contract evidence: ['e1']; "
                    "section s1 treated non-contract evidence e1"
                ),
            }
        )
        with self.app.state.databases.state.connect() as connection:
            connection.execute(
                "UPDATE node_runs SET status='failed', started_at=?, "
                "completed_at=?, error_json=? WHERE run_id=? AND node_id='draft_rewrite'",
                (utc_now(), utc_now(), mismatch, run.run_id),
            )
            connection.execute(
                "UPDATE runs SET status='failed', error_json=?, completed_at=? WHERE id=?",
                (mismatch, utc_now(), run.run_id),
            )
            connection.commit()
        second = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["manual_replay_count"], 2)


    def test_failed_initial_candidate_reconciliation_can_be_replayed(self) -> None:
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="Authoring candidate reconciliation replay",
                root_path=str(Path(self.temp.name) / "candidate-reconciliation-replay"),
            )
            node_input = {
                "project_id": project_id,
                "plan_id": "derived-plan",
                "section_contracts": [{"section_id": "s001"}],
                "draft_sections": [{"section_id": "s001", "text": "本文。"}],
                "proposed_html": (
                    '<article id="DOC"><section id="S1">'
                    '<h2 id="H1">論点</h2><p id="P1">本文。</p>'
                    "</section></article>"
                ),
            }
            run = RunRepository(connection).create(
                workflow_name="authoring",
                workflow_version="30",
                entry_node="reconcile_candidate_context",
                project_id=project_id,
                inputs=node_input,
                resource_lane="ingestion_cpu",
            )
            error = json.dumps(
                {
                    "type": "FukamiruError",
                    "message": (
                        "Authoring candidate no longer matches the locked "
                        "section contracts"
                    ),
                }
            )
            connection.execute(
                "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                "error_json=? WHERE run_id=? AND node_id='reconcile_candidate_context'",
                (utc_now(), utc_now(), error, run.run_id),
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                (error, utc_now(), run.run_id),
            )
            connection.commit()

        response = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["node_id"], "reconcile_candidate_context")
        with self.app.state.databases.state.connect() as connection:
            event = connection.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed'",
                (run.run_id,),
            ).fetchone()
        payload = json.loads(event["payload_json"])
        self.assertTrue(payload["candidate_reconciliation_recovery"])
        self.assertFalse(payload["submission_reconciliation_recovery"])

    def test_initial_decomposition_annotation_failure_can_be_replayed_in_place(self) -> None:
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="Authoring memo annotation replay",
                root_path=str(Path(self.temp.name) / "authoring-memo-annotation-replay"),
            )
            node_input = {
                "project_id": project_id,
                "note_text": "- 主張。(理: 著者指定の理論)",
                "authoring_workflow_policy": {
                    "id": "fukamiru-authoring-v30",
                    "workflow_version": 30,
                },
            }
            run = RunRepository(connection).create(
                workflow_name="authoring",
                workflow_version="30",
                entry_node="decompose_memo",
                project_id=project_id,
                inputs=node_input,
                resource_lane="codex_remote",
            )
            error = json.dumps(
                {
                    "type": "NonRetryableFukamiruError",
                    "message": (
                        "Memo decomposition lost author-supplied source constraints: "
                        "line 1 (理)"
                    ),
                }
            )
            connection.execute(
                "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                "error_json=? WHERE run_id=? AND node_id='decompose_memo'",
                (utc_now(), utc_now(), error, run.run_id),
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                (error, utc_now(), run.run_id),
            )
            connection.commit()

        response = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["node_id"], "decompose_memo")
        with self.app.state.databases.state.connect() as connection:
            event = connection.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed'",
                (run.run_id,),
            ).fetchone()
        self.assertTrue(
            json.loads(event["payload_json"])[
                "memo_annotation_preservation_recovery"
            ]
        )

    def test_v31_merged_topic_claim_tree_failure_can_replay_in_place(self) -> None:
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="V31 claim tree replay",
                root_path=str(Path(self.temp.name) / "v31-claim-tree-replay"),
            )
            run = RunRepository(connection).create(
                workflow_name="authoring",
                workflow_version="31",
                entry_node="build_claim_tree",
                project_id=project_id,
                inputs={"project_id": project_id, "topic_coverage": []},
                resource_lane="codex_remote",
            )
            error = json.dumps(
                {
                    "type": "NonRetryableFukamiruError",
                    "message": (
                        "Claim tree contract violation after one bounded retry: "
                        "claim tree needs at least one distinct subclaim branch per "
                        "central or supporting topic: required=22, actual=21; "
                        "claim provenance omitted topic t01 propositions: ['p0032']; "
                        "topic t02 has no direct subclaim branch; "
                        "claim c0001 uses unknown proposition IDs: "
                        "['research_question_v31']"
                    ),
                }
            )
            connection.execute(
                "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                "error_json=? WHERE run_id=? AND node_id='build_claim_tree'",
                (utc_now(), utc_now(), error, run.run_id),
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                (error, utc_now(), run.run_id),
            )
            connection.commit()

        response = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["node_id"], "build_claim_tree")
        with self.app.state.databases.state.connect() as connection:
            row = connection.execute(
                "SELECT status FROM runs WHERE id=?", (run.run_id,)
            ).fetchone()
            event = connection.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed'",
                (run.run_id,),
            ).fetchone()
        self.assertEqual(row["status"], "queued")
        payload = json.loads(event["payload_json"])
        self.assertEqual(payload["workflow_version"], "31")
        self.assertTrue(payload["v31_claim_tree_branch_recovery"])

    def test_v31_rebuttal_link_failure_replays_from_argument_graph(self) -> None:
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="V31 rebuttal link replay",
                root_path=str(Path(self.temp.name) / "v31-rebuttal-link-replay"),
            )
            run = RunRepository(connection).create(
                workflow_name="authoring",
                workflow_version="31",
                entry_node="build_argument_graph",
                project_id=project_id,
                inputs={"project_id": project_id, "claims": [{"id": "cl0003"}]},
                resource_lane="codex_remote",
            )
            error = json.dumps({
                "type": "NonRetryableFukamiruError",
                "message": (
                    "Argument graph batch 1 contract violation: support nodes lack "
                    "an edge to their claim node: ['node:rebuttal:cl0003']"
                ),
            })
            connection.execute(
                "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                "error_json=? WHERE run_id=? AND node_id='build_argument_graph'",
                (utc_now(), utc_now(), error, run.run_id),
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                (error, utc_now(), run.run_id),
            )
            connection.commit()

        response = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["node_id"], "build_argument_graph")
        with self.app.state.databases.state.connect() as connection:
            event = connection.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed'",
                (run.run_id,),
            ).fetchone()
        payload = json.loads(event["payload_json"])
        self.assertTrue(payload["v31_graph_rebuttal_link_recovery"])
        self.assertEqual(payload["policy"], "authoring-v31-rebuttal-link-recovery-v1")

    def test_v31_provenance_failure_allows_one_bounded_second_replay(self) -> None:
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="V31 graph attribution replay",
                root_path=str(Path(self.temp.name) / "v31-graph-attribution-replay"),
            )
            run = RunRepository(connection).create(
                workflow_name="authoring",
                workflow_version="31",
                entry_node="build_argument_graph",
                project_id=project_id,
                inputs={"project_id": project_id, "claims": [{"id": "cl0021"}]},
                resource_lane="codex_remote",
            )
            node_run_id = connection.execute(
                "SELECT id FROM node_runs WHERE run_id=?", (run.run_id,)
            ).fetchone()["id"]
            error = json.dumps({
                "type": "NonRetryableFukamiruError",
                "message": (
                    "Argument graph contract violation after one bounded retry: "
                    "claim cl0021 has unsupported verified evidence nodes: "
                    "['b06-ev_cl0021_fraser']; "
                    "atomic proposition p0123 must link its attributed theory citation; "
                    "atomic proposition p0150 must remain visibly memo-attributed"
                ),
            })
            connection.execute(
                "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                "error_json=? WHERE id=?",
                (utc_now(), utc_now(), error, node_run_id),
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                (error, utc_now(), run.run_id),
            )
            connection.execute(
                "INSERT INTO event_log "
                "(run_id,event_type,object_type,object_id,payload_json,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (run.run_id, "run.failed_authoring_node_replayed", "node_run",
                 node_run_id, json.dumps({"policy": "authoring-v31-rebuttal-link-recovery-v1"}),
                 utc_now()),
            )
            connection.commit()

        response = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(response.status_code, 200, response.text)
        with self.app.state.databases.state.connect() as connection:
            row = connection.execute(
                "SELECT status FROM runs WHERE id=?", (run.run_id,)
            ).fetchone()
            event = connection.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed' "
                "ORDER BY event_id DESC LIMIT 1",
                (run.run_id,),
            ).fetchone()
        self.assertEqual(row["status"], "queued")
        payload = json.loads(event["payload_json"])
        self.assertTrue(payload["v31_graph_provenance_recovery"])
        self.assertEqual(
            payload["policy"], "authoring-v31-evidence-attribution-recovery-v1"
        )

    def test_v31_graph_invalid_reference_review_is_visible_and_requires_revision(self) -> None:
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="V31 graph invalid reference review",
                root_path=str(Path(self.temp.name) / "v31-graph-invalid-reference"),
            )
            run = RunRepository(connection).create(
                workflow_name="manuscript_generator", workflow_version="31",
                entry_node="build_argument_graph", project_id=project_id,
                inputs={"project_id": project_id}, resource_lane="codex_remote",
            )
            node_run_id = connection.execute(
                "SELECT id FROM node_runs WHERE run_id=? AND node_id='build_argument_graph'",
                (run.run_id,),
            ).fetchone()["id"]
            review_id = "graph-review-1"
            packet = {
                "review_id": review_id, "batch_number": 1,
                "problems": ["claim scope handoff: Claim scope has unknown evidence basis: c0001"],
                "invalid_scopes": [{
                    "claim_id": "c0001", "invalid_evidence_basis_ids": ["unknown-1"],
                    "available_evidence_nodes": [{"id": "evidence-1", "text": "根拠"}],
                }],
                "invalid_edges": [], "invalid_node_claims": [],
                "candidate_graph": {"nodes": [], "edges": [], "claim_scopes": []},
            }
            connection.execute(
                "UPDATE node_runs SET status='waiting_human', output_json=? WHERE id=?",
                (json.dumps({"argument_graph_human_review": packet}), node_run_id),
            )
            connection.execute(
                "UPDATE runs SET status='waiting_human' WHERE id=?", (run.run_id,)
            )
            connection.execute(
                "INSERT INTO human_reviews "
                "(id,run_id,node_run_id,object_type,object_id,question,"
                "options_json,status,created_at) "
                "VALUES (?,?,?,'authoring_graph_invalid_reference','batch-1',?,?, 'open',?)",
                (
                    review_id, run.run_id, node_run_id, "根拠IDを確認",
                    json.dumps(["edited", "rejected"]), utc_now(),
                ),
            )
            connection.commit()

        listed = self.client.get("/api/reviews")
        self.assertEqual(listed.status_code, 200, listed.text)
        review = next(item for item in listed.json() if item["id"] == review_id)
        self.assertEqual(
            review["graph_review"]["invalid_scopes"][0]["invalid_evidence_basis_ids"],
            ["unknown-1"],
        )
        approval = self.client.post(
            f"/api/reviews/{review_id}/resolve", json={"decision": "approve"}
        )
        self.assertEqual(approval.status_code, 400)
        revision = self.client.post(
            f"/api/reviews/{review_id}/resolve",
            json={"decision": "revise", "feedback": "unknown-1 は evidence-1 を指す"},
        )
        self.assertEqual(revision.status_code, 200, revision.text)
        self.assertEqual(revision.json()["status"], "queued")

    def test_v31_missing_draft_source_replay_enters_human_review(self) -> None:
        from fukamiru.authoring.evidence_contracts import build_fixed_assets

        fixed = build_fixed_assets({
            "evidence_context": [{"id": "source-1", "text": "原典"}],
        })
        context = {
            "section_contracts": [{"section_id": "body", "evidence_ids": ["e1"]}],
            "argument_nodes": [{
                "id": "e1", "node_type": "evidence", "text": "出典不明の文",
                "provenance": {"source": "rag", "source_ids": [],
                               "verification_status": "unverified"},
            }],
            "fixed_assets": fixed.model_dump(mode="json"),
        }
        error = {
            "type": "FukamiruError",
            "message": (
                "V31 contracted evidence has no locked source; "
                "repair the argument graph before drafting: e1"
            ),
        }
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="V31 draft source review replay",
                root_path=str(Path(self.temp.name) / "v31-draft-source-review"),
            )
            run = RunRepository(connection).create(
                workflow_name="manuscript_generator", workflow_version="31",
                entry_node="draft_rewrite", project_id=project_id,
                inputs=context, resource_lane="codex_remote",
            )
            connection.execute(
                "UPDATE node_runs SET status='failed',input_json=?,error_json=?,"
                "started_at=?,completed_at=? WHERE run_id=? AND node_id='draft_rewrite'",
                (json.dumps(context), json.dumps(error), utc_now(), utc_now(), run.run_id),
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                (json.dumps(error), utc_now(), run.run_id),
            )
            connection.commit()
        response = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["node_id"], "draft_rewrite")
        with self.app.state.databases.state.connect() as connection:
            event = connection.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed'",
                (run.run_id,),
            ).fetchone()
        payload = json.loads(event["payload_json"])
        self.assertTrue(payload["v31_missing_draft_source_review"])
        self.assertEqual(
            payload["policy"], "authoring-v31-unlocked-draft-source-human-review-v1"
        )
        review_id = "draft-source-review-test"
        with self.app.state.databases.state.connect() as connection:
            node = connection.execute(
                "SELECT id FROM node_runs WHERE run_id=? AND node_id='draft_rewrite'",
                (run.run_id,),
            ).fetchone()
            connection.execute(
                "UPDATE node_runs SET status='waiting_human', output_json=? WHERE id=?",
                (json.dumps({
                    "authoring_draft_source_review": {
                        "review_id": review_id,
                        "evidence_nodes": [{"id": "e1", "text": "出典不明の文"}],
                    }
                }), node["id"]),
            )
            connection.execute(
                "UPDATE runs SET status='waiting_human' WHERE id=?", (run.run_id,)
            )
            connection.execute(
                "INSERT INTO human_reviews "
                "(id,run_id,node_run_id,object_type,object_id,question,"
                "options_json,status,created_at) "
                "VALUES (?,?,?,'authoring_draft_source_unresolved','e1',?,?, 'open',?)",
                (review_id, run.run_id, node["id"], "出典を確認",
                 json.dumps(["edited", "rejected"]), utc_now()),
            )
            connection.commit()
        listed = self.client.get("/api/reviews")
        self.assertEqual(listed.status_code, 200, listed.text)
        review = next(item for item in listed.json() if item["id"] == review_id)
        self.assertEqual(review["draft_source_review"]["evidence_nodes"][0]["id"], "e1")
        continued = self.client.post(
            f"/api/reviews/{review_id}/resolve", json={"decision": "defer"}
        )
        self.assertEqual(continued.status_code, 200, continued.text)
        self.assertEqual(continued.json()["status"], "queued")
        with self.app.state.databases.state.connect() as connection:
            review_row = connection.execute(
                "SELECT status,response_json FROM human_reviews WHERE id=?",
                (review_id,),
            ).fetchone()
            node_row = connection.execute(
                "SELECT status,input_json FROM node_runs WHERE id=?", (node["id"],)
            ).fetchone()
        self.assertEqual(review_row["status"], "cancelled")
        self.assertEqual(json.loads(review_row["response_json"])["decision"], "defer")
        self.assertEqual(node_row["status"], "queued")
        self.assertNotIn(
            "authoring_draft_source_review", json.loads(node_row["input_json"])
        )

    def test_v31_graph_scope_basis_failure_can_replay_in_place(self) -> None:
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="V31 graph scope replay",
                root_path=str(Path(self.temp.name) / "v31-graph-scope-replay"),
            )
            run = RunRepository(connection).create(
                workflow_name="authoring",
                workflow_version="31",
                entry_node="build_argument_graph",
                project_id=project_id,
                inputs={"project_id": project_id, "claims": [{"id": "cl0003"}]},
                resource_lane="codex_remote",
            )
            error = json.dumps({
                "type": "NonRetryableFukamiruError",
                "message": (
                    "Argument graph batch 1 contract violation: claim scope handoff: "
                    "Claim scope has unknown evidence basis: cl0003"
                ),
            })
            connection.execute(
                "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                "error_json=? WHERE run_id=? AND node_id='build_argument_graph'",
                (utc_now(), utc_now(), error, run.run_id),
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                (error, utc_now(), run.run_id),
            )
            connection.commit()

        response = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["node_id"], "build_argument_graph")
        with self.app.state.databases.state.connect() as connection:
            event = connection.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed'",
                (run.run_id,),
            ).fetchone()
        self.assertTrue(json.loads(event["payload_json"])["v31_graph_scope_basis_recovery"])

        core_error = json.dumps({
            "type": "NonRetryableFukamiruError",
            "message": (
                "Argument graph contract violation after one bounded retry: "
                "core atomic proposition p0118 must be represented as "
                "qualified_inference; core atomic proposition p0160 must be "
                "represented as qualified_inference"
            ),
        })
        with self.app.state.databases.state.connect() as connection:
            connection.execute(
                "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                "error_json=? WHERE run_id=? AND node_id='build_argument_graph'",
                (utc_now(), utc_now(), core_error, run.run_id),
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                (core_error, utc_now(), run.run_id),
            )
            connection.commit()
        response = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(response.status_code, 200, response.text)
        with self.app.state.databases.state.connect() as connection:
            events = connection.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed' "
                "ORDER BY event_id",
                (run.run_id,),
            ).fetchall()
        self.assertEqual(len(events), 2)
        self.assertTrue(json.loads(events[1]["payload_json"])["v31_graph_core_atom_recovery"])

        attribution_error = json.dumps({
            "type": "NonRetryableFukamiruError",
            "message": (
                "Argument graph batch 1 contract violation: atomic proposition "
                "p0031 must link its attributed theory citation"
            ),
        })
        with self.app.state.databases.state.connect() as connection:
            connection.execute(
                "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                "error_json=? WHERE run_id=? AND node_id='build_argument_graph'",
                (utc_now(), utc_now(), attribution_error, run.run_id),
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                (attribution_error, utc_now(), run.run_id),
            )
            connection.commit()
        response = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(response.status_code, 200, response.text)
        with self.app.state.databases.state.connect() as connection:
            events = connection.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed' "
                "ORDER BY event_id",
                (run.run_id,),
            ).fetchall()
        self.assertEqual(len(events), 3)
        self.assertTrue(
            json.loads(events[2]["payload_json"])[
                "v31_graph_batch_attribution_recovery"
            ]
        )

        drift_error = json.dumps({
            "type": "NonRetryableFukamiruError",
            "message": (
                "Argument graph batch 1 contract violation: evidence node e0002a "
                "claims verified status without an accepted external evidence ID; "
                "core atomic proposition p0068 must be represented as "
                "qualified_inference"
            ),
        })
        with self.app.state.databases.state.connect() as connection:
            connection.execute(
                "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                "error_json=? WHERE run_id=? AND node_id='build_argument_graph'",
                (utc_now(), utc_now(), drift_error, run.run_id),
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                (drift_error, utc_now(), run.run_id),
            )
            connection.commit()
        response = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(response.status_code, 200, response.text)
        with self.app.state.databases.state.connect() as connection:
            events = connection.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed' "
                "ORDER BY event_id",
                (run.run_id,),
            ).fetchall()
        self.assertEqual(len(events), 4)
        self.assertTrue(
            json.loads(events[3]["payload_json"])["v31_graph_repair_drift_recovery"]
        )

    def test_v31_scope_basis_third_replay_after_rebuttal_and_provenance(self) -> None:
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="V31 graph scope follow-up replay",
                root_path=str(Path(self.temp.name) / "v31-graph-scope-follow-up-replay"),
            )
            run = RunRepository(connection).create(
                workflow_name="authoring",
                workflow_version="31",
                entry_node="build_argument_graph",
                project_id=project_id,
                inputs={"project_id": project_id, "claims": [{"id": "cl0029"}]},
                resource_lane="codex_remote",
            )
            node_run_id = connection.execute(
                "SELECT id FROM node_runs WHERE run_id=? AND node_id='build_argument_graph'",
                (run.run_id,),
            ).fetchone()["id"]
            error = json.dumps({
                "type": "NonRetryableFukamiruError",
                "message": (
                    "Argument graph batch 8 contract violation: claim scope handoff: "
                    "Claim scope has unknown evidence basis: cl0029"
                ),
            })
            connection.execute(
                "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                "error_json=? WHERE id=?",
                (utc_now(), utc_now(), error, node_run_id),
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                (error, utc_now(), run.run_id),
            )
            for policy, recovery_flag in (
                (
                    "authoring-v31-rebuttal-link-recovery-v1",
                    "v31_graph_rebuttal_link_recovery",
                ),
                (
                    "authoring-v31-evidence-attribution-recovery-v1",
                    "v31_graph_provenance_recovery",
                ),
            ):
                connection.execute(
                    "INSERT INTO event_log "
                    "(run_id,event_type,object_type,object_id,payload_json,created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        run.run_id,
                        "run.failed_authoring_node_replayed",
                        "node_run",
                        node_run_id,
                        json.dumps({"policy": policy, recovery_flag: True}),
                        utc_now(),
                    ),
                )
            connection.commit()

        response = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )

        self.assertEqual(response.status_code, 200, response.text)
        with self.app.state.databases.state.connect() as connection:
            event = connection.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed' "
                "ORDER BY event_id DESC LIMIT 1",
                (run.run_id,),
            ).fetchone()
        payload = json.loads(event["payload_json"])
        self.assertEqual(
            payload["policy"], "authoring-v31-graph-scope-basis-recovery-v1"
        )
        self.assertTrue(payload["v31_graph_scope_basis_followup_recovery"])

    def test_v31_defense_reference_failure_can_replay_in_place(self) -> None:
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="V31 defense reference replay",
                root_path=str(Path(self.temp.name) / "v31-defense-replay"),
            )
            run = RunRepository(connection).create(
                workflow_name="authoring",
                workflow_version="31",
                entry_node="select_defense_obligations",
                project_id=project_id,
                inputs={"project_id": project_id, "argument_nodes": []},
                resource_lane="codex_remote",
            )
            error = json.dumps({
                "type": "NonRetryableFukamiruError",
                "message": (
                    "Defense obligation JSON handoff failed after one bounded retry: "
                    "Defense cites counterevidence for another claim"
                ),
            })
            connection.execute(
                "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                "error_json=? WHERE run_id=? AND node_id='select_defense_obligations'",
                (utc_now(), utc_now(), error, run.run_id),
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                (error, utc_now(), run.run_id),
            )
            connection.commit()

        response = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["node_id"], "select_defense_obligations")
        with self.app.state.databases.state.connect() as connection:
            event = connection.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed'",
                (run.run_id,),
            ).fetchone()
        self.assertTrue(json.loads(event["payload_json"])["v31_defense_reference_recovery"])

    def test_v31_discourse_synthesis_validation_failure_replays_once(self) -> None:
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="V31 discourse synthesis replay",
                root_path=str(Path(self.temp.name) / "v31-discourse-synthesis-replay"),
            )
            run = RunRepository(connection).create(
                workflow_name="authoring",
                workflow_version="31",
                entry_node="build_discourse_tree",
                project_id=project_id,
                inputs={
                    "project_id": project_id,
                    "research_problem": {"research_question": "RQ"},
                    "claims": [
                        {"id": "main", "level": "main_claim", "parent_id": None},
                        {"id": "sub1", "level": "subclaim", "parent_id": "main"},
                        {"id": "sub2", "level": "subclaim", "parent_id": "main"},
                    ],
                    "argument_nodes": [{"id": "n1"}],
                    "claim_scopes": [{"claim_id": "sub1"}, {"claim_id": "sub2"}],
                    "defense_obligations": [{"id": "d1"}],
                },
                resource_lane="codex_remote",
            )
            error = json.dumps(
                {
                    "type": "NonRetryableFukamiruError",
                    "message": (
                        "Discourse tree contract violation after one bounded retry: "
                        "V31 body discourse section s-syn1 recombined distinct "
                        "top-level topic branches: ['c_t002_01', 'c_t003', 'c_t006']. "
                        "Keep one topic branch per body phase; cross-branch synthesis "
                        "belongs in the conclusion or an explicitly separate synthesis phase.; "
                        "V31 body discourse section s-syn2 recombined distinct "
                        "top-level topic branches: ['c_t007', 'c_t008']. Keep one topic "
                        "branch per body phase; cross-branch synthesis belongs in the "
                        "conclusion or an explicitly separate synthesis phase."
                    ),
                }
            )
            connection.execute(
                "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                "error_json=? WHERE run_id=? AND node_id='build_discourse_tree'",
                (utc_now(), utc_now(), error, run.run_id),
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                (error, utc_now(), run.run_id),
            )
            connection.commit()

        response = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["node_id"], "build_discourse_tree")
        with self.app.state.databases.state.connect() as connection:
            event = connection.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed'",
                (run.run_id,),
            ).fetchone()
        self.assertTrue(
            json.loads(event["payload_json"])["v31_discourse_synthesis_recovery"]
        )

        second_error = json.dumps(
            {
                "type": "NonRetryableFukamiruError",
                "message": (
                    "Discourse tree contract violation after one bounded retry: "
                    "V31 body discourse section s018 recombined distinct "
                    "top-level topic branches: ['sub1', 'sub2']. Keep one topic "
                    "branch per body phase; cross-branch synthesis belongs in the "
                    "conclusion or an explicitly separate synthesis phase."
                ),
            }
        )
        with self.app.state.databases.state.connect() as connection:
            connection.execute(
                "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                "error_json=? WHERE run_id=? AND node_id='build_discourse_tree'",
                (utc_now(), utc_now(), second_error, run.run_id),
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                (second_error, utc_now(), run.run_id),
            )
            connection.execute(
                "INSERT INTO llm_result_checkpoints "
                "(id,run_id,node_run_id,node_id,stage,canonical_stage,operation_name,"
                "request_fingerprint_sha256,full_prompt_sha256,cache_family_sha256,"
                "result_json,validation_json,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "checkpoint-" + run.run_id,
                    run.run_id,
                    response.json()["node_run_id"],
                    "build_discourse_tree",
                    "build_discourse_tree",
                    "discourse_tree",
                    "authoring_build_discourse_tree",
                    "a" * 64,
                    "b" * 64,
                    "c" * 64,
                    json.dumps(
                        {
                            "nodes": [
                                {
                                    "article_role": "body",
                                    "relation": "contrast",
                                    "discourse_purpose": (
                                        "独立した横断的な検討として複数の主題を比較する"
                                    ),
                                    "claim_ids": ["sub1", "sub2"],
                                }
                            ]
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps({"valid": True}),
                    "accepted",
                    utc_now(),
                    utc_now(),
                ),
            )
            connection.commit()

        second = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(second.status_code, 200, second.text)
        with self.app.state.databases.state.connect() as connection:
            events = connection.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed' ORDER BY event_id",
                (run.run_id,),
            ).fetchall()
        self.assertEqual(
            len(events),
            2,
        )
        self.assertTrue(
            json.loads(events[1]["payload_json"])[
                "v31_discourse_synthesis_checkpoint_recovery"
            ]
        )

        with self.app.state.databases.state.connect() as connection:
            connection.execute(
                "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                "error_json=? WHERE run_id=? AND node_id='build_discourse_tree'",
                (utc_now(), utc_now(), second_error, run.run_id),
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                (second_error, utc_now(), run.run_id),
            )
            connection.commit()

        third = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(third.status_code, 409, third.text)
        self.assertIn("already used its manual replay", third.text)

    def test_v31_citation_source_claim_mapping_failure_replays_only_from_matching_contract(
        self,
    ) -> None:
        source_id = "annotation-citation:" + "a" * 24
        leading_source_id = "annotation-citation:" + "b" * 24
        error = json.dumps(
            {
                "type": "NonRetryableFukamiruError",
                "message": (
                    "Authoring citation JSON handoff lost required source-to-claim "
                    "mapping for section sec-magazine: "
                    f"['{source_id}']"
                ),
            }
        )

        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="V31 citation source-to-claim replay",
                root_path=str(Path(self.temp.name) / "v31-citation-bridge-replay"),
            )

            def create_failed_run(
                *, citation_proposition_id: str, mapped_source: bool = False
            ):
                citation = {
                    "id": source_id,
                    "allowed_use": "attributed_scholarship_citation",
                    "reference_id": "reference:r1",
                    "claim_ids": ["cl0006"] if mapped_source else [],
                    "annotation_proposition_ids": [citation_proposition_id],
                }
                leading_citation = {
                    "id": leading_source_id,
                    "allowed_use": "attributed_scholarship_citation",
                    "reference_id": "reference:jameson",
                    "claim_ids": [],
                    "annotation_proposition_ids": [],
                }
                run = RunRepository(connection).create(
                    workflow_name="authoring",
                    workflow_version="31",
                    entry_node="draft_rewrite",
                    project_id=project_id,
                    inputs={
                        "project_id": project_id,
                        "plan_id": "plan-1",
                        "research_problem": {"research_question": "RQ"},
                        "section_contracts": [
                            {
                                "section_id": "sec-introduction",
                                "article_role": "introduction",
                                "claim_ids": [],
                                "development_expectations": {
                                    "required_argument_moves": ["research_question"]
                                },
                            },
                            {
                                "section_id": "sec-introduction",
                                "article_role": "introduction",
                                "claim_ids": [],
                                "development_expectations": {
                                    "required_argument_moves": ["contribution"]
                                },
                            },
                            {
                                "section_id": "sec-magazine",
                                "article_role": "body",
                                "claim_ids": ["cl0006"],
                                "development_expectations": {
                                    "required_argument_moves": ["planned_move"]
                                },
                            },
                            {
                                "section_id": "sec-magazine",
                                "article_role": "body",
                                "claim_ids": ["cl0006"],
                                "development_expectations": {
                                    "required_argument_moves": ["paragraph_move"]
                                },
                            },
                            {
                                "section_id": "sec-conclusion",
                                "article_role": "conclusion",
                                "claim_ids": ["cl0006"],
                            },
                        ],
                        "argument_nodes": [
                            {
                                "id": "claim-node-cl0006",
                                "node_type": "claim",
                                "claim_id": "cl0006",
                            },
                            *(
                                [
                                    {
                                        "id": "evidence-node-source-r1",
                                        "node_type": "evidence",
                                        "claim_id": "cl0006",
                                        "source_span_id": leading_source_id,
                                        "provenance": {"source_ids": [source_id]},
                                    }
                                ]
                                if mapped_source
                                else []
                            ),
                        ],
                        "authoring_atomic_evidence_contracts": [
                            {
                                "realization": "attribute_theory_with_citation",
                                "proposition_id": "p0124",
                                "claim_ids": ["cl0006"],
                                "citation_evidence_ids": [source_id],
                            }
                        ],
                        "annotation_citation_evidence": (
                            [leading_citation, citation] if mapped_source else [citation]
                        ),
                        "evidence_context": (
                            [leading_citation, citation] if mapped_source else [citation]
                        ),
                        "reference_entries": [
                            {"reference_id": "reference:r1"},
                            *(
                                [{"reference_id": "reference:jameson"}]
                                if mapped_source
                                else []
                            ),
                        ],
                    },
                    resource_lane="codex_remote",
                )
                connection.execute(
                    "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                    "error_json=? WHERE run_id=? AND node_id='draft_rewrite'",
                    (utc_now(), utc_now(), error, run.run_id),
                )
                connection.execute(
                    "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                    (error, utc_now(), run.run_id),
                )
                return run

            valid_run = create_failed_run(citation_proposition_id="p0124")
            mismatched_run = create_failed_run(
                citation_proposition_id="p-unrelated"
            )
            post_correction_run = create_failed_run(
                citation_proposition_id="p0124", mapped_source=True
            )
            post_correction_node = connection.execute(
                "SELECT id FROM node_runs WHERE run_id=? AND node_id='draft_rewrite'",
                (post_correction_run.run_id,),
            ).fetchone()
            correction_event = {
                "policy": "authoring-v31-single-self-citation-record-correction-v1",
                "node_id": "draft_rewrite",
                "citation_evidence_id": source_id,
                "source_record_id": "source-record-1",
            }
            connection.execute(
                "INSERT INTO event_log(run_id,event_type,object_type,object_id,"
                "payload_json,created_at) VALUES (?, 'run.failed_authoring_node_replayed', "
                "'node_run', ?, ?, ?)",
                (
                    post_correction_run.run_id,
                    post_correction_node["id"],
                    json.dumps(correction_event),
                    utc_now(),
                ),
            )
            unrelated_history_run = create_failed_run(
                citation_proposition_id="p0124", mapped_source=True
            )
            unrelated_history_node = connection.execute(
                "SELECT id FROM node_runs WHERE run_id=? AND node_id='draft_rewrite'",
                (unrelated_history_run.run_id,),
            ).fetchone()
            connection.execute(
                "INSERT INTO event_log(run_id,event_type,object_type,object_id,"
                "payload_json,created_at) VALUES (?, 'run.failed_authoring_node_replayed', "
                "'node_run', ?, ?, ?)",
                (
                    unrelated_history_run.run_id,
                    unrelated_history_node["id"],
                    json.dumps({**correction_event, "policy": "unrelated-replay-v1"}),
                    utc_now(),
                ),
            )
            connection.commit()

        valid_response = self.client.post(
            f"/api/runs/{valid_run.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(valid_response.status_code, 200, valid_response.text)
        self.assertEqual(valid_response.json()["node_id"], "draft_rewrite")
        with self.app.state.databases.state.connect() as connection:
            event = connection.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed'",
                (valid_run.run_id,),
            ).fetchone()
        payload = json.loads(event["payload_json"])
        self.assertTrue(payload["v31_citation_source_claim_mapping_recovery"])
        self.assertEqual(
            payload["policy"],
            "authoring-v31-citation-source-claim-bridge-recovery-v1",
        )

        post_correction_response = self.client.post(
            f"/api/runs/{post_correction_run.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(
            post_correction_response.status_code, 200, post_correction_response.text
        )
        with self.app.state.databases.state.connect() as connection:
            post_correction_event = connection.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed' ORDER BY rowid DESC LIMIT 1",
                (post_correction_run.run_id,),
            ).fetchone()
        post_correction_payload = json.loads(post_correction_event["payload_json"])
        self.assertTrue(
            post_correction_payload[
                "v31_citation_source_claim_mapping_post_fix_replay"
            ]
        )
        self.assertEqual(
            post_correction_payload["policy"],
            "authoring-v31-citation-source-claim-mapping-post-fix-replay-v1",
        )

        unrelated_history_response = self.client.post(
            f"/api/runs/{unrelated_history_run.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(unrelated_history_response.status_code, 409)
        self.assertIn("no replayable input", unrelated_history_response.text)

        def fail_replayed_node_again(
            run_id: str, failure_json: str = error
        ) -> None:
            with self.app.state.databases.state.connect() as connection:
                connection.execute(
                    "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                    "error_json=? WHERE run_id=? AND node_id='draft_rewrite'",
                    (utc_now(), utc_now(), failure_json, run_id),
                )
                connection.execute(
                    "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                    (failure_json, utc_now(), run_id),
                )
                connection.commit()

        # One additional replay is allowed only when the first replay used this
        # exact contract-proven bridge policy and failed with the same signature.
        fail_replayed_node_again(valid_run.run_id)
        followup_response = self.client.post(
            f"/api/runs/{valid_run.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(followup_response.status_code, 200, followup_response.text)
        self.assertEqual(followup_response.json()["node_id"], "draft_rewrite")
        with self.app.state.databases.state.connect() as connection:
            events = connection.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed' ORDER BY rowid",
                (valid_run.run_id,),
            ).fetchall()
        followup_payload = json.loads(events[-1]["payload_json"])
        self.assertTrue(
            followup_payload["v31_citation_source_claim_mapping_followup_recovery"]
        )
        self.assertEqual(
            followup_payload["policy"],
            "authoring-v31-citation-source-claim-bridge-input-rebuild-v1",
        )

        fail_replayed_node_again(valid_run.run_id)
        third_response = self.client.post(
            f"/api/runs/{valid_run.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(third_response.status_code, 409, third_response.text)
        self.assertIn("already used its manual replay", third_response.text)

        duplicate_contract_error = json.dumps(
            {
                "type": "FukamiruError",
                "message": "V31 drafting requires Introduction, Body and Conclusion contracts",
            }
        )
        fail_replayed_node_again(
            valid_run.run_id, failure_json=duplicate_contract_error
        )
        contract_recovery = self.client.post(
            f"/api/runs/{valid_run.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(contract_recovery.status_code, 200, contract_recovery.text)
        self.assertEqual(contract_recovery.json()["node_id"], "draft_rewrite")
        with self.app.state.databases.state.connect() as connection:
            event = connection.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed' ORDER BY rowid DESC LIMIT 1",
                (valid_run.run_id,),
            ).fetchone()
        contract_payload = json.loads(event["payload_json"])
        self.assertTrue(contract_payload["v31_section_contract_identity_recovery"])
        self.assertEqual(
            contract_payload["policy"],
            "authoring-v31-section-contract-identity-merge-v1",
        )

        mismatched_response = self.client.post(
            f"/api/runs/{mismatched_run.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(mismatched_response.status_code, 409)
        self.assertIn("no replayable input", mismatched_response.text)
        with self.app.state.databases.state.connect() as connection:
            event_count = connection.execute(
                "SELECT COUNT(*) FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed'",
                (mismatched_run.run_id,),
            ).fetchone()[0]
        self.assertEqual(event_count, 0)

    def test_v31_section_role_dependency_failure_can_replay_in_place(self) -> None:
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="V31 section role replay",
                root_path=str(Path(self.temp.name) / "v31-section-role-replay"),
            )
            run = RunRepository(connection).create(
                workflow_name="authoring",
                workflow_version="31",
                entry_node="lock_section_contracts",
                project_id=project_id,
                inputs={"project_id": project_id, "discourse_nodes": []},
                resource_lane="codex_remote",
            )
            error = json.dumps({
                "type": "NonRetryableFukamiruError",
                "message": (
                    "Section contract violation after one bounded retry: "
                    "role contracts: Body section depends on a later or unknown section"
                ),
            })
            connection.execute(
                "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                "error_json=? WHERE run_id=? AND node_id='lock_section_contracts'",
                (utc_now(), utc_now(), error, run.run_id),
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                (error, utc_now(), run.run_id),
            )
            connection.commit()

        response = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["node_id"], "lock_section_contracts")
        with self.app.state.databases.state.connect() as connection:
            event = connection.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed'",
                (run.run_id,),
            ).fetchone()
        self.assertTrue(
            json.loads(event["payload_json"])["v31_section_role_dependency_recovery"]
        )

    def test_v31_global_defense_assignment_failure_can_replay_in_place(self) -> None:
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="V31 global defense replay",
                root_path=str(Path(self.temp.name) / "v31-global-defense-replay"),
            )
            run = RunRepository(connection).create(
                workflow_name="authoring", workflow_version="31",
                entry_node="lock_section_contracts", project_id=project_id,
                inputs={"project_id": project_id, "discourse_nodes": []},
                resource_lane="codex_remote",
            )
            error = json.dumps({
                "type": "NonRetryableFukamiruError",
                "message": (
                    "Section contract violation after one bounded retry: "
                    "role contracts: Body defense is assigned outside its target claim"
                ),
            })
            connection.execute(
                "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                "error_json=? WHERE run_id=? AND node_id='lock_section_contracts'",
                (utc_now(), utc_now(), error, run.run_id),
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                (error, utc_now(), run.run_id),
            )
            connection.commit()

        response = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(response.status_code, 200, response.text)
        with self.app.state.databases.state.connect() as connection:
            event = connection.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed'",
                (run.run_id,),
            ).fetchone()
        self.assertTrue(
            json.loads(event["payload_json"])["v31_global_defense_assignment_recovery"]
        )
        with self.app.state.databases.state.connect() as connection:
            connection.execute(
                "UPDATE node_runs SET status='failed',completed_at=?,error_json=? "
                "WHERE run_id=? AND node_id='lock_section_contracts'",
                (utc_now(), error, run.run_id),
            )
            connection.execute(
                "UPDATE runs SET status='failed',completed_at=?,error_json=? WHERE id=?",
                (utc_now(), error, run.run_id),
            )
            connection.commit()
        second = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["manual_replay_count"], 2)

    def test_v31_draft_handoff_failure_can_replay_in_place_once(self) -> None:
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="V31 draft handoff replay",
                root_path=str(Path(self.temp.name) / "v31-draft-handoff-replay"),
            )
            run = RunRepository(connection).create(
                workflow_name="authoring",
                workflow_version="31",
                entry_node="draft_rewrite",
                project_id=project_id,
                inputs={
                    "project_id": project_id,
                    "plan_id": "plan-1",
                    "section_contracts": [{"section_id": "s1"}],
                    "argument_nodes": [{"id": "e1"}],
                    "research_problem": {"research_question": "なぜか"},
                },
                resource_lane="codex_remote",
            )
            error = json.dumps({
                "type": "NonRetryableFukamiruError",
                "message": (
                    "V31 role draft JSON handoff violation: "
                    "section s1 scholarship evidence e1 lacks an exact visible "
                    "attribution sentence in its evidence paragraph"
                ),
            })
            connection.execute(
                "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                "error_json=? WHERE run_id=? AND node_id='draft_rewrite'",
                (utc_now(), utc_now(), error, run.run_id),
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                (error, utc_now(), run.run_id),
            )
            connection.commit()

        response = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(response.status_code, 200, response.text)
        with self.app.state.databases.state.connect() as connection:
            event = connection.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed'",
                (run.run_id,),
            ).fetchone()
        self.assertTrue(json.loads(event["payload_json"])["v31_draft_handoff_recovery"])

    def test_v31_sentence_anchor_failure_gets_one_bounded_replay_after_handoff_retry(
        self,
    ) -> None:
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="V31 sentence anchor replay",
                root_path=str(Path(self.temp.name) / "v31-sentence-anchor-replay"),
            )
            run = RunRepository(connection).create(
                workflow_name="authoring",
                workflow_version="31",
                entry_node="draft_rewrite",
                project_id=project_id,
                inputs={
                    "project_id": project_id,
                    "plan_id": "plan-1",
                    "section_contracts": [{"section_id": "s012"}],
                    "argument_nodes": [{"id": "e1"}],
                    "research_problem": {"research_question": "なぜか"},
                },
                resource_lane="codex_remote",
            )
            error = json.dumps({
                "type": "NonRetryableFukamiruError",
                "message": (
                    "V31 role draft JSON handoff violation: section s012 scholarship "
                    "evidence e1 has no unique sentence-level footnote anchor"
                ),
            })

            def fail_again(connection) -> None:
                connection.execute(
                    "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                    "error_json=? WHERE run_id=? AND node_id='draft_rewrite'",
                    (utc_now(), utc_now(), error, run.run_id),
                )
                connection.execute(
                    "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                    (error, utc_now(), run.run_id),
                )
                connection.commit()

            fail_again(connection)

        first = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["manual_replay_count"], 1)

        with self.app.state.databases.state.connect() as connection:
            connection.execute(
                "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                "error_json=? WHERE run_id=? AND node_id='draft_rewrite'",
                (utc_now(), utc_now(), error, run.run_id),
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                (error, utc_now(), run.run_id),
            )
            connection.commit()

        second = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["manual_replay_count"], 2)
        with self.app.state.databases.state.connect() as connection:
            events = connection.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed' ORDER BY rowid",
                (run.run_id,),
            ).fetchall()
        self.assertTrue(json.loads(events[1]["payload_json"])["v31_footnote_anchor_recovery"])

        with self.app.state.databases.state.connect() as connection:
            connection.execute(
                "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                "error_json=? WHERE run_id=? AND node_id='draft_rewrite'",
                (utc_now(), utc_now(), error, run.run_id),
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                (error, utc_now(), run.run_id),
            )
            connection.commit()
        third = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(third.status_code, 409, third.text)
        self.assertIn("already used its manual replay", third.text)

    def test_v31_citation_anchor_surface_failure_replays_uses_a_consistent_correction_ledger(
        self,
    ) -> None:
        from fukamiru.storage.events import EventRepository

        policies = [
            ("authoring-v31-section-contract-identity-merge-v1",
             "v31_section_contract_identity_recovery"),
            ("authoring-v31-citation-source-claim-bridge-recovery-v1",
             "v31_citation_source_claim_mapping_recovery"),
        ]

        def create_failed_run(
            name: str,
            message: str,
            *,
            corrupt_history: bool = False,
            include_surface_replay: bool = False,
        ):
            with self.app.state.databases.state.connect() as connection:
                project_id = RevisionService(connection).register_project(
                    name=name,
                    root_path=str(Path(self.temp.name) / name.replace(" ", "-")),
                )
                run = RunRepository(connection).create(
                    workflow_name="authoring",
                    workflow_version="31",
                    entry_node="draft_rewrite",
                    project_id=project_id,
                    inputs={
                        "project_id": project_id,
                        "plan_id": "plan-1",
                        "section_contracts": [{"section_id": "s012"}],
                        "argument_nodes": [{"id": "e1"}],
                        "research_problem": {"research_question": "なぜか"},
                    },
                    resource_lane="codex_remote",
                )
                node = connection.execute(
                    "SELECT id FROM node_runs WHERE run_id=? AND node_id='draft_rewrite'",
                    (run.run_id,),
                ).fetchone()
                error = json.dumps({
                    "type": "NonRetryableFukamiruError",
                    "message": message,
                })
                connection.execute(
                    "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                    "error_json=? WHERE id=?",
                    (utc_now(), utc_now(), error, str(node["id"])),
                )
                connection.execute(
                    "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                    (error, utc_now(), run.run_id),
                )
                connection.commit()
                events = EventRepository(connection)
                for index, (policy, flag) in enumerate(policies):
                    if corrupt_history and index == 1:
                        policy = "authoring-terminal-codex-replay-v1"
                    events.append(
                        run.run_id,
                        "run.failed_authoring_node_replayed",
                        object_type="node_run",
                        object_id=str(node["id"]),
                        payload={"policy": policy, flag: True},
                    )
                if include_surface_replay:
                    events.append(
                        run.run_id,
                        "run.failed_authoring_node_replayed",
                        object_type="node_run",
                        object_id=str(node["id"]),
                        payload={
                            "policy": "authoring-v31-citation-anchor-surface-recovery-v1",
                            "v31_citation_anchor_surface_recovery": True,
                        },
                    )
            return run

        prefix = "V31 role draft JSON handoff violation: "
        repairable = create_failed_run(
            "V31 citation anchor surface replay",
            prefix
            + "section sec-a scholarship evidence e1 has no unique sentence-level "
            "footnote anchor; section sec-b scholarship evidence e2 lacks an exact "
            "visible attribution sentence in its evidence paragraph",
        )
        accepted = self.client.post(
            f"/api/runs/{repairable.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        with self.app.state.databases.state.connect() as connection:
            replay = connection.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed' "
                "ORDER BY rowid DESC LIMIT 1",
                (repairable.run_id,),
            ).fetchone()
        replay_payload = json.loads(replay["payload_json"])
        self.assertTrue(replay_payload["v31_citation_anchor_surface_recovery"])
        self.assertEqual(
            replay_payload["policy"],
            "authoring-v31-citation-anchor-surface-recovery-v1",
        )

        support_hint_message = prefix + (
            "section sec-magazine evidence b01-ev-cl0004-01 lacks an exact "
            "support_sentence in a paragraph that lists the evidence ID; "
            "section sec-conclusion evidence b01-ev-cl0001-01 lacks an exact "
            "support_sentence in a paragraph that lists the evidence ID"
        )
        support_hint = create_failed_run(
            "V31 support sentence hint replay",
            support_hint_message,
            include_surface_replay=True,
        )
        accepted_hint = self.client.post(
            f"/api/runs/{support_hint.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(accepted_hint.status_code, 200, accepted_hint.text)
        self.assertEqual(accepted_hint.json()["manual_replay_count"], 4)
        with self.app.state.databases.state.connect() as connection:
            replay = connection.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed' "
                "ORDER BY rowid DESC LIMIT 1",
                (support_hint.run_id,),
            ).fetchone()
        replay_payload = json.loads(replay["payload_json"])
        self.assertTrue(replay_payload["v31_support_sentence_hint_recovery"])
        self.assertEqual(
            replay_payload["policy"],
            "authoring-v31-support-sentence-hint-recovery-v1",
        )

        mixed_support_failure = create_failed_run(
            "V31 support hint plus unrelated failure",
            support_hint_message + "; section sec-conclusion has invalid output schema",
            include_surface_replay=True,
        )
        rejected_mixed = self.client.post(
            f"/api/runs/{mixed_support_failure.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(rejected_mixed.status_code, 409, rejected_mixed.text)

        wrong_support_history = create_failed_run(
            "V31 support hint wrong replay history",
            support_hint_message,
            corrupt_history=True,
            include_surface_replay=True,
        )
        rejected_history = self.client.post(
            f"/api/runs/{wrong_support_history.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(rejected_history.status_code, 409, rejected_history.text)

        unrelated = create_failed_run(
            "V31 citation anchor unrelated failure",
            prefix
            + "section sec-a scholarship evidence e1 has no unique sentence-level "
            "footnote anchor; section sec-b has invalid output schema",
        )
        rejected = self.client.post(
            f"/api/runs/{unrelated.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(rejected.status_code, 409, rejected.text)

        wrong_history = create_failed_run(
            "V31 citation anchor wrong history",
            prefix
            + "section sec-a scholarship evidence e1 has no unique sentence-level "
            "footnote anchor",
            corrupt_history=True,
        )
        rejected_history = self.client.post(
            f"/api/runs/{wrong_history.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(rejected_history.status_code, 409, rejected_history.text)

    def test_scholarship_conclusion_anchor_requires_verified_source_and_ledger(
        self,
    ) -> None:
        from fukamiru.storage.events import EventRepository

        evidence_id = "fixture-evidence-ab9e68b275b9b21f"
        citation_id = "annotation-citation:fixture-79931a2c5ee2681e"
        error_message = (
            "V31 role draft JSON handoff violation: section sec-conclusion "
            "scholarship evidence fixture-evidence-ab9e68b275b9b21f has no unique "
            "sentence-level footnote anchor"
        )
        history = [
            ("authoring-v31-section-contract-identity-merge-v1",
             "v31_section_contract_identity_recovery"),
            ("authoring-v31-citation-source-claim-bridge-recovery-v1",
             "v31_citation_source_claim_mapping_recovery"),
        ]

        def create_failed_run(
            name: str,
            *,
            source_valid: bool = True,
            corrupt_history: bool = False,
            message: str = error_message,
        ):
            citation = {
                "id": citation_id,
                "title": "架空の分類研究",
                "authors": ["山田花子"],
                "allowed_use": "attributed_scholarship_citation",
                "verification_scope": "bibliographic_identity_only",
                "provenance": {
                    "bibliographic_identity_verified": True,
                    "substantive_support_verified": False,
                },
            }
            if not source_valid:
                citation["provenance"]["bibliographic_identity_verified"] = False
            with self.app.state.databases.state.connect() as connection:
                project_id = RevisionService(connection).register_project(
                    name=name,
                    root_path=str(Path(self.temp.name) / name.replace(" ", "-")),
                )
                run = RunRepository(connection).create(
                    workflow_name="authoring",
                    workflow_version="31",
                    entry_node="draft_rewrite",
                    project_id=project_id,
                    inputs={
                        "project_id": project_id,
                        "plan_id": "plan-1",
                        "research_problem": {"research_question": "なぜか"},
                        "section_contracts": [{
                            "section_id": "sec-conclusion",
                            "article_role": "conclusion",
                            "claim_ids": ["cl0032"],
                            "evidence_ids": [evidence_id],
                        }],
                        "argument_nodes": [{
                            "id": evidence_id,
                            "node_type": "evidence",
                            "claim_id": "cl0032",
                            "provenance": {"source_ids": [citation_id]},
                        }],
                        "annotation_citation_evidence": [citation],
                    },
                    resource_lane="codex_remote",
                )
                node = connection.execute(
                    "SELECT id FROM node_runs WHERE run_id=? AND node_id='draft_rewrite'",
                    (run.run_id,),
                ).fetchone()
                error = json.dumps({
                    "type": "NonRetryableFukamiruError",
                    "message": message,
                })
                connection.execute(
                    "UPDATE node_runs SET status='failed',started_at=?,completed_at=?"
                    ",error_json=? WHERE id=?",
                    (utc_now(), utc_now(), error, str(node["id"])),
                )
                connection.execute(
                    "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                    (error, utc_now(), run.run_id),
                )
                connection.commit()
                events = EventRepository(connection)
                for index, (policy, flag) in enumerate(history):
                    if corrupt_history and index == 1:
                        policy = "authoring-terminal-codex-replay-v1"
                    events.append(
                        run.run_id,
                        "run.failed_authoring_node_replayed",
                        object_type="node_run",
                        object_id=str(node["id"]),
                        payload={"policy": policy, flag: True},
                    )
            return run

        valid = create_failed_run("Conclusion scholarship conclusion anchor replay")
        accepted = self.client.post(
            f"/api/runs/{valid.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(accepted.json()["manual_replay_count"], 3)
        with self.app.state.databases.state.connect() as connection:
            replay = connection.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed' "
                "ORDER BY rowid DESC LIMIT 1",
                (valid.run_id,),
            ).fetchone()
        replay_payload = json.loads(replay["payload_json"])
        self.assertTrue(replay_payload["v31_conclusion_scholarship_conclusion_anchor_recovery"])
        self.assertEqual(
            replay_payload["policy"],
            "authoring-v31-conclusion_scholarship-conclusion-anchor-recovery-v1",
        )

        unverified_source = create_failed_run(
            "Conclusion scholarship conclusion anchor invalid source", source_valid=False
        )
        rejected_source = self.client.post(
            f"/api/runs/{unverified_source.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(rejected_source.status_code, 409, rejected_source.text)

        mixed_error = create_failed_run(
            "Conclusion scholarship conclusion anchor mixed failure",
            message=error_message + "; section sec-body has invalid output schema",
        )
        rejected_mixed = self.client.post(
            f"/api/runs/{mixed_error.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(rejected_mixed.status_code, 409, rejected_mixed.text)

        wrong_history = create_failed_run(
            "Conclusion scholarship conclusion anchor wrong history", corrupt_history=True
        )
        rejected_history = self.client.post(
            f"/api/runs/{wrong_history.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(rejected_history.status_code, 409, rejected_history.text)

    def test_v31_semantic_attribution_predicate_replay_requires_verified_source_and_ledger(
        self,
    ) -> None:
        from fukamiru.storage.events import EventRepository

        citation_id = "annotation-citation:fixture-68456429bdc75c8d"
        reference_id = "reference:fixture-88597fcc76404a35"
        error_evidence_id = "annotation-citation-node:" + hashlib.sha256(
            f"cl0006\0{citation_id}".encode()
        ).hexdigest()[:16]
        error_message = (
            "V31 role draft JSON handoff violation: section sec-magazine "
            f"scholarship evidence {error_evidence_id} "
            "does not identify its author or work in the unique "
            "sentence-level citation anchor"
        )
        history = [
            ("authoring-v31-section-contract-identity-merge-v1",
             {"v31_section_contract_identity_recovery": True}),
            ("authoring-v31-citation-source-claim-bridge-recovery-v1",
             {"v31_citation_source_claim_mapping_recovery": True}),
        ]

        def create_failed_run(
            name: str,
            *,
            source_valid: bool = True,
            corrupt_history: bool = False,
            message: str = error_message,
        ):
            citation = {
                "id": citation_id,
                "source_type": "author_supplied_reference",
                "provider_record_id": reference_id,
                "reference_id": reference_id,
                "title": (
                    "架空資料の分類"
                    "と資料保存"
                ),
                "authors": ["自著"],
                "allowed_use": "attributed_scholarship_citation",
                "verification_scope": "bibliographic_identity_only",
                "annotation_proposition_ids": ["p0133"],
                "claim_ids": [],
                "provenance": {
                    "source_ids": [reference_id, "p0133", "p0134"],
                    "bibliographic_identity_verified": True,
                    "substantive_support_verified": False,
                },
            }
            if not source_valid:
                citation["authors"] = ["別の著者"]
            with self.app.state.databases.state.connect() as connection:
                project_id = RevisionService(connection).register_project(
                    name=name,
                    root_path=str(Path(self.temp.name) / name.replace(" ", "-")),
                )
                run = RunRepository(connection).create(
                    workflow_name="authoring",
                    workflow_version="31",
                    entry_node="draft_rewrite",
                    project_id=project_id,
                    inputs={
                        "project_id": project_id,
                        "plan_id": "plan-1",
                        "research_problem": {"research_question": "なぜか"},
                        "section_contracts": [{
                            "section_id": "sec-magazine",
                            "article_role": "body",
                            "claim_ids": ["cl0006"],
                            "evidence_ids": ["b02-ev-cl0006-01"],
                        }],
                        "argument_nodes": [{
                            "id": "b02-ev-cl0006-01",
                            "node_type": "evidence",
                            "claim_id": "cl0006",
                            "provenance": {"source_ids": ["p0133"]},
                        }],
                        "annotation_citation_evidence": [citation],
                    },
                    resource_lane="codex_remote",
                )
                node = connection.execute(
                    "SELECT id FROM node_runs WHERE run_id=? AND node_id='draft_rewrite'",
                    (run.run_id,),
                ).fetchone()
                error = json.dumps({
                    "type": "NonRetryableFukamiruError",
                    "message": message,
                })
                connection.execute(
                    "UPDATE node_runs SET status='failed',started_at=?,completed_at=?"
                    ",error_json=? WHERE id=?",
                    (utc_now(), utc_now(), error, str(node["id"])),
                )
                connection.execute(
                    "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                    (error, utc_now(), run.run_id),
                )
                connection.commit()
                events = EventRepository(connection)
                for index, (policy, fields) in enumerate(history):
                    payload = {
                        "node_id": "draft_rewrite",
                        "workflow_version": "31",
                        **fields,
                    }
                    if corrupt_history and index == 1:
                        policy = "authoring-terminal-codex-replay-v1"
                    events.append(
                        run.run_id,
                        "run.failed_authoring_node_replayed",
                        object_type="node_run",
                        object_id=str(node["id"]),
                        payload={"policy": policy, **payload},
                    )
            return run

        valid = create_failed_run("V31 semantic attribution predicate replay")
        accepted = self.client.post(
            f"/api/runs/{valid.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(accepted.json()["manual_replay_count"], 3)
        with self.app.state.databases.state.connect() as connection:
            replay = connection.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed' "
                "ORDER BY rowid DESC LIMIT 1",
                (valid.run_id,),
            ).fetchone()
        replay_payload = json.loads(replay["payload_json"])
        self.assertTrue(replay_payload["v31_semantic_attribution_predicate_recovery"])
        self.assertEqual(
            replay_payload["policy"],
            "authoring-v31-attribution-predicate-semantic-recovery-v1",
        )

        invalid_source = create_failed_run(
            "V31 semantic attribution predicate invalid source", source_valid=False
        )
        rejected_source = self.client.post(
            f"/api/runs/{invalid_source.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(rejected_source.status_code, 409, rejected_source.text)

        wrong_history = create_failed_run(
            "V31 semantic attribution predicate wrong history",
            corrupt_history=True,
        )
        rejected_history = self.client.post(
            f"/api/runs/{wrong_history.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(rejected_history.status_code, 409, rejected_history.text)

        mixed_error = create_failed_run(
            "V31 semantic attribution predicate mixed failure",
            message=error_message + "; section sec-body has invalid output schema",
        )
        rejected_mixed = self.client.post(
            f"/api/runs/{mixed_error.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(rejected_mixed.status_code, 409, rejected_mixed.text)

    def test_v31_fixed_source_lineage_replay_requires_support_and_consistent_ledger(
        self,
    ) -> None:
        from fukamiru.authoring.evidence_contracts import build_fixed_assets
        from fukamiru.storage.events import EventRepository
        from fukamiru.storage.payloads import PayloadStore
        from fukamiru.storage.runs import _v31_fixed_source_lineage_is_repairable

        citation_id = "annotation-citation:book"
        reference_id = "reference:book"
        history = [
            ("authoring-v31-section-contract-identity-merge-v1",
             "v31_section_contract_identity_recovery"),
            ("authoring-v31-citation-source-claim-bridge-recovery-v1",
             "v31_citation_source_claim_mapping_recovery"),
        ]
        error_message = (
            "draft evidence e1 references unknown or disallowed fixed source IDs: "
            + reference_id
        )

        def create_failed_run(
            name: str,
            *,
            retain_memo_source: bool = True,
            correct_history: bool = True,
            message: str = error_message,
        ):
            fixed = build_fixed_assets({
                "evidence_context": [{"id": citation_id, "title": "作品の書誌情報"}],
                "propositions": [{"id": "p1", "text": "元メモ"}],
            })
            source_ids = [reference_id, "p1"] if retain_memo_source else [reference_id]
            with self.app.state.databases.state.connect() as connection:
                project_id = RevisionService(connection).register_project(
                    name=name,
                    root_path=str(Path(self.temp.name) / name.replace(" ", "-")),
                )
                run = RunRepository(connection).create(
                    workflow_name="authoring",
                    workflow_version="31",
                    entry_node="draft_rewrite",
                    project_id=project_id,
                    inputs={
                        "project_id": project_id,
                        "section_contracts": [{
                            "section_id": "s1", "evidence_ids": ["e1"],
                        }],
                        "argument_nodes": [{
                            "id": "e1", "node_type": "evidence",
                            "provenance": {"source_ids": source_ids},
                        }],
                        "fixed_assets": fixed.model_dump(mode="json"),
                        "annotation_citation_evidence": [{
                            "id": citation_id, "reference_id": reference_id,
                            "verification_scope": "bibliographic_identity_only",
                        }],
                    },
                    resource_lane="codex_remote",
                )
                node = connection.execute(
                    "SELECT id FROM node_runs WHERE run_id=? AND node_id='draft_rewrite'",
                    (run.run_id,),
                ).fetchone()
                error = json.dumps({"type": "FukamiruError", "message": message})
                connection.execute(
                    "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                    "error_json=? WHERE id=?",
                    (utc_now(), utc_now(), error, str(node["id"])),
                )
                connection.execute(
                    "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                    (error, utc_now(), run.run_id),
                )
                connection.commit()
                events = EventRepository(connection)
                for index, (policy, flag) in enumerate(history):
                    events.append(
                        run.run_id,
                        "run.failed_authoring_node_replayed",
                        object_type="node_run",
                        object_id=str(node["id"]),
                        payload={
                            "policy": (
                                "invalid-history" if index == 0 and not correct_history
                                else policy
                            ),
                            "node_id": "draft_rewrite",
                            "workflow_version": "31",
                            flag: True,
                        },
                    )
            return run

        valid = create_failed_run("V31 fixed-source lineage replay")
        with self.app.state.databases.state.connect() as connection:
            saved = connection.execute(
                "SELECT input_json FROM node_runs WHERE run_id=? AND node_id='draft_rewrite'",
                (valid.run_id,),
            ).fetchone()
            replay_input = PayloadStore(connection).unpack(json.loads(saved["input_json"]))
        self.assertTrue(
            _v31_fixed_source_lineage_is_repairable(error_message, replay_input),
            sorted(replay_input),
        )
        accepted = self.client.post(
            f"/api/runs/{valid.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(accepted.json()["manual_replay_count"], 3)
        with self.app.state.databases.state.connect() as connection:
            replay = connection.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed' "
                "ORDER BY rowid DESC LIMIT 1",
                (valid.run_id,),
            ).fetchone()
        payload = json.loads(replay["payload_json"])
        self.assertEqual(payload["policy"], "authoring-v31-fixed-source-lineage-recovery-v1")
        self.assertTrue(payload["v31_fixed_source_lineage_recovery"])

        unsupported = create_failed_run(
            "V31 fixed-source lineage unsupported", retain_memo_source=False
        )
        self.assertEqual(self.client.post(
            f"/api/runs/{unsupported.run_id}/replay-failed-authoring-node"
        ).status_code, 409)
        wrong_history = create_failed_run(
            "V31 fixed-source lineage history", correct_history=False
        )
        self.assertEqual(self.client.post(
            f"/api/runs/{wrong_history.run_id}/replay-failed-authoring-node"
        ).status_code, 409)
        mixed_error = create_failed_run(
            "V31 fixed-source lineage mixed error", message=error_message + "; other"
        )
        self.assertEqual(self.client.post(
            f"/api/runs/{mixed_error.run_id}/replay-failed-authoring-node"
        ).status_code, 409)

    def test_v31_bibliographic_contextual_replay_is_limited_to_identity_only(self) -> None:
        error_message = (
            "V31 rendered citation predicate lacks semantic attribution: "
            "s1:e-book (classification=mere_reference)"
        )

        def create_failed_run(name: str, *, scope: str = "bibliographic_identity_only",
                              message: str = error_message):
            with self.app.state.databases.state.connect() as connection:
                project_id = RevisionService(connection).register_project(
                    name=name,
                    root_path=str(Path(self.temp.name) / name.replace(" ", "-")),
                )
                run = RunRepository(connection).create(
                    workflow_name="authoring",
                    workflow_version="31",
                    entry_node="reconcile_candidate_context",
                    project_id=project_id,
                    inputs={
                        "project_id": project_id,
                        "argument_nodes": [{
                            "id": "e-book", "node_type": "evidence",
                            "provenance": {"source_ids": ["citation-book"]},
                        }],
                        "evidence_context": [{
                            "id": "citation-book",
                            "reference_id": "reference:book",
                            "allowed_use": "attributed_scholarship_citation",
                            "verification_scope": scope,
                            "authors": ["ベッカー"], "title": "アート・ワールド",
                            "provenance": {"substantive_support_verified": False},
                        }],
                        "draft_sections": [{
                            "section_id": "s1",
                            "paragraphs": [{
                                "text": "ベッカー『アート・ワールド』を比較枠に用いる。",
                                "evidence_ids": ["e-book"],
                            }],
                        }],
                    },
                    resource_lane="codex_remote",
                )
                node = connection.execute(
                    "SELECT id FROM node_runs WHERE run_id=? "
                    "AND node_id='reconcile_candidate_context'",
                    (run.run_id,),
                ).fetchone()
                error = json.dumps({"type": "FukamiruError", "message": message})
                connection.execute(
                    "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                    "error_json=? WHERE id=?",
                    (utc_now(), utc_now(), error, str(node["id"])),
                )
                connection.execute(
                    "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                    (error, utc_now(), run.run_id),
                )
                connection.commit()
            return run

        valid = create_failed_run("V31 contextual citation replay")
        accepted = self.client.post(
            f"/api/runs/{valid.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        with self.app.state.databases.state.connect() as connection:
            replay = connection.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed' "
                "ORDER BY rowid DESC LIMIT 1",
                (valid.run_id,),
            ).fetchone()
        payload = json.loads(replay["payload_json"])
        self.assertEqual(
            payload["policy"],
            "authoring-v31-bibliographic-contextual-use-recovery-v1",
        )
        self.assertTrue(payload["v31_bibliographic_contextual_recovery"])

        unsupported = create_failed_run(
            "V31 contextual citation wrong scope", scope="full_text_verified"
        )
        self.assertEqual(self.client.post(
            f"/api/runs/{unsupported.run_id}/replay-failed-authoring-node"
        ).status_code, 409)
        mixed = create_failed_run(
            "V31 contextual citation mixed error", message=error_message + "; other"
        )
        self.assertEqual(self.client.post(
            f"/api/runs/{mixed.run_id}/replay-failed-authoring-node"
        ).status_code, 409)

    def test_cancelled_v31_semantic_repair_reroutes_without_new_draft(self) -> None:
        def cancelled_run(*, category: str, terminal_failed: bool = False):
            inputs = {
                "source_preservation_validation": {"passed": True},
                "draft_sections": [{"section_id": "sec-body", "text": "元の本文"}],
                "audit_findings": [{
                    "finding_id": "AF-1", "section_id": "sec-body",
                    "category": category,
                }],
            }
            with self.app.state.databases.state.connect() as connection:
                project_id = RevisionService(connection).register_project(
                    name="V31 semantic audit reroute",
                    root_path=str(Path(self.temp.name) / f"semantic-{category}-{terminal_failed}"),
                )
                run = RunRepository(connection).create(
                    workflow_name="authoring", workflow_version="31",
                    entry_node="repair_failed_sections", project_id=project_id,
                    inputs=inputs, resource_lane="codex_remote",
                )
                connection.execute(
                    "UPDATE node_runs SET status='failed',error_json=? WHERE run_id=?",
                    (json.dumps({"type": "FukamiruError", "message": "old"}), run.run_id),
                )
                node_id = f"{run.run_id}-repair-attempt-4"
                connection.execute(
                    "INSERT INTO node_runs(id,run_id,node_id,attempt,status,input_json,"
                    "resource_lane) VALUES (?,?,?,?,?,?,?)",
                    (node_id, run.run_id, "repair_failed_sections", 4, "running",
                     json.dumps(inputs), "codex_remote"),
                )
                for policy in (
                    "authoring-v31-provisional-self-citation-repair-v1",
                    "authoring-v31-retired-source-human-review-v1",
                ):
                    connection.execute(
                        "INSERT INTO event_log(run_id,event_type,object_type,object_id,"
                        "payload_json,created_at) VALUES (?,?,?,?,?,?)",
                        (run.run_id, "run.failed_authoring_node_replayed",
                         "node_run", node_id, json.dumps({"policy": policy}),
                         "2026-10-07T11:30:00Z"),
                    )
                connection.execute(
                    "UPDATE runs SET status='running' WHERE id=?", (run.run_id,)
                )
                connection.commit()
            self.assertEqual(
                self.client.post(f"/api/runs/{run.run_id}/cancel").status_code,
                200,
            )
            if terminal_failed:
                error = json.dumps({
                    "type": "FukamiruError",
                    "message": "V31 repair did not prove resolution for findings: "
                    "AF-511c0b779b5d5119, AF-4bb582d81f236411",
                })
                with self.app.state.databases.state.connect() as connection:
                    connection.execute(
                        "UPDATE node_runs SET status='failed',error_json=? "
                        "WHERE id=?", (error, node_id),
                    )
                    connection.execute(
                        "UPDATE runs SET status='failed',error_json=? WHERE id=?",
                        (error, run.run_id),
                    )
                    connection.commit()
            return run

        valid = cancelled_run(category="source_review_note_leak")
        response = self.client.post(
            f"/api/runs/{valid.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["node_id"], "repair_failed_sections")
        self.assertEqual(response.json()["attempt"], 5)
        with self.app.state.databases.state.connect() as connection:
            row = connection.execute(
                "SELECT input_json FROM node_runs WHERE id=?",
                (response.json()["node_run_id"],),
            ).fetchone()
        self.assertEqual(
            json.loads(row["input_json"])["authoring_recovery_lineage"]["policy"],
            "authoring-v31-semantic-audit-review-reroute-v1",
        )
        self.assertEqual(self.client.post(
            f"/api/runs/{valid.run_id}/replay-failed-authoring-node"
        ).status_code, 409)

        failed_after_cancel = cancelled_run(
            category="source_review_note_leak", terminal_failed=True
        )
        recovered = self.client.post(
            f"/api/runs/{failed_after_cancel.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(recovered.status_code, 200, recovered.text)
        self.assertEqual(recovered.json()["attempt"], 5)

        unrelated = cancelled_run(category="unknown_source")
        self.assertEqual(self.client.post(
            f"/api/runs/{unrelated.run_id}/replay-failed-authoring-node"
        ).status_code, 409)

    def test_v31_audit_join_classification_recovery_requires_clean_classifier(self) -> None:
        error = json.dumps({
            "type": "FukamiruError",
            "message": "automatic repair limit reached for the same finding set",
        })

        def failed_run(*, clean_classifier: bool):
            inputs = {
                "proposed_html": "<article>draft</article>",
                "source_preservation_validation": {"passed": True},
                "draft_sections": [{"section_id": "sec-body"}],
                "auto_repair_findings": [{"finding_id": "AF-old"}],
            }
            classified = {
                **inputs,
                "auto_repair_findings": [] if clean_classifier else [{"finding_id": "AF-old"}],
                "human_review_findings": [{"finding_id": "AF-old"}],
            }
            with self.app.state.databases.state.connect() as connection:
                project_id = RevisionService(connection).register_project(
                    name="V31 audit join recovery",
                    root_path=str(Path(self.temp.name) / f"join-{clean_classifier}"),
                )
                run = RunRepository(connection).create(
                    workflow_name="authoring", workflow_version="31",
                    entry_node="repair_failed_sections", project_id=project_id,
                    inputs=inputs, resource_lane="codex_remote",
                )
                connection.execute(
                    "UPDATE node_runs SET status='failed',error_json=? WHERE run_id=?",
                    (json.dumps({"type": "FukamiruError", "message": "old"}), run.run_id),
                )
                connection.execute(
                    "INSERT INTO node_runs(id,run_id,node_id,attempt,status,input_json,"
                    "output_json,resource_lane) VALUES (?,?,?,?,?,?,?,?)",
                    (f"{run.run_id}-classified", run.run_id,
                     "classify_audit_findings", 1, "completed",
                     json.dumps(inputs), json.dumps(classified), "cpu"),
                )
                connection.execute(
                    "INSERT INTO node_runs(id,run_id,node_id,attempt,status,input_json,"
                    "error_json,resource_lane) VALUES (?,?,?,?,?,?,?,?)",
                    (f"{run.run_id}-repair-8", run.run_id,
                     "repair_failed_sections", 8, "failed",
                     json.dumps(inputs), error, "codex_remote"),
                )
                connection.execute(
                    "INSERT INTO event_log(run_id,event_type,object_type,object_id,"
                    "payload_json,created_at) VALUES (?,?,?,?,?,?)",
                    (run.run_id, "run.failed_authoring_node_replayed", "node_run",
                     f"{run.run_id}-earlier",
                     json.dumps({"policy": "authoring-v31-semantic-audit-review-reroute-v1"}),
                     "2026-10-07T12:20:50Z"),
                )
                connection.execute(
                    "UPDATE runs SET status='failed',error_json=? WHERE id=?",
                    (error, run.run_id),
                )
                connection.commit()
            return run

        valid = failed_run(clean_classifier=True)
        resumed = self.client.post(
            f"/api/runs/{valid.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(resumed.status_code, 200, resumed.text)
        self.assertEqual(resumed.json()["attempt"], 9)
        with self.app.state.databases.state.connect() as connection:
            row = connection.execute(
                "SELECT input_json FROM node_runs WHERE id=?",
                (resumed.json()["node_run_id"],),
            ).fetchone()
        self.assertEqual(
            json.loads(row["input_json"])["authoring_recovery_lineage"]["policy"],
            "authoring-v31-audit-join-classification-recovery-v1",
        )
        self.assertEqual(self.client.post(
            f"/api/runs/{valid.run_id}/replay-failed-authoring-node"
        ).status_code, 409)

        # A worker can fail before entering the V31 handler if the dispatcher
        # still reads a stale repair fingerprint. Recover that exact handoff once.
        with self.app.state.databases.state.connect() as connection:
            connection.execute(
                "UPDATE node_runs SET status='failed',error_json=? WHERE id=?",
                (error, resumed.json()["node_run_id"]),
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=? WHERE id=?",
                (error, valid.run_id),
            )
            connection.commit()
        dispatch = self.client.post(
            f"/api/runs/{valid.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(dispatch.status_code, 200, dispatch.text)
        self.assertEqual(dispatch.json()["attempt"], 10)
        with self.app.state.databases.state.connect() as connection:
            dispatch_input = json.loads(connection.execute(
                "SELECT input_json FROM node_runs WHERE id=?",
                (dispatch.json()["node_run_id"],),
            ).fetchone()["input_json"])
        self.assertEqual(
            dispatch_input["authoring_recovery_lineage"]["policy"],
            "authoring-v31-audit-join-dispatch-recovery-v1",
        )

        invalid = failed_run(clean_classifier=False)
        self.assertEqual(self.client.post(
            f"/api/runs/{invalid.run_id}/replay-failed-authoring-node"
        ).status_code, 409)

    def test_v31_retired_source_repair_replays_only_exact_prior_failure(self) -> None:
        error_message = (
            "V31 repair did not prove resolution for findings: "
            "AF-62959996db8441d1, AF-2ef6eb8ac286b64c, "
            "AF-a2a9c7e5f445dc32"
        )

        def failed_run(*, exact_error: bool):
            inputs = {
                "audit_findings": [
                    {"finding_id": finding_id}
                    for finding_id in (
                        "AF-62959996db8441d1",
                        "AF-2ef6eb8ac286b64c",
                        "AF-a2a9c7e5f445dc32",
                    )
                ],
            }
            with self.app.state.databases.state.connect() as connection:
                project_id = RevisionService(connection).register_project(
                    name="V31 retired source replay",
                    root_path=str(Path(self.temp.name) / f"retired-{exact_error}"),
                )
                run = RunRepository(connection).create(
                    workflow_name="authoring", workflow_version="31",
                    entry_node="repair_failed_sections", project_id=project_id,
                    inputs=inputs, resource_lane="codex_remote",
                )
                old_error = json.dumps({"type": "FukamiruError", "message": "old"})
                new_error = json.dumps({
                    "type": "FukamiruError",
                    "message": error_message if exact_error else error_message + "; other",
                })
                connection.execute(
                    "UPDATE node_runs SET status='failed',error_json=? WHERE run_id=?",
                    (old_error, run.run_id),
                )
                node_id = f"{run.run_id}-repair-attempt-3"
                connection.execute(
                    "INSERT INTO node_runs(id,run_id,node_id,attempt,status,input_json,"
                    "error_json,resource_lane) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        node_id, run.run_id, "repair_failed_sections", 3, "failed",
                        json.dumps(inputs), new_error, "codex_remote",
                    ),
                )
                connection.execute(
                    "UPDATE runs SET status='failed',error_json=? WHERE id=?",
                    (new_error, run.run_id),
                )
                connection.execute(
                    "INSERT INTO event_log(run_id,event_type,object_type,object_id,"
                    "payload_json,created_at) VALUES (?,?,?,?,?,?)",
                    (
                        run.run_id, "run.failed_authoring_node_replayed",
                        "node_run", node_id,
                        json.dumps({
                            "policy": "authoring-v31-provisional-self-citation-repair-v1"
                        }),
                        "2026-10-07T10:30:41Z",
                    ),
                )
                connection.commit()
            return run

        valid = failed_run(exact_error=True)
        resumed = self.client.post(
            f"/api/runs/{valid.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(resumed.status_code, 200, resumed.text)
        self.assertEqual(resumed.json()["attempt"], 4)
        with self.app.state.databases.state.connect() as connection:
            event = connection.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed' "
                "ORDER BY rowid DESC LIMIT 1", (valid.run_id,),
            ).fetchone()
        self.assertEqual(
            json.loads(event["payload_json"])["policy"],
            "authoring-v31-retired-source-human-review-v1",
        )
        self.assertEqual(self.client.post(
            f"/api/runs/{valid.run_id}/replay-failed-authoring-node"
        ).status_code, 409)

        unrelated = failed_run(exact_error=False)
        self.assertEqual(self.client.post(
            f"/api/runs/{unrelated.run_id}/replay-failed-authoring-node"
        ).status_code, 409)

    def test_cancelled_composite_self_citation_repair_replays_only_matching_run(self) -> None:
        citation_id = "annotation-citation:self-citation"
        evidence_id = "annotation-citation-node:self-citation"
        failed_error = {
            "type": "FukamiruError",
            "message": (
                "V31 targeted repair repeatedly broke its JSON section contract: "
                f"section sec-magazine scholarship evidence {evidence_id} "
                "has no semantically confirmed attribution predicate "
                "(classification=contextual_use)"
            ),
        }

        def cancelled_run(*, mixed_title: bool):
            title = (
                "単独論考――『作品』論」"
                "、『収録書』（2022） ・「別の論考」"
                if mixed_title else "単独論考"
            )
            inputs = {
                "argument_nodes": [{
                    "id": evidence_id, "node_type": "evidence",
                    "source_span_id": citation_id,
                }],
                "annotation_citation_evidence": [{
                    "id": citation_id, "authors": ["自著"],
                    "reference_id": "reference:self-citation",
                }],
                "reference_entries": [{
                    "reference_id": "reference:self-citation",
                    "original_text": "自著「単独論考」", "title": title,
                }],
                "audit_findings": [{
                    "section_id": "sec-magazine", "category": "evidence_mismatch",
                    "description": (
                        f"{evidence_id} のwork欄は複合文字列なので補修する"
                    ),
                }],
            }
            with self.app.state.databases.state.connect() as connection:
                project_id = RevisionService(connection).register_project(
                    name="V31 cancelled self citation repair",
                    root_path=str(Path(self.temp.name) / f"self-{mixed_title}"),
                )
                run = RunRepository(connection).create(
                    workflow_name="authoring", workflow_version="31",
                    entry_node="repair_failed_sections", project_id=project_id,
                    inputs=inputs, resource_lane="codex_remote",
                )
                connection.execute(
                    "UPDATE node_runs SET status='failed',error_json=?,completed_at=? "
                    "WHERE run_id=? AND node_id='repair_failed_sections'",
                    (json.dumps(failed_error), utc_now(), run.run_id),
                )
                connection.execute(
                    "INSERT INTO node_runs(id,run_id,node_id,attempt,status,input_json,"
                    "resource_lane) VALUES (?,?,?,?,?,?,?)",
                    (
                        f"{run.run_id}-attempt-2", run.run_id,
                        "repair_failed_sections", 2, "queued",
                        json.dumps(inputs, ensure_ascii=False), "codex_remote",
                    ),
                )
                connection.execute(
                    "UPDATE runs SET status='running' WHERE id=?", (run.run_id,)
                )
                connection.commit()
            self.assertEqual(
                self.client.post(f"/api/runs/{run.run_id}/cancel").status_code,
                200,
            )
            return run

        valid = cancelled_run(mixed_title=True)
        resumed = self.client.post(
            f"/api/runs/{valid.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(resumed.status_code, 200, resumed.text)
        self.assertEqual(resumed.json()["node_id"], "repair_failed_sections")
        self.assertEqual(resumed.json()["attempt"], 3)
        with self.app.state.databases.state.connect() as connection:
            event = connection.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed' "
                "ORDER BY rowid DESC LIMIT 1", (valid.run_id,),
            ).fetchone()
        self.assertEqual(
            json.loads(event["payload_json"])["policy"],
            "authoring-v31-provisional-self-citation-repair-v1",
        )
        self.assertEqual(self.client.post(
            f"/api/runs/{valid.run_id}/replay-failed-authoring-node"
        ).status_code, 409)

        unrelated = cancelled_run(mixed_title=False)
        self.assertEqual(self.client.post(
            f"/api/runs/{unrelated.run_id}/replay-failed-authoring-node"
        ).status_code, 409)

    def test_cancelled_rag_context_miss_can_resume_same_v31_draft_run(self) -> None:
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="V31 cancelled retrieval replay",
                root_path=str(Path(self.temp.name) / "v31-cancelled-retrieval-replay"),
            )
            run = RunRepository(connection).create(
                workflow_name="authoring",
                workflow_version="31",
                entry_node="draft_rewrite",
                project_id=project_id,
                inputs={
                    "project_id": project_id,
                    "plan_id": "plan-1",
                    "section_contracts": [{"section_id": "s012"}],
                    "argument_nodes": [{"id": "e1"}],
                    "research_problem": {"research_question": "なぜか"},
                },
                resource_lane="codex_remote",
            )
            error = json.dumps({
                "type": "NonRetryableFukamiruError",
                "message": (
                    "V31 role draft JSON handoff violation: section s012 scholarship "
                    "evidence e1 has no unique sentence-level footnote anchor"
                ),
            })

            def fail_again(connection) -> None:
                connection.execute(
                    "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                    "error_json=? WHERE run_id=? AND node_id='draft_rewrite'",
                    (utc_now(), utc_now(), error, run.run_id),
                )
                connection.execute(
                    "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                    (error, utc_now(), run.run_id),
                )
                connection.commit()

            def add_invocation(
                connection,
                operation_name: str,
                *,
                rag_context_characters: int,
                checkpoint_status: str,
                created_at: str,
            ) -> None:
                connection.execute(
                    "INSERT INTO llm_invocation_observations ("
                    "id,run_id,node_id,stage,operation_name,model,reasoning_effort,"
                    "full_prompt_sha256,prefix_1024_sha256,prefix_4096_sha256,"
                    "prefix_16384_sha256,request_fingerprint_sha256,cache_family_sha256,"
                    "prompt_characters,payload_characters,rag_context_characters,"
                    "common_prefix_characters,checkpoint_status,seed_role,"
                    "zero_cached_input_call_count,created_at"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        f"observation-{operation_name}-{checkpoint_status}-{created_at}",
                        run.run_id,
                        "draft_rewrite",
                        "draft_v31",
                        operation_name,
                        "gpt-6-luna",
                        "medium",
                        "full-prompt-hash",
                        "prefix-1024-hash",
                        "prefix-4096-hash",
                        "prefix-16384-hash",
                        f"request-{operation_name}-{checkpoint_status}-{created_at}",
                        f"family-{operation_name}-{checkpoint_status}-{created_at}",
                        1000,
                        800,
                        rag_context_characters,
                        0,
                        checkpoint_status,
                        "normal",
                        0,
                        created_at,
                    ),
                )

            fail_again(connection)

        first = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(first.status_code, 200, first.text)

        with self.app.state.databases.state.connect() as connection:
            add_invocation(
                connection,
                "authoring_draft_introduction_1",
                rag_context_characters=5418,
                checkpoint_status="hit",
                created_at="2026-10-04T09:50:00.000000+00:00",
            )
            add_invocation(
                connection,
                "authoring_draft_body_2",
                rag_context_characters=5418,
                checkpoint_status="hit",
                created_at="2026-10-04T09:50:01.000000+00:00",
            )
            fail_again(connection)

        second = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["manual_replay_count"], 2)

        with self.app.state.databases.state.connect() as connection:
            add_invocation(
                connection,
                "authoring_draft_introduction_1",
                rag_context_characters=0,
                checkpoint_status="miss",
                created_at=utc_now(),
            )
            add_invocation(
                connection,
                "authoring_draft_body_2",
                rag_context_characters=0,
                checkpoint_status="miss",
                created_at=utc_now(),
            )

        cancelled = self.client.post(f"/api/runs/{run.run_id}/cancel")
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        resumed = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(resumed.status_code, 200, resumed.text)
        self.assertEqual(resumed.json()["manual_replay_count"], 3)
        with self.app.state.databases.state.connect() as connection:
            event = connection.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed' ORDER BY rowid DESC LIMIT 1",
                (run.run_id,),
            ).fetchone()
        self.assertTrue(
            json.loads(event["payload_json"])["v31_cancelled_rag_context_recovery"]
        )

        with self.app.state.databases.state.connect() as connection:
            fail_again(connection)
        final_recovery = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(final_recovery.status_code, 200, final_recovery.text)
        self.assertEqual(final_recovery.json()["manual_replay_count"], 4)
        with self.app.state.databases.state.connect() as connection:
            event = connection.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed' ORDER BY rowid DESC LIMIT 1",
                (run.run_id,),
            ).fetchone()
        final_payload = json.loads(event["payload_json"])
        self.assertEqual(
            final_payload["policy"],
            "authoring-v31-structure-aware-draft-recovery-v1",
        )
        self.assertTrue(final_payload["v31_structure_aware_draft_recovery"])

        with self.app.state.databases.state.connect() as connection:
            fail_again(connection)
        exhausted = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(exhausted.status_code, 409, exhausted.text)
        self.assertIn("already used its manual replay", exhausted.text)

    def test_v31_aggregated_self_citation_failure_can_replay_in_place(self) -> None:
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="V31 self citation replay",
                root_path=str(Path(self.temp.name) / "v31-self-citation-replay"),
            )
            run = RunRepository(connection).create(
                workflow_name="authoring",
                workflow_version="31",
                entry_node="render_evidence_footnotes",
                project_id=project_id,
                inputs={
                    "project_id": project_id,
                    "reference_entries": [{
                        "is_self_authored": True,
                        "record_type": "nonstandard_contribution",
                        "title": "短題」、『論集』",
                        "container_title": "論集", "year": "2022", "pages": "10-20",
                        "bibliography_people": [{"role": "author"}],
                    }],
                },
                resource_lane="ingestion_cpu",
            )
            error = json.dumps({
                "type": "FukamiruError",
                "message": (
                    "Footnote bibliography metadata requires review: "
                    "record_kind_unconfirmed"
                ),
            })
            connection.execute(
                "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                "error_json=? WHERE run_id=? AND node_id='render_evidence_footnotes'",
                (utc_now(), utc_now(), error, run.run_id),
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                (error, utc_now(), run.run_id),
            )
            connection.commit()

        response = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["node_id"], "render_evidence_footnotes")
        with self.app.state.databases.state.connect() as connection:
            event = connection.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed'",
                (run.run_id,),
            ).fetchone()
        self.assertTrue(
            json.loads(event["payload_json"])["v31_aggregated_self_citation_recovery"]
        )

    def test_v31_resolved_nonstandard_footnote_can_replay_for_provisional_review(self) -> None:
        for resolution_status, expected_status in (("resolved", 200), ("ambiguous", 409)):
            with self.app.state.databases.state.connect() as connection:
                project_id = RevisionService(connection).register_project(
                    name="V31 provisional bibliography replay " + resolution_status,
                    root_path=str(Path(self.temp.name) / resolution_status),
                )
                run = RunRepository(connection).create(
                    workflow_name="manuscript_generator",
                    workflow_version="31",
                    entry_node="render_evidence_footnotes",
                    project_id=project_id,
                    inputs={"project_id": project_id, "reference_entries": [{
                        "reference_id": "reference:nonstandard-1",
                        "title": "架空資料の比較分析",
                        "record_type": "nonstandard_contribution",
                        "resolution_status": resolution_status,
                        "bibliography_people": [{
                            "role": "author", "family": "山田", "given": "花子",
                        }],
                    }]},
                    resource_lane="ingestion_cpu",
                )
                error = json.dumps({
                    "type": "FukamiruError",
                    "message": (
                        "Footnote bibliography metadata requires review: "
                        "record_kind_unconfirmed"
                    ),
                })
                connection.execute(
                    "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                    "error_json=? WHERE run_id=? AND node_id='render_evidence_footnotes'",
                    (utc_now(), utc_now(), error, run.run_id),
                )
                connection.execute(
                    "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                    (error, utc_now(), run.run_id),
                )
                connection.commit()

            response = self.client.post(
                f"/api/runs/{run.run_id}/replay-failed-authoring-node"
            )
            self.assertEqual(response.status_code, expected_status, response.text)
            if resolution_status == "resolved":
                with self.app.state.databases.state.connect() as connection:
                    event = connection.execute(
                        "SELECT payload_json FROM event_log WHERE run_id=? "
                        "AND event_type='run.failed_authoring_node_replayed'",
                        (run.run_id,),
                    ).fetchone()
                payload = json.loads(event["payload_json"])
                self.assertEqual(
                    payload["policy"],
                    "authoring-v31-provisional-bibliography-review-v1",
                )

    def test_self_authored_provenance_evidence_failure_can_replay_repair(self) -> None:
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="Authoring provenance repair replay",
                root_path=str(Path(self.temp.name) / "provenance-repair-replay"),
            )
            run = RunRepository(connection).create(
                workflow_name="authoring",
                workflow_version="30",
                entry_node="repair_failed_sections",
                project_id=project_id,
                inputs={
                    "project_id": project_id,
                    "section_contracts": [{"section_id": "sec-14"}],
                    "argument_nodes": [{"id": "memo-evidence", "node_type": "evidence"}],
                    "audit_findings": [{
                        "code": "provenance_loss", "object_id": "sec-14",
                        "message": "自著の出典を本文で示す",
                    }],
                },
                resource_lane="codex_remote",
            )
            error = json.dumps({
                "type": "NonRetryableFukamiruError",
                "message": (
                    "Structural repair violated the locked draft contract: "
                    "section sec-14 paragraphs use non-contract evidence: "
                    "['memo-evidence']"
                ),
            })
            connection.execute(
                "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                "error_json=? WHERE run_id=? AND node_id='repair_failed_sections'",
                (utc_now(), utc_now(), error, run.run_id),
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                (error, utc_now(), run.run_id),
            )
            connection.commit()

        response = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["node_id"], "repair_failed_sections")

    def test_compound_coordination_surface_false_positive_can_be_rechecked(self) -> None:
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="Authoring surface recheck",
                root_path=str(Path(self.temp.name) / "authoring-surface-recheck"),
            )
            node_input = {
                "project_id": project_id,
                "manuscript_surface_quality_gate": {
                    "remaining_issue_blocks": [
                        {
                            "block_id": "p1",
                            "issues": [
                                {"code": "surface.coordinated_lexical_repetition"}
                            ],
                        }
                    ]
                },
                "submission_validation_gate": {"failed_validations": []},
            }
            run = RunRepository(connection).create(
                workflow_name="authoring",
                workflow_version="30",
                entry_node="freeze_final_candidate",
                project_id=project_id,
                inputs=node_input,
                resource_lane="ingestion_cpu",
            )
            error = json.dumps(
                {
                    "type": "CoherenceResolutionRequired",
                    "message": "v26.surface_candidate_requires_repair",
                }
            )
            connection.execute(
                "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                "error_json=? WHERE run_id=? AND node_id='freeze_final_candidate'",
                (utc_now(), utc_now(), error, run.run_id),
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                (error, utc_now(), run.run_id),
            )
            connection.commit()

        response = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )

        self.assertEqual(response.status_code, 200, response.text)
        with self.app.state.databases.state.connect() as connection:
            event = connection.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed'",
                (run.run_id,),
            ).fetchone()
        self.assertTrue(
            json.loads(event["payload_json"])[
                "compound_coordination_surface_recheck"
            ]
        )

    def test_failed_revalidated_authoring_proposal_can_replay_finalization(self) -> None:
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="Authoring proposal revalidation replay",
                root_path=str(Path(self.temp.name) / "proposal-revalidation-replay"),
            )
            node_input = {
                "project_id": project_id,
                "authoring_restart_lineage": {
                    "policy": "authoring-contract-boundary-restart-v1"
                },
            }
            run = RunRepository(connection).create(
                workflow_name="authoring",
                workflow_version="30",
                entry_node="finalize_authoring",
                project_id=project_id,
                inputs=node_input,
                resource_lane="ingestion_cpu",
            )
            error = json.dumps(
                {
                    "type": "ValueError",
                    "message": "authoring run already has a pending proposal",
                }
            )
            connection.execute(
                "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                "error_json=? WHERE run_id=? AND node_id='finalize_authoring'",
                (utc_now(), utc_now(), error, run.run_id),
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                (error, utc_now(), run.run_id),
            )
            connection.execute(
                "INSERT INTO event_log(run_id,event_type,object_type,object_id,"
                "payload_json,created_at) VALUES "
                "(?,'run.authoring_submission_revalidation_started','run',?,'{}',?)",
                (run.run_id, run.run_id, utc_now()),
            )
            connection.commit()

        response = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["node_id"], "finalize_authoring")
        with self.app.state.databases.state.connect() as connection:
            event = connection.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed'",
                (run.run_id,),
            ).fetchone()
            replayed_input = connection.execute(
                "SELECT input_json FROM node_runs WHERE run_id=? "
                "AND node_id='finalize_authoring'",
                (run.run_id,),
            ).fetchone()
        self.assertTrue(
            json.loads(event["payload_json"])["proposal_revalidation_recovery"]
        )
        self.assertTrue(
            json.loads(replayed_input["input_json"])[
                "authoring_submission_revalidation"
            ]["recovered_from_event"]
        )

    def test_failed_checkpoint_plan_can_replay_finalization(self) -> None:
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="Authoring checkpoint plan replay",
                root_path=str(Path(self.temp.name) / "checkpoint-plan-replay"),
            )
            node_input = {
                "project_id": project_id,
                "authoring_restart_lineage": {
                    "policy": "authoring-contract-boundary-restart-v1"
                },
                "_authoring_checkpoint": {
                    "source_run_id": "source-run",
                    "source_plan_id": "source-plan",
                    "new_plan_id": "branch-plan",
                    "plan_identity_preserved": False,
                },
            }
            run = RunRepository(connection).create(
                workflow_name="authoring",
                workflow_version="30",
                entry_node="finalize_authoring",
                project_id=project_id,
                inputs=node_input,
                resource_lane="ingestion_cpu",
            )
            error = json.dumps(
                {
                    "type": "ValueError",
                    "message": "proposal plan does not belong to the target paper",
                }
            )
            connection.execute(
                "UPDATE node_runs SET status='failed',started_at=?,completed_at=?,"
                "error_json=? WHERE run_id=? AND node_id='finalize_authoring'",
                (utc_now(), utc_now(), error, run.run_id),
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                (error, utc_now(), run.run_id),
            )
            connection.commit()

        response = self.client.post(
            f"/api/runs/{run.run_id}/replay-failed-authoring-node"
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["node_id"], "finalize_authoring")
        with self.app.state.databases.state.connect() as connection:
            event = connection.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed'",
                (run.run_id,),
            ).fetchone()
        self.assertTrue(json.loads(event["payload_json"])["checkpoint_plan_recovery"])

    def test_v31_verified_publisher_restart_requeues_only_reference_tail(self) -> None:
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="V31 verified publisher restart",
                root_path=str(Path(self.temp.name) / "v31-publisher-restart"),
            )
            context = {
                "proposed_html": "<article><p>本文</p></article>",
                "reference_entries": [{
                    "reference_id": "reference:fixture-92908a94decffc2a",
                    "record_type": "chapter", "is_self_authored": True,
                    "title": "架空の章", "author": "山田花子", "publisher": None,
                    "field_evidence": {"publisher": [{
                        "value": "Example Research Press",
                        "source_url": "https://publisher.example.test/chapter",
                        "verification_status": "corroborated",
                        "bibliographic_identity": {
                            "reference_id": "reference:fixture-92908a94decffc2a",
                            "title": "架空の章", "author": "山田花子", "record_type": "chapter",
                        },
                    }]},
                }],
                "reference_quality_gate": {"passed": False},
                "submission_validation_gate": {"passed": False},
                "_join_generations": {"join_submission_validations": "old"},
            }
            run = RunRepository(connection).create(
                workflow_name="authoring", workflow_version="31",
                entry_node="render_evidence_footnotes", project_id=project_id,
                inputs=context, resource_lane="ingestion_cpu",
            )
            connection.execute(
                "UPDATE node_runs SET status='completed',output_json=?,completed_at=? "
                "WHERE run_id=? AND node_id='render_evidence_footnotes'",
                (json.dumps(context), utc_now(), run.run_id),
            )
            quality = {
                **context,
                "reference_quality_gate": {
                    "passed": False,
                    "issues": [{
                        "code": "reference.metadata_incomplete",
                        "severity": "error",
                        "reference_id": "reference:fixture-92908a94decffc2a",
                        "fields": ["publisher"],
                    }],
                },
            }
            connection.execute(
                "INSERT INTO node_runs(id,run_id,node_id,attempt,status,input_json,"
                "output_json,resource_lane) VALUES "
                "('publisher-quality',?,'validate_reference_quality',1,'completed',"
                "?,?,'ingestion_cpu')",
                (run.run_id, json.dumps(context), json.dumps(quality)),
            )
            connection.execute(
                "INSERT INTO node_runs(id,run_id,node_id,attempt,status,input_json,"
                "resource_lane) VALUES "
                "('old-structure',?,'validate_footnoted_structure',1,'queued',?,"
                "'ingestion_cpu')",
                (run.run_id, json.dumps(context)),
            )
            connection.execute(
                "UPDATE runs SET status='running' WHERE id=?", (run.run_id,)
            )
            connection.commit()
            restarted = RunRepository(connection).restart_v31_verified_reference_tail(
                run.run_id
            )
            queued = connection.execute(
                "SELECT input_json FROM node_runs WHERE id=?",
                (restarted["node_run_id"],),
            ).fetchone()
            old_status = connection.execute(
                "SELECT status FROM node_runs WHERE id='old-structure'"
            ).fetchone()[0]
            run_status = connection.execute(
                "SELECT status FROM runs WHERE id=?", (run.run_id,)
            ).fetchone()[0]
            source_node_run_id = connection.execute(
                "SELECT id FROM node_runs WHERE run_id=? AND node_id="
                "'render_evidence_footnotes' AND attempt=1", (run.run_id,)
            ).fetchone()[0]
        self.assertEqual(restarted["node_id"], "render_evidence_footnotes")
        self.assertEqual(restarted["attempt"], 2)
        self.assertEqual(old_status, "cancelled")
        self.assertEqual(run_status, "queued")
        replay_input = json.loads(queued["input_json"])
        self.assertNotIn("reference_quality_gate", replay_input)
        self.assertNotIn("submission_validation_gate", replay_input)
        self.assertEqual(
            replay_input["authoring_reference_tail_restart"]["source_node_run_id"],
            source_node_run_id,
        )

    def test_v31_verified_reference_join_reuses_completed_audits(self) -> None:
        html = "<article><p>updated bibliography</p></article>"
        previous_html = "<article><p>old bibliography</p></article>"
        sha = hashlib.sha256(html.encode()).hexdigest()
        base = {
            "proposed_html": html,
            "reference_rendered_html_v26": previous_html,
            "_join_generations": {"join_submission_validations": "new-generation"},
        }
        outputs = {
            "validate_reference_quality": {
                **base,
                "reference_quality_gate": {
                    "passed": True, "target_html_sha256": sha,
                },
            },
            "validate_footnoted_references": {
                **base, "reference_quality_gate": {"passed": True},
            },
            "validate_footnoted_structure": {
                **base,
                "reference_rendered_html_v26": html,
                "rendered_structure_v26": {"valid": True, "html_sha256": sha},
            },
            "validate_footnoted_coherence": {
                **base,
                "footnoted_coherence_report": {"html_sha256": sha},
                "footnoted_block_verification_records": [{"block_id": "p1"}],
            },
        }
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="V31 reference join recovery",
                root_path=str(Path(self.temp.name) / "v31-reference-join"),
            )
            run = RunRepository(connection).create(
                workflow_name="authoring", workflow_version="31",
                entry_node="validate_reference_quality", project_id=project_id,
                inputs=base, resource_lane="ingestion_cpu",
            )
            for index, (node_id, output) in enumerate(outputs.items()):
                status = (
                    "failed" if node_id == "validate_footnoted_coherence"
                    else "completed"
                )
                error = (
                    json.dumps({
                        "type": "FukamiruError",
                        "message": (
                            "Join context conflict at "
                            "validate_footnoted_structure.reference_rendered_html_v26"
                        ),
                    })
                    if status == "failed" else None
                )
                if index == 0:
                    connection.execute(
                        "UPDATE node_runs SET status=?,output_json=?,error_json=? "
                        "WHERE run_id=? AND node_id=?",
                        (status, json.dumps(output), error, run.run_id, node_id),
                    )
                else:
                    connection.execute(
                        "INSERT INTO node_runs(id,run_id,node_id,attempt,status,"
                        "input_json,output_json,error_json,resource_lane) VALUES "
                        "(?,?,?,1,?,?,?,?, 'ingestion_cpu')",
                        (
                            f"join-parent-{index}", run.run_id, node_id, status,
                            json.dumps(base), json.dumps(output), error,
                        ),
                    )
            connection.execute(
                "UPDATE runs SET status='failed' WHERE id=?", (run.run_id,)
            )
            connection.commit()
            restarted = RunRepository(connection).recover_v31_verified_reference_join(
                run.run_id
            )
            row = connection.execute(
                "SELECT input_json FROM node_runs WHERE id=?",
                (restarted["node_run_id"],),
            ).fetchone()
            run_status = connection.execute(
                "SELECT status FROM runs WHERE id=?", (run.run_id,)
            ).fetchone()[0]
        merged = json.loads(row["input_json"])
        self.assertEqual(run_status, "queued")
        self.assertEqual(merged["reference_rendered_html_v26"], html)
        self.assertTrue(merged["reference_quality_gate"]["passed"])
        self.assertEqual(merged["footnoted_coherence_report"]["html_sha256"], sha)

    def test_authoring_submission_revalidation_reuses_unaffected_tail_checks(self) -> None:
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="Authoring submission revalidation",
                root_path=str(Path(self.temp.name) / "authoring-submission-revalidation"),
            )
            base_context = {
                "project_id": project_id,
                "authoring_restart_lineage": {
                    "policy": "authoring-contract-boundary-restart-v1"
                },
                "citation_event_drafts": [
                    {"evidence_id": "annotation-citation:azuma"}
                ],
                "_join_generations": {
                    "join_submission_validations": "old-submission-generation",
                    "audit_final_findings": "old-finding-generation",
                },
            }
            quality_output = {
                **base_context,
                "reference_quality_gate": {
                    "passed": False,
                    "issues": [
                        {
                            "code": "reference.required_author_citation_missing",
                            "severity": "error",
                        }
                    ],
                },
            }
            run = RunRepository(connection).create(
                workflow_name="authoring",
                workflow_version="30",
                entry_node="validate_reference_quality",
                project_id=project_id,
                inputs=base_context,
                resource_lane="ingestion_cpu",
            )
            connection.execute(
                "UPDATE node_runs SET status='completed',started_at=?,completed_at=?,"
                "output_json=? WHERE run_id=? AND node_id='validate_reference_quality'",
                (
                    utc_now(),
                    utc_now(),
                    json.dumps(quality_output),
                    run.run_id,
                ),
            )
            for node_id in (
                "validate_footnoted_references",
                "validate_footnoted_structure",
                "validate_footnoted_coherence",
                "publish_proofreading_shadow",
            ):
                checkpoint_output = (
                    {
                        **base_context,
                        "reference_quality_gate": {
                            "rendered_author_citation_count": 0
                        },
                    }
                    if node_id == "validate_footnoted_references"
                    else base_context
                )
                connection.execute(
                    "INSERT INTO node_runs("
                    "id,run_id,node_id,attempt,status,input_json,output_json,"
                    "started_at,completed_at,resource_lane) "
                    "VALUES (?,?,?,?, 'completed',?,?,?,?, 'ingestion_cpu')",
                    (
                        f"source-{node_id}",
                        run.run_id,
                        node_id,
                        1,
                        json.dumps(base_context),
                        json.dumps(checkpoint_output),
                        utc_now(),
                        utc_now(),
                    ),
                )
            connection.execute(
                "INSERT INTO node_runs("
                "id,run_id,node_id,attempt,status,input_json,output_json,"
                "started_at,resource_lane) "
                "VALUES ('old-human',?, 'human_approval',1,'waiting_human',?,?,?,"
                "'ingestion_cpu')",
                (
                    run.run_id,
                    json.dumps(base_context),
                    json.dumps(base_context),
                    utc_now(),
                ),
            )
            connection.execute(
                "UPDATE runs SET status='waiting_human',output_json=? WHERE id=?",
                (json.dumps(base_context), run.run_id),
            )
            connection.commit()

        response = self.client.post(
            f"/api/runs/{run.run_id}/revalidate-authoring-submission"
        )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["node_id"], "validate_reference_quality")
        self.assertEqual(payload["attempt"], 2)
        self.assertEqual(payload["reused_node_count"], 4)
        with self.app.state.databases.state.connect() as connection:
            run_status = connection.execute(
                "SELECT status FROM runs WHERE id=?", (run.run_id,)
            ).fetchone()["status"]
            old_human_status = connection.execute(
                "SELECT status FROM node_runs WHERE id='old-human'"
            ).fetchone()["status"]
            queued = connection.execute(
                "SELECT input_json FROM node_runs WHERE id=?",
                (payload["node_run_id"],),
            ).fetchone()
            reused = connection.execute(
                "SELECT COUNT(*) AS count FROM event_log WHERE run_id=? "
                "AND event_type='node.checkpoint_reused'",
                (run.run_id,),
            ).fetchone()["count"]
        queued_context = json.loads(queued["input_json"])
        self.assertEqual(run_status, "queued")
        self.assertEqual(old_human_status, "completed")
        self.assertEqual(reused, 4)
        self.assertNotEqual(
            queued_context["_join_generations"]["join_submission_validations"],
            "old-submission-generation",
        )
        self.assertNotEqual(
            queued_context["_join_generations"]["audit_final_findings"],
            "old-finding-generation",
        )

        join_error = json.dumps(
            {
                "type": "FukamiruError",
                "message": (
                    "Join context conflict at "
                    "validate_footnoted_references.reference_quality_gate."
                    "rendered_author_citation_count"
                ),
            }
        )
        with self.app.state.databases.state.connect() as connection:
            connection.execute(
                "UPDATE node_runs SET status='failed',error_json=?,completed_at=? "
                "WHERE id=?",
                (join_error, utc_now(), payload["node_run_id"]),
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,completed_at=? WHERE id=?",
                (join_error, utc_now(), run.run_id),
            )
            connection.commit()

        recovery = self.client.post(
            f"/api/runs/{run.run_id}/revalidate-authoring-submission"
        )

        self.assertEqual(recovery.status_code, 200, recovery.text)
        self.assertEqual(recovery.json()["attempt"], 3)
        with self.app.state.databases.state.connect() as connection:
            latest_reused = connection.execute(
                "SELECT output_json FROM node_runs WHERE run_id=? "
                "AND node_id='validate_footnoted_references' "
                "ORDER BY attempt DESC LIMIT 1",
                (run.run_id,),
            ).fetchone()
        self.assertNotIn(
            "reference_quality_gate", json.loads(latest_reused["output_json"])
        )

    def test_authoring_pending_proposal_requires_explicit_confirmation(self) -> None:
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="Authoring proposal API",
                root_path=str(Path(self.temp.name) / "authoring-proposal-project"),
            )
        run = self.client.post(
            "/api/runs",
            json={
                "workflow_name": "authoring",
                "project_id": project_id,
                "inputs": {"title": "Source note", "note_text": "Source note content."},
            },
        )
        self.assertEqual(run.status_code, 201, run.text)
        run_id = run.json()["run_id"]
        source_html = (
            '<article id="source-article" lang="en"><section id="source-section">'
            '<h2 id="source-heading">Source</h2><p id="source-paragraph">Source note.</p>'
            "</section></article>"
        )
        proposed_html = (
            '<article id="paper-article" lang="en"><section id="paper-section">'
            '<h2 id="paper-heading">Draft</h2><p id="paper-paragraph">Proposed prose.</p>'
            "</section></article>"
        )
        with self.app.state.databases.state.connect() as connection:
            revisions = RevisionService(connection)
            source_document_id = revisions.create_document(
                project_id=project_id,
                kind="note",
                title="Source note",
                document_id="api-authoring-source",
            )
            source_revision = revisions.commit_revision(
                document_id=source_document_id,
                canonical_html=source_html,
                editor_type="human",
                operation="create",
            )
            target_document_id = revisions.create_document(
                project_id=project_id,
                kind="paper",
                title="Generated paper",
                document_id="api-authoring-paper",
            )
        repository = AuthoringRepository(self.app.state.databases.state)
        repository.create_plan(
            AuthoringPlan(
                id="api-authoring-plan",
                document_id=target_document_id,
                source_revision_id=source_revision.revision_id,
                revision=1,
                research_question=ResearchQuestion(text="What does the note establish?"),
            )
        )
        repository.create_pending_proposal(
            proposal_id="api-authoring-proposal",
            run_id=run_id,
            project_id=project_id,
            source_document_id=source_document_id,
            source_revision_id=source_revision.revision_id,
            target_document_id=target_document_id,
            plan_id="api-authoring-plan",
            plan_revision=1,
            title="Generated paper",
            proposed_html=proposed_html,
        )

        proposal = self.client.get("/api/authoring/proposals/api-authoring-proposal")
        self.assertEqual(proposal.status_code, 200, proposal.text)
        self.assertEqual(proposal.json()["status"], "pending")
        self.assertNotIn("proposed_html", proposal.json())
        draft = self.client.get(proposal.json()["draft_html_url"])
        self.assertEqual(draft.status_code, 200, draft.text)
        self.assertIn(f'<main class="document-output">{proposed_html}</main>', draft.text)
        self.assertIn("script-src 'none'", draft.headers["content-security-policy"])

        confirmed = self.client.post(
            "/api/authoring/proposals/api-authoring-proposal/confirm",
            json={"expected_sha256": proposal.json()["proposed_sha256"]},
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual(confirmed.json()["status"], "confirmed")
        history = self.client.get(f"/api/documents/{target_document_id}/revisions")
        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual(len(history.json()), 1)

    def test_restart_v31_from_draft_clears_downstream_and_reuses_same_run(self) -> None:
        with self.app.state.databases.state.connect() as connection:
            created = RunRepository(connection).create(
                workflow_name="manuscript_generator",
                workflow_version="31",
                entry_node="prepare_note",
                project_id=None,
                inputs={"source_note": "preserved upstream input"},
                resource_lane="codex_remote",
            )
            upstream = connection.execute(
                "SELECT id FROM node_runs WHERE run_id=? AND node_id='prepare_note'",
                (created.run_id,),
            ).fetchone()["id"]
            connection.execute(
                "UPDATE node_runs SET status='completed',completed_at=? WHERE id=?",
                (utc_now(), upstream),
            )
            handoff = {
                "plan_id": "plan-v31",
                "plan_revision": 1,
                "research_problem": {"research_question": "問い"},
                "section_contracts": [{"section_id": "sec-conclusion"}],
            }
            packed_handoff = json.dumps(
                PayloadStore(connection).pack(handoff), ensure_ascii=False
            )
            events = EventRepository(connection)
            first_draft_event_id = None
            for attempt, status in ((1, "failed"), (2, "completed")):
                node_run_id = f"draft-old-{attempt}"
                connection.execute(
                    "INSERT INTO node_runs("
                    "id,run_id,node_id,attempt,status,input_json,resource_lane,completed_at"
                    ") VALUES(?,?,'draft_rewrite',?,?,?,'codex_remote',?)",
                    (
                        node_run_id,
                        created.run_id,
                        attempt,
                        status,
                        packed_handoff,
                        utc_now(),
                    ),
                )
                event = events.append(
                    created.run_id,
                    "node.queued",
                    object_type="node_run",
                    object_id=node_run_id,
                    payload={"node_id": "draft_rewrite", "attempt": attempt},
                )
                if attempt == 1:
                    first_draft_event_id = event.event_id
            downstream_id = "review-old-1"
            connection.execute(
                "INSERT INTO node_runs("
                "id,run_id,node_id,attempt,status,input_json,resource_lane,completed_at"
                ") VALUES(?,?,'review_japanese_sections',1,'failed',?,'codex_remote',?)",
                (downstream_id, created.run_id, packed_handoff, utc_now()),
            )
            events.append(
                created.run_id,
                "node.failed",
                object_type="node_run",
                object_id=downstream_id,
                payload={"node_id": "review_japanese_sections"},
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,output_json=?,completed_at=? "
                "WHERE id=?",
                ('{"type":"FukamiruError"}', '{"stale":true}', utc_now(), created.run_id),
            )
            connection.execute(
                "INSERT INTO authoring_evidence_uses("
                "run_id,evidence_use_id,section_id,sentence_id,claim_id,payload_json,created_at"
                ") VALUES(?,?,?,?,?,?,?)",
                (
                    created.run_id,
                    "use-upstream",
                    "sec-introduction",
                    "s1",
                    "c1",
                    "{}",
                    "2000-01-01T00:00:00Z",
                ),
            )
            connection.execute(
                "INSERT INTO authoring_evidence_uses("
                "run_id,evidence_use_id,section_id,sentence_id,claim_id,payload_json,created_at"
                ") VALUES(?,?,?,?,?,?,?)",
                (
                    created.run_id,
                    "use-downstream",
                    "sec-conclusion",
                    "s2",
                    "c2",
                    "{}",
                    "9999-01-01T00:00:00Z",
                ),
            )
            connection.execute(
                "INSERT INTO manuscript_unit_checkpoints VALUES(?,?,?,?,?)",
                (
                    created.run_id,
                    "authoring-batch-v30:classify_propositions",
                    "upstream-signature",
                    "upstream-unit",
                    "{}",
                ),
            )
            connection.execute(
                "INSERT INTO manuscript_unit_checkpoints VALUES(?,?,?,?,?)",
                (
                    created.run_id,
                    "authoring-batch-v30:draft_body_v31",
                    "draft-signature",
                    "draft-unit",
                    "{}",
                ),
            )
            connection.execute(
                "INSERT INTO human_reviews("
                "id,run_id,node_run_id,object_type,object_id,question,options_json,status,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    "review-downstream",
                    created.run_id,
                    downstream_id,
                    "section",
                    "sec-conclusion",
                    "Review?",
                    "[]",
                    "open",
                    "9999-01-01T00:00:00Z",
                ),
            )

        response = self.client.post(
            f"/api/runs/{created.run_id}/restart-v31-from-draft"
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["run_id"], created.run_id)
        self.assertEqual(payload["restart_node_id"], "draft_rewrite")
        self.assertEqual(payload["status"], "queued")
        with self.app.state.databases.state.connect() as connection:
            run = connection.execute(
                "SELECT status,error_json,output_json,completed_at FROM runs WHERE id=?",
                (created.run_id,),
            ).fetchone()
            self.assertEqual(run["status"], "queued")
            self.assertIsNone(run["error_json"])
            self.assertIsNone(run["output_json"])
            self.assertIsNone(run["completed_at"])
            nodes = connection.execute(
                "SELECT node_id,attempt,status,input_json FROM node_runs "
                "WHERE run_id=? ORDER BY node_id,attempt",
                (created.run_id,),
            ).fetchall()
            draft_nodes = [row for row in nodes if row["node_id"] == "draft_rewrite"]
            self.assertEqual(len(draft_nodes), 1)
            self.assertEqual(draft_nodes[0]["attempt"], 1)
            self.assertEqual(draft_nodes[0]["status"], "queued")
            self.assertEqual(
                PayloadStore(connection).unpack(json.loads(draft_nodes[0]["input_json"])),
                handoff,
            )
            self.assertTrue(any(row["id"] == upstream for row in connection.execute(
                "SELECT id FROM node_runs WHERE run_id=?", (created.run_id,)
            )))
            self.assertFalse(any(row["node_id"] == "review_japanese_sections" for row in nodes))
            evidence_uses = connection.execute(
                "SELECT evidence_use_id FROM authoring_evidence_uses WHERE run_id=?",
                (created.run_id,),
            ).fetchall()
            self.assertEqual([row["evidence_use_id"] for row in evidence_uses], ["use-upstream"])
            checkpoint_phases = connection.execute(
                "SELECT phase FROM manuscript_unit_checkpoints WHERE run_id=?",
                (created.run_id,),
            ).fetchall()
            self.assertEqual(
                [row["phase"] for row in checkpoint_phases],
                ["authoring-batch-v30:classify_propositions"],
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT id FROM human_reviews WHERE id='review-downstream'"
                ).fetchone()
            )
            stale_events = connection.execute(
                "SELECT event_id FROM event_log WHERE run_id=? AND event_id>=? "
                "AND event_type NOT IN ('run.authoring_restarted_from_node','node.queued')",
                (created.run_id, first_draft_event_id),
            ).fetchall()
            self.assertEqual(stale_events, [])

    def test_restart_v31_from_entry_cleans_run_records_and_reuses_original_inputs(self) -> None:
        original_inputs = {
            "title": "同じ原稿",
            "note_text": "保存済みの元入力",
            "language": "ja",
            "output_language": "ja",
            "use_rag": True,
        }
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="V31 immutable restart project",
                root_path=str(Path(self.temp.name) / "v31-immutable-restart"),
            )
            source_document_id = RevisionService(connection).create_document(
                project_id=project_id,
                kind="note",
                title="Immutable plan source",
            )
            source_revision = RevisionService(connection).commit_revision(
                document_id=source_document_id,
                canonical_html=(
                    '<article id="immutable-article" lang="ja">'
                    '<section id="immutable-section">'
                    '<h2 id="immutable-heading">ノート</h2>'
                    '<p id="immutable-paragraph">保存する計画。</p>'
                    "</section></article>"
                ),
                editor_type="human",
                operation="create",
            )
            created = RunRepository(connection).create(
                workflow_name="manuscript_generator",
                workflow_version="31",
                entry_node="prepare_note",
                project_id=project_id,
                inputs=original_inputs,
                resource_lane="ingestion_cpu",
            )
            entry_id = connection.execute(
                "SELECT id FROM node_runs WHERE run_id=? AND node_id='prepare_note'",
                (created.run_id,),
            ).fetchone()["id"]
            connection.execute(
                "UPDATE node_runs SET status='completed',completed_at=? WHERE id=?",
                (utc_now(), entry_id),
            )
            waiting_id = "graph-waiting-old"
            immutable_producer_id = "immutable-plan-producer"
            connection.execute(
                "INSERT INTO node_runs("
                "id,run_id,node_id,attempt,status,input_json,resource_lane,completed_at"
                ") VALUES(?,?,'decompose_memo',1,'completed','{}','codex_remote',?)",
                (immutable_producer_id, created.run_id, utc_now()),
            )
            connection.execute(
                "INSERT INTO node_runs("
                "id,run_id,node_id,attempt,status,input_json,resource_lane"
                ") VALUES(?,?,'build_argument_graph',1,'waiting_human','{}','codex_remote')",
                (waiting_id, created.run_id),
            )
            connection.execute(
                "UPDATE runs SET status='waiting_human',started_at=?,output_json=? "
                "WHERE id=?",
                (utc_now(), '{"old":true}', created.run_id),
            )
            connection.execute(
                "INSERT INTO human_reviews("
                "id,run_id,node_run_id,object_type,object_id,question,options_json,status,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    "graph-review-old", created.run_id, waiting_id,
                    "authoring_graph_invalid_reference", "batch-6", "Review?", "[]",
                    "open", utc_now(),
                ),
            )
            connection.execute(
                "INSERT INTO authoring_evidence_uses("
                "run_id,evidence_use_id,section_id,sentence_id,claim_id,payload_json,created_at"
                ") VALUES(?,?,?,?,?,?,?)",
                (
                    created.run_id, "old-evidence-use", "sec", "s1", "c1", "{}", utc_now(),
                ),
            )
            connection.execute(
                "INSERT INTO manuscript_unit_checkpoints VALUES(?,?,?,?,?)",
                (created.run_id, "draft-phase", "old-signature", "old-unit", "{}"),
            )
            EventRepository(connection).append(
                created.run_id,
                "review.requested",
                object_type="node_run",
                object_id=waiting_id,
                payload={"node_id": "build_argument_graph"},
            )

        AuthoringRepository(self.app.state.databases.state).create_plan(
            AuthoringPlan(
                id="immutable-plan",
                document_id=source_document_id,
                source_revision_id=source_revision.revision_id,
                revision=1,
                research_question=ResearchQuestion(text="ノートは何を述べるか。"),
            ),
            producer_node_run_id=immutable_producer_id,
        )

        response = self.client.post(
            f"/api/runs/{created.run_id}/restart-v31-from-entry"
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["run_id"], created.run_id)
        self.assertEqual(payload["restart_node_id"], "prepare_note")
        self.assertEqual(payload["status"], "queued")
        self.assertEqual(payload["deleted_node_run_count"], 2)
        self.assertEqual(payload["retained_immutable_node_run_count"], 1)
        with self.app.state.databases.state.connect() as connection:
            run = connection.execute(
                "SELECT status,input_json,output_json,error_json,started_at,completed_at "
                "FROM runs WHERE id=?",
                (created.run_id,),
            ).fetchone()
            self.assertEqual(run["status"], "queued")
            self.assertEqual(json.loads(run["input_json"]), original_inputs)
            self.assertIsNone(run["output_json"])
            self.assertIsNone(run["error_json"])
            self.assertIsNone(run["started_at"])
            self.assertIsNone(run["completed_at"])
            nodes = connection.execute(
                "SELECT id,node_id,attempt,status,input_json FROM node_runs WHERE run_id=?",
                (created.run_id,),
            ).fetchall()
            self.assertEqual(len(nodes), 2)
            entry = next(row for row in nodes if row["node_id"] == "prepare_note")
            self.assertEqual(entry["attempt"], 1)
            self.assertEqual(entry["status"], "queued")
            self.assertEqual(json.loads(entry["input_json"]), original_inputs)
            self.assertTrue(any(
                row["id"] == immutable_producer_id
                and row["node_id"] == "decompose_memo"
                and row["status"] == "completed"
                for row in nodes
            ))
            self.assertFalse(any(row["node_id"] == "build_argument_graph" for row in nodes))
            self.assertEqual(
                connection.execute(
                    "SELECT producer_node_run_id FROM authoring_plan_revisions "
                    "WHERE plan_id='immutable-plan' AND revision=1"
                ).fetchone()[0],
                immutable_producer_id,
            )
            for table in (
                "human_reviews",
                "authoring_evidence_uses",
                "manuscript_unit_checkpoints",
            ):
                self.assertEqual(
                    connection.execute(
                        f"SELECT count(*) FROM {table} WHERE run_id=?", (created.run_id,)
                    ).fetchone()[0],
                    0,
                    table,
                )
            event_types = [row[0] for row in connection.execute(
                "SELECT event_type FROM event_log WHERE run_id=? ORDER BY event_id",
                (created.run_id,),
            )]
            self.assertEqual(
                event_types,
                ["run.authoring_restarted_from_entry", "node.queued"],
            )

    def test_restart_v31_from_entry_cleans_failed_run_and_queues_attempt_one(self) -> None:
        original_inputs = {"title": "再開", "note_text": "元入力", "output_language": "ja"}
        with self.app.state.databases.state.connect() as connection:
            created = RunRepository(connection).create(
                workflow_name="manuscript_generator",
                workflow_version="31",
                entry_node="prepare_note",
                project_id=None,
                inputs=original_inputs,
                resource_lane="ingestion_cpu",
            )
            entry = connection.execute(
                "SELECT id FROM node_runs WHERE run_id=? AND node_id='prepare_note'",
                (created.run_id,),
            ).fetchone()
            connection.execute(
                "UPDATE node_runs SET status='failed',completed_at=?,error_json=? WHERE id=?",
                (utc_now(), '{"message":"prior failure"}', entry["id"]),
            )
            connection.execute(
                "UPDATE runs SET status='failed',error_json=?,output_json=? WHERE id=?",
                ('{"message":"prior failure"}', '{"stale":true}', created.run_id),
            )

        response = self.client.post(
            f"/api/runs/{created.run_id}/restart-v31-from-entry"
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["run_id"], created.run_id)
        self.assertEqual(payload["restart_node_id"], "prepare_note")
        self.assertEqual(payload["attempt"], 1)
        self.assertEqual(payload["status"], "queued")
        self.assertEqual(payload["deleted_node_run_count"], 1)
        with self.app.state.databases.state.connect() as connection:
            run = connection.execute(
                "SELECT status,input_json,output_json,error_json FROM runs WHERE id=?",
                (created.run_id,),
            ).fetchone()
            node = connection.execute(
                "SELECT node_id,attempt,status,input_json FROM node_runs WHERE run_id=?",
                (created.run_id,),
            ).fetchone()
            self.assertEqual(run["status"], "queued")
            self.assertEqual(json.loads(run["input_json"]), original_inputs)
            self.assertIsNone(run["output_json"])
            self.assertIsNone(run["error_json"])
            self.assertEqual(node["node_id"], "prepare_note")
            self.assertEqual(node["attempt"], 1)
            self.assertEqual(node["status"], "queued")
            self.assertEqual(json.loads(node["input_json"]), original_inputs)

    def _create_authoring_review_fixture(self) -> tuple[str, Path]:
        with self.app.state.databases.state.connect() as connection:
            project_id = RevisionService(connection).register_project(
                name="Authoring review project",
                root_path=str(Path(self.temp.name) / "authoring-review-project"),
            )
        created = self.client.post(
            "/api/runs",
            json={
                "workflow_name": "authoring",
                "project_id": project_id,
                "inputs": {"title": "提出原稿", "note_text": "Source note content."},
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        run_id = created.json()["run_id"]
        proposed_html = (
            '<article id="paper-article" lang="ja"><section id="paper-section">'
            '<h2 id="paper-heading">提出原稿</h2><p id="paper-paragraph">本文。</p>'
            "</section></article>"
        )
        proposal_artifact_path = Path(self.temp.name) / "authoring.proposal.html"
        proposal_artifact_path.write_text(proposed_html, encoding="utf-8")
        artifact_path = Path(self.temp.name) / "authoring.submission.html"
        artifact_path.write_text(proposed_html, encoding="utf-8")
        with self.app.state.databases.state.connect() as connection:
            revisions = RevisionService(connection)
            source_document_id = revisions.create_document(
                project_id=project_id,
                kind="note",
                title="Source note",
                document_id="review-authoring-source",
            )
            source_revision = revisions.commit_revision(
                document_id=source_document_id,
                canonical_html=(
                    '<article id="source-article" lang="en"><section id="source-section">'
                    '<h2 id="source-heading">Source</h2><p id="source-paragraph">Note.</p>'
                    "</section></article>"
                ),
                editor_type="human",
                operation="create",
            )
            target_document_id = revisions.create_document(
                project_id=project_id,
                kind="paper",
                title="提出原稿",
                document_id="review-authoring-paper",
            )
        repository = AuthoringRepository(self.app.state.databases.state)
        repository.create_plan(
            AuthoringPlan(
                id="review-authoring-plan",
                document_id=target_document_id,
                source_revision_id=source_revision.revision_id,
                revision=1,
                research_question=ResearchQuestion(text="What does the note establish?"),
            )
        )
        proposal = repository.create_pending_proposal(
            proposal_id="review-authoring-proposal",
            run_id=run_id,
            project_id=project_id,
            source_document_id=source_document_id,
            source_revision_id=source_revision.revision_id,
            target_document_id=target_document_id,
            plan_id="review-authoring-plan",
            plan_revision=1,
            title="提出原稿",
            proposed_html=proposed_html,
            artifact_path=str(proposal_artifact_path),
            quality_status="ready",
        )
        with self.app.state.databases.state.connect() as connection:
            node = connection.execute(
                "SELECT id FROM node_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            context = {
                "authoring_proposal_id": proposal["id"],
                "claims": [{"id": "c1", "text": "限定した結論"}],
                "argument_nodes": [{
                    "id": "w1", "claim_id": "c1", "node_type": "warrant",
                    "text": "資料から結論への推論",
                }],
                "authoring_proposal_sha256": proposal["proposed_sha256"],
                "authoring_proposal_status": "pending",
                "final_artifact_id": "review-authoring-artifact",
                "final_path": str(artifact_path),
                "authoring_schema_version": "31.1",
                "authoring_result_status": "completed_with_review",
                "review_report_artifact_id": "review-authoring-report",
                "review_report_path": str(artifact_path.with_suffix(".review.json")),
                "human_review_items": [{
                    "review_item_id": "HRI-S005-P02-S03",
                    "section_id": "S005",
                    "sentence_id": "S005-P02-S03",
                    "claim_id": "C-S005-004",
                    "category": "original_source_check",
                    "reason": "RAG断片を越える主張には原典確認が必要",
                    "source_ids": ["SRC-014"],
                    "recommended_check": "原典の該当箇所と主張範囲を照合する",
                    "blocks_render": False,
                    "status": "open",
                }],
            }
            connection.execute(
                "UPDATE node_runs SET node_id='human_approval', status='waiting_human', "
                "output_json=? WHERE id=?",
                (json.dumps(context), node["id"]),
            )
            connection.execute(
                "UPDATE runs SET status='waiting_human', output_json=? WHERE id=?",
                (json.dumps(context), run_id),
            )
            connection.execute(
                "INSERT INTO run_artifacts(id,run_id,node_run_id,artifact_type,path,"
                "media_type,title,metadata_json,created_at) VALUES "
                "('review-authoring-artifact',?,?, 'authoring_submission',?,"
                "'text/html','提出原稿','{}',?)",
                (run_id, node["id"], str(artifact_path), utc_now()),
            )
            connection.execute(
                "INSERT INTO human_reviews(id,run_id,node_run_id,object_type,object_id,"
                "question,options_json,status,created_at) VALUES "
                "('review-authoring-review',?,?,'authoring_submission',"
                "'review-authoring-artifact','提出原稿を確認してください',"
                "'[\"approved\",\"rejected\"]','open',?)",
                (run_id, node["id"], utc_now()),
            )
            connection.commit()

        return target_document_id, artifact_path

    def test_v32_verified_book_correction_replays_footnotes_without_changing_claim_support(self):
        from fukamiru.storage.payloads import PayloadStore

        error = {"type": "FukamiruError", "message":
                 "Footnote bibliography metadata requires review: author_unconfirmed"}
        reference = {"reference_id": "ref-one", "provider": "knowledge_base",
                     "provider_record_id": "asset-one", "record_type": "document",
                     "title": "検証太郎_検証の本 (検証出版, 2016)", "authors": [],
                     "bibliography_people": []}
        original = {"reference_entries": [reference], "note_text": "著者の原メモ。",
                    "proposed_html": '<article id="paper"><p id="p1" data-protected="true">'
                                     '著者の固定本文。</p></article>',
                    "evidence_context": [{"id": "ev-one", "verification_status": "unverified"}],
                    "argument_nodes": [{"id": "node-one", "source_ids": ["ev-one"]}]}
        saved_input = json.dumps(original)
        packet = {"reference_id": "ref-one", "title": "検証の本", "authors": ["検証太郎"],
                  "publisher": "検証出版", "year": "2016", "isbn": "9780306406157",
                  "publication_language": "ja", "source_kind": "publisher",
                  "source_url": "https://publisher.example/book"}
        with self.app.state.databases.state.connect() as connection:
            created = RunRepository(connection).create(
                workflow_name="manuscript_generator", workflow_version="32",
                entry_node="reconcile_candidate_context_after_proofreading",
                inputs={"upstream": "retained"}, project_id=None,
            )
            connection.execute("UPDATE node_runs SET status='completed',output_json=? "
                               "WHERE run_id=?", ('{"upstream":"retained"}', created.run_id))
            connection.execute("INSERT INTO node_runs(id,run_id,node_id,attempt,status,"
                "input_json,error_json) VALUES('book-metadata-previous',?,"
                "'render_evidence_footnotes',1,'failed',?,?)",
                (created.run_id, saved_input, json.dumps(error)))
            connection.execute("INSERT INTO node_runs(id,run_id,node_id,attempt,status,"
                "input_json,error_json) VALUES('book-metadata-failed',?,"
                "'render_evidence_footnotes',2,'failed',?,?)",
                (created.run_id, saved_input, json.dumps(error)))
            connection.execute("UPDATE runs SET status='failed',error_json=? WHERE id=?",
                               (json.dumps(error), created.run_id))
            connection.commit()
        url = f"/api/runs/{created.run_id}/replay-failed-authoring-node"
        self.assertEqual(self.client.post(url).status_code, 409)
        self.assertEqual(self.client.post(url, json={**packet, "title": "別の本"}).status_code, 409)
        with self.app.state.databases.state.connect() as connection:
            row = connection.execute("SELECT input_json,status FROM node_runs WHERE id=?",
                                     ("book-metadata-failed",)).fetchone()
            self.assertEqual(tuple(row), (saved_input, "failed"))
            connection.execute("UPDATE node_runs SET input_json=? WHERE id=?",
                (json.dumps({**original, "note_text": "別の入力"}), "book-metadata-previous"))
            connection.commit()
        self.assertEqual(self.client.post(url, json=packet).status_code, 409)
        with self.app.state.databases.state.connect() as connection:
            connection.execute("UPDATE node_runs SET input_json=? WHERE id=?",
                               (saved_input, "book-metadata-previous"))
            connection.commit()
        response = self.client.post(url, json=packet)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["manual_replay_count"], 1)
        with self.app.state.databases.state.connect() as connection:
            row = connection.execute("SELECT input_json,status FROM node_runs WHERE id=?",
                                     ("book-metadata-failed",)).fetchone()
            updated = PayloadStore(connection).unpack(json.loads(row[0]))
            self.assertEqual(row[1], "queued")
            for key in ("note_text", "proposed_html", "evidence_context", "argument_nodes"):
                self.assertEqual(updated[key], original[key])
            self.assertEqual(updated["reference_entries"][0]["authors"], ["検証太郎"])
            self.assertEqual(updated["reference_entries"][0]["provider_record_id"], "asset-one")
            event = json.loads(connection.execute("SELECT payload_json FROM event_log "
                "WHERE run_id=? AND event_type='run.failed_authoring_node_replayed'",
                (created.run_id,)).fetchone()[0])
            self.assertEqual(event["verified_bibliography_correction"]["previous_node_input_json"],
                             saved_input)
            self.assertEqual(event["verified_bibliography_correction"]["source_url"],
                             packet["source_url"])
            self.assertEqual(updated["verified_bibliography_metadata_corrections"][0][
                "previous_reference_metadata"]["title"], reference["title"])
            upstream = connection.execute("SELECT output_json,status FROM node_runs "
                "WHERE run_id=? AND node_id='reconcile_candidate_context_after_proofreading'",
                (created.run_id,)).fetchone()
            self.assertEqual(tuple(upstream), ('{"upstream":"retained"}', "completed"))
            previous = connection.execute("SELECT input_json,error_json,status FROM node_runs "
                "WHERE id='book-metadata-previous'").fetchone()
            self.assertEqual(tuple(previous), (saved_input, json.dumps(error), "failed"))
            connection.execute("UPDATE node_runs SET status='failed',error_json=? WHERE id=?",
                               (json.dumps(error), "book-metadata-failed"))
            connection.execute("UPDATE runs SET status='failed' WHERE id=?", (created.run_id,))
            connection.commit()
        self.assertEqual(self.client.post(url, json=packet).status_code, 409)

    def test_reader_brief_excerpt_failure_replays_once_with_unchanged_checkpoints(self) -> None:
        error = {"type": "NonRetryableFukamiruError", "message": (
            "V31 reader brief marginal inference excerpts do not match"
        )}
        original_input = json.dumps({
            "protected_html": "<p>著者の原文。</p>",
            "human_question_feedback": {"answers": [{"answer": "資料は手元にない。"}]},
        })
        with self.app.state.databases.state.connect() as connection:
            created = RunRepository(connection).create(
                workflow_name="manuscript_generator", workflow_version="32",
                entry_node="plan_semantic_paragraphs", inputs={"original": "preserved"},
                project_id=None,
            )
            connection.execute("UPDATE node_runs SET status='completed',output_json=? "
                               "WHERE run_id=?", ('{"plan":"retained"}', created.run_id))
            connection.execute(
                "INSERT INTO node_runs(id,run_id,node_id,attempt,status,input_json,error_json) "
                "VALUES('old-excerpt-history',?,'render_evidence_footnotes',1,'failed','{}',?)",
                (created.run_id, '{"message":"old bibliography failure"}'),
            )
            connection.execute(
                "INSERT INTO node_runs(id,run_id,node_id,attempt,status,input_json,output_json) "
                "VALUES('completed-excerpt-history',?,'render_evidence_footnotes',2,"
                "'completed','{}',?)",
                (created.run_id, '{"footnotes":"retained"}'),
            )
            connection.execute(
                "INSERT INTO node_runs(id,run_id,node_id,attempt,status,input_json,error_json) "
                "VALUES('reader-excerpt-failure',?,'draft_rewrite',2,'failed',?,?)",
                (created.run_id, original_input, json.dumps(error)),
            )
            connection.execute("UPDATE runs SET status='failed',error_json=? WHERE id=?",
                               (json.dumps(error), created.run_id))
            connection.commit()
        url = f"/api/runs/{created.run_id}/replay-failed-authoring-node"
        self.assertEqual(self.client.post(url).status_code, 409)  # No saved audit to repair.
        with self.app.state.databases.state.connect() as connection:
            connection.execute(
                "INSERT INTO llm_result_checkpoints(id,run_id,node_run_id,node_id,stage,"
                "canonical_stage,operation_name,request_fingerprint_sha256,full_prompt_sha256,"
                "cache_family_sha256,result_json,validation_json,status,created_at,updated_at) "
                "VALUES('excerpt-checkpoint',?,'reader-excerpt-failure','draft_rewrite',"
                "'audit_reader_brief_v31','audit_reader_brief_v31','audit',?,?,?,?,?,"
                "'accepted','2026-11-09','2026-11-09')",
                (created.run_id, 'a' * 64, 'b' * 64, 'c' * 64, '{"sections":[]}', '{}'),
            )
            connection.execute("UPDATE node_runs SET status='cancelled' "
                               "WHERE id='completed-excerpt-history'")
            connection.commit()
        self.assertEqual(self.client.post(url).status_code, 409)  # Unresolved other failure.
        with self.app.state.databases.state.connect() as connection:
            connection.execute("UPDATE node_runs SET status='completed' "
                               "WHERE id='completed-excerpt-history'")
            before = [dict(row) for row in connection.execute(
                "SELECT * FROM node_runs WHERE run_id=? AND id!='reader-excerpt-failure'",
                (created.run_id,),
            )]
            checkpoint = dict(connection.execute(
                "SELECT * FROM llm_result_checkpoints WHERE id='excerpt-checkpoint'"
            ).fetchone())
            connection.commit()
        response = self.client.post(url)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["node_id"], "draft_rewrite")
        with self.app.state.databases.state.connect() as connection:
            node = connection.execute("SELECT * FROM node_runs WHERE id='reader-excerpt-failure'")
            node = node.fetchone()
            self.assertEqual(node["input_json"], original_input)
            self.assertEqual(node["attempt"], 2)
            self.assertEqual(node["status"], "queued")
            self.assertEqual([dict(row) for row in connection.execute(
                "SELECT * FROM node_runs WHERE run_id=? AND id!='reader-excerpt-failure'",
                (created.run_id,),
            )], before)
            self.assertEqual(dict(connection.execute(
                "SELECT * FROM llm_result_checkpoints WHERE id='excerpt-checkpoint'"
            ).fetchone()), checkpoint)
            event = connection.execute("SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed'", (created.run_id,)).fetchone()
            self.assertEqual(json.loads(event[0])["previous_node_error"], error)
            self.assertEqual(json.loads(event[0])["policy"],
                             "authoring-v31-reader-brief-excerpt-recovery-v1")
            connection.execute("UPDATE node_runs SET status='failed',error_json=? "
                               "WHERE id='reader-excerpt-failure'", (json.dumps(error),))
            connection.execute("UPDATE runs SET status='failed',error_json=? WHERE id=?",
                               (json.dumps(error), created.run_id))
            connection.commit()
        self.assertEqual(self.client.post(url).status_code, 409)

    def test_initial_name_sentence_replay_requires_recorded_truncation_and_is_once(self) -> None:
        original_input = json.dumps({
            "protected_html": "<p>原文。</p>",
            "reference_entries": [{"verification_status": "unverified"}],
            "human_question_feedback": {"answers": [{"answer": "資料は手元にない。"}]},
        })

        def failed_run(reason: str, *, mixed: bool = False):
            message = (
                "V31 role draft JSON handoff violation: section s1 scholarship evidence E1 "
                "has no semantically confirmed attribution predicate (classification=unclear)"
            )
            if mixed:
                message += "; section s1 has invalid output schema"
            error = {"type": "NonRetryableFukamiruError", "message": message}
            with self.app.state.databases.state.connect() as connection:
                created = RunRepository(connection).create(
                    workflow_name="manuscript_generator", workflow_version="32",
                    entry_node="draft_rewrite", inputs=json.loads(original_input), project_id=None,
                )
                node_id = connection.execute("SELECT id FROM node_runs WHERE run_id=?",
                                             (created.run_id,)).fetchone()[0]
                connection.execute("UPDATE node_runs SET status='failed',error_json=? WHERE id=?",
                                   (json.dumps(error), node_id))
                connection.execute("UPDATE runs SET status='failed',error_json=? WHERE id=?",
                                   (json.dumps(error), created.run_id))
                result = {"judgments": [{"candidate_id": "initial-candidate",
                                        "classification": "unclear", "reason": reason}]}
                connection.execute(
                    "INSERT INTO llm_result_checkpoints(id,run_id,node_run_id,node_id,stage,"
                    "canonical_stage,operation_name,request_fingerprint_sha256,full_prompt_sha256,"
                    "cache_family_sha256,result_json,validation_json,status,created_at,updated_at) "
                    "VALUES(?,?,?,'draft_rewrite','scholarship_attribution_judgment_v31',"
                    "'scholarship_attribution_judgment_v31','audit',?,?,?,?,?,'accepted',?,?)",
                    (created.run_id + '-audit', created.run_id, node_id, 'a' * 64, 'b' * 64,
                     'c' * 64, json.dumps(result), '{}', utc_now(), utc_now()),
                )
                EventRepository(connection).append(
                    created.run_id, "run.failed_authoring_node_replayed", object_type="node_run",
                    object_id=node_id, payload={
                        "policy": "authoring-v31-reader-brief-excerpt-recovery-v1",
                    },
                )
                connection.commit()
                before = connection.execute("SELECT input_json FROM node_runs WHERE id=?",
                                            (node_id,)).fetchone()[0]
            return created.run_id, node_id, error, before, result

        reason = "文が「アレクサ・J.」で途切れており、述語が完結していない。"
        run_id, node_id, error, before, result = failed_run(reason)
        url = f"/api/runs/{run_id}/replay-failed-authoring-node"
        response = self.client.post(url)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["manual_replay_count"], 2)
        with self.app.state.databases.state.connect() as connection:
            self.assertEqual(connection.execute("SELECT input_json FROM node_runs WHERE id=?",
                             (node_id,)).fetchone()[0], before)
            saved = connection.execute("SELECT result_json FROM llm_result_checkpoints WHERE id=?",
                                       (run_id + '-audit',)).fetchone()[0]
            self.assertEqual(json.loads(saved), result)
            event = connection.execute("SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed' ORDER BY rowid DESC LIMIT 1",
                (run_id,)).fetchone()[0]
            self.assertEqual(json.loads(event)["policy"],
                             "authoring-v31-initial-name-sentence-recovery-v1")
            self.assertEqual(json.loads(event)["previous_node_error"], error)
            connection.execute("UPDATE node_runs SET status='failed',error_json=? WHERE id=?",
                               (json.dumps(error), node_id))
            connection.execute("UPDATE runs SET status='failed',error_json=? WHERE id=?",
                               (json.dumps(error), run_id))
            connection.commit()
        self.assertEqual(self.client.post(url).status_code, 409)
        for other_reason, mixed in [("文に「K.」があるが帰属は不明である。", False),
                                    (reason, True)]:
            other_id, *_ = failed_run(other_reason, mixed=mixed)
            self.assertEqual(self.client.post(
                f"/api/runs/{other_id}/replay-failed-authoring-node"
            ).status_code, 409)

    def test_v32_local_proofreading_api_keeps_audit_and_source_preview(self) -> None:
        from fukamiru.revisions import proofreading_local as local
        from fukamiru.worker import sentence_proofreading as pipeline

        with self.app.state.databases.state.connect() as connection:
            service = RevisionService(connection)
            project = service.register_project(name="局所校正", root_path=str(self.root / "local"))
            document = service.create_document(project_id=project, kind="paper", title="校正")
            run = RunRepository(connection).create(
                workflow_name="manuscript_generator", workflow_version="32",
                entry_node="extract_proofreading_sentences", inputs={}, project_id=project,
            )
            node_id = connection.execute(
                "SELECT id FROM node_runs WHERE run_id=?", (run.run_id,)
            ).fetchone()[0]
        source = '<article id="d" lang="ja"><p id="p">比較視角から考える。</p></article>'
        context = {"document_id": document, "proposed_html": source, "use_llm_jp": True}

        def complete(**kwargs):
            payload = json.loads(kwargs["user_prompt"])
            return {
                **{key: payload[key] for key in local.IDENTITY_FIELDS},
                "edits": [{"before": "比較視角", "after": "比較視点", "reason": "連語の修正"}],
            }

        worker = SimpleNamespace(
            databases=self.app.state.databases, settings=self.settings,
            llm_jp=SimpleNamespace(model_identity="fixture", complete_json=complete),
            _string_list=lambda values: values or [], _raise_if_run_cancelled=lambda *a: None,
            execute_local_llm=lambda operation, **kw: operation(),
        )
        claimed = SimpleNamespace(
            run_id=run.run_id, node_run_id=node_id,
            workflow_name="manuscript_generator", workflow_version="32",
        )
        context = pipeline.prepare_sentences(worker, claimed, context)
        context = pipeline.generate_candidates(worker, claimed, context)
        context["pre_proofreading_html"] = source
        context["proposed_html"] = source.replace("比較視角", "比較視点")
        with self.app.state.databases.state.connect() as connection:
            connection.execute("UPDATE runs SET output_json=? WHERE id=?",
                               (json.dumps(context), run.run_id))
        result = self.client.get(f"/api/runs/{run.run_id}/proofreading-candidates")
        self.assertEqual(result.status_code, 200)
        body = result.json()
        self.assertEqual(body["total"], 1)
        record = body["records"][0]
        self.assertEqual(record["preview_text"], "比較視点から考える。")
        self.assertEqual(record["local_edit_audit"]["contract"], local.CONTRACT)
        self.assertEqual(record["local_edit_audit"]["block_id"], "p")

    def test_proofreading_generation_recovery_preserves_old_rows_and_requires_new_scope(
        self,
    ) -> None:
        from fukamiru.revisions.sentence_proofreading import digest, extract_sentences
        from fukamiru.revisions.service import RevisionService

        old_html = '<article id="d" lang="ja"><p id="p">旧文を検討する。</p></article>'
        new_html = '<article id="d" lang="ja"><p id="p">新文を検討する。</p></article>'
        error = {'type':'FukamiruError','message':'proofreading.source_checkpoint_changed'}

        def failed_run(*, same_text=False, no_marker=False, existing_scope=False,
                       unproven_history=False):
            with self.app.state.databases.state.connect() as c:
                revisions = RevisionService(c)
                root_index = c.execute('SELECT COUNT(*) FROM projects').fetchone()[0]
                project = revisions.register_project(name='校正',
                    root_path=f'/proofreading-test-{root_index}')
                document = revisions.create_document(project_id=project, kind='paper', title='校正')
                context = {'document_id':document, 'proposed_html':new_html,
                    'draft_evidence_identity_generation':{'producer_node_run_id':'new-draft'},
                    'prior_candidate_generations':[{'source_node_run_id':'old-input'}]}
                if no_marker:
                    context.pop('draft_evidence_identity_generation')
                created = RunRepository(c).create(workflow_name='manuscript_generator',
                    workflow_version='32', entry_node='review_japanese_sections',
                    inputs=context, project_id=project)
                run_id = created.run_id
                c.execute("UPDATE node_runs SET status='completed',output_json=? WHERE run_id=?",
                          (json.dumps(context),run_id))
                footnote_error = {'type':'FukamiruError','message':'old footnote failure'}
                for attempt in (2,3):
                    c.execute("INSERT INTO node_runs(id,run_id,node_id,attempt,status,input_json,"
                        "error_json) VALUES(?,?,'render_evidence_footnotes',?,'failed','{}',?)",
                        (run_id+f'-old-{attempt}',run_id,attempt,json.dumps(footnote_error)))
                c.execute("INSERT INTO node_runs(id,run_id,node_id,attempt,status,input_json,"
                    "output_json) VALUES(?,?,'reconcile_candidate_context',3,?,'{}','{}')",
                    (run_id+'-recovered',run_id,'failed' if unproven_history else 'completed'))
                EventRepository(c).append(run_id,'run.failed_authoring_node_replayed',payload={
                    'policy':'authoring-v31-stale-candidate-generation-recovery-v1',
                    'node_id':'render_evidence_footnotes','target_node_run_id':run_id+'-recovered',
                    'input_sha256':hashlib.sha256(b'{}').hexdigest(),
                    'previous_node_error':footnote_error})
                inputs = json.dumps(context)
                c.execute("INSERT INTO node_runs(id,run_id,node_id,attempt,status,input_json,"
                    "error_json) VALUES(?,?,'extract_proofreading_sentences',2,'failed',?,?)",
                    (run_id+'-extract',run_id,inputs,json.dumps(error)))
                sentence = extract_sentences(new_html if same_text else old_html,
                    run_id=run_id,document_id=document)[0]
                c.execute("INSERT INTO manuscript_proofreading_sentences "
                    "(id,run_id,document_id,source_sentence_id,block_id,sentence_ordinal,"
                    "start_offset,end_offset,original_text,original_hash,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,'now')",[sentence[k] for k in (
                        'id','run_id','document_id','source_sentence_id','block_id',
                        'sentence_ordinal','start_offset','end_offset','original_text',
                        'original_hash')])
                if existing_scope:
                    c.execute("INSERT INTO manuscript_proofreading_generations VALUES "
                        "(?,?,'changed-source','producer','now')",(run_id,'authoring:new-draft'))
                c.execute("UPDATE runs SET status='failed',error_json=? WHERE id=?",
                          (json.dumps(error),run_id))
                c.commit()
                before=[dict(row) for row in c.execute('SELECT * FROM node_runs WHERE run_id=?',
                                                       (run_id,))]
                old=dict(c.execute('SELECT * FROM manuscript_proofreading_sentences WHERE run_id=?',
                                   (run_id,)).fetchone())
            return run_id,inputs,before,old

        run_id,inputs,before,old=failed_run()
        url=f'/api/runs/{run_id}/replay-failed-authoring-node'
        response=self.client.post(url)
        self.assertEqual(response.status_code,200,response.text)
        with self.app.state.databases.state.connect() as c:
            for row in before:
                current=dict(c.execute('SELECT * FROM node_runs WHERE id=?',
                                       (row['id'],)).fetchone())
                if row['id']==run_id+'-extract':
                    self.assertEqual(current['input_json'],inputs)
                    self.assertEqual(current['status'],'queued')
                else:
                    self.assertEqual(current,row)
            self.assertEqual(dict(c.execute('SELECT * FROM manuscript_proofreading_sentences '
                                           'WHERE run_id=?',(run_id,)).fetchone()),old)
            event=json.loads(c.execute("SELECT payload_json FROM event_log WHERE run_id=? AND "
                "event_type='run.failed_authoring_node_replayed' ORDER BY rowid DESC LIMIT 1",
                (run_id,)).fetchone()[0])
            self.assertEqual(event['policy'],'authoring-v31-proofreading-generation-recovery-v1')
            self.assertEqual(event['previous_node_error'],error)
            self.assertEqual(event['proofreading_generation']['source_html_sha256'],digest(new_html))
            c.execute("UPDATE node_runs SET status='failed',error_json=? WHERE id=?",
                      (json.dumps(error),run_id+'-extract'))
            c.execute("UPDATE runs SET status='failed',error_json=? WHERE id=?",
                      (json.dumps(error),run_id))
            c.commit()
        self.assertEqual(self.client.post(url).status_code,409)
        for kwargs in ({'same_text':True},{'no_marker':True},{'existing_scope':True},
                       {'unproven_history':True}):
            other,_,_,_=failed_run(**kwargs)
            self.assertEqual(self.client.post(
                f'/api/runs/{other}/replay-failed-authoring-node').status_code,409)
        missing,_,_,_=failed_run()
        with self.app.state.databases.state.connect() as c:
            c.execute('DELETE FROM schema_migrations WHERE version=?',
                      ('0055_manuscript_proofreading_generations',))
            c.commit()
        self.assertEqual(self.client.post(
            f'/api/runs/{missing}/replay-failed-authoring-node').status_code,409)

    def test_submission_marker_recovery_requires_review_and_pending_proposal(self) -> None:
        from fukamiru.authoring.evidence_contracts import canonical_hash
        from fukamiru.storage.payloads import PayloadStore

        self._create_authoring_review_fixture()
        url = '/api/reviews/review-authoring-review/feedback'
        packet = self.client.get(url).json()
        self.assertEqual(self.client.put(url, json={'revision':0,'submit':True,
            'answers':[{'question_id':packet['questions'][0]['question_id'],
                        'answer':'資料は手元にない。'}]}).status_code, 200)
        self.assertEqual(self.client.post('/api/reviews/review-authoring-review/resolve',
                                         json={'decision':'revise'}).status_code, 200)
        with self.app.state.databases.state.connect() as c:
            run = c.execute("SELECT run_id FROM human_reviews WHERE id='review-authoring-review'"
                            ).fetchone()[0]
            row = c.execute("SELECT * FROM node_runs WHERE run_id=? AND status='queued'",
                            (run,)).fetchone()
            source = PayloadStore(c).unpack(json.loads(row['input_json']))
            pending = dict(c.execute('SELECT * FROM authoring_pending_proposals WHERE run_id=?',
                                      (run,)).fetchone())
            for key in ('source_document_id','source_revision_id','plan_id','plan_revision'):
                source[key] = pending[key]
            source['document_id'] = pending['target_document_id']
            marker = source['authoring_submission_revalidation']
            current = {key:value for key,value in source.items()
                       if key != 'authoring_submission_revalidation'}
            html = pending['proposed_html'].replace('本文。','回答を反映した本文。')
            current.update({'proposed_html':html,
                'frozen_candidate_v26':{'html_sha256':hashlib.sha256(html.encode()).hexdigest()},
                'prior_candidate_generations':[{'source_node_run_id':row['id'],'cleared_fields':{
                    'authoring_submission_revalidation':canonical_hash(marker)}}]})
            c.execute("UPDATE node_runs SET status='completed',input_json=? WHERE id=?",
                      (json.dumps(source),row['id']))
            error = {'type':'ValueError','message':'authoring run already has a pending proposal'}
            c.execute("INSERT INTO node_runs(id,run_id,node_id,attempt,status,"
                "input_json,error_json) "
                "VALUES('marker-finalize-failed',?,'finalize_authoring',2,'failed',?,?)",
                (run,json.dumps(current),json.dumps(error)))
            c.execute("UPDATE runs SET status='failed',error_json=? WHERE id=?",
                      (json.dumps(error),run))
            c.execute("UPDATE human_reviews SET status='open' WHERE id='review-authoring-review'")
            c.commit()
        replay_url = f'/api/runs/{run}/replay-failed-authoring-node'
        self.assertEqual(self.client.post(replay_url).status_code, 409)
        with self.app.state.databases.state.connect() as c:
            c.execute("UPDATE human_reviews SET status='edited' WHERE id='review-authoring-review'")
            c.execute("UPDATE authoring_pending_proposals SET status='confirmed',"
                      "confirmed_revision_id=?,resolved_at=? WHERE run_id=?",
                      (pending['source_revision_id'],utc_now(),run))
            c.commit()
        self.assertEqual(self.client.post(replay_url).status_code, 409)
        with self.app.state.databases.state.connect() as c:
            c.execute("UPDATE authoring_pending_proposals SET status='pending',"
                      "confirmed_revision_id=NULL,resolved_at=NULL WHERE run_id=?", (run,))
            c.commit()
            before = [dict(r) for r in c.execute('SELECT * FROM node_runs WHERE run_id=?',(run,))]
        response = self.client.post(replay_url)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['node_id'],'finalize_authoring')
        self.assertEqual(response.json()['attempt'],3)
        with self.app.state.databases.state.connect() as c:
            for old in before:
                self.assertEqual(dict(c.execute('SELECT * FROM node_runs WHERE id=?',
                                               (old['id'],)).fetchone()),old)
            data = PayloadStore(c).unpack(json.loads(c.execute(
                'SELECT input_json FROM node_runs WHERE id=?',
                (response.json()['node_run_id'],)).fetchone()[0]))
            self.assertTrue(all(data[key]==value for key,value in current.items()))
            self.assertEqual(data['authoring_submission_revalidation'],marker)
            self.assertEqual(dict(c.execute('SELECT * FROM authoring_pending_proposals '
                                           'WHERE run_id=?',(run,)).fetchone()),pending)
        self.assertEqual(self.client.post(replay_url).status_code,409)

    def test_rendered_qualification_recovery_preserves_input_and_verified_history(self) -> None:
        from test_scholarly_qualification import qualification_fixture

        error = {"type": "FukamiruError", "message":
                 "V31 rendered manuscript has reader-facing scaffold: SEC-plan-s2"}

        def failed_run(unproven=False):
            context = qualification_fixture()
            with self.app.state.databases.state.connect() as c:
                created = RunRepository(c).create(workflow_name="manuscript_generator",
                    workflow_version="32", entry_node="draft_rewrite", inputs=context,
                    project_id=None)
                run = created.run_id
                c.execute("UPDATE node_runs SET status='completed' WHERE run_id=?", (run,))
                for attempt, policy in [(3, "stale-candidate-generation"),
                                         (4, "empty-citation-projection")]:
                    c.execute("INSERT INTO node_runs(id,run_id,node_id,attempt,status,input_json,"
                        "error_json) VALUES(?,?,'render_evidence_footnotes',?,'failed','{}',?)",
                        (run+f'-old-{attempt}', run, attempt, '{"message":"old failure"}'))
                    target = run+f'-target-{attempt}'
                    c.execute("INSERT INTO node_runs(id,run_id,node_id,attempt,status,input_json) "
                        "VALUES(?,?,'reconcile_candidate_context_after_proofreading',?,?,'{}')",
                        (target, run, attempt, "failed" if unproven and attempt == 4
                         else "completed"))
                    EventRepository(c).append(run, 'run.failed_authoring_node_replayed', payload={
                        'policy':f'authoring-v31-{policy}-recovery-v1',
                        'node_id':'render_evidence_footnotes','target_node_run_id':target,
                        'input_sha256':hashlib.sha256(b'{}').hexdigest(),
                        'previous_node_error':{'message':'old failure'}})
                c.execute("INSERT INTO node_runs(id,run_id,node_id,attempt,status,input_json,"
                    "error_json) VALUES(?,?,'render_evidence_footnotes',5,'failed',?,?)",
                    (run+'-failed', run, json.dumps(context), json.dumps(error)))
                c.execute("UPDATE runs SET status='failed',error_json=? WHERE id=?",
                          (json.dumps(error), run))
                c.commit()
                before = [dict(r) for r in c.execute(
                    'SELECT * FROM node_runs WHERE run_id=?', (run,))]
            return run, before

        run, before = failed_run()
        url = f'/api/runs/{run}/replay-failed-authoring-node'
        response = self.client.post(url)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['attempt'], 5)
        with self.app.state.databases.state.connect() as c:
            for old in before:
                current = dict(c.execute('SELECT * FROM node_runs WHERE id=?',
                                         (old['id'],)).fetchone())
                if old['id'] == run+'-failed':
                    self.assertEqual(current['status'], 'queued')
                    self.assertEqual(current['input_json'], old['input_json'])
                else:
                    self.assertEqual(current, old)
            event = json.loads(c.execute("SELECT payload_json FROM event_log WHERE run_id=? AND "
                "event_type='run.failed_authoring_node_replayed' ORDER BY rowid DESC LIMIT 1",
                (run,)).fetchone()[0])
            self.assertEqual(event['previous_node_error'], error)
            self.assertEqual(event['qualification_review']['resolution_route'],
                             'prose_redraft_then_human_review')
            c.execute("UPDATE node_runs SET status='failed',error_json=? WHERE id=?",
                      (json.dumps(error), run+'-failed'))
            c.execute("UPDATE runs SET status='failed' WHERE id=?", (run,))
            c.commit()
        self.assertEqual(self.client.post(url).status_code, 409)
        unproven, _ = failed_run(unproven=True)
        self.assertEqual(self.client.post(
            f'/api/runs/{unproven}/replay-failed-authoring-node').status_code, 409)

    def test_empty_citation_projection_recovery_preserves_failed_node_and_is_once(self) -> None:
        from test_authoring_generation import EMPTY_PROJECTION_ERROR, empty_projection_fixture

        from fukamiru.storage.payloads import PayloadStore

        context = empty_projection_fixture()
        error = {"type": "FukamiruError", "message": EMPTY_PROJECTION_ERROR}
        with self.app.state.databases.state.connect() as connection:
            created = RunRepository(connection).create(workflow_name="manuscript_generator",
                workflow_version="32", entry_node="reconcile_candidate_context_after_proofreading",
                inputs=context, project_id=None)
            connection.execute("UPDATE node_runs SET status='completed',output_json=? "
                               "WHERE run_id=?",
                               (json.dumps(context), created.run_id))
            connection.execute("INSERT INTO node_runs(id,run_id,node_id,attempt,status,input_json,"
                "error_json) VALUES('empty-projection-failed',?,'render_evidence_footnotes',4,"
                "'failed',?,?)", (created.run_id, json.dumps(context), json.dumps(error)))
            connection.execute("UPDATE runs SET status='failed',error_json=? WHERE id=?",
                               (json.dumps(error), created.run_id))
            connection.commit()
            before = [dict(row) for row in connection.execute(
                "SELECT * FROM node_runs WHERE run_id=?", (created.run_id,))]
        url = f"/api/runs/{created.run_id}/replay-failed-authoring-node"
        response = self.client.post(url)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["node_id"],
                         "reconcile_candidate_context_after_proofreading")
        with self.app.state.databases.state.connect() as connection:
            for node in before:
                self.assertEqual(dict(connection.execute("SELECT * FROM node_runs WHERE id=?",
                                                         (node["id"],)).fetchone()), node)
            new = connection.execute("SELECT input_json FROM node_runs WHERE id=?",
                                     (response.json()["node_run_id"],)).fetchone()[0]
            corrected = PayloadStore(connection).unpack(json.loads(new))
            for key in ("proposed_html", "draft_sections", "fixed_assets", "evidence_uses",
                        "human_question_feedback", "manuscript_proofreading_shadow"):
                self.assertEqual(corrected[key], context[key])
            self.assertEqual(len(corrected["applied_edits"]), 1)
            event = json.loads(connection.execute("SELECT payload_json FROM event_log WHERE "
                "run_id=? AND event_type='run.failed_authoring_node_replayed' ORDER BY rowid DESC "
                "LIMIT 1", (created.run_id,)).fetchone()[0])
            self.assertEqual(event["previous_node_error"], error)
            self.assertEqual(event["input_sha256"], hashlib.sha256(
                json.dumps(context).encode()).hexdigest())
            connection.execute("UPDATE node_runs SET status='completed' WHERE id=?",
                               (response.json()["node_run_id"],))
            connection.execute("UPDATE runs SET status='failed' WHERE id=?", (created.run_id,))
            connection.commit()
        self.assertEqual(self.client.post(url).status_code, 409)

    def test_stale_candidate_projection_recovery_retains_failures_and_restarts_audits(self) -> None:
        from fukamiru.authoring.evidence_contracts import (
            build_fixed_assets,
            compile_draft_evidence,
            evidence_use_identity_hash,
        )
        from fukamiru.storage.payloads import PayloadStore
        from fukamiru.worker.runner import WorkerRunner

        def failed_run(*, tampered=False, same_candidate=False, missing_gate=False):
            context = {"plan_id": "plan", "language": "ja", "draft_attempt": 2,
                "propositions": [{"id": "P1", "text": "著者の考え。"}],
                "evidence_context": [{"id": "B1", "title": "資料名", "author": "著者B",
                    "reference_id": "R1",
                    "verification_status": "verified"}],
                "reference_entries": [{"reference_id": "R1", "title": "資料名",
                                       "author": "著者B", "year": "2020", "evidence_ids": ["B1"]}],
                "argument_nodes": [{"id": "E1", "node_type": "evidence", "claim_id": "C1",
                    "provenance": {"source_ids": ["B1"]}},
                    {"id": "E2", "node_type": "evidence", "claim_id": "C2",
                     "provenance": {"source_ids": ["P1"]}}]}
            context['fixed_assets'] = build_fixed_assets(context).model_dump(mode='json')
            context['section_contracts'] = [{"section_id": sid, "title": sid,
                "article_role": role, "claim_ids": [claim], "evidence_ids": [evidence],
                "development_expectations": {"required_argument_moves": []}}
                for sid, role, claim, evidence in [('s1', 'body', 'C1', 'E1'),
                                                  ('s2', 'conclusion', 'C2', 'E2')]]
            sections = [{"section_id": sid, "article_role": role, "claim_ids": [claim],
                "text": text, "paragraphs": [{"text": text, "evidence_ids": [evidence],
                                               "move_indices": []}],
                "evidence_treatment": [{"evidence_id": evidence, "claim_id": claim,
                    "prose_treatment": treatment, "support_sentence": text}]}
                for sid, role, claim, evidence, text, treatment in [
                    ('s1', 'body', 'C1', 'E1', '著者Bの『資料名』を比較の枠として参照する。',
                     'attributed_scholarship'),
                    ('s2', 'conclusion', 'C2', 'E2', '著者は二つの条件を区別する。',
                     'qualified_interpretation')]]
            sections, uses, _, _ = compile_draft_evidence(context, sections)
            context.update({'draft_sections': sections,
                            'evidence_uses': [use.model_dump(mode='json') for use in uses],
                            'evidence_identity_hash': evidence_use_identity_hash(uses)})
            html = WorkerRunner._authoring_render_sections(context, sections)
            context['proposed_html'] = html
            context['submission_validation_gate'] = {'failed_validations': ['references'],
                'candidate_sha256': hashlib.sha256(html.encode()).hexdigest()
                if same_candidate else '0' * 64}
            if missing_gate:
                context.pop('submission_validation_gate')
            context['evidence_footnote_render_report'] = {'old_generation': True}
            context['authoring_paragraph_citation_lineage'] = [{
                'output_block_id': 'P-plan-s2-001', 'evidence_ids': ['B1'],
                'argument_evidence_node_ids': ['E1']}]
            current = {**context, **WorkerRunner._authoring_v29_plan_bridge(
                context, html, semantic_attribution=True)}
            current.pop('submission_validation_gate', None)
            if tampered:
                current['evidence_uses'] = [{**use, 'verification_status': 'verified'}
                                            for use in current['evidence_uses']]
            error = {'type': 'FukamiruError', 'message': (
                'Authoring footnote needs a unique visible source mention '
                'in its cited sentence: P-plan-s2-001')}
            with self.app.state.databases.state.connect() as connection:
                created = RunRepository(connection).create(
                    workflow_name='manuscript_generator', workflow_version='32',
                    entry_node='reconcile_candidate_context', inputs=context, project_id=None)
                connection.execute("UPDATE node_runs SET status='completed',output_json=? "
                    "WHERE run_id=?", (json.dumps(current), created.run_id))
                for attempt in (2, 3):
                    connection.execute("INSERT INTO node_runs(id,run_id,node_id,attempt,status,"
                        "input_json,error_json) VALUES(?,?,'render_evidence_footnotes',?,"
                        "'failed',?,?)",
                        (created.run_id + f'-{attempt}', created.run_id, attempt,
                         json.dumps(current), json.dumps(error)))
                connection.execute("UPDATE runs SET status='failed',error_json=? WHERE id=?",
                                   (json.dumps(error), created.run_id))
                connection.commit()
                before = [dict(row) for row in connection.execute(
                    "SELECT * FROM node_runs WHERE run_id=?", (created.run_id,))]
            return created.run_id, before, current

        run_id, before, original = failed_run()
        response = self.client.post(f'/api/runs/{run_id}/replay-failed-authoring-node')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['node_id'], 'reconcile_candidate_context')
        with self.app.state.databases.state.connect() as connection:
            for old in before:
                self.assertEqual(dict(connection.execute('SELECT * FROM node_runs WHERE id=?',
                                                         (old['id'],)).fetchone()), old)
            target = connection.execute('SELECT * FROM node_runs WHERE id=?',
                                         (response.json()['node_run_id'],)).fetchone()
            corrected = PayloadStore(connection).unpack(json.loads(target['input_json']))
            for key in ['proposed_html', 'draft_sections', 'evidence_uses', 'fixed_assets',
                        'argument_nodes', 'reference_entries']:
                self.assertEqual(corrected[key], original[key])
            self.assertNotIn('evidence_footnote_render_report', corrected)
            self.assertNotIn('submission_validation_gate', corrected)
            conclusion = [item for item in corrected['authoring_paragraph_citation_lineage']
                          if item['authoring_section_id'] == 's2']
            self.assertEqual(conclusion, [])
            self.assertTrue(corrected['authoring_paragraph_citation_lineage'])
            runner = object.__new__(WorkerRunner)
            runner._authoring_v31_judge_attribution_predicates = lambda *args: {}
            result = runner._authoring_reconcile_candidate_context(SimpleNamespace(
                node_id='reconcile_candidate_context', workflow_version='32'), corrected)
            self.assertEqual(result.outcome, 'success')
        self.assertEqual(self.client.post(
            f'/api/runs/{run_id}/replay-failed-authoring-node').status_code, 409)
        for kwargs in ({'tampered': True}, {'same_candidate': True}, {'missing_gate': True}):
            other, _, _ = failed_run(**kwargs)
            self.assertEqual(self.client.post(
                f'/api/runs/{other}/replay-failed-authoring-node').status_code, 409)

    def test_stale_draft_evidence_baseline_replay_requires_intact_compiled_producer(self) -> None:
        from fukamiru.authoring.evidence_contracts import (
            build_fixed_assets,
            compile_draft_evidence,
            evidence_use_identity_hash,
        )
        from fukamiru.storage.payloads import PayloadStore

        error = {"type": "FukamiruError", "message":
                 "Proofreading changed claim/source evidence identity"}

        def failed_run(*, tamper_current=False, tamper_producer=False, same_generation=False,
                       proofreading=False, wrong_old_hash=False, unrelated_error=False):
            before = {"propositions": [{"id": "P1", "text": "著者の見解一。"},
                                       {"id": "P2", "text": "著者の見解二。"}]}
            before["fixed_assets"] = build_fixed_assets(before).model_dump(mode="json")
            sections = [{"section_id": "S1", "paragraphs": [{
                "text": "著者の見解一。著者の見解二。", "evidence_ids": ["P1", "P2"],
            }], "evidence_treatment": [{"evidence_id": source, "claim_id": "C" + source,
                "prose_treatment": "attributed_user_statement", "support_sentence": text}
                for source, text in (("P1", "著者の見解一。"), ("P2", "著者の見解二。"))]}]
            old_sections, old_uses, _, _ = compile_draft_evidence(before, sections)
            old_hash = evidence_use_identity_hash(old_uses)
            before.update({"draft_attempt": 1, "draft_sections": old_sections,
                "evidence_uses": [use.model_dump(mode="json") for use in old_uses],
                "evidence_identity_hash": "wrong" if wrong_old_hash else old_hash,
                "proofreading_evidence_identity_validation": {
                    "passed": True, "identity_hash": old_hash, "evidence_use_count": 2}})
            sections[0]["paragraphs"] = [{"text": "著者の見解一。", "evidence_ids": ["P1"]}]
            sections[0]["evidence_treatment"] = sections[0]["evidence_treatment"][:1]
            new_sections, new_uses, _, _ = compile_draft_evidence(before, sections)
            produced = {**before, "draft_attempt": 1 if same_generation else 2,
                "draft_sections": new_sections,
                "proposed_html": "<article>著者の見解一。</article>",
                "evidence_uses": [use.model_dump(mode="json") for use in new_uses]}
            if tamper_producer:
                produced["evidence_uses"][0]["verification_status"] = "verified"
            current = json.loads(json.dumps(produced))
            if tamper_current:
                current["evidence_uses"][0]["source_ids"] = ["P2"]
            failure = {**error, "message": "different error"} if unrelated_error else error
            inputs = json.dumps(current)
            with self.app.state.databases.state.connect() as connection:
                created = RunRepository(connection).create(
                    workflow_name="manuscript_generator", workflow_version="32",
                    entry_node="draft_rewrite", inputs=before, project_id=None,
                )
                connection.execute("UPDATE node_runs SET status='completed',output_json=? "
                    "WHERE run_id=?", (json.dumps(produced), created.run_id))
                producer = dict(connection.execute("SELECT * FROM node_runs WHERE run_id=?",
                                                   (created.run_id,)).fetchone())
                connection.execute("INSERT INTO node_runs(id,run_id,node_id,attempt,status,"
                    "input_json,error_json) VALUES(?,?,?,2,'failed',?,?)",
                    (created.run_id + '-failed', created.run_id,
                     'reconcile_candidate_context_after_proofreading' if proofreading
                     else 'reconcile_candidate_context', inputs, json.dumps(failure)))
                connection.execute("UPDATE runs SET status='failed',error_json=? WHERE id=?",
                                   (json.dumps(failure), created.run_id))
                connection.commit()
            return created.run_id, current, inputs, producer

        run_id, original, inputs, producer = failed_run()
        url = f"/api/runs/{run_id}/replay-failed-authoring-node"
        response = self.client.post(url)
        self.assertEqual(response.status_code, 200, response.text)
        with self.app.state.databases.state.connect() as connection:
            node = connection.execute("SELECT * FROM node_runs WHERE id=?",
                                      (run_id + '-failed',)).fetchone()
            corrected = PayloadStore(connection).unpack(json.loads(node['input_json']))
            self.assertEqual(node['status'], 'queued')
            for key in ['fixed_assets', 'evidence_uses', 'draft_sections', 'proposed_html']:
                self.assertEqual(corrected[key], original[key])
            self.assertNotEqual(corrected['evidence_identity_hash'],
                                original['evidence_identity_hash'])
            event = json.loads(connection.execute("SELECT payload_json FROM event_log "
                "WHERE run_id=? AND event_type='run.failed_authoring_node_replayed'",
                (run_id,)).fetchone()[0])
            self.assertEqual(event['policy'], 'authoring-v31-draft-evidence-generation-recovery-v1')
            correction = event['evidence_generation_correction']
            self.assertEqual(PayloadStore(connection).unpack(
                correction['previous_node_input'])['input_json'], inputs)
            self.assertEqual(event['previous_node_error'], error)
            self.assertEqual(dict(connection.execute("SELECT * FROM node_runs WHERE id=?",
                                                     (producer['id'],)).fetchone()), producer)
            connection.execute("UPDATE node_runs SET status='failed',error_json=? WHERE id=?",
                               (json.dumps(error), run_id + '-failed'))
            connection.execute("UPDATE runs SET status='failed',error_json=? WHERE id=?",
                               (json.dumps(error), run_id))
            connection.commit()
        self.assertEqual(self.client.post(url).status_code, 409)
        for kwargs in ({'tamper_current': True}, {'tamper_producer': True},
                       {'same_generation': True}, {'proofreading': True},
                       {'wrong_old_hash': True}, {'unrelated_error': True}):
            other, _, _, _ = failed_run(**kwargs)
            self.assertEqual(self.client.post(
                f"/api/runs/{other}/replay-failed-authoring-node"
            ).status_code, 409)

    def test_source_use_contract_replay_preserves_same_input_failures_and_frozen_assets(
        self,
    ) -> None:
        from fukamiru.authoring.evidence_contracts import build_fixed_assets

        error = {"type": "FukamiruError", "message": (
            "draft evidence E1 does not support claim role source_fact: B1"
        )}

        def failed_run(*, fact_source=False, memo_source=False, different_input=False,
                       mixed_error=False):
            source = {"id": "B1", "title": "書誌情報だけ", "verification_status": "verified"}
            if fact_source:
                source.update({"text": "確認済み本文", "source_span_id": "SPAN1"})
            context = {"evidence_context": [source], "argument_nodes": [{
                "id": "E1", "node_type": "evidence", "provenance": {"source_ids": ["B1"]},
            }], "human_question_feedback": {"answers": [{"answer": "資料は手元にない。"}]}}
            if memo_source:
                context["evidence_context"] = []
                context["propositions"] = [{"id": "B1", "text": "著者のメモ。"}]
            context["fixed_assets"] = build_fixed_assets(context).model_dump(mode="json")
            inputs = json.dumps(context)
            failure = {**error, "message": error["message"] + (
                "; unrelated failure" if mixed_error else ""
            )}
            with self.app.state.databases.state.connect() as connection:
                created = RunRepository(connection).create(
                    workflow_name="manuscript_generator", workflow_version="32",
                    entry_node="draft_rewrite", inputs=context, project_id=None,
                )
                connection.execute("UPDATE node_runs SET status='completed',output_json='{}' "
                                   "WHERE run_id=?", (created.run_id,))
                for attempt in (2, 3):
                    old_input = json.dumps({**context, "other_input": True}) if (
                        different_input and attempt == 2
                    ) else inputs
                    connection.execute(
                        "INSERT INTO node_runs(id,run_id,node_id,attempt,status,input_json,"
                        "error_json) VALUES(?,?,'draft_rewrite',?,'failed',?,?)",
                        (created.run_id + f'-{attempt}', created.run_id, attempt, old_input,
                         json.dumps(failure)),
                    )
                connection.execute("UPDATE runs SET status='failed',error_json=? WHERE id=?",
                                   (json.dumps(failure), created.run_id))
                connection.commit()
            return created.run_id, inputs

        run_id, inputs = failed_run()
        url = f"/api/runs/{run_id}/replay-failed-authoring-node"
        response = self.client.post(url)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["attempt"], 3)
        with self.app.state.databases.state.connect() as connection:
            nodes = connection.execute(
                "SELECT * FROM node_runs WHERE run_id=? AND attempt IN (2,3) ORDER BY attempt",
                (run_id,),
            ).fetchall()
            self.assertEqual([row["input_json"] for row in nodes], [inputs, inputs])
            self.assertEqual(nodes[0]["status"], "failed")
            self.assertEqual(json.loads(nodes[0]["error_json"]), error)
            self.assertEqual(nodes[1]["status"], "queued")
            event = json.loads(connection.execute("SELECT payload_json FROM event_log "
                "WHERE run_id=? AND event_type='run.failed_authoring_node_replayed' "
                "ORDER BY rowid DESC LIMIT 1", (run_id,)).fetchone()[0])
            self.assertEqual(event["policy"], "authoring-v31-source-use-contract-recovery-v1")
            self.assertEqual(event["previous_node_error"], error)
            self.assertEqual(event["source_use_constraints"][0]["permitted_uses"][0]["claim_role"],
                             "contextual")
            connection.execute("UPDATE node_runs SET status='failed',error_json=? WHERE id=?",
                               (json.dumps(error), run_id + '-3'))
            connection.execute("UPDATE runs SET status='failed',error_json=? WHERE id=?",
                               (json.dumps(error), run_id))
            connection.commit()
        self.assertEqual(self.client.post(url).status_code, 409)
        for kwargs in ({"fact_source": True}, {"memo_source": True},
                       {"different_input": True}, {"mixed_error": True}):
            other_id, _ = failed_run(**kwargs)
            self.assertEqual(self.client.post(
                f"/api/runs/{other_id}/replay-failed-authoring-node"
            ).status_code, 409)

    def test_v32_typed_source_failure_replays_only_after_migration_once(self) -> None:
        error = {"type": "IntegrityError", "message": (
            "CHECK constraint failed: source_type IN ('local_rag', 'online_paper')"
        )}
        original_input = json.dumps({"partial_searches": ["retained"], "claims": ["retained"]})
        with self.app.state.databases.state.connect() as connection:
            created = RunRepository(connection).create(
                workflow_name="manuscript_generator", workflow_version="32",
                entry_node="validate_evidence_support", inputs={"upstream": "preserved"},
                project_id=None,
            )
            connection.execute("UPDATE node_runs SET status='completed',output_json=? "
                               "WHERE run_id=?", ('{"evidence":"retained"}', created.run_id))
            connection.execute(
                "INSERT INTO node_runs(id,run_id,node_id,attempt,status,input_json,error_json) "
                "VALUES('typed-source-failure',?,'bounded_research_retry',1,'failed',?,?)",
                (created.run_id, original_input, json.dumps(error)),
            )
            connection.execute("UPDATE runs SET status='failed',error_json=? WHERE id=?",
                               (json.dumps(error), created.run_id))
            migration = connection.execute("SELECT * FROM schema_migrations WHERE version=?",
                ("0054_typed_manuscript_research_sources",)).fetchone()
            connection.execute("DELETE FROM schema_migrations WHERE version=?", (migration[0],))
            connection.commit()
        url = f"/api/runs/{created.run_id}/replay-failed-authoring-node"
        denied = self.client.post(url)
        self.assertEqual(denied.status_code, 409, denied.text)
        with self.app.state.databases.state.connect() as connection:
            connection.execute("INSERT INTO schema_migrations VALUES(?,?,?)", tuple(migration))
            connection.commit()
        replay = self.client.post(url)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json()["node_id"], "bounded_research_retry")
        self.assertEqual(replay.json()["manual_replay_count"], 1)
        with self.app.state.databases.state.connect() as connection:
            node = connection.execute("SELECT * FROM node_runs WHERE id='typed-source-failure'")
            node = node.fetchone()
            self.assertEqual(node["input_json"], original_input)
            self.assertEqual(node["status"], "queued")
            upstream = connection.execute("SELECT status,output_json FROM node_runs "
                                          "WHERE run_id=? AND node_id='validate_evidence_support'",
                                          (created.run_id,)).fetchone()
            self.assertEqual(upstream["status"], "completed")
            self.assertEqual(upstream["output_json"], '{"evidence":"retained"}')
            event = connection.execute("SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed'", (created.run_id,)).fetchone()
            self.assertEqual(json.loads(event[0])["previous_node_error"], error)
            connection.execute("UPDATE node_runs SET status='failed',error_json=? "
                               "WHERE id='typed-source-failure'", (json.dumps(error),))
            connection.execute("UPDATE runs SET status='failed',error_json=? WHERE id=?",
                               (json.dumps(error), created.run_id))
            connection.commit()
        repeated = self.client.post(url)
        self.assertEqual(repeated.status_code, 409, repeated.text)

    def test_answered_wrapped_request_is_hidden_without_changing_saved_questions(self) -> None:
        self._create_authoring_review_fixture()
        old_url = '/api/reviews/review-authoring-review/feedback'
        old = self.client.get(old_url).json()
        first = old['questions'][0]
        self.assertEqual(self.client.put(old_url, json={'revision':0,'submit':True,
            'answers':[{'question_id':first['question_id'],'answer':'資料は手元にない。'}]}
        ).status_code, 200)
        reason = first['reason']
        with self.app.state.databases.state.connect() as c:
            row = c.execute("SELECT * FROM human_reviews WHERE id='review-authoring-review'"
                            ).fetchone()
            context = {'proposed_html':old['preview_html'], 'human_review_items':[
                {'review_item_id':'different','section_id':'other','category':'interpretation',
                 'reason':'別の表現をどのように扱うか'}],
                'human_revision_decision':{'review_items':[
                    {'scope':'S005-P02-S03','code':'authoring.user_information_required',
                     'message':reason},
                    {'code':'unresolved.important','message':'原著の該当箇所を確認する。'}]}}
            c.execute("UPDATE human_reviews SET status='edited' WHERE id=?", (row['id'],))
            c.execute("INSERT INTO node_runs(id,run_id,node_id,attempt,status,output_json) "
                "VALUES('wrapper-next-node',?,'human_approval',2,'waiting_human',?)",
                (row['run_id'],json.dumps(context)))
            c.execute("INSERT INTO human_reviews(id,run_id,node_run_id,object_type,object_id,"
                "question,options_json,status,created_at) VALUES('wrapper-next-review',?,"
                "'wrapper-next-node','authoring_submission',?,'確認してください','[]','open',?)",
                (row['run_id'],row['object_id'],utc_now()))
            c.commit()
        url = '/api/reviews/wrapper-next-review/feedback'
        fresh = self.client.get(url).json()
        self.assertEqual(len(fresh['questions']),2)
        self.assertNotIn(reason,[q['reason'] for q in fresh['questions']])
        self.assertTrue(next(q for q in fresh['questions'] if q['category']=='unresolved.important'
                            )['question'].endswith('ありますか？'))
        legacy = {'question_id':'wrapped-copy','presentation_version':2,'category':
            'authoring.user_information_required','key':'legacy-copy','reason':reason,
            'question':first['question'],'target':'原稿全体','requested_target_id':'原稿全体'}
        stored = json.dumps([*fresh['questions'],legacy],ensure_ascii=False)
        with self.app.state.databases.state.connect() as c:
            c.execute('UPDATE review_feedback SET questions_json=? WHERE review_id=?',
                      (stored,'wrapper-next-review'))
            c.commit()
        projected = self.client.get(url).json()
        self.assertEqual(len(projected['questions']),2)
        self.assertEqual(projected['revision'],0)
        with self.app.state.databases.state.connect() as c:
            self.assertEqual(c.execute('SELECT questions_json FROM review_feedback '
                'WHERE review_id=?',
                ('wrapper-next-review',)).fetchone()[0],stored)
        late = self.client.put(url, json={'revision':0,'answers':[
            {'question_id':'wrapped-copy','answer':'現在の下書き','llm_overreach':False}]})
        self.assertEqual(late.status_code,200,late.text)
        self.assertEqual(len(self.client.get(url).json()['questions']),3)
        with self.app.state.databases.state.connect() as c:
            c.execute("UPDATE human_reviews SET status='edited' WHERE id='wrapper-next-review'")
            c.execute("UPDATE review_feedback SET answers_json='[]' WHERE review_id=?",
                      ('wrapper-next-review',))
            c.commit()
        self.assertEqual(len(self.client.get(url).json()['questions']),3)
        history = self.client.get(old_url).json()
        self.assertEqual(history['answers'][0]['answer'],'資料は手元にない。')
        self.assertIn(first['question_id'],[q['question_id'] for q in history['questions']])

    def test_review_question_feedback_survives_reload_and_reaches_revision(self) -> None:
        from fukamiru.review_feedback import apply_feedback, feedback_context

        self._create_authoring_review_fixture()
        url = "/api/reviews/review-authoring-review/feedback"
        packet = self.client.get(url).json()
        self.assertIn("本文。", packet["preview_html"])
        question = packet["questions"][0]
        self.assertEqual(question["question_id"], "HRI-S005-P02-S03")
        self.assertTrue(question["question"].endswith("ありますか？"))
        response = self.client.put(url, json={
            "revision": packet["revision"], "submit": True,
            "answers": [{"question_id": question["question_id"], "llm_overreach": True}],
            "overall_comment": "著者の解釈として維持する",
        })
        self.assertEqual(response.status_code, 200, response.text)
        reloaded = self.client.get(url).json()
        self.assertEqual(reloaded["answers"][0]["answer"], "")
        self.assertTrue(reloaded["answers"][0]["llm_overreach"])
        self.assertEqual(reloaded["overall_comment"], "著者の解釈として維持する")
        self.assertEqual(reloaded["revision"], reloaded["submitted_revision"])
        stale = self.client.put(url, json={"revision": 0, "answers": []})
        self.assertEqual(stale.status_code, 409)
        invalid = self.client.put(url, json={"revision": 1, "answers": [
            {"question_id": "another-review-question", "llm_overreach": True},
        ]})
        self.assertEqual(invalid.status_code, 409)
        with self.app.state.databases.state.connect() as connection:
            review = connection.execute(
                "SELECT run_id,node_run_id FROM human_reviews WHERE id='review-authoring-review'"
            ).fetchone()
            context = json.loads(connection.execute(
                "SELECT output_json FROM node_runs WHERE id=?", (review["node_run_id"],),
            ).fetchone()[0])
            context["authoring_review_id"] = "review-authoring-review"
            context["human_review"] = "元の著者コメント"
            connection.execute("UPDATE node_runs SET output_json=? WHERE id=?",
                               (json.dumps(context), review["node_run_id"]))
            connection.commit()
            feedback = feedback_context(connection, review["run_id"])
        changed = apply_feedback(context, feedback)
        self.assertEqual(changed["human_review_items"][0]["status"], "dismissed")
        self.assertEqual(changed["argument_nodes"], context["argument_nodes"])
        self.assertIn("元の著者コメント", changed["human_review"])
        self.assertIn('"llm_overreach": true', changed["human_review"])
        self.assertIn("外部資料の根拠ではありません", changed["human_review"])
        self.assertEqual(apply_feedback(changed, feedback)["human_review"], changed["human_review"])
        self.assertEqual(changed["human_review_items"][0]["source_ids"], ["SRC-014"])
        # A new audit identifier does not resurrect precisely the same demand.
        changed["human_review_items"][0]["review_item_id"] = "new-audit-id"
        self.assertEqual(apply_feedback(changed, feedback)["human_review_items"][0]["status"],
                         "dismissed")
        worker = self.app.state.worker
        capture = {}
        worker.authoring_adapter = SimpleNamespace(
            generate=lambda stage, payload, **kwargs: capture.update(payload) or {}
        )
        worker._authoring_invoke(
            SimpleNamespace(run_id=review["run_id"], workflow_version="32",
                            node_id="generate_hypotheses"),
            stage="generate_hypotheses", context={}, payload={},
            operation_name="feedback-test", checkpoint=False,
        )
        self.assertTrue(capture["human_question_feedback"]["answers"][0]["llm_overreach"])
        revised = self.client.post("/api/reviews/review-authoring-review/resolve",
                                   json={"decision": "revise"})
        self.assertEqual(revised.status_code, 200, revised.text)
        self.assertEqual(revised.json()["next_node"], "start_paragraph_formation")
        with self.app.state.databases.state.connect() as connection:
            queued = connection.execute(
                "SELECT input_json FROM node_runs WHERE run_id=? AND status='queued'",
                (review["run_id"],),
            ).fetchone()
            data = json.loads(queued[0])
            self.assertNotIn("authoring_review_id", data)
            self.assertTrue(data["authoring_submission_revalidation"])
            self.assertTrue(data["human_question_feedback"]["answers"][0]["llm_overreach"])
            self.assertIn('"llm_overreach": true', data["human_review"])
        history = self.client.get(url)
        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual(history.json()["status"], "edited")
        self.assertEqual(history.json()["preview_html"], packet["preview_html"])
        self.assertEqual(self.client.put(url, json={"revision": 1}).status_code, 409)
        with self.app.state.databases.state.connect() as connection:
            connection.execute(
                "INSERT INTO human_reviews(id,run_id,node_run_id,object_type,object_id,"
                "question,options_json,status,created_at) VALUES "
                "('next-review',?,?,'authoring_submission','review-authoring-artifact',"
                "'再生成した原稿を確認できますか？','[]','open',?)",
                (review["run_id"], review["node_run_id"], utc_now()),
            )
            connection.commit()
        next_packet = self.client.get("/api/reviews/next-review/feedback")
        self.assertEqual(next_packet.status_code, 200, next_packet.text)
        self.assertEqual(next_packet.json()["questions"], [])
        sent_history = self.client.get("/api/review-feedback-history").json()
        self.assertIn("review-authoring-review", [item["id"] for item in sent_history])

    def test_review_includes_additional_information_requests_as_questions(self) -> None:
        self._create_authoring_review_fixture()
        with self.app.state.databases.state.connect() as connection:
            row = connection.execute(
                "SELECT node_run_id FROM human_reviews WHERE id='review-authoring-review'"
            ).fetchone()
            context = json.loads(connection.execute(
                "SELECT output_json FROM node_runs WHERE id=?", (row[0],),
            ).fetchone()[0])
            context["required_user_information"] = [{
                "question": "想定する読者層について、補足できる情報はありますか？",
                "affected_scope": "読者層",
            }]
            connection.execute("UPDATE node_runs SET output_json=? WHERE id=?",
                               (json.dumps(context), row[0]))
            connection.commit()
        packet = self.client.get("/api/reviews/review-authoring-review/feedback").json()
        self.assertEqual(len(packet["questions"]), 2)
        self.assertEqual(packet["questions"][1]["question"],
                         "想定する読者層について、補足できる情報はありますか？")

    def test_review_maps_real_authoring_targets_and_coherence_requests(self) -> None:
        from fukamiru.authoring.render import render_canonical_html

        _, artifact_path = self._create_authoring_review_fixture()
        canonical = render_canonical_html(plan_id="review-plan", sections=[{
            "section_id": "section-body-001", "title": "仮想展示の配置", "article_role": "body",
            "paragraphs": [{"text": "対象の段落。著者の解釈を示す。"}],
        }])
        artifact_path.write_text(canonical, encoding="utf-8")
        with self.app.state.databases.state.connect() as connection:
            row = connection.execute("SELECT node_run_id FROM human_reviews WHERE id=?",
                                     ("review-authoring-review",)).fetchone()
            context = json.loads(connection.execute("SELECT output_json FROM node_runs WHERE id=?",
                                                    (row[0],)).fetchone()[0])
            context["human_review_items"] = [{
                "review_item_id": "readable-question", "section_id": "section-body-001",
                "sentence_id": "section-body-001-P01-S02", "status": "open",
                "category": "required_citation", "source_ids": ["SRC-014"],
                "reason": "required source has no citation-to-claim evidence mapping。"
                          "著者記録の主張: 仮想展示の配置は解釈に影響する。",
            }]
            context["coherence_unresolved_routes"] = [{
                "id": "coherence-1", "resolution_route": "additional_research",
                "explanation": "読者の範囲を確認する資料が必要という指摘。",
                "block_ids": ["P-review-plan-section-body-001-001"],
            }]
            context["propositions"] = [{"id": "memo-1", "text": "架空の展示計画を記したメモ。"}]
            context["claims"] = [{"id": "claim-1", "text": "仮想展示の配置に関する比較の主張。"}]
            context["required_user_information"] = [
                {"code": "source", "affected_scope": "SRC-014",
                 "reason": context["human_review_items"][0]["reason"]},
                {"code": "reference_unresolved", "affected_scope": "memo-1",
                 "reason": "原典の書誌情報を確認したい。"},
                {"code": "claim_requires_research", "affected_scope": "claim-1",
                 "reason": "比較の基準を確認する資料が必要。"},
            ]
            connection.execute("UPDATE node_runs SET output_json=? WHERE id=?",
                               (json.dumps(context), row[0]))
            connection.commit()
        packet = self.client.get("/api/reviews/review-authoring-review/feedback").json()
        self.assertEqual(len(packet["questions"]), 4)
        question, coherence, memo, claim = packet["questions"]
        self.assertEqual(question["question_id"], "readable-question")
        self.assertEqual(question["excerpt"], "対象の段落。著者の解釈を示す。")
        self.assertEqual(question["excerpt_scope"], "paragraph")
        self.assertEqual(question["target_id"], "P-review-plan-section-body-001-001")
        self.assertEqual(question["source_ids"], ["SRC-014"])
        self.assertNotIn("required source", question["question"])
        self.assertIn("required source", question["reason"])
        self.assertEqual(
            question["context_note"], "確認したい主張：仮想展示の配置は解釈に影響する。",
        )
        self.assertEqual(coherence["question_id"], "coherence:coherence-1")
        self.assertEqual(coherence["target"], "仮想展示の配置")
        self.assertEqual(coherence["excerpt"], question["excerpt"])
        self.assertTrue(coherence["question"].endswith("ありますか？"))
        self.assertEqual(memo["target"], "元メモの整理内容")
        self.assertEqual(memo["excerpt_scope"], "planning")
        self.assertEqual(memo["excerpt"], "架空の展示計画を記したメモ。")
        self.assertEqual(claim["excerpt"], "仮想展示の配置に関する比較の主張。")
        self.assertTrue(claim["question"].endswith("ありますか？"))
        again = self.client.get("/api/reviews/review-authoring-review/feedback").json()
        self.assertEqual(again["questions"], packet["questions"])
        from fukamiru.review_feedback import ReviewFeedbackService

        with self.app.state.databases.state.connect() as connection:
            review = dict(connection.execute("SELECT * FROM human_reviews WHERE id=?",
                                             ("review-authoring-review",)).fetchone())
            current, _ = ReviewFeedbackService(connection)._packet(
                review, {**context, "footnoted_coherence_unresolved_routes": []},
            )
            self.assertEqual(len(current), 3)
            self.assertNotIn("coherence:coherence-1", [item["question_id"] for item in current])
            legacy = packet["questions"]
            legacy[0].pop("presentation_version")
            legacy[0].pop("category")
            legacy[0]["question"] = (
                "必須引用の対応を確認してください: required source has no mapping？"
            )
            connection.execute("UPDATE review_feedback SET questions_json=?,answers_json=?,"
                               "revision=3 WHERE review_id=?",
                               (json.dumps(legacy), json.dumps([{
                                   "question_id": "readable-question", "answer": "著者の回答",
                                   "llm_overreach": False,
                               }]), "review-authoring-review"))
            connection.commit()
        restored = self.client.get("/api/reviews/review-authoring-review/feedback").json()
        self.assertEqual(restored["revision"], 3)
        self.assertEqual(restored["answers"][0]["answer"], "著者の回答")
        self.assertEqual(restored["questions"][0]["question_id"], "readable-question")
        self.assertEqual(restored["questions"][0]["excerpt"], question["excerpt"])
        self.assertNotIn("required source", restored["questions"][0]["question"])
        self.assertIn("required source", restored["questions"][0]["recorded_question"])
        with self.app.state.databases.state.connect() as connection:
            stored = json.loads(connection.execute("SELECT questions_json FROM review_feedback "
                "WHERE review_id=?", ("review-authoring-review",)).fetchone()[0])
            self.assertEqual(stored, legacy)
        sent = self.client.put("/api/reviews/review-authoring-review/feedback", json={
            "revision": 3, "submit": True, "answers": [{
                "question_id": "readable-question", "llm_overreach": True,
            }],
        })
        self.assertEqual(sent.status_code, 200, sent.text)
        with self.app.state.databases.state.connect() as connection:
            current, _ = ReviewFeedbackService(connection)._packet(review, context)
            self.assertEqual(len(current), 3)
            self.assertNotIn("readable-question", [item["question_id"] for item in current])

    def test_review_drafts_do_not_change_submitted_feedback_or_approval(self) -> None:
        self._create_authoring_review_fixture()
        url = "/api/reviews/review-authoring-review/feedback"
        packet = self.client.get(url).json()
        question_id = packet["questions"][0]["question_id"]
        for revision, text, submit in ((0, "送信する回答", True), (1, "未送信の変更", False)):
            saved = self.client.put(url, json={
                "revision": revision, "answers": [{"question_id": question_id, "answer": text}],
                "submit": submit,
            })
            self.assertEqual(saved.status_code, 200, saved.text)
        from fukamiru.review_feedback import feedback_context

        with self.app.state.databases.state.connect() as connection:
            feedback = feedback_context(connection, packet["run_id"])
        self.assertEqual(feedback["answers"][0]["answer"], "送信する回答")
        approved = self.client.post("/api/reviews/review-authoring-review/resolve",
                                    json={"decision": "approve"})
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()["status"], "completed")

    def test_authoring_review_approval_confirms_proposal_and_projects_output(self) -> None:
        target_document_id, artifact_path = self._create_authoring_review_fixture()
        ledger = self.client.get(
            "/api/reviews/review-authoring-review/argument-ledger"
        )
        self.assertEqual(ledger.status_code, 200, ledger.text)
        self.assertEqual(ledger.json()["claims"][0]["claim_id"], "c1")
        evidence_review = self.client.get(
            "/api/reviews/review-authoring-review/evidence-review"
        )
        self.assertEqual(evidence_review.status_code, 200, evidence_review.text)
        self.assertEqual(evidence_review.json()["schema_version"], "31.1")
        self.assertEqual(
            evidence_review.json()["result_status"], "completed_with_review"
        )
        self.assertEqual(
            evidence_review.json()["review_items"][0]["sentence_id"],
            "S005-P02-S03",
        )
        unknown_claim = self.client.post(
            "/api/reviews/review-authoring-review/resolve",
            json={"decision": "reject", "claim_id": "missing", "feedback": "根拠違い"},
        )
        self.assertEqual(unknown_claim.status_code, 400, unknown_claim.text)

        approved = self.client.post(
            "/api/reviews/review-authoring-review/resolve",
            json={"decision": "approve"},
        )

        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()["status"], "completed")
        refreshed = self.client.get(
            "/api/authoring/proposals/review-authoring-proposal"
        ).json()
        self.assertEqual(refreshed["status"], "confirmed")
        self.assertIsNotNone(refreshed["confirmed_revision_id"])
        history = self.client.get(f"/api/documents/{target_document_id}/revisions").json()
        self.assertEqual(len(history), 1)
        approved_reviews = self.client.get("/api/reviews?status=approved").json()
        review = next(
            item for item in approved_reviews if item["id"] == "review-authoring-review"
        )
        self.assertEqual(review["source_title"], "提出原稿")
        self.assertEqual(review["project_name"], "Authoring review project")
        self.assertEqual(review["output_path"], str(artifact_path))
        self.assertEqual(review["output_status"], "ready")
        self.assertEqual(
            review["confirmed_revision_id"], refreshed["confirmed_revision_id"]
        )

    def test_authoring_plan_reviews_show_their_own_audit_targets(self) -> None:
        target_document_id, _ = self._create_authoring_review_fixture()
        with self.app.state.databases.state.connect() as connection:
            run_id = connection.execute(
                "SELECT run_id FROM human_reviews WHERE id = 'review-authoring-review'"
            ).fetchone()["run_id"]
            for attempt, section_id, section_title, message in (
                (1, "sec-03", "自己投影", "第3節の仮定を確認する"),
                (2, "sec-05", "古書店の棚", "第5節の資料が不足している"),
            ):
                output = PayloadStore(connection).pack({
                    "section_contracts": [{
                        "section_id": section_id,
                        "title": section_title,
                        "notes": "x" * 9000 if attempt == 2 else "",
                    }],
                    "audit_findings": [{
                        "code": "missing_evidence",
                        "severity": "major",
                        "object_id": section_id,
                        "message": message,
                    }],
                })
                connection.execute(
                    "INSERT INTO node_runs(id,run_id,node_id,attempt,status,output_json,"
                    "started_at,completed_at) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        f"review-audit-{attempt}", run_id, "audit_structure", attempt,
                        "completed", json.dumps(output, ensure_ascii=False),
                        f"2026-09-30T12:{attempt}0:00.000Z",
                        f"2026-09-30T12:{attempt}0:02.000Z",
                    ),
                )
                connection.execute(
                    "INSERT INTO human_reviews(id,run_id,node_run_id,object_type,"
                    "object_id,question,options_json,status,created_at) "
                    "VALUES (?,?,NULL,'authoring_plan','review-authoring-plan',"
                    "'構造監査を確認','[]','open',?)",
                    (
                        f"review-audit-question-{attempt}", run_id,
                        f"2026-09-30T12:{attempt}0:01.000Z",
                    ),
                )
            connection.commit()

        response = self.client.get("/api/reviews?status=open")
        self.assertEqual(response.status_code, 200, response.text)
        reviews = {item["id"]: item for item in response.json()}
        for attempt, section_id, section_title, message in (
            (1, "sec-03", "自己投影", "第3節の仮定を確認する"),
            (2, "sec-05", "古書店の棚", "第5節の資料が不足している"),
        ):
            review = reviews[f"review-audit-question-{attempt}"]
            self.assertEqual(review["document_id"], target_document_id)
            self.assertEqual(review["source_title"], "提出原稿")
            self.assertEqual(review["audit_attempt"], attempt)
            self.assertEqual(review["audit_findings"], [{
                "severity": "major",
                "code": "missing_evidence",
                "object_id": section_id,
                "section_title": section_title,
                "message": message,
            }])

        rejected = self.client.post(
            "/api/reviews/review-audit-question-1/resolve",
            json={"decision": "reject"},
        )
        self.assertEqual(rejected.status_code, 200, rejected.text)
        history = self.client.get("/api/reviews?status=rejected")
        self.assertEqual(history.status_code, 200, history.text)
        archived = next(item for item in history.json() if item["id"] == "review-audit-question-1")
        self.assertEqual(archived["source_title"], "提出原稿")
        self.assertNotIn("audit_findings", archived)

    def test_authoring_review_returns_a_specific_claim_with_feedback(self) -> None:
        target_document_id, _ = self._create_authoring_review_fixture()
        missing_feedback = self.client.post(
            "/api/reviews/review-authoring-review/resolve",
            json={"decision": "reject", "claim_id": "c1"},
        )
        self.assertEqual(missing_feedback.status_code, 400, missing_feedback.text)

        rejected = self.client.post(
            "/api/reviews/review-authoring-review/resolve",
            json={"decision": "reject", "claim_id": "c1", "feedback": "根拠の範囲を再確認"},
        )
        self.assertEqual(rejected.status_code, 200, rejected.text)
        self.assertEqual(rejected.json()["status"], "failed")
        proposal = self.client.get(
            "/api/authoring/proposals/review-authoring-proposal"
        ).json()
        self.assertEqual(proposal["status"], "rejected")
        with self.app.state.databases.state.connect() as connection:
            response = connection.execute(
                "SELECT response_json FROM human_reviews WHERE id = ?",
                ("review-authoring-review",),
            ).fetchone()
        self.assertEqual(json.loads(response["response_json"])["claim_id"], "c1")
        self.assertEqual(
            json.loads(response["response_json"])["feedback"], "根拠の範囲を再確認"
        )
        history = self.client.get(f"/api/documents/{target_document_id}/revisions")
        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual(history.json(), [])

    def test_pruned_event_history_requests_snapshot_resync(self) -> None:
        created = self.client.post(
            "/api/runs",
            json={"workflow_name": "ingestion", "inputs": {"source_path": "D:/source.pdf"}},
        ).json()
        with self.app.state.databases.state.connect() as connection:
            events = EventRepository(connection)
            events.append(created["run_id"], "node.started")
            connection.execute(
                "DELETE FROM event_log WHERE run_id = ? AND event_id <= ?",
                (created["run_id"], created["event_id"] + 1),
            )
            connection.commit()

        response = self.client.get(created["events_url"] + "?after=1")
        self.assertEqual(response.status_code, 200)
        self.assertIn("event: sync.required", response.text)
        self.assertIn(created["snapshot_url"], response.text)


if __name__ == "__main__":
    unittest.main()
