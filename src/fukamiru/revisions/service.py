"""Revision persistence and protection invariants."""

from __future__ import annotations

import difflib
import hashlib
import sqlite3
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from fukamiru.documents.canonical_html import CanonicalHTMLValidator, structural_block_fragments
from fukamiru.errors import FukamiruError


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ProtectedBlockError(FukamiruError):
    """An agent attempted to change or remove a human-protected block."""


@dataclass(frozen=True)
class RevisionResult:
    revision_id: str
    content_sha256: str
    protected_block_ids: tuple[str, ...]


class RevisionService:
    def __init__(
        self,
        connection: sqlite3.Connection,
        validator: CanonicalHTMLValidator | None = None,
    ) -> None:
        self.connection = connection
        self.validator = validator or CanonicalHTMLValidator()

    def register_project(self, *, name: str, root_path: str, project_id: str | None = None) -> str:
        project_id = project_id or str(uuid.uuid4())
        now = utc_now()
        self.connection.execute(
            """
            INSERT INTO projects(id, name, root_path, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (project_id, name, root_path, now, now),
        )
        self.connection.commit()
        return project_id

    def create_document(
        self,
        *,
        project_id: str,
        kind: str,
        title: str,
        document_id: str | None = None,
    ) -> str:
        document_id = document_id or str(uuid.uuid4())
        now = utc_now()
        self.connection.execute(
            """
            INSERT INTO content_documents(id, project_id, kind, title, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (document_id, project_id, kind, title, now, now),
        )
        self.connection.commit()
        return document_id

    def commit_revision(
        self,
        *,
        document_id: str,
        canonical_html: str,
        editor_type: Literal["human", "agent", "system"],
        operation: str,
        message: str = "",
        parent_revision_id: str | None = None,
        revision_id: str | None = None,
        citation_events: Iterable[dict[str, Any]] = (),
        commit_transaction: bool = True,
    ) -> RevisionResult:
        self.validator.assert_valid(canonical_html)
        document = self.connection.execute(
            "SELECT current_revision_id FROM content_documents WHERE id = ?",
            (document_id,),
        ).fetchone()
        if document is None:
            raise FukamiruError(f"Unknown document: {document_id}")
        current_revision_id = document["current_revision_id"]
        if parent_revision_id is None:
            parent_revision_id = current_revision_id
        elif parent_revision_id != current_revision_id:
            raise FukamiruError(
                "parent_revision_id is stale; reload the current document revision"
            )

        fragments = structural_block_fragments(canonical_html)
        fragment_map = {block_id: fragment for block_id, _, fragment in fragments}
        existing = self._current_blocks(current_revision_id)
        protected = self._protected_blocks(document_id)
        if editor_type == "agent":
            changed = [
                block_id
                for block_id in protected
                if block_id not in fragment_map or existing.get(block_id) != fragment_map[block_id]
            ]
            if changed:
                raise ProtectedBlockError(
                    "Agent revision changes protected blocks: " + ", ".join(sorted(changed))
                )

        revision_id = revision_id or str(uuid.uuid4())
        now = utc_now()
        content_hash = sha256_text(canonical_html)
        human_protected: list[str] = []
        citation_event_list = [dict(item) for item in citation_events]
        if parent_revision_id:
            explicit_keys = {self._citation_event_key(event) for event in citation_event_list}
            explicit_targets = {
                self._citation_event_target(event) for event in citation_event_list
            }
            inherited = self.connection.execute(
                "SELECT * FROM citation_events WHERE revision_id = ?",
                (parent_revision_id,),
            ).fetchall()
            for event in inherited:
                block_id = str(event["block_id"] or "")
                if not block_id or existing.get(block_id) != fragment_map.get(block_id):
                    continue
                inherited_event = {
                    "block_id": block_id,
                    "source_span_id": str(event["source_span_id"]),
                    "bibliographic_record_id": event["bibliographic_record_id"],
                    "citation_kind": str(event["citation_kind"]),
                    "locator": event["locator"],
                    "rendered_text": event["rendered_text"],
                }
                key = self._citation_event_key(inherited_event)
                if key in explicit_keys or self._citation_event_target(
                    inherited_event
                ) in explicit_targets:
                    continue
                citation_event_list.append(inherited_event)
                explicit_keys.add(key)
        for event in citation_event_list:
            block_id = str(event.get("block_id") or "")
            source_span_id = str(event.get("source_span_id") or "")
            citation_kind = str(event.get("citation_kind") or "")
            if block_id not in fragment_map:
                raise FukamiruError("Citation event targets a missing final block: " + block_id)
            if not source_span_id:
                raise FukamiruError("Citation event requires a source span ID")
            if citation_kind not in {"quote", "paraphrase", "reference"}:
                raise FukamiruError("Citation event has an unknown citation kind")
        try:
            if not self.connection.in_transaction:
                self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute(
                """
                INSERT INTO revisions(
                    id, document_id, parent_revision_id, editor_type, operation,
                    content_sha256, message, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    revision_id,
                    document_id,
                    parent_revision_id,
                    editor_type,
                    operation,
                    content_hash,
                    message,
                    now,
                ),
            )
            self.connection.execute(
                """
                INSERT INTO revision_documents(revision_id, canonical_html, content_sha256)
                VALUES (?, ?, ?)
                """,
                (revision_id, canonical_html, content_hash),
            )
            for ordinal, (block_id, block_kind, fragment) in enumerate(fragments):
                protect = editor_type == "human" and (
                    operation == "human_import" or existing.get(block_id) != fragment
                )
                self.connection.execute(
                    """
                    INSERT INTO content_blocks(id, document_id, block_kind, protected, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        protected = MAX(content_blocks.protected, excluded.protected)
                    """,
                    (block_id, document_id, block_kind, int(protect), now),
                )
                if protect:
                    human_protected.append(block_id)
                self.connection.execute(
                    """
                    INSERT INTO revision_blocks(
                        revision_id, block_id, ordinal, html, content_sha256
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (revision_id, block_id, ordinal, fragment, sha256_text(fragment)),
                )
            for event in citation_event_list:
                draft_id = str(event.get("id") or "")
                event_id = (
                    "citation-event:"
                    + hashlib.sha256(
                        "\x1f".join(
                            (
                                revision_id,
                                draft_id,
                                *self._citation_event_key(event),
                            )
                        ).encode()
                    ).hexdigest()[:24]
                )
                self.connection.execute(
                    """
                    INSERT INTO citation_events(
                        id, revision_id, block_id, source_span_id,
                        bibliographic_record_id, citation_kind, locator,
                        rendered_text, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        revision_id,
                        str(event["block_id"]),
                        str(event["source_span_id"]),
                        event.get("bibliographic_record_id"),
                        str(event["citation_kind"]),
                        event.get("locator"),
                        event.get("rendered_text"),
                        now,
                    ),
                )
            self.connection.execute(
                """
                UPDATE content_documents
                SET current_revision_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (revision_id, now, document_id),
            )
            if commit_transaction:
                self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return RevisionResult(revision_id, content_hash, tuple(human_protected))

    @staticmethod
    def _citation_event_key(event: dict[str, Any]) -> tuple[str, ...]:
        return (
            str(event.get("block_id") or ""),
            str(event.get("source_span_id") or ""),
            str(event.get("bibliographic_record_id") or ""),
            str(event.get("citation_kind") or ""),
            str(event.get("locator") or ""),
        )

    @staticmethod
    def _citation_event_target(event: dict[str, Any]) -> tuple[str, str]:
        return (
            str(event.get("block_id") or ""),
            str(event.get("source_span_id") or ""),
        )

    def canonical_html(self, revision_id: str) -> str:
        row = self.connection.execute(
            "SELECT canonical_html FROM revision_documents WHERE revision_id = ?",
            (revision_id,),
        ).fetchone()
        if row is None:
            raise FukamiruError(f"Unknown revision: {revision_id}")
        return str(row["canonical_html"])

    def history(self, document_id: str) -> list[dict[str, Any]]:
        document = self.connection.execute(
            "SELECT id FROM content_documents WHERE id = ?", (document_id,)
        ).fetchone()
        if document is None:
            raise FukamiruError(f"Unknown document: {document_id}")
        return [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT r.id, r.document_id, r.parent_revision_id, r.editor_type,
                       r.operation, r.content_sha256, r.message, r.created_at,
                       COUNT(rb.block_id) AS block_count
                FROM revisions r
                LEFT JOIN revision_blocks rb ON rb.revision_id = r.id
                WHERE r.document_id = ?
                GROUP BY r.id
                ORDER BY r.created_at DESC, r.id DESC
                """,
                (document_id,),
            )
        ]

    def diff(self, revision_id: str) -> dict[str, Any]:
        revision = self.connection.execute(
            """
            SELECT id, document_id, parent_revision_id, editor_type,
                   operation, message, created_at
            FROM revisions WHERE id = ?
            """,
            (revision_id,),
        ).fetchone()
        if revision is None:
            raise FukamiruError(f"Unknown revision: {revision_id}")
        after = self.canonical_html(revision_id)
        parent_id = revision["parent_revision_id"]
        before = self.canonical_html(str(parent_id)) if parent_id else ""
        unified = "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=str(parent_id or "/dev/null"),
                tofile=revision_id,
            )
        )
        result = dict(revision)
        result["before_html"] = before
        result["after_html"] = after
        result["unified_diff"] = unified
        return result

    def _current_blocks(self, revision_id: str | None) -> dict[str, str]:
        if revision_id is None:
            return {}
        return {
            row["block_id"]: row["html"]
            for row in self.connection.execute(
                "SELECT block_id, html FROM revision_blocks WHERE revision_id = ?",
                (revision_id,),
            )
        }

    def _protected_blocks(self, document_id: str) -> set[str]:
        return {
            row["id"]
            for row in self.connection.execute(
                "SELECT id FROM content_blocks WHERE document_id = ? AND protected = 1",
                (document_id,),
            )
        }
