"""Taskiq durable task for ingestion (`core/architecture.md` §3, `IMPLEMENTATION_PLAN.md` Stage D).

The ingestion queue is a durable Postgres-backed Taskiq queue (``taskiq-pg``): one file = one task,
with retry + exponential backoff. The single task

    process_document_task(tenant_id, storage_ref, content_hash, access_level)

serves drag-and-drop Admin uploads, the local-folder File Watcher, and external connectors alike.
The payload is JSON-serializable (string UUIDs + ints), never a live ``IngestionPipeline`` object -
the pipeline is resolved from the worker composition root inside the task.

Taskiq and ``taskiq-pg`` are imported lazily (:func:`build_broker`) so the core package stays
importable without the dependency. The pure business logic lives in :func:`run_process_document`
(easily unit-tested); :func:`register_process_document_task` attaches the Taskiq decorator to a
broker and wires in the pipeline provider - this is what the worker entrypoint calls.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from rag_core.contracts.enums import AccessLevel, DocumentStatus
from rag_core.ingestion.pipeline import IngestionPipeline

# Canonical queue / channel names (single source for worker + NOTIFY listeners).
INGEST_QUEUE_NAME = "ingest_tasks"
INGEST_NOTIFY_CHANNEL = "ingest_events"

# The canonical registered Taskiq task name (stable across workers/producers).
PROCESS_DOCUMENT_TASK_NAME = "process_document_task"

# A provider that resolves the configured pipeline (the worker composition root supplies it).
PipelineProvider = Callable[[], IngestionPipeline]


async def run_process_document(
    pipeline: IngestionPipeline,
    tenant_id: UUID,
    storage_ref: str,
    content_hash: str,
    access_level: int = int(AccessLevel.PUBLIC),
) -> DocumentStatus:
    """Executes one ingest job through the pipeline and returns the resulting document status.

    This is the transport-agnostic core of ``process_document_task`` - the Taskiq wrapper only
    resolves the ``IngestionPipeline`` from the composition root and delegates here. The
    ``access_level`` is a raw ``int`` (the queue payload is JSON-serializable), normalized to enum.
    """
    result = await pipeline.process_document(
        tenant_id=tenant_id,
        storage_ref=storage_ref,
        content_hash=content_hash,
        access_level=AccessLevel(access_level),
    )
    return result.status


def build_broker(postgres_dsn: str) -> Any:
    """Builds the Taskiq Postgres broker (lazy import of ``taskiq-pg``).

    The worker composition root calls this, registers ``process_document_task`` against the returned
    broker, and starts the worker. Kept out of module import so the core stays dependency-light.
    """
    try:
        from taskiq_pg import AsyncpgBroker
    except ImportError as exc:  # pragma: no cover - exercised only without the heavy dep
        raise RuntimeError(
            "taskiq-pg is not installed; install it to run the ingest worker "
            "(pip install taskiq-pg)"
        ) from exc
    return AsyncpgBroker(dsn=postgres_dsn)


def register_process_document_task(
    broker: Any,
    get_pipeline: PipelineProvider,
) -> Callable[[str, str, str, int], Awaitable[str]]:
    """Registers the durable ``process_document_task`` against ``broker`` and returns it.

    The task accepts a JSON payload (``tenant_id``/``storage_ref``/``content_hash`` as strings,
    ``access_level`` as int), resolves the pipeline via ``get_pipeline`` (the composition root) and
    returns the resulting document status as a string. Taskiq's own retry middleware provides the
    durable retry; the embedder's in-process retry handles transient encode failures.
    """

    @broker.task(task_name=PROCESS_DOCUMENT_TASK_NAME)  # type: ignore[untyped-decorator]
    async def process_document_task(
        tenant_id: str,
        storage_ref: str,
        content_hash: str,
        access_level: int = int(AccessLevel.PUBLIC),
    ) -> str:
        status = await run_process_document(
            get_pipeline(),
            UUID(tenant_id),
            storage_ref,
            content_hash,
            access_level,
        )
        return status.value

    return process_document_task  # type: ignore[no-any-return]
