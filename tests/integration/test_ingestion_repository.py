"""Integration tests for the Stage-D concrete persistence (`IMPLEMENTATION_PLAN.md` Stage D).

They run against a real Postgres+pgvector (testcontainers, via the shared `admin_engine` fixture)
and exercise the parts that the in-memory fakes cannot prove:

- a real **transactional upsert** (document -> parents -> children) commits atomically, and
- the DB trigger actually emits ``NOTIFY ingest_events`` on the status transition, and
- a mid-transaction failure (FK violation) **rolls the whole document back** (no orphan rows), and
- the **re-embedding streaming loop** updates only the affected chunks in the DB.

A working Docker is required; without it the shared fixtures skip (CI has Docker).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from uuid import uuid4

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from rag_core.contracts.enums import AccessLevel
from rag_core.contracts.models import ChildChunk, ParentChunk, RawDocument
from rag_core.db import models
from rag_core.ingestion.reembedding import ReembeddingService
from rag_core.ingestion.repository import (
    SqlAlchemyIngestRepository,
    SqlAlchemyReembedRepository,
)

pytestmark = pytest.mark.asyncio

_DENSE = [0.1] * models.DENSE_DIM


class _StubEmbedder:
    """Deterministic embedder for the re-embed loop (dense + sparse per text)."""

    async def embed_documents(
        self, texts: list[str]
    ) -> tuple[list[list[float]], list[dict[int, float]]]:
        return [[0.2] * models.DENSE_DIM for _ in texts], [{1: 0.9} for _ in texts]

    async def embed_query(self, text: str) -> tuple[list[float], dict[int, float]]:
        return [0.2] * models.DENSE_DIM, {1: 0.9}


@pytest.fixture
def session_factory(admin_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=admin_engine, class_=Session, expire_on_commit=False)


@pytest.fixture
def tenant_id(admin_engine: Engine) -> Iterator[object]:
    tid = uuid4()
    with admin_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO tenants (id, slug, name) VALUES (:id, :slug, :name)"),
            {"id": str(tid), "slug": f"t-{tid}", "name": "ingest-test"},
        )
    yield tid
    with admin_engine.begin() as conn:
        conn.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": str(tid)})


def _doc(tenant: object) -> tuple[RawDocument, list[ParentChunk], list[ChildChunk]]:
    document = RawDocument(
        tenant_id=tenant,
        source_type="local_fs",
        source_uri="a.txt",
        content_hash=uuid4().hex,
        language="ru",
        access_level=AccessLevel.INTERNAL,
    )
    parent = ParentChunk(
        tenant_id=tenant,
        document_id=document.id,
        content="parent body",
        token_count=2,
        chunk_index=0,
        access_level=AccessLevel.INTERNAL,
    )
    child = ChildChunk(
        tenant_id=tenant,
        parent_id=parent.id,
        content="child body",
        token_count=2,
        chunk_index=0,
        access_level=AccessLevel.INTERNAL,
        dense_embedding=_DENSE,
        sparse_embedding={1: 0.5, 7: 0.3},
    )
    return document, [parent], [child]


async def test_save_document_commits_and_notifies(
    admin_engine: Engine, session_factory: sessionmaker[Session], tenant_id: object
) -> None:
    repo = SqlAlchemyIngestRepository(session_factory)
    document, parents, children = _doc(tenant_id)

    # LISTEN on a dedicated raw psycopg connection before the write.
    import psycopg  # noqa: PLC0415

    dsn = os.environ["POSTGRES_DSN"].replace("postgresql+psycopg://", "postgresql://")
    listen = psycopg.connect(dsn, autocommit=True)
    listen.execute("LISTEN ingest_events")

    assert await repo.document_exists(tenant_id, document.content_hash) is False  # type: ignore[arg-type]
    await repo.save_document(document, parents, children)
    assert await repo.document_exists(tenant_id, document.content_hash) is True  # type: ignore[arg-type]

    # Rows are persisted with status=indexed and the sparse/dense vectors round-trip.
    with session_factory() as session:
        row = session.get(models.Document, document.id)
        assert row is not None and row.status == "indexed"
        child_rows = session.query(models.ChildChunk).filter_by(parent_id=parents[0].id).all()
        assert len(child_rows) == 1
        assert child_rows[0].embedding is not None

    # The DB trigger emitted NOTIFY ingest_events for this document.
    notifies = []
    for note in listen.notifies(timeout=5.0):
        notifies.append(note)
        break
    listen.close()
    assert notifies, "expected a NOTIFY ingest_events from the trigger"
    payload = json.loads(notifies[0].payload)
    assert payload["document_id"] == str(document.id)
    assert payload["status"] == "indexed"


async def test_save_document_rolls_back_on_fk_violation(
    session_factory: sessionmaker[Session], tenant_id: object
) -> None:
    repo = SqlAlchemyIngestRepository(session_factory)
    document, parents, _ = _doc(tenant_id)
    # A child pointing at a non-existent parent => FK violation at commit => whole tx rolls back.
    orphan = ChildChunk(
        tenant_id=tenant_id,
        parent_id=uuid4(),
        content="orphan",
        token_count=1,
        chunk_index=0,
        dense_embedding=_DENSE,
        sparse_embedding={1: 0.1},
    )

    with pytest.raises(IntegrityError):
        await repo.save_document(document, parents, [orphan])

    # Nothing persisted (atomic rollback): the document is absent.
    assert await repo.document_exists(tenant_id, document.content_hash) is False  # type: ignore[arg-type]


async def test_reembed_corpus_updates_only_affected_rows(
    session_factory: sessionmaker[Session], tenant_id: object
) -> None:
    repo = SqlAlchemyIngestRepository(session_factory)
    document, parents, children = _doc(tenant_id)
    children[0].embedding_model_id = "old-model"
    children[0].embedding_version = 1
    await repo.save_document(document, parents, children)

    service = ReembeddingService(
        _StubEmbedder(),  # type: ignore[arg-type]
        repository=SqlAlchemyReembedRepository(session_factory),
        batch_size=16,
    )
    total = await service.reembed_corpus(tenant_id, "BAAI/bge-m3", 2)  # type: ignore[arg-type]

    assert total == 1
    with session_factory() as session:
        row = session.get(models.ChildChunk, children[0].id)
        assert row is not None
        assert row.embedding_model_id == "BAAI/bge-m3"
        assert row.embedding_version == 2
