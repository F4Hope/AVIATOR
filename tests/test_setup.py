"""Verify configuration, directories, SQLite, logging, and real CLI startup."""

from contextlib import closing
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest

from config.logging_config import configure_logging
from config.settings import PROJECT_ROOT, LOG_LEVELS, Settings, ensure_directories, load_settings
from database.database import connect_database
import main as application


def test_configuration_defaults(settings: Settings, tmp_path: Path) -> None:
    assert settings.project_root == tmp_path.resolve()
    assert settings.environment == "DEVELOPMENT"
    assert settings.log_level == "INFO"
    assert settings.database_path == tmp_path / "data" / "database" / "aie.sqlite3"
    assert not settings.data_dir.exists()  # Loading config has no filesystem writes.


def test_configuration_loads_dotenv(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "AIE_ENVIRONMENT=TEST\nAIE_LOG_LEVEL=DEBUG\nAIE_DATABASE_FILENAME=custom.db\n",
        encoding="utf-8",
    )
    settings = load_settings(project_root=tmp_path)
    assert settings.environment == "TEST"
    assert settings.log_level == "DEBUG"
    assert settings.database_path == tmp_path / "data" / "database" / "custom.db"


def test_existing_environment_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text("AIE_LOG_LEVEL=DEBUG\n", encoding="utf-8")
    monkeypatch.setenv("AIE_LOG_LEVEL", "WARNING")
    assert load_settings(project_root=tmp_path).log_level == "WARNING"


@pytest.mark.parametrize("variable,value", [
    ("AIE_ENVIRONMENT", "unknown"),
    ("AIE_LOG_LEVEL", "verbose"),
    ("AIE_DATABASE_FILENAME", ""),
    ("AIE_DATABASE_FILENAME", "../outside.db"),
    ("AIE_DATABASE_FILENAME", "subfolder/outside.db"),
    ("AIE_DATABASE_FILENAME", "subfolder\\outside.db"),
    ("AIE_DATABASE_FILENAME", "C:\\outside.db"),
    ("AIE_DATABASE_FILENAME", "settings.txt"),
])
def test_invalid_configuration_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    monkeypatch.setenv(variable, value)
    with pytest.raises(ValueError):
        load_settings(project_root=tmp_path)


def test_project_root_is_independent_of_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = load_settings()
    assert settings.project_root == PROJECT_ROOT
    assert settings.database_path.parent == PROJECT_ROOT / "data" / "database"


def test_required_directories_exist(settings: Settings) -> None:
    ensure_directories(settings)
    ensure_directories(settings)  # Repeated startup is safe.
    assert all(path.is_dir() for path in settings.required_directories)


def test_directory_creation_failure(settings: Settings) -> None:
    settings.data_dir.write_text("A file blocks the data directory.", encoding="utf-8")
    with pytest.raises(OSError):
        ensure_directories(settings)


def test_sqlite_connection_works_without_schema(settings: Settings) -> None:
    with closing(connect_database(settings)) as connection:
        assert isinstance(connection, sqlite3.Connection)
        assert connection.execute("SELECT 1 AS ready").fetchone()["ready"] == 1
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall() == []
    assert settings.database_path.is_file()
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")


def test_application_initialization_closes_connection(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    connections: list[sqlite3.Connection] = []

    def open_connection(active_settings: Settings) -> sqlite3.Connection:
        connection = connect_database(active_settings)
        connections.append(connection)
        return connection

    monkeypatch.setattr(application, "load_settings", lambda: settings)
    monkeypatch.setattr(application, "connect_database", open_connection)
    state = application.initialize_application()
    assert state.settings == settings
    assert state.schema_version == 1
    assert state.rounds_stored == 0
    assert settings.database_path.is_file()
    assert all(path.is_dir() for path in settings.required_directories)
    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        connections[0].execute("SELECT 1")


@pytest.mark.parametrize("level", LOG_LEVELS)
def test_logging_levels_and_format(
    level: str, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(level)
    child = logging.getLogger("aie.tests")
    child.log(getattr(logging, level), "Setup check.")
    output = capsys.readouterr().err
    assert f" | {level} | aie.tests | Setup check." in output
    assert output[:4].isdigit()  # Timestamp precedes the level and module.


def test_logging_does_not_duplicate_handlers() -> None:
    logger = configure_logging("INFO")
    configure_logging("DEBUG")
    assert len(logger.handlers) == 1
    assert logger.level == logging.DEBUG


def test_main_success_output(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(application, "load_settings", lambda: settings)
    assert application.main() == 0
    assert capsys.readouterr().out == (
        "Aviator Intelligence Engine\n"
        "Phase: 6\nStatus: INITIALIZED\nDatabase: READY\n"
        "Schema version: 1\nRounds stored: 0\nEnvironment: DEVELOPMENT\n"
    )


def test_main_failure_does_not_expose_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = "private-test-value"
    monkeypatch.setenv("AIE_LOG_LEVEL", marker)
    monkeypatch.setattr(application, "load_settings", lambda: load_settings(tmp_path))
    assert application.main() == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "Startup failed" in output.err
    assert marker not in output.err


def test_cli_startup_from_another_directory(tmp_path: Path) -> None:
    # Copy only runtime code so the subprocess cannot touch the real project data.
    project = tmp_path / "app"
    project.mkdir()
    for package in ("config", "database"):
        shutil.copytree(
            PROJECT_ROOT / package, project / package,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
    shutil.copy2(PROJECT_ROOT / "main.py", project / "main.py")
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith("AIE_") and key != "PYTHON_DOTENV_DISABLED"
    }
    result = subprocess.run(
        [sys.executable, str(project / "main.py")],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        "Aviator Intelligence Engine\n"
        "Phase: 6\nStatus: INITIALIZED\nDatabase: READY\n"
        "Schema version: 1\nRounds stored: 0\nEnvironment: DEVELOPMENT\n"
    )
    assert (project / "data" / "database" / "aie.sqlite3").is_file()
    assert not (tmp_path / "data").exists()
