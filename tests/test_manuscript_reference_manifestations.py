from __future__ import annotations

import xml.etree.ElementTree as ET

from fukamiru.literature import LiteratureCandidate
from fukamiru.revisions.reference_search import discover_reference
from fukamiru.revisions.references import JapaneseReferenceService

REFERENCE_HTML = (
    '<article id="DOC" lang="ja"><section id="MAIN">'
    '<h2 id="MAIN-H">本文</h2><p id="BODY">本文。</p></section>'
    '<section id="REFS"><h2 id="REFS-H">参考文献</h2>'
    '<p id="REF-ONE">デリダ『散種』。</p></section></article>'
)


def _entry(**overrides):
    entry = {
        "reference_id": "reference:test",
        "parse_status": "parsed",
        "resolution_status": "pending",
        "record_type": "book_or_journal",
        "author": "デリダ",
        "title": "散種",
        "publisher": None,
        "year": None,
        "volume": None,
        "edition": None,
        "isbn": None,
        "doi": None,
        "translators": [],
        "provenance": {
            "author": {"kind": "source_explicit"},
            "title": {"kind": "source_explicit"},
        },
    }
    entry.update(overrides)
    return entry


def _candidate(
    record_id: str,
    *,
    title: str = "散種",
    author: str = "デリダ",
    provider: str = "ndl_search",
    publisher: str = "法政大学出版局",
    year: str = "2013",
    isbn: str | None = None,
    doi: str | None = None,
    volume: str | None = None,
    edition: str | None = None,
    original_title: str | None = None,
    translated_title: str | None = None,
    publication_language: str | None = None,
    translators: list[str] | None = None,
    is_translation: bool | None = None,
) -> LiteratureCandidate:
    payload = {
        "isbn": isbn,
        "volume": volume,
        "edition": edition,
        "original_title": original_title,
        "translated_title": translated_title,
        "publication_language": publication_language,
        "translators": translators or [],
        "is_translation": is_translation,
    }
    return LiteratureCandidate(
        provider=provider,
        provider_record_id=record_id,
        title=title,
        authors=(author,),
        publication_title=None,
        publisher=publisher,
        publication_date=year,
        record_type="図書",
        doi=doi,
        url=None,
        oa_url=None,
        oa_status=None,
        license_url=None,
        provider_payload=payload,
    )


def test_v22_separates_raw_noise_from_the_only_eligible_manifestation() -> None:
    result = JapaneseReferenceService.resolve_with_candidates(
        _entry(),
        [
            _candidate("correct", isbn="9784588009853"),
            _candidate("related", title="散種研究", isbn="9780000000001"),
        ],
        workflow_version=22,
    )

    assert result["raw_hit_total"] == 2
    assert len(result["raw_candidate_hits"]) == 2
    assert len(result["eligible_work_candidates"]) == 1
    assert len(result["candidate_matches"]) == 1
    assert len(result["rejected_candidate_hits"]) == 1
    assert result["resolution_status"] == "manifestations_retained"
    assert result["work_identity_status"] == "matched"
    assert result["edition_status"] == "all_unspecified_manifestations_retained"
    assert result["consulted_status"] == "unconfirmed"
    assert len(result["rendered_manifestations"]) == 1
    assert result["machine_detected_references"] == []


def test_v22_deduplicates_same_doi_across_providers_and_resorts() -> None:
    entry = _entry(
        record_type="article_poem_or_chapter",
        year="2013",
        provenance={
            "author": {"kind": "source_explicit"},
            "title": {"kind": "source_explicit"},
            "year": {"kind": "source_explicit"},
        },
    )
    candidates = [
        LiteratureCandidate(
            provider=provider,
            provider_record_id=record_id,
            title="散種",
            authors=("デリダ",),
            publication_title="思想",
            publisher=None,
            publication_date="2013",
            record_type="journal-article",
            doi="10.1234/SAME",
            url=None,
            oa_url=None,
            oa_status=None,
            license_url=None,
            provider_payload={"page": "1-20"},
        )
        for provider, record_id in (("crossref", "a"), ("openalex", "b"))
    ]

    result = JapaneseReferenceService.resolve_with_candidates(
        entry, candidates, workflow_version=22
    )

    assert len(result["raw_candidate_hits"]) == 2
    assert len(result["eligible_work_candidates"]) == 1
    selected = result["candidate_matches"][0]
    assert selected["duplicate_record_count"] == 2
    assert selected["providers"] == ["crossref", "openalex"]
    assert selected["doi"] == "10.1234/SAME"


