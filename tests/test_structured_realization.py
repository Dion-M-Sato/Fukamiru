"""Structural realization examples, separate from the manuscript experiment."""

from __future__ import annotations

import pytest

from fukamiru.errors import NonRetryableFukamiruError
from fukamiru.revisions.structured_realization import (
    analyze_source,
    process_structured_frame,
)
from fukamiru.revisions.uniform_full_sentence import UniformTokenScorer
from fukamiru.worker.collocation import verify_and_decide


def _run(sentence: str, original: str, raw: str) -> dict:
    start = sentence.index(original)
    return process_structured_frame(
        sentence,
        start,
        start + len(original),
        frame_id="synthetic-test",
        raw_generations=[
            {
                "seed": 1001,
                "raw_text": raw,
                "generated_token_count": 3,
                "generation_origin": "fixed_test_lexeme_not_model_discovery",
            }
        ],
    )


@pytest.mark.parametrize(
    ("sentence", "original", "raw", "expected"),
    [
        ("資料を配列できた。", "配列できた", "並べる。", "資料を並べられた。"),
        ("資料を配列できた。", "配列できた", "並置する。", "資料を並置できた。"),
        ("文字を書いた。", "書いた", "描く。", "文字を描いた。"),
        ("文字を描きうる。", "描きうる", "書く。", "文字を書きうる。"),
        ("文字を書いている。", "書いている", "読む。", "文字を読んでいる。"),
        ("資料を利用可能にする。", "利用可能にする", "検証する。", "資料を検証可能にする。"),
        ("資料を試みなくてもよい。", "試みなくても", "書く。", "資料を書かなくてもよい。"),
    ],
)
def test_completed_sentence_from_source_functions(
    sentence: str, original: str, raw: str, expected: str
) -> None:
    result = _run(sentence, original, raw)
    assert result["source"]["status"] == "constructed"
    assert expected in {row["sentence"] for row in result["candidate_groups"]}
    assert result["source_reconstruction"][0]["roundtrip"]


def test_functional_expression_not_split_into_predicate_fragment() -> None:
    sentence = "この資料について検討する。"
    start = sentence.index("つい")
    result = analyze_source(sentence, start, start + 2, frame_id="synthetic-functional")
    assert result["status"] == "functional_expression_preserved"
    assert result["source_expression"] == "について"


def test_orthographic_source_roundtrip_preserves_manuscript_script() -> None:
    for expression in ("いった", "もって", "みなさない", "ならない", "あった"):
        sentence = "彼が" + expression + "。"
        result = analyze_source(sentence, 2, 2 + len(expression), frame_id="synthetic-script")
        assert result["status"] == "constructed", (expression, result.get("reason"))
        assert result["P"] + result["E0"] + result["R"] == sentence


def test_one_bad_generation_does_not_block_other_candidates() -> None:
    sentence = "文字を書いた。"
    result = process_structured_frame(
        sentence,
        3,
        6,
        frame_id="synthetic-independent",
        raw_generations=[
            {"seed": 1001, "raw_text": "。", "generated_token_count": 1},
            {"seed": 1002, "raw_text": "描く。", "generated_token_count": 3},
        ],
    )
    assert result["raw_results"][0]["extraction"]["status"] == "candidate_extraction_failed"
    assert any(row["sentence"] == "文字を描いた。" for row in result["candidate_groups"])


def test_potential_function_does_not_depend_on_dekiru_script() -> None:
    statuses = []
    for sentence in ("資料を確認出来た。", "資料を確認できた。"):
        result = _run(sentence, sentence[3:-1], "検証する。")
        statuses.append(result["source"]["status"])
        assert any(group["expression"] == "検証できた" for group in result["candidate_groups"])
    assert statuses == ["constructed", "constructed"]


def test_opt_in_worker_profile_cannot_execute_codex_review() -> None:
    with pytest.raises(NonRetryableFukamiruError, match="acceptance review is disabled"):
        verify_and_decide(
            None, None, {"collocation_candidate_profile": "structured-realization-v3"}
        )


def test_candidate_inherent_potential_is_not_stacked_twice() -> None:
    result = _run("資料を配列できた。", "配列できた", "確認できる。")
    assert any(group["expression"] == "確認できた" for group in result["candidate_groups"])
    assert all("できられ" not in group["expression"] for group in result["candidate_groups"])


def test_argument_links_exclude_unrelated_clause() -> None:
    sentence = "彼は本を読んだが、私は資料を書いた。"
    start = sentence.index("書いた")
    source = analyze_source(sentence, start, start + 3, frame_id="synthetic-clause")
    assert source["status"] == "constructed"
    heads = {item["head_token"] for item in source["argument_links"]}
    assert "本" not in heads
    assert "私" in heads
    assert "資料" in heads


def test_uniform_scorer_uses_bos_context_and_only_target_text_tokens() -> None:
    class _Settings:
        request_timeout_seconds = 1

    class _Adapter:
        settings = _Settings()

        def tokenize(self, text: str, *, add_special: bool) -> tuple[tuple[int, str], ...]:
            assert add_special
            return ((1, "<BOS>"),) if not text else ((1, "<BOS>"), (2, text))

        def detokenize(self, ids: tuple[int, ...]) -> str:
            return "<BOS>" + ("文" if ids == (1, 2) else "")

        def _request_json(self, route: str, body: dict, *, timeout: int) -> dict:
            assert route == "/completion" and timeout == 1
            assert body["prompt"] == [1]
            assert body["logit_bias"] == [[2, 100.0]]
            assert body["cache_prompt"] is False
            return {"tokens": [2], "completion_probabilities": [{"id": 2, "logprob": -1.25}]}

    scored = UniformTokenScorer(
        _Adapter(), context_size=16, cache_prompt=False, profile_hash="fixed-test-profile"
    ).score("文")
    assert scored["J_nats"] == -1.25
    assert scored["token_ids"] == [2]
    assert scored["bos_scored"] is False
    assert scored["eos_appended"] is False


def test_existential_lexeme_is_not_doubled_by_teiru_operator() -> None:
    for raw in ("いる。", "居る。", "ある。", "有る。"):
        bad = _run("彼は外側に位置していた。", "位置していた", raw)
        assert any(
            failure["reason"] == "existential_lexeme_would_double_aspect"
            for failure in bad["realization_failures"]
        )
        assert not bad["candidate_groups"]
    good = _run("彼は文字を書いていた。", "書いていた", "読んだ。")
    assert any(group["expression"] == "読んでいた" for group in good["candidate_groups"])
