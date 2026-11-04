from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import replace

import pytest

from fukamiru.documents.canonical_html import CanonicalHTMLValidator
from fukamiru.errors import FukamiruError
from fukamiru.revisions.bibliography_footnote_style import (
    FOOTNOTE_REFERENCE_RULESET,
    format_footnote_reference,
)
from fukamiru.revisions.bibliography_style import format_reference
from fukamiru.revisions.coherence import ManuscriptCoherenceService
from fukamiru.revisions.footnote_citations import (
    EvidenceFootnote,
    EvidenceFootnotePlanner,
    EvidenceFootnoteRenderer,
)

TRANSLATED_BOOK_REFERENCE = {
    "reference_id": "reference:fixture-ef6f62fd3d7e3f76:2023",
    "title": "架空の協働研究",
    "record_type": "book",
    "publication_language": "ja",
    "publisher": "架空出版",
    "publication_place": "東京",
    "year": "2023",
    "edition": "新装版",
    "bibliography_people": [
        {
            "role": "author",
            "name_profile": "literal",
            "literal": "山田",
            "authority_raw": "山田",
        },
        {
            "role": "translator",
            "role_subtype": "supervising_translator",
            "name_profile": "japanese_native",
            "family": "田中",
            "given": "一郎",
        },
        {
            "role": "translator",
            "role_subtype": "supervising_translator",
            "name_profile": "japanese_native",
            "family": "鈴木",
            "given": "二郎",
        },
    ],
}

EVIDENCE_REFERENCE = {
    "source_asset_id": "fixture-uuid-3ec01720a65aa6f4",
    "source_span_ids": [
        "fixture-uuid-7f9b86810c91cb68",
        "fixture-uuid-ce8349f5e54ddf81",
    ],
    "title": "山田『架空の協働研究』",
    "record_type": "chapter",
    "publication_language": "ja",
    "container_title": "架空の活動研究",
    "publisher": "架空学術社",
    "year": "2016",
    "bibliography_people": [
        {
            "role": "author",
            "name_profile": "japanese_native",
            "family": "佐々木",
            "given": "三郎",
        },
        {
            "role": "editor",
            "name_profile": "japanese_native",
            "family": "高橋",
            "given": "四郎",
        },
        {
            "role": "editor",
            "name_profile": "japanese_native",
            "family": "中村",
            "given": "五郎",
        },
        {
            "role": "editor",
            "name_profile": "japanese_native",
            "family": "小林",
            "given": "六郎",
        },
    ],
}

PARAGRAPH = (
    "共同作業を二人の対話だけで説明すると、周囲の役割が見えにくくなる。"
    "山田が『架空の協働研究』で役割の配分を検討したことを踏まえるなら、"
    "記録係と観察者を分けて考える必要がある。"
    "参加者の関係と運営の条件は、同じ活動の異なる側面である。"
)
ANCHOR = (
    "山田が『架空の協働研究』で役割の配分を検討したことを踏まえるなら、"
    "記録係と観察者を分けて考える必要がある。"
)
HTML = (
    '<article id="D" lang="ja">'
    '<section id="S"><h2 id="SH">本文</h2>'
    f'<p id="FIXTURE-BLOCK-584ecb2fedae4add">{PARAGRAPH}</p>'
    '<p id="P-PROTECTED"><q>既存引用</q><sup id="OLD-NOTE">9</sup></p>'
    "</section>"
    '<section id="R"><h2 id="RH">参考文献</h2>'
    '<p id="RP"><cite>既存文献。</cite></p></section>'
    "</article>"
)


def _citation() -> EvidenceFootnote:
    return EvidenceFootnote(
        block_id="FIXTURE-BLOCK-584ecb2fedae4add",
        evidence_ids=(
            "fixture-uuid-856cd5a1ec11233b",
            "fixture-uuid-c43772502db07e1a",
        ),
        source_span_ids=(
            "fixture-uuid-7f9b86810c91cb68",
            "fixture-uuid-ce8349f5e54ddf81",
        ),
        reference=EVIDENCE_REFERENCE,
        anchor_text=ANCHOR,
        locator="268-269",
    )


def test_japanese_translated_book_uses_dedicated_footnote_order() -> None:
    styled = format_footnote_reference(TRANSLATED_BOOK_REFERENCE, locator="274-275")

    assert styled.supported
    assert styled.text == (
        "山田『架空の協働研究』、"
        "田中一郎・鈴木二郎監訳、新装版、架空出版、2023年、274-275頁。"
    )
    assert FOOTNOTE_REFERENCE_RULESET["punctuation_is_normative"] is False
    assert FOOTNOTE_REFERENCE_RULESET["field_order"]["book"][:3] == [
        "creator",
        "title",
        "translator_or_editor",
    ]


