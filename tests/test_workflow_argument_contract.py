from __future__ import annotations

import copy
import json

import pytest
from test_workflow_partial_response import claimed_v22
from test_workflow_structure_partial_acceptance import (
    STRUCTURE_HTML,
    _claimed,
    _context,
    _isolate_structure_storage,
)

from fukamiru.errors import ExternalServiceUnavailable, NonRetryableFukamiruError
from fukamiru.revisions.coherence import ManuscriptCoherenceService
from fukamiru.worker import WorkerRunner


@pytest.fixture
def worker_case():
    from test_worker import WorkerTests

    case = WorkerTests()
    case.setUp()
    try:
        yield case
    finally:
        case.tearDown()


def link_payload(source="EXAMPLE"):
    link = {
        "source_block_id": source, "source_reference_id": source + ":R1",
        "target_claim_block_id": "CLAIM", "target_claim_anchor": "A precise claim",
        "relation": "exemplifies", "observed_property": "Observed feature",
        "warrant": "The observation provides an instance of the claim",
        "evidence_required": False, "research_query": None,
    }
    unit_id = WorkerRunner._argument_link_response_unit_id(link)
    return {
        "argument_links": [link], "research_queries": [], "unresolved_questions": [],
        "link_dispositions": [{
            "unit_id": unit_id, "source_block_id": source,
            "source_reference_id": source + ":R1", "status": "linked",
            "explanation": "Matching link supplied",
        }],
    }, unit_id


@pytest.mark.parametrize("with_field", [False, True])
def test_valid_link_survives_malformed_sibling_and_unit_only_supplement(with_field):
    payload, unit_id = link_payload()
    payload["argument_links"].append({"source_block_id": "BAD", "warrant": None})
    allowed = {unit_id}
    if with_field:
        allowed.add("argument_process_field:argument_links")
    accepted = set(WorkerRunner._argument_link_covered_unit_ids_v22(payload, allowed))
    sliced = WorkerRunner._select_argument_link_payload_v22(payload, accepted, allowed)
    assert len(sliced["argument_links"]) == 1
    assert sliced["argument_links"][0]["source_block_id"] == "EXAMPLE"
    assert unit_id in WorkerRunner._argument_link_covered_unit_ids_v22(sliced, allowed)


def stable_records(case, claimed, ids, script, calls, revision=1):
    def invoke(units):
        calls.append([u["unit_id"] for u in units])
        payload = script.pop(0)
        if isinstance(payload, BaseException):
            raise payload
        return payload

    return case.worker._complete_partial_response_v22(
        claimed, phase="stable-test", signature_payload={"variable_targets": ids},
        units=[{"unit_id": i} for i in ids], unit_id=lambda u: u["unit_id"], invoke=invoke,
        covered_unit_ids=lambda p, allowed: [r["unit_id"] for r in p.get("records", [])],
        select_accepted_payload=lambda p, accepted, allowed: {
            "records": [r for r in p.get("records", []) if r["unit_id"] in accepted]
        },
        unit_signature_payloads={i: {
            "source_revision": revision[i] if isinstance(revision, dict) else revision,
        } for i in ids},
    )


def test_terminal_budget_survives_target_changes_but_resets_for_new_semantics(worker_case):
    claimed = claimed_v22(worker_case)
    calls = []
    _, terminal = stable_records(worker_case, claimed, ["A", "B"], [
        {"records": [{"unit_id": "A", "value": "saved"}]}, {"records": []},
    ], calls)
    assert terminal == ["B"]
    assert calls == [["A", "B"], ["B"]]
    assert stable_records(worker_case, claimed, ["B"], [], calls) == ([], ["B"])
    assert len(calls) == 2
    payloads, terminal = stable_records(worker_case, claimed, ["B"], [
        {"records": [{"unit_id": "B", "value": "new evidence"}]},
    ], calls, revision=2)
    assert terminal == []
    assert payloads[0]["records"][0]["value"] == "new evidence"


def test_reused_response_does_not_restore_sibling_from_old_semantics(worker_case):
    claimed = claimed_v22(worker_case)
    calls = []
    stable_records(worker_case, claimed, ["A", "B"], [{"records": [
        {"unit_id": "A", "value": "keep"}, {"unit_id": "B", "value": "outdated"},
    ]}], calls)
    payloads, terminal = stable_records(worker_case, claimed, ["A", "B"], [{"records": [
        {"unit_id": "B", "value": "updated"},
    ]}], calls, revision={"A": 1, "B": 2})
    assert terminal == []
    assert calls == [["A", "B"], ["B"]]
    assert [r for p in payloads for r in p["records"]] == [
        {"unit_id": "A", "value": "keep"}, {"unit_id": "B", "value": "updated"},
    ]


