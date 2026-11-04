"""Local, opt-in structured Japanese predicate realization (V3).

This module has no model client, editorial classifier, saved-candidate lookup, or
case-specific repair table. Both the worker and the full-document experiment use
``analyze_source`` / ``extract_lexeme`` / ``build_plans`` / ``realize_plan`` /
``check_completed``. Offsets are Python Unicode code-point offsets.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from functools import lru_cache
from typing import Any

from fukamiru.revisions.japanese_analysis import parse_japanese

PROFILE = "structured-realization-v3"
BASE_GENERATION = {
    "samples_per_case": 8,
    "max_new_tokens": 16,
    "temperature": 0.8,
    "top_p": 0.9,
    "top_k": 40,
    "repeat_penalty": 1.0,
    "cache_prompt": False,
    "seeds": list(range(1001, 1009)),
}
FUNCTIONAL_EXPRESSIONS = (
    "について",
    "に付いて",
    "によって",
    "に依って",
    "において",
    "に於いて",
    "に関して",
    "に対して",
    "として",
    "にあたって",
)
PROTECTED_TERMS = ("編成", "配分", "反復", "変換", "区分", "検討")
PROTECTED_COMPOUNDS = ("相反する",)
FORMALIZATION_MARKERS = (
    "不可能である",
    "包含",
    "称する",
    "有する",
    "選定",
    "於け",
    "ならぬ",
    "可能ならしめ",
)
ORTHOGRAPHY_PAIRS = {
    ("いる", "居る"),
    ("いた", "居た"),
    ("ある", "有る"),
    ("なる", "成る"),
    ("おける", "於ける"),
}
GODAN_I = dict(zip("うくぐすつぬぶむる", "いきぎしちにびみり", strict=True))
GODAN_A = dict(zip("うくぐすつぬぶむる", "わかがさたなばまら", strict=True))
GODAN_E = dict(zip("うくぐすつぬぶむる", "えけげせてねべめれ", strict=True))
GODAN_TE_PAST = {
    "う": ("って", "った"),
    "つ": ("って", "った"),
    "る": ("って", "った"),
    "む": ("んで", "んだ"),
    "ぶ": ("んで", "んだ"),
    "ぬ": ("んで", "んだ"),
    "く": ("いて", "いた"),
    "ぐ": ("いで", "いだ"),
    "す": ("して", "した"),
}
_MECAB_LOCK = threading.RLock()


class StructureUnsupported(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def stable_id(prefix: str, *parts: Any) -> str:
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return prefix + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def generation_payload(prefix: str, seed: int, settings: dict[str, Any]) -> dict[str, Any]:
    """Plain causal continuation; no editing instruction or external provider."""
    return {
        "prompt": prefix,
        "n_predict": settings["max_new_tokens"],
        "temperature": settings["temperature"],
        "top_p": settings["top_p"],
        "top_k": settings["top_k"],
        "min_p": 0.0,
        "repeat_penalty": settings["repeat_penalty"],
        "presence_penalty": 0.0,
        "frequency_penalty": 0.0,
        "dry_multiplier": 0.0,
        "cache_prompt": settings["cache_prompt"],
        "return_tokens": True,
        "seed": seed,
        "stream": False,
    }


@lru_cache(maxsize=1)
def _tagger() -> Any:
    import MeCab
    import unidic_lite

    return MeCab.Tagger(f'-d "{unidic_lite.DICDIR}"')


@lru_cache(maxsize=8192)
def mecab_tokens(text: str) -> tuple[dict[str, Any], ...]:
    """Fast local morphology for raw continuations; never normalizes manuscript text."""
    with _MECAB_LOCK:
        node = _tagger().parseToNode(text)
        cursor = 0
        result: list[dict[str, Any]] = []
        while node:
            if node.surface:
                start = text.find(node.surface, cursor)
                if start < 0:
                    raise StructureUnsupported("mecab_offset_mismatch")
                fields = node.feature.split(",")
                result.append(
                    {
                        "text": node.surface,
                        "start": start,
                        "end": start + len(node.surface),
                        "pos": fields[:4],
                        "inflection": fields[4:6],
                        "lemma": fields[7] if len(fields) > 7 else node.surface,
                    }
                )
                cursor = start + len(node.surface)
            node = node.next
    if any(text[row["start"] : row["end"]] != row["text"] for row in result):
        raise StructureUnsupported("mecab_roundtrip_failed")
    return tuple(result)


def _pos(token: dict[str, Any]) -> str:
    return str((token.get("pos") or [""])[0])


def _form(token: dict[str, Any]) -> str:
    return str((token.get("inflection") or ["", ""])[1])


def _class(token: dict[str, Any]) -> str:
    value = str((token.get("inflection") or [""])[0])
    if value.startswith("五段"):
        return "godan"
    if value.startswith(("上一段", "下一段")):
        return "ichidan"
    if "サ行変格" in value:
        return "suru"
    if "カ行変格" in value:
        return "kuru"
    raise StructureUnsupported("inflection_class_unsupported:" + value)


def _is_sahen(token: dict[str, Any]) -> bool:
    return _pos(token) == "名詞" and "サ変可能" in token.get("pos", [])


def _godan(base: str, row: dict[str, str]) -> str:
    if not base or base[-1] not in row:
        raise StructureUnsupported("godan_base_unavailable:" + base)
    return base[:-1] + row[base[-1]]


def _potential_base(base: str, kind: str, construction: str) -> tuple[str, str]:
    if kind == "godan":
        return _godan(base, GODAN_E) + "る", "ichidan"
    if kind == "ichidan":
        return base[:-1] + "られる", "ichidan"
    if kind == "suru" and construction == "sahen":
        return base[:-2] + "できる", "ichidan"
    if kind == "kuru":
        return ("来られる" if base == "来る" else "こられる"), "ichidan"
    raise StructureUnsupported("potential_class_or_construction_unsupported:" + kind)


def _passive_base(base: str, kind: str, construction: str) -> tuple[str, str]:
    if kind == "godan":
        return _godan(base, GODAN_A) + "れる", "ichidan"
    if kind == "ichidan":
        return base[:-1] + "られる", "ichidan"
    if kind == "suru" and construction == "sahen":
        return base[:-2] + "される", "ichidan"
    if kind == "kuru":
        return ("来られる" if base == "来る" else "こられる"), "ichidan"
    raise StructureUnsupported("passive_class_or_construction_unsupported:" + kind)


def _inflect(base: str, kind: str, form: str) -> str:
    if form == "nonpast":
        return base
    if kind == "godan":
        if form == "renyou":
            return _godan(base, GODAN_I)
        if form == "mizen":
            return _godan(base, GODAN_A)
        if form in {"te", "past"}:
            if base.endswith("行く") or base == "いく":
                return base[:-1] + ("って" if form == "te" else "った")
            pair = GODAN_TE_PAST.get(base[-1:])
            if pair is None:
                raise StructureUnsupported("godan_onbin_unavailable")
            return base[:-1] + pair[0 if form == "te" else 1]
    if kind == "ichidan":
        if not base.endswith("る"):
            raise StructureUnsupported("ichidan_base_unavailable")
        stem = base[:-1]
        return stem + {"renyou": "", "mizen": "", "te": "て", "past": "た"}[form]
    if kind == "suru":
        if not base.endswith("する"):
            raise StructureUnsupported("suru_base_unavailable")
        stem = base[:-2]
        return stem + {"renyou": "し", "mizen": "し", "te": "して", "past": "した"}[form]
    if kind == "kuru":
        if base not in {"くる", "来る"}:
            raise StructureUnsupported("kuru_base_unavailable")
        stem = "来" if base == "来る" else "き"
        if form == "mizen":
            return "来" if base == "来る" else "こ"
        return stem + {"renyou": "", "te": "て", "past": "た"}[form]
    raise StructureUnsupported("inflection_unsupported:" + kind + ":" + form)


def _base_from_token(token: dict[str, Any]) -> tuple[str, str, list[str], dict[str, Any]]:
    """Keep actual inflecting base distinct from UniDic's lexical lemma."""
    kind = _class(token)
    surface = str(token["text"])
    lemma = str(token.get("lemma") or "")
    form = _form(token)
    if kind == "suru":
        if surface.endswith("する"):
            base = surface
        elif lemma.endswith("する") and surface.startswith(lemma[:-2]):
            base = lemma
        elif surface in {"し", "さ", "すれ", "せ"}:
            base = "する"
        else:
            raise StructureUnsupported("suru_base_not_recoverable")
        return base, kind, [], {"rule": "suru_surface_or_lemma", "form": form}
    if kind == "kuru":
        base = "来る" if "来" in surface else "くる"
        return base, kind, [], {"rule": "kuru_surface_script", "form": form}
    if kind == "ichidan":
        base = (
            surface
            if surface.endswith("る") and form.startswith(("終止", "連体"))
            else surface + "る"
        )
        if lemma and lemma[-1:] in GODAN_E and _potential_base(lemma, "godan", "single")[0] == base:
            return (
                lemma,
                "godan",
                ["potential"],
                {
                    "rule": "derived_godan_potential_attested_by_lemma_and_surface",
                    "inflecting_base": base,
                    "form": form,
                },
            )
        return base, kind, [], {"rule": "ichidan_observed_stem", "form": form}
    if kind == "godan":
        if form.startswith(("終止", "連体")) and surface[-1:] in GODAN_I:
            base = surface
        elif form.startswith("連用形-一般") and surface[-1:] in GODAN_I.values():
            inverse = {value: key for key, value in GODAN_I.items()}
            base = surface[:-1] + inverse[surface[-1]]
        elif lemma and lemma[-1:] in GODAN_I:
            # UniDic's lemma can use a different script from the manuscript
            # (いった/言った, もって/持って).  Derive the inflecting base from
            # the attested stem while using the lemma only for its final row.
            base = surface[:-1] + lemma[-1]
        else:
            raise StructureUnsupported("godan_lemma_or_surface_unavailable")
        return base, kind, [], {"rule": "godan_onbin_or_lemma", "form": form}
    raise StructureUnsupported("base_class_unavailable")


