"""Tests for fast gated prospective probability forecasting."""

from decimal import Decimal

from prediction.prospective_forecast import (
    attach_forecast,
    forecast_snapshot_probabilities,
    instant_prior_event,
    score_forecast,
)


def _lock(
    snapshot_id: str,
    trigger: str,
    *,
    forecast_probability: str | None = None,
    prior_probability: str | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "event": "pre_round_snapshot_locked",
        "snapshot_id": snapshot_id,
        "trigger_signature": trigger,
        "trigger_age_after_previous_seconds": 3.0,
    }
    if forecast_probability is not None and prior_probability is not None:
        value["forecast"] = {
            "model_name": "fixture",
            "model_version": "1",
            "probabilities": {
                "1.5": forecast_probability,
                "2": forecast_probability,
                "5": "0.100000",
            },
            "historical_probabilities": {
                "1.5": prior_probability,
                "2": prior_probability,
                "5": "0.100000",
            },
        }
    return value


def _score(snapshot_id: str, actual: str) -> dict[str, object]:
    return {
        "event": "pre_round_snapshot_scored",
        "snapshot_id": snapshot_id,
        "actual_multiplier": actual,
    }


def test_without_prospective_scores_forecast_equals_historical_prior() -> None:
    history = ["1.2", "2.5", "1.6", "6", "3", "1.1"] * 5
    current = _lock("current", "trigger-a")

    forecast = forecast_snapshot_probabilities(history, (), current)

    assert forecast.scored_prospective_samples == 0
    assert forecast.cohort_kind == "global"
    assert forecast.probability_map() == forecast.historical_probability_map()
    assert all(gate.source == "historical_prior" for gate in forecast.gates)


def test_small_trigger_cohort_cannot_override_historical_prior() -> None:
    history = ["1.1"] * 10 + ["2.5"] * 10 + ["6"] * 10
    events: list[dict[str, object]] = []
    for index, actual in enumerate(
        ("3", "4", "1.1", "2.2", "1.2", "5.5", "2.1", "1.3")
    ):
        snapshot_id = f"s-{index}"
        events.append(_lock(snapshot_id, "trigger-a"))
        events.append(_score(snapshot_id, actual))

    forecast = forecast_snapshot_probabilities(
        history,
        events,
        _lock("current", "trigger-a"),
    )

    assert forecast.scored_prospective_samples == 8
    assert forecast.cohort_kind == "same_trigger"
    assert forecast.cohort_samples == 8
    assert forecast.probability_map() == forecast.historical_probability_map()
    assert all(gate.validation_samples == 0 for gate in forecast.gates)


def test_forward_brier_gate_allows_proven_challenger() -> None:
    history = ["1.1"] * 10 + ["2.5"] * 20
    events: list[dict[str, object]] = []
    for index in range(20):
        snapshot_id = f"s-{index}"
        events.append(
            _lock(
                snapshot_id,
                "trigger-a",
                forecast_probability="0.900000",
                prior_probability="0.500000",
            )
        )
        events.append(_score(snapshot_id, "3"))

    forecast = forecast_snapshot_probabilities(
        history,
        events,
        _lock("current", "trigger-a"),
    )
    gate = next(item for item in forecast.gates if item.threshold == Decimal("1.5"))

    assert gate.validation_samples == 20
    assert gate.source == "prospective_adjusted"
    assert gate.candidate_skill_vs_prior_pct is not None
    assert gate.candidate_skill_vs_prior_pct > Decimal("5")
    assert (
        forecast.probability_map()["1.5"]
        == forecast.candidate_probability_map()["1.5"]
    )


def test_forward_brier_gate_rejects_worse_challenger() -> None:
    history = ["1.1"] * 10 + ["2.5"] * 20
    events: list[dict[str, object]] = []
    for index in range(20):
        snapshot_id = f"s-{index}"
        events.append(
            _lock(
                snapshot_id,
                "trigger-a",
                forecast_probability="0.100000",
                prior_probability="0.700000",
            )
        )
        events.append(_score(snapshot_id, "3"))

    forecast = forecast_snapshot_probabilities(
        history,
        events,
        _lock("current", "trigger-a"),
    )
    gate = next(item for item in forecast.gates if item.threshold == Decimal("1.5"))

    assert gate.validation_samples == 20
    assert gate.source == "historical_prior"
    assert (
        forecast.probability_map()["1.5"]
        == forecast.historical_probability_map()["1.5"]
    )


def test_instant_prior_event_is_available_without_trigger() -> None:
    history = ["1.2", "2.5", "1.6", "6", "3"] * 6
    event = instant_prior_event(
        previous_round_id="round-a",
        previous_round_timestamp="2026-10-03T00:00:10.000000Z",
        history_multipliers=history,
        observed_at="2026-10-03T00:00:10.100000Z",
    )

    assert event["event"] == "pre_round_prior_forecast"
    assert event["previous_round_id"] == "round-a"
    probabilities = event["probabilities"]
    assert isinstance(probabilities, dict)
    assert set(probabilities) == {"1.5", "2", "5"}


def test_forecast_probabilities_remain_nested() -> None:
    history = ["1.1", "1.2", "2.2", "8", "12"] * 6
    forecast = forecast_snapshot_probabilities(
        history,
        (),
        _lock("current", "trigger-z"),
    )
    values = [
        Decimal(value)
        for value in forecast.probability_map().values()
    ]
    assert all(left >= right for left, right in zip(values, values[1:]))


def test_attached_forecast_is_scored_with_brier() -> None:
    history = ["1.2", "2.5", "1.6", "6", "3"] * 6
    snapshot = _lock("current", "trigger-a")
    forecast = forecast_snapshot_probabilities(history, (), snapshot)
    enriched = attach_forecast(snapshot, forecast)

    score = score_forecast(enriched, Decimal("3"))
    assert score is not None
    threshold_scores = score["threshold_scores"]
    assert isinstance(threshold_scores, dict)
    assert threshold_scores["1.5"]["actual"] == 1
    assert threshold_scores["2"]["actual"] == 1
    assert threshold_scores["5"]["actual"] == 0
    assert all(
        "brier" in value
        for value in threshold_scores.values()
        if isinstance(value, dict)
    )
