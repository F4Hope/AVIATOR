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
