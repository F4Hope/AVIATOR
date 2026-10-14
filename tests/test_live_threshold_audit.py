"""Tests for prospective live threshold audit."""

from decimal import Decimal

from evaluation.live_threshold_audit import audit_live_thresholds


def _lock(lock_id: str, chosen: str, baseline: str, candidate: str | None, source: str):
    return {
        "event": "prediction_locked",
        "lock_id": lock_id,
        "threshold_probabilities": {"2": chosen},
        "threshold_model_details": {
            "2": {
                "source": source,
                "reason": "fixture",
                "baseline_probability": baseline,
                "raw_candidate_probability": candidate,
            }
        },
    }


def _score(lock_id: str, actual: str):
    return {
        "event": "prediction_scored",
        "lock_id": lock_id,
        "actual_multiplier": actual,
    }


def test_audit_compares_chosen_baseline_and_candidate_on_same_rounds() -> None:
    events = [
        _lock("a", "0.80", "0.60", "0.90", "gated_logistic_challenger"),
        _score("a", "3.0"),
        _lock("b", "0.20", "0.40", "0.10", "gated_logistic_challenger"),
        _score("b", "1.1"),
    ]

    report = audit_live_thresholds(events)
    item = report.thresholds[0]

    assert report.scored_locks == 2
    assert item.threshold == Decimal("2")
    assert item.challenger_selected == 2
    assert item.mean_chosen_brier == Decimal("0.040000")
    assert item.mean_baseline_brier == Decimal("0.160000")
    assert item.mean_raw_candidate_brier == Decimal("0.010000")
    assert item.chosen_skill_vs_baseline_pct == Decimal("75.000000")
    assert item.raw_candidate_skill_vs_baseline_pct == Decimal("93.750000")
    assert item.average_forecast == Decimal("0.500000")
    assert item.observed_rate == Decimal("0.500000")
    assert item.calibration_gap == Decimal("0.000000")


def test_audit_ignores_invalidated_lock_and_counts_prediction_gaps() -> None:
    events = [
        _lock("a", "0.70", "0.60", None, "historical_baseline"),
        {"event": "prediction_invalidated", "lock_id": "a"},
        _score("a", "3.0"),
        {"event": "prediction_gap", "unpredicted_rounds": 3},
    ]

    report = audit_live_thresholds(events)

    assert report.scored_locks == 0
    assert report.invalidated_locks == 1
    assert report.unpredicted_rounds == 3
    assert report.thresholds == ()


def test_audit_handles_legacy_lock_without_model_details() -> None:
    events = [
        {
            "event": "prediction_locked",
            "lock_id": "legacy",
            "threshold_probabilities": {"2": "0.500000"},
        },
        _score("legacy", "3.0"),
    ]

    report = audit_live_thresholds(events)
    item = report.thresholds[0]

    assert item.scored_forecasts == 1
    assert item.mean_chosen_brier == Decimal("0.250000")
    assert item.mean_baseline_brier is None
    assert item.comparable_candidate_rows == 0
