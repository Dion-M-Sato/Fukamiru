from __future__ import annotations

from dataclasses import replace

import pytest

from fukamiru.errors import FukamiruError
from fukamiru.literature import LiteratureCandidate
from fukamiru.revisions import ManuscriptDiscourseService
from fukamiru.revisions.reasoning import ManuscriptReasoningRepository
from fukamiru.revisions.references import JapaneseReferenceService
from fukamiru.revisions.service import RevisionService
from fukamiru.worker.runner import WorkerRunner

HTML = (
    '<article id="DOC" lang="ja"><section id="BODY">'
    '<h2 id="HEAD">本文</h2><p id="P1">論証本文。</p>'
    "</section></article>"
)


@pytest.fixture
def worker_case():
    from test_worker import WorkerTests

    case = WorkerTests()
    case.setUp()
    try:
        yield case
    finally:
        case.tearDown()


def claimed_v22(case):
    run_id = case._create_run("manuscript_rewrite")
    claimed = case.worker._claim(lane="ingestion_cpu")
    assert claimed is not None and claimed.run_id == run_id
    return replace(
        claimed,
        node_id="render_manuscript_references",
        workflow_version="22",
        resource_lane="ingestion_cpu",
    )


def accepted_evidence(record_id: str, title: str) -> dict:
    return {
        "id": "evidence:" + record_id,
        "query": "関連研究",
        "source_type": "online_paper",
        "provider": "crossref",
        "provider_record_id": record_id,
        "title": title,
        "authors": ["研究者"],
        "source_url": f"https://example.invalid/{record_id}",
        "provenance": {
            "doi": f"10.1000/{record_id}",
            "publication_title": "研究紀要",
            "publication_date": "2024-03-01",
            "support_required": True,
            "support_audits": [
                {
                    "status": "supports_subclaim",
                    "host_grounded": True,
                    "supported_claim": "関連研究が論点を支える。",
                }
            ],
        },
    }


def reference_usage(
    record_id: str,
    title: str,
    *,
    selected: bool,
    quality_passed: bool = True,
    **metadata,
) -> dict:
    return {
        "policy_id": "reference-usage-v22",
        "usage_id": "usage:" + record_id,
        "candidate_key": "crossref:" + record_id,
        "query": "関連研究",
        "obligation_ids": ["OBL-1"],
        "disposition": (
            "consulted_or_cited" if selected else "machine_detected_related"
        ),
        "support_frontier": True,
        "selected_by_support_audit": selected,
        "passed_to_draft": selected,
        "discovery_quality_passed": quality_passed,
        "support_audits": (
            [{"status": "supports_subclaim", "host_grounded": True}]
            if selected
            else []
        ),
        "provider": "crossref",
        "provider_record_id": record_id,
        "source_type": "online_paper",
        "title": title,
        "authors": ["研究者"],
        "source_url": f"https://example.invalid/{record_id}",
        "confidence": 0.9,
        "provenance": {
            "doi": f"10.1000/{record_id}",
            "publication_title": "研究紀要",
            "publication_date": "2024-03-01",
        },
        **metadata,
    }


def reference_candidate(
    record_id: str,
    *,
    edition: str,
    is_translation: bool = False,
    translators: tuple[str, ...] = (),
) -> LiteratureCandidate:
    return LiteratureCandidate(
        provider="ndl_search",
        provider_record_id=record_id,
        title="メタヒストリー",
        authors=("ヘイドン・ホワイト",),
        publication_title=None,
        publisher="作品社",
        publication_date="2017",
        record_type="図書",
        doi=None,
        url=f"https://example.invalid/{record_id}",
        oa_url=None,
        oa_status=None,
        license_url=None,
        provider_payload={
            "isbn": f"97800000000{record_id[-2:]}",
            "edition": edition,
            "is_translation": is_translation,
            "translators": list(translators),
        },
    )


@pytest.fixture
def rewrite_case():
    from test_manuscript_rewrite import ManuscriptRewriteTests

    case = ManuscriptRewriteTests()
    case.setUp()
    try:
        yield case
    finally:
        case.tearDown()


