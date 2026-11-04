"""Deterministic parts of the V29 full-sentence Japanese proofreading contract.

Only explicit numeric, attribution and bibliographic invariants can hard reject.
Natural rewrites and modality changes are left to the candidate selector.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter
from functools import lru_cache
from itertools import pairwise
from typing import Any

from fukamiru.revisions.japanese_analysis import analyzer_versions

CONTRACT = "manuscript-proofreading-v29.1"
DIFF_VERSION = "raw-character-sequencematcher-autojunk-false-v1"
NUMERIC_VERSION = "numeric-invariant-v50"
ATTRIBUTION_VERSION = "attribution-invariant-v50"
TITLE_VERSION = "original-work-title-registry-v50"
LINT_VERSION = "mechanical-lint-v47-mecab"
SENTENCE_END = frozenset("。！？!?")
BRACKET_CLOSE = {
    "「": "」",
    "『": "』",
    "（": "）",
    "(": ")",
    "［": "］",
    "[": "]",
    "【": "】",
    "《": "》",
}
QUOTE = re.compile(r"『([^『』]{1,100})』")
ANY_QUOTE = re.compile(r"[『「]([^『』「」]{1,100})[』」]")
NUMBER = re.compile(r"[〇零一二三四五六七八九]{2,}|[0-9０-９]+")
KANJI_DIGIT = dict(zip("〇零一二三四五六七八九", "00123456789", strict=True))
FULL_DIGIT = str.maketrans("０１２３４５６７８９", "0123456789")
PERSON = r"(?:[ァ-ヴー]+(?:・[ァ-ヴー]+)*|[一-龥々]{2,5})"
TITLE_SURFACE = r"(?:『[^』]{1,80}』|「[^」]{1,80}」)"
ATTR_RULES = [
    (
        rf"(?P<source>{PERSON})の指摘に従(?:えば|うなら)",
        "author_inference_from_source",
        "point_followed",
    ),
    (
        rf"(?P<source>{PERSON})(?:の?{TITLE_SURFACE})?を参照(?:するなら|すれば|すると)",
        "author_inference_from_source",
        "reference_if",
    ),
    (
        rf"(?P<source>{PERSON})の議論を参照(?:するなら|すれば|すると)",
        "author_inference_from_source",
        "argument_reference",
    ),
    (
        rf"(?P<source>{PERSON})の[^、。]{{1,80}}?を(?:分析上の)?手[が掛]かり(?:にすれば|とするなら)",
        "author_inference_from_source",
        "clue_if",
    ),
    (
        rf"(?P<source>{PERSON}){TITLE_SURFACE}の[^、。]{{1,80}}?を(?:分析上の)?手[が掛]かり(?:にすれば|とするなら)",
        "author_inference_from_source",
        "title_clue_if",
    ),
    (
        rf"(?P<source>{PERSON})が区別する[^、。]{{1,90}}?観点を援用すれば",
        "author_inference_from_source",
        "distinction_adopted",
    ),
    (
        rf"(?P<source>{PERSON})の(?:理論|議論|概念)を援用すれば",
        "author_inference_from_source",
        "theory_adopted",
    ),
    (rf"(?P<source>{PERSON})を踏まえる(?:なら|と)", "author_inference_from_source", "consider_if"),
    (
        rf"(?P<source>{PERSON})の[^、。]{{1,70}}?を(?:方法的に)?援用するなら",
        "author_inference_from_source",
        "method_adopted",
    ),
    (
        rf"(?P<source>{PERSON}){TITLE_SURFACE}を参照しうるのは",
        "author_inference_from_source",
        "reference_possible",
    ),
    (
        rf"(?P<source>{PERSON})(?:の?{TITLE_SURFACE})?によ(?:れば|ると)",
        "source_assertion",
        "according_to",
    ),
    (
        rf"(?P<source>{PERSON})が{TITLE_SURFACE}(?:で|において|を)(?:[^、。]{{0,30}}?)(?:論じる|論じた|述べる|述べた|示す|示した)",
        "source_assertion",
        "source_title_in_work",
    ),
    (rf"(?P<source>{PERSON}){TITLE_SURFACE}が論じたように", "source_assertion", "title_argument"),
    (
        r"(?P<source>[ァ-ヴー・]{3,24})[がは][^、。]{0,30}?(?:述べる|論じる|指摘する|主張する|定義する|示す|示唆する|捉える)",
        "source_assertion",
        "source_verb",
    ),
]
NON_PERSON = {
    "本稿",
    "筆者",
    "作品",
    "読者",
    "作者",
    "媒体",
    "研究",
    "理論",
    "これ",
    "それ",
    "ここ",
}
AUTHOR = re.compile(r"([一-龥々ァ-ヶー・]{2,18})(が|の)?$")
ATTRIBUTED = re.compile(
    r"^(?:で(?:論じ|述べ|示し|描き|扱い|検討|考察)|を(?:参照|論じ)|によ(?:れば|ると))"
)
NONWORK = re.compile(
    r"本稿|分析(?:上|カテゴリ|概念)|仮(?:称|ラベル)|と呼ぶ|と総称|という(?:概念|語)|ここでいう"
)
WORK_CONTEXT = re.compile(r"作品|漫画|小説|著書|映画|論考|写真論|書籍|文献")
NON_AUTHOR = {"年代", "掲載媒体", "代表例", "アニメーション映画", "映画", "作品", "漫画", "著書"}
JAPANESE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff々〆ヵヶ]")
JUSTIFIED_REPETITIONS = {"時々", "人々", "様々", "それぞれ"}


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def extract_sentences(
    canonical_html: str,
    *,
    run_id: str,
    document_id: str,
    source_preserve_block_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Use stable, block-local offsets; headings, references and protected markup stay out."""
    source_preserve_block_ids = source_preserve_block_ids or set()
    result: list[dict[str, Any]] = []
    root = ET.fromstring(canonical_html)
    for element in root.iter():
        if element.tag != "p":
            continue
        block_id = str(element.attrib.get("id") or "")
        if not block_id or block_id in source_preserve_block_ids:
            continue
        if any(
            child.tag in {"q", "blockquote", "cite", "ref", "a"}
            for child in element.iter()
            if child is not element
        ):
            continue
        text = "".join(element.itertext())
        if not text.strip() or _reference_only(text.strip()):
            continue
        for ordinal, (start_raw, end_raw) in enumerate(sentence_spans(text)):
            original = text[start_raw:end_raw].strip()
            if not original or _quote_only(original):
                continue
            leading = len(text[start_raw:end_raw]) - len(text[start_raw:end_raw].lstrip())
            start = start_raw + leading
            end = start + len(original)
            source_sentence_id = f"{block_id}:s{ordinal}"
            result.append(
                {
                    "id": f"{run_id}:{source_sentence_id}",
                    "source_sentence_id": source_sentence_id,
                    "run_id": run_id,
                    "document_id": document_id,
                    "block_id": block_id,
                    "sentence_ordinal": ordinal,
                    "start_offset": start,
                    "end_offset": end,
                    "original_text": original,
                    "original_hash": digest(original),
                }
            )
    return result


