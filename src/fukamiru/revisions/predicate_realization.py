"""V3.1 shared source construction, lexical realization, and local validation.

The optional worker profile and the 500-frame experiment both call this module.
It contains no model client, old-candidate injection, or editorial judgement.
The frozen V3 module remains available for read-only B0 comparison.
"""

from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from typing import Any

from fukamiru.revisions import structured_realization as v3
from fukamiru.revisions.japanese_analysis import normalized_word, parse_japanese

PROFILE = "structured-realization-v31-np-hybrid"
BASE_GENERATION = v3.BASE_GENERATION
StructureUnsupported = v3.StructureUnsupported

# These are local construction constraints, not semantic quality labels.  A
# missing lexical entry remains unverified, not falsely dictionary-confirmed.
CASE_FRAME_RULES: dict[str, str] = {
    "与える": "transitive",
    "施す": "transitive",
    "置く": "transitive",
    "据える": "transitive",
    "並べる": "transitive",
    "載せる": "transitive",
    "組織する": "transitive",
    "構成する": "transitive",
    "編集する": "transitive",
    "配列する": "transitive",
    "分析する": "transitive",
    "検証する": "transitive",
    "機能する": "intransitive",
    "成立する": "intransitive",
    "位置する": "intransitive",
    "浮かび上がる": "intransitive",
    "起こる": "intransitive",
    "生じる": "intransitive",
}
_DERIVATION_AMBIGUOUS = {"思える", "できる", "出来る"}


@lru_cache(maxsize=8192)
def tokens(text: str) -> tuple[dict[str, Any], ...]:
    """UniDic tokens including its attested orthographic base/form fields."""
    with v3._MECAB_LOCK:
        node = v3._tagger().parseToNode(text)
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
                        "orth_form": fields[8] if len(fields) > 8 else None,
                        "orth_base": fields[10] if len(fields) > 10 else None,
                    }
                )
                cursor = start + len(node.surface)
            node = node.next
    if any(text[row["start"] : row["end"]] != row["text"] for row in result):
        raise StructureUnsupported("mecab_roundtrip_failed")
    return tuple(result)


def _base(token: dict[str, Any]) -> tuple[str, str, list[str], dict[str, Any]]:
    kind = v3._class(token)
    lemma = str(token.get("lemma") or "")
    surface = str(token["text"])
    orth_base = str(token.get("orth_base") or "")
    orth_form = str(token.get("orth_form") or "")
    if orth_form not in {surface, "*"}:
        raise StructureUnsupported("dictionary_orthographic_form_not_attested")
    if kind == "suru" and lemma.endswith("ずる"):
        base = orth_base if orth_base.endswith("ずる") else lemma
        if not (surface == base or surface == base[:-2] + "じ"):
            raise StructureUnsupported("zuru_dictionary_form_unaligned")
        return base, "zuru", [], {"rule": "unidic_zuru_orthbase", "observed": surface}
    if kind == "suru":
        base = orth_base if orth_base.endswith("する") else "する"
        if base == "する" and surface in {"し", "さ", "すれ", "せ", "する", "す"}:
            return base, kind, [], {"rule": "unidic_suru_orthbase", "observed": surface}
        if surface == base or surface.startswith(base[:-2]):
            return base, kind, [], {"rule": "unidic_suru_orthbase", "observed": surface}
        raise StructureUnsupported("suru_dictionary_form_unaligned")
    if kind == "kuru":
        base = "来る" if orth_base == "来る" or "来" in surface else "くる"
        return base, kind, [], {"rule": "unidic_kuru_orthbase", "observed": surface}
    if kind == "godan":
        if not orth_base or orth_base[-1] not in v3.GODAN_I:
            raise StructureUnsupported("godan_dictionary_orthbase_unavailable")
        if lemma and lemma[-1] in v3.GODAN_I:
            try:
                derived = v3._potential_base(lemma, "godan", "single")[0]
            except StructureUnsupported:
                derived = None
            if derived == orth_base:
                return (
                    lemma,
                    "godan",
                    ["potential"],
                    {
                        "rule": "unidic_attested_derived_potential",
                        "observed_inflecting_base": orth_base,
                    },
                )
        return (
            orth_base,
            kind,
            [],
            {
                "rule": "unidic_attested_orthbase",
                "observed_form": surface,
                "inflection": token["inflection"],
            },
        )
    if kind == "ichidan":
        if surface.endswith("る") and token["inflection"][1].startswith(("終止", "連体")):
            base = surface
        elif token["inflection"][1].startswith(("連用", "未然")):
            # UniDic's lexical spelling may differ from the observed spelling:
            # 位置づけ -> 位置づける, not 位置付ける.
            base = surface + "る"
        else:
            base = orth_base
        if not base.endswith("る"):
            raise StructureUnsupported("ichidan_dictionary_orthbase_unavailable")
        return base, kind, [], {"rule": "unidic_attested_orthbase", "observed_form": surface}
    raise StructureUnsupported("base_class_unsupported")


