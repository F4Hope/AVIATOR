"""Discover causal network signatures that precede observable round starts.

Discovery uses only an earlier chronological segment. Selected signatures are
then applied causally to later intervals: the first qualifying signature after
the previous completed result becomes the prediction trigger, provided it occurs
before the observed next-round start.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import math
from statistics import median
from typing import Sequence

from prediction.features import FeatureConfig, feature_vector
from prediction.round_state import InferredRoundStart
from prediction.system_state import (
    ObservedRound,
    SystemProbeEvent,
    SystemStateSample,
    _parse_timestamp,
    _window_features,
)


def _size_bucket(size: int) -> str:
    if size < 64:
        return "<64"
    if size < 128:
        return "64-127"
    if size < 256:
        return "128-255"
    if size < 512:
        return "256-511"
    if size < 1024:
        return "512-1023"
    if size < 2048:
        return "1024-2047"
    return "2048+"


def event_signature(event: SystemProbeEvent) -> str:
    base = f"{event.direction}:{event.payload_format}:{_size_bucket(event.payload_bytes)}"
    if event.payload_format != "binary":
        return base
    entropy = "na" if event.entropy is None else f"{event.entropy:g}"
    zero = "na" if event.zero_ratio is None else f"{event.zero_ratio:g}"
    high = "na" if event.high_bit_ratio is None else f"{event.high_bit_ratio:g}"
    unique = "na" if event.unique_byte_bucket is None else str(event.unique_byte_bucket)
    return f"{base}|e={entropy}|z={zero}|h={high}|u={unique}"


@dataclass(frozen=True, slots=True)
class TriggerSignature:
    signature: str
    discovery_starts: int
    hit_starts: int
    hit_rate: float
    occurrences_near_start: int
    background_occurrences: int
    enrichment: float
    median_lead_seconds: float
    minimum_lead_seconds: float
    score: float


@dataclass(frozen=True, slots=True)
class CausalTrigger:
    target_completed_at: str
    previous_completed_at: str
    trigger_timestamp: float
    trigger_observed_at: str
    signature: str
    signature_score: float
    lead_seconds: float
    age_after_previous_seconds: float


@dataclass(frozen=True, slots=True)
class TriggerDiscovery:
    discovery_count: int
    evaluation_count: int
    signatures: tuple[TriggerSignature, ...]
    evaluation_starts: tuple[InferredRoundStart, ...]


def _iso_timestamp(timestamp: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def discover_prestart_trigger_signatures(
    starts: Sequence[InferredRoundStart],
    events: Sequence[SystemProbeEvent],
    *,
    discovery_fraction: float = 0.55,
    lookback_seconds: float = 2.5,
    minimum_lead_seconds: float = 0.20,
    minimum_hit_starts: int = 4,
    minimum_hit_rate: float = 0.12,
    minimum_enrichment: float = 1.5,
    max_signatures: int = 12,
) -> TriggerDiscovery:
    if not 0.3 <= discovery_fraction <= 0.8:
        raise ValueError("discovery_fraction must be between 0.3 and 0.8.")
    if lookback_seconds <= minimum_lead_seconds or minimum_lead_seconds <= 0:
        raise ValueError("lookback_seconds must exceed a positive minimum lead.")
    if minimum_hit_starts < 2:
        raise ValueError("minimum_hit_starts must be at least 2.")
    if not 0 < minimum_hit_rate <= 1:
        raise ValueError("minimum_hit_rate must be in (0, 1].")
    if minimum_enrichment <= 0:
        raise ValueError("minimum_enrichment must be positive.")
    if max_signatures < 1:
        raise ValueError("max_signatures must be positive.")

    ordered_starts = tuple(
        sorted(starts, key=lambda item: _parse_timestamp(item.start_observed_at) or 0.0)
    )
    split = max(1, min(len(ordered_starts) - 1, int(len(ordered_starts) * discovery_fraction)))
    discovery_starts = ordered_starts[:split]
    evaluation_starts = ordered_starts[split:]

    hit_starts: dict[str, set[int]] = {}
    near_counts: dict[str, int] = {}
    background_counts: dict[str, int] = {}
    leads: dict[str, list[float]] = {}
    discovery_background_seconds = 0.0

    for index, start in enumerate(discovery_starts):
        previous_ts = _parse_timestamp(start.previous_completed_at)
        start_ts = _parse_timestamp(start.start_observed_at)
        if previous_ts is None or start_ts is None or start_ts <= previous_ts:
            continue

        near_left = max(previous_ts, start_ts - lookback_seconds)
        near_right = start_ts - minimum_lead_seconds
        if near_right <= near_left:
            continue

        background_right = near_left
        if background_right > previous_ts:
            discovery_background_seconds += background_right - previous_ts

        seen_near: set[str] = set()
        for event in events:
            if event.timestamp <= previous_ts:
                continue
            if event.timestamp >= start_ts:
                break
            signature = event_signature(event)
            if near_left <= event.timestamp <= near_right:
                near_counts[signature] = near_counts.get(signature, 0) + 1
                lead = start_ts - event.timestamp
                leads.setdefault(signature, []).append(lead)
                seen_near.add(signature)
            elif event.timestamp < background_right:
                background_counts[signature] = background_counts.get(signature, 0) + 1

        for signature in seen_near:
            hit_starts.setdefault(signature, set()).add(index)

    near_window_seconds = max(
        1e-9,
        (lookback_seconds - minimum_lead_seconds) * len(discovery_starts),
    )
    candidates: list[TriggerSignature] = []

    for signature, count in near_counts.items():
        hits = len(hit_starts.get(signature, ()))
        if hits < minimum_hit_starts:
            continue
        hit_rate = hits / max(1, len(discovery_starts))
        if hit_rate < minimum_hit_rate:
            continue

        background = background_counts.get(signature, 0)
        near_rate = count / near_window_seconds
        background_rate = (
            background / discovery_background_seconds
            if discovery_background_seconds > 0
            else 0.0
        )
        # Add a small pseudocount so zero-background signatures remain finite.
        enrichment = (near_rate + 1e-6) / (background_rate + 1e-6)
        if enrichment < minimum_enrichment:
            continue

        signature_leads = sorted(leads.get(signature, ()))
        if not signature_leads:
            continue
        median_lead = float(median(signature_leads))
        min_lead = min(signature_leads)
        if min_lead < minimum_lead_seconds:
            continue

        score = hit_rate * math.log1p(enrichment) * math.log1p(hits)
        candidates.append(
            TriggerSignature(
                signature=signature,
                discovery_starts=len(discovery_starts),
                hit_starts=hits,
                hit_rate=round(hit_rate, 6),
                occurrences_near_start=count,
                background_occurrences=background,
                enrichment=round(enrichment, 6),
                median_lead_seconds=round(median_lead, 6),
                minimum_lead_seconds=round(min_lead, 6),
                score=round(score, 6),
            )
        )

    candidates.sort(
        key=lambda item: (
            item.score,
            item.hit_rate,
            item.enrichment,
            item.minimum_lead_seconds,
        ),
        reverse=True,
    )
    return TriggerDiscovery(
        discovery_count=len(discovery_starts),
        evaluation_count=len(evaluation_starts),
        signatures=tuple(candidates[:max_signatures]),
        evaluation_starts=tuple(evaluation_starts),
    )


def apply_causal_prestart_triggers(
    starts: Sequence[InferredRoundStart],
    events: Sequence[SystemProbeEvent],
    signatures: Sequence[TriggerSignature],
    *,
    minimum_live_lead_seconds: float = 0.20,
) -> tuple[CausalTrigger, ...]:
    if minimum_live_lead_seconds <= 0:
        raise ValueError("minimum_live_lead_seconds must be positive.")
    scores = {item.signature: item.score for item in signatures}
    if not scores:
        return ()

    result: list[CausalTrigger] = []
    for start in starts:
        previous_ts = _parse_timestamp(start.previous_completed_at)
        start_ts = _parse_timestamp(start.start_observed_at)
        if previous_ts is None or start_ts is None or start_ts <= previous_ts:
            continue

        trigger_event: SystemProbeEvent | None = None
        trigger_signature = ""
        for event in events:
            if event.timestamp <= previous_ts:
                continue
            if event.timestamp >= start_ts:
                break
            signature = event_signature(event)
            if signature not in scores:
                continue
            if start_ts - event.timestamp < minimum_live_lead_seconds:
                continue
            trigger_event = event
            trigger_signature = signature
            break

        if trigger_event is None:
            continue

        result.append(
            CausalTrigger(
                target_completed_at=start.target_completed_at,
                previous_completed_at=start.previous_completed_at,
                trigger_timestamp=trigger_event.timestamp,
                trigger_observed_at=_iso_timestamp(trigger_event.timestamp),
                signature=trigger_signature,
                signature_score=scores[trigger_signature],
                lead_seconds=round(start_ts - trigger_event.timestamp, 6),
                age_after_previous_seconds=round(
                    trigger_event.timestamp - previous_ts, 6
                ),
            )
        )

    return tuple(result)


def build_triggered_system_state_samples(
    rounds: Sequence[ObservedRound],
    events: Sequence[SystemProbeEvent],
    triggers: Sequence[CausalTrigger],
    signatures: Sequence[TriggerSignature],
    *,
    trailing_windows_seconds: Sequence[float] = (0.75, 2.0, 5.0),
    min_interval_events: int = 2,
    history_config: FeatureConfig | None = None,
) -> tuple[SystemStateSample, ...]:
    if (
        not trailing_windows_seconds
        or any(window <= 0 for window in trailing_windows_seconds)
    ):
        raise ValueError("trailing_windows_seconds must contain positive values.")
    if type(min_interval_events) is not int or min_interval_events < 1:
        raise ValueError("min_interval_events must be positive.")

    active_history = history_config if history_config is not None else FeatureConfig()
    trigger_by_target = {item.target_completed_at: item for item in triggers}
    signature_rank = {
        item.signature: index
        for index, item in enumerate(signatures)
    }
    event_times = [item.timestamp for item in events]
    round_values = [item.multiplier for item in rounds]
    windows = tuple(sorted(float(value) for value in trailing_windows_seconds))
    result: list[SystemStateSample] = []

    from bisect import bisect_right

    for target_index in range(max(1, active_history.warmup), len(rounds)):
        target = rounds[target_index]
        previous = rounds[target_index - 1]
        trigger = trigger_by_target.get(target.timestamp)
        if trigger is None or trigger.previous_completed_at != previous.timestamp:
            continue

        previous_ts = _parse_timestamp(previous.timestamp)
        target_ts = _parse_timestamp(target.timestamp)
        if previous_ts is None or target_ts is None:
            continue
        cutoff = trigger.trigger_timestamp
        if not (previous_ts < cutoff < target_ts):
            continue

        interval_left = bisect_right(event_times, previous_ts)
        interval_right = bisect_right(event_times, cutoff)
        interval_events = events[interval_left:interval_right]
        if len(interval_events) < min_interval_events:
            continue

        trailing: list[tuple[float, ...]] = []
        for window in windows:
            left = bisect_right(event_times, cutoff - window)
            trailing.append(_window_features(events[left:interval_right]))

        trigger_event = next(
            (
                item for item in reversed(interval_events)
                if event_signature(item) == trigger.signature
            ),
            interval_events[-1],
        )
        rank = signature_rank.get(trigger.signature, len(signature_rank))
        binary = 1.0 if trigger_event.payload_format == "binary" else 0.0
        received = 1.0 if trigger_event.direction == "received" else 0.0

        history = round_values[:target_index]
        recent = history[-8:]
        baseline = Decimal(str(median(float(value) for value in recent)))
        state_features = (
            math.log1p(trigger.age_after_previous_seconds),
            math.log1p(trigger.signature_score),
            1.0 / (1.0 + rank),
            received,
            binary,
            math.log1p(trigger_event.payload_bytes),
            float(trigger_event.entropy or 0.0),
            float(trigger_event.zero_ratio or 0.0),
            float(trigger_event.high_bit_ratio or 0.0),
            float(trigger_event.unique_byte_bucket or 0) / 256.0,
            *_window_features(interval_events),
            *(
                value
                for values in trailing
                for value in values
            ),
        )
        result.append(
            SystemStateSample(
                target_index=target_index,
                cutoff_timestamp=trigger.trigger_observed_at,
                target_timestamp=target.timestamp,
                actual_multiplier=target.multiplier,
                baseline_recent_median=baseline,
                state_features=tuple(state_features),
                history_features=feature_vector(history, active_history),
                longest_window_events=len(interval_events),
            )
        )

    return tuple(result)
