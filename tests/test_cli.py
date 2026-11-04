from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from fukamiru.cli import main


class CLITests(unittest.TestCase):
    def _config(self, root: Path) -> Path:
        config = root / "local.toml"
        config.write_text(
            f"""
[server]
host = "127.0.0.1"
port = 18765
open_browser = false
[paths]
state_db = "{(root / "data/state.db").as_posix()}"
knowledge_db = "{(root / "data/knowledge.db").as_posix()}"
runtime_root = "{(root / "runtime").as_posix()}"
corpus_root = "{(root / "corpus").as_posix()}"
ocr_root = "{(root / "ocr").as_posix()}"
knowledge_artifacts_root = "{(root / "kb").as_posix()}"
faiss_root = "{(root / "faiss").as_posix()}"
""",
            encoding="utf-8",
        )
        return config

    def test_init_status_and_html_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--config", str(config), "init"]), 0)
                self.assertEqual(main(["--config", str(config), "status"]), 0)
            self.assertIn("READY", output.getvalue())
            self.assertTrue((root / "data/state.db").is_file())

            valid = root / "valid.html"
            valid.write_text(
                '<article id="DOC-1" lang="ja"><section id="SEC-1">'
                '<h2 id="H-1">章</h2><p id="P-1">本文</p></section></article>',
                encoding="utf-8",
            )
            invalid = root / "invalid.html"
            invalid.write_text("<p>invalid</p>", encoding="utf-8")
            with redirect_stdout(StringIO()):
                self.assertEqual(main(["validate-html", str(valid)]), 0)
                self.assertEqual(main(["validate-html", str(invalid)]), 1)

    def test_invalid_configuration_returns_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            text = config.read_text(encoding="utf-8").replace(
                'host = "127.0.0.1"',
                'host = "0.0.0.0"',
            )
            config.write_text(text, encoding="utf-8")
            errors = StringIO()
            with redirect_stderr(errors):
                self.assertEqual(main(["--config", str(config), "status"]), 1)
            self.assertIn("loopback-only", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
