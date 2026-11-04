from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from fukamiru.bootstrap import bootstrap
from fukamiru.config import load_settings
from fukamiru.errors import FukamiruError
from fukamiru.knowledge.corpus import CorpusSyncService
from fukamiru.knowledge.scalable_index import (
    IndexCapacityError,
    _check_capacity,
    iter_retrieval_groups,
)
from fukamiru.knowledge.semantic import FaissSemanticIndex
from fukamiru.knowledge.service import IngestionService, split_token_bounded
from fukamiru.storage.database import DatabasePair
from fukamiru.storage.runs import RunRepository


@pytest.fixture
def environment(tmp_path):
    config = tmp_path / "local.toml"
    config.write_text(
        "[paths]\n" + "\n".join(
            f'{key} = "{(tmp_path / value).as_posix()}"' for key, value in {
                "state_db": "state.db", "knowledge_db": "knowledge.db",
                "runtime_root": "runtime", "corpus_root": "corpus", "ocr_root": "ocr",
                "knowledge_artifacts_root": "kb", "faiss_root": "faiss",
            }.items()
        ) + '\n[retrieval]\nrequire_cuda=false\nembedding_device="cpu"\n'
        'tokenizer_mode="unicode"\nscalable_index_enabled=true\n', encoding="utf-8",
    )
    settings = load_settings(config)
    bootstrap(settings)
    return settings, DatabasePair.from_settings(settings)


def shared_character_offsets(text, *_args, **_kwargs):
    # A real subword tokenizer may emit two tokens for the same Unicode character.
    return [(i, i + 1) for i, char in enumerate(text)
            for _ in range(2 if char == "漢" else 1)], "shared-character-fixture"


def test_retokenized_slices_are_bounded_and_form_an_exact_character_partition():
    text = "ab漢cd漢ef"
    with patch("fukamiru.knowledge.service._token_offsets", shared_character_offsets):
        slices = split_token_bounded(
            text, "fixture", max_tokens=3, use_embedding=False, snapshot_path=None
        )
    assert "".join(piece.text for piece in slices) == text
    assert all(0 < piece.token_count <= 3 for piece in slices)
    assert slices[0].start_char == 0 and slices[-1].end_char == len(text)
    assert all(a.end_char == b.start_char for a, b in zip(slices, slices[1:], strict=False))


def test_low_memory_still_blocks_publication(tmp_path):
    with (
        patch("fukamiru.knowledge.scalable_index._available_memory_bytes", return_value=1),
        pytest.raises(IndexCapacityError, match="available RAM"),
    ):
        _check_capacity(tmp_path, 2, 1024)


def test_new_chunks_retokenize_after_cutting_shared_offsets(environment):
    settings, databases = environment
    settings = replace(settings, retrieval=replace(settings.retrieval, hard_max_tokens=3))
    source = settings.paths.corpus_root / "managed_sources" / "source.txt"
    source.write_text("ab漢cd漢ef", encoding="utf-8")
    with databases.knowledge.connect() as c:
        result = IngestionService(c, settings).ingest_path(str(source))
        with patch("fukamiru.knowledge.service._token_offsets", shared_character_offsets):
            IngestionService(c, settings).build_chunks(result["document_unit_id"])
        rows = c.execute("SELECT text,token_count FROM chunks ORDER BY ordinal").fetchall()
    assert "".join(row["text"] for row in rows) == source.read_text(encoding="utf-8")
    assert all(0 < row["token_count"] <= 3 for row in rows)


class TinyEmbeddingProvider:
    model_name = "index-repair-fixture"
    model_revision = "fixture-v1"
    actual_device = "fixture"

    def available(self):
        return True

    def encode(self, texts):
        return [[float("a" in text), float("f" in text), 0.1] for text in texts]


