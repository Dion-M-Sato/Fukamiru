from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_workflow_language_transport import HTML, claimed_v22
from test_workflow_language_transport import worker_case as worker_case

from fukamiru.codex_cli import CodexCLIResolution
from fukamiru.errors import (
    ExternalServiceUnavailable,
    FukamiruError,
    InferenceEngineAuthorizationRequired,
)
from fukamiru.revisions import japanese_analysis
from fukamiru.translation.codex import CodexTranslationAdapter
from fukamiru.worker.runner import WorkerRunner


def _install_cli_resolution(monkeypatch, selected) -> None:
    def resolve(_command, *, required_models=(), **_kwargs):
        path = selected()
        return CodexCLIResolution(
            configured_command="codex",
            configured_resolved=str(path) if path else None,
            resolved_executable=str(Path(path).resolve()) if path else None,
            source="path" if path else None,
            version="0.158.0-alpha.2.1" if path else None,
            available=bool(path),
            authenticated=True if path else None,
            account_type="ChatGPT" if path else None,
            required_models=tuple(sorted(required_models)),
            error_reason=None if path else "Codex CLI executable was not found",
        )

    def require(command, *, required_models=(), **kwargs):
        result = resolve(command, required_models=required_models, **kwargs)
        if not result.available:
            raise FukamiruError(str(result.error_reason))
        return result

    monkeypatch.setattr("fukamiru.translation.codex.resolve_codex_cli", resolve)
    monkeypatch.setattr("fukamiru.translation.codex.require_codex_cli", require)


@pytest.mark.parametrize("diagnostic", [
    "Error: authentication failed token=SECRET manuscript=PRIVATE",
    "error: network error token=SECRET manuscript=PRIVATE",
    "2026-09-12T01:02:03Z ERROR: authentication failed token=SECRET",
    "2026-09-12T01:02:03Z ERROR codex_core::client: stream disconnected SECRET",
    "FATAL: request timed out SECRET",
    "Error: unexpected status 429 SECRET",
    "Error: HTTP 503 SECRET",
    '\x1b[31mError:\x1b[0m rate limit exceeded SECRET',
    '{"type":"turn.failed","error":{"message":"unauthorized SECRET"}}',
])
def test_v22_cli_diagnostics_are_typed_and_redacted(diagnostic):
    error = CodexTranslationAdapter._execution_error_v22("fixture", diagnostic)
    assert isinstance(error, ExternalServiceUnavailable)
    assert "SECRET" not in str(error)
    assert "PRIVATE" not in str(error)


def test_v22_stdout_prose_is_not_a_cli_diagnostic():
    error = CodexTranslationAdapter._execution_error_v22(
        "fixture", "", "ERROR: authentication failed PRIVATE"
    )
    assert type(error) is FukamiruError
    assert "PRIVATE" not in str(error)
    error = CodexTranslationAdapter._execution_error_v22(
        "fixture", "", '{"type":"error","message":"authentication failed SECRET"}'
    )
    assert isinstance(error, ExternalServiceUnavailable)
    assert "SECRET" not in str(error)


@pytest.mark.parametrize(("diagnostic", "kind"), [
    ("ERROR: unauthorized SECRET", "authentication"),
    ("ERROR: network failure SECRET", "network"),
    ("ERROR: request timed out SECRET", "timeout"),
    ("ERROR: status 429 SECRET", "rate_limit"),
])
def test_v22_cli_diagnostics_have_safe_failure_kind(diagnostic, kind):
    error = CodexTranslationAdapter._execution_error_v22("fixture", diagnostic)
    assert isinstance(error, ExternalServiceUnavailable)
    assert error.failure_kind == kind
    assert kind in str(error)
    assert "SECRET" not in str(error)


def test_v22_pretyped_external_failure_is_redacted():
    error = CodexTranslationAdapter.mandatory_execution_error(
        "fixture", ExternalServiceUnavailable("offline token=SECRET manuscript=PRIVATE")
    )
    assert isinstance(error, ExternalServiceUnavailable)
    assert error.failure_kind == "external_service"
    assert "SECRET" not in str(error)
    assert "PRIVATE" not in str(error)


