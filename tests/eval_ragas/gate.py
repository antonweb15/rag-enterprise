"""The evaluation gate: thresholds, aggregation and the pass/fail decision.

This is the heart of Stage J. :func:`evaluate` drives the pipeline probe over the golden dataset,
computes the IR metrics (deterministically) and the RAGAS generation metrics (via the injected
scorer), aggregates them and compares the corpus-level means against the SLO
:class:`Thresholds` from ``core/eval_and_ops.md`` §1.2. A single sub-threshold metric fails the
gate (``report.passed is False``) so a CI job can block the merge on a quality regression.

The function is pure (no global state) and seeded via :func:`seed_everything`, so repeated runs on
the same inputs yield identical reports (Stage J reproducibility requirement).
"""

from __future__ import annotations

import os
import random
from collections.abc import Sequence
from dataclasses import dataclass, field

from tests.eval_ragas.dataset import GoldenSample
from tests.eval_ragas.metrics import IRMetrics
from tests.eval_ragas.probe import PipelineProbe
from tests.eval_ragas.scoring import GenerationScorer, RagasScores

DEFAULT_SEED = 42


@dataclass(frozen=True, slots=True)
class Thresholds:
    """Target SLO thresholds (the merge-blocking gate) - ``core/eval_and_ops.md`` §1.2."""

    # Generation & alignment (RAGAS)
    faithfulness: float = 0.90
    answer_relevance: float = 0.85
    context_precision: float = 0.85
    context_recall: float = 0.85
    # Retrieval (IR)
    hit_rate_at_5: float = 0.85
    mrr_at_5: float = 0.75
    ndcg_at_5: float = 0.80


@dataclass(frozen=True, slots=True)
class SampleOutcome:
    """Per-sample record retained for the report (debugging a regression)."""

    sample_id: str
    access_level: int
    is_confident: bool
    retrieved_chunk_ids: list[str]
    scores: RagasScores


@dataclass(slots=True)
class EvalReport:
    """The aggregated gate result."""

    thresholds: Thresholds
    ir: IRMetrics
    faithfulness: float
    answer_relevance: float
    context_precision: float
    context_recall: float
    sample_count: int
    scorer_name: str
    seed: int
    outcomes: list[SampleOutcome] = field(default_factory=list)

    @property
    def failures(self) -> list[str]:
        """Human-readable list of ``metric: actual < threshold`` for every breached metric."""
        checks = {
            "faithfulness": (self.faithfulness, self.thresholds.faithfulness),
            "answer_relevance": (self.answer_relevance, self.thresholds.answer_relevance),
            "context_precision": (self.context_precision, self.thresholds.context_precision),
            "context_recall": (self.context_recall, self.thresholds.context_recall),
            "hit_rate@5": (self.ir.hit_rate_at_5, self.thresholds.hit_rate_at_5),
            "mrr@5": (self.ir.mrr_at_5, self.thresholds.mrr_at_5),
            "ndcg@5": (self.ir.ndcg_at_5, self.thresholds.ndcg_at_5),
        }
        return [
            f"{name}: {actual:.4f} < {target:.2f}"
            for name, (actual, target) in checks.items()
            if actual + 1e-9 < target
        ]

    @property
    def passed(self) -> bool:
        """``True`` only when every metric meets or exceeds its threshold."""
        return not self.failures

    @property
    def is_ragas_scored(self) -> bool:
        """``True`` only when the real LLM-judged RAGAS scorer produced the generation metrics."""
        return self.scorer_name == "ragas"

    def to_dict(self) -> dict[str, object]:
        """A machine-readable snapshot of the gate result (for a CI JSON artifact / trends)."""
        return {
            "status": "PASS" if self.passed else "FAIL",
            "passed": self.passed,
            "scorer": self.scorer_name,
            "is_ragas_scored": self.is_ragas_scored,
            "seed": self.seed,
            "sample_count": self.sample_count,
            "metrics": {
                "faithfulness": self.faithfulness,
                "answer_relevance": self.answer_relevance,
                "context_precision": self.context_precision,
                "context_recall": self.context_recall,
                "hit_rate@5": self.ir.hit_rate_at_5,
                "mrr@5": self.ir.mrr_at_5,
                "ndcg@5": self.ir.ndcg_at_5,
            },
            "thresholds": {
                "faithfulness": self.thresholds.faithfulness,
                "answer_relevance": self.thresholds.answer_relevance,
                "context_precision": self.thresholds.context_precision,
                "context_recall": self.thresholds.context_recall,
                "hit_rate@5": self.thresholds.hit_rate_at_5,
                "mrr@5": self.thresholds.mrr_at_5,
                "ndcg@5": self.thresholds.ndcg_at_5,
            },
            "failures": self.failures,
            "samples": [
                {
                    "sample_id": o.sample_id,
                    "access_level": o.access_level,
                    "is_confident": o.is_confident,
                    "retrieved_chunk_ids": o.retrieved_chunk_ids,
                    "faithfulness": o.scores.faithfulness,
                    "answer_relevance": o.scores.answer_relevance,
                    "context_precision": o.scores.context_precision,
                    "context_recall": o.scores.context_recall,
                }
                for o in self.outcomes
            ],
        }

    def format_report(self) -> str:
        """Render a compact, aligned text report for the CI log."""
        status = "PASS" if self.passed else "FAIL"
        lines = [
            f"RAGAS Evaluation Gate: {status}",
            f"  samples={self.sample_count}  scorer={self.scorer_name}  seed={self.seed}",
        ]
        if not self.is_ragas_scored:
            lines.append(
                "  NOTE: generation metrics are a DETERMINISTIC HEURISTIC approximation "
                "(ragas not installed), NOT LLM-judged RAGAS; install the 'eval' extra for the "
                "real gate."
            )
        lines += [
            "  Generation & Alignment (RAGAS):",
            f"    faithfulness       {self.faithfulness:.4f}  (>= {self.thresholds.faithfulness})",
            f"    answer_relevance   {self.answer_relevance:.4f}"
            f"  (>= {self.thresholds.answer_relevance})",
            f"    context_precision  {self.context_precision:.4f}"
            f"  (>= {self.thresholds.context_precision})",
            f"    context_recall     {self.context_recall:.4f}"
            f"  (>= {self.thresholds.context_recall})",
            "  Retrieval (IR):",
            f"    hit_rate@5         {self.ir.hit_rate_at_5:.4f}"
            f"  (>= {self.thresholds.hit_rate_at_5})",
            f"    mrr@5              {self.ir.mrr_at_5:.4f}  (>= {self.thresholds.mrr_at_5})",
            f"    ndcg@5             {self.ir.ndcg_at_5:.4f}  (>= {self.thresholds.ndcg_at_5})",
        ]
        if self.failures:
            lines.append("  Breached thresholds:")
            lines.extend(f"    - {f}" for f in self.failures)
        return "\n".join(lines)


