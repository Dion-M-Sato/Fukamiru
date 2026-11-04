from __future__ import annotations

from dataclasses import replace

import pytest

from fukamiru.errors import FukamiruError
from fukamiru.revisions import ManuscriptRewriteService
from fukamiru.revisions.coherence import ManuscriptCoherenceService
from fukamiru.revisions.reasoning import ManuscriptReasoningRepository

STRUCTURE_HTML = (
    '<article id="DOC-PARTIAL" lang="ja"><section id="SEC-PARTIAL">'
    '<h2 id="H-PARTIAL">論点</h2>'
    '<p id="P-CLAIM">この節の中心命題を示す。</p>'
    '<p id="P-EXAMPLE">『作品A』の特徴を具体例として検討する。</p>'
    "</section></article>"
)


@pytest.fixture
def worker_case():
    from test_worker import WorkerTests

    case = WorkerTests()
    case.setUp()
    try:
        yield case
    finally:
        case.tearDown()


def _claimed(case, *, version: int):
    run_id = case._create_run("manuscript_rewrite")
    claimed = case.worker._claim(lane="ingestion_cpu")
    assert claimed is not None and claimed.run_id == run_id
    return replace(
        claimed,
        node_id="plan_manuscript_structure",
        workflow_version=str(version),
        resource_lane="codex_remote",
    )


def _isolate_structure_storage(monkeypatch) -> None:
    def prepare(_service, **_kwargs):
        return {"base_html": STRUCTURE_HTML, "source_lineage": []}

    def no_stored_plan(_repository, _run_id):
        raise LookupError("no plan")

    def save_plan(_repository, **kwargs):
        return {
            "id": "structure-plan:test",
            "plan": kwargs["plan"],
            "validation": kwargs["validation"],
        }

    def no_argument_checkpoint(_repository, **_kwargs):
        raise LookupError("no argument checkpoint")

    def save_argument_checkpoint(_repository, **kwargs):
        return {
            "status": "completed" if not kwargs["validation"].get("issues") else "invalid",
            "output": kwargs["output"],
            "validation": kwargs["validation"],
        }

    monkeypatch.setattr(ManuscriptRewriteService, "prepare", prepare)
    monkeypatch.setattr(
        ManuscriptReasoningRepository,
        "structure_plan_for_run",
        no_stored_plan,
    )
    monkeypatch.setattr(
        ManuscriptReasoningRepository,
        "save_structure_plan",
        save_plan,
    )
    monkeypatch.setattr(
        ManuscriptReasoningRepository,
        "argument_checkpoint",
        no_argument_checkpoint,
    )
    monkeypatch.setattr(
        ManuscriptReasoningRepository,
        "save_argument_checkpoint",
        save_argument_checkpoint,
    )


def _context() -> dict:
    return {
        "workflow_version": 22,
        "document_id": "document:test",
        "base_revision_id": "revision:test",
        "document_title": "部分応答テスト",
        "instructions": "原文の論理構造を保持する。",
        "human_review": "",
        "reasoning_review_id": "reasoning:test",
        "reasoning_review": [],
        "outline_context": {},
        "source_lineage": [],
        "proposed_html": STRUCTURE_HTML,
    }


class _PartialBlockRoleTranslator:
    """Delegate valid payload construction to the established test fixture."""

    def __init__(self, *, terminal_missing: bool):
        from test_worker import SplitArgumentTranslator

        self._delegate = SplitArgumentTranslator()
        self.terminal_missing = terminal_missing
        self.block_role_calls: list[dict] = []
        self.process_calls: list[str] = []

    def plan_manuscript_structure_stage(self, **kwargs):
        return self._delegate.plan_manuscript_structure_stage(**kwargs)

    def plan_manuscript_argument_process(
        self,
        *,
        process_name: str,
        repair_targets: dict,
        **kwargs,
    ):
        self.process_calls.append(process_name)
        payload = self._delegate.plan_manuscript_argument_process(
            process_name=process_name,
            repair_targets=repair_targets,
            **kwargs,
        )
        if process_name != "block_roles":
            return payload
        self.block_role_calls.append(dict(repair_targets))
        call_index = len(self.block_role_calls)
        if call_index == 1:
            payload["block_roles"] = [
                item
                for item in payload["block_roles"]
                if item["block_id"] != "P-EXAMPLE"
            ]
        elif self.terminal_missing:
            payload["block_roles"] = []
        else:
            payload["block_roles"] = [
                item
                for item in payload["block_roles"]
                if item["block_id"] == "P-EXAMPLE"
            ]
        return payload


