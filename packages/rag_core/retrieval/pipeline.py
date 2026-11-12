"""Stage-E search orchestration + composition root (`IMPLEMENTATION_PLAN.md` Stage E,
`core/architecture.md` §2 p.7-9).

The individual Stage-E components (PDP, retriever, reranker, confidence gate, hydrator) are pure,
independently testable units. :class:`SearchPipeline` wires them into the single end-to-end flow the
stage's Definition of Done describes:

    PDP.effective_access -> Retriever.retrieve -> Reranker.rerank -> ConfidenceGate -> Hydrator

so Stage E is callable as an integrated unit (not only component-wise). The PDP decision
(``effective_access_level`` + ``allowed_document_ids`` + ``denied_document_ids``) is computed once
on the backend and threaded into the search arms as a predicate BEFORE top-k/RRF; after reranking
the Confidence Gate short-circuits the whole flow (no hydration, no LLM) when the best candidate is
below τ - the "no information found" path.

:func:`build_search_pipeline` is the composition root: it constructs the concrete pgvector-backed
providers and returns a wired pipeline. The LLM/generation layer (Stage F) consumes a
:class:`SearchResult` - it is intentionally out of scope here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rag_core.contracts.interfaces import (
    BaseConfidenceGate,
    BaseEmbedder,
    BasePolicyEvaluator,
    BaseReranker,
    BaseRetriever,
    BaseVectorStore,
    Retriever,
)
from rag_core.contracts.models import ParentChunk, RetrievalCandidate, UserContext
from rag_core.contracts.settings import Settings
from rag_core.retrieval.confidence_gate import ConfidenceGate
from rag_core.retrieval.hydration import SmallToBigHydrator
from rag_core.retrieval.repository import (
    SessionFactory,
    SqlAlchemyParentRepository,
    SqlAlchemyPolicyEvaluator,
)
from rag_core.retrieval.reranker import DEFAULT_RERANK_MODEL, FlashRankReranker, RankFn
from rag_core.retrieval.vector_store import DEFAULT_EF_SEARCH, PgVectorStore


@dataclass(slots=True)
class SearchResult:
    """The outcome of a Stage-E search.

    - ``candidates`` - the reranked child candidates (best first), always populated so the caller
      can log retrieval telemetry even when the gate blocks;
    - ``is_confident`` - ``max(rerank_score) >= τ``; when ``False`` the flow short-circuits (the
      "no information found" answer) and ``parents`` stays empty - no hydration, no LLM call;
    - ``parents`` - the Small-to-Big hydrated, access-rechecked parent chunks (only when confident).
    """

    candidates: list[RetrievalCandidate]
    is_confident: bool
    parents: list[ParentChunk] = field(default_factory=list)


class SearchPipeline:
    """Orchestrates the Stage-E flow into a single callable unit."""

    def __init__(
        self,
        policy_evaluator: BasePolicyEvaluator,
        retriever: BaseRetriever,
        reranker: BaseReranker,
        confidence_gate: BaseConfidenceGate,
        hydrator: SmallToBigHydrator,
    ) -> None:
        self._policy = policy_evaluator
        self._retriever = retriever
        self._reranker = reranker
        self._gate = confidence_gate
        self._hydrator = hydrator

    async def search(self, user: UserContext, query: str) -> SearchResult:
        """Runs PDP -> retrieve -> rerank -> confidence gate -> hydrate for a single query.

        ``user_access_level``/``allowed_document_ids``/``denied_document_ids`` are taken from the
        PDP decision (never from the client). When the Confidence Gate fails, the result is returned
        immediately with ``is_confident=False`` and no parents (the flow does not hydrate or call an
        LLM for a low-confidence query).
        """
        access = await self._policy.effective_access(user)
        candidates = await self._retriever.retrieve(
            query,
            user.tenant_id,
            user_access_level=int(access.effective_access_level),
            allowed_document_ids=access.allowed_document_ids,
            denied_document_ids=access.denied_document_ids,
        )
        reranked = await self._reranker.rerank(query, candidates)
        if not self._gate.evaluate(reranked):
            return SearchResult(candidates=reranked, is_confident=False)
        parents = await self._hydrator.hydrate(user, reranked)
        return SearchResult(candidates=reranked, is_confident=True, parents=parents)


def build_search_pipeline(
    session_factory: SessionFactory,
    embedder: BaseEmbedder,
    settings: Settings | None = None,
    *,
    enterprise_acl: bool = False,
    rank_fn: RankFn | None = None,
    rerank_model: str = DEFAULT_RERANK_MODEL,
    ef_search: int = DEFAULT_EF_SEARCH,
    top_parents: int = 5,
) -> SearchPipeline:
    """Composition root: build a fully wired :class:`SearchPipeline` on pgvector + Postgres.

    The single DB ``session_factory`` is shared by the vector store, the parent repository and the
    PDP; the ``embedder`` (BGE-M3) is injected (it is the heavy, shared singleton). ``rank_fn`` lets
    a caller/test inject a deterministic reranker instead of the lazy FlashRank model.
    """
    resolved = settings or Settings()
    vector_store: BaseVectorStore = PgVectorStore(session_factory, resolved, ef_search=ef_search)
    retriever = Retriever(vector_store, embedder, resolved)
    reranker = FlashRankReranker(model_name=rerank_model, rank_fn=rank_fn)
    gate = ConfidenceGate(resolved)
    policy = SqlAlchemyPolicyEvaluator(session_factory, enterprise_acl=enterprise_acl)
    hydrator = SmallToBigHydrator(
        SqlAlchemyParentRepository(session_factory), policy, top_parents=top_parents
    )
    return SearchPipeline(policy, retriever, reranker, gate, hydrator)
