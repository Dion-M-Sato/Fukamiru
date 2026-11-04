"""Deterministic embedding batch planning with stable chunk identity."""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Iterable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class EmbeddingBatchItem:
    """One chunk plus its immutable identity and measured token length."""

    chunk_id: str
    text: str
    token_count: int
    ordinal: int


@dataclass(frozen=True)
class EmbeddingBatch:
    items: tuple[EmbeddingBatchItem, ...]
    actual_token_count: int
    padded_token_count: int

    @property
    def padding_token_count(self) -> int:
        return self.padded_token_count - self.actual_token_count

    @property
    def padding_ratio(self) -> float:
        """Padded/actual work ratio (1.0 means no padding overhead)."""
        if self.actual_token_count <= 0:
            return 0.0
        return self.padded_token_count / self.actual_token_count


def _validated_boundaries(boundaries: Sequence[int]) -> tuple[int, ...]:
    result = tuple(int(value) for value in boundaries)
    if any(value < 1 for value in result):
        raise ValueError("embedding length bucket boundaries must be positive")
    if tuple(sorted(set(result))) != result:
        raise ValueError("embedding length bucket boundaries must be unique and increasing")
    return result


def _make_batch(items: Sequence[EmbeddingBatchItem]) -> EmbeddingBatch:
    actual = sum(item.token_count for item in items)
    padded = max((item.token_count for item in items), default=0) * len(items)
    return EmbeddingBatch(tuple(items), actual, padded)


def _length_order(
    items: Sequence[EmbeddingBatchItem], boundaries: Sequence[int]
) -> list[EmbeddingBatchItem]:
    checked = _validated_boundaries(boundaries)
    return sorted(
        items,
        key=lambda item: (
            bisect_left(checked, item.token_count),
            item.token_count,
            item.ordinal,
        ),
    )


def plan_embedding_batches(
    items: Iterable[EmbeddingBatchItem],
    *,
    strategy: str,
    batch_size: int,
    max_tokens_per_batch: int = 0,
    bucket_boundaries: Sequence[int] = (256, 384, 512, 768, 1024),
) -> list[EmbeddingBatch]:
    """Plan deterministic batches while retaining chunk IDs on every vector input.

    ``max_tokens_per_batch`` limits padded tokens, not merely the sum of actual
    tokens. This models the rectangular tensor sent to the transformer.
    """
    if batch_size < 1:
        raise ValueError("embedding batch size must be positive")
    materialized = list(items)
    if any(item.token_count < 1 for item in materialized):
        raise ValueError("embedding item token counts must be positive")
    if strategy == "fixed":
        ordered = materialized
    elif strategy in {"length_bucket", "token_budget"}:
        ordered = _length_order(materialized, bucket_boundaries)
    else:
        raise ValueError(f"unsupported embedding batch strategy: {strategy}")

    if strategy != "token_budget":
        return [
            _make_batch(ordered[offset : offset + batch_size])
            for offset in range(0, len(ordered), batch_size)
        ]
    if max_tokens_per_batch < 1:
        raise ValueError("token_budget strategy requires max_tokens_per_batch > 0")

    batches: list[EmbeddingBatch] = []
    pending: list[EmbeddingBatchItem] = []
    pending_max = 0
    for item in ordered:
        proposed_max = max(pending_max, item.token_count)
        proposed_size = len(pending) + 1
        exceeds_tokens = proposed_max * proposed_size > max_tokens_per_batch
        if pending and (proposed_size > batch_size or exceeds_tokens):
            batches.append(_make_batch(pending))
            pending = []
            pending_max = 0
        pending.append(item)
        pending_max = max(pending_max, item.token_count)
    if pending:
        batches.append(_make_batch(pending))
    return batches
