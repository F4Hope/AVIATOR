"""Tests for leakage-safe pre-round system-state features and evaluation."""

from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path

from evaluation.system_state_backtest import walk_forward_system_state_evaluate
from prediction.system_state import (
    ObservedRound,
    build_system_state_samples,
    load_system_probe_events,
)


def _event(stamp: str, size: int, received: bool = True) -> dict[str, object]:
    return {
        "observed_at": stamp,
        "kind": "cdp_websocket_received" if received else "cdp_websocket_sent",
        "url": "cdp://websocket/test",
        "socket_id": "socket-a",
        "payload": {
            "bytes": size,
            "sha256": f"hash-{stamp}-{size}-{received}",
            "format": "binary",
            "binary_fingerprint": {
                "entropy_bucket": 5.0,
                "printable_ratio_bucket": 0.2,
                "zero_ratio_bucket": 0.1,
                "high_bit_ratio_bucket": 0.4,
                "unique_byte_bucket": 128,
            },
        },
    }


def _write_events(path: Path, events: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")


def test_state_features_end_at_previous_round_completion(tmp_path: Path) -> None:
    probe = tmp_path / "probe.jsonl"
    _write_events(
        probe,
        [
            _event("2026-10-03T00:00:19Z", 100),
            _event("2026-10-03T00:00:20Z", 200),
            # This target-round event occurs after the previous completion cutoff
            # and must not appear in the target's state vector.
            _event("2026-10-03T00:00:25Z", 5000),
        ],
    )
    events = load_system_probe_events([probe])

    rounds = [
        ObservedRound(
            timestamp=f"2026-10-03T00:00:{index:02d}Z",
            multiplier=Decimal("1.5"),
        )
        for index in range(1, 21)
    ]
    rounds.append(
        ObservedRound(timestamp="2026-10-03T00:00:30Z", multiplier=Decimal("9.0"))
    )

    samples = build_system_state_samples(
        rounds,
        events,
        windows_seconds=(2.0, 8.0),
        min_long_window_events=1,
    )
    assert len(samples) == 1
    sample = samples[0]
    assert sample.target_index == 20
    assert sample.cutoff_timestamp == "2026-10-03T00:00:20Z"
    # The 5000-byte event at t=25 is after the cutoff. If it leaked, total-byte
    # features would be far larger than the values produced by 100+200 bytes.
    assert max(sample.state_features) < 10.0


def test_probe_loader_deduplicates_archived_overlap(tmp_path: Path) -> None:
    event = _event("2026-10-03T00:00:10Z", 256)
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    _write_events(first, [event])
    _write_events(second, [event])
    loaded = load_system_probe_events([first, second])
    assert len(loaded) == 1


def test_system_state_backtest_runs_chronologically(tmp_path: Path) -> None:
    probe = tmp_path / "probe.jsonl"
    events: list[dict[str, object]] = []
    rounds: list[ObservedRound] = []

    for index in range(1, 81):
        stamp = f"2026-10-03T00:{index // 60:02d}:{index % 60:02d}Z"
        value = Decimal("1.2") if index % 3 else Decimal("3.0")
        rounds.append(ObservedRound(timestamp=stamp, multiplier=value))
        if index >= 2:
            previous_second = index - 1
            event_stamp = (
                f"2026-10-03T00:{previous_second // 60:02d}:"
                f"{previous_second % 60:02d}Z"
            )
            events.append(_event(event_stamp, 128 + (index % 5) * 64))

    _write_events(probe, events)
    loaded = load_system_probe_events([probe])
    samples = build_system_state_samples(
        rounds,
        loaded,
        windows_seconds=(2.0, 8.0),
        min_long_window_events=1,
    )
    report = walk_forward_system_state_evaluate(
        samples,
        min_training_samples=20,
        validation_rows=5,
    )
    assert report.status == "COMPLETE"
    assert report.evaluated_targets > 0
    assert report.history_only_mae is not None
    assert report.state_only_mae is not None
    assert report.hybrid_mae is not None
    assert report.hybrid_win_rate_vs_history is not None
