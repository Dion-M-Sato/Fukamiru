from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from fukamiru.authoring.codex import _validate_codex_output_schema
from fukamiru.config import ModelSettings
from fukamiru.errors import CanonicalHTMLValidationError, NonRetryableFukamiruError
from fukamiru.local_llm import LlamaServerAdapter
from fukamiru.revisions import proofreading_context as rules
from fukamiru.revisions import proofreading_local as local
from fukamiru.revisions.sentence_proofreading import digest, extract_sentences
from fukamiru.revisions.service import RevisionService
from fukamiru.storage.database import SQLiteDatabase
from fukamiru.storage.runs import RunRepository
from fukamiru.worker import proofreading_context as pipeline
from fukamiru.worker.runner import WorkerRunner
from fukamiru.workflow.display import workflow_node_metadata
from fukamiru.workflow.templates import authoring_workflow


def sentences(text="担当者は責任を成し遂げた。"):
    return extract_sentences(
        f'<article id="a"><section id="s"><p id="p">{text}</p></section></article>',
        run_id="run",
        document_id="doc",
    )


def correction(source, before, after):
    return {
        **local.identity(source),
        "original_text": source["original_text"],
        "edits": [{"before": before, "after": after, "reason": "連語を自然にする"}],
    }


def test_graph_consolidates_only_opted_in_v32_and_keeps_old_runs_available():
    old = authoring_workflow()
    new = authoring_workflow(proofreading_profile=rules.PROFILE)
    assert len(old.nodes) == 71 and len(new.nodes) == 64
    assert new.next_nodes("review_japanese_sections", "success") == [
        "japanese_surface_quality_gate"
    ]
    assert not new.next_nodes("review_japanese_sections", "failure")
    assert "screen_proofreading_with_luna" not in {n.id for n in new.nodes}
    for node_id, operation in (
        ("diagnose_japanese_prose", "prepare"),
        ("redraft_japanese_paragraphs", "propose"),
        ("review_japanese_sections", "review"),
        ("japanese_surface_quality_gate", "apply"),
        ("reconcile_candidate_context_after_proofreading", "reconcile"),
    ):
        node = next(n for n in new.nodes if n.id == node_id)
        assert node.handler == "authoring.llmjp33b_" + operation
        assert node.retry.max_attempts == 1
        assert "33B" in workflow_node_metadata(new, node_id)["function_name_ja"] or operation in {
            "prepare",
            "apply",
        }
    assert len(authoring_workflow(version="31").nodes) == 71


def test_reconciliation_predicate_check_uses_the_same_pinned_high_backend(tmp_path):
    worker, claimed, context, calls = fixture_worker(tmp_path)
    prepared = pipeline.prepare(worker, claimed, context)
    candidates = [{"candidate_id": "citation-a", "sentence": "著者は論じる。"}]
    with patch.object(pipeline, "_call", return_value={"judgments": []}) as model:
        pipeline.judge_attribution(worker, claimed, prepared, candidates)
    args = model.call_args.args
    assert args[2] == "llmjp33b_attribution_review"
    assert args[4] == {"candidates": candidates}
    schema = args[5]
    _validate_codex_output_schema(schema)
    judgments = schema["properties"]["judgments"]
    assert judgments["minItems"] == judgments["maxItems"] == 1
    assert judgments["items"]["properties"]["candidate_id"]["enum"] == ["citation-a"]
    with patch.object(
        WorkerRunner, "_authoring_reconcile_candidate_context",
        return_value=SimpleNamespace(context={"reconciled": True}),
    ) as reconcile:
        assert pipeline.reconcile(worker, claimed, prepared) == {"reconciled": True}
        reconcile.assert_called_once_with(worker, claimed, prepared)
    assert not calls


def test_generated_profile_diagram_matches_all_executable_nodes_and_edges():
    root = Path(__file__).parents[1]
    script = (root / "docs/diagrams/manuscript-proofreading-high-data.js").read_text(
        encoding="utf-8"
    )
    data = json.loads(
        script.split("window.MANUSCRIPT_GRAPH_V32 = ", 1)[1].strip().removesuffix(";")
    )
    workflow = authoring_workflow(proofreading_profile=rules.PROFILE)
    assert [(n["id"], n["handler"]) for n in data["nodes"]] == [
        (n.id, n.handler) for n in workflow.nodes
    ]
    assert [(e["from"], e["to"], e["condition"]) for e in data["edges"]] == [
        (e.source, e.target, e.condition) for e in workflow.edges
    ]
    inference = [n["inference"] for n in data["nodes"] if "inference" in n]
    assert inference == [{"model": "LLM-jp-4.1 33B BF16", "effort": "high"}] * 3