def sentence_spans(text: str) -> list[tuple[int, int]]:
    """Split at sentence punctuation outside quoted works and balanced brackets."""
    spans: list[tuple[int, int]] = []
    closers: list[str] = []
    start = 0
    for index, character in enumerate(text):
        if character in BRACKET_CLOSE:
            closers.append(BRACKET_CLOSE[character])
        elif character in BRACKET_CLOSE.values():
            if not closers or character != closers.pop():
                return []
        elif character in SENTENCE_END and not closers:
            spans.append((start, index + 1))
            start = index + 1
    if closers:
        return []
    if text[start:].strip():
        spans.append((start, len(text)))
    return spans


def _quote_only(text: str) -> bool:
    return bool(
        re.fullmatch(r"[\s「」『』\"'（）()]+.*[」』\"'](?:[。！？!?])?", text)
        and not re.search(r"[はがをにでと]", text)
    )


def _reference_only(text: str) -> bool:
    return bool(
        re.match(r"^(?:参考文献|文献一覧|引用文献)", text)
        or re.fullmatch(r"[^。！？!?]{1,160}(?:19|20)\d{2}[^。！？!?]*", text)
    )


def raw_changed_spans(original: str, candidate: str) -> list[dict[str, Any]]:
    spans = []
    for op, i, j, k, end_candidate in difflib.SequenceMatcher(
        None, original, candidate, autojunk=False
    ).get_opcodes():
        if op != "equal":
            spans.append(
                {
                    "operation": op,
                    "source_span": [i, j],
                    "candidate_span": [k, end_candidate],
                    "source_text": original[i:j],
                    "candidate_text": candidate[k:end_candidate],
                    "source_context": original[max(0, i - 20) : min(len(original), j + 20)],
                    "candidate_context": candidate[
                        max(0, k - 20) : min(len(candidate), end_candidate + 20)
                    ],
                }
            )
    return spans


