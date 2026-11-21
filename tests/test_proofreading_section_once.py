from types import SimpleNamespace

import pytest

from fukamiru.authoring.codex import _validate_codex_output_schema, _validate_schema
from fukamiru.errors import NonRetryableFukamiruError
from fukamiru.revisions import proofreading_adoption as rules
from fukamiru.worker import proofreading_section_once as nodes

HTML = (
    '<article id="a" lang="ja"><section id="s1"><h2 id="h1">第一節</h2>'
    '<p id="p1">甲<cite id="c" data-reference-id="r">『書名』</cite>を読み、責任を成し遂げる。</p>'
    '<p id="p2">組織学習の再生産を論じる。</p></section>'
    '<section id="s2"><h2 id="h2">第二節</h2><p id="p3">参加者同間の関係を論じる。</p>'
    '<p id="human">人間の文章。</p></section></article>'
)


def setup(monkeypatch, *, bad_title=False, no_change=False):
    calls = []

    def model(worker, claimed, context, stage, prompt, payload, schema):
        _validate_codex_output_schema(schema)
        assert "document_context" not in payload and "html_template" not in str(payload)
        section = payload["section"]
        calls.append(section["section_id"])
        if section["section_id"] == "s1":
            assert [p["paragraph_id"] for p in section["paragraphs"]] == ["p1", "p2"]
            assert section["heading"] == "第一節"
            assert [p["paragraph_id"] for p in payload["adjacent_context"]] == ["p3"]
            before, after = (
                ("『書名』", "『別の書』") if bad_title else ("責任を成し遂げる", "責任を果たす")
            )
            pid = "p1"
        else:
            assert [p["paragraph_id"] for p in section["paragraphs"]] == ["p3"]
            assert [p["paragraph_id"] for p in payload["adjacent_context"]] == ["p2"]
            before, after, pid = "参加者同間", "参加者同士", "p3"
        result = {
            "corrections": [
                {
                    "paragraph_id": pid,
                    "before": before,
                    "after": before if no_change else after,
                    "reason": "不自然な語の結び付きを整える。",
                }
            ],
            "human_review": [],
        }
        _validate_schema(result, schema)
        return result

    monkeypatch.setattr(nodes.adoption, "_call", model)
    context = nodes.prepare_sections(
        None, None, {"proposed_html": HTML, "human_protected_block_ids": ["human"]}
    )
    context["section_once_automatic_content_review"] = False
    return context, calls


def test_one_request_per_section_combines_diagnosis_reason_and_local_fix(monkeypatch):
    context, calls = setup(monkeypatch)
    context = nodes.correct_sections_once(None, None, context)
    context = nodes.validate_sections(SimpleNamespace(), SimpleNamespace(), context)
    assert calls == ["s1", "s2"]
    state = context["japanese_proofreading_context"]
    candidate = rules.install(HTML, state["plan"], state["reviews"][0]["paragraphs"])
    assert "責任を果たす" in candidate and "参加者同士" in candidate
    assert '<p id="p2">組織学習の再生産を論じる。</p>' in candidate
    assert '<p id="human">人間の文章。</p>' in candidate
    assert '<cite id="c" data-reference-id="r">『書名』</cite>' in candidate
    assert context["japanese_section_once"]["additional_llmjp_requests"] == 0
    assert len(context["japanese_section_once"]["findings"]) == 2
    assert rules.validate_structure(HTML, candidate, {"human"})


def test_fixed_title_edit_stops_after_original_calls_without_model_retry(monkeypatch):
    context, calls = setup(monkeypatch, bad_title=True)
    context = nodes.correct_sections_once(None, None, context)
    with pytest.raises(NonRetryableFukamiruError, match="changed_fixed_asset"):
        nodes.validate_sections(None, None, context)
    assert calls == ["s1", "s2"] and context["proposed_html"] == HTML


def test_no_change_diagnostics_are_recorded_without_fake_applied_edits(monkeypatch):
    context, calls = setup(monkeypatch, no_change=True)
    context = nodes.correct_sections_once(None, None, context)
    context = nodes.validate_sections(None, None, context)
    assert calls == ["s1", "s2"]
    assert context["japanese_section_once"]["applied_repairs"] == []
    assert len(context["japanese_section_once"]["no_change_items"]) == 2


