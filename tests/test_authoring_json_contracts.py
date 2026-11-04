from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from fukamiru.authoring.evidence_contracts import (
    AuditBundle,
    AuditFindingV31,
    AuthoredClaim,
    AuthoringCheckpoint,
    BibliographyLink,
    DraftSectionState,
    EvidenceUse,
    FixedAssets,
    FootnoteLink,
    HumanReviewItem,
    ReviewReport,
    SectionOutput,
    adapt_legacy_evidence_use,
    build_fixed_assets,
    canonical_hash,
    lock_fixed_assets,
)
from fukamiru.errors import FukamiruError


def _fixed_assets() -> FixedAssets:
    return build_fixed_assets(
        {
            "evidence_context": [
                {
                    "id": "SRC-014",
                    "text": "雑誌媒体は作品を並置する。",
                    "source_span_id": "CHUNK-881",
                    "provenance": {
                        "support_audits": [{"host_grounded": True}],
                        "page": 12,
                    },
                }
            ],
            "propositions": [
                {
                    "id": "MEMO-001",
                    "text": "この並置が比較の枠を作る。",
                    "provenance": {"source": "memo"},
                }
            ],
        }
    )


def _checkpoint() -> AuthoringCheckpoint:
    fixed = _fixed_assets()
    evidence_use = EvidenceUse(
        evidence_use_id="EU-S005-003",
        section_id="S005",
        claim_id="C-S005-004",
        sentence_id="S005-P02-S03",
        source_ids=("SRC-014",),
        claim_role="attributed_position",
        evidence_kind="rag_excerpt",
        verification_status="review_required",
        support_locator={"chunk_ids": ["CHUNK-881"], "page": 12},
        supported_content="雑誌媒体は作品を並置する。",
    )
    inference_use = EvidenceUse(
        evidence_use_id="EU-S005-004",
        section_id="S005",
        claim_id="C-S005-005",
        sentence_id="S005-P02-S04",
        source_ids=("MEMO-001",),
        claim_role="author_inference",
        evidence_kind="memo_summary",
        verification_status="verified",
        supported_content="並置という観察",
        author_inference="この並置が比較の枠を作る。",
    )
    review_item = HumanReviewItem(
        review_item_id="HR-S005-001",
        section_id="S005",
        sentence_id="S005-P02-S03",
        claim_id="C-S005-004",
        category="source_attribution",
        reason="RAG断片だけでは著者への強い帰属を確定できない",
        source_ids=("SRC-014",),
        recommended_check="原典の該当箇所とページを確認する",
    )
    return AuthoringCheckpoint(
        fixed_assets=fixed,
        section_outputs=(
            SectionOutput(
                proposition="並置は比較の枠を形成する。",
                output_type="interpretive_result",
                required_evidence_level="traceable",
            ),
        ),
        draft_sections=(
            DraftSectionState(
                section_id="S005",
                sentence_ids=("S005-P02-S03", "S005-P02-S04"),
                text="雑誌媒体は作品を並置する。この並置が比較の枠を作る。",
            ),
        ),
        evidence_uses=(evidence_use, inference_use),
        authored_claims=(
            AuthoredClaim(
                claim_id="C-S005-005",
                claim_role="author_inference",
                proposition="この並置が比較の枠を作る。",
                derived_from_evidence_use_ids=("EU-S005-003", "EU-S005-004"),
                verification_status="verified",
            ),
        ),
        review_items=(review_item,),
        bibliography_links=(
            BibliographyLink(
                bibliography_entry_id="BIB-014",
                source_id="SRC-014",
            ),
        ),
        footnote_links=(
            FootnoteLink(
                footnote_id="FN-014",
                sentence_id="S005-P02-S03",
                evidence_use_ids=("EU-S005-003",),
                bibliography_entry_ids=("BIB-014",),
            ),
        ),
    )


def test_fixed_assets_separate_allow_list_from_support_capabilities():
    fixed = _fixed_assets()

    assert set(fixed.allowed_source_ids) == {"SRC-014", "MEMO-001"}
    source = next(item for item in fixed.sources if item.source_id == "SRC-014")
    assert source.support_capabilities.supports_direct_attribution is True
    memo = next(item for item in fixed.sources if item.source_id == "MEMO-001")
    assert memo.support_capabilities.supports_direct_attribution is False
    assert memo.verification_status == "review_required"


def test_fixed_asset_lock_rejects_source_inventory_drift():
    context = {
        "evidence_context": [{
            "id": "SRC-1",
            "text": "固定資料",
            "verification_status": "verified",
        }],
    }
    fixed = lock_fixed_assets(context)

    with pytest.raises(FukamiruError, match="changed after it was locked"):
        lock_fixed_assets({
            **context,
            "fixed_assets": fixed.model_dump(mode="json"),
            "evidence_context": [
                *context["evidence_context"],
                {
                    "id": "SRC-2",
                    "text": "後から追加した資料",
                    "verification_status": "verified",
                },
            ],
        })


