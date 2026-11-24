"""Stage O - Load testing & SLO verification (IMPLEMENTATION_PLAN.md §O).

This package is the runnable performance gate for the NFR targets of ``SPECIFICATION.md`` §1.2:

- **P95 Retrieval Latency < 400 ms** (dense + sparse + RRF + FlashRank at ``ef_search = 40``);
- **P95 Time To First Token < 1.5 s** (streaming to Telegram/SSE);
- a minimum sustained **ingestion throughput** (docs/sec).

Mirroring the Stage J evaluation gate, the core is pure and injection-friendly:

- :mod:`tests.load_slo.metrics` - exact, numpy-free latency percentiles & throughput;
- :mod:`tests.load_slo.slo` - the absolute SLO thresholds and the committed baseline contract;
- :mod:`tests.load_slo.gate` - the concurrent closed-loop load driver and the pass/fail decision;
- :mod:`tests.load_slo.scenarios` - the workloads (a dependency-free self-check and a lazy HTTP
  driver for a real nightly run against a live API).

The runnable gate is ``python -m tests.load_slo`` (see :mod:`tests.load_slo.__main__`); the real
HTTP load profile for a nightly/release run lives in ``scripts/locustfile.py``. The gate is wired
as a *nightly / release* job (not a per-PR check) - see ``.github/workflows/load.yml``.
"""

from tests.load_slo.gate import (
    LoadReport,
    LoadScenario,
    MetricCheck,
    SimpleScenario,
    Workload,
    evaluate,
    run_scenario,
)
from tests.load_slo.metrics import LatencySummary, percentile, summarize
from tests.load_slo.slo import (
    DEFAULT_BASELINE_PATH,
    SCENARIO_INGEST,
    SCENARIO_RETRIEVAL,
    SCENARIO_TTFT,
    Baseline,
    SloThresholds,
    load_baseline,
)

__all__ = [
    "DEFAULT_BASELINE_PATH",
    "SCENARIO_INGEST",
    "SCENARIO_RETRIEVAL",
    "SCENARIO_TTFT",
    "Baseline",
    "LatencySummary",
    "LoadReport",
    "LoadScenario",
    "MetricCheck",
    "SimpleScenario",
    "SloThresholds",
    "Workload",
    "evaluate",
    "load_baseline",
    "percentile",
    "run_scenario",
    "summarize",
]
