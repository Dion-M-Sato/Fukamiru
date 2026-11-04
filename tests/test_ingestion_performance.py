from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from fukamiru.bootstrap import bootstrap
from fukamiru.config import load_settings
from fukamiru.knowledge.service import IngestionService, _token_offsets
from fukamiru.storage.database import DatabasePair


@pytest.fixture
def ingestion(tmp_path: Path):
    paths = {
        "state_db": tmp_path / "state.db",
        "knowledge_db": tmp_path / "knowledge.db",
        "runtime_root": tmp_path / "runtime",
        "corpus_root": tmp_path / "corpus",
        "ocr_root": tmp_path / "ocr",
        "knowledge_artifacts_root": tmp_path / "artifacts",
        "faiss_root": tmp_path / "faiss",
    }
    config = tmp_path / "local.toml"
    config.write_text(
        "[paths]\n" + "\n".join(f'{key} = "{value.as_posix()}"' for key, value in paths.items())
        + '\n[retrieval]\ntokenizer_mode = "unicode"\nrequire_cuda = false\n',
        encoding="utf-8",
    )
    settings = load_settings(config)
    bootstrap(settings)
    databases = DatabasePair.from_settings(settings)
    with databases.knowledge.connect() as connection:
        service = IngestionService(connection, settings)
        source = tmp_path / "source.txt"
        source.write_text(
            "First evidence paragraph.\n\nSecond evidence paragraph.", encoding="utf-8"
        )
        result = service.ingest_path(str(source), source_language="en")
        yield service, databases, result


def test_other_connection_can_write_during_chunk_tokenization(ingestion) -> None:
    service, databases, result = ingestion
    checks = 0

    def tokenize(text, model_name, **kwargs):
        nonlocal checks
        assert not service.connection.in_transaction
        with databases.knowledge.connect(busy_timeout_ms=20) as writer:
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("UPDATE source_assets SET updated_at=updated_at WHERE id=?",
                           (result["source_asset_id"],))
        checks += 1
        return _token_offsets(text, model_name, **kwargs)

    with patch("fukamiru.knowledge.service._token_offsets", side_effect=tokenize):
        service.build_chunks(result["document_unit_id"])
    assert checks > 0


def test_chunk_insert_failure_restores_existing_document_chunks(ingestion) -> None:
    service, _, result = ingestion
    connection = service.connection
    previous = [tuple(row) for row in connection.execute(
        "SELECT id,text,token_count FROM chunks WHERE document_unit_id=? ORDER BY ordinal",
        (result["document_unit_id"],),
    )]
    connection.execute("""
        CREATE TRIGGER reject_chunk BEFORE INSERT ON chunks
        BEGIN SELECT RAISE(ABORT, 'forced chunk failure'); END
    """)
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError, match="forced chunk failure"):
        service.build_chunks(result["document_unit_id"])
    current = [tuple(row) for row in connection.execute(
        "SELECT id,text,token_count FROM chunks WHERE document_unit_id=? ORDER BY ordinal",
        (result["document_unit_id"],),
    )]
    assert current == previous


@pytest.mark.parametrize(("table", "column"), [
    ("chunk_index_records", "chunk_id"),
    ("ocr_text_revisions", "parent_revision_id"),
    ("ocr_page_boundaries", "revision_page_id"),
    ("ocr_page_boundaries", "block_id"),
    ("ocr_notes", "body_block_id"),
    ("ocr_note_references", "block_id"),
    ("ocr_note_references", "note_id"),
    ("ocr_provenance_links", "source_revision_page_id"),
    ("ocr_provenance_links", "descendant_block_id"),
])
def test_cascading_references_use_index_lookups(ingestion, table: str, column: str) -> None:
    service, _, _ = ingestion
    # Identifiers come only from the fixed list above.
    plans = service.connection.execute(
        f"EXPLAIN QUERY PLAN SELECT rowid FROM {table} WHERE {column}=?", ("absent",)
    ).fetchall()
    assert all("SCAN " not in str(row[3]) for row in plans)
    assert any("SEARCH " in str(row[3]) for row in plans)


