from __future__ import annotations

import json
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

from fukamiru.codex_cli import CodexCLIResolution
from fukamiru.errors import (
    ExternalServiceUnavailable,
    FukamiruError,
    InferenceEngineAuthorizationRequired,
    NonRetryableFukamiruError,
)
from fukamiru.revisions import ManuscriptRewriteService
from fukamiru.revisions.enumeration import ManuscriptEnumerationService
from fukamiru.storage.runs import RunRepository
from fukamiru.translation.codex import CodexTranslationAdapter
from fukamiru.worker import runner as runner_module

HTML = (
    '<article id="D" lang="ja"><section id="S"><h2 id="H">本文</h2>'
    '<p id="P1">この結果は前提を説明する。</p>'
    '<p id="P2">この関係は研究対象を示している。</p></section></article>'
)


@pytest.fixture
def worker_case():
    from test_worker import WorkerTests

    case = WorkerTests()
    case.setUp()
    try:
        yield case
    finally:
        case.tearDown()


def claim(case, version=22):
    run_id = case._create_run("manuscript_rewrite")
    claimed = case.worker._claim(lane="ingestion_cpu")
    assert claimed is not None and claimed.run_id == run_id
    return replace(claimed, workflow_version=str(version), resource_lane="codex_remote")


@pytest.mark.parametrize("error", [
    TimeoutError("timeout"), ConnectionError("connection failed"),
    FileNotFoundError("executable disappeared"), PermissionError("executable inaccessible"),
    subprocess.TimeoutExpired("codex", 30), URLError("name resolution failed"),
    HTTPError("https://example.test", 401, "Unauthorized", {}, None),
    HTTPError("https://example.test", 503, "Unavailable", {}, None),
    FukamiruError("Codex authentication is unavailable; run login status"),
    FukamiruError("Codex request failed: ERROR: Forbidden"),
    FukamiruError("Codex request failed: ERROR: unexpected status 403"),
    FukamiruError("Codex request failed: ERROR: rate limit exceeded"),
    FukamiruError("Codex request timed out after 30s"),
    FukamiruError("Codex request failed: ERROR: stream disconnected"),
])
def test_mandatory_classifier_recognizes_execution_failures(error):
    assert isinstance(
        CodexTranslationAdapter.mandatory_execution_error("audit", error),
        ExternalServiceUnavailable,
    )


@pytest.mark.parametrize("error", [
    FukamiruError("Structured output violates schema at $.rate_limit: enum"),
    FukamiruError("Codex returned invalid structured output"),
    FukamiruError("Codex did not create the structured audit output"),
    HTTPError("https://example.test", 400, "Invalid schema", {}, None),
    ValueError("bad content"),
])
def test_mandatory_classifier_preserves_content_failures(error):
    assert CodexTranslationAdapter.mandatory_execution_error("audit", error) is error


@pytest.mark.parametrize("method_name", [
    "review_manuscript_blocks", "plan_manuscript_structure",
    "plan_manuscript_structure_stage", "plan_manuscript_argument_process",
    "plan_manuscript_search_queries", "audit_manuscript_records",
    "resolve_manuscript_enumeration_context", "plan_semantic_paragraphs",
    "resolve_manuscript_predicate_relations", "plan_reference_queries", "infer_reference_entries",
    "rewrite_manuscript_blocks", "validate_manuscript_coherence",
    "plan_manuscript_language", "resolve_japanese_ambiguity",
])
def test_mandatory_backend_and_v21_compatibility(worker_case, method_name):
    claimed = claim(worker_case)
    worker_case.worker.translator = object()
    with pytest.raises(ExternalServiceUnavailable, match="required but unavailable"):
        worker_case.worker._manuscript_inference_backend(claimed, method_name)
    assert worker_case.worker._manuscript_inference_backend(
        replace(claimed, workflow_version="21"), method_name
    ) is None
    failure = FukamiruError("Codex authentication is unavailable")

    def unavailable(**_):
        raise failure

    worker_case.worker.translator = SimpleNamespace(**{method_name: unavailable})
    with pytest.raises(ExternalServiceUnavailable):
        worker_case.worker._invoke_manuscript_inference(claimed, method_name)
    with pytest.raises(FukamiruError) as raised:
        worker_case.worker._invoke_manuscript_inference(
            replace(claimed, workflow_version="21"), method_name
        )
    assert raised.value is failure


