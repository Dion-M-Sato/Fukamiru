"""Publication-neutral metadata evidence must remain identity-bound."""

from copy import deepcopy

import pytest

from fukamiru.bibliographic_metadata import reconcile_verified_publisher_metadata


def context():
    record = {
        "reference_id": "fixture-chapter",
        "title": "架空の観察研究",
        "author": "山田花子",
        "container_title": "架空論集",
        "year": "2001",
        "pages": "11-20",
        "publisher": None,
    }
    identity = {key: value for key, value in record.items() if key != "publisher"}
    record["field_evidence"] = {
        "publisher": [
            {
                "bibliographic_identity": identity,
                "value": "Example Press",
                "source_url": "https://publisher.example.test/fictional-chapter",
                "verification_status": "corroborated",
            }
        ]
    }
    return {"reference_entries": [record]}


def test_identity_bound_evidence_adds_publisher_without_modifying_input():
    source = context()
    original = deepcopy(source)
    output, corrections = reconcile_verified_publisher_metadata(source)
    assert source == original
    assert output["reference_entries"][0]["publisher"] == "Example Press"
    assert corrections[0]["reference_id"] == "fixture-chapter"
    assert output["reference_entries"][0]["provenance"]["publisher"]["source_url"].endswith(
        "/fictional-chapter"
    )


@pytest.mark.parametrize("field", ["reference_id", "title", "author", "pages", "year"])
def test_changed_identity_never_applies_evidence(field):
    source = context()
    source["reference_entries"][0][field] = "different"
    output, corrections = reconcile_verified_publisher_metadata(source)
    assert output is source
    assert corrections == []
    assert output["reference_entries"][0]["publisher"] is None


@pytest.mark.parametrize(
    "change",
    [
        {"verification_status": "candidate"},
        {"source_url": "file:///private/record"},
        {"source_url": "https://user:secret@publisher.example.test/record"},
        {"bibliographic_identity": {}},
    ],
)
def test_unverified_or_unbound_evidence_remains_for_review(change):
    source = context()
    source["reference_entries"][0]["field_evidence"]["publisher"][0].update(change)
    assert reconcile_verified_publisher_metadata(source) == (source, [])


def test_conflicting_corroborated_values_do_not_select_a_publisher():
    source = context()
    evidence = source["reference_entries"][0]["field_evidence"]["publisher"]
    evidence.append({**deepcopy(evidence[0]), "value": "Other Press"})
    assert reconcile_verified_publisher_metadata(source) == (source, [])
