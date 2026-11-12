"""Confidence Gate (`IMPLEMENTATION_PLAN.md` Stage E, `core/architecture.md` §2 p.8).

:class:`ConfidenceGate` is the concrete :class:`BaseConfidenceGate` provider. After reranking it
checks ``max(rerank_score) >= threshold``; when the best candidate is below the threshold the
pipeline aborts the search and the user receives a "no information found" answer (τ=0.35 by
default, read from ``Settings.confidence_threshold`` - the single source of the value, no hardcode).

Fail-closed: an empty candidate set (or candidates without a ``rerank_score``) never passes.
"""

from __future__ import annotations

from rag_core.contracts.interfaces import BaseConfidenceGate
from rag_core.contracts.models import RetrievalCandidate
from rag_core.contracts.settings import Settings


class ConfidenceGate(BaseConfidenceGate):
    """Thresholds the reranked candidates on ``max(rerank_score)``."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or Settings()

    def evaluate(
        self, candidates: list[RetrievalCandidate], threshold: float | None = None
    ) -> bool:
        """True iff the best reranked candidate meets the threshold.

        ``threshold=None`` uses ``Settings.confidence_threshold``. Candidates missing a
        ``rerank_score`` are ignored; if none carry a score the gate fails closed (returns False).
        """
        tau = self._settings.confidence_threshold if threshold is None else threshold
        scores = [c.rerank_score for c in candidates if c.rerank_score is not None]
        if not scores:
            return False
        return max(scores) >= tau
