"""Phase 5 HTTP checks use synthetic fixtures and temporary databases only."""

from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
import http.client
import json
from pathlib import Path
import sqlite3
from threading import Thread
from typing import Iterator
from urllib.parse import urlencode

import pytest

import analysis.descriptive as descriptive
from config.settings import Settings
from database.database import connect_database
from database.migrations import initialize_schema
from database.models import RoundRecord
from database.repository import RoundRepository
import dashboard.server as dashboard
from dashboard.server import DashboardHTTPServer, dashboard_metadata
import run_dashboard as command


BASE_TIME = datetime(2025, 1, 1, 12, tzinfo=UTC)


def seed(settings: Settings, values: list[str] | None = None, *, source: str = "TEST_FIXTURE") -> None:
    with closing(connect_database(settings)) as connection:
        initialize_schema(connection)
        RoundRepository(connection).insert_many([
            RoundRecord(
                source=source, round_id=f"fixture-{index}", multiplier=value,
                timestamp=BASE_TIME + timedelta(seconds=index),
                collection_timestamp=BASE_TIME + timedelta(seconds=index + 1),
                raw_data={"fixture_value": "DO_NOT_EXPOSE_PAYLOAD"},
            )
            for index, value in enumerate(values or [])
        ])


@contextmanager
def running(settings: Settings) -> Iterator[DashboardHTTPServer]:
    with DashboardHTTPServer(settings, port=0) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()