@pytest.mark.parametrize("failure", [ExternalServiceUnavailable("offline"), SystemExit("death")])
def test_sharded_supplement_reservation_survives_outage_and_interruption(worker_case, failure):
    claimed = claimed_v22(worker_case)
    calls = []
    with pytest.raises(type(failure)):
        stable_records(worker_case, claimed, ["A", "B"], [{"records": []}, failure], calls)
    if isinstance(failure, SystemExit):
        assert stable_records(worker_case, claimed, ["B", "A"], [], calls) == ([], ["A", "B"])
        assert len(calls) == 2
    else:
        _, terminal = stable_records(worker_case, claimed, ["B"], [
            {"records": [{"unit_id": "B"}]},
        ], calls)
        assert terminal == []
        assert calls[-1] == ["B"]


def test_corrupt_content_complete_pointer_is_rejected_before_reuse(worker_case):
    claimed = claimed_v22(worker_case)
    calls = []
    stable_records(worker_case, claimed, ["A"], [{"records": [{"unit_id": "A"}]}], calls)
    with worker_case.databases.state.connect() as db:
        db.execute(
            "UPDATE manuscript_unit_checkpoints SET payload_json=? "
            "WHERE run_id=? AND phase=? AND unit_id LIKE 'payload:%'",
            (json.dumps({"records": []}), claimed.run_id, "partial-response-v22:stable-test"),
        )
        db.commit()
    with pytest.raises(NonRetryableFukamiruError, match="stored response coverage"):
        stable_records(worker_case, claimed, ["A"], [], calls)
    assert len(calls) == 1


def test_selection_cannot_mark_lost_link_content_complete(worker_case):
    claimed = claimed_v22(worker_case)
    payload, unit_id = link_payload()
    with pytest.raises(NonRetryableFukamiruError, match="selection lost"):
        worker_case.worker._complete_partial_response_v22(
            claimed, phase="lost-link", signature_payload={}, units=[{"id": unit_id}],
            unit_id=lambda u: u["id"], invoke=lambda units: payload,
            covered_unit_ids=WorkerRunner._argument_link_covered_unit_ids_v22,
            select_accepted_payload=lambda p, accepted, allowed: {**p, "argument_links": []},
        )


def test_role_conflict_is_invalid_and_repair_includes_skeleton_and_dependents():
    from test_worker import FakeTranslator

    structure = ManuscriptCoherenceService.semantic_structure(STRUCTURE_HTML)
    plan = FakeTranslator().plan_manuscript_structure(semantic_structure=structure)
    for role in plan["block_roles"]:
        if role["block_id"] == "P-CLAIM":
            role["role"] = "conclusion"
    validation = WorkerRunner._argument_process_validation(
        structure, plan, "block_roles", {"full_stage": True}, strict_roles=True,
    )
    assert validation["valid"] is False
    assert "argument.central_claim_role" in {i["code"] for i in validation["issues"]}
    targets = WorkerRunner._manuscript_structure_repair_targets(structure, plan, validation)
    assert targets["skeleton"]["section_ids"]
    assert "P-EXAMPLE" in targets["argument"]["block_ids"]
    normalized, actions = ManuscriptCoherenceService.normalize_plan(
        structure, plan, repair_argument_roles=False,
    )
    claim_role = next(r for r in normalized["block_roles"] if r["block_id"] == "P-CLAIM")
    assert claim_role["role"] == "conclusion"
    assert not any(a["action"] == "set_central_claim_role" for a in actions)


class ConflictedRoles:
    def __init__(self, repair):
        from test_worker import SplitArgumentTranslator
        self.delegate = SplitArgumentTranslator()
        self.repair = repair
        self.role_calls = 0
        self.skeleton_calls = []
        self.link_calls = 0

    def plan_manuscript_structure_stage(self, **kwargs):
        if kwargs["stage"] == "skeleton":
            self.skeleton_calls.append(kwargs)
        return self.delegate.plan_manuscript_structure_stage(**kwargs)

    def plan_manuscript_argument_process(self, **kwargs):
        result = self.delegate.plan_manuscript_argument_process(**kwargs)
        if kwargs["process_name"] == "block_roles":
            self.role_calls += 1
            if not self.repair or self.role_calls == 1:
                for role in result["block_roles"]:
                    if role["block_id"] == "P-CLAIM":
                        role["role"] = "conclusion"
        if kwargs["process_name"] == "claim_evidence_links":
            self.link_calls += 1
        return result


