"""FlashRank cross-encoder reranker (`IMPLEMENTATION_PLAN.md` Stage E, `core/architecture.md` §2).

:class:`FlashRankReranker` is the concrete :class:`BaseReranker` provider. It re-scores the fused
Top-N candidates with a cross-encoder (FlashRank / bge-reranker-small) and sorts them by the fresh
``rerank_score`` (descending), so the Confidence Gate can threshold ``max(rerank_score)``.

The heavy ``flashrank`` dependency is imported lazily and built on first use, so this module (and
the whole retrieval package) imports and unit-tests without the model installed. Tests inject a
lightweight ``rank_fn`` to exercise the scoring/ordering logic deterministically (mirrors the
``encode_fn`` seam of :class:`rag_core.ingestion.embedder.BgeM3Embedder`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from rag_core.contracts.interfaces import BaseReranker
from rag_core.contracts.models import RetrievalCandidate

# A rank function maps (query, passages) to a parallel list of relevance scores (one per passage).
RankFn = Callable[[str, list[str]], list[float]]

# The canonical FlashRank model (small, CPU-friendly); matches core/architecture.md §2 p.8.
DEFAULT_RERANK_MODEL = "ms-marco-MiniLM-L-12-v2"


class FlashRankReranker(BaseReranker):
    """The FlashRank :class:`BaseReranker` provider.

    ``rank_fn`` is the single-call scorer. When omitted, a lazily-constructed ``flashrank.Ranker``
    is used (imported only on first ``rerank``).
    """

    def __init__(
        self,
        model_name: str = DEFAULT_RERANK_MODEL,
        rank_fn: RankFn | None = None,
    ) -> None:
        self._model_name = model_name
        self._rank_fn = rank_fn
        self._ranker: object | None = None

    @property
    def model_name(self) -> str:
        return self._model_name

    async def rerank(
        self, query: str, candidates: list[RetrievalCandidate]
    ) -> list[RetrievalCandidate]:
        """Re-scores candidates with the cross-encoder and returns them sorted by rerank_score.

        The input candidates are not mutated: fresh copies carry the ``rerank_score``, so the
        caller's fused list keeps its RRF signals intact.

        The cross-encoder pass is CPU-bound; it is offloaded to a worker thread
        (``asyncio.to_thread``) so a 30-60 passage rerank never blocks the event loop under load
        (mirrors the DB offloading in ``vector_store``/``repository``).
        """
        if not candidates:
            return []
        rank = self._resolve_rank_fn()
        scores = await asyncio.to_thread(rank, query, [c.content for c in candidates])
        if len(scores) != len(candidates):
            raise ValueError("rank_fn must return one score per candidate")
        reranked = [
            candidate.model_copy(update={"rerank_score": float(score)})
            for candidate, score in zip(candidates, scores, strict=True)
        ]
        # Stable sort: equal rerank_score keeps the incoming (RRF) order.
        return sorted(reranked, key=lambda c: c.rerank_score or 0.0, reverse=True)

    def _resolve_rank_fn(self) -> RankFn:
        if self._rank_fn is not None:
            return self._rank_fn
        self._rank_fn = self._build_flashrank_ranker()
        return self._rank_fn

    def _build_flashrank_ranker(self) -> RankFn:
        """Lazily constructs the FlashRank ``Ranker`` and wraps it as a ``RankFn``."""
        try:
            from flashrank import Ranker, RerankRequest
        except ImportError as exc:  # pragma: no cover - exercised only without the heavy dep
            raise RuntimeError(
                "flashrank is not installed; install it or inject a rank_fn (pip install flashrank)"
            ) from exc

        ranker = Ranker(model_name=self._model_name)
        self._ranker = ranker

        def _rank(query: str, passages: list[str]) -> list[float]:
            request = RerankRequest(
                query=query,
                passages=[{"id": i, "text": text} for i, text in enumerate(passages)],
            )
            results = ranker.rerank(request)
            # FlashRank returns the passages re-ordered; realign the scores to the input order.
            scores = [0.0] * len(passages)
            for item in results:
                scores[int(item["id"])] = float(item["score"])
            return scores

        return _rank
