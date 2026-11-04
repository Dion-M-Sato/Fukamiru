from __future__ import annotations

import json
from dataclasses import replace

import pytest

from fukamiru.errors import FukamiruError
from fukamiru.literature import LiteratureCandidate
from fukamiru.revisions.references import JapaneseReferenceService
from fukamiru.storage.runs import RunRepository
from fukamiru.worker import runner as runner_module
from fukamiru.worker.runner import ClaimedNode, HandlerResult

HTML = (
    '<article id="DOC" lang="ja"><section id="BODY">'
    '<h2 id="HEAD">本文</h2><p id="P1">論証本文。</p>'
    "</section></article>"
)


def _worker_case():
    from test_worker import WorkerTests

    case = WorkerTests()
    case.setUp()
    return case


def _claim_v22(case) -> ClaimedNode:
    run_id = case._create_run("manuscript_rewrite")
    claimed = case.worker._claim(lane="ingestion_cpu")
    assert claimed is not None and claimed.run_id == run_id
    return replace(
        claimed,
        node_id="resolve_manuscript_reference_metadata",
        workflow_version="22",
        resource_lane="codex_remote",
    )


def _machine_usage() -> dict:
    return {
        "policy_id": "reference-usage-v22",
        "usage_id": "usage:related-work",
        "candidate_key": "crossref:seed",
        "query": "共通著作",
        "obligation_ids": ["OBL-1"],
        "disposition": "machine_detected_related",
        "support_frontier": True,
        "selected_by_support_audit": False,
        "passed_to_draft": False,
        "discovery_quality_passed": True,
        "support_audits": [],
        "provider": "crossref",
        "provider_record_id": "seed",
        "source_type": "book",
        "record_type": "book",
        "title": "共通著作",
        "authors": ["研究者"],
        "source_url": "https://example.invalid/seed",
        "confidence": 0.93,
        "provenance": {},
    }


def _manifestation(
    provider: str,
    record_id: str,
    *,
    isbn: str,
    edition: str,
    volume: str,
    is_translation: bool,
    translators: tuple[str, ...] = (),
) -> LiteratureCandidate:
    return LiteratureCandidate(
        provider=provider,
        provider_record_id=record_id,
        title="共通著作",
        authors=("研究者",),
        publication_title=None,
        publisher="研究出版社",
        publication_date="2024",
        record_type="book",
        doi=None,
        url=None,
        oa_url=None,
        oa_status=None,
        license_url=None,
        provider_payload={
            "isbn": isbn,
            "edition": edition,
            "volume": volume,
            "is_translation": is_translation,
            "translators": list(translators),
            "original_title": "Common Work",
            "translated_title": "共通著作" if is_translation else None,
        },
    )


class _BibliographyProvider:
    def __init__(self, name: str, candidate: LiteratureCandidate, calls: list[str]):
        self.name = name
        self._candidate = candidate
        self._calls = calls

    def search_bibliographic(self, title: str, author: str, *, limit: int):
        del title, author, limit
        self._calls.append(self.name)
        return [self._candidate]

    def search(self, query: str, *, limit: int):
        del query, limit
        self._calls.append(self.name)
        return [self._candidate]


class _ReferencePlanner:
    def plan_reference_queries(self, entries):
        return {
            "queries": [
                {
                    "reference_id": entry["reference_id"],
                    "titles": [entry["title"]],
                    "authors": list(entry.get("authors") or []),
                }
                for entry in entries
            ]
        }

    def infer_reference_entries(self, *, entries, rule_set):
        del entries, rule_set
        return {"inferences": []}


