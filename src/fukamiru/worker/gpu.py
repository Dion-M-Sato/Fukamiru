"""Process-local GPU arbitration shared by embedding and local LLM work."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class GpuMutex:
    """Serialize GPU residents without blocking CPU-only Codex work."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._owner: str | None = None
        self._acquired_at: str | None = None
        self._waiters = 0
        self._next_ticket = 0
        self._serving_ticket = 0
        self._priority_waiters = 0

    @contextmanager
    def hold(
        self,
        owner: str,
        *,
        priority: bool = False,
        on_priority_waiter: Callable[[], None] | None = None,
    ) -> Iterator[None]:
        with self._condition:
            ticket = self._next_ticket
            self._next_ticket += 1
            self._waiters += 1
            if priority:
                self._priority_waiters += 1
            self._condition.wait_for(
                lambda: ticket == self._serving_ticket and self._owner is None
            )
            self._waiters -= 1
            if priority:
                self._priority_waiters -= 1
            self._owner = owner
            self._acquired_at = _utc_now()
        try:
            yield
        finally:
            with self._condition:
                try:
                    if on_priority_waiter is not None and self._priority_waiters > 0:
                        on_priority_waiter()
                finally:
                    # A failed residency-yield callback must never strand the
                    # process-wide lane. The callback error is still propagated
                    # after ownership is released so the embedding batch fails
                    # explicitly instead of silently sharing the GPU.
                    self._owner = None
                    self._acquired_at = None
                    self._serving_ticket += 1
                    self._condition.notify_all()

    def status(self) -> dict[str, Any]:
        with self._condition:
            return {
                "locked": self._owner is not None,
                "owner": self._owner,
                "acquired_at": self._acquired_at,
                "waiters": self._waiters,
                "priority_waiters": self._priority_waiters,
            }
