"""Build one authoring note from a Project folder and its table of contents."""

from __future__ import annotations

import os
import re
import sqlite3
import unicodedata
import uuid
from pathlib import Path
from typing import Any

from fukamiru.errors import FukamiruError
from fukamiru.revisions.manuscript_import import (
    ExtractedBlock,
    ManuscriptImportService,
    _decode_text,
)

_TOC_NAMES = {"目次", "もくじ", "toc", "contents", "tableofcontents"}
_TOC_HEADING = {"目次", "もくじ", "tableofcontents", "contents"}
_MAX_SECTIONS = 100
_MAX_NOTE_CHARS = 2_000_000


def _key(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold().strip()
    value = re.sub(
        r"^(?:第\s*[0-9一二三四五六七八九十]+\s*[章節項]\s*|"
        r"[0-9]{1,3}(?:\.[0-9]{1,3})*(?:[.)、．]\s*|\s+))",
        "",
        value,
    )
    return re.sub(r"[\s_\-・:：、。．.]+", "", value)


def _toc_entries(lines: list[str]) -> list[tuple[int, str]]:
    entries: list[tuple[int, str]] = []
    in_fence = False
    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence or not stripped or stripped in {"---", "***"}:
            continue
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", stripped)
        bullet = re.match(r"^[-*+]\s+(.+)$", stripped)
        numbered = re.match(
            r"^(\d{1,3}(?:\.\d{1,3})*)(?:[.)、．])?\s+(.+)$", stripped
        )
        if heading:
            level, title = len(heading.group(1)), heading.group(2)
        elif numbered:
            level, title = len(numbered.group(1).split(".")), numbered.group(2)
        elif bullet:
            level = 1 + (len(line) - len(line.lstrip(" "))) // 2
            title = bullet.group(1)
        else:
            level, title = 1, stripped
        link = re.fullmatch(r"\[([^]]+)\]\([^)]+\)", title.strip())
        if link:
            title = link.group(1)
        title = re.sub(r"\s*(?:\.{2,}|…{2,})\s*\d+\s*$", "", title).strip()
        if not title or _key(title) in _TOC_HEADING:
            continue
        entries.append((min(level, 6), title))
    if not entries:
        raise FukamiruError("目次に項目がありません")
    if len(entries) > _MAX_SECTIONS:
        raise FukamiruError(f"目次は{_MAX_SECTIONS}項目以内にしてください")
    return entries


def _blocks_to_markdown(blocks: tuple[ExtractedBlock, ...]) -> str:
    rendered: list[str] = []
    for block in blocks:
        if block.kind == "heading":
            rendered.append(f"{'#' * min(block.level + 2, 6)} {block.text}")
        elif block.kind == "list":
            rendered.append("\n".join(f"- {item}" for item in block.items))
        elif block.kind == "blockquote":
            rendered.append("\n".join(f"> {line}" for line in block.text.splitlines()))
        elif block.kind == "code":
            rendered.append(f"```\n{block.text}\n```")
        elif block.text.strip():
            rendered.append(block.text.strip())
    return "\n\n".join(rendered).strip()


class FolderNoteService:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.importer = ManuscriptImportService(connection)

    def _project_root(self, project_id: str) -> Path:
        row = self.connection.execute(
            "SELECT root_path FROM projects WHERE id=? AND archived_at IS NULL",
            (project_id,),
        ).fetchone()
        if row is None:
            raise FukamiruError(f"Active Project not found: {project_id}")
        root = Path(str(row["root_path"])).resolve()
        if not root.is_dir():
            raise FukamiruError(f"Project root is unavailable: {root}")
        return root

    @staticmethod
    def _inside(root: Path, path: Path) -> Path:
        resolved = path.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise FukamiruError("対象のフォルダはProject内に指定してください") from exc
        return resolved

    def list_folders(self, project_id: str) -> dict[str, Any]:
        root = self._project_root(project_id)
        folders: list[str] = []
        for directory, names, files in os.walk(root, followlinks=False):
            names[:] = [
                name for name in names
                if name not in {".git", "node_modules", "authoring_inputs"}
            ]
            if any(self._is_toc(Path(name)) for name in files):
                folder = self._inside(root, Path(directory))
                folders.append(str(folder.relative_to(root)) or ".")
            if len(folders) > 500:
                raise FukamiruError("目次を含むフォルダが多すぎます")
        return {"folders": sorted(folders, key=str.casefold)}

    @staticmethod
    def _is_toc(path: Path) -> bool:
        return (
            path.suffix.casefold() in ManuscriptImportService.SUPPORTED_SUFFIXES
            and _key(path.stem) in _TOC_NAMES
        )

    def build(self, project_id: str, folder_path: str) -> dict[str, Any]:
        root = self._project_root(project_id)
        requested = Path(folder_path or ".")
        if requested.is_absolute():
            raise FukamiruError("Project内の相対フォルダを選択してください")
        folder = self._inside(root, root / requested)
        if not folder.is_dir():
            raise FukamiruError("対象のフォルダが見つかりません")
        files = sorted(
            (self._inside(root, path) for path in folder.iterdir() if path.is_file()),
            key=lambda path: path.name.casefold(),
        )
        toc_files = [path for path in files if self._is_toc(path)]
        if len(toc_files) != 1:
            raise FukamiruError("目次ファイル（目次 / toc / contents）を1つ用意してください")
        toc = toc_files[0]
        _, toc_format, _, _, extracted_toc = self.importer._read_source(
            toc, max_bytes=10 * 1024 * 1024, require_absolute=True
        )
        if toc_format in {"txt", "markdown"}:
            toc_text, _ = _decode_text(toc.read_bytes())
            toc_lines = toc_text.splitlines()
        else:
            toc_lines = []
            for block in extracted_toc.blocks:
                toc_lines.extend(block.items if block.kind == "list" else block.text.splitlines())
        entries = _toc_entries(toc_lines)
        candidates: dict[str, list[Path]] = {}
        for path in files:
            if path == toc or path.suffix.casefold() not in self.importer.SUPPORTED_SUFFIXES:
                continue
            candidates.setdefault(_key(path.stem), []).append(path)
        sections: list[dict[str, Any]] = []
        used: set[Path] = set()
        warnings: list[str] = []
        for level, title in entries:
            matches = candidates.get(_key(title), [])
            if not matches:
                raise FukamiruError(f"目次「{title}」に対応するファイルがありません")
            if len(matches) > 1 or matches[0] in used:
                raise FukamiruError(f"目次「{title}」に対応するファイルを一意に特定できません")
            source = matches[0]
            used.add(source)
            _, source_format, _, _, extraction = self.importer._read_source(
                source, max_bytes=100 * 1024 * 1024, require_absolute=True
            )
            if source_format in {"txt", "markdown"}:
                content, _ = _decode_text(source.read_bytes())
                content = content.strip()
            else:
                content = _blocks_to_markdown(extraction.blocks)
            if not content:
                raise FukamiruError(f"本文ファイルが空です: {source.name}")
            warnings.extend(f"{source.name}: {item}" for item in extraction.warnings)
            sections.append({
                "title": title,
                "level": level,
                "source_path": str(source.relative_to(root)),
                "content": content,
            })
        unused = [path.name for paths in candidates.values() for path in paths if path not in used]
        if unused:
            warnings.append("目次にないファイルは含めません: " + ", ".join(sorted(unused)))
        toc_part = "\n".join(
            f"{'  ' * (section['level'] - 1)}- {section['title']}" for section in sections
        )
        body = "\n\n".join(
            f"{'#' * min(section['level'] + 1, 6)} {section['title']}\n\n"
            f"<!-- source: {section['source_path']} -->\n\n{section['content']}"
            for section in sections
        )
        note_text = f"# 目次\n\n{toc_part}\n\n# 本文\n\n{body}\n"
        if len(note_text) > _MAX_NOTE_CHARS:
            raise FukamiruError("統合後のノートが大きすぎます")
        return {
            "folder_path": str(folder.relative_to(root)) or ".",
            "toc_path": str(toc.relative_to(root)),
            "sections": sections,
            "warnings": warnings,
            "note_text": note_text,
        }

    def save(self, project_id: str, note_text: str) -> str:
        root = self._project_root(project_id)
        output_dir = self._inside(root, root / "authoring_inputs")
        output_dir.mkdir(exist_ok=True)
        output = output_dir / f"{uuid.uuid4().hex}.md"
        with output.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(note_text)
        return str(output.relative_to(root))
