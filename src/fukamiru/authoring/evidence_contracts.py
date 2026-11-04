"""Strict V31.1 contracts for evidence use, audit routing, and review handoff."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fukamiru.errors import FukamiruError

SCHEMA_VERSION = "31.1"
AUDIT_VERSION = "v31.1"

ClaimRole = Literal[
    "source_fact",
    "attributed_position",
    "memo_supported",
    "author_inference",
    "contextual",
]
VerificationStatus = Literal["verified", "review_required", "insufficient"]
EvidenceKind = Literal[
    "rag_excerpt",
    "memo_summary",
    "bibliographic_record",
    "full_text",
]
FindingDisposition = Literal["auto_repair", "human_review", "blocking"]
OutputType = Literal[
    "documented_result",
    "interpretive_result",
    "provisional_result",
]
RequiredEvidenceLevel = Literal["direct", "traceable", "reviewable"]
RepairOperationKind = Literal["replace", "delete", "merge", "split", "attach_evidence"]


class EvidenceContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SupportLocator(EvidenceContractModel):
    chunk_ids: tuple[str, ...] = ()
    page: int | str | None = None
    uri: str | None = None


class SupportCapability(EvidenceContractModel):
    claim_roles: tuple[ClaimRole, ...]
    maximum_verification_status: VerificationStatus
    supports_direct_attribution: bool = False
    supports_quotation: bool = False


class FixedSourceAsset(EvidenceContractModel):
    source_id: str = Field(min_length=1)
    evidence_kind: EvidenceKind
    retrieval_path: str = Field(min_length=1)
    verification_status: VerificationStatus
    support_locator: SupportLocator = Field(default_factory=SupportLocator)
    support_capabilities: SupportCapability
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class FixedAssets(EvidenceContractModel):
    schema_version: Literal["31.1"] = SCHEMA_VERSION
    allowed_source_ids: tuple[str, ...]
    sources: tuple[FixedSourceAsset, ...]
    asset_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_source_identity(self) -> FixedAssets:
        source_ids = [item.source_id for item in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("fixed assets contain duplicate source IDs")
        if len(self.allowed_source_ids) != len(set(self.allowed_source_ids)):
            raise ValueError("allowed_source_ids contains duplicates")
        if not set(self.allowed_source_ids).issubset(source_ids):
            raise ValueError("allowed_source_ids references an unknown fixed source")
        expected = fixed_asset_hash(self.sources, self.allowed_source_ids)
        if self.asset_hash != expected:
            raise ValueError("fixed asset hash does not match immutable source inventory")
        return self


class SectionOutput(EvidenceContractModel):
    proposition: str = Field(min_length=1)
    output_type: OutputType
    required_evidence_level: RequiredEvidenceLevel


class EvidenceUse(EvidenceContractModel):
    evidence_use_id: str = Field(min_length=1)
    section_id: str = Field(min_length=1)
    claim_id: str = Field(min_length=1)
    sentence_id: str = Field(min_length=1)
    source_ids: tuple[str, ...] = Field(min_length=1)
    lineage_source_ids: tuple[str, ...] = ()
    citation_source_ids: tuple[str, ...] = ()
    citation_required: bool = False
    claim_role: ClaimRole
    evidence_kind: EvidenceKind
    verification_status: VerificationStatus
    support_locator: SupportLocator = Field(default_factory=SupportLocator)
    supported_content: str = ""
    author_inference: str | None = None

    @model_validator(mode="after")
    def validate_role(self) -> EvidenceUse:
        if len(self.source_ids) != len(set(self.source_ids)):
            raise ValueError("evidence use contains duplicate source IDs")
        if len(self.lineage_source_ids) != len(set(self.lineage_source_ids)):
            raise ValueError("evidence use contains duplicate lineage source IDs")
        if len(self.citation_source_ids) != len(set(self.citation_source_ids)):
            raise ValueError("evidence use contains duplicate citation source IDs")
        if self.claim_role == "author_inference" and not self.author_inference:
            raise ValueError("author_inference evidence use requires the authored inference")
        if self.claim_role != "author_inference" and self.author_inference is not None:
            raise ValueError("only author_inference may carry author_inference text")
        return self


class AuthoredClaim(EvidenceContractModel):
    claim_id: str = Field(min_length=1)
    claim_role: ClaimRole
    proposition: str = Field(min_length=1)
    derived_from_evidence_use_ids: tuple[str, ...] = ()
    verification_status: VerificationStatus

    @model_validator(mode="after")
    def validate_derivation(self) -> AuthoredClaim:
        if self.claim_role == "author_inference" and not self.derived_from_evidence_use_ids:
            raise ValueError("author inference requires at least one evidence-use derivation")
        if self.claim_role != "author_inference" and self.derived_from_evidence_use_ids:
            raise ValueError("only author inference may declare evidence-use derivations")
        return self


class HumanReviewItem(EvidenceContractModel):
    review_item_id: str = Field(min_length=1)
    section_id: str = Field(min_length=1)
    sentence_id: str | None = None
    claim_id: str | None = None
    category: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    source_ids: tuple[str, ...] = ()
    recommended_check: str = Field(min_length=1)
    blocks_render: bool = False
    status: Literal["open", "resolved", "dismissed"] = "open"


class RepairOperation(EvidenceContractModel):
    operation: RepairOperationKind
    target_sentence_ids: tuple[str, ...] = Field(min_length=1)
    forbidden_old_text: tuple[str, ...] = ()
    required_result: str = Field(min_length=1)
    preserve_source_ids: tuple[str, ...] = ()


class AuditFindingV31(EvidenceContractModel):
    finding_id: str = Field(min_length=1)
    audit_version: Literal["v31.1"] = AUDIT_VERSION
    manuscript_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    section_id: str | None = None
    target_ids: tuple[str, ...] = Field(min_length=1)
    category: str = Field(min_length=1)
    disposition: FindingDisposition
    severity: Literal["info", "review", "minor", "major", "critical"]
    description: str = Field(min_length=1)
    source_ids: tuple[str, ...] = ()
    repair_operations: tuple[RepairOperation, ...] = ()
    parent_finding_id: str | None = None

    @model_validator(mode="after")
    def validate_disposition(self) -> AuditFindingV31:
        if self.disposition == "auto_repair" and not self.repair_operations:
            raise ValueError("auto_repair finding requires an explicit repair operation")
        if self.disposition != "auto_repair" and self.repair_operations:
            raise ValueError("only auto_repair findings may carry repair operations")
        return self


class AuditBundle(EvidenceContractModel):
    schema_version: Literal["31.1"] = SCHEMA_VERSION
    audit_version: Literal["v31.1"] = AUDIT_VERSION
    manuscript_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    audited_section_ids: tuple[str, ...] = Field(min_length=1)
    clean_section_ids: tuple[str, ...] = ()
    findings: tuple[AuditFindingV31, ...] = ()

    @model_validator(mode="after")
    def validate_coverage(self) -> AuditBundle:
        if len(self.audited_section_ids) != len(set(self.audited_section_ids)):
            raise ValueError("audit contains duplicate section IDs")
        if not set(self.clean_section_ids).issubset(self.audited_section_ids):
            raise ValueError("clean section is outside the audit target")
        finding_ids = [item.finding_id for item in self.findings]
        if len(finding_ids) != len(set(finding_ids)):
            raise ValueError("audit contains duplicate finding IDs")
        for finding in self.findings:
            if finding.manuscript_hash != self.manuscript_hash:
                raise ValueError("finding is bound to a different manuscript hash")
            if finding.section_id and finding.section_id not in self.audited_section_ids:
                raise ValueError("finding references a section outside the audit target")
        return self


class FindingClassification(EvidenceContractModel):
    auto_repair: tuple[AuditFindingV31, ...] = ()
    human_review: tuple[AuditFindingV31, ...] = ()
    blocking: tuple[AuditFindingV31, ...] = ()
    review_items: tuple[HumanReviewItem, ...] = ()

    @model_validator(mode="after")
    def validate_partition(self) -> FindingClassification:
        groups = [*self.auto_repair, *self.human_review, *self.blocking]
        identifiers = [item.finding_id for item in groups]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("a finding was routed to more than one disposition")
        if any(item.disposition != "auto_repair" for item in self.auto_repair):
            raise ValueError("repair input contains a non-auto-repair finding")
        if any(item.disposition != "human_review" for item in self.human_review):
            raise ValueError("human-review partition contains another disposition")
        if any(item.disposition != "blocking" for item in self.blocking):
            raise ValueError("blocking partition contains another disposition")
        return self


class RepairResolutionProof(EvidenceContractModel):
    finding_id: str
    resolved: bool
    checked_sentence_ids: tuple[str, ...]
    absent_forbidden_text: tuple[str, ...]
    preserved_source_ids: tuple[str, ...]
    retired_source_ids_for_review: tuple[str, ...] = ()


class EvidenceSyncResult(EvidenceContractModel):
    evidence_uses: tuple[EvidenceUse, ...]
    removed_evidence_use_ids: tuple[str, ...]
    sentence_id_map: dict[str, str]


class DraftSectionState(EvidenceContractModel):
    section_id: str = Field(min_length=1)
    sentence_ids: tuple[str, ...] = Field(min_length=1)
    text: str = Field(min_length=1)


class FootnoteLink(EvidenceContractModel):
    footnote_id: str = Field(min_length=1)
    sentence_id: str = Field(min_length=1)
    evidence_use_ids: tuple[str, ...] = Field(min_length=1)
    bibliography_entry_ids: tuple[str, ...] = Field(min_length=1)


class BibliographyLink(EvidenceContractModel):
    bibliography_entry_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)


class AuthoringCheckpoint(EvidenceContractModel):
    """Durable boundary payload used by checkpoint/resume contract tests."""

    schema_version: Literal["31.1"] = SCHEMA_VERSION
    fixed_assets: FixedAssets
    section_outputs: tuple[SectionOutput, ...]
    draft_sections: tuple[DraftSectionState, ...]
    evidence_uses: tuple[EvidenceUse, ...]
    authored_claims: tuple[AuthoredClaim, ...] = ()
    audit_bundle: AuditBundle | None = None
    review_items: tuple[HumanReviewItem, ...] = ()
    footnote_links: tuple[FootnoteLink, ...] = ()
    bibliography_links: tuple[BibliographyLink, ...] = ()

    @model_validator(mode="after")
    def validate_references(self) -> AuthoringCheckpoint:
        section_ids = {item.section_id for item in self.draft_sections}
        sentence_ids = {
            sentence_id
            for section in self.draft_sections
            for sentence_id in section.sentence_ids
        }
        source_ids = {item.source_id for item in self.fixed_assets.sources}
        allowed_source_ids = set(self.fixed_assets.allowed_source_ids)
        fixed_by_id = {item.source_id: item for item in self.fixed_assets.sources}
        evidence_use_ids = [item.evidence_use_id for item in self.evidence_uses]
        if len(evidence_use_ids) != len(set(evidence_use_ids)):
            raise ValueError("checkpoint contains duplicate evidence-use IDs")
        for item in self.evidence_uses:
            if item.section_id not in section_ids or item.sentence_id not in sentence_ids:
                raise ValueError("evidence use references a missing section or sentence")
            if not set(item.source_ids).issubset(allowed_source_ids):
                raise ValueError("evidence use references an unknown or disallowed source")
            if not set(item.citation_source_ids).issubset(allowed_source_ids):
                raise ValueError("evidence use references an unknown or disallowed citation")
            if any(
                fixed_by_id[source_id].evidence_kind != "bibliographic_record"
                for source_id in item.citation_source_ids
            ):
                raise ValueError("evidence use citation is not a bibliographic record")
            if item.citation_required and not item.citation_source_ids:
                raise ValueError("evidence use requires a canonical citation source")
            unsupported = [
                source_id
                for source_id in item.source_ids
                if item.claim_role
                not in fixed_by_id[source_id].support_capabilities.claim_roles
            ]
            if unsupported:
                raise ValueError("evidence use exceeds a source support capability")
        for item in self.review_items:
            if item.section_id != "document" and item.section_id not in section_ids:
                raise ValueError("review item references an unknown section")
            if item.sentence_id and item.sentence_id not in sentence_ids:
                raise ValueError("review item references an unknown sentence")
            if not set(item.source_ids).issubset(source_ids):
                raise ValueError("review item references an unknown source")
        evidence_use_id_set = set(evidence_use_ids)
        bibliography_ids = {
            item.bibliography_entry_id for item in self.bibliography_links
        }
        for item in self.footnote_links:
            if item.sentence_id not in sentence_ids:
                raise ValueError("footnote references an unknown sentence")
            if not set(item.evidence_use_ids).issubset(evidence_use_id_set):
                raise ValueError("footnote references an unknown evidence use")
            if not set(item.bibliography_entry_ids).issubset(bibliography_ids):
                raise ValueError("footnote references an unknown bibliography entry")
        for item in self.bibliography_links:
            if item.source_id not in source_ids:
                raise ValueError("bibliography entry references an unknown source")
        validate_author_inference_derivations(
            list(self.authored_claims), list(self.evidence_uses)
        )
        return self


class ReviewReport(EvidenceContractModel):
    schema_version: Literal["31.1"] = SCHEMA_VERSION
    manuscript_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_status: Literal["completed", "completed_with_review", "failed"]
    review_items: tuple[HumanReviewItem, ...]
    blocking_findings: tuple[AuditFindingV31, ...] = ()
    argument_graph_validation_findings: tuple[dict[str, Any], ...] = ()


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def fixed_asset_hash(
    sources: tuple[FixedSourceAsset, ...] | list[FixedSourceAsset],
    allowed_source_ids: tuple[str, ...] | list[str],
) -> str:
    return canonical_hash(
        {
            "schema_version": SCHEMA_VERSION,
            "allowed_source_ids": list(allowed_source_ids),
            "sources": [item.model_dump(mode="json") for item in sources],
        }
    )


def _content_hash(item: dict[str, Any]) -> str:
    content = (
        item.get("text")
        or item.get("content")
        or item.get("source_text")
        or item.get("title")
        or item
    )
    return canonical_hash(content)


def _evidence_kind(item: dict[str, Any], *, memo: bool = False) -> EvidenceKind:
    if memo:
        return "memo_summary"
    kind = str(item.get("evidence_kind") or item.get("retrieval_path") or "")
    if kind in {"rag_excerpt", "memo_summary", "bibliographic_record", "full_text"}:
        return cast(EvidenceKind, kind)
    if item.get("full_text") or item.get("document_text"):
        return "full_text"
    provenance = dict(item.get("provenance") or {})
    if provenance.get("chunk_ids") or item.get("chunk_ids") or item.get("source_span_id"):
        return "rag_excerpt"
    return "bibliographic_record"


def _verification_status(item: dict[str, Any], *, memo: bool = False) -> VerificationStatus:
    raw = str(
        item.get("verification_status")
        or dict(item.get("provenance") or {}).get("verification_status")
        or ""
    )
    if raw in {"verified", "corroborated", "accepted", "supported"}:
        return "verified"
    if raw in {"insufficient", "rejected", "unsupported"}:
        return "insufficient"
    if memo:
        return "review_required"
    support_audits = [
        audit
        for audit in dict(item.get("provenance") or {}).get("support_audits") or []
        if isinstance(audit, dict)
    ]
    return (
        "verified"
        if any(audit.get("host_grounded") is True for audit in support_audits)
        else "review_required"
    )


def build_fixed_assets(context: dict[str, Any]) -> FixedAssets:
    """Freeze memo/RAG identity separately from each source's support strength."""

    records: list[FixedSourceAsset] = []
    seen: set[str] = set()
    inputs: list[tuple[dict[str, Any], bool]] = [
        *[
            (dict(item), False)
            for item in context.get("evidence_context") or []
            if isinstance(item, dict)
        ],
        *[
            (dict(item), True)
            for item in context.get("propositions") or []
            if isinstance(item, dict)
        ],
    ]
    for item, memo in inputs:
        source_id = str(item.get("id") or item.get("proposition_id") or "").strip()
        if not source_id or source_id in seen:
            continue
        seen.add(source_id)
        kind = _evidence_kind(item, memo=memo)
        status = _verification_status(item, memo=memo)
        provenance = dict(item.get("provenance") or {})
        chunk_ids = tuple(
            str(value)
            for value in (
                item.get("chunk_ids")
                or provenance.get("chunk_ids")
                or ([item["source_span_id"]] if item.get("source_span_id") else [])
            )
            if str(value)
        )
        roles: tuple[ClaimRole, ...]
        if memo:
            roles = ("memo_supported", "author_inference", "contextual")
        elif kind == "bibliographic_record":
            roles = ("contextual",)
        else:
            roles = (
                "source_fact",
                "attributed_position",
                "author_inference",
                "contextual",
            )
        records.append(
            FixedSourceAsset(
                source_id=source_id,
                evidence_kind=kind,
                retrieval_path=("memo" if memo else kind),
                verification_status=status,
                support_locator=SupportLocator(
                    chunk_ids=chunk_ids,
                    page=item.get("page") or provenance.get("page"),
                    uri=item.get("url") or item.get("source_uri") or provenance.get("source_url"),
                ),
                support_capabilities=SupportCapability(
                    claim_roles=roles,
                    maximum_verification_status=status,
                    supports_direct_attribution=(
                        not memo and kind in {"rag_excerpt", "full_text"}
                    ),
                    supports_quotation=(not memo and kind == "full_text"),
                ),
                content_sha256=_content_hash(item),
            )
        )
    allowed = tuple(
        item.source_id
        for item in records
        if item.verification_status != "insufficient"
    )
    sources = tuple(records)
    return FixedAssets(
        allowed_source_ids=allowed,
        sources=sources,
        asset_hash=fixed_asset_hash(sources, allowed),
    )