def _lexeme_at(
    tokens: tuple[dict[str, Any], ...], index: int, text: str
) -> tuple[dict[str, Any], int]:
    if index >= len(tokens):
        raise StructureUnsupported("lexeme_start_outside_tokens")
    first = tokens[index]
    start = int(first["start"])
    if _is_sahen(first) and index + 1 < len(tokens):
        second = tokens[index + 1]
        if second["start"] == first["end"] and _pos(second) == "動詞":
            if str(second.get("lemma")) in {"為る", "する"}:
                return {
                    "lexical_identity": str(first.get("lemma") or first["text"]) + "する",
                    "dictionary_lemma": str(first.get("lemma") or first["text"]) + "する",
                    "inflecting_base": str(first["text"]) + "する",
                    "inflection_class": "suru",
                    "construction": "sahen",
                    "compound_structure": [{"kind": "sahen_noun", "surface": first["text"]}],
                    "derivation_tree": None,
                    "inherent_functions": [],
                    "surface_variants": [first["text"] + "する"],
                    "raw_surface": text[start : second["end"]],
                    "dictionary_evidence": [first, second],
                }, index + 2
            if str(second.get("lemma")) in {"出来る", "できる"}:
                return {
                    "lexical_identity": str(first.get("lemma") or first["text"]) + "する",
                    "dictionary_lemma": str(first.get("lemma") or first["text"]) + "する",
                    "inflecting_base": str(first["text"]) + "する",
                    "inflection_class": "suru",
                    "construction": "sahen",
                    "compound_structure": [{"kind": "sahen_noun", "surface": first["text"]}],
                    "derivation_tree": {"operator": "potential", "surface": second["text"]},
                    "inherent_functions": ["potential"],
                    "potential_surface_variant": "出来"
                    if second["text"].startswith("出来")
                    else "でき",
                    "surface_variants": [first["text"] + "できる", first["text"] + "出来る"],
                    "raw_surface": text[start : second["end"]],
                    "dictionary_evidence": [first, second],
                }, index + 2
        if (
            index + 3 < len(tokens)
            and tokens[index + 1]["text"] == "可能"
            and tokens[index + 2]["text"] == "に"
            and str(tokens[index + 3].get("lemma")) in {"為る", "する"}
        ):
            return {
                "lexical_identity": str(first.get("lemma") or first["text"]) + "する",
                "dictionary_lemma": str(first.get("lemma") or first["text"]) + "する",
                "inflecting_base": str(first["text"]) + "する",
                "inflection_class": "suru",
                "construction": "sahen",
                "compound_structure": [{"kind": "sahen_noun", "surface": first["text"]}],
                "derivation_tree": {"operator": "enable", "surface": "可能に"},
                "inherent_functions": ["enable"],
                "surface_variants": [first["text"] + "可能にする"],
                "raw_surface": text[start : tokens[index + 3]["end"]],
                "dictionary_evidence": list(tokens[index : index + 4]),
            }, index + 4
    if _pos(first) == "名詞" and index + 1 < len(tokens):
        second = tokens[index + 1]
        if second["start"] == first["end"] and _pos(second) == "動詞":
            base, kind, inherent, derivation = _base_from_token(second)
            return {
                "lexical_identity": str(first.get("lemma") or first["text"])
                + str(second.get("lemma") or base),
                "dictionary_lemma": str(first.get("lemma") or first["text"])
                + str(second.get("lemma") or base),
                "inflecting_base": first["text"] + base,
                "inflection_class": kind,
                "construction": "noun_verb_compound",
                "compound_structure": [
                    {"kind": "noun_prefix", "surface": first["text"]},
                    {"kind": "verb", "base": base, "class": kind},
                ],
                "derivation_tree": derivation if inherent else None,
                "inherent_functions": inherent,
                "surface_variants": [first["text"] + base],
                "raw_surface": text[start : second["end"]],
                "dictionary_evidence": [first, second],
            }, index + 2
    if _pos(first) != "動詞":
        raise StructureUnsupported("non_predicate_at_lexical_slot")
    base, kind, inherent, derivation = _base_from_token(first)
    if index + 2 < len(tokens) and tokens[index + 1]["text"] in {"て", "で"}:
        auxiliary = tokens[index + 2]
        if auxiliary["start"] == tokens[index + 1]["end"] and _class_safe(auxiliary) == "kuru":
            first_te = _inflect(base, kind, "te")
            observed = text[first["start"] : tokens[index + 1]["end"]]
            if first_te != observed:
                raise StructureUnsupported("te_compound_first_component_roundtrip")
            second_base, _, _, second_derivation = _base_from_token(auxiliary)
            return {
                "lexical_identity": str(first.get("lemma") or base)
                + "+te+"
                + str(auxiliary.get("lemma") or second_base),
                "dictionary_lemma": str(first.get("lemma") or base)
                + "て"
                + str(auxiliary.get("lemma") or second_base),
                "inflecting_base": observed + second_base,
                "inflection_class": "kuru",
                "construction": "te_compound_kuru",
                "compound_structure": [
                    {"kind": "verb", "base": base, "class": kind, "observed_te": observed},
                    {"kind": "auxiliary_kuru", "base": second_base},
                ],
                "derivation_tree": [derivation, second_derivation],
                "inherent_functions": inherent,
                "surface_variants": [observed + second_base],
                "raw_surface": text[start : auxiliary["end"]],
                "dictionary_evidence": list(tokens[index : index + 3]),
            }, index + 3
    construction = "suru_bound" if kind == "suru" and base != "する" else "single"
    return {
        "lexical_identity": str(first.get("lemma") or base),
        "dictionary_lemma": str(first.get("lemma") or base),
        "inflecting_base": base,
        "inflection_class": kind,
        "construction": construction,
        "compound_structure": [{"kind": "verb", "base": base, "class": kind}],
        "derivation_tree": derivation if inherent else None,
        "inherent_functions": inherent,
        "surface_variants": [base],
        "raw_surface": text[start : first["end"]],
        "dictionary_evidence": [first],
    }, index + 1