def test_resolved_nonstandard_work_can_use_provisional_footnote_and_bibliography() -> None:
    reference = {
        "reference_id": "reference:nonstandard-1",
        "resolution_status": "resolved",
        "record_type": "nonstandard_contribution",
        "publication_language": "ja",
        "title": "架空資料の比較分析",
        "evidence_ids": ["evidence:nonstandard-1"],
        "bibliography_people": [{
            "role": "author", "name_profile": "japanese_native",
            "family": "山田", "given": "花子",
        }],
        "provisional_bibliography_review": True,
    }

    footnote = format_footnote_reference(reference)
    bibliography = format_reference(reference)
    assert footnote.supported and bibliography.supported
    assert "山田花子" in footnote.text
    assert "架空資料の比較分析" in bibliography.text
    assert "書誌種別・刊行情報要確認" in footnote.text
    assert footnote.review_reasons == ("record_kind_unconfirmed",)


def test_resolved_nonstandard_contribution_uses_retrieved_container_year_and_pages() -> None:
    reference = {
        "reference_id": "reference:nonstandard-1",
        "resolution_status": "resolved",
        "record_type": "nonstandard_contribution",
        "evidence_ids": ["evidence:nonstandard-1"],
        "publication_language": "ja",
        "title": "架空資料の比較分析",
        "container_title": "架空論集",
        "year": "2022",
        "pages": "131-176",
        "bibliography_people": [{
            "role": "author", "name_profile": "japanese_native",
            "family": "山田", "given": "花子",
        }],
    }
    styled = format_footnote_reference(reference)
    assert styled.supported
    assert styled.review_reasons == ()
    assert styled.text == (
        "山田花子「架空資料の比較分析」、"
        "『架空論集』、2022年、131-176頁。"
    )
    rendered = EvidenceFootnoteRenderer.render(HTML, [EvidenceFootnote(
        block_id="FIXTURE-BLOCK-584ecb2fedae4add",
        evidence_ids=("evidence:nonstandard-1",),
        reference=reference,
        anchor_text=ANCHOR,
    )])
    assert styled.text in "".join(ET.fromstring(rendered.html).itertext())
    assert "書誌種別・刊行情報要確認" not in rendered.html


@pytest.mark.parametrize("change", [
    {"reference_id": ""},
    {"resolution_status": "ambiguous"},
    {"title": ""},
    {"bibliography_people": []},
    {"provisional_bibliography_review": False},
])
def test_nonstandard_provisional_display_does_not_hide_identity_failure(change: dict) -> None:
    reference = {
        "reference_id": "reference:nonstandard-1",
        "resolution_status": "resolved",
        "record_type": "nonstandard_contribution",
        "publication_language": "ja",
        "title": "架空資料の比較分析",
        "bibliography_people": [{
            "role": "author", "name_profile": "japanese_native",
            "family": "山田", "given": "花子",
        }],
        "provisional_bibliography_review": True,
        **change,
    }

    assert not format_footnote_reference(reference).supported


def test_unknown_record_kind_uses_provisional_bibliography_only_with_resolved_identity() -> None:
    reference = {
        "reference_id": "reference:unclassified-1",
        "resolution_status": "resolved",
        "record_type": "unclassified_media",
        "publication_language": "ja",
        "title": "特別な資料",
        "bibliography_people": [{
            "role": "author", "name_profile": "japanese_native",
            "family": "山田", "given": "花子",
        }],
        "provisional_bibliography_review": True,
    }

    assert format_footnote_reference(reference).supported
    assert format_reference(reference).supported
    assert "書誌種別・刊行情報要確認" in format_reference(reference).text
    assert not format_reference({**reference, "resolution_status": "ambiguous"}).supported