def lock_fixed_assets(context: dict[str, Any]) -> FixedAssets:
    """Create the immutable inventory once and reject later source drift."""

    candidate = build_fixed_assets(context)
    existing_raw = context.get("fixed_assets")
    if existing_raw is None:
        return candidate
    existing = FixedAssets.model_validate(existing_raw)
    if existing.asset_hash != candidate.asset_hash:
        raise FukamiruError("fixed asset inventory changed after it was locked")
    return existing


def adapt_legacy_evidence_use(raw: dict[str, Any]) -> EvidenceUse:
    adapted = dict(raw)
    adapted.setdefault("claim_role", "contextual")
    adapted.setdefault("verification_status", "review_required")
    adapted.setdefault("evidence_kind", "bibliographic_record")
    adapted.setdefault("support_locator", {})
    adapted.setdefault("supported_content", "")
    adapted.setdefault("author_inference", None)
    return EvidenceUse.model_validate(adapted)


def project_argument_evidence_sources(
    context: dict[str, Any],
) -> dict[str, dict[str, tuple[str, ...]]]:
    """Keep graph lineage separate from locked sources and bibliography identity.

    A raw reference ID is a lineage pointer, not an independently licensed
    source. Only an explicit, unique annotation-citation bridge may supply its
    canonical bibliographic ID. Unknown graph IDs remain visible as lineage but
    never enter an EvidenceUse's support source IDs.
    """

    fixed = FixedAssets.model_validate(context["fixed_assets"])
    fixed_by_id = {item.source_id: item for item in fixed.sources}
    allowed = set(fixed.allowed_source_ids)
    citations_by_reference: dict[str, set[str]] = {}
    for raw in context.get("annotation_citation_evidence") or []:
        if not isinstance(raw, dict):
            continue
        reference_id = str(raw.get("reference_id") or "")
        citation_id = str(raw.get("id") or "")
        if (
            reference_id
            and citation_id in allowed
            and fixed_by_id[citation_id].evidence_kind == "bibliographic_record"
        ):
            citations_by_reference.setdefault(reference_id, set()).add(citation_id)

    projections: dict[str, dict[str, tuple[str, ...]]] = {}
    for raw_node in context.get("argument_nodes") or []:
        if not isinstance(raw_node, dict) or raw_node.get("node_type") != "evidence":
            continue
        evidence_id = str(raw_node.get("id") or "")
        if not evidence_id:
            continue
        provenance = dict(raw_node.get("provenance") or {})
        raw_ids = tuple(dict.fromkeys(
            str(value)
            for value in (
                provenance.get("source_ids")
                or ([raw_node["source_span_id"]] if raw_node.get("source_span_id") else [])
                or [evidence_id]
            )
            if str(value)
        ))
        fixed_ids = tuple(source_id for source_id in raw_ids if source_id in allowed)
        citation_ids: list[str] = []
        for source_id in raw_ids:
            if (
                source_id in allowed
                and fixed_by_id[source_id].evidence_kind == "bibliographic_record"
            ):
                citation_ids.append(source_id)
            mapped = citations_by_reference.get(source_id, set())
            if len(mapped) == 1:
                citation_ids.extend(mapped)
        projections[evidence_id] = {
            "lineage_source_ids": raw_ids,
            "fixed_source_ids": fixed_ids,
            "citation_source_ids": tuple(dict.fromkeys(citation_ids)),
            "lineage_only_source_ids": tuple(
                source_id for source_id in raw_ids if source_id not in allowed
            ),
        }
    return projections