@pytest.mark.parametrize("path", ["$.records[429]", "$.rate_limit"])
def test_v22_outer_worker_does_not_reclassify_schema_text(worker_case, monkeypatch, path):
    run_id = worker_case._create_run("manuscript_rewrite")
    workflow = worker_case.worker._workflow_for("manuscript_rewrite", "22")
    spec = next(n for n in workflow.nodes if n.id == "plan_term_introductions")

    def invalid(*_):
        raise FukamiruError(f"Structured output violates schema at {path}: enum")

    monkeypatch.setitem(worker_case.worker.handlers, spec.handler, invalid)
    with worker_case.databases.state.connect() as connection:
        connection.execute(
            "UPDATE node_runs SET node_id=?, resource_lane='codex_remote', input_json=? "
            "WHERE run_id=?", (spec.id, json.dumps({"proposed_html": HTML}), run_id),
        )
        connection.execute("UPDATE runs SET workflow_version='22' WHERE id=?", (run_id,))
        connection.commit()
    assert worker_case.worker.run_once(lane="codex_remote")
    with worker_case.databases.state.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM event_log WHERE run_id=? "
            "AND event_type IN ('worker.codex.retry_scheduled','worker.codex.resume_required')",
            (run_id,),
        ).fetchone()[0] == 0
        assert not any("_external_retry" in row[0] for row in connection.execute(
            "SELECT input_json FROM node_runs WHERE run_id=?", (run_id,)
        ))


def test_v30_authoring_contract_text_with_p0429_is_not_a_rate_limit(worker_case):
    claimed = replace(
        claimed_v22(worker_case),
        workflow_name="authoring",
        workflow_version="30",
    )
    error = FukamiruError("atomic proposition p0429 must remain qualified")

    assert not WorkerRunner._should_wait_for_codex(claimed, error)
    assert WorkerRunner._should_wait_for_codex(
        claimed, ExternalServiceUnavailable("outage")
    )


@pytest.mark.parametrize("workflow,version", [
    ("manuscript_rewrite", "21"), ("translation", "22"),
])
def test_legacy_wait_classification_is_unchanged(worker_case, workflow, version):
    claimed = replace(claimed_v22(worker_case), workflow_name=workflow, workflow_version=version)
    assert WorkerRunner._should_wait_for_codex(claimed, FukamiruError("rate limit"))
    assert WorkerRunner._should_wait_for_codex(claimed, ExternalServiceUnavailable("outage"))
    assert not WorkerRunner._should_wait_for_codex(claimed, FukamiruError("bad content"))


def test_v22_invocation_context_is_reset_after_failure(worker_case, monkeypatch):
    from fukamiru.translation import codex

    claimed = claimed_v22(worker_case)
    seen = []

    class Adapter:
        def plan_manuscript_language(self, **_):
            seen.append(codex._MANDATORY_MANUSCRIPT_EXECUTION.get())
            raise FukamiruError("bad content")

    worker_case.worker.translator = Adapter()
    for version in (22, 21):
        with pytest.raises(FukamiruError):
            worker_case.worker._invoke_manuscript_inference(
                replace(claimed, workflow_version=str(version)), "plan_manuscript_language"
            )
        assert not codex._MANDATORY_MANUSCRIPT_EXECUTION.get()
    assert seen == [True, False]


def complete_record(schema, block_id):
    fields = schema["properties"]["records"]["items"]["properties"]
    return {
        **dict.fromkeys(fields, ""), "block_id": block_id, "kind": "block_review",
        "status": "no_revision_needed", "explanation": "確認済み。",
    }


def install_cli_fixture(case, monkeypatch, tmp_path, responses):
    adapter = CodexTranslationAdapter(case.settings)
    calls = []
    script = list(responses)
    original = adapter.plan_manuscript_language
    executable = tmp_path / "fixture.exe"
    executable.write_bytes(b"fixture-codex-cli")

    def traced(**kwargs):
        calls.append([b["id"] for b in kwargs["blocks"]])
        return original(**kwargs)

    adapter.plan_manuscript_language = traced

    class Process:
        returncode = 0

        def __init__(self, command, **_):
            schema_path = Path(command[command.index("--output-schema") + 1])
            self.schema = json.loads(schema_path.read_text())
            self.output = Path(command[command.index("-o") + 1])

        def communicate(self, *_args, **_kwargs):
            action = script.pop(0)
            if isinstance(action, Exception):
                raise action
            self.output.write_text(json.dumps(action(self.schema)), encoding="utf-8")
            return "", ""

    def home(_user_home, job_id):
        path = tmp_path / job_id
        path.mkdir()
        return path

    _install_cli_resolution(monkeypatch, lambda: executable)
    monkeypatch.setattr("fukamiru.translation.codex.subprocess.Popen", Process)
    monkeypatch.setattr(adapter, "_prepare_worker_home", home)
    monkeypatch.setattr(adapter, "_prepare_windows_ca_bundle", lambda _: None)
    case.worker.translator = adapter
    return adapter, calls, script


