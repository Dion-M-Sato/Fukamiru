from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from fukamiru.authoring.codex import describe_authoring_request
from fukamiru.bootstrap import bootstrap
from fukamiru.config import load_settings
from fukamiru.errors import FukamiruError
from fukamiru.storage.database import DatabasePair
from fukamiru.storage.llm_checkpoints import build_checkpoint_key
from fukamiru.storage.llm_observations import LLMObservationRepository
from fukamiru.storage.runs import RunRepository
from fukamiru.worker.runner import ClaimedNode, WorkerRunner


class Adapter:
    model = "gpt-6-luna"

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.fail_first = False

    def generate(self, stage, payload, context=""):
        self.calls.append(dict(payload))
        if self.fail_first and len(self.calls) == 1:
            raise FukamiruError("seed failed")
        return {
            "sections": [
                {
                    "section_id": item,
                    "paragraphs": [
                        {
                            "move_indices": [1],
                            "evidence_ids": ["e1"],
                            "text": "ok",
                        }
                    ],
                    "claim_ids": [],
                    "evidence_ids": ["e1"],
                    "article_role": "body",
                    "realized_evidence_ids": ["e1"],
                    "realized_warrant_ids": ["w1"],
                    "realized_qualifier_ids": ["q1"],
                    "evidence_treatment": [
                        {
                            "evidence_id": "e1",
                            "claim_id": "c1",
                            "basis": "llm_inference",
                            "verification_status": "unverified",
                            "prose_treatment": "qualified_interpretation",
                            "qualifier_id": "q1",
                            "citation_purpose": "",
                            "attribution_sentence": "",
                            "purpose_clause": "",
                        }
                    ],
                    "concrete_examples": ["example"],
                    "new_propositions": [],
                }
                for item in payload["target_section_ids"]
            ]
        }


def key(**overrides):
    payload = overrides.pop("payload", {"a": 1, "b": 2})
    request = describe_authoring_request(
        "draft_sections",
        payload,
        overrides.pop("rag", "rag"),
        model=overrides.pop("model", "gpt-6-luna"),
    )
    return build_checkpoint_key(
        run_id=overrides.pop("run_id", "run-1"),
        workflow_version="30",
        node_id="draft_sections",
        operation_name="draft",
        request=request,
        plan_id="plan-1",
        plan_revision=overrides.pop("plan_revision", 1),
        source_revision_id="source-1",
        **overrides,
    )


def test_fingerprint_canonicalizes_json_key_order_and_covers_request_contract():
    assert key(payload={"a": 1, "b": 2}).request_fingerprint_sha256 == key(
        payload={"b": 2, "a": 1}
    ).request_fingerprint_sha256
    baseline = key().request_fingerprint_sha256
    assert key(payload={"a": 2, "b": 2}).request_fingerprint_sha256 != baseline
    assert key(rag="other").request_fingerprint_sha256 != baseline
    assert key(model="gpt-6-sol").request_fingerprint_sha256 != baseline
    assert key(plan_revision=2).request_fingerprint_sha256 != baseline
    assert key(checkpoint_variant="repair").request_fingerprint_sha256 != baseline
    request = describe_authoring_request("draft_sections", {"a": 1, "b": 2}, "rag")

    def changed(request_value):
        return build_checkpoint_key(
            run_id="run-1", workflow_version="30", node_id="draft_sections",
            operation_name="draft", request=request_value, plan_id="plan-1",
            plan_revision=1, source_revision_id="source-1",
        ).request_fingerprint_sha256

    assert changed(replace(request, reasoning_effort="high")) != baseline
    assert changed(replace(request, schema={**request.schema, "title": "changed"})) != baseline
    assert changed(replace(request, guidance=request.guidance + " changed")) != baseline


@pytest.fixture
def worker_case(tmp_path: Path):
    config = tmp_path / "local.toml"
    config.write_text(
        "\n".join([
            "[paths]",
            f'state_db = "{(tmp_path / "state.db").as_posix()}"',
            f'knowledge_db = "{(tmp_path / "knowledge.db").as_posix()}"',
            f'runtime_root = "{(tmp_path / "runtime").as_posix()}"',
            f'corpus_root = "{(tmp_path / "corpus").as_posix()}"',
            f'ocr_root = "{(tmp_path / "ocr").as_posix()}"',
            f'knowledge_artifacts_root = "{(tmp_path / "knowledge").as_posix()}"',
            f'faiss_root = "{(tmp_path / "faiss").as_posix()}"',
            "[worker]",
            "enabled = false",
            "codex_checkpoint_seed_prefix_characters = 1024",
        ]), encoding="utf-8",
    )
    settings = load_settings(config)
    bootstrap(settings)
    databases = DatabasePair.from_settings(settings)
    adapter = Adapter()
    runner = WorkerRunner(
        settings, databases, {}, translator=object(), authoring_adapter=adapter,
        semantic_index=object(),
    )
    with databases.state.connect() as connection:
        created = RunRepository(connection).create(
            workflow_name="authoring", workflow_version="30",
            entry_node="draft_sections", project_id=None, inputs={},
            resource_lane="codex_remote",
        )
        node = connection.execute(
            "SELECT * FROM node_runs WHERE run_id=?", (created.run_id,)
        ).fetchone()
    claimed = ClaimedNode(
        node_run_id=str(node["id"]), run_id=created.run_id,
        node_id="draft_sections", attempt=1, workflow_name="authoring",
        workflow_version="30", resource_lane="codex_remote", lease_owner="test",
        project_id=None, inputs={},
    )
    return runner, adapter, claimed, databases


