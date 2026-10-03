"""Evaluate adaptive next-round point predictions on stored completed rounds."""

from __future__ import annotations

import argparse
from contextlib import closing
import sqlite3
import sys

from collectors.dom_round_ingest import DOM_OBSERVATION_SOURCE
from config.settings import DEVELOPMENT_PHASE, load_settings
from database.database import connect_database_readonly
from database.migrations import verify_schema
from evaluation.adaptive_backtest import walk_forward_point_evaluate
from prediction.adaptive import AdaptiveConfig


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Walk-forward evaluation of adaptive next-round point predictions."
    )
    parser.add_argument("--source", default=DOM_OBSERVATION_SOURCE)
    parser.add_argument("--min-history", type=int, default=20)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = AdaptiveConfig(min_history=args.min_history)
        settings = load_settings()
        with closing(connect_database_readonly(settings)) as connection:
            verify_schema(connection)
            rows = connection.execute(
                "SELECT multiplier FROM rounds WHERE source = ? "
                "ORDER BY timestamp ASC, round_id ASC",
                (args.source,),
            ).fetchall()
        report = walk_forward_point_evaluate(
            [row["multiplier"] for row in rows],
            config,
        )
    except (OSError, ValueError, sqlite3.Error, RuntimeError):
        print(
            "Point-model evaluation failed. Check the database, source, and arguments.",
            file=sys.stderr,
        )
        return 1

    print(
        "Aviator Intelligence Engine\n"
        f"Phase: {DEVELOPMENT_PHASE}\n"
        f"Point model evaluation: {report.status}\n"
        f"Source: {args.source}\n"
        f"Stored rounds: {report.stored_rounds}\n"
        f"Evaluated targets: {report.evaluated_targets}\n"
        f"Skipped targets: {report.skipped_targets}\n"
        f"Model MAE: {report.mae if report.mae is not None else 'N/A'}x\n"
        f"Model median absolute error: "
        f"{report.median_absolute_error if report.median_absolute_error is not None else 'N/A'}x\n"
        f"Recent-median baseline MAE: "
        f"{report.recent_median_mae if report.recent_median_mae is not None else 'N/A'}x\n"
        f"Within 0.5x: {report.within_half_x_rate if report.within_half_x_rate is not None else 'N/A'}\n"
        f"Within 1.0x: {report.within_one_x_rate if report.within_one_x_rate is not None else 'N/A'}\n"
        f"Interval coverage: {report.interval_coverage if report.interval_coverage is not None else 'N/A'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