def normalize_quotes(original: str, candidate: str) -> str:
    for opener, closer, other_open, other_close in (
        ("『", "』", "「", "」"),
        ("「", "」", "『", "』"),
    ):
        for match in re.finditer(
            re.escape(opener) + r"([^『』「」]{1,80})" + re.escape(closer), original
        ):
            other = other_open + match.group(1) + other_close
            if other in candidate:
                candidate = candidate.replace(other, match.group())
    return candidate


def numeric_tokens(sentence: str) -> list[dict[str, Any]]:
    tokens = []
    for match in NUMBER.finditer(sentence):
        raw = match.group()
        value = "".join(KANJI_DIGIT.get(ch, ch) for ch in raw).translate(FULL_DIGIT)
        tokens.append(
            {
                "surface": raw,
                "value": value,
                "start": match.start(),
                "end": match.end(),
                "decade": sentence[match.end() :].startswith("年代"),
            }
        )
    for index, token in enumerate(tokens):
        token["resolved_value"] = token["value"]
        token["inheritance"] = None
        if index and token["decade"] and len(token["value"]) == 2:
            previous = tokens[index - 1]
            between = sentence[previous["end"] : token["start"]]
            if (
                previous["decade"]
                and len(previous["resolved_value"]) == 4
                and re.fullmatch(r"年代(?:から|～|〜|－|-|―|–|−|至|ないし)", between)
            ):
                token["resolved_value"] = previous["resolved_value"][:2] + token["value"]
                token["inheritance"] = previous["surface"]
    return tokens


def numeric_check(original: str, candidate: str) -> dict[str, Any]:
    a, b = numeric_tokens(original), numeric_tokens(candidate)
    av, bv = [x["resolved_value"] for x in a], [x["resolved_value"] for x in b]
    if av == bv:
        status = "equivalent"
    elif (
        len(a) == len(b)
        and all(x["decade"] == y["decade"] for x, y in zip(a, b, strict=True))
        and all(len(x) == len(y) for x, y in zip(av, bv, strict=True))
    ):
        status = "high_confidence_content_change"
    else:
        status = "manual_check"
    normalized = candidate
    if status == "equivalent" and len(a) == len(b):
        for x, y in reversed(list(zip(a, b, strict=True))):
            if x["surface"] != y["surface"]:
                normalized = normalized[: y["start"]] + x["surface"] + normalized[y["end"] :]
    return {
        "status": status,
        "original_tokens": a,
        "candidate_tokens": b,
        "original_resolved": av,
        "candidate_resolved": bv,
        "normalized_candidate_sentence": normalized,
        "hard_reasons": ["numeric_content_changed"]
        if status == "high_confidence_content_change"
        else [],
        "manual_reasons": ["numeric_uncertain"] if status == "manual_check" else [],
    }


