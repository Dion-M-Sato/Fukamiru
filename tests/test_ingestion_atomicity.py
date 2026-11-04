from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fukamiru.bootstrap import bootstrap
from fukamiru.config import load_settings
from fukamiru.knowledge.service import IngestionService
from fukamiru.storage.database import DatabasePair


class IngestionAtomicityTests(unittest.TestCase):
    def test_ocr_pipeline_failure_marks_source_failed_without_revisions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "local.toml"
            config.write_text(
                f"""
[paths]
state_db = "{(root / 'state.db').as_posix()}"
knowledge_db = "{(root / 'knowledge.db').as_posix()}"
runtime_root = "{(root / 'runtime').as_posix()}"
corpus_root = "{(root / 'corpus').as_posix()}"
ocr_root = "{(root / 'ocr').as_posix()}"
knowledge_artifacts_root = "{(root / 'kb').as_posix()}"
faiss_root = "{(root / 'faiss').as_posix()}"
""",
                encoding="utf-8",
            )
            settings = load_settings(config)
            bootstrap(settings)
            source = root / "source.txt"
            source.write_text("Texte principal[1].\n\n[1] Note.", encoding="utf-8")
            databases = DatabasePair.from_settings(settings)
            with databases.knowledge.connect() as connection:
                service = IngestionService(connection, settings)
                located = service.locate(str(source))
                source_asset_id = service.register(located, "fr")
                with (
                    patch(
                        "fukamiru.knowledge.service.OCRPipelineService.process",
                        side_effect=RuntimeError("forced pipeline failure"),
                    ),
                    self.assertRaisesRegex(RuntimeError, "forced pipeline failure"),
                ):
                    service.extract(source_asset_id, source)

                status = connection.execute(
                    "SELECT ingestion_status FROM source_assets WHERE id = ?",
                    (source_asset_id,),
                ).fetchone()[0]
                revision_count = connection.execute(
                    "SELECT COUNT(*) FROM ocr_text_revisions WHERE source_asset_id = ?",
                    (source_asset_id,),
                ).fetchone()[0]
                self.assertEqual(status, "failed")
                self.assertEqual(revision_count, 0)


if __name__ == "__main__":
    unittest.main()
