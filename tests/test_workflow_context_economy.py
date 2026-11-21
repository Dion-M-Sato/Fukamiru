from __future__ import annotations

from dataclasses import replace

import pytest

from fukamiru.authoring.policy import enforce_authoring_quality_policy
from fukamiru.errors import ExternalServiceUnavailable
from fukamiru.revisions import ManuscriptDiscourseService, submission_validation
from fukamiru.storage.llm_observations import LLMObservationRepository
from fukamiru.storage.runs import RunRepository


@pytest.fixture
def case():
    from test_worker import WorkerTests

    value = WorkerTests()
    value.setUp()
    yield value
    value.tearDown()


def authoring_claim(case):
    run_id = case._create_run(
        "manuscript_generator", extra_inputs={"note_text": "根拠と結論。"}
    )
    claimed = case.worker._claim(lane="ingestion_cpu")
    assert claimed is not None and claimed.run_id == run_id
    return replace(
        claimed,
        node_id="draft_sections",
        workflow_version="30",
        resource_lane="codex_remote",
    )


def test_v30_authoring_policy_removes_economy_quality_bypasses() -> None:
    normalized = enforce_authoring_quality_policy(
        {
            "note_text": "根拠と結論。",
            "economy_priority": True,
            "authoring_economy_semantic_cleanup_only": True,
            "economy_deferred_machine_reference_count": 3,
        }
    )

    assert normalized["note_text"] == "根拠と結論。"
    assert "economy_priority" not in normalized
    assert "authoring_economy_semantic_cleanup_only" not in normalized
    assert "economy_deferred_machine_reference_count" not in normalized
    assert normalized["authoring_workflow_policy"]["quality_gate_mode"] == (
        "fail_closed"
    )
    assert normalized["authoring_workflow_policy"][
        "economy_priority_supported"
    ] is False


def test_authoring_batch_checkpoint_reuses_completed_payload(case, monkeypatch) -> None:
    work = case.worker
    claimed = authoring_claim(case)
    original = work.authoring_adapter.generate
    calls = 0

    def generate(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(work.authoring_adapter, "generate", generate)
    payload = {"note": "根拠と結論。"}
    first = work._authoring_parallel_generate(
        claimed,
        stage="decompose_memo",
        payloads=[payload],
        operation_name="checkpoint-test",
    )
    second = work._authoring_parallel_generate(
        claimed,
        stage="decompose_memo",
        payloads=[payload],
        operation_name="checkpoint-test",
    )

    assert second == first
    assert calls == 1


def test_authoring_batch_checkpoint_preserves_sibling_success(case, monkeypatch) -> None:
    work = case.worker
    claimed = authoring_claim(case)
    original = work.authoring_adapter.generate
    calls = []
    failing = True

    def generate(stage, payload, context=""):
        calls.append(payload["note"])
        if failing and payload["note"] == "A":
            raise ExternalServiceUnavailable("fixture")
        return original(stage, payload, context=context)

    monkeypatch.setattr(work.authoring_adapter, "generate", generate)
    monkeypatch.setattr(work, "_reserve_codex_subslots", lambda *_, **__: 1)
    monkeypatch.setattr(work, "_release_codex_subslots", lambda *_, **__: None)
    payloads = [{"note": "A"}, {"note": "B"}]

    with pytest.raises(ExternalServiceUnavailable):
        work._authoring_parallel_generate(
            claimed,
            stage="decompose_memo",
            payloads=payloads,
            operation_name="checkpoint-failure-test",
        )
    assert sorted(calls) == ["A", "B"]

    failing = False
    calls.clear()
    work._authoring_parallel_generate(
        claimed,
        stage="decompose_memo",
        payloads=payloads,
        operation_name="checkpoint-failure-test",
    )
    assert calls == ["A"]


def test_llm_observability_summary_is_content_free(case) -> None:
    claimed = authoring_claim(case)
    with case.databases.state.connect() as connection:
        LLMObservationRepository(connection).record(
            run_id=claimed.run_id,
            node_run_id=claimed.node_run_id,
            observation={
                "stage": "draft",
                "model": "gpt-5.6-sol",
                "reasoning_effort": "medium",
                "status": "completed",
                "prompt_characters": 1000,
                "prompt_utf8_bytes": 1200,
                "payload_characters": 600,
                "context_characters": 200,
                "schema_characters": 300,
                "output_characters": 400,
                "payload_sha256": "a" * 64,
                "prompt_prefix_sha256": "b" * 64,
                "latency_seconds": 2.5,
                "error_kind": None,
                "usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 60,
                    "cache_write_tokens": 20,
                    "output_tokens": 30,
                    "reasoning_tokens": 10,
                },
                "metadata": {"invocation_kind": "authoring_batch"},
            },
        )
        summary = RunRepository(connection).snapshot(claimed.run_id)[
            "llm_observability"
        ]

    assert summary["total"]["input_tokens"] == 100
    assert summary["total"]["cached_input_ratio"] == 0.6
    assert summary["by_stage"][0]["stage"] == "draft"
    assert "prompt" not in summary["by_stage"][0]


