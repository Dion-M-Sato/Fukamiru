from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from fukamiru.authoring.codex import STAGE_SCHEMAS, _validate_codex_output_schema
from fukamiru.documents.canonical_html import CanonicalHTMLValidator
from fukamiru.errors import NonRetryableFukamiruError
from fukamiru.revisions import proofreading_local as local
from fukamiru.revisions.sentence_proofreading import digest
from fukamiru.revisions.service import RevisionService
from fukamiru.storage.database import SQLiteDatabase
from fukamiru.storage.runs import RunRepository
from fukamiru.worker import sentence_proofreading as pipeline


def sentence(text="著者『Book Title』を比較視角として参照すると、差が際立つ。"):
    return dict(
        id="run:g:one:p:s0",
        source_sentence_id="p:s0",
        block_id="p",
        original_hash=digest(text),
        original_text=text,
    )


def response(source, *edits):
    return {
        **local.identity(source),
        "edits": [
            {"before": before, "after": after, "reason": "連語を自然にする"}
            for before, after in edits
        ],
    }


def test_schema_binds_existing_ids_and_every_object_is_strict():
    source = sentence()
    schema = local.response_schema(source)
    _validate_codex_output_schema(schema)
    _validate_codex_output_schema(STAGE_SCHEMAS["proofreading_local_selection_v32"])
    assert schema["properties"]["sentence_id"]["enum"] == [source["id"]]
    assert "candidate" not in schema["properties"]


def test_local_edit_keeps_ids_and_offsets_and_holds_only_unsafe_title_change():
    source = sentence()
    result = local.apply_response(
        source,
        response(
            source,
            ("比較視角", "比較視点"),
            ("Book Title", "Book\u202fTitle"),
            ("際立つ。", "際立っています。"),
        ),
    )
    assert result["candidate"] == source["original_text"].replace("比較視角", "比較視点")
    assert len(result["accepted_edits"]) == 1 and len(result["held_edits"]) == 2
    edit = result["accepted_edits"][0]
    assert source["original_text"][edit["start_offset"] : edit["end_offset"]] == "比較視角"
    assert result["sentence_id"] == source["id"]
    assert local.verify_candidate(source, result["candidate"], result) == []
    assert (
        local.apply_response(source, response(source, ("比較視角", "比較視点")))["accepted_edits"][
            0
        ]["edit_id"]
        == edit["edit_id"]
    )


def test_wrong_sentence_hash_and_ambiguous_or_overlapping_spans_cannot_move_edits():
    source = sentence()
    wrong = response(source)
    wrong["sentence_id"] = "another-sentence"
    with pytest.raises(NonRetryableFukamiruError, match="identity_mismatch"):
        local.apply_response(source, wrong)
    changed = {**source, "original_text": "別の原文。"}
    with pytest.raises(NonRetryableFukamiruError, match="source_identity"):
        local.response_schema(changed)
    repeated = sentence("比較視角と比較視角を比べる。")
    result = local.apply_response(repeated, response(repeated, ("比較視角", "比較視点")))
    assert result["candidate"] == repeated["original_text"]
    assert result["held_edits"][0]["issues"] == ["source_fragment_not_unique"]
    result = local.apply_response(
        source,
        response(
            source,
            ("比較視角", "比較視点"),
            ("比較視角として", "比較の視点として"),
        ),
    )
    assert len(result["accepted_edits"]) == 1
    assert "overlapping_edits" in result["held_edits"][0]["issues"]


@pytest.mark.parametrize(
    ("original", "before", "after"),
    [
        ("担当者は責任を成し遂げた。", "責任を成し遂げた", "責任を果たした"),
        ("責任を成し遂げたとはいえない。", "責任を成し遂げた", "責任を果たした"),
        ("影響をくれる可能性がある。", "影響をくれる", "影響を与える"),
        ("この資料で結論を達成します。", "結論を達成します", "結論を導きます"),
    ],
)
def test_required_predicate_corrections_keep_past_negation_modality_and_politeness(
    original,
    before,
    after,
):
    source = sentence(original)
    result = local.apply_response(source, response(source, (before, after)))
    assert len(result["accepted_edits"]) == 1
    assert local.style_issues(original, result["candidate"]) == []


def test_style_uses_auxiliaries_not_verb_substrings_or_quoted_speech():
    assert local.style_profile("作業を済ます。")["register"] == "plain"
    assert local.style_profile("『そうです』を参照する。")["register"] == "plain"
    assert "aspect_changed" in local.style_issues("差が際立つ。", "差が際立っている。")


