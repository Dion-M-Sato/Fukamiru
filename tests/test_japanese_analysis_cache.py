from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from fukamiru.errors import ExternalServiceUnavailable, FukamiruError, RunCancelled
from fukamiru.revisions import japanese_analysis as analysis


def connection(path=":memory:"):
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE IF NOT EXISTS manuscript_language_analyses "
        "(signature TEXT PRIMARY KEY, payload_json TEXT NOT NULL)"
    )
    db.commit()
    return db


@pytest.fixture(autouse=True)
def local_parser(monkeypatch):
    monkeypatch.setattr(analysis, "analyzer_versions", lambda: {"parser": "fixture-v1"})
    monkeypatch.setattr(
        analysis,
        "parse_japanese",
        lambda _: {
            "status": "unresolved",
            "ambiguities": ["implicit_subject"],
            "ambiguity_spans": [],
            "tokens": [],
        },
    )


def resolved(label="A"):
    return {"status": "resolved", "explanation": label, "roles": [{"subject": label}]}


def incomplete():
    return {"status": "resolved", "explanation": "", "roles": []}


def cached(db, resolver, engine="engine-A"):
    return analysis.cached_analysis_v22(
        db, "本文", "前後の文脈", resolver, engine_fingerprint=engine
    )


def saved(db):
    row = db.execute("SELECT payload_json FROM manuscript_language_analyses").fetchone()
    return json.loads(row[0]) if row else None


@pytest.mark.parametrize("fingerprint", [None, "", "   ", True])
def test_v22_requires_nonempty_explicit_engine(fingerprint):
    with connection() as db:
        with pytest.raises(ValueError, match="fingerprint"):
            cached(db, lambda **_: resolved(), fingerprint)
        assert saved(db) is None


def test_v22_missing_engine_argument_is_rejected():
    with connection() as db, pytest.raises(TypeError, match="engine_fingerprint"):
        analysis.cached_analysis_v22(db, "本文", "文脈", lambda **_: resolved())


def test_v22_engine_change_cannot_reuse_previous_judgment():
    calls = []

    def resolver_a(**_):
        calls.append("A")
        return resolved("A")

    def resolver_b(**_):
        calls.append("B")
        return resolved("B")

    with connection() as db:
        first = cached(db, resolver_a)
        second = cached(db, resolver_b, "engine-B")
        assert cached(db, resolver_b, "engine-B") == second
        assert calls == ["A", "B"]
        assert first["signature"] != second["signature"]
        assert second["adjudication"]["explanation"] == "B"
        assert second["inference_engine_fingerprint"] == "engine-B"
        assert second["analysis_contract"] == analysis.V22_ANALYSIS_CONTRACT
        assert second["analysis_provenance"] == {
            "contract": analysis.V22_ANALYSIS_CONTRACT,
            "engine_fingerprint": "engine-B",
            "decision_source": "external_adjudicator",
        }


