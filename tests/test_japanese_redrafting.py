from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

from fukamiru.authoring.codex import CodexAuthoringAdapter
from fukamiru.errors import ExternalServiceUnavailable, FukamiruError
from fukamiru.revisions import submission_validation
from fukamiru.revisions.footnote_citations import (
    EvidenceFootnotePlanner,
    EvidenceFootnoteRenderer,
)
from fukamiru.revisions.japanese_redrafting import (
    QUALITY_DIMENSIONS,
    apply_revisions,
    editable_sections,
)
from fukamiru.worker.runner import ClaimedNode, WorkerRunner
from fukamiru.workflow.templates import authoring_workflow, authoring_workflow_v30

HTML = (
    '<article id="a" lang="ja"><section id="s">'
    '<h2 id="h">見出し</h2>'
    '<p id="p1">山田は資料を検討する。数字は2026年である。</p>'
    '<p id="p2">「引用」は残す。</p>'
    '</section><section id="notes" data-citation-section="evidence-footnotes-v1">'
    '<h2 id="nh">注</h2><p id="n1">脚注本文</p></section></article>'
)
LINEAGE = [{"output_block_id": "p1", "evidence_ids": ["e1"]}]
EVIDENCE = [{"id": "e1", "authors": ["山田"], "title": "資料"}]


def apply(*revisions: dict[str, str], html: str = HTML):
    return apply_revisions(
        html, list(revisions), citation_lineage=LINEAGE,
        evidence_context=EVIDENCE, locked_texts=[],
    )


def test_section_review_resumes_after_external_failure(tmp_path) -> None:
    html = (
        '<article id="a" lang="ja"><section id="s1"><h2 id="h1">第一節</h2>'
        '<p id="p1">山田は資料を検討する。</p></section>'
        '<section id="s2"><h2 id="h2">第二節</h2>'
        '<p id="p2">田中は作品を読む。</p></section>'
        '<section id="s3"><h2 id="h3">第三節</h2>'
        '<p id="p3">佐藤は絵を描く。</p></section></article>'
    )
    db_path = tmp_path / "checkpoints.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "create table manuscript_unit_checkpoints ("
            "run_id text, phase text, signature text, unit_id text, payload_json text, "
            "primary key (run_id, phase, signature, unit_id))"
        )

    class State:
        @contextmanager
        def connect(self):
            connection = sqlite3.connect(db_path)
            try:
                yield connection
                connection.commit()
            finally:
                connection.close()

    class Runner:
        databases = SimpleNamespace(state=State())
        failed_once = False
        calls = []

        @staticmethod
        def _raise_if_run_cancelled(*_args):
            return None

        @staticmethod
        def _authoring_japanese_checked_bridge(_context, _html):
            return {"authoring_paragraph_citation_lineage": []}

        def _authoring_invoke(self, _claimed, *, stage, payload, **_kwargs):
            self.calls.append((stage, payload["section_id"]))
            if (
                stage == "japanese_argument_preservation_v31"
                and payload["section_id"] == "s3"
                and not self.failed_once
            ):
                self.failed_once = True
                raise ExternalServiceUnavailable("temporary capacity error")
            if stage == "japanese_argument_preservation_v31":
                return {"checks": [
                    {"block_id": item["block_id"], "preserved": True, "reason": "保持"}
                    for item in payload["original_paragraphs"]
                ]}
            assert stage == "japanese_section_review_v31"
            return {
                "ratings": [
                    {"dimension": dimension, "acceptable": True, "reason": "自然"}
                    for dimension in QUALITY_DIMENSIONS
                ],
                "flow_needs_revision": False,
                "flow_findings": ["段落の流れは自然である。"],
            }

    sections = editable_sections(html)
    work_sections = []
    for section, replacement in zip(
        sections,
        ("山田が資料を検討した。", "田中が作品を読んだ。", "佐藤が絵を描いた。"),
        strict=True,
    ):
        revision = {"block_id": section["paragraphs"][0]["block_id"],
                    "text": replacement}
        candidate_html, decisions = apply_revisions(
            html, [revision], citation_lineage=[], evidence_context=[], locked_texts=[]
        )
        assert decisions[0]["status"] == "accepted"
        candidate_section = next(
            item for item in editable_sections(candidate_html)
            if item["section_id"] == section["section_id"]
        )
        work_sections.append({
            "section_id": section["section_id"],
            "base": {**section, "citation_lineage": []},
            "diagnoses": [], "attempts": [{"attempt": 1, "decisions": decisions}],
            "feedback": [], "accepted": False,
            "candidate": {"revisions": [revision],
                          "paragraphs": candidate_section["paragraphs"]},
        })
    context = {
        "proposed_html": html,
        "japanese_redrafting_work": {
            "source_sha256": submission_validation.digest(html), "sections": work_sections,
        },
        "authoring_paragraph_citation_lineage": [],
        "evidence_context": [],
    }
    runner = Runner()
    claim = SimpleNamespace(run_id="resume-test")
    with pytest.raises(ExternalServiceUnavailable):
        WorkerRunner._authoring_review_japanese_sections(runner, claim, context)
    with sqlite3.connect(db_path) as connection:
        saved = connection.execute(
            "select payload_json from manuscript_unit_checkpoints "
            "where run_id='resume-test' and phase='authoring-japanese-review-v31'"
        ).fetchone()
    assert saved is not None
    assert json.loads(saved[0])["next_index"] == 2
    result = WorkerRunner._authoring_review_japanese_sections(runner, claim, context)
    assert result.outcome == "success"
    assert "山田が資料を検討した" in result.context["proposed_html"]
    assert "田中が作品を読んだ" in result.context["proposed_html"]
    assert "佐藤が絵を描いた" in result.context["proposed_html"]
    assert runner.calls.count(("japanese_argument_preservation_v31", "s1")) == 1
    assert runner.calls.count(("japanese_argument_preservation_v31", "s2")) == 1


