from __future__ import annotations

import copy
import unittest
from xml.etree import ElementTree as ET

from fukamiru.literature import LiteratureCandidate, NDLSearchProvider
from fukamiru.revisions import (
    SJLLF_JA_REFERENCE_RULESET,
    JapaneseReferenceService,
    ManuscriptCoherenceService,
)

HTML = (
    '<article id="DOC" lang="ja"><section id="SEC">'
    '<h2 id="INTRO">序論</h2><p id="BODY">本文を示す。</p>'
    '</section><section id="REF-SEC">'
    '<h2 id="REF-H">主要参照文献</h2>'
    '<p id="REF-1">デリダ『散種』／『グラマトロジーについて』。</p>'
    "</section></article>"
)


class JapaneseReferenceTests(unittest.TestCase):
    def test_reference_section_is_processable_but_not_argument_plannable(self) -> None:
        structure = ManuscriptCoherenceService.semantic_structure(HTML)
        reference = next(
            section for section in structure["sections"] if section["document_role"] == "references"
        )

        self.assertFalse(reference["plannable"])
        self.assertTrue(reference["bibliography_processable"])
        self.assertTrue(
            all(
                block["rewritable_as_argument"] is False
                for block in structure["blocks"]
                if block["section_id"] == reference["section_id"]
            )
        )

    def test_extracts_shared_author_entries(self) -> None:
        inventory = JapaneseReferenceService.extract(HTML)

        self.assertEqual(inventory["entry_count"], 2)
        self.assertEqual(
            [entry["author"] for entry in inventory["entries"]],
            ["デリダ", "デリダ"],
        )
        self.assertEqual(
            [entry["title"] for entry in inventory["entries"]],
            ["散種", "グラマトロジーについて"],
        )

    def test_retrieved_metadata_has_field_level_provenance(self) -> None:
        entry = JapaneseReferenceService.extract(HTML)["entries"][0]
        candidate = LiteratureCandidate(
            provider="ndl_search",
            provider_record_id="ndl-1",
            title="散種",
            authors=("デリダ",),
            publication_title=None,
            publisher="法政大学出版局",
            publication_date="2013",
            record_type="図書",
            doi=None,
            url="https://example.invalid/ndl-1",
            oa_url=None,
            oa_status="unknown",
            license_url=None,
            provider_payload={"publication_place": "東京"},
        )

        resolved = JapaneseReferenceService.resolve_with_candidates(entry, [candidate])

        self.assertEqual(resolved["resolution_status"], "resolved")
        self.assertEqual(resolved["publisher"], "法政大学出版局")
        self.assertEqual(resolved["year"], "2013")
        self.assertEqual(resolved["provenance"]["publisher"]["label"], "外部資料で確認")

    def test_inference_only_fills_missing_fields_and_is_marked(self) -> None:
        entry = JapaneseReferenceService.extract(HTML)["entries"][0]
        result = JapaneseReferenceService.apply_inferences(
            [entry],
            {
                "inferences": [
                    {
                        "reference_id": entry["reference_id"],
                        "fields": {
                            "record_type": "article_poem_or_chapter",
                            "publisher": "法政大学出版局",
                            "year": "2013",
                        },
                        "rationale": "既知の日本語版書誌から推定した。",
                        "confidence": 0.9,
                    }
                ]
            },
        )[0]

        self.assertEqual(result["record_type"], "book_or_journal")
        self.assertEqual(result["publisher"], "法政大学出版局")
        self.assertEqual(result["provenance"]["publisher"]["label"], "LLMによる推論")

    def test_render_is_sjllf_compliant_and_idempotent(self) -> None:
        inventory = JapaneseReferenceService.extract(HTML)
        entries = copy.deepcopy(inventory["entries"])
        for ordinal, entry in enumerate(entries):
            entry["publisher"] = "出版社"
            entry["year"] = str(2000 + ordinal)
            entry["provenance"]["publisher"] = {
                "kind": "llm_inference",
                "label": "LLMによる推論",
            }
            entry["provenance"]["year"] = {
                "kind": "llm_inference",
                "label": "LLMによる推論",
            }
            entry["resolution_status"] = "inferred"

        rendered, edits = JapaneseReferenceService.render(HTML, entries)
        report = JapaneseReferenceService.quality_report(rendered, entries)

        self.assertEqual(len(edits), 2)
        self.assertIn("デリダ『散種』、出版社、2000。", rendered)
        self.assertNotIn("pp.", rendered)
        self.assertTrue(report["passed"])
        rerendered, second_edits = JapaneseReferenceService.render(rendered, entries)
        self.assertEqual(rerendered, rendered)
        self.assertEqual(second_edits, [])

    def test_near_tied_identity_candidates_are_not_auto_selected(self) -> None:
        entry = JapaneseReferenceService.extract(HTML)["entries"][0]
        candidates = [
            LiteratureCandidate(
                provider="provider",
                provider_record_id=str(index),
                title="散種",
                authors=("デリダ",),
                publication_title=None,
                publisher=f"出版社{index}",
                publication_date="2013",
                record_type="図書",
                doi=None,
                url=None,
                oa_url=None,
                oa_status=None,
                license_url=None,
                provider_payload={},
            )
            for index in (1, 2)
        ]

        resolved = JapaneseReferenceService.resolve_with_candidates(entry, candidates)

        self.assertEqual(resolved["resolution_status"], "ambiguous")
        self.assertIsNone(resolved["publisher"])

    def test_ruleset_contains_only_explicit_japanese_profile(self) -> None:
        codes = {item["code"] for item in SJLLF_JA_REFERENCE_RULESET["rules"]}

        self.assertIn("sjllf.ja.title.book_or_journal", codes)
        self.assertIn("sjllf.reference.page_format", codes)
        self.assertFalse(any("op_cit" in code for code in codes))

    def test_ndl_rss_candidate_maps_japanese_book_metadata(self) -> None:
        item = ET.fromstring(
            '<item xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:dcndl="http://ndl.go.jp/dcndl/terms/">'
            "<title>散種</title><link>https://ndl.example/item</link>"
            "<dc:title>散種</dc:title><dc:creator>デリダ</dc:creator>"
            "<dc:publisher>法政大学出版局</dc:publisher>"
            "<dcterms:issued>2013</dcterms:issued>"
            "<dcndl:publicationPlace>東京</dcndl:publicationPlace>"
            "<category>図書</category></item>"
        )

        candidate = NDLSearchProvider()._candidate(item)

        self.assertEqual(candidate.title, "散種")
        self.assertEqual(candidate.authors, ("デリダ",))
        self.assertEqual(candidate.publisher, "法政大学出版局")
        self.assertEqual(candidate.provider_payload["publication_place"], "東京")

    def test_ndl_rss_preserves_contributor_responsibility_without_role_guess(self) -> None:
        item = ET.fromstring(
            '<item xmlns:dc="http://purl.org/dc/elements/1.1/">'
            "<title>検証書</title><dc:title>検証書</dc:title>"
            "<dc:creator>Bourdieu, Pierre, 1930-2002 著</dc:creator>"
            "<dc:contributor>石井, 洋二郎, 1951- 訳</dc:contributor>"
            "<category>図書</category></item>"
        )
        candidate = NDLSearchProvider()._candidate(item)
        assert candidate.provider_payload["creators_raw"] == ["Bourdieu, Pierre, 1930-2002 著"]
        assert candidate.provider_payload["contributors_raw"] == ["石井, 洋二郎, 1951- 訳"]
        assert candidate.provider_payload["responsibility_raw"] == [
            "Bourdieu, Pierre, 1930-2002 著",
            "石井, 洋二郎, 1951- 訳",
        ]
        record = JapaneseReferenceService.candidate_record(
            {"title": "検証書", "author": "Bourdieu", "record_type": "book"},
            candidate,
        )
        merged = JapaneseReferenceService._merge_manifestation_group([record])
        assert merged["responsibility_sources"][0]["responsibility_raw"] == [
            "Bourdieu, Pierre, 1930-2002 著",
            "石井, 洋二郎, 1951- 訳",
        ]


if __name__ == "__main__":
    unittest.main()