def seed_everything(seed: int = DEFAULT_SEED) -> None:
    """Fix every RNG that can influence the run so the gate is reproducible (Stage J).

    ``PYTHONHASHSEED`` is read by CPython only at interpreter startup, so setting it here would be a
    no-op; we export it only so a freshly re-``exec``'d child process (if any) inherits it, and do
    not rely on it for this process. The RNGs that actually affect the run - ``random``, ``numpy``
    and ``torch`` (the dominant source of LLM-judge / embedder non-determinism) - are seeded
    directly. ``numpy``/``torch`` are optional and seeded only when importable.
    """
    os.environ.setdefault("PYTHONHASHSEED", str(seed))
    random.seed(seed)
    try:  # numpy is optional; seed it only if present
        import numpy as np  # type: ignore[import-not-found]

        np.random.seed(seed)
    except ImportError:
        pass
    try:  # torch drives the embedder/reranker/LLM-judge; seed it when present
        import torch  # type: ignore[import-not-found]

        torch.manual_seed(seed)
        if torch.cuda.is_available():  # pragma: no cover - depends on the hardware
            torch.cuda.manual_seed_all(seed)
    except ImportError:
        pass


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


async def evaluate(
    samples: Sequence[GoldenSample],
    probe: PipelineProbe,
    scorer: GenerationScorer,
    thresholds: Thresholds | None = None,
    *,
    k: int = 5,
    seed: int = DEFAULT_SEED,
) -> EvalReport:
    """Run the full gate over ``samples`` and return the aggregated :class:`EvalReport`.

    For each sample the ``probe`` produces a :class:`~tests.eval_ragas.probe.ProbeResult`; the
    ordered ``retrieved_chunk_ids`` feed the IR metrics and the ``scorer`` produces the four RAGAS
    metrics. The corpus-level means are compared against ``thresholds``.
    """
    if not samples:
        raise ValueError("cannot evaluate an empty sample set")

    seed_everything(seed)
    resolved_thresholds = thresholds or Thresholds()

    outcomes: list[SampleOutcome] = []
    retrieved_per_query: list[list[str]] = []
    relevant_per_query: list[list[str]] = []

    for sample in samples:
        result = await probe(sample)
        scores = scorer.score(sample, result)

        retrieved_per_query.append(result.retrieved_chunk_ids)
        relevant_per_query.append(sample.ground_truth_chunk_ids)
        outcomes.append(
            SampleOutcome(
                sample_id=sample.id,
                access_level=sample.access_level,
                is_confident=result.is_confident,
                retrieved_chunk_ids=result.retrieved_chunk_ids,
                scores=scores,
            )
        )

    ir = IRMetrics.from_queries(retrieved_per_query, relevant_per_query, k=k)

    return EvalReport(
        thresholds=resolved_thresholds,
        ir=ir,
        faithfulness=_mean([o.scores.faithfulness for o in outcomes]),
        answer_relevance=_mean([o.scores.answer_relevance for o in outcomes]),
        context_precision=_mean([o.scores.context_precision for o in outcomes]),
        context_recall=_mean([o.scores.context_recall for o in outcomes]),
        sample_count=len(outcomes),
        scorer_name=scorer.name,
        seed=seed,
        outcomes=outcomes,
    )
