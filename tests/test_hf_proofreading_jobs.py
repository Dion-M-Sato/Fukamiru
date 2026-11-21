from __future__ import annotations

import json
import time
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_proofreading_context import fixture_worker

from fukamiru.config import HFProofreadingSettings
from fukamiru.errors import NonRetryableFukamiruError, RunCancelled
from fukamiru.local_llm import hf_jobs
from fukamiru.local_llm.hf_api import SameOriginRedirect
from fukamiru.local_llm.hf_protocol import MODEL
from fukamiru.local_llm.hf_worker import Tasks
from fukamiru.worker import proofreading_context as transport
from fukamiru.worker import proofreading_section_once as nodes

HTML = ('<article id="a" lang="ja"><section id="s1"><h2 id="h1">第一節</h2>'
        '<p id="p1">担当者は責任を成し遂げる。</p></section>'
        '<section id="s2"><h2 id="h2">第二節</h2>'
        '<p id="p2">象徴資本の再生産を論じる。</p></section></article>')


class API:
    def __init__(self):
        self.jobs, self.tasks = {}, {}
        self.submissions, self.cancelled, self.model_calls = [], [], []
        self.price = 83333
        self.failure = None
        self.cancel_on_poll = False
        self.poll_cancelled = False
        self.finish_reason = "stop"
        self.stop_failure = False

    def hardware(self):
        return [{"name": "h200", "unitLabel": "minute", "unitCostMicroUSD": self.price}]

    def launch(self, namespace, spec):
        self.submissions.append(deepcopy(spec))
        ident = f"{len(self.jobs) + 1:024x}"
        self.jobs[ident] = {"id": ident, "labels": spec["labels"], "status": {"stage": "RUNNING"}}
        if self.failure == "lost_launch_response":
            raise OSError("connection lost")
        return self.jobs[ident]

    def inspect(self, namespace, ident):
        return deepcopy(self.jobs[ident])

    def cancel(self, namespace, ident):
        if self.stop_failure:
            raise OSError("temporary offline")
        self.cancelled.append(ident)
        self.jobs[ident]["status"]["stage"] = "CANCELED"

    def find(self, namespace, labels):
        return [r for r in self.jobs.values() if r["labels"] == labels]

    def request(self, url, body=None):
        if url.endswith("/health"):
            return {"status": "ok"}
        if url.endswith("/identity"):
            return json.loads(json.dumps(MODEL))
        if url.endswith("/props"):
            return {"model_path": MODEL["model_file"],
                    "default_generation_settings": {"n_ctx": 65536}}
        if url.endswith("/apply-template"):
            return {"prompt": "Reasoning: high\n" + json.dumps(body)}
        if url.endswith("/tokenize"):
            return {"tokens": [1] * 100}
        if url.endswith("/tasks"):
            key, request = body["request_id"], body["request"]
            assert request["reasoning_effort"] == "high"
            self.model_calls.append(deepcopy(request))
            payload = json.loads(request["messages"][1]["content"])
            section = payload["section"]
            corrections = [{"paragraph_id": p["paragraph_id"], "before": "責任を成し遂げる",
                            "after": "責任を果たす", "reason": "自然な連語に直す。"}
                           for p in section["paragraphs"] if "責任を成し遂げる" in p["text"]]
            self.tasks[key] = {"state": "completed", "response": {"choices": [{
                "message": {"content": json.dumps({
                    "corrections": corrections, "human_review": []})},
                "finish_reason": self.finish_reason}], "usage": {"completion_tokens": 20}}}
            return {"task_id": key}
        if "/tasks/" in url:
            if self.cancel_on_poll:
                self.poll_cancelled = True
                return {"state": "running"}
            return self.tasks[url.rsplit("/", 1)[-1]]
        raise AssertionError(url)


def fixture(tmp_path, monkeypatch):
    worker, claimed, context, _ = fixture_worker(tmp_path)
    api = API()
    monkeypatch.setattr(hf_jobs, "token_available", lambda: "never-serialized-test-token")
    settings = SimpleNamespace(
        hf_proofreading=HFProofreadingSettings(enabled=True, namespace="tester"),
        paths=SimpleNamespace(state_db=worker.databases.state.path, runtime_root=tmp_path),
    )
    backend = hf_jobs.HFProofreadingBackend(settings, worker.databases, api=api)
    worker.llm_jp_proofreading = backend

    def cancelled(*args):
        if api.poll_cancelled:
            raise RunCancelled("cancelled")

    worker._raise_if_run_cancelled = cancelled
    context["proposed_html"] = HTML
    context = nodes.prepare_sections(worker, claimed, context)
    context["section_once_automatic_content_review"] = False
    return worker, claimed, context, backend, api, settings