def _class_safe(token: dict[str, Any]) -> str | None:
    try:
        return _class(token)
    except StructureUnsupported:
        return None


def _functional_overlap(sentence: str, start: int, end: int) -> dict[str, Any] | None:
    for expression in FUNCTIONAL_EXPRESSIONS:
        cursor = 0
        while (index := sentence.find(expression, cursor)) >= 0:
            if index < end and start < index + len(expression):
                return {
                    "expression": expression,
                    "span": [index, index + len(expression)],
                    "rule_id": "functional_expression_whole_unit_preserved",
                }
            cursor = index + 1
    return None


def _inside_quote(text: str, start: int) -> bool:
    pairs = {"「": "」", "『": "』"}
    stack: list[str] = []
    for char in text[:start]:
        if char in pairs:
            stack.append(pairs[char])
        elif stack and char == stack[-1]:
            stack.pop()
    return bool(stack)


def _source_start_index(
    tokens: tuple[dict[str, Any], ...], target_start: int, target_end: int
) -> int:
    overlapping = [
        index
        for index, token in enumerate(tokens)
        if int(token["start"]) < target_end and int(token["end"]) > target_start
    ]
    if not overlapping:
        raise StructureUnsupported("target_has_no_mecab_tokens")
    candidates = [index for index in overlapping if _pos(tokens[index]) == "動詞"]
    if not candidates:
        # A frame may point at an auxiliary fragment; backtrack only through a
        # contiguous local chain, never to a verb in another clause.
        first = overlapping[0]
        for index in range(first - 1, max(-1, first - 5), -1):
            if tokens[index]["end"] != tokens[index + 1]["start"]:
                break
            if _pos(tokens[index]) == "動詞":
                candidates = [index]
                break
    if not candidates:
        raise StructureUnsupported("target_not_verbal")
    index = candidates[0]
    verb = tokens[index]
    # Recover a lexical root when the frame happened to point at a light
    # auxiliary or only the inflected head of a larger predicate.
    if (
        index >= 2
        and tokens[index - 1]["text"] in {"て", "で"}
        and _pos(tokens[index - 2]) == "動詞"
        and verb["start"] == tokens[index - 1]["end"]
    ):
        index -= 2
    if (
        index >= 3
        and str(tokens[index].get("lemma")) in {"為る", "する"}
        and tokens[index - 1]["text"] == "に"
        and tokens[index - 2]["text"] == "可能"
        and _is_sahen(tokens[index - 3])
    ):
        return index - 3
    if (
        index > 0
        and _is_sahen(tokens[index - 1])
        and tokens[index - 1]["end"] == verb["start"]
        and str(verb.get("lemma")) in {"為る", "する", "出来る", "できる"}
    ):
        return index - 1
    return index