def test_readonly_outages_preserve_partial_success_and_content_budget(worker_case):
    claimed = claim(worker_case)
    calls = []
    script = [{"plans": [{
        "unit_id": "A", "subclaims": ["A"], "source_kind": "article", "variants": ["A"]
    }]}, TimeoutError("first"), TimeoutError("second"), TimeoutError("third"), {"plans": [{
        "unit_id": "B", "subclaims": ["B"], "source_kind": "book", "variants": ["B"]
    }]}]

    def planner(*, units):
        calls.append([u["unit_id"] for u in units])
        item = script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    worker_case.worker.translator = SimpleNamespace(plan_manuscript_search_queries=planner)
    units = [{"unit_id": "A", "query": "A"}, {"unit_id": "B", "query": "B"}]
    for _ in range(3):
        with pytest.raises(ExternalServiceUnavailable):
            worker_case.worker._readonly_manuscript_audit(claimed, "query_plan", units, [])
    result = worker_case.worker._readonly_manuscript_audit(claimed, "query_plan", units, [])
    assert [p["unit_id"] for p in result["plans"]] == ["A", "B"]
    assert calls == [["A", "B"], ["B"], ["B"], ["B"], ["B"]]
    with worker_case.databases.state.connect() as connection:
        statuses = [r[0] for r in connection.execute(
            "SELECT status FROM manuscript_generation_attempts WHERE run_id=? ORDER BY attempt",
            (claimed.run_id,),
        )]
    assert statuses == ["incomplete", *(["external_unavailable"] * 3), "complete"]


def test_readonly_invalid_content_stays_finite(worker_case):
    claimed = claim(worker_case)
    calls = []

    def planner(*, units):
        calls.append(units)
        return {"plans": []}

    worker_case.worker.translator = SimpleNamespace(plan_manuscript_search_queries=planner)
    for _ in range(3):
        assert worker_case.worker._readonly_manuscript_audit(
            claimed, "query_plan", [{"unit_id": "A", "query": "A"}], []
        ) == {
            "plans": [],
            "_response_contract_incomplete_unit_ids": ["A"],
        }
    assert len(calls) == 2


@pytest.mark.parametrize("version,raises", [(22, True), (21, False)])
def test_certainty_does_not_swallow_v22_outage(worker_case, monkeypatch, version, raises):
    claimed = claim(worker_case, version)

    def unavailable(**_):
        raise TimeoutError("no response")

    worker_case.worker.translator = SimpleNamespace(audit_manuscript_records=unavailable)
    monkeypatch.setattr(worker_case.worker, "_reserve_codex_subslots", lambda *a, **k: 0)
    if raises:
        with pytest.raises(ExternalServiceUnavailable):
            worker_case.worker._manuscript_calibrate_certainty(claimed, {"proposed_html": HTML})
    else:
        result = worker_case.worker._manuscript_calibrate_certainty(
            claimed, {"proposed_html": HTML}
        )
        assert result.context["unresolved_questions"]


@pytest.mark.parametrize("stage,method", [
    ("_manuscript_plan_paragraphs", "plan_semantic_paragraphs"),
    ("_manuscript_resolve_predicate_relations", "resolve_manuscript_predicate_relations"),
    ("_manuscript_resolve_enumeration_context", "resolve_manuscript_enumeration_context"),
])
def test_generation_stages_propagate_outages(worker_case, monkeypatch, stage, method):
    claimed = claim(worker_case)
    monkeypatch.setattr(ManuscriptRewriteService, "prepare", lambda *a, **k: {"base_html": HTML})
    monkeypatch.setattr(ManuscriptEnumerationService, "inventory", lambda *_: [{
        "candidate_id": "E1", "block_id": "P1", "surface": "A、B", "items": ["A", "B"]
    }])
    monkeypatch.setattr(worker_case.worker, "_reserve_codex_subslots", lambda *a, **k: 0)

    def unavailable(**_):
        raise ConnectionError("connection reset")

    worker_case.worker.translator = SimpleNamespace(**{method: unavailable})
    context = {
        "document_id": "D", "base_revision_id": "R", "document_title": "Fixture",
        "instructions": "Preserve", "proposed_html": HTML,
    }
    with pytest.raises(ExternalServiceUnavailable):
        getattr(worker_case.worker, stage)(claimed, context)
    with worker_case.databases.state.connect() as connection:
        rows = connection.execute(
            "SELECT status FROM manuscript_generation_attempts WHERE run_id=?", (claimed.run_id,)
        ).fetchall()
        assert all(row[0] == "external_unavailable" for row in rows)
        checkpoints = connection.execute(
            "SELECT payload_json FROM manuscript_unit_checkpoints WHERE run_id=? "
            "AND phase != 'manuscript-v22-engine'", (claimed.run_id,)
        ).fetchall()
        assert not [json.loads(row[0]) for row in checkpoints]