def test_real_language_contract_saves_valid_blocks_and_repairs_only_invalid_once(
    worker_case, monkeypatch, tmp_path,
):
    def partial(schema):
        invalid = complete_record(schema, "P2")
        del invalid["explanation"]
        return {"records": [complete_record(schema, "P1"), invalid, complete_record(schema, "P3")]}

    adapter, calls, _script = install_cli_fixture(worker_case, monkeypatch, tmp_path, [
        partial, lambda schema: {"records": [complete_record(schema, "P2")]},
    ])
    claimed = claimed_v22(worker_case)
    records = worker_case.worker._language_plan(claimed, {"proposed_html": HTML}, "plan")
    assert calls == [["P1", "P2", "P3"], ["P2"]]
    assert {r["block_id"] for r in records} == {"P1", "P2", "P3"}
    assert not any(r["status"] == "response_contract_incomplete" for r in records)
    assert adapter is worker_case.worker.translator
    assert worker_case.worker._language_plan(claimed, {"proposed_html": HTML}, "plan") == records
    assert len(calls) == 2


def test_v22_null_language_array_retries_all_expected_blocks_once(
    worker_case, monkeypatch, tmp_path,
):
    _adapter, calls, _script = install_cli_fixture(
        worker_case,
        monkeypatch,
        tmp_path,
        [
            lambda _schema: {"records": None},
            lambda schema: {
                "records": [
                    complete_record(schema, block_id)
                    for block_id in ("P1", "P2", "P3")
                ]
            },
        ],
    )
    claimed = claimed_v22(worker_case)

    records = worker_case.worker._language_plan(
        claimed, {"proposed_html": HTML}, "plan"
    )

    assert calls == [["P1", "P2", "P3"], ["P1", "P2", "P3"]]
    assert {record["block_id"] for record in records} == {"P1", "P2", "P3"}


def test_real_language_contract_second_failure_becomes_conditional_not_third_call(
    worker_case, monkeypatch, tmp_path,
):
    def partial(schema):
        return {"records": [complete_record(schema, "P1"), {"block_id": "P2"}]}

    _adapter, calls, _script = install_cli_fixture(worker_case, monkeypatch, tmp_path, [
        partial, lambda _: {"records": [{"block_id": "P2"}]},
    ])
    claimed = claimed_v22(worker_case)
    result = worker_case.worker._manuscript_plan_terms(claimed, {"proposed_html": HTML})
    assert result.context["completion_status"] == "conditional_success"
    assert calls == [["P1", "P2", "P3"], ["P2", "P3"]]
    repeated = worker_case.worker._manuscript_plan_terms(claimed, {"proposed_html": HTML})
    assert repeated.context["term_introduction_plan"] == result.context["term_introduction_plan"]
    assert len(calls) == 2


def test_real_v21_language_contract_remains_strict(worker_case, monkeypatch, tmp_path):
    adapter, _calls, _script = install_cli_fixture(worker_case, monkeypatch, tmp_path, [
        lambda schema: {"records": [complete_record(schema, "P1"), {"block_id": "P2"}]},
    ])
    with pytest.raises(FukamiruError, match="violates schema"):
        adapter.plan_manuscript_language(
            phase="plan", blocks=[{"id": "P1"}, {"id": "P2"}],
            first_mentions=[], evidence=[], plan=[], workflow_version=21,
        )
    with pytest.raises(ValueError, match="restricted"):
        adapter._complete_json(
            "", {}, operation="another strict node", partial_language_records=True
        )