def test_every_schema_object_including_alternatives_is_strict():
    sources = sentences("原文を読む。次の文を読む。")
    schema = rules.proposal_schema(sources)
    _validate_codex_output_schema(schema)
    alternative = schema["properties"]["corrections"]["items"]["anyOf"][1]
    assert alternative["properties"]["original_text"]["enum"] == [sources[1]["original_text"]]
    alternative["required"].remove("original_hash")
    with pytest.raises(NonRetryableFukamiruError, match="required"):
        _validate_codex_output_schema(schema)
    _validate_codex_output_schema(rules.review_schema([], sources))


@pytest.mark.parametrize(
    "text,before,after",
    [
        ("媒体が欲望する役を割り当てる。", "欲望する役", "欲望される役"),
        ("担当者が資料を読む。", "資料を読む", "資料を読ませる"),
    ],
)
def test_voice_and_causative_changes_are_held_before_model_review(text, before, after):
    source = sentences(text)[0]
    audit = rules.audit_proposal(
        [source], {"corrections": [correction(source, before, after)], "human_review": []}
    )["audits"][0]
    assert not audit["accepted_edits"]
    assert audit["candidate"] == text
    assert audit["held_edits"][0]["issues"] == ["voice_or_causative_changed"]


def test_correct_collocation_preserves_tense_and_source_identity():
    source = sentences()[0]
    result = {
        "corrections": [correction(source, "責任を成し遂げた", "責任を果たした")],
        "human_review": [],
    }
    audit = rules.audit_proposal([source], result)["audits"][0]
    assert audit["candidate"] == "担当者は責任を果たした。"
    assert len(audit["accepted_edits"]) == 1
    rules.verify_audit(source, audit)
    audit["accepted_edits"][0]["edit_id"] = "different-edit"
    with pytest.raises(NonRetryableFukamiruError, match="edit_identity_changed"):
        rules.verify_audit(source, audit)
    result["corrections"][0]["original_text"] = "別の原文。"
    with pytest.raises(NonRetryableFukamiruError, match="source_mismatch"):
        rules.audit_proposal([source], result)


def test_reason_without_japanese_is_held_without_discarding_other_valid_edits():
    sources = sentences("担当者は責任を成し遂げた。同僚も責任を成し遂げた。")
    bad = correction(sources[0], "責任を成し遂げた", "責任を果たした")
    bad["edits"][0]["reason"] = "、"
    good = correction(sources[1], "責任を成し遂げた", "責任を果たした")
    audits = rules.audit_proposal(
        sources, {"corrections": [bad, good], "human_review": []}
    )["audits"]
    assert not audits[0]["accepted_edits"]
    assert audits[0]["candidate"] == sources[0]["original_text"]
    assert audits[0]["held_edits"][0]["issues"] == ["japanese_reason_required"]
    assert audits[0]["held_edits"][0]["reason"] == "、"
    assert len(audits[1]["accepted_edits"]) == 1
    for source, audit in zip(sources, audits, strict=True):
        rules.verify_audit(source, audit)


def test_duplicate_sentence_rows_are_all_held_and_other_sentences_remain_checkable():
    sources = sentences("担当者は責任を成し遂げた。同僚も責任を成し遂げた。")
    first = correction(sources[0], "責任を成し遂げた", "責任を果たした")
    other = correction(sources[1], "責任を成し遂げた", "責任を果たした")
    audits = rules.audit_proposal(
        sources, {"corrections": [first, first, other], "human_review": []}
    )["audits"]
    assert len(audits) == 3
    for audit in audits[:2]:
        assert not audit["accepted_edits"]
        assert audit["candidate"] == sources[0]["original_text"]
        assert audit["held_edits"][0]["issues"] == ["duplicate_sentence_correction"]
        rules.verify_audit(sources[0], audit)
    assert len(audits[2]["accepted_edits"]) == 1


def test_a_valid_continuative_clause_cannot_be_replaced_with_a_finite_clause():
    source = sentences("探索経路を変え、比較対象を生み出す。")[0]
    audit = rules.audit_proposal(
        [source], {"corrections": [correction(source, "変え", "変える")], "human_review": []}
    )["audits"][0]
    assert not audit["accepted_edits"]
    assert audit["held_edits"][0]["issues"] == ["clause_link_form_changed"]


