"""Render web-service entry point for the Aviator Intelligence Engine."""

from __future__ import annotations

import os

from main import initialize_application
from run_dashboard import main as run_dashboard


def main() -> int:
    """Initialize persistent state, then serve the dashboard on Render's port."""
    initialize_application()
    port = os.environ.get("PORT", "10000").strip() or "10000"
    return run_dashboard(["--host", "0.0.0.0", "--port", port])


if __name__ == "__main__":
    raise SystemExit(main())
