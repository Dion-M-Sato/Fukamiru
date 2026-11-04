from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fukamiru.config import load_settings
from fukamiru.knowledge.semantic import BgeM3EmbeddingProvider
from fukamiru.worker import GpuMutex


class GpuArbitrationTests(unittest.TestCase):
    def test_priority_waiter_triggers_embedding_yield_before_lock_handoff(self) -> None:
        mutex = GpuMutex()
        yielded: list[str] = []
        local_acquired: list[str] = []

        def local_work() -> None:
            with mutex.hold("local_llm:llm-jp", priority=True):
                local_acquired.append("local")

        with mutex.hold("corpus:bge-m3", on_priority_waiter=lambda: yielded.append("bge")):
            thread = threading.Thread(target=local_work)
            thread.start()
            deadline = time.monotonic() + 2
            while mutex.status()["priority_waiters"] != 1 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(mutex.status()["priority_waiters"], 1)
            self.assertEqual(local_acquired, [])
        thread.join(2)

        self.assertEqual(yielded, ["bge"])
        self.assertEqual(local_acquired, ["local"])
        self.assertFalse(mutex.status()["locked"])

    def test_failed_yield_callback_releases_mutex(self) -> None:
        mutex = GpuMutex()
        waiter_started = threading.Event()

        def priority_waiter() -> None:
            waiter_started.set()
            with mutex.hold("local_llm:llm-jp", priority=True):
                pass

        with self.assertRaisesRegex(RuntimeError, "yield failed"), mutex.hold(
            "corpus:bge-m3",
            on_priority_waiter=lambda: (_ for _ in ()).throw(RuntimeError("yield failed")),
        ):
            thread = threading.Thread(target=priority_waiter)
            thread.start()
            waiter_started.wait(1)
            deadline = time.monotonic() + 1
            while mutex.status()["priority_waiters"] != 1 and time.monotonic() < deadline:
                time.sleep(0.005)
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertFalse(mutex.status()["locked"])

    def test_bge_model_moves_to_cpu_for_local_llm_and_back_to_cuda(self) -> None:
        class Parameter:
            device = "cuda:0"

        class Model:
            parameter = Parameter()

            def parameters(self) -> object:
                return iter((self.parameter,))

            def to(self, device: str) -> None:
                self.parameter.device = "cuda:0" if device == "cuda" else device

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
            provider = BgeM3EmbeddingProvider(load_settings(config))
            model = Model()
            provider._model = model
            with (
                patch("torch.cuda.synchronize"),
                patch("torch.cuda.empty_cache"),
            ):
                provider._yield_cuda_model()
            self.assertEqual(model.parameter.device, "cpu")
            self.assertIn("yielded", str(provider.actual_device))

            self.assertIs(provider._load(), model)
            self.assertEqual(model.parameter.device, "cuda:0")
            self.assertEqual(provider.actual_device, "cuda:0")


if __name__ == "__main__":
    unittest.main()
