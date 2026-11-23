"""CLI entry point for the Stage J evaluation gate: ``python -m tests.eval_ragas``.

Exit codes (so CI can branch on them):

- ``0`` - the gate passed (every metric met its threshold);
- ``1`` - the gate ran but a metric regressed below its threshold (BLOCK the merge);
- ``2`` - a usage/dataset error (bad arguments, malformed golden dataset);
- ``3`` - the real RAG pipeline could not be wired (infra/deps not configured for a live run).

By default the gate wires the real Stage E/F pipeline from the project ``Settings``
(Postgres DSN, embedder, LLM). ``--self-check`` runs the gate fully offline against an oracle probe
(perfect retrieval/answers) to validate the gate plumbing without any infrastructure - useful as the
smoke step before a full run and in environments without a live corpus.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

from tests.eval_ragas.dataset import GoldenSample, load_golden_dataset
from tests.eval_ragas.gate import DEFAULT_SEED, EvalReport, Thresholds, evaluate, seed_everything
from tests.eval_ragas.probe import PipelineProbe, ProbeResult
from tests.eval_ragas.scoring import HeuristicGenerationScorer, build_generation_scorer

SMOKE_SAMPLE_SIZE = 3


class _OracleProbe:
    """An offline probe that replays the ground truth (perfect retrieval + answer).

    Used by ``--self-check`` to exercise the full gate (dataset -> metrics -> thresholds -> report)
    without a live pipeline. It must always pass the gate, proving the plumbing is correct.
    """

    name = "oracle"

    async def __call__(self, sample: GoldenSample) -> ProbeResult:
        # The idealized pipeline fully grounds its answer: the answer text is itself part of the
        # retrieved context, so Faithfulness is maximal and the gate plumbing validates as PASS.
        contexts = [*sample.ground_truth_contexts, sample.ground_truth_answer]
        return ProbeResult(
            retrieved_chunk_ids=list(sample.ground_truth_chunk_ids),
            answer=sample.ground_truth_answer,
            retrieved_contexts=contexts,
            is_confident=True,
        )


def _select_samples(samples: list[GoldenSample], sample_size: int | None) -> list[GoldenSample]:
    """Deterministically take the first ``sample_size`` samples (the list order is stable)."""
    if sample_size is None or sample_size >= len(samples):
        return samples
    if sample_size <= 0:
        raise ValueError("sample size must be a positive integer")
    return samples[:sample_size]


def _build_real_probe(tenant_id: str | None, user_id: str | None) -> PipelineProbe:
    """Wire the real Stage E/F pipeline from Settings; raise a clear error if infra is missing."""
    from uuid import UUID

    from sqlalchemy.orm import Session

    from rag_core.contracts.settings import Settings
    from rag_core.db.session import build_session_factory, register_tenant_guc_listener
    from rag_core.generation.pipeline import build_generation_pipeline
    from rag_core.ingestion.embedder import BgeM3Embedder
    from rag_core.retrieval.pipeline import build_search_pipeline
    from tests.eval_ragas.pipeline_probe import (
        EVAL_ACCESS_LEVEL,
        EVAL_TENANT_ID,
        RealPipelineProbe,
    )

    settings = Settings()
    # User-retrieval MUST run under the app_user role (NOBYPASSRLS), never the BYPASSRLS
    # service_role (core/eval_and_ops.md §1.3 p.6): fall back to postgres_dsn only if the
    # RLS-scoped DSN is not configured.
    retrieval_dsn = settings.postgres_app_dsn or settings.postgres_dsn
    if not retrieval_dsn:
        raise RuntimeError(
            "neither POSTGRES_APP_DSN nor POSTGRES_DSN is configured; cannot run a live evaluation"
        )
    if tenant_id is None or user_id is None:
        raise RuntimeError(
            "a live run requires --tenant-id and --user-id (the seeded evaluation corpus owner)"
        )

    # The retrieval stack creates its ORM sessions internally, so the RLS GUCs cannot be set
    # per-request via session.info from the outside. Wrap the factory to stamp the current
    # evaluation tenant/level (ContextVars set by the probe) onto every session and register the
    # canonical after_begin listener, so each transaction does SET LOCAL app.current_tenant_id /
    # app.user_access_level and the RLS policies return rows instead of fail-closing to 0
    # (storage_schema.md §4.0).
    base_factory = build_session_factory(retrieval_dsn)
    register_tenant_guc_listener(base_factory)

    def session_factory() -> Session:
        session = base_factory()
        session.info["tenant_id"] = EVAL_TENANT_ID.get()
        session.info["user_access_level"] = EVAL_ACCESS_LEVEL.get()
        return session

    embedder = BgeM3Embedder()
    search_pipeline = build_search_pipeline(session_factory, embedder, settings)
    generation_pipeline = build_generation_pipeline(search_pipeline, settings=settings)
    return RealPipelineProbe(
        search_pipeline,
        generation_pipeline,
        tenant_id=UUID(tenant_id),
        user_id=UUID(user_id),
    )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m tests.eval_ragas",
        description="Stage J - RAGAS & IR evaluation gate (blocks a merge on a regression).",
    )
    parser.add_argument("--dataset", default=None, help="path to the golden dataset JSON")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="RNG seed (reproducibility)")
    parser.add_argument(
        "--sample-size", type=int, default=None, help="evaluate only the first N samples"
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help=f"smoke run on the first {SMOKE_SAMPLE_SIZE} samples (overrides --sample-size)",
    )
    parser.add_argument(
        "--no-ragas",
        action="store_true",
        help="force the deterministic heuristic scorer (never import ragas)",
    )
    parser.add_argument(
        "--self-check",
        action="store_true",
        help="run fully offline against an oracle probe (validates gate plumbing, no infra)",
    )
    parser.add_argument(
        "--list", action="store_true", help="print a dataset summary and exit (no evaluation)"
    )
    parser.add_argument("--tenant-id", default=None, help="tenant UUID for a live run")
    parser.add_argument("--user-id", default=None, help="user UUID for a live run")
    parser.add_argument(
        "--json-report",
        default=None,
        help="also write the machine-readable report JSON to this path (for CI artifacts/trends)",
    )
    return parser.parse_args(argv)


def _run(args: argparse.Namespace) -> int:
    try:
        samples = load_golden_dataset(args.dataset)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.list:
        print(f"golden dataset: {len(samples)} samples")
        for s in samples:
            print(f"  {s.id}  access_level={s.access_level}  qrels={len(s.ground_truth_chunk_ids)}")
        return 0

    sample_size = SMOKE_SAMPLE_SIZE if args.smoke else args.sample_size
    try:
        selected = _select_samples(samples, sample_size)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    seed_everything(args.seed)
    scorer = HeuristicGenerationScorer() if args.no_ragas else build_generation_scorer()

    if args.self_check:
        probe: PipelineProbe = _OracleProbe()
    else:
        try:
            probe = _build_real_probe(args.tenant_id, args.user_id)
        except Exception as exc:  # noqa: BLE001 - surface any wiring failure as exit code 3
            print(f"error: cannot wire the live RAG pipeline: {exc}", file=sys.stderr)
            print(
                "hint: configure POSTGRES_DSN + an embedder/LLM and pass --tenant-id/--user-id, "
                "or use --self-check to validate the gate offline.",
                file=sys.stderr,
            )
            return 3

    report: EvalReport = asyncio.run(
        evaluate(selected, probe, scorer, Thresholds(), seed=args.seed)
    )
    print(report.format_report())

    if args.json_report:
        import json
        from pathlib import Path

        path = Path(args.json_report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
        print(f"wrote JSON report to {path}", file=sys.stderr)

    return 0 if report.passed else 1


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, run the gate and return the process exit code."""
    return _run(_parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
