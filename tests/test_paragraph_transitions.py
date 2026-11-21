from copy import deepcopy
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

from fukamiru.authoring import paragraph_transitions as transitions
from fukamiru.errors import NonRetryableFukamiruError
from fukamiru.revisions import prose_redraft as prose
from fukamiru.worker.runner import WorkerRunner

BEFORE = "媒体は人物に役割を割り振るだけでなく、趣味の承認も左右する。"
AFTER = "また、媒体はこうした人物配置を形づくるだけでなく、趣味の承認も左右する。"
HTML = (
    '<article id="D" lang="ja"><section id="S"><h2 id="H">比較</h2>'
    '<p id="P">この比較は、一部の媒体で反復される人物配置の違いを捉える。</p>'
    '<p id="Q">' + BEFORE + 'すべての媒体が同じだとはいえない。'
    '<cite data-reference-id="R">『研究書<sup id="FNREF-N">'
    '<a role="doc-noteref" href="#N">1</a></sup>』</cite>を参照する。</p></section>'
    '<section id="T"><h2 id="TH">別の対象</h2><p id="Z">他方、作品内の時間を考える。</p>'
    '</section><section id="NOTES" data-citation-section="footnotes"><h2 id="NH">注</h2>'
    '<ol id="OL"><li id="N"><p id="NP">注の本文。</p></li></ol></section></article>'
)


def response(payload, *, repair=True, meaning=True, issue="both"):
    by_id = {p["paragraph_id"]: p for p in payload["paragraphs"]}
    rows = []
    for boundary in payload["boundaries"]:
        left = by_id[boundary["left_paragraph_id"]]["visible_text"]
        right = by_id[boundary["right_paragraph_id"]]["visible_text"]
        bad = boundary["right_paragraph_id"] == "Q" and not right.startswith("また、")
        rows.append({
            **{k: boundary[k] for k in ("boundary_id", "left_paragraph_id", "right_paragraph_id")},
            "relation": "addition" if boundary["right_paragraph_id"] == "Q" else "contrast",
            "carried_term": "人物配置" if boundary["right_paragraph_id"] == "Q" else "",
            "left_evidence": left[:30], "right_evidence": right[:30],
            "connective": "また" if right.startswith("また、") else "",
            "central_term_continuity": not bad or issue == "connective",
            "connective_appropriate": not bad or issue == "term",
            "reason": "中心語の受け直しと付加の予告が必要。" if bad else "明瞭。",
        })
    edits = []
    if repair and payload["mode"] == "review" and any(
        b["right_paragraph_id"] == "Q" for b in payload["boundaries"]
    ) and not by_id["Q"][
        "visible_text"
    ].startswith("また、") and by_id["Q"]["editable"]:
        slots = list(by_id["Q"]["text_slots"])
        slots[0] = slots[0].replace(BEFORE, AFTER)
        edits = [{"paragraph_id": "Q", "text_slots": slots}]
    return {"boundaries": rows, "paragraphs": edits,
            "meaning_preserved": meaning, "meaning_reason": "範囲・否定と支持対象を確認。"}


def test_all_boundaries_include_section_edges_and_preserve_protected_context():
    paragraphs, boundaries = transitions.inventory(HTML, {"Q"})
    assert [p["paragraph_id"] for p in paragraphs] == ["P", "Q", "Z"]
    assert [p["editable"] for p in paragraphs] == [True, False, True]
    assert [b["crosses_section"] for b in boundaries] == [False, True]
    assert "1" not in paragraphs[1]["visible_text"]


def test_generation_and_review_prompts_separate_central_terms_from_connectives():
    from fukamiru.authoring.codex import STAGE_GUIDANCE

    for stage in ("semantic_paragraphs_v31", "draft_introduction_v31", "draft_body_v31",
                  "draft_conclusion_v31", "compose_body_prose_v31"):
        assert "段落をまたぐ中心概念" in STAGE_GUIDANCE[stage]
        assert "文中にあっても" in STAGE_GUIDANCE[stage]
    guidance = STAGE_GUIDANCE["review_paragraph_transitions_v32"]
    assert "central_term_continuityとconnective_appropriateを別々に" in guidance
    assert "意味保存は字面の同一性で判定しない" in guidance
    assert "全段落に同じ語を付けない" in guidance


@pytest.mark.parametrize("issue", ["both", "term", "connective"])
def test_term_and_connective_are_checked_independently_then_rechecked(issue):
    calls = []

    def invoke(_claimed, **kwargs):
        calls.append(kwargs["payload"])
        return response(kwargs["payload"], issue=issue)

    revised, report, human = transitions.run(
        SimpleNamespace(_authoring_invoke=invoke), None, {}, HTML, set()
    )
    assert [c["mode"] for c in calls] == ["review", "verify"]
    assert report["status"] == "passed" and report["boundary_count"] == 2
    assert report["cross_section_boundary_count"] == 1
    assert report["changed_paragraph_ids"] == ["Q"] and human == []
    assert AFTER in revised and BEFORE not in revised
    assert "すべての媒体が同じだとはいえない。" in revised
    assert prose.manifest(HTML) == prose.manifest(revised)
    assert ET.tostring(ET.fromstring(HTML).find(".//*[@id='FNREF-N']")) == ET.tostring(
        ET.fromstring(revised).find(".//*[@id='FNREF-N']")
    )
    assert all(len(c["boundaries"]) == 2 for c in calls)


