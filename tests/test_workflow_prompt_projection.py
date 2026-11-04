from __future__ import annotations

import copy
import inspect
import json
from types import SimpleNamespace

import pytest

from fukamiru.errors import FukamiruError, NonRetryableFukamiruError
from fukamiru.translation.codex import CodexTranslationAdapter
from fukamiru.worker import WorkerRunner


@pytest.mark.parametrize("method_name", [
    "resolve_manuscript_enumeration_context",
    "plan_semantic_paragraphs",
    "rewrite_manuscript_blocks",
    "resolve_manuscript_predicate_relations",
    "validate_manuscript_coherence",
])
def test_downstream_prompts_exclude_repeated_audit_but_keep_semantic_contract(method_name):
    # Mirrors the production failure: 1.5 MB of repeated source-unit provenance
    # alongside a small, useful plan. The canonical audit must remain intact.
    plan = {
        "document_thesis": "Keep the argument grounded.",
        "sections": [{"section_id": "S", "central_claim_block_id": "P"}],
        "block_roles": [{"block_id": "P", "role": "claim"}],
        "research_queries": ["A query needed by research"],
        "unresolved_questions": ["An unresolved argument"],
        "_response_contract": {"source_preserve_block_ids": ["PROTECTED"]},
        "argument_research_records": [{"source_units": "AUDIT_ONLY" * 170_000}],
    }
    original = copy.deepcopy(plan)
    adapter = object.__new__(CodexTranslationAdapter)
    captured = []
    adapter._complete_json = lambda prompt, schema, **kw: captured.append(prompt) or {}
    arguments = {
        "document_title": "Fixture", "instructions": "Preserve meaning",
        "human_review": "Preserve protected source blocks", "structure_plan": plan,
        "candidates": [{"candidate_id": "C", "block_id": "P", "items": ["A", "B"]}],
        "blocks": [{"id": "P", "text": "A and B are examples."}],
        "document_map": [], "outline_context": {}, "evidence_context": [],
        "discourse_structure": {"spans": []}, "paragraph_plan": {}, "stage": "pre_polish",
    }
    method = getattr(adapter, method_name)
    method(**{key: value for key, value in arguments.items()
              if key in inspect.signature(method).parameters})
    prompt = captured[0]
    assert len(prompt) < 50_000
    assert "AUDIT_ONLY" not in prompt
    expected = {key: value for key, value in original.items() if key != "argument_research_records"}
    assert json.dumps(expected, ensure_ascii=False) in prompt
    assert plan == original


@pytest.mark.parametrize("network_first", [False, True])
def test_input_size_error_is_redacted_and_wins_over_secondary_network_diagnostics(network_first):
    too_large = (
        'Error: input exceeds the maximum length of 1048576 characters. '
        '(code -32602), data: {"input_error_code":"input_too_large"} SECRET'
    )
    network = "Error: stream disconnected during request shutdown SECRET"
    lines = [network, too_large] if network_first else [too_large, network]
    error = CodexTranslationAdapter._execution_error_v22("fixture", "\n".join(lines))
    assert type(error) is NonRetryableFukamiruError
    assert error.failure_kind == "input_too_large"
    assert "SECRET" not in str(error)
    assert CodexTranslationAdapter.mandatory_execution_error("fixture", error) is error
    claimed = SimpleNamespace(workflow_name="manuscript_rewrite", workflow_version="23")
    assert not WorkerRunner._should_wait_for_codex(claimed, error)


def test_size_words_in_plain_stdout_cannot_become_an_execution_verdict():
    error = CodexTranslationAdapter._execution_error_v22(
        "fixture", "", "ERROR: input_too_large PRIVATE MANUSCRIPT",
    )
    assert type(error) is FukamiruError
    assert "PRIVATE" not in str(error)


def test_oversized_final_prompt_is_rejected_before_process_or_worker_home(monkeypatch):
    adapter = object.__new__(CodexTranslationAdapter)
    spawned = []
    monkeypatch.setattr("fukamiru.translation.codex.subprocess.Popen",
                        lambda *a, **kw: spawned.append(a))
    # Even an input at the raw limit overflows after the mandatory security and
    # generation contracts. Check the final prompt, without truncating source.
    with (
        adapter.mandatory_manuscript_execution(),
        pytest.raises(NonRetryableFukamiruError, match="input_too_large") as raised,
    ):
        adapter._complete_json("X" * 1_048_576, {
            "type": "object", "properties": {}, "required": [],
            "additionalProperties": False,
        }, operation="manuscript fixture")
    assert raised.value.failure_kind == "input_too_large"
    assert spawned == []
