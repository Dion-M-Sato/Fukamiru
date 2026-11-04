from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from fukamiru.config import ModelSettings
from fukamiru.local_llm import LlamaServerAdapter
from fukamiru.revisions import (
    JapaneseReferenceService,
    ManuscriptDiscourseService,
    ManuscriptRewriteService,
    extract_collocation_frames,
    generate_probes,
)
from fukamiru.revisions.surface_warning import EFFORT, MODEL
from fukamiru.storage.payloads import PayloadStore
from fukamiru.storage.runs import RunRepository
from fukamiru.worker.collocation import _usable_mean_character_deltas
from fukamiru.worker.runner import ClaimedNode
from fukamiru.workflow.templates import (
    manuscript_rewrite_workflow,
    manuscript_rewrite_workflow_v28,
    manuscript_rewrite_workflow_v29,
)


def test_v29_replaces_only_the_six_proofreading_stages() -> None:
    old_ids = {
        "resolve_manuscript_predicate_relations",
        "analyze_japanese_structure",
        "detect_japanese_polish_targets",
        "polish_manuscript_japanese",
        "select_japanese_polish_candidates",
        "japanese_surface_quality_gate",
    }
    new_ids = [
        "extract_proofreading_sentences",
        "generate_proofreading_candidates",
        "preprocess_proofreading_candidates",
        "screen_proofreading_with_luna",
        "select_proofreading_candidates",
        "publish_proofreading_shadow",
    ]
    v28 = manuscript_rewrite_workflow_v28()
    v29 = manuscript_rewrite_workflow_v29()
    assert manuscript_rewrite_workflow().model_dump() == v29.model_dump()
    assert old_ids <= {node.id for node in v28.nodes}
    assert old_ids.isdisjoint(node.id for node in v29.nodes)
    assert {node.id for node in v29.nodes if node.id in new_ids} == set(new_ids)
    assert v29.next_nodes("validate_rewrite_coherence", "success") == [new_ids[0]]
    assert v29.next_nodes("validate_rewrite_coherence", "conditional_success") == [new_ids[0]]
    for source, target in zip(new_ids, new_ids[1:], strict=False):
        assert v29.next_nodes(source, "success") == [target]
    assert v29.next_nodes(new_ids[-1], "success") == ["render_manuscript_references"]
    assert v29.next_nodes(new_ids[-1], "needs_review") == []
    generator = next(node for node in v29.nodes if node.id == "generate_proofreading_candidates")
    screener = next(node for node in v29.nodes if node.id == "screen_proofreading_with_luna")
    assert generator.resource_lane == "local_llm_gpu"
    assert screener.resource_lane == "codex_remote"
    assert v29.next_nodes("repair_final_coherence", "success") == ["render_evidence_footnotes"]
    assert v29.next_nodes("repair_final_coherence", "conditional_success") == [
        "render_evidence_footnotes"
    ]
    assert v29.next_nodes("render_evidence_footnotes", "success") == [
        "validate_footnoted_references"
    ]
    assert v29.next_nodes("validate_footnoted_references", "success") == [
        "validate_footnoted_structure"
    ]
    assert v29.next_nodes("validate_footnoted_structure", "success") == [
        "validate_footnoted_coherence"
    ]
    assert v29.next_nodes("validate_footnoted_coherence", "success") == ["freeze_final_candidate"]
    assert v29.next_nodes("validate_footnoted_coherence", "conditional_success") == [
        "freeze_final_candidate"
    ]


