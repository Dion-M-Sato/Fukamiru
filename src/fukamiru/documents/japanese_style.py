"""Shared guidance for natural Japanese prose across generative stages."""

from __future__ import annotations

JAPANESE_PROSE_GUIDANCE = (
    "自然な学術日本語にする。動作主体と対象が明らかな箇所では、動詞で直接述べ、"
    "『分析の実施を行う』のようにサ変名詞を『実施する』『行う』『図る』へ重ねた"
    "名詞構文を作らない。意味上必要な概念名詞や専門用語まで機械的に動詞化しない。"
    "『重要な役割を果たす』『多角的な観点から』などの定型句、空疎な強調、"
    "同じ型の文頭・結語を、根拠や談話上の役割なしに足さない。簡潔さのために主体、"
    "対象、否定、時制、モダリティ、因果の強さを変えてはならない。"
)

ENGLISH_JAPANESE_PROSE_GUIDANCE = (
    "When the target is Japanese, use natural scholarly Japanese. Prefer a direct "
    "finite verb when the actor and action are clear; do not stack a verbal noun with "
    "generic verbs such as 実施する, 行う, or 図る merely to sound formal. Preserve "
    "genuine technical or conceptual nouns. Do not add canned claims such as "
    "『重要な役割を果たす』, empty intensifiers, or repetitive openings and summaries "
    "unless the source or discourse plan requires them. Concision must not change the "
    "agent, object, negation, tense, modality, or strength of causation. "
)
