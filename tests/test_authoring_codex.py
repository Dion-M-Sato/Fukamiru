"""Offline contract tests for the structured authoring Codex adapter."""

from __future__ import annotations

import json
import subprocess
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from fukamiru.authoring.codex import (
    STAGE_GUIDANCE,
    STAGE_MODEL,
    STAGE_REASONING_EFFORT,
    STAGE_SCHEMAS,
    CodexAuthoringAdapter,
    _validate_schema,
    describe_authoring_request,
)
from fukamiru.authoring.paragraph_moves import (
    INTRODUCTION_ARGUMENT_MOVE_IDS,
    PARAGRAPH_ARGUMENT_MOVE_IDS,
)
from fukamiru.codex_cli import CodexCLIResolution
from fukamiru.errors import (
    ExternalServiceUnavailable,
    FukamiruError,
    NonRetryableFukamiruError,
)


def _resolved_cli(executable: str = "codex") -> CodexCLIResolution:
    return CodexCLIResolution(
        configured_command="codex",
        configured_resolved=executable,
        resolved_executable=executable,
        source="path",
        version="0.158.0-alpha.2.1",
        available=True,
        authenticated=True,
        account_type="ChatGPT",
        required_models=("gpt-6-luna", "gpt-5.6-sol"),
        error_reason=None,
    )


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        paths=SimpleNamespace(runtime_root=tmp_path),
        translation=SimpleNamespace(codex_command="codex", timeout_seconds=5),
    )


def _fake_user_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    user_home = tmp_path / "user"
    source_home = user_home / ".codex"
    source_home.mkdir(parents=True)
    (source_home / "auth.json").write_text('{"auth":"test"}', encoding="utf-8")
    monkeypatch.setattr("fukamiru.authoring.codex.Path.home", lambda: user_home)
    return user_home


def test_schema_for_each_pipeline_stage_is_closed_and_requires_output() -> None:
    for _, schema in STAGE_SCHEMAS.items():
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert schema["required"]
        with pytest.raises(FukamiruError):
            _validate_schema({}, schema)


