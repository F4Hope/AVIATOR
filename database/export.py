"""Export validated completed-round data from one read-only database snapshot."""

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import logging
from pathlib import Path
import sqlite3

from analysis.descriptive import AnalysisFilters
from collectors.json_importer import FORMAT_VERSION, MAX_IMPORT_BYTES, MAX_IMPORT_ROUNDS
from config.settings import Settings
from database.database import connect_database_readonly
from database.migrations import verify_schema
from database.models import timestamp_text
from database.records import StoredRoundDataError, stored_round_document
from utils.files import plain_filename, write_bytes


logger = logging.getLogger("aie.database.export")


class ExportDataError(ValueError):
    """Stored data or the selection cannot be exported without losing information."""


@dataclass(frozen=True, slots=True)
class ExportReport:
    path: Path
    total_stored: int
    exported_rounds: int
    byte_count: int
    sha256: str


def _record_document(row: sqlite3.Row) -> dict[str, object]:
    """Apply the import-file budget as well as complete stored-field validation."""
    try:
        return stored_round_document(row, max_bytes=MAX_IMPORT_BYTES)
    except StoredRoundDataError:
        raise ExportDataError("Stored round data failed export validation.") from None


def export_rounds(
    settings: Settings, filename: str = "rounds-export.json", filters: AnalysisFilters | None = None,
    *, overwrite: bool = False,
) -> ExportReport:
    """Export all selected records or fail; output must remain import-compatible.

    Source-scoped identities, exact multipliers, UTC times, and all sanitized
    payloads are retained. Database-assigned IDs and ingestion times are not
    part of the portable round format. No schema changes or database writes.
    """
    filename = plain_filename(filename, (".json",))
    if type(overwrite) is not bool:
        raise TypeError("overwrite must be a boolean.")
    active_filters = filters if filters is not None else AnalysisFilters()
    if not isinstance(active_filters, AnalysisFilters):
        raise TypeError("filters must be AnalysisFilters.")
    target = settings.processed_data_dir / filename
    if not overwrite and (target.exists() or target.is_symlink()):
        raise FileExistsError("The export already exists.")
    clauses: list[str] = []
    parameters: list[object] = []
    for name, value, operator in (
        ("source", active_filters.source, "="),
        ("timestamp", active_filters.start, ">="), ("timestamp", active_filters.end, "<"),
    ):
        if value is not None:
            clauses.append(f"{name} {operator} ?")  # Fixed field names and operators only.
            parameters.append(timestamp_text(value) if isinstance(value, datetime) else value)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    content = bytearray(f'{{\n  "format_version": {FORMAT_VERSION},\n  "rounds": [\n'.encode())
    closing_bytes = b"\n  ]\n}\n"
    with closing(connect_database_readonly(settings)) as connection:
        connection.execute("BEGIN")
        try:
            verify_schema(connection)
            total = connection.execute("SELECT COUNT(*) FROM rounds").fetchone()[0]
            selected = connection.execute("SELECT COUNT(*) FROM rounds" + where, parameters).fetchone()[0]
            if selected > MAX_IMPORT_ROUNDS:
                raise ExportDataError("The selection exceeds 10000 rounds; narrow the filters.")
            count = 0
            for row in connection.execute("SELECT * FROM rounds" + where + " ORDER BY timestamp, source, round_id", parameters):
                document = _record_document(row)
                rendered = json.dumps(document, indent=2, sort_keys=True, allow_nan=False)
                encoded = (",\n" if count else "").encode() + "\n".join("    " + line for line in rendered.splitlines()).encode("utf-8")
                if len(content) + len(encoded) + len(closing_bytes) > MAX_IMPORT_BYTES:
                    raise ExportDataError("The export exceeds 10 MiB; narrow the filters.")
                content.extend(encoded)
                count += 1
            if count != selected:
                raise ExportDataError("The selection could not be read completely.")
            content.extend(closing_bytes)
            if len(content) > MAX_IMPORT_BYTES:
                raise ExportDataError("The export exceeds the file-size limit.")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    published = write_bytes(settings.processed_data_dir, filename, bytes(content), overwrite=overwrite)
    logger.info("Round export complete: %s rounds.", count)
    return ExportReport(published, total, count, len(content), hashlib.sha256(content).hexdigest())
