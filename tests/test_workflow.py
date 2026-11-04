from __future__ import annotations

import unittest

from pydantic import ValidationError

from fukamiru.authoring.policy import authoring_policy
from fukamiru.workflow.contracts import NodeInput, NodeOutput, NodeStatus
from fukamiru.workflow.graph import EdgeSpec, NodeSpec, WorkflowDefinition
from fukamiru.workflow.templates import (
    authoring_workflow,
    corpus_sync_workflow,
    corpus_sync_workflow_v1,
    ingestion_workflow,
    ingestion_workflow_v1,
    manuscript_rewrite_workflow,
    manuscript_rewrite_workflow_v2,
    manuscript_rewrite_workflow_v3,
    manuscript_rewrite_workflow_v4,
    manuscript_rewrite_workflow_v5,
    manuscript_rewrite_workflow_v6,
    manuscript_rewrite_workflow_v7,
    manuscript_rewrite_workflow_v8,
    manuscript_rewrite_workflow_v9,
    manuscript_rewrite_workflow_v10,
    manuscript_rewrite_workflow_v11,
    manuscript_rewrite_workflow_v12,
    manuscript_rewrite_workflow_v13,
    manuscript_rewrite_workflow_v16,
    manuscript_rewrite_workflow_v17,
    manuscript_rewrite_workflow_v21,
    manuscript_rewrite_workflow_v24,
    translation_workflow,
    translation_workflow_v4,
    translation_workflow_v5,
    translation_workflow_v6,
)


