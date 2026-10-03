"""Live prediction locking tests use only synthetic DOM fixtures."""

from __future__ import annotations

import json
from pathlib import Path

from config.settings import Settings
from models.baseline import BaselineConfig
from prediction.live import (
    invalidate_if_collector_session_changed,
    invalidate_outstanding_lock,
    process_live_prediction_cycle,
)


def _candidate(value: str, x: int) -> dict[str, object]:
    return {
        "multiplier": value,
        "x_bucket": x,
        "y_bucket": 50,
        "width_bucket": 40,
        "height_bucket": 20,
    }


def _append_snapshot(path: Path, stamp: str, values: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "kind": "dom_multiplier_snapshot",
        "observed_at": stamp,
        "candidates": [
            _candidate(value, 10 + index * 50)
            for index, value in enumerate(values)
        ],
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


def _ledger_events(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_live_cycle_locks_before_next_completed_result_and_scores_afterward(
    settings: Settings,
) -> None:
    dom = settings.raw_data_dir / "aviator-dom-multipliers.jsonl"
    ledger = settings.processed_data_dir / "live-prediction-ledger.jsonl"

    base = ("1.10", "1.20", "1.30", "1.40", "1.50", "1.60")
    first = ("1.20",) + base
    second = ("3.00",) + first

    _append_snapshot(dom, "2026-10-03T00:00:00Z", base)
    _append_snapshot(dom, "2026-10-03T00:00:10Z", first)

    config = BaselineConfig(thresholds=("2",), min_history=1)
    emitted = process_live_prediction_cycle(settings, dom, ledger, config)

    assert len(emitted) == 1
    lock = emitted[0]
    assert lock["event"] == "prediction_locked"
    assert lock["history_count"] == 1
    assert lock["threshold_probabilities"] == {"2": "0.000000"}

    _append_snapshot(dom, "2026-10-03T00:00:20Z", second)
    emitted = process_live_prediction_cycle(settings, dom, ledger, config)

    assert [event["event"] for event in emitted] == [
        "prediction_scored",
        "prediction_locked",
    ]
    score, next_lock = emitted
    assert score["actual_multiplier"] == "3"
    assert score["threshold_scores"] == {"2": {"actual": 1, "brier": "1.000000"}}
    assert next_lock["history_count"] == 2

    events = _ledger_events(ledger)
    assert [event["event"] for event in events] == [
        "prediction_locked",
        "prediction_scored",
        "prediction_locked",
    ]
    assert events[0]["locked_at"] < events[1]["scored_at"]


def test_live_cycle_does_not_retroactively_predict_missed_rounds(settings: Settings) -> None:
    dom = settings.raw_data_dir / "aviator-dom-multipliers.jsonl"
    ledger = settings.processed_data_dir / "live-prediction-ledger.jsonl"

    base = ("1.10", "1.20", "1.30", "1.40", "1.50", "1.60")
    first = ("2.00",) + base
    second = ("1.10",) + first
    third = ("4.00",) + second

    _append_snapshot(dom, "2026-10-03T00:00:00Z", base)
    _append_snapshot(dom, "2026-10-03T00:00:10Z", first)
    config = BaselineConfig(thresholds=("2",), min_history=1)
    process_live_prediction_cycle(settings, dom, ledger, config)

    _append_snapshot(dom, "2026-10-03T00:00:20Z", second)
    _append_snapshot(dom, "2026-10-03T00:00:30Z", third)
    emitted = process_live_prediction_cycle(settings, dom, ledger, config)

    assert [event["event"] for event in emitted] == [
        "prediction_scored",
        "prediction_gap",
        "prediction_locked",
    ]
    assert emitted[0]["actual_multiplier"] == "1.1"
    assert emitted[1]["unpredicted_rounds"] == 1
    assert emitted[2]["history_count"] == 3


def test_repeated_cycle_without_new_result_emits_nothing(settings: Settings) -> None:
    dom = settings.raw_data_dir / "aviator-dom-multipliers.jsonl"
    ledger = settings.processed_data_dir / "live-prediction-ledger.jsonl"

    base = ("1.10", "1.20", "1.30", "1.40", "1.50", "1.60")
    first = ("2.00",) + base
    _append_snapshot(dom, "2026-10-03T00:00:00Z", base)
    _append_snapshot(dom, "2026-10-03T00:00:10Z", first)

    config = BaselineConfig(thresholds=("2",), min_history=1)
    process_live_prediction_cycle(settings, dom, ledger, config)

    assert process_live_prediction_cycle(settings, dom, ledger, config) == ()



def test_invalidated_lock_is_never_scored(settings: Settings) -> None:
    dom = settings.raw_data_dir / "aviator-dom-multipliers.jsonl"
    ledger = settings.processed_data_dir / "live-prediction-ledger.jsonl"

    base = ("1.10", "1.20", "1.30", "1.40", "1.50", "1.60")
    first = ("2.00",) + base
    second = ("3.00",) + first
    _append_snapshot(dom, "2026-10-03T00:00:00Z", base)
    _append_snapshot(dom, "2026-10-03T00:00:10Z", first)

    config = BaselineConfig(thresholds=("2",), min_history=1)
    locked = process_live_prediction_cycle(settings, dom, ledger, config)
    assert locked[0]["event"] == "prediction_locked"

    invalidated = invalidate_outstanding_lock(ledger)
    assert invalidated is not None
    assert invalidated["event"] == "prediction_invalidated"

    _append_snapshot(dom, "2026-10-03T00:00:20Z", second)
    emitted = process_live_prediction_cycle(settings, dom, ledger, config)

    assert [event["event"] for event in emitted] == ["prediction_locked"]
    assert all(event["event"] != "prediction_scored" for event in emitted)


def test_repeated_invalidation_is_idempotent(settings: Settings) -> None:
    dom = settings.raw_data_dir / "aviator-dom-multipliers.jsonl"
    ledger = settings.processed_data_dir / "live-prediction-ledger.jsonl"

    base = ("1.10", "1.20", "1.30", "1.40", "1.50", "1.60")
    first = ("2.00",) + base
    _append_snapshot(dom, "2026-10-03T00:00:00Z", base)
    _append_snapshot(dom, "2026-10-03T00:00:10Z", first)
    process_live_prediction_cycle(
        settings, dom, ledger, BaselineConfig(thresholds=("2",), min_history=1)
    )

    assert invalidate_outstanding_lock(ledger) is not None
    assert invalidate_outstanding_lock(ledger) is None



def test_collector_session_change_invalidates_old_lock(settings: Settings) -> None:
    dom = settings.raw_data_dir / "aviator-dom-multipliers.jsonl"
    ledger = settings.processed_data_dir / "live-prediction-ledger.jsonl"

    base = ("1.10", "1.20", "1.30", "1.40", "1.50", "1.60")
    first = ("2.00",) + base
    _append_snapshot(dom, "2026-10-03T00:00:00Z", base)
    _append_snapshot(dom, "2026-10-03T00:00:10Z", first)

    process_live_prediction_cycle(
        settings,
        dom,
        ledger,
        BaselineConfig(thresholds=("2",), min_history=1),
        "collector-session-a",
    )

    assert invalidate_if_collector_session_changed(
        ledger, "collector-session-a"
    ) is None
    event = invalidate_if_collector_session_changed(
        ledger, "collector-session-b"
    )
    assert event is not None
    assert event["event"] == "prediction_invalidated"
    assert event["reason"] == "collector_session_changed"
