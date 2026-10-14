"""Leakage-safe gated threshold-probability challenger for live forecasts.

The historical empirical frequency remains the safety baseline. A logistic
challenger is trained only on already-completed rounds and may influence the
next forecast only after beating that baseline on a later chronological gate
window. The gate window is never used to select hyperparameters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_EVEN
import math
from typing import Iterable, Sequence

from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from models.baseline import BaselineConfig, predict_thresholds
from prediction.features import FeatureConfig, build_feature_samples, feature_vector


MODEL_NAME = "gated-logistic-threshold-ensemble"
MODEL_VERSION = "1"
PROBABILITY_PLACES = Decimal("0.000001")


@dataclass(frozen=True, slots=True)
class ThresholdChallengerConfig:
    min_history: int = 140
    min_train_rows: int = 60
    tune_rows: int = 40
    gate_rows: int = 60
    min_gate_rows: int = 40
    min_skill_pct: float = 3.0
    max_candidate_weight: float = 0.65
    max_probability_shift: float = 0.10
    c_values: tuple[float, ...] = (0.03, 0.1, 0.3, 1.0)
    feature_config: FeatureConfig = field(default_factory=FeatureConfig)

    def __post_init__(self) -> None:
        for name in (
            "min_history",
            "min_train_rows",
            "tune_rows",
            "gate_rows",
            "min_gate_rows",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer.")
        if self.min_gate_rows > self.gate_rows:
            raise ValueError("min_gate_rows cannot exceed gate_rows.")
        if self.min_skill_pct < 0:
            raise ValueError("min_skill_pct cannot be negative.")
        if not 0 < self.max_candidate_weight <= 1:
            raise ValueError("max_candidate_weight must be in (0, 1].")
        if not 0 < self.max_probability_shift <= 0.5:
            raise ValueError("max_probability_shift must be in (0, 0.5].")
        if not self.c_values or any(
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or value <= 0
            for value in self.c_values
        ):
            raise ValueError("c_values must contain positive numbers.")


@dataclass(frozen=True, slots=True)
class ThresholdDecision:
    threshold: Decimal
    source: str
    reason: str
    probability: Decimal | None
    baseline_probability: Decimal | None
    raw_candidate_probability: Decimal | None
    selected_c: float | None
    tune_samples: int
    gate_samples: int
    candidate_brier: Decimal | None
    baseline_brier: Decimal | None
    candidate_skill_pct: Decimal | None
    candidate_weight: Decimal | None


@dataclass(frozen=True, slots=True)
class ThresholdEnsemblePrediction:
    model_name: str
    model_version: str
    status: str
    history_count: int
    decisions: tuple[ThresholdDecision, ...]

    def probability_map(self) -> dict[str, str | None]:
        return {
            str(item.threshold): (
                None if item.probability is None else format(item.probability, "f")
            )
            for item in self.decisions
        }

    def detail_map(self) -> dict[str, dict[str, object]]:
        details: dict[str, dict[str, object]] = {}
        for item in self.decisions:
            details[str(item.threshold)] = {
                "source": item.source,
                "reason": item.reason,
                "baseline_probability": (
                    None
                    if item.baseline_probability is None
                    else format(item.baseline_probability, "f")
                ),
                "raw_candidate_probability": (
                    None
                    if item.raw_candidate_probability is None
                    else format(item.raw_candidate_probability, "f")
                ),
                "selected_c": item.selected_c,
                "tune_samples": item.tune_samples,
                "gate_samples": item.gate_samples,
                "candidate_brier": (
                    None
                    if item.candidate_brier is None
                    else format(item.candidate_brier, "f")
                ),
                "baseline_brier": (
                    None
                    if item.baseline_brier is None
                    else format(item.baseline_brier, "f")
                ),
                "candidate_skill_pct": (
                    None
                    if item.candidate_skill_pct is None
                    else format(item.candidate_skill_pct, "f")
                ),
                "candidate_weight": (
                    None
                    if item.candidate_weight is None
                    else format(item.candidate_weight, "f")
                ),
            }
        return details


def _decimal_probability(value: float) -> Decimal:
    bounded = min(1.0, max(0.0, float(value)))
    return Decimal(str(bounded)).quantize(
        PROBABILITY_PLACES,
        rounding=ROUND_HALF_EVEN,
    )


def _empirical_probability(
    values: Sequence[Decimal],
    end: int,
    threshold: Decimal,
) -> float:
    if end <= 0:
        raise ValueError("empirical probability requires prior history.")
    return sum(value >= threshold for value in values[:end]) / end


def _fit_model(
    x: Sequence[Sequence[float]],
    y: Sequence[int],
    c_value: float,
):
    model = make_pipeline(
        StandardScaler(),
        LogisticRegression(
            C=float(c_value),
            random_state=0,
            max_iter=1000,
            solver="lbfgs",
        ),
    )
    model.fit(x, y)
    return model


def _fallback(
    threshold: Decimal,
    baseline_probability: Decimal | None,
    reason: str,
    *,
    tune_samples: int = 0,
    gate_samples: int = 0,
    selected_c: float | None = None,
    candidate_brier: Decimal | None = None,
    baseline_brier: Decimal | None = None,
    skill: Decimal | None = None,
) -> ThresholdDecision:
    return ThresholdDecision(
        threshold=threshold,
        source="historical_baseline",
        reason=reason,
        probability=baseline_probability,
        baseline_probability=baseline_probability,
        raw_candidate_probability=None,
        selected_c=selected_c,
        tune_samples=tune_samples,
        gate_samples=gate_samples,
        candidate_brier=candidate_brier,
        baseline_brier=baseline_brier,
        candidate_skill_pct=skill,
        candidate_weight=None,
    )


def predict_threshold_ensemble(
    history: Sequence[Decimal | str | int | float]
    | Iterable[Decimal | str | int | float],
    baseline_config: BaselineConfig,
    challenger_config: ThresholdChallengerConfig | None = None,
) -> ThresholdEnsemblePrediction:
    """Forecast next-round threshold probabilities from completed history only."""
    if not isinstance(baseline_config, BaselineConfig):
        raise TypeError("baseline_config must be BaselineConfig.")
    active = (
        challenger_config
        if challenger_config is not None
        else ThresholdChallengerConfig()
    )
    if not isinstance(active, ThresholdChallengerConfig):
        raise TypeError("challenger_config must be ThresholdChallengerConfig.")

    values = tuple(Decimal(str(value)) for value in history)
    if any(not value.is_finite() or value < 1 for value in values):
        raise ValueError("historical multipliers must be finite and at least 1.")

    baseline = predict_thresholds(values, baseline_config)
    baseline_map = {
        item.threshold: item.probability
        for item in baseline.probabilities
    }
    thresholds = tuple(item.threshold for item in baseline.probabilities)

    if len(values) < active.min_history:
        decisions = tuple(
            _fallback(
                threshold,
                baseline_map[threshold],
                "insufficient_challenger_history",
            )
            for threshold in thresholds
        )
        return ThresholdEnsemblePrediction(
            MODEL_NAME,
            MODEL_VERSION,
            baseline.status,
            len(values),
            decisions,
        )

    samples = build_feature_samples(values, active.feature_config)
    required_rows = (
        active.min_train_rows + active.tune_rows + active.min_gate_rows
    )
    if len(samples) < required_rows:
        decisions = tuple(
            _fallback(
                threshold,
                baseline_map[threshold],
                "insufficient_challenger_rows",
            )
            for threshold in thresholds
        )
        return ThresholdEnsemblePrediction(
            MODEL_NAME,
            MODEL_VERSION,
            baseline.status,
            len(values),
            decisions,
        )

    gate_rows = min(active.gate_rows, len(samples) - active.min_train_rows - active.tune_rows)
    gate_rows = max(active.min_gate_rows, gate_rows)
    tune_rows = min(
        active.tune_rows,
        len(samples) - active.min_train_rows - gate_rows,
    )
    train_rows = len(samples) - tune_rows - gate_rows

    x_all = [sample.values for sample in samples]
    target_indices = [sample.target_index for sample in samples]
    current_vector = feature_vector(values, active.feature_config)

    decisions_list: list[ThresholdDecision] = []

    for threshold in thresholds:
        baseline_probability = baseline_map[threshold]
        if baseline_probability is None:
            decisions_list.append(
                _fallback(
                    threshold,
                    baseline_probability,
                    "baseline_not_ready",
                )
            )
            continue

        y_all = [
            int(values[index] >= threshold)
            for index in target_indices
        ]
        x_train = x_all[:train_rows]
        y_train = y_all[:train_rows]
        x_tune = x_all[train_rows : train_rows + tune_rows]
        y_tune = y_all[train_rows : train_rows + tune_rows]

        if len(set(y_train)) < 2:
            decisions_list.append(
                _fallback(
                    threshold,
                    baseline_probability,
                    "insufficient_train_class_variation",
                    tune_samples=tune_rows,
                    gate_samples=gate_rows,
                )
            )
            continue

        candidates: list[tuple[float, float]] = []
        for c_value in active.c_values:
            model = _fit_model(x_train, y_train, c_value)
            tune_probability = model.predict_proba(x_tune)[:, 1]
            candidates.append(
                (
                    float(brier_score_loss(y_tune, tune_probability)),
                    float(c_value),
                )
            )
        _, selected_c = min(candidates, key=lambda item: (item[0], item[1]))

        fit_end = train_rows + tune_rows
        y_fit = y_all[:fit_end]
        if len(set(y_fit)) < 2:
            decisions_list.append(
                _fallback(
                    threshold,
                    baseline_probability,
                    "insufficient_fit_class_variation",
                    tune_samples=tune_rows,
                    gate_samples=gate_rows,
                    selected_c=selected_c,
                )
            )
            continue

        gate_model = _fit_model(x_all[:fit_end], y_fit, selected_c)
        x_gate = x_all[fit_end:]
        y_gate = y_all[fit_end:]
        candidate_gate = gate_model.predict_proba(x_gate)[:, 1]
        baseline_gate = [
            _empirical_probability(values, index, threshold)
            for index in target_indices[fit_end:]
        ]

        candidate_brier_float = float(
            brier_score_loss(y_gate, candidate_gate)
        )
        baseline_brier_float = float(
            brier_score_loss(y_gate, baseline_gate)
        )
        skill_float = (
            ((baseline_brier_float - candidate_brier_float) / baseline_brier_float) * 100
            if baseline_brier_float > 0
            else 0.0
        )

        candidate_brier = _decimal_probability(candidate_brier_float)
        baseline_brier = _decimal_probability(baseline_brier_float)
        skill = Decimal(str(skill_float)).quantize(
            PROBABILITY_PLACES,
            rounding=ROUND_HALF_EVEN,
        )

        if (
            gate_rows < active.min_gate_rows
            or candidate_brier_float >= baseline_brier_float
            or skill_float < active.min_skill_pct
        ):
            decisions_list.append(
                _fallback(
                    threshold,
                    baseline_probability,
                    "chronological_gate_rejected",
                    tune_samples=tune_rows,
                    gate_samples=gate_rows,
                    selected_c=selected_c,
                    candidate_brier=candidate_brier,
                    baseline_brier=baseline_brier,
                    skill=skill,
                )
            )
            continue

        final_model = _fit_model(x_all, y_all, selected_c)
        raw_candidate = float(
            final_model.predict_proba([current_vector])[0, 1]
        )
        baseline_float = float(baseline_probability)

        evidence_weight = min(
            active.max_candidate_weight,
            max(
                0.20,
                (gate_rows / max(active.gate_rows, 1)) * 0.45
                + min(max(skill_float, 0.0), 25.0) / 25.0 * 0.20,
            ),
        )
        requested_shift = (raw_candidate - baseline_float) * evidence_weight
        bounded_shift = max(
            -active.max_probability_shift,
            min(active.max_probability_shift, requested_shift),
        )
        blended = baseline_float + bounded_shift

        decisions_list.append(
            ThresholdDecision(
                threshold=threshold,
                source="gated_logistic_challenger",
                reason="chronological_gate_passed",
                probability=_decimal_probability(blended),
                baseline_probability=baseline_probability,
                raw_candidate_probability=_decimal_probability(raw_candidate),
                selected_c=selected_c,
                tune_samples=tune_rows,
                gate_samples=gate_rows,
                candidate_brier=candidate_brier,
                baseline_brier=baseline_brier,
                candidate_skill_pct=skill,
                candidate_weight=_decimal_probability(evidence_weight),
            )
        )

    nested: list[ThresholdDecision] = []
    previous: Decimal | None = None
    for item in decisions_list:
        probability = item.probability
        if probability is not None and previous is not None:
            probability = min(previous, probability)
        if probability is not None:
            previous = probability
        nested.append(
            ThresholdDecision(
                threshold=item.threshold,
                source=item.source,
                reason=item.reason,
                probability=probability,
                baseline_probability=item.baseline_probability,
                raw_candidate_probability=item.raw_candidate_probability,
                selected_c=item.selected_c,
                tune_samples=item.tune_samples,
                gate_samples=item.gate_samples,
                candidate_brier=item.candidate_brier,
                baseline_brier=item.baseline_brier,
                candidate_skill_pct=item.candidate_skill_pct,
                candidate_weight=item.candidate_weight,
            )
        )

    status = (
        "READY"
        if any(item.probability is not None for item in nested)
        else baseline.status
    )
    return ThresholdEnsemblePrediction(
        model_name=MODEL_NAME,
        model_version=MODEL_VERSION,
        status=status,
        history_count=len(values),
        decisions=tuple(nested),
    )
