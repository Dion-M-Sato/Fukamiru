from __future__ import annotations

import copy
import unittest

from fukamiru.revisions import ManuscriptCoherenceService

HTML = (
    '<article id="DOC" lang="ja"><section id="SEC">'
    '<p id="H1">### 1　第一節</p>'
    '<p id="C1">1980年には中心的な変化が起きた。</p>'
    '<p id="E1">1970年の『作品A』は、その前史を示す。</p>'
    '<p id="Q1">ただし、この変化には制度上の例外がある。</p>'
    '<p id="H2">### 2　第二節</p>'
    '<p id="C2">1990年には別の制度へ移行した。</p>'
    '</section></article>'
)


def valid_plan() -> dict[str, object]:
    return {
        "document_thesis": "二つの時期の制度変化を説明する。",
        "global_ordering_policy": "chronological",
        "sections": [
            {
                "section_id": "semantic:H1",
                "title": "第一節",
                "central_claim": "1980年の変化には前史と例外がある。",
                "central_claim_block_id": "C1",
                "ordering_policy": "claim_evidence",
                "ordered_block_ids": ["H1", "C1", "E1", "Q1"],
                "transition_needed": False,
                "transition_from_previous": None,
                "transition_reason": "冒頭節である。",
            },
            {
                "section_id": "semantic:H2",
                "title": "第二節",
                "central_claim": "1990年に制度が移行した。",
                "central_claim_block_id": "C2",
                "ordering_policy": "chronological",
                "ordered_block_ids": ["H2", "C2"],
                "transition_needed": True,
                "transition_from_previous": "前史と例外を踏まえ、次の制度を扱う。",
                "transition_reason": "年代と制度が移るため。",
            },
        ],
        "block_roles": [
            {
                "block_id": "H1",
                "role": "heading",
                "parent_claim_block_id": None,
                "relation_to_previous": "none",
            },
            {
                "block_id": "C1",
                "role": "claim",
                "parent_claim_block_id": None,
                "relation_to_previous": "support",
            },
            {
                "block_id": "E1",
                "role": "example",
                "parent_claim_block_id": "C1",
                "relation_to_previous": "example",
            },
            {
                "block_id": "Q1",
                "role": "qualification",
                "parent_claim_block_id": "C1",
                "relation_to_previous": "qualification",
            },
            {
                "block_id": "H2",
                "role": "heading",
                "parent_claim_block_id": None,
                "relation_to_previous": "transition",
            },
            {
                "block_id": "C2",
                "role": "claim",
                "parent_claim_block_id": None,
                "relation_to_previous": "support",
            },
        ],
        "argument_links": [
            {
                "source_block_id": "E1",
                "target_claim_block_id": "C1",
                "relation": "exemplifies",
                "observed_property": "作品が制度変化以前の慣行を描く。",
                "warrant": "前史を示すため、1980年の変化の差分が明確になる。",
                "evidence_required": False,
                "research_query": None,
            }
        ],
        "temporal_constraints": [
            {
                "block_id": "C1",
                "start_year": 1980,
                "end_year": 1980,
                "temporal_kind": "historical_context",
                "relation_to_previous": "none",
            },
            {
                "block_id": "E1",
                "start_year": 1970,
                "end_year": 1970,
                "temporal_kind": "historical_context",
                "relation_to_previous": "retrospective",
            },
            {
                "block_id": "C2",
                "start_year": 1990,
                "end_year": 1990,
                "temporal_kind": "historical_context",
                "relation_to_previous": "after",
            },
        ],
        "connective_relations": [
            {
                "block_id": "Q1",
                "connective": "ただし",
                "relation": "qualification",
                "target_block_id": "C1",
                "explanation": "中心的主張の適用範囲を限定する。",
            }
        ],
        "merge_groups": [],
    }


