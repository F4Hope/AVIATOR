"""Run the live next-completion prediction lock and scoring loop."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sqlite3
import sys
import time

from config.settings import DEVELOPMENT_PHASE, load_settings
from models.baseline import BaselineConfig
from prediction.live import (
    DEFAULT_LEDGER_FILENAME,
    invalidate_if_collector_session_changed,
    invalidate_outstanding_lock,
    process_live_prediction_cycle,
)


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


def _heartbeat_session(path: Path, max_age_seconds: float) -> str | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            return None
        observed_at = value.get("observed_at")
        session_id = value.get("collector_session_id")
        if (
            value.get("kind") != "dom_probe_heartbeat"
            or not isinstance(observed_at, str)
            or not isinstance(session_id, str)
            or not session_id
        ):
            return None
        heartbeat_at = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        age = (datetime.now(UTC) - heartbeat_at.astimezone(UTC)).total_seconds()
        return session_id if 0 <= age <= max_age_seconds else None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


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
    elif kind == "prediction_invalidated":
        print(
            "\nPREDICTION INVALIDATED\n"
            f"Lock ID: {event['lock_id']}\n"
            f"Reason: {event['reason']}\n"
            "This lock will not be scored."
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
    parser.add_argument("--heartbeat", default="aviator-dom-heartbeat.json")
    parser.add_argument("--max-heartbeat-age", type=float, default=5.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)

    if sys.version_info < (3, 12):
        print("Live prediction failed: Python 3.12 or newer is required.", file=sys.stderr)
        return 1
    if "/" in args.input or "\\" in args.input or not args.input.endswith(".jsonl"):
        parser.error("--input must be a plain .jsonl filename.")
    if "/" in args.ledger or "\\" in args.ledger or not args.ledger.endswith(".jsonl"):
        parser.error("--ledger must be a plain .jsonl filename.")
    if "/" in args.heartbeat or "\\" in args.heartbeat or not args.heartbeat.endswith(".json"):
        parser.error("--heartbeat must be a plain .json filename.")
    if args.min_history < 1:
        parser.error("--min-history must be positive.")
    if not 0.1 <= args.poll_seconds <= 60:
        parser.error("--poll-seconds must be between 0.1 and 60.")
    if not 2.0 <= args.max_heartbeat_age <= 60:
        parser.error("--max-heartbeat-age must be between 2 and 60 seconds.")

    thresholds = tuple(args.thresholds) if args.thresholds else (
        Decimal("1.5"), Decimal("2"), Decimal("5")
    )

    try:
        config = BaselineConfig(thresholds=thresholds, min_history=args.min_history)
        settings = load_settings()
        dom_path = settings.raw_data_dir / args.input
        ledger_path = settings.processed_data_dir / args.ledger
        heartbeat_path = settings.raw_data_dir / args.heartbeat

        print(
            "Aviator Intelligence Engine\n"
            f"Phase: {DEVELOPMENT_PHASE}\n"
            "Live prediction: RUNNING\n"
            f"Minimum history: {config.min_history}\n"
            f"Thresholds: {[str(value) for value in config.thresholds]}\n"
            f"Ledger: {ledger_path}\n"
            "Prediction semantics: next completed result; no provider round ID assumed."
        )

        collector_session_id = _heartbeat_session(heartbeat_path, args.max_heartbeat_age)
        healthy = collector_session_id is not None
        if healthy:
            changed = invalidate_if_collector_session_changed(
                ledger_path, collector_session_id
            )
            if changed is not None:
                _print_event(changed)
            for event in process_live_prediction_cycle(
                settings, dom_path, ledger_path, config, collector_session_id
            ):
                _print_event(event)
        else:
            invalidated = invalidate_outstanding_lock(
                ledger_path, "collector_heartbeat_missing_or_stale"
            )
            if invalidated is not None:
                _print_event(invalidated)
            print(
                "\nCOLLECTOR NOT LIVE\n"
                "Waiting for a fresh collector heartbeat before locking or scoring."
            )

        if args.once:
            return 0

        signature = _file_signature(dom_path)
        was_healthy = healthy
        while True:
            time.sleep(args.poll_seconds)
            collector_session_id = _heartbeat_session(
                heartbeat_path, args.max_heartbeat_age
            )
            healthy = collector_session_id is not None
            if not healthy:
                invalidated = invalidate_outstanding_lock(
                    ledger_path, "collector_heartbeat_missing_or_stale"
                )
                if invalidated is not None:
                    _print_event(invalidated)
                if was_healthy:
                    print(
                        "\nCOLLECTOR NOT LIVE\n"
                        "Prediction scoring paused until capture continuity is restored."
                    )
                was_healthy = False
                continue

            recovered = not was_healthy
            was_healthy = True
            changed = invalidate_if_collector_session_changed(
                ledger_path, collector_session_id
            )
            if changed is not None:
                _print_event(changed)
            current = _file_signature(dom_path)
            if not recovered and current == signature:
                continue
            signature = current
            for event in process_live_prediction_cycle(
                settings, dom_path, ledger_path, config, collector_session_id
            ):
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
