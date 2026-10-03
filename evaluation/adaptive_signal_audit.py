"""Prequential adaptive signal audit with online model switching.

Each candidate is a recursive least-squares probability model updated only after
the target result is known. Candidates vary by feature family and forgetting
factor. The current champion is chosen from recent *past* Brier losses, so model
switches are leakage-safe and can react to regime changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from prediction.system_state import SystemStateSample


DEFAULT_THRESHOLDS: tuple[Decimal, ...] = (
    Decimal("1.5"),
    Decimal("2"),
    Decimal("5"),
    Decimal("10"),
)
DEFAULT_FORGETTING_FACTORS: tuple[float, ...] = (0.90, 0.95, 0.98, 1.00)
FEATURE_MODES: tuple[str, ...] = ("history", "state", "hybrid")


def _clip_probability(value: float) -> float:
    return max(0.001, min(0.999, float(value)))


def _q(value: float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.000001"))


def _features(sample: SystemStateSample, mode: str) -> tuple[float, ...]:
    if mode == "history":
        raw = sample.history_features
    elif mode == "state":
        raw = sample.state_features
    elif mode == "hybrid":
        raw = sample.history_features + sample.state_features
    else:
        raise ValueError("unknown feature mode.")
    # Existing system-state features are ratios/log-counts and history features
    # are log multipliers/rates. Bound extreme numerical accidents without
    # discarding direction.
    return tuple(max(-12.0, min(12.0, float(value))) for value in raw)


@dataclass(slots=True)
class _RLSProbability:
    dimension: int
    forgetting_factor: float
    regularization: float = 10.0

    def __post_init__(self) -> None:
        if self.dimension < 1:
            raise ValueError("dimension must be positive.")
        if not 0.5 <= self.forgetting_factor <= 1.0:
            raise ValueError("forgetting_factor must be between 0.5 and 1.")
        if self.regularization <= 0:
            raise ValueError("regularization must be positive.")
        size = self.dimension + 1
        self.weights = [0.0] * size
        initial = 1.0 / self.regularization
        self.covariance = [
            [initial if row == column else 0.0 for column in range(size)]
            for row in range(size)
        ]

    def _vector(self, values: Sequence[float]) -> list[float]:
        if len(values) != self.dimension:
            raise ValueError("feature dimension changed.")
        return [1.0, *[float(value) for value in values]]

    def predict(self, values: Sequence[float]) -> float:
        vector = self._vector(values)
        raw = sum(weight * value for weight, value in zip(self.weights, vector))
        return _clip_probability(raw)

    def update(self, values: Sequence[float], actual: int) -> None:
        vector = self._vector(values)
        p_x = [
            sum(row[column] * vector[column] for column in range(len(vector)))
            for row in self.covariance
        ]
        denominator = self.forgetting_factor + sum(
            vector[index] * p_x[index] for index in range(len(vector))
        )
        if denominator <= 1e-12:
            return
        gain = [value / denominator for value in p_x]
        raw_prediction = sum(
            weight * value for weight, value in zip(self.weights, vector)
        )
        error = float(actual) - raw_prediction
        self.weights = [
            weight + gain_value * error
            for weight, gain_value in zip(self.weights, gain)
        ]

        size = len(vector)
        new_covariance = [[0.0] * size for _ in range(size)]
        for row in range(size):
            for column in range(size):
                correction = gain[row] * sum(
                    vector[k] * self.covariance[k][column]
                    for k in range(size)
                )
                new_covariance[row][column] = (
                    self.covariance[row][column] - correction
                ) / self.forgetting_factor
        self.covariance = new_covariance


@dataclass(frozen=True, slots=True)
class AdaptiveThresholdResult:
    threshold: Decimal
    evaluated_targets: int
    positives: int
    prevalence: Decimal
    baseline_brier: Decimal
    history_adaptive_brier: Decimal
    state_adaptive_brier: Decimal
    hybrid_adaptive_brier: Decimal
    champion_brier: Decimal
    champion_skill_vs_baseline_pct: Decimal
    champion_skill_vs_history_pct: Decimal
    champion_switches: int
    fast_forgetting_selection_rate: Decimal
    state_or_hybrid_selection_rate: Decimal
    champion_better_blocks_than_history: int
    blocks: int
    selected_counts: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class AdaptiveSignalAudit:
    status: str
    aligned_samples: int
    min_training_samples: int
    selection_window: int
    results: tuple[AdaptiveThresholdResult, ...]


@dataclass(slots=True)
class _Candidate:
    mode: str
    forgetting_factor: float
    model: _RLSProbability
    losses: list[float]

    @property
    def name(self) -> str:
        return f"{self.mode}:ff={self.forgetting_factor:.2f}"


def _mean_recent(values: Sequence[float], window: int) -> float:
    subset = values[-window:]
    return sum(subset) / len(subset) if subset else float("inf")


def _brier(actual: Sequence[int], predicted: Sequence[float]) -> float:
    if not actual or len(actual) != len(predicted):
        raise ValueError("Brier inputs must be nonempty and aligned.")
    return sum(
        (probability - outcome) ** 2
        for outcome, probability in zip(actual, predicted)
    ) / len(actual)


def _block_wins(
    actual: Sequence[int],
    left: Sequence[float],
    right: Sequence[float],
    blocks: int,
) -> int:
    wins = 0
    count = len(actual)
    for block in range(blocks):
        start = block * count // blocks
        end = (block + 1) * count // blocks
        if end <= start:
            continue
        if _brier(actual[start:end], left[start:end]) < _brier(
            actual[start:end], right[start:end]
        ):
            wins += 1
    return wins


def _run_threshold(
    samples: Sequence[SystemStateSample],
    threshold: Decimal,
    min_training_samples: int,
    selection_window: int,
    forgetting_factors: Sequence[float],
    blocks: int,
) -> AdaptiveThresholdResult:
    dimensions = {
        mode: len(_features(samples[0], mode))
        for mode in FEATURE_MODES
    }
    candidates = [
        _Candidate(
            mode=mode,
            forgetting_factor=float(factor),
            model=_RLSProbability(
                dimension=dimensions[mode],
                forgetting_factor=float(factor),
            ),
            losses=[],
        )
        for mode in FEATURE_MODES
        for factor in forgetting_factors
    ]

    actual_scored: list[int] = []
    baseline_scored: list[float] = []
    history_scored: list[float] = []
    state_scored: list[float] = []
    hybrid_scored: list[float] = []
    champion_scored: list[float] = []
    selected_names: list[str] = []
    selected_counts: dict[str, int] = {}
    prior_positive = 0

    for position, sample in enumerate(samples):
        actual = int(sample.actual_multiplier >= threshold)
        denominator = position
        baseline_probability = (
            prior_positive / denominator
            if denominator > 0
            else 0.5
        )

        predictions: dict[str, float] = {}
        for candidate in candidates:
            predictions[candidate.name] = candidate.model.predict(
                _features(sample, candidate.mode)
            )

        if position >= min_training_samples:
            eligible = [
                candidate
                for candidate in candidates
                if len(candidate.losses) >= min(6, selection_window)
            ]
            if not eligible:
                eligible = candidates

            def score(candidate: _Candidate) -> tuple[float, str]:
                return (
                    _mean_recent(candidate.losses, selection_window),
                    candidate.name,
                )

            champion = min(eligible, key=score)
            per_mode: dict[str, _Candidate] = {}
            for mode in FEATURE_MODES:
                mode_candidates = [
                    candidate for candidate in eligible if candidate.mode == mode
                ]
                if not mode_candidates:
                    mode_candidates = [
                        candidate for candidate in candidates if candidate.mode == mode
                    ]
                per_mode[mode] = min(mode_candidates, key=score)

            actual_scored.append(actual)
            baseline_scored.append(_clip_probability(baseline_probability))
            history_scored.append(predictions[per_mode["history"].name])
            state_scored.append(predictions[per_mode["state"].name])
            hybrid_scored.append(predictions[per_mode["hybrid"].name])
            champion_scored.append(predictions[champion.name])
            selected_names.append(champion.name)
            selected_counts[champion.name] = selected_counts.get(champion.name, 0) + 1

        for candidate in candidates:
            probability = predictions[candidate.name]
            candidate.losses.append((probability - actual) ** 2)
            candidate.model.update(_features(sample, candidate.mode), actual)

        prior_positive += actual

    baseline_brier = _brier(actual_scored, baseline_scored)
    history_brier = _brier(actual_scored, history_scored)
    state_brier = _brier(actual_scored, state_scored)
    hybrid_brier = _brier(actual_scored, hybrid_scored)
    champion_brier = _brier(actual_scored, champion_scored)
    switches = sum(
        current != previous
        for previous, current in zip(selected_names, selected_names[1:])
    )
    fast_count = sum(
        any(
            name.endswith(f"ff={factor:.2f}")
            for factor in forgetting_factors
            if factor <= 0.95
        )
        for name in selected_names
    )
    nonhistory_count = sum(
        not name.startswith("history:")
        for name in selected_names
    )
    count = len(actual_scored)

    return AdaptiveThresholdResult(
        threshold=threshold,
        evaluated_targets=count,
        positives=sum(actual_scored),
        prevalence=_q(sum(actual_scored) / count),
        baseline_brier=_q(baseline_brier),
        history_adaptive_brier=_q(history_brier),
        state_adaptive_brier=_q(state_brier),
        hybrid_adaptive_brier=_q(hybrid_brier),
        champion_brier=_q(champion_brier),
        champion_skill_vs_baseline_pct=_q(
            (baseline_brier - champion_brier) / baseline_brier * 100
            if baseline_brier > 0 else 0.0
        ),
        champion_skill_vs_history_pct=_q(
            (history_brier - champion_brier) / history_brier * 100
            if history_brier > 0 else 0.0
        ),
        champion_switches=switches,
        fast_forgetting_selection_rate=_q(fast_count / count),
        state_or_hybrid_selection_rate=_q(nonhistory_count / count),
        champion_better_blocks_than_history=_block_wins(
            actual_scored,
            champion_scored,
            history_scored,
            blocks,
        ),
        blocks=blocks,
        selected_counts=tuple(
            sorted(selected_counts.items(), key=lambda item: (-item[1], item[0]))
        ),
    )


def walk_forward_adaptive_signal_audit(
    samples: Sequence[SystemStateSample],
    thresholds: Sequence[Decimal] = DEFAULT_THRESHOLDS,
    min_training_samples: int = 40,
    selection_window: int = 16,
    forgetting_factors: Sequence[float] = DEFAULT_FORGETTING_FACTORS,
    blocks: int = 4,
) -> AdaptiveSignalAudit:
    if len(samples) <= min_training_samples:
        return AdaptiveSignalAudit(
            status="INSUFFICIENT_DATA",
            aligned_samples=len(samples),
            min_training_samples=min_training_samples,
            selection_window=selection_window,
            results=(),
        )
    if min_training_samples < 20:
        raise ValueError("min_training_samples must be at least 20.")
    if selection_window < 6:
        raise ValueError("selection_window must be at least 6.")
    if blocks < 2:
        raise ValueError("blocks must be at least 2.")
    factors = tuple(float(value) for value in forgetting_factors)
    if not factors or any(not 0.5 <= value <= 1.0 for value in factors):
        raise ValueError("forgetting_factors must be between 0.5 and 1.")
    normalized = tuple(Decimal(str(value)) for value in thresholds)
    if not normalized or tuple(sorted(set(normalized))) != normalized:
        raise ValueError("thresholds must be unique and strictly increasing.")

    return AdaptiveSignalAudit(
        status="COMPLETE",
        aligned_samples=len(samples),
        min_training_samples=min_training_samples,
        selection_window=selection_window,
        results=tuple(
            _run_threshold(
                samples,
                threshold,
                min_training_samples,
                selection_window,
                factors,
                blocks,
            )
            for threshold in normalized
        ),
    )