def test_v22_normalizes_ndl_article_kind_before_rendering() -> None:
    entry = _entry(record_type="article_poem_or_chapter")
    candidate = LiteratureCandidate(
        provider="ndl_search",
        provider_record_id="ndl-article",
        title="散種",
        authors=("デリダ",),
        publication_title="思想",
        publisher=None,
        publication_date="2013",
        record_type="記事",
        doi=None,
        url=None,
        oa_url=None,
        oa_status=None,
        license_url=None,
        provider_payload={"pages": "1-20"},
    )

    result = JapaneseReferenceService.resolve_with_candidates(
        entry, [candidate], workflow_version=22
    )

    manifestation = result["candidate_matches"][0]
    assert manifestation["bibliography_kind"] == "article"
    assert result["rendered_manifestations"] == [
        "デリダ「散種」、『思想』、2013、p. 1-20。"
    ]


def test_v22_same_identifier_does_not_merge_original_and_translation() -> None:
    result = JapaneseReferenceService.resolve_with_candidates(
        _entry(),
        [
            _candidate(
                "original",
                doi="10.1234/WORK",
                original_title="散種",
                is_translation=False,
            ),
            _candidate(
                "translation",
                provider="crossref",
                doi="10.1234/WORK",
                original_title="Dissemination",
                translated_title="散種",
                translators=["訳者甲"],
                is_translation=True,
            ),
        ],
        workflow_version=22,
    )

    assert len(result["candidate_matches"]) == 2
    assert {item["is_translation"] for item in result["candidate_matches"]} == {
        False,
        True,
    }
    assert len({item["candidate_id"] for item in result["candidate_matches"]}) == 2


def test_v22_same_identifier_does_not_merge_distinct_volumes_or_editions() -> None:
    result = JapaneseReferenceService.resolve_with_candidates(
        _entry(),
        [
            _candidate(
                "upper-first",
                doi="10.1234/WORK",
                volume="上巻",
                edition="初版",
                is_translation=False,
            ),
            _candidate(
                "upper-revised",
                provider="crossref",
                doi="10.1234/WORK",
                volume="上巻",
                edition="改訂版",
                is_translation=False,
            ),
            _candidate(
                "lower-revised",
                provider="openalex",
                doi="10.1234/WORK",
                volume="下巻",
                edition="改訂版",
                is_translation=False,
            ),
        ],
        workflow_version=22,
    )

    assert len(result["candidate_matches"]) == 3
    assert {
        (item["volume"], item["edition"]) for item in result["candidate_matches"]
    } == {("上巻", "初版"), ("上巻", "改訂版"), ("下巻", "改訂版")}
    assert len({item["candidate_id"] for item in result["candidate_matches"]}) == 3


def test_v22_deduplicates_repeated_hit_from_same_provider() -> None:
    repeated = _candidate("same-record", isbn="9784588009853", is_translation=False)

    result = JapaneseReferenceService.resolve_with_candidates(
        _entry(), [repeated, repeated], workflow_version=22
    )

    assert result["raw_hit_total"] == 2
    assert len(result["raw_candidate_hits"]) == 1
    assert result["raw_candidate_hits"][0]["raw_hit_count"] == 2
    assert len(result["candidate_matches"]) == 1


