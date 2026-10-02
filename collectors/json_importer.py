"""Import explicitly supplied local JSON; never connect to a game or create data."""

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import logging
from pathlib import Path

from config.settings import Settings, ensure_directories
from database.database import connect_database
from database.migrations import initialize_schema
from database.models import RoundRecord
from database.repository import RoundRepository


FORMAT_VERSION = 1
MAX_IMPORT_BYTES = 10 * 1024 * 1024
MAX_IMPORT_ROUNDS = 10_000
REQUIRED_FIELDS = frozenset({
    "source", "round_id", "timestamp", "multiplier", "collection_timestamp",
})
OPTIONAL_FIELDS = frozenset({
    "started_at", "pre_round_observed_at", "pre_round_data", "post_round_data", "raw_data",
})
logger = logging.getLogger("aie.collectors.json_importer")


class ImportValidationError(ValueError):
    """A fixed diagnostic code and optional 1-based row number, without input values."""

    def __init__(self, code: str, row_number: int | None = None) -> None:
        self.code = code
        self.row_number = row_number
        message = code if row_number is None else f"{code} at round {row_number}"
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ValidatedImport:
    """Validated records and the fingerprint of the exact input bytes."""

    records: tuple[RoundRecord, ...]
    sha256: str


@dataclass(frozen=True, slots=True)
class ImportReport:
    """Counts refer to a completed import; dry runs do not query the database."""

    rows_read: int
    sha256: str
    dry_run: bool
    inserted: int | None = None
    duplicates: int | None = None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ImportValidationError("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ImportValidationError("NONFINITE_JSON_NUMBER")


def _parse_round(value: object, row_number: int) -> RoundRecord:
    if not isinstance(value, dict):
        raise ImportValidationError("ROUND_MUST_BE_OBJECT", row_number)
    if not REQUIRED_FIELDS.issubset(value):
        raise ImportValidationError("MISSING_ROUND_FIELD", row_number)
    if not value.keys() <= REQUIRED_FIELDS | OPTIONAL_FIELDS:
        raise ImportValidationError("UNKNOWN_ROUND_FIELD", row_number)
    # Require source decimal text, avoiding precision loss in JSON float decoding.
    if not isinstance(value["multiplier"], str):
        raise ImportValidationError("MULTIPLIER_MUST_BE_STRING", row_number)
    fields = dict(value)
    try:
        for name in ("timestamp", "collection_timestamp", "started_at", "pre_round_observed_at"):
            if name not in fields or (name in OPTIONAL_FIELDS and fields[name] is None):
                continue
            if not isinstance(fields[name], str):
                raise ValueError("Timestamp text required.")
            fields[name] = datetime.fromisoformat(fields[name])
        record = RoundRecord(**fields)
        # Escaped, unpaired Unicode surrogates cannot be bound as SQLite identifiers.
        record.source.encode("utf-8")
        record.round_id.encode("utf-8")
    except (ValueError, TypeError, OverflowError):
        raise ImportValidationError("INVALID_ROUND", row_number) from None
    return record


def load_json_rounds(path: Path) -> ValidatedImport:
    """Read a bounded UTF-8 JSON file and validate every completed round.

    Accept an optional UTF-8 BOM. Preserve the file unchanged. Duplicate keys,
    unknown fields, unsupported versions, and malformed records are rejected.
    """
    path = Path(path)
    if path.suffix.lower() != ".json":
        raise ImportValidationError("UNSUPPORTED_FILE_TYPE")
    try:
        if not path.is_file():
            raise ImportValidationError("FILE_NOT_FOUND_OR_NOT_REGULAR")
        if path.stat().st_size > MAX_IMPORT_BYTES:
            raise ImportValidationError("FILE_TOO_LARGE")
        with path.open("rb") as handle:
            content = handle.read(MAX_IMPORT_BYTES + 1)
    except OSError:
        raise ImportValidationError("FILE_NOT_READABLE") from None
    if len(content) > MAX_IMPORT_BYTES:
        raise ImportValidationError("FILE_TOO_LARGE")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ImportValidationError("INVALID_UTF8") from None
    try:
        document = json.loads(
            text, object_pairs_hook=_unique_object, parse_constant=_reject_constant,
        )
    except ImportValidationError:
        raise
    except (ValueError, RecursionError):
        raise ImportValidationError("INVALID_JSON") from None
    if not isinstance(document, dict) or set(document) != {"format_version", "rounds"}:
        raise ImportValidationError("INVALID_DOCUMENT_FIELDS")
    if type(document["format_version"]) is not int or document["format_version"] != FORMAT_VERSION:
        raise ImportValidationError("UNSUPPORTED_FORMAT_VERSION")
    rounds = document["rounds"]
    if not isinstance(rounds, list):
        raise ImportValidationError("ROUNDS_MUST_BE_ARRAY")
    if len(rounds) > MAX_IMPORT_ROUNDS:
        raise ImportValidationError("TOO_MANY_ROUNDS")
    records = tuple(_parse_round(value, index) for index, value in enumerate(rounds, start=1))
    return ValidatedImport(records, hashlib.sha256(content).hexdigest())


def import_json_file(path: Path, settings: Settings, *, dry_run: bool = False) -> ImportReport:
    """Validate first, then store all records atomically unless this is a dry run.

    Dry runs check file structure and record values only: no schema changes,
    inserts, database duplicate checks, or database connection are performed.
    """
    if type(dry_run) is not bool:
        raise TypeError("dry_run must be a boolean.")
    validated = load_json_rounds(path)
    if dry_run:
        return ImportReport(len(validated.records), validated.sha256, True)
    ensure_directories(settings)
    with closing(connect_database(settings)) as connection:
        initialize_schema(connection)
        result = RoundRepository(connection).insert_many(validated.records)
    logger.info("JSON import committed: %s inserted, %s duplicates.", result.inserted, result.duplicates)
    return ImportReport(
        len(validated.records), validated.sha256, False, result.inserted, result.duplicates,
    )
