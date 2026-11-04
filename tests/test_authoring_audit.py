from __future__ import annotations

import unittest
from xml.etree import ElementTree as ET

from fukamiru.authoring.audit import audit_plan
from fukamiru.authoring.render import annotate_citation_mentions, render_canonical_html


class AuthoringAuditTests(unittest.TestCase):
    def test_audit_detects_unsupported_and_unallocated_claim(self) -> None:
        findings = audit_plan(
            {
                "research_question": {"question": "Why?"},
                "claims": [{"id": "c1", "level": "main"}],
                "claim_edges": [],
                "argument_nodes": [{"id": "c1", "node_type": "claim"}],
                "argument_edges": [],
                "discourse_nodes": [{"id": "s1", "section_id": "s1"}],
                "discourse_edges": [],
                "section_contracts": [
                    {"section_id": "s1", "claim_ids": [], "local_question": "Why?"}
                ],
            }
        )
        self.assertIn("argument_graph.unsupported_claim", {item["code"] for item in findings})
        self.assertIn("section_contract.unallocated_claim", {item["code"] for item in findings})

    def test_render_produces_valid_canonical_html(self) -> None:
        rendered = render_canonical_html(
            plan_id="plan-1",
            sections=[{"section_id": "intro", "title": "導入", "prose": "第一段落。"}],
        )
        self.assertIn('<article id="DOC-plan-1"', rendered)
        self.assertIn("第一段落。", rendered)

    def test_citation_mentions_render_as_reference_tags_and_survive_reannotation(self) -> None:
        section = {
            "section_id": "sec-body",
            "title": "本文",
            "article_role": "body",
            "paragraphs": [{
                "text": "Jamesonの『Postmodernism』を参照する。",
                "citation_mentions": [{
                    "reference_id": "reference:jameson",
                    "text": "『Postmodernism』",
                }],
            }],
        }
        rendered = render_canonical_html(plan_id="plan-1", sections=[section])
        root = ET.fromstring(rendered)
        cite = next(root.iter("cite"))
        self.assertEqual(cite.get("data-reference-id"), "reference:jameson")
        self.assertEqual("".join(cite.itertext()), "『Postmodernism』")
        self.assertEqual(
            annotate_citation_mentions(
                rendered,
                plan_id="plan-1",
                sections=[section],
            ),
            rendered,
        )


if __name__ == "__main__":
    unittest.main()