def database_worker(tmp_path, *, edits=True):
    db = SQLiteDatabase(tmp_path / "state.db", Path(__file__).parents[1] / "migrations/state")
    db.migrate()
    with db.connect() as connection:
        service = RevisionService(connection)
        project = service.register_project(name="Local edits", root_path=str(tmp_path))
        document = service.create_document(project_id=project, kind="paper", title="Local edits")
        run = RunRepository(connection).create(
            workflow_name="manuscript_generator",
            workflow_version="32",
            entry_node="extract_proofreading_sentences",
            inputs={},
            project_id=project,
        )
        node = connection.execute(
            "SELECT id FROM node_runs WHERE run_id=?", (run.run_id,)
        ).fetchone()[0]
    calls = []

    def complete_json(**kwargs):
        calls.append(kwargs)
        payload = json.loads(kwargs["user_prompt"])
        items = []
        if edits and "比較視角" in payload["original_text"]:
            items = [{"before": "比較視角", "after": "比較視点", "reason": "連語を自然にする"}]
        return {**{key: payload[key] for key in local.IDENTITY_FIELDS}, "edits": items}

    worker = SimpleNamespace(
        databases=SimpleNamespace(state=db),
        llm_jp=SimpleNamespace(model_identity="test-local-model", complete_json=complete_json),
        settings=SimpleNamespace(
            paths=SimpleNamespace(runtime_root=tmp_path),
            translation=SimpleNamespace(manuscript_polish_max_output_tokens=1000),
            worker=SimpleNamespace(codex_review_concurrency=1, codex_max_concurrency=1),
        ),
        _string_list=lambda values: values or [],
        _raise_if_run_cancelled=lambda *a: None,
        execute_local_llm=lambda operation, **kw: operation(),
        _record_llm_observation=lambda *a: None,
        _authoring_reader_facing_v31_findings=lambda rows: [],
        _manuscript_predicate_inventory_record=lambda row: row,
    )
    claimed = SimpleNamespace(
        run_id=run.run_id,
        node_run_id=node,
        workflow_name="manuscript_generator",
        workflow_version="32",
    )
    context = dict(
        document_id=document,
        use_llm_jp=True,
        source_preserve_block_ids=["human"],
        proposed_html='<article id="d" lang="ja"><section id="sec"><h2 id="h">節</h2>'
        '<p id="p">比較視角から考える。次の文は維持する。</p>'
        '<p id="human">人間の保護文。</p></section></article>',
    )
    return db, worker, claimed, context, calls


@pytest.mark.parametrize("edits", [True, False])
def test_generation_checkpoints_even_noops_and_keeps_sentence_ids(tmp_path, edits):
    db, worker, claimed, context, calls = database_worker(tmp_path, edits=edits)
    context = pipeline.prepare_sentences(worker, claimed, context)
    assert context["_proofreading_local_contract"] == local.CONTRACT
    first = pipeline.generate_candidates(worker, claimed, context)
    assert len(calls) == 6
    again = pipeline.generate_candidates(worker, claimed, first)
    assert len(calls) == 6
    assert again["manuscript_proofreading_generation"]["reused_sample_count"] == 6
    with db.connect() as connection:
        sources = connection.execute(
            "SELECT id,source_sentence_id,block_id FROM manuscript_proofreading_sentences"
        ).fetchall()
        assert len(sources) == 2 and {row["block_id"] for row in sources} == {"p"}
        patches = connection.execute("SELECT * FROM manuscript_proofreading_patches").fetchall()
        assert len(patches) == (1 if edits else 0)
        assert all(
            json.loads(call["user_prompt"])["sentence_id"] in {row["id"] for row in sources}
            for call in calls
        )


def test_generation_stops_on_identity_or_deterministic_schema_failure(tmp_path):
    _, worker, claimed, context, _ = database_worker(tmp_path)
    context = pipeline.prepare_sentences(worker, claimed, context)
    count = []

    def bad(**kwargs):
        count.append(kwargs)
        raise RuntimeError("400 invalid_json_schema")

    worker.llm_jp.complete_json = bad
    with pytest.raises(NonRetryableFukamiruError, match="invalid_json_schema"):
        pipeline.generate_candidates(worker, claimed, context)
    assert len(count) == 1


def test_held_edits_survive_checkpoint_reuse_and_reach_paragraph_review(tmp_path):
    db, worker, claimed, context, calls = database_worker(tmp_path)
    original_complete = worker.llm_jp.complete_json

    def with_style_change(**kwargs):
        result = original_complete(**kwargs)
        if result["edits"]:
            result["edits"].append({
                "before": "考える。", "after": "考えます。", "reason": "丁寧にする",
            })
        return result

    worker.llm_jp.complete_json = with_style_change
    context = pipeline.prepare_sentences(worker, claimed, context)
    generated = pipeline.generate_candidates(worker, claimed, context)
    assert len(generated["human_review_items"]) == 1
    item = generated["human_review_items"][0]
    assert item["sentence_id"] == "p" and item["section_id"] == "sec"
    with db.connect() as connection:
        patch = connection.execute("SELECT * FROM manuscript_proofreading_patches").fetchone()
        assert patch["raw_candidate_text"] == "比較視点から考える。"
        assert connection.execute(
            "SELECT COUNT(*) FROM manuscript_proofreading_logs WHERE stage='local_edit_sample'"
        ).fetchone()[0] == 6
    repeated = pipeline.generate_candidates(worker, claimed, generated)
    assert len(calls) == 6
    assert repeated["human_review_items"] == generated["human_review_items"]