def test_renderer_keeps_nonstandard_citation_link_with_provisional_note() -> None:
    reference = {
        "reference_id": "reference:nonstandard-1",
        "resolution_status": "resolved",
        "record_type": "nonstandard_contribution",
        "publication_language": "ja",
        "title": "架空資料の比較分析",
        "evidence_ids": ["evidence:nonstandard-1"],
        "bibliography_people": [{
            "role": "author", "name_profile": "japanese_native",
            "family": "山田", "given": "花子",
        }],
        "provisional_bibliography_review": True,
    }
    citation = EvidenceFootnote(
        block_id="FIXTURE-BLOCK-584ecb2fedae4add",
        evidence_ids=("evidence:nonstandard-1",),
        reference=reference,
        anchor_text=ANCHOR,
    )

    rendered = EvidenceFootnoteRenderer.render(HTML, [citation])
    assert "書誌種別・刊行情報要確認" in rendered.html
    assert rendered.citation_event_drafts[0]["bibliographic_record_id"] == (
        "reference:nonstandard-1"
    )
    with pytest.raises(FukamiruError, match="year_unconfirmed"):
        EvidenceFootnoteRenderer.render(
            HTML,
            [EvidenceFootnote(
                block_id=citation.block_id,
                evidence_ids=citation.evidence_ids,
                reference={**reference, "resolution_status": "ambiguous"},
                anchor_text=ANCHOR,
            )],
        )


def test_japanese_footnote_keeps_explicit_source_language_author_name() -> None:
    styled = format_footnote_reference(
        {
            "title": "動物化するポストモダン",
            "record_type": "book",
            "publication_language": "ja",
            "publisher": "講談社",
            "year": "2001",
            "author": "東浩紀",
            "authors": ["Hiroki Azuma"],
            "provenance": {
                "author": {"kind": "source_explicit", "source": "memo_annotation"}
            },
            "bibliography_people": [
                {
                    "role": "author",
                    "name_profile": "western_latin",
                    "family": "Azuma",
                    "given": "Hiroki",
                }
            ],
        }
    )

    assert styled.supported
    assert styled.text == "東浩紀『動物化するポストモダン』、講談社、2001年。"


def test_western_article_uses_creator_title_container_volume_year_locator_order() -> None:
    styled = format_footnote_reference(
        {
            "title": "Remarques sur l'existence réelle d'une matière monoatomique",
            "record_type": "article",
            "publication_language": "fr",
            "container_title": "Compte rendus hebdomadaires des séances",
            "volume": "82",
            "year": "1876",
            "bibliography_people": [
                {
                    "role": "author",
                    "name_profile": "western_latin",
                    "given": "Marcelin",
                    "family": "Berthelot",
                }
            ],
        },
        locator="1129",
    )

    assert styled.supported
    assert styled.text == (
        "Marcelin Berthelot, « Remarques sur l'existence réelle d'une matière "
        "monoatomique », Compte rendus hebdomadaires des séances, 82, 1876, p. 1129."
    )
    assert ("Compte rendus hebdomadaires des séances", True) in styled.spans


def test_renderer_adds_marker_body_and_event_drafts_without_touching_existing_markup() -> None:
    result = EvidenceFootnoteRenderer.render(HTML, [_citation()])
    report = CanonicalHTMLValidator().assert_valid(result.html)
    root = ET.fromstring(result.html)
    target = next(
        element
        for element in root.iter()
        if element.get("id") == "FIXTURE-BLOCK-584ecb2fedae4add"
    )
    marker = next(target.iter("sup"))
    sections = list(root)

    assert report.valid
    assert marker.find("a").text == "1"
    assert marker.tail.startswith("。参加者の関係")
    assert "data-evidence-ids" in marker.attrib
    assert sections[-2].get("data-citation-section") == "evidence-footnotes-v1"
    assert "佐々木三郎" in "".join(sections[-2].itertext())
    assert "高橋四郎・中村五郎・小林六郎編" in "".join(sections[-2].itertext())
    assert "268-269頁" in "".join(sections[-2].itertext())
    note_item = next(sections[-2].iter("li"))
    assert note_item.get("data-source-asset-id") == ("fixture-uuid-3ec01720a65aa6f4")
    assert note_item.get("data-reference-id") is None
    assert sections[-1].find("h2").text == "参考文献"
    bibliography_rows = list(sections[-1].iter("p"))
    assert len(bibliography_rows) == 2
    assert bibliography_rows[-1].get("data-reference-generated") == ("evidence-footnote-v1")
    assert "".join(bibliography_rows[-1].itertext()) == (
        "佐々木三郎「山田『架空の協働研究』」、"
        "高橋四郎・中村五郎・小林六郎編、『架空の活動研究』、"
        "架空学術社、2016年。"
    )
    assert any(
        edit["operation"] == "insert_footnote_reference_in_terminal_bibliography"
        for edit in result.edits
    )
    assert '<q>既存引用</q><sup id="OLD-NOTE">9</sup>' in result.html
    assert [event["source_span_id"] for event in result.citation_event_drafts] == [
        "fixture-uuid-7f9b86810c91cb68",
        "fixture-uuid-ce8349f5e54ddf81",
    ]
    assert all(event["citation_kind"] == "paraphrase" for event in result.citation_event_drafts)
    manifest = ManuscriptCoherenceService.document_manifest(result.html)
    assert manifest["terminal_roles"] == ["notes", "references"]
    assert manifest["reference_section_count"] == 1
    assert manifest["last_section_role"] == "references"
    assert not any(
        block_id.startswith("D.evidence-footnotes") for block_id in manifest["editable_block_ids"]
    )


