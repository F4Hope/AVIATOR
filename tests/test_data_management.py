"""Export and backup checks use synthetic fixtures in temporary databases only."""

from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest

from analysis.descriptive import AnalysisFilters
import backup_database as backup_command
from collectors.json_importer import import_json_file, load_json_rounds
from config.settings import PROJECT_ROOT, Settings, load_settings
import database.backup as backup
from database.backup import BackupTimeoutError, BackupVerificationError, create_backup, verify_backup
from database.database import connect_database
import database.export as export
from database.export import ExportDataError, export_rounds
from database.migrations import SchemaVersionError, initialize_schema
from database.models import RoundRecord, timestamp_text
from database.repository import RoundRepository
import export_rounds as export_command
import utils.files as files


BASE_TIME = datetime(2025, 1, 1, 12, tzinfo=UTC)


def record(index: int = 0, multiplier: str = "1.25", **changes: object) -> RoundRecord:
    values = {
        "source": "TEST_FIXTURE", "round_id": f"fixture-{index}", "multiplier": multiplier,
        "timestamp": BASE_TIME + timedelta(seconds=index + 10),
        "collection_timestamp": BASE_TIME + timedelta(seconds=index + 11),
    }
    values.update(changes)
    return RoundRecord(**values)


def seed(settings: Settings, records: list[RoundRecord] | None = None) -> None:
    with closing(connect_database(settings)) as connection:
        initialize_schema(connection)
        RoundRepository(connection).insert_many(records or [])


def stored_rows(settings: Settings) -> list[tuple]:
    with closing(connect_database(settings)) as connection:
        return [tuple(row) for row in connection.execute("SELECT * FROM rounds ORDER BY id")]


def test_backup_path_configuration_has_no_side_effects(settings: Settings) -> None:
    assert settings.backup_dir == settings.database_dir / "backups"
    assert not settings.backup_dir.exists()
    assert settings.backup_dir not in settings.required_directories


def test_empty_export_is_valid_import_format_and_preserves_database(settings: Settings) -> None:
    seed(settings)
    original = settings.database_path.read_bytes()
    report = export_rounds(settings)
    assert report.total_stored == report.exported_rounds == 0
    assert json.loads(report.path.read_bytes()) == {"format_version": 1, "rounds": []}
    assert load_json_rounds(report.path).records == ()
    assert report.byte_count == report.path.stat().st_size
    assert report.sha256 == hashlib.sha256(report.path.read_bytes()).hexdigest()
    assert settings.database_path.read_bytes() == original
    assert not settings.backup_dir.exists()


def test_export_round_trip_preserves_all_round_fields_and_exact_replays(settings: Settings, tmp_path: Path) -> None:
    item = record(
        multiplier="1.00000000000000000000000000001", source="é_TEST_FIXTURE",
        started_at=BASE_TIME, pre_round_observed_at=BASE_TIME - timedelta(seconds=1),
        pre_round_data={"fixture_commitment": "synthetic"},
        post_round_data={"fixture": [1, True, None, {"precise": "1.0000000000001"}]},
        raw_data={"original_fixture": "1.0000000000000000000000000000100", "finite": 1.75},
    )
    seed(settings, [item])
    original = settings.database_path.read_bytes()
    report = export_rounds(settings)
    document = json.loads(report.path.read_bytes())
    assert set(document) == {"format_version", "rounds"}
    assert "id" not in document["rounds"][0] and "ingested_at" not in document["rounds"][0]
    assert document["rounds"][0]["multiplier"] == item.multiplier_text
    assert load_json_rounds(report.path).records == (item,)
    replay = import_json_file(report.path, settings)
    assert replay.inserted == 0 and replay.duplicates == 1
    assert settings.database_path.read_bytes() == original
    other = load_settings(tmp_path / "import-target")
    result = import_json_file(report.path, other)
    assert result.inserted == 1
    with closing(connect_database(other)) as connection:
        assert RoundRepository(connection).get(item.source, item.round_id).record == item


