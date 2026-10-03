"""Tests for split-safe causal pre-start transport triggers."""

from datetime import UTC, datetime
from decimal import Decimal

from prediction.prestart_trigger import (
    apply_causal_prestart_triggers,
    build_triggered_system_state_samples,
    discover_prestart_trigger_signatures,
    event_signature,
)
from prediction.round_state import InferredRoundStart
from prediction.system_state import ObservedRound, SystemProbeEvent


BASE = datetime(2026, 10, 3, tzinfo=UTC).timestamp()


def _event(offset: float, size: int = 256) -> SystemProbeEvent:
    return SystemProbeEvent(
        timestamp=BASE + offset,
        direction="received",
        socket="socket-a",
        payload_bytes=size,
        payload_format="binary",
        entropy=5.0,
        zero_ratio=0.1,
        high_bit_ratio=0.4,
        unique_byte_bucket=128,
    )


def _start(index: int) -> InferredRoundStart:
    previous = index * 20.0
    start = previous + 10.0
    target = previous + 18.0

    def stamp(offset: float) -> str:
        return datetime.fromtimestamp(BASE + offset, tz=UTC).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")

    return InferredRoundStart(
        previous_completed_at=stamp(previous),
        target_completed_at=stamp(target),
        start_observed_at=stamp(start),
        start_multiplier=Decimal("1.02"),
        seconds_after_previous_completion=10.0,
        seconds_before_target_completion=8.0,
    )


def test_discovers_signature_concentrated_before_start() -> None:
    starts = tuple(_start(index) for index in range(10))
    events = []
    for index, start in enumerate(starts):
        previous = index * 20.0
        # Background traffic with a different size.
        events.extend(
            [_event(previous + 2.0, 64), _event(previous + 4.0, 64)]
        )
        # Strong candidate 0.8 seconds before the inferred start.
        events.append(_event(previous + 9.2, 256))

    discovery = discover_prestart_trigger_signatures(
        starts,
        tuple(sorted(events, key=lambda item: item.timestamp)),
        discovery_fraction=0.5,
        lookback_seconds=2.0,
        minimum_lead_seconds=0.2,
        minimum_hit_starts=2,
        minimum_hit_rate=0.2,
        minimum_enrichment=1.2,
        max_signatures=5,
    )

    assert discovery.discovery_count == 5
    assert discovery.evaluation_count == 5
    assert discovery.signatures
    assert discovery.signatures[0].signature == event_signature(_event(9.2, 256))
    assert discovery.signatures[0].median_lead_seconds == 0.8


def test_frozen_signature_triggers_only_holdout_intervals() -> None:
    starts = tuple(_start(index) for index in range(10))
    events = []
    for index in range(10):
        previous = index * 20.0
        events.extend(
            [
                _event(previous + 2.0, 64),
                _event(previous + 9.2, 256),
            ]
        )
    ordered = tuple(sorted(events, key=lambda item: item.timestamp))

    discovery = discover_prestart_trigger_signatures(
        starts,
        ordered,
        discovery_fraction=0.5,
        lookback_seconds=2.0,
        minimum_lead_seconds=0.2,
        minimum_hit_starts=2,
        minimum_hit_rate=0.2,
        minimum_enrichment=1.2,
        max_signatures=5,
    )
    triggers = apply_causal_prestart_triggers(
        discovery.evaluation_starts,
        ordered,
        discovery.signatures,
        minimum_live_lead_seconds=0.2,
    )

    assert len(triggers) == 5
    assert all(item.lead_seconds == 0.8 for item in triggers)
    assert all(item.age_after_previous_seconds == 9.2 for item in triggers)


def test_triggered_sample_uses_trigger_time_not_target_result() -> None:
    starts = tuple(_start(index) for index in range(10))
    events = []
    for index in range(10):
        previous = index * 20.0
        events.extend(
            [
                _event(previous + 2.0, 64),
                _event(previous + 4.0, 64),
                _event(previous + 9.2, 256),
            ]
        )
        if index >= 5:
            # Holdout-only event after the trained trigger signature. It cannot
            # influence discovery and must not enter the triggered feature row.
            events.append(_event(previous + 9.6, 2048))
    ordered = tuple(sorted(events, key=lambda item: item.timestamp))

    discovery = discover_prestart_trigger_signatures(
        starts,
        ordered,
        discovery_fraction=0.5,
        lookback_seconds=2.0,
        minimum_lead_seconds=0.2,
        minimum_hit_starts=2,
        minimum_hit_rate=0.2,
        minimum_enrichment=1.2,
        max_signatures=5,
    )
    triggers = apply_causal_prestart_triggers(
        discovery.evaluation_starts,
        ordered,
        discovery.signatures,
        minimum_live_lead_seconds=0.2,
    )

    rounds = []
    for index in range(20):
        rounds.append(
            ObservedRound(
                timestamp=datetime.fromtimestamp(
                    BASE - (20 - index), tz=UTC
                ).isoformat(timespec="microseconds").replace("+00:00", "Z"),
                multiplier=Decimal("1.5"),
            )
        )
    # Add all completed boundaries so target timestamps can align exactly.
    for index in range(10):
        previous = index * 20.0
        rounds.append(
            ObservedRound(
                timestamp=datetime.fromtimestamp(
                    BASE + previous, tz=UTC
                ).isoformat(timespec="microseconds").replace("+00:00", "Z"),
                multiplier=Decimal("2.0"),
            )
        )
        rounds.append(
            ObservedRound(
                timestamp=datetime.fromtimestamp(
                    BASE + previous + 18.0, tz=UTC
                ).isoformat(timespec="microseconds").replace("+00:00", "Z"),
                multiplier=Decimal("3.0"),
            )
        )
    rounds = tuple(sorted(rounds, key=lambda item: item.timestamp))

    samples = build_triggered_system_state_samples(
        rounds,
        ordered,
        triggers,
        discovery.signatures,
        min_interval_events=2,
    )

    assert samples
    assert all(
        sample.cutoff_timestamp < sample.target_timestamp
        for sample in samples
    )
    # Each interval includes events through 9.2s, but excludes the 9.6s event.
    assert all(sample.longest_window_events == 3 for sample in samples)