def test_v22_machine_related_work_searches_every_provider_and_retains_manifestations(
    monkeypatch,
) -> None:
    """Related discoveries get a full manifestation search without becoming consulted."""

    case = _worker_case()
    try:
        claimed = _claim_v22(case)
        case.worker.settings = replace(
            case.settings,
            translation=replace(
                case.settings.translation,
                academic_research_online=True,
            ),
        )
        case.worker.translator = _ReferencePlanner()
        calls: list[str] = []
        candidates = {
            "ndl_search": _manifestation(
                "ndl_search",
                "original",
                isbn="9780000000001",
                edition="初版",
                volume="全巻",
                is_translation=False,
            ),
            "openalex": _manifestation(
                "openalex",
                "translation-upper",
                isbn="9780000000002",
                edition="初版",
                volume="上巻",
                is_translation=True,
                translators=("訳者甲",),
            ),
            "crossref": _manifestation(
                "crossref",
                "translation-lower",
                isbn="9780000000003",
                edition="初版",
                volume="下巻",
                is_translation=True,
                # A translated manifestation without a translator must survive
                # discovery and block final approval instead of being dropped.
                translators=(),
            ),
            "semantic_scholar": _manifestation(
                "semantic_scholar",
                "translation-revised",
                isbn="9780000000004",
                edition="改訳版",
                volume="全巻",
                is_translation=True,
                translators=("訳者乙",),
            ),
        }

        def provider_factory(name: str):
            return lambda *, timeout_seconds: _BibliographyProvider(
                name, candidates[name], calls
            )

        monkeypatch.setattr(
            runner_module, "NDLSearchProvider", provider_factory("ndl_search")
        )
        monkeypatch.setattr(
            runner_module, "OpenAlexProvider", provider_factory("openalex")
        )
        monkeypatch.setattr(
            runner_module, "CrossrefProvider", provider_factory("crossref")
        )
        monkeypatch.setattr(
            runner_module,
            "SemanticScholarProvider",
            provider_factory("semantic_scholar"),
        )

        result = case.worker._manuscript_resolve_reference_metadata(
            claimed,
            {
                "workflow_version": 22,
                "proposed_html": HTML,
                "reference_entries": [],
                "reference_usage_records": [_machine_usage()],
                "use_online_research": True,
                "reference_rule_set": {"id": "test-v22"},
                "manuscript_workflow_policy": {
                    "bibliography": {
                        "unmentioned_original_or_translation": (
                            "render_all_eligible_manifestations"
                        ),
                        "unspecified_volume": "render_complete_set",
                        "unspecified_edition": "render_all_eligible_editions",
                    }
                },
            },
        ).context

        assert set(calls) == {
            "ndl_search",
            "openalex",
            "crossref",
            "semantic_scholar",
        }
        assert result["reference_entries"] == []
        machine = result["machine_reference_entries"]
        assert len(machine) == 4
        assert len({item["machine_reference_id"] for item in machine}) == 4
        assert {item["isbn"] for item in machine} == {
            "9780000000001",
            "9780000000002",
            "9780000000003",
            "9780000000004",
        }
        assert all(item["consulted_status"] == "not_consulted" for item in machine)
        assert all(
            item["reference_role"]
            == "machine_detected_not_confirmed_consulted"
            for item in machine
        )

        sections = JapaneseReferenceService.bibliography_sections_v22(
            [], machine_entries=machine
        )
        assert [section["title"] for section in sections] == [
            "参考文献",
            "機械判別参考文献",
        ]
        assert len(sections[1]["entries"]) == 4
        missing_translator = next(
            item for item in machine if item["isbn"] == "9780000000003"
        )
        assert missing_translator["translator_status"] == "required_missing"
        assert "translators" in missing_translator["required_missing_fields"]
        assert missing_translator["final_revision_blocked"] is True

        completion = case.worker._manuscript_completion_gate(result)
        assert any(
            blocker["code"] == "reference.final_revision_confirmation_required"
            for blocker in completion["blocking_reasons"]
        )
    finally:
        case.tearDown()


class _FirstEngine:
    def __init__(self):
        self.calls: list[str] = []

    def __getattr__(self, name: str):
        def invoke(*args, **kwargs):
            del args, kwargs
            self.calls.append(name)
            return {"result": "ok"}

        return invoke


