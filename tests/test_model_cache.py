from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fukamiru.errors import NonRetryableFukamiruError
from fukamiru.knowledge.model_cache import resolve_local_model_snapshot
from fukamiru.knowledge.service import _embedding_tokenizer


class LocalModelCacheTests(unittest.TestCase):
    def test_missing_snapshot_fails_before_transformers_loader_is_called(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("transformers.AutoTokenizer.from_pretrained") as loader,
                self.assertRaises(NonRetryableFukamiruError),
            ):
                resolve_local_model_snapshot(
                    cache_dir=Path(directory),
                    model_name="BAAI/bge-m3",
                    revision="pinned-revision",
                )
            loader.assert_not_called()

    def test_tokenizer_loader_receives_only_an_explicit_local_path(self) -> None:
        _embedding_tokenizer.cache_clear()
        local_path = str(Path("C:/prepared/bge-m3-snapshot"))
        with patch("transformers.AutoTokenizer.from_pretrained") as loader:
            _embedding_tokenizer(local_path)
        self.assertEqual(loader.call_args.args, (local_path,))
        self.assertTrue(loader.call_args.kwargs["local_files_only"])
        self.assertIs(loader.call_args.kwargs["token"], False)
        self.assertNotIn("cache_dir", loader.call_args.kwargs)
        self.assertNotIn("revision", loader.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
