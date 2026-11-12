"""Stage E - Retrieval, Reranker & Confidence Gate (`core/architecture.md` §2 p.7-9).

This subpackage implements the hybrid-retrieval half of the query flow:

- `vector_store`     - the `BaseVectorStore` port on Postgres + pgvector (`PgVectorStore`): separate
  dense (HNSW) and sparse (`sparsevec`) arms with the tenant + ``access_level`` + optional
  ``allowed_document_ids`` filter applied BEFORE ``LIMIT`` (so top-k/RRF stay correct),
  ``ef_search`` tuning and the near-zero sparse-norm cutoff. RRF fusion itself lives in app-layer
  (`Retriever.fuse_rrf`, in `rag_core.contracts.interfaces`), not in the store;
- `reranker`         - the FlashRank cross-encoder `BaseReranker` (`FlashRankReranker`) with an
  injectable ``rank_fn`` (the heavy ``flashrank`` dep is imported lazily);
- `confidence_gate`  - the `BaseConfidenceGate` (`ConfidenceGate`): thresholds ``max(rerank_score)``
  on τ (``Settings.confidence_threshold``, default 0.35), fail-closed;
- `hydration`        - Small-to-Big expansion (`SmallToBigHydrator`) with a per-parent access
  re-check through `PolicyEvaluator.can_access_document` plus the `BaseParentRepository` port;
- `repository`       - the concrete SQLAlchemy halves (`SqlAlchemyParentRepository`,
  `SqlAlchemyPolicyEvaluator` - the single PDP).

Heavy third-party dependencies (FlashRank) are imported lazily so the package stays importable and
unit-testable with in-memory fakes and no external services.
"""

from __future__ import annotations

from rag_core.retrieval.confidence_gate import ConfidenceGate
from rag_core.retrieval.hydration import BaseParentRepository, SmallToBigHydrator
from rag_core.retrieval.pipeline import (
    SearchPipeline,
    SearchResult,
    build_search_pipeline,
)
from rag_core.retrieval.repository import (
    SqlAlchemyParentRepository,
    SqlAlchemyPolicyEvaluator,
)
from rag_core.retrieval.reranker import (
    DEFAULT_RERANK_MODEL,
    FlashRankReranker,
    RankFn,
)
from rag_core.retrieval.vector_store import (
    DEFAULT_EF_SEARCH,
    SPARSE_NORM_CUTOFF,
    PgVectorStore,
)

__all__ = [
    # vector store
    "PgVectorStore",
    "DEFAULT_EF_SEARCH",
    "SPARSE_NORM_CUTOFF",
    # reranker
    "FlashRankReranker",
    "RankFn",
    "DEFAULT_RERANK_MODEL",
    # confidence gate
    "ConfidenceGate",
    # hydration
    "BaseParentRepository",
    "SmallToBigHydrator",
    # concrete persistence
    "SqlAlchemyParentRepository",
    "SqlAlchemyPolicyEvaluator",
    # orchestration / composition root
    "SearchPipeline",
    "SearchResult",
    "build_search_pipeline",
]