def test_v22_complete_link_dedup_does_not_bridge_conflicting_editions() -> None:
    result = JapaneseReferenceService.resolve_with_candidates(
        _entry(),
        [
            _candidate("a-first", edition="初版", is_translation=False),
            _candidate("b-unspecified", edition=None, is_translation=False),
            _candidate("c-revised", edition="改訂版", is_translation=False),
        ],
        workflow_version=22,
    )

    assert len(result["candidate_matches"]) == 2
    assert {item["edition"] for item in result["candidate_matches"]} == {
        "初版",
        "改訂版",
    }
    assert sorted(
        item["duplicate_record_count"] for item in result["candidate_matches"]
    ) == [1, 2]


def test_v22_keeps_all_unspecified_editions_translations_and_volumes() -> None:
    result = JapaneseReferenceService.resolve_with_candidates(
        _entry(search_aliases={"titles": ["Dissemination"], "authors": []}),
        [
            _candidate(
                "upper-old",
                isbn="9780000000001",
                volume="上巻",
                edition="初版",
                original_title="Dissemination",
                translators=["訳者甲"],
                is_translation=True,
            ),
            _candidate(
                "lower-new",
                isbn="9780000000002",
                volume="下巻",
                edition="改訳版",
                original_title="Dissemination",
                translators=["訳者乙"],
                is_translation=True,
            ),
            _candidate(
                "original",
                title="Dissemination",
                isbn="9780000000003",
                publisher="Athlone",
                year="1981",
                is_translation=False,
            ),
        ],
        workflow_version=22,
    )

    assert len(result["candidate_matches"]) == 3
    assert {item["volume"] for item in result["candidate_matches"]} == {
        "上巻",
        "下巻",
        None,
    }
    assert {item["edition"] for item in result["candidate_matches"]} == {
        "初版",
        "改訳版",
        None,
    }
    assert result["edition_status"] == "all_unspecified_manifestations_retained"
    assert result["resolution_status"] == "manifestations_retained"
    assert len(result["expanded_reference_entries"]) == 3
    assert len(result["rendered_manifestations"]) == 3


def test_v22_explicit_volume_limits_selection_without_dropping_work_candidates() -> None:
    entry = _entry(
        volume="上巻",
        provenance={
            "author": {"kind": "source_explicit"},
            "title": {"kind": "source_explicit"},
            "volume": {"kind": "source_explicit"},
        },
    )
    result = JapaneseReferenceService.resolve_with_candidates(
        entry,
        [
            _candidate(
                "upper-first",
                isbn="9780000000001",
                volume="上巻",
                edition="初版",
                is_translation=False,
            ),
            _candidate(
                "upper-revised",
                isbn="9780000000002",
                volume="上巻",
                edition="改訂版",
                is_translation=False,
            ),
            _candidate(
                "lower", isbn="9780000000003", volume="下巻", is_translation=False
            ),
        ],
        workflow_version=22,
    )

    assert len(result["eligible_work_candidates"]) == 3
    assert len(result["candidate_matches"]) == 2
    assert {item["volume"] for item in result["candidate_matches"]} == {"上巻"}
    assert {item["edition"] for item in result["candidate_matches"]} == {
        "初版",
        "改訂版",
    }
    assert result["resolution_status"] == "manifestations_retained"
    assert "edition" in result["unspecified_manifestation_axes"]
    assert len(result["expanded_reference_entries"]) == 2


def test_v22_explicit_edition_keeps_complete_unspecified_volume_set() -> None:
    entry = _entry(
        edition="改訂版",
        provenance={
            "author": {"kind": "source_explicit"},
            "title": {"kind": "source_explicit"},
            "edition": {"kind": "source_explicit"},
        },
    )
    result = JapaneseReferenceService.resolve_with_candidates(
        entry,
        [
            _candidate(
                "upper",
                isbn="9780000000001",
                volume="上巻",
                edition="改訂版",
                is_translation=False,
            ),
            _candidate(
                "lower",
                isbn="9780000000002",
                volume="下巻",
                edition="改訂版",
                is_translation=False,
            ),
            _candidate(
                "old",
                isbn="9780000000003",
                volume="上巻",
                edition="初版",
                is_translation=False,
            ),
        ],
        workflow_version=22,
    )

    assert len(result["candidate_matches"]) == 2
    assert {item["volume"] for item in result["candidate_matches"]} == {"上巻", "下巻"}
    assert {item["edition"] for item in result["candidate_matches"]} == {"改訂版"}
    assert "volume" in result["unspecified_manifestation_axes"]
    assert len(result["expanded_reference_entries"]) == 2


