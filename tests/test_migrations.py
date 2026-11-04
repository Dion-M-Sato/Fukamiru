from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fukamiru.storage.database import SQLiteDatabase


class MigrationTests(unittest.TestCase):
    @property
    def migrations_root(self) -> Path:
        return Path(__file__).resolve().parents[1] / "migrations"

    def test_recovery_input_migration_preserves_original_job_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "legacy"
            legacy.mkdir()
            for migration in (self.migrations_root / "state").glob("*.sql"):
                if migration.name < "0050_":
                    shutil.copy2(migration, legacy / migration.name)
            database = SQLiteDatabase(root / "state.db", legacy)
            database.migrate()
            with database.connect() as connection:
                connection.execute(
                    "INSERT INTO runs(id,workflow_name,workflow_version,status,input_json,"
                    "created_at) VALUES ('run','corpus_sync','2','running','{}','now')"
                )
                connection.execute(
                    "INSERT INTO pdf_ocr_jobs(id,run_id,source_path,source_sha256,"
                    "source_language,engine,status,created_at,updated_at) "
                    "VALUES ('job','run','/original.pdf',?,'ja','ndlocr_lite',"
                    "'queued','now','now')",
                    ("a" * 64,),
                )
                connection.commit()
                before = dict(connection.execute("SELECT * FROM pdf_ocr_jobs").fetchone())
            current = SQLiteDatabase(root / "state.db", self.migrations_root / "state")
            self.assertEqual(
                current.migrate(), ["0050_pdf_ocr_recovery_inputs", "0051_corpus_sync_skips",
                                    "0052_research_source_handoffs"]
            )
            with current.connect() as connection:
                after = dict(connection.execute("SELECT * FROM pdf_ocr_jobs").fetchone())
                self.assertEqual({key: after[key] for key in before}, before)
                for key in (
                    "input_pdf_path",
                    "input_pdf_sha256",
                    "recovery_kind",
                    "recovery_manifest_path",
                ):
                    self.assertIsNone(after[key])
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_state_schema_is_created_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = SQLiteDatabase(
                Path(directory) / "state.db",
                self.migrations_root / "state",
            )
            self.assertEqual(
                database.migrate(),
                [
                    "0001_initial",
                    "0002_revision_documents",
                    "0003_run_artifacts",
                    "0004_corpus_sync",
                    "0005_embedding_progress",
                    "0006_embedding_metrics",
                    "0007_embedding_padding_metrics",
                    "0008_node_resource_leases",
                    "0009_defect_provenance",
                    "0010_run_documents",
                    "0011_evaluation_runs",
                    "0012_dissertation_evaluations",
                    "0013_html_import_previews",
                    "0014_dissertation_outline_csv",
                    "0015_project_lifecycle",
                    "0016_manuscript_import_previews",
                    "0017_manuscript_rewrite_proposals",
                    "0018_manuscript_reasoned_revision",
                    "0019_manuscript_coherence_plans",
                    "0020_manuscript_quality_gate",
                    "0021_manuscript_candidate_polish",
                    "0022_manuscript_coherence_checkpoints",
                    "0023_coherence_checkpoint_lineage",
                    "0024_manuscript_argument_checkpoints",
                    "0025_generation_attempt_ledger",
                    "0026_workflow_payloads",
                    "0027_manuscript_language_analyses",
                    "0028_manuscript_engine_authorizations",
                    "0029_v28_candidate_cardinality",
                    "0030_v29_collocation_scoring",
                    "0031_manuscript_proofreading_pipeline",
                    "0032_acrobat_ocr_jobs",
                    "0033_pdf_ocr_jobs",
                    "0034_argument_discourse",
                    "0035_authoring_pending_proposals",
                    "0036_authoring_proposal_review_context",
                    "0037_authoring_section_development",
                    "0037_pdf_ocr_language_decisions",
                    "0038_authoring_discourse_reconstruction",
                    "0038_llm_result_checkpoints",
                    "0040_authoring_proposal_revalidations",
                    "0042_llm_observation_runtime_details",
                    "0043_review_queue_projection",
                    "0044_authoring_submission_reviews",
                    "0045_authoring_submission_artifact_projection",
                    "0046_authoring_argument_decisions",
                    "0047_authoring_structure_aware_plan",
                    "0048_authoring_evidence_review",
                    "0049_pdf_ocr_multilingual",
                    "0049_v31_section_contract_zero_minima",
                    "0050_pdf_ocr_recovery_inputs",
                    "0051_corpus_sync_skips",
                    "0052_research_source_handoffs",
                ],
            )
            self.assertEqual(database.migrate(), [])
            required = {
                "projects",
                "runs",
                "event_log",
                "revisions",
                "claims",
                "dissertation_memberships",
                "corpus_sync_batches",
                "corpus_sync_items",
                "acrobat_ocr_batches",
                "acrobat_ocr_jobs",
                "pdf_ocr_jobs",
                "node_runs",
                "run_documents",
                "evaluation_runs",
                "evaluation_defects",
                "repair_attempts",
                "dissertation_evaluation_runs",
                "dissertation_evaluation_defects",
                "html_import_previews",
                "dissertation_outline_imports",
                "dissertation_outline_directives",
                "manuscript_import_previews",
                "manuscript_rewrite_proposals",
                "manuscript_reasoning_reviews",
                "manuscript_research_evidence",
                "manuscript_polish_checkpoints",
                "manuscript_structure_plans",
                "manuscript_argument_checkpoints",
                "manuscript_coherence_checks",
                "manuscript_coherence_checkpoints",
                "manuscript_polish_targets",
                "manuscript_polish_candidates",
                "manuscript_polish_generation_audits",
                "manuscript_polish_preferences",
                "manuscript_engine_authorizations",
                "manuscript_collocation_frames",
                "manuscript_collocation_candidates",
                "manuscript_collocation_probes",
                "manuscript_collocation_scores",
                "manuscript_collocation_semantic_verdicts",
                "manuscript_collocation_decisions",
                "manuscript_collocation_human_judgments",
                "manuscript_proofreading_sentences",
                "manuscript_proofreading_patches",
                "manuscript_proofreading_title_registry",
                "manuscript_luna_cache",
                "manuscript_luna_attempts",
                "manuscript_proofreading_logs",
                "authoring_plans",
                "authoring_plan_revisions",
                "atomic_propositions",
                "claim_tree_nodes",
                "argument_nodes",
                "argument_edges",
                "hypothesis_candidates",
                "discourse_nodes",
                "section_contracts",
                "section_drafts",
                "audit_findings",
                "authoring_fixed_asset_snapshots",
                "authoring_evidence_uses",
                "authoring_review_items",
                "authoring_pending_proposals",
                "llm_invocation_observations",
                "review_queue_entries",
            }
            self.assertTrue(required.issubset(database.table_names()))

    def test_multilingual_ocr_migration_preserves_existing_jobs(self) -> None:
        with sqlite3.connect(":memory:") as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("CREATE TABLE runs (id TEXT PRIMARY KEY)")
            connection.execute("CREATE TABLE corpus_sync_items (id TEXT PRIMARY KEY)")
            connection.executescript(
                (self.migrations_root / "state" / "0033_pdf_ocr_jobs.sql").read_text()
            )
            connection.execute("INSERT INTO runs(id) VALUES ('run-1')")
            connection.execute(
                """INSERT INTO pdf_ocr_jobs(
                    id, run_id, source_path, source_sha256, source_language,
                    status, created_at, updated_at
                ) VALUES ('job-1', 'run-1', 'old.pdf', ?, 'fr', 'completed', 't1', 't2')""",
                ("a" * 64,),
            )
            connection.executescript(
                (self.migrations_root / "state" / "0037_pdf_ocr_language_decisions.sql").read_text()
            )
            connection.executescript(
                (self.migrations_root / "state" / "0049_pdf_ocr_multilingual.sql").read_text()
            )
            old = connection.execute(
                "SELECT source_language, status, language_origin FROM pdf_ocr_jobs WHERE id='job-1'"
            ).fetchone()
            self.assertEqual(old, ("fr", "completed", "explicit"))
            connection.execute(
                """INSERT INTO pdf_ocr_jobs(
                    id, run_id, source_path, source_sha256, source_language,
                    status, created_at, updated_at
                ) VALUES ('job-2', 'run-1', 'new.pdf', ?, 'de', 'queued', 't3', 't3')""",
                ("b" * 64,),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE pdf_ocr_jobs SET source_language='es' WHERE id='job-2'")

    def test_knowledge_schema_is_created(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = SQLiteDatabase(
                Path(directory) / "knowledge.db",
                self.migrations_root / "knowledge",
            )
            self.assertEqual(
                database.migrate(),
                [
                    "0001_initial",
                    "0002_lexical_retrieval",
                    "0003_semantic_index",
                    "0004_embedding_cache",
                    "0005_embedding_metrics",
                    "0006_embedding_padding_metrics",
                    "0007_ocr_revisions",
                    "0008_proofreading_revisions",
                    "0009_translation_memory",
                    "0010_translation_provenance",
                    "0011_translation_batch_checkpoints",
                    "0012_translation_segment_alignments",
                    "0013_literature_discovery",
                    "0014_oa_acquisitions",
                    "0015_proofreading_chunk_checkpoints",
                    "0016_pdf_layout_structure",
                    "0017_translation_academic_research",
                    "0018_pdf_layout_artifact_role",
                    "0019_source_asset_bibliography",
                    "0020_structure_foreign_key_indexes",
                    "0021_ingestion_cascade_indexes",
                    "0022_lexical_source_rowids",
                    "0023_scalable_retrieval_groups",
                    "0024_retrieval_group_ranges",
                ],
            )
            self.assertEqual(database.migrate(), [])
            required = {
                "source_assets",
                "chunk_fts_sources",
                "document_units",
                "source_spans",
                "chunks",
                "translation_versions",
                "embedding_models",
                "chunk_embeddings",
                "embedding_cache",
                "embedding_builds",
                "embedding_batch_metrics",
                "embedding_resource_samples",
                "ocr_text_revisions",
                "ocr_revision_pages",
                "ocr_blocks",
                "ocr_page_boundaries",
                "ocr_notes",
                "ocr_note_references",
                "ocr_provenance_links",
                "proofreading_revisions",
                "proofreading_chunk_checkpoints",
                "translation_memory_entries",
                "translation_batch_alignments",
                "translation_batch_checkpoints",
                "translation_segment_alignments",
                "literature_searches",
                "literature_candidates",
                "bibliographic_provenance",
                "source_asset_bibliography",
                "oa_acquisitions",
                "pdf_layout_documents",
                "pdf_layout_pages",
                "pdf_layout_blocks",
                "translation_research_triggers",
                "translation_academic_evidence",
                "translation_term_decisions",
                "translation_compliance_findings",
            }
            self.assertTrue(required.issubset(database.table_names()))
            with database.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO source_assets(
                        id, sha256, byte_size, media_type, ingestion_status,
                        created_at, updated_at
                    ) VALUES ('asset', ?, 1, 'application/pdf', 'registered', 'now', 'now')
                    """,
                    ("a" * 64,),
                )
                connection.execute(
                    """
                    INSERT INTO pdf_layout_documents(
                        id, source_asset_id, analyzer_version, page_count, created_at
                    ) VALUES ('layout', 'asset', 'test', 1, 'now')
                    """
                )
                connection.execute(
                    """
                    INSERT INTO pdf_layout_pages(
                        id, layout_document_id, page_number, width, height,
                        rotation, created_at
                    ) VALUES ('page', 'layout', 1, 100, 100, 0, 'now')
                    """
                )
                connection.execute(
                    """
                    INSERT INTO pdf_layout_blocks(
                        id, layout_page_id, stable_block_id, structural_role,
                        ordinal, x0, y0, x1, y1, font_size, text,
                        confidence, translatable, created_at
                    ) VALUES (
                        'block', 'page', 'stable', 'artifact',
                        0, 0, 0, 10, 10, 10, '...', 0.98, 0, 'now'
                    )
                    """
                )

    def test_foreign_keys_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = SQLiteDatabase(
                Path(directory) / "state.db",
                self.migrations_root / "state",
            )
            database.migrate()
            with (
                database.connect() as connection,
                self.assertRaises(sqlite3.IntegrityError),
            ):
                connection.execute(
                    """
                    INSERT INTO content_documents(
                        id, project_id, kind, title, created_at, updated_at
                    ) VALUES ('doc', 'missing', 'paper', 'Title', 'now', 'now')
                    """
                )


if __name__ == "__main__":
    unittest.main()