def test_draft_evidence_projection_uses_claim_contract_links(case) -> None:
    context = {
        "evidence_context": [
            {"id": "E1", "finding_key": "c1", "excerpt": "one"},
            {"id": "E2", "finding_key": "c2", "excerpt": "two"},
        ],
        "authoring_evidence_contracts": [
            {"claim_id": "c1", "accepted_evidence_ids": ["E1"]},
            {"claim_id": "c2", "accepted_evidence_ids": ["E2"]},
        ],
    }

    projected = case.worker._authoring_evidence_projection(
        context, [{"section_id": "s1", "claim_ids": ["c1"]}]
    )

    assert [item["id"] for item in projected["accepted_evidence"]] == ["E1"]
    assert [item["claim_id"] for item in projected["claim_contracts"]] == ["c1"]


def test_authoring_rag_disabled_skips_all_semantic_context_work(case) -> None:
    assert (
        case.worker._authoring_retrieval_context(
            {
                "use_rag": False,
                "note_text": "検索してはならないメモ",
                "research_question": {"text": "検索してはならない問い"},
            }
        )
        == ""
    )


def test_reconcile_invalidates_reports_from_the_previous_candidate(
    case, monkeypatch
) -> None:
    monkeypatch.setattr(
        case.worker,
        "_authoring_v29_plan_bridge",
        lambda context, proposed_html, **_kwargs: {
            "structure_plan_validation": {"valid": True},
            "bridge_saw_stale_audits": any(
                key in context
                for key in (
                    "audit_findings",
                    "authoring_economy_audit_blockers",
                    "draft_audit_join",
                    "source_preservation_validation",
                )
            ),
        },
    )
    result = case.worker._authoring_reconcile_candidate_context(
        authoring_claim(case),
        {
            "proposed_html": '<article id="D" lang="ja"></article>',
            "audit_findings": [{"code": "old"}],
            "authoring_economy_audit_blockers": [{"code": "old"}],
            "draft_audit_join": {"candidate_sha256": "old"},
            "source_preservation_validation": {"candidate_sha256": "old"},
            "evidence_context": [{"id": "E1"}],
        },
    ).context

    assert result["bridge_saw_stale_audits"] is False
    assert result["evidence_context"] == [{"id": "E1"}]
    for key in (
        "audit_findings",
        "authoring_economy_audit_blockers",
        "draft_audit_join",
        "source_preservation_validation",
    ):
        assert key not in result


def test_reconcile_routes_submission_repair_back_to_footnote_render(
    case, monkeypatch
) -> None:
    monkeypatch.setattr(
        case.worker,
        "_authoring_v29_plan_bridge",
        lambda context, proposed_html, **_kwargs: {
            "structure_plan_validation": {"valid": True},
        },
    )

    result = case.worker._authoring_reconcile_candidate_context(
        authoring_claim(case),
        {
            "proposed_html": '<article id="D" lang="ja"></article>',
            "submission_validation_gate": {
                "failed_validations": ["footnoted_coherence"]
            },
            "evidence_footnote_render_report": {"footnote_count": 1},
            "footnoted_coherence_report": {"passed": False},
            "footnoted_block_verification_records": [{"block_id": "P1"}],
            "reference_quality_gate": {"passed": True},
            "rendered_structure_v26": {"valid": True},
        },
    )

    assert result.outcome == "conditional_success"
    assert result.context["evidence_footnote_render_report"]["footnote_count"] == 1
    for key in (
        "submission_validation_gate",
        "footnoted_coherence_report",
        "footnoted_block_verification_records",
        "reference_quality_gate",
        "rendered_structure_v26",
    ):
        assert key not in result.context


