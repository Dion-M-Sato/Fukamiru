from __future__ import annotations

import unittest

from fukamiru.revisions import ManuscriptEnumerationService


class ManuscriptEnumerationServiceTests(unittest.TestCase):
    def test_detects_two_item_coordination_without_count_threshold(self) -> None:
        canonical = (
            '<article id="D" lang="ja"><section id="S">'
            '<p id="P">編集部、書店が作品を選別する。</p>'
            '</section></article>'
        )

        records = ManuscriptEnumerationService.inventory(canonical)

        self.assertTrue(records)
        self.assertEqual(records[0]["block_id"], "P")
        self.assertGreaterEqual(len(records[0]["items"]), 2)

    def test_detects_two_adjacent_quoted_titles(self) -> None:
        canonical = (
            '<article id="D" lang="ja"><section id="S">'
            '<p id="P">『MONSTER』『JIN―仁―』を比較する。</p>'
            '</section></article>'
        )

        records = ManuscriptEnumerationService.inventory(canonical)

        self.assertEqual(records[0]["kind"], "quoted_coordination")
        self.assertEqual(records[0]["items"], ["『MONSTER』", "『JIN―仁―』"])


if __name__ == "__main__":
    unittest.main()
