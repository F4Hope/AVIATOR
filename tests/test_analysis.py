"""Phase 4 checks use synthetic TEST_FIXTURE records in temporary databases."""

from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, localcontext
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest

import analysis.descriptive as descriptive
from analysis.descriptive import AnalysisDataError, AnalysisFilters, analyze_database
import analysis.reporting as reporting
from analysis.reporting import report_to_dict, write_json_report
import analyze_rounds as command
from config.settings import PROJECT_ROOT, Settings, load_settings
from database.database import connect_database, connect_database_readonly
from database.migrations import SchemaVersionError, initialize_schema, verify_schema
from database.models import RoundRecord
from database.repository import RoundRepository


BASE_TIME = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)


def fixture_record(index: int, multiplier: str = "1.25", **changes: object) -> RoundRecord:
    values: dict[str, object] = {
        "source": "TEST_FIXTURE", "round_id": f"fixture-{index}", "multiplier": multiplier,
        "timestamp": BASE_TIME + timedelta(seconds=index),
        "collection_timestamp": BASE_TIME + timedelta(seconds=index + 1),
    }
    values.update(changes)
    return RoundRecord(**values)


def seed(settings: Settings, records: list[RoundRecord] | None = None) -> None:
    with closing(connect_database(settings)) as connection:
        initialize_schema(connection)
        RoundRepository(connection).insert_many(records or [])


def test_readonly_missing_database_does_not_create_paths(settings: Settings) -> None:
    with pytest.raises(sqlite3.OperationalError):
        connect_database_readonly(settings)
    assert not settings.data_dir.exists()


def test_readonly_connection_rejects_writes(settings: Settings) -> None:
    seed(settings)
    original = settings.database_path.read_bytes()
    with closing(connect_database_readonly(settings)) as connection:
        assert connection.row_factory is sqlite3.Row
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        assert verify_schema(connection) == 1
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("CREATE TABLE forbidden (value TEXT)")
    assert settings.database_path.read_bytes() == original


def test_readonly_uri_handles_special_path_characters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIE_DATABASE_FILENAME", "rounds #1.sqlite3")
    settings = load_settings(tmp_path / "project #+% with spaces")
    seed(settings, [fixture_record(1)])
    assert analyze_database(settings).selected_rounds == 1


def test_schema_verification_preserves_caller_transaction(settings: Settings) -> None:
    seed(settings)
    with closing(connect_database_readonly(settings)) as connection:
        connection.execute("BEGIN")
        assert verify_schema(connection) == 1
        assert connection.in_transaction
        connection.rollback()


def test_empty_database_reports_unavailable_statistics(settings: Settings) -> None:
    seed(settings)
    report = analyze_database(settings)
    assert report.status == "NO_DATA"
    assert report.total_stored == report.selected_rounds == report.source_count == 0
    assert report.first_result_at is report.last_result_at is None
    assert report.multipliers.minimum is report.multipliers.maximum is None
    assert report.multipliers.mean is report.multipliers.median is None
    assert all(bucket.count == 0 and bucket.percentage is None for bucket in report.buckets)
    assert report.quality.collection_delay_seconds.mean is None
    assert report.generated_at.tzinfo is UTC
    assert not settings.processed_data_dir.exists()


def test_single_round_has_defined_statistics(settings: Settings) -> None:
    seed(settings, [fixture_record(1, "1.23")])
    report = analyze_database(settings)
    assert report.status == "COMPLETE" and report.selected_rounds == 1
    assert report.multipliers.minimum == report.multipliers.maximum == report.multipliers.mean == report.multipliers.median == Decimal("1.23")
    assert report.quality.collection_delay_seconds.mean == Decimal("1")


def test_descriptive_statistics_and_bucket_boundaries(settings: Settings) -> None:
    seed(settings, [fixture_record(index, value) for index, value in enumerate(["1.1", "2", "5", "10", "20"])])
    report = analyze_database(settings)
    assert report.multipliers.minimum == Decimal("1.1")
    assert report.multipliers.maximum == Decimal("20")
    assert report.multipliers.mean == Decimal("7.62")
    assert report.multipliers.median == Decimal("5")
    assert [bucket.count for bucket in report.buckets] == [1, 1, 1, 2]
    assert [bucket.percentage for bucket in report.buckets] == [Decimal("20"), Decimal("20"), Decimal("20"), Decimal("40")]
    assert sum(bucket.count for bucket in report.buckets) == report.selected_rounds


def test_numeric_order_uses_decimals_not_strings(settings: Settings) -> None:
    seed(settings, [fixture_record(1, "10"), fixture_record(2, "2")])
    summary = analyze_database(settings).multipliers
    assert summary.minimum == Decimal("2") and summary.maximum == Decimal("10")
    assert summary.median == summary.mean == Decimal("6")