def test_v22_execution_identity_tracks_binary_not_copied_secrets(
    worker_case, monkeypatch, tmp_path,
):
    from fukamiru.translation import codex

    executable = tmp_path / "fixture.exe"
    executable.write_bytes(b"binary-version-one")
    _install_cli_resolution(monkeypatch, lambda: executable)
    monkeypatch.setattr(
        codex.Path,
        "home",
        lambda: (_ for _ in ()).throw(AssertionError("no config")),
    )
    worker_case.worker.translator = CodexTranslationAdapter(worker_case.settings)
    first = worker_case.worker._language_engine_descriptor_v22()
    executable.write_bytes(b"binary-version-two-changed")
    second = worker_case.worker._language_engine_descriptor_v22()
    assert first["fingerprint"] != second["fingerprint"]
    assert first["execution_identity"]["model_selection"] == "explicit_requested_model"
    assert first["execution_identity"]["requested_model"] == "gpt-5.6-sol"
    assert first["execution_identity"]["requested_reasoning_effort"] == "medium"
    assert first["execution_identity"]["config_policy"] == "ignore_user_config"
    assert "binary-version" not in json.dumps(first)


def test_v22_requested_model_changes_engine_fingerprint(worker_case, monkeypatch):
    _install_cli_resolution(monkeypatch, lambda: None)
    worker_case.worker.translator = CodexTranslationAdapter(worker_case.settings)
    first = worker_case.worker._language_engine_descriptor_v22()
    changed_settings = replace(
        worker_case.settings,
        translation=replace(
            worker_case.settings.translation,
            manuscript_codex_model="gpt-6-astra",
        ),
    )
    worker_case.worker.settings = changed_settings
    worker_case.worker.translator = CodexTranslationAdapter(changed_settings)

    second = worker_case.worker._language_engine_descriptor_v22()

    assert first["fingerprint"] != second["fingerprint"]
    assert second["execution_identity"]["requested_model"] == "gpt-6-astra"


def test_v22_same_path_binary_replacement_is_blocked_before_spawn(
    worker_case, monkeypatch, tmp_path,
):
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"approved-binary")
    adapter = CodexTranslationAdapter(worker_case.settings)
    worker_case.worker.translator = adapter
    claimed = claimed_v22(worker_case)
    spawned = []
    original = adapter.resolve_japanese_ambiguity

    def replace_binary_then_resolve(**kwargs):
        executable.write_bytes(b"unapproved-binary")
        return original(**kwargs)

    adapter.resolve_japanese_ambiguity = replace_binary_then_resolve
    _install_cli_resolution(monkeypatch, lambda: executable)
    monkeypatch.setattr(
        "fukamiru.translation.codex.subprocess.Popen",
        lambda *args, **kwargs: spawned.append((args, kwargs)),
    )

    def home(_user_home, job_id):
        path = tmp_path / job_id
        path.mkdir()
        return path

    monkeypatch.setattr(adapter, "_prepare_worker_home", home)
    monkeypatch.setattr(adapter, "_prepare_windows_ca_bundle", lambda _: None)

    with pytest.raises(InferenceEngineAuthorizationRequired) as raised:
        worker_case.worker._invoke_manuscript_inference(
            claimed,
            "resolve_japanese_ambiguity",
            text="本文",
            context="",
            analysis={},
        )

    current = worker_case.worker._language_engine_descriptor_v22(adapter)
    assert raised.value.target_fingerprint == current["runtime_fingerprint"]
    assert raised.value.previous_fingerprint != raised.value.target_fingerprint
    assert spawned == []


def test_v22_path_lookup_replacement_never_executes_unbound_path(
    worker_case, monkeypatch, tmp_path,
):
    approved = tmp_path / "approved.exe"
    replacement = tmp_path / "replacement.exe"
    approved.write_bytes(b"approved-binary")
    replacement.write_bytes(b"replacement-binary")
    selected = {"path": approved}
    commands = []
    adapter = CodexTranslationAdapter(worker_case.settings)
    worker_case.worker.translator = adapter
    claimed = claimed_v22(worker_case)
    original = adapter.resolve_japanese_ambiguity

    def change_lookup_then_resolve(**kwargs):
        selected["path"] = replacement
        return original(**kwargs)

    adapter.resolve_japanese_ambiguity = change_lookup_then_resolve

    class Process:
        returncode = 0

        def __init__(self, command, **_):
            commands.append(command)
            self.output = Path(command[command.index("-o") + 1])

        def communicate(self, *_args, **_kwargs):
            self.output.write_text(
                json.dumps(
                    {"status": "resolved", "explanation": "確定した。", "roles": ["論者"]}
                ),
                encoding="utf-8",
            )
            return "", ""

    def home(_user_home, job_id):
        path = tmp_path / job_id
        path.mkdir()
        return path

    _install_cli_resolution(monkeypatch, lambda: selected["path"])
    monkeypatch.setattr("fukamiru.translation.codex.subprocess.Popen", Process)
    monkeypatch.setattr(adapter, "_prepare_worker_home", home)
    monkeypatch.setattr(adapter, "_prepare_windows_ca_bundle", lambda _: None)

    worker_case.worker._invoke_manuscript_inference(
        claimed,
        "resolve_japanese_ambiguity",
        text="本文",
        context="",
        analysis={},
    )
    assert Path(commands[0][0]) == approved.resolve()
    assert str(replacement.resolve()) not in commands[0]

    with pytest.raises(InferenceEngineAuthorizationRequired):
        worker_case.worker._invoke_manuscript_inference(
            claimed,
            "resolve_japanese_ambiguity",
            text="本文",
            context="",
            analysis={},
        )
    assert len(commands) == 1


