"""A late authoring restart must preserve earlier records on repair cycles."""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from fukamiru.storage.events import EventRepository
from fukamiru.storage.runs import RunCreated, RunRepository


def test_contract_restart_discards_old_revision_parent_before_expensive_generation() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE runs (
            id TEXT PRIMARY KEY, project_id TEXT, workflow_name TEXT,
            workflow_version TEXT, status TEXT, input_json TEXT
        );
        CREATE TABLE node_runs (
            id TEXT PRIMARY KEY, run_id TEXT, node_id TEXT, status TEXT,
            input_json TEXT, completed_at TEXT
        );
    """)
    source = {
        "plan_id": "original-plan",
        "plan_revision": 2,
        "parent_plan_revision": 1,
        "research_question": {"text": "問い"},
        "claims": [{"claim_id": "c1"}],
        "argument_nodes": [{"node_id": "a1"}],
        "discourse_nodes": [{"node_id": "d1"}],
        "manuscript_structure_plan": {"sections": [{"section_id": "s1"}]},
        "annotation_citation_evidence": [{"citation_id": "cite1"}],
        "reference_entries": [{"reference_id": "r1"}],
    }
    connection.execute(
        "INSERT INTO runs VALUES(?,?,?,?,?,?)",
        ("source", "project", "manuscript_generator", "31", "waiting_human", "{}"),
    )
    connection.execute(
        "INSERT INTO node_runs VALUES(?,?,?,?,?,?)",
        ("boundary", "source", "lock_section_contracts", "completed",
         json.dumps(source), "2026-10-16T00:00:00Z"),
    )
    captured = {}

    def fake_create(_self, **kwargs):
        captured.update(kwargs)
        return RunCreated(run_id="derived", event_id=1)

    with patch.object(RunRepository, "create", fake_create), patch.object(
        EventRepository, "append", return_value=SimpleNamespace(event_id=2)
    ):
        result = RunRepository(connection).create_authoring_contract_restart(
            "source", workflow_version="31"
        )

    assert result.run_id == "derived"
    assert captured["entry_node"] == "lock_section_contracts"
    assert captured["inputs"]["plan_revision"] == 1
    assert "parent_plan_revision" not in captured["inputs"]
    assert captured["inputs"]["claims"] == source["claims"]
    assert captured["inputs"]["plan_id"] != "original-plan"


def test_decide_evidence_restart_accepts_its_saved_pre_structure_handoff() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(
        """
        CREATE TABLE authoring_plans (
            id TEXT PRIMARY KEY, current_revision INTEGER NOT NULL
        );
        CREATE TABLE authoring_plan_revisions (
            plan_id TEXT NOT NULL, revision INTEGER NOT NULL,
            producer_node_run_id TEXT REFERENCES node_runs(id) ON DELETE SET NULL,
            created_at TEXT, PRIMARY KEY(plan_id,revision)
        );
        CREATE TRIGGER immutable_plan_revision_update
        BEFORE UPDATE ON authoring_plan_revisions
        BEGIN SELECT RAISE(ABORT, 'authoring plan revisions are immutable'); END;
        CREATE TRIGGER immutable_plan_revision_delete
        BEFORE DELETE ON authoring_plan_revisions
        BEGIN SELECT RAISE(ABORT, 'authoring plan revisions are immutable'); END;
        CREATE TABLE runs (
            id TEXT PRIMARY KEY, workflow_name TEXT, workflow_version TEXT,
            status TEXT, error_json TEXT, output_json TEXT, completed_at TEXT
        );
        CREATE TABLE node_runs (
            id TEXT PRIMARY KEY, run_id TEXT, node_id TEXT, attempt INTEGER,
            status TEXT, input_json TEXT, resource_lane TEXT, started_at TEXT,
            UNIQUE(run_id, node_id, attempt)
        );
        CREATE TABLE event_log (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT,
            event_type TEXT, object_type TEXT, object_id TEXT,
            payload_json TEXT, created_at TEXT
        );
        """
    )
    run_id = "evidence-restart"
    handoff = {
        "plan_id": "plan-v31",
        "plan_revision": 1,
        "propositions": [{"id": "p1"}],
        "claims": [{"id": "c1"}],
        "claim_proposition_bindings": [],
        "proposition_verification_scopes": [],
        "atomic_evidence_obligations": [],
        "evidence_context": [],
        "evidence_coverage": [],
    }
    connection.execute(
        "INSERT INTO runs(id,workflow_name,workflow_version,status) "
        "VALUES(?,'manuscript_generator','31','cancelled')", (run_id,)
    )
    connection.execute(
        "INSERT INTO authoring_plans VALUES('plan-v31',1)"
    )
    events = EventRepository(connection)
    for node_run_id, node_id in (
        ("upstream", "resolve_reference_metadata"),
        ("old-decision", "decide_evidence_realization"),
        ("old-validation", "validate_evidence_ready"),
        ("old-lock", "lock_section_contracts"),
    ):
        connection.execute(
            "INSERT INTO node_runs(id,run_id,node_id,attempt,status,input_json,"
            "resource_lane,started_at) VALUES(?,?,?,1,'completed',?,'ingestion_cpu',?)",
            (node_run_id, run_id, node_id, json.dumps(handoff),
             "2026-01-01T00:00:00Z" if node_id == "resolve_reference_metadata"
             else "2026-01-01T00:01:00Z"),
        )
        events.append(
            run_id, "node.completed", object_type="node_run",
            object_id=node_run_id, payload={"node_id": node_id},
        )
    connection.execute(
        "INSERT INTO authoring_plan_revisions VALUES"
        "('plan-v31',1,'old-lock','2026-01-01T00:02:00Z')"
    )
    connection.commit()
    result = RunRepository(connection).restart_authoring_from_node(
        run_id,
        entry_node="decide_evidence_realization",
        downstream_node_ids={
            "decide_evidence_realization", "validate_evidence_ready",
            "lock_section_contracts",
        },
        resource_lane="ingestion_cpu",
    )
    rows = connection.execute(
        "SELECT id,node_id,status,input_json FROM node_runs WHERE run_id=? ORDER BY rowid",
        (run_id,),
    ).fetchall()
    assert result["deleted_node_run_count"] == 2
    assert [row["node_id"] for row in rows] == [
        "resolve_reference_metadata", "lock_section_contracts",
        "decide_evidence_realization",
    ]
    assert rows[-1]["status"] == "queued"
    assert json.loads(rows[-1]["input_json"]) == {
        **handoff, "plan_revision": 2, "parent_plan_revision": 1,
    }
    assert result["preserved_historical_node_run_ids"] == ["old-lock"]
    assert connection.execute(
        "SELECT producer_node_run_id FROM authoring_plan_revisions "
        "WHERE plan_id='plan-v31' AND revision=1"
    ).fetchone()[0] == "old-lock"
    assert connection.execute(
        "SELECT COUNT(*) FROM event_log WHERE object_id='old-lock'"
    ).fetchone()[0] == 1


def test_late_v31_restart_keeps_completed_ancestor_on_repair_cycle() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE runs (
            id TEXT PRIMARY KEY, workflow_name TEXT, workflow_version TEXT,
            status TEXT, error_json TEXT, output_json TEXT, completed_at TEXT
        );
        CREATE TABLE node_runs (
            id TEXT PRIMARY KEY, run_id TEXT, node_id TEXT, attempt INTEGER,
            status TEXT, input_json TEXT, resource_lane TEXT, started_at TEXT,
            UNIQUE(run_id, node_id, attempt)
        );
        CREATE TABLE event_log (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT,
            event_type TEXT, object_type TEXT, object_id TEXT,
            payload_json TEXT, created_at TEXT
        );
        """
    )
    run_id = "cycle-restart"
    entry_node = "reconcile_candidate_context_after_proofreading"
    handoff = json.dumps(
        {
            "plan_id": "plan-v31",
            "plan_revision": 1,
            "research_problem": {"research_question": "問い"},
            "section_contracts": [{"section_id": "sec-body"}],
        }
    )
    connection.execute(
        "INSERT INTO runs(id,workflow_name,workflow_version,status) "
        "VALUES(?,'manuscript_generator','31','failed')",
        (run_id,),
    )
    events = EventRepository(connection)
    for node_run_id, node_id, status, attempt in (
        ("earlier-reconcile", entry_node, "completed", 1),
        ("completed-polish", "polish_prose", "completed", 1),
        ("failed-reconcile", entry_node, "failed", 2),
    ):
        connection.execute(
            "INSERT INTO node_runs(id,run_id,node_id,attempt,status,input_json,resource_lane) "
            "VALUES(?,?,?,?,?,?,'codex_remote')",
            (node_run_id, run_id, node_id, attempt, status, handoff),
        )
        events.append(
            run_id,
            f"node.{status}",
            object_type="node_run",
            object_id=node_run_id,
            payload={"node_id": node_id},
        )

    result = RunRepository(connection).restart_authoring_from_node(
        run_id,
        entry_node=entry_node,
        downstream_node_ids={entry_node, "polish_prose"},
        resource_lane="codex_remote",
    )

    rows = connection.execute(
        "SELECT id,node_id,attempt,status FROM node_runs WHERE run_id=? ORDER BY rowid",
        (run_id,),
    ).fetchall()
    assert result["deleted_node_run_count"] == 1
    assert any(row["id"] == "earlier-reconcile" for row in rows)
    assert any(row["id"] == "completed-polish" for row in rows)
    assert not any(row["id"] == "failed-reconcile" for row in rows)
    assert any(
        row["node_id"] == entry_node and row["status"] == "queued"
        and row["attempt"] == 2
        for row in rows
    )


