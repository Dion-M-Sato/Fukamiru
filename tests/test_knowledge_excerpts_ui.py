from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from fukamiru.documents.canonical_html import CanonicalHTMLValidator
from scripts.install_knowledge_excerpts_ui import (
    DIAGRAMS,
    ROOT,
    VERSION,
    apply_plan,
    patched_index,
    plan_install,
)


def test_browser_excerpt_renderer_produces_valid_canonical_html() -> None:
    result = subprocess.run([
        "node", "--input-type=module", "-e",
        "import {createExcerptReport} from './ui/src/knowledge-excerpts.js';"
        "console.log(JSON.stringify(createExcerptReport('query <&>',["
        "{chunk_id:'a',source_asset_id:'book',title:'Book',text:'First <text>\\nSecond',"
        "bibliography:{id:'record',title:'Official <Book>',authors:['Writer & Co'],"
        "publisher:'Press',publication_date:'2026',volume:'1',issue:'2',pages:'3-4',"
        "doi:'10.1234/example',isbn:'9780000000000',url:'https://example.test/?a=1&b=2'},"
        "citation:{start_page:1,end_page:2}},"
        "{chunk_id:'b',source_asset_id:'book',title:'Chapter',text:'Other text'},"
        "{chunk_id:'c',source_asset_id:'partial',title:'File',text:'Partial',"
        "bibliography:{title:'File',metadata_source:'filename',metadata_status:'partial'}},"
        "{chunk_id:'d',source_asset_id:'missing',title:'Missing',text:'Missing'}])));",
    ], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=True)
    report = json.loads(result.stdout)
    assert CanonicalHTMLValidator().validate(report["canonical_html"]).valid
    assert report["groups"][0]["excerpts"][0]["text"] == "First <text>\nSecond"
    assert report["groups"][0]["bibliography"]["id"] == "record"
    assert "DOI：10.1234/example" in report["canonical_html"]


def fixture_root(tmp_path: Path) -> Path:
    root = tmp_path / "live"
    (root / "ui/src").mkdir(parents=True)
    (root / "ui/index.html").write_bytes(
        b'<html><head></head><body><script src="/src/app.js?v=existing"></script></body></html>'
    )
    (root / "ui/src/app.js").write_text("// existing human copy fixes", encoding="utf-8")
    (root / "docs/diagrams").mkdir(parents=True)
    for relative in DIAGRAMS:
        (root / relative).write_bytes((ROOT / relative).read_bytes())
    return root


def test_static_install_preserves_app_and_existing_index_and_is_idempotent(tmp_path) -> None:
    root = fixture_root(tmp_path)
    before = (root / "ui/index.html").read_bytes()
    plan = plan_install(root)
    assert plan[-1].relative == "ui/index.html"
    assert all(not change.relative.endswith("app.js") for change in plan)
    apply_plan(root, plan, tmp_path / "backup")
    assert (root / "ui/src/app.js").read_text() == "// existing human copy fixes"
    assert b'/src/app.js?v=existing' in (root / "ui/index.html").read_bytes()
    assert (tmp_path / "backup/ui/index.html").read_bytes() == before
    assert patched_index(patched_index(before)) == patched_index(before)
    previous = patched_index(before).replace(VERSION.encode(), b"previous-excerpts")
    assert patched_index(previous) == patched_index(before)
    assert plan_install(root) == []


def test_static_install_rejects_concurrent_edits_before_any_mutation(tmp_path) -> None:
    root = fixture_root(tmp_path)
    plan = plan_install(root)
    (root / "ui/index.html").write_text("concurrent human change", encoding="utf-8")
    with pytest.raises(ValueError, match="changed after preparation"):
        apply_plan(root, plan, tmp_path / "backup")
    assert not (root / "ui/src/knowledge-excerpts.js").exists()
    assert not (tmp_path / "backup").exists()


def test_failed_index_switch_restores_all_preceding_static_files(tmp_path, monkeypatch) -> None:
    from scripts import install_knowledge_excerpts_ui as installer

    root = fixture_root(tmp_path)
    plan = plan_install(root)
    original_write = installer.atomic_write

    def fail_index(path, data):
        if path.name == "index.html":
            raise OSError("fixture: index is locked")
        original_write(path, data)

    monkeypatch.setattr(installer, "atomic_write", fail_index)
    with pytest.raises(OSError, match="index is locked"):
        apply_plan(root, plan, tmp_path / "backup")
    for change in plan:
        path = root / change.relative
        assert (path.read_bytes() if path.exists() else None) == change.before
