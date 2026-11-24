"""Stage O SLO thresholds and the baseline regression contract (``SPECIFICATION.md`` §1.2).

Two independent checks make up the gate:

* **Absolute SLO** - the hard NFR targets from the specification: P95 retrieval < 400 ms,
  P95 TTFT < 1.5 s, a minimum sustained ingest throughput, and a cap on the error rate.
* **Baseline regression** - a committed ``load_slo_baseline.json`` fixes the last accepted
  numbers; a nightly run that drifts worse than ``regression_tolerance`` (default +15 %) fails even
  if it still formally meets the absolute SLO, so gradual latency creep is caught early.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# Canonical locations alongside the Stage J golden dataset.
DEFAULT_BASELINE_PATH = Path(__file__).resolve().parents[1] / "data" / "load_slo_baseline.json"

# Scenario names (also the keys used in the baseline file).
SCENARIO_RETRIEVAL = "retrieval"
SCENARIO_TTFT = "generation_ttft"
SCENARIO_INGEST = "ingestion"


@dataclass(frozen=True, slots=True)
class SloThresholds:
    """The absolute NFR targets from ``SPECIFICATION.md`` §1.2."""

    retrieval_p95_ms: float = 400.0
    ttft_p95_ms: float = 1500.0
    ingest_min_throughput_rps: float = 1.0
    max_error_rate: float = 0.01
    # Allowed relative drift over the committed baseline before a regression is flagged.
    regression_tolerance: float = 0.15

    def as_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Baseline:
    """A committed snapshot of per-scenario p95 (ms) and throughput (rps)."""

    p95_ms: dict[str, float]
    throughput_rps: dict[str, float]

    def as_dict(self) -> dict[str, dict[str, float]]:
        return {"p95_ms": self.p95_ms, "throughput_rps": self.throughput_rps}


def load_baseline(path: Path = DEFAULT_BASELINE_PATH) -> Baseline:
    """Loads the committed baseline; raises ``ValueError`` on a malformed file."""
    if not path.exists():
        raise ValueError(f"baseline file not found: {path}")
    try:
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        raise ValueError(f"malformed baseline JSON: {exc}") from exc
    if not isinstance(raw.get("p95_ms"), dict) or not isinstance(raw.get("throughput_rps"), dict):
        raise ValueError("baseline must contain 'p95_ms' and 'throughput_rps' objects")
    return Baseline(
        p95_ms={str(k): float(v) for k, v in raw["p95_ms"].items()},
        throughput_rps={str(k): float(v) for k, v in raw["throughput_rps"].items()},
    )