def test_authoring_v30_authorizes_screening_without_conflating_safety_and_selection(
    worker_case,
) -> None:
    claimed = ClaimedNode(
        node_run_id="node-run",
        run_id="run",
        node_id="screen_proofreading_with_luna",
        attempt=1,
        workflow_name="authoring",
        workflow_version="30",
        resource_lane="codex_remote",
        lease_owner="test",
        project_id=None,
        inputs={},
    )
    with patch(
        "fukamiru.worker.runner.proofreading_worker_v29.evaluate_luna",
        side_effect=lambda _worker, _claimed, context: context,
    ):
        result = worker_case.worker._manuscript_screen_proofreading_with_luna_v29(
            claimed, {"proposed_html": "<article></article>"}
        )

    assert MODEL == "gpt-5.6-luna"
    assert EFFORT == "none"
    assert result.context["_luna_external_send_authorized"] is True
    assert result.context["_luna_external_send_authorization_source"] == (
        "authoring-v30-remote-llm-workflow"
    )
    assert "auto_select_unique_clean" not in result.context
    assert "auto_select_safe_clean" not in result.context


def test_standalone_v29_proofreading_keeps_external_send_opt_in(worker_case) -> None:
    claimed = ClaimedNode(
        node_run_id="node-run",
        run_id="run",
        node_id="screen_proofreading_with_luna",
        attempt=1,
        workflow_name="manuscript_rewrite",
        workflow_version="29",
        resource_lane="codex_remote",
        lease_owner="test",
        project_id=None,
        inputs={},
    )
    with patch(
        "fukamiru.worker.runner.proofreading_worker_v29.evaluate_luna",
        side_effect=lambda _worker, _claimed, context: context,
    ):
        result = worker_case.worker._manuscript_screen_proofreading_with_luna_v29(
            claimed, {"proposed_html": "<article></article>"}
        )

    assert "_luna_external_send_authorized" not in result.context


def test_frame_and_three_probes_remain_natural_japanese() -> None:
    html = (
        '<article id="doc"><section id="s1">'
        '<p id="p1">この分析は両者の差異を明確にする。</p>'
        "</section></article>"
    )
    frames = extract_collocation_frames(
        canonical_html=html,
        run_id="run-1",
        document_id="doc-1",
        source_preserve_block_ids=set(),
        editable_blocks=ManuscriptRewriteService.editable_blocks,
    )
    frame = next(item for item in frames if item.expression == "明確にする")
    assert frame.voice == "active"
    assert frame.predicate_type == "complex_predicate"
    assert {(item.surface, item.surface_particle) for item in frame.arguments} >= {
        ("この分析", "は"),
        ("両者の差異", "を"),
    }
    probes = generate_probes(frame)
    assert [item.kind for item in probes] == [
        "original_sentence",
        "local_predicate_frame",
        "canonicalized_frame",
    ]
    assert probes[0].prefix + frame.expression + probes[0].suffix == frame.sentence
    assert all("{" not in item.prefix for item in probes)


def test_frame_extraction_requires_complete_document_coverage_beyond_legacy_limit() -> None:
    block_count = 405
    paragraphs = "".join(
        f'<p id="p{index:03d}">走る。</p>' for index in range(block_count)
    )
    html = f'<article id="doc">{paragraphs}</article>'

    def fake_parse(value: str) -> dict[str, object]:
        return {
            "dependencies": [
                {
                    "start": match.start(),
                    "end": match.end(),
                    "pos": "VERB",
                    "lemma": "走る",
                    "head_start": -1,
                }
                for match in re.finditer("走る", value)
            ],
            "arguments": [],
        }

    with (
        patch("fukamiru.revisions.collocation.parse_japanese", side_effect=fake_parse),
        patch("fukamiru.revisions.collocation.analyzer_versions", return_value={}),
    ):
        frames = extract_collocation_frames(
            canonical_html=html,
            run_id="run-full",
            document_id="doc-full",
            source_preserve_block_ids=set(),
            editable_blocks=ManuscriptRewriteService.editable_blocks,
        )

    assert len(frames) == block_count
    assert frames[-1].block_id == "p404"
    assert frames[-1].sentence_ordinal == 0


