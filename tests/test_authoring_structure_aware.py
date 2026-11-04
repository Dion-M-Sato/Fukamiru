"""Offline JSON handoff and role routing checks for authoring V31."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest

from fukamiru.authoring.codex import STAGE_GUIDANCE, STAGE_SCHEMAS, CodexAuthoringAdapter
from fukamiru.authoring.evidence_contracts import (
    AuditFindingV31,
    EvidenceUse,
    RepairOperation,
    build_fixed_assets,
    build_repair_resolution_proofs,
)
from fukamiru.authoring.render import recover_citation_mentions, render_canonical_html
from fukamiru.authoring.structure_aware import (
    body_section_output_problems,
    normalize_body_section_dependencies,
    normalize_claim_scope_basis_ids,
    normalize_conclusion_evidence_declaration,
    normalize_defense_obligation_references,
    normalize_defense_obligations_for_discourse_roles,
    normalize_v31_draft_references,
    restore_core_qualified_inferences,
    validate_claim_scopes,
    validate_conclusion_synthesis,
    validate_defense_obligations,
    validate_research_problem,
    validate_role_contracts,
)
from fukamiru.errors import FukamiruError, NonRetryableFukamiruError
from fukamiru.revisions import submission_validation
from fukamiru.revisions.coherence import ManuscriptCoherenceService
from fukamiru.worker import sentence_proofreading
from fukamiru.worker.runner import WorkerRunner
from fukamiru.workflow.templates import authoring_workflow, authoring_workflow_v30


def _json(value):
    return json.loads(json.dumps(value, ensure_ascii=False))


def test_v31_repair_proof_retires_only_removed_evidence_sources_for_human_review():
    old = [{"section_id": "sec", "paragraphs": [{
        "text": "残す主張。削除する資料整理。",
        "evidence_ids": ["ev-keep", "ev-retire"],
    }]}]
    current = [{"section_id": "sec", "paragraphs": [{
        "text": "残す主張。新しい解釈。",
        "sentence_ids": ["sec-P01-S01", "sec-P01-S02"],
        "evidence_ids": ["ev-keep"],
    }]}]
    use = EvidenceUse(
        evidence_use_id="use-keep", section_id="sec", claim_id="cl-1",
        sentence_id="sec-P01-S01", source_ids=("p-keep",),
        claim_role="memo_supported", evidence_kind="memo_summary",
        verification_status="verified",
    )

    def finding(category: str, sources: tuple[str, ...]) -> AuditFindingV31:
        return AuditFindingV31(
            finding_id="AF-test", manuscript_hash="0" * 64,
            section_id="sec", target_ids=("sec-P01-S01",),
            category=category, disposition="auto_repair", severity="major",
            description="資料整理を読者向けの説明に直す",
            source_ids=sources,
            repair_operations=(RepairOperation(
                operation="replace", target_sentence_ids=("sec-P01-S01",),
                required_result="本文の資料整理を除く",
                preserve_source_ids=sources,
            ),),
        )

    projections = {
        "ev-keep": {"fixed_source_ids": ("p-keep",)},
        "ev-retire": {"fixed_source_ids": ("p-retire",)},
    }
    proof = build_repair_resolution_proofs(
        [finding("source_review_note_leak", ("p-keep", "p-retire"))],
        current, [use], previous_sections=old, source_projections=projections,
    )[0]
    assert proof.resolved
    assert proof.preserved_source_ids == ("p-keep",)
    assert proof.retired_source_ids_for_review == ("p-retire",)

    unknown = build_repair_resolution_proofs(
        [finding("source_review_note_leak", ("p-keep", "p-unknown"))],
        current, [use], previous_sections=old, source_projections=projections,
    )[0]
    assert not unknown.resolved
    assert unknown.retired_source_ids_for_review == ()

    unrelated = build_repair_resolution_proofs(
        [finding("evidence_missing", ("p-keep", "p-retire"))],
        current, [use], previous_sections=old, source_projections=projections,
    )[0]
    assert not unrelated.resolved

    retained_link = build_repair_resolution_proofs(
        [finding("source_review_note_leak", ("p-keep", "p-retire"))],
        [{**current[0], "paragraphs": [{
            **current[0]["paragraphs"][0],
            "evidence_ids": ["ev-keep", "ev-retire"],
        }]}],
        [use], previous_sections=old, source_projections=projections,
    )[0]
    assert not retained_link.resolved


def test_v31_repair_scope_uses_section_id_and_keeps_other_sections_untouched():
    contracts = [
        {"section_id": "sec-intro", "article_role": "introduction"},
        {"section_id": "sec-body", "article_role": "body"},
        {"section_id": "sec-other", "article_role": "body"},
        {"section_id": "sec-conclusion", "article_role": "conclusion"},
    ]
    findings = [{
        "section_id": "sec-body", "category": "redundancy",
        "severity": "major", "repair_scope": "local_section",
    }]
    assert WorkerRunner._authoring_v31_repair_section_ids(contracts, findings) == {
        "sec-body", "sec-conclusion",
    }
    assert WorkerRunner._authoring_v31_findings_for_section(
        findings, "sec-other"
    ) == []
    assert WorkerRunner._authoring_v31_findings_for_section(
        findings, "sec-body"
    ) == findings


def test_v31_semantic_audit_reroute_does_not_generate_sections(monkeypatch):
    runner = object.__new__(WorkerRunner)
    context = {
        "authoring_recovery_lineage": {
            "policy": "authoring-v31-semantic-audit-review-reroute-v1"
        },
        "draft_sections": [{"section_id": "sec-body", "text": "元の本文"}],
        "audit_findings": [{"section_id": "sec-body"}],
    }
    monkeypatch.setattr(
        runner, "_authoring_v31_repair_composite_self_citations",
        lambda value: (value, []),
    )
    result = runner._authoring_repair_v31(SimpleNamespace(), context)
    assert result.context == context

    # The workflow invokes the dispatcher first. A stale repair fingerprint
    # must not reject this no-edit recovery before the V31 handler sees it.
    context["authoring_recovery_lineage"]["policy"] = (
        "authoring-v31-audit-join-dispatch-recovery-v1"
    )
    context["authoring_repair_fingerprint_history"] = [
        {"fingerprint": "same"} for _ in range(4)
    ]
    assert runner._authoring_repair(
        SimpleNamespace(workflow_version=31), context
    ).context == context


def _example(schema):
    kind = schema["type"]
    if isinstance(kind, list):
        kind = next(item for item in kind if item != "null")
    if kind == "object":
        return {key: _example(schema["properties"][key]) for key in schema["required"]}
    if kind == "array":
        return [_example(schema["items"]) for _ in range(schema.get("minItems", 0))]
    if kind == "string":
        return schema.get("enum", ["x"])[0]
    if kind == "integer":
        return schema.get("minimum", 0)
    raise AssertionError(f"Unsupported test schema type: {kind}")


def _handoff_fixture():
    question = {"text": "資料内の差異はどう説明できるか", "provisional_answer": "関係の差による"}
    propositions = [{"id": "p1"}]
    problem_raw = {
        "research_question": question["text"],
        "prior_positions": [],
        "problematisation": {
            "type": "source_internal",
            "description": "同一資料の二例が異なる",
            "evidence_ids": [],
            "proposition_ids": ["p1"],
        },
        "provisional_answer": question["provisional_answer"],
        "contribution_target": "差異の説明",
    }
    problem = validate_research_problem(
        _json(problem_raw), question=question, accepted_evidence=[], propositions=propositions
    )
    claims = [{"id": "c1"}]
    nodes = [
        {"id": "a1", "node_type": "claim", "claim_id": "c1"},
        {"id": "e1", "node_type": "evidence", "claim_id": "c1"},
        {"id": "w1", "node_type": "warrant", "claim_id": "c1"},
    ]
    scopes = validate_claim_scopes(
        _json(
            [
                {
                    "claim_id": "c1",
                    "proposition": "条件Aで差異が生じる",
                    "scope_conditions": ["条件A"],
                    "epistemic_status": "established",
                    "evidence_basis_ids": ["e1"],
                }
            ]
        ),
        claims=claims,
        argument_nodes=nodes,
    )
    defenses = validate_defense_obligations(
        _json([]), claims=claims, argument_nodes=nodes, accepted_evidence=[]
    )
    sections = [
        {"section_id": "s0", "article_role": "introduction", "counterarguments": []},
        *[
            {
                "section_id": sid,
                "article_role": "body",
                "claim_ids": ["c1"],
                "section_task": f"検討 {sid}",
                "section_output": f"確立 {sid}",
                "thesis_link": f"回答への寄与 {sid}",
                "required_input_section_ids": [] if sid == "s1" else ["s1"],
                "defense_obligation_ids": [],
                "counterarguments": [],
            }
            for sid in ("s1", "s2")
        ],
        {"section_id": "s3", "article_role": "conclusion", "counterarguments": []},
    ]
    intro = {
        "section_id": "s0",
        "genre_profile": "research article",
        "moves": [],
        "research_question": question["text"],
        "documented_prior_positions": [],
        "problematisation": "同一資料の二例が異なる",
        "provisional_answer": question["provisional_answer"],
        "contribution_target": "差異の説明",
        "roadmap_section_ids": ["s1", "s2"],
    }
    conclusion = {
        "section_id": "s3",
        "research_question": question["text"],
        "established_section_ids": ["s1", "s2"],
        "synthesis_goal": "二つの結果を統合",
        "contribution_target": "差異の説明",
        "scope": "条件A",
        "unresolved_questions": [],
    }
    intro_model, bodies, conclusion_model = validate_role_contracts(
        sections=_json(sections),
        introduction=_json(intro),
        conclusion=_json(conclusion),
        problem=problem,
        defenses=defenses,
    )
    synthesis = {
        "final_answer": "条件Aの下では関係の差が説明する",
        "synthesis": "二つの節の結果を合わせると条件Aが決定的になる",
        "contribution": "差異の説明",
        "established_section_ids": ["s1", "s2"],
        "scope": "条件A",
        "unresolved_questions": [],
        "new_evidence_ids": [],
        "new_evidence_rhetorical_reason": "",
    }
    return locals()


def test_global_introduction_claim_defense_may_resolve_in_relevant_body_section():
    fixture = _handoff_fixture()
    sections = deepcopy(fixture["sections"])
    sections[0]["claim_ids"] = ["global"]
    sections[2]["defense_obligation_ids"] = ["d1"]
    defenses = validate_defense_obligations(
        [{
            "id": "d1", "target_claim_id": "global",
            "challenge": "別の説明が成立する", "basis_ids": ["e-global"],
            "materiality": "中心仮説が弱まる", "response": "本文結果と照合する",
            "resolution_location": "same_section", "disposition": "body",
        }],
        claims=[{"id": "global"}, {"id": "c1"}],
        argument_nodes=[{"id": "e-global", "node_type": "evidence", "claim_id": "global"}],
        accepted_evidence=[],
    )
    _, bodies, _ = validate_role_contracts(
        sections=sections,
        introduction=fixture["intro"],
        conclusion=fixture["conclusion"],
        problem=fixture["problem"],
        defenses=defenses,
    )
    assert bodies[-1].defense_obligation_ids == ("d1",)
    sections[1]["defense_obligation_ids"] = ["d1"]
    sections[2]["defense_obligation_ids"] = []
    _, bodies, _ = validate_role_contracts(
        sections=sections,
        introduction=fixture["intro"],
        conclusion=fixture["conclusion"],
        problem=fixture["problem"],
        defenses=defenses,
    )
    assert bodies[0].defense_obligation_ids == ("d1",)
    sections[2]["defense_obligation_ids"] = ["d1"]
    with pytest.raises(FukamiruError, match="assigned exactly once"):
        validate_role_contracts(
            sections=sections,
            introduction=fixture["intro"],
            conclusion=fixture["conclusion"],
            problem=fixture["problem"],
            defenses=defenses,
        )


def test_scope_basis_normalization_removes_known_non_evidence_references():
    nodes = [
        {"id": "e1", "node_type": "evidence", "claim_id": "c1"},
        {"id": "q1", "node_type": "qualifier"},
        {"id": "c1", "node_type": "claim"},
    ]
    scopes, corrections = normalize_claim_scope_basis_ids(
        [{
            "claim_id": "c1", "proposition": "A", "epistemic_status": "provisional",
            "evidence_basis_ids": [
                "e1", "q1", "c1",
                "evidence:f750f9544f01b8269aae35a9",
                "unknown",
            ],
        }],
        argument_nodes=nodes,
        known_obligation_ids={"evidence:f750f9544f01b8269aae35a9"},
    )
    assert scopes[0]["evidence_basis_ids"] == ["e1", "unknown"]
    assert corrections[0]["removed_non_evidence_node_ids"] == ["q1", "c1"]
    assert corrections[0]["removed_non_graph_obligation_ids"] == [
        "evidence:f750f9544f01b8269aae35a9"
    ]
    with pytest.raises(FukamiruError, match="unknown evidence basis"):
        validate_claim_scopes(scopes, claims=[{"id": "c1"}], argument_nodes=nodes)
    scopes[0]["evidence_basis_ids"] = ["e1"]
    assert validate_claim_scopes(scopes, claims=[{"id": "c1"}], argument_nodes=nodes)


def test_scope_basis_allows_shared_citation_evidence_without_claim_id():
    scopes = [
        {
            "claim_id": claim_id, "proposition": claim_id,
            "epistemic_status": "provisional", "evidence_basis_ids": ["citation-1"],
        }
        for claim_id in ("c1", "c2")
    ]
    nodes = [{"id": "citation-1", "node_type": "evidence", "claim_id": None}]
    assert validate_claim_scopes(
        scopes, claims=[{"id": "c1"}, {"id": "c2"}], argument_nodes=nodes
    )


def test_core_qualified_inference_restoration_is_grounded_and_idempotent():
    claim = {"id": "c1-node", "node_type": "claim", "claim_id": "c1"}
    contract = {
        "claim_ids": ["c1"], "proposition_id": "p1",
        "argument_necessity": "core", "realization": "qualified_inference",
    }
    nodes, edges, restored = restore_core_qualified_inferences(
        argument_nodes=[claim], argument_edges=[],
        propositions=[{
            "id": "p1", "text": "資料に限定した推論",
            "provenance": {"source": "memo"},
        }],
        atomic_contracts=[contract],
    )
    assert len(restored) == 1
    assert nodes[1]["text"] == "資料に限定した推論"
    assert nodes[1]["provenance"] == {
        "source": "memo", "source_ids": ["p1"],
        "verification_status": "unverified",
    }
    assert edges[0]["target_id"] == "c1-node"
    same_nodes, same_edges, second = restore_core_qualified_inferences(
        argument_nodes=nodes, argument_edges=edges,
        propositions=[{"id": "p1", "text": "資料に限定した推論"}],
        atomic_contracts=[contract],
    )
    assert second == []
    assert same_nodes == nodes and same_edges == edges
    with pytest.raises(FukamiruError, match="lacks a locked proposition"):
        restore_core_qualified_inferences(
            argument_nodes=[claim], argument_edges=[], propositions=[],
            atomic_contracts=[contract],
        )

    supporting = {**contract, "argument_necessity": "supporting"}
    unchanged_nodes, unchanged_edges, omitted = restore_core_qualified_inferences(
        argument_nodes=[claim], argument_edges=[],
        propositions=[{
            "id": "p1", "text": "資料に限定した推論",
            "provenance": {"source": "memo"},
        }],
        atomic_contracts=[supporting],
    )
    assert omitted == []
    assert unchanged_nodes == [claim] and unchanged_edges == []
    selected_nodes, _, selected = restore_core_qualified_inferences(
        argument_nodes=[claim], argument_edges=[],
        propositions=[{
            "id": "p1", "text": "資料に限定した推論",
            "provenance": {"source": "memo"},
        }],
        atomic_contracts=[supporting],
        selected_supporting_claim_ids={"p1": ["c1"]},
    )
    assert selected_nodes[1]["claim_id"] == "c1"
    assert selected[0]["reason"] == "restore_selected_supporting_qualified_inference"

    attributed = {
        **supporting,
        "proposition_id": "p-memo",
        "realization": "attribute_to_memo",
    }
    memo_nodes, _, memo_restored = restore_core_qualified_inferences(
        argument_nodes=[claim],
        argument_edges=[],
        propositions=[{
            "id": "p-memo",
            "text": "記録上の役割の区別",
            "provenance": {"source": "memo"},
        }],
        atomic_contracts=[attributed],
        selected_supporting_claim_ids={"p-memo": ["c1"]},
    )
    assert memo_nodes[1]["id"] == "repair-memo-c1-p-memo"
    assert memo_restored[0]["reason"] == "restore_selected_memo_attribution"


def test_v31_evidence_gap_repair_exposes_locked_supporting_inference_to_section():
    context = {
        "research_problem": {"research_question": "問い"},
        "section_contracts": [{
            "section_id": "s1", "article_role": "body", "claim_ids": ["c1"],
            "evidence_ids": ["e-existing"],
        }],
        "audit_findings": [{
            "code": "evidence_treatment_gap", "severity": "major",
            "object_id": "s1",
        }],
        "argument_nodes": [
            {"id": "c1-node", "node_type": "claim", "claim_id": "c1"},
            {"id": "e-existing", "node_type": "evidence", "claim_id": "c1"},
        ],
        "argument_edges": [],
        "propositions": [{
            "id": "p-search", "text": "検索語とタグが表示候補を変える。",
            "provenance": {"source": "memo"},
        }],
        "authoring_atomic_evidence_contracts": [{
            "claim_ids": ["c1"], "proposition_id": "p-search",
            "argument_necessity": "supporting", "realization": "qualified_inference",
        }],
    }

    expanded, allowances = WorkerRunner._authoring_expand_evidence_gap_repair_context(
        context
    )

    assert len(allowances) == 1
    restored_id = allowances[0]["node_id"]
    assert allowances[0]["proposition_id"] == "p-search"
    assert any(item.get("id") == restored_id for item in expanded["argument_nodes"])
    projected = WorkerRunner._authoring_draft_contracts_with_scholarship(
        expanded, expanded["section_contracts"]
    )
    assert projected[0]["evidence_ids"] == ["e-existing", restored_id]
    localized = deepcopy(context)
    localized["audit_findings"][0]["code"] = "段落の証拠対応欠落"
    localized_expanded, localized_allowances = (
        WorkerRunner._authoring_expand_evidence_gap_repair_context(localized)
    )
    assert localized_allowances
    assert localized_expanded["evidence_gap_qualified_inference_restorations"]


def test_v31_evidence_mismatch_removes_dynamic_allowance_without_guessing_replacement():
    context = {
        "section_contracts": [{
            "section_id": "s1",
            "article_role": "body",
            "claim_ids": ["c1"],
            "evidence_ids": [],
        }],
        "audit_findings": [{
            "code": "evidence_mismatch",
            "severity": "major",
            "object_id": "s1",
            "message": (
                "検索結果とSNSの発見経路に bad-evidence を付けているが、"
                "別の命題を支える。"
            ),
        }],
        "argument_nodes": [
            {"id": "c1-node", "node_type": "claim", "claim_id": "c1"},
            {"id": "c2-node", "node_type": "claim", "claim_id": "c2"},
            {
                "id": "bad-evidence",
                "node_type": "evidence",
                "claim_id": "c1",
                "proposition_id": "p-bad",
            },
        ],
        "argument_edges": [],
        "propositions": [
            {
                "id": "p-bad",
                "text": "別の対象への評価",
                "provenance": {"source": "memo"},
            },
            {
                "id": "p-search",
                "text": "検索結果とSNSは作品を元の媒体から切り離す。",
                "provenance": {"source": "memo"},
            },
        ],
        "authoring_atomic_evidence_contracts": [
            {
                "claim_ids": ["c1"],
                "proposition_id": "p-bad",
                "argument_necessity": "supporting",
                "realization": "qualified_inference",
            },
            {
                "claim_ids": ["c2"],
                "proposition_id": "p-search",
                "argument_necessity": "supporting",
                "realization": "attribute_to_memo",
            },
        ],
        "authoring_repair_evidence_allowances": [{
            "section_id": "s1",
            "claim_id": "c1",
            "node_id": "bad-evidence",
            "proposition_id": "p-bad",
        }],
    }

    expanded, allowances = WorkerRunner._authoring_expand_evidence_gap_repair_context(
        context
    )

    assert all(
        item["node_id"] != "bad-evidence"
        for item in expanded["authoring_repair_evidence_allowances"]
    )
    assert allowances == []
    projected = WorkerRunner._authoring_draft_contracts_with_scholarship(
        expanded, expanded["section_contracts"]
    )
    assert projected[0]["evidence_ids"] == []


def test_v31_negative_only_citation_reduces_effective_evidence_minimum():
    context = {
        "audit_findings": [{
            "code": "negative_only_citation_purpose",
            "severity": "major",
            "object_id": "section-02",
            "message": (
                "不足を述べる文に b02-e-c0006-2 が対応づけられている。"
                "比較手順を直接示し、この文への証拠の対応づけを外す。"
            ),
        }],
        "argument_nodes": [
            {
                "id": evidence_id,
                "node_type": "evidence",
            }
            for evidence_id in (
                "b02-e-c0006-2",
                "b02-e-c0008-1",
                "b02-e-c0008-2",
                "b03-e0009a",
                "b03-e0009b",
            )
        ],
        "draft_sections": [{
            "section_id": "section-02",
            "realized_evidence_ids": [
                "b02-e-c0006-2",
                "b02-e-c0008-1",
                "b02-e-c0008-2",
                "b03-e0009a",
                "b03-e0009b",
            ],
        }],
    }

    eligible, minimum = WorkerRunner._authoring_effective_contract_evidence(
        context,
        "section-02",
        {
            "b02-e-c0006-2",
            "b02-e-c0008-1",
            "b02-e-c0008-2",
            "b03-e0009a",
            "b03-e0009b",
        },
        5,
    )

    assert "b02-e-c0006-2" not in eligible
    assert minimum == 4


def test_v31_audit_handoff_uses_effective_evidence_minimum_after_rejection():
    evidence_ids = [
        "b02-e-c0006-2",
        "b02-e-c0008-1",
        "b02-e-c0008-2",
        "b03-e0009a",
        "b03-e0009b",
    ]
    context = {
        "section_contracts": [{
            "section_id": "section-02",
            "article_role": "body",
            "claim_ids": ["c1"],
            "evidence_ids": evidence_ids,
            "development_expectations": {"minimum_evidence_items": 5},
        }],
        "argument_nodes": [
            {"id": evidence_id, "node_type": "evidence"}
            for evidence_id in evidence_ids
        ],
        "authoring_rejected_evidence_ids_by_section": {
            "section-02": ["b02-e-c0006-2"],
        },
        "draft_sections": [{
            "section_id": "section-02",
            "realized_evidence_ids": evidence_ids[1:],
        }],
    }

    projected = WorkerRunner._authoring_effective_audit_section_contracts(context)

    assert projected[0]["evidence_ids"] == evidence_ids[1:]
    assert (
        projected[0]["development_expectations"]["minimum_evidence_items"]
        == 4
    )


def test_v31_partial_evidence_mismatch_reanchors_instead_of_rejecting_sectionwide():
    context = {
        "argument_nodes": [
            {"id": "e-partial", "node_type": "evidence"},
            {"id": "e-rejected", "node_type": "evidence"},
        ],
        "audit_findings": [
            {
                "code": "evidence_mismatch",
                "severity": "major",
                "object_id": "s1",
                "message": (
                    "e-partial は第一の記述を支えるが人物の役割は支えない。"
                    "第一の文にだけ対応させる。"
                ),
            },
            {
                "code": "evidence_mismatch",
                "severity": "major",
                "object_id": "s2",
                "message": "e-rejected はこの命題を支えないため添付を外す。",
            },
        ],
    }

    rejected = WorkerRunner._authoring_rejected_evidence_ids_by_section(context)

    assert rejected.get("s1", set()) == set()
    assert rejected["s2"] == {"e-rejected"}


def test_v31_repair_drops_rejected_evidence_from_every_section_field():
    section = {
        "section_id": "section-02",
        "text": "比較する。[^rejected]",
        "evidence_ids": ["kept"],
        "realized_evidence_ids": ["kept", "rejected"],
        "paragraphs": [
            {
                "text": "比較する。[^rejected]",
                "evidence_ids": ["kept", "rejected"],
            }
        ],
        "evidence_treatment": [
            {"evidence_id": "kept", "support_sentence": "比較する。"},
            {"evidence_id": "rejected", "support_sentence": "旧文。"},
        ],
    }

    normalized, corrections = (
        WorkerRunner._authoring_drop_rejected_evidence_metadata(
            section, {"rejected", "unused-rejection"}
        )
    )

    assert normalized["evidence_ids"] == ["kept"]
    assert normalized["realized_evidence_ids"] == ["kept"]
    assert normalized["paragraphs"][0]["evidence_ids"] == ["kept"]
    assert normalized["evidence_treatment"] == [
        {"evidence_id": "kept", "support_sentence": "比較する。"}
    ]
    assert "rejected" not in normalized["text"]
    assert "rejected" not in normalized["paragraphs"][0]["text"]
    assert corrections[0]["policy"] == "audit-rejected-evidence-metadata-v1"
    assert corrections[0]["evidence_ids"] == ["rejected"]


def test_v31_allowlist_violation_revokes_prior_section_repair_allowances():
    context = {
        "audit_findings": [{
            "code": "evidence_allowlist_violation",
            "severity": "major",
            "object_id": "section-conclusion",
        }],
        "authoring_repair_evidence_allowances": [
            {
                "section_id": "section-conclusion",
                "claim_id": "c1",
                "node_id": "repair-e-conclusion",
            },
            {
                "section_id": "section-01",
                "claim_id": "c2",
                "node_id": "repair-e-body",
            },
        ],
    }

    expanded, allowances = WorkerRunner._authoring_expand_evidence_gap_repair_context(
        context
    )

    assert allowances == []
    assert expanded["authoring_repair_evidence_allowances"] == [{
        "section_id": "section-01",
        "claim_id": "c2",
        "node_id": "repair-e-body",
    }]


def test_v31_evidence_gap_can_expose_cross_claim_memo_without_stale_allowances():
    context = {
        "section_contracts": [{
            "section_id": "s1",
            "article_role": "body",
            "claim_ids": ["c1"],
            "evidence_ids": [],
        }],
        "draft_sections": [{
            "section_id": "s1",
            "evidence_ids": [],
            "paragraphs": [{"evidence_ids": [], "text": "検索経路を調べる。"}],
        }],
        "audit_findings": [{
            "code": "evidence_treatment_gap",
            "severity": "major",
            "object_id": "s1",
            "message": "検索結果とSNSの発見経路を支える記録が必要。",
        }],
        "argument_nodes": [
            {"id": "c1-node", "node_type": "claim", "claim_id": "c1"},
            {"id": "c2-node", "node_type": "claim", "claim_id": "c2"},
        ],
        "argument_edges": [],
        "propositions": [
            {
                "id": "p-search",
                "text": "検索結果とSNSは作品を元の媒体から切り離す。",
                "provenance": {"source": "memo"},
            },
            {
                "id": "p-draft",
                "text": "草稿では検索結果とSNSを同じ機能として扱っている。",
                "provenance": {"source": "memo"},
            },
        ],
        "authoring_atomic_evidence_contracts": [
            {
                "claim_ids": ["c2"],
                "proposition_id": "p-search",
                "argument_necessity": "supporting",
                "realization": "attribute_to_memo",
            },
            {
                "claim_ids": ["c1"],
                "proposition_id": "p-draft",
                "argument_necessity": "supporting",
                "realization": "qualified_inference",
            },
        ],
        "authoring_repair_evidence_allowances": [{
            "section_id": "s1",
            "claim_id": "c1",
            "node_id": "unused-old-allowance",
            "proposition_id": "p-old",
        }],
    }

    expanded, allowances = WorkerRunner._authoring_expand_evidence_gap_repair_context(
        context
    )

    assert len(allowances) == 1
    assert allowances[0]["section_id"] == "s1"
    assert allowances[0]["claim_id"] == "c2"
    assert allowances[0]["proposition_id"] == "p-search"
    assert allowances[0]["node_id"] == "repair-memo-c2-p-search"
    assert all(
        item["node_id"] != "unused-old-allowance"
        for item in expanded["authoring_repair_evidence_allowances"]
    )
    projected = WorkerRunner._authoring_draft_contracts_with_scholarship(
        expanded, expanded["section_contracts"]
    )
    assert projected[0]["evidence_ids"] == ["repair-memo-c2-p-search"]


def test_v31_repair_retains_materialized_allowance_until_targeted_section_uses_it():
    allowance = {
        "section_id": "s1",
        "claim_id": "c1",
        "node_id": "repair-memo-c1-p-attribution",
        "proposition_id": "p-attribution",
    }
    context = {
        "research_problem": {"research_question": "問い"},
        "section_contracts": [{
            "section_id": "s1",
            "article_role": "body",
            "claim_ids": ["c1"],
            "evidence_ids": [],
            "warrant_ids": ["w1"],
            "development_expectations": {
                "required_argument_moves": [],
                "minimum_evidence_items": 0,
                "minimum_concrete_examples": 0,
            },
        }],
        "draft_sections": [{
            "section_id": "s1",
            "article_role": "body",
            "claim_ids": ["c1"],
            "evidence_ids": [],
            "realized_evidence_ids": [],
            "realized_warrant_ids": ["w1"],
            "paragraphs": [{"evidence_ids": [], "text": "帰属を補う必要がある。"}],
        }],
        "audit_findings": [
            {
                "code": "structure_invalid",
                "severity": "major",
                "object_id": "s1",
                "message": "筆者の既刊論文への帰属を明記する。",
            },
            {
                "code": "evidence_treatment_gap",
                "severity": "major",
                "object_id": "s2",
                "message": "別節の根拠を補う。",
            },
        ],
        "argument_nodes": [
            {"id": "c1-node", "node_type": "claim", "claim_id": "c1"},
            {"id": "w1", "node_type": "warrant", "claim_id": "c1"},
            {
                "id": allowance["node_id"],
                "node_type": "evidence",
                "claim_id": "c1",
                "proposition_id": "p-attribution",
                "provenance": {
                    "source": "memo",
                    "verification_status": "unverified",
                },
            },
        ],
        "argument_edges": [],
        "propositions": [],
        "authoring_atomic_evidence_contracts": [],
        "authoring_repair_evidence_allowances": [allowance],
    }

    expanded, added = WorkerRunner._authoring_expand_evidence_gap_repair_context(
        context
    )

    assert added == []
    assert expanded["authoring_repair_evidence_allowances"] == [allowance]
    repaired = {
        **context["draft_sections"][0],
        "evidence_ids": [allowance["node_id"]],
        "realized_evidence_ids": [allowance["node_id"]],
        "paragraphs": [{
            "evidence_ids": [allowance["node_id"]],
            "text": "筆者が既刊論文で述べたように、帰属を明記する。",
        }],
        "evidence_treatment": [{
            "evidence_id": allowance["node_id"],
            "basis": "memo_close_reading",
            "verification_status": "unverified",
            "prose_treatment": "attributed_scholarship",
        }],
    }
    problems = WorkerRunner._authoring_repair_local_contract_violations(
        expanded,
        role="body",
        generated_section=repaired,
        section_by_id={"s1": context["draft_sections"][0]},
        contracts=context["section_contracts"],
    )

    assert not any("non-contract evidence" in problem for problem in problems)


def test_defense_reference_normalization_preserves_basis_and_fails_unknown_ids():
    nodes = [
        {"id": "ce1", "node_type": "counterexample", "claim_id": "c1"},
        {"id": "ce2", "node_type": "counterexample", "claim_id": "c2"},
        {"id": "w1", "node_type": "warrant", "claim_id": "c1"},
    ]
    raw = [{
        "id": "o1", "target_claim_id": "c1", "challenge": "Challenge",
        "basis_ids": ["ce2", "w1"], "materiality": "Material",
        "response": "Narrow scope", "resolution_location": "scope_absorbed",
        "disposition": "body", "source_argument_node_ids": ["ce1", "ce2", "w1"],
    }]
    normalized, changes = normalize_defense_obligation_references(
        raw, argument_nodes=nodes
    )
    assert normalized[0]["basis_ids"] == ["ce2", "w1"]
    assert normalized[0]["source_argument_node_ids"] == ["ce1"]
    assert normalized[0]["disposition"] == "scope_absorbed"
    assert len(changes) == 2
    assert validate_defense_obligations(
        normalized, claims=[{"id": "c1"}, {"id": "c2"}],
        argument_nodes=nodes, accepted_evidence=[],
    )
    raw[0]["source_argument_node_ids"] = ["unknown"]
    normalized, _ = normalize_defense_obligation_references(raw, argument_nodes=nodes)
    with pytest.raises(FukamiruError, match="counterevidence for another claim"):
        validate_defense_obligations(
            normalized, claims=[{"id": "c1"}, {"id": "c2"}],
            argument_nodes=nodes, accepted_evidence=[],
        )


def test_body_role_normalization_removes_only_nonbody_dependencies_and_moves():
    sections = [
        {"section_id": "intro", "article_role": "introduction",
         "counterarguments": ["unassigned"]},
        {"section_id": "body-1", "article_role": "body",
         "required_input_section_ids": ["intro"],
         "defense_obligation_ids": ["internal"],
         "counterarguments": ["unassigned"]},
        {"section_id": "body-2", "article_role": "body",
         "required_input_section_ids": ["body-1"],
         "defense_obligation_ids": ["material"],
         "counterarguments": ["material challenge"]},
        {"section_id": "conclusion", "article_role": "conclusion",
         "counterarguments": ["unassigned"]},
    ]
    normalized, corrections = normalize_body_section_dependencies(
        sections,
        defenses=[
            {"id": "internal", "disposition": "scope_absorbed"},
            {"id": "material", "disposition": "body"},
        ],
    )
    assert normalized[1]["required_input_section_ids"] == []
    assert normalized[1]["defense_obligation_ids"] == []
    assert normalized[1]["counterarguments"] == []
    assert normalized[2]["required_input_section_ids"] == ["body-1"]
    assert normalized[2]["defense_obligation_ids"] == ["material"]
    assert normalized[2]["counterarguments"] == ["material challenge"]
    assert normalized[0]["counterarguments"] == []
    assert normalized[3]["counterarguments"] == []
    assert len(corrections) == 5


def test_body_role_normalization_places_material_defenses_with_target_claims():
    sections = [
        {
            "section_id": "intro",
            "article_role": "introduction",
            "claim_ids": ["c0"],
            "defense_obligation_ids": [],
            "counterarguments": [],
        },
        {
            "section_id": "body-1",
            "article_role": "body",
            "claim_ids": ["c1"],
            "required_input_section_ids": [],
            "defense_obligation_ids": ["d2"],
            "counterarguments": ["stale d2 wording"],
        },
        {
            "section_id": "body-2",
            "article_role": "body",
            "claim_ids": ["c2"],
            "required_input_section_ids": ["body-1"],
            "defense_obligation_ids": ["d1", "d0"],
            "counterarguments": ["stale d1 wording"],
        },
        {
            "section_id": "conclusion",
            "article_role": "conclusion",
            "claim_ids": ["c0"],
            "defense_obligation_ids": [],
            "counterarguments": [],
        },
    ]
    normalized, corrections = normalize_body_section_dependencies(
        sections,
        defenses=[
            {"id": "d1", "target_claim_id": "c1", "disposition": "body"},
            {"id": "d2", "target_claim_id": "c2", "disposition": "body"},
            {"id": "d0", "target_claim_id": "c0", "disposition": "body"},
        ],
    )
    assert normalized[1]["defense_obligation_ids"] == ["d1"]
    assert normalized[2]["defense_obligation_ids"] == ["d2", "d0"]
    assert normalized[1]["counterarguments"] == []
    assert normalized[2]["counterarguments"] == []
    assert [
        item["reason"] for item in corrections
        if item["reason"] == "place_body_defense_with_target_claim"
    ] == [
        "place_body_defense_with_target_claim",
        "place_body_defense_with_target_claim",
    ]


def test_body_role_normalization_routes_global_defense_by_resolution_content():
    sections = [
        {
            "section_id": "intro",
            "article_role": "introduction",
            "claim_ids": ["global"],
        },
        {
            "section_id": "contact",
            "article_role": "body",
            "title": "誌面配置と作品への接触経路",
            "section_task": "作品形式、誌面配置、読者の接触経路を照合する",
            "section_output": "配置と接触経路を区別して比較できる",
            "thesis_link": "媒体と読みの関係を検討する",
            "claim_ids": ["contact-claim"],
            "required_input_section_ids": [],
            "defense_obligation_ids": [],
        },
        {
            "section_id": "production",
            "article_role": "body",
            "title": "制作条件",
            "section_task": "制作技法と分業を検討する",
            "section_output": "制作環境の差を確立する",
            "thesis_link": "制作条件の作用を示す",
            "claim_ids": ["production-claim"],
            "required_input_section_ids": ["contact"],
            "defense_obligation_ids": ["global-defense"],
        },
        {"section_id": "conclusion", "article_role": "conclusion"},
    ]
    normalized, corrections = normalize_body_section_dependencies(
        sections,
        defenses=[
            {
                "id": "global-defense",
                "target_claim_id": "global",
                "challenge": "読みの変化は作品選択や読者の好みでも説明できる",
                "materiality": "配置と発見経路の作用を示せなければ中心主張が弱まる",
                "response": "作品形式、配置、読者の接触経路を同じ事例で照合する",
                "disposition": "body",
            }
        ],
    )
    assert normalized[1]["defense_obligation_ids"] == ["global-defense"]
    assert normalized[2]["defense_obligation_ids"] == []
    assert any(
        item.get("section_id") == "contact"
        and item.get("reason") == "place_body_defense_with_target_claim"
        for item in corrections
    )


def test_conclusion_only_defense_is_absorbed_before_body_contracts():
    normalized, corrections = normalize_defense_obligations_for_discourse_roles(
        [
            {
                "id": "d-body",
                "target_claim_id": "c-body",
                "disposition": "body",
                "resolution_location": "same_section",
            },
            {
                "id": "d-thesis",
                "target_claim_id": "c-thesis",
                "disposition": "body",
                "resolution_location": "later_section",
            },
            {
                "id": "d-synthesis",
                "target_claim_id": "c-synthesis",
                "disposition": "body",
                "resolution_location": "later_section",
            },
        ],
        discourse_nodes=[
            {
                "section_id": "intro",
                "article_role": "introduction",
                "claim_ids": ["c-thesis"],
            },
            {
                "section_id": "body",
                "article_role": "body",
                "claim_ids": ["c-body"],
            },
            {
                "section_id": "conclusion",
                "article_role": "conclusion",
                "claim_ids": ["c-thesis", "c-synthesis"],
            },
        ],
    )
    assert normalized[0]["disposition"] == "body"
    assert normalized[1]["disposition"] == "body"
    assert normalized[2]["disposition"] == "scope_absorbed"
    assert normalized[2]["resolution_location"] == "scope_absorbed"
    assert corrections == [
        {
            "obligation_id": "d-synthesis",
            "target_claim_id": "c-synthesis",
            "from_disposition": "body",
            "to_disposition": "scope_absorbed",
            "reason": "conclusion_only_claim_has_no_body_resolution_location",
        }
    ]


def test_v31_workflow_preserves_v30_and_adds_ordered_handoffs():
    old = authoring_workflow_v30()
    new = authoring_workflow(version="31")
    assert old.version == "30"
    assert new.version == "31"
    old_nodes = {node.id for node in old.nodes}
    new_nodes = {node.id for node in new.nodes}
    assert new_nodes - old_nodes == {
        "redraft_body_prose",
        "finalize_section_headings",
        "start_paragraph_formation",
        "ratify_research_problem",
        "select_defense_obligations",
        "classify_audit_findings",
        "diagnose_japanese_prose",
        "redraft_japanese_paragraphs",
        "review_japanese_sections",
    }
    edges = {(edge.source, edge.target) for edge in new.edges}
    assert ("validate_evidence_ready", "ratify_research_problem") in edges
    assert ("ratify_research_problem", "build_argument_graph") in edges
    assert ("build_argument_graph", "select_defense_obligations") in edges
    graph_node = next(node for node in new.nodes if node.id == "build_argument_graph")
    assert graph_node.human_review == "never"
    assert ("select_defense_obligations", "generate_hypotheses") in edges
    assert ("audit_structure", "classify_audit_findings") in edges
    assert ("classify_audit_findings", "join_draft_audits") in edges
    assert ("validate_draft_coherence", "diagnose_japanese_prose") in edges
    assert ("diagnose_japanese_prose", "redraft_japanese_paragraphs") in edges
    assert ("redraft_japanese_paragraphs", "review_japanese_sections") in edges
    assert ("review_japanese_sections", "redraft_japanese_paragraphs") in edges
    assert ("review_japanese_sections", "extract_proofreading_sentences") in edges
    assert ("redraft_body_prose", "finalize_section_headings") in edges


def test_v31_stage_json_schemas_accept_round_trip_and_keep_roles_distinct():
    legacy_treatment = STAGE_SCHEMAS["draft"]["properties"]["sections"]["items"][
        "properties"
    ]["evidence_treatment"]["items"]
    assert "support_sentence" not in legacy_treatment["required"]
    assert "support_sentence" not in legacy_treatment["properties"]
    graph_guidance = STAGE_GUIDANCE["argument_graph_v31"]
    assert "evidence --supports--> warrant --warrants--> claim" in graph_guidance
    assert "same claim_id" in graph_guidance
    for stage in (
        "research_problem_v31",
        "argument_graph_v31",
        "defense_obligations_v31",
        "section_contracts_v31",
        "semantic_paragraphs_v31",
        "draft_introduction_v31",
        "draft_body_v31",
        "draft_conclusion_v31",
        "audit_v31",
    ):
        value = _example(STAGE_SCHEMAS[stage])
        CodexAuthoringAdapter.validate_result(stage, _json(value))
    assert (
        STAGE_SCHEMAS["section_contracts_v31"]["properties"]["sections"]["items"]["properties"][
            "development_expectations"
        ]["properties"]["minimum_evidence_items"]["minimum"]
        == 0
    )
    assert (
        STAGE_SCHEMAS["section_contracts"]["properties"]["sections"]["items"]["properties"][
            "development_expectations"
        ]["properties"]["minimum_evidence_items"]["minimum"]
        == 1
    )
    for role in ("introduction", "body", "conclusion"):
        stage = f"draft_{role}_v31"
        paragraph = STAGE_SCHEMAS[stage]["properties"]["sections"]["items"]["properties"][
            "paragraphs"
        ]["items"]["properties"]
        paragraph_schema = STAGE_SCHEMAS[stage]["properties"]["sections"][
            "items"
        ]["properties"]["paragraphs"]["items"]
        assert paragraph["move_indices"]["minItems"] == 0
        assert set(paragraph_schema["required"]) == set(paragraph_schema["properties"])
        section = STAGE_SCHEMAS[stage]["properties"]["sections"]["items"]["properties"]
        assert section["concrete_examples"]["minItems"] == 0
        treatment = section["evidence_treatment"]["items"]
        assert "support_sentence" in treatment["required"]
        assert {
            "support_paragraph_index", "support_sentence_index",
        } <= set(treatment["required"])
    assert "conclusion_synthesis" in STAGE_SCHEMAS["draft_conclusion_v31"]["required"]
    assert "conclusion_synthesis" not in STAGE_SCHEMAS["draft_body_v31"]["required"]


def test_research_problem_to_conclusion_json_handoff_and_new_evidence_gate():
    data = _handoff_fixture()
    conclusion = data["conclusion_model"]
    synthesis = validate_conclusion_synthesis(
        _json(data["synthesis"]),
        conclusion_contract=conclusion,
        conclusion_section={"realized_evidence_ids": ["e1"]},
        body_sections=[
            {"section_id": "s1", "realized_evidence_ids": ["e1"]},
            {"section_id": "s2", "realized_evidence_ids": []},
        ],
    )
    assert synthesis.established_section_ids == ("s1", "s2")
    assert [item.section_id for item in data["bodies"]] == ["s1", "s2"]
    validate_conclusion_synthesis(
        _json(data["synthesis"]),
        conclusion_contract=conclusion,
        conclusion_section={"realized_evidence_ids": ["intro-evidence"]},
        body_sections=[
            {"section_id": "s1", "realized_evidence_ids": ["e1"]},
            {"section_id": "s2", "realized_evidence_ids": []},
        ],
        introduction_section={"realized_evidence_ids": ["intro-evidence"]},
    )
    with pytest.raises(FukamiruError, match="new evidence"):
        validate_conclusion_synthesis(
            _json(data["synthesis"]),
            conclusion_contract=conclusion,
            conclusion_section={"realized_evidence_ids": ["new-evidence"]},
            body_sections=[
                {"section_id": "s1", "realized_evidence_ids": ["e1"]},
                {"section_id": "s2", "realized_evidence_ids": []},
            ],
        )


def test_conclusion_evidence_normalization_removes_only_undeclared_new_ids():
    sections = [
        {
            "section_id": "intro",
            "article_role": "introduction",
            "realized_evidence_ids": ["e-intro"],
        },
        {
            "section_id": "body",
            "article_role": "body",
            "realized_evidence_ids": ["e-body"],
        },
        {
            "section_id": "conclusion",
            "article_role": "conclusion",
            "evidence_ids": ["e-intro", "e-body", "e-new"],
            "realized_evidence_ids": ["e-intro", "e-body", "e-new"],
            "paragraphs": [
                {"text": "synthesis", "evidence_ids": ["e-body", "e-new"]}
            ],
            "evidence_treatment": [
                {"evidence_id": "e-body"},
                {"evidence_id": "e-new"},
            ],
        },
    ]
    normalized, corrections = normalize_conclusion_evidence_declaration(
        sections,
        conclusion_synthesis={"new_evidence_ids": []},
    )
    conclusion = normalized[-1]
    assert conclusion["evidence_ids"] == ["e-intro", "e-body"]
    assert conclusion["realized_evidence_ids"] == ["e-intro", "e-body"]
    assert conclusion["paragraphs"][0]["evidence_ids"] == ["e-body"]
    assert conclusion["evidence_treatment"] == [{"evidence_id": "e-body"}]
    assert corrections[0]["removed_undeclared_new_evidence_ids"] == ["e-new"]

    declared, corrections = normalize_conclusion_evidence_declaration(
        sections,
        conclusion_synthesis={"new_evidence_ids": ["e-new"]},
    )
    assert declared[-1]["realized_evidence_ids"] == ["e-intro", "e-body", "e-new"]
    assert corrections == []


def test_repair_validates_conclusion_handoff_before_persisting(monkeypatch):
    data = _handoff_fixture()
    monkeypatch.setattr(
        WorkerRunner,
        "_authoring_draft_contract_violations",
        staticmethod(lambda _context, _sections, **_kwargs: []),
    )
    context = {
        "conclusion_contract": data["conclusion_model"].model_dump(mode="json"),
    }
    section_by_id = {
        "s0": {
            "section_id": "s0",
            "article_role": "introduction",
            "realized_evidence_ids": ["intro-evidence"],
        },
        "s1": {
            "section_id": "s1",
            "article_role": "body",
            "realized_evidence_ids": ["e1"],
        },
        "s2": {
            "section_id": "s2",
            "article_role": "body",
            "realized_evidence_ids": [],
        },
    }
    generated = {
        "section_id": "s3",
        "article_role": "conclusion",
        "realized_evidence_ids": ["e1"],
    }
    invalid_synthesis = {
        **data["synthesis"],
        "new_evidence_ids": ["e1"],
        "new_evidence_rhetorical_reason": "新しい根拠として使う",
    }
    violations = WorkerRunner._authoring_repair_local_contract_violations(
        context,
        role="conclusion",
        generated_section=generated,
        section_by_id=section_by_id,
        contracts=data["sections"],
        conclusion_synthesis=invalid_synthesis,
    )
    assert violations == [
        "Conclusion new evidence metadata disagrees with realized evidence"
    ]
    assert not WorkerRunner._authoring_repair_local_contract_violations(
        context,
        role="conclusion",
        generated_section=generated,
        section_by_id=section_by_id,
        contracts=data["sections"],
        conclusion_synthesis=data["synthesis"],
    )


def test_provenance_and_defense_fail_closed_across_json_nodes():
    data = _handoff_fixture()
    problem = deepcopy(data["problem_raw"])
    problem["prior_positions"] = [{"proposition": "通説X", "source_ids": ["invented"]}]
    with pytest.raises(FukamiruError, match="accepted source"):
        validate_research_problem(
            _json(problem),
            question=data["question"],
            accepted_evidence=[],
            propositions=data["propositions"],
        )
    obligation = {
        "id": "d1",
        "target_claim_id": "c1",
        "challenge": "別解釈",
        "basis_ids": ["e1"],
        "materiality": "",
        "response": "再検討",
        "resolution_location": "same_section",
        "disposition": "body",
        "source_argument_node_ids": [],
    }
    with pytest.raises(FukamiruError, match="materiality"):
        validate_defense_obligations(
            _json([obligation]),
            claims=data["claims"],
            argument_nodes=data["nodes"],
            accepted_evidence=[],
        )
    wrong_target = {
        **obligation,
        "materiality": "元の因果説明が崩れる",
        "source_argument_node_ids": ["other-counter"],
    }
    with pytest.raises(FukamiruError, match="another claim"):
        validate_defense_obligations(
            _json([wrong_target]),
            claims=data["claims"],
            argument_nodes=[
                *data["nodes"],
                {"id": "other-counter", "node_type": "counterexample", "claim_id": "c2"},
            ],
            accepted_evidence=[],
        )


def test_research_problem_may_refine_provisional_answer_but_not_research_question():
    data = _handoff_fixture()
    problem = deepcopy(data["problem_raw"])
    problem["provisional_answer"] = "資料に示された二例の差は、掲載位置の違いから説明できる。"
    ratified = validate_research_problem(
        _json(problem),
        question=data["question"],
        accepted_evidence=[],
        propositions=data["propositions"],
    )
    assert ratified.provisional_answer == problem["provisional_answer"]

    problem["research_question"] = "別の問いへ変更する。"
    with pytest.raises(FukamiruError, match="changed the provisional RQ"):
        validate_research_problem(
            _json(problem),
            question=data["question"],
            accepted_evidence=[],
            propositions=data["propositions"],
        )


def test_v31_argument_coverage_does_not_require_counterexample():
    data = _handoff_fixture()
    context = {"claims": data["claims"], "argument_nodes": data["nodes"]}
    runner = object.__new__(WorkerRunner)
    assert runner._authoring_validate_argument_coverage(
        SimpleNamespace(workflow_version="31"), context
    ).context["argument_coverage_validation"]["passed"]
    with pytest.raises(FukamiruError, match="counterexample_or_rebuttal"):
        runner._authoring_validate_argument_coverage(
            SimpleNamespace(workflow_version="30"), context
        )


def test_v31_legacy_bridge_excludes_internal_counterevidence():
    runner = object.__new__(WorkerRunner)
    context = {
        "plan_id": "plan-1",
        "language": "ja",
        "research_problem": {"research_question": "なぜか"},
        "structure_plan_validation": {"valid": True},
        "section_contracts": [{
            "section_id": "s1", "title": "本文", "article_role": "body",
            "claim_ids": ["c1"], "evidence_ids": ["e1"], "warrant_ids": ["w1"],
            "defense_obligation_ids": ["d1"],
            "development_expectations": {"required_argument_moves": []},
        }],
        "argument_nodes": [
            {"id": "e1", "node_type": "evidence", "claim_id": "c1"},
            {"id": "w1", "node_type": "warrant", "claim_id": "c1"},
            {"id": "x1", "node_type": "counterexample", "claim_id": "c1"},
            {"id": "x2", "node_type": "counterexample", "claim_id": "c1"},
        ],
        "defense_obligations": [{
            "id": "d1", "disposition": "body",
            "source_argument_node_ids": ["x1"],
        }],
        "draft_sections": [{
            "section_id": "s1", "article_role": "body",
            "claim_ids": ["c1"], "realized_evidence_ids": ["e1"],
            "paragraphs": [{
                "move_indices": [], "claim_ids": ["c1"],
                "evidence_ids": ["e1"], "text": "本文。"
            }],
            "text": "本文。",
        }],
    }
    html = runner._authoring_render_sections(context, context["draft_sections"])
    bridge = runner._authoring_v29_plan_bridge(context, html)
    assert bridge["paragraph_plan_validation"]["valid"]
    assert bridge["realized_paragraphs"][0]["counterargument_ids"] == ["x1"]


def test_v31_draft_render_mapping_collapses_internal_blank_lines_without_loss():
    context = {
        "plan_id": "plan-1",
        "plan_revision": 1,
        "language": "ja",
        "research_problem": {"research_question": "問いは何か"},
        "section_contracts": [{
            "section_id": "sec-conclusion",
            "title": "結論",
            "article_role": "conclusion",
            "claim_ids": [],
            "evidence_ids": [],
            "development_expectations": {"required_argument_moves": ["answer"]},
        }],
    }
    generated = [{
        "section_id": "plan-1@1:sec-conclusion",
        "paragraphs": [{
            "text": "前半の結論。\n\n後半の結論。",
            "move_indices": [1],
            "evidence_ids": ["ev-1"],
            "sentence_ids": ["sec-conclusion-P01-S01", "sec-conclusion-P01-S02"],
        }],
        "text": "前半の結論。\n\n後半の結論。",
    }]

    normalized = WorkerRunner._authoring_normalize_generated_section_ids(
        context,
        generated,
        single_rendered_block_per_paragraph=True,
    )

    assert normalized[0]["paragraphs"][0]["text"] == "前半の結論。後半の結論。"
    assert normalized[0]["paragraphs"][0]["evidence_ids"] == ["ev-1"]
    assert normalized[0]["paragraphs"][0]["sentence_ids"] == [
        "sec-conclusion-P01-S01",
        "sec-conclusion-P01-S02",
    ]
    assert WorkerRunner._authoring_v31_render_mapping_problems(
        context, normalized
    ) == []
    html = WorkerRunner._authoring_render_sections(context, normalized)
    bridge = WorkerRunner._authoring_v29_plan_bridge(
        {**context, "draft_sections": normalized}, html, semantic_attribution=True
    )
    assert bridge["paragraph_plan_validation"]["valid"]
    assert len(bridge["paragraph_plan"]["paragraphs"]) == 1


def test_v31_draft_render_mapping_identifies_a_target_section_for_repair():
    context = {
        "plan_id": "plan-1",
        "plan_revision": 1,
        "language": "ja",
        "section_contracts": [{
            "section_id": "sec-conclusion",
            "title": "結論",
            "article_role": "conclusion",
        }],
    }
    section = {
        "section_id": "sec-conclusion",
        "paragraphs": [],
        "text": "第一段落。\n\n第二段落。",
    }

    problems = WorkerRunner._authoring_v31_render_mapping_problems(
        context, [section]
    )

    assert len(problems) == 1
    assert problems[0].startswith("section sec-conclusion paragraph mapping count mismatch")
    assert "0 semantic mappings for 2 rendered blocks" in problems[0]


def test_v31_japanese_bridge_validation_is_non_retryable_for_unchanged_input(
    monkeypatch,
):
    runner = object.__new__(WorkerRunner)
    monkeypatch.setattr(
        runner, "_authoring_japanese_checked_bridge", lambda *_args: None
    )
    monkeypatch.setattr(
        runner,
        "_authoring_v29_plan_bridge",
        lambda *_args, **_kwargs: {
            "structure_plan_validation": {"valid": True},
            "paragraph_plan_validation": {
                "valid": False,
                "issues": [{
                    "code": "paragraph.render_mapping_mismatch",
                    "section_id": "sec-conclusion",
                }],
            },
            "authoring_paragraph_citation_lineage": [],
            "authoring_citation_lineage_reconciliation": {"dropped_bindings": []},
        },
    )
    context = {
        "proposed_html": "<article />",
        "japanese_redrafting_work": {
            "sections": [{"section_id": "sec-conclusion", "accepted": True, "attempts": []}]
        },
    }

    with pytest.raises(
        NonRetryableFukamiruError,
        match="do not retry the unchanged input.*paragraph.render_mapping_mismatch",
    ):
        runner._authoring_review_japanese_sections(SimpleNamespace(), context)


def test_v31_reconciliation_keeps_only_visible_structured_citation_lineage(
    monkeypatch,
):
    visible_attribution = (
        "ランシエール『感性的なもののパルタージュ』を比較の枠として参照する。"
    )
    context = {
        "plan_id": "plan", "language": "ja", "research_problem": {"research_question": "RQ"},
        "section_contracts": [{
            "section_id": "s1", "title": "適用範囲", "article_role": "body",
            "claim_ids": ["c1"], "evidence_ids": ["e1"],
            "development_expectations": {"required_argument_moves": []},
        }],
        "argument_nodes": [
            {"id": "e1", "node_type": "evidence", "claim_id": "c1"},
            {"id": "a1", "node_type": "evidence", "claim_id": "c1",
             "provenance": {"source_ids": ["source-1"]}},
        ],
        "evidence_context": [{
            "id": "source-1",
            "allowed_use": "attributed_scholarship_citation",
            "authors": ["ランシエール"],
            "title": "感性的なもののパルタージュ",
        }],
        "draft_sections": [{
            "section_id": "s1", "article_role": "body", "claim_ids": ["c1"],
            "paragraphs": [{"text": visible_attribution, "evidence_ids": ["e1"],
                            "move_indices": []}],
            "text": visible_attribution,
        }],
        "authoring_paragraph_citation_lineage": [{
            "output_block_id": "P-plan-s1-001",
            "argument_evidence_node_ids": ["a1"],
            "evidence_ids": ["source-1"],
        }],
    }
    html = WorkerRunner._authoring_render_sections(context, context["draft_sections"])
    assert ManuscriptCoherenceService.document_manifest(html)["terminal_roles"] == []
    bridge = WorkerRunner._authoring_v29_plan_bridge(context, html)
    assert bridge["authoring_paragraph_citation_lineage"][0]["evidence_ids"] == [
        "source-1"
    ]

    self_citation_context = deepcopy(context)
    self_citation = (
        "筆者は『架空資料の比較分析』で作品の意味を"
        "要素相互の配置から捉えた。"
    )
    self_citation_context["evidence_context"][0].update(
        authors=["自著"],
        title="架空資料の比較分析――『星の記録』",
    )
    self_citation_context["reference_entries"] = [{
        "reference_id": "source-1",
        "is_self_authored": True,
        "original_text": "自著「架空資料の比較分析」",
    }]
    self_citation_context["draft_sections"][0]["paragraphs"][0]["text"] = self_citation
    self_citation_context["draft_sections"][0]["text"] = self_citation
    self_citation_html = WorkerRunner._authoring_render_sections(
        self_citation_context, self_citation_context["draft_sections"]
    )
    legacy_bridge = WorkerRunner._authoring_v29_plan_bridge(
        self_citation_context, self_citation_html
    )
    assert legacy_bridge["authoring_paragraph_citation_lineage"] == []
    semantic_bridge = WorkerRunner._authoring_v29_plan_bridge(
        self_citation_context, self_citation_html, semantic_attribution=True
    )
    assert semantic_bridge["authoring_paragraph_citation_lineage"][0]["evidence_ids"] == [
        "source-1"
    ]

    legacy_html = html.replace(' data-authoring-role="body"', "")
    annotated = WorkerRunner._authoring_annotate_rendered_roles(context, legacy_html)
    assert ManuscriptCoherenceService.document_manifest(annotated)["terminal_roles"] == []
    bridge = WorkerRunner._authoring_v29_plan_bridge(context, annotated)
    assert bridge["authoring_paragraph_citation_lineage"][0]["evidence_ids"] == ["source-1"]

    without_visible_attribution = annotated.replace(visible_attribution, "本文。")
    bridge = WorkerRunner._authoring_v29_plan_bridge(
        context, without_visible_attribution
    )
    assert bridge["authoring_paragraph_citation_lineage"] == []
    assert bridge["authoring_citation_lineage_reconciliation"][
        "dropped_bindings"
    ] == [{
        "section_id": "semantic:H-plan-s1",
        "authoring_section_id": "s1",
        "output_block_id": "P-plan-s1-001",
        "evidence_id": "source-1",
        "argument_evidence_node_ids": ["a1"],
        "reason": "visible_attribution_missing",
    }]

    runner = object.__new__(WorkerRunner)
    with pytest.raises(
        FukamiruError,
        match="dropped visible source attribution before proofreading",
    ):
        runner._authoring_reconcile_candidate_context(
            SimpleNamespace(workflow_version="31"),
            {**context, "proposed_html": without_visible_attribution},
        )

    no_prior_lineage = deepcopy(context)
    no_prior_lineage["authoring_paragraph_citation_lineage"] = []
    no_prior_lineage["draft_sections"][0]["paragraphs"][0]["text"] = "本文。"
    no_prior_lineage["draft_sections"][0]["text"] = "本文。"
    no_prior_html = WorkerRunner._authoring_render_sections(
        no_prior_lineage, no_prior_lineage["draft_sections"]
    )
    bridge = WorkerRunner._authoring_v29_plan_bridge(
        no_prior_lineage, no_prior_html
    )
    assert bridge["authoring_paragraph_citation_lineage"] == []

    monkeypatch.setattr(
        runner,
        "_authoring_v29_plan_bridge",
        lambda _context, _html, **_kwargs: {
            "structure_plan_validation": {"valid": True},
            "authoring_paragraph_citation_lineage": [],
        },
    )
    with pytest.raises(FukamiruError, match="dropped structured citation lineage"):
        runner._authoring_reconcile_candidate_context(
            SimpleNamespace(workflow_version="31"),
            {**context, "proposed_html": without_visible_attribution},
        )


def test_v31_footnoted_conditional_outcome_is_isolated_until_submission_join(
    monkeypatch,
):
    pre_footnote_conditional = {
        "status": "conditional_success",
        "reason": "coherence_unresolved",
        "stage": "post_polish",
    }
    context = {
        "proposed_html": "<article id='a'></article>",
        "v26_coherence_report": {"stage": "post_polish", "passed": False},
        "v26_block_verification_records": [],
        "completion_status": "conditional_success",
        "conditional_success": pre_footnote_conditional,
        "coherence_unresolved_routes": [{"stage": "post_polish"}],
        "additional_research_requirements": [{"stage": "post_polish"}],
        "reference_quality_gate": {"passed": True},
        "rendered_structure_v26": {"valid": True},
    }
    post_footnote_conditional = {
        **pre_footnote_conditional,
        "stage": "post_footnote",
    }

    def audit(_worker, _claimed, source, *, stage, allow_repairs=True):
        assert stage == "post_footnote"
        assert allow_repairs is False
        return (
            {
                **source,
                "v26_coherence_report": {"stage": stage, "passed": False},
                "conditional_success": post_footnote_conditional,
                "coherence_unresolved_routes": [{"stage": stage}],
                "additional_research_requirements": [{"stage": stage}],
            },
            "conditional_success",
        )

    monkeypatch.setattr(
        "fukamiru.worker.runner.final_worker_v27.audit_coherence", audit
    )
    runner = WorkerRunner.__new__(WorkerRunner)
    branch = runner._manuscript_validate_footnoted_coherence_v29(
        SimpleNamespace(), context
    )
    assert branch.outcome == "conditional_success"
    assert branch.context["conditional_success"] == pre_footnote_conditional
    assert branch.context["footnoted_conditional_success"] == (
        post_footnote_conditional
    )

    merged = WorkerRunner._merge_join_contexts(
        context,
        branch.context,
        path="validate_footnoted_coherence.conditional_success",
    )
    joined = runner._authoring_join_submission_validations(
        SimpleNamespace(), merged
    )
    assert joined.outcome == "conditional_success"
    assert joined.context["submission_validation_gate"][
        "conditional_validations"
    ] == ["footnoted_coherence"]
    assert joined.context["conditional_success"]["stage"] == "post_footnote"


def test_v31_unresolved_footnote_only_reaches_final_review():
    runner = WorkerRunner.__new__(WorkerRunner)
    base = {
        "proposed_html": '<article id="D" lang="ja"></article>',
        "authoring_workflow_policy": {"workflow_version": "31"},
        "reference_quality_gate": {
            "passed": False,
            "issues": [{
                "code": "reference.required_author_citation_missing",
                "severity": "error",
            }],
        },
        "rendered_structure_v26": {"valid": True},
        "footnoted_coherence_report": {"passed": True},
        "human_review_items": [{
            "review_item_id": "HR-EU-1",
            "section_id": "s1",
            "sentence_id": "s1-P01-S01",
            "category": "evidence_use_footnote_unresolved",
            "reason": "脚注位置が未解決",
            "source_ids": ["EU-1"],
            "recommended_check": "原資料と照合",
            "blocks_render": False,
            "status": "open",
        }],
        "evidence_footnote_render_report": {
            "unresolved_footnote_bindings": [{
                "category": "evidence_use_footnote_unresolved",
                "source_id": "EU-1",
                "section_id": "s1",
                "sentence_id": "s1-P01-S01",
            }],
        },
    }
    reviewed = runner._authoring_join_submission_validations(
        SimpleNamespace(), base
    )
    assert reviewed.outcome == "conditional_success"
    assert reviewed.context["submission_validation_gate"]["passed"] is False
    assert reviewed.context["submission_validation_gate"][
        "unresolved_footnote_binding_count"
    ] == 1
    assert reviewed.context["submission_validation_gate"][
        "conditional_validations"
    ] == ["reference_quality", "reference_integrity"]

    unsafe = deepcopy(base)
    unsafe["reference_quality_gate"]["issues"].append({
        "code": "reference.unknown_target", "severity": "error"
    })
    assert runner._authoring_join_submission_validations(
        SimpleNamespace(), unsafe
    ).outcome == "failure"
    missing_review = deepcopy(base)
    missing_review["human_review_items"] = []
    assert runner._authoring_join_submission_validations(
        SimpleNamespace(), missing_review
    ).outcome == "failure"


def test_v31_scholarship_handoff_follows_role_and_documented_prior_position():
    requirements = {
        "e-prior": {"id": "source-prior", "claim_id": "c1"},
        "e-body": {"id": "source-body", "claim_id": "c1"},
    }
    context = {
        "research_problem": {
            "prior_positions": [{"proposition": "A", "source_ids": ["source-prior"]}]
        },
        "authoring_atomic_evidence_contracts": [
            {
                "claim_ids": ["c1"],
                "realization": "attribute_theory_with_citation",
                "citation_evidence_ids": ["source-body"],
            }
        ],
    }
    required = WorkerRunner._authoring_required_scholarship_ids
    assert required(
        context, {"article_role": "introduction", "claim_ids": ["c1"]}, requirements
    ) == {"e-prior"}
    assert required(
        context, {"article_role": "body", "claim_ids": ["c1"]}, requirements
    ) == {"e-body"}
    assert required(
        context, {"article_role": "conclusion", "claim_ids": ["c1"]}, requirements
    ) == set()


def test_v31_conclusion_projects_assigned_author_and_work_without_upgrading_evidence():
    citation_id = "annotation-citation:conclusion_scholarship"
    evidence_id = "fixture-evidence-ab9e68b275b9b21f"
    title = "感性的なもののパルタージュ : 美学と政治"
    citation = {
        "id": citation_id,
        "reference_id": "reference:conclusion_scholarship",
        "title": title,
        "authors": ["ランシエール、ジャック"],
        "allowed_use": "attributed_scholarship_citation",
        "verification_scope": "bibliographic_identity_only",
        "provenance": {
            "bibliographic_identity_verified": True,
            "substantive_support_verified": False,
        },
    }
    context = {
        "annotation_citation_evidence": [citation],
        "reference_entries": [{"reference_id": "reference:conclusion_scholarship", "title": title}],
        "argument_nodes": [
            {
                "id": evidence_id,
                "node_type": "evidence",
                "claim_id": "cl0032",
                "provenance": {"source_ids": [citation_id]},
            }
        ],
    }
    conclusion = {
        "section_id": "sec-conclusion",
        "article_role": "conclusion",
        "claim_ids": ["cl0032"],
        "evidence_ids": [evidence_id],
    }

    anchors = WorkerRunner._authoring_v31_visible_scholarship_citation_anchors(
        context, [conclusion]
    )
    requirement = WorkerRunner._authoring_attributed_citation_requirements(context)[
        evidence_id
    ]

    assert anchors == [
        {
            "section_id": "sec-conclusion",
            "evidence_id": evidence_id,
            "citation_evidence_id": citation_id,
            "reference_id": "reference:conclusion_scholarship",
            "author": "ランシエール、ジャック",
            "work": title,
            "verification_scope": "bibliographic_identity_only",
            "bibliographic_identity_verified": True,
            "substantive_support_verified": False,
        }
    ]
    assert WorkerRunner._authoring_attribution_mentions_source(
        "ランシエール『感性的なもののパルタージュ』を本稿の理論的参照に置き、"
        "個別事例の根拠とは区別する。",
        requirement,
    )
    assert WorkerRunner._authoring_v31_visible_scholarship_citation_anchors(
        context, [{**conclusion, "evidence_ids": [], "claim_ids": []}]
    ) == []
    guidance = STAGE_GUIDANCE["draft_conclusion_v31"]
    assert "reader_facing_scholarship_citation_anchors" in guidance
    assert "identify each cited source" in guidance
    assert "evidence_ids includes that record's evidence_id" in guidance
    assert "not empirical support for an individual case" in guidance


def test_v31_body_scholarship_mention_keeps_the_exact_reference_identity():
    citation = {
        "id": "annotation-citation:book",
        "reference_id": "reference:book",
        "title": "感性的なもののパルタージュ",
        "authors": ["ランシエール"],
        "allowed_use": "attributed_scholarship_citation",
        "verification_scope": "bibliographic_identity_only",
        "provenance": {"bibliographic_identity_verified": True},
    }
    context = {
        "annotation_citation_evidence": [citation],
        "reference_entries": [{
            "reference_id": "reference:book",
            "title": citation["title"],
        }],
        "argument_nodes": [{
            "id": "ev-book",
            "node_type": "evidence",
            "claim_id": "claim-1",
            "provenance": {"source_ids": [citation["id"]]},
        }],
    }
    contract = {
        "section_id": "sec-body",
        "article_role": "body",
        "evidence_ids": ["ev-book"],
    }
    assert WorkerRunner._authoring_v31_visible_scholarship_citation_anchors(
        context, [contract]
    )[0]["reference_id"] == "reference:book"

    paragraph = {
        "text": "ランシエール『感性的なもののパルタージュ』を比較の枠に用いる。",
        "citation_mentions": [{
            "reference_id": "reference:book",
            "text": "『感性的なもののパルタージュ』",
        }],
    }
    assert WorkerRunner._authoring_v31_citation_mention_problems(
        context,
        [{"section_id": "sec-body", "paragraphs": [paragraph]}],
        [contract],
    ) == []

    wrong_reference = {
        **paragraph,
        "citation_mentions": [{
            "reference_id": "reference:other",
            "text": "『感性的なもののパルタージュ』",
        }],
    }
    problems = WorkerRunner._authoring_v31_citation_mention_problems(
        context,
        [{"section_id": "sec-body", "paragraphs": [wrong_reference]}],
        [contract],
    )
    assert any("uncontracted citation reference" in item for item in problems)


def test_v31_body_citation_anchors_include_each_record_for_the_claim():
    citations = [
        {
            "id": "citation:first",
            "reference_id": "reference:first",
            "title": "First Work",
            "authors": ["Scholar A"],
            "allowed_use": "attributed_scholarship_citation",
        },
        {
            "id": "citation:second",
            "reference_id": "reference:second",
            "title": "Second Work",
            "authors": ["Scholar B"],
            "allowed_use": "attributed_scholarship_citation",
        },
    ]
    context = {
        "annotation_citation_evidence": citations,
        "reference_entries": [
            {"reference_id": "reference:first", "title": "First Work"},
            {"reference_id": "reference:second", "title": "Second Work"},
        ],
        "argument_nodes": [
            {
                "id": "ev-first",
                "node_type": "evidence",
                "claim_id": "claim-1",
                "provenance": {"source_ids": ["citation:first"]},
            },
            {
                "id": "ev-second",
                "node_type": "evidence",
                "claim_id": "claim-1",
                "provenance": {"source_ids": ["citation:second"]},
            },
        ],
    }
    contract = {
        "section_id": "sec-body",
        "article_role": "body",
        "claim_ids": ["claim-1"],
        "evidence_ids": [],
    }
    anchors = WorkerRunner._authoring_v31_visible_scholarship_citation_anchors(
        context, [contract]
    )
    assert {item["reference_id"] for item in anchors} == {
        "reference:first",
        "reference:second",
    }
    paragraph = {
        "text": "『First Work』と『Second Work』を比較する。",
        "citation_mentions": [
            {"reference_id": "reference:first", "text": "『First Work』"},
            {"reference_id": "reference:second", "text": "『Second Work』"},
        ],
    }
    assert WorkerRunner._authoring_v31_citation_mention_problems(
        context,
        [{"section_id": "sec-body", "paragraphs": [paragraph]}],
        [contract],
    ) == []


def test_v31_scholarship_projection_rejects_dangling_source_to_claim_json_link():
    context = {
        "research_problem": {"research_question": "なぜか"},
        "authoring_atomic_evidence_contracts": [{
            "realization": "attribute_theory_with_citation",
            "claim_ids": ["c1"],
            "citation_evidence_ids": ["book"],
        }],
        "section_contracts": [{
            "section_id": "s1", "article_role": "body", "claim_ids": ["c1"],
            "evidence_ids": [],
        }],
        "argument_nodes": [],
        "evidence_context": [{
            "id": "book", "reference_id": "reference:book",
            "allowed_use": "attributed_scholarship_citation",
            "title": "A Book", "authors": ["Scholar"],
        }],
        "reference_entries": [{
            "reference_id": "reference:book", "title": "A Book",
            "authors": ["Scholar"],
        }],
    }

    with pytest.raises(
        NonRetryableFukamiruError, match="lost required source-to-claim mapping"
    ):
        WorkerRunner._authoring_draft_contracts_with_scholarship(
            context, context["section_contracts"]
        )
    review_context = {
        **context,
        "authoring_v31_source_review_policy":
            "unresolved-required-citation-to-final-review-v1",
    }
    candidate = WorkerRunner._authoring_draft_contracts_with_scholarship(
        review_context, context["section_contracts"]
    )[0]
    assert candidate["required_scholarship_evidence_ids"] == []
    assert candidate["unresolved_required_citation_sources"] == [{
        "section_id": "s1", "source_id": "book", "evidence_ids": [],
        "claim_ids": ["c1"], "reference_id": "reference:book",
        "title": "A Book", "authors": ["Scholar"],
        "author_supplied_assertions": [],
        "reason": "required source has no citation-to-claim evidence mapping",
    }]
    missing_bibliography = {
        **context,
        "argument_nodes": [{
            "id": "e-book", "node_type": "evidence", "claim_id": "c1",
            "provenance": {"source_ids": ["book"]},
        }],
        "reference_entries": [],
    }
    with pytest.raises(
        NonRetryableFukamiruError,
        match="lost required source-to-bibliography mapping",
    ):
        WorkerRunner._authoring_draft_contracts_with_scholarship(
            missing_bibliography, missing_bibliography["section_contracts"]
        )
    review_context = {
        **missing_bibliography,
        "authoring_v31_source_review_policy":
            "unresolved-required-citation-to-final-review-v1",
    }
    candidate = WorkerRunner._authoring_draft_contracts_with_scholarship(
        review_context, review_context["section_contracts"]
    )[0]
    assert candidate["evidence_ids"] == []
    assert candidate["required_scholarship_evidence_ids"] == []
    assert candidate["unresolved_required_citation_sources"][0]["source_id"] == "book"
    assert candidate["unresolved_required_citation_sources"][0]["evidence_ids"] == [
        "e-book"
    ]


def test_v31_scholarship_projection_keeps_all_citations_on_one_argument_node():
    jameson_id = "annotation-citation:jameson"
    williams_id = "annotation-citation:williams"
    evidence_id = "e-combined-theory"
    section = {
        "section_id": "sec-theory",
        "article_role": "body",
        "claim_ids": ["c1"],
        "evidence_ids": [],
    }
    context = {
        "research_problem": {"research_question": "理論はどう接続するか"},
        "authoring_atomic_evidence_contracts": [{
            "realization": "attribute_theory_with_citation",
            "claim_ids": ["c1"],
            "citation_evidence_ids": [williams_id],
            "assertion": "理論上の比較を行う",
        }],
        "section_contracts": [section],
        "argument_nodes": [{
            "id": evidence_id,
            "node_type": "evidence",
            "claim_id": "c1",
            "provenance": {"source_ids": [jameson_id, williams_id]},
        }],
        "annotation_citation_evidence": [
            {
                "id": jameson_id,
                "reference_id": "reference:jameson",
                "allowed_use": "attributed_scholarship_citation",
                "title": "The Political Unconscious",
                "authors": ["Fredric Jameson"],
            },
            {
                "id": williams_id,
                "reference_id": "reference:williams",
                "allowed_use": "attributed_scholarship_citation",
                "title": "Marxism and Literature",
                "authors": ["Raymond Williams"],
            },
        ],
        "reference_entries": [
            {"reference_id": "reference:jameson", "title": "The Political Unconscious"},
            {"reference_id": "reference:williams", "title": "Marxism and Literature"},
        ],
    }

    requirements = WorkerRunner._authoring_attributed_citation_requirements(context)
    projected = WorkerRunner._authoring_draft_contracts_with_scholarship(
        context, [section]
    )
    obligations = WorkerRunner._authoring_v31_required_scholarship_obligations(
        context, [section]
    )

    assert [
        citation["id"]
        for citation in WorkerRunner._authoring_citation_source_requirements(
            requirements[evidence_id]
        )
    ] == [jameson_id, williams_id]
    assert projected[0]["required_scholarship_evidence_ids"] == [evidence_id]
    assert WorkerRunner._authoring_required_scholarship_citation_ids(context) == {
        jameson_id,
        williams_id,
    }
    assert [item["citation_evidence_id"] for item in obligations] == [williams_id]

    shared = deepcopy(context)
    shared["argument_nodes"][0]["claim_id"] = ""
    shared_projection = WorkerRunner._authoring_draft_contracts_with_scholarship(
        shared, [section]
    )
    assert shared_projection[0]["required_scholarship_evidence_ids"] == [
        evidence_id
    ]
    shared_obligations = WorkerRunner._authoring_v31_required_scholarship_obligations(
        shared, [section]
    )
    assert [item["citation_evidence_id"] for item in shared_obligations] == [
        williams_id
    ]
    assert shared_obligations[0]["claim_ids"] == ["c1"]

    unresolved_shared = deepcopy(context)
    unresolved_shared["authoring_v31_source_review_policy"] = (
        "unresolved-required-citation-to-final-review-v1"
    )
    unresolved_shared["authoring_atomic_evidence_contracts"][0][
        "citation_evidence_ids"
    ] = [jameson_id, williams_id]
    unresolved_shared["reference_entries"] = [
        {"reference_id": "reference:williams", "title": "Marxism and Literature"}
    ]
    withheld = WorkerRunner._authoring_draft_contracts_with_scholarship(
        unresolved_shared, [section]
    )[0]
    assert withheld["required_scholarship_evidence_ids"] == []
    assert {gap["source_id"] for gap in withheld["unresolved_required_citation_sources"]} == {
        jameson_id, williams_id,
    }
    assert all(
        gap["evidence_ids"] == [evidence_id]
        for gap in withheld["unresolved_required_citation_sources"]
    )


def test_v31_optional_body_scholarship_survives_draft_json_handoff():
    context = {
        "research_problem": {"research_question": "なぜか"},
        "section_contracts": [{
            "section_id": "s1", "article_role": "body", "claim_ids": ["c1"],
            "evidence_ids": ["e-local"],
            "development_expectations": {"required_argument_moves": []},
        }],
        "argument_nodes": [
            {"id": "e-local", "node_type": "evidence", "claim_id": "c1"},
            {"id": "e-book", "node_type": "evidence", "claim_id": "c1",
             "provenance": {"source_ids": ["book"]}},
        ],
        "evidence_context": [{
            "id": "book", "reference_id": "reference:book",
            "allowed_use": "attributed_scholarship_citation",
            "title": "A Book", "authors": ["Scholar"],
        }],
        "reference_entries": [{
            "reference_id": "reference:book", "title": "A Book",
            "authors": ["Scholar"],
        }],
    }
    projected = WorkerRunner._authoring_draft_contracts_with_scholarship(
        context, context["section_contracts"]
    )
    assert projected[0]["required_scholarship_evidence_ids"] == []
    assert projected[0]["evidence_ids"] == ["e-local"]
    draft = [{
        "section_id": "s1", "article_role": "body", "claim_ids": ["c1"],
        "evidence_ids": ["e-local"],
        "paragraphs": [{"text": "局所資料から論じる。", "evidence_ids": ["e-local"],
                        "move_indices": []}],
        "realized_evidence_ids": ["e-local"],
        "evidence_treatment": [{"evidence_id": "e-local", "basis": "memo_close_reading",
                                "verification_status": "unverified",
                                "prose_treatment": "qualified_interpretation",
                                "support_sentence": "局所資料から論じる。",
                                "citation_purpose": "", "attribution_sentence": "",
                                "purpose_clause": ""}],
    }]
    violations = WorkerRunner._authoring_draft_contract_violations(context, draft)
    assert not any("scholarship" in item for item in violations)
    mismatched_draft = deepcopy(draft)
    mismatched_draft[0]["evidence_ids"].append("stale-evidence")
    mismatched_violations = WorkerRunner._authoring_draft_contract_violations(
        context, mismatched_draft
    )
    assert any(
        "declared evidence differs from realized evidence" in item
        for item in mismatched_violations
    )
    labelled_draft = deepcopy(draft)
    labelled_draft[0]["paragraphs"][0]["text"] = "この段落の出力は因果命題である。"
    labelled_violations = WorkerRunner._authoring_draft_contract_violations(
        context, labelled_draft
    )
    assert any("section s1" in item and "この段落の出力は" in item
               for item in labelled_violations)
    scaffold_draft = deepcopy(draft)
    scaffold_draft[0]["paragraphs"][0]["text"] = "提示メモはこの点を示す。"
    scaffold_violations = WorkerRunner._authoring_draft_contract_violations(
        context, scaffold_draft
    )
    assert any("section s1" in item and "提示メモ" in item for item in scaffold_violations)
    assert WorkerRunner._authoring_reader_facing_v31_findings(scaffold_draft)
    repaired = deepcopy(draft)
    repaired[0]["evidence_ids"] = ["e-local", "e-book"]
    repaired[0]["realized_evidence_ids"] = ["e-local", "e-book"]
    repaired[0]["paragraphs"][0]["evidence_ids"] = ["e-local", "e-book"]
    repaired[0]["evidence_treatment"].append({
        "evidence_id": "e-book", "prose_treatment": "attributed_scholarship",
    })
    aligned, corrections = (
        WorkerRunner._authoring_drop_unrealized_optional_scholarship_metadata(
            context, repaired, repaired_section_ids={"s1"}
        )
    )
    assert aligned[0]["realized_evidence_ids"] == ["e-local"]
    assert aligned[0]["paragraphs"][0]["evidence_ids"] == ["e-local"]
    assert [item["evidence_id"] for item in aligned[0]["evidence_treatment"]] == [
        "e-local"
    ]
    assert corrections[0]["evidence_ids"] == ["e-book"]
    repaired[0]["paragraphs"][0]["text"] = "ScholarのA Bookで述べられた議論を用いる。"
    retained, corrections = (
        WorkerRunner._authoring_drop_unrealized_optional_scholarship_metadata(
            context, repaired, repaired_section_ids={"s1"}
        )
    )
    assert "e-book" in retained[0]["realized_evidence_ids"]
    assert not corrections

    introduction_context = deepcopy(context)
    introduction_context["section_contracts"][0]["article_role"] = "introduction"
    introduction_context["research_problem"]["prior_positions"] = []
    repaired_introduction = deepcopy(repaired)
    repaired_introduction[0]["article_role"] = "introduction"
    repaired_introduction[0]["paragraphs"][0]["text"] = "局所資料から論じる。"
    aligned, corrections = (
        WorkerRunner._authoring_drop_unrealized_optional_scholarship_metadata(
            introduction_context,
            repaired_introduction,
            repaired_section_ids={"s1"},
        )
    )
    assert aligned[0]["realized_evidence_ids"] == ["e-local"]
    assert aligned[0]["paragraphs"][0]["evidence_ids"] == ["e-local"]
    assert corrections[0]["policy"] == (
        "repaired-prose-uncited-optional-scholarship-v1"
    )
    repaired[0]["paragraphs"][0]["text"] = "局所資料から論じる。[^e-book]"
    retained, corrections = (
        WorkerRunner._authoring_drop_unrealized_optional_scholarship_metadata(
            context, repaired, repaired_section_ids={"s1"}
        )
    )
    assert "e-book" in retained[0]["realized_evidence_ids"]
    assert not corrections


def test_v31_required_body_scholarship_is_not_silently_dropped_during_repair():
    context = {
        "research_problem": {"research_question": "なぜか"},
        "authoring_atomic_evidence_contracts": [{
            "realization": "attribute_theory_with_citation",
            "claim_ids": ["c1"],
            "citation_evidence_ids": ["book"],
        }],
        "section_contracts": [{
            "section_id": "s1", "article_role": "body", "claim_ids": ["c1"],
            "evidence_ids": [],
            "development_expectations": {"required_argument_moves": []},
        }],
        "argument_nodes": [{
            "id": "e-book", "node_type": "evidence", "claim_id": "c1",
            "provenance": {"source_ids": ["book"]},
        }],
        "evidence_context": [{
            "id": "book", "reference_id": "reference:book",
            "allowed_use": "attributed_scholarship_citation",
            "title": "A Book", "authors": ["Scholar"],
        }],
        "reference_entries": [{
            "reference_id": "reference:book", "title": "A Book",
            "authors": ["Scholar"],
        }],
    }
    repaired = [{
        "section_id": "s1", "article_role": "body", "claim_ids": ["c1"],
        "evidence_ids": ["e-book"], "realized_evidence_ids": ["e-book"],
        "paragraphs": [{
            "text": "局所資料から論じる。", "evidence_ids": ["e-book"],
            "move_indices": [],
        }],
        "evidence_treatment": [{
            "evidence_id": "e-book",
            "prose_treatment": "attributed_scholarship",
            "citation_purpose": "先行議論を理論的な参照点とする",
            "attribution_sentence": "局所資料から論じる。",
            "purpose_clause": "理論的な参照点",
        }],
    }]

    [draft_contract] = WorkerRunner._authoring_draft_contracts_with_scholarship(
        context, context["section_contracts"]
    )
    assert draft_contract["required_scholarship_evidence_ids"] == ["e-book"]

    retained, corrections = (
        WorkerRunner._authoring_drop_unrealized_optional_scholarship_metadata(
            context, repaired, repaired_section_ids={"s1"}
        )
    )

    assert retained[0]["realized_evidence_ids"] == ["e-book"]
    assert retained[0]["paragraphs"][0]["evidence_ids"] == ["e-book"]
    assert corrections == []
    violations = WorkerRunner._authoring_attributed_citation_violations(
        context, retained[0], allowed_evidence_ids={"e-book"}
    )
    assert any("no unique sentence-level footnote anchor" in item for item in violations)


def test_v31_internal_support_anchors_do_not_create_multiple_footnotes():
    sentence = "家政・身体管理・情報収集を同じ人物が担う作品を比較する。"
    context = {
        "research_problem": {"research_question": "なぜか"},
        "section_contracts": [{
            "section_id": "s1", "article_role": "body", "claim_ids": ["c1"],
            "evidence_ids": ["e1", "e2"],
            "development_expectations": {"required_argument_moves": []},
        }],
        "argument_nodes": [
            {"id": "e1", "node_type": "evidence", "claim_id": "c1",
             "provenance": {"source": "memo", "verification_status": "unverified"}},
            {"id": "e2", "node_type": "evidence", "claim_id": "c1",
             "provenance": {"source": "memo", "verification_status": "unverified"}},
        ],
    }
    section = {
        "section_id": "s1", "article_role": "body", "claim_ids": ["c1"],
        "evidence_ids": ["e1", "e2"],
        "paragraphs": [{
            "text": sentence, "evidence_ids": ["e1", "e2"], "move_indices": [],
        }],
        "realized_evidence_ids": ["e1", "e2"],
        "evidence_treatment": [
            {
                "evidence_id": evidence_id,
                "claim_id": "c1",
                "basis": "memo_close_reading",
                "verification_status": "unverified",
                "prose_treatment": "qualified_interpretation",
                "support_sentence": sentence,
                "citation_purpose": "",
                "attribution_sentence": "",
                "purpose_clause": "",
            }
            for evidence_id in ("e1", "e2")
        ],
    }
    violations = WorkerRunner._authoring_draft_contract_violations(
        context, [section]
    )
    assert not any("support_sentence" in item for item in violations)
    assert not any("multiple footnotes" in item for item in violations)

    section["evidence_treatment"][0]["support_sentence"] = "本文にない文。"
    violations = WorkerRunner._authoring_draft_contract_violations(
        context, [section]
    )
    assert not any("support_sentence" in item for item in violations)

    section["evidence_treatment"][0]["support_sentence"] = ""
    violations = WorkerRunner._authoring_draft_contract_violations(
        context, [section]
    )
    assert not any("support_sentence" in item for item in violations)

    section["paragraphs"][0]["evidence_ids"].remove("e1")
    violations = WorkerRunner._authoring_draft_contract_violations(
        context, [section]
    )
    assert any(
        "evidence e1 is not linked to any generated paragraph" in item
        for item in violations
    )
    section["paragraphs"][0]["evidence_ids"].append("e1")

    legacy = deepcopy(section)
    legacy["evidence_treatment"][0].update({
        "support_sentence": "",
        "citation_purpose": "内部根拠の目的",
        "attribution_sentence": sentence,
        "purpose_clause": "家政・身体管理・情報収集",
    })
    normalized, corrections = WorkerRunner._authoring_normalize_evidence_treatments(
        context, [legacy]
    )
    treatment = normalized[0]["evidence_treatment"][0]
    assert treatment["support_sentence"] == sentence
    assert treatment["citation_purpose"] == ""
    assert treatment["attribution_sentence"] == ""
    assert treatment["purpose_clause"] == ""
    assert corrections

def test_v31_link_cleanup_keeps_citation_metadata_json_in_sync():
    sentence = "ウィリアムズ[『マルクス主義と文学』](https://example.org/book)を概念上の参照点とする。"
    context = {
        "language": "ja",
        "section_contracts": [{
            "section_id": "s1", "article_role": "body",
            "development_expectations": {"required_argument_moves": []},
        }],
    }
    result = WorkerRunner._authoring_normalize_generated_section_ids(
        context,
        [{
            "section_id": "s1", "article_role": "body",
            "paragraphs": [{"text": sentence, "move_indices": []}],
            "evidence_treatment": [{
                "evidence_id": "e1", "support_sentence": sentence,
                "attribution_sentence": sentence,
                "purpose_clause": sentence,
            }],
        }],
    )
    paragraph = result[0]["paragraphs"][0]["text"]
    treatment = result[0]["evidence_treatment"][0]
    assert paragraph == "ウィリアムズ『マルクス主義と文学』を概念上の参照点とする。"
    assert treatment["support_sentence"] == paragraph
    assert treatment["attribution_sentence"] == paragraph
    assert treatment["purpose_clause"] == paragraph


def test_v31_manual_footnote_marker_cleanup_keeps_citation_metadata_json_in_sync():
    sentence = "ラマールの二著作を比較する〔注1〕¹。"
    context = {
        "language": "ja",
        "section_contracts": [{
            "section_id": "s1", "article_role": "body",
            "development_expectations": {"required_argument_moves": []},
        }],
    }
    result = WorkerRunner._authoring_normalize_generated_section_ids(
        context,
        [{
            "section_id": "s1", "article_role": "body",
            "paragraphs": [{"text": sentence, "move_indices": []}],
            "evidence_treatment": [{
                "evidence_id": "e1", "support_sentence": sentence,
                "attribution_sentence": sentence,
                "purpose_clause": sentence,
            }],
        }],
    )
    paragraph = result[0]["paragraphs"][0]["text"]
    treatment = result[0]["evidence_treatment"][0]
    assert paragraph == "ラマールの二著作を比較する。"
    assert treatment["support_sentence"] == paragraph
    assert treatment["attribution_sentence"] == paragraph
    assert treatment["purpose_clause"] == paragraph


def test_v31_self_citation_requires_the_resolved_work_title():
    requirement = {
        "authors": ["山田花子"],
        "title": "架空資料の比較分析——『星の記録』における行為と竜",
        "is_self_authored": True,
    }
    matches = WorkerRunner._authoring_attribution_mentions_source
    assert not matches("『寄生獣』論を含む自著の議論も参照する。", requirement)
    assert not matches("自著『架空資料の比較分析』でも論じた。", requirement)
    assert not matches("筆者は『架空資料の比較分析』を参照する。", requirement)
    assert matches(
        "筆者は『架空資料の比較分析——『星の記録』における行為と竜』で論じた。",
        requirement,
    )
    # The V30 deterministic predicate allowlist remains unchanged; V31 uses the
    # separate semantic-only classifier below for this verb.
    assert not matches(
        "筆者は『架空資料の比較分析』で作品の意味を捉えた。",
        requirement,
    )


@pytest.mark.parametrize(
    ("classification", "accepted"),
    [("attribution", True), ("contextual_use", False), ("mere_reference", False)],
)
def test_v31_judges_only_the_citation_predicate_semantically(
    monkeypatch: pytest.MonkeyPatch,
    classification: str,
    accepted: bool,
):
    sentence = (
        "筆者は『架空資料の比較分析』で作品の意味を"
        + ("要素相互の配置から捉えた。" if accepted else "参照した。")
    )
    context = {
        "argument_nodes": [{
            "id": "e-self", "node_type": "evidence", "claim_id": "c1",
            "provenance": {"source_ids": ["source-self"]},
        }],
        "evidence_context": [{
            "id": "source-self", "reference_id": "r-self",
            "allowed_use": "attributed_scholarship_citation",
            "authors": ["自著"],
            "title": "架空資料の比較分析――『星の記録』",
        }],
        "reference_entries": [{
            "reference_id": "r-self", "is_self_authored": True,
            "original_text": "自著「架空資料の比較分析」",
        }],
    }
    section = {
        "section_id": "sec-magazine",
        "paragraphs": [{"text": sentence, "evidence_ids": ["e-self"]}],
        "evidence_ids": ["e-self"],
        "realized_evidence_ids": ["e-self"],
        "evidence_treatment": [{
            "evidence_id": "e-self",
            "prose_treatment": "attributed_scholarship",
            "citation_purpose": "先行する分析枠を本稿の比較に用いる",
            "attribution_sentence": "本文の述語を自然な文で表現する",
            "purpose_clause": "分析枠を比較軸として用いる",
        }],
    }
    runner = object.__new__(WorkerRunner)
    captured: dict[str, Any] = {}

    def judge(_self, _claimed, stage, _context, **kwargs):
        assert stage == "scholarship_attribution_judgment_v31"
        candidates = kwargs["payload"]["candidates"]
        captured["candidates"] = candidates
        return {
            "judgments": [{
                "candidate_id": item["candidate_id"],
                "classification": classification,
                "reason": "Predicate-level semantic judgment.",
            } for item in candidates]
        }

    monkeypatch.setattr(WorkerRunner, "_authoring_generate", judge)
    judgments = runner._authoring_v31_judge_attribution_predicates(
        SimpleNamespace(), context, [section]
    )
    violations = WorkerRunner._authoring_attributed_citation_violations(
        context,
        section,
        allowed_evidence_ids={"e-self"},
        attribution_predicate_judgments=judgments,
    )

    assert captured["candidates"][0]["sentence"] == sentence
    assert (violations == []) is accepted
    if not accepted:
        assert any("semantically confirmed attribution predicate" in item for item in violations)


@pytest.mark.parametrize(
    ("scope", "classification", "accepted"),
    [
        ("bibliographic_identity_only", "contextual_use", True),
        ("bibliographic_identity_only", "mere_reference", False),
        ("full_text_verified", "contextual_use", False),
    ],
)
def test_v31_bibliographic_citation_may_state_a_bounded_analytical_use(
    scope: str, classification: str, accepted: bool,
):
    sentence = (
        "ベッカー『アート・ワールド』を、制作の分業を比較するための"
        "理論的参照点として用いる。"
    )
    context = {
        "argument_nodes": [{
            "id": "e-book", "node_type": "evidence", "claim_id": "c1",
            "provenance": {"source_ids": ["citation-book"]},
        }],
        "evidence_context": [{
            "id": "citation-book", "reference_id": "reference:book",
            "allowed_use": "attributed_scholarship_citation",
            "verification_scope": scope,
            "authors": ["ベッカー"], "title": "アート・ワールド",
        }],
    }
    section = {
        "section_id": "s1",
        "paragraphs": [{"text": sentence, "evidence_ids": ["e-book"]}],
        "evidence_ids": ["e-book"],
        "realized_evidence_ids": ["e-book"],
        "evidence_treatment": [{
            "evidence_id": "e-book", "prose_treatment": "attributed_scholarship",
            "citation_purpose": "制作の分業を比較するために用いる",
            "attribution_sentence": sentence,
            "purpose_clause": "制作の分業を比較する視角に用いる",
        }],
    }
    candidate_id = WorkerRunner._authoring_v31_attribution_candidate_id(
        "s1", "e-book", 0, sentence
    )
    violations = WorkerRunner._authoring_attributed_citation_violations(
        context,
        section,
        allowed_evidence_ids={"e-book"},
        attribution_predicate_judgments={candidate_id: classification},
    )
    assert (violations == []) is accepted


def test_v31_repair_separates_merged_self_citation_and_projects_memo_attribution():
    evidence_id = "annotation-citation-node:self"
    other_citation_id = "annotation-citation:jameson"
    citation_id = "annotation-citation:self"
    source_span = (
        "自著「単独論考」の概念を雑誌へ転用。"
        "作品の意味は内部だけでなく配置によって構成される。"
    )
    context = {
        "argument_nodes": [{
            "id": evidence_id, "node_type": "evidence", "claim_id": "c1",
            "source_span_id": other_citation_id,
            "provenance": {"source_ids": [other_citation_id, citation_id]},
        }],
        "annotation_citation_evidence": [
            {
                "id": other_citation_id,
                "authors": ["フレドリック・ジェイムソン"],
                "title": "The Political Unconscious",
                "reference_id": "reference:jameson",
                "allowed_use": "attributed_scholarship_citation",
            },
            {
                "id": citation_id, "authors": ["自著"],
                "title": "単独論考――『作品』論」"
                "、『収録書』（2022） ・「別の論考」",
                "reference_id": "reference:self",
                "allowed_use": "attributed_scholarship_citation",
                "verification_scope": "bibliographic_identity_only",
            },
        ],
        "reference_entries": [
            {
                "reference_id": "reference:jameson",
                "title": "The Political Unconscious",
                "is_self_authored": False,
            },
            {
                "reference_id": "reference:self", "original_text": "自著「単独論考」",
                "title": "単独論考――『作品』論」"
                "、『収録書』（2022） ・「別の論考」",
                "container_title": "収録書", "year": "2022", "pages": "1-20",
                "url": "https://example.test/profile", "is_self_authored": True,
            },
        ],
        "audit_findings": [{
            "section_id": "s1", "category": "evidence_mismatch",
            "description": f"{evidence_id} のwork欄は複合文字列",
        }],
        "section_contracts": [{
            "section_id": "s1", "article_role": "body", "claim_ids": ["c1"],
        }],
        "authoring_atomic_evidence_contracts": [{
            "realization": "attribute_theory_with_citation",
            "claim_ids": ["c1"], "citation_evidence_ids": [citation_id],
            "assertion": "本稿で概念を転用する", "source_span": source_span,
        }],
        "propositions": [{
            "id": "p1", "text": "本稿で概念を転用する。",
            "source_span": source_span,
            "memo_annotation_kinds": ["self_citation"],
        }, {
            "id": "p2", "text": "作品の意味は配置によって構成される。",
            "source_span": source_span,
            "memo_annotation_kinds": ["self_citation"],
        }],
    }
    repaired, corrections = WorkerRunner._authoring_v31_repair_composite_self_citations(
        context
    )
    assert len(corrections) == 1
    assert corrections[0]["citation_evidence_id"] == citation_id
    assert repaired["reference_entries"][1]["title"] == "単独論考"
    reference = repaired["reference_entries"][1]
    assert reference["title"] == "単独論考"
    assert reference["container_title"] is None
    assert reference["year"] is None
    assert reference["pages"] is None
    assert reference["final_revision_blocked"] is True
    obligation = WorkerRunner._authoring_v31_required_scholarship_obligations(
        repaired, repaired["section_contracts"]
    )[0]
    assert obligation["work"] == "単独論考"
    assert obligation["provisional_attribution_sentence"] == (
        "筆者は『単独論考』で、作品の意味は配置によって構成されると論じた。"
    )
    assert obligation["provisional_attribution_basis_proposition_ids"] == ["p2"]
    assert obligation["provisional_attribution_requires_human_review"] is True

    no_audit, no_corrections = WorkerRunner._authoring_v31_repair_composite_self_citations(
        {**context, "audit_findings": []}
    )
    assert no_audit["reference_entries"][1]["title"] != "単独論考"
    assert no_corrections == []


def test_v31_self_citation_uses_the_resolved_record_in_draft_validation():
    context = {
        "argument_nodes": [{
            "id": "e-self", "node_type": "evidence", "claim_id": "c1",
            "provenance": {"source_ids": ["source-self"]},
        }],
        "evidence_context": [{
            "id": "source-self", "reference_id": "r-self",
            "allowed_use": "attributed_scholarship_citation",
            "authors": ["自著"],
            "title": "架空資料の比較分析／『寄生獣』論",
            "excerpt": "自著「架空資料の比較分析」",
        }],
        "reference_entries": [{
            "reference_id": "r-self", "is_self_authored": True,
            "original_text": "自著「架空資料の比較分析」",
        }],
    }
    requirement = WorkerRunner._authoring_attributed_citation_requirements(context)[
        "e-self"
    ]
    assert requirement["is_self_authored"] is True
    assert requirement["title"] == "架空資料の比較分析"
    assert not WorkerRunner._authoring_attribution_mentions_source(
        "『寄生獣』論を含む自著の議論も参照する。", requirement
    )
    assert WorkerRunner._authoring_attribution_mentions_source(
        "筆者は『架空資料の比較分析』でこの問題を論じた。",
        requirement,
    )
    findings = WorkerRunner._authoring_reader_facing_scaffold_findings([
        {"section_id": "s1", "text": "自著の議論も参考にする。"}
    ])
    assert findings == []


def test_v31_keeps_argument_labels_out_of_manuscript_prose():
    labelled = (
        "ここで確立できるのは、誌面の配置という点である。"
        "本稿の主張は読者の反応が変化したという因果命題ではなく、"
        "作品の見え方に関わるという範囲で成立する。"
    )
    direct = (
        "誌面の並びが示すのは、読者の反応が実際に変化したかどうかではなく、"
        "作品が隣の作品との関係でどう見えるかということである。"
    )
    findings = WorkerRunner._authoring_reader_facing_v31_findings(
        [{"section_id": "s02", "text": labelled}]
    )
    assert len(findings) == 1
    assert findings[0]["object_id"] == "s02"
    assert "対象との関係" in findings[0]["message"]
    assert not WorkerRunner._authoring_reader_facing_v31_findings(
        [{"section_id": "s02", "text": direct}]
    )
    assert not WorkerRunner._authoring_reader_facing_v31_findings(
        [{"section_id": "s02", "text": "媒体編成が歴史変化を主導したという因果関係ではない。"}]
    )
    assert WorkerRunner._authoring_reader_facing_meta_prose_findings(
        [{
            "section_id": "s02", "text": "導入文。",
            "paragraphs": [{"text": "この段落の出力は因果命題である。"}],
        }]
    )
    for phrase in (
        "この段落の出力は因果命題である。",
        "本稿の貢献目標は比較である。",
        "入力メモから本文を生成した。",
        "検証ステータスは未確認である。",
    ):
        assert WorkerRunner._authoring_reader_facing_meta_prose_findings(
            [{"section_id": "s02", "text": phrase}]
        )
    for phrase in (
        "雑誌とSNSに共通する因果機構があるとはいえない。",
        "媒体レギュレーションを作業概念として用いる。",
        "媒体レギュレーションの適用範囲を限定できる。",
        "作品が慣行を変えたという因果方向は確定しない。",
        "本稿では二つの作品を比較し、技能の評価と賃金の違いを検討する。",
    ):
        assert not WorkerRunner._authoring_reader_facing_meta_prose_findings(
            [{"section_id": "s02", "text": phrase}]
        )
    for phrase in (
        "候補作の一覧には採否の異なる版がある。",
        "草稿と生ログを照合する。",
        "この作品を調査候補とする。",
        "この関係は検証候補である。",
        "掲載記録が確認できるまで保留される。",
        "この構成が原作のどの場面に見られるかは確認されていない。",
    ):
        assert WorkerRunner._authoring_reader_facing_scaffold_findings(
            [{"section_id": "s02", "text": phrase}]
        )
    for stage in ("draft_body_v31", "polish_v31", "japanese_paragraph_redraft_v31"):
        assert "not a checklist to define item by item" in STAGE_GUIDANCE[stage]
        assert "Explain a theorist's idea and proceed naturally" in STAGE_GUIDANCE[stage]
    assert "a surname alone does not preserve" in STAGE_GUIDANCE["polish_v31"]
    assert "technical terms" in STAGE_GUIDANCE["audit_v31"]
    assert "pre_footnote_render" in STAGE_GUIDANCE["audit_v31"]
    assert "support_sentence" in STAGE_GUIDANCE["audit_v31"]
    assert "internal provenance" in STAGE_GUIDANCE["audit_v31"]
    for stage in (
        "draft_introduction_v31",
        "draft_body_v31",
        "draft_conclusion_v31",
    ):
        assert "not an exact-match locator" in STAGE_GUIDANCE[stage]
        assert "Paragraph-level evidence_ids are the authoritative link" in (
            STAGE_GUIDANCE[stage]
        )
        assert "copy one exact claim-bearing sentence" not in STAGE_GUIDANCE[stage]
    assert "copy one exact claim-bearing sentence" in STAGE_GUIDANCE["draft"]
    assert "evidence_treatment_gap" in STAGE_GUIDANCE["audit_v31"]
    assert "evidence_mismatch" in STAGE_GUIDANCE["audit_v31"]
    assert "does not authorize substituting" in STAGE_GUIDANCE["audit_v31"]
    assert "comparison procedure" in STAGE_GUIDANCE["audit_v31"]
    assert "A supplied replacement sentence" in STAGE_GUIDANCE["audit_v31"]
    assert "local_section, not plan_revision" in STAGE_GUIDANCE["audit_v31"]
    assert "final human review" in STAGE_GUIDANCE["audit_v31"]
    assert "prior_audit_resolutions" in STAGE_GUIDANCE["audit_v31"]
    assert "must not instruct the repair to assert it" in STAGE_GUIDANCE["audit_v31"]
    assert "every remaining actionable defect" in STAGE_GUIDANCE["audit_v31"]
    assert "animation film" in STAGE_GUIDANCE["draft_body_v31"]
    assert "grounded counterexample" in STAGE_GUIDANCE["draft_body_v31"]
    assert "Only references outside required_scholarship_evidence_ids are optional" in (
        STAGE_GUIDANCE["draft_body_v31"]
    )
    assert "Every reference inside required_scholarship_evidence_ids is a deliberate" in (
        STAGE_GUIDANCE["draft_body_v31"]
    )
    assert "neither a prose quota nor an exclusive list" in STAGE_GUIDANCE[
        "section_contracts_v31"
    ]
    for stage in (
        "draft_introduction_v31",
        "draft_body_v31",
        "draft_conclusion_v31",
    ):
        assert "Move indices are optional metadata" in STAGE_GUIDANCE[stage]
        assert "leave them empty otherwise" in STAGE_GUIDANCE[stage]
        assert "Do not add or reshape prose to cover a move list" in STAGE_GUIDANCE[stage]
    assert "restart_findings" in STAGE_GUIDANCE["draft"]
    assert "propositionally mismatched evidence" in STAGE_GUIDANCE["draft"]
    assert "authorial comparison procedure" in STAGE_GUIDANCE["draft_body_v31"]
    assert "grammatical roles precise" in STAGE_GUIDANCE["draft_conclusion_v31"]


def test_v31_role_metadata_is_copied_and_only_unique_known_ids_are_recovered():
    context = {
        "research_problem": {
            "research_question": "作業概念の適用範囲は何か。",
            "provisional_answer": "技能の評価は賃金と一致するとは限らない。",
        },
        "argument_nodes": [
            {"id": "b06-warrant:c0022:recognition-within-order", "node_type": "warrant"},
            {"id": "e-a_b", "node_type": "evidence"},
            {"id": "e-a-b", "node_type": "evidence"},
        ],
        "discourse_nodes": [{"section_id": "s1", "claim_ids": ["claim-1"]}],
    }
    result = {
        "introduction_contract": {
            "research_question": "言い換えた問い",
            "provisional_answer": "短縮した答え",
        },
        "conclusion_contract": {"research_question": "言い換えた問い"},
        "sections": [{
            "section_id": "s1",
            "claim_ids": ["claim_1"],
            "warrant_ids": ["b06-warrant:c0022:recognition-within_order"],
            "evidence_ids": ["e_a-b"],
        }],
    }
    corrections = WorkerRunner._authoring_normalize_v31_locked_role_fields(
        context, result
    )
    assert result["introduction_contract"]["research_question"] == (
        context["research_problem"]["research_question"]
    )
    assert result["introduction_contract"]["provisional_answer"] == (
        context["research_problem"]["provisional_answer"]
    )
    assert result["conclusion_contract"]["research_question"] == (
        context["research_problem"]["research_question"]
    )
    assert result["sections"][0]["claim_ids"] == ["claim-1"]
    assert result["sections"][0]["warrant_ids"] == [
        "b06-warrant:c0022:recognition-within-order"
    ]
    # Two known evidence IDs have the same compact spelling; their identity
    # cannot be inferred from the generated value.
    assert result["sections"][0]["evidence_ids"] == ["e_a-b"]
    assert len(corrections) == 5


def test_v31_enumeration_context_carries_claims_and_candidate_items():
    runner = object.__new__(WorkerRunner)
    context = {
        "section_contracts": [{
            "section_id": "s1",
            "section_output": "技能の評価と賃金は一致するとは限らない。",
            "claim_ids": ["c1"],
            "evidence_ids": ["e1"],
            "qualifiers": ["賃金の記録がある場合に限る"],
            "counterarguments": ["技能評価が賃金を決めるという反論"],
        }],
        "claims": [{
            "id": "c1", "text": "工程・分業・承認を検討する。",
            "proposition_id": "p1",
        }],
        "propositions": [{"id": "p1", "text": "工程・賃金・分業・承認が描かれる。"}],
        "argument_nodes": [{
            "id": "e1", "node_type": "evidence",
            "text": "工程が分業へ移る場面を記録する。",
        }],
    }
    updated = runner._authoring_resolve_enumeration_context(
        SimpleNamespace(workflow_version="31"), context
    ).context
    record = updated["enumeration_context_contracts"][0]
    assert record["claim_statements"] == [{
        "source_field": "claim", "source_id": "c1",
        "text": "工程・分業・承認を検討する。",
    }]
    assert record["enumeration_candidates"][0]["source_id"] == "c1"
    assert {item["source_id"] for item in record["enumeration_candidates"]} == {
        "c1", "p1"
    }
    assert any(
        item["source_id"] == "e1" and item["source_field"] == "evidence"
        for item in record["source_statements"]
    )
    assert record["qualifications"] == ["賃金の記録がある場合に限る"]
    assert record["counterarguments"] == ["技能評価が賃金を決めるという反論"]
    assert (
        record["enumeration_candidates"][0]["relationship_status"]
    ) == "diagnostic_only_not_an_explanation_order"


def test_v31_citation_render_fallback_preserves_unaffected_book_tags():
    sections = [{
        "section_id": "s1",
        "paragraphs": [
            {
                "paragraph_id": "p1", "text": "『別の作品』を参照する。",
                "citation_mentions": [{
                    "reference_id": "ref-good", "text": "『別の作品』",
                }],
            },
            {
                "paragraph_id": "p2", "text": "『AB』を参照する。",
                "citation_mentions": [
                    {"reference_id": "ref-long", "text": "『AB』"},
                    {"reference_id": "ref-short", "text": "AB"},
                ],
            },
        ],
    }]
    safe, findings = WorkerRunner._authoring_isolate_unrenderable_citation_mentions(
        sections
    )
    assert safe[0]["paragraphs"][0]["citation_mentions"] == (
        sections[0]["paragraphs"][0]["citation_mentions"]
    )
    assert safe[0]["paragraphs"][1]["citation_mentions"] == []
    assert safe[0]["paragraphs"][1]["unrenderable_citation_mentions"] == (
        sections[0]["paragraphs"][1]["citation_mentions"]
    )
    assert sections[0]["paragraphs"][1]["citation_mentions"]
    assert findings[0]["paragraph_id"] == "p2"
    assert findings[0]["required_reference_ids"] == ["ref-long", "ref-short"]


def test_v31_body_output_must_be_an_established_result_not_a_research_task():
    procedural = [
        {
            "section_id": "body-1",
            "article_role": "body",
            "section_output": "読者の反応を判断するために必要な資料を特定する。",
        },
        {
            "section_id": "body-2",
            "article_role": "body",
            "section_output": "発見経路と評価対象を区別する分析上の手順を示す。",
        },
    ]
    substantive = [
        {
            "section_id": "body-1",
            "article_role": "body",
            "section_output": (
                "誌面上の隣接は作品を共通の比較枠に置くが、読者反応の変化までは"
                "それだけでは説明しない。"
            ),
        }
    ]

    assert len(body_section_output_problems(procedural)) == 2
    assert not body_section_output_problems(substantive)


def test_v31_reader_facing_gate_rejects_prospectus_dominated_body():
    findings = WorkerRunner._authoring_reader_facing_prospectus_findings(
        [
            {
                "section_id": "body-1",
                "article_role": "body",
                "text": (
                    "今後、掲載号を確認する。別途、作品の配置を比較する。"
                    "今後、読者資料を収集する。将来、受容の変化を検証する。"
                ),
            }
        ]
    )
    direct = WorkerRunner._authoring_reader_facing_prospectus_findings(
        [
            {
                "section_id": "body-1",
                "article_role": "body",
                "text": (
                    "同じ誌面に置かれた作品は、題材が異なっていても相互の差異を"
                    "見せる配置を作る。読者の反応が変化したかは、この配置だけからは"
                    "分からない。"
                ),
            }
        ]
    )
    projected_paragraphs = [
        {"text": "『雄獅少年』では学校と労働の描写を検討する。"},
        {"text": "『不滅のあなたへ』では主人公の設定を検討する。"},
        {"text": "日本の雑誌掲載作の掲載条件を別に照合する。"},
        {"text": "二作の差だけでは媒体の慣行を示せない。"},
        {"text": "年齢による比較の前提は作品ごとに異なる。"},
        {"text": "学校と労働は異なる生活領域である。"},
        {"text": "主人公の性質も比較条件に含まれる。"},
        {"text": "媒体条件と作品設定を区別する必要がある。"},
    ]
    projected = WorkerRunner._authoring_reader_facing_prospectus_findings(
        [
            {
                "section_id": "body-2",
                "article_role": "body",
                "text": "".join(item["text"] for item in projected_paragraphs),
                "paragraphs": projected_paragraphs,
            }
        ]
    )

    assert findings and findings[0]["object_id"] == "body-1"
    assert not direct
    assert not projected
    assert not WorkerRunner._authoring_reader_facing_prospectus_findings([{
        "section_id": "body-active",
        "article_role": "body",
        "text": (
            "本稿では雑誌の掲載号を確認する。二作品の配置を比較する。"
            "次に読者の反応を検討する。媒体条件との違いを整理する。"
        ),
    }])
    assert "framework for examining" in STAGE_GUIDANCE["draft_conclusion_v31"]
    for stage in ("draft_introduction_v31", "draft_body_v31"):
        assert "メモは" in STAGE_GUIDANCE[stage]
    assert "reference candidate without a checked" in STAGE_GUIDANCE[
        "draft_introduction_v31"
    ]
    assert WorkerRunner._authoring_reader_facing_scaffold_findings(
        [{"section_id": "s02", "text": "出発点となるメモは比較を求める。"}]
    )


def test_v31_final_html_gate_recovers_article_roles_before_prospectus_audit():
    sections = WorkerRunner._authoring_reader_facing_sections_from_html(
        """
        <article>
          <section id="intro">
            <h2 data-authoring-role="introduction">はじめに</h2>
            <p>今後、掲載号を確認する。別途、作品の配置を比較する。将来、読者資料を検討する。</p>
          </section>
          <section id="body">
            <h2 data-authoring-role="body">本論</h2>
            <p>今後、掲載号を確認する。別途、作品の配置を比較する。将来、読者資料を検討する。</p>
          </section>
        </article>
        """
    )

    assert [section["article_role"] for section in sections] == [
        "introduction",
        "body",
    ]
    findings = WorkerRunner._authoring_reader_facing_prospectus_findings(sections)
    assert [finding["object_id"] for finding in findings] == ["body"]


def test_v31_final_html_gate_checks_prose_and_generated_bibliography_separately():
    sections = WorkerRunner._authoring_reader_facing_sections_from_html(
        '<article><section id="body"><p>因果関係ではない。</p></section>'
        '<section id="notes" data-citation-section="evidence-footnotes-v1">'
        '<p>山田花子「作品」［書誌種別・刊行情報要確認］。</p></section>'
        '<section id="refs" data-reference-section="consulted-generated-v22">'
        '<p>山田花子「作品」。</p></section></article>'
    )
    assert not WorkerRunner._authoring_reader_facing_v31_findings(sections)
    sections[1]["text"] = "自著「作品」［書誌種別・刊行情報要確認］。"
    findings = WorkerRunner._authoring_reader_facing_v31_findings(sections)
    assert [item["object_id"] for item in findings] == ["notes"]


def test_v31_proofreading_rejects_only_new_scaffold_not_valid_causal_prose():
    runner = WorkerRunner.__new__(WorkerRunner)
    check = sentence_proofreading._introduces_reader_facing_scaffold
    assert not check(runner, "p1", "因果ではない。", "因果関係ではない。")
    assert not check(runner, "p1", "筆者の議論を参照する。", "自著の議論を参照する。")
    assert check(runner, "p1", "作品について論じる。", "入力メモでは作品について論じる。")


def test_v31_audit_uses_current_graph_and_marks_pre_footnote_stage(monkeypatch):
    runner = object.__new__(WorkerRunner)
    plan = SimpleNamespace(id="plan", revision=1)
    repository = SimpleNamespace(
        load_plan=lambda *args: plan,
        list_audit_findings=lambda *args: [],
        add_audit_finding=lambda *args: None,
    )
    monkeypatch.setattr(
        "fukamiru.worker.runner.AuthoringRepository", lambda state: repository
    )
    monkeypatch.setattr("fukamiru.worker.runner.audit_plan", lambda payload: [])
    runner._audit_payload = lambda value: {}
    runner._authoring_audit_llm_payload = lambda value: {
        "argument_nodes": [{"id": "stale"}],
        "argument_edges": [{"source_id": "stale", "target_id": "c1"}],
        "section_contracts": [],
    }
    runner._authoring_reader_facing_v31_findings = lambda sections: []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def execute(self, *args):
            raise AssertionError("no finding should write a record")

        def commit(self):
            pass

    runner.databases = SimpleNamespace(
        state=SimpleNamespace(connect=lambda: Connection())
    )
    captured = {}

    def generate(claimed, stage, payload):
        captured.update(payload)
        return {"findings": []}

    runner._authoring_generate = generate
    context = {
        "plan_id": "plan",
        "plan_revision": 1,
        "section_contracts": [{
            "section_id": "s1", "article_role": "body", "claim_ids": ["c1"],
            "evidence_ids": [],
        }],
        "authoring_repair_evidence_allowances": [{
            "section_id": "s1", "claim_id": "c1", "node_id": "repair-evidence",
        }],
        "argument_nodes": [
            {
                "id": "current",
                "node_type": "claim",
                "claim_id": "c1",
                "proposition_id": "p1",
                "provenance": {"source_ids": ["p1"]},
            },
            {
                "id": "repair-evidence",
                "node_type": "evidence",
                "claim_id": "c1",
                "proposition_id": "p-repair",
                "provenance": {"source": "memo", "source_ids": ["p-repair"]},
            },
            {
                "id": "annotation-citation-node:wrong",
                "node_type": "evidence",
                "claim_id": "c1",
                "proposition_id": "p2",
                "source_span_id": "annotation-citation:wrong",
                "provenance": {"source_ids": ["annotation-citation:wrong"]},
            },
        ],
        "argument_edges": [
            {
                "id": "annotation-citation-edge:wrong",
                "source_id": "annotation-citation-node:wrong",
                "target_id": "current",
                "relation": "supports",
            }
        ],
        "annotation_citation_evidence": [
            {
                "id": "annotation-citation:wrong",
                "claim_ids": ["c1"],
                "annotation_proposition_ids": ["p2"],
            }
        ],
        "draft_sections": [],
        "research_problem": {"research_question": "問い"},
        "authoring_audit_resolution_history": [{
            "object_id": "s1",
            "auditor_instruction": "根拠のない石への帰属を外す。",
        }],
    }

    runner._authoring_audit(
        SimpleNamespace(
            workflow_version="31", run_id="run", node_run_id="audit"
        ),
        context,
    )

    assert captured["plan"]["argument_nodes"] == context["argument_nodes"]
    assert captured["plan"]["argument_edges"] == []
    assert not any(
        str(item["id"]).startswith("annotation-citation-node:")
        for item in captured["plan"]["argument_nodes"]
    )
    assert captured["plan"]["section_contracts"][0]["evidence_ids"] == [
        "repair-evidence"
    ]
    assert captured["render_stage"] == "pre_footnote_render"
    assert captured["target_section_ids"] == ["s1"]
    assert captured["target_section_roles"] == [{
        "section_id": "s1",
        "article_role": "body",
    }]
    assert captured["prior_audit_resolutions"] == [
        {
            "object_id": "s1",
            "auditor_instruction": "根拠のない石への帰属を外す。",
        }
    ]


def test_v31_audit_schema_requires_a_review_ledger_for_every_section():
    schema = STAGE_SCHEMAS["audit_v31"]

    assert "section_reviews" in schema["required"]
    review = schema["properties"]["section_reviews"]["items"]
    assert set(review["required"]) == {
        "section_id",
        "article_role",
        "status",
        "finding_ids",
    }


def test_v31_candidate_reconciliation_drops_previous_audit_review_ledger():
    runner = object.__new__(WorkerRunner)
    runner._authoring_v29_plan_bridge = lambda context, html, **_kwargs: {
        **context,
        "proposed_html": html,
        "structure_plan_validation": {"valid": True},
        "authoring_citation_lineage_reconciliation": {"dropped_bindings": []},
        "authoring_paragraph_citation_lineage": [],
    }
    runner._authoring_persistence_metadata = lambda context: context
    result = runner._authoring_reconcile_candidate_context(
        SimpleNamespace(),
        {
            "proposed_html": "<article></article>",
            "audit_findings": [{"code": "old"}],
            "authoring_audit_section_reviews": [{
                "section_id": "s1",
                "article_role": "body",
                "status": "clean",
                "finding_ids": [],
            }],
        },
    ).context

    assert "audit_findings" not in result
    assert "authoring_audit_section_reviews" not in result


def test_v31_candidate_reconciliation_preserves_claim_source_identity():
    runner = object.__new__(WorkerRunner)
    runner._authoring_v29_plan_bridge = lambda _context, _html, **_kwargs: {
        "structure_plan_validation": {"valid": True},
        "authoring_citation_lineage_reconciliation": {"dropped_bindings": []},
        "authoring_paragraph_citation_lineage": [],
    }
    runner._authoring_persistence_metadata = lambda context: context
    fixed = build_fixed_assets({
        "evidence_context": [
            {"id": "SRC-1", "text": "根拠一", "verification_status": "verified"},
            {"id": "SRC-2", "text": "根拠二", "verification_status": "verified"},
        ],
    })
    context = {
        "proposed_html": "<article></article>",
        "fixed_assets": fixed.model_dump(mode="json"),
        "evidence_uses": [{
            "evidence_use_id": "EU-1",
            "section_id": "S1",
            "claim_id": "C1",
            "sentence_id": "S1-P01-S01",
            "source_ids": ["SRC-1"],
            "claim_role": "source_fact",
            "evidence_kind": "bibliographic_record",
            "verification_status": "verified",
            "support_locator": {},
            "supported_content": "根拠一",
            "author_inference": None,
        }],
    }

    reconciled = runner._authoring_reconcile_candidate_context(
        SimpleNamespace(), context
    ).context
    assert reconciled["proofreading_evidence_identity_validation"]["passed"] is True

    changed_use = {
        **reconciled["evidence_uses"][0],
        "source_ids": ["SRC-2"],
    }
    with pytest.raises(FukamiruError, match="claim/source evidence identity"):
        runner._authoring_reconcile_candidate_context(
            SimpleNamespace(),
            {**reconciled, "evidence_uses": [changed_use]},
        )


def test_v31_audit_checkpoint_requires_ordered_section_coverage():
    payload = {
        "target_section_ids": ["intro", "body", "conclusion"],
        "target_section_roles": [
            {"section_id": "intro", "article_role": "introduction"},
            {"section_id": "body", "article_role": "body"},
            {"section_id": "conclusion", "article_role": "conclusion"},
        ],
    }
    complete = {
        "findings": [{
            "id": "f1",
            "severity": "major",
            "finding_type": "evidence_mismatch",
            "message": "根拠が対応していない。",
            "affected_claim_ids": [],
            "affected_section_ids": ["body"],
            "repair_scope": "local_section",
        }],
        "section_reviews": [
            {
                "section_id": "intro",
                "article_role": "introduction",
                "status": "clean",
                "finding_ids": [],
            },
            {
                "section_id": "body",
                "article_role": "body",
                "status": "findings",
                "finding_ids": ["f1"],
            },
            {
                "section_id": "conclusion",
                "article_role": "conclusion",
                "status": "clean",
                "finding_ids": [],
            },
        ],
    }

    assert WorkerRunner._authoring_validate_checkpoint_result(
        "audit_v31", payload, complete
    )["valid"] is True

    incomplete = deepcopy(complete)
    incomplete["section_reviews"].pop(1)
    with pytest.raises(FukamiruError, match="ordered target section coverage"):
        WorkerRunner._authoring_validate_checkpoint_result(
            "audit_v31", payload, incomplete
        )

    unlinked = deepcopy(complete)
    unlinked["section_reviews"][1] = {
        **unlinked["section_reviews"][1],
        "status": "clean",
        "finding_ids": [],
    }
    with pytest.raises(FukamiruError, match="not fully represented"):
        WorkerRunner._authoring_validate_checkpoint_result(
            "audit_v31", payload, unlinked
        )

    wrong_role = deepcopy(complete)
    wrong_role["section_reviews"][1]["article_role"] = "conclusion"
    with pytest.raises(FukamiruError, match="target article role"):
        WorkerRunner._authoring_validate_checkpoint_result(
            "audit_v31", payload, wrong_role
        )


def test_v31_audit_does_not_turn_internal_support_anchors_into_footnotes():
    finding = {
        "message": "citation_purpose と purpose_clause が空欄である。",
        "affected_section_ids": ["s1"],
    }
    internal = {
        "draft_sections": [{
            "section_id": "s1",
            "evidence_treatment": [{
                "evidence_id": "e1",
                "prose_treatment": "qualified_interpretation",
                "support_sentence": "内部根拠を対応づける。",
                "citation_purpose": "",
                "attribution_sentence": "",
                "purpose_clause": "",
            }],
        }],
    }
    assert WorkerRunner._authoring_invalid_internal_footnote_audit_finding(
        internal, finding
    )
    scholarship = deepcopy(internal)
    scholarship["draft_sections"][0]["evidence_treatment"][0][
        "prose_treatment"
    ] = "attributed_scholarship"
    assert not WorkerRunner._authoring_invalid_internal_footnote_audit_finding(
        scholarship, finding
    )


def test_v31_draft_allows_multiple_sources_for_one_grouped_note_clause():
    sentence = "甲『第一作』と乙『第二作』の議論を比較する。"
    context = {
        "argument_nodes": [
            {"id": "e1", "node_type": "evidence", "claim_id": "c1",
             "provenance": {"source_ids": ["s1"]}},
            {"id": "e2", "node_type": "evidence", "claim_id": "c1",
             "provenance": {"source_ids": ["s2"]}},
        ],
        "evidence_context": [
            {"id": "s1", "allowed_use": "attributed_scholarship_citation",
             "authors": ["甲"], "title": "第一作"},
            {"id": "s2", "allowed_use": "attributed_scholarship_citation",
             "authors": ["乙"], "title": "第二作"},
        ],
    }
    section = {
        "section_id": "s", "paragraphs": [{
            "text": sentence, "evidence_ids": ["e1", "e2"]
        }],
        "evidence_ids": ["e1", "e2"],
        "realized_evidence_ids": ["e1", "e2"],
        "evidence_treatment": [
            {"evidence_id": eid, "prose_treatment": "attributed_scholarship",
             "citation_purpose": "理論枠を比較軸として使う",
             "attribution_sentence": "各著者の議論を比較枠として紹介する",
             "purpose_clause": "両者の枠組みを比較軸として参照する"}
            for eid in ("e1", "e2")
        ],
    }
    issues = WorkerRunner._authoring_attributed_citation_violations(
        context, section, allowed_evidence_ids={"e1", "e2"}
    )
    assert issues == []


def test_v31_draft_requires_source_in_visible_anchor_not_only_metadata():
    context = {
        "argument_nodes": [{
            "id": "e1", "node_type": "evidence", "claim_id": "c1",
            "provenance": {"source_ids": ["s1"]},
        }],
        "evidence_context": [{
            "id": "s1", "allowed_use": "attributed_scholarship_citation",
            "authors": ["佐藤花子"], "title": "比較の理論",
            "excerpt": "「配置の変化」",
        }],
    }
    section = {
        "section_id": "s1",
        "paragraphs": [{
            "text": "「配置の変化」は分析枠として有効である。",
            "evidence_ids": ["e1"],
        }],
        "evidence_ids": ["e1"],
        "realized_evidence_ids": ["e1"],
        "evidence_treatment": [{
            "evidence_id": "e1", "prose_treatment": "attributed_scholarship",
            "citation_purpose": "議論を比較するために用いる",
            "attribution_sentence": "佐藤花子『比較の理論』を比較枠として紹介する。",
            "purpose_clause": "比較軸として参照する",
        }],
    }

    violations = WorkerRunner._authoring_attributed_citation_violations(
        context, section, allowed_evidence_ids={"e1"}
    )

    assert any("does not identify its author or work" in item for item in violations)


def test_v31_negative_only_scholarship_sentence_is_rejected():
    sentence = "ウィリアムズ『マルクス主義と文学』も個々の号を示す資料にはならない。"
    context = {
        "argument_nodes": [{
            "id": "e1", "node_type": "evidence", "claim_id": "c1",
            "provenance": {"source_ids": ["source-1"]},
        }],
        "evidence_context": [{
            "id": "source-1", "allowed_use": "attributed_scholarship_citation",
            "authors": ["ウィリアムズ"], "title": "マルクス主義と文学",
        }],
    }
    section = {
        "section_id": "s1",
        "paragraphs": [{"text": sentence, "evidence_ids": ["e1"]}],
        "realized_evidence_ids": ["e1"],
        "evidence_treatment": [{
            "evidence_id": "e1", "prose_treatment": "attributed_scholarship",
            "citation_purpose": "個々の号を示す資料にはならない",
            "attribution_sentence": sentence,
            "purpose_clause": "個々の号を示す資料にはならない",
        }],
    }
    violations = WorkerRunner._authoring_attributed_citation_violations(
        context, section, allowed_evidence_ids={"e1"}
    )
    assert any("negative or deferred citation purpose" in item for item in violations)


def test_v31_role_text_handoff_allows_natural_paraphrase_but_requires_role_prose():
    data = _handoff_fixture()
    context = {"research_problem": data["problem"].model_dump(mode="json")}
    sections = [
        {
            "article_role": "introduction",
            "text": "本稿では、当時の媒体配置が作品の受容にどう関わったかを検討する。",
        },
        {
            "article_role": "conclusion",
            "text": data["synthesis"]["final_answer"],
        },
    ]
    assert WorkerRunner._authoring_v31_role_text_violations(
        context, sections, data["synthesis"]
    ) == []
    sections[0]["text"] = ""
    assert "Introduction prose is empty" in WorkerRunner._authoring_v31_role_text_violations(
        context, sections, data["synthesis"]
    )[0]


def test_v31_citation_ids_are_recovered_for_multiple_visible_work_titles():
    sections = [{
        "section_id": "sec-body",
        "paragraphs": [{
            "paragraph_id": "P-body-1",
            "text": (
                "ジェイムソン『Postmodernism』と"
                "ウィリアムズ『Marxism and Literature』を比較する。"
            ),
            "evidence_ids": ["ev-j", "ev-w"],
            "citation_mentions": [{"reference_id": "wrong", "text": "別の書名"}],
        }],
    }]
    anchors = [
        {"section_id": "sec-body", "reference_id": "ref-j", "work": "Postmodernism",
         "evidence_id": "ev-j", "citation_evidence_id": "cite-j"},
        {"section_id": "sec-body", "reference_id": "ref-w", "work": "Marxism and Literature",
         "evidence_id": "ev-w", "citation_evidence_id": "cite-w"},
    ]
    updated, findings, corrections = recover_citation_mentions(sections, anchors)
    mentions = updated[0]["paragraphs"][0]["citation_mentions"]
    assert findings == []
    assert {item["reference_id"] for item in mentions} == {"ref-j", "ref-w"}
    assert {item["text"] for item in mentions} == {
        "『Postmodernism』", "『Marxism and Literature』"
    }
    assert {item["reference_id"] for item in corrections} == {"ref-j", "ref-w"}
    rendered = render_canonical_html(
        plan_id="plan",
        sections=[{"section_id": "sec-body", "title": "本文", **updated[0]}],
    )
    assert '<cite data-reference-id="ref-j">『Postmodernism』</cite>' in rendered
    assert '<cite data-reference-id="ref-w">『Marxism and Literature』</cite>' in rendered


def test_v31_ambiguous_or_missing_title_keeps_evidence_and_creates_review_item():
    sections = [{
        "section_id": "sec-body",
        "paragraphs": [{
            "paragraph_id": "P-body-1", "text": "この議論を比較に用いる。",
            "evidence_ids": ["ev-a", "ev-b"], "citation_mentions": [],
        }],
    }]
    anchors = [
        {"section_id": "sec-body", "reference_id": "ref-a", "work": "Shared Title",
         "evidence_id": "ev-a", "citation_evidence_id": "cite-a"},
        {"section_id": "sec-body", "reference_id": "ref-b", "work": "Shared Title",
         "evidence_id": "ev-b", "citation_evidence_id": "cite-b"},
    ]
    updated, findings, _ = recover_citation_mentions(sections, anchors)
    assert updated[0]["paragraphs"][0]["evidence_ids"] == ["ev-a", "ev-b"]
    assert updated[0]["paragraphs"][0]["citation_mentions"] == []
    assert findings[0]["required_reference_ids"] == ["ref-a", "ref-b"]
    review = WorkerRunner._authoring_v31_record_citation_mapping_review(
        {"human_review_items": [], "required_user_information": []}, findings
    )
    assert review["human_review_items"][0]["blocks_render"] is True
    assert review["human_review_items"][0]["source_ids"] == [
        "cite-a", "cite-b", "ev-a", "ev-b", "ref-a", "ref-b"
    ]
    assert review["required_user_information"][0]["code"] == (
        "authoring.citation_title_mapping_review_required"
    )


def test_v31_missing_work_title_repair_targets_only_the_affected_paragraph():
    source_records = {
        "annotation_citation_evidence": [{
            "id": "cite-book", "reference_id": "ref-book", "title": "A Confirmed Work",
            "authors": ["Scholar"], "allowed_use": "attributed_scholarship_citation",
        }],
        "reference_entries": [{"reference_id": "ref-book", "title": "A Confirmed Work"}],
        "argument_nodes": [{
            "id": "ev-book", "node_type": "evidence", "claim_id": "claim-1",
            "provenance": {"source_ids": ["cite-book"]},
        }],
        "section_contracts": [{
            "section_id": "sec-body", "article_role": "body", "claim_ids": ["claim-1"],
        }],
        "claim_contracts": [{"claim_id": "claim-1", "claim": "locked claim"}],
    }
    sections = [{
        "section_id": "sec-body", "paragraphs": [{
            "paragraph_id": "P-body-1", "text": "この理論を比較に用いる。",
            "claim_ids": ["claim-1"], "evidence_ids": ["ev-book"],
        }, {
            "paragraph_id": "P-body-2", "text": "別の段落は変更しない。",
            "claim_ids": ["claim-1"], "evidence_ids": [],
        }],
    }]
    findings = [{
        "section_id": "sec-body", "paragraph_index": 1, "paragraph_id": "P-body-1",
        "reference_ids": ["ref-book"], "required_reference_ids": ["ref-book"],
        "reason": "書名が見つからない。",
    }]
    calls = []

    def invoke(*args, **kwargs):
        del args
        calls.append(kwargs["payload"])
        return {
            "text": "『A Confirmed Work』の比較を通じて、概念の適用範囲を検討する。",
            "citation_mentions": [{
                "reference_id": "ref-book", "text": "『A Confirmed Work』",
            }],
        }

    runner = SimpleNamespace(
        _authoring_v31_visible_scholarship_citation_anchors=(
            WorkerRunner._authoring_v31_visible_scholarship_citation_anchors
        ),
        _authoring_invoke=invoke,
    )
    repaired = WorkerRunner._authoring_v31_repair_citation_paragraphs(
        runner, SimpleNamespace(), source_records, sections, findings, repair_round=1
    )
    assert len(calls) == 1
    assert calls[0]["paragraph_id"] == "P-body-1"
    assert calls[0]["required_reference_ids"] == ["ref-book"]
    assert repaired[0]["paragraphs"][0]["text"].startswith("『A Confirmed Work』")
    assert repaired[0]["paragraphs"][0]["evidence_ids"] == ["ev-book"]
    assert repaired[0]["paragraphs"][1] == sections[0]["paragraphs"][1]
    assert recover_citation_mentions(
        repaired,
        WorkerRunner._authoring_v31_visible_scholarship_citation_anchors(
            source_records, source_records["section_contracts"]
        ),
    )[1] == []


def test_v31_draft_audit_blockers_continue_to_final_review_after_repair_limit():
    from fukamiru.worker.runner import _AUTHORING_STRUCTURE_REPAIR_LIMIT

    claimed = SimpleNamespace(workflow_name="manuscript_generator", workflow_version="31")
    blocker = {
        "finding_id": "finding-1", "section_id": "sec-body",
        "target_ids": ["P-body-1"], "category": "unsupported_claim",
        "description": "根拠が不足しています。", "source_ids": ["ev-1"],
    }
    repairable = {**blocker, "finding_id": "finding-2", "category": "claim_drift"}
    context = {
        "source_preservation_validation": {"passed": True},
        "blocking_findings": [blocker], "auto_repair_findings": [repairable],
        "repair_count": _AUTHORING_STRUCTURE_REPAIR_LIMIT,
        "proposed_html": (
            '<article id="D" lang="ja"><section id="S">'
            '<h2 id="H">本文</h2><p id="P">候補</p></section></article>'
        ),
    }
    result = WorkerRunner._authoring_join_draft_audits(
        object.__new__(WorkerRunner), claimed, context
    )
    assert result.outcome == "success"
    assert result.context["draft_audit_join"]["passed"] is False
    assert result.context["draft_audit_join"]["repair_exhausted"] is True
    assert result.context["human_review_items"][0]["blocks_render"] is True
    assert result.context["required_user_information"]


def test_v31_certainty_audit_preserves_scoped_prose_without_fixed_suffix():
    runner = object.__new__(WorkerRunner)
    runner._reserve_codex_subslots = lambda *args, **kwargs: 0
    runner._release_codex_subslots = lambda *args, **kwargs: None
    runner._record_manuscript_progress = lambda *args, **kwargs: None
    runner._response_contract_context_v22 = lambda claimed, payload, **kwargs: payload

    def audit(claimed, phase, units, sources, **kwargs):
        return {
            "verdicts": [
                {"unit_id": unit["unit_id"], "status": "tentative_interpretation"}
                for unit in units
            ]
        }

    def parallel(claimed, batches, **kwargs):
        for index, batch in enumerate(batches):
            kwargs["consume"](index, batch, kwargs["operation"](index, batch))

    runner._readonly_manuscript_audit = audit
    runner._run_ordered_parallel = parallel
    html = (
        '<article id="D" lang="ja"><section id="S"><h2 id="H">本文</h2>'
        '<p id="P">条件Aの下で差異が生じる。</p></section></article>'
    )
    result = runner._manuscript_calibrate_certainty(
        SimpleNamespace(run_id="r", workflow_version="31"),
        {"proposed_html": html, "research_problem": {"research_question": "なぜか"}},
    ).context
    assert result["proposed_html"] == html
    assert result["certainty_calibration"]["changes"] == []
    assert result["certainty_calibration"]["policy"] == "claim_scope_without_fixed_suffix_v31"


def test_v31_section_contract_json_handoff_accepts_role_specific_zero_moves(monkeypatch):
    data = _handoff_fixture()
    section_schema = STAGE_SCHEMAS["section_contracts_v31"]["properties"]["sections"]["items"]
    result = _example(STAGE_SCHEMAS["section_contracts_v31"])
    result["sections"] = []
    roles = [
        ("s0", "introduction"),
        ("s1", "body"),
        ("s2", "body"),
        ("s3", "conclusion"),
    ]
    for section_id, role in roles:
        section = _example(section_schema)
        section.update({
            "section_id": section_id,
            "article_role": role,
            "claim_ids": ["c1"],
            "evidence_ids": ["e1"] if role == "body" else [],
            "warrant_ids": ["w1"] if role == "body" else [],
            "section_task": f"検討 {section_id}",
            "section_output": f"確立 {section_id}",
            "section_output_contract": {
                "proposition": f"確立 {section_id}",
                "output_type": "interpretive_result",
                "required_evidence_level": "traceable",
            },
            "thesis_link": f"回答への寄与 {section_id}",
            "required_input_section_ids": ["s1"] if section_id == "s2" else [],
            "defense_obligation_ids": [],
            "counterarguments": [],
            "target_length": None,
            "development_expectations": {
                "minimum_evidence_items": 0,
                "minimum_concrete_examples": 0,
                "required_argument_moves": [],
            },
        })
        result["sections"].append(section)
    result["introduction_contract"] = {
        **data["intro"],
        "moves": [
            {"move": "research_question", "purpose": "問いを示す", "claim_ids": ["c1"]},
            {"move": "research_question", "purpose": "問いを具体化する", "claim_ids": ["c1"]},
        ],
    }
    result["conclusion_contract"] = data["conclusion"]
    CodexAuthoringAdapter.validate_result("section_contracts_v31", _json(result))
    context = {
        "claims": [{
            "id": "c1", "level": "main_claim", "parent_id": None, "text": "主張"
        }],
        "research_question": data["question"],
        "research_problem": data["problem"].model_dump(mode="json"),
        "propositions": data["propositions"],
        "argument_nodes": data["nodes"],
        "defense_obligations": [],
        "claim_scopes": [item.model_dump(mode="json") for item in data["scopes"]],
        "discourse_nodes": [
            {"id": "root", "parent_id": None},
            *[
                {
                    "id": f"d-{section_id}",
                    "parent_id": "root",
                    "section_id": section_id,
                    "article_role": role,
                    "claim_ids": ["c1"],
                }
                for section_id, role in roles
            ],
        ],
        "requested_total_length": None,
    }
    runner = object.__new__(WorkerRunner)
    runner.databases = SimpleNamespace(state=object())
    stages = []

    def generate(claimed, stage, context, **kwargs):
        stages.append(stage)
        return _json(result)

    runner._authoring_generate = generate
    plan = SimpleNamespace(
        id="plan", revision=1, claims=[], argument_nodes=[],
        discourse_nodes=[], section_contracts=[],
    )
    monkeypatch.setattr("fukamiru.worker.runner.build_plan", lambda context: plan)
    monkeypatch.setattr("fukamiru.worker.runner.plan_for_prompt", lambda plan: {})
    monkeypatch.setattr(
        "fukamiru.worker.runner.AuthoringRepository",
        lambda state: SimpleNamespace(create_plan=lambda *args, **kwargs: None),
    )
    output = runner._authoring_lock_contracts(
        SimpleNamespace(workflow_version="31", node_run_id="n1"), context
    ).context
    assert stages == ["section_contracts_v31"]
    assert output["conclusion_contract"]["section_id"] == "s3"
    assert [item["section_id"] for item in output["body_section_outputs"]] == [
        "s1", "s2"
    ]


@pytest.mark.parametrize(("repair_target", "conclusion_extra_id"), [
    ("body", False),
    ("body", True),
    ("conclusion", True),
    ("evidence", False),
    ("initial_ids", True),
])
def test_v31_draft_json_routes_introduction_body_then_conclusion(
    monkeypatch, tmp_path, repair_target, conclusion_extra_id,
):
    data = _handoff_fixture()
    context = {
        "plan_id": "plan", "plan_revision": 1,
        "use_rag": False,
        "section_contracts": data["sections"],
        "research_problem": data["problem"].model_dump(mode="json"),
        "claim_scopes": [item.model_dump(mode="json") for item in data["scopes"]],
        "defense_obligations": [],
        "introduction_contract": data["intro"],
        "conclusion_contract": data["conclusion"],
        "authoring_draft_restart_findings": [{
            "code": "evidence_mismatch",
            "severity": "major",
            "message": "Evidence supports a different proposition.",
            "object_type": "section",
            "object_id": "s1",
            "repair_scope": "targeted",
        }],
        "fixed_assets": build_fixed_assets({
                "evidence_context": [{
                    "id": "e1",
                    "text": "根拠本文",
                    "source_span_id": "e1-span",
                    "verification_status": "verified",
                }],
        }).model_dump(mode="json"),
    }
    runner = object.__new__(WorkerRunner)
    runner.databases = SimpleNamespace(state=object())
    runner.settings = SimpleNamespace(paths=SimpleNamespace(runtime_root=tmp_path))
    runner._register_artifact = lambda *args, **kwargs: None
    runner._authoring_reconcile_theory_citation_titles = lambda value, **_kwargs: value
    runner._authoring_draft_contracts_with_scholarship = lambda value, sections: sections
    runner._authoring_draft_plan_projection = lambda value, batch: {}
    runner._authoring_evidence_projection = lambda value, batch: {}
    runner._authoring_naturalize_reader_content_brief_v31 = (
        lambda claimed, value, brief: (brief, {"passed": True, "changes": []})
    )
    runner._authoring_normalize_evidence_treatments = lambda value, sections: (sections, [])
    validation_calls = []

    def contract_violations(value, sections, **_kwargs):
        validation_calls.append([item["section_id"] for item in sections])
        if len(validation_calls) != 1:
            return []
        if repair_target == "body":
            return ["section s1 citation handoff"]
        if repair_target == "conclusion":
            return ["Conclusion prose needs repair"]
        return []

    runner._authoring_draft_contract_violations = contract_violations
    runner._authoring_render_sections = lambda value, sections, **_kwargs: "<article></article>"
    runner._authoring_v29_plan_bridge = lambda value, html, **_kwargs: {}
    runner._authoring_v31_render_mapping_problems = lambda *_args: []
    runner._authoring_persistence_metadata = lambda value: value

    def normalize(value, sections, require_full=False, **_kwargs):
        return [
            {**section, "text": "\n\n".join(p["text"] for p in section["paragraphs"])}
            for section in sections
        ]

    runner._authoring_normalize_generated_section_ids = normalize
    runner._authoring_merge_targeted_sections = (
        lambda value, batches, results: [
            item
            for batch, result in zip(batches, results, strict=True)
            for item in normalize(value, result["sections"])
        ]
    )
    calls = []

    def generate(claimed, stage, value, **kwargs):
        payload = kwargs["payload"]
        calls.append((stage, payload))
        sections = []
        for contract in payload["target_section_contracts"]:
            role = contract["article_role"]
            section_id = contract["section_id"]
            item_schema = STAGE_SCHEMAS[stage]["properties"]["sections"]["items"]
            section = _example(item_schema)
            section.update({
                "section_id": section_id,
                "article_role": role,
                "claim_ids": ["c1"],
                "evidence_ids": ["e1"] if role == "body" else [],
                "realized_evidence_ids": ["e1"] if role == "body" else [],
                "realized_warrant_ids": ["w1"] if role == "body" else [],
                "evidence_treatment": (
                    [_example(item_schema["properties"]["evidence_treatment"]["items"])]
                    if role == "body" else []
                ),
                "concrete_examples": [],
                "paragraphs": [{
                    "move_indices": [],
                    "evidence_ids": ["e1"] if role == "body" else [],
                    "citation_mentions": [],
                    "text": (
                        (
                            data["question"]["text"]
                            if payload.get("current_section_draft")
                            else "RQを言い換えた導入"
                        ) if role == "introduction"
                        else data["synthesis"]["final_answer"] if role == "conclusion"
                        else f"本文 {section_id}"
                    ),
                }],
            })
            if role == "body":
                section["evidence_treatment"][0]["evidence_id"] = "e1"
            sections.append(section)
        result = {"sections": sections}
        if stage == "draft_conclusion_v31":
            result["conclusion_synthesis"] = _json(data["synthesis"])
            if conclusion_extra_id and (
                payload.get("current_section_draft") or repair_target == "initial_ids"
            ):
                result["conclusion_synthesis"]["established_section_ids"].append("s3")
            if repair_target == "evidence" and not payload.get("current_section_draft"):
                result["conclusion_synthesis"]["new_evidence_ids"] = ["phantom"]
        CodexAuthoringAdapter.validate_result(stage, _json(result))
        return _json(result)

    runner._authoring_generate = generate
    runner._authoring_parallel_generate = (
        lambda claimed, stage, payloads, **kwargs: [
            generate(claimed, stage, context, payload=payload)
            for payload in payloads
        ]
    )
    monkeypatch.setattr(
        "fukamiru.worker.runner.AuthoringRepository",
            lambda state: SimpleNamespace(
                load_plan=lambda *args: SimpleNamespace(section_contracts=[]),
                next_draft_attempt=lambda *args: 1,
                save_draft=lambda *args, **kwargs: None,
                save_evidence_state=lambda *args, **kwargs: None,
            ),
    )
    monkeypatch.setattr("fukamiru.worker.runner.build_drafts", lambda *args, **kwargs: [])
    output = runner._authoring_draft_sections_v31(
        SimpleNamespace(workflow_version="31", node_run_id="n1", run_id="run-1"),
        context,
    ).context
    assert [stage for stage, _ in calls[:3]] == [
        "draft_introduction_v31", "draft_body_v31", "draft_conclusion_v31"
    ]
    assert all(
        "restart_findings" not in payload
        and "locked_plan" not in payload
        and "enumeration_context_contracts" not in payload
        and payload["reader_content_brief"]["policy"]
        == "v31-shared-claim-citation-brief-v1"
        for _, payload in calls[:3]
    )
    assert [item["section_id"] for item in calls[2][1]["body_drafts"]] == [
        "s1", "s2"
    ]
    assert output["draft_contract_repair_count"] == (
        2 if repair_target == "body" else 0 if repair_target == "initial_ids" else 1
    )
    assert output["repair_count"] == 0
    assert output["conclusion_synthesis"]["final_answer"] == data["synthesis"]["final_answer"]
    assert output["conclusion_synthesis"]["established_section_ids"] == ["s1", "s2"]
    assert bool(output["conclusion_synthesis_id_corrections"]) is conclusion_extra_id
    if conclusion_extra_id:
        assert output["conclusion_synthesis_id_corrections"][-1] == {
            "operation_name": (
                "authoring_draft_conclusion_v31_repair_1"
                if repair_target != "initial_ids"
                else "authoring_draft_conclusion"
            ),
            "reported_section_ids": ["s1", "s2", "s3"],
            "locked_body_section_ids": ["s1", "s2"],
        }
    repair_payloads = [
        payload for _, payload in calls if payload.get("current_section_draft")
    ]
    assert bool(repair_payloads) is (repair_target != "initial_ids")
    intro_repairs = [
        payload
        for payload in repair_payloads
        if payload["target_section_contracts"][0]["article_role"] == "introduction"
    ]
    assert intro_repairs == []
    assert all(
        "exact_research_question" not in payload
        for _, payload in calls
    )
    if repair_target == "body":
        assert all("因果関係" in payload["repair_instruction"] for payload in repair_payloads)
        assert all(
            "move_indices" in payload["repair_instruction"]
            and (
                "not a prose quota" in payload["repair_instruction"]
                or "Move labels are optional" in payload["repair_instruction"]
            )
            for payload in repair_payloads
        )
    elif repair_target != "initial_ids":
        assert all(
            payload["target_section_contracts"][0]["article_role"] == "conclusion"
            for payload in repair_payloads
        )


def test_v31_draft_projection_does_not_expose_other_section_evidence_for_shared_claim():
    context = {
        "research_problem": {"research_question": "問い"},
        "research_question": {"text": "問い"},
        "claims": [{"id": "c1", "parent_id": None}],
        "argument_nodes": [
            {
                "id": "e-target", "node_type": "evidence", "claim_id": "c1",
                "provenance": {"source_ids": ["raw-target"]},
            },
            {
                "id": "e-other", "node_type": "evidence", "claim_id": "c1",
                "provenance": {"source_ids": ["raw-other"]},
            },
            {"id": "w1", "node_type": "warrant", "claim_id": "c1"},
        ],
        "argument_edges": [],
        "discourse_nodes": [],
        "evidence_context": [
            {"id": "raw-target", "finding_key": "c1", "excerpt": "対象節の資料"},
            {"id": "raw-other", "finding_key": "c1", "excerpt": "別節の資料"},
        ],
        "authoring_evidence_contracts": [{
            "claim_id": "c1",
            "accepted_evidence_ids": ["raw-target", "raw-other"],
        }],
    }
    target = [{
        "section_id": "s-target",
        "claim_ids": ["c1"],
        "evidence_ids": ["e-target"],
        "warrant_ids": ["w1"],
    }]

    plan = WorkerRunner._authoring_draft_plan_projection(context, target)
    evidence = WorkerRunner._authoring_evidence_projection(context, target)

    assert {item["id"] for item in plan["argument_nodes"]} == {"e-target", "w1"}
    assert [item["id"] for item in evidence["accepted_evidence"]] == ["raw-target"]


def test_v31_role_projection_withholds_other_section_scope_and_defense_evidence():
    context = {
        "claim_scopes": [
            {"claim_id": "c-intro", "evidence_basis_ids": ["e-intro", "e-body"]},
            {"claim_id": "c-body", "evidence_basis_ids": ["e-body"]},
        ],
        "defense_obligations": [
            {
                "id": "d-intro",
                "target_claim_id": "c-intro",
                "basis_ids": ["e-intro", "e-body"],
                "source_argument_node_ids": ["w-intro", "w-body"],
            },
            {
                "id": "d-global",
                "target_claim_id": "c-other",
                "basis_ids": ["e-other"],
                "source_argument_node_ids": ["w-other"],
            },
        ],
    }
    target = [
        {
            "section_id": "section-introduction",
            "claim_ids": ["c-intro"],
            "evidence_ids": ["e-intro"],
            "warrant_ids": ["w-intro"],
            "defense_obligation_ids": ["d-global"],
        }
    ]

    projection = WorkerRunner._authoring_argument_role_projection(context, target)

    assert projection["claim_scopes"] == [
        {"claim_id": "c-intro", "evidence_basis_ids": ["e-intro"]}
    ]
    assert projection["defense_obligations"] == [
        {
            "id": "d-intro",
            "target_claim_id": "c-intro",
            "basis_ids": ["e-intro"],
            "source_argument_node_ids": ["w-intro"],
        },
        {
            "id": "d-global",
            "target_claim_id": "c-other",
            "basis_ids": [],
            "source_argument_node_ids": [],
        },
    ]


def test_v31_conclusion_inherits_existing_body_evidence_treatment_metadata():
    sections = [
        {
            "section_id": "body",
            "article_role": "body",
            "evidence_treatment": [{
                "evidence_id": "e1",
                "claim_id": "c1",
                "basis": "memo_close_reading",
                "verification_status": "unverified",
                "prose_treatment": "qualified_interpretation",
            }],
        },
        {
            "section_id": "conclusion",
            "article_role": "conclusion",
            "realized_evidence_ids": ["e1"],
            "evidence_treatment": [],
        },
    ]

    normalized, corrections = (
        WorkerRunner._authoring_inherit_conclusion_evidence_treatments({}, sections)
    )

    assert normalized[1]["evidence_treatment"] == sections[0]["evidence_treatment"]
    assert corrections == [{
        "section_id": "conclusion",
        "evidence_ids": ["e1"],
        "policy": "inherit-established-body-evidence-treatment-v1",
    }]
    assert sections[1]["evidence_treatment"] == []


def test_v31_dependent_conclusion_repair_preserves_empty_evidence_contract():
    original = {
        "section_id": "section-conclusion",
        "article_role": "conclusion",
        "paragraphs": [{"text": "統合する。", "evidence_ids": []}],
        "evidence_ids": [],
        "realized_evidence_ids": [],
        "evidence_treatment": [],
    }
    regenerated = {
        "section_id": "section-conclusion",
        "article_role": "conclusion",
        "paragraphs": [{"text": "本文の成果を統合する。", "evidence_ids": ["e1"]}],
        "evidence_ids": ["e1"],
        "realized_evidence_ids": ["e1"],
        "evidence_treatment": [{"evidence_id": "e1"}],
    }

    normalized, changed = (
        WorkerRunner._authoring_preserve_evidence_free_conclusion_repair(
            original,
            regenerated,
            {"new_evidence_ids": []},
        )
    )

    assert changed is True
    assert normalized["paragraphs"][0]["text"] == "本文の成果を統合する。"
    assert normalized["paragraphs"][0]["evidence_ids"] == []
    assert normalized["evidence_ids"] == []
    assert normalized["realized_evidence_ids"] == []
    assert normalized["evidence_treatment"] == []
    assert regenerated["realized_evidence_ids"] == ["e1"]


def test_v31_dependent_conclusion_repair_keeps_declared_new_evidence():
    original = {
        "article_role": "conclusion",
        "evidence_ids": [],
        "realized_evidence_ids": [],
        "evidence_treatment": [],
    }
    regenerated = {
        "article_role": "conclusion",
        "paragraphs": [{"text": "新しい証拠を導入する。", "evidence_ids": ["e-new"]}],
        "evidence_ids": ["e-new"],
        "realized_evidence_ids": ["e-new"],
        "evidence_treatment": [{"evidence_id": "e-new"}],
    }

    normalized, changed = (
        WorkerRunner._authoring_preserve_evidence_free_conclusion_repair(
            original,
            regenerated,
            {"new_evidence_ids": ["e-new"]},
        )
    )

    assert changed is False
    assert normalized == regenerated


def test_v31_dependent_conclusion_repair_does_not_replace_rejected_evidence():
    original = {
        "article_role": "conclusion",
        "paragraphs": [{"text": "旧結論。", "evidence_ids": ["e-old"]}],
        "evidence_ids": ["e-old"],
        "realized_evidence_ids": ["e-old"],
        "evidence_treatment": [{"evidence_id": "e-old"}],
    }
    regenerated = {
        "article_role": "conclusion",
        "paragraphs": [{"text": "統合結果。", "evidence_ids": ["e-other"]}],
        "evidence_ids": ["e-other"],
        "realized_evidence_ids": ["e-other"],
        "evidence_treatment": [{"evidence_id": "e-other"}],
    }

    normalized, changed = (
        WorkerRunner._authoring_preserve_evidence_free_conclusion_repair(
            original,
            regenerated,
            {"new_evidence_ids": []},
            {"e-old"},
        )
    )

    assert changed is True
    assert normalized["paragraphs"][0]["text"] == "統合結果。"
    assert normalized["paragraphs"][0]["evidence_ids"] == []
    assert normalized["realized_evidence_ids"] == []


def test_v31_draft_reference_handoff_only_maps_known_same_claim_citations():
    nodes = [
        {"id": "q1", "node_type": "qualifier", "claim_id": "c1"},
        {
            "id": "citation-node-1", "node_type": "evidence", "claim_id": "c1",
            "provenance": {"source_ids": ["raw-citation"]},
        },
        {
            "id": "citation-node-2", "node_type": "evidence", "claim_id": "c2",
            "provenance": {"source_ids": ["other-citation"]},
        },
    ]
    nodes[1]["id"] = "annotation-citation-node:one"
    nodes[2]["id"] = "annotation-citation-node:two"
    section = {
        "section_id": "s1", "realized_evidence_ids": ["q1", "raw-citation"],
        "realized_qualifier_ids": [],
        "paragraphs": [{
            "text": "原文", "move_indices": [1, 3],
            "evidence_ids": ["q1", "raw-citation", "other-citation", "unknown"],
        }],
        "evidence_treatment": [
            {"evidence_id": "q1"}, {"evidence_id": "raw-citation"},
        ],
    }
    result, corrections = normalize_v31_draft_references(
        [section],
        contracts=[{
            "section_id": "s1", "claim_ids": ["c1"],
                "development_expectations": {"required_argument_moves": ["object_context"]},
        }],
        argument_nodes=nodes,
    )
    paragraph = result[0]["paragraphs"][0]
    assert paragraph["text"] == "原文"
    assert paragraph["move_indices"] == [1]
    assert paragraph["evidence_ids"] == [
        "annotation-citation-node:one", "other-citation", "unknown"
    ]
    assert result[0]["realized_qualifier_ids"] == ["q1"]
    assert result[0]["evidence_ids"] == [
        "annotation-citation-node:one", "other-citation", "unknown"
    ]
    assert result[0]["evidence_treatment"] == [
        {"evidence_id": "annotation-citation-node:one"}
    ]
    assert corrections


def test_visible_citation_metadata_is_extractive_and_source_specific():
    context = {
        "argument_nodes": [{
            "id": "e1", "node_type": "evidence", "claim_id": "c1",
            "provenance": {"source_ids": ["citation-1"]},
        }],
        "annotation_citation_evidence": [{
            "id": "citation-1", "allowed_use": "attributed_scholarship_citation",
            "authors": ["ランシエール"], "title": "感性的なもののパルタージュ",
        }],
    }
    sentence = "ランシエールの著作は参照候補であり、この作品分析の根拠には用いない。"
    sections = [{
        "section_id": "s1",
        "paragraphs": [{"text": sentence, "evidence_ids": ["e1"]}],
        "evidence_treatment": [{
            "evidence_id": "e1", "prose_treatment": "attributed_scholarship",
            "attribution_sentence": "本文にない文。", "purpose_clause": "架空の理由",
        }],
    }]
    result, corrections = WorkerRunner._authoring_align_visible_citation_metadata(
        context, sections
    )
    treatment = result[0]["evidence_treatment"][0]
    assert treatment["support_sentence"] == sentence
    assert treatment["attribution_sentence"] == sentence
    assert treatment["purpose_clause"] in sentence
    assert result[0]["paragraphs"] == sections[0]["paragraphs"]
    assert len(corrections) == 1
    repaired = deepcopy(sections)
    repaired_sentence = "ランシエールの著作を可視性の理論的な参照点として吟味する。"
    repaired[0]["paragraphs"][0]["text"] = repaired_sentence
    aligned, aligned_corrections = WorkerRunner._authoring_align_visible_citation_metadata(
        context, repaired
    )
    assert aligned[0]["evidence_treatment"][0]["attribution_sentence"] == repaired_sentence
    assert len(aligned_corrections) == 1
    repaired[0]["paragraphs"][0]["text"] = (
        "ウィリアムズ『マルクス主義と文学』の概念から、"
        "大人らしさを制度名に固まらない時代感覚として読む。"
    )
    context["annotation_citation_evidence"][0].update({
        "authors": ["ウィリアムズ"],
        "title": "マルクス主義と文学",
    })
    aligned, aligned_corrections = WorkerRunner._authoring_align_visible_citation_metadata(
        context, repaired
    )
    reading_treatment = aligned[0]["evidence_treatment"][0]
    assert reading_treatment["purpose_clause"] in reading_treatment[
        "attribution_sentence"
    ]
    assert len(aligned_corrections) == 1
    repaired[0]["paragraphs"][0]["text"] = (
        "ランシエールの著作を参照しても、個別作品の年代は証明できない。"
    )
    context["annotation_citation_evidence"][0].update({
        "authors": ["ランシエール"],
        "title": "感性的なもののパルタージュ",
    })
    aligned, aligned_corrections = WorkerRunner._authoring_align_visible_citation_metadata(
        context, repaired
    )
    assert aligned[0]["evidence_treatment"][0]["attribution_sentence"] == (
        repaired[0]["paragraphs"][0]["text"]
    )
    assert len(aligned_corrections) == 1
    absent = deepcopy(sections)
    absent[0]["paragraphs"][0]["text"] = "著作への参照はある。"
    unchanged, _ = WorkerRunner._authoring_align_visible_citation_metadata(
        context, absent
    )
    assert unchanged[0]["evidence_treatment"] == absent[0]["evidence_treatment"]


def test_v31_repair_drops_only_uncited_new_synthesis_evidence_labels():
    context = {
        "section_contracts": [{"section_id": "s03", "evidence_ids": ["local"]}],
        "draft_sections": [{
            "section_id": "s03",
            "evidence_ids": ["local"],
            "paragraphs": [{"text": "既存の分析。", "evidence_ids": ["local"]}],
        }],
    }
    sections = [{
        "section_id": "s03", "article_role": "body",
        "text": "既存の分析。\n\n他節との比較。",
        "evidence_ids": ["local", "foreign"],
        "realized_evidence_ids": ["local", "foreign"],
        "paragraphs": [
            {"text": "既存の分析。", "evidence_ids": ["local"]},
            {"text": "他節との比較。", "evidence_ids": ["foreign"]},
        ],
        "evidence_treatment": [{
            "evidence_id": "foreign", "basis": "memo_close_reading",
            "verification_status": "unverified",
            "prose_treatment": "qualified_interpretation",
        }],
    }]

    normalized, corrections = WorkerRunner._authoring_normalize_repair_synthesis_evidence(
        context, sections
    )

    assert normalized[0]["text"] == sections[0]["text"]
    assert normalized[0]["evidence_ids"] == ["local"]
    assert normalized[0]["realized_evidence_ids"] == ["local"]
    assert normalized[0]["paragraphs"][1]["evidence_ids"] == []
    assert normalized[0]["evidence_treatment"] == []
    assert corrections[0]["evidence_ids"] == ["foreign"]
    linked = deepcopy(sections)
    linked[0]["paragraphs"][1]["text"] = "[他著作](https://example.org)との比較。"
    preserved, _ = WorkerRunner._authoring_normalize_repair_synthesis_evidence(
        context, linked
    )
    assert preserved[0]["paragraphs"][1]["evidence_ids"] == ["foreign"]


def test_v31_polish_preserves_explicit_final_answer_when_style_pass_omits_it():
    runner = object.__new__(WorkerRunner)
    drafts = [
        {"section_id": "s1", "article_role": "introduction", "text": "研究質問は何か。"},
        {"section_id": "s2", "article_role": "body", "text": "本文の分析。"},
        {"section_id": "s3", "article_role": "conclusion", "text": "本文で確立した最終回答。"},
    ]
    context = {
        "draft_sections": drafts,
        "section_contracts": drafts,
        "locked_plan": {},
        "research_problem": {"research_question": "研究質問は何か。"},
        "conclusion_synthesis": {"final_answer": "本文で確立した最終回答。"},
        "language": "ja",
        "proposed_html": "<article/>",
    }
    runner._authoring_section_batches = lambda sections: [sections]
    runner._authoring_parallel_generate = lambda *args, **kwargs: [{}]
    runner._authoring_merge_targeted_sections = lambda *args, **kwargs: [
        {"section_id": "s1", "text": "研究質問は何か。"},
        {"section_id": "s2", "text": "本文の分析。"},
        {"section_id": "s3", "text": "本文のまとめ。"},
    ]
    runner._authoring_render_sections = lambda value, sections: "<article/>"
    runner._authoring_v29_plan_bridge = lambda value, html, **_kwargs: {}
    runner._authoring_persistence_metadata = lambda value: value

    result = runner._authoring_polish(SimpleNamespace(workflow_version="31"), context)

    assert result.context["polish_preserved_role_sections"] == ["s3"]
    assert result.context["polished_sections"][-1]["text"] == (
        "本文で確立した最終回答。"
    )


def _citation_title_repair_case():
    titles = (
        "『管理される心 : 感情が商品になるとき』",
        "『正義の秤(スケール) : グローバル化する世界で政治空間を再想像すること』",
    )
    source = (
        '<article><section><p id="p-labor">本稿がホックシールドの'
        '<cite data-reference-id="ref-hoch">' + titles[0] + '</cite>'
        'の議論を参照する。</p><p id="p-justice">本稿がフレイザーの'
        '<cite data-reference-id="ref-fraser">' + titles[1] + '</cite>'
        'を参照する。</p></section></article>'
    )
    candidate = (
        '<article><section><p id="p-labor">本稿がホックシールドの議論を'
        '作品の分析に適用する。</p><p id="p-justice">本稿がフレイザーの区別を'
        '読みの基準に使う。</p></section></article>'
    )
    context = {
        "annotation_citation_evidence": [
            {
                "id": "citation-hoch", "reference_id": "ref-hoch",
                "title": titles[0][1:-1], "authors": ["A.R.ホックシールド"],
                "excerpt": "ホックシールド『管理される心』",
                "allowed_use": "attributed_scholarship_citation",
                "verification_scope": "bibliographic_identity_only",
            },
            {
                "id": "citation-fraser", "reference_id": "ref-fraser",
                "title": titles[1][1:-1], "authors": ["フレイザー、ナンシー"],
                "excerpt": "フレイザー『正義の秤』",
                "allowed_use": "attributed_scholarship_citation",
                "verification_scope": "bibliographic_identity_only",
            },
        ],
        "reference_entries": [
            {"reference_id": "ref-hoch", "title": titles[0][1:-1]},
            {"reference_id": "ref-fraser", "title": titles[1][1:-1]},
        ],
        "argument_nodes": [
            {"id": "ev-hoch", "node_type": "evidence", "source_span_id": "citation-hoch"},
            {"id": "ev-fraser", "node_type": "evidence", "source_span_id": "citation-fraser"},
        ],
        "authoring_paragraph_citation_lineage": [
            {"output_block_id": "p-labor", "authoring_section_id": "labor",
             "argument_evidence_node_ids": ["ev-hoch"], "evidence_ids": ["citation-hoch"]},
            {"output_block_id": "p-justice", "authoring_section_id": "justice",
             "argument_evidence_node_ids": ["ev-fraser"], "evidence_ids": ["citation-fraser"]},
        ],
    }
    return context, source, candidate, titles


def test_v31_citation_title_restoration_keeps_revised_prose_and_reference_identity():
    context, source, candidate, titles = _citation_title_repair_case()
    repaired, records = WorkerRunner._authoring_restore_visible_citation_titles(
        context, candidate, source
    )
    root = ET.fromstring(repaired)
    paragraphs = {item.get("id"): item for item in root.iter("p")}
    for block_id, reference_id, title in (
        ("p-labor", "ref-hoch", titles[0]),
        ("p-justice", "ref-fraser", titles[1]),
    ):
        paragraph = paragraphs[block_id]
        cites = list(paragraph.iter("cite"))
        assert [(cite.get("data-reference-id"), "".join(cite.itertext())) for cite in cites] == [
            (reference_id, title)
        ]
        assert "".join(paragraph.itertext()).replace(title, "") == "".join(
            next(item for item in ET.fromstring(candidate).iter("p")
                 if item.get("id") == block_id).itertext()
        )
    assert [item["strategy"] for item in records] == [
        "insert_title_after_author", "insert_title_after_author"
    ]
    assert WorkerRunner._authoring_restore_visible_citation_titles(
        context, repaired, source
    ) == (repaired, [])


def test_v31_citation_title_restoration_uses_only_the_cited_source_on_ambiguity():
    context, source, candidate, _titles = _citation_title_repair_case()
    candidate = candidate.replace(
        "本稿がホックシールドの議論を作品の分析に適用する。",
        "ホックシールドを読み、ホックシールドを分析に使う。",
    )
    repaired, records = WorkerRunner._authoring_restore_visible_citation_titles(
        context, candidate, source
    )
    paragraphs = {item.get("id"): item for item in ET.fromstring(repaired).iter("p")}
    assert "".join(paragraphs["p-labor"].itertext()) == "".join(
        next(item for item in ET.fromstring(source).iter("p")
             if item.get("id") == "p-labor").itertext()
    )
    assert records[0]["strategy"] == "source_paragraph_restore"
    assert records[1]["strategy"] == "insert_title_after_author"


def test_v31_polish_restores_citation_markers_before_rebuilding_lineage():
    runner = object.__new__(WorkerRunner)
    context, source, candidate, _titles = _citation_title_repair_case()
    context.update({
        "draft_sections": [{"section_id": "labor", "text": "出典を維持する。"}],
        "section_contracts": [{"section_id": "labor", "article_role": "body"}],
        "locked_plan": {},
        "proposed_html": source,
        "language": "ja",
        "authoring_v31_attribution_predicate_validation": {"passed": True},
    })
    runner._authoring_section_batches = lambda sections: [sections]
    runner._authoring_parallel_generate = lambda *_args, **_kwargs: [{}]
    runner._authoring_merge_targeted_sections = lambda *_args, **_kwargs: [
        {"section_id": "labor", "text": "改稿された本文。"}
    ]
    runner._authoring_render_sections = lambda *_args: candidate
    runner._authoring_v29_plan_bridge = lambda _value, html, **_kwargs: {
        "structure_plan_validation": {"valid": True},
        "rendered_html": html,
    }
    runner._authoring_v31_role_text_violations = lambda *_args: []
    runner._authoring_persistence_metadata = lambda value: value

    result = runner._authoring_polish(
        SimpleNamespace(workflow_version="31"), context
    ).context

    assert len(result["polish_citation_title_restorations"]) == 2
    assert '<cite data-reference-id="ref-hoch">' in result["proposed_html"]
    assert '<cite data-reference-id="ref-fraser">' in result["proposed_html"]
    assert "authoring_v31_attribution_predicate_validation" not in result


def test_v31_post_proofreading_reconciliation_rechecks_current_citation_html():
    runner = object.__new__(WorkerRunner)
    context, source, candidate, _titles = _citation_title_repair_case()
    context.update({
        "proposed_html": candidate,
        "draft_sections": [{"section_id": "labor", "text": "出典を維持する。"}],
        "authoring_v31_attribution_predicate_validation": {"passed": True},
    })
    runner._authoring_render_sections = lambda *_args, **_kwargs: source
    runner._authoring_v29_plan_bridge = lambda value, _html, **_kwargs: {
        "structure_plan_validation": {"valid": True},
        "authoring_paragraph_citation_lineage": value["authoring_paragraph_citation_lineage"],
        "authoring_citation_lineage_reconciliation": {"dropped_bindings": []},
    }
    runner._authoring_v31_judge_attribution_predicates = lambda *_args: {}
    runner._authoring_v31_citation_predicate_permitted = lambda *_args: True
    runner._authoring_persistence_metadata = lambda value: value

    result = runner._authoring_reconcile_candidate_context(
        SimpleNamespace(
            workflow_version="31",
            node_id="reconcile_candidate_context_after_proofreading",
        ),
        context,
    ).context

    assert result["authoring_v31_attribution_predicate_validation"]["passed"] is True
    assert len(result["authoring_citation_title_restorations"]) == 2
    assert result["proposed_html"] != candidate
    assert '<cite data-reference-id="ref-hoch">' in result["proposed_html"]
    assert '<cite data-reference-id="ref-fraser">' in result["proposed_html"]


def test_aggregated_self_citation_repair_requires_verified_components():
    reference = {
        "reference_id": "r1", "is_self_authored": True,
        "record_type": "nonstandard_contribution",
        "original_text": "自著「短題」",
        "title": "短題——副題」、『論集』（2022） ・「別の論考」",
        "container_title": "論集", "year": "2022", "pages": "10-20",
        "author": "自著",
        "provenance": {
            "title": {"kind": "retrieved_metadata", "source_url": "https://example.test/r"}
        },
        "bibliography_people": [{
            "role": "author", "name_profile": "japanese_native",
            "family": "山田", "given": "花子",
            "responsibility_role_confirmed": True,
            "person_display_name_confirmed": True,
            "source": "https://example.test/r",
        }],
    }
    context = {
        "reference_entries": [reference],
        "evidence_context": [{"id": "e1", "reference_id": "r1", "title": "混合題"}],
        "annotation_citation_evidence": [{"id": "e1", "reference_id": "r1"}],
    }
    repaired, corrections = WorkerRunner._authoring_reconcile_aggregated_self_citations(
        context
    )
    assert len(corrections) == 1
    assert repaired["reference_entries"][0]["title"] == "短題——副題"
    assert repaired["reference_entries"][0]["record_type"] == "chapter"
    assert repaired["reference_entries"][0]["author"] == "山田花子"
    assert repaired["evidence_context"][0]["title"] == "短題——副題"
    assert repaired["annotation_citation_evidence"][0]["authors"] == ["山田花子"]
    assert reference["title"].endswith("別の論考」")

    incomplete = deepcopy(context)
    incomplete["reference_entries"][0].pop("pages")
    unchanged, corrections = WorkerRunner._authoring_reconcile_aggregated_self_citations(
        incomplete
    )
    assert corrections == []
    assert unchanged is incomplete


def test_unverified_aggregated_self_citation_is_removed_from_argument_handoffs():
    bad_node_id = "annotation-citation-node:bad"
    context = {
        "reference_entries": [{
            "reference_id": "bad-reference",
            "is_self_authored": True,
            "original_text": "自著「短題」",
            "title": "短題——副題」、『論集』（2022） ・「別の論考」",
            "annotation_proposition_ids": ["p-bad"],
        }],
        "argument_nodes": [
            {
                "id": "e-good",
                "node_type": "evidence",
                "claim_id": "c1",
                "proposition_id": "p-good",
                "provenance": {"verification_status": "verified"},
            },
            {
                "id": bad_node_id,
                "node_type": "evidence",
                "claim_id": "c1",
                "proposition_id": "p-bad",
                "text": "自著の集約候補",
                "provenance": {
                    "source_ids": ["bad-reference"],
                    "verification_status": "unverified",
                },
            },
            {"id": "c1", "node_type": "claim", "claim_id": "c1"},
        ],
        "argument_edges": [
            {"id": "edge-good", "source_id": "e-good", "target_id": "c1"},
            {"id": "edge-bad", "source_id": bad_node_id, "target_id": "c1"},
        ],
        "claim_scopes": [{"claim_id": "c1", "evidence_basis_ids": ["e-good"]}],
        "argument_graph_citation_title_corrections": [
            {"node_id": bad_node_id, "claim_id": "c1"}
        ],
        "locked_plan": {
            "argument_nodes": [{"id": f"locked:{bad_node_id}"}],
            "argument_edges": [{
                "source_id": f"locked:{bad_node_id}",
                "target_id": "locked:c1",
            }],
        },
        "structure_plan": {
            "argument_nodes": [{"id": f"plan:{bad_node_id}"}],
            "argument_edges": [{
                "source_node_id": f"plan:{bad_node_id}",
                "target_node_id": "plan:c1",
            }],
        },
    }

    cleaned, corrections = (
        WorkerRunner._authoring_drop_unusable_aggregated_self_citation_support(context)
    )

    assert {item["id"] for item in cleaned["argument_nodes"]} == {"e-good", "c1"}
    assert [item["id"] for item in cleaned["argument_edges"]] == ["edge-good"]
    assert cleaned["argument_graph_citation_title_corrections"] == []
    assert cleaned["locked_plan"]["argument_nodes"] == []
    assert cleaned["locked_plan"]["argument_edges"] == []
    assert cleaned["structure_plan"]["argument_nodes"] == []
    assert cleaned["structure_plan"]["argument_edges"] == []
    assert corrections[0]["removed_node_ids"] == [bad_node_id]

    protected = deepcopy(context)
    protected["section_contracts"] = [{"evidence_ids": [bad_node_id]}]
    unchanged, corrections = (
        WorkerRunner._authoring_drop_unusable_aggregated_self_citation_support(protected)
    )
    assert corrections == []
    assert bad_node_id in {item["id"] for item in unchanged["argument_nodes"]}


def test_verified_self_citation_publisher_requires_exact_publication_identity():
    reference = {
        "reference_id": "self-chapter", "is_self_authored": True,
        "record_type": "nonstandard_contribution",
        "original_text": "自著「架空資料の比較分析」",
        "title": (
            "架空資料の比較分析"
            "——『星の記録』における行為と竜」、『架空論集』"
        ),
        "container_title": "架空論集", "year": "2022",
        "pages": "131-176", "publisher": None,
        "provenance": {
            "title": {"kind": "retrieved_metadata", "source_url": "https://example.test/r"}
        },
        "bibliography_people": [{
            "role": "author", "name_profile": "japanese_native",
            "family": "山田", "given": "花子",
            "responsibility_role_confirmed": True,
            "person_display_name_confirmed": True,
            "source": "https://example.test/r",
        }],
    }
    reconciled, _ = WorkerRunner._authoring_reconcile_aggregated_self_citations(
        {"reference_entries": [reference]}
    )
    resolved = reconciled["reference_entries"][0]
    reference["field_evidence"] = {"publisher": [{
        "value": "Example Research Press",
        "source_url": "https://publisher.example.test/catalog/chapter",
        "verification_status": "corroborated",
        "bibliographic_identity": {key: resolved.get(key) for key in (
            "reference_id", "title", "author", "container_title", "year", "pages",
        )},
    }]}
    reconciled, _ = WorkerRunner._authoring_reconcile_aggregated_self_citations(
        {"reference_entries": [reference]}
    )
    repaired, corrections = (
        WorkerRunner._authoring_reconcile_verified_publisher_metadata(reconciled)
    )
    entry = repaired["reference_entries"][0]
    assert entry["publisher"] == "Example Research Press"
    assert entry["provenance"]["publisher"]["source_url"] == (
        "https://publisher.example.test/catalog/chapter"
    )
    assert any(item.get("field") == "publisher" for item in corrections)

    other = deepcopy(reference)
    other["pages"] = "1-20"
    reconciled, _ = WorkerRunner._authoring_reconcile_aggregated_self_citations(
        {"reference_entries": [other]}
    )
    unchanged, _ = WorkerRunner._authoring_reconcile_verified_publisher_metadata(
        reconciled
    )
    assert unchanged["reference_entries"][0].get("publisher") is None


def test_v31_exhausted_major_draft_audit_continues_to_final_review():
    runner = WorkerRunner.__new__(WorkerRunner)
    context = {
        "proposed_html": "<article id='a'></article>",
        "source_preservation_validation": {"passed": True},
        "audit_findings": [{"code": "unestablished_body_result", "severity": "major"}],
        "auto_repair_findings": [{
            "code": "unestablished_body_result",
            "severity": "major",
        }],
        "repair_count": 3,
    }
    result = runner._authoring_join_draft_audits(
        SimpleNamespace(workflow_name="authoring", workflow_version=31), context
    )
    assert result.outcome == "success"
    assert result.context["draft_audit_join"]["passed"] is False
    assert result.context["draft_audit_join"]["continued_with_unresolved_findings"]
    assert any(
        item.get("category") == "unestablished_body_result"
        and item.get("blocks_render") is True
        for item in result.context["human_review_items"]
    )
    assert any(
        item.get("code") == "unestablished_body_result"
        for item in result.context["required_user_information"]
    )
    legacy = runner._authoring_join_draft_audits(
        SimpleNamespace(workflow_name="authoring", workflow_version=30), context
    )
    assert legacy.outcome == "success"


def test_v31_draft_audit_join_uses_current_classification_not_stale_sibling():
    runner = WorkerRunner.__new__(WorkerRunner)
    html = "<article id='a'></article>"
    stale = {"finding_id": "AF-old", "disposition": "auto_repair"}
    review = {"finding_id": "AF-old", "disposition": "human_review"}
    preservation = {
        "proposed_html": html,
        "auto_repair_findings": [stale],
        "source_preservation_validation": {
            "passed": True,
            "candidate_sha256": submission_validation.digest(html),
        },
    }
    classified = {
        "proposed_html": html,
        "audit_bundle": {"findings": [review]},
        "audit_findings": [review],
        "auto_repair_findings": [],
        "human_review_findings": [review],
        "blocking_findings": [],
    }
    merged = runner._merge_v31_draft_audit_contexts(preservation, classified)
    assert merged["auto_repair_findings"] == []
    assert merged["human_review_findings"] == [review]
    assert runner._authoring_join_draft_audits(
        SimpleNamespace(workflow_name="authoring", workflow_version=31), merged
    ).context["draft_audit_join"]["passed"] is True

    with pytest.raises(FukamiruError, match="differ in candidate"):
        runner._merge_v31_draft_audit_contexts(
            {**preservation, "proposed_html": "<article>other</article>"}, classified
        )
    with pytest.raises(FukamiruError, match="missing or stale"):
        runner._merge_v31_draft_audit_contexts(
            {**preservation, "source_preservation_validation": {"passed": True}},
            classified,
        )


def test_v31_actionable_minor_audit_findings_are_promoted_to_major():
    normalized = WorkerRunner._authoring_v31_normalize_audit_severity(
        [
            {
                "code": "evidence_mismatch",
                "severity": "minor",
                "repair_scope": "local_section",
            },
            {
                "code": "observation",
                "severity": "minor",
                "repair_scope": "none",
            },
            {
                "code": "missing_dependency",
                "severity": "minor",
                "repair_scope": "plan_revision",
            },
        ]
    )

    assert [item["severity"] for item in normalized] == ["major", "minor", "major"]


def test_v31_role_specific_audit_findings_drop_context_only_section_targets():
    contracts = [
        {"section_id": "sec-intro", "article_role": "introduction"},
        {"section_id": "sec-body", "article_role": "body"},
        {"section_id": "sec-conclusion", "article_role": "conclusion"},
    ]
    findings = [
        {
            "code": "conceptual_mismatch",
            "severity": "major",
            "message": "序論の定義を改める。",
            "repair_scope": "local_section",
            "object_type": "section",
            "object_id": "sec-intro",
        },
        {
            "code": "conceptual_mismatch",
            "severity": "major",
            "message": "序論の定義を改める。",
            "repair_scope": "local_section",
            "object_type": "section",
            "object_id": "sec-body",
        },
        {
            "code": "conclusion_synthesis_gap",
            "severity": "major",
            "message": "節成果を結ぶ判断を加える。",
            "repair_scope": "local_section",
            "object_type": "section",
            "object_id": "sec-body",
        },
        {
            "code": "conclusion_synthesis_gap",
            "severity": "major",
            "message": "節成果を結ぶ判断を加える。",
            "repair_scope": "local_section",
            "object_type": "section",
            "object_id": "sec-conclusion",
        },
        {
            "code": "evidence_mismatch",
            "severity": "major",
            "message": "両節の根拠を直す。",
            "repair_scope": "local_section",
            "object_type": "section",
            "object_id": "sec-intro",
        },
        {
            "code": "evidence_mismatch",
            "severity": "major",
            "message": "両節の根拠を直す。",
            "repair_scope": "local_section",
            "object_type": "section",
            "object_id": "sec-body",
        },
    ]

    normalized, corrections = WorkerRunner._authoring_v31_normalize_audit_targets(
        contracts, findings
    )

    assert [item["object_id"] for item in normalized] == [
        "sec-intro", "sec-conclusion", "sec-intro", "sec-body"
    ]
    assert len(corrections) == 2
    assert corrections[0]["removed_context_only_object_ids"] == ["sec-body"]


def test_v31_conclusion_audit_expansion_uses_current_conclusion_section_id():
    expanded = WorkerRunner._authoring_expand_llm_audit_finding(
        {
            "finding_type": "conclusion_synthesis_gap",
            "severity": "major",
            "message": "結論に節横断の判断を加える。",
            "repair_scope": "local_section",
            "affected_section_ids": ["sec-body", "sec-conclusion"],
        }
    )

    assert [item["object_id"] for item in expanded] == ["sec-conclusion"]


def test_v31_repair_promotes_audit_messages_to_typed_mandatory_resolutions():
    resolutions = WorkerRunner._authoring_v31_mandatory_audit_resolutions(
        [
            {
                "code": "evidence_treatment_gap",
                "message": "事実ではなく比較手順として述べる。",
            },
            {
                "code": "conclusion_synthesis_gap",
                "message": "節成果を統合する。",
            },
            {
                "code": "structure_invalid",
                "message": "この役割には対応する証拠IDがない。比較手順に改める。",
            },
            {
                "code": "structure_invalid",
                "message": "添付IDは評価内容を示すが、石への帰属は支えない。",
            },
            {
                "code": "structure_invalid",
                "message": (
                    "役割の根拠がない。"
                    "「二人の位置は発言者と場を確かめてから区別する。」"
                    "と改める。"
                ),
            },
            {
                "code": "conclusion_claim_scope",
                "message": (
                    "次のように答えを限定できる。"
                    "「変化の有無は時系列資料によって確かめる必要がある。」"
                ),
            },
        ]
    )

    assert resolutions == [
        {
            "code": "evidence_treatment_gap",
            "object_id": None,
            "resolution_type": "use_procedure_without_new_fact",
            "auditor_instruction": "事実ではなく比較手順として述べる。",
            "must_remove_unestablished_fact": True,
        },
        {
            "code": "conclusion_synthesis_gap",
            "object_id": None,
            "resolution_type": "state_cross_section_synthesis",
            "auditor_instruction": "節成果を統合する。",
            "must_remove_unestablished_fact": False,
        },
        {
            "code": "structure_invalid",
            "object_id": None,
            "resolution_type": "use_procedure_without_new_fact",
            "auditor_instruction": (
                "この役割には対応する証拠IDがない。比較手順に改める。"
            ),
            "must_remove_unestablished_fact": True,
        },
        {
            "code": "structure_invalid",
            "object_id": None,
            "resolution_type": "use_procedure_without_new_fact",
            "auditor_instruction": (
                "添付IDは評価内容を示すが、石への帰属は支えない。"
            ),
            "must_remove_unestablished_fact": True,
        },
        {
            "code": "structure_invalid",
            "object_id": None,
            "resolution_type": "use_procedure_without_new_fact",
            "auditor_instruction": (
                "役割の根拠がない。"
                "「二人の位置は発言者と場を確かめてから区別する。」"
                "と改める。"
            ),
            "must_remove_unestablished_fact": True,
            "required_replacement_texts": [
                "二人の位置は発言者と場を確かめてから区別する。"
            ],
        },
        {
            "code": "conclusion_claim_scope",
            "object_id": None,
            "resolution_type": "apply_auditor_scope_exactly",
            "auditor_instruction": (
                "次のように答えを限定できる。"
                "「変化の有無は時系列資料によって確かめる必要がある。」"
            ),
            "must_remove_unestablished_fact": False,
            "required_replacement_texts": [
                "変化の有無は時系列資料によって確かめる必要がある。"
            ],
        },
    ]


def test_v31_repair_extracts_auditor_replacements_and_support_anchor():
    messages = [
        (
            "資料上の役割を事実として述べず、"
            "「久石譲と坂本龍一を同じ役割の例とみなすと、違いが消える。」"
            "と比較上の問題を直接述べる。"
        ),
        (
            "本文は「ジャンルの型は、版元が企画を組む際の枠組みにもなりうる。」"
            "と限定して述べられる。"
        ),
        (
            "冒頭を「以下では、貴族が救済と統治を担う物語を対象にする。」"
            "と対象宣言に改め、後続へつなぐ。"
        ),
        (
            "添付を外し、"
            "「古書店の棚と検索で作品に出会う経路を分けて検討する。」"
            "と推論として述べる。"
        ),
        (
            "末尾を「誌面上の隣接が期待に関わるのは同じ号に限られる。」"
            "と結ぶ。"
        ),
        (
            "本文を「ジャンルの型は企画にも用いられうる。」"
            "と未確認の見方として示す。"
        ),
        (
            "結論を「この見方は異なる対象を問う手掛かりとなる。」"
            "と限定する。"
        ),
        (
            "冒頭を「映画を生活資料として読むと細部が対象になる。」"
            "と始める。"
        ),
        (
            "添付を外し、"
            "「不遇から承認への配役を比較の着眼点とする。」"
            "と比較手順として述べられる。"
        ),
        (
            "本文を「二人の立場を分けて読む。」と記し、根拠を付ける。"
        ),
        (
            "許容根拠を保つなら、"
            "「掲載単位と発見経路を調べる。」"
            "と比較手順に限定できる。"
        ),
        (
            "添付を外し、"
            "「記録と帰属を事例ごとに比べる。」"
            "と本稿の比較手順として述べる。"
        ),
        (
            "読者向けには「棚による発見が期待に関わるかを検討する。」"
            "のように、確かめる関係を直接述べる。"
        ),
        (
            "第二段落を「掲載の場と制作条件は読まれ方に関わりうる。」"
            "とまとめれば仮説を示せる。"
        ),
        (
            "本文を「誰が承認するかを分けて問える。」と置き、後へ続ける。"
        ),
        (
            "三文を「ジャンルの型は探索と制作に用いられうる。」に統合し、"
            "処遇を対応させる。"
        ),
        (
            "答えを「実際の使い替えは未確定である。」と狭める。"
        ),
        (
            "現行の許可範囲なら「石の挙げた好悪は作品の位置づけを変えたかを"
            "示さない。」と述べる。"
        ),
        (
            "役割を断定せず、「石が挙げた好悪は評価として読める。」とすれば、"
            "節の成果を保てる。"
        ),
        (
            "「誌面、棚、タグは異なる接触経路を示す。」と統合し、答えにつなぐ。"
        ),
        (
            "現行の根拠なら「役割を断定せず二つの関係を分けて検討する。」として、"
            "節の成果を保つ。"
        ),
    ]
    resolutions = WorkerRunner._authoring_v31_mandatory_audit_resolutions(
        [
            {
                "code": "evidence_treatment_gap",
                "object_id": f"s{index}",
                "message": message,
            }
            for index, message in enumerate(messages, start=1)
        ]
    )

    assert [item["required_replacement_texts"] for item in resolutions] == [
        ["久石譲と坂本龍一を同じ役割の例とみなすと、違いが消える。"],
        ["ジャンルの型は、版元が企画を組む際の枠組みにもなりうる。"],
        ["以下では、貴族が救済と統治を担う物語を対象にする。"],
        ["古書店の棚と検索で作品に出会う経路を分けて検討する。"],
        ["誌面上の隣接が期待に関わるのは同じ号に限られる。"],
        ["ジャンルの型は企画にも用いられうる。"],
        ["この見方は異なる対象を問う手掛かりとなる。"],
        ["映画を生活資料として読むと細部が対象になる。"],
        ["不遇から承認への配役を比較の着眼点とする。"],
        ["二人の立場を分けて読む。"],
        ["掲載単位と発見経路を調べる。"],
        ["記録と帰属を事例ごとに比べる。"],
        ["棚による発見が期待に関わるかを検討する。"],
        ["掲載の場と制作条件は読まれ方に関わりうる。"],
        ["誰が承認するかを分けて問える。"],
        ["ジャンルの型は探索と制作に用いられうる。"],
        ["実際の使い替えは未確定である。"],
        ["石の挙げた好悪は作品の位置づけを変えたかを示さない。"],
        ["石が挙げた好悪は評価として読める。"],
        ["誌面、棚、タグは異なる接触経路を示す。"],
        ["役割を断定せず二つの関係を分けて検討する。"],
    ]

    support_resolution = {
        **resolutions[1],
        "auditor_instruction": (
            "許容根拠 b03-e0012b にこの節の support_sentence と扱いを追加する。"
            + messages[1]
        ),
    }
    section = {
        "section_id": "s2",
        "evidence_treatment": [{
            "evidence_id": "b03-e0012b",
            "support_sentence": "別の文。",
        }],
    }
    contract = {"evidence_ids": ["b03-e0012b"]}
    problems = WorkerRunner._authoring_required_support_anchor_violations(
        section, contract, [support_resolution]
    )
    assert problems == [
        "section s2 must anchor the auditor-required sentence to evidence "
        "b03-e0012b in evidence_treatment.support_sentence"
    ]
    section["paragraphs"] = [{
        "evidence_ids": ["b03-e0012b"],
        "text": support_resolution["required_replacement_texts"][0],
    }]
    normalized, corrections = (
        WorkerRunner._authoring_apply_required_support_anchors(
            section, contract, [support_resolution]
        )
    )
    assert corrections == [{
        "section_id": "s2",
        "evidence_id": "b03-e0012b",
        "from": "別の文。",
        "to": support_resolution["required_replacement_texts"][0],
        "policy": "auditor-required-support-anchor-v1",
    }]
    assert not WorkerRunner._authoring_required_support_anchor_violations(
        normalized, contract, [support_resolution]
    )


def test_v31_proofreading_cannot_change_locked_question_or_answer(monkeypatch):
    runner = WorkerRunner.__new__(WorkerRunner)
    claimed = SimpleNamespace(workflow_name="authoring", workflow_version=31)
    context = {
        "proposed_html": "問いA。結論B。",
        "research_problem": {"research_question": "問いA。"},
        "conclusion_synthesis": {"final_answer": "結論B。"},
    }
    monkeypatch.setattr(
        sentence_proofreading, "publish_shadow",
        lambda *_: {**context, "proposed_html": "問いA。別の結論。"},
    )
    with pytest.raises(FukamiruError, match="proofreading changed locked"):
        runner._manuscript_publish_proofreading_shadow_v29(claimed, context)
    received: dict[str, object] = {}

    def preserve_locks(_runner, _claimed, publish_context):
        received.update(publish_context)
        return {**publish_context, "proposed_html": "問いA。結論B。補足。"}

    monkeypatch.setattr(
        sentence_proofreading, "publish_shadow",
        preserve_locks,
    )
    result = runner._manuscript_publish_proofreading_shadow_v29(
        claimed, context
    ).context
    assert "補足。" in result["proposed_html"]
    assert received["_proofreading_locked_texts"] == ["問いA。", "結論B。"]
    assert "_proofreading_locked_texts" not in result


def test_v31_visible_citation_marker_needs_paragraph_lineage():
    runner = WorkerRunner.__new__(WorkerRunner)
    claimed = SimpleNamespace(workflow_name="authoring", workflow_version=31)
    context = {
        "proposed_html": (
            "<article id='a' lang='ja'><section id='s'><h2 id='h'>節</h2>"
            "<p id='p'>出典のない印。[^e1]</p></section></article>"
        ),
        "reference_entries": [],
        "applied_edits": [],
        "authoring_paragraph_citation_lineage": [],
    }
    with pytest.raises(FukamiruError, match="no paragraph citation lineage"):
        runner._manuscript_render_evidence_footnotes_v29(claimed, context)


def test_v31_question_uses_focused_stage_without_changing_v30(monkeypatch):
    runner = WorkerRunner.__new__(WorkerRunner)
    invoked = []

    def generate(_claimed, stage, _context, **_kwargs):
        invoked.append(stage)
        return {
            "research_question": {
                "text": "作品の配置は読まれ方とどう関わるか。",
                "provenance": {"source_ids": ["p1"]},
            },
            "topic_coverage": [{
                "topic_id": "t1", "proposition_ids": ["p1"],
                "disposition": "central",
            }],
            "required_user_information": [],
        }

    monkeypatch.setattr(runner, "_authoring_generate", generate)
    context = {
        "language": "ja",
        "propositions": [{"id": "p1", "text": "作品の配置は読まれ方に関わる。"}],
        "classifications": [],
    }
    for version in (30, 31):
        runner._authoring_formulate_question(
            SimpleNamespace(workflow_name="authoring", workflow_version=version), context
        )
    assert invoked == ["formulate_research_question", "research_question_v31"]
    assert "whole supplied manuscript" in STAGE_GUIDANCE["research_question_v31"]
    assert "broad enough" in STAGE_GUIDANCE["research_question"]
