"""V31 provisional bibliography remains visible without trapping submission."""

import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from fukamiru.errors import FukamiruError
from fukamiru.revisions.bibliography_footnote_style import format_footnote_reference
from fukamiru.revisions.bibliography_style import format_reference
from fukamiru.revisions.references import JapaneseReferenceService
from fukamiru.storage.runs import RunRepository
from fukamiru.worker.runner import NonRetryableFukamiruError, WorkerRunner

REFERENCE_ID = "reference:provisional-work"
HTML = (
    '<article id="D" lang="ja"><section id="S"><p id="P">本文。</p>'
    '<p id="R">著者「作品」［書誌種別・刊行情報要確認］。</p>'
    "</section></article>"
)


def test_v31_submission_join_uses_only_current_footnoted_audit():
    sha = hashlib.sha256(HTML.encode()).hexdigest()
    old_sha = hashlib.sha256(b"previous candidate").hexdigest()
    inherited = {
        "proposed_html": HTML,
        "footnoted_completion_status": "conditional_success",
        "footnoted_conditional_success": {"candidate_sha256": old_sha},
        "footnoted_coherence_unresolved_routes": [{"stage": "old"}],
    }
    parents = {
        "validate_reference_quality": {
            **inherited, "reference_quality_gate": {"passed": False, "target_html_sha256": sha}
        },
        "validate_footnoted_references": {
            **inherited, "reference_quality_gate": {"passed": False, "target_html_sha256": sha}
        },
        "validate_footnoted_structure": {
            **inherited, "rendered_structure_v26": {"valid": True, "html_sha256": sha}
        },
        "validate_footnoted_coherence": {
            **inherited,
            "footnoted_conditional_success": {"candidate_sha256": sha},
            "footnoted_coherence_unresolved_routes": [{"stage": "post_footnote"}],
            "footnoted_coherence_report": {"passed": False, "html_sha256": sha},
        },
    }
    merged = WorkerRunner._merge_v31_submission_validation_contexts(parents)
    assert merged["footnoted_conditional_success"]["candidate_sha256"] == sha
    assert merged["footnoted_coherence_unresolved_routes"] == [
        {"stage": "post_footnote"}
    ]
    parents["validate_footnoted_coherence"] = {
        **parents["validate_footnoted_coherence"],
        "footnoted_coherence_report": {"passed": False, "html_sha256": old_sha},
    }
    with pytest.raises(FukamiruError, match="coherence report is missing or stale"):
        WorkerRunner._merge_v31_submission_validation_contexts(parents)


