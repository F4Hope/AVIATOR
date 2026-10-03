"""Tests for prospective probability calibration and scoring."""

from decimal import Decimal

from prediction.prospective_forecast import (
    attach_forecast,
    forecast_snapshot_probabilities,
    score_forecast,
)


def _lock(
    snapshot_id: str,
    trigger: str,
    *,
    with_forecast: bool = False,
) -> dict[str, object]:
    value: dict[str, object] = {
        "event": "pre_round_snapshot_locked",
        "snapshot_id": snapshot_id,
        "trigger_signature": trigger,
        "trigger_age_after_previous_seconds": 3.0,
    }
    if with_forecast:
        value["forecast"] = {
            "model_name": "fixture",
            "model_version": "1",
            "probabilities": {
                "1.5": "0.600000",
                "2": "0.500000",
                "5": "0.200000",
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


def test_same_trigger_scored_samples_adjust_historical_prior() -> None:
    history = ["1.1"] * 10 + ["2.5"] * 10 + ["6"] * 10
    events: list[dict[str, object]] = []
    for index, actual in enumerate(
        ("3", "4", "1.1", "2.2", "1.2", "5.5", "2.1", "1.3")
    ):
        snapshot_id = f"s-{index}"
        events.append(_lock(snapshot_id, "trigger-a"))
        events.append(_score(snapshot_id, actual))

    current = _lock("current", "trigger-a")
    forecast = forecast_snapshot_probabilities(history, events, current)

    assert forecast.scored_prospective_samples == 8
    assert forecast.cohort_kind == "same_trigger"
    assert forecast.cohort_samples == 8
    assert forecast.probability_map() != forecast.historical_probability_map()


def test_forecast_probabilities_remain_nested() -> None:
    history = ["1.1", "1.2", "2.2", "8", "12"] * 6
    events: list[dict[str, object]] = []
    for index, actual in enumerate(("10", "1.1", "1.2", "6", "2.2", "1.3")):
        snapshot_id = f"s-{index}"
        events.append(_lock(snapshot_id, "trigger-z"))
        events.append(_score(snapshot_id, actual))

    forecast = forecast_snapshot_probabilities(
        history,
        events,
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
