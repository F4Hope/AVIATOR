"""Evaluate signals from the inter-round interval before the next round starts."""

from __future__ import annotations

import argparse
from contextlib import closing
from decimal import Decimal
from statistics import median
import sqlite3
import sys

from collectors.dom_round_ingest import DOM_OBSERVATION_SOURCE
from config.settings import DEVELOPMENT_PHASE, load_settings
from database.database import connect_database_readonly
from database.migrations import verify_schema
from evaluation.adaptive_signal_audit import walk_forward_adaptive_signal_audit
from prediction.round_state import (
    discover_dom_probe_paths,
    infer_round_starts,
    load_completed_round_boundaries,
    load_live_multiplier_observations,
)
from prediction.system_state import (
    ObservedRound,
    build_prestart_system_state_samples,
    discover_network_probe_paths,
    load_system_probe_events,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate leakage-safe network state observed after the prior result "
            "but before the next round's first detected live multiplier."
        )
    )
    parser.add_argument("--source", default=DOM_OBSERVATION_SOURCE)
    parser.add_argument("--safety-margin-seconds", type=float, default=0.50)
    parser.add_argument("--min-interval-events", type=int, default=3)
    parser.add_argument("--min-training-samples", type=int, default=30)
    parser.add_argument("--selection-window", type=int, default=12)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings()
        dom_paths = discover_dom_probe_paths(settings.raw_data_dir)
        network_paths = discover_network_probe_paths(settings.raw_data_dir)
        live = load_live_multiplier_observations(dom_paths)
        completed = load_completed_round_boundaries(dom_paths)
        starts = infer_round_starts(completed, live)
        events = load_system_probe_events(network_paths)

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
        samples = build_prestart_system_state_samples(
            rounds,
            events,
            starts,
            safety_margin_seconds=args.safety_margin_seconds,
            min_interval_events=args.min_interval_events,
        )
        report = walk_forward_adaptive_signal_audit(
            samples,
            min_training_samples=args.min_training_samples,
            selection_window=args.selection_window,
        )
    except (OSError, ValueError, sqlite3.Error, RuntimeError):
        print(
            "Pre-start signal evaluation failed. Check DOM/network probe archives and database.",
            file=sys.stderr,
        )
        return 1

    after = [start.seconds_after_previous_completion for start in starts]
    before = [start.seconds_before_target_completion for start in starts]
    print(
        "Aviator Intelligence Engine\n"
        f"Phase: {DEVELOPMENT_PHASE}\n"
        f"Pre-start signal evaluation: {report.status}\n"
        f"Source: {args.source}\n"
        f"DOM probe files: {len(dom_paths)}\n"
        f"Network probe files: {len(network_paths)}\n"
        f"Live multiplier observations: {len(live)}\n"
        f"Completed DOM boundaries: {len(completed)}\n"
        f"Inferred round starts: {len(starts)}\n"
        f"Matched strict pre-start samples: {len(samples)}\n"
        f"Safety margin before detected start: {args.safety_margin_seconds:.3f}s\n"
        f"Median completion-to-start gap: "
        f"{median(after) if after else 'N/A'}s\n"
        f"Median start-to-result gap: "
        f"{median(before) if before else 'N/A'}s\n"
        f"Sanitized CDP frame events: {len(events)}\n"
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
