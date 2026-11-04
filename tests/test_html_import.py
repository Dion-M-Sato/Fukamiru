from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from fukamiru.errors import CanonicalHTMLValidationError
from fukamiru.projects.service import ProjectService
from fukamiru.revisions import HTMLImportService, RevisionService
from fukamiru.storage.database import SQLiteDatabase


class HTMLImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        migrations = Path(__file__).resolve().parents[1] / "migrations" / "state"
        self.database = SQLiteDatabase(self.root / "state.db", migrations)
        self.database.migrate()
        self.connection = self.database.connect()
        self.project_root = self.root / "project"
        project = ProjectService(self.connection).create(
            name="HTML import fixture", root_path=self.project_root
        )
        self.document_id = RevisionService(self.connection).create_document(
            project_id=project["id"], kind="paper", title="Imported paper"
        )

    def tearDown(self) -> None:
        self.connection.close()
        self.temp.cleanup()

    def test_preview_preserves_original_and_confirms_only_valid_canonical_html(self) -> None:
        source = self.root / "external.html"
        original = "<html><body><p>External original without stable IDs.</p></body></html>"
        source.write_text(original, encoding="utf-8")
        imports = HTMLImportService(self.connection)

        preview = imports.preview(document_id=self.document_id, source_path=source)

        self.assertFalse(preview["validation"]["valid"])
        self.assertTrue(preview["original_unchanged"])
        with self.assertRaises(CanonicalHTMLValidationError):
            imports.confirm(preview["id"])
        canonical = (
            '<article id="DOC-IMPORT" lang="en">'
            '<section id="SEC-IMPORT"><h2 id="H-IMPORT">Imported</h2>'
            '<p id="P-IMPORT">Canonical text.</p></section></article>'
        )
        updated = imports.update_proposed(preview["id"], canonical_html=canonical)
        self.assertTrue(updated["validation"]["valid"])

        confirmed = imports.confirm(preview["id"])

        self.assertEqual(confirmed["status"], "imported")
        self.assertIsNotNone(confirmed["checkpoint_sha"])
        self.assertTrue(Path(confirmed["artifact_path"]).is_file())
        self.assertEqual(source.read_text(encoding="utf-8"), original)
        self.assertEqual(confirmed["original_html"], original)
        revision = self.connection.execute(
            "SELECT * FROM revisions WHERE id = ?", (confirmed["imported_revision_id"],)
        ).fetchone()
        self.assertEqual(revision["editor_type"], "human")
        self.assertEqual(revision["operation"], "human_import")
        protected = self.connection.execute(
            "SELECT COUNT(*) FROM content_blocks WHERE document_id = ? AND protected = 1",
            (self.document_id,),
        ).fetchone()[0]
        self.assertGreater(protected, 0)
        log = subprocess.run(
            ["git", "log", "-1", "--pretty=%s"],
            cwd=self.project_root,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        self.assertIn("Import canonical HTML revision", log)
        revisions = RevisionService(self.connection)
        history = revisions.history(self.document_id)
        diff = revisions.diff(confirmed["imported_revision_id"])
        self.assertEqual(history[0]["id"], confirmed["imported_revision_id"])
        self.assertGreater(history[0]["block_count"], 0)
        self.assertIn("DOC-IMPORT", diff["unified_diff"])


if __name__ == "__main__":
    unittest.main()