class _AlternateEngine(_FirstEngine):
    pass


class _RecoverableEngine:
    def __init__(self):
        self.review_manuscript_blocks = None
        self.executable_sha256 = None
        self.executable_state = "unavailable"
        self.calls: list[str] = []

    def execution_identity_v22(self):
        return {
            "identity_contract": "acceptance-recoverable-v1",
            "configured_command": "codex",
            "executable_sha256": self.executable_sha256,
            "executable_state": self.executable_state,
            "config_policy": "ignore_user_config",
            "model_selection": "explicit_requested_model",
            "requested_model": "gpt-test",
            "explicit_model": "gpt-test",
            "explicit_model_provider": None,
        }

    def recover(self):
        self.executable_sha256 = "a" * 64
        self.executable_state = "identified"

        def invoke(*args, **kwargs):
            del args, kwargs
            self.calls.append("review_manuscript_blocks")
            return {"result": "ok"}

        self.review_manuscript_blocks = invoke


def _queued_claim(case, run_id: str) -> ClaimedNode:
    with case.databases.state.connect() as connection:
        row = connection.execute(
            "SELECT * FROM node_runs WHERE run_id=? ORDER BY rowid LIMIT 1",
            (run_id,),
        ).fetchone()
        inputs = json.loads(str(row["input_json"]))
    return ClaimedNode(
        node_run_id=str(row["id"]),
        run_id=run_id,
        node_id=str(row["node_id"]),
        attempt=int(row["attempt"]),
        workflow_name="manuscript_rewrite",
        workflow_version="22",
        resource_lane=str(row["resource_lane"]),
        lease_owner="acceptance-seed",
        project_id=None,
        inputs=inputs,
    )


def test_v22_unavailable_backend_binds_desired_identity_then_same_identity_recovers() -> None:
    """Unavailable runtime state is separate from the durable desired identity."""

    case = _worker_case()
    try:
        run_id = case._create_run("manuscript_rewrite")
        workflow = case.worker._workflow_for("manuscript_rewrite", "22")
        entry_node = next(node for node in workflow.nodes if node.id == workflow.entry_node)
        original_handler = case.worker.handlers[entry_node.handler]

        def inference_handler(claimed: ClaimedNode, context: dict) -> HandlerResult:
            case.worker._invoke_manuscript_inference(
                claimed, "review_manuscript_blocks"
            )
            return HandlerResult(context)

        case.worker.handlers[entry_node.handler] = inference_handler
        recovered = _RecoverableEngine()
        case.worker.translator = recovered
        try:
            assert case.worker.run_once(lane=entry_node.resource_lane) is True
            with case.databases.state.connect() as connection:
                waiting = RunRepository(connection).snapshot(run_id)
                engine_records = connection.execute(
                    "SELECT payload_json FROM manuscript_unit_checkpoints "
                    "WHERE run_id=? AND phase='manuscript-v22-engine'",
                    (run_id,),
                ).fetchall()
            assert waiting["run"]["status"] == "queued"
            assert waiting["active_nodes"][0]["status"] == "queued"
            assert waiting["run"]["error"]["type"] == "ExternalServiceUnavailable"
            assert len(engine_records) == 1
            desired_binding = json.loads(engine_records[0]["payload_json"])
            assert desired_binding["binding_kind"] == "desired_only"
            assert desired_binding["runtime_engine_fingerprint"] is None
            assert desired_binding["engine"]["runtime_executable_sha256"] is None

            # Make the scheduled retry immediately claimable in this isolated
            # fixture. The same desired adapter/configuration may bind its newly
            # identified runtime without authorizing a quality-policy change.
            with case.databases.state.connect() as connection:
                row = connection.execute(
                    "SELECT id,input_json FROM node_runs WHERE run_id=? AND status='queued'",
                    (run_id,),
                ).fetchone()
                inputs = json.loads(str(row["input_json"]))
                inputs.pop("_external_retry", None)
                connection.execute(
                    "UPDATE node_runs SET input_json=? WHERE id=?",
                    (json.dumps(inputs, ensure_ascii=False), str(row["id"])),
                )
                connection.commit()

            recovered.recover()
            assert case.worker.run_once(lane=entry_node.resource_lane) is True
            with case.databases.state.connect() as connection:
                records = [
                    json.loads(row["payload_json"])
                    for row in connection.execute(
                        "SELECT payload_json FROM manuscript_unit_checkpoints "
                        "WHERE run_id=? AND phase='manuscript-v22-engine' ORDER BY rowid",
                        (run_id,),
                    )
                ]
                quality_changes = connection.execute(
                    "SELECT COUNT(*) FROM event_log WHERE run_id=? AND "
                    "event_type='manuscript.inference_engine.quality_policy_changed'",
                    (run_id,),
                ).fetchone()[0]
            assert recovered.calls == ["review_manuscript_blocks"]
            assert len(records) == 2
            assert records[0]["binding_kind"] == "desired_only"
            assert records[1]["binding_kind"] == "desired_and_runtime"
            assert records[1]["from_fingerprint"] == records[0]["to_fingerprint"]
            assert records[1]["quality_policy_change"] is False
            assert records[1]["change_authorized"] is False
            assert quality_changes == 0
        finally:
            case.worker.handlers[entry_node.handler] = original_handler
    finally:
        case.tearDown()


