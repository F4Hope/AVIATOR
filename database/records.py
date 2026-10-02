"""Validate stored round fields before portable export or backup verification."""

from datetime import datetime
import json
import sqlite3

from database.models import RoundRecord, json_text, timestamp_text
from database.repository import RECORD_COLUMNS


class StoredRoundDataError(ValueError):
    """A stored record fails validation; the message contains no record values."""


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Stored JSON contains duplicate keys.")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError("Stored JSON contains a nonfinite number.")


def stored_round_document(row: sqlite3.Row, *, max_bytes: int | None = None) -> dict[str, object]:
    """Validate complete stored data and reject information-changing normalization.

    Export callers may bound an individual stored row before JSON parsing.
    Backup callers have no import-format size limit. Database-assigned metadata
    is excluded from the returned portable round document.
    """
    try:
        if max_bytes is not None and sum(len(row[name].encode("utf-8")) for name in RECORD_COLUMNS if row[name] is not None) > max_bytes:
            raise ValueError("A stored record exceeds the permitted byte budget.")
        values = {name: row[name] for name in RECORD_COLUMNS}
        for name in ("timestamp", "collection_timestamp", "started_at", "pre_round_observed_at"):
            if values[name] is not None:
                values[name] = datetime.fromisoformat(values[name])
        for name in ("pre_round_data", "post_round_data", "raw_data"):
            values[name] = json.loads(values[name], object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        record = RoundRecord(**values)
        record.source.encode("utf-8")
        record.round_id.encode("utf-8")
        document: dict[str, object] = {
            "source": record.source, "round_id": record.round_id,
            "timestamp": timestamp_text(record.timestamp), "multiplier": record.multiplier_text,
            "collection_timestamp": timestamp_text(record.collection_timestamp),
            "started_at": timestamp_text(record.started_at) if record.started_at else None,
            "pre_round_observed_at": timestamp_text(record.pre_round_observed_at) if record.pre_round_observed_at else None,
            "pre_round_data": json.loads(json_text(record.pre_round_data)),
            "post_round_data": json.loads(json_text(record.post_round_data)),
            "raw_data": json.loads(json_text(record.raw_data)),
        }
        canonical = dict(document)
        for name in ("pre_round_data", "post_round_data", "raw_data"):
            canonical[name] = json_text(getattr(record, name))
        if any(canonical[name] != row[name] for name in RECORD_COLUMNS):
            raise ValueError("Stored values are not canonical.")
    except (ValueError, TypeError, AttributeError, OverflowError, RecursionError):
        raise StoredRoundDataError("Stored round data failed validation.") from None
    return document
