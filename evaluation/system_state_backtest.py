"""Chronological evaluation of pre-round system-state predictive information."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import math
from statistics import median
from typing import Sequence

from prediction.adaptive import _fit_ridge
from prediction.system_state import SystemStateSample


DEFAULT_ALPHAS: tuple[float, ...] = (1.0, 10.0, 100.0)


@dataclass(frozen=True, slots=True)
class SystemStateBacktestRow:
    target_index: int
    actual: Decimal
    recent_median: Decimal
    history_only: Decimal
    state_only: Decimal
    hybrid: Decimal
    history_absolute_error: Decimal
    state_absolute_error: Decimal
    hybrid_absolute_error: Decimal
    recent_median_absolute_error: Decimal


@dataclass(frozen=True, slots=True)
class SystemStateBacktestReport:
    status: str
    aligned_samples: int
    evaluated_targets: int
    skipped_training_targets: int
    history_only_mae: Decimal | None
    state_only_mae: Decimal | None
    hybrid_mae: Decimal | None
    recent_median_mae: Decimal | None
    hybrid_median_absolute_error: Decimal | None
    hybrid_within_half_x_rate: Decimal | None
    hybrid_within_one_x_rate: Decimal | None
    hybrid_win_rate_vs_history: Decimal | None
    hybrid_improvement_vs_history_pct: Decimal | None
    history_log_mae: Decimal | None
    state_log_mae: Decimal | None
    hybrid_log_mae: Decimal | None
    rows: tuple[SystemStateBacktestRow, ...]


def _q(value: float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.000001"))


def _target_log(value: Decimal) -> float:
    return math.log(min(float(value), 1000.0))


def _prediction_multiplier(log_value: float) -> Decimal:
    bounded = max(1.0, min(1000.0, math.exp(log_value)))
    return Decimal(str(round(bounded, 2)))


def _mae_float(actual: Sequence[float], predicted: Sequence[float]) -> float:
    if not actual or len(actual) != len(predicted):
        raise ValueError("MAE inputs must be nonempty and aligned.")
    return sum(abs(a - p) for a, p in zip(actual, predicted)) / len(actual)


def _select_alpha(
    x: Sequence[Sequence[float]],
    y: Sequence[float],
    alphas: Sequence[float],
    validation_rows: int,
) -> float:
    if len(x) != len(y) or len(x) < 6:
        raise ValueError("alpha selection requires aligned training rows.")
    validation = min(validation_rows, max(3, len(x) // 4))
    split = len(x) - validation
    if split < 3:
        return float(alphas[0])

    x_train, y_train = x[:split], y[:split]
    x_val, y_val = x[split:], y[split:]
    scored: list[tuple[float, float]] = []
    for alpha in alphas:
        model = _fit_ridge(x_train, y_train, float(alpha))
        predicted = model.predict(x_val)
        scored.append((_mae_float(y_val, predicted), float(alpha)))
    scored.sort()
    return scored[0][1]


def _fit_predict(
    train_x: Sequence[Sequence[float]],
    train_y: Sequence[float],
    target_x: Sequence[float],
    alphas: Sequence[float],
    validation_rows: int,
) -> float:
    alpha = _select_alpha(train_x, train_y, alphas, validation_rows)
    model = _fit_ridge(train_x, train_y, alpha)
    return float(model.predict([target_x])[0])


def walk_forward_system_state_evaluate(
    samples: Sequence[SystemStateSample],
    min_training_samples: int = 40,
    validation_rows: int = 12,
    alphas: Sequence[float] = DEFAULT_ALPHAS,
) -> SystemStateBacktestReport:
    """Compare history-only, state-only and hybrid models on future aligned targets."""
    if type(min_training_samples) is not int or min_training_samples < 20:
        raise ValueError("min_training_samples must be at least 20.")
    if type(validation_rows) is not int or validation_rows < 3:
        raise ValueError("validation_rows must be at least 3.")
    if not alphas or any(alpha <= 0 for alpha in alphas):
        raise ValueError("alphas must contain positive values.")

    rows: list[SystemStateBacktestRow] = []
    history_log_errors: list[float] = []
    state_log_errors: list[float] = []
    hybrid_log_errors: list[float] = []

    for position in range(min_training_samples, len(samples)):
        training = samples[:position]
        target = samples[position]

        train_y = [_target_log(sample.actual_multiplier) for sample in training]
        history_x = [sample.history_features for sample in training]
        state_x = [sample.state_features for sample in training]
        hybrid_x = [
            sample.history_features + sample.state_features
            for sample in training
        ]

        history_log = _fit_predict(
            history_x, train_y, target.history_features, alphas, validation_rows
        )
        state_log = _fit_predict(
            state_x, train_y, target.state_features, alphas, validation_rows
        )
        hybrid_log = _fit_predict(
            hybrid_x,
            train_y,
            target.history_features + target.state_features,
            alphas,
            validation_rows,
        )

        actual_log = _target_log(target.actual_multiplier)
        history_log_errors.append(abs(history_log - actual_log))
        state_log_errors.append(abs(state_log - actual_log))
        hybrid_log_errors.append(abs(hybrid_log - actual_log))

        history_prediction = _prediction_multiplier(history_log)
        state_prediction = _prediction_multiplier(state_log)
        hybrid_prediction = _prediction_multiplier(hybrid_log)
        actual = target.actual_multiplier

        rows.append(
            SystemStateBacktestRow(
                target_index=target.target_index,
                actual=actual,
                recent_median=target.baseline_recent_median,
                history_only=history_prediction,
                state_only=state_prediction,
                hybrid=hybrid_prediction,
                history_absolute_error=abs(history_prediction - actual),
                state_absolute_error=abs(state_prediction - actual),
                hybrid_absolute_error=abs(hybrid_prediction - actual),
                recent_median_absolute_error=abs(target.baseline_recent_median - actual),
            )
        )

    if not rows:
        return SystemStateBacktestReport(
            status="INSUFFICIENT_DATA",
            aligned_samples=len(samples),
            evaluated_targets=0,
            skipped_training_targets=min(len(samples), min_training_samples),
            history_only_mae=None,
            state_only_mae=None,
            hybrid_mae=None,
            recent_median_mae=None,
            hybrid_median_absolute_error=None,
            hybrid_within_half_x_rate=None,
            hybrid_within_one_x_rate=None,
            hybrid_win_rate_vs_history=None,
            hybrid_improvement_vs_history_pct=None,
            history_log_mae=None,
            state_log_mae=None,
            hybrid_log_mae=None,
            rows=(),
        )

    count = Decimal(len(rows))
    history_errors = [row.history_absolute_error for row in rows]
    state_errors = [row.state_absolute_error for row in rows]
    hybrid_errors = [row.hybrid_absolute_error for row in rows]
    baseline_errors = [row.recent_median_absolute_error for row in rows]

    history_mae = sum(history_errors) / count
    state_mae = sum(state_errors) / count
    hybrid_mae = sum(hybrid_errors) / count
    baseline_mae = sum(baseline_errors) / count
    improvement = (
        (history_mae - hybrid_mae) / history_mae * Decimal("100")
        if history_mae > 0
        else Decimal("0")
    )

    return SystemStateBacktestReport(
        status="COMPLETE",
        aligned_samples=len(samples),
        evaluated_targets=len(rows),
        skipped_training_targets=min_training_samples,
        history_only_mae=_q(history_mae),
        state_only_mae=_q(state_mae),
        hybrid_mae=_q(hybrid_mae),
        recent_median_mae=_q(baseline_mae),
        hybrid_median_absolute_error=_q(median(hybrid_errors)),
        hybrid_within_half_x_rate=_q(
            Decimal(sum(error <= Decimal("0.5") for error in hybrid_errors)) / count
        ),
        hybrid_within_one_x_rate=_q(
            Decimal(sum(error <= Decimal("1.0") for error in hybrid_errors)) / count
        ),
        hybrid_win_rate_vs_history=_q(
            Decimal(
                sum(
                    hybrid_error < history_error
                    for hybrid_error, history_error in zip(hybrid_errors, history_errors)
                )
            )
            / count
        ),
        hybrid_improvement_vs_history_pct=_q(improvement),
        history_log_mae=_q(sum(history_log_errors) / len(history_log_errors)),
        state_log_mae=_q(sum(state_log_errors) / len(state_log_errors)),
        hybrid_log_mae=_q(sum(hybrid_log_errors) / len(hybrid_log_errors)),
        rows=tuple(rows),
    )
