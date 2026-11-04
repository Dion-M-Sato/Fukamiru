"""Create and register a project at an explicit user-selected path."""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fukamiru.errors import FukamiruError
from fukamiru.projects.git_checkpoint import GitCheckpointService


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _windows_filesystem(path: Path) -> str | None:
    if os.name != "nt":
        return None
    volume_root = path.anchor
    if not volume_root:
        return None
    filesystem = ctypes.create_unicode_buffer(256)
    result = ctypes.windll.kernel32.GetVolumeInformationW(  # type: ignore[attr-defined]
        ctypes.c_wchar_p(volume_root),
        None,
        0,
        None,
        None,
        None,
        filesystem,
        len(filesystem),
    )
    return filesystem.value if result else None


class ProjectService:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def list_projects(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        where = "" if include_archived else "WHERE archived_at IS NULL"
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT id, name, root_path, git_enabled, archived_at, created_at, updated_at "
                f"FROM projects {where} ORDER BY updated_at DESC"
            )
        ]

    def preflight(self, *, root_path: Path) -> dict[str, Any]:
        if not root_path.is_absolute():
            raise FukamiruError("Project root must be an absolute path")
        root_path = root_path.resolve()
        existing = self.connection.execute(
            "SELECT id, archived_at FROM projects WHERE root_path = ?",
            (str(root_path),),
        ).fetchone()
        if existing:
            if existing["archived_at"]:
                raise FukamiruError(
                    "This project path is registered but archived; restore the existing "
                    "project instead of creating a duplicate"
                )
            raise FukamiruError("This project path is already registered")

        filesystem = _windows_filesystem(root_path)
        if filesystem and filesystem.casefold() != "ntfs":
            raise FukamiruError(
                f"Project Git repositories require NTFS; selected filesystem is {filesystem}"
            )

        if root_path.exists() and not root_path.is_dir():
            raise FukamiruError("Project root must be a directory")
        manifest = root_path / "fukamiru.project.toml"
        if manifest.exists():
            raise FukamiruError("A Fukamiru project manifest already exists at this path")
        if (root_path / ".git").exists():
            raise FukamiruError(
                "The selected path is already a Git repository; importing an existing "
                "repository is not supported by Project registration yet"
            )

        probe_directory = root_path if root_path.exists() else root_path.parent
        while not probe_directory.exists() and probe_directory != probe_directory.parent:
            probe_directory = probe_directory.parent
        if not probe_directory.is_dir():
            raise FukamiruError(
                f"Project root has no writable parent directory: {root_path}"
            )
        self._probe_write(probe_directory, requested_root=root_path)
        existing_entries = sum(1 for _ in root_path.iterdir()) if root_path.exists() else 0
        return {
            "root_path": str(root_path),
            "filesystem": filesystem,
            "writable": True,
            "root_exists": root_path.exists(),
            "existing_entries": existing_entries,
            "will_create_root": not root_path.exists(),
            "will_initialize_git": True,
        }

    @staticmethod
    def _probe_write(directory: Path, *, requested_root: Path) -> None:
        probe_path = directory / f".fukamiru-write-probe-{uuid.uuid4().hex}"
        descriptor: int | None = None
        try:
            descriptor = os.open(
                probe_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except OSError as exc:
            raise FukamiruError(
                "選択したProjectルートに書き込めません: "
                f"{requested_root}。Fukamiruサービスをこの場所へ書き込める通常の"
                "Windowsセッションから起動するか、書き込み権限のある場所を選択してください。"
            ) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
        try:
            probe_path.unlink()
        except OSError as exc:
            raise FukamiruError(
                "Projectルートの書き込み確認ファイルを削除できませんでした: "
                f"{probe_path}"
            ) from exc

    def create(self, *, name: str, root_path: Path) -> dict[str, Any]:
        assessment = self.preflight(root_path=root_path)
        root_path = Path(str(assessment["root_path"]))
        project_id = str(uuid.uuid4())
        manifest = root_path / "fukamiru.project.toml"
        ignore = root_path / ".gitignore"
        git_directory = root_path / ".git"
        created_root = not root_path.exists()
        created_directories: list[Path] = []
        created_files: list[Path] = []
        git_created = False

        try:
            root_path.mkdir(parents=True, exist_ok=True)
            for directory_name in ("documents", "imports"):
                directory = root_path / directory_name
                if not directory.exists():
                    directory.mkdir()
                    created_directories.append(directory)
                elif not directory.is_dir():
                    raise FukamiruError(
                        f"Project entry must be a directory: {directory}"
                    )

            with manifest.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(
                    "\n".join(
                        (
                            'schema_version = "1"',
                            f"project_id = {json.dumps(project_id)}",
                            f"name = {json.dumps(name, ensure_ascii=False)}",
                            "",
                        )
                    )
                )
            created_files.append(manifest)

            checkpoint_paths = [manifest]
            if not ignore.exists():
                with ignore.open("x", encoding="utf-8", newline="\n") as stream:
                    stream.write(
                        "\n".join(
                            (
                                "*.db",
                                "*.db-shm",
                                "*.db-wal",
                                "*.pdf",
                                "logs/",
                                "vector_index/",
                                "",
                            )
                        )
                    )
                created_files.append(ignore)
                checkpoint_paths.append(ignore)

            git = GitCheckpointService(root_path)
            git.initialize()
            git_created = git_directory.is_dir()
            git.checkpoint(checkpoint_paths, "Initialize Fukamiru project")

            now = utc_now()
            self.connection.execute(
                """
                INSERT INTO projects(
                    id, name, root_path, git_enabled, archived_at, created_at, updated_at
                ) VALUES (?, ?, ?, 1, NULL, ?, ?)
                """,
                (project_id, name, str(root_path), now, now),
            )
            self.connection.commit()
        except Exception as exc:
            self.connection.rollback()
            cleanup_failures = self._rollback_filesystem(
                root_path=root_path,
                created_root=created_root,
                created_directories=created_directories,
                created_files=created_files,
                git_created=git_created or git_directory.is_dir(),
            )
            if cleanup_failures:
                raise FukamiruError(
                    "Project登録に失敗し、自動クリーンアップも完了しませんでした: "
                    + "; ".join(cleanup_failures)
                ) from exc
            if isinstance(exc, FukamiruError):
                raise
            if isinstance(exc, OSError):
                raise FukamiruError(
                    f"Projectを作成できませんでした: {root_path} ({exc})"
                ) from exc
            if isinstance(exc, sqlite3.Error):
                raise FukamiruError(
                    "Projectのファイル作成後にDB登録できなかったため、作成内容を"
                    "ロールバックしました"
                ) from exc
            raise

        return {
            "id": project_id,
            "name": name,
            "root_path": str(root_path),
            "git_enabled": True,
            "archived_at": None,
            "created_at": now,
            "updated_at": now,
        }

    @staticmethod
    def _rollback_filesystem(
        *,
        root_path: Path,
        created_root: bool,
        created_directories: list[Path],
        created_files: list[Path],
        git_created: bool,
    ) -> tuple[str, ...]:
        failures: list[str] = []
        git_directory = root_path / ".git"
        if git_created and git_directory.is_dir() and git_directory.parent == root_path:
            try:
                shutil.rmtree(git_directory)
            except OSError as exc:
                failures.append(f"{git_directory}: {exc}")
        for file_path in reversed(created_files):
            if file_path.parent == root_path and file_path.exists():
                try:
                    file_path.unlink()
                except OSError as exc:
                    failures.append(f"{file_path}: {exc}")
        for directory in reversed(created_directories):
            if directory.parent == root_path and directory.exists():
                try:
                    directory.rmdir()
                except OSError as exc:
                    failures.append(f"{directory}: {exc}")
        if created_root and root_path.exists():
            try:
                root_path.rmdir()
            except OSError as exc:
                failures.append(f"{root_path}: {exc}")
        return tuple(failures)

    def archive(self, project_id: str) -> dict[str, Any]:
        project = self._project(project_id)
        if project["archived_at"] is not None:
            return {
                **project,
                "status": "archived",
                "filesystem_preserved": True,
                "data_preserved": True,
            }
        active_runs = int(
            self.connection.execute(
                """
                SELECT COUNT(*) FROM runs
                WHERE project_id = ? AND status IN ('queued', 'running', 'waiting_human')
                """,
                (project_id,),
            ).fetchone()[0]
        )
        if active_runs:
            raise FukamiruError(
                "実行中または確認待ちのRunがあるため、このProjectは登録解除できません"
            )
        now = utc_now()
        self.connection.execute(
            "UPDATE projects SET archived_at = ?, updated_at = ? WHERE id = ?",
            (now, now, project_id),
        )
        self.connection.commit()
        return {
            **project,
            "archived_at": now,
            "updated_at": now,
            "status": "archived",
            "filesystem_preserved": True,
            "data_preserved": True,
        }

    def restore(self, project_id: str) -> dict[str, Any]:
        project = self._project(project_id)
        if project["archived_at"] is None:
            return {
                **project,
                "status": "active",
                "filesystem_preserved": True,
                "data_preserved": True,
            }
        now = utc_now()
        self.connection.execute(
            "UPDATE projects SET archived_at = NULL, updated_at = ? WHERE id = ?",
            (now, project_id),
        )
        self.connection.commit()
        return {
            **project,
            "archived_at": None,
            "updated_at": now,
            "status": "active",
            "filesystem_preserved": True,
            "data_preserved": True,
        }

    def _project(self, project_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            """
            SELECT id, name, root_path, git_enabled, archived_at, created_at, updated_at
            FROM projects WHERE id = ?
            """,
            (project_id,),
        ).fetchone()
        if row is None:
            raise FukamiruError(f"Project not found: {project_id}")
        return dict(row)
