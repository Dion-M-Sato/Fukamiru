from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fukamiru.codex_cli import CodexCLIResolution
from fukamiru.config import load_settings
from fukamiru.errors import FukamiruError
from fukamiru.translation.codex import CodexTranslationAdapter


class CodexTranslationAdapterTests(unittest.TestCase):
    def test_translation_prompt_uses_shared_natural_japanese_policy(self) -> None:
        adapter = object.__new__(CodexTranslationAdapter)
        captured: dict[str, str] = {}

        def capture(prompt: str, _schema: dict[str, object], **_: object) -> dict[str, str]:
            captured["prompt"] = prompt
            return {"translation": "訳文"}

        adapter._complete_json = capture  # type: ignore[method-assign]
        adapter.translate(
            "source", source_language="en", target_language="ja", instructions=""
        )

        self.assertIn("do not stack a verbal noun", captured["prompt"])
        self.assertIn("重要な役割を果たす", captured["prompt"])

    def test_v22_partial_envelope_is_structured_outputs_compatible(self) -> None:
        strict = {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "minLength": 1},
                "records": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "unit_id": {"type": "string", "enum": ["A", "B"]},
                            "score": {"type": "number", "minimum": 0},
                        },
                        "required": ["unit_id", "score"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["summary", "records"],
            "additionalProperties": False,
        }

        envelope = CodexTranslationAdapter._partial_unit_envelope_schema_v22(
            strict, {"records": "unit_id"}
        )

        def assert_required_nullable(node: dict[str, object], *, root: bool) -> None:
            declared = node.get("type")
            types = declared if isinstance(declared, list) else [declared]
            if root:
                self.assertEqual(types, ["object"])
            else:
                self.assertIn("null", types)
            if "object" in types:
                properties = node.get("properties")
                self.assertIsInstance(properties, dict)
                self.assertEqual(set(node.get("required") or []), set(properties or {}))
                self.assertFalse(node.get("additionalProperties"))
                for child in (properties or {}).values():
                    assert_required_nullable(child, root=False)
            elif "array" in types:
                assert_required_nullable(node["items"], root=False)  # type: ignore[arg-type]

        assert_required_nullable(envelope, root=True)
        record = envelope["properties"]["records"]["items"]  # type: ignore[index]
        self.assertNotIn("enum", record["properties"]["unit_id"])  # type: ignore[index]
        self.assertNotIn("minimum", record["properties"]["score"])  # type: ignore[index]

    def test_v22_rewrite_contract_adds_block_coverage_without_changing_v21(self) -> None:
        adapter = object.__new__(CodexTranslationAdapter)
        captures: list[tuple[str, dict[str, object]]] = []

        def capture(prompt: str, schema: dict[str, object], **_: object) -> dict[str, object]:
            captures.append((prompt, schema))
            return {"summary": "", "edits": [], "unresolved_questions": []}

        adapter._complete_json = capture  # type: ignore[method-assign]
        arguments = {
            "document_title": "Fixture",
            "instructions": "",
            "blocks": [{"id": "P1"}],
            "document_map": [],
            "outline_context": {},
            "evidence_context": [],
        }
        adapter.rewrite_manuscript_blocks(**arguments, workflow_version=21)
        adapter.rewrite_manuscript_blocks(**arguments, workflow_version=22)

        self.assertNotIn("checked_block_ids", captures[0][1]["properties"])
        self.assertNotIn("checked_block_ids", captures[0][1]["required"])
        self.assertIn("checked_block_ids", captures[1][1]["properties"])
        self.assertIn("checked_block_ids", captures[1][1]["required"])
        self.assertNotIn("incomplete response contract", captures[0][0])
        self.assertIn("incomplete response contract", captures[1][0])

    def test_v22_language_contract_adds_causal_review_without_changing_v21(self) -> None:
        adapter = object.__new__(CodexTranslationAdapter)
        captures: list[tuple[str, dict[str, object]]] = []

        def capture(
            prompt: str, schema: dict[str, object], **options: object
        ) -> dict[str, object]:
            captures.append(
                (prompt, options.get("acceptance_schema") or schema)  # type: ignore[arg-type]
            )
            return {"records": []}

        adapter._complete_json = capture  # type: ignore[method-assign]
        arguments = {
            "phase": "plan",
            "blocks": [],
            "first_mentions": [],
            "evidence": [],
            "plan": [],
        }
        adapter.plan_manuscript_language(**arguments, workflow_version=21)
        adapter.plan_manuscript_language(**arguments, workflow_version=22)

        v21_kinds = captures[0][1]["properties"]["records"]["items"]["properties"][
            "kind"
        ]["enum"]
        v22_kinds = captures[1][1]["properties"]["records"]["items"]["properties"][
            "kind"
        ]["enum"]
        v21_fields = captures[0][1]["properties"]["records"]["items"]["properties"]
        v22_fields = captures[1][1]["properties"]["records"]["items"]["properties"]
        self.assertNotIn("causal_relation", v21_kinds)
        self.assertIn("causal_relation", v22_kinds)
        self.assertNotIn("causal_warrant", v21_fields)
        self.assertIn("causal_warrant", v22_fields)

    def test_argument_process_schemas_are_strictly_separated(self) -> None:
        expected = {
            "block_roles": {"block_roles"},
            "reference_classification": {"reference_classifications"},
            "claim_evidence_links": {
                "argument_links",
                "research_queries",
                "unresolved_questions",
            },
        }

        for process_name, fields in expected.items():
            schema = CodexTranslationAdapter._manuscript_argument_process_schema(
                process_name
            )
            self.assertEqual(set(schema["properties"]), fields)
            self.assertEqual(set(schema["required"]), fields)
            self.assertFalse(schema["additionalProperties"])

    def test_claim_link_schema_allows_only_checkpointed_work_examples(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "local.toml"
            config.write_text(
                f"""
[paths]
state_db = "{(root / 'data/state.db').as_posix()}"
knowledge_db = "{(root / 'data/knowledge.db').as_posix()}"
runtime_root = "{(root / 'runtime').as_posix()}"
corpus_root = "{(root / 'corpus').as_posix()}"
ocr_root = "{(root / 'ocr').as_posix()}"
knowledge_artifacts_root = "{(root / 'kb').as_posix()}"
faiss_root = "{(root / 'faiss').as_posix()}"
""",
                encoding="utf-8",
            )
            adapter = CodexTranslationAdapter(load_settings(config))
            captured: dict[str, object] = {}

            def capture(
                prompt: str, schema: dict[str, object], **_: object
            ) -> dict[str, object]:
                captured["prompt"] = prompt
                captured["schema"] = schema
                return {
                    "argument_links": [],
                    "research_queries": [],
                    "unresolved_questions": [],
                }

            adapter._complete_json = capture  # type: ignore[method-assign]
            adapter.plan_manuscript_argument_process(
                process_name="claim_evidence_links",
                document_title="Fixture",
                instructions="",
                human_review="",
                reasoning_review=[],
                repair_targets={"link_source_units": [{
                    "source_block_id": "B-ARGUMENT", "source_reference_id": "R-WORK"
                }]},
                semantic_structure={
                    "sections": [
                        {
                            "section_id": "S-ARGUMENT",
                            "plannable": True,
                            "bibliography_processable": False,
                        },
                        {
                            "section_id": "S-OTHER",
                            "plannable": True,
                        },
                        {
                            "section_id": "S-REFERENCES",
                            "plannable": False,
                            "bibliography_processable": True,
                        },
                    ],
                    "blocks": [
                        {"id": "B-CLAIM", "section_id": "S-ARGUMENT", "text": "同じ節の主張"},
                        {"id": "B-OTHER", "section_id": "S-OTHER", "text": "別の節の主張"},
                        {
                            "id": "B-ARGUMENT",
                            "section_id": "S-ARGUMENT",
                            "text": "Argument source",
                            "reference_candidates": [
                                {"reference_id": "R-WORK"},
                                {"reference_id": "R-THEORY"},
                            ],
                        },
                        {
                            "id": "B-REFERENCES",
                            "section_id": "S-REFERENCES",
                            "text": "BIBLIOGRAPHY-ONLY-MARKER",
                            "reference_candidates": [
                                {"reference_id": "R-BIB-WORK"}
                            ],
                        },
                    ],
                },
                outline_context={},
                current_plan={
                    "block_roles": [
                        {"block_id": "B-CLAIM", "role": "claim"},
                        {"block_id": "B-OTHER", "role": "claim"},
                        {"block_id": "B-ARGUMENT", "role": "example"},
                    ],
                    "reference_classifications": [
                        {
                            "reference_id": "R-WORK",
                            "kind": "work_example",
                        },
                        {
                            "reference_id": "R-THEORY",
                            "kind": "theory_reference",
                        },
                        {
                            "reference_id": "R-BIB-WORK",
                            "kind": "work_example",
                        },
                    ]
                },
            )

        schema = captured["schema"]
        source_reference = schema["properties"]["argument_links"]["items"][  # type: ignore[index]
            "properties"
        ]["source_reference_id"]
        self.assertEqual(source_reference["enum"], [None, "R-WORK"])
        self.assertEqual(schema["properties"]["argument_links"]["items"][
            "properties"]["target_claim_block_id"]["enum"], ["B-CLAIM"])
        self.assertIn('"target_claim_block_ids": ["B-CLAIM"]', captured["prompt"])
        self.assertNotIn('"target_claim_block_ids": ["B-OTHER"]', captured["prompt"])
        self.assertIn("machine-generated allowlist", captured["prompt"])
        self.assertIn("R-WORK", captured["prompt"])
        self.assertIn("R-THEORY", captured["prompt"])
        self.assertNotIn("BIBLIOGRAPHY-ONLY-MARKER", captured["prompt"])

    def test_argument_process_structure_scope_is_process_specific(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "local.toml"
            config.write_text(
                f"""
[paths]
state_db = "{(root / 'data/state.db').as_posix()}"
knowledge_db = "{(root / 'data/knowledge.db').as_posix()}"
runtime_root = "{(root / 'runtime').as_posix()}"
corpus_root = "{(root / 'corpus').as_posix()}"
ocr_root = "{(root / 'ocr').as_posix()}"
knowledge_artifacts_root = "{(root / 'kb').as_posix()}"
faiss_root = "{(root / 'faiss').as_posix()}"
""",
                encoding="utf-8",
            )
            adapter = CodexTranslationAdapter(load_settings(config))
            prompts: dict[str, str] = {}

            def capture(
                prompt: str,
                schema: dict[str, object],
                *,
                operation: str,
                **_: object,
            ) -> dict[str, object]:
                prompts[operation] = prompt
                return {field: [] for field in schema["required"]}  # type: ignore[index]

            adapter._complete_json = capture  # type: ignore[method-assign]
            structure = {
                "sections": [
                    {
                        "section_id": "S-ARGUMENT",
                        "plannable": True,
                        "bibliography_processable": False,
                        "document_role": "argument",
                    },
                    {
                        "section_id": "S-REFERENCES",
                        "plannable": False,
                        "bibliography_processable": True,
                        "document_role": "references",
                    },
                    {
                        "section_id": "S-IGNORED",
                        "plannable": False,
                        "bibliography_processable": False,
                        "document_role": "other",
                    },
                ],
                "blocks": [
                    {
                        "id": "B-ARGUMENT",
                        "section_id": "S-ARGUMENT",
                        "text": "ARGUMENT-ONLY-MARKER",
                        "reference_candidates": [],
                    },
                    {
                        "id": "B-REFERENCES",
                        "section_id": "S-REFERENCES",
                        "text": "BIBLIOGRAPHY-ONLY-MARKER",
                        "reference_candidates": [],
                    },
                    {
                        "id": "B-IGNORED",
                        "section_id": "S-IGNORED",
                        "text": "UNPROCESSABLE-MARKER",
                        "reference_candidates": [],
                    },
                ],
            }
            for process_name in (
                "block_roles",
                "reference_classification",
                "claim_evidence_links",
            ):
                adapter.plan_manuscript_argument_process(
                    process_name=process_name,
                    document_title="Fixture",
                    instructions="",
                    human_review="",
                    reasoning_review=[],
                    semantic_structure=structure,
                    outline_context={},
                    current_plan={},
                )

        block_roles = prompts["manuscript argument block_roles"]
        reference_classification = prompts[
            "manuscript argument reference_classification"
        ]
        claim_links = prompts["manuscript argument claim_evidence_links"]
        self.assertIn("ARGUMENT-ONLY-MARKER", block_roles)
        self.assertNotIn("BIBLIOGRAPHY-ONLY-MARKER", block_roles)
        self.assertIn("ARGUMENT-ONLY-MARKER", reference_classification)
        self.assertIn("BIBLIOGRAPHY-ONLY-MARKER", reference_classification)
        self.assertIn('"bibliography_processable": true', reference_classification)
        self.assertIn("ARGUMENT-ONLY-MARKER", claim_links)
        self.assertNotIn("BIBLIOGRAPHY-ONLY-MARKER", claim_links)
        self.assertNotIn("UNPROCESSABLE-MARKER", reference_classification)

    def test_timeout_grace_accepts_only_schema_valid_late_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "local.toml"
            config.write_text(
                f"""
[paths]
state_db = "{(root / 'data/state.db').as_posix()}"
knowledge_db = "{(root / 'data/knowledge.db').as_posix()}"
runtime_root = "{(root / 'runtime').as_posix()}"
corpus_root = "{(root / 'corpus').as_posix()}"
ocr_root = "{(root / 'ocr').as_posix()}"
knowledge_artifacts_root = "{(root / 'kb').as_posix()}"
faiss_root = "{(root / 'faiss').as_posix()}"
""",
                encoding="utf-8",
            )
            adapter = CodexTranslationAdapter(load_settings(config))
            worker_home = root / "worker-home"
            worker_home.mkdir()
            states: list[str] = []

            class LateProcess:
                pid = 123

                def __init__(self, command: list[str]) -> None:
                    self.output_path = Path(command[command.index("-o") + 1])
                    self.returncode: int | None = None
                    self.calls = 0

                def communicate(
                    self, _input: str | None = None, *, timeout: int
                ) -> tuple[str, str]:
                    self.calls += 1
                    if self.calls == 1:
                        import subprocess

                        raise subprocess.TimeoutExpired("codex", timeout)
                    self.output_path.write_text('{"items": []}', encoding="utf-8")
                    self.returncode = 0
                    return "", ""

            def make_process(command: list[str], **_: object) -> LateProcess:
                return LateProcess(command)

            schema = {
                "type": "object",
                "properties": {"items": {"type": "array", "items": {}}},
                "required": ["items"],
                "additionalProperties": False,
            }
            with (
                patch(
                    "fukamiru.translation.codex.require_codex_cli",
                    return_value=CodexCLIResolution(
                        configured_command="codex",
                        configured_resolved="codex.exe",
                        resolved_executable="codex.exe",
                        source="path",
                        version="0.158.0-alpha.2.1",
                        available=True,
                        authenticated=True,
                        account_type="ChatGPT",
                        required_models=("gpt-6-sol",),
                        error_reason=None,
                    ),
                ),
                patch("fukamiru.translation.codex.subprocess.Popen", side_effect=make_process),
                patch.object(
                    adapter, "_prepare_worker_home", return_value=worker_home
                ),
                patch.object(adapter, "_prepare_windows_ca_bundle", return_value=None),
            ):
                payload = adapter._complete_json(
                    "fixture",
                    schema,
                    operation="late fixture",
                    timeout_seconds=1,
                    timeout_grace_seconds=2,
                    timeout_grace_callback=lambda state, _details: states.append(state),
                )

        self.assertEqual(payload, {"items": []})
        self.assertEqual(states, ["waiting", "accepted"])

    def test_structured_validation_rejects_partial_late_payload_shape(self) -> None:
        schema = {
            "type": "object",
            "properties": {"items": {"type": "array"}},
            "required": ["items"],
            "additionalProperties": False,
        }
        with self.assertRaises(FukamiruError):
            CodexTranslationAdapter._validate_structured_payload(
                {"items": "partial"}, schema, path="$"
            )

    def test_structure_stage_schemas_separate_mentions_scopes_and_references(self) -> None:
        argument = CodexTranslationAdapter._manuscript_structure_stage_schema(
            "argument"
        )
        temporal = CodexTranslationAdapter._manuscript_structure_stage_schema(
            "temporal"
        )

        self.assertIn("reference_classifications", argument["properties"])
        link_properties = argument["properties"]["argument_links"]["items"][
            "properties"
        ]
        self.assertIn("source_reference_id", link_properties)
        self.assertIn("target_claim_anchor", link_properties)
        self.assertEqual(
            set(temporal["required"]),
            {"temporal_mentions", "block_temporal_scopes"},
        )
        self.assertNotIn("temporal_constraints", temporal["properties"])

    def test_concurrent_jobs_receive_isolated_codex_homes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            user_home = root / "user"
            source_home = user_home / ".codex"
            source_home.mkdir(parents=True)
            (source_home / "auth.json").write_text('{"fixture":true}', encoding="utf-8")
            (source_home / "config.toml").write_text(
                'model = "fixture"', encoding="utf-8"
            )
            config = root / "local.toml"
            config.write_text(
                f"""
[paths]
state_db = "{(root / 'data/state.db').as_posix()}"
knowledge_db = "{(root / 'data/knowledge.db').as_posix()}"
runtime_root = "{(root / 'runtime').as_posix()}"
corpus_root = "{(root / 'corpus').as_posix()}"
ocr_root = "{(root / 'ocr').as_posix()}"
knowledge_artifacts_root = "{(root / 'kb').as_posix()}"
faiss_root = "{(root / 'faiss').as_posix()}"
""",
                encoding="utf-8",
            )
            adapter = CodexTranslationAdapter(load_settings(config))

            first = adapter._prepare_worker_home(user_home, "job-a")
            second = adapter._prepare_worker_home(user_home, "job-b")

            self.assertNotEqual(first, second)
            self.assertEqual(first.parent, second.parent)
            self.assertEqual((first / "auth.json").read_text(), '{"fixture":true}')
            self.assertEqual((second / "config.toml").read_text(), 'model = "fixture"')

    def test_locked_paragraph_schema_forbids_downstream_structure_operations(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "local.toml"
            config.write_text(
                f"""
[paths]
state_db = "{(root / 'data/state.db').as_posix()}"
knowledge_db = "{(root / 'data/knowledge.db').as_posix()}"
runtime_root = "{(root / 'runtime').as_posix()}"
corpus_root = "{(root / 'corpus').as_posix()}"
ocr_root = "{(root / 'ocr').as_posix()}"
knowledge_artifacts_root = "{(root / 'kb').as_posix()}"
faiss_root = "{(root / 'faiss').as_posix()}"
""",
                encoding="utf-8",
            )
            adapter = CodexTranslationAdapter(load_settings(config))
            captured: dict[str, object] = {}

            def capture(
                prompt: str, schema: dict[str, object], **_: object
            ) -> dict[str, object]:
                captured["prompt"] = prompt
                captured["schema"] = schema
                return {}

            adapter._complete_json = capture  # type: ignore[method-assign]
            adapter.validate_manuscript_coherence(
                document_title="Fixture",
                human_review="",
                structure_plan={},
                blocks=[
                    {
                        "id": "R1",
                        "text": "参考文献項目",
                        "rewritable_as_argument": False,
                    }
                ],
                stage="pre_polish",
                document_manifest={
                    "block_count": 1,
                    "reference_section_count": 1,
                    "reference_block_count": 1,
                    "last_section_role": "references",
                },
                editable_block_ids=[],
                paragraph_plan={"boundary_contract": {"lock_status": "locked"}},
            )

        schema = captured["schema"]
        self.assertIsInstance(schema, dict)
        properties = schema["properties"]  # type: ignore[index]
        edit_operations = properties["repair_edits"]["items"]["properties"][  # type: ignore[index]
            "operation"
        ]["enum"]
        self.assertEqual(edit_operations, ["replace_text"])
        self.assertEqual(properties["section_moves"]["maxItems"], 0)  # type: ignore[index]
        self.assertIn("document_assertions", properties)
        self.assertIn("document_assertions", schema["required"])  # type: ignore[index]
        issue_required = properties["issues"]["items"]["required"]  # type: ignore[index]
        self.assertIn("verification_scope", issue_required)
        self.assertIn("Every supplied block is observable", captured["prompt"])
        self.assertIn("参考文献項目", captured["prompt"])
        self.assertIn("ALL OTHER BLOCKS ARE READ-ONLY", captured["prompt"])
        self.assertIn("Do not infer a defect from paragraph length", captured["prompt"])


if __name__ == "__main__":
    unittest.main()