def test_redrafting_changes_sentence_structure_but_retains_citation_and_notes() -> None:
    sections = editable_sections(HTML)
    assert [item["section_id"] for item in sections] == ["s"]
    result, decisions = apply({
        "block_id": "p1",
        "text": "山田が資料を検討した。対象となる数字は2026年である。",
    })
    assert decisions == [{"block_id": "p1", "status": "accepted", "reason": ""}]
    assert "山田が資料を検討した" in result
    assert 'id="n1">脚注本文</p>' in result
    assert 'id="p2">「引用」は残す。</p>' in result


def test_redrafted_paragraph_still_produces_the_expected_footnote() -> None:
    candidate, decisions = apply({
        "block_id": "p1",
        "text": "山田が資料を検討した。対象となる数字は2026年である。",
    })
    assert decisions[0]["status"] == "accepted"
    reference = {
        "reference_id": "R-1", "title": "資料", "record_type": "book",
        "publication_language": "ja", "publisher": "出版社", "year": "2020",
        "evidence_ids": ["e1"],
        "bibliography_people": [{
            "role": "author", "name_profile": "japanese_native",
            "family": "山田", "given": "太郎",
        }],
    }
    plan = EvidenceFootnotePlanner.plan(
        candidate,
        applied_edits=[{
            "operation": "replace_text", "block_id": "p1", "evidence_ids": ["e1"],
        }],
        evidence_context=EVIDENCE, reference_entries=[reference],
        require_distinct_anchors=True,
    )
    rendered = EvidenceFootnoteRenderer.render(candidate, list(plan.citations))
    root = ET.fromstring(rendered.html)
    assert len([node for node in root.iter("sup")]) == 1
    assert len([node for node in root.iter("li") if node.get("data-note-number")]) == 1
    assert any(node.get("id") == "n1" and node.text == "脚注本文" for node in root.iter("p"))
    assert plan.citations[0].evidence_ids == ("e1",)


