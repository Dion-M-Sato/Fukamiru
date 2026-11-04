"""Translation Memory lookup over human-confirmed canonical SQLite entries."""

from __future__ import annotations

import hashlib
import sqlite3
from difflib import SequenceMatcher
from typing import Any

MAX_FUZZY_QUERY_CHARACTERS = 20_000
MAX_FUZZY_CANDIDATES = 100
MAX_SIMILARITY_SAMPLE_CHARACTERS = 4_000


def _similarity_sample(text: str) -> str:
    if len(text) <= MAX_SIMILARITY_SAMPLE_CHARACTERS:
        return text
    section = MAX_SIMILARITY_SAMPLE_CHARACTERS // 3
    middle_start = max(0, (len(text) - section) // 2)
    return "\n".join(
        (
            text[:section],
            text[middle_start : middle_start + section],
            text[-section:],
        )
    )


class TranslationMemoryService:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def search(
        self,
        source_text: str,
        *,
        source_language: str | None = None,
        target_language: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        query = source_text.strip()
        if not query:
            return []
        query_hash = hashlib.sha256(query.encode("utf-8")).hexdigest()
        clauses = ["quality_status != 'rejected'"]
        parameters: list[object] = []
        if source_language and source_language != "auto":
            clauses.append("source_language = ?")
            parameters.append(source_language)
        if target_language:
            clauses.append("target_language = ?")
            parameters.append(target_language)
        selected_columns = """
            id, source_text_sha256, source_text, source_language,
            target_language, target_text, translation_unit_id,
            translation_version_id, created_by, quality_status, created_at
        """
        exact_rows = self.connection.execute(
            f"""
            SELECT {selected_columns}
            FROM translation_memory_entries
            WHERE {' AND '.join(clauses)} AND source_text_sha256 = ?
            ORDER BY
                CASE quality_status
                    WHEN 'human_corrected' THEN 0
                    WHEN 'approved' THEN 1
                    ELSE 2
                END,
            created_at DESC
            LIMIT ?
            """,
            [*parameters, query_hash, limit],
        ).fetchall()
        exact = []
        for row in exact_rows:
            if str(row["source_text"]) != query:
                continue
            item = dict(row)
            item["match_type"] = "exact"
            item["similarity"] = 1.0
            exact.append(item)
        if exact:
            return exact

        # Whole documents are useful as exact memories, but fuzzy-comparing two
        # book-length strings is both semantically weak and computationally explosive.
        if len(query) > MAX_FUZZY_QUERY_CHARACTERS:
            return []

        minimum_length = max(1, len(query) // 2)
        maximum_length = max(minimum_length, len(query) * 2)
        rows = self.connection.execute(
            f"""
            SELECT {selected_columns}
            FROM translation_memory_entries
            WHERE {' AND '.join(clauses)}
              AND LENGTH(source_text) BETWEEN ? AND ?
            ORDER BY
                CASE quality_status
                    WHEN 'human_corrected' THEN 0
                    WHEN 'approved' THEN 1
                    ELSE 2
                END,
                created_at DESC
            LIMIT ?
            """,
            [*parameters, minimum_length, maximum_length, MAX_FUZZY_CANDIDATES],
        ).fetchall()
        query_sample = _similarity_sample(query)
        ranked: list[dict[str, Any]] = []
        for row in rows:
            candidate = str(row["source_text"])
            score = SequenceMatcher(
                None,
                query_sample,
                _similarity_sample(candidate),
            ).ratio()
            if score < 0.25:
                continue
            item = dict(row)
            item["match_type"] = "similar"
            item["similarity"] = round(score, 6)
            ranked.append(item)
        ranked.sort(
            key=lambda item: (
                -float(item["similarity"]),
                0 if item["quality_status"] == "human_corrected" else 1,
                str(item["created_at"]),
            )
        )
        return ranked[:limit]
