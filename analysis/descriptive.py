"""Descriptive historical statistics from one read-only SQLite snapshot."""

from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
import logging
import sqlite3

from config.settings import Settings
from database.database import connect_database_readonly
from database.migrations import verify_schema
from database.models import RoundRecord, identifier, timestamp_text, utc_datetime


MAX_ANALYSIS_ROUNDS = 100_000
ARITHMETIC_PRECISION = 600
MEAN_DECIMAL_PLACES = 6
PERCENTAGE_DECIMAL_PLACES = 2
logger = logging.getLogger("aie.analysis.descriptive")


class AnalysisDataError(ValueError):
    """Stored records cannot be analyzed without discarding or altering data."""


@dataclass(frozen=True, slots=True)
class AnalysisFilters:
    """An optional source and UTC completion-time interval [start, end)."""

    source: str | None = None
    start: datetime | None = None
    end: datetime | None = None

    def __post_init__(self) -> None:
        if self.source is not None:
            object.__setattr__(self, "source", identifier(self.source, "source"))
        for name in ("start", "end"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, utc_datetime(value, name))
        if self.start is not None and self.end is not None and self.start >= self.end:
            raise ValueError("start must precede end.")


@dataclass(frozen=True, slots=True)
class NumericSummary:
    minimum: Decimal | None
    maximum: Decimal | None
    mean: Decimal | None
    median: Decimal | None


@dataclass(frozen=True, slots=True)
class MultiplierBucket:
    lower_inclusive: Decimal
    upper_exclusive: Decimal | None
    count: int
    percentage: Decimal | None


@dataclass(frozen=True, slots=True)
class DataQuality:
    rows_with_known_start: int
    rows_with_pre_observation: int
    rows_with_pre_data: int
    rows_with_post_data: int
    rows_with_raw_data: int
    repeated_result_timestamps: int
    collection_delay_seconds: NumericSummary


@dataclass(frozen=True, slots=True)
class AnalysisReport:
    generated_at: datetime
    schema_version: int
    filters: AnalysisFilters
    total_stored: int
    selected_rounds: int
    source_count: int
    first_result_at: datetime | None
    last_result_at: datetime | None
    multipliers: NumericSummary
    buckets: tuple[MultiplierBucket, ...]
    quality: DataQuality

    @property
    def status(self) -> str:
        return "COMPLETE" if self.selected_rounds else "NO_DATA"


def summarize_numbers(values: list[Decimal]) -> NumericSummary:
    """Keep exact extrema/median; round the mean to six decimals, half even.

    The local precision covers aligned 256-character stored multipliers and
    batches up to MAX_ANALYSIS_ROUNDS. The caller's decimal context is untouched.
    """
    if not values:
        return NumericSummary(None, None, None, None)
    ordered = sorted(values)
    with localcontext(Context(prec=ARITHMETIC_PRECISION, rounding=ROUND_HALF_EVEN)):
        size = len(ordered)
        midpoint = size // 2
        median = ordered[midpoint] if size % 2 else (ordered[midpoint - 1] + ordered[midpoint]) / 2
        mean = (sum(ordered) / size).quantize(Decimal("0.000001"))
    return NumericSummary(ordered[0], ordered[-1], mean, median)


def _stored_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if timestamp_text(parsed) != value:
        raise ValueError("Stored timestamps must use canonical UTC text.")
    return parsed


def _validated_record(row: sqlite3.Row) -> RoundRecord:
    """Validate stored scalars and timing without exposing stored payloads."""
    try:
        if any(row[name] != "object" for name in ("pre_type", "post_type", "raw_type")):
            raise ValueError("Stored payloads must be JSON objects.")
        record = RoundRecord(
            source=row["source"], round_id=row["round_id"], multiplier=row["multiplier"],
            timestamp=_stored_datetime(row["timestamp"]),
            collection_timestamp=_stored_datetime(row["collection_timestamp"]),
            started_at=_stored_datetime(row["started_at"]),
            pre_round_observed_at=_stored_datetime(row["pre_round_observed_at"]),
        )
        if row["has_pre_data"] and record.pre_round_observed_at is None:
            raise ValueError("Pre-round data requires an observation time.")
        if record.source != row["source"] or record.round_id != row["round_id"]:
            raise ValueError("Stored identities must use canonical text.")
    except (ValueError, TypeError, OverflowError):
        raise AnalysisDataError("Stored round data failed validation.") from None
    return record