def test_attached_example_can_rebuild_subject_and_sentence_order() -> None:
    original = (
        "媒体レギュレーションは、編集、掲載配置、流通、検索、推薦、想定読者、"
        "制作工程を一括して働く単一原因ではなく、それぞれを別個に観察できる"
        "機構の束として操作化する。"
    )
    revised = (
        "本稿では、「媒体レギュレーション」という概念を、編集方針や作品の"
        "掲載順、流通経路、検索・推薦の仕組み、想定読者、制作工程に分けて"
        "検討する。これらがすべて同じ原因によって動いていると考えるわけではない。"
    )
    html = (
        '<article id="a" lang="ja"><section id="s"><h2 id="h">方法</h2>'
        f'<p id="p">{original}</p></section></article>'
    )
    candidate, decisions = apply_revisions(
        html, [{"block_id": "p", "text": revised}],
        citation_lineage=[], evidence_context=[], locked_texts=[],
    )
    assert decisions[0]["status"] == "accepted"
    assert revised in candidate


def test_existing_inline_note_marker_is_not_rewritten() -> None:
    html = (
        '<article id="a" lang="ja"><section id="s"><h2 id="h">本文</h2>'
        '<p id="p">本文<sup id="mark">1</sup>。</p></section></article>'
    )
    assert editable_sections(html) == []
    with pytest.raises(FukamiruError, match="unknown paragraph IDs"):
        apply_revisions(
            html, [{"block_id": "p", "text": "書き換えた本文。"}],
            citation_lineage=[], evidence_context=[], locked_texts=[],
        )


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("資料を検討する。数字は2026年である。", "citation_anchor_removed"),
        ("研究を進める。数字は2026年である。", "citation_anchor_removed"),
        ("山田は資料を検討する。数字は2027年である。", "protected_token_changed"),
        ("山田は資料を検討する。", "protected_token_changed"),
    ],
)
def test_redrafting_rejects_lost_source_or_protected_information(
    text: str, reason: str
) -> None:
    result, decisions = apply({"block_id": "p1", "text": text})
    assert decisions[0]["reason"] == reason
    assert "山田は資料を検討する。数字は2026年である。" in result


def test_redrafting_rejects_unknown_ids_and_locked_argument_text() -> None:
    with pytest.raises(FukamiruError, match="unknown paragraph IDs"):
        apply({"block_id": "n1", "text": "削除"})
    result, decisions = apply_revisions(
        HTML, [{"block_id": "p2", "text": "残す。"}],
        citation_lineage=LINEAGE, evidence_context=EVIDENCE,
        locked_texts=["「引用」は残す。"],
    )
    assert decisions[0]["reason"] == "protected_token_changed"
    assert "「引用」は残す。" in result


def test_research_question_can_be_reworded_but_a_source_title_stays_fixed() -> None:
    question = (
        "作品・ジャンルが社会的関係と創作技法を可視化し、配役し、"
        "読者に受容させる仕方をどのように変えてきたのか"
    )
    original = f"20世紀末以降、本稿が問うのは「{question}」である。『漫画の歴史』を参照する。"
    revised = (
        "20世紀末以降、漫画を取り巻く媒体の変化は、社会的な関係や人物の役割の描き方に"
        "どう関わってきたのか。また、制作技法や作品の読まれ方には、どのような変化が"
        "見られるのか。『漫画の歴史』を参照する。"
    )
    html = (
        '<article id="a" lang="ja"><section id="s"><h2 id="h">序論</h2>'
        f'<p id="p">{original}</p></section></article>'
    )
    candidate, decisions = apply_revisions(
        html, [{"block_id": "p", "text": revised}],
        citation_lineage=[], evidence_context=[], locked_texts=[question],
        rephrasable_propositions=[question],
    )
    assert decisions[0]["status"] == "accepted"
    assert revised in candidate
    _, rejected = apply_revisions(
        html, [{"block_id": "p", "text": revised.replace("『漫画の歴史』", "")}],
        citation_lineage=[], evidence_context=[], locked_texts=[question],
        rephrasable_propositions=[question],
    )
    assert rejected[0]["reason"] == "protected_token_changed"


