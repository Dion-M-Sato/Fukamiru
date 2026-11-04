"""Per-call V23 admission sharing the legacy worker's reserved capacity."""

from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING

from fukamiru.storage.events import EventRepository

_LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from fukamiru.worker.runner import ClaimedNode, WorkerRunner


@contextmanager
def manuscript_call_slot(runner: WorkerRunner, claimed: ClaimedNode, operation: str):
    """Hold capacity only for an external invocation, never for its parent node.

    Tickets serialize admission, not execution. The shared counter includes
    legacy node/subslot reservations so V23 cannot borrow occupied capacity.
    """
    ticket = object()
    queued = time.monotonic()
    acquired = False
    started = queued
    success = False
    with runner._codex_gate_lock:
        runner._codex_v23_waiters.append(ticket)
    try:
        while True:
            runner._raise_if_run_cancelled(claimed, "V23 inference cancelled while waiting")
            if runner._try_acquire_codex_slot(v23_ticket=ticket):
                acquired = True
                started = time.monotonic()
                break
            runner._stop.wait(0.05)
        runner._raise_if_run_cancelled(claimed, "V23 inference cancelled before dispatch")
        yield
        success = True
    finally:
        finished = time.monotonic()
        with runner._codex_gate_lock:
            if ticket in runner._codex_v23_waiters:
                runner._codex_v23_waiters.remove(ticket)
        if acquired:
            runner._release_codex_slot()
            try:
                with runner.databases.state.connect() as connection:
                    EventRepository(connection).append(
                        claimed.run_id,
                        "worker.codex.call.completed" if success else "worker.codex.call.failed",
                        object_type="node_run",
                        object_id=claimed.node_run_id,
                        payload={
                            "node_id": claimed.node_id,
                            "operation": operation,
                            "workflow_version": claimed.workflow_version,
                            "admission": "v23_per_call",
                            "slot_wait_seconds": round(started - queued, 6),
                            "call_seconds": round(finished - started, 6),
                        },
                    )
            except sqlite3.Error:
                # Metrics must not discard a received answer or disguise the
                # original external failure as a different retryable outcome.
                _LOGGER.warning("Could not persist V23 inference timing", exc_info=True)
