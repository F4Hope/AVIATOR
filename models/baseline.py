"""Leakage-safe frequency baselines for multiplier-threshold prediction."""

from dataclasses import dataclass
from decimal import Context, Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
from typing import Iterable, Sequence


MODEL_NAME = "historical-threshold-frequency"
MODEL_VERSION = "1"
DEFAULT_THRESHOLDS: tuple[Decimal, ...] = (
    Decimal("1.5"),
    Decimal("2"),
    Decimal("5"),
)
PROBABILITY_PLACES = Decimal("0.000001")
ARITHMETIC_PRECISION = 50


def _decimal_threshold(value: Decimal | str | int | float) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("thresholds must be finite decimal values of at least 1.")
    try:
        threshold = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("thresholds must be finite decimal values of at least 1.") from None
    if not threshold.is_finite() or threshold < 1:
        raise ValueError("thresholds must be finite decimal values of at least 1.")
    return threshold


@dataclass(frozen=True, slots=True)
class BaselineConfig:
    """Configuration for an expanding or rolling empirical-frequency baseline."""

    thresholds: tuple[Decimal | str | int | float, ...] = DEFAULT_THRESHOLDS
    mode: str = "expanding"
    window: int | None = None
    min_history: int = 50

    def __post_init__(self) -> None:
        thresholds = tuple(_decimal_threshold(value) for value in self.thresholds)
        if not thresholds:
            raise ValueError("at least one threshold is required.")
        if tuple(sorted(set(thresholds))) != thresholds:
            raise ValueError("thresholds must be unique and strictly increasing.")
        if self.mode not in {"expanding", "rolling"}:
            raise ValueError("mode must be 'expanding' or 'rolling'.")
        if type(self.min_history) is not int or self.min_history < 1:
            raise ValueError("min_history must be a positive integer.")
        if self.mode == "expanding":
            if self.window is not None:
                raise ValueError("window is only valid for rolling mode.")
        else:
            if type(self.window) is not int or self.window < 1:
                raise ValueError("rolling mode requires a positive integer window.")
            if self.window < self.min_history:
                raise ValueError("rolling window cannot be smaller than min_history.")
        object.__setattr__(self, "thresholds", thresholds)


@dataclass(frozen=True, slots=True)
class ThresholdProbability:
    threshold: Decimal
    probability: Decimal | None


@dataclass(frozen=True, slots=True)
class BaselinePrediction:
    """One baseline estimate built only from supplied prior outcomes."""

    model_name: str
    model_version: str
    mode: str
    window: int | None
    min_history: int
    available_history: int
    sample_count: int
    probabilities: tuple[ThresholdProbability, ...]

    @property
    def status(self) -> str:
        return "READY" if self.sample_count >= self.min_history else "INSUFFICIENT_DATA"


def _multiplier(value: Decimal | str | int | float) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("historical multipliers must be finite decimal values of at least 1.")
    try:
        multiplier = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("historical multipliers must be finite decimal values of at least 1.") from None
    if not multiplier.is_finite() or multiplier < 1:
        raise ValueError("historical multipliers must be finite decimal values of at least 1.")
    return multiplier


def predict_thresholds(
    history: Sequence[Decimal | str | int | float] | Iterable[Decimal | str | int | float],
    config: BaselineConfig | None = None,
) -> BaselinePrediction:
    """Estimate P(multiplier >= threshold) from outcomes strictly supplied as history.

    This function has no access to a target outcome, timestamps, database state, or
    future records. Chronological callers are responsible for passing only rounds
    completed before the target prediction point.
    """
    active = config if config is not None else BaselineConfig()
    if not isinstance(active, BaselineConfig):
        raise TypeError("config must be BaselineConfig.")
    values = tuple(_multiplier(value) for value in history)
    selected = values if active.mode == "expanding" else values[-active.window :]
    sample_count = len(selected)

    if sample_count < active.min_history:
        probabilities = tuple(
            ThresholdProbability(threshold, None) for threshold in active.thresholds
        )
    else:
        with localcontext(Context(prec=ARITHMETIC_PRECISION, rounding=ROUND_HALF_EVEN)):
            probabilities = tuple(
                ThresholdProbability(
                    threshold,
                    (Decimal(sum(value >= threshold for value in selected)) / sample_count)
                    .quantize(PROBABILITY_PLACES),
                )
                for threshold in active.thresholds
            )

    known = [item.probability for item in probabilities if item.probability is not None]
    if any(left < right for left, right in zip(known, known[1:])):
        raise RuntimeError("threshold probabilities must be non-increasing.")

    return BaselinePrediction(
        model_name=MODEL_NAME,
        model_version=MODEL_VERSION,
        mode=active.mode,
        window=active.window,
        min_history=active.min_history,
        available_history=len(values),
        sample_count=sample_count,
        probabilities=probabilities,
    )