def test_long_book_title_is_not_unlocked_as_a_named_proposition() -> None:
    title = "『近代漫画研究における読者の視線と欲望の社会的配置について』"
    html = (
        '<article id="a" lang="ja"><section id="s"><h2 id="h">序論</h2>'
        f'<p id="p">{title}を参照する。</p></section></article>'
    )
    _, decisions = apply_revisions(
        html, [{"block_id": "p", "text": "先行研究を参照する。"}],
        citation_lineage=[], evidence_context=[], locked_texts=[],
        rephrasable_propositions=[title],
    )
    assert decisions[0]["reason"] == "protected_token_changed"


def test_human_protected_paragraph_is_not_editable() -> None:
    sections = editable_sections(HTML, protected_block_ids={"p1"})
    assert [item["block_id"] for item in sections[0]["paragraphs"]] == ["p2"]
    with pytest.raises(FukamiruError, match="unknown paragraph IDs"):
        apply_revisions(
            HTML, [{"block_id": "p1", "text": "別の文章。"}],
            citation_lineage=LINEAGE, evidence_context=EVIDENCE,
            locked_texts=[], protected_block_ids={"p1"},
        )


def test_v31_redrafting_precedes_sentence_proofreading_and_v30_is_unchanged() -> None:
    v31 = authoring_workflow()
    v30 = authoring_workflow_v30()
    assert v31.next_nodes("validate_draft_coherence", "success") == [
        "diagnose_japanese_prose"
    ]
    assert v31.next_nodes("diagnose_japanese_prose", "success") == [
        "redraft_japanese_paragraphs"
    ]
    assert v31.next_nodes("redraft_japanese_paragraphs", "success") == [
        "review_japanese_sections"
    ]
    assert v31.next_nodes("review_japanese_sections", "failure") == [
        "redraft_japanese_paragraphs"
    ]
    assert v31.next_nodes("review_japanese_sections", "success") == [
        "extract_proofreading_sentences"
    ]
    nodes = {node.id: node for node in v31.nodes}
    for node_id in (
        "diagnose_japanese_prose",
        "redraft_japanese_paragraphs",
        "review_japanese_sections",
    ):
        assert nodes[node_id].handler == f"authoring.{node_id}"
        assert hasattr(WorkerRunner, f"_authoring_{node_id}")
    assert v30.next_nodes("validate_draft_coherence", "success") == [
        "extract_proofreading_sentences"
    ]


def test_model_results_require_complete_diagnosis_and_independent_ratings() -> None:
    payload = {"section_id": "s", "paragraphs": [{"block_id": "p1"}]}
    with pytest.raises(FukamiruError, match="omitted"):
        WorkerRunner._authoring_validate_checkpoint_result(
            "japanese_diagnosis_v31", payload, {"diagnoses": []}
        )
    ratings = [
        {"dimension": dimension, "acceptable": True, "reason": "確認済み"}
        for dimension in QUALITY_DIMENSIONS
    ]
    output = {
        "section_id": "s", "ratings": ratings,
        "flow_needs_revision": False,
        "flow_findings": [],
    }
    CodexAuthoringAdapter.validate_result("japanese_section_review_v31", output)
    WorkerRunner._authoring_validate_checkpoint_result(
        "japanese_section_review_v31", payload, output
    )
    with pytest.raises(FukamiruError, match="omitted a dimension"):
        WorkerRunner._authoring_validate_checkpoint_result(
            "japanese_section_review_v31", payload,
            {**output, "ratings": ratings[:-1]},
        )
    with pytest.raises(FukamiruError, match="omitted or duplicated"):
        WorkerRunner._authoring_validate_checkpoint_result(
            "japanese_argument_preservation_v31",
            {"section_id": "s", "original_paragraphs": [{"block_id": "p1"}]},
            {"section_id": "s", "checks": []},
        )
    argument_result = {
        "section_id": "s", "checks": [
            {"block_id": "p1", "preserved": True, "reason": "限定を確認"}
        ],
    }
    CodexAuthoringAdapter.validate_result(
        "japanese_argument_preservation_v31", argument_result
    )
    WorkerRunner._authoring_validate_checkpoint_result(
        "japanese_argument_preservation_v31",
        {"section_id": "s", "original_paragraphs": [{"block_id": "p1"}]},
        argument_result,
    )


