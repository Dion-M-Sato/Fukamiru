from __future__ import annotations

import copy
import sqlite3
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_japanese_polish import CapturingCandidates, target_fixture
from test_manuscript_references import HTML

from fukamiru.revisions.japanese_analysis import cached_analysis, parse_japanese
from fukamiru.revisions.japanese_polish import CandidateHardVeto, JapaneseCandidateGenerator
from fukamiru.revisions.manuscript_language import discourse_instructions, group_findings
from fukamiru.revisions.reference_search import (
    discover_reference,
    publisher_page,
    query_variants,
)
from fukamiru.revisions.references import JapaneseReferenceService
from fukamiru.worker.runner import WorkerRunner
from fukamiru.workflow.templates import (
    manuscript_rewrite_workflow,
    manuscript_rewrite_workflow_v21,
    manuscript_rewrite_workflow_v22,
)


def test_v21_new_nodes_are_in_the_actual_graph():
    flow = manuscript_rewrite_workflow_v21()
    assert flow.version == "21"
    edges = {(e.source, e.target) for e in flow.edges}
    assert ("plan_term_introductions", "plan_semantic_paragraphs") in edges
    assert ("analyze_japanese_structure", "detect_japanese_polish_targets") in edges
    assert ("validate_discourse_introductions", "persist_rewrite_proposal") in edges
    assert all(e.source != e.target for e in flow.edges)


def test_v22_is_version_isolated_from_v21_with_the_same_graph():
    v21 = manuscript_rewrite_workflow_v21()
    v22 = manuscript_rewrite_workflow_v22()
    assert manuscript_rewrite_workflow().version == "29"
    assert v22.version == "22"
    assert [node.model_dump() for node in v22.nodes] == [
        node.model_dump() for node in v21.nodes
    ]
    assert [edge.model_dump() for edge in v22.edges] == [
        edge.model_dump() for edge in v21.edges
    ]


def test_real_mecab_dependency_and_negative_scope():
    result = parse_japanese("支配が主体の回復に寄与しない。")
    assert result["tokens"] and result["bunsetsu"] and result["dependencies"]
    assert result["negation_scopes"]
    assert any(a["role"] == "nsubj" for a in result["arguments"])


def test_ambiguity_failure_is_cached_not_retried_and_source_invalidates(monkeypatch):
    import fukamiru.revisions.japanese_analysis as module

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE manuscript_language_analyses(signature PRIMARY KEY,payload_json)")
    monkeypatch.setattr(
        module,
        "parse_japanese",
        lambda text: {"status": "unresolved", "ambiguities": ["implicit_subject"], "tokens": []},
    )
    calls = []

    def resolver(**kwargs):
        calls.append(kwargs["text"])
        raise RuntimeError("invalid response")

    first = cached_analysis(conn, "本文", "前後", resolver)
    assert cached_analysis(conn, "本文", "前後", resolver) == first
    assert len(calls) == 1 and first["decision"] == "semantic_analysis_unresolved"
    cached_analysis(conn, "変更本文", "前後", resolver)
    assert len(calls) == 2
    monkeypatch.setattr(module, "analyzer_versions", lambda: {"dictionary": "changed"})
    cached_analysis(conn, "本文", "前後", resolver)
    assert len(calls) == 3


def test_partial_output_is_usable():
    target = target_fixture()
    payload = {
        "candidates": [{"text": "促す", "relation_type": "contribution", "rationale": "寄与"}]
    }
    assert JapaneseCandidateGenerator.validate_payload(payload, target, 5)
    assert not JapaneseCandidateGenerator.validate_payload({"candidates": []}, target, 5)


def test_removed_vetoes_and_orthographic_alias():
    target = target_fixture(
        source_text="位置づける",
        end_offset=5,
        relation_type="surface",
        allowed_relation_types=("surface", "preserve"),
    )
    candidate = JapaneseCandidateGenerator(CapturingCandidates()).generate(target).candidates[0]
    candidate = replace(candidate, text="位置付ける", source_kind="llm_jp", relation_type="surface")
    issues = CandidateHardVeto.validate(target, candidate, block_text=target.source_text)
    assert "veto.new_concept" not in issues
    assert not set(issues) & {
        "veto.sentence_scope_changed",
        "veto.anaphora_changed",
        "veto.tense_changed",
        "veto.relation_not_lexicalized",
    }