def test_draft_audit_join_stops_after_bounded_repairs_and_preserves_blockers(
    case,
) -> None:
    result = case.worker._authoring_join_draft_audits(
        authoring_claim(case),
        {
            "proposed_html": '<article id="D" lang="ja"></article>',
            "repair_count": 3,
            "source_preservation_validation": {"passed": True},
            "audit_findings": [
                {
                    "code": "missing_warrant",
                    "severity": "major",
                    "object_id": "plan@1:sec-2",
                    "message": "根拠と主張の接続を人間が確認する必要がある。",
                }
            ],
        },
    )

    assert result.outcome == "success"
    assert result.context["draft_audit_join"] == {
        "passed": False,
        "source_preservation_passed": True,
        "blocking_audit_count": 1,
        "repair_count": 3,
        "repair_limit": 3,
        "repair_exhausted": True,
        "continued_with_unresolved_findings": True,
        "candidate_sha256": result.context["draft_audit_join"]["candidate_sha256"],
    }
    assert result.context["required_user_information"][0]["reason"] == (
        "根拠と主張の接続を人間が確認する必要がある。"
    )


def test_draft_audit_join_retries_before_repair_limit(case) -> None:
    result = case.worker._authoring_join_draft_audits(
        authoring_claim(case),
        {
            "proposed_html": '<article id="D" lang="ja"></article>',
            "repair_count": 2,
            "source_preservation_validation": {"passed": True},
            "audit_findings": [{"severity": "major", "message": "未解決"}],
        },
    )

    assert result.outcome == "failure"
    assert result.context["draft_audit_join"]["repair_exhausted"] is False


def test_economy_footnotes_do_not_render_deferred_machine_references(case) -> None:
    claimed = authoring_claim(case)
    html = (
        '<article id="D" lang="ja"><section id="S"><h2 id="H">本文</h2>'
        '<p id="P">本文。</p></section></article>'
    )
    context = {
        "proposed_html": html,
        "economy_priority": True,
        "applied_edits": [],
        "evidence_context": [],
        "reference_entries": [],
        "machine_reference_entries": [
            {"title": "未使用の検討候補", "authors": ["研究者"]}
        ],
        "paragraph_boundary_lock": (
            ManuscriptDiscourseService.rendered_boundary_lock(html, {})
        ),
    }

    footnoted = case.worker._manuscript_render_evidence_footnotes_v29(
        claimed, context
    ).context
    rendered = case.worker._manuscript_render_references(claimed, footnoted).context

    assert "参考文献" not in footnoted["proposed_html"]
    assert "未使用の検討候補" not in rendered["proposed_html"]
    assert rendered["economy_deferred_machine_reference_count"] == 1


def test_authoring_footnotes_skip_unverified_rag_inference_without_bibliography(
    case,
) -> None:
    claimed = authoring_claim(case)
    html = (
        '<article id="D" lang="ja"><section id="S"><h2 id="H">本文</h2>'
        '<p id="P">未検証の比較仮説を限定して示す。</p></section></article>'
    )
    context = {
        "proposed_html": html,
        "economy_priority": True,
        "applied_edits": [
            {
                "operation": "replace_text",
                "block_id": "P",
                "output_block_id": "P",
                "evidence_ids": ["rag-candidate"],
            }
        ],
        "evidence_context": [
            {
                "id": "rag-candidate",
                "provider": "knowledge_base",
                "provider_record_id": "chunk-1",
            }
        ],
        "argument_nodes": [
            {
                "id": "e1",
                "node_type": "evidence",
                "claim_id": "c1",
                "provenance": {
                    "source": "rag",
                    "source_ids": ["rag-candidate"],
                    "verification_status": "unverified",
                },
            }
        ],
        "reference_entries": [],
        "paragraph_boundary_lock": (
            ManuscriptDiscourseService.rendered_boundary_lock(html, {})
        ),
    }

    result = case.worker._manuscript_render_evidence_footnotes_v29(
        claimed, context
    ).context

    assert result["proposed_html"] == html
    assert result["evidence_footnote_render_report"]["footnote_count"] == 0
    assert result["footnote_suppressed_unverified_evidence_ids"] == [
        "rag-candidate"
    ]


