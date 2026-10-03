"""Tests for reconstructed priors and trigger-conditioned prospective audit."""

from decimal import Decimal

from evaluation.prospective_feature_audit import (
    load_prospective_feature_samples,
    reconstruct_missing_historical_priors,
    walk_forward_prospective_feature_audit,
)


def _lock(index: int, trigger: str) -> dict[str, object]:
    return {
        "event": "pre_round_snapshot_locked",
        "snapshot_id": f"s-{index}",
        "locked_at": f"2026-10-03T00:00:{index % 60:02d}Z",
        "previous_round_id": f"r-{index}",
        "previous_round_timestamp": f"2026-10-03T00:00:{index % 60:02d}Z",
        "trigger_signature": trigger,
        "trigger_score": 2.0,
        "trigger_age_after_previous_seconds": 2.0,
        "features": {
            "event_count_since_previous": 4,
            "received_count": 3,
            "sent_count": 1,
            "binary_count": 2,
            "total_payload_bytes": 300,
            "signature_counts": {trigger: 3},
            "transition_counts": {"a -> b": 2},
            "recent_sequence": [
                {"signature": trigger, "gap_ms": 150.0},
                {"signature": trigger, "gap_ms": 100.0},
            ],
        },
    }


def _score(index: int, actual: str) -> dict[str, object]:
    return {
        "event": "pre_round_snapshot_scored",
        "snapshot_id": f"s-{index}",
        "actual_multiplier": actual,
    }


def test_reconstructs_missing_prior_without_target_leakage() -> None:
    rounds = tuple(
        (
            f"r-{index}",
            f"2026-10-03T00:00:{index % 60:02d}Z",
            "2.5" if index % 2 == 0 else "1.1",
        )
        for index in range(30)
    )
    events = (_lock(25, "trigger-a"), _score(25, "9.0"))

    rebuilt, count = reconstruct_missing_historical_priors(events, rounds)

    assert count == 1
    lock = rebuilt[0]
    forecast = lock["forecast"]
    assert isinstance(forecast, dict)
    prior = forecast["historical_probabilities"]
    assert isinstance(prior, dict)
    # The scored target 9.0 is not part of the reconstruction history.
    assert prior["5"] == "0.000000"


def test_trigger_conditioned_challenger_uses_only_past_triggered_outcomes() -> None:
    events: list[dict[str, object]] = []
    rounds: list[tuple[str, str, str]] = []
    # Triggered rounds are strongly positive after a mixed historical baseline.
    for index in range(50):
        rounds.append(
            (
                f"r-{index}",
                f"2026-10-03T00:00:{index % 60:02d}Z",
                "2.5" if index % 2 == 0 else "1.1",
            )
        )
        lock = _lock(index, "trigger-a")
        lock["forecast"] = {
            "historical_probabilities": {
                "1.5": "0.500000",
                "2": "0.500000",
                "5": "0.100000",
            }
        }
        events.append(lock)
        events.append(_score(index, "2.5" if index >= 10 else "1.1"))

    samples = load_prospective_feature_samples(events, hash_bins=8)
    report = walk_forward_prospective_feature_audit(
        samples,
        min_training_samples=20,
        selection_window=6,
    )
    threshold = next(
        item for item in report.results
        if item.threshold == Decimal("1.5")
    )

    assert threshold.evaluated_targets == 30
    assert threshold.trigger_conditioned_brier < threshold.prior_brier
    assert threshold.trigger_conditioned_skill_vs_prior_pct > 0