def request(server: DashboardHTTPServer, target: str, method: str = "GET", headers: dict | None = None) -> tuple[int, dict[str, str], bytes]:
    with closing(http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)) as connection:
        connection.request(method, target, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()


@pytest.mark.parametrize("target,content_type", [
    ("/", "text/html"), ("/index.html", "text/html"),
    ("/styles.css", "text/css"), ("/app.js", "text/javascript"),
])
def test_static_assets_and_security_headers(settings: Settings, target: str, content_type: str) -> None:
    with running(settings) as server:
        status, headers, body = request(server, target)
    assert status == 200 and body
    assert headers["Content-Type"].startswith(content_type)
    assert int(headers["Content-Length"]) == len(body)
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    assert "Access-Control-Allow-Origin" not in headers
    assert not settings.data_dir.exists()


@pytest.mark.parametrize("target", ["/.env", "/data/database/aie.sqlite3", "/../README.md", "/%2e%2e/README.md", "/dashboard/static/", "/api/rounds"])
def test_unlisted_paths_cannot_expose_files(settings: Settings, target: str) -> None:
    with running(settings) as server:
        status, _, body = request(server, target)
    assert status == 404 and json.loads(body) == {"error": "NOT_FOUND"}


def test_missing_database_returns_errors_without_creating_paths(settings: Settings) -> None:
    with running(settings) as server:
        assert request(server, "/")[0] == 200
        for target in ("/api/status", "/api/summary", "/api/report"):
            status, _, body = request(server, target)
            assert status == 503 and "error" in json.loads(body)
    assert not settings.data_dir.exists()


def test_empty_database_has_real_empty_states(settings: Settings) -> None:
    seed(settings)
    with running(settings) as server:
        status, _, body = request(server, "/api/status")
        metadata = json.loads(body)
        assert status == 200 and metadata["sources"] == []
        assert metadata["phase"] == 5 and metadata["schema_version"] == 1
        assert metadata["prediction_engine"] == "NOT_IMPLEMENTED"
        assert metadata["live_collection"] == "NOT_CONNECTED"
        status, _, body = request(server, "/api/summary")
    report = json.loads(body)
    assert status == 200 and report["status"] == "NO_DATA"
    assert report["total_stored"] == report["selected_rounds"] == 0
    assert report["multipliers"]["mean"] is None
    assert all(bucket["count"] == 0 and bucket["percentage"] is None for bucket in report["buckets"])


def test_summary_and_download_preserve_database_and_exclude_payloads(settings: Settings) -> None:
    seed(settings, ["1.25", "2", "5", "10"])
    original = settings.database_path.read_bytes()
    before_files = set(settings.data_dir.rglob("*"))
    with running(settings) as server:
        status, _, body = request(server, "/api/summary")
        report = json.loads(body)
        assert status == 200 and report["status"] == "COMPLETE"
        assert report["total_stored"] == report["selected_rounds"] == 4
        assert report["multipliers"]["mean"] == "4.5625"
        assert [bucket["count"] for bucket in report["buckets"]] == [1, 1, 1, 1]
        assert report["quality"]["rows_with_raw_data"] == 4
        assert b"DO_NOT_EXPOSE_PAYLOAD" not in body and b"fixture-0" not in body
        status, headers, body = request(server, "/api/report")
        assert status == 200 and headers["Content-Disposition"] == 'attachment; filename="aie-analysis.json"'
        downloaded = json.loads(body)
        report.pop("generated_at")
        downloaded.pop("generated_at")
        assert downloaded == report
        assert request(server, "/api/status")[0] == 200
    assert settings.database_path.read_bytes() == original
    assert set(settings.data_dir.rglob("*")) == before_files


def test_exact_source_and_half_open_utc_filters(settings: Settings) -> None:
    source = "TEST_FIXTURE & source'; --"
    seed(settings, ["1", "2", "3"], source=source)
    seed(settings, ["10"], source="OTHER_TEST_FIXTURE")
    query = urlencode({"source": source, "start": "2025-01-01T13:00:01+01:00", "end": "2025-01-01T12:00:02Z"})
    with running(settings) as server:
        status, _, body = request(server, "/api/summary?" + query)
        report = json.loads(body)
        assert status == 200 and report["selected_rounds"] == 1 and report["total_stored"] == 4
        assert report["multipliers"]["mean"] == "2"
        assert report["filters"]["start"] == "2025-01-01T12:00:01.000000Z"
        assert json.loads(request(server, "/api/summary?source=ABSENT_TEST_FIXTURE")[2])["status"] == "NO_DATA"


@pytest.mark.parametrize("query", [
    "source=", "source=%0Asecret", "source=A&source=B", "unknown=value", "start=invalid",
    "start=2025-01-01T12:00:00", "start=2025-01-02T00:00:00Z&end=2025-01-01T00:00:00Z",
    "source=%GG", "source=%FF", "source", "source=A&start=&end=", "source=A&start=A&end=B&unknown=C",
])
def test_invalid_queries_have_fixed_errors(settings: Settings, query: str) -> None:
    seed(settings)
    with running(settings) as server:
        status, _, body = request(server, "/api/summary?" + query)
    assert status == 400 and json.loads(body) == {"error": "INVALID_FILTERS"}


def test_status_endpoint_rejects_query(settings: Settings) -> None:
    with running(settings) as server:
        assert request(server, "/api/status?source=ignored")[0] == 400


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def test_mutating_methods_refused(settings: Settings, method: str) -> None:
    seed(settings, ["1.25"])
    before = settings.database_path.read_bytes()
    with running(settings) as server:
        status, _, body = request(server, "/api/summary", method=method)
    assert status == 405 and json.loads(body) == {"error": "METHOD_NOT_ALLOWED"}
    assert settings.database_path.read_bytes() == before


def test_head_has_no_body_and_matching_length(settings: Settings) -> None:
    with running(settings) as server:
        _, headers, body = request(server, "/")
        status, head_headers, head_body = request(server, "/", method="HEAD")
    assert status == 200 and head_body == b""
    assert head_headers["Content-Length"] == headers["Content-Length"] == str(len(body))


def test_foreign_host_and_origins_rejected(settings: Settings) -> None:
    seed(settings)
    with running(settings) as server:
        assert request(server, "/", headers={"Host": "untrusted.example"})[0] == 403
        for headers in ({"Origin": "https://untrusted.example"}, {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"}):
            assert request(server, "/api/summary", headers=headers)[0] == 403
        origin = f"http://127.0.0.1:{server.server_address[1]}"
        assert request(server, "/api/summary", headers={"Origin": origin})[0] == 200


def test_codespace_forwarded_host_supported(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODESPACE_NAME", "test-fixture-codespace")
    monkeypatch.setenv("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "app.github.dev")
    seed(settings)
    with running(settings) as server:
        host = f"test-fixture-codespace-{server.server_address[1]}.app.github.dev"
        assert server.server_address[0] == "127.0.0.1"
        assert request(server, "/api/summary", headers={"Host": host, "Origin": "https://" + host})[0] == 200
        assert request(server, "/api/summary", headers={"Host": "another-8000.app.github.dev"})[0] == 403


def test_request_values_and_authentication_not_logged(settings: Settings, caplog: pytest.LogCaptureFixture) -> None:
    seed(settings)
    marker = "DO_NOT_LOG_TEST_FIXTURE"
    with caplog.at_level("DEBUG", logger="aie.dashboard.server"), running(settings) as server:
        request(server, "/api/summary?unknown=" + marker, headers={"Authorization": marker, "Cookie": marker})
        request(server, "/api/summary?source=" + marker)
    assert marker not in caplog.text
    assert "Dashboard response status" in caplog.text


@pytest.mark.parametrize("version", [0, 99])
def test_incompatible_schema_is_not_migrated(settings: Settings, version: int) -> None:
    with closing(connect_database(settings)) as connection:
        connection.execute(f"PRAGMA user_version = {version}")  # Fixed test parameters only.
    before = settings.database_path.read_bytes()
    with running(settings) as server:
        assert request(server, "/api/summary")[0] == 503
        assert request(server, "/api/status")[0] == 503
    assert settings.database_path.read_bytes() == before


def test_oversized_selection_is_refused(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    seed(settings, ["1", "2"])
    monkeypatch.setattr(descriptive, "MAX_ANALYSIS_ROUNDS", 1)
    with running(settings) as server:
        status, _, body = request(server, "/api/summary")
    assert status == 422 and json.loads(body) == {"error": "ANALYSIS_UNAVAILABLE"}


def test_invalid_stored_record_has_safe_error_and_is_preserved(settings: Settings) -> None:
    seed(settings, ["1"])
    with closing(connect_database(settings)) as connection:
        row = dict(connection.execute("SELECT * FROM rounds").fetchone())
        row.update(id=2, round_id="INVALID_TEST_FIXTURE", multiplier="0.5")
        connection.execute("PRAGMA ignore_check_constraints = ON")
        columns = ", ".join(row)  # Schema-defined column names from the test database.
        placeholders = ", ".join("?" for _ in row)
        connection.execute(f"INSERT INTO rounds ({columns}) VALUES ({placeholders})", tuple(row.values()))
    before = settings.database_path.read_bytes()
    with running(settings) as server:
        status, _, body = request(server, "/api/summary")
    assert status == 422 and json.loads(body) == {"error": "ANALYSIS_UNAVAILABLE"}
    assert settings.database_path.read_bytes() == before


def test_busy_analysis_returns_retryable_error(settings: Settings) -> None:
    seed(settings)
    with running(settings) as server:
        server.analysis_slots.acquire()
        server.analysis_slots.acquire()
        try:
            status, _, body = request(server, "/api/summary")
            assert status == 503 and json.loads(body) == {"error": "ANALYSIS_BUSY"}
        finally:
            server.analysis_slots.release()
            server.analysis_slots.release()
        assert request(server, "/api/summary")[0] == 200


def test_source_suggestions_are_bounded_without_limiting_source_filter(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    seed(settings, ["1"], source="A_TEST_FIXTURE")
    seed(settings, ["2"], source="B_TEST_FIXTURE")
    monkeypatch.setattr(dashboard, "MAX_SOURCE_SUGGESTIONS", 1)
    assert dashboard_metadata(settings)["sources"] == ["A_TEST_FIXTURE"]
    assert dashboard_metadata(settings)["source_suggestions_truncated"] is True
    with running(settings) as server:
        report = json.loads(request(server, "/api/summary?source=B_TEST_FIXTURE")[2])
    assert report["selected_rounds"] == 1 and report["multipliers"]["mean"] == "2"


def test_missing_asset_has_safe_error(settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(dashboard, "STATIC_ROOT", tmp_path / "absent")
    with running(settings) as server:
        status, _, body = request(server, "/")
    assert status == 503 and json.loads(body) == {"error": "ASSET_UNAVAILABLE"}


def test_long_request_target_refused(settings: Settings) -> None:
    with running(settings) as server:
        assert request(server, "/api/summary?source=" + "A" * dashboard.MAX_REQUEST_TARGET)[0] == 414


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_launch_command_rejects_invalid_ports(port: int) -> None:
    with pytest.raises(SystemExit) as error:
        command.main(["--port", str(port)])
    assert error.value.code == 2


def test_launch_port_conflict_is_safe(settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(command, "load_settings", lambda: settings)
    with running(settings) as server:
        assert command.main(["--port", str(server.server_address[1])]) == 1
    assert "failed to start" in capsys.readouterr().err
    assert not settings.data_dir.exists()


def test_launch_and_interrupt_close_the_server_without_database_writes(settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    opened = []

    class InterruptServer(DashboardHTTPServer):
        def __init__(self, settings: Settings, port: int) -> None:
            super().__init__(settings, port=0)  # Avoid relying on a free fixed port in this test.
            opened.append(self)

        def serve_forever(self, poll_interval: float = 0.5) -> None:
            raise KeyboardInterrupt

    monkeypatch.setattr(command, "load_settings", lambda: settings)
    monkeypatch.setattr(command, "DashboardHTTPServer", InterruptServer)
    assert command.main([]) == 0
    output = capsys.readouterr().out
    assert "Phase: 5\nDashboard: RUNNING\nURL: http://127.0.0.1:8000" in output
    assert opened[0].fileno() == -1
    assert not settings.data_dir.exists()
