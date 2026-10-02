"""Phase 4 startup entry point for the Aviator Intelligence Engine."""

from contextlib import closing
from dataclasses import dataclass
import logging
import sqlite3
import sys

from config.logging_config import configure_logging
from config.settings import Settings, ensure_directories, load_settings
from database.database import connect_database
from database.migrations import initialize_schema
from database.repository import RoundRepository


logger = logging.getLogger("aie.main")


@dataclass(frozen=True, slots=True)
class StartupState:
    """Verified startup state, without keeping a database connection open."""

    settings: Settings
    schema_version: int
    rounds_stored: int


def initialize_application(settings: Settings | None = None) -> StartupState:
    """Verify settings and SQLite, initialize the schema, then close the connection."""
    active_settings = settings if settings is not None else load_settings()
    configure_logging(active_settings.log_level)
    ensure_directories(active_settings)

    with closing(connect_database(active_settings)) as connection:
        if connection.execute("SELECT 1").fetchone()[0] != 1:
            raise RuntimeError("SQLite readiness check failed.")
        schema_version = initialize_schema(connection)
        rounds_stored = RoundRepository(connection).count()

    if not all(directory.is_dir() for directory in active_settings.required_directories):
        raise RuntimeError("Required directory check failed.")

    logger.info("Phase 4 initialization complete; schema version %s.", schema_version)
    return StartupState(active_settings, schema_version, rounds_stored)


def main() -> int:
    """Print startup status on success or return a nonzero exit code on failure."""
    if sys.version_info < (3, 12):
        print("Startup failed: Python 3.12 or newer is required.", file=sys.stderr)
        return 1

    try:
        state = initialize_application()
    except (OSError, ValueError, sqlite3.Error, RuntimeError):
        # Avoid printing configuration values, exception details, or credentials.
        configure_logging("ERROR")
        logger.error("Startup failed. Check AIE settings, database schema, and data directory permissions.")
        return 1

    print(
        "Aviator Intelligence Engine\n"
        "Phase: 4\n"
        "Status: INITIALIZED\n"
        "Database: READY\n"
        f"Schema version: {state.schema_version}\n"
        f"Rounds stored: {state.rounds_stored}\n"
        f"Environment: {state.settings.environment}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
