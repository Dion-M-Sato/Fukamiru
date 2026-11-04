from __future__ import annotations

import unittest

from fukamiru.knowledge.batching import EmbeddingBatchItem, plan_embedding_batches


def item(chunk_id: str, tokens: int, ordinal: int) -> EmbeddingBatchItem:
    return EmbeddingBatchItem(chunk_id, f"text-{chunk_id}", tokens, ordinal)


class EmbeddingBatchPlanningTests(unittest.TestCase):
    def test_fixed_batches_preserve_order_and_report_padding(self) -> None:
        batches = plan_embedding_batches(
            [item("a", 2, 0), item("b", 5, 1), item("c", 4, 2)],
            strategy="fixed",
            batch_size=2,
        )
        self.assertEqual(
            [[entry.chunk_id for entry in batch.items] for batch in batches], [["a", "b"], ["c"]]
        )
        self.assertEqual(batches[0].actual_token_count, 7)
        self.assertEqual(batches[0].padded_token_count, 10)
        self.assertEqual(batches[0].padding_token_count, 3)
        self.assertAlmostEqual(batches[0].padding_ratio, 10 / 7)

    def test_length_bucketing_keeps_each_chunk_identity(self) -> None:
        source = [
            item("long", 900, 0),
            item("short", 100, 1),
            item("medium", 500, 2),
            item("shorter", 80, 3),
        ]
        batches = plan_embedding_batches(
            source,
            strategy="length_bucket",
            batch_size=2,
            bucket_boundaries=(256, 512, 768, 1024),
        )
        planned = [entry.chunk_id for batch in batches for entry in batch.items]
        self.assertEqual(set(planned), {entry.chunk_id for entry in source})
        self.assertEqual(len(planned), len(set(planned)))
        self.assertEqual(planned[:2], ["shorter", "short"])

    def test_token_budget_limits_padded_tensor_and_allows_oversize_singleton(self) -> None:
        batches = plan_embedding_batches(
            [item("a", 100, 0), item("b", 110, 1), item("c", 400, 2), item("d", 1200, 3)],
            strategy="token_budget",
            batch_size=8,
            max_tokens_per_batch=512,
            bucket_boundaries=(256, 512, 1024),
        )
        self.assertTrue(
            all(batch.padded_token_count <= 512 or len(batch.items) == 1 for batch in batches)
        )
        self.assertEqual(
            {entry.chunk_id for batch in batches for entry in batch.items}, {"a", "b", "c", "d"}
        )

    def test_invalid_strategy_and_budget_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported"):
            plan_embedding_batches([], strategy="unknown", batch_size=8)
        with self.assertRaisesRegex(ValueError, "max_tokens"):
            plan_embedding_batches([], strategy="token_budget", batch_size=8)


if __name__ == "__main__":
    unittest.main()