def detect_attribution(text: str) -> list[dict[str, Any]]:
    found, seen = [], set()
    for pattern, mode, rule in ATTR_RULES:
        for match in re.finditer(pattern, text):
            source = match.group("source")
            if source in NON_PERSON:
                continue
            key = (source, mode, match.start())
            if key in seen:
                continue
            seen.add(key)
            found.append(
                {
                    "source_text": source,
                    "attribution_mode": mode,
                    "rule": rule,
                    "marker_surface": match.group(),
                    "marker_span": [match.start(), match.end()],
                    "confidence": "high",
                }
            )
    return sorted(found, key=lambda x: x["marker_span"][0])


def attribution_check(original: str, candidate: str) -> dict[str, Any]:
    source, target = detect_attribution(original), detect_attribution(candidate)
    hard, manual, evidence = [], [], []
    for item in source:
        name, mode = item["source_text"], item["attribution_mode"]
        matches = [x for x in target if x["source_text"] == name]
        direct = bool(
            re.search(re.escape(name) + rf"(?:の?{TITLE_SURFACE})?によ(?:れば|ると)", candidate)
        )
        if mode == "author_inference_from_source" and (
            direct or any(x["attribution_mode"] == "source_assertion" for x in matches)
        ):
            hard.append("attribution_inference_to_source_assertion")
            decision = "hard_reject"
        elif name not in candidate or (
            mode == "source_assertion"
            and any(x["attribution_mode"] == "author_inference_from_source" for x in matches)
        ):
            manual.append("attribution_mode_uncertain")
            decision = "manual_check"
        else:
            decision = "preserved"
        evidence.append(
            {
                "source": name,
                "original_mode": mode,
                "candidate_modes": [x["attribution_mode"] for x in matches],
                "direct_source_assertion": direct,
                "decision": decision,
            }
        )
    return {
        "original_detected": source,
        "candidate_detected": target,
        "hard_reasons": list(dict.fromkeys(hard)),
        "manual_reasons": list(dict.fromkeys(manual)),
        "evidence": evidence,
        "semantic_equivalence_assessed": False,
    }


def build_title_registry(canonical_html: str) -> tuple[dict[str, dict], list[dict]]:
    """Register only titles with visible bibliographic evidence in node-14 original."""
    detections = []
    root = ET.fromstring(canonical_html)
    for element in root.iter():
        block_id = str(element.attrib.get("id") or "")
        if element.tag != "p" or not block_id:
            continue
        content = "".join(element.itertext()).strip()
        matches = list(QUOTE.finditer(content))
        for index, match in enumerate(matches):
            title = unicodedata.normalize("NFC", match.group(1))
            before = content[max(0, match.start() - 42) : match.start()]
            after = content[match.end() : match.end() + 42]
            author_match = AUTHOR.search(before)
            author = author_match.group(1) if author_match else None
            if author and (len(author) > 12 or author in NON_AUTHOR):
                author = None
            adjacent = (
                index > 0
                and not content[matches[index - 1].end() : match.start()].strip("／/・ 、")
            ) or (
                index + 1 < len(matches)
                and not content[match.end() : matches[index + 1].start()].strip("／/・ 、")
            )
            nearby = content[max(0, match.start() - 35) : min(len(content), match.end() + 55)]
            evidence = []
            if author and ATTRIBUTED.search(after):
                evidence.append("attributed_work")
            if author and before.rstrip().endswith((author, author + "が")):
                evidence.append("author_title_adjacent")
            if adjacent and WORK_CONTEXT.search(nearby):
                evidence.append("parallel_work_list")
            if adjacent and author and "（" in before[-25:]:
                evidence.append("parallel_author_works")
            if re.search(r"^(?:は|が)、?[^。]{0,25}(?:4コマ|漫画|小説|作品)", after):
                evidence.append("explicit_work_description")
            if re.search(r"掲載媒体|月刊誌|週刊誌|青年誌|少年誌|雑誌", before[-25:]) and not author:
                evidence = []
            classification = (
                "bibliographic_work_title_high"
                if evidence
                else "non_bibliographic"
                if NONWORK.search(before[-25:] + after[:20])
                else "title_manual_check"
            )
            detections.append(
                {
                    "title": title,
                    "block_id": block_id,
                    "author": author if evidence else None,
                    "evidence_types": evidence,
                    "classification": classification,
                    "context_before": before[-35:],
                    "context_after": after[:35],
                    "source": "node14_original_manuscript",
                }
            )
    high = {
        x["title"] for x in detections if x["classification"] == "bibliographic_work_title_high"
    }
    for item in detections:
        if item["title"] in high and item["classification"] == "title_manual_check":
            item["classification"] = "bibliographic_work_title_high"
            item["evidence_types"].append("exact_registered_repeat")
    registry = {
        title: {
            "title": title,
            "evidence_types": sorted(
                {v for x in detections if x["title"] == title for v in x["evidence_types"]}
            ),
            "authors": sorted(
                {x["author"] for x in detections if x["title"] == title and x["author"]}
            ),
            "confidence": "high",
        }
        for title in high
    }
    return registry, detections


