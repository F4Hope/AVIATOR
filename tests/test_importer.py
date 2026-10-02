"""Phase 3 tests use synthetic records and temporary files/databases only."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from threading import Barrier

import pytest

import collectors.json_importer as importer
from collectors.json_importer import ImportValidationError, import_json_file, load_json_rounds
from config.settings import PROJECT_ROOT, Settings
from database.database import connect_database
from database.migrations import initialize_schema
from database.repository import DuplicateRoundError, RoundRepository
import import_rounds as command


def round_document(**changes: object) -> dict[str, object]:
    """Clearly labeled synthetic completed-round input."""
    values: dict[str, object] = {
        "source": "TEST_FIXTURE", "round_id": "fixture-1", "multiplier": "1.25",
        "timestamp": "2025-01-01T12:00:10Z", "collection_timestamp": "2025-01-01T12:00:11Z",
    }
    values.update(changes)
    return values


def write_import(tmp_path: Path, rounds: list | None = None) -> Path:
    path = tmp_path / "rounds.json"
    path.write_text(json.dumps({"format_version": 1, "rounds": rounds if rounds is not None else [round_document()]}), encoding="utf-8")
    return path


def round_count(settings: Settings) -> int:
    with closing(connect_database(settings)) as connection:
        return RoundRepository(connection).count()


def test_load_normalizes_time_and_preserves_precise_multiplier(tmp_path: Path) -> None:
    path = write_import(tmp_path, [round_document(
        timestamp="2025-01-01T13:00:10+01:00", multiplier="12.12345678901234567890123456789",
        raw_data={"nested": [True, None, 1.5, "é"]},
    )])
    original = path.read_bytes()
    result = load_json_rounds(path)
    assert result.records[0].timestamp == datetime(2025, 1, 1, 12, 0, 10, tzinfo=UTC)
    assert result.records[0].multiplier_text == "12.12345678901234567890123456789"
    assert result.sha256 == hashlib.sha256(original).hexdigest()
    assert path.read_bytes() == original


def test_utf8_bom_and_uppercase_extension_supported(tmp_path: Path) -> None:
    path = tmp_path / "rounds.JSON"
    content = json.dumps({"format_version": 1, "rounds": [round_document()]})
    path.write_text(content, encoding="utf-8-sig")
    assert len(load_json_rounds(path).records) == 1


@pytest.mark.parametrize("content,code", [
    (b"", "INVALID_JSON"), (b"{broken}", "INVALID_JSON"), (b"\xff", "INVALID_UTF8"),
    (b'{"format_version":1,"format_version":1,"rounds":[]}', "DUPLICATE_JSON_KEY"),
    (b'{"format_version":1,"rounds":[{"raw_data":{"value":1,"value":2}}]}', "DUPLICATE_JSON_KEY"),
    (b'{"format_version":1,"rounds":[NaN]}', "NONFINITE_JSON_NUMBER"),
    (b'{"format_version":1,"rounds":[Infinity]}', "NONFINITE_JSON_NUMBER"),
    (b'{"format_version":1,"rounds":[-Infinity]}', "NONFINITE_JSON_NUMBER"),
])
def test_invalid_bytes_rejected(tmp_path: Path, content: bytes, code: str) -> None:
    path = tmp_path / "rounds.json"
    path.write_bytes(content)
    with pytest.raises(ImportValidationError) as error:
        load_json_rounds(path)
    assert error.value.code == code


@pytest.mark.parametrize("document,code", [
    ([], "INVALID_DOCUMENT_FIELDS"), ({}, "INVALID_DOCUMENT_FIELDS"),
    ({"format_version": 1, "rounds": [], "extra": "private-marker"}, "INVALID_DOCUMENT_FIELDS"),
    ({"format_version": 2, "rounds": []}, "UNSUPPORTED_FORMAT_VERSION"),
    ({"format_version": True, "rounds": []}, "UNSUPPORTED_FORMAT_VERSION"),
    ({"format_version": 1.0, "rounds": []}, "UNSUPPORTED_FORMAT_VERSION"),
    ({"format_version": 1, "rounds": {}}, "ROUNDS_MUST_BE_ARRAY"),
])
def test_document_contract_rejected(tmp_path: Path, document: object, code: str) -> None:
    path = tmp_path / "rounds.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ImportValidationError) as error:
        load_json_rounds(path)
    assert error.value.code == code


@pytest.mark.parametrize("changes", [
    {"timestamp": "2025-01-01T12:00:10"}, {"timestamp": None}, {"timestamp": 123},
    {"collection_timestamp": "2025-01-01T12:00:09Z"},
    {"started_at": "2025-01-02T00:00:00Z"},
    {"pre_round_data": {"observed": "synthetic"}},
    {"pre_round_observed_at": "2025-01-01T12:00:00Z"},
    {"source": ""}, {"round_id": "\ud800"}, {"multiplier": "NaN"},
    {"raw_data": None}, {"raw_data": {"value": float("inf")}},
])
def test_invalid_rounds_report_index(tmp_path: Path, changes: dict) -> None:
    path = write_import(tmp_path, [round_document(), round_document(**changes)])
    with pytest.raises(ImportValidationError) as error:
        load_json_rounds(path)
    assert error.value.row_number in (None, 2)  # JSON constants reject the document before rows.
    assert error.value.code in ("INVALID_ROUND", "NONFINITE_JSON_NUMBER")


@pytest.mark.parametrize("value", [1.25, 1, True, None])
def test_import_requires_multiplier_text(tmp_path: Path, value: object) -> None:
    with pytest.raises(ImportValidationError, match="MULTIPLIER_MUST_BE_STRING"):
        load_json_rounds(write_import(tmp_path, [round_document(multiplier=value)]))


@pytest.mark.parametrize("item,code", [
    ([], "ROUND_MUST_BE_OBJECT"), ({}, "MISSING_ROUND_FIELD"),
    (round_document(extra="private-marker"), "UNKNOWN_ROUND_FIELD"),
])
def test_round_field_contract(tmp_path: Path, item: object, code: str) -> None:
    with pytest.raises(ImportValidationError) as error:
        load_json_rounds(write_import(tmp_path, [item]))
    assert error.value.code == code
    assert error.value.row_number == 1


def test_file_size_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_import(tmp_path)
    monkeypatch.setattr(importer, "MAX_IMPORT_BYTES", 5)
    with pytest.raises(ImportValidationError, match="FILE_TOO_LARGE"):
        load_json_rounds(path)


def test_record_count_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_import(tmp_path, [round_document(), round_document(round_id="fixture-2")])
    monkeypatch.setattr(importer, "MAX_IMPORT_ROUNDS", 1)
    with pytest.raises(ImportValidationError, match="TOO_MANY_ROUNDS"):
        load_json_rounds(path)


def test_missing_file_or_directory_rejected(tmp_path: Path) -> None:
    for path in (tmp_path / "missing.json", tmp_path / "directory.json"):
        if path.name.startswith("directory"):
            path.mkdir()
        with pytest.raises(ImportValidationError, match="FILE_NOT_FOUND_OR_NOT_REGULAR"):
            load_json_rounds(path)


def test_unsupported_extension_rejected(tmp_path: Path) -> None:
    with pytest.raises(ImportValidationError, match="UNSUPPORTED_FILE_TYPE"):
        load_json_rounds(tmp_path / "rounds.csv")


def test_valid_pre_and_post_data_remain_separate(tmp_path: Path) -> None:
    record = load_json_rounds(write_import(tmp_path, [round_document(
        started_at="2025-01-01T12:00:00Z", pre_round_observed_at="2025-01-01T11:59:59Z",
        pre_round_data={"commitment": "test-only"}, post_round_data={"revealed_seed": "test-only"},
    )])).records[0]
    assert record.pre_round_data == {"commitment": "test-only"}
    assert record.post_round_data == {"revealed_seed": "test-only"}


def test_optional_null_times_accepted(tmp_path: Path) -> None:
    record = load_json_rounds(write_import(tmp_path, [round_document(started_at=None, pre_round_observed_at=None)])).records[0]
    assert record.started_at is record.pre_round_observed_at is None


def test_rejected_file_does_not_create_database(tmp_path: Path, settings: Settings) -> None:
    path = write_import(tmp_path, [round_document(), round_document(round_id="bad", multiplier="0.5")])
    with pytest.raises(ImportValidationError):
        import_json_file(path, settings)
    assert not settings.database_path.exists()


def test_dry_run_does_not_open_database(tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importer, "connect_database", lambda _: pytest.fail("Dry run opened a database"))
    report = import_json_file(write_import(tmp_path), settings, dry_run=True)
    assert report.dry_run and report.rows_read == 1
    assert report.inserted is report.duplicates is None
    assert not settings.data_dir.exists()


def test_empty_file_creates_no_rounds(tmp_path: Path, settings: Settings) -> None:
    report = import_json_file(write_import(tmp_path, []), settings)
    assert report.rows_read == report.inserted == report.duplicates == round_count(settings) == 0


def test_import_then_exact_replay(tmp_path: Path, settings: Settings) -> None:
    path = write_import(tmp_path, [round_document(), round_document(round_id="fixture-2")])
    first = import_json_file(path, settings)
    second = import_json_file(path, settings)
    assert (first.inserted, first.duplicates) == (2, 0)
    assert (second.inserted, second.duplicates) == (0, 2)
    assert first.sha256 == second.sha256
    assert round_count(settings) == 2


def test_exact_duplicates_within_file_counted(tmp_path: Path, settings: Settings) -> None:
    report = import_json_file(write_import(tmp_path, [round_document(), round_document()]), settings)
    assert (report.rows_read, report.inserted, report.duplicates) == (2, 1, 1)


def test_conflict_with_existing_record_rolls_back_entire_file(tmp_path: Path, settings: Settings) -> None:
    import_json_file(write_import(tmp_path), settings)
    path = write_import(tmp_path, [round_document(round_id="new-fixture"), round_document(multiplier="2.0")])
    with pytest.raises(DuplicateRoundError):
        import_json_file(path, settings)
    assert round_count(settings) == 1
    with closing(connect_database(settings)) as connection:
        repository = RoundRepository(connection)
        assert repository.get("TEST_FIXTURE", "new-fixture") is None
        assert repository.get("TEST_FIXTURE", "fixture-1").record.multiplier_text == "1.25"


def test_conflict_within_file_rolls_back_all_records(tmp_path: Path, settings: Settings) -> None:
    path = write_import(tmp_path, [round_document(), round_document(multiplier="2.0")])
    with pytest.raises(DuplicateRoundError):
        import_json_file(path, settings)
    assert round_count(settings) == 0


def test_failed_sql_statement_rolls_back_batch(tmp_path: Path, settings: Settings) -> None:
    records = load_json_rounds(write_import(tmp_path, [round_document(), round_document(round_id="fixture-2")])).records
    with closing(connect_database(settings)) as connection:
        initialize_schema(connection)
        connection.execute("""CREATE TRIGGER fail_second BEFORE INSERT ON rounds
            WHEN NEW.round_id = 'fixture-2' BEGIN SELECT RAISE(ABORT, 'test failure'); END""")
        with pytest.raises(sqlite3.IntegrityError):
            RoundRepository(connection).insert_many(records)
        assert RoundRepository(connection).count() == 0
        assert not connection.in_transaction


def test_invalid_batch_item_does_not_write(tmp_path: Path, settings: Settings) -> None:
    record = load_json_rounds(write_import(tmp_path)).records[0]
    with closing(connect_database(settings)) as connection:
        initialize_schema(connection)
        with pytest.raises(TypeError):
            RoundRepository(connection).insert_many([record, object()])
        assert RoundRepository(connection).count() == 0
        assert not connection.in_transaction


def test_failed_iterator_does_not_write(tmp_path: Path, settings: Settings) -> None:
    record = load_json_rounds(write_import(tmp_path)).records[0]

    def failing_records():
        yield record
        raise RuntimeError("test iterator failure")

    with closing(connect_database(settings)) as connection:
        initialize_schema(connection)
        with pytest.raises(RuntimeError, match="iterator"):
            RoundRepository(connection).insert_many(failing_records())
        assert RoundRepository(connection).count() == 0
        assert not connection.in_transaction


def test_concurrent_replayed_batches(tmp_path: Path, settings: Settings) -> None:
    path = write_import(tmp_path, [round_document(), round_document(round_id="fixture-2")])
    barrier = Barrier(2)

    def import_at_same_time():
        barrier.wait(timeout=10)
        return import_json_file(path, settings)

    with ThreadPoolExecutor(max_workers=2) as executor:
        reports = list(executor.map(lambda _: import_at_same_time(), range(2)))
    assert sorted(report.inserted for report in reports) == [0, 2]
    assert sorted(report.duplicates for report in reports) == [0, 2]
    assert round_count(settings) == 2


def test_import_refuses_future_database(tmp_path: Path, settings: Settings) -> None:
    with closing(connect_database(settings)) as connection:
        connection.execute("PRAGMA user_version = 99")
    with pytest.raises(RuntimeError):
        import_json_file(write_import(tmp_path), settings)
    with closing(connect_database(settings)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 99


def test_cli_success_and_dry_run(tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    path = write_import(tmp_path)
    monkeypatch.setattr(command, "load_settings", lambda: settings)
    assert command.main([str(path), "--dry-run"]) == 0
    assert "Database changes: NONE" in capsys.readouterr().out
    assert not settings.database_path.exists()
    assert command.main([str(path)]) == 0
    output = capsys.readouterr().out
    assert "Import: COMPLETE\nRows read: 1\nInserted: 1\nDuplicates: 0" in output
    assert round_count(settings) == 1


def test_cli_rejects_secret_fields_without_echo(tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    marker = "SYNTHETIC_PRIVATE_MARKER"
    path = write_import(tmp_path, [round_document(raw_data={"nested": {"access_token": marker}})])
    monkeypatch.setattr(command, "load_settings", lambda: settings)
    assert command.main([str(path)]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "INVALID_ROUND at round 1" in output.err
    assert marker not in output.err and str(path) not in output.err
    assert not settings.database_path.exists()


def test_cli_duplicate_conflict_message(tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    path = write_import(tmp_path, [round_document(), round_document(multiplier="2.0")])
    monkeypatch.setattr(command, "load_settings", lambda: settings)
    assert command.main([str(path)]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "conflicting duplicate" in output.err
    assert round_count(settings) == 0


def test_cli_configuration_error_does_not_echo_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    marker = "SYNTHETIC_PRIVATE_CONFIG"
    monkeypatch.setenv("AIE_LOG_LEVEL", marker)
    assert command.main([str(tmp_path / "missing.json")]) == 1
    assert marker not in capsys.readouterr().err


def test_cli_invalid_usage_returns_two(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        command.main([])
    assert error.value.code == 2
    assert "usage:" in capsys.readouterr().err


def test_cli_subprocess_from_another_directory(tmp_path: Path) -> None:
    project = tmp_path / "app"
    project.mkdir()
    for package in ("config", "database", "collectors"):
        shutil.copytree(PROJECT_ROOT / package, project / package, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copyfile(PROJECT_ROOT / "import_rounds.py", project / "import_rounds.py")
    path = write_import(tmp_path)
    result = subprocess.run(
        [sys.executable, str(project / "import_rounds.py"), str(path)], cwd=tmp_path,
        text=True, capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "Inserted: 1" in result.stdout
    assert (project / "data" / "database" / "aie.sqlite3").is_file()
    assert not (tmp_path / "data").exists()
