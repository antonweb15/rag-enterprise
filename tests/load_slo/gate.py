"""The Stage O load driver and the pass/fail gate.

:func:`run_scenario` is a small concurrent closed-loop load generator: it drives a
:class:`Workload` (an async callable timed by the gate itself) with a fixed concurrency for a
target number of iterations, collecting per-request latencies and counting failures. The driver is
pure and infra-free - the *workload* decides what a "request" is (an HTTP call in a nightly run, or
an in-process probe / a synthetic sleep in a self-check), which keeps the gate unit-testable.

:func:`evaluate` turns the collected :class:`~tests.load_slo.metrics.LatencySummary` objects into a
:class:`LoadReport`, checking both the absolute SLO and the regression over the committed baseline.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from tests.load_slo.metrics import LatencySummary, summarize
from tests.load_slo.slo import (
    SCENARIO_INGEST,
    SCENARIO_RETRIEVAL,
    SCENARIO_TTFT,
    Baseline,
    SloThresholds,
)

# A workload performs one unit of work and either returns (success) or raises (failure).
Workload = Callable[[], Awaitable[None]]


class LoadScenario(Protocol):
    """A named load scenario: its workload plus how many iterations / concurrency to drive."""

    name: str
    iterations: int
    concurrency: int

    async def __call__(self) -> None: ...


@dataclass(slots=True)
class SimpleScenario:
    """A concrete :class:`LoadScenario` wrapping a plain :class:`Workload`."""

    name: str
    workload: Workload
    iterations: int = 100
    concurrency: int = 8

    async def __call__(self) -> None:
        await self.workload()


async def run_scenario(scenario: LoadScenario) -> LatencySummary:
    """Drives ``scenario`` with a bounded concurrency and returns its latency summary."""
    latencies_ms: list[float] = []
    errors = 0
    semaphore = asyncio.Semaphore(max(1, scenario.concurrency))

    async def one() -> None:
        nonlocal errors
        async with semaphore:
            start = time.perf_counter()
            try:
                await scenario()
            except Exception:  # noqa: BLE001 - a failed request is a load error, not a crash
                errors += 1
                return
            latencies_ms.append((time.perf_counter() - start) * 1000.0)

    wall_start = time.perf_counter()
    await asyncio.gather(*(one() for _ in range(max(1, scenario.iterations))))
    duration_s = time.perf_counter() - wall_start
    return summarize(scenario.name, latencies_ms, errors=errors, duration_s=duration_s)


@dataclass(frozen=True, slots=True)
class MetricCheck:
    """A single pass/fail line in the report."""

    name: str
    observed: float
    limit: float
    passed: bool
    kind: str  # "slo" | "regression"


@dataclass(slots=True)
class LoadReport:
    """The aggregate verdict of a load run."""

    summaries: dict[str, LatencySummary]
    checks: list[MetricCheck] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def as_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "summaries": {name: s.as_dict() for name, s in self.summaries.items()},
            "checks": [
                {
                    "name": c.name,
                    "observed": round(c.observed, 3),
                    "limit": round(c.limit, 3),
                    "passed": c.passed,
                    "kind": c.kind,
                }
                for c in self.checks
            ],
        }


def evaluate(
    summaries: Mapping[str, LatencySummary],
    *,
    thresholds: SloThresholds | None = None,
    baseline: Baseline | None = None,
) -> LoadReport:
    """Builds a :class:`LoadReport` from scenario summaries against SLO (+ optional baseline)."""
    thresholds = thresholds or SloThresholds()
    checks: list[MetricCheck] = []

    def add(name: str, observed: float, limit: float, *, ok: bool, kind: str) -> None:
        checks.append(MetricCheck(name=name, observed=observed, limit=limit, passed=ok, kind=kind))

    # --- absolute SLO (SPECIFICATION.md §1.2) ---
    if (ret := summaries.get(SCENARIO_RETRIEVAL)) is not None:
        add(
            "retrieval.p95_ms",
            ret.p95_ms,
            thresholds.retrieval_p95_ms,
            ok=ret.p95_ms < thresholds.retrieval_p95_ms,
            kind="slo",
        )
    if (ttft := summaries.get(SCENARIO_TTFT)) is not None:
        add(
            "generation_ttft.p95_ms",
            ttft.p95_ms,
            thresholds.ttft_p95_ms,
            ok=ttft.p95_ms < thresholds.ttft_p95_ms,
            kind="slo",
        )
    if (ing := summaries.get(SCENARIO_INGEST)) is not None:
        add(
            "ingestion.throughput_rps",
            ing.throughput_rps,
            thresholds.ingest_min_throughput_rps,
            ok=ing.throughput_rps >= thresholds.ingest_min_throughput_rps,
            kind="slo",
        )

    # The error-rate cap applies to every scenario.
    for name, summary in summaries.items():
        add(
            f"{name}.error_rate",
            summary.error_rate,
            thresholds.max_error_rate,
            ok=summary.error_rate <= thresholds.max_error_rate,
            kind="slo",
        )

    # --- regression over the committed baseline ---
    if baseline is not None:
        tol = thresholds.regression_tolerance
        for name, summary in summaries.items():
            if (base_p95 := baseline.p95_ms.get(name)) is not None and base_p95 > 0:
                limit = base_p95 * (1.0 + tol)
                add(
                    f"{name}.p95_ms.regression",
                    summary.p95_ms,
                    limit,
                    ok=summary.p95_ms <= limit,
                    kind="regression",
                )
            if (base_rps := baseline.throughput_rps.get(name)) is not None and base_rps > 0:
                limit = base_rps * (1.0 - tol)
                add(
                    f"{name}.throughput_rps.regression",
                    summary.throughput_rps,
                    limit,
                    ok=summary.throughput_rps >= limit,
                    kind="regression",
                )

    return LoadReport(summaries=dict(summaries), checks=checks)