def test_authoring_draft_requires_late_bound_author_supplied_scholarship(case) -> None:
    context = {
        "section_contracts": [
            {
                "section_id": "s1",
                "article_role": "body",
                "claim_ids": ["c1"],
                "evidence_ids": ["e-memo"],
                "warrant_ids": ["w1"],
                "development_expectations": {
                    "required_argument_moves": ["claim"],
                    "minimum_evidence_items": 1,
                    "minimum_concrete_examples": 1,
                },
            }
        ],
        "semantic_paragraph_plan": [
            {"section_id": "s1", "paragraphs": [{"move_indices": [1]}]}
        ],
        "argument_nodes": [
            {
                "id": "e-memo",
                "node_type": "evidence",
                "claim_id": "c1",
                "provenance": {
                    "source": "memo",
                    "source_ids": ["p1"],
                    "verification_status": "unverified",
                },
            },
            {
                "id": "e-theory",
                "node_type": "evidence",
                "claim_id": "c1",
                "source_span_id": "annotation-citation:r1",
                "provenance": {
                    "source": "memo",
                    "source_ids": ["annotation-citation:r1"],
                    "verification_status": "unverified",
                },
            },
            {"id": "w1", "node_type": "warrant", "claim_id": "c1"},
            {"id": "q1", "node_type": "qualifier", "claim_id": "c1"},
        ],
        "evidence_context": [
            {
                "id": "annotation-citation:r1",
                "allowed_use": "attributed_scholarship_citation",
                "bibliographic_verification_status": "render_ready",
                "authors": ["東浩紀"],
                "title": "動物化するポストモダン",
            }
        ],
    }
    projected = case.worker._authoring_draft_contracts_with_scholarship(
        context, context["section_contracts"]
    )
    assert projected[0]["evidence_ids"] == ["e-memo", "e-theory"]
    assert projected[0]["required_scholarship_evidence_ids"] == ["e-theory"]

    section = {
        "section_id": "s1",
        "article_role": "body",
        "paragraphs": [
            {
                "move_indices": [1],
                "evidence_ids": ["e-memo"],
                "text": "メモ由来の解釈だけを示す。",
            }
        ],
        "claim_ids": ["c1"],
        "realized_evidence_ids": ["e-memo"],
        "realized_warrant_ids": ["w1"],
        "realized_qualifier_ids": ["q1"],
        "evidence_treatment": [
            {
                "evidence_id": "e-memo",
                "claim_id": "c1",
                "basis": "memo_close_reading",
                "verification_status": "unverified",
                "prose_treatment": "qualified_interpretation",
                "qualifier_id": "q1",
                "citation_purpose": "",
                "attribution_sentence": "",
                "purpose_clause": "",
            }
        ],
        "paragraph_moves_realized": ["claim"],
        "concrete_examples": ["具体例"],
    }
    for validator in (
        case.worker._authoring_draft_contract_violations,
        case.worker._authoring_economy_draft_violations,
    ):
        violations = validator(context, [section])
        assert any(
            "omitted required author-supplied scholarship" in item
            for item in violations
        )


@pytest.mark.parametrize("actual_none", [False, True])
def test_authoring_footnotes_keep_render_ready_attributed_scholarship(case, actual_none) -> None:
    claimed = authoring_claim(case)
    html = (
        '<article id="D" lang="ja"><section id="S"><h2 id="H">本文</h2>'
        '<p id="P">東浩紀の議論を、媒体配置の比較枠として用いる。</p>'
        "</section></article>"
    )
    evidence_id = "annotation-citation:azuma"
    reference = {
        "reference_id": "annotation-reference:azuma",
        "title": "動物化するポストモダン",
        "record_type": "book",
        "publication_language": "ja",
        "publisher": "講談社",
        "year": "2001",
        "author": "東浩紀",
        "parse_status": "parsed",
        "evidence_ids": [evidence_id],
        "bibliography_people": [
            {
                "role": "author",
                "name_profile": "japanese_native",
                "family": "東",
                "given": "浩紀",
            }
        ],
    }
    context = {
        "proposed_html": html,
        "economy_priority": True,
        "applied_edits": [
            {
                "operation": "replace_text",
                "block_id": "P",
                "output_block_id": "P",
                "evidence_ids": [],
                "source": "downstream_proofreading",
            }
        ],
        "authoring_paragraph_citation_lineage": [
            {
                "output_block_id": "P",
                "claim_ids": ["c1"],
                "argument_evidence_node_ids": ["e-theory"],
                "evidence_ids": [evidence_id],
            }
        ],
        "evidence_context": [
            {
                "id": evidence_id,
                "allowed_use": "attributed_scholarship_citation",
                "bibliographic_verification_status": "render_ready",
                "authors": ["東浩紀"],
                "title": "動物化するポストモダン",
                "reference_id": reference["reference_id"],
            }
        ],
        "argument_nodes": [
            {
                "id": "e-theory",
                "node_type": "evidence",
                "claim_id": "c1",
                "source_span_id": evidence_id,
                "provenance": {
                    "source": "memo",
                    "source_ids": [evidence_id],
                    "verification_status": "unverified",
                },
            }
        ],
        "reference_entries": [reference],
        "paragraph_boundary_lock": (
            ManuscriptDiscourseService.rendered_boundary_lock(html, {})
        ),
    }

    if actual_none:
        # The old plan's available source is not an actual citation by the conclusion.
        claimed = replace(claimed, workflow_version="32")
        context["japanese_proofreading_profile"] = "llmjp33b_adopt"
        context["proposed_html"] = html.replace(
            "東浩紀の議論を、媒体配置の比較枠として用いる。", "本稿の結論は、人物配置の変化である。"
        )
        context["paragraph_boundary_lock"] = ManuscriptDiscourseService.rendered_boundary_lock(
            context["proposed_html"], {}
        )
        context["realized_paragraphs"] = context["authoring_paragraph_citation_lineage"]
        context["authoring_paragraph_citation_lineage"] = []
        context["evidence_uses"] = [{
            "evidence_use_id": "u1", "section_id": "S", "claim_id": "c1",
            "sentence_id": "S-P1-S1", "source_ids": ["memo:1"],
            "citation_required": False, "citation_source_ids": [],
            "claim_role": "author_inference", "evidence_kind": "memo_summary",
            "verification_status": "review_required",
            "author_inference": "本稿の結論は、人物配置の変化である。",
        }]
        context["proofreading_adoption"] = {
            "citation_realization": {"evidence_uses_available": True}
        }
    result = case.worker._manuscript_render_evidence_footnotes_v29(
        claimed, context
    ).context

    if actual_none:
        report = result["evidence_footnote_render_report"]
        assert report["footnote_count"] == report["recovered_lineage_evidence_count"] == 0
        assert report["citation_lineage_binding_count"] == 0
        assert report["unresolved_footnote_binding_count"] == 0
        assert "本稿の結論は、人物配置の変化である。" in result["proposed_html"]
        return
    assert result["evidence_footnote_render_report"]["footnote_count"] == 1
    assert result["evidence_footnote_render_report"][
        "recovered_lineage_evidence_count"
    ] == 1
    assert result["footnote_suppressed_unverified_evidence_ids"] == []
    assert result["final_bibliography_entries"][0]["author"] == "東浩紀"
    assert "東浩紀" in result["proposed_html"]


