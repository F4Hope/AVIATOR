"""Evaluate split-safe causal transport triggers before Aviator round start."""

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
from prediction.prestart_trigger import (
    apply_causal_prestart_triggers,
    build_triggered_system_state_samples,
    discover_prestart_trigger_signatures,
)
from prediction.round_state import (
    discover_dom_probe_paths,
    infer_round_starts,
    load_completed_round_boundaries,
    load_live_multiplier_observations,
)
from prediction.system_state import (
    ObservedRound,
    discover_network_probe_paths,
    load_system_probe_events,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Discover pre-start transport signatures on an earlier chronological "
            "segment, freeze them, and evaluate causal triggered predictions on later rounds."
        )
    )
    parser.add_argument("--source", default=DOM_OBSERVATION_SOURCE)
    parser.add_argument("--discovery-fraction", type=float, default=0.50)
    parser.add_argument("--lookback-seconds", type=float, default=2.5)
    parser.add_argument("--minimum-lead-seconds", type=float, default=0.20)
    parser.add_argument("--minimum-hit-starts", type=int, default=4)
    parser.add_argument("--minimum-hit-rate", type=float, default=0.12)
    parser.add_argument("--minimum-enrichment", type=float, default=1.5)
    parser.add_argument("--max-signatures", type=int, default=12)
    parser.add_argument("--min-training-samples", type=int, default=20)
    parser.add_argument("--selection-window", type=int, default=8)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        settings = load_settings()
        dom_paths = discover_dom_probe_paths(settings.raw_data_dir)
        network_paths = discover_network_probe_paths(settings.raw_data_dir)
        completed = load_completed_round_boundaries(dom_paths)
        live = load_live_multiplier_observations(dom_paths)
        starts = infer_round_starts(completed, live)
        events = load_system_probe_events(network_paths)

        discovery = discover_prestart_trigger_signatures(
            starts,
            events,
            discovery_fraction=args.discovery_fraction,
            lookback_seconds=args.lookback_seconds,
            minimum_lead_seconds=args.minimum_lead_seconds,
            minimum_hit_starts=args.minimum_hit_starts,
            minimum_hit_rate=args.minimum_hit_rate,
            minimum_enrichment=args.minimum_enrichment,
            max_signatures=args.max_signatures,
        )
        triggers = apply_causal_prestart_triggers(
            discovery.evaluation_starts,
            events,
            discovery.signatures,
            minimum_live_lead_seconds=args.minimum_lead_seconds,
        )

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
        samples = build_triggered_system_state_samples(
            rounds,
            events,
            triggers,
            discovery.signatures,
        )

        report = walk_forward_adaptive_signal_audit(
            samples,
            min_training_samples=args.min_training_samples,
            selection_window=args.selection_window,
        )
    except (OSError, ValueError, sqlite3.Error, RuntimeError):
        print(
            "Causal trigger evaluation failed. Check probe archives, database, and arguments.",
            file=sys.stderr,
        )
        return 1

    leads = [item.lead_seconds for item in triggers]
    ages = [item.age_after_previous_seconds for item in triggers]
    coverage = (
        len(triggers) / discovery.evaluation_count
        if discovery.evaluation_count
        else 0.0
    )

    print(
        "Aviator Intelligence Engine\n"
        f"Phase: {DEVELOPMENT_PHASE}\n"
        "Causal pre-start trigger evaluation: COMPLETE\n"
        f"Source: {args.source}\n"
        f"DOM probe files: {len(dom_paths)}\n"
        f"Network probe files: {len(network_paths)}\n"
        f"Inferred round starts: {len(starts)}\n"
        f"Discovery starts: {discovery.discovery_count}\n"
        f"Holdout starts: {discovery.evaluation_count}\n"
        f"Selected trigger signatures: {len(discovery.signatures)}\n"
        f"Holdout causal triggers: {len(triggers)}\n"
        f"Trigger coverage: {coverage:.6f}\n"
        f"Triggered prediction samples: {len(samples)}\n"
        f"Median trigger lead before start: "
        f"{median(leads) if leads else 'N/A'}s\n"
        f"Minimum trigger lead before start: "
        f"{min(leads) if leads else 'N/A'}s\n"
        f"Median trigger age after prior result: "
        f"{median(ages) if ages else 'N/A'}s\n"
        f"Sanitized CDP frame events: {len(events)}"
    )

    if discovery.signatures:
        print("\nFROZEN DISCOVERY SIGNATURES")
        for index, item in enumerate(discovery.signatures, start=1):
            print(
                f"[{index}] score={item.score:.6f} hits={item.hit_starts}/"
                f"{item.discovery_starts} hit_rate={item.hit_rate:.6f} "
                f"enrichment={item.enrichment:.3f} "
                f"median_lead={item.median_lead_seconds:.3f}s "
                f"min_lead={item.minimum_lead_seconds:.3f}s\n"
                f"    {item.signature}"
            )

    for result in report.results:
        state_skill = (
            (result.baseline_brier - result.state_adaptive_brier)
            / result.baseline_brier
            * Decimal("100")
            if result.baseline_brier > 0
            else Decimal("0")
        )
        print(
            f"\nTHRESHOLD >= {result.threshold}x\n"
            f"Evaluated targets: {result.evaluated_targets}\n"
            f"Expanding baseline Brier: {result.baseline_brier}\n"
            f"Adaptive history Brier: {result.history_adaptive_brier}\n"
            f"Adaptive state Brier: {result.state_adaptive_brier}\n"
            f"Adaptive hybrid Brier: {result.hybrid_adaptive_brier}\n"
            f"Dynamic champion Brier: {result.champion_brier}\n"
            f"State skill vs baseline: {state_skill.quantize(Decimal('0.000001'))}%\n"
            f"Champion skill vs baseline: {result.champion_skill_vs_baseline_pct}%\n"
            f"Champion skill vs adaptive history: "
            f"{result.champion_skill_vs_history_pct}%\n"
            f"Champion better blocks than history: "
            f"{result.champion_better_blocks_than_history}/{result.blocks}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
