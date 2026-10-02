"""Chronological machine-learning experiments for threshold classification."""

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
from typing import Iterable, Sequence

from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import make_pipeline

from models.baseline import BaselineConfig, predict_thresholds
from prediction.features import FeatureConfig, FeatureSample, build_feature_samples


MODEL_NAME = "logistic-threshold-classifier"
MODEL_VERSION = "1"
EXPERIMENT_VERSION = 1
DEFAULT_C_VALUES: tuple[float, ...] = (0.1, 1.0, 10.0)


@dataclass(frozen=True, slots=True)
class SplitConfig:
    validation_fraction: float = 0.2
    test_fraction: float = 0.2

    def __post_init__(self) -> None:
        for name in ("validation_fraction", "test_fraction"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 < value < 1:
                raise ValueError(f"{name} must be between 0 and 1.")
        if self.validation_fraction + self.test_fraction >= 1:
            raise ValueError("validation_fraction + test_fraction must be less than 1.")


@dataclass(frozen=True, slots=True)
class ThresholdExperiment:
    threshold: Decimal
    status: str
    selected_c: float | None
    train_rows: int
    validation_rows: int
    test_rows: int
    validation_brier: float | None
    test_brier: float | None
    baseline_test_brier: float | None
    test_positive_rate: float | None


@dataclass(frozen=True, slots=True)
class ExperimentReport:
    experiment_version: int
    dataset_fingerprint: str
    feature_names: tuple[str, ...]
    thresholds: tuple[Decimal, ...]
    split: SplitConfig
    feature_config: FeatureConfig
    c_values: tuple[float, ...]
    experiments: tuple[ThresholdExperiment, ...]


def _fingerprint(values: Sequence[Decimal | str | int | float]) -> str:
    encoded = json.dumps([str(value) for value in values], separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _chronological_slices(size: int, split: SplitConfig) -> tuple[slice, slice, slice]:
    if size < 5:
        raise ValueError("at least five feature rows are required for an experiment.")
    test_start = size - max(1, int(size * split.test_fraction))
    validation_start = test_start - max(1, int(size * split.validation_fraction))
    if validation_start < 1:
        raise ValueError("insufficient rows for chronological train/validation/test splits.")
    return slice(0, validation_start), slice(validation_start, test_start), slice(test_start, size)


def _labels(values: tuple[Decimal, ...], samples: tuple[FeatureSample, ...], threshold: Decimal) -> list[int]:
    return [int(values[sample.target_index] >= threshold) for sample in samples]


def _brier(y_true: Sequence[int], probabilities: Sequence[float]) -> float:
    return float(brier_score_loss(y_true, probabilities))


def run_logistic_experiment(
    multipliers: Sequence[Decimal | str | int | float]
    | Iterable[Decimal | str | int | float],
    *,
    thresholds: tuple[Decimal | str | int | float, ...] = (
        Decimal("1.5"),
        Decimal("2"),
        Decimal("5"),
    ),
    feature_config: FeatureConfig | None = None,
    split_config: SplitConfig | None = None,
    c_values: tuple[float, ...] = DEFAULT_C_VALUES,
) -> ExperimentReport:
    """Select C on chronological validation data, then score untouched final test data."""
    active_features = feature_config if feature_config is not None else FeatureConfig()
    active_split = split_config if split_config is not None else SplitConfig()
    values = tuple(Decimal(str(value)) for value in multipliers)
    threshold_values = tuple(Decimal(str(value)) for value in thresholds)
    if not threshold_values or tuple(sorted(set(threshold_values))) != threshold_values:
        raise ValueError("thresholds must be unique and strictly increasing.")
    if not c_values or any((not isinstance(c, (int, float)) or isinstance(c, bool) or c <= 0) for c in c_values):
        raise ValueError("c_values must contain positive numbers.")
    c_values = tuple(float(c) for c in c_values)

    samples = build_feature_samples(values, active_features)
    train_slice, validation_slice, test_slice = _chronological_slices(len(samples), active_split)
    x_all = [sample.values for sample in samples]
    reports: list[ThresholdExperiment] = []

    for threshold in threshold_values:
        y_all = _labels(values, samples, threshold)
        x_train, y_train = x_all[train_slice], y_all[train_slice]
        x_validation, y_validation = x_all[validation_slice], y_all[validation_slice]
        x_test, y_test = x_all[test_slice], y_all[test_slice]

        if len(set(y_train)) < 2:
            reports.append(
                ThresholdExperiment(
                    threshold, "INSUFFICIENT_CLASS_VARIATION", None,
                    len(x_train), len(x_validation), len(x_test),
                    None, None, None, sum(y_test) / len(y_test) if y_test else None,
                )
            )
            continue

        candidates: list[tuple[float, float]] = []
        for c_value in c_values:
            model = make_pipeline(
                StandardScaler(),
                LogisticRegression(C=c_value, random_state=0, max_iter=1000),
            )
            model.fit(x_train, y_train)
            validation_probability = model.predict_proba(x_validation)[:, 1]
            candidates.append((_brier(y_validation, validation_probability), c_value))
        validation_brier, selected_c = min(candidates, key=lambda item: (item[0], item[1]))

        final_model = make_pipeline(
            StandardScaler(),
            LogisticRegression(C=selected_c, random_state=0, max_iter=1000),
        )
        x_fit = x_all[: test_slice.start]
        y_fit = y_all[: test_slice.start]
        if len(set(y_fit)) < 2:
            reports.append(
                ThresholdExperiment(
                    threshold, "INSUFFICIENT_CLASS_VARIATION", selected_c,
                    len(x_train), len(x_validation), len(x_test),
                    validation_brier, None, None, sum(y_test) / len(y_test) if y_test else None,
                )
            )
            continue
        final_model.fit(x_fit, y_fit)
        test_probability = final_model.predict_proba(x_test)[:, 1]
        test_brier = _brier(y_test, test_probability)

        baseline_probabilities: list[float] = []
        for sample in samples[test_slice]:
            prediction = predict_thresholds(
                values[: sample.target_index],
                BaselineConfig(thresholds=(threshold,), min_history=active_features.warmup),
            )
            probability = prediction.probabilities[0].probability
            if probability is None:
                raise RuntimeError("baseline history unexpectedly unavailable for test sample.")
            baseline_probabilities.append(float(probability))
        baseline_brier = _brier(y_test, baseline_probabilities)

        reports.append(
            ThresholdExperiment(
                threshold=threshold,
                status="COMPLETE",
                selected_c=selected_c,
                train_rows=len(x_train),
                validation_rows=len(x_validation),
                test_rows=len(x_test),
                validation_brier=validation_brier,
                test_brier=test_brier,
                baseline_test_brier=baseline_brier,
                test_positive_rate=sum(y_test) / len(y_test) if y_test else None,
            )
        )

    return ExperimentReport(
        experiment_version=EXPERIMENT_VERSION,
        dataset_fingerprint=_fingerprint(values),
        feature_names=active_features.feature_names,
        thresholds=threshold_values,
        split=active_split,
        feature_config=active_features,
        c_values=c_values,
        experiments=tuple(reports),
    )
