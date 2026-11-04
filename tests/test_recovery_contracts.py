"""Recovery depends on verified identity and bounded corrections in any order."""

from copy import deepcopy

import pytest

from fukamiru.storage.recovery_contracts import recovery_available, verified_scholarship_anchor


def source_context():
    context = {
        "section_contracts": [{
            "section_id": "ending", "article_role": "conclusion",
            "claim_ids": ["claim-alpha"], "evidence_ids": ["evidence-beta"],
        }],
        "argument_nodes": [{
            "id": "evidence-beta", "node_type": "evidence", "claim_id": "claim-alpha",
            "provenance": {"source_ids": ["citation-gamma"]},
        }],
        "annotation_citation_evidence": [{
            "id": "citation-gamma", "title": "架空の分類研究", "authors": ["山田花子"],
            "allowed_use": "attributed_scholarship_citation",
            "verification_scope": "bibliographic_identity_only",
            "provenance": {
                "bibliographic_identity_verified": True, "substantive_support_verified": False,
            },
        }],
    }
    message = (
        "V32 role draft JSON handoff violation: section ending scholarship evidence "
        "evidence-beta has no unique sentence-level footnote anchor"
    )
    return context, message


def test_unrelated_source_ids_and_titles_can_recover_without_a_past_run():
    context, message = source_context()
    original = deepcopy(context)
    assert verified_scholarship_anchor(context, message) == "footnote"
    assert context == original


@pytest.mark.parametrize("failure", ["unverified", "ambiguous", "unlinked", "mixed"])
def test_anchor_rejects_missing_provenance_and_mixed_failures(failure):
    context, message = source_context()
    citations = context["annotation_citation_evidence"]
    if failure == "unverified":
        citations[0]["provenance"]["bibliographic_identity_verified"] = False
    elif failure == "ambiguous":
        citations.append(deepcopy(citations[0]))
    elif failure == "unlinked":
        context["argument_nodes"][0]["provenance"]["source_ids"] = ["other"]
    else:
        message += "; section body has invalid output schema"
    assert verified_scholarship_anchor(context, message) is None


def test_corrections_are_once_per_policy_and_do_not_require_a_fixed_order():
    source = {
        "policy": "authoring-v31-citation-source-claim-bridge-recovery-v1",
        "v31_citation_source_claim_mapping_recovery": True,
    }
    identity = {
        "policy": "authoring-v31-section-contract-identity-merge-v1",
        "v31_section_contract_identity_recovery": True,
    }
    next_policy = "authoring-v31-support-sentence-hint-recovery-v1"
    for ledger in ([source, identity], [identity, source]):
        assert recovery_available(ledger, next_policy, workflow_version="32")
        assert not recovery_available(ledger, source["policy"], workflow_version="32")
    assert not recovery_available([source, source], next_policy, workflow_version="32")
    assert not recovery_available(
        [{**source, "workflow_version": "31"}], next_policy, workflow_version="32"
    )
    assert not recovery_available(
        [{**source, "v31_citation_source_claim_mapping_recovery": False}],
        next_policy, workflow_version="32",
    )
