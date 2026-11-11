"""Ingestion worker composition root (`IMPLEMENTATION_PLAN.md` Stage D).

This is the runnable transport half of Stage D: it wires the pure ingestion domain
(``rag_core.ingestion``) to the real infrastructure - a SQLAlchemy ``Session`` bound to Postgres,
the configured object storage, the BGE-M3 embedder, the token-aware splitter - and exposes:

- :func:`build_ingestion_pipeline` - the composition root that builds a ready
  :class:`~rag_core.ingestion.pipeline.IngestionPipeline` from ``Settings``;
- :func:`build_worker` - builds the Taskiq Postgres broker and registers ``process_document_task``;
- :func:`scan_and_enqueue` - the File Watcher loop that turns every new, stable, not-yet-indexed
  file into exactly one enqueued task ("1 file = 1 task").

Taskiq is imported lazily (through ``rag_core.ingestion.tasks.build_broker``), so importing this
module does not require the worker dependency - only actually building the broker does.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import UUID

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from rag_core.contracts.settings import Settings
from rag_core.ingestion.embedder import BgeM3Embedder
from rag_core.ingestion.object_storage import build_object_storage
from rag_core.ingestion.pipeline import IngestionPipeline
from rag_core.ingestion.repository import SqlAlchemyIngestRepository
from rag_core.ingestion.splitter import build_splitter
from rag_core.ingestion.tasks import register_process_document_task
from rag_core.ingestion.watcher import FileWatcher, WatchedFile

SessionFactory = Callable[[], Session]

# Logical provenance recorded on the document, derived from the active object-storage provider
# (so a local-FS ingest is not mislabelled "s3").
_SOURCE_TYPE_BY_PROVIDER = {"local": "local_fs", "s3": "s3", "minio": "s3", "r2": "s3"}


def build_session_factory(settings: Settings) -> SessionFactory:
    """Builds a synchronous ``sessionmaker`` bound to the admin DSN (ingest workers use admin)."""
    engine = create_engine(settings.postgres_dsn, pool_pre_ping=True, future=True)
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def build_ingestion_pipeline(
    settings: Settings,
    *,
    session_factory: SessionFactory | None = None,
) -> IngestionPipeline:
    """Composition root: a ready :class:`IngestionPipeline` built entirely from ``Settings``.

    The embedder batch size, the splitter bounds and the retry policy all come from ``Settings``
    (no hardcoded constants), and the document's ``source_type`` reflects the active storage
    provider.
    """
    factory = session_factory or build_session_factory(settings)
    storage = build_object_storage(settings)
    embedder = BgeM3Embedder(
        model_name=settings.embedding_model, batch_size=settings.embedding_batch_size
    )
    repository = SqlAlchemyIngestRepository(factory)
    splitter = build_splitter(settings)
    source_type = _SOURCE_TYPE_BY_PROVIDER.get(settings.object_storage_provider, "s3")
    return IngestionPipeline(
        storage=storage,
        embedder=embedder,
        repository=repository,
        splitter=splitter,
        embedding_model_id=settings.embedding_model,
        embedding_version=settings.embedding_version,
        embed_attempts=settings.ingest_max_retries,
        embed_retry_base_delay=settings.ingest_retry_base_delay,
        embed_retry_max_delay=settings.ingest_retry_max_delay,
        default_source_type=source_type,
    )


def build_worker(settings: Settings) -> tuple[object, Callable[..., Awaitable[str]]]:
    """Builds the Taskiq Postgres broker and registers ``process_document_task`` on it.

    Returns ``(broker, task)``. The pipeline is lazily built once and shared across task
    invocations (the composition root is resolved on first use, not per message).
    """
    from rag_core.ingestion.tasks import build_broker

    broker = build_broker(settings.postgres_dsn)
    _pipeline: list[IngestionPipeline] = []

    def _get_pipeline() -> IngestionPipeline:
        if not _pipeline:
            _pipeline.append(build_ingestion_pipeline(settings))
        return _pipeline[0]

    task = register_process_document_task(broker, _get_pipeline)
    return broker, task


async def scan_and_enqueue(
    watcher: FileWatcher,
    tenant_id: UUID,
    enqueue: Callable[[WatchedFile], Awaitable[None]],
    *,
    known_hashes: set[str] | None = None,
) -> list[WatchedFile]:
    """Scans the tenant folder and enqueues exactly one task per stable, new file.

    ``enqueue`` is the transport callback (e.g. ``process_document_task.kiq(...)``). Returns the
    files that were enqueued so the caller can track/record them. "1 file = 1 task".
    """
    files = watcher.scan(tenant_id, known_hashes=known_hashes)
    for watched in files:
        await enqueue(watched)
    return files


__all__ = [
    "build_session_factory",
    "build_ingestion_pipeline",
    "build_worker",
    "scan_and_enqueue",
]