def test_lexical_mapping_backfills_legacy_rows_without_changing_search(ingestion) -> None:
    service, _, _ = ingestion
    connection = service.connection
    connection.execute(
        "INSERT INTO chunk_fts(rowid,chunk_id,document_unit_id,source_asset_id,text) "
        "VALUES (1000,'retired-chunk','retired-document','legacy-source','Legacy evidence text')"
    )
    before = [tuple(row) for row in connection.execute(
        "SELECT rowid,chunk_id,text FROM chunk_fts WHERE chunk_fts MATCH 'evidence' ORDER BY rowid"
    )]
    connection.execute("DROP TABLE chunk_fts_sources")
    connection.executescript((
        Path(__file__).resolve().parents[1] / "migrations/knowledge/0022_lexical_source_rowids.sql"
    ).read_text(encoding="utf-8"))
    assert [tuple(row) for row in connection.execute(
        "SELECT rowid,chunk_id,text FROM chunk_fts WHERE chunk_fts MATCH 'evidence' ORDER BY rowid"
    )] == before
    assert [tuple(row) for row in connection.execute(
        "SELECT fts_rowid,source_asset_id FROM chunk_fts_sources ORDER BY fts_rowid"
    )] == [tuple(row) for row in connection.execute(
        "SELECT rowid,source_asset_id FROM chunk_fts ORDER BY rowid"
    )]


def test_lexical_replacement_preserves_other_sources_and_mapping(ingestion) -> None:
    service, _, result = ingestion
    other_source = service.settings.paths.runtime_root.parent / "other-source.txt"
    other_source.write_text("Unrelated evidence remains searchable.", encoding="utf-8")
    other = service.ingest_path(str(other_source), source_language="en")
    connection = service.connection
    before = [tuple(row) for row in connection.execute(
        "SELECT rowid,chunk_id,text FROM chunk_fts WHERE source_asset_id=? ORDER BY rowid",
        (other["source_asset_id"],),
    )]
    queries: list[str] = []
    connection.set_trace_callback(queries.append)
    try:
        for _ in range(3):
            service.index(result["source_asset_id"], result["document_unit_id"])
    finally:
        connection.set_trace_callback(None)
    assert not any(
        "delete from chunk_fts where source_asset_id" in sql.casefold() for sql in queries
    )
    assert any("delete from chunk_fts where rowid" in sql.casefold() for sql in queries)
    assert [tuple(row) for row in connection.execute(
        "SELECT rowid,chunk_id,text FROM chunk_fts WHERE source_asset_id=? ORDER BY rowid",
        (other["source_asset_id"],),
    )] == before
    assert [tuple(row) for row in connection.execute(
        "SELECT fts_rowid,source_asset_id FROM chunk_fts_sources ORDER BY fts_rowid"
    )] == [tuple(row) for row in connection.execute(
        "SELECT rowid,source_asset_id FROM chunk_fts ORDER BY rowid"
    )]
    assert connection.execute(
        "SELECT COUNT(*) FROM chunk_fts WHERE chunk_fts MATCH 'Unrelated'"
    ).fetchone()[0] > 0
    plans = connection.execute(
        "EXPLAIN QUERY PLAN SELECT fts_rowid FROM chunk_fts_sources WHERE source_asset_id=?",
        (result["source_asset_id"],),
    ).fetchall()
    assert all("SEARCH " in str(row[3]) for row in plans)
    point_plan = connection.execute(
        "EXPLAIN QUERY PLAN SELECT rowid FROM chunk_fts WHERE rowid=?", (1,)
    ).fetchone()[3]
    assert "VIRTUAL TABLE INDEX" in point_plan and "=" in point_plan


@pytest.mark.parametrize("failure_stage", ["mapping", "ready"])
def test_lexical_failure_restores_old_search_rows_and_mapping(ingestion, failure_stage) -> None:
    service, _, result = ingestion
    connection = service.connection
    before_fts = [tuple(row) for row in connection.execute("SELECT rowid,* FROM chunk_fts")]
    before_map = [tuple(row) for row in connection.execute("SELECT * FROM chunk_fts_sources")]
    if failure_stage == "mapping":
        connection.execute("""
            CREATE TRIGGER reject_lexical BEFORE INSERT ON chunk_fts_sources
            BEGIN SELECT RAISE(ABORT, 'forced lexical failure'); END
        """)
    else:
        connection.execute("""
            CREATE TRIGGER reject_lexical BEFORE UPDATE OF ingestion_status ON source_assets
            BEGIN SELECT RAISE(ABORT, 'forced lexical failure'); END
        """)
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError, match="forced lexical failure"):
        service.index(result["source_asset_id"], result["document_unit_id"])
    assert [tuple(row) for row in connection.execute("SELECT rowid,* FROM chunk_fts")] == before_fts
    assert [tuple(row) for row in connection.execute(
        "SELECT * FROM chunk_fts_sources"
    )] == before_map
