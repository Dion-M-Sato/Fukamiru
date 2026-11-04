"""Non-destructive external HTML preview and canonical human import."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from fukamiru.documents.canonical_html import CanonicalHTMLValidator
from fukamiru.errors import FukamiruError
from fukamiru.projects.git_checkpoint import GitCheckpointError, GitCheckpointService
from fukamiru.revisions.service import RevisionService, utc_now


class HTMLImportService:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.validator = CanonicalHTMLValidator()

    def preview(
        self, *, document_id: str, source_path: Path, max_bytes: int = 10 * 1024 * 1024
    ) -> dict[str, Any]:
        document = self.connection.execute(
            """
            SELECT d.id, d.project_id, p.root_path
            FROM content_documents d JOIN projects p ON p.id = d.project_id
            WHERE d.id = ?
            """,
            (document_id,),
        ).fetchone()
        if document is None:
            raise FukamiruError(f"Unknown document: {document_id}")
        if not source_path.is_absolute():
            raise FukamiruError("HTML import source must be an absolute path")
        source_path = source_path.resolve()
        if source_path.suffix.casefold() not in {".html", ".htm"}:
            raise FukamiruError("HTML import source must use .html or .htm")
        try:
            source_size = source_path.stat().st_size
        except OSError as exc:
            raise FukamiruError(f"HTML import source is unavailable: {exc}") from exc
        if source_size > max_bytes:
            raise FukamiruError("HTML import source exceeds the preview size limit")
        source_bytes = source_path.read_bytes()
        try:
            source_html = source_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FukamiruError("HTML import source must be UTF-8") from exc
        preview_id = str(uuid.uuid4())
        validation = self._validation(source_html)
        self.connection.execute(
            """
            INSERT INTO html_import_previews(
                id, project_id, document_id, source_path, source_sha256,
                original_html, proposed_html, validation_json, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                preview_id,
                document["project_id"],
                document_id,
                str(source_path),
                hashlib.sha256(source_bytes).hexdigest(),
                source_html,
                source_html,
                json.dumps(validation, ensure_ascii=False),
                utc_now(),
            ),
        )
        self.connection.commit()
        return self.get(preview_id)

    def update_proposed(self, preview_id: str, *, canonical_html: str) -> dict[str, Any]:
        preview = self._row(preview_id)
        if preview["status"] != "pending":
            raise FukamiruError("Only a pending HTML Import Preview can be edited")
        validation = self._validation(canonical_html)
        self.connection.execute(
            """
            UPDATE html_import_previews
            SET proposed_html = ?, validation_json = ? WHERE id = ?
            """,
            (canonical_html, json.dumps(validation, ensure_ascii=False), preview_id),
        )
        self.connection.commit()
        return self.get(preview_id)

    def confirm(self, preview_id: str) -> dict[str, Any]:
        preview = self._row(preview_id)
        if preview["status"] != "pending":
            raise FukamiruError("HTML Import Preview is no longer pending")
        canonical_html = str(preview["proposed_html"])
        self.validator.assert_valid(canonical_html)
        result = RevisionService(self.connection).commit_revision(
            document_id=str(preview["document_id"]),
            canonical_html=canonical_html,
            editor_type="human",
            operation="human_import",
            message=f"Import preview {preview_id}",
        )
        project = self.connection.execute(
            "SELECT root_path FROM projects WHERE id = ?", (preview["project_id"],)
        ).fetchone()
        assert project is not None
        project_root = Path(str(project["root_path"])).resolve()
        artifact = (
            project_root / "documents" / str(preview["document_id"]) / f"{result.revision_id}.html"
        )
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(canonical_html, encoding="utf-8")
        checkpoint_sha: str | None = None
        status = "imported"
        error_text: str | None = None
        try:
            checkpoint = GitCheckpointService(project_root).checkpoint(
                [artifact], f"Import canonical HTML revision {result.revision_id}"
            )
            checkpoint_sha = checkpoint.commit_sha if checkpoint else None
        except GitCheckpointError as exc:
            status = "checkpoint_failed"
            error_text = str(exc)
        self.connection.execute(
            """
            UPDATE html_import_previews
            SET status = ?, imported_revision_id = ?, artifact_path = ?,
                checkpoint_sha = ?, error_text = ?, resolved_at = ?
            WHERE id = ?
            """,
            (
                status,
                result.revision_id,
                str(artifact),
                checkpoint_sha,
                error_text,
                utc_now(),
                preview_id,
            ),
        )
        self.connection.commit()
        return self.get(preview_id)

    def get(self, preview_id: str) -> dict[str, Any]:
        result = dict(self._row(preview_id))
        result["validation"] = json.loads(result.pop("validation_json"))
        result["original_unchanged"] = self._source_unchanged(result)
        return result

    def _row(self, preview_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM html_import_previews WHERE id = ?", (preview_id,)
        ).fetchone()
        if row is None:
            raise FukamiruError(f"HTML Import Preview not found: {preview_id}")
        return row

    def _validation(self, canonical_html: str) -> dict[str, Any]:
        report = self.validator.validate(canonical_html)
        return {
            "valid": report.valid,
            "element_count": report.element_count,
            "block_count": report.block_count,
            "issues": [
                {
                    "code": issue.code,
                    "message": issue.message,
                    "severity": issue.severity,
                    "element_id": issue.element_id,
                }
                for issue in report.issues
            ],
        }

    @staticmethod
    def _source_unchanged(preview: dict[str, Any]) -> bool | None:
        source = Path(str(preview["source_path"]))
        try:
            return hashlib.sha256(source.read_bytes()).hexdigest() == preview["source_sha256"]
        except OSError:
            return None