def title_check(original: str, candidate: str, registry: dict[str, dict]) -> dict[str, Any]:
    source = [unicodedata.normalize("NFC", x) for x in QUOTE.findall(original)]
    target = [unicodedata.normalize("NFC", x) for x in ANY_QUOTE.findall(candidate)]
    evidence, hard, manual = [], [], []
    for title in dict.fromkeys(source):
        if target.count(title) >= source.count(title):
            continue
        record = registry.get(title)
        if record:
            hard.append("protected_bibliographic_work_title_changed")
            decision = "hard_reject"
        else:
            manual.append("bibliographic_title_manual_check")
            decision = "manual_check"
        evidence.append(
            {
                "original_title": title,
                "original_count": source.count(title),
                "candidate_count": target.count(title),
                "decision": decision,
                "registry_evidence_types": record["evidence_types"] if record else [],
            }
        )
    return {
        "hard_reasons": sorted(set(hard)),
        "manual_reasons": sorted(set(manual)),
        "evidence": evidence,
        "scope": "original_node14_bibliographic_registry",
    }


def _new_characters(original: str, candidate: str) -> list[tuple[int, str]]:
    introduced = []
    for op, i, j, k, end_candidate in difflib.SequenceMatcher(
        None, original, candidate, autojunk=False
    ).get_opcodes():
        if op == "equal":
            continue
        remaining = Counter(original[i:j])
        for offset in range(k, end_candidate):
            char = candidate[offset]
            if remaining[char]:
                remaining[char] -= 1
            else:
                introduced.append((offset, char))
    return introduced


def _abnormal_separator(candidate: str, offset: int, char: str) -> bool:
    if not 0 < offset < len(candidate) - 1:
        return False
    before, after = candidate[offset - 1], candidate[offset + 1]
    if before in "。！？\n\r" or after in "。！？\n\r":
        return False
    if unicodedata.category(char) == "Cf":
        return before.isalnum() and after.isalnum()
    inside = any(m.start(1) <= offset < m.end(1) for m in ANY_QUOTE.finditer(candidate))
    if inside and before.isalnum() and after.isalnum():
        return True
    return bool(JAPANESE.fullmatch(before) and JAPANESE.fullmatch(after))


@lru_cache(maxsize=1)
def _mecab_tagger() -> Any:
    import MeCab
    import unidic_lite

    return MeCab.Tagger(f'-d "{unidic_lite.DICDIR}"')


def _token_spans(sentence: str) -> list[tuple[str, str, int, int]]:
    result = []
    node = _mecab_tagger().parseToNode(sentence)
    cursor = 0
    while node:
        if node.surface:
            surface = node.surface
            start = sentence.find(surface, cursor)
            if start < 0:
                raise ValueError("morphological_token_alignment_failed")
            end = start + len(surface)
            pos = node.feature.split(",", 1)[0]
            result.append((surface, pos, start, end))
            cursor = end
        node = node.next
    return result


