"""Content-free telemetry helpers for Codex CLI invocations."""

from __future__ import annotations

import json
from typing import Any


def extract_codex_usage(stdout: str) -> dict[str, int | None]:
    """Extract token/cache counters from JSONL without retaining model content.

    Codex CLI event envelopes have changed over time, so this deliberately walks
    dictionaries and recognizes both Responses API names and older CLI aliases.
    The greatest observed value is used because completion events may repeat a
    cumulative counter.
    """

    aliases = {
        "input_tokens": {"input_tokens", "prompt_tokens"},
        "cached_input_tokens": {"cached_input_tokens", "cached_tokens"},
        "cache_write_tokens": {"cache_write_tokens"},
        "output_tokens": {"output_tokens", "completion_tokens"},
        "reasoning_tokens": {"reasoning_tokens"},
    }
    found: dict[str, list[int]] = {key: [] for key in aliases}

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                for canonical, names in aliases.items():
                    if key in names and isinstance(child, int) and not isinstance(child, bool):
                        found[canonical].append(child)
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        visit(event)
    return {key: max(values) if values else None for key, values in found.items()}
