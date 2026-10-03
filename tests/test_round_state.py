"""Tests for inferred round starts and strict pre-start feature alignment."""

from decimal import Decimal
import json
from pathlib import Path

from prediction.round_state import (
    infer_round_starts,
    load_completed_round_boundaries,
    load_live_multiplier_observations,
)
from prediction.system_state import (
    ObservedRound,
    SystemProbeEvent,
    build_prestart_system_state_samples,
)


def _snapshot(
    observed_at: str,
    history: list[str],
    live: str | None = None,
) -> dict[str, object]:
    candidates: list[dict[str, object]] = []
    for index, value in enumerate(history):
        candidates.append(
            {
                "multiplier": value,
                "x_bucket": index * 50,
                "y_bucket": 50,
                "width_bucket": 40,
                "height_bucket": 20,
            }
        )
    if live is not None:
        candidates.append(
            {
                "multiplier": live,
                "x_bucket": 270,
                "y_bucket": 700,
                "width_bucket": 120,
                "height_bucket": 50,
            }
        )
    return {
        "observed_at": observed_at,
        "kind": "dom_multiplier_snapshot",
        "url": "about:blank",
        "canvas_count": 0,
        "candidates": candidates,
    }


def _write(path: Path, events: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")


def test_infers_low_live_multiplier_between_completed_rounds(tmp_path: Path) -> None:
    path = tmp_path / "dom.jsonl"
    base = ["1.10", "1.20", "1.30", "1.40", "1.50"]
    first = ["2.00", *base]
    second = ["3.00", *first]
    _write(
        path,
        [
            _snapshot("2026-10-03T00:00:00Z", base),
            _snapshot("2026-10-03T00:00:10Z", first),
            _snapshot("2026-10-03T00:00:15Z", first, "1.03"),
            _snapshot("2026-10-03T00:00:30Z", second, "3.00"),
        ],
    )

    completed = load_completed_round_boundaries([path])
    live = load_live_multiplier_observations([path])
    starts = infer_round_starts(completed, live)

    assert [value for _, value in completed] == [Decimal("2.00"), Decimal("3.00")]
    assert len(starts) == 1
    assert starts[0].start_observed_at == "2026-10-03T00:00:15Z"
    assert starts[0].start_multiplier == Decimal("1.03")
    assert starts[0].previous_completed_at == "2026-10-03T00:00:10Z"
    assert starts[0].target_completed_at == "2026-10-03T00:00:30Z"


def test_prestart_builder_excludes_events_inside_safety_margin(tmp_path: Path) -> None:
    path = tmp_path / "dom.jsonl"
    base = ["1.10", "1.20", "1.30", "1.40", "1.50"]
    first = ["2.00", *base]
    second = ["3.00", *first]
    _write(
        path,
        [
            _snapshot("2026-10-03T00:00:00Z", base),
            _snapshot("2026-10-03T00:00:10Z", first),
            _snapshot("2026-10-03T00:00:15Z", first, "1.02"),
            _snapshot("2026-10-03T00:00:30Z", second, "3.00"),
        ],
    )
    starts = infer_round_starts(
        load_completed_round_boundaries([path]),
        load_live_multiplier_observations([path]),
    )

    rounds = [
        ObservedRound(
            timestamp=f"2026-10-03T00:00:{index:02d}Z",
            multiplier=Decimal("1.5"),
        )
        for index in range(1, 20)
    ]
    rounds.extend(
        [
            ObservedRound(
                timestamp="2026-10-03T00:00:10Z",
                multiplier=Decimal("2.00"),
            ),
            ObservedRound(
                timestamp="2026-10-03T00:00:30Z",
                multiplier=Decimal("3.00"),
            ),
        ]
    )
    # Keep chronological order while ensuring 20 history rows exist before target.
    rounds = sorted(rounds, key=lambda item: item.timestamp)

    def probe(stamp: float) -> SystemProbeEvent:
        return SystemProbeEvent(
            timestamp=stamp,
            direction="received",
            socket="s",
            payload_bytes=100,
            payload_format="binary",
            entropy=5.0,
            zero_ratio=0.1,
            high_bit_ratio=0.4,
            unique_byte_bucket=128,
        )

    # Epoch timestamps for 00:00:10.5, 12, 14, 14.6 and 15.1 UTC.
    from datetime import datetime, UTC

    base_epoch = datetime(2026, 10, 3, tzinfo=UTC).timestamp()
    events = tuple(
        probe(base_epoch + offset)
        for offset in (10.5, 12.0, 14.0, 14.6, 15.1)
    )

    samples = build_prestart_system_state_samples(
        tuple(rounds),
        events,
        starts,
        safety_margin_seconds=0.5,
        min_interval_events=3,
    )
    assert len(samples) == 1
    assert samples[0].target_timestamp == "2026-10-03T00:00:30Z"
    assert samples[0].cutoff_timestamp == "2026-10-03T00:00:14.500000Z"
    assert samples[0].longest_window_events == 3