def _extend_source_end(tokens: tuple[dict[str, Any], ...], consumed: int, target_end: int) -> int:
    if consumed <= 0:
        raise StructureUnsupported("empty_lexical_head")
    end = int(tokens[consumed - 1]["end"])
    index = consumed
    while index < len(tokens) and tokens[index]["start"] == end:
        token = tokens[index]
        text = str(token["text"])
        if _pos(token) == "助動詞" and text not in {"に"}:
            end = int(token["end"])
            index += 1
            continue
        if text in {"て", "で"} and _pos(token) == "助詞":
            end = int(token["end"])
            index += 1
            if (
                index < len(tokens)
                and tokens[index]["start"] == end
                and tokens[index]["text"] in {"いる", "い", "いた", "くる", "来る"}
            ):
                end = int(tokens[index]["end"])
                index += 1
            continue
        if text in {"うる", "得る"} and _pos(token) == "動詞":
            end = int(token["end"])
            index += 1
            continue
        if text == "よう" and index + 2 < len(tokens):
            trio = tokens[index : index + 3]
            if [item["text"] for item in trio] == ["よう", "に", "なる"]:
                end = int(trio[-1]["end"])
                index += 3
                continue
        if text == "も" and end < target_end:
            end = int(token["end"])
            index += 1
            continue
        break
    if target_end > end:
        matching = next((row for row in tokens if row["start"] < target_end <= row["end"]), None)
        end = int(matching["end"]) if matching else target_end
    return end


def _operator_sequences(
    lexical: dict[str, Any], outer: str, source_form: str, *, epistemic: bool
) -> tuple[list[dict[str, Any]], str]:
    intrinsic = list(lexical["inherent_functions"])
    if intrinsic == ["enable"]:
        intrinsic = ["enable"]
    if outer in {"", "た", "だ"}:
        operators = intrinsic + (["past"] if outer in {"た", "だ"} else [])
        connection = "renyou" if not outer and source_form.startswith("連用形") else "nonpast"
        return [
            {"id": "observed", "operators": operators, "evidence": "source_morphology"}
        ], connection
    if outer in {"て", "で"}:
        return [
            {"id": "observed", "operators": intrinsic, "evidence": "source_te_connection"}
        ], "te"
    if outer in {"ない", "なかった"}:
        return [
            {
                "id": "observed",
                "operators": intrinsic + ["negative"] + (["past"] if outer == "なかった" else []),
                "evidence": "source_negative_auxiliary",
            }
        ], "nonpast"
    if outer in {"なくても", "なくては"}:
        return [
            {
                "id": "observed",
                "operators": intrinsic + ["negative_concessive"],
                "evidence": "source_negative_concessive",
            }
        ], "nonpast"
    if outer in {"ている", "でいる", "ていた", "でいた"}:
        return [
            {
                "id": "observed",
                "operators": intrinsic + ["teiru"] + (["past"] if outer.endswith("いた") else []),
                "evidence": "source_teiru",
            }
        ], "nonpast"
    if outer in {"うる", "得る"}:
        return [
            {"id": "observed", "operators": intrinsic + ["uru"], "evidence": "source_renyou_uru"}
        ], "nonpast"
    voice = None
    tail = outer
    if tail.startswith("られ"):
        voice, tail = "rareru", tail[2:]
    elif tail.startswith("れ"):
        voice, tail = "reru", tail[1:]
    if voice and tail in {"る", "た", "るようになる"}:
        past = tail == "た"
        become = tail.endswith("ようになる")
        kind = lexical["inflection_class"]
        if epistemic:
            names = ["epistemic_frame"]
        elif kind in {"ichidan", "kuru"} and voice == "rareru":
            names = ["passive", "potential"]
        elif kind in {"godan", "suru"} and voice == "reru":
            names = ["passive"]
        else:
            raise StructureUnsupported("voice_form_and_class_conflict")
        return [
            {
                "id": name,
                "operators": intrinsic
                + [name]
                + (["past"] if past else [])
                + (["become"] if become else []),
                "evidence": "source_reru_morphology"
                if name != "epistemic_frame"
                else "to_complement_epistemic_construction",
            }
            for name in names
        ], "nonpast"
    raise StructureUnsupported("source_operator_chain_unsupported:" + outer)


def _argument_links(
    analysis: dict[str, Any], sentence: str, predicate_start: int, edit_end: int
) -> list[dict[str, Any]]:
    dependencies = list(analysis.get("dependencies") or [])
    links = []
    for row in analysis.get("arguments") or []:
        governor = int(row.get("predicate_start", -1))
        if not predicate_start <= governor < edit_end:
            continue
        start = int(row.get("start", -1))
        token = next((entry for entry in dependencies if entry["start"] == start), None)
        if token is None:
            continue
        following = sentence[token["end"] : token["end"] + 2]
        particle = next(
            (
                value
                for value in ("から", "より", "が", "は", "を", "に", "へ", "で", "と")
                if following.startswith(value)
            ),
            None,
        )
        links.append(
            {
                "dependent_phrase_span": [start, token["end"]],
                "head_token": token["text"],
                "particle_or_marker": particle,
                "governor_predicate_start": governor,
                "clause_id": sentence[:start].count("、") + sentence[:start].count("。"),
                "syntactic_relation": row.get("role"),
                "observed_or_inferred": "observed_dependency",
                "possible_semantic_roles": None,
                "role_evidence": "GiNZA dependency; semantic role not inferred",
            }
        )
    return links


def _tree(operators: list[str]) -> dict[str, Any]:
    node: dict[str, Any] = {"operator": "lexeme"}
    for operator in operators:
        node = {"operator": operator, "scope": node}
    return node