def test_v22_argument_process_keeps_valid_units_and_supplements_only_missing_once(
    worker_case,
    monkeypatch,
) -> None:
    _isolate_structure_storage(monkeypatch)
    translator = _PartialBlockRoleTranslator(terminal_missing=False)
    worker_case.worker.translator = translator

    result = worker_case.worker._manuscript_rewrite_plan(
        _claimed(worker_case, version=22), _context()
    ).context

    assert len(translator.block_role_calls) == 2
    assert translator.block_role_calls[0]["full_stage"] is True
    assert translator.block_role_calls[1]["full_stage"] is False
    assert translator.block_role_calls[1]["block_ids"] == ["P-EXAMPLE"]
    assert translator.process_calls.count("reference_classification") == 1
    assert translator.process_calls.count("claim_evidence_links") == 1

    roles = result["structure_plan"]["block_roles"]
    assert [item["block_id"] for item in roles].count("H-PARTIAL") == 1
    assert [item["block_id"] for item in roles].count("P-CLAIM") == 1
    assert [item["block_id"] for item in roles].count("P-EXAMPLE") == 1
    assert not [
        condition
        for condition in result.get("conditional_conditions", [])
        if condition.get("reason") == "response_contract_incomplete"
    ]
    states = result["structure_plan"]["_argument_process_states"]
    assert [item["process_name"] for item in states] == [
        "block_roles",
        "reference_classification",
        "claim_evidence_links",
        "integration_validation",
    ]


def test_v22_argument_second_omission_preserves_source_without_fabricating_role(
    worker_case,
    monkeypatch,
) -> None:
    _isolate_structure_storage(monkeypatch)
    translator = _PartialBlockRoleTranslator(terminal_missing=True)
    worker_case.worker.translator = translator

    result = worker_case.worker._manuscript_rewrite_plan(
        _claimed(worker_case, version=22), _context()
    ).context

    assert len(translator.block_role_calls) == 2
    assert translator.block_role_calls[1]["full_stage"] is False
    assert translator.block_role_calls[1]["block_ids"] == ["P-EXAMPLE"]
    assert result["proposed_html"] == STRUCTURE_HTML
    assert result["completion_status"] == "conditional_success"
    conditions = [
        condition
        for condition in result["conditional_conditions"]
        if condition.get("reason") == "response_contract_incomplete"
    ]
    assert conditions == [
        {
            "reason": "response_contract_incomplete",
            "phase": "argument_process:block_roles",
            "unit_count": 1,
            "unit_ids": ["block_role:P-EXAMPLE"],
            "retry_count": 1,
            "disposition": "conditional_proposal",
        }
    ]

    # Accepted siblings are retained, but an unanswered semantic role must not
    # be invented merely to make deterministic validation appear successful.
    roles = {
        item["block_id"]: item for item in result["structure_plan"]["block_roles"]
    }
    assert roles["H-PARTIAL"]["role"] == "heading"
    assert roles["P-CLAIM"]["role"] == "claim"
    assert "P-EXAMPLE" not in roles
    assert result["source_preserve_block_ids"] == ["P-EXAMPLE"]
    assert result["structure_plan_validation"]["valid"] is False
    assert result["structure_plan_validation"]["valid_for_conditional_execution"] is True


class _ContradictoryStructureTranslator:
    def __init__(self):
        from test_worker import SplitArgumentTranslator

        self._delegate = SplitArgumentTranslator()

    def plan_manuscript_structure_stage(self, **kwargs):
        payload = self._delegate.plan_manuscript_structure_stage(**kwargs)
        if kwargs["stage"] == "skeleton":
            payload["sections"][0]["central_claim_block_id"] = "UNKNOWN-BLOCK"
        return payload

    def plan_manuscript_argument_process(self, **kwargs):
        return self._delegate.plan_manuscript_argument_process(**kwargs)


