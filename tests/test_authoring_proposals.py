from __future__ import annotations

import hashlib
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from fukamiru.authoring.models import AuthoringPlan, ResearchQuestion
from fukamiru.authoring.repository import AuthoringRepository
from fukamiru.authoring.service import AuthoringProposalService
from fukamiru.errors import FukamiruError
from fukamiru.revisions.service import RevisionService
from fukamiru.storage.database import SQLiteDatabase


class AuthoringProposalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(__file__).resolve().parents[1]
        self.database = SQLiteDatabase(
            Path(self.temp.name) / "state.db", root / "migrations" / "state"
        )
        self.database.migrate()
        with closing(self.database.connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO projects(id, name, root_path, created_at, updated_at)
                VALUES ('project', 'Project', '/project', 'now', 'now')
                """
            )
            revisions = RevisionService(connection)
            self.source_document_id = revisions.create_document(
                project_id="project", kind="note", title="Source note", document_id="source-note"
            )
            source = revisions.commit_revision(
                document_id=self.source_document_id,
                canonical_html=(
                    '<article id="source-article" lang="en"><section id="source-section">'
                    '<h2 id="source-heading">Source</h2><p id="source-paragraph">Note.</p>'
                    "</section></article>"
                ),
                editor_type="human",
                operation="create",
            )
            self.source_revision_id = source.revision_id
            self.target_document_id = revisions.create_document(
                project_id="project",
                kind="paper",
                title="Generated paper",
                document_id="target-paper",
            )
            connection.execute(
                """
                INSERT INTO runs(
                    id, project_id, workflow_name, workflow_version, status, created_at
                ) VALUES ('run', 'project', 'authoring', '30', 'completed', 'now')
                """
            )
            connection.commit()
        self.repository = AuthoringRepository(self.database)
        self.repository.create_plan(
            AuthoringPlan(
                id="plan",
                document_id=self.target_document_id,
                source_revision_id=self.source_revision_id,
                revision=1,
                research_question=ResearchQuestion(text="What does the note explain?"),
            )
        )
        self.proposed_html = (
            '<article id="paper-article" lang="en"><section id="paper-section">'
            '<h2 id="paper-heading">Generated</h2>'
            '<p id="paper-paragraph">A proposed paper.</p></section></article>'
        )
        self.proposal_id = "proposal"
        self.repository.create_pending_proposal(
            proposal_id=self.proposal_id,
            run_id="run",
            project_id="project",
            source_document_id=self.source_document_id,
            source_revision_id=self.source_revision_id,
            target_document_id=self.target_document_id,
            plan_id="plan",
            plan_revision=1,
            title="Generated paper",
            proposed_html=self.proposed_html,
            artifact_path="output/authoring.final.html",
            quality_status="ready",
            review_context={"required_user_information": []},
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_candidate_is_pending_and_target_has_no_revision(self) -> None:
        proposal = self.repository.get_pending_proposal(self.proposal_id)
        self.assertIsNotNone(proposal)
        assert proposal is not None
        self.assertEqual("pending", proposal["status"])
        self.assertEqual("ready", proposal["quality_status"])
        self.assertEqual(
            {"required_user_information": []}, proposal["review_context"]
        )
        self.assertEqual(
            hashlib.sha256(self.proposed_html.encode()).hexdigest(),
            proposal["proposed_sha256"],
        )
        with closing(self.database.connect()) as connection, connection:
            row = connection.execute(
                "SELECT current_revision_id FROM content_documents WHERE id = ?",
                (self.target_document_id,),
            ).fetchone()
        self.assertIsNone(row["current_revision_id"])

    def test_submission_revalidation_replaces_same_pending_proposal(self) -> None:
        revised_html = self.proposed_html.replace(
            "A proposed paper.", "A revalidated proposed paper."
        )

        proposal = self.repository.create_pending_proposal(
            proposal_id=self.proposal_id,
            run_id="run",
            project_id="project",
            source_document_id=self.source_document_id,
            source_revision_id=self.source_revision_id,
            target_document_id=self.target_document_id,
            plan_id="plan",
            plan_revision=1,
            title="Generated paper",
            proposed_html=revised_html,
            artifact_path="output/authoring.revalidated.html",
            quality_status="needs_human_review",
            review_context={"revalidated": True},
            replace_existing_pending=True,
        )

        self.assertEqual(self.proposal_id, proposal["id"])
        self.assertEqual(revised_html, proposal["proposed_html"])
        self.assertEqual("needs_human_review", proposal["quality_status"])
        self.assertEqual({"revalidated": True}, proposal["review_context"])
        with closing(self.database.connect()) as connection, connection:
            archived = connection.execute(
                "SELECT proposed_html,quality_status FROM "
                "authoring_proposal_revalidations WHERE proposal_id=?",
                (self.proposal_id,),
            ).fetchone()
        self.assertEqual(self.proposed_html, archived["proposed_html"])
        self.assertEqual("ready", archived["quality_status"])

    def test_confirmation_creates_first_revision_and_is_idempotent(self) -> None:
        with closing(self.database.connect()) as connection, connection:
            service = AuthoringProposalService(connection)
            expected_hash = hashlib.sha256(self.proposed_html.encode()).hexdigest()
            confirmed = service.confirm(self.proposal_id, expected_sha256=expected_hash)
            repeated = service.confirm(self.proposal_id)
            current = connection.execute(
                "SELECT current_revision_id FROM content_documents WHERE id = ?",
                (self.target_document_id,),
            ).fetchone()["current_revision_id"]
            history = RevisionService(connection).history(self.target_document_id)
        self.assertEqual("confirmed", confirmed["status"])
        self.assertEqual(confirmed, repeated)
        self.assertEqual(confirmed["revision_id"], current)
        self.assertEqual(1, len(history))
        self.assertEqual("create", history[0]["operation"])

    def test_confirmation_rejects_stale_hash_without_creating_revision(self) -> None:
        with closing(self.database.connect()) as connection, connection:
            with self.assertRaisesRegex(FukamiruError, "changed"):
                AuthoringProposalService(connection).confirm(
                    self.proposal_id, expected_sha256="0" * 64
                )
            current = connection.execute(
                "SELECT current_revision_id FROM content_documents WHERE id = ?",
                (self.target_document_id,),
            ).fetchone()["current_revision_id"]
        self.assertIsNone(current)

    def test_rejection_is_idempotent_and_creates_no_revision(self) -> None:
        with closing(self.database.connect()) as connection, connection:
            service = AuthoringProposalService(connection)
            rejected = service.reject(self.proposal_id)
            repeated = service.reject(self.proposal_id)
            current = connection.execute(
                "SELECT current_revision_id FROM content_documents WHERE id = ?",
                (self.target_document_id,),
            ).fetchone()["current_revision_id"]
        self.assertEqual("rejected", rejected["status"])
        self.assertEqual(rejected, repeated)
        self.assertIsNone(current)

    def test_pending_proposal_must_target_uninitialized_paper(self) -> None:
        with closing(self.database.connect()) as connection, connection:
            RevisionService(connection).commit_revision(
                document_id=self.target_document_id,
                canonical_html=(
                    '<article id="other-article" lang="en"><section id="other-section">'
                    '<h2 id="other-heading">Other</h2></section></article>'
                ),
                editor_type="human",
                operation="create",
            )
        with self.assertRaisesRegex(ValueError, "must not have a revision"):
            self.repository.create_pending_proposal(
                proposal_id="proposal-2",
                run_id="run-2",
                project_id="project",
                source_document_id=self.source_document_id,
                source_revision_id=self.source_revision_id,
                target_document_id=self.target_document_id,
                plan_id="plan",
                plan_revision=1,
                title="Second proposal",
                proposed_html=self.proposed_html,
            )
        self.assertIsNone(self.repository.get_pending_proposal("proposal-2"))

    def test_unknown_proposal_fails_cleanly(self) -> None:
        with (
            closing(self.database.connect()) as connection,
            connection,
            self.assertRaisesRegex(FukamiruError, "Unknown authoring proposal"),
        ):
            AuthoringProposalService(connection).confirm("missing")


if __name__ == "__main__":
    unittest.main()
