"""Probability forecasts for prospective PRE-ROUND snapshots.

Historical completed rounds provide the stable prior. Clean, already-scored
prospective snapshots can adjust that prior, but only when their outcomes were
known before the current snapshot lock. The current or future target result is
never available to this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Mapping, Sequence

from models.baseline import BaselineConfig, predict_thresholds


MODEL_NAME = "prospective-calibrated-threshold-probability"
MODEL_VERSION = "1"
DEFAULT_THRESHOLDS: tuple[Decimal, ...] = (
    Decimal("1.5"),
    Decimal("2"),
    Decimal("5"),
)
PROBABILITY_PLACES = Decimal("0.000001")
DEFAULT_PRIOR_STRENGTH = Decimal("20")
MIN_TRIGGER_COHORT = 6


@dataclass(frozen=True, slots=True)
class ProspectiveProbabilityForecast:
    model_name: str
    model_version: str
    status: str
    confidence: str
    historical_rounds: int
    scored_prospective_samples: int
    cohort_kind: str
    cohort_samples: int
    prior_strength: Decimal
    probabilities: tuple[tuple[Decimal, Decimal], ...]
    historical_probabilities: tuple[tuple[Decimal, Decimal], ...]

    def probability_map(self) -> dict[str, str]:
        return {
            str(threshold): format(probability, "f")
            for threshold, probability in self.probabilities
        }

    def historical_probability_map(self) -> dict[str, str]:
        return {
            str(threshold): format(probability, "f")
            for threshold, probability in self.historical_probabilities
        }


def _paired_scored_snapshots(
    ledger_events: Sequence[Mapping[str, object]],
) -> tuple[tuple[Mapping[str, object], Decimal], ...]:
    locks: dict[str, Mapping[str, object]] = {}
    invalidated: set[str] = set()
    pairs: list[tuple[Mapping[str, object], Decimal]] = []

    for event in ledger_events:
        kind = event.get("event")
        snapshot_id = event.get("snapshot_id")
        if kind == "pre_round_snapshot_locked" and isinstance(snapshot_id, str):
            locks[snapshot_id] = event
        elif kind == "pre_round_snapshot_invalidated" and isinstance(snapshot_id, str):
            invalidated.add(snapshot_id)
        elif kind == "pre_round_snapshot_scored" and isinstance(snapshot_id, str):
            if snapshot_id in invalidated:
                continue
            lock = locks.get(snapshot_id)
            actual = event.get("actual_multiplier")
            if lock is None or not isinstance(actual, str):
                continue
            value = Decimal(actual)
            if value.is_finite() and value >= 1:
                pairs.append((lock, value))

    return tuple(pairs)


def _cohort(
    pairs: Sequence[tuple[Mapping[str, object], Decimal]],
    current_snapshot: Mapping[str, object],
) -> tuple[str, tuple[tuple[Mapping[str, object], Decimal], ...]]:
    current_trigger = current_snapshot.get("trigger_signature")
    if isinstance(current_trigger, str):
        same_trigger = tuple(
            pair for pair in pairs
            if pair[0].get("trigger_signature") == current_trigger
        )
        if len(same_trigger) >= MIN_TRIGGER_COHORT:
            return "same_trigger", same_trigger
    return "global", tuple(pairs)


def _confidence(scored_samples: int, cohort_samples: int) -> str:
    # Confidence refers only to calibration sample support. It is not a claim
    # that the underlying game outcome is predictable.
    if scored_samples >= 150 and cohort_samples >= 50:
        return "HIGH"
    if scored_samples >= 60 and cohort_samples >= 20:
        return "MEDIUM"
    return "LOW"


def forecast_snapshot_probabilities(
    history_multipliers: Sequence[str | Decimal],
    ledger_events: Sequence[Mapping[str, object]],
    current_snapshot: Mapping[str, object],
    *,
    thresholds: Sequence[Decimal] = DEFAULT_THRESHOLDS,
    prior_strength: Decimal = DEFAULT_PRIOR_STRENGTH,
) -> ProspectiveProbabilityForecast:
    """Forecast threshold probabilities using only information known at lock time."""
    normalized = tuple(Decimal(str(value)) for value in thresholds)
    if not normalized or tuple(sorted(set(normalized))) != normalized:
        raise ValueError("thresholds must be unique and strictly increasing.")
    if prior_strength <= 0:
        raise ValueError("prior_strength must be positive.")

    baseline = predict_thresholds(
        history_multipliers,
        BaselineConfig(
            thresholds=normalized,
            min_history=min(20, max(1, len(history_multipliers))),
        ),
    )
    historical = tuple(
        (item.threshold, item.probability)
        for item in baseline.probabilities
        if item.probability is not None
    )
    if len(historical) != len(normalized):
        raise ValueError("historical prior probabilities are unavailable.")

    pairs = _paired_scored_snapshots(ledger_events)
    cohort_kind, cohort_pairs = _cohort(pairs, current_snapshot)

    probabilities: list[tuple[Decimal, Decimal]] = []
    previous_probability = Decimal("1")
    for threshold, base_probability in historical:
        successes = sum(
            actual >= threshold
            for _, actual in cohort_pairs
        )
        cohort_count = len(cohort_pairs)
        posterior = (
            (base_probability * prior_strength) + Decimal(successes)
        ) / (prior_strength + Decimal(cohort_count))
        posterior = posterior.quantize(
            PROBABILITY_PLACES,
            rounding=ROUND_HALF_EVEN,
        )
        # Nested multiplier thresholds require non-increasing probabilities.
        posterior = min(previous_probability, max(Decimal("0"), posterior))
        probabilities.append((threshold, posterior))
        previous_probability = posterior

    return ProspectiveProbabilityForecast(
        model_name=MODEL_NAME,
        model_version=MODEL_VERSION,
        status="READY",
        confidence=_confidence(len(pairs), len(cohort_pairs)),
        historical_rounds=len(history_multipliers),
        scored_prospective_samples=len(pairs),
        cohort_kind=cohort_kind,
        cohort_samples=len(cohort_pairs),
        prior_strength=prior_strength,
        probabilities=tuple(probabilities),
        historical_probabilities=tuple(
            (threshold, probability)
            for threshold, probability in historical
        ),
    )


def attach_forecast(
    snapshot: Mapping[str, object],
    forecast: ProspectiveProbabilityForecast,
) -> dict[str, object]:
    result = dict(snapshot)
    result["forecast"] = {
        "model_name": forecast.model_name,
        "model_version": forecast.model_version,
        "status": forecast.status,
        "confidence": forecast.confidence,
        "historical_rounds": forecast.historical_rounds,
        "scored_prospective_samples": forecast.scored_prospective_samples,
        "cohort_kind": forecast.cohort_kind,
        "cohort_samples": forecast.cohort_samples,
        "prior_strength": format(forecast.prior_strength, "f"),
        "probabilities": forecast.probability_map(),
        "historical_probabilities": forecast.historical_probability_map(),
    }
    return result


def score_forecast(
    snapshot: Mapping[str, object],
    actual_multiplier: Decimal,
) -> dict[str, object] | None:
    raw = snapshot.get("forecast")
    if not isinstance(raw, Mapping):
        return None
    probabilities = raw.get("probabilities")
    if not isinstance(probabilities, Mapping):
        return None

    scores: dict[str, object] = {}
    for threshold_text, probability_text in probabilities.items():
        if not isinstance(threshold_text, str) or not isinstance(probability_text, str):
            continue
        threshold = Decimal(threshold_text)
        probability = Decimal(probability_text)
        outcome = int(actual_multiplier >= threshold)
        brier = ((probability - Decimal(outcome)) ** 2).quantize(
            PROBABILITY_PLACES,
            rounding=ROUND_HALF_EVEN,
        )
        scores[threshold_text] = {
            "probability": probability_text,
            "actual": outcome,
            "brier": format(brier, "f"),
        }

    return {
        "model_name": raw.get("model_name"),
        "model_version": raw.get("model_version"),
        "threshold_scores": scores,
    }
