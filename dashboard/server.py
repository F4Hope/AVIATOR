"""Local dashboard server; serve allowlisted assets and read-only aggregates."""

from contextlib import closing
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
from threading import BoundedSemaphore
from urllib.parse import parse_qs, urlsplit

from analysis.descriptive import AnalysisDataError, AnalysisFilters, analyze_database
from analysis.reporting import report_to_dict
from config.settings import DEVELOPMENT_PHASE, Settings
from database.database import connect_database_readonly
from database.migrations import SchemaVersionError, verify_schema
from database.models import identifier


STATIC_ROOT = Path(__file__).resolve().parent / "static"
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
MAX_SOURCE_SUGGESTIONS = 1000
MAX_REQUEST_TARGET = 4096
logger = logging.getLogger("aie.dashboard.server")


def dashboard_metadata(settings: Settings) -> dict[str, object]:
    """Read schema and bounded source suggestions from one transaction."""
    with closing(connect_database_readonly(settings)) as connection:
        connection.execute("BEGIN")
        try:
            version = verify_schema(connection)
            source_rows = connection.execute(
                "SELECT DISTINCT source FROM rounds ORDER BY source LIMIT ?", (MAX_SOURCE_SUGGESTIONS + 1,),
            ).fetchall()
            sources = [row[0] for row in source_rows[:MAX_SOURCE_SUGGESTIONS]]
            if any(identifier(value, "source") != value for value in sources):
                raise AnalysisDataError("Stored source identities failed validation.")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    return {
        "phase": DEVELOPMENT_PHASE, "environment": settings.environment,
        "database": "READY", "schema_version": version,
        "sources": sources, "source_suggestions_truncated": len(source_rows) > MAX_SOURCE_SUGGESTIONS,
        "live_collection": "NOT_CONNECTED", "prediction_engine": "ML_EXPERIMENTS_AVAILABLE",
    }


def _request_filters(query: str) -> AnalysisFilters:
    if re.search(r"%(?![0-9a-fA-F]{2})", query):
        raise ValueError("Malformed query encoding.")
    parameters = parse_qs(query, keep_blank_values=True, strict_parsing=True, max_num_fields=3, errors="strict")
    if set(parameters) - {"source", "start", "end"} or any(len(values) != 1 for values in parameters.values()):
        raise ValueError("Only one value per supported filter is accepted.")
    return AnalysisFilters(
        source=parameters["source"][0] if "source" in parameters else None,
        start=datetime.fromisoformat(parameters["start"][0]) if "start" in parameters else None,
        end=datetime.fromisoformat(parameters["end"][0]) if "end" in parameters else None,
    )


class DashboardHTTPServer(ThreadingHTTPServer):
    """Bind to loopback and limit simultaneous analysis jobs."""

    daemon_threads = True

    def __init__(self, settings: Settings, port: int = 8000) -> None:
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("port must be an integer between 0 and 65535.")
        self.settings = settings
        self.analysis_slots = BoundedSemaphore(2)
        super().__init__(("127.0.0.1", port), DashboardHandler)
        actual_port = self.server_address[1]
        authorities = {f"127.0.0.1:{actual_port}", f"localhost:{actual_port}"}
        name = os.getenv("CODESPACE_NAME", "").lower()
        domain = os.getenv("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "app.github.dev").lower()
        if re.fullmatch(r"[a-z0-9-]{1,100}", name) and re.fullmatch(r"[a-z0-9.-]{1,200}", domain):
            authorities.add(f"{name}-{actual_port}.{domain}")
        self.allowed_authorities = frozenset(authorities)

    def handle_error(self, request, client_address) -> None:
        # Do not print tracebacks, request values, cookies, or authorization headers.
        logger.error("Dashboard request failed.")


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "AIE"
    sys_version = ""

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format: str, *args: object) -> None:
        # BaseHTTPRequestHandler normally logs raw request targets; suppress them.
        pass

    def _send(self, status: int, body: bytes, content_type: str, *, attachment: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        if attachment:
            self.send_header("Content-Disposition", 'attachment; filename="aie-analysis.json"')
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        logger.debug("Dashboard response status: %s.", status)

    def _json(self, status: int, document: dict[str, object], *, attachment: bool = False) -> None:
        body = (json.dumps(document, indent=2, allow_nan=False) + "\n").encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", attachment=attachment)

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        self.close_connection = True
        self._json(code, {"error": "HTTP_REQUEST_REJECTED"})

    def _guard_request(self, *, api: bool) -> bool:
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1 or hosts[0].lower() not in self.server.allowed_authorities:
            self._json(403, {"error": "HOST_NOT_ALLOWED"})
            return False
        if api:
            origin = self.headers.get("Origin")
            if origin is not None:
                try:
                    parsed = urlsplit(origin)
                    allowed = parsed.scheme in {"http", "https"} and parsed.netloc.lower() in self.server.allowed_authorities and parsed.path == "" and not parsed.query and not parsed.fragment
                except ValueError:
                    allowed = False
                if not allowed:
                    self._json(403, {"error": "ORIGIN_NOT_ALLOWED"})
                    return False
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                self._json(403, {"error": "ORIGIN_NOT_ALLOWED"})
                return False
        return True

    def do_GET(self) -> None:
        if len(self.path) > MAX_REQUEST_TARGET:
            self._json(414, {"error": "REQUEST_TOO_LONG"})
            return
        try:
            parsed = urlsplit(self.path)
        except ValueError:
            self._json(400, {"error": "INVALID_REQUEST"})
            return
        if parsed.scheme or parsed.netloc or parsed.fragment:
            self._json(400, {"error": "INVALID_REQUEST"})
            return
        if not self._guard_request(api=parsed.path.startswith("/api/")):
            return
        if parsed.path in STATIC_FILES:
            filename, content_type = STATIC_FILES[parsed.path]
            try:
                content = (STATIC_ROOT / filename).read_bytes()
            except OSError:
                self._json(503, {"error": "ASSET_UNAVAILABLE"})
                return
            self._send(200, content, content_type)
            return
        if parsed.path == "/api/status":
            if parsed.query:
                self._json(400, {"error": "INVALID_FILTERS"})
                return
            try:
                document = dashboard_metadata(self.server.settings)
            except (OSError, ValueError, sqlite3.Error, RuntimeError):
                self._json(503, {"error": "DATABASE_NOT_READY"})
                return
            self._json(200, document)
            return
        if parsed.path in {"/api/summary", "/api/report"}:
            try:
                filters = _request_filters(parsed.query)
            except ValueError:
                self._json(400, {"error": "INVALID_FILTERS"})
                return
            if not self.server.analysis_slots.acquire(blocking=False):
                self._json(503, {"error": "ANALYSIS_BUSY"})
                return
            try:
                report = analyze_database(self.server.settings, filters)
                document = report_to_dict(report)
            except SchemaVersionError:
                self._json(503, {"error": "DATABASE_NOT_READY"})
                return
            except AnalysisDataError:
                self._json(422, {"error": "ANALYSIS_UNAVAILABLE"})
                return
            except (OSError, ValueError, sqlite3.Error, RuntimeError):
                self._json(503, {"error": "DATABASE_UNAVAILABLE"})
                return
            finally:
                self.server.analysis_slots.release()
            self._json(200, document, attachment=parsed.path == "/api/report")
            return
        self._json(404, {"error": "NOT_FOUND"})

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_POST(self) -> None:
        self.close_connection = True
        self._json(405, {"error": "METHOD_NOT_ALLOWED"})

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST
    do_OPTIONS = do_POST
