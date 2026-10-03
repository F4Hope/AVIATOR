"""Evaluate immutable prospective snapshot features against stored priors."""

from __future__ import annotations

import argparse
import sys

from config.settings import DEVELOPMENT_PHASE, load_settings
from evaluation.prospective_feature_audit import (
    load_prospective_feature_samples,
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
        samples = load_prospective_feature_samples(
            events,
            hash_bins=args.hash_bins,
        )
        report = walk_forward_prospective_feature_audit(
            samples,
            min_training_samples=args.min_training_samples,
            selection_window=args.selection_window,
        )
    except (OSError, ValueError, RuntimeError) as exc:
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
        f"Usable feature/outcome pairs: {len(samples)}\n"
        f"Feature dimension: {report.feature_dimension}\n"
        f"Minimum training samples: {report.min_training_samples}\n"
        f"Audit status: {report.status}"
    )

    for result in report.results:
        verdict = (
            "CANDIDATE_EDGE"
            if result.feature_skill_vs_prior_pct > 0
            and result.better_blocks >= 3
            else "NO_DEMONSTRATED_EDGE"
        )
        print(
            f"\nTHRESHOLD >= {result.threshold}x\n"
            f"Evaluated targets: {result.evaluated_targets}\n"
            f"Positive targets: {result.positives}\n"
            f"Stored historical-prior Brier: {result.prior_brier}\n"
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
