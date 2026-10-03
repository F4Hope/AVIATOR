"""Leakage-aware audit for pre-round protocol disclosures and state signals."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
import json
from pathlib import Path
from statistics import median
from typing import Mapping, Sequence

from prediction.round_state import InferredRoundStart
from prediction.system_state import _parse_timestamp


RECEIVED_JSON_KINDS = frozenset(
    {
        "cdp_websocket_received",
        "websocket_received",
        "response",
    }
)
DEFAULT_THRESHOLDS: tuple[Decimal, ...] = (
    Decimal("1.5"),
    Decimal("2"),
    Decimal("5"),
)


@dataclass(frozen=True, slots=True)
class ProtocolEvent:
    timestamp: float
    observed_at: str
    kind: str
    url: str
    payload_sha256: str
    game_fields: tuple[Mapping[str, object], ...]
    categorical_fields: tuple[Mapping[str, object], ...]


@dataclass(frozen=True, slots=True)
class NumericFieldAudit:
    path: str
    observations: int
    rounds: int
    target_matches: int
    previous_matches: int
    target_match_rate: float
    previous_match_rate: float
    median_lead_seconds: float | None
    minimum_lead_seconds: float | None
    verdict: str


@dataclass(frozen=True, slots=True)
class IdentifierFieldAudit:
    path: str
    observations: int
    rounds: int
    unique_hashes: int
    median_lead_seconds: float | None


@dataclass(frozen=True, slots=True)
class CategoricalHoldoutAudit:
    threshold: Decimal
    token: str
    discovery_support: int
    discovery_positive_rate: float
    discovery_lift: float
    holdout_support: int
    holdout_positive_rate: float | None
    holdout_lift: float | None
    direction_consistent: bool


@dataclass(frozen=True, slots=True)
class ProtocolDisclosureAudit:
    eligible_intervals: int
    metadata_intervals: int
    json_events: int
    safety_margin_seconds: float
    numeric_fields: tuple[NumericFieldAudit, ...]
    identifier_fields: tuple[IdentifierFieldAudit, ...]
    categorical_events: int
    unique_categorical_tokens: int
    categorical_holdout: tuple[CategoricalHoldoutAudit, ...]


def load_protocol_events(paths: Sequence[Path]) -> tuple[ProtocolEvent, ...]:
    """Load only sanitized JSON metadata already written by the collector."""
    events: list[ProtocolEvent] = []
    seen: set[tuple[str, str, str, str]] = set()

    for path in paths:
        if not path.is_file():
            continue
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    document = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(document, dict):
                    continue
                kind = document.get("kind")
                observed_at = document.get("observed_at")
                payload = document.get("payload")
                if (
                    kind not in RECEIVED_JSON_KINDS
                    or not isinstance(observed_at, str)
                    or not isinstance(payload, dict)
                    or payload.get("format") != "json"
                ):
                    continue
                timestamp = _parse_timestamp(observed_at)
                if timestamp is None:
                    continue
                url = document.get("url")
                sha = payload.get("sha256")
                if not isinstance(url, str):
                    url = ""
                if not isinstance(sha, str):
                    sha = ""
                identity = (observed_at, str(kind), url, sha)
                if identity in seen:
                    continue
                seen.add(identity)

                raw_game = payload.get("game_fields")
                game_fields = tuple(
                    item for item in raw_game
                    if isinstance(item, dict)
                ) if isinstance(raw_game, list) else ()

                raw_categories = payload.get("categorical_fields")
                categories = tuple(
                    item for item in raw_categories
                    if isinstance(item, dict)
                ) if isinstance(raw_categories, list) else ()

                if not game_fields and not categories:
                    continue
                events.append(
                    ProtocolEvent(
                        timestamp=timestamp,
                        observed_at=observed_at,
                        kind=str(kind),
                        url=url,
                        payload_sha256=sha,
                        game_fields=game_fields,
                        categorical_fields=categories,
                    )
                )

    return tuple(sorted(events, key=lambda item: item.timestamp))


def _numeric(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
        return result if result == result and abs(result) != float("inf") else None
    return None


def _close(left: float, right: Decimal, tolerance: float) -> bool:
    return abs(left - float(right)) <= tolerance


def _categorical_token(field: Mapping[str, object]) -> str | None:
    path = field.get("path")
    value = field.get("value")
    if not isinstance(path, str) or not isinstance(value, str):
        return None
    if not path or not value:
        return None
    return f"{path}={value}"


def audit_protocol_disclosures(
    *,
    starts: Sequence[InferredRoundStart],
    events: Sequence[ProtocolEvent],
    target_multipliers: Mapping[str, Decimal],
    previous_multipliers: Mapping[str, Decimal],
    safety_margin_seconds: float = 2.0,
    numeric_tolerance: float = 0.01,
    discovery_fraction: float = 0.60,
    minimum_category_support: int = 5,
    thresholds: Sequence[Decimal] = DEFAULT_THRESHOLDS,
) -> ProtocolDisclosureAudit:
    if safety_margin_seconds <= 0:
        raise ValueError("safety_margin_seconds must be positive.")
    if numeric_tolerance <= 0:
        raise ValueError("numeric_tolerance must be positive.")
    if not 0.4 <= discovery_fraction <= 0.8:
        raise ValueError("discovery_fraction must be between 0.4 and 0.8.")
    if minimum_category_support < 2:
        raise ValueError("minimum_category_support must be at least 2.")

    intervals: list[
        tuple[InferredRoundStart, Decimal, Decimal, tuple[ProtocolEvent, ...]]
    ] = []

    event_index = 0
    for start in sorted(
        starts,
        key=lambda item: _parse_timestamp(item.start_observed_at) or 0.0,
    ):
        previous_ts = _parse_timestamp(start.previous_completed_at)
        start_ts = _parse_timestamp(start.start_observed_at)
        if previous_ts is None or start_ts is None:
            continue
        cutoff = start_ts - safety_margin_seconds
        if cutoff <= previous_ts:
            continue
        target = target_multipliers.get(start.target_completed_at)
        previous = previous_multipliers.get(start.previous_completed_at)
        if target is None or previous is None:
            continue

        while event_index < len(events) and events[event_index].timestamp <= previous_ts:
            event_index += 1
        index = event_index
        selected: list[ProtocolEvent] = []
        while index < len(events) and events[index].timestamp <= cutoff:
            if events[index].timestamp > previous_ts:
                selected.append(events[index])
            index += 1
        intervals.append((start, target, previous, tuple(selected)))

    numeric_stats: dict[str, dict[str, object]] = defaultdict(
        lambda: {
            "observations": 0,
            "rounds": set(),
            "target_matches": 0,
            "previous_matches": 0,
            "leads": [],
        }
    )
    identifier_stats: dict[str, dict[str, object]] = defaultdict(
        lambda: {
            "observations": 0,
            "rounds": set(),
            "hashes": set(),
            "leads": [],
        }
    )

    interval_tokens: list[tuple[Decimal, set[str]]] = []
    json_events = 0
    metadata_intervals = 0
    categorical_events = 0

    for interval_index, (start, target, previous, selected) in enumerate(intervals):
        start_ts = _parse_timestamp(start.start_observed_at)
        assert start_ts is not None
        if selected:
            metadata_intervals += 1
        tokens: set[str] = set()

        for event in selected:
            json_events += 1
            if event.categorical_fields:
                categorical_events += 1
            for field in event.categorical_fields:
                token = _categorical_token(field)
                if token is not None:
                    tokens.add(token)

            for field in event.game_fields:
                path = field.get("path")
                kind = field.get("kind")
                if not isinstance(path, str) or not isinstance(kind, str):
                    continue
                lead = start_ts - event.timestamp

                if kind == "numeric":
                    value = _numeric(field.get("value"))
                    if value is None:
                        continue
                    stat = numeric_stats[path]
                    stat["observations"] = int(stat["observations"]) + 1
                    rounds = stat["rounds"]
                    assert isinstance(rounds, set)
                    rounds.add(interval_index)
                    leads = stat["leads"]
                    assert isinstance(leads, list)
                    leads.append(lead)
                    if _close(value, target, numeric_tolerance):
                        stat["target_matches"] = int(stat["target_matches"]) + 1
                    if _close(value, previous, numeric_tolerance):
                        stat["previous_matches"] = int(stat["previous_matches"]) + 1

                elif kind == "identifier_hash":
                    digest = field.get("sha256_24")
                    if not isinstance(digest, str):
                        continue
                    stat = identifier_stats[path]
                    stat["observations"] = int(stat["observations"]) + 1
                    rounds = stat["rounds"]
                    assert isinstance(rounds, set)
                    rounds.add(interval_index)
                    hashes = stat["hashes"]
                    assert isinstance(hashes, set)
                    hashes.add(digest)
                    leads = stat["leads"]
                    assert isinstance(leads, list)
                    leads.append(lead)

        interval_tokens.append((target, tokens))

    numeric_result: list[NumericFieldAudit] = []
    for path, stat in numeric_stats.items():
        observations = int(stat["observations"])
        target_matches = int(stat["target_matches"])
        previous_matches = int(stat["previous_matches"])
        rounds = stat["rounds"]
        leads = stat["leads"]
        assert isinstance(rounds, set)
        assert isinstance(leads, list)
        target_rate = target_matches / observations if observations else 0.0
        previous_rate = previous_matches / observations if observations else 0.0
        verdict = "NO_DIRECT_DISCLOSURE"
        if (
            target_matches >= 3
            and target_rate >= 0.80
            and target_rate >= previous_rate + 0.20
        ):
            verdict = "CANDIDATE_PRE_ROUND_DISCLOSURE"
        numeric_result.append(
            NumericFieldAudit(
                path=path,
                observations=observations,
                rounds=len(rounds),
                target_matches=target_matches,
                previous_matches=previous_matches,
                target_match_rate=round(target_rate, 6),
                previous_match_rate=round(previous_rate, 6),
                median_lead_seconds=(
                    round(float(median(leads)), 6) if leads else None
                ),
                minimum_lead_seconds=(
                    round(float(min(leads)), 6) if leads else None
                ),
                verdict=verdict,
            )
        )

    identifier_result: list[IdentifierFieldAudit] = []
    for path, stat in identifier_stats.items():
        rounds = stat["rounds"]
        hashes = stat["hashes"]
        leads = stat["leads"]
        assert isinstance(rounds, set)
        assert isinstance(hashes, set)
        assert isinstance(leads, list)
        identifier_result.append(
            IdentifierFieldAudit(
                path=path,
                observations=int(stat["observations"]),
                rounds=len(rounds),
                unique_hashes=len(hashes),
                median_lead_seconds=(
                    round(float(median(leads)), 6) if leads else None
                ),
            )
        )

    split = int(len(interval_tokens) * discovery_fraction)
    split = max(1, min(len(interval_tokens) - 1, split)) if len(interval_tokens) > 1 else 0
    discovery = interval_tokens[:split]
    holdout = interval_tokens[split:]

    categorical_result: list[CategoricalHoldoutAudit] = []
    if discovery and holdout:
        all_tokens: set[str] = set()
        for _, tokens in discovery:
            all_tokens.update(tokens)

        for threshold in tuple(Decimal(str(value)) for value in thresholds):
            discovery_global = (
                sum(target >= threshold for target, _ in discovery) / len(discovery)
            )
            holdout_global = (
                sum(target >= threshold for target, _ in holdout) / len(holdout)
            )
            candidates: list[tuple[float, str, int, float]] = []

            for token in all_tokens:
                selected = [
                    target for target, tokens in discovery
                    if token in tokens
                ]
                support = len(selected)
                if support < minimum_category_support:
                    continue
                positive_rate = sum(
                    target >= threshold for target in selected
                ) / support
                lift = positive_rate - discovery_global
                score = abs(lift) * (support ** 0.5)
                candidates.append((score, token, support, positive_rate))

            for _, token, support, discovery_rate in sorted(
                candidates,
                reverse=True,
            )[:20]:
                selected_holdout = [
                    target for target, tokens in holdout
                    if token in tokens
                ]
                if selected_holdout:
                    holdout_rate = sum(
                        target >= threshold for target in selected_holdout
                    ) / len(selected_holdout)
                    holdout_lift = holdout_rate - holdout_global
                    discovery_lift = discovery_rate - discovery_global
                    consistent = (
                        discovery_lift == 0
                        or holdout_lift == 0
                        or (discovery_lift > 0) == (holdout_lift > 0)
                    )
                else:
                    holdout_rate = None
                    holdout_lift = None
                    discovery_lift = discovery_rate - discovery_global
                    consistent = False

                categorical_result.append(
                    CategoricalHoldoutAudit(
                        threshold=threshold,
                        token=token,
                        discovery_support=support,
                        discovery_positive_rate=round(discovery_rate, 6),
                        discovery_lift=round(discovery_lift, 6),
                        holdout_support=len(selected_holdout),
                        holdout_positive_rate=(
                            round(holdout_rate, 6)
                            if holdout_rate is not None
                            else None
                        ),
                        holdout_lift=(
                            round(holdout_lift, 6)
                            if holdout_lift is not None
                            else None
                        ),
                        direction_consistent=consistent,
                    )
                )

    unique_tokens = len(
        {
            token
            for _, tokens in interval_tokens
            for token in tokens
        }
    )

    return ProtocolDisclosureAudit(
        eligible_intervals=len(intervals),
        metadata_intervals=metadata_intervals,
        json_events=json_events,
        safety_margin_seconds=safety_margin_seconds,
        numeric_fields=tuple(
            sorted(
                numeric_result,
                key=lambda item: (
                    item.verdict == "CANDIDATE_PRE_ROUND_DISCLOSURE",
                    item.target_match_rate,
                    item.rounds,
                ),
                reverse=True,
            )
        ),
        identifier_fields=tuple(
            sorted(
                identifier_result,
                key=lambda item: (item.rounds, item.observations),
                reverse=True,
            )
        ),
        categorical_events=categorical_events,
        unique_categorical_tokens=unique_tokens,
        categorical_holdout=tuple(
            sorted(
                categorical_result,
                key=lambda item: (
                    item.direction_consistent,
                    item.holdout_support,
                    abs(item.holdout_lift or 0.0),
                ),
                reverse=True,
            )
        ),
    )
