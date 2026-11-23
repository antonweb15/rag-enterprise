"""Stage J - Evaluation Gate (RAGAS) unit tests.

These tests are the regression harness for the gate itself: they verify the deterministic IR
metrics, the heuristic RAGAS scorer, the pass/fail threshold logic, dataset validation, seed
reproducibility and the CLI exit codes. The full RAGAS run against the live pipeline is the gate
proper (``python -m tests.eval_ragas``); here we exercise its pure, infra-free core with an oracle
and a deliberately degraded probe.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from tests.eval_ragas import (
    GoldenSample,
    HeuristicGenerationScorer,
    IRMetrics,
    ProbeResult,
    Thresholds,
    evaluate,
    hit_rate_at_k,
    load_golden_dataset,
    mrr_at_k,
    ndcg_at_k,
)
from tests.eval_ragas.__main__ import main
from tests.eval_ragas.dataset import DEFAULT_DATASET_PATH

# --------------------------------------------------------------------------- probes / fixtures


class _OracleProbe:
    """Perfect retrieval + answer: must pass the gate."""

    name = "oracle"

    async def __call__(self, sample: GoldenSample) -> ProbeResult:
        contexts = [*sample.ground_truth_contexts, sample.ground_truth_answer]
        return ProbeResult(
            retrieved_chunk_ids=list(sample.ground_truth_chunk_ids),
            answer=sample.ground_truth_answer,
            retrieved_contexts=contexts,
            is_confident=True,
        )


class _MissProbe:
    """Retrieves an unrelated chunk and an empty answer: must fail the gate."""

    name = "miss"

    async def __call__(self, sample: GoldenSample) -> ProbeResult:
        return ProbeResult(
            retrieved_chunk_ids=["00000000-0000-0000-0000-000000000000"],
            answer="",
            retrieved_contexts=[],
            is_confident=False,
        )


def _sample(chunk_id: str = "c1", level: int = 0) -> GoldenSample:
    return GoldenSample(
        id="s1",
        question="q",
        ground_truth_answer="the parent chunk size is 800 to 1200 tokens",
        ground_truth_contexts=["the parent chunk size is 800 to 1200 tokens"],
        ground_truth_chunk_ids=[chunk_id],
        access_level=level,
    )


# --------------------------------------------------------------------------- IR metrics


def test_hit_rate_at_k_detects_relevant_in_top_k() -> None:
    assert hit_rate_at_k(["a", "b", "c"], {"c"}, k=5) == 1.0
    assert hit_rate_at_k(["a", "b", "c"], {"c"}, k=2) == 0.0
    assert hit_rate_at_k([], {"c"}, k=5) == 0.0


def test_mrr_at_k_uses_first_relevant_rank() -> None:
    assert mrr_at_k(["x", "rel", "y"], {"rel"}, k=5) == pytest.approx(1 / 2)
    assert mrr_at_k(["rel", "x"], {"rel"}, k=5) == pytest.approx(1.0)
    assert mrr_at_k(["x", "y"], {"rel"}, k=5) == 0.0


def test_ndcg_at_k_matches_manual_computation() -> None:
    # One relevant chunk at rank 2 => DCG = 1/log2(3); ideal (rank 1) => 1/log2(2) = 1.
    expected = (1 / math.log2(3)) / (1 / math.log2(2))
    assert ndcg_at_k(["x", "rel", "y"], {"rel"}, k=5) == pytest.approx(expected)
    # Relevant at rank 1 is perfect.
    assert ndcg_at_k(["rel", "x"], {"rel"}, k=5) == pytest.approx(1.0)


def test_ir_metrics_aggregate_means() -> None:
    ir = IRMetrics.from_queries(
        retrieved_per_query=[["rel"], ["x", "rel"]],
        relevant_per_query=[["rel"], ["rel"]],
        k=5,
    )
    assert ir.query_count == 2
    assert ir.hit_rate_at_5 == pytest.approx(1.0)
    assert ir.mrr_at_5 == pytest.approx((1.0 + 0.5) / 2)


def test_ir_metrics_length_mismatch_raises() -> None:
    with pytest.raises(ValueError):
        IRMetrics.from_queries([["a"]], [["a"], ["b"]])


# --------------------------------------------------------------------------- heuristic scorer


def test_heuristic_scorer_perfect_on_oracle_output() -> None:
    scorer = HeuristicGenerationScorer()
    sample = _sample()
    result = ProbeResult(
        retrieved_chunk_ids=["c1"],
        answer=sample.ground_truth_answer,
        retrieved_contexts=sample.ground_truth_contexts,
        is_confident=True,
    )
    scores = scorer.score(sample, result)
    assert scores.context_precision == pytest.approx(1.0)
    assert scores.context_recall == pytest.approx(1.0)
    assert scores.answer_relevance == pytest.approx(1.0)
    assert scores.faithfulness == pytest.approx(1.0)


def test_heuristic_scorer_blocked_answer_is_not_relevant() -> None:
    scorer = HeuristicGenerationScorer()
    sample = _sample()
    result = ProbeResult(
        retrieved_chunk_ids=[], answer="", retrieved_contexts=[], is_confident=False
    )
    scores = scorer.score(sample, result)
    assert scores.answer_relevance == 0.0
    assert scores.context_recall == 0.0


# --------------------------------------------------------------------------- gate


async def test_gate_passes_with_oracle_probe() -> None:
    samples = load_golden_dataset()
    report = await evaluate(samples, _OracleProbe(), HeuristicGenerationScorer(), Thresholds())
    assert report.passed, report.format_report()
    assert report.sample_count == len(samples)
    assert not report.failures


async def test_gate_fails_on_degraded_retrieval() -> None:
    samples = load_golden_dataset()
    report = await evaluate(samples, _MissProbe(), HeuristicGenerationScorer(), Thresholds())
    assert not report.passed
    # Every threshold-bearing metric regressed; the report lists them.
    assert any("hit_rate@5" in f for f in report.failures)
    assert any("context_recall" in f for f in report.failures)


async def test_gate_is_reproducible_across_runs() -> None:
    samples = load_golden_dataset()
    first = await evaluate(samples, _OracleProbe(), HeuristicGenerationScorer(), seed=42)
    second = await evaluate(samples, _OracleProbe(), HeuristicGenerationScorer(), seed=42)
    assert first.faithfulness == second.faithfulness
    assert first.ir == second.ir


async def test_gate_rejects_empty_sample_set() -> None:
    with pytest.raises(ValueError):
        await evaluate([], _OracleProbe(), HeuristicGenerationScorer())


async def test_smoke_subset_is_small_and_passes() -> None:
    samples = load_golden_dataset()[:3]
    report = await evaluate(samples, _OracleProbe(), HeuristicGenerationScorer())
    assert report.sample_count == 3
    assert report.passed


# --------------------------------------------------------------------------- dataset


def test_golden_dataset_file_exists_and_loads() -> None:
    assert DEFAULT_DATASET_PATH.exists()
    samples = load_golden_dataset()
    assert len(samples) >= 5
    # access_level coverage across the clearance range (Public .. Secret).
    assert {s.access_level for s in samples} >= {0, 1}
    for s in samples:
        assert s.ground_truth_chunk_ids


def test_dataset_rejects_sample_without_qrels(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps([{"id": "x", "question": "q", "ground_truth_answer": "a"}]), encoding="utf-8"
    )
    with pytest.raises(ValueError):
        load_golden_dataset(bad)


def test_dataset_rejects_duplicate_ids(tmp_path: Path) -> None:
    dup = tmp_path / "dup.json"
    entry = {
        "id": "dup",
        "question": "q",
        "ground_truth_answer": "a",
        "ground_truth_chunk_ids": ["c1"],
    }
    dup.write_text(json.dumps([entry, entry]), encoding="utf-8")
    with pytest.raises(ValueError):
        load_golden_dataset(dup)


def test_dataset_rejects_empty_array(tmp_path: Path) -> None:
    empty = tmp_path / "empty.json"
    empty.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError):
        load_golden_dataset(empty)


# --------------------------------------------------------------------------- CLI


def test_cli_self_check_passes() -> None:
    assert main(["--self-check", "--no-ragas"]) == 0


def test_cli_smoke_self_check_passes() -> None:
    assert main(["--self-check", "--no-ragas", "--smoke"]) == 0


def test_cli_list_exits_zero() -> None:
    assert main(["--list"]) == 0


def test_cli_bad_dataset_returns_usage_error(tmp_path: Path) -> None:
    missing = tmp_path / "nope.json"
    assert main(["--dataset", str(missing), "--self-check"]) == 2


def test_cli_live_run_without_infra_returns_three() -> None:
    # No --tenant-id/--user-id (and typically no DSN) => the live wiring must fail cleanly.
    assert main(["--no-ragas"]) == 3