def test_v31_stale_footnoted_join_recovery_is_bounded(monkeypatch):
    class PlainPayloads:
        def __init__(self, _connection):
            pass

        def unpack(self, value):
            return value

        def pack(self, value):
            return value

    monkeypatch.setattr("fukamiru.storage.runs.PayloadStore", PlainPayloads)
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE runs(id TEXT, workflow_name TEXT, workflow_version TEXT,
            status TEXT, error_json TEXT, completed_at TEXT, output_json TEXT);
        CREATE TABLE node_runs(id TEXT, run_id TEXT, node_id TEXT, attempt INTEGER,
            status TEXT, input_json TEXT, output_json TEXT, error_json TEXT,
            resource_lane TEXT, started_at TEXT, completed_at TEXT);
        CREATE TABLE authoring_pending_proposals(run_id TEXT);
        CREATE TABLE event_log(run_id TEXT,event_type TEXT,object_type TEXT,
            object_id TEXT,payload_json TEXT,created_at TEXT);
        """
    )
    connection.execute(
        "INSERT INTO runs(id,workflow_name,workflow_version,status) VALUES (?,?,?,?)",
        ("run", "manuscript_generator", "31", "failed"),
    )
    sha = hashlib.sha256(HTML.encode()).hexdigest()
    old_sha = hashlib.sha256(b"old").hexdigest()
    base = {
        "proposed_html": HTML,
        "_join_generations": {"join_submission_validations": "generation"},
        "footnoted_conditional_success": {"candidate_sha256": old_sha},
    }
    reports = (
        ("validate_reference_quality", "reference_quality_gate", {"target_html_sha256": sha}),
        ("validate_footnoted_references", "reference_quality_gate", {"target_html_sha256": sha}),
        ("validate_footnoted_structure", "rendered_structure_v26", {"html_sha256": sha}),
        ("validate_footnoted_coherence", "footnoted_coherence_report", {"html_sha256": sha}),
    )
    for node_id, key, report in reports:
        failed = node_id == "validate_footnoted_coherence"
        context = {**base, key: report}
        if failed:
            context["footnoted_conditional_success"] = {"candidate_sha256": sha}
        connection.execute(
            "INSERT INTO node_runs(id,run_id,node_id,attempt,status,output_json,error_json) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                node_id, "run", node_id, 14, "failed" if failed else "completed",
                json.dumps(context),
                json.dumps({"message": "Join context conflict at "
                    "validate_footnoted_coherence.footnoted_conditional_success.candidate_sha256"})
                if failed else None,
            ),
        )
    connection.commit()
    recovered = RunRepository(connection).recover_v31_stale_footnoted_submission_join("run")
    assert recovered["node_id"] == "join_submission_validations"
    joined = json.loads(connection.execute(
        "SELECT input_json FROM node_runs WHERE id=?", (recovered["node_run_id"],)
    ).fetchone()[0])
    assert joined["footnoted_conditional_success"]["candidate_sha256"] == sha
    with pytest.raises(FukamiruError, match="not an eligible"):
        RunRepository(connection).recover_v31_stale_footnoted_submission_join("run")


def test_self_citation_placeholder_never_renders_as_an_author_name():
    entry = {
        "reference_id": REFERENCE_ID,
        "author": "自著",
        "authors": ["自著"],
        "title": "架空資料の比較分析",
        "publication_language": "ja",
        "resolution_status": "resolved",
        "provisional_bibliography_review": True,
        "bibliography_people": [
            {"role": "author", "family": "山田", "given": "花子", "name_profile": "japanese_native"}
        ],
        "provenance": {"author": {"kind": "source_explicit"}},
    }
    reference = format_reference(entry)
    footnote = format_footnote_reference(entry)
    assert "山田花子" in reference.text
    assert "山田花子" in footnote.text
    assert "自著" not in reference.text + footnote.text
    assert "書誌種別・刊行情報要確認" in reference.text + footnote.text
    raw = {**entry, "original_text": "自著「架空資料の比較分析」"}
    rendered = JapaneseReferenceService.render_entry(raw, workflow_version=29)
    assert "山田花子" in rendered
    assert "自著" not in rendered


def test_self_citation_placeholder_without_an_identified_person_does_not_render():
    entry = {
        "reference_id": REFERENCE_ID,
        "author": "自著",
        "authors": ["自著"],
        "title": "作品",
        "publication_language": "ja",
        "resolution_status": "resolved",
        "provisional_bibliography_review": True,
        "provenance": {"author": {"kind": "source_explicit"}},
    }
    assert not format_footnote_reference(entry).supported
    assert "自著" not in format_reference(entry).text


@pytest.mark.parametrize("other_prose_defect", [False, True])
def test_reader_facing_render_tail_only_replays_identified_bibliography_bug(
    other_prose_defect,
):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        "CREATE TABLE runs(id TEXT,workflow_name TEXT,workflow_version TEXT,"
        "status TEXT,error_json TEXT,completed_at TEXT,output_json TEXT);"
        "CREATE TABLE node_runs(id TEXT,run_id TEXT,node_id TEXT,attempt INTEGER,"
        "status TEXT,input_json TEXT,error_json TEXT,resource_lane TEXT);"
        "CREATE TABLE authoring_pending_proposals(run_id TEXT);"
        "CREATE TABLE event_log(run_id TEXT,event_type TEXT,object_type TEXT,"
        "object_id TEXT,payload_json TEXT,created_at TEXT);"
    )
    html = (
        '<article><section id="body"><p>'
        + ("入力メモではこの議論を参照する。" if other_prose_defect else "因果関係ではない。")
        + '</p></section><section id="notes" '
        'data-citation-section="evidence-footnotes-v1"><p>自著「作品」。</p>'
        '</section></article>'
    )
    entry = {
        "reference_id": REFERENCE_ID,
        "author": "自著",
        "authors": ["自著"],
        "title": "作品",
        "publication_language": "ja",
        "resolution_status": "resolved",
        "is_self_authored": True,
        "bibliography_people": [
            {"role": "author", "family": "山田", "given": "花子", "name_profile": "japanese_native"}
        ],
        "provenance": {"author": {"kind": "source_explicit"}},
    }
    connection.execute(
        "INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
        ("run", "manuscript_generator", "31", "failed", "{}", "now", None),
    )
    connection.execute(
        "INSERT INTO node_runs VALUES (?,?,?,?,?,?,?,?)",
        (
            "failed", "run", "finalize_authoring", 1, "failed",
            json.dumps({"proposed_html": html}, ensure_ascii=False),
            json.dumps({"type": "NonRetryableFukamiruError", "message":
                        "V31 final manuscript contains reader-facing input scaffolding: notes"}),
            "cpu",
        ),
    )
    connection.execute(
        "INSERT INTO node_runs VALUES (?,?,?,?,?,?,?,?)",
        (
            "footnote", "run", "render_evidence_footnotes", 1, "completed",
            json.dumps({"proposed_html": "<article/>", "reference_entries": [entry]},
                       ensure_ascii=False),
            None, "cpu",
        ),
    )
    connection.commit()
    repository = RunRepository(connection)
    if other_prose_defect:
        with pytest.raises(FukamiruError, match="another reader-facing defect"):
            repository.resume_v31_reader_facing_render_tail("run")
        assert connection.execute("SELECT status FROM runs").fetchone()[0] == "failed"
    else:
        resumed = repository.resume_v31_reader_facing_render_tail("run")
        assert resumed["node_id"] == "render_evidence_footnotes"
        assert resumed["attempt"] == 2
        assert connection.execute("SELECT status FROM runs").fetchone()[0] == "queued"
        with pytest.raises(FukamiruError, match="not a failed V31 finalization"):
            repository.resume_v31_reader_facing_render_tail("run")
        connection.execute(
            "UPDATE node_runs SET status='failed',error_json=? WHERE id=?",
            (
                json.dumps({
                    "type": "FukamiruError",
                    "message": "V31 rendered manuscript has reader-facing scaffold: notes",
                }),
                resumed["node_run_id"],
            ),
        )
        connection.execute("UPDATE runs SET status='failed' WHERE id='run'")
        connection.commit()
        with pytest.raises(FukamiruError, match="already used its recovery"):
            repository.resume_v31_reader_facing_render_tail("run")
        second = repository.resume_v31_reader_facing_render_tail(
            "run", allow_second_attempt=True
        )
        assert second["attempt"] == 3
        connection.execute(
            "UPDATE node_runs SET status='failed',error_json=? WHERE id=?",
            (
                json.dumps({
                    "type": "FukamiruError",
                    "message": "V31 rendered manuscript has reader-facing scaffold: notes",
                }),
                second["node_run_id"],
            ),
        )
        connection.execute("UPDATE runs SET status='failed' WHERE id='run'")
        connection.commit()
        with pytest.raises(FukamiruError, match="already used its recovery"):
            repository.resume_v31_reader_facing_render_tail(
                "run", allow_second_attempt=True
            )


def _context(*, issue_code="reference.metadata_incomplete", version=31):
    issues = [
        {
            "code": "reference.metadata_incomplete",
            "reference_id": REFERENCE_ID,
            "fields": ["container_title", "year", "url"],
        },
        {"code": "reference.final_revision_blocked", "reference_id": REFERENCE_ID},
    ]
    if issue_code != "reference.metadata_incomplete":
        issues[0]["code"] = issue_code
    return {
        "authoring_workflow_policy": {"workflow_version": version},
        "proposed_html": HTML,
        "reference_entries": [
            {
                "reference_id": REFERENCE_ID,
                "title": "作品",
                "resolution_status": "resolved",
                "final_revision_blocked": True,
                "provisional_bibliography_review": True,
                "bibliography_people": [{"role": "author", "family": "著者"}],
            }
        ],
        "final_bibliography_entries": [
            {
                "reference_id": REFERENCE_ID,
                "title": "作品",
                "resolution_status": "resolved",
                "final_revision_blocked": True,
                "provisional_bibliography_review": True,
                "bibliography_people": [{"role": "author", "family": "著者"}],
            }
        ],
        "human_review_items": [
            {
                "review_item_id": "bibliography-kind:" + REFERENCE_ID,
                "section_id": "bibliography",
                "category": "bibliography_metadata",
                "reason": "刊行情報を確認",
                "source_ids": [REFERENCE_ID],
                "recommended_check": "原資料で確認",
                "blocks_render": False,
                "status": "open",
            }
        ],
        "reference_quality_gate": {
            "passed": False,
            "issues": issues,
            "missing_author_citation_ids": [],
            "expected_author_citation_count": 1,
            "rendered_author_citation_count": 1,
            "required_reference_entry_count": 1,
            "rendered_reference_entry_count": 1,
        },
        "reference_inventory": {"reference_section_count": 1},
        "reference_resolution_report": {"resolved_count": 1},
        "reference_render_report": {"rendered_count": 1},
        "rendered_structure_v26": {"valid": True},
        "footnoted_coherence_report": {"passed": True},
    }


def test_v31_provisional_publication_details_continue_with_visible_review():
    context = _context()
    result = WorkerRunner.__new__(WorkerRunner)._authoring_join_submission_validations(
        SimpleNamespace(), context
    )
    assert result.outcome == "conditional_success"
    gate = result.context["submission_validation_gate"]
    assert gate["failed_validations"] == []
    assert gate["provisional_bibliography_review_ids"] == [REFERENCE_ID]
    assert gate["reports"]["reference_quality"]["passed"] is False
    assert result.context["human_review_items"] == context["human_review_items"]
    completion = WorkerRunner._manuscript_completion_gate(result.context)
    codes = {item["code"] for item in completion["blocking_reasons"]}
    assert "reference.quality_gate_failed" not in codes
    assert "reference.final_revision_confirmation_required" not in codes


def test_submission_join_preserves_explicit_empty_footnoted_audit():
    context = _context()
    context["v26_coherence_report"] = {"stage": "pre_footnote", "passed": False}
    context["v26_block_verification_records"] = [
        {"block_id": "old", "html_sha256": "stale"}
    ]
    context["conditional_success"] = {"reason": "coherence_unresolved"}
    context["footnoted_coherence_report"] = {
        "stage": "post_footnote", "passed": True
    }
    context["footnoted_block_verification_records"] = []
    context["footnoted_conditional_success"] = {}

    result = WorkerRunner.__new__(WorkerRunner)._authoring_join_submission_validations(
        SimpleNamespace(), context
    )

    assert result.context["v26_coherence_report"] == context[
        "footnoted_coherence_report"
    ]
    assert result.context["v26_block_verification_records"] == []
    assert result.context.get("conditional_success") is None


def test_submission_join_uses_legacy_audit_only_without_footnoted_keys():
    context = _context()
    context.pop("footnoted_coherence_report")
    context["v26_coherence_report"] = {"stage": "legacy", "passed": True}
    context["v26_block_verification_records"] = [
        {"block_id": "legacy", "html_sha256": "current"}
    ]

    result = WorkerRunner.__new__(WorkerRunner)._authoring_join_submission_validations(
        SimpleNamespace(), context
    )

    assert result.context["v26_coherence_report"] == context["v26_coherence_report"]
    assert result.context["v26_block_verification_records"] == context[
        "v26_block_verification_records"
    ]


@pytest.mark.parametrize("change", ["unknown_issue", "missing_note", "missing_review", "v30"])
def test_provisional_exception_rejects_unverified_or_legacy_cases(change):
    context = _context()
    if change == "unknown_issue":
        context["reference_quality_gate"]["issues"][0]["code"] = "reference.identity_ambiguous"
    elif change == "missing_note":
        context["proposed_html"] = context["proposed_html"].replace(
            "［書誌種別・刊行情報要確認］", ""
        )
    elif change == "missing_review":
        context["human_review_items"] = []
    else:
        context["authoring_workflow_policy"]["workflow_version"] = 30
    result = WorkerRunner.__new__(WorkerRunner)._authoring_join_submission_validations(
        SimpleNamespace(), context
    )
    assert result.outcome == "failure"
    assert result.context["submission_validation_gate"]["failed_validations"] == [
        "reference_quality", "reference_integrity"
    ]
    if change != "v30":
        completion = WorkerRunner._manuscript_completion_gate(result.context)
        codes = {item["code"] for item in completion["blocking_reasons"]}
        assert "reference.quality_gate_failed" in codes


def test_unchanged_failed_submission_cannot_loop():
    worker = WorkerRunner.__new__(WorkerRunner)
    context = _context(issue_code="reference.identity_ambiguous")
    first = worker._authoring_join_submission_validations(SimpleNamespace(), context)
    with pytest.raises(NonRetryableFukamiruError, match="repeated unchanged"):
        worker._authoring_join_submission_validations(SimpleNamespace(), first.context)


def _recovery_database(monkeypatch):
    class PlainPayloads:
        def __init__(self, _connection):
            pass

        def unpack(self, value):
            return value

        def pack(self, value):
            return value

    monkeypatch.setattr("fukamiru.storage.runs.PayloadStore", PlainPayloads)
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE runs(id TEXT, workflow_name TEXT, workflow_version TEXT,
            status TEXT, error_json TEXT, completed_at TEXT, output_json TEXT);
        CREATE TABLE node_runs(id TEXT, run_id TEXT, node_id TEXT, attempt INTEGER,
            status TEXT, input_json TEXT, output_json TEXT, error_json TEXT,
            resource_lane TEXT, started_at TEXT, completed_at TEXT);
        CREATE TABLE authoring_pending_proposals(run_id TEXT);
        CREATE TABLE event_log(run_id TEXT,event_type TEXT,object_type TEXT,
            object_id TEXT,payload_json TEXT,created_at TEXT);
        """
    )
    connection.execute(
        "INSERT INTO runs(id,workflow_name,workflow_version,status,error_json) VALUES (?,?,?,?,?)",
        ("run", "manuscript_generator", "31", "failed", '{"message":"database is locked"}'),
    )
    context = _context()
    context["rendered_structure_v26"] = {"valid": True}
    joined = {**context, "submission_validation_gate": {
        "candidate_sha256": hashlib.sha256(HTML.encode()).hexdigest(),
        "failed_validations": ["reference_quality", "reference_integrity"],
    }}
    nodes = [
        ("structure-old", "validate_footnoted_structure", 12, "completed", context, context, None),
        ("structure-failed", "validate_footnoted_structure", 13, "failed", context, None,
         {"message": "database is locked"}),
        ("join-old", "join_submission_validations", 13, "completed", context, joined, None),
        ("repair-stale", "repair_final_coherence", 13, "queued", context, None, None),
    ]
    for node_id, name, attempt, status, input_, output, error in nodes:
        connection.execute(
            "INSERT INTO node_runs(id,run_id,node_id,attempt,status,input_json,"
            "output_json,error_json,resource_lane) VALUES (?,?,?,?,?,?,?,?,?)",
            (node_id, "run", name, attempt, status, json.dumps(input_),
             json.dumps(output) if output is not None else None,
             json.dumps(error) if error is not None else None, "ingestion_cpu"),
        )
    connection.commit()
    return connection


def test_recovery_retires_stale_repair_and_queues_one_join(monkeypatch):
    connection = _recovery_database(monkeypatch)
    result = RunRepository(connection).recover_v31_provisional_submission_join("run")
    assert result["node_id"] == "join_submission_validations"
    assert result["attempt"] == 14
    assert connection.execute(
        "SELECT status FROM node_runs WHERE id='repair-stale'"
    ).fetchone()[0] == "cancelled"
    assert connection.execute("SELECT status FROM runs WHERE id='run'").fetchone()[0] == "queued"
    with pytest.raises(FukamiruError, match="not the failed V31 submission lock"):
        RunRepository(connection).recover_v31_provisional_submission_join("run")


def test_recovery_rejects_changed_candidate(monkeypatch):
    connection = _recovery_database(monkeypatch)
    connection.execute(
        "UPDATE node_runs SET input_json=? WHERE id='structure-failed'",
        (json.dumps({"proposed_html": "different"}),),
    )
    connection.commit()
    with pytest.raises(FukamiruError, match="candidates or validation differ"):
        RunRepository(connection).recover_v31_provisional_submission_join("run")
    assert connection.execute(
        "SELECT status FROM node_runs WHERE id='repair-stale'"
    ).fetchone()[0] == "queued"
