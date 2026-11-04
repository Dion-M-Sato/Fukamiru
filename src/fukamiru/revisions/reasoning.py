"""Durable records for reasoned, evidence-aware manuscript revision."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import replace
from typing import Any

from fukamiru.local_llm.proofreading import (
    InvariantIssue,
    ProofreadingEdit,
    ProofreadingResult,
)
from fukamiru.revisions.japanese_polish import PolishTarget, WordingCandidate
from fukamiru.storage.runs import utc_now


class ManuscriptReasoningRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def save_review(
        self,
        *,
        run_id: str,
        node_run_id: str,
        document_id: str,
        source_proposal_id: str | None,
        human_review: str,
        assessment: list[dict[str, Any]],
        research_queries: list[str],
    ) -> dict[str, Any]:
        review_id = str(uuid.uuid4())
        now = utc_now()
        self.connection.execute(
            """
            INSERT INTO manuscript_reasoning_reviews(
                id, run_id, node_run_id, document_id, source_proposal_id,
                human_review, assessment_json, research_queries_json,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                node_run_id = excluded.node_run_id,
                source_proposal_id = excluded.source_proposal_id,
                human_review = excluded.human_review,
                assessment_json = excluded.assessment_json,
                research_queries_json = excluded.research_queries_json,
                status = 'completed',
                updated_at = excluded.updated_at
            """,
            (
                review_id,
                run_id,
                node_run_id,
                document_id,
                source_proposal_id,
                human_review,
                _json(assessment),
                _json(research_queries),
                now,
                now,
            ),
        )
        self.connection.commit()
        return self.review_for_run(run_id)

    def review_for_run(self, run_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM manuscript_reasoning_reviews WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise LookupError(f"Manuscript reasoning review not found for Run: {run_id}")
        result = dict(row)
        result["assessment"] = json.loads(result.pop("assessment_json"))
        result["research_queries"] = json.loads(result.pop("research_queries_json"))
        return result

    def save_evidence(self, record: dict[str, Any]) -> str:
        stable_key = "|".join(
            str(record.get(key) or "")
            for key in ("run_id", "query", "source_type", "provider", "provider_record_id")
        )
        evidence_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"fukamiru:{stable_key}"))
        self.connection.execute(
            """
            INSERT INTO manuscript_research_evidence(
                id, run_id, review_id, finding_key, query, source_type,
                provider, provider_record_id, title, authors_json, source_url,
                locator, excerpt, confidence, provenance_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, query, source_type, provider, provider_record_id)
            DO UPDATE SET
                finding_key = excluded.finding_key,
                title = excluded.title,
                authors_json = excluded.authors_json,
                source_url = excluded.source_url,
                locator = excluded.locator,
                excerpt = excluded.excerpt,
                confidence = excluded.confidence,
                provenance_json = excluded.provenance_json
            """,
            (
                evidence_id,
                record["run_id"],
                record["review_id"],
                record.get("finding_key"),
                record["query"],
                record["source_type"],
                record["provider"],
                record["provider_record_id"],
                record.get("title"),
                _json(record.get("authors") or []),
                record.get("source_url"),
                record.get("locator"),
                record.get("excerpt") or "",
                float(record.get("confidence") or 0.0),
                _json(record.get("provenance") or {}),
                utc_now(),
            ),
        )
        self.connection.commit()
        return evidence_id

    def evidence_for_run(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT * FROM manuscript_research_evidence
            WHERE run_id = ? ORDER BY query, source_type, provider, id
            """,
            (run_id,),
        ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["authors"] = json.loads(item.pop("authors_json"))
            item["provenance"] = json.loads(item.pop("provenance_json"))
            results.append(item)
        return results

    def save_structure_plan(
        self,
        *,
        run_id: str,
        review_id: str,
        document_id: str,
        plan: dict[str, Any],
        validation: dict[str, Any],
    ) -> dict[str, Any]:
        plan_id = str(uuid.uuid4())
        now = utc_now()
        status = "valid" if validation.get("valid") else "invalid"
        self.connection.execute(
            """
            INSERT INTO manuscript_structure_plans(
                id, run_id, review_id, document_id, plan_json,
                validation_json, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                review_id = excluded.review_id,
                document_id = excluded.document_id,
                plan_json = excluded.plan_json,
                validation_json = excluded.validation_json,
                status = excluded.status,
                updated_at = excluded.updated_at
            """,
            (
                plan_id,
                run_id,
                review_id,
                document_id,
                _json(plan),
                _json(validation),
                status,
                now,
                now,
            ),
        )
        self.connection.commit()
        return self.structure_plan_for_run(run_id)

    def structure_plan_for_run(self, run_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM manuscript_structure_plans WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise LookupError(f"Manuscript structure plan not found for Run: {run_id}")
        result = dict(row)
        result["plan"] = json.loads(result.pop("plan_json"))
        result["validation"] = json.loads(result.pop("validation_json"))
        return result

    def save_argument_checkpoint(
        self,
        *,
        run_id: str,
        node_run_id: str,
        process_name: str,
        target_signature: str,
        output: dict[str, Any],
        validation: dict[str, Any],
        target_count: int,
        completed_count: int,
        reused_count: int,
        unresolved_count: int,
    ) -> dict[str, Any]:
        """Persist one independently reusable argument-planning process result."""

        checkpoint_id = str(uuid.uuid4())
        now = utc_now()
        status = "completed" if not validation.get("issues") else "invalid"
        self.connection.execute(
            """
            INSERT INTO manuscript_argument_checkpoints(
                id, run_id, node_run_id, process_name, target_signature,
                output_json, validation_json, target_count, completed_count,
                reused_count, unresolved_count, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, process_name, target_signature) DO UPDATE SET
                node_run_id = excluded.node_run_id,
                output_json = excluded.output_json,
                validation_json = excluded.validation_json,
                target_count = excluded.target_count,
                completed_count = excluded.completed_count,
                reused_count = excluded.reused_count,
                unresolved_count = excluded.unresolved_count,
                status = excluded.status,
                updated_at = excluded.updated_at
            """,
            (
                checkpoint_id,
                run_id,
                node_run_id,
                process_name,
                target_signature,
                _json(output),
                _json(validation),
                max(0, int(target_count)),
                max(0, int(completed_count)),
                max(0, int(reused_count)),
                max(0, int(unresolved_count)),
                status,
                now,
                now,
            ),
        )
        self.connection.commit()
        return self.argument_checkpoint(
            run_id=run_id,
            process_name=process_name,
            target_signature=target_signature,
        )

    def argument_checkpoint(
        self,
        *,
        run_id: str,
        process_name: str,
        target_signature: str,
    ) -> dict[str, Any]:
        row = self.connection.execute(
            """
            SELECT * FROM manuscript_argument_checkpoints
            WHERE run_id = ? AND process_name = ? AND target_signature = ?
            """,
            (run_id, process_name, target_signature),
        ).fetchone()
        if row is None:
            raise LookupError("Manuscript argument checkpoint not found")
        result = dict(row)
        result["output"] = json.loads(result.pop("output_json"))
        result["validation"] = json.loads(result.pop("validation_json"))
        return result

    def save_coherence_check(
        self,
        *,
        run_id: str,
        node_run_id: str,
        stage: str,
        pass_index: int,
        report: dict[str, Any],
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO manuscript_coherence_checks(
                id, run_id, node_run_id, stage, pass_index,
                report_json, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, stage, pass_index) DO UPDATE SET
                node_run_id = excluded.node_run_id,
                report_json = excluded.report_json,
                status = excluded.status,
                created_at = excluded.created_at
            """,
            (
                str(uuid.uuid4()),
                run_id,
                node_run_id,
                stage,
                pass_index,
                _json(report),
                "passed" if report.get("passed") else "failed",
                utc_now(),
            ),
        )
        self.connection.commit()

    def save_coherence_checkpoint(
        self,
        *,
        run_id: str,
        node_run_id: str,
        stage: str,
        round_index: int,
        context_signature: str,
        lineage_signature: str,
        evidence_signature: str,
        source_sha256: str,
        evidence_round_index: int,
        candidate_html: str,
        report: dict[str, Any],
        reports: list[dict[str, Any]],
        repair_history: list[dict[str, Any]],
        applied_edits: list[dict[str, Any]],
        warnings: list[str],
        restored_block_ids: list[str],
        resolution_routes: list[dict[str, Any]],
        status: str,
    ) -> dict[str, Any]:
        """Persist one coherence repair round so a Node retry cannot lose it."""

        checkpoint_id = str(uuid.uuid4())
        now = utc_now()
        candidate_sha256 = hashlib.sha256(candidate_html.encode()).hexdigest()
        self.connection.execute(
            """
            INSERT INTO manuscript_coherence_checkpoints(
                id, run_id, node_run_id, stage, round_index,
                context_signature, lineage_signature, evidence_signature,
                source_sha256, evidence_round_index,
                candidate_sha256, candidate_html,
                report_json, reports_json, repair_history_json,
                applied_edits_json, warnings_json, restored_block_ids_json,
                resolution_routes_json, status, created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            ON CONFLICT(node_run_id, round_index) DO UPDATE SET
                context_signature = excluded.context_signature,
                lineage_signature = excluded.lineage_signature,
                evidence_signature = excluded.evidence_signature,
                source_sha256 = excluded.source_sha256,
                evidence_round_index = excluded.evidence_round_index,
                candidate_sha256 = excluded.candidate_sha256,
                candidate_html = excluded.candidate_html,
                report_json = excluded.report_json,
                reports_json = excluded.reports_json,
                repair_history_json = excluded.repair_history_json,
                applied_edits_json = excluded.applied_edits_json,
                warnings_json = excluded.warnings_json,
                restored_block_ids_json = excluded.restored_block_ids_json,
                resolution_routes_json = excluded.resolution_routes_json,
                status = excluded.status,
                updated_at = excluded.updated_at
            """,
            (
                checkpoint_id,
                run_id,
                node_run_id,
                stage,
                round_index,
                context_signature,
                lineage_signature,
                evidence_signature,
                source_sha256,
                evidence_round_index,
                candidate_sha256,
                candidate_html,
                _json(report),
                _json(reports),
                _json(repair_history),
                _json(applied_edits),
                _json(warnings),
                _json(restored_block_ids),
                _json(resolution_routes),
                status,
                now,
                now,
            ),
        )
        self.connection.commit()
        checkpoint = self.load_coherence_checkpoint(
            run_id=run_id,
            stage=stage,
            lineage_signature=lineage_signature,
            context_signature=context_signature,
            source_sha256=source_sha256,
        )
        if checkpoint is None:  # pragma: no cover - the upsert above is authoritative
            raise RuntimeError("Manuscript coherence checkpoint was not persisted")
        return checkpoint

    def load_coherence_checkpoint(
        self,
        *,
        run_id: str,
        stage: str,
        lineage_signature: str,
        context_signature: str,
        source_sha256: str,
    ) -> dict[str, Any] | None:
        rows = self.connection.execute(
            """
            SELECT * FROM manuscript_coherence_checkpoints
            WHERE run_id = ? AND stage = ?
              AND (
                  lineage_signature = ?
                  OR context_signature = ?
                  OR lineage_signature IS NULL
              )
            ORDER BY
                CASE
                    WHEN lineage_signature = ? THEN 0
                    WHEN context_signature = ? THEN 1
                    ELSE 2
                END,
                round_index DESC,
                updated_at DESC
            """,
            (
                run_id,
                stage,
                lineage_signature,
                context_signature,
                lineage_signature,
                context_signature,
            ),
        ).fetchall()
        for row in rows:
            result = self._decode_coherence_checkpoint(row)
            if str(result.get("lineage_signature") or "") == lineage_signature:
                result["checkpoint_match"] = "lineage"
                return result
            if str(result["context_signature"]) == context_signature:
                result["checkpoint_match"] = "context"
                return result
            if result.get("lineage_signature") is not None:
                continue
            lineage_inputs = {
                str(result["candidate_sha256"]),
                *{
                    str(item.get("input_sha256") or "")
                    for item in result.get("repair_history") or []
                    if isinstance(item, dict)
                },
            }
            if source_sha256 in lineage_inputs:
                result["checkpoint_match"] = "legacy_lineage"
                return result
        return None

    def bind_coherence_checkpoint_lineage(
        self,
        *,
        checkpoint_id: str,
        lineage_signature: str,
        evidence_signature: str,
        source_sha256: str,
    ) -> None:
        """Upgrade a v2 checkpoint after its source ancestry has been verified."""

        self.connection.execute(
            """
            UPDATE manuscript_coherence_checkpoints
            SET lineage_signature = COALESCE(lineage_signature, ?),
                evidence_signature = COALESCE(evidence_signature, ?),
                source_sha256 = COALESCE(source_sha256, ?),
                updated_at = ?
            WHERE id = ?
            """,
            (
                lineage_signature,
                evidence_signature,
                source_sha256,
                utc_now(),
                checkpoint_id,
            ),
        )
        self.connection.commit()

    @staticmethod
    def _decode_coherence_checkpoint(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        candidate_html = str(result["candidate_html"])
        expected_sha256 = str(result["candidate_sha256"])
        actual_sha256 = hashlib.sha256(candidate_html.encode()).hexdigest()
        if actual_sha256 != expected_sha256:
            raise RuntimeError("Manuscript coherence checkpoint failed SHA-256 validation")
        for field in (
            "report_json",
            "reports_json",
            "repair_history_json",
            "applied_edits_json",
            "warnings_json",
            "restored_block_ids_json",
            "resolution_routes_json",
        ):
            result[field.removesuffix("_json")] = json.loads(result.pop(field))
        return result

    def load_polish_checkpoint(
        self,
        *,
        run_id: str,
        block_id: str,
        chunk_ordinal: int,
        request_signature: str,
        source_text: str,
    ) -> ProofreadingResult | None:
        row = self.connection.execute(
            """
            SELECT * FROM manuscript_polish_checkpoints
            WHERE run_id = ? AND block_id = ? AND chunk_ordinal = ?
              AND request_signature = ?
            """,
            (run_id, block_id, chunk_ordinal, request_signature),
        ).fetchone()
        if row is None:
            return None
        if str(row["source_sha256"]) != hashlib.sha256(source_text.encode()).hexdigest():
            return None
        return ProofreadingResult(
            source_text=str(row["source_text"]),
            edited_text=str(row["edited_text"]),
            edits=tuple(ProofreadingEdit(**item) for item in json.loads(row["edits_json"])),
            reasons=tuple(str(item) for item in json.loads(row["reasons_json"])),
            issues=tuple(InvariantIssue(**item) for item in json.loads(row["issues_json"])),
            status=str(row["status"]),
            confidence=float(row["confidence"]),
            diff=str(row["diff"]),
        )

    def save_polish_checkpoint(
        self,
        result: ProofreadingResult,
        *,
        run_id: str,
        node_run_id: str,
        block_id: str,
        chunk_ordinal: int,
        request_signature: str,
        model_identity: str,
    ) -> None:
        now = utc_now()
        checkpoint_id = str(uuid.uuid4())
        self.connection.execute(
            """
            INSERT INTO manuscript_polish_checkpoints(
                id, run_id, node_run_id, block_id, chunk_ordinal,
                request_signature, model_identity, source_sha256, source_text,
                edited_text, edits_json, reasons_json, issues_json, status,
                confidence, diff, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, block_id, chunk_ordinal, request_signature)
            DO UPDATE SET
                node_run_id = excluded.node_run_id,
                model_identity = excluded.model_identity,
                source_sha256 = excluded.source_sha256,
                source_text = excluded.source_text,
                edited_text = excluded.edited_text,
                edits_json = excluded.edits_json,
                reasons_json = excluded.reasons_json,
                issues_json = excluded.issues_json,
                status = excluded.status,
                confidence = excluded.confidence,
                diff = excluded.diff,
                updated_at = excluded.updated_at
            """,
            (
                checkpoint_id,
                run_id,
                node_run_id,
                block_id,
                chunk_ordinal,
                request_signature,
                model_identity,
                hashlib.sha256(result.source_text.encode()).hexdigest(),
                result.source_text,
                result.edited_text,
                _json([item.__dict__ for item in result.edits]),
                _json(list(result.reasons)),
                _json([item.__dict__ for item in result.issues]),
                result.status,
                result.confidence,
                result.diff,
                now,
                now,
            ),
        )
        self.connection.commit()

    def replace_polish_targets(
        self,
        targets: tuple[PolishTarget, ...],
        *,
        run_id: str,
        node_run_id: str,
        document_id: str,
        project_id: str,
    ) -> None:
        """Atomically replace the application-owned target plan for one Run."""

        if self.polish_targets_for_run(run_id) == targets:
            return  # Preserve candidates and human choices on an identical replay.
        now = utc_now()
        self.connection.execute("DELETE FROM manuscript_polish_targets WHERE run_id = ?", (run_id,))
        self.connection.executemany(
            """
            INSERT INTO manuscript_polish_targets(
                id, run_id, node_run_id, document_id, project_id, section_id,
                block_id, target_ordinal, start_offset, end_offset,
                source_sha256, source_text, context_before, context_after,
                issue_codes_json, relation_type, semantic_contract_json,
                request_signature, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      'detected', ?, ?)
            """,
            [
                (
                    target.id,
                    run_id,
                    node_run_id,
                    document_id,
                    project_id,
                    target.section_id,
                    target.block_id,
                    target.ordinal,
                    target.start_offset,
                    target.end_offset,
                    hashlib.sha256(target.source_text.encode()).hexdigest(),
                    target.source_text,
                    target.context_before,
                    target.context_after,
                    _json(list(target.issue_codes)),
                    target.relation_type,
                    _json(
                        {
                            "allowed_relation_types": list(target.allowed_relation_types),
                            "semantic_roles": target.semantic_roles,
                            "named_entities": list(target.named_entities),
                            "terminology": list(target.terminology),
                        }
                    ),
                    target.request_signature,
                    now,
                    now,
                )
                for target in targets
            ],
        )
        self.connection.commit()

    def polish_targets_for_run(self, run_id: str) -> tuple[PolishTarget, ...]:
        rows = self.connection.execute(
            """
            SELECT * FROM manuscript_polish_targets
            WHERE run_id = ?
            ORDER BY target_ordinal, start_offset
            """,
            (run_id,),
        ).fetchall()
        targets: list[PolishTarget] = []
        for row in rows:
            contract = json.loads(str(row["semantic_contract_json"]))
            targets.append(
                PolishTarget(
                    id=str(row["id"]),
                    section_id=str(row["section_id"]),
                    block_id=str(row["block_id"]),
                    ordinal=int(row["target_ordinal"]),
                    start_offset=int(row["start_offset"]),
                    end_offset=int(row["end_offset"]),
                    source_text=str(row["source_text"]),
                    context_before=str(row["context_before"]),
                    context_after=str(row["context_after"]),
                    issue_codes=tuple(json.loads(str(row["issue_codes_json"]))),
                    relation_type=str(row["relation_type"]),
                    allowed_relation_types=tuple(
                        str(item) for item in contract.get("allowed_relation_types") or []
                    ),
                    semantic_roles={
                        str(key): str(value) if value is not None else None
                        for key, value in dict(contract.get("semantic_roles") or {}).items()
                    },
                    named_entities=tuple(
                        str(item) for item in contract.get("named_entities") or []
                    ),
                    terminology=tuple(str(item) for item in contract.get("terminology") or []),
                    request_signature=str(row["request_signature"]),
                )
            )
        return tuple(targets)

    def save_polish_candidates(
        self,
        candidates: tuple[WordingCandidate, ...],
        *,
        run_id: str,
        target_id: str,
        model_identity: str,
        expected_candidate_count: int,
        issues: tuple[str, ...],
    ) -> None:
        # Ordinals are snapshot positions, never identities assigned by the model.
        # Reject cross-target data before deleting the last durable checkpoint.
        if any(candidate.target_id != target_id for candidate in candidates):
            raise ValueError("Candidate target does not match checkpoint target")
        identities = {}
        unique = {}
        for candidate in sorted(candidates, key=lambda c: c.source_kind != "original"):
            if candidate.id in identities and identities[candidate.id] != candidate.text:
                raise ValueError("Candidate identity changed its text")
            identities[candidate.id] = candidate.text
            unique.setdefault(candidate.text, candidate)
        candidates = tuple(
            replace(candidate, ordinal=index)
            for index, candidate in enumerate(
                sorted(unique.values(), key=lambda c: c.source_kind != "original")
            )
        )
        self.connection.execute("SAVEPOINT polish_candidate_checkpoint")
        try:
            now = utc_now()
            self.connection.execute(
                "DELETE FROM manuscript_polish_candidates WHERE target_id = ?",
                (target_id,),
            )
            self.connection.executemany(
                """
                INSERT INTO manuscript_polish_candidates(
                    id, target_id, candidate_ordinal, candidate_text, source_kind,
                    relation_type, rationale, model_confidence, hard_vetoed,
                    veto_reasons_json, component_scores_json, total_score, rank,
                    selected, selection_mode, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        candidate.id,
                        target_id,
                        candidate.ordinal,
                        candidate.text,
                        candidate.source_kind,
                        candidate.relation_type,
                        candidate.rationale,
                        candidate.confidence,
                        int(candidate.hard_vetoed),
                        _json(list(candidate.hard_veto_reasons)),
                        _json(candidate.component_scores or {}),
                        candidate.total_score,
                        candidate.rank,
                        int(candidate.selected),
                        candidate.selection_mode,
                        now,
                        now,
                    )
                    for candidate in candidates
                ],
            )
            self.connection.execute(
                """
                INSERT INTO manuscript_polish_generation_audits(
                    target_id, run_id, model_identity, expected_candidate_count,
                    generated_candidate_count, issues_json, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(target_id) DO UPDATE SET
                    model_identity = excluded.model_identity,
                    expected_candidate_count = excluded.expected_candidate_count,
                    generated_candidate_count = excluded.generated_candidate_count,
                    issues_json = excluded.issues_json,
                    completed_at = excluded.completed_at
                """,
                (
                    target_id,
                    run_id,
                    model_identity,
                    expected_candidate_count,
                    len(candidates),
                    _json(list(issues)),
                    now,
                ),
            )
            self.connection.execute(
                """
                UPDATE manuscript_polish_targets
                SET status = ?, updated_at = ? WHERE id = ?
                """,
                (
                    "generated" if len(candidates) == expected_candidate_count else "detected",
                    now,
                    target_id,
                ),
            )
            self.connection.execute("RELEASE SAVEPOINT polish_candidate_checkpoint")
        except Exception:
            self.connection.execute("ROLLBACK TO SAVEPOINT polish_candidate_checkpoint")
            self.connection.execute("RELEASE SAVEPOINT polish_candidate_checkpoint")
            raise
        self.connection.commit()

    def reserve_generation_attempt(
        self,
        run_id: str,
        target_id: str,
        signature: str,
        limit: int,
    ) -> int | None:
        self.connection.execute("BEGIN IMMEDIATE")
        used = int(
            self.connection.execute(
                "SELECT count(*) FROM manuscript_generation_attempts "
                "WHERE run_id=? AND target_id=? AND signature=?",
                (run_id, target_id, signature),
            ).fetchone()[0]
        )
        if used >= limit:
            self.connection.commit()
            return None
        self.connection.execute(
            "INSERT INTO manuscript_generation_attempts "
            "(run_id,target_id,signature,attempt,started_at) VALUES (?,?,?,?,?)",
            (run_id, target_id, signature, used + 1, utc_now()),
        )
        self.connection.commit()
        return used + 1

    def finish_generation_attempt(
        self,
        run_id: str,
        target_id: str,
        signature: str,
        attempt: int,
        status: str,
        diagnostic: str = "",
    ) -> None:
        self.connection.execute(
            "UPDATE manuscript_generation_attempts SET status=?,diagnostic=?,completed_at=? "
            "WHERE run_id=? AND target_id=? AND signature=? AND attempt=?",
            (status, diagnostic[:2000], utc_now(), run_id, target_id, signature, attempt),
        )
        self.connection.commit()

    def polish_candidates_for_run(self, run_id: str) -> dict[str, tuple[WordingCandidate, ...]]:
        rows = self.connection.execute(
            """
            SELECT c.* FROM manuscript_polish_candidates c
            JOIN manuscript_polish_targets t ON t.id = c.target_id
            WHERE t.run_id = ?
            ORDER BY t.target_ordinal, t.start_offset,
                     c.candidate_ordinal
            """,
            (run_id,),
        ).fetchall()
        result: dict[str, list[WordingCandidate]] = {}
        for row in rows:
            candidate = self._candidate_from_row(row)
            result.setdefault(candidate.target_id, []).append(candidate)
        return {key: tuple(value) for key, value in result.items()}

    def update_scored_candidates(
        self,
        target_id: str,
        candidates: tuple[WordingCandidate, ...],
    ) -> None:
        now = utc_now()
        self.connection.executemany(
            """
            UPDATE manuscript_polish_candidates
            SET hard_vetoed = ?, veto_reasons_json = ?,
                component_scores_json = ?, total_score = ?, rank = ?,
                selected = ?, selection_mode = ?, updated_at = ?
            WHERE id = ? AND target_id = ?
            """,
            [
                (
                    int(candidate.hard_vetoed),
                    _json(list(candidate.hard_veto_reasons)),
                    _json(candidate.component_scores or {}),
                    candidate.total_score,
                    candidate.rank,
                    int(candidate.selected),
                    candidate.selection_mode,
                    now,
                    candidate.id,
                    target_id,
                )
                for candidate in candidates
            ],
        )
        selected = next((candidate for candidate in candidates if candidate.selected), None)
        self.connection.execute(
            """
            UPDATE manuscript_polish_targets
            SET status = ?, selected_candidate_id = ?, selection_mode = ?,
                selection_margin = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                "selected" if selected else "scored",
                selected.id if selected else None,
                selected.selection_mode if selected else None,
                ((selected.component_scores or {}).get("selection_margin") if selected else None),
                now,
                target_id,
            ),
        )
        self.connection.commit()

    def generation_audits_for_run(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT * FROM manuscript_polish_generation_audits
            WHERE run_id = ? ORDER BY completed_at, target_id
            """,
            (run_id,),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            record["issues"] = json.loads(record.pop("issues_json"))
            result.append(record)
        return result

    def preference_records(
        self, scopes: tuple[tuple[str, str], ...], *, limit: int = 500
    ) -> list[dict[str, Any]]:
        if not scopes:
            return []
        clauses = " OR ".join("(scope_type = ? AND scope_key = ?)" for _ in scopes)
        params: list[Any] = [value for scope in scopes for value in scope]
        rows = self.connection.execute(
            f"""
            SELECT * FROM manuscript_polish_preferences
            WHERE {clauses}
            ORDER BY created_at DESC LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            record["rejected_texts"] = json.loads(record.pop("rejected_texts_json"))
            record["features"] = json.loads(record.pop("features_json"))
            result.append(record)
        return result

    def preference_count(self, scopes: tuple[tuple[str, str], ...]) -> int:
        if not scopes:
            return 0
        clauses = " OR ".join("(scope_type = ? AND scope_key = ?)" for _ in scopes)
        params = [value for scope in scopes for value in scope]
        row = self.connection.execute(
            "SELECT COUNT(DISTINCT COALESCE(target_id, id)) AS count "
            f"FROM manuscript_polish_preferences WHERE {clauses}",
            params,
        ).fetchone()
        return int(row["count"] if row is not None else 0)

    def record_polish_preference(
        self,
        *,
        target_id: str,
        candidate_id: str,
        scope_type: str,
        scope_key: str,
        commit: bool = True,
    ) -> dict[str, Any]:
        if scope_type not in {"document", "research", "author"}:
            raise ValueError("Unknown manuscript polish preference scope")
        target = self.connection.execute(
            "SELECT * FROM manuscript_polish_targets WHERE id = ?", (target_id,)
        ).fetchone()
        candidate = self.connection.execute(
            """
            SELECT * FROM manuscript_polish_candidates
            WHERE id = ? AND target_id = ?
            """,
            (candidate_id, target_id),
        ).fetchone()
        if target is None or candidate is None:
            raise LookupError("Manuscript wording candidate not found")
        if int(candidate["hard_vetoed"]):
            raise ValueError("A hard-vetoed wording candidate cannot be selected")
        alternatives = self.connection.execute(
            """
            SELECT candidate_text FROM manuscript_polish_candidates
            WHERE target_id = ? AND id != ? AND hard_vetoed = 0
            ORDER BY rank, candidate_ordinal
            """,
            (target_id, candidate_id),
        ).fetchall()
        now = utc_now()
        preference_id = str(uuid.uuid4())
        self.connection.execute(
            """
            INSERT INTO manuscript_polish_preferences(
                id, scope_type, scope_key, target_id, relation_type,
                source_text, selected_text, rejected_texts_json,
                features_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                preference_id,
                scope_type,
                scope_key,
                target_id,
                str(candidate["relation_type"]),
                str(target["source_text"]),
                str(candidate["candidate_text"]),
                _json([str(row["candidate_text"]) for row in alternatives]),
                str(candidate["component_scores_json"]),
                now,
            ),
        )
        self.connection.execute(
            """
            UPDATE manuscript_polish_candidates
            SET selected = CASE WHEN id = ? THEN 1 ELSE 0 END,
                selection_mode = CASE WHEN id = ? THEN 'human' ELSE NULL END,
                updated_at = ? WHERE target_id = ?
            """,
            (candidate_id, candidate_id, now, target_id),
        )
        self.connection.execute(
            """
            UPDATE manuscript_polish_targets
            SET status = 'selected', selected_candidate_id = ?,
                selection_mode = 'human', updated_at = ? WHERE id = ?
            """,
            (candidate_id, now, target_id),
        )
        if commit:
            self.connection.commit()
        return {
            "preference_id": preference_id,
            "target_id": target_id,
            "candidate_id": candidate_id,
            "scope_type": scope_type,
            "scope_key": scope_key,
            "selected_text": str(candidate["candidate_text"]),
        }

    def polish_target_review(
        self, target_id: str
    ) -> tuple[dict[str, Any], tuple[WordingCandidate, ...]]:
        row = self.connection.execute(
            "SELECT * FROM manuscript_polish_targets WHERE id = ?", (target_id,)
        ).fetchone()
        if row is None:
            raise LookupError("Manuscript polish target not found")
        target = dict(row)
        target["issue_codes"] = json.loads(target.pop("issue_codes_json"))
        target["semantic_contract"] = json.loads(target.pop("semantic_contract_json"))
        candidates = tuple(
            self._candidate_from_row(candidate)
            for candidate in self.connection.execute(
                """
                SELECT * FROM manuscript_polish_candidates
                WHERE target_id = ? ORDER BY rank, candidate_ordinal
                """,
                (target_id,),
            ).fetchall()
        )
        return target, candidates

    def candidate_reviews_for_run(self, run_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT * FROM manuscript_polish_targets
            WHERE run_id = ? ORDER BY target_ordinal, start_offset LIMIT ?
            """,
            (run_id, limit),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            target, candidates = self.polish_target_review(str(row["id"]))
            ranked = sorted(
                (candidate for candidate in candidates if not candidate.hard_vetoed),
                key=lambda item: (
                    item.rank if item.rank is not None else 10_000,
                    item.ordinal,
                ),
            )
            visible = ranked[:3]
            original = next(
                (candidate for candidate in ranked if candidate.source_kind == "original"),
                None,
            )
            if original is not None and all(candidate.id != original.id for candidate in visible):
                visible = [*visible[:2], original]
            result.append(
                {
                    **target,
                    "candidates": [item.as_dict() for item in visible],
                    "vetoed_candidate_count": sum(
                        candidate.hard_vetoed for candidate in candidates
                    ),
                    "review_required": str(target.get("selection_mode") or "")
                    in {"human_training_original", "ambiguous_original"},
                }
            )
        return result

    @staticmethod
    def _candidate_from_row(row: sqlite3.Row) -> WordingCandidate:
        return WordingCandidate(
            id=str(row["id"]),
            target_id=str(row["target_id"]),
            ordinal=int(row["candidate_ordinal"]),
            text=str(row["candidate_text"]),
            relation_type=str(row["relation_type"]),
            rationale=str(row["rationale"]),
            confidence=float(row["model_confidence"]),
            source_kind=str(row["source_kind"]),
            hard_veto_reasons=tuple(
                str(item) for item in json.loads(str(row["veto_reasons_json"]))
            ),
            component_scores={
                str(key): float(value)
                for key, value in json.loads(str(row["component_scores_json"])).items()
            },
            total_score=(float(row["total_score"]) if row["total_score"] is not None else None),
            rank=int(row["rank"]) if row["rank"] is not None else None,
            selected=bool(row["selected"]),
            selection_mode=(
                str(row["selection_mode"]) if row["selection_mode"] is not None else None
            ),
        )


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
