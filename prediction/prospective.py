"""Append-only prospective PRE-ROUND snapshot records.

Snapshots are immutable observations created before the next completed result is
known. Outcome linkage is appended later as a separate score event so the audit
trail cannot be retroactively rewritten.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from typing import Mapping, Sequence

from prediction.prestart_trigger import TriggerSignature, event_signature
from prediction.system_state import SystemProbeEvent, _parse_timestamp


SNAPSHOT_LEDGER_VERSION = 1
DEFAULT_SNAPSHOT_LEDGER_FILENAME = "pre-round-snapshot-ledger.jsonl"


@dataclass(frozen=True, slots=True)
class SnapshotRound:
    round_id: str
    timestamp: str
    multiplier: str


def utc_text(timestamp: float | None = None) -> str:
    if timestamp is None:
        value = datetime.now(UTC)
    else:
        value = datetime.fromtimestamp(timestamp, tz=UTC)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def load_snapshot_ledger(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    events: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("Snapshot ledger contains a non-object event.")
            if value.get("ledger_version") != SNAPSHOT_LEDGER_VERSION:
                raise ValueError("Snapshot ledger version is unsupported.")
            events.append(value)
    return events


def append_snapshot_event(path: Path, event: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(event)
    payload["ledger_version"] = SNAPSHOT_LEDGER_VERSION
    line = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line + "\n")
        handle.flush()


def outstanding_snapshot(
    events: Sequence[Mapping[str, object]],
) -> Mapping[str, object] | None:
    closed = {
        event.get("snapshot_id")
        for event in events
        if event.get("event")
        in {"pre_round_snapshot_scored", "pre_round_snapshot_invalidated"}
        and isinstance(event.get("snapshot_id"), str)
    }
    for event in reversed(events):
        snapshot_id = event.get("snapshot_id")
        if (
            event.get("event") == "pre_round_snapshot_locked"
            and isinstance(snapshot_id, str)
            and snapshot_id not in closed
        ):
            return event
    return None


def latest_arm(
    events: Sequence[Mapping[str, object]],
    collector_session_id: str,
) -> Mapping[str, object] | None:
    for event in reversed(events):
        if (
            event.get("event") == "prospective_capture_armed"
            and event.get("collector_session_id") == collector_session_id
        ):
            return event
    return None


def make_arm_event(
    collector_session_id: str,
    rounds: Sequence[SnapshotRound],
) -> dict[str, object]:
    if not collector_session_id:
        raise ValueError("collector_session_id must be nonempty.")
    last = rounds[-1] if rounds else None
    return {
        "event": "prospective_capture_armed",
        "armed_at": utc_text(),
        "collector_session_id": collector_session_id,
        "history_count": len(rounds),
        "history_last_round_id": last.round_id if last is not None else None,
        "history_last_timestamp": last.timestamp if last is not None else None,
        "reason": "await_fresh_completion_before_pre_round_capture",
    }


def snapshot_id(
    collector_session_id: str,
    previous_round_id: str,
    locked_at: str,
    trigger_signature: str,
) -> str:
    material = (
        f"{collector_session_id}|{previous_round_id}|{locked_at}|{trigger_signature}"
    ).encode("utf-8", errors="replace")
    return "snapshot-" + hashlib.sha256(material).hexdigest()[:24]


def _bounded_sequence(
    events: Sequence[SystemProbeEvent],
    previous_timestamp: float,
    max_sequence_events: int,
) -> tuple[list[dict[str, object]], Counter[str], Counter[str]]:
    recent = tuple(events[-max_sequence_events:])
    sequence: list[dict[str, object]] = []
    signature_counts: Counter[str] = Counter()
    transition_counts: Counter[str] = Counter()

    previous_event_ts = previous_timestamp
    previous_signature: str | None = None
    for event in recent:
        signature = event_signature(event)
        signature_counts[signature] += 1
        if previous_signature is not None:
            transition_counts[f"{previous_signature} -> {signature}"] += 1
        sequence.append(
            {
                "signature": signature,
                "gap_ms": round(
                    max(0.0, (event.timestamp - previous_event_ts) * 1000.0),
                    3,
                ),
            }
        )
        previous_event_ts = event.timestamp
        previous_signature = signature

    return sequence, signature_counts, transition_counts


def build_pre_round_snapshot(
    *,
    collector_session_id: str,
    previous_round: SnapshotRound,
    trigger_event: SystemProbeEvent,
    interval_events: Sequence[SystemProbeEvent],
    trigger_signatures: Sequence[TriggerSignature],
    max_sequence_events: int = 32,
) -> dict[str, object]:
    """Create an immutable snapshot using only observations through trigger_event."""
    if not collector_session_id:
        raise ValueError("collector_session_id must be nonempty.")
    if type(max_sequence_events) is not int or not 4 <= max_sequence_events <= 128:
        raise ValueError("max_sequence_events must be between 4 and 128.")

    previous_ts = _parse_timestamp(previous_round.timestamp)
    if previous_ts is None or trigger_event.timestamp <= previous_ts:
        raise ValueError("trigger must occur after the previous completed result.")

    eligible = tuple(
        event
        for event in interval_events
        if previous_ts < event.timestamp <= trigger_event.timestamp
    )
    if not eligible:
        raise ValueError("snapshot requires at least one eligible pre-result event.")

    trigger_token = event_signature(trigger_event)
    scores = {item.signature: item.score for item in trigger_signatures}
    if trigger_token not in scores:
        raise ValueError("trigger event does not match a frozen trigger signature.")

    sequence, signature_counts, transition_counts = _bounded_sequence(
        eligible,
        previous_ts,
        max_sequence_events,
    )
    locked_at = utc_text(trigger_event.timestamp)
    elapsed = trigger_event.timestamp - previous_ts

    return {
        "event": "pre_round_snapshot_locked",
        "snapshot_id": snapshot_id(
            collector_session_id,
            previous_round.round_id,
            locked_at,
            trigger_token,
        ),
        "locked_at": locked_at,
        "collector_session_id": collector_session_id,
        "previous_round_id": previous_round.round_id,
        "previous_round_timestamp": previous_round.timestamp,
        "trigger_signature": trigger_token,
        "trigger_score": round(float(scores[trigger_token]), 6),
        "trigger_age_after_previous_seconds": round(elapsed, 6),
        "timing_guarantee": "locked_before_next_completed_result",
        "feature_schema": "prospective-signature-sequence-v1",
        "features": {
            "event_count_since_previous": len(eligible),
            "received_count": sum(event.direction == "received" for event in eligible),
            "sent_count": sum(event.direction == "sent" for event in eligible),
            "binary_count": sum(event.payload_format == "binary" for event in eligible),
            "total_payload_bytes": sum(event.payload_bytes for event in eligible),
            "signature_counts": dict(signature_counts.most_common(64)),
            "transition_counts": dict(transition_counts.most_common(64)),
            "recent_sequence": sequence,
        },
    }


def score_pre_round_snapshot(
    snapshot: Mapping[str, object],
    actual_round: SnapshotRound,
) -> dict[str, object]:
    if snapshot.get("event") != "pre_round_snapshot_locked":
        raise ValueError("only a locked snapshot can be scored.")
    snapshot_id_value = snapshot.get("snapshot_id")
    locked_at = snapshot.get("locked_at")
    if not isinstance(snapshot_id_value, str) or not isinstance(locked_at, str):
        raise ValueError("snapshot metadata is incomplete.")

    locked_ts = _parse_timestamp(locked_at)
    actual_ts = _parse_timestamp(actual_round.timestamp)
    if locked_ts is None or actual_ts is None or actual_ts <= locked_ts:
        raise ValueError("actual result must occur after the snapshot lock.")

    return {
        "event": "pre_round_snapshot_scored",
        "snapshot_id": snapshot_id_value,
        "scored_at": utc_text(),
        "actual_round_id": actual_round.round_id,
        "actual_timestamp": actual_round.timestamp,
        "actual_multiplier": actual_round.multiplier,
    }


def invalidate_snapshot(
    snapshot: Mapping[str, object],
    reason: str,
) -> dict[str, object]:
    snapshot_id_value = snapshot.get("snapshot_id")
    if not isinstance(snapshot_id_value, str):
        raise ValueError("snapshot_id is required for invalidation.")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("reason must be nonempty.")
    return {
        "event": "pre_round_snapshot_invalidated",
        "snapshot_id": snapshot_id_value,
        "invalidated_at": utc_text(),
        "reason": reason.strip(),
    }