def typed_plan(canonical_html: str = HTML) -> dict[str, object]:
    plan = copy.deepcopy(valid_plan())
    structure = ManuscriptCoherenceService.semantic_structure(canonical_html)
    central_by_section = {
        str(section["section_id"]): str(section["central_claim_block_id"])
        for section in plan["sections"]
    }
    claim_text_by_section = {
        str(section["section_id"]): str(section["central_claim"])
        for section in plan["sections"]
    }
    classifications: list[dict[str, object]] = []
    links: list[dict[str, object]] = []
    temporal_mentions: list[dict[str, object]] = []
    temporal_scopes: list[dict[str, object]] = []
    for block in structure["blocks"]:
        block_id = str(block["id"])
        section_id = str(block["section_id"])
        for reference in block.get("reference_candidates", []):
            kind = str(reference["preliminary_kind"])
            reference_id = str(reference["reference_id"])
            classifications.append(
                {
                    "reference_id": reference_id,
                    "block_id": block_id,
                    "kind": kind,
                    "rationale": "The source context determines this reference type.",
                }
            )
            if kind == "work_example":
                links.append(
                    {
                        "source_block_id": block_id,
                        "source_reference_id": reference_id,
                        "target_claim_block_id": central_by_section[section_id],
                        "target_claim_anchor": claim_text_by_section[section_id],
                        "relation": "exemplifies",
                        "observed_property": "The named work supplies an observable case.",
                        "warrant": "The observed case advances the section claim.",
                        "evidence_required": False,
                        "research_query": None,
                    }
                )
        mentions = list(block.get("temporal_mentions", []))
        for mention in mentions:
            temporal_mentions.append(
                {
                    "mention_id": str(mention["mention_id"]),
                    "block_id": block_id,
                    "temporal_kind": "historical_context",
                }
            )
        if mentions:
            temporal_scopes.append(
                {
                    "block_id": block_id,
                    "start_year": min(int(item["start_year"]) for item in mentions),
                    "end_year": max(int(item["end_year"]) for item in mentions),
                    "temporal_kind": "historical_context",
                    "relation_to_previous": "background",
                }
            )
    plan["reference_classifications"] = classifications
    plan["argument_links"] = links
    plan["temporal_mentions"] = temporal_mentions
    plan["block_temporal_scopes"] = temporal_scopes
    plan.pop("temporal_constraints")
    return plan