def test_source_or_missing_section_response_cannot_be_silently_accepted(monkeypatch):
    context, calls = setup(monkeypatch)
    with pytest.raises(NonRetryableFukamiruError, match="response_coverage_mismatch"):
        nodes.validate_sections(None, None, context)
    context["proposed_html"] = HTML.replace("組織学習", "別の本文")
    with pytest.raises(NonRetryableFukamiruError, match="source_changed"):
        nodes.correct_sections_once(None, None, context)
    assert not calls


def test_disabled_japanese_proofreading_has_no_model_requests():
    context = nodes.prepare_sections(None, None, {"proposed_html": HTML, "use_llm_jp": False})
    context = nodes.correct_sections_once(None, None, context)
    context = nodes.validate_sections(None, None, context)
    assert context["japanese_section_once"]["requests"] == []
    assert context["japanese_section_once"]["application_review"]["reviewer"] == "no_changes"
    assert context["proposed_html"] == HTML


def test_content_review_can_hold_one_proposal_without_losing_other_native_edits(monkeypatch):
    context, calls = setup(monkeypatch)
    context = nodes.correct_sections_once(None, None, context)
    proposals = [
        p for r in context["japanese_section_once"]["responses"] for p in r["result"]["corrections"]
    ]
    context["section_once_application_review"] = {
        "source_sha256": context["japanese_section_once"]["source_sha256"],
        "decisions": [
            {
                "correction_sha256": rules.digest(p),
                "verdict": "hold_for_human" if i == 0 else "apply",
                "reason": "内容の確認が必要。" if i == 0 else "内容追加なし。",
            }
            for i, p in enumerate(proposals)
        ],
    }
    context["section_once_require_application_review"] = True
    result = nodes.validate_sections(None, None, context)
    state = result["japanese_proofreading_context"]
    html = rules.install(HTML, state["plan"], state["reviews"][0]["paragraphs"])
    assert "責任を成し遂げる" in html and "参加者同士" in html
    assert len(result["japanese_section_once"]["held_corrections"]) == 1
    assert state["reviews"][0]["human_review"][0]["paragraph_id"] == "p1"
    assert calls == ["s1", "s2"]
    context["section_once_application_review"]["decisions"][1]["correction_sha256"] = "unknown"
    with pytest.raises(NonRetryableFukamiruError, match="application_review_incomplete"):
        nodes.validate_sections(None, None, context)


def test_required_application_review_cannot_be_skipped_or_applied_to_other_text(monkeypatch):
    context, calls = setup(monkeypatch)
    context = nodes.correct_sections_once(None, None, context)
    context["section_once_require_application_review"] = True
    with pytest.raises(NonRetryableFukamiruError, match="application_review_required"):
        nodes.validate_sections(None, None, context)
    context["section_once_application_review"] = {"source_sha256": "other", "decisions": []}
    with pytest.raises(NonRetryableFukamiruError, match="application_review_source_changed"):
        nodes.validate_sections(None, None, context)
    assert calls == ["s1", "s2"]


def test_nonexistent_typo_is_excluded_without_human_review_or_another_japanese_call(monkeypatch):
    context, calls = setup(monkeypatch)
    context = nodes.correct_sections_once(None, None, context)
    phantom = {"paragraph_id": "p1", "before": "架空の計画研球",
               "after": "架空の計画研究", "reason": "書名の誤記を直す。"}
    context["japanese_section_once"]["responses"][0]["result"]["corrections"].append(phantom)
    result = nodes.validate_sections(None, None, context)
    state = result["japanese_section_once"]
    assert state["excluded_corrections"][0]["proposal"] == phantom
    assert state["excluded_corrections"][0]["reason"] == "source_mismatch"
    assert len(state["applied_repairs"]) == 2
    assert not state["held_corrections"]
    assert not result["japanese_proofreading_context"]["reviews"][0]["human_review"]
    assert calls == ["s1", "s2"]


