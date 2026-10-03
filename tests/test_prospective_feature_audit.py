"""Tests for prospective feature walk-forward evaluation."""

from evaluation.prospective_feature_audit import (
    load_prospective_feature_samples,
    reconstruct_missing_historical_priors,
    walk_forward_prospective_feature_audit,
)


def _lock(index: int, positive: bool) -> dict[str, object]:
    return {
        "event": "pre_round_snapshot_locked",
        "snapshot_id": f"s-{index}",
        "locked_at": f"2026-10-03T00:{index // 60:02d}:{index % 60:02d}Z",
        "trigger_signature": "signal-high" if positive else "signal-low",
        "trigger_score": 3.0 if positive else 1.0,
        "trigger_age_after_previous_seconds": 4.0 if positive else 1.0,
        "features": {
            "event_count_since_previous": 20 if positive else 1,
            "received_count": 18 if positive else 1,
            "sent_count": 2 if positive else 0,
            "binary_count": 15 if positive else 0,
            "total_payload_bytes": 4000 if positive else 40,
            "signature_counts": {
                "high-token" if positive else "low-token": 8,
            },
            "transition_counts": {
                "a -> b" if positive else "c -> d": 5,
            },
            "recent_sequence": [
                {
                    "signature": "high-token" if positive else "low-token",
                    "gap_ms": 80.0 if positive else 900.0,
                }
            ],
        },
        "forecast": {
            "historical_probabilities": {
                "1.5": "0.500000",
                "2": "0.500000",
                "5": "0.100000",
            }
        },
    }


def _score(index: int, positive: bool) -> dict[str, object]:
    return {
        "event": "pre_round_snapshot_scored",
        "snapshot_id": f"s-{index}",
        "actual_multiplier": "2.5" if positive else "1.1",
    }


def test_loader_pairs_locked_features_with_later_scores() -> None:
    events: list[dict[str, object]] = []
    for index in range(12):
        positive = index % 2 == 0
        events.extend((_lock(index, positive), _score(index, positive)))

    samples = load_prospective_feature_samples(events, hash_bins=8)

    assert len(samples) == 12
    assert len(samples[0].features) > 20
    assert samples[0].snapshot_id == "s-0"


def test_walk_forward_feature_model_can_detect_synthetic_signal() -> None:
    events: list[dict[str, object]] = []
    for index in range(100):
        positive = index % 2 == 0
        events.extend((_lock(index, positive), _score(index, positive)))

    samples = load_prospective_feature_samples(events, hash_bins=8)
    report = walk_forward_prospective_feature_audit(
        samples,
        min_training_samples=30,
        selection_window=12,
    )

    assert report.status == "COMPLETE"
    threshold = next(
        result for result in report.results
        if str(result.threshold) == "1.5"
    )
    assert threshold.evaluated_targets == 70
    assert threshold.feature_brier < threshold.prior_brier
    assert threshold.feature_skill_vs_prior_pct > 0


def test_reconstructs_missing_prior_from_only_previous_round_history() -> None:
    rounds = tuple(
        (
            f"r-{index}",
            f"2026-10-03T00:00:{index:02d}Z",
            "2.5" if index % 2 == 0 else "1.1",
        )
        for index in range(10)
    )
    lock = _lock(1, True)
    lock.pop("forecast")
    lock["previous_round_id"] = "r-5"
    events = (lock, _score(1, True))

    rebuilt, count = reconstruct_missing_historical_priors(events, rounds)

    assert count == 1
    rebuilt_lock = rebuilt[0]
    forecast = rebuilt_lock["forecast"]
    assert isinstance(forecast, dict)
    probabilities = forecast["historical_probabilities"]
    assert isinstance(probabilities, dict)
    # History is r-0 through r-5 only: 3 of 6 are >=2x.
    assert probabilities["2"] == "0.500000"
    assert forecast["prior_reconstructed_for_audit"] is True


def test_trigger_conditioned_challenger_uses_only_prior_trigger_results() -> None:
    events: list[dict[str, object]] = []
    # Every triggered sample is positive while the stored historical prior is 0.5.
    for index in range(100):
        lock = _lock(index, True)
        lock["forecast"]["historical_probabilities"] = {
            "1.5": "0.500000",
            "2": "0.500000",
            "5": "0.100000",
        }
        events.extend((lock, _score(index, True)))

    samples = load_prospective_feature_samples(events, hash_bins=8)
    report = walk_forward_prospective_feature_audit(
        samples,
        min_training_samples=30,
        selection_window=12,
    )
    threshold = next(
        result for result in report.results
        if str(result.threshold) == "2"
    )

    assert threshold.trigger_conditioned_brier < threshold.prior_brier
    assert threshold.trigger_conditioned_skill_vs_prior_pct > 0
    assert threshold.trigger_conditioned_better_blocks >= 3
