"""Transactional schema initialization and explicit schema version checks."""

from datetime import UTC, datetime
import logging
import sqlite3

from database.models import timestamp_text


SCHEMA_VERSION = 1
logger = logging.getLogger("aie.database.migrations")


class SchemaVersionError(RuntimeError):
    """The database is unmanaged, incompatible, or newer than this code."""


MIGRATION_STATEMENTS: tuple[str, ...] = (
    """CREATE TABLE schema_migrations (
        version INTEGER PRIMARY KEY,
        description TEXT NOT NULL,
        applied_at TEXT NOT NULL
    )""",
    """CREATE TABLE rounds (
        id INTEGER PRIMARY KEY,
        source TEXT NOT NULL CHECK (length(trim(source)) > 0),
        round_id TEXT NOT NULL CHECK (length(trim(round_id)) > 0),
        timestamp TEXT NOT NULL CHECK (length(timestamp) = 27),
        multiplier TEXT NOT NULL CHECK (
            length(multiplier) BETWEEN 1 AND 256
            AND multiplier NOT GLOB '*[^0-9.]*'
            AND multiplier NOT LIKE '%.%.%'
            AND substr(multiplier, 1, 1) <> '.'
            AND substr(multiplier, -1, 1) <> '.'
            AND CAST(multiplier AS REAL) >= 1
        ),
        collection_timestamp TEXT NOT NULL CHECK (length(collection_timestamp) = 27),
        started_at TEXT CHECK (started_at IS NULL OR length(started_at) = 27),
        pre_round_observed_at TEXT CHECK (
            pre_round_observed_at IS NULL OR length(pre_round_observed_at) = 27
        ),
        pre_round_data TEXT NOT NULL CHECK (
            json_valid(pre_round_data) AND json_type(pre_round_data) = 'object'
        ),
        post_round_data TEXT NOT NULL CHECK (
            json_valid(post_round_data) AND json_type(post_round_data) = 'object'
        ),
        raw_data TEXT NOT NULL CHECK (json_valid(raw_data) AND json_type(raw_data) = 'object'),
        ingested_at TEXT NOT NULL CHECK (length(ingested_at) = 27),
        UNIQUE (source, round_id),
        CHECK (collection_timestamp >= timestamp),
        CHECK (started_at IS NULL OR started_at <= timestamp),
        CHECK (
            (pre_round_observed_at IS NULL AND pre_round_data = '{}')
            OR (pre_round_observed_at IS NOT NULL AND started_at IS NOT NULL
                AND pre_round_observed_at < started_at)
        )
    )""",
    "CREATE INDEX idx_rounds_timestamp ON rounds (timestamp, source, round_id)",
    "CREATE INDEX idx_rounds_source_timestamp ON rounds (source, timestamp, round_id)",
    """CREATE TRIGGER rounds_no_update BEFORE UPDATE ON rounds
        BEGIN SELECT RAISE(ABORT, 'Stored rounds are immutable'); END""",
    """CREATE TRIGGER rounds_no_delete BEFORE DELETE ON rounds
        BEGIN SELECT RAISE(ABORT, 'Stored rounds are immutable'); END""",
)

EXPECTED_ROUND_COLUMNS = {
    "id", "source", "round_id", "timestamp", "multiplier", "collection_timestamp",
    "started_at", "pre_round_observed_at", "pre_round_data", "post_round_data",
    "raw_data", "ingested_at",
}


def _verify_schema(connection: sqlite3.Connection) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(rounds)")}
    objects = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'index', 'trigger')"
    )}
    required = {
        "rounds", "schema_migrations", "idx_rounds_timestamp", "idx_rounds_source_timestamp",
        "rounds_no_update", "rounds_no_delete",
    }
    if columns != EXPECTED_ROUND_COLUMNS or not required.issubset(objects):
        raise SchemaVersionError("The database schema does not match this application version.")
    try:
        versions = [row[0] for row in connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        )]
    except sqlite3.Error:
        raise SchemaVersionError("The database migration history is invalid.") from None
    if versions != [SCHEMA_VERSION]:
        raise SchemaVersionError("The database migration history is invalid.")


def initialize_schema(connection: sqlite3.Connection) -> int:
    """Upgrade an empty Phase 1 database atomically; never delete existing data.

    Repeated calls are safe. Refuse unrelated and future-version databases.
    BEGIN IMMEDIATE serializes concurrent initialization and write operations.
    """
    if connection.in_transaction:
        raise RuntimeError("Schema initialization requires an idle connection.")
    connection.execute("BEGIN IMMEDIATE")
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            if tables:
                raise SchemaVersionError("An unversioned database with existing tables cannot be migrated automatically.")
            for statement in MIGRATION_STATEMENTS:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations (version, description, applied_at) VALUES (?, ?, ?)",
                (SCHEMA_VERSION, "Phase 2 completed-round storage", timestamp_text(datetime.now(UTC))),
            )
            connection.execute("PRAGMA user_version = 1")
        elif version != SCHEMA_VERSION:
            raise SchemaVersionError("The database version is not supported by this application.")
        _verify_schema(connection)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    logger.debug("Database schema version %s is ready.", SCHEMA_VERSION)
    return SCHEMA_VERSION
