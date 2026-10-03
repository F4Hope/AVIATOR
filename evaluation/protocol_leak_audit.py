"""Direct protocol-leak audit for sanitized game-integrity fields.

This module does not build a predictive model. It tests whether allow-listed
numeric game fields observed in HTTP/WS/EventSource metadata equal a later DOM
completed multiplier before that result is displayed.

Authentication/session/token fields are excluded upstream by network_probe.
Identifier-like fields are already irreversibly hashed before persistence.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from decimal import Decimal
import json
from pathlib import Path
from statistics import median
from typing import Iterable, Mapping, Sequence

from prediction.round_state import InferredRoundStart
from prediction.system_state import _parse_timestamp


OUTCOME_PATH_TERMS = (
    "nextmultiplier",
    "finalmultiplier",
    "resultmultiplier",
    "crashmultiplier",
    "crashpoint",
    "payoutmultiplier",
    "coefficient",
    "coef",
    "multiplier",
)


@dataclass(frozen=True, slots=True)
class GameFieldObservation:
    observed_at: str
    timestamp: float
    event_kind: str
    url: str
    payload_sha256: str
    path: str
    field_kind: str
    numeric_value: Decimal | None
    identifier_hash: str | None


@dataclass(frozen=True, slots=True)
class NumericPathLeakStat:
    path: str
    observations: int
    target_intervals_seen: int
    exact_match_targets: int
    exact_matches_ge_025s: int
    exact_matches_ge_1s: int
    exact_matches_ge_2s: int
    exact_matches_ge_5s: int
    exact_matches_before_inferred_start: int
    median_earliest_exact_lead_seconds: float | None
    maximum_exact_lead_seconds: float | None
    suspicious_outcome_path: bool


@dataclass(frozen=True, slots=True)
class IdentifierPathStat:
    path: str
    observations: int
    unique_hashes: int
    target_intervals_seen: int
    intervals_with_prestart_observation: int


@dataclass(frozen=True, slots=True)
class ExactLeakExample:
    path: str
    target_completed_at: str
    actual_multiplier: Decimal
    observed_at: str
    observed_value: Decimal
    lead_seconds: float
    before_inferred_start: bool
    event_kind: str
    url: str


@dataclass(frozen=True, slots=True)
class ProtocolLeakAudit:
    network_files: int
    completed_rounds: int
    inferred_starts: int
    structured_game_field_observations: int
    numeric_observations: int
    identifier_observations: int
    numeric_paths: tuple[NumericPathLeakStat, ...]
    identifier_paths: tuple[IdentifierPathStat, ...]
    exact_examples: tuple[ExactLeakExample, ...]


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except Exception:
        return None
    if not number.is_finite():
        return None
    return number


def _dedupe_key(
    event: Mapping[str, object],
    field: Mapping[str, object],
) -> tuple[object, ...]:
    payload = event.get("payload")
    payload_sha = payload.get("sha256") if isinstance(payload, Mapping) else None
    return (
        event.get("observed_at"),
        event.get("kind"),
        event.get("url"),
        payload_sha,
        field.get("path"),
        field.get("kind"),
        field.get("value"),
        field.get("sha256_24"),
    )


def load_game_field_observations(
    paths: Iterable[Path],
) -> tuple[GameFieldObservation, ...]:
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

                observed_at = event.get("observed_at")
                timestamp = _parse_timestamp(observed_at)
                payload = event.get("payload")
                if (
                    not isinstance(observed_at, str)
                    or timestamp is None
                    or not isinstance(payload, dict)
                ):
                    continue
                game_fields = payload.get("game_fields")
                if not isinstance(game_fields, list):
                    continue

                event_kind = event.get("kind")
                url = event.get("url")
                payload_sha = payload.get("sha256")
                if not isinstance(event_kind, str):
                    event_kind = "unknown"
                if not isinstance(url, str):
                    url = "unknown"
                if not isinstance(payload_sha, str):
                    payload_sha = ""

                for field in game_fields:
                    if not isinstance(field, dict):
                        continue
                    key = _dedupe_key(event, field)
                    if key in seen:
                        continue
                    seen.add(key)

                    field_path = field.get("path")
                    field_kind = field.get("kind")
                    if not isinstance(field_path, str) or not isinstance(field_kind, str):
                        continue

                    numeric_value = (
                        _decimal(field.get("value"))
                        if field_kind == "numeric"
                        else None
                    )
                    identifier_hash = (
                        field.get("sha256_24")
                        if field_kind == "identifier_hash"
                        and isinstance(field.get("sha256_24"), str)
                        else None
                    )
                    if numeric_value is None and identifier_hash is None:
                        continue

                    result.append(
                        GameFieldObservation(
                            observed_at=observed_at,
                            timestamp=timestamp,
                            event_kind=event_kind,
                            url=url,
                            payload_sha256=payload_sha,
                            path=field_path,
                            field_kind=field_kind,
                            numeric_value=numeric_value,
                            identifier_hash=identifier_hash,
                        )
                    )

    result.sort(key=lambda item: item.timestamp)
    return tuple(result)


def _exact_match(observed: Decimal, actual: Decimal) -> bool:
    tolerance = max(Decimal("0.001"), abs(actual) * Decimal("0.000001"))
    return abs(observed - actual) <= tolerance


def _suspicious_path(path: str) -> bool:
    normalized = "".join(character.lower() for character in path if character.isalnum())
    return any(term in normalized for term in OUTCOME_PATH_TERMS)


def audit_protocol_leakage(
    *,
    network_paths: Sequence[Path],
    completed_rounds: Sequence[tuple[str, Decimal]],
    inferred_starts: Sequence[InferredRoundStart],
    max_examples: int = 30,
) -> ProtocolLeakAudit:
    observations = load_game_field_observations(network_paths)
    numeric = tuple(
        item for item in observations if item.numeric_value is not None
    )
    identifiers = tuple(
        item for item in observations if item.identifier_hash is not None
    )
    starts_by_target = {
        item.target_completed_at: item
        for item in inferred_starts
    }

    numeric_by_path: dict[str, list[GameFieldObservation]] = {}
    for item in numeric:
        numeric_by_path.setdefault(item.path, []).append(item)

    identifier_by_path: dict[str, list[GameFieldObservation]] = {}
    for item in identifiers:
        identifier_by_path.setdefault(item.path, []).append(item)

    numeric_stats: list[NumericPathLeakStat] = []
    examples: list[ExactLeakExample] = []

    for path, path_observations in numeric_by_path.items():
        times = [item.timestamp for item in path_observations]
        intervals_seen = 0
        exact_targets = 0
        ge_025 = ge_1 = ge_2 = ge_5 = prestart_count = 0
        earliest_leads: list[float] = []

        for previous, target in zip(completed_rounds, completed_rounds[1:]):
            previous_at, _ = previous
            target_at, actual = target
            previous_ts = _parse_timestamp(previous_at)
            target_ts = _parse_timestamp(target_at)
            if previous_ts is None or target_ts is None or target_ts <= previous_ts:
                continue

            left = bisect_right(times, previous_ts)
            right = bisect_right(times, target_ts)
            interval = path_observations[left:right]
            if not interval:
                continue
            intervals_seen += 1

            matches = [
                item
                for item in interval
                if item.numeric_value is not None
                and _exact_match(item.numeric_value, actual)
            ]
            if not matches:
                continue

            exact_targets += 1
            earliest = min(matches, key=lambda item: item.timestamp)
            lead = target_ts - earliest.timestamp
            earliest_leads.append(lead)
            ge_025 += lead >= 0.25
            ge_1 += lead >= 1.0
            ge_2 += lead >= 2.0
            ge_5 += lead >= 5.0

            start = starts_by_target.get(target_at)
            start_ts = (
                _parse_timestamp(start.start_observed_at)
                if start is not None
                else None
            )
            before_start = start_ts is not None and earliest.timestamp < start_ts
            prestart_count += bool(before_start)

            if len(examples) < max_examples:
                examples.append(
                    ExactLeakExample(
                        path=path,
                        target_completed_at=target_at,
                        actual_multiplier=actual,
                        observed_at=earliest.observed_at,
                        observed_value=earliest.numeric_value,
                        lead_seconds=round(lead, 6),
                        before_inferred_start=bool(before_start),
                        event_kind=earliest.event_kind,
                        url=earliest.url,
                    )
                )

        numeric_stats.append(
            NumericPathLeakStat(
                path=path,
                observations=len(path_observations),
                target_intervals_seen=intervals_seen,
                exact_match_targets=exact_targets,
                exact_matches_ge_025s=ge_025,
                exact_matches_ge_1s=ge_1,
                exact_matches_ge_2s=ge_2,
                exact_matches_ge_5s=ge_5,
                exact_matches_before_inferred_start=prestart_count,
                median_earliest_exact_lead_seconds=(
                    round(float(median(earliest_leads)), 6)
                    if earliest_leads
                    else None
                ),
                maximum_exact_lead_seconds=(
                    round(max(earliest_leads), 6)
                    if earliest_leads
                    else None
                ),
                suspicious_outcome_path=_suspicious_path(path),
            )
        )

    identifier_stats: list[IdentifierPathStat] = []
    for path, path_observations in identifier_by_path.items():
        times = [item.timestamp for item in path_observations]
        intervals_seen = 0
        prestart_intervals = 0

        for previous, target in zip(completed_rounds, completed_rounds[1:]):
            previous_at, _ = previous
            target_at, _ = target
            previous_ts = _parse_timestamp(previous_at)
            target_ts = _parse_timestamp(target_at)
            if previous_ts is None or target_ts is None or target_ts <= previous_ts:
                continue

            left = bisect_right(times, previous_ts)
            right = bisect_right(times, target_ts)
            interval = path_observations[left:right]
            if not interval:
                continue
            intervals_seen += 1

            start = starts_by_target.get(target_at)
            start_ts = (
                _parse_timestamp(start.start_observed_at)
                if start is not None
                else None
            )
            if (
                start_ts is not None
                and any(item.timestamp < start_ts for item in interval)
            ):
                prestart_intervals += 1

        identifier_stats.append(
            IdentifierPathStat(
                path=path,
                observations=len(path_observations),
                unique_hashes=len(
                    {
                        item.identifier_hash
                        for item in path_observations
                        if item.identifier_hash is not None
                    }
                ),
                target_intervals_seen=intervals_seen,
                intervals_with_prestart_observation=prestart_intervals,
            )
        )

    numeric_stats.sort(
        key=lambda item: (
            item.exact_matches_before_inferred_start,
            item.exact_matches_ge_5s,
            item.exact_matches_ge_2s,
            item.exact_matches_ge_1s,
            item.exact_match_targets,
            item.observations,
        ),
        reverse=True,
    )
    identifier_stats.sort(
        key=lambda item: (
            item.intervals_with_prestart_observation,
            item.target_intervals_seen,
            item.unique_hashes,
        ),
        reverse=True,
    )
    examples.sort(
        key=lambda item: (
            item.before_inferred_start,
            item.lead_seconds,
        ),
        reverse=True,
    )

    return ProtocolLeakAudit(
        network_files=len(network_paths),
        completed_rounds=len(completed_rounds),
        inferred_starts=len(inferred_starts),
        structured_game_field_observations=len(observations),
        numeric_observations=len(numeric),
        identifier_observations=len(identifiers),
        numeric_paths=tuple(numeric_stats),
        identifier_paths=tuple(identifier_stats),
        exact_examples=tuple(examples[:max_examples]),
    )