def test_long_decimal_median_keeps_precision(settings: Settings) -> None:
    seed(settings, [fixture_record(1, "1.00000000000000000000000000001"), fixture_record(2, "2.00000000000000000000000000001")])
    report = analyze_database(settings)
    assert report.multipliers.median == Decimal("1.50000000000000000000000000001")
    assert report.multipliers.mean == Decimal("1.5")


def test_mixed_large_and_small_decimals_keep_exact_median(settings: Settings) -> None:
    small = "1." + "0" * 253 + "1"
    large = "1" + "0" * 255
    seed(settings, [fixture_record(1, small), fixture_record(2, large)])
    report = analyze_database(settings)
    expected = "5" + "0" * 254 + ".5" + "0" * 253 + "5"
    assert format(report.multipliers.median, "f") == expected


@pytest.mark.parametrize("values,expected", [
    (["1.000000", "1.000001"], "1.000000"),
    (["1.000001", "1.000002"], "1.000002"),
    (["1", "1", "2"], "1.333333"),
])
def test_mean_rounding_is_half_even(settings: Settings, values: list[str], expected: str) -> None:
    seed(settings, [fixture_record(index, value) for index, value in enumerate(values)])
    assert analyze_database(settings).multipliers.mean == Decimal(expected)


def test_percentage_rounding_and_decimal_context_isolation(settings: Settings) -> None:
    seed(settings, [fixture_record(1, "1"), fixture_record(2, "1"), fixture_record(3, "2")])
    with localcontext() as context:
        context.prec = 2
        context.rounding = ROUND_DOWN
        flags = context.flags.copy()
        report = analyze_database(settings)
        assert report.multipliers.mean == Decimal("1.333333")
        assert [bucket.percentage for bucket in report.buckets] == [Decimal("66.67"), Decimal("33.33"), Decimal("0"), Decimal("0")]
        assert context.prec == 2 and context.rounding == ROUND_DOWN and context.flags == flags


def test_collection_delays_include_days_and_microseconds(settings: Settings) -> None:
    seed(settings, [
        fixture_record(1, collection_timestamp=BASE_TIME + timedelta(seconds=1, microseconds=1)),
        fixture_record(2, collection_timestamp=BASE_TIME + timedelta(days=1, seconds=2, microseconds=3)),
    ])
    summary = analyze_database(settings).quality.collection_delay_seconds
    assert summary.minimum == Decimal("0.000001")
    assert summary.maximum == Decimal("86400.000003")
    assert summary.mean == summary.median == Decimal("43200.000002")


def test_metadata_coverage_and_timestamp_ties_are_source_scoped(settings: Settings) -> None:
    time = BASE_TIME + timedelta(seconds=10)
    records = [
        fixture_record(1, timestamp=time, collection_timestamp=time),
        fixture_record(2, timestamp=time, collection_timestamp=time,
            started_at=BASE_TIME, pre_round_observed_at=BASE_TIME - timedelta(seconds=1),
            pre_round_data={"commitment": "test-only"}, post_round_data={"revealed": "test-only"},
            raw_data={"fixture": "test-only"}),
        fixture_record(3, source="OTHER_TEST_FIXTURE", timestamp=time, collection_timestamp=time),
    ]
    seed(settings, records)
    report = analyze_database(settings)
    assert report.source_count == 2
    assert report.quality.repeated_result_timestamps == 1
    assert report.quality.rows_with_known_start == report.quality.rows_with_pre_observation == 1
    assert report.quality.rows_with_pre_data == report.quality.rows_with_post_data == report.quality.rows_with_raw_data == 1


def test_source_and_half_open_time_filters(settings: Settings) -> None:
    seed(settings, [fixture_record(index, source="OTHER_TEST_FIXTURE" if index == 2 else "TEST_FIXTURE") for index in range(4)])
    filters = AnalysisFilters(source="TEST_FIXTURE", start=BASE_TIME + timedelta(seconds=1), end=BASE_TIME + timedelta(seconds=3))
    report = analyze_database(settings, filters)
    assert report.total_stored == 4 and report.selected_rounds == 1
    assert report.first_result_at == report.last_result_at == BASE_TIME + timedelta(seconds=1)


def test_filter_timezone_normalizes_to_utc() -> None:
    value = datetime(2025, 1, 1, 13, tzinfo=timezone(timedelta(hours=1)))
    assert AnalysisFilters(start=value).start == BASE_TIME


@pytest.mark.parametrize("arguments", [
    {"source": " "}, {"source": "name\nvalue"}, {"start": datetime(2025, 1, 1)},
    {"end": datetime(2025, 1, 1)}, {"start": BASE_TIME, "end": BASE_TIME},
    {"start": BASE_TIME + timedelta(days=1), "end": BASE_TIME},
])
def test_invalid_filters_rejected(arguments: dict) -> None:
    with pytest.raises(ValueError):
        AnalysisFilters(**arguments)


