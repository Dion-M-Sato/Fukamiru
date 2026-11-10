from types import SimpleNamespace

import pytest
from test_authoring_generation import (
    EMPTY_PROJECTION_ERROR,
    empty_projection_fixture,
)
from test_workflow_bibliography import worker_case as worker_case

from fukamiru.authoring.generation import (
    recover_empty_citation_projection,
    rendered_qualification_recovery,
)
from fukamiru.worker.runner import WorkerRunner

QUALIFICATION = "その違いを確定する場面が確認されていない。"


def qualification_fixture():
    context = recover_empty_citation_projection(empty_projection_fixture(),
        error_message=EMPTY_PROJECTION_ERROR, source_node_run_id="failed")
    assert context is not None
    context["proposed_html"] = context["proposed_html"].replace(
        "著者は二つの条件を区別する。", "著者は二つの条件を区別する。" + QUALIFICATION)
    return context


def test_qualification_reaches_rendered_review_without_becoming_verified(worker_case):
    run_id = worker_case._create_run("manuscript_rewrite")
    claimed = SimpleNamespace(workflow_name="manuscript_generator", workflow_version="32",
                              run_id=run_id, node_run_id="render")
    html = '<article id="D" lang="ja"><section id="S"><h2 id="H">比較</h2>'
    html += '<p id="P">' + QUALIFICATION + '</p></section></article>'
    context = {"proposed_html": html, "reference_entries": [], "applied_edits": [],
               "authoring_paragraph_citation_lineage": [], "evidence_context": []}
    result = worker_case.worker._manuscript_render_evidence_footnotes_v29(claimed, context).context
    assert QUALIFICATION in result["proposed_html"]
    assert result["evidence_footnote_render_report"]["footnote_count"] == 0
    assert len(result["deferred_scholarly_qualifications_v31"]) == 1
    assert result["deferred_scholarly_qualifications_v31"][0]["code"] == "structure_invalid"
    review = result["human_review_items"][0]
    assert review["status"] == "open" and review["category"] == "scholarly_qualification"
    assert review["sentence_id"] == "P" and not review["blocks_render"]
    assert "留保" in review["reason"]
    assert result["evidence_context"] == []


@pytest.mark.parametrize("text,reviewable", [
    (QUALIFICATION, True), ("入力メモでは場面が確認されていない。", False),
    ("草稿と生ログを照合する。", False), ("この段落の出力は因果命題である。", False),
])
def test_qualification_routing_does_not_accept_internal_workflow_labels(text, reviewable):
    findings = WorkerRunner._authoring_reader_facing_v31_findings([
        {"section_id": "S", "paragraphs": [{"paragraph_id": "P", "text": text}]}])
    assert findings
    assert all(WorkerRunner._authoring_reviewable_scholarly_qualification(finding)
               for finding in findings) is reviewable


def test_qualification_recovery_checks_identity_and_preserves_unresolved_finding():
    context = qualification_fixture()
    error = "V31 rendered manuscript has reader-facing scaffold: SEC-plan-s2"
    recovery = rendered_qualification_recovery(context, error)
    assert recovery is not None
    assert recovery["qualification_findings"][0]["code"] == "structure_invalid"
    assert recovery["resolution_route"] == "prose_redraft_then_human_review"
    assert rendered_qualification_recovery(context, error + "-other") is None
    assert rendered_qualification_recovery({**context, "evidence_identity_hash": "changed"},
                                           error) is None
    internal = {**context, "proposed_html": context["proposed_html"].replace(
        QUALIFICATION, "入力メモでは場面が確認されていない。")}
    assert rendered_qualification_recovery(internal, error) is None
