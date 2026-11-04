"""Immutable, content-addressed node payloads; legacy inline JSON remains readable."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from typing import Any


class PayloadStore:
    marker = "$fukamiru_payload_sha256"

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def pack(self, context: dict[str, Any]) -> dict[str, Any]:
        packed = {}
        for key, value in context.items():
            raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if len(raw.encode()) < 8192:
                packed[key] = value
                continue
            digest = hashlib.sha256(raw.encode()).hexdigest()
            self.connection.execute(
                "INSERT OR IGNORE INTO workflow_payloads VALUES (?,?,?)",
                (digest, raw, datetime.now(UTC).isoformat()),
            )
            packed[key] = {self.marker: digest}
        return packed

    def unpack(self, context: Any) -> Any:
        if not isinstance(context, dict):
            return context
        unpacked = {}
        for key, value in context.items():
            if isinstance(value, dict) and set(value) == {self.marker}:
                row = self.connection.execute(
                    "SELECT payload_json FROM workflow_payloads WHERE sha256=?",
                    (value[self.marker],),
                ).fetchone()
                if row is None or hashlib.sha256(row[0].encode()).hexdigest() != value[self.marker]:
                    raise ValueError(f"Missing or corrupt workflow payload: {value[self.marker]}")
                value = json.loads(row[0])
            unpacked[key] = value
        return unpacked


class UnitCheckpoints:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def load(self, run_id: str, phase: str, signature: str) -> dict[str, Any]:
        return {
            row[0]: json.loads(row[1])
            for row in self.connection.execute(
                "SELECT unit_id,payload_json FROM manuscript_unit_checkpoints "
                "WHERE run_id=? AND phase=? AND signature=?",
                (run_id, phase, signature),
            )
        }

    def save(self, run_id: str, phase: str, signature: str, records: dict[str, Any]) -> None:
        self.connection.executemany(
            "INSERT OR IGNORE INTO manuscript_unit_checkpoints VALUES (?,?,?,?,?)",
            [
                (run_id, phase, signature, key, json.dumps(value, ensure_ascii=False))
                for key, value in records.items()
            ],
        )
        self.connection.commit()
