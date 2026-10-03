"""Walk-forward evaluation for adaptive next-round point prediction."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from statistics import median
from typing import Iterable, Sequence

from prediction.adaptive import AdaptiveConfig, predict_next_multiplier


@dataclass(frozen=True, slots=True)
class PointBacktestRow:
    target_index: int
    predicted: Decimal
    actual: Decimal
    absolute_error: Decimal
    recent_median: Decimal
    recent_median_absolute_error: Decimal
    within_interval: bool


@dataclass(frozen=True, slots=True)
class PointBacktestReport:
    status: str
    stored_rounds: int
    evaluated_targets: int
    skipped_targets: int
    mae: Decimal | None
    median_absolute_error: Decimal | None
    recent_median_mae: Decimal | None
    within_half_x_rate: Decimal | None
    within_one_x_rate: Decimal | None
    interval_coverage: Decimal | None
    rows: tuple[PointBacktestRow, ...]


def _q(value: float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.000001"))


def walk_forward_point_evaluate(
    multipliers: Sequence[Decimal | str | int | float]
    | Iterable[Decimal | str | int | float],
    config: AdaptiveConfig | None = None,
) -> PointBacktestReport:
    active = config if config is not None else AdaptiveConfig()
    values = tuple(Decimal(str(value)) for value in multipliers)
    rows: list[PointBacktestRow] = []
    skipped = 0

    for index in range(active.min_history, len(values)):
        prediction = predict_next_multiplier(values[:index], active)
        if prediction.status != "READY" or prediction.predicted_multiplier is None:
            skipped += 1
            continue

        actual = values[index]
        predicted = prediction.predicted_multiplier
        recent = values[max(0, index - active.short_window) : index]
        recent_median = Decimal(str(median(float(value) for value in recent)))
        lower = prediction.lower_multiplier
        upper = prediction.upper_multiplier
        within = (
            lower is not None
            and upper is not None
            and lower <= actual <= upper
        )
        rows.append(
            PointBacktestRow(
                target_index=index,
                predicted=predicted,
                actual=actual,
                absolute_error=abs(predicted - actual),
                recent_median=recent_median,
                recent_median_absolute_error=abs(recent_median - actual),
                within_interval=within,
            )
        )

    if not rows:
        return PointBacktestReport(
            status="INSUFFICIENT_DATA",
            stored_rounds=len(values),
            evaluated_targets=0,
            skipped_targets=skipped,
            mae=None,
            median_absolute_error=None,
            recent_median_mae=None,
            within_half_x_rate=None,
            within_one_x_rate=None,
            interval_coverage=None,
            rows=(),
        )

    errors = [row.absolute_error for row in rows]
    baseline_errors = [row.recent_median_absolute_error for row in rows]
    count = Decimal(len(rows))
    return PointBacktestReport(
        status="COMPLETE",
        stored_rounds=len(values),
        evaluated_targets=len(rows),
        skipped_targets=skipped,
        mae=_q(sum(errors) / count),
        median_absolute_error=_q(median(errors)),
        recent_median_mae=_q(sum(baseline_errors) / count),
        within_half_x_rate=_q(
            Decimal(sum(error <= Decimal("0.5") for error in errors)) / count
        ),
        within_one_x_rate=_q(
            Decimal(sum(error <= Decimal("1") for error in errors)) / count
        ),
        interval_coverage=_q(
            Decimal(sum(row.within_interval for row in rows)) / count
        ),
        rows=tuple(rows),
    )
