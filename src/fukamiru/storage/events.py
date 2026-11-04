"""Durable event log used for SSE replay; the database remains canonical."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class Event:
    event_id: int
    run_id: str
    event_type: str
    object_type: str | None
    object_id: str | None
    payload: dict[str, Any]
    created_at: str


class EventRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def append(
        self,
        run_id: str,
        event_type: str,
        *,
        object_type: str | None = None,
        object_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Event:
        created_at = utc_now()
        cursor = self.connection.execute(
            """
            INSERT INTO event_log(
                run_id, event_type, object_type, object_id, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                event_type,
                object_type,
                object_id,
                json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":")),
                created_at,
            ),
        )
        self.connection.commit()
        return Event(
            event_id=int(cursor.lastrowid),
            run_id=run_id,
            event_type=event_type,
            object_type=object_type,
            object_id=object_id,
            payload=payload or {},
            created_at=created_at,
        )

    def after(self, run_id: str, event_id: int, *, limit: int = 1000) -> list[Event]:
        rows = self.connection.execute(
            """
            SELECT event_id, run_id, event_type, object_type, object_id, payload_json, created_at
            FROM event_log
            WHERE run_id = ? AND event_id > ?
            ORDER BY event_id
            LIMIT ?
            """,
            (run_id, event_id, limit),
        ).fetchall()
        return [self._from_row(row) for row in rows]

    def recent(self, run_id: str, *, limit: int = 200) -> list[Event]:
        rows = self.connection.execute(
            """
            SELECT event_id, run_id, event_type, object_type, object_id, payload_json, created_at
            FROM event_log
            WHERE run_id = ?
            ORDER BY event_id DESC
            LIMIT ?
            """,
            (run_id, limit),
        ).fetchall()
        return [self._from_row(row) for row in reversed(rows)]

    def bounds(self, run_id: str) -> tuple[int | None, int | None]:
        row = self.connection.execute(
            """
            SELECT MIN(event_id) AS oldest, MAX(event_id) AS newest
            FROM event_log WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        return row["oldest"], row["newest"]

    def prune(self, retention_days: int, max_events_per_run: int) -> int:
        threshold = (
            (datetime.now(UTC) - timedelta(days=retention_days)).isoformat().replace("+00:00", "Z")
        )
        cursor = self.connection.execute(
            "DELETE FROM event_log WHERE created_at < ?",
            (threshold,),
        )
        deleted = cursor.rowcount
        run_ids = [
            row["run_id"] for row in self.connection.execute("SELECT id AS run_id FROM runs")
        ]
        for run_id in run_ids:
            cursor = self.connection.execute(
                """
                DELETE FROM event_log
                WHERE run_id = ? AND event_id NOT IN (
                    SELECT event_id FROM event_log
                    WHERE run_id = ? ORDER BY event_id DESC LIMIT ?
                )
                """,
                (run_id, run_id, max_events_per_run),
            )
            deleted += cursor.rowcount
        self.connection.commit()
        return deleted

    @staticmethod
    def _from_row(row: sqlite3.Row) -> Event:
        return Event(
            event_id=row["event_id"],
            run_id=row["run_id"],
            event_type=row["event_type"],
            object_type=row["object_type"],
            object_id=row["object_id"],
            payload=json.loads(row["payload_json"]),
            created_at=row["created_at"],
        )