def _lexeme_at(
    items: tuple[dict[str, Any], ...], index: int, text: str
) -> tuple[dict[str, Any], int]:
    if index >= len(items):
        raise StructureUnsupported("lexeme_start_outside_tokens")
    first = items[index]
    if v3._is_sahen(first) and index + 1 < len(items):
        second = items[index + 1]
        if second["start"] == first["end"] and v3._pos(second) == "動詞":
            lemma = str(second.get("lemma") or "")
            if lemma in {"為る", "する", "出来る", "できる"}:
                noun = str(first["text"])
                potential = lemma in {"出来る", "できる"}
                return {
                    "lexical_identity": str(first.get("lemma") or noun) + "する",
                    "dictionary_lemma": str(first.get("lemma") or noun) + "する",
                    "inflecting_base": noun + "する",
                    "inflection_class": "suru",
                    "construction": "sahen",
                    "compound_structure": [{"kind": "sahen_noun", "surface": noun}],
                    "derivation_tree": {"operator": "potential", "surface": second["text"]}
                    if potential
                    else None,
                    "inherent_functions": ["potential"] if potential else [],
                    "surface_variants": [noun + "する"],
                    "raw_surface": text[first["start"] : second["end"]],
                    "dictionary_evidence": [first, second],
                }, index + 2
    if v3._pos(first) == "名詞" and index + 1 < len(items):
        second = items[index + 1]
        if second["start"] == first["end"] and v3._pos(second) == "動詞":
            base, kind, inherent, derivation = _base(second)
            noun = str(first["text"])
            return {
                "lexical_identity": str(first.get("lemma") or noun)
                + str(second.get("lemma") or base),
                "dictionary_lemma": str(first.get("lemma") or noun)
                + str(second.get("lemma") or base),
                "inflecting_base": noun + base,
                "inflection_class": kind,
                "construction": "noun_verb_compound",
                "compound_structure": [
                    {"kind": "noun_prefix", "surface": noun},
                    {"kind": "verb", "base": base, "class": kind},
                ],
                "derivation_tree": derivation if inherent else None,
                "inherent_functions": inherent,
                "surface_variants": [noun + base],
                "raw_surface": text[first["start"] : second["end"]],
                "dictionary_evidence": [first, second],
            }, index + 2
    if v3._pos(first) != "動詞":
        raise StructureUnsupported("non_predicate_at_lexical_slot")
    base, kind, inherent, derivation = _base(first)
    return {
        "lexical_identity": str(first.get("lemma") or base),
        "dictionary_lemma": str(first.get("lemma") or base),
        "inflecting_base": base,
        "inflection_class": kind,
        "construction": "single",
        "compound_structure": [{"kind": "verb", "base": base, "class": kind}],
        "derivation_tree": derivation if inherent else None,
        "inherent_functions": inherent,
        "surface_variants": [base],
        "raw_surface": str(first["text"]),
        "dictionary_evidence": [first],
    }, index + 1


def _inflect(base: str, kind: str, form: str) -> str:
    if kind == "zuru":
        if not base.endswith("ずる"):
            raise StructureUnsupported("zuru_base_unavailable")
        stem = base[:-2]
        return {
            "nonpast": base,
            "renyou": stem + "じ",
            "mizen": stem + "じ",
            "te": stem + "じて",
            "past": stem + "じた",
        }[form]
    return v3._inflect(base, kind, form)


