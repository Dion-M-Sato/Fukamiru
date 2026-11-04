from __future__ import annotations

import unittest

from fukamiru.documents.canonical_html import CanonicalHTMLValidator, structural_block_fragments

VALID_HTML = """<article id="DOC-001" lang="ja">
  <section id="SEC-001">
    <h2 id="H-001">第一章</h2>
    <p id="P-001">本文と<a href="#P-002">参照</a>です。</p>
    <section id="SEC-002">
      <h3 id="H-002">第一節</h3>
      <p id="P-002"><span lang="fr">Un texte</span>です。</p>
    </section>
  </section>
</article>"""


class CanonicalHTMLTests(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = CanonicalHTMLValidator()

    def test_valid_document_and_structural_blocks(self) -> None:
        report = self.validator.validate(VALID_HTML)
        self.assertTrue(report.valid, report.issues)
        fragments = structural_block_fragments(VALID_HTML)
        self.assertEqual(
            [item[0] for item in fragments],
            ["DOC-001", "SEC-001", "H-001", "P-001", "SEC-002", "H-002", "P-002"],
        )

    def test_script_duplicate_id_and_broken_reference_are_rejected(self) -> None:
        html = """<article id="DOC-001" lang="ja">
          <section id="SEC-001"><h2 id="X">題</h2>
          <p id="X"><a href="#missing">本文</a><script id="S">bad()</script></p></section>
        </article>"""
        report = self.validator.validate(html)
        codes = {issue.code for issue in report.issues}
        self.assertIn("html.duplicate_id", codes)
        self.assertIn("html.tag_not_allowed", codes)
        self.assertIn("html.broken_xref", codes)

    def test_wrong_heading_depth_is_rejected(self) -> None:
        html = """<article id="DOC-001" lang="ja">
          <section id="SEC-001"><h4 id="H-001">題</h4><p id="P-001">本文</p></section>
        </article>"""
        report = self.validator.validate(html)
        self.assertIn("html.heading_level", {issue.code for issue in report.issues})


if __name__ == "__main__":
    unittest.main()