def test_v22_model_change_requires_authorization_before_spawn(
    worker_case, monkeypatch, tmp_path,
):
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"approved-binary")
    commands = []
    adapter = CodexTranslationAdapter(worker_case.settings)
    worker_case.worker.translator = adapter
    claimed = claimed_v22(worker_case)

    class Process:
        returncode = 0

        def __init__(self, command, **_):
            commands.append(command)
            self.output = Path(command[command.index("-o") + 1])

        def communicate(self, *_args, **_kwargs):
            self.output.write_text(
                json.dumps(
                    {"status": "resolved", "explanation": "確定した。", "roles": ["論者"]}
                ),
                encoding="utf-8",
            )
            return "", ""

    def home(_user_home, job_id):
        path = tmp_path / job_id
        path.mkdir()
        return path

    _install_cli_resolution(monkeypatch, lambda: executable)
    monkeypatch.setattr("fukamiru.translation.codex.subprocess.Popen", Process)
    monkeypatch.setattr(adapter, "_prepare_worker_home", home)
    monkeypatch.setattr(adapter, "_prepare_windows_ca_bundle", lambda _: None)

    worker_case.worker._invoke_manuscript_inference(
        claimed,
        "resolve_japanese_ambiguity",
        text="本文",
        context="",
        analysis={},
    )
    adapter.settings = replace(
        adapter.settings,
        translation=replace(
            adapter.settings.translation,
            manuscript_codex_model="gpt-6-astra",
        ),
    )
    with pytest.raises(InferenceEngineAuthorizationRequired):
        worker_case.worker._invoke_manuscript_inference(
            claimed,
            "resolve_japanese_ambiguity",
            text="本文",
            context="",
            analysis={},
        )
    assert len(commands) == 1
    assert commands[0][commands[0].index("--model") + 1] == "gpt-5.6-sol"


def test_codex_model_and_effort_are_pinned_for_every_inference_call(
    worker_case, monkeypatch, tmp_path,
):
    adapter = CodexTranslationAdapter(worker_case.settings)
    executable = tmp_path / "fixture.exe"
    executable.write_bytes(b"fixture-codex-cli")
    commands = []
    responses = [
        {"translation": "翻訳"},
        {"status": "resolved", "explanation": "確定した。", "roles": ["主語"]},
        {"status": "resolved", "explanation": "確定した。", "roles": ["主語"]},
    ]

    class Process:
        returncode = 0

        def __init__(self, command, **_):
            commands.append(command)
            self.output = Path(command[command.index("-o") + 1])

        def communicate(self, *_args, **_kwargs):
            self.output.write_text(json.dumps(responses.pop(0)), encoding="utf-8")
            return "", ""

    def home(_user_home, job_id):
        path = tmp_path / job_id
        path.mkdir()
        return path

    _install_cli_resolution(monkeypatch, lambda: executable)
    monkeypatch.setattr("fukamiru.translation.codex.subprocess.Popen", Process)
    monkeypatch.setattr(adapter, "_prepare_worker_home", home)
    monkeypatch.setattr(adapter, "_prepare_windows_ca_bundle", lambda _: None)

    adapter.translate("source", source_language="fr", target_language="ja", instructions="")
    adapter.resolve_japanese_ambiguity(text="本文", context="", analysis={})
    execution = CodexTranslationAdapter.bound_execution_v22(
        worker_case.worker._language_engine_descriptor_v22(adapter)
    )
    with CodexTranslationAdapter.mandatory_manuscript_execution(execution):
        adapter.resolve_japanese_ambiguity(text="本文", context="", analysis={})

    assert len(commands) == 3
    for command in commands:
        assert command[command.index("--model") + 1] == "gpt-5.6-sol"
        assert command[command.index("--config") + 1] == "model_reasoning_effort=medium"

    adapter.settings = replace(
        adapter.settings,
        translation=replace(
            adapter.settings.translation, manuscript_codex_model="gpt-6-astra"
        ),
    )
    with pytest.raises(FukamiruError, match="Codex model must be gpt-5.6-sol"):
        adapter.translate("source", source_language="fr", target_language="ja", instructions="")
    with pytest.raises(FukamiruError, match="reasoning effort must be medium"):
        adapter._complete_json("prompt", {}, operation="test", reasoning_effort="low")
    assert len(commands) == 3


