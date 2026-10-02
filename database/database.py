"""Open SQLite connections using the configured database path."""

import logging
import sqlite3

from config.settings import Settings


logger = logging.getLogger("aie.database")


def connect_database(settings: Settings) -> sqlite3.Connection:
    """Open a connection without creating application tables.

    The caller owns the connection and must close it, for example with
    contextlib.closing. SQLite's own context manager does not close connections.
    """
    settings.database_dir.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        settings.database_path, timeout=5.0, isolation_level=None,
        autocommit=sqlite3.LEGACY_TRANSACTION_CONTROL,
    )
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
    except sqlite3.Error:
        connection.close()
        raise
    logger.debug("SQLite connection opened.")
    return connection


def connect_database_readonly(settings: Settings) -> sqlite3.Connection:
    """Open an existing database in read-only mode without creating directories.

    pathlib encodes special characters in the URI. query_only also rejects SQL
    writes on this connection. The caller owns and closes the connection.
    """
    uri = settings.database_path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(
        uri, uri=True, timeout=5.0, isolation_level=None,
        autocommit=sqlite3.LEGACY_TRANSACTION_CONTROL,
    )
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA foreign_keys = ON")
    except sqlite3.Error:
        connection.close()
        raise
    logger.debug("Read-only SQLite connection opened.")
    return connection