@pytest.mark.parametrize("bad_evidence", [False, True])
def test_sol_reviews_scope_once_with_actual_changed_text_and_no_rewriting(
    monkeypatch, bad_evidence,
):
    from fukamiru.authoring.codex import CodexAuthoringAdapter
    from fukamiru.revisions import proofreading_scope_review as scope

    context, calls = setup(monkeypatch)
    context["section_once_automatic_content_review"] = True
    context = nodes.correct_sections_once(None, None, context)
    condition = {"paragraph_id": "p1", "before": "を読み、", "after": "を読むと、",
                 "reason": "つながりを整える。"}
    context["japanese_section_once"]["responses"][0]["result"]["corrections"].append(condition)
    reviews = []

    def sol(claimed, **kwargs):
        assert kwargs["stage"] == scope.STAGE
        payload = kwargs["payload"]
        reviews.append(payload)
        assert len(payload["paragraphs"]) == 2
        assert {p["paragraph_id"] for p in payload["paragraphs"]} == {"p1", "p3"}
        assert "責任を成し遂げる" in payload["paragraphs"][0]["original_text"]
        assert "を読むと、責任を果たす。" in payload["paragraphs"][0]["candidate_text"]
        assert "p2" not in str(payload) and "html" not in payload
        contract = CodexAuthoringAdapter.execution_contract(scope.STAGE)
        assert (contract["model"], contract["reasoning_effort"]) == ("gpt-5.6-sol", "medium")
        _validate_codex_output_schema(contract["schema"])
        return {"decisions": [{
            "correction_id": p["correction_id"],
            "verdict": "hold_for_human" if p["before"] == condition["before"] else "apply",
            "dimension": "condition" if p["before"] == condition["before"] else "none",
            "original_excerpt": ("存在しない語句" if bad_evidence else "を読み、")
                if p["before"] == condition["before"] else "",
            "candidate_excerpt": "を読むと、" if p["before"] == condition["before"] else "",
            "reason": "並列の記述が条件へ変わる。" if p["before"] == condition["before"]
                else "内容変更・文の破損なし。",
        } for p in payload["corrections"]]}

    if bad_evidence:
        with pytest.raises(NonRetryableFukamiruError, match="scope_evidence_not_in_changed_text"):
            nodes.validate_sections(SimpleNamespace(_authoring_invoke=sol), None, context)
    else:
        result = nodes.validate_sections(SimpleNamespace(_authoring_invoke=sol), None, context)
        state = result["japanese_proofreading_context"]
        html = rules.install(HTML, state["plan"], state["reviews"][0]["paragraphs"])
        assert "を読み、責任を果たす。" in html and "参加者同士" in html
        assert len(result["japanese_section_once"]["held_corrections"]) == 1
    assert len(reviews) == 1 and calls == ["s1", "s2"]