def test_authoring_evidence_normalization_preserves_attributed_scholarship(
    case,
) -> None:
    evidence_id = "annotation-citation:azuma"
    context = {
        "argument_nodes": [
            {
                "id": "e-theory",
                "node_type": "evidence",
                "claim_id": "c1",
                "source_span_id": evidence_id,
                "provenance": {
                    "source": "memo",
                    "source_ids": [evidence_id],
                    "verification_status": "unverified",
                },
            }
        ],
        "evidence_context": [
            {
                "id": evidence_id,
                "allowed_use": "attributed_scholarship_citation",
                "bibliographic_verification_status": "render_ready",
                "authors": ["東浩紀"],
                "title": "動物化するポストモダン",
            }
        ],
    }
    sections, _ = case.worker._authoring_normalize_evidence_treatments(
        context,
        [
            {
                "section_id": "s1",
                "evidence_treatment": [
                    {
                        "evidence_id": "e-theory",
                        "claim_id": "c1",
                        "basis": "external_source",
                        "verification_status": "verified",
                        "prose_treatment": "attributed_scholarship",
                        "qualifier_id": None,
                        "citation_purpose": "比較枠として用いる",
                        "attribution_sentence": "東浩紀の議論を比較枠として用いる。",
                        "purpose_clause": "比較枠として用いる",
                    }
                ],
            }
        ],
    )

    treatment = sections[0]["evidence_treatment"][0]
    assert treatment["basis"] == "external_source"
    assert treatment["verification_status"] == "unverified"
    assert treatment["prose_treatment"] == "attributed_scholarship"
    assert treatment["citation_purpose"] == "比較枠として用いる"


def test_authoring_attribution_accepts_surname_and_main_title(case) -> None:
    requirement = {
        "authors": ["ジャック・ランシエール"],
        "title": "感性的なもののパルタージュ：美学と政治",
    }
    assert case.worker._authoring_attribution_mentions_source(
        "ランシエール『感性的なもののパルタージュ』を比較枠として用いる。",
        requirement,
    )