def test_base_scorer_uses_forced_tokens_but_reads_raw_logprobs(tmp_path: Path) -> None:
    model = tmp_path / "base-Q4_K_M.gguf"
    model.write_bytes(b"fixture")
    adapter = LlamaServerAdapter(
        ModelSettings(path=model, server_command="python"),
        runtime_root=tmp_path,
        model_name="llm-jp-base",
    )
    response = {
        "tokens": [58721, 1073, 2],
        "completion_probabilities": [
            {"token": 58721, "logprob": -11.8756},
            {"token": 1073, "logprob": -0.25},
            {"token": 2, "logprob": -0.01},
        ],
    }
    with (
        patch.object(adapter, "ensure_started"),
        patch.object(
            adapter,
            "tokenize",
            side_effect=[((1, ""), (739, "を")), ((609, " "), (58721, "明確に"), (1073, "する"))],
        ),
        patch.object(adapter, "detokenize", side_effect=["差異を明確にする", "差異を"]),
        patch.object(adapter, "_request_json", return_value=response) as request,
    ):
        score = adapter.score_continuation(prefix="差異を", continuation="明確にする")
    body = request.call_args.args[1]
    assert body["temperature"] == -1.0
    assert body["post_sampling_probs"] is False
    assert body["logit_bias"] == [[1073, 100.0], [58721, 100.0]]
    assert score.token_ids == (58721, 1073)
    assert score.raw_logprob_sum == pytest.approx(-12.1256)
    assert score.mean_token_logprob == pytest.approx(-6.0628)
    assert score.mean_character_logprob == pytest.approx(-12.1256 / len("明確にする"))


def test_base_scorer_teacher_forces_ambiguous_tokenization(tmp_path: Path) -> None:
    model = tmp_path / "base-Q4_K_M.gguf"
    model.write_bytes(b"fixture")
    adapter = LlamaServerAdapter(
        ModelSettings(path=model, server_command="python"),
        runtime_root=tmp_path,
        model_name="llm-jp-base",
    )
    ambiguous = {
        "tokens": [37001, 739, 16836, 16374],
        "completion_probabilities": [
            {"token": 37001, "logprob": -0.4},
            {"token": 739, "logprob": -0.5},
            {"token": 16836, "logprob": -0.6},
            {"token": 16374, "logprob": -0.7},
        ],
    }
    forced = [
        {"tokens": [37001], "completion_probabilities": [{"token": 37001, "logprob": -1.0}]},
        {
            "tokens": [173033],
            "completion_probabilities": [{"token": 173033, "logprob": -2.0}],
        },
        {"tokens": [621], "completion_probabilities": [{"token": 621, "logprob": -3.0}]},
    ]
    with (
        patch.object(adapter, "ensure_started"),
        patch.object(
            adapter,
            "tokenize",
            side_effect=[
                ((1, ""), (16602, "選び")),
                (
                    (609, " "),
                    (37001, "するのか"),
                    (173033, "を検証する"),
                    (621, "。"),
                ),
            ],
        ),
        patch.object(
            adapter,
            "detokenize",
            side_effect=["選びするのかを検証する。", "選び"],
        ),
        patch.object(
            adapter,
            "_request_json",
            side_effect=[ambiguous, *forced],
        ) as request,
    ):
        score = adapter.score_continuation(
            prefix="選び", continuation="するのかを検証する。"
        )
    assert request.call_count == 4
    assert [call.args[1]["logit_bias"] for call in request.call_args_list[1:]] == [
        [[37001, 100.0]],
        [[173033, 100.0]],
        [[621, 100.0]],
    ]
    assert [call.args[1]["n_predict"] for call in request.call_args_list[1:]] == [1, 1, 1]
    assert score.token_ids == (37001, 173033, 621)
    assert score.token_logprobs == (-1.0, -2.0, -3.0)


def test_v29_incomplete_scores_are_not_ranked_as_comparable() -> None:
    complete = {
        "scorer_type": "llm_jp_base",
        "features": {
            "complete": True,
            "predicate": {
                "delta_from_original": {"mean_character_logprob": 0.25}
            },
        },
    }
    incomplete = {
        "scorer_type": "llm_jp_base",
        "features": {
            "complete": False,
            "predicate": {"mean_character_logprob": -1.0},
        },
    }
    deltas, scoring_incomplete = _usable_mean_character_deltas(
        [complete, complete, incomplete]
    )
    assert deltas == [0.25, 0.25]
    assert scoring_incomplete is True

    deltas, scoring_incomplete = _usable_mean_character_deltas(
        [complete, complete, complete]
    )
    assert deltas == [0.25, 0.25, 0.25]
    assert scoring_incomplete is False