def _apply_voice(base: str, kind: str, operator: str, construction: str) -> tuple[str, str]:
    if kind == "zuru":
        return base[:-2] + "じられる", "ichidan"
    if operator == "potential":
        return v3._potential_base(base, kind, construction)
    return v3._passive_base(base, kind, construction)


def realize_plan(plan: dict[str, Any], lexeme: dict[str, Any]) -> str:
    base = str(lexeme["inflecting_base"])
    kind = str(lexeme["inflection_class"])
    construction = str(lexeme["construction"])
    for operator in plan["operators"]:
        if operator in {"potential", "passive"}:
            base, kind = _apply_voice(base, kind, operator, construction)
        elif operator == "negative":
            base = base[:-2] + "じない" if kind == "zuru" else v3._negative(base, kind)
            kind = "negative_auxiliary"
        elif operator == "past":
            if base.endswith("ない"):
                base = base[:-2] + "なかった"
            elif base.endswith("ている") or base.endswith("でいる"):
                base = base[:-2] + "いた"
            elif base.endswith("てくる") or base.endswith("でくる"):
                base = base[:-2] + "きた"
            elif base.endswith("て来る") or base.endswith("で来る"):
                base = base[:-2] + "来た"
            else:
                base = _inflect(base, kind, "past")
        elif operator == "teiru":
            base, kind = _inflect(base, kind, "te") + "いる", "ichidan"
        elif operator == "tekuru":
            base, kind = _inflect(base, kind, "te") + "くる", "kuru"
        elif operator == "uru":
            base, kind = _inflect(base, kind, "renyou") + "うる", "ichidan"
        else:
            raise StructureUnsupported("operator_not_implemented:" + operator)
    connection = plan.get("outer_connection") or "nonpast"
    if connection in {"te", "renyou"} and not plan["operators"]:
        base = _inflect(base, kind, connection)
    elif connection != "nonpast" and not (connection in {"te", "renyou"} and plan["operators"]):
        raise StructureUnsupported("outer_connection_not_implemented:" + str(connection))
    return base


def _chain(
    items: tuple[dict[str, Any], ...], consumed: int, lexeme: dict[str, Any]
) -> tuple[int, list[dict[str, Any]], str, list[dict[str, Any]]]:
    """Own one contiguous predicate phrase and retain its functional scopes."""
    index = consumed
    inherent = list(lexeme["inherent_functions"])
    operators = list(inherent)
    roles: list[dict[str, Any]] = []
    voice: str | None = None
    end = int(items[consumed - 1]["end"])
    connection = "nonpast"
    while index < len(items) and items[index]["start"] == end:
        current = items[index]
        surface = str(current["text"])
        following = items[index + 1] if index + 1 < len(items) else None
        if surface in {"られ", "られる", "れ", "れる"} and v3._pos(current) == "助動詞":
            voice = "rareru" if surface.startswith("ら") else "reru"
            operators.append("voice")
        elif surface in {"て", "で"} and v3._pos(current) == "助詞":
            if (
                following
                and following["start"] == current["end"]
                and str(following.get("lemma")) in {"居る", "いる"}
            ):
                roles.append({"surface": surface, "role": "aspect_connector"})
                index += 1
                current = items[index]
                surface = str(current["text"])
                operators.append("teiru")
            elif (
                following
                and following["start"] == current["end"]
                and str(following.get("lemma")) in {"来る", "くる"}
            ):
                roles.append({"surface": surface, "role": "direction_connector"})
                index += 1
                current = items[index]
                surface = str(current["text"])
                operators.append("tekuru")
            else:
                connection = "te"
                roles.append({"surface": surface, "role": "outer_te_connection"})
                end = int(current["end"])
                index += 1
                break
        elif surface in {"た", "だ"} and v3._pos(current) == "助動詞":
            operators.append("past")
        elif surface == "ない" and v3._pos(current) == "助動詞":
            operators.append("negative")
        elif surface in {"うる", "得る"} and v3._pos(current) == "動詞":
            operators.append("uru")
        else:
            break
        roles.append({"surface": surface, "role": operators[-1]})
        end = int(current["end"])
        index += 1
    if operators == inherent and connection == "nonpast":
        final_form = str(items[consumed - 1]["inflection"][1])
        if final_form.startswith("連用形"):
            connection = "renyou"
        elif final_form.startswith("未然形") and not inherent:
            raise StructureUnsupported("unowned_unfinished_mizen_form")
    if voice:
        kind = lexeme["inflection_class"]
        if voice == "rareru" and kind in {"ichidan", "kuru", "zuru"}:
            variants = ["passive", "potential"]
        elif voice == "reru" and kind in {"godan", "suru"}:
            variants = ["passive"]
        else:
            raise StructureUnsupported("voice_form_and_class_conflict")
        interpretations = []
        for variant in variants:
            sequence = [variant if item == "voice" else item for item in operators]
            interpretations.append(
                {"id": variant, "operators": sequence, "evidence": "source_auxiliary_morphology"}
            )
    else:
        interpretations = [
            {"id": "observed", "operators": operators, "evidence": "source_morphology"}
        ]
    return end, interpretations, connection, roles


