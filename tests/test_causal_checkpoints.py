"""Tests for operationally causal inter-round checkpoints."""

from datetime import UTC, datetime
from decimal import Decimal

from prediction.round_state import InferredRoundStart
from prediction.system_state import (
    ObservedRound,
    SystemProbeEvent,
    build_causal_checkpoint_system_state_samples,
)


def _event(base: float, offset: float) -> SystemProbeEvent:
    return SystemProbeEvent(
        timestamp=base + offset,
        direction="received",
        socket="socket-a",
        payload_bytes=128,
        payload_format="binary",
        entropy=5.0,
        zero_ratio=0.1,
        high_bit_ratio=0.4,
        unique_byte_bucket=128,
    )


def _rounds() -> tuple[ObservedRound, ...]:
    history = [
        ObservedRound(
            timestamp=f"2026-10-02T23:59:{second:02d}Z",
            multiplier=Decimal("1.5"),
        )
        for second in range(40, 60)
    ]
    history.extend(
        [
            ObservedRound(
                timestamp="2026-10-03T00:00:10Z",
                multiplier=Decimal("2.0"),
            ),
            ObservedRound(
                timestamp="2026-10-03T00:00:30Z",
                multiplier=Decimal("3.0"),
            ),
        ]
    )
    return tuple(history)


def test_fixed_checkpoint_uses_only_events_observed_by_cutoff() -> None:
    rounds = _rounds()
    starts = (
        InferredRoundStart(
            previous_completed_at="2026-10-03T00:00:10Z",
            target_completed_at="2026-10-03T00:00:30Z",
            start_observed_at="2026-10-03T00:00:18Z",
            start_multiplier=Decimal("1.02"),
            seconds_after_previous_completion=8.0,
            seconds_before_target_completion=12.0,
        ),
    )
    base = datetime(2026, 10, 3, tzinfo=UTC).timestamp()
    events = tuple(
        _event(base, offset)
        for offset in (10.5, 11.0, 12.0, 14.0, 15.0, 15.1, 17.0)
    )

    samples = build_causal_checkpoint_system_state_samples(
        rounds,
        events,
        starts,
        checkpoint_seconds=5.0,
        min_interval_events=3,
    )

    assert len(samples) == 1
    sample = samples[0]
    assert sample.cutoff_timestamp == "2026-10-03T00:00:15.000000Z"
    assert sample.target_timestamp == "2026-10-03T00:00:30Z"
    # The 15.1s and 17s events occur after the operational checkpoint.
    assert sample.longest_window_events == 5


def test_checkpoint_is_skipped_when_round_already_started() -> None:
    rounds = _rounds()
    starts = (
        InferredRoundStart(
            previous_completed_at="2026-10-03T00:00:10Z",
            target_completed_at="2026-10-03T00:00:30Z",
            start_observed_at="2026-10-03T00:00:14Z",
            start_multiplier=Decimal("1.01"),
            seconds_after_previous_completion=4.0,
            seconds_before_target_completion=16.0,
        ),
    )
    base = datetime(2026, 10, 3, tzinfo=UTC).timestamp()
    events = tuple(_event(base, offset) for offset in (10.5, 11.0, 12.0, 13.0))

    samples = build_causal_checkpoint_system_state_samples(
        rounds,
        events,
        starts,
        checkpoint_seconds=5.0,
        min_interval_events=1,
    )

    assert samples == ()
