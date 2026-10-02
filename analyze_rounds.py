"""Phase 4 CLI for descriptive analysis and optional aggregate JSON export."""

import argparse
from datetime import datetime
import logging
import sqlite3
import sys

from analysis.descriptive import AnalysisFilters, analyze_database
from analysis.reporting import decimal_text, write_json_report
from config.logging_config import configure_logging
from config.settings import load_settings


logger = logging.getLogger("aie.analyze_rounds")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize historical rounds without changing the database.")
    parser.add_argument("--source", help="Select one source")
    parser.add_argument("--start", help="Inclusive result time, ISO 8601 with timezone")
    parser.add_argument("--end", help="Exclusive result time, ISO 8601 with timezone")
    parser.add_argument("--output", help="Optional JSON filename saved inside data/processed")
    parser.add_argument("--overwrite", action="store_true", help="Allow replacement of the named report")
    arguments = parser.parse_args(argv)
    if arguments.overwrite and arguments.output is None:
        parser.error("--overwrite requires --output")
    if sys.version_info < (3, 12):
        print("Analysis failed: Python 3.12 or newer is required.", file=sys.stderr)
        return 1
    try:
        settings = load_settings()
        configure_logging(settings.log_level)
        filters = AnalysisFilters(
            source=arguments.source,
            start=datetime.fromisoformat(arguments.start) if arguments.start is not None else None,
            end=datetime.fromisoformat(arguments.end) if arguments.end is not None else None,
        )
        report = analyze_database(settings, filters)
        if arguments.output is not None:
            write_json_report(report, settings, arguments.output, overwrite=arguments.overwrite)
    except FileExistsError:
        configure_logging("ERROR")
        logger.error("Report already exists. Choose another filename or use --overwrite.")
        return 1
    except (OSError, ValueError, sqlite3.Error, RuntimeError):
        configure_logging("ERROR")
        logger.error("Analysis failed. Check filters, schema, permissions, and selection size; initialize the database with python main.py if needed.")
        return 1

    print(
        "Aviator Intelligence Engine\nPhase: 4\n"
        f"Analysis: {report.status}\nDatabase rounds: {report.total_stored}\n"
        f"Selected rounds: {report.selected_rounds}\nSources: {report.source_count}"
    )
    if report.selected_rounds:
        print(
            f"Minimum multiplier: {decimal_text(report.multipliers.minimum)}\n"
            f"Median multiplier: {decimal_text(report.multipliers.median)}\n"
            f"Mean multiplier: {decimal_text(report.multipliers.mean)}\n"
            f"Maximum multiplier: {decimal_text(report.multipliers.maximum)}"
        )
    else:
        print("No rounds matched this selection.")
    if arguments.output is not None:
        print("Report: SAVED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