def _parsed_candidates(
    sentence: str, target_start: int, target_end: int
) -> list[tuple[int, int, dict[str, Any], list[dict[str, Any]], str, list[dict[str, Any]]]]:
    items = tokens(sentence)
    candidates = []
    for index, item in enumerate(items):
        if not target_start - 24 <= item["start"] <= target_start:
            continue
        if v3._pos(item) != "動詞" and not (
            v3._is_sahen(item) and index + 1 < len(items) and v3._pos(items[index + 1]) == "動詞"
        ):
            continue
        try:
            lexeme, consumed = _lexeme_at(items, index, sentence)
            end, interpretations, connection, roles = _chain(items, consumed, lexeme)
        except StructureUnsupported:
            continue
        if item["start"] <= target_start and end >= target_end:
            candidates.append((index, end, lexeme, interpretations, connection, roles))
    return sorted(candidates, key=lambda row: (items[row[0]]["start"], -row[1]))


def analyze_source(
    sentence: str,
    target_start: int,
    target_end: int,
    *,
    frame_id: str,
    source_snapshot_sha256: str | None = None,
) -> dict[str, Any]:
    source: dict[str, Any] = {
        "frame_id": frame_id,
        "source_sentence": sentence,
        "source_hash": hashlib.sha256(sentence.encode("utf-8")).hexdigest(),
        "source_snapshot_sha256": source_snapshot_sha256,
        "old_target_span": [target_start, target_end],
        "old_target_text": sentence[target_start:target_end],
        "status": "pending",
        "reason": None,
        "semantic_preservation": None,
    }
    if not 0 <= target_start < target_end <= len(sentence):
        source.update(status="not_constructible", reason="source_anchor_out_of_bounds")
        return source
    functional = v3._functional_overlap(sentence, target_start, target_end)
    if functional:
        source.update(
            status="functional_expression_preserved",
            reason=functional["rule_id"],
            source_expression=functional["expression"],
            edit_span=functional["span"],
            protected_spans=[functional],
        )
        return source
    if v3._inside_quote(sentence, target_start):
        source.update(status="protected_quotation", reason="target_inside_quotation")
        return source
    try:
        parsed = _parsed_candidates(sentence, target_start, target_end)
        if not parsed:
            raise StructureUnsupported("no_complete_owned_predicate_covering_frame")
        items = tokens(sentence)
        index, end, lexeme, interpretations, connection, roles = parsed[0]
        start = int(items[index]["start"])
        expression = sentence[start:end]
        if not any(
            realize_plan({"operators": row["operators"], "outer_connection": connection}, lexeme)
            == expression
            for row in interpretations
        ):
            raise StructureUnsupported("source_reinflection_roundtrip_failed")
        analysis = parse_japanese(sentence)
        links = v3._argument_links(analysis, sentence, start, end)
        external = []
        if re.match(r"(?:こと[がは]でき(?:る|ない)|ことが可能)", sentence[end:]):
            external.append("potential")
        if sentence[end:].startswith("と考えられる"):
            external.append("epistemic_frame")
        source.update(
            status="constructed",
            reason=None,
            construction_id=v3.stable_id("construction-v31-", sentence, start, end),
            construction_kind=lexeme["construction"],
            edit_span=[start, end],
            lexical_slot_span=[start, start + len(lexeme["raw_surface"])],
            generation_prefix_end=start,
            P=sentence[:start],
            E0=expression,
            R=sentence[end:],
            source_expression=expression,
            source_lexeme=lexeme,
            outer_connection=connection,
            interpretation_candidates=interpretations,
            operator_tree=[v3._tree(row["operators"]) for row in interpretations],
            function_tokens=roles,
            argument_links=links,
            external_operators=external,
            protected_spans=[],
            clause_spans=[{"start": 0, "end": len(sentence), "evidence": "sentence"}],
            ownership_evidence={
                "rule": "longest_contiguous_predicate_covering_frame",
                "token_start": index,
            },
        )
    except StructureUnsupported as exc:
        source.update(status="not_constructible", reason=exc.reason)
    return source