def pending_v22_reference_proposal(
    case, candidates: list[LiteratureCandidate]
) -> dict:
    base_html = RevisionService(case.connection).canonical_html(case.base.revision_id)
    source_entry = case._reference_entry(
        resolution_status="pending",
        final_revision_blocked=True,
    )
    resolved = JapaneseReferenceService.resolve_with_candidates(
        source_entry, candidates, workflow_version=22
    )
    rendered, _ = JapaneseReferenceService.render_sections_v22(base_html, [resolved])
    context = {
        "workflow_version": 22,
        "reference_entries": [resolved],
        "reference_inventory": {"entries": [resolved]},
        "reference_quality_gate": JapaneseReferenceService.quality_report(
            rendered, [resolved], workflow_version=22
        ),
    }
    return case.service.persist_proposal(
        run_id=case._run_row(),
        document_id=case.document_id,
        project_id=case.project["id"],
        base_revision_id=case.base.revision_id,
        outline_node_id=None,
        instructions="V22 manifestation selection",
        use_rag=False,
        context=context,
        source_html=base_html,
        proposed_html=rendered,
        edit_summary={"applied_edit_count": 0},
        quality_status="needs_human_review",
    )


def test_v22_machine_references_use_only_validated_non_consulted_evidence() -> None:
    # A source already selected by the support audit and passed to drafting is
    # consulted even if a legacy deterministic discovery score was false.
    selected = reference_usage(
        "selected", "参照済み研究", selected=True, quality_passed=False
    )
    accepted = reference_usage("accepted", "関連研究", selected=False)
    rejected = reference_usage(
        "rejected", "未検証研究", selected=False, quality_passed=False
    )
    raw_hit = accepted_evidence("raw", "raw hit")
    context = {
        "reference_usage_records": [selected, accepted, rejected],
        "research_discoveries": [{"query": "関連研究", "candidates": [raw_hit]}],
    }

    result = WorkerRunner._validated_machine_reference_sources_v22(context, [])
    consulted = WorkerRunner._support_selected_reference_sources_v22(context)

    assert [item["title"] for item in result] == ["関連研究"]
    assert result[0]["year"] == "2024"
    assert result[0]["container_title"] == "研究紀要"
    assert [item["title"] for item in consulted] == ["参照済み研究"]
    assert consulted[0]["consulted_status"] == "support_audit_selected"
    assert "raw hit" not in [item["title"] for item in [*result, *consulted]]


def test_v22_renderer_adds_machine_section_without_breaking_paragraph_lock(worker_case) -> None:
    claimed = claimed_v22(worker_case)
    machine_entries = worker_case.worker._validated_machine_reference_sources_v22(
        {
            "reference_usage_records": [
                reference_usage("accepted", "関連研究", selected=False)
            ]
        },
        [],
    )
    context = {
        "proposed_html": HTML,
        "reference_entries": [],
        "machine_reference_entries": machine_entries,
        "reference_inventory": {"reference_section_count": 0},
        "reference_rule_set": {"id": "test"},
        "applied_edits": [],
        "paragraph_boundary_lock": ManuscriptDiscourseService.rendered_boundary_lock(HTML, {}),
    }

    rendered = worker_case.worker._manuscript_render_references(claimed, context).context

    assert "機械判別参考文献" in rendered["proposed_html"]
    assert "実際に参照したことを意味しません" in rendered["proposed_html"]
    assert rendered["reference_render_report"]["machine_section_rendered"] is True
    assert rendered["paragraph_boundary_lock"]["structural_sequence"] != (
        context["paragraph_boundary_lock"]["structural_sequence"]
    )

    checked = worker_case.worker._manuscript_reference_quality_gate(
        claimed, rendered
    ).context
    assert checked["reference_quality_gate"]["passed"] is True


def test_v22_support_selected_reference_renders_in_main_section_idempotently() -> None:
    selected = JapaneseReferenceService.reference_from_usage_v22(
        reference_usage(
            "selected-main",
            "論証に使用した研究",
            selected=True,
            pages="1-20",
        )
    )

    rendered, edits = JapaneseReferenceService.render_sections_v22(HTML, [selected])
    rerendered, second_edits = JapaneseReferenceService.render_sections_v22(
        rendered, [selected]
    )

    assert "参考文献" in rendered
    assert "論証に使用した研究" in rendered
    assert "機械判別参考文献" not in rendered
    assert 'data-reference-generated="v22-support-selected"' in rendered
    assert edits
    assert rerendered == rendered
    assert second_edits == []


