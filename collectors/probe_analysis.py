"""Summarize sanitized browser observations without reading raw account data."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import json
from pathlib import Path


CANDIDATE_TERMS = (
    "round", "multiplier", "coefficient", "result", "crash", "payout",
    "gameid", "game_id", "roundid", "round_id", "status",
)
LIVE_KINDS = {
    "websocket_received",
    "websocket_sent",
    "cdp_websocket_received",
    "cdp_websocket_sent",
    "cdp_eventsource_received",
}


@dataclass(frozen=True, slots=True)
class ProbeCandidate:
    kind: str
    url: str
    events: int
    json_paths: tuple[tuple[str, int], ...]
    candidate_paths: tuple[tuple[str, int], ...]
    status_counts: tuple[tuple[int, int], ...]


@dataclass(frozen=True, slots=True)
class LiveTransportCandidate:
    url: str
    events: int
    received_events: int
    sent_events: int
    json_events: int
    text_events: int
    binary_events: int
    candidate_paths: tuple[tuple[str, int], ...]
    opcode_counts: tuple[tuple[int, int], ...]
    min_bytes: int | None
    max_bytes: int | None
    distinct_sizes: int
    score: int
    entropy_buckets: tuple[tuple[float, int], ...]
    printable_ratio_buckets: tuple[tuple[float, int], ...]
    zero_ratio_buckets: tuple[tuple[float, int], ...]
    high_bit_ratio_buckets: tuple[tuple[float, int], ...]
    unique_byte_buckets: tuple[tuple[int, int], ...]




@dataclass(frozen=True, slots=True)
class BinaryFrameClass:
    url: str
    direction: str
    size_bucket: str
    events: int
    median_gap_ms: float | None
    min_gap_ms: float | None
    max_gap_ms: float | None
    entropy_mode: float | None
    zero_ratio_mode: float | None
    high_bit_ratio_mode: float | None
    unique_byte_mode: int | None
    periodicity_score: float


def _interesting_paths(paths: Counter[str]) -> tuple[tuple[str, int], ...]:
    ordered = tuple(paths.most_common(50))
    return tuple(
        (path, count)
        for path, count in ordered
        if any(term in path.lower().replace("-", "_") for term in CANDIDATE_TERMS)
    )


def summarize_probe(path: Path) -> tuple[ProbeCandidate, ...]:
    """Aggregate sanitized JSONL observations by event kind and URL."""
    if not path.is_file():
        raise FileNotFoundError("Probe file does not exist.")
    groups: dict[tuple[str, str], dict[str, object]] = defaultdict(
        lambda: {"events": 0, "paths": Counter(), "statuses": Counter()}
    )
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("kind")
            url = event.get("url")
            if not isinstance(kind, str) or not isinstance(url, str):
                continue
            group = groups[(kind, url)]
            group["events"] = int(group["events"]) + 1
            status = event.get("status")
            if isinstance(status, int):
                statuses: Counter[int] = group["statuses"]  # type: ignore[assignment]
                statuses[status] += 1
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            shape = payload.get("shape")
            if not isinstance(shape, list):
                continue
            paths: Counter[str] = group["paths"]  # type: ignore[assignment]
            for item in shape:
                if isinstance(item, dict) and isinstance(item.get("path"), str):
                    paths[item["path"]] += 1

    candidates: list[ProbeCandidate] = []
    for (kind, url), data in groups.items():
        paths: Counter[str] = data["paths"]  # type: ignore[assignment]
        statuses: Counter[int] = data["statuses"]  # type: ignore[assignment]
        ordered = tuple(paths.most_common(50))
        candidates.append(
            ProbeCandidate(
                kind=kind,
                url=url,
                events=int(data["events"]),
                json_paths=ordered,
                candidate_paths=_interesting_paths(paths),
                status_counts=tuple(sorted(statuses.items())),
            )
        )
    return tuple(
        sorted(
            candidates,
            key=lambda item: (
                bool(item.candidate_paths),
                item.events,
                len(item.json_paths),
            ),
            reverse=True,
        )
    )


def summarize_live_transports(path: Path) -> tuple[LiveTransportCandidate, ...]:
    """Rank live socket/stream groups using only sanitized metadata."""
    if not path.is_file():
        raise FileNotFoundError("Probe file does not exist.")

    groups: dict[str, dict[str, object]] = defaultdict(
        lambda: {
            "events": 0,
            "received": 0,
            "sent": 0,
            "formats": Counter(),
            "paths": Counter(),
            "opcodes": Counter(),
            "sizes": Counter(),
            "entropy": Counter(),
            "printable": Counter(),
            "zero": Counter(),
            "high_bit": Counter(),
            "unique_bytes": Counter(),
        }
    )

    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("kind")
            url = event.get("url")
            if kind not in LIVE_KINDS or not isinstance(url, str):
                continue

            group = groups[url]
            group["events"] = int(group["events"]) + 1
            if kind.endswith("received"):
                group["received"] = int(group["received"]) + 1
            elif kind.endswith("sent"):
                group["sent"] = int(group["sent"]) + 1

            opcode = event.get("opcode")
            if isinstance(opcode, int):
                opcodes: Counter[int] = group["opcodes"]  # type: ignore[assignment]
                opcodes[opcode] += 1

            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue

            payload_format = payload.get("format")
            if isinstance(payload_format, str):
                formats: Counter[str] = group["formats"]  # type: ignore[assignment]
                formats[payload_format] += 1

            payload_bytes = payload.get("bytes")
            if isinstance(payload_bytes, int) and payload_bytes >= 0:
                sizes: Counter[int] = group["sizes"]  # type: ignore[assignment]
                sizes[payload_bytes] += 1

            fingerprint = payload.get("binary_fingerprint")
            if isinstance(fingerprint, dict):
                entropy_value = fingerprint.get("entropy_bucket")
                printable_value = fingerprint.get("printable_ratio_bucket")
                zero_value = fingerprint.get("zero_ratio_bucket")
                high_bit_value = fingerprint.get("high_bit_ratio_bucket")
                unique_value = fingerprint.get("unique_byte_bucket")
                if isinstance(entropy_value, (int, float)):
                    entropy: Counter[float] = group["entropy"]  # type: ignore[assignment]
                    entropy[float(entropy_value)] += 1
                if isinstance(printable_value, (int, float)):
                    printable: Counter[float] = group["printable"]  # type: ignore[assignment]
                    printable[float(printable_value)] += 1
                if isinstance(zero_value, (int, float)):
                    zero: Counter[float] = group["zero"]  # type: ignore[assignment]
                    zero[float(zero_value)] += 1
                if isinstance(high_bit_value, (int, float)):
                    high_bit: Counter[float] = group["high_bit"]  # type: ignore[assignment]
                    high_bit[float(high_bit_value)] += 1
                if isinstance(unique_value, int):
                    unique_bytes: Counter[int] = group["unique_bytes"]  # type: ignore[assignment]
                    unique_bytes[unique_value] += 1

            shape = payload.get("shape")
            if isinstance(shape, list):
                paths: Counter[str] = group["paths"]  # type: ignore[assignment]
                for item in shape:
                    if isinstance(item, dict) and isinstance(item.get("path"), str):
                        paths[item["path"]] += 1

    result: list[LiveTransportCandidate] = []
    for url, data in groups.items():
        formats: Counter[str] = data["formats"]  # type: ignore[assignment]
        paths: Counter[str] = data["paths"]  # type: ignore[assignment]
        opcodes: Counter[int] = data["opcodes"]  # type: ignore[assignment]
        sizes: Counter[int] = data["sizes"]  # type: ignore[assignment]
        entropy: Counter[float] = data["entropy"]  # type: ignore[assignment]
        printable: Counter[float] = data["printable"]  # type: ignore[assignment]
        zero: Counter[float] = data["zero"]  # type: ignore[assignment]
        high_bit: Counter[float] = data["high_bit"]  # type: ignore[assignment]
        unique_bytes: Counter[int] = data["unique_bytes"]  # type: ignore[assignment]
        candidate_paths = _interesting_paths(paths)
        events = int(data["events"])
        received = int(data["received"])

        # Heuristic ranking only: prioritize live inbound traffic and explicit
        # round-like structure. Size variation helps surface multiplexed streams.
        score = (
            min(received, 100)
            + 25 * len(candidate_paths)
            + min(formats.get("json", 0), 20) * 2
            + min(len(sizes), 20)
        )
        ordered_sizes = sorted(sizes)
        result.append(
            LiveTransportCandidate(
                url=url,
                events=events,
                received_events=received,
                sent_events=int(data["sent"]),
                json_events=formats.get("json", 0),
                text_events=formats.get("text", 0),
                binary_events=formats.get("binary", 0),
                candidate_paths=candidate_paths,
                opcode_counts=tuple(sorted(opcodes.items())),
                min_bytes=ordered_sizes[0] if ordered_sizes else None,
                max_bytes=ordered_sizes[-1] if ordered_sizes else None,
                distinct_sizes=len(ordered_sizes),
                score=score,
                entropy_buckets=tuple(sorted(entropy.items())),
                printable_ratio_buckets=tuple(sorted(printable.items())),
                zero_ratio_buckets=tuple(sorted(zero.items())),
                high_bit_ratio_buckets=tuple(sorted(high_bit.items())),
                unique_byte_buckets=tuple(sorted(unique_bytes.items())),
            )
        )

    return tuple(
        sorted(
            result,
            key=lambda item: (
                item.score,
                item.received_events,
                item.events,
            ),
            reverse=True,
        )
    )


def _parse_observed_at(value: object) -> float | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


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


def _mode(counter: Counter[float] | Counter[int]) -> float | int | None:
    if not counter:
        return None
    return counter.most_common(1)[0][0]


def summarize_binary_frame_classes(path: Path) -> tuple[BinaryFrameClass, ...]:
    """Cluster binary WebSocket frames by direction, size band, timing and coarse fingerprint."""
    if not path.is_file():
        raise FileNotFoundError("Probe file does not exist.")

    groups: dict[tuple[str, str, str], dict[str, object]] = defaultdict(
        lambda: {
            "times": [],
            "entropy": Counter(),
            "zero": Counter(),
            "high_bit": Counter(),
            "unique": Counter(),
        }
    )

    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue

            kind = event.get("kind")
            url = event.get("url")
            if kind not in {"cdp_websocket_received", "cdp_websocket_sent"} or not isinstance(url, str):
                continue

            payload = event.get("payload")
            if not isinstance(payload, dict) or payload.get("format") != "binary":
                continue
            size = payload.get("bytes")
            if not isinstance(size, int) or size < 0:
                continue

            direction = "received" if kind.endswith("received") else "sent"
            key = (url, direction, _size_bucket(size))
            group = groups[key]

            ts = _parse_observed_at(event.get("observed_at"))
            if ts is not None:
                times: list[float] = group["times"]  # type: ignore[assignment]
                times.append(ts)

            fingerprint = payload.get("binary_fingerprint")
            if not isinstance(fingerprint, dict):
                continue

            for field, group_key in (
                ("entropy_bucket", "entropy"),
                ("zero_ratio_bucket", "zero"),
                ("high_bit_ratio_bucket", "high_bit"),
                ("unique_byte_bucket", "unique"),
            ):
                value = fingerprint.get(field)
                if isinstance(value, (int, float)):
                    counter = group[group_key]
                    assert isinstance(counter, Counter)
                    counter[value] += 1

    result: list[BinaryFrameClass] = []
    for (url, direction, size_bucket), data in groups.items():
        times = sorted(data["times"])  # type: ignore[arg-type]
        gaps_ms = [
            (current - previous) * 1000.0
            for previous, current in zip(times, times[1:])
            if current >= previous
        ]
        gaps_ms.sort()
        if gaps_ms:
            middle = len(gaps_ms) // 2
            if len(gaps_ms) % 2:
                median_gap = gaps_ms[middle]
            else:
                median_gap = (gaps_ms[middle - 1] + gaps_ms[middle]) / 2
            mean = sum(gaps_ms) / len(gaps_ms)
            variance = sum((gap - mean) ** 2 for gap in gaps_ms) / len(gaps_ms)
            std = variance ** 0.5
            periodicity = max(0.0, 1.0 - min(1.0, std / mean)) if mean > 0 else 0.0
            min_gap = gaps_ms[0]
            max_gap = gaps_ms[-1]
        else:
            median_gap = min_gap = max_gap = None
            periodicity = 0.0

        event_count = len(times)
        entropy = data["entropy"]; zero = data["zero"]; high_bit = data["high_bit"]; unique = data["unique"]
        assert isinstance(entropy, Counter)
        assert isinstance(zero, Counter)
        assert isinstance(high_bit, Counter)
        assert isinstance(unique, Counter)

        result.append(
            BinaryFrameClass(
                url=url,
                direction=direction,
                size_bucket=size_bucket,
                events=event_count,
                median_gap_ms=round(median_gap, 1) if median_gap is not None else None,
                min_gap_ms=round(min_gap, 1) if min_gap is not None else None,
                max_gap_ms=round(max_gap, 1) if max_gap is not None else None,
                entropy_mode=float(_mode(entropy)) if _mode(entropy) is not None else None,
                zero_ratio_mode=float(_mode(zero)) if _mode(zero) is not None else None,
                high_bit_ratio_mode=float(_mode(high_bit)) if _mode(high_bit) is not None else None,
                unique_byte_mode=int(_mode(unique)) if _mode(unique) is not None else None,
                periodicity_score=round(periodicity, 3),
            )
        )

    return tuple(
        sorted(
            result,
            key=lambda item: (item.events, item.periodicity_score),
            reverse=True,
        )
    )


@dataclass(frozen=True, slots=True)
class MarkerCorrelation:
    event: str
    markers: int
    window_seconds: float
    nearby_classes: tuple[tuple[str, str, int], ...]


def summarize_marker_correlations(
    probe_path: Path,
    marker_path: Path,
    window_seconds: float = 1.5,
) -> tuple[MarkerCorrelation, ...]:
    """Count binary frame classes occurring near user-marked visible round events."""
    if not probe_path.is_file():
        raise FileNotFoundError("Probe file does not exist.")
    if not marker_path.is_file():
        return ()
    if window_seconds <= 0:
        raise ValueError("window_seconds must be positive.")

    frames: list[tuple[float, str, str]] = []
    with probe_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("kind")
            url = event.get("url")
            payload = event.get("payload")
            ts = _parse_observed_at(event.get("observed_at"))
            if (
                kind not in {"cdp_websocket_received", "cdp_websocket_sent"}
                or not isinstance(url, str)
                or not isinstance(payload, dict)
                or payload.get("format") != "binary"
                or ts is None
            ):
                continue
            size = payload.get("bytes")
            if not isinstance(size, int):
                continue
            direction = "received" if kind.endswith("received") else "sent"
            label = f"{direction}:{_size_bucket(size)}"
            frames.append((ts, url, label))

    markers: dict[str, list[float]] = defaultdict(list)
    with marker_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                marker = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(marker, dict) or marker.get("kind") != "visual_marker":
                continue
            event_name = marker.get("event")
            ts = _parse_observed_at(marker.get("observed_at"))
            if isinstance(event_name, str) and ts is not None:
                markers[event_name].append(ts)

    result: list[MarkerCorrelation] = []
    for event_name, marker_times in sorted(markers.items()):
        counts: Counter[tuple[str, str]] = Counter()
        for marker_ts in marker_times:
            for frame_ts, url, label in frames:
                if abs(frame_ts - marker_ts) <= window_seconds:
                    counts[(url, label)] += 1
        nearby = tuple(
            (url, label, count)
            for (url, label), count in counts.most_common(20)
        )
        result.append(
            MarkerCorrelation(
                event=event_name,
                markers=len(marker_times),
                window_seconds=window_seconds,
                nearby_classes=nearby,
            )
        )
    return tuple(result)
