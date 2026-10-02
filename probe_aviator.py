"""CLI for sanitized discovery of observable BetPawa Aviator browser traffic."""

import argparse
import logging
import os
from pathlib import Path
import signal
import sys

from collectors.browser_probe import BrowserProbe, BrowserProbeConfig
from config.logging_config import configure_logging
from config.settings import DEVELOPMENT_PHASE, load_settings


DEFAULT_SOURCE = "BETPAWA_CM_AVIATOR"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Observe sanitized browser traffic for the authenticated Aviator page."
    )
    parser.add_argument(
        "--output",
        default="aviator-network-probe.jsonl",
        help="Plain JSONL filename under data/raw.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if "/" in args.output or "\\" in args.output or not args.output.endswith(".jsonl"):
        print("Probe failed: --output must be a plain .jsonl filename.", file=sys.stderr)
        return 2

    try:
        settings = load_settings()
        configure_logging(settings.log_level)
        ws_origin = os.environ["AIE_BROWSERLESS_WS_URL"].strip()
        token = os.environ["AIE_BROWSERLESS_TOKEN"].strip()
        target_url = os.environ["AIE_BETPAWA_AVIATOR_URL"].strip()
        probe = BrowserProbe(
            BrowserProbeConfig(
                ws_origin=ws_origin,
                token=token,
                target_url=target_url,
                output_path=settings.raw_data_dir / args.output,
            )
        )
    except (KeyError, OSError, ValueError):
        print(
            "Probe failed: set AIE_BROWSERLESS_WS_URL, AIE_BROWSERLESS_TOKEN, "
            "and AIE_BETPAWA_AVIATOR_URL.",
            file=sys.stderr,
        )
        return 1

    def stop_probe(signum: int, frame: object) -> None:
        probe.stop()

    signal.signal(signal.SIGINT, stop_probe)
    signal.signal(signal.SIGTERM, stop_probe)

    print(
        "Aviator Intelligence Engine\n"
        f"Phase: {DEVELOPMENT_PHASE}\n"
        "Browser probe: RUNNING\n"
        f"Source label: {DEFAULT_SOURCE}\n"
        f"Sanitized output: {probe.config.output_path}\n"
        "No cookies, headers, query strings, passwords, tokens, or raw payload values are saved."
    )
    try:
        probe.run()
    except (OSError, RuntimeError, ValueError):
        logging.getLogger("aie.probe_aviator").error(
            "Browser probe stopped unexpectedly. Check the browser service and authenticated session."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
