"""Stage J - Evaluation Gate (RAGAS & Golden Dataset).

This package implements the quality regression gate described in
``IMPLEMENTATION_PLAN.md`` Stage J and ``core/eval_and_ops.md`` §1:

- the BEIR/QRELS-compatible golden dataset (``tests/data/golden_dataset.json``) carrying an
  ``access_level`` per example (:mod:`tests.eval_ragas.dataset`);
- deterministic Information-Retrieval metrics computed from ``ground_truth_chunk_ids`` -
  ``Hit Rate@k``, ``MRR@k`` and ``nDCG@k`` (:mod:`tests.eval_ragas.metrics`);
- the RAGAS generation/alignment metrics (``Faithfulness``, ``Answer Relevance``,
  ``Context Precision``, ``Context Recall``) via a pluggable scorer that uses the real ``ragas``
  package when installed and a deterministic, dependency-free fallback otherwise
  (:mod:`tests.eval_ragas.scoring`);
- the SLO thresholds and the pass/fail gate that blocks a merge on a regression
  (:mod:`tests.eval_ragas.gate`).

The runnable CI gate is ``python -m tests.eval_ragas`` (see :mod:`tests.eval_ragas.__main__`).

The core logic is pure and injection-friendly: the gate consumes a :class:`PipelineProbe` callable
so it is unit-testable without a live Postgres/LLM, while ``__main__`` wires the real Stage E/F
pipeline. A fixed ``seed`` makes the run reproducible (``core/eval_and_ops.md`` §1 / Stage J test
coverage note).
"""

from __future__ import annotations

from tests.eval_ragas.dataset import GoldenSample, load_golden_dataset
from tests.eval_ragas.gate import (
    EvalReport,
    SampleOutcome,
    Thresholds,
    evaluate,
)
from tests.eval_ragas.metrics import (
    IRMetrics,
    hit_rate_at_k,
    mrr_at_k,
    ndcg_at_k,
)
from tests.eval_ragas.probe import PipelineProbe, ProbeResult
from tests.eval_ragas.scoring import (
    GenerationScorer,
    HeuristicGenerationScorer,
    RagasGenerationScorer,
    RagasScores,
    build_generation_scorer,
)

__all__ = [
    "EvalReport",
    "GenerationScorer",
    "GoldenSample",
    "HeuristicGenerationScorer",
    "IRMetrics",
    "PipelineProbe",
    "ProbeResult",
    "RagasGenerationScorer",
    "RagasScores",
    "SampleOutcome",
    "Thresholds",
    "build_generation_scorer",
    "evaluate",
    "hit_rate_at_k",
    "load_golden_dataset",
    "mrr_at_k",
    "ndcg_at_k",
]