def test_redundant_words_can_be_deleted_without_rewriting_the_sentence():
    source = sentences("両者の比較の差を示す。")[0]
    schema = rules.proposal_schema([source])
    assert (
        "minLength"
        not in schema["properties"]["corrections"]["items"]["anyOf"][0]["properties"]["edits"][
            "items"
        ]["properties"]["after"]
    )
    result = rules.audit_proposal(
        [source], {"corrections": [correction(source, "比較の", "")], "human_review": []}
    )
    assert result["audits"][0]["candidate"] == "両者の差を示す。"


def test_transport_sends_high_and_rejects_truncation_even_if_json_is_valid(tmp_path):
    adapter = LlamaServerAdapter(
        ModelSettings(
            path=None,
            server_command="unused",
            endpoint="http://127.0.0.1:18085",
            context_size=65536,
            reasoning_effort="high",
        ),
        runtime_root=tmp_path,
        model_name=rules.MODEL,
    )
    completed = {"choices": [{"message": {"content": '{"ok":true}'}, "finish_reason": "stop"}]}
    with (
        patch.object(adapter, "ensure_started"),
        patch.object(adapter, "_request_json", return_value=completed) as request,
    ):
        assert adapter.complete_json(
            system_prompt="原文", user_prompt="点検", **rules.DECODING
        ) == {"ok": True}
        assert request.call_args.args[1]["reasoning_effort"] == "high"
        assert request.call_args.args[1]["repeat_penalty"] == 1.1
    completed["choices"][0]["finish_reason"] = "length"
    with (
        patch.object(adapter, "ensure_started"),
        patch.object(adapter, "_request_json", return_value=completed) as request,
    ):
        with pytest.raises(NonRetryableFukamiruError, match="did not finish"):
            adapter.complete_json(system_prompt="原文", user_prompt="点検")
        assert request.call_count == 1


def test_backend_checks_actual_loaded_model_context_and_template(tmp_path):
    adapter = LlamaServerAdapter(
        ModelSettings(path=None, server_command="unused", endpoint="http://127.0.0.1:18085"),
        runtime_root=tmp_path,
        model_name=rules.MODEL,
    )
    properties = {
        "model_path": "/models/" + rules.MODEL + ".gguf",
        "default_generation_settings": {"n_ctx": 65536},
    }
    with (
        patch.object(adapter, "ensure_started"),
        patch.object(
            adapter, "_request_json", side_effect=[properties, {"prompt": "Reasoning: high\n"}]
        ),
    ):
        assert adapter.verify_contextual_proofreading_backend()["slot_context"] == 65536
    properties["model_path"] = "/models/8b-Q4.gguf"
    with (
        patch.object(adapter, "ensure_started"),
        patch.object(
            adapter, "_request_json", side_effect=[properties, {"prompt": "Reasoning: high\n"}]
        ),
        pytest.raises(NonRetryableFukamiruError, match="verified_llmjp"),
    ):
        adapter.verify_contextual_proofreading_backend()


@pytest.mark.parametrize("profile,maximum", [("llmjp33b_high", 32768), ("llmjp33b_adopt", 65180)])
def test_native_adoption_budget_matches_verified_capacity_without_mutating_settings(
    tmp_path, profile, maximum,
):
    worker, claimed, context, calls = fixture_worker(tmp_path)
    context = pipeline.prepare(worker, claimed, context)
    context["japanese_proofreading_profile"] = profile

    def complete(**kwargs):
        calls.append(kwargs)
        return {"value": "ok"}

    worker.llm_jp_proofreading.complete_json = complete
    schema = {"type": "object", "properties": {"value": {"type": "string"}},
              "required": ["value"], "additionalProperties": False}
    for _ in range(2):
        assert pipeline._call(worker, claimed, "budget-test", "形式確認", {}, schema, context) == {
            "value": "ok"
        }
    assert len(calls) == 1  # The same actual request uses the stored result.
    assert calls[0]["max_tokens"] == maximum
    assert calls[0]["reasoning_effort"] == "high"
    assert worker.settings.translation.japanese_proofreading_max_output_tokens == 32768
    worker.llm_jp_proofreading.verify_contextual_proofreading_backend = lambda: {
        "slot_context": 300
    }
    if profile == "llmjp33b_adopt":
        with pytest.raises(NonRetryableFukamiruError, match="context_window_exceeded"):
            pipeline._call(worker, claimed, "budget-too-small", "形式確認", {}, schema, context)