def test_meaning_change_rejects_repair_and_preserves_review_without_a_prose_caveat():
    def invoke(_claimed, **kwargs):
        return response(kwargs["payload"], meaning=kwargs["payload"]["mode"] == "review")

    revised, report, human = transitions.run(
        SimpleNamespace(_authoring_invoke=invoke), None, {}, HTML, set()
    )
    assert revised == HTML
    assert report["status"] == "needs_human_review" and report["rejected_meaning_change"]
    assert report["changed_paragraph_ids"] == []
    assert human[0]["sentence_id"] == "Q" and human[0]["category"] == "paragraph_transition"


def test_protected_paragraph_is_reviewed_but_never_edited():
    revised, report, human = transitions.run(
        SimpleNamespace(_authoring_invoke=lambda _, **kw: response(kw["payload"])),
        None, {}, HTML, {"Q"},
    )
    assert revised == HTML and report["status"] == "needs_human_review"
    assert len(human) == 1


def test_clear_topic_shift_does_not_force_additive_connectives_or_repeated_terms():
    revised, report, human = transitions.run(
        SimpleNamespace(_authoring_invoke=lambda _, **kw: response(kw["payload"])),
        None, {}, HTML.replace(BEFORE, AFTER), set(),
    )
    assert report["status"] == "passed" and human == []
    assert "他方、作品内の時間を考える。" in revised


def test_changed_paragraph_is_rechecked_on_both_sides_of_a_batch_boundary(monkeypatch):
    monkeypatch.setattr(transitions, "BATCH_SIZE", 1)
    calls = []

    def invoke(_claimed, **kwargs):
        calls.append(kwargs["payload"])
        return response(kwargs["payload"])

    _, report, _ = transitions.run(
        SimpleNamespace(_authoring_invoke=invoke), None, {}, HTML, set()
    )
    assert [p["mode"] for p in calls] == ["review", "review", "verify", "verify"]
    assert report["status"] == "passed"
    assert next(p for p in calls[-1]["paragraphs"] if p["paragraph_id"] == "Q")[
        "visible_text"
    ].startswith(AFTER)


def test_prose_node_checks_final_boundaries_after_verification_note_deletion(tmp_path):
    from dataclasses import replace

    from tests.test_prose_redraft import claimed, invoke_fixture, mock_runner

    note = "ただし、資料の照合はできていない。"
    document = HTML.replace('<p id="Q">', '<p id="V">' + note + '</p><p id="Q">')
    runner, _ = mock_runner(tmp_path)
    calls = []

    def invoke(_claimed, *, stage, context, payload, operation_name):
        calls.append((stage, payload))
        if stage == "compose_body_prose_v31":
            return {"paragraphs": [{
                "paragraph_id": p["paragraph_id"], "text_slots": p["source_text_slots"],
                "review_reason": "", "addressed_issue_ids": [],
            } for p in payload["paragraphs"]]}
        if stage == "review_paragraph_transitions_v32":
            assert all(b["right_paragraph_id"] != "V" for b in payload["boundaries"])
            return response(payload)
        result = invoke_fixture(stage, payload)
        for p in result.get("paragraphs", []):
            if p["paragraph_id"] == "V":
                p["editorial_only"] = [note]
        return result

    runner._authoring_invoke = invoke
    context = WorkerRunner._authoring_redraft_body_prose(
        runner, replace(claimed(), workflow_version="32"), {"proposed_html": document}
    ).context
    assert AFTER in context["proposed_html"] and note not in context["proposed_html"]
    report = context["paragraph_transition_report_v32"]
    assert report["boundary_count"] == 2 and report["status"] == "passed"
    assert calls[-1][0] == "review_paragraph_transitions_v32"
    assert context["verification_notes_routed_v32"][0]["reason"] == note
    prose.validate_handoff(context)
    stale = deepcopy(context)
    stale["paragraph_transition_report_v32"]["target_html_sha256"] = "stale"
    with pytest.raises(NonRetryableFukamiruError, match="transition.*stale"):
        prose.validate_handoff(stale)


@pytest.mark.parametrize("damage", ["missing", "evidence", "unrelated", "later_sentence", "slots"])
def test_malformed_results_cannot_be_checkpointed_or_change_unrelated_prose(damage):
    paragraphs, boundaries = transitions.inventory(HTML, set())
    payload = {"mode": "review", "paragraphs": paragraphs, "boundaries": boundaries}
    result = response(payload)
    if damage == "missing":
        result["boundaries"].pop()
    elif damage == "evidence":
        result["boundaries"][0]["right_evidence"] = "本文にない根拠"
    elif damage == "unrelated":
        result["paragraphs"][0]["paragraph_id"] = "Z"
    elif damage == "later_sentence":
        result["paragraphs"][0]["text_slots"][0] = AFTER + "すべて同じである。"
    else:
        result["paragraphs"][0]["text_slots"].append("余計な文。")
    with pytest.raises(NonRetryableFukamiruError):
        WorkerRunner._authoring_validate_checkpoint_result(
            "review_paragraph_transitions_v32", payload, deepcopy(result)
        )
