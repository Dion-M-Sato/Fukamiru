from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from fukamiru.authoring.folder_input import FolderNoteService
from fukamiru.errors import FukamiruError


@pytest.fixture
def project(tmp_path: Path) -> tuple[FolderNoteService, Path]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE projects (id TEXT PRIMARY KEY, root_path TEXT, archived_at TEXT)"
    )
    connection.execute(
        "INSERT INTO projects (id, root_path) VALUES ('project', ?)", (str(tmp_path),)
    )
    yield FolderNoteService(connection), tmp_path
    connection.close()


def test_folder_note_follows_toc_and_keeps_body_separate(
    project: tuple[FolderNoteService, Path],
) -> None:
    service, root = project
    folder = root / "notes"
    folder.mkdir()
    (folder / "目次.md").write_text("# 目次\n1. 序論\n1.1 分析\n", encoding="utf-8")
    (folder / "序論.md").write_text("最初の主張。", encoding="utf-8")
    (folder / "分析.txt").write_text("次の根拠。", encoding="utf-8")
    (folder / "余分.txt").write_text("含めない。", encoding="utf-8")

    assert service.list_folders("project")["folders"] == ["notes"]
    result = service.build("project", "notes")
    assert [item["title"] for item in result["sections"]] == ["序論", "分析"]
    assert [item["level"] for item in result["sections"]] == [1, 2]
    assert result["note_text"].index("最初の主張。") < result["note_text"].index("次の根拠。")
    assert "余分.txt" in result["warnings"][0]
    assert "<!-- source: notes/序論.md -->" in result["note_text"].replace("\\", "/")
    output = root / service.save("project", result["note_text"])
    assert output.read_text(encoding="utf-8") == result["note_text"]


def test_folder_note_rejects_missing_ambiguous_and_outside_files(
    project: tuple[FolderNoteService, Path],
) -> None:
    service, root = project
    folder = root / "notes"
    folder.mkdir()
    (folder / "目次.md").write_text("# 目次\n- 序論\n", encoding="utf-8")
    with pytest.raises(FukamiruError, match="対応するファイルがありません"):
        service.build("project", "notes")
    (folder / "序論.md").write_text("内容", encoding="utf-8")
    (folder / "序論.txt").write_text("別内容", encoding="utf-8")
    with pytest.raises(FukamiruError, match="一意に特定できません"):
        service.build("project", "notes")
    with pytest.raises(FukamiruError, match="Project内"):
        service.build("project", "../outside")
