"""Phase H foreign-manifestation fallback contracts."""

from __future__ import annotations

import copy

from fukamiru.revisions.reference_canonical import finalize_reference_v29
from fukamiru.revisions.references import JapaneseReferenceService

HTML = (
    '<article id="DOC" lang="ja"><section id="REF">'
    '<h2 id="HEAD">参考文献</h2><p id="ROW">ウィリアムズ『マルクス主義と文学』</p>'
    "</section></article>"
)


def _foreign(
    candidate_id: str,
    *,
    title: str = "Marxism and Literature",
    author: str = "Raymond Williams",
    publisher: str = "Oxford University Press",
    year: str = "1977",
    isbn: str = "0198760566",
) -> dict:
    url = f"https://catalog.example/books/{candidate_id}"
    return {
        "candidate_id": candidate_id,
        "title": title,
        "record_type": "book",
        "publication_language": "en",
        "publisher": publisher,
        "year": year,
        "isbn": isbn,
        "authors": [author],
        "provider": "catalog",
        "url": url,
        "responsibility_sources": [{
            "provider": "catalog",
            "url": url,
            "responsibility_raw": [f"{author} 著"],
            "responsibility_source": "exact_record_detail",
            "authors": [author],
        }],
    }


def _parent(candidates: list[dict], *, completed: bool = True) -> dict:
    return {
        "reference_id": "ref-h",
        "source_block_id": "ROW",
        "entry_ordinal": 0,
        "parse_status": "parsed",
        "original_text": "ウィリアムズ『マルクス主義と文学』",
        "title": "マルクス主義と文学",
        "author": "ウィリアムズ",
        "record_type": "book_or_journal",
        "provenance": {
            "title": {"kind": "source_explicit", "label": "原稿に明記"},
            "author": {"kind": "source_explicit", "label": "原稿に明記"},
        },
        "search_hypotheses": {
            "titles": ["Marxism and Literature"],
            "authors": ["Raymond Williams"],
        },
        "candidate_matches": candidates,
        "translation_side_discovery_triggered": True,
        "translation_side_discovery_completed": completed,
        "japanese_manifestation_found": False,
        "japanese_manifestation_ready": False,
    }


def _render_quality(entry: dict) -> tuple[str, dict]:
    rendered, _ = JapaneseReferenceService.render_sections_v22(
        HTML, [entry], workflow_version=29, render_machine_section=False,
    )
    quality = JapaneseReferenceService.quality_report(
        rendered, [entry], workflow_version=29,
    )
    return rendered, quality


def test_one_ready_foreign_manifestation_falls_back_to_single() -> None:
    output = finalize_reference_v29(_parent([_foreign("oxford")]))
    assert output["reference_output_mode_v29"] == "SINGLE"
    assert output["selection_basis"] == "foreign_manifestation_fallback"
    assert output["selection_audit"]["selection_basis"] == (
        "foreign_manifestation_fallback"
    )
    assert output["foreign_manifestation_fallback_used"] is True
    assert output["title"] == "Marxism and Literature"
    assert output["authors"] == ["Raymond Williams"]
    assert output["source_title_mention_audit"]["value"] == "マルクス主義と文学"
    assert output["final_revision_blocked"] is False
    rendered, quality = _render_quality(output)
    assert "Marxism and Literature" in rendered
    assert "日本語版なし" not in rendered
    assert quality["passed"] is True
    assert not any(item["severity"] == "error" for item in quality["issues"])
    assert {
        item["code"] for item in quality["issues"] if item["severity"] == "info"
    } == {
        "reference.source_language_manifestation_missing",
        "reference.foreign_manifestation_fallback_used",
    }


def test_multiple_ready_foreign_manifestations_fall_back_to_multiple() -> None:
    second = _foreign(
        "paperback", publisher="Verso", year="1978", isbn="0860910008",
    )
    output = finalize_reference_v29(_parent([_foreign("oxford"), second]))
    assert output["reference_output_mode_v29"] == "MULTIPLE"
    assert output["foreign_manifestation_fallback_used"] is True
    assert output["selection_basis"] == "foreign_manifestation_fallback"
    assert len(output["expanded_reference_entries"]) == 2
    assert all(
        item["foreign_manifestation_fallback_used"]
        for item in output["expanded_reference_entries"]
    )
    _, quality = _render_quality(output)
    assert quality["passed"] is True


def test_ambiguous_foreign_identity_does_not_fallback() -> None:
    mismatch = _foreign("wrong", author="Jane Smith")
    output = finalize_reference_v29(_parent([mismatch]))
    assert output["reference_output_mode_v29"] == "REVIEW"
    assert output["foreign_manifestation_fallback_used"] is False
    assert output["foreign_manifestation_fallback_blocked_due_to_ambiguity"] is True
    assert output["foreign_manifestation_fallback_audit"]["blocking_reason"] == (
        "foreign_work_identity_not_confirmed"
    )


def test_incomplete_japanese_discovery_does_not_fallback() -> None:
    parent = _parent([_foreign("oxford")], completed=False)
    output = finalize_reference_v29(parent)
    assert output["reference_output_mode_v29"] == "REVIEW"
    assert output["foreign_manifestation_fallback_used"] is False
    assert output["foreign_manifestation_fallback_audit"]["blocking_reason"] == (
        "translation_side_discovery_not_completed"
    )
    _, quality = _render_quality(output)
    assert quality["passed"] is False
    assert any(
        item["code"] == "reference.source_language_manifestation_missing"
        and item["severity"] == "error"
        for item in quality["issues"]
    )


def test_unrelated_ready_foreign_work_blocks_the_entire_fallback_set() -> None:
    unrelated = _foreign(
        "unrelated", title="A Different Work", author="Raymond Williams",
        year="1980", isbn="0190000000",
    )
    output = finalize_reference_v29(
        _parent([_foreign("oxford"), copy.deepcopy(unrelated)])
    )
    assert output["reference_output_mode_v29"] == "REVIEW"
    assert output["foreign_manifestation_fallback_used"] is False
