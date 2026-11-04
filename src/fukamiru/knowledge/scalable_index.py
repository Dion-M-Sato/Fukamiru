"""Bounded-memory, resumable semantic index for large Corpus Sync runs.

Canonical Chunks and SourceSpans are not rewritten. Search-only groups keep
their provenance in Knowledge DB; normalized FP32 vectors are streamed to the
configured vector drive and a compact FAISS index is published atomically.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import math
import os
import shutil
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from fukamiru.config import Settings
from fukamiru.errors import EmptyEmbeddingOffsetsError, FukamiruError
from fukamiru.knowledge.service import (
    _token_offsets,
    preflight_embedding_tokenizer,
    split_token_bounded,
    utc_now,
)


class IndexBuildGuardError(FukamiruError):
    """A safe, searchable index cannot yet be published."""


class IndexCapacityError(IndexBuildGuardError):
    """Resource preflight could not reserve a safe indexing work set."""


class IndexQualityError(IndexBuildGuardError):
    """Candidate search did not meet the exact-search comparison threshold."""


@dataclass(frozen=True)
class RetrievalGroup:
    anchor_chunk_id: str
    document_unit_id: str
    start_source_span_id: str
    end_source_span_id: str
    source_span_ids: tuple[str, ...]
    text: str
    token_count: int
    source_ranges: tuple[tuple[str, int, int], ...] = ()


def _available_memory_bytes() -> int | None:
    if os.name == "nt":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong),
                ("memory_load", ctypes.c_ulong),
                ("total_physical", ctypes.c_ulonglong),
                ("available_physical", ctypes.c_ulonglong),
                ("total_page_file", ctypes.c_ulonglong),
                ("available_page_file", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong),
                ("available_virtual", ctypes.c_ulonglong),
                ("available_extended_virtual", ctypes.c_ulonglong),
            ]

        state = MemoryStatus()
        state.length = ctypes.sizeof(state)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(state)):
            return int(state.available_physical)
    if Path("/proc/meminfo").is_file():
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    return None


def _piece_tokens(text: str, settings: Settings, snapshot: str | None) -> int:
    offsets, _ = _token_offsets(
        text,
        settings.retrieval.embedding_model,
        use_embedding=settings.retrieval.tokenizer_mode == "embedding",
        snapshot_path=snapshot,
    )
    return len(offsets)


def iter_retrieval_groups(
    connection: sqlite3.Connection, settings: Settings, *, document_unit_id: str | None = None
) -> Iterator[RetrievalGroup]:
    """Pack contiguous source text within each DocumentUnit, without overlap."""
    snapshot = (
        preflight_embedding_tokenizer(settings)
        if settings.retrieval.tokenizer_mode == "embedding"
        else None
    )
    target = settings.retrieval.target_tokens
    hard_max = settings.retrieval.hard_max_tokens
    cursor = connection.execute(
        f"""
        SELECT c.id AS chunk_id, c.document_unit_id,
               s.id AS span_id, s.text AS span_text
        FROM chunks c
        JOIN source_spans s ON s.id = c.primary_source_span_id
        JOIN document_units d ON d.id = c.document_unit_id
        JOIN source_locations l
          ON l.source_asset_id = d.source_asset_id
         AND l.is_primary = 1 AND l.is_available = 1
        {"WHERE c.document_unit_id = ?" if document_unit_id is not None else ""}
        ORDER BY c.document_unit_id, c.ordinal
        """,  # noqa: S608 - fixed optional predicate with a bound value
        (document_unit_id,) if document_unit_id is not None else (),
    )
    parts: list[str] = []
    spans: list[str] = []
    document_id: str | None = None
    anchor_id: str | None = None
    tokens = 0
    ranges: list[tuple[str, int, int]] = []
    previous_span: str | None = None

    def emit() -> RetrievalGroup | None:
        if not parts or document_id is None or anchor_id is None:
            return None
        return RetrievalGroup(
            anchor_chunk_id=anchor_id,
            document_unit_id=document_id,
            start_source_span_id=spans[0],
            end_source_span_id=spans[-1],
            source_span_ids=tuple(dict.fromkeys(spans)),
            text="\n\n".join(parts),
            token_count=tokens,
            source_ranges=tuple(ranges),
        )

    try:
        for row in cursor:
            next_document = str(row["document_unit_id"])
            span_id = str(row["span_id"])
            # Historical retrieval slices can overlap or exceed the limit. Read
            # the canonical SourceSpan once and build a fresh exact partition.
            if span_id == previous_span:
                continue
            previous_span = span_id
            source_text = str(row["span_text"])
            if not source_text.strip():
                continue
            try:
                pieces = split_token_bounded(
                    source_text,
                    settings.retrieval.embedding_model,
                    max_tokens=hard_max,
                    use_embedding=settings.retrieval.tokenizer_mode == "embedding",
                    snapshot_path=snapshot,
                )
            except EmptyEmbeddingOffsetsError:
                import unicodedata

                # Tokenizers discard PDF control bytes as well as format-only
                # characters. Retain the canonical span without indexing it.
                if all(char.isspace() or unicodedata.category(char) in {"Cc", "Cf"}
                       for char in source_text):
                    continue
                raise IndexBuildGuardError(
                    f"Nonempty SourceSpan has no tokenizer offsets at Chunk {row['chunk_id']}"
                ) from None
            for piece in pieces:
                combined_tokens = (
                    _piece_tokens("\n\n".join([*parts, piece.text]), settings, snapshot)
                    if parts and next_document == document_id else piece.token_count
                )
                if parts and (next_document != document_id or combined_tokens > target):
                    group = emit()
                    if group is not None:
                        yield group
                    parts, spans, ranges, tokens, anchor_id = [], [], [], 0, None
                    combined_tokens = piece.token_count
                document_id = next_document
                if anchor_id is None:
                    anchor_id = str(row["chunk_id"])
                parts.append(piece.text)
                spans.append(span_id)
                ranges.append((span_id, piece.start_char, piece.end_char))
                tokens = combined_tokens
        group = emit()
        if group is not None:
            yield group
    finally:
        cursor.close()


def _group_signature(
    connection: sqlite3.Connection,
    settings: Settings,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[str, int]:
    digest = hashlib.sha256(b"retrieval-groups-v2\0")
    total = 0
    last_report = 0.0
    for group in iter_retrieval_groups(connection, settings):
        digest.update(group.anchor_chunk_id.encode("ascii"))
        digest.update(hashlib.sha256(group.text.encode("utf-8")).digest())
        digest.update(json.dumps(group.source_ranges).encode("utf-8"))
        total += 1
        if progress is not None and time.monotonic() - last_report >= 5:
            progress({"phase": "preflight_retrieval_groups", "checked_groups": total,
                      "embedded_count": 0, "total_count": 0})
            last_report = time.monotonic()
    return digest.hexdigest(), total


def _check_capacity(root: Path, total: int, dimensions: int) -> None:
    root.mkdir(parents=True, exist_ok=True)
    free_disk = shutil.disk_usage(root).free
    raw_bytes = total * dimensions * 4
    # Include raw vectors, grouped text in SQLite and its WAL, a second
    # snapshot during publication, and temporary filesystem headroom.
    required_disk = int(raw_bytes * 2.5 + max(4 * 1024**3, total * 2048))
    if free_disk < required_disk:
        raise IndexCapacityError(
            f"FAISS work drive needs {required_disk / 1024**3:.1f} GiB free; "
            f"only {free_disk / 1024**3:.1f} GiB is available"
        )
    free_ram = _available_memory_bytes()
    # 65,536 training vectors, two compact indexes, and application headroom.
    required_ram = max(2 * 1024**3, min(total, 65536) * dimensions * 4 * 4)
    if free_ram is None or free_ram < required_ram:
        available = "unknown" if free_ram is None else f"{free_ram / 1024**3:.1f} GiB"
        raise IndexCapacityError(
            f"FAISS build needs {required_ram / 1024**3:.1f} GiB available RAM; "
            f"available RAM is {available}"
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _exact_top_scores(vectors: Any, query: Any, np: Any, *, top_k: int) -> list[float]:
    """Stream one reference query over the raw vectors with bounded RAM."""
    best = np.empty(0, dtype="float32")
    for start in range(0, len(vectors), 8192):
        scores = np.asarray(vectors[start : start + 8192]) @ query
        combined = np.concatenate((best, scores))
        count = min(top_k, len(combined))
        best = np.sort(np.partition(combined, -count)[-count:])[::-1]
    return [float(value) for value in best]


def _choose_search_parameters(index: Any, vectors: Any, np: Any) -> dict[str, Any]:
    """Compare IVF-PQ candidates, reranked by raw vectors, with exact search."""
    if not hasattr(index, "nprobe"):
        return {"nprobe": 0, "candidates": min(256, len(vectors)), "max_score_loss": 0.0}
    positions = np.linspace(0, len(vectors) - 1, 4, dtype=np.int64)
    probes = np.ascontiguousarray(vectors[positions])
    exact = [
        _exact_top_scores(vectors, query, np, top_k=10)
        for query in probes
    ]
    for nprobe, candidates in ((64, 256), (128, 512), (256, 1024), (512, 2048)):
        index.nprobe = min(nprobe, int(index.nlist))
        _, keys = index.search(probes, min(candidates, len(vectors)))
        losses = []
        for query, expected, row in zip(probes, exact, keys, strict=True):
            selected = np.unique(row[row >= 0].astype(np.int64))
            if len(selected) < 10:
                losses.append(1.0)
                continue
            candidate_scores = np.asarray(vectors[selected]) @ query
            actual = float(np.partition(candidate_scores, -10)[-10])
            losses.append(max(0.0, expected[-1] - actual))
        worst_loss = max(losses)
        if worst_loss <= 0.03:
            return {
                "nprobe": int(index.nprobe),
                "candidates": candidates,
                "max_score_loss": round(worst_loss, 6),
                "reference_queries": len(probes),
            }
    raise IndexQualityError(
        "Compressed FAISS candidates lost more than 0.03 cosine score "
        "against bounded exact search on four reference queries"
    )


def build_scalable_index(
    semantic: Any,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Stream vectors, checkpoint their mapping, then train and publish IVF-PQ."""
    faiss, np = semantic._libraries()
    settings = semantic.settings
    with semantic.connection_factory() as connection:
        model = connection.execute(
            "SELECT id, dimensions FROM embedding_models WHERE model_name = ?",
            (semantic.model_storage_name,),
        ).fetchone()
        if model is None:
            raise FukamiruError("The pinned embedding model has no registered dimensions")
        model_id = str(model["id"])
        dimensions = int(model["dimensions"])
        signature, total = _group_signature(connection, settings, progress)
    if total < 1:
        raise FukamiruError("No retrieval groups are available for indexing")
    snapshots = settings.paths.faiss_root / "snapshots"
    _check_capacity(snapshots, total, dimensions)
    build_model_name = f"{semantic.model_identity_name}#retrieval-groups-v2"
    now = utc_now()
    with semantic.connection_factory() as connection:
        active = connection.execute(
            """
            SELECT b.id AS build_id, v.id AS version_id, v.index_path
            FROM embedding_builds b
            JOIN vector_index_versions v ON v.id = b.index_version_id
            WHERE b.corpus_signature = ? AND b.model_name = ?
              AND b.model_revision = ? AND b.status = 'completed'
              AND v.status = 'active' AND v.chunk_count = ?
            ORDER BY v.activated_at DESC LIMIT 1
            """,
            (signature, build_model_name, semantic.model_revision, total),
        ).fetchone()
        if (
            active
            and Path(str(active["index_path"])).is_file()
            and Path(str(active["index_path"])).with_suffix(".f32").is_file()
            and Path(str(active["index_path"])).with_suffix(".search.json").is_file()
        ):
            return {
                "semantic_index": "ready",
                "embedding_build_id": str(active["build_id"]),
                "index_version_id": str(active["version_id"]),
                "chunk_count": total,
                "index_path": str(active["index_path"]),
                "reused": True,
            }
        previous = connection.execute(
            """
            SELECT id, index_version_id, completed_count FROM embedding_builds
            WHERE corpus_signature = ? AND model_name = ? AND model_revision = ?
              AND status IN ('building', 'failed')
            ORDER BY updated_at DESC LIMIT 1
            """,
            (signature, build_model_name, semantic.model_revision),
        ).fetchone()
        if previous:
            build_id = str(previous["id"])
            version_id = str(previous["index_version_id"])
            completed = int(previous["completed_count"])
            mapped = connection.execute(
                "SELECT COUNT(*) FROM retrieval_index_groups WHERE index_version_id=?",
                (version_id,),
            ).fetchone()[0]
            if int(mapped) != completed:
                raise FukamiruError("Retrieval mapping differs from its committed checkpoint")
            connection.execute(
                """UPDATE embedding_builds SET status='building', error_json=NULL,
                   updated_at=? WHERE id=?""",
                (now, build_id),
            )
            connection.execute(
                "UPDATE vector_index_versions SET status='building' WHERE id=?",
                (version_id,),
            )
        else:
            build_id, version_id, completed = str(uuid.uuid4()), str(uuid.uuid4()), 0
            index_path = snapshots / f"{version_id}.faiss"
            connection.execute(
                """
                INSERT INTO vector_index_versions(
                    id, backend, embedding_model, dimensions, index_path,
                    status, chunk_count, created_at
                ) VALUES (?, 'faiss', ?, ?, ?, 'building', 0, ?)
                """,
                (version_id, semantic.model_identity_name, dimensions, str(index_path), now),
            )
            connection.execute(
                """
                INSERT INTO embedding_builds(
                    id, index_version_id, corpus_signature, embedding_model_id,
                    model_name, model_revision, configured_device, actual_device,
                    status, total_count, completed_count, batch_size, started_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'building', ?, 0, ?, ?, ?)
                """,
                (
                    build_id, version_id, signature, model_id, build_model_name,
                    semantic.model_revision, settings.retrieval.embedding_device,
                    total, settings.retrieval.embedding_batch_size, now, now,
                ),
            )
        connection.commit()

    raw_path = snapshots / f"{version_id}.f32"
    expected_bytes = completed * dimensions * 4
    if completed and (not raw_path.is_file() or raw_path.stat().st_size < expected_bytes):
        raise FukamiruError("Resumable vector file is shorter than its committed checkpoint")
    with raw_path.open("r+b" if raw_path.exists() else "w+b") as raw:
        raw.truncate(expected_bytes)
        raw.seek(expected_bytes)
        started = time.monotonic()
        completed_at_start = completed
        with (
            semantic.connection_factory() as read_connection,
            semantic.connection_factory() as write_connection,
        ):
            groups = iter_retrieval_groups(read_connection, settings)
            for _ in range(completed):
                next(groups)
            while completed < total:
                batch = []
                for _ in range(256):
                    try:
                        batch.append(next(groups))
                    except StopIteration:
                        break
                if not batch:
                    raise FukamiruError("Retrieval groups changed during embedding")
                hashes = [hashlib.sha256(g.text.encode("utf-8")).hexdigest() for g in batch]
                distinct = list(dict.fromkeys(hashes))
                placeholders = ",".join("?" for _ in distinct)
                cached = {
                    str(row["content_sha256"]): np.frombuffer(
                        row["vector_blob"], dtype="float32"
                    ).copy()
                    for row in write_connection.execute(
                        f"""
                        SELECT content_sha256, vector_blob FROM embedding_cache
                        WHERE embedding_model_id=? AND model_revision=?
                          AND content_sha256 IN ({placeholders})
                        """,  # noqa: S608 - generated placeholders only
                        (model_id, semantic.model_revision, *distinct),
                    )
                }
                missing = [key for key in distinct if key not in cached]
                texts = {key: group.text for key, group in zip(hashes, batch, strict=True)}
                gpu_batch = max(1, settings.retrieval.embedding_batch_size)
                for start in range(0, len(missing), gpu_batch):
                    keys = missing[start : start + gpu_batch]
                    encoded = semantic.provider.encode([texts[key] for key in keys])
                    if len(encoded) != len(keys):
                        raise FukamiruError("Embedding provider returned the wrong vector count")
                    for key, vector in zip(keys, encoded, strict=True):
                        cached[key] = np.asarray(vector, dtype="float32")
                matrix = np.stack([cached[key] for key in hashes]).astype("float32", copy=False)
                if matrix.shape != (len(batch), dimensions):
                    raise FukamiruError("Embedding dimensions changed during the build")
                faiss.normalize_L2(matrix)
                raw.write(matrix.tobytes(order="C"))
                raw.flush()
                os.fsync(raw.fileno())
                write_connection.execute("BEGIN IMMEDIATE")
                try:
                    write_connection.executemany(
                        """
                        INSERT INTO retrieval_index_groups(
                            index_version_id, vector_key, chunk_id, document_unit_id,
                            start_source_span_id, end_source_span_id,
                            source_span_ids_json, text, token_count, source_ranges_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            (
                                version_id, completed + offset, group.anchor_chunk_id,
                                group.document_unit_id, group.start_source_span_id,
                                group.end_source_span_id,
                                json.dumps(group.source_span_ids, ensure_ascii=False),
                                group.text, group.token_count, json.dumps(group.source_ranges),
                            )
                            for offset, group in enumerate(batch)
                        ),
                    )
                    completed += len(batch)
                    elapsed = max(time.monotonic() - started, 0.001)
                    rate = (completed - completed_at_start) / elapsed
                    write_connection.execute(
                        """
                        UPDATE embedding_builds SET completed_count=?,
                            encoded_count=encoded_count+?,
                            cache_hit_count=cache_hit_count+?, chunks_per_second=?,
                            eta_seconds=?, actual_device=?, updated_at=? WHERE id=?
                        """,
                        (
                            completed, len(missing), len(batch) - len(missing), rate,
                            (total - completed) / rate, str(getattr(
                                semantic.provider, "actual_device", "provider_managed"
                            )), utc_now(), build_id,
                        ),
                    )
                    write_connection.commit()
                except Exception:
                    write_connection.rollback()
                    raise
                if progress:
                    progress({
                        "embedding_build_id": build_id,
                        "embedded_count": completed,
                        "total_count": total,
                        "chunks_per_second": rate,
                        "eta_seconds": (total - completed) / rate,
                        "batch_size": gpu_batch,
                        "model": semantic.provider.model_name,
                        "model_revision": semantic.model_revision,
                        "index_type": "IVFPQ64x8",
                        "raw_vector_path": str(raw_path),
                    })
    if raw_path.stat().st_size != total * dimensions * 4:
        raise FukamiruError("Raw vector file does not match its committed vector count")
    with semantic.connection_factory() as connection:
        current_signature, current_total = _group_signature(connection, settings)
    if current_signature != signature or current_total != total:
        raise IndexBuildGuardError(
            "Corpus changed during index construction; the older active index was retained"
        )
    raw_hash = _sha256_file(raw_path)
    (snapshots / f"{version_id}.f32.sha256.json").write_text(
        json.dumps({"sha256": raw_hash, "vectors": total, "dimensions": dimensions}),
        encoding="utf-8",
    )
    _check_capacity(snapshots, total, dimensions)
    vectors = np.memmap(raw_path, dtype="float32", mode="r", shape=(total, dimensions))
    if total < 4096:
        index = faiss.IndexFlatIP(dimensions)
    else:
        nlist = min(1024, max(16, math.isqrt(total)))
        training_count = min(total, 65536)
        sample_positions = np.linspace(0, total - 1, training_count, dtype=np.int64)
        training = np.ascontiguousarray(vectors[sample_positions])
        index = faiss.IndexIVFPQ(
            faiss.IndexFlatIP(dimensions), dimensions, nlist, 64, 8,
            faiss.METRIC_INNER_PRODUCT,
        )
        index.train(training)
        del training
        index.nprobe = min(64, nlist)
    for start in range(0, total, 8192):
        index.add(np.ascontiguousarray(vectors[start : start + 8192]))
    search_parameters = _choose_search_parameters(index, vectors, np)
    del vectors
    temporary_path = snapshots / f"{version_id}.faiss.tmp"
    final_path = snapshots / f"{version_id}.faiss"
    try:
        faiss.write_index(index, str(temporary_path))
        published = faiss.read_index(str(temporary_path))
        if int(published.ntotal) != total or int(published.d) != dimensions:
            raise FukamiruError("Compressed FAISS snapshot failed count validation")
        del published
        os.replace(temporary_path, final_path)
        (snapshots / f"{version_id}.search.json").write_text(
            json.dumps(search_parameters, ensure_ascii=False), encoding="utf-8"
        )
    finally:
        temporary_path.unlink(missing_ok=True)
    finished = utc_now()
    with semantic.connection_factory() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                "UPDATE vector_index_versions SET status='superseded' WHERE status='active'"
            )
            connection.execute(
                """
                UPDATE vector_index_versions
                SET status='active', chunk_count=?, index_path=?, activated_at=? WHERE id=?
                """,
                (total, str(final_path), finished, version_id),
            )
            connection.execute(
                "DELETE FROM chunk_embeddings WHERE embedding_model_id=?", (model_id,)
            )
            connection.execute(
                """
                INSERT INTO chunk_embeddings(chunk_id, embedding_model_id, vector_key, created_at)
                SELECT chunk_id, ?, CAST(MIN(vector_key) AS TEXT), ?
                FROM retrieval_index_groups WHERE index_version_id=? GROUP BY chunk_id
                """,
                (model_id, finished, version_id),
            )
            connection.execute(
                """
                UPDATE embedding_builds SET status='completed', completed_count=?,
                    eta_seconds=0, updated_at=?, completed_at=? WHERE id=?
                """,
                (total, finished, finished, build_id),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return {
        "semantic_index": "ready",
        "embedding_build_id": build_id,
        "index_version_id": version_id,
        "chunk_count": total,
        "model": semantic.provider.model_name,
        "model_revision": semantic.model_revision,
        "index_path": str(final_path),
        "raw_vector_path": str(raw_path),
        "raw_vector_sha256": raw_hash,
        "index_type": "IVFPQ64x8" if total >= 4096 else "Flat-small",
        "search_quality": search_parameters,
    }


@lru_cache(maxsize=2)
def _compact_index(path: str) -> Any:
    import faiss

    index = faiss.read_index(path)
    if hasattr(index, "nprobe"):
        parameters = json.loads(Path(path).with_suffix(".search.json").read_text(encoding="utf-8"))
        index.nprobe = min(int(parameters["nprobe"]), int(index.nlist))
    return index


def search_scalable_index(
    semantic: Any,
    version: sqlite3.Row,
    query: str,
    limit: int,
) -> list[dict[str, Any]]:
    """Use compact candidate search and exact FP32 reranking from D storage."""
    faiss, np = semantic._libraries()
    index_path = Path(version["index_path"])
    if not index_path.is_file():
        raise FukamiruError(f"Active FAISS snapshot is missing: {index_path}")
    raw_path = index_path.with_suffix(".f32")
    index = _compact_index(str(index_path))
    dimensions = int(index.d)
    total = int(index.ntotal)
    if not raw_path.is_file() or raw_path.stat().st_size != total * dimensions * 4:
        raise FukamiruError("Active raw vector file is missing or incomplete")
    vector = np.asarray(semantic.provider.encode([query]), dtype="float32")
    if vector.shape != (1, dimensions):
        raise FukamiruError("Query embedding dimension does not match the index")
    faiss.normalize_L2(vector)
    parameters_path = index_path.with_suffix(".search.json")
    parameters = (
        json.loads(parameters_path.read_text(encoding="utf-8"))
        if parameters_path.is_file()
        else {"candidates": 256}
    )
    candidate_count = min(total, max(int(parameters["candidates"]), limit * 20))
    _, keys = index.search(vector, candidate_count)
    candidates = list(dict.fromkeys(int(key) for key in keys[0] if int(key) >= 0))
    if not candidates:
        return []
    raw = np.memmap(raw_path, dtype="float32", mode="r", shape=(total, dimensions))
    exact_vectors = np.asarray(raw[candidates])
    scores = exact_vectors @ vector[0]
    ranked = sorted(
        zip(candidates, (float(value) for value in scores), strict=True),
        key=lambda pair: (-pair[1], pair[0]),
    )[:limit]
    selected = [key for key, _ in ranked]
    placeholders = ",".join("?" for _ in selected)
    with semantic.connection_factory() as connection:
        rows = connection.execute(
            f"""
            SELECT g.vector_key, g.chunk_id, g.document_unit_id,
                   g.source_span_ids_json, g.source_ranges_json, g.end_source_span_id, g.text,
                   d.source_asset_id, d.title, l.path
            FROM retrieval_index_groups g
            JOIN document_units d ON d.id = g.document_unit_id
            JOIN source_locations l
              ON l.source_asset_id = d.source_asset_id
             AND l.is_primary = 1 AND l.is_available = 1
            WHERE g.index_version_id = ? AND g.vector_key IN ({placeholders})
            """,  # noqa: S608 - generated placeholders only
            (str(version["id"]), *selected),
        ).fetchall()
    by_key = {int(row["vector_key"]): row for row in rows}
    return [
        {
            "chunk_id": str(by_key[key]["chunk_id"]),
            "document_unit_id": str(by_key[key]["document_unit_id"]),
            "source_asset_id": str(by_key[key]["source_asset_id"]),
            "title": by_key[key]["title"],
            "path": by_key[key]["path"],
            "text": str(by_key[key]["text"]),
            "source_span_ids": json.loads(by_key[key]["source_span_ids_json"]),
            "retrieval_group_key": key,
            "source_ranges": json.loads(by_key[key]["source_ranges_json"]),
            "end_source_span_id": str(by_key[key]["end_source_span_id"]),
            "score": score,
            "retrieval": "semantic",
        }
        for key, score in ranked
        if key in by_key
    ]