def test_renderer_is_idempotent_for_the_same_evidence_note() -> None:
    first = EvidenceFootnoteRenderer.render(HTML, [_citation()])
    second = EvidenceFootnoteRenderer.render(first.html, [_citation()])

    assert second.html == first.html
    assert second.edits == ()
    assert second.citation_event_drafts == first.citation_event_drafts


def test_renderer_refreshes_generated_note_when_identified_author_changes() -> None:
    first_reference = {
        **EVIDENCE_REFERENCE,
        "reference_id": "reference:stable-author",
    }
    corrected_reference = {
        **first_reference,
        "bibliography_people": [
            {
                "role": "author", "name_profile": "japanese_native",
                "family": "山田", "given": "花子",
            },
        ],
    }
    first = EvidenceFootnoteRenderer.render(
        HTML, [replace(_citation(), reference=first_reference)]
    )
    second = EvidenceFootnoteRenderer.render(
        first.html, [replace(_citation(), reference=corrected_reference)]
    )
    assert "山田花子" in second.html
    assert second.html.count('data-note-number="1"') == 1
    assert any(edit["operation"] == "refresh_evidence_footnote_body" for edit in second.edits)
    assert EvidenceFootnoteRenderer.render(
        second.html, [replace(_citation(), reference=corrected_reference)]
    ).html == second.html


def test_repeated_footnotes_share_one_terminal_bibliography_entry() -> None:
    repeated = EvidenceFootnote(
        block_id="P-PROTECTED",
        evidence_ids=("E-2",),
        reference={**EVIDENCE_REFERENCE, "evidence_ids": ["E-2"]},
    )

    result = EvidenceFootnoteRenderer.render(HTML, [_citation(), repeated])
    root = ET.fromstring(result.html)
    bibliography = list(root)[-1]
    generated = [
        paragraph
        for paragraph in bibliography.iter("p")
        if paragraph.get("data-reference-generated") == "evidence-footnote-v1"
    ]

    assert len(generated) == 1
    assert len(list(root)[-2].find("ol")) == 2


def test_all_distinct_footnote_works_are_collected_in_terminal_bibliography() -> None:
    second_reference = {
        "title": "Care and Dependency",
        "record_type": "article",
        "publication_language": "en",
        "container_title": "Ethics",
        "volume": "12",
        "issue": "3",
        "year": "2020",
        "evidence_ids": ["E-SECOND"],
        "bibliography_people": [
            {
                "role": "author",
                "name_profile": "western_latin",
                "given": "Jane",
                "family": "Doe",
            }
        ],
    }
    second = EvidenceFootnote(
        block_id="P-PROTECTED",
        evidence_ids=("E-SECOND",),
        reference=second_reference,
        locator="44-45",
    )

    result = EvidenceFootnoteRenderer.render(HTML, [_citation(), second])
    root = ET.fromstring(result.html)
    bibliography_text = "".join(list(root)[-1].itertext())

    assert "佐々木三郎" in bibliography_text
    assert "Doe, Jane, “Care and Dependency”" in bibliography_text
    assert "44-45" not in bibliography_text
    assert (
        len(
            [
                paragraph
                for paragraph in list(root)[-1].iter("p")
                if paragraph.get("data-reference-generated") == "evidence-footnote-v1"
            ]
        )
        == 2
    )


def test_renderer_creates_terminal_bibliography_when_source_has_none() -> None:
    without_bibliography = HTML[: HTML.index('<section id="R">')] + "</article>"

    result = EvidenceFootnoteRenderer.render(without_bibliography, [_citation()])
    root = ET.fromstring(result.html)

    assert list(root)[-2].get("data-citation-section") == "evidence-footnotes-v1"
    assert list(root)[-1].get("data-reference-role") == "consulted-or-cited"
    assert list(root)[-1].find("h2").text == "参考文献"
    assert "佐々木三郎" in "".join(list(root)[-1].itertext())


def test_empty_citation_list_does_not_create_an_empty_bibliography() -> None:
    without_bibliography = HTML[: HTML.index('<section id="R">')] + "</article>"

    result = EvidenceFootnoteRenderer.render(without_bibliography, [])

    assert result.html == without_bibliography
    assert result.edits == ()