def test_protected_term_repetitions_can_reduce_but_not_disappear():
    target = target_fixture(source_text="主体と主体", end_offset=5, terminology=("主体",))
    original = JapaneseCandidateGenerator(CapturingCandidates()).generate(target).candidates[0]
    candidate = replace(original, text="主体", source_kind="llm_jp", relation_type="contribution")
    assert "veto.protected_term_changed" not in CandidateHardVeto.validate(
        target, candidate, block_text=target.source_text
    )
    assert "veto.protected_term_changed" in CandidateHardVeto.validate(
        target, replace(candidate, text="対象"), block_text=target.source_text
    )


def test_bibliography_format_keeps_title_punctuation_and_removes_country():
    source = (
        '<article id="a" lang="ja"><section id="s"><h2 id="h">参考文献</h2>'
        '<p id="r">ヘイドン・ホワイト『題名。JP、内部』、JP、作品社、2017。</p></section></article>'
    )
    entries = JapaneseReferenceService.extract(source)["entries"]
    assert len(entries) == 1
    assert entries[0]["title"] == "題名。JP、内部"
    entries[0]["publisher"] = "作品社"
    entries[0]["publication_place"] = "JP"
    entries[0]["resolution_status"] = "ambiguous"
    rendered, _ = JapaneseReferenceService.render(source, entries)
    assert "『題名。JP、内部』、作品社、2017。" in rendered
    assert JapaneseReferenceService.render(rendered, entries)[0] == rendered
    assert not JapaneseReferenceService.format_issues(rendered, entries)
    assert JapaneseReferenceService.format_issues(rendered.replace("2017。", "2017."), entries)


def test_references_are_individual_items_without_losing_source_ids():
    entries = JapaneseReferenceService.extract(HTML)["entries"]
    rendered, _ = JapaneseReferenceService.render(HTML, entries)
    assert ":ref:" in rendered
    assert not JapaneseReferenceService.format_issues(rendered, entries)
    assert JapaneseReferenceService.render(rendered, entries)[0] == rendered


def test_user_choice_never_mixes_editions():
    entry = JapaneseReferenceService.extract(HTML)["entries"][0]
    entry.update(
        year="1990",
        publisher="旧社",
        isbn="OLD",
        candidate_matches=[
            {
                "candidate_id": "C",
                "publisher": "新社",
                "year": "2000",
                "volume": "下巻",
                "provider": "test",
                "url": "https://example.org/book",
            }
        ],
    )
    result = JapaneseReferenceService.select_candidate(entry, "C")
    assert result["publisher"] == "新社" and result["isbn"] is None
    assert result["edition_status"] == "user_selected"
    with pytest.raises(ValueError):
        JapaneseReferenceService.select_candidate(entry, "unknown")


def test_reference_providers_are_parallel_and_independent(monkeypatch):
    entry = JapaneseReferenceService.extract(HTML)["entries"][0]
    barrier = threading.Barrier(4)
    calls = []

    def search(index, query, limit):
        calls.append(index)
        barrier.wait(timeout=3)
        raise RuntimeError("provider unavailable")

    providers = tuple(
        SimpleNamespace(name=str(i), search=lambda q, limit, i=i: search(i, q, limit))
        for i in range(4)
    )
    result = discover_reference(entry, providers)
    assert sorted(calls) == [0, 1, 2, 3]
    assert len(result["search_attempts"]) == 4
    assert result["resolution_status"] == "not_found"
    assert publisher_page("http://127.0.0.1/")["status"] == "unsafe_url"


def test_search_hypotheses_include_alternate_author_and_original_title():
    variants = query_variants(
        {"title": "散種", "author": "デリダ"},
        {"titles": ["La dissémination"], "authors": ["Jacques Derrida"]},
    )
    assert any("La dissémination" in v and "Jacques Derrida" in v for v in variants)


def test_grouping_tracks_merges_without_erasing_history():
    finding = {
        "code": "argument_gap",
        "message": "論拠不足",
        "details": {"claim_id": "C", "block_ids": ["old"]},
    }
    groups = group_findings(
        [finding, copy.deepcopy(finding)],
        "current",
        [{"output_block_id": "new", "source_block_ids": ["old"]}],
    )
    assert len(groups) == 1 and groups[0]["block_ids"] == ["new"]
    assert len(groups[0]["history"]) == 2


def test_original_preserved_known_issue_is_not_success_and_count_not_a_blocker():
    context = {
        "proposed_html": HTML,
        "manuscript_candidate_selection": {
            "selection_modes": {"mandatory_revision_unmet": 1},
            "generation_incomplete_target_count": 21,
        },
    }
    gate = WorkerRunner._manuscript_completion_gate(context)
    codes = {b["code"] for b in gate["blocking_reasons"]}
    assert "polish.mandatory_revision_unmet" in codes
    assert "polish.candidate_generation_incomplete" not in codes
    assert "polish.human_candidate_training_required" not in codes
    assert "DISCOURSE PLAN" in discourse_instructions(
        {"term_introduction_plan": [{"term": "M男性"}]}
    )