def analyze_source(
    sentence: str,
    target_start: int,
    target_end: int,
    *,
    frame_id: str,
    source_snapshot_sha256: str | None = None,
) -> dict[str, Any]:
    """Build a local source construction from manuscript text and one frame anchor."""
    source: dict[str, Any] = {
        "frame_id": frame_id,
        "source_sentence": sentence,
        "source_hash": hashlib.sha256(sentence.encode("utf-8")).hexdigest(),
        "source_snapshot_sha256": source_snapshot_sha256,
        "analysis_span": [0, len(sentence)],
        "old_target_span": [target_start, target_end],
        "status": "pending",
        "reason": None,
        "semantic_preservation": None,
    }
    if not (0 <= target_start < target_end <= len(sentence)):
        source.update(status="not_constructible", reason="source_anchor_out_of_bounds")
        return source
    source["old_target_text"] = sentence[target_start:target_end]
    functional = _functional_overlap(sentence, target_start, target_end)
    if functional:
        source.update(
            status="functional_expression_preserved",
            reason=functional["rule_id"],
            construction_kind="functional_expression",
            edit_span=functional["span"],
            lexical_slot_span=None,
            source_expression=functional["expression"],
            P=sentence[: functional["span"][0]],
            R=sentence[functional["span"][1] :],
            protected_spans=[functional],
            operator_tree={"operator": "functional_expression_preserved"},
            argument_links=[],
            interpretation_candidates=[],
        )
        return source
    if _inside_quote(sentence, target_start):
        source.update(status="protected_quotation", reason="target_inside_quotation")
        return source
    try:
        analysis = parse_japanese(sentence)
        tokens = mecab_tokens(sentence)
        if not analysis.get("dependencies"):
            raise StructureUnsupported("ginza_dependencies_unavailable")
        start_index = _source_start_index(tokens, target_start, target_end)
        first = tokens[start_index]
        if (
            start_index >= 2
            and first["text"] in {"する", "し", "さ"}
            and tokens[start_index - 1]["text"] in {"に", "と"}
            and _pos(tokens[start_index - 2]) in {"名詞", "形状詞"}
            and not _is_sahen(tokens[start_index - 2])
        ):
            raise StructureUnsupported("light_verb_complement_requires_whole_construction")
        lexical, consumed = _lexeme_at(tokens, start_index, sentence)
        start = int(first["start"])
        end = _extend_source_end(tokens, consumed, target_end)
        lexical_end = int(tokens[consumed - 1]["end"])
        if end < target_end or start > target_start:
            raise StructureUnsupported("source_anchor_not_covered_by_edit")
        expression = sentence[start:end]
        outer = sentence[lexical_end:end]
        final_lexical_form = _form(tokens[consumed - 1])
        epistemic = (
            lexical["lexical_identity"] in {"考える", "考えられる"}
            and sentence[max(0, start - 1) : start] == "と"
            and outer.startswith("られ")
        )
        interpretations, connection = _operator_sequences(
            lexical, outer, final_lexical_form, epistemic=epistemic
        )
        links = _argument_links(analysis, sentence, start, end)
        external_operators = []
        if re.match(r"(?:こと[がは]でき(?:る|ない)|ことが可能)", sentence[end:]):
            external_operators.append("potential")
        if sentence[end:].startswith("と考えられる"):
            external_operators.append("epistemic_frame")
        source.update(
            status="constructed",
            reason=None,
            construction_kind="epistemic_frame" if epistemic else lexical["construction"],
            clause_spans=[{"start": 0, "end": len(sentence), "evidence": "sentence"}],
            edit_span=[start, end],
            lexical_slot_span=[start, lexical_end],
            generation_prefix_end=start,
            P=sentence[:start],
            E0=expression,
            R=sentence[end:],
            source_expression=expression,
            source_lexeme=lexical,
            source_outer_surface=outer,
            operator_tree=[_tree(item["operators"]) for item in interpretations],
            outer_connection=connection,
            argument_links=links,
            external_operators=external_operators,
            interpretation_candidates=interpretations,
            protected_spans=[],
            evidence={
                "mecab_source_tokens": list(tokens[start_index:]),
                "ginza_target_dependencies": [
                    item for item in analysis["dependencies"] if start <= item["start"] < end
                ],
                "boundary_rules": ["frame_anchor", "local_lexeme", "contiguous_auxiliary_chain"],
            },
        )
        if source["P"] + source["E0"] + source["R"] != sentence:
            raise StructureUnsupported("source_edit_roundtrip_failed")
        if not any(
            realize_plan(
                {
                    "operators": item["operators"],
                    "outer_connection": connection,
                    "source_lexical_identity": lexical["lexical_identity"],
                },
                lexical,
            )
            == expression
            for item in interpretations
        ):
            raise StructureUnsupported("source_reinflection_roundtrip_failed")
    except StructureUnsupported as exc:
        source.update(status="not_constructible", reason=exc.reason)
    return source


def extract_lexeme(
    prefix: str, raw_text: str, *, generated_token_count: int, max_new_tokens: int = 16
) -> dict[str, Any]:
    """Extract only the first causal-continuation lexical predicate, never its advice."""
    result: dict[str, Any] = {
        "status": "pending",
        "reason": None,
        "raw_text": raw_text,
        "prefix_sha256": hashlib.sha256(prefix.encode("utf-8")).hexdigest(),
        "lexeme": None,
        "raw_grammatical_features": None,
        "generation_span": None,
    }
    if not raw_text or raw_text[0] in "、。！？!?「『（(\r\n":
        result.update(status="candidate_extraction_failed", reason="empty_or_punctuation_start")
        return result
    combined = prefix + raw_text
    boundary = len(prefix)
    try:
        tokens = mecab_tokens(combined)
        start = boundary + len(raw_text) - len(raw_text.lstrip(" 　"))
        if any(row["start"] < start < row["end"] for row in tokens):
            raise StructureUnsupported("generation_boundary_crosses_morpheme")
        first = next((i for i, row in enumerate(tokens) if row["start"] == start), None)
        if first is None:
            raise StructureUnsupported("generation_start_unaligned")
        if (
            _pos(tokens[first]) == "副詞"
            and first + 1 < len(tokens)
            and tokens[first + 1]["start"] == tokens[first]["end"]
        ):
            result["leading_modifier"] = tokens[first]["text"]
            first += 1
            start = int(tokens[first]["start"])
        if _pos(tokens[first]) in {"助詞", "助動詞", "補助記号"}:
            raise StructureUnsupported("non_predicate_at_generation_start")
        if (
            _pos(tokens[first]) == "名詞"
            and first + 1 < len(tokens)
            and tokens[first + 1]["text"] in {"を", "が", "に", "へ", "で", "から"}
        ):
            raise StructureUnsupported("new_argument_before_predicate")
        lexeme, consumed = _lexeme_at(tokens, first, combined)
        lexical_end = int(tokens[consumed - 1]["end"])
        if lexical_end == len(combined) and generated_token_count >= max_new_tokens:
            raise StructureUnsupported("truncated_lexeme_without_visible_boundary")
        if any(character in combined[start:lexical_end] for character in "。！？!?\r\n\t"):
            raise StructureUnsupported("lexeme_contains_sentence_boundary")
        result.update(
            status="extracted",
            lexeme=lexeme,
            generation_span=[start - boundary, lexical_end - boundary],
            raw_grammatical_features={
                "following_surface": combined[lexical_end : min(len(combined), lexical_end + 16)],
                "consumed_mecab_tokens": list(tokens[first:consumed]),
                "raw_morphology_is_not_source_operator_contract": True,
            },
        )
    except StructureUnsupported as exc:
        result.update(status="candidate_extraction_failed", reason=exc.reason)
    except UnicodeError:
        result.update(status="candidate_extraction_failed", reason="raw_unicode_decoding_invalid")
    return result


