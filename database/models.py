"""Validated completed-round records. This module creates no game data."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import json
import math
from types import MappingProxyType
from typing import Mapping


AUTHENTICATION_KEYS = frozenset({
    "password", "passwd", "authorization", "cookie", "cookies", "accesstoken",
    "refreshtoken", "sessiontoken", "apikey", "clientsecret", "authtoken",
})


def utc_datetime(value: datetime, name: str) -> datetime:
    """Require an explicit timezone and normalize to UTC."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime.")
    return value.astimezone(UTC)


def timestamp_text(value: datetime) -> str:
    """Use fixed-width UTC text so SQLite sorts timestamps chronologically."""
    return utc_datetime(value, "timestamp").isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def identifier(value: str, name: str) -> str:
    """Validate identifiers without inventing a provider-specific ID format."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string.")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{name} must not contain control characters.")
    return value.strip()


def _freeze_json(value: object) -> object:
    """Copy JSON recursively, reject authentication fields, and freeze containers."""
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings.")
            normalized_key = "".join(character for character in key.lower() if character.isalnum())
            if normalized_key in AUTHENTICATION_KEYS:
                raise ValueError("Authentication fields must be removed before storing round data.")
            result[key] = _freeze_json(item)
        return MappingProxyType(result)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError("Round payloads must contain only finite JSON-compatible values.")


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def json_text(value: Mapping[str, object]) -> str:
    """Serialize a payload deterministically without JSON NaN or Infinity."""
    return json.dumps(_thaw_json(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True, slots=True)
class RoundRecord:
    """One completed round from an identified source.

    timestamp is the result/completion time, not the round start time.
    collection_timestamp is when this completed-round event was collected.
    Pre-round timing is an asserted observation, not proof of live collection.
    """

    round_id: str
    timestamp: datetime
    multiplier: Decimal | str | int | float
    source: str
    collection_timestamp: datetime
    started_at: datetime | None = None
    pre_round_observed_at: datetime | None = None
    pre_round_data: Mapping[str, object] = field(default_factory=dict)
    post_round_data: Mapping[str, object] = field(default_factory=dict)
    raw_data: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("round_id", "source"):
            object.__setattr__(self, name, identifier(getattr(self, name), name))
        for name in ("timestamp", "collection_timestamp"):
            object.__setattr__(self, name, utc_datetime(getattr(self, name), name))
        for name in ("started_at", "pre_round_observed_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, utc_datetime(value, name))

        value = self.multiplier
        if isinstance(value, bool) or not isinstance(value, (Decimal, str, int, float)):
            raise ValueError("multiplier must be a finite decimal number of at least 1.")
        try:
            multiplier = Decimal(str(value))
        except InvalidOperation:
            raise ValueError("multiplier must be a finite decimal number of at least 1.") from None
        if not multiplier.is_finite() or multiplier < 1:
            raise ValueError("multiplier must be a finite decimal number of at least 1.")
        # Bound serialized size before formatting, without assuming a game maximum.
        if multiplier.adjusted() > 255 or multiplier.as_tuple().exponent < -255:
            raise ValueError("multiplier exceeds the supported decimal storage precision.")
        text = format(multiplier, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        if len(text) > 256:
            raise ValueError("multiplier exceeds the supported decimal storage precision.")
        object.__setattr__(self, "multiplier", Decimal(text))

        for name in ("pre_round_data", "post_round_data", "raw_data"):
            payload = getattr(self, name)
            if not isinstance(payload, Mapping):
                raise ValueError(f"{name} must be a JSON object.")
            try:
                frozen = _freeze_json(payload)
                json_text(frozen)
            except RecursionError:
                raise ValueError("Round payloads must not contain cyclic or excessively nested data.") from None
            object.__setattr__(self, name, frozen)

        if self.collection_timestamp < self.timestamp:
            raise ValueError("collection_timestamp cannot precede the completed result.")
        if self.started_at is not None and self.started_at > self.timestamp:
            raise ValueError("started_at cannot follow the completed result.")
        if self.pre_round_data and self.pre_round_observed_at is None:
            raise ValueError("Pre-round data requires an observation time.")
        if self.pre_round_observed_at is not None:
            if self.started_at is None or self.pre_round_observed_at >= self.started_at:
                raise ValueError("Pre-round observations must strictly precede a known round start.")

    @property
    def multiplier_text(self) -> str:
        """Return an exact decimal value; preserve original formatting in raw_data."""
        text = format(self.multiplier, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text


@dataclass(frozen=True, slots=True)
class StoredRound:
    """A round plus the database-assigned identity and ingestion time."""

    id: int
    record: RoundRecord
    ingested_at: datetime
