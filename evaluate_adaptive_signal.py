"""Evaluate rapid online adaptation to changing pre-round signal regimes."""

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
from evaluation.adaptive_signal_audit import walk_forward_adaptive_signal_audit
from prediction.system_state import (
    ObservedRound,
    build_system_state_samples,
    discover_network_probe_paths,
    load_system_probe_events,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Prequential champion-challenger evaluation with fast forgetting "
            "for changing Aviator system regimes."
        )
    )
    parser.add_argument("--source", default=DOM_OBSERVATION_SOURCE)
    parser.add_argument("--min-training-samples", type=int, default=40)
    parser.add_argument("--selection-window", type=int, default=16)
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
        report = walk_forward_adaptive_signal_audit(
            samples,
            min_training_samples=args.min_training_samples,
            selection_window=args.selection_window,
        )
    except (OSError, ValueError, sqlite3.Error, RuntimeError):
        print(
            "Adaptive signal audit failed. Check the database and sanitized probe archives.",
            file=sys.stderr,
        )
        return 1

    print(
        "Aviator Intelligence Engine\n"
        f"Phase: {DEVELOPMENT_PHASE}\n"
        f"Adaptive signal audit: {report.status}\n"
        f"Source: {args.source}\n"
        f"Network probe files: {len(probe_paths)}\n"
        f"Sanitized CDP frame events: {len(events)}\n"
        f"Aligned pre-round samples: {report.aligned_samples}\n"
        f"Minimum training samples: {report.min_training_samples}\n"
        f"Champion selection window: {report.selection_window}"
    )

    for result in report.results:
        print(
            f"\nTHRESHOLD >= {result.threshold}x\n"
            f"Evaluated targets: {result.evaluated_targets}\n"
            f"Positive targets: {result.positives}\n"
            f"Observed prevalence: {result.prevalence}\n"
            f"Expanding baseline Brier: {result.baseline_brier}\n"
            f"Adaptive history Brier: {result.history_adaptive_brier}\n"
            f"Adaptive state Brier: {result.state_adaptive_brier}\n"
            f"Adaptive hybrid Brier: {result.hybrid_adaptive_brier}\n"
            f"Dynamic champion Brier: {result.champion_brier}\n"
            f"Champion skill vs baseline: {result.champion_skill_vs_baseline_pct}%\n"
            f"Champion skill vs adaptive history: {result.champion_skill_vs_history_pct}%\n"
            f"Champion switches: {result.champion_switches}\n"
            f"Fast-forgetting selection rate: {result.fast_forgetting_selection_rate}\n"
            f"State/hybrid selection rate: {result.state_or_hybrid_selection_rate}\n"
            f"Champion better blocks than history: "
            f"{result.champion_better_blocks_than_history}/{result.blocks}\n"
            "Selected models: "
            + ", ".join(f"{name}={count}" for name, count in result.selected_counts)
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
