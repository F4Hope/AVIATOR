"""Prospective audit of locked live threshold forecasts.

The audit reads the append-only live prediction ledger and compares the chosen
forecast against the historical baseline and raw challenger only on rounds that
were genuinely locked before the result arrived.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Mapping, Sequence


PLACES = Decimal("0.000001")


@dataclass(frozen=True, slots=True)
class ThresholdLiveAudit:
    threshold: Decimal
    scored_forecasts: int
    challenger_selected: int
    challenger_selection_rate: Decimal | None
    mean_chosen_brier: Decimal | None
    mean_baseline_brier: Decimal | None
    mean_raw_candidate_brier: Decimal | None
    comparable_candidate_rows: int
    chosen_skill_vs_baseline_pct: Decimal | None
    raw_candidate_skill_vs_baseline_pct: Decimal | None
    average_forecast: Decimal | None
    observed_rate: Decimal | None
    calibration_gap: Decimal | None


@dataclass(frozen=True, slots=True)
class LiveThresholdAuditReport:
    scored_locks: int
    invalidated_locks: int
    unpredicted_rounds: int
    thresholds: tuple[ThresholdLiveAudit, ...]


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(PLACES, rounding=ROUND_HALF_EVEN)


def _probability(value: object) -> Decimal | None:
    if not isinstance(value, str):
        return None
    try:
        result = Decimal(value)
    except Exception:
        return None
    if not result.is_finite() or not Decimal("0") <= result <= Decimal("1"):
        return None
    return result


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    return _quantize(sum(values) / Decimal(len(values)))


def _skill(baseline: Decimal | None, challenger: Decimal | None) -> Decimal | None:
    if baseline is None or challenger is None or baseline <= 0:
        return None
    return _quantize((baseline - challenger) / baseline * Decimal("100"))


def audit_live_thresholds(
    events: Sequence[Mapping[str, object]],
) -> LiveThresholdAuditReport:
    locks: dict[str, Mapping[str, object]] = {}
    invalidated: set[str] = set()
    scored_locks = 0
    invalidated_locks = 0
    unpredicted_rounds = 0

    rows: dict[str, list[dict[str, object]]] = {}

    for event in events:
        kind = event.get("event")
        lock_id = event.get("lock_id")

        if kind == "prediction_locked" and isinstance(lock_id, str):
            locks[lock_id] = event
            continue

        if kind == "prediction_invalidated" and isinstance(lock_id, str):
            if lock_id not in invalidated:
                invalidated.add(lock_id)
                invalidated_locks += 1
            continue

        if kind == "prediction_gap":
            missed = event.get("unpredicted_rounds")
            if isinstance(missed, int) and missed > 0:
                unpredicted_rounds += missed
            continue

        if kind != "prediction_scored" or not isinstance(lock_id, str):
            continue
        if lock_id in invalidated:
            continue

        lock = locks.get(lock_id)
        if lock is None:
            continue

        actual_text = event.get("actual_multiplier")
        if not isinstance(actual_text, str):
            continue
        try:
            actual = Decimal(actual_text)
        except Exception:
            continue
        if not actual.is_finite() or actual < 1:
            continue

        probabilities = lock.get("threshold_probabilities")
        if not isinstance(probabilities, Mapping):
            continue
        details = lock.get("threshold_model_details")
        detail_map = details if isinstance(details, Mapping) else {}

        scored_locks += 1

        for threshold_text, chosen_raw in probabilities.items():
            if not isinstance(threshold_text, str):
                continue
            try:
                threshold = Decimal(threshold_text)
            except Exception:
                continue
            chosen = _probability(chosen_raw)
            if chosen is None:
                continue

            outcome = Decimal(int(actual >= threshold))
            chosen_brier = _quantize((chosen - outcome) ** 2)

            raw_detail = detail_map.get(threshold_text)
            detail = raw_detail if isinstance(raw_detail, Mapping) else {}
            baseline = _probability(detail.get("baseline_probability"))
            raw_candidate = _probability(detail.get("raw_candidate_probability"))

            baseline_brier = (
                None
                if baseline is None
                else _quantize((baseline - outcome) ** 2)
            )
            candidate_brier = (
                None
                if raw_candidate is None
                else _quantize((raw_candidate - outcome) ** 2)
            )

            rows.setdefault(threshold_text, []).append(
                {
                    "threshold": threshold,
                    "chosen": chosen,
                    "outcome": outcome,
                    "chosen_brier": chosen_brier,
                    "baseline_brier": baseline_brier,
                    "candidate_brier": candidate_brier,
                    "challenger_selected": (
                        detail.get("source") == "gated_logistic_challenger"
                    ),
                }
            )

    reports: list[ThresholdLiveAudit] = []
    for threshold_text in sorted(rows, key=Decimal):
        threshold_rows = rows[threshold_text]
        threshold = threshold_rows[0]["threshold"]
        if not isinstance(threshold, Decimal):
            continue

        chosen_briers = [
            row["chosen_brier"]
            for row in threshold_rows
            if isinstance(row["chosen_brier"], Decimal)
        ]
        baseline_briers = [
            row["baseline_brier"]
            for row in threshold_rows
            if isinstance(row["baseline_brier"], Decimal)
        ]
        candidate_pairs = [
            (row["baseline_brier"], row["candidate_brier"])
            for row in threshold_rows
            if isinstance(row["baseline_brier"], Decimal)
            and isinstance(row["candidate_brier"], Decimal)
        ]
        candidate_briers = [candidate for _, candidate in candidate_pairs]
        comparable_baselines = [baseline for baseline, _ in candidate_pairs]

        mean_chosen = _mean(chosen_briers)
        mean_baseline = _mean(baseline_briers)
        mean_candidate = _mean(candidate_briers)
        mean_comparable_baseline = _mean(comparable_baselines)
        forecasts = [
            row["chosen"]
            for row in threshold_rows
            if isinstance(row["chosen"], Decimal)
        ]
        outcomes = [
            row["outcome"]
            for row in threshold_rows
            if isinstance(row["outcome"], Decimal)
        ]
        average_forecast = _mean(forecasts)
        observed_rate = _mean(outcomes)
        calibration_gap = (
            None
            if average_forecast is None or observed_rate is None
            else _quantize(observed_rate - average_forecast)
        )
        challenger_selected = sum(
            bool(row["challenger_selected"])
            for row in threshold_rows
        )
        selection_rate = _quantize(
            Decimal(challenger_selected) / Decimal(len(threshold_rows))
        )

        reports.append(
            ThresholdLiveAudit(
                threshold=threshold,
                scored_forecasts=len(threshold_rows),
                challenger_selected=challenger_selected,
                challenger_selection_rate=selection_rate,
                mean_chosen_brier=mean_chosen,
                mean_baseline_brier=mean_baseline,
                mean_raw_candidate_brier=mean_candidate,
                comparable_candidate_rows=len(candidate_pairs),
                chosen_skill_vs_baseline_pct=_skill(
                    mean_baseline,
                    mean_chosen,
                ),
                raw_candidate_skill_vs_baseline_pct=_skill(
                    mean_comparable_baseline,
                    mean_candidate,
                ),
                average_forecast=average_forecast,
                observed_rate=observed_rate,
                calibration_gap=calibration_gap,
            )
        )

    return LiveThresholdAuditReport(
        scored_locks=scored_locks,
        invalidated_locks=invalidated_locks,
        unpredicted_rounds=unpredicted_rounds,
        thresholds=tuple(reports),
    )
