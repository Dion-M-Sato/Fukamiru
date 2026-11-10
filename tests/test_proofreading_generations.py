import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from fukamiru.errors import FukamiruError
from fukamiru.revisions.sentence_proofreading import digest, extract_sentences
from fukamiru.revisions.service import RevisionService
from fukamiru.storage.database import SQLiteDatabase
from fukamiru.storage.runs import RunRepository
from fukamiru.worker import sentence_proofreading as worker
from fukamiru.worker.runner import WorkerRunner

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations/state"
OLD_HTML = '<article id="d" lang="ja"><p id="p">旧文を検討する。</p></article>'
NEW_HTML = '<article id="d" lang="ja"><p id="p">新文を検討する。</p></article>'
TABLES = (
    "manuscript_proofreading_sentences",
    "manuscript_proofreading_patches",
    "manuscript_proofreading_title_registry",
    "manuscript_proofreading_logs",
)


def _database(tmp_path, *, legacy=False):
    directory = MIGRATIONS
    if legacy:
        directory = tmp_path / "legacy"
        directory.mkdir()
        for source in MIGRATIONS.glob("*.sql"):
            if source.name < "0055_":
                shutil.copy2(source, directory / source.name)
    db = SQLiteDatabase(tmp_path / "state.db", directory)
    db.migrate()
    with db.connect() as c:
        revisions = RevisionService(c)
        project = revisions.register_project(name="Test", root_path=str(tmp_path))
        document = revisions.create_document(project_id=project, kind="paper", title="Test")
        run = RunRepository(c).create(
            workflow_name="manuscript_generator",
            workflow_version="32",
            entry_node="extract_proofreading_sentences",
            inputs={},
            project_id=project,
        )
        node = c.execute("SELECT id FROM node_runs WHERE run_id=?", (run.run_id,)).fetchone()[0]
    claimed = SimpleNamespace(
        run_id=run.run_id,
        node_run_id=node,
        workflow_name="manuscript_generator",
        workflow_version="32",
    )
    stub = SimpleNamespace(
        databases=SimpleNamespace(state=db),
        _string_list=lambda x: x or [],
        _raise_if_run_cancelled=lambda *args: None,
        execute_local_llm=lambda operation, **kw: operation(),
        _manuscript_predicate_inventory_record=lambda x: x,
        _authoring_reader_facing_v31_findings=WorkerRunner._authoring_reader_facing_v31_findings,
        llm_jp=SimpleNamespace(
            model_identity="test",
            complete_json=lambda **kw: {"candidate": "新文を詳しく検討する。"},
        ),
        settings=SimpleNamespace(
            translation=SimpleNamespace(manuscript_polish_max_output_tokens=300)
        ),
    )
    return db, claimed, stub, {"document_id": document, "proposed_html": OLD_HTML}


def _patch(c, run, sentence, *, patch_id, text, generation="legacy"):
    columns = (
        "id,run_id,sentence_id,candidate_ordinal,original_text,raw_candidate_text,"
        "normalized_candidate_text,original_hash,candidate_hash,generation_seed,"
        "generation_model,generation_prompt_version,status,selected,applied,apply_allowed,"
        "luna_result_json,created_at,updated_at"
    )
    values = [
        patch_id,
        run,
        sentence["id"],
        1,
        sentence["original_text"],
        text,
        text,
        sentence["original_hash"],
        digest(text),
        1,
        "test",
        "test",
        "clean",
        1,
        1,
        1,
        json.dumps({"label": "clean", "confidence": "high"}),
        "now",
        "now",
    ]
    if "generation_id" in {
        row["name"] for row in c.execute("PRAGMA table_info(manuscript_proofreading_patches)")
    }:
        columns += ",generation_id"
        values.append(generation)
    c.execute(
        f"INSERT INTO manuscript_proofreading_patches ({columns}) "
        f"VALUES ({','.join('?' for _ in values)})",
        values,
    )


def test_migration_preserves_old_proofreading_records_and_foreign_keys(tmp_path):
    db, claimed, _, context = _database(tmp_path, legacy=True)
    sentence = extract_sentences(
        OLD_HTML, run_id=claimed.run_id, document_id=context["document_id"]
    )[0]
    with db.connect() as c:
        c.execute(
            "INSERT INTO manuscript_proofreading_sentences "
            "(id,run_id,document_id,source_sentence_id,block_id,sentence_ordinal,start_offset,"
            "end_offset,original_text,original_hash,status,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                sentence[k]
                for k in (
                    "id",
                    "run_id",
                    "document_id",
                    "source_sentence_id",
                    "block_id",
                    "sentence_ordinal",
                    "start_offset",
                    "end_offset",
                    "original_text",
                    "original_hash",
                )
            ]
            + ["generated", "now"],
        )
        _patch(c, claimed.run_id, sentence, patch_id="old", text="旧文を詳しく検討する。")
        c.execute(
            "INSERT INTO manuscript_proofreading_title_registry VALUES (?,?,?,?,?)",
            (claimed.run_id, "旧作", "{}", "high", "now"),
        )
        c.execute(
            "INSERT INTO manuscript_proofreading_logs "
            "(id,run_id,patch_id,sentence_id,stage,status,version,created_at) "
            "VALUES (?,?,?,?,'selection','selected','test','now')",
            ("old-log", claimed.run_id, "old", sentence["id"]),
        )
        c.commit()
        before = {
            table: [dict(row) for row in c.execute(f"SELECT * FROM {table}")] for table in TABLES
        }
    current = SQLiteDatabase(tmp_path / "state.db", MIGRATIONS)
    assert current.migrate() == ["0055_manuscript_proofreading_generations"]
    with current.connect() as c:
        for table, rows in before.items():
            after = [dict(row) for row in c.execute(f"SELECT * FROM {table}")]
            assert len(after) == len(rows)
            for old, new in zip(rows, after, strict=True):
                assert {key: new[key] for key in old} == old
                if table != "manuscript_proofreading_logs":
                    assert new["generation_id"] == "legacy"
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []


