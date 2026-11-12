"""PgVector-backed vector store (`IMPLEMENTATION_PLAN.md` Stage E, `core/architecture.md` §2).

:class:`PgVectorStore` is the concrete :class:`BaseVectorStore` provider for PostgreSQL + pgvector.
The contract is deliberately the **separate search arms** - ``search_dense`` / ``search_sparse``:
RRF fusion happens in the application layer (``Retriever.fuse_rrf``), not inside the store, so the
``hybrid_search_rrf`` SQL function remains an internal optimization detail (hence
``capabilities.supports_hybrid_rrf = False`` here - the application-layer fusion is the contract).

Both arms enforce, **before** ``LIMIT`` (never as a post-filter, which would break top-k/RRF):

- tenant isolation (``tenant_id`` predicate),
- the access predicate that matches the PDP (:class:`AccessDecision`): a chunk is visible when
  ``access_level <= user_access_level`` **OR** its document is in ``allowed_document_ids`` (an
  explicit ACL grant can raise visibility above the clearance level - the allowlist is OR-combined
  with the level, never AND), minus anything in ``denied_document_ids`` (denials win),
- the active ``embedding_version`` (so a re-embedding version bump never mixes dimensions/versions).

HNSW recall is tuned per statement via ``SET LOCAL hnsw.ef_search`` (canonically 40). The sparse arm
applies a near-zero-norm cutoff: ``sparsevec`` cosine distance is numerically unstable when the
query norm is ~0, so below the cutoff the arm returns an empty list and the retriever falls back to
dense-only RRF.

The ORM ``Session`` is synchronous, so the async port methods run the blocking DB work in a worker
thread (``asyncio.to_thread``); the retriever/pipeline stay fully async.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from uuid import UUID

from pgvector import SparseVector
from sqlalchemy import Row, and_, or_, select, text
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from rag_core.contracts.interfaces import BaseVectorStore, VectorStoreCapabilities
from rag_core.contracts.models import ChildChunk as DomainChildChunk
from rag_core.contracts.models import RetrievalCandidate
from rag_core.contracts.settings import Settings
from rag_core.db import models

SessionFactory = Callable[[], Session]

# Below this L2 norm the sparse cosine distance is numerically unstable (division by ~0); the
# sparse arm is skipped and the retriever falls back to dense-only RRF (storage_schema.md §2.1,
# mirrors the `l2_norm(...) > 1e-6` cutoff baked into the hybrid_search_rrf SQL function).
SPARSE_NORM_CUTOFF = 1e-6

# Canonical HNSW recall knob for retrieval (core/architecture.md §2 p.7, hybrid_search_rrf SQL).
DEFAULT_EF_SEARCH = 40


def _sparse_l2_norm(weights: dict[int, float]) -> float:
    return math.sqrt(sum(v * v for v in weights.values()))


class PgVectorStore(BaseVectorStore):
    """The pgvector :class:`BaseVectorStore` provider (dense HNSW + sparse ``sparsevec`` arms)."""

    def __init__(
        self,
        session_factory: SessionFactory,
        settings: Settings | None = None,
        ef_search: int = DEFAULT_EF_SEARCH,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings or Settings()
        self._ef_search = ef_search

    @property
    def capabilities(self) -> VectorStoreCapabilities:
        # supports_hybrid_rrf=False: RRF is the application-layer contract (Retriever.fuse_rrf);
        # the hybrid_search_rrf SQL function stays an internal optimization detail.
        return VectorStoreCapabilities(
            supports_sparse=True,
            supports_hybrid_rrf=False,
            max_dimensions=models.DENSE_DIM,
        )

    async def upsert_chunks(self, tenant_id: UUID, chunks: list[DomainChildChunk]) -> None:
        await asyncio.to_thread(self._upsert_chunks_sync, tenant_id, chunks)

    def _upsert_chunks_sync(self, tenant_id: UUID, chunks: list[DomainChildChunk]) -> None:
        with self._session_factory() as session, session.begin():
            for chunk in chunks:
                row = session.get(models.ChildChunk, chunk.id)
                sparse = (
                    SparseVector(chunk.sparse_embedding, models.SPARSE_DIM)
                    if chunk.sparse_embedding is not None
                    else None
                )
                if row is None:
                    session.add(
                        models.ChildChunk(
                            id=chunk.id,
                            tenant_id=chunk.tenant_id,
                            parent_id=chunk.parent_id,
                            content=chunk.content,
                            token_count=chunk.token_count,
                            chunk_index=chunk.chunk_index,
                            access_level=int(chunk.access_level),
                            embedding=chunk.dense_embedding,
                            sparse_vector=sparse,
                            embedding_model_id=chunk.embedding_model_id,
                            embedding_version=chunk.embedding_version,
                        )
                    )
                    continue
                if row.tenant_id != tenant_id:
                    # fail-closed, no silent drift: an existing chunk id owned by another tenant
                    # must never be overwritten or silently dropped - it is a programming/wiring
                    # bug (UUIDs collide across tenants or the tenant_id was mis-resolved).
                    raise ValueError(
                        f"child chunk {chunk.id} belongs to tenant {row.tenant_id}, not {tenant_id}"
                    )
                row.content = chunk.content
                row.token_count = chunk.token_count
                row.chunk_index = chunk.chunk_index
                row.access_level = int(chunk.access_level)
                row.embedding = chunk.dense_embedding
                row.sparse_vector = sparse
                row.embedding_model_id = chunk.embedding_model_id
                row.embedding_version = chunk.embedding_version

    async def search_dense(
        self,
        tenant_id: UUID,
        query_dense: list[float],
        k: int = 30,
        user_access_level: int = 0,
        allowed_document_ids: list[UUID] | None = None,
        denied_document_ids: list[UUID] | None = None,
    ) -> list[RetrievalCandidate]:
        return await asyncio.to_thread(
            self._search_dense_sync,
            tenant_id,
            query_dense,
            k,
            user_access_level,
            allowed_document_ids,
            denied_document_ids,
        )

    def _search_dense_sync(
        self,
        tenant_id: UUID,
        query_dense: list[float],
        k: int,
        user_access_level: int,
        allowed_document_ids: list[UUID] | None,
        denied_document_ids: list[UUID] | None,
    ) -> list[RetrievalCandidate]:
        child = models.ChildChunk
        parent = models.ParentChunk
        distance = child.embedding.cosine_distance(query_dense).label("distance")
        stmt = (
            select(
                child.id.label("child_id"),
                parent.id.label("parent_id"),
                parent.document_id.label("document_id"),
                child.content.label("content"),
                distance,
            )
            .join(parent, child.parent_id == parent.id)
            .where(
                child.tenant_id == tenant_id,
                self._access_predicate(
                    user_access_level, allowed_document_ids, denied_document_ids
                ),
                child.embedding.isnot(None),
                child.embedding_version == self._settings.embedding_version,
            )
            .order_by(distance)
            .limit(k)
        )
        with self._session_factory() as session, session.begin():
            self._apply_ef_search(session)
            rows = session.execute(stmt).all()
        return [
            self._candidate(row, dense_rank=rank, dense_distance=row.distance)
            for rank, row in enumerate(rows, start=1)
        ]

    async def search_sparse(
        self,
        tenant_id: UUID,
        query_sparse: dict[int, float],
        k: int = 30,
        user_access_level: int = 0,
        allowed_document_ids: list[UUID] | None = None,
        denied_document_ids: list[UUID] | None = None,
    ) -> list[RetrievalCandidate]:
        # Near-zero sparse norm => cosine distance is unstable; skip the arm (dense-only fallback).
        if not query_sparse or _sparse_l2_norm(query_sparse) <= SPARSE_NORM_CUTOFF:
            return []
        return await asyncio.to_thread(
            self._search_sparse_sync,
            tenant_id,
            query_sparse,
            k,
            user_access_level,
            allowed_document_ids,
            denied_document_ids,
        )

    def _search_sparse_sync(
        self,
        tenant_id: UUID,
        query_sparse: dict[int, float],
        k: int,
        user_access_level: int,
        allowed_document_ids: list[UUID] | None,
        denied_document_ids: list[UUID] | None,
    ) -> list[RetrievalCandidate]:
        child = models.ChildChunk
        parent = models.ParentChunk
        query_vec = SparseVector(query_sparse, models.SPARSE_DIM)
        distance = child.sparse_vector.cosine_distance(query_vec).label("distance")
        stmt = (
            select(
                child.id.label("child_id"),
                parent.id.label("parent_id"),
                parent.document_id.label("document_id"),
                child.content.label("content"),
                distance,
            )
            .join(parent, child.parent_id == parent.id)
            .where(
                child.tenant_id == tenant_id,
                self._access_predicate(
                    user_access_level, allowed_document_ids, denied_document_ids
                ),
                child.sparse_vector.isnot(None),
                child.embedding_version == self._settings.embedding_version,
            )
            .order_by(distance)
            .limit(k)
        )
        # NB: hnsw.ef_search is NOT set here - it tunes the dense HNSW index only; sparsevec cosine
        # distance does not use it, so applying it to the sparse arm would be misleading.
        with self._session_factory() as session, session.begin():
            rows = session.execute(stmt).all()
        return [
            self._candidate(row, sparse_rank=rank, sparse_distance=row.distance)
            for rank, row in enumerate(rows, start=1)
        ]

    async def delete_by_document(self, tenant_id: UUID, document_id: UUID) -> int:
        return await asyncio.to_thread(self._delete_by_document_sync, tenant_id, document_id)

    def _delete_by_document_sync(self, tenant_id: UUID, document_id: UUID) -> int:
        child = models.ChildChunk
        parent = models.ParentChunk
        with self._session_factory() as session, session.begin():
            # ORM expressions throughout (no raw string-cast UUID SQL), consistent with the arms.
            parent_ids = list(
                session.execute(
                    select(parent.id).where(
                        parent.tenant_id == tenant_id, parent.document_id == document_id
                    )
                ).scalars()
            )
            if not parent_ids:
                return 0
            deleted = (
                session.query(child)
                .filter(child.tenant_id == tenant_id, child.parent_id.in_(parent_ids))
                .delete(synchronize_session=False)
            )
            session.query(parent).filter(
                parent.tenant_id == tenant_id, parent.document_id == document_id
            ).delete(synchronize_session=False)
        return int(deleted)

    def _apply_ef_search(self, session: Session) -> None:
        # SET LOCAL is transaction-scoped (requires an open transaction, hence session.begin()).
        session.execute(text(f"SET LOCAL hnsw.ef_search = {int(self._ef_search)}"))

    @staticmethod
    def _access_predicate(
        user_access_level: int,
        allowed_document_ids: list[UUID] | None,
        denied_document_ids: list[UUID] | None,
    ) -> ColumnElement[bool]:
        """The access predicate (applied BEFORE LIMIT, so top-k/RRF stay correct).

        Mirrors the PDP (:class:`AccessDecision`) exactly:
        - ``allowed_document_ids is None`` => ACL disabled, level-only filter
          (``access_level <= user_access_level``);
        - a list => the level filter is **OR-combined** with the ACL allowlist, so an explicit
          grant can surface a document above the user's clearance (an empty list adds nothing -
          ``IN ()`` is always false - so it degrades to level-only, matching "no ACL grants");
        - ``denied_document_ids`` are excluded regardless (denials win, fail-closed on conflict).
        """
        child = models.ChildChunk
        parent = models.ParentChunk
        level_clause: ColumnElement[bool] = child.access_level <= user_access_level
        if allowed_document_ids is None:
            clause: ColumnElement[bool] = level_clause
        else:
            clause = or_(level_clause, parent.document_id.in_(allowed_document_ids))
        if denied_document_ids:
            clause = and_(clause, parent.document_id.notin_(denied_document_ids))
        return clause

    @staticmethod
    def _candidate(
        row: Row[tuple[UUID, UUID, UUID, str, float]],
        *,
        dense_rank: int | None = None,
        sparse_rank: int | None = None,
        dense_distance: float | None = None,
        sparse_distance: float | None = None,
    ) -> RetrievalCandidate:
        return RetrievalCandidate(
            child_id=row.child_id,
            parent_id=row.parent_id,
            document_id=row.document_id,
            content=row.content,
            # Expose cosine similarity (1 - distance) as the per-arm raw signal.
            dense_score=None if dense_distance is None else 1.0 - float(dense_distance),
            sparse_score=None if sparse_distance is None else 1.0 - float(sparse_distance),
            dense_rank=dense_rank,
            sparse_rank=sparse_rank,
        )
