"""Evaluate client-visible pre-round game fields for information leakage."""

from __future__ import annotations

import argparse
import sys

from config.settings import DEVELOPMENT_PHASE, load_settings
from evaluation.game_field_leakage import (
    audit_pre_round_game_fields,
    load_game_field_observations,
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
            "Audit sanitized game-integrity fields visible to the authenticated "
            "browser before inferred next-round start."
        )
    )
    parser.add_argument("--safety-margin-seconds", type=float, default=0.50)
    parser.add_argument("--exact-tolerance", type=float, default=0.001)
    parser.add_argument("--minimum-rounds", type=int, default=3)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.safety_margin_seconds <= 0:
        print("--safety-margin-seconds must be positive.", file=sys.stderr)
        return 2
    if args.exact_tolerance < 0:
        print("--exact-tolerance cannot be negative.", file=sys.stderr)
        return 2
    if args.minimum_rounds < 1:
        print("--minimum-rounds must be positive.", file=sys.stderr)
        return 2

    try:
        settings = load_settings()
        dom_paths = discover_dom_probe_paths(settings.raw_data_dir)
        network_paths = discover_network_probe_paths(settings.raw_data_dir)
        completed = load_completed_round_boundaries(dom_paths)
        live = load_live_multiplier_observations(dom_paths)
        starts = infer_round_starts(completed, live)
        observations = load_game_field_observations(network_paths)
        audit = audit_pre_round_game_fields(
            completed,
            starts,
            observations,
            safety_margin_seconds=args.safety_margin_seconds,
            exact_tolerance=args.exact_tolerance,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Game-field leakage audit failed: {exc}", file=sys.stderr)
        return 1

    print(
        "Aviator Intelligence Engine\n"
        f"Phase: {DEVELOPMENT_PHASE}\n"
        "Client-visible game-field leakage audit: COMPLETE\n"
        f"DOM probe files: {len(dom_paths)}\n"
        f"Network probe files: {len(network_paths)}\n"
        f"Completed DOM boundaries: {len(completed)}\n"
        f"Inferred round starts: {len(starts)}\n"
        f"Eligible pre-start rounds: {audit.eligible_rounds}\n"
        f"Numeric game-field observations: {audit.numeric_observations}\n"
        f"Hashed identifier observations: {audit.identifier_observations}\n"
        f"Safety margin before inferred start: "
        f"{args.safety_margin_seconds:.3f}s"
    )

    numeric = [
        item
        for item in audit.numeric_stats
        if item.rounds_observed >= args.minimum_rounds
    ]
    if not numeric:
        print(
            "\nNUMERIC PRE-ROUND FIELDS\n"
            "No numeric allowlisted game fields have enough observations yet."
        )
    else:
        print("\nNUMERIC PRE-ROUND FIELDS")
        for item in numeric[:30]:
            severity = (
                "DIRECT_LEAK_CANDIDATE"
                if item.rounds_observed >= 5
                and item.last_match_rate >= 0.80
                else "REVIEW"
            )
            print(
                f"{item.path}\n"
                f"  rounds={item.rounds_observed} "
                f"last_exact={item.last_exact_matches} "
                f"last_match_rate={item.last_match_rate:.3f} "
                f"any_match_rate={item.any_match_rate:.3f}\n"
                f"  median_lead={item.median_last_lead_seconds:.3f}s "
                f"minimum_lead={item.minimum_last_lead_seconds:.3f}s "
                f"distinct_values={item.distinct_values}\n"
                f"  decision={severity}"
            )

    identifiers = [
        item
        for item in audit.identifier_stats
        if item.rounds_observed >= args.minimum_rounds
    ]
    if identifiers:
        print("\nPRE-ROUND IDENTIFIER/HASH FIELDS")
        for item in identifiers[:30]:
            print(
                f"{item.path}: rounds={item.rounds_observed} "
                f"unique_hashes={item.unique_hashes}"
            )

    if audit.numeric_observations == 0 and audit.identifier_observations == 0:
        print(
            "\nCapture note: existing archives predate game-field extraction. "
            "Restart the collector after updating and let several rounds run, "
            "then repeat this audit."
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
