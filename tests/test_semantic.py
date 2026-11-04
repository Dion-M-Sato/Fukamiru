from __future__ import annotations

import tempfile
import unittest
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import patch

from fukamiru.bootstrap import bootstrap
from fukamiru.config import load_settings
from fukamiru.errors import FukamiruError, NonRetryableFukamiruError
from fukamiru.knowledge.semantic import BgeM3EmbeddingProvider, FaissSemanticIndex
from fukamiru.knowledge.service import IngestionService
from fukamiru.storage.database import DatabasePair


class FixtureEmbeddingProvider:
    def __init__(self) -> None:
        self.encoded_text_count = 0

    @property
    def model_name(self) -> str:
        return "fixture-embedding-v1"

    @property
    def model_revision(self) -> str:
        return "fixture-revision"

    @property
    def actual_device(self) -> str:
        return "fixture"

    @property
    def last_metrics(self) -> dict[str, float | int]:
        return {
            "text_count": 1,
            "token_count": 7,
            "average_tokens_per_chunk": 7.0,
            "tokenization_seconds": 0.001,
            "gpu_inference_seconds": 0.002,
        }

    def available(self) -> bool:
        return True

    def encode(self, texts: list[str]) -> list[list[float]]:
        self.encoded_text_count += len(texts)
        vectors = []
        for text in texts:
            lowered = text.lower()
            vectors.append(
                [
                    1.0 if "provenance" in lowered or "origine" in lowered else 0.0,
                    1.0 if "traduction" in lowered else 0.0,
                    0.1,
                ]
            )
        return vectors


def _write_fake_model_snapshot(root: Path) -> Path:
    snapshot = (
        root
        / "runtime"
        / "cache"
        / "huggingface"
        / "models--BAAI--bge-m3"
        / "snapshots"
        / "5617a9f61b028005a4858fdac845db406aefb181"
    )
    snapshot.mkdir(parents=True)
    for name in ("config.json", "modules.json", "tokenizer_config.json"):
        (snapshot / name).write_text("{}", encoding="utf-8")
    (snapshot / "pytorch_model.bin").write_bytes(b"fixture")
    return snapshot


