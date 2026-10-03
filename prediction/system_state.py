"""Leakage-safe pre-round system-state feature extraction.

For target round N, every network feature is cut off at the observed completion
of round N-1. This deliberately leaves a full round boundary between system
features and the target outcome.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import json
import math
from pathlib import Path
from statistics import median, pstdev
from typing import Iterable, Sequence

from prediction.features import FeatureConfig, feature_vector


CDP_FRAME_KINDS = {"cdp_websocket_received", "cdp_websocket_sent"}
DEFAULT_WINDOWS_SECONDS: tuple[float, ...] = (2.0, 8.0)


@dataclass(frozen=True, slots=True)
class ObservedRound:
    timestamp: str
    multiplier: Decimal


@dataclass(frozen=True, slots=True)
class SystemProbeEvent:
    timestamp: float
    direction: str
    socket: str
    payload_bytes: int
    payload_format: str
    entropy: float | None
    zero_ratio: float | None
    high_bit_ratio: float | None
    unique_byte_bucket: int | None


@dataclass(frozen=True, slots=True)
class SystemStateSample:
    target_index: int
    cutoff_timestamp: str
    target_timestamp: str
    actual_multiplier: Decimal
    baseline_recent_median: Decimal
    state_features: tuple[float, ...]
    history_features: tuple[float, ...]
    longest_window_events: int


def _parse_timestamp(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


def discover_network_probe_paths(raw_data_dir: Path) -> tuple[Path, ...]:
    """Return current and archived sanitized network probe files."""
    candidates: list[Path] = []
    current = raw_data_dir / "aviator-network-probe.jsonl"
    if current.is_file():
        candidates.append(current)
    archive = raw_data_dir / "archive"
    if archive.is_dir():
        candidates.extend(
            path
            for path in sorted(archive.glob("aviator-network-probe-*.jsonl"))
            if path.is_file()
        )
    return tuple(candidates)


def load_system_probe_events(paths: Iterable[Path]) -> tuple[SystemProbeEvent, ...]:
    """Load only sanitized CDP frame metadata; raw payload values are never required."""
    events: list[SystemProbeEvent] = []
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
                if not isinstance(event, dict) or event.get("kind") not in CDP_FRAME_KINDS:
                    continue

                timestamp = _parse_timestamp(event.get("observed_at"))
                payload = event.get("payload")
                if timestamp is None or not isinstance(payload, dict):
                    continue

                payload_bytes = payload.get("bytes")
                if not isinstance(payload_bytes, int) or payload_bytes < 0:
                    continue
                payload_format = payload.get("format")
                if not isinstance(payload_format, str):
                    continue

                kind = event["kind"]
                assert isinstance(kind, str)
                direction = "received" if kind.endswith("received") else "sent"
                socket_raw = event.get("socket_id")
                url_raw = event.get("url")
                socket = (
                    socket_raw
                    if isinstance(socket_raw, str)
                    else url_raw
                    if isinstance(url_raw, str)
                    else "unknown"
                )

                fingerprint = payload.get("binary_fingerprint")
                entropy = zero_ratio = high_bit_ratio = None
                unique_byte_bucket = None
                if isinstance(fingerprint, dict):
                    raw_entropy = fingerprint.get("entropy_bucket")
                    raw_zero = fingerprint.get("zero_ratio_bucket")
                    raw_high = fingerprint.get("high_bit_ratio_bucket")
                    raw_unique = fingerprint.get("unique_byte_bucket")
                    entropy = float(raw_entropy) if isinstance(raw_entropy, (int, float)) else None
                    zero_ratio = float(raw_zero) if isinstance(raw_zero, (int, float)) else None
                    high_bit_ratio = float(raw_high) if isinstance(raw_high, (int, float)) else None
                    unique_byte_bucket = int(raw_unique) if isinstance(raw_unique, int) else None

                identity = (
                    event.get("observed_at"),
                    kind,
                    socket,
                    payload_bytes,
                    payload.get("sha256"),
                )
                if identity in seen:
                    continue
                seen.add(identity)
                events.append(
                    SystemProbeEvent(
                        timestamp=timestamp,
                        direction=direction,
                        socket=socket,
                        payload_bytes=payload_bytes,
                        payload_format=payload_format,
                        entropy=entropy,
                        zero_ratio=zero_ratio,
                        high_bit_ratio=high_bit_ratio,
                        unique_byte_bucket=unique_byte_bucket,
                    )
                )

    events.sort(key=lambda item: item.timestamp)
    return tuple(events)


def _safe_mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _window_features(events: Sequence[SystemProbeEvent]) -> tuple[float, ...]:
    total = len(events)
    if total == 0:
        return (0.0,) * 17

    received = [event for event in events if event.direction == "received"]
    sent = [event for event in events if event.direction == "sent"]
    recv_binary = [event for event in received if event.payload_format == "binary"]
    sent_binary = [event for event in sent if event.payload_format == "binary"]
    binary = recv_binary + sent_binary

    total_bytes = sum(event.payload_bytes for event in events)
    recv_bytes = sum(event.payload_bytes for event in received)
    sent_bytes = sum(event.payload_bytes for event in sent)
    large_ratio = sum(event.payload_bytes >= 1024 for event in events) / total
    small_ratio = sum(event.payload_bytes < 128 for event in events) / total

    entropy = [event.entropy for event in binary if event.entropy is not None]
    zero = [event.zero_ratio for event in binary if event.zero_ratio is not None]
    high = [event.high_bit_ratio for event in binary if event.high_bit_ratio is not None]
    unique = [
        event.unique_byte_bucket / 256.0
        for event in binary
        if event.unique_byte_bucket is not None
    ]

    gaps_ms = [
        max(0.0, (current.timestamp - previous.timestamp) * 1000.0)
        for previous, current in zip(events, events[1:])
    ]
    gap_mean = _safe_mean(gaps_ms)
    gap_std = pstdev(gaps_ms) if len(gaps_ms) > 1 else 0.0
    sockets = len({event.socket for event in events})

    return (
        math.log1p(total),
        math.log1p(len(received)),
        math.log1p(len(sent)),
        math.log1p(total_bytes),
        math.log1p(recv_bytes),
        math.log1p(sent_bytes),
        len(recv_binary) / max(1, len(received)),
        len(sent_binary) / max(1, len(sent)),
        large_ratio,
        small_ratio,
        _safe_mean(entropy),
        _safe_mean(zero),
        _safe_mean(high),
        _safe_mean(unique),
        math.log1p(gap_mean),
        math.log1p(gap_std),
        math.log1p(sockets),
    )


def system_state_feature_names(
    windows_seconds: Sequence[float] = DEFAULT_WINDOWS_SECONDS,
) -> tuple[str, ...]:
    base = (
        "log_event_count",
        "log_received_count",
        "log_sent_count",
        "log_total_bytes",
        "log_received_bytes",
        "log_sent_bytes",
        "received_binary_ratio",
        "sent_binary_ratio",
        "large_frame_ratio",
        "small_frame_ratio",
        "mean_entropy_bucket",
        "mean_zero_ratio",
        "mean_high_bit_ratio",
        "mean_unique_byte_fraction",
        "log_gap_mean_ms",
        "log_gap_std_ms",
        "log_unique_sockets",
    )
    return tuple(
        f"state_{window:g}s_{name}"
        for window in windows_seconds
        for name in base
    )


def build_system_state_samples(
    rounds: Sequence[ObservedRound],
    events: Sequence[SystemProbeEvent],
    windows_seconds: Sequence[float] = DEFAULT_WINDOWS_SECONDS,
    min_long_window_events: int = 3,
    history_config: FeatureConfig | None = None,
) -> tuple[SystemStateSample, ...]:
    """Build target rows whose state features end at the previous round completion."""
    if not windows_seconds or any(window <= 0 for window in windows_seconds):
        raise ValueError("windows_seconds must contain positive values.")
    ordered_windows = tuple(sorted(float(window) for window in windows_seconds))
    if type(min_long_window_events) is not int or min_long_window_events < 1:
        raise ValueError("min_long_window_events must be positive.")

    active_history = history_config if history_config is not None else FeatureConfig()
    event_times = [event.timestamp for event in events]
    round_values = [round_.multiplier for round_ in rounds]
    samples: list[SystemStateSample] = []

    start_index = max(1, active_history.warmup)
    for target_index in range(start_index, len(rounds)):
        previous = rounds[target_index - 1]
        target = rounds[target_index]
        cutoff = _parse_timestamp(previous.timestamp)
        target_time = _parse_timestamp(target.timestamp)
        if cutoff is None or target_time is None or target_time <= cutoff:
            continue

        right = bisect_right(event_times, cutoff)
        per_window: list[tuple[float, ...]] = []
        longest_events = 0
        for window in ordered_windows:
            left_cutoff = cutoff - window
            left = bisect_right(event_times, left_cutoff)
            subset = events[left:right]
            per_window.append(_window_features(subset))
            if window == ordered_windows[-1]:
                longest_events = len(subset)

        if longest_events < min_long_window_events:
            continue

        history = round_values[:target_index]
        recent = history[-8:]
        baseline = Decimal(str(median(float(value) for value in recent)))
        samples.append(
            SystemStateSample(
                target_index=target_index,
                cutoff_timestamp=previous.timestamp,
                target_timestamp=target.timestamp,
                actual_multiplier=target.multiplier,
                baseline_recent_median=baseline,
                state_features=tuple(
                    value for window_values in per_window for value in window_values
                ),
                history_features=feature_vector(history, active_history),
                longest_window_events=longest_events,
            )
        )

    return tuple(samples)