def test_v22_year_and_publisher_do_not_implicitly_specify_other_axes() -> None:
    entry = _entry(
        year="2013",
        publisher="法政大学出版局",
        provenance={
            "author": {"kind": "source_explicit"},
            "title": {"kind": "source_explicit"},
            "year": {"kind": "source_explicit"},
            "publisher": {"kind": "source_explicit"},
        },
    )
    result = JapaneseReferenceService.resolve_with_candidates(
        entry,
        [
            _candidate(
                "first",
                isbn="9780000000001",
                edition="初版",
                is_translation=False,
            ),
            _candidate(
                "revised",
                isbn="9780000000002",
                edition="改訂版",
                is_translation=False,
            ),
        ],
        workflow_version=22,
    )

    assert result["manifestation_axes"]["publication_year"] == "explicit"
    assert result["manifestation_axes"]["publisher"] == "explicit"
    assert result["manifestation_axes"]["edition"] == "unspecified"
    assert result["manifestation_axes"]["volume"] == "unspecified"
    assert result["manifestation_axes"]["original_or_translation"] == "unspecified"
    assert len(result["expanded_reference_entries"]) == 2


def test_v22_translation_without_translator_blocks_final_revision_only() -> None:
    entry = _entry(
        year="2013",
        provenance={
            "author": {"kind": "source_explicit"},
            "title": {"kind": "source_explicit"},
            "year": {"kind": "source_explicit"},
        },
    )
    result = JapaneseReferenceService.resolve_with_candidates(
        entry,
        [
            _candidate(
                "translation",
                isbn="9780000000001",
                original_title="Dissemination",
                translators=[],
            )
        ],
        workflow_version=22,
    )

    assert result["translator_status"] == "required_missing"
    assert result["resolution_status"] == "manifestations_retained"
    assert result["conditional_proposal_allowed"] is True
    assert result["final_revision_blocked"] is True
    assert "translators" in result["required_missing_fields"]


def test_v22_search_alias_alone_does_not_confirm_a_translation() -> None:
    result = JapaneseReferenceService.resolve_with_candidates(
        _entry(search_aliases={"titles": ["Dissemination"], "authors": []}),
        [_candidate("hypothesis-only", isbn="9780000000001")],
        workflow_version=22,
    )

    candidate = result["candidate_matches"][0]
    assert candidate["translation_status"] == "undetermined"
    assert candidate["translator_status"] == "undetermined"
    assert "translation_status_confirmation" in result["required_missing_fields"]
    assert result["final_revision_blocked"] is True


def test_v22_manifestation_language_alone_does_not_confirm_an_original() -> None:
    result = JapaneseReferenceService.resolve_with_candidates(
        _entry(author="Jacques Derrida", title="Dissemination"),
        [
            _candidate(
                "language-only",
                title="Dissemination",
                author="Jacques Derrida",
                publisher="Athlone",
                year="1981",
                isbn="9780000000001",
                publication_language="en",
            )
        ],
        workflow_version=22,
    )

    candidate = result["candidate_matches"][0]
    assert candidate["translation_status"] == "undetermined"
    assert candidate["translator_status"] == "undetermined"
    assert result["final_revision_blocked"] is True


