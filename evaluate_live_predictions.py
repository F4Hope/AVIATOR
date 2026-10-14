"""Print prospective performance of the live threshold prediction ledger."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from config.settings import load_settings
from evaluation.live_threshold_audit import audit_live_thresholds
from prediction.live import DEFAULT_LEDGER_FILENAME, _load_ledger


def _decimal_text(value):
    return None if value is None else format(value, "f")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Audit only genuinely locked-and-scored live threshold forecasts. "
            "Reports chosen, baseline, and challenger Brier performance."
        )
    )
    parser.add_argument("--ledger", default=DEFAULT_LEDGER_FILENAME)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    if (
        "/" in args.ledger
        or "\\" in args.ledger
        or not args.ledger.endswith(".jsonl")
    ):
        parser.error("--ledger must be a plain .jsonl filename.")

    try:
        settings = load_settings()
        path = settings.processed_data_dir / args.ledger
        events = _load_ledger(path)
        report = audit_live_thresholds(events)

        document = {
            "scored_locks": report.scored_locks,
            "invalidated_locks": report.invalidated_locks,
            "unpredicted_rounds": report.unpredicted_rounds,
            "thresholds": [
                {
                    "threshold": format(item.threshold, "f"),
                    "scored_forecasts": item.scored_forecasts,
                    "challenger_selected": item.challenger_selected,
                    "challenger_selection_rate": _decimal_text(
                        item.challenger_selection_rate
                    ),
                    "mean_chosen_brier": _decimal_text(item.mean_chosen_brier),
                    "mean_baseline_brier": _decimal_text(item.mean_baseline_brier),
                    "mean_raw_candidate_brier": _decimal_text(
                        item.mean_raw_candidate_brier
                    ),
                    "comparable_candidate_rows": item.comparable_candidate_rows,
                    "chosen_skill_vs_baseline_pct": _decimal_text(
                        item.chosen_skill_vs_baseline_pct
                    ),
                    "raw_candidate_skill_vs_baseline_pct": _decimal_text(
                        item.raw_candidate_skill_vs_baseline_pct
                    ),
                    "average_forecast": _decimal_text(item.average_forecast),
                    "observed_rate": _decimal_text(item.observed_rate),
                    "calibration_gap": _decimal_text(item.calibration_gap),
                }
                for item in report.thresholds
            ],
        }

        if args.as_json:
            print(json.dumps(document, indent=2, sort_keys=True))
            return 0

        print("Aviator Intelligence Engine")
        print("Prospective threshold audit")
        print(f"Scored locks: {report.scored_locks}")
        print(f"Invalidated locks: {report.invalidated_locks}")
        print(f"Unpredicted rounds: {report.unpredicted_rounds}")
        if not report.thresholds:
            print("No scored threshold forecasts are available yet.")
            return 0

        for item in report.thresholds:
            print()
            print(f"Threshold >= {item.threshold}x")
            print(f"  scored forecasts: {item.scored_forecasts}")
            print(
                "  challenger selected: "
                f"{item.challenger_selected} "
                f"({_decimal_text(item.challenger_selection_rate)})"
            )
            print(f"  chosen Brier: {_decimal_text(item.mean_chosen_brier)}")
            print(f"  baseline Brier: {_decimal_text(item.mean_baseline_brier)}")
            print(
                "  raw challenger Brier: "
                f"{_decimal_text(item.mean_raw_candidate_brier)} "
                f"(n={item.comparable_candidate_rows})"
            )
            print(
                "  chosen skill vs baseline: "
                f"{_decimal_text(item.chosen_skill_vs_baseline_pct)}%"
            )
            print(
                "  raw challenger skill vs baseline: "
                f"{_decimal_text(item.raw_candidate_skill_vs_baseline_pct)}%"
            )
            print(f"  average forecast: {_decimal_text(item.average_forecast)}")
            print(f"  observed rate: {_decimal_text(item.observed_rate)}")
            print(f"  calibration gap: {_decimal_text(item.calibration_gap)}")

        print()
        print(
            "Interpretation: lower Brier is better. Positive skill means the "
            "model beat the historical baseline on the same prospectively "
            "locked outcomes. This does not imply a guaranteed future edge."
        )
        return 0
    except (OSError, ValueError, RuntimeError):
        print(
            "Live threshold audit failed. Check the prediction ledger.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
