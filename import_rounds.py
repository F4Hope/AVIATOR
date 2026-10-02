"""Command-line entry point for Phase 3 local completed-round JSON imports."""

import argparse
import logging
from pathlib import Path
import sqlite3
import sys

from collectors.json_importer import ImportValidationError, import_json_file
from config.logging_config import configure_logging
from config.settings import load_settings
from database.repository import DuplicateRoundError


logger = logging.getLogger("aie.import_rounds")


def main(argv: list[str] | None = None) -> int:
    """Return 0 on success, 1 on import failure; argparse uses 2 for usage errors."""
    parser = argparse.ArgumentParser(description="Validate and import a local completed-round JSON file.")
    parser.add_argument("file", type=Path, help="Path to an AIE format-version-1 JSON file")
    parser.add_argument("--dry-run", action="store_true", help="Validate the file without opening or changing the database")
    arguments = parser.parse_args(argv)
    if sys.version_info < (3, 12):
        print("Import failed: Python 3.12 or newer is required.", file=sys.stderr)
        return 1
    try:
        settings = load_settings()
        configure_logging(settings.log_level)
        report = import_json_file(arguments.file, settings, dry_run=arguments.dry_run)
    except ImportValidationError as error:
        configure_logging("ERROR")
        location = f" at round {error.row_number}" if error.row_number is not None else ""
        logger.error("Import rejected: %s%s. No round records were added.", error.code, location)
        return 1
    except DuplicateRoundError:
        configure_logging("ERROR")
        logger.error("Import rejected: conflicting duplicate. No round records were added.")
        return 1
    except (OSError, ValueError, sqlite3.Error, RuntimeError):
        configure_logging("ERROR")
        logger.error("Import failed. Check configuration, database compatibility, and file permissions.")
        return 1

    print("Aviator Intelligence Engine\nPhase: 3")
    if report.dry_run:
        print(f"Import: VALIDATED\nRows validated: {report.rows_read}\nDatabase check: NOT RUN\nDatabase changes: NONE")
    else:
        print(f"Import: COMPLETE\nRows read: {report.rows_read}\nInserted: {report.inserted}\nDuplicates: {report.duplicates}")
    print(f"Input SHA256: {report.sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
