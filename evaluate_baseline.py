"""Evaluate the Phase 7 empirical-frequency baseline against stored completed rounds."""

import argparse
from contextlib import closing
from decimal import Decimal, InvalidOperation
import sqlite3
import sys

from config.settings import DEVELOPMENT_PHASE, load_settings
from database.database import connect_database_readonly
from database.migrations import verify_schema
from database.models import identifier
from evaluation.backtest import walk_forward_evaluate
from models.baseline import BaselineConfig


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
        description="Chronologically evaluate the Phase 7 threshold-frequency baseline."
    )
    parser.add_argument("--source", required=True, help="Exact stored source identifier.")
    parser.add_argument("--threshold", action="append", type=_threshold, dest="thresholds")
    parser.add_argument("--min-history", type=int, default=50)
    parser.add_argument("--mode", choices=("expanding", "rolling"), default="expanding")
    parser.add_argument("--window", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        source = identifier(args.source, "source")
        thresholds = tuple(args.thresholds) if args.thresholds else (Decimal("1.5"), Decimal("2"), Decimal("5"))
        config = BaselineConfig(
            thresholds=thresholds,
            mode=args.mode,
            window=args.window,
            min_history=args.min_history,
        )
        settings = load_settings()
        with closing(connect_database_readonly(settings)) as connection:
            verify_schema(connection)
            rows = connection.execute(
                "SELECT multiplier FROM rounds WHERE source = ? "
                "ORDER BY timestamp ASC, source ASC, round_id ASC",
                (source,),
            ).fetchall()
        result = walk_forward_evaluate([row["multiplier"] for row in rows], config)
    except (OSError, ValueError, sqlite3.Error, RuntimeError):
        print("Baseline evaluation failed. Check the source, settings, database, and arguments.", file=sys.stderr)
        return 1

    print(
        "Aviator Intelligence Engine\n"
        f"Phase: {DEVELOPMENT_PHASE}\n"
        "Baseline: EVALUATED\n"
        f"Source: {source}\n"
        f"Stored rounds: {len(rows)}\n"
        f"Evaluated targets: {result.evaluated_targets}\n"
        f"Skipped targets: {result.skipped_targets}\n"
        f"Mode: {config.mode}\n"
        f"Minimum history: {config.min_history}"
    )
    for score in result.threshold_scores:
        brier = "N/A" if score.brier_score is None else str(score.brier_score)
        calibration = "N/A" if score.calibration_error is None else str(score.calibration_error)
        print(
            f"Threshold {score.threshold}x: predictions={score.predictions}, "
            f"positives={score.positives}, brier={brier}, calibration_error={calibration}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
