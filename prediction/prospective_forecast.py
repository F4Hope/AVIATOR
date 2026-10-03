"""Fast, leakage-safe probability forecasts for prospective PRE-ROUND capture.

A historical threshold-frequency forecast is available immediately after the
previous completed round. Trigger-specific calibration is treated as a
challenger: it may replace the historical prior for a threshold only after its
own previously locked forecasts have demonstrated better forward Brier loss.
The current target result is never available at forecast time.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Mapping, Sequence

from models.baseline import (
    MODEL_NAME as BASELINE_MODEL_NAME,
    MODEL_VERSION as BASELINE_MODEL_VERSION,
    BaselineConfig,
    predict_thresholds,
)


MODEL_NAME = "gated-prospective-threshold-probability"
MODEL_VERSION = "2"
DEFAULT_THRESHOLDS: tuple[Decimal, ...] = (
    Decimal("1.5"),
    Decimal("2"),
    Decimal("5"),
)
PROBABILITY_PLACES = Decimal("0.000001")
DEFAULT_PRIOR_STRENGTH = Decimal("20")
MIN_TRIGGER_COHORT = 6
GATE_MIN_COMPARISONS = 16
GATE_WINDOW = 60
GATE_MIN_SKILL_PCT = Decimal("5")


@dataclass(frozen=True, slots=True)
class ThresholdGate:
    threshold: Decimal
    source: str
    validation_samples: int
    candidate_brier: Decimal | None
    prior_brier: Decimal | None
    candidate_skill_vs_prior_pct: Decimal | None


@dataclass(frozen=True, slots=True)
class ProspectiveProbabilityForecast:
    model_name: str
    model_version: str
    status: str
    calibration_support: str
    historical_rounds: int
    scored_prospective_samples: int
    cohort_kind: str
    cohort_samples: int
    prior_strength: Decimal
    probabilities: tuple[tuple[Decimal, Decimal], ...]
    candidate_probabilities: tuple[tuple[Decimal, Decimal], ...]
    historical_probabilities: tuple[tuple[Decimal, Decimal], ...]
    gates: tuple[ThresholdGate, ...]

    def probability_map(self) -> dict[str, str]:
        return {
            str(threshold): format(probability, "f")
            for threshold, probability in self.probabilities
        }

    def candidate_probability_map(self) -> dict[str, str]:
        return {
            str(threshold): format(probability, "f")
            for threshold, probability in self.candidate_probabilities
        }

    def historical_probability_map(self) -> dict[str, str]:
        return {
            str(threshold): format(probability, "f")
            for threshold, probability in self.historical_probabilities
        }

    def gate_map(self) -> dict[str, dict[str, object]]:
        result: dict[str, dict[str, object]] = {}
        for gate in self.gates:
            result[str(gate.threshold)] = {
                "source": gate.source,
                "validation_samples": gate.validation_samples,
                "candidate_brier": (
                    None
                    if gate.candidate_brier is None
                    else format(gate.candidate_brier, "f")
                ),
                "prior_brier": (
                    None
                    if gate.prior_brier is None
                    else format(gate.prior_brier, "f")
                ),
                "candidate_skill_vs_prior_pct": (
                    None
                    if gate.candidate_skill_vs_prior_pct is None
                    else format(gate.candidate_skill_vs_prior_pct, "f")
                ),
            }
        return result


def historical_prior_probabilities(
    history_multipliers: Sequence[str | Decimal],
    *,
    thresholds: Sequence[Decimal] = DEFAULT_THRESHOLDS,
) -> tuple[tuple[Decimal, Decimal], ...]:
    """Return the immediately available historical threshold prior."""
    normalized = tuple(Decimal(str(value)) for value in thresholds)
    if not normalized or tuple(sorted(set(normalized))) != normalized:
        raise ValueError("thresholds must be unique and strictly increasing.")
    if not history_multipliers:
        raise ValueError("historical prior requires at least one completed round.")

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
    return tuple(
        (threshold, probability)
        for threshold, probability in historical
        if probability is not None
    )


def instant_prior_event(
    *,
    previous_round_id: str,
    previous_round_timestamp: str,
    history_multipliers: Sequence[str | Decimal],
    observed_at: str,
    thresholds: Sequence[Decimal] = DEFAULT_THRESHOLDS,
) -> dict[str, object]:
    """Create an auditable forecast available immediately at the round boundary."""
    probabilities = historical_prior_probabilities(
        history_multipliers,
        thresholds=thresholds,
    )
    return {
        "event": "pre_round_prior_forecast",
        "observed_at": observed_at,
        "previous_round_id": previous_round_id,
        "previous_round_timestamp": previous_round_timestamp,
        "model_name": BASELINE_MODEL_NAME,
        "model_version": BASELINE_MODEL_VERSION,
        "historical_rounds": len(history_multipliers),
        "timing_guarantee": "issued_immediately_after_previous_completed_observation",
        "probabilities": {
            str(threshold): format(probability, "f")
            for threshold, probability in probabilities
        },
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


def _calibration_support(scored_samples: int, cohort_samples: int) -> str:
    """Describe sample support only; this is not outcome confidence."""
    if scored_samples >= 150 and cohort_samples >= 50:
        return "HIGH"
    if scored_samples >= 60 and cohort_samples >= 20:
        return "MEDIUM"
    return "LOW"


def _decimal_probability(
    mapping: object,
    threshold: Decimal,
) -> Decimal | None:
    if not isinstance(mapping, Mapping):
        return None
    value = mapping.get(str(threshold))
    if not isinstance(value, str):
        return None
    try:
        probability = Decimal(value)
    except Exception:
        return None
    if not probability.is_finite() or not Decimal("0") <= probability <= Decimal("1"):
        return None
    return probability


def _gate_candidate(
    cohort_pairs: Sequence[tuple[Mapping[str, object], Decimal]],
    threshold: Decimal,
) -> ThresholdGate:
    """Evaluate stored challenger forecasts against their stored prior forecasts."""
    comparisons: list[tuple[Decimal, Decimal, int]] = []
    for lock, actual in cohort_pairs:
        raw = lock.get("forecast")
        if not isinstance(raw, Mapping):
            continue

        candidate = _decimal_probability(
            raw.get("candidate_probabilities", raw.get("probabilities")),
            threshold,
        )
        prior = _decimal_probability(raw.get("historical_probabilities"), threshold)
        if candidate is None or prior is None:
            continue
        outcome = int(actual >= threshold)
        comparisons.append((candidate, prior, outcome))

    comparisons = comparisons[-GATE_WINDOW:]
    count = len(comparisons)
    if count < GATE_MIN_COMPARISONS:
        return ThresholdGate(
            threshold=threshold,
            source="historical_prior",
            validation_samples=count,
            candidate_brier=None,
            prior_brier=None,
            candidate_skill_vs_prior_pct=None,
        )

    candidate_brier = sum(
        (candidate - Decimal(outcome)) ** 2
        for candidate, _, outcome in comparisons
    ) / Decimal(count)
    prior_brier = sum(
        (prior - Decimal(outcome)) ** 2
        for _, prior, outcome in comparisons
    ) / Decimal(count)
    skill = (
        (prior_brier - candidate_brier) / prior_brier * Decimal("100")
        if prior_brier > 0
        else Decimal("0")
    )

    candidate_brier = candidate_brier.quantize(
        PROBABILITY_PLACES, rounding=ROUND_HALF_EVEN
    )
    prior_brier = prior_brier.quantize(
        PROBABILITY_PLACES, rounding=ROUND_HALF_EVEN
    )
    skill = skill.quantize(PROBABILITY_PLACES, rounding=ROUND_HALF_EVEN)
    source = (
        "prospective_adjusted"
        if skill >= GATE_MIN_SKILL_PCT
        else "historical_prior"
    )
    return ThresholdGate(
        threshold=threshold,
        source=source,
        validation_samples=count,
        candidate_brier=candidate_brier,
        prior_brier=prior_brier,
        candidate_skill_vs_prior_pct=skill,
    )


def forecast_snapshot_probabilities(
    history_multipliers: Sequence[str | Decimal],
    ledger_events: Sequence[Mapping[str, object]],
    current_snapshot: Mapping[str, object],
    *,
    thresholds: Sequence[Decimal] = DEFAULT_THRESHOLDS,
    prior_strength: Decimal = DEFAULT_PRIOR_STRENGTH,
) -> ProspectiveProbabilityForecast:
    """Forecast probabilities using only information known before the target result."""
    normalized = tuple(Decimal(str(value)) for value in thresholds)
    if not normalized or tuple(sorted(set(normalized))) != normalized:
        raise ValueError("thresholds must be unique and strictly increasing.")
    if prior_strength <= 0:
        raise ValueError("prior_strength must be positive.")

    historical = historical_prior_probabilities(
        history_multipliers,
        thresholds=normalized,
    )
    historical_map = dict(historical)

    pairs = _paired_scored_snapshots(ledger_events)
    cohort_kind, cohort_pairs = _cohort(pairs, current_snapshot)

    candidates: list[tuple[Decimal, Decimal]] = []
    for threshold in normalized:
        base_probability = historical_map[threshold]
        successes = sum(actual >= threshold for _, actual in cohort_pairs)
        cohort_count = len(cohort_pairs)
        posterior = (
            (base_probability * prior_strength) + Decimal(successes)
        ) / (prior_strength + Decimal(cohort_count))
        posterior = posterior.quantize(
            PROBABILITY_PLACES,
            rounding=ROUND_HALF_EVEN,
        )
        candidates.append(
            (threshold, min(Decimal("1"), max(Decimal("0"), posterior)))
        )

    gates = tuple(
        _gate_candidate(cohort_pairs, threshold)
        for threshold in normalized
    )
    candidate_map = dict(candidates)

    selected: list[tuple[Decimal, Decimal]] = []
    previous_probability = Decimal("1")
    for gate in gates:
        probability = (
            candidate_map[gate.threshold]
            if gate.source == "prospective_adjusted"
            else historical_map[gate.threshold]
        )
        probability = min(previous_probability, probability)
        selected.append((gate.threshold, probability))
        previous_probability = probability

    return ProspectiveProbabilityForecast(
        model_name=MODEL_NAME,
        model_version=MODEL_VERSION,
        status="READY",
        calibration_support=_calibration_support(len(pairs), len(cohort_pairs)),
        historical_rounds=len(history_multipliers),
        scored_prospective_samples=len(pairs),
        cohort_kind=cohort_kind,
        cohort_samples=len(cohort_pairs),
        prior_strength=prior_strength,
        probabilities=tuple(selected),
        candidate_probabilities=tuple(candidates),
        historical_probabilities=tuple(historical),
        gates=gates,
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
        "calibration_support": forecast.calibration_support,
        # Retain compatibility with earlier ledgers; semantics are explicitly
        # sample support, not probability that the target forecast is correct.
        "confidence": forecast.calibration_support,
        "historical_rounds": forecast.historical_rounds,
        "scored_prospective_samples": forecast.scored_prospective_samples,
        "cohort_kind": forecast.cohort_kind,
        "cohort_samples": forecast.cohort_samples,
        "prior_strength": format(forecast.prior_strength, "f"),
        "probabilities": forecast.probability_map(),
        "candidate_probabilities": forecast.candidate_probability_map(),
        "historical_probabilities": forecast.historical_probability_map(),
        "gates": forecast.gate_map(),
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