def test_v22_legacy_unbound_cache_is_not_reused():
    legacy_signature = hashlib.sha256(
        json.dumps(
            [
                "本文",
                "前後の文脈",
                analysis.analyzer_versions(),
                "ambiguity-contract-v2-transport-separated",
            ],
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
    ).hexdigest()
    calls = []

    def resolver(**_):
        calls.append(1)
        return resolved("current")

    with connection() as db:
        db.execute(
            "INSERT INTO manuscript_language_analyses VALUES (?,?)",
            (legacy_signature, json.dumps({"decision": "legacy"})),
        )
        db.commit()
        result = cached(db, resolver)
        assert calls == [1]
        assert result["signature"] != legacy_signature
        assert result["adjudication"]["explanation"] == "current"


def test_v22_contract_version_invalidates_previous_cache(monkeypatch):
    with connection() as db:
        previous = cached(db, lambda **_: resolved("previous"))
        monkeypatch.setattr(analysis, "V22_ANALYSIS_CONTRACT", "fixture-contract-next")
        current = cached(db, lambda **_: resolved("current"))
        assert current["signature"] != previous["signature"]
        assert current["adjudication"]["explanation"] == "current"


def test_v22_local_analysis_records_provenance_without_calling_external(monkeypatch):
    monkeypatch.setattr(
        analysis, "parse_japanese", lambda _: {"status": "analyzed", "ambiguities": []}
    )

    def unexpected(**_):
        pytest.fail("Local analysis must not call an unnecessary adjudicator")

    with connection() as db:
        result = cached(db, unexpected)
        assert result["decision"] == "analysis_ready"
        assert result["analysis_provenance"]["decision_source"] == "local_analyzers"
        assert result["response_contract_state"] == "complete"


@pytest.mark.parametrize(
    "error", [ExternalServiceUnavailable("offline"), RunCancelled("cancelled")]
)
def test_v22_external_failure_or_cancel_is_never_a_negative_cache(error):
    def unavailable(**_):
        raise error

    with connection() as db:
        with pytest.raises(type(error)):
            cached(db, unavailable)
        assert saved(db) is None


def test_v22_execution_error_is_not_a_semantic_decision():
    def failed(**_):
        raise ValueError("bad transport wrapper")

    with connection() as db:
        with pytest.raises(FukamiruError, match="before a content decision"):
            cached(db, failed)
        assert saved(db) is None


def test_v22_supplement_transport_resume_preserves_one_content_repair():
    actions = [incomplete(), ExternalServiceUnavailable("offline")]
    calls = []

    def resolver(**_):
        calls.append(1)
        value = actions.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    with connection() as db:
        with pytest.raises(ExternalServiceUnavailable):
            cached(db, resolver)
        pending = saved(db)
        assert pending["response_contract_state"] == "supplement_pending"
        assert pending["adjudication_attempt_count"] == 1
        assert "adjudication_error" not in pending

        actions.append(ExternalServiceUnavailable("still offline"))
        with pytest.raises(ExternalServiceUnavailable):
            cached(db, resolver)
        assert saved(db) == pending

        actions.append(incomplete())
        result = cached(db, resolver)
        assert result["decision"] == "response_contract_incomplete"
        assert result["adjudication_attempt_count"] == 2
        assert len(calls) == 4  # Two received contents, two transport failures.
        assert cached(db, resolver) == result
        assert len(calls) == 4


def test_v22_valid_unresolved_is_terminal_content_not_execution_failure():
    calls = []

    def unresolved(**_):
        calls.append(1)
        return {"status": "unresolved", "explanation": "主語を確定できない。", "roles": []}

    with connection() as db:
        result = cached(db, unresolved)
        assert result["decision"] == "semantic_analysis_unresolved"
        assert result["response_contract_state"] == "complete"
        assert cached(db, unresolved) == result
        assert calls == [1]


def test_v22_late_incomplete_cannot_overwrite_complete_or_request_supplement(tmp_path):
    db_path = tmp_path / "analysis.sqlite"
    connection(db_path).close()
    entered = Event()
    release = Event()
    late_calls = []

    def late_resolver(**_):
        late_calls.append(1)
        entered.set()
        assert release.wait(5), "Test did not release the late response"
        return incomplete()

    def delayed():
        with connection(db_path) as db:
            return cached(db, late_resolver)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(delayed)
        try:
            assert entered.wait(5), "Late request did not start"
            with connection(db_path) as db:
                complete = cached(db, lambda **_: resolved("canonical"))
        finally:
            release.set()
        assert future.result(timeout=5) == complete
    assert late_calls == [1]
    with connection(db_path) as db:
        assert saved(db) == complete


def test_v22_late_valid_response_can_improve_conditional_incomplete(tmp_path):
    db_path = tmp_path / "analysis.sqlite"
    connection(db_path).close()
    entered = Event()
    release = Event()

    def late_resolver(**_):
        entered.set()
        assert release.wait(5), "Test did not release the late response"
        return resolved("valid late result")

    def delayed():
        with connection(db_path) as db:
            return cached(db, late_resolver)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(delayed)
        try:
            assert entered.wait(5), "Late request did not start"
            with connection(db_path) as db:
                conditional = cached(db, lambda **_: incomplete())
                assert conditional["decision"] == "response_contract_incomplete"
        finally:
            release.set()
        complete = future.result(timeout=5)
    assert complete["decision"] == "analysis_ready"
    with connection(db_path) as db:
        assert saved(db) == complete


def test_v21_cache_contract_is_unchanged():
    with connection() as db:
        result = analysis.cached_analysis(db, "本文", "文脈", lambda **_: resolved())
        expected = hashlib.sha256(
            json.dumps(
                ["本文", "文脈", analysis.analyzer_versions(), "ambiguity-contract-v1"],
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
        ).hexdigest()
        assert result["signature"] == expected
        assert "inference_engine_fingerprint" not in result
        assert "analysis_contract" not in result