@pytest.mark.parametrize(
    "failure", ["none", "source", "source_always", "lineage", "argument", "style"]
)
def test_redrafting_retries_failed_candidate_and_keeps_footnote_lineage(
    failure: str,
) -> None:
    class Runner:
        attempts = 0
        feedback_seen = []
        diagnoses = 0

        @staticmethod
        def _raise_if_run_cancelled(*_args):
            return None

        @staticmethod
        def _authoring_v29_plan_bridge(_context, _html, *, semantic_attribution):
            assert semantic_attribution is True
            return {
                "structure_plan_validation": {"valid": True},
                "paragraph_plan_validation": {"valid": True},
                "authoring_paragraph_citation_lineage": LINEAGE,
                "authoring_citation_lineage_reconciliation": {
                    "dropped_bindings": (
                        [{"block_id": "p1", "evidence_id": "e1"}]
                        if failure == "lineage" and Runner.attempts == 1 else []
                    )
                },
            }

        def _authoring_japanese_checked_bridge(self, context, candidate_html):
            return WorkerRunner._authoring_japanese_checked_bridge(
                self, context, candidate_html
            )

        @staticmethod
        def _authoring_invoke(_claimed, *, stage, payload, **_kwargs):
            if stage == "japanese_diagnosis_v31":
                Runner.diagnoses += 1
                return {"diagnoses": [
                    {"block_id": item["block_id"], "issues": [], "terms_to_keep": []}
                    for item in payload["paragraphs"]
                ]}
            if stage == "japanese_paragraph_redraft_v31":
                Runner.attempts += 1
                Runner.feedback_seen.append(payload["previous_feedback"])
                return {"revisions": [{
                    "block_id": "p1",
                    "text": (
                        "研究を進める。数字は2026年である。"
                        if failure in {"source", "source_always"}
                        and (Runner.attempts == 1 or failure == "source_always")
                        else "山田が資料を検討した。数字は2026年である。"
                    ),
                }]}
            if stage == "japanese_argument_preservation_v31":
                return {"section_id": payload["section_id"], "checks": [
                    {"block_id": item["block_id"],
                     "preserved": not (failure == "argument" and Runner.attempts == 1),
                     "reason": "原文の限定を復元する"}
                    for item in payload["original_paragraphs"]
                ]}
            assert stage == "japanese_section_review_v31"
            return {
                "section_id": payload["section_id"],
                "ratings": [
                    {"dimension": dimension,
                     "acceptable": not (
                         failure == "style" and Runner.attempts == 1
                         and dimension == "syntax"
                     ), "reason": "構文を確認"}
                    for dimension in QUALITY_DIMENSIONS
                ],
                "flow_needs_revision": False,
                "flow_findings": [],
            }

    def claimed(node_id: str, attempt: int = 1) -> ClaimedNode:
        return ClaimedNode(
            node_run_id=f"node-{node_id}-{attempt}", run_id="run", node_id=node_id,
            attempt=attempt, workflow_name="manuscript_generator",
            workflow_version="31", resource_lane="codex_remote",
            lease_owner="test", project_id=None, inputs={},
        )
    context = {
        "proposed_html": HTML,
        "authoring_paragraph_citation_lineage": LINEAGE,
        "evidence_context": EVIDENCE,
    }
    runner = Runner()
    diagnosed = WorkerRunner._authoring_diagnose_japanese_prose(
        runner, claimed("diagnose_japanese_prose"), context
    )
    assert diagnosed.context["proposed_html"] == HTML
    assert Runner.diagnoses == 1
    current = diagnosed.context
    outcomes = []
    for attempt in range(1, 4):
        redrafted = WorkerRunner._authoring_redraft_japanese_paragraphs(
            runner, claimed("redraft_japanese_paragraphs", attempt), current
        )
        assert redrafted.context["proposed_html"] == current["proposed_html"]
        result = WorkerRunner._authoring_review_japanese_sections(
            runner, claimed("review_japanese_sections", attempt), redrafted.context
        )
        outcomes.append(result.outcome)
        current = result.context
        if result.outcome == "success":
            break
    assert outcomes == (["success"] if failure in {"none", "style"} else
                        ["failure", "failure", "success"] if failure == "source_always"
                        else ["failure", "success"])
    if failure == "source_always":
        # The third review finishes with a human item and the original text.
        assert outcomes == ["failure", "failure", "success"]
    report = result.context["japanese_redrafting"]
    assert result.context["authoring_paragraph_citation_lineage"] == LINEAGE
    assert "脚注本文" in result.context["proposed_html"]
    if failure == "source_always":
        assert result.context["proposed_html"] == HTML
        assert report["sections"][0]["accepted"] is False
        assert Runner.attempts == 3
        assert result.context["human_review_items"][0]["category"] == (
            "japanese_redrafting_review"
        )
    else:
        assert "山田が資料を検討した" in result.context["proposed_html"]
        assert report["sections"][0]["accepted"] is True
        assert Runner.attempts == (1 if failure in {"none", "style"} else 2)
        if failure == "style":
            assert report["sections"][0]["attempts"][0]["disposition"] == "marginal"
            assert result.context["human_review_items"][0]["category"] == (
                "japanese_redrafting_review"
            )
        else:
            assert result.context["human_review_items"] == []
    if failure in {"source", "source_always"}:
        assert report["sections"][0]["attempts"][0]["decisions"][0]["reason"] == (
            "citation_anchor_removed"
        )
        assert "p1: citation_anchor_removed" in Runner.feedback_seen[1]
    if failure == "lineage":
        assert "citation lineage" in Runner.feedback_seen[1][0]