def _split_compound_head(lexeme: dict[str, Any]) -> tuple[str, str, str]:
    base = str(lexeme["inflecting_base"])
    kind = str(lexeme["inflection_class"])
    if lexeme["construction"] == "te_compound_kuru":
        prefix = str(lexeme["compound_structure"][0]["observed_te"])
        head = base[len(prefix) :]
        if head not in {"くる", "来る"}:
            raise StructureUnsupported("compound_kuru_head_missing")
        return prefix, head, kind
    return "", base, kind


def _negative(base: str, kind: str) -> str:
    if kind == "godan":
        return _inflect(base, kind, "mizen") + "ない"
    if kind == "ichidan":
        return base[:-1] + "ない"
    if kind == "suru":
        return base[:-2] + "しない"
    if kind == "kuru":
        return "来ない" if base == "来る" else "こない"
    raise StructureUnsupported("negative_class_unsupported:" + kind)


def realize_plan(plan: dict[str, Any], lexeme: dict[str, Any]) -> str:
    """Compose a completed edit phrase from an ordered operator plan."""
    prefix, base, kind = _split_compound_head(lexeme)
    construction = str(lexeme["construction"])
    operators = list(plan["operators"])
    if "enable" in operators and (construction != "sahen" or operators[0] != "enable"):
        raise StructureUnsupported("enable_requires_sahen_lexical_slot")
    for operator in operators:
        if operator == "potential":
            base, kind = _potential_base(base, kind, construction)
            if construction == "sahen" and lexeme.get("potential_surface_variant") == "出来":
                base = base[:-3] + "出来る"
        elif operator == "passive":
            base, kind = _passive_base(base, kind, construction)
        elif operator == "epistemic_frame":
            if lexeme["lexical_identity"] != plan.get("source_lexical_identity"):
                raise StructureUnsupported("epistemic_lexical_frame_mapping_unavailable")
            base, kind = _passive_base(base, kind, construction)
        elif operator == "past":
            if base.endswith("ない"):
                base = base[:-2] + "なかった"
            elif base.endswith("いる") and plan.get("previous_operator") == "teiru":
                base = base[:-2] + "いた"
            elif base.endswith("ようになる"):
                base = base[:-2] + "なった"
            else:
                base = _inflect(base, kind, "past")
        elif operator == "negative":
            base, kind = _negative(base, kind), "negative_auxiliary"
        elif operator == "negative_concessive":
            base = _negative(base, kind)[:-1] + "くても"
            kind = "fixed"
        elif operator == "teiru":
            base, kind = _inflect(base, kind, "te") + "いる", "ichidan"
            plan = {**plan, "previous_operator": "teiru"}
        elif operator == "uru":
            base, kind = _inflect(base, kind, "renyou") + "うる", "ichidan"
        elif operator == "become":
            base, kind = base + "ようになる", "fixed"
        elif operator == "enable":
            base, kind = base[:-2] + "可能にする", "suru"
        else:
            raise StructureUnsupported("operator_not_implemented:" + operator)
    connection = plan.get("outer_connection") or "nonpast"
    if connection == "renyou" and (not operators or operators[-1] in {"potential", "passive"}):
        base = _inflect(base, kind, "renyou")
    elif connection == "te" and (not operators or operators[-1] in {"potential", "passive"}):
        base = _inflect(base, kind, "te")
    elif connection not in {"nonpast", "renyou", "te"}:
        raise StructureUnsupported("outer_connection_not_implemented:" + str(connection))
    return prefix + base


def build_plans(source: dict[str, Any], lexeme: dict[str, Any]) -> list[dict[str, Any]]:
    if source["status"] != "constructed":
        return [{"status": "held", "reason": "source_not_constructed", "operators": []}]
    source_ops = source["interpretation_candidates"]
    intrinsic = set(lexeme["inherent_functions"])
    plans = []
    for interpretation in source_ops:
        operators = list(interpretation["operators"])
        notes: list[str] = []
        blocked = None
        if "teiru" in operators and lexeme["inflecting_base"] in {
            "いる",
            "居る",
            "ある",
            "有る",
            "在る",
        }:
            blocked = "existential_lexeme_would_double_aspect"
        for function in intrinsic:
            if function not in operators:
                blocked = "candidate_adds_unlicensed_operator:" + function
        external = source.get("external_operators") or []
        if "potential" in intrinsic and "potential" in external:
            blocked = "candidate_duplicates_external_potential"
        if source["construction_kind"] == "epistemic_frame":
            notes.append("epistemic_judgment_content_requires_review")
        if len(source["interpretation_candidates"]) > 1:
            notes.append("source_voice_interpretation_unresolved")
        if source["argument_links"]:
            notes.append("candidate_dictionary_valency_not_available; observed_arguments_unchanged")
        else:
            notes.append("no_target_attached_argument_observed")
        plan = {
            "plan_id": stable_id(
                "plan-v3-",
                source["frame_id"],
                lexeme["lexical_identity"],
                interpretation["id"],
                operators,
            ),
            "source_construction_id": source["frame_id"],
            "candidate_lexeme_id": stable_id(
                "lex-v3-",
                lexeme["lexical_identity"],
                lexeme["inflecting_base"],
                lexeme["construction"],
            ),
            "supported_source_interpretation": interpretation["id"],
            "target_construction": source["construction_kind"],
            "operators": operators,
            "operator_alignment": [
                {
                    "source_operator": operator,
                    "candidate_realization": "existing_derivation_aligned_once"
                    if operator in intrinsic
                    else "morphology_rule",
                }
                for operator in operators
            ],
            "argument_alignment": [
                {
                    "source_link": link,
                    "candidate_particle": link["particle_or_marker"],
                    "semantic_role": None,
                    "status": "surface_relation_preserved; lexical_valency_unverified",
                }
                for link in source["argument_links"]
            ],
            "morphology_steps": operators + [source["outer_connection"]],
            "fixed_material": {"P": source["P"], "R": source["R"]},
            "outer_connection": source["outer_connection"],
            "source_lexical_identity": source["source_lexeme"]["lexical_identity"],
            "evidence": {
                "source_interpretation": interpretation,
                "candidate_dictionary": lexeme["dictionary_evidence"],
            },
            "unresolved_content_notes": notes,
            "status": "held" if blocked else "planned",
            "reason": blocked,
        }
        if not blocked:
            try:
                plan["expression"] = realize_plan(plan, lexeme)
            except StructureUnsupported as exc:
                plan.update(status="held", reason=exc.reason)
        plans.append(plan)
    return plans