def test_role_conflict_repairs_jointly_before_link_generation(worker_case, monkeypatch):
    _isolate_structure_storage(monkeypatch)
    translator = ConflictedRoles(repair=True)
    worker_case.worker.translator = translator
    result = worker_case.worker._manuscript_rewrite_plan(
        _claimed(worker_case, version=23), _context(),
    ).context
    assert result["structure_plan_validation"]["valid"]
    assert translator.role_calls == 2
    assert translator.link_calls == 1
    assert len(translator.skeleton_calls) == 2
    assert any(i["code"] == "argument.central_claim_role"
               for i in translator.skeleton_calls[-1]["validation_feedback"])


def test_exhausted_role_conflict_is_non_retryable(worker_case, monkeypatch):
    _isolate_structure_storage(monkeypatch)
    translator = ConflictedRoles(repair=False)
    worker_case.worker.translator = translator
    with pytest.raises(NonRetryableFukamiruError, match="deterministic validation"):
        worker_case.worker._manuscript_rewrite_plan(_claimed(worker_case, version=23), _context())
    assert translator.role_calls == 2
    assert translator.link_calls == 0


def test_query_candidates_survive_link_rejection(worker_case, monkeypatch):
    from test_worker import SplitArgumentTranslator

    from fukamiru.revisions.reasoning import ManuscriptReasoningRepository

    class RejectedLink(SplitArgumentTranslator):
        def plan_manuscript_argument_process(self, **kwargs):
            payload = super().plan_manuscript_argument_process(**kwargs)
            if kwargs["process_name"] == "claim_evidence_links":
                for link in payload["argument_links"]:
                    link["target_claim_anchor"] = "An ungrounded invented claim"
                    link["target_claim_block_id"] = "P-EXAMPLE"
                payload["research_queries"] = ["作品A 具体例 論証 観察特性"]
                payload["unresolved_questions"] = ["Which claim does this example support?"]
            return payload

    _isolate_structure_storage(monkeypatch)
    saved = []

    def save_plan(_repository, **kwargs):
        saved.append(copy.deepcopy(kwargs))
        return {"id": "plan:test", "plan": kwargs["plan"], "validation": kwargs["validation"]}

    monkeypatch.setattr(ManuscriptReasoningRepository, "save_structure_plan", save_plan)
    worker_case.worker.translator = RejectedLink()
    with pytest.raises(NonRetryableFukamiruError):
        worker_case.worker._manuscript_rewrite_plan(_claimed(worker_case, version=23), _context())
    plan = saved[-1]["plan"]
    assert "作品A 具体例 論証 観察特性" in plan["research_queries"]
    assert plan["unresolved_questions"]
    assert plan["argument_research_records"][0]["source_units"]
    assert all(d["status"] != "linked" for d in plan["link_dispositions"])
    assert saved[-1]["validation"]["valid"] is False
    assert saved[-1]["validation"]["valid_for_conditional_execution"] is False
    assert any(d["status"] == "link_rejected" for d in plan["link_dispositions"])
    assert set(plan["_argument_semantic_repair_ledger"].values()) == {1}


def test_unit_identity_ignores_diagnostics_target_set_and_record_order():
    from test_worker import FakeTranslator

    structure = ManuscriptCoherenceService.semantic_structure(STRUCTURE_HTML)
    plan = FakeTranslator().plan_manuscript_structure(semantic_structure=structure)
    units = [{"unit_id": "A", "source_block_id": "P-EXAMPLE"}]
    before = WorkerRunner._argument_unit_inputs_v23(structure, plan, units, {})
    updated = copy.deepcopy(plan)
    updated["_argument_process_states"] = [{"status": "new progress"}]
    updated["block_roles"].reverse()
    after = WorkerRunner._argument_unit_inputs_v23(structure, updated, units, {})
    assert before == after
    assert WorkerRunner._argument_unit_inputs_v23(structure, plan, units[:1], {}) == before


