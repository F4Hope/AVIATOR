"""Run the direct sanitized protocol-leakage audit."""

from __future__ import annotations

import sys

from config.settings import DEVELOPMENT_PHASE, load_settings
from evaluation.protocol_leak_audit import audit_protocol_leakage
from prediction.round_state import (
    discover_dom_probe_paths,
    infer_round_starts,
    load_completed_round_boundaries,
    load_live_multiplier_observations,
)
from prediction.system_state import discover_network_probe_paths


def main() -> int:
    try:
        settings = load_settings()
        dom_paths = discover_dom_probe_paths(settings.raw_data_dir)
        network_paths = discover_network_probe_paths(settings.raw_data_dir)
        completed = load_completed_round_boundaries(dom_paths)
        live = load_live_multiplier_observations(dom_paths)
        starts = infer_round_starts(completed, live)
        report = audit_protocol_leakage(
            network_paths=network_paths,
            completed_rounds=completed,
            inferred_starts=starts,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Protocol leak audit failed: {exc}", file=sys.stderr)
        return 1

    print(
        "Aviator Intelligence Engine\n"
        f"Phase: {DEVELOPMENT_PHASE}\n"
        "Direct protocol leakage audit: COMPLETE\n"
        f"Network probe files: {report.network_files}\n"
        f"Completed DOM rounds: {report.completed_rounds}\n"
        f"Inferred round starts: {report.inferred_starts}\n"
        f"Structured game-field observations: "
        f"{report.structured_game_field_observations}\n"
        f"Numeric game-field observations: {report.numeric_observations}\n"
        f"Identifier/hash observations: {report.identifier_observations}"
    )

    if not report.numeric_paths:
        print(
            "\nNO NUMERIC GAME-INTEGRITY FIELDS OBSERVED\n"
            "The sanitized collector did not capture decoded JSON fields such as "
            "nextMultiplier, crashPoint, finalMultiplier, or equivalent names. "
            "This does not prove absence of a leak; it means no allow-listed "
            "structured numeric disclosure is present in the collected text/JSON data."
        )
    else:
        print("\nNUMERIC FIELD PATHS")
        for item in report.numeric_paths[:25]:
            verdict = "NO_DIRECT_LEAK"
            if item.exact_matches_before_inferred_start > 0:
                verdict = "POTENTIAL_PRESTART_DISCLOSURE"
            elif item.exact_matches_ge_5s > 0:
                verdict = "POTENTIAL_EARLY_DISCLOSURE"
            elif item.exact_matches_ge_1s > 0:
                verdict = "REVIEW_TIMING"
            print(
                f"\nPath: {item.path}\n"
                f"Observations: {item.observations}\n"
                f"Target intervals seen: {item.target_intervals_seen}\n"
                f"Exact later-result matches: {item.exact_match_targets}\n"
                f"Exact >=0.25s before result: {item.exact_matches_ge_025s}\n"
                f"Exact >=1s before result: {item.exact_matches_ge_1s}\n"
                f"Exact >=2s before result: {item.exact_matches_ge_2s}\n"
                f"Exact >=5s before result: {item.exact_matches_ge_5s}\n"
                f"Exact before inferred start: "
                f"{item.exact_matches_before_inferred_start}\n"
                f"Median earliest exact lead: "
                f"{item.median_earliest_exact_lead_seconds}\n"
                f"Maximum exact lead: {item.maximum_exact_lead_seconds}\n"
                f"Outcome-like path: {item.suspicious_outcome_path}\n"
                f"Decision: {verdict}"
            )

    if report.identifier_paths:
        print("\nIDENTIFIER / COMMITMENT PATHS")
        for item in report.identifier_paths[:25]:
            print(
                f"\nPath: {item.path}\n"
                f"Observations: {item.observations}\n"
                f"Unique hashes: {item.unique_hashes}\n"
                f"Target intervals seen: {item.target_intervals_seen}\n"
                f"Intervals with observation before inferred start: "
                f"{item.intervals_with_prestart_observation}"
            )

    if report.exact_examples:
        print("\nEARLIEST EXACT-MATCH EXAMPLES")
        for item in report.exact_examples[:20]:
            print(
                f"\nPath: {item.path}\n"
                f"Observed: {item.observed_value} at {item.observed_at}\n"
                f"Later actual: {item.actual_multiplier} at "
                f"{item.target_completed_at}\n"
                f"Lead: {item.lead_seconds}s\n"
                f"Before inferred start: {item.before_inferred_start}\n"
                f"Transport event: {item.event_kind}\n"
                f"Sanitized URL: {item.url}"
            )

    strongest = [
        item
        for item in report.numeric_paths
        if item.exact_matches_before_inferred_start > 0
        or item.exact_matches_ge_5s >= 2
    ]
    print("\nOVERALL DECISION")
    if strongest:
        print(
            "POTENTIAL_PROTOCOL_DISCLOSURE_FOUND: review the paths and timing "
            "above. Confirm prospectively before treating any field as exploitable."
        )
    else:
        print(
            "NO_DIRECT_PROTOCOL_DISCLOSURE_DEMONSTRATED in the currently "
            "captured sanitized structured fields."
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
