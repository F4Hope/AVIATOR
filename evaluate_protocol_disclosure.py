"""Audit sanitized protocol metadata for pre-round exposure candidates."""

from __future__ import annotations

import argparse
from decimal import Decimal
import sys

from config.settings import DEVELOPMENT_PHASE, load_settings
from evaluation.protocol_disclosure_audit import (
    audit_protocol_disclosures,
    load_protocol_events,
)
from prediction.round_state import (
    discover_dom_probe_paths,
    infer_round_starts,
    load_completed_round_boundaries,
    load_live_multiplier_observations,
)
from prediction.system_state import discover_network_probe_paths


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Search sanitized received JSON metadata for future-outcome-like fields, "
            "round identifiers, and categorical pre-round protocol states."
        )
    )
    parser.add_argument("--safety-margin-seconds", type=float, default=2.0)
    parser.add_argument("--numeric-tolerance", type=float, default=0.01)
    parser.add_argument("--category-min-support", type=int, default=5)
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
        events = load_protocol_events(network_paths)

        target_multipliers = {
            item.observed_at: Decimal(item.multiplier)
            for item in completed
        }
        previous_multipliers = dict(target_multipliers)

        report = audit_protocol_disclosures(
            starts=starts,
            events=events,
            target_multipliers=target_multipliers,
            previous_multipliers=previous_multipliers,
            safety_margin_seconds=args.safety_margin_seconds,
            numeric_tolerance=args.numeric_tolerance,
            minimum_category_support=args.category_min_support,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Protocol exposure audit failed: {exc}", file=sys.stderr)
        return 1

    print(
        "Aviator Intelligence Engine\n"
        f"Phase: {DEVELOPMENT_PHASE}\n"
        "Protocol exposure audit: COMPLETE\n"
        f"DOM probe files: {len(dom_paths)}\n"
        f"Network probe files: {len(network_paths)}\n"
        f"Completed DOM boundaries: {len(completed)}\n"
        f"Inferred round starts: {len(starts)}\n"
        f"Sanitized JSON metadata events: {len(events)}\n"
        f"Eligible pre-start intervals: {report.eligible_intervals}\n"
        f"Intervals with protocol metadata: {report.metadata_intervals}\n"
        f"Safety margin before detected start: "
        f"{report.safety_margin_seconds:.3f}s"
    )

    print("\nNUMERIC GAME-INTEGRITY FIELDS")
    if not report.numeric_fields:
        print("None observed in eligible pre-start intervals.")
    else:
        for item in report.numeric_fields:
            print(
                f"{item.path}: observations={item.observations}, rounds={item.rounds}, "
                f"target_matches={item.target_matches}, "
                f"previous_matches={item.previous_matches}, "
                f"target_match_rate={item.target_match_rate:.6f}, "
                f"previous_match_rate={item.previous_match_rate:.6f}, "
                f"median_lead={item.median_lead_seconds}, "
                f"minimum_lead={item.minimum_lead_seconds}, "
                f"verdict={item.verdict}"
            )

    candidates = [
        item
        for item in report.numeric_fields
        if item.verdict == "CANDIDATE_PRE_ROUND_DISCLOSURE"
    ]
    print("\nDIRECT EXPOSURE DECISION")
    if candidates:
        print(
            "CANDIDATE_PRE_ROUND_EXPOSURE_FOUND — requires prospective "
            "live confirmation before any conclusion."
        )
        for item in candidates:
            print(
                f"  {item.path}: {item.target_matches}/{item.observations} "
                f"target matches, median lead {item.median_lead_seconds}s"
            )
    else:
        print("NO_DIRECT_PRE_ROUND_EXPOSURE_DEMONSTRATED")

    print("\nIDENTIFIER / COMMITMENT FIELDS")
    if not report.identifier_fields:
        print("None observed in eligible pre-start intervals.")
    else:
        for item in report.identifier_fields[:20]:
            print(
                f"{item.path}: observations={item.observations}, rounds={item.rounds}, "
                f"unique_hashes={item.unique_hashes}, "
                f"median_lead={item.median_lead_seconds}"
            )

    print("\nCATEGORICAL PROTOCOL STATE")
    print(f"Events with categorical state: {report.categorical_events}")
    print(f"Unique categorical tokens: {report.unique_categorical_tokens}")
    if not report.categorical_holdout:
        print(
            "No split-safe categorical holdout candidates yet. "
            "Restart the updated collector to accumulate event/type/state/phase metadata."
        )
    else:
        shown = 0
        for item in report.categorical_holdout:
            if item.holdout_support < 3:
                continue
            print(
                f">={item.threshold}x | {item.token} | "
                f"discovery_support={item.discovery_support}, "
                f"discovery_lift={item.discovery_lift:+.6f}, "
                f"holdout_support={item.holdout_support}, "
                f"holdout_lift={item.holdout_lift}, "
                f"direction_consistent={item.direction_consistent}"
            )
            shown += 1
            if shown >= 20:
                break
        if shown == 0:
            print("No categorical candidate had adequate holdout support.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
