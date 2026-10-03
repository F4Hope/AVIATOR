"""Run prospective PRE-ROUND snapshot capture alongside the native collector."""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import sys
import time

from collectors.dom_round_ingest import DOM_OBSERVATION_SOURCE, ingest_dom_rounds
from config.settings import DEVELOPMENT_PHASE, load_settings
from database.database import connect_database_readonly
from database.migrations import verify_schema
from prediction.prestart_trigger import (
    discover_prestart_trigger_signatures,
    event_signature,
)
from prediction.prospective_forecast import (
    attach_forecast,
    forecast_snapshot_probabilities,
    instant_prior_event,
)
from prediction.prospective import (
    DEFAULT_SNAPSHOT_LEDGER_FILENAME,
    SnapshotRound,
    append_snapshot_event,
    build_pre_round_snapshot,
    invalidate_snapshot,
    load_snapshot_ledger,
    make_arm_event,
    outstanding_snapshot,
    score_pre_round_snapshot,
    utc_text,
)
from prediction.round_state import (
    discover_dom_probe_paths,
    infer_round_starts,
    load_completed_round_boundaries,
    load_live_multiplier_observations,
)
from prediction.system_state import (
    CDP_FRAME_KINDS,
    SystemProbeEvent,
    _parse_timestamp,
    discover_network_probe_paths,
    load_system_probe_events,
)


def _heartbeat_session(path: Path, max_age_seconds: float) -> str | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            return None
        observed_at = value.get("observed_at")
        session_id = value.get("collector_session_id")
        if (
            value.get("kind") != "dom_probe_heartbeat"
            or not isinstance(observed_at, str)
            or not isinstance(session_id, str)
            or not session_id
            or value.get("capture_ready") is False
        ):
            return None
        heartbeat_at = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        age = (datetime.now(UTC) - heartbeat_at.astimezone(UTC)).total_seconds()
        return session_id if 0 <= age <= max_age_seconds else None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _load_rounds(settings) -> tuple[SnapshotRound, ...]:
    with closing(connect_database_readonly(settings)) as connection:
        verify_schema(connection)
        rows = connection.execute(
            "SELECT round_id, timestamp, multiplier FROM rounds "
            "WHERE source = ? ORDER BY timestamp ASC, round_id ASC",
            (DOM_OBSERVATION_SOURCE,),
        ).fetchall()
    return tuple(
        SnapshotRound(
            round_id=row["round_id"],
            timestamp=row["timestamp"],
            multiplier=row["multiplier"],
        )
        for row in rows
    )


def _network_event(document: object) -> SystemProbeEvent | None:
    if not isinstance(document, dict) or document.get("kind") not in CDP_FRAME_KINDS:
        return None
    timestamp = _parse_timestamp(document.get("observed_at"))
    payload = document.get("payload")
    if timestamp is None or not isinstance(payload, dict):
        return None

    payload_bytes = payload.get("bytes")
    payload_format = payload.get("format")
    if (
        not isinstance(payload_bytes, int)
        or payload_bytes < 0
        or not isinstance(payload_format, str)
    ):
        return None

    kind = document.get("kind")
    assert isinstance(kind, str)
    direction = "received" if kind.endswith("received") else "sent"
    socket_raw = document.get("socket_id")
    url_raw = document.get("url")
    socket = (
        socket_raw
        if isinstance(socket_raw, str)
        else url_raw
        if isinstance(url_raw, str)
        else "unknown"
    )

    fingerprint = payload.get("binary_fingerprint")
    entropy = zero_ratio = high_bit_ratio = None
    unique_byte_bucket = None
    if isinstance(fingerprint, dict):
        raw_entropy = fingerprint.get("entropy_bucket")
        raw_zero = fingerprint.get("zero_ratio_bucket")
        raw_high = fingerprint.get("high_bit_ratio_bucket")
        raw_unique = fingerprint.get("unique_byte_bucket")
        entropy = float(raw_entropy) if isinstance(raw_entropy, (int, float)) else None
        zero_ratio = float(raw_zero) if isinstance(raw_zero, (int, float)) else None
        high_bit_ratio = float(raw_high) if isinstance(raw_high, (int, float)) else None
        unique_byte_bucket = int(raw_unique) if isinstance(raw_unique, int) else None

    return SystemProbeEvent(
        timestamp=timestamp,
        direction=direction,
        socket=socket,
        payload_bytes=payload_bytes,
        payload_format=payload_format,
        entropy=entropy,
        zero_ratio=zero_ratio,
        high_bit_ratio=high_bit_ratio,
        unique_byte_bucket=unique_byte_bucket,
    )


