from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fukamiru.errors import FukamiruError
from fukamiru.local_llm.proofreading import (
    JapaneseProofreader,
    ProofreadingRepository,
    bounded_repair_feedback,
)
from fukamiru.storage.database import SQLiteDatabase


class FakeCompletion:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def complete_json(self, **_: object) -> dict[str, object]:
        return self.payload


class CapturingCompletion(FakeCompletion):
    def __init__(self, payload: dict[str, object]) -> None:
        super().__init__(payload)
        self.calls: list[dict[str, object]] = []

    def complete_json(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return super().complete_json(**kwargs)


class JapaneseProofreadingTests(unittest.TestCase):
    def test_repair_feedback_is_bounded_before_building_local_model_prompt(self) -> None:
        feedback = tuple(
            f"surface.issue_{index}: " + (f"長い棄却理由{index}" * 80)
            for index in range(100)
        )
        adapter = CapturingCompletion({"edits": [], "confidence": 0.99})

        result = JapaneseProofreader(adapter).proofread(
            "整った文章である。",
            repair_feedback=feedback,
        )

        prompt = str(adapter.calls[0]["user_prompt"])
        bounded = bounded_repair_feedback(feedback)
        self.assertEqual(result.status, "accepted")
        self.assertLessEqual(len(bounded), 8)
        self.assertLessEqual(sum(len(item) for item in bounded) + len(bounded) - 1, 800)
        self.assertIn(bounded[0], prompt)
        self.assertNotIn("surface.issue_99", prompt)

    def test_prompt_allows_bounded_nominalization_edits(self) -> None:
        class Capture(FakeCompletion):
            def complete_json(self, **kwargs: object) -> dict[str, object]:
                self.kwargs = kwargs
                return self.payload

        capture = Capture({"edits": [], "confidence": 0.9})

        JapaneseProofreader(capture).proofread("資料の分析を行う。")

        prompt = str(capture.kwargs["system_prompt"])
        schema = capture.kwargs["json_schema"]
        self.assertIn("過度な名詞化", prompt)
        self.assertIn("editsとconfidenceだけ", prompt)
        self.assertEqual(set(schema["required"]), {"edits", "confidence"})
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["confidence"]["type"], "number")
        self.assertIn(
            "nominalization",
            schema["properties"]["edits"]["items"]["properties"]["category"]["enum"],
        )

    def test_edit_only_contract_is_reconstructed_locally(self) -> None:
        source = "この要因は結果に影響を与える。"
        result = JapaneseProofreader(
            FakeCompletion(
                {
                    "edits": [
                        {
                            "before": "影響を与える",
                            "after": "影響を及ぼす",
                            "reason": "自然なコロケーション",
                            "category": "collocation",
                        }
                    ],
                    "reasons": ["自然なコロケーションへ限定修正"],
                    "confidence": 0.98,
                }
            )
        ).proofread(source)

        self.assertEqual(result.edited_text, "この要因は結果に影響を及ぼす。")
        self.assertEqual(result.status, "accepted")

    def test_edit_count_is_bounded_even_for_non_schema_test_transport(self) -> None:
        edits = [
            {
                "before": str(index),
                "after": str(index),
                "reason": "fixture",
                "category": "grammar",
            }
            for index in range(6)
        ]
        with self.assertRaisesRegex(Exception, "too many edits"):
            JapaneseProofreader(
                FakeCompletion(
                    {
                        "edits": edits,
                        "reasons": [],
                        "confidence": 0.99,
                    }
                )
            ).proofread("012345", max_edits=5)

    def test_allowed_collocation_edit_is_accepted_and_persisted(self) -> None:
        source = "この要因は結果に影響を与える。"
        proofreader = JapaneseProofreader(
            FakeCompletion(
                {
                    "edited_text": "この要因は結果に影響を及ぼす。",
                    "edits": [
                        {
                            "before": "影響を与える",
                            "after": "影響を及ぼす",
                            "reason": "自然なコロケーション",
                            "category": "collocation",
                        }
                    ],
                    "reasons": ["自然なコロケーションへ限定修正"],
                    "confidence": 0.98,
                }
            )
        )
        result = proofreader.proofread(source)
        self.assertEqual(result.status, "accepted")
        self.assertFalse(result.issues)
        self.assertIn("影響を及ぼす", result.diff)

        with tempfile.TemporaryDirectory() as directory:
            migrations = Path(__file__).resolve().parents[1] / "migrations" / "knowledge"
            database = SQLiteDatabase(Path(directory) / "knowledge.db", migrations)
            database.migrate()
            with database.connect() as connection:
                revision_id = ProofreadingRepository(connection).save(
                    result,
                    model_name="llm-jp",
                    model_identity="fixture-model",
                )
                row = connection.execute(
                    "SELECT * FROM proofreading_revisions WHERE id = ?", (revision_id,)
                ).fetchone()
            self.assertEqual(row["status"], "accepted")
            self.assertIn("自然なコロケーション", row["reasons_json"])

    def test_modality_change_is_rejected(self) -> None:
        result = JapaneseProofreader(
            FakeCompletion(
                {
                    "edited_text": "この仮説は必ず正しい。",
                    "edits": [
                        {
                            "before": "可能性がある",
                            "after": "必ず正しい",
                            "reason": "簡潔化",
                            "category": "collocation",
                        }
                    ],
                    "reasons": [],
                    "confidence": 0.99,
                }
            )
        ).proofread("この仮説は可能性がある。")
        self.assertEqual(result.status, "rejected")
        self.assertIn("modality_changed", {issue.code for issue in result.issues})

    def test_single_reason_string_is_preserved_as_one_reason(self) -> None:
        result = JapaneseProofreader(
            FakeCompletion(
                {
                    "edited_text": "この結果は研究に大きな影響を与えた。",
                    "edits": [
                        {
                            "before": "強い",
                            "after": "大きな",
                            "reason": "自然なコロケーション",
                            "category": "collocation",
                        }
                    ],
                    "reasons": "自然なコロケーションへ限定修正",
                    "confidence": 0.95,
                }
            )
        ).proofread("この結果は研究に強い影響を与えた。")
        self.assertEqual(result.reasons, ("自然なコロケーションへ限定修正",))

    def test_named_confidence_from_real_model_is_normalized(self) -> None:
        source = "変更はありません。"
        result = JapaneseProofreader(
            FakeCompletion(
                {
                    "edited_text": source,
                    "edits": [],
                    "reasons": ["修正不要"],
                    "confidence": "high",
                }
            )
        ).proofread(source)
        self.assertEqual(result.confidence, 0.95)
        self.assertEqual(result.status, "accepted")

    def test_noop_edit_is_rejected_by_surface_gate(self) -> None:
        source = "この結果を説明する。"
        result = JapaneseProofreader(
            FakeCompletion(
                {
                    "edits": [
                        {
                            "before": "説明する",
                            "after": "説明する",
                            "reason": "変更なし",
                            "category": "grammar",
                        }
                    ],
                    "confidence": 0.99,
                }
            )
        ).proofread(source)

        self.assertEqual(result.status, "rejected")
        self.assertIn("surface.noop_edit", {issue.code for issue in result.issues})

    def test_ambiguous_source_fragment_is_rejected(self) -> None:
        result = JapaneseProofreader(
            FakeCompletion(
                {
                    "edits": [
                        {
                            "before": "接続する",
                            "after": "関連づける",
                            "reason": "関係を具体化",
                            "category": "collocation",
                        }
                    ],
                    "confidence": 0.99,
                }
            )
        ).proofread("前提に接続する。結論に接続する。")

        self.assertEqual(result.status, "rejected")
        self.assertIn(
            "edit_source_fragment_ambiguous",
            {issue.code for issue in result.issues},
        )

    def test_overlapping_source_occurrences_are_also_ambiguous(self) -> None:
        result = JapaneseProofreader(
            FakeCompletion(
                {
                    "edits": [
                        {
                            "before": "aa",
                            "after": "b",
                            "reason": "overlapping fixture",
                            "category": "orthography",
                        }
                    ],
                    "confidence": 0.99,
                }
            )
        ).proofread("aaa")

        self.assertEqual(result.status, "rejected")
        self.assertIn(
            "edit_source_fragment_ambiguous",
            {issue.code for issue in result.issues},
        )

    def test_non_finite_confidence_is_rejected(self) -> None:
        with self.assertRaisesRegex(FukamiruError, "non-finite confidence"):
            JapaneseProofreader(
                FakeCompletion({"edits": [], "confidence": "NaN"})
            ).proofread("整った文章である。")

    def test_generated_anchor_and_out_of_order_edits_are_rejected(self) -> None:
        generated_anchor = JapaneseProofreader(
            FakeCompletion(
                {
                    "edits": [
                        {
                            "before": "説明する",
                            "after": "媒介語",
                            "reason": "第一差分",
                            "category": "collocation",
                        },
                        {
                            "before": "媒介語",
                            "after": "論証する",
                            "reason": "生成文字列を再置換",
                            "category": "collocation",
                        },
                    ],
                    "confidence": 0.99,
                }
            )
        ).proofread("前提を説明する。結論を示す。")
        out_of_order = JapaneseProofreader(
            FakeCompletion(
                {
                    "edits": [
                        {
                            "before": "結論を示す",
                            "after": "結論を明示する",
                            "reason": "後方差分",
                            "category": "collocation",
                        },
                        {
                            "before": "前提を説明する",
                            "after": "前提を詳述する",
                            "reason": "前方差分",
                            "category": "collocation",
                        },
                    ],
                    "confidence": 0.99,
                }
            )
        ).proofread("前提を説明する。結論を示す。")

        self.assertIn(
            "edit_contract_mismatch",
            {issue.code for issue in generated_anchor.issues},
        )
        self.assertIn(
            "edit_spans_overlap_or_out_of_order",
            {issue.code for issue in out_of_order.issues},
        )

    def test_replacement_boundary_duplication_is_rejected(self) -> None:
        source = "透明な入口を設ける。"
        result = JapaneseProofreader(
            FakeCompletion(
                {
                    "edits": [
                        {
                            "before": "透明",
                            "after": "透明な",
                            "reason": "形容表現",
                            "category": "grammar",
                        }
                    ],
                    "confidence": 0.99,
                }
            )
        ).proofread(source)

        self.assertEqual(result.status, "rejected")
        self.assertIn(
            "surface.replacement_boundary_repetition",
            {issue.code for issue in result.issues},
        )

    def test_empty_edit_reason_is_rejected(self) -> None:
        result = JapaneseProofreader(
            FakeCompletion(
                {
                    "edits": [
                        {
                            "before": "説明する",
                            "after": "詳述する",
                            "reason": "",
                            "category": "collocation",
                        }
                    ],
                    "confidence": 0.99,
                }
            )
        ).proofread("前提を説明する。")

        self.assertEqual(result.status, "rejected")
        self.assertIn("edit_reason_missing", {issue.code for issue in result.issues})

    def test_numbers_quotations_terminology_and_names_are_rejected(self) -> None:
        source = "田中は「重要」と述べ、BGE-M3を12回使用した。"
        result = JapaneseProofreader(
            FakeCompletion(
                {
                    "edited_text": "佐藤は「不要」と述べ、BGE-M3を13回使用した。",
                    "edits": [
                        {
                            "before": "田中は「重要」と述べ、BGE-M3を12回",
                            "after": "佐藤は「不要」と述べ、BGE-M3を13回",
                            "reason": "表現調整",
                            "category": "grammar",
                        }
                    ],
                    "reasons": [],
                    "confidence": 0.99,
                }
            )
        ).proofread(source, terminology=("BGE-M3",), named_entities=("田中",))
        codes = {issue.code for issue in result.issues}
        self.assertEqual(result.status, "rejected")
        self.assertIn("numbers_changed", codes)
        self.assertIn("quotations_changed", codes)
        self.assertIn("named_entity_changed", codes)


if __name__ == "__main__":
    unittest.main()
