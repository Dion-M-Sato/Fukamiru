"""Deterministic evaluation against canonical state and knowledge provenance."""

from __future__ import annotations

import html
import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from fukamiru.defects import DefectRepository
from fukamiru.documents.canonical_html import CanonicalHTMLValidator
from fukamiru.errors import FukamiruError
from fukamiru.revisions.service import ProtectedBlockError, RevisionService
from fukamiru.storage.runs import utc_now

EVALUATOR_VERSION = "deterministic-v1"


class RepairNeedsHumanReview(FukamiruError):
    """A deterministic repair cannot identify one unambiguous safe edit."""


@dataclass(frozen=True)
class EvaluationFinding:
    defect_type: str
    severity: str
    excerpt: str
    context: str
    rationale: str
    page_start: int | None = None
    page_end: int | None = None
    repair_candidates: tuple[str, ...] = ()
    confidence: float = 1.0
    automatic_repair_allowed: bool = False


class EvaluationService:
    def __init__(
        self,
        state_connection: sqlite3.Connection,
        knowledge_connection: sqlite3.Connection,
    ) -> None:
        self.state = state_connection
        self.knowledge = knowledge_connection

    def evaluate(
        self,
        *,
        run_id: str,
        revision_id: str,
        max_repair_attempts: int = 2,
    ) -> dict[str, Any]:
        if not 0 <= max_repair_attempts <= 10:
            raise FukamiruError("max_repair_attempts must be between 0 and 10")
        run = self.state.execute("SELECT id FROM runs WHERE id = ?", (run_id,)).fetchone()
        revision = self.state.execute(
            """
            SELECT r.id, r.document_id, d.canonical_html
            FROM revisions r
            JOIN revision_documents d ON d.revision_id = r.id
            WHERE r.id = ?
            """,
            (revision_id,),
        ).fetchone()
        if run is None:
            raise FukamiruError(f"Run not found: {run_id}")
        if revision is None:
            raise FukamiruError(f"Revision not found: {revision_id}")
        evaluation_id = str(uuid.uuid4())
        started = utc_now()
        self.state.execute(
            """
            INSERT INTO evaluation_runs(
                id, run_id, revision_id, evaluator_version, status,
                max_repair_attempts, started_at
            ) VALUES (?, ?, ?, ?, 'running', ?, ?)
            """,
            (
                evaluation_id,
                run_id,
                revision_id,
                EVALUATOR_VERSION,
                max_repair_attempts,
                started,
            ),
        )
        self.state.commit()
        try:
            findings = self._findings(revision_id, str(revision["canonical_html"]))
            repository = DefectRepository(self.state)
            defect_ids: list[str] = []
            counts: dict[str, int] = {}
            for evaluator_name, finding, provenance in findings:
                defect_id = repository.record(
                    run_id=run_id,
                    finding=finding,
                    source_asset_id=str(provenance.get("source_asset_id") or ""),
                    source_span_id=provenance.get("source_span_id"),
                    object_type=str(provenance.get("object_type") or "revision"),
                    object_id=str(provenance.get("object_id") or revision_id),
                    details={"evaluation_run_id": evaluation_id, "evaluator": evaluator_name},
                )
                self.state.execute(
                    """
                    INSERT INTO evaluation_defects(
                        evaluation_run_id, defect_id, evaluator_name
                    ) VALUES (?, ?, ?)
                    """,
                    (evaluation_id, defect_id, evaluator_name),
                )
                defect_ids.append(defect_id)
                counts[finding.severity] = counts.get(finding.severity, 0) + 1
            summary = {
                "finding_count": len(defect_ids),
                "severity_counts": counts,
                "defect_ids": defect_ids,
            }
            completed = utc_now()
            self.state.execute(
                """
                UPDATE evaluation_runs
                SET status = 'completed', summary_json = ?, completed_at = ?
                WHERE id = ?
                """,
                (json.dumps(summary, ensure_ascii=False), completed, evaluation_id),
            )
            self.state.commit()
            return self.get(evaluation_id)
        except Exception as exc:
            self.state.execute(
                """
                UPDATE evaluation_runs
                SET status = 'failed', summary_json = ?, completed_at = ? WHERE id = ?
                """,
                (json.dumps({"error": str(exc)}, ensure_ascii=False), utc_now(), evaluation_id),
            )
            self.state.commit()
            raise

    def get(self, evaluation_id: str) -> dict[str, Any]:
        row = self.state.execute(
            "SELECT * FROM evaluation_runs WHERE id = ?", (evaluation_id,)
        ).fetchone()
        if row is None:
            raise FukamiruError(f"Evaluation not found: {evaluation_id}")
        result = dict(row)
        result["summary"] = json.loads(result.pop("summary_json"))
        defect_ids = [
            str(item["defect_id"])
            for item in self.state.execute(
                """
                SELECT e.defect_id FROM evaluation_defects e
                JOIN defects d ON d.id = e.defect_id
                WHERE e.evaluation_run_id = ? ORDER BY d.created_at, d.id
                """,
                (evaluation_id,),
            )
        ]
        repository = DefectRepository(self.state)
        result["defects"] = [repository.get(defect_id) for defect_id in defect_ids]
        attempts = self.state.execute(
            """
            SELECT * FROM repair_attempts
            WHERE evaluation_run_id = ? ORDER BY created_at, id
            """,
            (evaluation_id,),
        ).fetchall()
        result["repair_attempts"] = []
        for attempt in attempts:
            item = dict(attempt)
            item["details"] = json.loads(item.pop("details_json"))
            result["repair_attempts"].append(item)
        return result

    def _findings(
        self, revision_id: str, canonical_html: str
    ) -> list[tuple[str, EvaluationFinding, dict[str, Any]]]:
        findings: list[tuple[str, EvaluationFinding, dict[str, Any]]] = []
        report = CanonicalHTMLValidator().validate(canonical_html)
        for issue in report.issues:
            findings.append(
                (
                    "canonical_html",
                    EvaluationFinding(
                        defect_type=issue.code,
                        severity="major" if issue.severity == "error" else "minor",
                        excerpt=issue.element_id or "",
                        context=issue.message,
                        rationale=issue.message,
                    ),
                    {"object_type": "revision", "object_id": revision_id},
                )
            )
        findings.extend(self._claim_findings(revision_id))
        findings.extend(self._argument_findings(revision_id))
        findings.extend(self._evidence_findings(revision_id))
        findings.extend(self._citation_findings(revision_id))
        findings.extend(self._cross_reference_findings(revision_id))
        return findings

    def _argument_findings(
        self, revision_id: str
    ) -> list[tuple[str, EvaluationFinding, dict[str, Any]]]:
        findings: list[tuple[str, EvaluationFinding, dict[str, Any]]] = []
        claims = self.state.execute(
            "SELECT id, text, status FROM claims WHERE revision_id = ?",
            (revision_id,),
        ).fetchall()
        claim_map = {str(row["id"]): row for row in claims}
        for claim_id, claim in claim_map.items():
            relations = {
                str(row["relation_type"])
                for row in self.state.execute(
                    "SELECT relation_type FROM evidence_links WHERE claim_id = ?",
                    (claim_id,),
                )
            }
            if (
                claim["status"] == "supported"
                and relations
                and not relations.intersection({"supports", "quotes"})
            ):
                findings.append(
                    (
                        "argument_role",
                        EvaluationFinding(
                            defect_type="claim_evidence_relation_mismatch",
                            severity="critical",
                            excerpt=str(claim["text"])[:1000],
                            context="evidence_relations=" + ",".join(sorted(relations)),
                            rationale=(
                                "The Claim is marked supported but has no supporting or "
                                "quoting Evidence relation."
                            ),
                        ),
                        {"object_type": "claim", "object_id": claim_id},
                    )
                )
        adjacency: dict[str, set[str]] = {claim_id: set() for claim_id in claim_map}
        for relation in self.state.execute(
            """
            SELECT r.source_claim_id, r.target_claim_id
            FROM claim_relations r
            JOIN claims c ON c.id = r.source_claim_id
            WHERE c.revision_id = ? AND r.relation_type = 'depends_on'
            """,
            (revision_id,),
        ):
            source = str(relation["source_claim_id"])
            target = str(relation["target_claim_id"])
            if target in claim_map:
                adjacency[source].add(target)
        cycles: set[tuple[str, ...]] = set()

        def visit(node: str, path: list[str], active: set[str]) -> None:
            if node in active:
                start = path.index(node)
                cycles.add(tuple(sorted(path[start:])))
                return
            active.add(node)
            path.append(node)
            for target in adjacency[node]:
                visit(target, path, active)
            path.pop()
            active.remove(node)

        for claim_id in sorted(claim_map):
            visit(claim_id, [], set())
        for cycle in sorted(cycles):
            findings.append(
                (
                    "argument_graph",
                    EvaluationFinding(
                        defect_type="claim_dependency_cycle",
                        severity="major",
                        excerpt=" -> ".join(cycle),
                        context="A depends_on cycle prevents a valid argument ordering.",
                        rationale="Claim dependency relations must be acyclic.",
                    ),
                    {"object_type": "claim", "object_id": cycle[0]},
                )
            )
        return findings

    def _claim_findings(
        self, revision_id: str
    ) -> list[tuple[str, EvaluationFinding, dict[str, Any]]]:
        rows = self.state.execute(
            """
            SELECT c.id, c.text, c.status, COUNT(e.id) AS evidence_count
            FROM claims c
            LEFT JOIN evidence_links e ON e.claim_id = c.id
            WHERE c.revision_id = ?
            GROUP BY c.id
            """,
            (revision_id,),
        ).fetchall()
        return [
            (
                "claim_evidence",
                EvaluationFinding(
                    defect_type="unsupported_claim",
                    severity="major",
                    excerpt=str(row["text"])[:1000],
                    context=f"Claim status={row['status']}; evidence_count=0",
                    rationale="The claim has no persisted Evidence link.",
                    confidence=1.0,
                ),
                {"object_type": "claim", "object_id": str(row["id"])},
            )
            for row in rows
            if int(row["evidence_count"]) == 0
        ]

    def _evidence_findings(
        self, revision_id: str
    ) -> list[tuple[str, EvaluationFinding, dict[str, Any]]]:
        rows = self.state.execute(
            """
            SELECT e.id, e.source_span_id, c.text
            FROM evidence_links e
            JOIN claims c ON c.id = e.claim_id
            WHERE c.revision_id = ?
            """,
            (revision_id,),
        ).fetchall()
        findings: list[tuple[str, EvaluationFinding, dict[str, Any]]] = []
        for row in rows:
            exists = self.knowledge.execute(
                "SELECT 1 FROM source_spans WHERE id = ?", (row["source_span_id"],)
            ).fetchone()
            if exists is not None:
                continue
            findings.append(
                (
                    "evidence_existence",
                    EvaluationFinding(
                        defect_type="evidence_source_missing",
                        severity="critical",
                        excerpt=str(row["text"])[:1000],
                        context=str(row["source_span_id"]),
                        rationale="The Evidence link points to a missing SourceSpan.",
                    ),
                    {
                        "object_type": "evidence_link",
                        "object_id": str(row["id"]),
                        "source_span_id": str(row["source_span_id"]),
                    },
                )
            )
        return findings

    def _citation_findings(
        self, revision_id: str
    ) -> list[tuple[str, EvaluationFinding, dict[str, Any]]]:
        results: list[tuple[str, EvaluationFinding, dict[str, Any]]] = []
        citations = self.state.execute(
            "SELECT * FROM citation_events WHERE revision_id = ?", (revision_id,)
        ).fetchall()
        for citation in citations:
            span = self.knowledge.execute(
                """
                SELECT s.id, s.text, u.source_asset_id,
                       sp.page_number AS start_page, ep.page_number AS end_page
                FROM source_spans s
                JOIN document_units u ON u.id = s.document_unit_id
                LEFT JOIN pages sp ON sp.id = s.start_page_id
                LEFT JOIN pages ep ON ep.id = s.end_page_id
                WHERE s.id = ?
                """,
                (citation["source_span_id"],),
            ).fetchone()
            provenance = {
                "object_type": "citation_event",
                "object_id": str(citation["id"]),
                "source_span_id": str(citation["source_span_id"]),
                "source_asset_id": str(span["source_asset_id"]) if span else "",
            }
            if span is None:
                results.append(
                    (
                        "citation_existence",
                        EvaluationFinding(
                            defect_type="citation_source_missing",
                            severity="critical",
                            excerpt=str(citation["rendered_text"] or ""),
                            context=str(citation["source_span_id"]),
                            rationale="The cited SourceSpan does not exist in knowledge.db.",
                        ),
                        provenance,
                    )
                )
                continue
            if citation["citation_kind"] == "quote":
                quote = " ".join(str(citation["rendered_text"] or "").split())
                source = " ".join(str(span["text"]).split())
                if quote and quote not in source:
                    bounded_exact_span = bool(
                        source
                        and "\n" not in str(span["text"])
                        and len(source) <= max(len(quote) * 2, len(quote) + 80)
                    )
                    results.append(
                        (
                            "direct_quote",
                            EvaluationFinding(
                                defect_type="direct_quote_mismatch",
                                severity="critical",
                                excerpt=quote[:1000],
                                context=source[:2000],
                                rationale="The direct quote does not match its SourceSpan.",
                                page_start=span["start_page"],
                                page_end=span["end_page"],
                                repair_candidates=(source,) if bounded_exact_span else (),
                                automatic_repair_allowed=bounded_exact_span,
                            ),
                            provenance,
                        )
                    )
        return results

    def _cross_reference_findings(
        self, revision_id: str
    ) -> list[tuple[str, EvaluationFinding, dict[str, Any]]]:
        rows = self.state.execute(
            """
            SELECT id, label, status FROM cross_references
            WHERE revision_id = ? AND status != 'valid'
            """,
            (revision_id,),
        ).fetchall()
        return [
            (
                "cross_reference",
                EvaluationFinding(
                    defect_type=f"cross_reference_{row['status']}",
                    severity="major",
                    excerpt=str(row["label"]),
                    context=f"CrossReference status={row['status']}",
                    rationale="The persisted cross-reference is not valid.",
                ),
                {"object_type": "cross_reference", "object_id": str(row["id"])},
            )
            for row in rows
        ]