def test_source_page_images_are_explicit_cli_attachments_not_prompt_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "fukamiru.authoring.codex.require_codex_cli", lambda *_, **__: _resolved_cli()
    )
    _fake_user_home(tmp_path, monkeypatch)
    image = tmp_path / "source-page.png"
    image.write_bytes(b"synthetic image fixture; no corpus data")
    captured = []

    def fake_run(command, **_kwargs):
        captured.extend(command)
        Path(command[command.index("-o") + 1]).write_text(
            json.dumps({"classifications": []}), encoding="utf-8"
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("fukamiru.authoring.codex.subprocess.run", fake_run)
    result = CodexAuthoringAdapter(_settings(tmp_path)).generate(
        "classify_propositions", {"propositions": []}, image_paths=[image]
    )
    assert result == {"classifications": []}
    assert captured[captured.index("--image") + 1] == str(image.resolve())
    assert captured[-1] == "-"


def test_every_sent_stage_schema_meets_codex_strict_object_contract() -> None:
    def inspect(schema: dict, path: str) -> None:
        if schema.get("type") == "object":
            properties = schema["properties"]
            required = schema["required"]
            assert schema["additionalProperties"] is False, path
            assert len(required) == len(properties), path
            assert set(required) == set(properties), path
            for name, child in properties.items():
                inspect(child, f"{path}.{name}")
        elif schema.get("type") == "array":
            inspect(schema["items"], f"{path}[]")

    for stage, schema in STAGE_SCHEMAS.items():
        inspect(schema, stage)


def test_invalid_nested_wire_schema_fails_before_codex_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = deepcopy(STAGE_SCHEMAS["draft_introduction_v31"])
    treatment = schema["properties"]["sections"]["items"]["properties"][
        "evidence_treatment"
    ]["items"]
    treatment["required"].remove("support_paragraph_index")
    monkeypatch.setitem(STAGE_SCHEMAS, "draft_introduction_v31", schema)
    with pytest.raises(NonRetryableFukamiruError, match="evidence_treatment"):
        describe_authoring_request("draft_introduction_v31", {})


def test_research_question_requires_topic_coverage_and_preserves_memo_scope() -> None:
    schema = STAGE_SCHEMAS["research_question"]
    assert "topic_coverage" in schema["required"]
    question_schema = schema["properties"]["research_question"]
    assert "provenance" in question_schema["required"]
    coverage_schema = schema["properties"]["topic_coverage"]
    assert coverage_schema["minItems"] >= 1
    topic_schema = coverage_schema["items"]
    assert {
        "topic_id",
        "label",
        "proposition_ids",
        "disposition",
        "relation_to_question",
    } <= set(topic_schema["required"])
    assert topic_schema["properties"]["proposition_ids"]["minItems"] >= 1
    assert topic_schema["properties"]["disposition"]["enum"] == [
        "central",
        "supporting",
        "context",
        "research_lead",
        "excluded",
    ]

    guidance = STAGE_GUIDANCE["research_question"]
    for phrase in (
        "every substantive topic",
        "unless the user explicitly requested a narrower paper",
        "do not improve focus by silently discarding later parts of the memo",
        "Partition every supplied proposition exactly once into topic_coverage",
        "This is a coverage map, not an instruction to reuse the memo's headings or order",
        "Do not ask the user for facts that retrieval can verify",
    ):
        assert phrase in guidance


def test_section_contracts_require_article_roles_and_structural_development() -> None:
    stage_schema = STAGE_SCHEMAS["section_contracts"]
    schema = stage_schema["properties"]["sections"]["items"]
    properties = schema["properties"]
    assert "development_expectations" in schema["required"]
    assert "article_role" in schema["required"]
    development = properties["development_expectations"]
    assert development["required"] == [
        "minimum_evidence_items",
        "minimum_concrete_examples",
        "required_argument_moves",
    ]
    assert development["properties"]["required_argument_moves"]["minItems"] >= 5
    assert properties["target_length"]["type"] == ["integer", "null"]
    introduction = stage_schema["properties"]["introduction_contract"]
    assert introduction["properties"]["moves"]["minItems"] >= 5
    assert {
        "section_id", "genre_profile", "moves",
    } <= set(introduction["required"])

    guidance = STAGE_GUIDANCE["section_contracts"]
    assert "at least two" in guidance
    assert "requested_total_length" in guidance
    assert "Otherwise target_length must be null" in guidance
    assert "Never infer" in guidance
    assert "fixed total length" in guidance
    assert "at least five required_argument_moves" in guidance
    assert "not word, character, page, or paragraph-count targets" in guidance
    assert "historical-to-present loss" in guidance
    assert "must not replace the opening orientation" in guidance


def test_discourse_tree_is_an_independent_reconstruction_contract() -> None:
    node_schema = STAGE_SCHEMAS["discourse_tree"]["properties"]["nodes"]["items"]
    assert {"article_role", "discourse_purpose", "claim_ids"} <= set(
        node_schema["required"]
    )
    guidance = STAGE_GUIDANCE["discourse_tree"]
    for phrase in (
        "independent discourse reconstruction",
        "not a conversion of the claim tree into headings",
        "reorder, combine, revisit, or distribute claims",
        "their co-location does not mean their relation has been resolved",
        "Do not force every branch into a synthesis or contrast phase",
        "Do not make one section per claim",
        "Section titles should state a reader-facing question",
        "do not copy or lightly paraphrase claim-tree labels as titles",
    ):
        assert phrase in guidance


def test_argument_graph_distinguishes_external_evidence_memo_reading_and_inference() -> None:
    guidance = STAGE_GUIDANCE["argument_graph"]
    for phrase in (
        "external-source evidence",
        "retrieval alone is not verification",
        "memo close-reading",
        "not independent facts about the world",
        "LLM inference uses llm_inference provenance and remains unverified",
        "qualifier/limitation",
        "Never upgrade memo interpretation or model inference into externally verified fact",
    ):
        assert phrase in guidance


def test_research_query_planning_is_luna_mechanical_work() -> None:
    plan_schema = STAGE_SCHEMAS["research_query_plan"]["properties"]["plans"]
    assert plan_schema["items"]["properties"]["variants"]["maxItems"] == 6
    assert STAGE_REASONING_EFFORT["research_query_plan"] == "low"
    assert STAGE_MODEL["research_query_plan"] == "gpt-6-luna"


def test_draft_guidance_keeps_schema_ids_out_of_reader_prose() -> None:
    guidance = STAGE_GUIDANCE["draft"]

    assert "structured JSON metadata" in guidance
    assert "Never print claim, evidence, warrant" in guidance
    assert "memo_close_reading" in guidance
    assert "never use mechanical scaffold phrases" in guidance
    assert "提示メモ" in guidance
    assert "mechanical retrieval-planning task" in STAGE_GUIDANCE[
        "research_query_plan"
    ]


def test_draft_contract_requires_realized_academic_argument_development() -> None:
    section_schema = STAGE_SCHEMAS["draft"]["properties"]["sections"]["items"]
    required = section_schema["required"]
    assert {
        "article_role",
        "paragraphs",
        "realized_evidence_ids",
        "realized_warrant_ids",
        "realized_qualifier_ids",
        "evidence_treatment",
        "concrete_examples",
    } <= set(required)
    paragraph_schema = section_schema["properties"]["paragraphs"]
    assert "minItems" not in paragraph_schema
    assert "minLength" not in paragraph_schema["items"]["properties"]["text"]
    assert {
        "move_indices",
        "evidence_ids",
        "text",
    } <= set(paragraph_schema["items"]["required"])
    move_indices = paragraph_schema["items"]["properties"]["move_indices"]
    assert move_indices["minItems"] == 1
    assert "uniqueItems" not in move_indices
    for key in ("realized_evidence_ids", "realized_warrant_ids"):
        assert section_schema["properties"][key]["minItems"] >= 1
    assert "minItems" not in section_schema["properties"]["realized_qualifier_ids"]
    assert section_schema["properties"]["concrete_examples"]["minItems"] >= 1
    treatment_schema = section_schema["properties"]["evidence_treatment"]
    assert treatment_schema["minItems"] >= 1
    treatment_fields = treatment_schema["items"]["properties"]
    assert {
        "evidence_id",
        "claim_id",
        "basis",
        "verification_status",
        "prose_treatment",
        "qualifier_id",
        "citation_purpose",
        "attribution_sentence",
        "purpose_clause",
    } <= set(treatment_schema["items"]["required"])
    assert "memo_close_reading" in treatment_fields["basis"]["enum"]
    assert "llm_inference" in treatment_fields["basis"]["enum"]
    assert treatment_fields["qualifier_id"]["type"] == ["string", "null"]
    assert "attributed_scholarship" in treatment_fields["prose_treatment"]["enum"]

    guidance = STAGE_GUIDANCE["draft"]
    for phrase in (
        "introduction",
        "multiple developed body sections",
        "conclusion",
        "warrant",
        "qualifier",
        "Map moves and paragraphs many-to-many",
        "a paragraph may combine several tightly related moves",
        "a substantial move may span several paragraphs",
        "move_indices",
        "Do not use paragraph count or character length as a completeness proxy",
        "length is user-driven only when explicitly specified",
        "retrieval alone is not verification",
        "Memo close-reading",
        "internal provenance class",
        "not reader-facing prose",
        "LLM inference stays unverified",
        "appropriate scope",
        "never render it as externally verified fact",
        "one evidence_treatment record per used evidence ID",
        "never attach a bare footnote",
        "metadata fields are guidance, not text that must be copied verbatim",
        "unique complete sentence that names the cited author or work",
        "paragraph-level IDs determine where a later footnote may be placed",
    ):
        assert phrase in guidance


def test_semantic_paragraph_contract_is_many_to_many_without_length_quota() -> None:
    paragraph_schema = STAGE_SCHEMAS["semantic_paragraphs"]["properties"]["sections"]["items"][
        "properties"
    ]["paragraphs"]["items"]
    properties = paragraph_schema["properties"]
    assert "rhetorical_moves" in paragraph_schema["required"]
    assert "move_indices" not in properties
    guidance = STAGE_GUIDANCE["semantic_paragraphs"]
    for phrase in (
        "Map paragraphs and moves many-to-many",
        "one paragraph may realize several moves",
        "one move may be developed across several paragraphs",
        "exact required_argument_moves strings",
        "Do not generate numeric move indices",
        "derives them deterministically",
        "Do not assign paragraph-count, character, or word quotas",
    ):
        assert phrase in guidance


def test_v31_paragraph_roles_use_standard_catalog_without_schema_gate() -> None:
    contract = STAGE_SCHEMAS["section_contracts_v31"]["properties"]
    required_moves = contract["sections"]["items"]["properties"]["development_expectations"]
    required_moves = required_moves["properties"]["required_argument_moves"]["items"]
    intro_moves = contract["introduction_contract"]["properties"]["moves"]["items"]
    intro_moves = intro_moves["properties"]["move"]
    paragraph_moves = STAGE_SCHEMAS["semantic_paragraphs_v31"]["properties"]["sections"]
    paragraph_moves = paragraph_moves["items"]["properties"]["paragraphs"]["items"]
    paragraph_moves = paragraph_moves["properties"]["rhetorical_moves"]["items"]

    assert len(PARAGRAPH_ARGUMENT_MOVE_IDS) == 25
    assert required_moves == {"type": "string"}
    assert paragraph_moves == {"type": "string"}
    assert intro_moves == {"type": "string"}
    assert "research_question" in INTRODUCTION_ARGUMENT_MOVE_IDS
    assert "provisional_answer" in INTRODUCTION_ARGUMENT_MOVE_IDS
    assert "concrete_case_or_example" in PARAGRAPH_ARGUMENT_MOVE_IDS
    assert "case_analysis_or_close_reading" in PARAGRAPH_ARGUMENT_MOVE_IDS
    assert "warrant_or_explanatory_mechanism" in PARAGRAPH_ARGUMENT_MOVE_IDS
    assert "parallel_branch_contribution" in PARAGRAPH_ARGUMENT_MOVE_IDS
    assert "cross_branch_synthesis" in PARAGRAPH_ARGUMENT_MOVE_IDS
    assert "final_answer_to_research_question" in PARAGRAPH_ARGUMENT_MOVE_IDS
    assert "paragraph_argument_move_catalog" in STAGE_GUIDANCE["section_contracts_v31"]
    assert "explicitly unresolved" in STAGE_GUIDANCE["discourse_tree"]
    assert "Do not force every branch into a synthesis or contrast phase" in STAGE_GUIDANCE[
        "discourse_tree"
    ]
    assert "section may contain claims from more than one top-level topic branch" in (
        STAGE_GUIDANCE["section_contracts_v31"]
    )
    assert "cross_branch_synthesis is only for a relation actually" in STAGE_GUIDANCE[
        "semantic_paragraphs_v31"
    ]


def test_generate_routes_deliberative_stage_to_sol_medium_and_cleans_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "fukamiru.authoring.codex.require_codex_cli",
        lambda *_, **__: _resolved_cli(),
    )
    user_home = _fake_user_home(tmp_path, monkeypatch)
    captured: dict[str, object] = {}

    def fake_ca_bundle(worker_home: Path) -> Path:
        bundle = worker_home / "windows-root-ca.pem"
        bundle.write_text("test-ca", encoding="ascii")
        return bundle

    monkeypatch.setattr(
        CodexAuthoringAdapter,
        "_prepare_windows_ca_bundle",
        staticmethod(fake_ca_bundle),
    )

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        captured["command"] = command
        captured["prompt"] = kwargs["input"]
        captured["encoding"] = kwargs["encoding"]
        captured["errors"] = kwargs["errors"]
        captured["pythonioencoding"] = kwargs["env"]["PYTHONIOENCODING"]  # type: ignore[index]
        captured["pythonutf8"] = kwargs["env"]["PYTHONUTF8"]  # type: ignore[index]
        captured["home"] = kwargs["env"]["HOME"]  # type: ignore[index]
        captured["codex_home"] = kwargs["env"]["CODEX_HOME"]  # type: ignore[index]
        captured["ssl_cert_file"] = kwargs["env"]["SSL_CERT_FILE"]  # type: ignore[index]
        captured["requests_ca_bundle"] = kwargs["env"]["REQUESTS_CA_BUNDLE"]  # type: ignore[index]
        captured["ca_bundle"] = Path(str(captured["ssl_cert_file"])).read_text(
            encoding="ascii"
        )
        worker_home = Path(str(captured["codex_home"]))
        captured["auth"] = (worker_home / "auth.json").read_text(encoding="utf-8")
        captured["has_config"] = (worker_home / "config.toml").exists()
        output_file = Path(command[command.index("-o") + 1])
        output_file.write_text(json.dumps({"findings": []}), encoding="utf-8")
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 100,
                        "input_tokens_details": {"cached_tokens": 64},
                        "output_tokens": 12,
                    },
                }
            ),
            stderr="",
        )

    monkeypatch.setattr("fukamiru.authoring.codex.subprocess.run", fake_run)
    adapter = CodexAuthoringAdapter(_settings(tmp_path))  # type: ignore[arg-type]
    observations = []
    result = adapter.generate(
        "audit",
        {"draft": "媒体—欲望"},
        "retrieved source",
        observation_callback=observations.append,
    )

    command = captured["command"]
    assert result == {"findings": []}
    assert "--ephemeral" in command  # type: ignore[operator]
    assert "--ignore-user-config" in command  # type: ignore[operator]
    assert "--ignore-rules" in command  # type: ignore[operator]
    assert "--json" in command  # type: ignore[operator]
    assert command[command.index("--sandbox") + 1] == "read-only"  # type: ignore[union-attr]
    assert command[command.index("--model") + 1] == "gpt-5.6-sol"  # type: ignore[union-attr]
    assert command[command.index("-c") + 1] == "model_reasoning_effort=medium"  # type: ignore[union-attr]
    assert "媒体—欲望" in str(captured["prompt"])
    assert captured["encoding"] == "utf-8"
    assert captured["errors"] == "strict"
    assert captured["pythonioencoding"] == "utf-8"
    assert captured["pythonutf8"] == "1"
    assert captured["home"] == str(user_home)
    assert Path(str(captured["codex_home"])).name == "codex-home"
    assert captured["auth"] == '{"auth":"test"}'
    assert captured["has_config"] is False
    assert captured["ssl_cert_file"] == captured["requests_ca_bundle"]
    assert captured["ca_bundle"] == "test-ca"
    assert "retrieved source" in str(captured["prompt"])
    assert observations[0]["usage"]["input_tokens"] == 100
    assert observations[0]["usage"]["cached_input_tokens"] == 64
    assert observations[0]["status"] == "completed"
    assert "媒体—欲望" not in json.dumps(observations[0], ensure_ascii=False)
    assert not list((tmp_path / "tmp" / "codex-authoring").iterdir())