@pytest.mark.parametrize("phase", ["plan", "final"])
@pytest.mark.parametrize("status", ["candidate_generation_failed", "candidates_inappropriate"])
def test_minor_lexical_failure_preserves_source_as_conditional_success(worker_case, phase, status):
    claimed = claim(worker_case)
    context = {"proposed_html": HTML}
    result = worker_case.worker._language_condition_context_v22(
        claimed, context, phase=phase, records=[{
            "block_id": "P1", "kind": "contextual_lexicon", "status": status,
            "explanation": "語句候補を確定できないため原文を維持する。",
        }],
    )
    assert result["proposed_html"] == HTML
    assert result["completion_status"] == "conditional_success"
    assert result["language_review_requirements"][0]["review_class"] == (
        "minor_lexical_source_preserved"
    )
    assert result["language_review_requirements"][0]["disposition"] == "source_preserved"
    nonlexical = worker_case.worker._language_condition_context_v22(
        claimed, context, phase=phase, records=[{
            "block_id": "P1", "kind": "causal_relation", "status": status,
            "explanation": "Not a minor lexical choice.",
        }],
    )
    assert not nonlexical["language_review_requirements"]


class FirstEngine:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def invoke(**_):
            self.calls.append(name)
            return {"result": "ok"}
        return invoke


class AlternateEngine(FirstEngine):
    pass


class RecoverableConfiguredEngine:
    """One adapter whose executable and method can appear after startup."""

    def __init__(self, configured_command="codex-a"):
        self.configured_command = configured_command
        self.executable_sha256 = None
        self.executable_state = "unavailable"
        self.audit_manuscript_records = None
        self.calls = []

    def execution_identity_v22(self):
        return {
            "identity_contract": "test-recoverable-v1",
            "configured_command": self.configured_command,
            "executable_sha256": self.executable_sha256,
            "executable_state": self.executable_state,
            "config_policy": "ignore_user_config",
            "model_selection": "explicit_requested_model",
            "requested_model": "gpt-test",
            "explicit_model": "gpt-test",
            "explicit_model_provider": None,
        }

    def recover(self, executable_sha256):
        self.executable_sha256 = executable_sha256
        self.executable_state = "identified"

        def invoke(**_):
            self.calls.append("audit_manuscript_records")
            return {"result": "ok"}

        self.audit_manuscript_records = invoke


def engine_records(case, run_id):
    with case.databases.state.connect() as connection:
        return [json.loads(row[0]) for row in connection.execute(
            "SELECT payload_json FROM manuscript_unit_checkpoints "
            "WHERE run_id=? AND phase='manuscript-v22-engine' ORDER BY rowid", (run_id,)
        )]


def authorize_engine_transition(case, claimed, target_fingerprint):
    """Create the exact one-use record normally issued by manual resume."""

    previous = engine_records(case, claimed.run_id)[-1]["to_fingerprint"]
    authorization_id = str(uuid.uuid4())
    inputs = {
        **claimed.inputs,
        "_engine_authorization_id": authorization_id,
    }
    with case.databases.state.connect() as connection:
        connection.execute(
            "UPDATE manuscript_engine_authorizations "
            "SET status='superseded',superseded_at='2026-09-12T00:00:00.000Z' "
            "WHERE run_id=? AND node_run_id=? AND status='pending'",
            (claimed.run_id, claimed.node_run_id),
        )
        connection.execute(
            "INSERT INTO manuscript_engine_authorizations "
            "(id,run_id,node_run_id,from_fingerprint,target_fingerprint,status,created_at) "
            "VALUES (?,?,?,?,?,'pending','2026-09-12T00:00:00.000Z')",
            (
                authorization_id,
                claimed.run_id,
                claimed.node_run_id,
                previous,
                target_fingerprint,
            ),
        )
        connection.execute(
            "UPDATE node_runs SET input_json=? WHERE id=?",
            (json.dumps(inputs), claimed.node_run_id),
        )
        connection.commit()
    return replace(claimed, inputs=inputs), authorization_id


