from __future__ import annotations

import copy
import unittest

from fukamiru.revisions import (
    ManuscriptCoherenceService,
    ManuscriptDiscourseService,
)
from fukamiru.revisions.academic_connectives import (
    ACADEMIC_CONNECTIVES,
    academic_connective_matches,
)

HTML = (
    '<article id="DOC" lang="ja"><section id="SEC">'
    '<h2 id="H1">第一節</h2>'
    '<p id="P1">第一文。第二文。</p>'
    '<p id="P2">第三文。第四文。</p>'
    "</section></article>"
)


def paragraph(
    paragraph_id: str,
    span_id: str,
    unit_ids: list[str],
) -> dict[str, object]:
    return {
        "paragraph_id": paragraph_id,
        "span_id": span_id,
        "source_unit_ids": unit_ids,
        "governing_topic": "同一の対象について説明する。",
        "central_proposition": "対象に関する中心的な主張を示す。",
        "central_position": "medial",
        "rhetorical_chain": [
            {
                "source_unit_id": left,
                "target_unit_id": right,
                "relation": "elaborates",
                "explanation": "The following unit develops the same central proposition.",
            }
            for left, right in zip(unit_ids, unit_ids[1:], strict=False)
        ],
        "topic_entities": ["対象"],
        "temporal_scope": None,
        "entry_relation": "continuation",
        "boundary_reason": "次の単位で論証上の役割が変わるため。",
        "boundary_confidence": 0.91,
    }