def lint_sentence(original: str, candidate: str) -> dict[str, Any]:
    evidence: list[dict[str, Any]] = []
    for offset, char in _new_characters(original, candidate):
        category = unicodedata.category(char)
        if (category in {"Zs", "Zl", "Zp", "Cf"} or char in " \t\n\r") and _abnormal_separator(
            candidate, offset, char
        ):
            evidence.append(
                {
                    "type": "newly_inserted_abnormal_whitespace_or_format_char",
                    "category": "abnormal_whitespace_or_format_char",
                    "offset": offset,
                    "span": char,
                    "codepoint": f"U+{ord(char):04X}",
                }
            )
    tokens = _token_spans(candidate)
    for before, after in pairwise(tokens):
        if (
            before[1] == after[1] == "助詞"
            and before[0] == after[0]
            and before[3] == after[2]
            and candidate[before[2] : after[3]] not in original
        ):
            evidence.append(
                {
                    "type": "immediate_particle_duplication",
                    "category": "character_or_token_duplication",
                    "offset": before[2],
                    "span": candidate[before[2] : after[3]],
                }
            )
    markup = (
        ("markdown_bold", r"\*\*[^*\n]+\*\*"),
        ("markdown_fence", r"```"),
        ("backtick", r"`"),
        ("html_tag", r"</?[A-Za-z][A-Za-z0-9]*(?:\s+[^<>]*)?>"),
        ("markdown_heading_or_list", r"(?m)^(?:#{1,6}\s|[-*+]\s)"),
    )
    for kind, pattern in markup:
        for match in re.finditer(pattern, candidate):
            if match.group() not in original:
                evidence.append(
                    {
                        "type": "new_markup_artifact",
                        "subtype": kind,
                        "category": "markup_artifact",
                        "offset": match.start(),
                        "span": match.group(),
                    }
                )
    for match in re.finditer(r"([一-龥々ァ-ヴー]{2,6})\1", candidate):
        if match.group() not in original and match.group() not in JUSTIFIED_REPETITIONS:
            evidence.append(
                {
                    "type": "short_string_duplication",
                    "category": "character_or_token_duplication",
                    "offset": match.start(),
                    "span": match.group(),
                }
            )
    for match in re.finditer(r"([一-龥])\1", candidate):
        if candidate[: match.start() + 1] + candidate[match.end() :] == original:
            evidence.append(
                {
                    "type": "single_character_duplication_with_exact_source_recovery",
                    "category": "character_or_token_duplication",
                    "offset": match.start(),
                    "span": match.group(),
                }
            )
    for match in re.finditer(
        r"(?:する|される|できる|となる|である)りる|るりる|、、|。。", candidate
    ):
        if match.group() not in original:
            evidence.append(
                {
                    "type": "local_inflection_or_connection_break",
                    "category": "punctuation_corruption"
                    if match.group() in {"、、", "。。"}
                    else "morphological_corruption",
                    "offset": match.start(),
                    "span": match.group(),
                }
            )
    unique = {(x["type"], x["offset"], x["span"]): x for x in evidence}
    return {
        "flagged": bool(unique),
        "evidence": list(unique.values()),
        "used_llm": False,
        "tokenizer": "mecab-unidic-lite",
        "analyzer_versions": analyzer_versions(),
    }


