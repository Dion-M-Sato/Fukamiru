"""Argument reading budgets preserve opportunities and unresolved gaps."""

from copy import deepcopy

import pytest

from fukamiru.authoring.reading_budget import (
    allocations,
    reading_session,
    source_hints,
)
from fukamiru.authoring.source_reads import known_work_passages


def source(text="関連する索引。", key="A", asset="asset"):
    return {"candidate_key": key, "title": "研究書", "excerpt": text,
            "provenance": {"source_asset_id": asset, "source_level": "full_text"}}


def verdict(status="insufficient", quote="", key="A"):
    return {"verdicts": [{"unit_id": "O", "status": status,
                          "rationale": "定義と適用条件を確認する。",
                          "citations": [{"candidate_key": key, "quote": quote}]
                          if quote else []}]}


def run(**kwargs):
    defaults = dict(
        plan={"obligation_id": "O", "query": "概念の適用", "variants": ["概念"]},
        units=[{"unit_id": "O", "query": "概念の適用"}], sources=[source()],
        audit=lambda sources: verdict(), read=lambda source, queries, round_index: source,
        token_limit=20000, max_rounds=2, max_works=3, seconds=180,
    )
    return reading_session(**{**defaults, **kwargs})


def test_named_work_and_concept_survive_as_separate_leads():
    context = {"original_note_paragraph":
               "『架空資料の比較分析』の「セノグラフィー」を応用する。"}
    hints = source_hints(context)
    assert hints == ["架空資料の比較分析", "セノグラフィー"]
    reads = []

    def read(current, queries, round_index):
        reads.append(queries)
        return current

    run(plan={"query": "配置への応用", "source_hints": hints, "source_context": context}, read=read)
    assert "セノグラフィー" in reads[0]
    assert "架空資料の比較分析" not in reads[0]


def test_long_note_keeps_its_first_review_when_the_question_can_afford_it():
    body = "配置を示す資料である。"
    result = run(
        units=[{"unit_id": "O", "source_context": {"original": "文" * 1500}}],
        token_limit=5000, sources=[source(body)],
        audit=lambda sources: verdict("supports_subclaim", body),
    )
    assert result["reading_budget"]["stop_reason"] == "resolved"
    assert result["reading_budget"]["audit_submissions"] == 1
    assert result["reading_budget"]["estimated_input_tokens"] <= 5000


def test_reserved_shares_are_order_independent_and_sum_to_run_limit():
    plans = [{"obligation_id": "core", "argument_necessity": "core"},
             {"obligation_id": "explicit", "memo_annotation_kinds": ["self_citation"]},
             {"obligation_id": "other"}]
    shares = allocations(plans, 25000)
    assert shares == {"core": 10000, "explicit": 10000, "other": 5000}
    assert allocations(list(reversed(plans)), 25000) == shares
    assert sum(shares.values()) == 25000


def test_insufficient_without_quote_still_gets_substantive_read_and_expands_scope():
    reads = []
    audits = []

    def read(current, queries, round_index):
        reads.append((round_index, queries))
        return {**current, "excerpt": "概念とは配置を指す。" if round_index == 0 else
                "概念とは配置を指す。ただし本書の対象は舞台に限る。"}

    def audit(sources):
        audits.append(sources[0]["excerpt"])
        if "ただし" in audits[-1]:
            return verdict("contradicts", audits[-1])
        return verdict()

    result = run(read=read, audit=audit)
    assert len(audits) == 3 and [r[0] for r in reads] == [0, 1]
    assert "適用条件" in reads[1][1][-1]
    assert result["reading_budget"]["stop_reason"] == "resolved"
    assert result["verdicts"][0]["status"] == "contradicts"


def test_no_new_information_stops_without_repeat_audit():
    result = run()
    ledger = result["reading_budget"]
    assert ledger["stop_reason"] == "no_new_information"
    assert ledger["audit_submissions"] == 1
    assert ledger["source_read_attempts"] == 1
    assert result["verdicts"][0]["status"] == "insufficient"