def test_draft_restart_clears_only_failed_draft_checkpoint_and_keeps_handoff() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE runs (
            id TEXT PRIMARY KEY, workflow_name TEXT, workflow_version TEXT,
            status TEXT, error_json TEXT, output_json TEXT, completed_at TEXT
        );
        CREATE TABLE node_runs (
            id TEXT PRIMARY KEY, run_id TEXT, node_id TEXT, attempt INTEGER,
            status TEXT, input_json TEXT, resource_lane TEXT, started_at TEXT,
            UNIQUE(run_id, node_id, attempt)
        );
        CREATE TABLE event_log (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT,
            event_type TEXT, object_type TEXT, object_id TEXT,
            payload_json TEXT, created_at TEXT
        );
        CREATE TABLE llm_result_checkpoints (
            id TEXT PRIMARY KEY, run_id TEXT, node_run_id TEXT,
            node_id TEXT, result_json TEXT, created_at TEXT
        );
        """
    )
    run_id = "draft-restart"
    handoff = json.dumps({
        "plan_id": "plan-v31", "plan_revision": 1,
        "research_problem": {"research_question": "問い"},
        "section_contracts": [{"section_id": "s01"}],
    })
    connection.execute(
        "INSERT INTO runs(id,workflow_name,workflow_version,status) "
        "VALUES(?,'manuscript_generator','31','failed')", (run_id,)
    )
    events = EventRepository(connection)
    for node_run_id, node_id, status in (
        ("upstream", "lock_section_contracts", "completed"),
        ("failed-draft", "draft_rewrite", "failed"),
    ):
        connection.execute(
            "INSERT INTO node_runs(id,run_id,node_id,attempt,status,input_json,"
            "resource_lane,started_at) VALUES(?,?,?,1,?,?,'codex_remote',?)",
            (node_run_id, run_id, node_id, status, handoff,
             "2026-01-01T00:00:00Z" if node_id != "draft_rewrite"
             else "2026-01-01T00:01:00Z"),
        )
        events.append(
            run_id, f"node.{status}", object_type="node_run",
            object_id=node_run_id, payload={"node_id": node_id},
        )
        connection.execute(
            "INSERT INTO llm_result_checkpoints VALUES(?,?,?,?,?,?)",
            (node_run_id + "-checkpoint", run_id, node_run_id, node_id,
             "{}", "2026-01-01T00:00:00Z" if node_id != "draft_rewrite"
             else "2026-01-01T00:01:00Z"),
        )
    connection.commit()

    result = RunRepository(connection).restart_authoring_from_node(
        run_id, entry_node="draft_rewrite",
        downstream_node_ids={"draft_rewrite"}, resource_lane="codex_remote",
    )

    nodes = connection.execute(
        "SELECT id,node_id,status,input_json FROM node_runs WHERE run_id=? "
        "ORDER BY rowid", (run_id,)
    ).fetchall()
    checkpoints = connection.execute(
        "SELECT id FROM llm_result_checkpoints WHERE run_id=? ORDER BY id", (run_id,)
    ).fetchall()
    assert result["deleted_node_run_count"] == 1
    assert result["deleted_record_counts"]["llm_result_checkpoints"] == 1
    assert [row["id"] for row in nodes[:1]] == ["upstream"]
    assert nodes[1]["node_id"] == "draft_rewrite"
    assert nodes[1]["status"] == "queued"
    assert json.loads(nodes[1]["input_json"]) == json.loads(handoff)
    assert [row["id"] for row in checkpoints] == ["upstream-checkpoint"]


def test_cancelled_prose_restart_removes_abandoned_recomposition_tail() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE runs (
            id TEXT PRIMARY KEY, workflow_name TEXT, workflow_version TEXT,
            status TEXT, error_json TEXT, output_json TEXT, completed_at TEXT
        );
        CREATE TABLE node_runs (
            id TEXT PRIMARY KEY, run_id TEXT, node_id TEXT, attempt INTEGER,
            status TEXT, input_json TEXT, resource_lane TEXT, started_at TEXT,
            UNIQUE(run_id, node_id, attempt)
        );
        CREATE TABLE event_log (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT,
            event_type TEXT, object_type TEXT, object_id TEXT,
            payload_json TEXT, created_at TEXT
        );
        """
    )
    run_id = "cancelled-prose"
    handoff = json.dumps({
        "plan_id": "plan-v31", "plan_revision": 1,
        "research_problem": {"research_question": "問い"},
        "section_contracts": [{"section_id": "s01"}],
    })
    connection.execute(
        "INSERT INTO runs(id,workflow_name,workflow_version,status) "
        "VALUES(?,'manuscript_generator','31','cancelled')", (run_id,)
    )
    events = EventRepository(connection)
    for node_run_id, node_id, status, attempt in (
        ("upstream-draft", "draft_rewrite", "completed", 1),
        ("first-prose", "redraft_body_prose", "completed", 1),
        ("abandoned-plan", "plan_semantic_paragraphs", "completed", 2),
        ("abandoned-draft", "draft_rewrite", "cancelled", 2),
    ):
        connection.execute(
            "INSERT INTO node_runs(id,run_id,node_id,attempt,status,input_json,"
            "resource_lane) VALUES(?,?,?,?,?,?,'codex_remote')",
            (node_run_id, run_id, node_id, attempt, status, handoff),
        )
        events.append(
            run_id, f"node.{status}", object_type="node_run",
            object_id=node_run_id, payload={"node_id": node_id},
        )
    result = RunRepository(connection).restart_authoring_from_node(
        run_id, entry_node="redraft_body_prose",
        downstream_node_ids={
            "redraft_body_prose", "plan_semantic_paragraphs", "draft_rewrite",
        },
        resource_lane="codex_remote",
    )
    nodes = connection.execute(
        "SELECT id,node_id,status FROM node_runs WHERE run_id=? ORDER BY rowid",
        (run_id,),
    ).fetchall()
    assert result["deleted_node_run_count"] == 3
    assert [(row["id"], row["node_id"]) for row in nodes[:-1]] == [
        ("upstream-draft", "draft_rewrite")
    ]
    assert nodes[-1]["node_id"] == "redraft_body_prose"
    assert nodes[-1]["status"] == "queued"
    status = connection.execute(
        "SELECT status FROM runs WHERE id=?", (run_id,)
    ).fetchone()[0]
    assert status == "queued"