def test_review_adopts_marginal_style_and_records_human_review() -> None:
    html = (
        '<article id="a" lang="ja">'
        '<section id="s1"><h2 id="h1">一</h2><p id="p1">甲は考える。</p></section>'
        '<section id="s2"><h2 id="h2">二</h2><p id="p2">乙は考える。</p></section>'
        '</article>'
    )

    class Runner:
        calls = {"s1": 0, "s2": 0}

        @staticmethod
        def _raise_if_run_cancelled(*_args):
            return None

        @staticmethod
        def _authoring_japanese_checked_bridge(_context, _html):
            return {
                "structure_plan_validation": {"valid": True},
                "paragraph_plan_validation": {"valid": True},
                "authoring_paragraph_citation_lineage": [],
                "authoring_citation_lineage_reconciliation": {"dropped_bindings": []},
            }

        @staticmethod
        def _authoring_invoke(_claimed, *, stage, payload, **_kwargs):
            section_id = payload["section_id"]
            if stage == "japanese_diagnosis_v31":
                return {"diagnoses": [
                    {"block_id": item["block_id"], "issues": [], "terms_to_keep": []}
                    for item in payload["paragraphs"]
                ]}
            if stage == "japanese_paragraph_redraft_v31":
                Runner.calls[section_id] += 1
                return {"revisions": [{
                    "block_id": item["block_id"],
                    "text": item["text"].replace("考える", "検討する"),
                } for item in payload["paragraphs"]]}
            if stage == "japanese_argument_preservation_v31":
                return {"section_id": section_id, "checks": [
                    {"block_id": item["block_id"], "preserved": True, "reason": "保持"}
                    for item in payload["original_paragraphs"]
                ]}
            assert stage == "japanese_section_review_v31"
            return {
                "section_id": section_id,
                "ratings": [
                    {"dimension": dimension,
                     "acceptable": not (
                         section_id == "s1" and Runner.calls["s1"] == 1
                         and dimension == "syntax"
                     ), "reason": "語順を再確認"}
                    for dimension in QUALITY_DIMENSIONS
                ],
                "flow_needs_revision": False,
                "flow_findings": [],
            }

    def claimed(node_id: str, attempt: int) -> ClaimedNode:
        return ClaimedNode(
            node_run_id=f"{node_id}-{attempt}", run_id="run", node_id=node_id,
            attempt=attempt, workflow_name="manuscript_generator",
            workflow_version="31", resource_lane="codex_remote",
            lease_owner="test", project_id=None, inputs={},
        )

    runner = Runner()
    current = WorkerRunner._authoring_diagnose_japanese_prose(
        runner, claimed("diagnose_japanese_prose", 1), {"proposed_html": html}
    ).context
    drafted = WorkerRunner._authoring_redraft_japanese_paragraphs(
        runner, claimed("redraft_japanese_paragraphs", 1), current
    )
    reviewed = WorkerRunner._authoring_review_japanese_sections(
        runner, claimed("review_japanese_sections", 1), drafted.context
    )
    current = reviewed.context
    assert reviewed.outcome == "success"
    assert "乙は検討する" in current["proposed_html"]
    assert Runner.calls == {"s1": 1, "s2": 1}
    assert len(current["human_review_items"]) == 1
    assert current["human_review_items"][0]["section_id"] == "s1"
    assert "甲は検討する" in current["proposed_html"]
    assert "japanese_redrafting_work" not in current