def test_v22_structures_machine_detected_references_in_a_separate_section() -> None:
    result = JapaneseReferenceService.resolve_with_candidates(
        _entry(),
        [
            _candidate("correct", isbn="9784588009853"),
            _candidate("related", title="散種研究", isbn="9780000000001"),
        ],
        workflow_version=22,
    )
    sections = JapaneseReferenceService.bibliography_sections_v22(
        [result],
        machine_entries=[
            {
                "provider_record_id": "evidence:1",
                "title": "散種研究",
                "authors": ["研究者"],
                "record_type": "図書",
                "publisher": "研究出版社",
                "year": "2020",
            }
        ],
    )

    assert [section["title"] for section in sections] == ["参考文献", "機械判別参考文献"]
    machine = sections[1]
    assert machine["requires_human_confirmation"] is True
    assert len(machine["entries"]) == 1
    assert machine["entries"][0]["reference_role"] == (
        "machine_detected_not_confirmed_consulted"
    )


def test_v22_self_authored_unpublished_item_requests_metadata_in_the_reference() -> None:
    result = JapaneseReferenceService.resolve_with_candidates(
        _entry(author="自著", title="未刊研究"), [], workflow_version=22
    )
    rendered = JapaneseReferenceService.render_entry(result, workflow_version=22)

    assert result["resolution_status"] == "user_input_required"
    assert result["final_revision_blocked"] is True
    assert "document_type" in result["user_required_metadata"]
    assert "要ユーザー指定" in rendered
    assert "資料種別" in rendered


def test_v22_self_authored_no_hit_requests_only_remaining_metadata() -> None:
    entry = _entry(
        author="自著",
        title="未刊研究論文",
        record_type="article",
        year="2026",
        container_title="研究紀要",
        pages="1-20",
    )

    result = JapaneseReferenceService.resolve_with_candidates(
        entry, [], workflow_version=22
    )
    rendered = JapaneseReferenceService.render_entry(result, workflow_version=22)

    assert result["user_required_metadata"] == ["volume_issue_if_applicable"]
    assert result["user_action_required"]["fields"] == [
        "volume_issue_if_applicable"
    ]
    assert "巻号" in result["user_action_required"]["message"]
    assert "発表年" not in result["user_action_required"]["message"]
    assert "掲載先または出版社" not in result["user_action_required"]["message"]
    assert "ページ" not in result["user_action_required"]["message"]
    assert "巻号" in rendered
    assert "発表年" not in rendered
    assert "ページ" not in rendered


def test_v22_self_authored_no_hit_with_complete_metadata_only_requests_confirmation() -> None:
    result = JapaneseReferenceService.resolve_with_candidates(
        _entry(
            author="自著",
            title="未刊研究論文",
            record_type="article",
            year="2026",
            container_title="研究紀要",
            volume="12",
            pages="1-20",
        ),
        [],
        workflow_version=22,
    )
    rendered = JapaneseReferenceService.render_entry(result, workflow_version=22)

    assert result["user_required_metadata"] == []
    assert result["user_action_required"]["fields"] == []
    assert "登録済みの書誌情報と分類" in result["user_action_required"]["message"]
    assert "要ユーザー確認：登録済み書誌情報と資料分類" in rendered
    assert "要ユーザー指定：資料種別" not in rendered


def test_v22_unpublished_item_without_self_author_label_requests_metadata() -> None:
    entry = _entry(author="研究者", title="調査ノート（未刊）")
    entry["original_text"] = "研究者『調査ノート』、未刊。"

    result = JapaneseReferenceService.resolve_with_candidates(
        entry, [], workflow_version=22
    )
    rendered = JapaneseReferenceService.render_entry(result, workflow_version=22)

    assert result["resolution_status"] == "user_input_required"
    assert result["final_revision_blocked"] is True
    assert "要ユーザー指定" in rendered
    assert "発表年" in rendered


def test_v21_default_keeps_legacy_resolution_for_resumability() -> None:
    result = JapaneseReferenceService.resolve_with_candidates(
        _entry(),
        [
            _candidate("correct", isbn="9784588009853"),
            _candidate("related", title="散種研究", isbn="9780000000001"),
        ],
    )

    assert result["resolution_status"] == "ambiguous"
    assert "raw_candidate_hits" not in result


class _Provider:
    def __init__(self, name: str, candidates=None, *, fail: bool = False) -> None:
        self.name = name
        self.candidates = candidates or []
        self.fail = fail

    def search(self, query: str, *, limit: int):
        if self.fail:
            raise RuntimeError("provider unavailable")
        return self.candidates[:limit]


