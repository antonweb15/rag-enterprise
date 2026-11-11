"""Ingestion pipeline orchestrator (`core/architecture.md` §3, `IMPLEMENTATION_PLAN.md` Stage D).

The pipeline ties the Stage D components together for the single durable task
``process_document_task(tenant_id, storage_ref, content_hash, access_level)``:

1. **Idempotency** - if a document with the same ``content_hash`` already exists for the tenant, the
   file is skipped (``core/architecture.md`` §3: "Checks `documents` table, skip if already
   indexed"). This makes re-delivery of the same file a no-op.
2. **Fetch** the raw bytes from the ``BaseObjectStorage`` port (per-tenant).
3. **Parse** (Docling / plain text) -> **split** (token-aware Parent-Child) -> **embed** children in
   strict batches of 32 (BGE-M3 dense + sparse), with retry + exponential backoff around the
   embedder (resilience matrix, ``core/architecture.md`` §4).
4. **Near-duplicate suppression** of child chunks.
5. **Transactional upsert** (document -> parents -> children, status -> ``indexed``) through the
   ``BaseIngestRepository`` port. The embedding step runs *before* the write, so an embedder failure
   never leaves a partially-written document (rollback is natural; the persisted ``status`` stays
   ``failed``/absent).

``NOTIFY ingest_events`` is emitted by the DB trigger ``trg_documents_status_notify`` (migration
0001) on the ``documents.status`` transition - that is the single source of truth for the event, so
the pipeline does NOT emit a second application-level ``NOTIFY`` (this avoids the double-fire the
Stage-D review called out).

The DB access is abstracted behind :class:`BaseIngestRepository`; the orchestrator stays pure and
unit-testable with an in-memory fake, while the concrete SQLAlchemy implementation lives in
:mod:`rag_core.ingestion.repository` (:class:`SqlAlchemyIngestRepository`) and performs the whole
document write in one transaction.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar
from uuid import UUID

from rag_core.contracts.enums import AccessLevel, DocumentStatus
from rag_core.contracts.interfaces import BaseEmbedder, BaseObjectStorage
from rag_core.contracts.models import (
    DEFAULT_EMBEDDING_MODEL,
    ChildChunk,
    ParentChunk,
    RawDocument,
)
from rag_core.ingestion.dedup import suppress_near_duplicates
from rag_core.ingestion.parser import DocumentParser
from rag_core.ingestion.splitter import ParentChildSplitter

_T = TypeVar("_T")


class BaseIngestRepository(ABC):
    """The persistence port for the ingestion pipeline (transactional, tenant-scoped).

    A concrete implementation (SQLAlchemy + pgvector) performs the INSERTs atomically in one
    transaction; a failure anywhere rolls the whole document back (no orphan parents/children).
    """

    @abstractmethod
    async def document_exists(self, tenant_id: UUID, content_hash: str) -> bool:
        """Idempotency check: True if a document with this content hash is already indexed."""

    @abstractmethod
    async def save_document(
        self,
        document: RawDocument,
        parents: list[ParentChunk],
        children: list[ChildChunk],
    ) -> None:
        """Atomically persists the document, its parents and children; sets status=indexed.

        ``NOTIFY ingest_events`` is NOT the pipeline's job: the DB trigger
        ``trg_documents_status_notify`` fires it on the ``status`` transition inside this same
        transaction (single source of truth), so the concrete implementation only writes rows.
        """


@dataclass
class IngestResult:
    """The outcome of processing one file."""

    document_id: UUID
    status: DocumentStatus
    parent_count: int
    child_count: int
    skipped: bool = False


# Programming/validation errors are NOT transient: retrying them wastes backoff cycles and can
# mask real bugs. The retry helper re-raises these immediately; only transient/IO failures are
# retried (resilience matrix, core/architecture.md §4).
NON_RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    ValueError,
    TypeError,
    KeyError,
    IndexError,
    AttributeError,
    AssertionError,
    NotImplementedError,
)


def _is_transient(exc: BaseException) -> bool:
    return not isinstance(exc, NON_RETRYABLE_EXCEPTIONS)


async def retry_async(
    func: Callable[[], Awaitable[_T]],
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 5.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    retryable: Callable[[BaseException], bool] = _is_transient,
) -> _T:
    """Runs ``func`` with retries and exponential backoff (resilience matrix, §4: 3 attempts, 1-5s).

    Only failures for which ``retryable(exc)`` is True are retried; a non-transient error (e.g. a
    ``ValueError`` from bad data) is re-raised immediately. Re-raises the last exception after
    ``attempts`` failures. ``sleep`` is injectable so tests do not actually wait.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    last_exc: BaseException | None = None
    for attempt in range(attempts):
        try:
            return await func()
        except Exception as exc:  # noqa: BLE001 - classified by `retryable` below
            last_exc = exc
            if not retryable(exc) or attempt == attempts - 1:
                break
            delay = min(base_delay * (2**attempt), max_delay)
            await sleep(delay)
    assert last_exc is not None  # noqa: S101 - loop runs at least once
    raise last_exc


