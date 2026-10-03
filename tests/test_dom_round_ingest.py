"""Tests for local DOM-observed completed-round ingestion."""

from contextlib import closing
import json
from pathlib import Path

from collectors.dom_round_ingest import (
    DOM_OBSERVATION_SOURCE,
    ingest_dom_rounds,
    load_dom_round_records,
)
from config.settings import Settings
from database.database import connect_database
from database.migrations import initialize_schema
from database.repository import RoundRepository


def _candidate(value: str, x: int) -> dict[str, object]:
    return {
        "multiplier": value,
        "x_bucket": x,
        "y_bucket": 50,
        "width_bucket": 40,
        "height_bucket": 20,
    }


def _write_dom_capture(path: Path) -> None:
    base = ("1.07", "2.41", "1.64", "17.42", "1.33", "1.17", "4.27")
    one = ("44.88",) + base
    two = ("1.12",) + one

    rows = []
    for stamp, values in (
        ("2026-10-03T12:42:30.000000Z", base),
        ("2026-10-03T12:42:34.917025Z", one),
        ("2026-10-03T12:45:07.885930Z", two),
    ):
        rows.append({
            "kind": "dom_multiplier_snapshot",
            "observed_at": stamp,
            "candidates": [
                _candidate(value, 10 + index * 50)
                for index, value in enumerate(values)
            ],
        })
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_dom_records_use_local_observation_identity(tmp_path: Path) -> None:
    path = tmp_path / "dom.jsonl"
    _write_dom_capture(path)

    records = load_dom_round_records(path)

    assert [record.multiplier_text for record in records] == ["44.88", "1.12"]
    assert all(record.source == DOM_OBSERVATION_SOURCE for record in records)
    assert all(record.round_id.startswith("aie-dom-") for record in records)
    assert len({record.round_id for record in records}) == 2
    assert records[0].post_round_data["observation_method"] == "dom_history_shift"
    assert records[0].post_round_data["provider_round_id_available"] is False
    assert records[0].post_round_data["timestamp_semantics"] == "first_observed_history_insertion"


def test_dom_ingestion_is_idempotent(settings: Settings) -> None:
    settings.raw_data_dir.mkdir(parents=True, exist_ok=True)
    path = settings.raw_data_dir / "aviator-dom-multipliers.jsonl"
    _write_dom_capture(path)

    first = ingest_dom_rounds(path, settings)
    second = ingest_dom_rounds(path, settings)

    assert first.extracted == 2
    assert first.inserted == 2
    assert first.duplicates == 0
    assert second.extracted == 2
    assert second.inserted == 0
    assert second.duplicates == 2

    with closing(connect_database(settings)) as connection:
        initialize_schema(connection)
        repository = RoundRepository(connection)
        assert repository.count(DOM_OBSERVATION_SOURCE) == 2


def test_dom_ingestion_dry_run_does_not_create_database(settings: Settings) -> None:
    settings.raw_data_dir.mkdir(parents=True, exist_ok=True)
    path = settings.raw_data_dir / "aviator-dom-multipliers.jsonl"
    _write_dom_capture(path)

    report = ingest_dom_rounds(path, settings, dry_run=True)

    assert report.extracted == 2
    assert report.inserted is None
    assert report.duplicates is None
    assert not settings.database_path.exists()