class ManuscriptDiscourseTests(unittest.TestCase):
    def setUp(self) -> None:
        semantic = ManuscriptCoherenceService.semantic_structure(HTML)
        self.structure = ManuscriptDiscourseService.discourse_structure(
            HTML, semantic
        )

    def test_sentence_split_keeps_quoted_punctuation_inside_one_unit(self) -> None:
        sentences = ManuscriptDiscourseService.split_sentences(
            "彼は「終わった。」と述べた。次の論点へ移る。"
        )

        self.assertEqual(
            sentences,
            ["彼は「終わった。」と述べた。", "次の論点へ移る。"],
        )

    def test_plan_can_split_and_compose_without_a_character_target(self) -> None:
        span = self.structure["spans"][0]
        unit_ids = [str(item["unit_id"]) for item in span["units"]]
        plan = {
            "policy": "semantic_coherence_not_fixed_length",
            "paragraphs": [
                paragraph("para-1", str(span["span_id"]), unit_ids[:1]),
                paragraph("para-2", str(span["span_id"]), unit_ids[1:]),
            ],
            "unresolved_boundaries": [],
        }

        report = ManuscriptDiscourseService.validate_paragraph_plan(
            self.structure, plan
        )
        rendered, realized, edits = ManuscriptDiscourseService.apply_paragraph_plan(
            HTML, self.structure, plan
        )

        self.assertTrue(report.valid)
        self.assertEqual([item["character_count"] for item in realized], [4, 12])
        self.assertIn('<p id="P1">第一文。</p>', rendered)
        self.assertIn('<p id="P2">第二文。第三文。第四文。</p>', rendered)
        self.assertEqual(
            {item["operation"] for item in edits},
            {"split_semantic_paragraph", "compose_semantic_paragraph"},
        )
        distribution = ManuscriptDiscourseService.realized_distribution(
            rendered, realized
        )
        self.assertEqual(distribution["minimum_characters"], 4)
        self.assertEqual(distribution["maximum_characters"], 12)
        self.assertEqual(distribution["single_unit_paragraph_count"], 1)

    def test_reordered_or_duplicated_units_are_rejected(self) -> None:
        plan = ManuscriptDiscourseService.default_paragraph_plan(self.structure)
        invalid = copy.deepcopy(plan)
        invalid["paragraphs"][0]["source_unit_ids"].reverse()
        invalid["paragraphs"][1]["source_unit_ids"].append(
            invalid["paragraphs"][1]["source_unit_ids"][0]
        )

        report = ManuscriptDiscourseService.validate_paragraph_plan(
            self.structure, invalid
        )
        codes = {issue.code for issue in report.issues}

        self.assertFalse(report.valid)
        self.assertIn("paragraph.duplicate_unit", codes)
        self.assertIn("paragraph.incomplete_or_reordered_span", codes)

    def test_multi_sentence_paragraph_requires_a_connected_rhetorical_chain(self) -> None:
        span = self.structure["spans"][0]
        unit_ids = [str(item["unit_id"]) for item in span["units"]]
        plan = {
            "policy": "semantic_coherence_not_fixed_length",
            "paragraphs": [
                {
                    **paragraph("para-1", str(span["span_id"]), unit_ids),
                    "rhetorical_chain": [],
                }
            ],
            "unresolved_boundaries": [],
        }

        report = ManuscriptDiscourseService.validate_paragraph_plan(
            self.structure, plan
        )

        self.assertFalse(report.valid)
        self.assertIn(
            "paragraph.rhetorical_chain_disconnected",
            {issue.code for issue in report.issues},
        )

    def test_one_shot_adjacencies_project_to_paragraphs_without_group_size_input(
        self,
    ) -> None:
        analysis = ManuscriptDiscourseService.default_boundary_analysis(self.structure)
        decisions = analysis["boundary_decisions"]
        decisions[0]["decision"] = "break"
        decisions[1]["decision"] = "continue"
        decisions[2]["decision"] = "continue"

        analysis_report = ManuscriptDiscourseService.validate_boundary_analysis(
            self.structure, analysis
        )
        plan = ManuscriptDiscourseService.compose_locked_paragraph_plan(
            self.structure,
            analysis,
            source_sha256="source-sha",
            source_boundary_provenance="approved_revision",
        )
        lock_report = ManuscriptDiscourseService.validate_locked_paragraph_plan(
            self.structure, plan, source_sha256="source-sha"
        )

        self.assertTrue(analysis_report["valid"])
        self.assertTrue(lock_report["valid"])
        self.assertEqual(
            [len(item["source_unit_ids"]) for item in plan["paragraphs"]],
            [1, 3],
        )
        self.assertNotIn("paragraph_count", analysis)
        self.assertTrue(
            all(
                item["lock_status"] == "locked_model_decision"
                for item in plan["boundary_decisions"]
            )
        )

    def test_boundary_analysis_requires_every_application_owned_edge_once(self) -> None:
        analysis = ManuscriptDiscourseService.default_boundary_analysis(self.structure)
        analysis["boundary_decisions"].pop()

        report = ManuscriptDiscourseService.validate_boundary_analysis(
            self.structure, analysis
        )

        self.assertFalse(report["valid"])
        self.assertIn(
            "paragraph.boundary_decision_coverage",
            {item["code"] for item in report["issues"]},
        )

    def test_unresolved_edge_is_locked_to_source_without_model_regeneration(self) -> None:
        analysis = ManuscriptDiscourseService.default_boundary_analysis(self.structure)
        source_boundary = next(
            item for item in analysis["boundary_decisions"] if item["source_boundary"]
        )
        source_boundary["decision"] = "unresolved"
        plan = ManuscriptDiscourseService.compose_locked_paragraph_plan(
            self.structure,
            analysis,
            source_sha256="source-sha",
            source_boundary_provenance="unapproved_machine_proposal",
        )
        locked = next(
            item
            for item in plan["boundary_decisions"]
            if item["boundary_id"] == source_boundary["boundary_id"]
        )

        self.assertEqual(locked["effective_decision"], "break")
        self.assertEqual(locked["lock_status"], "locked_source_fallback")
        self.assertEqual(len(plan["unresolved_boundaries"]), 1)

    def test_partition_signature_uses_sentence_order_not_mutable_block_ids(self) -> None:
        first_analysis = ManuscriptDiscourseService.default_boundary_analysis(
            self.structure
        )
        first_plan = ManuscriptDiscourseService.compose_locked_paragraph_plan(
            self.structure,
            first_analysis,
            source_sha256="first-source",
            source_boundary_provenance="approved_revision",
        )
        renamed_html = HTML.replace('id="P1"', 'id="Q1"').replace(
            'id="P2"', 'id="Q2"'
        )
        renamed_structure = ManuscriptDiscourseService.discourse_structure(
            renamed_html,
            ManuscriptCoherenceService.semantic_structure(renamed_html),
        )
        renamed_plan = ManuscriptDiscourseService.compose_locked_paragraph_plan(
            renamed_structure,
            ManuscriptDiscourseService.default_boundary_analysis(renamed_structure),
            source_sha256="renamed-source",
            source_boundary_provenance="unapproved_machine_proposal",
        )

        first_contract = first_plan["boundary_contract"]
        renamed_contract = renamed_plan["boundary_contract"]
        self.assertNotEqual(
            first_contract["boundary_vector_sha256"],
            renamed_contract["boundary_vector_sha256"],
        )
        self.assertEqual(
            first_contract["partition_signature_sha256"],
            renamed_contract["partition_signature_sha256"],
        )

    def test_length_shape_is_advisory_and_never_invalidates_boundaries(self) -> None:
        span = self.structure["spans"][0]
        span_id = str(span["span_id"])
        unit_ids = [str(item["unit_id"]) for item in span["units"]]
        plan = {
            "policy": "semantic-boundary-fixture",
            "paragraphs": [
                paragraph(f"para-{index}", span_id, [unit_id])
                for index, unit_id in enumerate(unit_ids, start=1)
            ],
            "unresolved_boundaries": [],
        }

        validation = ManuscriptDiscourseService.validate_paragraph_plan(
            self.structure, plan
        )
        diagnostics = ManuscriptDiscourseService.paragraph_regularity_diagnostics(
            plan
        )

        self.assertTrue(validation.valid)
        self.assertTrue(diagnostics["advisory_only"])
        self.assertFalse(diagnostics["fed_back_to_planner"])
        self.assertIn(
            "consecutive_single_unit_paragraphs",
            diagnostics["observations"],
        )

    def test_model_packet_omits_source_length_and_paragraph_count_statistics(self) -> None:
        packet = ManuscriptDiscourseService.substructure(
            self.structure, list(self.structure["spans"])
        )

        self.assertNotIn("source_distribution", packet)
        self.assertNotIn("source_paragraph_count", packet)
        self.assertNotIn("unit_count", packet)
        self.assertIn("boundaries", packet)

    def test_rendered_boundary_lock_rejects_downstream_reordering(self) -> None:
        analysis = ManuscriptDiscourseService.default_boundary_analysis(self.structure)
        plan = ManuscriptDiscourseService.compose_locked_paragraph_plan(
            self.structure,
            analysis,
            source_sha256="source-sha",
            source_boundary_provenance="approved_revision",
        )
        rendered, _, _ = ManuscriptDiscourseService.apply_paragraph_plan(
            HTML, self.structure, plan
        )
        lock = ManuscriptDiscourseService.rendered_boundary_lock(rendered, plan)
        reordered = rendered.replace(
            '<p id="P1">第一文。第二文。</p><p id="P2">第三文。第四文。</p>',
            '<p id="P2">第三文。第四文。</p><p id="P1">第一文。第二文。</p>',
        )

        report = ManuscriptDiscourseService.validate_rendered_boundary_lock(
            reordered, lock
        )

        self.assertFalse(report["valid"])
        self.assertIn(
            "paragraph.rendered_order_changed",
            {item["code"] for item in report["issues"]},
        )

    def test_concatenated_plan_revalidates_cross_batch_semantics(self) -> None:
        first = paragraph("para-1", "span-1", ["unit-1"])
        first.update(
            {
                "planner_batch_index": 1,
                "topic_entities": ["青年誌"],
                "temporal_scope": "2000年",
            }
        )
        second = paragraph("para-2", "span-2", ["unit-2"])
        second.update(
            {
                "planner_batch_index": 2,
                "topic_entities": ["少女漫画"],
                "temporal_scope": "1980年",
                "entry_relation": "continuation",
                "boundary_reason": "同じ話題だから。",
                "boundary_confidence": 0.6,
            }
        )

        report = ManuscriptDiscourseService.validate_global_paragraph_boundaries(
            {
                "paragraphs": [first, second],
                "unresolved_boundaries": ["年代の接続が未解決"],
            }
        )

        codes = {item["code"] for item in report["issues"]}
        self.assertFalse(report["valid"])
        self.assertTrue(
            {
                "paragraph.unresolved_boundary",
                "paragraph.cross_batch_low_confidence",
                "paragraph.cross_batch_entity_chain_gap",
                "paragraph.cross_batch_temporal_conflict",
            }.issubset(codes)
        )

    def test_global_validation_also_checks_boundaries_inside_one_batch(self) -> None:
        first = paragraph("para-1", "span-1", ["unit-1"])
        first.update({"planner_batch_index": 1, "topic_entities": ["青年誌"]})
        second = paragraph("para-2", "span-2", ["unit-2"])
        second.update(
            {
                "planner_batch_index": 1,
                "topic_entities": ["少女漫画"],
                "entry_relation": "continuation",
                "boundary_reason": "同じ論証をそのまま継続するためである。",
                "boundary_confidence": 0.9,
            }
        )

        report = ManuscriptDiscourseService.validate_global_paragraph_boundaries(
            {"paragraphs": [first, second], "unresolved_boundaries": []}
        )

        self.assertFalse(report["valid"])
        self.assertIn(
            "paragraph.global_entity_chain_gap",
            {item["code"] for item in report["issues"]},
        )

    def test_recomposition_preserves_common_source_attributes(self) -> None:
        html = HTML.replace(
            '<p id="P1">',
            '<p id="P1" class="body" data-source-page="1">',
        ).replace(
            '<p id="P2">',
            '<p id="P2" class="body" data-source-page="1">',
        )
        structure = ManuscriptDiscourseService.discourse_structure(
            html, ManuscriptCoherenceService.semantic_structure(html)
        )
        span = structure["spans"][0]
        unit_ids = [str(item["unit_id"]) for item in span["units"]]
        plan = {
            "policy": "semantic_coherence_not_fixed_length",
            "paragraphs": [
                paragraph("para-1", str(span["span_id"]), unit_ids),
            ],
            "unresolved_boundaries": [],
        }

        rendered, _, _ = ManuscriptDiscourseService.apply_paragraph_plan(
            html, structure, plan
        )

        self.assertIn('class="body"', rendered)
        self.assertIn('data-source-page="1"', rendered)

    def test_inline_markup_and_cross_reference_targets_are_fixed(self) -> None:
        protected_html = (
            '<article id="DOC" lang="ja"><section id="SEC">'
            '<h2 id="H1">第一節</h2>'
            '<p id="P1">通常の本文。</p>'
            '<p id="P2">引用<a href="#H1">章</a>。</p>'
            '<p id="P3">別の本文。</p>'
            '<p id="P4">参照先の本文。</p>'
            '<p id="P5"><a href="#P4">参照</a>。</p>'
            "</section></article>"
        )
        semantic = ManuscriptCoherenceService.semantic_structure(protected_html)

        structure = ManuscriptDiscourseService.discourse_structure(
            protected_html, semantic
        )

        self.assertTrue({"P2", "P4", "P5"}.issubset(structure["fixed_block_ids"]))
        self.assertEqual(
            [item["source_block_ids"] for item in structure["spans"]],
            [["P1"], ["P3"]],
        )

    def test_contextual_predicate_rewrite_requires_explicit_roles_and_invariants(self) -> None:
        source = (
            "媒体ごとに、男性の羞恥と女性の支配が回復に接続されるのかが"
            "調整されていたのである。"
        )
        html = (
            '<article id="DOC" lang="ja"><section id="SEC">'
            '<h2 id="H1">第一節</h2>'
            f'<p id="P1">{source}</p>'
            "</section></article>"
        )
        candidates = ManuscriptDiscourseService.predicate_candidates(html)
        replacement = (
            "媒体ごとに、男性の羞恥と女性の支配が、傷ついた男性主体の回復に"
            "どのように寄与するのかが調整されていたのである。"
        )
        payload = {
            "decisions": [
                {
                    "candidate_id": candidates[0]["candidate_id"],
                    "block_id": candidates[0]["block_id"],
                    "action": "rewrite",
                    "relation_type": "contribution",
                    "semantic_roles": {
                        "subject": "男性の羞恥と女性の支配",
                        "affected_entity": "傷ついた男性主体",
                        "initial_state": "心理的に傷ついた状態",
                        "target_state": "回復した状態",
                        "mediator": None,
                        "mechanism": "回復への寄与",
                    },
                    "replacement_text": replacement,
                    "reason": "抽象的な接続を寄与関係として明示する。",
                    "confidence": 0.93,
                    "meaning_preserved": True,
                    "modality_preserved": True,
                    "citation_preserved": True,
                    "review_finding_ids": [],
                    "evidence_ids": [],
                }
            ]
        }

        report = ManuscriptDiscourseService.assess_predicate_decisions(
            candidates, payload
        )

        self.assertEqual(len(candidates), 1)
        self.assertGreater(candidates[0]["abstract_likelihood"], 0)
        self.assertEqual(report["applied_count"], 1)
        self.assertEqual(report["safe_edits"][0]["text"], replacement)

    def test_literal_connection_is_preserved_and_low_confidence_is_reviewed(self) -> None:
        html = (
            '<article id="DOC" lang="ja"><section id="SEC">'
            '<h2 id="H1">第一節</h2>'
            '<p id="P1">クライアントはサーバーに接続する。</p>'
            "</section></article>"
        )
        candidates = ManuscriptDiscourseService.predicate_candidates(html)
        candidate_id = candidates[0]["candidate_id"]
        preserved = ManuscriptDiscourseService.assess_predicate_decisions(
            candidates,
            {
                "decisions": [
                    {
                        "candidate_id": candidate_id,
                        "block_id": candidates[0]["block_id"],
                        "action": "preserve",
                        "relation_type": "literal_connection",
                        "semantic_roles": {
                            "subject": "クライアント",
                            "affected_entity": "サーバー",
                            "initial_state": None,
                            "target_state": None,
                            "mediator": None,
                            "mechanism": "通信路の技術的接続",
                        },
                        "replacement_text": "",
                        "reason": "物理的・技術的な接続を表す。",
                        "confidence": 0.99,
                        "meaning_preserved": True,
                        "modality_preserved": True,
                        "citation_preserved": True,
                    }
                ]
            },
        )
        reviewed = ManuscriptDiscourseService.assess_predicate_decisions(
            candidates,
            {
                "decisions": [
                    {
                        "candidate_id": candidate_id,
                        "block_id": candidates[0]["block_id"],
                        "action": "rewrite",
                        "relation_type": "cause",
                        "semantic_roles": {"subject": "クライアント"},
                        "replacement_text": "クライアントが障害を引き起こす。",
                        "reason": "関係が確定していない。",
                        "confidence": 0.4,
                        "meaning_preserved": True,
                        "modality_preserved": True,
                        "citation_preserved": True,
                    }
                ]
            },
        )

        self.assertEqual(preserved["preserved_count"], 1)
        self.assertEqual(preserved["applied_count"], 0)
        self.assertEqual(reviewed["human_review_count"], 1)
        self.assertEqual(reviewed["applied_count"], 0)

    def test_abstract_relation_cannot_be_preserved_as_literal_connection(self) -> None:
        html = (
            '<article id="DOC" lang="ja"><section id="SEC">'
            '<h2 id="H1">第一節</h2>'
            '<p id="P1">羞恥と支配が主体の回復に接続される。</p>'
            "</section></article>"
        )
        candidate = ManuscriptDiscourseService.predicate_candidates(html)[0]
        report = ManuscriptDiscourseService.assess_predicate_decisions(
            [candidate],
            {
                "decisions": [
                    {
                        "candidate_id": candidate["candidate_id"],
                        "block_id": candidate["block_id"],
                        "action": "preserve",
                        "relation_type": "literal_connection",
                        "semantic_roles": {
                            "subject": "羞恥と支配",
                            "affected_entity": "主体の回復",
                            "initial_state": None,
                            "target_state": None,
                            "mediator": None,
                            "mechanism": "抽象概念間の接続",
                        },
                        "replacement_text": "",
                        "reason": "Fixture attempts a literal label.",
                        "confidence": 1.0,
                        "meaning_preserved": True,
                        "modality_preserved": True,
                        "citation_preserved": True,
                    }
                ]
            },
        )

        self.assertEqual(report["human_review_count"], 1)
        self.assertIn(
            "abstract_relation_mislabeled_as_literal",
            report["decisions"][0]["validation_issues"],
        )

    def test_non_finite_predicate_confidence_cannot_auto_apply(self) -> None:
        html = (
            '<article id="DOC" lang="ja"><section id="SEC">'
            '<h2 id="H1">第一節</h2>'
            '<p id="P1">羞恥と支配が主体の回復に接続される。</p>'
            "</section></article>"
        )
        candidate = ManuscriptDiscourseService.predicate_candidates(html)[0]
        report = ManuscriptDiscourseService.assess_predicate_decisions(
            [candidate],
            {
                "decisions": [
                    {
                        "candidate_id": candidate["candidate_id"],
                        "block_id": candidate["block_id"],
                        "action": "rewrite",
                        "relation_type": "contribution",
                        "semantic_roles": {
                            "subject": "羞恥と支配",
                            "affected_entity": "主体の回復",
                        },
                        "replacement_text": "羞恥と支配が主体の回復に寄与する。",
                        "reason": "Fixture uses a non-finite confidence.",
                        "confidence": float("nan"),
                        "meaning_preserved": True,
                        "modality_preserved": True,
                        "citation_preserved": True,
                    }
                ]
            },
        )

        self.assertEqual(report["applied_count"], 0)
        self.assertIn(
            "confidence_invalid",
            report["decisions"][0]["validation_issues"],
        )

    def test_predicate_roles_cannot_be_invented_outside_supplied_context(self) -> None:
        html = (
            '<article id="DOC" lang="ja"><section id="SEC">'
            '<h2 id="H1">第一節</h2>'
            '<p id="P1">羞恥と支配が回復に接続される。</p>'
            "</section></article>"
        )
        candidate = ManuscriptDiscourseService.predicate_candidates(html)[0]
        report = ManuscriptDiscourseService.assess_predicate_decisions(
            [candidate],
            {
                "decisions": [
                    {
                        "candidate_id": candidate["candidate_id"],
                        "block_id": candidate["block_id"],
                        "action": "rewrite",
                        "relation_type": "cause",
                        "semantic_roles": {
                            "subject": "本文に存在しない主体X",
                            "affected_entity": "本文に存在しない対象Y",
                            "initial_state": None,
                            "target_state": None,
                            "mediator": None,
                            "mechanism": "本文に存在しない機構Z",
                        },
                        "replacement_text": "架空の主体Xが架空の対象Yを回復させる。",
                        "reason": "Fixture attempts an ungrounded rewrite.",
                        "confidence": 1.0,
                        "meaning_preserved": True,
                        "modality_preserved": True,
                        "citation_preserved": True,
                    }
                ]
            },
        )

        self.assertEqual(report["applied_count"], 0)
        self.assertEqual(report["human_review_count"], 1)
        self.assertIn(
            "semantic_subject_not_grounded",
            report["decisions"][0]["validation_issues"],
        )

    def test_predicate_roles_cannot_copy_or_duplicate_the_full_source(self) -> None:
        html = (
            '<article id="DOC" lang="ja"><section id="SEC">'
            '<h2 id="H1">第一節</h2>'
            '<p id="P1">羞恥と支配が回復に接続される。</p>'
            "</section></article>"
        )
        candidate = ManuscriptDiscourseService.predicate_candidates(html)[0]
        source = str(candidate["source_text"])
        report = ManuscriptDiscourseService.assess_predicate_decisions(
            [candidate],
            {
                "decisions": [
                    {
                        "candidate_id": candidate["candidate_id"],
                        "block_id": candidate["block_id"],
                        "action": "preserve",
                        "relation_type": "contribution",
                        "semantic_roles": {
                            "subject": source,
                            "affected_entity": source,
                            "initial_state": None,
                            "target_state": None,
                            "mediator": None,
                            "mechanism": "欄を本文で水増しする",
                        },
                        "replacement_text": "",
                        "reason": "Fixture attempts to satisfy grounding by copying prose.",
                        "confidence": 1.0,
                        "meaning_preserved": True,
                        "modality_preserved": True,
                        "citation_preserved": True,
                    }
                ]
            },
        )

        validation = set(report["decisions"][0]["validation_issues"])
        self.assertEqual(report["applied_count"], 0)
        self.assertEqual(report["human_review_count"], 1)
        self.assertIn("semantic_roles_duplicated", validation)
        self.assertIn("semantic_role_copies_full_source", validation)

    def test_jawl_inventory_covers_all_predicate_capable_entries(self) -> None:
        html = (
            '<article id="DOC" lang="ja"><section id="SEC">'
            '<h2 id="H1">第一節</h2>'
            '<p id="P1">本稿は先行研究を分析し、その関係を説明する。</p>'
            "</section></article>"
        )

        candidate = ManuscriptDiscourseService.predicate_candidates(html)[0]

        self.assertEqual(candidate["lexicon"]["predicate_entry_count"], 1094)
        lemmas = {item["lemma"] for item in candidate["academic_predicates"]}
        self.assertTrue({"分析", "関係", "説明"}.issubset(lemmas))

    def test_academic_connective_inventory_covers_all_source_definitions(self) -> None:
        self.assertEqual(len(ACADEMIC_CONNECTIVES), 523)
        self.assertEqual(len({item.order for item in ACADEMIC_CONNECTIVES}), 523)
        self.assertEqual(len({item.surface for item in ACADEMIC_CONNECTIVES}), 523)

        matches = academic_connective_matches(
            "しかし、この結果は限定的である。言い換えれば、条件が異なる。"
        )
        surfaces = {str(item["surface"]) for item in matches}
        matched_text = {str(item["matched_text"]) for item in matches}

        self.assertIn("しかし", surfaces)
        self.assertIn("言い換えれば", matched_text)
        self.assertTrue(all(item["function_label"] for item in matches))

    def test_academic_connectives_match_sentence_initial_longest_form_only(self) -> None:
        internal_particle = academic_connective_matches("対象が異なる。")
        result = academic_connective_matches("この結果は限定的である。")

        self.assertFalse(internal_particle)
        self.assertEqual(
            {str(item["matched_text"]) for item in result},
            {"この結果は"},
        )

    def test_connective_only_block_is_a_fail_closed_audit_candidate(self) -> None:
        html = (
            '<article id="DOC" lang="ja"><section id="SEC">'
            '<h2 id="H1">第一節</h2>'
            '<p id="P1">しかし。</p>'
            "</section></article>"
        )

        candidate = ManuscriptDiscourseService.predicate_candidates(html)[0]

        self.assertEqual(candidate["risk_tier"], "academic_connective")
        self.assertEqual(candidate["connective_inventory"]["entry_count"], 523)
        self.assertFalse(candidate["connective_inventory"]["match_is_allow_list"])
        self.assertTrue(candidate["academic_connectives"])


if __name__ == "__main__":
    unittest.main()