def test_v22_search_distinguishes_provider_failure_from_no_hits() -> None:
    result = discover_reference(
        _entry(),
        (
            _Provider("working", [_candidate("correct", isbn="9784588009853")]),
            _Provider("empty"),
            _Provider("failed", fail=True),
        ),
        workflow_version=22,
    )

    assert {item["provider"]: item["status"] for item in result["provider_statuses"]} == {
        "working": "available_with_hits",
        "empty": "available_no_hits",
        "failed": "provider_unavailable",
    }
    assert (
        result["resolution_status"]
        == "found_manifestations_retained_search_incomplete"
    )
    completeness = result["search_completeness"]
    assert completeness["status"] == "search_incomplete"
    assert completeness["pagination_enabled"] is False
    assert completeness["pagination_followed"] is False
    assert completeness["provider_failure_detected"] is True
    assert "provider_unavailable" in completeness["incompleteness_reasons"]
    assert completeness["search_signature"].startswith("sha256:")
    assert completeness["observed_provider_record_ids"]
    assert completeness["query_attempts"]
    assert all(
        attempt["pagination_enabled"] is False
        and attempt["per_query_limit"] == 20
        for attempt in completeness["query_attempts"]
    )
    assert (
        result["user_action_required"]["message"]
        .startswith("確認できた候補すべて（網羅性未確認）")
    )
    assert result["final_revision_blocked"] is True


def test_v22_search_limit_saturation_is_persisted_as_incomplete_evidence() -> None:
    result = discover_reference(
        _entry(),
        (
            _Provider(
                "limited",
                [
                    _candidate(
                        f"limited-{index}",
                        isbn=f"9784588{index:06d}",
                    )
                    for index in range(20)
                ],
            ),
        ),
        workflow_version=22,
    )

    completeness = result["search_completeness"]
    assert completeness["per_query_limit"] == 20
    assert completeness["per_query_limit_saturated"] is True
    assert "per_query_limit_saturated" in completeness["incompleteness_reasons"]
    assert completeness["provider_states"] == [
        {
            "provider": "limited",
            "status": "available_with_hits",
            "attempt_count": completeness["generated_query_variant_count"],
            "successful_attempt_count": completeness["generated_query_variant_count"],
            "failed_attempt_count": 0,
            "per_query_limit_saturated": True,
            "pagination_enabled": False,
        }
    ]
    assert all(
        attempt["per_query_limit"] == 20
        and attempt["pagination_enabled"] is False
        and attempt["limit_saturated"] is True
        for attempt in result["search_attempts"][0]["attempts"]
    )


def test_v22_explicit_volume_and_edition_mismatch_allows_manual_consulted_entry() -> None:
    entry = _entry(volume="下巻", edition="改訳版")
    entry["provenance"].update(
        volume={"kind": "source_explicit"},
        edition={"kind": "source_explicit"},
    )

    result = JapaneseReferenceService.resolve_with_candidates(
        entry,
        [
            _candidate(
                "upper-initial",
                isbn="9780000000001",
                volume="上巻",
                edition="初版",
                is_translation=True,
                translators=["初版訳者"],
            )
        ],
        workflow_version=22,
    )

    assert result["resolution_status"] == "ambiguous"
    assert result["edition_status"] == "specified_scope_unresolved"
    assert result["candidate_matches"] == []
    assert result["manual_consulted_manifestation_allowed"] is True
    assert result["user_action_required"]["code"] == (
        "select_or_enter_consulted_manifestation"
    )
    assert result["user_action_required"]["fields"] == ["volume", "edition"]
    assert result["final_revision_blocked"] is True


