"""Hosted Browserless collector for AIE.

Creates one persistent Browserless CDP session, publishes a short-lived
interactive Live URL for manual BetPawa login, captures visible completed
multiplier history, and emits the heartbeat consumed by the live predictor.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import signal
import sys
from urllib.parse import urlencode

from collectors.native_browser_probe import (
    NativeBrowserProbe,
    NativeBrowserProbeConfig,
)
from config.logging_config import configure_logging
from config.settings import DEVELOPMENT_PHASE, load_settings


def _endpoint(origin: str, token: str) -> str:
    if not origin.startswith(("ws://", "wss://", "http://", "https://")):
        raise ValueError("AIE_BROWSERLESS_WS_URL must use HTTP(S) or WS(S).")
    if not token or any(character.isspace() for character in token):
        raise ValueError("AIE_BROWSERLESS_TOKEN is invalid.")
    launch = base64.b64encode(
        json.dumps(
            {"args": ["--user-data-dir=/data/aie-profile"]},
            separators=(",", ":"),
        ).encode("utf-8")
    ).decode("ascii")
    separator = "&" if "?" in origin else "?"
    return origin + separator + urlencode(
        {
            "token": token,
            "launch": launch,
        }
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the hosted Browserless DOM collector for AIE."
    )
    parser.add_argument("--output", default="aviator-network-probe.jsonl")
    args = parser.parse_args(argv)

    if "/" in args.output or "\\" in args.output or not args.output.endswith(".jsonl"):
        parser.error("--output must be a plain .jsonl filename.")

    try:
        settings = load_settings()
        configure_logging(settings.log_level)
        endpoint = _endpoint(
            os.environ["AIE_BROWSERLESS_WS_URL"].strip(),
            os.environ["AIE_BROWSERLESS_TOKEN"].strip(),
        )
        target_url = os.environ.get(
            "AIE_BETPAWA_AVIATOR_URL",
            "https://www.betpawa.cm/",
        ).strip()
        probe = NativeBrowserProbe(
            NativeBrowserProbeConfig(
                target_url=target_url,
                output_path=settings.raw_data_dir / args.output,
                cdp_url=endpoint,
                dom_output_path=settings.raw_data_dir / "aviator-dom-multipliers.jsonl",
                heartbeat_path=settings.raw_data_dir / "aviator-dom-heartbeat.json",
                allow_remote_cdp=True,
                navigate_on_start=True,
                live_session_path=settings.processed_data_dir / "browser-session.json",
                live_url_timeout_ms=7_200_000,
            )
        )
    except (KeyError, OSError, ValueError):
        print(
            "Hosted collector failed: check Browserless and BetPawa configuration.",
            file=sys.stderr,
        )
        return 1

    def stop(signum: int, frame: object) -> None:
        probe.stop()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    print(
        "Aviator Intelligence Engine\n"
        f"Phase: {DEVELOPMENT_PHASE}\n"
        "Hosted collector: RUNNING\n"
        "Interactive browser session is published only through the protected AIE dashboard."
    )
    try:
        probe.run()
    except (OSError, RuntimeError, ValueError) as exc:
        logging.getLogger("aie.probe_aviator_hosted").error(
            "Hosted collector stopped (%s).",
            type(exc).__name__,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
