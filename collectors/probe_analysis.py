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


@dataclass(frozen=True, slots=True)
class MarkerClassStat:
    event: str
    url: str
    frame_class: str
    markers: int
    hits: int
    hit_rate: float
    nearby_frames: int
    expected_frames: float
    enrichment: float
    median_nearest_offset_ms: float | None


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def summarize_marker_class_stats(
    probe_path: Path,
    marker_path: Path,
    window_seconds: float = 0.75,
) -> tuple[MarkerClassStat, ...]:
    """Measure per-class enrichment and nearest timing around visual round markers.

    The baseline is each class's average rate across the capture. This prevents
    high-frequency traffic from looking important merely because it appears in
    every marker window.
    """
    if not probe_path.is_file():
        raise FileNotFoundError("Probe file does not exist.")
    if not marker_path.is_file():
        return ()
    if window_seconds <= 0:
        raise ValueError("window_seconds must be positive.")

    frames_by_class: dict[tuple[str, str], list[float]] = defaultdict(list)
    all_frame_times: list[float] = []
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
            frames_by_class[(url, label)].append(ts)
            all_frame_times.append(ts)

    if len(all_frame_times) < 2:
        return ()
    duration = max(all_frame_times) - min(all_frame_times)
    if duration <= 0:
        return ()

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

    stats: list[MarkerClassStat] = []
    for event_name, marker_times in sorted(markers.items()):
        marker_count = len(marker_times)
        if marker_count == 0:
            continue
        for (url, label), frame_times in frames_by_class.items():
            total_frames = len(frame_times)
            baseline_rate = total_frames / duration
            expected = baseline_rate * (2.0 * window_seconds) * marker_count
            nearby_frames = 0
            hits = 0
            nearest_offsets_ms: list[float] = []

            for marker_ts in marker_times:
                offsets = [
                    frame_ts - marker_ts
                    for frame_ts in frame_times
                    if abs(frame_ts - marker_ts) <= window_seconds
                ]
                nearby_frames += len(offsets)
                if offsets:
                    hits += 1
                    nearest = min(offsets, key=abs)
                    nearest_offsets_ms.append(nearest * 1000.0)

            enrichment = (nearby_frames / expected) if expected > 0 else 0.0
            stats.append(
                MarkerClassStat(
                    event=event_name,
                    url=url,
                    frame_class=label,
                    markers=marker_count,
                    hits=hits,
                    hit_rate=round(hits / marker_count, 3),
                    nearby_frames=nearby_frames,
                    expected_frames=round(expected, 2),
                    enrichment=round(enrichment, 2),
                    median_nearest_offset_ms=(
                        round(_median(nearest_offsets_ms), 1)
                        if nearest_offsets_ms
                        else None
                    ),
                )
            )

    return tuple(
        sorted(
            stats,
            key=lambda item: (
                item.event,
                item.hit_rate * item.enrichment,
                item.hits,
                item.nearby_frames,
            ),
            reverse=True,
        )
    )


@dataclass(frozen=True, slots=True)
class MarkerSignatureStat:
    event: str
    url: str
    signature: str
    markers: int
    hits: int
    hit_rate: float
    nearby_frames: int
    expected_frames: float
    enrichment: float
    median_nearest_offset_ms: float | None


def _frame_signature(direction: str, size: int, fingerprint: dict[str, object]) -> str | None:
    entropy = fingerprint.get("entropy_bucket")
    zero = fingerprint.get("zero_ratio_bucket")
    high_bit = fingerprint.get("high_bit_ratio_bucket")
    unique = fingerprint.get("unique_byte_bucket")
    if not isinstance(entropy, (int, float)):
        return None
    if not isinstance(zero, (int, float)):
        return None
    if not isinstance(high_bit, (int, float)):
        return None
    if not isinstance(unique, int):
        return None
    return (
        f"{direction}:{_size_bucket(size)}"
        f"|e={float(entropy):g}"
        f"|z={float(zero):g}"
        f"|h={float(high_bit):g}"
        f"|u={unique}"
    )


def summarize_marker_signature_stats(
    probe_path: Path,
    marker_path: Path,
    window_seconds: float = 0.5,
    min_total_frames: int = 2,
) -> tuple[MarkerSignatureStat, ...]:
    """Rank coarse binary signatures by enrichment around visual round markers."""
    if not probe_path.is_file():
        raise FileNotFoundError("Probe file does not exist.")
    if not marker_path.is_file():
        return ()
    if window_seconds <= 0:
        raise ValueError("window_seconds must be positive.")
    if min_total_frames < 1:
        raise ValueError("min_total_frames must be positive.")

    frames_by_signature: dict[tuple[str, str], list[float]] = defaultdict(list)
    all_frame_times: list[float] = []
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
            fingerprint = payload.get("binary_fingerprint")
            if not isinstance(size, int) or not isinstance(fingerprint, dict):
                continue
            direction = "received" if kind.endswith("received") else "sent"
            signature = _frame_signature(direction, size, fingerprint)
            if signature is None:
                continue
            frames_by_signature[(url, signature)].append(ts)
            all_frame_times.append(ts)

    if len(all_frame_times) < 2:
        return ()
    duration = max(all_frame_times) - min(all_frame_times)
    if duration <= 0:
        return ()

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

    stats: list[MarkerSignatureStat] = []
    for event_name, marker_times in sorted(markers.items()):
        marker_count = len(marker_times)
        if marker_count == 0:
            continue
        for (url, signature), frame_times in frames_by_signature.items():
            total_frames = len(frame_times)
            if total_frames < min_total_frames:
                continue

            baseline_rate = total_frames / duration
            expected = baseline_rate * (2.0 * window_seconds) * marker_count
            nearby_frames = 0
            hits = 0
            nearest_offsets_ms: list[float] = []

            for marker_ts in marker_times:
                offsets = [
                    frame_ts - marker_ts
                    for frame_ts in frame_times
                    if abs(frame_ts - marker_ts) <= window_seconds
                ]
                nearby_frames += len(offsets)
                if offsets:
                    hits += 1
                    nearest_offsets_ms.append(min(offsets, key=abs) * 1000.0)

            if nearby_frames == 0:
                continue

            enrichment = nearby_frames / expected if expected > 0 else 0.0
            stats.append(
                MarkerSignatureStat(
                    event=event_name,
                    url=url,
                    signature=signature,
                    markers=marker_count,
                    hits=hits,
                    hit_rate=round(hits / marker_count, 3),
                    nearby_frames=nearby_frames,
                    expected_frames=round(expected, 3),
                    enrichment=round(enrichment, 2),
                    median_nearest_offset_ms=(
                        round(_median(nearest_offsets_ms), 1)
                        if nearest_offsets_ms
                        else None
                    ),
                )
            )

    return tuple(
        sorted(
            stats,
            key=lambda item: (
                item.event,
                item.hit_rate * item.enrichment,
                item.hits,
                item.nearby_frames,
            ),
            reverse=True,
        )
    )


