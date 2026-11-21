from __future__ import annotations

from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

from fukamiru.authoring.codex import STAGE_GUIDANCE
from fukamiru.authoring.primary_works import (
    append_primary_work_rows,
    build_work_cards,
    candidate_titles,
    mentioned_creative_works,
    quoted_titles,
    titles_in_argument_prose,
)
from fukamiru.worker.runner import WorkerRunner


def _context() -> dict:
    return {
        "semantic_paragraph_plan": [
            {"section_id": "s1", "paragraphs": [
                {"text": "『ナナとカオル』を比較し、『アフタヌーン』の枠を考える。"},
                {"text": "筆者の『研究論文』は根拠の扱いを論じる。"},
            ]},
        ],
        "reference_entries": [{"title": "研究論文"}],
        "section_contracts": [{"section_id": "s1", "claim_ids": ["c1"]}],
        "argument_nodes": [
            {"id": "e1", "claim_id": "c1", "text": "『寄生獣』との違い"},
        ],
    }


def test_inventory_uses_contracted_titles_without_promoting_research_title():
    assert quoted_titles("『論文——『作品』の分析』") == ["論文——『作品』の分析"]
    rows = candidate_titles(_context())
    assert [row["title"] for row in rows] == ["ナナとカオル", "アフタヌーン", "寄生獣"]
    assert rows[2]["section_ids"] == ["s1"]


def test_work_card_uses_only_matching_catalogue_record_and_labels_corpus():
    searched = []

    def catalogue(title):
        searched.append(title)
        return [SimpleNamespace(
            title=title + " 1", authors=("著者",), publisher="出版社",
            publication_date="2009", url="https://ndl.example/one",
            provider_payload={"description": "資料の説明"},
        )]

    cards = build_work_cards(
        _context(), catalogue_search=catalogue,
        corpus_search=lambda title: [
            {"text": f"『{title}』についての記述", "chunk_id": "chunk-1"}
        ],
    )
    assert searched == ["ナナとカオル", "アフタヌーン", "寄生獣"]
    assert cards[0]["lookup_status"] == "catalogue_match"
    assert cards[0]["catalogue_description"] == "資料の説明"
    assert cards[0]["corpus_source"] == "chunk-1"
    assert cards[1]["kind"] == "periodical"


def test_unmatched_catalogue_does_not_invent_metadata_and_rows_are_idempotent():
    cards = build_work_cards(
        _context(), catalogue_search=lambda _: [SimpleNamespace(title="別作品")]
    )
    assert cards[0]["lookup_status"] == "catalogue_unmatched"
    assert cards[0]["creator"] is None
    source = (
        '<article id="doc"><section id="body"><h2 id="body-heading">本文</h2>'
        '<p id="p1">例えば『ナナとカオル』では関係を考える。</p></section>'
        '<section id="refs" data-reference-section="consulted-generated-v22">'
        '<h2 id="refs-heading">参考文献</h2></section></article>'
    )
    used = mentioned_creative_works(source, cards)
    assert [item["title"] for item in used] == ["ナナとカオル"]
    once, edits = append_primary_work_rows(source, used)
    twice, _ = append_primary_work_rows(once, used)
    assert len(edits) == 2
    assert once == twice
    root = ET.fromstring(twice)
    assert len([
        item for item in root.iter("p")
        if item.get("data-primary-work-title") == "ナナとカオル"
    ]) == 1
    assert "書誌未確認" in twice
    assert "アフタヌーン" not in twice


def test_writer_handoff_exposes_progression_without_full_roadmap():
    directives = WorkerRunner._authoring_writer_directives_v31({
        "introduction_contract": {
            "research_question": "問い", "roadmap_section_ids": ["s1", "s2"],
        },
        "section_contracts": [
            {"section_id": "s1", "article_role": "body", "title": "媒体"},
            {"section_id": "s2", "article_role": "body", "title": "作品"},
        ],
    }, "intro", "introduction")
    intro = directives["introduction"]
    assert intro["first_body_topic"] == "媒体"
    assert intro["body_section_count"] == 2
    assert "roadmap" not in intro
    assert "primary_work_cards" in STAGE_GUIDANCE["draft_body_v31"]
    assert "table of contents" in STAGE_GUIDANCE["draft_introduction_v31"]


@pytest.mark.parametrize("generated", [False, True])
def test_book_title_notes_never_become_creative_work_titles_or_bibliography_rows(generated):
    attribute = ' data-citation-generated="evidence-footnote-v1"' if generated else ""
    source = (
        '<article id="D"><section id="S"><h2 id="H">本文</h2><p id="P">'
        '<cite data-reference-id="R1">『<em>架空の協働研究</em>'
        f'<sup{attribute}><a role="doc-noteref" href="#N">6</a></sup>』</cite>と'
        '<cite data-reference-id="R2">『架空の記録論 : 観測と共同作業の条件'
        f'<sup{attribute}><a role="doc-noteref" href="#M">5</a></sup>』</cite>を参照し、'
        f'『架空作品2<sup{attribute}><a role="doc-noteref" href="#K">12</a></sup>』を分析する。'
        '『作品<sup>2</sup>』も扱う。</p></section>'
        '<section id="R" data-reference-section="generated"><h2 id="RH">参考文献</h2>'
        '<p id="RP">『参考文献だけの書名』</p></section></article>'
    )
    titles = titles_in_argument_prose(source)
    assert titles == [
        "架空の協働研究", "架空の記録論 : 観測と共同作業の条件", "架空作品2", "作品2",
    ]
    cards = build_work_cards({
        "semantic_paragraph_plan": [{"section_id": "S", "paragraphs": [{
            "text": "と".join(f"『{title}』" for title in titles),
        }]}],
        "reference_entries": [{"title": title} for title in titles[:2]],
    })
    assert [card["title"] for card in cards] == ["架空作品2", "作品2"]
    used = mentioned_creative_works(source, [*cards, {
        "title": "架空の協働研究6", "kind": "creative_work",
    }])
    assert [card["title"] for card in used] == ["架空作品2", "作品2"]
    rendered, _ = append_primary_work_rows(source, used)
    assert [p.get("data-primary-work-title") for p in ET.fromstring(rendered).iter("p")
            if p.get("data-primary-work-title")] == ["架空作品2", "作品2"]