def extract_lexeme(
    prefix: str, raw_text: str, *, generated_token_count: int, max_new_tokens: int = 16
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "pending",
        "reason": None,
        "raw_text": raw_text,
        "prefix_sha256": hashlib.sha256(prefix.encode("utf-8")).hexdigest(),
        "lexeme": None,
        "generation_span": None,
    }
    if not raw_text or raw_text[0] in "、。！？!?「『（(\r\n":
        result.update(status="candidate_extraction_failed", reason="empty_or_punctuation_start")
        return result
    combined = prefix + raw_text
    boundary = len(prefix)
    try:
        items = tokens(combined)
        start = boundary + len(raw_text) - len(raw_text.lstrip(" 　"))
        if any(row["start"] < start < row["end"] for row in items):
            raise StructureUnsupported("generation_boundary_crosses_morpheme")
        first = next((index for index, row in enumerate(items) if row["start"] == start), None)
        if first is None:
            raise StructureUnsupported("generation_start_unaligned")
        if v3._pos(items[first]) == "副詞" and first + 1 < len(items):
            result["leading_modifier"] = items[first]["text"]
            first += 1
            start = int(items[first]["start"])
        if v3._pos(items[first]) in {"助詞", "助動詞", "補助記号"}:
            raise StructureUnsupported("non_predicate_at_generation_start")
        if (
            v3._pos(items[first]) == "名詞"
            and first + 1 < len(items)
            and items[first + 1]["text"] in {"を", "が", "に", "へ", "で", "から"}
        ):
            raise StructureUnsupported("new_argument_before_predicate")
        lexeme, consumed = _lexeme_at(items, first, combined)
        end = int(items[consumed - 1]["end"])
        if end == len(combined) and generated_token_count >= max_new_tokens:
            raise StructureUnsupported("truncated_lexeme_without_visible_boundary")
        if any(char in combined[start:end] for char in "。！？!?\r\n\t"):
            raise StructureUnsupported("lexeme_contains_sentence_boundary")
        result.update(
            status="extracted",
            lexeme=lexeme,
            generation_span=[start - boundary, end - boundary],
            raw_grammatical_features={
                "observed_tokens": list(items[first:consumed]),
                "following_surface": combined[end : min(len(combined), end + 16)],
                "dictionary_orthographic_base_verified": True,
            },
        )
    except StructureUnsupported as exc:
        result.update(status="candidate_extraction_failed", reason=exc.reason)
    return result


def _valency_assessment(
    source: dict[str, Any], lexeme: dict[str, Any], operators: list[str]
) -> dict[str, Any]:
    candidate = normalized_word(str(lexeme["dictionary_lemma"]))
    original = normalized_word(str(source["source_lexeme"]["dictionary_lemma"]))
    candidate_frame = CASE_FRAME_RULES.get(candidate)
    original_frame = CASE_FRAME_RULES.get(original)
    relations = {link.get("syntactic_relation") for link in source["argument_links"]}
    markers = {link.get("particle_or_marker") for link in source["argument_links"]}
    mismatch = None
    if candidate_frame == "intransitive" and ("obj" in relations or "を" in markers):
        mismatch = "known_intransitive_with_preserved_direct_object"
    elif candidate_frame == "intransitive" and "passive" in operators:
        mismatch = "intransitive_direct_passive_unlicensed_by_local_frame"
    elif (
        original_frame == "intransitive"
        and candidate_frame == "transitive"
        and "obj" not in relations
        and "を" not in markers
        and any(link.get("particle_or_marker") in {"が", "は"} for link in source["argument_links"])
    ):
        mismatch = "transitive_candidate_object_unattested_for_intransitive_source"
    return {
        "source_frame": original_frame,
        "candidate_frame": candidate_frame,
        "evidence": "versioned_local_case_frame_rule"
        if candidate_frame
        else "local_case_frame_absent",
        "observed_markers": sorted(str(x) for x in markers if x),
        "known_mismatch": mismatch,
        "status": "mismatch"
        if mismatch
        else "case_pattern_not_contradicted_selection_unverified"
        if candidate_frame
        else "valency_unverified",
    }


