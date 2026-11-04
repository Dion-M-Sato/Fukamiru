from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fukamiru.storage.database import SQLiteDatabase
from fukamiru.storage.events import EventRepository
from fukamiru.storage.runs import RunRepository


class EventReplayTests(unittest.TestCase):
    def test_events_replay_after_cursor_and_snapshot_remains_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            migrations = Path(__file__).resolve().parents[1] / "migrations" / "state"
            database = SQLiteDatabase(Path(directory) / "state.db", migrations)
            database.migrate()
            with database.connect() as connection:
                runs = RunRepository(connection)
                created = runs.create(
                    workflow_name="translation",
                    workflow_version="1",
                    entry_node="ensure_ingested",
                    project_id=None,
                    inputs={"source_path": "D:/example.pdf"},
                )
                events = EventRepository(connection)
                second = events.append(
                    created.run_id,
                    "node.started",
                    object_type="node",
                    object_id="ensure_ingested",
                )
                replay = events.after(created.run_id, created.event_id)
                self.assertEqual(
                    [event.event_type for event in replay],
                    ["node.queued", "node.started"],
                )
                self.assertEqual(replay[-1].event_id, second.event_id)
                snapshot = runs.snapshot(created.run_id)
                self.assertEqual(snapshot["run"]["status"], "queued")
                self.assertEqual(snapshot["active_nodes"][0]["node_id"], "ensure_ingested")
                self.assertEqual(snapshot["latest_event_id"], second.event_id)

    def test_run_progress_projects_current_patch_node_and_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            migrations = Path(__file__).resolve().parents[1] / "migrations" / "state"
            database = SQLiteDatabase(Path(directory) / "state.db", migrations)
            database.migrate()
            with database.connect() as connection:
                runs = RunRepository(connection)
                created = runs.create(
                    workflow_name="authoring",
                    workflow_version="30",
                    entry_node="generate_proofreading_candidates",
                    project_id=None,
                    inputs={},
                    resource_lane="local_llm_gpu",
                )
                connection.execute(
                    "UPDATE runs SET status='running',started_at='2026-09-29T00:00:00Z' "
                    "WHERE id=?",
                    (created.run_id,),
                )
                connection.execute(
                    "UPDATE node_runs SET status='running',started_at='2026-09-29T00:00:00Z' "
                    "WHERE run_id=?",
                    (created.run_id,),
                )
                connection.commit()
                EventRepository(connection).append(
                    created.run_id,
                    "manuscript.proofreading.generation.progress",
                    object_type="node_run",
                    object_id=None,
                    payload={"completed": 2, "total": 5},
                )

                running = runs.list_progress(limit=1)[0]
                self.assertEqual(
                    running["current_nodes"][0]["node_id"],
                    "generate_proofreading_candidates",
                )
                self.assertEqual(running["patch_processing"]["state"], "running")
                self.assertEqual(
                    running["patch_processing"]["stage"], "candidates_generating"
                )
                self.assertFalse(running["patch_processing"]["empty"])

                connection.execute(
                    "UPDATE runs SET status='failed',error_json='{}' WHERE id=?",
                    (created.run_id,),
                )
                connection.execute(
                    "UPDATE node_runs SET status='failed',completed_at='2026-09-29T00:01:00Z' "
                    "WHERE run_id=?",
                    (created.run_id,),
                )
                connection.commit()

                failed = runs.progress(created.run_id)
                self.assertEqual(failed["current_nodes"][0]["status"], "failed")
                self.assertEqual(failed["patch_processing"]["state"], "failed")

                connection.execute(
                    "UPDATE runs SET status='completed',completed_at='2026-09-29T00:02:00Z' "
                    "WHERE id=?",
                    (created.run_id,),
                )
                connection.execute(
                    "UPDATE node_runs SET status='completed' WHERE run_id=?",
                    (created.run_id,),
                )
                connection.commit()

                completed = runs.progress(created.run_id)
                self.assertEqual(completed["patch_processing"]["state"], "completed")
                self.assertTrue(completed["patch_processing"]["empty"])


if __name__ == "__main__":
    unittest.main()
