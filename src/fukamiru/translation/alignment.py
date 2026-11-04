"""Translation segment alignment inspection with canonical source provenance."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from fukamiru.errors import FukamiruError


class TranslationAlignmentService:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def inspect(self, version_id: str) -> dict[str, Any]:
        version = self.connection.execute(
            """
            SELECT v.*, u.source_text, u.source_language, u.target_language,
                   u.raw_ocr_revision_id, u.normalized_ocr_revision_id,
                   u.translation_source_revision_id
            FROM translation_versions v
            JOIN translation_units u ON u.id = v.translation_unit_id
            WHERE v.id = ?
            """,
            (version_id,),
        ).fetchone()
        if version is None:
            raise FukamiruError(f"TranslationVersion not found: {version_id}")
        rows = self.connection.execute(
            """
            SELECT s.*, b.batch_ordinal
            FROM translation_segment_alignments s
            JOIN translation_batch_alignments b ON b.id = s.batch_alignment_id
            WHERE s.translation_version_id = ?
            ORDER BY s.segment_ordinal
            """,
            (version_id,),
        ).fetchall()
        segments: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            span_ids = [str(value) for value in json.loads(item.pop("source_span_ids_json"))]
            item["source_span_ids"] = span_ids
            placeholders = ",".join("?" for _ in span_ids)
            spans = []
            if placeholders:
                spans = [
                    dict(span)
                    for span in self.connection.execute(
                        f"""
                        SELECT s.id, s.start_char, s.end_char, s.reconstruction_method,
                               sp.page_number AS start_page, ep.page_number AS end_page,
                               u.source_asset_id, a.media_type, l.path AS source_path
                        FROM source_spans s
                        JOIN document_units u ON u.id = s.document_unit_id
                        JOIN source_assets a ON a.id = u.source_asset_id
                        LEFT JOIN pages sp ON sp.id = s.start_page_id
                        LEFT JOIN pages ep ON ep.id = s.end_page_id
                        LEFT JOIN source_locations l
                          ON l.source_asset_id = a.id AND l.is_primary = 1
                        WHERE s.id IN ({placeholders})
                        ORDER BY s.ordinal
                        """,
                        span_ids,
                    )
                ]
            item["source_spans"] = spans
            segments.append(item)
        result = dict(version)
        result["segments"] = segments
        result["segment_count"] = len(segments)
        result["ambiguous_count"] = sum(
            item["alignment_status"] == "ambiguous" for item in segments
        )
        return result
