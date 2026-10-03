"""Tests for prospective immutable pre-round snapshot capture."""

from datetime import UTC, datetime
from decimal import Decimal
import json
from pathlib import Path

from prediction.prestart_trigger import TriggerSignature, event_signature
from prediction.prospective import (
    SnapshotRound,
    append_snapshot_event,
    build_pre_round_snapshot,
    invalidate_snapshot,
    load_snapshot_ledger,
    outstanding_snapshot,
    score_pre_round_snapshot,
)
from prediction.system_state import SystemProbeEvent
from run_prospective_snapshots import _read_new_network_events


BASE = datetime(2026, 10, 3, tzinfo=UTC).timestamp()


def _event(offset: float, size: int = 128) -> SystemProbeEvent:
    return SystemProbeEvent(
        timestamp=BASE + offset,
        direction="received",
        socket="socket-a",
        payload_bytes=size,
        payload_format="binary",
        entropy=4.0,
        zero_ratio=0.25,
        high_bit_ratio=0.1,
        unique_byte_bucket=32,
    )


def _signature(event: SystemProbeEvent) -> TriggerSignature:
    return TriggerSignature(
        signature=event_signature(event),
        discovery_starts=50,
        hit_starts=30,
        hit_rate=0.6,
        occurrences_near_start=40,
        background_occurrences=5,
        enrichment=8.0,
        median_lead_seconds=1.1,
        minimum_lead_seconds=0.3,
        score=4.2,
    )


def test_snapshot_contains_only_events_through_trigger() -> None:
    previous = SnapshotRound(
        round_id="round-a",
        timestamp="2026-10-03T00:00:10.000000Z",
        multiplier="2.0",
    )
    before = _event(11.0, 64)
    trigger = _event(13.0, 128)
    after = _event(14.0, 2048)

    snapshot = build_pre_round_snapshot(
        collector_session_id="session-a",
        previous_round=previous,
        trigger_event=trigger,
        interval_events=(before, trigger, after),
        trigger_signatures=(_signature(trigger),),
        max_sequence_events=32,
    )

    assert snapshot["event"] == "pre_round_snapshot_locked"
    assert snapshot["locked_at"] == "2026-10-03T00:00:13.000000Z"
    features = snapshot["features"]
    assert isinstance(features, dict)
    assert features["event_count_since_previous"] == 2
    assert len(features["recent_sequence"]) == 2
    assert all(
        item["signature"] != event_signature(after)
        for item in features["recent_sequence"]
    )


def test_snapshot_score_requires_later_result() -> None:
    previous = SnapshotRound(
        round_id="round-a",
        timestamp="2026-10-03T00:00:10.000000Z",
        multiplier="2.0",
    )
    trigger = _event(13.0, 128)
    snapshot = build_pre_round_snapshot(
        collector_session_id="session-a",
        previous_round=previous,
        trigger_event=trigger,
        interval_events=(trigger,),
        trigger_signatures=(_signature(trigger),),
    )
    actual = SnapshotRound(
        round_id="round-b",
        timestamp="2026-10-03T00:00:30.000000Z",
        multiplier="3.5",
    )

    scored = score_pre_round_snapshot(snapshot, actual)
    assert scored["snapshot_id"] == snapshot["snapshot_id"]
    assert scored["actual_multiplier"] == "3.5"


def test_append_only_ledger_tracks_outstanding_and_invalidation(tmp_path: Path) -> None:
    path = tmp_path / "snapshots.jsonl"
    previous = SnapshotRound(
        round_id="round-a",
        timestamp="2026-10-03T00:00:10.000000Z",
        multiplier="2.0",
    )
    trigger = _event(13.0, 128)
    snapshot = build_pre_round_snapshot(
        collector_session_id="session-a",
        previous_round=previous,
        trigger_event=trigger,
        interval_events=(trigger,),
        trigger_signatures=(_signature(trigger),),
    )

    append_snapshot_event(path, snapshot)
    events = load_snapshot_ledger(path)
    assert outstanding_snapshot(events) is not None

    invalid = invalidate_snapshot(snapshot, "collector_session_changed")
    append_snapshot_event(path, invalid)
    assert outstanding_snapshot(load_snapshot_ledger(path)) is None


def test_network_tailer_reads_only_new_lines(tmp_path: Path) -> None:
    path = tmp_path / "probe.jsonl"

    def document(observed_at: str, payload_bytes: int) -> dict[str, object]:
        return {
            "observed_at": observed_at,
            "kind": "cdp_websocket_received",
            "url": "wss://example.invalid/socket",
            "socket_id": "socket-a",
            "payload": {
                "bytes": payload_bytes,
                "format": "binary",
                "binary_fingerprint": {
                    "entropy_bucket": 4.0,
                    "zero_ratio_bucket": 0.25,
                    "high_bit_ratio_bucket": 0.1,
                    "unique_byte_bucket": 32,
                },
            },
        }

    first = document("2026-10-03T00:00:11.000000Z", 64)
    path.write_text(json.dumps(first) + "\n", encoding="utf-8")

    events, offset = _read_new_network_events(path, 0)
    assert len(events) == 1

    second = document("2026-10-03T00:00:12.000000Z", 128)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(second) + "\n")

    events2, offset2 = _read_new_network_events(path, offset)
    assert len(events2) == 1
    assert events2[0].payload_bytes == 128
    assert offset2 > offset