@pytest.mark.parametrize("entry_node", ["draft_rewrite", "redraft_body_prose"])
def test_waiting_human_restart_preserves_upstream_and_supersedes_review(
    entry_node: str,
) -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE runs (
            id TEXT PRIMARY KEY, workflow_name TEXT, workflow_version TEXT,
            status TEXT, error_json TEXT, output_json TEXT, completed_at TEXT
        );
        CREATE TABLE node_runs (
            id TEXT PRIMARY KEY, run_id TEXT, node_id TEXT, attempt INTEGER,
            status TEXT, input_json TEXT, resource_lane TEXT, started_at TEXT,
            UNIQUE(run_id, node_id, attempt)
        );
        CREATE TABLE event_log (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT,
            event_type TEXT, object_type TEXT, object_id TEXT,
            payload_json TEXT, created_at TEXT
        );
        CREATE TABLE human_reviews (
            id TEXT PRIMARY KEY, run_id TEXT, status TEXT, created_at TEXT
        );
        """
    )
    run_id = f"human-waiting-{entry_node}"
    handoff = json.dumps({
        "plan_id": "plan-v31", "plan_revision": 1,
        "research_problem": {"research_question": "問い"},
        "section_contracts": [{"section_id": "s01"}],
    })
    connection.execute(
        "INSERT INTO runs(id,workflow_name,workflow_version,status) "
        "VALUES(?,'manuscript_generator','31','waiting_human')", (run_id,)
    )
    events = EventRepository(connection)
    for node_run_id, node_id, status in (
        ("plan", "plan_semantic_paragraphs", "completed"),
        ("upstream", "draft_rewrite", "completed"),
        ("prose", "redraft_body_prose", "completed"),
        ("approval", "human_approval", "waiting_human"),
    ):
        connection.execute(
            "INSERT INTO node_runs(id,run_id,node_id,attempt,status,input_json,"
            "resource_lane) VALUES(?,?,?,1,?,?,'codex_remote')",
            (node_run_id, run_id, node_id, status, handoff),
        )
        events.append(
            run_id, f"node.{status}", object_type="node_run",
            object_id=node_run_id, payload={"node_id": node_id},
        )
    connection.execute(
        "INSERT INTO human_reviews VALUES(?,?,?,?)",
        ("review", run_id, "open", "9999-01-01T00:00:00Z"),
    )
    connection.commit()

    result = RunRepository(connection).restart_authoring_from_node(
        run_id, entry_node=entry_node,
        downstream_node_ids={"draft_rewrite", "redraft_body_prose", "human_approval"},
        resource_lane="codex_remote",
    )
    assert result["deleted_node_run_count"] == (3 if entry_node == "draft_rewrite" else 2)
    assert result["deleted_record_counts"]["human_reviews"] == 1
    nodes = connection.execute(
        "SELECT node_id,status FROM node_runs WHERE run_id=? ORDER BY rowid", (run_id,)
    ).fetchall()
    expected = [("plan_semantic_paragraphs", "completed")]
    if entry_node == "redraft_body_prose":
        expected.append(("draft_rewrite", "completed"))
    expected.append((entry_node, "queued"))
    assert [(row["node_id"], row["status"]) for row in nodes] == expected
