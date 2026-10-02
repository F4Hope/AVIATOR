"""Parameterized, append-only persistence for validated completed rounds."""

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import logging
import sqlite3
from typing import Iterable

from database.models import RoundRecord, StoredRound, identifier, json_text, timestamp_text


logger = logging.getLogger("aie.database.repository")
RECORD_COLUMNS = (
    "source", "round_id", "timestamp", "multiplier", "collection_timestamp",
    "started_at", "pre_round_observed_at", "pre_round_data", "post_round_data", "raw_data",
)


class DuplicateRoundError(ValueError):
    """The same source and round ID already have a different stored record."""


@dataclass(frozen=True, slots=True)
class BatchInsertResult:
    """Counts from a successfully committed batch, including exact replays."""

    inserted: int
    duplicates: int


def _record_values(record: RoundRecord) -> tuple[object, ...]:
    return (
        record.source, record.round_id, timestamp_text(record.timestamp), record.multiplier_text,
        timestamp_text(record.collection_timestamp),
        timestamp_text(record.started_at) if record.started_at is not None else None,
        timestamp_text(record.pre_round_observed_at) if record.pre_round_observed_at is not None else None,
        json_text(record.pre_round_data), json_text(record.post_round_data), json_text(record.raw_data),
    )


def _stored_round(row: sqlite3.Row) -> StoredRound:
    return StoredRound(
        id=row["id"],
        ingested_at=datetime.fromisoformat(row["ingested_at"]),
        record=RoundRecord(
            source=row["source"], round_id=row["round_id"],
            timestamp=datetime.fromisoformat(row["timestamp"]), multiplier=row["multiplier"],
            collection_timestamp=datetime.fromisoformat(row["collection_timestamp"]),
            started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
            pre_round_observed_at=datetime.fromisoformat(row["pre_round_observed_at"])
            if row["pre_round_observed_at"] else None,
            pre_round_data=json.loads(row["pre_round_data"]),
            post_round_data=json.loads(row["post_round_data"]), raw_data=json.loads(row["raw_data"]),
        ),
    )


class RoundRepository:
    """Use a schema-initialized connection; the caller remains responsible for closing it."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def insert(self, record: RoundRecord) -> bool:
        """Return True for a new record, False for an exact replay.

        Conflicting duplicates raise DuplicateRoundError and never overwrite.
        A repeat collected at a different time is a distinct observation and
        must not silently replace the original completed-round record.
        """
        return self.insert_many((record,)).inserted == 1

    def insert_many(self, records: Iterable[RoundRecord]) -> BatchInsertResult:
        """Commit a whole batch or roll it back if any record conflicts.

        Validate and serialize the iterable before opening a transaction. An
        invalid item or failed iterator therefore cannot leave partial writes.
        Repeated identical records within the batch are counted as replays.
        """
        if self.connection.in_transaction:
            raise RuntimeError("Round insertion requires an idle connection.")
        prepared: list[tuple[object, ...]] = []
        for record in records:
            if not isinstance(record, RoundRecord):
                raise TypeError("insert requires a validated RoundRecord.")
            prepared.append(_record_values(record))
        if not prepared:
            return BatchInsertResult(0, 0)

        self.connection.execute("BEGIN IMMEDIATE")
        inserted = duplicates = 0
        try:
            for values in prepared:
                existing = self.connection.execute(
                    "SELECT * FROM rounds WHERE source = ? AND round_id = ?", values[:2],
                ).fetchone()
                if existing is not None:
                    if tuple(existing[column] for column in RECORD_COLUMNS) != values:
                        raise DuplicateRoundError("A conflicting record exists for this source and round ID.")
                    duplicates += 1
                    continue
                self.connection.execute(
                    """INSERT INTO rounds (
                        source, round_id, timestamp, multiplier, collection_timestamp,
                        started_at, pre_round_observed_at, pre_round_data, post_round_data,
                        raw_data, ingested_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (*values, timestamp_text(datetime.now(UTC))),
                )
                inserted += 1
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        logger.debug("Round batch committed: %s inserted, %s duplicates.", inserted, duplicates)
        return BatchInsertResult(inserted, duplicates)

    def get(self, source: str, round_id: str) -> StoredRound | None:
        """Retrieve one round using its source-scoped identity."""
        row = self.connection.execute(
            "SELECT * FROM rounds WHERE source = ? AND round_id = ?",
            (identifier(source, "source"), identifier(round_id, "round_id")),
        ).fetchone()
        return _stored_round(row) if row is not None else None

    def count(self, source: str | None = None) -> int:
        if source is None:
            return self.connection.execute("SELECT COUNT(*) FROM rounds").fetchone()[0]
        return self.connection.execute(
            "SELECT COUNT(*) FROM rounds WHERE source = ?", (identifier(source, "source"),)
        ).fetchone()[0]

    def list_rounds(
        self, *, limit: int = 100, offset: int = 0, source: str | None = None,
        start: datetime | None = None, end: datetime | None = None,
    ) -> list[StoredRound]:
        """Return oldest-first records, with optional UTC interval [start, end).

        Timestamp ties use source and round ID as deterministic tie-breakers.
        Ordering is by completed-result time, never by lexicographic round IDs.
        """
        if type(limit) is not int or not 1 <= limit <= 10_000:
            raise ValueError("limit must be an integer between 1 and 10000.")
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a nonnegative integer.")
        clauses: list[str] = []
        parameters: list[object] = []
        if source is not None:
            clauses.append("source = ?")
            parameters.append(identifier(source, "source"))
        if start is not None:
            clauses.append("timestamp >= ?")
            parameters.append(timestamp_text(start))
        if end is not None:
            clauses.append("timestamp < ?")
            parameters.append(timestamp_text(end))
        if start is not None and end is not None and timestamp_text(start) >= timestamp_text(end):
            raise ValueError("start must precede end.")
        # SQL fragments are fixed application strings; every external value is bound.
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            "SELECT * FROM rounds" + where
            + " ORDER BY timestamp ASC, source ASC, round_id ASC LIMIT ? OFFSET ?",
            (*parameters, limit, offset),
        ).fetchall()
        return [_stored_round(row) for row in rows]