def _draft_source_use(
    evidence_id: str,
    prose_treatment: str,
    projection: dict[str, tuple[str, ...]],
    fixed_by_id: dict[str, FixedSourceAsset],
) -> tuple[ClaimRole, tuple[str, ...], tuple[str, ...]]:
    available = projection["fixed_source_ids"]
    citations = projection["citation_source_ids"]

    def compatible(role: ClaimRole, candidates: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(
            source_id
            for source_id in dict.fromkeys(candidates)
            if role in fixed_by_id[source_id].support_capabilities.claim_roles
        )

    if prose_treatment == "externally_supported_fact":
        role: ClaimRole = "source_fact"
        sources = compatible(role, available)
    elif prose_treatment == "qualified_interpretation":
        role = "author_inference"
        sources = compatible(role, available)
    elif prose_treatment in {"attributed_close_reading", "attributed_user_statement"}:
        memo_sources = tuple(
            source_id for source_id in available
            if fixed_by_id[source_id].evidence_kind == "memo_summary"
        )
        role = "memo_supported" if memo_sources else "attributed_position"
        sources = compatible(role, memo_sources or available)
    elif prose_treatment == "attributed_scholarship":
        substantive_sources = compatible("attributed_position", available)
        if substantive_sources:
            role = "attributed_position"
            sources = substantive_sources
        else:
            # A bibliography identifies the work; it does not verify the
            # work's substantive claims. Preserve the attribution as context.
            role = "contextual"
            sources = compatible(role, (*available, *citations)) if citations else ()
    else:
        role = "contextual"
        sources = compatible(role, (*available, *citations))
    if not sources:
        if available:
            raise FukamiruError(
                f"draft evidence {evidence_id} does not support claim role {role}: "
                + ", ".join(available)
            )
        raise FukamiruError(
            f"draft evidence {evidence_id} has no permitted fixed source for "
            f"claim role {role}; lineage IDs: "
            + ", ".join(projection["lineage_source_ids"])
        )
    return role, sources, citations


def _sentences(text: str) -> list[str]:
    values = [
        item.strip()
        for item in re.split(r"(?<=[。！？!?])\s*|\n+", str(text or ""))
        if item.strip()
    ]
    return values or ([str(text).strip()] if str(text).strip() else [])


def _draft_sentence_records(
    section_id: str, paragraphs: list[dict[str, Any]]
) -> list[tuple[int, int, str, str, tuple[str, ...]]]:
    records: list[tuple[int, int, str, str, tuple[str, ...]]] = []
    for paragraph_index, paragraph in enumerate(paragraphs, start=1):
        sentences = _sentences(str(paragraph.get("text") or ""))
        sentence_ids = [
            f"{section_id}-P{paragraph_index:02d}-S{index:02d}"
            for index in range(1, len(sentences) + 1)
        ]
        paragraph["sentence_ids"] = sentence_ids
        evidence_ids = tuple(
            str(item) for item in paragraph.get("evidence_ids") or [] if str(item)
        )
        records.extend(
            (paragraph_index, index, sentence_id, sentence, evidence_ids)
            for index, (sentence_id, sentence) in enumerate(
                zip(sentence_ids, sentences, strict=True), start=1
            )
        )
    return records


def _resolve_draft_sentence_anchor(
    evidence_id: str,
    treatment: dict[str, Any],
    records: list[tuple[int, int, str, str, tuple[str, ...]]],
) -> tuple[int, int, str, str, tuple[str, ...]]:
    candidates = [record for record in records if evidence_id in record[4]]
    if not candidates:
        raise FukamiruError(
            f"draft evidence {evidence_id} has no sentence-level support anchor"
        )
    paragraph_index = treatment.get("support_paragraph_index")
    sentence_index = treatment.get("support_sentence_index")
    if paragraph_index is not None or sentence_index is not None:
        selected = [
            record for record in candidates
            if (paragraph_index is None or record[0] == paragraph_index)
            and (sentence_index is None or record[1] == sentence_index)
        ]
        if len(selected) != 1:
            raise FukamiruError(
                f"draft evidence {evidence_id} has invalid or ambiguous explicit "
                "sentence support indices"
            )
        return selected[0]
    hint = str(
        treatment.get("support_sentence")
        or treatment.get("attribution_sentence")
        or ""
    ).strip()
    matches = [
        record for record in candidates
        if hint and (hint == record[3] or hint in record[3] or record[3] in hint)
    ]
    if len(matches) == 1:
        return matches[0]
    if len(candidates) == 1:
        return candidates[0]
    raise FukamiruError(
        f"draft evidence {evidence_id} has ambiguous sentence-level support anchor; "
        "supply support_paragraph_index and support_sentence_index"
    )


def draft_support_anchor_problems(sections: list[dict[str, Any]]) -> list[str]:
    """Find ambiguous sentence bindings while section-local repair is still possible."""

    problems: list[str] = []
    for section in sections:
        section_id = str(section.get("section_id") or "")
        paragraphs = [
            dict(item) for item in section.get("paragraphs") or []
            if isinstance(item, dict)
        ]
        records = _draft_sentence_records(section_id, paragraphs)
        for treatment in section.get("evidence_treatment") or []:
            if not isinstance(treatment, dict):
                continue
            evidence_id = str(treatment.get("evidence_id") or "")
            try:
                _resolve_draft_sentence_anchor(evidence_id, treatment, records)
            except FukamiruError as exc:
                problems.append(f"section {section_id} {exc}")
    return problems


def compile_draft_evidence(
    context: dict[str, Any], sections: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[EvidenceUse], list[AuthoredClaim], list[HumanReviewItem]]:
    """Create sentence-addressable evidence uses without writing review prose to the draft."""

    fixed = FixedAssets.model_validate(context["fixed_assets"])
    fixed_by_id = {item.source_id: item for item in fixed.sources}
    projections = project_argument_evidence_sources(context)
    output_sections: list[dict[str, Any]] = []
    uses: list[EvidenceUse] = []
    review_items: list[HumanReviewItem] = []
    identity_occurrences: dict[str, int] = {}
    for section in sections:
        current = dict(section)
        section_id = str(current.get("section_id") or "")
        paragraphs = [
            dict(item)
            for item in current.get("paragraphs") or []
            if isinstance(item, dict)
        ]
        sentence_records = _draft_sentence_records(section_id, paragraphs)
        current["paragraphs"] = paragraphs
        treatments = [
            dict(item)
            for item in current.get("evidence_treatment") or []
            if isinstance(item, dict)
        ]
        for treatment in treatments:
            evidence_id = str(treatment.get("evidence_id") or "")
            projection = projections.get(evidence_id)
            if projection is None and evidence_id in fixed.allowed_source_ids:
                asset = fixed_by_id[evidence_id]
                projection = {
                    "lineage_source_ids": (evidence_id,),
                    "fixed_source_ids": (evidence_id,),
                    "citation_source_ids": (
                        (evidence_id,)
                        if asset.evidence_kind == "bibliographic_record"
                        else ()
                    ),
                    "lineage_only_source_ids": (),
                }
            if projection is None:
                raise FukamiruError(
                    f"draft evidence {evidence_id} is missing from the argument graph"
                )
            _, _, sentence_id, sentence_text, _ = _resolve_draft_sentence_anchor(
                evidence_id, treatment, sentence_records
            )
            prose_treatment = str(treatment.get("prose_treatment") or "")
            role, source_ids, citation_source_ids = _draft_source_use(
                evidence_id, prose_treatment, projection, fixed_by_id
            )
            weakest = (
                "review_required"
                if any(
                    fixed_by_id[source_id].verification_status == "review_required"
                    for source_id in source_ids
                )
                else "insufficient"
                if any(
                    fixed_by_id[source_id].verification_status == "insufficient"
                    for source_id in source_ids
                )
                else "verified"
            )
            asset = fixed_by_id[source_ids[0]]
            unsupported = [
                source_id
                for source_id in source_ids
                if role
                not in fixed_by_id[source_id].support_capabilities.claim_roles
            ]
            if unsupported:
                raise FukamiruError(
                    f"draft evidence {evidence_id} does not support claim role {role}: "
                    + ", ".join(sorted(unsupported))
                )
            identity = canonical_hash(
                {
                    "section_id": section_id,
                    "evidence_id": evidence_id,
                    "claim_id": str(treatment.get("claim_id") or "contextual"),
                    "source_ids": list(source_ids),
                    "claim_role": role,
                }
            )[:12]
            identity_occurrences[identity] = identity_occurrences.get(identity, 0) + 1
            evidence_use = EvidenceUse(
                evidence_use_id=(
                    f"EU-{section_id}-{identity}-{identity_occurrences[identity]:02d}"
                ),
                section_id=section_id,
                claim_id=str(treatment.get("claim_id") or "contextual"),
                sentence_id=sentence_id,
                source_ids=source_ids,
                lineage_source_ids=projection["lineage_source_ids"],
                citation_source_ids=citation_source_ids,
                citation_required=(
                    prose_treatment == "attributed_scholarship"
                    and bool(citation_source_ids)
                ),
                claim_role=role,
                evidence_kind=asset.evidence_kind,
                verification_status=cast(VerificationStatus, weakest),
                support_locator=asset.support_locator,
                supported_content=("" if role == "author_inference" else sentence_text),
                author_inference=(sentence_text if role == "author_inference" else None),
            )
            uses.append(evidence_use)
            if evidence_use.verification_status == "review_required":
                review_items.append(
                    HumanReviewItem(
                        review_item_id=f"HR-{evidence_use.evidence_use_id}",
                        section_id=section_id,
                        sentence_id=sentence_id,
                        claim_id=evidence_use.claim_id,
                        category="source_attribution",
                        reason="取得資料だけでは帰属範囲の原典確認が完了していない",
                        source_ids=source_ids,
                        recommended_check="原典の該当箇所とページを確認する",
                    )
                )
        output_sections.append(current)

    uses_by_claim: dict[str, list[EvidenceUse]] = {}
    for item in uses:
        uses_by_claim.setdefault(item.claim_id, []).append(item)
    claims = [
        AuthoredClaim(
            claim_id=claim_id,
            claim_role="author_inference",
            proposition=" ".join(
                item.author_inference or ""
                for item in claim_uses
                if item.claim_role == "author_inference"
            ).strip(),
            derived_from_evidence_use_ids=tuple(
                item.evidence_use_id for item in claim_uses
            ),
            verification_status=(
                "review_required"
                if any(item.verification_status == "review_required" for item in claim_uses)
                else "insufficient"
                if any(item.verification_status == "insufficient" for item in claim_uses)
                else "verified"
            ),
        )
        for claim_id, claim_uses in uses_by_claim.items()
        if any(item.claim_role == "author_inference" for item in claim_uses)
    ]
    return output_sections, uses, claims, review_items


_HUMAN_REVIEW_CATEGORY_PARTS = (
    "source_attribution",
    "over_attribution",
    "primary_source",
    "original_source",
    "passage_verification",
)
_BLOCKING_CATEGORY_PARTS = (
    "fabricated",
    "unknown_source",
    "missing_source",
    "missing_footnote",
    "missing_bibliography",
    "broken_reference",
    "json_reference",
)
_SEMANTIC_REVIEW_CATEGORIES = {
    "source_review_note_leak",
    "over_attribution",
    "evidence_mismatch",
    "audit_meta_prose",
    "claim_scope_overreach",
    "unwarranted_claim_scope",
    "unwarranted_thesis_link",
    "conclusion_synthesis_gap",
    "conclusion_overreach",
    "conclusion_overclaim",
    "roadmap_mismatch",
    "leaked_verification_vocabulary",
    "verification_language_leak",
}
_LOCAL_AUTO_REPAIR_CATEGORIES = {
    "redundancy", "duplicate_sentence", "reader_facing_internal_scaffold",
}


def infer_disposition(raw: dict[str, Any]) -> FindingDisposition:
    explicit = raw.get("disposition")
    category = str(raw.get("category") or raw.get("code") or raw.get("finding_type") or "")
    message = str(raw.get("description") or raw.get("message") or "")
    repair_scope = str(raw.get("repair_scope") or "")
    severity = str(raw.get("severity") or "")
    normalized = f"{category} {message}".casefold()
    if explicit == "blocking":
        return "blocking"
    if any(part in normalized for part in _BLOCKING_CATEGORY_PARTS):
        return "blocking"
    if category in _SEMANTIC_REVIEW_CATEGORIES:
        return "human_review"
    if explicit == "human_review":
        return "human_review"
    if severity == "critical" and repair_scope != "local_section":
        return "blocking"
    if (
        repair_scope == "user_input"
        or any(part in normalized for part in _HUMAN_REVIEW_CATEGORY_PARTS)
        or "原典" in message
    ):
        return "human_review"
    if (
        repair_scope == "local_section"
        and (category in _LOCAL_AUTO_REPAIR_CATEGORIES or explicit == "auto_repair")
    ):
        return "auto_repair"
    return "human_review"


def adapt_audit_finding(
    raw: dict[str, Any],
    *,
    manuscript_hash: str,
    ordinal: int,
    sentence_ids_by_section: dict[str, list[str]] | None = None,
) -> AuditFindingV31:
    sentence_ids_by_section = sentence_ids_by_section or {}
    category = str(raw.get("category") or raw.get("code") or raw.get("finding_type") or "other")
    description = str(raw.get("description") or raw.get("message") or category)
    section_id = str(raw.get("section_id") or raw.get("object_id") or "") or None
    explicit_targets = [str(item) for item in raw.get("target_ids") or [] if str(item)]
    generated_finding_id = "AF-" + canonical_hash(
        {
            "category": category,
            "description": description,
            "section_id": section_id,
            "target_ids": explicit_targets,
            "repair_scope": str(raw.get("repair_scope") or ""),
        }
    )[:16]
    finding_id = str(raw.get("finding_id") or raw.get("id") or generated_finding_id)
    targets = (
        explicit_targets
        or list(sentence_ids_by_section.get(section_id or "", []))
        or ([section_id] if section_id else [finding_id])
    )
    disposition = infer_disposition(raw)
    source_ids = tuple(str(item) for item in raw.get("source_ids") or [] if str(item))
    operations: tuple[RepairOperation, ...] = ()
    if disposition == "auto_repair":
        raw_operations = [
            item for item in raw.get("repair_operations") or [] if isinstance(item, dict)
        ]
        if raw_operations:
            operations = tuple(RepairOperation.model_validate(item) for item in raw_operations)
        else:
            sentence_targets = list(
                dict.fromkeys(
                    item
                    for target in targets
                    for item in sentence_ids_by_section.get(target, [])
                )
            )
            operations = (
                RepairOperation(
                    operation="replace",
                    target_sentence_ids=tuple(sentence_targets or targets),
                    forbidden_old_text=tuple(
                        str(item) for item in raw.get("forbidden_old_text") or [] if str(item)
                    ),
                    required_result=description,
                    preserve_source_ids=source_ids,
                ),
            )
    severity = str(raw.get("severity") or "review")
    if severity not in {"info", "review", "minor", "major", "critical"}:
        severity = "review"
    return AuditFindingV31(
        finding_id=finding_id,
        manuscript_hash=manuscript_hash,
        section_id=section_id,
        target_ids=tuple(targets),
        category=category,
        disposition=disposition,
        severity=cast(Any, severity),
        description=description,
        source_ids=source_ids,
        repair_operations=operations,
        parent_finding_id=(
            str(raw.get("parent_finding_id")) if raw.get("parent_finding_id") else None
        ),
    )


def classify_findings(findings: list[AuditFindingV31]) -> FindingClassification:
    auto = tuple(item for item in findings if item.disposition == "auto_repair")
    human = tuple(item for item in findings if item.disposition == "human_review")
    blocking = tuple(item for item in findings if item.disposition == "blocking")
    review_items = tuple(
        HumanReviewItem(
            review_item_id=f"HR-{item.finding_id}",
            section_id=item.section_id or "document",
            sentence_id=next(
                (target for target in item.target_ids if re.search(r"-P\d+-S\d+$", target)),
                None,
            ),
            category=item.category,
            reason=item.description,
            source_ids=item.source_ids,
            recommended_check=(
                "原典の該当箇所、ページ、帰属範囲を確認する"
                if item.source_ids
                else "対象箇所を人間が確認する"
            ),
        )
        for item in human
    )
    return FindingClassification(
        auto_repair=auto,
        human_review=human,
        blocking=blocking,
        review_items=review_items,
    )


def assert_repair_input(findings: list[dict[str, Any] | AuditFindingV31]) -> None:
    for raw in findings:
        disposition = (
            raw.disposition if isinstance(raw, AuditFindingV31) else raw.get("disposition")
        )
        if disposition != "auto_repair":
            raise FukamiruError("repair_failed_sections accepts auto_repair findings only")


def synchronize_evidence_uses(
    evidence_uses: list[EvidenceUse],
    *,
    current_sentence_ids: set[str],
    sentence_id_map: dict[str, str] | None = None,
    deleted_sentence_ids: set[str] | None = None,
    allowed_source_ids: set[str] | None = None,
) -> EvidenceSyncResult:
    sentence_id_map = sentence_id_map or {}
    deleted_sentence_ids = deleted_sentence_ids or set()
    synchronized: list[EvidenceUse] = []
    removed: list[str] = []
    for evidence_use in evidence_uses:
        if allowed_source_ids is not None and not set(evidence_use.source_ids).issubset(
            allowed_source_ids
        ):
            raise FukamiruError(
                f"evidence use {evidence_use.evidence_use_id} introduced an unknown source ID"
            )
        old_id = evidence_use.sentence_id
        if old_id in deleted_sentence_ids:
            removed.append(evidence_use.evidence_use_id)
            continue
        new_id = sentence_id_map.get(old_id, old_id)
        if new_id not in current_sentence_ids:
            raise FukamiruError(
                f"evidence use {evidence_use.evidence_use_id} references an unmapped sentence"
            )
        synchronized.append(evidence_use.model_copy(update={"sentence_id": new_id}))
    return EvidenceSyncResult(
        evidence_uses=tuple(synchronized),
        removed_evidence_use_ids=tuple(removed),
        sentence_id_map=dict(sentence_id_map),
    )


def validate_author_inference_derivations(
    claims: list[AuthoredClaim], evidence_uses: list[EvidenceUse]
) -> None:
    evidence_ids = {item.evidence_use_id for item in evidence_uses}
    for claim in claims:
        missing = set(claim.derived_from_evidence_use_ids) - evidence_ids
        if missing:
            raise FukamiruError(
                f"author inference {claim.claim_id} references unknown evidence uses: "
                + ", ".join(sorted(missing))
            )


def repair_fingerprint(findings: list[AuditFindingV31]) -> str:
    return canonical_hash(
        sorted(
            (
                item.finding_id,
                item.category,
                item.section_id or "",
                tuple(item.target_ids),
            )
            for item in findings
        )
    )


def validate_repair_history(
    history: list[dict[str, Any]],
    findings: list[AuditFindingV31],
    *,
    maximum_attempts: int,
) -> str:
    fingerprint = repair_fingerprint(findings)
    repeated = sum(str(item.get("fingerprint") or "") == fingerprint for item in history)
    if repeated >= maximum_attempts:
        raise FukamiruError("automatic repair limit reached for the same finding set")
    prior_sections: dict[str, set[str]] = {}
    for item in history:
        for finding_id, section_id in dict(item.get("finding_sections") or {}).items():
            prior_sections.setdefault(str(finding_id), set()).add(str(section_id))
    migrating = [
        item.finding_id
        for item in findings
        if item.section_id
        and prior_sections.get(item.finding_id)
        and item.section_id not in prior_sections[item.finding_id]
    ]
    if migrating:
        raise FukamiruError(
            "automatic repair stopped because findings moved across sections: "
            + ", ".join(sorted(migrating))
        )
    return fingerprint


def build_repair_resolution_proofs(
    findings: list[AuditFindingV31],
    sections: list[dict[str, Any]],
    evidence_uses: list[EvidenceUse],
    *,
    previous_sections: list[dict[str, Any]] | None = None,
    source_projections: dict[str, dict[str, Any]] | None = None,
) -> list[RepairResolutionProof]:
    sentence_text: dict[str, str] = {}
    for section in sections:
        for paragraph in section.get("paragraphs") or []:
            if not isinstance(paragraph, dict):
                continue
            texts = _sentences(str(paragraph.get("text") or ""))
            identifiers = [str(item) for item in paragraph.get("sentence_ids") or []]
            sentence_text.update(dict(zip(identifiers, texts, strict=False)))
    sources_by_sentence: dict[str, set[str]] = {}
    for item in evidence_uses:
        sources_by_sentence.setdefault(item.sentence_id, set()).update(item.source_ids)
    current_evidence_ids_by_section = {
        str(section.get("section_id") or ""): {
            str(evidence_id)
            for paragraph in section.get("paragraphs") or []
            if isinstance(paragraph, dict)
            for evidence_id in paragraph.get("evidence_ids") or []
        }
        for section in sections
    }
    retired_sources_by_section: dict[str, set[str]] = {}
    for section in previous_sections or []:
        section_id = str(section.get("section_id") or "")
        old_evidence_ids = {
            str(evidence_id)
            for paragraph in section.get("paragraphs") or []
            if isinstance(paragraph, dict)
            for evidence_id in paragraph.get("evidence_ids") or []
        }
        retired_evidence_ids = old_evidence_ids - current_evidence_ids_by_section.get(
            section_id, set()
        )
        retired_sources_by_section[section_id] = {
            str(source_id)
            for evidence_id in retired_evidence_ids
            for source_id in (
                (source_projections or {}).get(evidence_id, {}).get("fixed_source_ids")
                or ()
            )
        }
    proofs: list[RepairResolutionProof] = []
    for finding in findings:
        checked: list[str] = []
        absent: list[str] = []
        preserved: set[str] = set()
        retired_for_review: set[str] = set()
        resolved = True
        for operation in finding.repair_operations:
            checked.extend(operation.target_sentence_ids)
            target_present = any(
                sentence_id in sentence_text for sentence_id in operation.target_sentence_ids
            )
            if operation.operation == "delete":
                if not operation.forbidden_old_text and target_present:
                    resolved = False
            elif not target_present:
                resolved = False
            target_text = " ".join(
                sentence_text.get(sentence_id, "")
                for sentence_id in operation.target_sentence_ids
            )
            for forbidden in operation.forbidden_old_text:
                if forbidden and forbidden in target_text:
                    resolved = False
                else:
                    absent.append(forbidden)
            target_sources = {
                source_id
                for sentence_id in operation.target_sentence_ids
                for source_id in sources_by_sentence.get(sentence_id, set())
            }
            required_sources = set(operation.preserve_source_ids)
            if operation.operation != "delete":
                missing_sources = required_sources - target_sources
                # These audit categories frequently remove an obsolete
                # source-review sentence. Retire only sources tied to a
                # paragraph evidence link that actually disappeared, and
                # defer the semantic judgment to human review.
                allowed_retirement = (
                    retired_sources_by_section.get(finding.section_id or "", set())
                    if finding.category in {"source_review_note_leak", "evidence_mismatch"}
                    and operation.operation == "replace"
                    else set()
                )
                retired_for_review.update(missing_sources & allowed_retirement)
                if missing_sources - allowed_retirement:
                    resolved = False
            preserved.update(required_sources.intersection(target_sources))
        proofs.append(
            RepairResolutionProof(
                finding_id=finding.finding_id,
                resolved=resolved,
                checked_sentence_ids=tuple(dict.fromkeys(checked)),
                absent_forbidden_text=tuple(dict.fromkeys(absent)),
                preserved_source_ids=tuple(sorted(preserved)),
                retired_source_ids_for_review=tuple(sorted(retired_for_review)),
            )
        )
    return proofs


def result_status(
    *, review_items: list[HumanReviewItem], blocking_findings: list[AuditFindingV31]
) -> Literal["completed", "completed_with_review", "failed"]:
    if blocking_findings:
        return "failed"
    if any(item.status == "open" for item in review_items):
        return "completed_with_review"
    return "completed"