def test_v22_explicit_scope_without_search_hits_allows_manual_consulted_entry() -> None:
    entry = _entry(volume="下巻", edition="改訳版")
    entry["provenance"].update(
        volume={"kind": "source_explicit"},
        edition={"kind": "source_explicit"},
    )

    result = JapaneseReferenceService.resolve_with_candidates(
        entry, [], workflow_version=22
    )

    assert result["resolution_status"] == "specified_scope_unresolved"
    assert result["manual_consulted_manifestation_allowed"] is True
    assert result["user_action_required"]["code"] == "enter_consulted_manifestation"
    assert result["user_action_required"]["fields"] == ["volume", "edition"]
    assert result["final_revision_blocked"] is True


def test_v22_inferences_cannot_clear_human_or_approval_state() -> None:
    entry = JapaneseReferenceService.resolve_with_candidates(
        _entry(author="自著", title="未刊研究"), [], workflow_version=22
    )
    original = dict(entry)
    inferred = JapaneseReferenceService.apply_inferences(
        [entry],
        {
            "inferences": [
                {
                    "reference_id": entry["reference_id"],
                    "fields": {"publisher": "推定出版社", "year": "2025"},
                    "rationale": "モデル推定",
                    "confidence": 1.0,
                }
            ]
        },
        workflow_version=22,
    )[0]

    assert inferred == original
    assert inferred["resolution_status"] == "user_input_required"
    assert inferred["final_revision_blocked"] is True
    assert "要ユーザー指定" in JapaneseReferenceService.render_entry(
        inferred, workflow_version=22
    )


def test_v22_renders_all_unspecified_manifestations_as_independent_items() -> None:
    entry = JapaneseReferenceService.extract(REFERENCE_HTML)["entries"][0]
    resolved = JapaneseReferenceService.resolve_with_candidates(
        entry,
        [
            _candidate(
                "first",
                isbn="9780000000001",
                edition="初版",
                original_title="Dissemination",
                translators=["訳者甲"],
                is_translation=True,
            ),
            _candidate(
                "revised",
                isbn="9780000000002",
                edition="改訳版",
                original_title="Dissemination",
                translators=["訳者乙"],
                is_translation=True,
            ),
        ],
        workflow_version=22,
    )

    rendered, edits = JapaneseReferenceService.render_sections_v22(
        REFERENCE_HTML, [resolved]
    )
    rerendered, second_edits = JapaneseReferenceService.render_sections_v22(
        rendered, [resolved]
    )

    assert rendered.count("デリダ『散種』") == 2
    assert "訳者甲訳" in rendered and "訳者乙訳" in rendered
    assert 'data-reference-source-id="REF-ONE"' in rendered
    assert edits
    assert rerendered == rendered
    assert second_edits == []


def test_v22_machine_section_is_stable_but_unresolved_manifestation_blocks() -> None:
    html = REFERENCE_HTML.replace("『散種』。", "『散種』、2013。")
    entry = JapaneseReferenceService.extract(html)["entries"][0]
    unconfirmed = JapaneseReferenceService.resolve_with_candidates(
        entry,
        [_candidate("exact", isbn="9780000000001", is_translation=False)],
        workflow_version=22,
    )
    resolved = JapaneseReferenceService.select_candidate(
        unconfirmed,
        unconfirmed["candidate_matches"][0]["candidate_id"],
        workflow_version=22,
    )
    machine_entries = [{"title": "関連研究", "authors": ["研究者"]}]

    rendered, _ = JapaneseReferenceService.render_sections_v22(
        html, [resolved], machine_entries=machine_entries
    )
    rerendered, edits = JapaneseReferenceService.render_sections_v22(
        rendered, [resolved], machine_entries=machine_entries
    )
    report = JapaneseReferenceService.quality_report(
        rendered,
        [resolved],
        workflow_version=22,
        machine_entries=machine_entries,
    )
    extracted = JapaneseReferenceService.extract(rendered, workflow_version=22)

    assert rerendered == rendered
    assert edits == []
    assert rendered.count("機械判別参考文献") == 1
    assert rendered.count("関連研究") == 1
    assert "実際に参照したことを意味しません" in rendered
    assert extracted["entry_count"] == 1
    assert extracted["machine_reference_block_ids"]
    assert report["passed"] is False
    assert not any(
        issue.get("reference_id")
        == JapaneseReferenceService.bibliography_sections_v22(
            [resolved], machine_entries=machine_entries
        )[1]["entries"][0]["machine_reference_id"]
        and issue["code"] == "reference.metadata_incomplete"
        for issue in report["issues"]
    )
    assert any(
        issue.get("reference_id")
        == JapaneseReferenceService.bibliography_sections_v22(
            [resolved], machine_entries=machine_entries
        )[1]["entries"][0]["machine_reference_id"]
        and issue["code"] == "reference.machine_manifestation_unconfirmed"
        for issue in report["issues"]
    )


