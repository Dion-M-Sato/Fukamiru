"""Low-overhead wall and current-thread CPU timing for long-running stages."""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Any


class StageMetrics:
    """Accumulate time at stage boundaries without sampling or database writes."""

    def __init__(self, initial_stage: str) -> None:
        self._previous_wall = time.perf_counter()
        self._previous_cpu = time.thread_time()
        self._stage = initial_stage
        self._wall: defaultdict[str, float] = defaultdict(float)
        self._cpu: defaultdict[str, float] = defaultdict(float)
        self._external_cpu: defaultdict[str, float] = defaultdict(float)
        self._external_wall: defaultdict[str, float] = defaultdict(float)
        self._details: dict[str, Any] = {}

    def transition(self, stage: str | None) -> None:
        wall = time.perf_counter()
        cpu = time.thread_time()
        if self._stage is not None:
            self._wall[self._stage] += wall - self._previous_wall
            self._cpu[self._stage] += cpu - self._previous_cpu
        self._previous_wall = wall
        self._previous_cpu = cpu
        self._stage = stage

    def add_external_cpu(self, stage: str, seconds: float) -> None:
        if seconds >= 0:
            self._external_cpu[stage] += seconds

    def add_external_wall(self, stage: str, seconds: float) -> None:
        if seconds >= 0:
            self._external_wall[stage] += seconds

    def annotate(self, **details: Any) -> None:
        self._details.update(details)

    def finish(self, *, outcome: str) -> dict[str, Any]:
        self.transition(None)
        stages = sorted(self._wall.keys() | self._external_cpu.keys() | self._external_wall.keys())
        return {
            "outcome": outcome,
            "wall_seconds": round(sum(self._wall.values()), 3),
            "thread_cpu_seconds": round(sum(self._cpu.values()), 3),
            "external_cpu_seconds": round(sum(self._external_cpu.values()), 3),
            "cpu_scope": "current_thread_and_direct_child_process",
            "details": self._details,
            "stages": {
                stage: {
                    "wall_seconds": round(self._wall[stage], 3),
                    "thread_cpu_seconds": round(self._cpu[stage], 3),
                    "external_cpu_seconds": round(self._external_cpu[stage], 3),
                    "external_wall_seconds": round(self._external_wall[stage], 3),
                }
                for stage in stages
            },
        }