def test_real_authoring_bridge_retains_source_binding_after_redrafting() -> None:
    original = "ランシエール『感性的なもののパルタージュ』を比較の枠として参照する。"
    revised = "比較の枠を考える際に、ランシエール『感性的なもののパルタージュ』を参照する。"
    context = {
        "plan_id": "plan", "language": "ja",
        "research_problem": {"research_question": "RQ"},
        "section_contracts": [{
            "section_id": "s1", "title": "適用範囲", "article_role": "body",
            "claim_ids": ["c1"], "evidence_ids": ["e1"],
            "development_expectations": {"required_argument_moves": []},
        }],
        "argument_nodes": [
            {"id": "e1", "node_type": "evidence", "claim_id": "c1"},
            {"id": "a1", "node_type": "evidence", "claim_id": "c1",
             "provenance": {"source_ids": ["source-1"]}},
        ],
        "evidence_context": [{
            "id": "source-1", "allowed_use": "attributed_scholarship_citation",
            "authors": ["ランシエール"], "title": "感性的なもののパルタージュ",
        }],
        "draft_sections": [{
            "section_id": "s1", "article_role": "body", "claim_ids": ["c1"],
            "paragraphs": [{"text": original, "evidence_ids": ["e1"],
                            "move_indices": []}],
            "text": original,
        }],
        "authoring_paragraph_citation_lineage": [{
            "output_block_id": "P-plan-s1-001",
            "argument_evidence_node_ids": ["a1"],
            "evidence_ids": ["source-1"],
        }],
    }
    html = WorkerRunner._authoring_render_sections(context, context["draft_sections"])
    candidate, decisions = apply_revisions(
        html, [{"block_id": "P-plan-s1-001", "text": revised}],
        citation_lineage=context["authoring_paragraph_citation_lineage"],
        evidence_context=context["evidence_context"], locked_texts=[],
    )
    assert decisions[0]["status"] == "accepted"
    bridge = WorkerRunner._authoring_v29_plan_bridge(
        context, candidate, semantic_attribution=True
    )
    assert bridge["structure_plan_validation"]["valid"]
    assert bridge["paragraph_plan_validation"]["valid"]
    assert bridge["authoring_paragraph_citation_lineage"][0]["evidence_ids"] == [
        "source-1"
    ]
    assert bridge["authoring_citation_lineage_reconciliation"][
        "dropped_bindings"
    ] == []
