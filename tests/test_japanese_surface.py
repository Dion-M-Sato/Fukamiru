from __future__ import annotations

import unittest

from fukamiru.documents.japanese_surface import JapaneseSurfaceQualityGate


class JapaneseSurfaceQualityGateTests(unittest.TestCase):
    def test_required_surface_defects_are_detected(self) -> None:
        cases = {
            "論点を考察し。そのうえで進む。": "surface.dangling_shi_period",
            "論点を示す。。次へ進む。": "surface.consecutive_punctuation",
            "入口は透明なな構造である。": "surface.particle_or_inflection_repetition",
            "制度に依存しする説明である。": ("surface.particle_or_inflection_repetition"),
            "この研究はは重要である。": "surface.particle_or_inflection_repetition",
            "媒体の同一化同一化を避ける。": "surface.repeated_lexical_unit",
            "単行本棚・オンラインとオンライン上の検索を比較する。": (
                "surface.coordinated_lexical_repetition"
            ),
            "従来は周縁化された諸形式・に注目する。": (
                "surface.middle_dot_before_particle"
            ),
            "『MONSTER』『JIN―仁―』を比較する。": (
                "surface.missing_middle_dot_between_quoted_items"
            ),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertIn(
                    expected,
                    {issue.code for issue in JapaneseSurfaceQualityGate.find(text)},
                )

    def test_lexicalized_reduplication_is_not_rejected(self) -> None:
        natural_examples = (
            "ニコニコ動画では推薦機能が利用される。",
            "何が起こるかはわかっているし、その涙は心地よい。",
            "ジェンダー・アイデンティティを論じる。",
            "オパオパがアニメに登場する。",
            "喜びの一瞬一瞬を描き、一語一語を吟味する。",
            "作品が人物と読者をどの位置に置くかを検討する。",
        )
        for text in natural_examples:
            with self.subTest(text=text):
                codes = {issue.code for issue in JapaneseSurfaceQualityGate.find(text)}
                self.assertNotIn("surface.repeated_lexical_unit", codes)
                self.assertNotIn("surface.particle_or_inflection_repetition", codes)

    def test_compound_coordination_overlap_is_not_rejected(self) -> None:
        codes = {
            issue.code
            for issue in JapaneseSurfaceQualityGate.find(
                "同時掲載と掲載位置を号ごとに確認する。"
            )
        }

        self.assertNotIn("surface.coordinated_lexical_repetition", codes)
        defect_codes = {
            issue.code
            for issue in JapaneseSurfaceQualityGate.find(
                "単行本棚・オンラインとオンライン上の検索を比較する。"
            )
        }
        self.assertIn("surface.coordinated_lexical_repetition", defect_codes)

    def test_repeated_inflectional_stem_is_detected_at_word_boundary(self) -> None:
        codes = {
            issue.code
            for issue in JapaneseSurfaceQualityGate.find("論点を書き書くことは避ける。")
        }
        self.assertIn("surface.repeated_inflectional_stem", codes)

    def test_true_katakana_duplication_is_rejected(self) -> None:
        codes = {
            issue.code
            for issue in JapaneseSurfaceQualityGate.find(
                "このテストテストでは境界重複を検出する。"
            )
        }
        self.assertIn("surface.repeated_lexical_unit", codes)

    def test_repeated_light_verb_nominalization_is_detected_conservatively(self) -> None:
        natural = "資料の分析を行う。次に結果を検討する。"
        stacked = "資料の分析を行い、結果の検討を実施する。"

        self.assertNotIn(
            "style.excessive_light_verb_nominalization",
            {issue.code for issue in JapaneseSurfaceQualityGate.find(natural)},
        )
        finding = next(
            issue
            for issue in JapaneseSurfaceQualityGate.find(stacked)
            if issue.code == "style.excessive_light_verb_nominalization"
        )
        self.assertEqual(finding.token, "検討を実施する")

    def test_introduced_nominalization_compares_occurrence_counts(self) -> None:
        source = "資料の分析を行い、結果を検討する。"
        candidate = "資料の分析を行い、結果の検討を実施する。"

        self.assertIn(
            "style.excessive_light_verb_nominalization",
            {issue.code for issue in JapaneseSurfaceQualityGate.introduced(source, candidate)},
        )

    def test_existing_source_issue_is_not_treated_as_new(self) -> None:
        source = "引用に句点がある。。別の文。"
        candidate = "引用に句点がある。。別の説明。"

        self.assertEqual(JapaneseSurfaceQualityGate.introduced(source, candidate), ())

    def test_noop_and_replacement_boundary_repetition_are_detected(self) -> None:
        noop = JapaneseSurfaceQualityGate.edit_contract_issues(
            "主張を説明する。",
            before="説明する",
            after="説明する",
            start=3,
        )
        boundary = JapaneseSurfaceQualityGate.edit_contract_issues(
            "透明な入口である。",
            before="透明",
            after="透明な",
            start=0,
        )

        self.assertIn("surface.noop_edit", {issue.code for issue in noop})
        self.assertIn(
            "surface.replacement_boundary_repetition",
            {issue.code for issue in boundary},
        )

    def test_invalid_changed_block_is_restored_before_persistence(self) -> None:
        source = (
            '<article id="DOC" lang="ja"><section id="SEC">'
            '<h2 id="H">章</h2><p id="P-1">元の第一段落。</p>'
            '<p id="P-2">元の第二段落。</p></section></article>'
        )
        candidate = source.replace("元の第一段落。", "改稿した第一段落。。").replace(
            "元の第二段落。", "有効な改稿段落。"
        )

        result = JapaneseSurfaceQualityGate.guard_canonical_html(source, candidate)

        self.assertIn("元の第一段落。", result.canonical_html)
        self.assertIn("有効な改稿段落。", result.canonical_html)
        self.assertEqual(len(result.reverted_blocks), 1)
        self.assertEqual(result.reverted_blocks[0]["block_id"], "P-1")

    def test_adjacent_quoted_items_are_normalized_before_persistence(self) -> None:
        source = (
            '<article id="DOC" lang="ja"><section id="SEC">'
            '<p id="P-1">『MONSTER』『JIN―仁―』を扱う。</p></section></article>'
        )

        result = JapaneseSurfaceQualityGate.guard_canonical_html(source, source)

        self.assertIn("『MONSTER』・『JIN―仁―』", result.canonical_html)
        self.assertEqual(len(result.normalized_blocks), 1)


if __name__ == "__main__":
    unittest.main()
