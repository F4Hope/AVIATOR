"""Tests for online adaptive champion-challenger signal evaluation."""

from decimal import Decimal

from evaluation.adaptive_signal_audit import (
    _RLSProbability,
    walk_forward_adaptive_signal_audit,
)
from prediction.system_state import SystemStateSample


def _sample(index: int, actual: str, state_value: float) -> SystemStateSample:
    return SystemStateSample(
        target_index=index,
        cutoff_timestamp=f"2026-10-03T00:00:{index:02d}Z",
        target_timestamp=f"2026-10-03T00:01:{index:02d}Z",
        actual_multiplier=Decimal(actual),
        baseline_recent_median=Decimal("1.5"),
        state_features=(state_value,),
        history_features=(0.0,),
        longest_window_events=5,
    )


def test_rls_probability_updates_after_observation() -> None:
    model = _RLSProbability(dimension=1, forgetting_factor=0.95)
    before = model.predict((1.0,))
    model.update((1.0,), 1)
    after = model.predict((1.0,))
    assert 0 < before < 1
    assert after > before


def test_adaptive_signal_audit_can_select_state_signal() -> None:
    samples = []
    for index in range(80):
        positive = index % 2 == 0
        samples.append(
            _sample(
                index,
                "3.0" if positive else "1.2",
                1.0 if positive else -1.0,
            )
        )

    report = walk_forward_adaptive_signal_audit(
        samples,
        thresholds=(Decimal("2"),),
        min_training_samples=20,
        selection_window=8,
        forgetting_factors=(0.90, 1.00),
        blocks=4,
    )
    assert report.status == "COMPLETE"
    result = report.results[0]
    assert result.evaluated_targets == 60
    assert result.state_or_hybrid_selection_rate > Decimal("0")
    assert result.champion_brier < result.baseline_brier
    assert result.champion_better_blocks_than_history >= 1


def test_adaptive_signal_audit_reports_switching_under_regime_change() -> None:
    samples = []
    for index in range(100):
        if index < 50:
            positive = index % 2 == 0
            state = 1.0 if positive else -1.0
        else:
            positive = index % 2 == 0
            state = -1.0 if positive else 1.0
        samples.append(
            _sample(
                index,
                "4.0" if positive else "1.1",
                state,
            )
        )

    report = walk_forward_adaptive_signal_audit(
        samples,
        thresholds=(Decimal("2"),),
        min_training_samples=20,
        selection_window=8,
        forgetting_factors=(0.90, 0.95, 1.00),
        blocks=4,
    )
    result = report.results[0]
    assert result.evaluated_targets == 80
    assert result.champion_switches >= 1
    assert result.fast_forgetting_selection_rate >= Decimal("0")