@pytest.fixture
def worker_case():
    from test_worker import WorkerTests

    case = WorkerTests()
    case.setUp()
    try:
        yield case
    finally:
        case.tearDown()


def test_v29_worker_renders_evidence_footnotes_after_final_repairs(worker_case) -> None:
    run_id = worker_case._create_run("manuscript_rewrite")
    with worker_case.databases.state.connect() as connection:
        node = connection.execute(
            "SELECT id,node_id,attempt,resource_lane FROM node_runs "
            "WHERE run_id=? ORDER BY rowid LIMIT 1",
            (run_id,),
        ).fetchone()
    claimed = ClaimedNode(
        node_run_id=str(node["id"]),
        run_id=run_id,
        node_id="render_evidence_footnotes",
        attempt=int(node["attempt"]),
        workflow_name="manuscript_rewrite",
        workflow_version="29",
        resource_lane=str(node["resource_lane"]),
        lease_owner="test",
        project_id=None,
        inputs={},
    )
    reference = {
        "reference_id": "reference:ishida-2016",
        "title": "キテイ『愛の労働』",
        "record_type": "chapter",
        "publication_language": "ja",
        "container_title": "リベラリズム",
        "publisher": "東京大学出版会",
        "year": "2016",
        "pages": "268-269",
        "author": "石田格",
        "editors": ["宇野重規"],
        "parse_status": "parsed",
        "reference_usage": {"evidence_id": "E-1"},
        "bibliography_people": [
            {
                "role": "author",
                "name_profile": "japanese_native",
                "family": "石田",
                "given": "格",
            },
            {
                "role": "editor",
                "name_profile": "japanese_native",
                "family": "宇野",
                "given": "重規",
            },
        ],
    }
    uncited_reference = {
        **reference,
        "reference_id": "reference:unused",
        "title": "未使用文献",
        "reference_usage": {"evidence_id": "E-2"},
    }
    html = (
        '<article id="D" lang="ja"><section id="S"><h2 id="H">本文</h2>'
        '<p id="P">根拠に基づく追加文章。</p></section></article>'
    )
    context = {
        "proposed_html": html,
        "applied_edits": [
            {
                "operation": "replace_text",
                "block_id": "P",
                "output_block_id": "P",
                "evidence_ids": ["E-1"],
            }
        ],
        "evidence_context": [
                {
                    "id": "E-1",
                    "provider": "knowledge_base",
                    "provider_record_id": "chunk-1",
                    "authors": ["石田格"],
                    "provenance": {
                    "citation": {
                        "source_span_id": "SPAN-1",
                        "source_asset_id": "ASSET-1",
                        "start_page": 268,
                        "end_page": 269,
                    }
                },
            }
        ],
        "reference_entries": [reference, uncited_reference],
        "paragraph_boundary_lock": ManuscriptDiscourseService.rendered_boundary_lock(html, {}),
    }

    result = worker_case.worker._manuscript_render_evidence_footnotes_v29(claimed, context).context

    assert result["evidence_footnote_render_report"]["footnote_count"] == 1
    assert result["evidence_footnote_render_report"]["citation_event_draft_count"] == 1
    assert result["evidence_footnote_render_report"][
        "terminal_bibliography_addition_count"
    ] == 1
    assert [
        item["reference_id"] for item in result["final_bibliography_entries"]
    ] == ["reference:ishida-2016"]
    assert [
        item["reference_id"]
        for item in result["uncited_reference_review_candidates"]
    ] == ["reference:unused"]
    assert result["citation_reference_filter_report"] == {
        "policy": "citation-derived-final-bibliography-v1",
        "input_reference_count": 2,
        "cited_reference_count": 1,
        "uncited_reference_count": 1,
    }
    assert "未使用文献" not in result["proposed_html"]
    assert "268-269頁" in result["proposed_html"]
    assert result["proposed_html"].count('data-reference-id="reference:ishida-2016"') == 2
    assert any(
        edit.get("operation") == "insert_footnote_reference_in_terminal_bibliography"
        for edit in result["applied_edits"]
    )
    assert ManuscriptDiscourseService.validate_rendered_boundary_lock(
        result["proposed_html"], result["paragraph_boundary_lock"]
    )["valid"]
    reference_quality = JapaneseReferenceService.quality_report(
        result["proposed_html"],
        [reference],
        workflow_version=29,
        machine_entries=[],
    )
    assert reference_quality["passed"] is True, reference_quality

    v31_html = (
        '<article id="D" lang="ja"><section id="S"><h2 id="H">本文</h2>'
        '<p id="P">石田格は根拠に基づく追加文章を示した。</p></section></article>'
    )
    v31 = worker_case.worker._manuscript_render_evidence_footnotes_v29(
        replace(claimed, workflow_name="authoring", workflow_version="31"),
        {
            **context,
            "proposed_html": v31_html,
            "paragraph_boundary_lock": ManuscriptDiscourseService.rendered_boundary_lock(
                v31_html, {}
            ),
            "evidence_uses": [{
                "evidence_use_id": "EU-S-E1",
                "section_id": "S",
                "claim_id": "C1",
                "sentence_id": "S-P01-S01",
                "source_ids": ["E-1"],
                "claim_role": "source_fact",
                "evidence_kind": "rag_excerpt",
                "verification_status": "verified",
                "support_locator": {"chunk_ids": ["chunk-1"]},
                "supported_content": "根拠に基づく追加文章。",
                "author_inference": None,
            }],
        },
    ).context
    link = v31["evidence_use_citation_links"][0]
    assert link["evidence_use_id"] == "EU-S-E1"
    assert link["block_id"] == "P"
    assert link["footnote_ids"]
    assert link["bibliography_entry_ids"] == ["reference:ishida-2016"]
    second_paragraph_html = v31_html.replace(
        "</p></section>", '</p><p id="P2">別の判断を述べる。</p></section>'
    )
    misplaced = worker_case.worker._manuscript_render_evidence_footnotes_v29(
        replace(claimed, workflow_name="authoring", workflow_version="31"),
        {
            **context,
            "proposed_html": second_paragraph_html,
            "paragraph_boundary_lock": (
                ManuscriptDiscourseService.rendered_boundary_lock(
                    second_paragraph_html, {}
                )
            ),
            "evidence_uses": [{
                "evidence_use_id": "EU-S-E1-P2",
                "section_id": "S",
                "claim_id": "C1",
                "sentence_id": "S-P02-S01",
                "source_ids": ["E-1"],
                "claim_role": "source_fact",
                "evidence_kind": "rag_excerpt",
                "verification_status": "verified",
                "support_locator": {"chunk_ids": ["chunk-1"]},
                "supported_content": "別の判断を述べる。",
                "author_inference": None,
            }],
        },
    ).context
    assert misplaced["evidence_footnote_render_report"][
        "unresolved_footnote_binding_count"
    ] == 1
    assert misplaced["human_review_items"][0]["sentence_id"] == "S-P02-S01"
    missing = worker_case.worker._manuscript_render_evidence_footnotes_v29(
        replace(claimed, workflow_name="authoring", workflow_version="31"),
        {
            **context,
            "proposed_html": v31_html,
            "paragraph_boundary_lock": (
                ManuscriptDiscourseService.rendered_boundary_lock(v31_html, {})
            ),
            "evidence_uses": [{
                "evidence_use_id": "EU-S-E2",
                "section_id": "S",
                "claim_id": "C2",
                "sentence_id": "S-P01-S01",
                "source_ids": ["E-2"],
                "claim_role": "source_fact",
                "evidence_kind": "rag_excerpt",
                "verification_status": "verified",
                "support_locator": {},
                "supported_content": "未描画の根拠",
                "author_inference": None,
            }],
        },
    ).context
    assert missing["evidence_footnote_render_report"][
        "unresolved_footnote_binding_count"
    ] == 1
    assert missing["human_review_items"][0]["category"] == (
        "evidence_use_footnote_unresolved"
    )