def test_v22_engine_drift_waits_immediately_then_exact_sha_resume_records_policy_change() -> None:
    """A drift is an authorization wait, never a content retry or automatic switch."""

    case = _worker_case()
    try:
        run_id = case._create_run("manuscript_rewrite")
        queued = _queued_claim(case, run_id)
        first = _FirstEngine()
        case.worker.translator = first
        case.worker._invoke_manuscript_inference(
            queued, "review_manuscript_blocks"
        )

        alternate = _AlternateEngine()
        case.worker.translator = alternate
        target = case.worker._language_engine_descriptor_v22()["fingerprint"]
        workflow = case.worker._workflow_for("manuscript_rewrite", "22")
        entry_node = next(node for node in workflow.nodes if node.id == workflow.entry_node)
        original_handler = case.worker.handlers[entry_node.handler]

        def drift_handler(claimed: ClaimedNode, context: dict) -> HandlerResult:
            case.worker._invoke_manuscript_inference(
                claimed, "review_manuscript_blocks"
            )
            return HandlerResult(context)

        case.worker.handlers[entry_node.handler] = drift_handler
        try:
            assert case.worker.run_once(lane=entry_node.resource_lane) is True
            with case.databases.state.connect() as connection:
                repository = RunRepository(connection)
                waiting = repository.snapshot(run_id)
                attempts = connection.execute(
                    "SELECT attempt,status FROM node_runs "
                    "WHERE run_id=? AND node_id=? ORDER BY attempt",
                    (run_id, workflow.entry_node),
                ).fetchall()
                retry_events = connection.execute(
                    "SELECT event_type FROM event_log WHERE run_id=? "
                    "AND event_type IN "
                    "('node.retry_queued','worker.codex.retry_scheduled')",
                    (run_id,),
                ).fetchall()
            assert waiting["run"]["status"] == "waiting_human"
            assert waiting["active_nodes"][0]["status"] == "waiting_human"
            assert [(row["attempt"], row["status"]) for row in attempts] == [
                (1, "waiting_human")
            ]
            assert retry_events == []
            assert alternate.calls == []

            # A syntactically valid but different SHA is not permission to use
            # the currently installed engine.  Reject it before any durable
            # Run, node, attempt, or event state can change.
            with case.databases.state.connect() as connection:
                before = connection.execute(
                    "SELECT r.status AS run_status,r.error_json AS run_error,"
                    "n.status AS node_status,n.input_json,n.error_json AS node_error,"
                    "n.attempt FROM runs r JOIN node_runs n ON n.run_id=r.id "
                    "WHERE r.id=? AND n.status='waiting_human'",
                    (run_id,),
                ).fetchone()
                event_count_before = connection.execute(
                    "SELECT COUNT(*) FROM event_log WHERE run_id=?", (run_id,)
                ).fetchone()[0]
                with pytest.raises(FukamiruError, match="fingerprint|engine"):
                    RunRepository(connection).resume_external_wait(
                        run_id, authorized_engine_fingerprint="0" * 64
                    )
                after = connection.execute(
                    "SELECT r.status AS run_status,r.error_json AS run_error,"
                    "n.status AS node_status,n.input_json,n.error_json AS node_error,"
                    "n.attempt FROM runs r JOIN node_runs n ON n.run_id=r.id "
                    "WHERE r.id=? AND n.status='waiting_human'",
                    (run_id,),
                ).fetchone()
                event_count_after = connection.execute(
                    "SELECT COUNT(*) FROM event_log WHERE run_id=?", (run_id,)
                ).fetchone()[0]
            assert dict(after) == dict(before)
            assert event_count_after == event_count_before
            assert alternate.calls == []

            with case.databases.state.connect() as connection:
                resumed = RunRepository(connection).resume_external_wait(
                    run_id, authorized_engine_fingerprint=target
                )
            assert resumed["status"] == "queued"
            assert resumed["authorized_engine_fingerprint"] == target
            assert case.worker.run_once(lane=entry_node.resource_lane) is True

            with case.databases.state.connect() as connection:
                quality_events = [
                    json.loads(row["payload_json"])
                    for row in connection.execute(
                        "SELECT payload_json FROM event_log WHERE run_id=? "
                        "AND event_type="
                        "'manuscript.inference_engine.quality_policy_changed'",
                        (run_id,),
                    )
                ]
                node_attempts = connection.execute(
                    "SELECT COUNT(*) FROM node_runs WHERE run_id=? AND node_id=?",
                    (run_id, workflow.entry_node),
                ).fetchone()[0]
            assert alternate.calls == ["review_manuscript_blocks"]
            assert node_attempts == 1
            assert len(quality_events) == 1
            assert quality_events[0]["to_fingerprint"] == target
            assert quality_events[0]["quality_policy_change"] is True
            assert quality_events[0]["change_authorized"] is True
            assert quality_events[0]["authorization_field"] == "engine_authorization_id"
            with case.databases.state.connect() as connection:
                authorization = connection.execute(
                    "SELECT status,from_fingerprint,target_fingerprint,consumed_at "
                    "FROM manuscript_engine_authorizations WHERE id=?",
                    (resumed["engine_authorization_id"],),
                ).fetchone()
                stored_inputs = json.loads(
                    connection.execute(
                        "SELECT input_json FROM node_runs WHERE run_id=? ORDER BY rowid LIMIT 1",
                        (run_id,),
                    ).fetchone()[0]
                )
            assert authorization["status"] == "consumed"
            assert authorization["target_fingerprint"] == target
            assert authorization["consumed_at"]
            assert "_engine_authorization_id" not in stored_inputs
        finally:
            case.worker.handlers[entry_node.handler] = original_handler
    finally:
        case.tearDown()


def test_v21_engine_change_remains_unbound_and_does_not_require_authorization() -> None:
    case = _worker_case()
    try:
        claimed = replace(_claim_v22(case), workflow_version="21")
        case.worker.translator = _FirstEngine()
        case.worker._invoke_manuscript_inference(claimed, "audit_manuscript_records")
        alternate = _AlternateEngine()
        case.worker.translator = alternate
        case.worker._invoke_manuscript_inference(claimed, "plan_reference_queries", [])

        assert alternate.calls == ["plan_reference_queries"]
        with case.databases.state.connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM manuscript_unit_checkpoints "
                "WHERE run_id=? AND phase='manuscript-v22-engine'",
                (claimed.run_id,),
            ).fetchone()[0]
        assert count == 0
    finally:
        case.tearDown()
