"""Chronological walk-forward evaluation for leakage-safe threshold baselines."""

from dataclasses import dataclass
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from typing import Iterable, Sequence

from models.baseline import (
    BaselineConfig,
    BaselinePrediction,
    ThresholdProbability,
    predict_thresholds,
)


METRIC_PLACES = Decimal("0.000001")
ARITHMETIC_PRECISION = 50


@dataclass(frozen=True, slots=True)
class ThresholdScore:
    threshold: Decimal
    predictions: int
    positives: int
    brier_score: Decimal | None
    calibration_error: Decimal | None


@dataclass(frozen=True, slots=True)
class WalkForwardResult:
    """Chronological replay result; no target outcome is included in its own history."""

    evaluated_targets: int
    skipped_targets: int
    threshold_scores: tuple[ThresholdScore, ...]
    predictions: tuple[BaselinePrediction, ...]


def _decimal_multiplier(value: Decimal | str | int | float) -> Decimal:
    multiplier = Decimal(str(value))
    if not multiplier.is_finite() or multiplier < 1:
        raise ValueError("multipliers must be finite decimal values of at least 1.")
    return multiplier


def walk_forward_evaluate(
    multipliers: Sequence[Decimal | str | int | float]
    | Iterable[Decimal | str | int | float],
    config: BaselineConfig | None = None,
) -> WalkForwardResult:
    """Evaluate in chronological order using history ending strictly before each target."""
    active = config if config is not None else BaselineConfig()
    values = tuple(_decimal_multiplier(value) for value in multipliers)
    replay_predictions: list[BaselinePrediction] = []
    errors: list[list[Decimal]] = [[] for _ in active.thresholds]
    probabilities: list[list[Decimal]] = [[] for _ in active.thresholds]
    outcomes: list[list[Decimal]] = [[] for _ in active.thresholds]
    skipped = 0

    for index, actual in enumerate(values):
        prediction = predict_thresholds(values[:index], active)
        replay_predictions.append(prediction)
        if prediction.status != "READY":
            skipped += 1
            continue
        for threshold_index, item in enumerate(prediction.probabilities):
            probability = item.probability
            if probability is None:
                raise RuntimeError("ready predictions must contain probabilities.")
            outcome = Decimal(1 if actual >= item.threshold else 0)
            errors[threshold_index].append((probability - outcome) ** 2)
            probabilities[threshold_index].append(probability)
            outcomes[threshold_index].append(outcome)

    scores: list[ThresholdScore] = []
    with localcontext(Context(prec=ARITHMETIC_PRECISION, rounding=ROUND_HALF_EVEN)):
        for threshold, threshold_errors, probs, ys in zip(
            active.thresholds, errors, probabilities, outcomes
        ):
            count = len(threshold_errors)
            if count:
                mean_error = (sum(threshold_errors) / count).quantize(METRIC_PLACES)
                mean_probability = sum(probs) / count
                observed_rate = sum(ys) / count
                calibration_error = abs(mean_probability - observed_rate).quantize(
                    METRIC_PLACES
                )
                positives = int(sum(ys))
            else:
                mean_error = None
                calibration_error = None
                positives = 0
            scores.append(
                ThresholdScore(
                    threshold=threshold,
                    predictions=count,
                    positives=positives,
                    brier_score=mean_error,
                    calibration_error=calibration_error,
                )
            )

    evaluated = len(values) - skipped
    return WalkForwardResult(
        evaluated_targets=evaluated,
        skipped_targets=skipped,
        threshold_scores=tuple(scores),
        predictions=tuple(replay_predictions),
    )