def _rule_a_reasons(source: dict[str, Any], expression: str) -> list[str]:
    original = str(source["E0"])
    lemma = str(source["source_lexeme"]["dictionary_lemma"])
    start = int(source["edit_span"][0])
    sentence = str(source["source_sentence"])
    reasons = []
    if any(term in original or term == lemma for term in PROTECTED_TERMS):
        reasons.append("protected_term")
    if any(marker in expression for marker in FORMALIZATION_MARKERS):
        reasons.append("register_or_formalization")
    if (original, expression) in ORTHOGRAPHY_PAIRS:
        reasons.append("orthography_only")
    if re.fullmatch(r"[ァ-ヶー・]+", original) or original.endswith("っ"):
        reasons.append("non_predicate_or_fragment")
    if any(
        start > 0 and sentence[start - 1 : start - 1 + len(compound)] == compound
        for compound in PROTECTED_COMPOUNDS
    ):
        reasons.append("partial_compound_span")
    return sorted(set(reasons))


def check_completed(
    source: dict[str, Any], lexeme: dict[str, Any], plan: dict[str, Any], expression: str
) -> dict[str, Any]:
    """Observe the completed sentence independently of the generation success flag.

    The generated form is reparsed as a new source construction. Parser
    disagreement and unknown valency are not silently turned into semantic
    preservation or grammatical approval.
    """
    rejected: list[str] = []
    unresolved: list[str] = []
    if source["status"] != "constructed" or plan["status"] != "planned":
        return {
            "realization_status": "not_constructible",
            "local_constraint_status": "unresolved",
            "comparison_status": "held",
            "rejected_reasons": [],
            "unresolved_reasons": [plan.get("reason") or source.get("reason") or "plan_not_ready"],
            "observed_construction": None,
            "A": {"pass": None, "reasons": []},
        }
    original = source["source_sentence"]
    start, end = source["edit_span"]
    sentence = source["P"] + expression + source["R"]
    if source["P"] + source["E0"] + source["R"] != original:
        rejected.append("source_edit_not_roundtrip")
    if (
        sentence[:start] != original[:start]
        or sentence[start + len(expression) :] != original[end:]
    ):
        rejected.append("outside_edit_range_changed")
    if not expression or any(char in expression for char in "。！？!?\r\n\t"):
        rejected.append("incomplete_or_terminal_expression")
    try:
        rerendered = realize_plan(plan, lexeme)
        if rerendered != expression:
            rejected.append("plan_expression_mismatch")
    except StructureUnsupported as exc:
        rejected.append("plan_cannot_be_reexecuted:" + exc.reason)
    if lexeme["construction"] == "te_compound_kuru":
        prefix = lexeme["compound_structure"][0]["observed_te"]
        if not expression.startswith(prefix):
            rejected.append("compound_first_component_dropped")
    if lexeme["construction"] in {"sahen", "noun_verb_compound", "suru_bound"}:
        component = (
            lexeme["compound_structure"][0].get("surface")
            if lexeme["construction"] != "suru_bound"
            else lexeme["inflecting_base"][:-2]
        )
        if component and not expression.startswith(component):
            rejected.append("lexical_prefix_dropped")
    if "potential" in plan["operators"] and re.search(r"(?:でき|出来).*(?:られ|れ)", expression):
        rejected.append("potential_or_voice_double_stacking")
    if "negative" in plan["operators"] and not any(
        value in expression for value in ("ない", "なかった")
    ):
        rejected.append("negative_operator_missing")
    if "negative_concessive" in plan["operators"] and not expression.endswith("なくても"):
        rejected.append("negative_concessive_missing")
    if "enable" in plan["operators"] and "可能にする" not in expression:
        rejected.append("potentialization_operator_missing")
    if "uru" in plan["operators"] and not expression.endswith("うる"):
        rejected.append("uru_operator_missing")
    if "teiru" in plan["operators"] and not any(
        value in expression for value in ("ている", "でいる", "ていた", "でいた")
    ):
        rejected.append("aspect_operator_missing")
    if "past" in plan["operators"] and not expression.endswith(("た", "だ")):
        rejected.append("past_operator_missing")
    for suffix in ("なる", "うる", "ていた", "ても", "られる", "れる"):
        if (
            expression.endswith(suffix)
            and source["R"].startswith(suffix)
            and not source["E0"].endswith(suffix)
        ):
            rejected.append("boundary_auxiliary_duplication:" + suffix)
    observed: dict[str, Any] | None = None
    if not rejected:
        observed = analyze_source(
            sentence, start, start + len(expression), frame_id=source["frame_id"] + ":candidate"
        )
        if observed["status"] != "constructed":
            unresolved.append("candidate_reparse_unresolved:" + str(observed.get("reason")))
        else:
            if observed["edit_span"] != [start, start + len(expression)]:
                unresolved.append("candidate_reparse_edit_boundary_changed")
            observed_sequences = {
                tuple(item["operators"]) for item in observed["interpretation_candidates"]
            }
            if tuple(plan["operators"]) not in observed_sequences:
                unresolved.append("candidate_operator_reparse_not_aligned")
            old_links = {
                (link["head_token"], link["particle_or_marker"])
                for link in source["argument_links"]
            }
            new_links = {
                (link["head_token"], link["particle_or_marker"])
                for link in observed["argument_links"]
            }
            if old_links - new_links:
                unresolved.append("target_argument_attachment_not_reconfirmed")
    a_reasons = _rule_a_reasons(source, expression)
    if a_reasons:
        rejected.extend("existing_A:" + item for item in a_reasons)
    if rejected:
        status, comparison = "violated", "rejected"
    elif unresolved:
        status, comparison = "unresolved", "held"
    elif sentence == original:
        status, comparison = "satisfied", "original_identity"
    elif plan["unresolved_content_notes"]:
        status, comparison = "satisfied", "eligible_with_notes"
    else:
        status, comparison = "satisfied", "eligible"
    return {
        "realization_status": "constructed",
        "local_constraint_status": status,
        "comparison_status": comparison,
        "rejected_reasons": sorted(set(rejected)),
        "unresolved_reasons": sorted(set(unresolved)),
        "observed_construction": {
            "status": observed.get("status"),
            "edit_span": observed.get("edit_span"),
            "operator_sequences": observed.get("interpretation_candidates"),
            "argument_links": observed.get("argument_links"),
        }
        if observed is not None
        else None,
        "A": {"pass": not a_reasons, "reasons": a_reasons},
        "completed_sentence": sentence,
        "semantic_preservation": None,
        "naturalness_gain": None,
        "editorial_decision": None,
    }


