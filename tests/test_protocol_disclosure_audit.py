"""Tests for sanitized protocol-state capture and pre-round exposure audit."""

from datetime import UTC, datetime
from decimal import Decimal

from collectors.network_probe import payload_metadata
from evaluation.protocol_disclosure_audit import (
    ProtocolEvent,
    audit_protocol_disclosures,
)
from prediction.round_state import InferredRoundStart


BASE = datetime(2026, 10, 3, tzinfo=UTC).timestamp()


def _stamp(offset: float) -> str:
    return datetime.fromtimestamp(BASE + offset, tz=UTC).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _start(index: int) -> InferredRoundStart:
    previous = index * 30.0
    start = previous + 10.0
    target = previous + 20.0
    return InferredRoundStart(
        previous_completed_at=_stamp(previous),
        target_completed_at=_stamp(target),
        start_observed_at=_stamp(start),
        start_multiplier=Decimal("1.02"),
        seconds_after_previous_completion=10.0,
        seconds_before_target_completion=10.0,
    )


def _event(
    offset: float,
    *,
    numeric: float | None = None,
    category: str | None = None,
) -> ProtocolEvent:
    game_fields = ()
    if numeric is not None:
        game_fields = (
            {
                "path": "game.nextmultiplier",
                "kind": "numeric",
                "value": numeric,
            },
        )
    categories = ()
    if category is not None:
        categories = (
            {
                "path": "game.phase",
                "value": category,
            },
        )
    return ProtocolEvent(
        timestamp=BASE + offset,
        observed_at=_stamp(offset),
        kind="cdp_websocket_received",
        url="wss://example.test/game",
        payload_sha256=f"sha-{offset}",
        game_fields=game_fields,
        categorical_fields=categories,
    )


def test_payload_metadata_extracts_safe_protocol_categories() -> None:
    import json

    payload = json.dumps(
        {
            "event": "ROUND_START",
            "game": {"state": "WAITING", "phase": "BETTING"},
            "token": "must-not-survive",
        }
    )
    result = payload_metadata(payload)
    categories = {
        (item["path"], item["value"])
        for item in result["categorical_fields"]
    }
    assert ("event", "round_start") in categories
    assert ("game.state", "waiting") in categories
    assert ("game.phase", "betting") in categories
    assert "must-not-survive" not in str(result)


def test_future_numeric_field_is_flagged_only_after_repeated_matches() -> None:
    starts = tuple(_start(index) for index in range(4))
    events = tuple(
        _event(index * 30.0 + 6.0, numeric=3.0)
        for index in range(4)
    )
    targets = {
        start.target_completed_at: Decimal("3.0")
        for start in starts
    }
    previous = {
        start.previous_completed_at: Decimal("1.2")
        for start in starts
    }

    report = audit_protocol_disclosures(
        starts=starts,
        events=events,
        target_multipliers=targets,
        previous_multipliers=previous,
        safety_margin_seconds=2.0,
    )

    assert report.numeric_fields
    field = report.numeric_fields[0]
    assert field.target_matches == 4
    assert field.previous_matches == 0
    assert field.verdict == "CANDIDATE_PRE_ROUND_DISCLOSURE"
    assert field.minimum_lead_seconds == 4.0


def test_previous_result_echo_is_not_future_disclosure() -> None:
    starts = tuple(_start(index) for index in range(4))
    events = tuple(
        _event(index * 30.0 + 6.0, numeric=1.2)
        for index in range(4)
    )
    targets = {
        start.target_completed_at: Decimal("3.0")
        for start in starts
    }
    previous = {
        start.previous_completed_at: Decimal("1.2")
        for start in starts
    }

    report = audit_protocol_disclosures(
        starts=starts,
        events=events,
        target_multipliers=targets,
        previous_multipliers=previous,
        safety_margin_seconds=2.0,
    )

    field = report.numeric_fields[0]
    assert field.target_matches == 0
    assert field.previous_matches == 4
    assert field.verdict == "NO_DIRECT_DISCLOSURE"


def test_categorical_state_uses_discovery_then_holdout() -> None:
    starts = tuple(_start(index) for index in range(12))
    events = []
    targets = {}
    previous = {}
    for index, start in enumerate(starts):
        positive = index % 2 == 0
        targets[start.target_completed_at] = Decimal("2.5" if positive else "1.1")
        previous[start.previous_completed_at] = Decimal("1.2")
        if positive:
            events.append(_event(index * 30.0 + 5.0, category="armed"))
        else:
            events.append(_event(index * 30.0 + 5.0, category="idle"))

    report = audit_protocol_disclosures(
        starts=starts,
        events=tuple(events),
        target_multipliers=targets,
        previous_multipliers=previous,
        safety_margin_seconds=2.0,
        discovery_fraction=0.5,
        minimum_category_support=2,
    )

    candidate = next(
        item
        for item in report.categorical_holdout
        if item.threshold == Decimal("2") and item.token == "game.phase=armed"
    )
    assert candidate.discovery_lift > 0
    assert candidate.holdout_lift is not None
    assert candidate.holdout_lift > 0
    assert candidate.direction_consistent is True
