"""Live leakage-safe prediction locking and post-round scoring."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_EVEN
import hashlib
import json
from pathlib import Path
from typing import Mapping

from collectors.dom_round_ingest import DOM_OBSERVATION_SOURCE, ingest_dom_rounds
from config.settings import Settings
from database.database import connect_database_readonly
from database.migrations import verify_schema
from models.baseline import BaselineConfig, predict_thresholds
from prediction.adaptive import AdaptiveConfig, predict_next_multiplier


LEDGER_VERSION = 1
DEFAULT_LEDGER_FILENAME = "live-prediction-ledger.jsonl"
SCORE_PLACES = Decimal("0.000001")


@dataclass(frozen=True, slots=True)
class SourceRound:
    round_id: str
    timestamp: str
    multiplier: str


def _utc_now_text() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _load_rounds(settings: Settings) -> tuple[SourceRound, ...]:
    with closing(connect_database_readonly(settings)) as connection:
        verify_schema(connection)
        rows = connection.execute(
            "SELECT round_id, timestamp, multiplier FROM rounds "
            "WHERE source = ? ORDER BY timestamp ASC, round_id ASC",
            (DOM_OBSERVATION_SOURCE,),
        ).fetchall()
    return tuple(
        SourceRound(
            round_id=row["round_id"],
            timestamp=row["timestamp"],
            multiplier=row["multiplier"],
        )
        for row in rows
    )


def _load_ledger(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    events: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("Live prediction ledger contains a non-object event.")
            if value.get("ledger_version") != LEDGER_VERSION:
                raise ValueError("Live prediction ledger version is unsupported.")
            events.append(value)
    return events


def _append_ledger(path: Path, event: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(event)
    payload["ledger_version"] = LEDGER_VERSION
    line = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line + "\n")
        handle.flush()


def _outstanding_lock(events: list[dict[str, object]]) -> dict[str, object] | None:
    closed = {
        event.get("lock_id")
        for event in events
        if event.get("event") in {"prediction_scored", "prediction_invalidated"}
        and isinstance(event.get("lock_id"), str)
    }
    for event in reversed(events):
        if (
            event.get("event") == "prediction_locked"
            and isinstance(event.get("lock_id"), str)
            and event.get("lock_id") not in closed
        ):
            return event
    return None


def _latest_arm(
    events: list[dict[str, object]],
    collector_session_id: str,
) -> dict[str, object] | None:
    for event in reversed(events):
        if (
            event.get("event") == "prediction_armed"
            and event.get("collector_session_id") == collector_session_id
        ):
            return event
    return None


def arm_prediction_session(
    settings: Settings,
    dom_path: Path,
    ledger_path: Path,
    collector_session_id: str,
) -> dict[str, object] | None:
    """Synchronize known results, then require one fresh completion before a new lock."""
    if not isinstance(collector_session_id, str) or not collector_session_id.strip():
        raise ValueError("collector_session_id must be a nonempty string.")

    events = _load_ledger(ledger_path)
    if _outstanding_lock(events) is not None:
        return None

    # Bring the database current before establishing the continuity boundary.
    # Any result already visible at re-arm time is therefore excluded from being
    # treated as the fresh completion that authorizes the next prediction lock.
    ingest_dom_rounds(dom_path, settings)
    rounds = _load_rounds(settings)
    last = rounds[-1] if rounds else None
    event = {
        "event": "prediction_armed",
        "armed_at": _utc_now_text(),
        "collector_session_id": collector_session_id,
        "history_count": len(rounds),
        "history_last_round_id": last.round_id if last is not None else None,
        "reason": "await_fresh_completed_round_before_next_lock",
    }
    _append_ledger(ledger_path, event)
    return event


def invalidate_outstanding_lock(
    ledger_path: Path,
    reason: str = "collector_liveness_lost",
) -> dict[str, object] | None:
    """Close an outstanding prediction lock without scoring it."""
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("invalidation reason must be a nonempty string.")
    events = _load_ledger(ledger_path)
    outstanding = _outstanding_lock(events)
    if outstanding is None:
        return None
    event = {
        "event": "prediction_invalidated",
        "lock_id": outstanding["lock_id"],
        "invalidated_at": _utc_now_text(),
        "reason": reason.strip(),
    }
    _append_ledger(ledger_path, event)
    return event


def invalidate_if_collector_session_changed(
    ledger_path: Path,
    collector_session_id: str,
) -> dict[str, object] | None:
    """Invalidate an outstanding lock created under a different collector session."""
    if not isinstance(collector_session_id, str) or not collector_session_id.strip():
        raise ValueError("collector_session_id must be a nonempty string.")
    events = _load_ledger(ledger_path)
    outstanding = _outstanding_lock(events)
    if outstanding is None:
        return None
    if outstanding.get("collector_session_id") == collector_session_id:
        return None
    return invalidate_outstanding_lock(ledger_path, "collector_session_changed")


def _lock_id(history_count: int, history_last_round_id: str, locked_at: str) -> str:
    material = f"{history_count}|{history_last_round_id}|{locked_at}".encode("utf-8")
    return "lock-" + hashlib.sha256(material).hexdigest()[:24]


def _prediction_lock(
    rounds: tuple[SourceRound, ...],
    config: BaselineConfig,
    collector_session_id: str | None = None,
    adaptive_config: AdaptiveConfig | None = None,
) -> dict[str, object]:
    if not rounds:
        raise ValueError("At least one completed round is required before locking a prediction.")

    history = [round.multiplier for round in rounds]
    prediction = predict_thresholds(history, config)
    point = predict_next_multiplier(history, adaptive_config)
    locked_at = _utc_now_text()
    probabilities = {
        str(item.threshold): (
            None if item.probability is None else format(item.probability, "f")
        )
        for item in prediction.probabilities
    }
    last = rounds[-1]
    return {
        "event": "prediction_locked",
        "lock_id": _lock_id(len(rounds), last.round_id, locked_at),
        "locked_at": locked_at,
        "source": DOM_OBSERVATION_SOURCE,
        "model_name": prediction.model_name,
        "model_version": prediction.model_version,
        "status": prediction.status,
        "mode": prediction.mode,
        "window": prediction.window,
        "min_history": prediction.min_history,
        "history_count": len(rounds),
        "history_last_round_id": last.round_id,
        "history_last_timestamp": last.timestamp,
        "timing_guarantee": "locked_before_next_completed_result",
        "collector_session_id": collector_session_id,
        "threshold_probabilities": probabilities,
        "point_model_name": point.model_name,
        "point_model_version": point.model_version,
        "point_status": point.status,
        "predicted_multiplier": (
            None if point.predicted_multiplier is None else format(point.predicted_multiplier, "f")
        ),
        "prediction_interval": (
            None
            if point.lower_multiplier is None or point.upper_multiplier is None
            else {
                "lower": format(point.lower_multiplier, "f"),
                "upper": format(point.upper_multiplier, "f"),
            }
        ),
        "point_confidence": point.confidence,
        "regime": point.regime,
        "regime_shift": point.regime_shift,
        "point_validation_rows": point.validation_rows,
        "point_validation_mae_log": point.validation_mae_log,
        "recent_median_mae_log": point.recent_median_mae_log,
        "point_components": dict(point.components),
    }


def _score_lock(lock: Mapping[str, object], actual: SourceRound) -> dict[str, object]:
    probabilities = lock.get("threshold_probabilities")
    if not isinstance(probabilities, dict):
        raise ValueError("Prediction lock is missing threshold probabilities.")

    actual_multiplier = Decimal(actual.multiplier)
    scores: dict[str, object] = {}
    point_score: dict[str, object] | None = None
    predicted_multiplier_text = lock.get("predicted_multiplier")
    if isinstance(predicted_multiplier_text, str):
        predicted_multiplier = Decimal(predicted_multiplier_text)
        absolute_error = abs(predicted_multiplier - actual_multiplier).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_EVEN
        )
        relative_error = (
            absolute_error / actual_multiplier
        ).quantize(Decimal("0.000001"), rounding=ROUND_HALF_EVEN)
        interval = lock.get("prediction_interval")
        within_interval = None
        if isinstance(interval, dict):
            lower = interval.get("lower")
            upper = interval.get("upper")
            if isinstance(lower, str) and isinstance(upper, str):
                within_interval = Decimal(lower) <= actual_multiplier <= Decimal(upper)
        point_score = {
            "predicted_multiplier": predicted_multiplier_text,
            "absolute_error": format(absolute_error, "f"),
            "relative_error": format(relative_error, "f"),
            "within_interval": within_interval,
        }
    for threshold_text, probability_text in probabilities.items():
        if not isinstance(threshold_text, str):
            raise ValueError("Prediction threshold must be text.")
        threshold = Decimal(threshold_text)
        outcome = int(actual_multiplier >= threshold)
        if probability_text is None:
            scores[threshold_text] = {"actual": outcome, "brier": None}
            continue
        if not isinstance(probability_text, str):
            raise ValueError("Prediction probability must be decimal text or null.")
        probability = Decimal(probability_text)
        brier = ((probability - Decimal(outcome)) ** 2).quantize(
            SCORE_PLACES, rounding=ROUND_HALF_EVEN
        )
        scores[threshold_text] = {
            "actual": outcome,
            "brier": format(brier, "f"),
        }

    return {
        "event": "prediction_scored",
        "lock_id": lock["lock_id"],
        "scored_at": _utc_now_text(),
        "actual_round_id": actual.round_id,
        "actual_timestamp": actual.timestamp,
        "actual_multiplier": actual.multiplier,
        "point_score": point_score,
        "threshold_scores": scores,
    }


def process_live_prediction_cycle(
    settings: Settings,
    dom_path: Path,
    ledger_path: Path,
    config: BaselineConfig,
    collector_session_id: str | None = None,
    adaptive_config: AdaptiveConfig | None = None,
) -> tuple[dict[str, object], ...]:
    """Synchronize DOM results, score an existing lock, then lock the next result.

    A lock is never created retroactively. If more than one completed result arrived
    after an outstanding lock, only the first scores that lock; additional results
    are explicitly recorded as an unpredicted gap before a new lock is created.
    """
    ingest_dom_rounds(dom_path, settings)
    rounds = _load_rounds(settings)
    events = _load_ledger(ledger_path)
    emitted: list[dict[str, object]] = []
    outstanding = _outstanding_lock(events)

    if outstanding is None:
        if collector_session_id is not None:
            arm = _latest_arm(events, collector_session_id)
            if arm is None:
                arm = arm_prediction_session(
                    settings, dom_path, ledger_path, collector_session_id
                )
                if arm is not None:
                    emitted.append(arm)
                return tuple(emitted)

            arm_history_count = arm.get("history_count")
            if not isinstance(arm_history_count, int) or arm_history_count < 0:
                raise ValueError("Prediction arm history metadata is invalid.")
            if len(rounds) <= arm_history_count:
                return tuple(emitted)

        if rounds:
            lock = _prediction_lock(rounds, config, collector_session_id, adaptive_config)
            _append_ledger(ledger_path, lock)
            emitted.append(lock)
        return tuple(emitted)

    history_count = outstanding.get("history_count")
    last_round_id = outstanding.get("history_last_round_id")
    if not isinstance(history_count, int) or history_count < 1 or not isinstance(last_round_id, str):
        raise ValueError("Prediction lock history metadata is invalid.")
    if history_count > len(rounds):
        raise ValueError("Prediction ledger history exceeds the current stored source history.")
    if rounds[history_count - 1].round_id != last_round_id:
        raise ValueError("Stored round history no longer matches the prediction lock.")

    new_rounds = rounds[history_count:]
    if not new_rounds:
        return ()

    score = _score_lock(outstanding, new_rounds[0])
    _append_ledger(ledger_path, score)
    emitted.append(score)

    if len(new_rounds) > 1:
        gap = {
            "event": "prediction_gap",
            "observed_at": _utc_now_text(),
            "after_lock_id": outstanding["lock_id"],
            "unpredicted_rounds": len(new_rounds) - 1,
            "first_unpredicted_round_id": new_rounds[1].round_id,
            "last_unpredicted_round_id": new_rounds[-1].round_id,
            "reason": "multiple_completed_results_arrived_before_next_lock",
        }
        _append_ledger(ledger_path, gap)
        emitted.append(gap)

    lock = _prediction_lock(rounds, config, collector_session_id, adaptive_config)
    _append_ledger(ledger_path, lock)
    emitted.append(lock)
    return tuple(emitted)