def test_one_job_two_sections_then_stop_and_replay_without_gpu(tmp_path, monkeypatch):
    worker, claimed, context, backend, api, settings = fixture(tmp_path, monkeypatch)
    output = nodes.correct_sections_once(worker, claimed, context)
    output = nodes.validate_sections(worker, claimed, output)
    assert len(api.submissions) == 1 and len(api.model_calls) == 2 and len(api.cancelled) == 1
    spec = api.submissions[0]
    assert spec["timeoutSeconds"] == 59 * 60
    assert spec["expose"] == {"ports": [8080]} and "secrets" not in spec
    assert "never-serialized-test-token" not in str(spec)
    assert output["japanese_section_once"]["additional_llmjp_requests"] == 0
    assert len(output["japanese_section_once"]["applied_repairs"]) == 1
    with worker.databases.state.connect() as connection:
        assert connection.execute("SELECT status FROM hf_proofreading_jobs").fetchone()[0] == (
            "stopped")
        assert [r[0] for r in connection.execute(
            "SELECT status FROM hf_proofreading_requests")] == ["completed", "completed"]
    # A new service instance and ephemeral URL do not invalidate the saved exact requests.
    empty_api = API()
    worker.llm_jp_proofreading = hf_jobs.HFProofreadingBackend(
        settings, worker.databases, api=empty_api)
    replay = nodes.correct_sections_once(worker, claimed, context)
    assert replay["japanese_section_once"]["responses"] == (
        output["japanese_section_once"]["responses"])
    assert not empty_api.submissions and not empty_api.model_calls
    assert not backend.active


def test_invalid_schema_stops_before_any_gpu_allocation(tmp_path, monkeypatch):
    worker, claimed, context, backend, api, _ = fixture(tmp_path, monkeypatch)
    with (backend.proofreading_session(worker, claimed, context) as scoped,
          pytest.raises(NonRetryableFukamiruError, match="required")):
        transport._call(scoped, claimed, "bad", "", {}, {
            "type": "object", "properties": {"x": {"type": "string"}},
            "required": [], "additionalProperties": False}, context)
    assert not api.submissions


@pytest.mark.parametrize("mode", ["cancel", "truncated"])
def test_uncertain_or_unfinished_inference_is_saved_and_never_resubmitted(
    tmp_path, monkeypatch, mode,
):
    worker, claimed, context, backend, api, settings = fixture(tmp_path, monkeypatch)
    api.cancel_on_poll = mode == "cancel"
    api.finish_reason = "length" if mode == "truncated" else "stop"
    with pytest.raises((RunCancelled, NonRetryableFukamiruError)):
        nodes.correct_sections_once(worker, claimed, context)
    assert len(api.model_calls) == 1 and len(api.cancelled) == 1
    api.poll_cancelled = False
    empty_api = API()
    worker.llm_jp_proofreading = hf_jobs.HFProofreadingBackend(
        settings, worker.databases, api=empty_api)
    with pytest.raises(NonRetryableFukamiruError, match="unresolved|unfinished"):
        nodes.correct_sections_once(worker, claimed, context)
    assert not empty_api.submissions and not empty_api.model_calls


def test_lost_launch_reply_is_reconciled_by_labels_and_cancelled(tmp_path, monkeypatch):
    worker, claimed, context, _backend, api, _ = fixture(tmp_path, monkeypatch)
    api.failure = "lost_launch_response"
    with pytest.raises(OSError, match="connection lost"):
        nodes.correct_sections_once(worker, claimed, context)
    assert len(api.submissions) == 1 and len(api.cancelled) == 1 and not api.model_calls


def test_failed_stop_remains_visible_until_recovery_confirms_cancellation(tmp_path, monkeypatch):
    worker, claimed, context, backend, api, _ = fixture(tmp_path, monkeypatch)
    api.stop_failure = True
    with pytest.raises(OSError):
        nodes.correct_sections_once(worker, claimed, context)
    assert backend.status()["status"] == "error"
    with worker.databases.state.connect() as connection:
        assert connection.execute("SELECT status FROM hf_proofreading_jobs").fetchone()[0] == (
            "stop_failed")
    assert len(api.model_calls) == 2
    api.stop_failure = False
    backend.recover()
    assert len(api.cancelled) == 1
    assert backend.status()["last_error"] is None
    with worker.databases.state.connect() as connection:
        assert connection.execute("SELECT status FROM hf_proofreading_jobs").fetchone()[0] == (
            "stopped")


