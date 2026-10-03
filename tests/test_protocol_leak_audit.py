"""Tests for direct sanitized protocol leakage auditing."""

from decimal import Decimal
import json
from pathlib import Path

from evaluation.protocol_leak_audit import (
    audit_protocol_leakage,
    load_game_field_observations,
)
from prediction.round_state import InferredRoundStart


def _event(
    observed_at: str,
    *,
    path: str,
    kind: str,
    value: object,
) -> dict[str, object]:
    field: dict[str, object] = {"path": path, "kind": kind}
    if kind == "numeric":
        field["value"] = value
    else:
        field["sha256_24"] = str(value)
    return {
        "observed_at": observed_at,
        "kind": "cdp_websocket_received",
        "url": "wss://example.invalid/game",
        "payload": {
            "sha256": observed_at,
            "format": "json",
            "game_fields": [field],
        },
    }


def _write(path: Path, events: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )


def test_loads_numeric_and_identifier_fields(tmp_path: Path) -> None:
    path = tmp_path / "probe.jsonl"
    _write(
        path,
        [
            _event(
                "2026-10-03T00:00:05Z",
                path="data.nextmultiplier",
                kind="numeric",
                value=2.5,
            ),
            _event(
                "2026-10-03T00:00:06Z",
                path="data.roundid",
                kind="identifier_hash",
                value="abc123",
            ),
        ],
    )

    observations = load_game_field_observations((path,))

    assert len(observations) == 2
    assert observations[0].numeric_value == Decimal("2.5")
    assert observations[1].identifier_hash == "abc123"


def test_flags_exact_numeric_field_before_inferred_start(tmp_path: Path) -> None:
    path = tmp_path / "probe.jsonl"
    _write(
        path,
        [
            _event(
                "2026-10-03T00:00:05Z",
                path="data.nextmultiplier",
                kind="numeric",
                value=2.5,
            ),
            _event(
                "2026-10-03T00:00:16Z",
                path="data.multiplier",
                kind="numeric",
                value=2.5,
            ),
        ],
    )

    completed = (
        ("2026-10-03T00:00:00Z", Decimal("1.2")),
        ("2026-10-03T00:00:20Z", Decimal("2.5")),
    )
    starts = (
        InferredRoundStart(
            previous_completed_at="2026-10-03T00:00:00Z",
            target_completed_at="2026-10-03T00:00:20Z",
            start_observed_at="2026-10-03T00:00:10Z",
            start_multiplier=Decimal("1.01"),
            seconds_after_previous_completion=10.0,
            seconds_before_target_completion=10.0,
        ),
    )

    report = audit_protocol_leakage(
        network_paths=(path,),
        completed_rounds=completed,
        inferred_starts=starts,
    )

    next_stat = next(
        item for item in report.numeric_paths
        if item.path == "data.nextmultiplier"
    )
    assert next_stat.exact_match_targets == 1
    assert next_stat.exact_matches_ge_5s == 1
    assert next_stat.exact_matches_before_inferred_start == 1
    assert next_stat.suspicious_outcome_path is True


def test_live_multiplier_near_result_is_not_prestart(tmp_path: Path) -> None:
    path = tmp_path / "probe.jsonl"
    _write(
        path,
        [
            _event(
                "2026-10-03T00:00:19.8Z",
                path="data.multiplier",
                kind="numeric",
                value=3.0,
            )
        ],
    )

    completed = (
        ("2026-10-03T00:00:00Z", Decimal("1.1")),
        ("2026-10-03T00:00:20Z", Decimal("3.0")),
    )
    starts = (
        InferredRoundStart(
            previous_completed_at="2026-10-03T00:00:00Z",
            target_completed_at="2026-10-03T00:00:20Z",
            start_observed_at="2026-10-03T00:00:08Z",
            start_multiplier=Decimal("1.02"),
            seconds_after_previous_completion=8.0,
            seconds_before_target_completion=12.0,
        ),
    )

    report = audit_protocol_leakage(
        network_paths=(path,),
        completed_rounds=completed,
        inferred_starts=starts,
    )

    stat = report.numeric_paths[0]
    assert stat.exact_match_targets == 1
    assert stat.exact_matches_ge_025s == 0
    assert stat.exact_matches_before_inferred_start == 0


def test_identifier_path_reports_prestart_coverage(tmp_path: Path) -> None:
    path = tmp_path / "probe.jsonl"
    _write(
        path,
        [
            _event(
                "2026-10-03T00:00:04Z",
                path="data.roundid",
                kind="identifier_hash",
                value="hash-a",
            ),
            _event(
                "2026-10-03T00:00:05Z",
                path="data.roundid",
                kind="identifier_hash",
                value="hash-b",
            ),
        ],
    )

    completed = (
        ("2026-10-03T00:00:00Z", Decimal("1.1")),
        ("2026-10-03T00:00:20Z", Decimal("2.2")),
    )
    starts = (
        InferredRoundStart(
            previous_completed_at="2026-10-03T00:00:00Z",
            target_completed_at="2026-10-03T00:00:20Z",
            start_observed_at="2026-10-03T00:00:10Z",
            start_multiplier=Decimal("1.01"),
            seconds_after_previous_completion=10.0,
            seconds_before_target_completion=10.0,
        ),
    )

    report = audit_protocol_leakage(
        network_paths=(path,),
        completed_rounds=completed,
        inferred_starts=starts,
    )

    stat = report.identifier_paths[0]
    assert stat.unique_hashes == 2
    assert stat.target_intervals_seen == 1
    assert stat.intervals_with_prestart_observation == 1
