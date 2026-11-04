"""One-way epistemic calibration of interpretations, never factual verification."""

from __future__ import annotations

import re

_TENTATIVE = re.compile(
    r"だろう|であろう|と思われる|と考えられる|かもしれない|可能性|仮説|推測|未確認|未検証"
)


def has_tentative_wording(sentence: str) -> bool:
    return bool(_TENTATIVE.search(sentence))


def hedge_interpretation(sentence: str) -> str:
    if has_tentative_wording(sentence) or not sentence.endswith("。"):
        return sentence
    body = sentence[:-1]
    if body.endswith("である"):
        return body[:-3] + "であろう。"
    if body.endswith("のだ"):
        return body[:-2] + "のだろう。"
    if body.endswith("だ"):
        return body[:-1] + "だろう。"
    # Preserve negation, tense and arguments; only append epistemic qualification.
    if re.search(r"[うくぐすつぬぶむるいた]$", body):
        return body + "と考えられる。"
    return sentence
