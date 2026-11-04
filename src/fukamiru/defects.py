"""Canonical Defect persistence in state.db."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict
from typing import Any, Protocol

from fukamiru.errors import FukamiruError
from fukamiru.storage.runs import utc_now


class DefectLike(Protocol):
    defect_type: str
    severity: str
    page_start: int | None
    page_end: int | None
    excerpt: str
    context: str
    rationale: str
    repair_candidates: tuple[str, ...]
    confidence: float
    automatic_repair_allowed: bool


class DefectRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def record(
        self,
        *,
        run_id: str,
        finding: DefectLike,
        source_asset_id: str,
        node_run_id: str | None = None,
        source_span_id: str | None = None,
        object_type: str = "source_asset",
        object_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> str:
        defect_id = str(uuid.uuid4())
        payload = dict(details or {})
        try:
            payload.setdefault("finding", asdict(finding))
        except TypeError:
            payload.setdefault("finding", {"defect_type": finding.defect_type})
        self.connection.execute(
            """
            INSERT INTO defects(
                id, run_id, node_run_id, defect_type, severity, status,
                object_type, object_id, message, details_json, created_at,
                source_asset_id, source_span_id, page_start, page_end,
                excerpt, context_text, rationale, repair_candidates_json,
                confidence, automatic_repair_allowed
            ) VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                defect_id,
                run_id,
                node_run_id,
                finding.defect_type,
                finding.severity,
                object_type,
                object_id or source_asset_id,
                finding.rationale,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                utc_now(),
                source_asset_id,
                source_span_id,
                finding.page_start,
                finding.page_end,
                finding.excerpt,
                finding.context,
                finding.rationale,
                json.dumps(finding.repair_candidates, ensure_ascii=False),
                finding.confidence,
                int(finding.automatic_repair_allowed),
            ),
        )
        self.connection.commit()
        return defect_id

    def list_for_run(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM defects WHERE run_id = ? ORDER BY created_at, id", (run_id,)
        ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            item["repair_candidates"] = json.loads(item.pop("repair_candidates_json"))
            results.append(item)
        return results

    def list_open(self, *, run_id: str | None = None) -> list[dict[str, Any]]:
        if run_id:
            rows = self.connection.execute(
                """
                SELECT * FROM defects
                WHERE status = 'open' AND run_id = ? ORDER BY created_at, id
                """,
                (run_id,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM defects WHERE status = 'open' ORDER BY created_at, id"
            ).fetchall()
        return [self._decode(row) for row in rows]

    def list_open_summaries(
        self, *, run_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Return list-view fields without loading potentially large detail payloads."""
        parameters: tuple[str, ...] = ()
        run_filter = ""
        if run_id:
            run_filter = " AND run_id = ?"
            parameters = (run_id,)
        rows = self.connection.execute(
            f"""
            SELECT id, run_id, node_run_id, defect_type, severity, status,
                   object_type, object_id, message, created_at,
                   source_asset_id, source_span_id, page_start, page_end,
                   rationale, confidence, automatic_repair_allowed
            FROM defects
            WHERE status = 'open'{run_filter}
            ORDER BY created_at, id
            """,
            parameters,
        ).fetchall()
        return [dict(row) for row in rows]

    def get(self, defect_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM defects WHERE id = ?", (defect_id,)
        ).fetchone()
        if row is None:
            raise FukamiruError(f"Defect not found: {defect_id}")
        return self._decode(row)

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["details"] = json.loads(item.pop("details_json"))
        item["repair_candidates"] = json.loads(item.pop("repair_candidates_json"))
        return item
