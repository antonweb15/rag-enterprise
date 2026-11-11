"""Re-embedding on an embedding-model/version bump (`IMPLEMENTATION_PLAN.md` Stage D).

When the embedding model changes (a new ``embedding_model_id`` or a bumped ``embedding_version``),
the corpus must be re-embedded WITHOUT search downtime. The mechanism is **version-tagging + a
single active-version pointer**:

- only the *affected* chunks (whose ``(embedding_model_id, embedding_version)`` differs from the
  target) are re-embedded - already-current chunks are left untouched;
- **coexistence** is concrete, not aspirational: every ``child_chunks`` row carries its own
  ``embedding_model_id``/``embedding_version``, so a half-migrated corpus holds old-version and
  new-version rows side by side. Each re-embed batch is written transactionally
  (:class:`~rag_core.ingestion.repository.SqlAlchemyReembedRepository`), and the retrieval layer
  filters by the *active* version, so it keeps serving the old vectors until the cutover;
- the **atomic switch** is flipping that single active-version value (one config/row update) once
  the backfill reaches 100%. Because it is one value, the cutover is instantaneous and reversible -
  no mixed-dimension window in the index (the inactive version's rows are simply not queried);
- re-embedding is done in batches (the same strict batch-by-N as initial ingest).

``ReembeddingPlanner`` is the pure decision function (which chunks need work);
``ReembeddingService`` drives the batched re-embedding through the ``BaseEmbedder`` +
``BaseReembedRepository`` ports; the concrete repository performs the transactional batch write.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from uuid import UUID

from rag_core.contracts.interfaces import BaseEmbedder
from rag_core.contracts.models import ChildChunk
from rag_core.ingestion.embedder import batched


class ReembeddingPlanner:
    """Decides which child chunks need re-embedding for a target ``(model_id, version)``."""

    @staticmethod
    def needs_reembedding(chunk: ChildChunk, target_model_id: str, target_version: int) -> bool:
        """True iff the chunk's embedding model id or version differs from the target."""
        return (
            chunk.embedding_model_id != target_model_id or chunk.embedding_version != target_version
        )

    @classmethod
    def select(
        cls,
        chunks: Sequence[ChildChunk],
        target_model_id: str,
        target_version: int,
    ) -> list[ChildChunk]:
        """Returns only the chunks that must be re-embedded (affected subset, order preserved)."""
        return [c for c in chunks if cls.needs_reembedding(c, target_model_id, target_version)]


class BaseReembedRepository(ABC):
    """The persistence port used by the re-embedding service."""

    @abstractmethod
    async def fetch_chunks_batch(
        self,
        tenant_id: UUID,
        target_model_id: str,
        target_version: int,
        limit: int,
    ) -> list[ChildChunk]:
        """Returns the next batch of chunks NOT yet at the target ``(model_id, version)``."""

    @abstractmethod
    async def write_reembedded_batch(self, tenant_id: UUID, chunks: list[ChildChunk]) -> None:
        """Persists the new vectors for the batch (old vectors stay until the atomic switch)."""


class ReembeddingService:
    """Drives a batched, downtime-free re-embedding of a tenant's corpus to a new version."""

    def __init__(
        self,
        embedder: BaseEmbedder,
        repository: BaseReembedRepository,
        *,
        batch_size: int = 32,
    ) -> None:
        self._embedder = embedder
        self._repository = repository
        self._batch_size = batch_size

    async def reembed_chunks(
        self,
        tenant_id: UUID,
        chunks: Sequence[ChildChunk],
        target_model_id: str,
        target_version: int,
    ) -> list[ChildChunk]:
        """Re-embeds the given chunks in batches, stamping the target model id / version.

        Returns the updated chunks (new dense/sparse vectors + target version). Old versions are not
        mutated in place - only the affected chunks are touched - so differing dimensions never mix.
        """
        updated: list[ChildChunk] = []
        for batch in batched(list(chunks), self._batch_size):
            texts = [c.content for c in batch]
            dense, sparse = await self._embedder.embed_documents(texts)
            for chunk, dense_vec, sparse_vec in zip(batch, dense, sparse, strict=True):
                new_chunk = chunk.model_copy(
                    update={
                        "dense_embedding": dense_vec,
                        "sparse_embedding": sparse_vec,
                        "embedding_model_id": target_model_id,
                        "embedding_version": target_version,
                    }
                )
                updated.append(new_chunk)
        return updated

    async def reembed_corpus(
        self, tenant_id: UUID, target_model_id: str, target_version: int
    ) -> int:
        """Streams batches from the repo, re-embeds, writes them back; returns the count."""
        total = 0
        while True:
            batch = await self._repository.fetch_chunks_batch(
                tenant_id, target_model_id, target_version, self._batch_size
            )
            if not batch:
                break
            updated = await self.reembed_chunks(tenant_id, batch, target_model_id, target_version)
            await self._repository.write_reembedded_batch(tenant_id, updated)
            total += len(updated)
        return total