def build_plans(source: dict[str, Any], lexeme: dict[str, Any]) -> list[dict[str, Any]]:
    if source["status"] != "constructed":
        return [{"status": "held", "reason": "source_not_constructed", "operators": []}]
    intrinsic = set(lexeme["inherent_functions"])
    plans = []
    for interpretation in source["interpretation_candidates"]:
        operators = list(interpretation["operators"])
        reason = None
        if any(function not in operators for function in intrinsic):
            reason = "candidate_adds_unlicensed_operator"
        if lexeme["inflecting_base"] in _DERIVATION_AMBIGUOUS and "potential" in operators:
            reason = "candidate_potential_derivation_ambiguous"
        if "teiru" in operators and lexeme["inflecting_base"] in {
            "いる",
            "居る",
            "ある",
            "有る",
            "在る",
        }:
            reason = "existential_lexeme_would_double_aspect"
        if "potential" in intrinsic and "potential" in source.get("external_operators", []):
            reason = "candidate_duplicates_external_potential"
        valency = _valency_assessment(source, lexeme, operators)
        if valency["known_mismatch"]:
            reason = "argument_voice_mismatch:" + valency["known_mismatch"]
        notes = []
        if valency["status"] == "valency_unverified":
            notes.append("valency_unverified")
        elif valency["status"] == "case_pattern_not_contradicted_selection_unverified":
            notes.append("selectional_compatibility_unverified")
        if len(source["interpretation_candidates"]) > 1:
            notes.append("source_voice_interpretation_unresolved")
        plan = {
            "plan_id": v3.stable_id(
                "plan-v31-", source["construction_id"], lexeme["lexical_identity"], operators
            ),
            "source_construction_id": source["construction_id"],
            "candidate_lexeme_id": v3.stable_id(
                "lex-v31-", lexeme["lexical_identity"], lexeme["inflecting_base"]
            ),
            "supported_source_interpretation": interpretation["id"],
            "operators": operators,
            "outer_connection": source["outer_connection"],
            "operator_alignment": [
                {
                    "source_operator": operator,
                    "candidate_realization": "lexical_derivation_aligned_once"
                    if operator in intrinsic
                    else "v31_morphology_rule",
                }
                for operator in operators
            ],
            "argument_alignment": [
                {
                    "source_link": link,
                    "candidate_particle": link["particle_or_marker"],
                    "semantic_role": None,
                    "status": valency["status"],
                }
                for link in source["argument_links"]
            ],
            "valency_assessment": valency,
            "unresolved_content_notes": notes,
            "fixed_material": {"P": source["P"], "R": source["R"]},
            "status": "held" if reason else "planned",
            "reason": reason,
        }
        if not reason:
            try:
                plan["expression"] = realize_plan(plan, lexeme)
            except StructureUnsupported as exc:
                plan.update(status="held", reason=exc.reason)
        plans.append(plan)
    return plans


def _same_lexeme(expected: dict[str, Any], observed: dict[str, Any]) -> bool:
    left = normalized_word(str(expected["dictionary_lemma"]))
    right = normalized_word(str(observed["dictionary_lemma"]))
    if left == right:
        return True
    # UniDic uses the common lexical lemma for this orthographic variant.
    variants = [{"切り拓く", "切り開く"}, {"論じる", "論ずる"}]
    return any({left, right} <= group for group in variants)


