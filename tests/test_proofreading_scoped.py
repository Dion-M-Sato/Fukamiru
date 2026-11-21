from types import SimpleNamespace

import pytest

from fukamiru.authoring.codex import _validate_codex_output_schema, _validate_schema
from fukamiru.errors import NonRetryableFukamiruError
from fukamiru.revisions import proofreading_adoption as rules
from fukamiru.worker import proofreading_scoped as scoped
from fukamiru.worker.runner import WorkerRunner
from fukamiru.workflow.templates import authoring_workflow


def fixture(monkeypatch, before, after, *, empty=False, unresolved=False, outside=False):
    html = (
        '<article id="a" lang="ja"><section id="s"><h2 id="h">節</h2>'
        '<p id="p">甲<cite id="c" data-reference-id="r">『書名』</cite>を踏まえ、'
        + before
        + '。</p><p id="natural">象徴資本の再生産を論じる。</p>'
        '<p id="human">人間の文章。</p></section></article>'
    )
    calls = []

    def model(worker, claimed, context, stage, prompt, payload, schema):
        _validate_codex_output_schema(schema)
        calls.append((stage, payload))
        if stage == "llmjp_located_diagnosis":
            assert list(payload) == ["paragraphs"]
            assert len(payload["paragraphs"]) == 2
            assert all(set(p) == {"paragraph_id", "text"} for p in payload["paragraphs"])
            result = {
                "findings": []
                if empty
                else [
                    {"paragraph_id": "p", "quote": before, "reason": "この語句の結び付きが不自然。"}
                ]
            }
        elif stage == "llmjp_located_suggestions":
            assert [p["paragraph_id"] for p in payload["paragraphs"]] == ["p"]
            assert "象徴資本" not in str(payload)
            result = {
                "repairs": [
                    {
                        "finding_id": "J0001",
                        "before": before,
                        "after": after,
                        "reason": "指摘箇所だけを自然にする。",
                    }
                ]
            }
            if outside:
                result["repairs"][0].update(
                    before="を踏まえ、" + before, after="を誤読して、" + after
                )
        else:
            assert stage == "llmjp_located_verification"
            assert len(payload["original_paragraphs"]) == 1
            assert len(payload["candidate_paragraphs"]) == 1
            result = {
                "judgments": {
                    "J0001": {
                        "verdict": "要確認" if unresolved else "解消",
                        "reason": "元の指摘だけを確認。",
                    }
                }
            }
        _validate_schema(result, schema)
        return result

    monkeypatch.setattr(scoped.adoption, "_call", model)
    return {"proposed_html": html, "human_protected_block_ids": ["human"]}, calls


@pytest.mark.parametrize(
    "before,after",
    [
        ("責任を成し遂げる", "責任を果たす"),
        ("男性同間の関係を論じる", "男性同士の関係を論じる"),
        ("原典を参照するのはない", "原典を参照するものではない"),
    ],
)
def test_only_diagnosed_scope_changes_with_full_paragraph_context(monkeypatch, before, after):
    context, calls = fixture(monkeypatch, before, after)
    worker, claimed = SimpleNamespace(), SimpleNamespace()
    context = scoped.diagnose(worker, claimed, context)
    context = scoped.suggest(worker, claimed, context)
    context = scoped.verify(worker, claimed, context)
    state = context["japanese_proofreading_context"]
    candidate = rules.install(
        context["proposed_html"], state["plan"], state["reviews"][0]["paragraphs"]
    )
    assert after in candidate and before not in candidate
    assert '<p id="natural">象徴資本の再生産を論じる。</p>' in candidate
    assert '<p id="human">人間の文章。</p>' in candidate
    assert '<cite id="c" data-reference-id="r">『書名』</cite>' in candidate
    assert rules.validate_structure(context["proposed_html"], candidate, {"human"})
    assert len(calls) == 3
    assert context["proposed_html"] != candidate  # Only the application node adopts it.


def test_no_findings_means_no_suggestion_or_reverification_calls(monkeypatch):
    context, calls = fixture(monkeypatch, "自然な文を読む", "別の文", empty=True)
    for step in (scoped.diagnose, scoped.suggest, scoped.verify):
        context = step(None, None, context)
    state = context["japanese_proofreading_context"]
    assert (
        rules.install(context["proposed_html"], state["plan"], state["reviews"][0]["paragraphs"])
        == (context["proposed_html"])
    )
    assert len(calls) == 1


def test_outside_scope_changes_stop_without_a_retry_or_paragraph_rewrite(monkeypatch):
    context, calls = fixture(monkeypatch, "責任を成し遂げる", "責任を果たす", outside=True)
    original = context["proposed_html"]
    context = scoped.diagnose(None, None, context)
    with pytest.raises(NonRetryableFukamiruError, match="outside_diagnosed_scope"):
        scoped.suggest(None, None, context)
    assert len(calls) == 2 and context["proposed_html"] == original


def test_unresolved_verification_preserves_proposal_and_does_not_search_again(monkeypatch):
    context, calls = fixture(monkeypatch, "責任を成し遂げる", "責任を果たす", unresolved=True)
    original = context["proposed_html"]
    context = scoped.diagnose(None, None, context)
    context = scoped.suggest(None, None, context)
    proposed = context["japanese_located_corrections"]["candidate_rows"]
    with pytest.raises(NonRetryableFukamiruError, match="proposal_retained_for_human"):
        scoped.verify(None, None, context)
    assert len(calls) == 3 and context["proposed_html"] == original
    assert "責任を果たす" in str(proposed)


def test_workflow_exposes_only_scoped_roles_for_adoption_profile():
    workflow = authoring_workflow(proofreading_profile="llmjp33b_adopt")
    handlers = WorkerRunner._contextual_proofreading_handlers(WorkerRunner.__new__(WorkerRunner))
    for node_id, role in [
        ("diagnose_japanese_prose", "prepare_sections"),
        ("redraft_japanese_paragraphs", "correct_sections_once"),
        ("review_japanese_sections", "validate_sections"),
    ]:
        node = next(n for n in workflow.nodes if n.id == node_id)
        assert node.handler == "authoring.llmjp_adopt_" + role
        assert node.handler in handlers
    assert "authoring.llmjp_adopt_propose" not in handlers
    assert "authoring.llmjp_adopt_review" not in handlers
