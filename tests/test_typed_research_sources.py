from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from fukamiru.revisions.reasoning import ManuscriptReasoningRepository
from fukamiru.storage.database import SQLiteDatabase


def test_typed_research_source_migration_preserves_records_and_verification(tmp_path):
    migrations = Path(__file__).resolve().parents[1] / "migrations/state"
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    for migration in migrations.glob("*.sql"):
        if migration.name < "0054_":
            shutil.copy2(migration, legacy / migration.name)
    old = SQLiteDatabase(tmp_path / "state.db", legacy)
    old.migrate()
    with old.connect() as connection:
        connection.executescript("""
            INSERT INTO projects(id,name,root_path,created_at,updated_at)
                VALUES('p','P','/project','now','now');
            INSERT INTO content_documents(id,project_id,kind,title,created_at,updated_at)
                VALUES('d','p','paper','Paper','now','now');
            INSERT INTO runs(id,workflow_name,workflow_version,status,input_json,created_at)
                VALUES('r','manuscript_generator','32','running','{}','now');
            INSERT INTO manuscript_reasoning_reviews(
                id,run_id,document_id,assessment_json,status,created_at,updated_at)
                VALUES('review','r','d','{}','completed','now','now');
            INSERT INTO manuscript_research_evidence(
                id,run_id,review_id,query,source_type,provider,provider_record_id,
                excerpt,confidence,provenance_json,created_at)
                VALUES('original','r','review','query','local_rag','fixture','original',
                'Original text',0.7,'{"verification_status":"unverified"}','now');
        """)
        before = dict(connection.execute(
            "SELECT * FROM manuscript_research_evidence WHERE id='original'"
        ).fetchone())
    current = SQLiteDatabase(tmp_path / "state.db", migrations)
    assert current.migrate() == ["0054_typed_manuscript_research_sources",
                                 "0055_manuscript_proofreading_generations"]
    assert current.migrate() == []
    with current.connect() as connection:
        after = dict(connection.execute(
            "SELECT * FROM manuscript_research_evidence WHERE id='original'"
        ).fetchone())
        assert after == before
        repository = ManuscriptReasoningRepository(connection)
        record = {"run_id": "r", "review_id": "review", "query": "query",
                  "provider": "fixture", "confidence": 0.5, "excerpt": "",
                  "provenance": {"source_level": "bibliographic_metadata",
                                 "substantive_support_verified": False}}
        for kind in ("online_paper", "online_book", "online_primary", "online_bibliography"):
            evidence_id = repository.save_evidence({
                **record, "source_type": kind, "provider_record_id": kind,
            })
            row = connection.execute("SELECT * FROM manuscript_research_evidence WHERE id=?",
                                     (evidence_id,)).fetchone()
            assert row["source_type"] == kind
            assert json.loads(row["provenance_json"])["substantive_support_verified"] is False
        with pytest.raises(sqlite3.IntegrityError):
            repository.save_evidence({**record, "source_type": "human_review",
                                      "provider_record_id": "human"})
        with pytest.raises(sqlite3.IntegrityError):
            repository.save_evidence({**record, "source_type": "online_book", "confidence": 2,
                                      "provider_record_id": "invalid-confidence"})
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