def test_mechanical_authoring_stages_use_luna_low(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "fukamiru.authoring.codex.require_codex_cli",
        lambda *_, **__: _resolved_cli(),
    )
    _fake_user_home(tmp_path, monkeypatch)
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **_: object) -> SimpleNamespace:
        captured["command"] = command
        Path(command[command.index("-o") + 1]).write_text(
            json.dumps({"classifications": []}), encoding="utf-8"
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("fukamiru.authoring.codex.subprocess.run", fake_run)
    CodexAuthoringAdapter(_settings(tmp_path)).generate(  # type: ignore[arg-type]
        "classify_propositions", {"propositions": []}
    )
    command = captured["command"]
    assert command[command.index("--model") + 1] == "gpt-6-luna"  # type: ignore[union-attr]
    assert command[command.index("-c") + 1] == "model_reasoning_effort=low"  # type: ignore[union-attr]


def test_authoring_stage_routing_matches_v30_cost_and_judgement_policy() -> None:
    assert set(STAGE_MODEL) == set(STAGE_REASONING_EFFORT) == set(STAGE_SCHEMAS)
    luna_stages = {"decompose_memo", "classify_propositions", "research_query_plan"}
    sol_medium_stages = {
        "reasoning_review",
        "research_question",
        "claim_tree",
        "argument_graph",
        "hypotheses",
        "discourse_tree",
        "manuscript_structure",
        "section_contracts",
        "semantic_paragraphs",
        "draft",
        "audit",
    }
    assert {stage for stage, model in STAGE_MODEL.items() if model == "gpt-6-luna"} == (
        luna_stages | {"format_adopted_japanese_v32"}
    )
    assert STAGE_REASONING_EFFORT["format_adopted_japanese_v32"] == "low"
    assert STAGE_MODEL["review_japanese_change_scope_v32"] == "gpt-5.6-sol"
    assert STAGE_REASONING_EFFORT["review_japanese_change_scope_v32"] == "medium"
    assert all(STAGE_REASONING_EFFORT[stage] == "low" for stage in luna_stages)
    assert all(STAGE_MODEL[stage] == "gpt-5.6-sol" for stage in sol_medium_stages)
    assert all(STAGE_REASONING_EFFORT[stage] == "medium" for stage in sol_medium_stages)
    assert STAGE_MODEL["polish"] == "gpt-5.6-sol"
    assert STAGE_REASONING_EFFORT["polish"] == "low"
    assert STAGE_MODEL["proofreading_selection"] == "gpt-5.6-sol"
    assert STAGE_REASONING_EFFORT["proofreading_selection"] == "low"
    assert STAGE_MODEL["scholarship_attribution_judgment_v31"] == "gpt-5.6-sol"
    assert STAGE_REASONING_EFFORT["scholarship_attribution_judgment_v31"] == "medium"


def test_v31_attribution_stage_judges_only_predicate_semantics() -> None:
    schema = STAGE_SCHEMAS["scholarship_attribution_judgment_v31"]
    judgment = schema["properties"]["judgments"]["items"]["properties"]
    assert judgment["classification"]["enum"] == [
        "attribution", "contextual_use", "mere_reference", "unclear"
    ]
    guidance = STAGE_GUIDANCE["scholarship_attribution_judgment_v31"]
    assert "only the semantic role of its predicate" in guidance
    assert "Judge neither whether the statement is true" in guidance
    assert "筆者は『作品名』で作品の意味を配置から捉えた is attribution" in guidance


def test_generate_adds_explicit_output_language_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "fukamiru.authoring.codex.require_codex_cli",
        lambda *_, **__: _resolved_cli(),
    )
    _fake_user_home(tmp_path, monkeypatch)
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        captured["prompt"] = kwargs["input"]
        Path(command[command.index("-o") + 1]).write_text(
            json.dumps({"classifications": []}), encoding="utf-8"
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("fukamiru.authoring.codex.subprocess.run", fake_run)
    CodexAuthoringAdapter(_settings(tmp_path)).generate(  # type: ignore[arg-type]
        "classify_propositions", {"output_language": "ja", "propositions": []}
    )
    assert "Output language: ja" in str(captured["prompt"])
    assert "generated natural-language field in that language" in str(captured["prompt"])


def test_generate_rejects_unknown_stage_and_bad_nested_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "fukamiru.authoring.codex.require_codex_cli",
        lambda *_, **__: _resolved_cli(),
    )
    _fake_user_home(tmp_path, monkeypatch)

    def fake_run(command: list[str], **_: object) -> SimpleNamespace:
        Path(command[command.index("-o") + 1]).write_text(
            json.dumps({"propositions": [{"id": "p1"}]}), encoding="utf-8"
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("fukamiru.authoring.codex.subprocess.run", fake_run)
    adapter = CodexAuthoringAdapter(_settings(tmp_path))  # type: ignore[arg-type]
    with pytest.raises(FukamiruError, match="Unknown authoring stage"):
        adapter.generate("unknown", {})
    with pytest.raises(FukamiruError, match="missing text"):
        adapter.generate("decompose_memo", {})
    assert not list((tmp_path / "tmp" / "codex-authoring").iterdir())


def test_timeout_is_translated_and_temp_files_are_cleaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "fukamiru.authoring.codex.require_codex_cli",
        lambda *_, **__: _resolved_cli(),
    )
    _fake_user_home(tmp_path, monkeypatch)

    def fake_run(*_: object, **__: object) -> None:
        raise subprocess.TimeoutExpired("codex", 5)

    monkeypatch.setattr("fukamiru.authoring.codex.subprocess.run", fake_run)
    adapter = CodexAuthoringAdapter(_settings(tmp_path))  # type: ignore[arg-type]
    with pytest.raises(FukamiruError, match="timed out"):
        adapter.generate("polish", {})
    assert not list((tmp_path / "tmp" / "codex-authoring").iterdir())