def test_v29_machine_only_review_creates_terminal_bibliography_shell() -> None:
    html = (
        '<article id="D" lang="ja"><section id="S"><h2 id="H">本文</h2>'
        '<p id="P">本文。</p></section></article>'
    )
    machine_entries = [{"title": "関連研究", "authors": ["研究者"]}]

    rendered, edits = JapaneseReferenceService.render_sections_v22(
        html,
        [],
        machine_entries=machine_entries,
        render_machine_section=False,
        workflow_version=29,
    )
    rerendered, _ = JapaneseReferenceService.render_sections_v22(
        rendered,
        [],
        machine_entries=machine_entries,
        render_machine_section=False,
        workflow_version=29,
    )

    root = ET.fromstring(rendered)
    bibliography = root.find("./section[@data-reference-section='consulted-generated-v22']")
    assert bibliography is not None
    assert "参考文献" in "".join(bibliography.itertext())
    assert "機械判別候補（未確認・参照済みではありません）" in "".join(
        bibliography.itertext()
    )
    assert "関連研究" in "".join(bibliography.itertext())
    assert any(
        edit.get("operation") == "render_generated_consulted_reference_section"
        for edit in edits
    )
    assert rerendered == rendered


def test_v22_select_candidate_removes_stale_all_manifestation_render_state() -> None:
    entry = _entry()
    unconfirmed = JapaneseReferenceService.resolve_with_candidates(
        entry,
        [
            _candidate(
                "first",
                isbn="9780000000001",
                edition="初版",
                is_translation=False,
            ),
            _candidate(
                "revised",
                isbn="9780000000002",
                edition="改訂版",
                is_translation=False,
            ),
        ],
        workflow_version=22,
    )
    selected_id = unconfirmed["candidate_matches"][1]["candidate_id"]

    selected = JapaneseReferenceService.select_candidate(
        unconfirmed, selected_id, workflow_version=22
    )

    assert selected["selected_candidate_id"] == selected_id
    assert selected["resolution_status"] == "resolved"
    assert selected["consulted_status"] == "user_confirmed"
    assert selected["final_revision_blocked"] is False
    assert "expanded_reference_entries" not in selected
    assert "rendered_manifestations" not in selected
    assert "selected_candidate_ids" not in selected


def test_v22_manifestation_confirmation_recomputes_status_from_selected_subset() -> None:
    unconfirmed = JapaneseReferenceService.resolve_with_candidates(
        _entry(search_aliases={"titles": ["Dissemination"], "authors": []}),
        [
            _candidate(
                "translation-without-translator",
                isbn="9780000000001",
                original_title="Dissemination",
                is_translation=True,
            ),
            _candidate(
                "original",
                title="Dissemination",
                isbn="9780000000002",
                publisher="Athlone",
                year="1981",
                is_translation=False,
            ),
        ],
        workflow_version=22,
    )
    original_id = next(
        candidate["candidate_id"]
        for candidate in unconfirmed["candidate_matches"]
        if candidate["is_translation"] is False
    )

    confirmed = JapaneseReferenceService.confirm_manifestations_v22(
        unconfirmed, [original_id]
    )

    assert confirmed["translation_status"] == "confirmed_original"
    assert confirmed["translator_status"] == "not_required"
    assert confirmed["required_missing_fields"] == []
    assert confirmed["final_revision_blocked"] is False
    assert "user_action_required" not in confirmed
