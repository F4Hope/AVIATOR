"""Phase 2 tests use synthetic TEST_FIXTURE records in temporary databases only."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
import sqlite3
from threading import Barrier
from typing import Iterator

import pytest

from config.settings import Settings
from database.database import connect_database
import database.migrations as migrations
from database.migrations import SCHEMA_VERSION, SchemaVersionError, initialize_schema
from database.models import RoundRecord, timestamp_text
from database.repository import DuplicateRoundError, RoundRepository
import main as application


def fixture_record(**changes: object) -> RoundRecord:
    """Construct clearly labeled synthetic unit-test input, never a live result."""
    values: dict[str, object] = {
        "source": "TEST_FIXTURE", "round_id": "fixture-1",
        "timestamp": datetime(2025, 1, 1, 12, 0, 10, tzinfo=UTC),
        "collection_timestamp": datetime(2025, 1, 1, 12, 0, 11, tzinfo=UTC),
        "multiplier": "1.25",
    }
    values.update(changes)
    return RoundRecord(**values)


@pytest.fixture
def repository(settings: Settings) -> Iterator[RoundRepository]:
    with closing(connect_database(settings)) as connection:
        initialize_schema(connection)
        yield RoundRepository(connection)


def test_migration_is_idempotent_and_empty(settings: Settings) -> None:
    with closing(connect_database(settings)) as connection:
        assert initialize_schema(connection) == SCHEMA_VERSION
        assert initialize_schema(connection) == SCHEMA_VERSION
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM rounds").fetchone()[0] == 0
        assert not connection.in_transaction


def test_failed_migration_rolls_back(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(migrations, "MIGRATION_STATEMENTS", (
        *migrations.MIGRATION_STATEMENTS, "THIS IS INVALID SQL",
    ))
    with closing(connect_database(settings)) as connection:
        with pytest.raises(sqlite3.OperationalError):
            initialize_schema(connection)
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
        assert not connection.in_transaction


def test_future_schema_refused_without_changes(settings: Settings) -> None:
    with closing(connect_database(settings)) as connection:
        connection.execute("PRAGMA user_version = 99")
        with pytest.raises(SchemaVersionError):
            initialize_schema(connection)
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 99


def test_unmanaged_database_preserved(settings: Settings) -> None:
    with closing(connect_database(settings)) as connection:
        connection.execute("CREATE TABLE existing_data (value TEXT)")
        connection.execute("INSERT INTO existing_data VALUES ('existing')")
        with pytest.raises(SchemaVersionError):
            initialize_schema(connection)
        assert connection.execute("SELECT value FROM existing_data").fetchone()[0] == "existing"


def test_missing_schema_object_refused(repository: RoundRepository) -> None:
    repository.connection.execute("DROP TRIGGER rounds_no_update")
    with pytest.raises(SchemaVersionError):
        initialize_schema(repository.connection)


def test_timezone_normalization() -> None:
    record = fixture_record(timestamp=datetime(2025, 1, 1, 13, 0, 10, tzinfo=timezone(timedelta(hours=1))))
    assert timestamp_text(record.timestamp) == "2025-01-01T12:00:10.000000Z"


@pytest.mark.parametrize("field", ["timestamp", "collection_timestamp", "started_at", "pre_round_observed_at"])
def test_naive_datetimes_rejected(field: str) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        fixture_record(**{field: datetime(2025, 1, 1)})


@pytest.mark.parametrize("value", [0, -1, "NaN", "sNaN", "Infinity", float("inf"), True, "invalid", "1e999", "1.000e-999"])
def test_invalid_multiplier_rejected(value: object) -> None:
    with pytest.raises(ValueError):
        fixture_record(multiplier=value)


@pytest.mark.parametrize("field,value", [
    ("source", ""), ("round_id", " "), ("source", "source\nname"), ("round_id", 123),
])
def test_invalid_identifiers_rejected(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        fixture_record(**{field: value})


@pytest.mark.parametrize("payload", [
    [], {"value": float("nan")}, {"value": Decimal("1.2")}, {1: "value"}, {"value": object()},
])
def test_invalid_json_rejected(payload: object) -> None:
    with pytest.raises(ValueError):
        fixture_record(raw_data=payload)


@pytest.mark.parametrize("key", ["password", "Authorization", "cookies", "access_token", "api-key", "sessionToken"])
def test_authentication_fields_rejected(key: str) -> None:
    with pytest.raises(ValueError, match="Authentication"):
        fixture_record(raw_data={"nested": [{key: "synthetic-test-marker"}]})


def test_payload_is_deeply_immutable() -> None:
    original = {"nested": [{"value": "original"}]}
    record = fixture_record(raw_data=original)
    original["nested"][0]["value"] = "changed"
    assert record.raw_data["nested"][0]["value"] == "original"
    with pytest.raises(TypeError):
        record.raw_data["nested"][0]["value"] = "changed"
    with pytest.raises(FrozenInstanceError):
        record.round_id = "changed"


def test_cyclic_payload_rejected() -> None:
    payload: dict[str, object] = {}
    payload["self"] = payload
    with pytest.raises(ValueError, match="cyclic"):
        fixture_record(raw_data=payload)


def test_collection_before_result_rejected() -> None:
    with pytest.raises(ValueError, match="collection_timestamp"):
        fixture_record(collection_timestamp=datetime(2025, 1, 1, tzinfo=UTC))


def test_round_start_after_result_rejected() -> None:
    with pytest.raises(ValueError, match="started_at"):
        fixture_record(started_at=datetime(2025, 1, 2, tzinfo=UTC))


def test_pre_data_requires_observation_time() -> None:
    with pytest.raises(ValueError, match="observation time"):
        fixture_record(pre_round_data={"commitment": "test-only"})


def test_pre_observation_requires_known_start() -> None:
    with pytest.raises(ValueError, match="known round start"):
        fixture_record(pre_round_observed_at=datetime(2025, 1, 1, 12, 0, tzinfo=UTC))


@pytest.mark.parametrize("seconds", [0, 1])
def test_pre_observation_at_or_after_start_rejected(seconds: int) -> None:
    start = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="strictly precede"):
        fixture_record(started_at=start, pre_round_observed_at=start + timedelta(seconds=seconds))


def test_pre_and_post_payloads_remain_separate(repository: RoundRepository) -> None:
    start = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
    record = fixture_record(
        started_at=start, pre_round_observed_at=start - timedelta(seconds=1),
        pre_round_data={"commitment": "test-only"}, post_round_data={"revealed_seed": "test-only"},
    )
    assert repository.insert(record)
    stored = repository.get(record.source, record.round_id)
    assert stored.record.pre_round_data == {"commitment": "test-only"}
    assert stored.record.post_round_data == {"revealed_seed": "test-only"}
    assert "revealed_seed" not in stored.record.pre_round_data


def test_exact_decimal_and_json_round_trip(repository: RoundRepository) -> None:
    record = fixture_record(
        multiplier="12.12345678901234567890123456789", raw_data={"text": "é", "list": [None, True, 2]},
    )
    assert repository.insert(record) is True
    stored = repository.get(record.source, record.round_id)
    assert stored.record == record
    assert stored.record.multiplier == Decimal("12.12345678901234567890123456789")
    assert stored.ingested_at.tzinfo == UTC


def test_missing_round_and_counts(repository: RoundRepository) -> None:
    assert repository.get("TEST_FIXTURE", "absent") is None
    assert repository.count() == 0
    repository.insert(fixture_record())
    assert repository.count() == repository.count("TEST_FIXTURE") == 1
    assert repository.count("OTHER_FIXTURE") == 0


def test_exact_duplicate_is_idempotent(repository: RoundRepository) -> None:
    record = fixture_record(raw_data={"b": 2, "a": 1})
    assert repository.insert(record) is True
    before = repository.get(record.source, record.round_id)
    assert repository.insert(replace(record, raw_data={"a": 1, "b": 2}, multiplier="1.2500")) is False
    assert repository.get(record.source, record.round_id) == before
    assert repository.count() == 1


@pytest.mark.parametrize("changes", [
    {"multiplier": "2.0"}, {"raw_data": {"extra": "value"}},
    {"collection_timestamp": datetime(2025, 1, 1, 12, 0, 12, tzinfo=UTC)},
])
def test_conflicting_duplicates_preserve_original(repository: RoundRepository, changes: dict) -> None:
    record = fixture_record()
    repository.insert(record)
    before = repository.get(record.source, record.round_id)
    with pytest.raises(DuplicateRoundError):
        repository.insert(replace(record, **changes))
    assert repository.get(record.source, record.round_id) == before
    assert not repository.connection.in_transaction


def test_identical_ids_from_different_sources(repository: RoundRepository) -> None:
    record = fixture_record()
    repository.insert(record)
    repository.insert(replace(record, source="OTHER_FIXTURE"))
    assert repository.count() == 2


def test_parameterized_queries_preserve_sql_like_identifiers(repository: RoundRepository) -> None:
    record = fixture_record(round_id="fixture'; DROP TABLE rounds; --", source="source'quoted")
    repository.insert(record)
    assert repository.get(record.source, record.round_id).record == record
    assert repository.count(record.source) == 1
    assert repository.list_rounds(source=record.source)[0].record == record


def test_chronology_filtering_and_pagination(repository: RoundRepository) -> None:
    for index in [2, 0, 1]:
        record = fixture_record(
            round_id=f"fixture-{index}", timestamp=datetime(2025, 1, 1, 12, 0, index, tzinfo=UTC),
            collection_timestamp=datetime(2025, 1, 1, 12, 0, 12, tzinfo=UTC),
        )
        repository.insert(record)
    assert [row.record.round_id for row in repository.list_rounds()] == ["fixture-0", "fixture-1", "fixture-2"]
    assert [row.record.round_id for row in repository.list_rounds(limit=1, offset=1)] == ["fixture-1"]
    rows = repository.list_rounds(
        source="TEST_FIXTURE", start=datetime(2025, 1, 1, 12, 0, 1, tzinfo=UTC),
        end=datetime(2025, 1, 1, 12, 0, 2, tzinfo=UTC),
    )
    assert [row.record.round_id for row in rows] == ["fixture-1"]


def test_chronological_ties_are_deterministic(repository: RoundRepository) -> None:
    for round_id in ["c", "a", "b"]:
        repository.insert(fixture_record(round_id=round_id))
    assert [row.record.round_id for row in repository.list_rounds()] == ["a", "b", "c"]


@pytest.mark.parametrize("arguments", [
    {"limit": 0}, {"limit": True}, {"limit": 10001}, {"offset": -1}, {"offset": False},
    {"start": datetime(2025, 1, 1)},
    {"start": datetime(2025, 1, 2, tzinfo=UTC), "end": datetime(2025, 1, 1, tzinfo=UTC)},
])
def test_invalid_query_arguments_rejected(repository: RoundRepository, arguments: dict) -> None:
    with pytest.raises(ValueError):
        repository.list_rounds(**arguments)


@pytest.mark.parametrize("statement", ["UPDATE rounds SET multiplier='2'", "DELETE FROM rounds"])
def test_stored_rounds_cannot_be_overwritten(repository: RoundRepository, statement: str) -> None:
    repository.insert(fixture_record())
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        repository.connection.execute(statement)
    assert repository.count() == 1


def test_repository_preserves_callers_transaction(repository: RoundRepository) -> None:
    repository.connection.execute("BEGIN")
    with pytest.raises(RuntimeError, match="idle"):
        repository.insert(fixture_record())
    with pytest.raises(RuntimeError, match="idle"):
        initialize_schema(repository.connection)
    assert repository.connection.in_transaction
    repository.connection.rollback()


def test_persistence_and_restart_preserve_records(settings: Settings) -> None:
    record = fixture_record()
    with closing(connect_database(settings)) as connection:
        initialize_schema(connection)
        RoundRepository(connection).insert(record)
    state = application.initialize_application(settings)
    assert state.rounds_stored == 1
    with closing(connect_database(settings)) as connection:
        assert RoundRepository(connection).get(record.source, record.round_id).record == record


def test_concurrent_duplicate_writes(settings: Settings) -> None:
    with closing(connect_database(settings)) as connection:
        initialize_schema(connection)
    barrier = Barrier(2)
    record = fixture_record()

    def insert_from_separate_connection() -> bool:
        with closing(connect_database(settings)) as connection:
            barrier.wait(timeout=10)
            return RoundRepository(connection).insert(record)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(insert_from_separate_connection) for _ in range(2)]
        assert sorted(future.result(timeout=15) for future in futures) == [False, True]


def test_startup_refuses_future_schema(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    with closing(connect_database(settings)) as connection:
        connection.execute("PRAGMA user_version = 99")
    monkeypatch.setattr(application, "load_settings", lambda: settings)
    assert application.main() == 1