def analyze_database(settings: Settings, filters: AnalysisFilters | None = None) -> AnalysisReport:
    """Analyze the whole selection or fail; never truncate or mutate the database."""
    active_filters = filters if filters is not None else AnalysisFilters()
    if not isinstance(active_filters, AnalysisFilters):
        raise TypeError("filters must be AnalysisFilters.")
    clauses: list[str] = []
    parameters: list[object] = []
    if active_filters.source is not None:
        clauses.append("source = ?")
        parameters.append(active_filters.source)
    if active_filters.start is not None:
        clauses.append("timestamp >= ?")
        parameters.append(timestamp_text(active_filters.start))
    if active_filters.end is not None:
        clauses.append("timestamp < ?")
        parameters.append(timestamp_text(active_filters.end))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    multipliers: list[Decimal] = []
    delays: list[Decimal] = []
    sources: set[str] = set()
    observed_times: set[tuple[str, datetime]] = set()
    coverage = [0, 0, 0, 0, 0]
    bucket_counts = [0, 0, 0, 0]
    repeated = 0
    first = last = None

    with closing(connect_database_readonly(settings)) as connection:
        connection.execute("BEGIN")
        try:
            schema_version = verify_schema(connection)
            total_stored = connection.execute("SELECT COUNT(*) FROM rounds").fetchone()[0]
            selected = connection.execute("SELECT COUNT(*) FROM rounds" + where, parameters).fetchone()[0]
            if selected > MAX_ANALYSIS_ROUNDS:
                raise AnalysisDataError("The selection exceeds the analysis limit; narrow the filters.")
            rows = connection.execute(
                """SELECT source, round_id, timestamp, multiplier, collection_timestamp,
                    started_at, pre_round_observed_at,
                    json_type(pre_round_data) AS pre_type,
                    json_type(post_round_data) AS post_type,
                    json_type(raw_data) AS raw_type,
                    json(pre_round_data) <> '{}' AS has_pre_data,
                    json(post_round_data) <> '{}' AS has_post_data,
                    json(raw_data) <> '{}' AS has_raw_data
                    FROM rounds""" + where + " ORDER BY timestamp, source, round_id", parameters,
            )
            for row in rows:
                record = _validated_record(row)
                multipliers.append(record.multiplier)
                delta = record.collection_timestamp - record.timestamp
                microseconds = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
                # Construct exact decimal seconds without the caller's arithmetic context.
                delays.append(Decimal(f"{microseconds // 1_000_000}.{microseconds % 1_000_000:06d}"))
                sources.add(record.source)
                key = (record.source, record.timestamp)
                repeated += key in observed_times
                observed_times.add(key)
                first = record.timestamp if first is None else first
                last = record.timestamp
                flags = (
                    record.started_at is not None, record.pre_round_observed_at is not None,
                    row["has_pre_data"], row["has_post_data"], row["has_raw_data"],
                )
                for index, present in enumerate(flags):
                    coverage[index] += bool(present)
                index = 3 if record.multiplier >= 10 else 2 if record.multiplier >= 5 else 1 if record.multiplier >= 2 else 0
                bucket_counts[index] += 1
            if len(multipliers) != selected:
                raise AnalysisDataError("The database selection could not be read completely.")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    bounds = (("1", "2"), ("2", "5"), ("5", "10"), ("10", None))
    with localcontext(Context(prec=ARITHMETIC_PRECISION, rounding=ROUND_HALF_EVEN)):
        buckets = tuple(
            MultiplierBucket(
                Decimal(lower), Decimal(upper) if upper is not None else None, count,
                (Decimal(count) * 100 / selected).quantize(Decimal("0.01")) if selected else None,
            )
            for (lower, upper), count in zip(bounds, bucket_counts)
        )
    report = AnalysisReport(
        datetime.now(UTC), schema_version, active_filters, total_stored, selected, len(sources),
        first, last, summarize_numbers(multipliers), buckets,
        DataQuality(*coverage, repeated, summarize_numbers(delays)),
    )
    logger.info("Descriptive analysis complete: %s selected rounds.", selected)
    return report
