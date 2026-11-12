"""Unit tests for Stage E - Retrieval, Reranker & Confidence Gate (`IMPLEMENTATION_PLAN.md` §2 E).

Covers the plan's required test matrix (the DB-bound PgVectorStore arms are covered by the
integration test in tests/integration/test_retrieval_pgvector.py):
- RRF fusion correctness (formula, absence in one arm => that term contributes 0);
- the Retriever wiring (dense + sparse arms, dense-only fallback when supports_sparse=False);
- the Confidence Gate (max(rerank_score) < tau => "no info", fail-closed on empty/None);
- the FlashRank reranker ordering (+ no mutation of the fused candidates);
- the Small-to-Big hydration access re-check (inaccessible parents dropped) and dedup.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from rag_core.contracts.enums import AccessLevel, ChannelType
from rag_core.contracts.interfaces import (
    BaseEmbedder,
    BaseParentRepository,
    BasePolicyEvaluator,
    BaseVectorStore,
    Retriever,
    VectorStoreCapabilities,
)
from rag_core.contracts.models import (
    AccessDecision,
    ParentChunk,
    RetrievalCandidate,
    UserContext,
)
from rag_core.contracts.settings import Settings
from rag_core.retrieval import (
    ConfidenceGate,
    FlashRankReranker,
    SearchPipeline,
    SmallToBigHydrator,
)

# --------------------------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------------------------


def _candidate(content: str = "c", **kwargs: Any) -> RetrievalCandidate:
    return RetrievalCandidate(
        child_id=kwargs.pop("child_id", uuid4()),
        parent_id=kwargs.pop("parent_id", uuid4()),
        document_id=kwargs.pop("document_id", uuid4()),
        content=content,
        **kwargs,
    )


class _FakeVectorStore(BaseVectorStore):
    """Returns canned dense/sparse candidates; records the filter args it was called with."""

    def __init__(
        self,
        dense: list[RetrievalCandidate],
        sparse: list[RetrievalCandidate],
        supports_sparse: bool = True,
    ) -> None:
        self._dense = dense
        self._sparse = sparse
        self._supports_sparse = supports_sparse
        self.dense_calls: list[dict[str, object]] = []
        self.sparse_calls: list[dict[str, object]] = []

    @property
    def capabilities(self) -> VectorStoreCapabilities:
        return VectorStoreCapabilities(supports_sparse=self._supports_sparse)

    async def upsert_chunks(self, tenant_id: UUID, chunks: list[object]) -> None:  # type: ignore[override]
        return None

    async def search_dense(
        self,
        tenant_id: UUID,
        query_dense: list[float],
        k: int = 30,
        user_access_level: int = 0,
        allowed_document_ids: list[UUID] | None = None,
        denied_document_ids: list[UUID] | None = None,
    ) -> list[RetrievalCandidate]:
        self.dense_calls.append(
            {
                "k": k,
                "level": user_access_level,
                "allowed": allowed_document_ids,
                "denied": denied_document_ids,
            }
        )
        return list(self._dense)

    async def search_sparse(
        self,
        tenant_id: UUID,
        query_sparse: dict[int, float],
        k: int = 30,
        user_access_level: int = 0,
        allowed_document_ids: list[UUID] | None = None,
        denied_document_ids: list[UUID] | None = None,
    ) -> list[RetrievalCandidate]:
        self.sparse_calls.append(
            {
                "k": k,
                "level": user_access_level,
                "allowed": allowed_document_ids,
                "denied": denied_document_ids,
            }
        )
        return list(self._sparse)

    async def delete_by_document(self, tenant_id: UUID, document_id: UUID) -> int:
        return 0


class _FakeEmbedder(BaseEmbedder):
    async def embed_documents(
        self, texts: list[str]
    ) -> tuple[list[list[float]], list[dict[int, float]]]:
        return [[0.1, 0.2] for _ in texts], [{1: 0.5} for _ in texts]

    async def embed_query(self, text: str) -> tuple[list[float], dict[int, float]]:
        return [0.1, 0.2], {1: 0.5}


class _FakeParentRepository(BaseParentRepository):
    def __init__(self, parents: list[ParentChunk]) -> None:
        self._by_id = {p.id: p for p in parents}

    async def fetch_parents(self, tenant_id: UUID, parent_ids: list[UUID]) -> list[ParentChunk]:
        return [self._by_id[pid] for pid in parent_ids if pid in self._by_id]


class _FakePolicy(BasePolicyEvaluator):
    """Grants access only to document ids in ``allowed``."""

    def __init__(self, allowed: set[UUID]) -> None:
        self._allowed = allowed

    async def effective_access(self, user: UserContext) -> AccessDecision:
        return AccessDecision(effective_access_level=user.access_level)

    async def can_access_document(self, user: UserContext, document_id: UUID) -> bool:
        return document_id in self._allowed


def _user(level: AccessLevel = AccessLevel.INTERNAL) -> UserContext:
    return UserContext(
        user_id=uuid4(),
        tenant_id=uuid4(),
        channel_user_id="u1",
        channel=ChannelType.WEB_WIDGET,
        access_level=level,
    )


# --------------------------------------------------------------------------------------------
# RRF fusion (Retriever.fuse_rrf)
# --------------------------------------------------------------------------------------------


def test_fuse_rrf_formula_and_ranking() -> None:
    a, b, c = uuid4(), uuid4(), uuid4()
    dense = [_candidate(child_id=a), _candidate(child_id=b)]
    sparse = [_candidate(child_id=b), _candidate(child_id=c)]

    fused = Retriever.fuse_rrf(dense, sparse, k=60, alpha=0.6, beta=0.4)

    scores = {cand.child_id: cand.rrf_score for cand in fused}
    # b appears in both arms (rank 2 dense, rank 1 sparse) => highest fused score.
    assert scores[b] == pytest.approx(0.6 / 62 + 0.4 / 61)
    # a only in dense (rank 1): sparse term contributes 0.
    assert scores[a] == pytest.approx(0.6 / 61)
    # c only in sparse (rank 2): dense term contributes 0.
    assert scores[c] == pytest.approx(0.4 / 62)
    assert fused[0].child_id == b  # top-ranked
    # Per-arm ranks are recorded on the fused candidate.
    top = fused[0]
    assert top.dense_rank == 2 and top.sparse_rank == 1


def test_fuse_rrf_absent_in_one_list_contributes_zero() -> None:
    only = uuid4()
    fused = Retriever.fuse_rrf([_candidate(child_id=only)], [], k=60, alpha=0.6, beta=0.4)
    assert len(fused) == 1
    assert fused[0].sparse_rank is None
    assert fused[0].rrf_score == pytest.approx(0.6 / 61)


def test_fuse_rrf_does_not_mutate_inputs() -> None:
    dense = [_candidate(child_id=uuid4())]
    before = dense[0].rrf_score
    Retriever.fuse_rrf(dense, [], k=60)
    assert dense[0].rrf_score == before == 0.0


# --------------------------------------------------------------------------------------------
# Retriever wiring (dense + sparse arms, settings-driven params)
# --------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retriever_calls_both_arms_and_passes_filters() -> None:
    a, b = uuid4(), uuid4()
    store = _FakeVectorStore([_candidate(child_id=a)], [_candidate(child_id=b)])
    retriever = Retriever(store, _FakeEmbedder(), Settings())
    tenant = uuid4()
    allowed = [uuid4()]

    denied = [uuid4()]
    fused = await retriever.retrieve(
        "q",
        tenant,
        user_access_level=2,
        top_k=15,
        allowed_document_ids=allowed,
        denied_document_ids=denied,
    )

    assert {c.child_id for c in fused} == {a, b}
    expected = {"k": 15, "level": 2, "allowed": allowed, "denied": denied}
    assert store.dense_calls[0] == expected
    assert store.sparse_calls[0] == expected


@pytest.mark.asyncio
async def test_retriever_skips_sparse_when_unsupported() -> None:
    store = _FakeVectorStore([_candidate()], [_candidate()], supports_sparse=False)
    retriever = Retriever(store, _FakeEmbedder(), Settings())
    await retriever.retrieve("q", uuid4())
    assert store.dense_calls and not store.sparse_calls


@pytest.mark.asyncio
async def test_retriever_defaults_top_k_from_settings() -> None:
    store = _FakeVectorStore([], [])
    retriever = Retriever(store, _FakeEmbedder(), Settings(retrieval_top_candidates=7))
    await retriever.retrieve("q", uuid4())
    assert store.dense_calls[0]["k"] == 7


# --------------------------------------------------------------------------------------------
# FlashRank reranker
# --------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reranker_sorts_by_score_without_mutating_inputs() -> None:
    low, high, mid = _candidate("low"), _candidate("high"), _candidate("mid")
    scores = {"low": 0.1, "high": 0.9, "mid": 0.5}

    def _rank(query: str, passages: list[str]) -> list[float]:
        return [scores[p] for p in passages]

    reranker = FlashRankReranker(rank_fn=_rank)
    result = await reranker.rerank("q", [low, high, mid])

    assert [c.content for c in result] == ["high", "mid", "low"]
    assert [c.rerank_score for c in result] == [0.9, 0.5, 0.1]
    # The fused inputs are not mutated (rerank_score stays None on the originals).
    assert low.rerank_score is None and high.rerank_score is None


@pytest.mark.asyncio
async def test_reranker_empty_returns_empty() -> None:
    reranker = FlashRankReranker(rank_fn=lambda q, p: [])
    assert await reranker.rerank("q", []) == []


@pytest.mark.asyncio
async def test_reranker_rejects_length_mismatch() -> None:
    reranker = FlashRankReranker(rank_fn=lambda q, p: [0.5])
    with pytest.raises(ValueError):
        await reranker.rerank("q", [_candidate(), _candidate()])


# --------------------------------------------------------------------------------------------
# Confidence Gate
# --------------------------------------------------------------------------------------------


def test_confidence_gate_passes_above_threshold() -> None:
    gate = ConfidenceGate(Settings(confidence_threshold=0.35))
    assert gate.evaluate([_candidate(rerank_score=0.4), _candidate(rerank_score=0.1)]) is True


def test_confidence_gate_blocks_below_threshold() -> None:
    gate = ConfidenceGate(Settings(confidence_threshold=0.35))
    assert gate.evaluate([_candidate(rerank_score=0.2), _candidate(rerank_score=0.34)]) is False


def test_confidence_gate_explicit_threshold_overrides_settings() -> None:
    gate = ConfidenceGate(Settings(confidence_threshold=0.9))
    assert gate.evaluate([_candidate(rerank_score=0.5)], threshold=0.4) is True


def test_confidence_gate_fail_closed_on_empty_or_unscored() -> None:
    gate = ConfidenceGate(Settings())
    assert gate.evaluate([]) is False
    assert gate.evaluate([_candidate()]) is False  # no rerank_score


# --------------------------------------------------------------------------------------------
# Small-to-Big hydration + access re-check
# --------------------------------------------------------------------------------------------


def _parent(user: UserContext, content: str) -> ParentChunk:
    return ParentChunk(
        tenant_id=user.tenant_id,
        document_id=uuid4(),
        content=content,
        token_count=10,
        chunk_index=0,
        access_level=AccessLevel.INTERNAL,
    )


@pytest.mark.asyncio
async def test_hydrator_dedups_parents_and_preserves_order() -> None:
    user = _user()
    p1 = _parent(user, "alpha")
    p2 = _parent(user, "beta")
    repo = _FakeParentRepository([p1, p2])
    policy = _FakePolicy({p1.document_id, p2.document_id})
    hydrator = SmallToBigHydrator(repo, policy)

    # Two children map to p1, one to p2; p1 must appear once, before p2.
    candidates = [
        _candidate(parent_id=p1.id),
        _candidate(parent_id=p1.id),
        _candidate(parent_id=p2.id),
    ]
    parents = await hydrator.hydrate(user, candidates)
    assert [p.content for p in parents] == ["alpha", "beta"]


@pytest.mark.asyncio
async def test_hydrator_drops_inaccessible_parents() -> None:
    user = _user()
    allowed = _parent(user, "allowed")
    denied = _parent(user, "denied")
    repo = _FakeParentRepository([allowed, denied])
    policy = _FakePolicy({allowed.document_id})  # denied.document_id not granted
    hydrator = SmallToBigHydrator(repo, policy)

    parents = await hydrator.hydrate(
        user, [_candidate(parent_id=denied.id), _candidate(parent_id=allowed.id)]
    )
    assert [p.content for p in parents] == ["allowed"]


@pytest.mark.asyncio
async def test_hydrator_collapses_duplicate_content() -> None:
    user = _user()
    p1 = _parent(user, "same text")
    p2 = _parent(user, "same text")
    repo = _FakeParentRepository([p1, p2])
    policy = _FakePolicy({p1.document_id, p2.document_id})
    hydrator = SmallToBigHydrator(repo, policy)

    parents = await hydrator.hydrate(
        user, [_candidate(parent_id=p1.id), _candidate(parent_id=p2.id)]
    )
    assert len(parents) == 1


@pytest.mark.asyncio
async def test_hydrator_caps_at_top_parents() -> None:
    user = _user()
    parents_src = [_parent(user, f"p{i}") for i in range(6)]
    repo = _FakeParentRepository(parents_src)
    policy = _FakePolicy({p.document_id for p in parents_src})
    hydrator = SmallToBigHydrator(repo, policy, top_parents=3)

    result = await hydrator.hydrate(user, [_candidate(parent_id=p.id) for p in parents_src])
    assert len(result) == 3
    assert [p.content for p in result] == ["p0", "p1", "p2"]


# --------------------------------------------------------------------------------------------
# Fused-list truncation to Top-N (rerank input cap)
# --------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retriever_truncates_fused_to_top_candidates() -> None:
    # Each arm returns 3 DISTINCT candidates => 6 fused; retrieval_top_candidates=2 caps to 2.
    dense = [_candidate(child_id=uuid4()) for _ in range(3)]
    sparse = [_candidate(child_id=uuid4()) for _ in range(3)]
    store = _FakeVectorStore(dense, sparse)
    retriever = Retriever(store, _FakeEmbedder(), Settings(retrieval_top_candidates=2))

    fused = await retriever.retrieve("q", uuid4())

    assert len(fused) == 2  # capped before rerank/hydration (architecture.md §2 p.8)


# --------------------------------------------------------------------------------------------
# Small-to-Big hydration: backfill past inaccessible parents
# --------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hydrator_backfills_past_inaccessible_parents() -> None:
    user = _user()
    p1 = _parent(user, "denied")
    p2 = _parent(user, "ok-a")
    p3 = _parent(user, "ok-b")
    repo = _FakeParentRepository([p1, p2, p3])
    policy = _FakePolicy({p2.document_id, p3.document_id})  # p1 not granted
    hydrator = SmallToBigHydrator(repo, policy, top_parents=2)

    # p1 is first but inaccessible: the cap counts only accessible parents, so the hydrator
    # keeps scanning and returns the two accessible parents further down (backfill).
    parents = await hydrator.hydrate(
        user,
        [
            _candidate(parent_id=p1.id),
            _candidate(parent_id=p2.id),
            _candidate(parent_id=p3.id),
        ],
    )
    assert [p.content for p in parents] == ["ok-a", "ok-b"]


# --------------------------------------------------------------------------------------------
# SearchPipeline: end-to-end PDP -> retrieve -> rerank -> gate -> hydrate
# --------------------------------------------------------------------------------------------


def _build_pipeline(
    user: UserContext,
    candidates: list[RetrievalCandidate],
    parents: list[ParentChunk],
    scores: dict[str, float],
    *,
    threshold: float = 0.35,
) -> SearchPipeline:
    store = _FakeVectorStore(list(candidates), [])
    retriever = Retriever(store, _FakeEmbedder(), Settings())
    reranker = FlashRankReranker(rank_fn=lambda q, passages: [scores[p] for p in passages])
    gate = ConfidenceGate(Settings(confidence_threshold=threshold))
    policy = _FakePolicy({p.document_id for p in parents})
    hydrator = SmallToBigHydrator(_FakeParentRepository(parents), policy)
    return SearchPipeline(policy, retriever, reranker, gate, hydrator)


@pytest.mark.asyncio
async def test_search_pipeline_confident_hydrates_parents() -> None:
    user = _user()
    parent = _parent(user, "answer context")
    cand = _candidate("hit", parent_id=parent.id)
    pipeline = _build_pipeline(user, [cand], [parent], {"hit": 0.9})

    result = await pipeline.search(user, "q")

    assert result.is_confident is True
    assert [p.content for p in result.parents] == ["answer context"]
    assert [c.content for c in result.candidates] == ["hit"]


@pytest.mark.asyncio
async def test_search_pipeline_low_confidence_short_circuits() -> None:
    user = _user()
    parent = _parent(user, "answer context")
    cand = _candidate("weak", parent_id=parent.id)
    pipeline = _build_pipeline(user, [cand], [parent], {"weak": 0.1})

    result = await pipeline.search(user, "q")

    assert result.is_confident is False
    assert result.parents == []  # no hydration / LLM for a below-threshold query
    assert [c.content for c in result.candidates] == ["weak"]  # candidates still available


# --------------------------------------------------------------------------------------------
# Retrieval-SLO golden dataset (Hit Rate@5 / MRR@5 / nDCG@5) - deferred to Stage J
# --------------------------------------------------------------------------------------------


@pytest.mark.skip(
    reason="Retrieval-SLO metrics (Hit Rate@5>=0.85, MRR@5>=0.75, nDCG@5>=0.80) depend on the "
    "golden dataset built in Stage J (Evaluation Gate); placeholder guard until then."
)
def test_retrieval_slo_on_golden_dataset() -> None:  # pragma: no cover - placeholder
    raise AssertionError("implemented in Stage J")