def test_v22_provider_dedupe_preserves_distinct_manifestations() -> None:
    base = {
        "title": "同一著作",
        "authors": ["著者"],
        "author": "著者",
        "record_type": "book",
        "publisher": "出版社",
        "year": "2020",
        "parse_status": "parsed",
        "reference_role": "machine_detected_not_confirmed_consulted",
    }
    first_provider = {
        **base,
        "provider": "crossref",
        "provider_record_id": "first-a",
        "isbn": "9780000000001",
        "edition": "初版",
    }
    same_manifestation = {
        **base,
        "provider": "ndl_search",
        "provider_record_id": "first-b",
        "isbn": "978-0-000-00000-1",
        "edition": "初版",
    }
    revised = {
        **base,
        "provider": "ndl_search",
        "provider_record_id": "revised",
        "isbn": "9780000000002",
        "edition": "改訂版",
    }

    section = JapaneseReferenceService.bibliography_sections_v22(
        [], machine_entries=[first_provider, same_manifestation, revised]
    )[1]

    assert len(section["entries"]) == 2
    assert {item["edition"] for item in section["entries"]} == {"初版", "改訂版"}
    first = next(item for item in section["entries"] if item["edition"] == "初版")
    assert len(first["provider_records"]) == 2


def test_v22_machine_book_with_unknown_translation_status_blocks_approval() -> None:
    machine = JapaneseReferenceService.reference_from_usage_v22(
        reference_usage(
            "unknown-book-translation",
            "関連する書籍",
            selected=False,
            source_type="book",
            record_type="book",
            publisher="出版社",
            year="2020",
        )
    )
    rendered, _ = JapaneseReferenceService.render_sections_v22(
        HTML, [], machine_entries=[machine]
    )
    quality = JapaneseReferenceService.quality_report(
        rendered, [], workflow_version=22, machine_entries=[machine]
    )

    assert machine["translation_status"] == "undetermined"
    assert machine["final_revision_blocked"] is True
    assert machine["required_missing_fields"] == ["translation_status_confirmation"]
    assert any(
        issue["code"] == "reference.translation_status_confirmation_required"
        for issue in quality["issues"]
    )


def test_v22_machine_duplicate_cannot_downgrade_consulted_approval_state() -> None:
    consulted = {
        "reference_id": "reference:consulted",
        "parse_status": "parsed",
        "resolution_status": "resolved",
        "reference_role": "confirmed_consulted_manifestation",
        "consulted_status": "user_confirmed",
        "title": "同一著作",
        "author": "著者",
        "authors": ["著者"],
        "record_type": "book",
        "publisher": "出版社",
        "year": "2020",
        "edition": "初版",
        "isbn": "9780000000001",
        "is_translation": False,
        "translation_status": "confirmed_original",
        "translator_status": "not_required",
        "required_missing_fields": [],
        "final_revision_blocked": False,
        "conditional_proposal_allowed": False,
    }
    machine = {
        "title": "同一著作",
        "author": "著者",
        "authors": ["著者"],
        "record_type": "book",
        "publisher": "出版社",
        "year": "2020",
        "edition": "初版",
        "isbn": "978-0-000-00000-1",
        "provider": "ndl_search",
        "provider_record_id": "machine-copy",
    }

    sections = JapaneseReferenceService.bibliography_sections_v22(
        [consulted], machine_entries=[machine]
    )

    assert len(sections) == 1
    merged = sections[0]["entries"][0]
    assert merged["consulted_status"] == "user_confirmed"
    assert merged["translator_status"] == "not_required"
    assert merged["final_revision_blocked"] is False
    assert merged["required_missing_fields"] == []
    assert merged["machine_reconciliation_records"][0][
        "incoming_final_revision_blocked"
    ] is True
    assert merged["machine_reconciliation_records"][0]["classification_action"] == (
        "promoted_to_consulted_without_approval_downgrade"
    )


def test_v22_cross_section_dedupe_preserves_translation_conflict() -> None:
    base = {
        "title": "同一著作",
        "author": "著者",
        "authors": ["著者"],
        "record_type": "book",
        "publisher": "出版社",
        "year": "2020",
        "isbn": "9780000000001",
        "parse_status": "parsed",
    }
    consulted = {
        **base,
        "reference_id": "reference:original",
        "resolution_status": "resolved",
        "consulted_status": "user_confirmed",
        "is_translation": False,
        "translation_status": "confirmed_original",
        "translator_status": "not_required",
        "final_revision_blocked": False,
    }
    machine = {
        **base,
        "provider": "ndl_search",
        "provider_record_id": "translated-copy",
        "is_translation": True,
        "translation_status": "confirmed_translation",
        "translators": ["訳者"],
    }

    sections = JapaneseReferenceService.bibliography_sections_v22(
        [consulted], machine_entries=[machine]
    )

    assert [section["section_key"] for section in sections] == [
        "consulted_references",
        "machine_detected_references",
    ]
    assert sections[0]["entries"][0]["is_translation"] is False
    assert sections[1]["entries"][0]["is_translation"] is True