def test_workflow_planner_maps_applied_evidence_to_final_reference_and_pages() -> None:
    reference = {
        **EVIDENCE_REFERENCE,
        "reference_id": "reference:ishida-2016",
        "reference_usage": {
            "evidence_id": "E-WORKFLOW",
        },
    }
    evidence = {
        "id": "E-WORKFLOW",
        "provider": "knowledge_base",
        "provider_record_id": "chunk-1",
        "provenance": {
            "citation": {
                "source_span_id": "SPAN-WORKFLOW",
                "source_asset_id": "ASSET-WORKFLOW",
                "start_page": 268,
                "end_page": 269,
            }
        },
    }

    plan = EvidenceFootnotePlanner.plan(
        HTML,
        applied_edits=[
            {
                "operation": "replace_text",
                "block_id": "FIXTURE-BLOCK-584ecb2fedae4add",
                "output_block_id": "FIXTURE-BLOCK-584ecb2fedae4add",
                "evidence_ids": ["E-WORKFLOW"],
            }
        ],
        evidence_context=[evidence],
        reference_entries=[reference],
    )

    assert plan.evidence_edit_count == 1
    assert plan.cited_evidence_count == 1
    assert plan.cited_reference_count == 1
    assert plan.citations[0].locator == "268-269"
    assert plan.citations[0].source_span_ids == ("SPAN-WORKFLOW",)
    assert plan.citations[0].reference["source_asset_id"] == "ASSET-WORKFLOW"


def test_workflow_planner_uses_inserted_output_paragraph_id() -> None:
    inserted_html = HTML.replace(
        '</section><section id="R">',
        '<p id="P-INSERTED">追加文章。</p></section><section id="R">',
    )
    reference = {
        **EVIDENCE_REFERENCE,
        "reference_usage": {"evidence_id": "E-INSERTED"},
    }
    evidence = {
        "id": "E-INSERTED",
        "provider": "knowledge_base",
        "provider_record_id": "chunk-2",
        "provenance": {"citation": {"source_span_id": "SPAN-INSERTED"}},
    }

    plan = EvidenceFootnotePlanner.plan(
        inserted_html,
        applied_edits=[
            {
                "operation": "insert_paragraph_after",
                "block_id": "P-PROTECTED",
                "output_block_id": "P-INSERTED",
                "evidence_ids": ["E-INSERTED"],
            }
        ],
        evidence_context=[evidence],
        reference_entries=[reference],
    )

    assert plan.citations[0].block_id == "P-INSERTED"


def test_authoring_planner_anchors_the_cited_sentence() -> None:
    reference = {
        **EVIDENCE_REFERENCE,
        "evidence_ids": ["E-WORK"],
    }
    evidence = {
        "id": "E-WORK",
        "title": "架空の協働研究",
        "excerpt": "山田『架空の協働研究』",
    }
    plan = EvidenceFootnotePlanner.plan(
        HTML,
        applied_edits=[{
            "operation": "replace_text",
            "block_id": "FIXTURE-BLOCK-584ecb2fedae4add",
            "evidence_ids": ["E-WORK"],
        }],
        evidence_context=[evidence],
        reference_entries=[reference],
        require_distinct_anchors=True,
    )
    assert plan.citations[0].anchor_text == ANCHOR
    rendered = EvidenceFootnoteRenderer.render(HTML, list(plan.citations))
    paragraph = next(
        item for item in ET.fromstring(rendered.html).iter("p")
        if item.get("id") == "FIXTURE-BLOCK-584ecb2fedae4add"
    )
    assert list(paragraph.iter("sup"))[0].tail.startswith("。参加者の関係")


def test_visible_citation_units_skips_unterminated_excerpt_for_valid_title() -> None:
    evidence = {
        "id": "E-WHITE",
        "title": "メタヒストリー",
        "excerpt": "末尾の引用『構成の営み』",
    }
    text = "ホワイトは『メタヒストリー』で歴史叙述を論じた。『構成の営み』"

    placement = EvidenceFootnotePlanner.visible_citation_units(
        text,
        [evidence],
        {"title": "メタヒストリー"},
    )

    assert placement is not None
    assert placement["sentence"] == "ホワイトは『メタヒストリー』で歴史叙述を論じた。"


def test_visible_citation_units_normalizes_japanese_bibliographic_punctuation() -> None:
    evidence = {
        "id": "E-SENNETT",
        "title": "クラフツマン : 作ることは考えることである",
        "authors": ["セネット、リチャード"],
        "excerpt": "セネット『クラフツマン』",
    }
    text = (
        "リチャード・セネット『クラフツマン――作ることは考えることである』は、"
        "制作を思考の営みとして捉える。"
    )

    placement = EvidenceFootnotePlanner.visible_citation_units(
        text,
        [evidence],
        {"title": evidence["title"]},
    )

    assert placement is not None
    assert placement["sentence"] == text


