"""Prepare/apply a static-only excerpt UI, preserving the running app and diagrams."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "bibliography-1"
ASSETS = ("ui/src/knowledge-excerpts.js", "ui/src/knowledge-excerpts.css")
DIAGRAMS = (
    "docs/diagrams/knowledge-ingestion.workflow.json",
    "docs/diagrams/knowledge-ingestion.html",
)
TARGETS = (*ASSETS, *DIAGRAMS, "ui/index.html")


def digest(data: bytes | None) -> str | None:
    return hashlib.sha256(data).hexdigest() if data is not None else None


def checked_path(root: Path, relative: str) -> Path:
    if relative not in TARGETS:
        raise ValueError(f"Unexpected static target: {relative}")
    target = (root / relative).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError(f"Static target escapes root: {target}")
    return target


def flow_contract() -> dict:
    module = (ROOT / ASSETS[0]).read_text(encoding="utf-8")
    match = re.search(r"export const EXCERPT_REPORT_FLOW = (\[.*?\]);", module)
    if not match:
        raise ValueError("Excerpt UI flow is missing")
    keys = json.loads(match[1])
    labels = ["既存APIで検索", "資料ごとに整理", "登録書誌・未登録項目を表示",
              "原文と出典位置を表示", "クリックで右ペイン表示", "HTML保存"]
    return {"mode": "ui_only", "search_api": "GET /api/search", "inference": False,
            "storage": "tab_memory_latest_10", "backend_restart": False,
            "bibliography": "search_result.bibliography; missing fields explicit; no inference",
            "steps": [{"id": key, "label": label} for key, label in zip(keys, labels, strict=True)]}


def diagram_changes(source: bytes, document: bytes) -> tuple[bytes, bytes]:
    contract = flow_contract()
    source_data = json.loads(source)
    source_data["uiExcerptReport"] = contract
    html = document.decode("utf-8")
    section = (
        '<section id="knowledge-excerpts-flow" class="terminal" '
        'aria-label="UIだけで動く検索抜粋レポート"><h2>Knowledge Search · 検索抜粋レポート</h2>'
        '<p>既存APIで検索 → 資料ごとに整理 → 登録書誌・未登録項目を表示 → 原文と出典位置を表示 → '
        'クリックで右ペイン表示 → HTML保存</p><p>AI要約は行いません。'
        '書誌はcorpusの登録値を著者・タイトル・刊行情報・DOI/ISBNとともに表示し、'
        '未登録項目とファイル名からの仮登録を明記します。推測による補完は行いません。'
        '直近10件を開いているタブのメモリだけに保持し、再読み込みで消えるためHTMLで保存します。'
        'バックエンドの再起動・DB変更・Run操作は不要です。</p></section>'
    )
    marker = r'<section id="knowledge-excerpts-flow".*?</section>'
    if re.search(marker, html, flags=re.S):
        html = re.sub(marker, lambda _: section, html, flags=re.S)
    elif "<footer>" in html:
        html = html.replace("<footer>", section + "\n<footer>", 1)
    else:
        raise ValueError("RAG diagram footer is missing")
    match = re.search(r'(<script id="workflow-data" type="application/json">)(.*?)(</script>)',
                      html, flags=re.S)
    if not match:
        raise ValueError("RAG diagram data is missing")
    data = json.loads(match[2])
    data["workflows"]["corpus"]["uiExcerptReport"] = contract
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = html[:match.start(2)] + encoded + html[match.end(2):]
    serialized = json.dumps(source_data, ensure_ascii=False, indent=2) + "\n"
    return serialized.encode("utf-8"), html.encode("utf-8")


def patched_index(data: bytes) -> bytes:
    html = data.decode("utf-8")
    if "data-fukamiru-knowledge-excerpts" in html:
        html = re.sub(r'(/src/knowledge-excerpts\.(?:js|css)\?v=)[^"<>]+',
                      lambda match: match[1] + VERSION, html)
        return html.encode("utf-8")
    if "</head>" not in html or "</body>" not in html:
        raise ValueError("Workbench HTML is missing head/body")
    newline = "\r\n" if "\r\n" in html else "\n"
    css = f'  <link rel="stylesheet" href="/src/knowledge-excerpts.css?v={VERSION}">'
    script = (f'  <script type="module" data-fukamiru-knowledge-excerpts '
              f'src="/src/knowledge-excerpts.js?v={VERSION}"></script>')
    html = html.replace("</head>", css + newline + "</head>", 1)
    html = html.replace("</body>", script + newline + "</body>", 1)
    return html.encode("utf-8")


@dataclass(frozen=True)
class Change:
    relative: str
    before: bytes | None
    after: bytes


def plan_install(root: Path, *, diagram_only: bool = False) -> list[Change]:
    paths = DIAGRAMS if diagram_only else TARGETS
    before = {key: checked_path(root, key).read_bytes() if checked_path(root, key).exists()
              else None for key in paths}
    if any(before[key] is None for key in DIAGRAMS):
        raise ValueError("Existing RAG diagram files are required")
    source, html = diagram_changes(before[DIAGRAMS[0]], before[DIAGRAMS[1]])
    after = {DIAGRAMS[0]: source, DIAGRAMS[1]: html}
    if not diagram_only:
        if before["ui/index.html"] is None:
            raise ValueError("Existing Workbench index is required")
        after.update({key: (ROOT / key).read_bytes() for key in ASSETS})
        after["ui/index.html"] = patched_index(before["ui/index.html"])
    return [Change(key, before[key], after[key]) for key in paths if before[key] != after[key]]


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".excerpt-ui-", delete=False) as file:
        temporary = Path(file.name)
        file.write(data)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def apply_plan(root: Path, changes: list[Change], backup: Path) -> None:
    # Check every target before the first mutation. The index is always switched last.
    for change in changes:
        target = checked_path(root, change.relative)
        actual = target.read_bytes() if target.exists() else None
        if actual != change.before:
            raise ValueError(f"Static file changed after preparation: {change.relative}")
    backup.mkdir(parents=True, exist_ok=False)
    manifest = {"root": str(root.resolve()), "files": []}
    for change in changes:
        if change.before is not None:
            saved = backup / change.relative
            saved.parent.mkdir(parents=True, exist_ok=True)
            saved.write_bytes(change.before)
        manifest["files"].append({"path": change.relative,
                                  "before": digest(change.before), "after": digest(change.after)})
    (backup / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    applied = []
    try:
        for change in changes:
            target = checked_path(root, change.relative)
            actual = target.read_bytes() if target.exists() else None
            if actual != change.before:
                raise ValueError(f"Concurrent static change: {change.relative}")
            atomic_write(target, change.after)
            applied.append(change)
    except Exception:
        for change in reversed(applied):
            target = checked_path(root, change.relative)
            if digest(target.read_bytes()) != digest(change.after):
                raise RuntimeError(
                    f"Cannot restore a concurrently changed file: {target}"
                ) from None
            if change.before is None:
                target.unlink()
            else:
                atomic_write(target, change.before)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--diagram-only", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    changes = plan_install(root, diagram_only=args.diagram_only)
    if args.apply:
        if not args.backup:
            parser.error("--apply requires --backup")
        apply_plan(root, changes, args.backup.resolve())
    print(json.dumps({"applied": args.apply, "files": [change.relative for change in changes]},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
