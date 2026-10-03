"""Run the live next-completion prediction lock and scoring loop."""

from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
from pathlib import Path
import sqlite3
import sys
import time

from config.settings import DEVELOPMENT_PHASE, load_settings
from models.baseline import BaselineConfig
from prediction.live import DEFAULT_LEDGER_FILENAME, process_live_prediction_cycle


def _threshold(value: str) -> Decimal:
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise argparse.ArgumentTypeError("thresholds must be decimal values.") from None
    if not result.is_finite() or result < 1:
        raise argparse.ArgumentTypeError("thresholds must be finite and at least 1.")
    return result


def _file_signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_size, stat.st_mtime_ns


def _print_event(event: dict[str, object]) -> None:
    kind = event.get("event")
    if kind == "prediction_locked":
        print(
            "\nPREDICTION LOCKED\n"
            f"Lock ID: {event['lock_id']}\n"
            f"Locked at: {event['locked_at']}\n"
            f"History count: {event['history_count']}\n"
            f"History through: {event['history_last_timestamp']}\n"
            f"Status: {event['status']}\n"
            f"Probabilities: {event['threshold_probabilities']}\n"
            "Timing: locked before next completed result"
        )
    elif kind == "prediction_scored":
        print(
            "\nPREDICTION SCORED\n"
            f"Lock ID: {event['lock_id']}\n"
            f"Actual: {event['actual_multiplier']}x\n"
            f"Completed at: {event['actual_timestamp']}\n"
            f"Scores: {event['threshold_scores']}"
        )
    elif kind == "prediction_gap":
        print(
            "\nPREDICTION GAP\n"
            f"Unpredicted completed rounds: {event['unpredicted_rounds']}\n"
            "No retroactive predictions were created."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Lock leakage-safe threshold predictions from completed history and "
            "score them only after the next DOM-observed result."
        )
    )
    parser.add_argument("--input", default="aviator-dom-multipliers.jsonl")
    parser.add_argument("--ledger", default=DEFAULT_LEDGER_FILENAME)
    parser.add_argument("--threshold", action="append", type=_threshold, dest="thresholds")
    parser.add_argument("--min-history", type=int, default=50)
    parser.add_argument("--poll-seconds", type=float, default=0.5)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)

    if sys.version_info < (3, 12):
        print("Live prediction failed: Python 3.12 or newer is required.", file=sys.stderr)
        return 1
    if "/" in args.input or "\\" in args.input or not args.input.endswith(".jsonl"):
        parser.error("--input must be a plain .jsonl filename.")
    if "/" in args.ledger or "\\" in args.ledger or not args.ledger.endswith(".jsonl"):
        parser.error("--ledger must be a plain .jsonl filename.")
    if args.min_history < 1:
        parser.error("--min-history must be positive.")
    if not 0.1 <= args.poll_seconds <= 60:
        parser.error("--poll-seconds must be between 0.1 and 60.")

    thresholds = tuple(args.thresholds) if args.thresholds else (
        Decimal("1.5"), Decimal("2"), Decimal("5")
    )

    try:
        config = BaselineConfig(thresholds=thresholds, min_history=args.min_history)
        settings = load_settings()
        dom_path = settings.raw_data_dir / args.input
        ledger_path = settings.processed_data_dir / args.ledger

        print(
            "Aviator Intelligence Engine\n"
            f"Phase: {DEVELOPMENT_PHASE}\n"
            "Live prediction: RUNNING\n"
            f"Minimum history: {config.min_history}\n"
            f"Thresholds: {[str(value) for value in config.thresholds]}\n"
            f"Ledger: {ledger_path}\n"
            "Prediction semantics: next completed result; no provider round ID assumed."
        )

        for event in process_live_prediction_cycle(settings, dom_path, ledger_path, config):
            _print_event(event)

        if args.once:
            return 0

        signature = _file_signature(dom_path)
        while True:
            time.sleep(args.poll_seconds)
            current = _file_signature(dom_path)
            if current == signature:
                continue
            signature = current
            for event in process_live_prediction_cycle(settings, dom_path, ledger_path, config):
                _print_event(event)

    except KeyboardInterrupt:
        print("\nLive prediction: STOPPED")
        return 0
    except (OSError, ValueError, sqlite3.Error, RuntimeError):
        print(
            "Live prediction failed. Check the active DOM capture, SQLite database, and ledger.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