def test_claims_inside_aggregate_findings_are_grouped_not_lost():
    issue = {"code": "evidence_gap", "claim_id": "c1", "block_id": "p", "message": "不足"}
    findings = [{"code": "evidence", "details": [issue, dict(issue)]}]
    grouped = group_findings(findings, '<article id="a"><p id="p">本文</p></article>', [])
    assert len(grouped) == 1 and len(grouped[0]["history"]) == 2
    assert grouped[0]["location_status"] == "mapped"


def test_unsubstantiated_language_verdict_is_not_resolved():
    from fukamiru.revisions.manuscript_language import validate_language_records

    result = validate_language_records(
        [
            {
                "block_id": "p",
                "kind": "citation_function",
                "status": "resolved",
                "explanation": "確認",
            },
            {
                "block_id": "alien",
                "kind": "block_review",
                "status": "resolved",
                "explanation": "確認",
            },
        ],
        {"p"},
    )
    assert len(result) == 1
    assert result[0]["status"] == "semantic_analysis_unresolved"
    assert "application" in result[0]["missing_analysis_fields"]


def test_only_the_uncertain_sentence_is_held_for_review():
    from fukamiru.revisions.japanese_analysis import target_analysis_status

    analysis = {
        "status": "unresolved",
        "tokens": [{"start": 0, "end": 2}, {"start": 3, "end": 5}],
        "ambiguity_spans": [{"start": 0, "end": 3}],
    }
    assert target_analysis_status(analysis, 0, 2) == "unresolved"
    assert target_analysis_status(analysis, 3, 5) == "analyzed"
    assert target_analysis_status(analysis, 4, 5) == "unresolved"


def test_ambiguous_edition_cannot_be_completed_by_inference():
    entry = JapaneseReferenceService.extract(HTML)["entries"][0]
    entry.update(resolution_status="ambiguous", publisher=None)
    result = JapaneseReferenceService.apply_inferences(
        [entry],
        {
            "inferences": [
                {
                    "reference_id": entry["reference_id"],
                    "confidence": 1,
                    "fields": {"publisher": "想像社"},
                }
            ]
        },
    )
    assert result[0]["publisher"] is None


def test_chapter_order_is_distinct_from_journal_article():
    entry = {
        "parse_status": "parsed",
        "author": "著者",
        "title": "章題",
        "bibliography_kind": "chapter",
        "editors": ["編者"],
        "container_title": "書名",
        "publisher": "出版社",
        "year": "2020",
        "pages": "1-5",
    }
    assert (
        JapaneseReferenceService.render_entry(entry)
        == "著者「章題」、編者編、『書名』、出版社、2020、p. 1-5。"
    )


@pytest.fixture
def rewrite_case():
    from test_manuscript_rewrite import ManuscriptRewriteTests

    case = ManuscriptRewriteTests()
    case.setUp()
    try:
        yield case
    finally:
        case.tearDown()


def _bibliography_proposal(case):
    entries = JapaneseReferenceService.extract(HTML)["entries"]
    rendered, _ = JapaneseReferenceService.render(HTML, entries)
    entries[0]["candidate_matches"] = [
        {"candidate_id": "c", "publisher": "選択出版社", "year": "2026"}
    ]
    return case.service.persist_proposal(
        run_id=case._run_row(),
        document_id=case.document_id,
        project_id=case.project["id"],
        base_revision_id=case.base.revision_id,
        outline_node_id=None,
        instructions="書誌確認",
        use_rag=False,
        context={"reference_entries": entries},
        source_html=rendered,
        proposed_html=rendered,
        edit_summary={},
        quality_status="needs_human_review",
    )


def test_reference_selection_requires_current_proposal_and_valid_candidate(rewrite_case):
    from fukamiru.errors import FukamiruError

    case = rewrite_case
    proposal = _bibliography_proposal(case)
    reference_id = proposal["context"]["reference_entries"][0]["reference_id"]
    with pytest.raises(FukamiruError, match="does not belong"):
        case.service.select_reference(
            proposal["id"], reference_id, "alien", proposal["proposed_sha256"]
        )
    selected = case.service.select_reference(
        proposal["id"], reference_id, "c", proposal["proposed_sha256"]
    )
    assert "選択出版社、2026。" in selected["proposed_html"]
    assert selected["context"]["reference_selection_requires_revalidation"]
    assert selected["lifecycle_status"] == "pending"
    with pytest.raises(FukamiruError, match="reload"):
        case.service.select_reference(
            proposal["id"], reference_id, "c", proposal["proposed_sha256"]
        )


