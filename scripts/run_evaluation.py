#!/usr/bin/env python
"""Run the agent evaluation suites from the command line.

Used two ways:

* locally, to see how a change affected agent behaviour;
* in CI, as a **gate**: `--fail-under 0.9` makes the build fail when the pass
  rate drops. Behaviour regressions - the wrong tool chosen, a fabricated
  citation, a sensitive action that stopped pausing for approval - never show
  up in unit tests, and this is what catches them.

Judge scores are advisory and are excluded from the gate with `--no-judge`
in CI, because a language model's scores are not stable enough to block a
build on.

Examples:
    python -m scripts.run_evaluation --suite core
    python -m scripts.run_evaluation --all --no-judge --fail-under 0.9
    python -m scripts.run_evaluation --case breast-cancer-biomarkers-evidence
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.logging import configure_logging  # noqa: E402
from app.database.session import session_scope  # noqa: E402
from app.evaluation.runner import EvaluationRunner  # noqa: E402
from app.models.evaluation import EvaluationCase, EvaluationResult  # noqa: E402
from app.schemas.evaluation import SuiteSummary  # noqa: E402

DEFAULT_REPORT = ROOT / "evaluation-report.json"


async def discover_suites() -> list[str]:
    async with session_scope() as session:
        rows = (
            (
                await session.execute(
                    select(EvaluationCase.suite).where(EvaluationCase.enabled.is_(True)).distinct()
                )
            )
            .scalars()
            .all()
        )
    return sorted(rows)


async def failures_for(run_label: str) -> list[dict]:
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(EvaluationResult, EvaluationCase)
                .join(EvaluationCase, EvaluationCase.id == EvaluationResult.case_id)
                .where(EvaluationResult.run_label == run_label)
            )
        ).all()
    return [
        {
            "case": case.slug,
            "suite": case.suite,
            "passed": result.passed,
            "score": result.overall_score,
            "failures": result.failures,
            "tools_used": result.tools_used,
            "latency_ms": result.latency_ms,
            "cost_usd": result.cost_usd,
        }
        for result, case in rows
    ]


async def main_async(args: argparse.Namespace) -> tuple[int, dict]:
    settings = get_settings()
    configure_logging("WARNING" if args.quiet else settings.log_level, "console")

    label = args.label or dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    suites = await discover_suites() if args.all else [args.suite]
    runner = EvaluationRunner()

    summaries: list[SuiteSummary] = []
    case_rows: list[dict] = []

    provider = "openai" if settings.use_real_openai else "deterministic-offline"
    print(f"\nEvaluation run {label}  (LLM provider: {provider})")
    print("=" * 78)

    for suite in suites:
        suite_label = f"{label}:{suite}"
        summary = await runner.run_suite(
            suite=suite,
            run_label=suite_label,
            use_judge=not args.no_judge,
            case_slugs=args.case or None,
        )
        summaries.append(summary)
        case_rows.extend(await failures_for(suite_label))

        status = "PASS" if summary.failed == 0 else "FAIL"
        print(
            f"{status:4s}  {suite:<12s} {summary.passed}/{summary.total} "
            f"({summary.pass_rate:.0%})  mean score {summary.mean_score:.3f}  "
            f"mean latency {summary.mean_latency_ms:.0f}ms  "
            f"cost ${summary.total_cost_usd:.4f}"
        )

    total = sum(s.total for s in summaries)
    passed = sum(s.passed for s in summaries)
    pass_rate = passed / total if total else 0.0

    failed_cases = [row for row in case_rows if not row["passed"]]
    if failed_cases:
        print("\nFailures")
        print("-" * 78)
        for row in failed_cases:
            print(f"  {row['case']} ({row['suite']})  score {row['score']:.3f}")
            for failure in row["failures"]:
                print(f"      - {failure}")

    print("=" * 78)
    print(
        f"TOTAL {passed}/{total} ({pass_rate:.0%})  "
        f"cost ${sum(s.total_cost_usd for s in summaries):.4f}"
    )

    report = {
        "run_label": label,
        "provider": provider,
        "total": total,
        "passed": passed,
        "pass_rate": round(pass_rate, 4),
        "suites": [s.model_dump(mode="json") for s in summaries],
        "cases": case_rows,
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
    }
    if args.fail_under is not None and pass_rate < args.fail_under:
        print(f"\nFAILED: pass rate {pass_rate:.2%} is below the required {args.fail_under:.2%}")
        return 1, report
    return 0, report


def write_report(path_str: str, report: dict) -> None:
    """Persist the JSON report.

    Synchronous and outside the event loop: it is one blocking write, and the
    report is a convenience rather than the gate - a read-only working
    directory must not turn a passing suite into a failed build.
    """
    path = Path(path_str)
    try:
        path.write_text(json.dumps(report, indent=2, default=str))
        print(f"Report written to {path}")
    except OSError as exc:
        print(f"WARNING: could not write the report to {path}: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run agent evaluation suites")
    parser.add_argument("--suite", default="core", help="Suite to run (default: core)")
    parser.add_argument("--all", action="store_true", help="Run every enabled suite")
    parser.add_argument("--case", action="append", help="Run only these case slugs")
    parser.add_argument("--label", help="Run label (default: the current UTC timestamp)")
    parser.add_argument(
        "--no-judge",
        action="store_true",
        help="Skip LLM-as-a-judge; deterministic checks only (what CI uses)",
    )
    parser.add_argument(
        "--fail-under",
        type=float,
        help="Exit non-zero when the overall pass rate is below this (0-1)",
    )
    parser.add_argument(
        "--report",
        default=str(DEFAULT_REPORT),
        help="Where to write the JSON report (default: ./evaluation-report.json)",
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress info logging")
    args = parser.parse_args()

    exit_code, report = asyncio.run(main_async(args))
    write_report(args.report, report)
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
