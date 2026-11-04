"""V3.1 common source/realization/NP branch regression checks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fukamiru.revisions.noun_phrase import (
    extract_slots,
    probe_input,
    realize_expansion,
    validate_probe,
)
from fukamiru.revisions.predicate_realization import (
    analyze_source,
    build_plans,
    extract_lexeme,
    process_structured_frame,
)


@pytest.mark.parametrize(
    ("sentence", "anchor", "expected", "operators"),
    [
        ("それはそこに位置していた。", "いた", "位置していた", ["teiru", "past"]),
        (
            "作品は棚に並べられていた。",
            "いた",
            "並べられていた",
            ["passive", "teiru", "past"],
        ),
        ("それを概念と呼んできた。", "きた", "呼んできた", ["tekuru", "past"]),
        ("その差を分析できない。", "分析できない", "分析できない", ["potential", "negative"]),
    ],
)
def test_owned_full_source_construction(sentence, anchor, expected, operators):
    start = sentence.index(anchor)
    source = analyze_source(sentence, start, start + len(anchor), frame_id="test")
    assert source["status"] == "constructed", source.get("reason")
    assert source["E0"] == expected
    assert operators in [item["operators"] for item in source["interpretation_candidates"]]


@pytest.mark.parametrize(
    ("sentence", "anchor", "raw", "base", "expression"),
    [
        ("輪郭を与えた。", "与えた", "かたちづくろう", "かたちづくる", "かたちづくった"),
        ("その差を分析できない。", "分析できない", "論じられない", "論ずる", "論じられない"),
        ("差を与える。", "与える", "施そう", "施す", "施す"),
        ("未来を獲得する。", "獲得する", "切り拓く", "切り拓く", "切り拓く"),
    ],
)
def test_attested_base_and_reconstructed_expression(sentence, anchor, raw, base, expression):
    start = sentence.index(anchor)
    source = analyze_source(sentence, start, start + len(anchor), frame_id="test")
    assert source["status"] == "constructed", source.get("reason")
    extracted = extract_lexeme(source["P"], raw, generated_token_count=5)
    assert extracted["status"] == "extracted", extracted.get("reason")
    assert extracted["lexeme"]["inflecting_base"] == base
    plans = build_plans(source, extracted["lexeme"])
    assert expression in [plan.get("expression") for plan in plans]


def test_known_valency_conflict_is_not_comparable():
    sentence = "その隣接が成立した。"
    start = sentence.index("成立した")
    source = analyze_source(sentence, start, start + len("成立した"), frame_id="test")
    assert source["status"] == "constructed"
    extracted = extract_lexeme(source["P"], "編集した", generated_token_count=5)
    assert extracted["status"] == "extracted"
    plans = build_plans(source, extracted["lexeme"])
    assert all(plan["status"] == "held" for plan in plans)
    assert any("argument_voice_mismatch" in str(plan["reason"]) for plan in plans)


@pytest.mark.parametrize(
    ("sentence", "source_text", "raw_candidate"),
    [
        ("輪郭を与えた。", "与えた", "浮かび上がった"),
        ("各対象が組織される。", "組織される", "機能される"),
    ],
)
def test_known_case_or_voice_conflict_is_held(sentence, source_text, raw_candidate):
    start = sentence.index(source_text)
    source = analyze_source(sentence, start, start + len(source_text), frame_id="case-test")
    assert source["status"] == "constructed"
    extracted = extract_lexeme(source["P"], raw_candidate, generated_token_count=5)
    assert extracted["status"] == "extracted"
    plans = build_plans(source, extracted["lexeme"])
    assert plans
    assert all(plan["status"] == "held" for plan in plans)
    assert any("argument_voice_mismatch" in str(plan["reason"]) for plan in plans)


def test_known_case_pattern_still_keeps_selectional_uncertainty():
    sentence = "輪郭を与えた。"
    start = sentence.index("与えた")
    source = analyze_source(sentence, start, start + len("与えた"), frame_id="case-normal")
    extracted = extract_lexeme(source["P"], "施した", generated_token_count=5)
    assert extracted["status"] == "extracted"
    plans = build_plans(source, extracted["lexeme"])
    assert any(plan["status"] == "planned" for plan in plans)
    assert all(
        "selectional_compatibility_unverified" in plan["unresolved_content_notes"]
        for plan in plans
        if plan["status"] == "planned"
    )


def test_normal_candidate_processing_has_no_language_judgement():
    sentence = "差を与える。"
    result = process_structured_frame(
        sentence,
        sentence.index("与える"),
        sentence.index("与える") + len("与える"),
        frame_id="test",
        raw_generations=[{"seed": 1001, "raw_text": "施そう", "generated_token_count": 5}],
    )
    assert result["source"]["status"] == "constructed"
    assert result["candidate_groups"]
    assert all(group["semantic_preservation"] is None for group in result["candidate_groups"])


def _np_fixture():
    sentence = "各対象が使用環境のなかでどのように組織されるかを問う。"
    start = sentence.index("組織される")
    source = analyze_source(sentence, start, start + len("組織される"), frame_id="fixture")
    assert source["status"] == "constructed", source.get("reason")
    site = {
        "site_id": "site-fixture",
        "section_heading": "節",
        "previous_sentence": None,
        "next_sentence": None,
    }
    slots = extract_slots(site, source)
    slot = next(item for item in slots if item["source_np_text"] == "使用環境")
    assert slot["probe_status"] == "selected"
    return site, source, slot


def _probe(slot, *, template="{E01}が使用される環境"):
    return {
        "np_id": slot["np_id"],
        "branch_advice": "explore",
        "observations": [
            {"kind": "compressed_relation", "evidence_text": "使用環境", "note": "関係仮説"}
        ],
        "interpretations": [
            {
                "analysis_id": "I1",
                "predicate_base": "使用する",
                "head_noun": "環境",
                "head_relation": "setting",
                "participants": [{"entity_ref": "E01", "role": "theme", "basis": "inferred"}],
                "uncertainty": ["使用主体は未明示"],
            }
        ],
        "proposals": [
            {
                "proposal_id": "P1",
                "analysis_id": "I1",
                "np_template": template,
                "internal_voice": "passive",
                "placement": "in_place",
                "extra_requirements": ["主語照応を保つ"],
                "meaning_risks": ["分配範囲は未確認"],
            }
        ],
    }


def test_np_schema_and_referential_branch():
    site, source, slot = _np_fixture()
    request = probe_input(site, source, slot)
    schema_path = (
        Path(__file__).resolve().parents[1]
        / "src/fukamiru/revisions/assets/noun_phrase_probe_schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    response = _probe(slot)
    assert validate_probe(response, schema, request) == []
    plan = realize_expansion(site, source, slot, response, response["proposals"][0])
    assert plan["status"] == "constructed", plan.get("reason")
    assert "各対象が使用される環境のなかで、それぞれが" in plan["branch_baseline_sentence"]
    assert plan["branch_source"]["E0"] == source["E0"]
    assert plan["actual_generation_prefix"] != source["P"]


def test_np_role_reversal_is_held():
    site, source, slot = _np_fixture()
    response = _probe(slot, template="{E01}が使用する環境")
    plan = realize_expansion(site, source, slot, response, response["proposals"][0])
    assert plan["status"] == "held"
    assert plan["reason"] == "use_relation_theme_role_not_preserved"


def test_np_source_protected_span_is_not_probed():
    site, source, slot = _np_fixture()
    source = {**source, "protected_spans": [{"span": slot["source_np_span"]}]}
    revised = extract_slots(site, source)
    protected = next(item for item in revised if item["source_np_text"] == "使用環境")
    assert protected["probe_status"] == "protected"
    assert "source_protected_span_overlap" in protected["protected_reasons"]


def test_np_schema_rejects_invented_reference():
    site, source, slot = _np_fixture()
    request = probe_input(site, source, slot)
    schema_path = (
        Path(__file__).resolve().parents[1]
        / "src/fukamiru/revisions/assets/noun_phrase_probe_schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    response = _probe(slot, template="{E99}が使用される環境")
    assert "unrecognized_template_entity_ref" in validate_probe(response, schema, request)


def test_worker_v31_profile_is_explicit_and_default_unchanged():
    from fukamiru.worker.collocation import (
        STRUCTURED_V31_PREDICATE_ONLY_PROFILE,
        _structured_profile,
    )

    assert _structured_profile({}) is None
    assert _structured_profile({"collocation_candidate_profile": "v29"}) is None
    assert (
        _structured_profile(
            {"collocation_candidate_profile": STRUCTURED_V31_PREDICATE_ONLY_PROFILE}
        )
        == STRUCTURED_V31_PREDICATE_ONLY_PROFILE
    )