def test_checkpoint_rejects_claim_role_beyond_source_capability():
    payload = _checkpoint().model_dump(mode="json")
    payload["evidence_uses"][1]["claim_role"] = "source_fact"
    payload["evidence_uses"][1]["author_inference"] = None
    payload["authored_claims"] = []

    with pytest.raises(ValidationError, match="support capability"):
        AuthoringCheckpoint.model_validate(payload)


def test_checkpoint_round_trip_preserves_all_v31_evidence_linkage(tmp_path: Path):
    checkpoint = _checkpoint()
    path = tmp_path / "checkpoint.json"
    path.write_text(checkpoint.model_dump_json(indent=2), encoding="utf-8")

    restored = AuthoringCheckpoint.model_validate_json(path.read_text(encoding="utf-8"))

    assert restored.model_dump(mode="json") == checkpoint.model_dump(mode="json")
    assert restored.schema_version == "31.1"
    assert restored.evidence_uses[0].verification_status == "review_required"
    assert restored.review_items[0].source_ids == ("SRC-014",)
    assert restored.footnote_links[0].bibliography_entry_ids == ("BIB-014",)


def test_review_report_round_trip_preserves_argument_graph_findings():
    report = ReviewReport(
        manuscript_hash="a" * 64,
        result_status="completed_with_review",
        review_items=(),
        argument_graph_validation_findings=(
            {
                "node_id": "build_argument_graph",
                "problems": ["missing_typed_warrant_path"],
                "candidate_graph": {"nodes": [], "edges": []},
            },
        ),
    )

    restored = ReviewReport.model_validate_json(report.model_dump_json())

    assert restored.argument_graph_validation_findings == (
        {
            "node_id": "build_argument_graph",
            "problems": ["missing_typed_warrant_path"],
            "candidate_graph": {"nodes": [], "edges": []},
        },
    )


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda value: value["evidence_uses"][0].update({"claim_role": "opinion"}), "claim_role"),
        (
            lambda value: value["evidence_uses"][0].update({"verification_status": "maybe"}),
            "verification_status",
        ),
        (lambda value: value.update({"unknown": True}), "extra_forbidden"),
    ],
)
def test_checkpoint_rejects_bad_enums_and_unknown_fields(mutation, match: str):
    payload = _checkpoint().model_dump(mode="json")
    mutation(payload)

    with pytest.raises(ValidationError, match=match):
        AuthoringCheckpoint.model_validate(payload)


@pytest.mark.parametrize(
    "field,value,match",
    [
        ("sentence_id", "S005-P99-S99", "missing section or sentence"),
        ("source_ids", ["SRC-NOT-FOUND"], "unknown or disallowed source"),
    ],
)
def test_checkpoint_rejects_dangling_evidence_references(field: str, value, match: str):
    payload = _checkpoint().model_dump(mode="json")
    payload["evidence_uses"][0][field] = value

    with pytest.raises(ValidationError, match=match):
        AuthoringCheckpoint.model_validate(payload)


def test_legacy_adapter_never_infers_verified():
    evidence_use = adapt_legacy_evidence_use(
        {
            "evidence_use_id": "EU-OLD-001",
            "section_id": "S005",
            "claim_id": "C-OLD",
            "sentence_id": "S005-P01-S01",
            "source_ids": ["SRC-014"],
        }
    )

    assert evidence_use.claim_role == "contextual"
    assert evidence_use.verification_status == "review_required"


def test_audit_bundle_covers_all_sections_and_is_bound_to_manuscript_hash():
    digest = canonical_hash("本文")
    finding = AuditFindingV31(
        finding_id="AF-S005-001",
        manuscript_hash=digest,
        section_id="S005",
        target_ids=("S005-P02-S03",),
        category="over_attribution",
        disposition="human_review",
        severity="review",
        description="原典確認が必要",
        source_ids=("SRC-014",),
    )
    bundle = AuditBundle(
        manuscript_hash=digest,
        audited_section_ids=("S005", "S014"),
        clean_section_ids=("S014",),
        findings=(finding,),
    )

    assert bundle.audit_version == "v31.1"
    assert bundle.findings[0].target_ids == ("S005-P02-S03",)


def test_phase_zero_fixture_preserves_four_known_failure_sections():
    path = Path(__file__).parent / "fixtures" / "evidence_review_cases.json"
    fixture = json.loads(path.read_text(encoding="utf-8"))

    assert fixture["schema_version"] == "31.1"
    assert [item["section_id"] for item in fixture["sections"]] == [
        "S005",
        "S014",
        "S015",
        "S018",
    ]
