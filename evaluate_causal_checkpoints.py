"""Evaluate operationally causal inter-round prediction checkpoints."""

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
from prediction.round_state import (
    discover_dom_probe_paths,
    infer_round_starts,
    load_completed_round_boundaries,
    load_live_multiplier_observations,
)
from prediction.system_state import (
    ObservedRound,
    build_causal_checkpoint_system_state_samples,
    discover_network_probe_paths,
    load_system_probe_events,
)


DEFAULT_CHECKPOINTS: tuple[float, ...] = (1.0, 3.0, 5.0, 7.0)


def _skill(reference: Decimal, candidate: Decimal) -> Decimal:
    if reference <= 0:
        return Decimal("0")
    return ((reference - candidate) / reference * Decimal("100")).quantize(
        Decimal("0.000001")
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate fixed real-time prediction checkpoints after the prior "
            "round completes, using only state observed by that checkpoint."
        )
    )
    parser.add_argument("--source", default=DOM_OBSERVATION_SOURCE)
    parser.add_argument(
        "--checkpoints",
        default=",".join(str(value) for value in DEFAULT_CHECKPOINTS),
        help="Comma-separated seconds after the previous completed result.",
    )
    parser.add_argument("--min-interval-events", type=int, default=3)
    parser.add_argument("--min-training-samples", type=int, default=25)
    parser.add_argument("--selection-window", type=int, default=10)
    return parser


def _parse_checkpoints(value: str) -> tuple[float, ...]:
    try:
        checkpoints = tuple(float(item.strip()) for item in value.split(",") if item.strip())
    except ValueError:
        raise ValueError("checkpoints must be comma-separated positive numbers.") from None
    if not checkpoints or any(item <= 0 for item in checkpoints):
        raise ValueError("checkpoints must be comma-separated positive numbers.")
    if tuple(sorted(set(checkpoints))) != checkpoints:
        raise ValueError("checkpoints must be unique and strictly increasing.")
    return checkpoints


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        checkpoints = _parse_checkpoints(args.checkpoints)
        settings = load_settings()
        dom_paths = discover_dom_probe_paths(settings.raw_data_dir)
        network_paths = discover_network_probe_paths(settings.raw_data_dir)
        completed = load_completed_round_boundaries(dom_paths)
        live = load_live_multiplier_observations(dom_paths)
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

        evaluations = []
        for checkpoint in checkpoints:
            samples = build_causal_checkpoint_system_state_samples(
                rounds,
                events,
                starts,
                checkpoint_seconds=checkpoint,
                min_interval_events=args.min_interval_events,
            )
            report = walk_forward_adaptive_signal_audit(
                samples,
                min_training_samples=args.min_training_samples,
                selection_window=args.selection_window,
            )
            evaluations.append((checkpoint, samples, report))
    except (OSError, ValueError, sqlite3.Error, RuntimeError):
        print(
            "Causal checkpoint evaluation failed. Check probe archives, database, and arguments.",
            file=sys.stderr,
        )
        return 1

    print(
        "Aviator Intelligence Engine\n"
        f"Phase: {DEVELOPMENT_PHASE}\n"
        "Causal checkpoint evaluation: COMPLETE\n"
        f"Source: {args.source}\n"
        f"DOM probe files: {len(dom_paths)}\n"
        f"Network probe files: {len(network_paths)}\n"
        f"Completed DOM boundaries: {len(completed)}\n"
        f"Inferred round starts: {len(starts)}\n"
        f"Sanitized CDP frame events: {len(events)}\n"
        f"Checkpoints: {', '.join(f'{value:g}s' for value in checkpoints)}\n"
        f"Minimum training samples: {args.min_training_samples}\n"
        f"Champion selection window: {args.selection_window}"
    )

    for checkpoint, samples, report in evaluations:
        print(
            f"\n=== CHECKPOINT +{checkpoint:g}s AFTER PREVIOUS RESULT ===\n"
            f"Eligible causal samples: {len(samples)}\n"
            f"Audit status: {report.status}"
        )
        for result in report.results:
            state_skill_baseline = _skill(
                result.baseline_brier, result.state_adaptive_brier
            )
            state_skill_history = _skill(
                result.history_adaptive_brier, result.state_adaptive_brier
            )
            print(
                f"\nTHRESHOLD >= {result.threshold}x\n"
                f"Evaluated targets: {result.evaluated_targets}\n"
                f"Expanding baseline Brier: {result.baseline_brier}\n"
                f"Adaptive history Brier: {result.history_adaptive_brier}\n"
                f"Adaptive state Brier: {result.state_adaptive_brier}\n"
                f"Adaptive hybrid Brier: {result.hybrid_adaptive_brier}\n"
                f"Dynamic champion Brier: {result.champion_brier}\n"
                f"State skill vs baseline: {state_skill_baseline}%\n"
                f"State skill vs adaptive history: {state_skill_history}%\n"
                f"Champion skill vs baseline: {result.champion_skill_vs_baseline_pct}%\n"
                f"Champion better blocks than history: "
                f"{result.champion_better_blocks_than_history}/{result.blocks}"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
