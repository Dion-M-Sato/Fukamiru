from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fukamiru.revisions.service import ProtectedBlockError, RevisionService
from fukamiru.storage.database import SQLiteDatabase

IMPORTED = """<article id="DOC-001" lang="ja"><section id="SEC-001">
<h2 id="H-001">章</h2><p id="P-001">人間が書いた本文。</p></section></article>"""


class RevisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        migration_root = Path(__file__).resolve().parents[1] / "migrations" / "state"
        self.database = SQLiteDatabase(Path(self.temp.name) / "state.db", migration_root)
        self.database.migrate()
        self.connection = self.database.connect()
        self.service = RevisionService(self.connection)
        self.project_id = self.service.register_project(name="Test", root_path=self.temp.name)
        self.document_id = self.service.create_document(
            project_id=self.project_id,
            kind="paper",
            title="Test paper",
        )

    def tearDown(self) -> None:
        self.connection.close()
        self.temp.cleanup()

    def test_human_import_is_preserved_and_agent_may_add_new_block(self) -> None:
        imported = self.service.commit_revision(
            document_id=self.document_id,
            canonical_html=IMPORTED,
            editor_type="human",
            operation="human_import",
        )
        self.assertIn("P-001", imported.protected_block_ids)
        expanded = IMPORTED.replace(
            "</section>",
            '<p id="P-002">エージェントが追加した本文。</p></section>',
        )
        agent_revision = self.service.commit_revision(
            document_id=self.document_id,
            canonical_html=expanded,
            editor_type="agent",
            operation="agent_edit",
        )
        self.assertEqual(self.service.canonical_html(agent_revision.revision_id), expanded)

    def test_agent_cannot_change_human_protected_block(self) -> None:
        self.service.commit_revision(
            document_id=self.document_id,
            canonical_html=IMPORTED,
            editor_type="human",
            operation="human_import",
        )
        changed = IMPORTED.replace("人間が書いた本文。", "勝手に変更した本文。")
        with self.assertRaises(ProtectedBlockError):
            self.service.commit_revision(
                document_id=self.document_id,
                canonical_html=changed,
                editor_type="agent",
                operation="agent_edit",
            )

    def test_revision_registers_generated_citation_events_atomically(self) -> None:
        revision = self.service.commit_revision(
            document_id=self.document_id,
            canonical_html=IMPORTED,
            editor_type="human",
            operation="human_import",
            citation_events=[
                {
                    "id": "citation-event:draft-1",
                    "block_id": "P-001",
                    "source_span_id": "SPAN-001",
                    "bibliographic_record_id": "REF-001",
                    "citation_kind": "paraphrase",
                    "locator": "12-13",
                    "rendered_text": "文献、12-13頁。",
                }
            ],
        )

        event = self.connection.execute(
            "SELECT * FROM citation_events WHERE revision_id = ?",
            (revision.revision_id,),
        ).fetchone()

        self.assertIsNotNone(event)
        self.assertEqual(event["block_id"], "P-001")
        self.assertEqual(event["source_span_id"], "SPAN-001")
        self.assertEqual(event["bibliographic_record_id"], "REF-001")
        self.assertEqual(event["citation_kind"], "paraphrase")

        child = self.service.commit_revision(
            document_id=self.document_id,
            canonical_html=IMPORTED.replace("</section>", '<p id="P-002">追記。</p></section>'),
            editor_type="human",
            operation="human_edit",
        )
        inherited = self.connection.execute(
            "SELECT * FROM citation_events WHERE revision_id = ?",
            (child.revision_id,),
        ).fetchall()

        self.assertEqual(len(inherited), 1)
        self.assertEqual(inherited[0]["block_id"], "P-001")

        replaced = self.service.commit_revision(
            document_id=self.document_id,
            canonical_html=IMPORTED.replace(
                "</section>", '<p id="P-002">追記。</p></section>'
            ),
            editor_type="human",
            operation="human_edit",
            citation_events=[
                {
                    "block_id": "P-001",
                    "source_span_id": "SPAN-001",
                    "bibliographic_record_id": "REF-001",
                    "citation_kind": "paraphrase",
                    "locator": "14",
                    "rendered_text": "文献、14頁。",
                }
            ],
        )
        current = self.connection.execute(
            "SELECT * FROM citation_events WHERE revision_id = ?",
            (replaced.revision_id,),
        ).fetchall()

        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["locator"], "14")

    def test_changed_citation_block_does_not_inherit_stale_event(self) -> None:
        self.service.commit_revision(
            document_id=self.document_id,
            canonical_html=IMPORTED,
            editor_type="human",
            operation="human_import",
            citation_events=[
                {
                    "block_id": "P-001",
                    "source_span_id": "SPAN-001",
                    "citation_kind": "paraphrase",
                }
            ],
        )
        child = self.service.commit_revision(
            document_id=self.document_id,
            canonical_html=IMPORTED.replace("人間が書いた本文。", "人間が直した本文。"),
            editor_type="human",
            operation="human_edit",
        )

        count = self.connection.execute(
            "SELECT COUNT(*) FROM citation_events WHERE revision_id = ?",
            (child.revision_id,),
        ).fetchone()[0]
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
