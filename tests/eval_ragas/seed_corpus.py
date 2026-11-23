"""Seed the Stage-J golden corpus into Postgres so the live gate has something to retrieve.

The golden dataset (``tests/data/golden_dataset.json``) carries, per example, a reference answer,
the reference context text and the QRELS ``ground_truth_chunk_ids``. For the *offline* gate
(``--self-check``) those ids only need to match what the oracle probe replays, but for a *live* run
(``python -m tests.eval_ragas`` against the real Stage E/F pipeline) they must correspond to real
``child_chunks.id`` rows that the retriever can actually return - otherwise Hit Rate@5 / MRR@5 /
nDCG@5 are structurally 0 and the gate cannot run (Step-J review 🔴3).

This module is that missing seed procedure. For each golden sample it writes a deterministic
``documents`` -> ``parent_chunks`` -> ``child_chunks`` triple under a single evaluation tenant/user,
with ``child_chunks.id`` set to the sample's QRELS id and the context embedded by the injected
BGE-M3 embedder, honouring the sample's ``access_level``. Ids are derived with ``uuid5`` from the
sample id, so re-running is idempotent (an existing row is updated in place).

Run it once before a live gate::

    python -m tests.eval_ragas.seed_corpus --tenant-id <uuid> --user-id <uuid>

then::

    python -m tests.eval_ragas --tenant-id <uuid> --user-id <uuid>
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from uuid import NAMESPACE_URL, UUID, uuid5

from rag_core.contracts.interfaces import BaseEmbedder
from rag_core.db import models
from rag_core.db.session import SessionFactory
from tests.eval_ragas.dataset import GoldenSample, load_golden_dataset

# A fixed namespace so the derived document/parent ids are stable across seed runs.
_EVAL_NS = uuid5(NAMESPACE_URL, "enterprise-rag/eval/golden-corpus")

DEFAULT_TENANT_SLUG = "eval-golden"


def _document_id(sample: GoldenSample) -> UUID:
    return uuid5(_EVAL_NS, f"document:{sample.id}")


def _parent_id(sample: GoldenSample) -> UUID:
    return uuid5(_EVAL_NS, f"parent:{sample.id}")


def _child_id(sample: GoldenSample) -> UUID:
    # The QRELS id IS the child chunk's primary key, so the retriever returns exactly this id.
    return UUID(sample.ground_truth_chunk_ids[0])


async def seed_golden_corpus(
    session_factory: SessionFactory,
    embedder: BaseEmbedder,
    tenant_id: UUID,
    user_id: UUID,
    samples: Sequence[GoldenSample],
    *,
    tenant_slug: str = DEFAULT_TENANT_SLUG,
) -> int:
    """Seed/refresh the golden corpus; returns the number of child chunks written."""
    contexts = [
        (s.ground_truth_contexts[0] if s.ground_truth_contexts else s.ground_truth_answer)
        for s in samples
    ]
    dense_vectors, sparse_vectors = await embedder.embed_documents(contexts)

    return await asyncio.to_thread(
        _seed_sync,
        session_factory,
        tenant_id,
        user_id,
        list(samples),
        contexts,
        dense_vectors,
        sparse_vectors,
        tenant_slug,
    )


def _seed_sync(
    session_factory: SessionFactory,
    tenant_id: UUID,
    user_id: UUID,
    samples: list[GoldenSample],
    contexts: list[str],
    dense_vectors: list[list[float]],
    sparse_vectors: list[dict[int, float]],
    tenant_slug: str,
) -> int:
    from pgvector import SparseVector

    def _sparse(weights: dict[int, float] | None) -> object | None:
        if not weights:
            return None
        return SparseVector(weights, models.SPARSE_DIM)

    with session_factory() as session, session.begin():
        # Evaluation tenant + user (idempotent upsert by primary key).
        if session.get(models.Tenant, tenant_id) is None:
            session.add(
                models.Tenant(id=tenant_id, slug=tenant_slug, name="Evaluation Golden Corpus")
            )
        if session.get(models.User, user_id) is None:
            session.add(
                models.User(
                    id=user_id,
                    tenant_id=tenant_id,
                    channel_user_id=f"eval:{user_id}",
                    channel="web_widget",
                    access_level=max((s.access_level for s in samples), default=0),
                )
            )

        written = 0
        for sample, context, dense, sparse in zip(
            samples, contexts, dense_vectors, sparse_vectors, strict=True
        ):
            doc_id = _document_id(sample)
            parent_id = _parent_id(sample)
            child_id = _child_id(sample)
            level = int(sample.access_level)

            if session.get(models.Document, doc_id) is None:
                session.add(
                    models.Document(
                        id=doc_id,
                        tenant_id=tenant_id,
                        source_type="eval",
                        source_uri=f"eval://golden/{sample.id}",
                        content_hash=f"eval-{sample.id}",
                        status="indexed",
                        language="en",
                        access_level=level,
                    )
                )
            if session.get(models.ParentChunk, parent_id) is None:
                session.add(
                    models.ParentChunk(
                        id=parent_id,
                        tenant_id=tenant_id,
                        document_id=doc_id,
                        content=context,
                        token_count=max(1, len(context.split())),
                        chunk_index=0,
                        access_level=level,
                    )
                )

            child = session.get(models.ChildChunk, child_id)
            if child is None:
                session.add(
                    models.ChildChunk(
                        id=child_id,
                        tenant_id=tenant_id,
                        parent_id=parent_id,
                        content=context,
                        token_count=max(1, len(context.split())),
                        chunk_index=0,
                        access_level=level,
                        embedding=dense,
                        sparse_vector=_sparse(sparse),
                    )
                )
            else:
                child.content = context
                child.access_level = level
                child.embedding = dense
                child.sparse_vector = _sparse(sparse)
            written += 1

        return written


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m tests.eval_ragas.seed_corpus",
        description="Seed the Stage-J golden corpus into Postgres for a live evaluation run.",
    )
    parser.add_argument("--tenant-id", required=True, help="evaluation tenant UUID")
    parser.add_argument("--user-id", required=True, help="evaluation user UUID")
    parser.add_argument("--dataset", default=None, help="path to the golden dataset JSON")
    parser.add_argument(
        "--tenant-slug", default=DEFAULT_TENANT_SLUG, help="slug for the evaluation tenant"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        samples = load_golden_dataset(args.dataset)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    from rag_core.contracts.settings import Settings
    from rag_core.db.session import build_session_factory
    from rag_core.ingestion.embedder import BgeM3Embedder

    settings = Settings()
    # Seeding is an administrative write, so it uses the admin DSN (NOT the RLS app_user DSN).
    session_factory = build_session_factory(settings.postgres_dsn)
    embedder = BgeM3Embedder()

    written = asyncio.run(
        seed_golden_corpus(
            session_factory,
            embedder,
            UUID(args.tenant_id),
            UUID(args.user_id),
            samples,
            tenant_slug=args.tenant_slug,
        )
    )
    print(f"seeded {written} golden child chunks under tenant {args.tenant_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