def test_v22_missing_translator_in_machine_section_blocks_final_approval(
    worker_case,
) -> None:
    machine = JapaneseReferenceService.reference_from_usage_v22(
        reference_usage(
            "translated-machine",
            "翻訳書",
            selected=False,
            source_type="book",
            record_type="book",
            publisher="出版社",
            year="2020",
            is_translation=True,
            translators=[],
        )
    )
    rendered, _ = JapaneseReferenceService.render_sections_v22(
        HTML, [], machine_entries=[machine]
    )
    quality = JapaneseReferenceService.quality_report(
        rendered, [], workflow_version=22, machine_entries=[machine]
    )
    completion = worker_case.worker._manuscript_completion_gate(
        {
            "workflow_version": "22",
            "proposed_html": rendered,
            "machine_reference_entries": [machine],
        }
    )

    assert machine["translator_status"] == "required_missing"
    assert quality["passed"] is False
    assert any(
        issue["code"] == "reference.translator_required"
        and issue.get("reference_role")
        == "machine_detected_not_confirmed_consulted"
        for issue in quality["issues"]
    )
    assert any(
        blocker["code"] == "reference.final_revision_confirmation_required"
        and blocker["details"]["reference_role"]
        == "machine_detected_not_confirmed_consulted"
        for blocker in completion["blocking_reasons"]
    )


def test_v22_support_audit_classification_is_checkpointed_and_stable(
    worker_case, monkeypatch
) -> None:
    claimed = claimed_v22(worker_case)
    claimed = replace(claimed, node_id="validate_manuscript_evidence_support")
    selected = {
        **reference_usage("selected", "関連研究", selected=False),
        "excerpt": "関連研究は論点を支える。",
        "discovery_quality": {"passed": True},
    }
    related = {
        **reference_usage("related", "関連研究補説", selected=False),
        "excerpt": "関連研究補説は別の観点を提示する。",
        "discovery_quality": {"passed": True},
    }
    noise = {
        **reference_usage("noise", "無関係", selected=False),
        "excerpt": "無関係な候補。",
        "discovery_quality": {"passed": False},
    }
    stored: dict[str, dict] = {}

    def save_evidence(_claimed, _context, record):
        key = str(record["provider"]) + ":" + str(record["provider_record_id"])
        stored[key] = {**record, "id": "evidence:" + key}

    def evidence_for_run(_repository, _run_id):
        return list(stored.values())

    def audit(_claimed, _phase, units, sources):
        key = next(
            item["candidate_key"]
            for item in sources
            if item["provider_record_id"] == "selected"
        )
        return {
            "verdicts": [
                {
                    "unit_id": unit["unit_id"],
                    "status": "supports_subclaim",
                    "citations": [
                        {
                            "candidate_key": key,
                            "quote": "関連研究は論点を支える。",
                        }
                    ],
                    "rationale": "引用箇所が主張を直接支える。",
                }
                for unit in units
            ]
        }

    monkeypatch.setattr(worker_case.worker, "_save_manuscript_evidence", save_evidence)
    monkeypatch.setattr(worker_case.worker, "_readonly_manuscript_audit", audit)
    monkeypatch.setattr(
        ManuscriptReasoningRepository, "evidence_for_run", evidence_for_run
    )
    context = {
        "reasoning_review_id": "review:test",
        "research_search_plan": [
            {
                "obligation_id": "OBL-1",
                "query": "関連研究",
                "subclaims": ["関連研究は論点を支える"],
            }
        ],
        "research_discoveries": [
            {"query": "関連研究", "candidates": [selected, related, noise]}
        ],
    }

    first = worker_case.worker._manuscript_validate_evidence_support(
        claimed, context
    ).context
    second = worker_case.worker._manuscript_validate_evidence_support(
        claimed, context
    ).context

    assert [
        item["disposition"] for item in first["reference_usage_records"]
    ] == ["consulted_or_cited", "machine_detected_related"]
    assert first["reference_usage_records"] == second["reference_usage_records"]
    assert first["reference_usage_classification_report"]["raw_discovery_candidate_count"] == 3
    assert first["reference_usage_classification_report"]["quality_rejected_count"] == 1
    assert first["reference_usage_classification_report"]["support_frontier_count"] == 2
    assert first["reference_usage_classification_report"]["draft_evidence_ids"]
    with worker_case.databases.state.connect() as connection:
        checkpoint_count = connection.execute(
            "SELECT COUNT(*) FROM manuscript_unit_checkpoints "
            "WHERE run_id=? AND phase='reference_usage:v22'",
            (claimed.run_id,),
        ).fetchone()[0]
    assert checkpoint_count == 1


