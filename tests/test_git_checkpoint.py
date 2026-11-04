from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fukamiru.errors import GitCheckpointError
from fukamiru.projects.git_checkpoint import GitCheckpointService


class GitCheckpointTests(unittest.TestCase):
    def test_only_canonical_artifacts_are_checkpointed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = GitCheckpointService(root)
            service.initialize()
            document = root / "documents" / "paper.html"
            document.parent.mkdir()
            document.write_text("<article></article>", encoding="utf-8")

            result = service.checkpoint([document], "Create paper")

            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result.changed_paths, ("documents/paper.html",))
            self.assertEqual(len(result.commit_sha), 40)

            database = root / "state.db"
            database.write_bytes(b"not-a-database")
            with self.assertRaises(GitCheckpointError):
                service.checkpoint([database], "Do not commit database")


if __name__ == "__main__":
    unittest.main()
