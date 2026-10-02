"""Run Phase 8 chronological logistic-regression experiments on stored rounds."""

import argparse
from contextlib import closing
from decimal import Decimal, InvalidOperation
import sqlite3
import sys

from config.settings import DEVELOPMENT_PHASE, load_settings
from database.database import connect_database_readonly
from database.migrations import verify_schema
from database.models import identifier
from evaluation.ml_experiment import SplitConfig, run_logistic_experiment
from evaluation.ml_reporting import write_experiment_report
from prediction.features import FeatureConfig


def _threshold(value: str) -> Decimal:
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise argparse.ArgumentTypeError("thresholds must be decimal values.") from None
    if not result.is_finite() or result < 1:
        raise argparse.ArgumentTypeError("thresholds must be finite and at least 1.")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run leakage-safe Phase 8 chronological ML experiments."
    )
    parser.add_argument("--source", required=True, help="Exact stored source identifier.")
    parser.add_argument("--threshold", action="append", type=_threshold, dest="thresholds")
    parser.add_argument("--lag-count", type=int, default=5)
    parser.add_argument("--rolling-window", type=int, default=20)
    parser.add_argument("--validation-fraction", type=float, default=0.2)
    parser.add_argument("--test-fraction", type=float, default=0.2)
    parser.add_argument("--output", help="Optional plain JSON filename under data/processed.")
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.overwrite and not args.output:
        parser.error("--overwrite requires --output.")
    try:
        source = identifier(args.source, "source")
        thresholds = tuple(args.thresholds) if args.thresholds else (
            Decimal("1.5"), Decimal("2"), Decimal("5")
        )
        feature_config = FeatureConfig(
            lag_count=args.lag_count,
            rolling_window=args.rolling_window,
        )
        split_config = SplitConfig(
            validation_fraction=args.validation_fraction,
            test_fraction=args.test_fraction,
        )
        settings = load_settings()
        with closing(connect_database_readonly(settings)) as connection:
            verify_schema(connection)
            rows = connection.execute(
                "SELECT multiplier FROM rounds WHERE source = ? "
                "ORDER BY timestamp ASC, source ASC, round_id ASC",
                (source,),
            ).fetchall()
        report = run_logistic_experiment(
            [row["multiplier"] for row in rows],
            thresholds=thresholds,
            feature_config=feature_config,
            split_config=split_config,
        )
        output_path = (
            write_experiment_report(report, settings, args.output, overwrite=args.overwrite)
            if args.output else None
        )
    except (OSError, ValueError, sqlite3.Error, RuntimeError):
        print(
            "ML experiment failed. Check the source, settings, database, sample size, and arguments.",
            file=sys.stderr,
        )
        return 1

    print(
        "Aviator Intelligence Engine\n"
        f"Phase: {DEVELOPMENT_PHASE}\n"
        "ML experiment: COMPLETE\n"
        f"Source: {source}\n"
        f"Stored rounds: {len(rows)}\n"
        f"Dataset fingerprint: {report.dataset_fingerprint}\n"
        f"Features: {len(report.feature_names)}"
    )
    for experiment in report.experiments:
        test_brier = "N/A" if experiment.test_brier is None else f"{experiment.test_brier:.6f}"
        baseline_brier = (
            "N/A" if experiment.baseline_test_brier is None
            else f"{experiment.baseline_test_brier:.6f}"
        )
        print(
            f"Threshold {experiment.threshold}x: status={experiment.status}, "
            f"test_rows={experiment.test_rows}, test_brier={test_brier}, "
            f"baseline_test_brier={baseline_brier}"
        )
    print(f"Report: {output_path if output_path is not None else 'NOT SAVED'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
