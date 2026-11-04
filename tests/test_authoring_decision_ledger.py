from fukamiru.authoring.decision_ledger import build_decision_ledger


def test_decision_ledger_links_claim_to_passage_and_keeps_human_verdict_pending() -> None:
    ledger = build_decision_ledger(
        {
            "claims": [{"id": "c1", "text": "A bounded conclusion"}],
            "argument_nodes": [
                {
                    "id": "e1",
                    "claim_id": "c1",
                    "node_type": "evidence",
                    "text": "This passage supports a narrower reading",
                    "provenance": {"source_ids": ["s1"]},
                },
                {"id": "w1", "claim_id": "c1", "node_type": "warrant", "text": "The bridge"},
                {
                    "id": "x1",
                    "claim_id": "c1",
                    "node_type": "counterexample",
                    "text": "A rival reading",
                },
            ],
            "argument_claim_decisions": [
                {
                    "claim_id": "c1",
                    "disposition": "narrow",
                    "reason": "The source has a limited scope",
                    "counterinterpretation": "A rival reading",
                    "adopted_text": "A bounded conclusion",
                }
            ],
            "evidence_context": [
                {
                    "id": "s1",
                    "title": "Source",
                    "locator": "p. 3",
                    "excerpt": "Exact passage",
                    "verification_status": "verified",
                }
            ],
        }
    )
    entry = ledger["claims"][0]
    assert entry["source_passages"][0]["excerpt"] == "Exact passage"
    assert entry["warrants"][0]["text"] == "The bridge"
    assert entry["ai_decision"]["disposition"] == "narrow"
    assert entry["human_support_decision"] == "pending"