def test_export_order_and_source_time_filters(settings: Settings) -> None:
    source = "TEST_FIXTURE'; --"
    seed(settings, [record(2, source=source), record(0, source=source), record(1, source=source), record(1, source="OTHER_TEST_FIXTURE")])
    report = export_rounds(settings, filters=AnalysisFilters(source=source, start=BASE_TIME + timedelta(seconds=10), end=BASE_TIME + timedelta(seconds=12)))
    assert report.total_stored == 4 and report.exported_rounds == 2
    assert [item.round_id for item in load_json_rounds(report.path).records] == ["fixture-0", "fixture-1"]
    full = export_rounds(settings, "all.json")
    assert [(item.timestamp, item.source, item.round_id) for item in load_json_rounds(full.path).records] == sorted((item.timestamp, item.source, item.round_id) for item in load_json_rounds(full.path).records)
    empty = export_rounds(settings, "empty.json", AnalysisFilters(source="ABSENT_TEST_FIXTURE"))
    assert empty.exported_rounds == 0 and load_json_rounds(empty.path).records == ()


@pytest.mark.parametrize("operation", [export_rounds, create_backup])
def test_missing_database_does_not_create_paths(settings: Settings, operation) -> None:
    with pytest.raises(sqlite3.OperationalError):
        operation(settings)
    assert not settings.data_dir.exists()


@pytest.mark.parametrize("operation,filename", [(export_rounds, "../outside.json"), (export_rounds, "bad.txt"), (export_rounds, ".hidden.json"), (export_rounds, "bad\nname.json"), (export_rounds, " spaced.json"), (create_backup, "../outside.sqlite3"), (create_backup, "bad.json"), (create_backup, "C:\\file.sqlite3"), (create_backup, ".hidden.sqlite3")])
def test_output_names_are_rejected_before_database_access(settings: Settings, operation, filename: str) -> None:
    with pytest.raises(ValueError):
        operation(settings, filename)
    assert not settings.data_dir.exists()


def test_export_existing_file_is_preserved_until_explicit_overwrite(settings: Settings) -> None:
    seed(settings)
    report = export_rounds(settings)
    report.path.write_bytes(b"EXISTING_TEST_FIXTURE")
    with pytest.raises(FileExistsError):
        export_rounds(settings)
    assert report.path.read_bytes() == b"EXISTING_TEST_FIXTURE"
    export_rounds(settings, overwrite=True)
    assert load_json_rounds(report.path).records == ()


def test_export_overwrite_replaces_symlink_not_its_target(settings: Settings, tmp_path: Path) -> None:
    seed(settings)
    settings.processed_data_dir.mkdir()
    elsewhere = tmp_path / "original-fixture.txt"
    elsewhere.write_bytes(b"PRESERVE_TEST_FIXTURE")
    link = settings.processed_data_dir / "export.json"
    link.symlink_to(elsewhere)
    with pytest.raises(FileExistsError):
        export_rounds(settings, "export.json")
    export_rounds(settings, "export.json", overwrite=True)
    assert not link.is_symlink() and elsewhere.read_bytes() == b"PRESERVE_TEST_FIXTURE"


@pytest.mark.parametrize("operation,filename", [(export_rounds, "raced.json"), (create_backup, "raced.sqlite3")])
def test_concurrent_output_creation_is_preserved_and_temporary_files_cleaned(settings: Settings, monkeypatch: pytest.MonkeyPatch, operation, filename: str) -> None:
    seed(settings, [record()])
    original_link = files.os.link
    def create_rival(source, target) -> None:
        Path(target).write_bytes(b"RIVAL_TEST_FIXTURE")
        original_link(source, target)
    monkeypatch.setattr(files.os, "link", create_rival)
    with pytest.raises(FileExistsError):
        operation(settings, filename)
    directory = settings.processed_data_dir if operation is export_rounds else settings.backup_dir
    assert (directory / filename).read_bytes() == b"RIVAL_TEST_FIXTURE"
    assert not list(directory.glob(".aie-*"))


