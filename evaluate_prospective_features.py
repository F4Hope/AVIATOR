"""Evaluate immutable prospective snapshot features against stored priors."""

from __future__ import annotations

import argparse
from contextlib import closing
import sqlite3
import sys

from collectors.dom_round_ingest import DOM_OBSERVATION_SOURCE
from config.settings import DEVELOPMENT_PHASE, load_settings
from database.database import connect_database_readonly
from database.migrations import verify_schema
from evaluation.prospective_feature_audit import (
    load_prospective_feature_samples,
    reconstruct_missing_historical_priors,
    walk_forward_prospective_feature_audit,
)
from prediction.prospective import (
    DEFAULT_SNAPSHOT_LEDGER_FILENAME,
    load_snapshot_ledger,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Walk-forward evaluation of locked prospective pre-round feature "
            "snapshots against the historical probability prior stored at lock time."
        )
    )
    parser.add_argument("--ledger", default=DEFAULT_SNAPSHOT_LEDGER_FILENAME)
    parser.add_argument("--min-training-samples", type=int, default=40)
    parser.add_argument("--selection-window", type=int, default=20)
    parser.add_argument("--hash-bins", type=int, default=24)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if "/" in args.ledger or "\\" in args.ledger or not args.ledger.endswith(".jsonl"):
        print("--ledger must be a plain .jsonl filename.", file=sys.stderr)
        return 2

    try:
        settings = load_settings()
        ledger_path = settings.processed_data_dir / args.ledger
        events = load_snapshot_ledger(ledger_path)
        with closing(connect_database_readonly(settings)) as connection:
            verify_schema(connection)
            rows = connection.execute(
                "SELECT round_id, timestamp, multiplier FROM rounds "
                "WHERE source = ? ORDER BY timestamp ASC, round_id ASC",
                (DOM_OBSERVATION_SOURCE,),
            ).fetchall()
        rounds = tuple(
            (row["round_id"], row["timestamp"], row["multiplier"])
            for row in rows
        )
        audit_events, reconstructed_priors = reconstruct_missing_historical_priors(
            events,
            rounds,
        )
        samples = load_prospective_feature_samples(
            audit_events,
            hash_bins=args.hash_bins,
        )
        report = walk_forward_prospective_feature_audit(
            samples,
            min_training_samples=args.min_training_samples,
            selection_window=args.selection_window,
        )
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(f"Prospective feature audit failed: {exc}", file=sys.stderr)
        return 1

    locked = sum(
        event.get("event") == "pre_round_snapshot_locked"
        for event in events
    )
    scored = sum(
        event.get("event") == "pre_round_snapshot_scored"
        for event in events
    )
    missed = sum(
        event.get("event") == "pre_round_snapshot_missed"
        for event in events
    )

    print(
        "Aviator Intelligence Engine\n"
        f"Phase: {DEVELOPMENT_PHASE}\n"
        "Prospective feature audit: COMPLETE\n"
        f"Ledger events: {len(events)}\n"
        f"Locked snapshots: {locked}\n"
        f"Scored snapshots: {scored}\n"
        f"Missed snapshots: {missed}\n"
        f"Reconstructed historical priors: {reconstructed_priors}\n"
        f"Usable feature/outcome pairs: {len(samples)}\n"
        f"Feature dimension: {report.feature_dimension}\n"
        f"Minimum training samples: {report.min_training_samples}\n"
        f"Audit status: {report.status}"
    )

    for result in report.results:
        trigger_candidate = (
            result.trigger_conditioned_skill_vs_prior_pct > 0
            and result.trigger_conditioned_better_blocks >= 3
        )
        feature_candidate = (
            result.feature_skill_vs_prior_pct > 0
            and result.better_blocks >= 3
        )
        if trigger_candidate and feature_candidate:
            verdict = "TRIGGER_AND_FEATURE_CANDIDATE_EDGE"
        elif trigger_candidate:
            verdict = "TRIGGER_CONDITIONED_CANDIDATE_EDGE"
        elif feature_candidate:
            verdict = "FEATURE_CANDIDATE_EDGE"
        else:
            verdict = "NO_DEMONSTRATED_EDGE"
        print(
            f"\nTHRESHOLD >= {result.threshold}x\n"
            f"Evaluated targets: {result.evaluated_targets}\n"
            f"Positive targets: {result.positives}\n"
            f"Stored/reconstructed historical-prior Brier: "
            f"{result.prior_brier}\n"
            f"Trigger-conditioned Brier: "
            f"{result.trigger_conditioned_brier}\n"
            f"Trigger-conditioned skill vs prior: "
            f"{result.trigger_conditioned_skill_vs_prior_pct}%\n"
            f"Trigger-conditioned better blocks: "
            f"{result.trigger_conditioned_better_blocks}/{result.blocks}\n"
            f"Prospective feature-model Brier: {result.feature_brier}\n"
            f"Feature skill vs prior: {result.feature_skill_vs_prior_pct}%\n"
            f"Feature model better blocks: "
            f"{result.better_blocks}/{result.blocks}\n"
            f"Prediction stddev: {result.prediction_stddev}\n"
            f"Selected models: "
            + ", ".join(
                f"{name}={count}"
                for name, count in result.selected_counts
            )
            + f"\nDecision: {verdict}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
