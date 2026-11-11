"""Concrete SQLAlchemy persistence for ingestion (`IMPLEMENTATION_PLAN.md` Stage D).

This is the runnable half of the pipeline's persistence ports:

- :class:`SqlAlchemyIngestRepository` implements :class:`BaseIngestRepository` - the whole document
  (document -> parents -> children, ``status='indexed'``) is written in **one** transaction, so a
  failure rolls everything back (no orphan parents/children). ``NOTIFY ingest_events`` is emitted by
  the DB trigger ``trg_documents_status_notify`` inside that same transaction - the app does not
  emit a second event.
- :class:`SqlAlchemyReembedRepository` implements :class:`BaseReembedRepository` - it streams the
  chunks that are not yet at the target ``(embedding_model_id, embedding_version)`` and writes the
  re-embedded batch back transactionally.

Field-name translation lives here (the single hand-off point between the Pydantic contract and the
ORM): the domain ``ChildChunk.dense_embedding``/``sparse_embedding`` map to the ORM
``embedding``/``sparse_vector`` columns (dense list -> ``Vector``; ``dict[int, float]`` ->
``SparseVector``).

The ORM ``Session`` is synchronous (``core/db/session.py``), so the async port methods run the
blocking DB work in a worker thread (``asyncio.to_thread``); the pipeline/worker stay fully async.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from uuid import UUID

from pgvector import SparseVector
from sqlalchemy import select
from sqlalchemy.orm import Session

from rag_core.contracts.models import ChildChunk as DomainChildChunk
from rag_core.contracts.models import ParentChunk as DomainParentChunk
from rag_core.contracts.models import RawDocument
from rag_core.db import models
from rag_core.ingestion.pipeline import BaseIngestRepository
from rag_core.ingestion.reembedding import BaseReembedRepository

# A factory producing a fresh Session (e.g. a configured sessionmaker bound to POSTGRES_DSN).
SessionFactory = Callable[[], Session]


def _to_sparse_vector(weights: dict[int, float] | None) -> SparseVector | None:
    if weights is None:
        return None
    return SparseVector(weights, models.SPARSE_DIM)


def _domain_child_from_orm(row: models.ChildChunk) -> DomainChildChunk:
    sparse: Any = row.sparse_vector
    sparse_dict: dict[int, float] | None = None
    if sparse is not None and hasattr(sparse, "indices"):
        # pgvector SparseVector -> {index: value}
        indices = sparse.indices()
        values = sparse.values()
        sparse_dict = {int(i): float(v) for i, v in zip(indices, values, strict=True)}
    return DomainChildChunk(
        id=row.id,
        tenant_id=row.tenant_id,
        parent_id=row.parent_id,
        content=row.content,
        token_count=row.token_count,
        chunk_index=row.chunk_index,
        access_level=row.access_level,
        dense_embedding=list(row.embedding) if row.embedding is not None else None,
        sparse_embedding=sparse_dict,
        embedding_model_id=row.embedding_model_id,
        embedding_version=row.embedding_version,
    )


class SqlAlchemyIngestRepository(BaseIngestRepository):
    """Writes the whole document in one transaction via a synchronous ORM ``Session``."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def document_exists(self, tenant_id: UUID, content_hash: str) -> bool:
        return await asyncio.to_thread(self._document_exists_sync, tenant_id, content_hash)

    def _document_exists_sync(self, tenant_id: UUID, content_hash: str) -> bool:
        with self._session_factory() as session:
            stmt = select(models.Document.id).where(
                models.Document.tenant_id == tenant_id,
                models.Document.content_hash == content_hash,
            )
            return session.execute(stmt).first() is not None

    async def save_document(
        self,
        document: RawDocument,
        parents: list[DomainParentChunk],
        children: list[DomainChildChunk],
    ) -> None:
        await asyncio.to_thread(self._save_document_sync, document, parents, children)

    def _save_document_sync(
        self,
        document: RawDocument,
        parents: list[DomainParentChunk],
        children: list[DomainChildChunk],
    ) -> None:
        with self._session_factory() as session, session.begin():
            session.add(
                models.Document(
                    id=document.id,
                    tenant_id=document.tenant_id,
                    source_type=document.source_type,
                    source_uri=document.source_uri,
                    content_hash=document.content_hash,
                    status="indexed",
                    language=document.language,
                    access_level=int(document.access_level),
                    doc_metadata=dict(document.metadata),
                )
            )
            for parent in parents:
                session.add(
                    models.ParentChunk(
                        id=parent.id,
                        tenant_id=parent.tenant_id,
                        document_id=parent.document_id,
                        content=parent.content,
                        token_count=parent.token_count,
                        chunk_index=parent.chunk_index,
                        access_level=int(parent.access_level),
                        chunk_metadata=dict(parent.metadata),
                    )
                )
            for child in children:
                session.add(
                    models.ChildChunk(
                        id=child.id,
                        tenant_id=child.tenant_id,
                        parent_id=child.parent_id,
                        content=child.content,
                        token_count=child.token_count,
                        chunk_index=child.chunk_index,
                        access_level=int(child.access_level),
                        embedding=child.dense_embedding,
                        sparse_vector=_to_sparse_vector(child.sparse_embedding),
                        embedding_model_id=child.embedding_model_id,
                        embedding_version=child.embedding_version,
                    )
                )
            # session.begin() commits on exit; the status NOTIFY fires from the DB trigger.


class SqlAlchemyReembedRepository(BaseReembedRepository):
    """Streams/writes chunks for a downtime-free re-embedding to a new ``(model_id, version)``."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def fetch_chunks_batch(
        self,
        tenant_id: UUID,
        target_model_id: str,
        target_version: int,
        limit: int,
    ) -> list[DomainChildChunk]:
        return await asyncio.to_thread(
            self._fetch_chunks_batch_sync, tenant_id, target_model_id, target_version, limit
        )

    def _fetch_chunks_batch_sync(
        self,
        tenant_id: UUID,
        target_model_id: str,
        target_version: int,
        limit: int,
    ) -> list[DomainChildChunk]:
        with self._session_factory() as session:
            stmt = (
                select(models.ChildChunk)
                .where(
                    models.ChildChunk.tenant_id == tenant_id,
                    (models.ChildChunk.embedding_model_id != target_model_id)
                    | (models.ChildChunk.embedding_version != target_version),
                )
                .order_by(models.ChildChunk.id)
                .limit(limit)
            )
            rows = session.execute(stmt).scalars().all()
            return [_domain_child_from_orm(row) for row in rows]

    async def write_reembedded_batch(self, tenant_id: UUID, chunks: list[DomainChildChunk]) -> None:
        await asyncio.to_thread(self._write_reembedded_batch_sync, tenant_id, chunks)

    def _write_reembedded_batch_sync(self, tenant_id: UUID, chunks: list[DomainChildChunk]) -> None:
        with self._session_factory() as session, session.begin():
            for chunk in chunks:
                row = session.get(models.ChildChunk, chunk.id)
                if row is None or row.tenant_id != tenant_id:
                    continue
                row.embedding = chunk.dense_embedding
                row.sparse_vector = _to_sparse_vector(chunk.sparse_embedding)
                row.embedding_model_id = chunk.embedding_model_id
                row.embedding_version = chunk.embedding_version