def test_document_sampling_is_read_only_and_excludes_other_documents(environment):
    settings, databases = environment
    units = []
    with databases.knowledge.connect() as c:
        for i in range(2):
            path = settings.paths.corpus_root / "managed_sources" / f"sample-{i}.txt"
            path.write_text(f"Document number {i} has different source text.")
            units.append(IngestionService(c, settings).ingest_path(str(path))["document_unit_id"])
        c.execute("PRAGMA query_only=ON")
        groups = list(iter_retrieval_groups(c, settings, document_unit_id=units[0]))
    assert groups and {group.document_unit_id for group in groups} == {units[0]}


def test_legacy_overlap_is_replaced_only_in_search_groups_and_searchable(environment):
    settings, databases = environment
    settings = replace(settings, retrieval=replace(
        settings.retrieval, hard_max_tokens=3, target_tokens=2, soft_min_tokens=1,
    ))
    source = settings.paths.corpus_root / "managed_sources" / "source.txt"
    source.write_text("a b c d e f", encoding="utf-8")
    with databases.knowledge.connect() as c:
        IngestionService(c, settings).ingest_path(str(source))
        c.execute("UPDATE chunks SET text='historical duplicate',token_count=1025")
        c.commit()
        canonical = [tuple(row) for row in c.execute("SELECT * FROM chunks ORDER BY ordinal")]
        groups = list(iter_retrieval_groups(c, settings))
    assert "".join(group.text for group in groups) == source.read_text(encoding="utf-8")
    assert len({group.anchor_chunk_id for group in groups}) == 1
    assert all(group.token_count <= 3 and group.source_ranges for group in groups)
    index = FaissSemanticIndex(
        databases.knowledge.connect, settings, provider=TinyEmbeddingProvider()
    )
    with databases.knowledge.connect() as c:
        c.execute("""INSERT INTO embedding_models(
        id,model_name,dimensions,backend,created_at) VALUES ('model',?,3,'faiss','now')""",
                  (index.model_storage_name,))
        c.commit()
    with patch("fukamiru.knowledge.scalable_index._check_capacity"):
        built = index.build()
    assert built["chunk_count"] == len(groups)
    hits = index.search("f", limit=2)
    assert len(hits) == 2 and len({hit["retrieval_group_key"] for hit in hits}) == 2
    assert all(hit["source_ranges"] for hit in hits)
    with databases.knowledge.connect() as c:
        rows = c.execute("SELECT * FROM chunks ORDER BY ordinal").fetchall()
        assert canonical == [tuple(row) for row in rows]
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
        # A published v1 mapping must survive the additive migration unchanged.
        c.execute("DELETE FROM retrieval_index_groups WHERE vector_key>0")
        c.execute("ALTER TABLE retrieval_index_groups RENAME TO saved_groups")
        migrations = Path(__file__).resolve().parents[1] / "migrations" / "knowledge"
        # The renamed table owns this index until it is removed.
        c.execute("DROP INDEX idx_retrieval_index_groups_chunk")
        c.executescript((migrations / "0023_scalable_retrieval_groups.sql").read_text())
        c.execute("""INSERT INTO retrieval_index_groups SELECT index_version_id,vector_key,
        chunk_id,document_unit_id,start_source_span_id,end_source_span_id,
        source_span_ids_json,text,token_count FROM saved_groups""")
        c.execute("DROP TABLE saved_groups")
        old = [tuple(row) for row in c.execute("SELECT * FROM retrieval_index_groups")]
        c.commit()
        c.executescript((migrations / "0024_retrieval_group_ranges.sql").read_text())
        assert old == [tuple(row)[:-1] for row in c.execute("SELECT * FROM retrieval_index_groups")]
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []


def make_batch(settings, databases):
    root = settings.paths.corpus_root / "managed_sources"
    source = root / "primary.txt"
    source.write_text("source provenance remains available", encoding="utf-8")
    (root / "alias.txt").write_bytes(source.read_bytes())
    with databases.state.connect() as c:
        run = RunRepository(c).create(
            workflow_name="corpus_sync", workflow_version="2", entry_node="scan_sources",
            project_id=None, inputs={},
        ).run_id
    service = CorpusSyncService(databases, settings)
    batch = service.scan(run, str(root))["corpus_batch_id"]
    with databases.knowledge.connect() as c:
        result = IngestionService(c, settings).ingest_path(str(source))
    with databases.state.connect() as c:
        primary = c.execute("SELECT id FROM corpus_sync_items WHERE source_path=?",
                            (str(source),)).fetchone()[0]
        alias = c.execute("SELECT id FROM corpus_sync_items WHERE source_path=?",
                          (str(root / "alias.txt"),)).fetchone()[0]
        c.execute("UPDATE corpus_sync_items SET status='ready',source_asset_id=?,"
                  "document_unit_id=? WHERE id=?",
                  (result["source_asset_id"], result["document_unit_id"], primary))
        c.execute("UPDATE corpus_sync_items SET status='ocr_required',error_json=? WHERE id=?",
                  (json.dumps({"ocr_job_id": "job", "ocr_status": "needs_language"}), alias))
        c.execute("""INSERT INTO pdf_ocr_jobs(id,run_id,corpus_item_id,source_path,source_sha256,
        source_language,engine,status,created_at,updated_at) VALUES
        ('job',?,?,?,?,'ja','ndlocr_lite','completed','now','now')""",
                  (run, primary, str(source), hashlib.sha256(source.read_bytes()).hexdigest()))
        c.commit()
    return service, run, batch, alias


def test_completed_shared_ocr_reuses_all_aliases_and_is_idempotent(environment):
    settings, databases = environment
    service, run, batch, alias = make_batch(settings, databases)
    assert service.reconcile_completed_ocr_items(run, batch) == 1
    assert service.reconcile_completed_ocr_items(run, batch) == 0
    with databases.state.connect() as c:
        row = c.execute("SELECT * FROM corpus_sync_items WHERE id=?", (alias,)).fetchone()
        assert row["status"] == "reused" and row["error_json"] is None
        assert c.execute("SELECT COUNT(*) FROM pdf_ocr_jobs").fetchone()[0] == 1
    with databases.knowledge.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM source_assets").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM source_locations").fetchone()[0] == 2


def test_changed_alias_is_not_falsely_marked_ready(environment):
    settings, databases = environment
    service, run, batch, alias = make_batch(settings, databases)
    (settings.paths.corpus_root / "managed_sources" / "alias.txt").write_text("changed")
    assert service.reconcile_completed_ocr_items(run, batch) == 0
    with databases.state.connect() as c:
        assert c.execute("SELECT status FROM corpus_sync_items WHERE id=?", (alias,)
                         ).fetchone()[0] == "ocr_required"


def test_replay_preserves_failed_attempt_and_rejects_duplicate_or_wrong_batch(environment):
    settings, databases = environment
    _, run, batch, _ = make_batch(settings, databases)
    with databases.state.connect() as c:
        c.execute("UPDATE node_runs SET status='completed' WHERE run_id=?", (run,))
        c.execute("""INSERT INTO node_runs(id,run_id,node_id,attempt,status,input_json)
        VALUES ('upstream',?,'ingest_ocr_outputs',1,'completed','{}')""", (run,))
        c.execute("""INSERT INTO node_runs(id,run_id,node_id,attempt,status,input_json,error_json)
        VALUES ('failure',?,'build_semantic_index',2,'failed',?,'{"message":"old failure"}')""",
                  (run, json.dumps({"corpus_batch_id": batch})))
        c.execute("UPDATE runs SET status='failed',error_json='{}' WHERE id=?", (run,))
        c.execute("UPDATE corpus_sync_batches SET status='failed' WHERE id=?", (batch,))
        c.commit()
        with pytest.raises(FukamiruError):
            RunRepository(c).replay_failed_corpus_index(run, "wrong-batch")
        replay = RunRepository(c).replay_failed_corpus_index(run, batch)
        assert replay["run_id"] == run and replay["attempt"] == 3
        old = c.execute("SELECT status,error_json FROM node_runs WHERE id='failure'").fetchone()
        assert old["status"] == "failed" and "old failure" in old["error_json"]
        with pytest.raises(FukamiruError):
            RunRepository(c).replay_failed_corpus_index(run, batch)
