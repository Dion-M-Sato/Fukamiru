from __future__ import annotations

import unittest

from fukamiru.revisions.japanese_polish import (
    CandidateApplicationService,
    CandidateHardVeto,
    JapaneseCandidateGenerator,
    JapanesePolishTargetDetector,
    PolishTarget,
    SectionCandidateSelector,
    WordingCandidate,
)


class CapturingCandidates:
    def __init__(self) -> None:
        self.call: dict[str, object] = {}

    def complete_json(self, **kwargs: object) -> dict[str, object]:
        self.call = kwargs
        return {
            "candidates": [
                {
                    "text": text,
                    "relation_type": "contribution",
                    "rationale": "回復への寄与関係を保った表現",
                }
                for text in (
                    "羞恥と支配が主体の回復に貢献する。",
                    "羞恥と支配が主体の回復を促す。",
                    "羞恥と支配が主体の回復の契機となる。",
                    "羞恥と支配が主体の回復を支える。",
                    "羞恥と支配が主体の回復を可能にする。",
                )
            ]
        }


class EmptyCandidates:
    def __init__(self) -> None:
        self.call: dict[str, object] = {}

    def complete_json(self, **kwargs: object) -> dict[str, object]:
        self.call = kwargs
        return {"candidates": []}


def target_fixture(**updates: object) -> PolishTarget:
    values: dict[str, object] = {
        "id": "target-1",
        "section_id": "semantic:H",
        "block_id": "P",
        "ordinal": 0,
        "start_offset": 0,
        "end_offset": 20,
        "source_text": "羞恥と支配が主体の回復に寄与する。",
        "context_before": "",
        "context_after": "次の文で論証する。",
        "issue_codes": ("reasoning.relation_wording_audit",),
        "relation_type": "contribution",
        "allowed_relation_types": ("preserve", "contribution"),
        "semantic_roles": {
            "subject": "羞恥と支配",
            "affected_entity": "主体",
            "target_state": "回復",
        },
        "named_entities": (),
        "terminology": ("主体", "回復"),
        "request_signature": "signature",
    }
    values.update(updates)
    return PolishTarget(**values)  # type: ignore[arg-type]


