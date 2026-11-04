from __future__ import annotations

import unittest

from fukamiru.translation.research import (
    ResearchTriggerDetector,
    TranslationComplianceValidator,
)


class TranslationResearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source_phrase = (
            "L’opération éthique a lieu à travers la chair émotionnelle du jeu"
        )
        self.instructions = (
            "Maxime Deslongchamps-Gagnon の博士論文 Jeux édifiants, vertu et "
            "émotion : une éthique vidéoludique に関連することを調査する。"
            "Deslongchamps-Gagnonは、デロンシャン=ガニョンというカタカナにすべき。"
            "説明のある「倫理的作用はゲームの感情的な肉を通じて生じる」="
            f" {self.source_phrase}という一文は直訳しない。"
            "よって「倫理的な働きはゲームの情動を血肉化することで生じる」"
            "といった文にすべき。"
        )
        self.segments = [
            {
                "source_span_id": "span-1",
                "text": (
                    "Deslongchamps-Gagnon développe cette approche. "
                    + self.source_phrase
                    + "."
                ),
            }
        ]

    def test_detects_research_person_and_explicit_concept_decisions(self) -> None:
        triggers = ResearchTriggerDetector().detect(self.segments, self.instructions)

        mapped_name = next(
            trigger
            for trigger in triggers
            if trigger.trigger_type == "person"
            and trigger.source_text == "Deslongchamps-Gagnon"
        )
        concept = next(
            trigger
            for trigger in triggers
            if trigger.trigger_type == "concept"
            and "chair émotionnelle" in trigger.source_text
        )

        self.assertEqual(mapped_name.metadata["explicit_target"], "デロンシャン=ガニョン")
        self.assertEqual(concept.metadata["explicit_target"], (
            "倫理的な働きはゲームの情動を血肉化することで生じる"
        ))
        self.assertEqual(
            concept.metadata["forbidden_translations"],
            ["倫理的作用はゲームの感情的な肉を通じて生じる"],
        )
        self.assertTrue(
            any(
                trigger.trigger_type == "research"
                and "Jeux édifiants" in trigger.source_text
                for trigger in triggers
            )
        )

    def test_game_titles_are_not_misclassified_as_people(self) -> None:
        triggers = ResearchTriggerDetector().detect(
            [
                {
                    "source_span_id": "span-game",
                    "text": "Spec Ops: The Line et The Walking Dead sont comparés.",
                }
            ],
            "ゲーム作品名は原綴りを保つ。",
        )

        self.assertFalse(any(trigger.trigger_type == "person" for trigger in triggers))

    def test_compliance_rejects_literal_phrase_remaining_name_and_missing_decision(
        self,
    ) -> None:
        decisions = [
            {
                "id": "concept-1",
                "decision_kind": "term",
                "source_term": self.source_phrase,
                "target_term": "倫理的な働きはゲームの情動を血肉化することで生じる",
                "scope_type": "document",
                "scope_key": "doc-1",
                "required": True,
                "forbidden_translations": [
                    "倫理的作用はゲームの感情的な肉を通じて生じる"
                ],
            },
            {
                "id": "person-1",
                "decision_kind": "person_name",
                "source_term": "Deslongchamps-Gagnon",
                "target_term": "デロンシャン=ガニョン",
                "scope_type": "author",
                "scope_key": "deslongchamps-gagnon",
                "required": True,
                "forbidden_translations": [],
            },
        ]
        findings = TranslationComplianceValidator().validate(
            source_text=self.segments[0]["text"],
            target_text=(
                "Deslongchamps-Gagnonによれば、倫理的作用はゲームの感情的な肉を"
                "通じて生じる。"
            ),
            decisions=decisions,
            person_triggers=[
                {"trigger_type": "person", "source_text": "Deslongchamps-Gagnon"}
            ],
        )

        finding_types = {finding.finding_type for finding in findings}
        self.assertEqual(
            finding_types,
            {
                "forbidden_translation",
                "person_name_remaining",
                "decision_not_applied",
            },
        )

    def test_compliance_accepts_scoped_decisions(self) -> None:
        findings = TranslationComplianceValidator().validate(
            source_text=self.segments[0]["text"],
            target_text=(
                "デロンシャン=ガニョンによれば、倫理的な働きはゲームの情動を"
                "血肉化することで生じる。"
            ),
            decisions=[
                {
                    "id": "concept-1",
                    "decision_kind": "term",
                    "source_term": self.source_phrase,
                    "target_term": (
                        "倫理的な働きはゲームの情動を血肉化することで生じる"
                    ),
                    "scope_type": "document",
                    "scope_key": "doc-1",
                    "required": True,
                    "forbidden_translations": ["感情的な肉"],
                },
                {
                    "id": "person-1",
                    "decision_kind": "person_name",
                    "source_term": "Deslongchamps-Gagnon",
                    "target_term": "デロンシャン=ガニョン",
                    "scope_type": "author",
                    "scope_key": "deslongchamps-gagnon",
                    "required": True,
                    "forbidden_translations": [],
                },
            ],
            person_triggers=[
                {"trigger_type": "person", "source_text": "Deslongchamps-Gagnon"}
            ],
        )

        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