def test_partial_review_retains_valid_findings_metadata_and_field_diagnostics(
    worker_case, monkeypatch,
):
    from fukamiru.revisions import ManuscriptRewriteService
    from fukamiru.revisions.reasoning import ManuscriptReasoningRepository

    blocks = ManuscriptCoherenceService.semantic_structure(STRUCTURE_HTML)["blocks"]
    monkeypatch.setattr(ManuscriptRewriteService, "prepare", lambda *a, **k: {
        "base_html": STRUCTURE_HTML, "editable_blocks": blocks, "source_lineage": [],
    })
    monkeypatch.setattr(ManuscriptReasoningRepository, "save_review", lambda *a, **k: {
        "id": "review:test",
    })
    calls = []

    class PartialReview:
        def review_manuscript_blocks(self, **kwargs):
            selected = [b["id"] for b in kwargs["blocks"]]
            calls.append(selected)
            findings = [{
                "id": "good", "category": "style", "severity": "minor",
                "block_ids": ["P-CLAIM"], "explanation": "A valid finding",
                "recommendation": "Clarify the subject", "requires_research": False,
                "research_queries": [],
            }] if "P-CLAIM" in selected else []
            findings.append({
                "id": "bad", "category": "invented-category", "severity": "bad-label",
                "block_ids": ["P-EXAMPLE"], "explanation": "Needs inspection",
                "recommendation": "Inspect example", "requires_research": False,
                "research_queries": [],
            })
            return {
                "assessment": "A partial batch assessment", "checked_block_ids": selected,
                "findings": findings, "rewrite_priorities": ["Clarify the subject"],
                "research_queries": ["作品A 主張 具体例 観察特性"],
            }

    worker_case.worker.translator = PartialReview()
    claimed = _claimed(worker_case, version=23)
    context = {**_context(), "document_map": [], "use_rag": False}
    result = worker_case.worker._manuscript_rewrite_review(claimed, context).context
    assessment = result["reasoning_review"][0]
    assert assessment["assessment"] == "A partial batch assessment"
    assert len(assessment["findings"]) == 1
    assert assessment["research_queries"] == ["作品A 主張 具体例 観察特性"]
    assert assessment["assessment_scopes"][0]["complete"] is False
    assert calls[-1] == ["P-EXAMPLE"]
    with worker_case.databases.state.connect() as db:
        received = [json.loads(r[0]) for r in db.execute(
            "SELECT payload_json FROM manuscript_unit_checkpoints "
            "WHERE run_id=? AND unit_id LIKE 'received:%'", (claimed.run_id,),
        )]
    assert any(d["field"].endswith(".category") for r in received for d in r["diagnostics"]
               if d.get("code") == "response_contract.enum_invalid")
    assert result["conditional_conditions"][0]["unit_ids"] == ["P-EXAMPLE"]


def test_completed_role_checkpoint_is_revalidated_semantically(worker_case, monkeypatch):
    from test_worker import FakeTranslator, SplitArgumentTranslator

    from fukamiru.revisions.reasoning import ManuscriptReasoningRepository

    _isolate_structure_storage(monkeypatch)
    structure = ManuscriptCoherenceService.semantic_structure(STRUCTURE_HTML)
    wrong_roles = FakeTranslator().plan_manuscript_structure(
        semantic_structure=structure,
    )["block_roles"]
    for role in wrong_roles:
        if role["block_id"] == "P-CLAIM":
            role["role"] = "conclusion"

    def checkpoint(_repository, **kwargs):
        if kwargs["process_name"] == "block_roles":
            return {"status": "completed", "output": {"block_roles": wrong_roles}}
        raise LookupError("not cached")

    monkeypatch.setattr(ManuscriptReasoningRepository, "argument_checkpoint", checkpoint)
    translator = SplitArgumentTranslator()
    worker_case.worker.translator = translator
    result = worker_case.worker._manuscript_rewrite_plan(
        _claimed(worker_case, version=23), _context(),
    ).context
    assert result["structure_plan_validation"]["valid"]
    assert any(c["process_name"] == "block_roles" for c in translator.argument_process_calls)


def test_generation_prompt_contains_strict_contract_while_receiver_accepts_partial(
    worker_case, monkeypatch, tmp_path,
):
    from pathlib import Path

    from test_workflow_transport_regressions import install_cli_fixture

    adapter, _, _ = install_cli_fixture(worker_case, monkeypatch, tmp_path, [])
    prompts = []

    class Process:
        returncode = 0

        def __init__(self, command, **kwargs):
            self.output = Path(command[command.index("-o") + 1])

        def communicate(self, prompt, **kwargs):
            prompts.append(prompt)
            self.output.write_text(json.dumps({
                "assessment": "Partial", "checked_block_ids": ["A"], "findings": [],
                "rewrite_priorities": [], "research_queries": [],
            }), encoding="utf-8")
            return "", ""

    monkeypatch.setattr("fukamiru.translation.codex.subprocess.Popen", Process)
    result = worker_case.worker._invoke_manuscript_inference(
        claimed_v22(worker_case), "review_manuscript_blocks", document_title="Fixture",
        instructions="Review", human_review="", blocks=[{"id": "A", "text": "Source"}],
        document_map=[], outline_context={}, source_lineage=[],
    )
    assert result["checked_block_ids"] == ["A"]
    prompt = prompts[0].split("COMPLETE RESPONSE CONTRACT (application-owned):", 1)[1]
    assert '"enum": ["info", "minor", "major", "critical"]' in prompt
    assert '"required"' in prompt
    assert adapter is worker_case.worker.translator