def test_v22_language_generation_schema_is_relaxed_but_python_acceptance_is_strict(
    worker_case, monkeypatch, tmp_path,
):
    observed = {}

    def response(schema):
        observed["required"] = schema["properties"]["records"]["items"]["required"]
        observed["properties"] = schema["properties"]["records"]["items"][
            "properties"
        ]
        return {"records": [{"block_id": "P1"}]}

    adapter, _calls, _script = install_cli_fixture(
        worker_case, monkeypatch, tmp_path, [response]
    )
    payload = adapter.plan_manuscript_language(
        phase="plan",
        blocks=[{"id": "P1"}],
        first_mentions=[],
        evidence=[],
        plan=[],
        workflow_version=22,
    )
    assert set(observed["required"]) == set(observed["properties"])
    assert all(
        "null" in field_schema["type"]
        for field_schema in observed["properties"].values()
    )
    assert payload == {
        "records": [
            {"block_id": "P1", "_response_contract_error": "invalid_record_schema"}
        ]
    }


def test_v22_ambiguity_incomplete_content_reaches_single_supplement(
    worker_case, monkeypatch, tmp_path,
):
    adapter = CodexTranslationAdapter(worker_case.settings)
    executable = tmp_path / "fixture.exe"
    executable.write_bytes(b"fixture-codex-cli")
    schemas = []
    responses = [
        {"status": "resolved", "roles": []},
        {"status": "resolved", "explanation": "主語を確定した。", "roles": ["論者"]},
    ]

    class Process:
        returncode = 0

        def __init__(self, command, **_):
            schema_path = Path(command[command.index("--output-schema") + 1])
            schemas.append(json.loads(schema_path.read_text()))
            self.output = Path(command[command.index("-o") + 1])

        def communicate(self, *_args, **_kwargs):
            self.output.write_text(json.dumps(responses.pop(0)), encoding="utf-8")
            return "", ""

    def home(_user_home, job_id):
        path = tmp_path / job_id
        path.mkdir()
        return path

    _install_cli_resolution(monkeypatch, lambda: executable)
    monkeypatch.setattr("fukamiru.translation.codex.subprocess.Popen", Process)
    monkeypatch.setattr(adapter, "_prepare_worker_home", home)
    monkeypatch.setattr(adapter, "_prepare_windows_ca_bundle", lambda _: None)
    monkeypatch.setattr(
        japanese_analysis,
        "parse_japanese",
        lambda _: {"status": "unresolved", "ambiguities": ["implicit_subject"]},
    )

    execution = CodexTranslationAdapter.bound_execution_v22(
        worker_case.worker._language_engine_descriptor_v22(adapter)
    )

    def resolve(**kwargs):
        with CodexTranslationAdapter.mandatory_manuscript_execution(execution):
            return adapter.resolve_japanese_ambiguity(**kwargs)

    with worker_case.databases.state.connect() as connection:
        result = japanese_analysis.cached_analysis_v22(
            connection,
            "本文",
            "文脈",
            resolve,
            engine_fingerprint="fixture-engine",
        )
    assert result["decision"] == "analysis_ready"
    assert len(schemas) == 2
    assert all(set(schema["required"]) == set(schema["properties"]) for schema in schemas)
    assert all(
        "null" in schema["properties"]["explanation"]["type"]
        for schema in schemas
    )
