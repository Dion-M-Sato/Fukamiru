from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fukamiru.bootstrap import bootstrap
from fukamiru.config import load_settings
from fukamiru.evaluation import EvaluationService, RepairService
from fukamiru.knowledge.service import IngestionService
from fukamiru.revisions.service import RevisionService
from fukamiru.storage.database import DatabasePair
from fukamiru.storage.runs import RunRepository, utc_now


class EvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        config = root / "local.toml"
        config.write_text(
            f"""
[server]
open_browser = false
[paths]
state_db = "{(root / 'data/state.db').as_posix()}"
knowledge_db = "{(root / 'data/knowledge.db').as_posix()}"
runtime_root = "{(root / 'runtime').as_posix()}"
corpus_root = "{(root / 'corpus').as_posix()}"
ocr_root = "{(root / 'ocr').as_posix()}"
knowledge_artifacts_root = "{(root / 'kb').as_posix()}"
faiss_root = "{(root / 'faiss').as_posix()}"
[retrieval]
require_cuda = false
embedding_device = "cpu"
tokenizer_mode = "unicode"
""",
            encoding="utf-8",
        )
        self.settings = load_settings(config)
        bootstrap(self.settings)
        self.databases = DatabasePair.from_settings(self.settings)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_claim_and_direct_quote_defects_are_persisted_with_provenance(self) -> None:
        source_path = Path(self.temp.name) / "source.txt"
        source_path.write_text("Original exact quotation.", encoding="utf-8")
        with self.databases.knowledge.connect() as knowledge:
            ingested = IngestionService(knowledge, self.settings).ingest_path(
                str(source_path), source_language="en"
            )
            span = knowledge.execute(
                "SELECT id FROM source_spans WHERE document_unit_id = ? LIMIT 1",
                (ingested["document_unit_id"],),
            ).fetchone()

        with self.databases.state.connect() as state:
            revisions = RevisionService(state)
            project_id = revisions.register_project(
                name="Evaluation fixture", root_path=self.temp.name
            )
            document_id = revisions.create_document(
                project_id=project_id, kind="paper", title="Fixture"
            )
            revision = revisions.commit_revision(
                document_id=document_id,
                canonical_html=(
                    '<article id="DOC-EVAL" lang="en"><section id="SEC-EVAL">'
                    '<h2 id="H-EVAL">Evidence</h2>'
                    '<p id="P-EVAL">A test claim. Altered quotation.</p></section></article>'
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
                INSERT INTO claims(
                    id, document_id, revision_id, block_id, claim_type,
                    text, modality, status, created_at
                ) VALUES ('claim-eval', ?, ?, 'P-EVAL', 'assertion',
                          'A test claim.', 'assertive', 'proposed', ?)
                """,
                (document_id, revision.revision_id, utc_now()),
            )
            state.execute(
                """
                INSERT INTO claims(
                    id, document_id, revision_id, block_id, claim_type,
                    text, modality, status, created_at
                ) VALUES ('claim-contradicted', ?, ?, 'P-EVAL', 'assertion',
                          'A contradicted claim.', 'assertive', 'supported', ?)
                """,
                (document_id, revision.revision_id, utc_now()),
            )
            state.execute(
                """
                INSERT INTO evidence_links(
                    id, claim_id, source_span_id, relation_type, note, created_at
                ) VALUES ('evidence-contradicted', 'claim-contradicted', ?,
                          'contradicts', '', ?)
                """,
                (span["id"], utc_now()),
            )
            state.executemany(
                """
                INSERT INTO claim_relations(
                    source_claim_id, target_claim_id, relation_type, rationale
                ) VALUES (?, ?, 'depends_on', 'fixture cycle')
                """,
                (
                    ("claim-eval", "claim-contradicted"),
                    ("claim-contradicted", "claim-eval"),
                ),
            )
            state.execute(
                """
                INSERT INTO citation_events(
                    id, revision_id, block_id, source_span_id, citation_kind,
                    rendered_text, created_at
                ) VALUES ('citation-eval', ?, 'P-EVAL', ?, 'quote',
                          'Altered quotation.', ?)
                """,
                (revision.revision_id, span["id"], utc_now()),
            )
            state.commit()

        with (
            self.databases.state.connect() as state,
            self.databases.knowledge.connect() as knowledge,
        ):
            result = EvaluationService(state, knowledge).evaluate(
                run_id=run.run_id,
                revision_id=revision.revision_id,
                max_repair_attempts=2,
            )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["summary"]["finding_count"], 4)
        self.assertEqual(
            {defect["defect_type"] for defect in result["defects"]},
            {
                "unsupported_claim",
                "direct_quote_mismatch",
                "claim_evidence_relation_mismatch",
                "claim_dependency_cycle",
            },
        )
        quote = next(
            defect
            for defect in result["defects"]
            if defect["defect_type"] == "direct_quote_mismatch"
        )
        self.assertEqual(quote["source_span_id"], span["id"])
        self.assertEqual(quote["source_asset_id"], ingested["source_asset_id"])
        unsupported = next(
            defect
            for defect in result["defects"]
            if defect["defect_type"] == "unsupported_claim"
        )

        with (
            self.databases.state.connect() as state,
            self.databases.knowledge.connect() as knowledge,
        ):
            waiting = RepairService(state, knowledge).repair(
                evaluation_id=result["id"],
                defect_id=unsupported["id"],
            )
            repaired = RepairService(state, knowledge).repair(
                evaluation_id=result["id"],
                defect_id=quote["id"],
            )
            repaired_html = RevisionService(state).canonical_html(
                repaired["output_revision_id"]
            )
            reevaluated = EvaluationService(state, knowledge).evaluate(
                run_id=run.run_id,
                revision_id=repaired["output_revision_id"],
                max_repair_attempts=1,
            )
            review = state.execute(
                "SELECT * FROM human_reviews WHERE object_id = ?",
                (waiting["id"],),
            ).fetchone()

        self.assertEqual(waiting["status"], "waiting_human")
        self.assertIsNotNone(review)
        self.assertEqual(repaired["status"], "completed")
        self.assertIn("Original exact quotation.", repaired_html)
        self.assertNotIn("Altered quotation.", repaired_html)
        self.assertEqual(
            {defect["defect_type"] for defect in reevaluated["defects"]},
            {
                "unsupported_claim",
                "claim_evidence_relation_mismatch",
                "claim_dependency_cycle",
            },
        )


if __name__ == "__main__":
    unittest.main()
