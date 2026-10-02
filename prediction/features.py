"""Leakage-safe feature engineering from completed outcomes strictly before a target."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import math
from typing import Iterable, Sequence


FEATURE_VERSION = 1
MAX_FEATURE_MULTIPLIER = Decimal("1000")
DEFAULT_RATE_THRESHOLDS: tuple[Decimal, ...] = (
    Decimal("1.5"),
    Decimal("2"),
    Decimal("5"),
)


def _multiplier(value: Decimal | str | int | float) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("multipliers must be finite decimal values of at least 1.")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("multipliers must be finite decimal values of at least 1.") from None
    if not result.is_finite() or result < 1:
        raise ValueError("multipliers must be finite decimal values of at least 1.")
    return result


def _feature_value(value: Decimal) -> float:
    """Bound extreme historical values, then log-transform for numerical stability."""
    bounded = min(value, MAX_FEATURE_MULTIPLIER)
    return math.log(float(bounded))


@dataclass(frozen=True, slots=True)
class FeatureConfig:
    lag_count: int = 5
    rolling_window: int = 20
    rate_thresholds: tuple[Decimal | str | int | float, ...] = DEFAULT_RATE_THRESHOLDS

    def __post_init__(self) -> None:
        if type(self.lag_count) is not int or self.lag_count < 1:
            raise ValueError("lag_count must be a positive integer.")
        if type(self.rolling_window) is not int or self.rolling_window < self.lag_count:
            raise ValueError("rolling_window must be an integer at least as large as lag_count.")
        thresholds = tuple(_multiplier(value) for value in self.rate_thresholds)
        if not thresholds or tuple(sorted(set(thresholds))) != thresholds:
            raise ValueError("rate_thresholds must be unique and strictly increasing.")
        object.__setattr__(self, "rate_thresholds", thresholds)

    @property
    def warmup(self) -> int:
        return self.rolling_window

    @property
    def feature_names(self) -> tuple[str, ...]:
        lags = tuple(f"log_multiplier_lag_{index}" for index in range(1, self.lag_count + 1))
        rates = tuple(f"rolling_rate_ge_{threshold}" for threshold in self.rate_thresholds)
        return (*lags, "rolling_mean_log_multiplier", *rates)


@dataclass(frozen=True, slots=True)
class FeatureSample:
    target_index: int
    values: tuple[float, ...]


def feature_vector(
    history: Sequence[Decimal | str | int | float] | Iterable[Decimal | str | int | float],
    config: FeatureConfig | None = None,
) -> tuple[float, ...]:
    """Build one vector using only outcomes already completed before the target."""
    active = config if config is not None else FeatureConfig()
    if not isinstance(active, FeatureConfig):
        raise TypeError("config must be FeatureConfig.")
    values = tuple(_multiplier(value) for value in history)
    if len(values) < active.warmup:
        raise ValueError("insufficient history for the configured feature window.")

    lag_values = values[-active.lag_count :]
    window_values = values[-active.rolling_window :]
    log_window = tuple(_feature_value(value) for value in window_values)
    lag_features = tuple(_feature_value(value) for value in reversed(lag_values))
    mean_log = sum(log_window) / len(log_window)
    rate_features = tuple(
        sum(value >= threshold for value in window_values) / len(window_values)
        for threshold in active.rate_thresholds
    )
    return (*lag_features, mean_log, *rate_features)


def build_feature_samples(
    multipliers: Sequence[Decimal | str | int | float]
    | Iterable[Decimal | str | int | float],
    config: FeatureConfig | None = None,
) -> tuple[FeatureSample, ...]:
    """Create chronological target rows; each row excludes its own and future outcomes."""
    active = config if config is not None else FeatureConfig()
    if not isinstance(active, FeatureConfig):
        raise TypeError("config must be FeatureConfig.")
    values = tuple(_multiplier(value) for value in multipliers)
    return tuple(
        FeatureSample(target_index=index, values=feature_vector(values[:index], active))
        for index in range(active.warmup, len(values))
    )