@dataclass(frozen=True, slots=True)
class DomCompletedRound:
    observed_at: str
    multiplier: str
    edge: str
    overlap: int
    history_size: int
    y_bucket: int


def _dom_history_row(candidates: object) -> tuple[int, tuple[str, ...]] | None:
    """Return the densest compact horizontal multiplier row from one DOM snapshot."""
    if not isinstance(candidates, list):
        return None

    rows: dict[int, list[tuple[int, str]]] = defaultdict(list)
    for item in candidates:
        if not isinstance(item, dict):
            continue
        multiplier = item.get("multiplier")
        x = item.get("x_bucket")
        y = item.get("y_bucket")
        width = item.get("width_bucket")
        height = item.get("height_bucket")
        if (
            not isinstance(multiplier, str)
            or not isinstance(x, int)
            or not isinstance(y, int)
            or not isinstance(width, int)
            or not isinstance(height, int)
        ):
            continue
        # The completed-history chips form a dense compact row. Wider 120px
        # values in observed captures belong to a different scrolling region.
        if width > 70 or height > 40:
            continue
        rows[y].append((x, multiplier))

    if not rows:
        return None

    y_bucket, values = max(rows.items(), key=lambda pair: (len(pair[1]), -abs(pair[0])))
    if len(values) < 5:
        return None
    ordered = tuple(value for _, value in sorted(values))
    return y_bucket, ordered


def _sequence_overlap_after_left_insert(previous: tuple[str, ...], current: tuple[str, ...]) -> int:
    limit = min(len(previous), max(0, len(current) - 1))
    count = 0
    for index in range(limit):
        if current[index + 1] != previous[index]:
            break
        count += 1
    return count


def _sequence_overlap_after_right_insert(previous: tuple[str, ...], current: tuple[str, ...]) -> int:
    limit = min(max(0, len(previous) - 1), len(current))
    count = 0
    for index in range(1, limit + 1):
        if current[-index - 1] != previous[-index]:
            break
        count += 1
    return count


def extract_dom_completed_rounds(
    dom_path: Path,
    min_overlap: int = 5,
) -> tuple[DomCompletedRound, ...]:
    """Infer completed outcomes when the visible history strip inserts one new value.

    This is deliberately post-round extraction only. The function never treats
    transient live multiplier values as a completed result.
    """
    if not dom_path.is_file():
        return ()
    if min_overlap < 2:
        raise ValueError("min_overlap must be at least 2.")

    result: list[DomCompletedRound] = []
    previous_row: tuple[str, ...] | None = None
    previous_y: int | None = None
    last_event_identity: tuple[str, str] | None = None

    with dom_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("kind") != "dom_multiplier_snapshot":
                continue
            observed_at = event.get("observed_at")
            if not isinstance(observed_at, str):
                continue
            row = _dom_history_row(event.get("candidates"))
            if row is None:
                continue
            y_bucket, current_row = row

            if previous_row is not None and previous_y is not None and abs(y_bucket - previous_y) <= 20:
                left_overlap = _sequence_overlap_after_left_insert(previous_row, current_row)
                right_overlap = _sequence_overlap_after_right_insert(previous_row, current_row)

                if left_overlap >= min_overlap and current_row:
                    identity = (observed_at, current_row[0])
                    if identity != last_event_identity:
                        result.append(
                            DomCompletedRound(
                                observed_at=observed_at,
                                multiplier=current_row[0],
                                edge="left",
                                overlap=left_overlap,
                                history_size=len(current_row),
                                y_bucket=y_bucket,
                            )
                        )
                        last_event_identity = identity
                elif right_overlap >= min_overlap and current_row:
                    identity = (observed_at, current_row[-1])
                    if identity != last_event_identity:
                        result.append(
                            DomCompletedRound(
                                observed_at=observed_at,
                                multiplier=current_row[-1],
                                edge="right",
                                overlap=right_overlap,
                                history_size=len(current_row),
                                y_bucket=y_bucket,
                            )
                        )
                        last_event_identity = identity

            if previous_row != current_row:
                previous_row = current_row
                previous_y = y_bucket

    return tuple(result)
