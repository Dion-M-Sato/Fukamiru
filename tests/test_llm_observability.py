from __future__ import annotations

import json

from fukamiru.llm_observability import extract_codex_usage


def test_extract_codex_usage_accepts_responses_and_cli_aliases() -> None:
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 120,
                        "input_tokens_details": {
                            "cached_tokens": 80,
                            "cache_write_tokens": 16,
                        },
                        "output_tokens": 22,
                        "output_tokens_details": {"reasoning_tokens": 7},
                    },
                }
            ),
            json.dumps({"usage": {"prompt_tokens": 100, "completion_tokens": 20}}),
        ]
    )

    assert extract_codex_usage(stdout) == {
        "input_tokens": 120,
        "cached_input_tokens": 80,
        "cache_write_tokens": 16,
        "output_tokens": 22,
        "reasoning_tokens": 7,
    }


def test_extract_codex_usage_ignores_non_json_and_content() -> None:
    assert extract_codex_usage('prose\n{"type":"message","text":"input_tokens: 99"}') == {
        "input_tokens": None,
        "cached_input_tokens": None,
        "cache_write_tokens": None,
        "output_tokens": None,
        "reasoning_tokens": None,
    }