def test_source_filter_is_parameterized(settings: Settings) -> None:
    source = "source'; DROP TABLE rounds; --"
    seed(settings, [fixture_record(1, source=source), fixture_record(2)])
    report = analyze_database(settings, AnalysisFilters(source=source))
    assert report.selected_rounds == 1 and report.total_stored == 2


def test_empty_filtered_selection_has_null_statistics(settings: Settings) -> None:
    seed(settings, [fixture_record(1)])
    report = analyze_database(settings, AnalysisFilters(source="ABSENT_TEST_FIXTURE"))
    assert report.status == "NO_DATA" and report.total_stored == 1 and report.selected_rounds == 0
    assert report.multipliers.mean is None


def test_analysis_limit_refuses_truncation(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    seed(settings, [fixture_record(1), fixture_record(2, source="OTHER_TEST_FIXTURE")])
    monkeypatch.setattr(descriptive, "MAX_ANALYSIS_ROUNDS", 1)
    with pytest.raises(AnalysisDataError, match="limit"):
        analyze_database(settings)
    assert analyze_database(settings, AnalysisFilters(source="OTHER_TEST_FIXTURE")).selected_rounds == 1


def test_analysis_preserves_database_bytes(settings: Settings) -> None:
    seed(settings, [fixture_record(1)])
    before = hashlib.sha256(settings.database_path.read_bytes()).digest()
    analyze_database(settings)
    assert hashlib.sha256(settings.database_path.read_bytes()).digest() == before


@pytest.mark.parametrize("version", [0, 99])
def test_analysis_refuses_uninitialized_or_future_database(settings: Settings, version: int) -> None:
    with closing(connect_database(settings)) as connection:
        connection.execute(f"PRAGMA user_version = {version}")  # Fixed test parameters only.
    original = settings.database_path.read_bytes()
    with pytest.raises(SchemaVersionError):
        analyze_database(settings)
    assert settings.database_path.read_bytes() == original


@pytest.mark.parametrize("change", [
    {"multiplier": "0.5"}, {"timestamp": "2025-01-01T12:00:00"},
    {"pre_round_data": "[]"}, {"source": " spaced-source "},
])
def test_invalid_stored_data_is_rejected_without_silent_skipping(settings: Settings, change: dict) -> None:
    seed(settings, [fixture_record(1)])
    with closing(connect_database(settings)) as connection:
        row = dict(connection.execute("SELECT * FROM rounds").fetchone())
        row.update(id=2, round_id="invalid-fixture", **change)
        connection.execute("PRAGMA ignore_check_constraints = ON")
        columns = tuple(row)
        connection.execute(
            "INSERT INTO rounds (" + ",".join(columns) + ") VALUES (" + ",".join("?" for _ in columns) + ")",
            tuple(row.values()),
        )
    with pytest.raises(AnalysisDataError):
        analyze_database(settings)


def test_analysis_is_a_consistent_snapshot_during_concurrent_insert(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    seed(settings, [fixture_record(1)])
    with closing(connect_database(settings)) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
    original_verify = descriptive.verify_schema

    def verify_then_insert(connection):
        version = original_verify(connection)  # Establishes the read snapshot.
        with closing(connect_database(settings)) as writer:
            RoundRepository(writer).insert(fixture_record(2))
        return version

    monkeypatch.setattr(descriptive, "verify_schema", verify_then_insert)
    report = analyze_database(settings)
    assert report.total_stored == report.selected_rounds == 1
    with closing(connect_database(settings)) as connection:
        assert RoundRepository(connection).count() == 2


def test_json_report_contains_aggregates_without_raw_values(settings: Settings) -> None:
    marker = "SYNTHETIC_PRIVATE_PAYLOAD_MARKER"
    seed(settings, [fixture_record(1, raw_data={"note": marker})])
    report = analyze_database(settings)
    document = report_to_dict(report)
    assert document["phase"] == 6 and document["report_version"] == document["schema_version"] == 1
    assert document["multipliers"]["minimum"] == "1.25"
    assert document["numeric_policy"]["mean_decimal_places"] == 6
    serialized = json.dumps(document, allow_nan=False)
    assert marker not in serialized and "fixture-1" not in serialized


def test_empty_json_report_uses_null(settings: Settings) -> None:
    seed(settings)
    document = report_to_dict(analyze_database(settings))
    assert document["status"] == "NO_DATA"
    assert document["multipliers"]["mean"] is None
    assert all(bucket["percentage"] is None for bucket in document["buckets"])


def test_export_creates_report_inside_processed_directory(settings: Settings) -> None:
    seed(settings, [fixture_record(1)])
    original = settings.database_path.read_bytes()
    report = analyze_database(settings)
    path = write_json_report(report, settings)
    assert path == settings.processed_data_dir / "analysis.json"
    assert json.loads(path.read_text()) == report_to_dict(report)
    assert settings.database_path.read_bytes() == original
    assert list(settings.processed_data_dir.iterdir()) == [path]


@pytest.mark.parametrize("filename", ["../outside.json", "folder/name.json", "folder\\name.json", ".env", ".private.json", "report.txt", "report\n.json", " report.json", ""])
def test_export_rejects_unsafe_names(settings: Settings, filename: str) -> None:
    seed(settings)
    with pytest.raises(ValueError):
        write_json_report(analyze_database(settings), settings, filename)
    assert not settings.processed_data_dir.exists()


def test_existing_report_preserved_without_explicit_overwrite(settings: Settings) -> None:
    seed(settings)
    report = analyze_database(settings)
    path = write_json_report(report, settings)
    path.write_text("existing fixture report")
    with pytest.raises(FileExistsError):
        write_json_report(report, settings)
    assert path.read_text() == "existing fixture report"
    write_json_report(report, settings, overwrite=True)
    assert json.loads(path.read_text())["status"] == "NO_DATA"


def test_failed_publication_cleans_temporary_files(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    seed(settings)

    def fail_link(*args):
        raise OSError("synthetic publication failure")

    monkeypatch.setattr(reporting.os, "link", fail_link)
    with pytest.raises(OSError):
        write_json_report(analyze_database(settings), settings)
    assert list(settings.processed_data_dir.iterdir()) == []


def test_report_publication_race_preserves_newly_created_target(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    seed(settings)
    original_link = reporting.os.link

    def create_competing_report(source, target):
        Path(target).write_text("competing fixture report")
        original_link(source, target)

    monkeypatch.setattr(reporting.os, "link", create_competing_report)
    with pytest.raises(FileExistsError):
        write_json_report(analyze_database(settings), settings)
    assert (settings.processed_data_dir / "analysis.json").read_text() == "competing fixture report"
    assert len(list(settings.processed_data_dir.iterdir())) == 1


def test_cli_empty_analysis_and_export(settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    seed(settings)
    monkeypatch.setattr(command, "load_settings", lambda: settings)
    assert command.main(["--output", "analysis.json"]) == 0
    output = capsys.readouterr().out
    assert "Phase: 6\nAnalysis: NO_DATA\nDatabase rounds: 0\nSelected rounds: 0" in output
    assert "Report: SAVED" in output
    assert (settings.processed_data_dir / "analysis.json").is_file()


def test_cli_prints_defined_statistics(settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    seed(settings, [fixture_record(1, "2"), fixture_record(2, "10")])
    monkeypatch.setattr(command, "load_settings", lambda: settings)
    assert command.main([]) == 0
    output = capsys.readouterr().out
    assert "Minimum multiplier: 2\nMedian multiplier: 6\nMean multiplier: 6\nMaximum multiplier: 10" in output
    assert not settings.processed_data_dir.exists()


@pytest.mark.parametrize("arguments", [["--start", "SYNTHETIC_PRIVATE_MARKER"], ["--start", "2025-01-01"], ["--output", "../outside.json"]])
def test_cli_failure_does_not_echo_inputs(settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], arguments: list[str]) -> None:
    seed(settings)
    monkeypatch.setattr(command, "load_settings", lambda: settings)
    assert command.main(arguments) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "Analysis failed" in output.err and "SYNTHETIC_PRIVATE_MARKER" not in output.err


def test_cli_missing_database_does_not_create_it(settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(command, "load_settings", lambda: settings)
    assert command.main([]) == 1
    assert "python main.py" in capsys.readouterr().err
    assert not settings.data_dir.exists()


def test_cli_existing_report_requires_overwrite(settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    seed(settings)
    monkeypatch.setattr(command, "load_settings", lambda: settings)
    assert command.main(["--output", "analysis.json"]) == 0
    capsys.readouterr()
    assert command.main(["--output", "analysis.json"]) == 1
    assert "Report already exists" in capsys.readouterr().err
    assert command.main(["--output", "analysis.json", "--overwrite"]) == 0


def test_cli_overwrite_without_output_is_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        command.main(["--overwrite"])
    assert error.value.code == 2
    assert "requires --output" in capsys.readouterr().err


def test_cli_subprocess_from_another_directory(tmp_path: Path) -> None:
    project = tmp_path / "app"
    project.mkdir()
    for package in ("config", "database", "analysis"):
        shutil.copytree(PROJECT_ROOT / package, project / package, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copyfile(PROJECT_ROOT / "analyze_rounds.py", project / "analyze_rounds.py")
    settings = load_settings(project)
    seed(settings, [fixture_record(1)])
    result = subprocess.run([sys.executable, str(project / "analyze_rounds.py")], cwd=tmp_path, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "Selected rounds: 1" in result.stdout
    assert not (tmp_path / "data").exists()
