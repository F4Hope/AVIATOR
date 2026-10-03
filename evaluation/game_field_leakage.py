"""Audit client-visible game fields for pre-round information disclosure."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from decimal import Decimal
import json
from pathlib import Path
from statistics import median
from typing import Iterable, Sequence

from prediction.round_state import InferredRoundStart


@dataclass(frozen=True, slots=True)
class GameFieldObservation:
    observed_at: str
    timestamp: float
    path: str
    kind: str
    numeric_value: float | None
    identifier_hash: str | None
    direction: str
    url: str


@dataclass(frozen=True, slots=True)
class NumericLeakageStat:
    path: str
    rounds_observed: int
    any_exact_matches: int
    last_exact_matches: int
    any_match_rate: float
    last_match_rate: float
    median_last_lead_seconds: float
    minimum_last_lead_seconds: float
    distinct_values: int


@dataclass(frozen=True, slots=True)
class IdentifierExposureStat:
    path: str
    rounds_observed: int
    unique_hashes: int


@dataclass(frozen=True, slots=True)
class GameFieldLeakageAudit:
    eligible_rounds: int
    numeric_observations: int
    identifier_observations: int
    numeric_stats: tuple[NumericLeakageStat, ...]
    identifier_stats: tuple[IdentifierExposureStat, ...]


def _timestamp(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


def load_game_field_observations(
    paths: Iterable[Path],
) -> tuple[GameFieldObservation, ...]:
    """Load sanitized server-to-client game fields from current/archive probes."""
    result: list[GameFieldObservation] = []
    seen: set[tuple[object, ...]] = set()

    for path in paths:
        if not path.is_file():
            continue
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue

                event_kind = event.get("kind")
                if event_kind not in {
                    "cdp_websocket_received",
                    "websocket_received",
                    "cdp_eventsource_received",
                    "response",
                }:
                    continue

                timestamp = _timestamp(event.get("observed_at"))
                payload = event.get("payload")
                if timestamp is None or not isinstance(payload, dict):
                    continue
                raw_fields = payload.get("game_fields")
                if not isinstance(raw_fields, list):
                    continue

                url = event.get("url")
                safe_url = url if isinstance(url, str) else ""
                direction = "received"

                for field in raw_fields[:64]:
                    if not isinstance(field, dict):
                        continue
                    field_path = field.get("path")
                    field_kind = field.get("kind")
                    if not isinstance(field_path, str) or not isinstance(
                        field_kind, str
                    ):
                        continue

                    numeric_value: float | None = None
                    identifier_hash: str | None = None
                    if field_kind == "numeric":
                        raw_value = field.get("value")
                        if isinstance(raw_value, (int, float)) and not isinstance(
                            raw_value, bool
                        ):
                            numeric_value = float(raw_value)
                        else:
                            continue
                    elif field_kind == "identifier_hash":
                        raw_hash = field.get("sha256_24")
                        if not isinstance(raw_hash, str):
                            continue
                        identifier_hash = raw_hash
                    else:
                        continue

                    identity = (
                        event.get("observed_at"),
                        field_path,
                        field_kind,
                        numeric_value,
                        identifier_hash,
                        safe_url,
                    )
                    if identity in seen:
                        continue
                    seen.add(identity)
                    result.append(
                        GameFieldObservation(
                            observed_at=str(event.get("observed_at")),
                            timestamp=timestamp,
                            path=field_path,
                            kind=field_kind,
                            numeric_value=numeric_value,
                            identifier_hash=identifier_hash,
                            direction=direction,
                            url=safe_url,
                        )
                    )

    result.sort(key=lambda item: item.timestamp)
    return tuple(result)


def audit_pre_round_game_fields(
    completed: Sequence[tuple[str, Decimal]],
    starts: Sequence[InferredRoundStart],
    observations: Sequence[GameFieldObservation],
    *,
    safety_margin_seconds: float = 0.50,
    exact_tolerance: float = 0.001,
) -> GameFieldLeakageAudit:
    """Measure whether client-visible fields disclose the next multiplier pre-start.

    The audit uses only server-to-client observations after the previous completed
    result and before the inferred next-round start minus a safety margin.
    """
    if safety_margin_seconds <= 0:
        raise ValueError("safety_margin_seconds must be positive.")
    if exact_tolerance < 0:
        raise ValueError("exact_tolerance cannot be negative.")

    start_by_target = {
        item.target_completed_at: item
        for item in starts
    }
    event_times = [item.timestamp for item in observations]

    numeric_rounds: dict[str, int] = {}
    numeric_any_matches: dict[str, int] = {}
    numeric_last_matches: dict[str, int] = {}
    numeric_leads: dict[str, list[float]] = {}
    numeric_values: dict[str, set[float]] = {}

    identifier_rounds: dict[str, int] = {}
    identifier_values: dict[str, set[str]] = {}

    eligible_rounds = 0

    for previous, target in zip(completed, completed[1:]):
        previous_at, _ = previous
        target_at, target_multiplier = target
        start = start_by_target.get(target_at)
        if start is None or start.previous_completed_at != previous_at:
            continue

        previous_ts = _timestamp(previous_at)
        start_ts = _timestamp(start.start_observed_at)
        if previous_ts is None or start_ts is None:
            continue
        cutoff = start_ts - safety_margin_seconds
        if cutoff <= previous_ts:
            continue

        left = bisect_right(event_times, previous_ts)
        right = bisect_right(event_times, cutoff)
        interval = observations[left:right]
        eligible_rounds += 1

        numeric_by_path: dict[str, list[GameFieldObservation]] = {}
        identifiers_by_path: dict[str, list[GameFieldObservation]] = {}
        for observation in interval:
            if observation.kind == "numeric" and observation.numeric_value is not None:
                numeric_by_path.setdefault(observation.path, []).append(observation)
            elif (
                observation.kind == "identifier_hash"
                and observation.identifier_hash is not None
            ):
                identifiers_by_path.setdefault(observation.path, []).append(observation)

        target_value = float(target_multiplier)
        for path, values in numeric_by_path.items():
            numeric_rounds[path] = numeric_rounds.get(path, 0) + 1
            numeric_values.setdefault(path, set()).update(
                item.numeric_value
                for item in values
                if item.numeric_value is not None
            )
            if any(
                item.numeric_value is not None
                and abs(item.numeric_value - target_value) <= exact_tolerance
                for item in values
            ):
                numeric_any_matches[path] = numeric_any_matches.get(path, 0) + 1

            last = values[-1]
            if (
                last.numeric_value is not None
                and abs(last.numeric_value - target_value) <= exact_tolerance
            ):
                numeric_last_matches[path] = numeric_last_matches.get(path, 0) + 1

            numeric_leads.setdefault(path, []).append(start_ts - last.timestamp)

        for path, values in identifiers_by_path.items():
            identifier_rounds[path] = identifier_rounds.get(path, 0) + 1
            identifier_values.setdefault(path, set()).update(
                item.identifier_hash
                for item in values
                if item.identifier_hash is not None
            )

    numeric_stats: list[NumericLeakageStat] = []
    for path, rounds_observed in numeric_rounds.items():
        leads = numeric_leads.get(path, ())
        any_matches = numeric_any_matches.get(path, 0)
        last_matches = numeric_last_matches.get(path, 0)
        numeric_stats.append(
            NumericLeakageStat(
                path=path,
                rounds_observed=rounds_observed,
                any_exact_matches=any_matches,
                last_exact_matches=last_matches,
                any_match_rate=round(any_matches / rounds_observed, 6),
                last_match_rate=round(last_matches / rounds_observed, 6),
                median_last_lead_seconds=round(float(median(leads)), 6)
                if leads
                else 0.0,
                minimum_last_lead_seconds=round(min(leads), 6)
                if leads
                else 0.0,
                distinct_values=len(numeric_values.get(path, ())),
            )
        )

    numeric_stats.sort(
        key=lambda item: (
            item.last_match_rate,
            item.any_match_rate,
            item.rounds_observed,
        ),
        reverse=True,
    )

    identifier_stats = [
        IdentifierExposureStat(
            path=path,
            rounds_observed=rounds_observed,
            unique_hashes=len(identifier_values.get(path, ())),
        )
        for path, rounds_observed in identifier_rounds.items()
    ]
    identifier_stats.sort(
        key=lambda item: (item.rounds_observed, item.unique_hashes),
        reverse=True,
    )

    return GameFieldLeakageAudit(
        eligible_rounds=eligible_rounds,
        numeric_observations=sum(
            observation.kind == "numeric"
            for observation in observations
        ),
        identifier_observations=sum(
            observation.kind == "identifier_hash"
            for observation in observations
        ),
        numeric_stats=tuple(numeric_stats),
        identifier_stats=tuple(identifier_stats),
    )