def fixture_worker(tmp_path, *, approve=True, replacements=True):
    db = SQLiteDatabase(tmp_path / "state.db", Path(__file__).parents[1] / "migrations/state")
    db.migrate()
    with db.connect() as connection:
        service = RevisionService(connection)
        project = service.register_project(name="Context", root_path=str(tmp_path))
        document = service.create_document(project_id=project, kind="paper", title="Context")
        run = RunRepository(connection).create(
            workflow_name="manuscript_generator",
            workflow_version="32",
            entry_node="diagnose_japanese_prose",
            project_id=project,
            inputs={"japanese_proofreading_profile": rules.PROFILE},
        )
        node = connection.execute(
            "SELECT id FROM node_runs WHERE run_id=?", (run.run_id,)
        ).fetchone()[0]
    calls = []

    def complete_json(**kwargs):
        calls.append(kwargs)
        assert kwargs["reasoning_effort"] == "high"
        assert {key: kwargs[key] for key in rules.DECODING} == rules.DECODING
        payload = json.loads(kwargs["user_prompt"])
        target_sentences = [s for p in payload["targets"]["paragraphs"] for s in p["sentences"]]
        if "edits" not in payload:
            corrections = [
                correction(s, "責任を成し遂げた", "責任を果たした")
                for s in target_sentences
                if replacements and "責任を成し遂げた" in s["original_text"]
            ]
            return {"corrections": corrections, "human_review": []}
        assert payload["candidate_paragraphs"]
        return {
            "decisions": [
                {
                    "edit_id": e["edit_id"],
                    "verdict": "accept" if approve else "reject",
                    "checks": {k: approve for k in rules.CHECKS},
                    "reason": "意味と文体を保つ修正" if approve else "原文を維持する",
                }
                for e in payload["edits"]
            ],
            "human_review": [],
        }

    adapter = SimpleNamespace(
        configured=True,
        model_name=rules.MODEL,
        model_identity="pinned-33b-bf16",
        verify_contextual_proofreading_backend=lambda: {
            "slot_context": 65536,
            "reasoning_effort": "high",
        },
        _request_json=lambda *a, **k: {"prompt": "Reasoning: high\n対象"},
        tokenize=lambda *a: [1] * 100,
        complete_json=complete_json,
    )
    worker = SimpleNamespace(
        databases=SimpleNamespace(state=db),
        llm_jp_proofreading=adapter,
        _string_list=lambda x: x or [],
        _raise_if_run_cancelled=lambda *a: None,
        execute_local_llm=lambda fn, **kwargs: fn(),
        settings=SimpleNamespace(
            translation=SimpleNamespace(japanese_proofreading_max_output_tokens=32768)
        ),
    )
    claimed = SimpleNamespace(
        run_id=run.run_id,
        node_run_id=node,
        workflow_name="manuscript_generator",
        workflow_version="32",
    )
    context = {
        "document_id": document,
        "use_llm_jp": True,
        "proposed_html": '<article id="a" lang="ja"><section id="s"><h2 id="h">節</h2>'
        '<p id="p">担当者は責任を成し遂げた。'
        "次の文は維持する。</p>"
        '<p id="protected">保護した段落。</p><p id="inline">引用<a href="#h">出典</a>'
        "。</p></section></article>",
        "human_protected_block_ids": ["protected"],
    }
    return worker, claimed, context, calls


def run_four(worker, claimed, context):
    for operation in (pipeline.prepare, pipeline.propose, pipeline.review, pipeline.apply):
        context = operation(worker, claimed, context)
    return context


def test_section_calls_replace_three_sentence_samples_and_reuse_sqlite_results(tmp_path):
    worker, claimed, context, calls = fixture_worker(tmp_path)
    result = run_four(worker, claimed, context)
    assert len(calls) == 2
    assert "担当者は責任を果たした。次の文は維持する。" in result["proposed_html"]
    assert 'id="protected">保護した段落。' in result["proposed_html"]
    assert '<a href="#h">出典</a>' in result["proposed_html"]
    assert len(result["japanese_proofreading_context"]["application"]["accepted_edits"]) == 1
    run_four(worker, claimed, context)
    assert len(calls) == 2
    with worker.databases.state.connect() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM manuscript_proofreading_logs WHERE stage LIK"
                "E 'llmjp33b_context_%' AND stage!='llmjp33b_context_application'"
            ).fetchone()[0]
            == 2
        )


