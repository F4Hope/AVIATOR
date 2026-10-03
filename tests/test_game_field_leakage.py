"""Tests for sanitized game-field capture and pre-round leakage audit."""

from decimal import Decimal

from collectors.network_probe import game_field_metadata, payload_metadata
from evaluation.game_field_leakage import (
    GameFieldObservation,
    audit_pre_round_game_fields,
)
from prediction.round_state import InferredRoundStart


def test_game_field_metadata_keeps_only_allowlisted_game_values() -> None:
    value = {
        "roundId": "abc-123",
        "multiplier": 2.75,
        "nested": {
            "coefficient": "4.20",
            "authorizationToken": "do-not-store",
            "email": "person@example.com",
        },
    }

    fields = game_field_metadata(value)
    paths = {item["path"]: item for item in fields}

    assert paths["roundid"]["kind"] == "identifier_hash"
    assert "sha256_24" in paths["roundid"]
    assert paths["multiplier"]["value"] == 2.75
    assert paths["nested.coefficient"]["value"] == 4.2
    assert all("authorization" not in item["path"] for item in fields)
    assert all("email" not in item["path"] for item in fields)


def test_payload_metadata_attaches_game_fields_without_raw_identifier() -> None:
    payload = '{"roundId":"secret-round","crashPoint":3.5}'
    metadata = payload_metadata(payload)

    assert metadata["format"] == "json"
    fields = metadata["game_fields"]
    assert isinstance(fields, list)
    serialized = str(fields)
    assert "secret-round" not in serialized
    assert "3.5" in serialized


def test_pre_round_audit_flags_repeated_direct_future_value() -> None:
    completed = (
        ("2026-10-03T00:00:00Z", Decimal("1.2")),
        ("2026-10-03T00:00:20Z", Decimal("2.5")),
        ("2026-10-03T00:00:40Z", Decimal("3.5")),
        ("2026-10-03T00:01:00Z", Decimal("4.5")),
        ("2026-10-03T00:01:20Z", Decimal("5.5")),
        ("2026-10-03T00:01:40Z", Decimal("6.5")),
    )
    starts = tuple(
        InferredRoundStart(
            previous_completed_at=completed[index - 1][0],
            target_completed_at=completed[index][0],
            start_observed_at=f"2026-10-03T00:0{(index * 20 - 10) // 60}:{(index * 20 - 10) % 60:02d}Z",
            start_multiplier=Decimal("1.01"),
            seconds_after_previous_completion=10.0,
            seconds_before_target_completion=10.0,
        )
        for index in range(1, len(completed))
    )

    # Use explicit timestamps to avoid depending on string arithmetic above.
    starts = (
        InferredRoundStart(completed[0][0], completed[1][0], "2026-10-03T00:00:10Z", Decimal("1.01"), 10.0, 10.0),
        InferredRoundStart(completed[1][0], completed[2][0], "2026-10-03T00:00:30Z", Decimal("1.01"), 10.0, 10.0),
        InferredRoundStart(completed[2][0], completed[3][0], "2026-10-03T00:00:50Z", Decimal("1.01"), 10.0, 10.0),
        InferredRoundStart(completed[3][0], completed[4][0], "2026-10-03T00:01:10Z", Decimal("1.01"), 10.0, 10.0),
        InferredRoundStart(completed[4][0], completed[5][0], "2026-10-03T00:01:30Z", Decimal("1.01"), 10.0, 10.0),
    )

    def ts(value: str) -> float:
        from datetime import datetime
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()

    observations = tuple(
        GameFieldObservation(
            observed_at=(
                "2026-10-03T00:00:05Z",
                "2026-10-03T00:00:25Z",
                "2026-10-03T00:00:45Z",
                "2026-10-03T00:01:05Z",
                "2026-10-03T00:01:25Z",
            )[index],
            timestamp=ts((
                "2026-10-03T00:00:05Z",
                "2026-10-03T00:00:25Z",
                "2026-10-03T00:00:45Z",
                "2026-10-03T00:01:05Z",
                "2026-10-03T00:01:25Z",
            )[index]),
            path="game.nextmultiplier",
            kind="numeric",
            numeric_value=float(completed[index + 1][1]),
            identifier_hash=None,
            direction="received",
            url="wss://example.test/game",
        )
        for index in range(5)
    )

    audit = audit_pre_round_game_fields(
        completed,
        starts,
        observations,
        safety_margin_seconds=0.5,
    )

    stat = next(item for item in audit.numeric_stats if item.path == "game.nextmultiplier")
    assert stat.rounds_observed == 5
    assert stat.last_exact_matches == 5
    assert stat.last_match_rate == 1.0
    assert stat.minimum_last_lead_seconds >= 5.0