@unittest.skipUnless(find_spec("faiss") and find_spec("numpy"), "FAISS extra is unavailable")
class SemanticIndexTests(unittest.TestCase):
    def test_embedding_model_load_is_local_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = _write_fake_model_snapshot(root)
            config = root / "local.toml"
            config.write_text(
                f"""
[paths]
state_db = "{(root / "state.db").as_posix()}"
knowledge_db = "{(root / "knowledge.db").as_posix()}"
runtime_root = "{(root / "runtime").as_posix()}"
corpus_root = "{(root / "corpus").as_posix()}"
ocr_root = "{(root / "ocr").as_posix()}"
knowledge_artifacts_root = "{(root / "kb").as_posix()}"
faiss_root = "{(root / "faiss").as_posix()}"
""",
                encoding="utf-8",
            )

            class FakeModel:
                device = "cuda:0"

                @staticmethod
                def parameters():
                    return iter(())

                def __getitem__(self, index: int):
                    raise IndexError(index)

            provider = BgeM3EmbeddingProvider(load_settings(config))
            with patch(
                "sentence_transformers.SentenceTransformer",
                return_value=FakeModel(),
            ) as constructor:
                provider._load()
            self.assertEqual(constructor.call_args.args[0], str(snapshot.resolve()))
            self.assertTrue(constructor.call_args.kwargs["local_files_only"])
            self.assertIs(constructor.call_args.kwargs["token"], False)
            self.assertNotIn("revision", constructor.call_args.kwargs)
            self.assertNotIn("cache_folder", constructor.call_args.kwargs)

    def test_required_cuda_preflight_rejects_cpu_torch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "local.toml"
            config.write_text(
                f"""
[paths]
state_db = "{(root / "state.db").as_posix()}"
knowledge_db = "{(root / "knowledge.db").as_posix()}"
runtime_root = "{(root / "runtime").as_posix()}"
corpus_root = "{(root / "corpus").as_posix()}"
ocr_root = "{(root / "ocr").as_posix()}"
knowledge_artifacts_root = "{(root / "kb").as_posix()}"
faiss_root = "{(root / "faiss").as_posix()}"
""",
                encoding="utf-8",
            )
            provider = BgeM3EmbeddingProvider(load_settings(config))
            with (
                patch(
                    "fukamiru.knowledge.semantic.torch_runtime_status",
                    return_value={
                        "torch_version": "2.13.0+cpu",
                        "torch_cuda_version": None,
                        "cuda_available": False,
                        "gpu_name": None,
                        "torch_error": None,
                    },
                ),
                self.assertRaisesRegex(FukamiruError, "CPU fallback is disabled"),
            ):
                provider.preflight()

    def test_preflight_rejects_missing_local_snapshot_without_hub_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "local.toml"
            config.write_text(
                f"""
[paths]
state_db = "{(root / 'state.db').as_posix()}"
knowledge_db = "{(root / 'knowledge.db').as_posix()}"
runtime_root = "{(root / 'isolated-runtime').as_posix()}"
corpus_root = "{(root / 'corpus').as_posix()}"
ocr_root = "{(root / 'ocr').as_posix()}"
knowledge_artifacts_root = "{(root / 'kb').as_posix()}"
faiss_root = "{(root / 'faiss').as_posix()}"
[retrieval]
require_cuda = false
embedding_device = "cpu"
""",
                encoding="utf-8",
            )
            provider = BgeM3EmbeddingProvider(load_settings(config))

            with self.assertRaisesRegex(
                NonRetryableFukamiruError,
                "does not download models during a Run",
            ):
                provider.preflight()

    def test_non_default_dtype_uses_an_isolated_embedding_cache_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "local.toml"
            config.write_text(
                f"""
[paths]
state_db = "{(root / "state.db").as_posix()}"
knowledge_db = "{(root / "knowledge.db").as_posix()}"
runtime_root = "{(root / "runtime").as_posix()}"
corpus_root = "{(root / "corpus").as_posix()}"
ocr_root = "{(root / "ocr").as_posix()}"
knowledge_artifacts_root = "{(root / "kb").as_posix()}"
faiss_root = "{(root / "faiss").as_posix()}"
[retrieval]
embedding_dtype = "float16"
embedding_attention_backend = "sdpa"
""",
                encoding="utf-8",
            )
            provider = BgeM3EmbeddingProvider(load_settings(config))
            self.assertEqual(
                provider.cache_identity,
                "BAAI/bge-m3#dtype=float16;attention=sdpa",
            )

    def test_faiss_snapshot_is_published_and_searchable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "local.toml"
            config.write_text(
                f"""
[server]
host = "127.0.0.1"
port = 9876
open_browser = false
[paths]
state_db = "{(root / "data/state.db").as_posix()}"
knowledge_db = "{(root / "data/knowledge.db").as_posix()}"
runtime_root = "{(root / "runtime").as_posix()}"
corpus_root = "{(root / "corpus").as_posix()}"
ocr_root = "{(root / "ocr").as_posix()}"
knowledge_artifacts_root = "{(root / "kb").as_posix()}"
faiss_root = "{(root / "published-faiss").as_posix()}"
[retrieval]
tokenizer_mode = "unicode"
""",
                encoding="utf-8",
            )
            settings = load_settings(config)
            bootstrap(settings)
            databases = DatabasePair.from_settings(settings)
            source = settings.paths.corpus_root / "managed_sources" / "source.txt"
            source.write_text(
                "La provenance des sources doit rester vérifiable.",
                encoding="utf-8",
            )
            with databases.knowledge.connect() as connection:
                IngestionService(connection, settings).ingest_path(str(source))

            progress: list[dict[str, object]] = []
            provider = FixtureEmbeddingProvider()
            index = FaissSemanticIndex(
                databases.knowledge.connect,
                settings,
                provider=provider,
            )
            built = index.build(progress=progress.append)
            self.assertEqual(built["chunk_count"], 1)
            self.assertEqual(progress[-1]["embedded_count"], 1)
            self.assertEqual(progress[-1]["total_count"], 1)
            self.assertEqual(progress[-1]["newly_encoded_count"], 1)
            self.assertEqual(progress[-1]["average_tokens_per_chunk"], 7.0)
            self.assertGreater(float(progress[-1]["tokens_per_second"]), 0)
            self.assertTrue(Path(str(built["index_path"])).is_file())
            self.assertEqual(list((settings.paths.runtime_root / "index-build").iterdir()), [])

            results = index.search("origine", limit=3)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["retrieval"], "semantic")
            self.assertIn("provenance", results[0]["text"])
            self.assertEqual(index.status()["index"], "ready")

            traced_statements: list[str] = []

            def traced_connection():
                connection = databases.knowledge.connect()
                connection.set_trace_callback(traced_statements.append)
                return connection

            traced_index = FaissSemanticIndex(
                traced_connection,
                settings,
                provider=FixtureEmbeddingProvider(),
            )
            traced_index.search("origine", limit=3)
            mapping_queries = [
                statement
                for statement in traced_statements
                if "SELECT vector_key, chunk_id FROM chunk_index_records" in statement
            ]
            self.assertEqual(len(mapping_queries), 1)
            self.assertIn("AND vector_key IN (", mapping_queries[0])

            first_path = Path(str(built["index_path"]))
            second = index.build()
            self.assertEqual(provider.encoded_text_count, 2)  # one corpus chunk and one query
            self.assertEqual(second["cache_hit_count"], 1)
            self.assertEqual(second["newly_encoded_count"], 0)
            self.assertTrue(first_path.is_file())


if __name__ == "__main__":
    unittest.main()
