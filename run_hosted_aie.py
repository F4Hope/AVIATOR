"""Railway/hosted supervisor for dashboard, collector, and predictor."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time


CHILDREN: dict[str, subprocess.Popen[bytes]] = {}
STOPPING = False


def _command(name: str) -> list[str]:
    python = sys.executable
    if name == "dashboard":
        port = os.environ.get("PORT", "8000").strip() or "8000"
        return [python, "run_dashboard.py", "--host", "0.0.0.0", "--port", port]
    if name == "collector":
        return [python, "probe_aviator_hosted.py"]
    if name == "predictor":
        return [python, "run_live_predictions.py"]
    raise ValueError(name)


def _start(name: str) -> None:
    CHILDREN[name] = subprocess.Popen(_command(name))


def _stop(signum: int, frame: object) -> None:
    global STOPPING
    STOPPING = True
    for process in CHILDREN.values():
        if process.poll() is None:
            process.terminate()


def main() -> int:
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    initialized = subprocess.run(
        [sys.executable, "main.py"],
        check=False,
    )
    if initialized.returncode != 0:
        return initialized.returncode

    for name in ("dashboard", "collector", "predictor"):
        _start(name)

    try:
        while not STOPPING:
            time.sleep(1)
            dashboard = CHILDREN["dashboard"]
            if dashboard.poll() is not None:
                return dashboard.returncode or 1

            for name in ("collector", "predictor"):
                process = CHILDREN[name]
                if process.poll() is not None:
                    time.sleep(2)
                    if not STOPPING:
                        _start(name)
    finally:
        for process in CHILDREN.values():
            if process.poll() is None:
                process.terminate()
        deadline = time.time() + 10
        for process in CHILDREN.values():
            remaining = max(0.0, deadline - time.time())
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                process.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
