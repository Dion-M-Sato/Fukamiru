"""Conservative language signals shared by corpus ingestion and PDF OCR routing."""

from __future__ import annotations

import re
from collections import Counter

LATIN_MARKERS = {
    "en": {"the", "of", "and", "is", "to", "in", "that", "with", "for", "from", "this"},
    "fr": {"le", "la", "les", "de", "des", "du", "une", "est", "que", "dans", "pour"},
    "de": {
        "der",
        "die",
        "das",
        "und",
        "ist",
        "ein",
        "eine",
        "den",
        "von",
        "mit",
        "nicht",
        "sich",
        "auf",
        "für",
        "zu",
    },
}
RUSSIAN_MARKERS = {"и", "в", "не", "на", "что", "это", "как", "из", "для", "по", "с", "которые"}


def language_signals(text: str) -> dict[str, int]:
    """Count script and lexical evidence without assuming that all Cyrillic is Russian."""
    lowered = text.casefold()
    latin_words = re.findall(r"[a-z\u00c0-\u024f]+", lowered)
    cyrillic_words = re.findall(r"[\u0400-\u052f]+", lowered)
    latin_counts = Counter(latin_words)
    cyrillic_counts = Counter(cyrillic_words)
    signals = {
        "kana": len(re.findall(r"[\u3040-\u30ff]", text)),
        "han": len(re.findall(r"[\u3400-\u9fff]", text)),
        "hangul": len(re.findall(r"[\uac00-\ud7af]", text)),
        "cyrillic": sum(len(word) for word in cyrillic_words),
        "latin": sum(len(word) for word in latin_words),
        "latin_words": len(latin_words),
        "cyrillic_words": len(cyrillic_words),
        "ru": sum(cyrillic_counts[word] for word in RUSSIAN_MARKERS),
    }
    for language, markers in LATIN_MARKERS.items():
        signals[language] = sum(latin_counts[word] for word in markers)
    signals["fr"] += 2 * len(re.findall(r"[àâçéèêëîïôùûüÿœæ]", lowered))
    signals["de"] += 2 * len(re.findall(r"[äöüß]", lowered))
    return signals


def classify_language(text: str, *, strict: bool = False) -> tuple[str, dict[str, int]]:
    """Return an ISO 639-1 code or ``und`` when evidence is insufficient."""
    signals = language_signals(text)
    script_total = sum(signals[key] for key in ("kana", "han", "hangul", "cyrillic", "latin"))
    script_minimum = 8 if strict else 3
    if signals["hangul"] >= script_minimum and signals["hangul"] / max(1, script_total) >= 0.2:
        return "ko", signals
    if (
        signals["kana"] >= script_minimum
        and (signals["kana"] + signals["han"]) / max(1, script_total) >= 0.2
    ):
        return "ja", signals
    if (
        signals["han"] >= 20
        and signals["kana"] == 0
        and signals["han"] / max(1, script_total) >= 0.2
    ):
        return "zh", signals
    if signals["cyrillic"] >= script_minimum and signals["cyrillic"] / max(1, script_total) >= 0.2:
        if signals["ru"] >= (4 if strict else 2) and signals["cyrillic_words"] >= (
            12 if strict else 3
        ):
            return "ru", signals
        return "und", signals
    scores = {language: signals[language] for language in LATIN_MARKERS}
    winner = max(scores, key=scores.get)
    runner_up = max(score for language, score in scores.items() if language != winner)
    if (
        signals["latin_words"] >= (12 if strict else 3)
        and scores[winner] >= (4 if strict else 2)
        and scores[winner] >= max(1, runner_up) * 2
    ):
        return winner, signals
    return "und", signals