class JapaneseCandidatePolishTests(unittest.TestCase):
    def test_detector_owns_exact_sentence_offsets(self) -> None:
        text = "前提を示す。羞恥や支配が回復に接続されるのかを問う。結論を述べる。"
        canonical = (
            '<article id="D" lang="ja"><section id="S">'
            '<h2 id="H">論点</h2><p id="P">'
            + text
            + "</p></section></article>"
        )

        targets = JapanesePolishTargetDetector.detect(canonical)

        target = next(item for item in targets if "接続される" in item.source_text)
        self.assertEqual(
            text[target.start_offset : target.end_offset], target.source_text
        )
        self.assertIn("style.underspecified_predicate", target.issue_codes)

    def test_detector_localizes_block_reasoning_to_trigger_sentence(self) -> None:
        first = "作品の配置を順に説明する。"
        second = "その編成は読者像を構成する。"
        canonical = (
            '<article id="D" lang="ja"><section id="S"><p id="P">'
            + first
            + second
            + "</p></section></article>"
        )
        resolution = {
            "decisions": [
                {
                    "block_id": "P",
                    "source_text": first + second,
                    "relation_type": "constitutive",
                    "triggers": ["構成"],
                    "semantic_roles": {"subject": "その編成"},
                    "academic_connectives": [],
                }
            ]
        }

        targets = JapanesePolishTargetDetector.detect(
            canonical, predicate_resolution=resolution
        )

        by_text = {target.source_text: target for target in targets}
        self.assertNotIn(first, by_text)
        self.assertEqual(by_text[second].relation_type, "constitutive")
        self.assertIn(
            "reasoning.relation_wording_audit", by_text[second].issue_codes
        )

    def test_detector_does_not_treat_all_long_academic_prose_as_a_problem(self) -> None:
        sentence = (
            "本節では先行研究の対象と方法を確認したうえで、分析対象となる資料の"
            "範囲を明示し、次節の比較に必要な前提を整理する。"
        )
        canonical = (
            '<article id="D" lang="ja"><section id="S"><p id="P">'
            + sentence
            + "</p></section></article>"
        )

        targets = JapanesePolishTargetDetector.detect(canonical)

        self.assertEqual(targets, ())

    def test_polish_detector_does_not_own_enumeration_resolution(self) -> None:
        clauses = (
            "資料を確認し、議論の前提を定め、必要な範囲を説明し、"
            "その結果を次節で比較する。"
        )
        enumeration = "労働、制度、賃金、分業、搾取を一列に並べている。"
        canonical = (
            '<article id="D" lang="ja"><section id="S"><p id="P">'
            + clauses
            + enumeration
            + "</p></section></article>"
        )

        targets = JapanesePolishTargetDetector.detect(canonical)

        by_text = {target.source_text: target for target in targets}
        self.assertNotIn(clauses, by_text)
        self.assertNotIn("style.enumeration_density", by_text[enumeration].issue_codes)

    def test_v28_does_not_send_repeated_connectives_to_llm_jp(self) -> None:
        canonical = (
            '<article id="D" lang="ja"><section id="S"><p id="P">'
            "ただし、資料を読む。ただし、記録を読む。ただし、証言を読む。"
            "</p></section></article>"
        )

        targets = JapanesePolishTargetDetector.detect(canonical, policy_version=3)

        self.assertEqual(targets, ())

    def test_v28_phrase_slot_excludes_the_governing_particle(self) -> None:
        sentence = "この配置を歴史的条件に位置づける。"
        canonical = (
            '<article id="D" lang="ja"><section id="S"><p id="P">'
            + sentence
            + "</p></section></article>"
        )

        target = JapanesePolishTargetDetector.detect(canonical, policy_version=3)[0]

        self.assertEqual(target.source_text, "位置づける")
        self.assertTrue(target.context_before.endswith("に"))
        self.assertEqual(target.semantic_roles["_fixed_left_boundary"], "に")
        self.assertEqual(target.semantic_roles["_fixed_right_boundary"], "。")

    def test_v28_phrase_slot_receives_only_local_issue_codes(self) -> None:
        sentence = "『第一』『第二』を歴史的条件に位置づける。"
        canonical = (
            '<article id="D" lang="ja"><section id="S"><p id="P">'
            + sentence
            + "</p></section></article>"
        )

        target = JapanesePolishTargetDetector.detect(canonical, policy_version=3)[0]

        self.assertIn("style.underspecified_predicate", target.issue_codes)
        self.assertIn("scope.phrase_only", target.issue_codes)
        self.assertNotIn(
            "surface.missing_middle_dot_between_quoted_items", target.issue_codes
        )

    def test_generator_adds_original_and_never_requests_model_anchors(self) -> None:
        adapter = CapturingCandidates()
        target = target_fixture(
            end_offset=len("羞恥と支配が主体の回復に寄与する。")
        )

        result = JapaneseCandidateGenerator(adapter).generate(
            target, candidate_count=6
        )

        self.assertEqual(len(result.candidates), 6)
        self.assertEqual(result.candidates[0].source_kind, "original")
        schema = adapter.call["json_schema"]
        self.assertNotIn("before", str(schema).casefold())
        self.assertNotIn("after", str(schema).casefold())
        self.assertNotIn("start_offset", str(schema).casefold())
        self.assertNotIn("confidence", str(schema).casefold())
        self.assertTrue(
            all(candidate.confidence == 0 for candidate in result.candidates[1:])
        )

    def test_v28_generator_accepts_zero_to_two_contextual_alternatives(self) -> None:
        adapter = EmptyCandidates()
        target = target_fixture(
            source_text="位置づける",
            end_offset=len("位置づける"),
            context_before="この配置を歴史的条件に",
            context_after="。次の文で論証する。",
            issue_codes=("style.underspecified_predicate", "scope.phrase_only"),
        )

        result = JapaneseCandidateGenerator(adapter).generate(
            target,
            candidate_count=3,
            allow_empty=True,
            maximum_candidate_characters=40,
            context_character_limit=12,
        )

        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.candidates[0].source_kind, "original")
        self.assertIn("candidate.no_valid_alternative", result.issues)
        self.assertNotIn("candidate.count_incomplete", result.issues)
        schema = adapter.call["json_schema"]
        candidate_schema = schema["properties"]["candidates"]  # type: ignore[index]
        self.assertEqual(candidate_schema["minItems"], 0)  # type: ignore[index]
        self.assertEqual(candidate_schema["maxItems"], 2)  # type: ignore[index]
        self.assertEqual(
            candidate_schema["items"]["properties"]["text"]["maxLength"],  # type: ignore[index]
            40,
        )

    def test_hard_veto_allows_case_change_required_by_a_new_predicate(self) -> None:
        source = "羞恥と支配が主体の回復に寄与する。"
        target = target_fixture(source_text=source, end_offset=len(source))
        candidate = WordingCandidate(
            id="candidate-valid",
            target_id=target.id,
            ordinal=1,
            text="羞恥と支配が主体の回復を促す。",
            relation_type="contribution",
            rationale="寄与関係を保ち、動詞に必要な格を選んだ。",
            confidence=0,
            source_kind="llm_jp",
        )

        issues = CandidateHardVeto.validate(
            target, candidate, block_text=source
        )

        self.assertNotIn("veto.predicate_arguments_changed", issues)
        self.assertNotIn("veto.new_logical_relation", issues)

    def test_hard_veto_rejects_subject_and_negation_changes(self) -> None:
        source = "羞恥と支配が主体の回復に寄与しない。"
        target = target_fixture(
            source_text=source,
            end_offset=len(source),
            terminology=("主体", "回復"),
        )
        candidate = WordingCandidate(
            id="candidate-1",
            target_id=target.id,
            ordinal=1,
            text="制度が主体の回復に寄与する。",
            relation_type="contribution",
            rationale="fixture",
            confidence=0.9,
            source_kind="llm_jp",
        )

        issues = CandidateHardVeto.validate(
            target, candidate, block_text=source
        )

        self.assertIn("veto.negation_changed", issues)
        self.assertIn("veto.predicate_arguments_changed", issues)

    def test_v28_hard_veto_requires_the_assigned_issue_to_be_resolved(self) -> None:
        source = "この配置を歴史的条件に位置づける。"
        start = source.index("位置づける")
        target = target_fixture(
            start_offset=start,
            end_offset=start + len("位置づける"),
            source_text="位置づける",
            context_before=source[:start],
            context_after=source[start + len("位置づける") :],
            issue_codes=("style.underspecified_predicate", "scope.phrase_only"),
            relation_type="surface",
            allowed_relation_types=("preserve", "surface"),
            terminology=(),
        )
        candidate = WordingCandidate(
            id="candidate-orthography-only",
            target_id=target.id,
            ordinal=1,
            text="位置付ける",
            relation_type="surface",
            rationale="表記だけを変更した。",
            confidence=0,
            source_kind="llm_jp",
        )

        issues = CandidateHardVeto.validate(
            target,
            candidate,
            block_text=source,
            require_issue_resolution=True,
        )

        self.assertIn("veto.target_issue_unresolved", issues)

    def test_section_selector_penalizes_repeated_predicates(self) -> None:
        first = target_fixture(id="t1", block_id="P1")
        second = target_fixture(id="t2", block_id="P2")

        def candidate(
            target_id: str, identifier: str, text: str, score: float
        ) -> WordingCandidate:
            return WordingCandidate(
                id=identifier,
                target_id=target_id,
                ordinal=1,
                text=text,
                relation_type="contribution",
                rationale="fixture",
                confidence=1,
                source_kind="llm_jp",
                total_score=score,
            )

        selected = SectionCandidateSelector.select(
            (first, second),
            {
                "t1": (
                    candidate("t1", "a1", "第一の要因は回復に寄与する。", 1.0),
                ),
                "t2": (
                    candidate("t2", "a2", "第二の要因も回復に寄与する。", 1.0),
                    candidate("t2", "b2", "第二の要因は回復を促す。", 0.94),
                ),
            },
        )

        self.assertEqual(selected["t2"].id, "b2")

        selected_with_margin, margins = SectionCandidateSelector.select_with_margins(
            (first, second),
            {
                "t1": (
                    candidate("t1", "a1", "第一の要因は回復に寄与する。", 1.0),
                ),
                "t2": (
                    candidate("t2", "a2", "第二の要因も回復に寄与する。", 1.0),
                    candidate("t2", "b2", "第二の要因は回復を促す。", 0.94),
                ),
            },
        )
        self.assertEqual(selected_with_margin["t2"].id, "b2")
        self.assertGreater(margins["t2"], 0)

    def test_combined_boundary_repetition_restores_original_block(self) -> None:
        source = "前提を参照すれば、それは制度の作用である。"
        phrase = "前提を参照すれば、"
        target = target_fixture(
            source_text=phrase,
            end_offset=len(phrase),
            terminology=(),
        )
        candidate = WordingCandidate(
            id="duplicate",
            target_id=target.id,
            ordinal=1,
            text="前提を参照すれば、それは、",
            relation_type="contribution",
            rationale="fixture",
            confidence=1,
            source_kind="llm_jp",
            total_score=1,
        )

        replacements, audits, reverted = (
            CandidateApplicationService.compose_block_replacements(
                '<article id="D" lang="ja"><p id="P">'
                + source
                + "</p></article>",
                (target,),
                {target.id: candidate},
            )
        )

        self.assertFalse(replacements)
        self.assertEqual(reverted, {target.id})
        self.assertIn("veto.japanese_surface_quality", audits[0]["issues"])


if __name__ == "__main__":
    unittest.main()