def test_export_row_count_limit_refuses_truncation(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    seed(settings, [record(0), record(1)])
    monkeypatch.setattr(export, "MAX_IMPORT_ROUNDS", 1)
    with pytest.raises(ExportDataError):
        export_rounds(settings)
    assert not settings.processed_data_dir.exists()


def test_export_file_size_limit_has_no_partial_output(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    seed(settings, [record(0), record(1)])
    monkeypatch.setattr(export, "MAX_IMPORT_BYTES", 500)
    with pytest.raises(ExportDataError):
        export_rounds(settings)
    assert not settings.processed_data_dir.exists()


def test_export_large_stored_payload_rejected_before_parsing(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    seed(settings, [record(raw_data={"fixture": "x" * 600})])
    monkeypatch.setattr(export, "MAX_IMPORT_BYTES", 500)
    with pytest.raises(ExportDataError):
        export_rounds(settings)
    assert not settings.processed_data_dir.exists()


@pytest.mark.parametrize("changes", [
    {"multiplier": "2.00"}, {"source": " spaced_TEST_FIXTURE "},
    {"timestamp": "2025-01-01T12:00:10"}, {"raw_data": '{"key":1,"key":2}'},
    {"raw_data": '{"Cookie":"SECRET_TEST_FIXTURE"}'}, {"raw_data": '{"value":NaN}'},
    {"post_round_data": "[]"},
])
def test_export_rejects_invalid_or_noncanonical_stored_data(settings: Settings, changes: dict) -> None:
    seed(settings, [record()])
    with closing(connect_database(settings)) as connection:
        row = dict(connection.execute("SELECT * FROM rounds").fetchone())
        row.update(id=2, round_id="invalid-fixture", **changes)
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute("INSERT INTO rounds (" + ", ".join(row) + ") VALUES (" + ", ".join("?" for _ in row) + ")", tuple(row.values()))
    before = settings.database_path.read_bytes()
    with pytest.raises(ExportDataError):
        export_rounds(settings)
    assert settings.database_path.read_bytes() == before and not settings.processed_data_dir.exists()


@pytest.mark.parametrize("operation", [export_rounds, create_backup])
@pytest.mark.parametrize("version", [0, 99])
def test_incompatible_schema_is_preserved(settings: Settings, operation, version: int) -> None:
    with closing(connect_database(settings)) as connection:
        connection.execute(f"PRAGMA user_version = {version}")
    before = settings.database_path.read_bytes()
    with pytest.raises(SchemaVersionError):
        operation(settings)
    assert settings.database_path.read_bytes() == before
    assert not settings.processed_data_dir.exists() and not settings.backup_dir.exists()


def test_backup_preserves_complete_database_and_can_be_verified(settings: Settings) -> None:
    seed(settings, [record(0, "1.00000000000000000000000001", raw_data={"fixture": [True, "synthetic"]}), record(1, "10")])
    original = settings.database_path.read_bytes()
    rows = stored_rows(settings)
    report = create_backup(settings, "preserved.sqlite3")
    assert report.schema_version == 1 and report.rounds_stored == 2
    assert report.path.parent == settings.backup_dir
    assert report.sha256 == hashlib.sha256(report.path.read_bytes()).hexdigest()
    assert report.byte_count == report.path.stat().st_size
    copied = replace(settings, database_path=report.path, database_dir=report.path.parent)
    assert stored_rows(copied) == rows
    with closing(connect_database(copied)) as connection:
        assert initialize_schema(connection) == 1
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM rounds")
    assert verify_backup(settings, report.path.name, expected_sha256=report.sha256.upper()) == report
    assert settings.database_path.read_bytes() == original
    assert set(path.name for path in settings.backup_dir.iterdir()) == {"preserved.sqlite3"}


def test_empty_backup_and_unique_automatic_names(settings: Settings) -> None:
    seed(settings)
    first = create_backup(settings)
    second = create_backup(settings)
    assert first.path != second.path and first.rounds_stored == second.rounds_stored == 0
    assert first.path.name.startswith("aie-backup-")
    assert verify_backup(settings, first.path.name).rounds_stored == 0


def test_backup_never_overwrites_existing_file_or_symlink(settings: Settings, tmp_path: Path) -> None:
    seed(settings)
    report = create_backup(settings, "existing.sqlite3")
    original = report.path.read_bytes()
    with pytest.raises(FileExistsError):
        create_backup(settings, "existing.sqlite3")
    assert report.path.read_bytes() == original
    outside = tmp_path / "keep-fixture.sqlite3"
    outside.write_bytes(b"KEEP_TEST_FIXTURE")
    (settings.backup_dir / "linked.sqlite3").symlink_to(outside)
    with pytest.raises(FileExistsError):
        create_backup(settings, "linked.sqlite3")
    with pytest.raises(BackupVerificationError):
        verify_backup(settings, "linked.sqlite3")
    assert outside.read_bytes() == b"KEEP_TEST_FIXTURE"


def test_backup_can_be_verified_when_live_database_is_missing(settings: Settings) -> None:
    seed(settings, [record()])
    report = create_backup(settings)
    settings.database_path.unlink()
    assert verify_backup(settings, report.path.name).rounds_stored == 1
    assert not settings.database_path.exists()


def test_backup_digest_mismatch_and_malformed_digest_fail(settings: Settings) -> None:
    seed(settings)
    report = create_backup(settings)
    before = report.path.read_bytes()
    with pytest.raises(BackupVerificationError):
        verify_backup(settings, report.path.name, expected_sha256="0" * 64)
    with pytest.raises(ValueError):
        verify_backup(settings, report.path.name, expected_sha256="invalid")
    assert report.path.read_bytes() == before


def test_corrupted_backup_is_rejected_without_changes(settings: Settings) -> None:
    seed(settings)
    report = create_backup(settings)
    corrupted = b"CORRUPT_TEST_FIXTURE" + report.path.read_bytes()[20:]
    report.path.write_bytes(corrupted)
    with pytest.raises(sqlite3.DatabaseError):
        verify_backup(settings, report.path.name)
    assert report.path.read_bytes() == corrupted


def test_backup_timeout_does_not_publish_partial_file(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    seed(settings, [record()])
    before = settings.database_path.read_bytes()
    monkeypatch.setattr(backup, "BACKUP_TIMEOUT_SECONDS", 0)
    with pytest.raises(BackupTimeoutError):
        create_backup(settings, "timeout.sqlite3")
    assert settings.database_path.read_bytes() == before
    assert not list(settings.backup_dir.iterdir())


def test_backup_verification_failure_does_not_publish_file(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    seed(settings)
    def fail(*args) -> None:
        raise BackupVerificationError("TEST_FIXTURE failure")
    monkeypatch.setattr(backup, "_verify_file", fail)
    with pytest.raises(BackupVerificationError):
        create_backup(settings, "invalid.sqlite3")
    assert not list(settings.backup_dir.iterdir())


def test_backup_verification_refuses_invalid_round_data(settings: Settings) -> None:
    seed(settings, [record()])
    with closing(connect_database(settings)) as connection:
        row = dict(connection.execute("SELECT * FROM rounds").fetchone())
        row.update(id=2, round_id="invalid-fixture", multiplier="0.5")
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute("INSERT INTO rounds (" + ", ".join(row) + ") VALUES (" + ", ".join("?" for _ in row) + ")", tuple(row.values()))
    before = settings.database_path.read_bytes()
    with pytest.raises(BackupVerificationError):
        create_backup(settings, "constraint-failure.sqlite3")
    assert settings.database_path.read_bytes() == before
    assert not list(settings.backup_dir.iterdir())


@pytest.mark.parametrize("operation,filename", [(export_rounds, "failed.json"), (create_backup, "failed.sqlite3")])
def test_flush_failure_preserves_source_and_cleans_temporary_files(settings: Settings, monkeypatch: pytest.MonkeyPatch, operation, filename: str) -> None:
    seed(settings, [record()])
    before = settings.database_path.read_bytes()
    def fail(handle: int) -> None:
        raise OSError("TEST_FIXTURE flush failure")
    monkeypatch.setattr(files.os, "fsync", fail)
    with pytest.raises(OSError):
        operation(settings, filename)
    directory = settings.processed_data_dir if operation is export_rounds else settings.backup_dir
    assert not list(directory.iterdir())
    assert settings.database_path.read_bytes() == before


def test_file_writer_rejects_traversal_before_creating_directories(tmp_path: Path) -> None:
    directory = tmp_path / "not-created"
    with pytest.raises(ValueError):
        files.write_bytes(directory, "../outside.json", b"{}")
    assert not directory.exists() and not (tmp_path / "outside.json").exists()


def test_wal_backup_uses_pinned_snapshot_during_new_commit(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    seed(settings, [record(index, raw_data={"fixture": "x" * 5000}) for index in range(10)])
    with closing(connect_database(settings)) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    original_check = backup._check_deadline
    written = False
    def concurrent_write(deadline: float) -> None:
        nonlocal written
        original_check(deadline)
        if not written:
            written = True
            with closing(connect_database(settings)) as writer:
                RoundRepository(writer).insert(record(100))
    monkeypatch.setattr(backup, "BACKUP_PAGES_PER_STEP", 1)
    monkeypatch.setattr(backup, "_check_deadline", concurrent_write)
    report = create_backup(settings)
    assert report.rounds_stored == 10 and len(stored_rows(settings)) == 11
    copied = replace(settings, database_path=report.path, database_dir=report.path.parent)
    assert len(stored_rows(copied)) == 10
    assert verify_backup(settings, report.path.name).rounds_stored == 10


def test_export_counts_and_rows_share_a_snapshot_during_new_commit(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    seed(settings, [record(0), record(1)])
    with closing(connect_database(settings)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
    original_document = export._record_document
    written = False
    def concurrent_write(row):
        nonlocal written
        if not written:
            written = True
            with closing(connect_database(settings)) as writer:
                RoundRepository(writer).insert(record(100))
        return original_document(row)
    monkeypatch.setattr(export, "_record_document", concurrent_write)
    report = export_rounds(settings)
    assert report.total_stored == report.exported_rounds == 2
    assert len(load_json_rounds(report.path).records) == 2 and len(stored_rows(settings)) == 3


@pytest.mark.parametrize("command", [export_command, backup_command])
def test_command_errors_do_not_expose_values(settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], command) -> None:
    marker = "PRIVATE_TEST_FIXTURE"
    monkeypatch.setattr(command, "load_settings", lambda: settings)
    assert command.main(["--output", "../" + marker]) == 1
    output = capsys.readouterr()
    assert output.out == "" and marker not in output.err and "failed" in output.err


def test_export_command_and_backup_command_outputs(settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    seed(settings, [record()])
    monkeypatch.setattr(export_command, "load_settings", lambda: settings)
    monkeypatch.setattr(backup_command, "load_settings", lambda: settings)
    assert export_command.main([]) == 0
    output = capsys.readouterr().out
    assert "Phase: 8\nExport: SAVED" in output and "Rounds exported: 1" in output and "Database changes: NONE" in output
    assert backup_command.main(["--output", "cli.sqlite3"]) == 0
    output = capsys.readouterr().out
    assert "Phase: 8\nBackup: VERIFIED\nMode: CREATED" in output
    assert "Rounds backed up: 1" in output
    assert backup_command.main(["--verify", "cli.sqlite3"]) == 0
    assert "Mode: VERIFY_ONLY" in capsys.readouterr().out


@pytest.mark.parametrize("arguments", [["--expect-sha256", "0" * 64], ["--output", "x.sqlite3", "--verify", "x.sqlite3"], ["--overwrite"]])
def test_backup_command_rejects_invalid_usage(arguments: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        backup_command.main(arguments)
    assert error.value.code == 2


def test_real_commands_work_outside_project_working_directory(tmp_path: Path) -> None:
    project = tmp_path / "command-project"
    project.mkdir()
    for package in ("config", "database", "collectors", "analysis", "utils"):
        shutil.copytree(PROJECT_ROOT / package, project / package, ignore=shutil.ignore_patterns("__pycache__"))
    for filename in ("main.py", "export_rounds.py", "backup_database.py"):
        shutil.copyfile(PROJECT_ROOT / filename, project / filename)
    environment = os.environ.copy()
    for name in ("AIE_ENVIRONMENT", "AIE_LOG_LEVEL", "AIE_DATABASE_FILENAME", "PYTHON_DOTENV_DISABLED"):
        environment.pop(name, None)
    for script, arguments in (("main.py", []), ("export_rounds.py", []), ("backup_database.py", ["--output", "outside.sqlite3"]), ("backup_database.py", ["--verify", "outside.sqlite3"])):
        result = subprocess.run([sys.executable, str(project / script), *arguments], cwd=tmp_path, env=environment, text=True, capture_output=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert "Phase: 8" in result.stdout
    assert (project / "data/processed/rounds-export.json").is_file()
    assert (project / "data/database/backups/outside.sqlite3").is_file()
    assert not (tmp_path / "data").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission check")
def test_output_files_are_private(settings: Settings) -> None:
    seed(settings)
    assert export_rounds(settings).path.stat().st_mode & 0o777 == 0o600
    assert create_backup(settings).path.stat().st_mode & 0o777 == 0o600
