"""Deterministic latency / throughput statistics for the Stage O load gate.

These helpers are pure and dependency-free (no numpy): given a list of per-request latency
samples (in milliseconds) they compute the percentiles mandated by ``SPECIFICATION.md`` §1.2
(P95 Retrieval Latency, P95 TTFT) plus p50/p99 for context, and derive the sustained throughput
(requests/second) from the samples and the wall-clock duration of the run. Keeping this exact and
seed-free makes the gate's verdict reproducible and unit-testable without any live infrastructure.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


def percentile(samples: Sequence[float], pct: float) -> float:
    """Returns the ``pct`` (0..100) percentile using linear interpolation between ranks.

    Matches the common "exclusive-free" definition used by k6/locust closely enough for an SLO
    gate (identical on the exact ranks, interpolates between them). Empty input yields ``0.0``.
    """
    if not samples:
        return 0.0
    if pct <= 0:
        return float(min(samples))
    if pct >= 100:
        return float(max(samples))
    ordered = sorted(samples)
    rank = (pct / 100.0) * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    frac = rank - low
    return float(ordered[low] + (ordered[high] - ordered[low]) * frac)


@dataclass(frozen=True, slots=True)
class LatencySummary:
    """Aggregate latency/throughput statistics for one load scenario."""

    name: str
    count: int
    errors: int
    duration_s: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    throughput_rps: float

    @property
    def error_rate(self) -> float:
        total = self.count + self.errors
        return self.errors / total if total else 0.0

    def as_dict(self) -> dict[str, float | int | str]:
        return {
            "name": self.name,
            "count": self.count,
            "errors": self.errors,
            "duration_s": round(self.duration_s, 4),
            "p50_ms": round(self.p50_ms, 3),
            "p95_ms": round(self.p95_ms, 3),
            "p99_ms": round(self.p99_ms, 3),
            "max_ms": round(self.max_ms, 3),
            "throughput_rps": round(self.throughput_rps, 4),
            "error_rate": round(self.error_rate, 4),
        }


def summarize(
    name: str, latencies_ms: Sequence[float], *, errors: int, duration_s: float
) -> LatencySummary:
    """Builds a :class:`LatencySummary` from raw per-request latency samples."""
    count = len(latencies_ms)
    throughput = (count / duration_s) if duration_s > 0 else 0.0
    return LatencySummary(
        name=name,
        count=count,
        errors=errors,
        duration_s=duration_s,
        p50_ms=percentile(latencies_ms, 50),
        p95_ms=percentile(latencies_ms, 95),
        p99_ms=percentile(latencies_ms, 99),
        max_ms=float(max(latencies_ms)) if latencies_ms else 0.0,
        throughput_rps=throughput,
    )
