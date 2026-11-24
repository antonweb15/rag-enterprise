"""CLI entry point for the Stage O load/SLO gate: ``python -m tests.load_slo``.

Exit codes (so CI can branch on them, mirroring the Stage J gate):

- ``0`` - the gate passed (every SLO + regression check met);
- ``1`` - the gate ran but an SLO/baseline check regressed (BLOCK the nightly/release);
- ``2`` - a usage/configuration error (bad arguments, malformed baseline);
- ``3`` - the live target could not be driven (API/deps not reachable for a real run).

By default the gate runs ``--self-check`` (fully offline synthetic load) so the plumbing is
validated anywhere. ``--target http://host:port`` drives a live API over HTTP (needs the ``load``
extra). ``--update-baseline`` rewrites the committed baseline from the current run (used manually
to re-baseline after an intentional, reviewed performance change).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from tests.load_slo.gate import LoadReport, evaluate, run_scenario
from tests.load_slo.scenarios import (
    LoadProfile,
    build_http_scenarios,
    build_self_check_scenarios,
)
from tests.load_slo.slo import (
    DEFAULT_BASELINE_PATH,
    Baseline,
    SloThresholds,
    load_baseline,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m tests.load_slo", description=__doc__)
    parser.add_argument(
        "--target", default=None, help="base URL of a live API to load-test over HTTP"
    )
    parser.add_argument("--self-check", action="store_true", help="run the offline synthetic load")
    parser.add_argument("--tenant-id", default="00000000-0000-0000-0000-000000000000")
    parser.add_argument("--auth-token", default=None)
    parser.add_argument("--iterations", type=int, default=None)
    parser.add_argument("--concurrency", type=int, default=None)
    parser.add_argument(
        "--baseline",
        type=Path,
        default=DEFAULT_BASELINE_PATH,
        help="committed baseline JSON for regression detection",
    )
    parser.add_argument(
        "--no-baseline", action="store_true", help="skip the baseline regression check"
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="rewrite the baseline file from this run instead of checking it",
    )
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    return parser


def _write_baseline(report: LoadReport, path: Path) -> None:
    payload = {
        "p95_ms": {name: round(s.p95_ms, 3) for name, s in report.summaries.items()},
        "throughput_rps": {
            name: round(s.throughput_rps, 4) for name, s in report.summaries.items()
        },
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _print_human(report: LoadReport) -> None:
    for name, summary in report.summaries.items():
        print(
            f"  [{name}] n={summary.count} err={summary.errors} "
            f"p50={summary.p50_ms:.1f}ms p95={summary.p95_ms:.1f}ms p99={summary.p99_ms:.1f}ms "
            f"rps={summary.throughput_rps:.2f}"
        )
    for check in report.checks:
        mark = "PASS" if check.passed else "FAIL"
        print(
            f"  [{mark}] {check.kind}:{check.name} "
            f"observed={check.observed:.3f} limit={check.limit:.3f}"
        )
    print(f"\nLOAD/SLO GATE: {'PASSED' if report.passed else 'FAILED'}")


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    profile: LoadProfile | None = None
    if args.iterations is not None or args.concurrency is not None:
        base = LoadProfile()
        profile = LoadProfile(
            iterations=args.iterations or base.iterations,
            concurrency=args.concurrency or base.concurrency,
        )

    # Choose the load scenarios.
    if args.target and not args.self_check:
        try:
            scenarios = build_http_scenarios(
                args.target,
                tenant_id=args.tenant_id,
                auth_token=args.auth_token,
                profile=profile,
            )
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 3
    else:
        scenarios = build_self_check_scenarios(profile)

    async def _run() -> LoadReport:
        summaries = {}
        for scenario in scenarios:
            summaries[scenario.name] = await run_scenario(scenario)
        baseline: Baseline | None = None
        if not args.no_baseline and not args.update_baseline:
            try:
                baseline = load_baseline(args.baseline)
            except ValueError as exc:
                raise _UsageError(str(exc)) from exc
        return evaluate(summaries, thresholds=SloThresholds(), baseline=baseline)

    try:
        report = asyncio.run(_run())
    except _UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - surface infra failures as exit code 3
        print(f"error: could not drive the load target: {exc}", file=sys.stderr)
        return 3

    if args.update_baseline:
        _write_baseline(report, args.baseline)
        print(f"baseline updated: {args.baseline}")
        return 0

    if args.json:
        print(json.dumps(report.as_dict(), indent=2, ensure_ascii=False))
    else:
        _print_human(report)
    return 0 if report.passed else 1


class _UsageError(Exception):
    """Internal marker to translate a usage/config problem into exit code 2."""


if __name__ == "__main__":
    raise SystemExit(main())
