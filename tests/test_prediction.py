"""Phase 7 baseline-prediction tests with synthetic chronological fixtures only."""

from decimal import Decimal

import pytest

from evaluation.backtest import walk_forward_evaluate
from models.baseline import BaselineConfig, predict_thresholds


def test_expanding_baseline_probabilities_are_exact_and_monotone() -> None:
    config = BaselineConfig(thresholds=("1.5", "2", "5"), min_history=4)
    prediction = predict_thresholds(["1.2", "2.0", "6.0", "3.0"], config)
    assert prediction.status == "READY"
    assert prediction.sample_count == 4
    assert [item.probability for item in prediction.probabilities] == [
        Decimal("0.750000"),
        Decimal("0.750000"),
        Decimal("0.250000"),
    ]


def test_minimum_history_returns_no_probabilities() -> None:
    prediction = predict_thresholds(["2.0", "3.0"], BaselineConfig(min_history=3))
    assert prediction.status == "INSUFFICIENT_DATA"
    assert prediction.available_history == 2
    assert all(item.probability is None for item in prediction.probabilities)


def test_rolling_window_uses_only_most_recent_history() -> None:
    config = BaselineConfig(
        thresholds=("2",), mode="rolling", window=3, min_history=3
    )
    prediction = predict_thresholds(["10", "1", "1", "2"], config)
    assert prediction.sample_count == 3
    assert prediction.probabilities[0].probability == Decimal("0.333333")


@pytest.mark.parametrize(
    "config",
    [
        BaselineConfig,
    ],
)
def test_config_type_is_enforced(config: object) -> None:
    with pytest.raises(TypeError):
        predict_thresholds(["2"], config)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"thresholds": ()},
        {"thresholds": ("2", "1.5")},
        {"thresholds": ("2", "2")},
        {"thresholds": ("0.9",)},
        {"mode": "unknown"},
        {"min_history": 0},
        {"mode": "rolling", "window": None},
        {"mode": "rolling", "window": 2, "min_history": 3},
        {"mode": "expanding", "window": 3},
    ],
)
def test_invalid_baseline_config_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        BaselineConfig(**kwargs)


def test_walk_forward_never_uses_target_outcome_in_its_history() -> None:
    values = ["1", "1", "10"]
    result = walk_forward_evaluate(
        values, BaselineConfig(thresholds=("5",), min_history=2)
    )
    assert result.skipped_targets == 2
    assert result.evaluated_targets == 1
    # Before the third target, the only prior outcomes are 1x and 1x.
    assert result.predictions[2].probabilities[0].probability == Decimal("0.000000")
    assert result.threshold_scores[0].positives == 1
    assert result.threshold_scores[0].brier_score == Decimal("1.000000")


def test_walk_forward_metrics_match_known_fixture() -> None:
    result = walk_forward_evaluate(
        ["1", "3", "3", "1"],
        BaselineConfig(thresholds=("2",), min_history=2),
    )
    score = result.threshold_scores[0]
    assert result.evaluated_targets == 2
    assert result.skipped_targets == 2
    assert score.predictions == 2
    assert score.positives == 1
    assert score.brier_score == Decimal("0.347222")
    assert score.calibration_error == Decimal("0.083334")