@pytest.mark.parametrize("example", ["collocation", "nonexistent_typo", "verification_note"])
def test_section_segment_keeps_local_fix_titles_notes_ids_and_routes_caveats(monkeypatch, example):
    from xml.etree import ElementTree as ET

    from fukamiru.revisions.footnote_citations import EvidenceFootnote, EvidenceFootnoteRenderer
    from fukamiru.worker.runner import WorkerRunner

    note = "ただし、原典との照合はできていない。"
    limitation = "ただし、この概念は異なる制度を同一視する名称ではない。"
    html = ('<article id="a" lang="ja"><section id="s"><h2 id="h">節</h2>'
            '<p id="p1">甲<cite id="c1" data-reference-id="r1">『第一作』</cite>'
            'を読み、責任を成し遂げる。組織学習の再生産を論じる。</p>'
            '<p id="p2">架空著者<cite id="c2" data-reference-id="r2">'
            '『架空の計画研究』</cite>を参照する。' + note + limitation + '</p>'
            '<p id="human">人間の文章。' + note + '</p></section></article>')
    model_calls, remote_calls = [], []

    def model(worker, claimed, context, stage, prompt, payload, schema):
        model_calls.append(stage)
        _validate_codex_output_schema(schema)
        patches = [{"paragraph_id": "p1", "before": "責任を成し遂げる",
                    "after": "責任を果たす", "reason": "自然な連語に直す。"}]
        if example == "nonexistent_typo":
            patches.append({"paragraph_id": "p2", "before": "架空の計画研球",
                            "after": "架空の計画研究", "reason": "書名の誤記。"})
        return {"corrections": patches, "human_review": []}

    def remote(claimed, **kwargs):
        stage, payload = kwargs["stage"], kwargs["payload"]
        remote_calls.append(stage)
        if stage == nodes.scope_review.STAGE:
            return {"decisions": [{"correction_id": p["correction_id"], "verdict": "apply",
                "dimension": "none", "original_excerpt": "", "candidate_excerpt": "",
                "reason": "内容と文のつながりを保持。"} for p in payload["corrections"]]}
        if stage == "format_adopted_japanese_v32":
            assert payload["supplied_rules"]
            return {"paragraphs": [{**p, "reason": "既存の指定表記を保持。",
                "applied_rule_ids": []} for p in payload["paragraphs"]], "human_review": []}
        assert stage == "extract_paragraph_meaning_v31"
        assert payload["purpose"] == "route_verification_notes_to_human_review"
        return {"paragraphs": [{"paragraph_id": p["paragraph_id"],
            "editorial_only": [note] if p["paragraph_id"] == "p2" else []}
            for p in payload["paragraphs"]]}

    monkeypatch.setattr(nodes.adoption, "_call", model)
    monkeypatch.setattr(WorkerRunner, "_authoring_v29_plan_bridge", staticmethod(
        lambda *a, **kw: {"structure_plan_validation": {"valid": True}}))
    worker = SimpleNamespace(_authoring_invoke=remote)
    context = {"proposed_html": html, "human_protected_block_ids": ["human"]}
    for step in (nodes.prepare_sections, nodes.correct_sections_once, nodes.validate_sections,
                 nodes.adoption.apply, nodes.adoption.reconcile):
        context = step(worker, SimpleNamespace(), context)
    citations = [EvidenceFootnote(block_id=pid, evidence_ids=(rid,), anchor_reference_id=rid,
        reference={"reference_id": rid, "record_type": "book", "title": title,
                   "publisher": "出版社", "year": "2020", "authors": ["著者"],
                   "evidence_ids": [rid], "publication_language": "ja",
                   "bibliography_people": [{"role": "author", "name_profile": "japanese_native",
                                            "family": "山田", "given": "太郎"}]})
        for pid, rid, title in [("p1", "r1", "第一作"), ("p2", "r2", "架空の計画研究")]]
    context["proposed_html"] = EvidenceFootnoteRenderer.render(
        context["proposed_html"], citations, place_before_punctuation=True).html
    rendered = context["proposed_html"]
    context = nodes.adoption.finish(worker, SimpleNamespace(), context)
    output = context["proposed_html"]
    root = ET.fromstring(output)
    assert "責任を果たす" in output and "組織学習の再生産" in output
    assert note not in "".join(root.find(".//*[@id='p2']").itertext())
    assert limitation in output and note in "".join(root.find(".//*[@id='human']").itertext())
    assert [sup.tail for cite in root.iter("cite") for sup in cite.iter("sup")] == ["』", "』"]
    assert {n.get("id") for n in ET.fromstring(rendered).iter()} == {
        n.get("id") for n in root.iter()}
    for tag in ("cite", "sup", "li"):
        def contents(document, tag=tag):
            for n in ET.fromstring(document).iter(tag):
                n.tail = None  # Editable following prose is checked separately.
                yield ET.tostring(n)
        assert list(contents(output)) == list(contents(rendered))
    assert context["verification_notes_routed_v32"][0]["reason"] == note
    assert len(model_calls) == 1
    assert remote_calls == [nodes.scope_review.STAGE, "format_adopted_japanese_v32",
                            "extract_paragraph_meaning_v31"]


def test_finalizer_rejects_unrecorded_prose_changes_even_if_routing_claims_success(monkeypatch):
    from fukamiru.authoring import verification_notes

    context = nodes.prepare_sections(None, None, {"proposed_html": HTML})
    context["proofreading_adoption"]["application"] = {"formatted_html": HTML}
    monkeypatch.setattr(verification_notes, "route", lambda *a: (
        HTML.replace("組織学習", "個人学習"), []))
    with pytest.raises(NonRetryableFukamiruError, match="final_prose_changed"):
        nodes.adoption.finish(None, None, context)


def test_review_evidence_must_cover_the_changed_side_of_an_insertion_or_deletion():
    assert not nodes._covers_change("甲乙丙", "甲丙", "甲", after=False)
    assert nodes._covers_change("甲乙丙", "甲丙", "乙", after=False)
    assert nodes._covers_change("甲乙丙", "甲丙", "甲丙", after=True)
    assert not nodes._covers_change("甲乙丙", "甲新乙丙", "丙", after=True)
    assert nodes._covers_change("甲乙丙", "甲新乙丙", "新", after=True)