def test_local_pipeline_keeps_html_ids_and_rejects_candidate_tampering(tmp_path, monkeypatch):
    db, worker, claimed, context, _ = database_worker(tmp_path)
    context = pipeline.prepare_sentences(worker, claimed, context)
    context = pipeline.generate_candidates(worker, claimed, context)
    context = pipeline.preprocess(worker, claimed, context)
    with db.connect() as connection:
        patch = dict(connection.execute("SELECT * FROM manuscript_proofreading_patches").fetchone())
        assert patch["status"] == "luna_pending"
        connection.execute(
            "UPDATE manuscript_proofreading_patches SET status='clean',apply_allowed=1,"
            "luna_result_json=?",
            (json.dumps({"label": "clean", "confidence": "high"}),),
        )

    def select(_claimed, **kwargs):
        assert kwargs["stage"] == "proofreading_local_selection_v32"
        item = kwargs["payloads"][0]["sentences"][0]
        return [
            {
                "decisions": [
                    {
                        "sentence_id": item["sentence_id"],
                        "selected_candidate_id": item["candidates"][0]["candidate_id"],
                        "confidence": "high",
                        "reason_codes": ["better_collocation"],
                    }
                ]
            }
        ]

    worker._authoring_parallel_generate = select
    context = pipeline.rank_candidates(worker, claimed, context)
    context = pipeline.select_candidates(worker, claimed, context)
    monkeypatch.setattr(
        pipeline.ManuscriptDiscourseService, "predicate_candidates", lambda *a, **kw: []
    )
    published = pipeline.publish_shadow(worker, claimed, context)
    CanonicalHTMLValidator().assert_valid(published["proposed_html"])
    assert '<p id="p">比較視点から考える。次の文は維持する。</p>' in published["proposed_html"]
    assert '<p id="human">人間の保護文。</p>' in published["proposed_html"]
    assert published["manuscript_proofreading_shadow"]["applied_count"] == 1
    with db.connect() as connection:
        connection.execute(
            "UPDATE manuscript_proofreading_patches SET normalized_candidate_text=? WHERE id=?",
            ("比較視点から考えます。", patch["id"]),
        )
    refused = pipeline.publish_shadow(worker, claimed, context)
    assert refused["proposed_html"] == context["proposed_html"]
    assert refused["manuscript_proofreading_shadow"]["local_contract_rejection_count"] == 1
    assert refused["human_review_items"][0]["sentence_id"] == "p"


@pytest.mark.parametrize("interference", ["inline", "postprocess"])
def test_application_cannot_flatten_inline_or_add_unrecorded_surface_edits(
    tmp_path, monkeypatch, interference,
):
    db, worker, claimed, context, _ = database_worker(tmp_path)
    context = pipeline.prepare_sentences(worker, claimed, context)
    context = pipeline.generate_candidates(worker, claimed, context)
    context = pipeline.preprocess(worker, claimed, context)
    with db.connect() as connection:
        connection.execute(
            "UPDATE manuscript_proofreading_patches SET status='clean',apply_allowed=1,selected=1"
        )
    monkeypatch.setattr(
        pipeline.ManuscriptDiscourseService, "predicate_candidates", lambda *a, **kw: []
    )
    if interference == "inline":
        context["proposed_html"] = context["proposed_html"].replace(
            "比較視角から", '<em id="inline">比較視角</em>から'
        )
    else:
        guard = pipeline.JapaneseSurfaceQualityGate.guard_canonical_html

        def unrecorded(original, candidate):
            result = guard(original, candidate)
            if original != candidate:
                return replace(result, canonical_html=result.canonical_html.replace(
                    "比較視点から考える", "比較視点から検討する"
                ))
            return result

        monkeypatch.setattr(
            pipeline.JapaneseSurfaceQualityGate, "guard_canonical_html", unrecorded
        )
    published = pipeline.publish_shadow(worker, claimed, context)
    assert published["proposed_html"] == context["proposed_html"]
    assert published["manuscript_proofreading_shadow"]["applied_count"] == 0
    assert published["human_review_items"][0]["sentence_id"] == "p"


def test_unrecorded_preprocessing_is_held_without_replacing_candidate_identity(
    tmp_path, monkeypatch,
):
    db, worker, claimed, context, _ = database_worker(tmp_path)
    context = pipeline.prepare_sentences(worker, claimed, context)
    context = pipeline.generate_candidates(worker, claimed, context)
    with db.connect() as connection:
        before = dict(connection.execute(
            "SELECT * FROM manuscript_proofreading_patches"
        ).fetchone())
    preprocess = pipeline.rules.preprocess_candidate

    def unrecorded(*args):
        result = preprocess(*args)
        result["normalized_candidate_text"] = "比較視点から検討する。"
        return result

    monkeypatch.setattr(pipeline.rules, "preprocess_candidate", unrecorded)
    result = pipeline.preprocess(worker, claimed, context)
    with db.connect() as connection:
        after = dict(connection.execute("SELECT * FROM manuscript_proofreading_patches").fetchone())
        assert after["id"] == before["id"]
        assert after["normalized_candidate_text"] == before["raw_candidate_text"]
        assert after["status"] == "manual_check"
    assert result["human_review_items"][0]["sentence_id"] == "p"