def _read_new_network_events(path: Path, offset: int) -> tuple[tuple[SystemProbeEvent, ...], int]:
    if not path.exists():
        return (), 0
    size = path.stat().st_size
    if size < offset:
        offset = 0

    events: list[SystemProbeEvent] = []
    with path.open("rb") as handle:
        handle.seek(offset)
        for raw_line in handle:
            try:
                line = raw_line.decode("utf-8")
                document = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            event = _network_event(document)
            if event is not None:
                events.append(event)
        new_offset = handle.tell()
    return tuple(events), new_offset


def _eof(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _record_boundary_and_instant_forecast(
    ledger_path: Path,
    collector_session_id: str,
    previous_round: SnapshotRound,
    rounds: tuple[SnapshotRound, ...],
) -> None:
    """Publish the earliest causal forecast immediately after a completed result."""
    boundary = {
        "event": "prospective_boundary_ready",
        "observed_at": utc_text(),
        "collector_session_id": collector_session_id,
        "previous_round_id": previous_round.round_id,
        "previous_round_timestamp": previous_round.timestamp,
    }
    append_snapshot_event(ledger_path, boundary)
    _print_event(boundary)

    previous_index = next(
        (
            index for index, round_ in enumerate(rounds)
            if round_.round_id == previous_round.round_id
        ),
        None,
    )
    if previous_index is None:
        raise RuntimeError("Previous round is unavailable for immediate forecast.")

    instant = instant_prior_event(
        previous_round_id=previous_round.round_id,
        previous_round_timestamp=previous_round.timestamp,
        history_multipliers=[
            round_.multiplier
            for round_ in rounds[: previous_index + 1]
        ],
        observed_at=utc_text(),
    )
    append_snapshot_event(ledger_path, instant)
    _print_event(instant)


def _print_event(event: dict[str, object]) -> None:
    kind = event.get("event")
    if kind == "prospective_capture_armed":
        print(
            "\nPROSPECTIVE CAPTURE ARMED\n"
            f"Collector session: {event['collector_session_id']}\n"
            f"History count: {event['history_count']}\n"
            "Waiting for one fresh completed round before accepting a trigger."
        )
    elif kind == "prospective_boundary_ready":
        print(
            "\nPRE-ROUND BOUNDARY READY\n"
            f"Previous round: {event['previous_round_id']}\n"
            f"Completed at: {event['previous_round_timestamp']}\n"
            "Immediate historical forecast follows; trigger update is optional."
        )
    elif kind == "pre_round_prior_forecast":
        probabilities = event.get("probabilities")
        probability_lines = ""
        if isinstance(probabilities, dict):
            for threshold in ("1.5", "2", "5"):
                value = probabilities.get(threshold)
                if isinstance(value, str):
                    probability_lines += (
                        f"\nP(>= {threshold}x): {float(value) * 100:.1f}%"
                    )
        print(
            "\nINSTANT PRE-ROUND FORECAST\n"
            f"Issued at: {event['observed_at']}\n"
            f"History through: {event['previous_round_timestamp']}"
            + probability_lines
            + "\nSource: historical prior (available immediately)"
        )
    elif kind == "pre_round_snapshot_locked":
        forecast = event.get("forecast")
        forecast_text = ""
        if isinstance(forecast, dict):
            probabilities = forecast.get("probabilities")
            historical = forecast.get("historical_probabilities")
            gates = forecast.get("gates")
            probability_lines = ""
            if isinstance(probabilities, dict):
                for threshold in ("1.5", "2", "5"):
                    value = probabilities.get(threshold)
                    if isinstance(value, str):
                        probability_lines += (
                            f"\nP(>= {threshold}x): {float(value) * 100:.1f}%"
                        )
            gate_lines = ""
            if isinstance(gates, dict):
                for threshold in ("1.5", "2", "5"):
                    gate = gates.get(threshold)
                    if isinstance(gate, dict):
                        source = gate.get("source")
                        samples = gate.get("validation_samples")
                        skill = gate.get("candidate_skill_vs_prior_pct")
                        gate_lines += (
                            f"\n{threshold}x source: {source}; "
                            f"validation={samples}; skill_vs_prior={skill}"
                        )
            forecast_text = (
                "\n\nTRIGGER UPDATE\n"
                + probability_lines
                + f"\nHistorical prior: {historical}\n"
                f"Calibration support: {forecast.get('calibration_support')}\n"
                f"Prospective samples available: "
                f"{forecast.get('scored_prospective_samples')}\n"
                f"Calibration cohort: {forecast.get('cohort_kind')} "
                f"({forecast.get('cohort_samples')} samples)"
                + gate_lines
            )
        print(
            "\nPRE-ROUND SNAPSHOT LOCKED\n"
            f"Snapshot ID: {event['snapshot_id']}\n"
            f"Locked at: {event['locked_at']}\n"
            f"Trigger: {event['trigger_signature']}\n"
            f"Age after prior result: {event['trigger_age_after_previous_seconds']}s"
            + forecast_text
        )
    elif kind == "pre_round_snapshot_scored":
        score = event.get("forecast_score")
        score_text = ""
        if isinstance(score, dict):
            score_text = (
                "\nProbability scores: "
                f"{score.get('threshold_scores')}"
            )
        print(
            "\nPRE-ROUND SNAPSHOT SCORED\n"
            f"Snapshot ID: {event['snapshot_id']}\n"
            f"Actual: {event['actual_multiplier']}x\n"
            f"Completed at: {event['actual_timestamp']}"
            + score_text
        )
    elif kind == "pre_round_snapshot_missed":
        print(
            "\nPRE-ROUND SNAPSHOT MISSED\n"
            f"Previous round: {event['previous_round_id']}\n"
            f"Actual: {event['actual_multiplier']}x\n"
            "No frozen trigger fired before the next completed observation."
        )
    elif kind == "pre_round_snapshot_invalidated":
        print(
            "\nPRE-ROUND SNAPSHOT INVALIDATED\n"
            f"Snapshot ID: {event['snapshot_id']}\n"
            f"Reason: {event['reason']}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Prospectively lock sanitized pre-round transport sequences before "
            "the next completed DOM result is known."
        )
    )
    parser.add_argument("--network-input", default="aviator-network-probe.jsonl")
    parser.add_argument("--dom-input", default="aviator-dom-multipliers.jsonl")
    parser.add_argument("--heartbeat", default="aviator-dom-heartbeat.json")
    parser.add_argument("--ledger", default=DEFAULT_SNAPSHOT_LEDGER_FILENAME)
    parser.add_argument("--poll-seconds", type=float, default=0.25)
    parser.add_argument("--max-heartbeat-age", type=float, default=5.0)
    parser.add_argument("--minimum-trigger-age", type=float, default=1.0)
    parser.add_argument("--max-sequence-events", type=int, default=32)
    args = parser.parse_args(argv)

    for value, suffix, name in (
        (args.network_input, ".jsonl", "--network-input"),
        (args.dom_input, ".jsonl", "--dom-input"),
        (args.ledger, ".jsonl", "--ledger"),
        (args.heartbeat, ".json", "--heartbeat"),
    ):
        if "/" in value or "\\" in value or not value.endswith(suffix):
            parser.error(f"{name} must be a plain {suffix} filename.")
    if not 0.1 <= args.poll_seconds <= 5.0:
        parser.error("--poll-seconds must be between 0.1 and 5 seconds.")
    if not 2.0 <= args.max_heartbeat_age <= 60.0:
        parser.error("--max-heartbeat-age must be between 2 and 60 seconds.")
    if not 0.0 <= args.minimum_trigger_age <= 30.0:
        parser.error("--minimum-trigger-age must be between 0 and 30 seconds.")
    if not 4 <= args.max_sequence_events <= 128:
        parser.error("--max-sequence-events must be between 4 and 128.")

    try:
        settings = load_settings()
        network_path = settings.raw_data_dir / args.network_input
        dom_path = settings.raw_data_dir / args.dom_input
        heartbeat_path = settings.raw_data_dir / args.heartbeat
        ledger_path = settings.processed_data_dir / args.ledger

        historical_dom_paths = discover_dom_probe_paths(settings.raw_data_dir)
        historical_network_paths = discover_network_probe_paths(settings.raw_data_dir)
        completed = load_completed_round_boundaries(historical_dom_paths)
        live = load_live_multiplier_observations(historical_dom_paths)
        starts = infer_round_starts(completed, live)
        historical_events = load_system_probe_events(historical_network_paths)
        discovery = discover_prestart_trigger_signatures(
            starts,
            historical_events,
            discovery_fraction=0.80,
            lookback_seconds=2.5,
            minimum_lead_seconds=0.20,
            minimum_hit_starts=4,
            minimum_hit_rate=0.12,
            minimum_enrichment=1.5,
            max_signatures=16,
        )
        frozen = discovery.signatures
        if not frozen:
            print(
                "Prospective capture cannot start: no frozen historical trigger signatures.",
                file=sys.stderr,
            )
            return 1
        frozen_scores = {item.signature: item.score for item in frozen}

        print(
            "Aviator Intelligence Engine\n"
            f"Phase: {DEVELOPMENT_PHASE}\n"
            "Prospective PRE-ROUND capture: RUNNING\n"
            f"Frozen trigger signatures: {len(frozen)}\n"
            f"Snapshot ledger: {ledger_path}\n"
            "Data policy: trigger-time metadata only; outcome appended later."
        )

        active_session: str | None = None
        armed_history_count: int | None = None
        previous_round: SnapshotRound | None = None
        interval_events: list[SystemProbeEvent] = []
        network_offset = _eof(network_path)

        while True:
            session = _heartbeat_session(heartbeat_path, args.max_heartbeat_age)
            if session is None:
                ledger = load_snapshot_ledger(ledger_path)
                outstanding = outstanding_snapshot(ledger)
                if outstanding is not None:
                    invalid = invalidate_snapshot(
                        outstanding, "collector_heartbeat_missing_or_stale"
                    )
                    append_snapshot_event(ledger_path, invalid)
                    _print_event(invalid)
                active_session = None
                armed_history_count = None
                previous_round = None
                interval_events.clear()
                network_offset = _eof(network_path)
                time.sleep(args.poll_seconds)
                continue

            if session != active_session:
                ledger = load_snapshot_ledger(ledger_path)
                outstanding = outstanding_snapshot(ledger)
                if outstanding is not None:
                    invalid = invalidate_snapshot(
                        outstanding, "collector_session_changed"
                    )
                    append_snapshot_event(ledger_path, invalid)
                    _print_event(invalid)

                ingest_dom_rounds(dom_path, settings)
                rounds = _load_rounds(settings)
                arm = make_arm_event(session, rounds)
                append_snapshot_event(ledger_path, arm)
                _print_event(arm)

                active_session = session
                armed_history_count = len(rounds)
                previous_round = None
                interval_events.clear()
                network_offset = _eof(network_path)
                time.sleep(args.poll_seconds)
                continue

            ingest_dom_rounds(dom_path, settings)
            rounds = _load_rounds(settings)
            ledger = load_snapshot_ledger(ledger_path)
            outstanding = outstanding_snapshot(ledger)

            if outstanding is not None:
                previous_id = outstanding.get("previous_round_id")
                previous_index = next(
                    (
                        index for index, round_ in enumerate(rounds)
                        if round_.round_id == previous_id
                    ),
                    None,
                )
                if previous_index is not None and previous_index + 1 < len(rounds):
                    actual = rounds[previous_index + 1]
                    score = score_pre_round_snapshot(outstanding, actual)
                    append_snapshot_event(ledger_path, score)
                    _print_event(score)

                    extra = len(rounds) - (previous_index + 2)
                    if extra > 0:
                        arm = make_arm_event(session, rounds)
                        append_snapshot_event(ledger_path, arm)
                        _print_event(arm)
                        armed_history_count = len(rounds)
                        previous_round = None
                    else:
                        previous_round = actual
                        armed_history_count = None
                        _record_boundary_and_instant_forecast(
                            ledger_path,
                            session,
                            actual,
                            rounds,
                        )
                    interval_events.clear()
                    network_offset = _eof(network_path)
                    time.sleep(args.poll_seconds)
                    continue

            if outstanding is None:
                if previous_round is None:
                    if armed_history_count is None:
                        armed_history_count = len(rounds)
                    if len(rounds) > armed_history_count:
                        fresh_count = len(rounds) - armed_history_count
                        if fresh_count == 1:
                            previous_round = rounds[-1]
                            armed_history_count = None
                            _record_boundary_and_instant_forecast(
                                ledger_path,
                                session,
                                previous_round,
                                rounds,
                            )
                            interval_events.clear()
                            network_offset = _eof(network_path)
                        else:
                            arm = make_arm_event(session, rounds)
                            append_snapshot_event(ledger_path, arm)
                            _print_event(arm)
                            armed_history_count = len(rounds)
                            interval_events.clear()
                            network_offset = _eof(network_path)
                else:
                    previous_index = next(
                        (
                            index for index, round_ in enumerate(rounds)
                            if round_.round_id == previous_round.round_id
                        ),
                        None,
                    )
                    if previous_index is not None and previous_index + 1 < len(rounds):
                        actual = rounds[previous_index + 1]
                        missed = {
                            "event": "pre_round_snapshot_missed",
                            "observed_at": utc_text(),
                            "collector_session_id": session,
                            "previous_round_id": previous_round.round_id,
                            "previous_round_timestamp": previous_round.timestamp,
                            "actual_round_id": actual.round_id,
                            "actual_timestamp": actual.timestamp,
                            "actual_multiplier": actual.multiplier,
                            "reason": "no_frozen_trigger_before_next_completed_observation",
                        }
                        append_snapshot_event(ledger_path, missed)
                        _print_event(missed)
                        previous_round = actual
                        _record_boundary_and_instant_forecast(
                            ledger_path,
                            session,
                            previous_round,
                            rounds,
                        )
                        interval_events.clear()
                        network_offset = _eof(network_path)

            new_events, network_offset = _read_new_network_events(
                network_path, network_offset
            )
            if previous_round is not None and outstanding_snapshot(
                load_snapshot_ledger(ledger_path)
            ) is None:
                previous_ts = _parse_timestamp(previous_round.timestamp)
                if previous_ts is not None:
                    for event in new_events:
                        if event.timestamp <= previous_ts:
                            continue
                        interval_events.append(event)
                        age = event.timestamp - previous_ts
                        token = event_signature(event)
                        if (
                            age >= args.minimum_trigger_age
                            and token in frozen_scores
                        ):
                            snapshot = build_pre_round_snapshot(
                                collector_session_id=session,
                                previous_round=previous_round,
                                trigger_event=event,
                                interval_events=interval_events,
                                trigger_signatures=frozen,
                                max_sequence_events=args.max_sequence_events,
                            )
                            previous_index = next(
                                (
                                    index for index, round_ in enumerate(rounds)
                                    if round_.round_id == previous_round.round_id
                                ),
                                None,
                            )
                            if previous_index is None:
                                raise RuntimeError(
                                    "Previous round disappeared before forecast lock."
                                )
                            forecast = forecast_snapshot_probabilities(
                                [
                                    round_.multiplier
                                    for round_ in rounds[: previous_index + 1]
                                ],
                                load_snapshot_ledger(ledger_path),
                                snapshot,
                            )
                            snapshot = attach_forecast(snapshot, forecast)
                            append_snapshot_event(ledger_path, snapshot)
                            _print_event(snapshot)
                            break

            time.sleep(args.poll_seconds)

    except KeyboardInterrupt:
        print("\nProspective PRE-ROUND capture: STOPPED")
        return 0
    except (OSError, ValueError, sqlite3.Error, RuntimeError):
        print(
            "Prospective capture failed. Check collector heartbeat, probe files, database, and ledger.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