def test_codex_desired_identity_survives_missing_executable(worker_case, monkeypatch):
    monkeypatch.setattr(
        "fukamiru.translation.codex.resolve_codex_cli",
        lambda command, *, required_models, **_kwargs: CodexCLIResolution(
            configured_command=command,
            configured_resolved=None,
            resolved_executable=None,
            source=None,
            version=None,
            available=False,
            authenticated=None,
            account_type=None,
            required_models=tuple(sorted(required_models)),
            error_reason="Codex CLI executable was not found",
        ),
    )
    worker_case.worker.translator = CodexTranslationAdapter(worker_case.settings)

    descriptor = worker_case.worker._language_engine_descriptor_v22()

    desired = descriptor["desired_identity"]["execution_identity"]
    assert desired["configured_command"] == worker_case.settings.translation.codex_command
    assert desired["requested_model"] == (
        worker_case.settings.translation.manuscript_codex_model
    )
    assert desired["config_policy"] == "ignore_user_config"
    assert descriptor["runtime_identity"] is None
    assert descriptor["runtime_fingerprint"] is None
    assert descriptor["fingerprint"] == descriptor["desired_fingerprint"]


def test_v22_unavailable_bootstrap_persists_desired_identity_without_runtime(worker_case):
    claimed = claim(worker_case)
    engine = RecoverableConfiguredEngine()
    worker_case.worker.translator = engine

    with pytest.raises(ExternalServiceUnavailable, match="required but unavailable"):
        worker_case.worker._invoke_manuscript_inference(
            claimed, "audit_manuscript_records"
        )

    records = engine_records(worker_case, claimed.run_id)
    assert len(records) == 1
    binding = records[0]
    assert binding["binding_kind"] == "desired_only"
    assert binding["runtime_engine_fingerprint"] is None
    assert binding["engine"]["runtime_executable_sha256"] is None
    assert binding["engine"]["desired_identity"]["execution_identity"][
        "configured_command"
    ] == "codex-a"


def test_v22_same_desired_identity_binds_runtime_on_recovery_without_authorization(worker_case):
    claimed = claim(worker_case)
    engine = RecoverableConfiguredEngine()
    worker_case.worker.translator = engine
    with pytest.raises(ExternalServiceUnavailable):
        worker_case.worker._invoke_manuscript_inference(
            claimed, "audit_manuscript_records"
        )

    engine.recover("a" * 64)
    assert worker_case.worker._invoke_manuscript_inference(
        claimed, "audit_manuscript_records"
    ) == {"result": "ok"}
    assert engine.calls == ["audit_manuscript_records"]
    records = engine_records(worker_case, claimed.run_id)
    assert len(records) == 2
    assert records[1]["binding_kind"] == "desired_and_runtime"
    assert records[1]["quality_policy_change"] is False
    assert records[1]["change_authorized"] is False
    assert records[1]["engine"]["runtime_executable_sha256"] == "a" * 64


def test_v22_changed_desired_identity_after_outage_requires_exact_authorization_wait(
    worker_case,
):
    claimed = claim(worker_case)
    engine = RecoverableConfiguredEngine()
    worker_case.worker.translator = engine
    with pytest.raises(ExternalServiceUnavailable):
        worker_case.worker._invoke_manuscript_inference(
            claimed, "audit_manuscript_records"
        )

    engine.configured_command = "codex-b"
    engine.recover("b" * 64)
    target = worker_case.worker._language_engine_descriptor_v22()[
        "runtime_fingerprint"
    ]
    with pytest.raises(InferenceEngineAuthorizationRequired) as raised:
        worker_case.worker._invoke_manuscript_inference(
            claimed, "audit_manuscript_records"
        )
    assert raised.value.target_fingerprint == target
    assert not engine.calls

    worker_case.worker._wait_for_codex(claimed, raised.value)
    with worker_case.databases.state.connect() as connection:
        run = connection.execute(
            "SELECT status,error_json FROM runs WHERE id=?", (claimed.run_id,)
        ).fetchone()
    assert run["status"] == "waiting_human"
    error = json.loads(run["error_json"])
    assert error["reason"] == "inference_engine_authorization_required"
    assert error["target_engine_fingerprint"] == target
    assert len(engine_records(worker_case, claimed.run_id)) == 1


