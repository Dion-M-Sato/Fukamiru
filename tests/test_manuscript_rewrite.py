from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from fukamiru.errors import FukamiruError
from fukamiru.projects.dissertation import DissertationService
from fukamiru.projects.service import ProjectService
from fukamiru.revisions import ManuscriptRewriteService, RevisionService
from fukamiru.revisions.discourse import ManuscriptDiscourseService
from fukamiru.revisions.references import JapaneseReferenceService
from fukamiru.storage.database import SQLiteDatabase


class ManuscriptRewriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        migrations = Path(__file__).resolve().parents[1] / "migrations" / "state"
        database = SQLiteDatabase(self.root / "state.db", migrations)
        database.migrate()
        self.connection = database.connect()
        self.project = ProjectService(self.connection).create(
            name="Rewrite fixture", root_path=self.root / "project"
        )
        revisions = RevisionService(self.connection)
        self.document_id = revisions.create_document(
            project_id=self.project["id"], kind="paper", title="Draft"
        )
        self.base = revisions.commit_revision(
            document_id=self.document_id,
            canonical_html=(
                '<article id="DOC-RW" lang="ja"><section id="SEC-RW">'
                '<h2 id="H-RW">章題</h2>'
                '<p id="P-RW">変更可能な本文。</p>'
                '<p id="P-CITED">先行研究<a href="#P-RW">参照</a>。</p>'
                "</section></article>"
            ),
            editor_type="human",
            operation="human_import",
        )
        self.service = ManuscriptRewriteService(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.temp.cleanup()

    def test_prepare_uses_linked_dissertation_outline(self) -> None:
        dissertations = DissertationService(self.connection)
        dissertation = dissertations.create(
            project_id=self.project["id"], title="Thesis"
        )
        root_id = dissertation["nodes"][0]["id"]
        chapter = dissertations.add_outline_node(
            dissertation_document_id=dissertation["document"]["id"],
            parent_id=root_id,
            node_type="chapter",
            title="Chapter argument",
            order_index=0,
        )
        dissertations.include_revision(
            dissertation_document_id=dissertation["document"]["id"],
            outline_node_id=chapter["id"],
            content_document_id=self.document_id,
            included_revision_id=self.base.revision_id,
            role="chapter",
            order_index=0,
        )

        prepared = self.service.prepare(
            document_id=self.document_id,
            base_revision_id=None,
            outline_node_id=None,
        )

        self.assertEqual(prepared["outline_node_id"], chapter["id"])
        self.assertIn(
            "Chapter argument",
            [item["title"] for item in prepared["outline_context"]["nodes"]],
        )

    def test_bounded_edits_protect_citation_markup_and_allow_safe_rewrite(self) -> None:
        base_html = RevisionService(self.connection).canonical_html(self.base.revision_id)
        proposed, applied, warnings = self.service.apply_edits(
            base_html,
            [
                {
                    "operation": "replace_text",
                    "block_id": "P-RW",
                    "text": "中心的な主張を明確化した本文。",
                },
                {
                    "operation": "replace_text",
                    "block_id": "P-CITED",
                    "text": "引用を消す変更。",
                },
                {
                    "operation": "insert_paragraph_after",
                    "block_id": "P-RW",
                    "text": "追加の論証。",
                },
            ],
            run_id="run-rewrite",
            batch_index=0,
        )

        self.assertEqual(len(applied), 2)
        self.assertIn("中心的な主張を明確化した本文。", proposed)
        self.assertIn("追加の論証。", proposed)
        self.assertIn("先行研究<a", proposed)
        self.assertTrue(any("citation/quote" in item for item in warnings))

    def test_adjacent_paragraphs_can_be_merged_as_one_bounded_edit(self) -> None:
        base_html = RevisionService(self.connection).canonical_html(self.base.revision_id)
        mergeable = base_html.replace(
            '<p id="P-CITED">先行研究<a href="#P-RW">参照</a>。</p>',
            '<p id="P-NEXT">隣接する論証。</p>',
        )
        proposed, applied, warnings = self.service.apply_edits(
            mergeable,
            [
                {
                    "operation": "merge_with_next",
                    "block_id": "P-RW",
                    "text": "変更可能な本文と隣接する論証を統合した段落。",
                }
            ],
            run_id="run-merge",
            batch_index=0,
        )

        self.assertEqual(warnings, [])
        self.assertEqual(len(applied), 1)
        self.assertIn("統合した段落", proposed)
        self.assertNotIn("P-NEXT", proposed)

    def test_markdown_section_heading_cannot_be_merged_into_prose(self) -> None:
        base_html = RevisionService(self.connection).canonical_html(self.base.revision_id)
        candidate = base_html.replace(
            '<p id="P-RW">変更可能な本文。</p>',
            '<p id="P-RW">### 2　次節</p><p id="P-NEXT">次節の本文。</p>',
        )

        proposed, applied, warnings = self.service.apply_edits(
            candidate,
            [
                {
                    "operation": "merge_with_next",
                    "block_id": "P-RW",
                    "text": "見出しと本文を統合。",
                }
            ],
            run_id="run-semantic-heading",
            batch_index=0,
        )

        self.assertEqual(applied, [])
        self.assertEqual(proposed, candidate)
        self.assertTrue(any("Unsafe paragraph merge" in item for item in warnings))

    def test_prepare_can_use_a_pending_proposal_without_changing_current_revision(self) -> None:
        base_html = RevisionService(self.connection).canonical_html(self.base.revision_id)
        proposal = self.service.persist_proposal(
            run_id=self._run_row(),
            document_id=self.document_id,
            project_id=self.project["id"],
            base_revision_id=self.base.revision_id,
            outline_node_id=None,
            instructions="First rewrite",
            use_rag=False,
            context={},
            source_html=base_html,
            proposed_html=base_html.replace("変更可能な本文。", "未承認の改稿案。"),
            edit_summary={"applied_edit_count": 1},
            quality_status="needs_human_review",
        )

        prepared = self.service.prepare(
            document_id=self.document_id,
            base_revision_id=self.base.revision_id,
            outline_node_id=None,
            source_proposal_id=proposal["id"],
        )

        self.assertEqual(prepared["source_proposal_id"], proposal["id"])
        self.assertIn("未承認の改稿案。", prepared["base_html"])
        current = self.connection.execute(
            "SELECT current_revision_id FROM content_documents WHERE id = ?",
            (self.document_id,),
        ).fetchone()[0]
        self.assertEqual(current, self.base.revision_id)

    def test_v22_partial_response_saves_proposal_but_blocks_final_revision(self) -> None:
        base_html = RevisionService(self.connection).canonical_html(self.base.revision_id)
        condition = {
            "reason": "response_contract_incomplete",
            "phase": "rewrite",
            "unit_ids": ["P-RW"],
            "unit_count": 1,
            "retry_count": 1,
            "disposition": "conditional_proposal",
        }
        proposal = self.service.persist_proposal(
            run_id=self._run_row(),
            document_id=self.document_id,
            project_id=self.project["id"],
            base_revision_id=self.base.revision_id,
            outline_node_id=None,
            instructions="Preserve a reviewable partial response",
            use_rag=False,
            context={
                "workflow_version": 22,
                "completion_status": "conditional_success",
                "conditional_success": condition,
                "conditional_conditions": [condition],
                "reference_entries": [],
                "machine_reference_entries": [],
                "reference_quality_gate": {"passed": True, "issues": []},
                "completion_gate": {
                    "passed": False,
                    "quality_status": "needs_human_review",
                    "blocking_count": 1,
                    "blocking_reasons": [
                        {
                            "code": "response_contract.incomplete:rewrite",
                            "message": "Mandatory inference response incomplete.",
                            "details": condition,
                        }
                    ],
                },
            },
            source_html=base_html,
            proposed_html=base_html,
            edit_summary={"applied_edit_count": 0},
            quality_status="needs_human_review",
        )

        self.assertEqual(proposal["status"], "needs_human_review")
        self.assertEqual(proposal["quality_status"], "needs_human_review")
        with self.assertRaisesRegex(FukamiruError, "必須推論の部分応答"):
            self.service.confirm(proposal["id"])
        current = self.connection.execute(
            "SELECT current_revision_id FROM content_documents WHERE id = ?",
            (self.document_id,),
        ).fetchone()[0]
        self.assertEqual(current, self.base.revision_id)

    def test_v22_genuine_semantic_uncertainty_blocks_final_revision(self) -> None:
        base_html = RevisionService(self.connection).canonical_html(self.base.revision_id)
        requirement = {
            "requirement_id": "semantic-term-1",
            "phase": "final",
            "block_id": "P-RW",
            "kind": "term_introduction",
            "status": "semantic_analysis_unresolved",
            "review_class": "human_semantic_judgement_required",
            "disposition": "human_review_required",
            "explanation": "初出語の意味を人間が判断する必要があります。",
        }
        proposal = self.service.persist_proposal(
            run_id=self._run_row(),
            document_id=self.document_id,
            project_id=self.project["id"],
            base_revision_id=self.base.revision_id,
            outline_node_id=None,
            instructions="Keep semantic uncertainty reviewable",
            use_rag=False,
            context={
                "workflow_version": 22,
                "completion_status": "conditional_success",
                "conditional_conditions": [
                    {"reason": "language_review_required", "requirement_count": 1}
                ],
                "language_review_requirements": [requirement],
                "reference_entries": [],
                "machine_reference_entries": [],
                "reference_quality_gate": {"passed": True, "issues": []},
            },
            source_html=base_html,
            proposed_html=base_html,
            edit_summary={"applied_edit_count": 0},
            quality_status="needs_human_review",
        )

        with self.assertRaisesRegex(FukamiruError, "人間の判断が必要"):
            self.service.confirm(proposal["id"])
        current = self.connection.execute(
            "SELECT current_revision_id FROM content_documents WHERE id = ?",
            (self.document_id,),
        ).fetchone()[0]
        self.assertEqual(current, self.base.revision_id)

    def test_v22_minor_lexical_source_preservation_can_be_human_approved(self) -> None:
        base_html = RevisionService(self.connection).canonical_html(self.base.revision_id)
        requirement = {
            "requirement_id": "minor-lexical-1",
            "phase": "candidate_selection",
            "block_id": "P-RW",
            "kind": "contextual_lexicon",
            "status": "candidates_inappropriate",
            "review_class": "minor_lexical_source_preserved",
            "disposition": "source_preserved",
            "explanation": "安全な候補がないため原文を維持しました。",
        }
        proposal = self.service.persist_proposal(
            run_id=self._run_row(),
            document_id=self.document_id,
            project_id=self.project["id"],
            base_revision_id=self.base.revision_id,
            outline_node_id=None,
            instructions="Preserve the original minor lexical choice",
            use_rag=False,
            context={
                "workflow_version": 22,
                "completion_status": "conditional_success",
                "conditional_conditions": [
                    {"reason": "language_review_required", "requirement_count": 1}
                ],
                "language_review_requirements": [requirement],
                "reference_entries": [],
                "machine_reference_entries": [],
                "reference_quality_gate": {"passed": True, "issues": []},
            },
            source_html=base_html,
            proposed_html=base_html,
            edit_summary={"applied_edit_count": 0},
            quality_status="needs_human_review",
        )

        confirmed = self.service.confirm(proposal["id"])

        self.assertEqual(confirmed["status"], "confirmed")
        current = self.connection.execute(
            "SELECT current_revision_id FROM content_documents WHERE id = ?",
            (self.document_id,),
        ).fetchone()[0]
        self.assertNotEqual(current, self.base.revision_id)

    def test_v24_bulk_approval_preserves_unresolved_audit_and_checks_snapshot(self) -> None:
        base_html = RevisionService(self.connection).canonical_html(self.base.revision_id)
        proposed_html = base_html.replace("変更可能な本文。", "慎重に限定した本文。")
        proposal = self.service.persist_proposal(
            run_id=self._run_row(),
            document_id=self.document_id,
            project_id=self.project["id"],
            base_revision_id=self.base.revision_id,
            outline_node_id=None,
            instructions="Revise within node rules",
            use_rag=False,
            context={
                "workflow_version": 24,
                "structure_plan_validation": {
                    "valid": False, "valid_for_conditional_execution": True,
                },
                "paragraph_plan_validation": {
                    "valid": True, "global_valid": True, "fallback_used": True,
                },
                "paragraph_plan": {"unresolved_boundaries": ["source boundary retained"]},
                "paragraph_boundary_lock": ManuscriptDiscourseService.rendered_boundary_lock(
                    proposed_html, {"boundary_contract": {"boundary_vector_sha256": "test"}}
                ),
                "completion_status": "conditional_success",
                "conditional_success": {
                    "status": "conditional_success",
                    "reason": "coherence_unresolved",
                    "unresolved_routes": [
                        {"resolution_route": "claim_limitation", "block_ids": ["P-RW"]}
                    ],
                },
                "conditional_conditions": [
                    {"reason": "semantic_resolution_required", "phase": "argument"}
                ],
                "reference_entries": [],
                "machine_reference_entries": [],
                "reference_quality_gate": {"passed": False, "issues": ["unverified"]},
            },
            source_html=base_html,
            proposed_html=proposed_html,
            edit_summary={"applied_edit_count": 1},
            quality_status="needs_human_review",
        )
        with self.assertRaisesRegex(FukamiruError, "一括承認"):
            self.service.confirm(proposal["id"])
        with self.assertRaisesRegex(FukamiruError, "更新されています"):
            self.service.confirm(
                proposal["id"], expected_sha256="0" * 64, acknowledge_unresolved=True
            )
        reordered_html = proposed_html.replace(
            '<p id="P-RW">慎重に限定した本文。</p>'
            '<p id="P-CITED">先行研究<a href="#P-RW">参照</a>。</p>',
            '<p id="P-CITED">先行研究<a href="#P-RW">参照</a>。</p>'
            '<p id="P-RW">慎重に限定した本文。</p>',
        )
        self.assertNotEqual(reordered_html, proposed_html)
        reordered = self.service.update_proposed(proposal["id"], canonical_html=reordered_html)
        with self.assertRaisesRegex(FukamiruError, "構造・段落境界"):
            self.service.confirm(
                proposal["id"],
                expected_sha256=reordered["proposed_sha256"],
                acknowledge_unresolved=True,
            )
        changed_section = self.service.update_proposed(
            proposal["id"],
            canonical_html=proposed_html.replace('id="SEC-RW"', 'id="SEC-CHANGED"'),
        )
        with self.assertRaisesRegex(FukamiruError, "構造・段落境界"):
            self.service.confirm(
                proposal["id"],
                expected_sha256=changed_section["proposed_sha256"],
                acknowledge_unresolved=True,
            )
        proposal = self.service.update_proposed(proposal["id"], canonical_html=proposed_html)
        confirmed = self.service.confirm(
            proposal["id"],
            expected_sha256=proposal["proposed_sha256"],
            acknowledge_unresolved=True,
        )
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(
            confirmed["context"]["bulk_approval"]["status"],
            "approved_with_unresolved",
        )
        self.assertEqual(
            confirmed["context"]["bulk_approval"]["approved_candidate_sha256"],
            proposal["proposed_sha256"],
        )
        self.assertEqual(
            confirmed["context"]["conditional_success"]["reason"],
            "coherence_unresolved",
        )

    def test_v22_grouped_semantic_units_are_resolved_individually_with_cas(self) -> None:
        base_html = RevisionService(self.connection).canonical_html(self.base.revision_id)
        condition = {
            "reason": "semantic_resolution_required",
            "phase": "argument_process:claim_evidence_links",
            "category": "claim_causality",
            "unit_ids": ["claim-link-a", "claim-link-b"],
            "block_ids": ["P-RW"],
            "disposition": "human_judgement_required",
        }
        proposal = self.service.persist_proposal(
            run_id=self._run_row(),
            document_id=self.document_id,
            project_id=self.project["id"],
            base_revision_id=self.base.revision_id,
            outline_node_id=None,
            instructions="Adjudicate grouped semantic uncertainty",
            use_rag=False,
            context={
                "workflow_version": 22,
                "completion_status": "conditional_success",
                "conditional_success": condition,
                "conditional_conditions": [condition],
                "reference_entries": [],
                "machine_reference_entries": [],
                "reference_quality_gate": {"passed": True, "issues": []},
                "completion_gate": {
                    "passed": False,
                    "quality_status": "needs_human_review",
                    "blocking_count": 1,
                    "blocking_reasons": [
                        {
                            "code": (
                                "reasoning.semantic_resolution_required:"
                                "argument_process:claim_evidence_links"
                            ),
                            "message": "Human judgement required.",
                            "details": condition,
                        }
                    ],
                },
            },
            source_html=base_html,
            proposed_html=base_html,
            edit_summary={"applied_edit_count": 0},
            quality_status="needs_human_review",
        )

        first_read = self.service.get(proposal["id"])
        second_read = self.service.get(proposal["id"])
        first_items = first_read["context"]["semantic_review_items"]
        self.assertEqual(len(first_items), 2)
        self.assertEqual(
            [item["review_id"] for item in first_items],
            [
                item["review_id"]
                for item in second_read["context"]["semantic_review_items"]
            ],
        )
        with self.assertRaisesRegex(FukamiruError, "Unknown semantic review decision"):
            self.service.resolve_semantic_review_v22(
                proposal["id"],
                first_items[0]["review_id"],
                proposal["proposed_sha256"],
                first_read["semantic_review_context_sha256"],
                decision="arbitrary_unreviewed_value",
                rationale="列挙された判断以外では意味条件を解消できない。",
            )

        resolved_first = self.service.resolve_semantic_review_v22(
            proposal["id"],
            first_items[0]["review_id"],
            proposal["proposed_sha256"],
            first_read["semantic_review_context_sha256"],
            decision="confirm_current_interpretation",
            rationale="第一の因果関係は本文の定義と整合する。",
        )

        remaining = resolved_first["context"]["semantic_review_items"]
        self.assertEqual([item["unit_id"] for item in remaining], ["claim-link-b"])
        stored_condition = next(
            item
            for item in resolved_first["context"]["conditional_conditions"]
            if item.get("reason") == "semantic_resolution_required"
        )
        self.assertEqual(stored_condition["unit_ids"], ["claim-link-a", "claim-link-b"])
        self.assertEqual(stored_condition["resolved_unit_ids"], ["claim-link-a"])
        with self.assertRaisesRegex(FukamiruError, "reload"):
            self.service.resolve_semantic_review_v22(
                proposal["id"],
                remaining[0]["review_id"],
                proposal["proposed_sha256"],
                first_read["semantic_review_context_sha256"],
                decision="clarify_interpretation",
                rationale="古いcontext SHAでは更新できない。",
            )
        with self.assertRaisesRegex(FukamiruError, "reload"):
            self.service.resolve_semantic_review_v22(
                proposal["id"],
                remaining[0]["review_id"],
                "0" * 64,
                resolved_first["semantic_review_context_sha256"],
                decision="clarify_interpretation",
                rationale="古い本文SHAでも更新できない。",
            )
        with self.assertRaisesRegex(FukamiruError, "人間の判断が必要"):
            self.service.confirm(proposal["id"])

        resolved_all = self.service.resolve_semantic_review_v22(
            proposal["id"],
            remaining[0]["review_id"],
            proposal["proposed_sha256"],
            resolved_first["semantic_review_context_sha256"],
            decision="clarify_interpretation",
            rationale="第二の因果関係は限定条件つきで成立する。",
        )
        self.assertEqual(resolved_all["context"]["semantic_review_items"], [])
        self.assertFalse(
            any(
                item.get("reason") == "semantic_resolution_required"
                for item in resolved_all["context"]["conditional_conditions"]
            )
        )
        self.assertEqual(
            len(resolved_all["context"]["semantic_adjudication_history"]), 2
        )
        events = self.connection.execute(
            "SELECT payload_json FROM event_log "
            "WHERE run_id=? AND event_type='manuscript.semantic_review.resolved' "
            "ORDER BY event_id",
            (proposal["run_id"],),
        ).fetchall()
        self.assertEqual(len(events), 2)
        self.assertEqual(json.loads(events[0][0])["unit_id"], "claim-link-a")
        confirmed = self.service.confirm(proposal["id"])
        self.assertIn(confirmed["status"], {"confirmed", "checkpoint_failed"})

    def test_v22_semantic_action_resolves_only_true_semantic_requirement(self) -> None:
        base_html = RevisionService(self.connection).canonical_html(self.base.revision_id)
        semantic = {
            "requirement_id": "semantic-term-1",
            "phase": "final",
            "block_id": "P-RW",
            "kind": "term_introduction",
            "status": "semantic_analysis_unresolved",
            "review_class": "human_semantic_judgement_required",
            "explanation": "初出語の意味が未確定です。",
        }
        minor = {
            "requirement_id": "minor-term-1",
            "phase": "candidate_selection",
            "block_id": "P-CITED",
            "kind": "contextual_lexicon",
            "status": "candidates_inappropriate",
            "review_class": "minor_lexical_source_preserved",
            "disposition": "source_preserved",
        }
        proposal = self.service.persist_proposal(
            run_id=self._run_row(),
            document_id=self.document_id,
            project_id=self.project["id"],
            base_revision_id=self.base.revision_id,
            outline_node_id=None,
            instructions="Adjudicate one semantic requirement",
            use_rag=False,
            context={
                "workflow_version": 22,
                "completion_status": "conditional_success",
                "conditional_conditions": [
                    {
                        "reason": "language_review_required",
                        "requirement_count": 2,
                        "requirement_ids": ["semantic-term-1", "minor-term-1"],
                    }
                ],
                "conditional_success": {
                    "reason": "language_review_required",
                    "requirement_count": 2,
                },
                "language_review_requirements": [semantic, minor],
                "reference_entries": [],
                "machine_reference_entries": [],
                "reference_quality_gate": {"passed": True, "issues": []},
            },
            source_html=base_html,
            proposed_html=base_html,
            edit_summary={"applied_edit_count": 0},
            quality_status="needs_human_review",
        )
        fetched = self.service.get(proposal["id"])
        self.assertEqual(len(fetched["context"]["semantic_review_items"]), 1)
        manually_edited = self.service.update_proposed(
            proposal["id"],
            canonical_html=base_html.replace(
                "変更可能な本文。", "人間が手修正した本文である。"
            ),
        )
        self.assertEqual(
            len(manually_edited["context"]["semantic_review_items"]), 1
        )
        self.assertEqual(
            manually_edited["context"]["language_review_requirements"][0]["status"],
            "semantic_analysis_unresolved",
        )
        fetched = manually_edited
        human_item = fetched["context"]["semantic_review_items"][0]
        minor_review_id = self.service._language_semantic_review_id_v22(minor)
        with self.assertRaisesRegex(FukamiruError, "Only human_semantic"):
            self.service.resolve_semantic_review_v22(
                proposal["id"],
                minor_review_id,
                fetched["proposed_sha256"],
                fetched["semantic_review_context_sha256"],
                decision="confirm_current_interpretation",
                rationale="語彙候補は意味判定APIの対象ではない。",
            )

        updated = self.service.resolve_semantic_review_v22(
            proposal["id"],
            human_item["review_id"],
            fetched["proposed_sha256"],
            fetched["semantic_review_context_sha256"],
            decision="clarify_interpretation",
            rationale="この語は本文中では媒体による選別規則を意味する。",
        )
        requirements = updated["context"]["language_review_requirements"]
        self.assertEqual(requirements[0]["status"], "resolved")
        self.assertEqual(requirements[0]["human_judgement"]["actor_type"], "human")
        self.assertEqual(requirements[1]["status"], "candidates_inappropriate")
        self.assertFalse(requirements[1]["final_revision_blocked"])
        condition = next(
            item
            for item in updated["context"]["conditional_conditions"]
            if item.get("reason") == "language_review_required"
        )
        self.assertEqual(condition["requirement_ids"], ["minor-term-1"])
        self.assertEqual(
            updated["context"]["conditional_success"]["requirement_ids"],
            ["minor-term-1"],
        )

        edited = self.service.update_proposed(
            proposal["id"], canonical_html=base_html
        )
        self.assertEqual(edited["context"]["semantic_review_items"], [])
        self.assertEqual(len(edited["context"]["semantic_adjudication_history"]), 1)

    def test_v22_last_language_semantic_resolution_clears_conditional_success(
        self,
    ) -> None:
        base_html = RevisionService(self.connection).canonical_html(self.base.revision_id)
        requirement = {
            "requirement_id": "semantic-only-1",
            "phase": "final",
            "block_id": "P-RW",
            "kind": "citation_function",
            "status": "semantic_analysis_unresolved",
            "review_class": "human_semantic_judgement_required",
        }
        language_condition = {
            "reason": "language_review_required",
            "requirement_count": 1,
            "requirement_ids": ["semantic-only-1"],
        }
        proposal = self.service.persist_proposal(
            run_id=self._run_row(),
            document_id=self.document_id,
            project_id=self.project["id"],
            base_revision_id=self.base.revision_id,
            outline_node_id=None,
            instructions="Resolve the final language semantic requirement",
            use_rag=False,
            context={
                "workflow_version": 22,
                "completion_status": "conditional_success",
                "conditional_conditions": [language_condition],
                "conditional_success": language_condition,
                "language_review_requirements": [requirement],
                "reference_entries": [],
                "machine_reference_entries": [],
                "reference_quality_gate": {"passed": True, "issues": []},
            },
            source_html=base_html,
            proposed_html=base_html,
            edit_summary={},
            quality_status="needs_human_review",
        )
        fetched = self.service.get(proposal["id"])
        item = fetched["context"]["semantic_review_items"][0]

        resolved = self.service.resolve_semantic_review_v22(
            proposal["id"],
            item["review_id"],
            proposal["proposed_sha256"],
            fetched["semantic_review_context_sha256"],
            decision="confirm_current_interpretation",
            rationale="引用は直前の論点を限定するために用いられている。",
        )

        self.assertEqual(resolved["context"]["conditional_conditions"], [])
        self.assertNotIn("conditional_success", resolved["context"])
        self.assertEqual(resolved["context"]["completion_status"], "success")

    def test_v22_unknown_language_review_class_blocks_final_revision(self) -> None:
        base_html = RevisionService(self.connection).canonical_html(self.base.revision_id)
        requirement = {
            "requirement_id": "mandatory-revision-1",
            "status": "mandatory_revision_unmet",
            "review_class": "mandatory_revision_unmet",
        }
        proposal = self.service.persist_proposal(
            run_id=self._run_row(),
            document_id=self.document_id,
            project_id=self.project["id"],
            base_revision_id=self.base.revision_id,
            outline_node_id=None,
            instructions="Do not approve an unmet mandatory revision",
            use_rag=False,
            context={
                "workflow_version": 22,
                "language_review_requirements": [requirement],
                "reference_entries": [],
                "machine_reference_entries": [],
                "reference_quality_gate": {"passed": True, "issues": []},
            },
            source_html=base_html,
            proposed_html=base_html,
            edit_summary={"applied_edit_count": 0},
            quality_status="needs_human_review",
        )

        with self.assertRaisesRegex(FukamiruError, "mandatory_revision_unmet"):
            self.service.confirm(proposal["id"])

    def test_prepare_exposes_source_proposal_run_lineage(self) -> None:
        base_html = RevisionService(self.connection).canonical_html(self.base.revision_id)

        def insert_run(run_id: str, inputs: dict[str, object]) -> None:
            self.connection.execute(
                """
                INSERT INTO runs(
                    id, project_id, workflow_name, workflow_version,
                    status, input_json, created_at
                ) VALUES (?, ?, 'manuscript_rewrite', '8', 'completed', ?, 'now')
                """,
                (run_id, self.project["id"], json.dumps(inputs)),
            )
            self.connection.commit()

        insert_run("lineage-ancestor-run", {})
        ancestor = self.service.persist_proposal(
            run_id="lineage-ancestor-run",
            document_id=self.document_id,
            project_id=self.project["id"],
            base_revision_id=self.base.revision_id,
            outline_node_id=None,
            instructions="Ancestor rewrite",
            use_rag=False,
            context={},
            source_html=base_html,
            proposed_html=base_html.replace("変更可能な本文。", "祖先案の本文。"),
            edit_summary={"applied_edit_count": 1},
            quality_status="needs_human_review",
        )
        insert_run(
            "lineage-direct-run", {"source_proposal_id": ancestor["id"]}
        )
        direct = self.service.persist_proposal(
            run_id="lineage-direct-run",
            document_id=self.document_id,
            project_id=self.project["id"],
            base_revision_id=self.base.revision_id,
            outline_node_id=None,
            instructions="Direct rewrite",
            use_rag=False,
            context={},
            source_html=ancestor["proposed_html"],
            proposed_html=ancestor["proposed_html"].replace("祖先案", "直接案"),
            edit_summary={"applied_edit_count": 1},
            quality_status="needs_human_review",
        )

        prepared = self.service.prepare(
            document_id=self.document_id,
            base_revision_id=self.base.revision_id,
            outline_node_id=None,
            source_proposal_id=direct["id"],
        )

        self.assertEqual(
            [item["run_id"] for item in prepared["source_lineage"]],
            ["lineage-direct-run", "lineage-ancestor-run"],
        )
        self.assertTrue(
            all(item["workflow_version"] == "8" for item in prepared["source_lineage"])
        )

    def test_discard_rejects_proposal_without_creating_a_revision(self) -> None:
        base_html = RevisionService(self.connection).canonical_html(self.base.revision_id)
        proposal = self.service.persist_proposal(
            run_id=self._run_row(),
            document_id=self.document_id,
            project_id=self.project["id"],
            base_revision_id=self.base.revision_id,
            outline_node_id=None,
            instructions="Discard this generated draft",
            use_rag=False,
            context={},
            source_html=base_html,
            proposed_html=base_html.replace("変更可能な本文。", "破棄対象の改稿案。"),
            edit_summary={"applied_edit_count": 1},
            quality_status="needs_human_review",
        )

        discarded = self.service.discard(proposal["id"])

        self.assertEqual(discarded["status"], "rejected")
        self.assertIsNone(discarded["confirmed_revision_id"])
        self.assertIn("破棄対象の改稿案。", discarded["proposed_html"])
        current_revision_id = self.connection.execute(
            "SELECT current_revision_id FROM content_documents WHERE id = ?",
            (self.document_id,),
        ).fetchone()[0]
        self.assertEqual(current_revision_id, self.base.revision_id)
        prepared = self.service.prepare(
            document_id=self.document_id,
            base_revision_id=self.base.revision_id,
            outline_node_id=None,
            source_proposal_id=proposal["id"],
        )
        self.assertEqual(prepared["source_proposal_id"], proposal["id"])
        self.assertIn("破棄対象の改稿案。", prepared["base_html"])
        self.assertEqual(
            self.connection.execute(
                "SELECT current_revision_id FROM content_documents WHERE id = ?",
                (self.document_id,),
            ).fetchone()[0],
            self.base.revision_id,
        )
        event = self.connection.execute(
            """
            SELECT event_type, object_id FROM event_log
            WHERE run_id = ? ORDER BY created_at DESC LIMIT 1
            """,
            (proposal["run_id"],),
        ).fetchone()
        self.assertEqual(event["event_type"], "manuscript.rewrite.discarded")
        self.assertEqual(event["object_id"], proposal["id"])
        with self.assertRaisesRegex(FukamiruError, "pending"):
            self.service.discard(proposal["id"])
        with self.assertRaisesRegex(FukamiruError, "no longer pending"):
            self.service.confirm(proposal["id"])

    def test_stale_proposal_cannot_overwrite_a_newer_human_revision(self) -> None:
        base_html = RevisionService(self.connection).canonical_html(self.base.revision_id)
        proposal = self.service.persist_proposal(
            run_id=self._run_row(),
            document_id=self.document_id,
            project_id=self.project["id"],
            base_revision_id=self.base.revision_id,
            outline_node_id=None,
            instructions="Rewrite",
            use_rag=False,
            context={},
            source_html=base_html,
            proposed_html=base_html.replace("変更可能な本文。", "改稿案。"),
            edit_summary={"applied_edit_count": 1},
            quality_status="needs_human_review",
        )
        RevisionService(self.connection).commit_revision(
            document_id=self.document_id,
            canonical_html=base_html.replace("変更可能な本文。", "別の人間修正。"),
            editor_type="human",
            operation="human_edit",
        )

        with self.assertRaisesRegex(FukamiruError, "document changed"):
            self.service.confirm(proposal["id"])

    def test_surface_gate_reverts_invalid_edit_before_proposal_is_saved(self) -> None:
        base_html = RevisionService(self.connection).canonical_html(self.base.revision_id)
        run_id = self._run_row()

        proposal = self.service.persist_proposal(
            run_id=run_id,
            document_id=self.document_id,
            project_id=self.project["id"],
            base_revision_id=self.base.revision_id,
            outline_node_id=None,
            instructions="Rewrite",
            use_rag=False,
            context={},
            source_html=base_html,
            proposed_html=base_html.replace("変更可能な本文。", "改稿案。。"),
            edit_summary={"applied_edit_count": 1},
            quality_status="needs_human_review",
        )

        self.assertIn("変更可能な本文。", proposal["proposed_html"])
        report = proposal["validation"]["japanese_surface_quality_gate"]
        self.assertEqual(report["reverted_block_count"], 1)
        self.assertEqual(report["reverted_blocks"][0]["block_id"], "P-RW")

    def test_ready_proposal_requires_a_clean_completion_gate_record(self) -> None:
        base_html = RevisionService(self.connection).canonical_html(self.base.revision_id)

        with self.assertRaisesRegex(FukamiruError, "completion-gate"):
            self.service.persist_proposal(
                run_id=self._run_row(),
                document_id=self.document_id,
                project_id=self.project["id"],
                base_revision_id=self.base.revision_id,
                outline_node_id=None,
                instructions="Rewrite",
                use_rag=False,
                context={},
                source_html=base_html,
                proposed_html=base_html.replace("変更可能な本文。", "改稿案。"),
                edit_summary={"applied_edit_count": 1},
                quality_status="ready",
            )

    def test_persistence_surface_restore_downgrades_ready_proposal(self) -> None:
        base_html = RevisionService(self.connection).canonical_html(self.base.revision_id)
        context = {
            "completion_gate": {
                "passed": True,
                "quality_status": "ready",
                "blocking_count": 0,
                "blocking_reasons": [],
            }
        }

        proposal = self.service.persist_proposal(
            run_id=self._run_row(),
            document_id=self.document_id,
            project_id=self.project["id"],
            base_revision_id=self.base.revision_id,
            outline_node_id=None,
            instructions="Rewrite",
            use_rag=False,
            context=context,
            source_html=base_html,
            proposed_html=base_html.replace("変更可能な本文。", "改稿案。。"),
            edit_summary={"applied_edit_count": 1},
            quality_status="ready",
        )

        self.assertEqual(proposal["quality_status"], "needs_human_review")
        self.assertEqual(proposal["status"], "needs_human_review")
        completion = proposal["context"]["completion_gate"]
        self.assertFalse(completion["passed"])
        self.assertIn(
            "surface.persistence_reverted_blocks",
            {item["code"] for item in completion["blocking_reasons"]},
        )

    def test_frozen_final_candidate_cannot_be_changed_during_persistence(self) -> None:
        base_html = RevisionService(self.connection).canonical_html(self.base.revision_id)
        candidate = base_html.replace("変更可能な本文。", "改稿案。。")

        with self.assertRaisesRegex(FukamiruError, "frozen final candidate"):
            self.service.persist_proposal(
                run_id=self._run_row(),
                document_id=self.document_id,
                project_id=self.project["id"],
                base_revision_id=self.base.revision_id,
                outline_node_id=None,
                instructions="Rewrite",
                use_rag=False,
                context={},
                source_html=base_html,
                proposed_html=candidate,
                edit_summary={"applied_edit_count": 1},
                quality_status="needs_human_review",
                expected_proposed_sha256=hashlib.sha256(candidate.encode()).hexdigest(),
            )

        count = self.connection.execute(
            "SELECT COUNT(*) AS count FROM manuscript_rewrite_proposals"
        ).fetchone()["count"]
        self.assertEqual(count, 0)

    def test_pending_same_run_proposal_can_be_refreshed_by_a_stricter_gate(self) -> None:
        base_html = RevisionService(self.connection).canonical_html(self.base.revision_id)
        proposal = self.service.persist_proposal(
            run_id=self._run_row(),
            document_id=self.document_id,
            project_id=self.project["id"],
            base_revision_id=self.base.revision_id,
            outline_node_id=None,
            instructions="Rewrite",
            use_rag=False,
            context={"gate_version": 1},
            source_html=base_html,
            proposed_html=base_html.replace("変更可能な本文。", "最初の改稿案。"),
            edit_summary={"applied_edit_count": 1},
            quality_status="needs_human_review",
        )

        refreshed = self.service.refresh_pending_proposal(
            proposal["id"],
            context={"gate_version": 2},
            source_html=base_html,
            proposed_html=base_html.replace(
                "変更可能な本文。", "周縁化された諸形式・に注目する。"
            ),
            edit_summary={"applied_edit_count": 2},
            quality_status="needs_human_review",
        )

        self.assertEqual(refreshed["id"], proposal["id"])
        self.assertIn("変更可能な本文。", refreshed["proposed_html"])
        self.assertEqual(refreshed["context"]["gate_version"], 2)
        self.assertEqual(refreshed["edit_summary"]["applied_edit_count"], 2)
        self.assertEqual(
            refreshed["validation"]["japanese_surface_quality_gate"][
                "reverted_block_count"
            ],
            1,
        )

    def test_v22_unresolved_translation_is_saved_but_final_revision_is_blocked(self) -> None:
        base_html = RevisionService(self.connection).canonical_html(self.base.revision_id)
        entry = self._reference_entry(
            resolution_status="metadata_incomplete",
            translator_status="required_missing",
            final_revision_blocked=True,
            conditional_proposal_allowed=True,
            required_missing_fields=["translators"],
        )
        proposed_html, _ = JapaneseReferenceService.render(
            base_html, [entry], workflow_version=22
        )
        context = {
            "workflow_version": 22,
            "reference_entries": [entry],
            "reference_inventory": {"entries": [entry]},
            "reference_quality_gate": JapaneseReferenceService.quality_report(
                proposed_html, [entry], workflow_version=22
            ),
            "completion_gate": {
                "passed": True,
                "quality_status": "ready",
                "blocking_count": 0,
                "blocking_reasons": [],
            },
        }

        proposal = self.service.persist_proposal(
            run_id=self._run_row(),
            document_id=self.document_id,
            project_id=self.project["id"],
            base_revision_id=self.base.revision_id,
            outline_node_id=None,
            instructions="V22 bibliography confirmation",
            use_rag=True,
            context=context,
            source_html=base_html,
            proposed_html=proposed_html,
            edit_summary={"applied_edit_count": 1},
            quality_status="ready",
        )

        self.assertEqual(proposal["lifecycle_status"], "pending")
        self.assertEqual(proposal["quality_status"], "needs_human_review")
        self.assertTrue(proposal["context"]["reference_approval"]["final_revision_blocked"])
        with self.assertRaisesRegex(
            FukamiruError, "Proposalはpending.*メタヒストリー.*訳者が未確認"
        ):
            self.service.confirm(proposal["id"])
        self.assertEqual(
            self.connection.execute(
                "SELECT current_revision_id FROM content_documents WHERE id = ?",
                (self.document_id,),
            ).fetchone()[0],
            self.base.revision_id,
        )
        self.assertEqual(self.service.get(proposal["id"])["lifecycle_status"], "pending")

    def test_v22_policy_id_alone_enables_reference_approval_gate(self) -> None:
        base_html = RevisionService(self.connection).canonical_html(self.base.revision_id)
        proposal = self.service.persist_proposal(
            run_id=self._run_row(),
            document_id=self.document_id,
            project_id=self.project["id"],
            base_revision_id=self.base.revision_id,
            outline_node_id=None,
            instructions="V22 policy context",
            use_rag=False,
            context={
                "manuscript_workflow_policy": {
                    "id": "manuscript-quality-policy"
                }
            },
            source_html=base_html,
            proposed_html=base_html.replace("変更可能な本文。", "改稿案。"),
            edit_summary={"applied_edit_count": 1},
            quality_status="needs_human_review",
        )

        with self.assertRaisesRegex(FukamiruError, "参考文献品質ゲートの記録がありません"):
            self.service.confirm(proposal["id"])

    def test_v22_clean_reference_gate_allows_final_revision(self) -> None:
        base_html = RevisionService(self.connection).canonical_html(self.base.revision_id)
        reference_gate = JapaneseReferenceService.quality_report(
            base_html, [], workflow_version=22
        )
        proposal = self.service.persist_proposal(
            run_id=self._run_row(),
            document_id=self.document_id,
            project_id=self.project["id"],
            base_revision_id=self.base.revision_id,
            outline_node_id=None,
            instructions="V22 clean proposal",
            use_rag=False,
            context={
                "workflow_version": "22",
                "reference_entries": [],
                "reference_quality_gate": reference_gate,
            },
            source_html=base_html,
            proposed_html=base_html.replace("変更可能な本文。", "V22の改稿案。"),
            edit_summary={"applied_edit_count": 1},
            quality_status="needs_human_review",
        )

        confirmed = self.service.confirm(proposal["id"])

        self.assertIsNotNone(confirmed["confirmed_revision_id"])
        self.assertEqual(
            self.connection.execute(
                "SELECT current_revision_id FROM content_documents WHERE id = ?",
                (self.document_id,),
            ).fetchone()[0],
            confirmed["confirmed_revision_id"],
        )

    def test_v21_does_not_apply_v22_reference_approval_gate(self) -> None:
        base_html = RevisionService(self.connection).canonical_html(self.base.revision_id)
        proposal = self.service.persist_proposal(
            run_id=self._run_row(),
            document_id=self.document_id,
            project_id=self.project["id"],
            base_revision_id=self.base.revision_id,
            outline_node_id=None,
            instructions="Legacy proposal",
            use_rag=False,
            context={
                "workflow_version": 21,
                "reference_quality_gate": {"passed": False, "issues": []},
                "reference_entries": [],
            },
            source_html=base_html,
            proposed_html=base_html.replace("変更可能な本文。", "V21の改稿案。"),
            edit_summary={"applied_edit_count": 1},
            quality_status="needs_human_review",
        )

        confirmed = self.service.confirm(proposal["id"])

        self.assertIsNotNone(confirmed["confirmed_revision_id"])

    def test_v22_refresh_rejects_a_regression_to_legacy_reference_format(self) -> None:
        base_html = RevisionService(self.connection).canonical_html(self.base.revision_id)
        entry = self._reference_entry(
            resolution_status="resolved",
            translator_status="not_required",
            final_revision_blocked=False,
        )
        normalized, _ = JapaneseReferenceService.render(
            base_html, [entry], workflow_version=22
        )
        context = {
            "workflow_version": 22,
            "reference_entries": [entry],
            "reference_quality_gate": JapaneseReferenceService.quality_report(
                normalized, [entry], workflow_version=22
            ),
        }
        proposal = self.service.persist_proposal(
            run_id=self._run_row(),
            document_id=self.document_id,
            project_id=self.project["id"],
            base_revision_id=self.base.revision_id,
            outline_node_id=None,
            instructions="V22 normalized bibliography",
            use_rag=False,
            context=context,
            source_html=base_html,
            proposed_html=normalized,
            edit_summary={"applied_edit_count": 1},
            quality_status="needs_human_review",
        )
        legacy_format = normalized.replace("作品社、2017。", "JP、作品社、2017.")

        with self.assertRaisesRegex(
            FukamiruError, "Mandatory bibliography formatting changed"
        ):
            self.service.refresh_pending_proposal(
                proposal["id"],
                context=context,
                source_html=base_html,
                proposed_html=legacy_format,
                edit_summary={"applied_edit_count": 2},
                quality_status="needs_human_review",
            )
        with self.assertRaisesRegex(
            FukamiruError, "Mandatory bibliography formatting changed"
        ):
            self.service.update_proposed(proposal["id"], canonical_html=legacy_format)
        self.assertEqual(self.service.get(proposal["id"])["proposed_html"], normalized)

    def test_v22_machine_detected_section_is_preserved_through_approval(self) -> None:
        base_html = RevisionService(self.connection).canonical_html(self.base.revision_id)
        machine_entries = [
            {
                "provider_record_id": "crossref:related",
                "provider": "crossref",
                "title": "関連研究",
                "authors": ["研究者"],
                "record_type": "article",
                "container_title": "研究紀要",
                "year": "2024",
                "doi": "10.1000/related",
            }
        ]
        normalized, _ = JapaneseReferenceService.render_sections_v22(
            base_html, [], machine_entries=machine_entries
        )
        context = {
            "workflow_version": 22,
            "reference_entries": [],
            "machine_reference_entries": machine_entries,
            "reference_quality_gate": JapaneseReferenceService.quality_report(
                normalized,
                [],
                workflow_version=22,
                machine_entries=machine_entries,
            ),
        }
        proposal = self.service.persist_proposal(
            run_id=self._run_row(),
            document_id=self.document_id,
            project_id=self.project["id"],
            base_revision_id=self.base.revision_id,
            outline_node_id=None,
            instructions="V22 machine-detected references",
            use_rag=True,
            context=context,
            source_html=base_html,
            proposed_html=normalized,
            edit_summary={"applied_edit_count": 1},
            quality_status="needs_human_review",
        )

        self.assertIn("機械判別参考文献", proposal["proposed_html"])
        confirmed = self.service.confirm(proposal["id"])
        self.assertIn("機械判別参考文献", confirmed["proposed_html"])

    def test_v22_machine_detected_section_cannot_be_removed_before_persistence(self) -> None:
        base_html = RevisionService(self.connection).canonical_html(self.base.revision_id)
        machine_entries = [
            {
                "provider_record_id": "crossref:related",
                "provider": "crossref",
                "title": "関連研究",
                "authors": ["研究者"],
                "record_type": "article",
                "container_title": "研究紀要",
                "year": "2024",
            }
        ]
        context = {
            "workflow_version": 22,
            "reference_entries": [],
            "machine_reference_entries": machine_entries,
            "reference_quality_gate": {"passed": True, "issues": []},
        }

        with self.assertRaisesRegex(
            FukamiruError, "Mandatory bibliography formatting changed"
        ):
            self.service.persist_proposal(
                run_id=self._run_row(),
                document_id=self.document_id,
                project_id=self.project["id"],
                base_revision_id=self.base.revision_id,
                outline_node_id=None,
                instructions="V22 machine-detected references",
                use_rag=True,
                context=context,
                source_html=base_html,
                proposed_html=base_html,
                edit_summary={"applied_edit_count": 0},
                quality_status="needs_human_review",
            )

    def test_v26_and_v27_inline_machine_references_are_idempotent_at_persistence(self) -> None:
        base_html = RevisionService(self.connection).canonical_html(self.base.revision_id).replace(
            "</article>",
            '<section id="REF-RW"><h2 id="REF-H-RW">参考文献</h2>'
            '<p id="REF-P-RW">著者『資料』、出版社、2024年。</p></section></article>',
        )
        machine_entries = [
            {
                "provider_record_id": "crossref:inline-related",
                "provider": "crossref",
                "title": "機械判別候補",
                "authors": ["研究者"],
                "record_type": "article",
                "container_title": "研究紀要",
                "year": "2025",
            }
        ]
        inline_html, _ = JapaneseReferenceService.render_sections_v22(
            base_html,
            [],
            machine_entries=machine_entries,
            render_machine_section=False,
        )

        for workflow_version in (26, 27):
            with self.subTest(workflow_version=workflow_version):
                rendered, edits = self.service._reference_render(
                    inline_html,
                    [],
                    workflow_version=workflow_version,
                    machine_entries=machine_entries,
                )

                self.assertEqual(rendered, inline_html)
                self.assertEqual(edits, [])

    def test_v29_unsupported_reference_row_is_reviewable_and_idempotent(self) -> None:
        html = (
            '<article id="D" lang="ja"><section id="R"><h2 id="RH">参考文献</h2>'
            '<p id="RP">書誌事項が未確定の原文行。</p></section></article>'
        )
        entry = {
            "reference_id": "reference:unresolved",
            "source_block_id": "RP",
            "entry_ordinal": 0,
            "parse_status": "parsed",
            "record_type": "unknown",
            "title": "資料",
            "original_text": "書誌事項が未確定の原文行。",
        }
        rendered, _ = JapaneseReferenceService.render_sections_v22(
            html, [entry], workflow_version=29, render_machine_section=False
        )

        self.assertEqual(rendered, html)
        self.assertEqual(
            JapaneseReferenceService.format_issues(
                rendered, [entry], workflow_version=29
            ),
            [],
        )
        report = JapaneseReferenceService.quality_report(
            rendered, [entry], workflow_version=29
        )
        self.assertFalse(report["passed"])
        self.assertIn(
            "reference.house_style_undetermined",
            {item["code"] for item in report["issues"]},
        )

    def test_v22_machine_reference_metadata_confirmation_keeps_it_not_consulted(self) -> None:
        base_html = RevisionService(self.connection).canonical_html(self.base.revision_id)
        machine_entries = [
            {
                "provider_record_id": "ndl:machine-book",
                "provider": "ndl_search",
                "title": "機械判別された翻訳書",
                "authors": ["研究者"],
                "record_type": "book",
                "publisher": "研究出版社",
                "year": "2025",
            }
        ]
        machine = JapaneseReferenceService.bibliography_sections_v22(
            [], machine_entries=machine_entries
        )[1]["entries"][0]
        normalized, _ = JapaneseReferenceService.render_sections_v22(
            base_html, [], machine_entries=machine_entries
        )
        proposal = self.service.persist_proposal(
            run_id=self._run_row(),
            document_id=self.document_id,
            project_id=self.project["id"],
            base_revision_id=self.base.revision_id,
            outline_node_id=None,
            instructions="Confirm machine reference metadata",
            use_rag=True,
            context={
                "workflow_version": 22,
                "reference_entries": [],
                "machine_reference_entries": machine_entries,
                "reference_quality_gate": JapaneseReferenceService.quality_report(
                    normalized,
                    [],
                    workflow_version=22,
                    machine_entries=machine_entries,
                ),
            },
            source_html=base_html,
            proposed_html=normalized,
            edit_summary={"applied_edit_count": 1},
            quality_status="needs_human_review",
        )

        with self.assertRaisesRegex(
            FukamiruError, "cannot be reclassified as self-authored or unpublished"
        ):
            self.service.confirm_reference_metadata_v22(
                proposal["id"],
                machine["machine_reference_id"],
                proposal["proposed_sha256"],
                is_translation=False,
                is_unpublished=True,
            )

        updated = self.service.confirm_reference_metadata_v22(
            proposal["id"],
            machine["machine_reference_id"],
            proposal["proposed_sha256"],
            is_translation=True,
            translators=["訳者甲"],
            metadata={"edition": "改訳版", "volume": "全1巻"},
        )

        confirmed = updated["context"]["machine_reference_entries"][0]
        self.assertEqual(
            confirmed["reference_role"],
            "machine_detected_not_confirmed_consulted",
        )
        self.assertEqual(confirmed["consulted_status"], "not_consulted")
        self.assertEqual(confirmed["translators"], ["訳者甲"])
        self.assertEqual(
            confirmed["provenance"]["translators"]["kind"], "user_confirmed"
        )
        self.assertFalse(updated["context"]["reference_approval"]["final_revision_blocked"])
        self.assertIn("機械判別参考文献", updated["proposed_html"])
        self.assertIn("訳者甲訳", updated["proposed_html"])
        with self.assertRaisesRegex(FukamiruError, "Proposal changed"):
            self.service.confirm_reference_metadata_v22(
                proposal["id"],
                machine["machine_reference_id"],
                proposal["proposed_sha256"],
                is_translation=True,
                translators=["訳者甲"],
            )

    def test_v22_machine_metadata_confirmation_preserves_unresolved_edition_and_volume(
        self,
    ) -> None:
        base_html = RevisionService(self.connection).canonical_html(self.base.revision_id)
        machine_entries = [
            {
                "provider_record_id": "ndl:machine-unresolved-manifestation",
                "provider": "ndl_search",
                "title": "機械判別された翻訳書",
                "authors": ["研究者"],
                "record_type": "book",
                "publisher": "研究出版社",
                "year": "2025",
                "edition_status": "unresolved",
                "volume_status": "unresolved",
                "required_missing_fields": [
                    "translation_status_confirmation",
                    "edition",
                    "volume",
                ],
                "final_revision_blocked": True,
            }
        ]
        machine = JapaneseReferenceService.bibliography_sections_v22(
            [], machine_entries=machine_entries
        )[1]["entries"][0]
        normalized, _ = JapaneseReferenceService.render_sections_v22(
            base_html, [], machine_entries=machine_entries
        )
        proposal = self.service.persist_proposal(
            run_id=self._run_row(),
            document_id=self.document_id,
            project_id=self.project["id"],
            base_revision_id=self.base.revision_id,
            outline_node_id=None,
            instructions="Keep unresolved manifestation metadata",
            use_rag=True,
            context={
                "workflow_version": 22,
                "reference_entries": [],
                "machine_reference_entries": machine_entries,
                "reference_quality_gate": JapaneseReferenceService.quality_report(
                    normalized,
                    [],
                    workflow_version=22,
                    machine_entries=machine_entries,
                ),
            },
            source_html=base_html,
            proposed_html=normalized,
            edit_summary={"applied_edit_count": 1},
            quality_status="needs_human_review",
        )

        updated = self.service.confirm_reference_metadata_v22(
            proposal["id"],
            machine["machine_reference_id"],
            proposal["proposed_sha256"],
            is_translation=True,
            translators=["訳者甲"],
        )

        confirmed = updated["context"]["machine_reference_entries"][0]
        self.assertEqual(set(confirmed["required_missing_fields"]), {"edition", "volume"})
        self.assertTrue(confirmed["final_revision_blocked"])
        self.assertEqual(confirmed["consulted_status"], "not_consulted")
        self.assertTrue(updated["context"]["reference_approval"]["final_revision_blocked"])
        with self.assertRaisesRegex(FukamiruError, "finalRevision|最終Revision"):
            self.service.confirm(proposal["id"])

    def test_v22_year_and_publisher_do_not_resolve_edition_or_volume(self) -> None:
        base_html = RevisionService(self.connection).canonical_html(self.base.revision_id)
        machine_entries = [
            {
                "provider_record_id": "ndl:machine-unspecified-manifestation",
                "provider": "ndl_search",
                "title": "機械判別された原著",
                "authors": ["研究者"],
                "record_type": "book",
                "publisher": "研究出版社",
                "year": "2025",
            }
        ]
        machine = JapaneseReferenceService.bibliography_sections_v22(
            [], machine_entries=machine_entries
        )[1]["entries"][0]
        normalized, _ = JapaneseReferenceService.render_sections_v22(
            base_html, [], machine_entries=machine_entries
        )
        proposal = self.service.persist_proposal(
            run_id=self._run_row(),
            document_id=self.document_id,
            project_id=self.project["id"],
            base_revision_id=self.base.revision_id,
            outline_node_id=None,
            instructions="Preserve manifestation identity without guessing labels",
            use_rag=True,
            context={
                "workflow_version": 22,
                "reference_entries": [],
                "machine_reference_entries": machine_entries,
                "reference_quality_gate": JapaneseReferenceService.quality_report(
                    normalized,
                    [],
                    workflow_version=22,
                    machine_entries=machine_entries,
                ),
            },
            source_html=base_html,
            proposed_html=normalized,
            edit_summary={"applied_edit_count": 1},
            quality_status="needs_human_review",
        )

        updated = self.service.confirm_reference_metadata_v22(
            proposal["id"],
            machine["machine_reference_id"],
            proposal["proposed_sha256"],
            is_translation=False,
        )

        confirmed = updated["context"]["machine_reference_entries"][0]
        self.assertIsNone(confirmed.get("edition"))
        self.assertIsNone(confirmed.get("volume"))
        self.assertEqual(
            confirmed["manifestation_axes"]["edition"],
            "unresolved",
        )
        self.assertEqual(
            confirmed["manifestation_axes"]["volume"],
            "unresolved",
        )
        self.assertEqual(
            confirmed["manifestation_identity_evidence"]["edition"][
                "observed_basis_fields"
            ],
            ["year", "publisher"],
        )
        self.assertEqual(
            confirmed["manifestation_identity_evidence"]["edition"][
                "observed_basis_values"
            ],
            {"year": "2025", "publisher": "研究出版社"},
        )
        self.assertEqual(set(confirmed["required_missing_fields"]), {"edition", "volume"})
        self.assertTrue(confirmed["final_revision_blocked"])
        with self.assertRaisesRegex(FukamiruError, "最終Revision"):
            self.service.confirm(proposal["id"])

    def test_v22_machine_blank_axes_require_identity_or_exhaustive_scope_evidence(
        self,
    ) -> None:
        machine = JapaneseReferenceService.bibliography_sections_v22(
            [],
            machine_entries=[
                {
                    "provider_record_id": "local:machine-insufficient-manifestation",
                    "provider": "local_search",
                    "title": "機械判別された原著",
                    "authors": ["研究者"],
                    "record_type": "book",
                    "is_translation": False,
                }
            ],
        )[1]["entries"][0]

        self.assertEqual(machine["manifestation_axes"]["edition"], "unresolved")
        self.assertEqual(machine["manifestation_axes"]["volume"], "unresolved")
        self.assertEqual(
            set(machine["required_missing_fields"]), {"edition", "volume"}
        )
        self.assertTrue(machine["final_revision_blocked"])
        self.assertNotEqual(
            machine["manifestation_axes"]["edition"],
            "all_known_manifestations_retained",
        )

    def test_v22_machine_metadata_patch_does_not_promote_year_and_publisher_to_identity(
        self,
    ) -> None:
        base_html = RevisionService(self.connection).canonical_html(self.base.revision_id)
        machine_entries = [
            {
                "provider_record_id": "local:machine-patch-identity",
                "provider": "local_search",
                "title": "機械判別された原著",
                "authors": ["研究者"],
                "record_type": "book",
            }
        ]
        machine = JapaneseReferenceService.bibliography_sections_v22(
            [], machine_entries=machine_entries
        )[1]["entries"][0]
        normalized, _ = JapaneseReferenceService.render_sections_v22(
            base_html, [], machine_entries=machine_entries
        )
        proposal = self.service.persist_proposal(
            run_id=self._run_row(),
            document_id=self.document_id,
            project_id=self.project["id"],
            base_revision_id=self.base.revision_id,
            outline_node_id=None,
            instructions="Complete a machine manifestation identity",
            use_rag=True,
            context={
                "workflow_version": 22,
                "reference_entries": [],
                "machine_reference_entries": machine_entries,
                "reference_quality_gate": JapaneseReferenceService.quality_report(
                    normalized,
                    [],
                    workflow_version=22,
                    machine_entries=machine_entries,
                ),
            },
            source_html=base_html,
            proposed_html=normalized,
            edit_summary={"applied_edit_count": 1},
            quality_status="needs_human_review",
        )

        updated = self.service.confirm_reference_metadata_v22(
            proposal["id"],
            machine["machine_reference_id"],
            proposal["proposed_sha256"],
            is_translation=False,
            metadata={"year": "2025", "publisher": "研究出版社"},
        )

        confirmed = updated["context"]["machine_reference_entries"][0]
        self.assertEqual(
            confirmed["manifestation_axes"]["edition"],
            "unresolved",
        )
        self.assertEqual(
            confirmed["manifestation_axes"]["volume"],
            "unresolved",
        )
        self.assertEqual(
            confirmed["manifestation_identity_evidence"]["edition"][
                "observed_basis_values"
            ],
            {"year": "2025", "publisher": "研究出版社"},
        )
        self.assertTrue(confirmed["final_revision_blocked"])

    def test_v22_machine_scoped_identifier_can_resolve_blank_axes(self) -> None:
        scope = {
            axis: {
                "identifier_verified": True,
                "identifier_type": "isbn",
                "identifier_value": "978-4-00-000000-0",
                "identifier_scope": "single_manifestation",
                "verification_source": "ndl_manifestation_record",
            }
            for axis in ("edition", "volume")
        }
        machine = JapaneseReferenceService.bibliography_sections_v22(
            [],
            machine_entries=[
                {
                    "provider_record_id": "ndl:machine-scoped-isbn",
                    "provider": "ndl_search",
                    "title": "機械判別された原著",
                    "authors": ["研究者"],
                    "record_type": "book",
                    "isbn": "9784000000000",
                    "is_translation": False,
                    "manifestation_scope_evidence": scope,
                }
            ],
        )[1]["entries"][0]

        self.assertEqual(
            machine["manifestation_axes"]["edition"],
            "identified_by_manifestation_identifier",
        )
        self.assertEqual(
            machine["manifestation_axes"]["volume"],
            "identified_by_manifestation_identifier",
        )
        self.assertEqual(
            machine["manifestation_identity_evidence"]["volume"][
                "identifier_scope"
            ],
            "single_manifestation",
        )
        self.assertEqual(machine["required_missing_fields"], [])
        self.assertFalse(machine["final_revision_blocked"])

    def test_v22_bare_isbn_does_not_claim_edition_or_volume_scope(self) -> None:
        machine = JapaneseReferenceService.bibliography_sections_v22(
            [],
            machine_entries=[
                {
                    "provider_record_id": "ndl:machine-unscoped-isbn",
                    "provider": "ndl_search",
                    "title": "機械判別された原著",
                    "authors": ["研究者"],
                    "record_type": "book",
                    "isbn": "9784000000000",
                    "is_translation": False,
                }
            ],
        )[1]["entries"][0]

        self.assertEqual(machine["manifestation_axes"]["edition"], "unresolved")
        self.assertEqual(machine["manifestation_axes"]["volume"], "unresolved")
        self.assertEqual(
            machine["manifestation_identity_evidence"]["volume"][
                "observed_basis_fields"
            ],
            ["isbn"],
        )
        self.assertEqual(
            set(machine["required_missing_fields"]), {"edition", "volume"}
        )
        self.assertTrue(machine["final_revision_blocked"])

    def test_v22_machine_all_known_axes_require_durable_complete_set_evidence(
        self,
    ) -> None:
        retained_ids = ["manifestation:first", "manifestation:revised"]
        machine = JapaneseReferenceService.bibliography_sections_v22(
            [],
            machine_entries=[
                {
                    "provider_record_id": "local:machine-complete-manifestations",
                    "provider": "local_search",
                    "title": "機械判別された原著",
                    "authors": ["研究者"],
                    "record_type": "book",
                    "is_translation": False,
                    "manifestation_scope_evidence": {
                        axis: {
                            "complete_set_retained": True,
                            "search_signature": "sha256:manifestation-frontier",
                            "eligible_manifestation_ids": retained_ids,
                        }
                        for axis in ("edition", "volume")
                    },
                }
            ],
        )[1]["entries"][0]

        self.assertEqual(
            machine["manifestation_axes"]["edition"],
            "all_known_manifestations_retained",
        )
        self.assertEqual(
            machine["manifestation_axes"]["volume"],
            "all_known_manifestations_retained",
        )
        self.assertEqual(
            machine["manifestation_identity_evidence"]["edition"][
                "eligible_manifestation_ids"
            ],
            retained_ids,
        )
        self.assertEqual(machine["required_missing_fields"], [])
        self.assertFalse(machine["final_revision_blocked"])

    def test_v22_generic_no_hit_requires_explicit_reclassification_and_recomputes_missing(
        self,
    ) -> None:
        base_html = RevisionService(self.connection).canonical_html(self.base.revision_id)
        entry = self._reference_entry(
            author="研究者",
            title="調査ノート",
            publisher=None,
            year=None,
            record_type="unknown",
            resolution_status="not_found",
            work_identity_status="no_raw_hits",
            edition_status="not_evaluated",
            final_revision_blocked=True,
            conditional_proposal_allowed=True,
            user_action_required={
                "code": "identify_work_or_correct_query",
                "message": "著者・著作を確認してください。",
            },
        )
        normalized, _ = JapaneseReferenceService.render_sections_v22(base_html, [entry])
        proposal = self.service.persist_proposal(
            run_id=self._run_row(),
            document_id=self.document_id,
            project_id=self.project["id"],
            base_revision_id=self.base.revision_id,
            outline_node_id=None,
            instructions="Explicitly reclassify a no-hit reference",
            use_rag=True,
            context={
                "workflow_version": 22,
                "reference_entries": [entry],
                "reference_inventory": {"entries": [entry]},
                "machine_reference_entries": [],
                "reference_quality_gate": JapaneseReferenceService.quality_report(
                    normalized, [entry], workflow_version=22
                ),
            },
            source_html=base_html,
            proposed_html=normalized,
            edit_summary={"applied_edit_count": 0},
            quality_status="needs_human_review",
        )

        item = next(
            item
            for item in proposal["context"]["reference_metadata_confirmation_items"]
            if item["reference_id"] == entry["reference_id"]
        )
        self.assertEqual(item["confirmation_kind"], "unresolved_reference")
        with self.assertRaisesRegex(FukamiruError, "explicit"):
            self.service.confirm_reference_metadata_v22(
                proposal["id"],
                entry["reference_id"],
                proposal["proposed_sha256"],
                is_translation=False,
            )
        with self.assertRaisesRegex(FukamiruError, "explicit"):
            self.service.confirm_reference_metadata_v22(
                proposal["id"],
                entry["reference_id"],
                proposal["proposed_sha256"],
                is_translation=False,
                is_self_authored=False,
                is_unpublished=False,
            )

        updated = self.service.confirm_reference_metadata_v22(
            proposal["id"],
            entry["reference_id"],
            proposal["proposed_sha256"],
            is_translation=False,
            is_unpublished=True,
            metadata={
                "record_type": "article",
                "year": "2026",
                "venue": "研究紀要",
                "volume": "12",
            },
        )

        unresolved = updated["context"]["reference_entries"][0]
        self.assertTrue(unresolved["is_unpublished"])
        self.assertEqual(unresolved["consulted_status"], "user_confirmed")
        self.assertEqual(
            unresolved["user_required_metadata"], ["pages_if_applicable"]
        )
        self.assertEqual(unresolved["required_missing_fields"], ["pages"])
        self.assertTrue(unresolved["final_revision_blocked"])
        self.assertTrue(unresolved["conditional_proposal_allowed"])
        self.assertEqual(
            unresolved["provenance"]["is_unpublished"]["kind"], "user_confirmed"
        )
        classification = unresolved["reference_classification_history"][-1]
        self.assertEqual(classification["changed_fields"], ["is_unpublished"])
        confirmation = updated["context"]["reference_metadata_confirmation_history"][-1]
        self.assertEqual(
            confirmation["classification_change"]["after"]["is_unpublished"], True
        )
        self.assertEqual(updated["context"]["machine_reference_entries"], [])
        self.assertEqual(
            updated["context"]["reference_metadata_confirmation_items"][0][
                "confirmation_kind"
            ],
            "self_or_unpublished",
        )
        with self.assertRaisesRegex(FukamiruError, "最終Revision"):
            self.service.confirm(proposal["id"])

    def test_v22_self_unpublished_metadata_can_be_completed_without_search_identity(self) -> None:
        base_html = RevisionService(self.connection).canonical_html(self.base.revision_id)
        entry = self._reference_entry(
            author="自著",
            title="未刊研究",
            publisher=None,
            year=None,
            record_type="unknown",
            resolution_status="user_input_required",
            work_identity_status="external_identity_unavailable",
            user_required_metadata=[
                "document_type",
                "publication_year",
                "venue_or_publisher",
                "pages_if_applicable",
            ],
            final_revision_blocked=True,
            conditional_proposal_allowed=True,
        )
        normalized, _ = JapaneseReferenceService.render_sections_v22(base_html, [entry])
        proposal = self.service.persist_proposal(
            run_id=self._run_row(),
            document_id=self.document_id,
            project_id=self.project["id"],
            base_revision_id=self.base.revision_id,
            outline_node_id=None,
            instructions="Complete unpublished reference",
            use_rag=False,
            context={
                "workflow_version": 22,
                "reference_entries": [entry],
                "reference_inventory": {"entries": [entry]},
                "machine_reference_entries": [],
                "reference_quality_gate": JapaneseReferenceService.quality_report(
                    normalized, [entry], workflow_version=22
                ),
            },
            source_html=base_html,
            proposed_html=normalized,
            edit_summary={"applied_edit_count": 1},
            quality_status="needs_human_review",
        )

        updated = self.service.confirm_reference_metadata_v22(
            proposal["id"],
            entry["reference_id"],
            proposal["proposed_sha256"],
            is_translation=False,
            metadata={
                "record_type": "unpublished",
                "year": "2026",
                "venue": "博士論文草稿",
            },
        )

        resolved = updated["context"]["reference_entries"][0]
        self.assertEqual(resolved["resolution_status"], "resolved")
        self.assertFalse(resolved["final_revision_blocked"])
        self.assertEqual(resolved["consulted_status"], "user_confirmed")
        self.assertNotIn("user_required_metadata", resolved)
        self.assertEqual(resolved["publisher"], "博士論文草稿")
        self.assertFalse(updated["context"]["reference_approval"]["final_revision_blocked"])
        self.assertNotIn("要ユーザー指定", updated["proposed_html"])

    def test_v22_self_unpublished_metadata_stays_blocked_until_year_and_venue_exist(self) -> None:
        base_html = RevisionService(self.connection).canonical_html(self.base.revision_id)
        entry = self._reference_entry(
            author="自著",
            title="未刊研究",
            publisher=None,
            year=None,
            record_type="unknown",
            resolution_status="user_input_required",
            work_identity_status="external_identity_unavailable",
            user_required_metadata=[
                "document_type",
                "publication_year",
                "venue_or_publisher",
                "pages_if_applicable",
            ],
            final_revision_blocked=True,
            conditional_proposal_allowed=True,
        )
        normalized, _ = JapaneseReferenceService.render_sections_v22(base_html, [entry])
        proposal = self.service.persist_proposal(
            run_id=self._run_row(),
            document_id=self.document_id,
            project_id=self.project["id"],
            base_revision_id=self.base.revision_id,
            outline_node_id=None,
            instructions="Require unpublished metadata",
            use_rag=True,
            context={
                "workflow_version": 22,
                "reference_entries": [entry],
                "reference_quality_gate": JapaneseReferenceService.quality_report(
                    normalized, [entry], workflow_version=22
                ),
            },
            source_html=base_html,
            proposed_html=normalized,
            edit_summary={"applied_edit_count": 1},
            quality_status="needs_human_review",
        )

        updated = self.service.confirm_reference_metadata_v22(
            proposal["id"],
            entry["reference_id"],
            proposal["proposed_sha256"],
            is_translation=False,
            metadata={"record_type": "unpublished"},
        )

        unresolved = updated["context"]["reference_entries"][0]
        self.assertEqual(
            set(unresolved["user_required_metadata"]),
            {"publication_year", "venue_or_publisher"},
        )
        self.assertTrue(unresolved["final_revision_blocked"])
        self.assertIn("要ユーザー指定", updated["proposed_html"])
        self.assertIn("発表年", updated["proposed_html"])
        self.assertIn("掲載先または出版社", updated["proposed_html"])
        with self.assertRaisesRegex(FukamiruError, "最終Revision"):
            self.service.confirm(proposal["id"])

        completed = self.service.confirm_reference_metadata_v22(
            proposal["id"],
            entry["reference_id"],
            updated["proposed_sha256"],
            is_translation=False,
            is_self_authored=True,
            is_unpublished=True,
            metadata={
                "record_type": "unpublished",
                "year": "2026",
                "venue": "博士論文草稿",
            },
        )
        self.assertFalse(
            completed["context"]["reference_entries"][0]["final_revision_blocked"]
        )

    def test_v22_self_authored_article_keeps_applicable_fields_until_supplied(
        self,
    ) -> None:
        entry = self._reference_entry(
            author="自著",
            title="研究論文",
            publisher=None,
            year="2026",
            record_type="article",
            container_title="研究紀要",
            pages=None,
            resolution_status="user_input_required",
            work_identity_status="external_identity_unavailable",
            is_self_authored=True,
            user_required_metadata=[
                "volume_issue_if_applicable",
                "pages_if_applicable",
            ],
            final_revision_blocked=True,
            conditional_proposal_allowed=True,
        )

        self.assertEqual(
            JapaneseReferenceService.self_or_unpublished_required_metadata_v22(entry),
            ["volume_issue_if_applicable", "pages_if_applicable"],
        )
        entry.update(volume="12", pages="1-20")
        self.assertEqual(
            JapaneseReferenceService.self_or_unpublished_required_metadata_v22(entry),
            [],
        )

    def test_v22_single_consulted_translation_metadata_can_be_completed(self) -> None:
        source = self._reference_entry(
            candidate_matches=[
                {
                    "candidate_id": "translation:first",
                    "provider": "ndl_search",
                    "provider_record_id": "ndl:first",
                    "record_type": "book",
                    "title": "翻訳研究書",
                    "authors": ["原著者"],
                    "publisher": "研究出版社",
                    "year": "2025",
                    "edition": "初版",
                    "volume": "上巻",
                    "is_translation": True,
                    "translators": [],
                }
            ]
        )
        selected = JapaneseReferenceService.select_candidate(
            source, "translation:first", workflow_version=22
        )
        proposal = self._persist_v22_reference_proposal(selected)

        # A real worker-produced conditional Proposal already contains this
        # bibliography condition.  Confirmation must clean every projection,
        # rather than only enabling the final-approval button.
        stale_context = dict(proposal["context"])
        bibliography_condition = {
            "reason": "bibliography_confirmation_required",
            "reference_count": 1,
            "reference_ids": [source["reference_id"]],
            "disposition": "conditional_proposal_only",
            "final_revision_approval": "blocked",
        }
        stale_context["completion_status"] = "conditional_success"
        stale_context["conditional_success"] = bibliography_condition
        stale_context["conditional_conditions"] = [bibliography_condition]
        self.connection.execute(
            "UPDATE manuscript_rewrite_proposals SET context_json=? WHERE id=?",
            (
                json.dumps(stale_context, ensure_ascii=False, separators=(",", ":")),
                proposal["id"],
            ),
        )
        self.connection.commit()
        proposal = self.service.get(proposal["id"])

        confirmation = proposal["context"]["reference_metadata_confirmation_items"]
        self.assertEqual(len(confirmation), 1)
        self.assertEqual(confirmation[0]["confirmation_kind"], "consulted_manifestation")
        self.assertEqual(confirmation[0]["reference_id"], source["reference_id"])

        updated = self.service.confirm_reference_metadata_v22(
            proposal["id"],
            source["reference_id"],
            proposal["proposed_sha256"],
            is_translation=True,
            translators=["確認済み訳者"],
        )

        resolved = updated["context"]["reference_entries"][0]
        self.assertEqual(resolved["consulted_status"], "user_confirmed")
        self.assertEqual(resolved["translator_status"], "verified")
        self.assertEqual(resolved["translators"], ["確認済み訳者"])
        self.assertFalse(resolved["final_revision_blocked"])
        self.assertFalse(updated["context"]["reference_approval"]["final_revision_blocked"])
        self.assertTrue(updated["context"]["reference_quality_gate"]["passed"])
        self.assertFalse(
            any(
                item.get("reason") == "bibliography_confirmation_required"
                for item in updated["context"]["conditional_conditions"]
            )
        )
        self.assertNotIn("conditional_success", updated["context"])
        self.assertEqual(updated["context"]["completion_status"], "success")
        self.assertTrue(updated["context"]["completion_gate"]["passed"])
        self.assertEqual(updated["context"]["completion_gate"]["blocking_count"], 0)
        confirmed = self.service.confirm(updated["id"])
        self.assertIn(confirmed["lifecycle_status"], {"confirmed", "checkpoint_failed"})

    def test_v22_consulted_manifestations_are_completed_independently(self) -> None:
        candidates = [
            {
                "candidate_id": f"translation:{label}",
                "provider": "ndl_search",
                "provider_record_id": f"ndl:{label}",
                "record_type": "book",
                "title": "複数版翻訳書",
                "authors": ["原著者"],
                "publisher": "研究出版社",
                "year": year,
                "edition": edition,
                "volume": volume,
                "is_translation": True,
                "translators": [],
            }
            for label, year, edition, volume in (
                ("first", "2020", "初版", "上巻"),
                ("revised", "2024", "改訂版", "下巻"),
            )
        ]
        source = self._reference_entry(candidate_matches=candidates)
        selected = JapaneseReferenceService.confirm_manifestations_v22(
            source, ["translation:first", "translation:revised"]
        )
        proposal = self._persist_v22_reference_proposal(selected)

        items = proposal["context"]["reference_metadata_confirmation_items"]
        self.assertEqual(len(items), 2)
        self.assertTrue(
            all(item["confirmation_kind"] == "consulted_manifestation" for item in items)
        )
        first, second = items
        first_identity = {
            key: first.get(key)
            for key in ("reference_id", "source_reference_id", "candidate_id")
        }
        second_identity = {
            key: second.get(key)
            for key in ("reference_id", "source_reference_id", "candidate_id")
        }

        after_first = self.service.confirm_reference_metadata_v22(
            proposal["id"],
            first["reference_id"],
            proposal["proposed_sha256"],
            is_translation=True,
            translators=["第一版訳者"],
        )
        parent = after_first["context"]["reference_entries"][0]
        children = parent["expanded_reference_entries"]
        self.assertEqual(
            {
                key: children[0].get(key)
                for key in ("reference_id", "source_reference_id", "candidate_id")
            },
            first_identity,
        )
        self.assertEqual(
            {
                key: children[1].get(key)
                for key in ("reference_id", "source_reference_id", "candidate_id")
            },
            second_identity,
        )
        self.assertEqual(children[0]["translators"], ["第一版訳者"])
        self.assertEqual(children[1]["translators"], [])
        self.assertFalse(children[0]["final_revision_blocked"])
        self.assertTrue(children[1]["final_revision_blocked"])
        self.assertTrue(parent["final_revision_blocked"])
        self.assertEqual(
            [
                item["reference_id"]
                for item in after_first["context"][
                    "reference_metadata_confirmation_items"
                ]
            ],
            [second["reference_id"]],
        )

        after_second = self.service.confirm_reference_metadata_v22(
            proposal["id"],
            second["reference_id"],
            after_first["proposed_sha256"],
            is_translation=True,
            translators=["改訂版訳者"],
        )
        parent = after_second["context"]["reference_entries"][0]
        children = parent["expanded_reference_entries"]
        self.assertEqual(children[0]["translators"], ["第一版訳者"])
        self.assertEqual(children[1]["translators"], ["改訂版訳者"])
        self.assertTrue(all(not item["final_revision_blocked"] for item in children))
        self.assertFalse(parent["final_revision_blocked"])
        self.assertEqual(parent["required_missing_fields"], [])
        self.assertEqual(parent["resolution_status"], "resolved")
        self.assertEqual(after_second["context"]["reference_metadata_confirmation_items"], [])
        self.assertFalse(
            after_second["context"]["reference_approval"]["final_revision_blocked"]
        )

        with self.assertRaisesRegex(FukamiruError, "Proposal changed"):
            self.service.confirm_reference_metadata_v22(
                proposal["id"],
                second["reference_id"],
                proposal["proposed_sha256"],
                is_translation=True,
                translators=["改訂版訳者"],
            )

    def test_apply_edits_skips_noop_and_surface_invalid_replacement(self) -> None:
        base_html = RevisionService(self.connection).canonical_html(self.base.revision_id)
        proposed, applied, warnings = self.service.apply_edits(
            base_html,
            [
                {
                    "operation": "replace_text",
                    "block_id": "P-RW",
                    "text": "変更可能な本文。",
                },
                {
                    "operation": "insert_paragraph_after",
                    "block_id": "P-RW",
                    "text": "不正な追加。。",
                },
            ],
            run_id="run-surface-gate",
            batch_index=0,
        )

        self.assertEqual(applied, [])
        self.assertEqual(proposed, base_html)
        self.assertTrue(any("no-op" in warning for warning in warnings))
        self.assertTrue(any("surface quality gate" in warning for warning in warnings))

    @staticmethod
    def _reference_entry(**overrides: object) -> dict[str, object]:
        entry: dict[str, object] = {
            "reference_id": "reference:test",
            "source_block_id": "P-RW",
            "entry_ordinal": 0,
            "parse_status": "parsed",
            "resolution_status": "resolved",
            "record_type": "book",
            "author": "ヘイドン・ホワイト",
            "title": "メタヒストリー",
            "publisher": "作品社",
            "year": "2017",
            "publication_place": "JP",
            "translators": [],
            "provenance": {
                field: {"kind": "source_explicit", "label": "原稿に明記"}
                for field in ("author", "title", "publisher", "year")
            },
        }
        entry.update(overrides)
        return entry

    def _persist_v22_reference_proposal(
        self, entry: dict[str, object]
    ) -> dict[str, object]:
        base_html = RevisionService(self.connection).canonical_html(self.base.revision_id)
        normalized, _ = JapaneseReferenceService.render_sections_v22(base_html, [entry])
        return self.service.persist_proposal(
            run_id=self._run_row(),
            document_id=self.document_id,
            project_id=self.project["id"],
            base_revision_id=self.base.revision_id,
            outline_node_id=None,
            instructions="Complete exact consulted manifestation metadata",
            use_rag=True,
            context={
                "workflow_version": 22,
                "reference_entries": [entry],
                "machine_reference_entries": [],
                "reference_quality_gate": JapaneseReferenceService.quality_report(
                    normalized, [entry], workflow_version=22
                ),
            },
            source_html=base_html,
            proposed_html=normalized,
            edit_summary={"applied_edit_count": 1},
            quality_status="needs_human_review",
        )

    def _run_row(self) -> str:
        run_id = "rewrite-run-fixture"
        self.connection.execute(
            """
            INSERT INTO runs(
                id, project_id, workflow_name, workflow_version,
                status, input_json, created_at
            ) VALUES (?, ?, 'manuscript_rewrite', '1', 'completed', '{}', 'now')
            """,
            (run_id, self.project["id"]),
        )
        self.connection.commit()
        return run_id


if __name__ == "__main__":
    unittest.main()