def test_rejected_proposal_goes_to_human_review_without_changing_the_body(tmp_path):
    worker, claimed, context, calls = fixture_worker(tmp_path, approve=False)
    result = run_four(worker, claimed, context)
    assert "責任を成し遂げた" in result["proposed_html"]
    assert "責任を果たした" not in result["proposed_html"]
    assert result["human_review_items"][0]["sentence_id"] == "p"
    assert not result["human_review_items"][0]["blocks_render"]
    assert len(calls) == 2


def test_changed_decoding_does_not_reuse_results_from_previous_sampling(tmp_path, monkeypatch):
    worker, claimed, context, calls = fixture_worker(tmp_path)
    run_four(worker, claimed, context)
    assert len(calls) == 2
    monkeypatch.setattr(rules, "DECODING", {**rules.DECODING, "temperature": 0.71})
    run_four(worker, claimed, context)
    assert len(calls) == 4


def test_no_change_is_still_independently_reviewed_and_cached(tmp_path):
    worker, claimed, context, calls = fixture_worker(tmp_path, replacements=False)
    result = run_four(worker, claimed, context)
    assert len(calls) == 2
    assert not result["japanese_proofreading_context"]["application"]["accepted_edits"]


def test_proposal_omits_redundant_global_addresses_and_resolves_rendered_section_id(tmp_path):
    worker, claimed, context, calls = fixture_worker(tmp_path)
    context["proposed_html"] = context["proposed_html"].replace('id="s"', 'id="SEC-render-s"')
    context["section_contracts"] = [{"section_id": "s", "required_terms": ["責任"]}]
    prepared = pipeline.prepare(worker, claimed, context)
    prepared["japanese_proofreading_context"]["plan"]["repeated_terms"] = [
        {"term": "責任", "count": 10, "sentence_ids": ["outside-" + "x" * 100] * 1000}
    ]
    pipeline.propose(worker, claimed, prepared)
    payload = json.loads(calls[0]["user_prompt"])
    assert payload["document_repeated_terms"] == [{"term": "責任", "count": 10}]
    assert payload["locked_section_contracts"] == context["section_contracts"]
    assert payload["targets"]["paragraphs"][0]["sentences"][0]["id"]
    assert len(calls[0]["user_prompt"]) < 10000


def test_changed_html_or_wrong_backend_cannot_fall_back_to_8b(tmp_path):
    worker, claimed, context, calls = fixture_worker(tmp_path)
    prepared = pipeline.prepare(worker, claimed, context)
    with pytest.raises(NonRetryableFukamiruError, match="source_changed"):
        pipeline.propose(
            worker, claimed, {**prepared, "proposed_html": context["proposed_html"] + " "}
        )
    worker.llm_jp_proofreading.model_name = "llm-jp-8b"
    with pytest.raises(NonRetryableFukamiruError, match="33b_bf16_high"):
        pipeline.propose(worker, claimed, prepared)
    assert calls == []


def test_invalid_html_is_rejected_before_any_model_request(tmp_path):
    worker, claimed, context, calls = fixture_worker(tmp_path)
    context["proposed_html"] = context["proposed_html"].replace('href="#h"', 'href="#missing"')
    with pytest.raises(CanonicalHTMLValidationError, match="broken_xref"):
        pipeline.prepare(worker, claimed, context)
    assert calls == []


def test_existing_run_without_profile_retains_legacy_graph(tmp_path):
    worker, claimed, context, calls = fixture_worker(tmp_path)
    runner = WorkerRunner.__new__(WorkerRunner)
    runner.databases = worker.databases
    runner.workflow_versions = {
        ("manuscript_generator", "32"): authoring_workflow(proofreading_profile=rules.PROFILE)
    }
    new = runner._workflow_for("manuscript_generator", "32", run_id=claimed.run_id)
    assert len(new.nodes) == 64
    with worker.databases.state.connect() as connection:
        connection.execute("UPDATE runs SET input_json='{}' WHERE id=?", (claimed.run_id,))
    old = runner._workflow_for("manuscript_generator", "32", run_id=claimed.run_id)
    assert len(old.nodes) == 71
    assert digest(context["proposed_html"])