class IngestionPipeline:
    """Orchestrates parse -> split -> embed -> (dedup) -> transactional upsert for one document."""

    def __init__(
        self,
        storage: BaseObjectStorage,
        embedder: BaseEmbedder,
        repository: BaseIngestRepository,
        parser: DocumentParser | None = None,
        splitter: ParentChildSplitter | None = None,
        *,
        embedding_model_id: str = DEFAULT_EMBEDDING_MODEL,
        embedding_version: int = 1,
        embed_attempts: int = 3,
        embed_retry_base_delay: float = 1.0,
        embed_retry_max_delay: float = 5.0,
        dedup_children: bool = True,
        default_source_type: str = "s3",
        default_language: str = "ru",
    ) -> None:
        self._storage = storage
        self._embedder = embedder
        self._repository = repository
        self._parser = parser or DocumentParser()
        self._splitter = splitter or ParentChildSplitter()
        self._embedding_model_id = embedding_model_id
        self._embedding_version = embedding_version
        self._embed_attempts = embed_attempts
        self._embed_retry_base_delay = embed_retry_base_delay
        self._embed_retry_max_delay = embed_retry_max_delay
        self._dedup_children = dedup_children
        # The real provenance of a file (set by the worker from the active object-storage
        # provider), so a local-FS ingest is not mislabelled source_type="s3".
        self._default_source_type = default_source_type
        self._default_language = default_language

    async def process_document(
        self,
        tenant_id: UUID,
        storage_ref: str,
        content_hash: str,
        access_level: AccessLevel = AccessLevel.PUBLIC,
        *,
        source_type: str | None = None,
        language: str | None = None,
    ) -> IngestResult:
        """Processes one file end-to-end; returns an :class:`IngestResult` (skipped on dup).

        ``source_type``/``language`` default to the pipeline's configured values (set by the worker
        from the active object-storage provider) rather than a hardcoded ``"s3"``/``"ru"``.
        """
        document = RawDocument(
            tenant_id=tenant_id,
            source_type=source_type or self._default_source_type,
            source_uri=storage_ref,
            content_hash=content_hash,
            language=language or self._default_language,
            access_level=access_level,
        )

        # 1. Idempotency: a file with the same content hash is a no-op.
        if await self._repository.document_exists(tenant_id, content_hash):
            return IngestResult(
                document_id=document.id,
                status=DocumentStatus.INDEXED,
                parent_count=0,
                child_count=0,
                skipped=True,
            )

        # 2. Fetch raw bytes from the per-tenant object storage.
        data = await self._storage.get(tenant_id, storage_ref)

        # 3. Parse -> split.
        parsed = self._parser.parse(storage_ref, data)
        parent_drafts = self._splitter.split(parsed.text)

        parents: list[ParentChunk] = []
        children: list[ChildChunk] = []
        child_texts: list[str] = []

        for p_draft in parent_drafts:
            parent = ParentChunk(
                tenant_id=tenant_id,
                document_id=document.id,
                content=p_draft.content,
                token_count=p_draft.token_count,
                chunk_index=p_draft.chunk_index,
                access_level=access_level,
                metadata=dict(parsed.metadata),
            )
            parents.append(parent)
            for c_draft in p_draft.children:
                child = ChildChunk(
                    tenant_id=tenant_id,
                    parent_id=parent.id,
                    content=c_draft.content,
                    token_count=c_draft.token_count,
                    chunk_index=c_draft.chunk_index,
                    access_level=access_level,
                    embedding_model_id=self._embedding_model_id,
                    embedding_version=self._embedding_version,
                )
                children.append(child)
                child_texts.append(c_draft.content)

        # 4. Optional near-duplicate suppression (keeps embedding cost + index clean).
        if self._dedup_children and children:
            children = suppress_near_duplicates(children, key=lambda c: c.content)
            child_texts = [c.content for c in children]

        # 5. Embed (strict batching inside the embedder) with retry + backoff. Done BEFORE the
        #    write, so an embedder failure never persists a partial document.
        if child_texts:
            dense, sparse = await retry_async(
                lambda: self._embedder.embed_documents(child_texts),
                attempts=self._embed_attempts,
                base_delay=self._embed_retry_base_delay,
                max_delay=self._embed_retry_max_delay,
            )
            if len(dense) != len(children) or len(sparse) != len(children):
                raise ValueError("embedder returned a mismatched number of vectors")
            for child, dense_vec, sparse_vec in zip(children, dense, sparse, strict=True):
                child.dense_embedding = dense_vec
                child.sparse_embedding = sparse_vec

        # 6. Transactional upsert (document -> parents -> children, status=indexed). The DB trigger
        #    trg_documents_status_notify emits NOTIFY ingest_events inside this transaction.
        await self._repository.save_document(document, parents, children)

        return IngestResult(
            document_id=document.id,
            status=DocumentStatus.INDEXED,
            parent_count=len(parents),
            child_count=len(children),
        )