@pytest.mark.parametrize("limit,reason", [(1, "input_token_budget"), (20000, "expansion_budget")])
def test_budget_stop_preserves_insufficient_instead_of_disproving_claim(limit, reason):
    result = run(token_limit=limit, max_rounds=0)
    assert result["reading_budget"]["stop_reason"] == reason
    assert result["reading_budget"]["unresolved"] is True
    assert result["reading_budget"]["estimated_input_tokens"] <= limit
    assert result["verdicts"][0]["status"] == "insufficient"


def test_unavailable_read_is_distinct_from_no_new_information():
    def read(current, queries, round_index):
        return {**current, "provenance": {"content_acquisition": {"status": "unavailable"}}}

    assert run(read=read)["reading_budget"]["stop_reason"] == "source_inaccessible"


def test_deadline_stops_new_work_without_changing_audit_verdict():
    ticks = iter([0, 0, 181, 181])
    result = run(clock=lambda: next(ticks))
    assert result["reading_budget"]["stop_reason"] == "time_budget"
    assert result["verdicts"][0]["status"] == "insufficient"


def test_same_work_is_not_charged_as_three_independent_source_reads():
    sources = [source(key=str(i)) for i in range(3)]
    result = run(sources=sources)
    assert result["reading_budget"]["source_read_attempts"] == 1


def test_retry_cannot_reset_its_question_share():
    result = run(token_limit=12000)
    previous = deepcopy(result["reading_budget"])
    retry = run(token_limit=12000, previous=previous)
    assert retry["reading_budget"]["estimated_input_tokens"] > previous["estimated_input_tokens"]
    assert retry["reading_budget"]["expansion_rounds"] == 2
    assert retry["reading_budget"]["estimated_input_tokens"] <= 12000


def test_unchanged_resolution_survives_retry_without_spending_again():
    body = "配置を示す資料である。"
    first = run(sources=[source(body)], audit=lambda sources: verdict("supports_subclaim", body))
    previous = first["reading_budget"]
    previous["elapsed_seconds"] = 181
    previous["estimated_input_tokens"] = previous["token_limit"]
    result = run(sources=first["sources"], previous=previous,
                 audit=lambda sources: pytest.fail("An identical resolution must be reused"))
    assert result["reading_budget"]["stop_reason"] == "resolved"
    assert result["reading_budget"]["resolution_reuses"] == 1
    assert result["reading_budget"]["audit_submissions"] == 1
    assert result["reading_budget"]["estimated_input_tokens"] == previous["token_limit"]
    assert "resolution_reuses" not in previous
    changed = run(sources=[source("本文が変わった。")], previous=previous)
    assert changed["reading_budget"]["stop_reason"] == "time_budget"
    changed_model = run(sources=first["sources"], previous=previous,
                        audit_identity={"model": "different-model"})
    assert changed_model["reading_budget"]["stop_reason"] == "time_budget"
    changed_claim = run(sources=first["sources"], previous=previous,
                        units=[{"unit_id": "O", "query": "異なる主張"}])
    assert changed_claim["reading_budget"]["stop_reason"] == "time_budget"


def test_window_keeps_neighboring_limitation_and_grows_without_changing_asset():
    spans = [{"id": str(i), "text": text, "start_page": i + 1} for i, text in enumerate([
        "前置き。", "「セノグラフィー」とは配置を指す。", "ただし舞台のみを対象とする。",
        "雑誌への応用は本書では論じない。",
    ])]
    read = {"source_asset_id": "asset", "document_unit_id": "doc",
            "content_sha256": "hash", "bibliography": {}, "spans": spans}
    result = known_work_passages(read, ["セノグラフィー"], limit=70)
    assert "ただし" in result["excerpt"]
    assert result["reading_scope"] == "selected_passages"
    expanded = known_work_passages(read, ["セノグラフィー"], limit=200, context_radius=2)
    assert expanded["excerpt_complete"] is True
    assert "雑誌への応用" in expanded["excerpt"]
    assert expanded["citation"]["source_asset_id"] == "asset"
