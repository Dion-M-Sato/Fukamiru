"""A completed embedding build must not leave a late database writer behind."""

from threading import Event, Thread
from unittest.mock import Mock

from fukamiru.knowledge.semantic import EmbeddingResourceSampler


def test_shutdown_waits_for_slow_gpu_sample_without_opening_a_late_database(monkeypatch):
    sampling = Event()
    release = Event()
    stopped = Event()
    connection_factory = Mock()

    def sample():
        sampling.set()
        assert release.wait(8)
        return 1.0, 1.0, 2.0

    monkeypatch.setattr(EmbeddingResourceSampler, "_gpu_sample", staticmethod(sample))
    sampler = EmbeddingResourceSampler(connection_factory, "fixture-build", interval_seconds=0.01)
    sampler.__enter__()
    assert sampling.wait(2)

    def shutdown():
        sampler.__exit__()
        stopped.set()

    closer = Thread(target=shutdown)
    closer.start()
    try:
        assert sampler._stop.wait(2)
        # The previous short join returned before a valid GPU query had finished.
        assert not stopped.wait(2.2)
    finally:
        release.set()
        closer.join(timeout=3)
    assert stopped.is_set()
    assert not sampler._thread.is_alive()
    connection_factory.assert_not_called()