def test_visible_citation_units_rejects_repeated_normalized_source_anchor() -> None:
    evidence = {
        "id": "E-SENNETT",
        "title": "クラフツマン : 作ることは考えることである",
        "authors": ["セネット、リチャード"],
        "excerpt": "セネット『クラフツマン』",
    }
    text = (
        "リチャード・セネット『クラフツマン――作ることは考えることである』は重要だ。"
        "また、リチャード・セネット『クラフツマン――作ることは考えることである』を参照する。"
    )

    placement = EvidenceFootnotePlanner.visible_citation_units(
        text,
        [evidence],
        {"title": evidence["title"]},
    )

    assert placement is None


def test_visible_citation_units_ignores_markdown_note_definition_without_changing_text() -> None:
    evidence = {
        "id": "E-RANCIERE",
        "title": "感性的なもののパルタージュ――美学と政治",
        "authors": ["ジャック・ランシエール"],
    }
    text = (
        "ジャック・ランシエール『感性的なもののパルタージュ――美学と政治』を参照する。"
        "[^1] 次の文に進む。\n\n"
        "[^1]: ジャック・ランシエール『感性的なもののパルタージュ――美学と政治』。"
    )

    placement = EvidenceFootnotePlanner.visible_citation_units(
        text, [evidence], evidence
    )

    assert placement is not None
    assert placement["sentence"] == (
        "ジャック・ランシエール『感性的なもののパルタージュ――美学と政治』を参照する。"
    )
    assert text.endswith("[^1]: ジャック・ランシエール『感性的なもののパルタージュ――美学と政治』。")


def test_authoring_planner_rejects_paragraph_end_note_stack() -> None:
    references = [
        {**EVIDENCE_REFERENCE, "reference_id": "R-1", "evidence_ids": ["E-1"]},
        {**TRANSLATED_BOOK_REFERENCE, "reference_id": "R-2", "evidence_ids": ["E-2"]},
    ]
    with pytest.raises(FukamiruError, match="unique visible source mention"):
        EvidenceFootnotePlanner.plan(
            HTML,
            applied_edits=[{
                "operation": "replace_text",
                "block_id": "P-PROTECTED",
                "evidence_ids": ["E-1", "E-2"],
            }],
            evidence_context=[{"id": "E-1"}, {"id": "E-2"}],
            reference_entries=references,
            require_distinct_anchors=True,
        )


def test_authoring_planner_groups_two_sources_into_one_sentence_note() -> None:
    html = (
        '<article id="D" lang="ja"><section id="S"><h2 id="H">本文</h2>'
        '<p id="P">甲『第一作』と乙『第二作』の議論を比較する。</p>'
        '</section></article>'
    )
    refs = [
        {**EVIDENCE_REFERENCE, "reference_id": "R-1", "title": "第一作",
         "evidence_ids": ["E-1"]},
        {**EVIDENCE_REFERENCE, "reference_id": "R-2", "title": "第二作",
         "evidence_ids": ["E-2"]},
    ]
    plan = EvidenceFootnotePlanner.plan(
        html,
        applied_edits=[{"operation": "replace_text", "block_id": "P",
                        "evidence_ids": ["E-1", "E-2"]}],
        evidence_context=[
            {"id": "E-1", "title": "第一作"},
            {"id": "E-2", "title": "第二作"},
        ],
        reference_entries=refs,
        require_distinct_anchors=True,
    )
    assert len(plan.citations) == 1
    assert plan.citations[0].evidence_ids == ("E-1", "E-2")
    assert len(plan.citations[0].references) == 2
    rendered = EvidenceFootnoteRenderer.render(html, list(plan.citations))
    root = ET.fromstring(rendered.html)
    assert len(list(root.iter("sup"))) == 1
    generated_notes = [
        item for item in root.iter("li") if item.get("data-note-number")
    ]
    assert len(generated_notes) == 1
    assert len(list(generated_notes[0].iter("cite"))) == 2
    assert len(rendered.citation_event_drafts) == 2
    assert {
        item["bibliographic_record_id"]
        for item in rendered.citation_event_drafts
    } == {"R-1", "R-2"}


