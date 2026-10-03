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


@dataclass(frozen=True, slots=True)
class ProbeCandidate:
    kind: str
    url: str
    events: int
    json_paths: tuple[tuple[str, int], ...]
    candidate_paths: tuple[tuple[str, int], ...]
    status_counts: tuple[tuple[int, int], ...]


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
        interesting = tuple(
            (path, count) for path, count in ordered
            if any(term in path.lower().replace("-", "_") for term in CANDIDATE_TERMS)
        )
        candidates.append(
            ProbeCandidate(
                kind=kind,
                url=url,
                events=int(data["events"]),
                json_paths=ordered,
                candidate_paths=interesting,
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