def test_failure_diagnostics_do_not_hide_stdout_behind_stderr_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "fukamiru.authoring.codex.require_codex_cli",
        lambda *_, **__: _resolved_cli(),
    )
    _fake_user_home(tmp_path, monkeypatch)

    def fake_run(*_: object, **__: object) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=1,
            stdout='{"type":"error","message":"actual API rejection"}',
            stderr="WARN shell snapshot unavailable",
        )

    monkeypatch.setattr("fukamiru.authoring.codex.subprocess.run", fake_run)
    adapter = CodexAuthoringAdapter(_settings(tmp_path))  # type: ignore[arg-type]
    with pytest.raises(FukamiruError) as raised:
        adapter.generate("polish", {})
    message = str(raised.value)
    assert "exit 1" in message
    assert "shell snapshot unavailable" in message
    assert "actual API rejection" in message
    assert not list((tmp_path / "tmp" / "codex-authoring").iterdir())


def test_codex_invalid_json_schema_is_not_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "fukamiru.authoring.codex.require_codex_cli",
        lambda *_, **__: _resolved_cli(),
    )
    _fake_user_home(tmp_path, monkeypatch)

    def fake_run(*_: object, **__: object) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=1,
            stdout='{"type":"error","message":"invalid_json_schema: missing required field"}',
            stderr="WARN shell snapshot unavailable",
        )

    monkeypatch.setattr("fukamiru.authoring.codex.subprocess.run", fake_run)
    adapter = CodexAuthoringAdapter(_settings(tmp_path))  # type: ignore[arg-type]
    with pytest.raises(NonRetryableFukamiruError, match="invalid_json_schema"):
        adapter.generate("polish", {})
    assert not list((tmp_path / "tmp" / "codex-authoring").iterdir())


def test_capacity_failure_is_classified_as_external_service_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "fukamiru.authoring.codex.require_codex_cli",
        lambda *_, **__: _resolved_cli(),
    )
    _fake_user_home(tmp_path, monkeypatch)

    def fake_run(*_: object, **__: object) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=1,
            stdout=(
                '{"type":"turn.failed","error":'
                '{"message":"Selected model is at capacity. Please try again."}}'
            ),
            stderr="WARN shell snapshot unavailable",
        )

    monkeypatch.setattr("fukamiru.authoring.codex.subprocess.run", fake_run)
    adapter = CodexAuthoringAdapter(_settings(tmp_path))  # type: ignore[arg-type]
    with pytest.raises(ExternalServiceUnavailable, match="temporarily unavailable"):
        adapter.generate("polish", {})
    assert not list((tmp_path / "tmp" / "codex-authoring").iterdir())