class ManuscriptCoherenceTests(unittest.TestCase):
    def test_restore_blocks_does_not_report_identical_content_as_progress(self) -> None:
        restored_html, restored_ids = ManuscriptCoherenceService.restore_blocks(
            HTML, HTML, {"C1"}
        )

        self.assertEqual(restored_html, HTML)
        self.assertEqual(restored_ids, [])

    def test_japanese_years_are_extracted_as_distinct_mentions(self) -> None:
        html = HTML.replace(
            "1980年には中心的な変化が起きた。",
            "一九八〇年代から1997年を経て二〇〇〇年代前半に変化した。",
        )

        structure = ManuscriptCoherenceService.semantic_structure(html)
        block = next(item for item in structure["blocks"] if item["id"] == "C1")

        self.assertEqual(
            [
                (item["text"], item["start_year"], item["end_year"])
                for item in block["temporal_mentions"]
            ],
            [
                ("一九八〇年代", 1980, 1989),
                ("1997年", 1997, 1997),
                ("二〇〇〇年代前半", 2000, 2004),
            ],
        )

    def test_typed_intra_block_example_targets_a_distinct_claim_unit(self) -> None:
        html = HTML.replace(
            "1980年には中心的な変化が起きた。",
            "1980年の『作品B』は中心的な変化を具体化している。",
        )
        plan = typed_plan(html)
        structure = ManuscriptCoherenceService.semantic_structure(html)

        report = ManuscriptCoherenceService.validate_plan(structure, plan)

        self.assertTrue(report.valid)
        intra_block = next(
            item
            for item in plan["argument_links"]
            if item["source_block_id"] == "C1"
        )
        self.assertEqual(intra_block["target_claim_block_id"], "C1")
        self.assertTrue(intra_block["source_reference_id"])
        broken = copy.deepcopy(plan)
        broken_link = next(
            item
            for item in broken["argument_links"]
            if item["source_block_id"] == "C1"
        )
        broken_link["source_reference_id"] = None

        codes = {
            issue.code
            for issue in ManuscriptCoherenceService.validate_plan(
                structure, broken
            ).issues
        }
        self.assertIn("argument.self_link", codes)
        self.assertIn("argument.example_unlinked", codes)

        ungrounded = copy.deepcopy(plan)
        ungrounded_link = next(
            item
            for item in ungrounded["argument_links"]
            if item["source_block_id"] == "C1"
        )
        ungrounded_link["target_claim_anchor"] = "存在しない中心命題"
        codes = {
            issue.code
            for issue in ManuscriptCoherenceService.validate_plan(
                structure, ungrounded
            ).issues
        }
        self.assertIn("argument.claim_anchor_unresolved", codes)

    def test_theory_reference_is_not_forced_into_an_example_link(self) -> None:
        html = HTML.replace(
            "1980年には中心的な変化が起きた。",
            "ブルデュー『ディスタンクシオン』を参照して中心命題を定める。",
        )
        plan = typed_plan(html)
        structure = ManuscriptCoherenceService.semantic_structure(html)
        c1_reference = next(
            item
            for item in plan["reference_classifications"]
            if item["block_id"] == "C1"
        )

        self.assertEqual(c1_reference["kind"], "theory_reference")
        self.assertFalse(
            any(
                item.get("source_reference_id") == c1_reference["reference_id"]
                for item in plan["argument_links"]
            )
        )
        self.assertTrue(ManuscriptCoherenceService.validate_plan(structure, plan).valid)

    def test_multiple_mentions_use_one_aggregate_temporal_scope(self) -> None:
        html = HTML.replace(
            "1980年には中心的な変化が起きた。",
            "一九八〇年代から1997年を経て二〇〇〇年代前半に変化した。",
        )
        plan = typed_plan(html)
        structure = ManuscriptCoherenceService.semantic_structure(html)
        c1_mentions = [
            item for item in plan["temporal_mentions"] if item["block_id"] == "C1"
        ]
        c1_scopes = [
            item
            for item in plan["block_temporal_scopes"]
            if item["block_id"] == "C1"
        ]

        self.assertEqual(len(c1_mentions), 3)
        self.assertEqual(len(c1_scopes), 1)
        self.assertEqual(
            (c1_scopes[0]["start_year"], c1_scopes[0]["end_year"]),
            (1980, 2004),
        )
        self.assertTrue(ManuscriptCoherenceService.validate_plan(structure, plan).valid)

        duplicate = copy.deepcopy(plan)
        duplicate["block_temporal_scopes"].append(copy.deepcopy(c1_scopes[0]))
        codes = {
            issue.code
            for issue in ManuscriptCoherenceService.validate_plan(
                structure, duplicate
            ).issues
        }
        self.assertIn("temporal.duplicate_scope", codes)

    def test_malformed_temporal_arrays_are_reported_without_normalizer_crash(
        self,
    ) -> None:
        plan = typed_plan()
        plan["temporal_mentions"] = None
        plan["block_temporal_scopes"] = None
        structure = ManuscriptCoherenceService.semantic_structure(HTML)

        normalized, _ = ManuscriptCoherenceService.normalize_plan(structure, plan)
        codes = {
            issue.code
            for issue in ManuscriptCoherenceService.validate_plan(
                structure, normalized
            ).issues
        }

        self.assertIn("temporal.mentions_invalid", codes)
        self.assertIn("temporal.scopes_invalid", codes)

    def test_named_section_paragraph_is_recognized_as_a_heading(self) -> None:
        self.assertEqual(
            ManuscriptCoherenceService.heading(
                "序　媒体はいかに欲望を配置するか", "p"
            ),
            (2, "序　媒体はいかに欲望を配置するか"),
        )
        self.assertIsNone(
            ManuscriptCoherenceService.heading(
                "結論として、この関係を次のように整理できる。", "p"
            )
        )
        self.assertEqual(
            ManuscriptCoherenceService.heading(
                "主要参照文献デリダ『散種』。ブルデュー『芸術の規則』。", "p"
            ),
            (2, "主要参照文献"),
        )
        self.assertIsNone(
            ManuscriptCoherenceService.heading(
                "参照文献一覧の来歴については、内部記録上の説明がある。", "p"
            )
        )

    def test_terminal_sections_are_normalized_before_paragraph_lock(self) -> None:
        source = (
            '<article id="DOC-END" lang="ja"><section id="SEC-END">'
            '<p id="H19">## 19　AI生成環境</p>'
            '<p id="P19">最終事例を検討する。</p>'
            '<p id="HC">## 結　結論</p>'
            '<p id="PC">本稿の結論を示す。</p>'
            '<p id="REF">主要参照文献デリダ『散種』。</p>'
            '<p id="HL">## 適用範囲と検証課題</p>'
            '<p id="PL">今後の検証課題を示す。</p>'
            "</section></article>"
        )

        normalized, edits, warnings = (
            ManuscriptCoherenceService.normalize_terminal_sections(source)
        )
        structure = ManuscriptCoherenceService.semantic_structure(normalized)

        self.assertEqual(warnings, [])
        self.assertEqual(
            [block["id"] for block in structure["blocks"]],
            ["H19", "P19", "HC", "PC", "HL", "PL", "REF"],
        )
        self.assertEqual(
            [section["title"] for section in structure["sections"][-3:]],
            ["結　結論", "適用範囲と検証課題", "主要参照文献"],
        )
        self.assertEqual(structure["sections"][-2]["level"], 3)
        self.assertFalse(structure["sections"][-1]["plannable"])
        self.assertEqual(
            [edit["operation"] for edit in edits],
            ["normalize_terminal_section_order", "demote_terminal_subsection"],
        )

        plannable = {
            str(section["section_id"]): section
            for section in structure["sections"]
            if section["plannable"]
        }
        raw_plan = {
            "sections": [
                {
                    "section_id": section_id,
                    "central_claim_block_id": section["prose_block_ids"][0],
                    "ordered_block_ids": section["block_ids"],
                }
                for section_id in (
                    "semantic:H19",
                    "semantic:HL",
                    "semantic:HC",
                )
                for section in (plannable[section_id],)
            ],
            "block_roles": [],
            "argument_links": [],
            "connective_relations": [],
            "merge_groups": [],
        }
        normalized_plan, actions = ManuscriptCoherenceService.normalize_plan(
            structure, raw_plan
        )
        renormalized_plan, repeated_actions = ManuscriptCoherenceService.normalize_plan(
            structure, normalized_plan
        )
        self.assertEqual(
            [section["section_id"] for section in normalized_plan["sections"]],
            ["semantic:H19", "semantic:HC", "semantic:HL"],
        )
        self.assertEqual(renormalized_plan["sections"], normalized_plan["sections"])
        self.assertNotIn(
            "normalize_terminal_section_order",
            {str(item.get("action") or "") for item in repeated_actions},
        )
        self.assertTrue(
            any(
                action.get("action") == "normalize_terminal_section_order"
                for action in actions
            )
        )

    def test_document_manifest_keeps_references_observable_but_read_only(self) -> None:
        source = (
            '<article id="DOC-MANIFEST" lang="ja"><section id="SEC-MANIFEST">'
            '<h2 id="H19">第19節　AI生成環境</h2>'
            '<p id="P19">最終事例を検討する。</p>'
            '<h2 id="HC">結論</h2><p id="PC">結論を示す。</p>'
            '<h3 id="HL">適用範囲と検証課題</h3>'
            '<p id="PL">今後の検証課題を示す。</p>'
            '<h2 id="HR">主要参照文献</h2>'
            '<p id="R1">著者（2024）「論文」『誌名』。</p>'
            "</section></article>"
        )

        manifest = ManuscriptCoherenceService.document_manifest(source)

        self.assertEqual(manifest["block_count"], 8)
        self.assertEqual(manifest["reference_section_count"], 1)
        self.assertEqual(manifest["reference_block_count"], 2)
        self.assertEqual(manifest["observable_block_ids"][-2:], ["HR", "R1"])
        self.assertNotIn("HR", manifest["editable_block_ids"])
        self.assertNotIn("R1", manifest["editable_block_ids"])
        self.assertEqual(manifest["read_only_block_ids"], ["HR", "R1"])
        self.assertEqual(
            manifest["terminal_roles"],
            ["conclusion", "limitations", "references"],
        )

    def test_terminal_contract_uses_conventional_order_over_review_word_order(
        self,
    ) -> None:
        source = (
            '<article id="DOC-CONTRACT" lang="ja"><section id="SEC-CONTRACT">'
            '<h2 id="H19">第19節　AI生成環境</h2><p id="P19">事例。</p>'
            '<h2 id="HC">結論</h2><p id="PC">結論。</p>'
            '<h3 id="HL">適用範囲と検証課題</h3><p id="PL">課題。</p>'
            '<h2 id="HR">主要参照文献</h2><p id="R1">文献。</p>'
            "</section></article>"
        )

        contract = ManuscriptCoherenceService.terminal_contract(
            source,
            human_review=(
                "第19節 → 結論 → 主要参照文献 → 適用範囲と検証課題"
            ),
        )

        self.assertTrue(contract["valid"])
        self.assertEqual(
            contract["required_roles"],
            ["argument", "conclusion", "limitations", "references"],
        )
        self.assertEqual(contract["last_section_role"], "references")

    def test_terminal_contract_detects_reference_section_before_limitations(
        self,
    ) -> None:
        source = (
            '<article id="DOC-BAD-END" lang="ja"><section id="SEC-BAD-END">'
            '<h2 id="HC">結論</h2><p id="PC">結論。</p>'
            '<h2 id="HR">主要参照文献</h2><p id="R1">文献。</p>'
            '<h3 id="HL">適用範囲と検証課題</h3><p id="PL">課題。</p>'
            "</section></article>"
        )

        contract = ManuscriptCoherenceService.terminal_contract(source)

        self.assertFalse(contract["valid"])
        self.assertIn(
            "terminal.role_order_invalid",
            {item["code"] for item in contract["issues"]},
        )
        self.assertIn(
            "terminal.references_not_last",
            {item["code"] for item in contract["issues"]},
        )

    def test_flat_markdown_headings_become_semantic_sections(self) -> None:
        structure = ManuscriptCoherenceService.semantic_structure(HTML)

        self.assertEqual(structure["markdown_heading_count"], 2)
        self.assertEqual(structure["plannable_section_count"], 2)
        self.assertEqual(structure["sections"][0]["block_ids"], ["H1", "C1", "E1", "Q1"])
        self.assertTrue(ManuscriptCoherenceService.validate_plan(structure, valid_plan()).valid)

    def test_invalid_warrant_timeline_role_and_connective_are_rejected(self) -> None:
        plan = copy.deepcopy(valid_plan())
        plan["argument_links"][0]["warrant"] = ""
        plan["temporal_constraints"][1]["relation_to_previous"] = "after"
        plan["connective_relations"][0]["relation"] = "contrast"
        plan["block_roles"] = [
            item for item in plan["block_roles"] if item["block_id"] != "E1"
        ]

        report = ManuscriptCoherenceService.validate_plan(
            ManuscriptCoherenceService.semantic_structure(HTML), plan
        )
        codes = {issue.code for issue in report.issues}

        self.assertFalse(report.valid)
        self.assertTrue(
            {
                "argument.warrant_missing",
                "argument.role_missing",
                "temporal.unexplained_regression",
                "discourse.tadashi_mismatch",
            }.issubset(codes)
        )

    def test_plan_contract_is_validated_without_json_schema_trust(self) -> None:
        plan = copy.deepcopy(valid_plan())
        plan["document_thesis"] = ""
        plan["global_ordering_policy"] = "invented"
        plan["sections"][0]["transition_needed"] = "false"
        plan["block_roles"][1]["relation_to_previous"] = "invented"
        plan["argument_links"][0]["relation"] = "invented"
        plan["argument_links"][0]["evidence_required"] = "false"
        plan["temporal_constraints"][0]["temporal_kind"] = "invented"
        plan["connective_relations"][0]["connective"] = "しかし"

        report = ManuscriptCoherenceService.validate_plan(
            ManuscriptCoherenceService.semantic_structure(HTML), plan
        )
        codes = {issue.code for issue in report.issues}

        self.assertFalse(report.valid)
        self.assertTrue(
            {
                "plan.document_thesis_missing",
                "plan.global_ordering_policy",
                "plan.transition_flag_invalid",
                "argument.relation_unknown",
                "argument.link_relation_unknown",
                "argument.evidence_flag_invalid",
                "temporal.kind_unknown",
                "discourse.connective_mismatch",
            }.issubset(codes)
        )

    def test_mechanical_plan_references_are_normalized_without_inventing_warrants(self) -> None:
        plan = copy.deepcopy(valid_plan())
        plan["block_roles"][1]["role"] = "background"
        plan["block_roles"][2]["parent_claim_block_id"] = "H1"
        plan["argument_links"][0]["target_claim_block_id"] = "H1"
        plan["temporal_constraints"][1]["start_year"] = 1980
        plan["connective_relations"][0]["target_block_id"] = "C2"
        structure = ManuscriptCoherenceService.semantic_structure(HTML)

        normalized, actions = ManuscriptCoherenceService.normalize_plan(structure, plan)
        report = ManuscriptCoherenceService.validate_plan(structure, normalized)

        self.assertTrue(report.valid)
        self.assertGreaterEqual(len(actions), 5)
        self.assertEqual(
            normalized["argument_links"][0]["warrant"],
            plan["argument_links"][0]["warrant"],
        )

    def test_v18_connective_target_is_validated_instead_of_silently_reattached(
        self,
    ) -> None:
        plan = copy.deepcopy(valid_plan())
        plan["connective_relations"][0]["target_block_id"] = "C2"
        structure = ManuscriptCoherenceService.semantic_structure(HTML)

        normalized, actions = ManuscriptCoherenceService.normalize_plan(
            structure,
            plan,
            repair_connective_targets=False,
        )
        report = ManuscriptCoherenceService.validate_plan(structure, normalized)

        self.assertEqual(
            normalized["connective_relations"][0]["target_block_id"], "C2"
        )
        self.assertNotIn(
            "reattach_connective",
            {str(item.get("action") or "") for item in actions},
        )
        self.assertFalse(report.valid)
        self.assertIn(
            "discourse.target_not_prior",
            {str(item.get("code") or "") for item in report.as_dict()["issues"]},
        )

    def test_duplicate_connective_records_are_deduplicated_and_audited(self) -> None:
        plan = copy.deepcopy(valid_plan())
        plan["connective_relations"].append(
            copy.deepcopy(plan["connective_relations"][0])
        )
        structure = ManuscriptCoherenceService.semantic_structure(HTML)

        normalized, actions = ManuscriptCoherenceService.normalize_plan(structure, plan)
        report = ManuscriptCoherenceService.validate_plan(structure, normalized)

        self.assertTrue(report.valid)
        self.assertEqual(len(normalized["connective_relations"]), 1)
        self.assertIn(
            {"action": "drop_duplicate_connective", "block_id": "Q1"},
            actions,
        )

    def test_structure_reorder_and_section_batches_preserve_boundaries(self) -> None:
        plan = valid_plan()
        plan["sections"][0]["ordered_block_ids"] = ["H1", "E1", "C1", "Q1"]
        plan["connective_relations"][0]["target_block_id"] = "C1"
        structure = ManuscriptCoherenceService.semantic_structure(HTML)

        reordered, edits, warnings = ManuscriptCoherenceService.apply_structure_plan(
            HTML, structure, plan
        )
        batches = ManuscriptCoherenceService.section_batches(
            reordered, max_characters=10_000
        )

        self.assertEqual(warnings, [])
        self.assertTrue(edits)
        self.assertEqual([item["id"] for item in batches[0]], ["H1", "E1", "C1", "Q1"])
        self.assertEqual([item["id"] for item in batches[1]], ["H2", "C2"])

    def test_section_move_preserves_the_complete_semantic_section(self) -> None:
        moved, edits, warnings = ManuscriptCoherenceService.apply_section_moves(
            HTML,
            [
                {
                    "operation": "move_section_before",
                    "section_id": "semantic:H2",
                    "anchor_section_id": "semantic:H1",
                    "issue_id": "chronology-1",
                }
            ],
        )
        block_ids = [
            block["id"]
            for block in ManuscriptCoherenceService.semantic_structure(moved)["blocks"]
        ]

        self.assertEqual(block_ids, ["H2", "C2", "H1", "C1", "E1", "Q1"])
        self.assertEqual(warnings, [])
        self.assertEqual(edits[0]["block_ids"], ["H2", "C2"])
        self.assertEqual(edits[0]["issue_id"], "chronology-1")


if __name__ == "__main__":
    unittest.main()