def process_structured_frame(
    sentence: str,
    target_start: int,
    target_end: int,
    *,
    frame_id: str,
    raw_generations: list[dict[str, Any]],
    source_snapshot_sha256: str | None = None,
    max_new_tokens: int = 16,
) -> dict[str, Any]:
    """The shared normal/experiment entry point; no model or score is called here."""
    source = analyze_source(
        sentence,
        target_start,
        target_end,
        frame_id=frame_id,
        source_snapshot_sha256=source_snapshot_sha256,
    )
    output: dict[str, Any] = {
        "source": source,
        "raw_results": [],
        "candidate_lexemes": [],
        "realization_plans": [],
        "candidate_groups": [],
        "realization_failures": [],
    }
    if source["status"] != "constructed":
        return output
    source_lexeme = source["source_lexeme"]
    source_plans = build_plans(source, source_lexeme)
    output["source_reconstruction"] = [
        {
            "plan_id": plan.get("plan_id"),
            "status": plan["status"],
            "expression": plan.get("expression"),
            "roundtrip": plan.get("expression") == source["E0"],
        }
        for plan in source_plans
    ]
    groups: dict[str, dict[str, Any]] = {}
    lexemes: dict[str, dict[str, Any]] = {}
    for raw in raw_generations:
        extracted = extract_lexeme(
            source["P"],
            str(raw["raw_text"]),
            generated_token_count=int(raw["generated_token_count"]),
            max_new_tokens=max_new_tokens,
        )
        raw_record = {
            "seed": raw["seed"],
            "raw_text": raw["raw_text"],
            "generation_origin": raw.get("generation_origin"),
            "prefix": source["P"],
            "extraction": extracted,
        }
        output["raw_results"].append(raw_record)
        if extracted["status"] != "extracted":
            output["realization_failures"].append(
                {"seed": raw["seed"], "stage": "lexical_extraction", "reason": extracted["reason"]}
            )
            continue
        lexeme = extracted["lexeme"]
        lexeme_id = stable_id(
            "lex-v3-", lexeme["lexical_identity"], lexeme["inflecting_base"], lexeme["construction"]
        )
        lexemes.setdefault(lexeme_id, {**lexeme, "lexeme_id": lexeme_id, "raw_refs": []})[
            "raw_refs"
        ].append(raw["seed"])
        for plan in build_plans(source, lexeme):
            plan["raw_seed"] = raw["seed"]
            output["realization_plans"].append(plan)
            if plan["status"] != "planned":
                output["realization_failures"].append(
                    {
                        "seed": raw["seed"],
                        "stage": "planning",
                        "reason": plan["reason"],
                        "plan_id": plan.get("plan_id"),
                    }
                )
                continue
            expression = str(plan["expression"])
            checked = check_completed(source, lexeme, plan, expression)
            sentence_candidate = checked["completed_sentence"]
            group = groups.setdefault(
                sentence_candidate,
                {
                    "candidate_id": stable_id("candidate-v3-", frame_id, sentence_candidate),
                    "sentence": sentence_candidate,
                    "expression": expression,
                    "lexical_structure": lexeme,
                    "realization_plan_refs": [],
                    "raw_refs": [],
                    "branch_checks": [],
                    "semantic_preservation": None,
                    "naturalness_gain": None,
                    "editorial_decision": None,
                },
            )
            group["realization_plan_refs"].append(plan["plan_id"])
            group["raw_refs"].append(raw["seed"])
            group["branch_checks"].append({"plan_id": plan["plan_id"], **checked})
    output["candidate_lexemes"] = list(lexemes.values())
    for group in groups.values():
        checks = group["branch_checks"]
        statuses = [item["comparison_status"] for item in checks]
        if "eligible" in statuses:
            group["comparison_status"] = "eligible"
        elif "eligible_with_notes" in statuses:
            group["comparison_status"] = "eligible_with_notes"
        elif "original_identity" in statuses:
            group["comparison_status"] = "original_identity"
        elif "held" in statuses:
            group["comparison_status"] = "held"
        else:
            group["comparison_status"] = "rejected"
        group["realization_status"] = "constructed"
        group["local_constraint_status"] = (
            "satisfied"
            if group["comparison_status"]
            in {"eligible", "eligible_with_notes", "original_identity"}
            else "unresolved"
            if group["comparison_status"] == "held"
            else "violated"
        )
        group["raw_refs"] = sorted(set(group["raw_refs"]))
        group["realization_plan_refs"] = sorted(set(group["realization_plan_refs"]))
    output["candidate_groups"] = sorted(groups.values(), key=lambda item: item["candidate_id"])
    return output