def test_authoring_reference_quality_fails_when_required_citations_are_empty(
    case,
) -> None:
    claimed = authoring_claim(case)
    html = (
        '<article id="D" lang="ja"><section id="S"><h2 id="H">本文</h2>'
        '<p id="P">本文。</p></section></article>'
    )
    context = {
        "proposed_html": html,
        "reference_entries": [],
        "machine_reference_entries": [],
        "reference_inventory": {"reference_section_count": 0},
        "section_contracts": [{"section_id": "s1", "claim_ids": ["c1"]}],
        "argument_nodes": [
            {
                "id": "e-theory",
                "node_type": "evidence",
                "claim_id": "c1",
                "source_span_id": "annotation-citation:azuma",
                "provenance": {
                    "source": "memo",
                    "source_ids": ["annotation-citation:azuma"],
                    "verification_status": "unverified",
                },
            }
        ],
        "evidence_context": [
            {
                "id": "annotation-citation:azuma",
                "allowed_use": "attributed_scholarship_citation",
                "bibliographic_verification_status": "render_ready",
                "authors": ["東浩紀"],
                "title": "動物化するポストモダン",
            }
        ],
        "citation_event_drafts": [],
    }

    result = case.worker._manuscript_reference_quality_gate(
        claimed, context
    ).context["reference_quality_gate"]

    assert result["passed"] is False
    assert result["expected_author_citation_count"] == 1
    assert result["rendered_author_citation_count"] == 0
    assert any(
        item.get("code") == "reference.required_author_citation_missing"
        for item in result["issues"]
    )
def test_authoring_reference_quality_accepts_rendered_singular_evidence_id(
    case,
) -> None:
    claimed = authoring_claim(case)
    html = (
        '<article id="D" lang="ja"><section id="S"><h2 id="H">本文</h2>'
        '<p id="P">東浩紀『動物化するポストモダン』を比較枠として用いる。</p>'
        '</section></article>'
    )
    context = {
        "proposed_html": html,
        "reference_entries": [],
        "machine_reference_entries": [],
        "reference_inventory": {"reference_section_count": 0},
        "section_contracts": [{"section_id": "s1", "claim_ids": ["c1"]}],
        "argument_nodes": [
            {
                "id": "e-theory",
                "node_type": "evidence",
                "claim_id": "c1",
                "source_span_id": "annotation-citation:azuma",
                "provenance": {
                    "source": "memo",
                    "source_ids": ["annotation-citation:azuma"],
                    "verification_status": "unverified",
                },
            }
        ],
        "evidence_context": [
            {
                "id": "annotation-citation:azuma",
                "allowed_use": "attributed_scholarship_citation",
                "bibliographic_verification_status": "render_ready",
                "authors": ["東浩紀"],
                "title": "動物化するポストモダン",
            }
        ],
        "citation_event_drafts": [
            {"evidence_id": "annotation-citation:azuma"}
        ],
    }

    result = case.worker._manuscript_reference_quality_gate(
        claimed, context
    ).context["reference_quality_gate"]

    assert result["expected_author_citation_count"] == 1
    assert result["rendered_author_citation_count"] == 1
    assert result["missing_author_citation_ids"] == []
    assert not any(
        item.get("code") == "reference.required_author_citation_missing"
        for item in result["issues"]
    )


def test_v31_reference_quality_does_not_force_unrealized_body_theory(case) -> None:
    claimed = replace(authoring_claim(case), workflow_version="31")
    html = (
        '<article id="D" lang="ja"><section id="S"><h2 id="H">本文</h2>'
        '<p id="P">作品の配置を比較する。</p></section></article>'
    )
    context = {
        "proposed_html": html,
        "research_problem": {"prior_positions": []},
        "final_bibliography_entries": [],
        "reference_entries": [
            {
                "reference_id": "reference:azuma",
                "title": "動物化するポストモダン",
                "author": "東浩紀",
            }
        ],
        "machine_reference_entries": [],
        "reference_inventory": {"reference_section_count": 1},
        "reference_render_report": {
            "rule_set_id": "house-style",
            "entry_count": 0,
            "source_reference_count": 1,
            "uncited_reference_review_count": 1,
            "machine_section_rendered": False,
        },
        "evidence_footnote_render_report": {
            "policy": "evidence-footnotes-v1",
            "cited_reference_count": 0,
        },
        "section_contracts": [
            {
                "section_id": "s1",
                "article_role": "body",
                "claim_ids": ["c1"],
            }
        ],
        "argument_nodes": [
            {
                "id": "e-theory",
                "node_type": "evidence",
                "claim_id": "c1",
                "source_span_id": "annotation-citation:azuma",
                "provenance": {
                    "source": "memo",
                    "source_ids": ["annotation-citation:azuma"],
                    "verification_status": "unverified",
                },
            }
        ],
        "evidence_context": [
            {
                "id": "annotation-citation:azuma",
                "allowed_use": "attributed_scholarship_citation",
                "bibliographic_verification_status": "render_ready",
                "authors": ["東浩紀"],
                "title": "動物化するポストモダン",
            }
        ],
        "authoring_paragraph_citation_lineage": [],
        "citation_event_drafts": [],
    }

    result = case.worker._manuscript_reference_quality_gate(
        claimed, context
    ).context["reference_quality_gate"]

    assert result["expected_author_citation_count"] == 0
    assert result["missing_author_citation_ids"] == []
    assert not any(
        item.get("code")
        in {
            "reference.required_author_citation_missing",
            "reference.section_without_entries",
        }
        for item in result["issues"]
    )