def test_same_run_exact_request_hits_without_second_llm_call_and_other_run_misses(worker_case):
    runner, adapter, claimed, databases = worker_case
    payload = {
        "target_section_ids": ["s1"],
        "locked_plan": "SENSITIVE_PROMPT_BODY_MUST_NOT_BE_STORED",
    }
    first = runner._authoring_generate(
        claimed, "draft_sections", {"plan_id": "p", "plan_revision": 1}, payload=payload
    )
    second = runner._authoring_generate(
        claimed, "draft_sections", {"plan_id": "p", "plan_revision": 1}, payload=payload
    )
    assert first == second
    assert len(adapter.calls) == 1
    with databases.state.connect() as connection:
        checkpoint = dict(connection.execute(
            "SELECT * FROM llm_result_checkpoints WHERE run_id=?", (claimed.run_id,)
        ).fetchone())
        observation = dict(connection.execute(
            "SELECT * FROM llm_invocation_observations WHERE run_id=? LIMIT 1",
            (claimed.run_id,),
        ).fetchone())
        assert "SENSITIVE_PROMPT_BODY" not in str(checkpoint)
        assert "SENSITIVE_PROMPT_BODY" not in str(observation)
        other = RunRepository(connection).create(
            workflow_name="authoring", workflow_version="30", entry_node="draft_sections",
            project_id=None, inputs={}, resource_lane="codex_remote",
        )
        node = connection.execute(
            "SELECT * FROM node_runs WHERE run_id=?", (other.run_id,)
        ).fetchone()
    runner._authoring_generate(
        replace(claimed, run_id=other.run_id, node_run_id=str(node["id"])),
        "draft_sections", {"plan_id": "p", "plan_revision": 1}, payload=payload,
    )
    assert len(adapter.calls) == 2


def test_schema_valid_semantic_mismatch_is_not_saved(worker_case):
    runner, adapter, claimed, databases = worker_case
    adapter.generate = lambda *_args, **_kwargs: {
        "sections": [
            {
                "section_id": "wrong",
                "paragraphs": [
                    {"move_indices": [1], "evidence_ids": ["e1"], "text": "ok"}
                ],
                "claim_ids": [],
                "evidence_ids": ["e1"],
                "article_role": "body",
                "realized_evidence_ids": ["e1"],
                "realized_warrant_ids": ["w1"],
                "realized_qualifier_ids": ["q1"],
                "evidence_treatment": [
                    {
                        "evidence_id": "e1",
                        "claim_id": "c1",
                        "basis": "llm_inference",
                        "verification_status": "unverified",
                        "prose_treatment": "qualified_interpretation",
                        "qualifier_id": "q1",
                        "citation_purpose": "",
                        "attribution_sentence": "",
                        "purpose_clause": "",
                    }
                ],
                "concrete_examples": ["example"],
                "new_propositions": [],
            }
        ]
    }
    with pytest.raises(FukamiruError, match="coverage"):
        runner._authoring_generate(
            claimed, "draft_sections", {},
            payload={"target_section_ids": ["s1"]},
        )
    with databases.state.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM llm_result_checkpoints").fetchone()[0] == 0


def test_seed_failure_does_not_launch_fanout(worker_case):
    runner, adapter, claimed, _databases = worker_case
    adapter.fail_first = True
    shared = "x" * 3000
    payloads = [
        {"locked_plan": shared, "target_section_ids": [f"s{index}"]}
        for index in range(3)
    ]
    with pytest.raises(FukamiruError, match="seed failed"):
        runner._authoring_parallel_generate(
            claimed, stage="draft_sections", payloads=payloads,
            operation_name="draft", context={"plan_id": "p", "plan_revision": 1},
        )
    assert len(adapter.calls) == 1


def test_observation_preserves_null_and_reported_zero():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """CREATE TABLE llm_invocation_observations(
        id TEXT,run_id TEXT,node_run_id TEXT,node_id TEXT,stage TEXT,operation_name TEXT,
        model TEXT,reasoning_effort TEXT,full_prompt_sha256 TEXT,prefix_1024_sha256 TEXT,
        prefix_4096_sha256 TEXT,prefix_16384_sha256 TEXT,request_fingerprint_sha256 TEXT,
        cache_family_sha256 TEXT,prompt_characters INTEGER,payload_characters INTEGER,
        rag_context_characters INTEGER,common_prefix_characters INTEGER,checkpoint_status TEXT,
        seed_role TEXT,input_tokens_reported_count INTEGER,
        cached_input_tokens_reported_count INTEGER,
        cache_write_tokens_reported_count INTEGER,output_tokens_reported_count INTEGER,
        reasoning_tokens_reported_count INTEGER,
        zero_cached_input_call_count INTEGER,created_at TEXT);"""
    )
    repo = LLMObservationRepository(connection)
    kwargs = dict(
        run_id="r", node_run_id=None, node_id="n", stage="s", operation_name="o",
        model="m", reasoning_effort="low", key=key(), prompt_characters=1,
        payload_characters=1, rag_context_characters=0, checkpoint_status="miss",
    )
    repo.record(**kwargs, reported_counts={"cached_input_tokens": None, "cache_write_tokens": None})
    repo.record(**kwargs, reported_counts={"cached_input_tokens": 0, "cache_write_tokens": 0})
    rows = connection.execute(
        "SELECT cached_input_tokens_reported_count,cache_write_tokens_reported_count,"
        "zero_cached_input_call_count FROM llm_invocation_observations ORDER BY rowid"
    ).fetchall()
    assert tuple(rows[0]) == (None, None, 0)
    assert tuple(rows[1]) == (0, 0, 1)
    summary = repo.summary("r")
    assert summary["checkpoints"] == {
        "hit": 0,
        "miss": 2,
        "saved": 0,
        "invalidated": 0,
    }