def test_v22_bound_runtime_sha_change_also_requires_exact_authorization(worker_case):
    claimed = claim(worker_case)
    engine = RecoverableConfiguredEngine()
    engine.recover("a" * 64)
    worker_case.worker.translator = engine
    worker_case.worker._invoke_manuscript_inference(
        claimed, "audit_manuscript_records"
    )

    engine.executable_sha256 = "b" * 64
    target = worker_case.worker._language_engine_descriptor_v22()[
        "runtime_fingerprint"
    ]
    with pytest.raises(InferenceEngineAuthorizationRequired) as raised:
        worker_case.worker._invoke_manuscript_inference(
            claimed, "audit_manuscript_records"
        )
    assert raised.value.target_fingerprint == target
    assert engine.calls == ["audit_manuscript_records"]


@pytest.mark.parametrize("method_name", [
    "review_manuscript_blocks", "plan_manuscript_structure",
    "plan_manuscript_structure_stage", "plan_manuscript_argument_process",
    "plan_manuscript_search_queries", "audit_manuscript_records",
    "resolve_manuscript_enumeration_context", "plan_semantic_paragraphs",
    "resolve_manuscript_predicate_relations", "plan_reference_queries", "infer_reference_entries",
    "rewrite_manuscript_blocks", "validate_manuscript_coherence",
    "plan_manuscript_language", "resolve_japanese_ambiguity",
])
def test_v22_run_binding_rejects_changed_engine_across_every_method(worker_case, method_name):
    claimed = claim(worker_case)
    first = FirstEngine()
    worker_case.worker.translator = first
    worker_case.worker._invoke_manuscript_inference(claimed, "review_manuscript_blocks")
    alternate = AlternateEngine()
    worker_case.worker.translator = alternate
    # A new source/context/phase cannot reset the Run-scoped binding.
    changed_context = replace(claimed, inputs={"instructions": "New instructions"})
    with pytest.raises(NonRetryableFukamiruError, match="without_authorization"):
        worker_case.worker._invoke_manuscript_inference(changed_context, method_name)
    assert not alternate.calls
    assert len(engine_records(worker_case, claimed.run_id)) == 1


def test_v22_run_binding_records_authorized_changes_and_rejects_unapproved_return(worker_case):
    claimed = claim(worker_case)
    first = FirstEngine()
    worker_case.worker.translator = first
    worker_case.worker._invoke_manuscript_inference(claimed, "review_manuscript_blocks")
    first_fingerprint = worker_case.worker._language_engine_descriptor_v22()["fingerprint"]
    alternate = AlternateEngine()
    worker_case.worker.translator = alternate
    alternate_fingerprint = worker_case.worker._language_engine_descriptor_v22()["fingerprint"]
    authorized, authorization_id = authorize_engine_transition(
        worker_case, claimed, alternate_fingerprint
    )
    worker_case.worker._invoke_manuscript_inference(authorized, "rewrite_manuscript_blocks")
    worker_case.worker._invoke_manuscript_inference(authorized, "audit_manuscript_records")
    records = engine_records(worker_case, claimed.run_id)
    assert len(records) == 2
    assert records[1]["from_fingerprint"] == first_fingerprint
    assert records[1]["to_fingerprint"] == alternate_fingerprint
    assert records[1]["method"] == "rewrite_manuscript_blocks"
    assert records[1]["recorded_at"]
    assert records[1]["quality_policy_change"] is True
    assert records[1]["change_authorized"] is True
    assert records[1]["authorization_id"] == authorization_id
    with worker_case.databases.state.connect() as connection:
        event = connection.execute(
            "SELECT payload_json FROM event_log WHERE run_id=? "
            "AND event_type='manuscript.inference_engine.quality_policy_changed'",
            (claimed.run_id,),
        ).fetchone()
    assert json.loads(event[0]) == records[1]
    worker_case.worker.translator = first
    with pytest.raises(NonRetryableFukamiruError, match="without_authorization"):
        worker_case.worker._invoke_manuscript_inference(
            authorized, "audit_manuscript_records"
        )
    authorized_return, _ = authorize_engine_transition(
        worker_case, claimed, first_fingerprint
    )
    worker_case.worker._invoke_manuscript_inference(
        authorized_return, "audit_manuscript_records"
    )
    assert len(engine_records(worker_case, claimed.run_id)) == 3


