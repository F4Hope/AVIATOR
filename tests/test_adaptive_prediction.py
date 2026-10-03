"""Tests for adaptive leakage-safe point prediction."""

from decimal import Decimal

from prediction.adaptive import AdaptiveConfig, predict_next_multiplier


def _series(repeats: int = 12) -> list[str]:
    pattern = ["1.10", "1.35", "1.80", "2.40", "1.20", "3.20", "1.55", "5.50"]
    return pattern * repeats


def test_adaptive_predictor_returns_real_point_and_interval() -> None:
    values = _series()
    result = predict_next_multiplier(values)
    assert result.status == "READY"
    assert result.predicted_multiplier is not None
    assert result.predicted_multiplier >= Decimal("1")
    assert result.lower_multiplier is not None
    assert result.upper_multiplier is not None
    assert result.lower_multiplier <= result.predicted_multiplier <= result.upper_multiplier
    assert result.validation_rows >= 3
    assert result.components


def test_adaptive_prediction_is_deterministic() -> None:
    values = _series()
    first = predict_next_multiplier(values)
    second = predict_next_multiplier(values)
    assert first == second


def test_future_value_cannot_change_prediction_for_same_history() -> None:
    history = _series()
    low_future = history + ["1.00"]
    high_future = history + ["100.00"]
    low_prediction = predict_next_multiplier(low_future[:-1])
    high_prediction = predict_next_multiplier(high_future[:-1])
    assert low_prediction == high_prediction


def test_insufficient_history_returns_no_point_estimate() -> None:
    result = predict_next_multiplier(
        ["1.2"] * 19,
        AdaptiveConfig(min_history=20, long_window=20, short_window=8),
    )
    assert result.status == "INSUFFICIENT_DATA"
    assert result.predicted_multiplier is None


def test_recent_regime_shift_is_reported() -> None:
    values = ["1.10"] * 35 + ["4.00", "5.00", "6.00", "4.50", "5.50", "7.00", "4.20", "6.50"]
    result = predict_next_multiplier(values)
    assert result.status == "READY"
    assert result.regime == "HIGHER_RECENT_MULTIPLIERS"
    assert result.regime_shift is not None
    assert result.regime_shift > 0
