from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fukamiru.projects.dissertation import DissertationService
from fukamiru.revisions.service import RevisionService
from fukamiru.storage.database import SQLiteDatabase
from fukamiru.storage.runs import RunRepository, utc_now


class DissertationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        migrations = Path(__file__).resolve().parents[1] / "migrations" / "state"
        self.database = SQLiteDatabase(Path(self.temp.name) / "state.db", migrations)
        self.database.migrate()
        self.connection = self.database.connect()
        self.revisions = RevisionService(self.connection)
        self.project_id = self.revisions.register_project(
            name="Dissertation fixture", root_path=self.temp.name
        )

    def tearDown(self) -> None:
        self.connection.close()
        self.temp.cleanup()

    def test_membership_pins_revision_and_creates_change_request_for_update(self) -> None:
        source_document_id = self.revisions.create_document(
            project_id=self.project_id, kind="paper", title="Existing paper"
        )
        revision_one = self.revisions.commit_revision(
            document_id=source_document_id,
            canonical_html=(
                '<article id="DOC-PAPER" lang="en"><section id="SEC-PAPER">'
                '<h2 id="H-PAPER">Paper</h2>'
                '<p id="P-PAPER">Version one.</p></section></article>'
            ),
            editor_type="agent",
            operation="create",
        )
        dissertations = DissertationService(self.connection)
        dissertation = dissertations.create(
            project_id=self.project_id, title="Thesis"
        )
        dissertation_id = dissertation["document"]["id"]
        root_id = dissertation["nodes"][0]["id"]
        chapter = dissertations.add_outline_node(
            dissertation_document_id=dissertation_id,
            parent_id=root_id,
            node_type="chapter",
            title="Chapter 1",
            order_index=0,
        )
        membership = dissertations.include_revision(
            dissertation_document_id=dissertation_id,
            outline_node_id=chapter["id"],
            content_document_id=source_document_id,
            included_revision_id=revision_one.revision_id,
            role="chapter",
            order_index=0,
        )

        revision_two = self.revisions.commit_revision(
            document_id=source_document_id,
            canonical_html=(
                '<article id="DOC-PAPER" lang="en"><section id="SEC-PAPER">'
                '<h2 id="H-PAPER">Paper</h2>'
                '<p id="P-PAPER">Version two.</p></section></article>'
            ),
            editor_type="agent",
            operation="agent_edit",
        )
        changes = dissertations.refresh_updates(dissertation_id)
        snapshot = dissertations.snapshot(dissertation_id)

        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["included_revision_id"], revision_one.revision_id)
        self.assertEqual(changes[0]["current_revision_id"], revision_two.revision_id)
        self.assertEqual(snapshot["memberships"][0]["id"], membership["id"])
        self.assertEqual(
            snapshot["memberships"][0]["included_revision_id"],
            revision_one.revision_id,
        )
        self.assertEqual(snapshot["memberships"][0]["status"], "update_available")
        change_request = self.connection.execute(
            "SELECT * FROM change_requests WHERE object_id = ?", (membership["id"],)
        ).fetchone()
        self.assertEqual(change_request["status"], "open")

        run = RunRepository(self.connection).create(
            workflow_name="translation",
            workflow_version="test",
            entry_node="ensure_ingested",
            project_id=self.project_id,
            inputs={"source_path": "fixture.pdf"},
        )
        evaluation = dissertations.evaluate_consistency(
            run_id=run.run_id,
            dissertation_document_id=dissertation_id,
        )
        self.assertEqual(evaluation["status"], "completed")
        self.assertEqual(evaluation["summary"]["pinned_revision_count"], 1)
        self.assertIn(
            "dissertation_pinned_revision_stale",
            {defect["defect_type"] for defect in evaluation["defects"]},
        )
        restored = dissertations.get_consistency_evaluation(evaluation["id"])
        self.assertEqual(restored["summary"], evaluation["summary"])

    def test_consistency_detects_unfilled_content_node(self) -> None:
        dissertations = DissertationService(self.connection)
        dissertation = dissertations.create(
            project_id=self.project_id, title="Thesis"
        )
        dissertation_id = dissertation["document"]["id"]
        dissertations.add_outline_node(
            dissertation_document_id=dissertation_id,
            parent_id=dissertation["nodes"][0]["id"],
            node_type="chapter",
            title="Empty chapter",
            order_index=0,
        )
        run = RunRepository(self.connection).create(
            workflow_name="translation",
            workflow_version="test",
            entry_node="ensure_ingested",
            project_id=self.project_id,
            inputs={},
        )

        evaluation = dissertations.evaluate_consistency(
            run_id=run.run_id,
            dissertation_document_id=dissertation_id,
        )

        self.assertEqual(evaluation["summary"]["finding_count"], 1)
        self.assertEqual(
            evaluation["defects"][0]["defect_type"],
            "dissertation_outline_content_missing",
        )

    def test_csv_preview_and_confirm_persist_generation_directives(self) -> None:
        dissertations = DissertationService(self.connection)
        dissertation = dissertations.create(project_id=self.project_id, title="Thesis")
        dissertation_id = dissertation["document"]["id"]
        csv_path = Path(self.temp.name) / "outline.csv"
        csv_path.write_text(
            "種別,章タイトル,概要,執筆指示,目標文字数,親\n"
            "部,理論篇,理論的枠組み,,,\n"
            "章,メディア論,媒体概念を整理する,一次資料を優先,20000,理論篇\n"
            "節,ゲーム研究,研究史を検討する,論争点を比較,,メディア論\n",
            encoding="utf-8-sig",
        )

        preview = dissertations.preview_outline_csv(
            dissertation_document_id=dissertation_id,
            source_path=str(csv_path),
            total_target_characters=30000,
        )

        self.assertEqual(preview["status"], "previewed")
        self.assertEqual(preview["row_count"], 3)
        self.assertEqual(preview["target_characters"], 30000)
        self.assertEqual(dissertations.snapshot(dissertation_id)["nodes"], dissertation["nodes"])

        result = dissertations.confirm_outline_csv(
            dissertation_document_id=dissertation_id,
            import_id=preview["id"],
        )
        nodes = result["dissertation"]["nodes"]
        generated = {node["title"]: node for node in nodes if node["node_type"] != "root"}
        self.assertEqual(set(generated), {"理論篇", "メディア論", "ゲーム研究"})
        self.assertEqual(generated["メディア論"]["directive"]["target_characters"], 20000)
        self.assertEqual(generated["ゲーム研究"]["directive"]["target_characters"], 10000)
        self.assertEqual(
            generated["ゲーム研究"]["parent_id"], generated["メディア論"]["id"]
        )
        self.assertEqual(result["import"]["status"], "applied")

        repeated = dissertations.confirm_outline_csv(
            dissertation_document_id=dissertation_id,
            import_id=preview["id"],
        )
        self.assertEqual(len(repeated["dissertation"]["nodes"]), 4)

    def test_consistency_checks_claims_across_pinned_revisions(self) -> None:
        dissertations = DissertationService(self.connection)
        dissertation = dissertations.create(project_id=self.project_id, title="Thesis")
        dissertation_id = dissertation["document"]["id"]
        root_id = dissertation["nodes"][0]["id"]
        revision_ids: list[str] = []
        document_ids: list[str] = []
        claim_ids = ["claim-chapter-a", "claim-chapter-b"]
        for index in range(2):
            document_id = self.revisions.create_document(
                project_id=self.project_id, kind="paper", title=f"Paper {index}"
            )
            revision = self.revisions.commit_revision(
                document_id=document_id,
                canonical_html=(
                    f'<article id="DOC-{index}" lang="en"><section id="SEC-{index}">'
                    f'<h2 id="H-{index}">Chapter</h2>'
                    f'<p id="P-{index}">Shared claim.</p></section></article>'
                ),
                editor_type="agent",
                operation="create",
            )
            chapter = dissertations.add_outline_node(
                dissertation_document_id=dissertation_id,
                parent_id=root_id,
                node_type="chapter",
                title=f"Chapter {index + 1}",
                order_index=index,
            )
            dissertations.include_revision(
                dissertation_document_id=dissertation_id,
                outline_node_id=chapter["id"],
                content_document_id=document_id,
                included_revision_id=revision.revision_id,
                role="chapter",
                order_index=index,
            )
            document_ids.append(document_id)
            revision_ids.append(revision.revision_id)
            self.connection.execute(
                """
                INSERT INTO claims(
                    id, document_id, revision_id, block_id, claim_type,
                    text, modality, status, created_at
                ) VALUES (?, ?, ?, ?, 'assertion', 'Shared claim.', ?, ?, ?)
                """,
                (
                    claim_ids[index],
                    document_id,
                    revision.revision_id,
                    f"P-{index}",
                    "certain" if index == 0 else "possible",
                    "supported" if index == 0 else "rejected",
                    utc_now(),
                ),
            )
        self.connection.executemany(
            """
            INSERT INTO claim_relations(
                source_claim_id, target_claim_id, relation_type, rationale
            ) VALUES (?, ?, 'depends_on', 'cross-chapter fixture')
            """,
            ((claim_ids[0], claim_ids[1]), (claim_ids[1], claim_ids[0])),
        )
        self.connection.commit()
        run = RunRepository(self.connection).create(
            workflow_name="translation",
            workflow_version="test",
            entry_node="ensure_ingested",
            project_id=self.project_id,
            inputs={},
        )

        evaluation = dissertations.evaluate_consistency(
            run_id=run.run_id,
            dissertation_document_id=dissertation_id,
        )

        self.assertEqual(evaluation["summary"]["pinned_revision_count"], 2)
        self.assertEqual(
            {defect["defect_type"] for defect in evaluation["defects"]},
            {
                "dissertation_claim_status_conflict",
                "dissertation_claim_modality_conflict",
                "dissertation_claim_dependency_cycle",
            },
        )


if __name__ == "__main__":
    unittest.main()
