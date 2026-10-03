"""Evaluate pre-round system-state features against outcome-history baselines."""

from __future__ import annotations

import argparse
from contextlib import closing
from decimal import Decimal
import sqlite3
import sys

from collectors.dom_round_ingest import DOM_OBSERVATION_SOURCE
from config.settings import DEVELOPMENT_PHASE, load_settings
from database.database import connect_database_readonly
from database.migrations import verify_schema
from evaluation.system_state_backtest import walk_forward_system_state_evaluate
from prediction.system_state import (
    ObservedRound,
    build_system_state_samples,
    discover_network_probe_paths,
    load_system_probe_events,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Test whether sanitized network state available before a target round "
            "adds predictive information beyond completed-outcome history."
        )
    )
    parser.add_argument("--source", default=DOM_OBSERVATION_SOURCE)
    parser.add_argument("--min-training-samples", type=int, default=40)
    parser.add_argument("--validation-rows", type=int, default=12)
    parser.add_argument("--min-window-events", type=int, default=3)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings()
        probe_paths = discover_network_probe_paths(settings.raw_data_dir)
        events = load_system_probe_events(probe_paths)

        with closing(connect_database_readonly(settings)) as connection:
            verify_schema(connection)
            rows = connection.execute(
                "SELECT timestamp, multiplier FROM rounds WHERE source = ? "
                "ORDER BY timestamp ASC, round_id ASC",
                (args.source,),
            ).fetchall()

        rounds = tuple(
            ObservedRound(
                timestamp=row["timestamp"],
                multiplier=Decimal(row["multiplier"]),
            )
            for row in rows
        )
        samples = build_system_state_samples(
            rounds,
            events,
            min_long_window_events=args.min_window_events,
        )
        report = walk_forward_system_state_evaluate(
            samples,
            min_training_samples=args.min_training_samples,
            validation_rows=args.validation_rows,
        )
    except (OSError, ValueError, sqlite3.Error, RuntimeError):
        print(
            "System-state evaluation failed. Check the database and sanitized probe archives.",
            file=sys.stderr,
        )
        return 1

    print(
        "Aviator Intelligence Engine\n"
        f"Phase: {DEVELOPMENT_PHASE}\n"
        f"System-state evaluation: {report.status}\n"
        f"Source: {args.source}\n"
        f"Network probe files: {len(probe_paths)}\n"
        f"Sanitized CDP frame events: {len(events)}\n"
        f"Aligned pre-round samples: {report.aligned_samples}\n"
        f"Evaluated future targets: {report.evaluated_targets}\n"
        f"Warm-up targets skipped: {report.skipped_training_targets}\n"
        f"History-only MAE: {report.history_only_mae if report.history_only_mae is not None else 'N/A'}x\n"
        f"State-only MAE: {report.state_only_mae if report.state_only_mae is not None else 'N/A'}x\n"
        f"Hybrid MAE: {report.hybrid_mae if report.hybrid_mae is not None else 'N/A'}x\n"
        f"Recent-median MAE: {report.recent_median_mae if report.recent_median_mae is not None else 'N/A'}x\n"
        f"Hybrid median absolute error: "
        f"{report.hybrid_median_absolute_error if report.hybrid_median_absolute_error is not None else 'N/A'}x\n"
        f"Hybrid within 0.5x: {report.hybrid_within_half_x_rate if report.hybrid_within_half_x_rate is not None else 'N/A'}\n"
        f"Hybrid within 1.0x: {report.hybrid_within_one_x_rate if report.hybrid_within_one_x_rate is not None else 'N/A'}\n"
        f"Hybrid win rate vs history-only: "
        f"{report.hybrid_win_rate_vs_history if report.hybrid_win_rate_vs_history is not None else 'N/A'}\n"
        f"Hybrid improvement vs history-only: "
        f"{report.hybrid_improvement_vs_history_pct if report.hybrid_improvement_vs_history_pct is not None else 'N/A'}%\n"
        f"History-only log-MAE: {report.history_log_mae if report.history_log_mae is not None else 'N/A'}\n"
        f"State-only log-MAE: {report.state_log_mae if report.state_log_mae is not None else 'N/A'}\n"
        f"Hybrid log-MAE: {report.hybrid_log_mae if report.hybrid_log_mae is not None else 'N/A'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
