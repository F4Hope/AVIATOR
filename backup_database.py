"""Create or verify local SQLite backups; this command does not restore them."""

import argparse
import logging
import sqlite3
import sys

from config.logging_config import configure_logging
from config.settings import DEVELOPMENT_PHASE, load_settings
from database.backup import create_backup, verify_backup


logger = logging.getLogger("aie.backup_database")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a verified local backup, or verify a named backup read-only.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--output", help="Optional backup filename inside data/database/backups")
    mode.add_argument("--verify", metavar="FILENAME", help="Verify an existing named backup without restoring")
    parser.add_argument("--expect-sha256", help="Expected file digest; requires --verify")
    arguments = parser.parse_args(argv)
    if arguments.expect_sha256 is not None and arguments.verify is None:
        parser.error("--expect-sha256 requires --verify")
    if sys.version_info < (3, 12):
        print("Backup failed: Python 3.12 or newer is required.", file=sys.stderr)
        return 1
    try:
        settings = load_settings()
        configure_logging(settings.log_level)
        report = verify_backup(settings, arguments.verify, expected_sha256=arguments.expect_sha256) if arguments.verify is not None else create_backup(settings, arguments.output)
    except FileExistsError:
        configure_logging("ERROR")
        logger.error("Backup already exists. Choose a different filename; backups are never overwritten.")
        return 1
    except (OSError, ValueError, sqlite3.Error, RuntimeError):
        configure_logging("ERROR")
        logger.error("Backup failed. Check configuration, schema, integrity, expected digest, file permissions, and database contention.")
        return 1
    print(
        f"Aviator Intelligence Engine\nPhase: {DEVELOPMENT_PHASE}\nBackup: VERIFIED\n"
        f"Mode: {'VERIFY_ONLY' if arguments.verify is not None else 'CREATED'}\n"
        f"Schema version: {report.schema_version}\nRounds backed up: {report.rounds_stored}\n"
        f"File: data/database/backups/{report.path.name}\nBytes: {report.byte_count}\n"
        f"SHA256: {report.sha256}\nDatabase changes: NONE"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