def check_completed(
    source: dict[str, Any], lexeme: dict[str, Any], plan: dict[str, Any], expression: str
) -> dict[str, Any]:
    if source["status"] != "constructed" or plan["status"] != "planned":
        return {
            "comparison_status": "held",
            "local_constraint_status": "unresolved",
            "unresolved_reasons": [plan.get("reason") or source.get("reason")],
            "rejected_reasons": [],
            "completed_sentence": None,
            "A": {"pass": None, "reasons": []},
        }
    sentence = source["P"] + expression + source["R"]
    start, end = source["edit_span"]
    rejected: list[str] = []
    unresolved: list[str] = []
    if source["P"] + source["E0"] + source["R"] != source["source_sentence"]:
        rejected.append("source_edit_not_roundtrip")
    if (
        sentence[:start] != source["source_sentence"][:start]
        or sentence[start + len(expression) :] != source["source_sentence"][end:]
    ):
        rejected.append("outside_edit_range_changed")
    if not expression or any(char in expression for char in "。！？!?\r\n\t"):
        rejected.append("incomplete_or_terminal_expression")
    if realize_plan(plan, lexeme) != expression:
        rejected.append("plan_expression_mismatch")
    if not rejected:
        observed = analyze_source(
            sentence,
            start,
            start + len(expression),
            frame_id=source["construction_id"] + ":candidate",
        )
        if observed["status"] != "constructed":
            unresolved.append("candidate_reparse_unresolved:" + str(observed["reason"]))
        else:
            if observed["edit_span"] != [start, start + len(expression)]:
                unresolved.append("candidate_reparse_edit_boundary_changed")
            if not _same_lexeme(lexeme, observed["source_lexeme"]):
                unresolved.append("candidate_lexical_head_reparse_mismatch")
            if tuple(plan["operators"]) not in {
                tuple(row["operators"]) for row in observed["interpretation_candidates"]
            }:
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
    else:
        observed = None
    a_reasons = v3._rule_a_reasons(source, expression)
    rejected.extend("existing_A:" + reason for reason in a_reasons)
    if rejected:
        status, comparison = "violated", "rejected"
    elif unresolved:
        status, comparison = "unresolved", "held"
    elif sentence == source["source_sentence"]:
        status, comparison = "satisfied", "original_identity"
    elif plan["unresolved_content_notes"]:
        status, comparison = "satisfied", "eligible_with_notes"
    else:
        status, comparison = "satisfied", "eligible"
    return {
        "comparison_status": comparison,
        "local_constraint_status": status,
        "rejected_reasons": sorted(set(rejected)),
        "unresolved_reasons": sorted(set(unresolved)),
        "observed_construction": observed,
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
    source_plans = build_plans(source, source["source_lexeme"])
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
        output["raw_results"].append(
            {
                "seed": raw["seed"],
                "raw_text": raw["raw_text"],
                "generation_origin": raw.get("generation_origin"),
                "prefix": source["P"],
                "extraction": extracted,
            }
        )
        if extracted["status"] != "extracted":
            output["realization_failures"].append(
                {"seed": raw["seed"], "stage": "lexical_extraction", "reason": extracted["reason"]}
            )
            continue
        lexeme = extracted["lexeme"]
        lexeme_id = v3.stable_id("lex-v31-", lexeme["lexical_identity"], lexeme["inflecting_base"])
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
            completed = checked["completed_sentence"]
            if not completed:
                continue
            group = groups.setdefault(
                completed,
                {
                    "candidate_id": v3.stable_id(
                        "candidate-v31-", source["construction_id"], completed
                    ),
                    "sentence": completed,
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
        statuses = [check["comparison_status"] for check in group["branch_checks"]]
        group["comparison_status"] = next(
            (
                status
                for status in (
                    "eligible",
                    "eligible_with_notes",
                    "original_identity",
                    "held",
                    "rejected",
                )
                if status in statuses
            ),
            "rejected",
        )
        group["raw_refs"] = sorted(set(group["raw_refs"]))
        group["realization_plan_refs"] = sorted(set(group["realization_plan_refs"]))
    output["candidate_groups"] = sorted(groups.values(), key=lambda row: row["candidate_id"])
    return output