class _PartialDiscourseFieldTranslator:
    def __init__(self, *, terminal_missing: bool):
        from test_worker import SplitArgumentTranslator

        self._delegate = SplitArgumentTranslator()
        self.terminal_missing = terminal_missing
        self.discourse_calls: list[dict] = []

    def plan_manuscript_structure_stage(self, **kwargs):
        payload = self._delegate.plan_manuscript_structure_stage(**kwargs)
        if kwargs["stage"] != "discourse":
            return payload
        self.discourse_calls.append(dict(kwargs["repair_targets"]))
        if len(self.discourse_calls) == 1 or self.terminal_missing:
            payload.pop("merge_groups", None)
        return payload

    def plan_manuscript_argument_process(self, **kwargs):
        return self._delegate.plan_manuscript_argument_process(**kwargs)


class _SemanticUnresolvedLinkTranslator:
    def __init__(self):
        from test_worker import SplitArgumentTranslator

        self._delegate = SplitArgumentTranslator()

    def plan_manuscript_structure_stage(self, **kwargs):
        return self._delegate.plan_manuscript_structure_stage(**kwargs)

    def plan_manuscript_argument_process(self, **kwargs):
        payload = self._delegate.plan_manuscript_argument_process(**kwargs)
        if kwargs["process_name"] != "claim_evidence_links":
            return payload
        target = next(
            item
            for item in payload["link_dispositions"]
            if item["source_block_id"] == "P-EXAMPLE"
            and item["source_reference_id"] is not None
        )
        payload["argument_links"] = [
            item
            for item in payload["argument_links"]
            if not (
                item["source_block_id"] == target["source_block_id"]
                and item["source_reference_id"] == target["source_reference_id"]
            )
        ]
        target["status"] = "semantic_unresolved"
        target["explanation"] = "The evidence relation requires human judgement."
        return payload


class _NotApplicableRequiredExampleTranslator(_SemanticUnresolvedLinkTranslator):
    def plan_manuscript_argument_process(self, **kwargs):
        payload = super().plan_manuscript_argument_process(**kwargs)
        if kwargs["process_name"] != "claim_evidence_links":
            return payload
        target = next(
            item
            for item in payload["link_dispositions"]
            if item["source_block_id"] == "P-EXAMPLE"
            and item["source_reference_id"] is not None
        )
        target["status"] = "not_applicable"
        target["explanation"] = "No link is required."
        return payload


def test_v22_valid_default_does_not_hide_missing_stage_field(
    worker_case,
    monkeypatch,
) -> None:
    _isolate_structure_storage(monkeypatch)
    translator = _PartialDiscourseFieldTranslator(terminal_missing=False)
    worker_case.worker.translator = translator

    result = worker_case.worker._manuscript_rewrite_plan(
        _claimed(worker_case, version=22), _context()
    ).context

    assert len(translator.discourse_calls) == 2
    assert translator.discourse_calls[1]["full_stage"] is False
    assert translator.discourse_calls[1]["response_fields"] == ["merge_groups"]
    assert result["structure_plan_validation"]["valid"] is True
    assert not result.get("conditional_conditions")


def test_v22_terminal_stage_field_omission_is_not_reported_as_valid(
    worker_case,
    monkeypatch,
) -> None:
    _isolate_structure_storage(monkeypatch)
    translator = _PartialDiscourseFieldTranslator(terminal_missing=True)
    worker_case.worker.translator = translator

    result = worker_case.worker._manuscript_rewrite_plan(
        _claimed(worker_case, version=22), _context()
    ).context

    assert len(translator.discourse_calls) == 2
    assert result["structure_plan_validation"]["valid"] is False
    assert result["structure_plan_validation"]["valid_for_conditional_execution"] is True
    conditions = [
        item
        for item in result["conditional_conditions"]
        if item.get("reason") == "response_contract_incomplete"
    ]
    assert conditions == [
        {
            "reason": "response_contract_incomplete",
            "phase": "structure_stage:discourse",
            "unit_count": 1,
            "unit_ids": ["stage_field:merge_groups"],
            "retry_count": 1,
            "disposition": "conditional_proposal",
        }
    ]