def test_crashed_owner_is_recovered_without_cancelling_unrelated_jobs(tmp_path, monkeypatch):
    worker, claimed, context, backend, api, settings = fixture(tmp_path, monkeypatch)
    with backend.proofreading_session(worker, claimed, context) as scoped:
        scoped.llm_jp_proofreading.ensure_started()
        api.jobs["f" * 24] = {"id": "f" * 24, "labels": {"somebody": "else"},
                               "status": {"stage": "RUNNING"}}
        with worker.databases.state.connect() as connection:
            connection.execute("UPDATE hf_proofreading_jobs SET heartbeat_at=?",
                               (time.time() - 100,))
        other = hf_jobs.HFProofreadingBackend(settings, worker.databases, api=api)
        other.recover()
        assert api.cancelled == ["0" * 23 + "1"]
        assert api.jobs["f" * 24]["status"]["stage"] == "RUNNING"


def test_price_increase_is_rejected_before_launch(tmp_path, monkeypatch):
    worker, claimed, context, _backend, api, _ = fixture(tmp_path, monkeypatch)
    api.price *= 2
    with pytest.raises(NonRetryableFukamiruError, match="price_exceeds_limit"):
        nodes.correct_sections_once(worker, claimed, context)
    assert not api.submissions and not api.model_calls


def test_remote_gateway_accepts_an_identical_task_only_once_and_rejects_changed_body():
    tasks = Tasks(None)
    calls = []
    tasks.pool.shutdown()
    tasks.pool = SimpleNamespace(submit=lambda *args: calls.append(args))
    payload = {"reasoning_effort": "high", "messages": []}
    key = "a" * 64
    tasks.submit(key, payload)
    tasks.submit(key, payload)
    assert len(calls) == 1
    with pytest.raises(ValueError, match="identity_conflict"):
        tasks.submit(key, {**payload, "temperature": 1})
    with pytest.raises(ValueError, match="invalid_request_contract"):
        tasks.submit("b" * 64, {"reasoning_effort": "low"})


def test_cross_origin_redirect_cannot_forward_credentials():
    from urllib.request import Request

    with pytest.raises(NonRetryableFukamiruError, match="cross_origin"):
        SameOriginRedirect().redirect_request(Request("https://huggingface.co/api/jobs"),
            None, 302, "", {}, "https://example.org/")


def test_disabling_japanese_proofreading_never_allocates_a_job(tmp_path, monkeypatch):
    worker, claimed, context, _backend, api, _ = fixture(tmp_path, monkeypatch)
    context["use_llm_jp"] = False
    context = nodes.prepare_sections(worker, claimed, context)
    result = nodes.correct_sections_once(worker, claimed, context)
    assert not result["japanese_section_once"]["requests"] and not api.submissions


def test_received_raw_answer_survives_crash_before_parsed_cache_write(tmp_path, monkeypatch):
    worker, claimed, context, _backend, api, settings = fixture(tmp_path, monkeypatch)
    context["proofreading_target_paragraph_ids"] = ["p1"]
    context = nodes.prepare_sections(worker, claimed, context)
    original = hf_jobs.ManagedAdapter.remember

    def crash(*args):
        raise OSError("simulated process loss before parsed cache write")

    monkeypatch.setattr(hf_jobs.ManagedAdapter, "remember", crash)
    with pytest.raises(OSError, match="simulated process loss"):
        nodes.correct_sections_once(worker, claimed, context)
    with worker.databases.state.connect() as connection:
        row = connection.execute("SELECT * FROM hf_proofreading_requests").fetchone()
        assert row["status"] == "received" and row["raw_response_json"] and row["request_json"]
    monkeypatch.setattr(hf_jobs.ManagedAdapter, "remember", original)
    empty_api = API()
    worker.llm_jp_proofreading = hf_jobs.HFProofreadingBackend(
        settings, worker.databases, api=empty_api)
    replay = nodes.correct_sections_once(worker, claimed, context)
    assert len(replay["japanese_section_once"]["responses"]) == 1
    assert len(api.model_calls) == len(api.cancelled) == 1
    assert not empty_api.submissions and not empty_api.model_calls


def test_live_owner_prevents_a_second_allocation_without_cancelling_its_job(tmp_path, monkeypatch):
    worker, claimed, context, backend, api, settings = fixture(tmp_path, monkeypatch)
    with backend.proofreading_session(worker, claimed, context) as scoped:
        scoped.llm_jp_proofreading.ensure_started()
        other = hf_jobs.HFProofreadingBackend(settings, worker.databases, api=api)
        other.recover()
        with (other.proofreading_session(worker, claimed, context) as second,
              pytest.raises(NonRetryableFukamiruError, match="cleanup_pending")):
            second.llm_jp_proofreading.ensure_started()
        assert len(api.submissions) == 1 and not api.cancelled
    assert len(api.cancelled) == 1