def test_v22_pending_db_authorization_authorizes_only_exact_target(worker_case):
    claimed = claim(worker_case)
    worker_case.worker.translator = FirstEngine()
    worker_case.worker._invoke_manuscript_inference(
        claimed, "review_manuscript_blocks"
    )
    worker_case.worker.translator = AlternateEngine()
    target = worker_case.worker._language_engine_descriptor_v22()["fingerprint"]

    authorized, authorization_id = authorize_engine_transition(
        worker_case, claimed, target
    )
    worker_case.worker._invoke_manuscript_inference(
        authorized, "audit_manuscript_records"
    )

    records = engine_records(worker_case, claimed.run_id)
    assert records[-1]["authorization_field"] == "engine_authorization_id"
    assert records[-1]["authorization_id"] == authorization_id
    assert records[-1]["to_fingerprint"] == target


@pytest.mark.parametrize("authorization", [True, "*", "wrong-fingerprint", None])
def test_v22_run_binding_requires_exact_authorization(worker_case, authorization):
    claimed = claim(worker_case)
    worker_case.worker.translator = FirstEngine()
    worker_case.worker._invoke_manuscript_inference(claimed, "audit_manuscript_records")
    worker_case.worker.translator = AlternateEngine()
    with pytest.raises(NonRetryableFukamiruError, match="without_authorization"):
        worker_case.worker._invoke_manuscript_inference(
            replace(claimed, inputs={"authorized_manuscript_inference_engine": authorization}),
            "plan_reference_queries",
        )


def test_v21_does_not_bind_or_reject_engine_changes(worker_case):
    claimed = claim(worker_case, 21)
    worker_case.worker.translator = FirstEngine()
    worker_case.worker._invoke_manuscript_inference(claimed, "audit_manuscript_records")
    worker_case.worker.translator = AlternateEngine()
    worker_case.worker._invoke_manuscript_inference(claimed, "plan_reference_queries")
    assert engine_records(worker_case, claimed.run_id) == []


def test_run_authorization_is_accepted_by_legacy_language_phase_ledger(worker_case):
    claimed = claim(worker_case)
    worker_case.worker.translator = FirstEngine()
    worker_case.worker._register_language_engine_v22(
        claimed, {}, phase="plan", context_signature="first"
    )
    worker_case.worker.translator = AlternateEngine()
    fingerprint = worker_case.worker._language_engine_descriptor_v22()["fingerprint"]
    authorized, _ = authorize_engine_transition(worker_case, claimed, fingerprint)
    engine = worker_case.worker._register_language_engine_v22(
        authorized, dict(authorized.inputs),
        phase="plan", context_signature="different-source",
    )
    assert engine["fingerprint"] == fingerprint
    assert len(engine_records(worker_case, claimed.run_id)) == 2
    with worker_case.databases.state.connect() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM manuscript_unit_checkpoints WHERE run_id=? "
            "AND phase='language-v22-engine:plan'", (claimed.run_id,),
        ).fetchone()[0]
    assert count == 2


def test_parallel_first_calls_bind_the_run_exactly_once(worker_case):
    claimed = claim(worker_case)
    worker_case.worker.translator = FirstEngine()
    with ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(
            lambda _: worker_case.worker._invoke_manuscript_inference(
                claimed, "audit_manuscript_records"
            ),
            range(5),
        ))
    assert len(results) == 5
    assert len(engine_records(worker_case, claimed.run_id)) == 1


def test_parallel_authorized_calls_consume_once_without_spurious_wait(worker_case):
    claimed = claim(worker_case)
    worker_case.worker.translator = FirstEngine()
    worker_case.worker._invoke_manuscript_inference(claimed, "audit_manuscript_records")
    alternate = AlternateEngine()
    worker_case.worker.translator = alternate
    target = worker_case.worker._language_engine_descriptor_v22()["fingerprint"]
    authorized, authorization_id = authorize_engine_transition(
        worker_case, claimed, target
    )

    with ThreadPoolExecutor(max_workers=5) as executor:
        results = list(
            executor.map(
                lambda _: worker_case.worker._invoke_manuscript_inference(
                    authorized, "audit_manuscript_records"
                ),
                range(5),
            )
        )

    assert len(results) == 5
    assert len(alternate.calls) == 5
    assert len(engine_records(worker_case, claimed.run_id)) == 2
    with worker_case.databases.state.connect() as connection:
        status = connection.execute(
            "SELECT status FROM manuscript_engine_authorizations WHERE id=?",
            (authorization_id,),
        ).fetchone()[0]
    assert status == "consumed"