def test_v22_semantic_unresolved_is_distinct_and_source_preserved(
    worker_case,
    monkeypatch,
) -> None:
    _isolate_structure_storage(monkeypatch)
    worker_case.worker.translator = _SemanticUnresolvedLinkTranslator()

    result = worker_case.worker._manuscript_rewrite_plan(
        _claimed(worker_case, version=22), _context()
    ).context

    semantic = [
        item
        for item in result["conditional_conditions"]
        if item.get("reason") == "semantic_resolution_required"
    ]
    assert len(semantic) == 1
    assert semantic[0]["block_ids"] == ["P-EXAMPLE"]
    assert "P-EXAMPLE" in result["source_preserve_block_ids"]
    assert not [
        item
        for item in result["conditional_conditions"]
        if item.get("reason") == "response_contract_incomplete"
    ]
    assert result["structure_plan_validation"]["valid"] is False
    assert result["structure_plan_validation"]["valid_for_conditional_execution"] is True


def test_v22_required_example_cannot_become_not_applicable(
    worker_case,
    monkeypatch,
) -> None:
    _isolate_structure_storage(monkeypatch)
    worker_case.worker.translator = _NotApplicableRequiredExampleTranslator()

    result = worker_case.worker._manuscript_rewrite_plan(
        _claimed(worker_case, version=22), _context()
    ).context

    disposition = next(
        item
        for item in result["structure_plan"]["link_dispositions"]
        if item["source_block_id"] == "P-EXAMPLE"
        and item["source_reference_id"] is not None
    )
    assert disposition["status"] == "semantic_unresolved"
    assert result["structure_plan_validation"]["valid_for_conditional_execution"] is True
    assert "P-EXAMPLE" in result["source_preserve_block_ids"]


def test_v22_tadashi_normalization_is_audited_but_not_blocking(worker_case) -> None:
    class ValidReport:
        @staticmethod
        def as_dict() -> dict[str, object]:
            return {"valid": True, "issues": []}

    action = {"action": "classify_tadashi", "block_id": "P-EXAMPLE"}
    legacy = worker_case.worker._manuscript_structure_validation(ValidReport(), [action])
    current = worker_case.worker._manuscript_structure_validation(
        ValidReport(),
        [action],
        allow_tadashi_normalization=True,
    )

    assert legacy["valid"] is False
    assert current["valid"] is True
    assert current["normalization_actions"] == [action]
    assert current["blocking_normalization_actions"] == []


def test_v22_final_failed_validation_is_persisted(
    worker_case,
    monkeypatch,
) -> None:
    _isolate_structure_storage(monkeypatch)
    saved: list[dict] = []

    def save_plan(_repository, **kwargs):
        saved.append(kwargs)
        return {
            "id": "structure-plan:test",
            "plan": kwargs["plan"],
            "validation": kwargs["validation"],
        }

    monkeypatch.setattr(ManuscriptReasoningRepository, "save_structure_plan", save_plan)
    worker_case.worker.translator = _ContradictoryStructureTranslator()

    with pytest.raises(
        FukamiruError,
        match="Manuscript structure plan failed deterministic validation",
    ):
        worker_case.worker._manuscript_rewrite_plan(
            _claimed(worker_case, version=22), _context()
        )

    assert saved
    assert saved[-1]["validation"]["valid"] is False
    assert saved[-1]["validation"]["valid_for_conditional_execution"] is False


def test_v22_no_transition_allows_nullable_transition_text(worker_case) -> None:
    structure = ManuscriptCoherenceService.semantic_structure(STRUCTURE_HTML)
    from test_worker import SplitArgumentTranslator

    payload = SplitArgumentTranslator().plan_manuscript_structure_stage(
        stage="skeleton",
        semantic_structure=structure,
        repair_targets={"full_stage": True},
    )
    payload["sections"][0]["transition_needed"] = False
    payload["sections"][0]["transition_from_previous"] = None
    payload["sections"][0]["transition_reason"] = None

    missing = worker_case.worker._structure_stage_response_contract_missing_units_v22(
        structure,
        "skeleton",
        {"full_stage": True},
        payload,
    )

    assert "section:SEC-PARTIAL" not in missing


