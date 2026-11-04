"""Evidence-grounded academic translation research and compliance checks."""

from __future__ import annotations

import hashlib
import html as html_lib
import io
import json
import re
import sqlite3
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from pypdf import PdfReader

from fukamiru.config import Settings
from fukamiru.errors import NonRetryableFukamiruError
from fukamiru.knowledge.service import KnowledgeSearchService
from fukamiru.literature.service import (
    CrossrefProvider,
    LiteratureCandidate,
    LiteratureProvider,
    OpenAlexProvider,
    _system_ssl_context,
)
from fukamiru.storage.runs import utc_now

_PERSON_NAME = re.compile(
    r"(?<![\w-])"
    r"[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’.-]{1,}"
    r"(?:\s+[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’.-]{1,}){1,3}"
    r"(?![\w-])"
)
_CITED_SURNAME = re.compile(
    r"(?<![\w-])([A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’.-]{2,})"
    r"\s*[（(](?:18|19|20)\d{2}[a-z]?[）)]"
)
_EXPLICIT_NAME = re.compile(
    r"(?P<source>[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’.-]{2,}"
    r"(?:\s+[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’.-]{1,}){0,3})"
    r"\s*は[、,]?\s*"
    r"(?P<target>[ァ-ヺー・=＝]+)"
    r"(?:という)?(?:カタカナ|表記|漢字)?(?:に|と)(?:すべき|する|置換)",
)
_EXPLICIT_SOURCE_EQUIVALENT = re.compile(
    r"[「『](?P<literal>[^」』]{2,})[」』]\s*[=＝]\s*"
    r"(?P<source>[^\r\n。]{3,}?)(?=という(?:一文|文|表現)|[。\r\n])"
)
_EXPLICIT_RECOMMENDATION = re.compile(
    r"(?:よって|したがって|そのため)[、,\s]*[「『]"
    r"(?P<target>[^」』]{2,})[」』][^。\r\n]{0,80}?(?:すべき|望ましい|とする)"
)
_DIRECT_TRANSLATION = re.compile(
    r"(?P<source>[A-Za-zÀ-ÖØ-öø-ÿ][^。\r\n「」『』]{1,100}?)"
    r"(?:は|を)[「『](?P<target>[^」』]{1,120})[」』]"
    r"(?:と|に)(?:訳す|翻訳する|置換する|表記する)"
)
_RESEARCH_TITLE = re.compile(
    r"(?:博士論文|修士論文|論文|著書|研究)\s*[「『\"“]?"
    r"(?P<title>[A-ZÀ-ÖØ-Þ][^\r\n。；;「」『』\"”]{5,180}?)"
    r"(?=\s*(?:に|を|で|は|について|において|[」』\"”]))"
)


def _normalized(value: str) -> str:
    return " ".join(value.replace("’", "'").replace("＝", "=").casefold().split())


def _stable_id(namespace: str, *values: str) -> str:
    key = ":".join((namespace, *values))
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"fukamiru:{key}"))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _tokens(value: str) -> set[str]:
    return {
        token.casefold()
        for token in re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ0-9][\wÀ-ÖØ-öø-ÿ'-]{2,}", value)
        if token.casefold()
        not in {"the", "and", "for", "une", "des", "les", "dans", "avec", "pour"}
    }


def _source_span_ids(segments: Iterable[dict[str, Any]], needle: str) -> tuple[str, ...]:
    normalized_needle = _normalized(needle)
    return tuple(
        dict.fromkeys(
            str(segment.get("source_span_id") or "")
            for segment in segments
            if normalized_needle in _normalized(str(segment.get("text") or ""))
            and segment.get("source_span_id")
        )
    )


@dataclass(frozen=True)
class ResearchTrigger:
    trigger_type: str
    source_text: str
    query_text: str
    source_span_ids: tuple[str, ...] = ()
    context_text: str = ""
    confidence: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str | None = None

    @property
    def normalized_key(self) -> str:
        return _normalized(self.source_text)


@dataclass(frozen=True)
class ComplianceFinding:
    finding_type: str
    severity: str
    source_excerpt: str
    target_excerpt: str
    decision_id: str | None
    details: dict[str, Any]


