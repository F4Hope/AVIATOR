"""Explicit CLI export of stored completed rounds to the portable AIE format."""

import argparse
from datetime import datetime
import logging
import sqlite3
import sys

from analysis.descriptive import AnalysisFilters
from config.logging_config import configure_logging
from config.settings import DEVELOPMENT_PHASE, load_settings
from database.export import export_rounds


logger = logging.getLogger("aie.export_rounds")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export stored rounds without changing the database.")
    parser.add_argument("--output", default="rounds-export.json", help="JSON filename inside data/processed")
    parser.add_argument("--source", help="Select one exact source")
    parser.add_argument("--start", help="Inclusive completion time, ISO 8601 with timezone")
    parser.add_argument("--end", help="Exclusive completion time, ISO 8601 with timezone")
    parser.add_argument("--overwrite", action="store_true", help="Explicitly replace this export file")
    arguments = parser.parse_args(argv)
    if sys.version_info < (3, 12):
        print("Export failed: Python 3.12 or newer is required.", file=sys.stderr)
        return 1
    try:
        settings = load_settings()
        configure_logging(settings.log_level)
        filters = AnalysisFilters(
            source=arguments.source,
            start=datetime.fromisoformat(arguments.start) if arguments.start is not None else None,
            end=datetime.fromisoformat(arguments.end) if arguments.end is not None else None,
        )
        report = export_rounds(settings, arguments.output, filters, overwrite=arguments.overwrite)
    except FileExistsError:
        configure_logging("ERROR")
        logger.error("Export already exists. Choose another filename or use --overwrite.")
        return 1
    except (OSError, ValueError, sqlite3.Error, RuntimeError):
        configure_logging("ERROR")
        logger.error("Export failed. Check configuration, filters, schema, stored data, output permissions, and import-compatible size limits.")
        return 1
    print(
        f"Aviator Intelligence Engine\nPhase: {DEVELOPMENT_PHASE}\nExport: SAVED\n"
        f"Database rounds: {report.total_stored}\nRounds exported: {report.exported_rounds}\n"
        f"File: data/processed/{report.path.name}\nBytes: {report.byte_count}\n"
        f"SHA256: {report.sha256}\nDatabase changes: NONE"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
