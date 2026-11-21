from __future__ import annotations

import gc
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from fukamiru.authoring.evidence_contracts import SectionOutput, build_fixed_assets
from fukamiru.authoring.models import (
    ArgumentEdge,
    ArgumentNode,
    AuditFinding,
    AuthoringPlan,
    ClaimNode,
    DiscourseNode,
    Proposition,
    ResearchQuestion,
    SectionContract,
    SectionDraft,
)
from fukamiru.authoring.repository import AuthoringRepository
from fukamiru.authoring.service import build_drafts, build_plan
from fukamiru.authoring.structure_aware import ArchitectureV31
from fukamiru.storage.database import SQLiteDatabase
from fukamiru.storage.runs import RunRepository


class AuthoringRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(__file__).resolve().parents[1]
        self.database = SQLiteDatabase(
            Path(self.temp.name) / "state.db", root / "migrations" / "state"
        )
        self.database.migrate()
        with closing(self.database.connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO projects(id, name, root_path, created_at, updated_at)
                VALUES ('p', 'P', '/p', 'now', 'now')
                """
            )
            connection.execute(
                """
                INSERT INTO content_documents(id, project_id, kind, title, created_at, updated_at)
                VALUES ('d', 'p', 'note', 'Note', 'now', 'now')
                """
            )
            connection.execute(
                """
                INSERT INTO revisions(
                    id, document_id, editor_type, operation, content_sha256, created_at
                ) VALUES ('source', 'd', 'human', 'create', 'sha', 'now')
                """
            )
        self.repository = AuthoringRepository(self.database)

    def tearDown(self) -> None:
        gc.collect()
        self.temp.cleanup()

    def make_plan(self, revision: int = 1, parent_revision: int | None = None) -> AuthoringPlan:
        propositions = (
            Proposition(
                id=f"prop-{revision}",
                text="Observation",
                proposition_type="observation",
                provenance="memo",
                argument_role="research_lead",
            ),
        )
        claims = (
            ClaimNode(
                id=f"main-{revision}", level="main", text="Main claim", provenance="llm_inference"
                , research_gap="Find a contrary case", rival_interpretation="Reverse causality"
            ),
            ClaimNode(
                id=f"sub-{revision}",
                parent_id=f"main-{revision}",
                level="sub",
                text="Subclaim",
                provenance="rag",
            ),
        )
        nodes = (
            ArgumentNode(
                id=f"claim-node-{revision}",
                node_type="claim",
                text="Main claim",
                claim_tree_node_id=f"main-{revision}",
            ),
            ArgumentNode(
                id=f"ev-{revision}",
                node_type="evidence",
                text="Observed evidence",
                source_span_id="span-1",
                provenance="rag",
            ),
        )
        discourse = (
            DiscourseNode(
                id=f"root-{revision}",
                ordinal=0,
                node_kind="root",
                article_role="root",
                rhetorical_relation="purpose",
                discourse_purpose="Coordinate the article-level argument.",
                claim_ids=(f"main-{revision}",),
            ),
            DiscourseNode(
                id=f"section-{revision}",
                parent_id=f"root-{revision}",
                ordinal=0,
                node_kind="section",
                article_role="body",
                rhetorical_relation="evidence",
                discourse_purpose="Turn the claim and evidence into a reader-facing phase.",
                section_answer="The evidence supports a bounded conclusion.",
                claim_ids=(f"main-{revision}",),
                section_id=f"contract-{revision}",
            ),
        )
        contract = SectionContract(
            id=f"contract-{revision}",
            discourse_node_id=f"section-{revision}",
            title="Evidence",
            local_question="What supports the claim?",
            expected_conclusion="The evidence supports it.",
            section_answer="The evidence supports a bounded conclusion.",
            claim_ids=(f"main-{revision}",),
            evidence_ids=(f"ev-{revision}",),
        )
        return AuthoringPlan(
            id="plan",
            document_id="d",
            source_revision_id="source",
            revision=revision,
            parent_revision=parent_revision,
            research_question=ResearchQuestion(
                text="Why?",
                provisional_answer="The mechanism is plausible.",
                research_gaps=("Find a contrary case",),
                rival_interpretations=("Reverse causality",),
            ),
            propositions=propositions,
            claims=claims,
            argument_nodes=nodes,
            argument_edges=(
                ArgumentEdge(
                    id=f"edge-{revision}",
                    source_node_id=f"ev-{revision}",
                    target_node_id=f"claim-node-{revision}",
                    relation_type="supports",
                    provenance="rag",
                ),
            ),
            discourse_nodes=discourse,
            section_contracts=(contract,),
        )

    def test_plan_round_trips_all_structures_and_provenance(self) -> None:
        plan = self.make_plan()
        self.repository.create_plan(plan)
        loaded = self.repository.load_plan("plan")
        self.assertEqual(plan, loaded)
        self.assertEqual("rag", loaded.argument_nodes[1].provenance)
        self.assertEqual("evidence", loaded.discourse_nodes[1].rhetorical_relation)

    def test_v31_architecture_json_round_trips_without_changing_legacy_plan(self) -> None:
        base = self.make_plan()
        architecture = ArchitectureV31.model_validate({
            "research_problem": {
                "research_question": "Why?", "prior_positions": [],
                "problematisation": {
                    "type": "source_internal", "description": "The memo poses a puzzle",
                    "evidence_ids": [], "proposition_ids": ["prop-1"],
                },
                "provisional_answer": "The mechanism is plausible.",
                "contribution_target": "Explain the mechanism",
            },
            "claim_scopes": [{
                "claim_id": "main-1", "proposition": "Bounded claim",
                "scope_conditions": [], "epistemic_status": "supported_interpretation",
                "evidence_basis_ids": ["ev-1"],
            }],
            "defense_obligations": [],
            "introduction_contract": {
                "section_id": "intro", "genre_profile": "article", "moves": [],
                "research_question": "Why?", "documented_prior_positions": [],
                "problematisation": "The memo poses a puzzle",
                "provisional_answer": "The mechanism is plausible.",
                "contribution_target": "Explain the mechanism",
                "roadmap_section_ids": ["contract-1"],
            },
            "body_section_contracts": [{
                "section_id": "contract-1", "section_task": "Examine evidence",
                "section_output": "A bounded result", "thesis_link": "Answers why",
                "required_input_section_ids": [], "defense_obligation_ids": [],
            }],
            "conclusion_contract": {
                "section_id": "conclusion", "research_question": "Why?",
                "established_section_ids": ["contract-1"],
                "synthesis_goal": "Integrate", "contribution_target": "Explain the mechanism",
                "scope": "bounded", "unresolved_questions": [],
            },
            "sections": [{"section_id": "contract-1", "article_role": "body"}],
        })
        v31 = base.model_copy(update={"architecture_v31": architecture})
        self.repository.create_plan(v31)
        loaded = self.repository.load_plan("plan")
        self.assertEqual(v31, loaded)
        self.assertEqual("bounded", loaded.architecture_v31.conclusion_contract.scope)
        with closing(self.database.connect()) as connection:
            row = connection.execute(
                "SELECT schema_version, architecture_json FROM authoring_plan_revisions "
                "WHERE plan_id='plan' AND revision=1"
            ).fetchone()
            self.assertEqual(3, row["schema_version"])
            self.assertIn("research_problem", row["architecture_json"])

    def test_v31_evidence_contracts_and_fixed_asset_hash_round_trip(self) -> None:
        base = self.make_plan()
        contract = base.section_contracts[0].model_copy(update={
            "minimum_evidence_items": 0,
            "minimum_concrete_examples": 0,
            "section_output_contract": SectionOutput(
                proposition="The evidence supports a bounded conclusion.",
                output_type="interpretive_result",
                required_evidence_level="traceable",
            ),
        })
        plan = base.model_copy(update={
            "schema_version": 4,
            "fixed_asset_hash": "a" * 64,
            "section_contracts": (contract,),
        })

        self.repository.create_plan(plan)
        loaded = self.repository.load_plan("plan")

        self.assertEqual(loaded.schema_version, 4)
        self.assertEqual(loaded.fixed_asset_hash, "a" * 64)
        self.assertEqual(loaded.section_contracts[0].minimum_evidence_items, 0)
        self.assertEqual(loaded.section_contracts[0].minimum_concrete_examples, 0)
        self.assertEqual(loaded.section_contracts[0].section_output_contract, (
            contract.section_output_contract
        ))
        drafts = build_drafts(
            loaded,
            [{"section_id": "contract-1", "text": "根拠を論じる。", "evidence_ids": []}],
            attempt=self.repository.next_draft_attempt(["contract-1"]),
        )
        self.repository.save_draft(drafts[0])
        self.assertEqual(self.repository.list_drafts("contract-1"), drafts)
        with closing(self.database.connect()) as connection, connection:
            connection.execute(
                "INSERT INTO discourse_nodes(id, plan_id, plan_revision, parent_id, ordinal, "
                "node_kind, article_role, rhetorical_relation, created_at) "
                "VALUES ('another-section', 'plan', 1, 'root-1', 1, 'section', "
                "'body', 'evidence', 'now')"
            )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "minimum_evidence_items >= 0"):
                connection.execute(
                    "INSERT INTO section_contracts(id, plan_id, plan_revision, "
                    "discourse_node_id, title, local_question, expected_conclusion, "
                    "minimum_evidence_items, minimum_concrete_examples, created_at) "
                    "VALUES ('invalid', 'plan', 1, 'another-section', 'X', 'Q', "
                    "'C', -1, 0, 'now')"
                )

    def test_zero_minima_migration_preserves_legacy_contracts_and_drafts(self) -> None:
        root = Path(__file__).resolve().parents[1]
        old_migrations = Path(self.temp.name) / "old_migrations"
        old_migrations.mkdir()
        for migration in (root / "migrations" / "state").glob("[0-9][0-9][0-9][0-9]_*.sql"):
            if migration.name < "0049_":
                shutil.copy2(migration, old_migrations / migration.name)
        old_db_path = Path(self.temp.name) / "legacy.db"
        old_database = SQLiteDatabase(old_db_path, old_migrations)
        old_database.migrate()
        with closing(old_database.connect()) as connection, connection:
            connection.execute(
                "INSERT INTO projects(id, name, root_path, created_at, updated_at) "
                "VALUES ('p', 'P', '/p', 'now', 'now')"
            )
            connection.execute(
                "INSERT INTO content_documents(id, project_id, kind, title, "
                "created_at, updated_at) "
                "VALUES ('d', 'p', 'note', 'Note', 'now', 'now')"
            )
            connection.execute(
                "INSERT INTO revisions(id, document_id, editor_type, operation, "
                "content_sha256, created_at) "
                "VALUES ('source', 'd', 'human', 'create', 'sha', 'now')"
            )
        legacy_contract = self.make_plan().section_contracts[0].model_copy(update={
            "minimum_evidence_items": 4,
            "minimum_concrete_examples": 2,
        })
        legacy_plan = self.make_plan().model_copy(update={
            "section_contracts": (legacy_contract,),
        })
        AuthoringRepository(old_database).create_plan(legacy_plan)
        with closing(old_database.connect()) as connection, connection:
            connection.execute(
                "INSERT INTO section_drafts(id, contract_id, body, content_sha256, created_at) "
                "VALUES ('draft-1', 'contract-1', 'Text', 'sha', 'now')"
            )

        migrated_database = SQLiteDatabase(old_db_path, root / "migrations" / "state")
        self.assertEqual(
            [
                "0049_pdf_ocr_multilingual",
                "0049_v31_section_contract_zero_minima",
                "0050_pdf_ocr_recovery_inputs",
                "0051_corpus_sync_skips",
                "0052_research_source_handoffs",
                "0053_review_feedback",
                "0054_typed_manuscript_research_sources",
                "0055_manuscript_proofreading_generations",
                "0056_knowledge_search_reports",
                "0057_hf_proofreading_jobs",
            ],
            migrated_database.migrate(),
        )
        loaded = AuthoringRepository(migrated_database).load_plan("plan")
        self.assertEqual(loaded.section_contracts[0].minimum_evidence_items, 4)
        self.assertEqual(loaded.section_contracts[0].minimum_concrete_examples, 2)
        with closing(migrated_database.connect()) as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(
                connection.execute("SELECT contract_id FROM section_drafts WHERE id='draft-1'")
                .fetchone()[0],
                "contract-1",
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE section_contracts SET minimum_evidence_items=0 "
                    "WHERE id='contract-1'"
                )

    def test_replaying_exact_revision_is_idempotent(self) -> None:
        plan = self.make_plan()
        self.repository.create_plan(plan)
        self.repository.create_plan(plan)
        self.assertEqual(plan, self.repository.load_plan("plan"))

    def test_build_plan_scopes_local_ids_and_references_by_plan(self) -> None:
        def context(plan_id: str) -> dict[str, object]:
            return {
                "plan_id": plan_id,
                "document_id": "d",
                "source_revision_id": "source",
                "research_question": {"text": "Why?"},
                "propositions": [{"id": "p1", "text": "Observation"}],
                "classifications": [{"proposition_id": "p1", "type": "observation"}],
                "claims": [
                    {
                        "id": "c1",
                        "level": "main_claim",
                        "text": "Claim",
                        "proposition_id": "p1",
                    }
                ],
                "argument_nodes": [
                    {
                        "id": "e1",
                        "node_type": "evidence",
                        "text": "Evidence",
                        "proposition_id": "p1",
                    }
                ],
                "argument_edges": [],
                "hypotheses": [{"id": "h1", "text": "Hypothesis", "gap_type": "causal"}],
                "discourse_nodes": [
                    {
                        "id": "root",
                        "parent_id": None,
                        "article_role": "root",
                        "discourse_purpose": "Coordinate the article argument.",
                        "claim_ids": ["c1"],
                    },
                    {
                        "id": "s1-node",
                        "parent_id": "root",
                        "section_id": "s1",
                        "article_role": "body",
                        "relation": "evidence",
                        "discourse_purpose": "Explain the evidence as an argument phase.",
                        "claim_ids": ["c1"],
                    },
                ],
                "section_contracts": [
                    {
                        "section_id": "s1",
                        "title": "Evidence",
                        "local_question": "Why?",
                        "expected_conclusion": "Supported",
                        "claim_ids": ["c1"],
                        "evidence_ids": ["e1"],
                    }
                ],
            }

        first = build_plan(context("plan-a"))
        replay = build_plan(context("plan-a"))
        other = build_plan(context("plan-b"))
        self.assertEqual(first, replay)
        self.assertNotEqual(first.propositions[0].id, other.propositions[0].id)
        self.assertEqual("plan-a@1:c1", first.claims[0].id)
        self.assertEqual("plan-a@1:p1", first.claims[0].proposition_id)
        self.assertEqual(("plan-a@1:c1",), first.section_contracts[0].claim_ids)
        self.assertEqual(("plan-a@1:e1",), first.section_contracts[0].evidence_ids)
        self.assertEqual("plan-a@1:s1", first.section_contracts[0].id)
        next_revision = build_plan({**context("plan-a"), "plan_revision": 2})
        self.assertNotEqual(first.propositions[0].id, next_revision.propositions[0].id)
        self.assertEqual("plan-a@2:s1", next_revision.section_contracts[0].id)

    def test_proposition_id_collision_from_another_plan_is_rejected(self) -> None:
        first = self.make_plan()
        self.repository.create_plan(first)
        colliding = first.model_copy(
            update={"id": "other-plan", "revision": 1, "parent_revision": None}
        )
        with self.assertRaisesRegex(ValueError, "already owned by another plan revision"):
            self.repository.create_plan(colliding)
        with closing(self.database.connect()) as connection:
            plans = connection.execute(
                "SELECT COUNT(*) AS n FROM authoring_plans WHERE id = 'other-plan'"
            ).fetchone()["n"]
        self.assertEqual(0, plans)

    def test_existing_revision_with_different_content_is_rejected(self) -> None:
        original = self.make_plan()
        self.repository.create_plan(original)
        changed = original.model_copy(
            update={"research_question": ResearchQuestion(text="A different question?")}
        )
        with self.assertRaisesRegex(ValueError, "already exists with different content"):
            self.repository.create_plan(changed)

    def test_new_revision_preserves_old_and_becomes_current(self) -> None:
        self.repository.create_plan(self.make_plan())
        revised = self.make_plan(2, 1)
        self.repository.create_plan(revised)
        self.assertEqual(revised, self.repository.load_plan("plan"))
        self.assertEqual(1, self.repository.load_plan("plan", 1).revision)
        with (
            closing(self.database.connect()) as connection,
            connection,
            self.assertRaises(sqlite3.IntegrityError),
        ):
            connection.execute(
                """
                UPDATE authoring_plan_revisions SET research_question = 'mutated'
                WHERE plan_id = 'plan' AND revision = 1
                """
            )

    def test_invalid_claim_or_discourse_tree_rejected_before_persistence(self) -> None:
        with self.assertRaises(ValueError):
            AuthoringPlan(
                id="bad",
                document_id="d",
                source_revision_id="source",
                revision=1,
                research_question=ResearchQuestion(text="Question"),
                claims=(
                    ClaimNode(id="main", level="main", parent_id="sub", text="M"),
                    ClaimNode(id="sub", level="sub", parent_id="main", text="S"),
                ),
            )

    def test_drafts_and_findings_are_queryable(self) -> None:
        self.repository.create_plan(self.make_plan())
        draft = SectionDraft(
            id="draft",
            contract_id="contract-1",
            body="Draft",
            content_sha256="hash",
            used_claim_ids=("main-1",),
            used_evidence_ids=("ev-1",),
        )
        self.repository.save_draft(draft)
        self.assertEqual([draft], self.repository.list_drafts("contract-1"))
        self.assertEqual(
            self.repository.next_draft_attempt(["contract-1", "contract-2"]),
            2,
        )
        finding = AuditFinding(
            id="finding",
            defect_type="missing_warrant",
            severity="major",
            message="Warrant needed",
            object_type="claim",
            object_id="main-1",
        )
        self.repository.add_audit_finding("plan", 1, finding)
        self.assertEqual([finding], self.repository.list_audit_findings("plan", 1, status="open"))

    def test_replanned_draft_uses_next_attempt_for_same_contract(self) -> None:
        self.repository.create_plan(self.make_plan())
        first = SectionDraft(
            id="first-draft", contract_id="contract-1", attempt=1,
            body="First wording", content_sha256="first-hash",
        )
        self.repository.save_draft(first)
        second = SectionDraft(
            id="replanned-draft", contract_id="contract-1",
            attempt=self.repository.next_draft_attempt(["contract-1"]),
            body="Replanned wording", content_sha256="second-hash",
        )
        self.repository.save_draft(second)
        self.assertEqual(
            [(draft.attempt, draft.body) for draft in self.repository.list_drafts("contract-1")],
            [(1, "First wording"), (2, "Replanned wording")],
        )

    def test_evidence_checkpoint_round_trips_and_fixed_assets_are_immutable(self) -> None:
        with closing(self.database.connect()) as connection, connection:
            run = RunRepository(connection).create(
                workflow_name="authoring",
                workflow_version="31",
                entry_node="prepare_note",
                project_id="p",
                inputs={},
            )
        fixed = build_fixed_assets({
            "evidence_context": [{
                "id": "SRC-1",
                "text": "Source text",
                "verification_status": "verified",
            }],
        }).model_dump(mode="json")
        evidence_uses = [{
            "evidence_use_id": "EU-1",
            "section_id": "S1",
            "claim_id": "C1",
            "sentence_id": "S1-P01-S01",
            "source_ids": ["SRC-1"],
            "claim_role": "source_fact",
            "evidence_kind": "bibliographic_record",
            "verification_status": "verified",
            "support_locator": {},
            "supported_content": "Source text",
            "author_inference": None,
        }]
        review_items = [{
            "review_item_id": "HR-1",
            "section_id": "S1",
            "sentence_id": "S1-P01-S01",
            "claim_id": "C1",
            "category": "source_attribution",
            "reason": "Check the original source",
            "source_ids": ["SRC-1"],
            "recommended_check": "Compare the page and wording",
            "blocks_render": False,
            "status": "open",
        }]

        self.repository.save_evidence_state(
            run_id=run.run_id,
            fixed_assets=fixed,
            evidence_uses=evidence_uses,
            review_items=review_items,
        )

        self.assertEqual(
            self.repository.load_evidence_state(run.run_id),
            {
                "fixed_assets": fixed,
                "evidence_uses": evidence_uses,
                "human_review_items": review_items,
            },
        )
        with self.assertRaisesRegex(ValueError, "immutable"):
            self.repository.save_evidence_state(
                run_id=run.run_id,
                fixed_assets={**fixed, "asset_hash": "b" * 64},
                evidence_uses=evidence_uses,
                review_items=review_items,
            )


if __name__ == "__main__":
    unittest.main()