def test_v22_rejects_completed_checkpoint_without_link_dispositions(
    worker_case,
    monkeypatch,
) -> None:
    from test_worker import SplitArgumentTranslator

    _isolate_structure_storage(monkeypatch)

    def legacy_checkpoint(_repository, *, process_name, **_kwargs):
        if process_name != "claim_evidence_links":
            raise LookupError("no checkpoint")
        return {
            "status": "completed",
            "output": {
                "argument_links": [],
                "research_queries": [],
                "unresolved_questions": [],
            },
        }

    monkeypatch.setattr(
        ManuscriptReasoningRepository,
        "argument_checkpoint",
        legacy_checkpoint,
    )
    translator = SplitArgumentTranslator()
    worker_case.worker.translator = translator

    worker_case.worker._manuscript_rewrite_plan(
        _claimed(worker_case, version=22), _context()
    )

    assert [
        item
        for item in translator.argument_process_calls
        if item["process_name"] == "claim_evidence_links"
    ]


def test_v22_source_restore_changes_only_protected_block(worker_case) -> None:
    candidate = STRUCTURE_HTML.replace(
        "この節の中心命題を示す。",
        "この節の中心命題を詳しく示す。",
    ).replace(
        "『作品A』の特徴を具体例として検討する。",
        "『作品A』を別の仕方で検討する。",
    )

    restored, restored_ids = worker_case.worker._restore_source_preserved_blocks_v22(
        STRUCTURE_HTML,
        candidate,
        {"P-EXAMPLE"},
    )

    assert restored_ids == ["P-EXAMPLE"]
    assert "この節の中心命題を詳しく示す。" in restored
    assert "『作品A』の特徴を具体例として検討する。" in restored
    assert "『作品A』を別の仕方で検討する。" not in restored


def test_v22_unresolved_central_claim_preserves_dependency_section(worker_case) -> None:
    from test_worker import SplitArgumentTranslator

    structure = ManuscriptCoherenceService.semantic_structure(STRUCTURE_HTML)
    translator = SplitArgumentTranslator()
    plan = translator.plan_manuscript_structure_stage(
        stage="skeleton",
        semantic_structure=structure,
        repair_targets={"full_stage": True},
    )
    plan.update(
        translator.plan_manuscript_argument_process(
            process_name="block_roles",
            semantic_structure=structure,
            repair_targets={"full_stage": True},
        )
    )
    unit = {"source_block_id": "P-CLAIM", "source_reference_id": None}
    plan["link_dispositions"] = [
        {
            **unit,
            "unit_id": worker_case.worker._argument_link_response_unit_id(unit),
            "status": "semantic_unresolved",
            "explanation": "The central claim relation needs human judgement.",
        }
    ]

    protected = worker_case.worker._structure_response_preserved_block_ids_v22(
        plan, structure
    )

    assert protected == {"H-PARTIAL", "P-CLAIM", "P-EXAMPLE"}


def test_v22_complete_but_logically_contradictory_structure_still_fails(
    worker_case,
    monkeypatch,
) -> None:
    _isolate_structure_storage(monkeypatch)
    worker_case.worker.translator = _ContradictoryStructureTranslator()

    with pytest.raises(
        FukamiruError,
        match="Manuscript structure plan failed deterministic validation",
    ):
        worker_case.worker._manuscript_rewrite_plan(
            _claimed(worker_case, version=22), _context()
        )


def test_v21_staged_argument_flow_never_enters_v22_partial_protocol(
    worker_case,
    monkeypatch,
) -> None:
    from test_worker import SplitArgumentTranslator

    _isolate_structure_storage(monkeypatch)
    worker_case.worker.translator = SplitArgumentTranslator()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("V21 must not enter the V22 partial-response protocol")

    monkeypatch.setattr(
        worker_case.worker,
        "_complete_partial_response_v22",
        forbidden,
    )
    context = _context()
    context["workflow_version"] = 21
    result = worker_case.worker._manuscript_rewrite_plan(
        _claimed(worker_case, version=21), context
    ).context

    assert result["structure_plan_validation"]["valid"] is True
    assert not result.get("conditional_conditions")
