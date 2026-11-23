"""Deterministic Information-Retrieval metrics (``core/eval_and_ops.md`` §1.2).

These metrics are computed purely from the ordered list of retrieved ``chunk_id`` values and the
QRELS relevance set (``ground_truth_chunk_ids``). They are exact, dependency-free and deterministic
(no numpy, no LLM), which makes the retrieval half of the gate reproducible and fast:

- ``Hit Rate@k`` - was at least one reference chunk retrieved in the Top-k;
- ``MRR@k`` - reciprocal rank of the first reference chunk in the Top-k;
- ``nDCG@k`` - normalized discounted cumulative gain with binary relevance.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from math import log2


def _top_k(retrieved: Sequence[str], k: int) -> list[str]:
    if k <= 0:
        raise ValueError(f"k must be a positive integer, got {k}")
    return list(retrieved[:k])


def hit_rate_at_k(retrieved: Sequence[str], relevant: Iterable[str], k: int = 5) -> float:
    """1.0 if any reference chunk appears in the Top-k, else 0.0 (per-query hit)."""
    relevant_set = set(relevant)
    if not relevant_set:
        return 0.0
    return 1.0 if any(chunk_id in relevant_set for chunk_id in _top_k(retrieved, k)) else 0.0


def mrr_at_k(retrieved: Sequence[str], relevant: Iterable[str], k: int = 5) -> float:
    """Reciprocal rank (1/rank) of the first reference chunk within the Top-k, else 0.0."""
    relevant_set = set(relevant)
    if not relevant_set:
        return 0.0
    for index, chunk_id in enumerate(_top_k(retrieved, k), start=1):
        if chunk_id in relevant_set:
            return 1.0 / index
    return 0.0


def ndcg_at_k(retrieved: Sequence[str], relevant: Iterable[str], k: int = 5) -> float:
    """Binary-relevance nDCG@k.

    DCG sums ``1 / log2(rank + 1)`` over retrieved relevant chunks; the ideal DCG assumes all
    relevant chunks (capped at ``k``) are ranked first. Returns 0.0 when there are no judgements.
    """
    relevant_set = set(relevant)
    if not relevant_set:
        return 0.0

    dcg = 0.0
    for index, chunk_id in enumerate(_top_k(retrieved, k), start=1):
        if chunk_id in relevant_set:
            dcg += 1.0 / log2(index + 1)

    ideal_hits = min(len(relevant_set), k)
    idcg = sum(1.0 / log2(rank + 1) for rank in range(1, ideal_hits + 1))
    return dcg / idcg if idcg > 0 else 0.0


@dataclass(frozen=True, slots=True)
class IRMetrics:
    """Corpus-level IR metrics - the mean of the per-query scores."""

    hit_rate_at_5: float
    mrr_at_5: float
    ndcg_at_5: float
    query_count: int

    @classmethod
    def from_queries(
        cls,
        retrieved_per_query: Sequence[Sequence[str]],
        relevant_per_query: Sequence[Iterable[str]],
        k: int = 5,
    ) -> IRMetrics:
        """Aggregate per-query retrievals/judgements into mean Hit Rate@k, MRR@k and nDCG@k."""
        if len(retrieved_per_query) != len(relevant_per_query):
            raise ValueError("retrieved_per_query and relevant_per_query must be the same length")

        count = len(retrieved_per_query)
        if count == 0:
            return cls(0.0, 0.0, 0.0, 0)

        relevant_lists = [list(rel) for rel in relevant_per_query]
        paired = list(zip(retrieved_per_query, relevant_lists, strict=True))
        hit = sum(hit_rate_at_k(r, rel, k) for r, rel in paired)
        mrr = sum(mrr_at_k(r, rel, k) for r, rel in paired)
        ndcg = sum(ndcg_at_k(r, rel, k) for r, rel in paired)
        return cls(hit / count, mrr / count, ndcg / count, count)