def test_format_regression_is_blocked_at_refresh_and_approval(rewrite_case):
    from fukamiru.errors import FukamiruError

    case = rewrite_case
    proposal = _bibliography_proposal(case)
    broken = proposal["proposed_html"].replace("。", ".")
    with pytest.raises(FukamiruError, match="Mandatory bibliography"):
        case.service.refresh_pending_proposal(
            proposal["id"],
            context=proposal["context"],
            source_html=broken,
            proposed_html=broken,
            edit_summary={},
            quality_status="ready",
        )
    case.service.update_proposed(proposal["id"], canonical_html=broken)
    with pytest.raises(FukamiruError, match="Mandatory bibliography"):
        case.service.confirm(proposal["id"])


def test_fresh_bibliography_parse_does_not_erase_translation_volume_or_unknown_fields():
    source = (
        '<article id="a" lang="ja"><section id="s"><h2 id="h">参考文献</h2>'
        '<p id="r">著者『題名、内部』、訳者訳、上巻、改訂版、東京、出版社、2017。</p>'
        "</section></article>"
    )
    entries = JapaneseReferenceService.extract(source)["entries"]
    assert entries[0]["translators"] == ["訳者"] and entries[0]["volume"] == "上巻"
    rendered, _ = JapaneseReferenceService.render(source, entries)
    again, _ = JapaneseReferenceService.render(
        rendered, JapaneseReferenceService.extract(rendered)["entries"]
    )
    assert again == rendered
    assert "東京、出版社、2017。" in rendered


def test_dependency_roles_and_negation_not_occurrence_counts():
    from fukamiru.revisions.japanese_analysis import compare_grammar

    source = "男性が女性を支える。"
    assert compare_grammar(source, "女性が男性を支える。", 0, len(source))
    assert compare_grammar(source, "男性が女性を支えない。", 0, len(source))


def test_partial_candidates_apply_with_zero_training_history(rewrite_case, monkeypatch):
    from fukamiru.revisions.japanese_polish import CandidateCorpusScorer, WordingCandidate
    from fukamiru.revisions.reasoning import ManuscriptReasoningRepository
    from fukamiru.storage.database import SQLiteDatabase

    case = rewrite_case
    run_id = case._run_row()
    target = target_fixture(
        source_text="位置づける",
        end_offset=5,
        terminology=(),
        relation_type="surface",
        allowed_relation_types=("surface", "preserve"),
    )
    repo = ManuscriptReasoningRepository(case.connection)
    repo.replace_polish_targets(
        (target,),
        run_id=run_id,
        node_run_id=None,
        document_id=case.document_id,
        project_id=case.project["id"],
    )
    original = WordingCandidate(
        "orig", target.id, 0, target.source_text, "preserve", "原文", 1, "original"
    )
    candidate = replace(
        original,
        id="alt",
        ordinal=1,
        text="位置付ける",
        source_kind="llm_jp",
        relation_type="surface",
    )
    repo.save_polish_candidates(
        (original, candidate),
        run_id=run_id,
        target_id=target.id,
        model_identity="test",
        expected_candidate_count=6,
        issues=(),
    )
    monkeypatch.setattr(
        CandidateCorpusScorer,
        "score",
        staticmethod(lambda t, c, **kwargs: replace(c, total_score=10.0 if c.id == "alt" else 0.0)),
    )
    worker = object.__new__(WorkerRunner)
    database = SQLiteDatabase(case.root / "state.db", case.root / "unused-migrations")
    worker.databases = SimpleNamespace(state=database, knowledge=database)
    worker.settings = SimpleNamespace(
        translation=SimpleNamespace(
            manuscript_candidate_beam_width=8,
            manuscript_candidate_human_training_target=100,
            manuscript_candidate_auto_apply_margin=0.12,
        )
    )
    worker._record_manuscript_progress = lambda *a, **kw: None
    result = worker._manuscript_select_polish_candidates(
        SimpleNamespace(run_id=run_id, workflow_version="27"),
        {
            "proposed_html": (
                '<article id="a" lang="ja"><section id="s"><h2 id="h">章</h2>'
                '<p id="P">位置づける</p></section></article>'
            ),
            "use_llm_jp": True,
            "project_id": case.project["id"],
            "document_id": case.document_id,
        },
    ).context
    assert "位置付ける" in result["proposed_html"]
    assert result["manuscript_candidate_selection"]["selection_modes"] == {"independent_auto": 1}
    assert result["manuscript_candidate_selection"]["generation_incomplete_target_count"] == 1
