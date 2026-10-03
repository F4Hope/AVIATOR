"""Adaptive leakage-safe next-round point prediction.

The model predicts log(multiplier) from completed-round history only. It combines
a regularized linear model with robust recent-history estimators and chooses
weights from chronological validation performance. No target outcome is visible
at prediction time.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import math
from statistics import median, pstdev
from typing import Iterable, Sequence

from prediction.features import MAX_FEATURE_MULTIPLIER


MODEL_NAME = "adaptive-log-multiplier-ensemble"
MODEL_VERSION = "2"


def _value(value: Decimal | str | int | float) -> float:
    result = float(Decimal(str(value)))
    if not math.isfinite(result) or result < 1.0:
        raise ValueError("multipliers must be finite values of at least 1.")
    return result


def _log_value(value: float) -> float:
    return math.log(min(value, float(MAX_FEATURE_MULTIPLIER)))


@dataclass(frozen=True, slots=True)
class AdaptiveConfig:
    min_history: int = 20
    lag_count: int = 5
    short_window: int = 8
    long_window: int = 20
    validation_rows: int = 8
    ridge_alphas: tuple[float, ...] = (0.1, 1.0, 10.0)

    def __post_init__(self) -> None:
        if type(self.min_history) is not int or self.min_history < 10:
            raise ValueError("min_history must be an integer of at least 10.")
        if type(self.lag_count) is not int or self.lag_count < 1:
            raise ValueError("lag_count must be positive.")
        if type(self.short_window) is not int or self.short_window < self.lag_count:
            raise ValueError("short_window must be at least lag_count.")
        if type(self.long_window) is not int or self.long_window < self.short_window:
            raise ValueError("long_window must be at least short_window.")
        if self.min_history < self.long_window:
            raise ValueError("min_history must be at least long_window.")
        if type(self.validation_rows) is not int or self.validation_rows < 3:
            raise ValueError("validation_rows must be at least 3.")
        if not self.ridge_alphas or any(alpha <= 0 for alpha in self.ridge_alphas):
            raise ValueError("ridge_alphas must contain positive values.")


@dataclass(frozen=True, slots=True)
class AdaptivePointPrediction:
    model_name: str
    model_version: str
    status: str
    predicted_multiplier: Decimal | None
    lower_multiplier: Decimal | None
    upper_multiplier: Decimal | None
    confidence: str
    history_count: int
    regime: str
    regime_shift: float | None
    validation_rows: int
    validation_mae_log: float | None
    recent_median_mae_log: float | None
    components: tuple[tuple[str, float], ...]


def _features(history: Sequence[float], config: AdaptiveConfig) -> tuple[float, ...]:
    if len(history) < config.long_window:
        raise ValueError("insufficient history for adaptive feature window.")

    logs = [_log_value(value) for value in history]
    short = logs[-config.short_window :]
    long = logs[-config.long_window :]
    lags = tuple(reversed(logs[-config.lag_count :]))

    short_mean = sum(short) / len(short)
    long_mean = sum(long) / len(long)
    short_std = pstdev(short) if len(short) > 1 else 0.0
    long_std = pstdev(long) if len(long) > 1 else 0.0

    short_values = history[-config.short_window :]
    long_values = history[-config.long_window :]
    threshold_rates = []
    for threshold in (1.5, 2.0, 5.0):
        threshold_rates.extend(
            (
                sum(value >= threshold for value in short_values) / len(short_values),
                sum(value >= threshold for value in long_values) / len(long_values),
            )
        )

    return (
        *lags,
        short_mean,
        long_mean,
        short_mean - long_mean,
        short_std,
        long_std,
        *threshold_rates,
    )


def _training_rows(
    values: Sequence[float],
    config: AdaptiveConfig,
) -> tuple[list[tuple[float, ...]], list[float], list[int]]:
    x: list[tuple[float, ...]] = []
    y: list[float] = []
    indices: list[int] = []
    for index in range(config.long_window, len(values)):
        x.append(_features(values[:index], config))
        y.append(_log_value(values[index]))
        indices.append(index)
    return x, y, indices


def _recent_median_log(history: Sequence[float], config: AdaptiveConfig) -> float:
    window = history[-config.short_window :]
    return median(_log_value(value) for value in window)


def _ewma_log(history: Sequence[float], config: AdaptiveConfig) -> float:
    window = history[-config.long_window :]
    logs = [_log_value(value) for value in window]
    alpha = 2.0 / (config.short_window + 1.0)
    result = logs[0]
    for value in logs[1:]:
        result = alpha * value + (1.0 - alpha) * result
    return result


@dataclass(frozen=True, slots=True)
class _PureRidgeModel:
    means: tuple[float, ...]
    scales: tuple[float, ...]
    intercept: float
    coefficients: tuple[float, ...]

    def predict(self, rows: Sequence[Sequence[float]]) -> list[float]:
        result: list[float] = []
        for row in rows:
            if len(row) != len(self.coefficients):
                raise ValueError("prediction row has unexpected feature count.")
            standardized = [
                (float(value) - mean) / scale
                for value, mean, scale in zip(row, self.means, self.scales)
            ]
            result.append(
                self.intercept
                + sum(
                    coefficient * value
                    for coefficient, value in zip(self.coefficients, standardized)
                )
            )
        return result


def _solve_linear_system(
    matrix: Sequence[Sequence[float]],
    vector: Sequence[float],
) -> tuple[float, ...]:
    """Solve Ax=b with partial-pivot Gaussian elimination."""
    size = len(vector)
    if size == 0 or len(matrix) != size or any(len(row) != size for row in matrix):
        raise ValueError("linear system must be square and nonempty.")

    augmented = [
        [float(value) for value in row] + [float(rhs)]
        for row, rhs in zip(matrix, vector)
    ]

    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            raise ValueError("ridge system is numerically singular.")
        if pivot != column:
            augmented[column], augmented[pivot] = augmented[pivot], augmented[column]

        pivot_value = augmented[column][column]
        for item in range(column, size + 1):
            augmented[column][item] /= pivot_value

        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            if factor == 0.0:
                continue
            for item in range(column, size + 1):
                augmented[row][item] -= factor * augmented[column][item]

    return tuple(augmented[row][size] for row in range(size))


def _fit_ridge(
    x: Sequence[Sequence[float]],
    y: Sequence[float],
    alpha: float,
) -> _PureRidgeModel:
    """Fit standardized Ridge regression using only the Python standard library."""
    if not x or not y or len(x) != len(y):
        raise ValueError("ridge training data must be nonempty and aligned.")
    if alpha <= 0:
        raise ValueError("ridge alpha must be positive.")

    feature_count = len(x[0])
    if feature_count == 0 or any(len(row) != feature_count for row in x):
        raise ValueError("ridge rows must have a consistent nonzero feature count.")

    sample_count = len(x)
    means = tuple(
        sum(float(row[column]) for row in x) / sample_count
        for column in range(feature_count)
    )
    scales_list: list[float] = []
    for column, mean in enumerate(means):
        variance = (
            sum((float(row[column]) - mean) ** 2 for row in x) / sample_count
        )
        scale = math.sqrt(variance)
        scales_list.append(scale if scale > 1e-12 else 1.0)
    scales = tuple(scales_list)

    standardized = [
        [
            (float(value) - means[column]) / scales[column]
            for column, value in enumerate(row)
        ]
        for row in x
    ]
    intercept = sum(float(value) for value in y) / sample_count
    centered_y = [float(value) - intercept for value in y]

    gram = [[0.0 for _ in range(feature_count)] for _ in range(feature_count)]
    rhs = [0.0 for _ in range(feature_count)]

    for row, target in zip(standardized, centered_y):
        for i in range(feature_count):
            rhs[i] += row[i] * target
            for j in range(i, feature_count):
                gram[i][j] += row[i] * row[j]

    for i in range(feature_count):
        for j in range(i):
            gram[i][j] = gram[j][i]
        gram[i][i] += float(alpha)

    coefficients = _solve_linear_system(gram, rhs)
    return _PureRidgeModel(
        means=means,
        scales=scales,
        intercept=intercept,
        coefficients=coefficients,
    )


def _mae(actual: Sequence[float], predicted: Sequence[float]) -> float:
    if not actual or len(actual) != len(predicted):
        raise ValueError("MAE inputs must be nonempty and equal length.")
    return sum(abs(a - p) for a, p in zip(actual, predicted)) / len(actual)


def _quantile(values: Sequence[float], q: float) -> float:
    if not values:
        raise ValueError("quantile requires data.")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _decimal_multiplier(value: float) -> Decimal:
    bounded = max(1.0, min(float(MAX_FEATURE_MULTIPLIER), value))
    return Decimal(str(round(bounded, 2)))


def predict_next_multiplier(
    history: Sequence[Decimal | str | int | float]
    | Iterable[Decimal | str | int | float],
    config: AdaptiveConfig | None = None,
) -> AdaptivePointPrediction:
    """Predict the next multiplier from already-completed outcomes only."""
    active = config if config is not None else AdaptiveConfig()
    if not isinstance(active, AdaptiveConfig):
        raise TypeError("config must be AdaptiveConfig.")

    values = tuple(_value(value) for value in history)
    if len(values) < active.min_history:
        return AdaptivePointPrediction(
            model_name=MODEL_NAME,
            model_version=MODEL_VERSION,
            status="INSUFFICIENT_DATA",
            predicted_multiplier=None,
            lower_multiplier=None,
            upper_multiplier=None,
            confidence="LOW",
            history_count=len(values),
            regime="UNKNOWN",
            regime_shift=None,
            validation_rows=0,
            validation_mae_log=None,
            recent_median_mae_log=None,
            components=(),
        )

    x_all, y_all, indices = _training_rows(values, active)
    available_validation = min(active.validation_rows, max(0, len(x_all) - 3))
    if available_validation < 3:
        return AdaptivePointPrediction(
            model_name=MODEL_NAME,
            model_version=MODEL_VERSION,
            status="INSUFFICIENT_TRAINING_ROWS",
            predicted_multiplier=None,
            lower_multiplier=None,
            upper_multiplier=None,
            confidence="LOW",
            history_count=len(values),
            regime="UNKNOWN",
            regime_shift=None,
            validation_rows=available_validation,
            validation_mae_log=None,
            recent_median_mae_log=None,
            components=(),
        )

    split = len(x_all) - available_validation
    x_train, y_train = x_all[:split], y_all[:split]
    x_val, y_val = x_all[split:], y_all[split:]
    val_indices = indices[split:]

    candidate_errors: list[tuple[str, float, float | None]] = []

    for alpha in active.ridge_alphas:
        model = _fit_ridge(x_train, y_train, alpha)
        predicted = [float(value) for value in model.predict(x_val)]
        candidate_errors.append((f"ridge_{alpha:g}", _mae(y_val, predicted), alpha))

    median_predictions = [
        _recent_median_log(values[:index], active) for index in val_indices
    ]
    median_mae = _mae(y_val, median_predictions)
    candidate_errors.append(("recent_median", median_mae, None))

    ewma_predictions = [_ewma_log(values[:index], active) for index in val_indices]
    candidate_errors.append(("ewma", _mae(y_val, ewma_predictions), None))

    candidate_errors.sort(key=lambda item: (item[1], item[0]))
    selected = candidate_errors[: min(3, len(candidate_errors))]
    inverse = [1.0 / max(error, 1e-6) for _, error, _ in selected]
    total_inverse = sum(inverse)
    weights = [value / total_inverse for value in inverse]

    next_features = _features(values, active)
    component_predictions: list[tuple[str, float]] = []
    for (name, _, alpha), weight in zip(selected, weights):
        if name.startswith("ridge_"):
            model = _fit_ridge(x_all, y_all, float(alpha))
            prediction = float(model.predict([next_features])[0])
        elif name == "recent_median":
            prediction = _recent_median_log(values, active)
        else:
            prediction = _ewma_log(values, active)
        component_predictions.append((name, prediction))

    predicted_log = sum(
        weight * prediction
        for weight, (_, prediction) in zip(weights, component_predictions)
    )

    best_validation_mae = selected[0][1]
    residuals: list[float] = []
    best_name, _, best_alpha = selected[0]
    if best_name.startswith("ridge_"):
        best_model = _fit_ridge(x_train, y_train, float(best_alpha))
        best_val = [float(value) for value in best_model.predict(x_val)]
    elif best_name == "recent_median":
        best_val = median_predictions
    else:
        best_val = ewma_predictions
    residuals = [abs(actual - pred) for actual, pred in zip(y_val, best_val)]
    radius = _quantile(residuals, 0.80)

    predicted = math.exp(predicted_log)
    lower = math.exp(predicted_log - radius)
    upper = math.exp(predicted_log + radius)

    logs = [_log_value(value) for value in values]
    short_mean = sum(logs[-active.short_window :]) / active.short_window
    long_mean = sum(logs[-active.long_window :]) / active.long_window
    shift = short_mean - long_mean
    long_std = pstdev(logs[-active.long_window :]) if active.long_window > 1 else 0.0
    scale = max(long_std, 1e-6)
    standardized_shift = shift / scale

    if standardized_shift > 0.35:
        regime = "HIGHER_RECENT_MULTIPLIERS"
    elif standardized_shift < -0.35:
        regime = "LOWER_RECENT_MULTIPLIERS"
    else:
        regime = "STABLE"

    improvement = (median_mae - best_validation_mae) / max(median_mae, 1e-6)
    if available_validation >= 8 and improvement >= 0.15:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    return AdaptivePointPrediction(
        model_name=MODEL_NAME,
        model_version=MODEL_VERSION,
        status="READY",
        predicted_multiplier=_decimal_multiplier(predicted),
        lower_multiplier=_decimal_multiplier(lower),
        upper_multiplier=_decimal_multiplier(upper),
        confidence=confidence,
        history_count=len(values),
        regime=regime,
        regime_shift=round(standardized_shift, 6),
        validation_rows=available_validation,
        validation_mae_log=round(best_validation_mae, 6),
        recent_median_mae_log=round(median_mae, 6),
        components=tuple(
            (name, round(weight, 6))
            for weight, (name, _) in zip(weights, component_predictions)
        ),
    )