def test_v29_replays_failed_scoring_with_existing_assets(worker_case) -> None:
    run_id = worker_case._create_run("manuscript_rewrite")
    error = {
        "type": "NonRetryableFukamiruError",
        "message": "continuation_score.forced_token_mismatch",
    }
    with worker_case.worker.databases.state.connect() as connection:
        entry_id = connection.execute(
            "SELECT id FROM node_runs WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        packed = PayloadStore(connection).pack(
            {"proposed_html": '<article id="D"><p id="P">本文。</p></article>'}
        )
        connection.execute(
            "UPDATE node_runs SET node_id='generate_collocation_probes', "
            "status='completed' WHERE id=?",
            (entry_id,),
        )
        connection.execute(
            "INSERT INTO node_runs(id,run_id,node_id,attempt,status,input_json,resource_lane,"
            "error_json,completed_at) VALUES (?,?,?,1,'failed',?,'local_llm_gpu',?,?)",
            (
                "v29-failed-score",
                run_id,
                "score_with_llmjp_base",
                json.dumps(packed),
                json.dumps(error),
                "old-time",
            ),
        )
        connection.execute(
            "INSERT INTO projects(id,name,root_path,git_enabled,created_at,updated_at) "
            "VALUES ('p-v29','p-v29','p-v29',0,'now','now')"
        )
        connection.execute(
            "INSERT INTO content_documents(id,project_id,kind,title,created_at,updated_at) "
            "VALUES ('d-v29','p-v29','paper','d-v29','now','now')"
        )
        connection.execute(
            "INSERT INTO manuscript_collocation_frames("
            "id,run_id,node_run_id,document_id,block_id,sentence_ordinal,sentence_start,"
            "sentence_end,predicate_start,predicate_end,source_expression,frame_json,signature,"
            "created_at) VALUES ('f-v29',?,?,'d-v29','P',0,0,3,0,2,'本文','{}','sig','now')",
            (run_id, entry_id),
        )
        connection.execute(
            "INSERT INTO manuscript_collocation_candidates("
            "id,run_id,frame_id,candidate_ordinal,expression,source_kind,rationale,created_at) "
            "VALUES ('c-v29',?,'f-v29',0,'本文','original','','now')",
            (run_id,),
        )
        connection.execute(
            "INSERT INTO manuscript_collocation_probes("
            "id,run_id,frame_id,probe_kind,prefix_text,suffix_text,created_at) "
            "VALUES ('q-v29',?,'f-v29','original_sentence','','。','now')",
            (run_id,),
        )
        connection.execute(
            "UPDATE runs SET workflow_version='29',status='failed',error_json=?,"
            "completed_at=? WHERE id=?",
            (json.dumps(error), "old-time", run_id),
        )
        connection.commit()
        resumed = RunRepository(connection).replay_failed_v29_collocation_scoring(run_id)
        assert resumed == {
            "run_id": run_id,
            "node_run_id": "v29-failed-score",
            "node_id": "score_with_llmjp_base",
            "attempt": 1,
            "status": "queued",
            "frame_count": 1,
            "candidate_count": 1,
            "probe_count": 1,
            "score_checkpoint_count": 0,
        }
        assert RunRepository(connection).get(run_id)["status"] == "queued"
        statuses = connection.execute(
            "SELECT node_id,status FROM node_runs WHERE run_id=? ORDER BY rowid", (run_id,)
        ).fetchall()
        assert [(row["node_id"], row["status"]) for row in statuses] == [
            ("generate_collocation_probes", "completed"),
            ("score_with_llmjp_base", "queued"),
        ]
