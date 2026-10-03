"""Infer observable round-start transitions from multiplier-only DOM snapshots.

This module never treats a live multiplier as a completed result. It pairs the
known completed-history insertions with the first low-valued, non-history
multiplier observation in the following inter-round interval. Consumers should
apply a safety margin before the inferred start timestamp when constructing
PRE-ROUND features.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from typing import Iterable, Sequence

from collectors.probe_analysis import extract_dom_completed_rounds


@dataclass(frozen=True, slots=True)
class LiveMultiplierObservation:
    observed_at: str
    timestamp: float
    multiplier: Decimal
    occurrences: int
    max_width_bucket: int
    max_height_bucket: int


@dataclass(frozen=True, slots=True)
class InferredRoundStart:
    previous_completed_at: str
    target_completed_at: str
    start_observed_at: str
    start_multiplier: Decimal
    seconds_after_previous_completion: float
    seconds_before_target_completion: float


def _timestamp(value: str) -> float:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware.")
    return parsed.timestamp()


def discover_dom_probe_paths(raw_data_dir: Path) -> tuple[Path, ...]:
    candidates: list[Path] = []
    current = raw_data_dir / "aviator-dom-multipliers.jsonl"
    if current.is_file():
        candidates.append(current)
    archive = raw_data_dir / "archive"
    if archive.is_dir():
        candidates.extend(
            path
            for path in sorted(archive.glob("aviator-dom-multipliers-*.jsonl"))
            if path.is_file()
        )
    return tuple(candidates)


def _decimal_multiplier(value: object) -> Decimal | None:
    if not isinstance(value, str):
        return None
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or number < 1 or number > 1000:
        return None
    return number


def _select_live_multiplier(candidates: object) -> tuple[Decimal, int, int, int] | None:
    """Choose the strongest non-history multiplier candidate from one snapshot.

    Completed-history chips observed in this UI are compact (<=70x40 buckets).
    Wider/taller multiplier elements are treated only as live-state candidates.
    Values are grouped because nested DOM elements can repeat the same visible
    multiplier.
    """
    if not isinstance(candidates, list):
        return None

    groups: dict[Decimal, list[tuple[int, int]]] = {}
    for item in candidates[:100]:
        if not isinstance(item, dict):
            continue
        multiplier = _decimal_multiplier(item.get("multiplier"))
        width = item.get("width_bucket")
        height = item.get("height_bucket")
        if (
            multiplier is None
            or not isinstance(width, int)
            or not isinstance(height, int)
            or width < 0
            or height < 0
        ):
            continue
        if width <= 70 and height <= 40:
            continue
        # Avoid page-sized ancestors that happen to inherit multiplier text.
        if width > 1000 or height > 600:
            continue
        groups.setdefault(multiplier, []).append((width, height))

    if not groups:
        return None

    ranked: list[tuple[int, int, int, Decimal]] = []
    for multiplier, boxes in groups.items():
        occurrences = len(boxes)
        max_width = max(width for width, _ in boxes)
        max_height = max(height for _, height in boxes)
        max_area = max(width * height for width, height in boxes)
        ranked.append((occurrences, max_area, max_width, multiplier))

    occurrences, _, max_width, multiplier = max(ranked)
    max_height = max(height for width, height in groups[multiplier] if width <= max_width)
    return multiplier, occurrences, max_width, max_height


def load_live_multiplier_observations(
    paths: Iterable[Path],
) -> tuple[LiveMultiplierObservation, ...]:
    result: list[LiveMultiplierObservation] = []
    seen: set[tuple[str, Decimal]] = set()

    for path in paths:
        if not path.is_file():
            continue
        with path.open("r", encoding="utf-8") as handle:
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
                try:
                    stamp = _timestamp(observed_at)
                except ValueError:
                    continue
                selected = _select_live_multiplier(event.get("candidates"))
                if selected is None:
                    continue
                multiplier, occurrences, width, height = selected
                identity = (observed_at, multiplier)
                if identity in seen:
                    continue
                seen.add(identity)
                result.append(
                    LiveMultiplierObservation(
                        observed_at=observed_at,
                        timestamp=stamp,
                        multiplier=multiplier,
                        occurrences=occurrences,
                        max_width_bucket=width,
                        max_height_bucket=height,
                    )
                )

    result.sort(key=lambda item: item.timestamp)
    return tuple(result)


def load_completed_round_boundaries(paths: Iterable[Path]) -> tuple[tuple[str, Decimal], ...]:
    result: list[tuple[str, Decimal]] = []
    seen: set[tuple[str, Decimal]] = set()
    for path in paths:
        for event in extract_dom_completed_rounds(path):
            multiplier = _decimal_multiplier(event.multiplier)
            if multiplier is None:
                continue
            identity = (event.observed_at, multiplier)
            if identity in seen:
                continue
            seen.add(identity)
            result.append(identity)
    result.sort(key=lambda item: _timestamp(item[0]))
    return tuple(result)


def infer_round_starts(
    completed: Sequence[tuple[str, Decimal]],
    live_observations: Sequence[LiveMultiplierObservation],
    *,
    maximum_start_multiplier: Decimal = Decimal("1.25"),
    minimum_seconds_after_completion: float = 0.75,
    minimum_seconds_before_completion: float = 1.0,
) -> tuple[InferredRoundStart, ...]:
    """Infer next-round starts strictly between consecutive completed results."""
    if maximum_start_multiplier < 1:
        raise ValueError("maximum_start_multiplier must be at least 1.")
    if minimum_seconds_after_completion < 0 or minimum_seconds_before_completion < 0:
        raise ValueError("minimum timing margins cannot be negative.")

    starts: list[InferredRoundStart] = []
    live = tuple(live_observations)

    for previous, target in zip(completed, completed[1:]):
        previous_at, _ = previous
        target_at, _ = target
        previous_ts = _timestamp(previous_at)
        target_ts = _timestamp(target_at)
        if target_ts <= previous_ts:
            continue

        lower = previous_ts + minimum_seconds_after_completion
        upper = target_ts - minimum_seconds_before_completion
        if upper <= lower:
            continue

        candidate = next(
            (
                observation
                for observation in live
                if lower <= observation.timestamp <= upper
                and observation.multiplier <= maximum_start_multiplier
            ),
            None,
        )
        if candidate is None:
            continue

        starts.append(
            InferredRoundStart(
                previous_completed_at=previous_at,
                target_completed_at=target_at,
                start_observed_at=candidate.observed_at,
                start_multiplier=candidate.multiplier,
                seconds_after_previous_completion=round(
                    candidate.timestamp - previous_ts, 6
                ),
                seconds_before_target_completion=round(
                    target_ts - candidate.timestamp, 6
                ),
            )
        )

    return tuple(starts)
