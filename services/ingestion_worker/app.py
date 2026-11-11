"""Taskiq worker entrypoint for ingestion (`IMPLEMENTATION_PLAN.md` Stage D).

Run the durable ingest worker with::

    taskiq worker services.ingestion_worker.app:broker

Importing this module builds the broker and registers ``process_document_task`` (resolving the
pipeline from :class:`~rag_core.contracts.settings.Settings`). It requires the ``ingestion`` extra
(``taskiq-pg`` etc.) to be installed - it is the composition root the worker process boots from,
not something imported by the core package or the unit tests.
"""

from __future__ import annotations

from rag_core.contracts.settings import Settings
from services.ingestion_worker import build_worker

settings = Settings()

# ``broker`` is the object the Taskiq CLI loads; ``process_document_task`` is the registered task.
broker, process_document_task = build_worker(settings)
