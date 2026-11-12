"""Small-to-Big hydration + access re-check (`IMPLEMENTATION_PLAN.md` Stage E,
`core/architecture.md` §2 p.9).

After reranking, the best child chunks (Top 3-5) are expanded to their full ``parent_chunks`` so the
LLM gets coherent context (Small-to-Big). Two invariants are enforced here:

1. **Defense-in-depth access re-check**: every hydrated parent is re-checked through
   :meth:`BasePolicyEvaluator.can_access_document` (fail-closed). This closes direct access to
   parent content that bypasses the child-arm filter - inaccessible parents are dropped, even if a
   child somehow surfaced them.
2. **Deduplication**: multiple children often map to the same parent; parents are de-duplicated by
   ``parent_id`` (and identical content collapsed), preserving the rerank order.

The DB access sits behind the :class:`BaseParentRepository` port so the hydrator stays pure and
unit-testable with an in-memory fake; the concrete SQLAlchemy implementation lives in
:mod:`rag_core.retrieval.repository`.
"""

from __future__ import annotations

from uuid import UUID

from rag_core.contracts.interfaces import BaseParentRepository, BasePolicyEvaluator
from rag_core.contracts.models import ParentChunk, RetrievalCandidate, UserContext

# Re-exported for backwards compatibility; the canonical home is `contracts.interfaces`.
__all__ = ["BaseParentRepository", "SmallToBigHydrator"]


class SmallToBigHydrator:
    """Expands reranked child candidates to their parent chunks, with an access re-check."""

    def __init__(
        self,
        repository: BaseParentRepository,
        policy_evaluator: BasePolicyEvaluator,
        top_parents: int = 5,
    ) -> None:
        self._repository = repository
        self._policy = policy_evaluator
        self._top_parents = top_parents

    async def hydrate(
        self,
        user: UserContext,
        candidates: list[RetrievalCandidate],
    ) -> list[ParentChunk]:
        """Maps the best children to deduplicated, access-checked parent chunks (Top 3-5).

        ``candidates`` are expected already reranked (best first). Parents are collected in that
        order, each re-checked via ``PolicyEvaluator.can_access_document`` (fail-closed), and
        duplicate content is collapsed. The cap counts only *accessible* parents: if the first
        candidates map to inaccessible/duplicate parents, the hydrator keeps scanning further down
        the reranked list until ``top_parents`` accessible parents are collected (backfill), rather
        than capping the raw candidate order and returning fewer than available.
        """
        # Deduplicate parent_ids preserving the incoming (rerank) order (no early cap: the cap is
        # applied AFTER the access re-check, so dropped parents are backfilled from further down).
        ordered_parent_ids: list[UUID] = []
        seen_parents: set[UUID] = set()
        for candidate in candidates:
            if candidate.parent_id in seen_parents:
                continue
            seen_parents.add(candidate.parent_id)
            ordered_parent_ids.append(candidate.parent_id)

        if not ordered_parent_ids:
            return []

        fetched = await self._repository.fetch_parents(user.tenant_id, ordered_parent_ids)
        by_id = {parent.id: parent for parent in fetched}

        hydrated: list[ParentChunk] = []
        seen_content: set[str] = set()
        for parent_id in ordered_parent_ids:
            if len(hydrated) >= self._top_parents:
                break
            parent = by_id.get(parent_id)
            if parent is None:
                continue
            # Defense-in-depth: re-check each parent through the PDP (fail-closed).
            if not await self._policy.can_access_document(user, parent.document_id):
                continue
            if parent.content in seen_content:
                continue
            seen_content.add(parent.content)
            hydrated.append(parent)
        return hydrated