def test_semantic_title_tags_anchor_multiple_works_without_overlapping_text_ranges() -> None:
    html = (
        '<article id="D" lang="ja"><section id="S"><h2 id="H">本文</h2>'
        '<p id="P">比較枠として'
        '<cite data-reference-id="R-1">『第一作』</cite>と'
        '<cite data-reference-id="R-2">『第二作』</cite>を用いる。</p>'
        '</section></article>'
    )
    refs = [
        {**EVIDENCE_REFERENCE, "reference_id": "R-1", "title": "第一作",
         "evidence_ids": ["E-1"]},
        {**EVIDENCE_REFERENCE, "reference_id": "R-2", "title": "第二作",
         "evidence_ids": ["E-2"]},
    ]
    plan = EvidenceFootnotePlanner.plan(
        html,
        applied_edits=[{
            "operation": "replace_text",
            "block_id": "P",
            "evidence_ids": ["E-1", "E-2"],
        }],
        evidence_context=[{"id": "E-1"}, {"id": "E-2"}],
        reference_entries=refs,
        require_distinct_anchors=True,
    )

    assert [item.anchor_reference_id for item in plan.citations] == ["R-1", "R-2"]
    rendered = EvidenceFootnoteRenderer.render(html, list(plan.citations))
    paragraph = next(
        item for item in ET.fromstring(rendered.html).iter("p")
        if item.get("id") == "P"
    )
    cites = list(paragraph.iter("cite"))
    markers = list(paragraph.iter("sup"))
    assert [item.get("data-reference-id") for item in cites] == ["R-1", "R-2"]
    assert len(markers) == 2
    children = list(paragraph)
    assert children[children.index(cites[0]) + 1] is markers[0]
    assert children[children.index(cites[1]) + 1] is markers[1]
    assert len(rendered.citation_event_drafts) == 2

    v32 = EvidenceFootnoteRenderer.render(
        html, list(plan.citations), place_before_punctuation=True
    )
    v32_paragraph = next(
        item for item in ET.fromstring(v32.html).iter("p") if item.get("id") == "P"
    )
    v32_markers = list(v32_paragraph.iter("sup"))
    assert [marker.tail or "" for marker in v32_markers] == ["", "。"]
    assert [cite.tail for cite in v32_paragraph.iter("cite")] == ["と", "を用いる"]
    assert len(v32.citation_event_drafts) == len(rendered.citation_event_drafts)


def test_authoring_planner_allows_one_note_per_comma_clause() -> None:
    html = (
        '<article id="D" lang="ja"><section id="S"><h2 id="H">本文</h2>'
        '<p id="P">甲『第一作』は配置を論じ、乙『第二作』は受容を論じる。</p>'
        '</section></article>'
    )
    refs = [
        {**EVIDENCE_REFERENCE, "reference_id": "R-1", "title": "第一作",
         "evidence_ids": ["E-1"]},
        {**EVIDENCE_REFERENCE, "reference_id": "R-2", "title": "第二作",
         "evidence_ids": ["E-2"]},
    ]
    plan = EvidenceFootnotePlanner.plan(
        html,
        applied_edits=[{"operation": "replace_text", "block_id": "P",
                        "evidence_ids": ["E-2", "E-1"]}],
        evidence_context=[
            {"id": "E-1", "title": "第一作"},
            {"id": "E-2", "title": "第二作"},
        ],
        reference_entries=refs,
        require_distinct_anchors=True,
    )
    assert [item.anchor_text for item in plan.citations] == [
        "甲『第一作』は配置を論じ、", "乙『第二作』は受容を論じる。"
    ]
    assert [item.evidence_ids for item in plan.citations] == [("E-1",), ("E-2",)]
    rendered = EvidenceFootnoteRenderer.render(html, list(plan.citations))
    paragraph = next(
        item for item in ET.fromstring(rendered.html).iter("p")
        if item.get("id") == "P"
    )
    markers = list(paragraph.iter("sup"))
    assert len(markers) == 2
    assert markers[0].tail.startswith("、乙")
    assert markers[1].tail == "。"

    v32 = EvidenceFootnoteRenderer.render(
        html, list(plan.citations), place_before_punctuation=True
    )
    v32_paragraph = next(
        item for item in ET.fromstring(v32.html).iter("p") if item.get("id") == "P"
    )
    assert [marker.tail[:1] for marker in v32_paragraph.iter("sup")] == ["、", "。"]


def test_renderer_refuses_incomplete_footnote_metadata() -> None:
    incomplete = EvidenceFootnote(
        block_id="FIXTURE-BLOCK-584ecb2fedae4add",
        evidence_ids=("E-1",),
        reference={"title": "資料", "record_type": "book", "evidence_ids": ["E-1"]},
    )

    with pytest.raises(FukamiruError, match="requires review"):
        EvidenceFootnoteRenderer.render(HTML, [incomplete])