def test_v31_reference_quality_blocks_missing_required_body_scholarship(case) -> None:
    claimed = replace(authoring_claim(case), workflow_version="31")
    html = (
        '<article id="D" lang="ja"><section id="S"><h2 id="H">本文</h2>'
        '<p id="P">局所資料から論じる。</p></section></article>'
    )
    context = {
        "proposed_html": html,
        "research_problem": {"research_question": "なぜか", "prior_positions": []},
        "final_bibliography_entries": [],
        "reference_entries": [
            {
                "reference_id": "reference:jameson",
                "title": "The Political Unconscious",
                "author": "Fredric Jameson",
            },
            {
                "reference_id": "reference:williams",
                "title": "Marxism and Literature",
                "author": "Raymond Williams",
            },
        ],
        "machine_reference_entries": [],
        "reference_inventory": {"reference_section_count": 2},
        "reference_render_report": {
            "rule_set_id": "house-style",
            "entry_count": 0,
            "source_reference_count": 2,
            "uncited_reference_review_count": 2,
            "machine_section_rendered": False,
        },
        "evidence_footnote_render_report": {
            "policy": "evidence-footnotes-v1",
            "cited_reference_count": 0,
        },
        "section_contracts": [{
            "section_id": "s1", "article_role": "body", "claim_ids": ["c1"],
        }],
        "authoring_atomic_evidence_contracts": [{
            "realization": "attribute_theory_with_citation",
            "claim_ids": ["c1"],
            "citation_evidence_ids": ["annotation-citation:williams"],
        }],
        "argument_nodes": [{
            "id": "e-theory", "node_type": "evidence", "claim_id": "c1",
            "source_span_id": "annotation-citation:jameson",
            "provenance": {
                "source": "memo",
                "source_ids": [
                    "annotation-citation:jameson",
                    "annotation-citation:williams",
                ],
                "verification_status": "unverified",
            },
        }],
        "evidence_context": [
            {
                "id": "annotation-citation:jameson",
                "reference_id": "reference:jameson",
                "allowed_use": "attributed_scholarship_citation",
                "bibliographic_verification_status": "render_ready",
                "authors": ["Fredric Jameson"],
                "title": "The Political Unconscious",
            },
            {
                "id": "annotation-citation:williams",
                "reference_id": "reference:williams",
                "allowed_use": "attributed_scholarship_citation",
                "bibliographic_verification_status": "render_ready",
                "authors": ["Raymond Williams"],
                "title": "Marxism and Literature",
            },
        ],
        "authoring_paragraph_citation_lineage": [],
        "citation_event_drafts": [],
    }

    result = case.worker._manuscript_reference_quality_gate(
        claimed, context
    ).context["reference_quality_gate"]

    assert result["expected_author_citation_count"] == 2
    assert result["rendered_author_citation_count"] == 0
    assert result["missing_author_citation_ids"] == [
        "annotation-citation:jameson",
        "annotation-citation:williams",
    ]
    assert any(
        item.get("code") == "reference.required_author_citation_missing"
        for item in result["issues"]
    )
    event_only = case.worker._manuscript_reference_quality_gate(
        claimed,
        {
            **context,
            "citation_event_drafts": [{
                "evidence_ids": ["annotation-citation:jameson"],
            }],
        },
    ).context["reference_quality_gate"]
    assert event_only["missing_author_citation_ids"] == [
        "annotation-citation:williams"
    ]
    assert event_only["rendered_citation_marker_count"] == 0
    assert any(
        item.get("code") == "reference.required_citation_render_missing"
        for item in event_only["issues"]
    )


