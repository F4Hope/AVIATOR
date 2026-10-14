"""Tests for the leakage-safe gated threshold challenger."""

from decimal import Decimal

from models.baseline import BaselineConfig
from prediction.threshold_challenger import (
    ThresholdChallengerConfig,
    predict_threshold_ensemble,
)


def test_challenger_falls_back_when_history_is_too_small() -> None:
    history = ["1.1", "3.0"] * 30
    prediction = predict_threshold_ensemble(
        history,
        BaselineConfig(thresholds=("1.5", "2", "5"), min_history=20),
    )

    assert prediction.status == "READY"
    assert all(
        decision.source == "historical_baseline"
        for decision in prediction.decisions
    )
    assert all(
        decision.reason == "insufficient_challenger_history"
        for decision in prediction.decisions
    )


def test_challenger_can_pass_gate_for_strong_chronological_signal() -> None:
    history = ["1.1", "3.0"] * 160
    prediction = predict_threshold_ensemble(
        history,
        BaselineConfig(thresholds=("2",), min_history=20),
        ThresholdChallengerConfig(
            min_history=120,
            min_train_rows=60,
            tune_rows=40,
            gate_rows=60,
            min_gate_rows=40,
            min_skill_pct=3.0,
        ),
    )

    decision = prediction.decisions[0]
    assert decision.source == "gated_logistic_challenger"
    assert decision.reason == "chronological_gate_passed"
    assert decision.candidate_skill_pct is not None
    assert decision.candidate_skill_pct > Decimal("3")
    assert decision.raw_candidate_probability is not None
    assert decision.probability is not None
    assert decision.baseline_probability is not None
    assert (
        abs(decision.probability - decision.baseline_probability)
        <= Decimal("0.100000")
    )


def test_challenger_preserves_nested_threshold_probabilities() -> None:
    history = ["1.1", "1.6", "2.5", "7.0"] * 90
    prediction = predict_threshold_ensemble(
        history,
        BaselineConfig(thresholds=("1.5", "2", "5"), min_history=20),
    )

    values = [
        decision.probability
        for decision in prediction.decisions
        if decision.probability is not None
    ]
    assert all(left >= right for left, right in zip(values, values[1:]))


def test_challenger_rejects_nonvarying_training_labels() -> None:
    history = ["3.0"] * 180
    prediction = predict_threshold_ensemble(
        history,
        BaselineConfig(thresholds=("2",), min_history=20),
    )

    decision = prediction.decisions[0]
    assert decision.source == "historical_baseline"
    assert decision.reason == "insufficient_train_class_variation"
    assert decision.probability == Decimal("1.000000")
