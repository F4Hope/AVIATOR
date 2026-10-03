"""Command-line entry point for importing DOM-observed completed rounds."""

from __future__ import annotations

import argparse
import sqlite3
import sys

from collectors.dom_round_ingest import ingest_dom_rounds
from config.logging_config import configure_logging
from config.settings import DEVELOPMENT_PHASE, load_settings
from database.repository import DuplicateRoundError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract completed rounds from the sanitized DOM history capture and store them."
    )
    parser.add_argument(
        "--input",
        default="aviator-dom-multipliers.jsonl",
        help="Plain JSONL filename under data/raw",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Extract and validate without changing SQLite",
    )
    args = parser.parse_args(argv)

    if sys.version_info < (3, 12):
        print("DOM ingestion failed: Python 3.12 or newer is required.", file=sys.stderr)
        return 1
    if "/" in args.input or "\\" in args.input or not args.input.endswith(".jsonl"):
        parser.error("--input must be a plain .jsonl filename.")

    try:
        settings = load_settings()
        configure_logging(settings.log_level)
        path = settings.raw_data_dir / args.input
        report = ingest_dom_rounds(path, settings, dry_run=args.dry_run)
    except DuplicateRoundError:
        configure_logging("ERROR")
        print("DOM ingestion failed: conflicting local observation identity.", file=sys.stderr)
        return 1
    except (OSError, ValueError, sqlite3.Error, RuntimeError):
        configure_logging("ERROR")
        print(
            "DOM ingestion failed. Check the DOM capture, configuration, and database.",
            file=sys.stderr,
        )
        return 1

    print(f"Aviator Intelligence Engine\nPhase: {DEVELOPMENT_PHASE}")
    if report.dry_run:
        print(
            "DOM ingestion: VALIDATED\n"
            f"Completed rounds extracted: {report.extracted}\n"
            "Database changes: NONE"
        )
    else:
        print(
            "DOM ingestion: COMPLETE\n"
            f"Completed rounds extracted: {report.extracted}\n"
            f"Inserted: {report.inserted}\n"
            f"Duplicates: {report.duplicates}"
        )
    print("Source: AIE_DOM_HISTORY_BETPAWA_CM")
    print("Provider round IDs: NOT CLAIMED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