def test_v22_completion_gate_names_reference_confirmation_blocker(worker_case) -> None:
    context = {
        "workflow_version": "22",
        "proposed_html": HTML,
        "reference_entries": [
            {
                "reference_id": "REF-1",
                "title": "翻訳書",
                "final_revision_blocked": True,
                "resolution_status": "metadata_incomplete",
                "translator_status": "required_missing",
                "required_missing_fields": ["translators"],
            }
        ],
    }

    report = worker_case.worker._manuscript_completion_gate(context)

    blocker = next(
        item
        for item in report["blocking_reasons"]
        if item["code"] == "reference.final_revision_confirmation_required"
    )
    assert blocker["details"]["required_fields"] == ["translators"]


def test_v22_service_confirms_multiple_manifestations_in_one_cas(rewrite_case) -> None:
    proposal = pending_v22_reference_proposal(
        rewrite_case,
        [
            reference_candidate("01", edition="初版"),
            reference_candidate("02", edition="改訂版"),
        ],
    )
    entry = proposal["context"]["reference_entries"][0]
    expected_ids = [candidate["candidate_id"] for candidate in entry["candidate_matches"]]

    with pytest.raises(FukamiruError, match="does not belong"):
        rewrite_case.service.select_reference(
            proposal["id"],
            entry["reference_id"],
            None,
            proposal["proposed_sha256"],
            candidate_ids=[expected_ids[0], "unknown-manifestation"],
        )
    unchanged = rewrite_case.service.get(proposal["id"])
    assert unchanged["proposed_sha256"] == proposal["proposed_sha256"]
    assert "selected_candidate_ids" not in unchanged["context"]["reference_entries"][0]

    selected = rewrite_case.service.select_reference(
        proposal["id"],
        entry["reference_id"],
        None,
        proposal["proposed_sha256"],
        candidate_ids=list(reversed(expected_ids)),
    )
    selected_entry = selected["context"]["reference_entries"][0]

    assert selected_entry["selected_candidate_ids"] == expected_ids
    assert len(selected_entry["expanded_reference_entries"]) == 2
    assert all(
        item["reference_role"] == "confirmed_consulted_manifestation"
        for item in selected_entry["expanded_reference_entries"]
    )
    assert selected["proposed_html"].count("ヘイドン・ホワイト『メタヒストリー』") == 2
    assert 'data-reference-generated="v22"' in selected["proposed_html"]
    assert selected["context"]["reference_inventory"]["entries"][0][
        "selected_candidate_ids"
    ] == expected_ids
    assert selected["context"]["reference_approval"]["final_revision_blocked"] is False
    assert selected["context"]["reference_selection_history"][-1][
        "candidate_ids"
    ] == expected_ids


def test_v22_service_keeps_missing_translator_as_final_approval_blocker(
    rewrite_case,
) -> None:
    proposal = pending_v22_reference_proposal(
        rewrite_case,
        [reference_candidate("03", edition="邦訳版", is_translation=True)],
    )
    entry = proposal["context"]["reference_entries"][0]
    candidate_id = entry["candidate_matches"][0]["candidate_id"]

    selected = rewrite_case.service.select_reference(
        proposal["id"],
        entry["reference_id"],
        None,
        proposal["proposed_sha256"],
        candidate_ids=[candidate_id],
    )

    selected_entry = selected["context"]["reference_entries"][0]
    assert selected_entry["translator_status"] == "required_missing"
    assert selected_entry["final_revision_blocked"] is True
    assert "translators" in selected_entry["required_missing_fields"]
    assert selected["context"]["reference_approval"]["final_revision_blocked"] is True
    with pytest.raises(FukamiruError, match="訳者が未確認"):
        rewrite_case.service.confirm(proposal["id"])
