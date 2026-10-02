"""Launch the Phase 5 development dashboard on localhost."""

import argparse
import logging
import sys

from config.logging_config import configure_logging
from config.settings import DEVELOPMENT_PHASE, load_settings
from dashboard.server import DashboardHTTPServer


logger = logging.getLogger("aie.run_dashboard")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Open the local read-only AIE dashboard.")
    parser.add_argument("--port", type=int, default=8000, help="Dashboard port, default 8000")
    parser.add_argument(
        "--host", choices=("127.0.0.1", "0.0.0.0"), default="127.0.0.1",
        help="Bind host; use 0.0.0.0 only behind a trusted reverse proxy.",
    )
    parser.add_argument(
        "--public-host", action="append", default=[],
        help="Allowed public Host header, e.g. app.example.com. Repeat as needed.",
    )
    arguments = parser.parse_args(argv)
    if not 1 <= arguments.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if sys.version_info < (3, 12):
        print("Dashboard failed: Python 3.12 or newer is required.", file=sys.stderr)
        return 1
    try:
        settings = load_settings()
        configure_logging(settings.log_level)
        with DashboardHTTPServer(settings, arguments.port, arguments.host, tuple(arguments.public_host)) as server:
            print(
                f"Aviator Intelligence Engine\nPhase: {DEVELOPMENT_PHASE}\n"
                f"Dashboard: RUNNING\nURL: http://{arguments.host}:{arguments.port}\n"
                "Press Ctrl+C to stop.", flush=True,
            )
            server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        logger.info("Dashboard stopped.")
        return 0
    except (OSError, ValueError, RuntimeError):
        configure_logging("ERROR")
        logger.error("Dashboard failed to start. Check configuration, port availability, and permissions.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
