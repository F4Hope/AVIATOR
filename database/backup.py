"""Publish verified SQLite snapshots without replacing the application database."""

from contextlib import closing
from dataclasses import dataclass, replace
from datetime import UTC, datetime
import hashlib
import logging
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time

from config.settings import Settings
from database.database import connect_database_readonly
from database.migrations import verify_schema
from database.models import timestamp_text
from database.records import StoredRoundDataError, stored_round_document
from utils.files import plain_filename, publish_file


BACKUP_TIMEOUT_SECONDS = 30.0
BACKUP_PAGES_PER_STEP = 256
BACKUP_SUFFIXES = (".db", ".sqlite", ".sqlite3")
logger = logging.getLogger("aie.database.backup")


class BackupVerificationError(ValueError):
    """The file does not pass the backup checks; never echo stored values."""


class BackupTimeoutError(TimeoutError):
    """A backup or verification exceeded its bounded processing time."""


@dataclass(frozen=True, slots=True)
class BackupReport:
    path: Path
    schema_version: int
    rounds_stored: int
    byte_count: int
    sha256: str


def _check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise BackupTimeoutError("Backup processing exceeded its time limit.")


def _verify_file(settings: Settings, path: Path, deadline: float) -> BackupReport:
    configured = replace(settings, database_path=path, database_dir=path.parent)
    with closing(connect_database_readonly(configured)) as connection:
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        connection.execute("BEGIN")
        try:
            version = verify_schema(connection)
            if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "delete":
                raise BackupVerificationError("The backup must be a standalone database in DELETE journal mode.")
            integrity = connection.execute("PRAGMA integrity_check").fetchmany(2)
            if len(integrity) != 1 or integrity[0][0] != "ok":
                raise BackupVerificationError("The backup did not pass SQLite integrity checks.")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise BackupVerificationError("The backup did not pass SQLite foreign-key checks.")
            count = connection.execute("SELECT COUNT(*) FROM rounds").fetchone()[0]
            validated = 0
            for row in connection.execute("SELECT * FROM rounds"):
                _check_deadline(deadline)
                try:
                    stored_round_document(row)
                    if type(row["id"]) is not int or row["id"] <= 0 or timestamp_text(datetime.fromisoformat(row["ingested_at"])) != row["ingested_at"]:
                        raise ValueError("Stored database metadata is invalid.")
                except (StoredRoundDataError, ValueError, TypeError, OverflowError):
                    raise BackupVerificationError("Stored round data failed backup validation.") from None
                validated += 1
            if validated != count:
                raise BackupVerificationError("The backup records could not be read completely.")
            digest = hashlib.sha256()
            size = 0
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    _check_deadline(deadline)
                    digest.update(chunk)
                    size += len(chunk)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    _check_deadline(deadline)
    return BackupReport(path, version, count, size, digest.hexdigest())


def verify_backup(settings: Settings, filename: str, *, expected_sha256: str | None = None) -> BackupReport:
    """Verify one named local backup read-only; an optional digest checks exact bytes."""
    filename = plain_filename(filename, BACKUP_SUFFIXES)
    if expected_sha256 is not None and (not isinstance(expected_sha256, str) or re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256) is None):
        raise ValueError("Expected SHA256 must contain exactly 64 hexadecimal characters.")
    path = settings.backup_dir / filename
    if path.is_symlink() or not path.is_file():
        raise BackupVerificationError("The backup must be an existing regular file, not a symbolic link.")
    report = _verify_file(settings, path, time.monotonic() + BACKUP_TIMEOUT_SECONDS)
    if expected_sha256 is not None and report.sha256 != expected_sha256.lower():
        raise BackupVerificationError("The backup does not match the expected SHA256.")
    logger.info("Backup verification complete: %s rounds.", report.rounds_stored)
    return report


def create_backup(settings: Settings, filename: str | None = None) -> BackupReport:
    """Copy a pinned read-only SQLite snapshot, verify, and publish without overwrite.

    Backups retain database IDs, ingestion times, migration history, indexes,
    triggers, and payloads. They are never restored automatically. The backup
    API handles SQLite journaling; a filesystem copy of the live file is not used.
    """
    if filename is None:
        filename = "aie-backup-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + ".sqlite3"
    filename = plain_filename(filename, BACKUP_SUFFIXES)
    target = settings.backup_dir / filename
    if target.exists() or target.is_symlink():
        raise FileExistsError("The backup already exists; choose a different filename.")
    deadline = time.monotonic() + BACKUP_TIMEOUT_SECONDS
    temporary: Path | None = None
    try:
        with closing(connect_database_readonly(settings)) as source:
            source.execute("BEGIN")
            try:
                version = verify_schema(source)
                count = source.execute("SELECT COUNT(*) FROM rounds").fetchone()[0]
                settings.backup_dir.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=settings.backup_dir, prefix=".aie-backup-", suffix=".sqlite3", delete=False) as handle:
                    temporary = Path(handle.name)
                with closing(sqlite3.connect(temporary, timeout=5, isolation_level=None, autocommit=sqlite3.LEGACY_TRANSACTION_CONTROL)) as destination:
                    def progress(status: int, remaining: int, total: int) -> None:
                        _check_deadline(deadline)
                    source.backup(destination, pages=BACKUP_PAGES_PER_STEP, progress=progress, sleep=0.05)
                    if destination.execute("PRAGMA journal_mode = DELETE").fetchone()[0].lower() != "delete":
                        raise BackupVerificationError("The backup could not be made standalone.")
                source.commit()
            except BaseException:
                source.rollback()
                raise
        report = _verify_file(settings, temporary, deadline)
        if report.schema_version != version or report.rounds_stored != count:
            raise BackupVerificationError("The backup did not match the source snapshot.")
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        _check_deadline(deadline)
        publish_file(temporary, target)
        logger.info("Verified database backup created: %s rounds.", count)
        return replace(report, path=target)
    finally:
        if temporary is not None:
            for suffix in ("", "-journal", "-wal", "-shm"):
                Path(str(temporary) + suffix).unlink(missing_ok=True)
