"""Walk-forward audit of immutable prospective pre-round feature snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
import hashlib
import math
from statistics import pstdev
from typing import Mapping, Sequence

from evaluation.adaptive_signal_audit import _RLSProbability


DEFAULT_THRESHOLDS: tuple[Decimal, ...] = (
    Decimal("1.5"),
    Decimal("2"),
    Decimal("5"),
)
DEFAULT_HASH_BINS = 24
DEFAULT_FORGETTING_FACTORS: tuple[float, ...] = (0.95, 0.98, 1.0)


@dataclass(frozen=True, slots=True)
class ProspectiveFeatureSample:
    snapshot_id: str
    locked_at: str
    actual_multiplier: Decimal
    prior_probabilities: tuple[tuple[Decimal, float], ...]
    features: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class ProspectiveThresholdAudit:
    threshold: Decimal
    evaluated_targets: int
    positives: int
    prior_brier: Decimal
    feature_brier: Decimal
    feature_skill_vs_prior_pct: Decimal
    better_blocks: int
    blocks: int
    prediction_stddev: Decimal
    selected_counts: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class ProspectiveFeatureAudit:
    status: str
    paired_snapshots: int
    usable_samples: int
    min_training_samples: int
    feature_dimension: int
    results: tuple[ProspectiveThresholdAudit, ...]


def _q(value: float) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.000001"))


def _bucket(token: str, bins: int) -> tuple[int, float]:
    digest = hashlib.sha256(token.encode("utf-8", errors="replace")).digest()
    index = int.from_bytes(digest[:4], "big") % bins
    sign = 1.0 if digest[4] & 1 else -1.0
    return index, sign


def _hashed_values(
    mapping: Mapping[str, object],
    bins: int,
) -> tuple[float, ...]:
    values = [0.0] * bins
    for token, raw_count in mapping.items():
        if isinstance(raw_count, bool) or not isinstance(raw_count, (int, float)):
            continue
        count = max(0.0, float(raw_count))
        index, sign = _bucket(str(token), bins)
        values[index] += sign * math.log1p(count)
    return tuple(values)


def _snapshot_features(
    lock: Mapping[str, object],
    *,
    hash_bins: int = DEFAULT_HASH_BINS,
) -> tuple[float, ...] | None:
    raw = lock.get("features")
    if not isinstance(raw, Mapping):
        return None

    def numeric(name: str) -> float:
        value = raw.get(name, 0)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0.0
        return float(value)

    trigger_age = lock.get("trigger_age_after_previous_seconds", 0)
    trigger_score = lock.get("trigger_score", 0)
    age = (
        float(trigger_age)
        if isinstance(trigger_age, (int, float)) and not isinstance(trigger_age, bool)
        else 0.0
    )
    score = (
        float(trigger_score)
        if isinstance(trigger_score, (int, float)) and not isinstance(trigger_score, bool)
        else 0.0
    )

    signature_counts = raw.get("signature_counts")
    transition_counts = raw.get("transition_counts")
    if not isinstance(signature_counts, Mapping):
        signature_counts = {}
    if not isinstance(transition_counts, Mapping):
        transition_counts = {}

    sequence = raw.get("recent_sequence")
    gaps: list[float] = []
    sequence_bins = [0.0] * hash_bins
    if isinstance(sequence, list):
        length = len(sequence)
        for position, item in enumerate(sequence):
            if not isinstance(item, Mapping):
                continue
            signature = item.get("signature")
            gap = item.get("gap_ms")
            if isinstance(gap, (int, float)) and not isinstance(gap, bool):
                gaps.append(max(0.0, float(gap)))
            if isinstance(signature, str):
                index, sign = _bucket("seq:" + signature, hash_bins)
                recency = (position + 1) / max(1, length)
                sequence_bins[index] += sign * recency

    gap_mean = sum(gaps) / len(gaps) if gaps else 0.0
    gap_std = pstdev(gaps) if len(gaps) > 1 else 0.0
    gap_last = gaps[-1] if gaps else 0.0

    trigger_signature = lock.get("trigger_signature")
    trigger_bins = [0.0] * hash_bins
    if isinstance(trigger_signature, str):
        index, sign = _bucket("trigger:" + trigger_signature, hash_bins)
        trigger_bins[index] = sign

    scalar = (
        math.log1p(max(0.0, age)),
        math.log1p(max(0.0, score)),
        math.log1p(max(0.0, numeric("event_count_since_previous"))),
        math.log1p(max(0.0, numeric("received_count"))),
        math.log1p(max(0.0, numeric("sent_count"))),
        math.log1p(max(0.0, numeric("binary_count"))),
        math.log1p(max(0.0, numeric("total_payload_bytes"))),
        math.log1p(gap_mean),
        math.log1p(gap_std),
        math.log1p(gap_last),
    )
    return (
        *scalar,
        *_hashed_values(signature_counts, hash_bins),
        *_hashed_values(transition_counts, hash_bins),
        *sequence_bins,
        *trigger_bins,
    )


def load_prospective_feature_samples(
    ledger_events: Sequence[Mapping[str, object]],
    *,
    thresholds: Sequence[Decimal] = DEFAULT_THRESHOLDS,
    hash_bins: int = DEFAULT_HASH_BINS,
) -> tuple[ProspectiveFeatureSample, ...]:
    if hash_bins < 8 or hash_bins > 128:
        raise ValueError("hash_bins must be between 8 and 128.")

    normalized = tuple(Decimal(str(value)) for value in thresholds)
    locks: dict[str, Mapping[str, object]] = {}
    invalidated: set[str] = set()
    scored: list[tuple[Mapping[str, object], Decimal]] = []

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
                scored.append((lock, value))

    result: list[ProspectiveFeatureSample] = []
    for lock, actual in scored:
        forecast = lock.get("forecast")
        if not isinstance(forecast, Mapping):
            continue
        prior = forecast.get("historical_probabilities")
        if not isinstance(prior, Mapping):
            continue

        prior_values: list[tuple[Decimal, float]] = []
        valid = True
        for threshold in normalized:
            raw_probability = prior.get(str(threshold))
            if not isinstance(raw_probability, str):
                valid = False
                break
            probability = float(Decimal(raw_probability))
            if not 0.0 <= probability <= 1.0:
                valid = False
                break
            prior_values.append((threshold, probability))
        if not valid:
            continue

        features = _snapshot_features(lock, hash_bins=hash_bins)
        snapshot_id = lock.get("snapshot_id")
        locked_at = lock.get("locked_at")
        if (
            features is None
            or not isinstance(snapshot_id, str)
            or not isinstance(locked_at, str)
        ):
            continue
        result.append(
            ProspectiveFeatureSample(
                snapshot_id=snapshot_id,
                locked_at=locked_at,
                actual_multiplier=actual,
                prior_probabilities=tuple(prior_values),
                features=features,
            )
        )

    return tuple(result)


@dataclass(slots=True)
class _Candidate:
    forgetting_factor: float
    model: _RLSProbability
    losses: list[float] = field(default_factory=list)

    @property
    def name(self) -> str:
        return f"ff={self.forgetting_factor:.2f}"


def _brier(actual: Sequence[int], probabilities: Sequence[float]) -> float:
    return sum(
        (probability - outcome) ** 2
        for outcome, probability in zip(actual, probabilities)
    ) / len(actual)


def _block_wins(
    actual: Sequence[int],
    feature: Sequence[float],
    prior: Sequence[float],
    blocks: int,
) -> int:
    wins = 0
    count = len(actual)
    for block in range(blocks):
        left = block * count // blocks
        right = (block + 1) * count // blocks
        if right <= left:
            continue
        if _brier(actual[left:right], feature[left:right]) < _brier(
            actual[left:right], prior[left:right]
        ):
            wins += 1
    return wins


def _threshold_audit(
    samples: Sequence[ProspectiveFeatureSample],
    threshold: Decimal,
    *,
    min_training_samples: int,
    selection_window: int,
    forgetting_factors: Sequence[float],
    blocks: int,
) -> ProspectiveThresholdAudit:
    dimension = len(samples[0].features)
    candidates = [
        _Candidate(
            factor,
            _RLSProbability(
                dimension=dimension,
                forgetting_factor=factor,
                regularization=20.0,
            ),
        )
        for factor in forgetting_factors
    ]

    actual_scored: list[int] = []
    prior_scored: list[float] = []
    feature_scored: list[float] = []
    selected_counts: dict[str, int] = {}

    for position, sample in enumerate(samples):
        actual = int(sample.actual_multiplier >= threshold)
        prior = dict(sample.prior_probabilities)[threshold]
        predictions = {
            candidate.name: candidate.model.predict(sample.features)
            for candidate in candidates
        }

        if position >= min_training_samples:
            def recent(candidate: _Candidate) -> tuple[float, str]:
                losses = candidate.losses[-selection_window:]
                score = sum(losses) / len(losses) if losses else float("inf")
                return score, candidate.name

            champion = min(candidates, key=recent)
            probability = predictions[champion.name]
            actual_scored.append(actual)
            prior_scored.append(prior)
            feature_scored.append(probability)
            selected_counts[champion.name] = selected_counts.get(champion.name, 0) + 1

        for candidate in candidates:
            probability = predictions[candidate.name]
            candidate.losses.append((probability - actual) ** 2)
            candidate.model.update(sample.features, actual)

    prior_brier = _brier(actual_scored, prior_scored)
    feature_brier = _brier(actual_scored, feature_scored)
    skill = (
        (prior_brier - feature_brier) / prior_brier * 100
        if prior_brier > 0
        else 0.0
    )
    return ProspectiveThresholdAudit(
        threshold=threshold,
        evaluated_targets=len(actual_scored),
        positives=sum(actual_scored),
        prior_brier=_q(prior_brier),
        feature_brier=_q(feature_brier),
        feature_skill_vs_prior_pct=_q(skill),
        better_blocks=_block_wins(
            actual_scored,
            feature_scored,
            prior_scored,
            blocks,
        ),
        blocks=blocks,
        prediction_stddev=_q(
            pstdev(feature_scored) if len(feature_scored) > 1 else 0.0
        ),
        selected_counts=tuple(
            sorted(selected_counts.items(), key=lambda item: (-item[1], item[0]))
        ),
    )


def walk_forward_prospective_feature_audit(
    samples: Sequence[ProspectiveFeatureSample],
    *,
    thresholds: Sequence[Decimal] = DEFAULT_THRESHOLDS,
    min_training_samples: int = 40,
    selection_window: int = 20,
    forgetting_factors: Sequence[float] = DEFAULT_FORGETTING_FACTORS,
    blocks: int = 4,
) -> ProspectiveFeatureAudit:
    if min_training_samples < 20:
        raise ValueError("min_training_samples must be at least 20.")
    if selection_window < 6:
        raise ValueError("selection_window must be at least 6.")
    if blocks < 2:
        raise ValueError("blocks must be at least 2.")
    if not samples or len(samples) <= min_training_samples:
        dimension = len(samples[0].features) if samples else 0
        return ProspectiveFeatureAudit(
            status="INSUFFICIENT_DATA",
            paired_snapshots=len(samples),
            usable_samples=len(samples),
            min_training_samples=min_training_samples,
            feature_dimension=dimension,
            results=(),
        )

    dimension = len(samples[0].features)
    if any(len(sample.features) != dimension for sample in samples):
        raise ValueError("prospective feature dimension changed.")
    normalized = tuple(Decimal(str(value)) for value in thresholds)
    factors = tuple(float(value) for value in forgetting_factors)

    return ProspectiveFeatureAudit(
        status="COMPLETE",
        paired_snapshots=len(samples),
        usable_samples=len(samples),
        min_training_samples=min_training_samples,
        feature_dimension=dimension,
        results=tuple(
            _threshold_audit(
                samples,
                threshold,
                min_training_samples=min_training_samples,
                selection_window=selection_window,
                forgetting_factors=factors,
                blocks=blocks,
            )
            for threshold in normalized
        ),
    )