def test_new_manuscript_generation_is_isolated_and_retry_is_idempotent(tmp_path, monkeypatch):
    db, claimed, stub, context = _database(tmp_path)
    worker.prepare_sentences(stub, claimed, context)
    with db.connect() as c:
        sentence = dict(c.execute("SELECT * FROM manuscript_proofreading_sentences").fetchone())
        _patch(c, claimed.run_id, sentence, patch_id="old", text="旧文を詳しく検討する。")
        c.commit()
        old = dict(
            c.execute("SELECT * FROM manuscript_proofreading_patches WHERE id='old'").fetchone()
        )
    current = {
        **context,
        "use_llm_jp": True,
        "proposed_html": NEW_HTML,
        "research_problem": {},
        "draft_evidence_identity_generation": {"producer_node_run_id": "new-draft"},
    }
    current = worker.prepare_sentences(stub, claimed, current)
    repeated = worker.prepare_sentences(stub, claimed, current)
    assert repeated["_proofreading_generation_id"] == current["_proofreading_generation_id"]
    generated = worker.generate_candidates(stub, claimed, current)
    assert generated["manuscript_proofreading_generation"]["sentence_count"] == 1
    assert generated["manuscript_proofreading_generation"]["generation_call_count"] == 3
    with db.connect() as c:
        assert (
            c.execute("SELECT COUNT(*) FROM manuscript_proofreading_sentences").fetchone()[0] == 2
        )
        patch = dict(
            c.execute("SELECT * FROM manuscript_proofreading_patches WHERE id!='old'").fetchone()
        )
        c.execute(
            "UPDATE manuscript_proofreading_patches SET status='clean',apply_allowed=1,"
            "luna_result_json=? WHERE id=?",
            (json.dumps({"label": "clean", "confidence": "high"}), patch["id"]),
        )
    selected = worker.select_candidates(
        stub,
        claimed,
        {
            **generated,
            "_proofreading_ranked_candidate_ids": {patch["sentence_id"]: patch["id"]},
            "auto_apply_japanese_proofreading": True,
        },
    )
    monkeypatch.setattr(
        worker.ManuscriptDiscourseService, "predicate_candidates", lambda *a, **kw: []
    )
    published = worker.publish_shadow(stub, claimed, selected)
    assert published["manuscript_proofreading_shadow"]["sentence_count"] == 1
    assert published["manuscript_proofreading_shadow"]["candidate_count"] == 1
    assert published["manuscript_proofreading_shadow"]["applied_count"] == 1
    with db.connect() as c:
        assert (
            dict(
                c.execute("SELECT * FROM manuscript_proofreading_patches WHERE id='old'").fetchone()
            )
            == old
        )
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    with pytest.raises(FukamiruError, match="proofreading.source_checkpoint_changed"):
        worker.prepare_sentences(
            stub, claimed, {**current, "proposed_html": NEW_HTML.replace("新文", "別文")}
        )


def test_title_protection_uses_only_the_current_manuscript_generation(tmp_path, monkeypatch):
    db, claimed, stub, context = _database(tmp_path)
    worker.prepare_sentences(stub, claimed, context)
    current = worker.prepare_sentences(
        stub,
        claimed,
        {
            **context,
            "use_llm_jp": True,
            "research_problem": {},
            "proposed_html": NEW_HTML,
            "draft_evidence_identity_generation": {"producer_node_run_id": "new-draft"},
        },
    )
    generated = worker.generate_candidates(stub, claimed, current)
    with db.connect() as c:
        for title, generation in [
            ("旧作", "legacy"),
            ("新作", current["_proofreading_generation_id"]),
        ]:
            c.execute(
                "INSERT INTO manuscript_proofreading_title_registry "
                "(run_id,generation_id,title,evidence_json,confidence,created_at) "
                "VALUES (?,?,?,'{}','high','now')",
                (claimed.run_id, generation, title),
            )
    original = worker.rules.preprocess_candidate
    seen = []

    def capture(source, candidate, registry, *args, **kwargs):
        seen.append(set(registry))
        return original(source, candidate, registry, *args, **kwargs)

    monkeypatch.setattr(worker.rules, "preprocess_candidate", capture)
    result = worker.preprocess(stub, claimed, generated)
    assert seen == [{"新作"}]
    assert result["_proofreading_generation_id"] == current["_proofreading_generation_id"]