def test_v22_consumed_authorization_cannot_be_replayed_after_a_branch_returns(worker_case):
    claimed = claim(worker_case)
    first = FirstEngine()
    second = AlternateEngine()
    worker_case.worker.translator = first
    worker_case.worker._invoke_manuscript_inference(claimed, "audit_manuscript_records")
    first_fingerprint = worker_case.worker._language_engine_descriptor_v22()["fingerprint"]

    worker_case.worker.translator = second
    second_fingerprint = worker_case.worker._language_engine_descriptor_v22()["fingerprint"]
    authorized_second, consumed_id = authorize_engine_transition(
        worker_case, claimed, second_fingerprint
    )
    worker_case.worker._invoke_manuscript_inference(
        authorized_second, "audit_manuscript_records"
    )

    worker_case.worker.translator = first
    authorized_first, _ = authorize_engine_transition(
        worker_case, claimed, first_fingerprint
    )
    worker_case.worker._invoke_manuscript_inference(
        authorized_first, "audit_manuscript_records"
    )

    worker_case.worker.translator = second
    with pytest.raises(InferenceEngineAuthorizationRequired):
        worker_case.worker._invoke_manuscript_inference(
            replace(claimed, inputs={"_engine_authorization_id": consumed_id}),
            "audit_manuscript_records",
        )
    assert second.calls == ["audit_manuscript_records"]
    with worker_case.databases.state.connect() as connection:
        status = connection.execute(
            "SELECT status FROM manuscript_engine_authorizations WHERE id=?",
            (consumed_id,),
        ).fetchone()[0]
    assert status == "consumed"


@pytest.mark.parametrize("scope", ["other_run", "other_node"])
def test_v22_authorization_is_scoped_to_exact_run_and_node(worker_case, scope):
    claimed = claim(worker_case)
    worker_case.worker.translator = FirstEngine()
    worker_case.worker._invoke_manuscript_inference(claimed, "audit_manuscript_records")
    worker_case.worker.translator = AlternateEngine()
    target = worker_case.worker._language_engine_descriptor_v22()["fingerprint"]
    _authorized, authorization_id = authorize_engine_transition(
        worker_case, claimed, target
    )

    if scope == "other_run":
        scoped_claim = claim(worker_case)
        worker_case.worker.translator = FirstEngine()
        worker_case.worker._invoke_manuscript_inference(
            scoped_claim, "audit_manuscript_records"
        )
    else:
        branch_node_id = str(uuid.uuid4())
        with worker_case.databases.state.connect() as connection:
            connection.execute(
                "INSERT INTO node_runs(id,run_id,node_id,attempt,status,input_json,resource_lane) "
                "VALUES (?,?,?,1,'queued','{}','codex_remote')",
                (branch_node_id, claimed.run_id, "authorization_scope_branch"),
            )
            connection.commit()
        scoped_claim = replace(claimed, node_run_id=branch_node_id)

    worker_case.worker.translator = AlternateEngine()
    with pytest.raises(InferenceEngineAuthorizationRequired):
        worker_case.worker._invoke_manuscript_inference(
            replace(scoped_claim, inputs={"_engine_authorization_id": authorization_id}),
            "audit_manuscript_records",
        )
    with worker_case.databases.state.connect() as connection:
        status = connection.execute(
            "SELECT status FROM manuscript_engine_authorizations WHERE id=?",
            (authorization_id,),
        ).fetchone()[0]
    assert status == "pending"


def test_v22_desired_and_runtime_change_requires_full_runtime_fingerprint(worker_case):
    claimed = claim(worker_case)
    engine = RecoverableConfiguredEngine("codex-a")
    engine.recover("a" * 64)
    worker_case.worker.translator = engine
    worker_case.worker._invoke_manuscript_inference(claimed, "audit_manuscript_records")

    engine.configured_command = "codex-b"
    engine.executable_sha256 = "b" * 64
    descriptor = worker_case.worker._language_engine_descriptor_v22()
    assert descriptor["runtime_fingerprint"] != descriptor["desired_fingerprint"]
    wrong, wrong_id = authorize_engine_transition(
        worker_case, claimed, descriptor["desired_fingerprint"]
    )
    with pytest.raises(InferenceEngineAuthorizationRequired) as raised:
        worker_case.worker._invoke_manuscript_inference(
            wrong, "audit_manuscript_records"
        )
    assert raised.value.target_fingerprint == descriptor["runtime_fingerprint"]
    with worker_case.databases.state.connect() as connection:
        assert connection.execute(
            "SELECT status FROM manuscript_engine_authorizations WHERE id=?",
            (wrong_id,),
        ).fetchone()[0] == "pending"

    correct, correct_id = authorize_engine_transition(
        worker_case, claimed, descriptor["runtime_fingerprint"]
    )
    worker_case.worker._invoke_manuscript_inference(
        correct, "audit_manuscript_records"
    )
    record = engine_records(worker_case, claimed.run_id)[-1]
    assert record["to_fingerprint"] == descriptor["runtime_fingerprint"]
    assert record["authorization_fingerprint"] == record["to_fingerprint"]
    assert record["authorization_id"] == correct_id