def test_footnoted_coherence_reuses_pre_polish_when_proofreading_changed_nothing(
    case,
) -> None:
    claimed = authoring_claim(case)
    html = (
        '<article id="D" lang="ja"><section id="S"><h2 id="H">本文</h2>'
        '<p id="P">本文。</p></section></article>'
    )
    digest = submission_validation.digest(html)
    context = {
        "proposed_html": html,
        "economy_priority": True,
        "manuscript_coherence_checks": [
            {"stage": "pre_polish", "passed": False, "issues": []}
        ],
        "v26_coherence_report": {
            "stage": "pre_polish",
            "passed": False,
            "issues": [],
        },
        "proofreading_application": {
            "applied_count": 0,
            "source_html_sha256": digest,
            "target_html_sha256": digest,
        },
        "evidence_footnote_render_report": {"source_html_sha256": digest},
    }

    result = case.worker._manuscript_validate_footnoted_coherence_v29(
        claimed, context
    ).context

    assert result["v26_coherence_report"]["stage"] == "pre_polish"
    assert result["footnoted_coherence_report"]["stage"] == "post_polish"
    assert result["footnoted_coherence_report"]["economy_reused_semantic_stage"] == (
        "pre_polish"
    )
    assert result["authoring_economy_footnoted_coherence_reused"] is True
    assert result["footnoted_block_verification_records"]
    assert all(
        item["html_sha256"] == digest
        for item in result["footnoted_block_verification_records"]
    )

    merged = case.worker._merge_join_contexts(
        dict(context), result, path="validate_footnoted_coherence"
    )
    assert merged["footnoted_coherence_report"]["stage"] == "post_polish"


def test_submission_join_preserves_non_ready_economy_result_for_human_review(
    case,
) -> None:
    result = case.worker._authoring_join_submission_validations(
        authoring_claim(case),
        {
            "proposed_html": '<article id="D" lang="ja"></article>',
            "economy_priority": True,
            "reference_quality_gate": {"passed": True},
            "rendered_structure_v26": {"valid": True},
            "v26_coherence_report": {"passed": True, "stage": "pre_polish"},
            "footnoted_coherence_report": {
                "passed": False,
                "stage": "post_polish",
            },
            "v26_block_verification_records": [
                {"block_id": "old", "html_sha256": "old", "verified": True}
            ],
            "footnoted_block_verification_records": [
                {"block_id": "new", "html_sha256": "new", "verified": True}
            ],
        },
    )

    assert result.outcome == "conditional_success"
    assert result.context["submission_validation_gate"]["passed"] is False
    assert result.context["submission_validation_gate"][
        "continued_for_human_review"
    ] is True
    assert result.context["v26_coherence_report"]["stage"] == "post_polish"
    assert result.context["v26_block_verification_records"][0]["block_id"] == "new"
    assert result.context["required_user_information"][-1]["affected_scope"] == (
        "footnoted_coherence"
    )


def test_submission_join_routes_conditional_coherence_to_human_review(case) -> None:
    result = case.worker._authoring_join_submission_validations(
        authoring_claim(case),
        {
            "proposed_html": '<article id="D" lang="ja"></article>',
            "reference_quality_gate": {"passed": True},
            "rendered_structure_v26": {"valid": True},
            "footnoted_coherence_report": {
                "passed": False,
                "stage": "post_polish",
            },
            "completion_status": "conditional_success",
            "conditional_success": {
                "reason": "response_contract_incomplete",
                "disposition": "conditional_proposal",
            },
        },
    )

    assert result.outcome == "conditional_success"
    assert result.context["submission_validation_gate"]["passed"] is False
    assert result.context["submission_validation_gate"][
        "failed_validations"
    ] == []
    assert result.context["submission_validation_gate"][
        "conditional_validations"
    ] == ["footnoted_coherence"]
    assert result.context["required_user_information"][-1][
        "affected_scope"
    ] == "footnoted_coherence"


def test_block_evidence_projection_falls_back_without_lineage(case) -> None:
    evidence = [
        {"id": "E1", "finding_key": "c1"},
        {"id": "E2", "finding_key": "c2"},
    ]
    assert case.worker._manuscript_evidence_for_blocks(
        {"evidence_context": evidence}, {"P1"}
    ) == evidence

    projected = case.worker._manuscript_evidence_for_blocks(
        {
            "evidence_context": evidence,
            "realized_paragraphs": [
                {
                    "output_block_id": "P1",
                    "claim_ids": ["c1"],
                    "evidence_ids": ["E1"],
                }
            ],
        },
        {"P1"},
    )
    assert projected == [evidence[0]]


def test_discourse_context_keeps_global_first_location_for_local_term(case) -> None:
    context = {
        "term_introduction_plan": [
            {"term": "局所語", "first_section_id": "s1"},
            {"term": "無関係語", "first_section_id": "s2"},
        ],
        "realized_paragraphs": [
            {"output_block_id": "P1", "authoring_section_id": "s1"}
        ],
        "evidence_context": [],
    }
    projected = case.worker._manuscript_language_context_for_blocks(
        context,
        [{"id": "P1", "text": "ここでは局所語を説明する。"}],
        [],
        {"局所語": "P0", "無関係語": "P9"},
    )

    assert projected["first_mentions"] == {"局所語": "P0"}
    assert projected["plan"] == [
        {"term": "局所語", "first_section_id": "s1"}
    ]