def test_paragraph_end_marker_does_not_move_existing_inline_markup() -> None:
    citation = EvidenceFootnote(
        block_id="P-PROTECTED",
        evidence_ids=("E-1",),
        reference={**EVIDENCE_REFERENCE, "evidence_ids": ["E-1"]},
    )

    result = EvidenceFootnoteRenderer.render(HTML, [citation])

    assert '<q>既存引用</q><sup id="OLD-NOTE">9</sup><sup id="FNREF-' in result.html


@pytest.mark.parametrize("punctuation", ["、", "，", ",", "。"])
def test_v32_paragraph_end_marker_precedes_comma_or_period(punctuation: str) -> None:
    html = (
        '<article id="D" lang="ja"><section id="S"><h2 id="H">本文</h2>'
        f'<p id="P">出典に基づく記述{punctuation}</p></section></article>'
    )
    citation = EvidenceFootnote(
        block_id="P",
        evidence_ids=("E-1",),
        reference={**EVIDENCE_REFERENCE, "evidence_ids": ["E-1"]},
    )

    rendered = EvidenceFootnoteRenderer.render(
        html, [citation], place_before_punctuation=True
    )
    paragraph = next(
        item for item in ET.fromstring(rendered.html).iter("p") if item.get("id") == "P"
    )
    marker = next(paragraph.iter("sup"))
    assert paragraph.text == "出典に基づく記述"
    assert marker.tail == punctuation
    assert EvidenceFootnoteRenderer.render(
        rendered.html, [citation], place_before_punctuation=True
    ).html == rendered.html


def test_v32_realigns_marker_after_prose_redraft_without_changing_words() -> None:
    html = (
        '<article id="D" lang="ja"><section id="S"><h2 id="H">本文</h2>'
        '<p id="P"><cite data-reference-id="R-1">『第一作』</cite>'
        '<sup id="FNREF-ONE"><a href="#FN-ONE">1</a></sup>は論点を示す、次へ。</p>'
        '<p id="Q"><cite data-reference-id="R-2">『第二作』</cite>'
        '<sup id="FNREF-TWO"><a href="#FN-TWO">2</a></sup>は続く</p>'
        '</section><section id="N"><h2 id="NH">注</h2><ol id="NL">'
        '<li id="FN-ONE" data-note-number="1"><p id="NP1">第一作</p></li>'
        '<li id="FN-TWO" data-note-number="2"><p id="NP2">第二作</p></li>'
        '</ol></section></article>'
    )
    aligned, unresolved = (
        EvidenceFootnoteRenderer.align_existing_markers_before_punctuation(html)
    )
    assert unresolved == ("Q",)
    paragraphs = {item.get("id"): item for item in ET.fromstring(aligned).iter("p")}
    marker = next(paragraphs["P"].iter("sup"))
    assert marker.tail == "、次へ。"
    assert list(paragraphs["P"].iter("cite"))[0].tail == "は論点を示す"
    original_root, aligned_root = ET.fromstring(html), ET.fromstring(aligned)
    for root in (original_root, aligned_root):
        for note_marker in root.iter("sup"):
            for link in note_marker:
                link.text = ""
    assert "".join(original_root.itertext()) == "".join(aligned_root.itertext())


def test_v31_paragraph_end_comma_placement_is_preserved() -> None:
    html = (
        '<article id="D" lang="ja"><section id="S"><h2 id="H">本文</h2>'
        '<p id="P">出典に基づく記述、</p></section></article>'
    )
    citation = EvidenceFootnote(
        block_id="P",
        evidence_ids=("E-1",),
        reference={**EVIDENCE_REFERENCE, "evidence_ids": ["E-1"]},
    )

    rendered = EvidenceFootnoteRenderer.render(html, [citation])
    paragraph = next(
        item for item in ET.fromstring(rendered.html).iter("p") if item.get("id") == "P"
    )
    assert paragraph.text == "出典に基づく記述、"
    assert next(paragraph.iter("sup")).tail is None


def test_renderer_rejects_reference_not_linked_to_evidence() -> None:
    citation = EvidenceFootnote(
        block_id="FIXTURE-BLOCK-584ecb2fedae4add",
        evidence_ids=("E-UNRELATED",),
        reference=TRANSLATED_BOOK_REFERENCE,
    )

    with pytest.raises(FukamiruError, match="not explicitly linked"):
        EvidenceFootnoteRenderer.render(HTML, [citation])
