"""Stage D - Ingestion Pipeline & Embedder (`core/architecture.md` §3, `core/storage_schema.md`).

This subpackage implements the background data-ingestion ETL flow:

- `object_storage` - the `BaseObjectStorage` port implementations (local FS + S3/MinIO/R2),
  strictly per-tenant (prefix ``{tenant_id}/``);
- `parser`         - the Docling-backed document parser (`.pdf/.docx/.md/.txt/...`) with a pure
  fallback for plain-text formats (so the pipeline is importable without heavy deps);
- `splitter`       - the token-aware Parent-Child splitter (parent 800-1200, child 150-250,
  overlap 20-40 tokens) that never breaks a word;
- `embedder`       - the BGE-M3 `BaseEmbedder` (dense 1024 + sparse) with strict batching by 32;
- `dedup`          - SHA-256 content hashing and near-duplicate child-chunk suppression;
- `pipeline`       - the orchestrator (idempotency, transactional upsert, status transitions,
  ``NOTIFY ingest_events``) plus the `BaseIngestRepository` port;
- `reembedding`    - the re-embedding planner/service for an embedding-model version bump without
  search downtime (old and new vectors coexist, the switch is atomic);
- `watcher`        - the File Watcher (debounce, ``st_size`` stability check, SHA-256 dedup);
- `tasks`          - the Taskiq task ``process_document_task`` (durable Postgres queue, retry).

Heavy third-party dependencies (Docling, FlagEmbedding, Taskiq, boto3) are imported lazily so the
package stays importable and unit-testable with in-memory fakes and no external services.
"""

from __future__ import annotations

from rag_core.ingestion.dedup import (
    compute_content_hash,
    near_duplicate_key,
    suppress_near_duplicates,
)
from rag_core.ingestion.embedder import BgeM3Embedder, batched
from rag_core.ingestion.object_storage import (
    LocalObjectStorage,
    S3ObjectStorage,
    build_object_storage,
)
from rag_core.ingestion.parser import DocumentParser, ParsedDocument
from rag_core.ingestion.pipeline import (
    BaseIngestRepository,
    IngestionPipeline,
    IngestResult,
    retry_async,
)
from rag_core.ingestion.reembedding import (
    BaseReembedRepository,
    ReembeddingPlanner,
    ReembeddingService,
)
from rag_core.ingestion.repository import (
    SqlAlchemyIngestRepository,
    SqlAlchemyReembedRepository,
)
from rag_core.ingestion.splitter import (
    ChildDraft,
    ParentChildSplitter,
    ParentDraft,
    SplitterConfig,
    build_hf_token_counter,
    build_splitter,
)
from rag_core.ingestion.tasks import (
    INGEST_NOTIFY_CHANNEL,
    INGEST_QUEUE_NAME,
    PROCESS_DOCUMENT_TASK_NAME,
    build_broker,
    register_process_document_task,
    run_process_document,
)
from rag_core.ingestion.watcher import FileWatcher, WatchedFile

__all__ = [
    # object storage
    "LocalObjectStorage",
    "S3ObjectStorage",
    "build_object_storage",
    # parser
    "DocumentParser",
    "ParsedDocument",
    # splitter
    "ChildDraft",
    "ParentChildSplitter",
    "ParentDraft",
    "SplitterConfig",
    "build_hf_token_counter",
    "build_splitter",
    # embedder
    "BgeM3Embedder",
    "batched",
    # dedup
    "compute_content_hash",
    "near_duplicate_key",
    "suppress_near_duplicates",
    # pipeline
    "BaseIngestRepository",
    "IngestionPipeline",
    "IngestResult",
    "retry_async",
    # re-embedding
    "BaseReembedRepository",
    "ReembeddingPlanner",
    "ReembeddingService",
    # concrete persistence
    "SqlAlchemyIngestRepository",
    "SqlAlchemyReembedRepository",
    # tasks
    "INGEST_NOTIFY_CHANNEL",
    "INGEST_QUEUE_NAME",
    "PROCESS_DOCUMENT_TASK_NAME",
    "build_broker",
    "register_process_document_task",
    "run_process_document",
    # watcher
    "FileWatcher",
    "WatchedFile",
]