def policy_check(original: str, candidate: str, rules: object) -> dict[str, Any]:
    """Only structured policy rules are interpreted; unknown rules fail closed."""
    register_rules = [
        {"type": "register_shift", "from": "plain", "to": "polite", "action": "block"},
        {"type": "register_shift", "from": "polite", "to": "plain", "action": "block"},
    ]
    if rules is None:
        rules = []
    if not isinstance(rules, list):
        return {
            "blocked": True,
            "reasons": ["unsupported_policy_rule"],
            "unsupported_rules": [rules],
        }
    rules = [*register_rules, *rules]
    reasons, unsupported = [], []
    for rule in rules:
        if not isinstance(rule, dict):
            unsupported.append(rule)
            continue
        kind = rule.get("type")
        if kind == "preserve_exact_terms" and isinstance(rule.get("terms"), list):
            if any(
                isinstance(t, str) and t in original and t not in candidate for t in rule["terms"]
            ):
                reasons.append("preserve_exact_terms")
        elif kind == "register_shift" and rule.get("action") == "block":
            polite_pattern = (
                r"(?:ませんでした|ません|でした|ました|です|ます)(?:[。！？!?]|$)"
            )
            original_polite = bool(re.search(polite_pattern, original))
            candidate_polite = bool(re.search(polite_pattern, candidate))
            direction = (rule.get("from"), rule.get("to"))
            if (
                direction == ("plain", "polite")
                and candidate_polite
                and not original_polite
            ) or (
                direction == ("polite", "plain")
                and original_polite
                and not candidate_polite
            ):
                reasons.append("register_shift")
            elif direction not in {("plain", "polite"), ("polite", "plain")}:
                unsupported.append(rule)
        else:
            unsupported.append(rule)
    if unsupported:
        reasons.append("unsupported_policy_rule")
    return {
        "blocked": bool(reasons),
        "reasons": list(dict.fromkeys(reasons)),
        "unsupported_rules": unsupported,
    }


def preprocess_candidate(
    original: str,
    raw_candidate: str,
    registry: dict[str, dict],
    policy_rules: object = None,
) -> dict[str, Any]:
    """Priority: input, policy, hard, manual, lint, then Luna eligibility."""
    if not isinstance(raw_candidate, str) or not raw_candidate.strip():
        return {"status": "input_error", "status_reasons": ["empty_candidate"]}
    quoted = normalize_quotes(original, raw_candidate.strip())
    numeric = numeric_check(original, quoted)
    candidate = numeric["normalized_candidate_sentence"]
    policy = policy_check(original, candidate, policy_rules)
    attribution = attribution_check(original, candidate)
    title = title_check(original, candidate, registry)
    lint = lint_sentence(original, candidate)
    hard = list(
        dict.fromkeys(numeric["hard_reasons"] + attribution["hard_reasons"] + title["hard_reasons"])
    )
    manual = list(
        dict.fromkeys(
            numeric["manual_reasons"] + attribution["manual_reasons"] + title["manual_reasons"]
        )
    )
    if hard:
        status, reasons = "hard_reject", hard
    elif policy["blocked"]:
        status, reasons = "policy_blocked", policy["reasons"]
    elif manual:
        status, reasons = "manual_check", manual
    elif lint["flagged"]:
        status, reasons = "lint_flag", [x["category"] for x in lint["evidence"]]
    else:
        status, reasons = "luna_pending", []
    return {
        "status": status,
        "status_reasons": list(dict.fromkeys(reasons)),
        "normalized_candidate_text": candidate,
        "raw_candidate_text": raw_candidate,
        "candidate_hash": digest(candidate),
        "raw_changed_spans": raw_changed_spans(original, candidate),
        "normalization_changed": candidate != raw_candidate,
        "policy_result": policy,
        "numeric_result": numeric,
        "attribution_result": attribution,
        "bibliographic_title_result": title,
        "lint_result": lint,
    }


def luna_cache_key(
    *,
    model_id: str,
    prompt_hash: str,
    original_hash: str,
    candidate_hash: str,
    raw_changed_spans: list[dict[str, Any]],
) -> tuple[str, str]:
    spans_hash = digest(json.dumps(raw_changed_spans, ensure_ascii=False, sort_keys=True))
    key = digest(
        json.dumps(
            [model_id, prompt_hash, original_hash, candidate_hash, spans_hash],
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return key, spans_hash