class ResearchTriggerDetector:
    """Detect research-bearing entities before document translation is batched."""

    def detect(
        self,
        segments: list[dict[str, Any]],
        instructions: str,
    ) -> list[ResearchTrigger]:
        source_text = "\n\n".join(str(segment.get("text") or "") for segment in segments)
        combined = source_text + "\n" + instructions
        triggers: dict[tuple[str, str], ResearchTrigger] = {}

        def add(trigger: ResearchTrigger) -> None:
            key = (trigger.trigger_type, trigger.normalized_key)
            current = triggers.get(key)
            if current is None or trigger.confidence > current.confidence:
                triggers[key] = trigger

        for match in _PERSON_NAME.finditer(combined):
            name = match.group(0).strip()
            before = combined[max(0, match.start() - 80) : match.start()]
            after = combined[match.end() : match.end() + 100]
            person_context = bool(
                re.search(
                    r"(?:by|par|selon|d['’]après|auteur|autrice|chercheu[rs]?)\s+$",
                    before,
                    re.IGNORECASE,
                )
                or re.search(
                    r"^(?:\s+の)?(?:博士論文|修士論文|論文|著書|研究|教授|博士)",
                    after,
                )
                or re.search(r"^\s*[（(](?:18|19|20)\d{2}", after)
            )
            if not person_context:
                continue
            add(
                ResearchTrigger(
                    "person",
                    name,
                    name,
                    _source_span_ids(segments, name),
                    combined[max(0, match.start() - 160) : match.end() + 240],
                    0.86,
                )
            )
        for match in _CITED_SURNAME.finditer(combined):
            name = match.group(1).strip()
            add(
                ResearchTrigger(
                    "person",
                    name,
                    match.group(0),
                    _source_span_ids(segments, name),
                    combined[max(0, match.start() - 160) : match.end() + 240],
                    0.82,
                    {"citation_year": re.findall(r"\d{4}", match.group(0))[0]},
                )
            )
        for match in _EXPLICIT_NAME.finditer(instructions):
            name = match.group("source").strip()
            target = match.group("target").replace("＝", "=").strip()
            add(
                ResearchTrigger(
                    "person",
                    name,
                    name,
                    _source_span_ids(segments, name),
                    match.group(0),
                    1.0,
                    {
                        "explicit_target": target,
                        "decision_kind": "person_name",
                        "scope_type": "author",
                    },
                )
            )
        for match in _RESEARCH_TITLE.finditer(instructions):
            title = match.group("title").strip(" \t,、")
            add(
                ResearchTrigger(
                    "research",
                    title,
                    title,
                    _source_span_ids(segments, title),
                    match.group(0),
                    0.9,
                    {"research_title": title},
                )
            )

        equivalents = list(_EXPLICIT_SOURCE_EQUIVALENT.finditer(instructions))
        recommendation = _EXPLICIT_RECOMMENDATION.search(instructions)
        if recommendation is not None:
            target = recommendation.group("target").strip()
            for match in equivalents:
                source = match.group("source").strip(" \t,、")
                forbidden = match.group("literal").strip()
                add(
                    ResearchTrigger(
                        "concept",
                        source,
                        source,
                        _source_span_ids(segments, source),
                        match.group(0) + "\n" + recommendation.group(0),
                        1.0,
                        {
                            "explicit_target": target,
                            "forbidden_translations": [forbidden],
                            "decision_kind": "term",
                            "scope_type": "document",
                        },
                    )
                )
        for match in _DIRECT_TRANSLATION.finditer(instructions):
            source = match.group("source").strip(" \t,、")
            target = match.group("target").strip()
            add(
                ResearchTrigger(
                    "concept",
                    source,
                    source,
                    _source_span_ids(segments, source),
                    match.group(0),
                    1.0,
                    {
                        "explicit_target": target,
                        "decision_kind": "term",
                        "scope_type": "document",
                    },
                )
            )
        return sorted(
            triggers.values(),
            key=lambda item: (item.trigger_type, item.normalized_key),
        )


class AcademicResearchRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def save_triggers(
        self,
        *,
        run_id: str,
        document_unit_id: str,
        triggers: Iterable[ResearchTrigger],
    ) -> list[dict[str, Any]]:
        now = utc_now()
        ids: list[str] = []
        for trigger in triggers:
            trigger_id = _stable_id(
                "translation-trigger", run_id, trigger.trigger_type, trigger.normalized_key
            )
            ids.append(trigger_id)
            self.connection.execute(
                """
                INSERT INTO translation_research_triggers(
                    id, run_id, document_unit_id, trigger_type, source_text,
                    normalized_key, query_text, source_span_ids_json, context_text,
                    metadata_json, status, confidence, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'detected', ?, ?)
                ON CONFLICT(run_id, trigger_type, normalized_key) DO UPDATE SET
                    query_text = excluded.query_text,
                    source_span_ids_json = excluded.source_span_ids_json,
                    context_text = excluded.context_text,
                    metadata_json = excluded.metadata_json,
                    confidence = excluded.confidence
                """,
                (
                    trigger_id,
                    run_id,
                    document_unit_id,
                    trigger.trigger_type,
                    trigger.source_text,
                    trigger.normalized_key,
                    trigger.query_text,
                    _json(trigger.source_span_ids),
                    trigger.context_text,
                    _json(trigger.metadata),
                    trigger.confidence,
                    now,
                ),
            )
        self.connection.commit()
        if not ids:
            return []
        return self.triggers(run_id)

    def triggers(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT * FROM translation_research_triggers
            WHERE run_id = ? ORDER BY trigger_type, normalized_key
            """,
            (run_id,),
        ).fetchall()
        return [self._decode_trigger(row) for row in rows]

    def add_evidence(
        self,
        *,
        trigger_id: str,
        evidence_type: str,
        excerpt: str,
        title: str | None = None,
        authors: Iterable[str] = (),
        source_url: str | None = None,
        locator: str | None = None,
        bibliographic_record_id: str | None = None,
        relevance: float = 1.0,
        confidence: float = 1.0,
        provenance: dict[str, Any] | None = None,
    ) -> str:
        excerpt = excerpt.strip()
        digest = hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
        evidence_id = _stable_id(
            "translation-evidence",
            trigger_id,
            evidence_type,
            source_url or "",
            locator or "",
            digest,
        )
        self.connection.execute(
            """
            INSERT OR IGNORE INTO translation_academic_evidence(
                id, trigger_id, evidence_type, bibliographic_record_id, title,
                authors_json, source_url, locator, excerpt, excerpt_sha256,
                relevance, confidence, provenance_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                evidence_id,
                trigger_id,
                evidence_type,
                bibliographic_record_id,
                title,
                _json(tuple(authors)),
                source_url,
                locator,
                excerpt,
                digest,
                max(0.0, min(1.0, relevance)),
                max(0.0, min(1.0, confidence)),
                _json(provenance or {}),
                utc_now(),
            ),
        )
        self.connection.commit()
        return evidence_id

    def evidence(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT e.* FROM translation_academic_evidence e
            JOIN translation_research_triggers t ON t.id = e.trigger_id
            WHERE t.run_id = ? ORDER BY e.relevance DESC, e.created_at, e.id
            """,
            (run_id,),
        ).fetchall()
        return [self._decode_evidence(row) for row in rows]

    def mark_resolution(self, trigger_id: str, *, resolved: bool) -> None:
        self.connection.execute(
            """
            UPDATE translation_research_triggers
            SET status = ?, resolved_at = ? WHERE id = ?
            """,
            ("resolved" if resolved else "unresolved", utc_now(), trigger_id),
        )
        self.connection.commit()

    def upsert_decision(self, decision: dict[str, Any]) -> str:
        source_term = str(decision["source_term"]).strip()
        scope_type = str(decision["scope_type"])
        scope_key = _normalized(str(decision["scope_key"]))
        decision_kind = str(decision.get("decision_kind", "term"))
        source_language = str(decision.get("source_language", "auto"))
        target_language = str(decision.get("target_language", "ja"))
        decision_id = _stable_id(
            "translation-decision",
            decision_kind,
            _normalized(source_term),
            source_language,
            target_language,
            scope_type,
            scope_key,
        )
        now = utc_now()
        self.connection.execute(
            """
            INSERT INTO translation_term_decisions(
                id, origin_run_id, decision_kind, source_term, source_language,
                target_term, target_language, scope_type, scope_key,
                document_unit_id, author_name, research_title, rationale,
                evidence_ids_json, forbidden_translations_json, required,
                status, confidence, created_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(
                decision_kind, source_term, source_language, target_language,
                scope_type, scope_key
            ) DO UPDATE SET
                target_term = excluded.target_term,
                document_unit_id = excluded.document_unit_id,
                author_name = excluded.author_name,
                research_title = excluded.research_title,
                rationale = excluded.rationale,
                evidence_ids_json = excluded.evidence_ids_json,
                forbidden_translations_json = excluded.forbidden_translations_json,
                required = excluded.required,
                status = excluded.status,
                confidence = excluded.confidence,
                created_by = excluded.created_by,
                updated_at = excluded.updated_at
            """,
            (
                decision_id,
                decision.get("origin_run_id"),
                decision_kind,
                source_term,
                source_language,
                str(decision["target_term"]).strip(),
                target_language,
                scope_type,
                scope_key,
                decision.get("document_unit_id"),
                decision.get("author_name"),
                decision.get("research_title"),
                str(decision.get("rationale") or ""),
                _json(decision.get("evidence_ids", [])),
                _json(decision.get("forbidden_translations", [])),
                int(bool(decision.get("required", True))),
                str(decision.get("status", "candidate")),
                float(decision.get("confidence", 0.0)),
                str(decision.get("created_by", "agent")),
                now,
                now,
            ),
        )
        self.connection.commit()
        return decision_id

    def decisions_for_run(
        self,
        *,
        run_id: str,
        document_unit_id: str,
        target_language: str,
    ) -> list[dict[str, Any]]:
        trigger_keys = {
            str(row["normalized_key"])
            for row in self.connection.execute(
                "SELECT normalized_key FROM translation_research_triggers WHERE run_id = ?",
                (run_id,),
            )
        }
        rows = self.connection.execute(
            """
            SELECT * FROM translation_term_decisions
            WHERE status = 'approved' AND target_language = ?
              AND (
                origin_run_id = ?
                OR (scope_type = 'document' AND scope_key = ?)
              )
            ORDER BY required DESC, confidence DESC, created_at
            """,
            (target_language, run_id, _normalized(document_unit_id)),
        ).fetchall()
        decoded = [self._decode_decision(row) for row in rows]
        reusable = self.connection.execute(
            """
            SELECT * FROM translation_term_decisions
            WHERE status = 'approved' AND target_language = ?
              AND scope_type IN ('author', 'research')
            ORDER BY required DESC, confidence DESC, created_at
            """,
            (target_language,),
        ).fetchall()
        seen = {str(item["id"]) for item in decoded}
        for row in reusable:
            if str(row["scope_key"]) in trigger_keys and str(row["id"]) not in seen:
                decoded.append(self._decode_decision(row))
        return decoded

    def replace_compliance_findings(
        self,
        *,
        run_id: str,
        translation_version_id: str,
        findings: Iterable[ComplianceFinding],
    ) -> list[str]:
        self.connection.execute(
            """
            UPDATE translation_compliance_findings
            SET status = 'resolved', resolved_at = ?
            WHERE run_id = ? AND status = 'open'
            """,
            (utc_now(), run_id),
        )
        ids: list[str] = []
        now = utc_now()
        for ordinal, finding in enumerate(findings):
            finding_id = _stable_id(
                "translation-compliance",
                run_id,
                translation_version_id,
                str(ordinal),
                finding.finding_type,
                finding.decision_id or "",
            )
            ids.append(finding_id)
            self.connection.execute(
                """
                INSERT OR REPLACE INTO translation_compliance_findings(
                    id, run_id, translation_version_id, batch_ordinal,
                    finding_type, severity, decision_id, source_excerpt,
                    target_excerpt, details_json, status, created_at
                ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, 'open', ?)
                """,
                (
                    finding_id,
                    run_id,
                    translation_version_id,
                    finding.finding_type,
                    finding.severity,
                    finding.decision_id,
                    finding.source_excerpt,
                    finding.target_excerpt,
                    _json(finding.details),
                    now,
                ),
            )
        self.connection.commit()
        return ids

    def snapshot(
        self,
        *,
        run_id: str,
        document_unit_id: str,
        target_language: str,
    ) -> dict[str, Any]:
        decisions = self.decisions_for_run(
            run_id=run_id,
            document_unit_id=document_unit_id,
            target_language=target_language,
        )
        candidate_rows = self.connection.execute(
            """
            SELECT * FROM translation_term_decisions
            WHERE origin_run_id = ? AND status != 'approved'
            ORDER BY created_at, id
            """,
            (run_id,),
        ).fetchall()
        decisions.extend(self._decode_decision(row) for row in candidate_rows)
        finding_rows = self.connection.execute(
            """
            SELECT * FROM translation_compliance_findings
            WHERE run_id = ? ORDER BY created_at, id
            """,
            (run_id,),
        ).fetchall()
        findings: list[dict[str, Any]] = []
        for row in finding_rows:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            findings.append(item)
        return {
            "run_id": run_id,
            "triggers": self.triggers(run_id),
            "evidence": self.evidence(run_id),
            "decisions": decisions,
            "compliance_findings": findings,
        }

    @staticmethod
    def _decode_trigger(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["source_span_ids"] = json.loads(item.pop("source_span_ids_json"))
        item["metadata"] = json.loads(item.pop("metadata_json"))
        return item

    @staticmethod
    def _decode_evidence(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["authors"] = json.loads(item.pop("authors_json"))
        item["provenance"] = json.loads(item.pop("provenance_json"))
        return item

    @staticmethod
    def _decode_decision(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["evidence_ids"] = json.loads(item.pop("evidence_ids_json"))
        item["forbidden_translations"] = json.loads(
            item.pop("forbidden_translations_json")
        )
        item["required"] = bool(item["required"])
        return item


class AcademicEvidenceResolver:
    """Resolve triggers against local knowledge, metadata providers and OA PDFs."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        settings: Settings,
        *,
        semantic_index: Any | None = None,
        providers: Iterable[LiteratureProvider] | None = None,
        opener: Any = urllib.request.urlopen,
    ) -> None:
        self.connection = connection
        self.settings = settings
        self.semantic_index = semantic_index
        self.providers = tuple(providers or (OpenAlexProvider(), CrossrefProvider()))
        self.opener = opener
        self.ssl_context = _system_ssl_context()
        self.repository = AcademicResearchRepository(connection)

    def resolve(
        self,
        trigger: dict[str, Any],
        *,
        instructions: str,
        online: bool,
        semantic_index_version_id: str | None = None,
    ) -> dict[str, Any]:
        evidence_ids: list[str] = []
        substantive_evidence_count = 0
        errors: list[str] = []
        trigger_id = str(trigger["id"])
        if instructions.strip():
            evidence_ids.append(
                self.repository.add_evidence(
                    trigger_id=trigger_id,
                    evidence_type="user_instruction",
                    excerpt=instructions[:6000],
                    relevance=1.0,
                    confidence=1.0,
                    provenance={"kind": "user_instruction"},
                )
            )
        try:
            hits = KnowledgeSearchService(
                self.connection,
                semantic_index=self.semantic_index,
                rrf_k=self.settings.retrieval.hybrid_rrf_k,
            ).search(
                str(trigger["query_text"]),
                limit=self.settings.translation.academic_research_local_results,
                semantic_index_version_id=semantic_index_version_id,
            )
        except NonRetryableFukamiruError:
            raise
        except Exception as exc:  # transient local semantic index failure is recorded
            hits = []
            errors.append(f"local:{exc}")
        for hit in hits:
            citation = hit.get("citation") or {}
            evidence_ids.append(
                self.repository.add_evidence(
                    trigger_id=trigger_id,
                    evidence_type="local_corpus",
                    excerpt=str(hit.get("text") or "")[:6000],
                    source_url=str(citation.get("source_path") or "") or None,
                    locator=(
                        f"page {citation['start_page']}"
                        if citation.get("start_page")
                        else None
                    ),
                    relevance=float(hit.get("score") or 0.6),
                    confidence=0.85,
                    provenance={"citation": citation, "chunk_id": hit.get("chunk_id")},
                )
            )
            substantive_evidence_count += 1
        candidates: list[tuple[float, LiteratureCandidate]] = []
        if online and trigger["trigger_type"] in {"research", "person"}:
            for provider in self.providers:
                try:
                    for candidate in provider.search(
                        str(trigger["query_text"]),
                        limit=self.settings.translation.academic_research_provider_results,
                    ):
                        score = self._candidate_score(trigger, candidate)
                        if score >= 0.2:
                            candidates.append((score, candidate))
                except Exception as exc:
                    errors.append(f"{provider.name}:{exc}")
        candidates.sort(key=lambda item: item[0], reverse=True)
        seen: set[tuple[str, str]] = set()
        for score, candidate in candidates[: self.settings.translation.academic_research_sources]:
            candidate_key = (candidate.provider, candidate.provider_record_id)
            if candidate_key in seen:
                continue
            seen.add(candidate_key)
            excerpt = self._candidate_excerpt(candidate)
            evidence_ids.append(
                self.repository.add_evidence(
                    trigger_id=trigger_id,
                    evidence_type="provider_metadata",
                    excerpt=excerpt,
                    title=candidate.title,
                    authors=candidate.authors,
                    source_url=candidate.url or candidate.oa_url,
                    relevance=score,
                    confidence=0.75,
                    provenance={
                        "provider": candidate.provider,
                        "provider_record_id": candidate.provider_record_id,
                        "doi": candidate.doi,
                        "oa_url": candidate.oa_url,
                        "license_url": candidate.license_url,
                    },
                )
            )
            substantive_evidence_count += 1
            if candidate.oa_url and score >= 0.45:
                try:
                    for page_number, page_excerpt in self._oa_pdf_excerpts(
                        candidate.oa_url,
                        str(trigger["query_text"]),
                    ):
                        evidence_ids.append(
                            self.repository.add_evidence(
                                trigger_id=trigger_id,
                                evidence_type="primary_pdf",
                                excerpt=page_excerpt,
                                title=candidate.title,
                                authors=candidate.authors,
                                source_url=candidate.oa_url,
                                locator=f"PDF page {page_number}",
                                relevance=min(1.0, score + 0.15),
                                confidence=0.95,
                                provenance={
                                    "provider": candidate.provider,
                                    "doi": candidate.doi,
                                    "license_url": candidate.license_url,
                                },
                            )
                        )
                        substantive_evidence_count += 1
                except Exception as exc:
                    errors.append(f"oa:{candidate.oa_url}:{exc}")
        resolved = bool(
            substantive_evidence_count
            or (trigger.get("metadata") or {}).get("explicit_target")
        )
        self.repository.mark_resolution(trigger_id, resolved=resolved)
        return {"evidence_ids": evidence_ids, "errors": errors, "resolved": resolved}

    @staticmethod
    def _candidate_score(
        trigger: dict[str, Any], candidate: LiteratureCandidate
    ) -> float:
        query = _tokens(str(trigger["query_text"]))
        haystack = _tokens(candidate.title + " " + " ".join(candidate.authors))
        if not query:
            return 0.0
        overlap = len(query & haystack) / len(query)
        exact = _normalized(str(trigger["source_text"])) in _normalized(
            candidate.title + " " + " ".join(candidate.authors)
        )
        return min(1.0, overlap + (0.45 if exact else 0.0))

    @staticmethod
    def _candidate_excerpt(candidate: LiteratureCandidate) -> str:
        payload = candidate.provider_payload
        abstract = str(payload.get("abstract") or "").strip()
        inverted = payload.get("abstract_inverted_index")
        if not abstract and isinstance(inverted, dict):
            positioned = sorted(
                (int(position), word)
                for word, positions in inverted.items()
                for position in positions
            )
            abstract = " ".join(word for _, word in positioned)
        parts = [candidate.title, "Authors: " + "; ".join(candidate.authors)]
        if abstract:
            parts.append(abstract[:6000])
        if candidate.doi:
            parts.append("DOI: " + candidate.doi)
        return "\n".join(part for part in parts if part)

    def _oa_pdf_excerpts(self, source_url: str, query: str) -> list[tuple[int, str]]:
        parsed = urllib.parse.urlparse(source_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            return []
        cache_root = self.settings.paths.corpus_root / "oa_downloads" / "translation_research"
        cache_root.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(source_url.encode("utf-8")).hexdigest()
        cached = cache_root / f"{digest}.pdf"
        if cached.is_file():
            pdf_bytes = cached.read_bytes()
        else:
            pdf_bytes, final_url, content_type = self._fetch_evidence_url(source_url)
            if not pdf_bytes.startswith(b"%PDF-"):
                html_text = pdf_bytes.decode("utf-8", errors="replace")
                candidate_urls = [
                    html_lib.unescape(url)
                    for url in re.findall(
                        r"(?:href|content)=[\"']([^\"']+)[\"']",
                        html_text,
                        flags=re.IGNORECASE,
                    )
                    if ".pdf" in url.casefold() or "/bitstream" in url.casefold()
                ]
                pdf_bytes = b""
                for candidate_url in candidate_urls[:5]:
                    resolved_url = urllib.parse.urljoin(final_url, candidate_url)
                    resolved = urllib.parse.urlparse(resolved_url)
                    if (
                        resolved.scheme != "https"
                        or not resolved.netloc
                        or resolved.username
                        or resolved.password
                    ):
                        continue
                    candidate_bytes, _, candidate_type = self._fetch_evidence_url(
                        resolved_url
                    )
                    if candidate_bytes.startswith(b"%PDF-") or candidate_type == "application/pdf":
                        pdf_bytes = candidate_bytes
                        break
                if not pdf_bytes:
                    return []
            temporary = cached.with_suffix(".partial")
            temporary.write_bytes(pdf_bytes)
            temporary.replace(cached)
        reader = PdfReader(io.BytesIO(pdf_bytes))
        query_tokens = _tokens(query)
        ranked: list[tuple[int, int, int, str]] = []
        for page_number, page in enumerate(reader.pages, start=1):
            text = " ".join((page.extract_text() or "").split())
            if not text:
                continue
            normalized_text = text.casefold()
            coverage = sum(token in normalized_text for token in query_tokens)
            frequency = sum(normalized_text.count(token) for token in query_tokens)
            ranked.append((coverage, frequency, page_number, text[:6000]))
        ranked.sort(key=lambda item: (item[0], item[1], -item[2]), reverse=True)
        return [
            (page_number, excerpt)
            for coverage, _, page_number, excerpt in ranked[:3]
            if coverage > 0 or page_number <= 3
        ]

    def _fetch_evidence_url(self, source_url: str) -> tuple[bytes, str, str]:
        request = urllib.request.Request(
            source_url,
            headers={
                "Accept": "application/pdf,text/html;q=0.9",
                "User-Agent": "Fukamiru/0.4",
            },
        )
        buffer = io.BytesIO()
        with self.opener(
            request,
            timeout=self.settings.translation.academic_research_timeout_seconds,
            context=self.ssl_context,
        ) as response:
            final_url = str(response.geturl())
            parsed = urllib.parse.urlparse(final_url)
            if parsed.scheme != "https" or not parsed.netloc:
                raise ValueError("academic evidence redirected outside HTTPS")
            content_type = str(response.headers.get_content_type())
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                if (
                    buffer.tell() + len(chunk)
                    > self.settings.translation.academic_research_max_pdf_bytes
                ):
                    raise ValueError("academic evidence response exceeds the configured limit")
                buffer.write(chunk)
        return buffer.getvalue(), final_url, content_type


class TranslationComplianceValidator:
    def validate(
        self,
        *,
        source_text: str,
        target_text: str,
        decisions: Iterable[dict[str, Any]],
        person_triggers: Iterable[dict[str, Any]],
    ) -> list[ComplianceFinding]:
        findings: list[ComplianceFinding] = []
        source_normalized = _normalized(source_text)
        target_normalized = _normalized(target_text)
        decisions_list = list(decisions)
        for decision in decisions_list:
            source_term = str(decision["source_term"])
            normalized_source_term = _normalized(source_term)
            source_occurrences = source_normalized.count(normalized_source_term)
            if source_occurrences == 0:
                continue
            decision_id = str(decision["id"])
            for forbidden in decision.get("forbidden_translations", []):
                if forbidden and _normalized(str(forbidden)) in target_normalized:
                    findings.append(
                        ComplianceFinding(
                            "forbidden_translation",
                            "critical",
                            source_term[:500],
                            str(forbidden)[:500],
                            decision_id,
                            {"forbidden_translation": forbidden},
                        )
                    )
            target_term = str(decision["target_term"])
            target_occurrences = target_normalized.count(_normalized(target_term))
            if decision.get("required") and target_occurrences < source_occurrences:
                findings.append(
                    ComplianceFinding(
                        "decision_not_applied",
                        "critical",
                        source_term[:500],
                        target_text[:1000],
                        decision_id,
                        {
                            "required_translation": target_term,
                            "source_occurrences": source_occurrences,
                            "target_occurrences": target_occurrences,
                            "scope_type": decision["scope_type"],
                            "scope_key": decision["scope_key"],
                        },
                    )
                )
        known_people = {
            _normalized(str(trigger["source_text"])): str(trigger["source_text"])
            for trigger in person_triggers
            if re.search(r"[A-Za-zÀ-ÖØ-öø-ÿ]", str(trigger["source_text"]))
        }
        for normalized_name, source_name in known_people.items():
            if normalized_name in source_normalized and normalized_name in target_normalized:
                mapped = next(
                    (
                        item
                        for item in decisions_list
                        if item["decision_kind"] == "person_name"
                        and _normalized(str(item["source_term"])) == normalized_name
                    ),
                    None,
                )
                findings.append(
                    ComplianceFinding(
                        "person_name_remaining",
                        "major",
                        source_name,
                        source_name,
                        str(mapped["id"]) if mapped else None,
                        {
                            "required_translation": (
                                str(mapped["target_term"]) if mapped else None
                            )
                        },
                    )
                )
        unique: dict[tuple[str, str | None, str], ComplianceFinding] = {}
        for finding in findings:
            key = (
                finding.finding_type,
                finding.decision_id,
                _normalized(finding.target_excerpt),
            )
            unique[key] = finding
        return list(unique.values())


def instruction_decisions(
    *,
    run_id: str,
    document_unit_id: str,
    source_language: str,
    target_language: str,
    triggers: Iterable[dict[str, Any]],
    evidence_ids_by_trigger: dict[str, list[str]],
) -> list[dict[str, Any]]:
    decisions: list[dict[str, Any]] = []
    for trigger in triggers:
        metadata = trigger.get("metadata") or {}
        target = str(metadata.get("explicit_target") or "").strip()
        if not target:
            continue
        scope_type = str(metadata.get("scope_type") or "document")
        scope_key = (
            str(trigger["normalized_key"])
            if scope_type in {"author", "research"}
            else document_unit_id
        )
        decisions.append(
            {
                "origin_run_id": run_id,
                "decision_kind": str(metadata.get("decision_kind") or "term"),
                "source_term": str(trigger["source_text"]),
                "source_language": source_language,
                "target_term": target,
                "target_language": target_language,
                "scope_type": scope_type,
                "scope_key": scope_key,
                "document_unit_id": document_unit_id,
                "author_name": (
                    str(trigger["source_text"]) if scope_type == "author" else None
                ),
                "research_title": (
                    str(trigger["source_text"]) if scope_type == "research" else None
                ),
                "rationale": "User instruction compiled into an enforceable decision.",
                "evidence_ids": evidence_ids_by_trigger.get(str(trigger["id"]), []),
                "forbidden_translations": metadata.get("forbidden_translations", []),
                "required": True,
                "status": "approved",
                "confidence": 1.0,
                "created_by": "instruction",
            }
        )
    return decisions