class RepairService:
    """Bounded deterministic repairs that always create a new immutable Revision."""

    def __init__(
        self,
        state_connection: sqlite3.Connection,
        knowledge_connection: sqlite3.Connection,
    ) -> None:
        self.state = state_connection
        self.knowledge = knowledge_connection

    def repair(
        self,
        *,
        evaluation_id: str,
        defect_id: str,
        strategy: str = "restore_source_quote",
    ) -> dict[str, Any]:
        evaluation = self.state.execute(
            "SELECT * FROM evaluation_runs WHERE id = ?", (evaluation_id,)
        ).fetchone()
        defect = self.state.execute(
            """
            SELECT d.* FROM defects d
            JOIN evaluation_defects e ON e.defect_id = d.id
            WHERE e.evaluation_run_id = ? AND d.id = ?
            """,
            (evaluation_id, defect_id),
        ).fetchone()
        if evaluation is None:
            raise FukamiruError(f"Evaluation not found: {evaluation_id}")
        if defect is None:
            raise FukamiruError("Defect does not belong to this evaluation")
        if defect["status"] != "open":
            raise FukamiruError(f"Defect is not open: {defect['status']}")
        attempt_count = int(
            self.state.execute(
                """
                SELECT COUNT(*) AS count FROM repair_attempts
                WHERE evaluation_run_id = ? AND defect_id = ?
                """,
                (evaluation_id, defect_id),
            ).fetchone()["count"]
        )
        if attempt_count >= int(evaluation["max_repair_attempts"]):
            raise FukamiruError("Repair attempt limit reached")
        attempt = attempt_count + 1
        attempt_id = str(uuid.uuid4())
        now = utc_now()
        self.state.execute(
            """
            INSERT INTO repair_attempts(
                id, evaluation_run_id, defect_id, attempt, strategy,
                input_revision_id, status, details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'running', '{}', ?)
            """,
            (
                attempt_id,
                evaluation_id,
                defect_id,
                attempt,
                strategy,
                evaluation["revision_id"],
                now,
            ),
        )
        self.state.execute(
            "UPDATE defects SET status = 'repairing' WHERE id = ?", (defect_id,)
        )
        self.state.commit()
        if not bool(defect["automatic_repair_allowed"]):
            return self._finish_without_revision(
                attempt_id,
                defect_id,
                status="waiting_human",
                details={"reason": "Defect is not allowlisted for automatic repair."},
            )
        if defect["defect_type"] != "direct_quote_mismatch" or strategy != "restore_source_quote":
            return self._finish_without_revision(
                attempt_id,
                defect_id,
                status="waiting_human",
                details={"reason": "No deterministic repair strategy is available."},
            )
        try:
            self._restore_source_quote(
                input_revision_id=str(evaluation["revision_id"]),
                defect=defect,
                attempt_id=attempt_id,
            )
            return self.get(attempt_id)
        except ProtectedBlockError as exc:
            self.state.rollback()
            return self._finish_without_revision(
                attempt_id,
                defect_id,
                status="rejected",
                details={"reason": str(exc), "invariant": "protected_human_block"},
            )
        except RepairNeedsHumanReview as exc:
            self.state.rollback()
            return self._finish_without_revision(
                attempt_id,
                defect_id,
                status="waiting_human",
                details={"reason": str(exc), "invariant": "unambiguous_exact_replacement"},
            )
        except Exception as exc:
            self.state.rollback()
            self._finish_without_revision(
                attempt_id,
                defect_id,
                status="failed",
                details={"error": str(exc)},
            )
            raise

    def get(self, attempt_id: str) -> dict[str, Any]:
        row = self.state.execute(
            "SELECT * FROM repair_attempts WHERE id = ?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise FukamiruError(f"Repair attempt not found: {attempt_id}")
        result = dict(row)
        result["details"] = json.loads(result.pop("details_json"))
        return result

    def _restore_source_quote(
        self,
        *,
        input_revision_id: str,
        defect: sqlite3.Row,
        attempt_id: str,
    ) -> str:
        citation = self.state.execute(
            "SELECT * FROM citation_events WHERE id = ?", (defect["object_id"],)
        ).fetchone()
        if citation is None:
            raise FukamiruError("Citation event no longer exists")
        span = self.knowledge.execute(
            "SELECT text FROM source_spans WHERE id = ?", (citation["source_span_id"],)
        ).fetchone()
        if span is None:
            raise FukamiruError("Citation SourceSpan no longer exists")
        revisions = RevisionService(self.state)
        canonical_html = revisions.canonical_html(input_revision_id)
        rendered = str(citation["rendered_text"] or "")
        source_text = str(span["text"])
        replacements = (
            (rendered, source_text),
            (html.escape(rendered), html.escape(source_text)),
        )
        proposed = canonical_html
        matched = False
        for before, after in replacements:
            if before and proposed.count(before) == 1:
                proposed = proposed.replace(before, after, 1)
                matched = True
                break
        if not matched:
            raise RepairNeedsHumanReview(
                "Quoted text is absent or ambiguous in canonical HTML; human review required"
            )
        revision = self.state.execute(
            "SELECT document_id FROM revisions WHERE id = ?", (input_revision_id,)
        ).fetchone()
        assert revision is not None
        output = revisions.commit_revision(
            document_id=str(revision["document_id"]),
            canonical_html=proposed,
            editor_type="agent",
            operation="agent_edit",
            message="Deterministic direct-quote restoration from SourceSpan",
            parent_revision_id=input_revision_id,
            commit_transaction=False,
        )
        self._copy_revision_graph(
            input_revision_id=input_revision_id,
            output_revision_id=output.revision_id,
            repaired_citation_id=str(citation["id"]),
            repaired_text=source_text,
        )
        completed = utc_now()
        details = {
            "invariant_validation": "passed",
            "source_span_id": str(citation["source_span_id"]),
            "before": rendered,
            "after": source_text,
        }
        self.state.execute(
            """
            UPDATE repair_attempts
            SET output_revision_id = ?, status = 'completed', details_json = ?, completed_at = ?
            WHERE id = ?
            """,
            (output.revision_id, json.dumps(details, ensure_ascii=False), completed, attempt_id),
        )
        self.state.execute(
            "UPDATE defects SET status = 'resolved', resolved_at = ? WHERE id = ?",
            (completed, defect["id"]),
        )
        self.state.commit()
        return output.revision_id

    def _copy_revision_graph(
        self,
        *,
        input_revision_id: str,
        output_revision_id: str,
        repaired_citation_id: str,
        repaired_text: str,
    ) -> None:
        claim_ids: dict[str, str] = {}
        claims = self.state.execute(
            "SELECT * FROM claims WHERE revision_id = ?", (input_revision_id,)
        ).fetchall()
        for claim in claims:
            new_id = str(uuid.uuid4())
            claim_ids[str(claim["id"])] = new_id
            self.state.execute(
                """
                INSERT INTO claims(
                    id, document_id, revision_id, block_id, claim_type,
                    text, modality, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id,
                    claim["document_id"],
                    output_revision_id,
                    claim["block_id"],
                    claim["claim_type"],
                    claim["text"],
                    claim["modality"],
                    claim["status"],
                    utc_now(),
                ),
            )
        for evidence in self.state.execute(
            """
            SELECT e.* FROM evidence_links e
            JOIN claims c ON c.id = e.claim_id WHERE c.revision_id = ?
            """,
            (input_revision_id,),
        ).fetchall():
            self.state.execute(
                """
                INSERT INTO evidence_links(
                    id, claim_id, source_span_id, bibliographic_record_id,
                    relation_type, locator, note, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    claim_ids[str(evidence["claim_id"])],
                    evidence["source_span_id"],
                    evidence["bibliographic_record_id"],
                    evidence["relation_type"],
                    evidence["locator"],
                    evidence["note"],
                    utc_now(),
                ),
            )
        for relation in self.state.execute(
            """
            SELECT r.* FROM claim_relations r
            JOIN claims c ON c.id = r.source_claim_id WHERE c.revision_id = ?
            """,
            (input_revision_id,),
        ).fetchall():
            self.state.execute(
                """
                INSERT INTO claim_relations(
                    source_claim_id, target_claim_id, relation_type, rationale
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    claim_ids[str(relation["source_claim_id"])],
                    claim_ids.get(
                        str(relation["target_claim_id"]),
                        str(relation["target_claim_id"]),
                    ),
                    relation["relation_type"],
                    relation["rationale"],
                ),
            )
        for citation in self.state.execute(
            "SELECT * FROM citation_events WHERE revision_id = ?", (input_revision_id,)
        ).fetchall():
            rendered = (
                repaired_text
                if str(citation["id"]) == repaired_citation_id
                else citation["rendered_text"]
            )
            self.state.execute(
                """
                INSERT INTO citation_events(
                    id, revision_id, block_id, source_span_id, bibliographic_record_id,
                    citation_kind, locator, rendered_text, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    output_revision_id,
                    citation["block_id"],
                    citation["source_span_id"],
                    citation["bibliographic_record_id"],
                    citation["citation_kind"],
                    citation["locator"],
                    rendered,
                    utc_now(),
                ),
            )
        for reference in self.state.execute(
            "SELECT * FROM cross_references WHERE revision_id = ?", (input_revision_id,)
        ).fetchall():
            self.state.execute(
                """
                INSERT INTO cross_references(
                    id, revision_id, source_block_id, target_document_id,
                    target_block_id, label, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    output_revision_id,
                    reference["source_block_id"],
                    reference["target_document_id"],
                    reference["target_block_id"],
                    reference["label"],
                    reference["status"],
                    utc_now(),
                ),
            )

    def _finish_without_revision(
        self,
        attempt_id: str,
        defect_id: str,
        *,
        status: str,
        details: dict[str, Any],
    ) -> dict[str, Any]:
        completed = utc_now()
        self.state.execute(
            """
            UPDATE repair_attempts
            SET status = ?, details_json = ?, completed_at = ? WHERE id = ?
            """,
            (status, json.dumps(details, ensure_ascii=False), completed, attempt_id),
        )
        self.state.execute(
            "UPDATE defects SET status = 'open' WHERE id = ?", (defect_id,)
        )
        if status == "waiting_human":
            defect = self.state.execute(
                "SELECT run_id FROM defects WHERE id = ?", (defect_id,)
            ).fetchone()
            assert defect is not None
            self.state.execute(
                """
                INSERT INTO human_reviews(
                    id, run_id, object_type, object_id, question,
                    options_json, status, created_at
                ) VALUES (?, ?, 'repair_attempt', ?, ?, ?, 'open', ?)
                """,
                (
                    str(uuid.uuid4()),
                    defect["run_id"],
                    attempt_id,
                    "決定論的に一意な修復を作成できません。候補を確認してください。",
                    json.dumps(["approve", "reject", "edit"], ensure_ascii=False),
                    completed,
                ),
            )
        self.state.commit()
        return self.get(attempt_id)
