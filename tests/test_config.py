from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from fukamiru.config import load_settings, validate_settings


class ConfigTests(unittest.TestCase):
    def test_local_config_overrides_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            defaults = root / "defaults.toml"
            local = root / "local.toml"
            defaults.write_text(
                """
[server]
host = "127.0.0.1"
port = 8765
[paths]
state_db = "/tmp/default-state.db"
knowledge_db = "/tmp/default-knowledge.db"
runtime_root = "/tmp/runtime"
corpus_root = "/tmp/corpus"
ocr_root = "/tmp/ocr"
knowledge_artifacts_root = "/tmp/kb"
faiss_root = "/tmp/faiss"
""",
                encoding="utf-8",
            )
            local.write_text("[server]\nport = 9876\n", encoding="utf-8")

            settings = load_settings(local, defaults)

            self.assertEqual(settings.server.port, 9876)
            self.assertEqual(settings.server.host, "127.0.0.1")
            self.assertEqual(settings.paths.state_db, Path("/tmp/default-state.db"))
            self.assertEqual(settings.retrieval.embedding_device, "cuda")
            self.assertEqual(
                settings.retrieval.embedding_cache_dir,
                Path("/tmp/runtime/cache/huggingface"),
            )
            self.assertTrue(settings.retrieval.require_cuda)
            self.assertEqual(settings.retrieval.embedding_batch_strategy, "fixed")
            self.assertEqual(settings.retrieval.embedding_dtype, "float32")
            self.assertEqual(settings.retrieval.embedding_attention_backend, "auto")
            self.assertEqual(settings.retrieval.embedding_measurement_interval_batches, 16)
            self.assertEqual(settings.ocr.provider, "language_router")
            self.assertEqual(settings.ocr.mistral_model, "mistral-ocr-latest")
            self.assertEqual(settings.ocr.ndlocr_command, "ndlocr-lite")
            self.assertTrue(settings.llm_jp.fit)
            self.assertTrue(settings.llm_jp.warmup)
            self.assertEqual(settings.llm_jp.parallel_slots, 1)
            self.assertEqual(settings.llm_jp.http_threads, -1)
            self.assertTrue(settings.llm_jp.webui)
            self.assertEqual(
                settings.translation.manuscript_structure_timeout_seconds, 1800
            )
            self.assertEqual(
                settings.translation.manuscript_structure_timeout_grace_seconds, 30
            )
            self.assertEqual(settings.translation.manuscript_codex_model, "gpt-5.6-sol")
            self.assertEqual(settings.translation.post_edit_lines_per_chunk, 5)
            self.assertEqual(settings.translation.post_edit_max_characters, 400)
            self.assertEqual(settings.translation.post_edit_context_characters, 600)
            self.assertEqual(settings.translation.post_edit_max_output_tokens, 512)
            self.assertEqual(settings.translation.post_edit_max_edits_per_chunk, 2)
            self.assertEqual(settings.translation.post_edit_max_repair_rounds, 3)
            self.assertEqual(
                settings.translation.manuscript_polish_max_output_tokens, 1536
            )
            self.assertEqual(
                settings.translation.manuscript_polish_max_edits_per_chunk, 6
            )
            self.assertEqual(settings.translation.manuscript_candidate_count, 6)
            self.assertEqual(settings.translation.manuscript_candidate_top_k, 3)
            self.assertEqual(
                settings.translation.manuscript_candidate_generation_attempts, 2
            )
            self.assertEqual(
                settings.translation.manuscript_candidate_human_training_target,
                100,
            )
            self.assertEqual(
                settings.translation.manuscript_candidate_auto_apply_margin, 0.12
            )
            self.assertEqual(settings.worker.ingestion_cpu_max_concurrency, 3)
            self.assertEqual(settings.worker.codex_max_concurrency, 5)
            self.assertEqual(settings.worker.codex_review_concurrency, 3)
            self.assertEqual(settings.worker.codex_reference_inference_concurrency, 2)
            self.assertEqual(settings.worker.codex_enumeration_concurrency, 5)
            self.assertEqual(settings.worker.codex_paragraph_concurrency, 5)
            self.assertEqual(settings.worker.codex_rewrite_concurrency, 5)
            self.assertEqual(settings.worker.codex_predicate_concurrency, 5)
            self.assertEqual(settings.worker.codex_coherence_concurrency, 3)
            self.assertEqual(settings.worker.evidence_search_concurrency, 5)
            self.assertEqual(settings.worker.reference_lookup_concurrency, 5)
            self.assertEqual(settings.worker.codex_translation_yield_batches, 20)
            self.assertEqual(settings.worker.codex_rate_limit_retry_delay_seconds, 30.0)
            self.assertEqual(settings.worker.codex_rate_limit_cooldown_seconds, 300.0)
            self.assertEqual(validate_settings(settings), [])

            changed = replace(
                settings,
                translation=replace(
                    settings.translation, manuscript_codex_model="gpt-6-astra"
                ),
            )
            self.assertIn(
                "translation.manuscript_codex_model must be 'gpt-5.6-sol'",
                validate_settings(changed),
            )

    def test_non_loopback_host_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            config.write_text(
                """
[server]
host = "0.0.0.0"
[paths]
state_db = "/tmp/state.db"
knowledge_db = "/tmp/knowledge.db"
runtime_root = "/tmp/runtime"
corpus_root = "/tmp/corpus"
ocr_root = "/tmp/ocr"
knowledge_artifacts_root = "/tmp/kb"
faiss_root = "/tmp/faiss"
""",
                encoding="utf-8",
            )
            settings = load_settings(config, config)
            self.assertIn("loopback-only", validate_settings(settings)[0])

    def test_embedding_cache_can_be_shared_independently_of_runtime_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shared_cache = root / "shared-model-cache"
            config = root / "config.toml"
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
embedding_cache_dir = "{shared_cache.as_posix()}"
""",
                encoding="utf-8",
            )

            settings = load_settings(config, config)

            self.assertEqual(settings.retrieval.embedding_cache_dir, shared_cache)


if __name__ == "__main__":
    unittest.main()