class WorkflowTests(unittest.TestCase):
    def test_templates_are_valid_and_repair_loop_is_explicit(self) -> None:
        ingestion = ingestion_workflow()
        translation = translation_workflow()
        translation_v4 = translation_workflow_v4()
        translation_v5 = translation_workflow_v5()
        corpus = corpus_sync_workflow()
        rewrite = manuscript_rewrite_workflow()
        rewrite_v2 = manuscript_rewrite_workflow_v2()
        rewrite_v3 = manuscript_rewrite_workflow_v3()
        rewrite_v4 = manuscript_rewrite_workflow_v4()
        rewrite_v5 = manuscript_rewrite_workflow_v5()
        rewrite_v6 = manuscript_rewrite_workflow_v6()
        rewrite_v7 = manuscript_rewrite_workflow_v7()
        rewrite_v8 = manuscript_rewrite_workflow_v8()
        rewrite_v9 = manuscript_rewrite_workflow_v9()
        rewrite_v10 = manuscript_rewrite_workflow_v10()
        rewrite_v11 = manuscript_rewrite_workflow_v11()
        rewrite_v12 = manuscript_rewrite_workflow_v12()
        rewrite_v13 = manuscript_rewrite_workflow_v13()
        rewrite_v16 = manuscript_rewrite_workflow_v16()
        rewrite_v17 = manuscript_rewrite_workflow_v17()
        rewrite_v21 = manuscript_rewrite_workflow_v21()
        self.assertEqual(ingestion.version, "2")
        self.assertEqual(
            ingestion.next_nodes("locate_source", "success"), ["inspect_text_layer"]
        )
        self.assertEqual(
            ingestion.next_nodes("run_ocr", "success"), ["register_asset"]
        )
        self.assertEqual(ingestion_workflow_v1().version, "1")
        authoring = authoring_workflow()
        self.assertEqual(authoring.version, "32")
        self.assertEqual(
            authoring_policy("32")["footnote_marker_placement"],
            "before-comma-and-period",
        )
        self.assertEqual(authoring_workflow(version="31").version, "31")
        self.assertEqual(
            authoring.next_nodes("prepare_note", "success"),
            ["prepare_rewrite_context", "extract_manuscript_references"],
        )
        self.assertEqual(
            authoring.next_nodes("draft_rewrite", "success"),
            ["reconcile_candidate_context"],
        )
        authoring_node_ids = {node.id for node in authoring.nodes}
        self.assertEqual(len(authoring_node_ids), 71)
        self.assertEqual(
            authoring.next_nodes("screen_proofreading_with_luna", "success"),
            ["rank_proofreading_candidates"],
        )
        self.assertEqual(
            authoring.next_nodes("rank_proofreading_candidates", "success"),
            ["select_proofreading_candidates"],
        )
        self.assertIn("prepare_rewrite_context", authoring_node_ids)
        self.assertIn("extract_manuscript_references", authoring_node_ids)
        self.assertTrue(
            all(
                node.input_json_contract == "authoring-context-v32"
                and node.output_json_contract == "authoring-context-v32"
                for node in authoring.nodes
            )
        )
        self.assertNotIn("persist_rewrite_proposal", authoring_node_ids)
        self.assertIn("finalize_authoring", authoring_node_ids)
        self.assertEqual(
            authoring.next_nodes("build_claim_tree", "success"),
            ["bind_claims_to_propositions"],
        )
        self.assertEqual(
            authoring.next_nodes("validate_evidence_support", "success"),
            ["bounded_research_retry"],
        )
        self.assertEqual(
            authoring.next_nodes("bounded_research_retry", "success"),
            ["resolve_reference_metadata"],
        )
        self.assertEqual(
            authoring.next_nodes("derive_atomic_evidence_obligations", "success"),
            ["build_source_preservation_map", "plan_research_queries"],
        )
        self.assertEqual(
            authoring.next_nodes("decide_evidence_realization", "success"),
            ["validate_evidence_ready"],
        )
        self.assertEqual(
            authoring.next_nodes("validate_evidence_ready", "success"),
            ["ratify_research_problem"],
        )
        self.assertEqual(
            authoring.next_nodes("audit_structure", "success"),
            ["classify_audit_findings"],
        )
        self.assertEqual(
            authoring.next_nodes("classify_audit_findings", "success"),
            ["join_draft_audits"],
        )
        self.assertEqual(
            translation.next_nodes("semantic_validation", "failure"),
            ["repair_translation"],
        )
        self.assertEqual(
            translation.next_nodes("repair_translation", "success"),
            ["translation_compliance_validation"],
        )
        self.assertEqual(rewrite.version, "29")
        self.assertEqual(manuscript_rewrite_workflow_v24().version, "24")
        for node_id in ("plan_research_queries",):
            self.assertEqual(
                next(node for node in rewrite.nodes if node.id == node_id).resource_lane,
                "ingestion_cpu",
            )
            self.assertEqual(
                next(
                    node for node in manuscript_rewrite_workflow_v24().nodes
                    if node.id == node_id
                ).resource_lane,
                "codex_remote",
            )
        self.assertEqual(rewrite_v2.version, "2")
        self.assertEqual(rewrite_v3.version, "3")
        self.assertEqual(rewrite_v4.version, "4")
        self.assertEqual(rewrite_v5.version, "5")
        self.assertEqual(rewrite_v6.version, "6")
        self.assertEqual(rewrite_v7.version, "7")
        self.assertEqual(rewrite_v8.version, "8")
        self.assertEqual(rewrite_v9.version, "9")
        self.assertEqual(rewrite_v10.version, "10")
        self.assertEqual(rewrite_v11.version, "11")
        self.assertEqual(rewrite_v12.version, "12")
        self.assertEqual(rewrite_v13.version, "13")
        self.assertEqual(rewrite_v16.version, "16")
        self.assertEqual(rewrite_v17.version, "17")
        self.assertEqual(rewrite_v21.version, "21")
        self.assertEqual(
            rewrite.next_nodes("prepare_rewrite_context", "success"),
            ["extract_manuscript_references"],
        )
        self.assertEqual(
            rewrite.next_nodes("extract_manuscript_references", "success"),
            ["review_manuscript_reasoning"],
        )
        self.assertEqual(
            rewrite.next_nodes("review_manuscript_reasoning", "success"),
            ["plan_manuscript_structure"],
        )
        self.assertEqual(
            rewrite.next_nodes("plan_manuscript_structure", "success"),
            ["plan_research_queries"],
        )
        self.assertEqual(
            rewrite.next_nodes("plan_research_queries", "success"),
            ["resolve_manuscript_evidence"],
        )
        self.assertEqual(
            rewrite.next_nodes("resolve_manuscript_evidence", "success"),
            ["validate_evidence_support"],
        )
        self.assertEqual(
            rewrite.next_nodes("validate_evidence_support", "success"),
            ["resolve_reference_metadata"],
        )
        self.assertEqual(
            rewrite.next_nodes("resolve_reference_metadata", "success"),
            ["resolve_enumeration_context"],
        )
        self.assertEqual(
            rewrite.next_nodes("resolve_enumeration_context", "success"),
            ["plan_term_introductions"],
        )
        self.assertEqual(
            rewrite.next_nodes("plan_term_introductions", "success"),
            ["plan_semantic_paragraphs"],
        )
        self.assertEqual(
            rewrite.next_nodes("plan_semantic_paragraphs", "success"),
            ["draft_rewrite"],
        )
        self.assertEqual(
            rewrite.next_nodes("draft_rewrite", "success"),
            ["calibrate_manuscript_certainty"],
        )
        self.assertEqual(
            rewrite.next_nodes("calibrate_manuscript_certainty", "success"),
            ["validate_rewrite_coherence"],
        )
        self.assertEqual(
            rewrite.next_nodes("validate_rewrite_coherence", "success"),
            ["extract_proofreading_sentences"],
        )
        self.assertEqual(
            rewrite.next_nodes("validate_rewrite_coherence", "conditional_success"),
            ["extract_proofreading_sentences"],
        )
        paragraph_node = next(
            node for node in rewrite.nodes if node.id == "plan_semantic_paragraphs"
        )
        self.assertEqual(paragraph_node.retry.max_attempts, 1)
        self.assertEqual(
            paragraph_node.title,
            "Annotate and lock one-shot semantic boundaries",
        )
        self.assertEqual(
            rewrite.next_nodes("extract_proofreading_sentences", "success"),
            ["generate_proofreading_candidates"],
        )
        self.assertEqual(
            rewrite.next_nodes("generate_proofreading_candidates", "success"),
            ["preprocess_proofreading_candidates"],
        )
        self.assertEqual(
            rewrite.next_nodes("preprocess_proofreading_candidates", "success"),
            ["screen_proofreading_with_luna"],
        )
        self.assertEqual(
            rewrite.next_nodes("screen_proofreading_with_luna", "success"),
            ["select_proofreading_candidates"],
        )
        self.assertEqual(
            rewrite.next_nodes("select_proofreading_candidates", "success"),
            ["publish_proofreading_shadow"],
        )
        self.assertEqual(
            rewrite.next_nodes("publish_proofreading_shadow", "success"),
            ["render_manuscript_references"],
        )
        self.assertEqual(
            rewrite.next_nodes("render_manuscript_references", "success"),
            ["validate_reference_quality"],
        )
        self.assertEqual(
            rewrite.next_nodes("validate_reference_quality", "success"),
            ["validate_rendered_structure"],
        )
        self.assertEqual(
            rewrite.next_nodes("validate_rendered_structure", "success"),
            ["repair_final_coherence"],
        )
        self.assertEqual(
            rewrite.next_nodes("repair_final_coherence", "success"),
            ["render_evidence_footnotes"],
        )
        self.assertEqual(
            rewrite.next_nodes("render_evidence_footnotes", "success"),
            ["validate_footnoted_references"],
        )
        self.assertEqual(
            rewrite.next_nodes("validate_footnoted_references", "success"),
            ["validate_footnoted_structure"],
        )
        self.assertEqual(
            rewrite.next_nodes("validate_footnoted_structure", "success"),
            ["validate_footnoted_coherence"],
        )
        self.assertEqual(
            rewrite.next_nodes("validate_footnoted_coherence", "success"),
            ["freeze_final_candidate"],
        )
        self.assertEqual(
            rewrite.next_nodes("freeze_final_candidate", "success"),
            ["validate_discourse_introductions"],
        )
        self.assertEqual(
            rewrite.next_nodes("validate_discourse_introductions", "success"),
            ["audit_final_findings"],
        )
        self.assertEqual(
            rewrite.next_nodes("audit_final_findings", "success"),
            ["validate_final_coherence"],
        )
        self.assertEqual(
            rewrite.next_nodes("validate_final_coherence", "success"),
            ["persist_rewrite_proposal"],
        )
        self.assertEqual(
            rewrite.next_nodes("validate_final_coherence", "conditional_success"),
            ["persist_rewrite_proposal"],
        )
        self.assertEqual(
            next(node for node in rewrite.nodes if node.id == "draft_rewrite").resource_lane,
            "codex_remote",
        )
        self.assertEqual(
            next(
                node for node in rewrite.nodes if node.id == "generate_proofreading_candidates"
            ).resource_lane,
            "local_llm_gpu",
        )
        self.assertEqual(corpus.version, "2")
        self.assertEqual(
            corpus.next_nodes("ingest_ready_sources", "success"),
            ["run_ocr_jobs"],
        )
        self.assertEqual(
            corpus.next_nodes("ingest_ocr_outputs", "success"),
            ["build_semantic_index"],
        )
        self.assertEqual(corpus_sync_workflow_v1().version, "1")
        self.assertEqual(
            next(node for node in corpus.nodes if node.id == "build_semantic_index").resource_lane,
            "embedding_gpu",
        )
        self.assertEqual(
            next(
                node for node in translation.nodes if node.id == "draft_translation"
            ).resource_lane,
            "codex_remote",
        )
        self.assertEqual(translation.version, "7")
        self.assertEqual(translation_workflow_v6().version, "6")
        self.assertEqual(
            next(node for node in translation.nodes if node.id == "human_review").resource_lane,
            "ingestion_cpu",
        )
        self.assertEqual(
            next(
                node for node in translation_workflow_v6().nodes
                if node.id == "human_review"
            ).resource_lane,
            "codex_remote",
        )
        self.assertEqual(translation_v4.version, "4")
        self.assertEqual(translation_v5.version, "5")
        self.assertEqual(
            translation_v4.next_nodes("ensure_ingested", "success"),
            ["gaperon_source_inspection"],
        )
        self.assertEqual(
            translation.next_nodes("gaperon_source_inspection", "success"),
            ["research_trigger_detection"],
        )
        self.assertEqual(
            translation.next_nodes("research_trigger_detection", "success"),
            ["academic_evidence_resolution"],
        )
        self.assertEqual(
            translation.next_nodes("draft_translation", "success"),
            ["translation_compliance_validation"],
        )
        self.assertEqual(
            authoring.next_nodes("audit_structure", "failure"),
            [],
        )
        self.assertEqual(
            authoring.next_nodes("audit_structure", "success"),
            ["classify_audit_findings"],
        )
        self.assertEqual(
            authoring.next_nodes("repair_failed_sections", "success"),
            ["reconcile_candidate_context"],
        )
        self.assertEqual(
            authoring.next_nodes("polish_prose", "success"),
            ["calibrate_manuscript_certainty"],
        )
        self.assertEqual(
            authoring.next_nodes("calibrate_manuscript_certainty", "success"),
            ["validate_draft_coherence"],
        )
        self.assertEqual(
            authoring.next_nodes("reconcile_candidate_context", "success"),
            ["validate_source_preservation", "audit_structure"],
        )
        self.assertEqual(
            authoring.next_nodes(
                "reconcile_candidate_context", "conditional_success"
            ),
            ["render_evidence_footnotes"],
        )
        self.assertEqual(
            authoring.next_nodes("publish_proofreading_shadow", "success"),
            ["japanese_surface_quality_gate"],
        )
        self.assertEqual(
            authoring.next_nodes("validate_reference_quality", "success"),
            ["join_submission_validations"],
        )
        self.assertEqual(
            authoring.next_nodes("freeze_final_candidate", "success"),
            ["validate_discourse_introductions"],
        )
        self.assertEqual(
            authoring.next_nodes(
                "join_submission_validations", "conditional_success"
            ),
            ["freeze_final_candidate"],
        )
        self.assertEqual(
            authoring.next_nodes("validate_final_coherence", "success"),
            ["evaluate_human_revision_requirement"],
        )

    def test_unreachable_node_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            WorkflowDefinition(
                name="bad_graph",
                version="1",
                title="Bad graph",
                entry_node="start",
                nodes=[
                    NodeSpec(id="start", handler="x.start", title="Start"),
                    NodeSpec(id="orphan", handler="x.orphan", title="Orphan"),
                ],
                edges=[],
            )

    def test_final_coherence_barrier_rejects_downstream_html_mutation(self) -> None:
        with self.assertRaisesRegex(
            ValidationError,
            "transformation is forbidden after validate_final_coherence",
        ):
            WorkflowDefinition(
                name="manuscript_rewrite",
                version="broken",
                title="Broken final barrier",
                entry_node="start",
                nodes=[
                    NodeSpec(id="start", handler="x.start", title="Start"),
                    NodeSpec(
                        id="validate_final_coherence",
                        handler="manuscript_rewrite.validate_coherence",
                        title="Final coherence",
                    ),
                    NodeSpec(
                        id="render_references",
                        handler="manuscript_rewrite.render_references",
                        title="Late mutation",
                    ),
                    NodeSpec(
                        id="persist",
                        handler="manuscript_rewrite.persist_proposal",
                        title="Persist",
                    ),
                ],
                edges=[
                    EdgeSpec(source="start", target="validate_final_coherence"),
                    EdgeSpec(
                        source="validate_final_coherence",
                        target="render_references",
                    ),
                    EdgeSpec(
                        source="validate_final_coherence",
                        target="render_references",
                        condition="conditional_success",
                    ),
                    EdgeSpec(source="render_references", target="persist"),
                ],
            )

    def test_node_contract_forbids_unknown_fields(self) -> None:
        node_input = NodeInput(run_id="run-1", node_id="node-1")
        self.assertEqual(node_input.contract_version, "1")
        output = NodeOutput(status=NodeStatus.COMPLETED)
        self.assertEqual(output.status, NodeStatus.COMPLETED)
        with self.assertRaises(ValidationError):
            NodeInput(run_id="run-1", node_id="node-1", unexpected=True)


if __name__ == "__main__":
    unittest.main()
