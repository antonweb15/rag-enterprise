"""Stage O - Load testing & SLO verification test suite (IMPLEMENTATION_PLAN.md §O).

These tests are the regression harness for the gate itself: they verify the exact latency
percentiles / throughput, the absolute SLO thresholds (SPECIFICATION.md §1.2: P95 retrieval
< 400 ms, P95 TTFT < 1.5 s, ingest throughput), the baseline regression contract, the closed-loop
load driver (including error accounting) and the CLI exit codes. Everything runs offline - no live
Postgres/LLM/HTTP - because the driver times an injected async workload.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tests.load_slo import (
    SCENARIO_INGEST,
    SCENARIO_RETRIEVAL,
    SCENARIO_TTFT,
    Baseline,
    LatencySummary,
    SimpleScenario,
    SloThresholds,
    evaluate,
    load_baseline,
    percentile,
    run_scenario,
    summarize,
)
from tests.load_slo.__main__ import main
from tests.load_slo.scenarios import LoadProfile, build_self_check_scenarios
from tests.load_slo.slo import DEFAULT_BASELINE_PATH

# --------------------------------------------------------------------------- metrics


def test_percentile_exact_and_interpolated() -> None:
    samples = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert percentile(samples, 0) == 10.0
    assert percentile(samples, 100) == 50.0
    assert percentile(samples, 50) == 30.0
    # Between ranks: p95 of 5 points interpolates between 40 and 50.
    assert 40.0 < percentile(samples, 95) <= 50.0
    assert percentile([], 95) == 0.0


def test_summarize_throughput_and_error_rate() -> None:
    summary = summarize(SCENARIO_RETRIEVAL, [100.0] * 10, errors=0, duration_s=2.0)
    assert summary.count == 10
    assert summary.throughput_rps == pytest.approx(5.0)
    assert summary.p95_ms == pytest.approx(100.0)
    assert summary.error_rate == 0.0
    with_errors = summarize(SCENARIO_RETRIEVAL, [1.0] * 9, errors=1, duration_s=1.0)
    assert with_errors.error_rate == pytest.approx(0.1)


# --------------------------------------------------------------------------- SLO gate


def _summaries(ret_p95: float, ttft_p95: float, ingest_rps: float) -> dict[str, LatencySummary]:
    return {
        SCENARIO_RETRIEVAL: summarize(SCENARIO_RETRIEVAL, [ret_p95] * 50, errors=0, duration_s=1.0),
        SCENARIO_TTFT: summarize(SCENARIO_TTFT, [ttft_p95] * 50, errors=0, duration_s=1.0),
        SCENARIO_INGEST: summarize(
            SCENARIO_INGEST, [10.0] * int(ingest_rps), errors=0, duration_s=1.0
        ),
    }


def test_gate_passes_within_slo() -> None:
    report = evaluate(_summaries(180.0, 900.0, 10.0))
    assert report.passed
    assert all(c.kind == "slo" for c in report.checks)


def test_gate_fails_on_retrieval_slo_breach() -> None:
    report = evaluate(_summaries(450.0, 900.0, 10.0))  # retrieval p95 >= 400 ms
    assert not report.passed
    breached = [c for c in report.checks if c.name == "retrieval.p95_ms"]
    assert breached and not breached[0].passed


def test_gate_fails_on_ttft_slo_breach() -> None:
    report = evaluate(_summaries(180.0, 1600.0, 10.0))  # TTFT p95 >= 1.5 s
    assert not report.passed
    assert any(c.name == "generation_ttft.p95_ms" and not c.passed for c in report.checks)


def test_gate_fails_on_low_ingest_throughput() -> None:
    thresholds = SloThresholds(ingest_min_throughput_rps=5.0)
    summaries: dict[str, LatencySummary] = {
        SCENARIO_INGEST: summarize(SCENARIO_INGEST, [10.0] * 2, errors=0, duration_s=1.0),
    }
    report = evaluate(summaries, thresholds=thresholds)
    assert not report.passed
    assert any(c.name == "ingestion.throughput_rps" and not c.passed for c in report.checks)


def test_gate_fails_on_error_rate() -> None:
    summaries: dict[str, LatencySummary] = {
        SCENARIO_RETRIEVAL: summarize(SCENARIO_RETRIEVAL, [100.0] * 90, errors=10, duration_s=1.0),
    }
    report = evaluate(summaries)
    assert not report.passed
    assert any(c.name == "retrieval.error_rate" and not c.passed for c in report.checks)


# --------------------------------------------------------------------------- baseline regression


def test_baseline_regression_flags_drift() -> None:
    baseline = Baseline(p95_ms={SCENARIO_RETRIEVAL: 100.0}, throughput_rps={})
    # 150 ms is +50 % over the 100 ms baseline (> 15 % tolerance) yet still under the 400 ms SLO.
    summaries: dict[str, LatencySummary] = {
        SCENARIO_RETRIEVAL: summarize(SCENARIO_RETRIEVAL, [150.0] * 50, errors=0, duration_s=1.0),
    }
    report = evaluate(summaries, baseline=baseline)
    assert not report.passed
    reg = [c for c in report.checks if c.name == "retrieval.p95_ms.regression"]
    assert reg and not reg[0].passed and reg[0].kind == "regression"


def test_baseline_regression_within_tolerance_passes() -> None:
    baseline = Baseline(p95_ms={SCENARIO_RETRIEVAL: 100.0}, throughput_rps={})
    summaries: dict[str, LatencySummary] = {
        SCENARIO_RETRIEVAL: summarize(SCENARIO_RETRIEVAL, [110.0] * 50, errors=0, duration_s=1.0),
    }
    report = evaluate(summaries, baseline=baseline)
    assert report.passed


def test_committed_baseline_loads_and_is_under_slo() -> None:
    baseline = load_baseline(DEFAULT_BASELINE_PATH)
    thresholds = SloThresholds()
    assert baseline.p95_ms[SCENARIO_RETRIEVAL] < thresholds.retrieval_p95_ms
    assert baseline.p95_ms[SCENARIO_TTFT] < thresholds.ttft_p95_ms


def test_load_baseline_rejects_malformed(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"p95_ms": 1}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_baseline(bad)


# --------------------------------------------------------------------------- driver


def test_run_scenario_counts_errors() -> None:
    state = {"n": 0}

    async def flaky() -> None:
        state["n"] += 1
        if state["n"] % 2 == 0:
            raise RuntimeError("boom")

    summary = asyncio.run(run_scenario(SimpleScenario("x", flaky, iterations=10, concurrency=1)))
    assert summary.errors == 5
    assert summary.count == 5


# --------------------------------------------------------------------------- scenarios / CLI


def test_self_check_scenarios_pass_against_committed_baseline() -> None:
    scenarios = build_self_check_scenarios(LoadProfile.smoke())

    async def _run() -> None:
        summaries = {s.name: await run_scenario(s) for s in scenarios}
        report = evaluate(summaries, baseline=load_baseline(DEFAULT_BASELINE_PATH))
        assert report.passed, report.as_dict()

    asyncio.run(_run())


def test_cli_self_check_exit_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--self-check"]) == 0
    assert "LOAD/SLO GATE: PASSED" in capsys.readouterr().out


def test_cli_json_output(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--self-check", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["passed"] is True
    assert SCENARIO_RETRIEVAL in payload["summaries"]


def test_cli_malformed_baseline_exit_two(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert main(["--self-check", "--baseline", str(bad)]) == 2


def test_cli_update_baseline_writes_file(tmp_path: Path) -> None:
    out = tmp_path / "baseline.json"
    assert main(["--self-check", "--update-baseline", "--baseline", str(out)]) == 0
    written = json.loads(out.read_text(encoding="utf-8"))
    assert SCENARIO_RETRIEVAL in written["p95_ms"]
    assert SCENARIO_RETRIEVAL in written["throughput_rps"]