@pytest.mark.parametrize(
    "legacy_key",
    [
        "authorized_engine_fingerprint",
        "authorized_manuscript_inference_engine",
        "authorized_language_inference_engine",
    ],
)
def test_v22_legacy_context_flags_cannot_authorize_an_engine_change(
    worker_case, legacy_key
):
    claimed = claim(worker_case)
    worker_case.worker.translator = FirstEngine()
    worker_case.worker._invoke_manuscript_inference(claimed, "audit_manuscript_records")
    worker_case.worker.translator = AlternateEngine()
    target = worker_case.worker._language_engine_descriptor_v22()["fingerprint"]
    inputs = {legacy_key: target}
    with pytest.raises(InferenceEngineAuthorizationRequired):
        worker_case.worker._invoke_manuscript_inference(
            replace(claimed, inputs=inputs), "audit_manuscript_records"
        )
    assert inputs == {}


def test_v22_engine_authorization_consumption_rolls_back_with_binding_failure(
    worker_case, monkeypatch
):
    claimed = claim(worker_case)
    worker_case.worker.translator = FirstEngine()
    worker_case.worker._invoke_manuscript_inference(claimed, "audit_manuscript_records")
    worker_case.worker.translator = AlternateEngine()
    target = worker_case.worker._language_engine_descriptor_v22()["fingerprint"]
    authorized, authorization_id = authorize_engine_transition(
        worker_case, claimed, target
    )

    with monkeypatch.context() as scoped:
        scoped.setattr(
            runner_module.EventRepository,
            "append",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("event write failed")),
        )
        with pytest.raises(RuntimeError, match="event write failed"):
            worker_case.worker._invoke_manuscript_inference(
                authorized, "audit_manuscript_records"
            )

    with worker_case.databases.state.connect() as connection:
        authorization = connection.execute(
            "SELECT status,consumed_at FROM manuscript_engine_authorizations WHERE id=?",
            (authorization_id,),
        ).fetchone()
        stored_inputs = json.loads(
            connection.execute(
                "SELECT input_json FROM node_runs WHERE id=?",
                (claimed.node_run_id,),
            ).fetchone()[0]
        )
    assert dict(authorization) == {"status": "pending", "consumed_at": None}
    assert stored_inputs["_engine_authorization_id"] == authorization_id
    assert len(engine_records(worker_case, claimed.run_id)) == 1

    worker_case.worker._invoke_manuscript_inference(
        replace(claimed, inputs=stored_inputs), "audit_manuscript_records"
    )
    assert len(engine_records(worker_case, claimed.run_id)) == 2


def test_v22_ordinary_outage_resume_rejects_engine_fingerprint(worker_case):
    claimed = claim(worker_case)
    error = {
        "type": "ExternalServiceUnavailable",
        "reason": "external_service_unavailable",
        "count": 4,
        "not_before": None,
    }
    with worker_case.databases.state.connect() as connection:
        connection.execute(
            "UPDATE node_runs SET status='waiting_human',error_json=?,"
            "lease_owner=NULL,lease_acquired_at=NULL,lease_heartbeat_at=NULL,"
            "lease_expires_at=NULL WHERE id=?",
            (json.dumps(error), claimed.node_run_id),
        )
        connection.execute(
            "UPDATE runs SET status='waiting_human',error_json=? WHERE id=?",
            (json.dumps(error), claimed.run_id),
        )
        connection.commit()
        before = connection.total_changes
        with pytest.raises(FukamiruError, match="accepted only"):
            RunRepository(connection).resume_external_wait(
                claimed.run_id, authorized_engine_fingerprint="a" * 64
            )
        assert connection.total_changes == before
